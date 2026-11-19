# HTTP API

Every panel serves a status page and a JSON API on **port 80** — or, for a card
chained behind another, on **8000+h** of the head's IP, where h is its hop
([ARCH.md §14](ARCH.md#14-card-chains--one-ip-many-cards)). No auth — this is a
device on a trusted segment, and it is the only interface it has.

All examples use `<panel>` for the board's address. Responses below are real,
captured from a running panel.

| | |
|---|---|
| **Look at** | [`/api/status`](#get-apistatus) · [`/api/display`](#get-apidisplay) · [`/api/layout`](#get-apilayout) · [`/api/bitmap/stats`](#get-apibitmapstats) · [`/api/fb`](#get-apifb) · [`/api/fbdump`](#post-apifbdump) |
| **Programs** | [`/api/programs`](#get-apiprograms) · [`/api/program/on`](#post-apiprogramon--post-apiprogramoff) · [`/api/values`](#get-apivalues) |
| **Control** | [`/api/dma/on`](#post-apidmaon--post-apidmaoff) · [`/api/layout`](#post-apilayout) · [`/api/clkdiv`](#post-apiclkdiv--the-panel-shift-clock-and-therefore-the-refresh-rate) · [`/api/display/pattern`](#post-apidisplaypattern) · [`/api/display/hold/on`](#post-apidisplayholdon--off) · [`/api/reboot`](#post-apireboot) |
| **Updates** | [`/api/netboot`](#post-apinetboot-get-apinetboot) · [`/api/flash/firmware`](#post-apiflashfirmware-get-apiflashfirmware) · [`/api/flash/bitstream`](#post-apiflashbitstream-get-apiflashbitstream) |

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
  "mcast_dropped": 44895, "arp_dropped": 450, "http_syn_deferred": 0,
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
| `http_syn_deferred` | new HTTP connections that found all four sockets busy and were dropped for the client to retry (5.0.3+; before that they were refused) |
| `max_batch` | packets handled in one ISR entry; capped at 64 |
| `prev_mark`, `prev_mcause`, `prev_mepc` | **the board rebooted** — see below |

> **`prev_*` are crash breadcrumbs**, written to uncached SDRAM that survives
> `soc_rst`. After an unexplained reboot they say how far execution got and why.
> This found a bug that bisection had failed on outright — there is no serial
> console on this board, so they may be all you get.

> **The card has four HTTP sockets** (firmware 5.0.3; one connection at a time
> before that), and a long `/api/status` response is occasionally truncated
> mid-JSON. Any script that reads it must retry on a dropped or refused
> connection and on a parse error, or it will die mid-sweep.

#### Identity and build agreement

| Field | Meaning |
|---|---|
| `version` | the firmware's semver (`Cargo.toml`) |
| `gateware` | the bitstream's identifier ROM — board, panel, output count, gateware semver, clock, build time. **What is actually running**, not what was pushed |
| `fw_outputs` / `hw_outputs` | the firmware's compiled `OUTPUTS` and the bitstream's output count |
| `output_mismatch`, `module_mismatch` | nonzero = firmware and bitstream disagree (connectors silently dropped) or the layout's module size is not the bitstream's |
| `hw_rgb_groups` | 2 on HUB75, 4 on HUB320 |

#### Streaming path (gateware pixel DMA)

| Field | Meaning |
|---|---|
| `dma_enabled`, `dma_pixels`, `dma_chunks`, `dma_bad_magic` | the hardware pixel path; chunks/s ÷ chunks-per-frame = frames/s |
| `frames_held` | frames **not shown** because they were incomplete (`swap_whole_only` = 1, the default). Each one is a 50 ms repeat instead of a torn band |
| `c_dropped` | whole packets lost at the receive FIFO — the site where chunk loss is counted |
| `c_fifo_max`, `arrive_run_max`, `drain_stall_max`, `cpu_stall_max`, `dma_stall_peak` | **peaks since the last** `POST /api/peaks/clear` — clear before a measurement window, or you are reading a lifetime maximum |
| `n_gate` … `n_udp`, `c_core` … `c_narrow_out` | per-stage packet counts through the receive path |
| `dma_base`, `dma_limit`, `fb_base_now`, `auto_swap_hw` | where the DMA is writing, the frame size it accepts (pixels), and which half is displayed |
| `link_inband` | the PHY's RGMII in-band status as the card sees it — raw bits: bit 0 link, bits 2:1 speed (2 = 1000), bit 3 full duplex. Read from the card, not from a switch |

#### S-PWM scan-out (HUB320 builds)

| Field | Meaning |
|---|---|
| `underruns` | serialiser reads of a line-buffer slot that did not yet hold its row — a wrong pixel on the glass. **Output 0 only**, and **~121 after every boot** (the unprimed first rows): measure the delta over a window, never the absolute |
| `perf_fill_cycles`, `perf_fills` | clocks output 0's line buffer spent filling, and how many fills. Their ratio is the average row fill, to compare against the row budget `columns × 16 × clk_div` (8192 for the P1.25 at `clk_div` 4) |
| `perf_cmds`, `perf_stall` | read commands the display port accepted, and clocks a command waited for it (all outputs) |
| `xbar` | six counters: commands accepted per SDRAM crossbar master — CPU L2, uncached bridge, **display**, `dmatest`, **pixel DMA**, unused. The order is printed by the gateware build |
| `refresh_count` | scan-out frames — the FSM, **not** proof of light: it counts with nothing attached |
| `spwm_built` | 1 on an S-PWM build; 0 on a HUB75 one, where the fields below are absent and `drive` is ignored |
| `spwm_configured`, `spwm_drive` | which driver-chip table the firmware wrote (`default` = none asked for, the bitstream's reset values stand; `unknown` = no table by that name, nothing written), and which one the layout asked for |
| `hw_driver`, `hw_chip` | the bitstream's output stage (1 = S-PWM) and the index of the chip table it was built with -- what an unconfigured card runs |
| `chip_cfg`, `row_ser`, `latch_phase`, `chan_xor`, `sweep_adv` | the live S-PWM CSRs, read back |
| `chip_regs`, `chip_regs_matched` | `chip_reg:` lines in the layout config, and how many name a register the chip's table carries (the rest are ignored) |
| `rotate` | how the modules are mounted, degrees clockwise (`rotate:` in the layout config; firmware 5.7.0). On-card programs, the boot screen and the `grid`/`quad`/`ramp` patterns are drawn turned to match. `display_width`/`display_height` and `/api/fbdump` stay the framebuffer's |

All counters are cumulative; read twice and subtract. Measured on the
four-output E320 streaming 256x512 at 20 fps: ~5,400 `dma_chunks`/s, 66.67
`refresh_count`/s, 0 `underruns`/s, `perf_cmds`/`perf_fills` = 2,048 (four
outputs × four groups × 128 columns).

### The `chain` block of `/api/status`

```json
"chain": {"role": "member", "hop": 2, "http_port": 8002, "pixel_port": 7002,
          "port1_link": true, "port1_speed": 1000,
          "fwd_frames": 81234, "fwd_rewrites": 410, "fwd_dropped": 5120, "up_frames": 96, "up_dropped": 3,
          "ignored": 52011, "discoveries": 410, "server": "192.168.61.1",
          "clock_ms": 1299769266, "clock_offset": 1299636871, "wall": "512x256", "origin": "256,0"}
```

| field | meaning |
|---|---|
| `role` | `none` (no discovery seen yet: a lone card), `head` (hop 0), `member` (hop ≥ 1) |
| `hop`, `http_port`, `pixel_port` | this card's position and the ports it answers on (80/7000 at hop 0, 8000+h/7000+h behind) |
| `port1_link`, `port1_speed` | the DOWNSTREAM port: is another card cabled into it, and at what speed (must be 1000) |
| `counters` | `false` when the gateware was built without the chain counters (the four-output E320 build): the five counts below are then `null`, not 0 (firmware 5.0.4+) |
| `fwd_frames` | frames repeated out of the downstream port (gateware) |
| `fwd_rewrites` | discovery frames whose hop this card incremented on the way through |
| `fwd_dropped` | group-addressed frames NOT forwarded downstream (by design) |
| `up_frames`, `up_dropped` | frames from downstream cards queued to the switch / dropped -- broadcasts (by design), FIFO full, runts |
| `ignored` | frames this card's CPU discarded because they belong to another card in the chain |
| `discoveries`, `server` | discovery frames received, and the host that sent them (where a member fetches its config) |
| `clock_ms`, `clock_offset` | the shared program clock (wrapping u32 ms, marquee's once a v2 discovery arrived) and its offset from uptime |
| `wall`, `origin` | this card's wall size and origin from the boot config (`0x0` = the wall is the card) |

The counters are cumulative; read twice and subtract. A member is reached at
`http://<head>:8000+hop/`. The JSON endpoints send
`Access-Control-Allow-Origin: *`, which is what lets a head's own status page
list its members from the browser.

### `GET /api/display`

What is on the glass, and where it is coming from.

```json
{"width":128,"height":128,"mode":"indexed","animation":"none",
 "source":"program (on-panel)","hold":true}
```

| Field | Values |
|---|---|
| `mode` | `fullcolor` \| `indexed` — the **output** mode |
| `source` | `streaming` \| `program (on-panel)` \| `held (native pattern)` \| `animation` \| `idle (no frames)` |
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
 "miss_buckets":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0],
 "swap_overlap_max":0,"swap_overlap_last":0,
 "mac_overflow":121,"ring_overflow":0, ...}
```

`miss_buckets` (firmware 3.4.0) says **WHERE in the frame** chunks went
missing -- sixteen buckets across the frame's chunk range. A chunk index maps
linearly to panel rows, so bucket 8 of 16 is the middle of the glass. It
exists because nothing recorded the POSITION of a loss, and a frame missing
two or fewer chunks is repaired and counted nowhere at all, so "the artifact
is only in the centre" had no measurement that could confirm or refute it.

It earns its keep: it showed loss concentrated in the **first dozen chunks of
every frame**, which matched a capture of the host's own sub-100 us
inter-packet gaps at the same chunk indices and identified the sender -- not
the network -- as the burst source.

Diagnosing a stream, in the order to check:

| Symptom | Field | Means |
|---|---|---|
| nothing moves | `packets_total` 0 | nothing is arriving at all |
| nothing moves, packets arriving | `frames_completed` 0 + `last_missing` > 0 | chunks are being lost — **check `dma_enabled`** |
| nothing moves, all counters fine | — | it is a colour/mode fault; see `/api/fb` |
| `bad_size` climbing | `last_size` | the sender's geometry does not match the panel |
| `bad_magic` climbing | — | something that is not this protocol is on port 7000 |
| stutter | `jitter_ms` | frames are not arriving evenly -- check the SENDER before the card |
| blocks of stale picture | `c_dropped` in `/api/status` | whole chunks lost; **read `miss_buckets` for where** |

**`mac_overflow` is a decoy** and reads 20+/s during entirely healthy
operation. It is `sram_writer_errors` -- the **CPU's** MAC slots overflowing
on multicast and ARP. Pixel chunks take the gateware hardware UDP path and
never touch it: tens of millions of packets pass `hw_filter` while only
hundreds reach the CPU (`slow_udp`). A large `mac_overflow` says the network
is noisy, not that pixels are being lost.

`swap_overlap_max` is how many chunks of the **next** frame the DMA had
already written into the buffer at the moment the firmware made it visible.
Nonzero means the top of the frame can briefly carry next-frame data; it is
invisible on static content and shows as a tear on moving content.

`palette_writes` exists because **a palette that never arrived and a palette
full of black look identical on the wall**.

### `GET /api/fb`

**The endpoint that ends arguments.** Dumps the front buffer — what is actually
being scanned out — sampled every `step` pixels each way, tagged with the true
output mode. `step` is 4, or 8 (16, ...) on a frame too big for every 4th pixel
to fit the 8 KiB reply — 256x256 and up (firmware 5.0.2; before it, those
replies were cut mid-string).

```console
$ curl http://<panel>/api/fb
```

Use it when every counter says healthy and the wall says otherwise. It once
returned a pixel-perfect test pattern tagged `mode: indexed`: correct pixels,
wrong mode, which nothing else could have revealed.

### `POST /api/fbdump`

Up to sixteen **consecutive rows** of the framebuffer starting at `y0`, as the
low byte of each pixel word in hex — the precise read-back where `/api/fb`
samples. `rows` says how many: as many as fit the 8 KiB reply, so 15 on a
256-wide card. Step `y0` by the `rows` you got.

```console
$ curl -X POST http://<panel>/api/fbdump -d '{"y0":"300"}'
{"w":256,"h":512,"y0":300,"rows":15,"mode":"fullcolor","data":"1e2023252828..."}
```

> **`y0` must be a quoted string.** The firmware's JSON reader only takes string
> values, so `{"y0":300}` is silently ignored and dumps from row 0.
>
> Before firmware 5.0.2 the reply was always sixteen rows, and on a 256-wide
> card that overflowed the response buffer: every block arrived cut short and
> unparseable.

**This is the question it answers: is that module dark, or is it showing
black?** The two look identical on the wall. On 2026-10-26 two modules moved to
the E320's J3/J4 showed nothing; `fbdump` at rows 300 and 450 read all zeros
while row 100 held picture, which put the fault in the content (a show drawn for
a smaller wall), not in the connectors.

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

Up to 24 keys (`MAX_VALUES`), each with its own age. `stale_ms` is time since **any** value arrived.

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

Whole-frame costs measured on the board: a bare fill, the current program,
and a palette rewrite. A deferred job, like `/api/profile`: the first GET
answers `{"status":"queued","note":"GET again for the result"}`, the main loop
runs it, and a later GET collects it (`running` in between).

```json
{"status":"done","pixels":16384,"display_on":true,"program":"hello",
 "fill_ns":…,"program_ns":…,"palette_ns":…,
 "fill_cycles":…,"program_cycles":…,"palette_cycles":…,
 "fill_cy_per_px":…,"program_cy_per_px":…,
 "fill_ms":…,"program_ms":…,"program_fps_x100":…}
```

`fill_*` is the store path alone; `program_*` includes your code. The gap
between them is what your drawing costs. The `*_ns` fields are the
measurement; the `*_cycles` fields are derived from them as ns / 25, i.e. they
assume a 40 MHz system clock and read 14% high on a 35 MHz E320. See
[docs/BENCHMARKS.md](docs/BENCHMARKS.md).

> **Before firmware 2.6.0 every count here read 0.** The endpoint used
> `mcycle` (`csrr 0xB00`), which is not implemented on this VexRiscv build and
> returns 0 forever. It was rebuilt on the millisecond tick, which cannot
> advance inside the trap handler where HTTP is served -- hence the deferred
> job.

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

### `POST /api/peaks/clear`

Resets the two high-water marks -- `c_fifo_max` (deepest `burst_fifo` level)
and `pixdma.stall_peak` (longest run of blocked writer cycles) -- so they can
be read as a **window** instead of a lifetime mark.

```json
{"ok":true,"cleared":["c_fifo_max","dma_stall_peak"],
 "c_fifo_max":0,"dma_stall_peak":0}
```

**Use it. An uncleared peak is close to useless, and it has produced two
wrong conclusions.** `c_fifo_max` read 4097 of 4096 and was reported -- in a
Jira ticket, as settled evidence -- as proof that `burst_fifo` saturates
routinely. It proves only that the FIFO filled **once** at some point since
the FPGA was configured, which a single starved moment during boot satisfies.
Cleared and measured over 420 s of streaming, the same counter read 1314 of
2048: **64%**, nowhere near saturation.

`stall_peak` has the same trap and an extra one: 8192 is its **saturation**
value (`stall` is `Signal(max=8192)` and stops counting there), so 8193 means
"at least 234 us", not "234 us".

The honest sequence is always: **clear, run a known window, read** -- and
record the delivered fps beside the result, because the loss curve is steeply
nonlinear in load and a window taken while the host is busy is void.

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

### `POST /api/clkdiv` — the panel shift clock, and therefore the REFRESH RATE

```
POST /api/clkdiv   {"n": 4}
-> {"ok":true,"clk_div":4,"effective":4,"panel_clk_hz":8750000}
```

Panel shift clock = `sys_clk / n`, minimum 2. **This is a runtime CSR, so the
refresh rate is testable live in seconds with no bitstream build** -- and it
is reversible just as fast, which makes it the right first thing to try for
any flicker complaint.

**Refresh rate is a picture-quality setting, not just a capacity one.** On the
P1.25 bench module:

    clk_div 6 ... 44.45 Hz refresh, 0.00 underruns/s
    clk_div 4 ... 66.67 Hz refresh, 0.00 underruns/s

44 Hz is **below human flicker fusion**, and the symptom is distinctive: a
near-black region beat visibly while bright text drawn on top of it sat
perfectly still. A near-black pixel is lit for a very short pulse each refresh
and short pulses at 44 Hz are seen; a bright pixel is lit long enough to fuse.

> **If an artifact is selective by BRIGHTNESS rather than by position or
> content, suspect the refresh rate before the datapath.** Content, the
> sender, the line buffer and a buffer-swap-versus-scan beat were each ruled
> out by measurement first, at considerable cost.

Lowering `n` costs scan-out DRAM bandwidth. Sweep it against
`hub75.underruns` -- every underrun is a pixel the serialiser latched before
its read returned, i.e. a wrong pixel on the glass. **Re-sweep after any
change to the scan-out path**: `clk_div` was pinned at 6 because 4 measured
5454 underruns/s on gateware 1.28.0, and the line buffer in 1.29.0 removed
exactly the read deadline a faster clock narrows. The old number stood for a
day after it stopped being true.

Persist it in the module's `PANEL_TYPES` entry in marquee, or the card reverts
to the preset on its next boot.

### `POST /api/throttle`

```
POST /api/throttle   {"n": 0}
```

Spacing between the pixel DMA's writes, in clocks. A workaround for the same
coupling the line buffer removed; **0 requires gateware >= 1.30.0**, where it
measures 0 underruns. On an older bitstream 0 meant snow.

### `POST /api/layout`

The same text a boot config carries (`grid:`, `panel_width:`, `drive:`,
`group_origins:`, `J1: 0,0` ...), applied now. For bringing up a module on a
card that has no boot config to fetch. The request must fit the server's
512-byte limit, and nothing is stored: the next boot config overwrites it.
`chip_reg: <addr> <r> <g> <b>` lines (decimal or `0x` hex; one value means all
three colour lines) replace registers of the chip's table by address; a full
vendor table does not fit the 512 bytes, so that comes from the boot config.
`{"ok":true}`, or `400 layout did not parse`.

### S-PWM live tuning (HUB320 builds only)

All `POST` with a JSON body, each answering with the value read back, and all
lost at the next boot — persist a good value in the build or the module's
config. `/api/status` carries `chip_cfg`, `row_ser`, `latch_phase`, `chan_xor`
and `sweep_adv`; `vsync_align` and `row_rot` are only in their own replies.

| Endpoint | Body | What |
|---|---|---|
| `/api/chipreg` | `{"index":10,"value":3212}` or per-lane `r`/`g`/`b` | one row of the driver-chip register table; one register is sent per frame, so allow ~15 frames |
| `/api/chipcfg` | `{"row_pulse":4,...}` | fields of `chip_cfg`: `row_pulse`, `row_pulse_first`, `data_latch`, `row_address`, `row_addr_shift`, and the grammar half `reg_count`, `slots`, `rot_slot`, `mid_latch`, `chan_rev` |
| `/api/rowser` | `{"a_src":0,"inv":7,...}` | row-driver pin mapping and polarity (serial row chains such as the RT5958); `din_row` (0-63) is the scan row on which the chain's DIN is high |
| `/api/auxpins` | `{"d_mode":2,"e_mode":3}` | HUB320 pins 20/21: 0 row bit, 1 low, 2 high, 3 hi-Z. Boots 2/3, the safe state for a module with a calibration-flash bus there (`docs/HUB320.md`). `GET` reads the modes and the pads as read (`d_in`, `e_in`) |
| `/api/latchphase` | `{"n":1}` | extra DATA_LATCH pulses |
| `/api/vsyncalign` | `{"n":1}` | start every data frame (and its VSYNC) on a row-scan pass boundary. Needed by a chip that resets its line counter on VSYNC (ICND1065L): off, the picture rolls |
| `/api/rowrot` | `{"n":0}` | rotate the fetched row by n (0-63) within a scan; a diagnostic, 0 in every shipped table |
| `/api/sweepadv`, `/api/chanxor`, `/api/passes` | `{"n":…}` | the commit-position, channel-pair and display-pass knobs (docs/E320-BRINGUP.md §4.6a, §4.6b). `/api/passes` also rewrites chip register 0x03, which is the pass count only on the DP32xx family: on an `icnd1065` module it clobbers an unrelated register (restore it with `/api/chipreg`) |

The alternative to each of these is a seven-minute place-and-route per guess.
docs/E320-BRINGUP.md §5d has the bench workflow.

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

`grid`, `rainbow`, `rainbow_anim`, `white`, `red`, `green`, `blue`. An unknown
name is refused.

Bring-up patterns for mapping an unknown module, each built to answer one
wiring question (`src/patterns.rs` says which): `quad`, `ramp`, `cols`,
`cols8`, `remap`, `redge`, `hstripe`, `chanends`, `rowid`, `rowid_adv`,
`sweepcal`, `chanmap`, `stair`, `chan0`, `chanhalf`, `probe`, `bincode`.

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

### `POST /api/flash/firmware`, `GET /api/flash/firmware`

`{"file": "<name>.fbi"}`, optionally `"server"`. Writes an FBI-wrapped firmware
image (u32 length, u32 crc32, little-endian, then `boot.bin` -- what
`crcfbigen.py -f -l` writes) into the firmware copy at flash `0x100000`. The
whole image is received by TFTP into the framebuffer's back half first (the
stream is held, the front frame stays lit), its length and crc32 checked
there, and only then is flash erased and written, a sector at a time, and read
back. Refused without a golden image. Poll the GET for `state`/`written`.

This copy is the **survival copy**: what a card runs when it boots and cannot
reach marquee, and where a chain member's BIOS always lands (its DHCP cannot
leave the chain). An update does not write it; marquee's firmware sync does,
once the assigned image has run cleanly for ten minutes and the copy's header
(`GET /api/flash`, `offset100000`: length then crc32) differs.

### `POST /api/netboot`, `GET /api/netboot`

`{"file": "<name>.fbi"}`, optionally `"server"` (default: the server that
discovered or booted the card). Firmware 5.0.5+. **Start** a firmware image
instead of writing it: the card's own netboot, which is how a chain member
gets its assigned image. Received and checked exactly as above, then the card
stops the pixel DMA and filter, masks interrupts and hands over the way the
BIOS does (`src/chainload.rs`). Flash is not touched; a failed fetch or bad
CRC leaves the card on what it runs (`state` `failed: image length or CRC
wrong; still running this one`). The card stops answering once it has handed
over -- the new image's `/api/status` reports its version about 8 s after the
request on the E320. The GET is the progress (`target: "netboot"`).

### `POST /api/flash/bitstream`, `GET /api/flash/bitstream`

The over-the-air BITSTREAM update. `{"file": "<tftp name>"}`, optionally
`"server"` (default: the TFTP server this card booted from). Returns as soon as
the fetch is queued; poll the GET for `state`/`written`, plus `golden` and
`app_preamble`. **Refused without a golden image** (`FlashError::NoGolden`).
Writing flash does not reconfigure the FPGA — the new bitstream runs from the
next power cycle. Procedure: [docs/FLASHING.md](docs/FLASHING.md) §2e.

### `GET /api/bitstream` (also `GET /api/gateware`)

What gateware and what firmware this card is running, read out of the
gateware's own identifier ROM:

```json
{"bitstream":{"name":"e320-v1.0.0","version":"1.0.0","board":"e320",
              "panel":"128x64","outputs":6,"clock_mhz":35,"built":"2026-10-07 15:47:46"},
 "firmware":{"version":"2.18.0"}}
```

`name`/`version` are null (and `raw` carries the string) on a bitstream built
before the structured ident existed.

### Flash diagnostics

| Endpoint | Does |
|---|---|
| `GET /api/flash` | the first bytes at flash offset 0 and 0x100000, read through the memory-mapped window — an independent path from the SPI master that wrote them |
| `POST /api/flash/verify` | `{"addr":…, "len":…}` (decimal or `0x` hex): read a region back and report what is there |
| `POST /api/flash/job`, `GET /api/flash/job` | `{"kind":"erase"\|"selftest","addr":…,"len":…}`: queue a flash erase or the write self-test (`tools/ota_selftest.sh`); the work runs in the main loop, never in the network ISR; poll the GET |
| `POST /api/flash/drive` | write the flash's status register 3 (output drive strength; default `{"sr3":0}` = full drive). **Non-volatile** |

### Display-path switches and probes

Bench levers, all reset at the next boot. Each answers with the state it set.

| Endpoint | Does |
|---|---|
| `POST /api/autoswap/on\|off` | who flips the framebuffer — gateware (`on`) or CPU. Normally set together with the pixel filter; compare `dma_base` and `fb_base_now` in `/api/status` if they disagree |
| `POST /api/swapwhole/on\|off` | gateware 1.36.0's whole-frames-only swap (default on); `off` restores swap-on-every-frame, for an on-glass A/B |
| `GET /api/rgborder`, `POST /api/rgborder` | read, or try live, the channel order (body `GRB` or `{"order":"GRB"}`). Record the answer as `rgb_order` in the card's config so it survives a reboot |
| `GET /api/palette` | both palette banks and which one is live |
| `GET /api/profile` | queue a profile of the running on-panel program; poll until it reports the result (`idle` with an error if no program runs) |
| `GET /api/dmatest` | a timed SDRAM write burst through the test DMA master — bandwidth, not a health check |

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

10-byte little-endian header `<2sHBBHH>`, or 12 bytes for `BW`:

```
0..1  magic          2..3  frame_id u16
4     chunk_index    5     total_chunks           (BW: 4..5 index u16, 6..7 total u16)
6..7  width u16      8..9  height u16             (BW: 8..9 width, 10..11 height)
```

| Magic | Format | Pixels/chunk |
|---|---|---|
| `BM` | RGB888, 3 B/px | 487 |
| `BI` | indexed, 1 B/px | **1461** (`= 3 × 487`) |
| `BP` | palette — 256 entries, one packet | — |
| `BW` | RGB888 with u16 counters — for frames over 255 chunks | **486** |

Four rules that are not negotiable:

1. **The magic sets the output mode.** `BI` puts the panel in indexed, `BM`
   returns it to full colour. There is no mode endpoint to keep in sync.
2. **Frame geometry must match the panel exactly.** Mismatches are refused and
   counted as `bad_size`, never drawn as a part-frame.
3. **1461, not 1462.** 1462 is not divisible by three; using it puts every chunk
   after the first one pixel left of where the hardware writes it, which shows
   up as *shearing* rather than as an error.
4. **Every packet fits a 1500-byte MTU** — at most 1472 bytes of UDP payload.
   The card cannot reassemble IP fragments. That is why `BW` carries 486
   pixels: at 487 its 12-byte header made a 1501-byte datagram, and the lost
   tail pixel of every chunk showed as a fixed lattice of stale dots with every
   card counter at zero. Check `Ip: FragOKs` in `/proc/net/snmp` on the sender
   if in doubt. [ARCH.md §4](ARCH.md#4-the-wire-protocol).

Reference senders: `tools/send_video.py`, `tools/send_image.py`,
`tools/send_test_pattern.py`.

---

## Other interfaces

| Interface | Port | Notes |
|---|---|---|
| HTTP | 80 (hop h ≥ 1 in a chain: **8000+h**) | this document |
| UDP pixels | 7000 (chain: **7000+h**) | above |
| Chain discovery | **6990** UDP | `"CH"` hop byte, rewritten by every card it passes; ARCH.md §14 |
| Art-Net | 6454 | validated header; see [ARCH.md](ARCH.md) |
| Telnet | 23 | the management console |
| TFTP (client) | **6969** | the panel fetches `boot.bin` + `<mac>.yml` — **not port 69**. Local ports 6900/6901 (chain member: 20000+4h / +1) |

---

## See also

- **[docs/GETTING-STARTED.md](docs/GETTING-STARTED.md)** — from parts to a lit panel
- **[docs/PROGRAMMING.md](docs/PROGRAMMING.md)** — writing on-panel programs
- **[docs/BENCHMARKS.md](docs/BENCHMARKS.md)** — what these numbers should look like
- **[ARCH.md](ARCH.md)** — how the firmware is put together
