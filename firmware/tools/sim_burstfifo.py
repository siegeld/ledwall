#!/usr/bin/env python3
"""How many back-to-back packets does burst_fifo actually absorb?

The whole residual-loss argument rests on a number nobody has computed from
the real modules: the gate drops a packet the moment `burst_fifo` refuses a
beat, and the arithmetic-by-hand said 2048 beats should never fill at our
packet rate. That estimate used a hand-waved drain rate. This runs the actual
chain -- AlwaysReady -> SyncFIFO -> Bytes32to8 -> an always-ready consumer --
and reports the peak level and the drop count.

Fill rate: at 1 Gbps the PHY delivers 125 MB/s = 31.25M words/s against a
35 MHz system clock, so the 32-bit stream runs at about 0.89 words per cycle
during a packet. Drain: Bytes32to8 has `sink.ready.eq(~loaded)`, one word per
four byte-emissions, so 8.75M words/s. Net fill is therefore ~0.64 words per
cycle while a packet is on the wire.

./build.sh sim-burstfifo
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateware"))

from migen import *
from litex.soc.interconnect import stream
from smoleth import AlwaysReady, Bytes32to8
from liteeth.common import eth_phy_description

WORDS_PER_PKT = 376          # 1503 bytes of Ethernet/IP/UDP + 1461 payload
FILL_DUTY = 8                # present valid 8 cycles in 9 -> ~0.89 words/cycle


class Chain(Module):
    def __init__(self, depth):
        self.submodules.gate = gate = AlwaysReady(eth_phy_description(32))
        self.submodules.fifo = fifo = stream.SyncFIFO(
            eth_phy_description(32), depth, buffered=True)
        self.submodules.narrow = narrow = Bytes32to8()
        self.comb += gate.source.connect(fifo.sink)
        self.comb += fifo.source.connect(narrow.sink)
        self.comb += narrow.source.ready.eq(1)      # downstream ~97% idle
        self.sink = gate.sink
        self.dropped = gate.dropped
        self.level = fifo.level


def run(depth, n_pkts, gap_cycles):
    dut = Chain(depth)
    peak = {"level": 0, "dropped": 0}

    def gen():
        tick = 0
        for _ in range(n_pkts):
            sent = 0
            while sent < WORDS_PER_PKT:
                # Duty-cycle the wire to model 125 MB/s into a 35 MHz domain.
                present = (tick % 9) < FILL_DUTY
                yield dut.sink.valid.eq(1 if present else 0)
                yield dut.sink.data.eq(sent)
                yield dut.sink.last.eq(1 if sent == WORDS_PER_PKT - 1 else 0)
                yield dut.sink.last_be.eq(0b1000 if sent == WORDS_PER_PKT - 1 else 0)
                yield
                tick += 1
                if present and (yield dut.sink.ready):
                    sent += 1
                lv = (yield dut.level)
                if lv > peak["level"]:
                    peak["level"] = lv
            yield dut.sink.valid.eq(0)
            for _ in range(gap_cycles):
                yield
                tick += 1
                lv = (yield dut.level)
                if lv > peak["level"]:
                    peak["level"] = lv
        for _ in range(200):
            yield
        peak["dropped"] = (yield dut.dropped)

    run_simulation(dut, gen())
    return peak


def main():
    print(__doc__.strip().splitlines()[0])
    print()
    fails = 0
    # Back-to-back is the worst case the sender can produce after the pacing
    # clamp: a hiccup now costs ONE close gap (measured, tcpdump, longest run
    # of sub-100us gaps = 1), so 2 consecutive is the realistic bound.
    for depth in (2048, 4096):
        print(f"burst_fifo depth {depth}:")
        for n, gap, label in ((1, 0, "1 packet"),
                              (2, 0, "2 back-to-back"),
                              (4, 0, "4 back-to-back"),
                              (8, 0, "8 back-to-back"),
                              (16, 0, "16 back-to-back")):
            r = run(depth, n, gap)
            verdict = "DROPS" if r["dropped"] else "clean"
            pct = 100.0 * r["level"] / depth
            print(f"  {label:18s} peak {r['level']:5d}/{depth} ({pct:5.1f}%)  "
                  f"dropped {r['dropped']}  {verdict}")
        print()
    print("A realistic stream after the clamp presents at most 2 consecutive")
    print("packets with no gap. Compare that row against the peak.")
    return fails


if __name__ == "__main__":
    sys.exit(main())
