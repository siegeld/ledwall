"""Over-the-air FPGA BITSTREAM updates — the thing that retires the JTAG cable.

Firmware OTA (`firmware.py`) was the easy half: a card refetches `boot.bin` over
TFTP on every boot and keeps no copy, so updating it is a server-side change plus
a reboot. The *bitstream* is different — it lives in the card's SPI flash, and
until the firmware could rewrite that flash, a new bitstream meant attaching a
programmer to a board that is usually bolted behind a panel.

How a push works:

1. Upload a `.bit` here. The 28-byte ASCII header is stripped and the RAW payload
   is stored, because that is exactly what must land in flash for the ECP5 to
   find a preamble at the slot. The preamble is validated now, on the server,
   rather than after a card has already erased its working image.
2. `POST /api/v1/cards/{id}/bitstream` tells the card to fetch it. The card
   streams it over TFTP straight into flash offset 0, one sector at a time
   (erase, program, verify), never buffering the whole image.
3. The new bitstream is live at the card's **next power cycle**. Configuration is
   re-read on power-up or a JTAG REFRESH, and this SoC exposes no
   self-reconfiguration primitive, so this is not instant. It is, however, no
   longer a cable.

Why a bad push cannot brick a card
----------------------------------
A GOLDEN bitstream is written once over JTAG at `0x200000` and never again. When
configuration from offset 0 fails, the ECP5 scans forward through flash, finds
the golden, and boots that instead — so the card comes back on the network and
can be pushed another image. Measured three ways on an E320: a corrupted primary,
a corrupted primary built `--bootaddr 0`, and a primary whose first sector was
erased; all three booted the golden with 0% packet loss. See
`docs/FLASHING.md` section 2d in the colorlight repo.

The card enforces the other half: its `region_is_writable()` refuses the golden
region unconditionally, and refuses an application write at all when no golden is
present. This router checks the same thing before starting, so the common case
fails fast with an explanation instead of a flash error.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import urllib.request

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from marquee_core.models import Card

from ..config import settings
from ..db import get_db

router = APIRouter(prefix="/api/v1", tags=["bitstream"])

# A bitstream's filename MUST be its identity:
# "<board>-<panel>-out<n>-v<semver>.bit", e.g. "e320-128x64-out6-v1.0.0.bit".
#
# Enforced rather than suggested, because the alternative was measured and it is
# confusing: the fleet view shows what a card is RUNNING (read from the gateware's
# own identifier ROM) next to what was PUSHED to it (this filename). If the
# filename is anything else -- "app-e320-128x64.bit", say -- the two columns can
# never agree, and an operator cannot tell "already live" from "written, awaiting a
# power cycle", which is the one question the pair exists to answer.
#
# Board, panel and output count are ALL compiled into the bitstream and all change
# what the gateware does, so all three are part of the identity. An earlier version
# used just "<board>-v<semver>" on the reasoning that panel geometry was a build
# variant. It is not: "--panel 128x64" and "--panel 256x64" are different,
# non-interchangeable logic -- the panel sets the shift register width and row
# count, which is why build.sh keys its bitstream cache by panel and says "firmware
# is universal, only bitstreams differ". Under that scheme both were
# "e320-v1.0.0", and a name that cannot distinguish two incompatible images is not
# an identity.
#
# The system clock is NOT in the name: in practice it follows the board, and
# build.sh refuses a firmware whose clock constant disagrees with the gateware's,
# so a mismatch cannot reach a card silently. Build the same board, panel and
# output count at a different clock and the VERSION must be bumped --
# gateware/VERSION.md says so, and the same goes for anything else compile-time
# that the name does not spell out.
#
# Boards are enumerated rather than pattern-matched: a permissive pattern accepted
# "e320-128x64-v2.18.0.bit" by reading "e320-128x64" as a board name. Adding a
# board here is a deliberate one-line change; it should be.
BOARDS = ("5a-75e", "e320")
# The PANEL may contain hyphens: the presets are not all bare "WxH" --
# "256x128-icnd1065" and "256x256-icnd1065-hub320" name the driver chip and the
# connector family too, and those are the presets the P1.25 modules actually use.
# A pattern of "-\d+x\d+-" rejected every one of them, so the panel group is
# "digits x digits" optionally followed by hyphenated qualifiers, and the board is
# anchored at the FRONT where it is unambiguous.
SAFE_NAME = re.compile(
    r"^(?:" + "|".join(re.escape(b) for b in BOARDS)
    + r")-\d+x\d+(?:-[a-z0-9]+)*-out\d+-v\d+\.\d+\.\d+\.bit$")

# The ECP5 bitstream sync word. A payload begins ff ff ff bd b3; we look for the
# bd b3 within the first few bytes rather than demanding a fixed offset, which is
# what the firmware does too.
SYNC = bytes([0xBD, 0xB3])


def _root() -> str:
    d = os.path.join(settings.firmware_root, "bitstreams")
    os.makedirs(d, exist_ok=True)
    return d


def _safe_path(name: str) -> str:
    """Resolve a name without judging its shape -- for reading and DELETING.

    Deleting must NOT require a conforming name. The strict rule below rejects
    legacy ad-hoc names, and applying it to delete made exactly the files that
    need removing undeletable through the API: "app-e320-128x64.bit" could neither
    be re-uploaded nor cleaned up. A rule about what you may CREATE should never
    become a rule about what you may remove.
    """
    if not name or "/" in name or ".." in name or name.startswith("."):
        raise HTTPException(400, f"unsafe bitstream name: {name!r}")
    return os.path.join(_root(), name)


def _path(name: str) -> str:
    """Resolve a name for WRITING, enforcing that it is a valid identity."""
    if "/" in (name or "") or ".." in (name or ""):
        raise HTTPException(400, f"unsafe bitstream name: {name!r}")
    if not SAFE_NAME.match(name or ""):
        raise HTTPException(
            400,
            f"bitstream name must be '<board>-<panel>-out<n>-v<semver>.bit' with "
            f"board one of {', '.join(BOARDS)} -- e.g. "
            f"'e320-128x64-out6-v1.0.0.bit' -- got {name!r}. Board, panel and "
            "output count are all compiled into the bitstream, so all three are "
            "part of its identity. The name has to match what the card reports "
            "running, or the "
            "fleet view compares a filename against an identity and they never "
            "agree. ./build.sh prints the right name after a bitstream build.",
        )
    return os.path.join(_root(), name)


def _payload(raw: bytes) -> bytes:
    """Strip a `.bit` ASCII header and return the raw payload.

    A `.bit` starts `ff 00 "Part: LFE5U-25F-..." 00` and only then the real
    preamble. openFPGALoader strips that when it parses a .bit, but a card
    streaming bytes into flash does no parsing -- so if the header were written
    verbatim the ECP5 would find no preamble at the slot and refuse the image
    (BSE error 4). Measured; see FLASHING.md.
    """
    i = raw.find(bytes([0xFF, 0xFF, 0xFF, 0xBD, 0xB3]))
    if i < 0:
        # Already a payload? Accept it if the sync word is right at the front.
        if SYNC in raw[:8]:
            return raw
        raise HTTPException(
            400,
            "no ECP5 preamble (ff ff ff bd b3) found: this is not a bitstream. "
            "Refusing it here rather than letting a card erase a working image "
            "to install it.",
        )
    return raw[i:]


class BitstreamOut(BaseModel):
    name: str
    size: int
    modified: float
    used_by: list[str] = []


class PushIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bitstream: str
    # Long by default: a 426 KB image is ~850 TFTP blocks and every 4 KB costs a
    # sector erase, so a push is minutes, not seconds.
    timeout_s: float = 600.0


def _card_get(host: str, path: str, timeout: float = 5.0) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{host}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _card_post(host: str, path: str, body: dict, timeout: float = 10.0) -> dict | None:
    try:
        req = urllib.request.Request(
            f"http://{host}{path}", data=json.dumps(body).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


@router.get("/bitstreams", response_model=list[BitstreamOut])
def list_bitstreams(db: Session = Depends(get_db)):
    users: dict[str, list[str]] = {}
    for c in db.scalars(select(Card)).all():
        if c.bitstream:
            users.setdefault(c.bitstream, []).append(c.name)
    out = []
    for name in sorted(os.listdir(_root())):
        p = os.path.join(_root(), name)
        if os.path.isfile(p):
            st = os.stat(p)
            out.append(BitstreamOut(name=name, size=st.st_size,
                                    modified=st.st_mtime,
                                    used_by=sorted(users.get(name, []))))
    return out


@router.post("/bitstreams", response_model=BitstreamOut, status_code=201)
async def upload_bitstream(file: UploadFile = File(...)):
    name = os.path.basename(file.filename or "")
    if not name.endswith(".bit"):
        name = f"{name}.bit"
    # _path() rejects anything that is not "<board>-v<semver>.bit"; see SAFE_NAME.
    dest = _path(name)
    raw = await file.read()
    body = _payload(raw)

    # Write a temp file and rename, so a card fetching this name never sees a
    # half-written image -- it would erase its flash and stream in a truncated
    # one, which is exactly the case the golden fallback exists to survive and
    # which there is no reason to cause on purpose.
    tmp = dest + ".part"
    with open(tmp, "wb") as fh:
        fh.write(body)
    os.replace(tmp, dest)
    st = os.stat(dest)
    return BitstreamOut(name=name, size=st.st_size, modified=st.st_mtime)


@router.delete("/bitstreams/{name}", status_code=204)
def delete_bitstream(name: str, db: Session = Depends(get_db)):
    p = _safe_path(name)
    if not os.path.exists(p):
        raise HTTPException(404, "no such bitstream")
    users = [c.name for c in db.scalars(select(Card)).all() if c.bitstream == name]
    if users:
        raise HTTPException(409, f"still assigned to {', '.join(users)}")
    os.remove(p)


@router.get("/cards/{cid}/bitstream")
def bitstream_status(cid: int, db: Session = Depends(get_db)):
    """What the card reports about its flash and any update in flight."""
    card = db.get(Card, cid)
    if not card:
        raise HTTPException(404, "no such card")
    st = _card_get(card.host, "/api/flash/bitstream")
    return {
        "card": card.name, "host": card.host,
        "assigned": card.bitstream,
        "online": st is not None,
        "card_state": st,
    }


@router.post("/cards/{cid}/bitstream")
def push_bitstream(cid: int, body: PushIn, db: Session = Depends(get_db)):
    """Push a bitstream to one card and wait for it to be written.

    Does NOT reboot the card. Writing flash does not reconfigure the FPGA, so a
    reboot would only restart the firmware and would make the operator believe
    the new gateware was live when it is not. The new bitstream arrives at the
    next power cycle, and the response says so.
    """
    card = db.get(Card, cid)
    if not card:
        raise HTTPException(404, "no such card")
    if not os.path.exists(_safe_path(body.bitstream)):
        raise HTTPException(400, f"no such bitstream: {body.bitstream}")

    before = _card_get(card.host, "/api/flash/bitstream")
    if before is None:
        raise HTTPException(409, f"{card.name} is not answering; nothing attempted")
    if not before.get("golden"):
        # The card would refuse this too, but failing here says why.
        raise HTTPException(
            409,
            f"{card.name} has no golden bitstream at 0x200000. Without a recovery "
            "image a failed write needs JTAG, so this is refused. Provision the "
            "golden once over JTAG first.",
        )

    started = _card_post(card.host, "/api/flash/bitstream", {"file": body.bitstream})
    if not started or not started.get("ok"):
        raise HTTPException(502, f"card refused the update: {started}")

    card.bitstream = body.bitstream
    db.commit()

    deadline = time.time() + max(30.0, body.timeout_s)
    last: dict | None = None
    while time.time() < deadline:
        last = _card_get(card.host, "/api/flash/bitstream")
        if last is None:
            time.sleep(2)
            continue
        state = last.get("state", "")
        if state == "done":
            return {
                "card": card.name, "bitstream": body.bitstream,
                "written": last.get("written"), "sectors": last.get("sectors"),
                "state": "done",
                "note": "written to flash; it becomes live at the next power "
                        "cycle. Nothing reconfigures the FPGA from software.",
            }
        if state.startswith("failed"):
            return {
                "card": card.name, "bitstream": body.bitstream, "state": state,
                "written": last.get("written"), "error": last.get("error"),
                "note": "the card is still running its previous gateware; if the "
                        "image it half-wrote does not configure, it boots the "
                        "golden instead. Nothing needs JTAG.",
            }
        time.sleep(2)

    return {"card": card.name, "bitstream": body.bitstream, "state": "timeout",
            "last": last,
            "note": "stopped waiting. The card may still be writing; poll "
                    "GET /api/v1/cards/{id}/bitstream."}
