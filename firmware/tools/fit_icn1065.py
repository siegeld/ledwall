#!/usr/bin/env python3
"""Synthesise N ICN1065 output stages for the ECP5 and report what they cost.

The SoC is not touched: this builds the output stage ALONE against the real
device, so the question "does a 3x3 wall of P1.25 modules fit alongside
everything else" can be answered before committing to a rewrite of hub75.py.

    ./build.sh fit-icn1065            # 9 outputs, 256x128, 1/64 scan
"""
import sys, os, argparse
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateware"))

from functools import reduce

from migen import *
from litex.build.lattice import LatticePlatform
from litex.build.generic_platform import Pins, IOStandard, Subsignal
from icn1065 import (Icn1065Output, Icn1065PixelSource, frame_clocks,
                     ROW_OE_LEN, SPWM_BITS, sdram_bytes_per_second,
                     sustainable_refresh, shift_divider,
                     Icn1065PipelinedArbiter, Icn1065SharedGamma,
                     GROUPS_HUB75, GROUPS_HUB320)


class FakeMemory(Module):
    """A split-transaction stand-in for LiteDRAM that cannot be constant-folded.

    This exists because of a measurement bug worth remembering. The first
    version left the data bus undriven and tied the ack to bit 0 of it. An
    undriven signal is a constant 0, so the ack never arrived, every pixel
    source stalled in its read state forever, and every signal downstream of
    the read became a constant. Yosys duly deleted the entire data path and the
    tool reported LESS logic for nine full read-paths than for nine bare
    protocol engines -- a number that looked like good news and was in fact the
    measurement destroying the thing it was measuring.

    An LFSR fixes it honestly: the data is real registered state that depends
    on itself, so nothing downstream can be folded to a constant, and the
    returning valid arrives on a pattern the synthesiser cannot predict.
    """
    COST_FF = 32 + 4

    def __init__(self, seed=0x1234_5678):
        self.dat = Signal(32, reset=seed)
        self.dat_valid = Signal()
        self.cmd_valid = Signal()
        self.cmd_ready = Signal()

        fb = self.dat[31] ^ self.dat[21] ^ self.dat[1] ^ self.dat[0]
        self.sync += self.dat.eq(Cat(fb, self.dat[:31]))
        # Variable acceptance and a variable return, which is a better model of
        # a real memory controller than a fixed pipeline and, more to the point,
        # is something synthesis cannot predict away.
        delay = Signal(4)
        self.comb += self.cmd_ready.eq(self.dat[3])
        self.sync += delay.eq(Cat(self.cmd_valid & self.cmd_ready, delay[:3]))
        self.comb += self.dat_valid.eq(delay[3])


class Harness(Module):
    """The whole output stage: N outputs, N sources, one arbiter, one gamma set.

    This is the shape the SoC would instantiate, not a collection of parts --
    which matters, because the two things that dominate the cost only appear
    once the parts are connected. The arbiter is what stops memory latency
    becoming a throughput limit, and sharing one data bus is what lets three
    gamma converters serve nine outputs instead of twenty-seven.
    """

    def __init__(self, n_outputs, columns, rows, gamma=True,
                 groups=GROUPS_HUB75):
        self.lat = Signal()
        self.oe = Signal()
        self.rgb = [Signal(3 * groups) for _ in range(n_outputs)]
        self.start = Signal()

        self.submodules.mem = mem = FakeMemory()
        self.submodules.arb = arb = Icn1065PipelinedArbiter(n_outputs)

        shared = None
        if gamma:
            self.submodules.gamma = g = Icn1065SharedGamma(arb.dat)
            shared = g.channels

        srcs = []
        for i in range(n_outputs):
            out = Icn1065Output(columns=columns, rows_per_frame=rows,
                                groups=groups)
            src = Icn1065PixelSource(columns, rows, width=columns * 3,
                                     fb_base=i * columns * rows * 2,
                                     gamma=gamma, shared_gamma=shared,
                                     groups=groups)
            srcs.append(src)
            self.submodules += out, src
            self.comb += [
                out.start.eq(self.start),
                self.rgb[i].eq(out.rgb),
                src.req.eq(out.pix_req),
                src.col.eq(out.pix_col),
                src.row.eq(out.pix_row),
                *[out.pix_in[c].eq(src.channels[c])
                  for c in range(3 * groups)],
            ]
            if i == 0:
                self.comb += [self.lat.eq(out.lat), self.oe.eq(out.oe)]

        self.comb += arb.connect(srcs)
        self.comb += [
            arb.dat.eq(mem.dat),
            arb.dat_valid.eq(mem.dat_valid),
            arb.cmd_ready.eq(mem.cmd_ready),
            mem.cmd_valid.eq(arb.cmd_valid),
        ]
        # The command address must reach a pin or nextpnr trims the address
        # path -- the same class of mistake as the undriven data bus above.
        self.probe = Signal()
        self.comb += self.probe.eq(arb.cmd_valid ^ arb.cmd_adr[0] ^ arb.grant[0])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outputs", type=int, default=9)
    ap.add_argument("--columns", type=int, default=256)
    ap.add_argument("--rows", type=int, default=64)
    ap.add_argument("--sys-clk", type=float, default=40e6)
    ap.add_argument("--groups", type=int, default=GROUPS_HUB75,
                    choices=(GROUPS_HUB75, GROUPS_HUB320),
                    help="RGB groups per connector: 2 = HUB75, 4 = HUB320")
    ap.add_argument("--no-gamma", action="store_true",
                    help="linear 8->12 expansion, to price the gamma units")
    args = ap.parse_args()

    io = [("clk", 0, Pins("P3"), IOStandard("LVCMOS33")),
          ("lat", 0, Pins("N1"), IOStandard("LVCMOS33")),
          ("oe", 0, Pins("M4"), IOStandard("LVCMOS33"))]
    # The REAL data pins, read out of the board's own connector table rather
    # than invented -- an earlier version made up names and nextpnr rejected
    # "T1", which does not exist on CABGA256.
    conn = [
        "C4 D4 E4 D3 E3 F4", "F3 F5 G3 G4 H3 H4", "G5 H5 J5 J4 B1 C2",
        "C1 D1 E2 E1 F2 F1", "G2 G1 H2 K5 K4 L3", "L4 L5 P2 R2 T2 R3",
        "T3 R4 M5 P5 N6 N7", "P7 M7 P8 R8 M8 M9", "P11 N11 M11 T13 R12 R13",
        "R14 T14 D16 C15 C16 B16", "B15 C14 T15 P15 R15 P12",
        "P13 N12 N13 M12 P14 N14", "H15 H14 G16 F16 G15 F15",
        "E15 E16 L12 L13 M14 L14", "J13 K13 J12 H13 H12 G12",
        "G14 G13 F12 F13 F14 E14",
    ]
    # HUB320 carries four RGB groups on one connector, so a fit needs twelve
    # data pins per output rather than six. The board's table is six pins per
    # HUB75 connector, so pairs are concatenated to make twelve.
    #
    # This is a REAL pin count and a real placement, but it is NOT the real
    # HUB320 pinout -- that is card-specific and no HUB320 card has been
    # chosen. What this measures is logic, routing and IO pressure, which is
    # the question being asked. The mapping is a config table, written once the
    # card exists.
    per_output = 3 * args.groups
    pins = " ".join(conn).split()
    needed = args.outputs * per_output
    assert needed <= len(pins), (f"{needed} data pins needed, board table has "
                                 f"{len(pins)}")
    for i in range(args.outputs):
        group = " ".join(pins[i * per_output:(i + 1) * per_output])
        io.append((f"rgb{i}", 0, Pins(group), IOStandard("LVCMOS33")))

    plat = LatticePlatform("LFE5U-25F-6BG256C", io, [], toolchain="trellis")
    dut = Harness(args.outputs, args.columns, args.rows,
                  gamma=not args.no_gamma, groups=args.groups)
    clk = plat.request("clk")
    dut.clock_domains.cd_sys = ClockDomain()
    dut.comb += dut.cd_sys.clk.eq(clk)
    dut.comb += [plat.request("lat").eq(dut.lat),
                 plat.request("oe").eq(dut.oe | dut.probe)]
    for i in range(args.outputs):
        dut.comb += plat.request(f"rgb{i}").eq(dut.rgb[i])
    plat.add_period_constraint(clk, 1e9 / args.sys_clk)

    print(f"(the {FakeMemory.COST_FF}-FF LFSR standing in for LiteDRAM is "
          f"included in the reported totals)")
    print(f"gamma: {'OFF (linear)' if args.no_gamma else 'ON (piecewise 2.8)'}")
    wiring = "HUB320" if args.groups == GROUPS_HUB320 else "HUB75"
    print(f"Building {args.outputs} x Icn1065Output, "
          f"{args.columns}x{args.rows * args.groups} at 1/{args.rows} scan, "
          f"{wiring} ({args.groups} RGB groups, {per_output} data pins each)\n")
    plat.build(dut, build_name="fit_icn1065", run=True)

    clocks = frame_clocks(args.columns, args.rows)
    # The panel shift clock is sys_clk/2, as in hub75.py.
    shift_hz = args.sys_clk / 2
    fps = shift_hz / clocks
    px = args.columns * args.rows * args.groups * args.outputs
    print(f"\n--- timing model ---")
    print(f"  clocks per frame, per output : {clocks:,}")
    print(f"  shift clock                  : {shift_hz/1e6:.1f} MHz (sys/2)")
    print(f"  data refresh                 : {fps:.1f} Hz")
    print(f"  wall                         : {px:,} px across {args.outputs} outputs")
    print(f"  framebuffer reads            : {px*fps/1e6:.1f} Mpx/s "
          f"= {sdram_bytes_per_second(args.columns, args.rows, args.outputs, fps, groups=args.groups)/1e6:.0f} MB/s")
    peak = args.sys_clk * 2 / 1e6
    print(f"  SDRAM peak (16-bit SDR)      : {peak:.0f} MB/s")
    for target in (60, 30):
        mb = sdram_bytes_per_second(args.columns, args.rows, args.outputs,
                                    target, groups=args.groups)/1e6
        print(f"    at {target:>2} Hz data refresh      : {mb:5.1f} MB/s"
              f"  {'OVER' if mb > peak*0.6 else 'ok'}")

    # What to actually build. The shift clock sets the data refresh, so a
    # bandwidth limit is a statement about how fast the panel may be clocked --
    # this turns "about 30 Hz" into a divider someone can put in a PLL.
    print(f"\n--- what to clock it at ---")
    for eff in (0.8, 0.6, 0.5):
        div, shift, refresh = shift_divider(args.columns, args.rows,
                                            args.outputs, args.sys_clk, eff,
                                            groups=args.groups)
        print(f"  at {eff:.0%} SDRAM efficiency : sys/{div} = {shift/1e6:5.2f} MHz"
              f"  ->  {refresh:5.1f} Hz data refresh")
    print(f"  (the efficiency figure is the least trustworthy number here --")
    print(f"   refresh, precharge, row misses and the pixel DMA's own writes)")


if __name__ == "__main__":
    main()
