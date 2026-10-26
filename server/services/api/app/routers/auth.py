"""Login: Active Directory, with a local break-glass admin.

A console must stay reachable when AD is down, so the local `admin` account
authenticates through the password path and is never auto-disabled.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Response
from passlib.hash import pbkdf2_sha256
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from marquee_core.models import Setting, User

from ..config import settings
from ..db import get_db
from ..security import (COOKIE, REMEMBER_TTL_S, SESSION_TTL_S, Principal,
                        current_principal, issue_session)

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str
    password: str
    remember: bool = False


def ensure_admin(db: Session) -> None:
    """Seed the break-glass account on first boot."""
    if db.scalar(select(User).where(User.username == "admin")):
        return
    db.add(User(username="admin", display_name="Local admin", role="admin",
                auth_source="local",
                password_hash=pbkdf2_sha256.hash(settings.admin_password)))
    db.commit()


def _ad_settings(db: Session) -> dict:
    return {s.key: s.value for s in db.scalars(select(Setting)).all()
            if s.key.startswith("ad_")}


@router.post("/login")
def login(body: LoginIn, response: Response, db: Session = Depends(get_db)):
    ensure_admin(db)
    user = db.scalar(select(User).where(User.username == body.username))

    # Local password path first -- this is the route that still works when AD is
    # unreachable, which is the entire point of the break-glass account.
    if user and user.password_hash and pbkdf2_sha256.verify(body.password, user.password_hash):
        return _grant(user, body.remember, response, db)

    cfg = _ad_settings(db)
    if cfg.get("ad_enabled") == "true":
        try:
            from ..ad_auth import ad_authenticate  # type: ignore
            info = ad_authenticate(body.username, body.password, cfg)
        except Exception:
            info = None
        if info:
            if not info.get("is_admin"):
                # System consoles are domain-admins only; do not silently
                # provision a viewer for someone who is not authorized.
                raise HTTPException(403, "not authorized for this system")
            if not user:
                user = User(username=body.username, auth_source="ad")
                db.add(user)
            user.role = "admin"
            user.display_name = info.get("display_name") or body.username
            user.email = info.get("email")
            db.commit()
            return _grant(user, body.remember, response, db)

    raise HTTPException(401, "invalid credentials")


def _grant(user: User, remember: bool, response: Response, db: Session):
    user.last_login = dt.datetime.now(dt.timezone.utc)
    db.commit()
    response.set_cookie(
        COOKIE, issue_session(user.username), httponly=True, samesite="lax",
        max_age=REMEMBER_TTL_S if remember else SESSION_TTL_S, path="/",
    )
    return {"ok": True, "username": user.username, "role": user.role}


@router.post("/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(p: Principal = Depends(current_principal)):
    return {"username": p.name, "role": p.role, "scopes": list(p.scopes),
            "kind": p.kind}
