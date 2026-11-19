"""Runtime geometry: a stage built for a bigger module and told a smaller one
must emit, clock for clock, what a stage BUILT for the smaller one emits.

    docker run --rm -v $PWD:/w -w /w litex-hub75:out4 python3 tools/sim_rtgeom.py

Compares LAT, OE, the RGB lines, busy, the pixel request and its column/row
over three frames (start held high, so every later frame starts from HOLD).
The line buffer and pixel source are covered by `RT=1 tools/sim_fillrate.py`.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateware"))
from migen import *
from migen.sim import run_simulation
from icn1065 import Icn1065Output, RtGeometry, GROUPS_HUB320

SLOT = 16


def trace(dut, frames=3, cap=400000):
    out = []

    def bench():
        yield dut.start.eq(1)
        done = 0
        for _ in range(cap):
            col = yield dut.pix_col
            row = yield dut.pix_row
            for ch, sig in enumerate(dut.pix_in):
                yield sig.eq((row * 37 + col * 5 + ch * 11) & 0xFFF)
            out.append(((yield dut.lat), (yield dut.oe), (yield dut.rgb),
                        (yield dut.busy), (yield dut.pix_req), col, row))
            if (yield dut.frame_done):
                done += 1
                if done == frames:
                    break
            yield
    run_simulation(dut, bench())
    return out


def case(cols, rows, mcols, mrows):
    ref = trace(Icn1065Output(cols, rows, slot_len=SLOT, groups=GROUPS_HUB320))
    g = RtGeometry(mcols, mrows)
    g.cols_m1.reset, g.scan_m1.reset, g.cols_n.reset = cols - 1, rows - 1, cols
    got = trace(Icn1065Output(mcols, mrows, slot_len=SLOT,
                              groups=GROUPS_HUB320, geom=g))
    if ref == got:
        print(f"  ok   {cols}x{rows} inside a {mcols}x{mrows} build: "
              f"{len(ref)} clocks identical")
        return 0
    n = next((i for i, (a, b) in enumerate(zip(ref, got)) if a != b),
             min(len(ref), len(got)))
    print(f"  FAIL {cols}x{rows} inside {mcols}x{mrows}: lengths {len(ref)}/"
          f"{len(got)}, first difference at clock {n}: "
          f"{ref[n] if n < len(ref) else None} vs {got[n] if n < len(got) else None}")
    return 1


def frames_of(tr):
    """Split a trace into whole frames at frame_done (the last tuple field)."""
    out, cur = [], []
    for t in tr:
        cur.append(t[:-1])
        if t[-1]:
            out.append(cur)
            cur = []
    return out


def switch_case(cols, rows, mcols, mrows, at):
    """Firmware's sequence on a RUNNING display: off, rewrite the geometry,
    on. `at` clocks into the full-size picture, wherever that lands. The
    frames after the switch must be the frames a build of that size emits."""
    def run(dut, g):
        tr = []

        def bench():
            yield dut.start.eq(1)
            done = 0
            for n in range(900000):
                if g is not None and n == at:
                    yield dut.start.eq(0)
                if g is not None and n == at + 40:
                    yield g.cols_m1.eq(cols - 1)
                    yield g.scan_m1.eq(rows - 1)
                    yield g.cols_n.eq(cols)
                if g is not None and n == at + 60000:
                    yield dut.start.eq(1)
                col = yield dut.pix_col
                row = yield dut.pix_row
                for ch, sig in enumerate(dut.pix_in):
                    yield sig.eq((row * 37 + col * 5 + ch * 11) & 0xFFF)
                fd = yield dut.frame_done
                tr.append(((yield dut.lat), (yield dut.oe), (yield dut.rgb),
                           (yield dut.busy), (yield dut.pix_req), col, row, fd))
                if fd and (g is None or n > at + 60000):
                    done += 1
                    if done == 4:
                        break
                yield
        run_simulation(dut, bench())
        return frames_of(tr)

    ref = run(Icn1065Output(cols, rows, slot_len=SLOT, groups=GROUPS_HUB320), None)
    g = RtGeometry(mcols, mrows)
    got = run(Icn1065Output(mcols, mrows, slot_len=SLOT, groups=GROUPS_HUB320,
                            geom=g), g)
    # The last two whole frames of each: steady state, past any restart frame.
    # Two things are NOT compared clock for clock, because a build of the
    # small size does not repeat them frame to frame either: the row scanner
    # (OE here) free-runs against the frame, and the prefix sends a different
    # chip register each frame. So: LAT, busy, the pixel request and its
    # column/row identical; RGB identical wherever LAT is low and no command
    # is in flight (the pixel shift); OE the same number of clocks high, give
    # or take one pulse.
    def same(fa, fb):
        if len(fa) != len(fb):
            return False
        for x, y in zip(fa, fb):
            if (x[0], x[3], x[4], x[5], x[6]) != (y[0], y[3], y[4], y[5], y[6]):
                return False
            if x[4] and x[2] != y[2]:
                return False
        return abs(sum(x[1] for x in fa) - sum(y[1] for y in fb)) <= 4

    if (len(ref) >= 3 and len(got) >= 3
            and all(same(a, b) for a, b in zip(ref[-2:], got[-2:]))):
        print(f"  ok   switched {mcols}x{mrows} -> {cols}x{rows} at clock {at}: "
              f"steady frames match ({len(ref[-1])} clocks each)")
        return 0
    print(f"  FAIL switched {mcols}x{mrows} -> {cols}x{rows} at clock {at}: "
          f"frame lengths ref {[len(f) for f in ref[-2:]]} "
          f"got {[len(f) for f in got[-2:]]}")
    return 1


if __name__ == "__main__":
    print("Runtime geometry vs a build of that geometry (Icn1065Output)")
    fails = (case(16, 8, 32, 16) + case(32, 8, 64, 16) + case(16, 4, 64, 16)
             + case(32, 16, 32, 16))
    print("Geometry rewritten under a running display (off, write, on)")
    for at in (777, 5003, 20011, 33333):
        fails += switch_case(16, 4, 64, 16, at) + switch_case(32, 8, 64, 16, at)
    sys.exit(1 if fails else 0)
