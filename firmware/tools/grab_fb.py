#!/usr/bin/env python3
"""Capture what is actually on a panel's glass, as a PNG.

    tools/grab_fb.py 192.168.1.50 -o panel.png
    tools/grab_fb.py 192.168.1.50 --scale 4 --wait

Reads the framebuffer back over `/api/fbdump` and recolours it through the
panel's live `/api/palette`. That is the only way to see what a program is
really drawing without standing in front of the panel -- and, unlike a
screenshot of what you *sent*, it catches the difference between the two.

## It refuses on a fullcolor panel, and that is the point

`/api/fbdump` emits only the LOW BYTE of each framebuffer word. What that byte
means depends on the output mode:

    indexed     the byte IS the palette index   -> reconstructable
    fullcolor   the byte is one RGB channel     -> the other two are not sent

Running a colour channel through a palette produces confetti that looks exactly
like a corrupt framebuffer. It is convincing: the LAYOUT is still correct,
because a channel still varies with the content. This tool refuses rather than
hand back a plausible lie -- a wrong screenshot is worse than no screenshot,
because you act on it.

A program-mode panel is indexed and captures fine. A streamed `format: rgb`
show is fullcolor and cannot be captured in colour at all; preview it host-side
instead.

## Two details that will bite a reimplementation

* **`y0` is parsed as a STRING.** `{"y0": 16}` reads as absent and returns rows
  0-15 again -- the symptom is an image that repeats the top of the screen all
  the way down.
* **The palette hex is little-endian.** `/api/palette` reports brass `#C9A84C`
  as `4ca8c9`, so each triplet is reversed before use.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

from PIL import Image

ROWS_PER_CHUNK = 16          # what one /api/fbdump call returns


def _get(host: str, path: str, body: dict | None = None, timeout: float = 25.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://{host}{path}", data=data,
                                 method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as fh:
        return json.load(fh)


def palette(host: str, timeout: float = 25.0) -> list[tuple[int, int, int]]:
    """The panel's live palette as RGB triples.

    Entries arrive as one little-endian hex triplet per index, so each is
    reversed: `4ca8c9` is `#C9A84C`.
    """
    raw = _get(host, "/api/palette", timeout=timeout)["entries"]
    if isinstance(raw, list):
        raw = "".join(raw)
    out = []
    for i in range(256):
        t = raw[i * 6:i * 6 + 6]
        b, g, r = (int(t[k * 2:k * 2 + 2], 16) for k in range(3))
        out.append((r, g, b))
    return out


def capture(host: str, timeout: float = 25.0, retries: int = 3) -> Image.Image:
    pal = palette(host, timeout)
    img = None
    for y0 in range(0, 4096, ROWS_PER_CHUNK):
        for attempt in range(retries):
            try:
                # y0 as a STRING -- an int reads as absent and re-returns rows 0-15.
                d = _get(host, "/api/fbdump", {"y0": str(y0)}, timeout)
                break
            except (urllib.error.URLError, OSError, ValueError):
                if attempt == retries - 1:
                    raise
                time.sleep(2)
        if d.get("mode") != "indexed":
            raise SystemExit(
                f"panel is in {d.get('mode')!r} mode: /api/fbdump sends only the low\n"
                "byte of each word, which is one RGB channel there, not a palette\n"
                "index. A colour capture needs an indexed panel -- a program-mode\n"
                "one, or a show with `format: indexed`."
            )
        w, h = d["w"], d["h"]
        if img is None:
            img = Image.new("RGB", (w, h), (0, 0, 0))
            px = img.load()
        if y0 >= h:
            break
        row_hex = d["data"]
        for r in range(d["rows"]):
            line = row_hex[(r * w) * 2:(r * w) * 2 + w * 2]
            y = y0 + r
            if y >= h:
                break
            for x in range(w):
                px[x, y] = pal[int(line[x * 2:x * 2 + 2], 16)]
        if y0 + ROWS_PER_CHUNK >= h:
            break
    if img is None:
        raise SystemExit("no framebuffer returned")
    return img


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("host", help="panel address")
    ap.add_argument("-o", "--out", default="panel.png")
    ap.add_argument("--scale", type=int, default=1,
                    help="nearest-neighbour upscale, so the pixel grid stays visible")
    ap.add_argument("--wait", action="store_true",
                    help="retry while the panel is rebooting")
    ap.add_argument("--timeout", type=float, default=25.0)
    a = ap.parse_args()

    deadline = time.time() + (120 if a.wait else 0)
    while True:
        try:
            img = capture(a.host, a.timeout)
            break
        except SystemExit:
            raise                      # a mode refusal is not worth retrying
        except Exception as e:
            if time.time() >= deadline:
                print(f"capture failed: {type(e).__name__}: {e}", file=sys.stderr)
                return 1
            time.sleep(3)

    if a.scale > 1:
        # Nearest neighbour: the point is to see the actual pixels.
        img = img.resize((img.width * a.scale, img.height * a.scale), Image.NEAREST)
    img.save(a.out)
    print(f"wrote {a.out} ({img.width}x{img.height})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
