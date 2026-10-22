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

from hub75 import RGB_ORDERS, half_words, sdram_offset
from spwm_chips import CHIPS, DEFAULT_CHIP
# Connector signal names, in serialiser channel order. The first six are what
# a HUB75 connector carries; all twelve are what HUB320 carries. Indexing this
# rather than unrolling is what makes a 4-group build drive all of its pins.
RGB_PINS = ["r0", "g0", "b0", "r1", "g1", "b1",
            "r2", "g2", "b2", "r3", "g3", "b3"]

from icn1065 import (Icn1065PaletteStage, GROUPS_HUB75, GROUPS_HUB320,
                     Icn1065Output, SpwmPrefix, GREY_BITS,
                     best_pix_bits, framebuffer_pixels,
                     Icn1065PipelinedArbiter, Icn1065PixelSource,
                     Icn1065SharedGamma, frame_clocks,
                     Icn1065LineBuffer)


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
                 gamma=True, chip="icnd1065", pix_bits="auto",
                 module_w=None, module_h=None, group_origins=None):
        chip_desc = (chip if hasattr(chip, "regs")
                     else CHIPS[chip]).with_scan(scan)
        rows_per_frame = scan
        assert rows == rows_per_frame * groups, (
            f"{columns}x{rows} at 1/{scan} scan needs {rows // scan} RGB groups; "
            f"this build has {groups}. A HUB75 connector carries 2, HUB320 4.")
        assert clk_div >= 2, "the panel shift clock is at most sys/2"

        # THE MODULE'S PHYSICAL SHAPE, which is not the same thing as
        # `columns x rows`. `columns` is one RGB group's chain length and
        # `rows` is `scan * groups`: the framebuffer area one output needs.
        # HUB75 stacks its groups vertically, so the module IS columns x rows
        # and the defaults below say so. The bench P1.25 module does not: its
        # four 128x64 groups sit in a 2x2, so the glass is 256 x 128 while
        # columns x rows is 128 x 256. Same pixel count, different shape --
        # and if the framebuffer has the wrong shape then every raw write to
        # it (marquee's stream, the test screen) lands scrambled and only a
        # software remap can hide it, which is exactly what happened
        # (docs/E320-BRINGUP.md 4.3, docs/PANELS.md).
        #
        # `module_w x module_h` is the glass. `group_origins[g]` is where
        # group g's `columns x scan` strip starts inside it, in pixels. Both
        # come from the panel preset as defaults and the origins are also
        # CSRs, so a module wired differently is a line in its layout config
        # rather than a bitstream.
        module_w = columns if module_w is None else module_w
        module_h = rows if module_h is None else module_h
        assert module_w * module_h == columns * rows, (
            f"module {module_w}x{module_h} is not {columns}x{rows} pixels "
            f"rearranged ({module_w * module_h} vs {columns * rows})")
        assert module_w % columns == 0 and module_h % scan == 0, (
            f"module {module_w}x{module_h} is not a whole number of "
            f"{columns}x{scan} group strips")
        if group_origins is None:
            group_origins = [(0, g * scan) for g in range(groups)]
        assert len(group_origins) == groups, (
            f"{len(group_origins)} group origins for {groups} groups")
        for g, (gx, gy) in enumerate(group_origins):
            assert gx % 16 == 0 and gy % 16 == 0, (
                f"group {g} origin ({gx},{gy}) must be a multiple of 16")
            assert gx + columns <= module_w and gy + scan <= module_h, (
                f"group {g} origin ({gx},{gy}) puts its {columns}x{scan} "
                f"strip outside the {module_w}x{module_h} module")
        self.module_w, self.module_h = module_w, module_h
        print(f"  module: {module_w}x{module_h}, {groups} groups of "
              f"{columns}x{scan} at {group_origins}")

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
        # HUB320 is now wired as well as HUB75. It needs twelve data pins per
        # connector against HUB75's six, and the mapping is card-specific --
        # vendors do not agree on it -- so it needs a connector table for the
        # specific card. The E320's was measured with `gateware/pinwalk.py`
        # (docs/PINWALK.md) and lives in `gateware/colorlight_e320.py`; use
        # `helper.hub320_conn()` to request those pins. Any other HUB320 card
        # needs its own table before this will drive it correctly.
        assert groups in (GROUPS_HUB75, GROUPS_HUB320), (
            f"groups must be {GROUPS_HUB75} (HUB75) or {GROUPS_HUB320} "
            f"(HUB320), not {groups}")
        for i, p in enumerate(pins):
            missing = [n for n in RGB_PINS[:3 * groups] if not hasattr(p, n)]
            assert not missing, (
                f"connector {i} is missing {missing} -- a {groups}-group build "
                f"needs {3 * groups} data pins per connector. A HUB75 "
                f"connector carries 6; use helper.hub320_conn() for 12.")

        # ---- CSRs: deliberately the same shape as hub75.Hub75 ----
        self.ctrl = CSRStorage(fields=[
            CSRField("indexed", description="Pixels are palette indices (low byte), looked up before gamma"),
            CSRField("enabled", description="Enable the output"),
            # THE FRAMEBUFFER STRIDE, in pixels. Until gateware 1.20.0 this field
            # was decorative on the S-PWM stage: the stride was the bitstream
            # constant module_w * n_outputs, so a layout narrower than the full
            # card (one module on a four-output bitstream) folded every row and
            # squeezed the picture into the top quarter of the glass
            # (E320-BRINGUP 4.2, bitten again 2026-10-19). Now read, as
            # hub75.py always did. Reset to the full-card stride so an
            # unconfigured card still shows its boot image whole.
            CSRField("width", description="Framebuffer stride in pixels (the layout's virtual width)",
                     size=16, reset=module_w * n_outputs),
        ])
        self.fb_base = CSRStorage(fields=[
            CSRField("offset", description="Framebuffer base in 32-bit words", size=20),
        ], reset=sdram_offset)
        # Module channel order, exactly as the plain HUB75 stage has had it.
        #
        # This stage had no such CSR, which meant two things: a P1.25 panel wired
        # anything other than plain RGB could not be corrected from config at all,
        # and the FIRMWARE would not compile with --features spwm because
        # layout.rs writes this register unconditionally. So the S-PWM variant was
        # unbuildable and the gap invisible.
        #
        # Indexes RGB_ORDERS, imported from hub75.py rather than re-listed, so the
        # gateware, the other stage and the firmware's copy in layout.rs cannot
        # disagree about what value 3 means.
        self.rgb_order = CSRStorage(3, reset=0,
            description="Module channel order, indexes RGB_ORDERS "
                        "(0=RGB, 1=RBG, 2=GRB, 3=GBR, 4=BRG, 5=BGR)")
        self.auto_swap = CSRStorage(1, reset=0,
            description="Flip the framebuffer in gateware at each frame boundary")
        self.swap_req = Signal()
        self.fb_base_now = CSRStatus(20,
            description="Framebuffer half currently displayed (follows auto_swap)")

        # SNOW, COUNTED.
        #
        # A white dot is a scan-out UNDERRUN: the serialiser reaches the moment
        # it must latch a pixel and the memory read for it has not come back,
        # so it shifts whatever was on the bus. Nothing downstream records it --
        # the framebuffer is correct, every DMA counter is clean, and a
        # read-back shows nothing wrong -- so until now the only instrument was
        # somebody looking at the panel and saying "some snow". That is not
        # good enough to tune against, and it sent this work down two wrong
        # paths (display passes, and the buffer flip) before the real cause
        # turned out to be the pixel DMA's write bursts stealing read
        # bandwidth.
        #
        # This is the one place that knows. `served` is cleared when a request
        # is issued and set when its data lands, so a request being issued
        # while the previous one is still unserved IS the dropped pixel. Data
        # arriving on the same tick is in time and does not count -- that tie
        # is resolved in favour of the arrival where `served` is assigned.
        self.underruns = CSRStatus(32, description=(
            "Pixels the serialiser latched before their memory read returned. "
            "Every one is a wrong pixel on the glass. Zero is the target."))

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

        # The MODULE as the firmware and marquee describe it (panel_width x
        # panel_height in the layout config), not one group's chain length:
        # what one output drives, as the glass is shaped. Read-only.
        self.hw_columns = CSRStatus(16, reset=module_w,
            description="Module width in pixels, as one output drives it (read-only)")
        self.hw_rows = CSRStatus(16, reset=module_h,
            description="Module height in pixels, as one output drives it (read-only)")
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
                CSRField("x", size=8, offset=0, reset=(i * module_w) // 16,
                         description="x position in multiples of 16"),
                CSRField("y", size=8, offset=8, reset=0,
                         description="y position in multiples of 16"),
                CSRField("rot", size=2, offset=16,
                         description="rotation, not implemented in this stage"),
            ])
            setattr(self, f"panel{i}_0", csr)
            panel_cfg.append(csr)

        # Where each RGB GROUP's strip starts inside the module, in 16-pixel
        # units like the panel placement above. Reset to the preset's
        # `group_origins`; the firmware rewrites them from the layout config's
        # `group_origins:` line when a module is wired differently. Shared by
        # every output, because they drive the same kind of module.
        group_cfg = []
        for g, (gx, gy) in enumerate(group_origins):
            csr = CSRStorage(name=f"group{g}_origin", fields=[
                CSRField("x", size=8, offset=0, reset=gx // 16,
                         description="group strip x origin in the module, multiples of 16"),
                CSRField("y", size=8, offset=8, reset=gy // 16,
                         description="group strip y origin in the module, multiples of 16"),
            ])
            setattr(self, f"group{g}_origin", csr)
            group_cfg.append(csr)

        # The palette memory the firmware writes for indexed mode.
        #
        # Same CSR surface as hub75.Hub75, so one firmware serves both
        # bitstreams. INDEXED MODE IS IMPLEMENTED since gateware 1.19.0: an
        # `Icn1065PaletteStage` between the SDRAM port and the arbiter replaces
        # each returning word by its palette entry when `ctrl.indexed` is set,
        # before the shared gamma converters. Until then this memory was
        # declared and never read, and an on-device program -- which renders
        # palette indices -- came out as red intensities (glass, 2026-10-19).
        # Exposed as a PLAIN attribute as well as a special: that is how
        # hub75.Hub75 does it and how LiteX finds a memory to publish, and a
        # `self.specials.x = Memory(...)` alone is not picked up.
        # TWO banks of 256, like hub75.py (RB-5516): the live bank follows the
        # live framebuffer half, so a program's palette rewrite lands with the
        # frame it belongs to rather than tearing the one on screen.
        self.hw_palette_banks = CSRStatus(4, reset=2,
            description="Palette banks in this bitstream (2 = flips with the framebuffer)")
        palette_mem = Memory(width=32, depth=512, name="palette")
        self.specials += palette_mem
        self.palette_memory = palette_mem
        palette_bank = Signal()
        self.comb += palette_bank.eq(fb_base_eff != sdram_offset)

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
            CSRField("chan_rev", size=1, offset=7,
                     reset=int(getattr(chip_desc, "chan_rev", 0)),
                     description="Send a line channel 15->0 (11.2) not 0->15"),
            CSRField("slots", size=4, offset=8, reset=chip_desc.slots,
                     description="Register slots per frame prefix"),
            CSRField("rot_slot", size=4, offset=12, reset=chip_desc.rot_slot,
                     description="Which slot carries the rotating register"),
            CSRField("mid_latch", size=1, offset=16,
                     reset=int(chip_desc.mid_latch),
                     description="Send the middle 11-clock LAT burst"),
            # The ROW/OE pulse that advances the chip's scan counter. Uniform
            # on an ICND1065; W12-then-W4 on a DP3264S, which reads the HIGH
            # WIDTH as meaning and shows nothing at all if it is wrong.
            CSRField("row_pulse", size=5, offset=17,
                     reset=getattr(chip_desc, "row_pulse", 2),
                     description="ROW pulse width in panel clocks"),
            CSRField("row_pulse_first", size=5, offset=22,
                     reset=getattr(chip_desc, "row_pulse_first", 2),
                     description="ROW pulse width for the first row of a frame"),
            CSRField("data_latch", size=1, offset=27,
                     reset=int(getattr(chip_desc, "data_latch", 0)),
                     description="Pulse LAT at the end of each row of pixels"),
            CSRField("row_address", size=1, offset=28,
                     reset=int(getattr(chip_desc, "row_address", 0)),
                     description="Drive A-E with the scanned row number"),
            CSRField("row_addr_shift", size=3, offset=29,
                     reset=int(getattr(chip_desc, "row_addr_shift", 0)),
                     description="Low bits of the row to drop before A/B/C"),
        ], description="Chip grammar; reset values are the built-in default")

        # ---- serial row driver (RT5958D / 5958 / DM5953 / SM5266 family) ----
        #
        # These modules do not take a row ADDRESS. The row driver is a cascaded
        # SHIFT REGISTER: Raffar's RT5958D is an "8-channel PMOS with built-in
        # 8-bit shift register ... data transfer by serial connection without
        # decode components on board", and its feature list says the quiet part
        # out loud -- "Wipe off 138 decoder". Eight chips of eight outputs,
        # cascaded DOUT->DIN, is exactly the 64 rows this panel scans.
        #
        # Its three control pins are DIN (serial data), LCK (clock) and BK
        # (blank / LED discharge). A row is selected by walking a single 1
        # along the chain: pulse LCK once per row with DIN high only on the
        # wrap back to row 0, and the bit steps from row to row for free.
        #
        # Which of HUB320's A, B and C carries which signal is NOT documented
        # for this module -- the hzeller tracker reports A=LCK, B=BK, C=DIN for
        # an RT5958 panel, which is where the reset values come from, but that
        # is one report about a different board. So the mapping and the
        # polarities are RUNTIME state: six permutations times eight polarity
        # combinations is a search, and it should cost an HTTP request rather
        # than a seven-minute place-and-route. The same lesson as chip_cfg.
        self.row_ser = CSRStorage(fields=[
            CSRField("enable", size=1, offset=0,
                     reset=int(getattr(chip_desc, "row_serial", 0)),
                     description="Drive A/B/C as a serial row chain, not an address"),
            CSRField("a_src", size=2, offset=1, reset=1,
                     description="Pin 17: 0=DIN 1=LCK 2=BK 3=low"),
            CSRField("b_src", size=2, offset=3, reset=2,
                     description="Pin 18: 0=DIN 1=LCK 2=BK 3=low"),
            CSRField("c_src", size=2, offset=5, reset=0,
                     description="Pin 19: 0=DIN 1=LCK 2=BK 3=low"),
            CSRField("inv", size=3, offset=7, reset=0,
                     description="Invert pins 17/18/19 after selection"),
            CSRField("lck_w", size=4, offset=10, reset=2,
                     description="LCK high width, in panel bit periods"),
            CSRField("bk_w", size=5, offset=14, reset=4,
                     description="BK assert width across the row change"),
            CSRField("lck_dly", size=3, offset=19, reset=1,
                     description="Bit periods DIN is held before LCK rises"),
        ], description="Serial row-driver mapping; all of it live")

        # # #

        # ---- shift-clock division ----
        #
        # The same shape hub75.py uses: a counter in the sys domain, with the
        # panel CLK emitted as a DATA signal rather than a gated clock. `tick`
        # is one sys cycle per panel bit, and CLK falls at the tick and rises
        # halfway through, so data driven on the tick is stable well before the
        # chip samples it on the rising edge.
        # The divider is RUNTIME state, not a build constant.
        #
        # The panel clock is the one part of this stage that no simulation has
        # ever touched: tools/sim_icn1065.py instantiates Icn1065Output, the
        # protocol FSM, and never this module -- so every check runs one sim
        # cycle per emitted bit while the real pin is driven from the free
        # running counter below. Which edge the chip samples on, how much setup
        # the data gets, and whether the wire is clean at 8.75 MHz are all
        # unverified by anything in the repo.
        #
        # Making N writable turns "is this a clocking problem?" into an
        # experiment instead of a seven-minute place-and-route per guess:
        # sweep the panel clock down and watch the glass. A fault that is
        # really setup/hold or signal integrity disappears as N grows; one that
        # survives 137 kHz is not a clocking fault and the search moves on.
        #
        # `tick` gates the whole stage through CEInserter (`out.ce`,
        # `prefix.ce`), so slowing N slows the protocol coherently rather than
        # shearing the clock away from the data it carries.
        # WIDENED rather than a new CSR: adding one would shift every register
        # after it, which is the exact failure that cost hours (an endpoint
        # answering 200 off a neighbouring register). Growing an existing
        # CSRStorage leaves every address where it is.
        self.latch_cfg = CSRStorage(fields=[
            CSRField("latch_phase", size=4, offset=0, reset=0, description=(
                "Extra DATA_LATCH pulses emitted after the frame prefix. "
                "Tested 0, 1 and 15 on hardware 2026-10-19: the pulses reach "
                "the chip (verified 1024 -> 1025 -> 1027 in simulation at the "
                "real geometry) and the picture does not move, so the 16-pitch "
                "dots are NOT a write-pointer phase error. Kept as an "
                "instrument; 0 is no correction.")),
            CSRField("chan_xor", size=4, offset=4,
                     reset=int(getattr(chip_desc, "chan_xor", 0)), description=(
                "XOR applied to the channel index -- a permutation of the "
                "sixteen columns inside every driver. 1 is the measured "
                "pair swap for this module. This belongs in the gateware "
                "rather than in the firmware's pattern generator, because "
                "STREAMED content writes the framebuffer directly and never "
                "sees a pattern-side correction; 0 is identity.")),
            CSRField("sweep_adv", size=3, offset=8,
                     reset=int(getattr(chip_desc, "sweep_adv", 0)), description=(
                "Advance the FETCH ADDRESS by N sweeps, carrying into the row, "
                "WITHOUT changing the latch count. Not latch_phase: that adds "
                "DATA_LATCH pulses and moves our data LATER against the chip's "
                "line boundary; this moves the DATA EARLIER and leaves the "
                "count at 16 per row. The 16-pitch dots sit at the columns fed "
                "by sweep 0 -- the first latch of every row, in all eight chips "
                "-- and carry the adjacent row's value, while simulation proves "
                "our first latch commits the RIGHT row's data. That means the "
                "chip's line advance lands one latch later than ours, and this "
                "is the lever for it. 0 is no correction.")),
            # Appended at offset 11, which was free: every field below it keeps
            # its address, so no firmware moves.
            CSRField("passes", size=7, offset=11,
                     reset=int(getattr(chip_desc, "display_passes", 1)) - 1,
                     description=(
                "Display passes per data frame MINUS ONE -- the chip's own "
                "reg0x03. The wide W12 ROW pulse fires on row 0 of every "
                "(passes + 1)-th pass (11.6.2), and the SAME number sets the "
                "chip's displayable grey range, 4 x (reg0x04 + 1) x "
                "(passes + 1) (11.7.5). Runtime because each value otherwise "
                "costs a power cycle: too few and every mixed colour clips to "
                "full, too many and the W12 cadence drifts against the row "
                "chain (the 'rain' of 4.4). MUST match reg0x03 in the chip "
                "table.")),
        ])
        self.clk_div = CSRStorage(8, reset=clk_div, description=(
            "Panel shift clock divider, sys/N. Clamped to N>=2, so at 35 MHz "
            "the range is 17.5 MHz down to 137 kHz. Live."))
        div_n = Signal(8)
        # Clamp rather than trust: N<2 makes `divcnt >= N//2` permanently true,
        # which parks CLK HIGH and stops the panel clock entirely. That was a
        # build-time assert when N was a constant; a writable register needs it
        # enforced in logic, because a bad write would otherwise be a dead card
        # reachable only by power cycling.
        self.comb += div_n.eq(Mux(self.clk_div.storage < 2, 2, self.clk_div.storage))
        divcnt = Signal(8)
        tick = Signal()
        self.sync += If(divcnt >= div_n - 1, divcnt.eq(0)).Else(divcnt.eq(divcnt + 1))
        self.comb += tick.eq(divcnt == 0)
        self.comb += pins_common.clk.eq(divcnt >= (div_n >> 1))

        # ---- the memory side, at full sys speed ----
        port = sdram.crossbar.get_port(mode="read", data_width=32)
        # `per_source=groups`: one pixel is `groups` reads and they are now
        # issued back to back rather than serially, so each source needs that
        # many commands in flight. At one, a pixel cost 4L+16 cycles and the
        # four-group path starved at the shipped divider (E320-BRINGUP 4.6).
        self.submodules.arb = arb = Icn1065PipelinedArbiter(
            n_outputs, per_source=groups)
        self.comb += [
            port.cmd.valid.eq(arb.cmd_valid),
            port.cmd.we.eq(0),
            port.cmd.addr.eq(arb.cmd_adr),
            arb.cmd_ready.eq(port.cmd.ready),
            port.rdata.ready.eq(1),
        ]
        # Indexed colour: the returning word goes through the palette (one
        # cycle) before the arbiter hands it to the sources. 32-bit pixels
        # only -- a 16-bit framebuffer packs two pixels per word and has no
        # index byte, so there it passes straight through.
        if pix_bits == 32:
            self.submodules.palette_stage = ps = Icn1065PaletteStage(
                palette_mem, self.ctrl.fields.indexed, palette_bank,
                port.rdata.data, port.rdata.valid)
            self.comb += [arb.dat.eq(ps.dat), arb.dat_valid.eq(ps.valid)]
        else:
            self.comb += [arb.dat.eq(port.rdata.data),
                          arb.dat_valid.eq(port.rdata.valid)]

        # THE GREY DEPTH IS THE CHIP TABLE'S, derived from its 0x03/0x04
        # registers (11.7.5) rather than a constant here -- see spwm_chips.py.
        # A stage wider than the chip displays clips every mixed colour to
        # full; a stage narrower wastes range and lands everything in the
        # low-grey region where the per-colour compensation registers distort
        # the hue. Both were seen on glass (E320-BRINGUP 4.10).
        grey_bits = getattr(chip_desc, "grey_bits", GREY_BITS)
        print(f"  grey: {grey_bits}-bit ({chip_desc.pwm_levels} PWM levels, "
              f"{chip_desc.display_passes} display passes per frame)")

        shared = None
        if gamma:
            self.submodules.gamma = g = Icn1065SharedGamma(arb.dat,
                                                           grey_bits=grey_bits)
            shared = g.channels

        # Framebuffer stride in PIXELS: the layout's virtual width, from
        # ctrl.width, so the firmware's picture of the image and the scan-out's
        # agree by construction. The pixel source turns this into a word
        # address, which differs between the 32-bit and 16-bit formats. It was
        # the constant module_w * n_outputs until 1.20.0 -- see ctrl above.
        width_words = self.ctrl.fields.width

        # Each group's strip origin, in PIXELS relative to the output's
        # placement -- the same expression shape as `origin` below.
        group_offsets = [c.fields.y * (16 * width_words) + c.fields.x * 16
                         for c in group_cfg]

        # ONE prefix for the whole board -- see Icn1065Output. It is clock-
        # enabled on the same tick as the outputs, so the command train it
        # emits is in step with the pixel data they emit.
        self.submodules.prefix = prefix = CEInserter()(
            SpwmPrefix(columns, chip=chip_desc))
        self.comb += prefix.ce.eq(tick)

        # The line buffer costs block RAM, and on this part that is the binding
        # constraint -- not logic, which is what every earlier note here
        # assumed. Measured on the 1.30.0 one-output E320 build: 47/56 DP16KD
        # (83%). Each additional output adds 6 EBRs (two halves x three RGB
        # groups), so two outputs need 53/56 and a third needs 59 and does not
        # fit.
        #
        # Refuse it here rather than letting place-and-route fail. nextpnr's
        # message for an EBR overflow names the resource, not the reason, and
        # the nine-output preset in docs/BENCHMARKS.md still advertises 36/56
        # from before the line buffer existed -- so the obvious reading of a
        # failed 9-output build is "the design got too big", which sends you
        # looking at logic.
        # Per-output cost is 5, DERIVED not scaled: the buffer is
        # `3 * groups * grey_bits` = 156 bits wide by `2 * columns` = 512
        # entries, and a DP16KD is at most 36 bits wide, so it needs
        # ceil(156/36) = 5 in parallel -- which is also exactly its capacity,
        # ceil(79872/18432) = 5. An earlier version of this guard used 6 from
        # dividing a measured total, and that one wrong EBR made it refuse 3
        # outputs outright.
        #
        # The 1.30.0 one-output E320 build measured 47/56, so the fixed cost is
        # 42. Three outputs therefore want 57 -- over by ONE, which is a budget
        # exercise and not an architectural wall. Candidates, largest first:
        # the BIOS ROM (0x88bc), `burst_fifo` (2048 x 40 bits ~ 5 EBR, itself
        # widened from 512 for RB-5524, where 1536 would still hold ~4
        # packets), the palette (512 x 32 ~ 1), and the VexRiscv caches. The
        # Ethernet MAC slots are already minimal at nrxslots/ntxslots = 2,
        # reduced from 8 the last time this bit.
        #
        # So this refuses 3 with a message that says what to free, rather than
        # pretending the part cannot do it. Stock Colorlight firmware drives
        # three of these modules at 20 fps on the same LFE5U-25F, so any claim
        # that it does not fit is a claim about THIS design, not the silicon.
        # The fixed cost is 42 at burst_depth 2048, and colorlight.py drops
        # that FIFO to 1536 above two outputs, which returns one EBR. So the
        # budget is not a single number: 47 / 52 / 56 for one, two and three
        # outputs, and three only fits BECAUSE of that trade.
        _EBR_TOTAL = 56
        _EBR_PER_OUTPUT = 5
        # The fixed cost moves with burst_depth, which colorlight.py scales by
        # output count: 4096 at one output (47), 2048 at two (42), 1536 above
        # (41). Everything but the FIFO is 37.
        _EBR_FIXED_WIDE = 41   # burst_depth 1536, used above 2 outputs
        _EBR_FIXED = 42        # burst_depth 2048
        _EBR_FIXED_DEEP = 47   # burst_depth 4096, one output only
        _fixed = (_EBR_FIXED_DEEP if n_outputs <= 1
                  else _EBR_FIXED if n_outputs <= 2
                  else _EBR_FIXED_WIDE)
        _ebr_budget = (_EBR_TOTAL - _EBR_FIXED_WIDE) // _EBR_PER_OUTPUT  # -> 3
        if n_outputs > _ebr_budget:
            raise ValueError(
                f"{n_outputs} outputs needs {n_outputs * _EBR_PER_OUTPUT} "
                f"EBRs of line buffer on top of the ~{_fixed} the rest of "
                f"the SoC already holds, which is more than the "
                f"{_EBR_TOTAL} this part has. The limit is {_ebr_budget} "
                f"outputs (measured: 47/56 at one output, gateware 1.30.0, "
                f"and burst_depth already drops to 1536 above two).\n"
                f"This is the line buffer added in gateware 1.29.0 (see "
                f"Icn1065LineBuffer). Builds before it used zero block RAM "
                f"here, and docs/BENCHMARKS.md still shows 36/56 for nine "
                f"outputs from that era -- those figures are superseded.\n"
                f"To go wider, free EBR from the FIXED cost -- the BIOS ROM, "
                f"burst_fifo (2048 x 40, ~5 EBR; 1536 still holds ~4 packets), "
                f"the palette, or the VexRiscv caches. Three outputs need just "
                f"ONE more EBR than the part has, so this is a budget exercise. "
                f"Stock firmware drives three of these modules at 20 fps on the "
                f"same LFE5U-25F, so the limit is this design, not the silicon."
            )

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
                                     groups=groups, pix_bits=pix_bits,
                                     group_offsets=group_offsets,
                                     grey_bits=grey_bits)
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
            # A ROW AHEAD, not on demand.
            #
            # The serialiser used to pull pixels through `want`/`served` with a
            # hard deadline: all `groups` reads inside its 16-clock window or
            # it shifts whatever was on the bus. That made the PICTURE a
            # function of memory latency, and memory latency moves with DRAM
            # refresh, the pixel DMA's writes and output arbitration. Measured
            # 2026-10-20: 13.8 underruns/s while streaming at 20 fps, on a
            # panel that was spotless with nothing writing.
            #
            # The line buffer breaks that coupling. The fill engine runs flat
            # out a row ahead; the serialiser does a one-cycle memory read.
            # Nothing downstream of DRAM can starve the shift register any
            # more, which is what the throttle and the slowed panel clock were
            # both working around.
            lb = Icn1065LineBuffer(
                columns, rows_per_frame, groups=groups,
                grey_bits=getattr(chip_desc, "grey_bits", GREY_BITS))
            self.submodules += lb
            self.comb += [
                lb.col.eq(out.pix_col),
                lb.row.eq(out.pix_row),
                src.req.eq(lb.src_req),
                src.col.eq(lb.src_col),
                src.row.eq(lb.src_row),
                lb.src_valid.eq(src.valid),
                *[lb.src_channels[c].eq(src.channels[c])
                  for c in range(3 * groups)],
            ]
            served = Signal()
            want = Signal()
            self.sync += [
                If(out.ce, want.eq(out.pix_req)),
                # `served` records that the PENDING request has been answered.
                # Two things were wrong with `If(out.ce, 0).Elif(valid, 1)`:
                #
                #  * it cleared on EVERY tick -- `out.ce` fires once per panel
                #    BIT, sixteen times per word -- not on the tick that
                #    actually issues a new request;
                #  * `out.ce` took priority, so a `src.valid` landing ON a tick
                #    was dropped. That is data which arrived exactly in time:
                #    `held` updates on the same edge the serialiser latches, so
                #    the word is there and usable, and discarding the flag left
                #    `src.req` high so the source refetched against
                #    already-advanced coordinates -- a repeat AND a skipped
                #    pixel. Measured at the threshold: latency 12 gives a clean
                #    one-load lag (0,0,16,1,17,2,18...) while latency 13 gives
                #    2:1 undersampling (0,0,16,16,17,17...), half the pixels
                #    never fetched at all.
                #
                # So: an arrival always wins the tie, and the flag clears only
                # on a tick that issues a new request.
                If(src.valid, served.eq(1)).Elif(out.ce & out.pix_req, served.eq(0)),
            ]
            # One counter for the whole stage: the outputs run in lockstep, so
            # a starve hits them together and counting each would multiply the
            # same event.
            if i == 0:
                # With the line buffer this should be structurally zero: it
                # fires only if the serialiser reaches a row the fill engine
                # never finished, which the frame prefix (~1332 clocks) gives
                # it ample time to do. Kept, because "should be impossible" is
                # exactly the claim worth instrumenting -- the previous
                # version of this counter overturned my own theory within a
                # minute of existing.
                self.sync += If(out.ce & out.pix_req & ~lb.primed,
                                self.underruns.status.eq(self.underruns.status + 1))
            # The permuted view of src.channels, selected by the rgb_order CSR.
            _ordered = []
            for c in range(3 * groups):
                grp, slot = divmod(c, 3)
                sig = Signal(len(src.channels[c]))
                self.comb += Case(self.rgb_order.storage, {
                    v: sig.eq(lb.channels[3 * grp + "RGB".index(name[slot])])
                    for v, name in enumerate(RGB_ORDERS)
                })
                _ordered.append(sig)

            self.comb += [
                out.ce.eq(tick),
                out.start.eq(self.ctrl.fields.enabled),
                # Channels permuted by rgb_order on the way to the serialiser.
                #
                # Per GROUP, not globally: a 4-group HUB320 build has twelve
                # channels and each group of three is one module's r/g/b, so the
                # permutation applies within a group. Permuting the flat list
                # would cross-wire colours between modules.
                #
                # One mux per channel. At order 0 it selects its own channel, so
                # the cost is the mux and nothing else -- no extra memory reads
                # and no change to the shift train's timing, which matters here
                # because this stage's critical path already set the clock
                # ceiling once (see the `want` register above).
                *[out.pix_in[c].eq(_ordered[c]) for c in range(3 * groups)],
                # One connector pin per serialiser channel, in order. The
                # connector's names are r0/g0/b0 for the first group, r1/g1/b1
                # for the second and so on, which is off by one against the
                # chip's naming (it calls them r1 g1 b1 r2 g2 b2 ...) and has
                # to be read carefully rather than pattern-matched.
                #
                # Driven by name rather than unrolled, because unrolling is
                # what silently capped this at six: the stage accepted
                # groups=4 and computed twelve channels while only six of them
                # ever reached a pin, so a HUB320 build would have lit half of
                # each panel and looked like a wiring fault.
                *[getattr(pins[i], RGB_PINS[c]).eq(out.rgb[c])
                  for c in range(3 * groups)],
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

        # The prefix is shared, but each output owns its own row scanner, so
        # the ROW widths go to ALL of them. Wiring these to starts[:1] like the
        # table above would light connector 1 and leave the rest dark.
        for out in starts:
            self.comb += [
                out.row_pulse.eq(self.chip_cfg.fields.row_pulse),
                out.row_pulse_first.eq(self.chip_cfg.fields.row_pulse_first),
                out.data_latch_en.eq(self.chip_cfg.fields.data_latch),
                out.chan_rev.eq(self.chip_cfg.fields.chan_rev),
                out.latch_phase.eq(self.latch_cfg.fields.latch_phase),
                out.chan_xor.eq(self.latch_cfg.fields.chan_xor),
                out.sweep_adv.eq(self.latch_cfg.fields.sweep_adv),
                out.passes_m1.eq(self.latch_cfg.fields.passes),
            ]

        self.comb += arb.connect(srcs)
        # The shared prefix is started once, by the board rather than by any
        # one output.
        self.comb += prefix.start.eq(starts[0].prefix_req)
        # A-E. Whether they carry anything depends on the MODULE, not the
        # driver IC: an ICND1065 counts rows internally off the ROW pulse and
        # wants these dead, while a module with a 4051-style row decoder --
        # HX6158, DM5953, VOD5958 -- selects the row from this address and
        # shows nothing at all without it. Holding them at 0 unconditionally,
        # which this did, means row 0 is selected forever.
        # The serial row chain. One LCK pulse per row, DIN high only on the
        # wrap to row 0, BK held across the change so the row is blanked while
        # it moves -- which is the anti-ghosting timeslot the RT5958D exists to
        # give the controller ("let controller determined the turn-on,
        # discharge, and row blank timing").
        scan_row = starts[0].scan_row
        prev_row = Signal(len(scan_row))
        row_changed = Signal()
        self.sync += If(tick, prev_row.eq(scan_row))
        self.comb += row_changed.eq(tick & (scan_row != prev_row))

        ser_cnt = Signal(6)
        ser_run = Signal()
        ser_din = Signal()
        lck_end = Signal(6)
        span = Signal(6)
        self.comb += lck_end.eq(self.row_ser.fields.lck_dly + self.row_ser.fields.lck_w)
        self.comb += span.eq(Mux(lck_end > self.row_ser.fields.bk_w,
                                 lck_end, self.row_ser.fields.bk_w))
        self.sync += [
            If(row_changed,
                ser_cnt.eq(0),
                ser_run.eq(1),
                # DIN is sampled ONCE, at the row change, and held for the
                # whole pulse. Deriving it combinationally from scan_row would
                # put a comparator in the setup path of the only data line the
                # row chain has.
                ser_din.eq(scan_row == 0),
            ).Elif(tick & ser_run,
                If(ser_cnt >= span, ser_run.eq(0)).Else(ser_cnt.eq(ser_cnt + 1)),
            ),
        ]
        din_o = Signal()
        lck_o = Signal()
        bk_o = Signal()
        self.comb += [
            # DIN is valid for the whole sequence; LCK rises only after
            # `lck_dly` bit periods of it.
            #
            # board707's DMD_Mux595::set_mux() -- a tested implementation of
            # this exact chain -- raises the latch, sets SDI, then
            # busyWaitDelay()s with the comment "hold SDI before CLK" before
            # clocking. Asserting DIN and LCK on the same edge, which this did
            # first, offers the shift register zero setup and is a coin toss on
            # whether the bit lands.
            din_o.eq(ser_run & ser_din),
            lck_o.eq(ser_run & (ser_cnt >= self.row_ser.fields.lck_dly)
                             & (ser_cnt < lck_end)),
            bk_o.eq(ser_run & (ser_cnt < self.row_ser.fields.bk_w)),
        ]
        pick = Array([din_o, lck_o, bk_o, Constant(0)])
        ser_row = Signal(5)
        self.comb += [
            ser_row[0].eq(pick[self.row_ser.fields.a_src] ^ self.row_ser.fields.inv[0]),
            ser_row[1].eq(pick[self.row_ser.fields.b_src] ^ self.row_ser.fields.inv[1]),
            ser_row[2].eq(pick[self.row_ser.fields.c_src] ^ self.row_ser.fields.inv[2]),
            # Pins 20 and 21 stay LOW in serial mode. On this module they are
            # NC, but on a module built to the other HUB320 convention they are
            # S-CS and S-MI -- and S-MI is an output FROM the panel, so driving
            # D/E there is output against output. See docs/HUB320.md.
            ser_row[3].eq(0),
            ser_row[4].eq(0),
        ]

        self.comb += [pins_common.lat.eq(starts[0].lat),
                      pins_common.oe.eq(starts[0].oe),
                      pins_common.row.eq(
                          Mux(self.row_ser.fields.enable,
                              ser_row,
                              Mux(self.chip_cfg.fields.row_address,
                                  starts[0].scan_row >> self.chip_cfg.fields.row_addr_shift,
                                  0)))]

        # One frame_done is enough: the outputs are started together and run
        # identical sequences, so they finish in lockstep.
        #
        # `swap_req` is NOT driven from here, and must not be. It is an INPUT,
        # driven by the pixel DMA in colorlight.py, exactly as in hub75.py: the
        # buffer flip belongs to the STREAM's frame boundary, not to the
        # display's refresh boundary. This module used to also assign it from
        # `starts[0].frame_done`, which cost a long hunt (RB-5596):
        #
        #   Migen puts every combinational assignment to one signal in a single
        #   process, submodule statements LAST, so the inner assignment silently
        #   won and the DMA's pulse never reached `fb_base_eff`. The display
        #   then never flipped, while `write_base_next` in dma_writer.py went on
        #   assuming it had -- so each frame's chunk 0 was written to the half
        #   on screen and chunks 1..67 to the half nobody read. Every counter
        #   stayed clean (all pixels written, all arrival bits set, every frame
        #   "complete") and the glass showed 487 fresh pixels over a stale
        #   frame. ONE DRIVER, in the parent, is what makes the two agree.
        # EDGE, not level. `frame_done` is a combinational output of an FSM
        # that is CLOCK-ENABLED at the panel bit rate, so it stays asserted for
        # the whole `clk_div`-wide tick. Counting it directly counted SYS
        # CLOCKS, not frames -- which is why this register read ~267/s at every
        # divider from 4 to 24 while the real frame rate was 267/clk_div, and
        # why a 6x change in the panel clock appeared to move it by 0.3%.
        #
        # That bogus constant is what led this work to "the scan-out is
        # memory-bound at four clocks per pixel", which was wrong, and the
        # number was quoted as measured fact. An instrument nobody has
        # calibrated is worse than no instrument.
        fd_prev = Signal()
        self.sync += fd_prev.eq(starts[0].frame_done)
        self.sync += If(starts[0].frame_done & ~fd_prev,
                        self.refresh_count.status.eq(self.refresh_count.status + 1))

    @staticmethod
    def frame_clocks(columns, scan):
        return frame_clocks(columns, scan)
