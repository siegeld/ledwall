"""Sessions, principals, and scoped API tokens."""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from marquee_core.models import ApiToken, User

from .config import settings
from .db import get_db

COOKIE = "marquee_session"
SESSION_TTL_S = 8 * 3600
REMEMBER_TTL_S = 30 * 24 * 3600
TOKEN_PREFIX = "mrq_"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="marquee-session")


def issue_session(username: str) -> str:
    return _serializer().dumps({"u": username})


def read_session(raw: str, max_age: int) -> str | None:
    try:
        return _serializer().loads(raw, max_age=max_age)["u"]
    except (BadSignature, KeyError, TypeError):
        return None


@dataclass
class Principal:
    name: str
    role: str = "viewer"
    scopes: tuple[str, ...] = ()
    kind: str = "user"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin" or "admin" in self.scopes


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def mint_token() -> tuple[str, str]:
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def _from_bearer(raw: str, db: Session) -> Principal | None:
    tok = db.scalar(select(ApiToken).where(ApiToken.token_hash == hash_token(raw)))
    if not tok or tok.revoked:
        return None
    now = dt.datetime.now(dt.timezone.utc)
    if tok.expires and tok.expires < now:
        return None
    # Throttled: stamping last_used on every request would write on every poll.
    if not tok.last_used or (now - tok.last_used).total_seconds() > 60:
        tok.last_used = now
        db.commit()
    scopes = tuple(s.strip() for s in tok.scopes.split(",") if s.strip())
    return Principal(name=f"token:{tok.name}", role="admin" if "admin" in scopes
                     else "viewer", scopes=scopes, kind="token")


def current_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        p = _from_bearer(auth[7:].strip(), db)
        if p:
            return p
        raise HTTPException(401, "invalid token")
    raw = request.cookies.get(COOKIE)
    if raw:
        for age in (REMEMBER_TTL_S,):
            u = read_session(raw, age)
            if u:
                user = db.scalar(select(User).where(User.username == u))
                if user:
                    return Principal(name=user.username, role=user.role,
                                     scopes=("read", "write", "admin")
                                     if user.role == "admin" else ("read",))
    raise HTTPException(401, "not authenticated")


def require_admin(p: Principal = Depends(current_principal)) -> Principal:
    if not p.is_admin:
        raise HTTPException(403, "not authorized for this system")
    return p


def require_write(p: Principal = Depends(current_principal)) -> Principal:
    if p.is_admin or "write" in p.scopes:
        return p
    raise HTTPException(403, "write scope required")
