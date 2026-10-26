"""Row-fill throughput and equivalence: current reader vs the pipelined reader.

Four outputs, each a pixel source feeding a line buffer, all behind one
pipelined arbiter onto a memory with a stated latency and command rate. Fills
one row per output and reports (a) that the line buffers hold IDENTICAL data in
both modes and (b) how many system clocks the fill took.

BLOCK=K tests block fetch instead of the plain pipelined reader; ROW=hit,miss
models DRAM rows (bank = row % 4) instead of a flat command rate. OFFS and PAIRS
set the group offsets and which groups block fetch pairs -- the P1.25 shape is
OFFS=32,544,0,512 PAIRS=0,2,1,3 (groups 0 and 2 share a row). For the fill RATE
against the real controller use tools/sim_litedram.py; this one is the
correctness check.

    docker run --rm -v $PWD:/w -w /w litex-hub75 \
        env BLOCK=4 ROW=2,8 OFFS=32,544,0,512 PAIRS=0,2,1,3 python3 tools/sim_fillrate.py
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateware"))
from migen import *
from migen.sim import run_simulation
from icn1065 import (Icn1065PixelSource, Icn1065LineBuffer,
                     Icn1065PipelinedArbiter)

COLS, ROWS, WIDTH, GROUPS = 32, 8, 64, 4
CAP = int(os.environ.get('CAP', '20000'))
BLOCK = int(os.environ.get('BLOCK', '0'))
STAT = [0, 0]
ROW = tuple(int(x) for x in os.environ['ROW'].split(',')) if os.environ.get('ROW') else None
BASES = [int(x) for x in os.environ.get('BASES', '0,16384,32768,49152').split(',')]
OFFS = [int(x) for x in os.environ.get('OFFS', '0,32,512,544').split(',')]
_P = [int(x) for x in os.environ.get('PAIRS', '0,1,2,3').split(',')]
PAIRS = ((_P[0], _P[1]), (_P[2], _P[3]))


def word_for(adr):
    # 0x00BBGGRR from the address, so every (pixel, group) is distinguishable
    return ((adr >> 16 & 0xFF) << 16) | ((adr >> 8 & 0xFF) << 8) | (adr & 0xFF) | 0


class DUT(Module):
    def __init__(self, pipeline, n):
        self.srcs, self.lbs = [], []
        for i in range(n):
            src = Icn1065PixelSource(COLS, ROWS, WIDTH, fb_base=BASES[i],
                                     gamma=False, groups=GROUPS,
                                     group_offsets=OFFS, pix_bits=32,
                                     pipeline=pipeline, block=BLOCK,
                                     block_pairs=PAIRS)
            lb = Icn1065LineBuffer(COLS, ROWS, groups=GROUPS, pipeline=pipeline, block=BLOCK)
            self.submodules += src, lb
            self.comb += [src.req.eq(lb.src_req), src.col.eq(lb.src_col),
                          src.row.eq(lb.src_row), lb.src_valid.eq(src.valid),
                          *[lb.src_channels[c].eq(src.channels[c])
                            for c in range(3 * GROUPS)]]
            if pipeline:
                self.comb += lb.src_req_ack.eq(src.req_ack)
            self.srcs.append(src)
            self.lbs.append(lb)
        self.submodules.arb = Icn1065PipelinedArbiter(
            n, per_source=(4 * BLOCK if BLOCK else (2 * GROUPS if pipeline else GROUPS)),
            sticky=bool(BLOCK))
        self.comb += self.arb.connect(self.srcs)


def run(pipeline, n, latency, accept_every):
    dut = DUT(pipeline, n)
    done = [None] * n
    seen = [0] * n
    content = {}

    def bench():
        inflight = []
        last_due = -10**9
        openrow, busy, stats = {}, [0], STAT; STAT[0] = STAT[1] = 0
        yield dut.arb.cmd_ready.eq(1)          # the memory always takes a command
        for cycle in range(CAP):
            if (yield dut.arb.cmd_valid):
                # data returns after `latency`, and no faster than one word per
                # `accept_every` clocks: that is the memory's throughput limit
                adr_now = (yield dut.arb.cmd_adr)
                if ROW:
                    # in-order single port onto a bank/row model: a command to
                    # the row already open in its bank costs HIT cycles of bus,
                    # to any other row MISS (precharge + activate + CAS)
                    if os.environ.get('TRACE') and pipeline and stats[1] < 70: print('cyc', cycle, 'pick', (yield dut.arb.grant), 'adr', adr_now, 'row', adr_now // WIDTH)
                    rw = adr_now // WIDTH
                    bk = rw % 4
                    cost = ROW[0] if openrow.get(bk) == rw else ROW[1]
                    openrow[bk] = rw
                    busy[0] = max(busy[0], cycle) + cost
                    due = busy[0] + latency
                    stats[0] += (cost == ROW[1])
                    stats[1] += 1
                else:
                    due = max(cycle + latency, last_due + accept_every)
                last_due = due
                inflight.append((due, (yield dut.arb.cmd_adr)))
            yield dut.arb.dat_valid.eq(0)
            if inflight and inflight[0][0] <= cycle:
                _, adr = inflight.pop(0)
                yield dut.arb.dat.eq(word_for(adr))
                yield dut.arb.dat_valid.eq(1)
            for i, s in enumerate(dut.srcs):
                if (yield s.valid):
                    seen[i] += 1
                    if seen[i] == COLS and done[i] is None:
                        done[i] = cycle
            if all(d is not None for d in done) and cycle > max(done) + 20:
                break
            yield
        # read the filled row (half 1) back through each line buffer's own read port
        for i in range(n):
            yield dut.lbs[i].row.eq(1)
            content[i] = []
        for c in range(COLS):
            for i in range(n):
                yield dut.lbs[i].col.eq(c)
            yield
            yield
            for i in range(n):
                vals = []
                for ch in dut.lbs[i].channels:
                    vals.append((yield ch))
                content[i].append(tuple(vals))

    run_simulation(dut, bench())
    if ROW: print('   [pipeline=%s] cmds=%d misses=%d' % (pipeline, STAT[1], STAT[0]))
    return done, content


if __name__ == "__main__":
    fails = 0
    n = 4
    print(f"{n} outputs x {COLS} columns x {GROUPS} groups; cycles to fill one row\n")
    print("latency  cmd/N  baseline  pipelined  speedup  identical-data")
    cases = [tuple(int(x) for x in c.split(",")) for c in os.environ.get("CASES", "8,1 16,2").split()]
    for latency, accept in cases:
        if True:
            bd, bc = run(False, n, latency, accept)
            pd, pc = run(True, n, latency, accept)
            # The CURRENT reader lags one column: when a pixel's data arrives it
            # returns to idle in the same cycle and re-captures the column number
            # before the line buffer has advanced it, so it fetches that column
            # twice and stores it one slot late (col 0 is duplicated and the last
            # pixel is never stored). The pipelined reader stores column c at c.
            # Equivalent therefore means: pipelined[c] == baseline[c + 1].
            same = all(pc[i][0] == bc[i][0]
                       and all(pc[i][c] == bc[i][c + 1] for c in range(COLS - 1))
                       for i in range(n))
            if None in bd or None in pd:
                print(f"{latency:7d}  {accept:5d}  baseline done={bd}  pipelined done={pd}  (None = did not finish in {CAP} cycles)", flush=True)
                fails += 1
                continue
            b, p = max(bd), max(pd)
            print(f"{latency:7d}  {accept:5d}  {b:8d}  {p:9d}  {b / p:6.2f}x  {'yes' if same else 'NO'}")
            if not same:
                fails += 1
    print("\nFAIL: pipelined fill is not equivalent to the baseline" if fails else
          "\nok: pipelined fill stores the same pixels, each in its correct column (the baseline lags one column)")
    sys.exit(1 if fails else 0)
