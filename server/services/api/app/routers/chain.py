"""Card chains: see what is actually cabled behind a head card.

Cards daisy-chain through their second Ethernet port and a whole chain is one
IP address, the head's. Member h answers at <head>:8000+h (see
marquee_core.models.Card.api). Which cards exist and where they sit is in the
database; what is actually PLUGGED IN is only known by asking the hardware,
so this walks it: hop 1, hop 2, ... until a card does not answer or reports no
link on its downstream port.

The walk runs here, on the sender, because the panel bus is private: a browser
elsewhere cannot reach the cards, and the card's own status page (which walks
the chain from the browser when it is opened directly) cannot do it through
the controller relay.
"""
from __future__ import annotations

import json
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from marquee_core.models import Card

from ..db import get_db

router = APIRouter(prefix="/api/v1", tags=["chain"])

MAX_HOPS = 63


def _status(api: str, timeout: float = 1.5) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{api}/api/status", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _summary(st: dict) -> dict:
    ch = st.get("chain") or {}
    return {
        "mac": st.get("mac"),
        "version": st.get("version"),
        "gateware": st.get("gateware"),
        "width": st.get("display_width"),
        "height": st.get("display_height"),
        "role": ch.get("role"),
        "port1_link": ch.get("port1_link"),
        "port1_speed": ch.get("port1_speed"),
        "fwd_frames": ch.get("fwd_frames"),
        "up_frames": ch.get("up_frames"),
        "up_dropped": ch.get("up_dropped"),
    }


@router.get("/cards/{cid}/chain")
def card_chain(cid: int, db: Session = Depends(get_db)):
    """The card's own chain status, read live from the card."""
    card = db.get(Card, cid)
    if not card:
        raise HTTPException(404, "no such card")
    st = _status(card.api, timeout=3)
    if st is None:
        return {"card": card.name, "reachable": False}
    return {"card": card.name, "reachable": True, "hop": card.chain_hop,
            "chain_supported": bool(st.get("chain")), **_summary(st)}


@router.get("/cards/{cid}/chain/scan")
def scan_chain(cid: int, db: Session = Depends(get_db)):
    """Walk the chain behind a head card and match what answers to the DB.

    Each hop is reported with the card record it belongs to (by MAC), so the
    UI can show what is cabled but not registered -- and what is registered
    at a hop but answers with a different MAC, i.e. was moved.
    """
    head = db.get(Card, cid)
    if not head:
        raise HTTPException(404, "no such card")
    if head.chain_hop:
        raise HTTPException(400, f"{head.name} is hop {head.chain_hop}; scan from its head")
    by_mac = {(c.mac or "").lower(): c for c in db.scalars(select(Card)) if c.mac}
    by_hop = {c.chain_hop: c for c in db.scalars(
        select(Card).where(Card.host == head.host, Card.chain_hop > 0))}

    st = _status(head.api, timeout=3)
    out = {"head": head.name, "host": head.host, "reachable": st is not None,
           "chain_supported": bool(st and st.get("chain")), "hops": []}
    if not st or not st.get("chain"):
        return out
    out["head_status"] = _summary(st)
    link = bool(st["chain"].get("port1_link"))
    hop = 1
    while link and hop <= MAX_HOPS:
        st = _status(f"{head.host}:{8000 + hop}")
        if st is None:
            out["hops"].append({"hop": hop, "answered": False,
                                "registered": _rec(by_hop.get(hop))})
            break
        s = _summary(st)
        rec = by_mac.get((s["mac"] or "").lower())
        expected = by_hop.get(hop)
        out["hops"].append({
            "hop": hop, "answered": True, **s,
            "registered": _rec(rec),
            # A record says hop h, but a different card answers there.
            "mismatch": bool(expected and rec is not expected),
            "registered_here": _rec(expected),
        })
        link = bool(s["port1_link"])
        hop += 1
    # Records behind this head that nothing answered for.
    seen = {h["hop"] for h in out["hops"] if h.get("answered")}
    out["missing"] = [_rec(c) | {"hop": h} for h, c in sorted(by_hop.items()) if h not in seen]
    return out


def _rec(c: Card | None) -> dict | None:
    if c is None:
        return None
    return {"id": c.id, "name": c.name, "hop": c.chain_hop, "mac": c.mac}


@router.get("/chains")
def chains(db: Session = Depends(get_db)):
    """Every chain in the database: heads with the members registered behind
    them, in hop order. Pure DB -- no card is contacted."""
    cards = db.scalars(select(Card).order_by(Card.host, Card.chain_hop)).all()
    heads: dict[str, dict] = {}
    for c in cards:
        if c.chain_hop == 0:
            heads[c.host] = {"head": _rec(c), "members": []}
    orphans = []
    for c in cards:
        if c.chain_hop:
            if c.host in heads:
                heads[c.host]["members"].append(_rec(c))
            else:
                orphans.append(_rec(c))
    return {"chains": [v for v in heads.values() if v["members"]], "orphans": orphans}
