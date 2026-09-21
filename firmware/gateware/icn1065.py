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

SPWM_BITS = 16       # bits clocked per pixel
GREY_BITS = 12       # of which the low 12 are greyscale; 15..12 must be zero

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

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(self.start,
                NextValue(shift, self.data),
                NextValue(count, self.length),
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
                NextValue(count, self.pad),
                # A zero-length pad must not wrap the counter into a 64-clock
                # command; go straight back to idle instead.
                If(self.pad == 0,
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


def brightness12(value8):
    """8-bit colour component to a 12-bit SPWM value, LINEAR (no gamma).

    A plain `v << 4` would never reach full scale (0xFF -> 0xFF0, not 0xFFF), so
    the high nibble is folded back in.

    This is the identity-ish path, kept for tests and for anyone who wants raw
    values. The display path uses `gamma12()` below.
    """
    return ((value8 << 4) | (value8 >> 4)) & 0x0FFF


GAMMA = 2.8            # same exponent as hub75.py's LUT
GAMMA_SEGMENTS = 16    # piecewise-linear segments, indexed by the top nibble


def gamma_table12(gamma=GAMMA, bits_out=GREY_BITS):
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
    """Piecewise-linear approximation of `gamma_table12`: (base[], slope[]).

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


def gamma12(value8, gamma=GAMMA, bits_out=GREY_BITS, segments=GAMMA_SEGMENTS):
    """Software twin of the `Icn1065Gamma` hardware -- bit-exact, for tests."""
    base, slope = _gamma_segments(gamma, bits_out, segments)
    step = 256 // segments
    seg, off = value8 // step, value8 % step
    return min((base[seg] + ((slope[seg] * off) >> 4)), (1 << bits_out) - 1)


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
        self.channels_in = [Signal(grey_bits) for _ in range(n)]
        self.channels_out = Signal(n)      # r1 g1 b1 r2 g2 b2 ..., one bit each

        # Kept so a single pixel can still be shifted in isolation (the unit
        # test does exactly that); equivalent to one cycle of `run`.
        self.start = Signal()

        # # #

        # Bits 15..12 must be zero, so the 12-bit value sits in the low bits of
        # a 16-bit window: the first four clocks of every pixel emit zero.
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
                 pulse_len=ROW_OE_PULSE, pulses=ROW_OE_CNT):
        assert slot_len > pulses * pulse_len, "slot too short for its OE pulses"
        self.start = Signal()
        self.busy = Signal()
        self.oe = Signal()
        self.row = Signal(max=rows_per_frame)   # for status/debug only
        self.frame_done = Signal()              # one-cycle strobe

        # # #

        left = Signal(max=slot_len + 1)
        pulse_left = Signal(max=max(pulses, 2))

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
            # Pulses sit at the head of the slot; the rest is hold time.
            self.oe.eq(left > (slot_len - pulses * pulse_len)),
            NextValue(left, left - 1),
            If(left == 1,
                If(self.row == rows_per_frame - 1,
                    self.frame_done.eq(1),
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

    def __init__(self, columns, rows_per_frame, slot_len=ROW_OE_LEN,
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
        self.pix_col = Signal(max=columns)
        self.pix_row = Signal(max=rows_per_frame)
        self.pix_in = [Signal(GREY_BITS) for _ in range(n)]

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
        self.submodules.ser = ser = Icn1065Serialiser(groups=groups)
        self.submodules.scan = scan = Icn1065RowScanner(rows_per_frame, slot_len)

        col = Signal(max=columns)
        row = Signal(max=rows_per_frame)

        if not self.shared_prefix:
            self.comb += prefix.start.eq(self.prefix_req)
        self.comb += [
            self.lat.eq(prefix.lat),
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
            self.pix_row.eq(row),
        ]

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(self.start,
                NextValue(col, 0),
                NextValue(row, 0),
                NextState("PREFIX"),
            ),
        )
        fsm.act("PREFIX",
            self.busy.eq(1),
            # Ask for the prefix rather than starting it. With a shared prefix
            # the board starts it once; with a private one this output does,
            # wired below. Either way the FSM here is identical.
            self.prefix_req.eq(1),
            NextState("PREFIX_WAIT"),
        )
        fsm.act("PREFIX_WAIT",
            self.busy.eq(1),
            If(~prefix.busy, NextState("PIXELS")),
        )
        fsm.act("PIXELS",
            self.busy.eq(1),
            ser.run.eq(1),
            # One fetch per pixel, issued as the serialiser takes the previous
            # one -- the address advances a pixel ahead of the shift register.
            self.pix_req.eq(ser.loading),
            If(ser.loading,
                If(col == columns - 1,
                    NextValue(col, 0),
                    If(row == rows_per_frame - 1,
                        NextState("DRAIN"),
                    ).Else(
                        NextValue(row, row + 1),
                    ),
                ).Else(
                    NextValue(col, col + 1),
                ),
            ),
        )
        fsm.act("DRAIN",
            # Let the final pixel finish shifting before the scan begins.
            self.busy.eq(1),
            If(~ser.busy, NextState("SCAN")),
        )
        # The row scan FREE-RUNS between frames. Two reasons, and the second is
        # the one that matters for a large wall:
        #
        #   1. The panel is only lit while the scan is walking rows. Stopping it
        #      to wait for the next frame would blank the display.
        #   2. It paces the data refresh. Pixel data costs framebuffer reads --
        #      294,912 px x 4 bytes x the refresh rate -- and at the natural
        #      73.6 Hz a 3x3 wall wants 87 MB/s, which this SDRAM does not have.
        #      Re-scanning without re-sending pixels costs nothing and lets the
        #      caller choose the refresh it can afford. See docs/BENCHMARKS.md.
        #
        # `frame_done` strobes after the first pass, so the caller knows the
        # pixel data was consumed and can start preparing the next frame.
        fsm.act("SCAN",
            self.busy.eq(1),
            scan.start.eq(1),
            NextState("SCAN_WAIT"),
        )
        fsm.act("SCAN_WAIT",
            self.busy.eq(1),
            If(~scan.busy,
                self.frame_done.eq(1),
                NextState("HOLD"),
            ),
        )
        fsm.act("HOLD",
            # Idle for the protocol's purposes, but still scanning: the caller
            # pulses `start` when it wants new pixels shown.
            scan.start.eq(~scan.busy),
            If(self.start & ~scan.busy,
                NextValue(col, 0),
                NextValue(row, 0),
                NextState("PREFIX"),
            ),
        )


def frame_clocks(columns, rows_per_frame, slot_len=ROW_OE_LEN):
    """Clocks per frame for one output -- the basis of every rate estimate.

    Prefix is the VSYNC/PRE_ACT train plus five row-length register blocks.
    """
    prefix = (V_SYNC + CLK_PAD + PRE_ACT1 + CLK_PAD + PRE_ACT2 + CLK_PAD
              + 5 * columns)
    pixels = columns * rows_per_frame * SPWM_BITS
    scan = rows_per_frame * slot_len
    return prefix + pixels + scan


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
                 groups=GROUPS_HUB75, pix_bits=32):
        assert pix_bits in (32, 16), "framebuffer pixel is 32 or 16 bits"
        n = 3 * groups
        self.groups = groups
        self.pix_bits = pix_bits
        self.req = Signal()                 # one pixel per group, please
        self.col = Signal(max=columns)
        self.row = Signal(max=rows_per_frame)
        self.valid = Signal()               # channels are good this cycle
        self.channels = [Signal(grey_bits) for _ in range(n)]

        # Generic memory port.
        self.mem_adr = Signal(32)
        self.mem_dat = Signal(32)
        self.mem_req = Signal()
        self.mem_ack = Signal()

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
            live = [Cat(b[4:8], b) for b in rawbytes]       # brightness12()

        # Every group but the last is latched; the last is used live, which is
        # what makes `valid` a single cycle with all channels correct at once.
        stored = [[Signal(grey_bits) for _ in range(3)]
                  for _ in range(groups - 1)]

        def linear(g):
            """This pixel's index within the framebuffer, in PIXELS."""
            return (self.row + g * rows_per_frame) * width + self.col

        def address(g):
            # 16-bit pixels pack two to a word, so the word address is the
            # pixel index halved; `half` picks which one.
            if pix_bits == 16:
                return fb_base + (linear(g) >> 1)
            return fb_base + linear(g)

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            If(self.req,
                self.mem_adr.eq(address(0)),
                *([half.eq(linear(0)[0])] if pix_bits == 16 else []),
                NextState("READ0"),
            ),
        )
        for g in range(groups):
            last = (g == groups - 1)
            fsm.act(f"READ{g}",
                self.mem_adr.eq(address(g)),
                *([half.eq(linear(g)[0])] if pix_bits == 16 else []),
                self.mem_req.eq(1),
                If(self.mem_ack,
                    # Not the last group: latch its corrected channels.
                    *([NextValue(stored[g][c], live[c]) for c in range(3)]
                      if not last else []),
                    # The last group: publish everything at once, the earlier
                    # groups from their latches and this one straight off the
                    # bus, so `valid` is a single cycle with all channels good.
                    *([self.valid.eq(1)]
                      + [self.channels[3 * k + c].eq(stored[k][c])
                         for k in range(groups - 1) for c in range(3)]
                      + [self.channels[3 * (groups - 1) + c].eq(live[c])
                         for c in range(3)]
                      if last else []),
                    NextState("IDLE" if last else f"GAP{g}"),
                ),
            )
            if not last:
                fsm.act(f"GAP{g}",
                    # One cycle with req LOW between consecutive reads.
                    #
                    # Without it a memory that holds `ack` asserted -- which a
                    # simple one is entitled to do -- satisfies the NEXT read in
                    # the same breath as this one, and every group after the
                    # first gets the first group's colour. The simulation caught
                    # exactly that: all six channels came back identical.
                    NextState(f"READ{g + 1}"),
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
        self.src_adr = [Signal(32) for _ in range(n_sources)]
        self.src_req = [Signal() for _ in range(n_sources)]
        self.src_ack = [Signal() for _ in range(n_sources)]

        # The shared port, the same shape as Icn1065PixelSource's.
        self.mem_adr = Signal(32)
        self.mem_dat = Signal(32)
        self.mem_req = Signal()
        self.mem_ack = Signal()

        # Exposed for the tests, and useful on a logic analyser.
        self.grant = Signal(bits_for(n_sources - 1))

        # # #

        any_req = Signal()
        self.comb += any_req.eq(reduce(lambda a, b: a | b, self.src_req))

        # Pick the next asking source, searching from `grant` upward and
        # wrapping -- a priority chain over a rotated order, one arm per
        # possible starting point. That is n muxes rather than a barrel
        # shifter, and n is nine.
        pick = Signal(bits_for(n_sources - 1))
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
                  s.mem_ack.eq(self.src_ack[i])]
        return r


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

    def __init__(self, n_sources, max_outstanding=None):
        self.src_adr = [Signal(32) for _ in range(n_sources)]
        self.src_req = [Signal() for _ in range(n_sources)]
        self.src_ack = [Signal() for _ in range(n_sources)]

        self.cmd_adr = Signal(32)
        self.cmd_valid = Signal()
        self.cmd_ready = Signal()
        self.dat = Signal(32)
        self.dat_valid = Signal()

        self.grant = Signal(bits_for(n_sources - 1))

        # # #

        depth = max_outstanding or n_sources
        # One bit per source: a command is out and its data has not come back.
        # This is what stops a source being issued twice, since it holds `req`
        # high until it sees `ack`.
        outstanding = Signal(n_sources)

        # Tag FIFO: which source each in-flight read belongs to, in order.
        tags = Memory(bits_for(n_sources - 1), depth)
        self.specials += tags
        wr = tags.get_port(write_capable=True)
        rd = tags.get_port(async_read=True)
        self.specials += wr, rd
        wptr = Signal(bits_for(depth - 1))
        rptr = Signal(bits_for(depth - 1))
        count = Signal(bits_for(depth))

        # Eligible = asking and not already in flight.
        eligible = [self.src_req[i] & ~outstanding[i] for i in range(n_sources)]
        any_elig = Signal()
        self.comb += any_elig.eq(reduce(lambda a, b: a | b, eligible))

        pick_c = Signal(bits_for(n_sources - 1))
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
        pick = Signal(bits_for(n_sources - 1))
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
            # `outstanding` is set by an issue and cleared by a return, and both
            # can land in the same cycle. Written as one expression so the
            # order of the two effects is explicit rather than incidental.
            outstanding.eq((outstanding | Mux(issue, 1 << pick, 0))
                           & ~Mux(self.dat_valid, 1 << rd.dat_r, 0)),
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
                  s.mem_ack.eq(self.src_ack[i])]
        return r
