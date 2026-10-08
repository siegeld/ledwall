#!/usr/bin/env python3
"""Send a bitmap image to the LED panel via UDP."""
import argparse
import socket
import struct
import time

from PIL import Image

HEADER_FMT = "<2sHBBHH"  # magic, frame_id, chunk_idx, total_chunks, width, height
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 10 bytes
MAX_PAYLOAD = 1462
PIXELS_PER_CHUNK = MAX_PAYLOAD // 3  # 487
BYTES_PER_CHUNK = PIXELS_PER_CHUNK * 3  # 1461 — pixel-aligned


def send_image(host, port, image_path, width=96, height=48, frame_id=None):
    if frame_id is None:
        frame_id = int(time.time()) & 0xFFFF
    img = Image.open(image_path).convert("RGB").resize((width, height))
    rgb_data = img.tobytes()  # flat R,G,B,R,G,B,...

    total_chunks = (len(rgb_data) + BYTES_PER_CHUNK - 1) // BYTES_PER_CHUNK

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for i in range(total_chunks):
        offset = i * BYTES_PER_CHUNK
        chunk = rgb_data[offset : offset + BYTES_PER_CHUNK]
        header = struct.pack(
            HEADER_FMT, b"BM", frame_id, i, total_chunks, width, height
        )
        sock.sendto(header + chunk, (host, port))
        # Small delay between chunks to avoid overwhelming the receiver
        time.sleep(0.01)
    sock.close()
    print(f"Sent {image_path} ({width}x{height}) in {total_chunks} chunks")


def parse_layout_dims(layout_str, panel_size_str="128x64"):
    """Compute virtual display dimensions from layout spec and panel size."""
    pw, ph = (int(x) for x in panel_size_str.split("x"))
    cols, rows = (int(x) for x in layout_str.split("x"))
    return pw * cols, ph * rows


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Send a bitmap image to the LED panel")
    parser.add_argument("image", help="Path to image file")
    parser.add_argument("--host", default="192.168.1.50")
    parser.add_argument("--port", type=int, default=7000)
    parser.add_argument("--width", type=int, default=128)
    parser.add_argument("--height", type=int, default=64)
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
    send_image(args.host, args.port, args.image, args.width, args.height)
