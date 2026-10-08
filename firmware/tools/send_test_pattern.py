#!/usr/bin/env python3
"""Generate and send test patterns to the LED panel via UDP (no image file needed)."""
import argparse
import colorsys
import socket
import struct
import time

HEADER_FMT = "<2sHBBHH"  # magic, frame_id, chunk_idx, total_chunks, width, height
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 10 bytes
MAX_PAYLOAD = 1462
PIXELS_PER_CHUNK = MAX_PAYLOAD // 3  # 487
BYTES_PER_CHUNK = PIXELS_PER_CHUNK * 3  # 1461 — pixel-aligned

# Measured on a 256x128 display: clean to ~1250 packets/second, collapsing
# above it. 0.8 ms holds just under that; the old 0.01/0.1 defaults were 12x
# to 125x more conservative than the hardware actually needs.
DEFAULT_DELAY = 0.0008


def gradient(width, height):
    """Red-green gradient: red increases left-to-right, green increases top-to-bottom."""
    data = bytearray()
    for y in range(height):
        for x in range(width):
            r = x * 255 // max(width - 1, 1)
            g = y * 255 // max(height - 1, 1)
            b = 0
            data.extend([r, g, b])
    return bytes(data)


def color_bars(width, height):
    """Vertical color bars: white, yellow, cyan, green, magenta, red, blue, black."""
    colors = [
        (255, 255, 255),
        (255, 255, 0),
        (0, 255, 255),
        (0, 255, 0),
        (255, 0, 255),
        (255, 0, 0),
        (0, 0, 255),
        (0, 0, 0),
    ]
    data = bytearray()
    for y in range(height):
        for x in range(width):
            idx = x * len(colors) // width
            r, g, b = colors[idx]
            data.extend([r, g, b])
    return bytes(data)


def rainbow(width, height):
    """Rainbow: hue varies diagonally."""
    data = bytearray()
    for y in range(height):
        for x in range(width):
            hue = ((x + y) / (width + height)) % 1.0
            r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 1.0)
            data.extend([int(r * 255), int(g * 255), int(b * 255)])
    return bytes(data)


def heart(width, height):
    """Filled red heart on black background."""
    data = bytearray()
    for y in range(height):
        # Map to math coords: x in [-1.3, 1.3], y in [-1.2, 1.9] (top=1.9)
        ny = 1.9 - 3.1 * y / (height - 1)
        for x in range(width):
            nx = 2.6 * x / (width - 1) - 1.3
            # Implicit heart: (x^2 + y^2 - 1)^3 - x^2 * y^3 <= 0
            v = (nx * nx + ny * ny - 1.0) ** 3 - nx * nx * ny * ny * ny
            if v <= 0:
                data.extend([255, 0, 0])
            else:
                data.extend([0, 0, 0])
    return bytes(data)


PATTERNS = {
    "gradient": gradient,
    "bars": color_bars,
    "rainbow": rainbow,
    "heart": heart,
}


def send_rgb_data(host, port, rgb_data, width, height, frame_id=None, delay=DEFAULT_DELAY,
                  sock=None):
    """Send one frame, pacing packets against a monotonic deadline.

    Pacing matters: the panel is fine up to roughly 1250 packets/second and
    falls off a cliff above it, so the send rate has to be held accurately.
    A bare time.sleep(delay) per packet drifts badly -- the OS rounds every
    sleep up, so a requested 0.8 ms actually delivers about 0.95 ms and the
    error compounds across a frame. Sleeping until an absolute deadline
    instead keeps the average on target, and busy-waits the last stretch
    where sleep() is too coarse to be useful.
    """
    if frame_id is None:
        frame_id = int(time.time()) & 0xFFFF
    total_chunks = (len(rgb_data) + BYTES_PER_CHUNK - 1) // BYTES_PER_CHUNK
    own_sock = sock is None
    if own_sock:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    deadline = time.perf_counter()
    for i in range(total_chunks):
        offset = i * BYTES_PER_CHUNK
        chunk = rgb_data[offset : offset + BYTES_PER_CHUNK]
        header = struct.pack(
            HEADER_FMT, b"BM", frame_id, i, total_chunks, width, height
        )
        sock.sendto(header + chunk, (host, port))
        if delay > 0:
            deadline += delay
            remaining = deadline - time.perf_counter()
            if remaining > 0.002:
                time.sleep(remaining - 0.001)
            while time.perf_counter() < deadline:
                pass
    if own_sock:
        sock.close()
    return total_chunks


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Send a generated test pattern to the LED panel"
    )
    parser.add_argument(
        "pattern",
        nargs="?",
        choices=list(PATTERNS.keys()),
        help="Pattern to generate",
    )
    parser.add_argument("--host", default="192.168.1.50")
    parser.add_argument("--port", type=int, default=7000)
    parser.add_argument("--width", type=int, default=128)
    parser.add_argument("--height", type=int, default=64)
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY,
                        help=f"Seconds between packets (default {DEFAULT_DELAY}; "
                             "the panel is clean to ~1250 pkt/s)")
    parser.add_argument("--smoke", action="store_true", help="Cycle through all patterns")
    parser.add_argument("--smoke-delay", type=float, default=1.0, help="Delay between patterns in smoke mode (seconds)")
    parser.add_argument("--smoke-loops", type=int, default=0, help="Number of loops in smoke mode (0=forever)")
    args = parser.parse_args()

    if args.smoke:
        pattern_names = list(PATTERNS.keys())
        loop_count = 0
        try:
            while args.smoke_loops == 0 or loop_count < args.smoke_loops:
                for name in pattern_names:
                    timestamp = time.strftime("%H:%M:%S")
                    print(f"[{timestamp}] {name}")
                    rgb_data = PATTERNS[name](args.width, args.height)
                    send_rgb_data(args.host, args.port, rgb_data, args.width, args.height, delay=args.delay)
                    time.sleep(args.smoke_delay)
                loop_count += 1
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        if not args.pattern:
            parser.error("pattern is required unless --smoke is specified")
        rgb_data = PATTERNS[args.pattern](args.width, args.height)
        send_rgb_data(args.host, args.port, rgb_data, args.width, args.height, delay=args.delay)
        print(f"Sent '{args.pattern}' pattern ({args.width}x{args.height})")
