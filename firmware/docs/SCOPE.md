# Probing a HUB320 panel connector

Written 2026-10-17, when the P1.25 module had gone from dark to lit-but-wrong
and every remaining question was one a scope answers in minutes and an eyeball
cannot. The symptom at the time: pixels arrive **sparse**, with a **left-to-right
density gradient**, in **colours unrelated to the source**, while the framebuffer
reads back solid `ff`.

All numbers below are for the configuration on the bench — E320, gateware
`e320-256x256-icnd1065-hub320-out8-v1.8.0`, `clk_div = 4`. If you change
`/api/clkdiv`, every time scales with it.

## Where to put the probes

The module's own HUB320 header, 26 pins, 2x13. Pin 1 is the arrow/square pad;
odd pins are the row nearest it. Transcribed from this module's silkscreen and
confirmed against the card — see `docs/HUB320.md`.

| Pin | Signal | Why probe it |
|---|---|---|
| **23** | CLK | Is the shift clock clean at 8.75 MHz? |
| **24** | LAT / LE | Command widths and the per-sweep data latch |
| **25** | OE / ROW | **Is ROW continuous?** The fix this session turned on |
| **1** | R1 | Is the data what we think we are sending? |
| **2** | G1 | The per-lane question |
| **17 / 18 / 19** | A / B / C | The serial row chain: LCK, BK, DIN |
| **22 or 26** | GND | Ground the probe HERE, not at the PSU |

**Ground at pin 22 or 26.** These are the grounds adjacent to the control block.
A long ground lead to the supply will add ringing that looks exactly like the
signal-integrity fault you are trying to rule in or out, and that artefact has
cost this project time before.

## The numbers to expect

At `clk_div = 4` on a 35 MHz system clock:

| Quantity | Value |
|---|---|
| Panel CLK | **8.75 MHz**, period **114.3 ns** |
| Row slot (`ROW_OE_LEN` 128 clocks) | **14.63 us** -> row rate **68.4 kHz** |
| Full scan pass (64 rows) | **936 us** -> **1068 Hz** |
| Data frame (256 x 64 x 16 clocks) | **29.96 ms** -> **33.4 Hz** |
| One row of pixel data (16 sweeps x 256 bits) | **468 us** |
| `DATA_LATCH` | 1 clock = **114 ns**, **16 per row** |
| ROW pulse W12 / W4 | **1.37 us** / **457 ns** |

Command LE widths, in CLK cycles (DP3264S 11.1): `DATA_LATCH` 1, `VSYNC` 3,
`WR_CFG` 5, `PRE_ACT` 14. This gateware also emits an 11-clock `PRE_ACT_MID` for
chips that want it; `dp3264s` does not (`mid_latch=False`).

## The five measurements, in order of what they settle

### 1. Is ROW continuous? (pin 25)

The headline fix of 2026-10-17. Trigger on pin 25 rising, timebase 2 ms/div, and
look at the whole sweep.

* **Good**: a pulse every **14.63 us**, forever, with no gaps. One pulse in every
  64 is **1.37 us** wide (W12); the rest are **457 ns** (W4).
* **Bad**: a burst of 64 pulses followed by ~30 ms of silence. That is the
  pre-1.8.0 behaviour and means the fix did not take -- check
  `/api/gateware` really says `v1.8.0`.

This is verifiable from the bench and proves on glass what only simulation has
shown so far.

### 2. Is the data what we think? (pins 1 and 2) -- THE HIGH-VALUE ONE

Set a known pattern first:

```
curl -X POST http://192.168.1.62/api/display/pattern -d '{"name":"red"}'
```

Solid red means every pixel is `0x0FFF` on R and `0x0000` on G and B.

* **Good**: R1 (pin 1) is **high for 12 of every 16 clocks** and low for 4 -- the
  greyscale value is 12 bits in a 16-bit word. G1 (pin 2) is **low throughout**
  the data phase.
* **Bad, and diagnostic**:
  * R1 and G1 look the **same** -> the serialiser is putting the same data on
    every lane, and the per-colour split is broken.
  * R1 is **sparse or random** rather than 12-high-4-low -> the data leaving the
    FPGA is already wrong, upstream of the panel entirely. The framebuffer reads
    back solid `ff`, so the fault would be in the gamma, the arbiter or the
    serialiser.
  * R1 is **correct at the connector** -> the data is leaving correctly and is
    being lost in the chain. Then the gradient is a chain problem: go to (5).

Nothing else on this list distinguishes "we send garbage" from "we send fine and
it gets lost", and those have completely different fixes.

### 3. Is the clock clean? (pin 23)

Trigger on CLK, timebase 50 ns/div, and look at the edges.

* **Good**: full-swing square wave, 114 ns period, monotonic edges, flat tops.
* **Bad**: amplitude well under the logic rail, slow edges, or ringing/overshoot
  that crosses back through threshold. Any of those sample randomly, and random
  sampling on a uniform field is exactly a scatter of lit dots.
* Measure amplitude **and** rise time. A meter reading 2.3 V on this pin (as an
  earlier session recorded) is a time-average of a square wave and says nothing
  about edge quality -- which is the whole point of using a scope here.

### 4. Does LAT land where we think? (pin 24)

Trigger on pin 24 with a **pulse-width trigger > 1 us** to catch `PRE_ACT`
(14 clocks = 1.6 us) and ignore the 114 ns data latches.

* **Good**: once per data frame (~30 ms) a prefix burst -- `VSYNC` 343 ns,
  `PRE_ACT` 1.6 us, `WR_CFG` 571 ns -- then nothing but **16 narrow 114 ns
  pulses per row**, 468 us apart.
* **Bad**: data latches of the wrong width (a 5-clock pulse where a 1-clock was
  meant is a `WR_CFG`, not a `DATA_LATCH`, and would write registers where
  pixels were intended), or a latch count per row that is not 16.

Count the narrow pulses in one row. Sixteen is right; anything else is a
sweep-indexing bug.

### 5. Is the row chain shifting? (pins 17, 18, 19)

Current mapping (`/api/status` -> `row_ser`): **17 = LCK, 18 = BK, 19 = DIN**,
and it is changeable live with `/api/rowser`.

Trigger on pin 19 (DIN) rising -- it should fire **once per scan pass, 936 us**,
because a single 1 is injected on the wrap to row 0 and then walked.

* **Good**: DIN pulses once per 936 us. LCK pulses **once per row**, 14.63 us
  apart, ~229 ns wide, rising **114 ns after** DIN is stable (`lck_dly = 1`).
  BK brackets each LCK, ~457 ns wide.
* **Bad**: LCK not pulsing -> the chain never shifts and the mapping is wrong;
  try the permutations through `/api/rowser`. DIN pulsing every row rather than
  every 64 -> polarity inverted, 63 ones in the chain instead of one.

### 6. If (2) says the data leaves correctly: find where it dies

The gradient says loss is **positional along the chain**. The module is 16
cascaded driver ICs per RGB group. If you can reach a chip's `SDO` (pin 22 of a
QSOP24 DP3264S, which is `SDO`) partway along the row, compare it against the
connector's R1:

* Data present and clean at the input, degraded or absent further along ->
  signal integrity or clock skew down the chain. That is a drive/termination
  question, not a protocol one.
* Data clean all the way along -> the chips are receiving it and not displaying
  it, which puts the fault back in the register configuration.

## Starvation: the one test that is not a scope measurement

If fine detail shows sparse wrong-coloured pixels, **sweep `/api/clkdiv`
before reaching for a probe**:

```
POST /api/clkdiv {"n": 6}     # 5.83 MHz -- slow
POST /api/clkdiv {"n": 2}     # 17.5 MHz -- fast
```

* Wrong pixels **disappear as the clock slows** -> framebuffer read starvation.
  The pixel source's `held` register is reusing the previous pixel because the
  read did not land in time. Measured 2026-10-17: dirty at n=2, clean at n=6.
* Wrong pixels **unchanged** -> a mapping or protocol fault. Now use the scope.

A mapping error is invariant to the clock and starvation is not, so this
separates the two classes in seconds with no rebuild and no power cycle. It
is the cheapest discriminator on this list and belongs before every other
entry. Note that a SOLID-colour pattern cannot show this fault at all -- every
pixel is the same colour, so a stale one is invisible.

## Gotchas specific to this board

* **`refresh_count` is not evidence of output.** `CLAUDE.md` says so explicitly:
  it counts the scan-out state machine inside the FPGA and increments happily
  with nothing attached and every pin on the wrong ball.
* **Turning writes off freezes the picture and stops `refresh_count`** -- that is
  expected since 1.8.0, not a hang. The row scan keeps free-running, so the panel
  still displays; `frame_done` just stops firing. Use it deliberately: a frozen
  frame is far easier to photograph and reason about than moving snow.
* **The card answers HTTP but not ICMP.** `ping` failing means nothing.
* The crash breadcrumb (`prev_mcause`, `prev_mepc`, `prev_mark`) survives a soft
  reset but **not a power cycle**. If the card wedges, read `/api/status` BEFORE
  pulling power, or the evidence is gone.

## Live knobs, so a scope session needs no rebuilds

```
POST /api/clkdiv   {"n": 4}                 panel clock = 35 MHz / n, n >= 2
POST /api/colshift {"n": 15}                slide the surviving column window
POST /api/chipcfg  {"row_pulse": 4, ...}    row/scan half of chip_cfg
POST /api/rowser   {"a_src":0,"inv":7,...}  DIN/LCK/BK mapping and polarity
POST /api/display/pattern {"name":"red"}    red|green|blue|white|grid|rainbow
POST /api/display/off                       freeze: stop writes, keep scanning
GET  /api/status                            every value above, read back
```

## Related

* `docs/HUB320.md` -- the connector, all three pinout variants, the 20/21 hazard.
* `docs/ICN1065.md` -- the DP3264S protocol, and the ROW-continuity finding.
* RB-5504.
