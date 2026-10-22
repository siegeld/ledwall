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
import hashlib
import logging
import queue
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


def _stop_program(host: str) -> None:
    """Take a card out of program mode.

    The counterpart of `_start_program`, and the reason it exists: a card whose
    marquee mode is `stream` may still be RUNNING an on-board program -- left
    on by a previous session, applied from a boot config, or set by hand on the
    card's own API. Nothing used to clear it, so the player would stream into a
    panel that was also composing its own pixels. Two writers on one
    framebuffer; see the `panel-program-two-writers` note. The firmware holds
    the stream while a program is active, so the panel shows the PROGRAM while
    marquee reports it as streaming -- a display that looks fine and is showing
    the wrong thing.
    """
    import urllib.request
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"http://{host}/api/program/off", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"}), timeout=3).read()
        log.info("stopped on-board program on %s", host)
    except Exception as e:
        log.info("could not stop program on %s (%s)", host, e)


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
        self.packet_delay = card.packet_delay
        self.stream = CardStream(card.host, card.port, packet_delay=card.packet_delay)
        self.last_palette: bytes | None = None
        # Set once the render thread retires this link. The SENDER thread holds
        # references to links inside queued payloads, and the queue is two
        # seconds deep, so a link can be closed while frames that name it are
        # still waiting to go out. Without this flag the sender then writes to
        # a closed socket -- a card removed from a wall, or one whose geometry
        # changed, is enough to trigger it, and the symptom is an exception
        # traceback per frame for as long as the backlog lasts.
        #
        # A plain bool is sufficient: one thread only ever sets it, the other
        # only ever reads it, and a frame sent a moment before the flag lands
        # is merely a frame to a card that is going away.
        self.closed = False
        _enable_card_dma(card.host)

    def matches(self, card: Card) -> bool:
        # packet_delay is in here because the CardStream bakes it at
        # construction: without it, editing a card's pacing changed the DB and
        # nothing else, and the only way to apply it was to restart the whole
        # player. That is exactly the knob you reach for while tuning against a
        # panel, so a silent no-op is the worst possible behaviour -- you
        # conclude the pacing does not matter.
        return (self.host == card.host and self.box == card.box
                and (self.w, self.h) == (card.width, card.height)
                and self.packet_delay == card.packet_delay)

    def close(self):
        self.closed = True
        self.stream.close()


# How far ahead of real time the renderer works. This is the lead that
# makes the queue a buffer rather than an ornament: a compose overrunning
# by less than this never reaches the glass late. Half a second is ten
# frames at 20 fps, well inside the queue, and far below the two seconds
# of latency nobody watching recorded video can perceive.
LEAD_S = 0.5


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
        # Hash of the show body in use, so an EDIT reloads the renderer and not
        # just a change of show. See the reload check in run().
        self._doc_sig: str | None = None
        # fps_rendered is the COMPOSE rate; fps_actual is what was actually put
        # on the wire. They differ whenever the renderer skips indices to keep
        # up, which is the case worth seeing, so they are counted separately.
        # Both used to be a single `fps_actual` measured on the render thread,
        # which named the compose rate as if it were the delivered one.
        self.fps_actual = 0.0
        self.fps_rendered = 0.0
        self._rendered = 0
        self._rendered_t0 = 0.0
        self._last_render_done = 0.0
        # Set by the render loop so the monitor thread can ask the renderer
        # what it is spending its time on.
        self._renderer = None
        self._target_fps = 20.0
        # DEEP, because none of this is live.
        #
        # Three frames was only 150 ms of cover and left 0.28 dropped frames/s
        # on video. These are recorded films and rendered signage, not a camera
        # feed, so lead time costs nothing a viewer can perceive -- and a deep
        # queue is what absorbs a compose that overruns badly rather than
        # slightly. Forty frames is two seconds at 20 fps.
        #
        # It does NOT hide a renderer that is persistently too slow: that still
        # shows up as the resync in the render loop and as `sends_late` here,
        # because every frame carries an ABSOLUTE deadline and the sender
        # reports one it could not meet. It no longer DROPS a past-due frame --
        # see the note in _sender(); dropping took the palette with it.
        self._q: "queue.Queue" = queue.Queue(maxsize=40)
        self.sends_late = 0
        self._last_send = 0.0
        self._sent = 0
        self._sent_t0 = 0.0
        self._last_send_done = 0.0

    def _monitor(self):
        """Report health on a timer, from OUTSIDE the send loop.

        The first version of this logged from inside `_sender`, after a
        successful send. That makes the instrument go silent exactly when it
        matters: on 2026-10-21 a build on this host starved the sender until
        each 600 us inter-packet sleep took ~10 ms, frames fell to 1.5/s, and
        the reports simply STOPPED -- so the log showed a healthy 19.9 fps and
        then nothing, and the stall had to be inferred from the card instead.
        A watchdog that dies with its subject is not a watchdog.

        This thread owns the line, so a sender that is slow, blocked or dead
        all read differently: `last send` grows, and `queue` fills behind it.
        """
        while not self.stop_flag.is_set():
            if self.stop_flag.wait(30.0):
                return
            now = time.time()
            span = now - (self._sent_t0 or now)
            if span > 0:
                self.fps_actual = self._sent / span
            rspan = now - (self._rendered_t0 or now)
            if rspan > 0:
                self.fps_rendered = self._rendered / rspan
            since = now - self._last_send_done if self._last_send_done else -1.0
            rsince = now - self._last_render_done if self._last_render_done else -1.0
            log.info(
                "wall %s: %.1f fps sent, %.1f fps rendered, %d late, "
                "queue %d/%d, last send %.1fs ago, last render %.1fs ago",
                self.wall_id, self.fps_actual, self.fps_rendered,
                self.sends_late, self._q.qsize(), self._q.maxsize, since, rsince,
            )
            # Name the stalled party. A sender with an empty queue is waiting
            # on the RENDERER, not blocked itself, and saying so is the
            # difference between looking at ffmpeg and looking at the network.
            if rsince > 2.0:
                log.warning(
                    "wall %s: RENDERER has not produced a frame in %.1fs "
                    "-- decode/source stall, not the network",
                    self.wall_id, rsince,
                )
            # WHY compose is slow, not just that it is. A show running under
            # its own fps was a dead end all day because nothing said where
            # the time went. frame_ms against the frame period says whether
            # compose can keep up; layer_ms says which layer to blame; and the
            # decoder hold_pct separates a starving SOURCE from a starving CPU.
            r = self._renderer
            if r is not None and self.fps_rendered < 0.95 * self._target_fps:
                try:
                    st = r.stats()
                    budget = 1000.0 / max(self._target_fps, 1)
                    log.warning(
                        "wall %s: compose %.1f fps vs %.0f target -- "
                        "frame %.1fms (max %.1f) of %.1fms budget, layers %s, "
                        "decoders %s",
                        self.wall_id, self.fps_rendered, self._target_fps,
                        st["frame_ms"], st["frame_ms_max"], budget,
                        st["layer_ms"], st["decoders"],
                    )
                except Exception:
                    log.exception("wall %s: renderer stats", self.wall_id)
            # A sender that has not completed a frame in several periods is
            # stalled, whatever the averages say. Say so plainly rather than
            # leaving it to be read out of a rate that is still decaying.
            if since > 2.0:
                log.warning(
                    "wall %s: sender has not completed a frame in %.1fs "
                    "(queue %d/%d) -- starved or blocked, not merely slow",
                    self.wall_id, since, self._q.qsize(), self._q.maxsize,
                )
            # Rolling window, so a bad half-minute is visible instead of being
            # averaged into an hour of good ones.
            self._sent, self._sent_t0 = 0, now
            self._rendered, self._rendered_t0 = 0, now

    def _sender(self):
        """Transmit queued frames on the clock, whatever the renderer is doing.

        This is the half that must be punctual. It does no image work at all:
        everything expensive happened on the render thread, and what arrives
        here is bytes plus the deadline they belong to.
        """
        fps_hint = [20.0]
        while not self.stop_flag.is_set():
            try:
                deadline, payloads, rate = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            fps_hint[0] = rate
            # NEVER two frames back to back, whatever the deadlines say.
            #
            # When the renderer skips indices to keep up with the clock,
            # several frames can arrive with deadlines already past, and firing
            # them consecutively bunches ~68 packets per frame into one burst.
            # A burst like that is what the card cannot absorb. Measured on
            # the P1.25 panel, same content, one writer:
            #
            #   bunched   0.46  dropped frames/s   @ ~19.5 fps delivered
            #   spaced    0.158 dropped frames/s   @  15.7 fps delivered
            #   spaced    0.73  dropped frames/s   @  19.3 fps delivered
            #
            # Read the third row before drawing a conclusion from the second.
            # The card's loss curve is steeply nonlinear in DELIVERED frame
            # rate -- +23% frames gave +187% c_dropped -- so the 0.158 figure
            # is a light-load artefact from before this file paced correctly,
            # not evidence that spacing fixed the drops. What spacing
            # demonstrably fixed is JITTER (36ms -> 0-3ms) and bunching.
            # Always record delivered fps next to a drop rate here.
            #
            # Where the loss actually happens is NOT the MAC receive FIFO --
            # mac_crc_errors is 0 across ~37M frames and pixel chunks bypass
            # the CPU's MAC slots entirely. It is the gateware gate's
            # `c_dropped`: the pixel DMA gets backpressured by LiteDRAM while
            # the scan-out reader holds the port, and the rest of that chunk is
            # discarded. A burst makes that collision far likelier, which is
            # why spacing helps even though the wire is nowhere near full.
            # Minimum spacing costs nothing -- it is the rate the show asked
            # for.
            # The floor is slightly TIGHTER than the frame interval, and that
            # 10% matters. At exactly 1/fps the sender can never drain a
            # backlog: the deadline still governs normal pacing, but any
            # transient that puts a frame into the queue leaves it there
            # forever, so the queue grows monotonically and the picture falls
            # further behind real time without bound. Caught by the 30s report
            # within two minutes of adding it -- queue 11 -> 16 and `late`
            # 0 -> 294 while fps looked perfect at 19.9.
            #
            # With headroom the deadline governs when we are on time (so the
            # cadence is still the show's) and the sender can catch up at
            # ~1.11x when it is behind. It cannot run AHEAD of the show,
            # because `deadline` is the other half of the max().
            now = time.time()
            earliest = self._last_send + 0.9 / max(fps_hint[0], 1.0)
            wait = max(deadline, earliest) - now
            if wait > 0:
                time.sleep(wait)
            self._last_send = time.time()
            slack = deadline - time.time()
            if slack < -0.25:
                self.sends_late += 1
                # DO NOT `continue`. Dropping a composed frame here used to
                # look like schedule hygiene and was strictly worse than
                # sending it late: the compose is already paid for, and the
                # old welded loop -- which this replaced -- simply ran slightly
                # slow, which the render loop's own comment argues is the
                # better failure. Worse, dropping took the PALETTE with it
                # (see below), so an indexed show lost its colours for good.
                # Fall through and send.
                pass
            for link, data, palette, indexed in payloads:
                # Retired between queueing and now -- see _CardLink.closed.
                if link.closed:
                    continue
                try:
                    if indexed:
                        # The palette compare happens HERE, on the thread that
                        # actually transmits, and `last_palette` advances only
                        # after `send_palette` returns.
                        #
                        # Doing it on the render thread meant the bookkeeping
                        # said "sent" the moment the frame was QUEUED. Any
                        # later discard -- a drop, or an exception out of
                        # send_palette -- lost the palette while the state
                        # claimed otherwise, and the next frame then computed
                        # "unchanged" and sent nothing. The card keeps applying
                        # whatever palette it holds, so the show renders in the
                        # PREVIOUS scene's colours indefinitely: right shapes,
                        # wrong colours, every counter clean. That is the exact
                        # failure the render-side comment warns about, and
                        # moving the send off-thread re-introduced it.
                        if palette is not None and palette != link.last_palette:
                            link.stream.send_palette(palette)
                            link.last_palette = palette
                        link.stream.send_frame_indexed(data, link.w, link.h)
                    else:
                        link.stream.send_frame(data, link.w, link.h)
                except Exception:
                    log.exception("wall %s send", self.wall_id)

            # Give the counters a READER. Both fps_actual and sends_late were
            # incremented and never looked at by anything -- no log line, no
            # endpoint, no caller -- which is the same as not measuring at all,
            # and this loop was tuned four separate ways against numbers that
            # had to be read off the CARD because the sender reported none.
            #
            # fps here is frames that actually went out, which is the number
            # that corresponds to what a viewer sees; fps_rendered is the
            # compose rate. A persistent gap between them means the renderer is
            # skipping, and `late` plus `qdepth` say whether the queue is
            # covering for it or has run dry.
            self._sent += 1
            self._last_send_done = time.time()

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
        threading.Thread(target=self._sender, daemon=True,
                         name=f"send-{self.wall_id}").start()
        threading.Thread(target=self._monitor, daemon=True,
                         name=f"mon-{self.wall_id}").start()
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

                # RELOAD ON AN EDIT, not only on a different show.
                #
                # This compared the show ID alone, so editing a show's BODY
                # changed nothing on the wall until something else forced a
                # reload -- switching shows, or restarting the player. The
                # body was being re-read from the database every iteration and
                # then ignored, which is the worst version of this: the data
                # was right there.
                #
                # Caught on 2026-10-21 setting fps 20 -> 22.22 on the live rail
                # board. The database held 22.22, the player kept sending 20.00,
                # and nothing anywhere reported a disagreement. Anyone editing a
                # show in the UI hit this every time.
                doc_sig = hashlib.sha256((doc or "").encode()).hexdigest()
                if want != self.current_show_id or doc_sig != self._doc_sig:
                    if renderer:
                        renderer.close()
                    renderer = None
                    if want != self.current_show_id:
                        self.current_show_id = want
                    elif doc is not None:
                        log.info("wall %s: show %s edited -- reloading",
                                 self.wall_id, want)
                    self._doc_sig = doc_sig
                    t0, n, last_idx = time.time(), 0, -1
                    # Drop whatever is still queued for the OLD show, or up to
                    # the queue's depth of the previous scene is transmitted
                    # after the change -- two seconds of the wrong thing.
                    try:
                        while True:
                            self._q.get_nowait()
                    except queue.Empty:
                        pass

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
                self._renderer = renderer
                self._target_fps = float(fps or 20)

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
                # MONOTONIC, so the renderer can get AHEAD of real time.
                #
                # This used to be `int((now - t0) * fps)` plus a sleep back to
                # the next index, which paced the renderer to the wall clock --
                # and that made the whole queue pointless. The index was always
                # the frame due NOW, so its deadline `t0 + idx/fps` was already
                # in the past by construction (int() floors), the sender's
                # sleep branch was unreachable, and the queue never held more
                # than nothing. The only net effect was that a frame whose
                # compose overran got composed at full cost and thrown away,
                # where the old welded loop sent it late.
                #
                # Advancing by one and letting `_q.put` block is what actually
                # creates lead: the renderer runs ahead until the queue is full
                # and is then paced by the SENDER, which is paced by the clock.
                # Falling behind is still caught, by the resync below.
                # Wall clock PLUS a lead, so the show tracks real time AND
                # the sender has something to wait on.
                #
                # Two wrong versions got here first. The original derived idx
                # from elapsed time and slept back to it, which tracks the
                # clock but pins the renderer to it: the deadline `t0+idx/fps`
                # was always already past (int() floors), the sender never
                # slept, and the queue never held a frame -- so the buffer
                # absorbed nothing and merely discarded late frames.
                #
                # The fix for that, a monotonic counter, gave real lead but
                # stopped skipping, so a show the renderer cannot sustain plays
                # SLOW -- measured 16.5 fps against a 20 fps show, and a clock
                # on the glass would drift by a fifth.
                #
                # Rendering LEAD_S ahead gives both: deadlines land in the
                # future so the sender genuinely paces, while the index is
                # still time-derived, so falling behind skips an index and the
                # clock stays right.
                idx = int((time.time() - t0 + LEAD_S) * fps)
                if idx <= last_idx:
                    time.sleep(max(0.0,
                        t0 + (last_idx + 1) / fps - LEAD_S - time.time()))
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

                # PREPARE here, SEND on the sender thread.
                #
                # Rendering and sending used to be welded together: compose,
                # transmit, sleep to the next deadline. So one slow compose
                # made the TRANSMIT late too, and a late frame is one the card
                # discards. Measured 2026-10-20: 0.24 dropped frames/s on video
                # against 0.02/s on content costing nothing to render -- a
                # twelve-fold difference, entirely this coupling, on a 32-core
                # host idling at a load of 2.
                #
                # The bounded queue decouples them. The renderer runs a few
                # frames ahead and the sender transmits on a strict schedule,
                # so an occasional overrun is absorbed instead of reaching the
                # glass. Both threads progress: PIL and socket sends release
                # the GIL.
                payloads = []
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
                        # Carry the palette unconditionally; the sender
                        # decides whether it is new, because only the sender
                        # knows whether the previous one actually went out.
                        payloads.append((link, idxs, palette, True))
                    else:
                        payloads.append((link, frame_to_rgb(frame), None, False))

                # Blocks when the sender is behind: the backpressure that
                # paces the renderer, and the reason it may run ahead at all.
                # Interruptible, or stopping a wall with a full queue wedges
                # this thread forever and leaks the show's ffmpeg children and
                # every card socket with it.
                while not self.stop_flag.is_set():
                    try:
                        self._q.put((t0 + idx / fps, payloads, fps), timeout=0.5)
                        break
                    except queue.Full:
                        continue
                if self.stop_flag.is_set():
                    break

                n += 1
                elapsed = time.time() - t0
                # ROLLING, not cumulative. This was `n / elapsed` since the
                # show started, so after 44 minutes it read a healthy 19.4 fps
                # while the renderer was stalled for 80 s at a stretch -- and
                # the stall was invisible in the only line that reports it.
                # fps_actual was already windowed; this was not, which left
                # exactly one half of the pipeline unobservable.
                self._rendered += 1
                self._last_render_done = time.time()
                self.fps_rendered = n / max(1e-6, elapsed)

                # Pace against the TIME-DERIVED index, not the count of frames
                # sent. Once a frame is skipped n falls behind idx, so a
                # deadline built from n is already in the past and the loop
                # spins instead of sleeping.
                # NO SLEEP HERE any more: the bounded queue is the pacing.
                # The renderer runs ahead by at most its depth and then blocks
                # on `put`, so it tracks real time without the send waiting on
                # it. The resync below still matters -- it catches a wall that
                # genuinely cannot absorb the rate, as opposed to one frame
                # that took too long.
                nxt = t0 + (idx + 1) / fps
                slack = nxt - time.time()
                if -slack > 1.0:
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
                    if streamable and card_streams:
                        # ONE WRITER. Reconciling only the program-mode case
                        # below left the opposite hole open: a stream-mode card
                        # still running an on-board program gets streamed into
                        # by us while it composes its own pixels. The firmware
                        # resolves that by holding the stream, so the glass
                        # shows the program and marquee says "streaming" --
                        # indistinguishable from working, from here.
                        st = _program_state(c.host)
                        if st is not None and st[1]:
                            log.warning("%s is mode=stream but is RUNNING "
                                        "program %r -- stopping it so there is "
                                        "one writer", c.host, st[0] or "(unnamed)")
                            _stop_program(c.host)
                        if _dma_is_off(c.host):
                            log.warning("pixel DMA was off on %s (card "
                                        "rebooted?) -- re-enabling", c.host)
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
