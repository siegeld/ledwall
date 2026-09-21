"""SoC wrapper: an ICND1065 wall as a drop-in for `hub75.Hub75`.

Same constructor shape and the same CSRs, so `colorlight.py` picks one or the
other from the panel preset and everything downstream -- the pixel DMA, the
buffer swap, the firmware's status page -- is unchanged.

`icn1065.py` is the protocol library and knows nothing about LiteDRAM, CSRs or
clock division. This file is the only place the two meet.

WHAT IS AND IS NOT VERIFIED
---------------------------
The protocol, the pixel source, the gamma unit and both arbiters are simulated
(`./build.sh sim-icn1065`) and placed on the real device (`fit-icn1065`). This
wrapper is simulated too, but **no ICND1065 panel has ever been connected to
this board**, so every constant it passes through is transcribed from a
third-party driver rather than captured from a logic analyser. If a panel comes
up dark, distrust `scan_register()` first and `ROW_OE_LEN` second.

THE PART FAMILY MATTERS
-----------------------
This drives `ICND1065L` and `ICND1065S`. It does NOT drive `ICND1065(AP)`,
which numbers its registers differently, nor `DP3364S`, which is a different
vendor's chip entirely. See docs/ICN1065.md -- three supplier specifications
for the same physical module have named three different drivers.
"""

from migen import *
from migen.fhdl.decorators import CEInserter
from litex.soc.interconnect.csr import AutoCSR, CSRField, CSRStatus, CSRStorage

from hub75 import half_words, sdram_offset
from spwm_chips import CHIPS, DEFAULT_CHIP
from icn1065 import (GROUPS_HUB75, GROUPS_HUB320, Icn1065Output, SpwmPrefix,
                     best_pix_bits, framebuffer_pixels,
                     Icn1065PipelinedArbiter, Icn1065PixelSource,
                     Icn1065SharedGamma, frame_clocks)


class Icn1065Display(Module, AutoCSR):
    """N ICND1065 outputs, one arbiter, one framebuffer.

    `clk_div` sets the panel shift clock to `sys_clk / clk_div`, and that is a
    capacity decision rather than a style one: the shift clock is what sets the
    DATA refresh rate, so a wall that cannot be fed at 20 MHz has to be clocked
    slower rather than left to drop frames. `shift_divider()` in icn1065.py
    computes it from the pixel count -- sys/4 for a nine-module wall, sys/2 for
    four modules. See docs/BENCHMARKS.md section 4c.

    Only the OUTPUT stage is divided. The pixel sources and the arbiter run at
    full `sys` speed, because they talk to LiteDRAM and dividing them would cap
    the wall below what its own bandwidth budget allows -- at sys/4 a CE'd
    arbiter could issue at most 10 M reads/s against the 10.8 M/s a nine-module
    wall needs at 36.8 Hz. Just short, and for no benefit.
    """

    def __init__(self, pins_common, pins, sdram, columns=256, rows=128,
                 scan=64, n_outputs=9, clk_div=4, groups=GROUPS_HUB75,
                 gamma=True, chip="icnd1065", pix_bits="auto"):
        chip_desc = (chip if hasattr(chip, "regs")
                     else CHIPS[chip]).with_scan(scan)
        rows_per_frame = scan
        assert rows == rows_per_frame * groups, (
            f"{columns}x{rows} at 1/{scan} scan needs {rows // scan} RGB groups; "
            f"this build has {groups}. A HUB75 connector carries 2, HUB320 4.")
        assert clk_div >= 2, "the panel shift clock is at most sys/2"

        # Pixel width: "auto" picks the WIDEST that fits, because colour is
        # free until the framebuffer runs out. A nine-module wall has room to
        # spare at 32 bits and there is no reason to spend its colour on
        # headroom it will never use. 16-bit is B5 G6 R5, which bands visibly
        # in bright gradients -- gamma 2.8 is steep at the top, so the two
        # highest red codes are 8% of full brightness apart against 1% at 8
        # bits -- so it is a last resort, not a tuning knob.
        if pix_bits == "auto":
            pix_bits = best_pix_bits(columns, scan, n_outputs, groups)
        assert pix_bits in (32, 16), "framebuffer pixel is 32 or 16 bits"
        wall_px = columns * scan * groups * n_outputs
        cap = framebuffer_pixels(pix_bits // 8)
        # Refuse rather than truncate. A wall that does not fit shows the
        # missing part as black and nothing says why.
        assert wall_px <= cap, (
            f"{n_outputs} output(s) of {columns}x{scan * groups} is "
            f"{wall_px:,} px and the framebuffer holds {cap:,} even at "
            f"{pix_bits}-bit pixels. Use fewer outputs per card, or split the "
            f"wall across more cards.")
        print(f"  framebuffer: {pix_bits}-bit pixels, {wall_px:,} of {cap:,} px "
              f"({100 * wall_px // cap}% full)")
        # The board's HUB75 connectors carry six data pins each. HUB320 needs
        # twelve, and the mapping is card-specific -- vendors do not agree on
        # it -- so a HUB320 build needs its own connector table before it can
        # be wired to real pins. The stage itself is parametrised and measured
        # (docs/BENCHMARKS.md section 4d); only the pinout is missing.
        assert groups == GROUPS_HUB75, (
            "this SoC wrapper drives HUB75 connectors (6 data pins). HUB320 "
            "needs a 12-pin-per-connector pin map for the specific card.")

        # ---- CSRs: deliberately the same shape as hub75.Hub75 ----
        self.ctrl = CSRStorage(fields=[
            CSRField("indexed", description="Unused here; the chip takes greyscale"),
            CSRField("enabled", description="Enable the output"),
            CSRField("width", description="Width of the image", size=16),
        ])
        self.fb_base = CSRStorage(fields=[
            CSRField("offset", description="Framebuffer base in 32-bit words", size=20),
        ], reset=sdram_offset)
        self.auto_swap = CSRStorage(1, reset=0,
            description="Flip the framebuffer in gateware at each frame boundary")
        self.swap_req = Signal()
        self.fb_base_now = CSRStatus(20,
            description="Framebuffer half currently displayed (follows auto_swap)")

        fb_base_eff = Signal(20, reset=sdram_offset)
        self.fb_base_eff = fb_base_eff
        # Identical rule to hub75.py: a CSR write always wins, so firmware can
        # place the buffer explicitly at boot or with auto_swap off; otherwise
        # the gateware flips at the frame boundary, one cycle ahead of the first
        # pixel write of the next frame.
        self.sync += [
            If(self.fb_base.re,
                fb_base_eff.eq(self.fb_base.storage),
            ).Elif(self.auto_swap.storage & self.swap_req,
                If(fb_base_eff == sdram_offset,
                    fb_base_eff.eq(sdram_offset + half_words),
                ).Else(
                    fb_base_eff.eq(sdram_offset),
                ),
            ),
        ]
        self.comb += self.fb_base_now.status.eq(fb_base_eff)

        self.hw_columns = CSRStatus(16, reset=columns,
            description="Bitstream pixel columns (read-only)")
        self.hw_rows = CSRStatus(16, reset=rows,
            description="Bitstream pixel rows (read-only)")
        hw_config_val = ((scan & 0xFF) | ((groups & 0xF) << 8)
                         | ((n_outputs & 0xF) << 12))
        self.hw_config = CSRStatus(16, reset=hw_config_val,
            description="scan[7:0], rgb_groups[11:8], n_outputs[15:12]")
        # Tells the firmware which output stage it is talking to, so /api/status
        # can say so without a second build-time constant to keep in sync.
        # Which chip table the bitstream was built with. The CPU can rewrite
        # the table at runtime, so this is the DEFAULT rather than the truth --
        # but it is what an unconfigured card will be running.
        self.hw_chip = CSRStatus(8, reset=sorted(CHIPS).index(chip_desc.name),
            description="Default chip table index (see spwm_chips.CHIPS)")
        self.hw_driver = CSRStatus(8, reset=1,
            description="Output stage: 0 = HUB75 bit-plane, 1 = ICND1065 S-PWM")
        self.hw_clk_div = CSRStatus(8, reset=clk_div,
            description="Panel shift clock divider (sys/N)")
        # Bits per framebuffer pixel, so the firmware and marquee know which
        # format to write without a second constant to keep in sync.
        self.hw_pix_bits = CSRStatus(8, reset=pix_bits,
            description="Framebuffer bits per pixel: 32 (0x00BBGGRR) or 16 (B5G6R5)")
        self.refresh_count = CSRStatus(32, description="Completed frames")

        # Per-output placement, same names and semantics as hub75.Hub75's so the
        # firmware's layout code is unchanged. x and y are in multiples of 16
        # pixels. Defaults tile the outputs left to right, which is what this
        # stage did before the CSRs existed.
        #
        # This is not decoration: it is how a card's outputs map onto the wall,
        # and marquee already serves that mapping in the boot config.
        panel_cfg = []
        for i in range(n_outputs):
            csr = CSRStorage(name=f"panel{i}_0", fields=[
                CSRField("x", size=8, offset=0, reset=(i * columns) // 16,
                         description="x position in multiples of 16"),
                CSRField("y", size=8, offset=8, reset=0,
                         description="y position in multiples of 16"),
                CSRField("rot", size=2, offset=16,
                         description="rotation, not implemented in this stage"),
            ])
            setattr(self, f"panel{i}_0", csr)
            panel_cfg.append(csr)

        # The palette memory the firmware writes for indexed mode.
        #
        # Declared so this stage keeps the same CSR surface as hub75.Hub75 and
        # the firmware compiles against either -- but INDEXED IS NOT IMPLEMENTED
        # here. The chip takes 12-bit greyscale per channel, so an indexed path
        # would have to expand through the palette into the gamma units, and
        # that is not built. A firmware that writes a palette is harmless; one
        # that selects indexed mode will not get indexed output.
        # Exposed as a PLAIN attribute as well as a special: that is how
        # hub75.Hub75 does it and how LiteX finds a memory to publish, and a
        # `self.specials.x = Memory(...)` alone is not picked up.
        # Single bank here, unlike hub75.py: this stage does not implement
        # indexed mode at all, so there is nothing to tear. The CSR exists so
        # one firmware can serve both bitstreams without guessing.
        self.hw_palette_banks = CSRStatus(4, reset=1,
            description="Palette banks in this bitstream (1 = single, shared)")
        palette_mem = Memory(width=32, depth=256, name="palette")
        self.specials += palette_mem
        self.palette_memory = palette_mem

        # ---- runtime chip configuration ----
        #
        # One bitstream drives the whole S-PWM family: the chips differ in a
        # register table and three grammar bits, and both live here where the
        # CPU can rewrite them. Adding a chip becomes a table in the firmware
        # rather than a rebuild.
        #
        # Broadcast to EVERY output's prefix. The outputs run identical command
        # trains in lockstep, so one write configures the whole wall; a
        # per-output table would be N copies of the same thing and N chances
        # for them to disagree.
        self.chip_table = CSRStorage(64, fields=[
            CSRField("addr", size=8, offset=0,
                     description="Table row: 0..MAX_REGS-1 registers, then wrappers"),
            CSRField("r", size=16, offset=16, description="red lane word"),
            CSRField("g", size=16, offset=32, description="green lane word"),
            CSRField("b", size=16, offset=48, description="blue lane word"),
        ], description="Write one row of the chip register table")
        self.chip_cfg = CSRStorage(fields=[
            CSRField("reg_count", size=7, offset=0, reset=len(chip_desc.regs),
                     description="Registers in the rotation"),
            CSRField("slots", size=4, offset=8, reset=chip_desc.slots,
                     description="Register slots per frame prefix"),
            CSRField("rot_slot", size=4, offset=12, reset=chip_desc.rot_slot,
                     description="Which slot carries the rotating register"),
            CSRField("mid_latch", size=1, offset=16,
                     reset=int(chip_desc.mid_latch),
                     description="Send the middle 11-clock LAT burst"),
        ], description="Chip grammar; reset values are the built-in default")

        # # #

        # ---- shift-clock division ----
        #
        # The same shape hub75.py uses: a counter in the sys domain, with the
        # panel CLK emitted as a DATA signal rather than a gated clock. `tick`
        # is one sys cycle per panel bit, and CLK falls at the tick and rises
        # halfway through, so data driven on the tick is stable well before the
        # chip samples it on the rising edge.
        divcnt = Signal(max=clk_div)
        tick = Signal()
        self.sync += If(divcnt == clk_div - 1, divcnt.eq(0)).Else(divcnt.eq(divcnt + 1))
        self.comb += tick.eq(divcnt == 0)
        self.comb += pins_common.clk.eq(divcnt >= (clk_div // 2))

        # ---- the memory side, at full sys speed ----
        port = sdram.crossbar.get_port(mode="read", data_width=32)
        self.submodules.arb = arb = Icn1065PipelinedArbiter(n_outputs)
        self.comb += [
            port.cmd.valid.eq(arb.cmd_valid),
            port.cmd.we.eq(0),
            port.cmd.addr.eq(arb.cmd_adr),
            arb.cmd_ready.eq(port.cmd.ready),
            arb.dat.eq(port.rdata.data),
            arb.dat_valid.eq(port.rdata.valid),
            port.rdata.ready.eq(1),
        ]

        shared = None
        if gamma:
            self.submodules.gamma = g = Icn1065SharedGamma(arb.dat)
            shared = g.channels

        # Framebuffer stride in PIXELS. The DMA writes the wall as one image,
        # so a card's row is the WHOLE wall's width, not one output's. The
        # pixel source turns this into a word address, which differs between
        # the 32-bit and 16-bit formats.
        width_words = columns * n_outputs

        # ONE prefix for the whole board -- see Icn1065Output. It is clock-
        # enabled on the same tick as the outputs, so the command train it
        # emits is in step with the pixel data they emit.
        self.submodules.prefix = prefix = CEInserter()(
            SpwmPrefix(columns, chip=chip_desc))
        self.comb += prefix.ce.eq(tick)

        srcs = []
        starts = []
        for i in range(n_outputs):
            # The output stage is clock-enabled; the source is not.
            out = CEInserter()(Icn1065Output(columns=columns,
                                             rows_per_frame=rows_per_frame,
                                             groups=groups, chip=chip_desc,
                                             prefix=prefix))
            # fb_base is an EXPRESSION, not a constant: it follows the live
            # framebuffer half so the hardware buffer swap reaches the scan-out
            # without the CPU touching anything, and the placement CSR so an
            # operator can move an output's region without a rebuild.
            # Where this output's region starts, from its placement CSR.
            # 16-pixel units, matching hub75.Hub75.
            origin = (fb_base_eff
                      + panel_cfg[i].fields.y * (16 * width_words)
                      + panel_cfg[i].fields.x * 16)
            src = Icn1065PixelSource(columns, rows_per_frame, width_words,
                                     fb_base=origin,
                                     gamma=gamma, shared_gamma=shared,
                                     groups=groups, pix_bits=pix_bits)
            self.submodules += out, src
            srcs.append(src)
            starts.append(out)

            # Fetch EXACTLY ONCE per panel bit-period.
            #
            # `pix_req` is a state of the output's FSM, so with the stage
            # clock-enabled it stays asserted for the whole `clk_div` window,
            # and the source -- which runs at full speed -- would otherwise
            # complete its reads and immediately start again, burning
            # bandwidth and racing the value the output is about to latch.
            # `want` is REGISTERED, and that is a timing fix rather than a
            # style choice. Driving `src.req` straight from `out.pix_req` put a
            # combinational path from the serialiser's bit counter, through the
            # source's `valid` logic, into this latch -- and in the full SoC
            # build that path was the critical one, holding the whole design to
            # 39.68 MHz against a 40 MHz requirement. Sampling the request on
            # the clock-enable costs a cycle out of a window `clk_div` cycles
            # wide, which there is room for, and takes the serialiser out of
            # the path entirely.
            served = Signal()
            want = Signal()
            self.sync += [
                If(out.ce, want.eq(out.pix_req)),
                If(out.ce, served.eq(0)).Elif(src.valid, served.eq(1)),
            ]
            self.comb += [
                out.ce.eq(tick),
                out.start.eq(self.ctrl.fields.enabled),
                src.req.eq(want & ~served),
                src.col.eq(out.pix_col),
                src.row.eq(out.pix_row),
                *[out.pix_in[c].eq(src.channels[c]) for c in range(3 * groups)],
                # r1 g1 b1 r2 g2 b2, in the serialiser's channel order. The
                # connector's names are r0/g0/b0 for the upper half and r1/g1/b1
                # for the lower, which is off by one against the chip's naming
                # and has to be read carefully rather than pattern-matched.
                pins[i].r0.eq(out.rgb[0]),
                pins[i].g0.eq(out.rgb[1]),
                pins[i].b0.eq(out.rgb[2]),
                pins[i].r1.eq(out.rgb[3]),
                pins[i].g1.eq(out.rgb[4]),
                pins[i].b1.eq(out.rgb[5]),
            ]

        # One table and one grammar, for the one prefix.
        for out in starts[:1]:
            self.comb += [
                out.prefix.tbl_adr.eq(self.chip_table.fields.addr),
                out.prefix.tbl_dat.eq(Cat(self.chip_table.fields.r,
                                          self.chip_table.fields.g,
                                          self.chip_table.fields.b)),
                out.prefix.tbl_we.eq(self.chip_table.re),
                out.prefix.cfg_reg_count.eq(self.chip_cfg.fields.reg_count),
                out.prefix.cfg_slots.eq(self.chip_cfg.fields.slots),
                out.prefix.cfg_rot_slot.eq(self.chip_cfg.fields.rot_slot),
                out.prefix.cfg_mid_latch.eq(self.chip_cfg.fields.mid_latch),
            ]

        self.comb += arb.connect(srcs)
        # The shared prefix is started once, by the board rather than by any
        # one output.
        self.comb += prefix.start.eq(starts[0].prefix_req)
        self.comb += [pins_common.lat.eq(starts[0].lat),
                      pins_common.oe.eq(starts[0].oe),
                      # The ICND1065 has no row address lines: OE steps a
                      # counter inside the chip and VSYNC resets it. The pins
                      # exist on the connector, so they are driven low rather
                      # than left floating.
                      pins_common.row.eq(0)]

        # One frame_done is enough: the outputs are started together and run
        # identical sequences, so they finish in lockstep.
        self.comb += self.swap_req.eq(starts[0].frame_done)
        self.sync += If(starts[0].frame_done,
                        self.refresh_count.status.eq(self.refresh_count.status + 1))

    @staticmethod
    def frame_clocks(columns, scan):
        return frame_clocks(columns, scan)
