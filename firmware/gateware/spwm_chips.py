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
                 capture_scan=None, source="", note=""):
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
        self.source = source
        self.note = note

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
        c = SpwmChip(self.name, out, self.wrappers, self.slots, self.rot_slot,
                     self.mid_latch, self.scan_reg, self.scan_mask,
                     self.bright_reg, self.bright_max, self.grey_bits,
                     self.capture_scan, self.source, self.note)
        return c

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


CHIPS = {c.name: c for c in (ICND1065, DP3364S)}
DEFAULT_CHIP = ICND1065

# The widest table any chip needs, which sizes the gateware's register memory.
# Sized with headroom so adding a chip is a table here and not a rebuild.
MAX_REGS = 64
MAX_WRAPPERS = 8
assert all(len(c.regs) <= MAX_REGS for c in CHIPS.values())
assert all(len(c.wrappers) <= MAX_WRAPPERS for c in CHIPS.values())
