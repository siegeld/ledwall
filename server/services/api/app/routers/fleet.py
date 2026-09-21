"""Walls, cards, shows, assignments, schedules, stats -- the control surface.

A WALL is what a viewer sees and what content is assigned to. A CARD is a
receiver board driving a rectangle of one. Content endpoints therefore address
walls; device endpoints address cards. See marquee_core.models for why the two
are not the same object.

Also the integration plane: other homelab apps POST /api/v1/play to put content
on a wall right now (Aria announcing something, a doorbell showing a camera
still, an alert taking over the lobby sign).
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from marquee_core.models import (Assignment, Card, CardStat, Schedule, Show,
                                 Wall, boot_config, resolve_show_for)
from marquee_core.scene import load_show

from ..db import get_db
from .. import shows_io

router = APIRouter(prefix="/api/v1", tags=["fleet"])


# ----- schemas -------------------------------------------------------------

class WallIn(BaseModel):
    name: str
    width: int = 256
    height: int = 128
    enabled: bool = True
    notes: str | None = None


class WallPatch(BaseModel):
    name: str | None = None
    width: int | None = None
    height: int | None = None
    enabled: bool | None = None
    notes: str | None = None


class WallOut(WallIn):
    id: int
    show_id: int | None = None
    card_ids: list[int] = []
    # Pixels covered by enabled cards. Less than width*height means the wall has
    # a gap -- worth surfacing, because the symptom is a black band nobody can
    # explain rather than an error.
    covered: int = 0

    model_config = {"from_attributes": True}


class CardIn(BaseModel):
    name: str
    wall_id: int
    host: str
    port: int = 7000
    mac: str | None = None
    width: int = 256
    height: int = 128
    x: int = 0
    y: int = 0
    packet_delay: float = 0.0008
    layout_yaml: str | None = None
    firmware: str | None = None
    # "stream" (marquee renders and sends frames) or "program" (the card
    # composes its own pixels from pushed values). Never both: they would fight
    # over the framebuffer.
    mode: str = "stream"
    program: str | None = None
    # Which S-PWM driver chip the modules carry. "" or null means the card uses
    # whatever its bitstream was built with.
    drive: str | None = None
    # Channel order the MODULES expect, one of RGB_ORDERS. "" or "RGB" is the
    # standard order and is not sent to the card at all.
    rgb_order: str | None = None
    enabled: bool = True
    notes: str | None = None


class CardPatch(BaseModel):
    """Every field optional -- this is a PATCH.

    The handler has always used exclude_unset, i.e. it was written for partial
    updates, but it validated against the full input model where name and host
    are required. So changing one field meant resending the whole record, and
    anything that tried to patch a single key got a 422. Updating one dropdown
    should not require the caller to know every other field's current value.
    """
    name: str | None = None
    wall_id: int | None = None
    host: str | None = None
    port: int | None = None
    mac: str | None = None
    width: int | None = None
    height: int | None = None
    x: int | None = None
    y: int | None = None
    packet_delay: float | None = None
    layout_yaml: str | None = None
    firmware: str | None = None
    mode: str | None = None
    program: str | None = None
    drive: str | None = None
    rgb_order: str | None = None
    enabled: bool | None = None
    notes: str | None = None


class CardOut(CardIn):
    id: int
    state: str
    last_seen: dt.datetime | None = None
    last_error: str | None = None
    # The show this card is displaying, which is a property of its WALL. Echoed
    # here so a device view does not have to join it back up.
    show_id: int | None = None

    model_config = {"from_attributes": True}


class ShowIn(BaseModel):
    name: str
    body: str
    description: str | None = None


class ShowOut(ShowIn):
    id: int
    updated: dt.datetime
    # Which walls this show reaches, standing assignment and schedule alike.
    # Editing a show changes what is on the glass NOW, and the editor had no
    # way to say so -- you could rewrite a live wall believing you were editing
    # a draft.
    walls: list[str] = []
    model_config = {"from_attributes": True}


class ScheduleIn(BaseModel):
    wall_id: int
    show_id: int
    name: str
    start_min: int = Field(ge=0, le=1439)
    end_min: int = Field(ge=0, le=1440)
    days: str = "0123456"
    priority: int = 0
    enabled: bool = True


class PlayRequest(BaseModel):
    """Put content on a wall now -- the integration entry point."""

    wall: str | None = Field(None, description="wall name or id")
    # Accepted for compatibility: callers written before walls existed pass a
    # panel name. It resolves to a wall -- by wall name first, then by card
    # name, since the migration gave every former panel a wall of the same name.
    # Other homelab apps call this endpoint; breaking them to tidy a parameter
    # name would be a poor trade.
    panel: str | None = Field(None, description="deprecated alias for `wall`")
    show: str | None = Field(None, description="existing show name")
    body: str | None = Field(None, description="inline scene YAML instead of a saved show")
    duration_s: float | None = Field(None, description="revert afterwards; null = until changed")


# ----- walls ---------------------------------------------------------------

def _404(what: str):
    raise HTTPException(404, f"no such {what}")


def _wall_show(w: Wall, db: Session) -> int | None:
    scheds = list(db.scalars(select(Schedule).where(Schedule.wall_id == w.id)).all())
    return resolve_show_for(w, scheds, dt.datetime.now())


def _wall_out(w: Wall, db: Session) -> WallOut:
    return WallOut(**{
        **{k: getattr(w, k) for k in WallIn.model_fields},
        "id": w.id, "show_id": _wall_show(w, db),
        "card_ids": [c.id for c in w.cards], "covered": w.covers(),
    })


@router.get("/walls", response_model=list[WallOut])
def list_walls(db: Session = Depends(get_db)):
    return [_wall_out(w, db)
            for w in db.scalars(select(Wall).order_by(Wall.name)).all()]


@router.post("/walls", response_model=WallOut, status_code=201)
def create_wall(body: WallIn, db: Session = Depends(get_db)):
    if db.scalar(select(Wall).where(Wall.name == body.name)):
        raise HTTPException(409, f"wall {body.name!r} already exists")
    w = Wall(**body.model_dump())
    db.add(w); db.commit(); db.refresh(w)
    return _wall_out(w, db)


@router.patch("/walls/{wid}", response_model=WallOut)
def update_wall(wid: int, body: WallPatch, db: Session = Depends(get_db)):
    w = db.get(Wall, wid) or _404("wall")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(w, k, v)
    db.commit(); db.refresh(w)
    return _wall_out(w, db)


@router.delete("/walls/{wid}", status_code=204)
def delete_wall(wid: int, db: Session = Depends(get_db)):
    """Deleting a wall deletes its cards' records too (cascade).

    That is deliberate: a card with no wall has nowhere to display and would be
    invisible in every view. Move the cards to another wall first if the boards
    are being kept.
    """
    w = db.get(Wall, wid) or _404("wall")
    db.delete(w); db.commit()


@router.get("/walls/{wid}/cards", response_model=list[CardOut])
def wall_cards(wid: int, db: Session = Depends(get_db)):
    w = db.get(Wall, wid) or _404("wall")
    return [_card_out(c, db) for c in w.cards]


class WallMode(BaseModel):
    mode: str


@router.post("/walls/{wid}/mode")
def wall_mode(wid: int, body: WallMode, db: Session = Depends(get_db)):
    """Put every card on this wall into one mode.

    Assigning content to a wall is a statement that the wall shows that
    picture, and a card running an on-board program does not. Before this, the
    only way to act on that was to open each card in turn -- nine clicks on a
    nine-card wall to say one thing -- and nothing on the Walls page even
    showed that some cards were ignoring the content.

    Mode stays a per-CARD column and this does not change that. It is
    deliberate: a card in program mode still has its pixels when the host dies,
    so one board can keep showing something true while its neighbours hold a
    frozen frame that looks perfectly healthy. That capability is worth keeping
    -- it just should not be the DEFAULT cost of choosing content.

    So: this is the bulk verb for the common case, per-card remains the
    override, and the wall reports when they disagree.
    """
    if body.mode not in ("stream", "program"):
        raise HTTPException(400, "mode must be 'stream' or 'program'")
    w = db.get(Wall, wid) or _404("wall")
    for c in w.cards:
        c.mode = body.mode
    db.commit()
    # Report which boards did not take it rather than claiming they all did.
    failed = [c.name for c in w.cards if _apply_mode(c)]
    return {"wall": w.name, "mode": body.mode, "cards": len(w.cards),
            "unreachable": failed}


def _card_programs(host: str) -> list[str] | None:
    """What one board's firmware can run, or None if it cannot be asked."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}/api/programs", timeout=3) as r:
            return list(json.loads(r.read().decode()).get("programs") or [])
    except Exception:
        return None


@router.get("/walls/{wid}/content")
def wall_content(wid: int, db: Session = Depends(get_db)):
    """Everything this wall can show -- both kinds, in one list.

    Content used to mean "a show", and a program was something you set per card
    on a different page. That split is an implementation detail leaking into
    the UI: both are answers to "what is on this wall?".

    They are NOT the same operation, though, and the difference is real rather
    than cosmetic. A show is rendered ONCE host-side and each card is sent its
    own crop, so N cards show one picture. A program runs ON each card from
    that card's own width and height -- the firmware's program context carries
    `w`, `h`, a frame counter and the value table, and NO wall size and NO
    origin -- so it cannot know it is part of something bigger. N cards run N
    independent copies. `copies` says so per entry rather than leaving someone
    to discover it on the glass.

    Programs are the INTERSECTION across the wall's cards, because one firmware
    carries every program and two cards need not be running the same build. A
    program only one card has is not something the wall can show.
    """
    w = db.get(Wall, wid) or _404("wall")
    shows = [{"kind": "show", "id": s.id, "name": s.name, "copies": 1}
             for s in db.scalars(select(Show).order_by(Show.name)).all()]

    cards = [c for c in w.cards if c.enabled]
    common: set[str] | None = None
    unreachable = []
    for c in cards:
        got = _card_programs(c.host)
        if got is None:
            unreachable.append(c.name)
            continue
        common = set(got) if common is None else (common & set(got))
    programs = [{"kind": "program", "name": n, "copies": len(cards)}
                for n in sorted(common or [])]
    return {"wall": w.name, "cards": len(cards),
            "shows": shows, "programs": programs,
            "unreachable": unreachable}


class WallContent(BaseModel):
    kind: str
    show_id: int | None = None
    name: str | None = None


@router.put("/walls/{wid}/content")
def set_wall_content(wid: int, body: WallContent, db: Session = Depends(get_db)):
    """Say what this wall shows, and let mode follow from it.

    Choosing content IS the mode decision -- a wall showing a streamed show has
    its cards on the network path, a wall running a program has them running
    it. Making someone set both, in two places, to say one thing was the whole
    complaint.

    Per-card mode remains as an override for the case it exists for: a
    program-mode card still has its pixels when the host dies, so one board can
    keep showing something true while its neighbours hold a frozen frame that
    looks perfectly healthy.
    """
    w = db.get(Wall, wid) or _404("wall")
    if body.kind == "show":
        if body.show_id is None:
            raise HTTPException(422, "pass show_id for kind=show")
        sh = db.get(Show, body.show_id) or _404("show")
        existing = db.scalar(select(Assignment).where(Assignment.wall_id == w.id))
        if existing:
            existing.show_id = sh.id
        else:
            db.add(Assignment(wall_id=w.id, show_id=sh.id))
        for c in w.cards:
            c.mode = "stream"
        chose = sh.name
    elif body.kind == "program":
        if not body.name:
            raise HTTPException(422, "pass name for kind=program")
        for c in w.cards:
            c.mode = "program"
            c.program = body.name
        chose = body.name
    else:
        raise HTTPException(400, "kind must be 'show' or 'program'")
    db.commit()
    failed = [c.name for c in w.cards if _apply_mode(c)]
    return {"wall": w.name, "kind": body.kind, "content": chose,
            "cards": len(w.cards), "unreachable": failed}


@router.get("/walls/{wid}/layout")
def wall_layout(wid: int, db: Session = Depends(get_db)):
    """The wall's geometry and any problems with it.

    Overlaps and gaps are both legal to store -- an operator mid-edit will
    briefly have them -- so they are reported rather than rejected. A silent
    overlap means two boards render the same pixels and one is wasted; a silent
    gap means a black band.
    """
    w = db.get(Wall, wid) or _404("wall")
    cards = [c for c in w.cards if c.enabled]
    problems = []
    for i, a in enumerate(cards):
        ax0, ay0, ax1, ay1 = a.box
        if ax1 > w.width or ay1 > w.height:
            problems.append({"kind": "overhang", "card": a.name,
                             "detail": f"{a.box} extends past {w.width}x{w.height}"})
        for b in cards[i + 1:]:
            bx0, by0, bx1, by1 = b.box
            if ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1:
                problems.append({"kind": "overlap", "card": a.name, "other": b.name})
    covered = w.covers()
    if covered < w.width * w.height and not any(
            p["kind"] == "overlap" for p in problems):
        problems.append({"kind": "gap", "detail":
                         f"{w.width * w.height - covered} px not covered by any card"})
    return {
        "wall": {"id": w.id, "name": w.name, "width": w.width, "height": w.height},
        # layout_yaml rides along so the map can draw the MODULE seams inside
        # each card rectangle. A card rectangle is not one screen -- it is
        # however many modules hang off that board's connectors -- and drawn
        # undivided a two-module card looks like one panel of twice the size.
        "cards": [{"id": c.id, "name": c.name, "host": c.host, "box": list(c.box),
                   "width": c.width, "height": c.height, "mode": c.mode,
                   "enabled": c.enabled, "layout_yaml": c.layout_yaml}
                  for c in w.cards],
        "covered": covered, "problems": problems, "ok": not problems,
        # What mode this wall is in, as one word. "mixed" is the interesting
        # one: it means some cards are ignoring the wall's content, which is
        # legal and deliberate but should never be silent.
        "mode": (lambda ms: ms.pop() if len(ms) == 1 else ("none" if not ms else "mixed"))(
            {c.mode or "stream" for c in w.cards}),
    }


# ----- cards ---------------------------------------------------------------

def _card_out(c: Card, db: Session) -> CardOut:
    return CardOut(**{
        **{k: getattr(c, k) for k in CardIn.model_fields},
        "id": c.id, "state": c.state.value, "last_seen": c.last_seen,
        "last_error": c.last_error,
        "show_id": _wall_show(c.wall, db) if c.wall else None,
    })


@router.get("/drive-types")
def drive_types():
    """Driver chips a card can be told to drive.

    Mirrors `gateware/spwm_chips.py` in the colorlight repo, which is the
    authority. An empty value means the card uses whatever its bitstream was
    built with; the firmware leaves that default in place rather than guessing
    if it is handed a name it has no table for.
    """
    from marquee_core.models import DRIVE_TYPES
    return {"drive_types": list(DRIVE_TYPES)}


@router.get("/rgb-orders")
def rgb_orders():
    """Channel orders a card's modules can expect.

    Mirrors RGB_ORDERS in the firmware's `layout.rs` and the gateware's
    `rgb_order` CSR -- same order, and the index IS the CSR value. The value is
    what the PANEL expects, so "RGB" is standard and "GRB" means the first pin
    group carries green.

    Finding the right one is a look-and-see job: POST the name to the card's
    own `/api/rgborder` to try it live, then record the answer here so it
    survives a reboot.
    """
    from marquee_core.models import RGB_ORDERS
    return {"rgb_orders": list(RGB_ORDERS)}


@router.get("/cards", response_model=list[CardOut])
def list_cards(db: Session = Depends(get_db), wall_id: int | None = None):
    q = select(Card)
    if wall_id:
        q = q.where(Card.wall_id == wall_id)
    return [_card_out(c, db) for c in db.scalars(q.order_by(Card.name)).all()]


@router.post("/cards", response_model=CardOut, status_code=201)
def create_card(body: CardIn, db: Session = Depends(get_db)):
    if db.scalar(select(Card).where(Card.name == body.name)):
        raise HTTPException(409, f"card {body.name!r} already exists")
    db.get(Wall, body.wall_id) or _404("wall")
    c = Card(**body.model_dump())
    db.add(c); db.commit(); db.refresh(c)
    return _card_out(c, db)


def _apply_mode(c: Card) -> str | None:
    """Push mode and program to the BOARD now, not only at its next boot.

    Without this, switching a card to program mode updated the database and the
    card's TFTP config and did nothing else, so the board carried on doing
    whatever it was already doing until someone power-cycled it. The UI said
    "program: dashboard" while the card reported `running: false` -- and since
    the player skips program-mode cards, nothing streamed to it either. The
    panel sat on a stale frame looking like a working sign.

    The boot config is still the record of intent, and still what a cold card
    reads. This just makes the change take effect when you make it.

    Returns None on success, or the error text, so the caller can say "saved,
    but the board did not take it" rather than implying it worked.
    """
    import json
    import urllib.request
    mode = c.mode or "stream"
    try:
        if mode == "program":
            body = json.dumps({"name": c.program}).encode() if c.program else b"{}"
            req = urllib.request.Request(f"http://{c.host}/api/program/on",
                                         data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        else:
            req = urllib.request.Request(f"http://{c.host}/api/program/off",
                                         method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            r.read()
        return None
    except Exception as e:  # unreachable, rebooting, older firmware
        return str(e)


@router.patch("/cards/{cid}", response_model=CardOut)
def update_card(cid: int, body: CardPatch, db: Session = Depends(get_db)):
    c = db.get(Card, cid) or _404("card")
    data = body.model_dump(exclude_unset=True)
    if "wall_id" in data:
        db.get(Wall, data["wall_id"]) or _404("wall")
    for k, v in data.items():
        setattr(c, k, v)
    db.commit(); db.refresh(c)
    # Only when the thing that decides what the board draws actually moved.
    if {"mode", "program"} & set(data):
        _apply_mode(c)
    return _card_out(c, db)


@router.get("/cards/{cid}/bootconfig")
def card_bootconfig(cid: int, db: Session = Depends(get_db)):
    """Exactly what this card is served as its `<mac>.yml` at boot.

    Rendered by the same `boot_config()` the TFTP server uses, not a second
    copy -- an operator reading a config the card never received is worse than
    no display at all, because it looks like confirmation.

    `mode`, `program` and `drive` are appended here rather than stored in the
    layout text, which is why they appear in this output but not in the field
    you edit.
    """
    c = db.get(Card, cid) or _404("card")
    return {"card": c.name, "mac": c.mac,
            "filename": f"{(c.mac or '').replace(':', '-')}.yml" if c.mac else None,
            "config": boot_config(c),
            "served": bool(c.layout_yaml)}


@router.get("/cards/{cid}/programs")
def card_programs(cid: int, db: Session = Depends(get_db)):
    """The on-board programs this card's firmware carries.

    Asked of the CARD rather than kept here. One firmware holds every program
    and the config picks which runs, so the list is a property of the build on
    that board -- a copy in this database would be a second place to record the
    same fact, and would drift the first time a card was flashed and marquee
    was not told.

    A card that is off or streaming still answers: the list is what the
    firmware CAN run, not what it is running.
    """
    import json
    import urllib.error
    import urllib.request

    c = db.get(Card, cid) or _404("card")
    try:
        with urllib.request.urlopen(f"http://{c.host}/api/programs", timeout=3) as r:
            doc = json.loads(r.read().decode())
    except (urllib.error.URLError, OSError, ValueError) as e:
        # Unreachable is not an error worth a 5xx: the card may simply be off.
        # Report what we know so the UI can still show the configured name.
        return {"programs": [], "active": None, "running": False,
                "reachable": False, "error": str(e)}
    doc["reachable"] = True
    return doc


@router.delete("/cards/{cid}", status_code=204)
def delete_card(cid: int, db: Session = Depends(get_db)):
    c = db.get(Card, cid) or _404("card")
    db.delete(c); db.commit()


@router.get("/panels", response_model=list[CardOut], deprecated=True)
def list_panels(db: Session = Depends(get_db)):
    """Deprecated alias for /cards, kept so existing callers do not 404.

    Before v0.11.0 a "panel" was both the board and the display. It is now a
    card (the board) on a wall (the display).
    """
    return list_cards(db)


# ----- shows ---------------------------------------------------------------

@router.get("/shows", response_model=list[ShowOut])
def list_shows(db: Session = Depends(get_db)):
    walls = {w.id: w.name for w in db.scalars(select(Wall)).all()}
    used: dict[int, set[str]] = {}
    for a in db.scalars(select(Assignment)).all():
        used.setdefault(a.show_id, set()).add(walls.get(a.wall_id, f"wall {a.wall_id}"))
    for sc in db.scalars(select(Schedule)).all():
        used.setdefault(sc.show_id, set()).add(walls.get(sc.wall_id, f"wall {sc.wall_id}"))
    out = []
    for sh in db.scalars(select(Show).order_by(Show.name)).all():
        o = ShowOut.model_validate(sh)
        o.walls = sorted(used.get(sh.id, ()))
        out.append(o)
    return out


# ----- shows on disk -------------------------------------------------------
#
# Files are the source of truth for anything under custom/; the database is
# where the running system reads from. Nothing keeps them in step, so both
# directions are exposed here rather than living only in the Makefile.

@router.get("/shows/files")
def shows_files(db: Session = Depends(get_db)):
    return shows_io.file_status(db, shows_io.shows_dir())


@router.post("/shows/export")
def shows_export(db: Session = Depends(get_db)):
    """Database -> files. What `make shows-export` does."""
    written = shows_io.export_dir(db, shows_io.shows_dir())
    return {"written": len(written), "dir": shows_io.shows_dir()}


@router.post("/shows/import")
def shows_import(db: Session = Depends(get_db)):
    """Files -> database. What `make shows-import` does, and what a change to a
    file needs before it reaches a wall."""
    created, updated = shows_io.import_dir(db, shows_io.shows_dir())
    return {"created": created, "updated": updated}


@router.post("/shows", response_model=ShowOut, status_code=201)
def create_show(body: ShowIn, db: Session = Depends(get_db)):
    _validate(body.body)
    s = Show(**body.model_dump())
    db.add(s); db.commit(); db.refresh(s)
    return s


@router.put("/shows/{sid}", response_model=ShowOut)
def update_show(sid: int, body: ShowIn, db: Session = Depends(get_db)):
    s = db.get(Show, sid) or _404("show")
    _validate(body.body)
    s.name, s.body, s.description = body.name, body.body, body.description
    db.commit(); db.refresh(s)
    return s


def _validate(body: str):
    """Reject a broken document at write time, not at play time.

    A sign that fails to render is discovered by someone looking at a blank wall;
    failing the PUT puts the error in front of the person who caused it.
    """
    try:
        load_show(body)
    except Exception as e:
        raise HTTPException(422, f"invalid scene document: {e}") from e


@router.post("/shows/validate")
def validate_show(body: ShowIn):
    _validate(body.body)
    sh = load_show(body.body)
    return {"ok": True, "scenes": [s.name for s in sh.scenes],
            "duration_s": sh.total_duration,
            "display": sh.display.model_dump()}


# ----- assignment + schedule ----------------------------------------------

@router.put("/walls/{wid}/show/{sid}", response_model=WallOut)
def assign(wid: int, sid: int, db: Session = Depends(get_db)):
    """Point a content source at a wall -- the standing assignment.

    On the wall, not the card: every card covering one picture must play the
    same show, and assigning per card would allow them to disagree.
    """
    w = db.get(Wall, wid) or _404("wall")
    db.get(Show, sid) or _404("show")
    existing = db.scalar(select(Assignment).where(Assignment.wall_id == wid))
    if existing:
        existing.show_id = sid
    else:
        db.add(Assignment(wall_id=wid, show_id=sid))
    db.commit(); db.refresh(w)
    return _wall_out(w, db)


@router.get("/schedules")
def list_schedules(db: Session = Depends(get_db), wall_id: int | None = None):
    q = select(Schedule)
    if wall_id:
        q = q.where(Schedule.wall_id == wall_id)
    return db.scalars(q.order_by(Schedule.start_min)).all()


@router.post("/schedules", status_code=201)
def create_schedule(body: ScheduleIn, db: Session = Depends(get_db)):
    s = Schedule(**body.model_dump())
    db.add(s); db.commit(); db.refresh(s)
    return s


@router.delete("/schedules/{sid}", status_code=204)
def delete_schedule(sid: int, db: Session = Depends(get_db)):
    s = db.get(Schedule, sid) or _404("schedule")
    db.delete(s); db.commit()


# ----- stats ---------------------------------------------------------------

@router.get("/cards/{cid}/stats")
def card_stats(cid: int, hours: int = Query(24, ge=1, le=720),
               db: Session = Depends(get_db)):
    """Counters for one CARD.

    Per card rather than per wall, because these are properties of a board --
    and on a multi-card wall it is exactly the difference between two cards that
    says which one is struggling.
    """
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    rows = db.scalars(
        select(CardStat)
        .where(CardStat.card_id == cid, CardStat.at >= since)
        .order_by(CardStat.at)
    ).all()
    return {
        "card_id": cid, "hours": hours, "samples": len(rows),
        "series": [
            {"at": r.at, "reachable": r.reachable, "fps": r.fps,
             "refresh_hz": r.refresh_hz, "mac_overflow": r.mac_overflow,
             "crc_errors": r.mac_crc_errors, "rebooted": r.rebooted}
            for r in rows
        ],
        "reboots": sum(1 for r in rows if r.rebooted),
        "availability": (sum(1 for r in rows if r.reachable) / len(rows)) if rows else None,
    }


# ----- integration ---------------------------------------------------------

@router.post("/play")
def play(req: PlayRequest, db: Session = Depends(get_db)):
    """Put content on a wall now. For other homelab apps.

    Either name a saved `show` or pass inline `body` YAML. The player picks the
    override up on its next tick.

    `wall` names the target. `panel` is accepted as a deprecated alias and
    resolves by wall name first, then by CARD name -- so a caller written before
    walls existed keeps working, because the migration gave every former panel a
    wall of the same name.
    """
    target = req.wall or req.panel
    if not target:
        raise HTTPException(422, "pass 'wall'")
    p = db.scalar(select(Wall).where(Wall.name == target))
    if not p and target.isdigit():
        p = db.get(Wall, int(target))
    if not p:
        # A card name: put the content on the wall that card belongs to.
        card = db.scalar(select(Card).where(Card.name == target))
        p = card.wall if card else None
    if not p:
        raise HTTPException(404, f"no such wall {target!r}")
    if req.body:
        _validate(req.body)
        s = Show(name=f"_adhoc_{p.name}_{int(dt.datetime.now().timestamp())}",
                 body=req.body, description="ad-hoc via /play")
        db.add(s); db.flush()
    elif req.show:
        s = db.scalar(select(Show).where(Show.name == req.show)) or _404("show")
    else:
        raise HTTPException(422, "pass either 'show' or 'body'")
    existing = db.scalar(select(Assignment).where(Assignment.wall_id == p.id))
    if existing:
        existing.show_id = s.id
    else:
        db.add(Assignment(wall_id=p.id, show_id=s.id))
    db.commit()
    # "panel" is echoed alongside "wall" so older callers that read it back
    # still find the key they expect.
    return {"ok": True, "wall": p.name, "panel": p.name,
            "show": s.name, "show_id": s.id}
