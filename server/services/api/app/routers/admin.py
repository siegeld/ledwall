"""Settings and API tokens."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from marquee_core.models import ApiToken, Setting

from ..db import get_db
from ..security import Principal, mint_token, require_admin

router = APIRouter(prefix="/api/v1", tags=["admin"])


class SettingIn(BaseModel):
    key: str
    value: str | None = None


@router.get("/settings")
def list_settings(db: Session = Depends(get_db), _: Principal = Depends(require_admin)):
    rows = db.scalars(select(Setting)).all()
    # Never echo a stored secret back to a browser.
    return [{"key": s.key,
             "value": "********" if "password" in s.key or "secret" in s.key else s.value}
            for s in rows]


@router.put("/settings")
def set_setting(body: SettingIn, db: Session = Depends(get_db),
                _: Principal = Depends(require_admin)):
    row = db.get(Setting, body.key)
    if row:
        row.value = body.value
    else:
        db.add(Setting(key=body.key, value=body.value))
    db.commit()
    return {"ok": True}


class TokenIn(BaseModel):
    name: str
    scopes: str = "read"
    expires_days: int | None = None


@router.get("/tokens")
def list_tokens(db: Session = Depends(get_db), _: Principal = Depends(require_admin)):
    return [{"id": t.id, "name": t.name, "prefix": t.prefix, "scopes": t.scopes,
             "created": t.created, "last_used": t.last_used, "revoked": t.revoked}
            for t in db.scalars(select(ApiToken)).all()]


@router.post("/tokens", status_code=201)
def create_token(body: TokenIn, db: Session = Depends(get_db),
                 p: Principal = Depends(require_admin)):
    raw, hashed = mint_token()
    tok = ApiToken(name=body.name, prefix=raw[:12], token_hash=hashed,
                   scopes=body.scopes, created_by=p.name)
    db.add(tok); db.commit(); db.refresh(tok)
    # Shown exactly once -- only the hash is stored.
    return {"id": tok.id, "name": tok.name, "token": raw,
            "warning": "copy this now; it cannot be shown again"}


@router.delete("/tokens/{tid}", status_code=204)
def revoke(tid: int, db: Session = Depends(get_db), _: Principal = Depends(require_admin)):
    t = db.get(ApiToken, tid)
    if not t:
        raise HTTPException(404, "no such token")
    t.revoked = True
    db.commit()
