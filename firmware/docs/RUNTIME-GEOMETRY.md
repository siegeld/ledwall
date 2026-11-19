# Runtime geometry: one bitstream per chip family

**Released in 5.8.0 (2026-11-18)**, merged from branch
`exp/runtime-geometry-cols`, with Myled's P1.25 seen on glass on it. "The
branch" below is that work. Started 2026-11-15. It answers one
question: can module shape become configuration without losing the fit or the
timing of the four-output E320 build?

It can. Seven builds of seven close timing, with room, and since 2026-11-16 it
**runs on the bench card** (gateware 3.6.2, firmware 5.8.0, Myled's P1.25):
see "On the card" below. Build it with `exp/build-rt.sh`.

## What is on the branch

| Knob | What it does |
|---|---|
| `EXP_BLOCK_SEQ=1` | The row fetch takes one RGB group per run even when two groups share a framebuffer row. A same-row pair read one after the other stays in one DRAM row, so ARGO's 2x2 loses nothing (sim: 64 row misses of 512, as before) and Myled's stacked strips get what their own bitstream gave them (128). **One bitstream then serves every arrangement of four 128x64 groups**; the only build-time difference left between the two presets is reset values. |
| `EXP_RT_GEOM=1` | `icn1065.RtGeometry` and a `geom` CSR: columns per chain and scan lines as registers, inside the build's maxima. `tools/sim_rtgeom.py` shows the output stage clock-for-clock identical to a build of the smaller size; `RT=1 tools/sim_fillrate.py` the same fill and pixels. Columns stay a multiple of 16 and the scan count even. |
| `EXP_DMATEST=0` | Leaves out the SDRAM write tester's engine and keeps its registers (`dma_writer.SdramWriteTesterStub`). About 450 cells, which is what runtime geometry needed. `/api/dmatest` then reports zero and the `xbar` list in `/api/status` is one master shorter. |
| (always on) `gateware/chain.py` | The two 125 MHz RX-domain paths that made every build a seed lottery, pipelined. See below. |

## The timing failures were ours

Every failing seed of gateware 3.5.0 failed on the same two paths, both in
`ChainUpstream`, the card-chain FIFO:

* **Gray-to-binary conversion** of the FIFO pointers. Written as independent
  XOR reductions "two LUTs deep", synthesised as a ten-LUT ripple: 8.3 ns
  against 8.0. Now two registered steps, each one LUT deep by construction.
* **The FIFO write path**: frame counter and PHY valid, through the terminator
  mux, into the block RAM's data pins; and the fullness compare into the write
  pointer. The write port, the fullness flag and the max-length compare are now
  registered (fullness with two more slots of margin for the extra cycle).

`sim-chain` passes and `sim-chain-deep` passes 20/20 with these in.

## Measurements

E320, four outputs, `exp/build-out4.sh gateware`, ARGO preset, 24,288 cells.
RX is the two Ethernet receive clocks (need 125 MHz); sys needs 35.

| Build | Seed | RX0 / RX1 MHz | sys MHz | Cells |
|---|---|---|---|---|
| SEQ only (no chain fix) | 2 | **121.0 / 123.5 fail** | 48.7 | 24,012 |
| | 7 | **121.7 / 121.9 fail** | 50.2 | |
| | 3 | 135.0 / 147.7 | 49.1 | |
| + chain fix | 2 | 129.9 / 180.7 | 49.2 | 24,185 |
| | 7 | 143.0 / 170.2 | 45.2 | 24,166 |
| + chain fix, RT_GEOM | 2, 7 | does not fit | | 24,398 / 24,527 |
| + chain fix, DMATEST=0 | 2 | 144.0 / 165.6 | 49.3 | 23,638 |
| | 7 | 141.2 / 185.8 | 48.1 | 23,604 |
| + chain fix, DMATEST=0, RT_GEOM | 7 | 143.4 / 211.7 | 49.2 | 23,894 |
| | 2 | 143.7 / 182.0 | 46.7 | |
| | 3 | 138.1 / 212.2 | 47.1 | |
| | 11 | 145.0 / 170.7 | 50.0 | |
| | 5 | 137.0 / 205.8 | 49.7 | |
| | 1 | 136.2 / 203.5 | 48.3 | |
| the same, Myled preset, with the hold (3.6.1) | 7 | 138.2 / 195.2 | 49.3 | 23,714 |

Worst case of the full build over six seeds: 136.2 MHz RX, 46.7 MHz sys. One
earlier run of seed 2 failed to route in 45 minutes while other builds were
using the machine; alone it routed in under six.

## The firmware half (5.8.0)

* `gateware/colorlight.py` writes `RT_GEOM`, `RT_GEOM_MAX_COLUMNS` and
  `RT_GEOM_MAX_SCAN` into soc.h; `build.rs` turns them into `cfg(rt_geom)` and
  constants, so the register is only touched by a firmware built beside a
  bitstream that has it.
* Config keys `group_columns:` and `group_scan:` (one RGB group's chain length
  and the scan lines). Absent means the build's own size, so re-posting a
  layout without them undoes one that had them. `apply_layout` turns the
  display off, writes `geom`, configures the chip with the same scan count and
  restarts the display.
* Refused, changing nothing, and reported as `geom_refused: 1` in
  `/api/status`: columns not a multiple of 16, an odd scan, anything past the
  maxima, or either key on a bitstream without the register (unless it states
  the size the bitstream already is). Status also carries `geom_columns`,
  `geom_scan` and `geom_max`.
* `module_mismatch` no longer fires for a module of the same four groups in a
  different arrangement: when the config gives `group_origins`, the test is the
  pixel count (groups x columns x scan), not the preset's width and height.
* A boot with no config lays out the bitstream's own module when the image
  compiled into the firmware is another size. (An ARGO-built 5.5.0 on the
  Myled bitstream came up at stride 256 on a 128-wide module.)

## On the card (2026-11-16)

Bench E320, Myled P1.25 on J1, `PANEL=128x256-icnd1065l-hub320 SEED=7
exp/build-rt.sh`: 138.2 / 195.2 MHz RX, 49.3 MHz sys, 23,714 cells (97%).
Flashed over the network, power cycled.

| Geometry (`group_columns` x `group_scan`) | Refresh | Row fill, sys clocks | Underruns/s |
|---|---|---|---|
| 128 x 64, the module's own, idle | 63.2 Hz | 3690 | 0 |
| 128 x 64, 20 fps stream, `clk_div` 4 | 63.1 Hz | 3940 | 0 |
| 128 x 64, 20 fps stream, `clk_div` 3 | 84.1 Hz | 3944 | 0 |
| 96 x 48 | 110.2 Hz | 2756 | 0 |
| 64 x 32 | 239.0 Hz | 1825 | 0 |

795 frames of a 20 fps stream: 0 chunks dropped, 0 partial frames. On 3.6.2,
ten minutes at 20 fps: 12,000 frames of 12,000 whole, 816,068 chunks, 0
dropped, 0 underruns. The
`ticker` program turned 270 degrees runs with 0 underruns. Ten switches
128x64 <-> 64x32 back to back leave it filling normally. `group_columns: 72`
with `group_scan: 33` is refused; from firmware 5.8.1 a refusal puts the
build's own size back (128x64 here) rather than leaving the running one.

The plug the bench hangs on reports watts and serves as a light meter (below).
At 128x64 the branch bitstream draws what 3.5.0 draws: white 27.3 W against
27.5, red 19.5 / 19.6, green 18.3 / 18.4, blue 15.8 / 15.9, black 12.7.

### The fault the card found: a geometry write stopped every fill

The first branch bitstream (3.6.0) went dark on 128x64 -> 64x32 and stayed
dark after switching back, until a reboot: 480,000 underruns/s, no fill ever
completing, and the plug at black-level watts under a white frame. A fill
caught by a shorter chain had already counted past the new last column, which
it only ever tests for equality; its requests stopped at the new threshold, so
the column it was waiting for never came.

`RtGeometry.hold` (3.6.1): for 4096 clocks after every write of `geom` the
line buffer drops its fill and forgets its rows. That outlasts any read still
in flight, which matters as much as the hang: reads left over from the old
fill would be counted into the next and shift every row after it.

`tools/sim_rtgeom_fill.py` is the test: source, three-slot line buffer and
arbiter, the geometry rewritten at clocks that land inside and between fills.
Without the hold both mid-fill switches end with 0 fills, as on the card;
with it all nine come back with whole rows of the right data.
`tools/sim_rtgeom.py` gained the same off / write / on sequence for the output
stage, which recovers by itself.

### Display off did not blank (fixed in 3.6.2, not a branch fault)

`ctrl.enabled = 0` stops new frames; the row scan runs on and the column chips
keep showing what they last latched. At the plug a white frame drew white's
watts for as long as the display was "off", on 3.5.0 and on the branch alike.
Withholding the row chain's start token does blank it (27.3 W -> 13.0 W, tried
live through `/api/rowser`), so the token is now gated on `enabled`
(`icn1065_soc.py`, `ser_din`). 3.6.2: white 22.6 W, off 15.5 W or less, on
again 21 W and rising. Both P1.25 modules use the serial row chain, so both
get it. Seed 7: 136.8 / 202.0 / 49.2 MHz.

The pin driven as the chain's blanking (BK, pin 18) made no difference to the
watts held high or held low.

### After the review: 5.8.1, then gateware 3.6.3 / firmware 5.8.2 (2026-11-18)

A three-reviewer read of 5.8.0 found no fault in a card that boots at its own
size, and several in changing it (CHANGELOG, 5.8.1 and 5.8.2). What changed in
the design:

* The firmware turns the display off, **waits for the frame in flight to end**
  (`wait_frame_end`), and only then writes the geometry. A refusal puts the
  build's own size back and is reported; a size is refused if it does not
  parse, or if no chip table is named to be told the scan count.
* The gateware **clamps the register**, ends a counter that is already past a
  shrunken limit (`_at_last`: `>=` against a runtime limit, `==` against a
  constant), and starts no frame while `hold` is up.
* Group origins are checked (16-pixel grid, inside the module) and a config
  without them gets the preset's back.

Posted to the card as configs and read back from `/api/status`, on 3.6.2 with
5.8.1 and again on 3.6.3 with 5.8.2: 64x32 applied; `72`/`33`, an unparsable
scan and a size with no `drive:` each refused and back at 128x64; origins off
the grid or outside the module flagged; ten switches 128x64 <-> 64x32 leave it
at 0 underruns/s, 63.2 Hz. 3.6.3 closes at 138.1 / 210.1 MHz RX, 48.1 MHz sys.
Then a show streamed by marquee 0.46.2 (its own quarter turn, the module type's
registers): 10 fps, 0 underruns, 0 dropped, the framebuffer read back with
every edge and seam line in its row.

### What the bench card is left running

Bitstream `e320-128x256-icnd1065l-hub320-out4-v3.6.3` and firmware
`e320-spwm-v5.8.3-bench8` in flash: 5.8.3 with `exp/bench-myled.cfg` baked in
(`exp/build-rt.sh --default-config exp/bench-myled.cfg firmware`), because no
boot server reaches that card. The config turns the picture 90 degrees (270 hung upside down on the bench,
seen on glass 2026-11-18) and
lays the vendor profile's per-colour registers over ours, all but 0x02-0x04
(docs/PANELS.md, "The vendor's registers at the plug").

Since 2026-11-18 it drives **two modules**, J1 the left and J2 the right, as
one 512x128 picture, and marquee streams to it: card `myled-bench`, module
type `myled-p1.25-128x256-icnd1065`, `rotate` 90, layout `grid: 1x2`,
`J1: 0,1`, `J2: 0,0` (turned 90, the framebuffer's last rows are the left of
the picture). Which connectors carried a module was read off the bench plug:
each output lit white in turn, 24.7 W for J1 and J2 against 16 W for J3 and J4.

## Not done

* ~~ARGO's module on this bitstream.~~ Done 2026-11-18: a second E320 with
  three ARGO P1.25 modules stacked, gateware 3.6.3 built for that module (SEQ
  fetch on its 2x2), firmware 5.8.3, streamed by marquee: 10 fps, 0 underruns,
  0 dropped chunks, 66.8 Hz, fill 3399 clocks.
* ~~The chain fix on two cards.~~ Done 2026-11-18: two E320s cabled port 2
  to port 1, both gateware 3.6.3 / firmware 5.8.3, six ARGO P1.25 modules as
  one 512x384 wall. The member took its hop from the head's rewritten
  discovery, fetched its layout up the chain, and both cards took 211 frames
  in 20 s at 10 fps with 0 underruns and 0 dropped chunks. On 2026-11-19, with
  firmware 5.8.5 and marquee 0.46.6 pacing the chain's cards from one loop:
  video at 20 fps, 800 frames in 40 s on each card, 0 dropped, 0 held.
* **A module that really is smaller.** 64x32 and 96x48 were run on the 128x64
  module: the counters show the stage running at that size, not a picture.
* **Group count** is still fixed at four per connector, and the line buffers
  are sized for the maxima, so a module bigger than 128x64 per group is still
  a different build.
* `row_rot` wraps at the build's scan count, so the firmware (5.8.1) turns it
  off at a smaller runtime scan. It is 0 in every table.
* Growing the size costs one frame of underruns (129 at 64 lines), like a boot:
  the line buffer starts empty. Not visible, and left as it is; from firmware
  5.8.3 `underruns` counts from once the display has settled and
  `underruns_raw` is the register.
* The SDRAM write tester is gone from this build (`/api/dmatest` reports 0).
