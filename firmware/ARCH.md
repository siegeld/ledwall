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

Not in the picture above: the **second RJ45**. Every build drives its PHY as
the downstream port of a card chain, in gateware on the Ethernet clock and
with no CPU: everything unicast arriving on port 1 is repeated out of port 2,
and downstream cards' replies are merged back into port 1's transmit. A whole
chain is one IP address. §14.

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
| the card is a **chain member** (behind another card, §14) | the image in flash first — its DHCP never leaves the chain — and then the assigned image, which marquee has it fetch into RAM and start (`POST /api/netboot`, §14.4) |

**What runs, and when flash is written** (firmware 5.0.5 / marquee 0.45.0;
marquee `ARCH.md` §2c is the policy and its enforcement):

- marquee decides what runs, **per chain**: a member runs its head's image.
- Every card runs the assigned image whenever marquee can be reached -- a head
  by its BIOS netboot, a member by netbooting itself from its firmware
  (`src/chainload.rs`). **An update never writes flash.**
- **Flash is the survival copy**: what runs when a card boots and cannot reach
  marquee. marquee keeps it equal to the assigned image, writing it once a
  card has run that image cleanly for ten minutes and its flash header
  (length + crc32) differs. So every card of a chain comes up on the same
  version even with no server.
- Gateware has no netboot: a bitstream update writes flash, live at the next
  power cycle.

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
> the reorder. **Keep both flashed and deployed in step** -- since marquee
0.45.0 its firmware sync does that for every card (above) — otherwise a panel
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

The fork exists for two reasons: it exposes **DHCP option 66** as
`Config.tftp_server_name`, so a panel learns its boot server from DHCP; and
`Interface::fill_neighbor`, which lets a chain member (§14.3) learn a peer's
MAC from the peer's own frame, since a member does no ARP.

### Pixel packets never get here

Since v1.38.0 the gateware kills UDP pixel packets on the CPU branch before the
MAC raises a receive event (§1), so none of the filtering below ever sees one.
The numbers in this section are the *history* that led to the gateware fix, and
they still describe what happens to every other packet on the wire.

### Everything runs in the interrupt handler

Since v1.10.0 the whole network stack runs inside the external-interrupt
handler (`network_handler()` in `network.rs`). The main loop runs no network
code of its own; since firmware 5.0.3 it calls the same handler, interrupts
masked, when no packet has for `SOCKET_POLL_MS` (5 ms) -- see "Nothing wakes
the ISR on a streaming card" below.

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

The **main loop** does only: timer tracking, interrupt re-enable, the socket
deadline (`socket_tick()`), display refresh / program tick, the serial menu,
and MAC error counters.

### Nothing wakes the ISR on a streaming card

With the pixel filter on (gateware DMA), pixel packets never interrupt the
CPU, and Timer0's 1 Hz event does not either: `get_time_ms()` in the main loop
clears its pending bit long before the IRQ can fire. So until firmware 5.0.3
the handler ran only when some *other* packet arrived, and everything HTTP
does after a packet -- send the rest of a reply larger than the 2 KB TX buffer,
close, recycle the socket, listen again -- waited for the next unrelated one.
Measured on the E320 under a 20 fps stream, 150 status reads on 5.0.2: 4
refused, 2 timed out, p90 0.49 s; a FIN up to 0.9 s after its body; one request
ACKed and answered only when the client gave up 3 s later. Same gateware, same
probe, 300 reads on 5.0.3: none refused, none timed out, worst 24 ms.

Three things changed:

- **`socket_tick()`** -- the main loop, and the program renderer between rows
  (a full redraw is longer than 5 ms), run `network_handler` with interrupts
  masked once `SOCKET_POLL_MS` has passed. It reads the clock itself; nothing
  else moves `TIME_MS` during a render. The deadline pass also runs
  `iface.poll()`, so smoltcp's own timers (retransmits) keep time.
- **Sockets are recycled before smoltcp sees a SYN** (`http_recycle()` in the
  slow path and before every poll), not only afterwards; a socket whose
  connection had ended was otherwise still closed when the next one arrived,
  and the SYN got a RST.
- **Four HTTP sockets** (`HTTP_SOCKETS`, was 2; ~10.5 KB each), and a SYN that
  finds all of them busy is **dropped, not refused** (`http_syn_must_wait()` in
  `rx_drop()`, counted as `http_syn_deferred` in `/api/status`). smoltcp has
  no listen backlog and answers such a SYN with a RST, which clients report as
  "connection refused" and do not retry; a dropped SYN is retransmitted a
  second later. marquee's flash push failed outright that way.

What is left is packet loss, not refusal: with three clients each polling 20
times a second, about one connection in eight takes 0.2-1 s because a SYN,
request or ACK was dropped by the MAC (two receive slots, `nrxslots`, cut to
fit the E320) while the CPU was building a reply, and the client retransmitted.
Every request still completed. One client, or marquee's own polling, sees none.

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

`POST /api/values` into a 24-key table (`MAX_VALUES`) with per-key age and a staleness clock.
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

An S-PWM card's layout also carries the module's driver chip and operating
point: `drive:`, `chip_reg:`, `group_origins:`, `clk_div:`, `write_throttle:`
(and `wall:` / `origin:` for a card that is part of a larger wall). The full
key list is in the README, "Panel Layout"; `POST /api/layout` applies the same
text to a running card without storing it.

---

## 8. Boot sequence

1. **BIOS** runs from ROM, initialises SDRAM
2. **BIOS tries the network first**, then flash — `netboot()` fetches `boot.bin`
   when marquee is reachable, and falls through to `FLASH_BOOT_ADDRESS` in about
   0.32 s when it is not (§1). A cold power-on normally lands on flash, because
   the PHY has not negotiated yet
   A **chain member** (§14) always lands on flash: its BIOS DHCP is dropped by
   the card in front of it. Once running it is netbooted into the assigned
   image by marquee (`POST /api/netboot`, §14.4)
3. **Firmware starts** — reads the flash unique ID, derives a locally-
   administered MAC `02:xx:xx:xx:xx:xx`, brings up HUB75
4. **Interrupts** — installs the trap handler, enables ETHMAC IRQ #2
4b. **Pixel filter on** — firmware sets `pixel_filter_enable`, and from here the
   CPU takes no interrupt for pixel traffic. Deliberately done **here** and not
   defaulted on in the bitstream: the CSR resets to 0 on FPGA configuration, so
   the BIOS always netboots with the filter off and a power cycle always
   recovers a bad firmware
5. **DHCP** — acquires an address; falls back to a static one after 10 s. A
   chain member instead takes the head's IP and MAC from marquee's discovery
   frame (§14.3)
6. **TFTP config** — fetches `<mac>.yml` from DHCP option 66 (or a fallback), on
   port 6969; a chain member fetches it from the discovery's sender, from
   client port 20000+4h
7. **Layout applied** — parses it, configures panel CSRs, redraws at the new
   virtual size
8. **Mode applied** — `mode: program` starts the named program, falling back to
   stream if it is not in this firmware
9. **Main loop** — display refresh and program ticks; network in the ISR, plus
   `socket_tick()` when no packet has run it for 5 ms

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
| `gateware/smoleth.py` | LiteEth glue, the pixel classifier, `burst_fifo` and its instruments; `hw_udp_port`, `pixel_filter_span` |
| **`gateware/chain.py`** | card chains: the downstream forwarder, the upstream FIFO + arbiter, the MAC-facing shim (§14) |

### Firmware — `sw_rust/barsign_disp/src/`

| File | Purpose |
|---|---|
| `main.rs` | entry, ISR setup, main loop (display + program tick only) |
| `network.rs` | the ISR: packet dispatch, DHCP, TFTP, HTTP, telnet, program tick |
| `ethernet.rs` | smoltcp device driver, `peek_rx()`/`ack_rx()`, **MTU** |
| `bitmap_udp.rs` | wire formats, mode selection, palette staging, `bad_size` |
| `hub75.rs` | double-buffered framebuffer, `swap_buffers()`, buffer access |
| `http.rs` | HTTP/1.1 server, status page, REST API |
| `layout.rs` | the layout/boot-config parser (`LayoutConfig::parse`): grid, connectors, mode, and the S-PWM keys incl. `chip_reg:` |
| `spwm.rs`, `spwm_tables.rs` | S-PWM driver-chip setup: `configure()` writes a chip's table (with `chip_reg` overrides) over the CSRs; the tables are **generated** from `gateware/spwm_chips.py` (`./build.sh spwm-tables`) |
| `chainload.rs` | start a fetched firmware image from RAM (`POST /api/netboot`) |
| `tftp_config.rs` | TFTP client for `<mac>.yml` |
| **`program.rs`** | `Ctx`, `Program`, `RowFn`, the palette, `sin64`, `hash2` |
| **`prim.rs`** | the drawing library — borders, text, scroll, sprites, effects |
| **`sprite.rs`** | 4bpp coverage sprites, fonts, `cover_at`, `cover_scroll` |
| **`values.rs`** | the value table (24 keys, `MAX_VALUES`) and staleness clock |
| **`generated.rs`**, **`assets.rs`** | emitted by `panelc` — **do not edit** |
| `patterns.rs` | built-in test patterns |
| `breadcrumb.rs` | crash trail in uncached SDRAM, survives `soc_rst` |
| `flash_id.rs` | flash unique ID → MAC |
| **`chain.rs`** | card chains: role, hop, the port map, discovery parsing, head/member frame filters (§14) |
| `tftp_flash.rs` | OTA into flash: bitstream at 0x0, FBI firmware at 0x100000 |
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
| `tools/gen_spwm_tables.py` | `gateware/spwm_chips.py` → `spwm_tables.rs` (`./build.sh spwm-tables`) |
| `tools/decode_rcvbp.py`, `tools/rcvbp_import.py` | read a vendor's `.rcvbp` receiver profile; turn it into a marquee module type carrying its registers (`docs/PANELS.md`) |
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

**The chip is data, not logic.** The prefix's grammar and register payload
come from a table per driver chip (`gateware/spwm_chips.py`: `dp3264s`,
`icnd1065`, `dp3364s`). The bitstream wakes up with its preset's table; the
firmware rewrites it at boot from the layout's `drive:` name
(`spwm::configure`), laying any `chip_reg:` lines from the boot config over it
by register address (firmware 5.6.0) -- so a module whose vendor tunes the
registers differently is a config change, not a build. Two things a chip can
need from the scan-out itself are CSR fields with per-chip reset values: the
wide-ROW-pulse cadence (`latch_cfg.passes`) and `latch_cfg.vsync_align`, which
starts every data frame on a scan-pass boundary for a chip that resets its
line counter on VSYNC (the ICND1065L; gated in both the IDLE and HOLD states
of the output FSM since gateware 3.4.1).

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

**A module with no same-row pair gets `block_seq`** (gateware 3.5.0). Myled's
P1.25 stacks its four strips, so no two groups share a framebuffer row and the
interleaved pair order changed DRAM row on every read: ~7,800 clocks a row,
and underruns whenever frames arrived. When `_block_pairs()` finds no pair, the
reader issues each group as its own run of K columns -- the same commands in a
different order, one row change per K reads. Measured on that module: row fill
7,830 → 3,905 clocks (`tools/sim_fillrate.py` with `SEQ=1`). The choice is made
at build time from the preset's origins, which is why that module has its own
preset and bitstream (`128x256-icnd1065l-hub320`); ARGO's is unchanged.

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
| module shape | the preset's `group_origins` (they fix the row-fetch order at build time) vs the module on the connector | not wrong, slow: Myled's stacked module on ARGO's 2x2 bitstream filled a row in ~7,800 of 8,192 clocks and flickered under a stream. `module_mismatch` in `/api/status` flags a size disagreement |
| chip table | `drive:` (and any `chip_reg:` lines) in the layout vs the driver chip on the module | dark panel, or a rolling one; `spwm_configured` / `chip_regs_matched` in `/api/status` say what was written |
| layout | `J1..J4` placements in `<mac>.yml` vs the wall's real stacking | modules swapped or showing black; a layout box drawn for a smaller wall leaves the extra modules black, which reads as a dead connector |

## 14. Card chains — one IP, many cards

Every card has two RJ45s. **Port 1** (the gateware's `eth 0`, `ethphy`) faces
the switch; **port 2** (`eth 1`, `ethphy1`) feeds the next card. A whole chain
is **one IP address**, the head's. Same bitstream and firmware on every card;
the role is decided at runtime, and a card on its own is simply the head of a
chain of one -- nothing about it changes. Gateware `gateware/chain.py`,
firmware `src/chain.rs`, simulation `./build.sh sim-chain`; marquee's side is
its `ARCH.md` §2b.

> **E320: port 1 (upstream) is the jack marked `eth0`, port 2 (downstream) is
> `eth1`.** The wall card has run on `eth0` all along, and it is the only jack a
> card reaches the network through (port 2 only forwards). Facing the
> connectors, `eth0` is the LEFT jack, so a chain is cabled left-in, right-out:
> the sender to card 1's left jack, card 1's right jack to card 2's left jack.
> The pins are certain (rev 8.2 `eth 0` / `eth 1`; on the E320 confirmed from
> the factory image). **5A-75E: not yet read off.** `chain.port1_link` in
> `/api/status` says when the DOWNSTREAM jack has a cable.

```
 marquee ── switch ── [port1  HEAD  port2] ── [port1  hop 1  port2] ── [port1  hop 2 ...
                         <ip> :80  :7000           :8001  :7001             :8002  :7002
```

| | hop 0 (head / lone card) | hop h >= 1 (member) |
|---|---|---|
| pixels (UDP) | 7000 | 7000 + h |
| web UI + API (TCP) | 80 | 8000 + h |
| config TFTP client | 6900 | 20000 + 4h |
| bitstream / firmware TFTP client | 6901 | 20000 + 4h + 1 |
| boots | BIOS netboots the assigned image; flash is the survival copy | its flash copy, then **netbooted** by marquee (`POST /api/netboot`, §14.4) |

### 14.1 Downstream: port 1 RX → port 2 TX (`ChainForwarder`)

Every UNICAST frame, unmodified, in gateware. Port 2 **transmits on port 1's
receive clock** (`tx_clk=` on its `LiteEthPHYRGMII`), so this is a same-clock
pass-through: no CDC FIFO, an input register, an 11-byte delay line and an
output register -- thirteen cycles, 104 ns a hop plus PHY latency -- and no CPU. Every card therefore sees every
unicast frame its head sees.

**Group-addressed frames are not forwarded.** A member needs none (discovery,
pixels, TFTP and TCP are all unicast to the head), and repeating them made a
mis-cabled loop -- port 2 back into the switch -- a broadcast storm, and
handed every member the segment's whole multicast load (the bench card had
dropped 140 million). The verdict comes at the first byte after the SFD,
8 bytes into the frame behind a 7-byte preamble; the delay line is 11 so the
frame's first preamble byte is still waiting when it lands, and a dropped
frame never starts on the wire at all (`fwd_dropped` counts them).

One exception: marquee's **discovery** frame (IPv4 UDP to 6990, not a
fragment, payload `"CH"` at byte 42). The forwarder adds one to the hop byte
(44) and subtracts one from the anti-hop byte (46) -- AT THE OUTPUT, only once
the input has shown the frame reaches byte 51 (a 52-byte frame, FCS included)
and its anti byte is 255 - hop,
so a short or malformed look-alike passes through untouched; and it gives the
frame a fresh FCS only if the frame ARRIVED intact (a second CRC engine checks
the incoming FCS), so an error is never laundered into a valid frame. Both are high bytes of 16-bit words, so the UDP checksum
is unchanged; the Ethernet FCS is regenerated from the bare `LiteEthMACCRCEngine`
(`LiteEthMACCRC32` in the pinned LiteEth never drives its `value` -- the first
version emitted a constant FCS and `sim-chain` caught it). Every other frame
keeps its ORIGINAL FCS, so a bit error on a hop is still an error at the next
card rather than being blessed with a fresh CRC.

### 14.2 Upstream: port 2 RX → port 1 TX (`ChainUpstream`)

Downstream cards' replies, merged with this card's own MAC TX at frame
boundaries through one 2048x9 block RAM.

- **Group-addressed frames are dropped** at the first byte, before anything is
  written. That is why a member never gets an IP: its DHCP discover is a
  broadcast and never leaves the chain. (Its BIOS's netboot fails the same way
  and falls through to `flashboot()`, §1b.)
- **Cut-through, not store-and-forward.** Store-and-forward was the first
  version and `sim-chain` killed it: a 1518-byte frame holds its space until it
  has entirely LEFT, so the next back-to-back frame -- the second segment of a
  member's web page -- failed admission. Both clocks of a hop are 125 MHz
  within 200 ppm (0.3 bytes over a frame), so the reader starts once 32 bytes
  are in. The FIFO only fills while this card's CPU holds port 1.
- **A frame is delivered whole or cut short with a bad FCS** -- never altered.
  If the FIFO fills, the frame is ended early with a terminator; the next
  receiver drops it.
- The arbiter alternates between the MAC and the FIFO when both wait, and puts
  12 idle cycles after every frame from either.

**The reader resynchronises to the writer (3.0.5).** The write side resets
with either PHY; the read side with port 0's TX only, because it also carries
the CPU's own frames to the switch. Tying it to port 2's reset held it in reset
-- every CPU frame silently lost -- on a card whose port 2 RX clock never runs:
a reset synchroniser only clears on clock edges (`sim-chain-deep` case 16).
Instead the reader watches the writer's synchronised reset, which asserts
asynchronously and so is visible with port 2's clock stopped, and whenever it
sees one it ADOPTS the writer's pointers (the FIFO looks empty) in IDLE or COOL,
latched until that has happened. Two failures this replaces: a head reboot
with port 2's clock stopped -- both PHYs share one reset pin on rev 8.x --
replayed stale member frames with valid FCS (case 15); and a write-side-only
reset arriving mid-frame let the first version of the fix clear itself before
the reader was back in IDLE, replaying 91 frames (case 13). Before 3.0.4 the
two sides simply shared one reset, which replayed up to 255 frames whenever
only one of them took it.
After a member frame the port goes to the CPU only if a CPU frame is actually
WAITING (`tx_waiting`); otherwise the next member frame follows the 12-cycle
gap directly, which is what sustains line rate.

**The MAC is never stalled at 125 MHz.** Port 1's TX `ready` toward LiteEth
stays the constant 1 the PHY always gave it. Before a member's frame goes out,
the arbiter raises `hold`; SmolEth (sys domain, `tx_hold`/`tx_held`) keeps the
CPU's NEXT frame back at a frame boundary and answers `held`; once held and the
MAC stream has been idle 32 cycles (longer than the TX CDC FIFO's latency), the
member's frame goes, followed by a 12-cycle gap and, if a CPU frame is
waiting, 64 cycles with `hold` low so it gets through. `held` must have stood
for 96 cycles first: a CPU frame committed just before the hold took effect is
still inside LiteEth's TX pipeline, where `quiet` cannot yet see it.

**Timing.** Port 1's RX clock (= its TX clock) carries the forwarder and the
arbiter, and every decision there is REGISTERED, each for a measured reason:
a stallable MAC `ready` put LiteEth's TX CDC read logic on the critical path
(120.0 MHz on the 5A-75E with a buffer, 115 MHz on the E320 -- hence the
hold/held scheme above); registered `empty`/`avail` (the FIFO level
subtraction: 111.8 MHz); end-of-frame acted on a cycle late from `last_r` (the
BRAM's unregistered 5.8 ns clock-to-out: 94 MHz on the E320); the gray decode
as a per-bit XOR tree (the textbook ripple: 8.6 ns against 8.0); a registered
arbiter output into the ODDRs.

**Headroom (3.0.6).** On the E320 the 125 MHz clock still closed on only 1
seed in 12 with 3.0.5, and reading the worst path of EVERY failing seed --
not just one -- showed four different paths taking turns, each 8.0-8.6 ns
against 8.0, three of them ours. Fixing one would only have handed the miss to
the next, so all four were cut:

- the upstream FIFO's block RAM output went through the arbiter's mux into the
  output register (5.8 ns of clock-to-out before any logic). Now `dq` takes it
  with nothing in between and every other source gets a matching stage:
  `UP_TX_LATENCY` = 2, one cycle (8 ns) more on everything port 1 sends;
- its read address was `stall` -> `last_r` -> the FSM -> `rptr + 1`. Now it is
  a choice between two registers (`rptr`, `rptr_p1`) on `adv`, which is built
  from registers only; `last_r` is the stored flag of the byte sent last
  cycle, read from `dq`;
- the forwarder's discovery match (index decode, byte compare, the AND of ten
  checks, then the hop/anti logic) was eight LUTs deep. Stage 0 now only
  registers one flag per check; stage 1 combines them a cycle later, and the
  hop/anti tags join the delay line one entry in. The data path and its 13
  cycles are unchanged;
- LiteEth's clock-crossing FIFO -- not LiteEth's code but migen's
  `AsyncFIFOBuffered` under it -- read its RAM at `consume.q_next_binary`, so
  `readable` -> `re` -> +1 -> address -> LUT-RAM -> register was one cycle.
  `gateware/cdcfifo.py` reads at a registered address; SmolEth builds the MAC
  core inside `fast_cdc()`, which substitutes it for that one call only.
  Tested on its own against migen's FIFO as the control
  (`./build.sh sim-cdcfifo`).

### 14.3 Discovery and roles

marquee sends `"CH" hop=0 ver=2 anti=255 0 clock(4)` to UDP 6990 of every card
host once a second, unicast to the head. The frame a member receives therefore carries
its own hop, the head's MAC/IP (destination) and marquee's (source).

- **hop 0** → head. Nothing changes; it drops frames for members' ports before
  smoltcp (`chain::for_a_member`), because smoltcp answers a port it does not
  own with RST / ICMP port-unreachable, which would kill the member's
  connection from the far end.
- **hop h ≥ 1** → member (slave mode): takes the head's MAC and IP (prefix 0, no
  routes, no ARP), writes them into the hardware pixel filter, sets
  `hw_udp_port` to 7000 + h, moves its sockets to its hop's ports, and fetches
  **its own** `<mac>.yml` from the discovery's source -- the config is passed
  down the chain at boot without the head storing or relaying anything. It
  then accepts ONLY frames to its own ports (`chain::member_accepts`); the
  head answers ARP and ICMP for the whole chain. Peers are learned from their
  own frames -- every IPv4 frame, inside smoltcp (`learn_peers`, a local
  patch; learning only the first frame of each batch left a SYN second in a
  batch without a SYN-ACK) -- and for an off-subnet browser that maps the
  browser's IP to the router's MAC, which is exactly where the reply must go.
  A failed config fetch is retried: the TFTP client retransmits a lost RRQ or
  ACK, and a member still without its config restarts the fetch every 10 s off
  the discoveries (it has no DHCP path to retry from).
- **The filters run where smoltcp gets its frames** (`network::rx_drop`, called
  from `Eth::receive()`), not only in the ISR's peek loop. `iface.poll()` pulls
  every pending frame through `receive()`, so a filter applied only to the
  peeked frame missed the rest of the batch: found on the bench, where a head
  answered a member's port with a RST and swallowed one discovery in three.
  The same bypass had been letting multicast, unwanted UDP and foreign ARP
  reach smoltcp on every card since long before chains.
- **Any change of position takes three in a row** -- head, hop or head MAC,
  whatever the card is now (member, head or still undecided): a switch
  flooding a unicast discovery meant for another chain's head must not make a
  card take that identity, and a card booting during such a flood used to
  adopt the first one it heard.
- **The firmware accepts only discoveries the gateware would have rewritten**
  (anti-hop matches, hop < 255, not an IP fragment) and only a UNICAST head
  and server: a broadcast "head" was adopted as the card's own MAC, smoltcp
  panicked on it, and one such packet rebooted every lone card on the LAN.
- **A member that hears hop 0 reboots** (its cable moved to the switch): undoing
  a borrowed MAC, IP, ports and a stopped DHCP piecemeal is where the bugs would
  be, and a reboot comes back as an ordinary card.
- **Every card kills all 64 pixel ports on its CPU branch**
  (`pixel_filter_span`), because every card's CPU sees every hop's pixels.

### 14.4 Updating, browsing, and bandwidth

- **A member starts from flash and is then netbooted** (5.0.5): marquee sends
  `POST /api/netboot {"file": "<image>.fbi"}`; the card receives the image into
  the framebuffer's back half exactly as below, checks its length and crc32,
  then stops the pixel DMA and filter (anything that writes SDRAM), masks
  interrupts, copies a 72-byte routine into the on-chip SRAM and jumps to it;
  the routine copies the image to `0x40000000`, flushes the instruction cache
  (`fence.i`; this VexRiscv has no data cache and the L2 serves both buses) and
  jumps -- the BIOS's own hand-off (`src/chainload.rs`). Flash is not touched,
  and a failed fetch leaves the card on what it ran. Measured on the E320
  member, 3.0.7: 8 s from the request to the new image answering, 594 KB.
- **The survival copy** of a card's firmware is written with
  `POST /api/flash/firmware`, an FBI image into the copy at `0x100000` --
  by marquee's firmware sync, never as part of an update (marquee §2c).
  It is received WHOLE into the framebuffer's back half first (1.25 MB; stream
  held, program paused once the main loop confirms no render is in progress,
  the front frame stays lit), its length and crc32 checked THERE, and only then
  is flash erased and written from RAM a sector per poll and read back. Written
  as it arrived (5.0.0), any failure after the first 4 KB -- a TFTP timeout,
  the server restarting, a bad CRC at the end -- had already destroyed the old
  image, and a member cannot netboot its way out. Now every network failure
  leaves flash untouched (`failed: image length or CRC wrong; flash not
  touched`); what remains is a power cut during the seconds of local write.
  A member fetches from the server that discovered it -- the only one it can
  reach, since it cannot ARP.
  Bitstreams go to flash on every card anyway. A member left with no bootable
  firmware is recovered by cabling it straight to the switch, where it netboots.
- **Browsing**: member h's own page is `http://<head>:8000+h/`. A head's page
  walks its members from the browser (`fetch` to :8001, :8002 …; the JSON APIs
  send `Access-Control-Allow-Origin: *`). Opened through marquee's controller
  relay it cannot -- the probes would go to the relay host -- so it says so, and
  marquee's Cards page does the walk server-side (`/cards/{id}/chain/scan`).
- **Bandwidth**: one gigabit link carries the whole chain, ~38 Mpx/s of RGB888
  after headers -- four to five 256x512 E320 cards at 60 fps, more at 20.

### 14.5 Programs across a wall: each card draws its part, frame-synced

An on-card PROGRAM (§6) on a multi-card wall runs IN FULL on every card, and
each card displays only its own rectangle:

- **Geometry.** The boot config carries `wall: WxH` and `origin: x,y` (marquee
  writes them when the wall is bigger than the card). `program_tick` hands the
  program `ctx.w/h` = the WALL, and asks it for rows `y + origin_y` and columns
  from `origin_x` -- generated programs are unchanged, and a ticker laid out
  across 512 pixels scrolls across both 256-pixel cards. Absent, the wall is the
  card at 0,0: exactly the old behaviour.
- **One clock.** marquee's discovery frame (version 2) carries its millisecond
  clock at payload bytes 6..10; every card of a wall receives it within a
  fraction of a millisecond (104 ns a hop in a chain). `CLOCK_OFFSET` re-anchors
  the local clock to it, and `ctx.frame` / `ctx.time_ms` come from the SHARED
  clock, not from frames drawn -- so two cards agree on which frame it is, and
  a skipped tick moves content by the time that passed (the scroll shift is
  `speed x frames advanced`; a shift wider than the card redraws the band).
- **Frame sync.** Ticks land on shared 33 ms boundaries, and a frame is
  RENDERED ONE PERIOD AHEAD and swapped in at the next boundary
  (`PROGRAM_SWAP_PENDING`, `program_swap`). Every card swaps at the same
  instant whatever its own render took; swapping at the end of the render put a
  few milliseconds between neighbours, a visible step in a ticker crossing the
  seam. The cost is one frame of latency.
- **One program per wall.** marquee applies a mode/program change to every
  card of the wall: one picture, one program.

This works for cards on separate switch ports too (discovery reaches each
within microseconds of the others); a chain only makes it tighter.

### 14.6 Status

**Tested** (2026-11-08):

- **Simulation**: `sim-chain` (forwarder: rewrite, FCS, checksum, group frames
  dropped whole; upstream: merge, broadcast drop, overload, MAC never stalled),
  `sim-chain-deep` (17 adversarial cases: forwarders in series, minimum IFG and
  short preambles, near-miss and malformed discoveries, runts and jumbos, a
  300-frame fuzz against a reference model, 200 ppm drift both ways, sustained
  line rate, CPU fairness and a stuck CPU, the TX-pipeline latency sweep,
  group addressing, overlong frames, IFG on every transition, resets mid-frame
  -- it found five defects, all fixed),
  the chain cases of `sim-classifier`, and `test-chain-rs` (the firmware's
  discovery parsing, port map, head/member filters, on the host).
- **On the bench, one 5A-75E, no chain cabled** (`tools/chain_lab_test.py`):
  chain bitstream + firmware boot and netboot; as a head: DHCP, config, API,
  CORS, stream through the hardware DMA (0 partial, 0 dropped), program mode,
  silent on members' ports; the shared clock follows discovery and wraps past
  2^32; faked into member mode (a hop-1 discovery addressed to itself): answers
  only on 8000+h/7000+h, silent on :80 and other hops, its forwarder rewrote
  every discovery (`fwd_rewrites`), re-hops, boots straight into member mode
  and fetches its config through the chain port block, writes firmware to
  flash over TFTP and then BOOTS IT with netboot blocked, and reboots to a head
  when it hears hop 0; a program across a wall is pixel-exact (same wall, two
  origins, the overlapping columns/rows identical in the framebuffer).
  Repeated in full on gateware 3.0.0 / firmware 5.0.0, plus: group frames not
  forwarded (`fwd_dropped` counts the segment's broadcasts), and the flash
  firmware write REFUSES a truncated image and a one-bit-flipped one (read-back
  crc32) while a good one verifies and then boots with netboot blocked.
- **Repeated on gateware 3.0.5 / firmware 5.0.1** (the fourth sweep's fixes),
  2026-11-09: the head suite; a hop-1 discovery BROADCAST from the LAN (which
  rebooted every lone card before) is ignored, card up throughout;
  `Access-Control-Allow-Origin` on `/api/status` only; member at hop 1 and 3,
  the shared clock and its wrap, and a member that hears hop 5 / 3 / 5 / 5
  stays at hop 3 (three in a row, not a majority); unmember reboots. Flash
  firmware, staged in RAM: a truncated and a bit-flipped image each arrive
  WHOLE (577,888 / 578,888 bytes), are refused, and leave the flash sector
  CRC unchanged -- 0 sectors written; a good one writes 142 sectors and the
  region's crc32 equals the image's; the same through a member's ports (8001,
  TFTP client 20005); the program keeps running and resumes. The bench found
  two defects of its own here, both fixed and re-run: a member fetched from
  its netboot server rather than its discoverer (which it cannot ARP), and a
  program frame mid-render when the update began wrote into the staged image
  -- the RAM CRC caught it with flash untouched; the transfer now waits for the
  main loop to confirm no render is in progress. Booted from flash with
  netboot blocked (the TFTP log shows `<mac>.yml` and no `boot.bin`), then six
  alternating flash/net reboot cycles watched 90 s each: no misses.
  **One unexplained hang**: the first flash boot of this build went silent
  ~10 s after its config arrived (no ARP from two hosts, no reboot, no crash
  marker -- the one read after the power cut was SDRAM residue) and has not
  recurred in seven boots since. Not attributed; watch for it.
- **Not yet** (as of 2026-11-09): anything that needs a second card on port 2.
  Done 2026-11-13, below.

- **On the E320 P1.25 wall** (four outputs, gateware 3.0.3 seed 8, firmware
  5.0.0, then reverted to the released 1.46.0 / 3.9.0): boots, S-PWM configured,
  the head suite and the shared clock pass, member mode (8001/7001, silent on
  :80) and back, and the live 256x256 stream is indistinguishable from the
  released build over 30 s -- ~600 frames, 0 underruns, 0 dropped chunks, 0
  partial or dropped frames on both.

**Fit**: 5A-75E six outputs ~76% LUT, timing clean. E320 four outputs 56/56
EBR, 97-99% LUT, built with `EXP_CHAIN_COUNTERS=0`. Up to 3.0.5 port-1 RX
timing closed only on some seeds (seed 8 of six on 3.0.3; seeds 3 and 10 of six on 3.0.4 -- seed
10 at 130.1 / 133.5 / 48.8 MHz; seed 4 of TWELVE on 3.0.5, 127.4 / 141.3 /
48.6 MHz, the misses 113.5-124.8; artifacts kept). That was first written
up as "LiteEth's TX CDC FIFO, not chain logic" from ONE seed's report; the
worst path of every failing seed, read afterwards, was four different paths,
three of them chain logic -- see "Headroom" in §14.2. 3.0.5 seed 4 with firmware 5.0.1 ran on the P1.25 wall: S-PWM
configured, the head suite passes, and a 30 s stream matched the released
build -- 600 frames at 20 fps, 2 ms jitter, 0 dropped chunks, 0 partial --
before the wall went back to 1.46.0 / 3.9.0. The bitstream's ident carries a build
timestamp, so a passing seed does NOT reproduce -- sweep, and keep the
artifact from the run that passed.

**3.0.6** (the headroom work, §14.2) **closes on 11 of 12 seeds**: slowest
pass 132.2 MHz, best 140.2 / 148.4 / 48.4 (seed 3, kept); the miss, seed 1 at
120.2, is LiteEth's RX width converter, mostly routing. 3.0.6 with firmware
5.0.1 and then 5.0.2 ran on the P1.25 wall, 2026-11-09: S-PWM configured, the
head suite passes, 601-612 frames per 30 s at exactly 135 chunks a frame, 0
dropped, 0 CRC errors, against 603 on the released build, then back to 1.46.0
/ 3.9.0. One boot of 3.0.6 counted 79 dropped chunks in its first 30 s and
none after; the next boot of 3.0.6 counted 0, as did 3.0.5 and 1.46.0 --
not reproduced. The bench 5A-75E on 3.0.6 (139.0 / 140.5 / 50.4 MHz) passed
the head, member, re-hop, clock and unmember suites and a 30 s stream at 34
chunks a frame with none lost; its `mac_overflow` rate on the busy lab LAN
was 834 per 30 s, against 734 for 3.0.5 on the same card and stream.

**Two cabled cards** (2026-11-13; E320s, the wall card as head and a new card,
provisioned by JTAG, at hop 1 -- the wall card's `eth1` to the new card's
`eth0`; firmware 5.0.3 -> 5.0.5, gateware 3.0.6 -> 3.0.7):

- **Discovery and roles through a real forwarder**: the new card heard hop 1,
  took the head's MAC and IP, and answered on `<head>:8001` / pixels 7001
  while the head stayed on :80; its downstream jack reported no link, the
  head's 1000 Mb/s. marquee's chain scan found it at hop 1 by MAC, and its own
  web page loads through marquee's controller relay with every control acting
  on the member, not the head.
- **Streaming to both** through the head's link, each card's counters moving.
- **Firmware** 5.0.4 by rollout to both (head netbooted, member written to
  flash and rebooted from it -- the pre-5.0.5 path); then the **member netboot**
  (`POST /api/netboot`): from a 5.0.5 flash copy into 5.0.4 in RAM in 8 s, a
  reboot back to 5.0.5 from flash in 30 s (the BIOS netboot attempt timing
  out), flash untouched throughout. Driven by marquee 0.45.1: assigning the
  head brought the chain over in both directions, and its firmware sync
  netbooted a member left on the wrong image by itself. One member netboot
  started 0.7 s after its head rebooted stalled at block 62 -- the member
  accepted none of the server's three sends of it -- and is not explained;
  marquee now waits 15 s after a head is back before netbooting its members.
- **Bitstream** 3.0.7 written to both cards' flash over the chain (142 sectors
  each, ~25 s), live on both after ONE power cycle, both answering ~21 s later.
- Not yet on two cards: frame sync of a program across them (§14.5) and a
  wall laid out across both.

**3.0.7** (a soc.h constant, `CHAIN_COUNTERS`; logic unchanged) closes on **5
of 6 seeds** (seed 2 missed at 119.0 MHz); released seed 7, 137.6 / 145.6 /
49.4 MHz. E320 only: the 5A-75E stays on 3.0.6 / 5.0.3. The four-output build
compiles the chain counters out, and since firmware 5.0.4 the card reports
them as `null` ("not counted by this gateware") instead of 0.
