#!/usr/bin/env python3
"""Three-slot line buffer (EXP_TRIPLE): every cell it returns is the right one.

The serialiser's `pix_row` is NOT a plain row counter: with sweep_adv=1 the last
channel of each row reads the NEXT row, so `pix_row` runs 1/16 of a row ahead of
the true counter (`pix_row_base`). The three-slot buffer rotates on the true row
and serves `row_base + 1` from the next slot. This sim models exactly that and
checks every cell against a golden two-slot run at a long row period, and that
no read lands on an unprimed slot.

Run inside the build image, from the repo root:
    docker run --rm -v $PWD:/w -w /w litex-hub75 python3 tools/sim_linebuf3.py
Expect: P=1400 mismatches=0, unprimed=0. (A period shorter than the fill itself,
PS=1000, shows unprimed rows by design: three slots absorb jitter, not a
shortfall in average throughput.)
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateware"))
from migen import *
from migen.sim import run_simulation
from icn1065 import (Icn1065PixelSource, Icn1065LineBuffer, Icn1065PipelinedArbiter)
COLS, ROWS, WIDTH, GROUPS, N = 32, 8, 64, 4, 4
BASES = [0, 16384, 32768, 49152]; OFFS = [0, 32, 64*8, 64*8+32]
LAT, ACC = 12, 2
def word_for(a): return ((a >> 16 & 255) << 16) | ((a >> 8 & 255) << 8) | (a & 255)

class DUT(Module):
    def __init__(self, bufs):
        self.srcs, self.lbs = [], []
        for i in range(N):
            src = Icn1065PixelSource(COLS, ROWS, WIDTH, fb_base=BASES[i], gamma=False, groups=GROUPS,
                                     group_offsets=OFFS, pix_bits=32, pipeline=True)
            lb = Icn1065LineBuffer(COLS, ROWS, groups=GROUPS, pipeline=True, bufs=bufs)
            self.submodules += src, lb
            self.comb += [src.req.eq(lb.src_req), src.col.eq(lb.src_col), src.row.eq(lb.src_row),
                          lb.src_valid.eq(src.valid), lb.src_req_ack.eq(src.req_ack),
                          *[lb.src_channels[c].eq(src.channels[c]) for c in range(3*GROUPS)]]
            self.srcs.append(src); self.lbs.append(lb)
        self.submodules.arb = Icn1065PipelinedArbiter(N, per_source=2*GROUPS)
        self.comb += self.arb.connect(self.srcs)

def run(bufs, P, frames=3, flick=True):
    dut = DUT(bufs)
    rec = {}      # (frame,row) -> (primed, data)
    def bench():
        inflight = []; last_due = -10**9
        yield dut.arb.cmd_ready.eq(1)
        row = 0; t_row = 0; cyc = 0; frame = 0
        for lb in dut.lbs:
            yield lb.row.eq(0); yield lb.row_base.eq(0)
        state = 'wait'; col = 0; primed = None
        total = P * ROWS * frames + 3 * P
        while cyc < total:
            if (yield dut.arb.cmd_valid):
                due = max(cyc + LAT, last_due + ACC); last_due = due
                inflight.append((due, (yield dut.arb.cmd_adr)))
            yield dut.arb.dat_valid.eq(0)
            if inflight and inflight[0][0] <= cyc:
                _, a = inflight.pop(0)
                yield dut.arb.dat.eq(word_for(a)); yield dut.arb.dat_valid.eq(1)
            # serialiser model: advance the row every P cycles; sample at +4 and at P-COLS*2
            ph = cyc - t_row
            if ph == 4:
                primed = []
                for lb in dut.lbs: primed.append((yield lb.primed))
            if ph >= P - 3 * COLS - 4 and ph < P - 4:
                k = (ph - (P - 3 * COLS - 4))
                c = k // 3
                if k % 3 == 0:
                    rr = (row + 1) % ROWS if (flick and c % 16 == 15) else row
                    for lb in dut.lbs:
                        yield lb.col.eq(c); yield lb.row.eq(rr)
                elif k % 3 == 2:
                    pr = []
                    for lb in dut.lbs: pr.append((yield lb.primed))
                    rec.setdefault(('p', frame, row), []).append(all(pr))
                    vals = []
                    for lb in dut.lbs:
                        t = []
                        for ch in lb.channels: t.append((yield ch))
                        vals.append(tuple(t))
                    rec.setdefault((frame, row), [primed, {}])[1][c] = vals if True else None
            if ph == P - 1:
                row = (row + 1) % ROWS; t_row = cyc + 1
                if row == 0: frame += 1
                for lb in dut.lbs:
                    yield lb.row.eq(row); yield lb.row_base.eq(row)
            cyc += 1
            yield
    run_simulation(dut, bench())
    return rec

if __name__ == "__main__":
    # golden: legacy double buffer, huge period, every cell read from its own row
    g = run(2, 6000, 2, flick=False)
    # per true row: data[c] for the row actually read (row, or row+1 on flicker columns)
    def gold(row, c): return g[(1, row)][1][c]
    for P in [int(x) for x in os.environ.get("PS", "1400").split(",")]:
        r = run(3, P, 3, flick=True)
        bad = checked = unp = 0
        for k, v in r.items():
            if k[0] == 'p':
                if k[1] >= 1 and not all(v): unp += 1
                continue
            f, row = k
            if f < 1: continue
            for c, vals in v[1].items():
                rr = (row + 1) % ROWS if c % 16 == 15 else row
                checked += 1
                if vals != g[(1, rr)][1][c]:
                    bad += 1
                    if bad <= 12: print('  mismatch frame', f, 'row', row, 'col', c, 'read-row', rr)
        print(f"P={P}: bufs=3 flicker: cells checked={checked} mismatches={bad} rows-with-unprimed-reads={unp}", flush=True)
        if bad or unp:
            sys.exit(1)
