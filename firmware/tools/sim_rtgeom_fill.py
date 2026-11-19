"""Runtime geometry, the FILL side: a geometry rewritten under a running line
buffer must leave it filling whole, correctly placed rows.

    docker run --rm -v $PWD:/w -w /w litex-hub75:out4 python3 tools/sim_rtgeom_fill.py

One pixel source, the three-slot line buffer and the pipelined arbiter onto a
memory with latency, built for 64x16 and running at that size. At some clock
the geometry becomes 32x8 (or back), with `RtGeometry.hold` raised as the SoC
raises it on the CSR write. Afterwards every fill must deliver each column
exactly once, in order, with the data a line buffer that was ALWAYS the new
size stores for that row and column -- and fills must keep completing.

The same switch WITHOUT the hold is run too and must fail: that is the fault
found on the card (2026-11-16, 128x64 -> 64x32: every fill stopped until a
reboot), and a check that cannot fail proves nothing.
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateware"))
from migen import *
from migen.sim import run_simulation
from icn1065 import (Icn1065PixelSource, Icn1065LineBuffer,
                     Icn1065PipelinedArbiter, RtGeometry)

MC, MR, WIDTH, GROUPS, BLOCK = 64, 16, 64, 4, 4
OFFS = [0, 1024, 2048, 3072]
LAT = 12
ROW_T = 1500          # clocks per scan row: room for a fill, as on the card
HOLD = 400


def word_for(adr):
    return ((adr >> 16 & 0xFF) << 16) | ((adr >> 8 & 0xFF) << 8) | (adr & 0xFF)


class DUT(Module):
    def __init__(self, cols, rows, hold=True):
        self.g = g = RtGeometry(MC, MR, block=BLOCK)
        g.cols_m1.reset, g.scan_m1.reset = cols - 1, rows - 1
        g.cols_n.reset, g.cols_thr.reset = cols, cols - BLOCK
        self.strobe = Signal()
        g.drive(self)
        g.hold_on(self, self.strobe if hold else 0, clocks=HOLD)
        self.submodules.src = src = Icn1065PixelSource(
            MC, MR, WIDTH, fb_base=0, gamma=False, groups=GROUPS,
            group_offsets=OFFS, pix_bits=32, pipeline=True, block=BLOCK,
            block_pairs=((0, 1), (2, 3)), block_seq=True)
        self.submodules.lb = lb = Icn1065LineBuffer(
            MC, MR, groups=GROUPS, pipeline=True, block=BLOCK, bufs=3, geom=g)
        self.comb += [src.req.eq(lb.src_req), src.col.eq(lb.src_col),
                      src.row.eq(lb.src_row), lb.src_valid.eq(src.valid),
                      lb.src_req_ack.eq(src.req_ack),
                      *[lb.src_channels[c].eq(src.channels[c])
                        for c in range(3 * GROUPS)]]
        self.submodules.arb = Icn1065PipelinedArbiter(1, per_source=4 * BLOCK,
                                                      sticky=True)
        self.comb += self.arb.connect([src])


def run(start, switch=None, hold=True, clocks=26000):
    """-> list of fills after the switch: (row, [channels per column], whole)."""
    dut = DUT(*start, hold=hold)
    fills = []

    def bench():
        inflight, cur, was = [], None, 0
        cols, rows = start
        row = 0
        yield dut.arb.cmd_ready.eq(1)
        for cycle in range(clocks):
            if switch and cycle == switch[0]:
                cols, rows = switch[1]
                yield dut.g.cols_m1.eq(cols - 1)
                yield dut.g.scan_m1.eq(rows - 1)
                yield dut.strobe.eq(1)
                # The row the serialiser is on is NOT reset: the frame in
                # flight keeps its row, which may be past the new last row,
                # until the output stage ends it (`_at_last`).
            elif switch and cycle == switch[0] + 1:
                yield dut.strobe.eq(0)
            if cycle % ROW_T == 0:
                row = (row + 1) % rows
                yield dut.lb.row.eq(row)
                yield dut.lb.row_base.eq(row)
            if (yield dut.arb.cmd_valid):
                inflight.append((cycle + LAT, (yield dut.arb.cmd_adr)))
            yield dut.arb.dat_valid.eq(0)
            if inflight and inflight[0][0] <= cycle:
                _, adr = inflight.pop(0)
                yield dut.arb.dat.eq(word_for(adr))
                yield dut.arb.dat_valid.eq(1)
            f = yield dut.lb.filling_sig
            if f and not was:
                cur = [(yield dut.lb.src_row), [], cycle]
            if f and (yield dut.lb.src_valid) and cur is not None:
                vals = []
                for ch in dut.lb.src_channels:
                    vals.append((yield ch))
                cur[1].append(tuple(vals))
            if was and not f and cur is not None:
                after = not switch or cur[2] > switch[0] + HOLD + 2
                if after:
                    fills.append((cur[0], cur[1], cols))
                cur = None
            was = f
            yield

    run_simulation(dut, bench())
    return fills


def check(frm, to, at, hold=True):
    ref = {}
    for row, data, _ in run(to):
        ref.setdefault(row, data)
    got = run(frm, switch=(at, to), hold=hold)
    bad = [(r, len(d)) for r, d, c in got if len(d) != c or ref.get(r) != d]
    ok = len(got) >= 8 and not bad
    return ok, len(got), bad[:3]


if __name__ == "__main__":
    fails = 0
    big, small = (MC, MR), (32, 8)
    print("Geometry rewritten under a running line buffer")
    for frm, to in ((big, small), (small, big), (big, (48, 12))):
        # 6200 and 7700 land inside a fill, past column 32 (the hang);
        # 7003 between fills.
        for at in (6200, 7003, 7700):
            ok, n, bad = check(frm, to, at)
            print(f"  {'ok  ' if ok else 'FAIL'} {frm[0]}x{frm[1]} -> {to[0]}x{to[1]} "
                  f"at clock {at}: {n} fills after, "
                  f"{'all whole and correct' if ok else 'wrong: ' + str(bad)}", flush=True)
            fails += not ok
    print("The same switch WITHOUT the hold (must fail somewhere)")
    broke = 0
    for at in (6200, 7700):
        ok, n, bad = check(big, small, at, hold=False)
        print(f"  {'still fine' if ok else 'breaks    '} at clock {at}: {n} fills after"
              + ("" if ok else f", {bad}"))
        broke += not ok
    if not broke:
        print("  FAIL: the unprotected switch never broke -- this test cannot see the fault")
        fails += 1
    sys.exit(1 if fails else 0)
