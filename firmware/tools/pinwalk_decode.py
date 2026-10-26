#!/usr/bin/env python3
"""
Decode pin-walker meter readings into FPGA balls, and check them against the
5A-75E map so a familiar card is confirmed in one pass instead of mapped from
scratch.

    ./tools/pinwalk_decode.py 1=2.9 2=2.85 3=3.35
    ./tools/pinwalk_decode.py --connector j5 1=2.9 2=2.85 3=3.35

Readings may be given in kHz (2.9) or Hz (2900) -- anything under 100 is taken
as kHz, which is what a bench meter shows. A reading of 0 is a ground or an
undriven pin and is recorded as such.

Why bother with --connector: if an unknown card turns out to reuse a 5A-75E
connector's ball set, the tool says so and prints what the REMAINING pins
should read. Confirming a prediction is far faster than measuring blind, and a
prediction that fails is the interesting result rather than a silent wrong map.
"""

import argparse
import csv
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_CSV = os.path.join(HERE, "build", "pinwalk", "pinwalk-map.csv")

# Slot order of a 5A-75E connector entry, from gateware/helper.py: it takes
# pins[0..2] as r0/g0/b0, pins[4..6] as r1/g1/b1, pins[8..11]+pins[7] as the
# row address A..E, and pins[12..14] as clk/lat/oe.
SLOT_LABELS = [
    "r0", "g0", "b0", "gnd", "r1", "g1", "b1", "E",
    "A", "B", "C", "D", "clk", "lat", "oe", "gnd",
]


def load_map(path=MAP_CSV):
    if not os.path.exists(path):
        raise SystemExit(
            f"pinwalk_decode: no {path}.\n"
            "Build the walker first: python3 gateware/pinwalk.py --build"
        )
    with open(path) as handle:
        rows = list(csv.DictReader(handle))
    return {int(r["frequency_hz"]): r["ball"] for r in rows}


def load_5a75e_connectors():
    """The reference card's connector tables, for comparison."""
    try:
        from litex_boards.platforms import colorlight_5a_75e
        src = colorlight_5a_75e.__file__
        text = open(src).read()
    except Exception:
        return {}
    match = re.search(r"_connectors_v8_2\s*=\s*\[(.*?)\n\]\n", text, re.S)
    if not match:
        return {}
    out = {}
    for line in match.group(1).strip().split("\n"):
        m = re.match(r'\s*\("(j\d+)",\s*"(.*?)"\)', line)
        if m:
            out[m.group(1)] = m.group(2).split()
    return out


def normalise(value):
    """Accept 2.9 (kHz) or 2900 (Hz); return Hz, or 0 for ground."""
    hz = float(value)
    if hz == 0:
        return 0
    if hz < 100:               # a meter showing kHz
        hz *= 1000
    return int(round(hz))


def nearest(freq_to_ball, hz, tolerance=25):
    """Ball whose frequency is closest to the reading, or None if nothing is
    close enough. Balls are 50 Hz apart, so anything within 25 Hz is
    unambiguous; beyond that the reading is not one of ours."""
    if hz == 0:
        return None, 0
    best = min(freq_to_ball, key=lambda f: abs(f - hz))
    err = abs(best - hz)
    return (freq_to_ball[best], err) if err <= tolerance else (None, err)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("readings", nargs="+", metavar="PIN=KHZ",
                        help="e.g. 1=2.9 2=2.85 3=0")
    parser.add_argument("--connector", help="compare against this 5A-75E connector, e.g. j5")
    args = parser.parse_args()

    freq_to_ball = load_map()
    conns = load_5a75e_connectors()

    observed = {}
    for item in args.readings:
        if "=" not in item:
            raise SystemExit(f"pinwalk_decode: expected PIN=KHZ, got {item!r}")
        pin_s, val_s = item.split("=", 1)
        observed[int(pin_s)] = normalise(val_s)

    ref = conns.get(args.connector) if args.connector else None

    print(f"{'pin':>4} {'reading':>10} {'ball':>6}   {'5A-75E ' + (args.connector or '') :<12} {'verdict'}")
    matched = mismatched = grounds = unknown = 0
    for pin in sorted(observed):
        hz = observed[pin]
        ball, err = nearest(freq_to_ball, hz)
        slot = pin - 1
        exp_ball = exp_lab = ""
        verdict = ""
        if ref and slot < len(ref):
            raw = ref[slot]
            exp_lab = SLOT_LABELS[slot] if slot < len(SLOT_LABELS) else f"slot{slot}"
            exp_ball = "gnd" if raw == "-" else raw

        if hz == 0:
            grounds += 1
            shown, verdict = "0 Hz (gnd)", "ground/undriven"
            if exp_ball == "gnd":
                verdict = "ground -- MATCHES"
                matched += 1
        elif ball is None:
            unknown += 1
            shown, verdict = f"{hz} Hz", f"NO MATCH (nearest off by {err} Hz)"
        else:
            shown = f"{hz} Hz"
            if exp_ball and exp_ball != "gnd":
                if ball == exp_ball:
                    verdict = "MATCHES"
                    matched += 1
                else:
                    verdict = f"DIFFERS (expected {exp_ball})"
                    mismatched += 1
        label = f"{exp_lab} {exp_ball}".strip() if exp_ball else ""
        print(f"{pin:>4} {shown:>10} {ball or '-':>6}   {label:<12} {verdict}")

    if ref:
        print(f"\n  {matched} match, {mismatched} differ, {unknown} unrecognised, {grounds} ground")
        done = set(observed)
        todo = [s for s in range(len(ref)) if (s + 1) not in done]
        if todo and not mismatched:
            print(f"\n  If it keeps following {args.connector}, the rest should read:")
            for slot in todo:
                raw = ref[slot]
                lab = SLOT_LABELS[slot] if slot < len(SLOT_LABELS) else f"slot{slot}"
                if raw == "-":
                    print(f"    pin {slot+1:>2}  {lab:<4} 0 Hz (ground)")
                else:
                    hz = {v: k for k, v in freq_to_ball.items()}.get(raw)
                    shown = f"{hz/1000:.2f} kHz" if hz else "not driven (reserved ball)"
                    print(f"    pin {slot+1:>2}  {lab:<4} {raw:<4} {shown}")
            print("\n  HUB320 is 26 pins, so anything past pin 16 has no 5A-75E")
            print("  equivalent -- those are the two extra RGB groups and must be read.")


if __name__ == "__main__":
    main()
