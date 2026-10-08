# HTTP API

Every panel serves a status page and a JSON API on **port 80**. No auth — this
is a device on a trusted segment, and it is the only interface it has.

All examples use `<panel>` for the board's address. Responses below are real,
captured from a running panel.

| | |
|---|---|
| **Look at** | [`/api/status`](#get-apistatus) · [`/api/display`](#get-apidisplay) · [`/api/layout`](#get-apilayout) · [`/api/bitmap/stats`](#get-apibitmapstats) · [`/api/fb`](#get-apifb) |
| **Programs** | [`/api/programs`](#get-apiprograms) · [`/api/program/on`](#post-apiprogramon--apiprogramoff) · [`/api/values`](#get-apivalues) |
| **Control** | [`/api/dma/on`](#post-apidmaon--apidmaoff) · [`/api/display/pattern`](#post-apidisplaypattern) · [`/api/display/hold/on`](#post-apidisplayholdon--off) · [`/api/reboot`](#post-apireboot) |

> ### Almost everything here reports *intent*, not reality
>
> `/api/display` reports the mode that was **requested**. `frames_completed`
> counts what the **CPU** saw. Both can read perfectly healthy while the wall is
> wrong — indexed mode once shipped broken for an entire release with
> `bad_magic` at 0, chunks counting and frames completing.
>
> **[`/api/fb`](#get-apifb) is the exception.** It dumps the front buffer: what
> is actually being scanned out. Reach for it when the glass disagrees with
> everything else.

---

## Reading state

### `GET /api/status`

Everything at once: identity, geometry, the interrupt path, MAC counters, crash
breadcrumbs.

```console
$ curl http://<panel>/api/status
```
```json
{
  "mac": "02:00:5e:00:53:01", "ip": "192.168.1.50",
  "hw_columns": 128, "hw_rows": 64, "hw_scan": 32,
  "hw_chain_length": 2, "hw_outputs": 6,
  "display_width": 128, "display_height": 128,
  "grid": "1x2", "virtual_width": 128, "virtual_height": 128,
  "panel_width": 128, "panel_height": 64,
  "animation": "none", "bitmap_frames": 0,
  "isr_count": 35496, "isr_driven": true,
  "fast_path": 0, "slow_path": 177, "max_batch": 11,
  "slow_arp": 7, "slow_tcp": 167, "slow_udp": 3, "slow_other": 0,
  "mcast_dropped": 44895, "arp_dropped": 450,
  "refresh_count": 82040,
  "dma_enabled": 0, "dma_pixels": 0, "dma_chunks": 0, "dma_bad_magic": 0,
  "mac_overflow": 121, "mac_crc_errors": 0, "mac_preamble_errors": 0,
  "prev_mark": 1, "prev_mcause": "...", "prev_mepc": "..."
}
```

The fields that answer real questions:

| Field | Read it when |
|---|---|
| `hw_*` | you want the **bitstream's** geometry — what the FPGA was built for |
| `display_*`, `virtual_*`, `grid` | you want the **runtime layout** from the TFTP config |
| **`dma_enabled`** | **a stream shows a frozen picture.** `0` means the CPU pixel path |
| `mac_overflow` | packets arrived faster than the ISR drained the FIFO |
| `slow_path`, `slow_arp` | network noise is stealing time from the pixel path |
| `mcast_dropped`, `arp_dropped` | the fast-path filters are working |
| `max_batch` | packets handled in one ISR entry; capped at 64 |
| `prev_mark`, `prev_mcause`, `prev_mepc` | **the board rebooted** — see below |

> **`prev_*` are crash breadcrumbs**, written to uncached SDRAM that survives
> `soc_rst`. After an unexplained reboot they say how far execution got and why.
> This found a bug that bisection had failed on outright — there is no serial
> console on this board, so they may be all you get.

### `GET /api/display`

What is on the glass, and where it is coming from.

```json
{"width":128,"height":128,"mode":"indexed","animation":"none",
 "source":"program (on-panel)","hold":true}
```

| Field | Values |
|---|---|
| `mode` | `full` \| `indexed` — the **output** mode |
| `source` | `stream` \| `program (on-panel)` \| `pattern` \| `animation` |
| `hold` | the stream is held (both the CPU parser **and** the gateware DMA) |

> **"The whole panel is one colour but I can see the shapes"** is this endpoint's
> question. `mode: indexed` with full-colour content renders every pixel's low
> byte as a palette index.

### `GET /api/layout`

The runtime panel arrangement — from `<mac>.yml` if one was fetched at boot,
otherwise from the config baked into the firmware, otherwise a single panel at
the bitstream's own geometry.

```json
{"grid":"1x2","panel_width":128,"panel_height":64,
 "virtual_width":128,"virtual_height":128,
 "panels":{"J1":["0,0","0,0"],"J2":["0,1","0,1"],
           "J3":[null,null],"J4":[null,null],"J5":[null,null],"J6":[null,null]}}
```

Each connector lists its chain slots. `null` is an unassigned slot — six of the
twelve here, because these panels are one per connector with no chaining.

### `GET /api/bitmap/stats`

Streaming health. **This, not `/api/status`, is where the frame counters live.**

```json
{"packets_total":0,"packets_valid":0,"palette_writes":0,
 "bad_magic":0,"bad_header":0,"bad_size":0,"duplicate":0,
 "frames_completed":0,"frames_partial":0,"frames_dropped":0,"frames_stale":0,
 "chunks_repaired":0,"last_missing":0,
 "fps":0,"frame_interval_ms":0,"avg_interval_ms":0,"jitter_ms":0,
 "last_frame_id":0,"last_chunk":"0/0","last_size":"0x0","last_data_len":0,
 "mac_overflow":121,"ring_overflow":0, ...}
```

Diagnosing a stream, in the order to check:

| Symptom | Field | Means |
|---|---|---|
| nothing moves | `packets_total` 0 | nothing is arriving at all |
| nothing moves, packets arriving | `frames_completed` 0 + `last_missing` > 0 | chunks are being lost — **check `dma_enabled`** |
| nothing moves, all counters fine | — | it is a colour/mode fault; see `/api/fb` |
| `bad_size` climbing | `last_size` | the sender's geometry does not match the panel |
| `bad_magic` climbing | — | something that is not this protocol is on port 7000 |
| stutter | `mac_overflow`, `jitter_ms` | the ISR is not draining the FIFO in time |

`palette_writes` exists because **a palette that never arrived and a palette
full of black look identical on the wall**.

### `GET /api/fb`

**The endpoint that ends arguments.** Dumps the front buffer — what is actually
being scanned out — sampled every 4 pixels, tagged with the true output mode.

```console
$ curl http://<panel>/api/fb
```

Use it when every counter says healthy and the wall says otherwise. It once
returned a pixel-perfect test pattern tagged `mode: indexed`: correct pixels,
wrong mode, which nothing else could have revealed.

---

## On-panel programs

See **[docs/PROGRAMMING.md](docs/PROGRAMMING.md)** for writing them.

### `GET /api/programs`

```json
{"active":"timessquare","running":true,"frame":3339,"period_ms":33,
 "programs":["dashboard-home","blank","ticker","timessquare"]}
```

`programs` is what this **firmware** contains. One firmware holds every program
and the config picks which runs — so `POST /api/program/on` can only name one of
these, and adding a program means rebuilding the firmware.

### `POST /api/program/on` / `POST /api/program/off`

```console
$ curl -X POST http://<panel>/api/program/on -d '{"name":"ticker"}'
$ curl -X POST http://<panel>/api/program/off
```

Starting a program **stops the stream and the gateware DMA**. The two never
share a panel — two writers on one framebuffer looks like corruption, not like a
conflict.

> **This does not survive a reboot.** For that, set `mode: program` and
> `program: <name>` in the panel's TFTP config — or, for a panel with no network,
> bake them in with `./build.sh --default-config`. If the named program is not in
> the firmware, the panel falls back to stream mode rather than showing nothing.
>
> Precedence at boot: **TFTP config > baked default > single panel, streaming.**

### `GET /api/values`

```json
{"stale_ms":3156,
 "values":{"temp":{"v":"62","age_ms":3156},
           "cond":{"v":"sunny","age_ms":3156},
           "doors":{"v":"0","age_ms":3156}}}
```

16 keys, each with its own age. `stale_ms` is time since **any** value arrived.

### `POST /api/values`

```console
$ curl -X POST http://<panel>/api/values -d '{"temp":"62","cond":"sunny"}'
```

The entire data plane for a program-mode panel: **~20 bytes/s**, against the
~24 Mbit/s a streamed panel needs to show the same clock face.

Because the pixels are already on the panel, it can render the fact that you
stopped sending (`stale_marker`). A streamed panel cannot — it keeps showing a
frame that looks correct forever. **A sign that lies is worse than a blank one.**

### `GET /api/bench`

Per-pixel cost of the current program, measured on the board.

```json
{"pixels":16384,"iters":3,"display_on":true,
 "fill_cycles":0,"program_cycles":0,"palette_cycles":0,
 "fill_cy_per_px":0,"program_cy_per_px":0,"fill_ms":0,"program_ms":0}
```

`fill_cy_per_px` is the store path alone; `program_cy_per_px` includes your
code. The gap between them is what your drawing costs. Expect ~190 cycles per
memory access — see [docs/BENCHMARKS.md](docs/BENCHMARKS.md).

> **Every cycle count here reads 0, and always has.** This endpoint uses
> `mcycle` (`csrr 0xB00`), which is not implemented on this VexRiscv build and
> returns 0 forever. The ratios above are therefore meaningless as shipped. Use
> `Timer0` instead, as `/api/flashbench` does. Fixing this properly is TODO
> item 7.

---

### `GET /api/hwfilter`, `POST /api/hwfilter/on|off`

The gateware pixel filter: whether the CPU is out of the pixel path.

```json
{"enabled":1,"port":7000,"killed":91222}
```

`killed` counts packets ended by `SmolEthInvalidator` before the MAC could raise
a receive event. It should track the number of pixel packets sent exactly — 30
frames of 34 chunks gave `killed: 1020`.

Enabled at boot by the firmware, **not** defaulted on in the bitstream:
`pixel_filter_enable` resets to 0 on FPGA configuration, so the BIOS always
netboots with the filter off and a power cycle recovers a bad firmware. Turning
it on also enables the gateware framebuffer swap.

---

### `GET /api/isrprof`, `POST /api/isrprof/reset`

What the receive ISR costs, per entry and per packet.

```json
{"isr_cycles":14599050,"isr_entries":1343,"isr_packets":1382,
 "cycles_per_entry":10870,"cycles_per_packet":10563,"us_per_packet":264,
 "ethmem_cycles_per_byte":7,"dram_cycles_per_byte":13,"sys_clk_hz":40000000}
```

`isr_packets / isr_entries` is the batching factor — it measured **1.00**, so
fixed entry cost dominates. The two cycles-per-byte figures are a control: they
show the ISR is **not** bus-bound, which rules out the obvious explanation.

Uses `Timer0`, not `mcycle` — see the note on `/api/bench`.

---

### `GET /api/dma/arrival`

The DMA's live chunk-arrival bitmap, naming what is missing rather than counting.

```json
{"frame_id":4001,"total_chunks":34,"indexed":0,"bits_set":34,"missing":[]}
```

Read in isolation after a single frame this should show every chunk. If it shows
`missing:[33]` during streaming, the sender is padding its final chunk past the
end of the image — see `tools/bench_stream.py`.

---

### `POST /api/cpupix/on|off`

Makes the **CPU** ignore pixel packets, simulating the gateware filter in
software. A test instrument: it proves the whole downstream path over the
network, reversibly, without building a bitstream. It does **not** save the
interrupt, so it is not a performance setting.

---

### `GET /api/flashbench`

Times reads out of the memory-mapped SPI flash, and sweeps the SPI clock.

```json
{"n":4096,"byte_cycles":1006800,"word_cycles":167000,"ram_byte_cycles":59902,
 "img_len":292976,"flashboot_cycles":155840000,"flashboot_ms":3896,
 "spin_cycles":800064,"netboot_fail_ms":320,"sys_clk_hz":40000000,
 "divisor_reset":1,
 "sweep":[{"div":9,"mhz_x10":20,"cycles":49952000,"ok":true,"chk":2956753878},
          {"div":1,"mhz_x10":100,"cycles":12452000,"ok":true,"chk":2956753878}]}
```

| Field | Means |
|---|---|
| `byte_cycles` / `word_cycles` | cost of reading `n` bytes from flash, byte- and word-wise |
| `ram_byte_cycles` | the same loop over DRAM — the control, so loop overhead is visible |
| `flashboot_ms` | what a flash boot costs: crc32 over the image **twice** plus the copy |
| `netboot_fail_ms` | what `netboot()` costs when nothing answers (2 × 8 ARP tries × 100,000 spins) |
| `divisor_reset` | the gateware's `default_divisor` — what the **BIOS** boots at |
| `sweep[]` | per-divisor full-image read; `ok` is false if the data read back differs |

`clk_divisor` is a `CSRStorage`, so the sweep retunes the SPI clock at runtime
and checksums the **whole 292,976-byte image** at each setting. That is how a
faster clock gets proven on real hardware before it is baked into a bitstream —
but only `default_divisor` changes boot time, because the BIOS runs at the reset
value.

Timing uses the 1-second countdown timer, not `mcycle`, and is taken in chunks
and summed so a pass longer than one timer period cannot alias.

See [docs/HARDWARE.md §8c](docs/HARDWARE.md).

---

## Control

### `POST /api/dma/on` / `POST /api/dma/off`

Hardware pixel DMA: streamed pixels go straight to SDRAM in gateware, and the
CPU never touches them.

```console
$ curl -X POST http://<panel>/api/dma/on
```

> **A panel boots with this OFF, and that is the single most common streaming
> fault.** On the CPU path the receive ISR cannot drain the MAC FIFO: roughly 3
> chunks of every 12 are lost, so **no frame ever completes** and the panel keeps
> showing whatever was on it. It reads as a frozen or corrupt display, not as a
> slow one.
>
> **Re-assert it after every reboot** — a power cycle, a firmware reload, a
> watchdog. A sender that enables it once at startup will silently lose panels.

### `POST /api/display/pattern`

```console
$ curl -X POST http://<panel>/api/display/pattern -d '{"name":"grid"}'
```

`grid`, `rainbow`, `rainbow_anim`, `white`, `red`, `green`, `blue`, `gradient`.

`grid` is the geometry test — border, centre lines, corner-to-corner diagonals,
and the firmware version. How to read it is in
[docs/GETTING-STARTED.md](docs/GETTING-STARTED.md#step-6-prove-the-wiring-is-right).

Setting a pattern **exits program mode**, so the two cannot fight over the
framebuffer.

### `POST /api/display/hold/on` / `off`

Freeze what is displayed. Stops the CPU parser **and** the gateware DMA.

> An earlier "hold" stopped only the parser, so a panel reporting itself held was
> still being overwritten ten times a second by the DMA.

### `POST /api/display/on` / `POST /api/display/off`

Blank the panel without changing anything else.

### `POST /api/reboot`

```json
{"ok":true,"rebooting":true,"in_ms":300}
```

Resets the SoC ~300 ms after answering. The delay is not cosmetic: the response
is still sitting in a smoltcp TX buffer when the handler returns, so resetting
there would drop the connection and the caller would see a hang instead of an
answer.

After it comes back, `prev_mark`/`prev_mcause`/`prev_mepc` in `/api/status`
describe the *previous* run.

> **Before v1.27.0 this endpoint was a stub.** It returned
> `{"ok":true,"rebooting":true}` and did nothing at all — the board simply kept
> running. If you are on older firmware, use the telnet console's `reboot`, or
> reload the bitstream over JTAG.

---

## The UDP pixel protocol

Port **7000**. Fully specified in [ARCH.md §4](ARCH.md#4-the-wire-protocol).

10-byte little-endian header `<2sHBBHH>`:

```
0..1  magic          2..3  frame_id u16
4     chunk_index    5     total_chunks
6..7  width u16      8..9  height u16
```

| Magic | Format | Pixels/chunk |
|---|---|---|
| `BM` | RGB888, 3 B/px | 487 |
| `BI` | indexed, 1 B/px | **1461** (`= 3 × 487`) |
| `BP` | palette — 256 entries, one packet | — |

Three rules that are not negotiable:

1. **The magic sets the output mode.** `BI` puts the panel in indexed, `BM`
   returns it to full colour. There is no mode endpoint to keep in sync.
2. **Frame geometry must match the panel exactly.** Mismatches are refused and
   counted as `bad_size`, never drawn as a part-frame.
3. **1461, not 1462.** 1462 is not divisible by three; using it puts every chunk
   after the first one pixel left of where the hardware writes it, which shows
   up as *shearing* rather than as an error.

Reference senders: `tools/send_video.py`, `tools/send_image.py`,
`tools/send_test_pattern.py`.

---

## Other interfaces

| Interface | Port | Notes |
|---|---|---|
| HTTP | 80 | this document |
| UDP pixels | 7000 | above |
| Art-Net | 6454 | validated header; see [ARCH.md](ARCH.md) |
| Telnet | 23 | the management console |
| TFTP (client) | **6969** | the panel fetches `boot.bin` + `<mac>.yml` — **not port 69** |

---

## See also

- **[docs/GETTING-STARTED.md](docs/GETTING-STARTED.md)** — from parts to a lit panel
- **[docs/PROGRAMMING.md](docs/PROGRAMMING.md)** — writing on-panel programs
- **[docs/BENCHMARKS.md](docs/BENCHMARKS.md)** — what these numbers should look like
- **[ARCH.md](ARCH.md)** — how the firmware is put together
