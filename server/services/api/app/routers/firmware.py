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

Flash, and chains (ARCH.md §2c)
-------------------------------
An update never writes flash. A head netboots its assigned image; a chain
MEMBER (whose BIOS cannot netboot) is netbooted by its firmware, over HTTP
(`netboot()`; colorlight 5.0.5). A member's assignment is its head's: a chain
runs one image, and assigning the head assigns the chain. Flash is the
SURVIVAL copy, written later and only by the firmware sync (app/fwsync.py)
once the image has run cleanly -- a bad image never replaces a good copy.

The exception is a member whose firmware predates /api/netboot: the only way
onto it is the old one, write flash and reboot.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
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
        # .fbi files are derived (the flash-wrapped copy of a .bin, made on
        # demand for chain members), not images anyone assigns.
        if not os.path.isfile(p) or name.endswith((".fbi", ".tmp")) or name.startswith("."):
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
    if name.endswith(".fbi"):
        # Derived from a .bin on demand (chain members' flash copy), never
        # uploaded: an uploaded one would be listed nowhere and could go stale.
        raise HTTPException(400, "upload the .bin; the .fbi is made from it")
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
    if os.path.exists(p + ".fbi"):
        os.unlink(p + ".fbi")


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


def _card_get(host: str, path: str, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{host}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def image_path(name: str) -> str | None:
    """The image's path if it is on disk, else None."""
    try:
        p = _path(name)
    except HTTPException:
        return None
    return p if os.path.isfile(p) else None


def chain_head(card: Card, db: Session) -> Card | None:
    if not card.chain_hop:
        return card
    return db.scalar(select(Card).where(Card.host == card.host, Card.chain_hop == 0))


def chain_assignment(card: Card, db: Session) -> str:
    """The image this card must run: a chain runs ONE image, its head's
    assignment. A member's own `firmware` column only mirrors it."""
    head = chain_head(card, db) or card
    return head.firmware or DEFAULT_NAME


def chain_members(head: Card, db: Session) -> list[Card]:
    return list(db.scalars(select(Card).where(Card.host == head.host, Card.chain_hop > 0)
                           .order_by(Card.chain_hop)).all())


def netboot(card: Card, name: str, timeout_s: float = 90.0) -> dict:
    """Start `name` on the card from RAM, without writing flash, and wait
    until it reports the image's version (colorlight 5.0.5 `/api/netboot`).

    This is what a member does instead of a BIOS netboot. The card fetches the
    .fbi by TFTP, checks length and CRC in RAM, and only then hands over; a
    failed fetch leaves it running what it ran.
    """
    from ..fwsync import image_version
    want = image_version(name)
    try:
        fbi = _fbi(name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    req = urllib.request.Request(
        f"http://{card.api}/api/netboot", method="POST",
        data=json.dumps({"file": fbi}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            started = json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": f"card unreachable or too old for netboot: {e}"}
    if not started.get("ok"):
        return {"ok": False, "error": started.get("error", "refused")}
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(1.0)
        st = _running(card.api)
        if st and want and st.get("version") == want:
            return {"ok": True, "file": fbi, "now_running": want}
        prog = _card_get(card.api, "/api/netboot") if st else None
        if prog and str(prog.get("state", "")).startswith("failed"):
            return {"ok": False, "error": prog.get("state")}
    return {"ok": False, "error": f"did not come up on {want} in {timeout_s:.0f} s"}


# After a head comes back from a reboot, its members are left alone this long
# before they are netbooted. Measured 2026-11-13: a member netboot started
# 0.7 s after the head answered stalled at block 62 -- the member accepted
# none of the server's three sends of it over 10 s -- while the same netboot
# with the head settled took 8 s, and the sync's own retry 3 minutes later
# succeeded. The stall is not explained (the firmware never touches the
# downstream PHY); waiting it out, and one retry, is.
MEMBER_SETTLE_S = 15.0
NETBOOT_TRIES = 2


def _bring_member(member: Card, name: str, verify_s: float) -> dict:
    """Put a chain member onto `name`: netboot when its firmware can, else
    (older than 5.0.5) write flash and reboot -- the only way onto it."""
    from ..fwsync import SYNC, has_netboot, image_version
    SYNC.touch(member.id)
    st = _running(member.api)
    out: dict = {"card": member.name, "was_running": (st or {}).get("version")}
    if st is None:
        out.update(ok=False, error="not answering; the firmware sync will bring it "
                   "onto the image when it is back")
        return out
    if has_netboot(st.get("version")):
        for attempt in range(1, NETBOOT_TRIES + 1):
            r = netboot(member, name, verify_s)
            if r.get("ok") or attempt == NETBOOT_TRIES:
                break
            time.sleep(10)
        out.update(method="netboot", attempts=attempt, **r)
        return out
    r = _flash_firmware(member, name)
    out.update(method="flash-and-reboot", flashed=r)
    if not r.get("ok"):
        out.update(ok=False, error=r.get("error"))
        return out
    _reboot(member.api)
    want = image_version(name)
    deadline = time.time() + verify_s
    time.sleep(3)
    while time.time() < deadline:
        st = _running(member.api)
        if st and st.get("version") == want:
            out.update(ok=True, now_running=want)
            return out
        time.sleep(2)
    out.update(ok=False, error=f"did not come back on {want}")
    return out


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


FW_FLASH_LEN = 0x100000


def _fbi(name: str) -> str:
    """`<name>.fbi`: the image wrapped for the card's flash copy.

    The LiteX BIOS's flashboot() reads a Flash Boot Image -- u32 length, u32
    crc32, both little-endian, then the image -- exactly what
    `crcfbigen.py -f -l` writes. Built next to the .bin on first use.
    """
    import tempfile
    from marquee_core.fbi import fbi
    src = _path(name)
    out = src + ".fbi"
    # Always rebuilt (half a megabyte, cheap): an mtime test is fooled by a
    # .bin copied in with its old timestamp, and the card must never be handed
    # a wrapper of a previous image. A unique temp file, so two pushes at once
    # cannot interleave.
    with open(src, "rb") as fh:
        data = fh.read()
    # The card's limits, checked here so a refusal says why. Its flash region
    # is 1 MiB including the 8-byte header (tftp_flash.rs FW_LEN), and its
    # TFTP client takes names up to 96 bytes; past either the card reports
    # only "not an FBI firmware image" / a failed fetch.
    if len(data) > FW_FLASH_LEN - 8:
        raise ValueError(f"{name} is {len(data)} bytes; a card's flash firmware "
                         f"region holds {FW_FLASH_LEN - 8}")
    # A chain member boots ONLY what is in its flash, and finds its place in
    # the chain only with chain-aware firmware (colorlight 5.0.0+). An older
    # image would come up, never hear it is chained, and be unreachable until
    # someone re-cables or JTAGs it. The status JSON's chain block is a string
    # every chain-aware build carries.
    if b'"chain":{"role":"' not in data:
        raise ValueError(f"{name} has no chain support (needs firmware 5.0.0 or "
                         f"later); a chain member flashed with it would be stranded")
    if len(os.path.basename(out)) > 96:
        raise ValueError(f"image name too long for the card's TFTP client: "
                         f"{os.path.basename(out)}")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(out), prefix=".fbi-")
    with os.fdopen(fd, "wb") as fh:
        fh.write(fbi(data))
    os.replace(tmp, out)
    return os.path.basename(out)


def _flash_firmware(card: Card, name: str, timeout_s: float = 120.0) -> dict:
    """Write `name` into the card's FLASH firmware copy and wait for it.

    Only a chain MEMBER needs this: it cannot netboot (its DHCP never leaves
    the chain), so whatever is in its flash is what it runs. A card on the
    switch netboots the assigned image and keeps flash as a survival copy.
    The card fetches the .fbi by TFTP from the server that discovered it.
    """
    try:
        fbi = _fbi(name)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    req = urllib.request.Request(
        f"http://{card.api}/api/flash/firmware", method="POST",
        data=json.dumps({"file": fbi}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            started = json.loads(r.read().decode())
    except Exception as e:
        return {"ok": False, "error": f"card unreachable: {e}"}
    if not started.get("ok"):
        return {"ok": False, "error": started.get("error", "refused")}
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        time.sleep(1.0)
        try:
            with urllib.request.urlopen(f"http://{card.api}/api/flash/firmware", timeout=3) as r:
                last = json.loads(r.read().decode())
        except Exception:
            continue
        if last.get("state") == "done":
            return {"ok": True, "file": fbi, "written": last.get("written")}
        if str(last.get("state", "")).startswith("failed"):
            return {"ok": False, "error": last.get("error") or last.get("state")}
    return {"ok": False, "error": "timed out", "last": last}


@router.post("/cards/{cid}/firmware")
def assign_firmware(cid: int, body: AssignIn, db: Session = Depends(get_db)):
    """Point one card at an image and (by default) reboot it into it.

    Verifies the card comes back before reporting success. It does NOT roll
    back automatically: a card that does not return is still netbooting on
    every power cycle, so the recovery is to reassign and reboot -- and doing
    that automatically would hide a firmware that crashes late, which is worse
    than a card that is visibly down.
    """
    from ..fwsync import SYNC
    card = db.get(Card, cid)
    if not card:
        raise HTTPException(404, "no such card")
    if body.firmware is not None and not os.path.isfile(_path(body.firmware)):
        raise HTTPException(400, f"no such firmware: {body.firmware}")
    if card.chain_hop:
        # A chain runs ONE image (ARCH.md §2c). Assigning a member on its own
        # would leave the chain split, and the firmware sync would put it back.
        head = chain_head(card, db)
        raise HTTPException(409, f"{card.name} is a chain member and runs its head's "
                            f"image; assign {head.name if head else 'the head'} instead")

    previous = card.firmware
    members = chain_members(card, db)
    result = {"card": card.name, "host": card.host, "previous": previous,
              "firmware": body.firmware or DEFAULT_NAME, "rebooted": False,
              "back": None, "status": None}
    if members and body.firmware is not None:
        # Refused here rather than discovered on a member: an image a member
        # cannot netboot (no chain support, too large) is no image for this chain.
        try:
            _fbi(body.firmware)
        except ValueError as e:
            raise HTTPException(400, f"not usable on this chain: {e}")
    card.firmware = body.firmware
    for m in members:
        m.firmware = body.firmware
    db.commit()
    SYNC.touch(card.id)
    for m in members:
        SYNC.touch(m.id)
    if not body.reboot:
        return result

    before = _running(card.api) or {}
    result["was_running"] = before.get("version")
    result["rebooted"] = _reboot(card.api)

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
        if _running(card.api) is None:
            result["went_down"] = True
            break
        time.sleep(0.5)
    else:
        result["went_down"] = False

    while time.time() < deadline:
        st = _running(card.api)
        if st:
            result["back"] = True
            result["now_running"] = st.get("version")
            result["status"] = {k: st.get(k) for k in
                                ("mac", "ip", "version", "bitmap_frames", "isr_count")}
            if not result["went_down"]:
                result["hint"] = ("the card never stopped answering, so it may not "
                                  "have rebooted; compare was_running/now_running")
            if members:
                time.sleep(MEMBER_SETTLE_S)
                result["members"] = [_bring_member(m, result["firmware"], body.verify_s)
                                     for m in members]
                result["back"] = all(m.get("ok") for m in result["members"])
            return result
        time.sleep(2)
    result["back"] = False
    result["hint"] = (
        "card did not answer in time. It netboots on every "
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
    from ..fwsync import SYNC
    sync = SYNC.report()
    out = []
    for c in db.scalars(select(Card).order_by(Card.name)).all():
        st = _running(c.api) if c.host else None
        bs = (_card_bitstream(c.api) or {}).get("bitstream") if st else None
        out.append({
            "card_id": c.id,
            "card": c.name,
            "host": c.host,
            # A chain member reaches the network through its head and boots
            # from FLASH: its firmware push writes flash, not just a netboot
            # assignment.
            "chain_hop": c.chain_hop,
            "api": c.api,
            # --- the Rust firmware ---
            # The CHAIN's assignment: a member runs its head's (ARCH.md §2c).
            "assigned": chain_assignment(c, db),
            "is_default": (chain_head(c, db) or c).firmware is None,
            # The firmware sync's view: what it is doing about this card, and
            # whether its flash survival copy equals the assignment.
            "sync": sync.get(c.id, {}).get("state"),
            "survival": sync.get(c.id, {}).get("survival", "unknown"),
            "sync_error": sync.get(c.id, {}).get("error"),
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


# Rollouts run in the background: one card at a time with a reboot (and, for a
# chain member, a flash write) each, so even four cards outlast any HTTP
# timeout -- the request used to be cut at nginx's 300 s with the rollout still
# going and its result lost. POST starts a job; GET polls it.
_ROLLOUTS: dict[str, dict] = {}
_ROLLOUT_LOCK = threading.Lock()


@router.post("/firmware/rollout", status_code=202)
def rollout(body: RolloutIn, db: Session = Depends(get_db)):
    """Roll an image across the fleet ONE CARD AT A TIME, verifying each.

    Serial and halting by default: the failure that matters is an image that
    boots nowhere, and pushing it to twenty cards in parallel turns a
    one-card problem into a dark wall. `stop_on_failure=False` is for when you
    already know a card is down for other reasons.

    Returns a job at once; `GET /firmware/rollout/{job}` reports progress and,
    when `done`, the same result this used to return inline.
    """
    if not os.path.isfile(_path(body.firmware)):
        raise HTTPException(400, f"no such firmware: {body.firmware}")
    # One entry per CHAIN: a head brings its members (assign_firmware), and a
    # member is never assigned on its own. A chain is in the rollout when its
    # head is enabled, or -- for a wall -- when any of its cards is on that wall.
    q = select(Card)
    if body.wall_id is not None:
        q = q.where(Card.wall_id == body.wall_id)
    ids: list[int] = []
    for c in db.scalars(q).all():
        head = chain_head(c, db)
        if head is not None and getattr(head, "enabled", True) and head.id not in ids:
            ids.append(head.id)
    with _ROLLOUT_LOCK:
        if any(not j["done"] for j in _ROLLOUTS.values()):
            raise HTTPException(409, "a rollout is already running")
        job = uuid.uuid4().hex[:12]
        _ROLLOUTS[job] = {"job": job, "firmware": body.firmware, "done": False,
                          "total": len(ids), "completed": [], "current": None}
    threading.Thread(target=_run_rollout, args=(job, body, ids), daemon=True).start()
    return _ROLLOUTS[job]


@router.get("/firmware/sync")
def firmware_sync(db: Session = Depends(get_db)):
    """The firmware sync's state for every card it has seen (app/fwsync.py):
    what it is doing, the survival copy, the last action and error."""
    from ..fwsync import SYNC, SETTLE_S
    names = {c.id: c.name for c in db.scalars(select(Card)).all()}
    return {"settle_s": SETTLE_S, "enabled": SYNC._thread is not None,
            "cards": [{"card_id": cid, "card": names.get(cid), **r}
                      for cid, r in sorted(SYNC.report().items())]}


def rollout_running() -> bool:
    with _ROLLOUT_LOCK:
        return any(not j["done"] for j in _ROLLOUTS.values())


@router.get("/firmware/rollout/{job}")
def rollout_status(job: str):
    j = _ROLLOUTS.get(job)
    if j is None:
        raise HTTPException(404, "no such rollout")
    return j


def _run_rollout(job: str, body: RolloutIn, ids: list[int]) -> None:
    from ..db import SessionLocal
    j = _ROLLOUTS[job]
    try:
        with SessionLocal() as db:
            for n, cid in enumerate(ids):
                c = db.get(Card, cid)
                if c is None:
                    continue
                j["current"] = c.name
                try:
                    r = assign_firmware(c.id, AssignIn(firmware=body.firmware, reboot=True,
                                                       verify_s=body.verify_s), db)
                except HTTPException as e:
                    r = {"card": c.name, "back": False, "error": str(e.detail)}
                j["completed"].append(r)
                if not r.get("back") and body.stop_on_failure:
                    rest = [db.get(Card, x) for x in ids[n + 1:]]
                    j.update(halted=True, remaining=[x.name for x in rest if x],
                             hint=f"{c.name} did not come back; stopped before the rest")
                    return
            j["halted"] = False
    except Exception as e:  # the job must END, whatever happened
        j.update(halted=True, error=str(e))
    finally:
        j["current"] = None
        j["done"] = True
