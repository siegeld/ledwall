"""Firmware sync: keep every card on its assigned image, and its flash survival
copy equal to it. ARCH.md §2c is the policy; this is the whole of its
enforcement.

The rules, briefly:

* marquee decides what runs, per CHAIN: a member runs its head's assignment.
* Every card runs the assigned image whenever marquee can be reached. A head's
  BIOS netboots it; a member cannot (its DHCP never leaves the chain), so it
  comes up on its flash copy and is then netbooted into the assigned image
  over HTTP (`POST /api/netboot`, colorlight 5.0.5). Neither writes flash.
* Flash is the SURVIVAL copy -- what runs when marquee cannot be reached at
  boot -- and is kept equal to the assigned image: once a card has run it
  cleanly for SETTLE_S, and its flash header (length + crc32) differs, the
  copy is written through the card's checked flash path. Once per card per
  firmware change; never as part of an update.

So a member that was off during a rollout, a head that cold-booted from flash,
and a card whose flash copy is stale or corrupt all correct themselves.

ONE SYNC PER FLEET, like chain discovery: a second marquee pointed at a copy of
the database (a dev host) must set MARQUEE_FIRMWARE_SYNC=false, or the two
reboot and rewrite the same cards.

`plan()` decides and touches nothing; `Sync` carries a decision out. The split
is what lets tests/api/test_fwsync.py check every rule without a card.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import zlib
from dataclasses import dataclass, field

log = logging.getLogger("marquee.fwsync")

POLL_S = 15.0
# A card must run the assigned image this long, continuously, before its flash
# copy is replaced with it: a firmware that boots and then misbehaves must not
# become the survival copy straight away. The cost is that a power cut inside
# the window boots the previous version -- the safe direction.
SETTLE_S = float(os.environ.get("MARQUEE_FIRMWARE_SETTLE_S", "600"))
# After an attempt to bring a card onto its image, wait this long before the
# next; give up on that image after MAX_ATTEMPTS (until the assignment changes,
# or marquee restarts) -- a card that cannot run an image must be visibly
# wrong, not rebooted forever.
RETRY_S = 120.0
MAX_ATTEMPTS = 3
# A card someone just pointed at an image (assign, rollout) is left alone for
# this long, so the sync never races an operator's push.
QUIET_AFTER_TOUCH_S = 180.0
# First firmware with POST /api/netboot.
NETBOOT_SINCE = (5, 0, 5)

_VERSION = re.compile(r"-v(\d+)\.(\d+)\.(\d+)\.bin$")


def image_version(name: str | None) -> str | None:
    """The semver in a canonical image name (`e320-spwm-v5.0.5.bin`), else
    None -- `boot.bin` and other legacy names carry no version, so a card
    assigned one cannot be compared and is left alone."""
    m = _VERSION.search(name or "")
    return ".".join(m.groups()) if m else None


def _vtuple(v: str | None) -> tuple[int, ...] | None:
    try:
        return tuple(int(x) for x in (v or "").split("."))
    except ValueError:
        return None


def has_netboot(running_version: str | None) -> bool:
    t = _vtuple(running_version)
    return bool(t) and t >= NETBOOT_SINCE


def flash_header(hexdump: str | None) -> tuple[int, int] | None:
    """(length, crc32) from the first 8 bytes of the card's firmware copy, as
    `GET /api/flash` reports them (`offset100000`, hex)."""
    try:
        b = bytes.fromhex((hexdump or "")[:16])
    except ValueError:
        return None
    if len(b) < 8:
        return None
    return int.from_bytes(b[:4], "little"), int.from_bytes(b[4:8], "little")


def image_header(path: str) -> tuple[int, int]:
    """(length, crc32) an FBI of this .bin carries -- what the card's flash
    header must say for its survival copy to BE this image."""
    with open(path, "rb") as fh:
        data = fh.read()
    return len(data), zlib.crc32(data) & 0xFFFFFFFF


@dataclass
class Track:
    """What the sync remembers about one card between polls."""
    # When the card was first seen running its assigned image, continuously up
    # since (None while it is not).
    since: float | None = None
    last_isr: int | None = None
    image: str | None = None          # the assignment the counters below are for
    attempts: int = 0
    next_try: float = 0.0
    touched: float = 0.0
    state: str = "unknown"            # what the UI shows
    survival: str = "unknown"         # in sync | stale | writing | unknown
    error: str | None = None
    last_action: str | None = None
    last_action_at: float | None = None


@dataclass
class View:
    """One card as the sync sees it on this poll."""
    id: int
    name: str
    hop: int
    assigned: str                     # image name, the CHAIN's assignment
    status: dict | None               # GET /api/status, None if no answer
    flash: tuple[int, int] | None = None     # its flash header, when read
    want_flash: tuple[int, int] | None = None


def plan(v: View, t: Track, now: float) -> str:
    """Decide what to do with one card. Updates `t`; performs nothing.

    Returns one of: "none", "reboot" (a head: its BIOS netboots the image),
    "netboot" (a member on 5.0.5+), "flash-and-reboot" (a member too old to
    netboot -- the only way onto it), "read-flash" (in sync and settled: read
    the flash header, then call again with it), "survival" (write the flash
    copy).
    """
    if t.image != v.assigned:
        # A new assignment starts a new history: earlier failures were for
        # another image.
        t.image, t.attempts, t.next_try, t.since = v.assigned, 0, 0.0, None
        t.survival, t.error = "unknown", None

    want = image_version(v.assigned)
    st = v.status
    if st is None:
        t.since, t.last_isr, t.state = None, None, "offline"
        return "none"
    isr = st.get("isr_count")
    if t.last_isr is not None and isinstance(isr, int) and isr < t.last_isr:
        t.since = None                # it restarted between polls
    t.last_isr = isr if isinstance(isr, int) else None
    if want is None:
        t.state = f"not synced: {v.assigned} has no version in its name"
        return "none"
    running = st.get("version")

    if running != want:
        t.since = None
        if now - t.touched < QUIET_AFTER_TOUCH_S:
            t.state = f"runs {running}, assigned {want}; an update is in progress"
            return "none"
        if t.attempts >= MAX_ATTEMPTS:
            t.state = (f"runs {running}, assigned {want}; gave up after "
                       f"{t.attempts} attempts")
            return "none"
        if now < t.next_try:
            t.state = f"runs {running}, assigned {want}; retrying"
            return "none"
        t.attempts += 1
        t.next_try = now + RETRY_S
        t.state = f"runs {running}, assigned {want}; bringing it onto {want}"
        if v.hop == 0:
            return "reboot"
        return "netboot" if has_netboot(running) else "flash-and-reboot"

    # Running the assigned image.
    t.attempts, t.error = 0, None
    if t.since is None:
        t.since = now
    if now - t.since < SETTLE_S:
        t.state = f"runs {want}; settling before its survival copy is checked"
        return "none"
    if v.want_flash is None:
        t.state = f"runs {want}"
        return "none"
    if v.flash is None:
        return "read-flash"
    if v.flash == v.want_flash:
        t.survival, t.state = "in sync", f"runs {want}; survival copy in sync"
        return "none"
    if t.survival == "writing" or now < t.next_try:
        return "none"
    t.survival = "stale"
    t.next_try = now + RETRY_S
    t.state = f"runs {want}; writing its survival copy"
    return "survival"


@dataclass
class Sync:
    """The thread that applies `plan()` to every card, every POLL_S."""
    tracks: dict[int, Track] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _thread: threading.Thread | None = None

    def track(self, cid: int) -> Track:
        with self._lock:
            return self.tracks.setdefault(cid, Track())

    def touch(self, cid: int) -> None:
        """An operator just pointed this card at an image: leave it alone."""
        self.track(cid).touched = time.time()

    def start(self) -> None:
        if os.environ.get("MARQUEE_FIRMWARE_SYNC", "true").lower() in ("0", "false", "no"):
            log.info("firmware sync disabled (MARQUEE_FIRMWARE_SYNC)")
            return
        self._thread = threading.Thread(target=self._run, name="fwsync", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                self.once()
            except Exception:           # the loop must survive anything
                log.exception("firmware sync pass failed")
            time.sleep(POLL_S)

    def once(self, now: float | None = None) -> None:
        from marquee_core.models import Card
        from sqlalchemy import select
        from .db import SessionLocal
        from .routers import firmware as fw

        if fw.rollout_running():
            return
        with SessionLocal() as db:
            cards = [c for c in db.scalars(select(Card)).all() if c.host]
            for c in cards:
                assigned = fw.chain_assignment(c, db)
                path = fw.image_path(assigned)
                v = View(id=c.id, name=c.name, hop=c.chain_hop or 0, assigned=assigned,
                         status=fw._running(c.api),
                         want_flash=image_header(path) if path else None)
                t = self.track(c.id)
                now_ = time.time() if now is None else now
                action = plan(v, t, now_)
                if action == "read-flash":
                    v.flash = flash_header((fw._card_get(c.api, "/api/flash") or {}).get("offset100000"))
                    action = plan(v, t, now_)
                if action != "none":
                    self._do(action, c, assigned, t, fw)

    def _do(self, action: str, card, image: str, t: Track, fw) -> None:
        log.info("firmware sync: %s %s -> %s", action, card.name, image)
        t.last_action, t.last_action_at = action, time.time()
        if action == "reboot":
            if not fw._reboot(card.api):
                t.error = "reboot refused or card unreachable"
        elif action == "netboot":
            r = fw.netboot(card, image)
            if not r.get("ok"):
                t.error = r.get("error")
        elif action == "flash-and-reboot":
            r = fw._flash_firmware(card, image)
            if r.get("ok"):
                fw._reboot(card.api)
            else:
                t.error = r.get("error")
        elif action == "survival":
            t.survival = "writing"
            r = fw._flash_firmware(card, image)
            t.survival = "in sync" if r.get("ok") else "stale"
            if not r.get("ok"):
                t.error = f"survival copy: {r.get('error')}"

    def report(self) -> dict[int, dict]:
        with self._lock:
            return {cid: {"state": t.state, "survival": t.survival, "error": t.error,
                          "attempts": t.attempts, "since": t.since,
                          "last_action": t.last_action, "last_action_at": t.last_action_at}
                    for cid, t in self.tracks.items()}


SYNC = Sync()
