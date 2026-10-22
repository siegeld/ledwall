# Receiver cards — exactly what is supported

**Two cards. One of them is the only HUB320 card supported.**

This page is the authority on which *receiver cards* this repo builds for.
`PANELS.md` is the authority on which *LED modules* those cards drive; they
are separate questions and conflating them is how a wall gets ordered against
hardware that cannot drive it.

Counts here are read out of the platform definitions and the gateware, not
remembered. Three documents in this repo disagreed about them until
2026-10-21 — see "Getting the numbers right" at the bottom.

---

## Supported

### Colorlight 5A-75E — HUB75

| | |
|---|---|
| Build with | `--board 5a-75e` (the default) |
| Revisions | **6.0, 7.1, 8.2** — `--revision`, default `8.2` |
| FPGA | Lattice `LFE5U-25F`, CABGA256. 6.0 and 7.1 are `-6` parts, 8.2 is a `-7I` |
| Panel connectors | **16 × HUB75**, 16-pin (`j1`..`j16`) |
| RGB groups | **2 per connector** (`R1G1B1` / `R2G2B2`, 6 data lines) — **32 total** |
| System clock | 40 MHz |
| Status | **proven**, in daily use |

Revisions are **not** cosmetic: the pinouts differ and so does the speed
grade, so a bitstream built for the wrong one does nothing on the board.

### Colorlight E320 V6.2 — HUB320

> **This is the only HUB320 card supported.** If you need HUB320, this is the
> card. There is no second option, and no other Colorlight model has been
> tested against this stack.

| | |
|---|---|
| Build with | `--board e320` |
| Revision | **V6.2** only |
| FPGA | Lattice `LFE5U-25F`, CABGA256, `-6` speed grade |
| Panel connectors | **8 × HUB320**, 26-pin |
| RGB groups | **4 per connector** (12 data lines) — **32 total** |
| System clock | **35 MHz**, not 40 — build it with `--sys-clk 35000000` |
| Status | **proven**: drives Linsn P1.25 modules, two per card streaming video |

**It is electrically a 5A-75E.** Same die, same package, same flash
(`GD25Q32`), same SDRAM (`M12L64322A`), same JTAG pad designators — and it
runs the stock 5A-75E bitstream unchanged if you want plain HUB75 out of it.
The difference that matters is the panel side: **a HUB320 port is two 5A-75E
HUB75 connectors merged**, which is why 8 × 4 groups and 16 × 2 groups are the
same 96 data lines.

Two consequences worth knowing before building:

- **35 MHz, not 40.** It is a `-6` part and the design does not close 40 MHz
  on it. The build refuses a firmware whose clock constant disagrees with the
  gateware's, because a mismatch gives a card that boots and then fails at
  everything time-dependent — which presents as a network fault.
- **Outputs are limited by block RAM, not logic**, on the S-PWM path. Each
  output's scan-out line buffer costs 5 EBR. Measured: 1 output 47/56,
  2 outputs 52/56, **3 outputs 56/56 exactly**. Three is the ceiling, and at
  three the system clock margin is 5% and logic is 88% used. The HUB75 path
  has no line buffer and is not limited this way.

---

## Not supported

### The K-series: K5+, K8, K9+, K10

A **different product class**, not a card. They are 70 × 45 mm mezzanine
modules with **no RJ45 and no HUB75/HUB320 connectors** — everything leaves
through two 120-pin board-to-board headers into a cabinet's own HUB carrier
board. Even Ethernet exits as raw MDI pairs; the magnetics are on-module, the
jacks are not. Without the matching carrier there is nothing to plug in.

The **K9+ is also the wrong FPGA.** Its main chip is a 484-ball BGA, and ECP5
has no 484 package (256/381/554/756) — 484 is Xilinx FGG/FBG484, Artix-7
class. Corroborating: the vendor's own updater reserves a 2176 KiB slot for
the K9+ against 576–704 KiB for the 5A family, and 2176 KiB sits just above an
XC7A35T/50T bitstream. Its output stage is also far beyond this one: 32
parallel RGB groups, 1/128 scan, per-colour OE.

**If the question is "we need more FPGA", read [SCALING.md](SCALING.md)
first** -- it compares more cards, reclaiming block RAM in the current design,
a bigger ECP5, and the Xilinx route, against our own measured ceiling. The
short version is that more cards almost always wins and the K-series loses on
connectors before the FPGA is even relevant.

**The K5+ is the only K worth probing.** Same carrier-board problem, but its
FPGA is a 256-ball BGA — the same package class as our `LFE5U-25F-BG256`. If
any K-series part is an ECP5, it is that one. Unproven: the marking is under a
vendor label.

**The cheap experiment, if one ever lands on a bench:** four plated pads carry
the same refdes set as our JTAG pads (`J27 J30 J31 J32`). `openFPGALoader
--detect` on those settles the FPGA family in one command. Do that before
anything else.

---

## Getting the numbers right

Until 2026-10-21 three documents in this repo disagreed about the 5A-75E's
connectors: `HARDWARE.md` said 8 ports, the private README said "8 ports, 2
RGB groups each", and the public README said "16 ports, one RGB group each".

The source settles it: the platform defines **`j1`..`j16`** at 16 pins each,
and `gateware/icn1065.py` sets `GROUPS_HUB75 = 2` with the comment
"`R1G1B1 / R2G2B2` — 6 data lines". So **16 connectors × 2 groups = 32**,
which is the same 32 the E320 reaches as 8 × 4 — as it must be, since the E320
is the same silicon with the same 96 data lines rearranged.

If you quote a count from this page, it came from:

    litex_boards/platforms/colorlight_5a_75e.py   connector list
    gateware/icn1065.py                           GROUPS_HUB75
    gateware/colorlight_e320.py                   the eight HUB320 ports
