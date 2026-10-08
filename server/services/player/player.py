"""Renders each enabled wall's current show and streams it to that wall's cards.

One thread per WALL, not per card. The wall is the unit of content -- one show,
one frame clock, one rendered canvas -- and each card is sent its own crop of
that canvas. Rendering once and cropping N times is both cheaper than rendering
per card and the only way two boards can show halves of one picture without
drifting apart: they are handed the same frame, from the same render, in the
same pass.

Rendering is CPU-bound in PIL/ffmpeg and streaming is paced, so walls must not
block each other. The scheduler is re-evaluated every tick, so a daypart change
or an /api/v1/play override takes effect within one scene frame without
restarting anything.
"""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from marquee_core.models import Card, CardStat, Schedule, Show, Wall, resolve_show_for
from marquee_core.poller import sample
from marquee_core.render import Renderer, frame_to_indexed, frame_to_rgb
from marquee_core.scene import load_show
from marquee_core.sources import Resolver
from marquee_core.stream import CardStream

log = logging.getLogger("marquee.player")


def _dma_is_off(host: str) -> bool:
    """True when the card is running the CPU pixel path.

    A card reboots with DMA OFF. Enabling it once when the player starts is
    therefore not enough: any reboot -- a power cycle, a firmware reload, a
    watchdog -- silently drops that board onto the ~600k px/s CPU path, where
    the receive ISR cannot drain the MAC FIFO. The visible result is not "slow":
    ~3 chunks of every 12 are lost, so NO frame ever completes and the card
    keeps showing whatever was on it before, which looks like a frozen or
    corrupt display rather than a performance problem. It cost most of an
    evening to find, twice.
    """
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}/api/status", timeout=3) as r:
            return json.loads(r.read().decode()).get("dma_enabled") == 0
    except Exception:
        return False


def _program_state(host: str) -> tuple[str, bool] | None:
    """(active program, running) on a card, or None if it cannot be asked."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{host}/api/programs", timeout=3) as r:
            d = json.loads(r.read().decode())
        return d.get("active") or "", bool(d.get("running"))
    except Exception:
        return None


def _start_program(host: str, name: str | None) -> None:
    import json
    import urllib.request
    try:
        body = json.dumps({"name": name}).encode() if name else b"{}"
        urllib.request.urlopen(urllib.request.Request(
            f"http://{host}/api/program/on", data=body, method="POST",
            headers={"Content-Type": "application/json"}), timeout=3).read()
        log.info("started program %s on %s", name or "(default)", host)
    except Exception as e:
        log.info("could not start program on %s (%s)", host, e)


def _enable_card_dma(host: str) -> None:
    import urllib.request
    try:
        urllib.request.urlopen(
            urllib.request.Request(f"http://{host}/api/dma/on", method="POST"),
            timeout=3,
        ).read()
        log.info("enabled hardware pixel DMA on %s", host)
    except Exception as e:
        # Older firmware has no such endpoint; streaming still works on the
        # slow path provided packet_delay is conservative.
        log.info("could not enable DMA on %s (%s)", host, e)


class _CardLink:
    """A card's stream plus the per-card state that goes with it."""

    def __init__(self, card: Card):
        self.id = card.id
        self.name = card.name
        self.host = card.host
        self.box = card.box
        self.w, self.h = card.width, card.height
        self.stream = CardStream(card.host, card.port, packet_delay=card.packet_delay)
        self.last_palette: bytes | None = None
        _enable_card_dma(card.host)

    def matches(self, card: Card) -> bool:
        return (self.host == card.host and self.box == card.box
                and (self.w, self.h) == (card.width, card.height))

    def close(self):
        self.stream.close()


class WallPlayer(threading.Thread):
    def __init__(self, wall_id: int, session_factory, resolver: Resolver, tz: str,
                 make_bindings=None):
        super().__init__(daemon=True, name=f"wall-{wall_id}")
        self.wall_id = wall_id
        self.sf = session_factory
        self.resolver = resolver
        self.tz = tz
        self.make_bindings = make_bindings
        self.stop_flag = threading.Event()
        self.current_show_id: int | None = None
        self.fps_actual = 0.0

    def _streamable_cards(self, wall: Wall) -> list[Card]:
        """Cards this wall should be sending pixels to, right now.

        A program-mode card composes its own pixels. Streaming at it is not
        merely wasted bandwidth: both write the framebuffer, so the picture
        corrupts. Skipping it here is the honest fix -- and on a multi-card wall
        one board can legitimately be in program mode while its neighbours
        stream, so this is per card rather than per wall.
        """
        return [c for c in wall.cards
                if c.enabled and (c.mode or "stream") != "program"]

    def run(self):
        renderer: Renderer | None = None
        links: dict[int, _CardLink] = {}
        t0 = time.time()
        n = 0
        last_idx = -1
        while not self.stop_flag.is_set():
            try:
                with self.sf() as db:
                    wall = db.scalars(
                        select(Wall).where(Wall.id == self.wall_id)
                        .options(selectinload(Wall.cards),
                                 selectinload(Wall.assignments))).first()
                    if not wall or not wall.enabled:
                        time.sleep(2); continue
                    scheds = list(db.scalars(
                        select(Schedule).where(Schedule.wall_id == wall.id)).all())
                    want = resolve_show_for(wall, scheds, dt.datetime.now())
                    doc = db.get(Show, want).body if want else None
                    cards = self._streamable_cards(wall)
                    wall_w, wall_h = wall.width, wall.height
                    # Detach: the session closes below and the render loop must
                    # not touch lazily-loaded attributes afterwards.
                    cards = [db.merge(c) for c in cards]
                    db.expunge_all()

                if want != self.current_show_id:
                    if renderer:
                        renderer.close()
                    renderer = None
                    self.current_show_id = want
                    t0, n, last_idx = time.time(), 0, -1

                if doc is None or not cards:
                    time.sleep(2); continue

                if renderer is None:
                    renderer = Renderer(load_show(doc), resolver=self.resolver,
                                        bindings=self.make_bindings(self.tz))

                # Reconcile the set of card links against the DB every tick, so
                # a card added to, moved within, or removed from the wall takes
                # effect without a restart.
                live = {c.id for c in cards}
                for gone in [i for i in links if i not in live]:
                    links.pop(gone).close()
                for c in cards:
                    if c.id in links and not links[c.id].matches(c):
                        links.pop(c.id).close()
                    if c.id not in links:
                        links[c.id] = _CardLink(c)
                        log.info("wall %s: streaming %s at %s to %s",
                                 self.wall_id, c.box, c.name, c.host)

                fps = renderer.show.display.fps

                # Render at the SCHEDULED frame time, not the wall clock.
                #
                # Using elapsed wall-clock made every frame advance the scene by
                # however long the previous render+send happened to take, so
                # scrolling text moved in uneven jumps -- visible stutter. Frame
                # time advances by exactly 1/fps per frame, so motion is uniform
                # even when delivery is late; falling behind then shows as
                # slightly slow playback rather than judder, which is far less
                # objectionable.
                # Frame index derived from elapsed time, so the show tracks the
                # real clock: pure n/fps is uniform but silently runs slow if a
                # card cannot absorb the rate, and a wall clock on screen would
                # then drift behind the actual time. This skips an index instead.
                idx = int((time.time() - t0) * fps)
                if idx <= last_idx:
                    time.sleep(max(0.0, t0 + (last_idx + 1) / fps - time.time()))
                    continue
                last_idx = idx

                # ONE render of the whole canvas, however many cards cover it,
                # AT THE WALL'S OWN SIZE. A show with fractional boxes lays
                # itself out against that size, so nothing is drawn small and
                # stretched afterwards. The resize below is the fallback for a
                # show that declares a fixed display size not matching this
                # wall -- it still works, it is just scaled, which is exactly
                # what fractional boxes exist to avoid.
                canvas = renderer.render(idx / fps, (wall_w, wall_h))
                if canvas.size != (wall_w, wall_h):
                    canvas = canvas.resize((wall_w, wall_h))
                indexed = renderer.show.display.format == "indexed"

                for c in cards:
                    link = links[c.id]
                    frame = canvas.crop(link.box) if link.box != (0, 0, wall_w, wall_h) \
                        else canvas
                    if indexed:
                        idxs, palette = frame_to_indexed(frame)
                        # Send the palette only when it actually changes. Median
                        # cut is stable across similar frames, so on most content
                        # this is one extra packet at scene start rather than one
                        # per frame. It MUST precede the first frame that uses
                        # it: the card applies whatever palette it holds, so a
                        # frame arriving first renders through the previous
                        # scene's colours.
                        #
                        # Per card, because each card quantises its OWN crop --
                        # two crops of one canvas do not generally produce the
                        # same palette.
                        if palette != link.last_palette:
                            link.stream.send_palette(palette)
                            link.last_palette = palette
                        link.stream.send_frame_indexed(idxs, link.w, link.h)
                    else:
                        link.stream.send_frame(frame_to_rgb(frame), link.w, link.h)

                n += 1
                elapsed = time.time() - t0
                self.fps_actual = n / max(1e-6, elapsed)

                # Pace against the TIME-DERIVED index, not the count of frames
                # sent. Once a frame is skipped n falls behind idx, so a
                # deadline built from n is already in the past and the loop
                # spins instead of sleeping.
                nxt = t0 + (idx + 1) / fps
                slack = nxt - time.time()
                if slack > 0:
                    time.sleep(slack)
                elif -slack > 1.0:
                    # Persistently behind (the wall cannot absorb this rate).
                    # Resync in one jump instead of accumulating unbounded lag,
                    # which would otherwise make the scene drift further from
                    # real time every second.
                    t0 = time.time() - n / fps
            except Exception:
                log.exception("wall %s render loop", self.wall_id)
                time.sleep(2)
        if renderer:
            renderer.close()
        for link in links.values():
            link.close()


class Supervisor:
    """Keeps one player per enabled wall, and samples per-card stats."""

    def __init__(self, session_factory, resolver: Resolver, tz: str, poll_s: float = 15.0,
                 make_bindings=None):
        self.sf = session_factory
        self.resolver = resolver
        self.tz = tz
        self.poll_s = poll_s
        self.make_bindings = make_bindings
        self.players: dict[int, WallPlayer] = {}
        self._last: dict[int, CardStat] = {}

    def tick(self):
        with self.sf() as db:
            walls = list(db.scalars(
                select(Wall).options(selectinload(Wall.cards))).all())
            for w in walls:
                # A wall with nothing to stream to needs no player: every card
                # is disabled, or every card runs its own on-board program.
                streamable = w.enabled and any(
                    c.enabled and (c.mode or "stream") != "program" for c in w.cards)
                if streamable and w.id not in self.players:
                    pl = WallPlayer(w.id, self.sf, self.resolver, self.tz,
                                    self.make_bindings)
                    pl.start(); self.players[w.id] = pl
                    log.info("started player for wall %s (%d card(s), %dx%d)",
                             w.name, len(w.cards), w.width, w.height)
                elif not streamable and w.id in self.players:
                    self.players.pop(w.id).stop_flag.set()

                for c in w.cards:
                    # Re-assert DMA rather than assuming it survived. Cheap: one
                    # GET per card per tick, off the render path entirely.
                    card_streams = c.enabled and (c.mode or "stream") != "program"
                    if streamable and card_streams and _dma_is_off(c.host):
                        log.warning("pixel DMA was off on %s (card rebooted?) "
                                    "-- re-enabling", c.host)
                        _enable_card_dma(c.host)
                    # Same idea for the other mode: a program-mode card that is
                    # not actually RUNNING its program is a dead sign. It shows
                    # a stale frame and nothing streams to it either, because
                    # the player skips program-mode cards -- so it looks like a
                    # working display and is not one. Reboots cause it (the
                    # program is applied from the boot config, and a card that
                    # came up before marquee answered has none), and so does
                    # anything that stops the program.
                    if c.enabled and not card_streams:
                        st = _program_state(c.host)
                        if st is not None:
                            active, running = st
                            want = c.program or ""
                            if not running or (want and active != want):
                                log.warning("%s should be running program %r "
                                            "but has active=%r running=%s "
                                            "-- starting it",
                                            c.host, want or "(default)",
                                            active, running)
                                _start_program(c.host, c.program)
                    if self.poll_s > 0:
                        # Sampling costs the CARD, not us: it serves /api/status
                        # from the same interrupt handler that consumes the
                        # pixel stream, over a single socket, so every poll is a
                        # short gap in frame processing. Set
                        # MARQUEE_POLL_INTERVAL_S=0 to stop sampling entirely
                        # when smoothness matters more than history.
                        st = sample(c, self._last.get(c.id))
                        self._last[c.id] = st
                        db.add(st)
            db.commit()

    def run(self):
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("supervisor tick")
            time.sleep(self.poll_s if self.poll_s > 0 else 30.0)
