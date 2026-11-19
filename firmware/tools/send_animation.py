#!/usr/bin/env python3
"""Send animated test patterns to the LED panel via UDP."""
import argparse
import math
import time

from send_test_pattern import send_rgb_data


def pulsing_heart(width, height, num_frames=60, fps=30):
    """Generate frames of a heart that pulses between small and large."""
    frames = []
    for f in range(num_frames):
        # Scale oscillates between 0.7 and 1.1
        t = f / num_frames
        scale = 0.9 + 0.2 * math.sin(2 * math.pi * t)
        data = bytearray()
        for y in range(height):
            ny = (1.9 - 3.1 * y / (height - 1)) / scale
            for x in range(width):
                nx = (2.6 * x / (width - 1) - 1.3) / scale
                v = (nx * nx + ny * ny - 1.0) ** 3 - nx * nx * ny * ny * ny
                if v <= 0:
                    data.extend([255, 0, 0])
                else:
                    data.extend([0, 0, 0])
        frames.append(bytes(data))
    return frames


ANIMATIONS = {
    "heart": pulsing_heart,
}


def parse_layout_dims(layout_str, panel_size_str="128x64"):
    """Compute virtual display dimensions from layout spec and panel size."""
    pw, ph = (int(x) for x in panel_size_str.split("x"))
    cols, rows = (int(x) for x in layout_str.split("x"))
    return pw * cols, ph * rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Send an animated pattern to the LED panel"
    )
    parser.add_argument(
        "animation",
        choices=list(ANIMATIONS.keys()),
        help="Animation to send",
    )
    parser.add_argument("--host", default="192.168.1.50")
    parser.add_argument("--port", type=int, default=7000)
    parser.add_argument("--width", type=int, default=128)
    parser.add_argument("--height", type=int, default=64)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--loops", type=int, default=3, help="Number of loops (0=infinite)")
    parser.add_argument(
        "--layout",
        help="Grid layout (e.g. 2x1) — overrides --width/--height",
    )
    parser.add_argument(
        "--panel-size",
        default="128x64",
        help="Physical panel size (default: 128x64)",
    )
    args = parser.parse_args()
    if args.layout:
        args.width, args.height = parse_layout_dims(args.layout, args.panel_size)

    print(f"Generating '{args.animation}' frames...")
    frames = ANIMATIONS[args.animation](args.width, args.height, fps=args.fps)
    print(f"Generated {len(frames)} frames, sending at {args.fps} fps")

    frame_period = 1.0 / args.fps
    frame_id = int(time.time()) & 0xFFFF
    loop = 0
    try:
        while args.loops == 0 or loop < args.loops:
            loop += 1
            for i, frame in enumerate(frames):
                t0 = time.monotonic()
                send_rgb_data(args.host, args.port, frame, args.width, args.height, frame_id=frame_id)
                frame_id = (frame_id + 1) & 0xFFFF
                elapsed = time.monotonic() - t0
                sleep = frame_period - elapsed
                if sleep > 0:
                    time.sleep(sleep)
            print(f"Loop {loop} done")
    except KeyboardInterrupt:
        print("\nStopped")
