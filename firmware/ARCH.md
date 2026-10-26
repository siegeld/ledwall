# Architecture

How the SoC, the gateware and the firmware fit together.

There are **two output stages** behind one SoC, chosen at build time by the
panel preset:

| Stage | Card | Connector | Driver chips | Section |
|---|---|---|---|---|
| bit-plane PWM, `gateware/hub75.py` | 5A-75E (and E320) | HUB75, 2 RGB groups | shift-register constant-current | §1–§12 |
| **S-PWM**, `gateware/icn1065_soc.py` | **E320** | **HUB320**, 4 RGB groups | ICN1065 / DP3264S family | **§13** |

Everything up to §12 — network, wire protocol, pixel DMA, double buffering,
boot — is shared. §13 is what differs, and it is where the four-output E320
work lives: a line buffer, a pipelined and block-fetching reader, and the
resource trims that let four outputs fit an LFE5U-25F.

For **what we discovered about the board itself** — the flash part, the missing
D-cache, the JTAG traps — see [docs/HARDWARE.md](docs/HARDWARE.md).
For **numbers**, see [docs/BENCHMARKS.md](docs/BENCHMARKS.md).
For the E320 bring-up story, wrong turns included, see
[docs/E320-BRINGUP.md](docs/E320-BRINGUP.md).

---

## 1. The shape of it

```
┌───────────────────────────────────────────────────────────────────┐
│                     Colorlight 5A-75E rev 8.2                     │
│                                                                   │
│   ┌──────────┐    ┌──────────┐    ┌──────────────┐  ┌──────────┐  │
│   │ VexRiscv │    │  LiteEth │    │ Hub75UdpDma  │  │  HUB75   │  │
│   │  "lite"  │    │   MAC    │───▶│  (gateware)  │  │  driver  │  │
│   │  40 MHz  │◀───┤ 2 rx / 2 │    │ pixels->SDRAM│  │ 6 × 2    │  │
│   └────┬─────┘    └────┬─────┘    └──────┬───────┘  └────┬─────┘  │
│        │               │                  │              │        │
│        ▼               ▼                  ▼              ▼        │
│   ═══════════════════ Wishbone ═══════════════════════════════    │
│        │               │                                 │        │
│        ▼               ▼                                 ▼        │
│   ┌─────────┐    ┌───────────┐                     ┌─────────┐    │
│   │  SDRAM  │    │ SPI Flash │                     │  CSRs   │    │
│   │   4 MB  │    │   4 MB    │                     │  64 KB  │    │
│   └─────────┘    └───────────┘                     └─────────┘    │
└───────────────────────────────────────────────────────────────────┘
```

The one structural thing to understand: **the CPU is not in the pixel path at
all.** Not the data, and since v1.38.0 not even the interrupt.

The receive stream is forked in gateware. One copy goes to `Hub75UdpDma`, which
parses the header and writes pixels straight to SDRAM. The other goes to the
CPU's MAC — and on that branch `SmolEthPixelClassifier` recognises a pixel packet
by beat 9 and has `SmolEthInvalidator` end the frame with an error, so the MAC
discards it and **never raises a receive event**. The CPU takes no interrupt, and
the slot is reusable immediately.

```
                    ┌─► Hub75UdpDma ──► SDRAM          (pixels, always)
   LiteEth core ────┤
                    └─► classifier ──► invalidator ──► CPU MAC
                          pixel packet?  kill it        (everything else)
```

That ordering matters: the classifier is an **observer** that never drives
`ready`, because the splitter only advances when both consumers take a beat, and
anything that backpressures the CPU branch would throttle the pixel branch too.

The consequence is that streaming is no longer bounded by interrupts. It used to
be — one interrupt per packet at ~10,000 cycles, which put a hard ceiling near
1,250 packets/s and made *indexed* mode the only lever worth pulling, because it
carries 3× the pixels per packet. With the CPU out of the way the bound moves to
the DMA and full RGB becomes practical: **~33 fps at 384×192**, nine panels'
worth, against 5.5 fps before.

The CPU still owns *presentation*: it reads the DMA's arrival bitmap to know
which chunks landed, and flips the buffer. See §5.

---

## 1b. What runs, and what only builds

**Once a card is programmed, marquee is the entire runtime.** Nothing in this
repo runs continuously — `build.sh`'s TFTP server exists for bring-up only, and
marquee deliberately owns port 6969 on the fleet host.

| | colorlight | marquee |
|---|---|---|
| Gateware + firmware | **builds** | serves the built image over TFTP |
| On-panel programs | **compiles** into the firmware | selects which one runs |
| Panel layout | can bake a default in | serves the authoritative one |
| Pixels | — | renders and streams |
| Live values | program consumes them | pushes them |
| Stats, inventory, scheduling | — | owns all of it |

**marquee is the source of truth. Flash is the survival copy.** The LiteX BIOS
here tries `netboot()` **first** and falls through to `flashboot()` when the
network cannot answer — the reverse of stock LiteX, via
`patches/litex-bios-netboot-first.patch`.

| At boot | What runs |
|---|---|
| marquee reachable, link up | the image **marquee** serves — so `./build.sh deploy` + reboot is a real update |
| no server, or the PHY has not negotiated yet (cold power-on) | the image **in flash** — the panel comes up standalone |

This works because `netboot()` is a single bounded attempt: one try, no link
wait, no retry, every wait inside `tftp.c` a counted loop. It cannot hang the
cold path; it costs a couple of seconds of TFTP timeout before falling through.

| To update | Do |
|---|---|
| what a panel runs **now** | **`./build.sh deploy`**, then reboot the panel |
| what it will run **after a power cut** | **`./build.sh flash-firmware`** (or `flash-all` for gateware too) |
| the gateware or the BIOS | **`./build.sh flash-all`** — neither lives in the firmware image, and the BIOS is in block-RAM ROM, so it ships only inside a bitstream |

> **Both halves of this were broken until 2026-09-17, in opposite directions.**
> `flashboot()` had never once succeeded: `flash-firmware` wrote the raw
> `boot.bin`, and the BIOS needs an FBI header — `length` (u32), `crc32` (u32),
> then the image — so it read the firmware's first RISC-V instruction as a 1 GB
> length and rejected it every time. `build.sh` now wraps the image with
> `crcfbigen.py -f -l` and refuses to flash one whose header would not pass
> (v1.36.0).
>
> Fixing that alone would have broken the other half: with stock LiteX ordering,
> a panel holding good firmware in flash never fetches the deployed image, so
> `deploy` becomes a silent no-op and every change needs a JTAG re-flash. Hence
> the reorder. **Keep both flashed and deployed in step** — otherwise a panel
> quietly changes behaviour the next time the power blinks.

**A cold boot is 6.2 seconds**, power to DHCP, measured with a marked power-on
and a packet capture:

| Stage | Cost |
|---|---|
| ECP5 configuration from flash | ~1.4 s |
| BIOS start, DRAM init, `serialboot()` | 0.53 s |
| `netboot()` failing, which is what hands over to flash | 0.32 s |
| `flashboot()` | 3.90 s |
| firmware start → DHCP | 0.4 s |

It was ~20.6 s the day flash first became bootable, because `flashboot()` reads
the image out of memory-mapped SPI flash **three times** — `check_image_in_flash()`
runs once in `flashboot()` and again inside `copy_image_from_flash_to_ram()`, so
the CRC is computed twice, then the image is copied. At LiteSPI's default clock
that was 17.9 s on its own. `LiteSPIPHY(default_divisor=1)` took it to 3.9 s
(v1.37.0). See [docs/HARDWARE.md §8c](docs/HARDWARE.md) for the measurements.

A panel therefore needs **nothing on the network to boot** — which is what
`--default-config` (v1.28.0) was built for, and what the fleet could not do
before. See [docs/HARDWARE.md §8](docs/HARDWARE.md).

## 2. Two ways to get pixels on the glass

The whole system is these two paths, and they never share a panel.

| | **Stream mode** | **Program mode** |
|---|---|---|
| Who composes pixels | a host, elsewhere | **the panel** |
| What crosses the wire | frames, ~24 Mbit/s | values, ~20 bytes/s |
| Frame rate | **~33 fps full RGB @384×192 (9 panels)**, ~103 fps indexed @128×128 | ~12.5 fps ceiling |
| If the host dies | last frame stays lit, **looks fine forever** | panel keeps drawing, and can say so |
| Selected by | `mode: stream` | `mode: program` + `program: <name>` |

**They are mutually exclusive by design.** A native test pattern exits program
mode; program mode stops the stream *and* the gateware DMA. Two writers on one
framebuffer is a class of bug with no good failure mode — it looks like
corruption, not like a conflict.

> An early "hold" was a lie: it stopped the CPU parser while the gateware DMA
> kept writing, so a panel reporting itself held was still being overwritten ten
> times a second. `set_stream_hold()` now stops both.

---

## 3. The network stack

**smoltcp** (patched fork at `sw_rust/smoltcp-0.8.0/`) handles ARP, ICMP, TCP,
UDP and DHCP in software over raw LiteEth frames. A hardware-only stack
(etherbone) was rejected because it cannot do TCP, and TCP is what gives us
telnet and an HTTP UI.

The fork exists for one reason: it exposes **DHCP option 66** as
`Config.tftp_server_name`, so a panel learns its boot server from DHCP.

### Pixel packets never get here

Since v1.38.0 the gateware kills UDP pixel packets on the CPU branch before the
MAC raises a receive event (§1), so none of the filtering below ever sees one.
The numbers in this section are the *history* that led to the gateware fix, and
they still describe what happens to every other packet on the wire.

### Everything runs in the interrupt handler

Since v1.10.0 the whole network stack runs inside the external-interrupt
handler (`network_handler()` in `network.rs`). The main loop contains **zero**
network code.

```
packet arrives → IRQ #2 → trap handler saves all GPRs → network_handler()
   │
   ├─ for each packet in the RX FIFO (max 64 per entry):
   │     bitmap UDP?      → gateware DMA already has it; bookkeeping only
   │     foreign ARP?     → ack and drop, without waking smoltcp
   │     multicast?       → drop
   │     unwanted UDP?    → drop
   │     otherwise        → iface.poll()   ← ~500 µs, ten times the fast path
   │
   ├─ run only the socket handlers the traffic warrants
   └─ disable ev_enable, return; the main loop re-enables
```

Four filters, in that order, each added because of a measured problem:

| Filter | Why it exists |
|---|---|
| `MAX_PACKETS_PER_ISR = 64` | an unbounded loop processed **3094** packets in one entry and starved the display |
| multicast drop | VRRP and mDNS were taking the 500 µs path |
| unwanted-UDP drop | NetBIOS broadcasts, likewise |
| foreign-ARP drop | one broadcast/second was a **visible stutter** in scrolling text |

Selective dispatch matters as much as the filters: a single ARP used to invoke
all six socket handlers. Now TCP handlers (cheap when idle) run every entry and
UDP handlers only when UDP arrived — halving ISR time for the commonest traffic.

The **main loop** does only: timer tracking, interrupt re-enable, display
refresh / program tick, the serial menu, and MAC error counters.

### Streaming detection

The ISR stamps `LAST_BITMAP_PACKET_MS` on every bitmap packet. "Streaming" means
`TIME_MS - LAST_BITMAP_PACKET_MS < 200`.

> With the pixel filter on, no bitmap packet reaches the ISR, so liveness comes
> from the hardware instead: `merge_dma_arrival()` advances `last_packet_ms`
> whenever the DMA's arrival bitmap grows. A chunk landing in SDRAM is activity
> whoever noticed it. Before that, `tick()` judged every frame stale the instant
> it looked — 30 frames sent came back 30 stale and 30 dropped.

---

## 4. The wire protocol

UDP port 7000. Four formats told apart by the magic. Three share a 10-byte
header `<2sHBBHH>`; the fourth widens the chunk counters:

```
narrow  0..1 magic  2..3 frame_id  4 chunk_index u8   5 total u8          6..7 width  8..9 height    -> pixels at 10
wide    0..1 'BW'   2..3 frame_id  4..5 chunk_index   6..7 total (u16)   8..9 width  10..11 height  -> pixels at 12
```

| Magic | Header | Payload | Pixels/chunk | Handled by |
|---|---|---|---|---|
| `'B','M'` | 10 | RGB888, 3 B/px | 487 | `Hub75UdpDma` (gateware) |
| `'B','I'` | 10 | indexed, 1 B/px | **1461** | `Hub75UdpDma` (gateware) |
| `'B','P'` | 10 | palette, RGB triples from byte 4 | 256 entries in one packet | CPU |
| **`'B','W'`** | **12** | RGB888, 3 B/px, u16 counters | **486** | `Hub75UdpDma` (gateware) |

Chunk N starts at pixel `N × pixels-per-chunk`. The card computes that offset
itself (`chunk_pixels` in `gateware/dma_writer.py`, `ppc()` in
`bitmap_udp.rs`), so the sender and the card must agree on the stride exactly —
a mismatch **shears** the image rather than raising anything.

### The MTU budget is part of the protocol

**Every packet must fit a 1500-byte MTU: 1472 bytes of UDP payload.** The card
cannot reassemble IP fragments; it parses the first one as if it were the whole
packet and never sees the rest.

| Format | Header + payload | IP datagram |
|---|---|---|
| `'B','M'` | 10 + 3×487 = 1471 | 1499 — 487 was chosen to fill it exactly |
| `'B','I'` | 10 + 1461 = 1471 | 1499 |
| `'B','W'` at 487 px *(before gateware 1.46.0)* | 12 + 1461 = **1473** | **1501 — fragmented** |
| `'B','W'` at 486 px | 12 + 1458 = 1470 | 1498 |

So **a wide chunk is one pixel shorter**, and that is not a tuning choice. When
the wide header was added its payload was described as "identical" to the
narrow one, and nobody did the byte arithmetic. Every wide chunk left the
sender as two IP fragments; the first carried 1460 of the 1461 pixel bytes, and
the last pixel of every chunk was never updated. On glass that was a **fixed
diagonal lattice of stale pixels**: chunk N's lost pixel is N×487+486, which on
a 256-wide frame steps 25 px left and ~1.9 rows down per chunk. Every card
counter — underruns, dropped chunks, held frames — read zero, because a
truncated payload looks exactly like a legal short last chunk. The evidence
was on the **sender**: `Ip: FragOKs` in `/proc/net/snmp` rose by one per chunk.
Frames of 255 chunks or fewer use the narrow header and were never affected,
which is why it surfaced only with the first full four-output frame (270
chunks). See [E320-BRINGUP §6a](docs/E320-BRINGUP.md).

> **Adding a header field or a magic? Recompute header + payload + 28 against
> 1500 first.** The sender in the companion server asserts the budget for both
> headers at import time, and its wire test checks every packet of a 270-chunk
> frame.

`'B','W'` is used **exactly when a frame needs more than 255 chunks** — the
narrow header cannot express more. The firmware relies on that: the DMA's
frame snapshot does not report the magic, so the paths that adopt a frame from
the hardware read its format back from `total_chunks > 255`.

**The magic drives the output mode.** `'B','I'` puts the panel in indexed
output; `'B','M'` returns it to full colour. Guarded, so it is one CSR write per
format *change*, not per packet.

> Without that, an indexed stream sent to a panel left in full colour renders
> every palette index as a blue value — **a dim, plausible-looking picture
> rather than an obvious fault.** That is the worst way for it to fail, and it
> happened: three call sites forced `FullColor` after every presented frame, so
> indexed mode shipped broken for a release with every counter reading healthy.
> No counter can see colour.

`1461 = 3 × 487` — an indexed chunk carries exactly three times the pixels of an
RGB chunk, so it matches the gateware's existing chunk stride and needs no new
register.

**The framebuffer format does not change between modes.** An index is written as
`0x000000II`, the low byte the palette lookup already reads (the pixel word is
`0x00BBGGRR` on this panel — see docs/HARDWARE.md §3). So indexing touches neither the scan-out path nor
`write_img_rgb888`.

Palette packets are passed to the CPU by the DMA **without** counting as bad
magic — so `bad_magic` keeps meaning "something is wrong" rather than climbing
during normal operation. Re-sending a rotated palette animates an entire wall
for ~778 bytes a frame, whatever the panel count.

Wrong-sized frames are **refused** and counted as `bad_size`, rather than drawn
as a garbled part-frame while every counter reads healthy.

---

## 5. Double buffering

Both output stages have an `fb_base` CSR (20-bit, in 32-bit SDRAM **words**)
selecting which SDRAM region the scan-out reads. The framebuffer is two halves
of 327,680 words each (`FB_HALF_WORDS`), one pixel per word:

```
SDRAM (4 MB), byte offsets
  0x000000 ─ 0x140000   firmware text/data/bss     1.25 MB (the image is ~300 KB)
  0x140000 ─ 0x280000   framebuffer half 0         327,680 words
  0x280000 ─ 0x3C0000   framebuffer half 1         327,680 words
  0x3C0000 ─ 0x400000   stack                      256 KB reserved
```

| | Word offset (`fb_base`) | CPU address (uncached mirror) |
|---|---|---|
| Buffer 0 | `0x50000` | `0x90140000` |
| Buffer 1 | `0xA0000` | `0x90280000` |

A half holds 327,680 pixels — a 3×3 wall of 256×128 modules (294,912) with
room to spare, and a 256×512 four-output frame (131,072) comfortably.

**One definition, three consumers.** `FB_BASE_BYTES`/`FB_HALF_BYTES` live in
`gateware/hub75.py` and are imported by the scan-out, by the pixel DMA in
`colorlight.py`, and mirrored by `FB_SDRAM_OFFSET_BYTES` in `hub75.rs`;
`build.sh` refuses to build if the Rust side disagrees. There were three
independent copies until 2026-09-18, and changing two of them made the display
and the DMA address different halves.

**The stack reserve is deliberate.** `memory.x` aliases the stack region to the
whole of main RAM, so the stack starts at the top of SDRAM and grows down — into
the framebuffer's upper half, which used to end at `0x400000` too. It never bit
while a 128×128 image used 16,384 words of a half; a 768×384 wall would have.

The CPU always writes the **back** buffer, then `swap_buffers()` swaps the slice
references and writes the new front address to `fb_base`. One CSR write flips
both the display and the pixel DMA's target, so the two can never disagree about
which half is live — doing it as two writes once left a window in which a chunk
landed in the buffer being displayed.

> **This is why partial redraw needs two full frames to settle.** A row written
> once lands in one buffer while the other still holds the old content. Any
> `dynamic` band must be flushed twice, and forgetting that looks like a
> rendering bug.

### The gateware owns the swap, because the CPU cannot be quick enough

The gateware finishes a frame and the next one **starts writing immediately**.
Until the buffer is flipped, the DMA is filling the very half that is about to be
displayed, and everything it writes in that window lands on the glass as a band
of the next frame across the **top** of the image.

Measured at 25.8 fps on a 384×192 frame:

| Who flips `fb_base` | Overlap at swap time |
|---|---|
| CPU, on the 1 ms timer tick | up to **10 chunks** — 6.6% of the image |
| CPU, polling flat out | still **2** |
| **Gateware, at the frame boundary** | **0, by construction** |

No polling rate fixes it. The correct instant is between the last pixel of one
frame and the first of the next, and only the gateware is there.

`Hub75UdpDma` pulses `swap_req` at the frame-change latch; `hub75.py` flips an
internal `fb_base_eff` on it when `auto_swap` is set. **Scan-out and the DMA's
`write_base` both follow `fb_base_eff`**, so they cannot disagree about which
half is live. A CSR write to `fb_base` still wins, so firmware can place the
buffer explicitly with `auto_swap` off.

> **The trap that made this worse before it made it better.** `pix_adr` for the
> incoming frame is computed at `hdr_idx == 9` — the same cycle `swap_req` fires
> — and a Migen `NextValue` lands at the *end* of that cycle. So the new frame's
> **first chunk** was addressed with the outgoing base and written into the
> buffer now on screen: a glitch at the top of *every* frame, continuously,
> worse than the intermittent one it replaced. `write_base_next` computes the
> frame-change case explicitly rather than waiting a cycle for the register.

### Repair is off when the gateware swaps

`present()` used to patch missing chunks from the front buffer. With the hardware
swapping at the frame boundary the "back" buffer already holds the **next** frame
by the time `present()` runs, so patching it overwrites live pixels with
two-frame-old ones — a whole-frame glitch instead of the band it was meant to
hide.

It was also doing harm on frames that were fine. `arrival` is set when a chunk's
**write completes**, so a bit that lands after the frame-change latch leaves the
bitmap one short for a frame whose pixels are all present. Repair then copied
stale content over correct content. Removing the CPU-timed swap removed the
symptom entirely:

| 600 frames at 25.8 fps | CPU swap | Gateware swap |
|---|---|---|
| completed | 388 | **599** |
| partial | 212 | **0** |
| chunks repaired | 212 | **0** |

### Only whole frames reach the glass

With the gateware swapping and repair off, a frame that **lost** chunks would
still be swapped onto the glass with a hole of two-frame-old pixels where the
lost chunks should have landed. The receive FIFO drops whole packets when it
overflows, ~26 at a time, so the hole is a band ~49 rows tall. On a scrolling
ticker that reads as **jitter** (text at a stale position); on video it reads
as **flicker** (a stale image band). One cause, two symptoms, and it was
reported as both.

So the swap is **gated on completeness** (gateware 1.36.0). The DMA's arrival
bitmap already knows whether the outgoing frame is whole; if it is not, the swap
is skipped, the previous complete frame stays up for one more frame period (a
50 ms repeat at 20 fps), and `frames_held` counts it. The incoming frame then
overwrites the damaged back buffer, because the write base only moves when a
swap fires — nothing lost, nothing written twice.

| CSR | Reset | Meaning |
|---|---|---|
| `swap_whole_only` | **1** | the gate; 0 restores the old behaviour for an on-glass A/B |
| `frames_held` | — | frames not shown because they were incomplete |

`frames_held/s` against `c_dropped/s` says how many drops became an invisible
repeat instead of a torn band.

---

## 6. On-panel programs

A program is a **pure function of `(ctx, x, y) → palette index`**. The panel
asks it for every pixel of every frame; it never draws *into* anything.

### Compiled, never interpreted

`tools/panelc.py` reads `panels/*.yaml` and emits Rust:

```
panels/foo.yaml ──panelc──▶ src/assets.rs     4bpp coverage sprites (fonts + SVG)
                            src/generated.rs  one render fn per program + registry
                                    │
                                    └──▶ compiled into the firmware
```

**A per-pixel YAML interpreter was measured at ~0.3 fps.** That number is the
entire reason this is a compiler and not a VM. YAML is a *build-time* artifact,
exactly as in ESPHome — nothing is parsed on the panel.

Widgets compile to **ordered early-returns**; the first that covers a pixel
wins, so **earlier is on top** — the opposite of CSS. A `lambda:` splices raw
Rust in at its position in that order, so user code composes *with* declarative
widgets instead of living beside them.

Fonts and SVG icons rasterise to the same thing: 4bpp coverage, blitted through
16-entry palette ramps as `ramp_base + coverage` — one add, and anti-aliasing
comes out free. One pipeline, one consumer (`sprite.rs`).

### The value plane

`POST /api/values` into a 16-key table with per-key age and a staleness clock.
That is the whole data plane: ~20 bytes/s against the ~24 Mbit/s a streamed
panel eats to show a static clock face.

Because the pixels are *already on the panel*, it can render the fact that the
feed went quiet (`stale_marker`). A streamed panel cannot — it just keeps
showing a frame that looks correct. **A sign that lies is worse than a blank
one.**

### Making it survive a reboot

`mode: program` and `program: <name>` in the TFTP config are applied at boot,
with a fallback to stream if the named program is not in the firmware. One
firmware holds every program and the config picks which runs — so selecting a
program is a config change, not a build.

See **[docs/PROGRAMMING.md](docs/PROGRAMMING.md)**.

---

## 7. The three configuration layers

These must agree. Two of them are compile-time, one is not.

### Layer 1 — the bitstream

Built with `--panel`, `--chain-length`, `--outputs`. Sets HUB75 shift-register
timing: `columns`, `rows`, `scan` (= rows/2), `chain_length_2` (log2 of panels
per output), `n_outputs`.

Exposed back as read-only CSRs (`hw_columns`, `hw_rows`, `hw_config`) so the
firmware can read its own geometry and show it in the web UI.

**Default build**: columns=128, rows=64, scan=32, chain_length_2=1, n_outputs=6.

### Layer 2 — firmware constants

`hub75.rs`: `CHAIN_LENGTH` must equal `1 << chain_length_2`, `OUTPUTS` must
equal `n_outputs`. **A mismatch crashes the SoC** — the firmware addresses panel
CSRs that do not exist.

Const asserts now check that `layout::MAX_OUTPUTS`/`MAX_CHAIN` cover them.
Before that, growing the wall without updating `layout.rs` **silently dropped
connectors**, which on the wall is indistinguishable from dead panels or bad
cabling.

### Layer 2b — the compile-time default config (optional)

A layout, and optionally `mode:`/`program:`, baked into the firmware by
`build.rs` from `./build.sh --default-config <file>`. Same format as the TFTP
config and parsed by the same `LayoutConfig::parse()`, applied at boot **before
the network comes up**.

This is what makes a panel standalone. The program code was always compiled in;
what came only from the network was the line selecting it and the panel map.

| | Without a baked config | With one |
|---|---|---|
| No network | one panel at the bitstream's geometry, expecting a stream | its real layout, running its program |
| Network present | TFTP config applies after DHCP | baked config applies at once, TFTP **overrides** it |

TFTP winning is deliberate: a default must never stop you changing a panel
remotely. And there is no runtime cache of a fetched config — a stored copy that
could disagree with what the server is serving is worse than no copy.

### Layer 3 — the runtime layout

Fetched at boot from `<mac>.yml` over TFTP (port **6969**, not 69):

```yaml
grid: 2x1              # 2 panels across, 1 down
panel_width: 128       # must match the bitstream's `columns`
panel_height: 64       # must match `rows`
J1: 0,0 1,0            # chain slot [0] -> grid (0,0);  slot [1] -> (1,0)
mode: stream
```

Virtual size = `panel_width × grid_cols` by `panel_height × grid_rows`. The
firmware writes that width to the DMA `image_width` CSR, which the gateware uses
as the framebuffer row stride.

| Connector | Output | Chain slots | CSRs |
|---|---|---|---|
| J1–J6 | 0–5 | `[0]`, `[1]` | `panelN_0`, `panelN_1` |

Each panel CSR holds x (8-bit, ×16), y (8-bit, ×16), rot (2-bit).

> **`MAX_CONFIG_SIZE` was 512 and truncated silently.** A 6-panel layout is 311
> bytes and a 3×3 is 347 — so the cap sat ~165 bytes from dropping connectors.
> Now 2048, and a truncated config is **refused** rather than partly applied.
>
> Unknown keys are still ignored silently, so a misspelled key is a no-op.

---

## 8. Boot sequence

1. **BIOS** runs from ROM, initialises SDRAM
2. **BIOS tries the network first**, then flash — `netboot()` fetches `boot.bin`
   when marquee is reachable, and falls through to `FLASH_BOOT_ADDRESS` in about
   0.32 s when it is not (§1). A cold power-on normally lands on flash, because
   the PHY has not negotiated yet
3. **Firmware starts** — reads the flash unique ID, derives a locally-
   administered MAC `02:xx:xx:xx:xx:xx`, brings up HUB75
4. **Interrupts** — installs the trap handler, enables ETHMAC IRQ #2
4b. **Pixel filter on** — firmware sets `pixel_filter_enable`, and from here the
   CPU takes no interrupt for pixel traffic. Deliberately done **here** and not
   defaulted on in the bitstream: the CSR resets to 0 on FPGA configuration, so
   the BIOS always netboots with the filter off and a power cycle always
   recovers a bad firmware
5. **DHCP** — acquires an address; falls back to a static one after 10 s
6. **TFTP config** — fetches `<mac>.yml` from DHCP option 66 (or a fallback), on
   port 6969
7. **Layout applied** — parses it, configures panel CSRs, redraws at the new
   virtual size
8. **Mode applied** — `mode: program` starts the named program, falling back to
   stream if it is not in this firmware
9. **Main loop** — display refresh and program ticks; all network in the ISR

Steps 5–8 happen inside `network_handler()`. The config fetch usually completes
1–2 s after DHCP.

Which files appear in the TFTP log says where the panel booted from:

| Log shows | Booted from |
|---|---|
| `boot.bin` then `<mac>.yml` | **network** — link was already up (warm reboot) |
| `<mac>.yml` only | **flash** — normal for a cold power-on |
| `boot.bin`, no `<mac>.yml` | network, then died before asking for its config — a gateware/firmware mismatch, not a network fault |

**A cold boot is about 6.2 s**, power to DHCP, of which ~3.9 s is the BIOS
reading the firmware out of SPI flash. See [docs/HARDWARE.md §8c](docs/HARDWARE.md)
for the full budget.

---

## 9. Key files

### Gateware

| File | Purpose |
|---|---|
| `gateware/colorlight.py` | the LiteX SoC: peripherals, memory map, clocking, `xbarperf` |
| `gateware/hub75.py` | HUB75 scan-out driver, `fb_base`, panel position CSRs; **the framebuffer map** |
| `gateware/icn1065_soc.py` | the **S-PWM** stage as an SoC block: per-output sources, line buffers, arbiter, CSRs, the `EXP_*` knobs (§13) |
| `gateware/icn1065.py` | S-PWM building blocks: prefix/command generator, serialiser, row scanner, pixel source, line buffer, arbiters |
| `gateware/spwm_chips.py` | per-driver-chip register tables (DP3264S, ICND1065, DP3364S) |
| `gateware/colorlight_e320.py` | the E320 platform: the measured HUB320 connector table |
| `gateware/helper.py` | `hub75_conn()` / `hub320_conn()` — connector to pin requests |
| `gateware/dma_writer.py` | `Hub75UdpDma` — UDP sink straight to SDRAM; the wire formats and their strides |
| `gateware/smoleth.py` | LiteEth glue, the pixel classifier, `burst_fifo` and its instruments |

### Firmware — `sw_rust/barsign_disp/src/`

| File | Purpose |
|---|---|
| `main.rs` | entry, ISR setup, main loop (display + program tick only) |
| `network.rs` | the ISR: packet dispatch, DHCP, TFTP, HTTP, telnet, program tick |
| `ethernet.rs` | smoltcp device driver, `peek_rx()`/`ack_rx()`, **MTU** |
| `bitmap_udp.rs` | wire formats, mode selection, palette staging, `bad_size` |
| `hub75.rs` | double-buffered framebuffer, `swap_buffers()`, buffer access |
| `http.rs` | HTTP/1.1 server, status page, REST API |
| `layout.rs` | TFTP YAML parser |
| `tftp_config.rs` | TFTP client for `<mac>.yml` |
| **`program.rs`** | `Ctx`, `Program`, `RowFn`, the palette, `sin64`, `hash2` |
| **`prim.rs`** | the drawing library — borders, text, scroll, sprites, effects |
| **`sprite.rs`** | 4bpp coverage sprites, fonts, `cover_at`, `cover_scroll` |
| **`values.rs`** | the 16-key value table and staleness clock |
| **`generated.rs`**, **`assets.rs`** | emitted by `panelc` — **do not edit** |
| `patterns.rs` | built-in test patterns |
| `breadcrumb.rs` | crash trail in uncached SDRAM, survives `soc_rst` |
| `flash_id.rs` | flash unique ID → MAC |
| `panic.rs` | must never spin forever — there is no serial console |

### Tooling

| Path | Purpose |
|---|---|
| `build.sh` | every build, load and flash path |
| `exp/build-out4.sh`, `exp/Dockerfile.out4` | the four-output E320 build and its BIOS-trimmed image (§13.6) |
| `tools/sim_icn1065.py` | the S-PWM stage's 120-odd checks (`./build.sh sim-icn1065`) |
| `tools/sim_litedram.py` | row-fill rate against the real LiteDRAM core, P1.25 geometry by default |
| `tools/sim_linebuf3.py` | three-slot line buffer correctness, with the row flip modelled |
| `tools/sim_fillrate.py` | the pipelined/block reader against the shipped one, cell for cell |
| `tools/panelc.py` | YAML → Rust compiler for on-panel programs |
| `tools/bench_stream.py` | throughput / loss sweep |
| `tools/test_indexed_panel.py` | end-to-end indexed vs RGB, from panel counters |
| `tools/send_{video,youtube,image,animation}.py` | simple senders |
| `panels/*.yaml` | the programs that ship |
| `.tftp/` | TFTP root: `boot.bin` + `<mac>.yml` |

---

## 10. Memory map

| Region | Address | Size |
|---|---|---|
| ROM | `0x00000000` | 64 KB — LiteX BIOS |
| SRAM | `0x10000000` | 8 KB — BIOS scratch (4 KB on the four-output E320 build, `EXP_SRAM`) |
| Main RAM | `0x40000000` | 4 MB — SDRAM through the 8 KB L2; firmware runs here |
| EthMAC | `0x80000000` | 8 KB — 2 RX + 2 TX slots × 2 KB (8 RX before v1.40.0) |
| SPI Flash | `0x80400000` | 4 MB — memory-mapped |
| Flash boot | `0x80500000` | firmware load address (chip offset `0x100000`) |
| Uncached SDRAM | `0x90000000` | 4 MB — the same SDRAM, bypassing L2; the framebuffer is accessed here |
| CSR | `0xF0000000` | 64 KB |

The SDRAM's own layout (firmware / framebuffer / stack) is in §5.

**CSR addresses move between builds.** Adding a CSR block (`xbarperf`, in the
four-output build) shifted `CTRL` from `0xf0002000` to `0xf0002800`. The
firmware's exception handler resets the SoC by writing `CTRL`'s reset register,
and it used a hard-coded address — so on that build a CPU fault would have
**hung** the card instead of rebooting it, with a power cycle as the only way
back. It now takes the address from the PAC, which carries each block's base as
a const generic generated from the same build:

```rust
const fn periph_base<RB, const A: usize>(_: Option<&pac::generic::Periph<RB, A>>) -> usize { A }
static CTRL_RESET_ADDR: usize = periph_base(None::<&pac::Ctrl>);   // read by the asm handler
```

Anything the firmware addresses by number rather than through the PAC is a bug
waiting for the next CSR.

Alignment rules and the flash-part history: [docs/HARDWARE.md](docs/HARDWARE.md).

---

## 11. Telnet IAC handling

The telnet path strips IAC (Interpret As Command) sequences before the menu
parser sees them. Without it, option bytes — e.g. `0x22`, which is `"` — leak
through as spurious menu input.

| State | Meaning |
|---|---|
| 0 | normal; `0xFF` → 1 |
| 1 | got IAC; WILL/WONT/DO/DONT → 2, SB → 3 |
| 2 | consume the option byte → 0 |
| 3 | in subnegotiation, skip until `0xFF` |
| 4 | IAC inside subneg; `0xF0` (SE) ends it → 0 |

---

## 12. Build ordering

```bash
./build.sh bitstream pac firmware     # after ANY gateware change
```

**The PAC must be regenerated between them.** `sw_rust/litex-pac/` is generated
from the SoC's SVD; changing `colorlight.py` without regenerating it builds
firmware against the *previous* register map, which fails in ways that look like
hardware faults.

`sw_rust/smoltcp-0.8.0/` is a patched fork — do not replace it with upstream.

---

## 13. The S-PWM output stage — the E320 and HUB320

The E320's HUB320 connectors carry **four RGB groups** each (twelve data lines),
and the modules they drive use **S-PWM** driver chips (DP3264S / ICN1065
family): the chip holds a whole frame of greyscale and generates its own PWM, so
the controller's job is to deliver each row's pixels before the chip needs them,
plus a per-frame command prefix. The protocol is in
[docs/ICN1065.md](docs/ICN1065.md), the connector in
[docs/HUB320.md](docs/HUB320.md). This section is the **data path**: how
pixels get from SDRAM to the pins fast enough for four modules.

**How the performance was found** — the measurements, step by step, with the
dead ends — is a case study of its own:
[docs/E320-PERFORMANCE.md](docs/E320-PERFORMANCE.md). This section is the
resulting architecture.

### 13.1 The shape

```
                SDRAM — one LiteDRAM crossbar port, mode=read, data returns in order
                          ▲ command (address)                  │ data
                          │                                     ▼
        Icn1065PipelinedArbiter — round robin, split transactions, tag FIFO,
                                  "sticky" for a whole run of same-row reads
             ▲               ▲               ▲               ▲
        PixelSource 0   PixelSource 1   PixelSource 2   PixelSource 3      pipelined + block fetch
             │               │               │               │
        LineBuffer 0    LineBuffer 1    LineBuffer 2    LineBuffer 3       3 row slots, 5 EBR each
             │               │               │               │
        Icn1065Output 0 Icn1065Output 1 Icn1065Output 2 Icn1065Output 3    serialiser, 12 data lines
             │               │               │               │
         J1 data         J2 data         J3 data         J4 data           HUB320, 4 RGB groups each

        shared: SpwmPrefix (frame prefix + driver registers), row scan, LAT, OE
                → the common block (CLK, LAT, OE, A–E), driven once, bused to every connector
```

**One prefix, one row scan, N data paths.** Every output drives the same kind
of module in lock-step, so the frame prefix (VSYNC, pre-activation, the
chip's configuration registers) and the row scan are generated once. The
common block is requested from J1's balls only — the measured connector table
shows the same eight balls on all eight ports, i.e. bused on the board — and
reaches J2..J8 for free. Proven on glass with modules on J3/J4 on 2026-10-26.

**Each output reads its own region.** An output's framebuffer origin is
`fb_base + panelN.y × 16 × width + panelN.x × 16`, from its placement CSR, which
the firmware writes from the layout (`J1: 0,0`, `J3: 0,2` …). Within a module,
each RGB group's strip starts at a `group<g>_origin` (P1.25: (128,0) (128,64)
(0,0) (0,64) — four 128×64 tiles in a 2×2).

### 13.2 The row budget — the number everything answers to

A row is `columns × 16` panel clocks (16 bits per pixel, every data line in
parallel), and the panel clock is `sys / clk_div`. For the P1.25 preset
(`columns` 128, scan 64) at `clk_div` 4:

```
row period     = 128 × 16 × 4                 =  8,192 sys clocks
reads per row  = outputs × groups × columns   =  4 × 4 × 128 = 2,048
frame          = 64 rows × 8,192 + prefix     →  66.7 Hz refresh at 35 MHz
```

**The line buffer for row r+1 must be full before the serialiser reaches it.**
The serialiser cannot wait — the driver chips clock on regardless — so a late
row is a wrong row on the glass. Everything in §13.3–13.5 exists to make 2,048
reads fit comfortably inside 8,192 clocks **while the pixel DMA is also writing
the next frame into the same SDRAM**.

Refresh is a picture-quality setting, not just a capacity one: at `clk_div` 6
(44 Hz) near-black regions visibly beat while bright content stays still —
short PWM pulses below flicker fusion. 66.7 Hz is the operating point; see
`POST /api/clkdiv` in [API.md](API.md).

### 13.3 The line buffer (`Icn1065LineBuffer`)

The first S-PWM path read pixels **on demand** with a hard 16-clock deadline per
pixel. That made the picture a function of memory latency — DRAM refresh, the
DMA's write bursts and the other outputs all moved it — and the result was
snow: 13.8 wrong pixels per second while streaming, none with nothing writing.
The line buffer fetches a row **ahead** into block RAM; the serialiser then
does a one-cycle read and nothing downstream of DRAM can starve it.

**Three row slots, not two** (`EXP_TRIPLE`). `pix_row` is not a plain counter:
with `sweep_adv` = 1 the serialiser reads the **next** row for the last 1/16 of
each row (the DP3264S commits data one sweep ahead — E320-BRINGUP §4.6a), so
row r+1 must already be resident while row r is still showing. With two slots
the fill of r+2 got a single row period; a third slot gives it **two**, which
absorbs the DMA's write bursts. The memory is 512 words deep in its block RAMs
regardless (`4 × columns`, slot = the top address bits), so the third slot is
free.

| Property | How |
|---|---|
| slot rotation | on the serialiser's **true** row counter `pix_row_base`, not `pix_row` — keying it on `pix_row` was the first attempt and was wrong |
| what fills | only a slot that does not already hold its row (`need1`/`need2`), then stop — an early version re-walked the same row continuously, tripled the read traffic and starved the DMA |
| `primed` | high when the slot being read holds the row being read; the underrun counter fires on a read while it is low |
| read port | `READ_FIRST`, plus `no_rw_check` on the memory |

**The `READ_FIRST` detail bought ~940 LUTs.** Migen's default read port
registers the *address*, which asks for write-first transparency, and yosys
builds that as a word-wide bypass mux: 176 LUTs per line buffer measured in
isolation, for a collision that cannot happen (the fill never writes the slot
being read). A `READ_FIRST` port registers the data instead, and
`mem.attr = {("no_rw_check", 1)}` tells yosys the collision is don't-care. The
attribute has to go on `mem.attr`: LiteX emits that tuple verbatim ahead of the
memory declaration, but it **ignores** a Memory subclass's own `emit_verilog`,
so the obvious subclass approach silently did nothing.

### 13.4 The reader (`Icn1065PixelSource`)

One pixel is four reads, one per RGB group, from four different strips. Three
generations, each fixing the previous one's bottleneck:

| Reader | Cost | Row fill, 4 outputs, `clk_div` 4 (budget 8,192) |
|---|---|---|
| sequential — each group's read waits for the previous one's data | `4L + 16` per pixel | does not fit (L ≈ 16 on LiteDRAM) |
| **pipelined** (`EXP_PIPELINE`) — the four commands go out back to back | `L + groups` per pixel | ~5,900 paused, **7,700–8,400 streaming** — over budget |
| **block fetch** (`EXP_BLOCK=4`) — K columns of one same-row group pair per run | 2K reads per open DRAM row | ~3,100 paused, **~2,650–3,450 streaming** |

**Why pipelining was not enough.** The pipelined reader got the SDRAM to ~0.35
reads per clock, which fits with the stream paused. With it running, each DMA
write cost ~2.6 read slots, and the fill went over budget. Buffering cannot fix
an *average* deficit — only more reads per clock can — and the limit was not
latency but **DRAM row switching**: four strips means four DRAM rows, and the
single in-order port pays a precharge + activate every time consecutive reads
land in different rows.

**Block fetch** reads K columns of the two groups that share a DRAM row, then K
of the other two, staging the first pair and publishing a column when its
second pair arrives. The arbiter holds the port for each run (`mem_last`), so a
run is 2K reads with one row open.

**The pairing is a property of the module, and getting it wrong was slower than
not blocking at all.** It was built pairing groups 0+1 and 2+3, and *simulated*
2.4× faster. On hardware it was 15% **slower**, because the P1.25's strips are
at (128,0) (128,64) (0,0) (0,64): it is groups **0 and 2** that share a
framebuffer row. The simulation had used an idealised layout. The pairing is
now derived at build time from the preset's `group_origins` (`_block_pairs()`
in `icn1065_soc.py`), while the addresses still come from the runtime offset
CSRs — so a different module layout is slower, never wrong.
`tools/sim_litedram.py` defaults to the real origins and, so set up,
reproduces the hardware within ~10%, mis-pairing included.

> **Simulate with the module's real geometry.** A model fed idealised inputs
> confirms the design you meant, not the one you built. Check the simulation's
> inputs before trusting its model.

### 13.5 The arbiter (`Icn1065PipelinedArbiter`)

Round-robin over the outputs onto **one** read port, with **split
transactions**: commands are issued as fast as the memory accepts them and the
source id rides in a tag FIFO; returning data — in order, which LiteDRAM's
native port guarantees — is matched to its source by popping the FIFO. A
blocking arbiter (`Icn1065Arbiter`, one read in flight) turns latency into a
throughput limit; this one does not.

| Parameter | Four-output build | Why |
|---|---|---|
| `per_source` | `4 × K` = 16 | how many reads one source may have in flight — one block run |
| `sticky` | on with block fetch | keep the grant until the source marks `last`, so a same-row run reaches the memory contiguously |
| tag width | `max(1, bits_for(n − 1))` | `bits_for(0)` is 0, and a one-output build used to fail Migen's memory assert on a zero-width tag |

LiteDRAM's crossbar does not let one port overlap banks, so everything the
display reads goes through this one port, in order; the speed comes from
keeping rows open, not from parallelism.

### 13.6 Fitting four outputs in an LFE5U-25F

Block RAM is the binding constraint, not logic. Each output costs **5 EBR** of
line buffer (`3 × groups × 13` grey bits = 156 bits wide); the rest of the SoC
is a fixed cost.

| | 3 outputs (stock) | **4 outputs** (`exp/build-out4.sh`) |
|---|---|---|
| line buffers | 15 EBR | 20 EBR |
| fixed cost | 41 (`burst_depth` 1536) | **35** (`burst_depth` 2048, trims below) |
| **total** | 56 / 56 | **55 / 56** |
| LUT4 | 88% | **94%** (22,969 / 24,288) |
| system clock (target 35 MHz) | 36.8 MHz | **44–50 MHz** |
| gigabit RX (target 125 MHz) | ~130 MHz | 128.3 MHz on the shipped seed |

The fixed cost came down with three trims, none of which touch the pixel path:

| Trim | Knob | Effect |
|---|---|---|
| BIOS scratch SRAM 8 KB → 4 KB | `EXP_SRAM=0x1000` | 2 EBR |
| BIOS built with `-ffunction-sections -fdata-sections`, LTO forced on, no `cmd_*.o` console commands | `exp/Dockerfile.out4` | LiteX auto-sizes the ROM to the BIOS image, so a smaller BIOS is fewer EBR; the trimmed one is `0x56E8` bytes (~22 KB). It keeps netboot (DHCP + TFTP); it loses only serial commands, and this board has no serial console |
| block-RAM accounting | `EXP_EBR_FIXED=35` | tells the build's own budget check what the trims achieved, so it lets a fourth output through; nextpnr remains the judge |

```bash
docker build -t litex-hub75:out4 -f exp/Dockerfile.out4 exp     # once, after the base image
exp/build-out4.sh                                                # gateware, SVD, PAC, firmware
SEED=<n> exp/build-out4.sh                                       # another placement seed
```

It produces the bitstream **and** the firmware from one tree, so their CSR maps
match. Seed 7 closes both clocks; seed 3 missed the RX clock by 0.4 MHz — the
gigabit RX clock-domain crossing is the chronically marginal path, so try
seeds before changing anything.

All three `EXP_*` reader knobs default **off** (`EXP_BLOCK` and `EXP_TRIPLE`
require `EXP_PIPELINE`), so any build without them is the shipped reader.

### 13.7 Timing closure came from two places nobody would look

The first four-output build (sequential reader) closed at 35.4–37.4 MHz but at
99.5% LUT; the block-fetch builds then closed at only 30–34 MHz against 35.
Neither fix was in the pixel logic:

1. **The bypass mux** in §13.3 — ~940 LUTs back across four line buffers.
2. **CSR multipliers behind the command acknowledge.** The group offsets are
   CSR *expressions* (`y × 16 × width + x × 16`), i.e. multipliers. In the block
   issue path yosys shared one multiplier between the two pairs by muxing its
   operands on the phase bit, which put the DSP — and the row product, which
   the FSM gated — behind the SDRAM command acknowledge. The offsets only
   change when firmware rewrites the CSRs, so they, the four address steps and
   the row product (`rowbase_r`, recomputed every cycle from registered inputs)
   are now registers, and the issue address is a register stepped by constants.
   fmax went from 30–34 MHz to 47–50.

> **When a design will not close, read the critical path before redesigning.**
> Both of these were a few registers; the pixel datapath itself had margin.

### 13.8 Instruments, and what they cannot see

| Field (`/api/status`) | Measures | Caveat |
|---|---|---|
| `underruns` | serialiser reads of a slot not yet holding its row (`~primed`) | **output 0 only**; reads **~121 after every boot** (the unprimed first rows) — measure deltas |
| `perf_fill_cycles` / `perf_fills` | clocks output 0's line buffer spent filling, and how many fills | **output 0 only**; the ratio is the average fill against the row budget |
| `perf_cmds` / `perf_stall` | read commands the display port accepted / clocks a command waited | all outputs |
| `xbar[0..5]` | commands accepted per SDRAM crossbar master: CPU L2, uncached bridge, **display**, `dmatest`, **pixel DMA**, unused | read twice and subtract |

Output 0 is representative because the round-robin arbiter keeps every output's
fill within one run of the others — but it is an argument, not a measurement.
The crossbar counters are what ruled out stray traffic: with the stream paused
the CPU's L2 issues ~0.1 M commands/s against the display's 8.7 M.

**What none of them can see** is a wrong pixel that arrived on time: the
wide-header MTU fault (§4) read zero on every counter here. When the glass shows
a **fixed, regular** pattern and every counter is clean, suspect the wire or the
addressing, not the memory.

### 13.9 What must agree

| Thing | Where | If it disagrees |
|---|---|---|
| outputs | `--outputs N` (bitstream) and `pub const OUTPUTS` in `hub75.rs` | `build.sh` refuses: a surplus connector is silently dropped by the layout parser, which looks like a dead cable |
| CSR map | the bitstream and the PAC the firmware was built against | rebuild PAC + firmware from the same tree (`exp/build-out4.sh` does); a stale map fails like hardware |
| reset address | the PAC (`CTRL_RESET_ADDR`, §10) | a CPU fault hangs instead of rebooting |
| chunk strides | `chunk_pixels` (gateware), `ppc()` (firmware), the sender | shearing — or, over the MTU, stale pixels (§4) |
| framebuffer map | `hub75.py`, `hub75.rs` | display and DMA address different halves |
| layout | `J1..J4` placements in `<mac>.yml` vs the wall's real stacking | modules swapped or showing black; a layout box drawn for a smaller wall leaves the extra modules black, which reads as a dead connector |
