# Architecture

How the SoC, the gateware and the firmware fit together.

For **what we discovered about the board itself** — the flash part, the missing
D-cache, the JTAG traps — see [docs/HARDWARE.md](docs/HARDWARE.md).
For **numbers**, see [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

---

## 1. The shape of it

```
┌───────────────────────────────────────────────────────────────────┐
│                     Colorlight 5A-75E rev 8.2                     │
│                                                                   │
│   ┌──────────┐    ┌──────────┐    ┌──────────────┐  ┌──────────┐  │
│   │ VexRiscv │    │  LiteEth │    │ Hub75UdpDma  │  │  HUB75   │  │
│   │  "lite"  │    │   MAC    │───▶│  (gateware)  │  │  driver  │  │
│   │  40 MHz  │◀───┤ 8 rx / 2 │    │ pixels->SDRAM│  │ 6 × 2    │  │
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

UDP port 7000. One 10-byte header `<2sHBBHH>`, three formats told apart by the
magic:

| Magic | Payload | Pixels/chunk | Handled by |
|---|---|---|---|
| `'B','M'` | RGB888, 3 B/px | 487 | `Hub75UdpDma` (gateware) |
| `'B','I'` | indexed, 1 B/px | **1461** | `Hub75UdpDma` (gateware) |
| `'B','P'` | palette, RGB triples from byte 4 | 256 entries in one packet | CPU |

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

The HUB75 gateware has an `fb_base` CSR (20-bit, at `HUB75 + 0x04`) selecting
which SDRAM region the scan-out DMA reads. The firmware splits the framebuffer
area in half:

| | Word offset | Byte address |
|---|---|---|
| Buffer 0 | `0x80000` | `0x90200000` |
| Buffer 1 | `0x90000` | `0x90240000` |

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
| `gateware/colorlight.py` | the LiteX SoC: peripherals, memory map, clocking |
| `gateware/hub75.py` | HUB75 scan-out driver, `fb_base`, panel position CSRs |
| `gateware/dma_writer.py` | `Hub75UdpDma` — UDP sink straight to SDRAM |
| `gateware/smoleth.py` | LiteEth glue |

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
| SRAM | `0x10000000` | 8 KB — stack/heap |
| Main RAM | `0x40000000` | 4 MB — SDRAM, firmware runs here |
| EthMAC | `0x80000000` | 20 KB — 8 RX + 2 TX slots × 2 KB |
| SPI Flash | `0x80400000` | 4 MB — memory-mapped |
| Flash boot | `0x80500000` | firmware load address (chip offset `0x100000`) |
| CSR | `0xF0000000` | 64 KB |

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
