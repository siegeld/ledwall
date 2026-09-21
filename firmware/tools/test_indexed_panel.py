#!/usr/bin/env python3
"""Validate the indexed wire format against ONE live panel.

This is the measurement the whole indexed change rests on. Everything else about
it has been verified in software -- chunk arithmetic, packet shapes, timing
closure -- but the CLAIM is a frame-rate gain, and that can only be established
here.

What it does, in order:

  1. Confirms the panel is alive and reports its firmware version.
  2. Streams RGB888 for a fixed window, reading the panel's own counters.
  3. Streams indexed (palette + frames) for the same window.
  4. Compares chunks per frame and frames per second, and prints a verdict.

It deliberately measures the PANEL's counters rather than the sender's: the
sender knows what it put on the wire, not what arrived or was drawn. `fps` here
is derived from frames the panel actually COMPLETED.

Usage:
    python3 tools/test_indexed_panel.py --host 192.168.1.50 --width 256 --height 192

Nothing here needs marquee. Run it from any host that can reach the panel.
"""
from __future__ import annotations

import argparse
import json
import socket
import struct
import sys
import time
import urllib.error
import urllib.request

HEADER = struct.Struct("<2sHBBHH")
PIXELS_PER_CHUNK = 487
PIXELS_PER_CHUNK_INDEXED = PIXELS_PER_CHUNK * 3  # 1461 -- see the firmware
UDP_PORT = 7000


def status(host: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(f"http://{host}/api/status", timeout=timeout) as r:
        return json.loads(r.read().decode())


def bitmap_stats(host: str, timeout: float = 5.0) -> dict:
    """The bitmap receiver's own counters.

    These live on /api/bitmap/stats, NOT /api/status -- see `api_bitmap_stats`
    in sw_rust/barsign_disp/src/network.rs. /api/status carries the dma_* and
    link counters; every per-frame figure this test reads (frames_completed,
    palette_writes, bad_magic, chunks_repaired) is only here.
    """
    with urllib.request.urlopen(f"http://{host}/api/bitmap/stats", timeout=timeout) as r:
        return json.loads(r.read().decode())


def make_pattern(w: int, h: int) -> tuple[bytes, bytes, bytes]:
    """A pattern that is honest about BOTH formats.

    Sixteen vertical colour bands. Few enough colours that quantisation is
    lossless, so the indexed and RGB renderings should be INDISTINGUISHABLE on
    the glass -- which is the point. If indexed looks different, the fault is
    real and not an artefact of the test content.

    Returns (rgb_bytes, index_bytes, palette_bytes).
    """
    bands = [
        (0, 0, 0), (255, 255, 255), (255, 0, 0), (0, 255, 0),
        (0, 0, 255), (255, 255, 0), (0, 255, 255), (255, 0, 255),
        (128, 0, 0), (0, 128, 0), (0, 0, 128), (128, 128, 0),
        (0, 128, 128), (128, 0, 128), (192, 192, 192), (64, 64, 64),
    ]
    bw = max(1, w // len(bands))
    rgb = bytearray(w * h * 3)
    idx = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            b = min(x // bw, len(bands) - 1)
            r, g, bl = bands[b]
            o = (y * w + x) * 3
            rgb[o : o + 3] = bytes((r, g, bl))
            idx[y * w + x] = b
    palette = bytearray(256 * 3)
    for i, (r, g, bl) in enumerate(bands):
        palette[i * 3 : i * 3 + 3] = bytes((r, g, bl))
    return bytes(rgb), bytes(idx), bytes(palette)


class Sender:
    def __init__(self, host: str, delay: float):
        self.host, self.delay = host, delay
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        self.fid = 0
        self.packets = 0

    def _chunked(self, magic: bytes, body: bytes, per_chunk: int, w: int, h: int) -> int:
        self.fid = (self.fid + 1) & 0xFFFF
        total = (len(body) + per_chunk - 1) // per_chunk
        deadline = time.perf_counter()
        for i in range(total):
            self.sock.sendto(
                HEADER.pack(magic, self.fid, i, total, w, h)
                + body[i * per_chunk : (i + 1) * per_chunk],
                (self.host, UDP_PORT),
            )
            self.packets += 1
            if self.delay > 0:
                deadline += self.delay
                rem = deadline - time.perf_counter()
                if rem > 0.002:
                    time.sleep(rem - 0.001)
                while time.perf_counter() < deadline:
                    pass
        return total

    def rgb(self, body: bytes, w: int, h: int) -> int:
        return self._chunked(b"BM", body, PIXELS_PER_CHUNK * 3, w, h)

    def indexed(self, body: bytes, w: int, h: int) -> int:
        return self._chunked(b"BI", body, PIXELS_PER_CHUNK_INDEXED, w, h)

    def palette(self, pal: bytes) -> None:
        self.sock.sendto(
            HEADER.pack(b"BP", self.fid, 0, 1, len(pal) // 3, 1) + pal,
            (self.host, UDP_PORT),
        )
        self.packets += 1


def run_window(send, host: str, seconds: float, label: str) -> dict:
    """Stream for `seconds`, then report what the PANEL saw."""
    before = bitmap_stats(host)
    t0 = time.time()
    sent = 0
    chunks = 0
    while time.time() - t0 < seconds:
        chunks = send()
        sent += 1
    elapsed = time.time() - t0
    time.sleep(0.4)  # let the last frame land and the counters settle
    after = bitmap_stats(host)

    def d(k):
        return after.get(k, 0) - before.get(k, 0)

    completed = d("frames_completed")
    return {
        "label": label,
        "elapsed": elapsed,
        "frames_sent": sent,
        "chunks_per_frame": chunks,
        "frames_completed": completed,
        "fps": completed / elapsed if elapsed else 0.0,
        "frames_partial": d("frames_partial"),
        "frames_dropped": d("frames_dropped"),
        "chunks_repaired": d("chunks_repaired"),
        "bad_magic": d("bad_magic"),
        "palette_writes": d("palette_writes"),
        "mac_overflow": d("mac_overflow"),
    }


def show(r: dict) -> None:
    print(f"\n  {r['label']}")
    print(f"    chunks/frame      {r['chunks_per_frame']}")
    print(f"    frames sent       {r['frames_sent']} in {r['elapsed']:.1f}s")
    print(f"    frames COMPLETED  {r['frames_completed']}  ->  {r['fps']:.1f} fps")
    print(f"    partial/dropped   {r['frames_partial']} / {r['frames_dropped']}")
    print(f"    chunks repaired   {r['chunks_repaired']}")
    print(f"    mac_overflow      {r['mac_overflow']}")
    print(f"    bad_magic         {r['bad_magic']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", required=True, help="panel address")
    ap.add_argument("--width", type=int, default=256)
    ap.add_argument("--height", type=int, default=192)
    ap.add_argument("--seconds", type=float, default=10.0, help="per format")
    ap.add_argument("--delay", type=float, default=0.0008,
                    help="inter-packet spacing; the measured practical limit")
    a = ap.parse_args()

    try:
        st = status(a.host)
    except (urllib.error.URLError, OSError) as e:
        print(f"FAIL: cannot reach {a.host}: {e}")
        print("      The panel must be powered and on the network.")
        return 2

    print(f"panel {a.host} is up")
    for k in ("version", "uptime_s", "dma_enabled", "image_width", "image_height"):
        if k in st:
            print(f"  {k:16} {st[k]}")
    try:
        bs = bitmap_stats(a.host)
    except (urllib.error.URLError, OSError) as e:
        print(f"FAIL: cannot read /api/bitmap/stats on {a.host}: {e}")
        return 2
    if "palette_writes" not in bs:
        print("\nFAIL: this firmware predates the indexed format.")
        print("      /api/bitmap/stats has no palette_writes -- need colorlight >= 1.11.0.")
        return 2

    rgb, idx, pal = make_pattern(a.width, a.height)
    s = Sender(a.host, a.delay)

    print(f"\nstreaming {a.width}x{a.height} colour bands, {a.seconds:.0f}s per format")
    print("WATCH THE PANEL: both passes must look IDENTICAL.")
    print("The pattern uses 16 flat colours, so quantisation is lossless -- any")
    print("visible difference between the two passes is a real fault.")

    r_rgb = run_window(lambda: s.rgb(rgb, a.width, a.height), a.host, a.seconds, "RGB888  'B','M'")
    show(r_rgb)

    # The palette is sent BEFORE the indexed window opens -- it has to be, or
    # the first frames render against a stale palette. So its counter delta has
    # to be taken from a baseline captured before the send: run_window's own
    # "before" snapshot is taken after it, and would always report zero.
    pal_before = bitmap_stats(a.host)["palette_writes"]
    s.palette(pal)
    time.sleep(0.2)
    r_idx = run_window(lambda: s.indexed(idx, a.width, a.height), a.host, a.seconds,
                       "INDEXED 'B','I'")
    r_idx["palette_writes"] = bitmap_stats(a.host)["palette_writes"] - pal_before
    show(r_idx)

    # --- verdict ----------------------------------------------------------
    print("\n" + "=" * 58)
    ok = True

    cr = r_rgb["chunks_per_frame"] / max(1, r_idx["chunks_per_frame"])
    print(f"  chunks/frame  {r_rgb['chunks_per_frame']} -> {r_idx['chunks_per_frame']}  ({cr:.2f}x fewer)")
    if not 2.8 <= cr <= 3.2:
        print("    FAIL: expected ~3x. Chunk stride disagrees between sender and panel.")
        ok = False

    fr = r_idx["fps"] / max(0.01, r_rgb["fps"])
    print(f"  fps           {r_rgb['fps']:.1f} -> {r_idx['fps']:.1f}  ({fr:.2f}x)")
    if fr < 1.5:
        print("    FAIL: fewer packets did NOT buy frame rate.")
        print("    This is the interesting failure: it means per-interrupt cost is")
        print("    not the bound after all, and TODO item 9's model needs revising.")
        print("    Look at mac_overflow and chunks_repaired above before theorising.")
        ok = False
    elif fr < 2.5:
        print("    PARTIAL: real gain, below the predicted ~3x. Worth understanding.")

    if r_idx["palette_writes"] < 1:
        print(f"  FAIL: palette_writes did not increase -- the 'B','P' packet never landed.")
        ok = False
    else:
        print(f"  palette       {r_idx['palette_writes']} write(s) accepted")

    if r_idx["bad_magic"] > 0:
        print(f"  FAIL: bad_magic +{r_idx['bad_magic']} during the indexed pass.")
        print("    Either the firmware does not know 'B','I'/'B','P', or the DMA is")
        print("    counting palette packets it should be passing to the CPU.")
        ok = False

    print("=" * 58)
    print("  PASS" if ok else "  FAIL")
    print("\n  Counters are only half the test. If the two passes did not look")
    print("  identical on the glass, that is a failure whatever the numbers say.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
