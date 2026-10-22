#!/usr/bin/env python3
"""Stream video to the P1.25 S-PWM module on the E320, with the quadrant remap.

The module is four 128x64 quadrants, one per RGB group, arranged 2x2:

    g2 -> top-left      g0 -> top-right
    g3 -> bottom-left   g1 -> bottom-right

and the gateware stacks those groups VERTICALLY in the logical framebuffer,
so a normal 256x128 picture has to be cut up and placed by group. This mirrors
`quad_src_xy` in sw_rust/barsign_disp/src/patterns.rs -- the two MUST agree,
including SERIALISER_ROT, or the stream and the on-card test patterns will
disagree about where a pixel goes.

Protocol facts that shape this (docs/E320-BRINGUP.md 6):

  * `_send_chunked` refuses more than 255 chunks -- `chunk_index` is a u8.
    RGB888 at the OLD 512x256 geometry was 270 chunks and unsendable. At
    256x256 it is 135, which is why getting `width_words` right is what made
    streaming possible at all.
  * Indexed mode is NOT supported on this S-PWM driver. A frame of one
    repeated palette index renders as noise.

Usage:
    stream_video.py                        # moving test pattern
    stream_video.py --source clip.mp4 --loop
    stream_video.py --source rtsp://camera/stream
"""
import argparse
import subprocess
import sys
import time

sys.path.insert(0, "/srv/docker/marquee/packages/marquee_core")
from marquee_core.stream import CardStream  # noqa: E402

SRC_W, SRC_H = 256, 128          # ONE panel's picture; scaled by --panels-wide
QUAD_W, QUAD_H = 128, 64         # one RGB group's quadrant

# Must match SERIALISER_ROT in patterns.rs. Compensation for the output
# stage's pipeline latency; a property of the BUILD, re-measure if the
# gateware changes. See docs/E320-BRINGUP.md 4.7.
SERIALISER_ROT = (17, 17, 18, 18)


def remap_plan(vw: int, vh: int, outputs: int, src_w: int):
    """Precompute (dst_off, src_off, nbytes) row copies for the whole wall.

    One HUB320 connector drives one 256x128 panel, and the panels sit side by
    side, so output `o` shows source columns o*256 .. o*256+255. Within a
    panel the four RGB groups are a 2x2 of 128x64 quadrants:

        g2 -> top-left      g0 -> top-right
        g3 -> bottom-left   g1 -> bottom-right

    and the gateware stacks those groups VERTICALLY in the logical
    framebuffer, so each row of each quadrant is a contiguous run of the
    source -- a slice copy, which is why this needs no numpy.

    Must agree with `quad_src_xy` in patterns.rs, SERIALISER_ROT included, or
    the stream and the on-card test patterns disagree about where a pixel is.
    """
    plan = []
    for o in range(outputs):
        col_base = o * QUAD_W          # this output's logical columns
        img_base = o * (2 * QUAD_W)    # this output's slice of the wall
        for row in range(vh):
            g, y = divmod(row, QUAD_H)
            if g >= 4:
                continue
            rot = SERIALISER_ROT[g]
            img_y = y + (QUAD_H if g % 2 else 0)
            base = img_base + (QUAD_W if g < 2 else 0)
            n = QUAD_W - rot
            plan.append((((row * vw) + col_base) * 3,
                         ((img_y * src_w) + base + rot) * 3, n * 3))
            if rot:
                plan.append((((row * vw) + col_base + n) * 3,
                             ((img_y * src_w) + base) * 3, rot * 3))
    return plan


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="192.168.1.62")
    ap.add_argument("--source", default=None, help="file or URL; omit for a test pattern")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--packet-delay", type=float, default=0.0001)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--realtime", action="store_true", help="pace a file at its own rate")
    ap.add_argument("--frames", type=int, default=0, help="stop after N frames")
    ap.add_argument("--outputs", type=int, default=2,
                    help="HUB320 connectors in use; one panel each (1, 2 or 4)")
    ap.add_argument("--panels-wide", type=int, default=0,
                    help="source image width in panels (default: same as --outputs)")
    args = ap.parse_args()

    # Geometry follows the card, not a constant. One connector per panel:
    #   outputs=1 -> 128x256   outputs=2 -> 256x256   outputs=4 -> 512x256
    # width_words = columns * outputs, and the virtual width MUST equal it.
    outputs = args.outputs
    vw, vh = QUAD_W * outputs, 256
    src_w = SRC_W * (args.panels_wide or outputs)
    plan = remap_plan(vw, vh, outputs, src_w)
    chunks = -(-vw * vh * 3 // 1461)
    print(f"wall {src_w}x{SRC_H} ({outputs} panel(s)) -> fb {vw}x{vh} RGB = "
          f"{vw*vh*3} bytes = {chunks} chunks", flush=True)
    if chunks > 255:
        print(f"  {chunks} chunks > 255: using the WIDE 'B','W' header", flush=True)

    if args.source:
        src = (["-stream_loop", "-1"] if args.loop else []) \
            + (["-re"] if args.realtime else []) + ["-i", args.source]
    else:
        src = ["-f", "lavfi", "-i", f"testsrc2=size={src_w}x{SRC_H}:rate={args.fps}"]

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", *src,
           "-vf", f"scale={src_w}:{SRC_H}:force_original_aspect_ratio=decrease,"
                  f"pad={src_w}:{SRC_H}:(ow-iw)/2:(oh-ih)/2,fps={args.fps}",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)

    frame_bytes = src_w * SRC_H * 3
    stream = CardStream(args.host, packet_delay=args.packet_delay)
    n, t0 = 0, time.perf_counter()
    try:
        while True:
            raw = proc.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            dest = bytearray(vw * vh * 3)
            for dst, src_off, ln in plan:
                dest[dst:dst + ln] = raw[src_off:src_off + ln]
            stream.send_frame(bytes(dest), vw, vh)
            n += 1
            if n % 30 == 0:
                el = time.perf_counter() - t0
                print(f"{n} frames  {n/el:5.1f} fps  "
                      f"{stream.packets_sent/el:6.0f} pkt/s", flush=True)
            if args.frames and n >= args.frames:
                break
    except KeyboardInterrupt:
        pass
    finally:
        proc.kill()
        stream.close()
    el = time.perf_counter() - t0
    print(f"done: {n} frames in {el:.1f}s = {n/max(el,1e-9):.1f} fps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
