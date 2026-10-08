# Finding an unknown card's connector pinout with a multimeter

A receiver card's panel connectors are wired to FPGA balls that no datasheet
documents. The 5A-75E's map exists only because the chubby75 project reverse
engineered it. The **E320's HUB320 map is not published anywhere**, and the
trick that recovered its SDRAM, flash and Ethernet pins — listing PIO sites
with `IOLOGIC` configured in the factory bitstream — cannot reach the panel
data lines, because those are combinational and leave no IOLOGIC behind.

So it has to be measured. `gateware/pinwalk.py` does it in one pass.

## The idea

Every candidate ball is driven simultaneously with a square wave at its **own**
frequency. Touch a connector pin with a multimeter in Hz mode and the reading
names the ball:

```
index  = (reading_hz - 1000) / 50
ball   = look up index in build/pinwalk/pinwalk-map.csv
```

No CPU, no firmware, no network, no host in the loop. One bitstream, one meter,
one pass over the connector. 108 balls, 1000 Hz to 6350 Hz in 50 Hz steps.

The frequencies are exact integer divides of the 25 MHz input, so a reading
that is not within a few Hz of a table entry is not a mis-read — it is a
floating pin, a ground, or a power rail.

## What it will not tell you

It identifies **which ball** a connector pin is, not **what the signal means**.
That is the right split: the ball is the hard half and needs the FPGA; the
meaning is the easy half and comes from the panel's HUB pinout. HUB320 is 26
pins carrying four RGB groups (`RD1`–`RD4`, `GD1`–`GD4`, `BD1`–`BD4`), A–E,
clock, latch, output enable and grounds.

## Safety: what is deliberately never driven

Driving an output against an output is how boards die, so the walker excludes:

| Excluded | Why |
|---|---|
| Everything in the 5A-75E platform map — SDRAM, Ethernet, SPI flash, `clk25`, serial | the board drives or shares these: the PHY's receive pins, flash MISO, the oscillator, the shared SDRAM bus |
| **All of bank 8** | sysCONFIG and JTAG. Driving these does not just risk the board, it risks the only recovery path there is |

Everything else is safe on these cards because the remaining balls feed
**74HC245 buffer inputs** — the E320 carries twenty of them, in two rows of
ten, on the underside.

The exclusion list is derived at build time from the platform's own IO map
rather than hand-written, so it cannot drift out of step with the platform
file. Verify it after any platform change:

```bash
python3 gateware/pinwalk.py --list        # candidates, with frequencies
```

## Procedure

```bash
./build.sh pinwalk                        # build + flash the walker
```

Then, for each connector in turn:

1. Meter in **frequency (Hz)** mode, black lead on a connector ground pin.
2. Touch each pin of the connector in order; write down the reading.
3. Convert: `index = (hz - 1000) / 50`, then look the index up in
   `build/pinwalk/pinwalk-map.csv`.
4. A pin reading 0 Hz, DC, or a frequency not in the table is ground, a rail,
   or not connected to the FPGA — record it as such, it is still information.

Work one connector at a time and record ALL 26 pins before moving on. A partial
map of eight connectors is much less useful than a complete map of one, because
the groups repeat and the pattern is what lets you generalise.

## Worked example: the E320, mapped in about twenty probes

The first real use, and the shape of it is worth copying.

**Guess the structure before probing.** The arithmetic said an E320 port might
be two 5A-75E connectors merged: 8 ports x 12 data lines is 96, and the
5A-75E's `j1`..`j16` provide exactly 96 data balls. That is a hypothesis with
teeth, because it predicts every pin.

**Then probe to falsify it, not to confirm it.** The decisive reading was J3
pin 9 — the first data line of the *second* merged half, and the only pin
whose value distinguishes the merge from a plain HUB75 layout. It landed on
prediction. Pins 1-5 would have matched either way and proved nothing.

**Predict, then read, and announce the prediction first.** Roughly twenty
probes mapped all eight ports, because most readings were confirmations of a
model rather than discoveries. The two that did not match were the valuable
ones: pin 22 was NC where `clk` was predicted, which revealed the ground
between the address and control blocks.

**Read a run where frequencies are adjacent.** `F3`/`F4`/`F5` are neighbours
in both the frequency plan and the connector. A single pin reading 2.50 rather
than 2.45 was equally explained by an off-by-one probe, by rounding, or by a
real layout difference. Pins 9, 10, 11 together gave 2.45 / 2.55 / 2.95 and
settled it at once; an off-by-one would have shown 2.50 / NC / 2.45, with a
ground in the middle. The shape disambiguates where a value cannot.

**Check the board, not the datasheet.** A vendor page for "the E320"
advertises eight HUB320 interfaces; a passing report that the card had only
four was enough to make the table briefly wrong in both directions. The
silkscreen is the authority. The platform self-check now asserts
`ports x 12 == distinct data balls` so a wrong port count fails loudly.

Result: `gateware/colorlight_e320.py`.

## Afterwards

Nothing here touches flash outside the bitstream region, so reflashing the
normal gateware restores the card:

```bash
./build.sh --panel 128x64 flash
```

Turn the result into a `_connectors_*` table entry in a platform file, in the
same shape as `colorlight_5a_75e.py` uses, and the existing output stages can
be pointed at it. Note that `helper.hub75_conn()` currently takes only two RGB
groups per connector; four-group HUB320 width lives in the `icn1065` stage
(`groups=`), not in `hub75.py`.

## Why not walk one pin at a time

The obvious design — drive one ball, probe, advance — needs a host in the loop
and about 115 round trips per connector pin, and every probe touch has to be
synchronised with whatever the host is doing. Driving all of them at once with
distinct frequencies removes the host, the synchronisation and the round trips.
The cost is 108 small dividers, which on an LFE5U-25 is a rounding error.
