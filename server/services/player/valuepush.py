"""Push named values to cards running an on-board program.

A panel in `mode: program` composes its own pixels and must NOT be streamed to.
What it needs is a handful of named values, which is a few hundred bytes a
second against the ~24 Mbit/s a streamed panel eats to show a static clock face.

Marquee is the right place for this even though the panel could in principle
talk to Home Assistant itself: marquee already holds ONE panel-bus subscription
for the whole fleet -- one integration, one set of credentials, one place to fix
a broken feed -- and the panel has no TLS and no business holding an HA token.

The panel is asked what mode it is in rather than being told. DISPLAY-PROGRAMS
§6: do not configure that twice. A panel that is streaming is skipped, so this
can run unconditionally without fighting the pixel path.
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("marquee.valuepush")

# Where each value comes from in the bus payload. Keeping this as data rather
# than code means a new widget needs a mapping entry, not a new function.
MAPPING = {
    "temp": ("home.status", "weather.temp"),
    "cond": ("home.status", "weather.cond"),
    "humidity": ("home.status", "weather.humidity"),
    "doors": ("home.status", "open.doors"),
    "windows": ("home.status", "open.windows"),
    "garage": ("home.status", "garage_open"),
    "unlocked": ("home.status", "open.unlocked"),
    "power": ("home.status", "power.total"),
    "gallons": ("home.status", "sprinkler.gallons_today"),
    "nextzone": ("home.status", "sprinkler.next_name"),
    "zones": ("home.status", "sprinkler.zones_total"),
    "running": ("home.status", "sprinkler.running"),
}

# How many characters the encoded power trace is. One per sample, and the panel
# indexes it by column, so this is also the trace's horizontal resolution.
# How long each page of a cycling program is shown, and how many there are.
# One page per push keeps the cadence even: the pusher runs every 10s and a
# page only becomes visible when a push repaints the canvas.
PAGE_SECONDS = 10
PAGES = 4

HIST_LEN = 60
# Printable range used for the encoding: '0' plus 0..63.
HIST_BASE = ord("0")
HIST_STEPS = 63


def encode_series(values, length=HIST_LEN) -> str:
    """Pack a numeric series into one character per sample.

    The panel draws the power trace by asking "how tall is column x?" for every
    pixel, and per-pixel parsing is forbidden -- a program is O(1) per pixel or
    it is a frame-rate cut. Sending "43.2,42.8,..." would force the panel to
    parse on every pixel; sending one byte per sample makes a column a single
    index into a &str.

    Scaled to the series' own min..max rather than an absolute ceiling:
    household load has no meaningful fixed maximum, and pinning one would
    flatten the shape that is the whole point of a trace.
    """
    nums = [float(v) for v in (values or [])
            if isinstance(v, (int, float)) and not isinstance(v, bool)]
    if not nums:
        return ""
    lo, hi = min(nums), max(nums)
    span = (hi - lo) or 1.0
    # Resample to a fixed width so the string length -- and so the trace's
    # resolution -- does not change as the feed's history grows.
    out = []
    for i in range(length):
        v = nums[int(i * (len(nums) - 1) / max(1, length - 1))]
        out.append(chr(HIST_BASE + int((v - lo) / span * HIST_STEPS)))
    return "".join(out)


def site_now(now=None):
    """Wall-clock time AT THE SITE, not in the container.

    The containers run UTC. `datetime.now()` is therefore four or five hours
    off the site depending on the season, and it went straight onto the glass:
    a card showed 21:07 in its corner beside a train leaving at 17:08. The
    clock a card displays has to come from the same zone as everything else on
    that card, and MARQUEE_SITE_TZ is where marquee already records it.
    """
    import datetime as _dt
    import os
    from zoneinfo import ZoneInfo
    if now is not None:
        return now
    tz = os.environ.get("MARQUEE_SITE_TZ", "")
    return _dt.datetime.now(ZoneInfo(tz)) if tz else _dt.datetime.now()


def derived(live, now=None) -> dict[str, str]:
    """Values the panel cannot work out for itself.

    `ctx.time_ms` on the panel is UPTIME, not wall clock, and there is no RTC --
    so a clock face has to be pushed like any other value. The panel holds no
    timezone either; marquee already knows the site's.
    """
    now = site_now(now)
    out = {
        "hhmm": now.strftime("%H:%M"),
        "date": now.strftime("%a %d %b"),
        # WHICH PAGE a cycling program shows. The host drives this, and that is
        # a deliberate trade rather than laziness.
        #
        # A program can cycle on its own frame counter with no host at all --
        # but then the whole canvas changes between frames, and a full 128x128
        # of anti-aliased text measured 0.13 fps on this panel (0.57 with every
        # value read band-guarded) against 10.6 for a program that repaints
        # eighteen rows. Rows outside `dynamic` are painted only when a VALUE
        # ARRIVES, so making the page a value is what makes the repaint cheap.
        #
        # Derived from the clock rather than counted, so it is stateless and a
        # pusher restart does not jump the sequence.
        "page": str(int(now.timestamp()) // PAGE_SECONDS % PAGES),
    }
    hist = _dig(live.get("home.status"), "power.history")
    enc = encode_series(hist)
    if enc:
        out["hist"] = enc
    return out


def _dig(doc, path):
    for part in [p for p in path.split(".") if p]:
        if doc is None:
            return None
        doc = doc[int(part)] if part.lstrip("-").isdigit() else doc.get(part)
    return doc


def collect(live) -> dict[str, str]:
    """Flatten the bus cache into the value table the panel expects."""
    out = {}
    for key, (topic, path) in MAPPING.items():
        v = _dig(live.get(topic), path)
        if v is None:
            continue
        out[key] = f"{v:g}" if isinstance(v, float) else str(v)
    out.update(derived(live))
    return out


def _mode(host: str, timeout: float = 3.0) -> str | None:
    try:
        with urllib.request.urlopen(f"http://{host}/api/display", timeout=timeout) as r:
            return json.loads(r.read().decode()).get("source")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def push(host: str, values: dict, timeout: float = 3.0) -> bool:
    body = json.dumps(values).encode()
    req = urllib.request.Request(
        f"http://{host}/api/values", data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except (urllib.error.URLError, OSError):
        return False


def provider_values(providers, live, program: str, now=None) -> dict[str, str] | None:
    """The value set for a card running `program`, or None if nothing serves it.

    The on-card table holds 24 keys and `set()` IGNORES further keys rather than
    evicting, so merging every source's values together would silently drop
    whichever key happened to go last. Values are therefore selected BY PROGRAM
    rather than accumulated: a provider declares the programs it can feed, and
    only its pages are sent to a card running one of them.

    The ROTATION is host-side, for the same reason the page is a value at all:
    rows outside a program's `dynamic` band repaint only when a value arrives,
    so a card cycling on its own frame counter would have to redraw the whole
    canvas every frame. Derived from the clock rather than counted, so it is
    stateless and a pusher restart does not jump the sequence.
    """
    pages = [pg for prov in providers
             if program and program in getattr(prov, "PROGRAMS", ())
             for pg in (prov.board_pages(live) or [])]
    if not pages:
        return None
    now = site_now(now)
    page = int(now.timestamp()) // PAGE_SECONDS % len(pages)
    values = dict(pages[page])
    # The card has no RTC and ctx.time_ms is UPTIME, so the clock is pushed like
    # any other value. `setdefault`, not assignment: a board that formats times
    # in its own zone supplies its own clock, and that one is right -- it is the
    # zone the train times beside it are in. This is only the fallback.
    values.setdefault("hhmm", now.strftime("%H:%M"))
    return values


class ValuePusher(threading.Thread):
    """Sends values to every program-mode card, and only to those."""

    def __init__(self, session_factory, live, interval_s: float = 10.0,
                 providers=None):
        super().__init__(daemon=True, name="value-push")
        self.sf = session_factory
        self.live = live
        self.interval_s = interval_s
        self.providers = providers
        self._last: dict[str, dict] = {}

    def run(self) -> None:
        from sqlalchemy import select
        from marquee_core.models import Card

        while True:
            try:
                bus_values = collect(self.live)
                with self.sf() as db:
                    cards = [c for c in db.scalars(select(Card)).all() if c.enabled]
                instances = self.providers.instances if self.providers else []
                for card in cards:
                    src = _mode(card.host)
                    if src is None or not src.startswith("program"):
                        continue
                    # WHICH program comes from the database, not from the card:
                    # /api/display reports "program (on-panel)" without naming
                    # it, and marquee is what serves the name over TFTP in the
                    # first place, so the row is the source of truth. The MODE
                    # is still asked of the card -- that is the setting that
                    # must not be configured twice.
                    values = provider_values(instances, self.live,
                                             card.program or "") or bus_values
                    if not values:
                        continue
                    # Re-push unchanged values periodically anyway: the card
                    # shows a staleness marker when nothing arrives, and a
                    # silent feed is indistinguishable from a dead one.
                    if push(card.host, values):
                        if self._last.get(card.host) != values:
                            log.info("values -> %s (%s): %d keys",
                                     card.host, card.program or "bus", len(values))
                            self._last[card.host] = dict(values)
                    else:
                        log.warning("value push to %s failed", card.host)
            except Exception:
                log.exception("value push tick")
            time.sleep(self.interval_s)
