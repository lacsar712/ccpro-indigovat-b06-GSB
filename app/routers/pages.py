from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jinja2.utils import markupsafe
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.auth import get_current_user
from app.db import get_db
from app.models import DipLot, Vat, Workshop
from app.services.vat_rules import VatRuleError, validate_vat_status_change

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _tojson(value):
    return markupsafe.Markup(json.dumps(value, ensure_ascii=False))


templates.env.filters["tojson"] = _tojson

STATUS_LABELS = {
    Vat.STATUS_IDLE: "闲置",
    Vat.STATUS_REDUCING: "还原中",
    Vat.STATUS_READY: "可染色",
}
# 固定顺序，供筛选下拉与对账条复用
STATUS_ORDER = [Vat.STATUS_REDUCING, Vat.STATUS_READY, Vat.STATUS_IDLE]


def render(request: Request, name: str, context: dict, status_code: int = 200):
    ctx = {k: v for k, v in context.items() if k != "request"}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _need_login(request: Request, db: Session):
    return get_current_user(request, db)


def _parse_int(raw: Optional[str]) -> Optional[int]:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_status(raw: Optional[str]) -> Optional[str]:
    """状态筛：仅接受三种合法状态，其余一律视为不筛。"""
    if raw is None:
        return None
    raw = raw.strip()
    if raw in STATUS_LABELS:
        return raw
    return None


def _spark_points(lots: list[DipLot], width: int = 72, height: int = 28) -> list[dict]:
    """把 redox 序列压成 sparkline 坐标（无有效读数则空）。"""
    vals = [float(l.redoxMv) for l in lots if l.redoxMv is not None]
    if not vals:
        return []
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = 0 if n == 1 else round(i * (width - 1) / (n - 1), 2)
        y = round(height - 1 - ((v - lo) / span) * (height - 1), 2)
        pts.append({"x": x, "y": y})
    return pts


def _vat_payload(vat: Vat) -> dict:
    lots = sorted(vat.lots, key=lambda x: (x.dippedAt, x.id))
    chronological = lots
    latest = lots[-1] if lots else None
    recent = list(reversed(lots[-8:]))  # 展开区展示近几笔
    return {
        "id": vat.id,
        "code": vat.code,
        "dyeType": vat.dyeType,
        "volumeL": float(vat.volumeL),
        "status": vat.status,
        "statusLabel": STATUS_LABELS.get(vat.status, vat.status),
        "workshopId": vat.workshop_id,
        "workshopName": vat.workshop.name if vat.workshop else "",
        "lastRedox": float(latest.redoxMv) if latest and latest.redoxMv is not None else None,
        "lastMeters": float(latest.clothMeters) if latest else None,
        "lastDippedAt": latest.dippedAt.strftime("%Y-%m-%d %H:%M") if latest else None,
        "spark": _spark_points(chronological),
        "recentLots": [
            {
                "id": l.id,
                "dippedAt": l.dippedAt.strftime("%Y-%m-%d %H:%M"),
                "clothMeters": float(l.clothMeters),
                "redoxMv": float(l.redoxMv) if l.redoxMv is not None else None,
            }
            for l in recent
        ],
    }


def _query_vats(
    db: Session,
    workshop_id: Optional[int],
    dye: Optional[str],
    status: Optional[str],
) -> list[Vat]:
    """按筛选条件取缸位（工坊 / 染种精确等值 / 状态精确等值）。

    仅决定「可见缸位」，与对账口径无关。
    """
    q = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .order_by(Vat.code)
    )
    if workshop_id is not None:
        q = q.filter(Vat.workshop_id == workshop_id)
    if dye is not None:
        q = q.filter(Vat.dyeType == dye)
    if status is not None:
        q = q.filter(Vat.status == status)
    return q.all()


def _reconcile(db: Session) -> dict:
    """对账四项：始终按「无筛全库」口径实时复算，不接受任何筛选入参。

    全库缸数 / 还原中数 / 可染色数 / 当日浸染笔数，均由数据库当场聚合，
    页面不得写死。当日以服务器本地自然日为准（含时区列按 UTC 存储的读数）。
    """
    total_vats = db.query(func.count(Vat.id)).scalar() or 0
    reducing_vats = (
        db.query(func.count(Vat.id)).filter(Vat.status == Vat.STATUS_REDUCING).scalar()
        or 0
    )
    ready_vats = (
        db.query(func.count(Vat.id)).filter(Vat.status == Vat.STATUS_READY).scalar() or 0
    )
    today = datetime.now(timezone.utc).date()
    day_start = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)
    lots_today = (
        db.query(func.count(DipLot.id)).filter(DipLot.dippedAt >= day_start).scalar() or 0
    )
    return {
        "total_vats": total_vats,
        "reducing_vats": reducing_vats,
        "ready_vats": ready_vats,
        "lots_today": lots_today,
    }


def _bay_results(
    db: Session,
    workshop_id: Optional[int],
    dye: Optional[str],
    status: Optional[str],
    selected_vat: Optional[int] = None,
) -> dict:
    """整页与局部刷新共用的唯一片段数据源：可见缸位（带筛）+ 对账（全库无筛）。"""
    workshops = db.query(Workshop).order_by(Workshop.name).all()
    dye_types = [r for (r,) in db.query(Vat.dyeType).distinct().order_by(Vat.dyeType).all()]
    vats = _query_vats(db, workshop_id, dye, status)
    return {
        "workshops": [{"id": w.id, "name": w.name, "region": w.region} for w in workshops],
        "dye_types": dye_types,
        "vats": [_vat_payload(v) for v in vats],
        "reconcile": _reconcile(db),
        "filter_workshop": workshop_id,
        "filter_dye": dye,
        "filter_status": status,
        "selected_vat": selected_vat,
        "status_labels": STATUS_LABELS,
        "status_order": STATUS_ORDER,
    }


def _bay_context(
    request: Request,
    db: Session,
    user,
    workshop_id: Optional[int] = None,
    dye: Optional[str] = None,
    status: Optional[str] = None,
    selected_vat: Optional[int] = None,
    error: Optional[str] = None,
):
    # 始终下发全部缸位；工坊/染种/状态仅作前端可见筛选，避免切回「全部」时缺数据
    all_vats = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .order_by(Vat.code)
        .all()
    )
    results = _bay_results(db, workshop_id, dye, status, selected_vat)
    ctx = {
        "request": request,
        "user": user,
        "active": "bay",
        "selected_vat": selected_vat,
        "error": error,
        "all_vats": [_vat_payload(v) for v in all_vats],
    }
    ctx.update(results)
    return ctx


@router.get("/", response_class=HTMLResponse)
async def bay(
    request: Request,
    workshop: Optional[str] = None,
    dye: Optional[str] = None,
    status: Optional[str] = None,
    vat: Optional[int] = None,
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    workshop_id = _parse_int(workshop)
    dye = dye.strip() if dye and dye.strip() else None
    status = _parse_status(status)
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, workshop_id, dye, status, vat),
    )


@router.get("/bay/partial", response_class=HTMLResponse)
async def bay_partial(
    request: Request,
    workshop: Optional[str] = None,
    dye: Optional[str] = None,
    status: Optional[str] = None,
    vat: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """局部刷新：仅回传「对账条 + 可见缸位」片段。

    与整页 GET / 渲染同一模板片段、同一数据源，保证相同筛选下可见集合一致；
    对账四项始终为无筛全库实时值，筛选只改可见缸位。
    """
    user = _need_login(request, db)
    if not user:
        return HTMLResponse(status_code=401)
    workshop_id = _parse_int(workshop)
    dye = dye.strip() if dye and dye.strip() else None
    status = _parse_status(status)
    selected_vat = _parse_int(vat)
    results = _bay_results(db, workshop_id, dye, status, selected_vat)
    return render(request, "_bay_results.html", results)


def _redirect_to_bay(
    pk: int,
    workshop_id: Optional[int],
    dye: Optional[str],
    status: Optional[str],
) -> RedirectResponse:
    params = [f"vat={pk}"]
    if workshop_id is not None:
        params.append(f"workshop={workshop_id}")
    if dye:
        params.append(f"dye={dye}")
    if status:
        params.append(f"status={status}")
    return RedirectResponse("/?" + "&".join(params), status_code=303)


@router.post("/bay/vats/{pk}/status", response_class=HTMLResponse)
async def bay_vat_status(
    pk: int,
    request: Request,
    status: str = Form(...),
    workshop: str = Form(""),
    dye: str = Form(""),
    statusFilter: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .filter(Vat.id == pk)
        .first()
    )
    ws = _parse_int(workshop)
    dye_val = dye.strip() or None
    status_filter = _parse_status(statusFilter)
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        latest = item.latest_lot()
        validate_vat_status_change(item, status, latest)
        item.status = status
        db.commit()
        return _redirect_to_bay(pk, ws, dye_val, status_filter)
    except VatRuleError as exc:
        error = exc.message
        db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, dye_val, status_filter, pk, error),
        status_code=400,
    )


@router.post("/bay/vats/{pk}/lots", response_class=HTMLResponse)
async def bay_log_lot(
    pk: int,
    request: Request,
    dippedAt: str = Form(...),
    clothMeters: str = Form(...),
    redoxMv: str = Form(""),
    workshop: str = Form(""),
    dye: str = Form(""),
    statusFilter: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = db.get(Vat, pk)
    ws = _parse_int(workshop)
    dye_val = dye.strip() or None
    status_filter = _parse_status(statusFilter)
    if not item:
        return RedirectResponse("/", status_code=303)
    error = None
    try:
        dipped_at = datetime.fromisoformat(dippedAt)
        if dipped_at.tzinfo is None:
            # datetime-local 为无时区时间，与种子一致按 UTC 落库，统一当日口径
            dipped_at = dipped_at.replace(tzinfo=timezone.utc)
        lot = DipLot(
            vat_id=pk,
            dippedAt=dipped_at,
            clothMeters=Decimal(clothMeters),
            redoxMv=Decimal(redoxMv) if redoxMv.strip() else None,
        )
        db.add(lot)
        db.commit()
        return _redirect_to_bay(pk, ws, dye_val, status_filter)
    except (ValueError, InvalidOperation) as exc:
        error = f"浸染记录无效：{exc}"
        db.rollback()
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, dye_val, status_filter, pk, error),
        status_code=400,
    )


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)
