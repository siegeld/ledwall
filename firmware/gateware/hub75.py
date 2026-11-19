#!/usr/bin/env python3
# Protocol description https://fw.hardijzer.nl/?p=223
# Using binary code modulation (http://www.batsocks.co.uk/readme/art_bcm_1.htm)
from types import SimpleNamespace

from migen import If, Signal, Array, Memory, Module, FSM, NextValue, NextState, Mux, Cat, Case
from litex.soc.interconnect.csr import AutoCSR, CSRStorage, CSRStatus, CSRField
from litedram.frontend.dma import LiteDRAMDMAReader

# Framebuffer base, in 32-bit words. MUST match FB_SDRAM_OFFSET_BYTES in
# sw_rust/barsign_disp/src/hub75.rs, and half_words in colorlight.py.
#
# SDRAM map, low to high:
#   0x000000..0x140000  firmware text/data/bss   (1.25 MB; the image is ~300 KB)
#   0x140000..0x3C0000  framebuffer, 2 x 327,680 words
#   0x3C0000..0x400000  stack                    (256 KB, reserved)
#
# The stack reserve is new and it was overdue. memory.x aliases REGION_STACK to
# the whole of main_ram, so _stack_start is the TOP of SDRAM and the stack grows
# down -- straight into the framebuffer's upper half, which used to end at
# 0x400000 as well. Reading the ELF confirms it: the last LOAD segment runs from
# 0x4004a478 to 0x40400000. It never bit because a 128x128 image fills 16,384 of
# a 262,144-word buffer, so the stack never reached the used part. A 768x384
# wall fills 294,912 of them and that luck runs out.
# SDRAM map, low to high:
#   0x000000..0x140000  firmware text/data/bss   (1.25 MB; the image is ~300 KB)
#   0x140000..0x3C0000  framebuffer, 2 x half_words
#   0x3C0000..0x400000  stack                    (256 KB, reserved)
#
# The stack reserve is deliberate. memory.x aliases REGION_STACK to the whole of
# main_ram, so _stack_start is the TOP of SDRAM and the stack grows down --
# straight into the framebuffer's upper half, which used to end at 0x400000 too.
# Reading the ELF confirms it: the last LOAD segment runs to 0x40400000. It never
# bit because a 128x128 image fills 16,384 of a 262,144-word buffer, so the stack
# never reached the used part. A 768x384 wall fills 294,912 and that luck ends.
#
# ONE definition, used by the scan-out below, by colorlight.py for the pixel
# DMA, and mirrored in sw_rust/barsign_disp/src/hub75.rs. build.sh refuses to
# build if the Rust side disagrees. There were THREE copies of half_words until
# 2026-09-18 and changing two of them made the display and the DMA address
# different halves.
FB_BASE_BYTES = 0x140000
FB_HALF_BYTES = 0x140000
sdram_offset = FB_BASE_BYTES // 4
half_words = FB_HALF_BYTES // 4


class Hub75(Module, AutoCSR):
    def __init__(self, pins_common, pins, sdram, columns=96, rows=48, scan=24, chain_length_2=0, n_outputs=8):
        """
        HUB75 LED Panel Controller.

        Args:
            pins_common: Common HUB75 pins (active accent, accent, row select, etc.)
            pins: Per-output RGB data pins
            sdram: SDRAM controller for framebuffer access
            columns: Number of columns (e.g., 96, 128). Default 96.
            rows: Number of rows (e.g., 48, 64). Default 48.
            scan: Scan rate - number of row addresses (e.g., 24 for 1/24 scan). Default 24.
            chain_length_2: log2 of chain positions (0=1, 1=2, 2=4). Default 0 for single panel.
        """
        # Calculate derived values
        rows_per_half = rows // 2
        row_bits = (scan - 1).bit_length()  # Number of address bits needed
        # Registers
        self.ctrl = CSRStorage(fields=[
            CSRField("indexed", description="Display an indexed image"),
            CSRField("enabled", description="Enable the output"),
            CSRField("width", description="Width of the image", size=16),
        ])
        self.fb_base = CSRStorage(fields=[
            CSRField("offset", description="Framebuffer base address in 32-bit words", size=20),
        ], reset=sdram_offset)

        # HARDWARE BUFFER SWAP, at the frame boundary.
        #
        # The CPU used to own the swap, and it could not be quick enough. The
        # gateware finishes a frame and the next one starts writing IMMEDIATELY,
        # into the buffer the CPU has not swapped away from yet -- so whatever
        # the DMA writes before the CPU gets round to it appears on the glass as
        # a band of the next frame across the TOP of the image. Measured at
        # 25.8 fps on a 384x192 frame: 10 chunks (6.6%) with the swap on the 1 ms
        # tick, and still 2 with the CPU polling flat out. That residue is
        # exactly the "sometimes crap at the top" of a video feed.
        #
        # There is no CPU latency small enough to fix this, because the correct
        # instant is inside the gateware: the frame changes when the next
        # header is parsed, and the flip has to happen BEFORE the first pixel of
        # that frame is written. So `swap_req` pulses there and the effective
        # base flips here, one cycle ahead of any PIX write.
        #
        # fb_base_eff, not fb_base.storage, is what the scan-out and the DMA's
        # write_base both follow -- they must never disagree about which half is
        # live. A CSR write still wins (`fb_base.re`), so the firmware can place
        # the buffer explicitly at boot or with auto_swap off.
        self.auto_swap = CSRStorage(1, reset=0,
            description="Flip the framebuffer in gateware at each frame boundary")

        # Which logical colour each HUB75 pin group actually carries.
        #
        # The IC and the panel's own wiring decide this, not us: two modules
        # with identical connectors can expect different orders, and the only
        # symptom is wrong hues -- reds that come out green -- with every
        # counter clean. Before this it was fixed by the board pin map plus the
        # slice assignments in Output, so a module that disagreed needed a
        # gateware rebuild or swapped wires.
        #
        # Named the way LED parts are: the value is what the PANEL expects, so
        # RGB (0) is standard and GRB (2) means the first pin group carries
        # green. Reset 0 keeps every existing bitstream behaving exactly as it
        # did. Per CARD, not per module -- all of a card's outputs run in
        # lockstep off one engine, so there is one order for the board.
        self.rgb_order = CSRStorage(3, reset=0,
            description="Panel channel order: 0=RGB 1=RBG 2=GRB 3=GBR 4=BRG 5=BGR")
        self.swap_req = Signal()
        self.fb_base_now = CSRStatus(20,
            description="Framebuffer half currently displayed (follows auto_swap)")
        fb_base_eff = Signal(20, reset=sdram_offset)
        self.fb_base_eff = fb_base_eff
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

        # THE PALETTE FLIPS WITH THE FRAMEBUFFER (RB-5516).
        #
        # The framebuffer is double-buffered and swapped here; the palette was a
        # single 256-entry memory the CPU wrote in place. In indexed mode a
        # pixel is `ramp_base + coverage`, so the COLOUR of everything on the
        # glass comes from that table -- and rewriting it while the scan-out is
        # reading it means the display renders a palette that is half old and
        # half new. Every counter stays clean through this, because nothing is
        # dropped or late: the frames are perfect and the colours are not.
        #
        # A streamed panel can dodge it with `format: rgb` (no palette exists on
        # the wire). A program cannot: indexed is the whole point, because a
        # glyph's coverage IS the ramp offset, which is what makes anti-aliased
        # text one add per pixel and recolouring 16 words instead of a repaint.
        #
        # So the palette memory holds two banks and the LIVE one is selected by
        # `fb_base_eff` -- the same signal the scan-out and the write DMA
        # follow. Not a second swap register: a second register is a second
        # thing to keep in step, and the two would eventually disagree for
        # exactly one frame. Deriving it means the palette and the pixels that
        # index it can never be from different frames, and it works identically
        # whether the flip came from a CSR write or from gateware `auto_swap`.
        palette_bank = Signal()
        self.comb += palette_bank.eq(fb_base_eff != sdram_offset)
        self.hw_palette_banks = CSRStatus(4, reset=2,
            description="Palette banks in this bitstream (1 = single, shared)")
        # Read-only registers exposing compile-time bitstream parameters
        self.hw_columns = CSRStatus(16, reset=columns,
            description="Bitstream pixel columns (read-only)")
        self.hw_rows = CSRStatus(16, reset=rows,
            description="Bitstream pixel rows (read-only)")
        # hw_config packs: scan (bits 0-7), chain_length_2 (bits 8-11), n_outputs (bits 12-15)
        hw_config_val = (scan & 0xFF) | ((chain_length_2 & 0xF) << 8) | ((n_outputs & 0xF) << 12)
        self.hw_config = CSRStatus(16, reset=hw_config_val,
            description="Bitstream config: scan[7:0], chain_length_2[11:8], n_outputs[15:12]")
        panel_config = Array()
        for panel_output in range(n_outputs):
            for chain_pos in range(1 << chain_length_2):
                name = "panel" + str(panel_output) + "_" + str(chain_pos)
                csr = CSRStorage(name=name,
                                 fields=[
                                     CSRField(
                                         "x", description="x position in multiples of 16", size=8, offset=0),
                                     CSRField(
                                         "y", description="y position in multiples of 16", size=8, offset=8),
                                     CSRField(
                                         "rot", description="rotation in clockwise 90°", size=2, offset=16),
                                 ])
                setattr(self, name, csr)
                panel_config.append(csr)

        read_port = sdram.crossbar.get_port(mode="read", data_width=32)
        output_config = SimpleNamespace(
            indexed=self.ctrl.fields.indexed, width=self.ctrl.fields.width
        )
        self.submodules.common = FrameController(
            pins_common,
            self.ctrl.fields.enabled,
            brightness_psc=16,
            scan=scan,
            row_bits=row_bits,
        )
        self.submodules.specific = RowController(
            self.common, pins, output_config, panel_config, read_port,
            fb_base=fb_base_eff,
            columns=columns, rows_per_half=rows_per_half, scan=scan,
            chain_length_2=chain_length_2, n_outputs=n_outputs,
            rgb_order=self.rgb_order.storage,
            palette_bank=palette_bank,
        )
        # Completed refreshes since reset. Read twice over a known interval to
        # get the true refresh rate, and from it the read bandwidth actually
        # achieved -- which decides whether a write DMA has any headroom.
        self.refresh_count = CSRStatus(32, description="Completed framebuffer refreshes")
        self.sync += self.refresh_count.status.eq(self.refresh_count.status + self.common.refresh_tick)

        self.palette_memory = self.specific.palette_memory


# Taken from https://learn.adafruit.com/led-tricks-gamma-correction/the-longer-fix
def _get_gamma_corr(bits_in=8, bits_out=8):
    gamma = 2.8
    max_in = (1 << bits_in) - 1
    max_out = (1 << bits_out) - 1
    gamma_lut = Array()
    for i in range(max_in + 1):
        gamma_lut.append(int(pow(i / max_in, gamma) * max_out + 0.5))
    return gamma_lut


class FrameController(Module):
    def __init__(
            self, outputs_common, enable: Signal(1), brightness_psc=1, brightness_bits=8,
            scan=24, row_bits=5
    ):
        self.start_shifting = start_shifting = Signal(1)
        self.shifting_done = shifting_done = Signal(1)
        # Pulses once per completed refresh (row counter wrapping back to 0).
        # Instrumentation: the display DMA re-reads the WHOLE framebuffer every
        # refresh, so the achieved refresh rate is what tells us how much SDRAM
        # bandwidth the read side is actually getting.
        self.refresh_tick = refresh_tick = Signal()
        self.clk = outputs_common.clk
        counter_max = 8

        counter = Signal(max=counter_max)
        self.output_bit = brightness_bit = Signal(max=brightness_bits)
        brightness_counter = Signal(
            max=(1 << brightness_bits) * brightness_psc)
        row_active = Signal(row_bits)  # Address bits for scan rate
        self.row_select = row_shifting = Signal(row_bits)
        self.scan = scan  # Store for row counter limit
        self.submodules.fsm = fsm = FSM(reset_state="RST")
        fsm.act("RST",
                start_shifting.eq(1),
                NextState("WAIT"))
        fsm.act("WAIT",
                If((brightness_counter == 0) & shifting_done & enable,
                    NextValue(counter, counter_max - 1),
                    NextState("LATCH")))
        fsm.act("LATCH",
                outputs_common.lat.eq(1),
                If(
                    counter == 0,
                    NextValue(brightness_counter,
                              (1 << brightness_bit) * brightness_psc),
                    start_shifting.eq(1),
                    If(brightness_bit != 0,
                        NextValue(row_active, row_shifting),
                        NextValue(brightness_bit, brightness_bit - 1),)
                    .Else(
                        # Wrap row counter at scan rate (e.g., 24 for 1/24 scan)
                        If(row_shifting >= (scan - 1),
                            NextValue(row_shifting, 0),
                            refresh_tick.eq(1),
                        ).Else(
                            NextValue(row_shifting, row_shifting + 1),
                        ),
                        NextValue(brightness_bit, brightness_bits - 1),
                    ),
                    NextValue(counter, counter_max - 1),
                    NextState("WAIT"),
                ))

        self.sync += [
            If(counter != 0,
               counter.eq(counter - 1)),
            If((brightness_counter != 0) & (counter == 0),
                brightness_counter.eq(brightness_counter - 1)),
        ]

        self.comb += [
            outputs_common.oe.eq((brightness_counter == 0) | (counter != 0)),
            outputs_common.row.eq(row_active),
        ]


class RowController(Module):
    def __init__(self, hub75_common, outputs_specific, output_config,
                 panel_config, read_port, fb_base=None, columns=96, rows_per_half=24, scan=24,
                 chain_length_2=0, n_outputs=8, rgb_order=None, palette_bank=None):
        # 512, not 256: two banks of 256, selected by `palette_bank` so the
        # palette flips with the framebuffer. See the comment on `palette_bank`
        # in Hub75. The CPU sees the whole 512 and writes the bank belonging to
        # the buffer it is drawing into.
        self.specials.palette_memory = palette_memory = Memory(
            width=32, depth=512, name="palette"
        )

        # Calculate buffer size: columns * 2 (top + bottom halves) * chain_length
        buffer_depth = columns * 2 * (1 << chain_length_2)
        buffer_addr_bits = (buffer_depth - 1).bit_length()

        shifting_buffer = Signal()

        # ONE memory per output holding BOTH ping-pong banks, selected by an
        # extra high address bit, rather than two memories per output.
        #
        # Why: an ECP5 EBR is 16 Kbit. At chain_length 1 a bank is
        # columns*2 x 32 = 256 x 32 = 8 Kbit, so two separate banks burn two
        # EBRs at HALF occupancy each. Merged, both banks are 512 x 32 =
        # exactly one full EBR. Six outputs cost 6 EBRs instead of 12, at full
        # 32-bit colour depth -- no RGB565, no banding.
        #
        # At chain_length 2 a merged pair is 1024 x 32 = 2 EBRs and this saves
        # nothing, which is fine: it is never worse, and chain_length 1 is what
        # a one-panel-per-connector wall should be built with anyway (TODO 7).
        #
        # The submodules still see one port each and address a bank as if it
        # were its own memory; the bank bit is concatenated on here. They are
        # given proxies rather than the real ports because RamToBufferReader
        # indexes its port array with a *Signal* (buffer_select), so the
        # collection has to stay a Migen Array.
        class _PortProxy:
            def __init__(self, adr_bits, dat_bits, write=False):
                self.adr = Signal(adr_bits)
                self.dat_r = Signal(dat_bits)
                if write:
                    self.dat_w = Signal(dat_bits)
                    self.we = Signal()

        row_readers = []
        row_writers = Array()
        for _ in range(n_outputs):
            row_buffer = Memory(width=32, depth=2 * buffer_depth)
            row_writer = row_buffer.get_port(write_capable=True)
            row_reader = row_buffer.get_port()
            self.specials += [row_buffer, row_reader, row_writer]

            w_proxy = _PortProxy(buffer_addr_bits, 32, write=True)
            r_proxy = _PortProxy(buffer_addr_bits, 32)
            self.comb += [
                # Writer fills the bank that is NOT being shifted out.
                row_writer.adr.eq(Cat(w_proxy.adr, ~shifting_buffer)),
                row_writer.dat_w.eq(w_proxy.dat_w),
                row_writer.we.eq(w_proxy.we),
                # Reader shifts out the bank the writer is not touching.
                row_reader.adr.eq(Cat(r_proxy.adr, shifting_buffer)),
                r_proxy.dat_r.eq(row_reader.dat_r),
            ]
            row_writers.append(w_proxy)
            row_readers.append(r_proxy)
        mem_start = Signal()
        row_start = Signal()
        # Row mask based on scan rate (e.g., 0x17 for scan=24, 0x1F for scan=32)
        row_mask = scan - 1
        # Compute next row with wrap-around (can't use Python % on Migen signals)
        row_bits = (scan - 1).bit_length()
        next_row = Signal(row_bits)
        self.comb += If(hub75_common.row_select >= (scan - 1),
            next_row.eq(0)
        ).Else(
            next_row.eq(hub75_common.row_select + 1)
        )
        self.submodules.buffer_reader = RamToBufferReader(
            mem_start, next_row,
            output_config.indexed, output_config.width, panel_config,
            read_port, row_writers, palette_memory,
            fb_base=fb_base, palette_bank=palette_bank,
            columns=columns, rows_per_half=rows_per_half, chain_length_2=chain_length_2,
            n_outputs=n_outputs)
        self.submodules.row_module = RowModule(
            row_start, hub75_common.clk, columns=columns, chain_length_2=chain_length_2
        )

        self.submodules.output = Output(outputs_specific,
                                        row_readers, self.row_module.counter,
                                        hub75_common.output_bit, self.row_module.buffer_select,
                                        n_outputs=n_outputs, rgb_order=rgb_order)

        self.submodules.fsm = FSM(reset_state="IDLE")
        self.fsm.act("IDLE",
                     If((hub75_common.start_shifting & (hub75_common.output_bit == 7)),
                        mem_start.eq(True),
                        row_start.eq(True),
                        NextState("WAIT_TILL_START"))
                     .Elif((hub75_common.start_shifting & (hub75_common.output_bit != 7)),
                           row_start.eq(True),
                           NextState("WAIT_TILL_START"))
                     .Else(
                         hub75_common.shifting_done.eq(True),
                     ))
        self.fsm.act("WAIT_TILL_START",
                     If(self.row_module.shifting,
                        NextState("SHIFT_OUT")))
        self.fsm.act("SHIFT_OUT",
                     If((hub75_common.output_bit == 0) & ~self.row_module.shifting
                        & self.buffer_reader.done,
                        NextValue(shifting_buffer, ~shifting_buffer),
                        NextState("IDLE")),
                     If((hub75_common.output_bit != 0) & ~self.row_module.shifting,
                         NextState("IDLE"))
                     )


class RamToBufferReader(Module):
    def __init__(
            self,
            start: Signal(1),
            row,  # Row address signal
            use_palette: Signal(1),
            image_width: Signal(16),
            panel_config,
            mem_read_port,
            buffer_write_port,
            palette_memory,
            fb_base=None,
            palette_bank=None,
            columns=96,
            rows_per_half=24,
            chain_length_2=0,
            n_outputs=8,
    ):
        self.done = Signal()
        # If ram bandwidth is needed for something else
        self.prevent_read = Signal()
        done = Signal()
        # Eliminate the delay
        self.comb += self.done.eq(~start & done)
        self.sync += If(start, done.eq(False))

        # RAM Reader
        self.submodules.reader = LiteDRAMDMAReader(mem_read_port, 16)
        self.submodules.ram_adr = RamAddressGenerator(
            start, self.reader.sink.ready & ~self.prevent_read, row, image_width, panel_config,
            fb_base=fb_base,
            columns=columns, rows_per_half=rows_per_half, chain_length_2=chain_length_2,
            n_outputs=n_outputs)

        # Generate rsv_level which was removed
        rsv_level = Signal(max=16 + 1)
        self.sync += [
            If(self.reader.sink.valid & self.reader.sink.ready,
               If(~(self.reader.source.valid & self.reader.source.ready),
                  rsv_level.eq(rsv_level + 1)
            )).Elif(self.reader.source.valid & self.reader.source.ready,
                rsv_level.eq(rsv_level - 1)
            )
        ]
        ram_valid = self.reader.source.valid
        ram_data = self.reader.source.data
        ram_done = Signal()
        self.comb += [
            self.reader.sink.address.eq(self.ram_adr.adr),
            self.reader.sink.valid.eq(self.ram_adr.valid & ~self.prevent_read),
            ram_done.eq((self.ram_adr.started == False)
                        & (rsv_level == 0)
                        & (self.reader.source.valid == False))
        ]
        self.sync += [
            If(self.reader.source.valid,
                self.reader.source.ready.eq(True),
               )
            .Elif(
                (self.ram_adr.started == False)
                & (rsv_level == 0),
                self.reader.source.ready.eq(False),
            )
            .Else(
                self.reader.source.ready.eq(True),
            ),
        ]

        # Palette Lookup
        self.specials.palette_port = palette_port = palette_memory.get_port()

        palette_data_done = Signal()
        palette_data_valid = Signal()
        palette_data = Signal(24)
        palette_data_buffer = Signal(24)
        # The index is the pixel's low byte; the BANK is whichever half of the
        # framebuffer is live, so a pixel and the palette entry it names always
        # come from the same frame.
        bank = palette_bank if palette_bank is not None else 0
        self.comb += [palette_data.eq(Mux(use_palette,
                                          palette_port.dat_r, palette_data_buffer)),
                      palette_port.adr.eq(Cat(ram_data[0:8], bank))
                      ]
        self.sync += [
            palette_data_buffer.eq(ram_data & 0x0FFFFFF),
            palette_data_valid.eq(ram_valid),
            palette_data_done.eq(ram_done),
        ]

        # Gamma Correction
        gamma_lut = _get_gamma_corr()
        gamma_data_done = Signal()
        gamma_data_valid = Signal()
        gamma_data = Signal().like(palette_data)
        self.sync += [
            gamma_data.eq(Cat(gamma_lut[palette_data[:8]],
                              gamma_lut[palette_data[8:16]],
                              gamma_lut[palette_data[16:24]])),
            gamma_data_valid.eq(palette_data_valid),
            gamma_data_done.eq(palette_data_done),
        ]

        # Buffer Writer
        # Calculate bits needed for column addressing
        columns_bits = (columns - 1).bit_length()  # e.g., 7 for 96 columns (fits in 7 bits)
        outputs_bits = (n_outputs - 1).bit_length()
        buffer_done = Signal()
        buffer_counter = Signal(columns_bits + 1 + chain_length_2 + outputs_bits)
        buffer_select = Signal(outputs_bits)
        buffer_address = Signal(columns_bits + 1 + chain_length_2)

        for i in range(n_outputs):
            self.sync += [
                If(gamma_data_valid,
                    buffer_write_port[i].dat_w.eq(gamma_data),
                    buffer_write_port[i].adr.eq(buffer_address),
                   )
            ]
        # Build buffer_address: handle chain_length_2=0 (no chain select bits)
        if chain_length_2 > 0:
            buffer_addr_cat = Cat(
                buffer_counter[columns_bits],
                buffer_counter[:columns_bits],
                buffer_counter[columns_bits + 1:columns_bits + 1 + chain_length_2]
            )
        else:
            buffer_addr_cat = Cat(
                buffer_counter[columns_bits],
                buffer_counter[:columns_bits]
            )
        self.comb += [
            buffer_select.eq(buffer_counter[columns_bits + 1 + chain_length_2:]),
            buffer_address.eq(buffer_addr_cat),
        ]
        # TODO Check if data & adress match
        self.sync += [
            If(gamma_data_valid,
                buffer_write_port[buffer_select - 1].we.eq(False),
                buffer_write_port[buffer_select].we.eq(True),
               buffer_counter.eq(buffer_counter + 1),)
            .Elif(gamma_data_done & (~buffer_done),
                  buffer_write_port[buffer_select - 1].we.eq(False),
                  buffer_counter.eq(0),
                  done.eq(True)),
            buffer_done.eq(gamma_data_done)
        ]


class RamAddressGenerator(Module):
    def __init__(
        self,
        start: Signal(1),
        enable: Signal(1),
        row,  # Row address signal
        image_width: Signal(16),
        panel_config,
        fb_base=None,
        columns=96,
        rows_per_half=24,
        chain_length_2=0,
        n_outputs=8,
    ):
        outputs_2 = (n_outputs - 1).bit_length()
        columns_bits = (columns - 1).bit_length()  # e.g., 7 for 96 columns
        counter = Signal(columns_bits + 1 + chain_length_2 + outputs_2)
        running = Signal(1)
        self.started = Signal()
        delay = 2
        en = Signal()
        counter_select = Signal(chain_length_2 + outputs_2)

        # Total count: columns * 2 (halves) * chains * outputs
        counter_max = columns * 2 * (1 << chain_length_2) * n_outputs - 1

        # Started
        self.comb += [
            en.eq((counter < delay) | enable),
            counter_select.eq(counter[(columns_bits + 1):]),
            running.eq(start | (counter != 0)),
        ]
        self.sync += [
            If(start, self.started.eq(True)),
            If((counter == 0) & start,
                self.started.eq(True),
                counter.eq(1))
            .Elif(counter == 0)
            .Elif((counter == counter_max) & en,
                counter.eq(0))
            .Elif((counter > 0) & en,
                  counter.eq(counter + 1)),
        ]

        # Delay 1
        cur_panel_config = Signal().like(panel_config[0].storage)
        config_lookup_valid = Signal()
        counter_previous = Signal().like(counter)
        collumn = counter_previous[:columns_bits]
        half_select = counter_previous[columns_bits]
        self.sync += [
            If(en,
                cur_panel_config.eq(panel_config[counter_select].storage),
                config_lookup_valid.eq(running),
                counter_previous.eq(counter))
        ]

        # Delay 2
        self.adr = Signal(32)
        self.valid = Signal(1)
        # Signal sizes depend on panel configuration
        row_comb = Signal(7)  # Enough for rows_per_half * 2
        x_offset = Signal(columns_bits)
        y_offset = Signal(7)
        col_max = columns - 1
        self.comb += [
            row_comb.eq(half_select * rows_per_half + row),  # Top: 0 to rows_per_half-1, Bottom: rows_per_half to rows-1
            Case((cur_panel_config >> 16) & 0x3, {
                 0b00: [x_offset.eq(collumn),
                        y_offset.eq(row_comb)],
                 0b01: [x_offset.eq((rows_per_half - 1) - row_comb),
                        y_offset.eq(collumn)],
                 0b10: [x_offset.eq(col_max - collumn),
                        y_offset.eq((rows_per_half - 1) - row_comb)],
                 0b11:[x_offset.eq(row_comb),
                       y_offset.eq(col_max - collumn)],
                 })
        ]
        # Use fb_base CSR if provided, otherwise fall back to hardcoded offset
        base_addr = fb_base if fb_base is not None else sdram_offset
        self.sync += [
            If(en,
               self.valid.eq(config_lookup_valid),
                self.adr.eq(
                    base_addr
                    + (y_offset +
                        ((cur_panel_config >> 8) & 0xFF) * 16)
                    * image_width + x_offset
                    + (cur_panel_config & 0xFF) * 16),
                If(self.valid & (~config_lookup_valid),
                    self.started.eq(False)))
        ]


class RowModule(Module):
    def __init__(
        self,
        start: Signal(1),
        clk: Signal(1),
        columns=96,
        chain_length_2=0,
    ):
        pipeline_delay = 1  # Can't change
        output_delay = 2
        delay = pipeline_delay + output_delay
        # Counter max: columns * 2 (halves) * chain_length + delay
        counter_max = columns * 2 * (1 << chain_length_2) + delay
        self.counter = counter = Signal(max=counter_max)
        buffer_counter = Signal(max=counter_max)
        self.buffer_select = buffer_select = Signal(1)
        self.shifting = Signal(1)
        self.comb += [
            buffer_select.eq(buffer_counter[0]),
        ]

        self.sync += [
            If(buffer_counter < output_delay, clk.eq(0)).Else(
                clk.eq(buffer_counter[0])
            ),
            buffer_counter.eq(counter),
            If((counter == 0) & start,
                counter.eq(1),
                self.shifting.eq(True))
            .Elif((counter == (counter_max - 1)),
                  counter.eq(0),
                  self.shifting.eq(False))
            .Elif((counter > 0),
                  counter.eq(counter + 1)),
        ]


# The six ways three channels can be ordered, indexed by the rgb_order CSR.
# Each entry says which LOGICAL colour the first, second and third HUB75 pin
# group carries, so the value reads like an LED part number: "GRB" means the
# pins labelled r/g/b carry green/red/blue.
RGB_ORDERS = ["RGB", "RBG", "GRB", "GBR", "BRG", "BGR"]


class Output(Module):
    def __init__(self, outputs_specific, buffer_readers, address, output_bit,
                 buffer_select, n_outputs=8, rgb_order=None):
        for i in range(n_outputs):
            out = outputs_specific[i]
            r_pins = Array([out.r0, out.r1])
            g_pins = Array([out.g0, out.g1])
            b_pins = Array([out.b0, out.b1])
            buffer_reader = buffer_readers[i]

            # Framebuffer word is 0x00GGRRBB -- R high, B middle, G low. That
            # layout is a property of the format, not of the panel, so the
            # permutation happens HERE, between the fetched word and the pins,
            # and nothing upstream has to know a panel is unusual.
            src = {
                "R": buffer_reader.dat_r[16:24],
                "G": buffer_reader.dat_r[0:8],
                "B": buffer_reader.dat_r[8:16],
            }

            if rgb_order is None:
                # No CSR wired (older callers): keep the fixed standard order
                # rather than inventing a signal, so behaviour is unchanged.
                feeds = [src["R"], src["G"], src["B"]]
            else:
                # One 8-bit 6-way mux per pin group. Cheap, and it costs
                # nothing at order 0 beyond the mux itself -- no extra memory
                # reads, no change to the shift train's timing.
                feeds = []
                for slot in range(3):
                    sig = Signal(8)
                    self.comb += Case(rgb_order, {
                        v: sig.eq(src[name[slot]])
                        for v, name in enumerate(RGB_ORDERS)
                    })
                    feeds.append(sig)

            for pins, colour in zip((r_pins, g_pins, b_pins), feeds):
                self.submodules += RowColorOutput(
                    pins,
                    output_bit,
                    buffer_select,
                    colour,
                )

            self.comb += [buffer_reader.adr.eq(address)]


class RowColorOutput(Module):
    def __init__(
        self,
        outputs: Array(Signal(1)),
        output_bit: Signal(3),
        buffer_select: Signal(1),
        color_input: Signal(8),
    ):
        outputs_buffer = Array((Signal()) for x in range(2))
        self.sync += [
            outputs_buffer[buffer_select].eq(
                color_input >> output_bit),
        ]

        self.sync += [If((buffer_select == 0), outputs[i].eq(outputs_buffer[i]))
                      for i in range(2)]
