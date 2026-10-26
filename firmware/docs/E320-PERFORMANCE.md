# Four outputs at full refresh on an E320 — a performance case study

How one Colorlight E320 (Lattice LFE5U-25F, 35 MHz) was made to drive **four**
fine-pitch S-PWM modules at the same refresh rate as three — 66.7 Hz, with a
full four-module stream running and zero wrong pixels — when the first build
that fitted could only manage 44 Hz.

Every number below was measured on the bench card on 2026-10-25/26 unless it
is marked as a simulation or a place-and-route result. Where a figure is not in
the record, this document says so rather than estimating it. The resulting
design is described as architecture in [ARCH.md §13](../ARCH.md); the bring-up
story around it is [E320-BRINGUP.md §6b](E320-BRINGUP.md).

| | Start (first four-output build) | **End** |
|---|---|---|
| refresh at which the glass is clean | 44 Hz (`clk_div` 6), below flicker fusion | **66.7 Hz** (`clk_div` 4), the three-output rate |
| underruns/s at `clk_div` 4, streaming | 15,191 | **0** |
| row fill against an 8,192-clock budget | (no counter yet) | **~2,650–3,450 clocks** |
| logic | 24,163 / 24,288 LUT4 (99.5%) | **22,969 (94%)** |
| system clock (35 MHz target) | 35.4–37.4 MHz | **44–50 MHz** |
| full four-module frame (256x512) | never streamed | **20 fps**, 0 underruns over a 30-minute soak |

---

## 1. The problem, quantified

The modules are Linsn P1.25, 256x128, DP3264S S-PWM drivers. Each one is four
128x64 RGB-group tiles; a connector drives one module through twelve data
lines. The serialiser shifts 16 bits per pixel on every data line in parallel,
so for the preset (`columns` 128, 64 scan rows):

```
row period      = columns × 16 × clk_div  = 128 × 16 × 4   = 8,192 system clocks
frame           = 64 rows × 8,192 (+ a ~1,300-clock prefix)  ->  66.7 Hz at 35 MHz
row fill        = outputs × groups × columns = 4 × 4 × 128   = 2,048 SDRAM reads
needed rate     = 2,048 reads / 8,192 clocks                 = 0.25 reads per clock, sustained
```

Each output's line buffer must hold row r+1 before the serialiser reaches it.
The serialiser cannot wait — the driver chips clock on regardless — so a late
row is a wrong row on the glass, counted as an **underrun**.

**The baseline, measured on the same two modules, same 20 fps stream**
(2026-10-25; the three-output build is the comparison target):

| `clk_div` | refresh | 3 outputs, underruns/s | first 4-output build (gateware 1.38.0), underruns/s |
|---|---|---|---|
| 4 | 66.7 Hz | **0.00** | **15,191** |
| 5 | 53 Hz | 0.00 | 29.8 |
| 6 | 44 Hz | 0.00 | 0.8 |
| 8 | 33 Hz | 0.00 | 0.00 |

On the network side the four-output build matched or beat three outputs (20.00
fps, 0 dropped, 0 held, longest packet run 3, `burst_fifo` peak 949 of 2,048
against 966 of 1,536). The whole gap was the scan-out. And 44 Hz is not a
usable fallback: at `clk_div` 6, near-black regions visibly beat while bright
content stays still — short PWM pulses below flicker fusion.

## 2. Why it was hard

**One read port, in order.** Every display read goes through one LiteDRAM
native port, which returns data in order, and the crossbar does not overlap
banks for a single master. Speed has to come from the *pattern* of reads, not
from parallelism.

**Row misses dominate.** One pixel is four reads from four tiles. On this
module those tiles sit in two framebuffer rows — and each framebuffer row is a
different DRAM row — so a naive reader switches DRAM rows on most reads, and
every switch is a precharge plus an activate. A simple model (row hit 2 cycles,
miss 8, ~50% misses) put the sustainable rate at **0.196 reads per clock**;
the hardware sustained about 0.2. The target is 0.25.

**The stream writes into the same memory.** The pixel DMA writes the next frame
while the scan-out reads the current one. Measured at 20 fps on a 256x256
stream: **1.30 M write commands/s** against the display's 8.68 M reads/s. A
reader that fits with the stream paused can still miss when it is running.

**The part was full.** The first build that fitted four outputs was at 55 of 56
block RAMs and 99.5% of LUTs. Any fix had to cost no block RAM and give logic
back.

**No bigger silicon.** The goal was the three-output refresh on the same card.

## 3. What was done, in order, with the measured effect

### 3.1 Make four outputs fit at all

The line buffer costs 5 EBR per output, and four outputs on the stock build
needed 61 EBR of 56. The block RAM came out of the **fixed** cost, not the
pixel path:

| Trim | How | Effect |
|---|---|---|
| BIOS scratch SRAM 8 KB → 4 KB | `EXP_SRAM=0x1000` | 2 EBR |
| BIOS built with `-ffunction-sections -fdata-sections`, LTO forced, no `cmd_*.o` console commands | `exp/Dockerfile.out4` | LiteX auto-sizes the ROM to the BIOS image: `0x56E0`–`0x56E8` bytes (~22 KB) |
| build's own EBR accounting told what the trims achieved | `EXP_EBR_FIXED=35` | lets a fourth output through; nextpnr remains the judge |
| BIOS console level | `EXP_BIOS_CONSOLE=lite` | `disable` was tried first and **the card came up silent** — a BIOS that configures and never reaches the network, which the golden image cannot catch (FLASHING §2d). Recovered over JTAG |

**Result, gateware 1.38.0:** 55/56 EBR, 24,163 LUT4 (99.5%), 35.4–37.4 MHz on
four seeds, 130 MHz on the gigabit RX — and the 15,191 underruns/s of §1.

### 3.2 Not this: a faster clock, or faster memory

Raising the system clock raises memory bandwidth and the panel clock together,
so the clean divider stays ~6 whatever the clock. At 40 MHz `clk_div` 6 would
be ~51 Hz — still short of 66.7. It also did not build: ten four-output builds
at 38.3 and 40 MHz all failed timing (25.0–35.5 MHz).

**Half-rate SDRAM** (memory at twice the system clock) closed timing on two of
six seeds — at 56/56 EBR and 99.5% LUT — but on the card the image configured
and never reached the network; most likely the BIOS could not bring up the
memory at the new rate and phase. Recovered over JTAG and dropped. (The SoC had
also never passed the half-rate setting to its clock generator — that path had
never been built before.)

### 3.3 Pipelined reader (`EXP_PIPELINE`) — gateware 1.41.0

The shipped reader issued a pixel's four reads one after another, each waiting
for the previous one's data: `4L + 16` clocks per pixel. The pipelined reader
issues all four back to back and consumes the data as it returns: `L + groups`.

| | sim prediction | hardware, underruns/s |
|---|---|---|
| per-pixel cost (budget 64 at `clk_div` 4) | 33–43 clocks, against 52–92 for the shipped reader | — |
| `clk_div` 5 | — | 29.8 → **3.1** |
| `clk_div` 4 | — | 15,191 → **25,478** — no better |

The ten-fold improvement at `clk_div` 5 confirmed latency was a real cost; the
unchanged `clk_div` 4 said the remaining limit was **throughput**, not latency
— the row-miss rate of §2. (Timing: three of six seeds closed, at 35.01–35.29
MHz.)

The simulation also found a latent bug: the shipped reader fetched each column
twice and stored it **one slot late**, so every tile was shifted one pixel and
the last pixel of each row never stored. The pipelined reader stores every
pixel in its own column. (The later glass geometry test — 1 px rules across the
x=128 group seam — read continuous.)

### 3.4 Instruments, so the next step could be measured instead of guessed — 1.43.0 / 1.44.3

`perf_fill_cycles` / `perf_fills` (clocks output 0 spent filling a row, and how
many fills), `perf_cmds` / `perf_stall` (display-port commands accepted, and
clocks waited), and later `xbar` (commands accepted per SDRAM crossbar master).
From here every build reports its average row fill against the 8,192 budget.

The crossbar counters ruled out stray traffic (gateware 1.44.3, `clk_div` 4):

| Mcmd/s | CPU L2 | uncached bridge | **display** | dmatest | **pixel DMA** |
|---|---|---|---|---|---|
| streaming | 0.131 | 0.000 | 8.682 | 0.000 | 1.302 |
| paused | 0.111 | 0.000 | 8.738 | 0.000 | 0.000 |

Adding a CSR block moved the `CTRL` registers from `0xf0002000` to `0xf0002800`,
and the firmware's exception handler reset the SoC through a hard-coded
address — a CPU fault would have hung the card. The address now comes from the
PAC (`CTRL_RESET_ADDR`, ARCH §10).

### 3.5 A third line-buffer slot (`EXP_TRIPLE`) — 1.43.0, 1.43.1, 1.44.0

With two slots, the fill of row r+2 gets one row period. A third slot (free:
the buffer's block RAMs are 512 deep regardless) gives it two, so a burst of
DMA writes can be absorbed. Measured, pipelined reader:

| Build | `clk_div` 6 | `clk_div` 4, paused | `clk_div` 4, streaming |
|---|---|---|---|
| 1.43.0 — slots keyed on `pix_row` (32 MHz) | **4,938–5,250/s** | 12,757/s, fill 6,129 | 178,363/s, fill 8,448 |
| 1.43.1 — keyed on `pix_row_base` (32 MHz) | 0 | **0**, fill 5,894 | 137,562/s, fill 8,129 |
| 1.44.0 — same, 35 MHz, LUT fix (§3.7) | 0 | **0**, fill 5,876 | 100,875/s, fill 7,719 |

**The first keying was wrong**, and the instruments said so at once: underruns
at `clk_div` 6 with nothing streaming, where the two-slot build had none.
`pix_row` is not a plain counter — with `sweep_adv` = 1 the serialiser reads
the *next* row for the last 1/16 of each row — so slots must rotate on the true
row counter `pix_row_base`. `tools/sim_linebuf3.py` now checks every cell with
that row flip modelled.

**What the third slot proved:** with the stream paused the pipelined reader
fits (fill ~5,880 against 8,192); streaming, it does not (7,700–8,450). A
buffer absorbs *jitter*; it cannot fix an *average* deficit. Only more reads
per clock could.

### 3.6 Block fetch (`EXP_BLOCK=4`) — and the pairing detour — 1.42.0 to 1.45.0

**The idea.** Read K columns of the two groups that share a DRAM row, then K
of the other two, and let the arbiter hold the port for each run (`mem_last`,
"sticky"). A run is 2K reads with one row open. The row-miss model put it at
**0.36 reads per clock** for K = 4 (1.81x), against the 0.25 needed.

**The first hardware said it was slower.**

| Build | reader | `clk_div` 4, paused | `clk_div` 4, streaming | `clk_div` 5 |
|---|---|---|---|---|
| 1.41.0 (35 MHz) | pipelined | — | 25,478/s | 3.1/s |
| 1.42.0 (32 MHz) | block, pairs (0,1)(2,3) | — | **35,522/s** | **107/s** |
| 1.44.0 (35 MHz) | pipelined + 3 slots | fill **5,876** | fill 7,719, 100,875/s | 0 |
| 1.44.2 (35 MHz) | block (0,1)(2,3) + 3 slots | fill **6,784** | fill 7,698, 1,632/s | — |
| **1.45.0 (35 MHz)** | **block (0,2)(1,3) + 3 slots** | fill **3,105**, 0/s | fill **3,433**, **0/s** | 0 |

Block fetch with groups 0+1 and 2+3 paired was **15% slower** than not
blocking at all (6,784 against 5,876 paused). The reason was in the module's
geometry: its tiles sit at (128,0) (128,64) (0,0) (0,64), so it is groups **0
and 2** that share a framebuffer row, and 1 and 3. Pairing 0 with 1 made every
other read a row miss.

**The simulation had said 2.4x faster**, because it had been given an idealised
layout — (0,0) (128,0) (0,64) (128,64) — in which 0 and 1 *do* share a row.
Re-run with the module's real origins, `tools/sim_litedram.py` (the real
LiteDRAM core, not a model) reproduced the hardware: **6,256 vs 6,784 clocks
measured** for the mis-paired build, **6,255 vs 5,876** for the pipelined one.

**The fix:** derive the pairing at build time from the panel preset's
`group_origins` (`_block_pairs()` in `icn1065_soc.py`). Addresses still come
from the runtime offset CSRs, so a module with a different layout gets a slower
fill, never a wrong picture. With the right pairs the fill dropped to **3,105
clocks paused, 3,433 streaming** — 42% of the budget — and underruns went to
zero at every divider tried, including `clk_div` 3.

> **Check a simulation's inputs before trusting its model.** The model here was
> sound; it was fed the wrong geometry, and it confirmed the design that was
> meant rather than the one that was built.

### 3.7 Logic and timing: two fixes outside the pixel datapath

**~940 LUTs from one read port.** Migen's default read port registers the
*address*, which asks for write-first transparency, and yosys builds that as a
word-wide bypass mux. The fill never writes the slot being read, so the mux
guards a case that cannot happen. A `READ_FIRST` port plus a `no_rw_check`
attribute: the three-slot line buffer synthesised to **198 LUTs instead of
433** — 235 per output, ~940 across four.

The first attempt did nothing at all. A Memory **subclass** overriding
`emit_verilog` to add the attribute was silently ignored, because LiteX does
not call it; a test through yosys's command hook showed the attribute absent.
What works is `mem.attr = {("no_rw_check", 1)}` — LiteX emits that tuple
verbatim ahead of the memory declaration, where yosys reads it.

**15 MHz from registering three multipliers.** The group offsets are CSR
*expressions* — `y × 16 × width + x × 16` — i.e. multipliers, and in the block
issue path yosys shared one between the two pairs by muxing its operands on the
phase bit. That put the DSP, and the row product the FSM gated, behind the SDRAM
command acknowledge. They only change when firmware rewrites the CSRs, so they
became registers: the four offsets, the four address steps, the row product
(`rowbase_r`, recomputed every cycle from registered inputs) and the second
phase's first address (`first1`). The issue address itself is a register
stepped by constants.

| Block-fetch builds | LUT4 | system clock (35 MHz target) |
|---|---|---|
| first, combinational issue address | 23,129 | 30.2–32.9 MHz |
| issue address registered | 23,509 | 32.0–33.1 MHz |
| + three slots | 23,796–23,998 | 29.1–31.3 MHz |
| + `READ_FIRST` / `no_rw_check` | 22,734–22,785 | 31.8–34.0 MHz |
| **+ offsets, steps, row product registered** (1.44.2) | **22,387** | **47.4–49.8 MHz** |
| + crossbar counters (1.44.3) | 22,879 | 49.2–50.1 MHz |
| + correct pairing (1.45.0) | 22,864 | 48.3–49.4 MHz |
| + 486-pixel wide chunk (1.46.0, shipped seed) | 22,969 | 44.2 MHz |

### 3.8 The last 5 fps: the wire, not the memory — 1.46.0

Every measurement above streamed a 256x256 frame (the two fitted modules). The
first full four-module frame — 256x512, 270 chunks — needed the wide `'B','W'`
header, and its 487-pixel chunk made a 1501-byte datagram: one byte over the
MTU. The sender fragmented every chunk, the card parsed only the first
fragment, and the last pixel of every chunk was never written — a fixed zigzag
of stale dots with **every card counter at zero**. Fragmenting also doubled the
packet count and held the stream to 15 fps. A 486-pixel wide chunk fixed both:
0 fragments (from ~5,400 a second), **20 fps**. The read side was unaffected
throughout — 66.7 Hz, 0 underruns. Full account in [ARCH.md §4](../ARCH.md).

## 4. What made it measurable

| Tool | What it answered |
|---|---|
| `underruns` | is any pixel wrong on the glass right now (output 0; measure deltas — it reads ~121 after every boot) |
| `perf_fill_cycles` / `perf_fills` | how much of the row budget a fill takes — the number every step was judged by |
| `perf_cmds` / `perf_stall` | how hard the display port is being driven, and how long commands wait |
| `xbar` | who else is using the SDRAM (nobody, it turned out — §3.4) |
| a `clk_div` sweep | the margin: which divider is the first clean one |
| `tools/sim_fillrate.py` | that a new reader stores **exactly** the same pixels as the shipped one, cell for cell, under a modelled memory |
| `tools/sim_litedram.py` | the fill rate against the **real** LiteDRAM controller, P1.25 geometry by default |
| `tools/sim_linebuf3.py` | that the three-slot buffer returns the right cell with the `sweep_adv` row flip modelled |

The rule the simulators taught twice: **a simulation is only as good as its
inputs.** One with idealised group origins predicted 2.4x for a change that was
15% slower on hardware; the same simulation with the real origins matched the
hardware within ~8%.

## 5. What did not work, or was not needed

| Tried | Outcome |
|---|---|
| BIOS console `disable` | card silent after configuration; `lite` keeps the BIOS on the network |
| 38.3 / 40 MHz system clock (four outputs) | all ten builds failed timing (25.0–35.5 MHz); would not have reached 66.7 Hz anyway (§3.2) |
| half-rate SDRAM | closed timing, card never reached the network |
| pipelined reader alone | fixed latency, not throughput (§3.3) |
| three slots keyed on `pix_row` | wrong rows — underruns with nothing streaming (§3.5) |
| block fetch paired (0,1)(2,3) | 15% slower than no blocking (§3.6) |
| Memory subclass `emit_verilog` | ignored by LiteX (§3.7) |
| seeds | several builds missed the gigabit RX clock by a little (e.g. 124.6 against 125 on 1.46.0 seed 3) — try seeds before changing anything |

## 6. Result

Gateware 1.46.0 / firmware 3.9.0, built by `exp/build-out4.sh`:

| | |
|---|---|
| fit | **55/56 EBR, 22,969 / 24,288 LUT4 (94%)** |
| timing | system **44.2 MHz** against 35, gigabit RX **128.3 MHz** against 125 (seed 7) |
| row fill (1.45.x; the read path is unchanged in 1.46.0) | **3,105** clocks paused, **3,433** streaming 256x256, ~2,650 streaming 256x512 — against **8,192** |
| underruns | **0** at `clk_div` 3, 4, 5, 6 and 8, streaming and paused |
| refresh | **66.7 Hz** at `clk_div` 4 — the three-output rate |
| full four-module stream | 256x512, 270 wide chunks of 486 px, **20 fps**, 0 IP fragments; 30-minute soak below |
| connectors | all four lit on glass (two modules, moved J1/J2 → J3/J4) |
| 5-minute soak, 256x256 stream (1.45.0) | 0 underruns, 0 dropped chunks, 0 held frames, 66.67 Hz, 20.00 fps |

**30-minute soak, full four-module stream (1.46.0 / firmware 3.9.0), `clk_div`
4.** Ten three-minute windows of a 256x512 video show filling all four module
regions, modules on J3/J4:

| window (180 s) | refresh Hz | underruns | dropped chunks | held frames | fps | chunks/s |
|---|---|---|---|---|---|---|
| 0 | 66.95 | 0 | 0 | 0 | 19.55 | 5,342 |
| 1 | 66.58 | 0 | 1 | 0 | 19.87 | 5,371 |
| 2 | 66.59 | 0 | 0 | 0 | 19.97 | 5,391 |
| 3 | 66.67 | 0 | 11 | 1 | 19.99 | 5,400 |
| 4 | 66.90 | 0 | 0 | 0 | 20.09 | 5,421 |
| 5 | 67.03 | 0 | 0 | 0 | 20.10 | 5,427 |
| 6 | 66.67 | 0 | 0 | 0 | 20.00 | 5,400 |
| 7 | 66.35 | 0 | 0 | 0 | 19.91 | 5,374 |
| 8 | 67.03 | 0 | 0 | 0 | 20.11 | 5,429 |
| 9 | 66.73 | 0 | 0 | 0 | 20.02 | 5,405 |

**0 underruns in every window**; refresh 66.35-67.03 Hz per window (66.67
nominal — the spread is the measurement's HTTP timing, not the panel);
19.55-20.11 fps, ~5,340-5,430 chunks/s (270 x 20 = 5,400); **0 IP fragments**
on the sender and 0 bad magic. 12 dropped chunks in total, 11 of them in one
window from a single host-side burst (`arrive_run_max` 12, `c_fifo_max` 2,049
of 2,048 — the known sender-descheduling burst, not the card); that burst's
frame was held for one 50 ms tick instead of shown torn: **1 held frame in
~36,000**.

## 7. Remaining limits and open items

- **The fill counters watch output 0 only**, and so does `underruns`. The
  round-robin arbiter keeps every output within one run of the others, so
  output 0 is representative — an argument, not a measurement.
- **Host packet bursts.** At 5,400 packets/s an occasional train from the
  sender overruns the card's receive FIFO (runs of 7 and 12 measured; 12
  chunks lost in a 30-minute soak, 11 of them in one burst). The
  whole-frame swap gate turns each into a held frame rather than a torn band,
  but the fix belongs in the sender's pacing.
- **Four modules lit at once** has not been seen — the bench owns two — so the
  simultaneous power budget is unmeasured.
- **Headroom.** The fill uses ~40% of the row budget at `clk_div` 4 and stays
  clean at `clk_div` 3. Five outputs would need 25 EBR of line buffer against 21
  left: four is the ceiling for this design on this part, and the next step up
  is more cards, not more outputs ([SCALING.md](SCALING.md)).
