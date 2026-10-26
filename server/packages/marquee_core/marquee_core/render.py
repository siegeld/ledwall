"""Compositor: a Show plus a wall-clock time -> one RGB frame.

Pillow does all compositing; ffmpeg decodes video. No numpy: at 256x128 the
frame is 32k pixels and PIL's C paths are comfortably fast enough, and avoiding
numpy keeps the image small.

The renderer is a pure function of (show, t) apart from video decoders, which
necessarily hold position. That makes previewing a frame in the web UI exactly
the same code path as streaming it, so what an operator previews is what a panel
shows.
"""

from __future__ import annotations

import math
import os
import random
import select
import subprocess
import threading
import time
from dataclasses import dataclass, field

from PIL import Image, ImageDraw, ImageFont

from .bindings import _FAIL, Bindings
from .live import dig as _dig
from .scene import Box, Scene, Show, _dim
from .sources import Resolver, SourceError


@dataclass
class _Cell:
    """One table cell, shaped like the part of a TextLayer that `_text` reads.

    `_text` takes a layer and uses box/text/size/color/font/align/valign/
    scroll/speed and nothing else, so a table cell can BE one without being a
    TextLayer -- which would drag in pydantic validation per cell, per frame,
    on a board redrawing twenty times a second.
    """

    box: "Box"
    text: str
    size: int
    color: str
    font: str | None
    align: str
    valign: str
    scroll: str
    speed: float


_FONT_CANDIDATES = [
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/gnu-free/FreeSansBold.ttf",
]


class _ScrollStrip:
    """One rendering of a scrolling string, reused across frames.

    WHY A STRIP. The scroll used to rasterise the WHOLE string twice every
    frame. A crawl is far wider than its window by definition -- the live
    banner is 1993 px of text in a 248 px box -- so nearly all of that work was
    discarded at the clip, measured at 2.71 ms a frame.

    The obvious fix, drawing only the visible substring, is WRONG and the test
    caught it: the substring has to be placed at a fractional x, and PIL
    anti-aliases a fractional position differently from the integer one a full
    draw uses, so every glyph edge shifted. 25 of 1028 scroll positions
    differed.

    Drawing once into a strip and blitting a window out of it is exact by
    construction -- the pixels ARE the full draw's pixels -- and it also stops
    re-rendering text that changes once a minute at twenty frames a second.

    The cost that matters is not only CPU. The render thread and the send
    thread are both Python, so every millisecond the renderer holds the GIL is
    a millisecond the pacing loop cannot run, and a stalled sender lets the
    kernel drain its 1 MB socket buffer at line rate -- which overflows the
    card's FIFO and costs whole chunks. Measured on the two live shows: the
    rail board composes in 2.2 ms and drops nothing; the movie banner composes
    in 8-10 ms and drops 12.96 chunks/s.
    """

    __slots__ = ("img", "span", "key")

    def __init__(self, text, font, colour, h, gap, y):
        box = ImageDraw.Draw(Image.new("RGBA", (1, 1))).textbbox(
            (0, 0), text, font=font)
        tw = box[2] - box[0]
        self.span = tw + gap
        self.img = Image.new("RGBA", (self.span, h), (0, 0, 0, 0))
        ImageDraw.Draw(self.img).text((-box[0], y), text, font=font, fill=colour)

    def window(self, dst, offset):
        """Paste the `dst`-wide view starting at `offset`, wrapping."""
        w = dst.size[0]
        offset %= self.span
        first = min(w, self.span - offset)
        dst.paste(self.img.crop((offset, 0, offset + first, dst.size[1])), (0, 0))
        taken = first
        while taken < w:                       # a window wider than the strip
            n = min(w - taken, self.span)
            dst.paste(self.img.crop((0, 0, n, dst.size[1])), (taken, 0))
            taken += n


_STRIP_CACHE: dict = {}


def _scroll_strip(text, font, colour, h, gap, y, size, path):
    key = (text, size, path, colour, h, gap, y)
    hit = _STRIP_CACHE.get(key)
    if hit is None:
        if len(_STRIP_CACHE) > 24:             # bounded: a crawl changes rarely
            _STRIP_CACHE.clear()
        hit = _STRIP_CACHE[key] = _ScrollStrip(text, font, colour, h, gap, y)
    return hit


def _load_font(size: int, path: str | None = None) -> ImageFont.FreeTypeFont:
    for cand in ([path] if path else []) + _FONT_CANDIDATES:
        if not cand:
            continue
        try:
            return ImageFont.truetype(cand, size)
        except OSError:
            continue
    # Never fail a render for a missing font -- a sign with ugly text beats a
    # black sign.
    return ImageFont.load_default()


# Probed source sizes, keyed by path. Module-level so it survives the decoder
# that filled it -- see VideoDecoder._source_size.
# (storage size, DISPLAY size). They differ on anamorphic files, where the
# pixels are not square -- see `_source_size`.
_SRC_SIZE_CACHE: dict[str, tuple[tuple[int, int], tuple[int, int]] | None] = {}


class VideoDecoder:
    """Decodes a video to raw RGB frames at a fixed size via ffmpeg.

    Holds a pipe and hands out the most recent frame. Looping restarts the
    process, which is cheap relative to a scene duration.
    """

    def __init__(self, path: str, w: int, h: int, fps: float, loop: bool = True,
                 start: float = 0.0, fit: str = "cover"):
        self.path, self.w, self.h, self.fps = path, w, h, fps
        self.loop, self.start = loop, start
        # The FRAMING happens in ffmpeg, not in _fit(). It has to: the decoder
        # reads exactly w*h*3 bytes per frame, so whatever comes out of ffmpeg
        # is already the final size and _fit() would have nothing left to do.
        #
        # That was a silent bug rather than a design: the filter chain was
        # hard-coded to scale-up-and-crop, so `fit` was IGNORED on every video
        # layer. `contain` and `smart` did nothing and everything was
        # cover-cropped, whatever the show asked for.
        self.fit = fit
        self.proc: subprocess.Popen | None = None
        self.frame_bytes = w * h * 3
        self._last = Image.new("RGB", (w, h))
        # Set once the file runs out and `loop` is off. Without it a finished
        # video is indistinguishable from a stalled one -- both just keep
        # handing back the last frame -- so nothing above could tell that a
        # film had ENDED and it was time to choose another.
        self.ended = False
        # Bytes of a frame already read but not yet complete. A bounded read
        # can return a partial frame, and those bytes must survive to the next
        # call or the stream desynchronises and every later frame is shredded.
        self._partial = bytearray()
        # How long next_frame() may wait for ffmpeg before giving up and
        # handing back the previous frame. Half a frame period keeps the render
        # loop responsive while still letting a decoder that is merely a little
        # late deliver on time.
        self._read_budget = 0.5 / fps if fps else 0.025
        # INSTRUMENTATION. Without these, "the renderer is slow" is a guess:
        # a decoder starved by its source and a decoder starved of CPU look
        # identical from outside, and they have nothing in common as fixes.
        #   holds  -- bounded read expired, previous frame handed back
        #   reads  -- frames actually delivered
        #   wait_s -- cumulative seconds spent waiting on the pipe
        self.holds = 0
        self.reads = 0
        self.wait_s = 0.0
        self._open()

    def _open(self):
        self.close()
        # New stream, new alignment: any partial frame from the old process
        # would offset every frame after the loop point by those bytes.
        if hasattr(self, "_partial"):
            self._partial.clear()
        cmd = ["ffmpeg", "-loglevel", "error"]
        if self.start:
            cmd += ["-ss", str(self.start)]
        cmd += [
            "-i", self.path,
            "-vf", self._filter(),
            "-r", str(self.fps), "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def _source_size(self) -> tuple[int, int] | None:
        """Source size in SQUARE pixels -- the geometry a viewer should see.

        Probed once per FILE, ever. ffprobe costs 110-480ms on these files, so
        without a cache every decoder built for a film paid it again. Cached
        across decoders because a file's dimensions do not change; the only
        cost of being wrong would be a re-probe.

        This returns the DISPLAY size, not the stored size, and the two differ
        whenever the pixels are not square. The 1965 home movie is 1920x1080
        stored with a sample aspect ratio of 3:4 -- it is a 4:3 picture, and
        1440x1080 is its real shape. Treating 1920x1080 as 16:9 stretches it
        horizontally by a third, which on a face is obvious and was reported
        from the glass as "not square". `sample_aspect_ratio` is therefore part
        of the probe, and `_filter` normalises to square pixels before any
        scaling decision is made.
        """
        if getattr(self, "_src", "unset") != "unset":
            return self._src
        if self.path in _SRC_SIZE_CACHE:
            self._src_storage, self._src = _SRC_SIZE_CACHE[self.path] or (None, None)
            return self._src
        self._src = self._src_storage = None
        try:
            out = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,sample_aspect_ratio",
                 "-of", "csv=p=0:s=x", self.path],
                capture_output=True, text=True, timeout=20).stdout.strip()
            parts = out.split("x")
            w, h = int(parts[0]), int(parts[1])
            # "N:D", or "0:1"/"" when the file does not say. Unknown means
            # square: assuming otherwise would distort every ordinary video.
            sar = 1.0
            if len(parts) > 2 and ":" in parts[2]:
                num, den = parts[2].split(":")[:2]
                if int(num) > 0 and int(den) > 0:
                    sar = int(num) / int(den)
            if w > 0 and h > 0:
                self._src_storage = (w, h)
                self._src = (max(1, round(w * sar)), h)
        except Exception:
            pass
        _SRC_SIZE_CACHE[self.path] = (
            (self._src_storage, self._src) if self._src else None)
        return self._src

    def _filter(self) -> str:
        """The scale/crop/pad chain that applies this layer's `fit`.

        Always ends exactly w x h, because next_frame() reads a fixed number of
        bytes per frame and a different size would desynchronise the stream.
        """
        w, h = self.w, self.h
        # ASPECT IS ARITHMETIC, NOT AN EXTRA SCALE PASS.
        #
        # `force_original_aspect_ratio` and the `smart` arithmetic both reason
        # about STORED dimensions, and on an anamorphic file those are not the
        # picture's shape: the 1965 home movie stores 1920x1080 with a 3:4
        # sample aspect, so every fit mode treated a 4:3 picture as 16:9 and
        # stretched it by a third.
        #
        # The first fix for that prepended `scale=<display size>,setsar=1`,
        # which was correct and WASTEFUL: it resampled 1920x1080 to 1620x1080
        # before scaling down to 256x104, so every frame paid a full-resolution
        # pass for nothing. This loop is single-threaded and has a 50 ms budget
        # per frame at 20 fps; overrunning it is a frame the card never sees.
        # Measured 2026-10-20: 0.24 dropped frames/s on video against 0.02/s on
        # content that costs nothing to render.
        #
        # So compute the target size from the DISPLAY geometry here and emit
        # ONE scale. Same output, one resample instead of two.
        src = self._source_size()          # display size: square pixels
        if not src:
            # PROBE FAILED -- let ffmpeg do the aspect arithmetic, which is
            # what `cover`/`contain` did before the single-scale rewrite and is
            # probe-INDEPENDENT.
            #
            # Substituting the display box here instead makes the scale factor
            # 1.0, i.e. a hard STRETCH, on the default fit mode. And
            # `_source_size` caches a failure permanently, so one ffprobe
            # timeout -- 20 s, against /share/homemovies over NFS -- would
            # stretch that film for the life of the process.
            if self.fit == "stretch":
                return f"scale={w}:{h}"
            if self.fit == "contain":
                return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
            return (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                    f"crop={w}:{h}")
        dw, dh = src
        if self.fit == "stretch":
            return f"scale={w}:{h}"
        if self.fit == "contain":
            s_ = min(w / dw, h / dh)
            tw, th = max(1, round(dw * s_)), max(1, round(dh * s_))
            return (f"scale={tw}:{th},setsar=1,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
        if self.fit == "smart":
            src = self._source_size()
            if src:
                sw, sh = src
                sc = _smart_scale(sw, sh, w, h)
                tw, th = max(1, round(sw * sc)), max(1, round(sh * sc))
                # scale to the chosen size, crop whatever overflows, pad
                # whatever falls short -- both centred.
                return (f"scale={tw}:{th},setsar=1,"
                        f"crop={min(tw, w)}:{min(th, h)},"
                        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2")
            # Could not probe: fall back to the old behaviour rather than
            # guessing a scale from nothing.
        # cover: scale so the picture covers the box, then crop the overflow.
        s_ = max(w / dw, h / dh)
        tw, th = max(1, round(dw * s_)), max(1, round(dh * s_))
        return f"scale={tw}:{th},setsar=1,crop={w}:{h}"

    def close(self):
        if self.proc:
            try:
                self.proc.kill()
            except Exception:
                pass
            self.proc = None

    def next_frame(self) -> Image.Image:
        if not self.proc or not self.proc.stdout:
            self.ended = True
            return self._last
        t0 = time.monotonic()
        buf = self._read_frame()
        self.wait_s += time.monotonic() - t0
        if buf is None:
            # Nothing ready in the budget. HOLD, do not block the wall.
            self.holds += 1
            return self._last
        if len(buf) < self.frame_bytes:
            if self.loop:
                self._open()
                buf2 = self._read_frame()
                buf = buf2 if buf2 is not None else b""
            if not buf or len(buf) < self.frame_bytes:
                self.ended = True
                return self._last
        self._last = Image.frombytes("RGB", (self.w, self.h), bytes(buf))
        self.reads += 1
        return self._last

    def _read_frame(self):
        """One frame, or None if ffmpeg has not produced one in time.

        THE WHOLE POINT. This used to be `stdout.read(frame_bytes)`, a
        BLOCKING read of a full frame, so the single-threaded render loop sat
        in it until ffmpeg delivered. Anything that stalled the decoder -- a
        CIFS hiccup mid-file on a multi-gigabyte source, a codec that will not
        decode at the show's rate -- froze the ENTIRE wall for exactly as long
        as the stall. Measured 2026-10-21: repeated 70-80 second freezes, with
        the send queue draining to zero behind it, while the only counter that
        reported the render side was a cumulative average that still read a
        comfortable 19.4 fps.

        `ShufflePlayer` above is built on the right principle -- "a held frame
        is a held frame, a blocked loop stalls the whole wall" -- and then
        delegated to a call that blocked unboundedly. This closes that gap.

        Returns a full frame, b"" at EOF, or None for "not ready, hold".
        Partial bytes are kept in `self._partial` across calls: a bounded read
        can stop mid-frame, and discarding those bytes would desynchronise the
        rawvideo stream and shred every frame after it.
        """
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + self._read_budget
        while len(self._partial) < self.frame_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                ready, _, _ = select.select([fd], [], [], remaining)
            except (OSError, ValueError):
                return b""
            if not ready:
                return None
            try:
                chunk = os.read(fd, self.frame_bytes - len(self._partial))
            except (OSError, BlockingIOError):
                return b""
            if not chunk:
                # EOF. Hand back whatever we have so the caller's short-frame
                # path runs; the leftover is dropped with the process.
                out = bytes(self._partial)
                self._partial.clear()
                return out
            self._partial += chunk
        out = bytes(self._partial[:self.frame_bytes])
        del self._partial[:self.frame_bytes]
        return out


class ShufflePlayer:
    """Plays a library end to end, in a random order, forever.

    Why a shuffled BAG and not an independent random pick each time: with a few
    hundred films, picking independently repeats often enough to notice and to
    annoy -- the birthday problem, not a bug, but it reads as one. Drawing
    without replacement guarantees every film before any repeat, and only
    reshuffles once the bag is empty. On the reshuffle the old last film is
    pushed back one place, so the join between two bags cannot play the same
    thing twice in a row.

    Owns the same interface as VideoDecoder -- `next_frame()` -- so the layer
    above does not care which it has.
    """

    def __init__(self, pool: list[str], w: int, h: int, fps: float,
                 rng: random.Random | None = None, pick: str = "bag",
                 fit: str = "cover"):
        self.pool = list(pool)
        self.w, self.h, self.fps = w, h, fps
        self.rng = rng or random.Random()
        # "bag"    -- draw without replacement (every film before any repeat)
        # "random" -- independent pick each time, so repeats can happen
        self.pick = pick
        self.fit = fit
        self.bag: list[str] = []
        self.current: str | None = None
        self.dec: VideoDecoder | None = None
        # The next film, already decoding. See _prewarm().
        self.nxt: VideoDecoder | None = None
        self.nxt_path: str | None = None
        self._warming = False
        self.plays = 0
        self._advance()

    def _pick(self) -> str:
        """Choose the next film, honouring the pick policy.

        Called while the CURRENT film is still playing, so `self.current` is
        what is on screen and the back-to-back guards still mean what they say.
        """
        if self.pick == "random":
            # Independent pick. The ONE concession: never the same film twice
            # in a row. At 339 films that is a 1-in-339 event, so excluding it
            # changes nothing statistically, but when it does happen it reads
            # as "the picker is stuck" rather than as luck.
            choices = self.pool
            if self.current and len(self.pool) > 1:
                choices = [f for f in self.pool if f != self.current]
            return self.rng.choice(choices)
        if not self.bag:
            self.bag = list(self.pool)
            self.rng.shuffle(self.bag)
            if self.current and len(self.bag) > 1 and self.bag[0] == self.current:
                self.bag.append(self.bag.pop(0))
        return self.bag.pop(0)

    def _spawn(self, path: str) -> "VideoDecoder":
        # loop=False is the whole point: we want the file to END so we can
        # move to the next one.
        return VideoDecoder(path, self.w, self.h, self.fps, loop=False,
                            fit=self.fit)

    def _prewarm(self) -> None:
        """Start the NEXT film's ffmpeg now, while this one is still playing.

        Opening a multi-gigabyte file and initialising a decoder costs 120-470ms
        MEASURED, and the render loop is single-threaded -- so paying it at the
        transition stalled the whole wall for 3 to 9 frames every time a film
        changed. Paying it early costs nothing: ffmpeg does the work, fills its
        pipe buffer, and blocks. Measured after a 0.5s lead the first read is
        0.1ms, and more lead does not help, so there is nothing to tune.

        The cost is one extra ffmpeg per shuffling layer, asleep on a full pipe.
        """
        if self.nxt is not None or self._warming:
            return
        # Pick HERE, on the caller's thread: it touches the bag and
        # `self.current`, and doing that from a worker would race the swap.
        # Only the expensive part -- ffprobe plus ffmpeg startup, 110-480ms --
        # goes to the worker.
        path = self._pick()
        self.nxt_path = path
        self._warming = True

        def _work():
            try:
                dec = self._spawn(path)
            except Exception:
                dec = None
            self.nxt = dec
            self._warming = False

        threading.Thread(target=_work, name="prewarm", daemon=True).start()

    def _advance(self) -> None:
        if self.dec:
            self.dec.close()
        if self.nxt is not None:
            self.dec, self.current = self.nxt, self.nxt_path
            self.nxt = self.nxt_path = None
        else:
            # No warm one -- the very first film, or the warm one was consumed
            # by a dead-file retry. Pay the startup here rather than show
            # nothing.
            self.current = self._pick()
            self.dec = self._spawn(self.current)
        self.plays += 1
        self._prewarm()

    def next_frame(self) -> Image.Image:
        # A file that yields nothing -- unreadable, zero-length, a codec ffmpeg
        # will not touch -- ends immediately, and without a bound this would
        # spin through the whole bag inside one frame. Try a few, then hold the
        # last good frame and try again next tick rather than blocking the
        # render loop.
        for _ in range(4):
            assert self.dec is not None
            img = self.dec.next_frame()
            if not self.dec.ended:
                return img
            if self.nxt is None:
                # The film has ended and the next one is not open yet. Hold its
                # final frame for this tick rather than blocking the render
                # loop on ffmpeg -- a held frame is a held frame, a blocked
                # loop stalls the whole wall. Only reachable on a cold start or
                # if the worker is slow; the normal path has one ready.
                self._prewarm()
                return img
            self._advance()
        assert self.dec is not None
        return self.dec.next_frame()

    def close(self) -> None:
        if self.dec:
            self.dec.close()
            self.dec = None
        # The warm one is a live ffmpeg too; leaking it leaks a process and a
        # file handle every time a show changes.
        if self.nxt:
            self.nxt.close()
            self.nxt = self.nxt_path = None


# How much of the picture `cover` may throw away before we stop filling the
# box and start splitting the difference instead.
_SMART_CROP_BUDGET = 0.15


def _smart_scale(sw: int, sh: int, tw: int, th: int) -> float:
    """Zoom a bit, and centre -- decided from the two aspect ratios.

    `cover` fills the box and crops whatever overflows; `contain` shows
    everything and letterboxes the rest. On a well-matched box either is fine.
    On a badly matched one both are bad in the same measure: 16:9 footage on a
    square wall is 44% cropped by cover, or 44% black by contain.

    So: if cover would crop only a little, fill the box -- a thin black edge
    where a small crop would do looks like a fault. Otherwise take the
    GEOMETRIC MEAN of the two scales, which is the scale where the crop
    fraction and the letterbox fraction are exactly equal (their ratios are
    reciprocals, so neither side can be favoured). 16:9 on a square wall
    becomes 25% cropped and 25% black instead of 44% of either.

    Parameter-free on purpose. The right amount of zoom is a function of the
    two shapes, and the wall's shape is not known when a show is written.
    """
    s_cov = max(tw / sw, th / sh)
    cw, ch = sw * s_cov, sh * s_cov
    cover_crop = max(1 - tw / cw if cw > tw else 0.0,
                     1 - th / ch if ch > th else 0.0)
    if cover_crop <= _SMART_CROP_BUDGET:
        return s_cov
    return math.sqrt((tw / sw) * (th / sh))


def _fit(img: Image.Image, box: Box, mode: str) -> Image.Image:
    tw, th = box.w, box.h
    if mode == "stretch":
        return img.resize((tw, th), Image.LANCZOS)
    if mode == "none":
        return img
    sw, sh = img.size
    if mode == "smart":
        scale = _smart_scale(sw, sh, tw, th)
    else:
        scale = max(tw / sw, th / sh) if mode == "cover" else min(tw / sw, th / sh)
    nw, nh = max(1, int(sw * scale)), max(1, int(sh * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    if mode == "cover":
        left, top = (nw - tw) // 2, (nh - th) // 2
        return img.crop((left, top, left + tw, top + th))
    # contain never overflows, so pasting is enough. `smart` can overflow on
    # one axis and fall short on the other at the same time, so it crops what
    # sticks out and pads what does not -- both centred.
    if mode == "smart" and (nw > tw or nh > th):
        left, top = max(0, (nw - tw) // 2), max(0, (nh - th) // 2)
        img = img.crop((left, top, left + min(nw, tw), top + min(nh, th)))
        nw, nh = img.size
    canvas = Image.new("RGB", (tw, th), (0, 0, 0))
    canvas.paste(img, ((tw - nw) // 2, (th - nh) // 2))
    return canvas


class _AtSize:
    """A layer with its box resolved to pixels for this wall.

    A shim rather than a signature change on every `_solid`/`_text`/`_video`:
    those all read `layer.box`, and this hands them the resolved one while
    everything else passes straight through. Keeps the resolution in ONE place
    instead of threading a size through eight methods.
    """

    __slots__ = ("_layer", "box")

    def __init__(self, layer, box):
        self._layer = layer
        self.box = box

    def __getattr__(self, name):
        return getattr(self._layer, name)


@dataclass
class Renderer:
    show: Show
    resolver: Resolver = field(default_factory=Resolver)
    bindings: Bindings = field(default_factory=Bindings)
    _videos: dict[int, VideoDecoder] = field(default_factory=dict)
    # Compose-side instrumentation; see stats().
    frame_ms: float = 0.0
    frame_ms_max: float = 0.0
    frames_composed: int = 0
    layer_ms: dict = field(default_factory=dict)
    _images: dict[str, Image.Image] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def close(self):
        for v in self._videos.values():
            v.close()
        self._videos.clear()

    # -- scene selection ---------------------------------------------------

    def scene_at(self, t: float) -> tuple[Scene, float]:
        """Which scene is showing at show-time t, and how far into it."""
        total = self.show.total_duration
        if total <= 0:
            return self.show.scenes[0], 0.0
        t = t % total
        for sc in self.show.scenes:
            if t < sc.duration:
                return sc, t
            t -= sc.duration
        return self.show.scenes[-1], 0.0

    # -- rendering ---------------------------------------------------------

    def render(self, t: float, size: tuple[int, int] | None = None) -> Image.Image:
        """Render one frame, at `size` if given.

        `size` is the WALL's size. Passing it is what lets a show with
        fractional boxes be portable: the frame is produced at the size it will
        actually be shown at, so nothing is rendered small and stretched later.
        A show that declares `display.width/height` keeps using those when no
        size is passed, so every existing show is unaffected.
        """
        d = self.show.display
        W, H = size or (d.width or 256, d.height or 128)
        scene, st = self.scene_at(t)
        frame = Image.new("RGB", (W, H), (0, 0, 0))
        _t_frame = time.monotonic()
        for idx, layer in enumerate(sorted(scene.layers, key=lambda l: l.z)):
            layer = _AtSize(layer, layer.box.resolve(W, H))
            _t_layer = time.monotonic()
            try:
                tile = self._layer(layer, idx, st)
            except SourceError as e:
                # Tagged so a checker can tell a MISSING SOURCE -- no camera
                # registered, no wowza base, a blocked download -- from a code
                # fault. The first is this install's configuration and will be
                # true forever on a machine that has no cameras; the second is
                # a regression. Lumping them together makes any gate cry wolf.
                self._note(f"{scene.name}/{layer.type}: source: {e}")
                continue
            except Exception as e:  # a broken layer must not blank the sign
                self._note(f"{scene.name}/{layer.type}: {type(e).__name__}: {e}")
                continue
            # PER-LAYER TIMING. "The renderer is slow" was a dead end all
            # day: a 20 fps show running at 15.8 could be video decode, text
            # scrolling, an image resize or the composite, and those have
            # nothing in common as fixes. This attributes the cost.
            self.layer_ms[layer.type] = (
                0.8 * self.layer_ms.get(layer.type, 0.0)
                + 0.2 * (time.monotonic() - _t_layer) * 1000.0
            )
            if tile is None:
                continue
            # ALPHA-AWARE. A tile with an alpha channel contributes only the
            # pixels it actually drew; an RGB tile still overwrites its box, so
            # solid/gradient/image/video layers are unchanged.
            alpha = tile.mode == "RGBA"
            if layer.opacity >= 1.0:
                if alpha:
                    frame.paste(tile, (layer.box.x, layer.box.y), tile)
                else:
                    frame.paste(tile, (layer.box.x, layer.box.y))
            else:
                if alpha:
                    # Scale the tile's own alpha by the layer opacity, so a
                    # half-opacity caption fades its glyphs rather than fading
                    # a black rectangle over the picture.
                    a = tile.getchannel("A").point(
                        lambda v: int(v * layer.opacity))
                    frame.paste(tile.convert("RGB"), (layer.box.x, layer.box.y), a)
                else:
                    # Cropped HERE, not above: the alpha branch never reads it,
                    # so computing it unconditionally was ~5 us per such layer
                    # per frame thrown away.
                    base = frame.crop(
                        (layer.box.x, layer.box.y,
                         layer.box.x + layer.box.w, layer.box.y + layer.box.h)
                    )
                    frame.paste(Image.blend(base, tile, layer.opacity),
                                (layer.box.x, layer.box.y))
        ms = (time.monotonic() - _t_frame) * 1000.0
        self.frame_ms = 0.8 * self.frame_ms + 0.2 * ms
        if ms > self.frame_ms_max:
            self.frame_ms_max = ms
        self.frames_composed += 1
        return frame

    def stats(self) -> dict:
        """What the compose side is actually spending its time on.

        Exists because every diagnosis of "the renderer is slow" today was a
        guess. `frame_ms` against the show's frame period says whether compose
        can keep up at all; `layer_ms` says which layer type to look at; and
        the decoder counters separate a source that is starving us
        (holds high, wait_ms high) from a CPU that cannot keep up
        (frame_ms high, holds low). Those are different problems.
        """
        out = {
            "frames_composed": self.frames_composed,
            "frame_ms": round(self.frame_ms, 2),
            "frame_ms_max": round(self.frame_ms_max, 2),
            "layer_ms": {k: round(v, 2) for k, v in self.layer_ms.items()},
            "decoders": {},
        }
        for idx, dec in self._videos.items():
            d = getattr(dec, "dec", dec)      # unwrap ShufflePlayer
            if d is None:
                continue
            reads = getattr(d, "reads", 0)
            holds = getattr(d, "holds", 0)
            out["decoders"][idx] = {
                "reads": reads,
                "holds": holds,
                "hold_pct": round(100.0 * holds / max(reads + holds, 1), 2),
                "wait_ms": round(getattr(d, "wait_s", 0.0) * 1000.0, 1),
                "path": getattr(d, "path", "?").rsplit("/", 1)[-1][:40],
            }
        return out

    def _note(self, msg: str):
        if msg not in self.errors:
            self.errors.append(msg)
            del self.errors[:-20]

    def _layer(self, layer, idx: int, st: float) -> Image.Image | None:
        b = layer.box
        kind = layer.type
        if kind == "solid":
            return Image.new("RGB", (b.w, b.h), layer.color)
        if kind == "gradient":
            return self._gradient(layer)
        if kind == "image":
            return self._image(layer)
        if kind == "video":
            return self._video(layer, idx)
        if kind == "text":
            return self._text(layer, st)
        if kind == "graph":
            return self._graph(layer)
        if kind == "sparkline":
            return self._sparkline(layer)
        if kind == "rows":
            return self._rows(layer, st)
        return None

    def _rows(self, layer, st: float) -> Image.Image | None:
        """A table: one row per entry of a list in the live cache.

        Every cell is rendered by `_text` on a synthetic layer rather than by
        drawing here. That is deliberate: alignment, vertical centring and the
        whole continuous-scroll machinery -- integer pixels per frame, the
        double draw, the wrap on text+gap -- are subtle enough that a second
        copy would drift from the first. A cell that scrolls has to scroll
        identically to a text layer that scrolls, because on a departures board
        they sit side by side.

        The row count comes from the DATA, capped by what fits in the box, so
        the layer cannot draw outside itself however long the list grows and a
        short list leaves the rest of the box dark instead of filling it with
        no-value glyphs.
        """
        b = layer.box
        # Transparent for the same reason as `_text`: a rows layer that
        # blacked out its box could not sit on a gradient or an image.
        img = Image.new("RGBA", (b.w, b.h), (0, 0, 0, 0))

        rows = self.bindings.data_series(layer.address)
        if not rows:
            # A topic that has not arrived yet, or a path that is not a list.
            # An empty table is the honest rendering of "no data"; raising here
            # would take the sign down for a feed that is merely late.
            return img

        fits = b.h // layer.row_height if layer.row_height else 0
        count = min(len(rows), max(0, fits))
        if layer.limit is not None:
            count = min(count, layer.limit)

        # Column x positions accumulate, so changing one width shifts the rest
        # rather than needing every x recomputed by hand.
        xs, x = [], 0
        for col in layer.columns:
            w = _dim(col.w, b.w)
            xs.append((x, w))
            x += w + layer.col_gap

        for r in range(count):
            row = rows[r]
            y = r * layer.row_height
            for col, (cx, cw) in zip(layer.columns, xs):
                if cx >= b.w or cw <= 0:
                    continue                      # column starts past the edge
                cw = min(cw, b.w - cx)            # never draw outside the box
                value = _dig(row, col.field) if isinstance(row, dict) else row
                if value is None:
                    # The ROW is real and this field is not: that is the
                    # no-value case bindings already have a glyph for. Skipping
                    # it silently would hide a feed that changed shape.
                    text = _FAIL
                elif isinstance(value, bool):
                    text = "yes" if value else "no"
                elif isinstance(value, float):
                    text = f"{value:g}"
                else:
                    text = str(value)
                cell = _Cell(
                    box=Box(x=0, y=0, w=cw, h=layer.row_height),
                    text=text,
                    size=col.size if col.size is not None else layer.size,
                    color=col.color if col.color is not None else layer.color,
                    font=col.font if col.font is not None else layer.font,
                    align=col.align, valign=col.valign,
                    scroll=col.scroll, speed=col.speed,
                )
                # Mask by the cell's own alpha, or each cell would black out
                # the one beside it inside the rows tile.
                cell_img = self._text(cell, st)
                img.paste(cell_img, (cx, y), cell_img)
        return img

    def _graph(self, layer) -> Image.Image | None:
        return self._plot(layer, self.bindings.history(layer.entity, layer.hours))

    def _sparkline(self, layer) -> Image.Image | None:
        """Plot a series the live bus already holds -- no HTTP, no credentials.

        Anything that is not a finite number is dropped, not coerced. A feed
        that sends null for "no reading yet" would otherwise draw a cliff to
        the floor, which reads as a real measurement rather than a gap.
        """
        raw = self.bindings.data_series(layer.address)
        series = []
        for v in raw or []:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            f = float(v)
            if f == f and f not in (float("inf"), float("-inf")):
                series.append(f)
        return self._plot(layer, series)

    def _plot(self, layer, series) -> Image.Image | None:
        b = layer.box
        img = Image.new("RGBA", (b.w, b.h), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        col = Image.new("RGB", (1, 1), layer.color).getpixel((0, 0))

        if layer.baseline:
            dr.line([(0, b.h - 1), (b.w - 1, b.h - 1)], fill=tuple(c // 4 for c in col))

        if len(series) < 2:
            # Nothing to plot. The baseline alone reads as "no data yet" rather
            # than as a broken layer, and a missing integration must never blank
            # a sign.
            return img

        lo = layer.min if layer.min is not None else min(series)
        hi = layer.max if layer.max is not None else max(series)
        if hi <= lo:
            # A dead-flat signal has no range to scale into; draw it up the
            # middle rather than dividing by zero.
            lo, hi = lo - 0.5, hi + 0.5

        # Downsample by picking one sample per column. Averaging would smooth
        # away the spikes, which on a sensor trace are usually the point.
        top = 1 if layer.baseline else 0
        usable = max(1, b.h - 1 - top)
        pts = []
        for x in range(b.w):
            i = int(x * (len(series) - 1) / max(1, b.w - 1))
            f = (series[i] - lo) / (hi - lo)
            f = 0.0 if f < 0 else 1.0 if f > 1 else f
            y = int(round(top + (1.0 - f) * (usable - 1)))
            pts.append((x, y))

        if layer.fill:
            for x, y in pts:
                dr.line([(x, y), (x, b.h - 1)], fill=tuple(c // 3 for c in col))

        # 1px polyline, no anti-aliasing: at this pixel pitch a smoothed edge is
        # two dim LEDs where the signal wants one lit cleanly.
        dr.line(pts, fill=col, width=1)
        return img

    def _gradient(self, layer) -> Image.Image:
        b = layer.box
        img = Image.new("RGB", (b.w, b.h))
        dr = ImageDraw.Draw(img)
        c0 = Image.new("RGB", (1, 1), layer.start).getpixel((0, 0))
        c1 = Image.new("RGB", (1, 1), layer.end).getpixel((0, 0))
        n = b.h if layer.angle == "v" else b.w
        for i in range(max(1, n)):
            f = i / max(1, n - 1)
            col = tuple(int(c0[k] + (c1[k] - c0[k]) * f) for k in range(3))
            if layer.angle == "v":
                dr.line([(0, i), (b.w, i)], fill=col)
            else:
                dr.line([(i, 0), (i, b.h)], fill=col)
        return img

    def _image(self, layer) -> Image.Image:
        path = self.resolver.resolve(layer.src)
        img = self._images.get(path)
        if img is None:
            img = Image.open(path).convert("RGB")
            self._images[path] = img
        return _fit(img, layer.box, layer.fit)

    def _video(self, layer, idx: int) -> Image.Image:
        dec = self._videos.get(idx)
        if dec is None:
            if layer.src.startswith(("shuffle:", "random:")):
                # `loop` and `start` are meaningless here and are ignored: the
                # playlist never ends, and seeking into a film you did not
                # choose has no meaning.
                scheme, _, ref = layer.src.partition(":")
                dec = ShufflePlayer(self.resolver.pool(ref),
                                    layer.box.w, layer.box.h,
                                    self.show.display.fps,
                                    pick="random" if scheme == "random" else "bag",
                                    fit=layer.fit)
            else:
                path = self.resolver.resolve(layer.src)
                dec = VideoDecoder(path, layer.box.w, layer.box.h,
                                   self.show.display.fps, layer.loop, layer.start,
                                   fit=layer.fit)
            self._videos[idx] = dec
        return dec.next_frame()

    def _text(self, layer, st: float) -> Image.Image:
        b = layer.box
        text = self.bindings.resolve(layer.text)
        font = _load_font(layer.size, layer.font)
        # TRANSPARENT, not opaque black.
        #
        # Every layer used to return an RGB tile and the compositor pasted it
        # wholesale, so a text layer painted a black rectangle over whatever
        # was beneath it. On the rail board that erased the header gradient
        # everywhere except a 4px sliver above and left of the text boxes --
        # which on the glass reads as a coloured border along the top that
        # runs a little way down the side, and is what prompted this.
        #
        # It also meant no show could ever put text over a gradient, an image
        # or video and have the backdrop show through. Shows compensate today
        # by drawing an explicit `solid` under the text, which still works
        # unchanged -- that solid is simply now the thing you see rather than
        # the text layer's accidental black.
        img = Image.new("RGBA", (b.w, b.h), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        l, t, r, bt = dr.textbbox((0, 0), text, font=font)
        tw, th = r - l, bt - t
        y = {"top": 0, "middle": (b.h - th) // 2, "bottom": b.h - th}[layer.valign] - t
        scrolling = layer.scroll == "left" or (layer.scroll == "auto" and tw > b.w)
        if not scrolling:
            x = {"left": 0, "center": (b.w - tw) // 2, "right": b.w - tw}[layer.align] - l
            dr.text((x, y), text, font=font, fill=layer.color)
            return img

        # CONTINUOUS scroll: the string is drawn twice, separated by a gap, and
        # the offset wraps on (text + gap). The tail of one copy is followed
        # immediately by the head of the next, so the line never blanks and
        # never "restarts" -- which is both what a real marquee does and the
        # only way to judge smoothness, since a restart hides judder.
        gap = max(24, b.w // 4)
        span = tw + gap

        # INTEGER PIXELS PER FRAME.
        #
        # At 15fps a 40 px/s scroll is 2.67 px per frame; drawing at integer
        # positions turns that into a 3,3,2,3,2 stagger that reads as judder no
        # matter how precise the frame timing is. Snapping to a whole number of
        # pixels per frame makes every step identical. The speed changes by less
        # than half a pixel per frame, which is imperceptible; the evenness is
        # very perceptible.
        fps = max(1.0, float(self.show.display.fps))
        px_per_frame = max(1, round(layer.speed / fps))
        frame = int(round(st * fps))
        offset = (frame * px_per_frame) % span

        # DRAW ONLY WHAT CAN LAND IN THE BOX.
        #
        # This used to rasterise the WHOLE string, twice, every frame. A crawl
        # is far wider than its window by definition -- the live banner is
        # 1993 px of text in a 248 px box, 8x -- so almost all of that work was
        # thrown away at the clip. Measured 2026-10-21: 2.71 ms a frame for the
        # full string against 0.44 ms for the visible span, 6.2x.
        #
        # That cost is not just CPU. The render thread and the send thread are
        # both Python, so every millisecond the renderer spends holding the GIL
        # is a millisecond the pacing loop cannot run -- and a stalled sender
        # lets the kernel drain its 1 MB socket buffer at line rate, which
        # overflows the card's FIFO and costs whole chunks. The two shows
        # running today show exactly that split: the rail board composes in
        # 2.2 ms and drops nothing, the movie banner composes in 8-10 ms and
        # drops 12.96 chunks/s. Shortening the hold is the fix.
        strip = _scroll_strip(text, font, layer.color, b.h, gap, y,
                              layer.size, layer.font)
        strip.window(img, offset)
        return img


def frame_to_rgb(img: Image.Image) -> bytes:
    return img.convert("RGB").tobytes()


def frame_to_indexed(img: Image.Image) -> tuple[bytes, bytes]:
    """Quantise a frame to 256 colours: (indices, palette).

    Returns one byte per pixel plus a 768-byte RGB palette, which is what the
    panel's 'B','I' and 'B','P' packets carry.

    MEDIANCUT with dither=NONE is deliberate on both counts:

    - Median cut picks a palette from the image's actual colour distribution,
      so flat graphics and text -- which is what this format is for -- come back
      essentially lossless. WEB or ADAPTIVE with a fixed palette would band.
    - Dithering is OFF because these are LED panels at low resolution. Dither
      noise that disappears on a 200 dpi screen is individually visible lit
      pixels here, and on moving content it crawls. The panel repo's HUB75
      notes make the same point about anti-aliasing small glyphs: at this pixel
      pitch, structure beats smoothness.

    The palette is padded to a full 256 entries so a short palette never leaves
    stale colours from a previous scene in the high indices.
    """
    rgb = img.convert("RGB")
    pal_img = rgb.quantize(colors=256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    indices = pal_img.tobytes()
    palette = bytes(pal_img.getpalette() or b"")[: 256 * 3]
    if len(palette) < 256 * 3:
        palette = palette + bytes(256 * 3 - len(palette))
    return indices, palette
