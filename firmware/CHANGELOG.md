# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.13.0] - 2026-09-21

### Added

**`tools/grab_fb.py`** — capture what is actually on a panel's glass, as a PNG.
Reads the framebuffer back over `/api/fbdump` and recolours it through the
panel's live `/api/palette`, which is the only way to see what a program is
really drawing without standing in front of the panel — and, unlike a
screenshot of what you *sent*, it catches the difference between the two.

**It refuses on a fullcolor panel rather than returning a plausible lie.**
`/api/fbdump` sends only the low byte of each word, which is one RGB channel
there and not a palette index, so a colour capture is impossible. The failure
is convincing if you let it through: the layout is still correct, because a
channel still varies with the content. A wrong screenshot is worse than no
screenshot, because you act on it.

Implements the two traps documented in v2.12.2 — `y0` sent as a string, and the
little-endian palette triplets — so a reimplementation is not needed.

Both paths verified against a live panel: refused on a streamed `rgb` wall with
no file written, and captured correctly from a program-mode one.

## [2.12.2] - 2026-09-21

### Documented

**How to read the framebuffer back, and the two things that will mislead you.**
`/api/fbdump`, `/api/palette` and the real behaviour of `/api/fb` were missing
from the HTTP reference entirely, and reading a panel back is the only way to
check what is actually on the glass without standing in front of it.

- **`y0` is parsed as a STRING.** `{"y0": 16}` reads as absent and returns rows
  0–15 again. The symptom is a capture that repeats the top of the screen down
  the whole image.
- **It emits only the LOW BYTE of each framebuffer word**, and what that byte
  means depends on the output mode: in `indexed` it is the palette index and
  maps cleanly through `/api/palette`; in `fullcolor` it is one RGB channel and
  the other two are not sent, so a colour capture is **impossible** in that
  mode. Running a channel through a palette produces confetti that looks
  exactly like a corrupt framebuffer — the tell is that the layout is still
  correct, because genuine corruption does not respect a layout. A tool that
  captures colour should refuse on a non-indexed panel rather than hand back a
  plausible lie.
- **The palette hex is little-endian.** `/api/palette` reports brass `#C9A84C`
  as `4ca8c9`.

All four checked against `network.rs` and against a live panel before writing.

## [2.12.1] - 2026-09-20

### Documented

**Three things every shipped program uses that the programmer's guide never
mentioned.** Found by auditing `docs/PROGRAMMING.md` against what
`railboard.yaml` and `dashboard-home.yaml` actually do:

- **`lambda` has two phases, `row:` and `px:`.** Used five times across the
  shipped programs and documented zero times — §5 showed only the plain
  block-scalar form, which runs per pixel. This is the single most important
  performance tool a lambda has: `ctx.val()` costs ~79 cycles, and calling it
  per pixel to answer a question constant across the row made one lambda the
  most expensive thing on a panel *after* the declarative widgets had been
  fixed. Also documents the generated `v_<key>` per-row locals.
- **`value` takes `prefix` and `suffix`.** Used six times, documented nowhere,
  and the widget table did not list them. Includes the counter-case: put the
  unit in a label when the number can get long, which is why one panel's
  header reads `POWER kW` rather than suffixing a value that overflows 128px.
- **The value table holds 24 keys and drops the 25th in silence.** `set()`
  ignores a key once full rather than evicting, so the key that vanishes is
  whichever went last, and nothing raises or logs. `dashboard-home` lost its
  power trace to exactly this when the limit was 16. The cap on key and value
  length was undocumented too.

Also corrects `when`, which the guide implied was general: it is accepted on
`text`, `value` and `icon`, not on `border`.

## [2.12.0] - 2026-09-20

### Added

**`custom/example/panels/railboard.yaml`** — a station departure board composed
on the card, and the second worked example. Four rows of time, destination and
countdown, fed entirely by pushed values from marquee's rail providers, with the
destination column **scrolling** when a name does not fit.

The program is **generic**: there is no Metro-North, LIRR or Amtrak in it and no
page logic. It binds `sys`, `hdr`, `foot` and twelve row values, and the host
pushes whichever board should be showing. Nine pages of nine boards would be
~180 widgets each with a `when` guard, to say something the host already knows.

### Documented

Four findings from building it, all in `docs/PROGRAMMING.md` §7:

- **A scroll BAND cannot scroll one column.** `scroll:` is the cheap path — it
  shifts the glass and re-renders only the exposed columns, ~2 memory accesses
  a pixel against ~5 — but a band is full width and drags the static columns
  along with the moving one. A scrolled sub-row column has to be drawn in a
  lambda with those rows in `dynamic`. Measured on the bench card: **30.3 fps**
  static (the frame-period cap) against **7.9 fps** with one scrolling column
  and 86 of 128 rows dynamic. Roughly 4x, and worth knowing before choosing a
  layout.
- **Drive a scroll from `time_ms`, not `ctx.frame`.** The scrolling is what
  drags the frame rate down, so a per-frame step makes the speed a function of
  the work the speed is causing. Stepping 1px every 2 frames crawled at ~6 px/s
  once the card had gone from 30 fps to 13, and the column read as *empty* for
  most of a page. `time_ms / 40` is 25 px/s whatever the card manages.
- **Take the advance from the generated font, not the TTF.**
  `DejaVuSansMono-Bold` at 11 measures 6 on the outline and comes out **7** in
  `src/assets.rs`. Budgeting from the outline put a clock 3px off the right edge
  and butted two columns together.
- **Check what power-of-two padding costs before using it.** It makes a
  scrolled wrap a mask instead of a division, but `North White Plains` is 18
  characters and rounds to 32 — 112px of blank dragged past a 56px window, so
  the column reads as empty for most of the page. Three spaces and a division
  read far better.

## [2.11.0] - 2026-09-20

Everything here came out of preparing the public release, including two
corrections to things this repo had been saying.

### Fixed

- **The CPU has the M extension, and several comments said it did not.** The
  `lite` VexRiscv has always had hardware multiply and divide, the build sets
  `target-feature=+m`, and the binary carries 426 MUL/DIV instructions with an
  ELF arch tag of `rv32i2p1_m2p0_zmmul1p0`. The target *triple* is named
  `riscv32i`, which is what misled — but `docs/BENCHMARKS.md` has a section
  headed "The M extension — 6× for one linker flag" saying so plainly. What is
  actually true is the **absent D-cache**. The optimisations stand; every one
  was measured. Only the explanation was wrong.
- **`--revision` could not produce a working build.** It defaulted to `7.0` and
  its help offered `6.1` or `8.0`; the platform asserts on
  `["6.0", "7.1", "8.2"]`, so all three crash. Nobody noticed because
  `build.sh` always passes `8.2`. Now has `choices`, and says why it matters:
  the pinouts differ *and* so does the ECP5 speed grade.
- **`BENCHMARKS.md` still warned that indexed has a colour-artifact bug** and
  told readers to prefer `rgb`. That was fixed in v2.7.0 by double-buffering the
  palette in gateware. Rewritten in the past tense, keeping the war story — every
  counter stayed clean while it happened — and noting that the fix lives in the
  **bitstream**, which does not arrive with a firmware netboot.
- **The publish scan was blind to any file extension nobody had listed.**
  `is_text()` gated both the transforms *and* the forbidden scan on a suffix
  allowlist, so `.conf` and `.patch` files shipped unscrubbed and unscanned —
  two of them carrying a hostname, an internal app name and an internal
  address, all patterns already in `forbidden.txt`. It decides from content now.
  Found by hand-grepping an export the scan had just called clean.

### Added

- **A public-repo landing page.** The first export was two bare directories and
  an empty README panel. The manifest had no way to express a top-level file, so
  it gained a `root:` section, staged before the transforms and the scan. README,
  CONTRIBUTING and LICENSE now sit at the root.
- The worked example the extension point is supposed to ship. Deleting
  `hello.yaml` as a test program had left `custom/example/` with no program in
  it, so the public repo documented an extension point and demonstrated nothing.

### Changed

- The crate version was `1.40.0` while the tags were at `v2.10.0` — and that is
  what stamps a published release.

## [2.10.0] - 2026-09-20

### Fixed

**The pause on every value arrival is gone.** A value arrival owes the whole
non-scroll canvas a repaint, and that was done in ONE frame: ~75ms on top of a
~31ms frame, twice (once per buffer). The sign hesitated once per push.

It is now **progressive** — each tick pays off at most 12 rows of whatever the
back buffer still owes, and the rest of the frame proceeds normally. Measured
over 30 seconds with pushes every ten:

    before   32 31 31 36 33 32 31 29 27 31 32 32 31 35 32
    after    32 31 35 32 31 36 31 31 34 31 33 31 32 37 30 32 36 31 ...

Indexed by **buffer**, and the index comes from the gateware rather than from
`active_buf`: with `auto_swap` on the flip happens without the CPU being told,
so tracking it in software would fill one half twice and leave the other stale
for ever. Rows already drawn by the declared `dynamic` band are skipped, as are
scroll bands — the shift carries those into both halves anyway — so a program
with no `dynamic` band drains its owed rows without drawing anything twice and
nothing regresses.

The cost of spreading it: a changed value arrives as a wipe down the panel
rather than all at once. At this size that reads as a transition; a hitch never
does.

Verified that it converges rather than merely being smooth: the clock on the
glass read 12:18 against a stored `hhmm` of 12:18, and four rapid framebuffer
dumps hashed identically.

## [2.9.1] - 2026-09-20

### Fixed

- `cover_at` indexes a monospace run instead of walking it, as `cover_scroll`
  already did. Every `text`/`value` widget in a fixed-advance face was doing up
  to one glyph iteration per character for every pixel across its width.

### Notes

Answering "is the pause the webserver?" — measured, it is not. About 10 HTTP
requests a second cost 32.2 → 30.8 fps (~1.4ms per request), and real traffic
is a few requests per *ten* seconds. Stopping value pushes gives a flat 31-38
fps with no dips; restarting them brings the dips back. The remaining hitch is
the value-arrival repaint: the tape rows are skipped since v2.9.0, and what is
left is ~75ms of centre rows, nearly all of it the clock and temperature.

## [2.9.0] - 2026-09-20

### Fixed

**The sign paused once every ten seconds.** Measured: steady 31-35 fps with
dips to 16 and 20, eleven seconds apart, against player value pushes eleven
seconds apart — exact correlation. Every value arrival armed
`PROGRAM_FULL_LEFT = 2`, and a full canvas repaints both scroll bands with
`cover_scroll`: ~250ms each, twice, per push.

The tapes never needed it. A scroll band's content is a pure function of
`x + frame*speed` and the shift propagates it into both buffers every frame;
they need an outright paint only when the program *starts*, because then the
buffers hold nothing to propagate. `PROGRAM_PRIME_LEFT` is now separate from
`PROGRAM_FULL_LEFT` — activation owes both, a value arrival owes only the rows
drawn from values.

**Both tapes were mostly blank.** Padding sets the scroll PERIOD, so it must be
the next power of two *above* the text, not far above it. v2.8.0's power-of-two
padding left `ticker` at 38 characters in a period of 64 and `timessquare`'s
lower tape at 62 in a period of 128 — a 66-character blank stretch. Both
messages now fill their periods.

**The clock and the temperature overlapped by three rows.** The 26px mono face
inks +6..+24 from the band top and the 13px sans +4..+12; at 48 and 66 that is
54..72 against 70..78.

### Added

**An attract loop on `ticker`.** An eight-second beat: seven seconds of
concentric rings rushing outward under the clock, then a two-second blast where
they accelerate, saturate and strobe with the bulbs, the type and the tape
grounds together.

None of it writes a pixel. Each centre pixel takes the index of the *ring* it
sits on — Chebyshev distance, two `abs_diff`s and a `max`, no multiply, since
a squared radius would be two multiplies and a compare per pixel where this is
three single-cycle operations. Rotating those entries in the palette makes the rings rush outward;
dropping their brightness together makes the sign flash. 256 words a frame,
flat in wall size.

Held to ~60% of full scale rather than 255 — a HUB75 module at this pitch is
painful at full white, and the gateware applies gamma downstream. The chasing
bulbs are at 70% for the same reason: they ring the whole frame and are lit
constantly.

### Changed

- `/api/palette` dumps all 256 entries rather than the first 64, which could
  not see the rainbow band at 64..191 or the bulbs at 208 — exactly where the
  demos animate.

## [2.8.0] - 2026-09-20

### Removed

- `hello` and `dashboard` — test programs. What ships is `blank` (the floor),
  the two demos, and `dashboard-home`.

### Changed

**The two demos are rewritten as showpieces, and as arguments.** The panel is a
40 MHz VexRiscv with **no D-cache**, so a read costs about what a write does and
anything per-pixel happens 16384 times a frame. Both files now say where their
cycles go.

| program | fps | what it is |
|---|---|---|
| `blank` | 247 | the floor: one store per pixel and nothing else |
| `ticker` | 29.4 | two tapes, a centred clock, a rainbow bulb ring |
| `timessquare` | 16.3 | plasma, spinning sprite, sparkle, chasing border, two live tapes — full canvas, every frame |

### Fixed

Everything below was found with `/api/profile`, not guessed at:

- **`plasma` was four software divides per pixel** (`x/3`, `y/3`, `(x+y)/5`,
  `/12`), 16384 pixels a frame. Shifts now, plus a `PlasmaRow` that hoists the
  y term out of the pixel loop. The spatial frequency changes slightly and
  nothing else does.
- **`sprite_rotated` paid two multiplies for every pixel on the panel** to
  answer "no" for 92% of them — a 26px star occupies 1369 of 16384 pixels. It
  rejects on a bounding disc first, and a star row now costs barely more than a
  plasma row (0.80ms against 0.69ms).
- **`cover_scroll` did three software div/mod per pixel** while
  `fixed_advance` was already 16 — the compiler cannot know a runtime struct
  field is a power of two. `Font` states `adv_shift`, and the demos pad their
  messages to a power-of-two length so the message-index modulo becomes a mask
  too. The tapes were 85% of a full frame; a full canvas went 251ms → 196ms.
- **`RAMP_WATER` collided with `RAINBOW`.** Added at 64 in v2.6.0, where the
  rainbow starts and runs 128 entries — and `palette_default` tests ramps
  first, so `hue()` returned water-blue for the first sixteen hues of every
  sweep. Moved to 192. Nothing using only ramps would have seen it; anything
  using `hue()` would have, and it would have looked like a broken rainbow.

### Added

- `Font.adv_shift`, and `PlasmaRow` — a row-hoisted plasma, for use from a
  `lambda`'s `row:` block.

## [2.7.0] - 2026-09-19

### Fixed

**The palette is double-buffered in gateware, and indexed mode is usable again
(RB-5516).** In indexed mode a pixel is `ramp_base + coverage`, so the colour of
everything on the glass comes from a 256-entry table. The framebuffer was
double-buffered and swapped in gateware; the palette was one table the CPU
rewrote in place. Rewriting it while the scan-out reads it renders a palette
that is half old and half new — once per change, and a palette changes on any
scene with a clock in it, because median cut re-runs and every entry shifts.

Every counter stayed clean through this, which is what made it expensive to
find: nothing is dropped or late, so the frames are perfect and the colours are
not.

The palette memory is now 512 entries — two banks — and the live one is selected
by `fb_base_eff`, the same signal the scan-out and the write DMA already follow.
Deliberately **not** a second swap register: a second register is a second thing
to keep in step, and the two would eventually disagree for exactly one frame.
Derived, the palette and the pixels that index it cannot come from different
frames, and it works identically whether the flip came from a CSR write or from
gateware `auto_swap`.

Cost was nil: the palette was 256x32 = 8 Kbit in a 16 Kbit EBR, one block at
half occupancy. Two banks is exactly one full block. 48/56 EBRs, system clock
58.98 MHz against a 40 MHz target.

**The second bug, which would have been worse than the first.** marquee sends a
palette only *when it changes* — typically once at scene start, not per frame.
Writing only the back bank would have left the other bank on the previous
scene's colours indefinitely, and since the banks alternate every frame the
panel would have flipped between right and wrong colours on *every* frame. The
firmware keeps a shadow of the last palette with a per-bank generation, and
`sync_palette_bank()` catches up the bank that is not displayed, after the swap,
never while it is live — a no-op unless a palette arrived since that bank was
last written.

Programs do not use that path: an animated palette writes its own frame's bank
every tick, and a static one goes into every bank once via `set_palette_all()`.
Filling only the back bank would leave the other black until the second frame,
so a static palette would flash the right colours and then nothing.

`hw_palette_banks` is read from the gateware rather than assumed, because the
firmware netboots while the bitstream lives in flash and the two are not updated
together. An absent CSR reads 0 and falls through to the single-bank path, so an
old bitstream keeps the old behaviour.

Verified against the symptom rather than the mechanism: the bench wall streaming
`dashboard-home` at `format: indexed`, palette changing as the clock ticks, live
bank alternating every frame, both banks in step on every sample across several
scene changes — 3588 frames, 0 bad magic, 0 DMA stalls.

### Added

- `/api/palette` — both banks and which one is live, so the double buffering
  stays checkable from outside rather than by reading the gateware.

## [2.6.0] - 2026-09-19

### Fixed

**An on-panel program rendered a frame every 8.3 seconds — 20268 cycles per
pixel against 11 for a bare framebuffer store.** Three guesses at why were all
wrong, so this started with a profiler, and the first thing the profiler found
was that there was nothing to profile with.

`mcycle` does not exist on this VexRiscv. `csrr 0xB00` assembles, executes, and
always yields 0 — the same reason `main()` notes that reading back `mtvec`
gives 0. `/api/bench` read it, so **every cycle count it has ever printed was
that zero**, and the firmware comments derived from it ("~190 cycles per
framebuffer store", "a full 128x128 redraw caps near 13 fps") were fiction. A
store is ~6 cycles amortised and an empty frame fills in 2 ms, so the real
ceiling was never 13 fps.

`/api/profile` and a rebuilt `/api/bench` run as deferred jobs on the main loop,
because `TIME_MS` is the only working timebase and it cannot advance inside the
trap handler where HTTP is serviced. Best of three, so an interrupt burst is not
reported as a slow row.

What that measured, and what was done about it:

- Everything frame-invariant was evaluated **per pixel**. The band test, the
  `when:` guard, the value lookup and the centring `width()` depend only on `y`,
  but ran once per widget for all 16384 pixels — and a four-page program asks
  `page == 'N'` in every widget, so a pixel paid for ~24 table scans. `panelc`
  now emits one function per row: a prologue, then the pixel loop.
- `Values::get` re-**validated UTF-8** on every slot it compared, and again on
  the value it returned — data that `set()` copied out of a `&str` and that
  cannot be invalid.
- `cover_at` walks the glyph run from the start of the string for every pixel.
  The prologue already computes the text width for centring, so it now also
  rejects on the horizontal extent, which is most of a row.
- A `lambda` is verbatim code `panelc` cannot analyse, so it kept paying
  `ctx.val()` per pixel and became the most expensive thing left. It may now
  declare `row:`, spliced into the same prologue.

**8.30 s → 0.34 s per frame.** The panel is also idle between value pushes now:
a program with no `dynamic` band, no scroll and a static palette is a pure
function of its values, so it draws two frames per push and nothing in between.

**A program-mode panel shared its framebuffer with streamed video.**
`set_pixel_filter()` turns on `auto_swap`, and marquee's player re-enables pixel
DMA on any card it believes is streaming — so the gateware flipped buffers
mid-render while video was DMA'd into the same buffer. It read as a rendering
bug: flicker and smeared glyphs. `auto_swap` now follows `!PROGRAM_ACTIVE` and
is re-asserted each tick. The real fix is at the wall: content belongs to the
wall, and a card running a program is not showing it.

**Every hero number on `dashboard-home` was the wrong face at two thirds the
size.** `panelc` deduplicated assets by the NAME a program gives them, so the
first program to define `big` defined it for all of them — `hello.yaml`'s `big`
is DejaVuSans-Bold 20, so `dashboard-home`'s DejaVuSans 30 was silently
discarded. Assets are now keyed by file+size and each program binds its own
aliases at the top of its render function, so a hand-written lambda can still
say `FONT_TICK` and mean its own.

**The power trace rendered as a solid slab, then as nothing at all.** `VAL_LEN`
was 24 against a 60-sample series, and the series is normalised to its own
min..max before sending — so a truncated window is not a shortened trace but a
wrong one. With that fixed the trace vanished: `MAX_VALUES` was 16, the program
binds exactly 16, and `set()` ignores a key once full rather than evicting, so
the last key sent (`hist`) never arrived. Now 64 and 24.

**Text overlapped on two pages.** `at` is the top of the band, not of the ink,
and the ink offset depends on the string — offsets measured from all-caps
labels are wrong for anything with an ascender or descender. The clock's digits
sit 8 rows lower than a capital and ran into the date; "39 gal" is 12 rows
taller than "39" and its `g` ran into TODAY.

### Added

- `/api/profile` — per-row cost of the running program, so the expensive band is
  identified by measurement rather than by reading the generated code.
- `/api/fbdump` — full-resolution framebuffer dump, 16 rows per request.
  `/api/fb` subsamples 1 pixel in 4, which cannot answer "does this glyph look
  right" — and that question is what separated a rendering bug from two writers
  sharing a buffer.
- `panelc`: `prefix`/`suffix` on a `value` widget, so "HUMIDITY 71%" is one
  string and can be centred as a unit; `row:` on a `lambda`; a `water` colour
  ramp; `FONT_<name>_LH` as a compile-time constant.
- `Program.palette_static` — a palette whose body never reads `ctx` is written
  once instead of every frame. Partial mitigation for RB-5516: the palette is
  single-buffered while the framebuffer is gateware-swapped, so every rewrite is
  scanned out while it happens.

### Changed

- `dashboard-home` is now a faithful port of the streamed show rather than a
  paragraph of it: OUTSIDE, the degree sign, HUMIDITY n%, the GARAGE section and
  its OPEN row, TOTAL, RECENT LOAD, TODAY, n ZONES and RUN n restored; headers
  centred; the border dropped. Verified by dumping all four pages off the panel
  and measuring every gap — nothing under 2 rows, and no ink on rows 63-65, the
  seam between the two physical panels.

## [2.5.0] - 2026-09-19

### Fixed

**The gateware dropped about one pixel packet a second, and it showed as a
stale band in streamed video.** `burst_fifo` was 512 words deep, and a
1500-byte packet is 375 words — so it held about 1.4 packets. `SmolEthGate`
abandons a packet mid-flight the moment that FIFO refuses a beat, so any stall
longer than one packet cost a whole chunk.

A lost chunk is not recoverable when the hardware pixel filter is on. The CPU
sees no pixel packets, holds no pixel data, and therefore **cannot repair a
frame** — `chunks_repaired: 0` was never "nothing is lost", it was "repair is
impossible". The frame is still presented and the missing region keeps whatever
was in that half of the double buffer, which is the frame from TWO frames ago.
Only the badly damaged frames were counted at all: `frames_dropped` needs more
than `MAX_REPAIR_CHUNKS` missing, so everything milder was shown with a bad
band and counted nowhere. That is why every counter looked healthy.

FIFO is now 2048 words, about 5.5 packets.

Measured on the bench card, 120-second windows, same content and pacing:

| | before | after |
|---|---|---|
| gate drops | 0.86/s | **0.00/s** |
| frames carrying a stale band | 4.33% | **0%** |
| fps | 19.8 | 19.9 |

At the original faster pacing (`packet_delay` 0.00025) it is 1.67/s before and
0.18/s after — an 89% cut. Zero needs the FIFO *and* 0.0006 pacing, which costs
no frame rate because the show's own `fps: 20` is the limiter.

Cost: EBR 44 → 48 of 56 (79% → 85%), LUTs 62%, timing 57.85 MHz against a
40 MHz requirement.

### Added

`dma_stalls` in `/api/status` — payloads the pixel DMA abandoned after the
SDRAM write side stalled. Added in the same build deliberately, because it is
what distinguishes "the FIFO is too shallow for a momentary stall" from "the
write side is genuinely starved by the display re-reading the whole framebuffer
every refresh". It reads **0** throughout, which is what confirms this was FIFO
depth and not SDRAM bandwidth — otherwise a deeper FIFO would only have delayed
the loss.

RB-5524.

## [2.4.0] - 2026-09-19

### Added

**Per-card RGB channel order**, settable at runtime. The IC and the panel's own
wiring decide which logical colour each HUB75 pin group carries, and until now
that was fixed in two places -- the board pin map and the slice assignments in
`Output` -- so a module that disagreed needed a gateware rebuild or swapped
wires. `panel-check`'s RGBW swatches could DETECT a wrong order; nothing could
correct it.

- `rgb_order` CSR (3 bits, reset 0) selects one of `RGB RBG GRB GBR BRG BGR`.
  Named the way LED parts are: the value is what the PANEL expects, so `GRB`
  means the first pin group carries green. Reset 0 is the standard order, so
  every existing bitstream behaves exactly as it did.
- One 8-bit 6-way mux per pin group, between the fetched framebuffer word and
  the pins. The `0x00GGRRBB` word layout is a property of the FORMAT, not of
  the panel, so nothing upstream has to know a panel is unusual.
- `rgb_order:` in the card's boot config (also `color_order`/`colour_order`),
  taking the NAME rather than a number -- a digit there would be unreadable and
  easy to get wrong by one. An unknown name falls back to RGB rather than
  failing the whole layout: a typo should cost you the colour order, not the
  panel.
- `GET`/`POST /api/rgborder` to try an order live. Finding the right one is a
  look-and-see job -- change it, glance at the panel, change it again -- so
  making each attempt cost a config edit and a reboot would turn a two-minute
  task into a long one. The API finds the answer; the config records it.

Per CARD, not per module: all of a card's outputs run in lockstep off one
engine, so there is one order for the board. Modules on a card that disagree
cannot both be right, which is worth knowing before buying them.

### Measured

Timing is unchanged: 61.12 MHz, PASS at 40 MHz (56.48 before, so the mux costs
nothing measurable). Verified on hardware -- `POST /api/rgborder` with `GRB`
returned `{"rgb_order":2,"name":"GRB"}` and visibly changed the panel's hues;
`RGB` restored it.

## [2.3.0] - 2026-09-19

### Added

**The BIOS gets its boot server from DHCP option 66, and its own identity from
the flash.** `patches/litex-bios-dhcp.patch`.

Two changes that only work together:

- **Per-card MAC, derived from the SPI flash unique ID** in `net_init()`, using
  the same XOR fold as `sw_rust/barsign_disp/src/flash_id.rs` so the BIOS and
  the firmware present the *same* identity. Before this the BIOS used one
  compiled-in MAC for the whole fleet, so every card netbooted as the same host
  at the same address -- which is why the boot server had to identify cards by
  ARP, and why a DHCP reservation could not target one.
- **A minimal DHCP client** in `libliteeth/udp.c`. Option 66 wins, then BOOTP
  `siaddr`, then the compiled-in `REMOTEIP`, so a card still boots where
  neither is served. Option 66 rather than `siaddr` because this estate's DHCP
  is Windows Server, which leaves `siaddr` zero and only serves 66/67.

### Fixed

**Upstream: broadcast receive was dead code.** `process_frame()` drops every IP
packet whose `dst_ip` is not ours *one level above* the broadcast branch in
`process_udp()` -- so `udp_set_broadcast_callback()` and that entire branch
could never fire for a real 255.255.255.255 datagram. DHCP is exactly the case
that needs it: a client with no address yet can only be answered by broadcast.
Every reply was being discarded before it reached the callback. The patch lets
broadcast survive that test when `ETH_UDP_BROADCAST` is set.

This was invisible from the card -- no serial is wired to the Tigard on this
board, only JTAG -- and was found by packet capture from the marquee host,
which sits on the same subnet: the DISCOVER went out, the OFFER came back 3ms
later, and the card asked again as though nothing had arrived.

### Measured on hardware

| | before | after |
|---|---|---|
| TFTP request source | `192.168.1.50` (compiled-in, same for every card) | `192.168.1.70` (its own reservation) |
| card identification | `identified as bench ... via ARP` | direct, by host |
| boot server | compiled into the bitstream | DHCP option 66 |

Option 66 was proven to actually override, not merely agree: with option 66
temporarily pointed at `192.168.1.99`, the card ARPed for `192.168.1.99` rather
than the compiled-in `192.168.1.10`.

DHCP costs **6.6 ms** (DISCOVER to ACK, measured). BIOS DISCOVER to the
firmware's own DHCP is ~760 ms, covering DHCP, the 305 KB TFTP fetch and
firmware start. The no-server path is bounded by two tries and gives up rather
than retrying eight times, because netboot runs before flashboot here and every
card pays it on every boot.

`ETH_UDP_BROADCAST` is now enabled in `gateware/colorlight.py` (it was
commented out with a TODO).

### Still true

`marquee` can now identify a booting card by its source address, so the ARP
fallback in `services/player/main.py` is no longer the only thing that works.
It is kept: a card whose bitstream predates this still netboots as
`192.168.1.50`.

## [2.2.0] - 2026-09-19

### Added

`--tftp-server` build flag. The address the BIOS fetches `boot.bin` from was a
bare literal in `gateware/colorlight.py` -- the marquee host welded into every
bitstream with nothing naming it. Moving marquee to another machine silently
broke fleet netboot, and the address was a **DHCP lease**, so it could have
moved on its own.

Default is unchanged (`192.168.1.10`), so no bitstream behaves differently; the
value is simply visible and overridable now. `build.sh` carries it as
`TFTP_SERVER`.

### Infrastructure (not in this repo, recorded here because bitstreams depend on it)

- The TFTP host now has a **DHCP reservation** rather than a dynamic lease,
  because the address is compiled into every bitstream.
- **DHCP option 66** (`Boot Server Host Name` = `192.168.1.10`) is served to the
  two colorlight cards, set **per-reservation** (`192.168.1.70`, `192.168.1.50`)
  rather than scope-wide -- the Laboratory scope holds ~20 unrelated lab
  devices and none of them should be told about a boot server.

Nothing reads option 66 yet. The BIOS has no DHCP client at all (`libliteeth`
ships `udp.c`/`tftp.c` and zero lines of DHCP), so it still uses the compiled-in
`REMOTEIP`. Teaching it to prefer option 66 is RB-5517; the DHCP side is in
place and waiting for it.

## [2.1.0] - 2026-09-19

### Fixed

**The pixel filter was eating the palette.** An indexed stream drew the right
shape in the wrong colours with every counter on the card clean. Three correct
decisions left a hole: the classifier kills UDP traffic to the pixel port
before the CPU's MAC (worth 33 fps against 5.5), a palette packet goes to that
same port, and the DMA drops the `'P'` magic commented "palette, CPU's job".
Killed by one and dropped by the other, the palette reached nobody.

The classifier now defers its verdict one beat -- it latches the port match at
beat 9 and at beat 10, where the payload magic sits, spares `'BP'`. A palette
is ~800 bytes once a second against ~140 pixel packets a second, so the pixel
path is untouched. `tools/sim_classifier.py` covers it with 9 cases.

Measured on the bench card, filter ON, indexed: `palette_writes` 145 -> 175
over 30s (one per second, as sent) while `packets_valid` stayed frozen at
10973. Before the change `palette_writes` could not advance at all.

### Added

`/api/dma/arrival` now reports the gateware's latched frame (`done.seq`,
`frame_id`, `total_chunks`) and the guards `present_done_frame()` returns on
(`rx.last_done_seq`, `cpu_saw_packet`, `hw_filter_on`, `pending_palette`).
With the pixel filter on the CPU sees no pixel packets, so a stalled
`frames_completed` looks exactly like a healthy card -- this is the difference
between reading the answer and flashing firmware to find it.

### Known issue

**Indexed mode shows colour artifacts whenever the palette CHANGES** -- which
means any scene carrying a clock or live data. The framebuffer is
double-buffered and swapped by gateware while the palette is single-buffered
and written by the CPU, so the indices and the palette on screen can belong to
different frames. Every counter stays clean throughout; the only reliable test
is switching the same content to `format: rgb`.

Tracked as RB-5516, with the fix (double-buffer the palette, flip it on
`swap_req`) written up there. Prefer `rgb` where it fits: at 128x128 RGB888
sustains 30 fps measured with zero drops, so indexed buys nothing on a small
wall. See `docs/BENCHMARKS.md`.

## [2.0.0] - 2026-09-19

A major bump because the output stage is no longer one thing: the gateware now
carries a second, table-driven S-PWM stage beside the HUB75 bit-plane one, and
the panel preset decides which a bitstream gets.

**The HUB75 path is unchanged and was verified on hardware before release** --
program mode and stream mode both run on a bitstream and firmware built from
this tree, flashed to the bench panel. The stream delivers exactly 16,384
pixels per frame (128x128) at the show's configured 10 fps, with bad_magic and
CRC errors both zero.

**NOTHING in the S-PWM stage has driven a panel.** No ICND1065 or DP3364S
module has ever been connected to this board. Every constant in it is
transcribed from third-party drivers or captured by other projects, and the
first thing to distrust if a panel comes up dark is the scan register.

### Added -- S-PWM output stage

An ICN1065 / S-PWM output stage, for the P1.25 modules. **Nothing here has
driven a panel**, and none of it is wired into the SoC yet. Simulated (80
checks, `./build.sh sim-icn1065`) and placed against the real device
(`./build.sh fit-icn1065`).

### Added
- **`gateware/icn1065.py`** — command encoder, prefix sequencer, register
  block, continuous serialiser, row scanner, pixel source and gamma unit.
- **`Icn1065PixelSource`** — two framebuffer reads per request, upper and lower
  half of the panel, with a `GAP` cycle between them. Without it a memory that
  holds `ack` high serves the second read from the first read's data, and all
  six channels come back identical. The simulation caught exactly that.
- **`Icn1065Gamma`** — 8-bit framebuffer to 12-bit SPWM through a real 2.8
  curve, piecewise-linear over 16 segments, within 0.24% of the true curve and
  matching its software twin `gamma12()` on all 256 codes. `hub75.py` corrects
  8 bits to 8, which flattens the first 28 codes to black; this flattens 8.

  It is combinational because the serialiser reloads every 16 clocks whether or
  not data arrived — latency in this path does not stall, it tears the image
  sideways. Three converters per output, not six: the two halves of a pixel are
  read on different cycles, so one set serves both. That halving is worth about
  3,900 LUTs, and `check_pixel_source(gamma=True)` guards the latch it relies
  on.

- **`Icn1065PipelinedArbiter`** — and it is the piece the whole design rests
  on. A blocking round-robin arbiter has one read in flight, so a pixel pair
  costs `2 x sources x latency`; at a realistic 16-cycle LiteDRAM latency that
  is 291 clocks and a **7.7 Hz** wall. The data fits the SDRAM budget
  comfortably — a blocking arbiter simply converts a latency into a throughput
  limit. Issuing commands without waiting and matching returning data through a
  tag FIFO brings the same case to 46 clocks, and the test asserts the linear
  bound `4n + 2*latency` rather than a number.

  `Icn1065Arbiter`, the blocking one, is kept as the thing the tests compare
  against. Had the stage been built from the bandwidth arithmetic alone and
  wired to it, the wall would have run at 7.7 Hz and the cause would have looked
  like SDRAM rather than like arbitration.
- **`Icn1065SharedGamma`** — one set of three converters for all nine outputs.
  Behind one arbiter every source reads the same returning data bus, so all nine
  compute the same function of the same input at the same instant and differ
  only in when they latch. That is 3 converters instead of 27, and takes gamma
  from 6,231 LUTs (26% of the device) to 1,361 (5.6%). Written out explicitly
  rather than left to Yosys to notice: a saving that depends on the synthesiser
  spotting something is a saving nobody can reason about.

- **HUB320 support**, as a parameter rather than a second design.
  `GROUPS_HUB75 = 2` and `GROUPS_HUB320 = 4` set the RGB groups a connector
  carries; `Icn1065Serialiser`, `Icn1065Output` and `Icn1065PixelSource` follow
  it. HUB320 is a width change, not a protocol change -- same CLK, LAT and OE,
  same driver ICs, twelve data lines instead of six driving four horizontal
  slices instead of two.

  It costs pins, not bandwidth: a request fetches four pixels rather than two
  but each covers twice the panel, so bytes per pixel is unchanged. 16 HUB75
  outputs and 8 HUB320 outputs cover the same pixels for the same 63 MB/s, and
  `check_hub320()` asserts exactly that.

  Eight HUB320 outputs, 524,288 px, placed: 5,669 LUTs (23%), 3,313 FF (13%),
  0/56 block RAM, 99/197 IO (50%), timing PASS at 70.7 MHz. That is **1.8x the
  pixels of the nine-output HUB75 wall for 3% more logic** -- logic is not the
  constraint, and IO is the honest ceiling at 16 outputs (192 pins of 197).

  Four sequential reads off one bus means THREE gap cycles rather than one, and
  the leakage test is the point of `check_hub320()`: with a memory entitled to
  hold `ack` high, every group after the first would otherwise get the first
  group's colour.

  Not built: the pin mapping, which is card-specific and which vendors do not
  agree on (NovaStar ships HUB320 and HUB320F; Colorlight, Huidu and Sysolution
  each differ). A config table, written once a card is chosen.

### Fixed
- **The fit harness was measuring itself.** `mem_dat` was an undriven
  `Signal(32)` — a constant zero — and `mem_ack` was tied to `mem_dat[0]`, so
  the ack never arrived, every pixel source stalled forever and Yosys deleted
  the whole data path. The harness then reported that adding nine read paths
  made the design *smaller*, 3,465 → 2,465 LUTs, which is not a thing that can
  happen. A per-source LFSR with a data-dependent ack fixes it; nine distinct
  seeds stop the nine gamma converter sets deduplicating into one, which had
  been understating gamma by roughly 9x.
- **A `*` in the gamma unit inferred 54 DSPs** on a 28-DSP device and nextpnr
  failed at 192%. The operands are six bits by four; written as conditional
  shifts and an adder tree it costs no DSP.

### Measured
The complete stage — nine outputs, nine sources, one pipelined arbiter, one
shared gamma set: **5,067 LUTs (20%), 2,203 FF (9%), 0/56 block RAM, 0/28
DSP**, timing PASS at 64.9 MHz against a 40 MHz requirement. Linear instead of
gamma: 3,706 LUTs (15%) and 95.6 MHz. The four-output 512x256 wall: 2,630 LUTs
(10%).

Note the direction: the connected design is CHEAPER than the sum of its
disconnected parts (9,631 LUTs), because sharing one data bus is what collapses
27 gamma converters into 3.

Zero block RAM is the point — the HUB75 stage spends one to two EBRs per output
on row buffers, which is what forced `nrxslots` 8→2 in v1.40.0.

The real ceiling is SDRAM, not logic: a 768×384 wall wants 87 MB/s at its
natural 73.6 Hz against an 80 MB/s peak, so **~30 Hz is the practical data
refresh**. With S-PWM that is not a flicker rate — the chip PWMs at 3840–7680 Hz
regardless — which is why the row scanner free-runs between frames.

At HUB320 density the framebuffer format becomes the binding decision: 524,288
px lands at 21 Hz reading 4 bytes per pixel, 30.5 Hz at 3 bytes packed and 45.8
at RGB565. The reads use 24 bits of every 32-bit word, so bytes-per-pixel stops
being an optimisation and becomes the thing that decides whether a dense wall is
watchable.

Also derived: the shift clock is what sets the data refresh, so a wall that
cannot be fed at 20 MHz has to be **clocked slower**, not left to drop frames.
`shift_divider()` turns an SDRAM efficiency assumption into the divider that
goes in a PLL — sys/4 and 36.8 Hz at 60%. The small 512x256 wall comes out at
sys/2 and 73.6 Hz, limited by the clock rather than by SDRAM.

### Fixed -- documentation that was actively misleading
TODO item 3 claimed JTAG was "fixed" and that the Tigard gave byte-identical
dumps. The write path is sound; the READ path is intermittently corrupt, about
one read in three being good, which makes `--verify` report false failures.
Three `flash-firmware` runs "failed" at three different offsets and had all
written correctly -- proven by dumping the region and matching it against the
source byte for byte.

`build.sh`'s default cable was `usb-blaster` -- the Waveshare that same section
documents as corrupting 29% of bytes while `--detect` passes. Now `ft2232_b`.

Also recorded: SRAM configuration never works on this board, a failed attempt
leaves the FPGA dark, and `--reset` (JTAG REFRESH) is the recovery. There is
therefore no volatile try-before-commit path here; a bitstream can only be
tested by flashing it.

### Not built
Driving an actual S-PWM panel. The stage is wired into the SoC and builds a
bitstream, but no such panel exists on this bench.

## [1.40.0] - 2026-09-18

Groundwork for a 3x3 wall of P1.25 256x128 modules (768x384). The FPGA side now
fits; see the note at the end about why the panels still will not light.

### Changed
- **`nrxslots` 8 -> 2.** Eight MAC receive slots existed only to absorb pixel
  bursts, and since v1.38.0 pixel packets never reach that MAC at all. This is
  the last item from RB-5486 and it frees ~6 EBRs — which is the difference
  between a 9-output 256x128 build fitting and not:

  | Build | DP16KD | Timing |
  |---|---|---|
  | 4 x 256x128 (512x256) | 52/56 | PASS |
  | **9 x 256x128 (768x384)** | **56/56** | PASS, 46.2 / 55.4 MHz at 40 |

  At 256 columns each output's row buffer is 1024 x 32 bits, which needs **two**
  EBRs rather than the one a 128-column buffer fits in exactly.

- **Framebuffer moved and enlarged**: 262,144 -> **327,680 words per buffer**,
  based at `0x140000`. 768x384 is 294,912 px and did not fit the old buffer —
  12% short.

  It moved **down** rather than growing upward, and that fixes a latent hazard.
  `memory.x` aliases `REGION_STACK` to the whole of `main_ram`, so `_stack_start`
  is the top of SDRAM and the stack grows down into the framebuffer's upper
  half — the ELF's last LOAD segment runs to `0x40400000`. It never bit because
  a 128x128 image fills 16,384 of a 262,144-word buffer. A full-size wall would
  have found it. The new map reserves **256 KB above the framebuffer** for the
  stack.

- **New `256x128` panel preset** at 1/64 scan, matching the P1.25 320x160 mm
  module datasheet.

### Fixed
- **`/api/display/pattern` left the panel in INDEXED mode**, so a pattern posted
  while a program was running rendered its fullcolor words as palette indices.
  `bitmap_tick()` tests `PROGRAM_ACTIVE` *before* `program_tick()` renders 16,384
  pixels, and only asserts the mode after — so an ISR landing in that window
  handed the display away and the main loop then took it back. Reproduced 3 times
  out of 3; the render loop is long enough to hit every time. `program_tick()`
  now re-checks the flag inside its critical section.

### Added — two build guards, because both of these cost real time today
- **MAC RX slot count.** `ethernet.rs` hardcoded `NRXSLOTS = 8` and uses it to
  *locate the TX buffers* at `(NRXSLOTS + slot) * SLOT_SIZE`. With the gateware at
  2 slots the firmware wrote outgoing frames 12 KB past the region: the panel
  booted, ran, took interrupts and was completely **mute** — no DHCP, no ARP, no
  ping — while the BIOS netbooted perfectly, because it has its own driver. That
  signature reads as a firmware hang and is nothing of the sort.

- **Framebuffer geometry.** `half_words` existed in **three** places. Enlarging
  the framebuffer changed two of them and missed `hub75.py`'s copy — which the
  hardware buffer swap added in v1.39.0 depends on — so the display toggled to
  buffer 1 at word 589,824 while the pixel DMA wrote it at 655,360. Now one
  definition in `hub75.py`, consumed by `colorlight.py`, mirrored in `hub75.rs`,
  and `build.sh` refuses to build if they disagree.

  The bisect that found it is worth keeping: `nrxslots` alone -> works; base move
  alone -> works; size increase -> dead. Two bad flashes, each ~15 minutes.

### The panels still will not light, and it is not the card
Established by datasheet and reference implementation, not assumption:

- Colorlight's own 5A-75E spec V8.0 says **"supports up to 1/64 scan"**, so the
  card is right for these modules. The five parallel address lines in the pinout
  are not the limit, because these panels do not use parallel row addressing.
- The modules use **`ICN1065L`, a Scramble-PWM driver, not a shift register**. It
  takes a **16-bit greyscale word per LED per refresh**, generates PWM in the
  chip, advances rows on **OE pulses** against an internal counter reset by VSYNC,
  and needs **38 configuration registers written one per frame** after a `PRE_ACT`
  unlock of three LAT pulses (3, 11, 14 CLK).

Our HUB75 output stage shifts 1-bit planes and does its own BCM. It is the wrong
shape for this panel and needs rewriting. Everything upstream — pixel DMA,
classifier, framebuffer, hardware swap — stays valid.

Reference: https://github.com/nimakolahi/hub75-icn1065

## [1.39.2] - 2026-09-18

### Fixed
- **Every remaining colour writer was rotated.** v1.39.1 corrected the fullcolor
  streaming path after measuring solid frames. The `grid` test pattern then
  settled the rest: it draws red/green/blue/yellow vertical bars and rendered
  **green/blue/red**. So `patterns.rs`, `program::rgb()` and `artnet.rs` were
  wrong too — and `timessquare` had been rotated the whole time, which a text
  program simply does not advertise.

  All four writers now pack `B<<16 | G<<8 | R` (`0x00BBGGRR`) and the codebase
  agrees for the first time. Verified on the glass: grid bars read
  red/green/blue/yellow, streamed R/G/B bands read correctly.

  > **How this survived so long.** The v1.10.1 "colour order fix" moved every
  > writer ONTO the wrong order — including `artnet.rs`, which had been right.
  > Each file's comment then cited the others as justification, and `TODO` item 7
  > recorded Art-Net as the odd one out. Four files agreeing is not evidence when
  > they were all changed by one commit. **Treat the pixel word as a property of
  > the panel in front of you**: HUB75 panels differ in RGB pin order, which is
  > the only way both this and the v1.10.1 observation can be true.

### Known defect, recorded not fixed
`/api/display/pattern` calls `swap_buffers()` *before* clearing `PROGRAM_ACTIVE`
and setting `FullColor`, so a program tick landing in between re-asserts INDEXED
and the pattern's fullcolor words are read as palette indices. Observed right
after a reboot: the first POST drew nothing recognisable and reported
`"mode":"indexed"`, a second POST worked. `TODO` item 7.

## [1.39.1] - 2026-09-18

Three faults found by **looking at the panel**, after v1.38.0 and v1.39.0 were
tagged on the strength of frame counters that read 599/600 and zero dropped
throughout. This repo's own notes say it: *no counter can see colour*.

### Fixed
- **On-panel programs stopped displaying entirely (regression, v1.39.0).**
  `swap_buffers()` was made to defer to the hardware whenever `auto_swap` was on.
  But the gateware only flips on a **pixel-DMA frame boundary**, so with no
  stream running the CPU could never swap at all: programs, test patterns and
  animations drew into the back buffer forever. That is the panel's normal
  operating mode.

  `swap_buffers()` always writes `fb_base` again. The streaming path does not
  call it — `present()` calls `sync_buffers_from_hw()` instead, because by then
  the gateware has already flipped, and a CSR write wins over `auto_swap` so the
  two cannot fight.

- **The output mode was never set once the pixel filter was on (v1.38.0).**
  "The magic decides RGB vs indexed" lived in `process_packet`, which is
  precisely what the filter stops running. The panel kept whatever mode it was
  last left in, so RGB888 video was displayed as palette indices. The gateware
  already parses and publishes the magic, so `apply_mode()` now takes it from
  there — the wire format is still authoritative, only the source of the answer
  changed.

- **Fullcolor channel order was rotated.** Measured end to end with solid frames:
  pure RED lit GREEN, pure GREEN lit BLUE, pure BLUE lit RED. The fullcolor
  writers — the gateware DMA and `write_img_rgb888()` — now pack
  **`0x00BBGGRR`** (`B<<16 | G<<8 | R`). Verified with solid red, solid green,
  solid blue and a three-band frame.

  > **The history records a fix in the opposite direction.** The v1.9-era
  > changelog has "green showed as red, red as blue, blue as green" being
  > standardised *onto* `0x00GGRRBB`. Same framebuffer and same scan-out, so both
  > cannot be right for the same panel — HUB75 panels differ in RGB pin order,
  > and these are evidently not what that was written against. **Treat the pixel
  > word as a property of the panel in front of you** and send three solid frames
  > before believing any colour. See `docs/HARDWARE.md` §3.

### Known inconsistency, deliberately not changed
`patterns.rs` and `program::rgb()` still pack `0x00GGRRBB`. Only one order can be
right for a shared scan-out, so these are now suspect — but on-panel programs
render through the **palette** and were confirmed looking correct on the bench
the same day. Changing them blind would risk breaking the path that works in
order to match the path that was broken. Both are annotated in place; verify with
a solid-colour pattern before touching them.

### Fixed (tooling)
- **`send_youtube.py` streamed nothing.** `yt-dlp -g` into `ffmpeg` now returns
  **403** — the URL is bound to the client yt-dlp negotiated as. It pipes yt-dlp
  into ffmpeg instead. The default format also moved to progressive MP4: `bv*`
  selects a DASH stream whose index ffmpeg cannot use on a non-seekable pipe.
  Both failures presented identically as `Done: 0 frames in 0.2s`.

### Added
- **`tools/test_9panel_rgb.sh`** — streams full-RGB video at a nine-panel frame
  size and reports `frames_completed` / `frames_dropped` from the panel.

### The process failure worth recording
Every measurement behind v1.38.0 and v1.39.0 was the **streaming** path, at one
frame size, through `bench_stream`/`send_video`. The buffer swap is shared by
every drawing path, and program mode was never exercised before tagging — twice.

Taking the CPU out of the pixel path also removed work `process_packet` had been
doing as a **side effect**: frame identity, liveness timestamps, and the output
mode. The first two were found by measurement. The third was found by a human
looking at the glass.

## [1.39.0] - 2026-09-18

### The headline
**The framebuffer swap moved into gateware, and video is clean.** v1.38.0 took
the CPU out of the pixel path; it was still deciding *when* to flip the buffer,
and it cannot be quick enough. 600 frames of 384×192 RGB888 video at 25.8 fps:

| | CPU-timed swap | Gateware swap |
|---|---|---|
| completed | 388 | **599** |
| partial | 212 | **0** |
| dropped | 0 | 0 |
| chunks repaired | 212 | **0** |

### Fixed
- **A band of the next frame across the top of the image.** The gateware
  finishes a frame and the next one starts writing immediately, so until the
  buffer is flipped the DMA is filling the very half about to be displayed.
  Everything written in that window lands on the glass.

  | Who flips `fb_base` | Overlap at swap time |
  |---|---|
  | CPU, on the 1 ms timer tick | up to **10 chunks** — 6.6% of the image |
  | CPU, polling flat out | still **2** |
  | **Gateware, at the frame boundary** | **0, by construction** |

  `Hub75UdpDma` pulses `swap_req` at the frame-change latch and `hub75.py` flips
  an internal `fb_base_eff` on it. Scan-out and the DMA's `write_base` both
  follow that one signal, so they cannot disagree about which half is live; a CSR
  write to `fb_base` still wins, so firmware can place the buffer explicitly with
  `auto_swap` off.

- **Every frame glitched at the top, continuously** — the first attempt at the
  above, and worse than what it replaced. `pix_adr` for the incoming frame is
  computed at `hdr_idx == 9`, the same cycle `swap_req` fires, and a Migen
  `NextValue` lands at the **end** of that cycle. The new frame's **first chunk**
  was therefore addressed with the outgoing base and written straight into the
  buffer on screen. `write_base_next` computes the frame-change case explicitly
  instead of waiting a cycle for the register to catch up.

- **Occasional whole-frame corruption.** `present()` patched missing chunks from
  the front buffer, but with the gateware swapping at the frame boundary the
  "back" buffer already holds the **next** frame by then, so repair overwrote
  live pixels with two-frame-old ones. It was also doing harm on frames that were
  fine: `arrival` is set when a chunk's **write completes**, so a bit landing
  after the frame-change latch leaves the bitmap one short for a frame whose
  pixels are all present. Repair is now off whenever `auto_swap` is on — and with
  the swap fixed, there is nothing left to repair.

- **`send_youtube.py` streamed nothing.** It resolved a URL with `yt-dlp -g` and
  handed it to ffmpeg, which now gets **403 Forbidden** — the URL is bound to the
  client yt-dlp negotiated as. It pipes `yt-dlp` into `ffmpeg` instead, so yt-dlp
  does its own HTTP. The default format also changed to progressive MP4:
  `bv*` selects a DASH stream whose index ffmpeg cannot use on a non-seekable
  pipe ("Invalid data found when processing input"). The symptom of both was
  `Done: 0 frames in 0.2s`, because ffmpeg's complaint goes to stderr.

### Added
- **`tools/test_9panel_rgb.sh`** — the acceptance test for this work: streams
  full-RGB video at a nine-panel frame size and reports `frames_completed` /
  `frames_dropped` from the panel, which are the numbers that mean anything once
  the CPU is out of the pixel path.

### Documentation
`ARCH.md` §5 rewritten around the hardware swap; `README.md` architecture section
rewritten and Known Issues expanded; `docs/BENCHMARKS.md` gained the video
results; `docs/FPGA-GUIDE.md` gained the `NextValue` timing trap and the
"two consumers, one signal" rule; `API.md` documents `/api/hwfilter`,
`/api/isrprof`, `/api/dma/arrival` and `/api/cpupix`; `docs/HARDWARE.md` symptom
table gained the three glitch signatures; `TODO.md` item 1 closed.

Corrected in `CLAUDE.md` and `docs/DISPLAY-PROGRAMS.md`: the "~20.6 fps,
per-interrupt bound" framing is now history. That bound was removed in v1.38.0.

## [1.38.0] - 2026-09-18

### The headline
**20 fps full RGB across 9 panels, and the ceiling is ~33.** It was 5.5 fps. The
CPU is out of the pixel path entirely: the gateware kills pixel packets before
the MAC can raise a receive event, and the display runs on the DMA's own
evidence.

Measured on a 384x192 RGB888 frame — 73,728 px, 152 chunks, 3 bytes per pixel:

| Offered | Packets/s | Delivered before | Delivered after |
|---|---|---|---|
| 8.22 fps | 1,250 | 7.95 (20 dropped) | 7.68 (0 dropped) |
| 13.16 fps | 2,000 | 12.94 (15 dropped) | 12.50 (0 dropped) |
| 21.93 fps | 3,333 | **0.37 — collapse** | **18.6–20.5** |
| 32.89 fps | 5,000 | **0.55 — collapse** | **32.89, 60/60 frames** |

`mac_overflow` is **0 at every rate**, where it reached 1,123 before.

### Added
- **`SmolEthPixelClassifier`** (`gateware/smoleth.py`) — observes the CPU branch
  of the split RX stream and decides by beat 9 whether a packet is UDP to the
  pixel port: ethertype at beat 3, IHL in the same beat, IP protocol at beat 5,
  destination port at beat 9. Byte lanes follow `Bytes32to8` — the first wire
  byte of a word is in the **low** 8 bits — and ports are big-endian, so the
  high byte is the low lane.

  A pure **observer**: it never drives `ready` or `valid`. The splitter only
  advances when both consumers take a beat, so anything that backpressures here
  would throttle the DMA branch, which is the path being protected.

  Deliberately **not** driven from the existing `SmolEthUDP` filter: that sits
  behind a 512-deep `burst_fifo`, so by the time it has a verdict the CPU branch
  is several packets ahead and it would invalidate **the wrong packet**.

  `SmolEthInvalidator` — written long ago and instantiated nowhere until now —
  ends the frame with `error=0xFF`, `last_be=0x01`. Verified in
  `liteeth/mac/sram.py` rather than assumed: `LiteEthMACSRAMWriter` tests
  `(sink.error & sink.last_be) != 0` on `last` and takes `DISCARD`, which resets
  its length and returns to `WRITE` **without** pushing slot+length into
  `stat_fifo` — and that push is what raises the receive event. No event, no
  interrupt, slot reusable immediately.

- **Double-buffered arrival bitmap** (`gateware/dma_writer.py`) — `done0..7`,
  `frame_id_done`, `total_chunks_done`, `indexed_done`, `done_seq`. On a frame
  change the outgoing bitmap and its identity are latched **before** `arrival` is
  cleared, so a reader has no deadline. `done_seq` distinguishes snapshots;
  `frame_id` cannot, because it wraps at 16 bits. `dma_done_frame()` reads
  `done_seq` either side of the words and returns `None` on a torn read.

- **Frame identity in gateware** — `total_chunks` (header byte 5) and `indexed`
  (the magic), committed together with `frame_id` at `hdr_idx == 9`.

- **`/api/hwfilter`**, **`/api/isrprof`**, **`/api/dma/arrival`**,
  **`/api/cpupix`** — control and diagnostics for all of the above.

### Changed
- **The pixel filter is enabled at boot**, from firmware rather than defaulted on
  in the gateware. That distinction is the safety story: `pixel_filter_enable` is
  a `CSRStorage` with reset 0, so it clears on every FPGA configuration and the
  **BIOS always netboots with the filter off**, however badly this firmware
  behaves. Pull the power and the CSR resets. Defaulting it on in the bitstream
  would have made a bad classifier a JTAG recovery over a link that corrupts most
  of what it reads.

### Fixed
- **`merge_dma_arrival()` ran on every packet.** 8 CSR reads plus a full
  re-popcount of the 256-bit mask, and rv32i has no popcount instruction, so
  every `count_ones()` is a software loop. Now only when a packet could complete
  the frame. **11,311 → 10,030 cycles/packet**, and at 128x128 that moved 58.8 fps
  from 62.7% loss to 15.8%.

- **Three bugs that only existed once the CPU stopped seeing packets**, each
  invisible before and each fatal:

  | Symptom | Cause |
  |---|---|
  | 30 frames sent, 1 shown | **Time stopped.** `TIME_MS` advanced only inside the receive ISR, so no packets meant no clock, so `timer_tick` was never true and `bitmap_tick()` never ran. `get_time_ms()` now reads the timer itself, masked so it cannot race the ISR. |
  | 30 sent, 30 stale, 30 dropped | **Every frame looked quiet.** `last_packet_ms` was written only by packets the CPU parsed. A chunk landing in SDRAM is activity whoever noticed it. |
  | **80 frames shown for 40 sent** | **Double presentation.** The snapshot path and the live path both presented every frame — two buffer swaps each. `tick()` now returns after the snapshot when the filter is on. |

- **`tools/bench_stream.py` padded the final chunk** of every frame to full size.
  marquee's `stream.py` slices the body and sends a short tail. Not harmless: the
  gateware declines to set the arrival bit for a chunk running past the end of
  the image, so with the CPU out of the path every frame arrived one chunk short
  — reproduced exactly as `missing:[33]` of 34.

### Measurement notes
- The ISR was profiled directly for the first time (`/api/isrprof`); RB-5486's
  19,800 cycles/packet was derived, never measured. The real figure was 11,311,
  of which 6,373 was `process_packet` **with the CPU writing no pixels at all**.
- Not bus-bound, which killed the obvious theory: MAC packet buffer **7**
  cycles/byte, DRAM **13**, CSR read **8**.
- All figures are a 384x192 frame driven into a board with **2 panels physically
  lit**, so nine outputs' worth of scan-out bandwidth is not represented and a
  real wall will be somewhat worse.
- `bench_stream.py`'s loss column reads 100% with the filter on — it is derived
  from a CPU counter that is now always zero, which is the entire point.

## [1.37.0] - 2026-09-17

### The headline
**Cold boot: 20.6 s → 6.2 s.** v1.36.0 made a panel able to survive a power cut;
this makes it come back quickly. The entire difference is one line of gateware,
and finding it took measuring every stage of the boot until they added up.

### Fixed
- **`flashboot()` read the firmware image out of SPI flash three times, at
  1142 CPU cycles per byte.**

  `check_image_in_flash()` runs in `flashboot()` and **again** inside
  `copy_image_from_flash_to_ram()` (`boot.c:623`), so the 292,976-byte image is
  crc32'd twice and then copied. The `spiflash` SoCRegion is `cached=False` with
  `READ_1_1_1`, and `LiteSPIPHY`'s `default_divisor` was LiteSPI's own default of
  9 — a 2 MHz SPI clock at `sys_clk` 40 MHz. Every single byte therefore cost a
  full 8 command + 24 address + 8 data = 40-clock transaction before any data came
  back:

  | Read | Cycles per byte |
  |---|---|
  | DRAM | 14 |
  | SPI flash, divisor 9 (2 MHz) | **1142** (28.5 µs) — 78× slower |
  | SPI flash, divisor 1 (10 MHz) | **246** |

  | Pass | divisor 9 | divisor 1 |
  |---|---|---|
  | crc32 over the image, ×2 | 16.72 s | 3.62 s |
  | copy to DRAM | 1.19 s | 0.28 s |
  | **flashboot** | **17.92 s** | **3.90 s** |

  `LiteSPIPHY(default_divisor=1)`. Chosen with margin: `clk_divisor` is a
  `CSRStorage`, so `/api/flashbench` sweeps the SPI clock at **runtime** and
  checksums the whole image at each setting — 9, 4, 2, 1 and 0 all return
  identical data, so 20 MHz is proven to read correctly on this board and 10 MHz
  ships with 2× headroom against it. The failure mode is a panel that will not
  boot standalone and has to be recovered over a JTAG link that corrupts most of
  what it reads, so margin is worth more than the extra 1.7 s.

  **Measured on the bench**, power to DHCP, with a marked plug-in and a packet
  capture:

  | Stage | Cost | Measured how |
  |---|---|---|
  | ECP5 configuration | ~1.4 s | computed from image size ÷ 2.4 MHz MCLK |
  | BIOS + DRAM init + `serialboot()` | 0.53 s | warm reboot: reset → first RRQ on the wire |
  | `netboot()` failing | 0.32 s | `/api/flashbench` |
  | `flashboot()` | 3.90 s | `/api/flashbench` |
  | firmware start → DHCP | 0.4 s | warm reboot: `boot.bin` served → `<mac>.yml` asked |
  | | **≈6.55 s** | against **6.24 s** observed |

  The budget closes to within 0.3 s, which is inside the error on a human-timed
  power-on. Nothing is left unexplained.

- **`/api/bench` has reported zeros since it was written.** `mcycle`
  (`csrr 0xB00`) is not implemented on this VexRiscv build and reads back 0, so
  `fill_cycles`, `program_cycles` and every derived figure are 0 — under a
  comment explaining at length why the endpoint uses `mcycle` rather than
  `TIME_MS`. Documented, not yet fixed; `/api/flashbench` uses `Timer0` instead.

### Added
- **`GET /api/flashbench`** — times memory-mapped SPI flash reads (byte-wise,
  word-wise, and the same loop over DRAM as a control), reports what
  `flashboot()` and a failed `netboot()` actually cost, and sweeps `clk_divisor`
  over the **full** image with a checksum per setting. Timed with `Timer0` in
  chunks, because that timer wraps every second: the first version of the sweep
  reported the slowest divisor as the *fastest*, since its real 1248.8 ms aliased
  to 247 ms.

### Documentation
Every finding from this investigation is written down, including the parts that
were wrong: `docs/HARDWARE.md` §8c (the budget, the traps, the divisor sweep) and
a corrected §8 cold-boot reading table, `ARCH.md` §1, `docs/BENCHMARKS.md` §5b,
`docs/FPGA-GUIDE.md` §10–11, `docs/INSTALLATION.md`, `docs/PROGRAMMING.md`,
`docs/GETTING-STARTED.md`, `docs/tftp-boot-research.md`, `README.md`, `CLAUDE.md`
and `NOTES.md`.

Two claims that stood in these docs for days are now marked wrong where they
stood, rather than quietly deleted:

- *"Cold power-on does not configure the FPGA — needs a scope."* **It configured
  every time.** The board booted, ran the BIOS, and stalled at `netboot()`. Every
  indicator anyone used — a TFTP request, a ping to the DHCP address, pixels on
  the panels — sits downstream of that, so a healthy board that stalled late was
  indistinguishable from one that never configured. **Do not buy scope time.**
- *"A panel needs a JTAG load or a reachable netboot server to come up."* It
  needs neither.

### Method notes, which is the part worth keeping
- **Every "it takes 30 seconds" figure was `last ping reply → first ping reply`**,
  which contains however long the plug was out. Treating that as zero invented a
  20-second phantom and cost most of a session. Mark the power-on.
- **Two confident hypotheses died on measurement.** STP forwarding delay — 30 s
  fits suspiciously well, and it was impossible. And `netboot()`'s ARP retries,
  which look enormous written out (2 × 8 tries × 100,000 spins) and cost 320 ms,
  because idle `udp_service()` is one CSR read at 8 cycles.
- **The BIOS and the firmware have different MACs.** `tcpdump -e ether host
  10:e2:d5:00:00:00` answers "network or flash?" directly — the BIOS transmits
  nothing at all on a cold boot and is chatty on a warm one.
- **A runtime-tunable CSR can test a gateware change before you build one.**
- **Verify flash with the board, not the programmer.** `--verify` failed seven
  times running at seven different offsets on a write that was correct; six full
  dumps of the same region differed from the source by 0.54% to 8.77% and from
  each other, and a majority vote across them converged on the wrong answer. The
  FPGA's own LiteSPI path reads correctly — identical checksum at every SPI clock
  — so comparing `/api/flashbench`'s checksum against the local `.fbi` settles it
  in seconds. That is how this release's firmware write was confirmed.

## [1.36.0] - 2026-09-17

### The headline
**A panel can now survive a power cut on its own.** It has never been able to
before. `--default-config` (v1.28.0) was built for exactly this and could never
run, because the firmware in flash was never in a form the BIOS would accept.

### Fixed
- **Firmware flashed to `FLASH_BOOT_ADDRESS` was never bootable.**
  `flash-firmware` wrote `.tftp/boot.bin` **raw**, but the BIOS's
  `check_image_in_flash()` expects an 8-byte header — `length` then `crc32` —
  ahead of the image. With no header the BIOS read the firmware's first RISC-V
  instruction, `0x400000b7`, as a 1 GB length, rejected it, and fell through to
  `netboot()`. **Every time, since `flash-firmware` was added on 2026-09-06.**

  `build.sh flash-firmware` now wraps the image with LiteX's own
  `crcfbigen.py -f -l`, validates the header exactly as the BIOS will — length in
  range, length matches, crc32 matches, payload identical — **refuses to flash if
  it would not pass**, and writes the resulting `.tftp/boot.fbi`.

  **Verified on hardware**: power removed, 31 seconds dark, and the panel came
  back unaided, fetching only its `<mac>.yml` and **no `boot.bin`** — the
  firmware came from flash, not the network. That is the first genuine
  standalone boot this board has ever managed.

### Changed
- **The BIOS now tries the network *before* flash.** New
  `patches/litex-bios-netboot-first.patch`, applied to the image alongside the
  broadcast patch, moves the `CSR_ETHMAC_BASE` block ahead of the
  `FLASH_BOOT_ADDRESS` block in `boot_sequence()`.

  Making flash bootable (above) would otherwise have broken the iteration loop:
  with LiteX's stock order (serial → flash → rom → sdcard → sata → net) a panel
  holding good firmware in flash **never** fetches the deployed image, so
  `./build.sh deploy` + reboot silently does nothing and every change needs a
  JTAG re-flash. marquee is the fleet's source of truth for firmware; flash is
  the fallback that keeps a panel alive without it.

  Both properties hold because `netboot()` is bounded — one attempt, no link
  wait, no retry, and every wait inside `tftp.c` is a counted loop:

  | Situation | What happens |
  | --- | --- |
  | Developing — marquee reachable, link already up | netboot wins, `deploy` + reboot lands the new firmware |
  | Power cut — no server, or the PHY has not negotiated yet | netboot fails in a few seconds, **falls through to flash**, panel comes up standalone |

  The cold-boot case is not luck: it is the same one-shot netboot whose failure
  masqueraded for two sessions as "the FPGA does not configure at power-on".
  Cost is a couple of seconds of TFTP timeout before the fall-through.

  A BIOS change reaches hardware only through a **full bitstream rebuild** — the
  BIOS lives in block-RAM ROM, not in flash.

  **Verified on hardware**: after flashing this bitstream, the card fetched
  `boot.bin` (292,976 bytes) from the BIOS's compile-time IP `192.168.1.50` and
  then its `<mac>.yml` from the DHCP address — the boot.bin fetch that a
  flash-booting panel did *not* make under v1.35.0.

- **`build.sh` defaulted to a chain length the firmware contradicts.**
  `CHAIN_LENGTH=2` had sat in `build.sh` since v1.7.0, while
  `hub75.rs` has `const CHAIN_LENGTH: u8 = 1` since v1.34.0 — so any plain
  `./build.sh bitstream` produced a gateware wiring up panel slots the firmware
  never addresses. It builds, boots and looks fine; only the released bitstreams
  were right, because they were built with an explicit `--chain 1`.

  The default is now 1, and `build_bitstream()` reads `const CHAIN_LENGTH`
  straight out of `hub75.rs` and **refuses to build a gateware that disagrees**.

### The cold-boot investigation, resolved
`TODO` item 2 spent two sessions on "the FPGA does not configure at power-on".
**It always configured.** The board booted, ran the BIOS, and stalled one step
later at netboot — which makes **one** attempt with no link wait and no retry,
while the Ethernet PHY is still negotiating. Nothing is transmitted, so the TFTP
server logs nothing, and the BIOS idles while answering pings. A JTAG `REFRESH`
succeeds only because the PHY already has link.

Every indicator in use — a TFTP request, a ping to the DHCP address, pixels on
the panels — sits **downstream of netboot**, so a healthy board that stalled late
was indistinguishable from one that never configured. The discriminator is the
BIOS's compile-time IP (MAC `10:e2:d5:00:00:00`); it was never probed.

## [1.35.0] - 2026-09-16

### Fixed
- **The CPU now asks the hardware what it actually wrote.** `bitmap_udp.rs`
  built its chunk-arrival mask from packets *it* received. The gateware DMA's
  bitmap records chunks the hardware actually **wrote**. They are not the same
  set, and the gap runs both ways: `AlwaysReady` in `smoleth.py` drops whole
  packets rather than backpressure the CPU's ethernet, and the DMA watchdog
  abandons a payload if the DRAM writer stalls. Neither is visible to the CPU.

  The consequence was a display that **froze with a correct image already in
  the framebuffer**: `present()` saw more than `MAX_REPAIR_CHUNKS` missing and
  dropped frame after frame while the sender ran happily.

  `Hub75::dma_arrival()` exposes `pixdma.arrival0..7` plus `frame_id` — which
  the gateware has always published and the firmware has always ignored.
  `merge_dma_arrival()` ORs it into `chunk_mask` and re-derives `received` by
  popcount, so count and mask cannot drift apart. Called before the completion
  test in `process_packet` **and** before the staleness judgement in `tick()`;
  without the second, a frame the hardware finished but the CPU saw no tail of
  sits forever.

  **Union, not replacement**, deliberately: the CPU still receives every pixel
  packet today, so its own mask carries real information. When port-7000
  traffic stops reaching the CPU's MAC (RB-5486) that mask goes empty and the
  union degenerates to the DMA's bitmap alone — the same code serves both.

### Measured
Proving this needed a load the bench could not previously produce. v1.34.0's EBR
headroom supplied it: `--outputs 12 --chain-length 1` driving a **512x192**
virtual display across a 4x3 grid — **202 chunks/frame**, double the 256x192
regime the bug was originally measured in.

Identical bitstream, two firmwares differing only in `bitmap_udp.rs`, saturated
with `send_test_pattern.py bars --delay 0`, two runs each. Divergence between
what the DMA wrote and what the CPU saw was 490-550 chunks throughout:

| | frames completed | frames dropped | `last_missing` |
|---|---|---|---|
| without the merge, run 1 | 0 | +2 | **133** |
| without the merge, run 2 | 0 | +1 | **23** |
| **with the merge, run 1** | **+1** | **0** | **0** |
| **with the merge, run 2** | **+1** | **0** | **0** |

`last_missing` is the direct evidence: without the merge the CPU believes up to
**133 of 202** chunks are missing and drops the frame, while those pixels are
already correct in SDRAM.

The divergence appears **only under saturation**. The same sweep at 512x192 runs
clean to 16.50 fps with CPU packets exactly equal to `dma_chunks`
(28,280 = 28,280), which is why a sweep alone never showed it.

### Verified on hardware, in passing
**12 outputs ran on a real board for the first time** — configured, booted, took
a 12-connector layout and streamed. That is v1.34.0's headroom actually being
used, not just a build report.

### Not done here
The MAC filter itself (RB-5486 steps 2 and 3). `SmolEthInvalidator` in
`smoleth.py` remains written and instantiated nowhere; add it to `TODO` item 7's
dead-code list. This release is its hard prerequisite: once the CPU stops seeing
pixel packets, this merge is the only thing that lets a frame present at all.

## [1.34.0] - 2026-09-16

### Fixed
- **The build could no longer build.** `./build.sh bitstream` died in LiteX's
  libc with `'_FDEV_SETUP_RW' undeclared here (not in a function)`.

  `litex_setup.py --tag=2025.12` pins LiteX's own repos but **not**
  `pythondata-software-picolibc`, which tracks HEAD — so the image was only
  reproducible by accident, and drifted. A 2026-06-05 picolibc (data
  `16ff442da`) removed `newlib/libc/tinystdio`, which LiteX 2025.12's
  `soc/software/common.mak` hardcodes in `INCLUDES`. GCC ignores a `-I` that
  does not exist, so `libc/stdio.c` silently fell through to the xPack
  toolchain's **newlib** `stdio.h` and failed on picolibc-only macros.

  It stayed hidden because the software half only rebuilds when the SoC config
  changes — i.e. on any gateware change, which is exactly when a toolchain
  failure is least expected. The last good image (`litex-hub75:im`) still had
  the 2022 picolibc and had never been rebuilt.

  Pinned to `a5e1122` (data `f165dc22f`) in the `Dockerfile`, with a build-time
  assertion so a bad pin fails the image rather than the bitstream. `build.sh`
  now **asks the image** whether `tinystdio` is present before building, and
  says what to do — the repo's existing stale-artifact guards cover a bitstream
  older than its gateware and a `boot.bin` older than its sources, but nothing
  covered "the software half no longer compiles".

### Changed
- **Row buffers: one memory per output instead of two — 12 EBRs → 6.**

  An ECP5 EBR is 16 Kbit. At `chain_length 1` a bank is `columns*2 × 32` =
  256 × 32 = 8 Kbit, so two separate ping-pong memories burn two EBRs at **half
  occupancy each**. Merged behind one extra high address bit, both banks are
  512 × 32 = **exactly one full EBR**. Full 32-bit colour depth is kept — this
  is not RGB565 and there is no banding.

  At `chain_length 2` a merged pair is 1024 × 32 = 2 EBRs, identical to today,
  so the change is never worse.

### Measured
| Build | EBR | IO | Timing (40 MHz constraint) |
|---|---|---|---|
| before — chain 2, split | **56/56** | 113/197 | — |
| after — chain 2, merged, 6 outputs | **50/56** | 113/197 | 61.51 MHz PASS |
| after — chain 1, merged, **12 outputs** | **56/56** | 149/197 | 61.46 MHz PASS |

**The output ceiling doubles, 6 → 12**, one panel per connector, and the
chain-slot trap in `TODO` item 7 goes with it. Each output now costs **one** EBR
instead of two.

### Corrected
- **`TODO` item 4 was wrong.** It cleared 9 panels — "there is no framebuffer
  work to do before adding panels" — by checking framebuffer *words* in SDRAM
  (correct: 262,144 per buffer, ample) and **never checking EBRs**, where the
  device sat at 56/56 with nowhere to put a seventh output. This is the
  prerequisite it said did not exist.

### Verified on hardware (2026-09-16)
Firmware `CHAIN_LENGTH` 2 → 1, PAC regenerated, layout YAML dropped to one
position per connector. Loaded, booted, and then flashed on the bench panel:

```
boot.bin 291,600 bytes   <mac>.yml 282 bytes (was 349, doubled)
display 128x128  grid 1x2  hw chain=1 outputs=6  SR1 0x00
```

Flash, served firmware and layout are all on the chain-1 pair, so a
`--reset` recovers a consistent board.

`OUTPUTS` stays 6 and `layout::MAX_OUTPUTS` is already 6, so the existing
`const` asserts in `hub75.rs` covered the constants — item 4's "fails silently"
warning applies to the *layout parse* path, not to these.

### Found while doing it: the Tigard's voltage switch decides whether JTAG works
**The same signature as the Waveshare, from a good adapter in the wrong mode.**
In self-powered `3V3` with `J33` disconnected, `--detect` passes and returns the
right IDCODE while **every bulk transfer dies with `CRC ERR`**; `--freq 1000000`
does not help. Correct mode is **`VTGT` with `J33` connected**, which gives
first-attempt SRAM loads and verified flash writes.

A **known-good** bitstream failed identically, and that is what proved it was
the link and not the image — the rule worth keeping is *load a bitstream you
have loaded before whenever a CRC error appears, before suspecting your build*.

It collides with the cold-boot work: `J33` must be **off** for a POR test and
**on** to program, and the self-powered mode that looks like it resolves that
does not work here. Recorded in `docs/HARDWARE.md` §7 and `TODO` item 3.

## [1.33.1] - 2026-09-16

### Investigated, still not fixed
Two more cold-power-on hypotheses tested on hardware, both negative. Recorded so
neither is re-run: `TODO.md` item 2 and `docs/HARDWARE.md` §8b carry the detail.

- **Stale multiboot data in flash — excluded.** The openFPGALoader maintainer
  suggested on [#732](https://github.com/trabucayre/openFPGALoader/issues/732)
  that a partition table at the end of the chip could be redirecting the
  power-on fetch, since the tool only erases the sectors it writes. **It was
  there.** A full 4 MB dump found twelve non-blank regions where we have only
  ever written two: complete factory Lattice **Diamond 3.10.2.115** bitstreams
  at `0x200000` and `0x2B0000`, and a JUMP descriptor in the chip's last sector —
  `ff ff bd b3 / ff ff ff ff / 7e 00 00 00 / 03 20 00 00`, i.e. sync, dummy,
  ECP5 opcode `0x7E` (JUMP), SPI read `0x03` at `0x200000` — pointing away from
  our image at offset 0.

  `--bulk-erase` removed all of it (verified: 0 non-`0xFF` bytes in 4 MB, zero
  sync words), and the bitstream and firmware were rewritten and verified.
  **Cold power-on fails identically.** So multiboot is now properly excluded
  rather than merely untested, and the asymmetry survives on a chip holding
  nothing but our own two regions.

- **The Tigard's 3.3 V wire (`J33`) — excluded.** The ruled-out table said the
  programmer had been "unplugged entirely"; what was actually done was pulling
  the **USB cable** while every wire stayed on the board, `J33` included. That
  leaves the board's live rail backfeeding the adapter's target-side supply net
  and its level-shifter decoupling — a plausible cause of the "rail ramp at
  power-up" candidate. Disconnected at the board and power-cycled: **no change.**
  `J33` is staying off regardless; there is no reason to wire it.

### Fixed (documentation)
- **The `0x200000` gap was never free.** `TODO.md` and `docs/HARDWARE.md` §8b
  both stated it was "free, and it is free **on purpose**". It held a 576 KB
  factory bitstream, and had since the board left the factory.
- **The bench has one programmer, not two.** `--cable ft2232_b` and
  `--cable tigard` are two cable definitions for the **same physical adapter** —
  a Tigard V1.1 (`SecuringHardware.com`, serial `TG11169c`), which is an
  FT2232H. TODO item 3 read as though these were separate units.
- **Stale line reference.** Item 3 cited `README.md:31-45` for the board pads;
  the table is at `README.md:69-82`. Pads spelled out inline so the next drift
  is harmless: `J27` TCK, `J31` TMS, `J32` TDI, `J30` TDO, `J33` 3.3 V,
  `J34` GND.
- **Bitstream size in the flash map** still said ~712 KB, from before
  `--ecppack-compress`.

### Known, unrecorded until now
- **Our FPGA's part marking has never been written down.** IDCODE `0x41111043`
  gives only `LFE5U-25` — no speed or temperature grade. The maintainer runs a
  rev **8.0** board with `LFE5U-25F-7BG256C` and reports flash boot working, and
  flags the pinout and the `I`-vs-`C` grade as the differences from our rev
  **8.2**. It needs reading off the chip.
- **`build.sh` flashes with openFPGALoader v0.11.0**, from inside the Docker
  image — the version that prints `flash chip unknown` for the GD25Q32 and
  leaves `SR1 = 0x1c`, causing a later `--write-flash` to fail with
  `Error: block protection is set`. The v1.1.1 build at `.ofl/bin/` that this
  repo carries is not wired into any target, so `./build.sh flash-all`
  reintroduces the artefact v1.1.1 exists to avoid.

### Next, both instrument-free
Pull the four JTAG **signal** wires and power-cycle (`J33` and USB are excluded;
the signal lines never have been), and try a **second Colorlight card** — the
fleet has exactly one, so "faulty board" has never been separable from
"design/revision".

The pre-erase 4 MB image is kept at
`the build host:~/flash-backup-20260916/full-4MB-before-bulkerase.bin` (two dumps,
byte-identical, `9baac91f…`).

## [1.33.0] - 2026-09-16

### Added
- **`custom/` — an extension point the maintainers actually use.** Site content
  moved out of the stock tree; `panels/` now holds only generic examples.

  ```
  custom/README.md      what goes here                     SHIPS
  custom/example/       a worked example, end to end       SHIPS
  custom/<name>/        your programs and layouts          NEVER SHIPS
  ```

  Our own bench programs live here, in the shape a stranger would use. An
  extension point the maintainers do not use is always subtly broken — an
  undocumented assumption, a path that only works from the repo root, a flag
  nobody tested. `./build.sh panelc` globs `panels/*.yaml` **and**
  `custom/*/panels/*.yaml`, so both are found by identical code every build.

- **`./build.sh panelc`** — compiles every program, stock and custom.

### Fixed
- **The build image had no Pillow or cairosvg**, so `panelc` could not run
  inside Docker at all: compiling a program meant installing Python packages on
  the host, which breaks the one promise this repo makes about its toolchain.
  Added along with the cairo shared library. This is what would have made
  `custom/` useless to anyone but us.

### Publishing
`custom/README.md` and `custom/example/**` are in the manifest; everything else
under `custom/` is private **by default** because it is not listed. That is the
reason the manifest is an allowlist — a new site file cannot leak by being
forgotten.

### Verified
5 programs compile (3 stock, `dashboard`, and the example); firmware builds with
`--default-config` pointing into `custom/`; panel runs.

## [1.32.0] - 2026-09-16

### Added
- **`GET /api/flash`** — dumps flash through the SoC's **memory-mapped LiteSPI
  window**. Every previous check read back through the same SPI bridge that
  wrote it, where a write that silently did nothing and a read that returns what
  you expect are indistinguishable. This shares no assumption with
  openFPGALoader, and it proved the flash contents are genuinely correct.
- **`POST /api/flash/drive`** — writes flash SR2 (QE) or SR3 (drive strength).
  On demand, not at boot: these are non-volatile with limited endurance. Both
  take a value, because reverting a change to a non-volatile register must be as
  easy as making it.

### Investigated, still not fixed
Cold power-on still does not configure the FPGA. Added to the ruled-out list:
flash **QE bit** (SR2 0→2), flash **drive strength** (SR3 0x20→0x00), a raw image
with a **128-byte `0xFF` preamble**, `SYSCONFIG MASTER_SPI_PORT=ENABLE`, and
**openFPGALoader v1.1.1**, which behaves identically to v0.11.0.

Reported upstream with the full evidence:
[openFPGALoader #732](https://github.com/trabucayre/openFPGALoader/issues/732),
[LiteX #2603](https://github.com/enjoy-digital/litex/issues/2603).

### Tooling note
**Use openFPGALoader v1.1.1, not the v0.11.0 in oss-cad-suite.** v0.11.0 prints
`flash chip unknown: use basic protection detection` for the GD25Q32 and leaves
`SR1 = 0x1c` behind, so a later plain `--write-flash` fails with
`Error: block protection is set`. v1.1.1 recognises the part and leaves SR1
clean. It does not fix the boot fault, but it removes a confusing artefact.

## [1.31.0] - 2026-09-15

### Added
- **Flash status registers in `/api/status`** (`flash_sr1/2/3`). The firmware
  already drives the SPI master to read the flash unique ID; it now also reads
  SR1/SR2/SR3 at boot and publishes them. Added to diagnose a cold-boot fault
  where the flash *contents* verify perfectly — the useful state is in the
  status registers, and no JTAG tool reads them back.
- **`--ecppack-compress` in the bitstream build.** LiteX's `trellis.py` declares
  `compress = True` in `build()`, but registers the CLI flag with
  `action="store_true"`, so `trellis_argdict()` passes **False** unless it is
  given — the function default never applies. Every bitstream this project has
  ever produced was uncompressed. 712,177 → 458,090 bytes.

### Investigated, not fixed
**Cold power-on does not configure the FPGA.** A power cycle leaves the panel
dark with no ping and no TFTP request. A JTAG `Refresh` configures the ECP5 from
that same flash reliably, every time — so the data is readable, the bitstream
parses, and master-SPI works. Only the power-on trigger fails.

Ruled out by direct test: a bad write (byte-identical readback of both regions),
a gateware regression (a **February** bitstream fails identically), multiboot,
the configuration window length, flash block protection, the cable definition,
the programmer being physically attached, and the power supply.

What remains is the POR sequence, and settling it needs a scope on
INITN/DONE/flash CS. Recorded in `TODO.md` item 2 and `docs/HARDWARE.md` so the
ruled-out list is not re-derived.

The fleet is unaffected — netboot takes precedence whenever marquee is reachable.
A *standalone* panel is not achievable until this is solved; `--default-config`
is verified and works, it simply never gets to run.

## [1.30.2] - 2026-09-15

### Fixed (documentation)
- **The guides claimed flash boot works. On this bench it does not.** Tested
  with a real power cycle: the board does not come up — no ping, and **no TFTP
  request at all**, so the BIOS never runs. `INSTALLATION.md` said *"Then
  power-cycle. The board now comes up with no JTAG and no build host"*, and
  `GETTING-STARTED.md` had a step titled *"Make it survive a power cut"*. Both
  now say what was actually measured.

  This was flagged as unverified throughout, but the installation guide stated
  it as fact anyway — which is the more dangerous of the two, because that is
  the page someone follows when deploying.

### What was ruled out, by direct verification
- **Not a bad write.** The bitstream at offset 0 is byte-identical to its source
  (712,148 bytes compared) and the firmware at `0x100000` is byte-identical to
  the built `boot.bin`.
- **Not a broken boot path.** openFPGALoader's `Refresh` configures the ECP5
  from that same flash reliably — it is what recovered the board.
- **Not a dead board.** `--detect` answers afterwards.

So the data is right and the mechanism works; only the power-on trigger does
not. Candidates are recorded in `TODO.md` item 2 and `docs/HARDWARE.md` §8b:
flash not ready within the FPGA's power-up read window, panel inrush sagging the
rail, or CFG pins not selecting master SPI. The cheapest next test is powering
the card with the panels unplugged.

**Consequence**: a panel needs a JTAG load or a reachable netboot server. The
`--default-config` feature itself is unaffected and still verified — a baked
config was shown to apply when the fleet server refused one — but it cannot
deliver a *truly* standalone panel until the board reaches its firmware from
cold.

## [1.30.1] - 2026-09-15

### Added
- **`LICENSE` — BSD 2-Clause.** The README has carried a BSD-2-Clause badge
  since January with **no licence file behind it**. A badge grants nothing:
  without the file, default copyright applies and nobody has permission to use,
  copy or modify any of this.
- **The DejaVu licence, next to the fonts** (`assets/fonts/LICENSE-DejaVu.txt`).
  Its terms require the notice to travel with the files, so redistributing the
  `.ttf` without it was a genuine compliance gap. Note for anyone changing them:
  modified DejaVu must be renamed to contain neither "Bitstream" nor "Vera".
  These are unmodified — `panelc` rasterises them at build time and never
  touches the files.
- **`THIRD-PARTY.md`**, which separates what is *redistributed* here (smoltcp
  0BSD, the fonts) from what is merely *built against* (LiteX, yosys, nextpnr,
  VexRiscv, openFPGALoader). Only the first kind carries obligations.

## [1.30.0] - 2026-09-15

### Added
- **`publish/` — the generator for the public repo.** The public repo will be an
  *output*, not somewhere anyone works: a script exports a scrubbed snapshot of
  both private trees into it. That turns the scrub from a one-time manual pass
  that decays into code that runs every time, and lets the public repo start
  with clean history.

  Four gates, because **the script's job is refusing, not copying**:

  | Gate | Catches |
  |---|---|
  | allowlist | a file nobody listed does not ship — a new site file is private by default |
  | forbidden scan | the backstop: a site string added to a file that *does* ship |
  | build | the exported tree must compile |
  | divergence | a merged PR upstream, which the next publish would silently revert |

  Verified end to end against a throwaway repo: first publish, no-op republish,
  a simulated merged PR refused by name with exit 3, and `--force` overriding.

### Fixed
- The canonical bare repo's host removed from a changelog entry — one of the
  nine violations the scan refused on. The rest were in marquee (0.9.0).

### What the first dry run caught
Both directions, which is the point of having two gates:

- **Nine real site references** that would have shipped — a hardcoded bus host
  in two code defaults and three documents, a canonical remote in a Makefile
  check and two changelogs, and an internal camera address in a UI placeholder.
- **Six files wrongly withheld**, every one needed to *build*: `Dockerfile`
  (step one of the getting-started guide), `assets/fonts`, `assets/icons`,
  `patches/`, and the server's `VERSION` and `tests/`.
- **Build artifacts that would have been committed**: the gate's own `target/`
  comes back root-owned from Docker, and `.tftp/boot.bin` is *explicitly
  un-ignored* by the shipped `.gitignore`, so `git add -A` would have published
  a firmware binary built from whatever this host happened to have.

Rollout is deferred; see marquee `doc/PUBLIC-RELEASE-PLAN.md`.

## [1.29.1] - 2026-09-15

### Documentation
- **The rule, stated up front: once a card is programmed, all you need is
  marquee.** Nothing in this repo runs at runtime. It builds gateware and
  firmware, compiles on-panel programs, and flashes boards; marquee serves boot,
  config, pixels, values and stats.

  In the README (with a table of exactly when you come back here),
  `GETTING-STARTED.md` at the point the sign first works, `INSTALLATION.md`,
  `ARCH.md` and `CLAUDE.md`.

  The boundary is the useful part: the firmware carries **every** program and the
  config picks one, so switching a panel between them is a marquee change, while
  adding a new one is a firmware build.

- **Netboot takes precedence over flash** — while marquee is reachable, the image
  marquee serves is what runs and the flashed copy is the fallback. So
  `./build.sh deploy` rather than re-flashing is how a networked fleet is
  updated, and flashing is what makes a *standalone* panel possible. This is the
  opposite of what "we flashed the board" suggests, and it was only obvious from
  watching every boot this session fetch `boot.bin` after a flash.

## [1.29.0] - 2026-09-15

### Added
- **`./build.sh deploy`** — copies the built firmware into marquee's firmware
  root, keeping the previous image beside it and stamping the outgoing one with
  its version (`boot-v1.29.0.bin`). The build produced `.tftp/boot.bin` and
  marquee served `/data/firmware/boot.bin`, with **nothing connecting them**: the
  copy was manual, and forgetting it meant marquee kept serving the PREVIOUS
  firmware while you debugged why your change had no effect. `--marquee-fw`
  overrides the destination.

### Fixed
- **TODO item 8 rewritten against the actual root cause.** It said per-panel
  firmware failed because `tftp_resolve`'s host lookup missed. In fact **the
  resolver was never called for `boot.bin` at all** — tftpy serves a static file
  from its root in preference to its dynamic hook, and `boot.bin` was in that
  root. Fixed in marquee 0.8.0.

  With the resolver running, the real limit is now recorded: the BIOS fetches
  `boot.bin` **before DHCP**, using the gateware's own IP *and* MAC — and
  `eth_mac_address = 0x10e2d5000001` is **hardcoded in `colorlight.py`**, so
  every panel built from a bitstream presents identical addresses. The request
  carries no panel identity, and no lookup can recover it.

  **Per-panel firmware is therefore a flashing operation**, which `flash-all`
  now does and verifies. Netboot staging would require a per-panel bitstream
  with a distinct `--ip`.

## [1.28.1] - 2026-09-15

### Documentation
A pass over every document for things that had quietly stopped being true.

- **`TODO.md` item 2 closed.** It said the flash targets "have never been run by
  anyone" and that **`/api/reboot` DOES NOT WORK**. Both were true when written
  and are not now: flashing has been run repeatedly and verifies, and the reboot
  endpoint was a *stub* — which is exactly why the 2026-09-09 investigation saw
  HTTP 200 and no TFTP transfer — fixed in v1.27.0. The programmer acceptance
  test that item 3 demands has been run on the FT2232 (`ft2232_b`): two 1 MB
  dumps, byte-identical.

  What remains open is recorded precisely: **nothing has yet proved the board
  boots from SPI flash**, because every bring-up so far used a JTAG SRAM load
  and `openFPGALoader --reset` does not make this ECP5 reconfigure from SPI.
  That needs a physical power cycle, and the tell is whether the panel fetches
  `boot.bin` at all.

- **`CLAUDE.md` described the wrong bench.** It said six 128x64 panels at
  256x192; it has been two panels at 128x128 for some time. A stale test-rig
  description is quoted as fact by the next session, so it now carries a note
  saying to check the hardware before trusting it.

- **Boot precedence is written down** in `API.md`, `ARCH.md` and the guides:
  TFTP config > compiled-in default > a single panel expecting a stream.

- **`HARDWARE.md` gains the flash map** — and why the free region at `0x200000`
  stays free. Caching a fetched config there was considered and rejected: a
  stored copy that can silently disagree with what the fleet server is serving
  is a second source of truth, and a panel quietly ignoring a config change is
  worse than one that has no config. Compile-time follows the model the
  bitstream already uses for geometry.

- `FPGA-GUIDE.md` covers what `build.rs` now does; `README.md`,
  `GETTING-STARTED.md`, `INSTALLATION.md` and `PROGRAMMING.md` all cover
  `--default-config` and the no-data consequence of running offline.

## [1.28.0] - 2026-09-15

### Added
- **A panel config compiled into the firmware — a panel that needs no network.**
  `./build.sh --default-config <file> firmware` bakes a layout, and optionally
  `mode:`/`program:`, into the binary. `build.rs` reads it and emits a constant;
  the firmware parses it at boot with the **same `LayoutConfig::parse()`** that
  handles the TFTP config.

  Same format, same parser, one code path — so a config moves between the fleet
  server and the firmware unchanged, and the offline case cannot drift from the
  fleet case, which it would, being exercised far less often.

  The program code was always compiled in. What came only from the network was
  the single line **selecting** it, plus the panel-to-connector map. Without a
  baked config a network-less board comes up as one panel at the bitstream's
  geometry, expecting a stream that will never arrive — so it sits dark.

  Applied **before the network comes up**, so even a fleet panel draws its own
  content within a second of power-on instead of after DHCP has timed out.

  **A TFTP config still overrides it.** That ordering is deliberate: baking a
  default must never stop you changing a panel remotely. There is deliberately
  no runtime cache of a fetched config either — a stored copy that could
  disagree with what the server is serving is worse than no copy at all.

- `configs/standalone-128x128.yml` — a worked example for two stacked 128x64
  panels running `timessquare` with no network.

### Changed
- The TFTP config path and the compile-time default share one `apply_layout()`,
  extracted from `handle_tftp()`.

### Verified on hardware
Tested by making the fleet server **refuse** the panel's config (`tftp ... ->
not found`): the panel came up 128x128 in program mode running `timessquare`,
from the baked config alone. Then, with marquee set to a *different* program
than the baked one, a reboot brought it up on marquee's choice — confirming
TFTP still wins. `POST /api/reboot` drove both reboots, which is the v1.27.0
fix working on real hardware.

### Note
A standalone program has **no data** — no network means no Home Assistant, so
`ctx.val()` returns `"-"` for every key. A program meant to run offline should
take no values, or be written so missing data looks deliberate rather than
broken. Documented in PROGRAMMING.md.

## [1.27.0] - 2026-09-15

### Added
- **`scroll_message` / `scroll_message_rev`** — a ticker whose text is built
  from live values. `scroll_text` takes a `&'static str`, which a tape showing
  the temperature cannot use. The builder runs **only when a value lands**,
  typically once every several seconds; every pixel in between costs one extra
  `u32` comparison.

  Keyed on `ValueTable::writes` rather than on the frame, and that is not a
  performance decision — rebuilding per frame would not be measurable against a
  ~3.1M-cycle redraw. It buys a correctness property: **the message can only
  change LENGTH on a frame where a value arrived**, which is exactly when
  `program_tick` has already re-armed two full redraws. `cover_scroll` wraps
  modulo `len * advance`, so a length change shifts the scroll period; tying the
  rebuild to value arrivals means it can never leave a seam in a shifted band.
- **`Msg`** — a fixed 128-byte buffer implementing `core::fmt::Write`, with four
  slots keyed by the builder's function pointer so two tapes in one program get
  their own. Truncates rather than erroring: a ticker losing its tail is
  cosmetic, a `write!` failing mid-render would have to be handled per pixel.
  `Msg::pad_to()` keeps the period constant.
- **`timessquare` now runs on live Home Assistant data** — outside temperature
  and condition on the top tape, door/window/garage/unlocked state on the
  bottom, via marquee's value pusher. It is the full exercise of the system:
  both scroll directions, text from live values, a rotating SVG sprite, plasma,
  sparkle, a chasing border, palette cycling and a staleness marker.

  Measured **7.8 fps** at 128x128, up from 4.7: the two tapes are declared as
  `scroll:` bands, so they are shifted rather than re-rendered and only the
  uncovered columns pay for glyph lookups.

### Fixed
- **`POST /api/reboot` was a stub that lied.** It returned
  `{"ok":true,"rebooting":true}` and did nothing — the board simply kept
  running, which is indistinguishable from a reboot that happened and completed
  very fast. The reset now fires from the main loop ~300 ms after the response,
  because the response is still in a smoltcp TX buffer when the handler returns:
  resetting there drops the connection and the caller sees a hang instead of an
  answer. Verified: only the JTAG path and the telnet console could reboot this
  panel before.

### Documentation
A full set, written to be readable by someone who has never seen the project:
- **`docs/GETTING-STARTED.md`** — parts on a desk to a lit panel running its own
  program and playing a video, in about an hour, with a FAQ built from the
  questions that actually get asked. Every trap that has cost someone an evening
  is flagged where they will meet it.
- **`docs/FPGA-GUIDE.md`** — a beginner's guide to the gateware. What an FPGA
  is here, how to read Migen (`comb` is a wire, `sync` is a flip-flop), the
  HUB75 driver including why brightness is BCM and not PWM, the pixel DMA, and
  recipes for adding a register or a peripheral.
- **`docs/HARDWARE.md`** — what we found out about the 5A-75E itself, now
  including what the board is *for* and why that explains its quirks, revision
  differences, the RGMII `rx_delay` that is not optional, why `add_ethernet()`
  was not enough, power as the most common "software" fault, and a field
  reference card.
- **`docs/BENCHMARKS.md`** — every measured number with its conditions, and
  where a figure was once wrong, the correction beside it.
- **`docs/INSTALLATION.md`**, **`API.md`** (real captured responses), and
  `ARCH.md` rewritten to cover both pixel paths.
- `docs/ON-PANEL-PROGRAMS.md` -> **`docs/PROGRAMMING.md`**, with a full API
  appendix and a cheat sheet; a stub remains at the old path.

### Corrected
- **The "~6 fps full-redraw ceiling" was measured with a stream running.** With
  the stream off the same `blank` program measures **12.5 fps**. Every ceiling
  recorded before this was found is suspect for the same reason.

## [1.26.0] - 2026-09-15

### Added
- **Multi-band scroll.** `scroll:` takes a list as well as a single band, so
  independent tapes can run at different speeds and in opposite directions.
- **Backgrounds that survive a shift.** A shift moves pixels *along a row*, so
  anything constant in x is untouched by it — a tape can have a ground as long
  as it depends only on y. And because that ground is a palette *index*, the
  `palette:` hook animates it for **zero pixel writes**: a moving background
  behind shifted text, which a per-pixel composite could never afford.

  What does not work is an independently animating background inside a scrolled
  band; the shift drags it along with the letters.

## [1.24.0] - 2026-09-15

### Added
- **`prim.rs` — a drawing library.** `border`, `border_chase`, `rect`, `bar`,
  `text`, `text_centered`, `scroll_text`, `scroll_text_rev`, `sprite`,
  `sprite_rotated`, `plasma`, `sparkle`, `sweep`, `stale_marker`, `hue`,
  `shade`. Every one is a query — "what, if anything, goes at this pixel?" —
  and takes its bounding box first, so a pixel outside costs one comparison.
  The maths that used to live in each program (distance-to-edge for a ring, the
  hash that gives each pixel its own sparkle phase, inverse rotation) is now in
  one documented place.
- **`scroll:` — shift the glass, fill what it uncovers.** Rather than
  re-rendering a ticker, the renderer shifts what is displayed and calls the
  program back for only the columns that exposed. **10.4 -> 24.8 fps.**

  Not luck, arithmetic: a re-render costs ~5 memory accesses per pixel, a shift
  costs 2, and the expensive glyph lookups shrink to the handful of uncovered
  columns. Exact rather than approximate, because ticker content is a pure
  function of `x + frame * speed`.

  Copies front -> back, never shifts the back buffer, which holds the frame from
  two swaps ago rather than what is on the glass.

## [1.23.0] - 2026-09-15

### Added
- **Palette cycling.** A `palette:` hook runs once per frame: **256 words, flat
  in wall size**. Nine panels animate for the cost of one, and nothing else in
  this system has that shape. The `ticker` program moves colour *along* its
  letters without rewriting a pixel.
- **`Font::cover_scroll`** — O(1) scrolling text on a monospace face. The
  character under a pixel is one divide instead of walking the run, so cost
  stops depending on message length. The message wraps, so no second copy is
  needed to cover the seam.

### Measured
- **Text is READ-bound, not write-bound.** Halving the writes bought 27%, not
  2x: a 40-row band gives 8.2 fps, an 18-row band 10.4. Glyph rendering does
  several dependent lookups per pixel and with no D-cache **a read costs the
  same ~190 cycles as a write**.
- Consequence for scrolling: **step size is smoothness, frame rate is the
  budget.** At ~10 fps, 2px/frame is about as smooth as text gets here.

### Fixed
- **Debris along the top edge of the scrolling tape.** The `dynamic` band was
  `[56, 74]` while the font inks from row **53**. Rows outside the band are
  written only on a full frame, so anything drawn there stays. The band must
  cover every row the content *can* ink, not the rows it usually does.

## [1.22.1] - 2026-09-15

### Performance
- **`display.dynamic` — redraw only the rows that change.** `dashboard` goes from
  **1.2 to 8.5 fps** by skipping 110 of 128 rows. A framebuffer store costs
  ~190 cycles on this core, so **not writing a word is the only real speedup
  available**.

  Requires **two** full frames to settle, because the framebuffer is double
  buffered: a row written once lands in one buffer while the other still holds
  the old content. A value arriving re-arms a full pair, since only the program
  knows where it drew that value.

  Does nothing for a program where every pixel moves — `timessquare` was
  unchanged at 4.7 fps.

## [1.21.0] - 2026-09-15

### Added
- **On-panel programs.** A panel can now compose its own pixels from named
  values and receive no frames at all. `POST /api/program/on`, or `mode:
  program` in its TFTP config so it survives a reboot. See
  [docs/ON-PANEL-PROGRAMS.md](docs/ON-PANEL-PROGRAMS.md).
- **`panelc`** (`tools/panelc.py`) — compiles `panels/*.yaml` into Rust that is
  linked into the firmware. Widgets become ordered early-returns; a `lambda:`
  splices raw Rust in at its position in that order. YAML is a BUILD artifact:
  an interpreter was measured at ~0.3 fps, so this is a compiler, not a VM.
- **Fonts and SVG sprites**, rasterised at build time into 4bpp coverage and
  blitted through 16-entry palette ramps — `ramp_base + coverage`, one add. An
  SVG icon and a large glyph are the same asset type and share one pipeline.
- **A value data plane**: `POST /api/values`, a 16-key table with per-key age
  and a staleness clock. ~20 bytes/s against the ~24 Mbit/s a streamed panel
  eats to show a static clock face.
- `GET /api/programs`, `GET /api/fb` (front-buffer dump), `GET /api/values`.
- Three programs: `dashboard` (dashboard), `timessquare` (tickers, spinning
  star, plasma, glitter — takes no data), `blank` (the performance yardstick).
- A 128-entry rainbow palette band, `hash2`, `sin64`/`cos64`, `unrotate`, and
  `Font::cover_scroll` for O(1) scrolling text on a monospace face.

### Fixed
- **The MTU was 2048 — the MAC slot size, not the Ethernet MTU.** smoltcp
  advertised MSS 1994 and built segments too large for a 1500-byte link, which
  were dropped before reaching the wire. Every HTTP response bigger than one
  frame sent only its tail, so the status page NEVER loaded while every
  sub-1460-byte JSON endpoint worked perfectly. UDP was untouched, which is
  exactly why streaming always worked and the web UI never did.
- **Hold was a lie.** It stopped the CPU parser while the gateware pixel DMA
  kept writing the framebuffer, so a panel reporting itself held was still
  being overwritten ~10 times a second.
- **Wrong-sized frames** are refused and counted as `bad_size` rather than
  drawn as a garbled part-frame with every counter reading healthy.
- **The version banner overdrew the test pattern.** It is suppressed when it
  cannot fit, and left-aligned so it crosses only a vertical rather than both
  diagonals.
- A native pattern now exits program mode, so the two cannot fight over the
  framebuffer.

### Performance
- **The CPU has always had the M extension** (`lite` VexRiscv is
  `-march=rv32i2p0_m`), but the firmware was built for plain `riscv32i`, so
  every multiply and divide was a SOFTWARE ROUTINE. `-C target-feature=+m`:
  0 → 184 hardware mul/div instructions, and 6x on a program.
- Row-at-a-time rendering, bounding-box guards on text and icons, and
  `Ctx::int()` so per-pixel code never touches soft float.
- **The ceiling is ~6 fps for a full-screen redraw**, measured with `blank`.
  It is the store path, not the program. Smooth motion needs a cheaper update
  path — partial redraw, `fb_base`, or the gateware blitter — not faster code.

## [1.11.1] - 2026-09-14

### Fixed
- **Indexed mode never actually displayed correctly -- the output mode was
  clobbered on every completed frame.** `process_packet()` selects the output
  mode from the wire magic, but three call sites then forced
  `OutputMode::FullColor` immediately afterwards, on every frame that
  presented: the smoltcp path, the `is_bitmap_udp()` fast path, and the
  staleness tick. Indexed content therefore scanned out as full colour: the
  palette index sits in the low byte of `0x00GGRRBB`, so every pixel rendered
  as a single channel at index brightness -- the whole sign ONE COLOUR with the
  shapes still legible.

  It survived every test because **no counter can see colour**. `bad_magic` was
  0, chunks and frames counted up, and `tools/test_indexed_panel.py` reported
  PASS at 2.84x. Only looking at the glass found it. The mode now follows the
  wire format as intended, and `/api/display` reports `indexed` while indexed
  frames are streaming.

- **`BitmapReceiver` cached the mode it *believed* the hardware was in**, so
  once anything else wrote the CSR it never re-asserted. The one remaining
  external setter (the HTTP test-pattern handler) now calls
  `invalidate_mode_cache()`. The guard stays cached rather than reading the
  CSR back: a read there is bus traffic contending with the pixel DMA, and it
  slowed the receive ISR enough to overflow the MAC FIFO -- the CPU then missed
  chunks, the arrival mask never filled, and NO frame completed while the DMA
  went on drawing pixels.

- **The version banner was invisible on any panel narrower than ~176px.**
  `is_version_pixel()` computes `square_right - square_left - text_width` in
  `usize`; at 128 wide that is `31 - 1 - 42`, which underflows and wraps to
  ~1.8e19 in a release build, so `col < start_x` was always true and nothing
  drew. It was only ever visible because the old wall was 256 wide. Now falls
  back to centring across the full width when the quarter-cell is too narrow.

### Verified on hardware
Two 128x64 panels stacked (128x128), bench panel 192.168.1.50:
`mode: indexed`, `frames_completed` climbing at the full 10 fps,
`last_missing: 0`, `bad_magic: 0`, and the RGBW swatches confirmed as four
distinct colours on the glass.

## [1.11.0] - 2026-09-14

### Added
- **Indexed bitmap wire format (`'B','I'`) -- ~3x fewer packets per frame.**
  One byte per pixel instead of three, so the same MTU carries 3x the pixels.

  This targets the right bound. The CPU is already out of the streaming pixel
  path (`Hub75UdpDma` takes `udp_sink` directly), so the ~600k px/s unpack
  ceiling is not what limits streaming -- item 9 measured **per-interrupt
  cost**, ~1.13 packets per ISR entry and ~20.6 fps at 256x192 where a frame is
  ~101 chunks. Indexed makes that frame ~34 chunks (verified against marquee's
  sender), moving the interrupt bound by the same factor.

  The chunk stride is derived from the format rather than given its own
  register: an indexed chunk carries exactly `3 * pixels_per_chunk`.

  **The framebuffer format is unchanged** -- an index is written as `0x000000II`,
  the low byte the palette lookup already reads. So this touches neither the
  scan-out path nor `write_img_rgb888`, and is unaffected by the per-panel
  rotation constraint that limits index *packing*.

- **Palette packets (`'B','P'`).** Byte 4 is the first entry, then RGB triples;
  256 entries fit one 778-byte packet and partial updates are legal. Re-sending
  a rotated palette animates the whole wall for ~778 bytes a frame, whatever the
  panel count. The DMA passes these to the CPU **without** counting bad_magic,
  so that counter stays a fault signal.

- **The wire format now drives the OUTPUT mode.** `bitmap_udp.rs` calls
  `set_mode()` when the magic changes, so `'B','I'` puts the panel in indexed
  output and `'B','M'` returns it to full colour. Guarded, so it is a CSR write
  per format change rather than per packet.

  Without this, sending an indexed stream to a panel left in full colour renders
  every palette index as a blue value -- a dim, plausible-looking picture rather
  than an obvious fault, which is the worst way for it to fail. Making the magic
  authoritative also means there is no second setting to drift out of sync and
  no mode endpoint for a caller to forget.

- `write_img_indexed()` for the CPU fallback path: four pixels per aligned word
  load against the same four stores, where RGB888 needs three loads.
- `tools/test_indexed_panel.py` -- the end-to-end check against one live panel.
  Streams RGB then indexed for a fixed window each and compares the PANEL's own
  counters (the sender only knows what it put on the wire, not what was drawn):
  chunks per frame, frames completed, `palette_writes`, `bad_magic`. The test
  pattern is 16 flat colours so quantisation is lossless and the two passes must
  look IDENTICAL -- any visible difference is a real fault, not a test artefact.
- `palette_writes` in `/api/status` -- a palette that never arrived and a
  palette full of black look identical on the wall.
- `docs/DISPLAY-PROGRAMS.md` -- design note for composing content ON the panel
  rather than streaming it, and the staged plan this release is step one of.
- `dev-fast` cargo profile. Every build was full LTO with `codegen-units = 1`;
  the release firmware build measures 10m21s, which is the wrong thing to sit
  behind while tuning.

### Fixed
- **`MAX_CONFIG_SIZE` was 512 and truncated SILENTLY.** Raised to 2048, and a
  truncated config is now REFUSED rather than partly applied. Measured: a
  6-panel layout is 311 bytes and a 9-panel 3x3 is 347, so the old cap sat ~165
  bytes from dropping connectors -- which on the wall is indistinguishable from
  dead panels or bad cabling.
- **Framebuffer size comments were 4x low.** `FB_TOTAL_WORDS` is 524,288 and a
  buffer is 262,144 words; the comments said 131072/65536 and `CLAUDE.md`
  claimed "8 panels and no headroom". 16 panels is half a buffer. `TODO.md`
  item 4 corrected the arithmetic on 2026-09-09 and the fix never reached the
  code.
- **Const asserts that `layout::MAX_OUTPUTS`/`MAX_CHAIN` cover
  `OUTPUTS`/`CHAIN_LENGTH`.** `parse()` tests `n <= MAX_OUTPUTS` and drops the
  rest silently, so growing the wall without updating `layout.rs` lost
  connectors with no error. Now a build failure.

### Verified
Firmware compiles; bitstream builds and **timing closes** -- `main_crg_clkout0`
61.04 MHz against a 40 MHz target (the extra mux levels cost no margin),
`eth_clocks0_rx` 130.01 MHz against 125. SVD diff is timestamps only, so no CSRs
changed and the PAC is untouched.

### NOT verified
Nothing has run on a panel -- the board is powered down. Frame-rate gains are
predicted from the chunk-count model, not measured on hardware.

Frame-rate gains are predicted from the chunk-count model; the chunk reduction
itself is verified (101 -> 34 at 256x192, against marquee's sender), the
frame-rate consequence is not. `tools/test_indexed_panel.py` is what settles it.

## [1.10.11] - 2026-09-07

### Fixed
- **Broadcast ARP stalled the pixel stream.** The panel handled every ARP frame
  on the segment through smoltcp on the SLOW path -- inside the same interrupt
  handler that consumes streamed pixels -- so roughly one broadcast per second
  became a short gap in frame processing, visible as a periodic stutter in
  scrolling text. `is_foreign_arp()` now acks and discards ARP not addressed to
  this panel in the fast path, without waking the network stack.

  ARP *for* the panel is still handled: dropping it would let the sender's ARP
  cache expire and stop the stream entirely, which is far worse than a stutter.
  The filter is inert until an address is assigned, so it can never interfere
  with bring-up. `arp_dropped` is published alongside `slow_arp` in
  `/api/status`.

  Measured on the bench panel: `slow_arp` 15,553 before; after, 1.0 foreign ARP
  per second dropped in the fast path and **zero** reaching smoltcp.

  This is a mitigation, not the cure -- the panels share a general-purpose /24
  (`mcast_dropped` 1.4M, `mac_overflow` 806k). A dedicated panel VLAN removes
  the broadcast domain instead of filtering it; recorded as TODO item 6.

- **Art-Net colours had red and blue swapped.** `artnet.rs` packed
  `0x00BBGGRR` where the framebuffer word is `0x00GGRRBB`. The v1.10.1
  colour-order fix corrected `patterns.rs` and `bitmap_udp.rs` and missed this
  file.

- **Art-Net accepted anything.** Every header check -- magic, opcode, protocol
  version -- was commented out, so ANY UDP datagram of 18 bytes or more
  reaching that port was decoded as pixel data and written to the display.
  Validation restored, with the length bounded against the packet.

### Added
- **Hardware chunk-arrival tracking (gateware).** `Hub75UdpDma` now sets a bit
  in a 256-bit bitmap as each chunk *finishes writing*, exposed with the
  frame_id it describes. This is the ground truth the CPU currently lacks: its
  own mask counts packets it received, which is a different set from what the
  DMA actually wrote, so a frame can be marked complete and shown with stale
  bands.

  **The firmware does not read these registers yet** -- the bitstream carries
  them and the CPU still uses its own mask. Wiring it up is TODO item 1.

## [1.10.10] - 2026-09-07

### Fixed
- **The interrupt handler was overwriting the firmware's own code.** The
  assembly trap vector in `main.rs` incremented its counter at a hardcoded
  `0x40020000`. Nothing reserves that address: `.text` spans
  `0x40000000-0x40026938`, so every single interrupt wrote the ISR count over an
  instruction inside the running image. Confirmed on hardware — `mcause=2`
  (illegal instruction) with `mepc=0x40020000`, and that address disassembles to
  an instruction in `BitmapReceiver::present()`.

  This is why v1.10.8 and v1.10.9 crashed under packet loss while v1.10.6 did
  not: the bug is old, but the clobbered word only matters if it is executed,
  and v1.10.8's layout moved `present()` onto it. The corrupted word lands in a
  stats-increment path that runs only for *damaged* frames, so clean streaming
  never touched it. It was intermittent (~1 run in 3) because the word holds the
  live interrupt count, and some counter values happen to decode as legal
  instructions.

  The counter is now a linker-allocated `ISR_COUNTER` in `.bss`.

- **The panic handler could never finish.** `panic.rs` spun on
  `while uart.txfull().read().bits() != 0 {}` with no bound. This board has no
  serial console, so nothing drains the TX FIFO: once it filled, the handler
  spun forever and never reached its own `soc_rst`. That turned every panic into
  an unrecoverable hang, and it is why identical firmware appeared to "reset" on
  some runs and "hang" on others — it depended only on how full the FIFO was.
  The wait is now bounded and the message is dropped rather than the reboot.
  The handler also masks interrupts on entry; it previously ran with them
  enabled, so the network ISR kept firing while it was stuck.

- **The trap vector ignored `mcause`.** It assumed every trap was the network
  interrupt and called `network_handler` unconditionally, so a CPU exception
  re-entered the failing code instead of being reported — invisible without a
  serial console. Exceptions are now recorded and reset cleanly.

- **`load_image()` accepted uninitialised flash as a valid image.** Its only
  check was bit 31 of the first word. Erased flash (`0xFFFFFFFF`) is rejected by
  that, but stale flash is not: the actual contents at chip offset `0x300000`
  are `0x0000B080`, which passes, yielding `width=45184` and `length=4201088`
  straight into the HUB75 width CSR and every buffer bound in `hub75.rs`. Now
  bounds-checked against `MAX_IMG_PIXELS`.

- **`read_img_data()` sliced by `self.length` with no clamp**, unlike every
  other consumer of that field. Reachable from the image-save path in `menu.rs`.

### Added
- **Firmware version on the panel.** The running version is drawn top-left at
  boot, so the display itself reports which build is loaded. Identifying the
  running firmware otherwise means correlating a TFTP log against a build
  artifact, which cost hours when a 210-day-old daemon was serving a
  months-old `boot.bin`.
- **`breadcrumb` module** — records how far execution got before a crash, in
  uncached SDRAM that survives `soc_rst`, readable from `/api/status`
  (`prev_mark`, `prev_mcause`, `prev_mepc`). This is what located the bug above
  after bisection failed.

### Verified on hardware
- 10 consecutive health-gated drop-test runs: **zero resets, zero hangs**, all
  breadcrumbs clean.
- Tier 0 now does what it was written to do. Omitting the last chunk (#67):
  `partial=10, dropped=0, repaired=10` — every frame presented, each missing
  band patched. v1.10.6 on the same gateware loses those frames outright.
- `chunks_repaired` increments for the first time; it never could before,
  because the firmware crashed before completing a repair.

### Corrected
- **Tier 1's "~2x throughput" is not supported by measurement.** Sweeping
  v1.10.10 against v1.10.6 on identical gateware: v1.10.10 is better at moderate
  rates (3.5% vs 11.1% loss at 1.20ms) and comparable at 0.80ms (9.8% vs 11.0%),
  but both are clean only to ~2ms (7.35fps). No 2x. The previously documented
  "clean to 15.59fps" baseline was measured on the mismatched flash gateware and
  does not reproduce here.

## [1.10.9] - 2026-09-06

### Fixed
- **`img_flash` addressed the wrong peripheral.** `read_byte()` and
  `read_image()` hardcoded a `0x80000000` mmap base with a 2MB span. That
  address is the EthMAC buffer region, not the flash window, and has been wrong
  since the spiflash region moved out of it — `read_image()` built a 512KB slice
  at `0x80180000`, which is mapped to nothing in either the old or new memory
  map, and it runs on every boot from `main.rs:174`. Now uses a named
  `MMAP_BASE` documented as having to track the gateware.
- **`FLASH_SIZE` was 2MB**, sized for the GD25Q16 the gateware no longer
  declares. It drives `img_offset = (FLASH_SIZE / 4) * 3`, so stored images sat
  at `0x180000` — only 512KB above the firmware at `0x100000`, with `boot.bin`
  already 218KB, close enough that a growing firmware would eventually be erased
  by `write_image()`. At the correct 4MB the image store moves to `0x300000` and
  headroom goes from 300KB to 1.8MB.
  (Both found by the the build host session while reviewing my claim that no firmware
  source referenced the flash addresses. It did; my grep was too narrow.)

### Changed
- **Repair budget cut from 8 chunks to 2.** Patching missing chunks runs in the
  ISR, which makes it the one part of the loss handling that spends time exactly
  when the system is already behind — packets keep arriving during the patch, so
  an over-generous budget under heavy loss feeds back into more loss. Two caps it
  under a millisecond, and it is the same threshold the pre-v1.10.8 code used
  (`chunks_count >= total - 2`), so nothing that used to reach the panel stops
  reaching it; those chunks are simply patched now instead of showing a
  two-frame-old band.

### Known
- Still untested on hardware. v1.10.8 has never completed a clean bench run: the
  one attempt crashed the board mid-test, on a rig with a long-standing JTAG
  fault, and the cause has not been isolated to firmware or hardware.

---

## [1.10.8] - 2026-09-06

Streaming reliability and throughput. All measurements from
`tools/bench_stream.py` against the live panel; see `CLAUDE.md` for the baseline.

### Fixed
- **A lost packet no longer stalls the frame.** The receiver tracked progress
  with a counter and declared a frame complete when the packet carrying the
  *last* chunk index arrived. Measured: omitting only chunk 67 of 68 left
  **0 of 10 frames completing** — the frame reached the panel late via the
  partial-swap path or not at all, so loss froze the display rather than
  degrading it. `BitmapReceiver` now records *which* chunks arrived in a
  256-bit mask and completes on the mask being full, so nothing depends on one
  specific packet.
- **A lost packet no longer shows a two-frame-old band.** Chunks that never
  arrived are now patched from the displayed buffer before the swap
  (`Hub75::repair_from_front`), so a gap is one frame stale instead of two.
  Bounded to 8 chunks per frame (~3.4 ms of ISR time); a frame missing more
  than that is dropped and the previous frame stays up. Every presented frame
  therefore has each chunk either freshly written or patched, so a partial
  frame cannot leave stale data behind for a later frame to inherit.
- **A frame is presented when the stream goes quiet.** `bitmap_tick()` runs
  from the main loop and flushes an in-progress frame after 250 ms of silence.
  Previously the ISR could only finish a frame when a packet arrived, so the
  last frame before a sender stopped hung indefinitely.
- **A malformed packet no longer reboots the board.** `total_chunks` was a
  `u8` validated only against `chunk_index`, never against the frame size, so
  `chunk_index * 487` could index past the framebuffer — and the resulting
  panic issues `soc_rst`. One UDP packet to port 7000 with
  `total_chunks=255, chunk_index=200` was enough. Indices between 68 and 134
  did not panic but silently corrupted the other buffer through an underflowing
  `self.length - offset`. Now bounded against the configured image length.
- **Stats no longer freeze when frames stop completing.** The snapshot copy to
  `BITMAP_STATS_PTR` sat inside `if complete`, so `bad_magic`, `bad_header` and
  `frames_dropped` were unreadable in exactly the failure regime they exist to
  diagnose. Published unconditionally now.
- **Duplicate and reordered chunks no longer corrupt the arrival count** — they
  are detected via the mask, counted separately, and skipped rather than
  rewritten.

### Changed
- **Payload is read 32 bits at a time** (`Hub75::write_img_rgb888`). This SoC's
  VexRiscv_Lite has an I-cache but no D-cache, so every access is a full bus
  round-trip; the byte-at-a-time RGB unpack cost 4 of them per pixel (3 loads +
  1 store, ~17 cycles each, ~67 cycles/pixel total — which is exactly the
  measured ~600,000 px/s ceiling). Four pixels are twelve bytes, so the
  word-wise form issues 3 loads + 4 stores per 4 pixels instead of 12 + 4.
  The alignment this needs always holds: eth(14) + ip(4*IHL) + udp(8) +
  header(10) is a multiple of 4 for every legal IHL, inside a 2048-aligned MAC
  slot. Output is bit-identical to the old path, verified exhaustively across
  every chunk length including the short final chunk.
- **The test sender paces against an absolute deadline** instead of calling
  `time.sleep()` per packet, which drifts — a requested 0.8 ms delivered ~0.95 ms
  and the error compounded across a frame. Default spacing is now 0.8 ms
  (~1250 pkt/s, the measured limit) rather than 10 ms.

### Added
- `tools/bench_stream.py` — throughput sweep, per-pixel-vs-per-packet cost
  isolation, and single-chunk-loss behaviour, with the v1.10.6 baseline
  recorded in the script header.
- New counters on `/api/bitmap/stats` and the dashboard: `duplicate`,
  `frames_stale`, `chunks_repaired`, `last_missing`.

### Known
- Untested on hardware. The gateware bitstream and this firmware both need a
  build and flash from `the build host` (the USB-Blaster host, 192.168.1.10 leg); the
  panel is still running v1.10.6.

---

## [1.10.7] - 2026-09-06

### Fixed
- **Flash boot on rev 8.2 boards** — `gateware/colorlight.py` declared a `GD25Q16`
  (2MB GigaDevice) while rev 8.2 hardware carries a **W25Q32JV** (4MB Winbond). The
  SPI flash never enumerated, so the BIOS always fell through to TFTP network boot and
  the board could not survive a power cycle without a TFTP server running. Now
  instantiates `W25Q32JV`.
  - Both parts share the `READ_1_1_1` opcode, a 256-byte page and 8 dummy bits, so
    `SPIFLASH_PAGE_SIZE` is unchanged and only the mapped size differs.
  - The SPI flash region grows 2MB → 4MB and its origin moves to `0x80400000`
    (spanning `0x80400000`–`0x80800000`): LiteX requires a region's origin be
    aligned to its size, and the old `0x80200000` was chosen for the 2MB part.
    Clear of EthMAC at `0x80000000` and main_ram_uncached at `0x90000000`.
  - `FLASH_BOOT_ADDRESS` follows the origin to `0x80500000`, but the **chip**
    offset it maps to is unchanged at `0x100000`.
  - **Requires a bitstream rebuild and a flash write** — gateware, not firmware:
    `./build.sh bitstream pac firmware` then `./build.sh flash`.
- **Dashboard reported a phantom interrupt fault** — the status page always showed
  `mstatus.MIE` as a red "disabled". The page is rendered from inside the trap handler
  (all network processing runs in the ISR), where hardware has already cleared `MIE` on
  trap entry, so that bit could never read as set. Now reports `mstatus.MPIE` (bit 7),
  which preserves the pre-trap value and is the bit that actually indicates whether
  interrupts are enabled.

### Documentation
- Memory map in `ARCH.md` and `README.md` updated to 4MB SPI flash.
- The "Flash Boot Fails (rev 8.2)" known issue in `ARCH.md` rewritten as fixed, with
  the rebuild-and-flash procedure.
- Panel configuration docs clarified: per-panel size vs virtual display size, and the
  current test setup as a 2x2 grid of four 128x64 panels on J1+J2 (256x128 virtual).

### Repository
- Project relocated to `/share/src/colorlight` per the non-container-code convention.
- Stale `fastclock` branch and its worktree removed (fully merged; its uncommitted
  `--delay` work was already superseded by `--delay`/`--smoke` on `main`).
- Fixed `HEAD` in the canonical bare repo, which pointed at a nonexistent
  `refs/heads/master` and made every fresh clone fail.

---

## [1.10.6] - 2026-02-09

### Fixed
- **TFTP config loading reliability** — boot config now loads 100% of the time:
  - Removed the `had_udp` gate on `handle_tftp()` — `iface.poll()` can process multiple
    packets internally, so the peeked-packet classification flags don't always reflect
    what smoltcp actually consumed.
  - Fixed `socket.close()` dropping a queued ACK by deferring the close to the next ISR
    call, after `iface.poll()` has transmitted it.
  - Added port 6900 (TFTP client) to the `is_unwanted_udp` allowed list.
  - Fixed `is_unwanted_udp()` to use the variable IHL field instead of assuming IHL=5.
- **Fallback panel size** — default panel geometry is now read from the bitstream CSRs
  instead of a hardcoded 96x48, so the fallback layout matches the actual hardware.
- **Streaming regression from the above** — making all socket handlers and `iface.poll()`
  unconditional cost 25K+ dropped frames during streaming. TCP handlers (telnet, HTTP)
  stay unconditional for responsiveness; UDP handlers are gated on
  `had_slow_path || tftp_active`, and `iface.poll()` on
  `had_slow_path || http_needs_poll || tftp_active`.

### Changed
- Cargo cache persisted across Docker builds via `CARGO_HOME`, so dependencies are not
  re-fetched when GitHub is unreachable.

### Documentation
- `ARCH.md` main-loop section rewritten for the ISR-driven architecture; boot sequence
  documented; three-layer configuration model (bitstream / firmware / YAML) clarified.
- Corrected 256x64 references — the default build is 128x64 with `chain_length=2`, not
  a single 256x64 panel.

---

## [1.10.5] - 2026-02-09

### Fixed
- **HTTP connection close** — Fixed Chrome spinner never stopping by calling `iface.poll()` only when HTTP socket closes (needs to send TCP FIN). Returns bool from `handle_http()` to signal when poll is needed.

---

## [1.10.4] - 2026-02-09

### Fixed
- **Selective handler dispatch** — Reduced ISR time for slow-path packets by only calling relevant handlers based on packet type:
  - ARP packets: Skip all UDP handlers (4 calls saved per packet)
  - TCP packets: Only call TCP handlers (telnet, http)
  - UDP packets: Call all handlers as before
  - TCP handlers now run every ISR for reliable HTTP (cheap when idle)

### Changed
- Drop rate improved from 3.5% to <1% during streaming by reducing handler overhead

---

## [1.10.3] - 2026-02-09

### Fixed
- **MAC overflow during streaming** — Fixed multiple issues causing packet loss during video streaming:
  - Added ISR batch limit (64 packets max) to prevent interrupt starvation when packets arrive faster than processing
  - Filter out multicast packets (VRRP, mDNS) in fast path instead of slow smoltcp processing
  - Filter out unwanted UDP broadcasts (NetBIOS port 138, etc.) - only keep ports 7000, 6454, 67/68, 69
  - Fixed `is_bitmap_udp()` to handle variable IP header length (IHL field)

### Added
- **Slow path traffic breakdown** — Debug counters now show packet type breakdown: `slow_arp`, `slow_tcp`, `slow_udp`, `slow_other`
- **Multicast drop counter** — Tracks dropped multicast/unwanted UDP packets in `mcast_dropped`
- **HTTP dashboard diagnostics** — MAC Diagnostics card now shows slow path breakdown and multicast drop count

---

## [1.10.2] - 2026-02-09

### Fixed
- **Bitmap packets via smoltcp** — Added missing `handle_bitmap_smoltcp()` call to drain bitmap UDP packets that went through smoltcp when non-bitmap packets triggered `iface.poll()`. Previously, these packets would accumulate in the socket buffer and be lost.

---

## [1.10.1] - 2026-02-09

### Fixed
- **RGB color order** — Fixed color channel swap in HUB75 output (green showed as red, red as blue, blue as green). Both `patterns.rs` and `bitmap_udp.rs` now correctly pack pixels as GRB (0x00GGRRBB format) to match hardware expectations.

### Changed
- **Professional HTTP dashboard** — Redesigned web status page with dark theme, larger fonts (15px), wider cards (320px min), and comprehensive diagnostics:
  - Interrupt Status card showing ISR count, mstatus.MIE, mie.MEIE, IRQ_MASK state
  - Streaming card with FPS, jitter, frame completion stats
  - MAC Diagnostics card with overflow, CRC, and preamble error counters
  - Panel Assignments card showing J1/J2 chain slot mappings
  - Test pattern controls (grid, rainbow, rainbow_anim, solid colors)

---

## [1.10.0] - 2026-02-09

### Changed
- **Fully interrupt-driven network stack** — Complete rewrite of network handling. All packet processing now happens in ISR context:
  - Bitmap UDP packets use fast path (direct pixel writes, ~50μs per packet)
  - Non-bitmap packets processed via smoltcp (DHCP, HTTP, Telnet, Art-Net)
  - Main loop does zero network code — only display refresh and animations
- **Static network state** — All smoltcp state (interface, sockets, buffers) moved to statics for ISR access
- **Eliminated streaming mode** — HTTP/Telnet now remain responsive during active video streaming

### Added
- **`network.rs` module** — New 1000+ line module containing:
  - `network_handler()` — ISR-callable function for all packet processing
  - `is_bitmap_udp()` — Fast packet classification (5 byte comparisons)
  - `process_raw_bitmap()` — Direct pixel writes bypassing smoltcp
  - All HTTP handlers moved from `http.rs`
- **ISR count tracking** — Assembly trap handler increments counter at 0x40020000, visible in web UI

### Technical Notes
- ISR saves all 31 GPRs (128 bytes) before calling Rust code
- Interrupt re-enabled at end of handler after draining hardware FIFO
- Fast path processes bitmap UDP in ~50μs vs ~500μs through smoltcp

---

## [1.9.2] - 2026-02-08

### Changed
- **CPU variant changed to "lite"** — VexRiscv "minimal" variant doesn't support external interrupts. Changed to "lite" which enables full interrupt/CSR support (~25% larger CPU).
- **Experimental interrupt support** — Added trap handler and interrupt enable infrastructure for ETHMAC. Interrupts fire correctly but calling Rust code from ISR crashes, limiting usefulness to wake-up signal only. Enable via `POST /api/irq/enable`.

### Added
- **Debug endpoint `/api/irq/enable`** — Returns CSR state (mstatus, mie, irq_mask, irq_pending) and MAC event registers for interrupt debugging.
- **Ring buffer infrastructure** — 32-slot ring buffer in `ethernet.rs` for future ISR packet draining (currently unused due to ISR crash issue).

### Technical Notes
- mtvec is WRITE_ONLY in VexRiscv (reads return 0, writes work)
- LiteX EventManager is level-triggered; must check ev_status before re-enabling
- See NOTES.md for detailed interrupt investigation findings

---

## [1.9.1] - 2026-02-08

### Fixed
- **Test pattern timing** — Fixed timing issues in test pattern generation.
- **Include prebuilt bitstreams** — Added 128x64.bit and 256x64.bit to bitstreams/ directory.

---

## [1.9.0] - 2026-02-08

### Changed
- **Pre-clock-domain checkpoint** — Stable baseline before implementing dual clock domain architecture to improve UDP packet handling. No functional changes from v1.8.2.

---

## [1.8.2] - 2026-01-28

### Fixed
- **Boot grace period for streaming** — Firmware now ignores streaming detection for the first 10 seconds after boot, allowing DHCP, TFTP config loading, and network initialization to complete even if video is already streaming to port 7000. Previously, streaming could block the boot process by disabling slow-path networking before configuration was fetched.

---

## [1.8.1] - 2026-01-28

### Added
- **Version display in test pattern** — Grid test pattern now shows firmware version (e.g. "v1.8.1") in the second-row left square, avoiding diagonal lines for readability. Version is derived from Cargo.toml at compile time. Both Rust runtime pattern (`patterns::grid()`) and Python-generated default image (`gen_test_image.py`) include the version.

---

## [1.8.0] - 2026-01-27

### Changed
- **Skip slow path and discard non-bitmap packets during streaming** — While bitmap UDP frames are arriving, the firmware skips all slow-path processing (DHCP, telnet, HTTP, Art-Net) and discards non-bitmap packets in the fast-path burst loop via `ack_rx()` instead of calling `iface.poll()`. This eliminates multi-ms smoltcp stalls that were overflowing the 8-slot MAC FIFO. HTTP/telnet are unreachable during streaming. Services resume within 200ms of the last packet. Streaming detection uses `last_bitmap_packet_ms` (any packet, not just completed frames) to avoid getting stuck on partial final frames.
- **Auto chunk-delay in sender tools** — `send_video.py` and `send_youtube.py` now auto-calculate inter-chunk delay from fps and frame size when `--chunk-delay` is omitted: `(0.9 / fps) / total_chunks` (10% headroom). Explicit `--chunk-delay` still overrides. Enables higher panel counts (e.g. 8 panels at 10fps = 135 chunks) without manual tuning.

### Planned
- Re-enable Art-Net direct pixel writes
- Add serial console support documentation

---

## [1.7.0] - 2026-01-27

### Fixed
- **R/G/B color channels** — Physical pin mapping on Colorlight 5A-75E was incorrect (connector pins[0]=Blue, pins[1]=Red, pins[2]=Green). Gateware `Output` class now maps pixel data bits to correct physical pins. Also eliminates a bottom-row display artifact.
- **Bitmap UDP overwriting virtual width** — `process_packet()` called `set_img_param()` on every new frame, overwriting the TFTP-configured virtual width (e.g. 256 for chain_length_2) with the sender's physical width (128). Added dimension validation that rejects frames not matching the configured image size.

### Changed
- **Bitmap chunk tracking** — Replaced bitmask (u32/u64) with u16 counter, supporting up to 255 chunks per frame (scales to 12+ panel virtual displays). Frame completes when last chunk arrives even if earlier chunks were dropped by MAC overflow.
- **Slow-path frequency** — Non-bitmap work (telnet/DHCP/HTTP/ArtNet) runs every 5ms instead of 1ms, with extra `iface.poll()` calls between blocks. Reduces MAC RX overflow during bitmap streaming.
- **UDP receive buffer** — Bitmap socket increased from 32KB/24 metadata to 65KB/48 metadata slots.

### Added
- **256×64 bitstream** — Pre-built bitstream for 2×1 panel chain layout.
- **Debug readbacks** — TFTP handler logs `set_img_param` readback; `panel show` displays raw CTRL register; `layout apply` shows image params after apply.

---

## [1.6.0] - 2026-01-27

### Added
- **Panel chaining** — Each HUB75 output now supports 2 daisy-chained panels (`chain_length_2=1` in gateware), doubling panel capacity from 6 to 12 with zero extra EBRs.
- **YAML chain config syntax** — Space-separated positions per output: `J1: 0,0 1,0` assigns chain slot 0 at (0,0) and chain slot 1 at (1,0).
- **Telnet chain commands** — `panel show` displays all chain slots; `panel J1 0,0 1,0` sets both chain positions at once.
- **Web UI chain display** — Panels card shows `J1[0]`, `J1[1]` etc. per chain slot.
- **HTTP API chain support** — `GET /api/layout` returns arrays per output: `"J1":["0,0","1,0"]`; `POST /api/layout` accepts both array and legacy string format.
- **`--chain-length` build option** — `build.sh -l 2` and gateware `--chain-length 2` configure the chain depth.

### Changed
- `gateware/colorlight.py`: `BaseSoC` accepts `chain_length_2`, passed to `hub75.Hub75()` constructor.
- `build.sh`: New `CHAIN_LENGTH=2` config variable, `-l|--chain-length` CLI option.
- `sw_rust/barsign_disp/src/hub75.rs`: `CHAIN_LENGTH` = 2.
- `sw_rust/barsign_disp/src/layout.rs`: `assignments` changed from `[Option<(u8,u8)>; 6]` to `[[Option<(u8,u8)>; 2]; 6]`; `MAX_CHAIN = 2` added; `parse()` splits values on whitespace for chain slots.
- `sw_rust/barsign_disp/src/menu.rs`: `layout show/apply` and `panel show/set` display `J#[chain]` notation.
- `sw_rust/barsign_disp/src/http.rs`: Panels table, `api_layout_get/post` updated for chain arrays; new `json_get_array()` helper.

---

## [1.5.0] - 2026-01-27

### Changed
- **HUB75 outputs 4→6** — Expanded from 4 to 6 HUB75 outputs (J1–J6), using 53/56 EBRs on the ECP5-25F. Enables up to 6 independent panels.
- **JTAG retry logic** — SRAM programming now retries up to 5 attempts with a JTAG chain probe before loading, working around unreliable USB Blaster clones.

### Added
- **README: HUB75 Output Count section** — Documents that output count must be set in both `build.sh` and `hub75.rs` to avoid gateware/firmware mismatch.

---

## [1.4.1] - 2026-01-27

### Fixed
- **HTTP server stops responding** — Both TCP sockets on port 80 could get permanently stuck if a client connected without sending a complete request (browser prefetch, half-open connections, remote close). Added idle timeout (5s) and `may_recv()` check to abort stuck sockets so they re-listen.

---

## [1.4.0] - 2026-01-26

### Changed
- **HUB75 outputs reduced from 8 to 4** — Frees BRAM by halving the row buffer allocation (the largest BRAM consumer). Output count is now configurable via `--outputs` flag in gateware build and `build.sh -o|--outputs`.
- **MAC RX slots doubled: 4 → 8** — Uses freed BRAM for deeper ethernet receive buffering, eliminating MAC RX overflow drops during sustained UDP streaming.
- **Firmware matches new defaults** — `OUTPUTS=4`, `MAX_OUTPUTS=4`, `NRXSLOTS=8`; web/telnet UI shows J1–J4 only.
- **`build.sh` new option** — `-o|--outputs N` flag passed through to gateware build (default: 4).

### Technical Details
- `gateware/hub75.py`: All 4 submodule classes (`Hub75`, `RowController`, `RamToBufferReader`, `RamAddressGenerator`, `Output`) parameterized with `n_outputs`; signal widths computed dynamically from `n_outputs`.
- `gateware/helper.py`: Connector allocation parameterized.
- `gateware/colorlight.py`: `BaseSoC` accepts `n_outputs`, `nrxslots` bumped to 8, `--outputs` CLI argument added.
- `sw_rust/barsign_disp/src/ethernet.rs`: `NRXSLOTS` = 8.
- `sw_rust/barsign_disp/src/hub75.rs`: `OUTPUTS` = 4.
- `sw_rust/barsign_disp/src/layout.rs`: `MAX_OUTPUTS` = 4, bounds checks use constant.
- `sw_rust/barsign_disp/src/menu.rs`: Help text and validation updated for J1–J4.
- `sw_rust/barsign_disp/src/http.rs`: Layout API loop bounded by `MAX_OUTPUTS`.

---

## [1.3.1] - 2026-01-26

### Changed
- **Pin LiteX to 2025.12** — Dockerfile now fetches `litex_setup.py` from the `2025.12` tag and passes `--tag=2025.12` to ensure reproducible builds
- **TFTP server listens on 0.0.0.0** — `build.sh` binds TFTP server to all interfaces instead of a specific host IP
- **Rebuild bitstream and firmware** — Full rebuild with pinned LiteX to restore all network optimizations (4 RX slots, dual HTTP sockets, 32KB UDP buffer)

---

## [1.3.0] - 2026-01-26

### Added
- **BIOS broadcast TFTP on custom port** — BIOS now broadcasts its TFTP request to `255.255.255.255` on port 6969 instead of unicasting to a hardcoded IP on port 69. Any TFTP server on the subnet listening on port 6969 will respond. Eliminates the last hardcoded server IP.

### Changed
- **TFTP port 69 → 6969** — All three layers (BIOS bitstream, Rust firmware, dnsmasq server) now use port 6969 to avoid conflicts with other TFTP servers on the network

---

## [1.2.0] - 2026-01-26

### Added
- **Dynamic TFTP server discovery** — Firmware now discovers the TFTP server address from DHCP instead of using a hardcoded IP. Priority: `siaddr` header field → DHCP Option 66 → fallback `192.168.1.10`
- **DHCP Option 66 parsing** — Patched smoltcp to parse DHCP Option 66 (TFTP Server Name) as a dotted-decimal IP address; the DHCP client now requests Option 66 in its parameter request list
- **Boot server in web GUI** — Status page shows the active TFTP server IP and how it was discovered (siaddr, option 66, or fallback)

### Changed
- **smoltcp DHCP `Config`** — Now exposes both `server_ip` (siaddr) and `tftp_server_name` (Option 66) separately so firmware can distinguish the source
- **DHCP parameter request list** — Added Option 66 so DHCP servers (especially Windows DHCP Server) include it in responses

---

## [1.1.0] - 2026-01-26

### Added
- **Web reboot button** — New "System" section on status page with a Reboot button that triggers a full SoC reset via `POST /api/reboot`; response is sent before reset fires
- **Modern dark theme** — Status page restyled with dark navy background, system font, styled tables/buttons, responsive viewport meta tag
- **Higher contrast text** — Body text `#eee`, label column `#aaa`, 16px font for readability

### Changed
- **`./build.sh firmware` no longer starts TFTP server** — Build and serve are now separate concerns; use `./build.sh start` or `./build.sh boot` for TFTP
- **HTTP response buffer** — Increased from 2048 to 2560 bytes to accommodate the new CSS

---

## [1.0.0] - 2026-01-26

First stable release. All core features working and tested.

### Added
- **Multi-panel build system** — `./build.sh build-all` builds bitstreams for all 4 panel sizes (128x64, 96x48, 64x32, 64x64) in one command, with pre-built bitstreams committed to `bitstreams/`
- **Universal firmware** — Single firmware binary works with all panel sizes; only bitstreams differ per panel
- **TFTP auto-start** — `./build.sh firmware` and `./build.sh boot` automatically start the TFTP server if not already running; idempotent and non-blocking
- **128x64 default panel** — Default panel changed from 96x48 to 128x64
- **Architecture docs** — New `ARCH.md` with internals: memory map, double buffering, IAC state machine, hardware notes, debugging tips
- **Video streaming tool** — `tools/send_video.py` streams video files to the panel via UDP using ffmpeg

### Changed
- **Repository cleanup** — Legacy scripts, old notes, and unused code moved to `legacy/`
- **README rewritten** — Concise build instructions via `build.sh`, removed incorrect manual docker commands, added multi-panel workflow
- **Pre-built binaries** — All 4 panel bitstreams included in `bitstreams/` directory
- **`build.sh` improvements** — `build-all` target, `get_bitstream_path` for panel-aware flash/sram, `--panel` flag selects bitstream without rebuilding
- **Python tools** — All tools default to 128x64; `send_video.py` added with `--layout`, `--fps`, `--loop` options
- **TFTP configs tracked** — `.tftp/*.yml` board configs committed to git

### Fixed
- **TFTP not attempted during batch builds** — `ensure_tftp` only runs on explicit `firmware`/`boot` targets, not when called internally from `build-all`
- **TFTP failure non-fatal** — Warns instead of aborting the build if dnsmasq can't start

---

## [0.2.9] - 2026-01-25

### Added
- **Video streaming tool** — `tools/send_video.py` streams video files to the LED panel via UDP using ffmpeg for real-time decoding
  - Supports `--layout`, `--fps`, `--loop`, `--chunk-delay` options
  - Auto-detects video FPS via ffprobe
- **Fast bitmap receive loop** — Firmware drains all queued UDP packets per main loop iteration instead of one-at-a-time, reducing packet loss during video streaming

### Changed
- **LiteEth RX slots: 2 → 4** — Gateware change doubles hardware ethernet receive buffering (8KB), allowing higher sustained packet rates
- **Ethernet driver rewrite** — Direct buffer address computation bypasses broken LiteX SVD generator (which only describes 2 RX buffers regardless of `nrxslots`)
- **smoltcp burst size: 1 → 4** — Firmware processes up to 4 packets per `iface.poll()` call, matching the hardware RX slot count
- **Bitmap UDP RX buffer: 16KB → 32KB** — Socket buffer increased with 24 metadata slots (was 12) to hold full frames

### Performance
- 96×96 (2 panels): **19 fps** at 2.8ms chunk delay (was 10.8 fps before — 76% improvement)
- Reliable frame delivery at 3ms chunk delay (was 5ms)

---

## [0.2.8] - 2026-01-25

### Added
- **Web pattern selector** — Dropdown on HTTP status page to load test patterns (grid, rainbow, rainbow_anim, white, red, green, blue) directly from the browser
- **JTAG pinout in README** — Documented J27-J34 programmer connection pins

---

## [0.2.7] - 2026-01-25

### Added
- **TFTP boot config** — Firmware fetches `<mac-address>.yml` (e.g., `02-78-7b-21-ae-53.yml`) from TFTP server at boot
  - Simple YAML layout config: `grid`, `panel_width`, `panel_height`, `J1`..`J8` mappings
  - Automatically applies layout and redraws display at new virtual size after config load
- **Patched smoltcp** — Local patch adds `server_ip` (DHCP `siaddr`) to `Dhcpv4Config` for TFTP server discovery
- **Persistent bitstream flash** — `./build.sh flash` now uses `--board colorlight` flag for reliable SPI flash writes (seconds instead of hours)

### Changed
- `build.sh` — TFTP server stays running after `boot` (firmware needs it for config fetch); `flash` uses `--board colorlight` for correct flash chip handling
- `tftp_config.rs` — Accepts dynamic filename instead of hardcoded `layout.cfg`
- `layout.rs` — Parses YAML-style `key: value` separators in addition to `key=value`
- `http.rs` — Web page title renamed from "Barsign" to "Colorlight"

### Fixed
- **Flash programming speed** — `--board colorlight` flag tells openFPGALoader the correct flash chip, eliminating timeout/retry on every sector write

---

## [0.2.6] - 2026-01-25

### Added
- **HTTP REST API** - Web status page and JSON API on port 80
  - `GET /` — HTML status page with MAC, IP, display config, panel layout
  - `GET /api/status` — JSON system status
  - `GET /api/layout` / `POST /api/layout` — Get/set panel grid layout
  - `POST /api/layout/apply` — Apply layout to HUB75 hardware
  - `GET /api/display` / `POST /api/display/on` / `POST /api/display/off` — Display control
  - `POST /api/display/pattern` — Load test patterns (grid, rainbow, rainbow_anim, solid colors)
  - `GET /api/bitmap/stats` — Bitmap UDP receiver statistics
- **Dual HTTP sockets** — Two TCP sockets on port 80 so one can accept new connections while the other completes graceful TCP close, eliminating "site cannot be reached" on browser refresh

### Changed
- `http.rs` — New module with minimal HTTP/1.1 request parser, response writer, and route dispatcher

---

## [0.2.5] - 2026-01-25

### Added
- **DHCP client** - Firmware now acquires IP address via DHCP at boot using smoltcp's `Dhcpv4Socket`
  - Falls back to static IP `192.168.1.50/24` after 10 seconds if no DHCP server responds
  - Applies gateway route from DHCP server when provided
  - Logs IP assignment and lease loss to serial and telnet output
- **Unique MAC from SPI flash** - Reads the W25Q32JV 64-bit factory unique ID at boot and derives a locally-administered MAC address (`02:xx:xx:xx:xx:xx`)
  - Each board gets a deterministic, unique MAC without manual configuration
  - XOR-folds the 8-byte UID into 5 bytes, prepends `0x02` (locally administered, unicast)
- **`flash_id.rs` module** - New firmware module for reading flash unique ID via SPI master CSRs

### Changed
- **Gateware**: Added `with_master=True` to LiteSPI instantiation (explicit raw SPI command support)
- **Network init**: Interface starts with unspecified IP and routing table; DHCP configures both
- **Context**: Replaced `IpMacData` with plain `mac: [u8; 6]` (IP is now dynamic)
- Removed unused `IpData`, `IpMacData` types from `ethernet.rs`

---

## [0.2.4] - 2026-01-25

### Added
- **Bitmap UDP protocol** - Send RGB images to the panel over UDP port 7000
  - 10-byte little-endian header: magic "BM", frame_id, chunk_index, total_chunks, width, height
  - Pixel-aligned chunking (487 pixels / 1461 bytes per chunk, 10 chunks for 96x48)
  - Bitmask-based frame assembly with automatic buffer swap on completion
  - 16KB receive buffer with drain loop for reliable multi-packet reception
- **`bitmap_status` telnet command** - Shows packet counters, frame completion stats, and last packet details
- **`debug` telnet command** - Toggle live per-packet logging to telnet console
- **`tools/send_image.py`** - Send any image file (resized to panel dimensions) via UDP
- **`tools/send_test_pattern.py`** - Generate and send test patterns (gradient, bars, rainbow) without needing an image file

---

## [0.2.3] - 2026-01-25

### Added
- **Telnet IAC filtering** - State machine parser strips telnet negotiation sequences from input, preventing binary option bytes from corrupting the menu
- **`quit` command** - Closes the telnet connection cleanly
- **Prompt on connect** - Welcome message now includes `> ` prompt so users can type immediately

---

## [0.2.2] - 2026-01-25

### Added
- **Framebuffer double buffering** - New `fb_base` CSR register in HUB75 gateware allows software-controlled framebuffer base address
  - Two 256KB framebuffer regions in SDRAM (front and back)
  - CPU writes to back buffer, then flips `fb_base` to swap atomically
  - Eliminates tearing from CPU/DMA contention during animation
- `swap_buffers()` method on Hub75 driver

### Changed
- Animation tick restored to 30fps (33ms) now that double buffering prevents tearing
- All image-writing paths (patterns, default image, SPI image) now use write-then-swap

---

## [0.2.1] - 2026-01-25

### Added
- **Animated rainbow pattern** - `pattern rainbow_anim` telnet command displays a smoothly scrolling diagonal rainbow
- `Animation` enum in Context for tracking active animation state
- `animated_rainbow()` pattern generator with phase offset
- `animation_tick()` method called from main loop

---

## [0.2.0] - 2026-01-25

### Added
- **General-purpose panel configuration** - Support for multiple panel types via `--panel` argument
  - 96x48 (default), 128x64, 64x32, 64x64 panels supported
  - Configurable scan rates (1/16, 1/24, 1/32)
- **build.sh enhancements**
  - `--panel` argument to select panel type at build time
  - Automatic test image generation with correct panel dimensions
- **gen_test_image.py** - New script to generate panel-specific test patterns
  - Horizontal lines at key rows (top, middle, bottom)
  - Vertical lines at evenly spaced columns (colored: RED, GREEN, BLUE, YELLOW, MAGENTA)
  - Diagonal X pattern (CYAN and MAGENTA) for visual alignment verification

### Changed
- hub75.py now accepts `columns`, `rows`, `scan` parameters instead of hardcoded values
- colorlight.py uses PANELS configuration dictionary for panel definitions
- Row addressing properly wraps using Migen conditional logic instead of Python modulo

### Fixed
- Row wrap-around calculation using proper Migen signal expressions

---

## [0.1.0] - 2025-01-25

### Added
- Initial working release with telnet support
- LiteX SoC with VexRiscv CPU at 40MHz
- Standard LiteEth MAC with smoltcp TCP/IP stack in firmware
- Telnet management console on port 23
- ICMP ping support
- Art-Net UDP receiver (palette updates)
- HUB75 display driver supporting 8 outputs, 4 panels per chain
- Full-color (24-bit) and indexed (8-bit) display modes
- SPI flash image storage
- Docker-based build environment
- Support for Colorlight 5A-75E rev 8.2

### Fixed
- TCP socket state machine - changed `&` to `&&` in listen/bind conditions
- Timing in main loop - added proper delay for smoltcp timestamp handling
- MAC address consistency - use fixed MAC matching hardware default
- PAC imports for new svd2rust structure

### Changed
- Switched from custom SmolEth to standard LiteEth for better compatibility
- Reorganized repository structure, moved old projects to `legacy/`

### Known Issues
- Flash boot requires TFTP on rev 8.2 (flash chip definition mismatch)
- Art-Net direct pixel writes disabled pending testing

---

## Version History

| Version | Date | Description |
|---------|------|-------------|
| 1.10.3 | 2026-02-09 | Fix MAC overflow: ISR batch limit, filter multicast/unwanted UDP |
| 1.10.2 | 2026-02-09 | Fix bitmap packets going through smoltcp |
| 1.10.1 | 2026-02-09 | Fix RGB color order, professional HTTP dashboard |
| 1.10.0 | 2026-02-09 | Fully interrupt-driven network stack |
| 1.9.2 | 2026-02-08 | CPU variant "lite" for interrupt support |
| 1.9.1 | 2026-02-08 | Test pattern timing fix, include bitstreams |
| 1.9.0 | 2026-02-08 | Pre-clock-domain checkpoint (no functional changes) |
| 1.8.2 | 2026-01-28 | Boot grace period: 10s delay before streaming can block network init |
| 1.8.1 | 2026-01-28 | Version display in grid test pattern |
| 1.8.0 | 2026-01-27 | Skip slow path during streaming, auto chunk-delay, zero MAC overflows |
| 1.7.0 | 2026-01-27 | Fix color channels, bitmap dimension validation, streaming improvements |
| 1.6.0 | 2026-01-27 | Panel chaining: 2 panels per output, up to 12 total |
| 1.5.0 | 2026-01-27 | Expand HUB75 outputs 4→6, JTAG retry logic |
| 1.4.1 | 2026-01-27 | Fix HTTP server stuck sockets with idle timeout |
| 1.4.0 | 2026-01-26 | Parameterize HUB75 outputs (8→4), nrxslots 4→8 |
| 1.3.1 | 2026-01-26 | Pin LiteX to 2025.12, reproducible Docker builds |
| 1.3.0 | 2026-01-26 | BIOS broadcast TFTP on custom port 6969 |
| 1.2.0 | 2026-01-26 | Dynamic TFTP server via DHCP siaddr/Option 66 |
| 1.1.0 | 2026-01-26 | Web reboot button, modern dark theme, build.sh TFTP fix |
| 1.0.0 | 2026-01-26 | First stable release: multi-panel build, auto TFTP, repo cleanup |
| 0.2.9 | 2026-01-25 | Video streaming, 4 RX slots, fast bitmap receive |
| 0.2.8 | 2026-01-25 | Web pattern selector, JTAG pinout docs |
| 0.2.7 | 2026-01-25 | TFTP boot config, persistent flash, YAML layout |
| 0.2.6 | 2026-01-25 | HTTP REST API with dual-socket refresh fix |
| 0.2.5 | 2026-01-25 | DHCP client with unique MAC from SPI flash |
| 0.2.4 | 2026-01-25 | Bitmap UDP protocol for sending images |
| 0.2.3 | 2026-01-25 | Telnet IAC filtering and quit command |
| 0.2.2 | 2026-01-25 | Framebuffer double buffering via fb_base CSR |
| 0.2.1 | 2026-01-25 | Animated rainbow pattern |
| 0.2.0 | 2026-01-25 | General-purpose panel configuration |
| 0.1.0 | 2025-01-25 | Initial release with telnet support |

---

## Release Process

1. Update version in this file
2. Update version badge in README.md if applicable
3. Commit changes: `git commit -m "Release vX.Y.Z"`
4. Tag release: `git tag -a vX.Y.Z -m "Version X.Y.Z"`
5. Push: `git push && git push --tags`
