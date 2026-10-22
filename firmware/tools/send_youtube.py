#!/usr/bin/env python3
"""Stream a YouTube video to the LED panel via UDP.

Uses yt-dlp to resolve the stream URL and ffmpeg to decode it into raw RGB24
frames, then sends them over the bitmap UDP protocol.

Requirements: yt-dlp, ffmpeg
  pip install yt-dlp   (or: brew install yt-dlp)
"""
import argparse
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


def parse_layout_dims(layout_str, panel_size_str="128x64"):
    """Compute virtual display dimensions from layout spec and panel size."""
    pw, ph = (int(x) for x in panel_size_str.split("x"))
    cols, rows = (int(x) for x in layout_str.split("x"))
    return pw * cols, ph * rows


def resolve_stream_url(url, format_sel, cookies=None):
    """Use yt-dlp -g to resolve the direct stream URL."""
    cmd = ["yt-dlp", "--no-warnings", "-f", format_sel, "-g", url]
    if cookies:
        cmd.insert(1, "--cookies")
        cmd.insert(2, cookies)
    print(f"Resolving: {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        print(f"yt-dlp error: {result.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    # -g may return multiple lines (video + audio); take the first (video)
    stream_url = result.stdout.strip().split("\n")[0]
    if not stream_url:
        print("Error: yt-dlp returned no URL", file=sys.stderr)
        sys.exit(1)
    return stream_url


def open_pipeline(url, width, height, fps=None, format_sel="18/b[ext=mp4]/b", cookies=None):
    """yt-dlp | ffmpeg, piped, decoding to raw RGB24 frames.

    NOT `yt-dlp -g` into `ffmpeg -i <url>`. That is what this did until
    2026-09-18 and it now fails with **403 Forbidden**: the URL yt-dlp hands
    back is bound to the client it negotiated as (the resolved URL carries
    `c=ANDROID`), and ffmpeg fetching it with its own headers is refused. The
    symptom is not an error from this script -- it is "Done: 0 frames in 0.2s",
    because ffmpeg writes its complaint to stderr and exits.

    Piping sidesteps the whole problem: yt-dlp does its own HTTP with its own
    headers and writes the container to stdout, and ffmpeg only has to decode a
    stream. It also drops one round trip, since the URL is never resolved twice.

    The format selector matters and the old default (`bv*`) does NOT work here.
    `bv*` picks a DASH video-only stream, which arrives as fragmented MP4 whose
    index ffmpeg cannot use on a non-seekable pipe -- "Invalid data found when
    processing input". A PROGRESSIVE file (itag 18) is a single container with
    its moov up front and decodes straight off stdin. Hence the default
    `18/b[ext=mp4]/b`: progressive first, any muxed MP4 next, anything last.
    Pass --format explicitly if you want something bigger and are willing to
    download to a file first.
    """
    dl = ["yt-dlp", "--no-warnings", "-f", format_sel, "-o", "-", url]
    if cookies:
        dl += ["--cookies", cookies]
    ff = [
        "ffmpeg",
        "-loglevel", "error",
        "-i", "pipe:0",
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}",
    ]
    if fps:
        ff += ["-r", str(fps)]
    ff += ["-"]
    dlp = subprocess.Popen(dl, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    proc = subprocess.Popen(ff, stdin=dlp.stdout, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    # So yt-dlp sees EPIPE and exits when ffmpeg goes away.
    dlp.stdout.close()
    proc._yt_dlp = dlp
    return proc


def stream_frames(url, width, height, dest, sock, args):
    """Resolve URL via yt-dlp, decode with ffmpeg, send frames over UDP."""
    print(f"Piping yt-dlp -> ffmpeg (format {args.format})", file=sys.stderr)
    proc = open_pipeline(url, width, height, fps=args.fps,
                         format_sel=args.format, cookies=args.cookies)

    frame_size = width * height * 3
    frame_id = int(time.time()) & 0xFFFF
    frame_count = 0
    burst_size = args.burst_size
    burst_delay = args.burst_delay
    total_chunks_est = (frame_size + BYTES_PER_CHUNK - 1) // BYTES_PER_CHUNK
    chunk_delay = args.chunk_delay
    if chunk_delay is None:
        chunk_delay = (0.9 / args.fps) / total_chunks_est
    frame_period = 1.0 / args.fps
    t_start = time.monotonic()

    try:
        while True:
            t0 = time.monotonic()
            frame_data = proc.stdout.read(frame_size)
            if len(frame_data) < frame_size:
                break

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

            if frame_count % 30 == 0:
                elapsed = time.monotonic() - t_start
                actual_fps = frame_count / elapsed if elapsed > 0 else 0
                print(f"\rFrame {frame_count}  {actual_fps:.1f} fps",
                      end="", file=sys.stderr)

            elapsed = time.monotonic() - t0
            remaining = frame_period - elapsed
            if remaining > 0:
                time.sleep(remaining)
    finally:
        proc.stdout.close()
        proc.terminate()
        proc.wait()

    return frame_count, time.monotonic() - t_start


def main():
    parser = argparse.ArgumentParser(
        description="Stream a YouTube video to the LED panel via UDP"
    )
    parser.add_argument("url", help="YouTube URL (or any yt-dlp supported URL)")
    parser.add_argument("--host", default="192.168.1.50")
    parser.add_argument("--port", type=int, default=7000)
    parser.add_argument("--width", type=int, default=128)
    parser.add_argument("--height", type=int, default=64)
    parser.add_argument("--fps", type=float, default=15,
                        help="Output frame rate (default: 15)")
    parser.add_argument("--format", default="18/b[ext=mp4]/b",
                        help="yt-dlp format selector (default: progressive MP4, which is what pipes)")
    parser.add_argument("--cookies",
                        help="Path to cookies.txt for auth/age-gated videos")
    parser.add_argument("--loop", action="store_true",
                        help="Loop video indefinitely (re-resolves URL each loop)")
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

    if args.layout:
        args.width, args.height = parse_layout_dims(args.layout, args.panel_size)

    width, height = args.width, args.height
    dest = (args.host, args.port)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    print(f"Streaming {args.url} -> {args.host}:{args.port} ({width}x{height})",
          file=sys.stderr)

    total_frames = 0
    total_time = 0.0
    loop_count = 0

    try:
        while True:
            loop_count += 1
            frames, elapsed = stream_frames(args.url, width, height, dest, sock, args)
            total_frames += frames
            total_time += elapsed

            if not args.loop:
                break
            print(f"\nLoop {loop_count} done, restarting...", file=sys.stderr)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()

    actual_fps = total_frames / total_time if total_time > 0 else 0
    print(f"\nDone: {total_frames} frames in {total_time:.1f}s ({actual_fps:.1f} fps)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
