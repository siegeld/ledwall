# Benchmarks

Every number here was measured on hardware unless it says otherwise. Where a
figure was once wrong in this repo, the correction is kept with it — a stale
benchmark that still looks plausible is worse than none, and several of these
were believed for weeks.

**Bench rig**: Colorlight 5A-75E rev 8.2, VexRiscv `lite` @ 40 MHz, two 128×64
HUB75 panels stacked into one 128×128 canvas, 100 Mbit link on a shared /24.

---

## 1. The one-line summary

| Path | Rate | Bound by |
|---|---|---|
| Streaming, RGB888 | ~36 fps @ 128×128 | packets per second |
| Streaming, indexed | **~103 fps** @ 128×128 | packets per second |
| Streaming, full RGB, CPU filtered out | **~33 fps** @ 384×192 (9 panels) | the DMA, not the CPU |
| On-panel program, full redraw | **~12.5 fps** @ 128×128 | framebuffer stores |
| On-panel program, 18-row band | ~25 fps | framebuffer stores |
| On-panel program, palette only | refresh rate | nothing — it is 256 words |
| Cold boot, power to DHCP | **6.2 s** | reading SPI flash |

Two completely different ceilings, for two completely different reasons.
**Streaming is limited by interrupts; drawing is limited by memory writes.** An
optimisation that helps one does nothing for the other, which is why this page
keeps them apart.

---

## 2. Streaming

### Indexed vs RGB888 — the measurement that settled it

`tools/test_indexed_panel.py`, one live panel, comparing the **panel's own**
counters rather than what the sender put on the wire:

| Format | Bytes/px | Pixels/chunk | Chunks/frame @128×128 | Frames/s |
|---|---|---|---|---|
| `'B','M'` RGB888 | 3 | 487 | 34 | **36.2** |
| `'B','I'` indexed | 1 | **1461** | 12 | **102.6** |

**2.84×**, matching the chunk-count ratio almost exactly. That agreement is the
real result: it confirms the bound is **packets**, not pixels or bandwidth. The
same MTU carrying three times the pixels is three times fewer interrupts.

At 256×192 the chunk counts are 101 → 34, verified against marquee's sender.

> ### Indexed used to have a colour-artifact bug — fixed in v2.7.0
>
> Worth knowing about, because the symptom was invisible in every counter and
> the fix is in the **bitstream**, not the firmware.
>
> The framebuffer was double-buffered and swapped by gateware while the palette
> was single-buffered and written by the CPU, and there is no ordering that
> makes those agree. The display flipped to a finished frame at `swap_req` and
> that frame's palette was applied later, so for the gap the new indices were
> lit against the old palette. It showed whenever the palette changed between
> frames — **any scene with a clock or live data in it**, because the sender
> re-runs median cut and every entry shifts — as a colour flash about once a
> second. Static artwork was unaffected.
>
> **Every counter stayed clean while it happened**: frames complete, nothing
> dropped, no bad magic, refresh rock steady. The only reliable test was to
> switch the same content to `format: rgb` and watch the artifacts stop.
>
> Tracked as RB-5516 and fixed by double-buffering the palette in gateware: it
> is 512 entries now, two banks, and the live one is selected by the same
> `fb_base_eff` the scan-out follows — so a palette and the pixels that index it
> can never be from different frames. Verified on a wall streaming indexed with
> the palette changing: 3588 frames, no artifacts, no drops.
>
> **The firmware netboots but the bitstream lives in flash**, so the two are not
> updated together. On an older bitstream the firmware falls back to a single
> shared palette and the artifact returns with no other symptom. Check with
> `curl http://<card>/api/palette` — `"banks": 2` means the fix is present.

> **Indexed loses nothing on graphics and bands on photographs.** Text, logos,
> charts and flat colour quantise to 256 entries invisibly. Median cut on a
> photograph will band, and dithering is *not* the answer at this pixel pitch —
> dither noise that disappears on a 200 dpi screen is individually visible lit
> pixels here, and it crawls on moving content. Pick per content, not per panel.

### Packet pacing

| Setting | Result |
|---|---|
| ~0.8 ms between packets (~1250 pkt/s) | the measured practical limit |
| 2.0 ms spacing | 0% loss, **7.35 fps** — the clean ceiling on v1.10.6/1.10.10 |
| 1.20 ms spacing | loss appears (3.5%–11%) |
| `--delay 0.1` (old advice) | ~0.15 fps — **125× more conservative than needed** |

> **The "~15.6 fps clean" figure in older notes does not reproduce.** It was
> measured on mismatched flash gateware. Re-measured 2026-09-07 on matched
> gateware, the honest clean number is 7.35 fps. Trust a fresh
> `tools/bench_stream.py sweep` over any number on this page.

### Where the interrupt bound comes from

| Path | Cost per packet |
|---|---|
| Fast path (bitmap UDP → gateware DMA) | ~50 µs |
| Slow path (smoltcp: ARP, TCP, other UDP) | **~500 µs** |

Roughly **1.13 packets per ISR entry** — the handler is re-entered almost per
packet, so per-entry overhead dominates. That is the whole reason indexed mode
exists.

The 10× gap between the paths is why the fast path filters so aggressively:

| Fix | Before | After |
|---|---|---|
| ISR batch limit (`MAX_PACKETS_PER_ISR = 64`) | max batch 3094 | max batch 6 |
| Multicast + unwanted-UDP drop | 2562 frames dropped | 9 |
| | mac_overflow 1718 | 71 |
| Selective handler dispatch | 763 dropped (3.5%) | 12 (0.85%) |
| Foreign-ARP fast-path drop | `slow_arp` 15,553 | **0** reaching smoltcp |

One broadcast ARP per second was a visible stutter in scrolling text, because it
landed in the same ISR that consumes pixels. On a shared /24 the panel saw
1.4M multicast drops and 806k MAC overflows — **a dedicated VLAN removes the
broadcast domain instead of filtering it**, and is the real fix.

### The CPU fallback path

With the gateware DMA **off**, the CPU unpacks pixels itself:

- **~600,000 px/s hard ceiling**, flat however hard you push (18 fps at 256×128)
- Reading the payload 32 bits at a time instead of byte-at-a-time was expected
  to double it. **It did not.** Better at moderate rates (3.5% vs 11.1% loss at
  1.20 ms), a tie at 0.80 ms, and the clean ceiling unchanged. The 2× is
  unproven; do not repeat it as fact.

This path exists as a fallback. In normal operation `Hub75UdpDma` takes the UDP
sink directly and the CPU never sees a pixel.

---

## 3. On-panel programs

### The programs that ship

Measured at 128×128, program mode, nothing streaming to the panel:

| Program | fps | What it does |
|---|---|---|
| `blank` | **12.5** | returns the background and nothing else — **the ceiling** |
| `dashboard` | 8.5 | a dashboard; `dynamic` skips 110 of 128 rows |
| `ticker` | ~10 | monospace tape over an 18-row band, 2 px/frame |
| `timessquare` | **7.8** | two live tapes, spinning star, plasma, glitter, palette cycling |

**`blank` is the yardstick.** It draws *nothing* and still caps at 12.5 fps. The
gap between `blank` and your program is what your drawing costs; everything
below that is the machine, not your code.

> ### The number that was wrong for a week
>
> Earlier notes said the ceiling was **~6 fps**. It was measured while the panel
> was *also* being streamed to, so most of its time went to parsing packets it
> would never draw. The same `blank` measures **12.5 fps** with the stream off.
>
> Every "ceiling" recorded before this was found is suspect for the same reason.
> marquee skipping program-mode panels is itself worth **2×**.

### Why 12.5 fps, and why no amount of clever code beats it

| Quantity | Value |
|---|---|
| Framebuffer store (or load) | **~190 cycles** |
| Per pixel written | ~390 cycles |
| 128×128 full redraw | ~3.1M cycles |
| At 40 MHz | **~12 fps** |

No D-cache, no write buffer: every access is a full bus round trip. The ceiling
is **arithmetic**, not inefficiency.

**Not writing a word is the only real speedup available.** This is the single
most important fact about programming this panel.

A row-based refactor was tried on the theory that the per-pixel indirect call
was the cost. It was not — `blank` proved the store path dominates, and the
refactor changed nothing on its own.

### The three ways to write fewer words

**1. `display.dynamic` — redraw only the rows that change**

| Program | Full redraw | With a band |
|---|---|---|
| `dashboard` (18 of 128 rows) | 1.2 fps | **8.5 fps** — 7× |

Helps dashboards, where most of the panel is static. Does nothing on its own for
`timessquare`, where every pixel moves — but declaring its two tapes as `scroll:`
bands took it from **4.7 to 7.8 fps**, because the tapes are then shifted rather
than re-rendered and only the uncovered columns run the glyph lookups.

> Needs **two** full frames to settle, because the framebuffer is double
> buffered: a row written once lands in one buffer and the other still holds the
> old content.
>
> And **the band must cover every row the content can ink**, not the rows it
> usually does. Rows outside it are written only on a full frame, so anything
> drawn there *stays* — it looked like debris along the top edge of the tape. The
> real cause was a band of `[56, 74]` against a font that inks from row **53**.

**2. `scroll:` — shift the glass, fill what it uncovers**

Copy the band front-to-back by the scroll step, then have the program fill only
the newly exposed columns. Valid because ticker text is a pure function of
`x + frame*speed`: last frame's pixel at `x+1` **is** this frame's pixel at `x`.

| Approach | fps |
|---|---|
| Re-rendering the band every frame | 10.4 |
| Shift and fill | **24.8** — 2.4× |

**3. Palette cycling — animate without touching a pixel**

256 words per frame, **flat in wall size**. Nine panels animate for the cost of
one. Nothing else in this system has that shape. `ticker` moves colour *along*
its letters without rewriting a single pixel.

### Text is READ-bound, not write-bound

The surprise. Halving the writes barely helped:

| Band | fps |
|---|---|
| 40 rows | 8.2 |
| 18 rows | 10.4 |

**Halving the writes bought 27%, not 2×.** Drawing a glyph does several
dependent lookups per pixel — ascii table → glyph struct → sprite byte — and
with no D-cache a **read costs the same ~190 cycles as a write**. A text band is
read-bound long before it is write-bound.

This is why `cover_scroll` requires a **monospace** face: the character under a
given pixel becomes one divide instead of walking the run, so cost stops
depending on message length.

### Building ticker text from live values is free

A tape that says "OUTSIDE 62F" has to *format* a string. Doing that per pixel
would run `write!` 16,384 times a frame, which on this core is ruinous.

`scroll_message` builds it into a fixed buffer and caches the result, keyed on
`ValueTable::writes` — so the rebuild happens when **a value actually lands**,
typically once every ten seconds, not once per frame.

| | Per pixel |
|---|---|
| `scroll_text` (a `&'static str`) | baseline |
| `scroll_message` (built from values) | **+1 `u32` comparison** |

Rebuilding per frame would not have been measurable against a ~3.1M-cycle
redraw. Keying on value writes is still the better design, for a reason that is
not about speed: **the message can only change *length* on a frame where a value
landed** — which is exactly when `program_tick` has already re-armed two full
redraws. So a length change can never leave a seam in a shifted band.

### Compiled, not interpreted

A per-pixel YAML interpreter was measured at **~0.3 fps**. That measurement is
the entire reason `panelc` is a compiler that emits Rust rather than a VM that
runs on the panel — 40× below the floor of usable.

### The M extension — 6× for one linker flag

The `lite` VexRiscv reports `-march=rv32i2p0_m`: **it has always had hardware
multiply and divide.** The firmware was being built for plain `riscv32i`, so
every multiply and divide in every program was a *software routine*.

```
-C target-feature=+m     0 → 184 hardware mul/div instructions,  6× on a program
```

Switching to the `riscv32im` *target* does not work (atomics); the target
feature flag is the fix.

### Soft float will look like a hang

There is **no FPU**. `ctx.num()` parses and computes in soft float, and one
gauge calling it per pixel held a panel **under 0.2 fps**. That does not read as
"slow" — it reads as *hung*, and sends you looking for an infinite loop. Use
`ctx.int()`.

### What scan-out costs

The display DMA reads the whole framebuffer every refresh, so it competes with
the CPU for SDRAM. Measured contention: **~3%**. It is not your problem.

---

## 4. Data planes compared

The reason on-panel programs exist, in one table:

| Approach | Wire cost to show a clock face |
|---|---|
| Streaming pixels | **~24 Mbit/s** |
| Pushing values | **~20 bytes/s** |

Six orders of magnitude, for content that is a number and a font.

And the part that is not about bandwidth: **a streamed panel goes stale
silently.** When the host dies, the last frame stays lit and looks correct
forever. A program-mode panel still has its pixels — it can notice the feed
stopped and *say so* (`stale_marker`). A sign that lies is worse than a blank one.

---

## 5. Build and memory

| Thing | Value |
|---|---|
| Full release firmware build (LTO, `codegen-units=1`) | **10m 21s** |
| `dev-fast` profile | for iterating; the above is the wrong thing to sit behind |
| Framebuffer, per buffer | **262,144 words** (524,288 total, double buffered) |
| 128×128 canvas | 16,384 px — 6% of a buffer |
| 16 panels of 128×64 | 131,072 px — **half of one buffer** |
| Timing closure, `main_crg_clkout0` | 61.04 MHz against a 40 MHz target |
| Timing closure, `eth_clocks0_rx` | 130.01 MHz against 125 |

> **The framebuffer comments were 4× low for months**, claiming 65,536 words and
> "exactly 8 panels, no headroom". **Panel count is not limited by memory** and
> never was. The arithmetic was corrected in `TODO.md` on 2026-09-09 and the fix
> did not reach the code or `CLAUDE.md` until 2026-09-14.

---

## 4b. Taking the CPU out of the pixel path (v1.38.0)

The streaming ceiling was never pixels — it was one interrupt per packet. The
gateware classifier kills UDP pixel packets before the MAC raises a receive
event, so the CPU takes no interrupt for them at all.

Measured on a 384×192 RGB888 frame — 73,728 px, 152 chunks, 3 bytes/px, which is
the 9-panel case:

| Offered | Packets/s | Delivered, filter off | Delivered, filter on |
|---|---|---|---|
| 8.22 fps | 1,250 | 7.95 (20 dropped) | 7.68 (0 dropped) |
| 13.16 fps | 2,000 | 12.94 (15 dropped) | 12.50 (0 dropped) |
| 21.93 fps | 3,333 | **0.37 — collapse** | **18.6–20.5** |
| 32.89 fps | 5,000 | **0.55 — collapse** | **32.89, 60/60** |
| 50.60 fps | 7,692 | — | 35.4 |

`mac_overflow` is 0 at every rate with the filter on; it reached 1,123 without.

### What the ISR actually cost

Profiled directly with `/api/isrprof` — the 19,800 cycles/packet quoted in
RB-5486 was derived, never measured:

| | Cycles per packet |
|---|---|
| Full path | **11,311** (283 µs) |
| Dropped right after the magic | 4,938 |
| **`process_packet`, writing no pixels** | **6,373** |

Packets per ISR entry was **1.00** — no batching, so fixed entry cost dominates.

It is **not** bus-bound, which rules out the obvious explanation:

| Read | Cycles per byte |
|---|---|
| MAC packet buffer | **7** |
| DRAM | 13 |
| CSR read | 8 |

The largest single item was `merge_dma_arrival()` running per packet: 8 CSR
reads plus a full re-popcount of the 256-bit mask, and rv32i has no popcount
instruction. Made conditional: **11,311 → 10,030**.

### Why a double buffer was needed

The classifier alone did not help above ~8 fps. The arrival bitmap was
single-buffered and cleared the instant a new `frame_id` was parsed, so the CPU
had to read it within one inter-packet gap:

| Inter-packet gap | Frames shown |
|---|---|
| 10 ms | 40/40 |
| 0.8 ms | 34/40 |
| 0.5 ms | **1/40** |

Every chunk was in SDRAM; nobody could read the bitmap in time. The gateware now
latches the finished frame into `done0..7` + `done_seq` before clearing.

### Real video, and why the buffer swap had to move into gateware

600 frames of 384×192 RGB888 video at 25.8 fps:

| | CPU-timed swap | Gateware swap |
|---|---|---|
| completed | 388 | **599** |
| partial | 212 | **0** |
| dropped | 0 | 0 |
| chunks repaired | 212 | **0** |

The 212 partials were an artifact of *when* the buffer flipped, not lost data.
The gateware finishes a frame and the next begins writing at once, so until the
flip the DMA is filling the half about to be displayed:

| Who flips `fb_base` | Overlap at swap time |
|---|---|
| CPU, on the 1 ms tick | up to **10 chunks** — 6.6% of the image |
| CPU, polling flat out | still **2** |
| **Gateware, at the frame boundary** | **0** |

Visible as a band of the next frame across the top. No polling rate fixes it;
the correct instant is inside the gateware.

> All of these are a 384×192 frame driven into a board with **two panels
> physically lit**. Nine outputs of scan-out consume SDRAM read bandwidth that
> competes with DMA writes, and that is not represented here.

---

## 4c. A 3×3 wall of P1.25 modules — what the numbers say

Nine `256×128` P1.25 modules, 768×384 = 294,912 px, driven by the ICN1065
output stage on branch `feat/icn1065-spwm`. **Synthesised and simulated, never
run on a panel** — the modules are on order. Every figure below is a model or a
place-and-route result, not a measurement on glass.

### What the output stage costs

The **whole stage** — nine outputs, nine pixel sources, one pipelined arbiter,
one shared gamma set — placed against the real device (`./build.sh fit-icn1065`):

| | 9 outputs (768×384) | 9 outputs, linear | 4 outputs (512×256) |
|---|---|---|---|
| TRELLIS_COMB | **5,067 / 24,288 (20%)** | 3,706 (15%) | 2,630 (10%) |
| TRELLIS_FF | 2,203 / 24,288 (9%) | 2,095 (8%) | 1,001 (4%) |
| **DP16KD** | **0 / 56** | **0 / 56** | **0 / 56** |
| MULT18X18D | 0 / 28 | 0 / 28 | 0 / 28 |
| Timing | PASS, 64.9 MHz against 40 | PASS, 95.6 MHz | PASS, 81.9 MHz |

**Zero block RAM** is the headline. The existing HUB75 stage spends one to two
EBRs per output on row buffers; this stage needs none, because it shifts
straight from the pixel source with no bit-plane storage. That reverses the
constraint that dominated v1.40.0 — there, 9 outputs at 256 columns took 56/56
EBRs and only fitted after `nrxslots` 8→2.

### Gamma costs 5.6% of the device, once the parts are connected

Measured in isolation, nine independent output stages wanted **27** gamma
converters and 6,231 LUTs — 26% of the device, and the most expensive thing in
the design. Connected behind one arbiter they want **three**, because every
source reads the same returning data bus: all nine compute the same function of
the same input at the same instant, and differ only in *when* they latch it.
`Icn1065SharedGamma` makes that explicit rather than leaving it to Yosys to
notice, and the cost falls to **1,361 LUTs**.

It buys a real 2.8 curve corrected into 12 bits, which matters more here than on
the current panels: `hub75.py` corrects 8 bits to 8, which maps the first **28**
input codes to black. Correcting into 12 crushes only **8**, so dark gradients
keep their gradation instead of posterising.

Two things had to be got right to afford it at all, each measured:

| Attempt | Cost |
|---|---|
| Six converters per source, `*` for the multiply | **fails** — 54 DSPs on a 28-DSP device |
| Six converters per source, shift-add multiply | 10,148 LUTs (41%) |
| Three per source, shared between panel halves | 9,631 LUTs (39%) |
| **Three total, shared across all nine outputs** | **5,067 LUTs (20%)** |

The multiply is six bits by four. Written as `*`, Yosys maps it to a
`MULT18X18D` and nextpnr fails at 192% DSP usage; written as four conditional
shifts and an adder tree it costs a few LUTs and no DSP.

A real 256-entry lookup table was never an option: a combinational 256-to-1 mux
is about 85 LUT4s per output bit, so one 12-bit lookup is ~1,000 LUTs. A block
RAM would be cheap in logic but costs a cycle, and the serialiser reloads every
16 clocks whether or not the data arrived — latency in this path does not stall,
it tears the image sideways.

### The arbiter is the whole design, and the first one was wrong

Bandwidth arithmetic said a 3×3 wall needs 87 MB/s against an 80 MB/s peak —
tight, but survivable at a lower refresh. That arithmetic was necessary and
nowhere near sufficient, and simulating the arbiter is what showed it.

A blocking round-robin arbiter — hold the port, issue a read, wait for data,
release — costs `2 × sources × latency` clocks per pixel pair, because only one
read is ever in flight. Simulated against nine sources (`check_arbiter`):

| Memory latency | Clocks per pixel pair | Divider forced | Refresh |
|---|---|---|---|
| 1 | 55 | sys/4 | 36.8 Hz |
| 4 | 75 | sys/5 | 29.4 Hz |
| **16** (realistic for LiteDRAM) | **291** | **sys/19** | **7.7 Hz** |

Unusable. And the failure is not a bandwidth failure — the data itself fits
comfortably. A blocking arbiter converts a **latency** into a **throughput**
limit by refusing to have more than one request outstanding.

`Icn1065PipelinedArbiter` issues commands as fast as the memory will accept
them and matches returning data to its source through a tag FIFO, which needs
in-order return — what LiteDRAM's native port gives. Nine sources then give nine
outstanding reads (`check_pipelined_arbiter`):

| Memory latency | Blocking | Pipelined | Fits sys/4? |
|---|---|---|---|
| 1 | 55 | 21 | yes |
| 4 | 75 | 24 | yes |
| 16 | 291 | **46** | yes |
| 32 | 576 | 78 | no — sys/5, 29.4 Hz |

The cost is now roughly `2 × sources + latency` rather than
`2 × sources × latency`, and the test asserts that linear bound rather than a
number, so the claim degrades honestly if the design changes.

**This is the result that justifies the whole exercise.** Had the stage been
built from the bandwidth arithmetic alone and wired to a blocking arbiter, the
wall would have run at 7.7 Hz and the cause would have looked like SDRAM rather
than like arbitration.

### A withdrawn measurement, and why it is written down

An earlier revision of this section carried a two-column table claiming the
stage got **smaller** when the read path was added — 3,465 → 2,465 LUTs. That
was wrong, and the way it was wrong is the useful part.

The fit harness declared `mem_dat` as a bare `Signal(32)` and never drove it.
An undriven signal is a constant zero. `mem_ack` was tied to `mem_dat[0]`, so
the ack never arrived, every pixel source stalled in its read state forever,
and every signal downstream of the read folded to a constant. Yosys deleted the
entire data path, exactly as it should have, and the harness reported the cost
of nine protocol engines driving nothing.

The tell was in the result itself: adding nine framebuffer read paths cannot
reduce logic. **A benchmark that moves the wrong way is reporting on itself,
not on the design.** The fix is `FakeMemory` in `tools/fit_icn1065.py` — an
LFSR whose state depends on itself, so nothing downstream can be folded, with
a data-dependent ack that also models LiteDRAM's variable latency more
honestly than a fixed one would.

### Frame timing

`frame_clocks()` in `gateware/icn1065.py`, verified against the simulator to
**0.0%**:

```
prefix   VSYNC + PRE_ACT1/2 + five 256-clock register blocks      1,332
pixels   256 columns x 64 rows x 16 SPWM bits                   262,144
scan     64 rows x 128-clock slots                                8,192
                                                                -------
                                                                271,668 clocks
```

At a 20 MHz shift clock (sys/2, as `hub75.py` uses) that is **73.6 Hz** data
refresh — but that is not the number to plan around.

### The real constraint is SDRAM, not the FPGA

Every frame of pixel data costs framebuffer reads, one 32-bit word per pixel:

| Wall | 73.6 Hz | 60 Hz | 30 Hz |
|---|---|---|---|
| **Large** 768×384 | **87 MB/s** | 71 MB/s | 35 MB/s |
| Small 512×256 | 39 MB/s | 31 MB/s | 16 MB/s |

The `M12L16161A` is 16-bit SDR at `sys_clk`: **80 MB/s theoretical**, and
meaningfully less once refresh, precharge and the pixel DMA's own writes are
paid. So the large wall **cannot** run at its natural 73.6 Hz, and ~30 Hz is the
practical ceiling. The small wall has room at any rate.

**This is fine, and it is worth understanding why.** With S-PWM the *data*
refresh and the *flicker* refresh are decoupled: the ICN1065 generates PWM
internally at 3840–7680 Hz regardless of how often new pixels arrive. Thirty
frames a second of new content on a panel refreshing at 4 kHz is 30 fps video
with no visible flicker. Under the current BCM design those two rates are the
same number, which is why `hub75.py` has to refresh fast enough to hide
flicker.

### So what do you clock it at?

"About 30 Hz" is only half a design note. The shift clock is what *sets* the
data refresh — a frame is always `frame_clocks()` shift clocks long — so a wall
that cannot be fed at 20 MHz does not drop frames at 20 MHz. It has to be
clocked slower, and the divider is the thing someone actually puts in a PLL:

| SDRAM efficiency assumed | Shift clock | Data refresh, 768×384 |
|---|---|---|
| 80% | sys/3 = 13.3 MHz | 49.1 Hz |
| **60%** | **sys/4 = 10 MHz** | **36.8 Hz** |
| 50% | sys/5 = 8 MHz | 29.4 Hz |

The efficiency figure is the least trustworthy number on this page — it stands
for refresh, precharge, row misses and the pixel DMA's own writes, none of which
have been measured on this design. It is an argument to
`sustainable_refresh()` rather than a constant for exactly that reason.

The small 512×256 wall is a different shape of problem: it comes out at
**sys/2 = 20 MHz and 73.6 Hz**, limited by the shift clock rather than by SDRAM.
Two walls, two binding constraints.

**Against the original target** — "9 panels at around 20 fps with full RGB" —
even the pessimistic row clears it, on 294,912 pixels. That is 2.25× the pixels
of the current 768×256 configuration.

That is why `Icn1065Output`'s row scanner **free-runs between frames**: it keeps
the panel lit and lets the caller choose a data refresh it can afford, rather
than the maximum the shift clock allows.

### What is still unknown

- **Nothing here has driven a panel.** The protocol is transcribed from a
  working ESP32 driver, not captured from a logic analyser.
- **Nothing is wired into the SoC.** The stage above is complete and placed on
  its own; `colorlight.py` still instantiates `hub75.py`. Until the two are
  built together, "does it all fit" is arithmetic — the v1.40.0 SoC is 11,663
  LUT4 / 6,395 FF / 56/56 EBR, and this stage hands nearly all those EBRs back
  while taking 5,067 LUTs.
- **The memory model is a stand-in.** `FakeMemory` is an LFSR with a variable
  accept and a four-deep return pipeline, not LiteDRAM. The 16-cycle latency
  figure the arbiter is judged against is an estimate, and the real controller
  may reorder or stall in ways this does not capture.
- **Gamma is built and verified in simulation, not on glass.** The curve is
  right to 0.24% and the gateware matches its software twin on all 256 codes,
  but whether 2.8 is the correct exponent for *these* modules is a question
  only the panel can answer.
- The slot width (`ROW_OE_LEN = 128`) is the reference's "most important tuning
  parameter for this panel" and may well differ for a 256×128 module.

---

## 4c-bis. The whole SoC, built — and why a 3x3 wall needs two cards

Everything above measures the output stage alone. This is the real build:
`./build.sh` with `--panel 256x128-icnd1065`, the complete SoC — VexRiscv,
LiteDRAM, LiteEth, the pixel DMA — with the ICND1065 stage wired in place of
`hub75.py`. A bitstream comes out.

| Outputs | Wall | TRELLIS_COMB | DP16KD | Timing at 40 MHz |
|---|---|---|---|---|
| 4 | 512×256 | 15,980 (65%) | 36 / 56 | **PASS, 53.3 MHz** |
| **5** | 1280×128 | 16,499 (67%) | 36 / 56 | **PASS, 43.7 MHz** |
| 9 | 768×384 | 18,861 (77%) | 36 / 56 | **FAIL, 37.7 MHz** |
| 9, gamma off | 768×384 | 18,085 (74%) | 36 / 56 | **FAIL, 37.6 MHz** |

**The block RAM result held.** 36/56 against the HUB75 build's 56/56 — the ICN
stage hands back twenty EBRs, which is what made a 256-column build stop being a
squeeze in the first place.

**Nine outputs does not close timing, and gamma is not why.** Removing the gamma
units saves 776 LUTs and buys 0.1 MHz. At 74–77% LUT utilisation the design is
routing-congested, and congestion — not any one path — is what holds it at
~37.6 MHz. Placement variance between runs is over 2 MHz at that density, which
is itself the signal: a build that close to the edge is not one to trust on
hardware.

So **a 3×3 wall of these panels is two cards, 5 and 4** — and that is not a
workaround for the connector count, it is the only configuration that closes
timing. It also happens to be faster (49.1 Hz against 36.8) and to halve the
framebuffer each card must hold. Three independent limits, one answer.

### Two timing fixes worth keeping

The first build came in at **26.2 MHz**, and the critical path was the
arbiter's rotated priority chain — nine muxes deep, from a pixel source's FSM
state through `arb_pick` to the issue logic. **Registering the pick took it to
33.1 MHz placed / 39.7 MHz routed.** The chain is still computed; it just no
longer sits in the issue path. A registered choice can be a cycle stale, so
eligibility is re-checked at issue through a single 9-to-1 mux.

That costs a cycle per issue, and the honest consequence is that **the pipelined
arbiter is now SLOWER than the blocking one at trivial memory latency** — 39
clocks against 18 at latency 1. There is nothing for a pipeline to hide when
there is no latency. At a realistic 16-cycle LiteDRAM latency it is 56 against
288, five times better. `check_pipelined_arbiter` asserts the crossover in both
directions rather than reporting only the cases that flatter it.

The second attempt — registering the pixel handshake to take the serialiser's
bit counter out of the `served` latch's path — measured 37.7 MHz against the
previous 39.7. **It is kept anyway**, because at this density a 2 MHz swing
between runs is placement noise rather than a result, and the registered
handshake is correct on its own terms. It is recorded here so the next person
does not re-derive it and read the noise as a regression.

---

## 4d. HUB320 — four RGB groups per connector

HUB320 carries **four** sets of RGB data lines on one connector where HUB75
carries two, so twelve data pins instead of six, driving four horizontal slices
of the panel at once instead of two. Cards typically expose eight of them, for
32 groups in total.

The protocol on the wires is unchanged — same CLK, LAT and OE, same driver ICs,
same register sequence. In this codebase it is one parameter, `groups`, on
`Icn1065Serialiser`, `Icn1065Output` and `Icn1065PixelSource`
(`GROUPS_HUB75 = 2`, `GROUPS_HUB320 = 4`).

### A PORT is not a MODULE, and eight ports means eight modules

Worth stating plainly, because getting it wrong inflates every wall estimate by
2x. **An eight-port HUB320 card drives eight modules, not sixteen.** A port
normally feeds ONE module — one that needs all four groups, which means a
1/32-scan 256×128 or a 256×256 at 1/64 scan.

The tempting error is to reason "a HUB320 port carries four groups, a 1/64-scan
256×128 module presents two, therefore one port drives two modules". That
configuration does exist, but only through a fan-out adapter splitting the four
groups into two HUB75 connectors — the "adapter board" a commercial receiver
integrates. It is not what a bare port does, and it needs specific hardware.

So when sizing a wall:

| | HUB75 card | HUB320 card |
|---|---|---|
| Ports | 8 | 8 |
| Groups per port | 2 | 4 |
| Modules (no adapter) | 8 | 8 |
| Module a port suits | 1/64-scan 256×128 | 1/32-scan 256×128, or 256×256 |

The pixel counts below are quoted per PORT and are unaffected by any of this:
eight ports of four groups is 524,288 px however the modules behind them are
arranged.

**A 3×3 wall of 1/64-scan 256×128 modules is nine 2-group modules, so it needs
nine HUB75 ports** — one more than a card has. That is a second, independent
reason it is two cards, alongside the timing result in section 4c-bis.

### The cost is pins, not bandwidth

This is the point worth internalising before choosing a card. A request fetches
four pixels instead of two, but each covers twice the panel, so **bytes per
pixel is identical**. Sixteen HUB75 outputs and eight HUB320 outputs cover the
same pixels for the same 63 MB/s, which `check_hub320()` asserts directly.

Eight HUB320 outputs at 256×256 each — a 524,288 px wall — placed against the
real device (`./build.sh fit-icn1065` with `--groups 4 --outputs 8`):

| Metric | 9 × HUB75 (294,912 px) | 8 × HUB320 (524,288 px) |
|---|---|---|
| TRELLIS_COMB | 5,067 (20%) | **5,669 (23%)** |
| TRELLIS_FF | 2,203 (9%) | 3,313 (13%) |
| **TRELLIS_IO** | 57 / 197 (28%) | **99 / 197 (50%)** |
| DP16KD | 0 / 56 | **0 / 56** |
| MULT18X18D | 0 / 28 | 0 / 28 |
| Timing | PASS, 64.9 MHz | PASS, 70.7 MHz |

**1.8× the pixels for 3% more logic**, comparing nine HUB75 ports against eight
HUB320 ports. Logic is not the constraint and neither is block RAM. IO pressure
doubles but still fits, and that is the honest limit on how far this goes: 16
HUB320 ports would want 192 data pins against 197 — and no card exposes that
many anyway.

### Bandwidth is the constraint, and the lever is bytes per pixel

| SDRAM efficiency | Shift clock | Refresh, 524,288 px |
|---|---|---|
| 80% | sys/5 = 8 MHz | 29.4 Hz |
| **60%** | **sys/7 = 5.7 MHz** | **21.0 Hz** |
| 50% | sys/8 = 5 MHz | 18.4 Hz |

That is right at the edge of the 20 fps target, and it is not fixable by
choosing a bigger FPGA. The framebuffer reads a 32-bit word per pixel and uses
24 bits of it, so at a 48 MB/s budget:

| Framebuffer format | Refresh, 524,288 px |
|---|---|
| 4 bytes/px (today) | 22.9 Hz |
| 3 bytes/px, packed | 30.5 Hz |
| RGB565, 2 bytes/px | 45.8 Hz |

**For a denser wall, bytes-per-pixel stops being an optimisation and becomes
the enabling change.** `TODO.md` item 5 files RGB565 as deferred on its own
merits; at HUB320 densities it is the thing that decides whether the wall is
watchable. The counter-argument is real — 5 bits of red is 32 source levels
into a 12-bit PWM path — but the gamma curve already recovers the dark end far
better than the current 8-to-8 correction does.

### RGB565: the format change that makes a dense wall possible

The framebuffer stores one 32-bit word per pixel and uses 24 bits of it. A
16-bit pixel -- **two to a word** -- halves both the memory a card must hold and
the bandwidth it must read, which are the two limits that bind before logic or
timing ever do.

| | 4 bytes/px | **2 bytes/px** |
|---|---|---|
| Framebuffer capacity | 327,680 px (10 modules) | **655,360 px (20 modules)** |
| 9 modules (294,912 px) | sys/4, **36.8 Hz** | sys/2, **73.6 Hz** |
| 8 x HUB320 (524,288 px) | sys/7, 21.0 Hz | sys/4, **36.8 Hz** |
| 8 x HUB320 fits in memory? | **no** | **yes** |

That last row is the point. A 524,288 px wall does not fit the framebuffer at
four bytes per pixel **at all** -- it is not slow, it is impossible. At two
bytes it fits with 25% spare.

The nine-module case is now limited by the shift clock rather than by memory:
sys/2 is as fast as this design shifts, so 73.6 Hz is the ceiling and there is
bandwidth left over.

**It costs 4 LUTs.** Measured, 5 outputs, full SoC:

| | 32-bit pixel | 16-bit pixel |
|---|---|---|
| TRELLIS_COMB | 16,670 (68%) | **16,674 (68%)** |
| DP16KD | 37 / 56 | 37 / 56 |
| Timing | PASS, 42.3 MHz | PASS, 49.6 MHz |

### The default picks itself: the widest pixel that fits

Colour is free until the framebuffer runs out, so the right default is not a
format you choose but the widest one that fits. `pix_bits="auto"` (the default)
computes it, and the build says what it picked:

    framebuffer: 32-bit pixels, 163,840 of 327,680 px (50% full)

| Wall on one card | Auto picks | Why |
|---|---|---|
| 5 modules (half a 3x3) | **32-bit** | 50% full, full colour |
| 9 modules (3x3) | **32-bit** | 90% full, full colour |
| 10 modules | **32-bit** | exactly full |
| 8 HUB320 ports (524,288 px) | 16-bit | 32-bit does not fit |

A wall that fits at neither is **refused at build time** rather than truncated,
because a wall that does not fit shows the missing part as black and nothing
says why.

So RGB565 is never something to reach for on a wall that fits without it, and
the 3x3 this project is building fits comfortably.

### The bit order is deliberate, and it is not conventional RGB565

    bits [0:5] red    [5:11] green    [11:16] blue

Red in the LOW bits, matching the 32-bit layout's `0x00BBGGRR` rather than
conventional RGB565, which puts red at the top. Keeping one channel order
across both formats is worth more than matching a convention nobody here reads:
this repo has already shipped a rotated channel order once, and it is invisible
in review and obvious only on the wall.

5- and 6-bit codes are expanded by folding the high bits back into the low ones,
so  reaches 255 rather than stopping at 248 -- the same trick
 uses, and the reason white is white.

### What is not built

The **write** side. The pixel source reads 16-bit pixels; nothing yet writes
them. `LiteDRAMDMAWriter` asserts all byte enables on every write, so two
pixels cannot be packed into one word by partial writes -- the host has to send
them already paired, one 32-bit word carrying two pixels, which is a new wire
format in  plus chunk arithmetic in . Until that
exists,  builds and simulates but has nothing to display.

### What is not built

The **pin mapping**. The fit above concatenates pairs of the board's HUB75
connector entries to get twelve pins per output: a real pin count and a real
placement, but not a real HUB320 pinout. Those are card-specific and vendors
differ — NovaStar ships both HUB320 and HUB320F, and Colorlight, Huidu and
Sysolution each have their own. It is a config table, written once a card is
chosen, not a redesign.

---

## 5b. Boot, and the cost of reading flash

A cold power-on is **6.2 s**, power to DHCP. It was ~20.6 s when flash first
became bootable (v1.36.0), and all of the difference is one number:

| Read | Cycles per byte |
|---|---|
| DRAM | **14** |
| SPI flash, `default_divisor=9` (2 MHz) | **1142** — 28.5 µs, 78× slower |
| SPI flash, `default_divisor=1` (10 MHz) | **246** |

The `spiflash` region is `cached=False` with `READ_1_1_1`, so every access is its
own command-plus-address transaction — 40 SPI clocks before a single byte comes
back. A word-wise read costs 650 cycles for four bytes, so the transaction, not
the data, is essentially the whole cost.

`flashboot()` pays it three times over: `check_image_in_flash()` runs in
`flashboot()` and **again** inside `copy_image_from_flash_to_ram()`
(`boot.c:623`), so the 292,976-byte image is CRC'd twice and then copied.

| | divisor 9 | divisor 1 |
|---|---|---|
| crc32 pass ×2 | 16.72 s | 3.62 s |
| copy to DRAM | 1.19 s | 0.28 s |
| **flashboot** | **17.92 s** | **3.90 s** |

Full-image read at each divisor, all returning identical data:

| Divisor | SPI clock | Full image | Speedup |
|---|---|---|---|
| 9 | 2.0 MHz | 1248.8 ms | 1.00× |
| 4 | 4.0 MHz | 662.9 ms | 1.88× |
| 2 | 6.6 MHz | 428.5 ms | 2.91× |
| **1** | **10.0 MHz** | **311.3 ms** | **4.01×** |
| 0 | 20.0 MHz | 194.1 ms | 6.43× |

20 MHz reads correctly; 10 MHz ships for margin, because the failure mode is a
board that will not boot standalone.

The rest of the budget, for completeness: ECP5 configuration ~1.4 s (computed),
BIOS + DRAM init + `serialboot()` 0.53 s, `netboot()` failing 0.32 s, firmware
start → DHCP 0.4 s. Total ≈6.55 s against 6.24 s observed.

> **Two warnings if you extend these measurements.** `mcycle` (`csrr 0xB00`)
> reads back **0** on this VexRiscv — which is why §3's `/api/bench` figures are
> gathered with an external stopwatch and why that endpoint's own JSON is all
> zeros. Use `Timer0`. And `Timer0` wraps every second, so time anything long in
> chunks and sum: the first full-image sweep reported the *slowest* divisor as
> the fastest, because 1248.8 ms aliased to 247 ms.

---

## 6. Reproducing these

```bash
# streaming sweep — loss vs packet spacing
python3 tools/bench_stream.py --host <board-ip> sweep

# indexed vs RGB, end to end, using the panel's own counters
python3 tools/test_indexed_panel.py --host <board-ip>

# on-panel program frame rate
curl http://<board-ip>/api/bench

# what is actually on the glass, not what was requested
curl http://<board-ip>/api/fb
```

### Rules for trusting a measurement here

These were each learned the expensive way:

1. **Check the panel is alive before every run.** A dead board reports zero
   errors, because nothing is running to fail.
2. **Never bisect on a single run.** The worst bug in this project's history was
   intermittent at about **1 run in 3**. Bisection failed on it outright; a
   breadcrumb in uncached SDRAM found it.
3. **No counter can see colour.** Indexed mode was broken for a full release
   with `bad_magic` at 0, chunks counting, frames completing, and the test
   reporting PASS at 2.84× — while the wall was one flat colour. Only looking at
   the glass, or at `/api/fb`, finds that class of fault.
4. **Say whether a number is measured or predicted.** v1.11.0 shipped a
   *predicted* 3× from the chunk-count model and labelled it as such; the
   measured 2.84× arrived a release later. Both are on this page, marked.
