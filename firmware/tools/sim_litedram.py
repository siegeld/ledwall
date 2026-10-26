#!/usr/bin/env python3
"""Row-fill rate of the ICN1065 readers against the REAL LiteDRAM controller.

tools/sim_fillrate.py models the SDRAM as a latency and a row-miss cost. This
one instantiates LiteDRAMCore with SDRAMPHYModel for the E320's memory (two
M12L16161A, 32 bits, 1:1) and the actual reader, line buffers and arbiter, and
reports how many sys clocks four outputs take to fill one row. On the P1.25 the
budget is columns * 16 * clk_div = 8192 clocks at clk_div 4.

USE THE MODULE'S REAL GEOMETRY. The defaults are the P1.25: group strips at
(128,0) (128,64) (0,0) (0,64), so groups 0 and 2 share a DRAM row. An earlier
version of this sim assumed (0,0) (128,0) (0,64) (128,64), predicted block fetch
2.4x faster, and the hardware was SLOWER -- because the build had paired 0 with
1. With the real offsets the sim reproduced the hardware (6256 vs 6784 clocks
for the mis-paired build, 6255 vs 5876 for the pipelined one).

Run inside the build image, from the repo root:
    docker run --rm -v $PWD:/w -w /w litex-hub75 python3 tools/sim_litedram.py
Environment: MODE=base|pipe|block  K=<block>  PAIRS=a,b,c,d  OFFS=g0,g1,g2,g3
BASES=o0,o1,o2,o3  WR=<words per write burst>  WGAP=<clocks>  CPUGAP=<clocks>
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "gateware"))
from migen import *
from migen.sim import run_simulation
from litedram.modules import M12L16161A
from litedram.phy.model import SDRAMPHYModel
from litedram.core import LiteDRAMCore
from litedram.common import *
from litedram.core.controller import ControllerSettings
from litex.tools.litex_sim import get_sdram_phy_settings
from icn1065 import (Icn1065PixelSource, Icn1065LineBuffer, Icn1065PipelinedArbiter)

COLS = int(os.environ.get('COLS', '128')); ROWS = 128; WIDTH = 256; GROUPS = 4
OFFS = [int(x) for x in os.environ.get('OFFS', '128,%d,0,%d' % (64*WIDTH+128, 64*WIDTH)).split(',')]
BASES = [int(x) for x in os.environ.get('BASES', '327680,360448,393216,425984').split(',')]
N = int(os.environ.get('N', '4'))
MODE = os.environ.get('MODE', 'block')   # base | pipe | block
K = int(os.environ.get('K', '4'))
_p = [int(x) for x in os.environ.get('PAIRS', '0,2,1,3').split(',')]
PAIRS = ((_p[0], _p[1]), (_p[2], _p[3]))
CLK = float(os.environ.get('CLK', '35e6'))
AMAP = os.environ.get('AMAP','ROW_BANK_COL')
WR = int(os.environ.get('WR','0'))   # 0 = no writer; else words per chunk
WGAP = int(os.environ.get('WGAP','1500'))
WBASE = int(os.environ.get('WBASE','655360'))
CPUGAP = int(os.environ.get('CPUGAP','0'))   # 0 = no CPU; else one 8-word refill per CPUGAP clocks
CAP = int(os.environ.get('CAP', '400000'))

class DUT(Module):
    def __init__(self):
        mod = M12L16161A(CLK, "1:1")
        st = get_sdram_phy_settings(memtype="SDR", data_width=32, clk_freq=CLK)
        self.submodules.phy = phy = SDRAMPHYModel(mod, st, clk_freq=CLK, we_granularity=0, address_mapping=AMAP)
        self.submodules.core = core = LiteDRAMCore(phy, mod.geom_settings, mod.timing_settings, CLK, controller_settings=ControllerSettings(address_mapping=AMAP, bank_byte_alignment=int(os.environ.get('BBA','0'),0)))
        core.dfii._control.storage.reset = Constant(0b1110, 4)   # hardware control, cke/odt/reset_n up
        port = core.crossbar.get_port(mode="read", data_width=32)
        pipe = MODE != 'base'; blk = K if MODE == 'block' else 0
        self.srcs, self.lbs = [], []
        for i in range(N):
            src = Icn1065PixelSource(COLS, ROWS, WIDTH, fb_base=BASES[i], gamma=False, groups=GROUPS,
                                     group_offsets=OFFS, pix_bits=32, pipeline=pipe, block=blk, block_pairs=PAIRS)
            lb = Icn1065LineBuffer(COLS, ROWS, groups=GROUPS, pipeline=pipe, block=blk)
            self.submodules += src, lb
            self.comb += [src.req.eq(lb.src_req), src.col.eq(lb.src_col), src.row.eq(lb.src_row),
                          lb.src_valid.eq(src.valid),
                          *[lb.src_channels[c].eq(src.channels[c]) for c in range(3*GROUPS)]]
            if pipe: self.comb += lb.src_req_ack.eq(src.req_ack)
            self.srcs.append(src); self.lbs.append(lb)
        if pipe:
            self.submodules.arb = arb = Icn1065PipelinedArbiter(
                N, per_source=(4*blk if blk else 2*GROUPS), sticky=bool(blk))
        else:
            self.submodules.arb = arb = Icn1065PipelinedArbiter(N, per_source=GROUPS)
        self.comb += arb.connect(self.srcs)
        self.comb += [port.cmd.valid.eq(arb.cmd_valid), port.cmd.we.eq(0), port.cmd.addr.eq(arb.cmd_adr),
                      arb.cmd_ready.eq(port.cmd.ready), port.rdata.ready.eq(1),
                      arb.dat.eq(port.rdata.data), arb.dat_valid.eq(port.rdata.valid)]
        self.cmds = 0
        if CPUGAP:
            cp = core.crossbar.get_port(mode="read", data_width=32)
            cc = Signal(16); left = Signal(4); cadr = Signal(24)
            lfsr = Signal(16, reset=0xACE1)
            self.sync += [
                lfsr.eq(Cat(lfsr[1:], lfsr[0] ^ lfsr[2] ^ lfsr[3] ^ lfsr[5])),
                cc.eq(Mux(cc == CPUGAP - 1, 0, cc + 1)),
                If((cc == 0) & (left == 0), left.eq(8), cadr.eq(Cat(Constant(0, 3), lfsr[0:12], Constant(0b11, 2)))),
                If(cp.cmd.valid & cp.cmd.ready, left.eq(left - 1), cadr.eq(cadr + 1)),
            ]
            self.comb += [cp.cmd.valid.eq(left != 0), cp.cmd.we.eq(0), cp.cmd.addr.eq(cadr), cp.rdata.ready.eq(1)]
        if WR:
            from litedram.frontend.dma import LiteDRAMDMAWriter
            wp = core.crossbar.get_port(mode="write", data_width=32)
            self.submodules.wr = wr = LiteDRAMDMAWriter(wp, fifo_depth=16)
            cnt = Signal(32); adr = Signal(24, reset=WBASE); left = Signal(16)
            self.sync += [
                cnt.eq(Mux(cnt == WR + WGAP, 0, cnt + 1)),
                If(cnt == 0, left.eq(WR)),
                If(wr.sink.valid & wr.sink.ready, left.eq(left - 1), adr.eq(Mux(adr == WBASE + 65535, WBASE, adr + 1))),
            ]
            self.comb += [wr.sink.valid.eq(left != 0), wr.sink.address.eq(adr), wr.sink.data.eq(adr)]

def main():
    dut = DUT()
    done = [None]*N; seen = [0]*N
    def bench():
        cmds = 0
        for cyc in range(CAP):
            if (yield dut.arb.cmd_valid) and (yield dut.arb.cmd_ready): cmds += 1
            for i, s in enumerate(dut.srcs):
                if (yield s.valid):
                    seen[i] += 1
                    if seen[i] == COLS and done[i] is None: done[i] = cyc
            if all(d is not None for d in done): break
            yield
        print(f"MODE={MODE} K={K} N={N} COLS={COLS}: done={done} cmds={cmds}")
        if None not in done:
            tot = N*COLS*GROUPS
            print(f"  cycles={max(done)}  words/clk={tot/max(done):.3f}")
    run_simulation(dut, bench())
main()
