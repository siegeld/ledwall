"""plug_patterns.py <host> <log> <hold_s> <pattern>...   hold each pattern, log UTC start times.

Blind measurement: no camera, a mains plug that reports watts (docs/PANELS.md,
"A power plug as the light meter"). Needs marquee_core on PYTHONPATH.

A plug that reports watts is the light meter: every pattern is held long
enough for it to report, and the log lines are matched to its history.
Patterns (framebuffer 128x256, four 128x64 groups stacked):
  black white r g b yellow cyan magenta  grey:N  rgb:R,G,B
  top bottom left right  group:N
  rowcomb:K[:PH]  every Kth row white   colcomb:K[:PH]  every Kth column
  rowcombc:K:R,G,B  comb in a colour    checker
  dots:K  one white pixel per KxK cell
"""
import sys, time, datetime
from marquee_core.stream import CardStream
host, log, hold = sys.argv[1], sys.argv[2], float(sys.argv[3])
W, H = 128, 256
NAMED = {"black": (0, 0, 0), "white": (255, 255, 255), "r": (255, 0, 0),
         "g": (0, 255, 0), "b": (0, 0, 255), "yellow": (255, 255, 0),
         "cyan": (0, 255, 255), "magenta": (255, 0, 255)}

def frame(p):
    name, *a = p.split(":")
    on = lambda x, y: True
    c = (255, 255, 255)
    if name in NAMED: c = NAMED[name]
    elif name == "grey": c = (int(a[0]),) * 3
    elif name == "rgb": c = tuple(int(v) for v in a[0].split(","))
    elif name == "top": on = lambda x, y: y < H // 2
    elif name == "bottom": on = lambda x, y: y >= H // 2
    elif name == "left": on = lambda x, y: x < W // 2
    elif name == "right": on = lambda x, y: x >= W // 2
    elif name == "group": g = int(a[0]); on = lambda x, y: y // 64 == g
    elif name in ("rowcomb", "rowcombc"):
        k = int(a[0])
        if name == "rowcombc": c = tuple(int(v) for v in a[1].split(",")); ph = 0
        else: ph = int(a[1]) if len(a) > 1 else 0
        on = lambda x, y: y % k == ph
    elif name in ("colcomb", "colcombc"):
        k = int(a[0])
        if name == "colcombc": c = tuple(int(v) for v in a[1].split(",")); ph = 0
        else: ph = int(a[1]) if len(a) > 1 else 0
        on = lambda x, y: x % k == ph
    elif name == "checker": on = lambda x, y: (x + y) % 2 == 0
    elif name == "dots": k = int(a[0]); on = lambda x, y: x % k == 0 and y % k == 0
    else: raise SystemExit(f"unknown pattern {p}")
    buf = bytearray(W * H * 3)
    n = 0
    for y in range(H):
        for x in range(W):
            if on(x, y):
                i = (y * W + x) * 3; buf[i:i + 3] = bytes(c); n += 1
    return bytes(buf), n

with CardStream(host, 7000, packet_delay=0.0006) as s, open(log, "a") as f:
    for p in sys.argv[4:]:
        buf, n = frame(p)
        for _ in range(3):
            s.send_frame(buf, W, H); time.sleep(0.15)
        t0 = datetime.datetime.now(datetime.timezone.utc)
        f.write(f"{t0.isoformat()} {p} {n}\n"); f.flush()
        end = time.time() + hold
        while time.time() < end:
            time.sleep(2.0); s.send_frame(buf, W, H)
    t0 = datetime.datetime.now(datetime.timezone.utc)
    f.write(f"{t0.isoformat()} END 0\n")
