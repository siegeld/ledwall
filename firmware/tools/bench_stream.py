#!/usr/bin/env python3
"""Measure bitmap-UDP streaming throughput and loss behaviour on a live panel.

Three modes, all of which diff /api/bitmap/stats across a controlled send:

  sweep    Packet loss vs offered rate. Finds the rate at which the panel stops
           keeping up. This is the headline number.

  isolate  Separates per-pixel cost from per-packet cost by holding the packet
           rate fixed and varying only the payload size. Payload size *is* the
           number of SDRAM writes the firmware performs (one 32-bit store per
           RGB triple), so if loss tracks payload size the bottleneck is the
           pixel loop, not the network stack.

  drop     Deliberately omits one chunk per frame to show how the receiver's
           completion logic reacts to a single loss.

Baseline measured on v1.10.6 (256x128, 68 chunks/frame), for comparison after
the v1.10.8 changes:

    sweep    clean to 15.6 fps; 22.6% loss at 23.9 fps
    isolate  at 2400 pkt/s: 2.1% loss at 10 px/packet vs 49.4% at 487 px/packet
             -> a flat ~600,000 px/s ceiling regardless of offered rate
    drop     omitting the LAST chunk: 0 of 10 frames completed

Usage:
    ./bench_stream.py --host 192.168.1.50 sweep
    ./bench_stream.py --host 192.168.1.50 isolate
    ./bench_stream.py --host 192.168.1.50 drop
"""
import argparse
import json
import socket
import struct
import sys
import time
import urllib.request

HEADER_FMT = "<2sHBBHH"
PAYLOAD = 1461  # 487 pixels, pixel-aligned


def stats(host, tries=10):
    """Read the stats API, retrying: the panel has a single HTTP socket and
    refuses back-to-back connections while the previous one drains."""
    last = None
    for _ in range(tries):
        try:
            with urllib.request.urlopen(
                f"http://{host}/api/bitmap/stats", timeout=5
            ) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 - report whatever finally failed
            last = e
            time.sleep(0.7)
    raise SystemExit(f"cannot read stats from {host}: {last}")


def paced_send(host, port, nframes, chunks, w, h, delay, paylen=PAYLOAD, omit=None):
    """Send nframes frames, pacing against an absolute deadline so the offered
    rate is actually the requested one. Returns (packets_sent, elapsed)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    chunk = bytes(paylen)
    # The LAST chunk of a frame is short, because the image is not a whole
    # number of chunks. marquee's stream.py slices the body and so sends a short
    # tail; padding it to full size is not what the panel ever sees in the
    # field, and it is not harmless: the gateware DMA declines to set the
    # arrival bit for a chunk whose pixels run past the end of the image, so
    # every frame arrives one chunk short. The CPU's own packet count used to
    # paper over that. Once pixel traffic is filtered away from the CPU
    # (RB-5486) the DMA bitmap is the only evidence there is, and a padded
    # benchmark reports every frame as partial while the real sender is fine.
    tail = (w * h * 3) - (chunks - 1) * paylen
    last_chunk = bytes(tail) if 0 < tail < paylen else chunk
    sent = 0
    t0 = deadline = time.perf_counter()
    for f in range(nframes):
        for i in range(chunks):
            if omit is not None and i == (omit if omit >= 0 else chunks + omit):
                continue
            s.sendto(
                struct.pack(HEADER_FMT, b"BM", f & 0xFFFF, i, chunks, w, h)
                + (last_chunk if i == chunks - 1 else chunk),
                (host, port),
            )
            sent += 1
            if delay > 0:
                deadline += delay
                rem = deadline - time.perf_counter()
                if rem > 0.002:
                    time.sleep(rem - 0.001)
                while time.perf_counter() < deadline:
                    pass
    el = time.perf_counter() - t0
    s.close()
    return sent, el


def measure(host, port, nframes, chunks, w, h, delay, paylen=PAYLOAD, omit=None):
    stats(host)
    time.sleep(0.4)
    a = stats(host)
    time.sleep(0.3)
    sent, el = paced_send(host, port, nframes, chunks, w, h, delay, paylen, omit)
    time.sleep(1.5)
    b = stats(host)
    d = lambda k: b.get(k, 0) - a.get(k, 0)  # noqa: E731
    return {
        "sent": sent,
        "elapsed": el,
        "rate": sent / el,
        "got": d("packets_total"),
        "loss": 100 * max(0, sent - d("packets_total")) / sent,
        "completed": d("frames_completed"),
        "partial": d("frames_partial"),
        "dropped": d("frames_dropped"),
        "stale": d("frames_stale"),
        "repaired": d("chunks_repaired"),
        "ovf": d("mac_overflow"),
    }


def sweep(args, chunks):
    print(f"{'spacing':>9} {'offered':>10} {'loss':>7} {'mac_ovf':>8} "
          f"{'shown':>7} {'partial':>8} {'dropped':>8} {'repaired':>9}")
    for delay in [0.010, 0.005, 0.002, 0.0012, 0.0008, 0.0005, 0.0003]:
        r = measure(args.host, args.port, args.frames, chunks,
                    args.width, args.height, delay)
        fps = r["rate"] / chunks
        print(f"{delay*1000:8.2f}ms {fps:8.2f}fps {r['loss']:6.1f}% {r['ovf']:8d} "
              f"{r['completed']:7d} {r['partial']:8d} {r['dropped']:8d} {r['repaired']:9d}")


def isolate(args, chunks):
    print("payload size is the SDRAM write count; packet rate is held fixed\n")
    print(f"{'offered':>11} {'10 px/pkt':>11} {'487 px/pkt':>12}   {'px/s absorbed':>14}")
    for delay in [0.0008, 0.0005, 0.0004, 0.0003, 0.0002]:
        tiny = measure(args.host, args.port, args.frames, chunks,
                       args.width, args.height, delay, paylen=30)
        full = measure(args.host, args.port, args.frames, chunks,
                       args.width, args.height, delay, paylen=PAYLOAD)
        absorbed = full["rate"] * (1 - full["loss"] / 100) * (PAYLOAD // 3)
        print(f"{full['rate']:8.0f}/s {tiny['loss']:10.1f}% {full['loss']:11.1f}% "
              f"{absorbed:14,.0f}")


def drop(args, chunks):
    print(f"one chunk omitted per frame, at {args.drop_delay*1000:.2f}ms spacing\n")
    print(f"{'omitted':>16} {'completed':>10} {'partial':>8} {'dropped':>8} "
          f"{'stale':>6} {'repaired':>9}")
    cases = [("none (control)", None), ("#0 first", 0),
             (f"#{chunks//2} middle", chunks // 2), (f"#{chunks-1} LAST", -1)]
    for label, omit in cases:
        r = measure(args.host, args.port, args.frames, chunks,
                    args.width, args.height, args.drop_delay, omit=omit)
        print(f"{label:>16} {r['completed']:10d} {r['partial']:8d} "
              f"{r['dropped']:8d} {r['stale']:6d} {r['repaired']:9d}")


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("mode", choices=["sweep", "isolate", "drop"])
    p.add_argument("--host", default="192.168.1.50")
    p.add_argument("--port", type=int, default=7000)
    p.add_argument("--width", type=int, default=256)
    p.add_argument("--height", type=int, default=128)
    p.add_argument("--frames", type=int, default=20)
    p.add_argument("--drop-delay", type=float, default=0.0012,
                   help="spacing for 'drop' mode; must be loss-free on its own")
    args = p.parse_args()

    nbytes = args.width * args.height * 3
    chunks = (nbytes + PAYLOAD - 1) // PAYLOAD
    if chunks > 255:
        sys.exit(f"{args.width}x{args.height} needs {chunks} chunks; "
                 "the wire format's chunk index is a u8")
    print(f"{args.host}  {args.width}x{args.height} = {nbytes} bytes, "
          f"{chunks} chunks/frame, {args.frames} frames/run\n")

    {"sweep": sweep, "isolate": isolate, "drop": drop}[args.mode](args, chunks)


if __name__ == "__main__":
    main()
