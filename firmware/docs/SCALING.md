# Running out of card — what the options actually are

**You are here because a build hit 56/56 block RAM, or because somebody asked
whether a bigger FPGA is the answer.**

Short version: **more cards is almost always the right answer**, a bigger ECP5
is the only port worth considering, and the Xilinx route costs far more than
it returns. The numbers below are measured on our own builds, not quoted from
a datasheet, except where marked as vendor figures.

---

## 1. Where the ceiling actually is

Measured on the `LFE5U-25F` (CABGA256) in an E320, S-PWM path, 35 MHz:

| outputs | block RAM | logic (LUT4) | system clock |
|---|---|---|---|
| 1 | 47 / 56 | — | — |
| 2 | 52 / 56 | 19,844 / 24,288 | 39.59 MHz |
| 3 | **56 / 56 (100%)** | **21,581 / 24,288 (88%)** | 36.76 MHz |

Flip-flops are at 37% and multipliers at 14%, so **neither of those is the
constraint** — do not go shopping for them.

Each added output costs **+4 EBR and ~1,737 LUT4** (measured 2 -> 3). A
**fourth output would need 60 EBR against 56**, so it does not fit, and the
logic would be at 96%. Three is the ceiling on this part, and at three the
system clock margin is down to 5% (36.76 against 35) and the gigabit RX to 4%.

The cost is the scan-out **line buffer**, 5 EBR per output
(`Icn1065LineBuffer`, gateware 1.29.0). That buffer is not optional — it is
what removed the dependency of picture correctness on memory latency. The
HUB75 path has no line buffer and is not limited this way.

---

## 2. The options, cheapest first

### A. More cards — no porting at all

A wall is already multi-card. Nine P1.25 modules is **three E320s at three
outputs each**, which is proven hardware doing proven work. The cards are
cheap, the gateware is unchanged, and marquee renders one canvas and crops a
region per card, so the wall stays coherent across them.

**This is the default.** Every option below costs weeks and this costs an
afternoon of cabling. Take it unless there is a reason you cannot — physical
space in the cabinet, or a single-card constraint somebody else imposed.

### B. Free resources inside the current design

Before buying silicon, there is reclaimable space on the card you have:

| what | cost | notes |
|---|---|---|
| `burst_fifo` | ~4-10 EBR depending on depth | already stepped down as outputs rise (4096 -> 2048 -> 1536) |
| the palette | 1 EBR | 32x512; only indexed mode uses it |
| VexRiscv caches | some | the CPU is not in the pixel path any more |

Reclaiming 4 EBR buys the fourth output. That is days of work against months
for a port, and it keeps the toolchain, the flashing path and every document
in this repo valid.

### C. A bigger ECP5 — the only port worth considering

`wuxx/Colorlight-FPGA-Projects` documents the **Colorlight i9 v7.x** as an
**`LFE5U-45F-6BG381C`**, with the **same `M12L64322A` SDRAM** as our cards,
dual Broadcom B50612D gigabit PHYs, and a **DDR2 SODIMM-200 slot**.

Vendor figures:

| | `LFE5U-25F` (ours) | `LFE5U-45F` (i9) |
|---|---|---|
| logic | 24k LUT4 | 44k (**1.8x**) |
| block RAM | 56 EBR / 1,008 Kb | 108 / 1,944 Kb (**1.9x**) |

**The 45F is the largest ECP5 on any Colorlight board.** There is an
`LFE5U-85F` (84k logic, 208 EBR, roughly 3.5x and 3.7x ours) and it appears on
third-party boards such as the OrangeCrab and ULX3S -- but **not on an LED
receiver card**. Reaching it means a board with no panel connectors and no
Colorlight lineage at all, which is a different project from porting between
two Colorlight cards. Do not plan around it.

**Why this is the sane port:** same family, so **the toolchain does not
change** -- Yosys, Project Trellis, nextpnr-ecp5, ecppack, and the same
openFPGALoader flashing path. The SoC, the output stages, the firmware and
every document here carry over. At 1.9x block RAM, the output count roughly
doubles.

**What it still costs:**

- **A new platform file.** BG381, not BG256 — a different pinout entirely.
  But **the pin numbers are already known**, which is the part that normally
  costs weeks: see below.
- **A carrier board.** The i9 has no HUB75 or HUB320 footprint. Its panel
  signals all leave on one **DDR2-200 SODIMM gold-finger edge (`J28`)** for a
  cabinet HUB board to fan out. That board is a passive two-layer design —
  no logic, no level shifting — or a commercial HUB75E-8 adapter if one is
  confirmed to take that edge. It is a real cost, but it is board fabrication,
  not reverse engineering.

**The pinout is NOT an open problem — corrected 2026-10-22.** An earlier
version of this document said the i9 "has no panel-connector section" and that
pin discovery would be needed. That was wrong, and wrong in a way that made
this option look far more expensive than it is.

Colorlight's own `i9 Specification V1.4.1` names all 200 pins of `J28`
(`LED_R1..R32`, `G`, `B`, `FA..FE`, `LED_SCLK`, `LED_LAT`, `LED_OE`, eight SPI
config channels, monitoring). `wuxx` has **already traced those same 200 pins
to FPGA balls** — he just filed it under the heading **"DDR2-SODIMM-200P"**,
because that is the footprint, so nobody recognised it as the panel connector.
Cross-referencing the two gives a complete ball-level HUB pinout with no
probing at all; the two documents agree on which pins are live, and every
disagreement runs one way (vendor names a signal, wuxx records `NC`).

**`docs/I9-PANEL-PINOUT.md` has the derived table and the one check still
owed** — whether the eight SPI driver-config channels (pins 159-176) reach the
FPGA, which our S-PWM panel would need. `PINWALK.md` and
`chmousset/colorlight_reverse`'s JTAG boundary-scan `PinoutFinder` remain the
method if a board ever does need mapping from scratch.

### D. Xilinx Artix-7 — not worth it

The **i9plus-v6.1 is `XC7A50T-FGG484`**, and the **K9+** is a 484-ball BGA,
which ECP5 has no package for (256/381/554/756). Vendor figures put an
XC7A50T at roughly 2.1x our logic and 2.7x our block RAM.

**That is genuinely more than the biggest Colorlight ECP5** -- the i9's 45F is
1.8x and 1.9x -- so be honest about it: on raw resources the Xilinx route
wins. The case against it is not the silicon, it is everything attached to
the silicon:

- **Vivado**, not Trellis. ~100 GB, a different flow, a new Docker image, and
  `build.sh` rewritten end to end. LiteX ports the SoC; the build plumbing
  does not come with it.
- **The K-series has no connectors at all** — 70 x 45 mm mezzanine modules,
  no RJ45 and no panel headers, everything through two 120-pin board-to-board
  connectors into a cabinet carrier you would have to obtain or build. Even
  Ethernet leaves as raw MDI pairs.
- The K9+'s output stage is built for 32 parallel RGB groups at 1/128 scan.
  Ours emits 2 (HUB75) or 4 (HUB320). You would be paying for silicon sized
  for a job you are not doing.
- **And the i9+'s pinout is genuinely unknown, where the i9's is not.** This
  is the asymmetry that decides it. Colorlight publishes signal names for the
  i9+ and the K9+ as well — the K9+'s `JH1`/`JH2` carry `R1..R32`, `FA..FE`,
  `DCLK`, `FLAT`, `FCTRL`, per-colour `OE_*` — but **nobody has traced those
  pins to FPGA balls**, and there is no community work on any of these boards
  to check against. For the i9, half that work was already done and filed
  under a misleading heading (see above). The Xilinx boards have no such gift
  waiting.

So the comparison that matters is not 2.7x against 1.9x. It is: the i9 needs a
passive carrier board fabricated against a pinout we can already write down,
while the i9+ or a K-series needs that *plus* 200 pins reverse-engineered
*plus* the entire toolchain replaced. That is what makes the ECP5 the sane
port — not a resource advantage it does not have.

**The one cheap experiment**, if such a board ever lands on a bench: four
plated pads carry the same refdes set as our JTAG pads — `J27 J30 J31 J32`,
the same designators on the 5A-75E, the E320 and the i9. `openFPGALoader
--detect` on those settles the FPGA family in one command. Do that before
anything else.

---

## 3. What a bigger FPGA does NOT fix

**Our clock ceiling is the design, not the silicon.** We close at 36.76 MHz on
a part comfortably capable of several times that. A faster or larger FPGA does
not shorten a long combinational path; it moves the same bottleneck slightly.
If the goal is a faster panel clock rather than more outputs, the work is
pipelining — the stride multiplier is the known offender — not a new board.

**Flip-flops and DSP are not short.** 37% and 14%. Any comparison that leads
with those numbers is measuring the wrong thing.

**The host is a separate budget.** At three outputs a wall is 202 chunks a
frame and 4,040 packets/s, which caps packet spacing at 248 us. That is a
sender problem and no FPGA changes it.

---

## 4. If you do port

In rough order, and each one gates the next:

1. **Confirm the FPGA** — `openFPGALoader --detect` on `J27/J30/J31/J32`.
2. **Confirm the panels can physically connect.** This is the one that kills
   most candidates, and it has nothing to do with the FPGA.
3. **Platform file** — pinout, either from a vendor document or by probing.
4. **Build target** — a new `--board`, and the speed grade and clock constant
   agreeing with the firmware, or you get a card that boots and then fails at
   everything time-dependent.
5. **Re-sweep every operating point.** `clk_div`, `write_throttle`,
   `burst_depth` and the seed are all measured against one specific part and
   one specific design. None of them transfers. See `PANELS.md` for what
   happens when a number outlives the architecture it was measured on.

---

## Sources

Measured numbers are from this repo's own build logs. Board facts:

- `wuxx/Colorlight-FPGA-Projects` — i5-v7.0 (`LFE5U-25F-6BG381C`), i9 v7.x
  (`LFE5U-45F-6BG381C`), i9plus-v6.1 (`XC7A50T-FGG484`). Pinouts, schematics,
  demos.
- `chmousset/colorlight_reverse` — i9+ and i5a-907 pinouts via a LiteX
  `PinoutFinder` and JTAG boundary scan. States that HUB75 connector pinouts
  are out of scope.

**Neither covers the 5A-75E, the E320, HUB320 or the K-series**, which is why
the HUB320 pin map in `HARDWARE.md` was measured here rather than looked up.
