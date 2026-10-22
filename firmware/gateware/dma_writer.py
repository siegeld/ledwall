# SDRAM write-path experiments for Tier 2 (pixels off the CPU).
#
# The display DMA re-reads the whole framebuffer every refresh and measures at
# ~79 MB/s, close to the theoretical ceiling of a 16-bit SDRAM at 40 MHz. The
# open question for Tier 2 is how much WRITE bandwidth is left, and what taking
# it costs the refresh rate.
#
# SdramWriteTester answers that without touching the ethernet stack: the CPU
# arms a burst of N words, hardware streams them through a LiteDRAMDMAWriter as
# fast as the bus allows, and reports the cycles taken. Words/cycle at a known
# clock gives achieved write bandwidth directly, and sampling the refresh
# counter across the burst gives the cost to the display.
#
# SPDX-License-Identifier: BSD-2-Clause

from migen import (If, Signal, Module, FSM, NextValue, NextState, Cat, Mux,
                   Case, Replicate)
from litex.soc.interconnect.csr import (AutoCSR, CSRConstant, CSRStorage,
                                        CSRStatus, CSRField)
from litedram.frontend.dma import LiteDRAMDMAWriter


class SdramWriteTester(Module, AutoCSR):
    """Burst-write engine used to measure available SDRAM write bandwidth.

    `data_width` exists to answer ONE question that cannot be read off the
    source: is LiteDRAM's write overhead charged per TRANSACTION or per 32-bit
    DRAM beat?

    It matters a great deal. Measured 2026-10-21 on the live card, streaming,
    this engine at data_width=32 sustains 0.671 writes/cycle -- 1.49 cycles per
    write, which is 94 MB/s of the SDRAM's 140 MB/s peak (the bus is 32 bits at
    35 MHz; two 16-bit M12L16161A in parallel). That is nearly identical to the
    1.34 cycles/write the live pixel path shows at 8 bits in, so the overhead
    does NOT amortize when the input gets faster -- it is structural.

    At 4 framebuffer bytes per pixel that caps writes at 23.5 Mpx/s, so 70.5
    MB/s of payload against a wire that fills at 125 MB/s. If the overhead is
    per TRANSACTION, a 64-bit port carries two pixels for the same 1.49 cycles
    -- 47 Mpx/s, 141 MB/s, which would clear the wire. If it is per BEAT, a
    64-bit write costs two beats and buys exactly nothing.

    Instantiating both widths and comparing is the only honest way to find out,
    and it is one build instead of a datapath rewrite resting on a guess.
    """

    def __init__(self, sdram, fifo_depth=16, data_width=32):
        port = sdram.crossbar.get_port(mode="write", data_width=data_width)
        self.data_width = CSRConstant(data_width)
        self.submodules.writer = writer = LiteDRAMDMAWriter(port, fifo_depth=fifo_depth)

        self.base = CSRStorage(24, description="First word address of the burst")
        self.length = CSRStorage(24, description="Words to write in the burst")
        self.ctrl = CSRStorage(fields=[
            # pulse=True is load-bearing, not decoration. Without it this field
            # latches, and because the FSM re-arms from IDLE whenever start is
            # high, the burst restarts the instant it finishes. busy is then low
            # for ~2 cycles in every ~262,144, so the CPU's poll almost never
            # sees the gap and keeps re-triggering -- making elapsed_ms and the
            # refresh delta span an unknown number of bursts instead of the
            # BURST_WORDS * REPEATS the caller asked for.
            CSRField("start", pulse=True,
                     description="Write 1 to arm a burst (self-clearing pulse)"),
        ])
        self.status = CSRStatus(fields=[
            CSRField("busy", description="Burst in progress"),
        ])
        self.cycles = CSRStatus(32, description="Sys-clk cycles taken by the last burst")
        self.written = CSRStatus(32, description="Words written by the last burst")

        adr = Signal(24)
        remaining = Signal(24)
        cyc = Signal(32)
        wrote = Signal(32)
        busy = Signal()

        self.comb += [
            self.status.fields.busy.eq(busy),
            writer.sink.address.eq(adr),
            # Data is the address itself: a pattern that is cheap to generate and
            # verifiable by reading it back over the existing CPU bus. Replicated
            # across a wider port so every byte lane carries something.
            writer.sink.data.eq(Replicate(adr, data_width // 24 + 1)[0:data_width]),
        ]

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(self.ctrl.fields.start & (self.length.storage != 0),
                NextValue(adr, self.base.storage),
                NextValue(remaining, self.length.storage),
                NextValue(cyc, 0),
                NextValue(wrote, 0),
                NextState("RUN"),
            ))
        fsm.act("RUN",
            busy.eq(1),
            writer.sink.valid.eq(1),
            NextValue(cyc, cyc + 1),
            If(writer.sink.ready,
                NextValue(adr, adr + 1),
                NextValue(wrote, wrote + 1),
                If(remaining == 1,
                    NextState("DONE"),
                ).Else(
                    NextValue(remaining, remaining - 1),
                )),
        )
        fsm.act("DONE",
            NextValue(self.cycles.status, cyc),
            NextValue(self.written.status, wrote),
            NextState("IDLE"),
        )


class Hub75UdpDma(Module, AutoCSR):
    """Write streamed pixels straight into the framebuffer, bypassing the CPU.

    Consumes the UDP payload of the bitmap protocol (8-bit stream) and drives a
    LiteDRAMDMAWriter. This is the point of Tier 2: the CPU's per-pixel loop
    costs 3 loads + 4 stores per 4 pixels over an uncached bus with no D-cache,
    which measures out at a hard ~600 kpx/s. Hardware doing the same work is
    limited only by the line rate.

    Wire format, little-endian, 10-byte header:
        0..1  magic 'B','M' (RGB888) or 'B','I' (indexed, 1 byte/px)
        2..3  frame_id   u16
        4     chunk_index u8
        5     total_chunks u8
        6..7  width      u16
        8..9  height     u16
        then pixels, chunk N carrying pixels N*pixels_per_chunk..

    INDEXED ('B','I') is the fast path and exists because the bound on streaming
    is per-interrupt cost, not pixels -- the CPU is already out of this loop.
    TODO.md item 9 measured ~1.13 packets per ISR entry and a ~20.6 fps ceiling
    at 256x192, where a frame is ~101 chunks. One byte per pixel instead of three
    puts the SAME MTU to work on 3x the pixels, so a frame is ~34 chunks and the
    interrupt bound moves by the same 3x.

    The pixels-per-chunk relationship therefore falls out of the format rather
    than needing its own register: an indexed chunk carries exactly 3x the pixels
    of an RGB chunk of identical size. Senders must use `3 * pixels_per_chunk`
    for indexed, which is what the CPU's own parser assumes too.

    The framebuffer format is UNCHANGED -- an index is written as 0x000000II,
    the low byte the palette memory already reads (`ram_data & 0x000FF`). So this
    needs no change to the scan-out path, no change to write_img_*, and it is
    unaffected by the per-panel rotation constraint that limits index PACKING.
    See docs/DISPLAY-PROGRAMS.md.

    The CPU still receives every packet through its own copy of the stream, so
    it keeps the arrival mask, completion detection, repair and buffer swap
    exactly as before. Only the pixel copy moves to hardware.
    """

    def __init__(self, sdram, udp_sink, display_base, fb_base, half_words,
                 pixels_per_chunk=487, fifo_depth=64, stall_limit=8192):
        port = sdram.crossbar.get_port(mode="write", data_width=32)
        self.submodules.writer = writer = LiteDRAMDMAWriter(port, fifo_depth=fifo_depth)

        # The write base is DERIVED from the display's fb_base, never written
        # separately. When the CPU owned both registers a buffer swap took two
        # CSR writes, and between them the display and this DMA both pointed at
        # the same half -- so a chunk header parsed in that window latched
        # pix_adr into the buffer being displayed and put a band of the next
        # frame on screen. One register, written once, flips both atomically.
        self.base = CSRStatus(24, description="Derived framebuffer write address (read-only)")
        self.limit = CSRStorage(24, description="Pixels in the image; writes past this are dropped")
        self.ctrl = CSRStorage(fields=[
            CSRField("enable", description="Enable hardware pixel writes"),
        ])
        self.pixels = CSRStatus(32, description="Pixels written")
        self.chunks = CSRStatus(32, description="Chunks accepted")
        self.bad_magic = CSRStatus(32, description="Payloads rejected on magic")
        self.last_chunk = CSRStatus(8, description="Most recent chunk_index")
        self.status = CSRStatus(fields=[
            CSRField("busy", description="A payload is being written"),
        ])
        self.stalls = CSRStatus(32, description="Payloads abandoned after a DRAM stall")

        # SUB-WATCHDOG stall visibility. `stalls` above only increments once a
        # payload has been blocked for `stall_limit` (8192 cycles = 234 us at
        # 35 MHz) and is abandoned, so it reads 0 whenever the write path is
        # merely slow. That is exactly the case that matters: measured
        # 2026-10-21, `stalls` was 0 while the gate was still dropping ~2
        # packets/s, which means `burst_fifo` (2048 beats, ~5.5 packets) filled
        # without any single stall reaching the watchdog -- and nothing in the
        # design could say how long the writer actually waited.
        #
        # Without these two the question is unfalsifiable. The DRAM budget is
        # ~4% (reader 2.09%, writer 1.87% of a 1-access/clock port), so "the
        # memory is busy" does not survive arithmetic, yet the FIFO demonstrably
        # fills. These say whether the writer is blocked in long contiguous
        # runs (arbiter starvation or refresh storms) or in many short ones
        # (per-access overhead), and those want different fixes.
        #
        # `stall_peak` is the high-water mark of the same counter that feeds the
        # watchdog, so it is directly comparable to stall_limit: 8192 means the
        # watchdog fired, 4000 means it came half way without being counted.
        self.stall_peak = CSRStatus(16, description=(
            "Longest run of consecutive cycles the writer spent in PIX without "
            "accepting a beat since the last peak_clear. Compare against "
            "stall_limit (8192); that value means it SATURATED."))
        # Clearable, for the reason written out at length next to
        # smoleth's c_fifo_clear: an uncleared peak is read as a rate sooner or
        # later. This one read 8193 -- its saturation value -- from three events
        # over the lifetime of the configuration, and I quoted it as though it
        # described the current state.
        self.peak_clear = CSRStorage(1, description=(
            "Write 1 to reset stall_peak, so it reads as a window"))
        self.stall_cycles = CSRStatus(32, description=(
            "Total cycles spent in PIX without accepting a beat. Against "
            "pix_cycles this is the write path's blocked fraction."))
        self.pix_cycles = CSRStatus(32, description=(
            "Total cycles spent in PIX, the denominator for stall_cycles"))

        # SPACING BETWEEN WRITES, so a chunk cannot monopolise the memory.
        #
        # The S-PWM scan-out reads every pixel ON DEMAND -- it has no row
        # buffer (see Icn1065PixelSource) -- so a read that arrives late makes
        # the serialiser shift whatever was on the bus, which is a white dot on
        # the glass. A chunk is 487 pixels written back to back, and that burst
        # starves the reads for its whole length.
        #
        # Measured on the bench 2026-10-20, same detailed image throughout:
        # no writes at all is PERFECTLY clean; 245 chunks/s shows a little
        # snow; 1360 chunks/s shows a lot. The fault tracks write traffic and
        # nothing else -- solid colours never show it, because a stale pixel
        # equals the correct one.
        #
        # So leave gaps. `n` idle cycles between writes caps the DMA at one
        # word per (n+1) clocks, which is bandwidth the display gets back. It
        # costs the DMA almost nothing: a 32,768 px frame at one write per four
        # clocks is 3.7 ms, against a 50 ms frame period at 20 fps.
        #
        # A CSR because the right value is a glass measurement, and a rebuild
        # per guess is what made `clk_div` expensive to find. 0 is the old
        # behaviour, full speed.
        self.throttle = CSRStorage(4, reset=0, description=(
            "Idle cycles between pixel writes. 0 = full speed. Buys read "
            "bandwidth back for the scan-out, which has no row buffer."))

        # Which chunks the hardware ACTUALLY wrote, for the frame it is writing.
        #
        # Without this the CPU infers completion from packets it received, which
        # is a different set: the always-ready gate drops whole packets under
        # sustained load and the watchdog below abandons payloads when DRAM
        # stalls. Neither is visible to the CPU, so it marked frames complete
        # that had never been written and presented them with stale bands.
        # These bits are set only when a chunk finishes writing.
        #
        # A WINDOW, not sixteen registers each. `arrival` and `arrival_done`
        # below are Signal(512) -- a four-panel RGB888 frame is 270 chunks and
        # `chunk_index[0:9]` sets bits up to 511 -- so the CPU needs 512 bits
        # of each. Exporting them as 16 + 16 CSRStatus(32) worked and cost
        # about 940 LUTs and 4% of the device, because every CSR in a bank
        # widens that bank's read mux: the build went from 75% to 79% LUT and
        # Ethernet RX fell from 131 MHz to 118 against a 125 MHz requirement,
        # at two different placement seeds.
        #
        # So select instead of enumerate. `bitmap_sel` picks a 32-bit word and
        # the two status registers present that word of each bitmap: three CSRs
        # instead of thirty-two, one 16:1 mux instead of thirty-two extra
        # entries in the bank's mux, and the same 512 bits reachable.
        #
        # The cost is two bus transactions per word instead of one. That is
        # paid once per frame in `dma_arrival`/`dma_done_frame`, against a
        # 30 Hz frame rate, and it buys back the timing margin.
        self.bitmap_sel = CSRStorage(4, description=(
            "Which 32-bit word of the chunk bitmaps `bitmap_arrival` and "
            "`bitmap_done` present. 0 is chunks 0-31, 15 is chunks 480-511."))
        self.bitmap_arrival = CSRStatus(32, description=(
            "The `bitmap_sel`-th word of the LIVE chunk arrival bitmap"))
        self.bitmap_done = CSRStatus(32, description=(
            "The `bitmap_sel`-th word of the COMPLETED frame's bitmap"))
        self.frame_id = CSRStatus(16, description="frame_id the arrival bitmap describes")

        # Frame IDENTITY, published so the CPU can track frames it never sees.
        #
        # bitmap_udp.rs learns frame_id and total_chunks from packet headers it
        # parses itself. That is fine while every pixel packet also reaches the
        # CPU's MAC, and it is a hard blocker for RB-5486: filter port 7000 away
        # from the CPU and `current_frame_id` stays u16::MAX forever, so
        # merge_dma_arrival() rejects every bitmap as belonging to another frame
        # and the display freezes with correct pixels already in SDRAM.
        #
        # frame_id above was already independent of the CPU. These two complete
        # the set, and all three are committed together at hdr_idx == 9 so a
        # reader can never pair a new frame's total_chunks with an old frame_id.
        # SIXTEEN bits: `total_chunks_hdr` is Signal(16) and the wide header
        # carries a u16. At 8 bits a 270-chunk frame published 270 & 0xff = 14,
        # so a reader comparing `received >= total` called the frame complete
        # after ~5% of it had arrived and presented a torn frame.
        self.total_chunks = CSRStatus(16, description="total_chunks for frame_id")
        self.indexed = CSRStatus(1, description="1 if frame_id is indexed ('B','I'), 0 if RGB888")

        # DOUBLE BUFFER: the bitmap of the frame that just FINISHED.
        #
        # `arrival` above is single-buffered and cleared the instant a new
        # frame_id is parsed, so a reader has to catch it in the gap between the
        # last chunk of one frame and the first packet of the next -- one
        # inter-packet time. The firmware samples at 1 kHz, and that gap is
        # 0.5 ms at 13 fps on a 9-panel frame, so it missed about half of them:
        # measured 40/40 frames shown at a 10 ms gap, 34/40 at 0.8 ms, 1/40 at
        # 0.5 ms. The chunks were all there; nobody could read them in time.
        #
        # On a frame change the outgoing bitmap and its identity are latched
        # here BEFORE `arrival` is cleared, so the CPU can read a finished frame
        # whenever it gets round to it. `done_seq` increments with each latch,
        # which is how a reader tells a new snapshot from one it has already
        # presented -- frame_id alone cannot, since it wraps at 16 bits.
        # The completed-frame bitmap is read through `bitmap_done` above.
        self.frame_id_done = CSRStatus(16, description="frame_id the completed bitmap describes")
        self.total_chunks_done = CSRStatus(16, description="total_chunks of the completed frame")
        self.indexed_done = CSRStatus(1, description="1 if the completed frame was indexed")
        self.done_seq = CSRStatus(16, description="Increments each time a frame is latched")

        # Mirrors hub75's auto_swap, so this module knows whether the base is
        # about to move under it.
        self.auto_swap = Signal()

        # One-cycle pulse at the frame boundary, for the hub75 hardware swap.
        # Fires in HDR, before the first PIX write of the new frame, so the
        # display flips to the finished frame while the incoming one still has
        # nothing in the buffer it is about to be given.
        self.swap_req = Signal()

        # Two halves: the DMA always targets whichever one the display is not
        # reading. Written as an explicit compare rather than an XOR so it does
        # not silently depend on the two bases differing in exactly one bit.
        write_base = Signal(24)
        self.comb += [
            If(display_base == fb_base,
                write_base.eq(fb_base + half_words),
            ).Else(
                write_base.eq(fb_base),
            ),
            self.base.status.eq(write_base),
        ]

        sink = udp_sink
        hdr_idx = Signal(4)
        # 16 bits, not 8: the WIDE header ('B','W') carries u16 counters so a
        # frame may exceed 255 chunks. Four P1.25 panels on one card is
        # 512x256 = 131,072 px = 270 chunks, which a u8 cannot express.
        chunk_index = Signal(16)
        wide = Signal()
        # Last header byte: 9 narrow, 11 wide (two extra bytes of counters).
        # A Mux rather than a duplicated branch, so the frame-start logic that
        # fires on the final header byte exists in exactly one place.
        hdr_last = Signal(4)
        pix_adr = Signal(24)
        sub = Signal(2)          # which byte of the RGB triple (RGB888 only)
        r = Signal(8)
        g = Signal(8)
        # Set by the magic: 'B','I' rather than 'B','M'. Latched per payload.
        indexed = Signal()
        # Pixels this chunk_index is offset by. An indexed chunk of the same wire
        # size carries 3x the pixels, so this is a shift-add, not a register.
        chunk_pixels = Signal(24)
        self.comb += hdr_last.eq(Mux(wide, 11, 9))
        self.comb += If(indexed,
                        chunk_pixels.eq(chunk_index * (3 * pixels_per_chunk)),
                     ).Else(
                        chunk_pixels.eq(chunk_index * pixels_per_chunk),
                     )
        enable = self.ctrl.fields.enable
        # Watchdog. If the DRAM writer stops accepting -- the display DMA can
        # hold the bus for a long time -- this FSM would sit in PIX forever, and
        # because everything upstream backpressures into it the entire hardware
        # path wedges behind one packet. Measured: chunks froze at exactly 204
        # (3 frames) while the UDP filter kept counting. Abandon the payload
        # instead; a dropped chunk is repaired from the previous frame, a wedged
        # pipeline is not recoverable.
        stall = Signal(max=stall_limit + 1)
        progress = Signal()
        frame_id = Signal(16)
        total_chunks_hdr = Signal(16)
        # 512 arrival bits, not 256: enough for four panels (270 chunks) with
        # headroom. The wide header technically allows 65,535, but the
        # framebuffer bounds a real frame long before that.
        arrival = Signal(512)
        arrival_done = Signal(512)
        set_arrival = Signal()
        clear_arrival = Signal()

        in_range = Signal()
        self.comb += in_range.eq(pix_adr < (write_base + self.limit.storage))

        # Throttle countdown. Non-zero means "do not issue a write this cycle";
        # it reloads from the CSR after each write and counts down regardless
        # of state, so a gap is never carried across a chunk boundary into a
        # stall that the watchdog would then count.
        gap = Signal(4)
        gap_ok = Signal()
        self.comb += gap_ok.eq(gap == 0)
        self.sync += If(~gap_ok, gap.eq(gap - 1))

        # Where the NEXT frame's pixels go.
        #
        # swap_req flips hub75's fb_base_eff, but a NextValue lands at the END of
        # the cycle, and pix_adr for the incoming frame is computed in that same
        # cycle at hdr_idx == 9. Using write_base there puts the new frame's
        # FIRST CHUNK in the buffer that is now being displayed -- a glitch at
        # the top of the image on every single frame, which is exactly what
        # driving the swap from gateware looked like before this existed.
        #
        # So the frame-change case is computed explicitly rather than waiting a
        # cycle for the register to catch up.
        write_base_next = Signal(24)
        self.comb += [
            If(self.swap_req & self.auto_swap,
                If(write_base == fb_base,
                    write_base_next.eq(fb_base + half_words),
                ).Else(
                    write_base_next.eq(fb_base),
                ),
            ).Else(
                write_base_next.eq(write_base),
            ),
        ]

        # HUB75 framebuffer word format is 0x00GGRRBB. In indexed mode the
        # palette lookup reads `ram_data & 0x000FF`, so the index goes in the low
        # byte and the rest is zero -- same framebuffer layout either way.
        word = Signal(32)
        self.comb += If(indexed,
                        word.eq(Cat(sink.data, Signal(24))),
                     ).Else(
                        # Cat is LSB-first, so this is R in [0:8], G in [8:16],
                        # B in [16:24] -- 0x00BBGGRR.
                        #
                        # NOT 0x00GGRRBB, which is what this and
                        # write_img_rgb888() both produced until 2026-09-18 and
                        # what the docs still claimed. Measured end to end on the
                        # bench panel with solid frames: sending pure RED lit
                        # GREEN, pure GREEN lit BLUE, pure BLUE lit RED -- one
                        # cyclic rotation, consistently. The scan-out takes
                        # red from dat_r[16:24], green from [0:8] and blue from
                        # [8:16] (hub75.py), and with this panel's wiring that
                        # comes out as 0x00BBGGRR at the glass.
                        #
                        # Indexed mode is unaffected: it puts the palette index
                        # in the low byte and the palette RAM is its own path.
                        word.eq(Cat(r, g, sink.data, Signal(8))),
                     )

        self.comb += [
            writer.sink.address.eq(pix_adr),
            writer.sink.data.eq(word),
        ]

        self.sync += [
            If(clear_arrival,
                arrival.eq(0),
            ).Elif(set_arrival,
                # Shift by the LOW 9 bits only. chunk_index is 16 bits for the
                # wide header, and `1 << chunk_index` at that width would build
                # a 65,536-bit shifter for a mask that is 512 wide. A frame
                # claiming more chunks than the mask holds is rejected on the
                # firmware side; here we simply never set a bit out of range.
                arrival.eq(arrival | (1 << chunk_index[0:9])),
            ),
        ]
        # One 16:1 window over both bitmaps, selected by `bitmap_sel`.
        self.comb += Case(self.bitmap_sel.storage, {
            i: [self.bitmap_arrival.status.eq(arrival[32 * i:32 * (i + 1)]),
                self.bitmap_done.status.eq(arrival_done[32 * i:32 * (i + 1)])]
            for i in range(16)
        })

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            sink.ready.eq(1),
            If(sink.valid & enable,
                NextValue(hdr_idx, 1),
                If(sink.data == 0x42,          # 'B'
                    NextState("HDR"),
                ).Else(
                    NextValue(self.bad_magic.status, self.bad_magic.status + 1),
                    If(~sink.last, NextState("DROP")),
                )),
            # Consume and ignore while disabled.
            If(sink.valid & ~enable, sink.ready.eq(1)),
        )
        fsm.act("HDR",
            self.status.fields.busy.eq(1),
            sink.ready.eq(1),
            If(sink.valid,
                NextValue(hdr_idx, hdr_idx + 1),
                If(hdr_idx == 1,
                    If(sink.data == 0x4D,          # 'M' -- RGB888
                        NextValue(indexed, 0),
                        NextValue(wide, 0),
                    ).Elif(sink.data == 0x57,      # 'W' -- RGB888, u16 counters
                        NextValue(indexed, 0),
                        NextValue(wide, 1),
                    ).Elif(sink.data == 0x49,      # 'I' -- indexed, 1 byte/px
                        NextValue(indexed, 1),
                        NextValue(wide, 0),
                    ).Elif(sink.data == 0x50,      # 'P' -- palette, CPU's job
                        # Not ours, and NOT a bad packet. The CPU sees the same
                        # stream and writes the palette from it; counting these
                        # as bad_magic would make that counter climb steadily
                        # during normal operation and stop being a fault signal.
                        #
                        # "The CPU sees the same stream" is a DEPENDENCY, not an
                        # observation, and it was broken for a while: the pixel
                        # classifier in smoleth.py kills traffic to the pixel
                        # port before the CPU's MAC, and a palette goes to that
                        # same port. Killed there and dropped here, the palette
                        # reached nobody -- indexed frames drew the right shape
                        # in stale colours. The classifier now spares the 'BP'
                        # magic specifically. If you ever widen what it kills,
                        # this drop is what makes that a silent colour bug.
                        NextState("DROP"),
                    ).Else(
                        NextValue(self.bad_magic.status, self.bad_magic.status + 1),
                        NextState("DROP"),
                    ),
                ),
                If(hdr_idx == 2, NextValue(frame_id[0:8], sink.data)),
                If(hdr_idx == 3, NextValue(frame_id[8:16], sink.data)),
                # Narrow: chunk_index u8 at 4, total u8 at 5, header ends at 9.
                # Wide:   chunk_index u16 at 4..5, total u16 at 6..7, ends at 11.
                If(~wide,
                    If(hdr_idx == 4,
                        NextValue(chunk_index, sink.data),
                        NextValue(self.last_chunk.status, sink.data),
                    ),
                    If(hdr_idx == 5, NextValue(total_chunks_hdr, sink.data)),
                ).Else(
                    If(hdr_idx == 4,
                        NextValue(chunk_index[0:8], sink.data),
                        NextValue(self.last_chunk.status, sink.data),
                    ),
                    If(hdr_idx == 5, NextValue(chunk_index[8:16], sink.data)),
                    If(hdr_idx == 6, NextValue(total_chunks_hdr[0:8], sink.data)),
                    If(hdr_idx == 7, NextValue(total_chunks_hdr[8:16], sink.data)),
                ),
                If(hdr_idx == hdr_last,
                    # New frame: the bitmap describes one frame at a time.
                    If(frame_id != self.frame_id.status,
                        clear_arrival.eq(1),
                        self.swap_req.eq(1),
                        # Latch the OUTGOING frame before clearing it. These are
                        # the pre-update values by construction: NextValue lands
                        # at the end of the cycle, so .status still holds the
                        # frame that is finishing.
                        NextValue(arrival_done, arrival),
                        NextValue(self.frame_id_done.status, self.frame_id.status),
                        NextValue(self.total_chunks_done.status, self.total_chunks.status),
                        NextValue(self.indexed_done.status, self.indexed.status),
                        NextValue(self.done_seq.status, self.done_seq.status + 1),
                        NextValue(self.frame_id.status, frame_id),
                        # Committed with frame_id, never before it.
                        NextValue(self.total_chunks.status, total_chunks_hdr),
                        NextValue(self.indexed.status, indexed),
                    ),
                    # Header consumed; pixels start on the next beat.
                    # write_base_NEXT: on a frame change the base moves this
                    # cycle, and write_base still holds the outgoing value.
                    NextValue(pix_adr, write_base_next + chunk_pixels),
                    NextValue(sub, 0),
                    NextValue(self.chunks.status, self.chunks.status + 1),
                    NextState("PIX"),
                ),
                If(sink.last, NextState("IDLE")),
            ))
        self.sync += [
            If(~self.fsm.ongoing("PIX"),
                stall.eq(0),
            ).Elif(progress,
                stall.eq(0),
            ).Elif(stall != stall_limit,
                stall.eq(stall + 1),
            ),
        ]

        # Instrumentation for the counters declared above. Sampled from the
        # same `stall`/`progress` signals the watchdog uses, so the three
        # numbers can never disagree about what counts as blocked.
        #
        # stall_peak updates on the cycle stall INCREMENTS rather than when it
        # resets: a stall that is still in progress is already visible, so a
        # run that never ends (the wedge this watchdog exists for) is not
        # invisible in the peak.
        self.sync += [
            If(self.fsm.ongoing("PIX"),
                self.pix_cycles.status.eq(self.pix_cycles.status + 1),
                If(~progress,
                    self.stall_cycles.status.eq(self.stall_cycles.status + 1),
                    If(stall + 1 > self.stall_peak.status,
                        self.stall_peak.status.eq(stall + 1),
                    ),
                ),
            ),
            If(self.peak_clear.re, self.stall_peak.status.eq(0)),
        ]

        fsm.act("PIX",
            self.status.fields.busy.eq(1),
            If(stall == stall_limit,
                NextValue(self.stalls.status, self.stalls.status + 1),
                NextState("DROP"),
            ),
            # RGB888: only the third byte of each triple emits a word, so the
            # first two are always accepted and the third waits on the DMA.
            # Indexed: every byte is a pixel, so every byte waits on the DMA.
            If(~indexed & (sub != 2),
                sink.ready.eq(1),
                If(sink.valid,
                    progress.eq(1),
                    If(sub == 0, NextValue(r, sink.data)).Else(NextValue(g, sink.data)),
                    NextValue(sub, sub + 1),
                ),
            ).Else(
                # `gap_ok` gates the HANDSHAKE, not just the write: holding
                # sink.ready low is what makes the upstream wait, and it is the
                # waiting that hands the memory to the display.
                sink.ready.eq(writer.sink.ready & gap_ok),
                writer.sink.valid.eq(sink.valid & in_range & gap_ok),
                If(sink.valid & writer.sink.ready & gap_ok,
                    progress.eq(1),
                    NextValue(sub, 0),
                    NextValue(gap, self.throttle.storage),
                    NextValue(pix_adr, pix_adr + 1),
                    # Count only writes actually issued. Counting the ones the
                    # limit suppressed made `pixels` look healthy while a stale
                    # bound was discarding everything past it.
                    If(in_range,
                        NextValue(self.pixels.status, self.pixels.status + 1),
                    ),
                ),
            ),
            If(sink.valid & sink.ready & sink.last,
                # Only now, having written the whole payload without a stall
                # abort, is this chunk genuinely present in the framebuffer.
                set_arrival.eq(1),
                NextState("IDLE"),
            ),
        )
        fsm.act("DROP",
            sink.ready.eq(1),
            If(sink.valid & sink.last, NextState("IDLE")),
        )
