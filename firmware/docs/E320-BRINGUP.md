# Bringing up a Colorlight E320 and an S-PWM fine-pitch panel

A complete, honest account of driving a **Colorlight E320** receiver card and a
**P1.25 256x128 S-PWM module** from open-source gateware — no vendor sender, no
LEDVISION, no NovaStar. As far as we can establish this is the first public
description of either part being driven this way.

It is written as the document we wanted and could not find. Everything here was
measured on hardware. Where we inferred something and the panel disagreed, the
panel wins and the inference is recorded as wrong, because the wrong turns cost
more time than the right answers did.

---

## 0. State of play

Updated 2026-10-20. Read this first; it is what the rest of the document is
evidence for.

**STREAMING WORKS, on the wire (gateware 1.26.0, firmware 2.97.0).** Measured
on card 2 at 256x128 RGB888: **73 frames per second presented**, 732 of 736 offered, zero
dropped, and a whole-framebuffer read-back correct at every consecutive
address. The per-card `packet_delay` is what sets it -- 0.0008 gives 18 fps,
0.0004 gives 37, **0.0002 gives 73** with 0.5% of frames partial, and 0.0001
overruns the DRAM write path (22% partial). marquee card 2 is set to 0.0002.
The show's own `display.fps` is the content limit above that, not the link.
Until 1.26.0 streaming put 487 pixels of each frame on the glass with every
counter clean -- §4.11, the fault worth reading before trusting any counter here.

**OPEN, and the current limit on streamed quality:** the scan-out is
memory-bound at four system clocks per pixel, so one S-PWM pass takes 267 Hz
and a complete 64-pass grey cycle takes 240 ms (~4.2 Hz). Streaming faster than
that builds each pixel's brightness from several different frames: sparkle, and
moving edges that are not straight. Measured and derived in 4.10's table
correction. Solid colours are blind to it. `clk_div 6` thins it; nothing short
of more read bandwidth fixes it.

**Working (gateware 1.26.0, firmware 2.97.0, `--outputs 1`, `clk_div 6`):** the framebuffer IS the
glass. The module's 2x2 group arrangement is data in the panel preset and the
layout config (`group_origins`), applied by per-group origin CSRs; nothing is
remapped in software any more (`quad_src_xy` is the identity). On the raw path
the single-panel test screen is flush on all four edges, both diagonals are
clean, the version text reads, and the 16-pitch dots are gone. Chip registers
are the factory's (vendor profile, §5c). Gigabit Ethernet closes timing (§5b).
Four outputs fit the device (§6a). The wide wire protocol carries a four-panel
frame.

**Corrections in the chip table, and their standing:**

| knob | value | standing |
|---|---|---|
| `sweep_adv` | 1 | derived (§4.6a), reproduced in simulation, confirmed on glass, **not yet seen on a scope** -- the one remaining inferred value |
| `reg0x03` | **`0x3f`, the factory value** | sets BOTH the displayable grey range and the W12 cadence, and they are not independent (§4.10). We wrote `0x00` for three days and every MIXED colour clipped to full while pure primaries looked perfect |
| `grey_bits` | 13, **derived** | `4 x (reg0x04+1) x (reg0x03+1) = 8192` levels (11.7.5), matching the vendor profile's `grayScale: 8192`. Computed from the register table, not a constant |
| `row_slot` | 64 panel clocks | floor is the chip's own: `4 x (reg0x04+1)` GCLKs per row at `FGCLK = FDCLK x (reg0x06[2:0]+1)` = 42.7 clocks (11.7.2, 11.7.4) |
| `clk_div` | 4 | **measured**, not derived: 6 clean, 4 clean, 2 snows -- a latency wall (§4.6) |
| `chan_xor` | 0 | the pair swap carried as 1 until 2026-10-19 was a fitted artefact of the compensated pattern path; retired on glass evidence (§4.6b) |
| `chan_rev` | 0 | glass overrules §11.2's channel-15-first for this wiring |
| serialiser rotation | none | fixed at source (`854e8ae`, `d1f7b86`); the 15/17/18-column table in §4.7 is history |

**Open, in priority order:**

1. **Scope the DATA_LATCH commit** (LE pin 24 against SDI pin 1): the one
   measurement that turns `sweep_adv = 1` from inferred into seen. `SCOPE.md`.
2. **Streamed content on the raw path.** The geometry fix means marquee's
   frames should now land pixel-exact with no remap; nobody has looked yet.
   The bench wall's card box must become 1024x128.
3. **Four modules on one card.** Everything so far is one module on J1.
4. **The vendor profile's remaining fields** -- sendagi is decoding the
   arrangement records with Colorlight's own server; Linsn and ARGO have the
   open PCB questions (`docs/PANELS.md`).

**What to run first on a card that looks wrong**

| symptom | test | what it means |
|---|---|---|
| sparse wrong pixels on fine detail | sweep `clk_div` up | density follows the clock -> read starvation (§4.6); unchanged -> mapping |
| whole image sheared, or squeezed into the top of the glass | on gateware < 1.20: the layout's virtual width must equal `module_w x n_outputs` | the stride was a bitstream constant until 1.20.0; since then it follows `ctrl.width` (§4.2) |
| dark panel, healthy `refresh_count` | `refresh_count` counts the FSM, not output | it increments with nothing attached (§6) |
| picture frozen, `refresh_count` stopped | expected since 1.8.0 | writes are off; the row scan free-runs (§4.5) |
| rows lit out of step, "rain" | check `0x03` against the W12 cadence | one W12 per `groups x scan` ROW pulses (§5c) |
| solid colour looks perfect | it cannot find a column fault | use `hstripe`, `chan0`, `colmarks` (§4.8) |
| nothing on J2-J4 | patterns were windowed to one strip | fixed 2026-10-19; since 1.18.0 `quad_src_xy` is the identity and every pattern is per-output |
| mixed colours washed out, pure primaries fine | check `reg0x03` against the factory value | the chip displays `4 x (reg0x04+1) x (reg0x03+1)` levels; too few and every mixed colour clips to full while primaries look perfect (§4.10) |
| sparse wrong pixels ("snow") | sweep `/api/clkdiv` | density follows the divider -> starvation. 4 is the measured operating point (§4.6) |
| a test pattern is replaced within seconds | disable the card in marquee | the player streams, or the value pusher feeds a program, straight over it. The card still reports `source: held (native pattern)` |
| a program YAML edit has no effect | run `./build.sh panelc` BEFORE `firmware` | the firmware build does not regenerate `generated.rs`; the old program is compiled in |

**The three commands that answer most questions**

```bash
./build.sh sim-icn1065                      # 120+ checks, no hardware needed
curl -s http://<card>/api/status | jq        # every live register, read back
python3 tools/decode_rcvbp.py <file>.rcvbp   # what the factory actually programs
```

---

## 1. The hardware

### The card: Colorlight E320 V6.2

Despite no public teardown existing, the E320 is **the same silicon as the
5A-75E** and runs that card's bitstream and firmware unmodified:

| | |
|---|---|
| FPGA | Lattice `LFE5U-25F-6CABGA256` (ECP5) |
| SDRAM | ESMT `M12L64322A-5T`, 8 MB |
| Flash | GigaDevice `GD25Q32` |
| Connectors | **8 x HUB320**, 26-pin, four RGB groups each |
| JTAG | factory-fitted header (the 5A-75E lacks one) |

**Build it at 35 MHz, not 40.** It is a `-6` speed grade and the design does not
close 40 MHz. A 40 MHz firmware on 35 MHz gateware boots and then fails at
everything time-dependent, which presents as a network fault and wastes a day.

The method that predicted the E320 would work before we had one, and which
generalises to any unknown Colorlight card: dump the factory flash, `ecpunpack`
it, and list the PIO sites with **`IOLOGIC`** configured. A registered signal has
it; an untouched pad does not. Do **not** use `BASE_TYPE` — Diamond assigns one
to unused pins too, so half of every bank looks configured and the result is
noise. 36 of the E320's 45 bonded IOLOGIC pins landed exactly on 5A-75E signals.

### The module: XP1.25-5958S-256X128-64S-1010-B1

| | |
|---|---|
| Pitch / size | P1.25, 320 x 160 mm |
| Resolution | **256 x 128** |
| Column driver | **DP3264S** (16-channel S-PWM, Depuw) |
| Row driver | `5958` serial shift register (vendor config says `7559`) |
| Interface | HUB320, 26-pin |
| Scan | "1/64" per the spec sheet — **see §4.1, this is not what you feed the gateware** |

**The driver IC part number is the only thing worth trusting.** We received
*four* mutually contradictory supplier claims for this one module — `ICN1065L`,
`DP3364S`, `ICND1065S`, and `DP3264S` — identical pitch, size, scan and refresh
on every sheet. Photograph the chips. `DP3264S` and `DP3364S` are different
parts with different register counts; one digit decides whether any of your code
works.

---

## 1a. The signal chain, end to end

The single most useful thing to hold in your head, because almost every fault
in section 4 is a handoff between two of these stages and the symptom rarely
points at the stage that is actually wrong.

```
 marquee (host)
   |  UDP, 10-byte header, 487 px per chunk
   v
 Ethernet  --->  LiteEth MAC  --->  either the CPU or the gateware DMA
   |                                        |
   |                                        v
   |                              pixel DMA writes SDRAM directly
   v
 SDRAM framebuffer, 0x00BBGGRR, one word per pixel
   |  virtual width = module_w x n_outputs = 256 x 4 = 1024 words (gw 1.18+)
   v
 Icn1065PixelSource    one request -> `groups` reads (4 on HUB320)
   |                   each at its group's ORIGIN in the module (2x2 here)
   v
 Icn1065PipelinedArbiter    N sources -> one SDRAM port, split transactions
   |                        `per_source` reads in flight per source
   v
 gamma (8 -> 12 bit)  ->  Icn1065Serialiser   16 bits per pixel, MSB first
   |
   v
 HUB320 pins: 12 data lanes (4 groups x RGB), CLK, LE, ROW, A/B/C
   |
   v
 DP3264S chain: 8 chips x 16 channels = 128 columns, cascaded
   |  one 16-bit word held per chip; DATA_LATCH commits it
   v
 LEDs
```

### The two clocks that decide everything

| | |
|---|---|
| **System clock** | 35 MHz. The E320 is a `-6` grade and will not close 40. |
| **Panel clock** | `sys / clk_div`. **The DP3264S preset ships `clk_div = 4`** (8.75 MHz), measured -- 6 (5.83 MHz) is also clean, 2 (17.5 MHz) snows. See 4.6. |

A pixel is 16 panel bits, so the **pixel budget is `16 x clk_div` system
cycles**: 32 at `clk_div=2`, 64 at 4, 96 at 6. Every bandwidth question on this
board reduces to whether the fetch fits that budget, and section 4.6 is the
story of it not fitting.

### What a pixel costs to fetch

Measured in simulation (`./build.sh sim-icn1065`, `check_group_read_overlap`),
for one source reading four groups, against memory round trip `L`:

| `L` | serial (before) | overlapped (now) |
|---|---|---|
| 1 | 20 | 6 |
| 4 | 32 | 9 |
| 8 | 48 | 13 |
| **16** (LiteDRAM's realistic figure) | **80** | **21** |
| 24 | 112 | 29 |

`4L + 16` became `L + 5`. At L=16 that is 21 cycles against a 32-cycle budget,
so **`clk_div=2` is now reachable** where before even `clk_div=4` (64 cycles)
starved. That ratio is the whole four-panel refresh budget; see section 6a.

### The scan-row arithmetic, which trips everyone

The module is 256 x 128 physically and the gateware drives it as **four RGB
groups of 128 x 64**:

```
  columns      = 128      what one group's driver chain holds
  scan         = 64       rows the row driver walks
  groups       = 4        RGB triples on one HUB320 connector
  rows         = 256      = groups x scan, the LOGICAL framebuffer height
  width_words  = 512      = columns x n_outputs, the framebuffer STRIDE
```

**Until gateware 1.17.0** a 256 x 128 panel was therefore addressed as a
128-wide, 256-tall image, the four quadrants being four horizontal bands 64
rows apart -- the HUB75 picture of a panel, which this module does not match.
Every raw framebuffer write (marquee's stream, the boot screen) landed with the
halves swapped, and only the diagnostic patterns, pre-distorted through
`quad_src_xy`, looked right. **From gateware 1.18.0** the preset carries the
module's real shape (`module_w x module_h` = 256 x 128) and a per-group origin
(`group<g>_origin` CSRs, reset from the preset, overridable by `group_origins:`
in the layout config), so the framebuffer IS the glass:

```
  module_w     = 256, module_h = 128          the glass
  width_words  = module_w x n_outputs = 1024  the framebuffer STRIDE
  linear(g)    = row * width_words + col + origin_x[g] + origin_y[g] * width_words
```

Get `width_words` wrong and the picture shears rather than reshaping -- the
S-PWM stage never reads `ctrl.width`, the stride is a bitstream constant, so
every counter looks clean while the image is wrong (section 4.2).

---

## 2. HUB320 is a shape, not a standard

Pins 1–19 and 22–26 agree across every source we have. **Pins 20 and 21 do
not.**

| Pin | Our card drives | This module's silkscreen | A third vendor |
|---|---|---|---|
| 1,2,3 | R1 G1 B1 | R1 G1 B1 | R1, G1, **B1 / S-CK** |
| 5,6,7 | R2 G2 B2 | R2 G2 B2 | R2, G2, **B2 / S-MO** |
| 9–11, 13–15 | R3 G3 B3, R4 G4 B4 | same | same |
| 17,18,19 | A, B, C | A, B, C | A, B, C |
| **20** | **D** | **NC** | **S-CS** |
| **21** | **E** | **NC** | **S-MI** |
| 23,24,25 | clk, lat, oe | CLK, LAT, OE | same |
| grounds | 4, 8, 12, 16, 22, 26 | same | same |

**Read any new module's silkscreen at pins 20 and 21 before plugging it in.** On
the third convention, pin 21 is **S-MI — an output from the module**. Driving a
row-address bit into it is output against output.

---

## 3. The row driver is a shift register, not a decoder

Three address lines cannot select one of 64 rows, and that arithmetic sends
people hunting for a cleverer mapping. There isn't one.

Raffar's **RT5958D** datasheet settles it: *"8-channel PMOS … built in the 8-bit
shift register, make the data transfer by serial connection **without decode
components**"*, and in its own feature list, **"Wipe off 138 decoder."** Eight
cascaded chips (`DOUT -> DIN`) is exactly 64 rows.

Its three control pins are **DIN**, **LCK**, **BK**. A row is selected by
**walking a single 1 along the chain**: one LCK pulse per row, DIN high only on
the wrap to row 0, BK held across the transition as the anti-ghosting discharge
window. board707's `DMD_Mux595::set_mux()` is a working reference — note it
raises the latch, sets data, **waits**, *then* clocks. Data and clock asserted on
the same edge gives the shift register zero setup time.

Row/mux chips split into two families, and the distinction is the whole game:

| family | examples |
|---|---|
| **binary decoder (138)** | ICND2012/2013, TC7258, SM5166 |
| **shift register (595)** | DP32020, ICND2018, SM5158, TC7559, **RT5958/DM5953** |

Colorlight's own software agrees: its `chip.ts` maps `decodeChipId 3` to
`DM5953/5958` and routes it to the **595** editor, never the 138 one.

---

## 4. The faults, and how each presents

Every one of these produced a *different* wrong picture. Recognising the
symptom is most of the work.

### 4.1 Geometry: scan is per-group, and the spec sheet lies

The module says **1/64 scan**. Feed `--scan 64` with four groups and you drive
twice the rows each group has.

Work it out from pixels instead:

```
256 x 128 = 32,768 pixels
groups x columns x scan = 32,768
```

This module is **four 128x64 quadrants, one per RGB group**:

```
top-left  group 2      top-right  group 0
bot-left  group 3      bot-right  group 1
```

so `4 x 128 x 64`. The arithmetic above is the authority: four groups at
1/64 scan means **128 columns per group**, not 256. (An earlier revision of
this file wrote `4 x 256 x 64` one line after deriving 128, and that
contradiction cost a whole evening — driving 256 clocks more than each
128-deep chain holds, so the surplus overflows.) **Measure it — don't derive
it.** See §5.1.

*Symptom of getting it wrong:* dark bands through every quadrant (scan too
small), or straight lines breaking into a **sawtooth** (too large).

### 4.2 The framebuffer row stride

**RETIRED in gateware 1.20.0 (2026-10-19): the stride is now `ctrl.width`,
set by the firmware to the layout's virtual width, as hub75.py always had it.**
A layout narrower than the card -- one module on a four-output bitstream -- is
legal and renders whole. The trap below bit twice (the second time on 10-19,
when a `grid: 1x1` layout on the four-output bitstream squeezed the whole
picture into the top quarter of the module) before the constant was replaced.
Kept for anyone running an older bitstream.

Before 1.20.0, `icn1065_soc.py` set `width_words = columns * n_outputs`
(`module_w * n_outputs` from 1.18), and the pixel source used it as the **row
stride**:

```python
linear(g) = (row + g*rows_per_frame) * width_words + col
```

If your layout's virtual width is not **equal to that product**, the reader and
writer disagree and every image row folds. With `columns=256, n_outputs=2` the
stride is 512; a 256-wide layout renders a 256x128 image as **128 wide x 256
tall**.

Fix it in the **layout**, not the gateware — place the outputs side by side:

```yaml
grid: 2x1
panel_width: 256
J1: 0,0
J2: 1,0      # drives nothing physical; it makes the arithmetic agree
```

`--outputs 1` is not an alternative: `Icn1065PipelinedArbiter(1)` builds a
zero-width tag signal and asserts.

*Symptom:* half-width coverage **under every value of `columns`** — raising
columns raises the stride by the same factor, so it never helps. Plus doubled
bands and a dead half-panel.

### 4.3 ROW must never stop

`DP3264S_REV2.0` §11.5: the on-chip GCLK generator turns what is OE on an
ordinary part into a **ROW** signal. §11.6.2: *"ROW is sent continuously at a
fixed display frequency **without interruption**."*

**ROW clocks the display engine.** A scan-out loop that emits ROW only after
sending pixel data leaves it absent for the whole data phase — at 256 columns x
64 rows that is 262,144 clocks of silence against 8,192 of scan, **97% absent**.

Its rising edge starts a row and its **width carries meaning**: **W12** (12 DCLK)
for the first row of group 0, **W4** for all others.

*Symptom:* **total darkness**, invariant to every row mapping, chip table and
pulse width — because the display engine is stopped. Note §11.6.2 note 1
explicitly permits ROW to overlap VSYNC, so it can free-run concurrently.

### 4.4 The pixel handoff

A producer that publishes a value **combinationally for one cycle** cannot be
read by a consumer latching on a **slower clock enable**. They coincide only by
luck.

*Symptom:* a **sparse scatter of correct pixels in a dark field**, whose density
follows the clock divider (the divider *is* the sampling cadence), with a
left-to-right gradient as the phase drifts, and output **identical for every
input pattern** because the pattern mostly never arrives.

Register the published value so it **holds** between pixels.

### 4.5 Writing SRAM while the chip displays from it

These drivers hold the whole frame and refresh autonomously at 3840 Hz. If you
re-send pixels continuously, you are rewriting the buffer under the read-out.

*Symptom:* tearing that migrates across the image, plus a blink at the data
frame rate. **Freeze the writes: if it goes rock steady, this is your fault.**
Updates belong on VSYNC.

---

### 4.6 Memory starvation: stale pixels that follow the clock

**Measured on glass 2026-10-17.** At `clk_div = 2` (17.5 MHz panel clock) a
fine test pattern shows isolated wrong pixels scattered along otherwise clean
lines. At `clk_div = 6` (5.83 MHz) they vanish completely.

The cause is the handoff of §4.4 seen from the other side. The pixel source
latches each pixel into a `held` register so it survives between clock
enables. If the framebuffer read has **not completed** by the time the
serialiser needs that pixel, the register still holds the PREVIOUS pixel --
so one pixel renders in a neighbouring pixel's colour. Registering the value
fixed the handoff; it did not create bandwidth.

*Symptom:* sparse wrong-coloured pixels on fine detail, **whose density
follows the panel clock**. Solid fills look perfect, because every pixel is
the same colour and a stale one is indistinguishable -- this is exactly why a
solid-colour test cannot clear a panel of this fault.

*How to tell it from a mapping bug in one step:* change `/api/clkdiv` and
look again. A mapping error is invariant to the clock; starvation is not.
That one test took this from "structural, chase it at the chip boundary" to
"bandwidth" in seconds, and it needs no rebuild and no power cycle.

**The dots are not random per column.** They land wherever a read happened to
be late, so a single photograph can show only two affected columns and invite
a structural explanation. Do not conclude "those two columns are special"
from one frame -- sweep the clock first.

*Operating point:* **`clk_div = 4`, measured on glass 2026-10-20.** Swept live
through `/api/clkdiv` with the `ramp` pattern up and marquee kept off the card:
6 clean, **4 clean**, 2 snow. The snow is this section's signature -- sparse
wrong pixels whose density follows the divider.

This paragraph has been wrong in both directions and the history is the useful
part. It first said 4 was clean (true, but untested at the time); then said
"6 or slower, 4 STARVES" after section 5a disagreed; it is now 4 again, this
time because somebody looked. What changed in between is real: the group-read
overlap (`63c3880`) cut a pixel from `4L + 16` cycles to `L + 5`, which is
what made 4 affordable. **Do not edit this line from arithmetic. Sweep the
divider and look at the glass.**

*And 2 is out of reach for a reason worth knowing:* one module alone snows at
`clk_div = 2`. That is a LATENCY wall, not bandwidth -- 32 system cycles per
pixel against a LiteDRAM round trip plus refresh jitter -- so no number of
outputs, modules or cards buys it. Only a deeper pixel pipeline would.

The arithmetic, which is what settles it. The four group reads are strictly
sequential -- `READ0 -> GAP0 -> READ1 -> GAP1 -> READ2 -> GAP2 -> READ3` in
`Icn1065PixelSource`, each `READ` blocking on its own `mem_ack` -- so one pixel
costs `4L + 16` system cycles for a memory round trip `L`. Measured in
simulation: L=1 -> 20 cycles, L=4 -> 32, L=8 -> 48, **L=16 -> 80**. The budget
is one pixel per 16 panel bits, i.e. `16 x clk_div` cycles:

| `clk_div` | budget | fits if |
|---|---|---|
| 2 | 32 cycles | L <= 4 -- unreachable |
| 4 | 64 cycles | L <= 12 -- **below LiteDRAM's realistic 16** |
| 6 | 96 cycles | L <= 20 |

So at the repo's own realistic 16-cycle figure the path needed 80 cycles
against a 64-cycle budget at `clk_div = 4`: over budget by 25% with ZERO
arbiter contention, before four outputs start competing. 6 was the first
divider with real margin, which is exactly where the bench landed empirically
-- and is why the preset ships 6. **That was all before the fix below.**

### FIXED 2026-10-19: the four group reads now overlap

The serial dependency was the whole cost, and it is gone. Two changes:

* **The source's memory handshake is SPLIT.** `mem_cmd_ack` says the command
  was accepted, `mem_ack` says the data is on the bus. The FSM is now one
  `XFER` state with two counters: `issued` walks the group addresses as fast as
  the arbiter takes commands, `recvd` consumes the words as they return **in
  order** -- which LiteDRAM's native port guarantees and the arbiter's tag FIFO
  preserves. The `READ0/GAP0/READ1/...` chain is gone, and with it the GAP
  states, whose only job was to stop a sticky `ack` satisfying two reads.
  That cannot happen when commands and data are counted separately.
* **The arbiter allows `per_source` reads in flight.** `outstanding` was one
  BIT per source -- which is what capped a source at a single read however well
  the arbiter pipelined across sources. It is a counter now, and the SoC passes
  `per_source=groups`.

Measured (`check_group_read_overlap`), cost per pixel:

| `L` | serial | overlapped | ratio |
|---|---|---|---|
| 1 | 20 | 6 | 3.3x |
| 4 | 32 | 9 | 3.6x |
| 8 | 48 | 13 | 3.7x |
| **16** | **80** | **21** | **3.8x** |
| 24 | 112 | 29 | 3.9x |

`4L + 16` became `L + 5`. At L=16, 21 cycles against the 32-cycle budget of
`clk_div = 2`, so **every divider down to 2 now fits** where before even 4 did
not. `check_group_read_overlap` asserts this so it cannot silently regress --
the four-panel budget rests on it.

A 16-bit framebuffer needed care here: `half` selects which pixel of a packed
word to decode, and with reads in flight the group whose ADDRESS is going out
is not the group whose WORD is arriving. It now follows `recvd`, not `issued`.

*What this does NOT by itself prove:* that the dots were starvation. It removes
the mechanism, which is the right fix either way, but §4.6a is the open
question of whether something else produces the same signature.

*Why this starves rather than drops frames:* nothing backpressures. `PIXELS`
advances on `ser.loading` alone and never waits on `src.valid`; the serialiser
reloads unconditionally; and `held` keeps its last value. A missed deadline
therefore re-shifts the PREVIOUS word instead of stalling, so `refresh_count`
stays perfectly healthy while the wall displays stale pixels.

### 4.6a The 16-pitch dots: the open question

**Observed on glass 2026-10-18, not yet closed.** The most carefully
characterised fault on this board and worth the detail, because its *geometry*
rules out most explanations on its own.

*Symptom*, with `hstripe` (alternating white and black **rows**):

* exactly **one wrong pixel per driver chip, on every row**
* at columns **0, 16, 32, 48, 64, 80, 96, 112**
* each carrying the **adjacent scan row's** value -- so white rows get a black
  dot and black rows get a white one
* **static** in position
* **invisible on a solid fill**, and invisible on any pattern that does not vary
  by row

#### Why those columns are the whole clue

The data phase walks **channel-major**: `col = Cat(chan_eff, sect)`, i.e.
`col = chan + 16*sect`. So sweep `chan=k` -- the k-th `DATA_LATCH` of the row --
feeds columns `k, k+16, k+32 ... k+112`, one column in each of the eight chips:

| sweep | latch # | columns it feeds |
|---|---|---|
| `chan=0` | 1st of the row | **0, 16, 32, 48, 64, 80, 96, 112** |
| `chan=1` | 2nd | 1, 17, 33, 49, ... |
| `chan=15` | 16th | 15, 31, 47, 63, ... |

The observed columns are *exactly* the `chan=0` set. **The defect is the FIRST
`DATA_LATCH` of every row, in all eight chips.**

#### Ruled out, with evidence

* **A single late fetch.** It corrupts one word, hence one chip. Eight dots is
  all eight chips, so one late read cannot do it.
* **Our latch stream.** The pulse-width census over a whole frame is exactly
  `{1: 16 x rows, 3: 1, 14: 1, 5: 1}` -- no stray width-1 LE anywhere, the
  prefix is `[3, 14, 5]`, and there are precisely `rows x 16` data latches
  (`check_latch_budget`). Since VSYNC resets the chip's write pointer
  (datasheet 10.7.1) and our count is exact, there is no spare latch to blame.
* **The fetch ORDER and the row-boundary race.** Three independent chain models
  agreed: every latch commits its own sweep's eight words, for the right row,
  with the right column in the right chip, across row and frame boundaries,
  0 mismatches. The only row straddle is the deliberate one-ahead prefetch,
  consumed by the next latch.
* **A ROW / DATA_LATCH collision.** `ROW_OE_LEN=128` equals one sweep at
  `columns=128`, so the two *look* frequency-locked -- but the scanner inserts
  one extra clock per pass (§6), so the phase creeps one clock per pass and no
  latch ever lands while ROW is high. Any interaction would flicker at ~8 Hz.
  The dots are static.
* **A pattern-generator artifact.** `hstripe` depends on `iy` only; there is no
  column term of any period in it, and the column pipeline's result is
  discarded by that pattern.
* **A factory remap we are missing.** The vendor's pixel map is the identity
  (§5c).

#### The two surviving candidates

**(a) Read starvation as a STEADY-STATE LAG.** The source re-samples the
current coordinates each time it goes idle, so once fetch time exceeds the
pixel period it settles into a *permanent* lag of `m-1` loads rather than
missing one. Display load `d` then carries pixel `d-(m-1)` forever, and the
loads whose pixel falls in the previous row are exactly `(chan=0,
sect=0..m-2)` -- columns 0, 16, 32 ... **A lag of 8 is the entire `chan=0`
sweep, on every row.** Simulated dot counts fill in from column 0 at a
16-column pitch as latency rises: 0 dots, then 2 at cols 0/16, then 4, then 5.
The repo had already recorded the same signature at
`icn1065.py`'s prefix prime: *"one stale first sweep is eight wrong pixels, one
per chip, 16 columns apart, starting at pixel (0,0)"*.

The caveat, stated honestly: eight dots needs ~100-140 cycles per read, far
above the 16-cycle model, reachable only with heavy crossbar contention.

**§4.6's fix removes this mechanism** -- per-pixel cost is now `L + 5`, so the
lag is zero at every divider. If the dots survive it, candidate (a) is dead.

**(b) The chip's write-pointer origin is one position ahead of ours.** If our
latch `n` commits to position `n+1`, then for line `L` channel 0 (position
`16L`) receives our latch `16L-1` = (row `L-1`, sweep 15) -- the adjacent row,
in every chip, at `x = 0 mod 16`. Exactly the observed set. This model makes
two further predictions worth checking on glass:

1. position 0 (line 0, channel 0) is **never written by any frame**, so the
   top-left dot of each quadrant should be a **frozen** pixel, not an
   adjacent-row one. Switch from all-white to row stripes and watch whether
   (0,0) keeps the old colour while the other 63 follow the neighbour row.
2. `latch_phase = 1` must move the dot column to `x = 1 mod 16` and the frozen
   cell to (0,1). If it moves by 2, or by 16, the model is dead.

Direction is already pinned: dots at `x = 0 mod 16` mean the pointer **leads**
us. Were we ahead of it, they would sit at `x = 15 mod 16` with the frozen cell
at the bottom-right.

The related lead is register `0x03` (§5c): we set one display pass where the
factory sets 64, and that register governs precisely how the chip's internal
data pointer advances against the external row chain.

#### TESTED ON HARDWARE 2026-10-19, and the result

All of this was run on gateware 1.16.0 / firmware 2.85.0, with the FACTORY
register values (section 5c) and `spwm_configured = dp3264s`. Five hypotheses
went in and all five came out dead.

| test | result | what it eliminates |
|---|---|---|
| factory register values, 12 of 13 applied | **no change** | the seven differing registers |
| `clk_div` 4 -> 6 (and 15) | **no change** | READ STARVATION -- the dots are clk_div-INVARIANT, and our own discriminator says invariant means a mapping or protocol fault |
| group-read overlap (4L+16 -> L+5) | **no change** | starvation again, structurally this time: the lag is zero at every divider |
| red fill, then row stripes | **no frozen pixel anywhere** | a once-per-frame pointer origin offset -- every position IS written |
| `latch_phase` 0, 1, 15 | **no change** -- the pattern is the same as before any of it | the latch phase, as a count of extra pulses |

On the last row, be careful about what was and was not seen. At N=15 there was
an initial impression of a change ("it changed", "uniform"), which did not hold
up: the settled observation across N = 0, 1 and 15 is that **the pattern is
identical to what it was before this session's work**. The tentative reading is
recorded only so nobody re-derives it as a result -- it was not one.

That is consistent with the geometry, which is worth stating because it means
the knob was always a weak lever: we emit exactly 16 latches per row and the
chip steps a line every 16, so **one channel always sits on the wrap**. A count
of extra pulses once per frame can only move WHICH channel lands there; it
cannot remove the wrap. Clearing the dots needs the wrap to coincide with our
ROW boundary -- a rotation of the channel/row ADDRESSING -- or needs the line
advance itself to change (`0x03`, below). Adding latches was never going to do
either, and that follows from the counting; it did not need a build to find
out.

#### WE HAD THE WRONG DATASHEET (2026-10-19)

The board is marked **DP3264S**. `docs/datasheets/DP3364S_Rev1.0_EN.pdf` is a
**different part**. Section 1 of this document already warned that one digit
decides whether any of the code works, and the supplier was asked to confirm
which and never answered.

The real one is now in the repo as
`docs/datasheets/DP3264S_REV2.0_translated.pdf`. The two differ:

| | DP3364S Rev1.0 | DP3264S Rev2.0 |
|---|---|---|
| sections | 10.x | **11.x** |
| register count | 15 addresses, 15 frames | **13 addresses, 13 frames** |
| LE commands | 6 (adds DDR=2, SDR=15) | **4** |

Our table has **13** registers, which matches the DP3264S -- so the register
table was right even while the datasheet being quoted at it was not.

**The factory values ARE the DP3264S datasheet defaults.** Section 11.9 gives
`0x03` default `3F`, `0x06` default `42`, `0x0a` default `BE`, `0x0b` default
R=`28`. The vendor config matches all four. The values this repo carried before
2026-10-18 (`0x0a=FE`, `0x06=41`, `0x0b` R=`29`) match none of them, which is
independent confirmation that the old table had drifted and the factory one is
correct.

**Exact register meanings, from 11.9 rather than inferred:**

```
0x02[5:0]  LINE_SET     scan lines = LINE_SET + 1
0x03[6:0]  GROUP_SET    display refresh rate = (REG0F[7]*128 + REG03[6:0] + 1)
0x04[6:0]  PWM_WIDTH    row PWM width = 4 * (PWM_WIDTH + 1)
0x06[2:0]  PLL_DIV      fGCLK = fDCLK * (PLL_DIV + 1)
0x06[7:3]  DECOUP_RAT   coupling optimisation coefficient
0x08[7:0]  IGAIN        reg08[7:6] is the 50% current-gain increment
```

So `0x04 = 0x1f` is 128 grey levels per row; the `0x13` we carried was 80.

**11.2 confirms the channel order in the RIGHT datasheet: channel 15 first,
counting down to channel 0.** (Its sequence numbers are misprinted -- it shows
497..512 for Line 63 where 1009..1024 is arithmetically required, and the
DP3364S prints the correct figures for the same table. The ORDER is the part
that matters and both documents agree on it.)

#### GROUP_SET, computed rather than guessed

11.6.2 gives the formula for our mode:

> Number of display data groups = round(1 / frame rate / (number of row scans *
> display time of each line)); `reg0x03[6:0]` = groups - 1.
> Group 0 row 0 sends W12, otherwise W4. **There is only one W12 for each
> (number of groups * number of rows scanned) ROW signals.**

With our numbers at `clk_div = 6` (panel clock 5.833 MHz):

```
per-line display time = ROW_OE_LEN 128 clocks      = 21.94 us
one group             = 64 rows x 21.94 us         =  1.404 ms
data frame            = 128 x 64 x 16 panel clocks = 22.47 ms
groups                = 22.47 / 1.404              = 16
  => GROUP_SET = 15 (0x0f), and ONE W12 per 16 x 64 = 1024 ROW pulses
```

We ship `GROUP_SET = 0` with one W12 per 64 ROW pulses. That pair is
self-consistent, which is why it works at all, but it is not what the panel's
timing calls for: it tells the chip to display each frame **once**, so the
visual refresh collapses to the data frame rate instead of the 16x the part is
designed to give. The factory's `0x3f` (64 groups) is the other
self-consistent pair and needs W12 once per 4096.

**None of the three values explains the dots**, since all three are internally
consistent -- but 15 is the right one for this panel's timing and is worth
setting once the dots are understood, because it is a 16x visual refresh
improvement for free.

#### Our emission is PROVEN correct at the real geometry

Every earlier check ran at toy sizes -- 32 columns (2 chips), 2-4 rows. The
real build is 128 columns (**8 chips**), 64 rows, 4 groups, and the cascade
depth is exactly the dimension a chain bug would hide in. Re-run at real size:

```
LE high-pulse widths: {3: 1, 14: 1, 5: 1, 1: 1024}
DATA_LATCH pulses: 1024   (= 64 rows x 16)   per row: 16.000
fetches 8193, distinct columns 128, distinct rows 64
```

and with a memory whose contents encode their own address, decoding what every
latch commits to every chip of every group, across two frames:

```
frame 0: 1024 latches, mismatches 0
frame 1: 1024 latches, mismatches 0
```

Chip *j* (nearest the FPGA) holds columns `16*(7-j) + sweep`, for the right
row, in all four groups, across row and frame boundaries. **The data we emit is
correct.** Whatever is wrong is in how the chip interprets it, or in the
wiring between them -- not in the gateware's addressing or latch sequence.

#### THE DERIVATION (2026-10-19) -- why `latch_phase` could never work, and what can

Five hypotheses died on hardware. What survived was not a sixth guess but a
consequence of the four facts already established, once the right datasheet
closed an ambiguity the repo had carried since 10-17.

**Why `latch_phase` did nothing, structurally.** An extra `DATA_LATCH`
advances the chip's write pointer AND delays our data by one position. The
RELATIVE phase between them is unchanged. `latch_phase` is therefore incapable
of correcting a pointer offset -- not "did not happen to", but cannot. The
hardware null result at N = 0, 1 and 15 is exactly what the mechanism predicts,
and the knob was mis-aimed from the day it was built. (It remains useful as an
instrument: it proves pulses reach the chip, verified 1024 -> 1025 -> 1027 in
simulation at the real geometry.)

**The channel-order ambiguity, resolved.** `spwm_chips.py` recorded that
`chan_rev=1` breaks the diagonals and `chan_rev=0` leaves them clean, and left
two explanations open: either our ascending walk already produces the chip's
order, or the module's wiring compensates. The glass cannot separate them --
both predict `chan_rev=0` looking right. DP3264S_REV2.0 11.2 settles it: the
chip's order IS channel 15 down to 0, so our ascending walk is NOT the chip's
order. The first branch is eliminated and the wiring reverses. `chan_rev=0` is
correct because TWO reversals cancel.

That is not a detail. It fixes which physical column channel 15 drives, and
that is what gives the correction its sign.

**The chain, with every link's basis:**

| step | basis |
|---|---|
| dots at physical columns 0, 16, 32 ... | observed on glass |
| those columns are fed by CHANNEL 15 | 11.2 + the `chan_rev` result, jointly determinate |
| channel 15 is the FIRST position written in a line | 11.2 |
| line r's first-written position receives our latch `r*16 - 1`, the previous row's last sweep | arithmetic |
| => the chip's write pointer runs ONE AHEAD of our latch index | follows |
| => the correction is +1 SWEEP of fetch address | follows |
| our emission is correct to begin with | simulation, 0 mismatches at real geometry over two frames |

**The lever: `sweep_adv`.** It advances the FETCH ADDRESS by N sweeps, carrying
into the row, and leaves the latch count at exactly 16 per row. That is the
distinction from `latch_phase`: it moves the data WITHOUT moving the pointer,
which is the only thing that changes their relative phase. Verified in
simulation:

```
sweep_adv=0 : fetches (0,0) (0,16) (0,1) (0,17) ...    latches 64
sweep_adv=1 : fetches (0,1) (0,17) (0,2) (0,18) ...    latches 64
row boundary: row 1 begins one sweep sooner; frame wraps cleanly
```

Defaulted to **1** in the `dp3264s` chip table so the correction is live on
power-up. `/api/sweepadv {"n":0}` reverts it with no rebuild if the glass
disagrees.

**THE DERIVATION REPRODUCES THE SYMPTOM IN SIMULATION.** Not a restatement of
the reasoning -- a model of the CHIP under the hypothesis, driven by the real
output stage, which independently generates the defect:

```
rows= 8 sweep_adv=0:  49 wrong, columns [0, 16, 32, 48, 64, 80, 96]
rows=16 sweep_adv=0: 105 wrong, columns [0, 16, 32, 48, 64, 80, 96]
rows=24 sweep_adv=0: 161 wrong, columns [0, 16, 32, 48, 64, 80, 96]

rows= 8 sweep_adv=1:   8 wrong, columns [127]
rows=16 sweep_adv=1:  16 wrong, columns [127]
rows=24 sweep_adv=1:  24 wrong, columns [127]
```

The model holds a write pointer one ahead of our latch index, maps position p
to `(line = p//16, channel = 15 - p%16)` per 11.2, and lights physical column
`16*s + (15-c)` for the reversed wiring. With `sweep_adv=0` it produces ONE
WRONG PIXEL PER CHIP at columns 0, 16, 32 ... each showing the row ABOVE --
which is the defect on the glass, generated from the hypothesis rather than
fitted to it. With `sweep_adv=1` those columns are clean.

*Treat the residual at column 127 as a model artifact rather than a
prediction*: it is a single column instead of the per-chip signature, and the
`sweep_adv=0` row lists only SEVEN of the eight chips (112 is absent). Both
point at the model's crude latch indexing across the prefix pulses, which it
does not align exactly. The per-chip result is the robust part and it scales
cleanly with row count.

**CONFIRMED ON GLASS, 2026-10-19 morning.** `sweepcal` showed the bottom-right
quadrant (shift 1) clean while the other three kept their dots, and the detail
settled the direction too: a RED row carried WHITE dots -- white is `row % 4
== 3`, the row ABOVE a red one -- exactly the "row above" the pre-increment
model predicts. With `sweep_adv = 1` applied live on gateware 1.17.0 the whole
panel is clean. The 16-pitch dots are fixed.

#### Is `sweep_adv` a fix or a kludge?

A fair question, and the answer is "a fix, but with its cause still inferred".
It is NOT fitted: the value was derived before it was tested, from a stated
mechanism, and that mechanism made four predictions that all held -- dots at
the first-written channel, carrying the row above, no frozen pixel, and
`latch_phase` structurally useless. The value is 1 because the offset is one
position, not because 1 looked best. And it lives in the chip table, as a
per-chip property, not in a pattern generator.

What is still inferred is WHY the chip's pointer starts one late. The
datasheet's 11.2 table does not state commit order. The leading candidate, and
it is testable live:

**The register write consumes a data position.** The frame prefix is
`VSYNC -> PRE_ACT -> WR_CFG -> data`. In the 11.4 figure, `WR_CFG` is sixteen
bits shifted on SDI followed by an LE pulse -- structurally identical to a data
latch, with only the LE WIDTH naming a register as the destination instead of
SRAM. If the chip's write pointer advances on any data-carrying latch, the one
register write per frame consumes SRAM position 0, and the first real pixel
lands at position 1. That predicts an offset of exactly one, which is what we
have, and it predicts something sharper:

> **Two register writes per frame must give an offset of two.** `slots` is the
> number of `WR_CFG` per prefix (a `chip_cfg` field, now exposed on
> `/api/chipcfg`). With `sweep_adv = 1` still set, `slots = 2` must bring the
> dots back at the same columns; `sweep_adv = 2` must then clear them. If
> `slots = 2` changes nothing, the register write is NOT the cause and the
> offset comes from somewhere else (PRE_ACT, or VSYNC's own semantics).

**TESTED, AND FALSIFIED (2026-10-19).** `slots = 2` with `sweep_adv = 1`: the
panel stayed clean. Two register writes per frame did NOT add a second position
of offset, so `WR_CFG` does not consume an SRAM write position. Verified in
simulation that the test was real -- `slots = 2` emits two width-5 pulses with
the data-latch count unchanged -- so this is a genuine negative, not a no-op.

The hypothesis was attractive (it predicted exactly one) and it is wrong. Noted
here so nobody re-derives it.

**`PRE_ACT` TESTED, ALSO FALSIFIED.** `mid_latch = 1` adds a second `PRE_ACT`
(the 11-clock `PRE_ACT_MID`) per frame, verified in simulation. The panel
stayed clean.

#### Conclusion: the chip commits at pointer+1. That is the whole answer.

Three live eliminations, each adding one pulse of a different kind per frame:

| extra pulse per frame | effect on the dots |
|---|---|
| `DATA_LATCH` (`latch_phase`) | none |
| `WR_CFG` (`slots = 2`) | none |
| `PRE_ACT` (`mid_latch = 1`) | none |

Nothing in the prefix moves the offset, so nothing in the prefix causes it.
Our data stream is correct and our latch fires at the right moment relative to
it (both proven in simulation at the real geometry). The remaining fact is a
property of the part: **after VSYNC resets the pointer to 0, each `DATA_LATCH`
writes at pointer+1** -- increment, then commit. The datasheet's 11.2 table
lists positions without stating the commit order, and this is the order.

So **`sweep_adv = 1` is not a kludge and not a compensation for anything we
do.** It is the correct alignment to how this chip writes, in the same way
that `chan_rev` handling is the correct alignment to channel 15 going first.
Position 0 of each frame is written by the LAST latch via wrap, and
`sweep_adv` is precisely what makes that wrap carry row 0's data. There is no
more principled form; feeding the data one position early IS the protocol for
this part. It belongs in the chip table as a per-chip property, which is where
it is.

What WAS inferred rather than measured at the start of the night -- the wiring
reversal that gives the correction its sign -- is now corroborated three ways:
the datasheet's 11.2, the `chan_rev` glass result, and the fact that shift 1
(not 15) is the one that cleared the panel.

**The receiver config does NOT corroborate it, and I said for several hours
that it did (§5c).** Its pixel map is the identity, all 8192 entries; the
"line carry" I reported was the records mis-framed by one byte. So the file
is silent on the commit order. What the file does say is what Colorlight's
stock firmware is told: chip type, scan, columns, data groups, registers --
and nothing else. The chip's data order is therefore handled inside the
stock firmware, keyed by chip type. Our equivalent of that per-chip code is
the chip table (`spwm_chips.py`), which is where `sweep_adv` sits. The
evidence for pointer+1 is §4.6a's eliminations, the simulation and the glass
-- not the vendor file.

### 4.6b The column pair swap was not real -- `chan_xor` retired (2026-10-19)

`chan_xor = 1` was carried from 2026-10-18 as "three glass observations and a
constant that cancels them": both borders and the 2:1 diagonal's sawtooth,
all cured by `x ^ 1` on the source column. Those observations were made
through the TEST-PATTERN path, which at the time also carried the per-half
serialiser rotation (§4.7) and the stacked 128x256 geometry (§4.3). Both were
later removed at source. The swap went with them, and nobody could see that,
because every check after the move into the gateware was a row-colour
pattern -- blind to a column swap by construction (§4.8).

The first swap-sensitive picture on the raw path (gateware 1.18.0, framebuffer
= glass, nothing remapped) showed it: with `chan_xor = 1` the red border sat
at column 1, the magenta border at column 254 (reading blue beside black),
and the 5x7 version text was garbled. Flipping the CSR live to 0 put all three
flush. That is a fitted compensation for a fault that had already been fixed
elsewhere, and it is gone: `chan_xor = 0` in the chip table, written at boot.
The CSR stays as the instrument for asking the question of the next module.

The lesson is the one §4.8 already states, applied to ourselves: a correction
validated only with a pattern that cannot see what it corrects is not
validated.

**What would falsify this.** If the dots persist unchanged at `sweep_adv=1`,
the pointer-offset model is wrong and the fault is not a phase relationship at
all. If they MOVE but do not clear, the offset is real and N is wrong -- sweep
it. If they move to columns 15/31/47, the sign is inverted and the wiring
premise above is wrong despite the datasheet.

Two patterns exist to read the answer, both uncompensated:

* **`chanends`** -- red at column 0 of each 16, blue at column 15. Which end is
  red measures the channel order DIRECTLY, settling the one premise that is a
  deduction rather than a measurement.
* **`rowid`** -- four hues cycling every four rows. `hstripe` can only say a
  wrong pixel came from "an adjacent row"; `rowid` makes it NAME its source
  row, giving the offset's direction and its size mod 4 from one photograph.

#### What has NOT been tried

1. **`0x03` with a matching W12 cadence.** The last divergence from the vendor
   configuration: we send one display pass per frame, the factory sends 64
   (section 5c). It governs how the chip's internal data pointer advances
   against the external ROW chain, which is the only per-LINE mechanism left
   after the eliminations above. Needs `Icn1065RowScanner` to count passes and
   assert `row_pulse_first` only on row 0 of every 64th pass. Do not write
   `0x3f` without that -- the chip would see 64x too many W12s.
2. **A scope on LE / CLK / ROW.** Never done. `docs/SCOPE.md` exists for it and
   would settle in minutes where the chip's line boundary actually falls, which
   is the fact every surviving model disagrees about. After five hypotheses and
   four eliminations, this is the honest next instrument rather than a sixth
   inference.

#### A trap worth knowing before judging the glass

**After a power cycle the card came up on COMPILED-IN DEFAULTS** --
`spwm_configured = default` (so the bitstream's built-in `icnd1065` table, not
`dp3264s`), `grid 1x1`, `virtual_width 128`, display 128x64. The panel was
black, which is exactly right for that configuration and says nothing about the
gateware. A soft reboot (`POST /api/reboot`) picked the layout up over TFTP and
everything came good.

So: **after any power cycle, check `spwm_configured` and `grid` in
`/api/status` before drawing any conclusion from the panel.**

#### The knob built for this

`latch_phase` (CSR, `/api/latchphase`) emits N extra `DATA_LATCH` pulses after
the prefix, sliding our start against the chip's line counter. Each is a
**1-clock pulse spaced a full 16-clock word apart** -- LE encodes the command
in its width, so N cycles held high would be one N-wide pulse and N=3 would
send a VSYNC. `check_latch_phase` asserts the widths, because a count-only
check passes the broken version and N=1 passes it either way.

Simulation cannot settle this: it models our counters, which are
self-consistent by construction, and not the chip's.

---

### 4.11 The buffer swap: 487 fresh pixels over a stale frame (2026-10-20)

**Symptom.** Streaming to the module showed a narrow band of the new frame
across the top -- a few rows -- with the rest of the panel holding whatever was
there before, frame after frame. The band was always exactly **487 pixels**:
one chunk.

**Every counter was clean**, which is what made this expensive. Measured on
card 2 while streaming a frame whose red channel encoded its own chunk number:

| What | Reading |
| --- | --- |
| chunks per frame | 68, exactly as sent |
| pixels per frame | 32,281, all inside `limit` |
| arrival bits set | 68 of 68 |
| `frames_completed` | rising; `frames_partial` 0 |
| `chunks_repaired` | 0 |
| `dma_stalls` | 0 |

So every pixel of every frame was written to SDRAM, in range, and the firmware
agreed the frame was complete. A framebuffer read-back still showed fresh data
only at indices 0..486.

**Cause.** `dma_writer.py` computes `write_base_next` for the first chunk of a
new frame, because the display's base register moves in that same cycle and a
`NextValue` lands at the end of it. That expression is "the opposite of
`write_base`" -- and since `write_base` is itself derived as the opposite of
the displayed half, **`write_base_next` is by construction the half currently
ON SCREEN.** That is correct only if the display really does flip in that
cycle. It did not.

`icn1065_soc.py` declared `swap_req` as an input, exactly as `hub75.py` does,
and then **also drove it** from its own `starts[0].frame_done`. Migen collects
every combinational assignment to one signal into a single process with
submodule statements last, so the inner assignment won and the pixel DMA's
frame-boundary pulse never reached `fb_base_eff`. The display therefore never
flipped, and each frame's chunk 0 went to the half on screen while chunks
1..67 went to the half nobody read.

**The one honest signal** was `fb_base_now`: pinned at 655,360 across 25
jittered samples while `refresh_count` climbed steadily. Nothing else in the
system distinguishes "writing the half nobody displays" from "writing the
right half", which is why `/api/status` now reports `dma_base`, `dma_limit`,
`fb_base_now`, `auto_swap_hw`, `auto_swap_sw`, `active_buf` and
`display_base_sw` together. `POST /api/autoswap/on|off` moves the flip between
gateware and CPU without a rebuild.

**Fixed in gateware 1.26.0** by removing the inner driver, so `swap_req` has
exactly one: the pixel DMA, wired in `colorlight.py`.
`tools/sim_icn1065.py::check_swap_req_single_driver` is a source check that
fails if an output stage ever assigns it again.

**The method lesson.** Four separate readings were consistent with two
different stories, and the arithmetic settled it before the instrument did:
`write_base_next == display_base` is an identity in that code, not a
coincidence, so an exact one-chunk boundary could only be the first-chunk path.
An exact, reproducible boundary at a protocol quantity is a LOGIC boundary --
look for the branch that treats that quantity specially, not for a timing or
bandwidth effect. (An earlier read of the same fault put the boundary at 1024
pixels, because `/api/fb` samples every fourth row and the first sample past
row 0 is index 1024. A sampling grid is not a measurement; `/api/fbdump` reads
consecutive addresses.)

### 4.7 Serialiser load latency: the image rotates, it does not truncate

**Measured on glass 2026-10-18.** With the geometry finally correct
(`columns=128`), the four corner blocks of an orientation image were still not
in the corners and the 1px borders were plainly wrong.

The pixel path presents data to the driver chain a fixed number of pixels
BEHIND the shift sequence -- `Icn1065Serialiser` loads `channels_in` through
`NextValue`, one registered cycle, and the gamma stage is zero-latency (it is
combinational precisely so it can sit in this path). Over a row of exactly
`columns` shifts, that rotates each quadrant's content **right**, wrapping at
the quadrant edge.

*It wraps, it does not truncate*, and that is the diagnostic. A feature near
the right edge of a quadrant reappears at that quadrant's LEFT edge. On this
module the red corner block, which belongs hard against the right edge, showed
up **mid-panel** -- the left edge of the right-hand quadrant. One rotation
explains a block being 16 columns out AND another block being 128 columns out,
which no simple offset does.

**The two quadrant columns do not share a pipeline depth.** Measured here:

| groups | quadrants | rotation |
|---|---|---|
| g0, g1 | right half | **17** |
| g2, g3 | left half | **18** |

**HISTORY.** This compensation lived in `patterns.rs` (`quad_src_xy`) and was
then removed when the output stage was fixed to latch after the word reaches
the chain (`854e8ae`, `d1f7b86`); the table is zero and, since 1.18.0, the
function is the identity. Kept because the measurements are how the fault was
found. At the time, the reasoning was: compensate by rotating the source read
by the same amount. That is the exact inverse and loses no columns. **It is
still compensation for a gateware defect**: the output stage should account for its
own pipeline latency so the right `columns` pixels land in each chain, and the
skew between lane groups should not exist. Fix it in `Icn1065Output`, set the
table to zero, and re-measure -- these constants are a property of the BUILD,
not of the panel.

*How to measure it:* bracket on a 1px border. One unit moves one physical
column. Thin features are the only sensitive test -- see the warning below.

### 4.8 A solid-colour pattern cannot find a column fault

This cost more time than any other single mistake, twice in one session.

**Rotating a uniform field changes nothing.** So `quadrants` -- one flat colour
per RGB group -- is blind, by construction, to every column offset, rotation
and starvation fault. It can confirm the group-to-quadrant mapping and
*nothing else*.

The failure mode is believing it. A panel that looks "perfect" under solid
colours and "a mess" under a test image has not changed; you have merely
started using an instrument that can see. Validate geometry with **1-pixel
features** -- borders, single-column markers, a diagonal -- and treat a clean
solid fill as evidence of exactly one thing.

### 4.9 What is MEASURED about this module's column mapping

Recorded as findings, not fixes. Everything here was read off the glass with
single-purpose patterns; the mechanism for the last one is still unknown.

**Adjacent columns are exchanged in PAIRS.** Three independent 1-pixel
features agree: the left border sits one column in with the x=1 block
reaching the edge; the right border likewise; and the 2:1 diagonal shows a
1-pixel sawtooth, its two-pixel-per-row step reordered. `x ^ 1` on the source
column cancels all three -- confirmed on glass. A 19px solid block is
invariant under it, which is why only 1-pixel features ever revealed it.

NOT the framebuffer's pixel packing: `best_pix_bits` returns 32 for this wall
(FB_HALF_BYTES 0x140000 / 4 = 327,680 px against 131,072), so there is one
pixel per word and `half` is None. That explanation was proposed and
falsified; the transformation survived because it rests on the observations,
not on the story.

**`chan_rev` reverses all 16 channels within a chip.** Confirmed directly:
with it set, the diagonal breaks into mirrored **16-pixel segments** -- the
chip width. Useful to know, but NOT the fix for this module; see 4.10.

**The two quadrant columns are one CHIP apart.** With no compensation, the
left quadrants (g2/g3) land correctly -- their 1px border is on the panel
edge -- while the right quadrants (g0/g1) sit 16 columns in.

This cannot come from the source mapping. All four groups are clocked
IDENTICALLY -- same sweeps, same `sect`, same `chan` -- and differ in the
firmware only by which source pixels they read. So no constant in
`quad_src_xy` addresses the cause, which is why fitting one moved the picture
without fixing it, in both directions. It is a property of the MODULE: the
right-hand chains appear to start one chip further along.

Settle it with the vendor's own configuration or a scope (`SCOPE.md` 4),
not by trying offsets on glass. Fitting constants against a photograph cost
most of a day here and converged on nothing.

### 4.10 Colour: the chip displays the range its OWN REGISTERS set (2026-10-19)

**The symptom.** Running the same program with the same pushed values on both
bench cards, `brass (201,168,76)` read as a washed-out near-white on the P1.25
module and as gold on the e75's HUB75 module. Pure primaries were fine -- solid
red, solid green, solid white, the whole `grid` test screen -- so it was not a
lane swap, not a colour order, and not the palette.

**The cause: `reg0x03`.** 11.7.5 gives the chip's displayable PWM range as

```
levels = 4 * (reg0x04[6:0] + 1) * (reg0x03[6:0] + 1)
```

This table wrote `reg0x03 = 0x00` from 2026-10-16 to 2026-10-19, so the module
displayed `4 * 32 * 1 = 128` levels while we sent it 12- and then 13-bit
values. Everything above about input code 56 clipped to full. That is
*invisible* on a pure primary -- a channel that should be full IS full, and a
zero stays zero -- and it flattens every MIXED colour toward white:

```
brass (201,168,76) -> gamma 2.8 -> 4015 / 2374 / 270 of 8191
                      all three >> 127, all three clip to full
```

It also explains the `ramp` pattern photographed that night: the gradient dies
in the first ~50 columns and the rest of each band is flat.

**Why 0x03 was 0, and what it really cost.** The same field is read a second
way (11.6.2): the chip re-displays its SRAM `reg0x03 + 1` times per data frame
and expects exactly one W12 ROW pulse per `passes x scan` ROW signals. Our row
scanner emitted W12 on row 0 of every 64-row pass, so at the factory's 64
passes it would have sent 64 times too many -- the "rain" of 4.4. Setting
`0x03 = 0` made the chip match the scanner. It worked, and nobody looked at a
mixed colour for three days. **The two readings of this field are not
independent: a group count small enough to give a W12 every frame leaves
almost no greyscale.**

**The fix, in the right place.** `Icn1065RowScanner` now counts passes and
asserts the wide pulse on row 0 of every `display_passes`-th pass, so the
factory's `0x03 = 0x3f` can stand: 64 passes, `4 * 32 * 64 = 8192` levels --
exactly the `grayScale: 8192` the vendor profile reports (5c). And the grey
depth is **derived from the register table** (`pwm_range_from_regs` in
`spwm_chips.py`) instead of being a constant that can silently disagree with
it. The pass count is also a runtime CSR (`latch_cfg.passes`), because each
value otherwise costs a power cycle to try.

**The dead end that came first, kept because it cost a night.** Before any of
the above I read "solid green looks dimmer on the E320" as a brightness
problem and raised `GREY_BITS` from 12 to 14 on the strength of 11.4 ("the
part receives the lower 14 bits"), shipping it as gateware 1.21.0 -- in the
same bitstream as the first-ever single-output build. On glass: no coherent
picture, and the `ramp` pattern's full-scale white tick columns came out
BLACK. Three lessons:

* **11.4 is the part's maximum, 11.7.5 is what it is configured to display.**
  Both sentences are correct; I read the wrong one. David, bluntly and
  rightly: *"the data sheets are very likely to be correct - you keep blaming
  them but it is never the case."*
* **One change per bitstream** when a power cycle is the only way to see the
  result. 1.21.0 moved the grey depth AND the output count, so a bad result
  had two suspects and separating them cost a rebuild and a power cycle.
* **The registers were in the repo the whole time.** The answer was not in a
  translated datasheet sentence; it was in `spwm_chips.py`, in a comment that
  had already written down the experiment to run (*"the alternative, untried,
  is to keep the FACTORY value and fix the cadence on our side"*) and had been
  sitting there unread for three days.

**Depth, refresh and the panel clock are ONE coupled choice.** Taking the
factory's 64 display passes makes a complete PWM cycle cost `64 x 64 = 4096`
ROW pulses instead of 64, so the row rate has to rise with it or the fix trades
a colour fault for a flicker fault. Caught by arithmetic before it reached the
glass:

| clk_div | row slot | DCLK | GCLK | a row needs | ROW rate | optical | grey cycle | |
|---|---|---|---|---|---|---|---|---|
| 6 | 128 | 5.83 MHz | 17.5 MHz | 42.7 clks | 45.6 kHz | 712 Hz | **11.1 Hz** | flicker |
| 4 | 64 | 8.75 MHz | 26.2 MHz | 42.7 clks | 136.7 kHz | 2136 Hz | **33.4 Hz** | flicker |
| **2** | **64** | **17.50 MHz** | **52.5 MHz** | 42.7 clks | 273.4 kHz | 4272 Hz | **66.8 Hz** | **ok** |
| vendor profile | -- | 17.9 MHz | 35.7 MHz | -- | ~246 kHz | 3840 Hz | 60 Hz | |

**MEASURED 2026-10-20, AND THE TABLE ABOVE IS WRONG.** Every row of it assumes
the row rate follows `clk_div`. It does not. `refresh_count`, which counts the
row scanner's `frame_done`, reads the SAME rate at every divider:

| clk_div | panel clock | `frame_done` measured |
|---|---|---|
| 4 | 8.75 MHz | 267.0 Hz |
| 6 | 5.83 MHz | 266.4 Hz |
| 12 | 2.92 MHz | 267.2 Hz |
| 24 | 1.46 MHz | 266.3 Hz |

A 6x change in the panel clock moves it by 0.3%. So the scanner is not waiting
on the panel clock at all, and the arithmetic above describes a machine we do
not have. What it is waiting on falls straight out of the number:

    267 Hz x 64 scan rows      = 17.1 k rows/s = 58.5 us per row
    58.5 us x 35 MHz           = 2048 sys clocks per row
    4 groups x 128 columns     = 512 pixel reads per row
    2048 / 512                 = 4.0 sys clocks per pixel, exactly

**The scan-out is memory-bound at four system clocks per pixel.** That is the
whole story, and two separate symptoms are the same fault:

- **The grey cycle is ~4.2 Hz, not 33 Hz.** 64 S-PWM passes at 267 Hz per pass
  is 240 ms for one complete cycle. Streaming at 15 fps therefore assembles
  every pixel's brightness out of three or four DIFFERENT frames. On glass that
  is per-pixel sparkle plus moving edges that are not straight -- a 1-px-per-
  frame sweeping bar showed both, while solid white streamed at the same rate
  was perfectly clean, because blending white with white is white. **A uniform
  field cannot see this fault**; see `validate-on-the-raw-path`.
- **`clk_div` still matters, but for starvation inside the row slot, not for
  the refresh rate.** At `clk_div 4` the 128 shifts take 14.6 us of the 58.5 us
  slot and the memory path cannot keep the shift register fed; at `clk_div 6`
  they take 21.9 us and the sparkle visibly thins. Nothing about the grey cycle
  changes. **The bench therefore runs `clk_div 6`, not 4** -- 4 was chosen on
  2026-10-19 by sweeping against the test screen, which is large flat blocks
  and nearly blind to a stale-pixel fetch.

So `clk_div 2` is not merely unaffordable (4.6) -- it would not buy refresh
rate even if the reads kept up, because the panel clock is not what limits it.
Getting the grey cycle up means **more scan-out read bandwidth**: one pixel per
sys clock instead of one per four would give ~1068 Hz per pass and a ~17 Hz
grey cycle. That is RB-5596 item 5 and the "deeper pixel pipeline" note, and it
is also the real ceiling on driving several modules from one card, since each
one multiplies this demand.

The lower bound on the row slot is the chip's own: a row needs
`4 x (reg0x04 + 1)` GCLKs at `FGCLK = FDCLK x (reg0x06[2:0] + 1)` (11.7.2,
11.7.4) -- 42.7 panel clocks here. The upper bound is flicker. 64 clocks at
`clk_div 2` satisfies both, and lands within a few percent of the
configuration the module was shipped with. `row_slot` is therefore a chip-table
property too, and the preset moved to `clk_div 2` -- affordable only because
the group-read overlap (`63c3880`) removed the starvation that forced 6.

**A fourth trap, which wasted two observations:** a `POST /api/display/pattern`
is overwritten within seconds if marquee is streaming to the card or feeding a
program. The card reports `source: held (native pattern)` while the glass shows
the rail board. Disable the card in marquee before judging any test pattern.


## 5. Bring-up procedure

### 5.1 Find the geometry with a quadrant pattern

Before anything else, paint **one solid colour per RGB group** and photograph
it. One image gives you group count, group-to-corner mapping, scan depth (dark
bands = too small) and stride errors (half width, doubled bands) at once.

This replaced three rounds of guessing for us. Guessing costs a bitstream build
and a power cycle each; the pattern costs one photograph.

**Then immediately stop trusting it.** The quadrant pattern is solid colour, so
it is blind to column rotation and to starvation (§4.7, §4.8). Follow it with a
1-pixel border and a diagonal before believing any geometry is correct.

### 5.2 Instrument before you experiment

Expose, as live HTTP knobs with read-back:

* the chip table actually loaded, and the live `chip_cfg` register
* the row-driver pin mapping and polarity
* the panel shift clock divider
* the gateware-vs-firmware output count

We spent a day sweeping parameters against a panel that could not have lit,
because nothing told us whether the configuration had even been applied. A knob
you cannot read back is not an instrument.

### 5.3 Scope, early

See `SCOPE.md`. Two measurements settle most questions:

* **ROW continuity** (pin 25) at 2 ms/div — a continuous train, or bursts?
* **A data lane under a known pattern** — solid red should put 12-high-4-low on
  R1 and nothing on G1

**Use a 10x probe.** A 1x probe has ~6–10 MHz of bandwidth and turns a clean
square wave into a rounded, drooping mess that looks exactly like a signal
integrity fault. Check the probe before you believe the trace — and set the
scope's probe ratio to match, or every voltage reads ten times low.

### 5.4 Freeze the frame

Stopping the pixel writes while the row scan free-runs gives a **static image**.
Debugging a still is incomparably easier than debugging moving snow, and the
camera integrates out exactly the flicker you need to see.

---

## 5a. The configuration that works

Proven on glass 2026-10-18 on the bench E320 driving one
XP1.25-5958S-256X128-64S module on J1.

```
./build.sh --board e320 --panel 256x128-dp3264s-hub320 \
           --outputs N --sys-clk 35000000 --seed 1 gateware
./build.sh --board e320 --panel 256x128-dp3264s-hub320 --outputs N pac
./build.sh --board e320 --panel 256x128-dp3264s-hub320 --outputs N firmware
```

`N` is the number of HUB320 connectors in use -- one P1.25 module each. The
E320 has eight. Four fit comfortably (see below), and since the Ethernet fix
in 5b the seed no longer matters: seed 1 closes.

**Do NOT use `--outputs 1` to "simplify".** It locks the card to a single
connector, and it does not help timing either -- 5b shows the Ethernet path
is depth-bound, not congestion-bound.

Card layout (`layout_yaml`, served per-MAC over TFTP):

```yaml
grid: 2x1
panel_width: 128
panel_height: 256
J1: 0,0
J2: 1,0
```

| | |
|---|---|
| preset | **`256x128-dp3264s-hub320`** (gw 1.18+): `columns 128, scan 64, groups 4, module 256x128, group_origins [(128,0),(128,64),(0,0),(0,64)]`, chip `dp3264s`. The older `128x256-icnd1065-hub320` stacks the groups and needs a software remap |
| `width_words` | `module_w x n_outputs` -- 256 x 4 = **1024** for four panels (was `columns x n_outputs` = 512 before 1.18) |
| virtual framebuffer | `width_words` wide x **128** tall; layout `panel_width: 256`, `panel_height: 128`, `grid: 4x1`. MUST equal the module the bitstream reports (`hw_columns x hw_rows`), or `module_mismatch` is set in `/api/status` and every raw write scrambles |
| groups -> quadrants | g0 TR, g1 BR, g2 TL, g3 BL -- now the gateware's `group<g>_origin` CSRs, data from the preset, not a pattern remap |
| `clk_div` | **6** as shipped. Before 2026-10-19 that was forced: 4 and below starved (4.6). The group-read fix makes every divider down to **2** fit, and 2 roughly triples the data frame rate -- raise it once 4.6a is closed on glass |
| RGB frame | 135 chunks at two panels, inside the 255 cap. **270 at four panels**, which needs the wide `BW` header (6a) |
| chip registers | the FACTORY values for this module, from the vendor `.rcvbp` -- see 5c. Seven of thirteen differed from what we had |
| `0x03` | **0x00**, one display pass, where the factory uses 0x3f (64). The one deliberate divergence; it pairs with the W12 cadence (5c) |

**Read the registers back rather than trusting the build.** `/api/status`
reports `chip_cfg`, `row_ser`, `latch_phase` and `spwm_configured`, and the
last of those tells you which chip table the FIRMWARE wrote over the
bitstream's built-in default. On this bench it says `dp3264s` while the
bitstream's compiled-in default is `icnd1065`, because the HUB320 preset sets
no `chip` -- so the table that is live is the one the firmware applied, not the
one the gateware shipped. A card whose firmware fails to configure runs the
built-in default silently.

**A live register can be changed without a rebuild.** `/api/chipreg` writes one
row of the chip register table (`{"index":N,"value":V}` or per-lane `r`/`g`/`b`),
`/api/clkdiv` the panel clock, `/api/chipcfg` the row/scan fields, `/api/rowser`
the row-driver mapping, `/api/latchphase` the latch alignment. One register is
sent per frame, so allow ~15 frames. This matters more than it sounds: the
alternative is a seven-minute place-and-route per guess, and that cost is what
turns an experiment into a day.

**The seed used to matter enormously and no longer does.** Before the CDC
buffering of 5b, Ethernet RX swung 97.7-134 MHz against a 125 MHz
requirement and only ~1 build in 15 closed. With it, seeds 1, 2 and 3 all
pass with 1-8 MHz of margin. Never reach for `--allow-timing-fail`: the
failing domain is the gigabit receive path, and streaming video needs it.

**`--outputs 1` is the better configuration** now that the arbiter bug is
fixed (6) -- `width_words` becomes 128, halving the framebuffer and dropping
the phantom J2 half. It was not used here only because the passing seed was
found for `--outputs 2` first.

## 5b. Gigabit Ethernet timing: buffer the MAC CDC FIFOs

If builds fail on `eth_clocks0_rx` at 125 MHz and you find yourself sweeping
seeds, **stop and set two parameters**:

```python
LiteEthMACCore(..., tx_cdc_buffered=True, rx_cdc_buffered=True)
```

Measured on this design 2026-10-18, same seed, `--outputs 4`:

| | eth RX | logic | routing |
|---|---|---|---|
| default (`buffered=False`) | 119.79 MHz **FAIL** | 2.2 ns | 6.2 ns |
| **buffered** | **132.87 MHz PASS** | 1.9 ns | **5.6 ns** |

Before: 46 builds ran 97.7-127.8 MHz, median 116.1, **3 passing (6.5%)**.
After: seeds 1, 2 and 3 all passed (132.87, 126.17, 133.37). The lottery is
gone, and every gateware change stops costing an hour.

### Why "6.2 ns of routing" is not about distance

The critical path is

```
synchroniser -> 3 LUT4 levels of sink_valid -> gray-counter next-state -> BRAM
```

six logic levels in one 8 ns clock. On ECP5 every level crosses the switch
matrix, **~0.9-1.5 ns per hop even between ADJACENT tiles** -- the per-net
delays here were 0.9, 1.0, 0.4, 1.1, 0.4, 1.5, 1.1. So routing time is
*hop count x switch cost*, and hop count IS logic depth. A register on the
FIFO output splits the chain; nothing else has to move.

### Three plausible explanations that are WRONG

Tested on hardware builds, recorded so they are not retried:

* **Congestion.** Doubling the panel datapath (2 -> 4 outputs) left the path
  unchanged: 120.55 -> 119.79 MHz. A fuller chip did not hurt it.
* **Placement / region constraints.** At 4 outputs the cells packed into a
  4x3 tile cluster and it still failed. You cannot constrain away hop count.
* **`with_sys_datapath=False`.** Made it WORSE: 105.90 MHz.

The corollary matters for scaling: because this is depth and not congestion,
**a bigger design is no harder to close**. `--outputs 4` fits at 77% LUT with
block RAM unchanged, and times the same as `--outputs 2`.

## 5c. The vendor configuration, and what it settled

**Obtained 2026-10-18.** The reseller sent the receiver configuration they
use for *this exact panel*: `factory/CLTp1.25-320-160-3264s-7559-64s-6.rcfgx.rcvbp`.
It is the authoritative answer to "what does this panel actually want", and it
is worth asking any supplier for one before reverse-engineering a register
table.

**Who is who** -- this got muddled in the write-ups and is worth one table:

| | |
|---|---|
| Module maker | **Linsn**, by matching the PCB back to linsn-led.com's 320x160 P1.25 module. Asked their published support address for the schematic, mapping and profile; no reply yet. Unconfirmed until they answer |
| Reseller | **ARGO** (contact details withheld). Shipped the module, the E320, a K5+ and a K9+. Writes no firmware |
| Receiver firmware | **Stock Colorlight** -- the images in `factory/` (E320 primary/golden; `K5plus_*.hex`, `K9plus_*.hex` are Colorlight's own receiver builds forwarded by ARGO 2026-10-19) |
| This file | A stock **Colorlight LEDVISION / LEDSetting** receiver profile. Data the stock firmware reads; it contains no code and no vendor-specific logic |

So "the vendor's firmware" and "the reseller's config" in earlier notes both mean:
Colorlight's generic engine, told about this panel by this file.

### The file format

A 32-byte header over a **single zlib stream**, which is how it was identified:
`78 9c` sits at offset 32 and the two u32s at +20 and +24 state the compressed
and uncompressed lengths exactly.

```
offset  0..31   header
        16      u32  version (4 on this file)
        20      u32  compressed length    17919
        24      u32  uncompressed length  199763
offset 32..     zlib stream
```

Decode it with `tools/decode_rcvbp.py`, which finds the structures by
**signature** rather than absolute offset, so it keeps working on a different
config:

```bash
python3 tools/decode_rcvbp.py factory/CLTp1.25-...rcvbp --pixel-map
```

Inside the inflated blob:

| offset | what |
|---|---|
| 4, 5 | `columns`, `scan` -- plain bytes: **128, 64** |
| 94..109 | data-group map, 16 slots, `0xff` unused: **`[8, 9, 0, 1, 10, 11, 2, 3]`** |
| 173409.. | per-pixel coordinate map, 3 bytes `(x_lo, x_hi, y)`, 128x64 |
| 198321.. | **driver register table**, 4 bytes `(addr, r, g, b)` x 13 |

### The driver register table -- SEVEN OF THIRTEEN DISAGREED WITH OURS

Our table had come from a *different* profile,
`CD-P1.2-256X128-DP3264+32020-64S--9.51.rcvbp`: same pitch class, same 256x128,
same 1/64 scan, decoded through LEDSettingServer's `getChipRegisterInfo_DP3256`.
Close enough to light the panel and not close enough to be right.

| reg | factory | ours was | what it is |
|---|---|---|---|
| `0x02` | 3f | 3f | LINE_SET, 64 scan lines |
| `0x03` | **3f** | 00 | display passes per frame - 1 (**we still diverge, below**) |
| `0x04` | **1f** | 13 | PWM_WIDTH / GCLK per row |
| `0x05` | 05/04/04 | 05/04/04 | eliminationTime |
| `0x06` | **42** | 41 | PLL_DIV |
| `0x07` | 00 | 00 | gamma fine/coarse |
| `0x08` | bf | bf | IGAIN |
| `0x09` | 60 | 60 | DECOUP_1 |
| `0x0a` | **be** | fe | (unnamed) |
| `0x0b` | **28**/2b/31 | 29/2b/31 | eliminationLevel |
| `0x0c` | **8c** | 9c | SYNC_MODE |
| `0x0d` | **08/12/18** | 0b/08/17 | coupledOptimizationLevel |
| `0x11` | 00 | 00 | (unnamed) |

All of them are in `gateware/spwm_chips.py` now except `0x03`.

### Three things it settled

**SYNC_MODE 2 is correct.** Factory `0x8c` has bits [7:6] = `0b10`, the same
mode as our `0x9c`. This had been argued both ways in this repo, and the comment
heading that value read *"SYNC_MODE 0: GENERAL FRAME SYNCHRONISATION"* while the
code emitted mode 2 -- prose arguing for a value it did not set. That comment is
why mode 0 was tried on glass at all. Mode 0 needs continuous writes to keep
getting a VSYNC; with the write-once policy it goes dark after one frame.

**The pixel map is the identity. All 8192 entries.** This paragraph has been
rewritten twice and the history matters. The records are `(y, x_lo, x_hi)`.
Searching for the byte run `00 00 00 01 00 00 02 00 ..` finds the table one
byte LATE and frames every record as `(x_lo, x_hi, y)`; in that framing every
in-row entry still reads as identity and the last entry of each row reads
`(127, r+1)`, because its "y" is the next row's first byte. I reported that
first as "one artifact", then -- after the dots were fixed -- as the vendor
pre-compensating a pointer+1 commit, one carry per row. Both were the same
off-by-one in `decode_rcvbp.py`. David's "does not make sense" was correct:
a 1-per-row carry cannot compensate 8 cascaded chips, and it didn't have to,
because it isn't there. Anchored one byte earlier the map is 8192/8192
identity and the stray last value (`0x24`) is the byte after the table.

What that leaves: the receiver profile tells the stock firmware *chip type,
geometry, data groups and registers*, and the stock firmware's per-chip
driver does the rest. Nothing about data order or commit alignment is in the
file, so the file can neither confirm nor refute `sweep_adv`. It also means
there is no vendor table to ingest for it; the per-chip behaviour belongs
where it is, in the chip table.

**The geometry is 128 x 64 per group**, confirming section 1a's arithmetic
against the spec sheet's "1/64 scan, 256x128".

### 0x03: the one place we knowingly diverge

Factory `0x3f` means **64 display passes per data frame** (datasheet 10.7.1:
`REG0x03[6:0] = refresh rate / frame rate - 1`). We set `0x00`, one pass.

We changed the chip to match our scanner rather than the reverse. Datasheet
11.6.2 step 5 says there is one W12 per `(groups x scan rows)` ROW pulses, so
the factory value implies **one W12 every 64 x 64 = 4096 ROW pulses**, while
`Icn1065RowScanner` emits one every 64. Writing `0x3f` without changing the
cadence gives the chip 64x too many W12s, and its internal data pointer then
cycles with period 4096 against a 64-long external row chain. The two drift and
the glass shows "rain".

**This is the strongest open lead on the 16-pitch dots** (section 4.6a), because
the quantity we overrode is precisely the one governing how the chip's internal
data pointer advances against the external row chain -- which is the artifact's
exact shape. The experiment is to keep the factory value and count passes in the
scanner, asserting `row_pulse_first` only on row 0 of every 64th pass. Not yet
tried on glass.

---

## 5d. The bench setup, concretely

Everything below was read off the running system, not remembered. It is here so
that operating this bench needs no rediscovery -- which has cost real time
before, most expensively when a bitstream was pushed under a name that did not
match what the card reports running.

### What is where

| | |
|---|---|
| Gateware + firmware source | **`/share/src/colorlight`** -- on NFS, the same path on every host |
| Canonical git remote | `origin` = `ssh://the git host/home/git/colorlight.git`; `github` mirrors it. **Push both.** |
| marquee (the host service) | `/srv/docker/marquee` on **the server host**, API on **`localhost:8410`** |
| Vendor artifacts | `factory/` -- flash references, the receiver config (§5c), K5+/K9+ firmware |

### The two cards marquee knows

| id | name | IP | MAC | mode | what it is |
|---|---|---|---|---|---|
| 1 | `bench` | 192.168.1.70 | `02:00:5e:00:53:03` | program | 5A-75E, 128x64, the railboard. **Do not break it.** |
| 2 | `e320` | 192.168.1.62 | `02:00:5e:00:53:02` | stream | the E320 under test, `--outputs 2`, drive `dp3264s` |

**Updated 2026-10-21: the E320 now drives TWO P1.25 modules**, J1 above J2, as
one 256x256 picture. Its layout is `grid: 1x2`, `J1: 0,0`, `J2: 0,1`, with
`panel_width: 256`, `panel_height: 128` coming from the card's module type
rather than from the layout file.

Mind the three things that must agree, because two of them fail SILENTLY:
`--outputs 2` on the build, `pub const OUTPUTS: u8 = 2` in
`sw_rust/barsign_disp/src/hub75.rs` (set by hand -- `--outputs` does not touch
it, and `LayoutConfig::parse()` drops surplus connectors with no error), and
the card record's bitstream name. `build.sh` compares the first two and
refuses; nothing checks the third but the card reports what it is running.

**The card answers HTTP but not ICMP.** `ping` failing means nothing; use
`curl http://192.168.1.62/api/status`.

### The two artifacts update over the network, and they are different things

| | firmware | bitstream |
|---|---|---|
| file | `boot.bin` | `.bit` |
| lives | **nowhere on the card** -- refetched over TFTP every boot | the card's SPI flash, offset 0 |
| live when | immediately, on reboot | **the next POWER CYCLE** |
| version | its own semver, `Cargo.toml` | its own semver, `gateware/VERSION` |

TFTP is **port 6969**, not 69, and marquee owns it -- the gateware sets
`TFTP_SERVER_PORT` and the default server is `192.168.1.10`.

```bash
# firmware: upload, then assign (assigning reboots the card)
curl -F 'file=@.tftp/boot.bin;filename=e320-spwm-v2.84.0.bin' \
     http://localhost:8410/api/v1/firmware
curl -X POST http://localhost:8410/api/v1/cards/2/firmware \
     -H 'Content-Type: application/json' \
     -d '{"firmware":"e320-spwm-v2.84.0.bin"}'

# bitstream: upload, then push to flash (a minute or two; no reboot)
curl -F 'file=@bitstreams/e320/256x128-dp3264s-hub320.bit;filename=e320-256x128-dp3264s-hub320-out1-v1.25.0.bit' \
     http://localhost:8410/api/v1/bitstreams
curl -X POST http://localhost:8410/api/v1/cards/2/bitstream \
     -H 'Content-Type: application/json' \
     -d '{"bitstream":"e320-256x128-dp3264s-hub320-out1-v1.25.0.bit"}'
```

**The bitstream filename must be the identity:**
`<board>-<panel>-out<n>-v<semver>.bit`, and the semver must be the one in
`gateware/VERSION` **at the time it was built**, because that string is
compiled into the identifier ROM and is what the card reports running. marquee
compares reported against pushed to tell "already live" from "written, awaiting
a power cycle". Rename a bitstream and those can never agree -- which is
exactly how a v1.14.0 image spent a session being called v1.21.0.

### Build order, and why it is an order

```bash
./build.sh --board e320 --panel 256x128-dp3264s-hub320 --outputs 4 --sys-clk 35000000 --seed 1 gateware    # writes the SVD too
./build.sh --board e320 --panel 256x128-dp3264s-hub320 --outputs 4 pac
./build.sh --board e320 --panel 256x128-dp3264s-hub320 --outputs 4 firmware
```

The PAC is generated from the SVD and the SVD is written by the gateware build,
so a CSR added or **moved** without re-running both leaves the firmware
addressing every later register one slot low -- silently, with endpoints
answering 200 off the wrong register. `./build.sh svd` regenerates the SVD in
seconds with no place-and-route, and two freshness guards now refuse a stale
SVD or PAC. **Always pass `--sys-clk 35000000`**: the E320 is a `-6` grade and
the 40 MHz default does not close.

### Changing something without a rebuild

The live knobs, all POST with a JSON body, all read back in `/api/status`:

| endpoint | body | what |
|---|---|---|
| `/api/clkdiv` | `{"n":6}` | panel clock = sys/n, n >= 2 |
| `/api/chipreg` | `{"index":10,"value":3212}` | one row of the chip register table (§5c) |
| `/api/chipcfg` | `{"row_pulse":4,...}` | the row/scan half of `chip_cfg` |
| `/api/rowser` | `{"a_src":0,"inv":7,...}` | row-driver pin mapping and polarity |
| `/api/latchphase` | `{"n":1}` | extra DATA_LATCH pulses (§4.6a) |
| `/api/display/pattern` | `{"name":"hstripe"}` | a test pattern |
| `/api/display/off` | — | freeze: stop writes, keep scanning |

One chip register is sent per frame, so allow ~15 frames for a `chipreg` write
to land. This matters more than it sounds: the alternative is a seven-minute
place-and-route per guess.

### Reading the card

```bash
curl -s http://192.168.1.62/api/status | jq
```

The fields worth knowing: `gateware` (the identifier ROM -- what is ACTUALLY
running), `spwm_configured` (which chip table the FIRMWARE wrote over the
bitstream default), `refresh_count` (FSM frames; **freezes when writes stop, by
design**), `latch_phase`, `chip_cfg`, `row_ser`, `hw_*` (the bitstream's own
geometry), and `output_mismatch`.

**A crash breadcrumb survives a soft reset but NOT a power cycle.** If the card
wedges, read `/api/status` BEFORE pulling power or the evidence is gone.

---

## 6. Traps that cost us real time

* **`refresh_count` is not evidence of output.** It counts the scan-out state
  machine inside the FPGA and increments happily with nothing attached and every
  pin on the wrong ball.
* **The card answers HTTP but not ICMP.** `ping` failing means nothing.
* **The crash breadcrumb does not survive a power cycle.** Read it *before*
  pulling power.
* **Components verified in isolation are not verified together.** Our pixel
  source and serialiser each passed their own tests; the bug lived in the
  handoff that nothing instantiated jointly. Test the seam.
* **A test can encode the bug.** Ours asserted "row scan runs after the pixels,
  never during the prefix" — the exact behaviour that kept the panel dark. It
  passed for a year.
* **Count the pulses, measure the gaps.** Counting ROW pulses per frame cannot
  detect a 262,144-clock hole. Assert on the longest gap instead.

---

* **`--outputs 1` was not impossible, it was a one-line bug.** Both arbiters in
  `icn1065.py` sized their tag signals `bits_for(n_sources - 1)`, which is ZERO
  for a single source, and the tag FIFO collapsed to one entry Migen cannot
  build a port for. That assert is why every earlier session built `--outputs 2`
  for a card driving ONE connector -- which doubles `width_words`, and that is
  the root of the stride, the overflow and the 255-chunk streaming limit below.
  Check a degenerate case before designing around it.
* **`pkill -f <pattern>` matches the shell running it.** Three times in one
  session this killed the very command issuing it, once leaving a wait spinning
  for 25 minutes against a process that no longer existed. Kill by PID, or make
  the pattern unmatchable (`stream_[v]ideo`).
* **Kill a waiter when you kill what it waits for.** A loop like
  `until [ -f /tmp/good.txt ] || grep -q DONE log; do sleep 40; done` never
  exits once its sweep is cancelled -- the file is never created and the
  marker never written. Three such waiters accumulated in one session, one of
  them stalling a check for 25 minutes against a process that no longer
  existed. Cancel the waiter in the same breath as the work, or give it a
  deadline.
* **A background waiter outlives its purpose.** Four stale waiters from earlier
  in a session were still armed to `PATCH` a card's layout, firmware and
  bitstream to superseded values when their condition finally tripped. If the
  panel changes with nobody touching it, suspect those first.
* **The host protocol caps a frame at 255 chunks** (`chunk_index` is a `u8`).
  At 512x256 an RGB888 frame is 270 chunks and simply cannot be sent; the card
  also rejects a short frame, so trimming rows is not a way out. Indexed mode
  is NOT supported on this S-PWM driver -- a frame of one repeated palette
  index renders as noise, which is the one-step way to prove it. Getting
  `width_words` right is therefore what makes streaming possible at all.

### 6a. Scaling to four panels on one card

Measured 2026-10-18. Four P1.25 modules on one E320, `--outputs 4`:

| | |
|---|---|
| `width_words` | 128 x 4 = **512** |
| virtual framebuffer | **512 x 256** = 131,072 px = exactly 4 x (256x128) |
| device usage | **77% LUT** (18828/24288), **73% BRAM unchanged** (41/56) |
| Ethernet timing | same as 2 outputs -- see 5b, it is depth-bound |
| SDRAM read | 10-20 MB/s of ~70 MB/s peak -- **not** the limit |
| RGB frame | 393,216 bytes = **270 chunks** |

Two things bite, and only one is hard:

**The wire protocol caps a frame at 255 chunks.** `chunk_index` is a `u8`, so
124,185 RGB pixels is the ceiling -- under four panels. The card also rejects
a short frame, so trimming rows is not a way out. Four panels therefore need
the WIDE header: magic `'B','W'` with u16 chunk counters, a separate magic so
the existing `'B','M'` path stays byte-for-byte identical and cards on older
firmware keep working.

**Packet rate, not bandwidth, sets the frame rate.** Measured ceiling is
~10,000 pkt/s clean (~14,000 at 0.1% loss), and the card is only using ~12%
of the gigabit link at that point -- per-packet cost dominates. So:

| panels | chunks/frame | fps at 10k pkt/s |
|---|---|---|
| 1 | 68 | ~147 |
| 4 | 270 | **~37** |

Four panels at 30 fps is comfortable; 60 fps needs the per-packet cost
reduced, not more bandwidth.

**Three defects sat in the wide path and all three bite only above 255
chunks** -- i.e. only on a four-panel RGB888 wall, which is why indexed
streaming (1461 px per chunk, 90 chunks) never found them. Fixed 2026-10-19:

* The DMA arrival merge indexed off the end of its array. `MASK_WORDS` is
  `MAX_CHUNKS / 64 = 8`, but the merge pairs `words[2*i]` with `words[2*i+1]`
  over `i in 0..8`, i.e. up to index 15 on a `[u32; 8]`. It **panicked on the
  first wide frame** and the card rebooted. Correct when `MAX_CHUNKS` was 256;
  widening it to 512 for four panels broke it and nothing else was widened.
* Only **256 of the gateware's 512 arrival bits were exported** -- `arrival`
  is `Signal(512)` and `chunk_index[0:9]` sets bits up to 511, but the CSR
  export loop covered eight words. Chunks 256.. were invisible to the CPU, so
  a wide frame could never complete on DMA evidence even with the panic fixed.
* **`total_chunks` truncated to 8 bits.** A 270-chunk frame published `270 &
  0xff = 14`, so `received >= total` declared the frame complete after about
  5% of it had arrived -- a torn frame presented as a good one.

### The refresh budget, after the group-read fix

Section 4.6's fix changes the arithmetic that mattered most here. Per-pixel
fetch cost went from `4L + 16` to `L + 5`, so at L=16 a pixel costs 21 cycles
against `16 x clk_div`:

| `clk_div` | panel clock | pixel budget | fits? | data frame (256x64x16 clk) |
|---|---|---|---|---|
| 2 | 17.5 MHz | 32 | yes | ~15 ms -> **~67 Hz** |
| 4 | 8.75 MHz | 64 | yes | ~30 ms -> ~33 Hz |
| 6 | 5.83 MHz | 96 | yes | ~45 ms -> ~22 Hz |

Before the group-read overlap only `clk_div=6` was safe, capping the data frame
rate near 22 Hz. **The preset now ships `clk_div = 4`** (gateware 1.25.0),
measured on glass. The "raise it to 2" that used to stand here is FALSIFIED: 2
snows even with a single module, because 32 system cycles per pixel does not
cover a LiteDRAM round trip plus refresh jitter. Reclaiming it needs a deeper
pixel pipeline, not a bigger budget -- which is the same work as pipelining the
stride multiplier for 3- and 5-output builds.

*What still has to be done to scale past four outputs:* the arbiter pipelines
across sources and the source now pipelines within itself, but 8-9 contending
sources still need sys/6 at L=16 (`check_pipelined_arbiter` prints this as a
HEADROOM line). Four outputs fit sys/2 with margin; nine do not.

## 7. Sources

* **`factory/CLTp1.25-320-160-3264s-7559-64s-6.rcfgx.rcvbp`** -- the vendor's
  own receiver configuration for THIS module, and the authoritative source for
  its driver registers. Decode with `tools/decode_rcvbp.py` (§5c).
* `docs/datasheets/DP3364S_Rev1.0_EN.pdf` -- §10.1 command widths, §10.2 data
  order (channel 15 first, 1024 positions per frame), §10.5 the per-frame
  command order, §10.7 display modes. Note the part on the board is marked
  **DP3264S**; the supplier never resolved which (§1).

* `DP3264S_REV2.0` datasheet — §11.1 command widths, §11.2 data order, §11.4
  register protocol, §11.5 ROW signal, §11.6 display modes
* Raffar `RT5958D` v1.6 datasheet — the serial row driver
* [`board707/DMD_STM32`](https://github.com/board707/DMD_STM32) —
  `DMD_Multiplexer.cpp` has a working 595-type row mux; its wiki classifies
  driver and mux chips by family
* [`hzeller/rpi-rgb-led-matrix`](https://github.com/hzeller/rpi-rgb-led-matrix)
  issues #466, #1226, #1866 — S-PWM chips and the RT5958 pin mapping
* `chubby75` — the 5A-75B/E reverse engineering this all stands on

## 8. See also

* `HUB320.md` — the connector in detail, and the pin 20/21 hazard
* `ICN1065.md` — the DP3264S protocol and register tables
* `SCOPE.md` — probe points, expected waveforms, what good looks like
* `HARDWARE.md` — the card itself
* `PINWALK.md` — mapping an unknown card's connectors with a meter
* `FLASHING.md` — the only procedure for getting a bitstream or firmware onto a
  card, including the over-the-network path that avoids JTAG entirely
* `BENCHMARKS.md` — where the bandwidth actually goes
* `tools/decode_rcvbp.py` — read a vendor receiver configuration (§5c)
* `tools/sim_icn1065.py` — the 120-odd checks; `./build.sh sim-icn1065`

## 9. How to extend this document

Two rules, both learned the hard way and both visible in the wrong turns
recorded above.

**Record what the panel did, not what you concluded.** Several entries here are
of the form "X was tried and the glass disagreed". Those are the most valuable
paragraphs in the file, because each one is a day somebody else does not spend.
A conclusion without its observation cannot be re-checked when the next fact
arrives -- and on this board the next fact routinely overturned the story
(§4.9, §5c).

**Put the evidence next to the constant.** Every magic number in this repo that
lacks a measurement beside it has been wrong at least once. When a value is
compensation for a defect rather than a property of the hardware, say so where
the value lives, so the next person deletes it when the defect is fixed instead
of preserving it forever.
