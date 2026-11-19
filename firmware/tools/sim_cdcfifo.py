#!/usr/bin/env python3
"""AsyncFIFORegRead (gateware/cdcfifo.py) against random traffic, both ways.

Two unrelated clocks, random write and read stalls, a run with a reader that
never stalls (the MAC TX side's `ready` is mostly 1) and one each way with the
reader much slower and much faster than the writer. Every word must come out
exactly once, in order; `readable` must never show a word twice; and the FIFO
must hold `depth` words when the reader stops (it is not allowed to be
smaller than migen's). Also checked: built inside `fast_cdc()`, a LiteX
`ClockDomainCrossing` really gets this FIFO, and outside it does not.
"""
import os, random, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "gateware"))

from migen import *

from cdcfifo import AsyncFIFORegRead, fast_cdc

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)
        print("  FAIL:", msg)


# migen's simulator applies a generator's writes a cycle after it issues
# them, but everything read in one cycle is a consistent snapshot of that
# cycle -- inputs included. So the drivers below are random and blind, and
# each side RECORDS what the FIFO saw at each edge: a word went in when
# we & writable, came out when re & readable. The check is that the two
# records agree.
def _sim(dut, wclk, rclk, wdrive, rdrive, wcycles, rcycles):
    accepted, got = [], []

    def writer():
        v = 0
        for c in range(wcycles):
            if (yield dut.we) and (yield dut.writable):
                accepted.append((yield dut.din))
            v += 1
            yield dut.din.eq(v)
            yield dut.we.eq(wdrive(c))
            yield

    def reader():
        for c in range(rcycles):
            if (yield dut.re) and (yield dut.readable):
                got.append((yield dut.dout))
            yield dut.re.eq(rdrive(c))
            yield

    run_simulation(dut, {"write": writer(), "read": reader()},
                   clocks={"write": wclk, "read": rclk})
    return accepted, got


def run(name, wclk, rclk, p_write, p_read, depth=32, seed=1, cls=AsyncFIFORegRead):
    rng = random.Random(seed)
    dut = ClockDomainsRenamer({"write": "write", "read": "read"})(cls(16, depth))
    wcycles = 3000
    # Writes stop at 2/3 so the reader can drain everything at the end.
    rcycles = wcycles * wclk // rclk + 400
    accepted, got = _sim(dut, wclk, rclk,
                         lambda c: c < 2 * wcycles // 3 and rng.random() < p_write,
                         lambda c: rng.random() < p_read, wcycles, rcycles)
    ok = got == accepted and len(got) > 300 and len(set(got)) == len(got)
    bad = next((i for i, (a, b) in enumerate(zip(got, accepted)) if a != b), None)
    check(ok, f"{name}: {len(accepted)} in, {len(got)} out, first mismatch at {bad}")
    print(f"  {name}: {len(accepted)} in, {len(got)} out, in order" if ok
          else f"  {name}: WRONG")


def capacity(depth=32, cls=AsyncFIFORegRead):
    """With the reader stopped, how many words go in before writable drops."""
    dut = ClockDomainsRenamer({"write": "write", "read": "read"})(cls(8, depth))
    accepted, _ = _sim(dut, 25, 8, lambda c: 1, lambda c: 0, 80, 300)
    # migen's AsyncFIFOBuffered holds depth + 1 (the RAM and dout).
    check(len(accepted) >= depth, f"capacity {len(accepted)} < depth {depth}")
    print(f"  capacity with the reader stopped: {len(accepted)} (depth {depth})")


def patched():
    from litex.soc.interconnect import stream
    from liteeth.common import eth_phy_description
    with fast_cdc():
        a = stream.ClockDomainCrossing(eth_phy_description(8), "sys", "eth_tx",
                                       depth=32, buffered=True)
    b = stream.ClockDomainCrossing(eth_phy_description(8), "sys", "eth_tx",
                                   depth=32, buffered=True)

    def fifos(m):
        out = []
        for _, sub in m._submodules:
            out.append(type(sub).__name__)
            out += fifos(sub)
        return out
    check("AsyncFIFORegRead" in fifos(a), "fast_cdc did not substitute the FIFO")
    check("AsyncFIFORegRead" not in fifos(b), "fast_cdc leaked out of its block")
    print("  fast_cdc substitutes inside the block only")


if __name__ == "__main__":
    # The harness first, on migen's own FIFO: a test that fails the
    # reference is a broken test, not a broken FIFO.
    from migen.genlib.fifo import AsyncFIFOBuffered
    print("control: migen AsyncFIFOBuffered")
    run("random both sides", 25, 8, 0.7, 0.6, seed=2, cls=AsyncFIFOBuffered)
    run("slow reader", 8, 25, 0.9, 0.5, seed=3, cls=AsyncFIFOBuffered)
    capacity(cls=AsyncFIFOBuffered)
    print("AsyncFIFORegRead")
    run("125 MHz reader always ready", 25, 8, 0.9, 1.0)
    run("random both sides", 25, 8, 0.7, 0.6, seed=2)
    run("slow reader", 8, 25, 0.9, 0.5, seed=3)
    run("fast reader, sparse writes", 25, 7, 0.3, 0.9, seed=4)
    run("near-equal clocks", 8, 9, 0.8, 0.8, seed=5)
    capacity()
    patched()
    print("RESULT:", "PASS" if not FAIL else f"FAIL ({len(FAIL)})")
    sys.exit(1 if FAIL else 0)
