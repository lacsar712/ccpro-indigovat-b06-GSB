from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Optional
from urllib.parse import urlencode
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

VALID_STATUSES = (Vat.STATUS_IDLE, Vat.STATUS_REDUCING, Vat.STATUS_READY)


def render(request: Request, name: str, context: dict, status_code: int = 200):
    ctx = {k: v for k, v in context.items() if k != "request"}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def _need_login(request: Request, db: Session):
    return get_current_user(request, db)


def _to_int(raw) -> Optional[int]:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return None


def _norm_filters(workshop=None, dye=None, status=None):
    """统一解析三类筛选：工坊 id、染种（精确匹配）、状态。非法值视为不筛。"""
    ws = _to_int(workshop)
    dye = (dye or "").strip() or None
    status = (status or "").strip()
    if status not in VALID_STATUSES:
        status = None
    return ws, dye, status


def _filter_vats(vats, workshop_id=None, dye=None, status=None):
    """筛选只决定可见缸位；对账统计不经过这里。"""
    out = []
    for v in vats:
        if workshop_id is not None and v.workshop_id != workshop_id:
            continue
        if dye is not None and v.dyeType != dye:  # 染种精确筛：全等，不做模糊
            continue
        if status is not None and v.status != status:
            continue
        out.append(v)
    return out


def _bay_stats(db: Session) -> dict:
    """对账四项：始终按无筛全库口径复算，与筛选条件无关。"""
    return {
        "total": db.query(Vat).count(),
        "reducing": db.query(Vat).filter(Vat.status == Vat.STATUS_REDUCING).count(),
        "ready": db.query(Vat).filter(Vat.status == Vat.STATUS_READY).count(),
        "today_lots": (
            db.query(DipLot)
            .filter(func.date(DipLot.dippedAt) == func.current_date())
            .count()
        ),
    }


def _bay_redirect(selected=None, workshop_id=None, dye=None, status=None):
    """回跳还原台并保留筛选与选中缸。"""
    q = {}
    if selected is not None:
        q["vat"] = selected
    if workshop_id is not None:
        q["workshop"] = workshop_id
    if dye:
        q["dye"] = dye
    if status:
        q["status"] = status
    qs = urlencode(q)
    return RedirectResponse("/" + ("?" + qs if qs else ""), status_code=303)


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
    latest = lots[-1] if lots else None
    recent = list(reversed(lots[-8:]))  # 展开区展示近几笔
    spark = _spark_points(lots)
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
        "lastRedoxText": (
            f"{float(latest.redoxMv):g} mV"
            if latest and latest.redoxMv is not None
            else None
        ),
        "lastMeters": float(latest.clothMeters) if latest else None,
        "lastDippedAt": latest.dippedAt.strftime("%Y-%m-%d %H:%M") if latest else None,
        "spark": spark,
        "sparkLine": " ".join(f"{p['x']},{p['y']}" for p in spark),
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
    # 缸位条只渲染筛后可见集合；对账四项另走 _bay_stats 全库口径
    workshops = db.query(Workshop).order_by(Workshop.name).all()
    vats = (
        db.query(Vat)
        .options(joinedload(Vat.workshop), joinedload(Vat.lots))
        .order_by(Vat.code)
        .all()
    )
    visible = _filter_vats(vats, workshop_id, dye, status)
    visible_ids = {v.id for v in visible}
    if selected_vat not in visible_ids:
        selected_vat = None
    return {
        "request": request,
        "user": user,
        "workshops": [{"id": w.id, "name": w.name, "region": w.region} for w in workshops],
        # 全量缸位仅作展开面板数据源；缸位条（整页与局部）只用 visible_vats
        "vats": [_vat_payload(v) for v in vats],
        "visible_vats": [_vat_payload(v) for v in visible],
        "dye_options": sorted({v.dyeType for v in vats}),
        "stats": _bay_stats(db),
        "filter_workshop": workshop_id,
        "filter_dye": dye,
        "filter_status": status,
        "selected_vat": selected_vat,
        "error": error,
        "status_labels": STATUS_LABELS,
        "active": "bay",
    }


@router.get("/", response_class=HTMLResponse)
async def bay(
    request: Request,
    workshop: Optional[str] = None,
    dye: Optional[str] = None,
    status: Optional[str] = None,
    vat: Optional[str] = None,
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    ws, dye_f, status_f = _norm_filters(workshop, dye, status)
    return render(
        request,
        "bay.html",
        _bay_context(request, db, user, ws, dye_f, status_f, _to_int(vat)),
    )


@router.get("/bay/vats", response_class=HTMLResponse)
async def bay_vats_strip(
    request: Request,
    workshop: Optional[str] = None,
    dye: Optional[str] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """缸位条局部刷新：与整页共用 _bay_context 与 _vat_strip.html，同筛必同集合。"""
    user = _need_login(request, db)
    if not user:
        return HTMLResponse("未登录", status_code=401)
    ws, dye_f, status_f = _norm_filters(workshop, dye, status)
    return render(
        request,
        "_vat_strip.html",
        _bay_context(request, db, user, ws, dye_f, status_f),
    )


@router.post("/bay/vats/{pk}/status", response_class=HTMLResponse)
async def bay_vat_status(
    pk: int,
    request: Request,
    status: str = Form(...),
    workshop: str = Form(""),
    filter_dye: str = Form(""),
    filter_status: str = Form(""),
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
    ws, dye_f, status_f = _norm_filters(workshop, filter_dye, filter_status)
    if not item:
        return RedirectResponse("/", status_code=303)
    try:
        latest = item.latest_lot()
        validate_vat_status_change(item, status, latest)
        item.status = status
        db.commit()
        return _bay_redirect(pk, ws, dye_f, status_f)
    except VatRuleError as exc:
        db.rollback()
        return render(
            request,
            "bay.html",
            _bay_context(request, db, user, ws, dye_f, status_f, pk, exc.message),
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
    filter_dye: str = Form(""),
    filter_status: str = Form(""),
    db: Session = Depends(get_db),
):
    user = _need_login(request, db)
    if not user:
        return RedirectResponse("/login", status_code=303)
    item = db.get(Vat, pk)
    ws, dye_f, status_f = _norm_filters(workshop, filter_dye, filter_status)
    if not item:
        return RedirectResponse("/", status_code=303)
    try:
        lot = DipLot(
            vat_id=pk,
            dippedAt=datetime.fromisoformat(dippedAt),
            clothMeters=Decimal(clothMeters),
            redoxMv=Decimal(redoxMv) if redoxMv.strip() else None,
        )
        db.add(lot)
        db.commit()
        return _bay_redirect(pk, ws, dye_f, status_f)
    except (ValueError, InvalidOperation) as exc:
        db.rollback()
        return render(
            request,
            "bay.html",
            _bay_context(
                request, db, user, ws, dye_f, status_f, pk, f"浸染记录无效：{exc}"
            ),
            status_code=400,
        )


# 旧顶栏 CRUD 路径一律回到还原台，避免「换皮表页」残留入口
@router.get("/workshops")
@router.get("/vats")
@router.get("/lots")
@router.get("/home")
async def legacy_redirect():
    return RedirectResponse("/", status_code=303)
