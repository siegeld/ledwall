# Display programs — composing on the panel instead of streaming to it

Design note for running content **on** the Colorlight board rather than blasting
finished bitmaps at it from marquee. Written 2026-09-14.

This does not replace streaming. Video, camera and photographic content stay
host-side, permanently — see *Why host-side rendering* in `marquee/README.md`,
which is right about codecs. What this adds is a second content class: text,
data and procedural graphics that the panel generates itself.

---

## 1. Why bother — the numbers, not the vibe

Every hard ceiling recorded in `TODO.md` is a cost of **shipping pixels**, not a
cost of driving panels:

| Constraint | Cause |
|---|---|
| ~600k px/s CPU ceiling | unpacking streamed RGB |
| ~20.6 fps at 256x192 indexed (v1.11 era) | per-interrupt cost — **removed in v1.38.0**, see below |
| packet loss, `mac_overflow` | the UDP frame stream |
| item 6, dedicated panel VLAN | ~24 Mbps sustained per panel, forever |
| item 1, arrival-tracking correctness bug | reconciling streamed frames |

A 256x192 panel streaming RGB at 20 fps is **24 Mbps continuously — even to show
a static clock face**. Push named values instead and it is a few hundred bytes a
second.

Two things follow that matter more than the bandwidth:

- **Failure behaviour.** A streamed sign goes *black* when the host, the network
  or marquee hiccups. A locally-composed sign keeps showing last-known values and
  can mark them stale.
- **Idle cost.** Streaming burns full bandwidth and full CPU to display something
  that is not changing.

---

## 2. What the hardware actually gives you

**Read this section before designing anything.** An earlier draft of this note
got it wrong and the corrected version is below.

### sys_clk is 40 MHz

`gateware/colorlight.py`, `sys_clk_freq=40e6`. Item 10 records that 60 MHz was
tried and reverted — the HUB75 clock is not decoupled from `sys_clk`.

### Indexed mode is ONE 32-BIT WORD PER PIXEL

`gateware/hub75.py`: `palette_port.adr.eq(ram_data & 0x000FF)`. The palette index
is the **low byte of a full 32-bit word**. Indexed mode therefore does **not**:

- shrink the framebuffer
- reduce scan-out DMA read bandwidth
- allow 4 pixels per store

The top 24 bits of every word are simply unused in indexed mode. **Packing four
indices per word would deliver a genuine 4x** on framebuffer size, scan-out
bandwidth and CPU store cost — but it does not exist today, and it is not as
simple as it sounds. See below.

#### Index packing is constrained by per-panel ROTATION

`RamAddressGenerator` computes

```
adr = base + (y_offset + panel_y*16) * image_width + x_offset + panel_x*16
```

and `x_offset`/`y_offset` are swapped by the orientation `Case` on
`(panel_config >> 16) & 0x3`:

| Orientation | scan traversal in framebuffer | packable? |
|---|---|---|
| `0b00` | x = column, y = row — **consecutive addresses** | **yes** |
| `0b10` | x = col_max - column — consecutive, **descending** | yes, byte order reverses |
| `0b01` | x = row, y = column — **strides by `image_width`** | **no** |
| `0b11` | x = row, y = col_max - column — strides by `image_width` | **no** |

For the rotated orientations, four consecutive scan-out pixels live in four
different words, so byte packing buys nothing there.

**The design that handles this gracefully** is a one-word register cache rather
than a hard 4:1 serializer: have the address generator emit a *pixel* linear
index, derive `word_adr = index >> 2` and `byte_sel = index & 3`, and **suppress
the DRAM request when `word_adr` equals the previous one**. Unrotated panels then
get the full 4x automatically, `0b10` gets 4x with reversed byte order within the
word, and rotated panels degrade to exactly today's behaviour — no gain, but no
regression and no correctness risk.

The delicate part is not the cache, it is that suppressing requests changes the
`LiteDRAMDMAReader` valid/ready handshake and the `rsv_level` in-flight
accounting in `RamToBufferReader`. Full-colour mode must also bypass the whole
path (one word per pixel, no byte select), which makes pipeline depth
mode-dependent — historically where the subtle bugs in this design live.

Firmware impact is wide: `write_img_data`, `write_img_rgb888`, `img.rs`,
`bitmap_udp.rs` and `patterns.rs` all assume one word per pixel, as does the
meaning of `length`.

**Status: designed, not built.** It needs a bitstream rebuild and hardware
testing, and a wrong scan-out pipeline shows as a garbled or dark wall needing
JTAG recovery — so it is not a change to land untested.

### Making BLAST mode faster — indexed wire format is the lever

A goal in its own right, and it lands on indexed mode too, which is why the two
workstreams share a foundation.

**The CPU is out of the blast-mode pixel path entirely.** `Hub75UdpDma` takes
`udp_sink` directly and writes SDRAM in gateware, and since v1.38.0 the CPU does
not even take an interrupt for a pixel packet — the gateware classifier kills it
before the MAC raises an event.

> **The per-interrupt bound below is history, not current.** It was real: 1.13
> packets per ISR entry, ~20.6 fps at 256×192 indexed, and ~5.5 fps for full RGB
> across nine panels. Removing the interrupt moved full RGB at 384×192 to
> **~33 fps**, so "send fewer packets" is no longer the only lever — it is just a
> cheap one. See `docs/BENCHMARKS.md` §4b.

Fewer chunks per frame is still worth having, and the cheapest way to cut it is
to send fewer bytes:

| Wire format | bytes/frame @ 256x192 | chunks | implied fps |
|---|---|---|---|
| RGB888 (today) | 147,456 | ~101 | ~20 |
| **Indexed, 1 byte/px** | **49,152** | **~34** | **~60** |

Three times fewer packets is three times fewer interrupts, straight against the
binding constraint.

**The change is contained.** `Hub75UdpDma` expands each incoming byte to a 32-bit
word with the index in the low byte — the **framebuffer format does not change**,
so there is no rotation constraint, no `write_img_*` rewrite, and no firmware-wide
impact. It is a much smaller and safer change than packing.

Costs: marquee must quantise to 256 colours host-side and push the palette (256
words, trivial). mantel already does Floyd-Steinberg for the ePaper frame, so the
capability exists in the estate. Photographic content loses real quality at 256
colours; graphics and text do not. Make it a per-scene choice, not a global mode.

### Ordering

1. **Indexed wire format** — ~3x blast mode, contained, no framebuffer change.
   **BUILT 2026-09-14** (see below).
2. **Index packing** — 4x SDRAM traffic on writes, and on reads for unrotated
   panels. Buys little while interrupts are the bound, so do it when a fresh
   measurement says SDRAM is the next wall.
3. **Local programs** on top of indexed mode.

### Stage A as built (2026-09-14)

Magic `'B','I'` alongside `'B','M'`, header layout unchanged, so the format is
self-describing per packet and old senders are unaffected.

- `gateware/dma_writer.py` — `indexed` latched from the magic; the framebuffer
  word becomes `Cat(sink.data, Signal(24))`, i.e. the index in the low byte that
  `ram_data & 0x000FF` already reads. `PIX` emits a word on every byte instead of
  every third. Chunk stride is derived, not registered:
  `chunk_pixels = chunk_index * (3 * pixels_per_chunk)` when indexed.
- `bitmap_udp.rs` — both magics accepted; `indexed` held **per frame** because
  the repair path runs after the packet that set it. `ppc()` (487 or 1461) feeds
  the pixel offset, the `max_chunks` bound and the repair loop.
- `hub75.rs` — `write_img_indexed()` for the CPU fallback: four pixels per
  aligned word load against the same four stores.

`PIXELS_PER_CHUNK_INDEXED` is `3 * PIXELS_PER_CHUNK` = **1461, not
`MAX_PAYLOAD`** (1462). 1462 is not divisible by three, so using it would put
every chunk after the first one pixel left of where the hardware wrote it — which
would present as a tearing or shearing artefact, not as an obvious error.

**Verified:** firmware compiles (10m21s, full LTO); bitstream builds and **timing
closes** — `main_crg_clkout0` 61.04 MHz against a 40 MHz target, `eth_clocks0_rx`
130.01 MHz against 125 MHz. The extra mux levels on the address and data paths
cost no timing margin. The SVD diff is timestamps only, so no CSRs changed and
the PAC is untouched.

(That 61.04 MHz ceiling is also why item 10's 60 MHz attempt was marginal.)

**NOT verified:** nothing has run on a panel — the board is powered down. And
nothing speaks `'B','I'` yet: marquee must quantise to 256 colours, push a
palette, and use 1461 pixels per chunk. Until then the format is dead code that
compiles and places.

**Resolved since:** the output mode now follows the wire format. `bitmap_udp.rs`
calls `set_mode()` on a format change, so the magic is authoritative and there is
no second setting to drift. Writing the panel test is what forced the decision —
it needed a way to put the panel in indexed mode, and adding an endpoint would
have created exactly the two-places-disagree bug this repo has been bitten by
before.

An earlier draft of this note had packing first. It should be second: the wire
format is the bigger win for streaming, the lower risk, and it unblocks the local
program work regardless.

### What indexed mode DOES give you, and it is the important one

**Palette cycling is O(1) in pixel count.** Rewriting the 256-entry palette is
**256 words per frame regardless of how many panels are in the wall.** Nine
panels animate for exactly the same CPU as one. Nothing else in this system has
that shape, and it is the single most valuable property here.

The whole demoscene canon — plasma, fire, water, tunnels, colour cycling, fades,
flashes — animates through the palette with the framebuffer held still.

### `fb_base` is a writable CSR

`gateware/hub75.py` exposes `fb_base` as `CSRStorage`, already wired to the row
reader. It is what double buffering flips, and it also buys:

- **Hardware scrolling** — advance `fb_base` by a row's worth of words and the
  whole display scrolls. Zero pixel writes; one CSR write per frame.
- **Page flipping** — at 9 panels a frame is 73,728 words against 524,288 in the
  region, so roughly **7 complete frames** can be resident and played back with
  one CSR write each, at zero CPU during playback.

### Framebuffer size — the number in the comments is wrong

`FB_TOTAL_WORDS = 0x00400000 / 2 / 4` is **524,288**, so each buffer is
**262,144 words**. The comments on `hub75.rs:8-9` say 131072 and 65536 — both 4x
low — and `CLAUDE.md` repeats the wrong figure. `TODO.md` item 4 corrected this
on 2026-09-09; the correction never reached the code. Fixed 2026-09-14.

| Panels | Pixels = words/buffer | of 262,144 |
|---|---|---|
| 6 (current) | 49,152 | 19 % |
| 9 | 73,728 | 28 % |
| 12 | 98,304 | 38 % |
| 16 (one per connector) | 131,072 | 50 % |

Memory is not what limits panel count. Rev 8.2 defines 16 connectors; outputs
shift **in parallel**, so adding connectors costs no refresh time while
`chain_length: 2` halves it. Scale wide across connectors, not down chains.

### Derived cost model

Bus accesses cost ~17 cycles with no D-cache (`VexRiscv_Lite`, I-cache only).

| Path | cycles/px | px/s | 9 panels (73,728 px) |
|---|---|---|---|
| UDP streaming (**measured**) | ~67 | 600 k | **8 fps** |
| Local procedural (**derived**) | ~25 | 1.6 M | **~21 fps** |

Local generation wins by dropping 3 loads per pixel from the packet path, not by
packing. **The ~25 figure is derived and unmeasured** — see §8.

---

## 3. Three tiers of motion

Order of preference, cheapest first:

**Tier 0 — O(1) in pixels. Nearly free, works today.**
Palette cycling, `fb_base` scrolling, page flipping. These **compose**: a
scrolling, colour-cycling, page-flipped display costs almost no CPU, leaving the
whole budget for one procedural layer on top.

**Tier 1 — CPU procedural, full screen, ~20 fps at 9 panels.**
Requires discipline: table-driven maths, no per-pixel divide/sqrt/trig,
word-aligned stores. A plasma calling `sin()` per pixel lands back at 8 fps.

**Tier 2 — gateware.** Spare ECP5 fabric: a blitter for rect fill and sprite
copy, a second layer with alpha blend for crossfades, procedural generators at
pixel clock, or the index-packing change above. Where 60 fps with compositing
lives.

**What does not work:** streaming it (that is the 8 fps path), or interpreting
per pixel (~0.3 fps — see §5).

---

## 3b. Scaling the wall

### Scale wide across connectors, not down chains

Board rev 8.2 defines **16 connectors** (`j1`..`j16`); `--outputs 6` is a build
flag, not a hardware limit. Outputs shift **in parallel** — shared clock and
latch, private RGB lines — so adding connectors costs no refresh time.
`chain_length: 2` doubles the per-row shift and **halves** refresh. One panel per
connector is the scaling axis.

Memory is not the limit either (see §2): 16 panels of 128x64 is 131,072 px,
**half of one 262,144-word buffer**.

### Growing the panel count takes FOUR coordinated changes

1. Rebuild the bitstream: `--outputs N --chain-length M`
2. Regenerate the PAC
3. `hub75.rs`: `OUTPUTS`, `CHAIN_LENGTH`
4. `layout.rs`: `MAX_OUTPUTS`, `MAX_CHAIN`

**Miss (4) and it fails silently.** `LayoutConfig::parse()` tests
`n <= MAX_OUTPUTS`, so connectors above it are dropped with no error and those
panels show the unassigned default — which on the wall is indistinguishable from
dead connectors or bad cabling, on a board with no serial console.

Const asserts in `hub75.rs` now make a firmware-internal mismatch a **build
failure** (added 2026-09-14). They cannot see the gateware, so a
bitstream/firmware mismatch still needs the panel-count CSR check in §8.

Also update the panel's TFTP layout (`grid:` plus a `J<n>:` line per connector)
and marquee's panel record geometry. A 3x3 of 128x64 panels is 384x192.

### Several bitstream builds for several wall sizes

Already the pattern — see `bitstreams/variants/`. What makes it scale is the rule
in §4: **a display program must never encode geometry.** Canvas size comes from
config, so swapping a 9-panel build for a 4-panel build is a bitstream-and-config
change that touches no program.

The cost of multiple builds is that each pairs with firmware constants that must
agree, which is exactly the failure above — so the asserts matter more, not less,
once more than one build is in rotation.

---

## 4. The program model

A program is **Rust, compiled into the firmware, selected by name in the
per-panel config.**

```rust
// programs/robot_lab.rs
use crate::display::*;

pub struct RobotLab {
    plasma: Handle<Plasma>,
    temp:   Handle<Label>,
    bar:    Handle<Bar>,
}

impl Program for RobotLab {
    fn build(c: &mut Canvas) -> Self {
        // runs once at boot; c.w()/c.h() come from config, never hardcoded
        Self {
            plasma: c.add(Plasma::new().palette(FIRE).speed(1.2)),
            temp:   c.add(Label::new(FONT_8X13).at(4, 24).color(GREEN)),
            bar:    c.add(Bar::new().at(4, c.h() - 12).size(c.w() - 8, 8).max(100.0)),
        }
    }

    fn update(&mut self, c: &mut Canvas, ctx: &Ctx) {
        let t = ctx.value("temp_f");
        c[self.temp].write(format_args!("{:.1}F", t));
        c[self.temp].set_color(if t > 80.0 { RED } else { GREEN });
        c[self.bar].set_value(ctx.value("disk_pct"));
        c[self.plasma].set_speed(1.0 + ctx.value("cpu_load"));
    }
}
```

**It is retained-mode.** `build()` runs once; `update()` runs per frame and
touches only properties — it never sees a pixel. The native compositor does the
blitting. This is `displayio`'s architecture, and it is why CircuitPython feels
pleasant: the ergonomics come from retained mode, not from the language.

`Handle<T>` indexes a fixed-capacity `heapless::Vec` built during `build()`.
**Nothing allocates after boot** — the firmware has no `global_allocator`.

### The rule that keeps programs portable

**A program must never encode geometry.** Canvas size comes from config; use
`c.w()`/`c.h()` or normalised coordinates. `patterns.rs` already has this
instinct with `fn(width, height, phase)`. Get it right and swapping a 9-panel
build for a 4-panel build touches no program. Get it wrong once and every
program becomes wall-specific.

### Safety without an MMU

Programs only receive `Handle`s and safe APIs, never raw pointers. Safe Rust
cannot form the out-of-bounds write that put a counter inside `.text` at
`0x40020000` (see `CLAUDE.md`). **The borrow checker is the memory protection
this SoC does not have**, and it is why native programs are acceptable here at
all.

A frame-deadline watchdog on Timer0 (working since v1.10.11) should fall back to
a safe pattern if `update()` overruns.

---

## 5. Why not a scripting language

Per-pixel interpretation costs 10-30 bytecode ops per pixel with dispatch
overhead on a CPU with no D-cache — conservatively 20-50x native, so **12-30k
px/s, or 1.6-4 seconds per frame** at 9 panels. A quarter of a frame per second.

The dividing line is exactly the retained-mode boundary: a script mutating a
scene graph does ~500 operations per frame, which at 30 fps is 15k ops/s and
entirely affordable. **Scripting is viable if and only if it never touches
pixels** — at which point Rust does the same job with no runtime.

---

## 6. Loading and the mixed fleet

`./build.sh firmware boot` fetches `boot.bin` over TFTP into SDRAM and runs it.
**Loading a program and loading firmware are already the same operation** and it
takes seconds. The friction is compile time, not deployment — hence the
`dev-fast` profile added alongside this note.

Per-panel selection rides the per-MAC config that already exists:

```yaml
mode: program          # or: stream (default, today's behaviour)
program: robot-lab
params: { speed: 1.4, palette: ice }
```

**One firmware contains every program; config picks which runs.** This matters
because `TODO.md` item 8 records that marquee **cannot do per-panel firmware** —
`tftp_resolve` ignores the `firmware` field, so every panel gets the same
`boot.bin`. Putting the mode in config sidesteps that entirely: a mixed fleet
(some panels streaming, some composing) needs no per-panel firmware at all, and
a panel flips between modes with a config edit and a reboot.

If a program fails to load or validate, **fall back to `stream`** rather than
showing nothing.

Marquee must not blast UDP at a panel running `mode: program`. Do not configure
that twice — have the panel report its mode in `/api/status` and let marquee read
it. This repo has already been bitten by constants that must agree and do not
check each other (§7).

### If programs later ship independently of firmware

Link at a fixed SDRAM address, pass a function table in, TFTP the blob
separately. Tractable, but the real cost is **the ABI, not the loader**: once
programs and firmware ship separately every draw call is a versioned contract,
and a stale program on new firmware jumps through a stale function pointer on a
board with no MMU and no serial console. Needs an ABI version stamped in both and
refused on mismatch, plus checksum and watchdog. Earns its keep when someone
other than the firmware author writes programs, or when one panel of nine needs
updating alone. Not before.

---

## 7. Fonts and assets

`HUB75.md` in the `esphome` repo already has the legibility rules, learned
expensively. **There is a break at ~10px and the guidance inverts across it:**

| Size | Source | Format |
|---|---|---|
| <= 10px | pixel-grid BDF (Silkscreen, Tom Thumb, Cozette) | **1bpp mask** |
| > 10px | outline face rasterised at exact size, weight 400-500 | **4bpp alpha** |

Above 10px, heavier is *worse* — a weight-600 stroke closes both counters of an
`8` at 20px. Below 10px **no outline font works at any weight or bpp**: the info
display ran Roboto Mono @700 at 8px and `0` and `8` were indistinguishable.
Anti-aliasing a 1px stem spreads it across two dim pixels instead of lighting one
clean one. At that size the font is the variable, not the knobs.

Today the firmware has only `FONT_5X7` in `patterns.rs` — 12 glyphs (`0-9`, `.`,
`v`) for the boot banner.

### The palette-ramp trick

Reserve 16 consecutive palette entries as a ramp from background to text colour.
A 4bpp glyph's nibble **is** the offset into that ramp, so blitting anti-aliased
text is `fb[x] = ramp_base + nibble` — one add, no blend, no multiply. Changing
text colour rewrites 16 palette words and the framebuffer never moves.

### SVG

Runtime SVG is out — a parser plus an anti-aliasing path rasteriser, allocating,
at 40 MHz. And it buys nothing: you always know the exact target size.

Rasterise upstream instead, at build time (`build.rs` + `resvg`) or host-side
(marquee already has Pillow, and can push sprites the panel caches by id). The
output is the same either way, and the unification is the point:

```
SVG icon    ─┐
             ├─ rasterise ─→ 4bpp alpha sprite ─→ ramp blit
outline font ─┘
pixel BDF font ───────────→ 1bpp mask         ─→ index blit
photo / logo ─────────────→ indexed or RGB    ─→ direct blit
```

**An anti-aliased SVG icon and a large glyph are the same asset type**, blitted
by the same code. Three sprite formats total; `Label` is a program drawing a run
of glyph sprites.

One constraint: a single ramp gives one colour plus alpha. That covers all text
and virtually every UI icon, but a multi-colour anti-aliased illustration needs
its own palette region or a direct-colour program.

### And the lesson that applies hardest to artwork

`HUB75.md`: `"BRK"` on a ~20px plate was blamed on flash rate, then contrast, and
neither was the problem — **three glyphs do not fit in that space at any weight,
bpp or face.** The fix was an icon from two rectangles: no counters to close, no
anti-aliasing to smear, read rather than decoded.

The same applies to vector art. Scaling a detailed SVG to 12px gives mush however
good the rasteriser. **At marker sizes geometry beats artwork** — keep `rect`,
`line` and `circle` first-class in the program API, and treat SVG as the source
for things large enough to survive it.

---

## 8. Open questions

- **The measurement that decides the shape of this.** Put a panel in indexed mode
  and time a full-screen table-driven effect. Near 21 fps and Tier 2 is optional;
  near 8 and the blitter moves up the list. Everything in §2's cost model below
  the measured row is derived arithmetic.
- **Index packing** (§2) — 4 indices per word is the single largest available
  win and it is a gateware change.
- **Value transport.** `POST /api/values` on the existing HTTP server is the
  lowest-friction option: no new client, no MQTT, no polling loop. Not yet built.
- **Time.** A clock widget needs a source. Simplest is marquee including a
  timestamp with each value push, with Timer0 free-running between.
