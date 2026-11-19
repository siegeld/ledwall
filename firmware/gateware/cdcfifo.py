"""An asynchronous FIFO whose read side is shallow enough for 125 MHz.

LiteEth's MAC core crosses between the sys domain and the Ethernet clocks
with `stream.ClockDomainCrossing`, which builds migen's `AsyncFIFOBuffered`.
That FIFO's read side runs, in one cycle:

    readable (gray compare) -> re -> consume.q_next_binary (+1)
        -> RAM read address -> LUT-RAM read -> output register

That chain was the E320's 125 MHz critical path on 9 of 24 seeds swept for
gateware 3.0.x. Here the RAM is read at a REGISTERED address (`rbin`), so the
only logic in front of the RAM is the RAM; `readable` and `re` decide only
whether the output register and the pointers load (their clock enables).

Same interface and behaviour as `AsyncFIFOBuffered` -- first word through,
`dout` registered, one cycle of read latency -- so it can stand in for it
unchanged. `fast_cdc()` does that while LiteEth's MAC core is built, and only
then; nothing outside is touched (see SmolEth).
"""
from contextlib import contextmanager
from types import SimpleNamespace

from migen import *
from migen.genlib import fifo as _migen_fifo
from migen.genlib.cdc import MultiReg, GrayCounter
from migen.genlib.fifo import _FIFOInterface


def _gray(value):
    return value ^ value[1:]


class AsyncFIFORegRead(Module, _FIFOInterface):
    """`AsyncFIFOBuffered`, read address from a register. Domains `write` and
    `read`, renamed by the caller."""
    def __init__(self, width, depth):
        _FIFOInterface.__init__(self, width, depth)

        ###

        depth_bits = log2_int(depth, True)

        # Write side: migen's AsyncFIFO verbatim (sys domain, never the
        # critical side).
        produce = ClockDomainsRenamer("write")(GrayCounter(depth_bits + 1))
        self.submodules += produce
        self.comb += produce.ce.eq(self.writable & self.we)

        # Read side. `rbin` is the next entry to FETCH into dout; an entry
        # is free for the writer as soon as it has been fetched, so `rgray`
        # (what the writer sees) follows rbin. `rbin_p1` is always rbin + 1,
        # so the advance is a register copy, not an adder in the path.
        rbin = Signal(depth_bits + 1)
        rbin_p1 = Signal(depth_bits + 1, reset=1)
        rgray = Signal(depth_bits + 1)

        produce_rdomain = Signal(depth_bits + 1)
        produce.q.attr.add("no_retiming")
        rgray.attr.add("no_retiming")
        self.specials += MultiReg(produce.q, produce_rdomain, "read")
        consume_wdomain = Signal(depth_bits + 1)
        self.specials += MultiReg(rgray, consume_wdomain, "write")

        if depth_bits == 1:
            self.comb += self.writable.eq((produce.q[-1] == consume_wdomain[-1])
                | (produce.q[-2] == consume_wdomain[-2]))
        else:
            self.comb += self.writable.eq((produce.q[-1] == consume_wdomain[-1])
                | (produce.q[-2] == consume_wdomain[-2])
                | (produce.q[:-2] != consume_wdomain[:-2]))

        storage = Memory(self.width, depth)
        wrport = storage.get_port(write_capable=True, clock_domain="write")
        rdport = storage.get_port(async_read=True, clock_domain="read")
        self.specials += storage, wrport, rdport
        self.comb += [
            wrport.adr.eq(produce.q_binary[:-1]),
            wrport.dat_w.eq(self.din),
            wrport.we.eq(produce.ce),
            rdport.adr.eq(rbin[:-1]),
        ]

        stored = Signal()           # the RAM holds an entry not yet fetched
        load = Signal()
        self.comb += [
            stored.eq(rgray != produce_rdomain),
            load.eq(stored & (self.re | ~self.readable)),
        ]
        self.sync.read += [
            If(load,
                self.dout.eq(rdport.dat_r),
                self.readable.eq(1),
                rbin.eq(rbin_p1),
                rbin_p1.eq(rbin_p1 + 1),
                rgray.eq(_gray(rbin_p1)),
            ).Elif(self.re,
                self.readable.eq(0),
            ),
        ]


@contextmanager
def fast_cdc():
    """While active, `stream.AsyncFIFO(..., buffered=True)` -- what
    `ClockDomainCrossing` builds -- is an `AsyncFIFORegRead`. Unbuffered
    FIFOs are left as they are."""
    from litex.soc.interconnect import stream
    saved = stream.fifo
    stream.fifo = SimpleNamespace(**{
        name: getattr(_migen_fifo, name) for name in _migen_fifo.__all__})
    stream.fifo.AsyncFIFOBuffered = AsyncFIFORegRead
    try:
        yield
    finally:
        stream.fifo = saved
