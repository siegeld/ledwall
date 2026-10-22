"""32-bit words in, up to two RGB pixels out, one word per cycle.

WHY THIS EXISTS. The hardware pixel path narrows the 32-bit MAC stream to 8
bits (`Bytes32to8`) and parses byte-wise. At 35 MHz that is 35 MB/s against a
125 MB/s gigabit wire -- 3.6x too slow -- so `burst_fifo` is the only
elasticity in the design and any burst longer than it costs whole chunks,
which show on the glass as stale blocks. Measured 2026-10-21 on the live
card: `c_fifo_max` pinned at 4097 of 4096, `c_dropped` ~0.76/s.

The original narrowing had a real justification -- "the 10-byte header is not
a multiple of 4, so parsing at 32 bits leaves every RGB triple straddling
beats" -- but it also rested on an assumption the comment states outright: a
12.5 MB/s line rate, i.e. 100 Mbit. The link negotiates 1 Gbps, so that
premise is gone.

The straddling turns out not to apply to the PAYLOAD at all. Ethernet 14 +
IP 20 + UDP 8 + the app's 10-byte header = 52 bytes, and 52/4 = 13 exactly,
so pixel data always starts word-aligned. LCM(3,4) = 12, so every 3 words
carry exactly 4 pixels and only a residue of 0-2 bytes ever crosses a cycle.

THROUGHPUT. 125 MB/s at 35 MHz is 3.57 bytes/cycle, so one 32-bit word per
cycle (140 MB/s) is enough on the input. That is 1.19 pixels/cycle, so the
output is built to emit TWO pixels in a cycle.

That output width is NOT currently matched by the write side, and the note
that used to sit here -- "exactly what a 64-bit DRAM port consumes, so the two
sides match" -- was wrong. Measured 2026-10-21 with SdramWriteTester holding
`valid` high on the live card while streaming: the 32-bit write port sustains
0.671 writes/cycle, 1.49 cycles per write. That is 94 MB/s of the SDRAM's 140
MB/s peak (the bus is 32 bits at 35 MHz, two M12L16161A in parallel), and at
4 framebuffer bytes per pixel it caps the pixel path at 23.5 Mpx/s -- 70.5
MB/s of payload against a wire that fills at 125 MB/s.

So widening the input does not make drain exceed the wire; nothing available
here does. What it does is take the 8-bit parse off the critical path: 24 MB/s
to ~70 MB/s, which is roughly 2.2x the burst tolerance for the same FIFO. The
remaining limit is DRAM write bandwidth, and the two ways past THAT both cost
something a measurement cannot decide -- a 16-bit framebuffer (two pixels per
write, which would clear the wire, at RGB565 colour depth) or a 64-bit port
(which only helps if LiteDRAM charges its overhead per transaction rather than
per 32-bit beat, and the instance built to test that cost 5 EBR and dropped
the system clock to 31.24 MHz).

The second pixel output is kept anyway: it costs nothing, it is already
verified, and it is what either of those routes would need.

Byte order is LiteX's: the first wire byte sits in the LOW bits, so word W
holds b0=W[0:8], b1=W[8:16], b2=W[16:24], b3=W[24:32]. Within a pixel the
wire order is R,G,B, and `px` is packed {R,G,B} with R in the low byte --
the same packing `pixdma` already writes to DRAM.
"""

from migen import *


class Words32ToPixels(Module):
    """Unpack a word stream into RGB pixels, one or two per cycle.

    Input  (`valid`/`ready`/`data`/`last`): 32-bit words, `last` marking the
    final word of a payload.
    Output (`out_valid`/`out_ready`/`px0`/`px1`/`n_out`/`done`): `n_out` says
    how many of px0,px1 are valid, and is always 1 or 2 -- a word plus a
    residue is at least 4 bytes, so a whole pixel always comes out. `done`
    accompanies the final pixels of a payload.

    Because every accepted word yields at least one pixel and at most two,
    and the consumer takes up to two per cycle, the handshake is 1:1: one
    word in, one output beat out. That is what lets `ready` simply follow
    `out_ready` with no internal queue.

    A payload whose length is not a multiple of 3 leaves 1-2 bytes that
    cannot form a pixel. They are discarded at `last`, which is correct: a
    partial pixel is not a pixel. Carrying them would offset every pixel of
    the NEXT chunk by a byte or two -- a colour shear that looks like a
    palette fault rather than the alignment bug it is, so it is worth being
    deliberate about. Chunk lengths are validated upstream anyway.
    """

    def __init__(self):
        # --- input ---
        self.valid = Signal()
        self.ready = Signal()
        self.data = Signal(32)
        self.last = Signal()

        # --- output ---
        self.out_valid = Signal()
        self.out_ready = Signal()
        self.px0 = Signal(24)
        self.px1 = Signal(24)
        self.n_out = Signal(2)
        self.done = Signal()

        # --- residue preload ---
        # The pixel stream does NOT start on a word boundary. Measured with
        # `./build.sh sim-hwpath32`: LiteEth's depacketizers realign, so the
        # UDP payload arrives at lane 0 -- but the app's own header is 10
        # bytes, so pixel data starts at lane 2 of payload word 2. The two
        # pixel bytes already sitting in that word have to come in somehow.
        #
        # So the residue register is loadable. The header FSM consumes words
        # 0..2 itself and, on word 2, loads the top two bytes here; from word
        # 3 on it simply asserts `valid` and the window is already correct.
        # Drive this while `valid` is LOW -- a load and a beat in the same
        # cycle would be ambiguous, and the FSM has no reason to do both.
        self.res_set = Signal()
        self.res_set_val = Signal(16)
        self.res_set_n = Signal(2)

        # # #

        # Bytes carried over from the previous word: 0, 1 or 2 of them, in the
        # low bytes. Three would have formed a pixel and left. Bytes above
        # `res_n` are held at zero so the incoming word can simply be ORed in
        # at its byte offset -- a stale byte there would corrupt the overlap.
        res = Signal(16)
        res_n = Signal(2)

        # The working window: residue first, then this word's four bytes, so
        # 4-6 valid bytes -- two whole pixels at most, plus a new residue.
        win = Signal(48)

        self.comb += Case(res_n, {
            0: win.eq(self.data),
            1: win.eq(res[0:8] | (self.data << 8)),
            2: win.eq(res | (self.data << 16)),
        })

        accept = Signal()
        self.comb += [
            self.out_valid.eq(self.valid),
            self.ready.eq(self.out_ready),
            accept.eq(self.valid & self.out_ready),

            self.px0.eq(win[0:24]),
            self.px1.eq(win[24:48]),
            # res_n 0 -> 4 bytes -> 1 pixel; 1 -> 5 -> 1; 2 -> 6 -> 2.
            self.n_out.eq(Mux(res_n == 2, 2, 1)),
            self.done.eq(self.valid & self.last),
        ]

        self.sync += If(self.res_set,
            res.eq(self.res_set_val),
            res_n.eq(self.res_set_n),
        ).Elif(accept,
            If(self.last,
                res.eq(0),
                res_n.eq(0),
            ).Else(
                Case(res_n, {
                    0: [res.eq(win[24:32]), res_n.eq(1)],   # 1 byte over
                    1: [res.eq(win[24:40]), res_n.eq(2)],   # 2 bytes over
                    2: [res.eq(0), res_n.eq(0)],            # consumed exactly
                }),
            ),
        )
