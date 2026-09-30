from functools import wraps

from fastapi import Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.models import User
from app.seed import verify_password


def get_current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


def login_user(request: Request, user: User) -> None:
    request.session["user_id"] = user.id
    request.session["username"] = user.username


def logout_user(request: Request) -> None:
    request.session.clear()


def authenticate(db: Session, username: str, password: str) -> User | None:
    user = db.query(User).filter_by(username=username).first()
    if not user or not verify_password(password, user.password_hash):
        return None
    return user


def require_login(view):
    @wraps(view)
    async def wrapper(request: Request, *args, **kwargs):
        db = kwargs.get("db")
        if db is None:
            return RedirectResponse("/login", status_code=303)
        user = get_current_user(request, db)
        if not user:
            return RedirectResponse("/login", status_code=303)
        request.state.user = user
        return await view(request, *args, **kwargs)

    return wrapper
