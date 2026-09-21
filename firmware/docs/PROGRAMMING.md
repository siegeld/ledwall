# Programmer's guide

How to write a program that runs **on** a Colorlight panel — composing its own
pixels from named values, with no frame ever sent to it. Like ESPHome, for LED
signs.

New here? Start with **[GETTING-STARTED.md](GETTING-STARTED.md)**, which gets a
panel lit and running a first program in under an hour. This page is the full
reference.

| | |
|---|---|
| **Concepts** | [The idea](#1-the-idea-in-one-picture) · [Quick start](#2-quick-start) · [A complete program](#3-a-complete-program) |
| **Reference** | [Widgets](#4-widget-reference) · [Lambdas](#5-lambdas) · [Colour](#6-colour-ramps-and-the-rainbow) · [Primitives](#7-helpers-and-primitives) · [API appendix](#appendix-a--complete-api-reference) |
| **Operations** | [Modes and boot](#8-modes-boot-and-the-data-plane) · [Troubleshooting](#10-troubleshooting) · [HTTP](#11-panel-http-reference) |
| **Going fast** | [Performance](#9-performance--the-rules-that-actually-matter) · [BENCHMARKS.md](BENCHMARKS.md) |

The design reasoning — why compiled and not interpreted, why coverage sprites —
is in [DISPLAY-PROGRAMS.md](DISPLAY-PROGRAMS.md). The machine it runs on is in
[HARDWARE.md](HARDWARE.md). Read those when you want to know *why*; read this
when you want to build something.

---

## 1. The idea in one picture

```
  STREAMING (the old way)          ON-PANEL PROGRAM (this)
  ───────────────────────          ───────────────────────
  marquee renders 128x128          marquee sends  temp=64
  → 24 Mbit/s of pixels            → ~20 bytes/s of values
  → panel displays frames          → panel draws it itself
  → host dies: panel BLACK         → host dies: panel keeps showing
                                      the last values, marked stale
```

A panel is in exactly one of these modes. Both write the framebuffer, so they
can never share a panel — see §8.

**What you write** is YAML. `panelc` compiles it to Rust, that is linked into
the firmware, and the panel runs native code. Nothing is parsed on the panel:
an interpreter was measured at ~0.3 fps, so this is a compiler, not a VM.

---

## 2. Quick start

```bash
cd /share/src/colorlight

# 1. write a program
$EDITOR panels/mysign.yaml

# 2. compile YAML -> Rust  (regenerates src/assets.rs and src/generated.rs)
python3 tools/panelc.py panels/*.yaml

# 3. build the firmware
./build.sh firmware

# 4. load it (dev: SRAM over JTAG; the panel re-fetches firmware over TFTP)
cp .tftp/boot.bin /srv/docker/marquee/data/firmware/boot.bin
./build.sh --panel 128x64 --cable ft2232_b sram
```

Steps 2–4 take about **10 seconds** end to end. That is the whole iteration
loop; there is no OTA step and no separate program upload, because loading a
program and loading firmware are the same operation.

Then pick it, either from marquee's Panels page (mode → `program`, program →
`mysign`) or directly:

```bash
curl -X POST http://<panel>/api/program/on -d '{"name":"mysign"}'
```

The marquee route persists — the panel reads it from its TFTP config at boot,
so it survives a power cycle. The `curl` route does not.

> **`--cable ft2232_b`, not `ft2232`.** On the bench panel the JTAG is on
> channel B. The wrong channel does not report a bad cable: the adapter opens,
> the frequency negotiates, and the chain scans `empty`, which is
> indistinguishable from an unpowered board.

### `sram` iterates. `flash-all` deploys.

**`sram` is volatile.** It loads the bitstream over JTAG and the tool says so:
*"This is temporary — configuration will be lost on power cycle."* It is the
right thing for the edit-compile-look loop above, and the wrong thing to walk
away from.

To make a panel start on its own, write both the bitstream AND the firmware to
SPI flash:

```bash
./build.sh --panel 128x64 --cable ft2232_b flash-all
```

Skip this and the panel comes back on whatever gateware was in flash *before*
you started — quite possibly a build old enough that today's firmware crashes on
it, because the CSRs it expects are not there.

**That failure is easy to misread.** A *netboot* looks like this in marquee's
TFTP log:

```
Opening boot.bin      Transferred 292976 bytes    <- firmware
Opening <mac>.yml     Transferred    349 bytes    <- config, with mode: program
```

If you see the **first line and not the second**, the firmware started and died
before it could ask for its configuration — the gateware/firmware mismatch above,
not a network or config problem.

**A cold power-on normally shows only the second line.** The BIOS tries the
network first and gives up in 0.32 s when the link is not yet negotiated, so the
firmware comes out of flash and only the config is fetched. That is healthy, and
it is the difference between `deploy` (changes what runs now) and
`flash-firmware` (changes what survives a power cut).

Expect about **6 seconds** from power to the panel appearing. Do not conclude
anything from the first second or two — and when you time it, time from the
moment the plug goes in, not from the last ping reply.

---

## 3. A complete program

```yaml
program: mysign                      # registry name; what you select
display: {width: 128, height: 128}

assets:                              # rasterised at BUILD time
  fonts:
    - {name: big,   file: assets/fonts/DejaVuSans.ttf, size: 30}
    - {name: small, file: assets/fonts/DejaVuSans.ttf, size: 13}
  icons:
    - {name: sun, file: assets/icons/sun.svg, size: 22}

values: [temp, cond]                 # keys marquee pushes

widgets:                             # drawn in order; FIRST match wins
  - border: {color: slate}
  - text:  {at: [6, 4], font: small, color: slate, value: "OUTSIDE"}
  - icon:  {at: [98, 6], name: sun, color: brass, when: "cond != 'rainy'"}
  - value: {at: [center, 34], font: big, color: white, key: temp}
  - lambda: |
      // raw Rust, compiled in, evaluated per pixel
      if y == ctx.h - 1 { return Some(BRASS); }
      None
```

### Widget order is z-order — and it is the wrong way round from CSS

Widgets are tested in declaration order and the **first one that covers a pixel
wins**. So **earlier is on top**, and a background belongs **last**. Get this
backwards and a full-width wash silently paints over your text — which is
exactly what happened the first time, and on a two-panel stack it rendered as a
hard brightness step at the seam that looked like a hardware fault.

---

## 4. Widget reference

| Widget | Fields | Notes |
|---|---|---|
| `border` | `color` | 1px ring, the full canvas edge |
| `text` | `at`, `font`, `color`, `value` | static string |
| `value` | `at`, `font`, `color`, `key`, `prefix`, `suffix` | a pushed value; `—` when absent |
| `icon` | `at`, `name`, `color`, `when` | a build-time-rasterised SVG |
| `lambda` | (block scalar) | raw Rust, spliced in at this position |

**`at`** is `[x, y]`, the top-left of the text box. `x` may be `center`, which
centres on the string's own advance width.

**`color`** is one of `white`, `slate`, `brass` (16-entry ramps, for
anti-aliased content) or `bg`, `alert` (flat). See §6.

**`when`** compiles to a plain comparison against the value table:
`"cond == 'rainy'"` or `"cond != 'rainy'"`. It is accepted on **`text`,
`value` and `icon`** — not on `border`, and a `lambda` tests its own conditions
in code. It is how one program draws several pages: gate each page's widgets on
a pushed `page` value rather than cycling on `ctx.frame` (see §9).

**`prefix` / `suffix`** wrap a `value` without a second widget and without the
host having to send the units:

```yaml
  - value: {at: [6, 80], font: big, color: brass, key: temp, suffix: "°"}
  - value: {at: [8, 106], font: small, color: slate, key: zones, suffix: " ZONES"}
```

Put the unit in a label rather than a suffix when the number can get long: the
power feed sends two decimals, and `58.61 kW` at a size worth reading overflows
128px, so that panel's header says `POWER kW` and the value stands alone.

---

## 5. Lambdas

A `lambda:` is raw Rust spliced in **at its position in the widget order**, so
your code composes with declarative widgets rather than sitting beside them —
it can draw under some things and over others.

It returns `Option<u8>`: `Some(index)` paints, `None` falls through to the next
widget.

In scope: `ctx`, `x`, `y`, every colour and ramp constant, every asset
(`FONT_BIG`, `ICON_SUN`, …), and the helpers in §7.

```yaml
  - lambda: |
      // a bar gauge, clamped, from the brass ramp
      let bar_y = ctx.h - 14;
      if y >= bar_y && y < bar_y + 4 && x >= 6 && x < ctx.w - 6 {
          let n = ctx.int("lights").unwrap_or(0).clamp(0, 30) as usize;
          let span = ctx.w - 12;
          return Some(if x - 6 < span * n / 30 { BRASS } else { RAMP_BRASS + 2 });
      }
      None
```

### Two phases: `row:` and `px:`

The form above runs **per pixel**, 16384 times a frame. Most of what a lambda
needs to know is constant across a row — which band `y` is in, what a value
says, how wide a string is — and `panelc` hoists exactly that for the
declarative widgets. A lambda is code it cannot analyse, so you hoist it
yourself:

```yaml
  - lambda:
      row: |
        // once per ROW. Work out what this row is, and read your values here.
        let band   = y >= 84 && y < 119;
        let hist: &[u8] = if band { ctx.val("hist").as_bytes() } else { &[] };
      px: |
        // once per PIXEL. Cheap tests only, using what `row:` worked out.
        if band && !hist.is_empty() {
            let col = (x * hist.len()) / ctx.w;
            return Some(RAMP_BRASS + 15);
        }
        None
```

**This is not a style preference, it is the difference between programs that
run and programs that do not.** `ctx.val()` costs ~79 cycles; calling it per
pixel to answer a question that is constant across the row made the trace and
page-dots lambda the most expensive thing on the `dashboard-home` panel — rows
72–117 cost 3x the rest — *after* the declarative widgets had already been
fixed. Anything that walks a string (`FONT.width(s)`) belongs in `row:` for the
same reason.

Per-row locals generated for declared values are named `v_<key>`, so a `row:`
block can test `v_page == "2"` without a second lookup.

A plain `lambda: |` block is shorthand for `px:` only, and is right when there
genuinely is nothing to hoist.

### `Ctx`

| Call | Returns |
|---|---|
| `ctx.w`, `ctx.h` | canvas size |
| `ctx.frame` | frames since the program started — your animation clock |
| `ctx.time_ms` | panel uptime |
| `ctx.val("k")` | value as `&str`, or `"-"` if it never arrived |
| **`ctx.int("k")`** | **integer — use this in per-pixel code** |
| `ctx.num("k")` | `f32` — **soft float, see §9** |
| `ctx.stale_ms()` | ms since *any* value arrived |

Always show staleness somehow. A sign that lies is worse than a blank one, and
unlike a streamed panel you *can*: the pixels are already on the panel when the
feed dies.

---

## 6. Colour: ramps and the rainbow

The palette is not arbitrary RGB — a program returns **palette indices**.

| Range | What |
|---|---|
| `BG`, `SLATE`, `BRASS`, `ALERT` (0–3) | flat colours |
| `RAMP_WHITE`, `RAMP_BRASS`, `RAMP_SLATE` (16/32/48) | 16 entries each, background → colour |
| `RAINBOW` (64–191) | 128 entries of full-saturation hue |

**Ramps are what make anti-aliasing free.** A sprite's 4-bit coverage *is* the
offset into a ramp:

```rust
return Some(RAMP_WHITE + coverage);   // one add. no blend, no multiply
```

And recolouring anything rewrites **16 palette words** while the framebuffer
never moves — the same cost at nine panels as at one.

A ramp gives one colour plus alpha, which is right for text and UI icons and
useless for vivid colour. For that, index the rainbow:

```rust
let hue = |v: usize| Some(RAINBOW + (v % RAINBOW_LEN as usize) as u8);
return hue(x + y + ctx.frame as usize * 3);
```

---

## 7. Helpers and primitives

`prim.rs` is the drawing library — use it rather than re-deriving the maths.
Every primitive is a **query** ("what, if anything, goes at this pixel?"),
returns `Option<u8>`, and takes its bounding box first so a pixel outside costs
one comparison.

| Primitive | Draws |
|---|---|
| `border`, `border_chase` | the outer ring, optionally colour-cycling |
| `rect`, `bar` | a filled box; a gauge with a track |
| `text`, `text_centered` | static text through a ramp |
| `scroll_text`, `scroll_text_rev` | a ticker line, either direction |
| `scroll_message`, `scroll_message_rev` | **a ticker built from live values** |
| `sprite`, `sprite_rotated` | an icon, optionally spinning |
| `plasma`, `sparkle`, `sweep` | full-field effects |
| `stale_marker` | say the feed died rather than lying |
| `hue`, `shade` | rainbow index; ramp entry from coverage |

Cost, worst to best — this is the thing to know before composing a screen:

| | Per pixel |
|---|---|
| `text` / `scroll_text` | ~4 reads (ascii table, glyph, sprite byte) |
| `sprite` / `sprite_rotated` | 1 read |
| `bar`, `border`, `sparkle`, `plasma` | arithmetic only, no memory |

A read costs the same ~190 cycles as a write here, so the text primitives are
the expensive ones — which is exactly why `scroll:` exists.

### Lower-level helpers

| Helper | Use |
|---|---|
| `hash2(x, y)` | stable per-pixel pseudo-random (sparkles). Multiply-free |
| `sin64(a)`, `cos64(a)` | 64-step sine, scaled by 256 |
| `unrotate(x, y, cx, cy, a)` | inverse-rotate a pixel into source space |
| `FONT.cover_at(s, x, y, ox, oy)` | coverage of static text |
| `FONT.cover_scroll(s, vx, y, oy)` | **scrolling** text, wraps, O(1) |
| `SPRITE.cover_at(x, y, ox, oy)` | coverage of an icon |

**Rotation is inverse.** A program is asked *what belongs at this pixel*, so it
maps the destination back into source space and samples. Rotating forward would
need a buffer to scatter into and would leave holes.

**Scrolling text needs a monospace face.** `cover_scroll` finds the character
under a pixel with one divide — but only if the advance is fixed. In a
proportional font it would have to walk the run: ~40 glyph lookups per pixel for
a 40-character ticker, 16384 pixels a frame. Declare a mono font:

```yaml
  fonts:
    - {name: tick, file: assets/fonts/DejaVuSansMono-Bold.ttf, size: 26}
```

Size 26 gives advance 16 — a power of two, so the divide is a shift.

> **Take the advance from the GENERATED font, not from the TTF.** `panelc`
> rasterises at the exact size and rounds, and the two do not always agree:
> `DejaVuSansMono-Bold` at 11 measures 6 on the outline and comes out **7** in
> `src/assets.rs`. A layout budgeted from the outline put a clock 3px off the
> right edge and butted two columns together. `grep fixed_advance
> src/assets.rs` is the number that counts.

### A scroll BAND cannot scroll one column

`scroll:` is the cheap path and it is worth reaching for: it shifts what is on
the glass and asks the program only about the columns that exposed — about two
memory accesses a pixel against the five a re-render costs.

But a band is `(y0, y1)`. It is **full width**, and it moves everything in those
rows. A row holding a static time, a scrolling destination and a static
countdown cannot use it: the shift would drag the static columns along with the
moving one.

Such a column has to be drawn in a lambda instead, from `ctx`, with those rows
in `display.dynamic` so they redraw every frame. That is the expensive path, and
it is worth knowing what it costs before choosing the layout. Measured on the
bench card with `railboard`, a four-row board at 128x128:

| | fps |
|---|---|
| static, `dynamic` covering two empty rows | **30.3** — the frame-period cap |
| one scrolling column, 86 of 128 rows dynamic | **7.9** |

So a scrolled sub-row column costs roughly **4x**. A ticker that can own its
whole row should use `scroll:` and pay nothing like that.

### Drive a scroll from `time_ms`, not `ctx.frame`

`ctx.frame` is the obvious clock for an animation and it is a trap for anything
whose motion is expensive. The scrolling is what drags the frame rate down, so a
per-frame step makes the speed a function of how much work the speed is causing:
`railboard` stepping one pixel every two frames crawled at ~6 px/s once the
scroll had taken the card from 30 fps to 13, and a padded name took half a
minute to come round — the column read as *empty* for most of the page it was
on.

`ctx.time_ms` is uptime in milliseconds and independent of all that. `time_ms /
40` is 25 px/s whatever the card is managing, and a divide by a constant is
strength-reduced.

### Padding a scrolled message

`cover_scroll` wraps the run, so a message needs trailing space or it runs into
itself with no gap. Padding to a power-of-two length lets the wrap be a mask
instead of a division, which is why the demos pad to 64 — but check what the
padding costs first. A station name padded that way is mostly gap:
`North White Plains` is 18 characters and rounds to 32, dragging 112px of blank
past a 56px window. Three spaces and a division read far better than a mask and
a blank column.

---

## 8. Modes, boot and the data plane

### Selecting a program so it survives a reboot

The panel's per-MAC TFTP config carries the mode. marquee appends it from the
database, so the Panels page is the source of truth:

```yaml
grid: 1x2
panel_width: 128
panel_height: 64
J1: 0,0 0,0
J2: 0,1 0,1
mode: program
program: mysign
```

If the named program is not in the firmware, the panel **falls back to
streaming** rather than showing nothing. A panel dark after a config typo is far
worse than one showing the wrong thing.

### Getting values in

marquee pushes them. It already holds one panel-bus subscription for the whole
fleet — one integration, one set of credentials — and the panel has no TLS and
no business holding a Home Assistant token.

```bash
curl -X POST http://<panel>/api/values \
  -H 'Content-Type: application/json' \
  -d '{"temp":"64","cond":"clear-night"}'
```

> **The table holds 24 keys, and the 25th is dropped in SILENCE.** `set()`
> ignores a key once the table is full rather than evicting one, so the key
> that vanishes is whichever the pusher happened to send last — and nothing
> raises, nothing logs, and the panel shows one blank field with no
> explanation. `dashboard-home` lost its power trace to exactly this when the
> limit was 16. Keys are capped at 16 bytes and values at 64.
>
> A dashboard that needs more than 24 wants **several programs**, not a bigger
> push. If a host feeds more than one program, it must send the set belonging
> to the program that panel is actually running rather than merging them.

The pusher (`services/player/valuepush.py` in marquee) maps bus fields to keys,
asks each panel what mode it is in, and pushes only to program-mode panels. Add
a key by adding a line to its `MAPPING`.

### A panel with no network at all

Everything a program needs is already in the firmware — the code, the fonts, the
sprites. What came only from the network was the single line *selecting* it, plus
the panel-to-connector map. Bake those in and the panel needs nothing:

```bash
./build.sh --default-config configs/standalone-128x128.yml firmware
./build.sh --panel 128x64 --cable <cable> flash-all
```

The config file is **the same format the panel fetches over TFTP**, parsed at
boot by the same parser — so a config moves between the fleet server and the
firmware without being rewritten, and a standalone panel is not a different kind
of thing from a managed one:

```yaml
grid: 1x2
panel_width: 128
panel_height: 64
J1: 0,0 0,0
J2: 0,1 0,1
mode: program          # without these two lines the panel expects a stream,
program: timessquare   # and with no network there is nothing to stream it
```

**A TFTP config still wins when one arrives.** The baked config is what the panel
shows until then, and what it falls back to when there is no network to ask. That
ordering is deliberate: baking a default must never stop you changing a panel
remotely.

It is applied *before* the network comes up, so even a fleet panel draws its own
content within a second of power-on rather than after DHCP has timed out.

> ### An offline program must be authored for having no data
>
> With no network there is no Home Assistant, so `ctx.val()` returns `"-"` for
> every key and `stale_ms()` only grows. A program meant to run standalone
> either takes **no values at all**, or is written so that missing data looks
> *deliberate* rather than broken — which is what `stale_marker` is for.
>
> Test it the way it will run: clear the panel's layout on the fleet server, or
> unplug it, and look at what you actually get.

### Streaming and programs never share a panel

Both write the framebuffer. If both run you get corruption, not one winning.
Three things enforce this:

1. marquee does not start a player for a program-mode panel.
2. Entering program mode **holds** the stream.
3. Hold stops the CPU path **and the gateware pixel DMA** — see §10.

---

## 9. Performance — the rules that actually matter

The panel is a 40 MHz VexRiscv with **no D-cache** and **no FPU**. A full-screen
redraw is 16384 pixels; your per-pixel code runs 16384 times per frame.

**The ceiling is ~12.5 fps for a full-screen redraw, whatever you draw.** That
is measured with the `blank` program, which returns the background and nothing
else. It is the store path — ~390 cycles per pixel into SDRAM — not your code.
Keep `blank` in mind as the yardstick: the gap between it and your program is
what your drawing costs.

> Earlier versions of this page said **6 fps**. That was measured while the
> panel was *also* being streamed to, so most of its time went to parsing
> packets it would never draw. Same program, stream off: 12.5 fps. Every
> "ceiling" recorded before that was found is suspect for the same reason.

```
blank        12.5 fps     draws nothing — the ceiling
timessquare   4.7 fps     tickers, spinning star, plasma, glitter — every pixel moves
dashboard      8.5 fps     a dashboard, using `dynamic` to skip 110 of 128 rows
```

(Those numbers are with the panel in program mode. A panel that is ALSO being
streamed to spends much of its time parsing packets it will not draw — `blank`
measured 6 fps under a stream and 12.5 without, so marquee staying off a
program-mode panel is itself worth 2x.)

Rules, in the order they bite:

1. **Never call `ctx.num()` per pixel.** No FPU: it parses and computes in soft
   float. One gauge doing this held a panel under 0.2 fps — which reads as
   *hung*, not slow, and sends you hunting for an infinite loop. Use
   `ctx.int()`.
2. **Guard by bounding box first.** Check `y` is in your band before doing
   anything else. `panelc` does this for `text`, `value` and `icon`
   automatically; in a lambda it is on you. A centred value that computes
   `width()` before its band check pays that walk for *every pixel on the
   panel*.
3. **O(1) per pixel.** No loops over strings, no work whose cost grows with
   content.
4. **Integers only.** Constant multiplies and divides are strength-reduced by
   the compiler and are fine; variable ones are real instructions — the CPU
   *does* have the M extension (see §10), so they are single instructions, not
   software routines, but they are not free.
5. **Animate through the palette when you can.** Palette cycling is 256 words
   per frame *regardless of wall size*. Nine panels animate for the cost of one.
   Nothing else in this system has that shape.

### Redraw only what changes — `display.dynamic`

A framebuffer store costs **~190 cycles** on this core: no D-cache, no write
buffer, a full bus round trip per word. 16384 of them is ~3.1M cycles, which is
why `blank` — a program that draws *nothing* — still caps near 12 fps. **Not
writing a word is the only real speedup available.**

So declare the rows that actually change every frame:

```yaml
display: {width: 128, height: 128, dynamic: [110, 128]}
```

Everything outside that band is redrawn only when it *can* have changed. On the
`dashboard` dashboard, whose top two-thirds only move when a value arrives, that
is **1.2 fps → 8.5 fps**.

Two details that matter:

- **The renderer owes TWO full frames, not one**, because the framebuffer is
  double-buffered — static content has to reach *both* buffers before partial
  frames are safe. They are owed on program activation and again whenever a
  value lands, since a value can change any row and only the program knows where
  it drew it.
- **It does nothing for a full-screen effect.** `timessquare` animates every
  pixel, so it has no static rows to skip and stays at ~4.7 fps. Declaring a band
  it does not respect would just corrupt the picture.

### Palette cycling — animate without touching pixels

A `palette:` block runs once per frame and rewrites the palette. 256 words
against 16384 for a redraw, and — unlike pixels — **the cost does not grow with
the wall**. Nine panels recolour for what one costs.

```yaml
palette: |
  let f = ctx.frame as usize;
  match i {
      0 => rgb(0, 0, 0),
      1..=15 => { /* rotate hue through the ramp, scaled by coverage */ }
      _ => rgb(0, 0, 0),
  }
```

Because a sprite's coverage nibble indexes the ramp, rotating those 16 entries
makes colour flow **along the letters** with the framebuffer untouched. That is
how the demoscene canon works — plasma, fire, fades, colour cycling — and it is
the only motion here that is free at any wall size.

### `scroll:` — shift the glass, fill what it uncovers

Don't re-render a ticker every frame. Declare it and the renderer **shifts what
is already displayed** and calls your program back for only the columns that
exposed:

```yaml
display: {width: 128, height: 128, dynamic: [52, 74]}
scroll:  {band: [52, 74], speed: 1}
```

**Several tapes at once.** `scroll:` also takes a *list*, so independent bands
can run at different speeds and in opposite directions — the Times Square look:

```yaml
scroll:
  - {band: [52, 74], speed: 2, dir: left}
  - {band: [80, 98], speed: 1, dir: right}
```

| Field | Default | Meaning |
|---|---|---|
| `band` | — | `[y0, y1)` — rows this tape occupies |
| `speed` | `1` | pixels shifted per frame |
| `dir` | `left` | `left` or `right` |

`display.dynamic` must still cover **every** scrolled band, plus any other rows
that change. Bands must not overlap: they are shifted independently, and an
overlap means one band drags the other's pixels.

```
re-rendering the band   10.4 fps
shift-and-fill          24.8 fps      2.4x
```

That ratio is not luck — it is the memory-access count. A re-render costs ~5
accesses per pixel (ascii table → glyph → sprite byte → write); a shift costs
**2** (one read, one write), and the expensive glyph lookups shrink to the
handful of columns actually uncovered.

**The fill callback is your own program.** `RowFn` takes an x origin, so the
renderer calls your per-pixel code for just the new strip — you write one
drawing function and it serves both the full redraw and the scroll.

**You can still have a background.** A shift moves pixels *along a row*, so
anything **constant in x** is untouched by it — a vertical gradient, stripes, a
solid band. And because those are palette *indices*, the `palette:` hook
animates them for **zero pixel writes**: a moving background behind scrolled
text, which a per-pixel composite could never afford. `timessquare` does exactly
this — its tapes sit on a breathing gradient.

What does *not* work is an independently animating background inside a scrolled
band (a plasma, a starfield): the shift would drag it along with the letters.
Either keep it out of the band, or make it translate at the same speed as the
text — then the band moves as one and the shift is still exact.

Two conditions, both load-bearing:

- **Content must be a pure function of `x + frame * speed`.** Then last frame's
  pixel at `x+speed` *is* this frame's pixel at `x`, so the shift is exact
  rather than an approximation. A ticker qualifies; a ticker with a blinking
  cursor does not.
- **It copies front → back, never shifts the back buffer.** The back buffer
  holds the frame from two swaps ago, not what is on the glass; shifting that
  scrolls the wrong image.

### Text is READ-bound, not write-bound

`cover_scroll` does several memory lookups per pixel — ascii table → glyph
struct → sprite byte — and with no D-cache a **read costs the same ~190 cycles
as a write**. So a text band hits its limit on reads long before its writes
matter:

```
ticker, 40-row band    8.2 fps
ticker, 18-row band   10.4 fps     <- halving the WRITES bought only 27%
```

Shrinking the band helps, but not proportionally, because the reads did not
move. The consequence for scrolling: **step size is smoothness, frame rate is
the budget.** At ~10 fps, 2px/frame is ~21px/s and about as smooth as text gets
here. Genuinely silky scrolling needs a row-level primitive that hoists the
glyph lookups out of the pixel loop, or the gateware blitter.

### Cheaper still: don't touch pixels at all

| Technique | Words written per frame | Scales with wall size? |
|---|---|---|
| Full redraw | 16384 | yes |
| `dynamic` band (a ticker line) | ~2500 | yes |
| **`scroll:` shift-and-fill** | ~2800 read+write, but ~2% of the glyph lookups | yes |
| **Palette cycling** | **256** | **no** |
| **`fb_base` scroll / page flip** | **1** | **no** |

Palette cycling animates the *whole wall* for 256 words — nine panels cost the
same as one. The demoscene canon (plasma, fire, colour cycling, fades) works
this way with the framebuffer held still. That is the direction to go when a
`dynamic` band is not enough.

### If you need smooth motion

You will not get it from a faster program — the ceiling is the model. The
options are a cheaper *update path*: partial redraw (only the band that
changed), `fb_base` hardware scrolling (one CSR write, zero pixel writes,
vertical only), or the gateware blitter. See DISPLAY-PROGRAMS.md §3.

---

## 10. Troubleshooting

Each of these cost real time. The symptom is listed first because that is what
you will have.

**The panel shows a frozen or corrupt picture, and counters look fine.**
The pixel DMA is probably off. A panel reboots with DMA **off**; on the CPU
pixel path the receive ISR cannot drain the MAC FIFO, so ~3 chunks of every 12
are lost and *no frame ever completes* — the panel keeps showing whatever was on
it. Check `dma_enabled` in `/api/status`. marquee re-asserts it every tick now.

**A test pattern displays in completely wrong colours.**
The panel is in `indexed` mode while the pattern is full colour, so every word's
low byte is read as a palette index. Check `mode` in `/api/display`.

**The web UI will not load but the JSON endpoints work.**
That was an MTU bug (2048 — the MAC *slot* size — on a 1500-byte link), fixed in
v1.15.1. Any HTTP response larger than one frame sent only its tail.

**`frames_completed` is 0 but packets are arriving.**
Chunks are being lost. Look at `mac_overflow` and `last_missing` in
`/api/bitmap/stats`, and at `dma_enabled`.

**`bad_size` climbing.**
A show authored for a different wall. The panel refuses frames whose declared
geometry is not its own rather than drawing a garbled part-frame.

**"Held" but the picture still changes.**
Fixed in v1.15.1: hold used to stop only the CPU parser while the gateware DMA
kept writing. If you are on older firmware, that is why.

**You cannot tell what is driving the glass.**
`/api/display` reports `source`: `streaming`, `program (on-panel)`,
`held (native pattern)` or `idle`. The status page shows it too.

### The endpoint that ends arguments

```bash
curl http://<panel>/api/fb
```

Dumps the **front buffer** — what is actually being scanned out — sampled every
4 pixels, with the real output mode. Every other signal reports *intent*:
`/api/display` says the mode that was requested, `frames_completed` counts what
the CPU saw. When the glass disagrees with all of them, this is the only thing
left. It once showed a pixel-perfect grid pattern with `mode: indexed` — correct
pixels, wrong mode — which no amount of reasoning from the other counters would
have found.

---

## 11. Panel HTTP reference

| Method | Path | Does |
|---|---|---|
| GET | `/` | status page: source, program, values, controls |
| GET | `/api/status` | link, DMA, interrupt and display state |
| GET | `/api/display` | size, output mode, **source**, hold |
| GET | `/api/programs` | programs in this firmware, and which is running |
| GET | `/api/values` | the value table, with per-key age |
| POST | `/api/values` | push values (flat JSON object) |
| POST | `/api/program/on` | run a program — `{"name":"…"}` optional |
| POST | `/api/program/off` | back to streaming |
| POST | `/api/display/hold/on`\|`off` | stop/resume incoming frames |
| POST | `/api/display/pattern` | built-in pattern — `{"name":"grid"}` |
| POST | `/api/dma/on`\|`off` | gateware pixel DMA. **Off = CPU pixel path**, where frames stop completing |
| POST | `/api/display/on`\|`off` | blank or unblank the panel |
| GET | `/api/layout` | grid, panel size, connector assignments |
| GET | `/api/bitmap/stats` | per-frame receive counters |
| GET | `/api/fb` | front-buffer dump, every 4th pixel |
| POST | `/api/fbdump` | 16 rows of the framebuffer — `{"y0":"16"}` |
| GET | `/api/palette` | the live palette, and how many banks |
| GET | `/api/dmatest` | SDRAM DMA throughput probe |
| POST | `/api/reboot` | restart |

### Reading the framebuffer back

`/api/fbdump` is the way to see what is actually on the glass rather than what
you believe you sent — invaluable for checking a program without standing in
front of the panel. Two things will mislead you if you do not know them.

**`y0` is read as a STRING.** `{"y0": 16}` parses as absent and you get rows
0–15 again; `{"y0": "16"}` is what works. Eight identical-looking chunks are the
symptom — a capture that repeats the top of the screen down the whole image.

**It emits only the LOW BYTE of each framebuffer word**, and what that byte
*means* depends on the output mode `/api/display` reports:

| mode | the low byte is | reconstructable? |
|---|---|---|
| `indexed` | the palette index | **yes** — map it through `/api/palette` |
| `fullcolor` | one RGB channel | **no** — the other two bytes are not sent |

Running a colour channel through a palette produces confetti that looks exactly
like a corrupt framebuffer. The tell is that the **layout is still correct**:
genuine corruption does not respect a layout. A tool that captures colour
should refuse on a non-indexed panel rather than hand back a plausible lie.

`tools/grab_fb.py` does all of this:

```bash
tools/grab_fb.py <panel> --scale 4 -o panel.png   # indexed panels only
```

It refuses on a `fullcolor` panel, sends `y0` as a string, and reverses the
palette triplets. A program-mode panel is indexed and captures fine; a streamed
`format: rgb` show cannot be captured in colour at all, so preview it host-side
instead.

**The palette hex is little-endian.** `/api/palette` reports brass `#C9A84C` as
`4ca8c9`, so reverse each triplet before using it as RGB.

---

## 12. The programs that ship

| Name | What it is |
|---|---|
| `ticker` | A Times Square tape: monospace text scrolling over an 18-row band, coloured by a cycling palette. ~10 fps, 2px/frame |
| `dashboard` | House dashboard: outside temperature, condition icon, security bar, heartbeat. Reads pushed values |
| `timessquare` | Two scrolling tickers, a spinning star, plasma, glitter. Takes **no** data — unplug marquee and it keeps running |
| `blank` | Draws nothing. The performance yardstick — see §9 |

`blank` is not decoration. It is how you tell "my program is slow" from "the
machinery is slow", and it exists so the next person measures instead of
guessing.

---

# Appendix A — complete API reference

Everything a lambda can call. All of it is in scope automatically; `panelc`
emits `use crate::prim::*;` into every lambda.

**The universal shape**: every primitive is a **query** — "what, if anything,
goes at this pixel?" — returns `Option<u8>` (a palette index), and tests its
bounding box *first*, so a pixel outside costs one comparison.

## A.1 `Ctx` — what the program knows

```rust
pub struct Ctx { pub w: usize, pub h: usize, pub frame: u32, pub time_ms: i64, … }
```

| Member | Type | Notes |
|---|---|---|
| `ctx.w`, `ctx.h` | `usize` | canvas size in pixels |
| `ctx.frame` | `u32` | frames since the program started — **your animation clock** |
| `ctx.time_ms` | `i64` | panel uptime in ms |
| `ctx.val(k)` | `&str` | the value, or `"-"` if it never arrived |
| `ctx.int(k)` | `Option<i32>` | **use this in per-pixel code** |
| `ctx.num(k)` | `Option<f32>` | ⚠️ soft float — never per pixel |
| `ctx.stale_ms()` | `i64` | ms since *any* value arrived |

## A.2 Shapes and gauges

```rust
border(ctx, x, y, t: usize, color: u8)                  -> Option<u8>
border_chase(ctx, x, y, t: usize, speed: usize)         -> Option<u8>
rect(x, y, ox, oy, w, h, color: u8)                     -> Option<u8>
bar(x, y, ox, oy, w, h, value: i32, max: i32,
    fg: u8, track: u8)                                  -> Option<u8>
```

`border` uses distance-to-nearest-edge, so the ring is **one expression** rather
than four rectangles that have to agree at the corners — which is where
hand-written borders go wrong.

`bar` clamps and divides in **integers** on purpose. Feed it `ctx.int()`, never
`ctx.num()`.

## A.3 Text

```rust
text(font, s, x, y, ox, oy, ramp: u8)                       -> Option<u8>
text_centered(ctx, font, s, x, y, oy, ramp: u8)             -> Option<u8>
scroll_text(ctx, font, s, x, y, oy, speed, ramp: u8)        -> Option<u8>
scroll_text_rev(ctx, font, s, x, y, oy, speed, ramp: u8)    -> Option<u8>

scroll_message(ctx, font, build, x, y, oy, speed, ramp)     -> Option<u8>
scroll_message_rev(ctx, font, build, x, y, oy, speed, ramp) -> Option<u8>
```

`ox`/`oy` are the text origin; `x`/`y` are the pixel being asked about.

**The expensive primitives** — several dependent reads per pixel (ascii table →
glyph struct → sprite byte) — but they reject the whole vertical band in one
comparison, which is what keeps them affordable for the ~90% of a panel that is
nowhere near a given line.

`scroll_text*` need a **monospace** face, and the message wraps, so no second
copy is needed to cover the seam. `scroll_text_rev` adds a whole period before
subtracting, so the `usize` cannot underflow.

> **The band you declare in `display.dynamic` must cover every row these can
> ink**, not the rows they usually do. Rows outside it are written only on a full
> frame, so anything drawn there **stays as debris**.

### A tape that says something real — `scroll_message`

`scroll_text` takes a `&'static str`, which is all a fixed slogan needs. A tape
showing the temperature cannot use one: the string has to be *formatted*.

`build` is a `fn(&Ctx, &mut Msg)`, and it runs **only when a value has actually
landed** — typically once every several seconds, not once per frame. Every pixel
in between reads the cached text, for one extra `u32` comparison.

```yaml
  - lambda: |
      fn tape(ctx: &Ctx, m: &mut Msg) {
          use core::fmt::Write;
          let _ = write!(m, "*** OUTSIDE {:>3.3}F *** {:<7.7} *** ",
                         ctx.val("temp"), ctx.val("cond"));
          m.pad_to(48);
      }
      if y >= 6 && y < 40 {
          if let Some(c) = scroll_message(ctx, &FONT_TICK, tape, x, y, 8, 1, RAMP_WHITE) {
              return Some(c);
          }
          return Some(RAMP_SLATE + ((y - 6) * 15 / 34) as u8);
      }
```

> ### Give every field an explicit width **and** precision
>
> `{:>3.3}` and `{:<7.7}` pad *and* truncate, so the message is the **same
> length whatever arrives**. This matters more than it looks: `cover_scroll`
> wraps modulo `len * advance`, so a message that changes length changes the
> scroll period and the tape visibly jumps. `Msg::pad_to()` is the belt to that
> pair of braces.
>
> Because the rebuild is keyed on value arrivals, a length change can only
> happen on a frame where `program_tick` has already re-armed two full redraws —
> so it can never leave a seam in a shifted band. That is not luck; it is why
> the cache is keyed on `writes` and not on `frame`.

**`Msg` is a fixed 128-byte buffer** implementing `core::fmt::Write`. It
truncates rather than erroring — a ticker losing its tail is cosmetic, while a
`write!` that failed mid-render would have to be handled per pixel. There are
**four slots**, keyed automatically by the `build` function pointer, so two tapes
in one program get their own buffers.

> Non-ASCII will not work. `cover_scroll`'s lookup table covers **32..127 only**,
> and a multi-byte character also breaks the `len * advance` period arithmetic.
> A degree sign is the obvious thing to reach for and the obvious thing to
> get wrong.

## A.4 Sprites

```rust
sprite(sp, x, y, ox, oy, ramp: u8)                          -> Option<u8>
sprite_rotated(sp, x, y, cx, cy, angle: usize, ramp: u8)    -> Option<u8>
```

`angle` is `0..63`, not degrees.

**Rotation is inverse.** The program is asked what belongs at *this* pixel, so
it maps back into source space and samples. Rotating forward would need a buffer
to scatter into and would leave holes.

## A.5 Effects

```rust
plasma(ctx, x, y)                                      -> u8          // always covers
sparkle(ctx, x, y, one_in: u32, ramp: u8)              -> Option<u8>
sweep(ctx, x, y, oy, h, width, speed, color: u8)       -> Option<u8>
stale_marker(ctx, x, y, after_ms: i64, size, color)    -> Option<u8>
```

`plasma` is summed sines — table lookups and adds, **no multiply per pixel**.
The cheapest full-screen effect available.

`sparkle` derives each pixel's phase from `hash2(x, y)`, so twinkling is stable
per pixel and needs **no state**. Storing a phase would cost a
framebuffer-sized array; deriving it costs a few shifts. Higher `one_in` is
sparser.

`stale_marker` is the one you should not skip. See §8.

## A.6 Colour

```rust
hue(v: usize) -> u8                    // rainbow index from any changing quantity
shade(ramp_base: u8, coverage: u8) -> u8   // ramp entry from 4-bit coverage
```

| Constant | Value | What |
|---|---|---|
| `BG`, `SLATE`, `BRASS`, `ALERT` | 0–3 | flat colours |
| `RAMP_WHITE` | 16 | 16 entries, background → white |
| `RAMP_BRASS` | 32 | 16 entries |
| `RAMP_SLATE` | 48 | 16 entries |
| `RAINBOW` | 64 | **128** entries of full-saturation hue |
| `RAINBOW_LEN` | 128 | the length of that band |

`shade(RAMP_WHITE, coverage)` is **one add**. That is what makes anti-aliasing
free here — no blend, no multiply.

```rust
rgb(r: u8, g: u8, b: u8) -> u32        // for the `palette:` hook only
```

## A.7 Maths helpers

```rust
sin64(a: usize) -> i32                 // 64-step sine, scaled by 256
cos64(a: usize) -> i32
unrotate(x, y, cx: i32, cy: i32, a: usize) -> (i32, i32)
hash2(x: usize, y: usize) -> u32       // stable per-pixel noise, multiply-free
```

## A.8 Font and sprite internals

Reach for these when a primitive does not fit:

```rust
FONT.cover_at(s, x, y, ox, oy)   -> Option<u8>   // coverage of static text
FONT.cover_scroll(s, vx, y, oy)  -> Option<u8>   // scrolling text, wraps, O(1)
SPRITE.cover_at(x, y, ox, oy)    -> Option<u8>   // coverage of an icon
```

All return **4-bit coverage** (`0..15`), which you then pass through `shade()`.
Both `cover_at` and `cover_scroll` do two-sided band rejection internally
(`y < oy || y >= oy + line_height`) — a one-sided test lets a glyph ink above
its own line, which is exactly the debris bug in §9.

## A.9 The YAML schema

```yaml
program: <name>                      # required — the registry key
display:
  width: 128
  height: 128
  dynamic: [y0, y1]                  # optional — rows that change every frame
scroll:                              # optional — a dict, or a list of them
  - {band: [y0, y1], speed: 1, dir: left}
assets:
  fonts: [{name: big, file: <path>.ttf, size: 20}]
  icons: [{name: sun, file: <path>.svg, size: 24}]
values: [temp, cond]                 # keys this program reads
palette: |                           # optional — runs once per frame, `i` is 0..255
  <rust returning u32>
widgets:                             # FIRST MATCH WINS — earlier is ON TOP
  - border: {thickness: 2, color: brass}
  - text:   {at: [x, y], font: big, color: white, value: "HELLO"}
  - value:  {at: [center, y], font: big, color: white, key: temp}
  - icon:   {at: [x, y], name: sun, color: brass, when: "cond == 'sunny'"}
  - lambda: |
      <rust returning Option<u8>>
```

`panelc` emits, per program: `DYNAMIC_<NAME>`, `SCROLL_<NAME>`, optionally
`palette_<name>`, the render fn, and a registry entry.

---

# Appendix B — cheat sheet

**The three-step loop.** `panelc` runs on your machine, not the panel:

```bash
python3 tools/panelc.py panels/mine.yaml    # YAML -> Rust
./build.sh firmware                          # Rust -> binary
./build.sh --panel 128x64 --cable <c> sram   # binary -> board  (volatile!)
./build.sh --panel 128x64 --cable <c> flash-all   # ...and permanent
```

**The five rules, in the order they bite:**

1. **Never `ctx.num()` per pixel.** No FPU. One gauge doing it held a panel
   under 0.2 fps, which reads as *hung*, not slow.
2. **Guard by bounding box first**, before any other work.
3. **O(1) per pixel** — no loops over strings.
4. **Integers only.**
5. **Animate through the palette when you can** — 256 words a frame, flat in
   wall size.

**Widget order is z-order, and it is backwards from CSS: earlier is on top.**
If your background hides your text, the background is listed first.

**Not writing a word is the only real speedup available.** A store is ~190
cycles; a 128×128 redraw is ~3.1M cycles; that is the 12.5 fps ceiling, before
your code does anything.

| Want | Reach for |
|---|---|
| A dashboard, mostly static | `display.dynamic` — 1.2 → 8.5 fps |
| A ticker | `scroll:` — 10.4 → 24.8 fps |
| Colour movement anywhere | `palette:` — free, and flat in wall size |
| Smooth motion beyond that | not a faster program; a cheaper update path (§9) |

**When the glass disagrees with every counter**: `curl http://<panel>/api/fb`.
It dumps the front buffer with the true output mode. Everything else reports
*intent*.
