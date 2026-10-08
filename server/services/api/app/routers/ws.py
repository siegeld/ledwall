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
            # Keepalive; the server is the one that pushes.
            await asyncio.wait_for(ws.receive_text(), timeout=60)
    except (WebSocketDisconnect, asyncio.TimeoutError, Exception):
        pass
    finally:
        _clients.discard(ws)
