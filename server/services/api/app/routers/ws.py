"""Live push. The standard forbids polling as the update mechanism."""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()
_clients: set[WebSocket] = set()


async def broadcast(kind: str, payload: dict) -> None:
    dead = []
    msg = json.dumps({"type": kind, **payload})
    for ws in list(_clients):
        try:
            await ws.send_text(msg)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _clients.discard(ws)


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)
    try:
        while True:
            # The client never sends; this only notices it going away. It
            # used to time out after 60 s of client silence -- i.e. always --
            # and drop every browser a minute after it connected.
            await ws.receive_text()
    except (WebSocketDisconnect, Exception):
        pass
    finally:
        _clients.discard(ws)


def _fingerprint() -> tuple:
    """What the console shows, minus what changes every second (last_seen).

    Most changes are not made by this process -- the player marks cards
    online/offline, another session imports shows -- so the push is driven
    by watching the database rather than by hooks in the routes. Nothing
    called broadcast() at all before this, so the UI never updated live.
    """
    from sqlalchemy import select

    from marquee_core.models import Assignment, Card, Show, Wall

    from ..db import SessionLocal
    with SessionLocal() as db:
        cards = tuple(db.execute(select(
            Card.id, Card.state, Card.mode, Card.program, Card.enabled, Card.host,
            Card.chain_hop, Card.wall_id, Card.x, Card.y, Card.width, Card.height,
            Card.rotate, Card.panel_type, Card.firmware, Card.bitstream, Card.last_error).order_by(Card.id)).all())
        walls = tuple(db.execute(select(Wall.id, Wall.enabled, Wall.width,
                                        Wall.height).order_by(Wall.id)).all())
        walls += tuple(db.execute(select(Assignment.wall_id, Assignment.show_id)
                                  .order_by(Assignment.id)).all())
        shows = tuple(db.execute(select(Show.id, Show.updated).order_by(Show.id)).all())
    return cards + walls, shows


async def watch(period_s: float = 2.0) -> None:
    """Broadcast card/show changes as they land in the database."""
    last = None
    ticks = 0
    while True:
        await asyncio.sleep(period_s)
        if not _clients:
            last = None
            continue
        # A keepalive every ~24 s: nginx closes a proxied websocket that has
        # carried nothing for its 60 s read timeout.
        ticks += 1
        if ticks % 12 == 0:
            await broadcast("ping", {})
        try:
            fp = await asyncio.to_thread(_fingerprint)
        except Exception:
            continue
        if last is not None:
            if fp[0] != last[0]:
                await broadcast("card_update", {})
            if fp[1] != last[1]:
                await broadcast("show_update", {})
        last = fp
