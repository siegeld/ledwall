"""Driver-chip descriptors for the S-PWM family.

Adding a chip should be adding a table here and nothing else. That is the whole
point of this file: the gateware is one engine, the differences are data, and
the firmware loads the data at boot.

WHY THIS IS A TABLE AND NOT A CIRCUIT
-------------------------------------
Every chip in this family speaks the same grammar -- a few bare LAT bursts,
then N 16-bit slots each latched over their last five clocks, with one slot
rotating through the chip's configuration one word per frame. They differ in:

  - the register payload, which is DIFFERENT PER COLOUR LINE (the R, G and B
    chips are separate parts and do not have to agree);
  - how many slots follow the command bursts;
  - whether the middle 11-clock LAT burst is sent at all;
  - which register carries the scan row count, and which the brightness.

None of that is per-pixel. It happens once per frame, in the prefix, while no
pixel data is moving -- which is what makes generality free here. The serialiser,
the pixel source, the arbiter and the gamma units are common to the family and
are not parameterised by chip at all.

SOURCES
-------
The ICND1065 table was transcribed from `nimakolahi/hub75-icn1065`, an ESP32
driver. The DP3364S table was captured from a working 128x64 1/64-scan panel by
the Falcon Player project (`FalconChristmas/fpp`,
`src/non-gpl/BBShiftPanel/BBShiftPanel.cpp`), which in turn cites
hzeller/rpi-rgb-led-matrix issue #1821. The chip's own datasheet documents the
access protocol but NOT the register map -- which is why a hobbyist capture is
the authoritative source here, and why this file cites where each number came
from rather than presenting them as facts.

**A capture is only strictly valid for the geometry it was taken at.** Registers
other than the scan count change with scan rate on these parts, so a table used
at a different scan rate is an extrapolation. `capture_scan` records what each
was taken at.

NOTHING HERE HAS BEEN TESTED ON HARDWARE.
"""

import inspect

# Command burst lengths, in CLK cycles of LAT-high.
V_SYNC = 3
PRE_ACT_MID = 11     # the "middle" burst; not every chip wants it
PRE_ACT_END = 14
REG_LATCH = 5        # LAT is high for the last five clocks of a register slot
CLK_PAD = 8          # padding between commands, LAT low


class SpwmChip:
    """One driver chip's protocol data.

    `regs` is a list of (r, g, b) 16-bit words, high byte the register address
    and low byte its value, emitted one per frame in order and then repeated --
    the configuration is continuously re-asserted, which is why these panels
    recover from a glitch without a reset.

    `wrappers` are the non-rotating slots, in order, with the rotating register
    substituted at `rot_slot`.
    """

    def __init__(self, name, regs, wrappers=(), slots=5, rot_slot=2,
                 mid_latch=True, scan_reg=0x02, scan_mask=0x3F,
                 bright_reg=None, bright_max=0x7F, grey_bits=12,
                 capture_scan=None, row_pulse=2, row_pulse_first=2,
                 data_latch=0, row_address=0, row_addr_shift=0, row_serial=0, chan_rev=0,
                 chan_xor=0, sweep_adv=0, pwm_range_from_regs=False,
                 row_slot=None,
                 source="", note=""):
        self.name = name
        self.regs = [tuple(x) if isinstance(x, (tuple, list)) else (x, x, x)
                     for x in regs]
        self.wrappers = [tuple(x) if isinstance(x, (tuple, list)) else (x, x, x)
                         for x in wrappers]
        self.slots = slots
        self.rot_slot = rot_slot
        self.mid_latch = mid_latch
        self.scan_reg = scan_reg
        self.scan_mask = scan_mask
        self.bright_reg = bright_reg
        self.bright_max = bright_max
        self.grey_bits = grey_bits
        self.capture_scan = capture_scan
        # Width of the ROW/OE pulse that advances the chip's scan counter, in
        # panel clocks, and the width used for the FIRST row of a frame. They
        # are the same on parts that do not distinguish the two (the default,
        # which preserves the ICND1065 behaviour this stage shipped with).
        self.row_pulse = row_pulse
        self.row_pulse_first = row_pulse_first
        # Clocks of LAT at the end of each row of pixel data. 0 means this
        # stage's original behaviour: no latch during the data phase at all.
        self.data_latch = data_latch
        # Does the MODULE decode a row ADDRESS on A-E, or does the driver
        # count rows internally off the ROW/OE pulse? An ICND1065 self-counts
        # and the address pins are dead; a module with a 4051-style decoder
        # (HX6158, DM5953, VOD5958 and friends) needs the row number on the
        # wire or nothing is ever switched on.
        self.row_address = row_address
        # How many low bits of the scan row to DROP before driving A/B/C.
        # A module whose decoder picks a coarse row GROUP, leaving the driver's
        # own counter to walk the rows inside it, wants the high bits.
        self.row_addr_shift = row_addr_shift
        # Or the row driver is not a decoder at ALL but a cascaded SHIFT
        # REGISTER -- the RT5958D/DM5953/5958/SM5266 family, whose datasheet
        # advertises "wipe off 138 decoder". Then A/B/C are DIN, LCK and BK in
        # some order, a row is selected by walking a single 1 along the chain,
        # and an address driven onto those pins is noise. See docs/HUB320.md.
        self.row_serial = row_serial
        # 11.2: this family sends a line channel 15 -> 0.
        self.chan_rev = chan_rev
        self.chan_xor = chan_xor
        self.sweep_adv = sweep_adv
        # THE DISPLAYABLE PWM RANGE IS SET BY THE CHIP'S OWN REGISTERS, and on
        # the DP32xx family the datasheet gives it outright (11.7.5):
        #
        #     levels = 4 * (reg0x04[6:0] + 1) * (reg0x03[6:0] + 1)
        #
        # Everything above `levels - 1` clips to full. That makes the grey
        # depth a property of THIS TABLE, not of the output stage, and getting
        # it wrong is invisible on pure primaries while it washes out every
        # mixed colour (2026-10-19: reg0x03 = 0 gave 128 levels, so `brass`
        # clipped all three channels to full -- see E320-BRINGUP 4.10).
        #
        # `display_passes` is the same reg0x03 term read the other way: how
        # many times the chip re-displays its SRAM per data frame, which sets
        # the W12 cadence the row scanner has to match (11.6.2).
        #
        # Opt-in, because the register NUMBERS mean different things on other
        # families: an ICND1065 is not a DP3264 and 11.7.5 does not apply to
        # it. Only the DP parts set this.
        # PANEL CLOCKS PER ROW SLOT. The row slot must be long enough for the
        # chip to emit the row's own GCLKs -- 4 * (reg0x04 + 1) of them at
        # FGCLK = FDCLK * (reg0x06[2:0] + 1), per 11.7.2 and 11.7.4 -- and
        # short enough that `display_passes` of them still complete a grey
        # cycle above flicker. Those two pull in opposite directions and the
        # answer is a property of the CHIP's registers, not a constant:
        #
        #   dp3264s, clk_div 2:  DCLK 17.5 MHz, GCLK 52.5 MHz, a row needs
        #                        42.7 clocks; 64 gives 273 kHz of ROW, 4272 Hz
        #                        optical and a 66.8 Hz grey cycle -- which is
        #                        the vendor profile's 3840 Hz / 60 Hz almost
        #                        exactly (5c).
        #
        # None keeps icn1065.ROW_OE_LEN, the value this stage shipped with.
        self.row_slot = row_slot
        self.pwm_range_from_regs = pwm_range_from_regs
        if pwm_range_from_regs:
            r03, r04 = self.reg_r(0x03), self.reg_r(0x04)
            assert r03 is not None and r04 is not None, (
                f"{name}: pwm_range_from_regs needs both 0x03 and 0x04")
            self.display_passes = (r03 & 0x7F) + 1
            self.pwm_levels = 4 * ((r04 & 0x7F) + 1) * self.display_passes
            self.grey_bits = (self.pwm_levels - 1).bit_length()
            assert 8 <= self.grey_bits <= 14, (
                f"{name}: {self.pwm_levels} PWM levels is {self.grey_bits} "
                f"bits; the part carries 14 at most and this is almost "
                f"certainly a wrong 0x03/0x04")
        else:
            self.display_passes = 1
            self.pwm_levels = 1 << self.grey_bits
        self.source = source
        self.note = note

    def reg_r(self, addr):
        """The RED value this table writes to `addr`, or None.

        The three colours may differ (elimination level, coupled optimisation);
        the fields this is used for -- 0x03 and 0x04 -- are the same on all
        three, and red is the representative.
        """
        for r, _g, _b in self.regs:
            if (r >> 8) == addr:
                return r & 0xFF
        return None

    def __repr__(self):
        return f"<SpwmChip {self.name}: {len(self.regs)} regs, {self.slots} slots>"

    def with_scan(self, rows):
        """A copy with the scan-count register patched for `rows` scan lines.

        Every profile in the catalogue puts (rows - 1) in the low bits of this
        register and leaves the upper bits as a chip constant, so the patch
        preserves whatever the table carries there.
        """
        out = []
        for r, g, b in self.regs:
            if (r >> 8) == self.scan_reg:
                keep = r & ~self.scan_mask & 0xFF
                w = (self.scan_reg << 8) | keep | ((rows - 1) & self.scan_mask)
                out.append((w, w, w))
            else:
                out.append((r, g, b))
        # Copy EVERY __init__ parameter off `self`, overriding only `regs`.
        #
        # This was a hand-written list of keywords, and the list fell behind
        # __init__ TWICE. First it passed positionally, and adding two
        # parameters slid `source` into one of them -- a string where the
        # gateware wanted a Signal reset value. Then it was rewritten with
        # keywords, and `row_serial` and `chan_rev` were added to __init__
        # later and never added here, so every call silently reset them to 0.
        #
        # That second one matters: `icn1065_soc.py` calls with_scan() on EVERY
        # build and the result seeds the CSR reset values, so a table saying
        # `row_serial=1` produced a bitstream whose power-on state drives A/B/C
        # as a row ADDRESS -- while `row_address` survived the copy -- on a
        # module whose row drivers are a serial chain. A walking 1 clocked to
        # arbitrary positions is the "rain" already recorded in this repo.
        #
        # So stop enumerating. Reflection over the signature cannot fall behind
        # it, and the assertion below fails loudly at import time if a
        # parameter is ever not stored under its own name.
        params = [n for n in inspect.signature(SpwmChip.__init__).parameters
                  if n != "self"]
        missing = [n for n in params if not hasattr(self, n)]
        assert not missing, (
            f"SpwmChip.__init__ takes {missing} but does not store them under "
            f"those names, so with_scan() cannot copy them")
        kwargs = {n: getattr(self, n) for n in params}
        kwargs["regs"] = out
        return SpwmChip(**kwargs)

    def words(self):
        """(r, g, b) words in emission order: wrappers with the registers rotated in.

        The gateware stores the rotating table and the wrappers in one memory;
        this is the host-side twin used by the tests and by the firmware
        generator.
        """
        return {"regs": self.regs, "wrappers": self.wrappers}


# --- ICND1065L / ICND1065S -------------------------------------------------
#
# Chipone. 16 channels, 1-64 scan, 3840 Hz, 32 KB of on-chip SRAM -- which is
# why the panel holds the frame and only needs new data when the content
# changes. Transcribed from the nimakolahi ESP32 driver, which states
# compatibility with the L and S variants and explicitly NOT with ICND1065(AP),
# whose registers are numbered differently.
#
# NOTE: this table carries one value for all three colour lines, because that
# is what the ESP32 reference sends. Falcon Player's ICND1065L profiles (from
# kingdo9/rpi-rgb-led-matrix_pwm_experiment) carry DIFFERENT values per colour,
# so one of the two is wrong or they are different panel builds. Kept as
# transcribed, flagged here, and first to check if colour balance is off.
_ICND1065_REGS = [
    0x00aa, 0x01aa, 0x022a, 0x0335, 0x0412, 0x0500, 0x0601, 0x0720,
    0x0c18, 0x0d01, 0x0e86, 0x0f01, 0x1040, 0x1127, 0x1200, 0x1300,
    0x1400, 0x1500, 0x1600, 0x1800, 0x1906, 0x1c60, 0x1dca, 0x1e73,
    0x1f00, 0x2000, 0x2100, 0x2200, 0x2300, 0x2400, 0x2500, 0x2600,
    0x2700, 0x7000, 0x7100, 0x7200, 0x7300, 0x74a0,
]

ICND1065 = SpwmChip(
    name="icnd1065",
    regs=_ICND1065_REGS,
    wrappers=(0x00AA, 0x01AA, 0x0055, 0x0155),
    slots=5, rot_slot=2, mid_latch=True,
    scan_reg=0x02, scan_mask=0x3F,
    capture_scan=43,
    source="nimakolahi/hub75-icn1065 (ESP32), ICND1065_REG_VALUE",
    note="drives ICND1065L and ICND1065S. NOT ICND1065(AP).",
)

# --- DP3364S ---------------------------------------------------------------
#
# NovaStar. Captured from a working 128x64 1/64-scan panel by Falcon Player.
# The datasheet says 15 valid register addresses and that 15 frames complete a
# full refresh, which is exactly 0x02-0x0F plus 0x15; 0x10-0x14 read back zero
# and are taken as reserved.
#
# Documented meanings, for the ones that are documented:
#   0x03[6:0] PWM display packet count - 1 (0x3f = 64, the async mode default)
#   0x04[6:0] row PWM display length - 1
#   0x06[2:0] internal GCLK multiplier, FGCLK = FDCLK * (n + 1)
#   0x08[7:0] linear output current multiplier -- the brightness knob. The
#             captured 0x7f is what the panel was built for, so that is full
#             brightness and anything else only scales DOWN from it.
#   0x0b[5]   1.5x current gain
#   0x0c[7:6] PWM display mode (01 = high gray data independent refresh, the
#             free-running row scan this design generates); [1] drop open circuit
#   0x0f[6:0] current reference
# 0x02 is undocumented but holds 0x3f on a 64-row panel and carries the scan
# count, the same as 0x02 on the FM6373.
#
# The upload grammar differs from the ICND1065: VSYNC (3) then PRE_ACT (14)
# with NO middle burst, and a SINGLE slot rather than five.
DP3364S = SpwmChip(
    name="dp3364s",
    regs=[
        (0x023f, 0x023f, 0x023f), (0x033f, 0x033f, 0x033f),
        (0x041a, 0x041a, 0x041a), (0x0504, 0x0504, 0x0500),
        (0x0639, 0x0639, 0x0639), (0x0700, 0x070c, 0x070c),
        (0x087f, 0x087f, 0x087f), (0x0968, 0x096b, 0x0961),
        (0x0abe, 0x0abf, 0x0abe), (0x0b28, 0x0b2b, 0x0b31),
        (0x0c58, 0x0c58, 0x0c58), (0x0d08, 0x0d12, 0x0d18),
        (0x0e08, 0x0e0b, 0x0e01), (0x0f20, 0x0f20, 0x0f20),
        (0x1500, 0x1504, 0x1504),
    ],
    wrappers=(),
    slots=1, rot_slot=0, mid_latch=False,
    scan_reg=0x02, scan_mask=0x3F,
    bright_reg=0x08, bright_max=0x7F,
    capture_scan=64,
    source="FalconChristmas/fpp BBShiftPanel.cpp, from hzeller issue #1821",
    note="datasheet documents the access protocol but not the register map.",
)


# --- DP3264S ---------------------------------------------------------------
#
# Depuw. The part on the P1.25 320x160 mm modules bought for the wall, paired
# with an HX6158HSP row driver. NOT the same part as the DP3364S above, and the
# difference is not only the register table -- see `row_pulse` below.
#
# Registers are `dp3264_regtype9` from board707/DMD_STM32's profile catalogue,
# labelled "P1.25 - DP3265 - 5368 - 1/64", 12-bit grey, which is the closest
# published profile to this module: same pitch, same scan, same grey depth as
# this stage emits. The catalogue's 13 addresses are 0x02-0x0D plus 0x11, which
# matches the datasheet's "13 frames are required to complete the refresh of
# all registers". The DP3364S table's 0x0E, 0x0F and 0x15 are Reserved here.
#
# ONE DELIBERATE DEPARTURE from the profile: register 0x08 is 0x7F, not the
# profile's 0xBF. 0x08 is IGAIN, and the datasheet gives
# reg08[5:0] = round(128*IGAIN/(reg08[7:6]+1))-1 with reg08[7:6] a 50% gain
# step -- so 0xBF is 150% of nominal drive current and 0x7F is 100%, which is
# also the datasheet default. A first light test is not the moment to run LEDs
# half again over nominal. Raise it later, deliberately, if the panel is dim.
#
# Documented register meanings used here:
#   0x02[5:0] LINE_SET, scan lines = LINE_SET + 1 (0x3f = 64)
#   0x03[6:0] GROUP_SET, display refresh rate = REG0F[7]*128 + REG03[6:0] + 1
#   0x04[6:0] PWM_WIDTH, row PWM width = 4*(PWM_WIDTH + 1)
#   0x06[2:0] PLL_DIV, fGCLK = fDCLK*(PLL_DIV + 1)
#   0x08[7:0] IGAIN, as above
#   0x0c[7:6] SYNC_MODE = 2, high gray independent refresh SYNCHRONOUS.
#             I argued my way to mode 0 from the datasheet and was wrong: the
#             vendor's working profile for this panel class reports
#             highGrayIndependentRefresh sync with Enable set, which is 0x9C.
#             Datasheet 11.6.2 then requires ROW to free-run independent of
#             VSYNC with ONE W12 per (groups x rows) pulses -- 64 x 64 here --
#             which this frame-gated scanner does NOT yet do. That is the known
#             remaining gap, not a thing to guess at again.
#   0x03[6:0] GROUP_SET 63, i.e. dataGroup 64. I had set this to 0 (one group)
#             to match our one-W12-per-frame cadence. The real panels use 64.
#   0x08      IGAIN 0xBF. The datasheet formula, and the app's own display
#             formula ((v&0x3f)+1)*((v>>6)+1)*100/128, both make this 150% of
#             nominal -- and it is what Colorlight ships for this panel class,
#             with brightness trimmed elsewhere (brightPercent 61). An earlier
#             version of this table used 0x7F out of caution; that was caution
#             against the manufacturer's own design point.
#
# Grammar, confirmed by the same decode via getChipControlSettings:
#   globalLatching 3 = VSYNC, prefixCommand 14 = PRE_ACT,
#   writeRegister1 5 = WR_CFG, dataLatching 1 = DATA_LATCH,
#   registerNumber 1 = one register per frame (slots=1).
# And moduleInfo.width 16 states the chain structure outright: a module is
# sixteen pixels wide, which is one chip.
DP3264S = SpwmChip(
    name="dp3264s",
    regs=[
        # THE FACTORY CONFIGURATION FOR THIS EXACT MODULE, 2026-10-18.
        #
        # The vendor sent `CLTp1.25-320-160-3264s-7559-64s-6.rcfgx.rcvbp`
        # (kept in factory/, decode it with tools/decode_rcvbp.py). It is a
        # 32-byte header over a zlib stream, and at offset 198321 of the
        # inflated blob sits the driver register table as thirteen 4-byte
        # (addr, r, g, b) records -- 0x02-0x0D then 0x11, exactly the address
        # list below.
        #
        # SEVEN OF THE THIRTEEN DISAGREED with what this table used to hold,
        # because the old values came from a DIFFERENT profile:
        # "CD-P1.2-256X128-DP3264+32020-64S--9.51.rcvbp", decoded through
        # LEDSettingServer's getChipRegisterInfo_DP3256. Same pitch class, same
        # 256x128, same 1/64 scan -- but not this module, and the differences
        # are not cosmetic: PWM width, PLL divider, elimination level, the
        # coupled-optimisation levels and one bit of SYNC_MODE.
        #
        # Each line that was changed says FACTORY and gives the old value. The
        # one place we still deliberately diverge is 0x03, below, and it is
        # flagged as an open question rather than a settled choice.
        #
        # Also measured from the same file, and worth knowing before anyone
        # goes looking for a remap table to copy: the factory's per-pixel
        # coordinate map for this module is the IDENTITY over 128x64 -- all
        # 8192 entries, once the records are framed as (y, x_lo, x_hi); framed
        # one byte off they fake a "carry" at column 127, which was briefly
        # mistaken for corroboration of sweep_adv. It corroborates nothing.
        # The factory does NO pixel remapping at the receiver. So any
        # scrambling we see comes from the data ORDER we emit and the chip's
        # channel semantics -- not from a lookup table we are failing to apply.
        # Its data-group map is [8, 9, 0, 1, 10, 11, 2, 3] in 16 slots.
        (0x023f, 0x023f, 0x023f),   # LINE_SET 63 -> 64 scan lines
        # 0x03 -- THE FACTORY VALUE, and it is not optional.
        #
        # This field is read two ways and BOTH matter:
        #
        #   11.6.2  the chip re-displays its SRAM (reg0x03 + 1) times per data
        #           frame, and expects exactly one W12 ROW pulse per
        #           (passes x scan) ROW signals
        #   11.7.5  the DISPLAYABLE PWM range is
        #               4 * (reg0x04 + 1) * (reg0x03 + 1) - 1
        #
        # From 2026-10-16 to 2026-10-19 this table wrote 0x00 -- one display
        # pass -- so that the scanner, which emitted W12 on row 0 of every
        # 64-row pass, was in step with the chip. It was, and the cost was
        # invisible until someone looked at a MIXED colour: one pass gives
        # 4 * 32 * 1 = 128 grey levels, so every value above about code 56
        # clipped to full. Pure primaries and white were unaffected (a clipped
        # channel that should be full IS full, and a zero stays zero), which is
        # why the grid test screen, solid red and solid green all passed while
        # `brass` read as washed-out near-white beside the HUB75 module's gold.
        # See docs/E320-BRINGUP.md 4.10.
        #
        # The factory's 0x3f gives 4 * 32 * 64 = 8192 levels -- the vendor
        # profile's "grayScale: 8192" exactly -- and `grey_bits` is now DERIVED
        # from these two registers (pwm_range_from_regs below) rather than
        # being a constant that can silently disagree with them.
        #
        # The cadence that made 0x00 attractive is handled where it belongs:
        # `Icn1065RowScanner` counts passes and asserts the wide pulse on row 0
        # of every `display_passes`-th pass. That count is also a runtime CSR
        # (`latch_cfg.passes`), because each value otherwise costs a power
        # cycle to try.
        (0x033f, 0x033f, 0x033f),   # GROUP_SET 63 -> 64 display passes (FACTORY)
        # FACTORY: 0x1f (31), not 0x13 (19). PWM_WIDTH / GCLK per row.
        (0x041f, 0x041f, 0x041f),   # PWM_WIDTH 31
        (0x0505, 0x0504, 0x0504),   # eliminationTime 5/4/4
        # FACTORY: 0x42, not 0x41.
        (0x0642, 0x0642, 0x0642),   # PLL_DIV
        (0x0700, 0x0700, 0x0700),   # gamma fine/coarse off
        (0x08bf, 0x08bf, 0x08bf),   # IGAIN 0xBF. See the note below.
        (0x0960, 0x0960, 0x0960),   # DECOUP_1, the datasheet default
        # FACTORY: 0xbe, not 0xfe.
        (0x0abe, 0x0abe, 0x0abe),
        # FACTORY: R is 0x28, not 0x29; G and B agree.
        (0x0b28, 0x0b2b, 0x0b31),   # eliminationLevel
        # SYNC_MODE -- and note the heading below USED TO SAY "mode 0" while
        # the emitted word was mode 2. The prose argued for a value it did
        # not set, and that cost a session: it is why mode 0 was tried on
        # glass at all. The vendor configuration settles it as mode 2.
    #
    # 11.6 gives three display modes in reg0x0C[7:6]:
    #   0  general frame sync -- "after the current frame of data is
    #      displayed, STOP DISPLAYING until the next VSYNC arrives"
    #   2  high-grey independent refresh, sync   <- was 0x9C
    #   3  the same, asynchronous
    #
    # Mode 2 refreshes continuously from SRAM and nothing swaps buffers, so
    # anything written lands in the buffer being read: the image tears and
    # blinks at the data frame rate. 11.6.2 note 1 is explicit that VSYNC and
    # ROW are asynchronous there, so VSYNC is NOT a buffer swap in that mode --
    # which is why "synchronise the writes" was never going to work.
    #
    # Mode 0 displays a whole frame then stops until the next VSYNC, so a frame
    # is coherent. The cost is blanking between frames: dimmer, and a blink at
    # whatever our data rate is. Raise the panel clock until that is above
    # flicker fusion (a 256x128 frame at 17.5 MHz is ~7 ms, so ~140 Hz).
    # FACTORY: 0x8c, not 0x9c -- bit 4 differs. Bits [7:6] are 0b10 in BOTH,
    # so MODE 2 IS CONFIRMED CORRECT for this module by the vendor's own
    # configuration. That settles a question this repo had argued both ways.
    (0x0c8c, 0x0c8c, 0x0c8c),   # SYNC_MODE 2: high-grey independent, SYNC
    #
    # Mode 0 (0x1C, general frame sync) was tried on 2026-10-17 and gives a
    # DARK PANEL with the write-once policy, for a reason 11.6.1 states
    # plainly: "after the current frame of data is displayed, stop displaying
    # until the next VSYNC arrives". Write-once stops sending frames, so no
    # further VSYNC ever arrives and the chip halts after one.
    #
    # The two are mutually exclusive:
    #
    #   mode 2 + write once      works: the chip self-refreshes from SRAM
    #   mode 2 + write always    tears: we rewrite the buffer it is reading
    #   mode 0 + write once      DARK: no VSYNC, display stops
    #   mode 0 + write always    coherent frames, blinks at the frame rate
    #
    # So mode 2 is right for static content and mode 0 only earns its place
    # for STREAMED content, where frames are being sent continuously anyway.
    # If streaming is added, make the mode follow the content rather than
    # baking one in here.
        # FACTORY: 0x08/0x12/0x18, not 0x0b/0x08/0x17.
        (0x0d08, 0x0d12, 0x0d18),   # coupledOptimizationLevel
        (0x1100, 0x1100, 0x1100),
    ],
    wrappers=(),
    slots=1, rot_slot=0, mid_latch=False,
    scan_reg=0x02, scan_mask=0x3F,
    bright_reg=0x08, bright_max=0xBF,
    capture_scan=64,
    # Datasheet 11.5: the on-chip GCLK generator turns what is OE on an
    # ordinary constant-current part into a ROW signal, whose RISING EDGE
    # starts a row and whose HIGH WIDTH is meaningful -- W12 (12 DCLK) for the
    # first row of group 0, W4 (4 DCLK) for every other row. A uniform 2-clock
    # pulse, which is what this stage emitted before, starts no row at all and
    # the panel stays dark.
    row_pulse=4, row_pulse_first=12,
    # DATA_LATCH is LE width 1 on this part.
    data_latch=1,
    # The P1.25 module pairs this driver with an HX6158HSP ROW DECODER, and
    # Colorlight's own profiles for this panel class report bEnable4051 = 1 on
    # every one. So A-E carry a row address and must be driven. The stage held
    # them at 0 for the ICND1065, which self-counts -- which selected row 0
    # forever and is why a correct frame still lit nothing.
    # NO column pair swap. chan_xor=1 was carried here from 2026-10-18 to
    # 2026-10-19 on the strength of three 1-pixel features (both borders and
    # the 2:1 diagonal's sawtooth) that `x ^ 1` on the source column appeared
    # to cancel -- measured through the TEST-PATTERN path while that path also
    # carried a per-half serialiser rotation (15/17 columns) and the stacked
    # 128x256 geometry. Both of those have since been removed at source, and
    # the "swap" went with them: on gateware 1.18.0, with the framebuffer the
    # glass and nothing remapped in software, chan_xor=1 put the red border
    # at column 1, the magenta border at column 254 and garbled the 5x7
    # version text; chan_xor=0 put every one of them flush (glass,
    # 2026-10-19). A fitted compensation for a fault that no longer existed.
    # The CSR stays, because it is the right instrument for testing exactly
    # this question on the next module.
    chan_xor=0,
    # The fetch address runs one sweep ahead of the latch cadence.
    #
    # DERIVED, not fitted, and the derivation is checkable:
    #
    #  * the dots sit at physical columns 0, 16, 32 ... -- one per chip
    #  * physical column 16s+0 is fed by CHANNEL 15 (the module's wiring
    #    reverses the sixteen channels; that is why chan_rev=1 mirrors the
    #    image instead of correcting it, measured on glass 2026-10-18)
    #  * channel 15 is the FIRST position written in each line (11.2: the
    #    data order is channel 15 down to channel 0)
    #  * so line r's first-written position receives our latch r*16 - 1,
    #    which is the PREVIOUS row's last sweep -- the adjacent row, which is
    #    what the glass shows
    #  * therefore the chip's write pointer runs ONE AHEAD of our latch index
    #
    # And that is why `latch_phase` did nothing on hardware: an extra
    # DATA_LATCH advances the chip's pointer AND delays our data by one, so
    # the RELATIVE phase is unchanged. latch_phase is structurally incapable
    # of correcting a pointer offset. `sweep_adv` moves the data without
    # moving the pointer, which is the only lever that can.
    #
    # CONFIRMED ON GLASS 2026-10-19, and the cause pinned by elimination: an
    # extra DATA_LATCH, an extra WR_CFG (slots=2) and an extra PRE_ACT
    # (mid_latch=1) per frame each changed nothing, so nothing in the prefix
    # causes the offset. The chip commits each DATA_LATCH at pointer+1 --
    # increment, then write -- as a property of the part. This value is
    # therefore NOT a compensation for anything the gateware does: it is the
    # correct alignment to how the chip writes, the same kind of fact as
    # channel 15 going first. Position 0 of a frame is written by the LAST
    # latch via wrap, and this is what makes that wrap carry row 0.
    # Grey depth and W12 cadence come from 0x03/0x04 above (11.7.5, 11.6.2),
    # not from a constant: 4 * 32 * 64 = 8192 levels, 13 bits, 64 display passes.
    pwm_range_from_regs=True,
    # 64 panel clocks per row: long enough for the chip's 128 GCLKs per row at
    # clk_div 2 (42.7 clocks), short enough that 64 display passes still turn
    # over at 66.8 Hz. See the note on `row_slot` above; the shipped 128 would
    # have given an 11 Hz grey cycle -- heavy flicker.
    row_slot=64,
    sweep_adv=1,
    row_address=1,
    # The module's own silkscreen gives only A, B and C on pins 17-19, with 20
    # and 21 marked NC: THREE address lines, eight addresses, against a panel
    # that scans 64 rows. So the decoder selects one row GROUP of eight and the
    # driver's internal counter walks the eight rows within it -- 8 x 8 = 64.
    # Hence the high bits: row >> 3.
    row_addr_shift=3,
    # The P1.25 module on the bench is XP1.25-5958S-...: its row drivers are
    # the 5958 serial chain, so A/B/C are DIN/LCK/BK and the address above is
    # not used. Kept only so a 4051-style module can still be driven by
    # clearing row_ser.enable at runtime.
    row_serial=1,
    # 0, DESPITE 11.2.
    #
    # The datasheet's data-order table sends each line channel 15 down to 0,
    # and this stage walks 0 -> 15, so the obvious reading is that we are
    # reversed. Tried on glass 2026-10-17 with the grid pattern: chan_rev=1
    # BREAKS the diagonals into zigzag fragments and chan_rev=0 leaves them
    # clean. So either the chain walk already produces the chip's order, or
    # this module's wiring compensates.
    #
    # RESOLVED 2026-10-19: it is the wiring. The either/or above could not be
    # settled from the glass alone, but the DP3264S_REV2.0 datasheet (11.2,
    # now in docs/datasheets/ -- the DP3364S document this repo had been
    # reading is a DIFFERENT PART) states the chip's order IS channel 15 down
    # to channel 0. So our ascending walk is NOT the chip's order, which
    # eliminates the first branch and leaves the second: the module reverses
    # the sixteen channels in its wiring, and chan_rev=0 is correct BECAUSE
    # two reversals cancel.
    #
    # That matters beyond this knob: it fixes which physical column channel
    # 15 drives, which is what gives `sweep_adv` its sign. See the derivation
    # above. The knob stays for the next module, which may differ.
    chan_rev=0,
    source="board707/DMD_STM32 register_test/profiles/dp3264 regtype9; "
           "DP3264S_REV2.0 datasheet sections 11.1-11.5 and 11.9",
    note="row pulse is W12/W4, unlike the ICND1065's uniform pulse.",
)


CHIPS = {c.name: c for c in (ICND1065, DP3364S, DP3264S)}
DEFAULT_CHIP = ICND1065

# The widest table any chip needs, which sizes the gateware's register memory.
# Sized with headroom so adding a chip is a table here and not a rebuild.
MAX_REGS = 64
MAX_WRAPPERS = 8
assert all(len(c.regs) <= MAX_REGS for c in CHIPS.values())
assert all(len(c.wrappers) <= MAX_WRAPPERS for c in CHIPS.values())

# chan_rev=0 on this module, FALSIFIED FOUR TIMES on glass.
#
# DP3364S/DP3264S 10.2 says a chip receives its channels 15 -> 0, and 10.5
# says the first DAT_LAT of a frame commits channel 15. That reads like
# chan_rev should be 1. It is not: every attempt produced sawtooth, including
# 2026-10-18 on a bitstream where the DATA_LATCH phase bug was already fixed
# and no firmware compensation was present -- the first genuinely fair test.
#
# So either the part does not follow the DP3364S text here, or the serialiser
# already emits that order. Do not try it a fifth time without new evidence.
