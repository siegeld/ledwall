#!/usr/bin/env python3
"""Stream a video file to the LED panel via UDP using ffmpeg for decoding."""
import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import time

HEADER_FMT = "<2sHBBHH"
HEADER_SIZE = struct.calcsize(HEADER_FMT)
MAX_PAYLOAD = 1462
PIXELS_PER_CHUNK = MAX_PAYLOAD // 3  # 487
BYTES_PER_CHUNK = PIXELS_PER_CHUNK * 3  # 1461


def get_video_fps(path):
    """Probe video file for frame rate using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                "-select_streams", "v:0",
                path,
            ],
            capture_output=True, text=True, timeout=10,
        )
        info = json.loads(result.stdout)
        stream = info["streams"][0]
        # r_frame_rate is like "30/1" or "30000/1001"
        num, den = stream["r_frame_rate"].split("/")
        return float(num) / float(den)
    except Exception as e:
        print(f"Warning: could not probe FPS ({e}), defaulting to 30", file=sys.stderr)
        return 30.0


def open_ffmpeg(path, width, height):
    """Spawn ffmpeg to decode video into raw RGB24 frames on stdout."""
    return subprocess.Popen(
        [
            "ffmpeg",
            "-loglevel", "error",
            "-i", path,
            "-f", "rawvideo",
            "-pix_fmt", "rgb24",
            "-s", f"{width}x{height}",
            "-",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def parse_layout_dims(layout_str, panel_size_str="128x64"):
    """Compute virtual display dimensions from layout spec and panel size."""
    pw, ph = (int(x) for x in panel_size_str.split("x"))
    cols, rows = (int(x) for x in layout_str.split("x"))
    return pw * cols, ph * rows


def main():
    parser = argparse.ArgumentParser(
        description="Stream a video file to the LED panel via UDP"
    )
    parser.add_argument("video", help="Path to video file")
    parser.add_argument("--host", default="192.168.1.50")
    parser.add_argument("--port", type=int, default=7000)
    parser.add_argument("--width", type=int, default=128)
    parser.add_argument("--height", type=int, default=64)
    parser.add_argument("--fps", type=float, default=15,
                        help="Output frame rate (default: 15)")
    parser.add_argument("--loop", action="store_true",
                        help="Loop video indefinitely")
    parser.add_argument("--layout",
                        help="Grid layout (e.g. 1x2) — overrides --width/--height")
    parser.add_argument("--panel-size", default="128x64",
                        help="Physical panel size (default: 128x64)")
    parser.add_argument("--chunk-delay", type=float, default=None,
                        help="Delay between UDP chunks in seconds. "
                             "Auto-calculated from fps when omitted (burst-size=0 only).")
    parser.add_argument("--burst-size", type=int, default=0,
                        help="Packets per burst (default: 0=uniform chunk-delay mode)")
    parser.add_argument("--burst-delay", type=float, default=0.003,
                        help="Pause between bursts in seconds (default: 0.003)")
    args = parser.parse_args()

    if not os.path.isfile(args.video):
        print(f"Error: file not found: {args.video}", file=sys.stderr)
        sys.exit(1)

    if args.layout:
        args.width, args.height = parse_layout_dims(args.layout, args.panel_size)

    # Determine frame rate
    fps = args.fps
    frame_period = 1.0 / fps
    frame_size = args.width * args.height * 3
    burst_size = args.burst_size
    burst_delay = args.burst_delay
    total_chunks_est = (frame_size + BYTES_PER_CHUNK - 1) // BYTES_PER_CHUNK
    chunk_delay = args.chunk_delay
    if chunk_delay is None:
        chunk_delay = (0.9 / fps) / total_chunks_est

    print(f"Streaming {args.video} at {fps:.1f} fps -> {args.host}:{args.port} ({args.width}x{args.height})",
          file=sys.stderr)

    frame_id = int(time.time()) & 0xFFFF
    frame_count = 0
    loop_count = 0
    t_start = time.monotonic()
    width, height = args.width, args.height
    dest = (args.host, args.port)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    try:
        while True:
            loop_count += 1
            proc = open_ffmpeg(args.video, width, height)

            while True:
                t0 = time.monotonic()
                frame_data = proc.stdout.read(frame_size)
                if len(frame_data) < frame_size:
                    break

                # Send frame chunks with small delay to avoid receiver buffer overflow
                total_chunks = (len(frame_data) + BYTES_PER_CHUNK - 1) // BYTES_PER_CHUNK
                for i in range(total_chunks):
                    offset = i * BYTES_PER_CHUNK
                    chunk = frame_data[offset : offset + BYTES_PER_CHUNK]
                    header = struct.pack(
                        HEADER_FMT, b"BM", frame_id, i, total_chunks, width, height
                    )
                    sock.sendto(header + chunk, dest)
                    if i < total_chunks - 1:
                        if burst_size > 0 and (i + 1) % burst_size == 0:
                            time.sleep(burst_delay)
                        elif burst_size == 0:
                            time.sleep(chunk_delay)
                frame_id = (frame_id + 1) & 0xFFFF
                frame_count += 1

                # Print stats every 30 frames
                if frame_count % 30 == 0:
                    elapsed = time.monotonic() - t_start
                    actual_fps = frame_count / elapsed if elapsed > 0 else 0
                    print(f"\rFrame {frame_count}  {actual_fps:.1f} fps  loop {loop_count}",
                          end="", file=sys.stderr)

                # Frame rate control
                elapsed = time.monotonic() - t0
                remaining = frame_period - elapsed
                if remaining > 0:
                    time.sleep(remaining)

            proc.stdout.close()
            proc.wait()

            if not args.loop:
                break
            print(f"\nLoop {loop_count} done, restarting...", file=sys.stderr)

    except KeyboardInterrupt:
        pass
    finally:
        sock.close()

    elapsed = time.monotonic() - t_start
    actual_fps = frame_count / elapsed if elapsed > 0 else 0
    print(f"\nDone: {frame_count} frames in {elapsed:.1f}s ({actual_fps:.1f} fps)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
