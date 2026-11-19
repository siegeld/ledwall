# Panels — what this stack drives, and what to buy

**Before anything else: ask the vendor for the driver IC part number.**
Pixel pitch, module dimensions, "scan rate" and the receiving card model tell
you almost nothing about whether this repo can drive a module. The driver IC
decides it, because the two families speak genuinely different protocols on
the wire — not fast and slow versions of one protocol.

`CARDS.md` is the authority on *receiver cards*; this file is the authority on
*LED modules*. They are separate purchases and separate risks. Conflating them
is how a wall gets ordered against hardware that cannot drive it.

## The two families

| | shift-register (`hub75`) | S-PWM (`icn1065`) |
|---|---|---|
| Driver ICs | MBI5124, ICN2038S and similar | Depuw **DP3264S**, Chipone **ICND1065L** (both lit), NovaStar **DP3364S** (table only) |
| Who makes the greyscale | **the card**, as bit planes / BCM + a gamma LUT in fabric | **the chip**, from a 12-bit word per LED |
| Row stepping | parallel `A`–`E` address lines from the card | **`OE` pulses step a counter inside the chip**; VSYNC resets it |
| Start-up | none | **a register table** (13 on the DP3264S, 38 on the ICND1065L), one register re-sent every frame behind a `PRE_ACT` unlock -- commands are `LAT` pulse widths |
| Typical advertised specs | 8-bit grey, refresh to ~2 kHz | **13–16 bit grey, refresh above ~3 kHz** |
| Connector here | HUB75 / HUB75E, 16-pin | HUB320, 26-pin (also HUB75E on other modules) |
| Status | **proven**, in daily use | **proven** on two modules from two makers: ARGO's (in daily use) and Myled's (working, seen on glass 2026-11-18) — see the records below |

**The "advertised specs" row is the quick test when reading a spec sheet.** Anything
advertising 13–16 bit greyscale or refresh above about 3 kHz is almost
certainly S-PWM. A card with only five address lines can still drive 1/64
scan, because on an S-PWM module the row addressing happens *inside* the
driver IC.

## What to buy, if you want the thing we know works

Two P1.25 modules, from two makers and with two different driver chips, run
under the S-PWM stage. The one in daily use is
**ARGO's P1.25 320 × 160 mm, `XP1.25-5958S-256X128-64S`, 256 × 128 px,
1/64 scan, HUB320, Depuw `DP3264S` column driver.** **Four** of them run on one
Colorlight E320 as a 256 × 512 wall, streaming video at 20 fps — that is what
is on the bench and measured, not a projection.

The second is **Myled's P1.25** (Shenzhen Myled Optech; Chipone `ICND1065L`,
record further down). Two of them hang side by side on one E320 as a 512 × 128
picture, upright and whole, streaming with zero underruns on the white balance
its maker supplied (gateware 3.6.3 / firmware 5.8.3, 2026-11-18). It is the
newer of the two: a ghost twin seen in an early close-up has not been
re-examined, and its on-module calibration is not read.

Both have a ready-made layout and a server module type — the table under
"Ready-made" below is the shortest path from a module in your hand to a lit
one. If you are buying new, ARGO's has the most hours on it; Myled sent its
receiver profiles when asked, which is where its mounting and white balance
came from; ARGO's module has no maker we can ask. If you are buying
something else, the questions below are the ones whose answers decide whether
it works — and the ones a reseller usually cannot answer, which is itself
useful information.

### Five questions to ask before ordering

1. **The exact driver IC marking on the chip.** Not the model on the invoice.
   Four supplier sheets gave four different parts for the single module below
   (`ICN1065L`, `DP3364S`, `ICND1065S`, `DP3264S`); only one was right, and
   `DP3364S` is a genuinely different chip whose datasheet cost a day.
   **The part marking on the chip wins** over every spec sheet and invoice.
2. **The row driver.** A 595-type serial shift register and a 138-type decoder
   need different scan-out. Ours is a shift register (`HX6158HSP`), 8 chips ×
   8 outputs = 64 rows, which is *not* what "1/64 scan" implies on its own.
3. **The connector and its pinout** — HUB75, HUB75E or HUB320, and what sits
   on **pins 20 and 21**. Some modules put a serial bus there (Myled's does);
   gateware 3.1.0 and later boots with pin 20 high and pin 21 released, every
   earlier bitstream drove both. **Read the silkscreen before plugging a new
   module into a card** (`docs/HUB320.md`).
4. **The RGB group arrangement.** How many groups, how wide each chain is, and
   where each group lands on the glass. The module below is four 128 × 64
   quadrants in a 2 × 2 — which is not what its connector or its "256 × 128,
   1/64" label suggests, and getting it wrong gives a sawtooth or a blank half
   rather than an error. Myled's, with the same label, is four strips stacked
   and hangs a quarter turn; the arrangement also decides the row-fetch order,
   so it is a property of the bitstream.
5. **The receiver profile** (`.rcvbp` / `.rcfgx`), and a schematic if they
   will part with one. The vendor profile is how we recovered the factory
   register values; `tools/decode_rcvbp.py` reads it, and
   `tools/rcvbp_import.py` turns it into a marquee module type whose registers
   reach the card as `chip_reg:` lines.

### What will not work

* **Panels with an on-module receiver you cannot bypass.** This stack *is* the
  receiver.
* **Any module whose driver is neither family above.** A third protocol means
  a third output stage: `gateware/icn1065.py` is roughly what that costs.
* **Per-pixel flash calibration is not applied.** Modules store brightness and
  chroma correction that a stock receiver reads and uses; we do not (open item
  below). Invisible on one module; across a multi-module wall it is what keeps
  them looking like one display. Buy modules from **one batch** if you can.

### Ready-made: the bitstream, firmware, layout and module type per module

Everything a module needs is four things, and all four exist for both:

| | ARGO P1.25 (`DP3264S`) | Myled P1.25 (`ICND1065L`) |
|---|---|---|
| Maker / source | maker unidentified; sold by ARGO | Shenzhen Myled Optech (smyled.com) |
| As the card sees it | 256 × 128, four 128 × 64 quadrants in a 2 × 2 | 128 × 256, four 128 × 64 strips stacked; hangs a quarter turn (`rotate: 90`), so the picture is 256 × 128 |
| Bitstream (E320, four outputs) | `PANEL=256x128-dp3264s-hub320 exp/build-rt.sh` | `PANEL=128x256-icnd1065l-hub320 exp/build-rt.sh` |
| Firmware image | built against that bitstream: `e320-spwm-argo-v<ver>.bin` on a release | built against that bitstream: `e320-spwm-v<ver>.bin` on a release |
| Layout (boot config) | [`custom/example/configs/argo-p1.25.yml`](../custom/example/configs/argo-p1.25.yml) | [`custom/example/configs/myled-p1.25.yml`](../custom/example/configs/myled-p1.25.yml) (two side by side: `exp/bench-myled.cfg`) |
| Server module type | `linsn-p1.25-256x128-dp3264s` (the key predates knowing whose module it is) | `myled-p1.25-128x256-icnd1065`, carrying the vendor's white balance |
| Most recently run | six on two chained E320s, 512 × 384, video at 20 fps, gateware 3.6.3 / firmware 5.8.5 (2026-11-19); four stacked, 256 × 512, on 1.46.0 / 3.9.0 | two stacked on one E320, 256 × 256, video at 20 fps, gateware 3.6.3 / firmware 5.8.5 (2026-11-19) |

**The firmware image follows the bitstream, not the module.** Each bitstream
wakes up with its own module's chip table and group positions, and the
firmware is compiled against that bitstream's register map, so the two images
above are not interchangeable even though the source is one tree. With the
server (`marquee`) you pick the module type on the card's page and it serves
the layout; without one, bake the layout in with `--default-config` as each
file's header shows.

Both bitstreams are **runtime-geometry** builds (`docs/RUNTIME-GEOMETRY.md`):
the size of one RGB group is a setting (`group_columns:`, `group_scan:`)
inside the build's 128 × 64 maximum, so a smaller module of the same family
is a layout edit rather than a new bitstream. A different chip or a different
arrangement of groups still needs its own entry in `gateware/spwm_chips.py`
and its own preset.

A vendor's receiver profile (`.rcvbp`) becomes a module type with
`tools/rcvbp_import.py`: it reads the mounting, the group positions and the
per-colour registers and writes the JSON the server loads, and those
registers reach the card as the `chip_reg:` lines you see in Myled's layout.

### Geometry presets

`gateware/colorlight.py` `PANELS` is the list, and it is the source of truth:
`128x64`, `256x64`, `96x48`, `64x32`, `64x64` for shift-register panels, and
the `*-icnd1065-*` / `*-icnd1065l-*` / `*-dp3264s-*` family for S-PWM, with
variants trading shift clock, colour depth (32-bit or RGB565) and gamma against
refresh rate and FPGA timing. Per card you also set channel order, scan rate,
chain length, rotation and position. `256x128-dp3264s-hub320` is the preset
for ARGO's module and `128x256-icnd1065l-hub320` for Myled's; the other S-PWM
presets build but have never lit a module.

---

## Who makes what

Four different companies have to agree before a module works, and they are
rarely the company you bought from. Knowing which is which is what lets you ask
the right question of the right party.

### Driver ICs — this is the part that decides compatibility

| Vendor | Part | Family | Our status |
|---|---|---|---|
| **Depuw** | `DP3264S` | S-PWM, 16-channel | **proven on glass.** `gateware/spwm_chips.py` carries the factory register table; datasheet in `docs/datasheets/` |
| **Chipone** | `ICND1065L` (`ICND1065S` by the same table) | S-PWM, 16-channel | **`ICND1065L` lit on glass** (Myled's P1.25, 2026-11-15). The `icnd1065` table is transcribed from an ESP32 driver (`nimakolahi/hub75-icn1065`), not a datasheet -- the register map is unpublished -- with the row pulse, data latch and VSYNC alignment measured on that module. No `ICND1065S` module has been connected; `ICND1065(AP)` is a different register map and is not driven |
| **NovaStar** | `DP3364S` | S-PWM | **a different part from the DP3264S** — 15 registers, 6 LE commands. Table unverified. Two supplier sheets called our DP3264S module this, and believing them cost a day |
| **Chipone** | `ICN2038S` | shift register | works under `hub75` |
| **Macroblock** | `MBI5124` | shift register | works under `hub75` |
| **Raffar / HX** | `RT5958D`, `HX6158HSP`, `TC7559` | 595-type **row** shift register | the row half of ARGO's P1.25 module. Not a 138 decoder — different scan-out |
| **Chipone** | `ICND1028AP` | 16-channel PMOS **row** switch, serial decoding | the row half of Myled's P1.25. Steps on the same serial-chain mapping (`A=LCK B=BK C=DIN`). No public datasheet |

Chipone is also written "ICN"/"ICND" inconsistently across sheets, which is how
`ICN1065L`, `ICND1065S` and `ICN1065` all end up naming the same family while
`DP3364S` and `DP3264S` differ by one digit and are not interchangeable.

### Module makers

| Company | Site | What |
|---|---|---|
| **Shenzhen Linsn LED Co., Ltd** | linsn-led.com / linsnled.com | makes a similar-looking 320×160 mm P1.25 module and sent the `DP3365S` datasheet. **Not the maker of the module below** — an early PCB match said so and was wrong. Also **not** Linsn Technology, who make sender cards |
| **Shenzhen Myled Optech** | smyled.com | maker of the second P1.25 module (RB-5504): sold as `ICND1065S`, the chips read `ICND1065L`. On the bench by 2026-11-15, working by 2026-11-18. Sent its receiver profiles and a stock E320 bitstream, which is where its white balance and mounting came from |
| *unidentified* | — | maker of the first P1.25 module, `XP1.25-5958S-256X128-64S-1010-B1`, sold to us by ARGO. The PCB marking is all we have |

### Receiver cards and resellers

* **Colorlight** makes every card this repo targets — the 5A-75E and the E320.
  See `CARDS.md`. They also write the stock firmware and the LEDVISION /
  LEDSetting profiles in `factory/`, which is where our register values came
  from.
* **ARGO** resold us the first P1.25 module alongside an E320, a K5+ and a K9+.

Nobody in either supply chain writes receiver firmware: the cards run stock
Colorlight firmware, and the profiles the suppliers send are stock LEDVISION
files. **Resellers usually cannot answer any of the five questions above.** That is not a complaint — it is a planning fact. Every
technical answer we have came from one of three places: the part marking on the
chip, the vendor's own `.rcvbp` profile, or a datasheet. For ARGO's module
**none came from a reseller, and none came from the manufacturer either** — its
maker is still not identified. Myled did send its profiles, and they answered
the mounting and register questions (record below).

So: budget for recovering the facts yourself, and treat a vendor reply as a
bonus rather than a dependency. `tools/decode_rcvbp.py` plus the chip marking
is the path that actually works.

---

# The per-module record

The receiver cards and the gateware are generic; what differs is the module on
the other end of the ribbon, and that is where every bring-up has spent its
time. Below is the per-module record: who made it, what is on it, which chip
table drives it, how marquee declares it, and what is still open. Add an entry
when a new module type arrives; **do not** write its facts into
`E320-BRINGUP.md` or a chip-table comment where the next module cannot find
them.

---

## ARGO P1.25 320x160 -- `XP1.25-5958S-256X128-64S-1010-B1`

The module on the bench since 2026-10-15, and the one the four-module wall is
built from. The first S-PWM module this project has lit, and the one
`docs/E320-BRINGUP.md` is about.

### Identity

| | |
|---|---|
| PCB marking | `XP1.25-5958S-256X128-64S-1010-B1` |
| Board code | `C4S5393A`, dated 2022-06-25 |
| Sticker | `P1.25-RS260709-2985` |
| Size / pitch | 320 x 160 mm, P1.25, SMD1010 |
| Pixels | **256 x 128** |
| Scan | "1/64" on the sheet. Four RGB groups of 128 x 64; see geometry |
| Connector | HUB320, 26-pin ribbon. **Pins 20 and 21 are NC** on this module -- three address lines A/B/C only |
| Column driver | **Depuw `DP3264S`**, 16-channel S-PWM, read off the chip. Four supplier sheets said `ICN1065L`, `DP3364S`, `ICND1065S` and `DP3264S` for the same module; `DP3364S` is a different part and its datasheet cost a day |
| Row driver | Marked `5958S` on the module code; the chip reads `HX6158HSP`. A 595-type **serial shift register** (Raffar `RT5958D` datasheet used), 8 chips x 8 outputs = 64 rows -- NOT a 138 decoder. The reseller's profile name says `7559` (TC7559), same family |
| Datasheets | `docs/datasheets/DP3264S_REV2.0_translated.pdf`; `RT5958D` v1.6 |

### Supply chain -- who is who

| | |
|---|---|
| Maker | **Not identified.** This is **not** a Linsn module: a 2026-10-18 match of the PCB to Linsn's 320x160 P1.25 was wrong (corrected 2026-11-15). ARGO is the only source we have for it |
| Reseller | **ARGO** (contact details withheld -- a small reseller, not a public company). Shipped this module as a "soft-connection test module" with an E320, a K5+ and a K9+. Writes no firmware |
| Receiver firmware | **Stock Colorlight**. `factory/` holds the E320's flash images and the K5+/K9+ builds ARGO forwarded on 2026-10-19 |
| Receiver profile | `factory/CLTp1.25-320-160-3264s-7559-64s-6.rcfgx.rcvbp` -- a stock Colorlight LEDVISION/LEDSetting profile. Decode with `tools/decode_rcvbp.py`. Register table, data-group map `[8, 9, 0, 1, 10, 11, 2, 3]`, pixel map **identity** (records are `(y, x_lo, x_hi)`; framed one byte off it fakes a carry at column 127 -- see `E320-BRINGUP.md` §5c) |
| Not this module | Shenzhen Myled Optech (smyled.com) supplied a DIFFERENT P1.25 (sold as `ICND1065S`; RB-5504), which arrived by 2026-11-15: Chipone `ICND1065L` column drivers, `ICND1028AP` row drivers, four strips stacked, and **a serial bus on HUB320 pins 3, 7, 20 and 21** (`docs/HUB320.md` column 3) -- safe only on gateware 3.1.0 and later. It has its own record below, its own preset and its own bitstream |

### How we drive it

| | |
|---|---|
| Card | Colorlight **E320** (ECP5 `LFE5U-25F-6`), built at **35 MHz** -- the -6 part does not close 40 |
| Build | **Four outputs: `exp/build-out4.sh`** (the reader and block-RAM trims that let a fourth output fit; `E320-BRINGUP.md` §5a, `ARCH.md` §13). Up to three: `./build.sh --board e320 --panel 256x128-dp3264s-hub320 --outputs N --sys-clk 35000000 gateware` / `pac` / `firmware`. Either way `pub const OUTPUTS` in `hub75.rs` must equal N. **`--outputs` does NOT update that constant — set it by hand, and the failure if you forget is silent.** Timing tightens fast with N: one output closes with margin, three need `--seed 1`. Editing a program's YAML also needs `./build.sh panelc` BEFORE `firmware` -- the firmware build does not regenerate `generated.rs` |
| Known good | **gateware 3.6.3, firmware 5.8.5** (2026-11-19), the runtime-geometry build `PANEL=256x128-dp3264s-hub320 exp/build-rt.sh`: six modules on two chained E320s (three stacked on each), one 512x384 picture, video at 20 fps with 0 underruns, 0 dropped chunks and 0 held frames, `packet_delay` 0.00014 per card. First run 2026-11-18 on 5.8.3: three stacked, 256x384, 10 fps, 66.8 Hz. Layout: `custom/example/configs/argo-p1.25.yml`. Earlier: **gateware 1.46.0, firmware 3.9.0**, 35 MHz, `clk_div 4` (66.7 Hz): **four outputs**, a full 256x512 four-module stream at 20 fps, 0 underruns, all four connectors proven on glass (`E320-BRINGUP.md` §6b). Before it, gateware 1.34.0 / firmware 3.6.0 ran two modules at 20.00 fps, and a three-output bitstream closed with `--seed 1` at 56/56 EBR. Also runs `railboard` fed by marquee's value pusher |
| Operating point | `clk_div` **4** (8.75 MHz panel clock), `row_slot` **64** panel clocks, **64 display passes**, **13-bit** grey. All four are coupled and none was guessed -- see the derivation below and `E320-BRINGUP.md` 4.6 and 4.10 |
| Geometry | **Four 128 x 64 RGB groups in a 2x2** on one connector: g0 top-right, g1 bottom-right, g2 top-left, g3 bottom-left (glass, 2026-10-17). The gateware places each group at its origin (`group<g>_origin` CSRs, preset defaults `128,0 128,64 0,0 0,64`), so the framebuffer is the glass, 256 wide x 128 tall, and a raw write lands upright. Before gw 1.18 the framebuffer was a 128x256 vertical stack and only the test patterns were remapped (`quad_src_xy`), so streamed content was scrambled. §1a / §4.3 of `E320-BRINGUP.md` |
| marquee layout | card `e320` (id 2), `panel_width: 256`, `panel_height: 128`, `drive: dp3264s`, `group_origins: 128,0 128,64 0,0 0,64`. With one module fitted: `grid: 1x1`, `J1: 0,0` (legal since gateware 1.20.0, when the stride started following `ctrl.width`; on 1.18/1.19 all four outputs had to be declared, `grid: 4x1`, or the picture folded). Adding modules is a layout edit, no gateware change: stack them with `grid: 1xN`, `J1: 0,0` ... `JN: 0,N-1`. **A show must be drawn for the wall's full size** -- layer boxes are absolute, so a 256x256 show on a 256x512 wall leaves the lower modules black |
| Chip table | `gateware/spwm_chips.py` `dp3264s`: the **factory register values** from the vendor profile, including **`reg0x03 = 0x3f`** -- see the derivation below. `chan_rev=0` (the glass overrules 11.2's channel-15-first for this wiring; most likely PCB routing). **`sweep_adv=1`** -- the chip commits each `DATA_LATCH` at pointer+1, the cause of the 16-pitch dots (4.6a), and the ONE value still inferred rather than scoped. **`chan_xor=0`** -- the pair swap carried as 1 until 2026-10-19 was a fitted artefact of the pre-1.18 pattern path; on the raw path it put both borders one column in and garbled the version text |
| Row chain | ROW free-runs for the whole frame (the mode requires it uninterrupted). A/B/C are the 5958 **serial chain** -- A=LCK, B=BK, C=DIN, the `row_ser` reset mapping (`docs/SCOPE.md`). The table's `row_address` / `row_addr_shift 3` (A/B/C as `scan_row >> 3`) only take effect if `row_ser.enable` is cleared, for a decoder-type module |
| Signals confirmed on a scope | `docs/SCOPE.md`; CLK on pin 23, LAT on 24 |

### The derivation everything else hangs off

The chip displays the range its OWN REGISTERS set, and the same field sets the
row-pulse cadence. Get either wrong and the failure is silent on pure colours:

```
11.7.5   levels = 4 x (reg0x04+1) x (reg0x03+1)
         factory: 4 x 32 x 64 = 8192  ->  13-bit grey, = the profile's grayScale
         ours was reg0x03 = 0x00 -> 128 levels: every MIXED colour clipped to
         full while red, green, white and the grid test screen looked perfect

11.6.2   one W12 ROW pulse per (groups x scans) = 64 x 64 = 4096 pulses
         so the row scanner counts display passes

11.7.2/4 a row needs 4 x (reg0x04+1) GCLKs at FGCLK = FDCLK x (reg0x06[2:0]+1)
         = 42.7 panel clocks  ->  row_slot 64 clears it
```

`grey_bits`, `display_passes` and `pwm_levels` are **computed from the register
table** (`pwm_range_from_regs`), so the stage and the chip cannot silently
disagree. `clk_div` is the one number that is measured rather than derived.
`DP3365S_Rev1.0_EN` in `docs/datasheets/` confirms all three rules
independently for this chip family.

### Status and open items (current: 2026-10-22)

* Gateware 1.25.0 / firmware 2.95.1: the framebuffer is the glass, the raw
  test screen is flush on all four edges with legible version text; `sweep_adv`
  (1) and `chan_xor` (0) are written at boot by `spwm::configure()`.
* **Closed 2026-10-19:** `chan_xor` is 0. The pair swap was an artefact of
  the compensated pattern path, not a property of the module or the chip.
* **Open:** the pointer+1 commit is established by elimination and
  simulation; seeing it on LE (pin 24) against the data would close it.
* **Open:** the module's maker, schematic and HUB320 sheet -- ARGO is the only
  channel.
* **Closed 2026-10-19:** the "washed out" colours were the chip's DISPLAYABLE
  RANGE. We wrote `reg0x03 = 0x00` against the factory's `0x3f`, which sets
  `4 * (0x04+1) * (0x03+1) = 128` grey levels, so every mixed colour clipped
  to full while pure primaries looked perfect. Gateware 1.23.0 takes the
  factory value (8192 levels, matching the vendor profile's
  `grayScale: 8192`), counts display passes so the W12 cadence still matches
  (11.6.2), and DERIVES the grey depth from the register table. See
  `E320-BRINGUP.md` 4.10.
* **RESOLVED 2026-10-21:** streaming through marquee works, and **two of
  these modules run on one E320** -- J1 above J2, 256x256, gateware
  `out2-v1.34.0` (seed 3), firmware 3.6.0. Measured at 20.00 fps delivered:
  **zero chunks lost in 1.13 million**, `frames_dropped` 0, `underruns` 0.
  Chunk loss turned out to be host-side (a sender starved by the renderer),
  not the card or the network.

* **REFRESH RATE IS A PICTURE SETTING, and 44 Hz is not enough.** `clk_div`
  ran at **6** from 2026-10-20 because 4 measured 5454 underruns/s -- but that
  was gateware **1.28.0, before the line buffer**, which removed exactly the
  read deadline a faster panel clock narrows. Re-swept on 1.34.0, one variable,
  25 s each, two modules streaming:

      clk_div 6 ... 44.45 Hz refresh, 0.00 underruns/s
      clk_div 4 ... 66.67 Hz refresh, 0.00 underruns/s

  44.45 Hz is **below human flicker fusion** and produced a visible beat in
  near-black content while bright content on the same rows sat still -- a
  near-black pixel is lit for a very short pulse each refresh. `clk_div 4` is
  now the module's operating point in marquee's `PANEL_TYPES`.

  **Re-sweep `clk_div` after any change to the scan-out path.** A number
  measured against the old architecture stood for a day after it stopped being
  true, and cost an afternoon chasing a flicker through the content, the
  sender, the line buffer and the buffer swap. `POST /api/clkdiv {"n": N}` is
  a runtime CSR -- seconds to test, seconds to revert.
* **Open:** the module stores **per-pixel flash calibration** (brightness and
  chroma) that a stock receiver reads and applies; we do not, and the vendor
  profile's 49 KB correction records are empty here. Invisible on one module;
  across a multi-module wall it is what keeps them looking like one display.
* **Open:** `clk_div 2` would double the refresh again but snowed even with a
  single module when last tried -- a latency wall, not bandwidth. That test
  also predates the line buffer, so like `clk_div 4` it deserves a re-sweep
  against `underruns` before being treated as settled.

---

## Myled P1.25 320x160 -- Chipone ICND1065L + ICND1028AP

From Shenzhen Myled Optech (smyled.com), the nine-module cabinet ordered under
RB-5504. The second S-PWM module this project has lit, and **working**: two
hang side by side on one E320, upright and whole. The dated sections below
were written as the bring-up happened, so read them as a log; the tables here
are where it stands.

### Where it stands (gateware 3.6.3 / firmware 5.8.3)

| | |
|---|---|
| Card / build | E320, four outputs: `PANEL=128x256-icnd1065l-hub320 exp/build-rt.sh`, the runtime-geometry build. **Its own bitstream**, not ARGO's: the preset is what the card wakes up as (chip table, strip positions) |
| Mounting | `rotate: 90`. Two side by side, J1 the left one: `grid: 1x2`, `J1: 0,1`, `J2: 0,0` (`exp/bench-myled.cfg`); one alone: `custom/example/configs/myled-p1.25.yml` |
| Preset | `128x256-icnd1065l-hub320`: `columns 128, scan 64, groups 4`, module 128x256, `group_origins` `0,0 0,64 0,128 0,192`, chip `icnd1065`, `clk_div 4` |
| Chip table | `gateware/spwm_chips.py` `icnd1065` (and the generated `spwm_tables.rs`): the ESP32 transcription plus what the glass required -- `data_latch 1`, ROW pulse 4 / first 12, `display_passes 64`, `row_serial 1`, `vsync_align 1`, `row_din 1`. One value per register on all three colour lines |
| Per-module registers | `chip_reg: <addr> <r> <g> <b>` lines in the boot config (firmware 5.6.0) lay a module's own values over that table. `tools/rcvbp_import.py` writes them from the vendor profile |
| marquee | module type `myled-p1.25-128x256-icnd1065` (`drive: icnd1065`, stacked origins, `rotate 90`, `clk_div 4`, `write_throttle 0`, and the vendor's ten per-colour registers as `chip_regs`) |
| Measured | two modules, 512x128, streamed at 10 frames/s: 0 underruns, 63 Hz at `clk_div` 4 |
| Seen on glass | 2026-11-18: the test grid upright and whole on both modules, a single edge line on each side |

**Not yet verified on a panel:** the ghost twin from the first close-up has
not been looked for again on the current build; register 0x02 bit 6; and
84 Hz (`clk_div` 3) as a default.

### Identity

| | |
|---|---|
| Board marking | `03`, `D/C 2616.001` |
| Size / pixels | 320 x 160 mm, P1.25, **256 x 128**, 1/64 scan |
| Column driver | **Chipone `ICND1065L`**, read off the chip (board position BU20) |
| Row driver | **Chipone `ICND1028AP`** (UU2, UU16): 16-channel PMOS row switch with "serial decoding" (chiponeic.com/en/tmpld/610). No public datasheet |
| Connector | HUB320, 26-pin. **Pins 3/7/20/21 double as a serial bus** -- `B1/S_C?`, `B2/S_MO`, `S_CS`, `S_M?` -- to the module's calibration flash (the supplier, 2026-10-20: "we have stored the calibration file in each LED module"). See `docs/HUB320.md`. **Needs gateware >= 3.1.0**, which boots pin 20 high and pin 21 released |
| Calibration | Per-module colour calibration in on-module flash. Not read or applied yet |
| Vendor files | **Received 2026-11-15**: two receiver profiles and a stock E320 bitstream, in `factory/`. See "The vendor profile" below |

### What lit it (2026-11-15, gateware 3.1.0 / firmware 5.1.0, E320, J1)

Applied LIVE over the card's endpoints (`/api/chipreg`, `/api/chipcfg`,
`/api/rowser`), because the card sat on the main LAN with no boot config. The
script is `tools/icnd1065_live.sh`.

| Setting | Value | Note |
|---|---|---|
| Register table | `icnd1065` (38 regs, the nimakolahi ESP32 table) with **`reg0x02 = 0x023f`** (64 scan lines) | unchanged from the transcription otherwise |
| Grammar | `slots 5`, `rot_slot 2`, `mid_latch 1`, `reg_count 38` | as transcribed |
| **ROW pulse** | **`row_pulse 4`, `row_pulse_first 12`** | the reference driver's `OE_LEN_REGULAR`/`OE_LEN_RESTART`. The firmware table carried 2/2 |
| **Data latch** | **`data_latch 1`** -- a 1-clock LAT after every 16-bit word | **the change that lit it.** Everything else in place and the panel was dark through a 51-step row sweep; one write of this and it was bright white. The `icnd1065` table carried 0 |
| Row chain | serial, `A=LCK B=BK C=DIN`, no inversion (the `row_ser` reset mapping) | the same as the ARGO module's 5958 chain. The ICND1028 steps the same way |
| `sweep_adv`, `chan_xor` | 0, 0 | not yet checked at pixel level |

The 51-step sweep (address mode at shift 0 and 3, rows held, and all 48
serial-chain mappings and polarities) ran **before** `data_latch` was set and
stayed dark throughout -- so it says nothing about which row mappings work.
Only the reset mapping has been seen lit.

### Geometry

**Not the ARGO 2x2.** Read off a photo of `grid` (2026-11-15), driven with the
ARGO preset's origins (`128,0 128,64 0,0 0,64`, a 256x128 framebuffer):

* With the module held **portrait** (128 wide, 256 tall), the four 128x64 group
  tiles are **stacked top to bottom, each rotated 180 degrees**, in the order
  g3, g2, g1, g0. The image's left half (g2, g3) filled the top square, the
  right half (g0, g1) the bottom square; each square showed its half upside
  down, with the diagonals and the coloured verticals (red col 0, green 64,
  blue 128, magenta 255) exactly where that predicts.
* So, **rotated 180 degrees as a whole, the module is 128 x 256 with groups
  0..3 stacked top to bottom, all upright** -- the gateware's plain stacked
  layout (`group_origins` `0,0 0,64 0,128 0,192`, framebuffer 128 x 256), no
  fold at all. Mounted landscape (the 3x3 cabinet is 960 x 480 mm), that is a
  90-degree rotation of the picture.
* `quad` looked right because each group still carried its own colour; it
  cannot see placement inside the module. `grid` can.

**Stacked origins on glass (firmware 5.2.0, `POST /api/layout`):** a
`grid` photo, portrait, came up STABLE and almost right:

* Columns correct -- coloured verticals at 0/32/64/96/127, both edges lit
  (mirrored as held, col 0 on the connector side).
* **Rows rotated by 32 inside every group.** The white lines sat mid-group
  (32, 96, 160, 223/224 merged) instead of on the group boundaries, and the
  diagonals broke into 32-row segments. The ICND1065's scan line `s` lights
  physical row `s + 32`: the ICND1028 chain and the column chips' line counter
  start half a group apart. Gateware 3.2.0 added `row_ser.din_row` -- the scan
  row at which the chain's DIN goes in -- to cancel that. It did not: the
  offset was VSYNC phase (next sections), `din_row` had no visible effect, and
  the `icnd1065` entry carries 0.

### Sync -- the picture scrolled until the display was restarted

With firmware 5.2.0 configuring the chip from its own table (`drive:
icnd1065` in a posted layout) the picture **scrolled continuously**, on both
layouts, with every register identical to the hand-loaded setup that had been
stable. Two causes, both fixed in 5.3.0:

1. **W12 cadence.** The generated table carried `passes_m1 0` (W12 every
   pass); what locked was 63 (W12 once per 64 passes). The `icnd1065` entry
   now states `display_passes=64`. Do NOT set it with `/api/passes` on this
   chip: that also rewrites register 0x03, which on the ICND1065 is not the
   pass count (`0x0335` must be restored by `/api/chipreg` index 3).
2. **The scan must restart after a chip change.** Even with the cadence
   right, the picture only locked after `display/off` + `display/on`.
   `apply_layout` now restarts the display after configuring the chip.

### The rotation and the scrolling were one fault: VSYNC phase

What looked like a fixed per-group row rotation was not fixed. Measured on
glass, 2026-11-15:

* `row_ser.din_row` (where the serial chain's DIN goes in) at 0, 16, 31, 32:
  **no visible change** -- the ICND1028 rows are not stepped by our A/B/C chain.
* A fetch-side rotation (`latch_cfg.row_rot`, gateware 3.3.0) at 0, 32, 48: the
  residual moved ~30 rows for 16 of rotation, and in no consistent direction
  -- because it was not a constant to cancel.
* The picture **locked or scrolled depending on what had just happened** (a
  chip table written live, a pattern re-drawn, a stream started), with every
  register identical in both states.

The explanation that fits all of it: the ICND1065L **resets its line counter
on VSYNC**, and VSYNC goes out at the start of every data frame -- while ROW
free-runs, as the DP3264S requires. So the chip's line 0 lands on whatever row
the scanner happens to be on when the frame starts: a rotation that changes
from frame to frame (scrolling) or holds still when the data frame happens to
stay in phase (a "lock"). The reference ESP32 driver pauses row scanning
around VSYNC, which is exactly the alignment this was missing.

**Gateware 3.4.0 adds `latch_cfg.vsync_align`**: a data frame (and its VSYNC)
starts only where the row scanner begins a pass. It costs at most one pass
(~4 k clocks) per data frame (~130 k), about 3%. The `icnd1065` entry sets it;
`row_rot` is back to 0 and stays a live knob (`/api/rowrot`), as does
`/api/vsyncalign`. The `din_row` field is kept but has no effect on this
module.

**3.4.0 still scrolled while streaming, and 3.4.1 fixes why.** The output FSM
starts a frame from two states: IDLE (the first frame after enable) and HOLD
(every frame after that). 3.4.0 gated only IDLE, so a static pattern -- which
starts one frame and sits in HOLD -- aligned, and a stream, which re-enters
from HOLD every frame, did not. That also explains the earlier "locked after
X, scrolling after Y" confusion. 3.4.1 shares one start condition between both
states, and `sim-icn1065` now runs five frames with `start` held high (as the
SoC holds it) and requires every VSYNC to follow a pass end. The check fails
on 3.4.0 (frames 1-4 mid-pass), which is the proof it can catch this.

**On glass with 3.4.1 (2026-11-15), streaming the ruler:** every group shows
all eight bands in order with its white row-0 line, so the row mapping inside
each group is exact and does not rotate. The whole picture is turned 180°
relative to the stacked layout as photographed (connector on the right):
groups read 4-3-2-1 from the top, bands run pink to red downwards, and the
group ticks drawn on the left appear on the right. That is orientation, not
mapping. Remaining artifacts: a dim twin row beside each white line (the
ghosting below, now seen vertically too), and colour casts (yellow reads
cream, orange reads pink) still to be checked by eye rather than by camera.

Also: a ruler frame streamed over UDP (`tools/ruler_frame.py` -- 8-row colour
bands per group) is the way to read the row mapping exactly; estimating row numbers from
grid diagonals in angled photos is good to about 5 rows and cost several
rounds.

### Flicker while streaming: the row fetch has no margin

Measured 2026-11-15 on 3.4.1 with the plain ruler arriving at 12 frames/s
(`underruns` in `/api/status`, read twice and subtracted):

| state | underruns/s | fill (sys clk per row) | refresh |
| --- | --- | --- | --- |
| idle, last frame held | 0 | 7490 | 65 Hz |
| streaming, `clk_div` 4 (as booted) | 2000-5000 | 7830 | 65 Hz |
| streaming, `clk_div` 5 | 0 | 7780 | 52 Hz |
| streaming, `clk_div` 6 | 0 | 7780 | 43 Hz |

Every underrun is a pixel shifted before its row was fetched, which is the
flicker. The fetch takes ~7800 clocks a row against a budget of about 8200 at
`clk_div` 4, and incoming frame writes push it over. `write_throttle` does not
help (2 is the least bad; 4 and up are far worse). `clk_div` 5 clears it at
the cost of refresh.

Why this module is 2.4x slower than ARGO's on the same bitstream (ARGO fills a
row in ~3100 clocks): the block fetch reads the two groups that share a
framebuffer row as one run with one DRAM row open. ARGO's strips are a 2x2, so
it has two such pairs. This module's strips are stacked, so NO two groups share
a row, and the interleaved order changes DRAM row on every single read --
`tools/sim_fillrate.py` with stacked offsets shows 512 row misses in 512
reads, exactly as slow as not pipelining at all. (An earlier note here blamed a
256-column shift. Wrong: the build already shifts 128; `hw_columns` reports
the glass.)

**Gateware 3.5.0: `block_seq`, and a preset for this module.** When the
origins have no same-row pair, the reader issues each group as its own run of
K columns -- one row change per K reads. Same commands and count, different
order; the line buffer contents are identical (sim: 128 misses in 512, fill
2.27x faster). The pairing is chosen at build time from the preset's origins,
so this module gets its own bitstream: `128x256-icnd1065l-hub320`
(`PANEL=128x256-icnd1065l-hub320 exp/build-out4.sh`), which also wakes up with
the ICND1065 chip table and the stacked origins.

Measured on the card with 3.5.0, straight from a power cycle with no layout
posted and no live register writes, the plain ruler streaming at 12 frames/s:

| `clk_div` | underruns/s | fill (sys clk per row) | refresh |
| --- | --- | --- | --- |
| 4 (boot default) | 0 | 3905 | 63 Hz |
| 3 | 0 | 3918 | 84 Hz |

The fill halved (7830 to 3905) and `clk_div` 3 now has room, where on ARGO's
bitstream 4 did not. 63 Hz rather than 66.7 is the cost of `vsync_align`.

### The vendor profile (received 2026-11-15)

The supplier sent three files, now in `factory/`:

| File | What it is |
|---|---|
| `myled-COB-p1.25-640x320-1065L.rcvbp` | receiver profile for a 640x320 mm cabinet: ONE card, area 512x256, 16 data groups (2x2 modules) |
| `myled-COB-p1.25-960x480-1065L.rcvbp` | receiver profile for a 960x480 mm cabinet: card area 256x384, 12 data groups (three modules, one above the other), so three cards a cabinet |
| `E320_PCB6.0_PWM_FPGA12.52_2024527_FDFP3.0_encryption_ICND106.hex` | stock Colorlight E320 bitstream for this chip (Diamond 3.10.3.144, 2025-05-26). Reference only; never flashed here |

`tools/decode_rcvbp.py --pixel-map <file>` prints all of the below. The two
profiles agree on everything except the card area and the data-group map.

**The module mounts landscape, a quarter turn from how we have held it.**
marquee's `Card.rotate` (streamed frames) and the firmware's `rotate:` (what
the card draws itself, 5.7.0) act on this. **It is 90**, seen on glass
2026-11-18: the profile's map reads as 270, and 270 hung upside down, so our
group order runs the other way from the profile's. The
profile describes one data group as 64 wide x 128 tall, and its pixel map
(record 0x03) is not identity: logical pixel (row r, column c) is physical
(scan line c, chain column 127 - r). So a group's 128-column shift direction
runs BOTTOM TO TOP in the cabinet, its 64 scan lines run left to right, and the
module is 256 wide x 128 tall (320x160 mm): four 64x128 tiles side by side.
3x3 of those is the 960x480 cabinet. This is the answer to "which way up": the
180-degree turn seen on the bench was us holding it portrait.

The first two bytes of record 0x01 are therefore the group's LOGICAL width and
height. On ARGO's profile (identity map) they happened to equal columns and
scan lines, and the decoder used to label them so.

Data-group maps (slot = tile position in the card area, value = card output):

    640x320  [1, 5, 9, 13, 0, 4, 8, 12, 17, 21, 25, 29, 16, 20, 24, 28]
    960x480  [0, 8, 16, 24, 1, 9, 17, 25, 2, 10, 18, 26]

How those output numbers map to connectors and RGB groups on the stock card
has not been worked out.

**The factory register table is per colour, and differs from ours in 13 of 38
registers.** Ours was one value on all three lines (the ESP32 transcription).

| Reg | Ours | Factory R / G / B |
|---|---|---|
| 0x02 | 0x3f | 0x7f (low six bits = 64 scan lines, as ours; bit 6 unknown) |
| 0x03 | 0x35 | 0x3f |
| 0x04 | 0x12 | 0x2c |
| 0x07 | 0x20 | 0x10 |
| 0x0c | 0x18 | 0x01 / 0x0e / 0x0e |
| 0x0d | 0x01 | 0x01 / 0x05 / 0x03 |
| 0x0f | 0x01 | 0x01 / 0x01 / 0x11 |
| 0x19 | 0x06 | 0x07 / 0x08 / 0x0b |
| 0x1c | 0x60 | 0x25 / 0x3d / 0x26 |
| 0x1d | 0xca | 0x86 / 0xc6 / 0xaa |
| 0x1e | 0x73 | 0xa1 / 0xb3 / 0x85 |
| 0x20 | 0x00 | 0x40 / 0x40 / 0x60 |
| 0x23 | 0x00 | 0x40 / 0x43 / 0x40 |

The profile also lists 0xf0-0xf4 and 0xf6, all zero, which our table does not
send. No meaning is known for any of these registers (the datasheet does not
publish the map); per-colour values are the first suspect for the blue cast and
the ghost twin. `tools/icnd1065_factory_regs.sh` loads the table into a running
card (`REG02=0x7f` to take bit 6 as well); `tools/icnd1065_live.sh` puts the
old one back. Loaded live 2026-11-15 with 0x02 left at 0x3f: 0 underruns/s,
84 Hz, unchanged. **Not yet judged on glass, and not yet in the chip tables.**

The table does not have to go into the chip tables at all. Firmware 5.6.0
takes per-module registers from the boot config (`chip_reg:` lines), and
`tools/rcvbp_import.py` writes a marquee module type carrying them straight
from the profile:

    tools/rcvbp_import.py factory/myled-COB-p1.25-640x320-1065L.rcvbp \
        --key myled-p1.25-factory --preset 128x256-icnd1065l-hub320 \
        --clk-div 4 --write-throttle 0 --packet-delay 0.0006 -o myled-p1.25-factory.json

Checked on the bench card 2026-11-15 (firmware 5.6.0 netbooted, never
flashed): six `chip_reg:` lines posted to `/api/layout` -- 0x0c, 0x0d, 0x03,
0xf0, 0x0c again and one malformed -- read back as `chip_regs: 4,
chip_regs_matched: 3` (the repeat merged, the malformed line dropped, 0xf0 not
in the table), with 0 underruns. The effect of the values themselves is still unseen.

Other fields: gamma 2.8, 3840 Hz refresh, 37 GCLK per scan, blanking 12..125,
prefixCommand 14, dataLatching 1, globalLatching 3, driverChipId 146,
decodeChipId 3.

### A power plug as the light meter (2026-11-16)

With nobody at the bench, the mains plug the bench hangs on is the only
instrument that sees light: the LED rail is constant-current, so watts above a
black frame follow what is lit. `tools/plug_patterns.py` holds a list of
patterns on the card and logs when each began; the plug's history is matched
to the log afterwards.

It is coarse. The plug reports only a change of roughly 1.5 W, at once, and
the first report can be a half-way value, so every pattern is preceded by
black and repeated, and the highest reading of a rising step is taken. Good
for "is it lit, is every colour and every half there, did the brightness
move"; useless for anything under about 0.3 W.

Myled P1.25, gateware 3.5.0, our register table, watts above black (12.7 W):

| Pattern | W | | Pattern | W |
|---|---|---|---|---|
| white | 14.8 | | red + green + blue, measured apart | 15.8 |
| red | 6.9 | | yellow | 12.1 (red + green: 12.6) |
| green | 5.7 | | cyan | 8.6 (green + blue: 8.9) |
| blue | 3.2 | | magenta | 9.8 (red + blue: 10.1) |
| top half, solid | 7.8 | | every other row / every other column | 7.7 / 7.8 |

* **No blue where there should be none.** Yellow draws slightly LESS than red
  plus green, like every other mix (the supply is a little more efficient
  loaded). Blue leaking into yellow at a level that turns it cream would add
  the better part of a watt. The cast in the photographs is most likely the
  camera clipping on saturated LEDs.
* **The ghost twin carries under about 3% of the light.** Combs with a dark
  line beside every lit one draw what a solid half draws. That bounds it; it
  does not clear it -- the eye sees far less than 3%.

#### The vendor's registers at the plug

Loaded live over our table (`tools/icnd1065_factory_regs.sh`), watts above
that table's own black:

| Table | White | Red | Green | Blue | Black frame |
|---|---|---|---|---|---|
| ours | 14.8 | 6.9 | 5.7 | 3.2 | 12.7 W |
| vendor, all 13 | 7.6 | ~2.7 | 3.7 | ~1.2 | 13.4 W |
| vendor without 0x03 and 0x04 | 9.3 | 3.5 | 4.6 | ~1.5 | 13.3 W |

(~ marks a colour too small for the plug to report, taken from the mixes.)

* **The per-colour registers are a white balance.** Against green, our table
  drives red about 65% harder and blue about 75% harder than the vendor does
  (red/green 1.21 against 0.74, blue/green 0.56 against 0.32). If the picture
  has a cast with our table, this is the first cause to believe.
* **It costs brightness**: 63% of ours without 0x03/0x04, 51% with them.
* **Black draws 0.6 W more** with the vendor's values, arriving with
  0x1c-0x1e / 0x20 / 0x23. Either the chips' own current (de-ghosting
  circuits would do that) or LEDs glowing at black. The plug cannot tell;
  look at a black frame.

The bench card boots with the vendor's values less 0x02-0x04, as `chip_reg:`
lines (`exp/bench-myled.cfg`). Posting a
layout without those lines puts our table back.

### Ghosting

A close-up (white line and cyan diagonal) shows **every lit pixel with a
dimmer twin one position over**, always on the same side. A one-position
timing offset -- the data latch commit (`sweep_adv`, `latch_phase`) or row
blanking (`row_ser.bk_w`). Not yet chased.

### Incidents

* The card hung once, mid-session, after `grid` was shown: off the network
  entirely (no ARP), panel dark. A power cycle brought it back on 3.1.0/5.1.0
  with the live settings lost; the script restored them. Cause unknown.

### Open

* **Done:** the working values are in the `icnd1065` entry of both chip tables
  (`gateware/spwm_chips.py`; `spwm_tables.rs` is generated from it), and
  marquee has the module type, so the setup survives a reboot.
* **Done, on glass 2026-11-18** (gateware 3.6.2 / firmware 5.8.0): the test
  grid upright and whole at `rotate: 90`. `din_row` is 1 for this chip: at 0
  each strip showed its last scan line first, which the grid only gave away
  once its edge line sat on the module's edge (two lines at one side, yellow
  outside magenta, none at the other; `/api/fbdump` had the buffer right).
* **Done 2026-11-18:** the vendor's per-colour registers are in force as
  `chip_reg:` lines (a white balance, about 63% of our table's brightness),
  and two modules run side by side on one card.
* The ghost twin (`sweep_adv`, `latch_phase`, `bk_w`): seen in one early
  close-up, not looked for since `row_din` and the vendor registers went in.
* Register 0x02 bit 6 (factory 0x7f against our 0x3f): meaning unknown, not
  tried on glass.
* 84 Hz (`clk_div` 3) measured clean once; not adopted as the default.
* `sweep_adv` / `chan_xor` at pixel level (both 0, unchecked).
* How the profiles' data-group output numbers map to connectors and groups.
* One bitstream for both P1.25 modules: both now run on runtime-geometry
  builds (5.8.3, docs/RUNTIME-GEOMETRY.md), but each on its own preset, since
  the preset still carries the chip table and group positions the card wakes
  up with. One image that takes both from the boot config is not built.
* Read and apply the per-module calibration from the on-module flash.
