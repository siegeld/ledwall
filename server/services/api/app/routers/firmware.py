"""Firmware images and OTA rollout -- RB-5554.

Why this exists
---------------
Updating a card used to mean JTAG: a programmer on the bench, a cable in the
right header, and a human. That is a bad dependency for a fleet, and the
colorlight repo's `docs/FLASHING.md` is a long record of how badly it fails.

The BIOS is netboot-FIRST, so a card already fetches its firmware from here on
every boot. That makes firmware OTA almost free -- the missing pieces were a
place to put images, a way to say which card runs which, and a rollout that
checks the card came back instead of hoping. This module is those pieces.

What it deliberately does NOT do
--------------------------------
It does not write the card's SPI flash. Flash holds the *bitstream* and a
survival copy of the firmware, and a bad bitstream can only be recovered over
JTAG -- the exact thing we are removing. Bitstream OTA needs a golden slot and
an atomic pointer flip, and the one hard lesson from writing the card-side
flash code is that **the writer must read back what it wrote before committing
to it**: a write-protected chip refuses an erase outright without ever
asserting BUSY, so every busy-poll returns instantly and every layer reports
success while nothing is written. Until that path is proven on hardware,
rollout here means "serve it and reboot", which is safe because a card that
fails to boot netboots again and can be pointed back at the previous image.
"""

from __future__ import annotations

import hashlib
import logging
import json
import os
import re
import shutil
import time
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from marquee_core.models import Card

from ..config import settings
from ..db import get_db

log = logging.getLogger("marquee.firmware")
router = APIRouter(prefix="/api/v1", tags=["firmware"])

# The name the TFTP resolver falls back to for a card with no `firmware` set.
# Must match services/player/main.py.
DEFAULT_NAME = "boot.bin"

# Images are addressed by bare filename over TFTP, so a name has to be a plain
# filename -- no separators, no traversal, nothing clever.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# What a NEW upload should be called: "<board>-v<semver>.bin".
#
# One firmware source builds every board -- the 5A-75E and E320 register maps are
# byte-identical -- but the system clock is baked in from the gateware, so a 40 MHz
# image on 35 MHz gateware boots and then fails at everything time-dependent, which
# presents as a dead network stack. The board is therefore the one thing a firmware
# filename must carry, and "colorlight-2.20.0.bin" carried exactly none of it.
#
# Advisory, not enforced: `boot.bin` is the fleet default and dozens of historical
# images predate this. Uploads that do not match get a warning in the response
# rather than a refusal -- unlike bitstreams, where an unparseable name breaks the
# running-vs-pushed comparison outright.
BOARDS = ("5a-75e", "e320")
# "-spwm" marks a build with the ICN1065 S-PWM output support compiled in. It is
# part of the name because it is NOT interchangeable with a plain build: the two
# are compiled against different register maps (the S-PWM gateware's hub75
# peripheral has chip_cfg/chip_table registers and no rgb_order), so a plain
# firmware cannot drive a P1.25 panel and an S-PWM firmware cannot set channel
# order on a plain one.
CANON_NAME = re.compile(
    r"^(?:" + "|".join(re.escape(b) for b in BOARDS)
    + r")(?:-spwm)?-v\d+\.\d+\.\d+\.bin$")


def _root() -> str:
    os.makedirs(settings.firmware_root, exist_ok=True)
    return settings.firmware_root


def _path(name: str) -> str:
    if not SAFE_NAME.match(name or "") or "/" in name or ".." in name:
        raise HTTPException(400, f"unsafe firmware name: {name!r}")
    return os.path.join(_root(), name)


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class FirmwareOut(BaseModel):
    name: str
    size: int
    modified: float
    sha256: str
    is_default: bool
    used_by: list[str]


@router.get("/firmware", response_model=list[FirmwareOut])
def list_firmware(db: Session = Depends(get_db)):
    """Every image on disk, with which cards are pointed at it."""
    cards = db.scalars(select(Card)).all()
    users: dict[str, list[str]] = {}
    for c in cards:
        users.setdefault(c.firmware or DEFAULT_NAME, []).append(c.name)
    out = []
    for name in sorted(os.listdir(_root())):
        p = os.path.join(_root(), name)
        if not os.path.isfile(p):
            continue
        st = os.stat(p)
        out.append(FirmwareOut(name=name, size=st.st_size, modified=st.st_mtime,
                               sha256=_sha256(p), is_default=(name == DEFAULT_NAME),
                               used_by=sorted(users.get(name, []))))
    return out


@router.post("/firmware", response_model=FirmwareOut, status_code=201)
def upload_firmware(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Stage an image. Does not point any card at it -- that is a separate,
    deliberate step, so an upload can never change what the fleet runs."""
    name = os.path.basename(file.filename or "")
    dest = _path(name)
    tmp = dest + ".part"
    with open(tmp, "wb") as fh:
        shutil.copyfileobj(file.file, fh)
    if os.path.getsize(tmp) == 0:
        os.unlink(tmp)
        raise HTTPException(400, "empty upload")
    # Rename only once it is complete: the TFTP server serves straight out of
    # this directory, so a half-written file here is a card that netboots a
    # truncated image.
    os.replace(tmp, dest)
    st = os.stat(dest)
    if not CANON_NAME.match(name) and name != DEFAULT_NAME:
        # Stored, but say so: a firmware whose name does not name its board is the
        # one that ends up on the wrong card.
        log.warning("firmware %s does not follow '<board>-v<semver>.bin'; a name "
                    "that does not say which board it is for is how a 40 MHz image "
                    "reaches 35 MHz gateware", name)
    return FirmwareOut(name=name, size=st.st_size, modified=st.st_mtime,
                       sha256=_sha256(dest), is_default=(name == DEFAULT_NAME),
                       used_by=[])


@router.delete("/firmware/{name}", status_code=204)
def delete_firmware(name: str, db: Session = Depends(get_db)):
    p = _path(name)
    if not os.path.isfile(p):
        raise HTTPException(404, "no such firmware")
    if name == DEFAULT_NAME:
        raise HTTPException(409, f"{DEFAULT_NAME} is the fleet default; "
                                 "upload a replacement instead of deleting it")
    using = [c.name for c in db.scalars(select(Card)).all() if c.firmware == name]
    if using:
        raise HTTPException(409, f"still assigned to: {', '.join(sorted(using))}")
    os.unlink(p)


class AssignIn(BaseModel):
    """What image to point a card at.

    `firmware` is REQUIRED, though it may be explicitly null to mean "back to
    the fleet default". It must not have a default: with `= None`, a body that
    misspelled the field -- `{"name": "x"}` instead of `{"firmware": "x"}` --
    was accepted, `firmware` fell back to None, and the endpoint read that as
    "clear this card's assignment", then rebooted the card onto the default
    image. On a card whose gateware the default does not match, that is a
    working card turned into a crash loop by a typo, reported as success.

    `extra="forbid"` makes the typo a 422 instead of a silent rollback.
    """
    model_config = ConfigDict(extra="forbid")

    firmware: str | None
    reboot: bool = True
    verify_s: float = 90.0


def _reboot(host: str) -> bool:
    try:
        req = urllib.request.Request(f"http://{host}/api/reboot", method="POST")
        with urllib.request.urlopen(req, timeout=5):
            return True
    except Exception:
        return False


def _running(host: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{host}/api/status", timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


@router.post("/cards/{cid}/firmware")
def assign_firmware(cid: int, body: AssignIn, db: Session = Depends(get_db)):
    """Point one card at an image and (by default) reboot it into it.

    Verifies the card comes back before reporting success. It does NOT roll
    back automatically: a card that does not return is still netbooting on
    every power cycle, so the recovery is to reassign and reboot -- and doing
    that automatically would hide a firmware that crashes late, which is worse
    than a card that is visibly down.
    """
    card = db.get(Card, cid)
    if not card:
        raise HTTPException(404, "no such card")
    if body.firmware is not None and not os.path.isfile(_path(body.firmware)):
        raise HTTPException(400, f"no such firmware: {body.firmware}")

    previous = card.firmware
    card.firmware = body.firmware
    db.commit()

    result = {"card": card.name, "host": card.host, "previous": previous,
              "firmware": card.firmware or DEFAULT_NAME, "rebooted": False,
              "back": None, "status": None}
    if not body.reboot:
        return result

    before = _running(card.host) or {}
    result["was_running"] = before.get("version")
    result["rebooted"] = _reboot(card.host)

    # Wait for the card to go DOWN before waiting for it to come back.
    #
    # Polling straight for "does it answer" reported success instantly: the old
    # firmware was still serving HTTP in the moment between accepting /api/reboot
    # and actually restarting. So `back: true` could mean "never rebooted", and a
    # push that had not taken effect looked verified. Observed 2026-10-07 -- the
    # card answered with the PREVIOUS image's version right after a push.
    # Bound the down-wait separately from the come-back wait. Sharing one
    # deadline meant a card that never appeared to go down consumed the entire
    # budget here, leaving nothing for the second loop -- so a push of a WORKING
    # image reported back:false. Observed 2026-10-07 on a card that was up and
    # answering seconds later. 20s is far longer than this board's reboot.
    deadline = time.time() + max(5.0, body.verify_s)
    down_deadline = min(deadline, time.time() + 20.0)
    while time.time() < down_deadline:
        if _running(card.host) is None:
            result["went_down"] = True
            break
        time.sleep(0.5)
    else:
        result["went_down"] = False

    while time.time() < deadline:
        st = _running(card.host)
        if st:
            result["back"] = True
            result["now_running"] = st.get("version")
            result["status"] = {k: st.get(k) for k in
                                ("mac", "ip", "version", "bitmap_frames", "isr_count")}
            if not result["went_down"]:
                result["hint"] = ("the card never stopped answering, so it may not "
                                  "have rebooted; compare was_running/now_running")
            return result
        time.sleep(2)
    result["back"] = False
    result["hint"] = ("card did not answer in time. It netboots on every "
                      "power cycle, so reassign the previous image and reboot "
                      "to recover; nothing is written to its flash.")
    return result


def _card_bitstream(host: str) -> dict | None:
    """Ask a card which BITSTREAM it is running (colorlight 2.19.0 and later).

    A separate endpoint from /api/status because it is a separate artifact with a
    separate version, and reading it from the gateware's own identifier ROM is the
    only answer that cannot be stale -- our own record says what we sent, which is
    not the same thing until the card has been power-cycled.
    """
    try:
        with urllib.request.urlopen(f"http://{host}/api/bitstream", timeout=3) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


@router.get("/firmware/running")
def running_firmware(db: Session = Depends(get_db)):
    """What each card is ASSIGNED versus what it is actually RUNNING.

    One call for the whole fleet, because the browser generally cannot reach the
    cards: they sit on the panel network that only the player host is on, so a
    per-card fetch from the UI would simply time out.

    Reports BOTH artifacts, because a card runs two of them and they are updated
    by different paths:

    * `firmware` -- the Rust code's semver, from `CARGO_PKG_VERSION`. Refetched
      over TFTP on every boot, never stored on the card. Needs colorlight 2.15.0;
      an older image answers without it, so null with `online: true` means
      "running something older", not "unreachable".
    * `bitstream` -- the FPGA gateware's name, e.g. `e320-v1.0.0`: the board plus
      the bitstream's OWN semver, read by the firmware out of the gateware's own
      identifier ROM. Independent of the firmware's version, because the two
      change at very different rates and tying them together made every firmware
      release look like it shipped new FPGA logic. Needs colorlight 2.19.0; an
      older card reports null here.

    `bitstream` is read from the FPGA, while `bitstream_assigned` is what marquee
    recorded pushing. When they disagree the card has not been power-cycled since
    the push -- which is the normal state right after one, and the only way to see
    it.
    """
    out = []
    for c in db.scalars(select(Card).order_by(Card.name)).all():
        st = _running(c.host) if c.host else None
        bs = (_card_bitstream(c.host) or {}).get("bitstream") if st else None
        out.append({
            "card_id": c.id,
            "card": c.name,
            "host": c.host,
            # --- the Rust firmware ---
            "assigned": c.firmware or DEFAULT_NAME,
            "is_default": c.firmware is None,
            "version": (st or {}).get("version"),
            # --- the FPGA bitstream ---
            # From the dedicated endpoint rather than /api/status's raw string, so
            # the UI gets the NAME ("e320-v1.0.0") and not a build description to
            # pick apart. None on a card too old to report it.
            "bitstream": (bs or {}).get("name"),
            "bitstream_version": (bs or {}).get("version"),
            "bitstream_detail": (bs or {}).get("raw"),
            "bitstream_assigned": c.bitstream,
            "online": st is not None,
            "isr_count": (st or {}).get("isr_count"),
        })
    return out


class RolloutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    firmware: str
    wall_id: int | None = None      # None -> every enabled card
    verify_s: float = 90.0
    stop_on_failure: bool = True


@router.post("/firmware/rollout")
def rollout(body: RolloutIn, db: Session = Depends(get_db)):
    """Roll an image across the fleet ONE CARD AT A TIME, verifying each.

    Serial and halting by default: the failure that matters is an image that
    boots nowhere, and pushing it to twenty cards in parallel turns a
    one-card problem into a dark wall. `stop_on_failure=False` is for when you
    already know a card is down for other reasons.
    """
    if not os.path.isfile(_path(body.firmware)):
        raise HTTPException(400, f"no such firmware: {body.firmware}")
    q = select(Card)
    if body.wall_id is not None:
        q = q.where(Card.wall_id == body.wall_id)
    cards = [c for c in db.scalars(q).all() if getattr(c, "enabled", True)]

    results = []
    for c in cards:
        r = assign_firmware(c.id, AssignIn(firmware=body.firmware, reboot=True,
                                           verify_s=body.verify_s), db)
        results.append(r)
        if not r.get("back") and body.stop_on_failure:
            return {"firmware": body.firmware, "halted": True,
                    "completed": results,
                    "remaining": [x.name for x in cards[len(results):]],
                    "hint": f"{c.name} did not come back; stopped before the rest"}
    return {"firmware": body.firmware, "halted": False, "completed": results}
