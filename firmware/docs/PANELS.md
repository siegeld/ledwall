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
| Driver ICs | MBI5124, ICN2038S and similar | Chipone **ICND1065**, Depuw **DP3264S**, NovaStar **DP3364S** |
| Who makes the greyscale | **the card**, as bit planes / BCM + a gamma LUT in fabric | **the chip**, from a 12-bit word per LED |
| Row stepping | parallel `A`–`E` address lines from the card | **`OE` pulses step a counter inside the chip**; VSYNC resets it |
| Start-up | none | **38 config registers**, written once per frame after a `PRE_ACT` unlock of three `LAT` pulses |
| Typical advertised specs | 8-bit grey, refresh to ~2 kHz | **13–16 bit grey, refresh above ~3 kHz** |
| Connector here | HUB75 / HUB75E, 16-pin | HUB320, 26-pin (also HUB75E on other modules) |
| Status | **proven**, in daily use | **proven**, in daily use — see the module below |

**The "advertised specs" row is the quick test when reading a spec sheet.** Anything
advertising 13–16 bit greyscale or refresh above about 3 kHz is almost
certainly S-PWM. A card with only five address lines can still drive 1/64
scan, because on an S-PWM module the row addressing happens *inside* the
driver IC.

## What to buy, if you want the thing we know works

The only module this project has lit under the S-PWM stage is the one in the
record below: **a Linsn P1.25 320 × 160 mm, `XP1.25-5958S-256X128-64S`,
256 × 128 px, 1/64 scan, HUB320, Depuw `DP3264S` column driver.** **Two** of
them run on one Colorlight E320 as a 256 × 256 wall, streaming video at
20 fps — that is what is on the bench and measured, not a projection.

If you are buying new, that is the lowest-risk order. If you are buying
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
   on **pins 20 and 21**. Our E320 drives both; some modules put a serial bus
   there. **Read the silkscreen before plugging a new module into a card.**
4. **The RGB group arrangement.** How many groups, how wide each chain is, and
   where each group lands on the glass. The module below is four 128 × 64
   quadrants in a 2 × 2 — which is not what its connector or its "256 × 128,
   1/64" label suggests, and getting it wrong gives a sawtooth or a blank half
   rather than an error.
5. **The receiver profile** (`.rcvbp` / `.rcfgx`), and a schematic if they
   will part with one. The vendor profile is how we recovered the factory
   register values; `tools/decode_rcvbp.py` reads it.

### What will not work

* **Panels with an on-module receiver you cannot bypass.** This stack *is* the
  receiver.
* **Any module whose driver is neither family above.** A third protocol means
  a third output stage: `gateware/icn1065.py` is roughly what that costs.
* **Per-pixel flash calibration is not applied.** Modules store brightness and
  chroma correction that a stock receiver reads and uses; we do not (open item
  below). Invisible on one module; across a multi-module wall it is what keeps
  them looking like one display. Buy modules from **one batch** if you can.

### Geometry presets

`gateware/colorlight.py` `PANELS` is the list, and it is the source of truth:
`128x64`, `256x64`, `96x48`, `64x32`, `64x64` for shift-register panels, and
the `*-icnd1065-*` / `*-dp3264s-*` family for S-PWM, with variants trading
shift clock, colour depth (32-bit or RGB565) and gamma against refresh rate
and FPGA timing. Per card you also set channel order, scan rate, chain length,
rotation and position. `256x128-dp3264s-hub320` is the preset for the module
below.

---

## Who makes what

Four different companies have to agree before a module works, and they are
rarely the company you bought from. Knowing which is which is what lets you ask
the right question of the right party.

### Driver ICs — this is the part that decides compatibility

| Vendor | Part | Family | Our status |
|---|---|---|---|
| **Depuw** | `DP3264S` | S-PWM, 16-channel | **proven on glass.** `gateware/spwm_chips.py` carries the factory register table; datasheet in `docs/datasheets/` |
| **Chipone** | `ICND1065` / `ICND1065S` | S-PWM | table transcribed from the datasheet, **no module connected** |
| **NovaStar** | `DP3364S` | S-PWM | **a different part from the DP3264S** — 15 registers, 6 LE commands. Table unverified. Two supplier sheets called our DP3264S module this, and believing them cost a day |
| **Chipone** | `ICN2038S` | shift register | works under `hub75` |
| **Macroblock** | `MBI5124` | shift register | works under `hub75` |
| **Raffar / HX** | `RT5958D`, `HX6158HSP`, `TC7559` | 595-type **row** shift register | the row half of our P1.25 module. Not a 138 decoder — different scan-out |

Chipone is also written "ICN"/"ICND" inconsistently across sheets, which is how
`ICN1065L`, `ICND1065S` and `ICN1065` all end up naming the same family while
`DP3364S` and `DP3264S` differ by one digit and are not interchangeable.

### Module makers

| Company | Site | What |
|---|---|---|
| **Shenzhen Linsn LED Co., Ltd** | linsn-led.com / linsnled.com | the P1.25 320×160 mm module below. **Not** Linsn Technology, who make sender cards — same brand name, different company |
| **Shenzhen Myled Optech** | smyled.com | supplier of the `ICND1065S` modules on order (RB-5504) |

### Receiver cards and resellers

* **Colorlight** makes every card this repo targets — the 5A-75E and the E320.
  See `CARDS.md`. They also write the stock firmware and the LEDVISION /
  LEDSetting profiles in `factory/`, which is where our register values came
  from.
* **ARGO** resold us the P1.25 module alongside an E320, a K5+ and a K9+.

**Resellers write no firmware and usually cannot answer any of the five
questions above.** That is not a complaint — it is a planning fact. Every
technical answer we have came from one of three places: the part marking on the
chip, the vendor's own `.rcvbp` profile, or a datasheet. **None came from a
reseller, and none came from the manufacturer either.** Linsn's contact page
says they support direct customers only, and our enquiries went unanswered.

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

## Linsn P1.25 320x160 -- `XP1.25-5958S-256X128-64S-1010-B1`

The module on the bench since 2026-10-15. The first S-PWM module this project
has lit, and the one `docs/E320-BRINGUP.md` is about.

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
| Maker | **Shenzhen Linsn LED Co., Ltd** (probable; linsn-led.com / linsnled.com -- not Linsn Technology the sender-card maker). Identified by matching the PCB back to their 320x160 P1.25 module. Their published support and sales addresses were both written to with the five open questions below (group arrangement, PCB channel order, DATA_LATCH commit position, receiver profile, schematic) and neither answered. **Their contact page says they support direct customers only**, which probably explains it -- and the channels they actually staff are WhatsApp and WeChat rather than email, which is worth knowing before you plan on a reply. Unconfirmed |
| Reseller | **ARGO** (contact details withheld -- a small reseller, not a public company). Shipped this module as a "soft-connection test module" with an E320, a K5+ and a K9+. Writes no firmware |
| Receiver firmware | **Stock Colorlight**. `factory/` holds the E320's flash images and the K5+/K9+ builds ARGO forwarded on 2026-10-19 |
| Receiver profile | `factory/CLTp1.25-320-160-3264s-7559-64s-6.rcfgx.rcvbp` -- a stock Colorlight LEDVISION/LEDSetting profile. Decode with `tools/decode_rcvbp.py`. Register table, data-group map `[8, 9, 0, 1, 10, 11, 2, 3]`, pixel map **identity** (records are `(y, x_lo, x_hi)`; framed one byte off it fakes a carry at column 127 -- see `E320-BRINGUP.md` §5c) |
| Not this module | **Shenzhen Myled Optech** (smyled.com) is the supplier of the nine `ICND1065S` modules on order (RB-5504). Different driver, different entry when they arrive |

### How we drive it

| | |
|---|---|
| Card | Colorlight **E320** (ECP5 `LFE5U-25F-6`), built at **35 MHz** -- the -6 part does not close 40 |
| Build | `./build.sh --board e320 --panel 256x128-dp3264s-hub320 --outputs N --sys-clk 35000000 gateware` / `pac` / `firmware`, with `pub const OUTPUTS` in `hub75.rs` set to the same N. **`--outputs` does NOT update that constant — set it by hand, and the failure if you forget is silent.** Timing tightens fast with N: one output closes with margin, three need `--seed 1`. Editing a program's YAML also needs `./build.sh panelc` BEFORE `firmware` -- the firmware build does not regenerate `generated.rs` |
| Known good | **gateware 1.34.0, firmware 3.6.0**, 35 MHz, `clk_div 4`. Two modules streaming video at 20.00 fps; a three-output bitstream closes with `--seed 1` and every budget at its limit (56/56 EBR). Running `railboard` fed by marquee's value pusher |
| Operating point | `clk_div` **4** (8.75 MHz panel clock), `row_slot` **64** panel clocks, **64 display passes**, **13-bit** grey. All four are coupled and none was guessed -- see the derivation below and `E320-BRINGUP.md` 4.6 and 4.10 |
| Geometry | **Four 128 x 64 RGB groups in a 2x2** on one connector: g0 top-right, g1 bottom-right, g2 top-left, g3 bottom-left (glass, 2026-10-17). The gateware places each group at its origin (`group<g>_origin` CSRs, preset defaults `128,0 128,64 0,0 0,64`), so the framebuffer is the glass, 256 wide x 128 tall, and a raw write lands upright. Before gw 1.18 the framebuffer was a 128x256 vertical stack and only the test patterns were remapped (`quad_src_xy`), so streamed content was scrambled. §1a / §4.3 of `E320-BRINGUP.md` |
| marquee layout | card `e320` (id 2), `192.168.1.62`, `panel_width: 256`, `panel_height: 128`, `drive: dp3264s`, `group_origins: 128,0 128,64 0,0 0,64`. With one module fitted: `grid: 1x1`, `J1: 0,0` (legal since gateware 1.20.0, when the stride started following `ctrl.width`; on 1.18/1.19 all four outputs had to be declared, `grid: 4x1`, or the picture folded). Adding modules is a layout edit, no gateware change |
| Chip table | `gateware/spwm_chips.py` `dp3264s`: the **factory register values** from the vendor profile, including **`reg0x03 = 0x3f`** -- see the derivation below. `chan_rev=0` (the glass overrules 11.2's channel-15-first for this wiring; most likely PCB routing). **`sweep_adv=1`** -- the chip commits each `DATA_LATCH` at pointer+1, the cause of the 16-pitch dots (4.6a), and the ONE value still inferred rather than scoped. **`chan_xor=0`** -- the pair swap carried as 1 until 2026-10-19 was a fitted artefact of the pre-1.18 pattern path; on the raw path it put both borders one column in and garbled the version text |

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
| Row chain | ROW free-runs for the whole frame (the mode requires it uninterrupted); A/B/C carry `scan_row >> 3` and the chip's internal counter walks eight rows per address |
| Signals confirmed on a scope | `docs/SCOPE.md`; CLK on pin 23, LAT on 24 |

### Status and open items (current: 2026-10-22)

* Gateware 1.25.0 / firmware 2.95.1: the framebuffer is the glass, the raw
  test screen is flush on all four edges with legible version text; `sweep_adv`
  (1) and `chan_xor` (0) are written at boot by `spwm::configure()`.
* **Closed 2026-10-19:** `chan_xor` is 0. The pair swap was an artefact of
  the compensated pattern path, not a property of the module or the chip.
* **Open:** the pointer+1 commit is established by elimination and
  simulation; seeing it on LE (pin 24) against the data would close it.
* **Open:** Linsn's reply -- schematic, HUB320 sheet, and whether the profile
  above is theirs or ARGO's.
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
