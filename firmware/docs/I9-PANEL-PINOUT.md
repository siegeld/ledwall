# The Colorlight i9's panel connector is already fully mapped

Derived 2026-10-22. **No probing was needed.** Two public documents,
cross-referenced, give a ball-level HUB pinout for the `LFE5U-45F` on a
Colorlight i9 v7.x:

1. **Colorlight's own `i9 Specification V1.4.1`**
   (`support.colorlightinside.com`) names all 200 pins of **J28, the
   "Connecting finger"**.
2. **`wuxx/Colorlight-FPGA-Projects`, `colorlight_i9_v7.x.md`** traces the same
   200 pins to FPGA balls -- in a section headed **"DDR2-SODIMM-200P"**.

## The section called "DDR2" is the panel connector

That heading is why this sat unnoticed. J28 is physically a **DDR2-200 SODIMM
gold-finger edge**, so a reverse-engineer with no vendor document in hand
labels it by its footprint. It carries no DRAM. It carries the LED panel:
**32 RGB groups, five address lines `FA..FE`, `LED_SCLK`, `LED_LAT`,
`LED_OE`**, the two gigabit MDI pairs, and panel housekeeping.

This is the i9's equivalent of our HUB320 -- what a 5A-75E spreads over
sixteen `J1..J16` headers, the i9 puts on one card edge for a cabinet HUB board
to fan out.

## Why the cross-reference is trustworthy

The two documents were produced independently and **agree on which pins are
live**: over the 186 pins both describe, **159 agree**, and every one of the
27 that do not fails *in the same direction* -- Colorlight names a signal
where wuxx records `NC`. **wuxx never assigns a ball to a pin Colorlight leaves
blank** (asserted in the generator, not eyeballed). The ground runs line up
exactly too -- 55/56, 105-108, 157/158 -- as do the `D5V` rails and all eight
`D1A±..D1D±` / `D2A±..D2D±` MDI pairs.

A mis-aligned table cannot produce that pattern. An off-by-two would put balls
on blank pins; an off-by-one would swap the odd/even columns and scatter the
`NC`s. Neither happens.

## RGB groups -> ECP5 balls

| grp | R | G | B | | grp | R | G | B |
|--:|---|---|---|---|--:|---|---|---|
| 1 | `T18` | `R18` | `R17` | | 17 | `E19` | `D20` | `D19` |
| 2 | `N2` | `N3` | `T2` | | 18 | `E17` | `D18` | `D17` |
| 3 | `P17` | `M17` | `T17` | | 19 | `C20` | `B20` | `B19` |
| 4 | `M3` | `T3` | `R3` | | 20 | `G5` | `D16` | `F5` |
| 5 | `U18` | `U17` | `P18` | | 21 | `B18` | `A19` | `C17` |
| 6 | `N4` | `M4` | `L4` | | 22 | `E6` | `E5` | `F4` |
| 7 | `N17` | `N18` | `M18` | | 23 | `A18` | `D3` | `C4` |
| 8 | `L5` | `P16` | `J16` | | 24 | `E4` | `F1` | `F3` |
| 9 | `L20` | `L18` | `K20` | | 25 | `B4` | `C3` | `E3` |
| 10 | `J18` | `J17` | `H18` | | 26 | `G3` | `H3` | `H4` |
| 11 | `K19` | `J20` | `J19` | | 27 | `A3` | `C2` | `B1` |
| 12 | `H17` | `G18` | `H16` | | 28 | `H5` | `J4` | `J5` |
| 13 | `H20` | `G20` | `G19` | | 29 | `C1` | `D2` | `D1` |
| 14 | `F18` | `G16` | `E18` | | 30 | `K3` | `K4` | `K5` |
| 15 | `F20` | `F19` | `E20` | | 31 | `E2` | `E1` | `F2` |
| 16 | `F17` | `F16` | `E16` | | 32 | `B3` | `A2` | `B2` |

## Control and address

| signal | J28 pin | ball |
|---|---|---|
| `LED_BTN_IND` | 41 | `L2` |
| `FA` | 42 | `R1` |
| `FB` | 44 | `T1` |
| `FC` | 46 | `U1` |
| `FD` | 48 | `Y2` |
| `LED_CTRL` | 49 | `K18` |
| `FE` | 50 | `W1` |
| `LED_SCLK` | 51 | `C18` |
| `LED_LAT` | 52 | `V1` |
| `LED_OE` | 54 | `M1` |

`FA..FE` are the row-address lines. `LED_CTRL` and `LED_BTN_IND` are housekeeping, not pixel path.

The eight `D1A±..D1D±` / `D2A±..D2D±` MDI pairs (pins 15-36) are
**deliberately absent** from this table: they run to the two Broadcom
B50612D PHYs, not to FPGA balls, and wuxx labels them `ETH1_*`/`ETH2_*`
by function. Their agreement with Colorlight's names is corroboration for
the alignment, not a pinout we need.

## What is NOT mapped -- and the one thing to check before committing

wuxx records `NC` for every pin from 159 to 198, which is exactly where
Colorlight puts the **eight SPI driver-configuration channels** and the analog
monitoring:

`LED_TEMP` (43), `LED_HUM` (45), `LED_FAN` (47), `LED_SPI_SCK` (159), `LED_SPI_SDI` (160), `LED_SPI_CS1` (161), `LED_SPI_CS2` (162), `LED_SPI_SDO1` (163), `LED_SPI_SDO2` (164), `LED_SPI_CS3` (165), `LED_SPI_CS4` (166), `LED_SPI_SDO3` (167), `LED_SPI_SDO4` (168), `LED_SPI_CS5` (169), `LED_SPI_CS6` (170), `LED_SPI_SDO5` (171), `LED_SPI_SDO6` (172), `LED_SPI_CS7` (173), `LED_SPI_CS8` (174), `LED_SPI_SDO7` (175), `LED_SPI_SDO8` (176), `Power_M1` (177), `Power_M2` (179), `Power_M3` (181), `Power_M4` (183), `Power_M5` (185)

**Do not read that block of `NC`s as proof those pins are unconnected.** A
shipping receiver card has to configure its driver chips, and a clean 40-pin
run of `NC` looks far more like "not traced" than "not fitted". It matters to
us specifically: the DP3264S on our Linsn P1.25 is an S-PWM part that needs
exactly this kind of configuration channel. Before any i9 work is costed,
**continuity-check pins 159-176 to the FPGA on a real board** -- a meter and
twenty minutes, and the only open question in this pinout.

The analog monitors (`LED_TEMP`, `LED_HUM`, `LED_FAN`, `Power_M1..5`) are
genuinely uninteresting to us and plausibly really are off-FPGA.

## What this changes

Earlier notes here recorded that the i9 "has no panel-connector section at
all" and that our HUB320 map had no prior art to check against. **The first
half was wrong** -- the panel pinout is published twice over, once by the
vendor and once by a reverse-engineer who did not know what he had found.
`docs/SCALING.md` was corrected on the same date.

The second half still stands: nobody publishes a **HUB320** pinout, and the
i9's edge exposes raw RGB groups rather than HUB320 or HUB75 footprints, so the
physical carrier remains a board someone has to make.

## The adapter question

**Nobody has published an open HUB320 or HUB75 adapter for the i9.** What
exists:

* **Commercial HUB boards are an ordinary product category.** The whole
  i-series and K-series exist *because* the fan-out board is a separate
  purchase. Generic "HUB75E-8" adapters are advertised for "Novastar Armor,
  Linsn Mini and Colorlight i-Receiving Cards" -- roughly $10-50 for eight
  HUB75E ports with `A..E` address lines. **Confirm the mating connector with
  the seller before buying**: "i-Receiving Card" is a product-family label, not
  a connector spec, and we need the DDR2-200 edge specifically.
* **wuxx sells an "Ext-Board"** for the i5 and i9 (`i9_v7.2_pinout.png`). It is
  a general GPIO breakout, not a panel fan-out.
* For **HUB320** there is nothing, commercial or open. It is an E320/5A-75E
  family footprint; an i9 carrier exposing HUB320 would be our own design. With
  the pinout above that is a passive two-layer board -- no logic, no level
  shifting, 32 groups of three signals plus eight shared controls.

## The K-series is a different story

`Colorlight K9 Plus Specification V3.1.1` publishes its signal names too, on
**two `Header2x60` mezzanine connectors `JH1`/`JH2`** (240 pins): `R1..R32`,
`G`/`B` likewise, `FA..FE`, `DCLK`, `DCLK2`, `FLAT`, `FCTRL`, separate
`OE_RED`/`OE_GREEN`/`OE_BLUE`, the MDI pairs, and `EXT_LCD_*`. **But no one has
traced those pins to balls**, there is no community work on any K-series board,
and the FPGA is a 484-ball Xilinx. The i9 trick does not transfer -- half the
cross-reference does not exist.

See `docs/CARDS.md` for the cards we actually support and `docs/SCALING.md` for
whether any of this is worth doing.
