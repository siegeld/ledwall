"""ICN1065L Scramble-PWM output stage.

See docs/ICN1065.md for the protocol. Nothing here is on the critical path for
existing panels -- `hub75.py` still drives those, unchanged.

The chip has no command line: it distinguishes a command from pixel data purely
by counting how many CLK cycles LAT stays high. So the foundation of the whole
driver is a generator that emits "LAT high for N clocks, then M clocks of
padding with LAT low", shifting a 16-bit word out on the data lines meanwhile.
Everything else -- the frame prefix, the register writes, the row scanner -- is
a sequence of those.
"""

from functools import reduce

from spwm_chips import (CLK_PAD, DEFAULT_CHIP, MAX_REGS, MAX_WRAPPERS,
                        PRE_ACT_END, PRE_ACT_MID, REG_LATCH, V_SYNC)

from migen import *

# Command lengths, in CLK cycles of LAT-high. From ICN1065_* in the reference
# driver's leddrivers.h; see docs/ICN1065.md.
# Command lengths now live in spwm_chips.py with the tables they belong to, and
# are re-exported here so existing callers and docs keep working.
PRE_ACT1 = PRE_ACT_MID
PRE_ACT2 = PRE_ACT_END

# Magic words that bracket a register write, distinguishing it from pixel data.
WRAPPER = (0x00AA, 0x01AA, 0x0055, 0x0155)

# The 38 configuration registers: high byte is the index, low byte the value.
REG_VALUE = [
    0x00aa, 0x01aa, 0x022a, 0x0335, 0x0412, 0x0500, 0x0601, 0x0720,
    0x0c18, 0x0d01, 0x0e86, 0x0f01, 0x1040, 0x1127, 0x1200, 0x1300,
    0x1400, 0x1500, 0x1600, 0x1800, 0x1906, 0x1c60, 0x1dca, 0x1e73,
    0x1f00, 0x2000, 0x2100, 0x2200, 0x2300, 0x2400, 0x2500, 0x2600,
    0x2700, 0x7000, 0x7100, 0x7200, 0x7300, 0x74a0,
]
REG_CNT = len(REG_VALUE)
CFG_SCAN = 2         # index of the scan-line-count register

# Row scanner. Each OE pulse advances the chip's internal scan counter by one;
# VSYNC resets it. OE is ACTIVE HIGH on this part -- the opposite of a plain
# HUB75 panel, and an easy way to get a dark display.
ROW_OE_LEN = 128     # clocks per row slot
ROW_OE_CNT = 1       # OE pulses per slot
ROW_OE_PULSE = 2     # clocks per pulse: rising edge + falling edge
ROW_OE_ADD_LEN = ROW_OE_LEN - ROW_OE_CNT * ROW_OE_PULSE   # 126 hold clocks

CHANNELS_PER_CHIP = 16   # constant-current outputs on one driver IC
SPWM_BITS = 16       # bits clocked per pixel
GREY_BITS = 13       # of which the low 13 are greyscale; 15..13 must be zero.
                     #
                     # WHY 13, FROM THE DATASHEET (11.7.5): the displayable PWM
                     # grey range is 4 * (reg0x04 + 1) * (reg0x03 + 1) - 1. The
                     # factory registers for the DP3264S module are 0x04 = 0x1F
                     # (128 steps per row) and 0x03 = 0x3F (64 display groups):
                     # 4 * 32 * 64 - 1 = 8191, thirteen bits -- the vendor
                     # profile's "grayScale 8192". The part CAN take 14 (with
                     # 0x04 = 0x3F); fed 14-bit words at the factory setting it
                     # saturated above ~code 32 and showed full scale as black
                     # (gateware 1.21.0, glass 2026-10-19). 12 bits used half the
                     # range. The depth therefore belongs with the chip table's
                     # register values, not as a constant here -- see TODO in
                     # E320-BRINGUP 4.10.

# RGB groups carried by one connector, which is the ONLY thing that differs
# between the HUB75 and HUB320 wiring standards as far as this file is
# concerned. A group is one set of R/G/B data lines driving one horizontal
# slice of the panel, so `groups` slices are shifted in parallel and the
# module is `groups * rows_per_frame` pixels tall.
#
# The protocol on the wires is identical either way -- same CLK, LAT and OE,
# same driver ICs, same register sequence. HUB320 is more wires, not a
# different conversation, and it costs no extra bandwidth PER PIXEL: a request
# fetches `groups` pixels instead of two, but each covers proportionally more
# panel. What it buys is pin count and cable count.
GROUPS_HUB75 = 2     # R1G1B1 / R2G2B2 -- 6 data lines
GROUPS_HUB320 = 4    # four slices    -- 12 data lines


def scan_register(rows_per_frame):
    """Register 2's value for a given scan depth.

    The reference table carries 0x022a -- 43 rows, its own panel. A 256x128
    module at 1/64 scan needs 0x023f. This is the one register that must be
    computed rather than copied, and the first thing to doubt if a panel stays
    dark.
    """
    assert 1 <= rows_per_frame <= 256
    return 0x0200 | (rows_per_frame - 1)


class SpwmCommand(Module):
    """Emit one command: LAT high for `length` clocks, then `pad` clocks low.

    Data is shifted out MSB-first on `data_out` throughout, one bit per clock,
    so a register write is just a command whose payload happens to be the
    register word. `start` is a one-cycle strobe; `busy` falls when the command
    including its padding has been emitted.

    The whole driver rests on this, so it is deliberately small enough to
    simulate exhaustively -- see tools/sim_icn1065.py.
    """

    def __init__(self, data_width=16):
        self.start = Signal()
        self.length = Signal(max=32)        # LAT-high clocks
        self.pad = Signal(max=32)           # trailing LAT-low clocks
        self.data = Signal(data_width)      # shifted out MSB first

        self.lat = Signal()
        self.data_out = Signal()
        self.busy = Signal()

        # # #

        shift = Signal(data_width)
        count = Signal(max=64)
        # `pad` has to be LATCHED at `start`, for the same reason `length` is
        # captured into `count` there: the caller drives both from inside a
        # ONE-CYCLE state (`SpwmPrefix`'s "CMD"), and `pad` is not read until
        # `count` runs out 3-14 cycles later, by which time the driver is gone
        # and the input reads 0. That made `If(self.pad == 0)` always true, so
        # the PAD state below never executed and every command was separated
        # by the 2 clocks the FSM takes to come back round instead of the
        # CLK_PAD=8 the design budgets (see `frame_clocks()`). The command
        # grammar is "LE high N clocks, then M clocks LOW" -- the low interval
        # is how the chip separates one command from the next, so the prefix
        # was issuing VSYNC -> PRE_ACT -> WR_CFG at a quarter of the intended
        # separation.
        pad_l = Signal(max=64)

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(self.start,
                NextValue(shift, self.data),
                NextValue(count, self.length),
                NextValue(pad_l, self.pad),
                NextState("LAT_HIGH"),
            ),
        )
        fsm.act("LAT_HIGH",
            self.busy.eq(1),
            self.lat.eq(1),
            self.data_out.eq(shift[-1]),
            NextValue(shift, Cat(0, shift)),
            NextValue(count, count - 1),
            If(count == 1,
                NextValue(count, pad_l),
                # A zero-length pad must not wrap the counter into a 64-clock
                # command; go straight back to idle instead.
                If(pad_l == 0,
                    NextState("IDLE"),
                ).Else(
                    NextState("PAD"),
                ),
            ),
        )
        fsm.act("PAD",
            self.busy.eq(1),
            self.lat.eq(0),
            self.data_out.eq(shift[-1]),
            NextValue(shift, Cat(0, shift)),
            NextValue(count, count - 1),
            If(count == 1, NextState("IDLE")),
        )


class SpwmRegBlock(Module):
    """One register-write block: the 16-bit word repeated, LAT at the END.

    This is where the README's prose misleads and the source does not. "0x00AA
    data, 5 LAT clocks" reads as a five-clock command -- but a 16-bit word
    cannot shift out in five clocks. `setDataRegBuffer_n()` in the reference
    shows what actually happens: the block is `block_len` clocks long (one full
    row), the word is shifted MSB-first ONCE PER DRIVER IC to fill it, and LAT
    is asserted only for the final `REG_LATCH` clocks.

    Each ICN1065 has 16 channels, so a 256-column module has 16 of them in the
    chain and 16 x 16 bits exactly fills the row. That is not a coincidence --
    it is why the block is a row long.
    """

    def __init__(self, block_len, data_width=16, lanes=3):
        assert block_len % data_width == 0, (
            f"block_len {block_len} must be a whole number of {data_width}-bit "
            "words -- one per driver IC in the chain")
        self.repeats = block_len // data_width
        self.start = Signal()
        self.busy = Signal()
        self.lat = Signal()
        # THREE lanes, not one. The R, G and B chips on a module are separate
        # parts and their configuration does not have to agree -- DP3364S
        # captures carry different values per colour line, and Falcon Player's
        # ICND1065L profiles do too. Sending one word to all three, which this
        # did until the chip table was generalised, is only correct when the
        # table happens to be uniform.
        self.data = [Signal(data_width) for _ in range(lanes)]
        self.data_out = Signal(lanes)

        # # #

        shift = [Signal(data_width) for _ in range(lanes)]
        bit = Signal(max=data_width)
        rep = Signal(max=max(2, self.repeats + 1))
        left = Signal(max=block_len + 1)

        self.comb += [self.data_out[i].eq(shift[i][-1]) for i in range(lanes)]

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(self.start,
                *[NextValue(shift[i], self.data[i]) for i in range(lanes)],
                NextValue(bit, data_width - 1),
                NextValue(rep, self.repeats - 1),
                NextValue(left, block_len),
                NextState("SHIFT"),
            ),
        )
        fsm.act("SHIFT",
            self.busy.eq(1),
            # LAT rises for the last REG_LATCH clocks of the block, and not
            # before: the chip times the command from the trailing edge.
            self.lat.eq(left <= REG_LATCH),
            *[NextValue(shift[i], Cat(0, shift[i])) for i in range(lanes)],
            NextValue(left, left - 1),
            If(bit == 0,
                # Reload for the next driver IC in the chain.
                *[NextValue(shift[i], self.data[i]) for i in range(lanes)],
                NextValue(bit, data_width - 1),
                NextValue(rep, rep - 1),
            ).Else(
                NextValue(bit, bit - 1),
            ),
            If(left == 1, NextState("IDLE")),
        )


class SpwmPrefix(Module):
    """The per-frame command train, driven by a chip table the CPU can rewrite.

    Every chip in this family speaks one grammar: a few bare LAT bursts, then
    `slots` 16-bit slots each latched over their last five clocks, with the slot
    at `rot_slot` rotating through the configuration one word per frame. They
    differ only in the payload, how many slots follow, and whether the middle
    11-clock burst is sent.

        ICND1065L/S   LAT 3, LAT 11, LAT 14, then 5 slots, register at slot 2
        DP3364S       LAT 3,         LAT 14, then 1 slot,  register at slot 0

    So the chip is DATA, not a circuit -- see spwm_chips.py. The table lives in
    a small memory the CPU writes at boot, which means one bitstream drives the
    whole family and adding a chip is a table plus a line of firmware.

    **That generality is free on the hot path.** All of this happens once per
    frame, in the prefix, while no pixel data is moving. The serialiser, pixel
    source, arbiter and gamma units are common to the family and take no chip
    parameter at all.

    `reg_index` advances once per frame, so the whole table is refreshed every
    `reg_count` frames rather than written once at power-on. That is the chip's
    design: the configuration is continuously re-asserted, which is why these
    panels recover from a glitch without a reset.
    """

    def __init__(self, block_len, max_regs=MAX_REGS, max_wrappers=MAX_WRAPPERS,
                 chip=None):
        chip = chip or DEFAULT_CHIP
        self.start = Signal()
        self.busy = Signal()
        self.lat = Signal()
        self.data_out = Signal(3)              # one bit per colour lane
        self.reg_index = Signal(max=max_regs)

        # Runtime configuration, written by the CPU. Reset values are the
        # default chip's, so a card whose firmware never configures it still
        # lights rather than showing nothing at all.
        self.cfg_reg_count = Signal(max=max_regs + 1, reset=len(chip.regs))
        self.cfg_slots = Signal(4, reset=chip.slots)
        self.cfg_rot_slot = Signal(4, reset=chip.rot_slot)
        self.cfg_mid_latch = Signal(reset=int(chip.mid_latch))

        # Table write port: the CPU supplies an address and three 16-bit words.
        self.tbl_adr = Signal(max=max_regs + max_wrappers)
        self.tbl_dat = Signal(48)
        self.tbl_we = Signal()

        # # #

        # ONE memory holding the rotating registers and then the wrappers, 48
        # bits wide -- three 16-bit lanes side by side. A word per frame is read
        # from it, so it is nowhere near any timing-critical path.
        depth = max_regs + max_wrappers
        init = [0] * depth
        for i, (r, g, b) in enumerate(chip.regs):
            init[i] = (b << 32) | (g << 16) | r
        for i, (r, g, b) in enumerate(chip.wrappers):
            init[max_regs + i] = (b << 32) | (g << 16) | r
        tbl = Memory(48, depth, init=init)
        rd = tbl.get_port(async_read=True)
        wr = tbl.get_port(write_capable=True)
        self.specials += tbl, rd, wr
        self.comb += [wr.adr.eq(self.tbl_adr), wr.dat_w.eq(self.tbl_dat),
                      wr.we.eq(self.tbl_we)]

        self.submodules.cmd = cmd = SpwmCommand()
        self.submodules.blk = blk = SpwmRegBlock(block_len)
        self.comb += [
            self.lat.eq(cmd.lat | blk.lat),
            # A bare command burst carries no data -- the chip does not look at
            # the data lines during one -- so the command's own bit is
            # replicated across the lanes and the register block drives them
            # independently.
            self.data_out.eq(Mux(blk.busy, blk.data_out,
                                 Replicate(cmd.data_out, 3))),
            *[blk.data[i].eq(rd.dat_r[16 * i:16 * (i + 1)]) for i in range(3)],
        ]

        # Command bursts: VSYNC, optionally the middle burst, then PRE_ACT.
        step = Signal(4)
        slot = Signal(4)
        last_cmd = Signal(4)
        self.comb += last_cmd.eq(Mux(self.cfg_mid_latch, 2, 1))
        cmd_len = Signal(max=PRE_ACT_END + 1)
        self.comb += Case(step, {
            0: cmd_len.eq(V_SYNC),
            1: cmd_len.eq(Mux(self.cfg_mid_latch, PRE_ACT_MID, PRE_ACT_END)),
            2: cmd_len.eq(PRE_ACT_END),
        })
        # Which table row this slot reads: the rotating register, or a wrapper.
        self.comb += If(slot == self.cfg_rot_slot,
                        rd.adr.eq(self.reg_index),
                    ).Else(
                        # Wrappers are consumed in order, skipping the rotating
                        # slot, so a chip with no wrappers at all (DP3364S) is
                        # simply a table with one slot and never reads them.
                        rd.adr.eq(max_regs + Mux(slot > self.cfg_rot_slot,
                                                 slot - 1, slot)),
                    )

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(self.start,
                NextValue(step, 0),
                NextValue(slot, 0),
                NextState("CMD"),
            ),
        )
        fsm.act("CMD",
            self.busy.eq(1),
            cmd.start.eq(1),
            cmd.length.eq(cmd_len),
            cmd.pad.eq(CLK_PAD),
            NextState("CMD_WAIT"),
        )
        fsm.act("CMD_WAIT",
            self.busy.eq(1),
            If(~cmd.busy,
                If(step == last_cmd,
                    NextState("BLOCK"),
                ).Else(
                    NextValue(step, step + 1),
                    NextState("CMD"),
                ),
            ),
        )
        fsm.act("BLOCK",
            self.busy.eq(1),
            blk.start.eq(1),
            NextState("BLOCK_WAIT"),
        )
        fsm.act("BLOCK_WAIT",
            self.busy.eq(1),
            If(~blk.busy,
                If(slot == self.cfg_slots - 1,
                    # Frame's worth of configuration done; advance the rotation.
                    If(self.reg_index == self.cfg_reg_count - 1,
                        NextValue(self.reg_index, 0),
                    ).Else(
                        NextValue(self.reg_index, self.reg_index + 1),
                    ),
                    NextState("IDLE"),
                ).Else(
                    NextValue(slot, slot + 1),
                    NextState("BLOCK"),
                ),
            ),
        )


# The names these carried while the stage drove exactly one chip. Kept so the
# simulation, the fit harness and the docs do not all have to move at once.
Icn1065Prefix = SpwmPrefix
Icn1065Command = SpwmCommand
Icn1065RegBlock = SpwmRegBlock


def brightness(value8, bits=GREY_BITS):
    """8-bit colour component to a `bits`-wide SPWM value, LINEAR (no gamma).

    A plain shift would never reach full scale (0xFF << 4 is 0xFF0, not 0xFFF),
    so the high bits are folded back into the low ones: for 12 bits
    `(v << 4) | (v >> 4)`, for 14 `(v << 6) | (v >> 2)`. Full scale maps to
    full scale and zero to zero.

    This is the identity-ish path, kept for tests and for anyone who wants raw
    values. The display path uses `gamma_value()` below.
    """
    assert 8 < bits <= 16
    return ((value8 << (bits - 8)) | (value8 >> (16 - bits))) & ((1 << bits) - 1)


def brightness12(value8):
    """The 12-bit form, kept for callers that mean exactly 12."""
    return brightness(value8, 12)


GAMMA = 2.8            # same exponent as hub75.py's LUT
GAMMA_SEGMENTS = 16    # piecewise-linear segments, indexed by the top nibble


def gamma_table(gamma=GAMMA, bits_out=GREY_BITS):
    """The true 8 -> `bits_out` gamma curve, as a list of 256 ints.

    `hub75.py` corrects 8 bits to 8 bits, which throws the bottom of the curve
    away: with gamma 2.8 the first fourteen input codes all map to output 0, so
    a dark gradient posterises into flat black. Correcting into 12 bits instead
    is most of the point of driving a chip that takes 12-bit greyscale -- code 1
    lands on 0 and code 4 is already distinguishable.
    """
    max_out = (1 << bits_out) - 1
    return [int(pow(i / 255.0, gamma) * max_out + 0.5) for i in range(256)]


def _gamma_segments(gamma=GAMMA, bits_out=GREY_BITS, segments=GAMMA_SEGMENTS):
    """Piecewise-linear approximation of `gamma_table`: (base[], slope[]).

    Returned as `base` (the curve at each segment start) and `slope` (the rise
    per input code within that segment, in 1/16ths so the multiply stays small).

    Why not the real 256-entry table? Because six channels have to be converted
    at once. A combinational 256-to-1 mux is about 85 LUT4s per output bit, so
    one 12-bit lookup is ~1,000 LUTs and six of them per output, nine outputs
    over, is an order of magnitude more logic than the whole rest of the design.
    A block RAM would be cheap in logic but costs a cycle, and the serialiser
    reloads every 16 clocks whether or not the data arrived -- latency here is
    not free, it tears the image sideways. Sixteen straight lines cost a 16-to-1
    mux and a 4x6 multiply, and the error is measured in `sim_icn1065.py`.
    """
    step = 256 // segments
    max_out = (1 << bits_out) - 1
    base, slope = [], []
    for seg in range(segments):
        lo = seg * step
        y0 = pow(lo / 255.0, gamma) * max_out
        # The last segment has no table entry above it; extrapolate to full
        # scale at code 256 so that code 255 still reaches max_out.
        y1 = pow(min(lo + step, 256) / 255.0, gamma) * max_out
        base.append(int(y0 + 0.5))
        slope.append(int((y1 - y0) / step * 16 + 0.5))   # 1/16ths of a code
    return base, slope


def gamma_value(value8, gamma=GAMMA, bits_out=GREY_BITS, segments=GAMMA_SEGMENTS):
    """Software twin of the `Icn1065Gamma` hardware -- bit-exact, for tests."""
    base, slope = _gamma_segments(gamma, bits_out, segments)
    step = 256 // segments
    seg, off = value8 // step, value8 % step
    return min((base[seg] + ((slope[seg] * off) >> 4)), (1 << bits_out) - 1)


# Names before gateware 1.21.0, when the depth was fixed at 12.
gamma_table12 = gamma_table
gamma12 = gamma_value


class Icn1065Gamma(Module):
    """Combinational 8-bit -> 12-bit gamma, piecewise-linear over 16 segments.

    Zero latency and zero block RAM, which is what lets it sit in the pixel path
    without touching the serialiser's 16-clock budget. See `_gamma_segments`
    for why it is not a real lookup table.
    """

    def __init__(self, bits_out=GREY_BITS, gamma=GAMMA,
                 segments=GAMMA_SEGMENTS):
        self.i = Signal(8)
        self.o = Signal(bits_out)

        # # #

        base_l, slope_l = _gamma_segments(gamma, bits_out, segments)
        shift = (256 // segments).bit_length() - 1      # 4 for 16 segments

        base = Array([Constant(v, bits_out) for v in base_l])
        slope = Array([Constant(v, max(slope_l).bit_length()) for v in slope_l])

        seg = self.i[shift:]
        off = self.i[:shift]
        acc = Signal(bits_out + 2)

        # slope * off, written out as shift-and-add rather than `*`.
        #
        # This is not premature cleverness. Yosys maps a `*` to a MULT18X18D,
        # and six channels per output across nine outputs is 54 DSPs on a
        # device that has 28 -- nextpnr fails outright at 192%. The operands
        # here are six bits by four, so the whole product is four conditional
        # shifts and an adder tree, which costs a handful of LUTs and no DSP.
        sl = slope[seg]
        product = sum([Mux(off[b], sl << b, 0) for b in range(shift)])

        self.comb += [
            acc.eq(base[seg] + (product >> 4)),
            # Saturate: the top segment extrapolates past full scale by design
            # so that code 255 reaches it, and a few codes below can round over.
            If(acc > (1 << bits_out) - 1,
                self.o.eq((1 << bits_out) - 1),
            ).Else(
                self.o.eq(acc),
            ),
        ]


class Icn1065Serialiser(Module):
    """Shift pixels out as SPWM: 16 clocks each, MSB first, six channels.

    The chip wants a 16-bit greyscale word per LED per refresh and generates the
    PWM itself. So unlike `hub75.py` there is no bit-plane loop and no BCM: each
    clock carries ONE bit of every channel at once, and after 16 clocks the
    pixel is fully described.

    `r1/g1/b1` are the upper half of the panel and `r2/g2/b2` the lower, exactly
    as the two RGB groups on a HUB75 connector.

    CONTINUOUS while `run` is high, reloading from `channels_in` every 16
    clocks. That is not a convenience: the panel clocks data on every CLK, so a
    bubble between pixels shifts the rest of the row by that many bits and the
    image tears sideways. The first version of this had a two-state fetch
    between pixels and the full-frame simulation caught it as 19 clocks per
    pixel instead of 16.

    `loading` strobes on the cycle a new pixel is taken, so the caller can
    advance its address a pixel ahead and have the next value ready.
    """

    def __init__(self, grey_bits=GREY_BITS, spwm_bits=SPWM_BITS,
                 groups=GROUPS_HUB75):
        n = 3 * groups
        self.groups = groups
        self.run = Signal()
        self.busy = Signal()
        self.loading = Signal()
        # High on the cycle the LAST bit of a word goes out, so the caller can
        # place a DATA_LATCH against the end of a row.
        self.last_bit = Signal()
        self.channels_in = [Signal(grey_bits) for _ in range(n)]
        self.channels_out = Signal(n)      # r1 g1 b1 r2 g2 b2 ..., one bit each

        # Kept so a single pixel can still be shifted in isolation (the unit
        # test does exactly that); equivalent to one cycle of `run`.
        self.start = Signal()

        # # #

        # Bits above `grey_bits` must be zero, so the value sits in the low bits
        # of a 16-bit window: the first 16 - grey_bits clocks of every pixel
        # emit zero (two at 14 bits).
        shifts = [Signal(spwm_bits) for _ in range(n)]
        count = Signal(max=spwm_bits + 1)

        load = Signal()
        self.comb += [
            self.loading.eq(load),
            *[self.channels_out[i].eq(shifts[i][spwm_bits - 1]) for i in range(n)],
        ]

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(self.run | self.start,
                load.eq(1),
                *[NextValue(sh, ch) for sh, ch in zip(shifts, self.channels_in)],
                NextValue(count, spwm_bits),
                NextState("SHIFT"),
            ),
        )
        fsm.act("SHIFT",
            self.busy.eq(1),
            self.last_bit.eq(count == 1),
            NextValue(count, count - 1),
            If(count == 1,
                # Reload in the SAME cycle the last bit goes out, so the stream
                # never stalls.
                If(self.run,
                    load.eq(1),
                    *[NextValue(sh, ch) for sh, ch in zip(shifts, self.channels_in)],
                    NextValue(count, spwm_bits),
                ).Else(
                    NextState("IDLE"),
                ),
            ).Else(
                *[NextValue(sh, Cat(0, sh)) for sh in shifts],
            ),
        )


class Icn1065RowScanner(Module):
    """Walk the row slots, advancing the chip's counter with one OE pulse each.

    There is no row address to drive: `OE` steps an internal counter that VSYNC
    reset at the top of the frame. Slot width is the reference's "most important
    tuning parameter for this panel", which is why it is a constructor argument
    rather than a constant buried in the FSM.

    OE is ACTIVE HIGH here.
    """

    def __init__(self, rows_per_frame, slot_len=ROW_OE_LEN,
                 pulse_len=ROW_OE_PULSE, pulses=ROW_OE_CNT,
                 row_pulse=None, row_pulse_first=None, display_passes=1):
        assert slot_len > pulses * pulse_len, "slot too short for its OE pulses"
        # Pulse width is RUNTIME, because it is not the same on every chip in
        # this family. The ICND1065 steps its counter on any edge, so a 2-clock
        # pulse serves; the DP3264S reads the HIGH WIDTH as meaning -- W12 for
        # the first row of a frame, W4 for the rest (datasheet 11.5) -- and a
        # uniform 2-clock pulse starts no row at all on it. Defaults preserve
        # the behaviour this stage shipped with.
        row_pulse = pulses * pulse_len if row_pulse is None else row_pulse
        if row_pulse_first is None:
            row_pulse_first = row_pulse
        assert slot_len > max(row_pulse, row_pulse_first), "slot too short"
        self.start = Signal()
        self.busy = Signal()
        self.oe = Signal()
        self.pulse_w = Signal(5, reset=row_pulse)
        self.pulse_w_first = Signal(5, reset=row_pulse_first)
        self.row = Signal(max=rows_per_frame)   # for status/debug only
        self.frame_done = Signal()              # one-cycle strobe

        # HOW OFTEN THE WIDE PULSE FIRES, as `display_passes - 1`. Runtime,
        # because each value costs a power cycle to test otherwise.
        #
        # 11.6.2 step 5: "there is only one W12 for each (number of groups x
        # number of rows scanned) ROW signals". The group count is reg0x03 + 1,
        # and it ALSO sets the chip's displayable grey range (11.7.5), so the
        # two cannot be chosen independently: a group count small enough for a
        # W12 every frame leaves almost no grey. The factory runs 64 groups,
        # so W12 belongs on row 0 of every 64th pass, not of every pass.
        # Emitting it 64x too often is the "rain" in 4.4; never emitting it is
        # a dark panel.
        self.passes_m1 = Signal(7, reset=max(0, display_passes - 1))
        pass_n = Signal(7)
        first_pass = Signal()
        self.comb += first_pass.eq(pass_n == 0)

        # # #

        left = Signal(max=slot_len + 1)
        pulse_left = Signal(max=max(pulses, 2))
        # The first row of a frame may need a wider pulse than the rest.
        width = Signal(5)
        self.comb += width.eq(Mux((self.row == 0) & first_pass,
                                  self.pulse_w_first, self.pulse_w))

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(self.start,
                NextValue(self.row, 0),
                NextValue(left, slot_len),
                NextValue(pulse_left, pulses),
                NextState("SLOT"),
            ),
        )
        fsm.act("SLOT",
            self.busy.eq(1),
            # The pulse sits at the head of the slot; the rest is hold time.
            self.oe.eq(left > (slot_len - width)),
            NextValue(left, left - 1),
            If(left == 1,
                If(self.row == rows_per_frame - 1,
                    self.frame_done.eq(1),
                    # One pass of the row chain is done. The chip re-displays
                    # its SRAM `passes_m1 + 1` times between wide pulses.
                    If(pass_n >= self.passes_m1,
                        NextValue(pass_n, 0),
                    ).Else(
                        NextValue(pass_n, pass_n + 1),
                    ),
                    NextState("IDLE"),
                ).Else(
                    NextValue(self.row, self.row + 1),
                    NextValue(left, slot_len),
                    NextState("SLOT"),
                ),
            ),
        )


class Icn1065Output(Module):
    """One connector's worth of ICN1065 driving: prefix, pixels, row scan.

    Frame structure, per docs/ICN1065.md:

        prefix   VSYNC + PRE_ACT1/2 + five register blocks   ~1332 clocks
        pixels   columns x 16 clocks, once per row slot      columns*16*rows
        scan     rows x slot_len, one OE pulse each          rows*128

    Pixels are pulled through `pix_req`/`pix_ack`: the output raises `pix_req`
    with `pix_col`/`pix_row` set and expects six 12-bit channel values by the
    time it acknowledges. Whoever supplies them owns the framebuffer read path,
    which keeps this module about the PROTOCOL and nothing else -- the same
    split that let hub75.py's scan-out survive four changes of pixel source.

    `clk_out` is the panel shift clock: one full cycle per emitted bit.
    """

    def __init__(self, columns, rows_per_frame, slot_len=None,
                 groups=GROUPS_HUB75, chip=None, prefix=None):
        n = 3 * groups
        self.groups = groups
        self.start = Signal()
        self.busy = Signal()
        self.frame_done = Signal()

        # Panel pins for this connector. Six lines on HUB75, twelve on HUB320.
        self.lat = Signal()
        self.oe = Signal()
        self.rgb = Signal(n)               # r1 g1 b1 r2 g2 b2 ...

        # Pixel fetch interface.
        self.pix_req = Signal()
        # DATA_LATCH enable. The DP3264S moves a shifted word into its SRAM
        # only on a LAT pulse (datasheet 11.1, LE width 1; 11.4 step 4 "send
        # DAT_LAT several times and write display data with SDI"), and the
        # reference driver raises LAT for the final `latches` clocks of each
        # row. This stage asserted LAT only during the register prefix, so
        # pixel data was shifted and never committed -- clock toggling on the
        # wire, nothing on the glass.
        self.data_latch_en = Signal(reset=int(getattr(chip, "data_latch", 0)))
        self.pix_col = Signal(max=columns)
        self.pix_row = Signal(max=rows_per_frame)
        grey_bits = getattr(chip, "grey_bits", GREY_BITS) if chip else GREY_BITS
        if slot_len is None:
            slot_len = (getattr(chip, "row_slot", None) or ROW_OE_LEN) if chip \
                       else ROW_OE_LEN
        self.pix_in = [Signal(grey_bits) for _ in range(n)]

        # # #

        block_len = columns               # one register block spans a row
        # The prefix may be SHARED across every output on the board, and on a
        # multi-output wall it should be. All outputs are started together and
        # run identical command trains, so a prefix each is N copies of one
        # register table and N chances for them to disagree. Once the table
        # became writable that stopped being merely wasteful: five private
        # tables are five real RAMs rather than five ROMs, which measured
        # +2,763 LUTs -- 67% utilisation to 79% -- for nothing.
        #
        # `chip` only sets the reset values of the table and grammar, so an
        # unconfigured card still lights rather than showing nothing.
        self.prefix_req = Signal()
        self.shared_prefix = prefix is not None
        if prefix is None:
            self.submodules.prefix = prefix = SpwmPrefix(block_len, chip=chip)
        else:
            self.prefix = prefix
        self.submodules.ser = ser = Icn1065Serialiser(groups=groups,
                                                      grey_bits=grey_bits)
        self.submodules.scan = scan = Icn1065RowScanner(
            rows_per_frame, slot_len,
            row_pulse=getattr(chip, "row_pulse", None),
            row_pulse_first=getattr(chip, "row_pulse_first", None),
            display_passes=getattr(chip, "display_passes", 1))
        # Re-exported so the SoC can drive them from `chip_cfg` at runtime.
        self.row_pulse = scan.pulse_w
        self.row_pulse_first = scan.pulse_w_first
        self.passes_m1 = scan.passes_m1
        # The row currently being SCANNED OUT, for modules that decode a row
        # address on A-E rather than counting internally.
        self.scan_row = scan.row

        # The chain is walked CHANNEL-MAJOR, not left to right, because each
        # driver holds ONE 16-bit word at a time and a DATA_LATCH commits that
        # word to the channel its internal counter points at. So a row is 16
        # sweeps: each sweep shifts one word per chip along the chain and ends
        # in its own LAT. Walking the row linearly and latching once at the end
        # leaves fifteen sixteenths of it clocked through the chain and thrown
        # away -- which looks exactly like a dead panel.
        #
        #   board707/DMD_STM32 DMD_SPWM_Driver.h, the upload loop:
        #       for y in rows: for x in 0..16: for sect in 0..x_len/16
        #       ... LAT on the last bit of the last section
        assert columns % CHANNELS_PER_CHIP == 0, \
            f"{columns} columns is not a whole number of {CHANNELS_PER_CHIP}-channel chips"
        sections = columns // CHANNELS_PER_CHIP
        sect = Signal(max=max(sections, 2))     # which chip along the chain
        chan = Signal(4)                        # which channel within the chip
        col = Signal(max=columns)
        row = Signal(max=rows_per_frame)
        # Channel order within a line.
        #
        # DP3264S_REV2.0 11.2 sends a line as channel 15 FIRST, counting down
        # to channel 0 -- "Data sending order 1: Line 0, Channel 15(OUT15)"
        # through "16: Channel 0(OUT0)", repeated for every line. This walked
        # 0 -> 15, which reverses the sixteen channels inside every driver.
        # Runtime, because it is one bit and the alternative is another
        # place-and-route to test a table in a machine-translated datasheet.
        self.chan_rev = Signal()
        # XOR applied to the channel index, i.e. a permutation of the sixteen
        # columns inside every driver.
        #
        # This exists because the compensation for it lived in the FIRMWARE's
        # pattern generator (`patterns.rs`, `let x = p ^ 1`), which streamed
        # content does not go through: marquee writes the framebuffer directly.
        # So the test patterns were pre-compensated and looked right while
        # VIDEO -- the actual deliverable -- would show the swap. Putting it
        # here applies it to everything the panel displays, which is where a
        # property of the hardware belongs.
        #
        # 1 is the measured value for this module: adjacent columns are
        # exchanged in pairs, confirmed on glass by three independent 1-pixel
        # features (both borders and the 2:1 diagonal's sawtooth). It is an
        # XOR rather than a fixed swap so other permutations can be tried
        # without a rebuild -- 0 is identity.
        self.chan_xor = Signal(4)
        # Advance the FETCH ADDRESS by N sweeps, carrying into the row.
        #
        # This is NOT `latch_phase`, and the difference is the whole point.
        # `latch_phase` adds DATA_LATCH pulses, which moves our data LATER
        # against the chip's line boundary. This moves the DATA EARLIER while
        # leaving the pulse count at exactly 16 per row.
        #
        # Why that direction. The 16-pitch dots sit at the columns fed by
        # sweep 0 -- the FIRST latch of every row, in all eight chips -- and
        # carry the ADJACENT row's value. Simulation at the real geometry
        # proves our first latch of row r commits row r's data to every chip
        # (0 mismatches over two frames), so if the glass shows row r-1 there,
        # the chip is still on line r-1 when that latch arrives: its line
        # advance happens one latch LATER than ours. Feeding the address one
        # sweep ahead puts the pixel the chip is about to write where the chip
        # is about to write it.
        #
        # Runtime, 3 bits, because the premise is a deduction and the panel is
        # the only thing that can confirm it. 0 is no correction.
        self.sweep_adv = Signal(3)
        # Extra DATA_LATCH pulses emitted after the prefix, before pixel data.
        #
        # The chip commits one word per DATA_LATCH and steps to the next LINE
        # after 16. We emit exactly 16 per row (verified in simulation: every
        # latch closes over one row's pixels). The two counters therefore stay
        # in step only by counting from a COMMON START, and nothing establishes
        # that start except the frame prefix.
        #
        # If the chip begins its line one latch away from where we think, one
        # channel per line carries the adjacent line's data -- on glass, one
        # wrong pixel per chip on EVERY row, showing the neighbouring scan
        # row's value (`hstripe`: black dots in white rows, white in black).
        #
        # Runtime so the alignment can be swept on the panel without a
        # bitstream per value. If the dot moves by N positions when this is
        # set to N, the hypothesis is proven and N is the correction.
        self.latch_phase = Signal(4)
        chan_eff = Signal(4)
        # `chan_a` is the channel the ADDRESS is taken from: `chan` advanced by
        # `sweep_adv`, with the overflow carried into the row. `chan` itself
        # still drives the latch cadence, so the pulse count stays 16 per row.
        chan_a = Signal(4)
        sweep_carry = Signal()
        adv_sum = Signal(5)
        self.comb += [
            adv_sum.eq(chan + self.sweep_adv),
            chan_a.eq(adv_sum[0:4]),
            sweep_carry.eq(adv_sum[4]),
        ]
        self.comb += chan_eff.eq(Mux(self.chan_rev, ~chan_a, chan_a) ^ self.chan_xor)
        # The row that goes with it: one on when the sweep counter carried,
        # wrapping at the last row so the frame stays closed.
        row_a = Signal(max=rows_per_frame)
        self.comb += row_a.eq(Mux(sweep_carry,
                                  Mux(row == rows_per_frame - 1, 0, row + 1),
                                  row))
        self.comb += col.eq(Cat(chan_eff, sect))   # sect*16 + channel

        if not self.shared_prefix:
            self.comb += prefix.start.eq(self.prefix_req)
        data_lat = Signal()
        primed = Signal()
        phase_n = Signal(4)
        phase_lat = Signal()
        # Spacing between the phase latches. LE encodes the command in its
        # WIDTH, so these have to be separate 1-clock pulses with a low gap
        # between them (see the PHASE state).
        phase_gap = Signal(max=CHANNELS_PER_CHIP)
        # ROW FREE-RUNS, ALWAYS -- through the prefix and the pixel phase too,
        # not merely between frames.
        #
        # DP3264S_REV2.0 11.6.2, the mode our register table selects
        # (reg0x0C[7:6]=2, high-grey independent refresh): "ROW is sent
        # continuously at a fixed display frequency WITHOUT INTERRUPTION. The
        # frequency of ROW is 1/the display time of one row." The on-chip GCLK
        # generator turns what is OE on an ordinary part into ROW (11.5), so
        # ROW is what clocks the display engine -- stop it and the panel stops,
        # however correct the data and the row address are.
        #
        # This FSM used to emit ROW only in a dedicated SCAN phase AFTER all the
        # pixel data. At 256 columns x 64 rows x 16 bits that is 262,144 clocks
        # of silence against 8,192 clocks of scan: ROW absent 97% of every
        # frame. The panel was dark, and dark INVARIANTLY -- no row-address
        # mapping, chip table or pulse width could matter while the display
        # engine was stalled, which is exactly what weeks of sweeping those
        # three things found. `refresh_count` climbed throughout, because it
        # counts this state machine and not photons (CLAUDE.md says so).
        #
        # Only 11.6.1's general frame-sync mode (reg0x0C[7:6]=0) permits the
        # burst shape: there, display "stops until the next VSYNC arrives". We
        # select mode 2 and were driving it like mode 0.
        #
        # Concurrency is safe because the two paths are independent: ROW is the
        # only line the scanner drives, CLK/LE/SDI the only ones the data path
        # drives. board707's survey classifies this family exactly so --
        # "timing is handled by the clock signal, while row switching is driven
        # by OE pulses; the two signals are completely independent".
        self.comb += scan.start.eq(~scan.busy)
        self.comb += [
            self.lat.eq(prefix.lat | data_lat | phase_lat),
            self.oe.eq(scan.oe),
            # The prefix drives every line together (the register word goes to
            # every channel); pixel data drives them independently.
            # During the prefix each colour lane drives its own lines: lane 0
            # every R, lane 1 every G, lane 2 every B, however many RGB groups
            # the connector carries. Replicating ONE bit across all of them, as
            # this did while the table was uniform, silently sends the red
            # chip's configuration to the green and blue chips too.
            self.rgb.eq(Mux(prefix.busy,
                            Cat(*[prefix.data_out[c % 3] for c in range(n)]),
                            ser.channels_out)),
            *[ser.channels_in[i].eq(self.pix_in[i]) for i in range(n)],
            self.pix_col.eq(col),
            self.pix_row.eq(row_a),
        ]

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(self.start,
                # `col` is COMBINATIONAL now (Cat(chan, sect)); resetting it
                # here too gave it two drivers. Migen's simulator let that pass
                # -- the comb assignment won each cycle -- and synthesis did
                # not, which wedged the frame loop and pinned refresh_count at
                # 0 on hardware while every simulation stayed green.
                NextValue(sect, 0),
                NextValue(chan, 0),
                NextValue(row, 0),
                NextState("PREFIX"),
            ),
        )
        fsm.act("PREFIX",
            self.busy.eq(1),
            # Arm the one-shot prime for this frame.
            NextValue(primed, 0),
            # Ask for the prefix rather than starting it. With a shared prefix
            # the board starts it once; with a private one this output does,
            # wired below. Either way the FSM here is identical.
            self.prefix_req.eq(1),
            NextState("PREFIX_WAIT"),
        )
        fsm.act("PREFIX_WAIT",
            self.busy.eq(1),
            # PRIME the pixel pipeline while the prefix runs.
            #
            # PIXELS issues `pix_req` ON the load (`pix_req.eq(ser.loading)`),
            # and the SoC registers that request so the fetch it starts serves
            # the NEXT load -- "the address advances a pixel ahead of the
            # shift register". That invariant holds for every word except the
            # FIRST: nothing requested anything before PIXELS began, so the
            # frame's first load latched whatever `pix_in` happened to hold.
            #
            # A sweep is one word per chip, so one stale first sweep is eight
            # wrong pixels, one per chip, 16 columns apart, starting at pixel
            # (0,0). All four groups share the row counter, so scan row 0 is
            # drawn at y=0 AND y=64 -- which on glass was black dots punched
            # through the top border and white dots across mid-panel, both at
            # a 16-pixel pitch. Measured 2026-10-18.
            #
            # `sect`, `chan` and `row` are all 0 here, so `col` is 0 and this
            # fetches exactly the pixel the first load needs. Holding the
            # request for the whole prefix is harmless -- the SoC's
            # `want & ~served` simply re-requests the same column -- and the
            # prefix (VSYNC 3, PRE_ACT 14, WR_CFG 5, plus gaps) is far longer
            # than a memory read, so the data is there before PIXELS starts.
            # ONE request, not one per cycle. Holding it for the whole prefix
            # issued a fetch every cycle -- the simulation caught that at 253
            # fetches against an expected 128, with the pixel order destroyed.
            self.pix_req.eq(~primed),
            NextValue(primed, 1),
            If(~prefix.busy,
                NextValue(phase_n, self.latch_phase),
                # Arm the gap FULL, not 0, so the first phase latch is spaced
                # like every other one. `SpwmRegBlock` has no trailing pad of
                # its own, so entering PHASE with the gap at 0 put the first
                # latch two clocks after the WR_CFG latch -- the one place in
                # the design still issued at the pre-fix spacing, and it would
                # have been the first pulse of every latch_phase sweep.
                NextValue(phase_gap, CHANNELS_PER_CHIP - 1),
                NextState("PHASE"),
            ),
        )
        fsm.act("PHASE",
            # Emit `latch_phase` bare DATA_LATCH pulses to slide our latch
            # sequence against the chip's line counter.
            #
            # ONE CLOCK HIGH, then a low gap -- not N cycles high. LE encodes
            # the command in its WIDTH (DATA_LATCH 1, VSYNC 3, WR_CFG 5,
            # PRE_ACT 14, DP3264S 11.1), exactly as SpwmCmd does it with
            # LAT_HIGH followed by PAD. Holding LE high for N cycles is ONE
            # N-wide pulse, so N=3 would send a VSYNC and N=5 a WR_CFG --
            # a register write where sixteen latches were meant.
            #
            # The gap is a full 16-clock word period, which is the cadence the
            # chip sees during the data phase (one latch per word). Matching it
            # means this sweep changes only the latch COUNT and not their
            # timing, so a result cannot be blamed on either.
            self.busy.eq(1),
            If(phase_n == 0,
                NextState("PIXELS"),
            ).Elif(phase_gap == 0,
                phase_lat.eq(1),
                NextValue(phase_gap, CHANNELS_PER_CHIP - 1),
                NextValue(phase_n, phase_n - 1),
            ).Else(
                NextValue(phase_gap, phase_gap - 1),
            ),
        )
        fsm.act("PIXELS",
            self.busy.eq(1),
            ser.run.eq(1),
            # One fetch per pixel, issued as the serialiser takes the previous
            # one -- the address advances a pixel ahead of the shift register.
            self.pix_req.eq(ser.loading),
            If(ser.loading,
                # Inner loop walks the chain; outer loop walks the channels.
                If(sect == sections - 1,
                    NextValue(sect, 0),
                    If(chan == CHANNELS_PER_CHIP - 1,
                        NextValue(chan, 0),
                        If(row == rows_per_frame - 1,
                            NextState("DRAIN"),
                        ).Else(
                            NextValue(row, row + 1),
                        ),
                    ).Else(
                        NextValue(chan, chan + 1),
                    ),
                ).Else(
                    NextValue(sect, sect + 1),
                ),
            ),
        )
        # One-clock DATA_LATCH at the end of EVERY sweep -- once per channel,
        # sixteen times per row -- not once per row.
        # Latch when the LAST word has actually finished shifting, which is
        # one section AFTER the counter says so.
        #
        # `sect` advances on the load event (`If(ser.loading, ... sect+1)`),
        # so while section S's word is shifting out over its 16 clocks the
        # counter already reads S+1 -- the counter runs ONE SECTION AHEAD of
        # the data on the wire. Latching at `sect == sections - 1` therefore
        # committed while section `sections-2`'s word was on the chain, so
        # every chip latched the word meant for its neighbour: a displacement
        # of exactly one chip, 16 columns, the same for all four groups, with
        # the tail wrapping in from the adjacent sweep.
        #
        # That is the 16-column offset measured on glass from the very first
        # bring-up ("green needs to be shifted 16 pixels to the left"). It was
        # papered over in firmware for a long time with per-group constants of
        # 17 and 18 that could never be justified; this is the cause.
        #
        # `sect == 0` is the section after the wrap, at which point the word
        # completing its shift is `sections-1`'s. It cannot false-trigger on
        # the first word of a row: section 0's load leaves the counter at 1.
        # ... and the FINAL sweep's latch lands in DRAIN.
        #
        # Moving the latch one section later means the last word of the frame
        # finishes shifting after the FSM has already left PIXELS for DRAIN,
        # which exists for exactly that purpose. Without this the frame ends
        # one latch short -- the simulation checks 16 per row and caught it at
        # 31 of 32.
        # `sect == 0` gates BOTH states, not just PIXELS.
        #
        # DRAIN is entered on the final word's load, which is the same event
        # that wraps `sect` to 0 -- so the gate is identical in both states
        # and there is no reason to special-case it. Gating only PIXELS left
        # DRAIN firing on ANY `last_bit`, which was safe only by accident:
        # `ser.run` is asserted three states away, in PIXELS, so the
        # serialiser happens to stop after one word. Nothing in the latch
        # condition said so.
        #
        # That mattered because every extra DATA_LATCH advances the driver's
        # own channel counter, so one spurious latch shifts EVERY subsequent
        # column by one channel, wrapping at each chip boundary -- a 1px
        # border lands one column in, and a solid block grows a dark line
        # where it crosses x=16. DRAIN is the frame boundary, which is also
        # where scan row 0 picks up the previous frame's tail.
        #
        # With the gate shared, a second latch is impossible by construction:
        # a further load would advance `sect` to 1.
        self.comb += data_lat.eq(
            self.data_latch_en
            & ser.last_bit
            & (sect == 0)
            & (fsm.ongoing("PIXELS") | fsm.ongoing("DRAIN")))

        fsm.act("DRAIN",
            # Let the final pixel finish shifting. The scan does NOT wait for
            # this -- it has been running throughout -- so `frame_done` now
            # means "the pixel data was consumed", which is all the caller
            # ever needed it for.
            self.busy.eq(1),
            If(~ser.busy,
                self.frame_done.eq(1),
                NextState("HOLD"),
            ),
        )
        fsm.act("HOLD",
            # Idle for the protocol's purposes, but still scanning: the caller
            # pulses `start` when it wants new pixels shown.
            If(self.start,
                # `col` is COMBINATIONAL now (Cat(chan, sect)); resetting it
                # here too gave it two drivers. Migen's simulator let that pass
                # -- the comb assignment won each cycle -- and synthesis did
                # not, which wedged the frame loop and pinned refresh_count at
                # 0 on hardware while every simulation stayed green.
                NextValue(sect, 0),
                NextValue(chan, 0),
                NextValue(row, 0),
                NextState("PREFIX"),
            ),
        )


def frame_clocks(columns, rows_per_frame, slot_len=ROW_OE_LEN):
    """Clocks per frame for one output -- the basis of every rate estimate.

    Prefix is the VSYNC/PRE_ACT train plus five row-length register blocks.
    """
    # Per-state FSM overhead, MEASURED at 20 clocks (COLS=16, icnd1065):
    # every command costs two cycles beyond its LE train -- one in
    # `SpwmPrefix`'s CMD state asserting start, one in CMD_WAIT watching busy
    # fall -- and every register-block slot the same, plus four for the
    # IDLE->CMD, CMD->BLOCK and block-exit transitions:
    #
    #     3 commands * 2  +  5 slots * 2  +  4  =  20
    #
    # This was invisible until CLK_PAD began being emitted at all. The pad was
    # read one state too late and always came back 0 (see `SpwmCommand`), so
    # the 24 clocks of padding this function budgeted and the hardware never
    # sent very nearly cancelled these 20 clocks it did not budget. The
    # predictor agreed with reality to 0.1% for two compensating wrong
    # reasons, and fixing either one alone makes it disagree.
    fsm_overhead = 3 * 2 + 5 * 2 + 4
    prefix = (V_SYNC + CLK_PAD + PRE_ACT1 + CLK_PAD + PRE_ACT2 + CLK_PAD
              + 5 * columns + fsm_overhead)
    pixels = columns * rows_per_frame * SPWM_BITS
    # The row scan is NOT added. It free-runs concurrently with the prefix and
    # the pixel phase (DP3264S 11.6.2 -- ROW must never be interrupted), so it
    # costs a frame no time at all; `rows_per_frame * slot_len` is the panel's
    # own visual refresh period, independent of how fast data arrives. That
    # independence is the point of the part: the chip PWMs at 3840 Hz off its
    # internal GCLK whatever the data rate is.
    #
    # It WAS added while the scan ran as a separate phase after the pixels --
    # which is exactly the bug that kept the panel dark, so the old figure was
    # describing a frame shape that should never have existed.
    return prefix + pixels


class Icn1065PixelSource(Module):
    """Fetch one pixel per RGB group per request: two on HUB75, four on HUB320.

    A connector carries `groups` sets of RGB data lines, each driving one
    horizontal slice of the panel -- rows `r`, `r + R`, `r + 2R` ... where `R`
    is `rows_per_frame`. So one request means `groups` framebuffer reads, and
    the serialiser needs all of them within its 16-clock window.

    That window is the reason this has no row buffer. At a 20 MHz shift clock a
    pixel lasts 800 ns, which is comfortably more than an SDRAM read, so reading
    on demand keeps `Icn1065Output`'s zero-EBR footprint instead of spending one
    to two blocks per output on prefetch -- which is exactly what the existing
    HUB75 path pays.

    Going from HUB75 to HUB320 doubles the reads per request AND doubles the
    pixels each request covers, so bandwidth per pixel is unchanged. What does
    change is the burst: four reads have to land inside the same 16 clocks that
    used to hold two, which is why `Icn1065PipelinedArbiter` matters more here,
    not less.

    Framebuffer word is 0x00BBGGRR (docs/HARDWARE.md section 3 -- measured, and
    not what the code assumed until 2026-09-18).

    The memory interface is deliberately generic -- `adr`/`dat_r`/`req`/`ack` --
    so this can sit behind a LiteDRAM port, a wishbone bridge or a simulation
    stub without caring which.
    """

    def __init__(self, columns, rows_per_frame, width, fb_base=0,
                 grey_bits=GREY_BITS, gamma=True, shared_gamma=None,
                 groups=GROUPS_HUB75, pix_bits=32, group_offsets=None):
        # `width` is the framebuffer stride in pixels: an int in the tests, a
        # 16-bit Signal (the ctrl.width CSR) in the SoC since gateware 1.20.0,
        # so `row * width` below is a real multiplier there. That is the price
        # of a stride the layout can set; the alternative was a bitstream
        # constant that folded every row of a layout narrower than the card.
        # `group_offsets`: where each RGB group's strip STARTS in the
        # framebuffer, in pixels, relative to `fb_base`. None keeps the HUB75
        # picture -- group g is the horizontal band `g * rows_per_frame` rows
        # down, which is right for a HUB75 panel and for an S-PWM module whose
        # groups really are stacked. It is WRONG for the bench P1.25 module,
        # whose four groups are 128x64 tiles in a 2x2: there the framebuffer
        # has to be the glass (256x128) and each group needs its own origin.
        # The SoC supplies these from CSRs so the module's arrangement is data
        # in the layout config, not a remap in software (docs/PANELS.md).
        assert pix_bits in (32, 16), "framebuffer pixel is 32 or 16 bits"
        assert group_offsets is None or len(group_offsets) == groups, (
            f"{len(group_offsets)} group offsets for {groups} groups")
        n = 3 * groups
        self.groups = groups
        self.pix_bits = pix_bits
        self.req = Signal()                 # one pixel per group, please
        self.col = Signal(max=columns)
        self.row = Signal(max=rows_per_frame)
        self.valid = Signal()               # channels are good this cycle
        self.channels = [Signal(grey_bits) for _ in range(n)]

        # Generic memory port, SPLIT: `mem_cmd_ack` says the command was
        # accepted, `mem_ack` says the data is on `mem_dat`. They are the same
        # cycle on a blocking arbiter and different cycles on a pipelined one,
        # and keeping them separate is what lets this source have all `groups`
        # reads in flight at once instead of one.
        self.mem_adr = Signal(32)
        self.mem_dat = Signal(32)
        self.mem_req = Signal()
        self.mem_ack = Signal()
        self.mem_cmd_ack = Signal()

        # # #

        # THREE converters regardless of group count -- or none at all, if the
        # caller supplies `shared_gamma`.
        #
        # The groups are read SEQUENTIALLY off one bus, so three converters
        # serve all of them however many there are; only the number of latched
        # results grows. And behind one arbiter every source reads the same
        # returning bus, so three serve the whole wall: the sources differ in
        # WHEN they latch, not in what the curve says. That is 3 converters
        # instead of 27 on a nine-output wall -- measured at 6,231 LUTs against
        # roughly 700, which is 23% of the device.
        #
        # Yosys would deduplicate them anyway, and that is precisely why this is
        # written out explicitly: a saving that depends on the synthesiser
        # noticing something is a saving nobody can reason about, and this file
        # has already been bitten twice by measurements that were really
        # reporting on dead or deduplicated logic.
        # FRAMEBUFFER PIXEL FORMAT
        #
        # 32-bit: one pixel per word, 0x00BBGGRR -- R in the low byte. Measured
        # at the glass, not assumed; see docs/HARDWARE.md section 3.
        #
        # 16-bit: TWO pixels per word, each B5 G6 R5 with R in the LOW bits so
        # the channel order matches the 32-bit layout rather than conventional
        # RGB565, which puts red at the top. Keeping one channel order across
        # both formats is worth more than matching a convention nobody here
        # reads: the whole reason this repo measured 0x00BBGGRR in the first
        # place is that a rotated channel order is invisible in review and
        # obvious only on the wall.
        #
        #   bits  [0:5]  red      [5:11]  green     [11:16] blue
        #
        # Halving bytes per pixel is the point. It halves the framebuffer a
        # card must hold AND the bandwidth it must read, which are the two
        # limits that decide how dense a wall can get -- see
        # docs/BENCHMARKS.md section 4d.
        if pix_bits == 16:
            # Which half of the word this pixel is in. The framebuffer is
            # addressed in WORDS, so the pixel index is halved and its low bit
            # selects.
            # Which half of the word this pixel is in. With the group reads
            # pipelined, this must follow the group whose word is ARRIVING
            # (`recvd` in the FSM below), not the one whose address is going
            # out: those name different groups while reads are in flight, and
            # selecting with the issue-side group would decode every word with
            # the wrong half on a 16-bit framebuffer.
            half = Signal()
            px16 = Signal(16)
            self.comb += px16.eq(Mux(half, self.mem_dat[16:32], self.mem_dat[0:16]))

            def expand(v, bits):
                """5 or 6 bits to 8, reaching full scale.

                A plain shift left leaves the top code short of white --
                0b11111 << 3 is 0xF8, not 0xFF -- so the high bits are folded
                back into the low ones. Same trick as brightness12().
                """
                return Cat(v[bits - (8 - bits):bits], v)

            rawbytes = [expand(px16[0:5], 5), expand(px16[5:11], 6),
                        expand(px16[11:16], 5)]
        else:
            half = None
            rawbytes = [self.mem_dat[0:8], self.mem_dat[8:16], self.mem_dat[16:24]]
        if shared_gamma is not None:
            live = list(shared_gamma)
        elif gamma:
            conv = [Icn1065Gamma(bits_out=grey_bits) for _ in range(3)]
            self.submodules += conv
            self.comb += [conv[i].i.eq(rawbytes[i]) for i in range(3)]
            live = [c.o for c in conv]
        else:
            live = [Cat(b[16 - grey_bits:8], b) for b in rawbytes]   # brightness()

        # Every group but the last is latched; the last is used live, which is
        # what makes `valid` a single cycle with all channels correct at once.
        stored = [[Signal(grey_bits) for _ in range(3)]
                  for _ in range(groups - 1)]

        # Latched copies of the requested pixel's coordinates.
        #
        # `self.row`/`self.col` are driven COMBINATIONALLY from the output
        # stage's counters (`src.col.eq(out.pix_col)` in the SoC, and
        # `pix_col` follows `Cat(chan_eff, sect)`), and the output advances
        # `sect` on EVERY load. Servicing one request takes several cycles --
        # READ0, GAP0, READ1, GAP1, ... plus memory latency -- and every READ
        # state recomputed `address(g)` from those live signals. A load
        # landing partway through therefore moved the address mid-sequence,
        # and the groups read after it came from a DIFFERENT column: one
        # pixel assembled out of two.
        #
        # Within a sweep `chan` is fixed and `sect` walks, so consecutive
        # loads are 16 columns apart -- the mix is not subtle. It also gets
        # likelier as the panel clock rises: at clk_div=6 a load is 96 sys
        # cycles away and the read finishes comfortably; at clk_div=2 it is
        # 32, which is the same place starvation shows up on glass.
        #
        # Latch once when the request is accepted and read from the latch, so
        # the address cannot move underneath the sequence.
        row_l = Signal(max=rows_per_frame)
        col_l = Signal(max=columns)

        def linear(g):
            """This pixel's index within the framebuffer, in PIXELS."""
            if group_offsets is not None:
                return row_l * width + col_l + group_offsets[g]
            return (row_l + g * rows_per_frame) * width + col_l

        def address(g):
            # 16-bit pixels pack two to a word, so the word address is the
            # pixel index halved; `half` picks which one.
            if pix_bits == 16:
                return fb_base + (linear(g) >> 1)
            return fb_base + linear(g)

        # `channels` is REGISTERED and holds between pixels. See the comment
        # at the last READ state for why a combinational one-cycle publish was
        # wrong. `valid` is registered alongside, so the two still line up and
        # a consumer that waits for `valid` reads the matching channels.
        held = [Signal(grey_bits) for _ in range(n)]
        valid_r = Signal()
        self.sync += valid_r.eq(0)
        self.comb += [self.valid.eq(valid_r)]
        self.comb += [self.channels[i].eq(held[i]) for i in range(n)]

        # Four group reads, ALL IN FLIGHT AT ONCE.
        #
        # This was READ0 -> GAP0 -> READ1 -> GAP1 -> READ2 -> GAP2 -> READ3,
        # each READ waiting for its DATA before the next command went out. A
        # pixel therefore cost `4L + 16` system cycles for a memory round trip
        # L -- measured 20/32/48/80 at L = 1/4/8/16 -- against a budget of one
        # pixel per 16 panel bits, i.e. `16 * clk_div`: 64 at clk_div=4, 96 at
        # 6. At LiteDRAM's realistic L=16 the path needed 80 against 64 and
        # starved, with ZERO arbiter contention, before four outputs began
        # competing for the same port.
        #
        # Nothing in the path backpressures -- PIXELS advances on ser.loading
        # alone and never waits on valid, the serialiser reloads
        # unconditionally, and `held` keeps its last value -- so a missed
        # deadline silently re-shifts the PREVIOUS word. The source re-samples
        # the current coordinates each time it goes idle, so once its fetch
        # time exceeds the pixel period it settles into a PERMANENT lag of
        # m-1 loads. The loads whose pixel falls in the previous row are then
        # exactly (chan=0, sect=0..m-2), i.e. display columns 0, 16, 32 ... --
        # one wrong pixel per chip on every row, carrying the adjacent row's
        # value, invisible on a solid fill. See docs/E320-BRINGUP.md.
        #
        # So split the handshake and pipeline it. `issued` walks the group
        # addresses as fast as the arbiter takes commands (`mem_cmd_ack`), and
        # `recvd` consumes the words as they come back IN ORDER (`mem_ack`),
        # which is what LiteDRAM's native port guarantees and what the
        # arbiter's tag FIFO preserves. A pixel now costs about `L + groups`.
        #
        # The GAP states are gone with the serialisation that needed them.
        # They existed because a memory holding `ack` high -- which a simple
        # one is entitled to do -- would satisfy the next read in the same
        # breath and give every group the first group's colour. That cannot
        # happen now: a command is consumed on `mem_cmd_ack` and a word on
        # `mem_ack`, counted separately, so a held `ack` advances `recvd`
        # exactly as many times as there are words.
        issued = Signal(max=groups + 1)
        recvd = Signal(max=groups + 1)

        # 16-bit pixels pack two to a word, so `half` picks which one. It has
        # to exist TWICE once the two sides are decoupled: the ISSUE side needs
        # the half of the group whose address is going out, the RECEIVE side
        # the half of the group whose word just came back, and with reads in
        # flight those are different groups.
        if pix_bits == 16:
            # The decode selector follows the group whose word is arriving.
            self.comb += Case(recvd, {g: half.eq(linear(g)[0])
                                      for g in range(groups)})

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(self.req,
                # Capture the coordinates for the whole read sequence, so the
                # address cannot move underneath it: `self.row`/`self.col` are
                # driven combinationally from the output stage's counters and
                # those advance on every load.
                NextValue(row_l, self.row),
                NextValue(col_l, self.col),
                NextValue(issued, 0),
                NextValue(recvd, 0),
                NextState("XFER"),
            ),
        )
        fsm.act("XFER",
            # ---- issue side: keep commands going out until all are placed
            self.mem_req.eq(issued != groups),
            Case(issued, {g: [self.mem_adr.eq(address(g))]
                          for g in range(groups)}),
            # The ISSUE side needs no `half` of its own: `address(g)` already
            # halves the pixel index for a 16-bit framebuffer, and which half
            # of the returned word to decode is the RECEIVE side's question,
            # answered from `recvd` above.

            If((issued != groups) & self.mem_cmd_ack,
                NextValue(issued, issued + 1),
            ),
            # ---- receive side: words arrive in order, one per group
            If(self.mem_ack,
                Case(recvd, {
                    g: ([NextValue(stored[g][c], live[c]) for c in range(3)]
                        if g != groups - 1 else
                        # The last word completes the pixel: publish the
                        # earlier groups from their latches and this one
                        # straight off the bus, so `valid` is a single cycle
                        # with every channel good at once.
                        [NextValue(held[3 * k + c], stored[k][c])
                         for k in range(groups - 1) for c in range(3)]
                        + [NextValue(held[3 * (groups - 1) + c], live[c])
                           for c in range(3)]
                        + [NextValue(valid_r, 1)])
                    for g in range(groups)
                }),
                If(recvd == groups - 1,
                    NextState("IDLE"),
                ).Else(
                    NextValue(recvd, recvd + 1),
                ),
            ),
        )


def best_pix_bits(columns, rows_per_frame, n_outputs, groups=GROUPS_HUB75,
                  half_bytes=None):
    """The widest framebuffer pixel that still FITS this wall. More is better.

    Colour depth is free until the framebuffer runs out, so the right default
    is the widest pixel that fits rather than the smallest that works -- a
    9-module wall has room to spare at 32 bits and there is no reason to spend
    its colour on headroom it will never use.

    32-bit is 8 bits per channel, the full source depth. 16-bit is B5 G6 R5,
    which bands visibly in bright gradients because gamma 2.8 is steep at the
    top: the two highest red codes are 8% of full brightness apart, against 1%
    at 8 bits. So it is a last resort, not a tuning knob.

    Returns 32 or 16. If even 16 does not fit, returns 16 anyway and lets the
    build-time assertion in the SoC report the real problem -- silently
    choosing a format for a wall that cannot be stored either way would hide
    it.
    """
    px = columns * rows_per_frame * groups * n_outputs
    for bits in (32, 16):
        if px <= framebuffer_pixels(bits // 8, half_bytes):
            return bits
    return 16


def framebuffer_pixels(px_bytes=4, half_bytes=None):
    """How many pixels one framebuffer half holds.

    The half is a fixed number of BYTES (hub75.FB_HALF_BYTES), so the pixel
    capacity doubles when the pixel halves. This is the limit that bites first
    on a dense wall -- before bandwidth, before logic.
    """
    from hub75 import FB_HALF_BYTES
    return (half_bytes or FB_HALF_BYTES) // px_bytes


def sustainable_refresh(columns, rows_per_frame, n_outputs, sys_clk=40e6,
                        efficiency=0.60, bus_bytes=2, groups=GROUPS_HUB75,
                        px_bytes=4):
    """The data refresh a wall can actually sustain, and the shift clock it implies.

    Returns `(refresh_hz, shift_clk_hz, bandwidth_bytes_per_second)`.

    This exists because "the natural rate is 73.6 Hz but SDRAM only allows ~30"
    is only half a design note. The shift clock is what SETS the data refresh --
    a frame is `frame_clocks()` shift clocks long, always -- so a wall that
    cannot feed 20 MHz does not drop frames at 20 MHz, it has to be clocked
    slower. Saying "about 30 Hz" without saying "so clock the panel at 9 MHz,
    not 20" leaves the actual design decision unmade.

    `efficiency` is the fraction of the SDRAM's theoretical peak that survives
    refresh, precharge, row misses and the incoming pixel DMA's own writes. It
    is a guess, and it is the least trustworthy number here -- which is why it
    is an argument rather than a constant, and why the caller is told what was
    assumed.
    """
    peak = sys_clk * bus_bytes
    budget = peak * efficiency
    clocks = frame_clocks(columns, rows_per_frame)
    # Bytes read per frame: one 32-bit word per pixel, both halves, all outputs.
    bytes_per_frame = columns * rows_per_frame * groups * n_outputs * px_bytes
    refresh = budget / bytes_per_frame
    return refresh, refresh * clocks, budget


def shift_divider(columns, rows_per_frame, n_outputs, sys_clk=40e6,
                  efficiency=0.60, min_div=2, groups=GROUPS_HUB75, px_bytes=4):
    """Pick the shift-clock divider for a wall: `(divider, shift_hz, refresh_hz)`.

    The shift clock is `sys_clk / divider` and the divider is an integer, so the
    answer is not the sustainable rate itself but the first divider at or below
    it. `min_div` is 2 because `hub75.py` shifts at sys/2 and nothing here goes
    faster.

    Worth reading together with `sustainable_refresh()`: a small wall comes out
    at `min_div` because it is limited by the shift clock rather than by SDRAM,
    and a large one comes out slower because it is limited by SDRAM. Those are
    different constraints and the divider is where they meet.
    """
    _, want_shift, _ = sustainable_refresh(columns, rows_per_frame, n_outputs,
                                           sys_clk, efficiency, groups=groups,
                                           px_bytes=px_bytes)
    div = min_div
    while sys_clk / div > want_shift:
        div += 1
    shift = sys_clk / div
    return div, shift, shift / frame_clocks(columns, rows_per_frame)


def sdram_bytes_per_second(columns, rows_per_frame, n_outputs, refresh_hz,
                           groups=GROUPS_HUB75, px_bytes=4):
    """Framebuffer read bandwidth for a wall at a given data refresh rate.

    `px_bytes` per pixel, `groups` pixels per request, every frame. This is the
    number that decides what a 3x3 wall can actually run at -- see
    docs/BENCHMARKS.md section 4c. The FPGA is not the constraint; this is.

    `px_bytes` is 4 for the 32-bit framebuffer and 2 for RGB565, and halving it
    halves this directly. On a dense wall that is not an optimisation: a
    524,288 px wall does not FIT the framebuffer at 4 bytes per pixel at all.

    Note that `groups` does not change the bytes per PIXEL, only how many
    pixels one connector carries. HUB320 costs pins, not bandwidth.
    """
    px = columns * rows_per_frame * groups * n_outputs
    return px * refresh_hz * px_bytes


class Icn1065SharedGamma(Module):
    """One set of three gamma converters for every source behind an arbiter.

    Feed it the arbiter's returning data word and hand `self.channels` to each
    `Icn1065PixelSource` as `shared_gamma`. See the comment in
    `Icn1065PixelSource.__init__` for why one set is enough for nine outputs.
    """

    def __init__(self, dat, grey_bits=GREY_BITS):
        self.channels = [Signal(grey_bits) for _ in range(3)]
        conv = [Icn1065Gamma(bits_out=grey_bits) for _ in range(3)]
        self.submodules += conv
        for i, c in enumerate(conv):
            self.comb += [c.i.eq(dat[i * 8:(i + 1) * 8]),
                          self.channels[i].eq(c.o)]


class Icn1065PaletteStage(Module):
    """Indexed-colour lookup on the word coming back from SDRAM, one cycle.

    On-device programs render INDEXED pixels -- one palette-index byte per
    pixel in the low byte of the framebuffer word -- and the HUB75 stage looks
    them up through a palette memory on the way to the panel. This stage did
    not, so a program on an S-PWM panel showed its indices as red intensities
    (glass, 2026-10-19: shapes and text right, every colour wrong).

    Sits between the SDRAM port and the arbiter: when `indexed` is set the
    returning word is replaced by `palette[bank][word[7:0]]`, otherwise it
    passes through unchanged. Either way it is delayed by exactly one cycle,
    with `valid` delayed alongside, so the arbiter sees a memory one cycle
    slower and nothing else changes -- data still returns in order and the
    gamma stage downstream still sees 0x00BBGGRR words.

    `bank` selects which half of a two-bank palette is live, the same way
    hub75.py does (RB-5516): the palette flips with the framebuffer so a pixel
    and the entry it names always come from the same frame.
    """

    def __init__(self, palette_mem, indexed, bank, dat_in, valid_in):
        self.dat = Signal(32)
        self.valid = Signal()
        port = palette_mem.get_port()            # synchronous read: 1 cycle
        self.specials += port
        raw = Signal(32)
        self.comb += port.adr.eq(Cat(dat_in[0:8], bank))
        self.sync += [raw.eq(dat_in), self.valid.eq(valid_in)]
        self.comb += self.dat.eq(Mux(indexed, port.dat_r, raw))


class Icn1065Arbiter(Module):
    """Round-robin one memory port between N pixel sources.

    Each source presents `adr`/`req` and waits for `ack`, and exactly one is
    connected to the shared port at a time. The grant is held for the whole of
    a transfer rather than re-decided every cycle: splitting a transfer across
    two grants would hand the returning data to whoever held the port when
    `ack` arrived, which is the same class of bug as the sticky-ack one that
    `Icn1065PixelSource`'s GAP state exists to avoid.

    Round-robin rather than fixed priority because the sources are equals and
    the schedule has to be FAIR, not fast: source 8 missing its 16-clock window
    tears output 8's row exactly as badly as source 0 missing its own. Fixed
    priority starves the high-numbered outputs under precisely the load this
    design runs at.

    This is where the SDRAM budget in docs/BENCHMARKS.md section 4c stops being
    a spreadsheet. Nine sources each want two 32-bit reads per pixel and a pixel
    is 16 shift clocks, so the port must sustain 18 reads per 16 shift clocks --
    which is why the wall has to be clocked at sys/4 rather than sys/2. See
    `shift_divider()`, and `check_arbiter()` for the simulation that holds it
    to that.
    """

    def __init__(self, n_sources):
        # bits_for(0) is 0, so a single source builds zero-width tag signals
        # and a zero-width tag Memory, and Migen asserts. One output is a
        # legitimate configuration -- a card driving one connector -- so widen
        # the degenerate case rather than forbidding it.
        tag_w = max(1, bits_for(n_sources - 1))
        self.src_adr = [Signal(32) for _ in range(n_sources)]
        self.src_req = [Signal() for _ in range(n_sources)]
        self.src_ack = [Signal() for _ in range(n_sources)]

        # The shared port, the same shape as Icn1065PixelSource's.
        self.mem_adr = Signal(32)
        self.mem_dat = Signal(32)
        self.mem_req = Signal()
        self.mem_ack = Signal()

        # Exposed for the tests, and useful on a logic analyser.
        self.grant = Signal(tag_w)

        # # #

        any_req = Signal()
        self.comb += any_req.eq(reduce(lambda a, b: a | b, self.src_req))

        # Pick the next asking source, searching from `grant` upward and
        # wrapping -- a priority chain over a rotated order, one arm per
        # possible starting point. That is n muxes rather than a barrel
        # shifter, and n is nine.
        pick = Signal(tag_w)
        cases = {}
        for start in range(n_sources):
            order = [(start + k) % n_sources for k in range(n_sources)]
            arm = pick.eq(order[-1])
            for cand in reversed(order[:-1]):
                arm = If(self.src_req[cand], pick.eq(cand)).Else(arm)
            cases[start] = arm
        self.comb += Case(self.grant, cases)

        self.submodules.fsm = fsm = FSM(reset_state="PICK")
        fsm.act("PICK",
            If(any_req,
                NextValue(self.grant, pick),
                NextState("SERVE"),
            ),
        )
        fsm.act("SERVE",
            self.mem_req.eq(1),
            # Only the granted source drives the bus or sees the ack.
            Case(self.grant, {
                i: [self.mem_adr.eq(self.src_adr[i]),
                    self.src_ack[i].eq(self.mem_ack)]
                for i in range(n_sources)
            }),
            If(self.mem_ack,
                # Step past the source just served so the next search starts at
                # the following one. Without this the same source wins every
                # arbitration and the rest never get the port.
                If(self.grant == n_sources - 1,
                    NextValue(self.grant, 0),
                ).Else(
                    NextValue(self.grant, self.grant + 1),
                ),
                NextState("PICK"),
            ),
        )

    def connect(self, sources):
        """Statements wiring a list of `Icn1065PixelSource` onto this arbiter."""
        r = []
        for i, s in enumerate(sources):
            r += [self.src_adr[i].eq(s.mem_adr),
                  self.src_req[i].eq(s.mem_req),
                  s.mem_dat.eq(self.mem_dat),
                  s.mem_ack.eq(self.src_ack[i]),
                  # This arbiter holds the port for the whole transfer, so a
                  # command is accepted exactly when its data returns.
                  s.mem_cmd_ack.eq(self.src_ack[i])]
        return r


class Icn1065LineBuffer(Module):
    """A row of pixels, fetched ahead, so memory latency is not a deadline.

    THE PROBLEM THIS EXISTS FOR. `Icn1065PixelSource` reads on demand and the
    serialiser has a hard deadline: all `groups` reads must land inside its
    16-clock window or it shifts whatever was on the bus, which is a white dot
    on the glass. That couples picture correctness to memory latency, and
    memory latency moves with DRAM refresh, with the pixel DMA's writes, and
    with however many outputs are arbitrating. Measured 2026-10-20: streaming
    at 20 fps put 13.8 underruns per second on a panel that was spotless when
    nothing was writing.

    Everything that looked like a separate fault was that one coupling --
    snow, the panel-clock sensitivity (a faster clock is a narrower deadline),
    and write contention. Both workarounds found for it, spacing the DMA's
    writes and slowing the panel clock, are just ways of widening the deadline,
    and neither removes it.

    `hub75.py` has had a row buffer since the beginning. The S-PWM path skipped
    it to save one or two block RAMs per output, reasoning in
    `Icn1065PixelSource`'s docstring that "a pixel lasts 800 ns, which is
    comfortably more than an SDRAM read". True in isolation; false the moment
    anything else touches the memory.

    HOW IT WORKS. One row is `columns` pixels and the serialiser visits every
    column of a row exactly once -- its `col` is `Cat(chan_eff, sect)`, which
    is not ascending, but it IS a permutation. So the fill walks columns 0..N-1
    in plain order and stores each at its own index, and the read side indexes
    by whatever column the serialiser asks for. Order-independent by
    construction, which is what keeps this from re-introducing the 16-column
    displacement that cost the first bring-up weeks.

    Two buffers, selected by the low bit of the row. The serialiser reads the
    row it is on while the fill engine works on the next one, and because the
    fill runs at full system clock while the serialiser consumes one pixel per
    16 panel bits, the fill finishes a row long before it is needed -- roughly
    six times over at `clk_div 6`. The frame prefix (~1332 clocks) covers
    priming row 0 before any pixel is shifted.

    The read is a plain memory read with fixed one-cycle latency. Nothing
    downstream can starve any more: there is no path from DRAM timing to the
    shift register.
    """

    def __init__(self, columns, rows_per_frame, groups=GROUPS_HUB75,
                 grey_bits=GREY_BITS):
        n = 3 * groups
        width = n * grey_bits

        # The two halves are selected by the LOW BIT of the row, so fill and
        # read only stay apart while consecutive rows have opposite parity.
        # With an odd `rows_per_frame` the wrap from the last row to row 0
        # keeps the same parity and the fill walks straight into the half being
        # read -- simulated at rows=3, the entire wrap row landed in the live
        # half. Every preset is 24/32/64, so this is a guard rather than a
        # fix, but nothing else checks it.
        assert rows_per_frame % 2 == 0, (
            f"line buffer needs an even scan count, got {rows_per_frame}")

        # --- serialiser side -------------------------------------------------
        self.col = Signal(max=columns)
        self.row = Signal(max=rows_per_frame)
        self.channels = [Signal(grey_bits) for _ in range(n)]
        # High once the buffer holding `row` has been filled for THIS row.
        # The serialiser does not wait on it -- it cannot -- but it makes the
        # one unprimed row at startup visible rather than silent.
        self.primed = Signal()

        # --- pixel source side -----------------------------------------------
        self.src_req = Signal()
        self.src_col = Signal(max=columns)
        self.src_row = Signal(max=rows_per_frame)
        self.src_valid = Signal()
        self.src_channels = [Signal(grey_bits) for _ in range(n)]

        # # #

        mem = Memory(width, 2 * columns)
        self.specials += mem
        wr = mem.get_port(write_capable=True)
        rd = mem.get_port()
        self.specials += wr, rd

        fill_col = Signal(max=columns)
        fill_row = Signal(max=rows_per_frame)
        # Which row each half currently holds, so `primed` can be honest.
        held = [Signal(max=rows_per_frame) for _ in range(2)]
        held_ok = [Signal() for _ in range(2)]

        # FILL: always one row ahead of the serialiser. The target follows the
        # serialiser's row rather than free-running, so a frame boundary or a
        # restart cannot leave the two walking different rows.
        next_row = Signal(max=rows_per_frame)
        self.comb += If(self.row == rows_per_frame - 1,
                        next_row.eq(0)).Else(next_row.eq(self.row + 1))

        # FETCH ONLY WHAT IS MISSING, then idle.
        #
        # The first version tied `src_req` high and, on finishing a row,
        # reloaded `fill_row` from `next_row` -- which still named the row just
        # filled, because the serialiser had not moved yet. So it re-walked the
        # same row into the same half continuously. Simulated at 8 columns: 74
        # writes to fill 8 cells, about nine redundant passes, and 64 of 288
        # writes landed in the half being READ once the serialiser advanced
        # mid-pass -- a torn row once per frame.
        #
        # Worse, the read port then never yielded, which starves the pixel
        # DMA's write port: three times the DRAM read traffic of the on-demand
        # path this was written to improve on, and the contention it was
        # supposed to remove made worse.
        #
        # So: start a fill only when the half for the NEXT row does not already
        # hold it, run that row to completion, then stop.
        need = Signal()
        filling = Signal()
        self.comb += need.eq(~Array(held_ok)[next_row[0]]
                             | (Array(held)[next_row[0]] != next_row))
        self.comb += [
            self.src_req.eq(filling),
            self.src_col.eq(fill_col),
            self.src_row.eq(fill_row),
            # `half * columns + col`, NOT Cat(col, half): the memory is
            # 2*columns deep, so with a non-power-of-two `columns` the
            # concatenation puts half 1 at 2**len(col) and runs off the end.
            # Live presets are 128 and 256, but PANELS["96x48"] is 96.
            wr.adr.eq(fill_row[0] * columns + fill_col),
            wr.dat_w.eq(Cat(*self.src_channels)),
            wr.we.eq(self.src_valid & filling),
        ]
        self.sync += [
            If(~filling,
                If(need,
                    filling.eq(1),
                    fill_row.eq(next_row),
                    fill_col.eq(0),
                    # The half stops being trustworthy the moment we start
                    # overwriting it.
                    Case(next_row[0], {0: [held_ok[0].eq(0)],
                                       1: [held_ok[1].eq(0)]}),
                ),
            ).Elif(self.src_valid,
                If(fill_col == columns - 1,
                    filling.eq(0),
                    Case(fill_row[0], {
                        0: [held[0].eq(fill_row), held_ok[0].eq(1)],
                        1: [held[1].eq(fill_row), held_ok[1].eq(1)],
                    }),
                ).Else(
                    fill_col.eq(fill_col + 1),
                ),
            ),
        ]

        # READ: one cycle, by construction.
        self.comb += rd.adr.eq(self.row[0] * columns + self.col)
        self.comb += [self.channels[i].eq(rd.dat_r[i * grey_bits:(i + 1) * grey_bits])
                      for i in range(n)]
        self.comb += self.primed.eq(Array(held_ok)[self.row[0]]
                                    & (Array(held)[self.row[0]] == self.row))


class Icn1065PipelinedArbiter(Module):
    """Round-robin N sources onto one memory port, WITHOUT waiting for data.

    `Icn1065Arbiter` holds the port for the whole of a transfer, so exactly one
    read is ever in flight and the cost of a pixel pair is
    `2 * n_sources * latency` clocks. Simulated against a 16-cycle memory --
    which is the order of magnitude a LiteDRAM native port actually costs, not
    the 4 the first version of this test assumed -- that is 291 clocks per pixel
    pair, forcing sys/19 and a 7.7 Hz wall. Unusable.

    The problem is not bandwidth. Nine sources want 18 words per pixel and the
    SDRAM can deliver about 0.5 words per clock, so the data itself fits. The
    problem is that a blocking arbiter converts a LATENCY into a THROUGHPUT
    limit by refusing to have more than one request outstanding.

    So: split transactions. Commands are issued round-robin as fast as the
    memory will take them and the source id rides along in a tag FIFO; returning
    data is matched to its source by popping that FIFO. Nine sources give nine
    outstanding reads, so 9 words arrive every `max(latency, 9)` clocks and the
    18-word pixel pair costs ~32 clocks regardless of latency.

    The memory interface is therefore split, unlike `Icn1065Arbiter`'s:

        cmd_adr / cmd_valid / cmd_ready     command accepted this cycle
        dat / dat_valid                     data returns, IN ORDER

    In-order return is required and is what LiteDRAM's native port gives.
    """

    def __init__(self, n_sources, max_outstanding=None, per_source=1):
        # `per_source` is how many reads ONE source may have in flight.
        #
        # It was fixed at one, which capped a source's own pixel at
        # `groups * latency`: Icn1065PixelSource reads its four RGB groups
        # STRICTLY SEQUENTIALLY, each read waiting for data rather than for
        # command acceptance, so a pixel cost 4L + 16 system cycles -- 80 at
        # LiteDRAM's realistic L=16 -- against a budget of 16 * clk_div, which
        # is 64 at clk_div=4 and 96 at 6. This arbiter pipelined ACROSS sources
        # and could not help WITHIN one, so the four-group path starved; and
        # because nothing in that path backpressures, the symptom was stale
        # pixels with a healthy refresh_count rather than dropped frames.
        #
        # With per_source = groups the four commands go out back to back and a
        # pixel costs about L + groups instead of 4L + 16.
        # bits_for(0) is 0, so a single source builds zero-width tag signals
        # and a zero-width tag Memory, and Migen asserts. One output is a
        # legitimate configuration -- a card driving one connector -- so widen
        # the degenerate case rather than forbidding it.
        tag_w = max(1, bits_for(n_sources - 1))
        self.src_adr = [Signal(32) for _ in range(n_sources)]
        self.src_req = [Signal() for _ in range(n_sources)]
        self.src_ack = [Signal() for _ in range(n_sources)]
        # Command ACCEPTED, as distinct from data RETURNED (`src_ack`). A
        # source needs both to pipeline: it advances its address on cmd_ack and
        # consumes a word on ack.
        self.src_cmd_ack = [Signal() for _ in range(n_sources)]

        self.cmd_adr = Signal(32)
        self.cmd_valid = Signal()
        self.cmd_ready = Signal()
        self.dat = Signal(32)
        self.dat_valid = Signal()

        self.grant = Signal(tag_w)

        # # #

        # At least 2: a one-entry tag FIFO degenerates its address signal to
        # zero width and Migen cannot build a port for it. Extra depth is
        # harmless -- `outstanding[i]` already stops a source being issued
        # twice, so a single source still has at most one read in flight.
        depth = max(2, max_outstanding or n_sources * per_source)
        # How many commands are out for each source with data not yet back.
        # This used to be ONE BIT per source, which is what capped a source at
        # a single in-flight read; it is a counter now, capped at `per_source`.
        outst = [Signal(max=per_source + 1) for _ in range(n_sources)]

        # Tag FIFO: which source each in-flight read belongs to, in order.
        tags = Memory(tag_w, depth)
        self.specials += tags
        wr = tags.get_port(write_capable=True)
        rd = tags.get_port(async_read=True)
        self.specials += wr, rd
        wptr = Signal(max(1, bits_for(depth - 1)))
        rptr = Signal(max(1, bits_for(depth - 1)))
        count = Signal(bits_for(depth))

        # Eligible = asking, and under its in-flight allowance.
        eligible = [self.src_req[i] & (outst[i] != per_source)
                    for i in range(n_sources)]
        any_elig = Signal()
        self.comb += any_elig.eq(reduce(lambda a, b: a | b, eligible))

        pick_c = Signal(tag_w)
        cases = {}
        for start in range(n_sources):
            order = [(start + k) % n_sources for k in range(n_sources)]
            arm = pick_c.eq(order[-1])
            for cand in reversed(order[:-1]):
                arm = If(eligible[cand], pick_c.eq(cand)).Else(arm)
            cases[start] = arm
        self.comb += Case(self.grant, cases)

        # REGISTER the choice. The rotated priority chain above is nine muxes
        # deep and, in the full SoC build, it was the critical path outright --
        # 26 MHz against a 40 MHz requirement, with the whole design failing
        # timing because of this one chain. Registering it takes the chain out
        # of the issue path entirely.
        #
        # The cost is that the registered choice may be a cycle stale: the
        # source it names can have been served in the meantime. So eligibility
        # is re-checked at issue, which is a single 9-to-1 mux rather than a
        # nine-deep chain. A stale pick costs one idle cycle and is corrected
        # on the next, which the simulation's throughput bound still absorbs.
        pick = Signal(tag_w)
        armed = Signal()
        self.sync += [
            pick.eq(pick_c),
            armed.eq(any_elig),
        ]
        still_eligible = Signal()
        self.comb += Case(pick, {i: still_eligible.eq(eligible[i])
                                 for i in range(n_sources)})

        issue = Signal()
        self.comb += [
            # Never issue more than the FIFO can remember.
            self.cmd_valid.eq(armed & still_eligible & (count < depth)),
            Case(pick, {i: self.cmd_adr.eq(self.src_adr[i])
                        for i in range(n_sources)}),
            issue.eq(self.cmd_valid & self.cmd_ready),
            wr.adr.eq(wptr),
            wr.dat_w.eq(pick),
            wr.we.eq(issue),
            rd.adr.eq(rptr),
        ]

        # Data comes back in order, so the head of the tag FIFO owns it.
        self.comb += [self.src_ack[i].eq(self.dat_valid & (rd.dat_r == i))
                      for i in range(n_sources)]
        # Whose command was just taken.
        self.comb += [self.src_cmd_ack[i].eq(issue & (pick == i))
                      for i in range(n_sources)]

        # `depth` is n_sources, which is not a power of two, so the pointers
        # have to be wrapped explicitly -- letting a 4-bit counter roll over at
        # 16 against a 9-entry memory would silently reorder the tags and hand
        # every source the wrong pixel.
        def bump(ptr):
            return If(ptr == depth - 1, ptr.eq(0)).Else(ptr.eq(ptr + 1))

        self.sync += [
            If(issue,
                bump(wptr),
                If(self.grant == n_sources - 1,
                    self.grant.eq(0),
                ).Else(
                    self.grant.eq(self.grant + 1),
                ),
            ),
            # An issue and a return for the SAME source can land in the same
            # cycle, so each counter moves by (+issue - return) rather than
            # being set and then cleared.
            *[outst[i].eq(outst[i]
                          + (issue & (pick == i))
                          - (self.dat_valid & (rd.dat_r == i)))
              for i in range(n_sources)],
            If(self.dat_valid, bump(rptr)),
            count.eq(count + issue - self.dat_valid),
        ]

    def connect(self, sources):
        """Statements wiring a list of `Icn1065PixelSource` onto this arbiter."""
        r = []
        for i, s in enumerate(sources):
            r += [self.src_adr[i].eq(s.mem_adr),
                  self.src_req[i].eq(s.mem_req),
                  s.mem_dat.eq(self.dat),
                  s.mem_ack.eq(self.src_ack[i]),
                  s.mem_cmd_ack.eq(self.src_cmd_ack[i])]
        return r
