# A beginner's guide to the FPGA code

You do not need to know Verilog to work on this project, and you will not write
any. But `gateware/` is 2,300 lines of Python that *becomes* hardware, and it
reads like nothing else in the repo until someone explains the trick.

This page is that explanation. It assumes you can read Python and know what a
register is. It does not assume you have used an FPGA.

| | |
|---|---|
| **Concepts** | [What an FPGA is here](#1-what-an-fpga-actually-is-here) · [Reading Migen](#2-reading-migen-the-whole-trick) · [Clock domains](#3-one-clock-and-why-you-cant-just-raise-it) |
| **The design** | [The SoC](#4-the-soc-in-one-page) · [HUB75 driver](#5-the-hub75-driver) · [Pixel DMA](#6-the-pixel-dma) |
| **Doing things** | [Add a CSR](#7-recipe-add-a-register) · [Add a peripheral](#8-recipe-add-a-peripheral) · [Build + timing](#9-building-and-reading-the-timing-report) |
| **When stuck** | [Debugging](#10-debugging-hardware-you-cannot-printf) · [Traps](#11-traps-specific-to-this-design) |

---

## 1. What an FPGA actually is here

An FPGA is a chip full of **unconfigured logic** — lookup tables, flip-flops,
block RAMs, wires — that you arrange into a circuit by loading a **bitstream**.
There is no processor inside it until you *put* one there.

That is the first thing to internalise: **in this project we built the CPU.**
The VexRiscv running the Rust firmware is not a chip on the board. It is a RISC-V
core described in software, compiled into the FPGA fabric alongside the Ethernet
MAC, the SDRAM controller and the HUB75 driver, all of which we also placed.

```
     gateware/*.py                                     the board
  ┌──────────────────┐   LiteX    ┌──────────┐  nextpnr  ┌─────────────┐
  │ Python describing│──────────▶ │ Verilog  │──────────▶│  bitstream  │
  │    a circuit     │  (Migen)   │          │  yosys    │   .bit      │
  └──────────────────┘            └──────────┘           └─────────────┘
                                                                │
                                                          JTAG or flash
                                                                ▼
                                                        ECP5 becomes: a
                                                        CPU + MAC + SDRAM
                                                        ctrl + HUB75 driver
```

**Two things get built, and confusing them wastes hours:**

| | Built by | Contains | Changing it needs |
|---|---|---|---|
| **Gateware** (`.bit`) | `gateware/*.py` → LiteX | the CPU, MAC, HUB75 driver | a bitstream rebuild, ~10 min |
| **Firmware** (`boot.bin`) | `sw_rust/` → cargo | the Rust program the CPU runs | a firmware build, ~10 s |

Panel size lives in the **gateware** because it sets shift-register timing.
Panel *arrangement* lives in firmware config because it is just addresses. That
asymmetry is the source of most "why didn't my change take effect".

---

## 2. Reading Migen: the whole trick

The gateware is written in **Migen**, a Python library where you build an object
graph that is then emitted as Verilog. The code you read is **not executed on
the board** — it runs once, at build time, to *describe* a circuit.

Once you have that, everything else follows.

### `self.comb` is a wire. `self.sync` is a flip-flop.

```python
self.comb += outputs_common.oe.eq((brightness_counter == 0) | (counter != 0))
```

Read as: *there is a wire called `oe`, permanently driven by that expression.*
Not "compute this once" — **continuously, forever, at the speed of light through
the gates.** Change an input, the output changes.

```python
self.sync += [
    If(counter != 0,
       counter.eq(counter - 1)),
]
```

Read as: *on every rising clock edge, if `counter` is nonzero, it becomes
`counter - 1`.* This is a register. One update per clock, 40 million times a
second.

> **`.eq()` is not `=`.** It builds an assignment *node* in the graph. And `==`
> inside a Migen expression builds a comparator — a piece of hardware — not a
> Python bool. Using a Migen signal in a plain `if` is a common beginner bug: it
> is always truthy, so the branch is taken at *build* time and the condition
> never exists in hardware.

### `Signal()` is a bundle of wires

```python
counter = Signal(max=counter_max)   # just wide enough to hold counter_max
row_active = Signal(row_bits)       # exactly row_bits wide
```

Width matters — it is physical. `Signal(max=8)` is 3 bits, and 3 bits wrap at 8
with no error, which is how you get a counter that silently resets.

### `FSM` is a state machine, and it is where the logic lives

```python
self.submodules.fsm = fsm = FSM(reset_state="RST")
fsm.act("WAIT",
        If((brightness_counter == 0) & shifting_done & enable,
            NextValue(counter, counter_max - 1),
            NextState("LATCH")))
```

`fsm.act("X", ...)` says *while in state X, do this*. `NextState` moves on next
clock; `NextValue` is a registered assignment. Everything inside an `act` is
**combinational within that state** unless it is a `NextValue`.

Note `&` and `|`, not `and` and `or`. Python's `and`/`or` short-circuit and
return one operand; Migen needs bitwise operators to build gates.

### `self.submodules` is instantiation

```python
self.submodules.common = FrameController(...)
self.submodules.specific = RowController(...)
```

This places a copy of that circuit. **Two `submodules` entries are two physical
copies** consuming two lots of fabric — loops that instantiate per output really
do multiply the hardware.

### A CSR is the bridge to the CPU

```python
self.ctrl = CSRStorage(fields=[
    CSRField("indexed", description="Display an indexed image"),
    CSRField("enabled", description="Enable the output"),
    CSRField("width",   description="Width of the image", size=16),
])
```

| Type | Direction | Read as |
|---|---|---|
| `CSRStorage` | **CPU writes**, gateware reads | a control register |
| `CSRStatus` | gateware writes, **CPU reads** | a counter or a status flag |

`AutoCSR` on the class makes LiteX collect them, assign addresses, and emit them
into the SVD — which becomes the Rust PAC. So `self.ctrl.fields.enabled` in
Python is `hub75.ctrl().write(|w| w.enabled().set_bit())` in Rust, and the
plumbing between is generated.

> **This is why the PAC must be regenerated after any gateware change**
> (`./build.sh bitstream pac firmware`). Add a CSR, skip the PAC, and the
> firmware is compiled against the *old* register map: it writes the right value
> to the wrong address. The symptom looks like broken hardware.

---

## 3. One clock, and why you can't just raise it

Everything runs in one domain, `sys`, at **40 MHz**, from a PLL in `_CRG`.

40 MHz is slow for an ECP5, and raising it is the obvious first optimisation.
**It has been tried, and it fails twice over:**

- **Timing barely closes.** At a 60 MHz constraint the design made 61.15 MHz —
  and the *first* placement attempt failed outright at **51.56 MHz**. Place-and-
  route is randomised; a design that only sometimes meets timing is a design
  that sometimes ships broken.
- **The HUB75 shift clock is literally `sys_clk / 2`**:
  ```python
  clk.eq(buffer_counter[0])     # hub75.py
  ```
  At 60 MHz the panels get 30 MHz, past the ~20 MHz they tolerate. **Confirmed
  on hardware: visible artifacts.**

And the divider cannot simply be widened, because the pixel data path is
*indexed* on `buffer_counter[0]`. A /4 clock needs the pipeline reworked, and
60/4 = 15 MHz would be **slower than today**.

The clock cannot move until the HUB75 shift is decoupled with a proper CDC
(clock-domain crossing). That is a real project, not a constant edit.

> **`SYS_CLK_HZ` in `network.rs` must match `sys_clk_freq` in `colorlight.py`.**
> Every timer deadline in the firmware derives from it. A mismatch silently
> rescales all of them and nothing reports an error.

---

## 4. The SoC in one page

`gateware/colorlight.py` assembles everything. The interesting lines:

```python
platform = colorlight_5a_75e.Platform(revision=revision)   # pins, for this board
SoCCore.__init__(self, platform, sys_clk_freq,
                 cpu_variant="lite",   # 'minimal' has NO external interrupts
                 ...)
flash = GD25Q32(Codes.READ_1_1_1)                          # the real part (§HARDWARE)
sdram_cls = M12L16161A
self.submodules.hub75 = hub75.Hub75(...)                   # the display
self.submodules.pixdma = Hub75UdpDma(...)                  # pixels: wire -> SDRAM
self.irq.add("ethmac", use_loc_if_exists=True)             # IRQ #2
```

`cpu_variant="lite"` is load-bearing: **`minimal` does not support external
interrupts**, and the whole network stack runs in one. `lite` also turns out to
carry hardware multiply/divide — see HARDWARE.md, it was unused for months.

Everything hangs off a **Wishbone** bus, LiteX's interconnect. Peripherals are
memory regions; the memory map in [ARCH.md](../ARCH.md#10-memory-map) is
literally what the bus decoder was told.

> **Region origins must be aligned to their own size**, or
> `SoCRegion.decoder()` raises `SoCError`. Growing the flash window from 2 MB to
> 4 MB therefore forced its origin to move. This is the kind of constraint that
> looks like a mysterious build failure until you know it exists.

---

## 5. The HUB75 driver

`gateware/hub75.py`, 576 lines, and the most interesting hardware here.

### What a HUB75 panel actually is

It is **not** addressable memory. It is a pair of long shift registers with no
brightness control and no memory of what you sent. To show a picture you must,
continuously and forever:

1. shift a row of pixels in, one clock per pixel
2. assert `LAT` to latch them
3. drive the row-address lines to select which row lights
4. assert `OE` for as long as that row should be lit
5. move to the next row

Stop doing this and the panel goes dark. A 1/32-scan panel lights **one row pair
at a time** — the picture you see is persistence of vision at a few hundred Hz.

### Brightness: BCM, not PWM

LEDs are on or off; there is no analogue level. The standard trick is PWM, but
this design uses **binary code modulation** — cheaper and the reason the FSM
looks the way it does.

Show bit 7 of every pixel for 128 ticks, bit 6 for 64, bit 5 for 32… Each bit
plane is a full shift-and-latch pass, weighted by its place value. Eight passes
reconstruct 8-bit brightness with **8 passes instead of 256**.

In `FrameController`:

```python
NextValue(brightness_counter, (1 << brightness_bit) * brightness_psc)
...
If(brightness_bit != 0,
    NextValue(brightness_bit, brightness_bit - 1))
```

`(1 << brightness_bit)` *is* the place value. `brightness_psc=16` is a
prescaler that stretches every plane, trading refresh rate for a longer
dimmest-plane window — LEDs do not respond well to extremely short pulses.

`oe` is driven so the row is lit while `brightness_counter` runs down:

```python
self.comb += outputs_common.oe.eq((brightness_counter == 0) | (counter != 0))
```

(HUB75 `OE` is active-low, so this expression *disables* output during shifting
and during the latch settling window — which is what `counter` counts.)

### Gamma

```python
gamma_lut.append(int(pow(i / max_in, gamma) * max_out + 0.5))   # gamma = 2.8
```

Perceived brightness is not linear in LED current. Without this, the bottom of
every ramp is a cliff and the top is flat. The table is computed in Python at
build time and becomes a ROM in the fabric — **free at runtime**, which is the
whole appeal of doing it in hardware.

### The pipeline, module by module

```
  SDRAM
    │  LiteDRAMDMAReader (16 deep)
    ▼
  RamToBufferReader ◀── RamAddressGenerator  (which pixel is this, given
    │                                          panel position + rotation?)
    ▼
  RowModule          (assemble a row; palette lookup if indexed)
    │
    ▼
  Output ── RowColorOutput × 3 per output × n_outputs   (R, G, B pin drivers)
    │
    ▼
  HUB75 connectors J1..J6
```

- **`RamAddressGenerator`** is where the panel map lives. It turns "output 3,
  chain slot 1, row 12, column 40" into a framebuffer word address, applying
  that panel's x/y offset and 90° rotation from its CSR. The `_16` in the CSR
  field descriptions is real: positions are stored in multiples of 16 to keep
  them 8 bits wide.
- **`RowModule`** does the indexed-mode palette lookup, from
  `palette_memory` — a block RAM the CPU writes.
- **`Output`/`RowColorOutput`** instantiate **three** colour drivers per output,
  per the loop in `Output.__init__`. Six outputs is 18 of them. This is why
  `n_outputs` costs fabric.

### The double-buffer CSR

```python
self.fb_base = CSRStorage(fields=[
    CSRField("offset", description="Framebuffer base address in 32-bit words", size=20),
], reset=sdram_offset)
```

One register, and it is the entire double-buffering mechanism: the CPU renders
into the buffer that is *not* being displayed, then writes the other address
here. The switch is atomic because it is a single register write.

`refresh_count` counts completed refreshes, and exists as **instrumentation**:
the display DMA re-reads the whole framebuffer every refresh, so the achieved
refresh rate tells you how much SDRAM read bandwidth you are actually getting —
which is what decides whether a write DMA has any headroom.

---

## 6. The pixel DMA

`gateware/dma_writer.py` — `Hub75UdpDma`. The single biggest architectural
decision in the project.

**It takes the LiteEth UDP sink directly and writes SDRAM, in hardware.** The
CPU never sees a streamed pixel — and since v1.38.0 it does not see the packet
either: `SmolEthPixelClassifier` kills pixel packets on the CPU branch before the
MAC can raise a receive event, so there is not even an interrupt.

What the CPU still does is *presentation*: it reads the arrival bitmap this
module publishes to know which chunks landed. The **buffer swap** is the
gateware's job too (`swap_req` here, `fb_base_eff` in `hub75.py`) — a CPU-timed
swap leaves this module writing the incoming frame into the half about to be
displayed.

```python
self.limit    = CSRStorage(24, description="Pixels in the image; writes past this are dropped")
self.ctrl     = CSRStorage(fields=[CSRField("enable", ...)])
self.pixels   = CSRStatus(32, description="Pixels written")
self.chunks   = CSRStatus(32, description="Chunks accepted")
self.bad_magic= CSRStatus(32, description="Payloads rejected on magic")
self.stalls   = CSRStatus(32, description="Payloads abandoned after a DRAM stall")
```

Two of those are worth dwelling on, because they encode hard-won lessons:

- **`limit`** drops writes past the image. Without it a malformed length walks
  off the end of the framebuffer into whatever is next.
- **`stalls`** counts payloads abandoned when DRAM back-pressures. The
  alternative — blocking — stalls the UDP sink, which back-pressures the MAC,
  which overflows the FIFO. **Dropping a payload is recoverable; wedging the
  receive path is not.**

`base` is a `CSRStatus`, not `CSRStorage` — *derived*, read-only. It is computed
from the display's `fb_base` so the write DMA always targets the back buffer.
Making it independently writable is exactly how you get the display and the
write DMA pointed at the same buffer, which tears.

> **The DMA boots disabled**, and that is the most common streaming fault in the
> whole system. On the CPU path the receive ISR cannot drain the MAC FIFO, so
> roughly 3 chunks of every 12 are lost and **no frame ever completes** — the
> panel keeps showing its last image, which reads as *frozen*, not as *slow*.

---

## 7. Recipe: add a register

Say you want the firmware to set a brightness scale.

**1. Declare it** in `gateware/hub75.py`:

```python
self.brightness = CSRStorage(8, reset=255, description="Global brightness scale")
```

**2. Use it** in the circuit — it is just a signal:

```python
self.comb += something.eq(self.brightness.storage)
```

**3. Rebuild in the right order.** This is not optional:

```bash
./build.sh bitstream pac firmware
```

`bitstream` regenerates the SVD; `pac` turns that into Rust; `firmware` compiles
against it. **Skipping `pac` compiles the firmware against the previous register
map**, and the failure looks like broken hardware rather than a stale build.

**4. Use it from Rust:**

```rust
unsafe { (*pac::Hub75::ptr()).brightness().write(|w| w.bits(128)) };
```

**5. Load it.** A new bitstream means JTAG (`sram`) or flash (`flash-all`) —
a firmware-only reload will not do, because the register does not exist yet in
the gateware that is running.

> The SVD diff is worth checking: if it shows only timestamps, no CSRs changed
> and the PAC is genuinely untouched. That check is how you tell a real register
> change from noise.

---

## 8. Recipe: add a peripheral

```python
class MyThing(Module, AutoCSR):
    def __init__(self, some_bus):
        self.ctrl  = CSRStorage(fields=[CSRField("go", pulse=True)])
        self.count = CSRStatus(32)

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE", If(self.ctrl.fields.go, NextState("WORK")))
        fsm.act("WORK", NextValue(self.count.status, self.count.status + 1),
                        NextState("IDLE"))
```

Then in `colorlight.py`:

```python
self.submodules.mything = MyThing(...)
self.add_csr("mything")          # without this the CSRs are not addressable
```

`CSRField(..., pulse=True)` is the idiom for "do it once": the bit self-clears,
so the CPU writes a 1 and the hardware sees a single-cycle strobe. Without
`pulse`, a set bit means *keep doing it*, forever.

Need bus access to memory? Ask the crossbar for a port, the way the display does:

```python
read_port = sdram.crossbar.get_port(mode="read", data_width=32)
```

**Every port competes for the same SDRAM.** The display already re-reads the
entire framebuffer every refresh and is close to read-bandwidth bound — measured
contention against the CPU is ~3%, but a new bulk reader is a different matter.

---

## 9. Building, and reading the timing report

```bash
./build.sh docker                      # once: the whole toolchain in an image
./build.sh --panel 128x64 bitstream    # yosys + nextpnr, ~10 minutes
./build.sh bitstream pac firmware      # after ANY gateware change
```

All open-source: **yosys** synthesises, **nextpnr** places and routes,
**ecppack** makes the bitstream. No vendor tools, no licence server.

The firmware side has a `build.rs` doing two jobs: placing the linker scripts
where the linker will find them, and — when `BARSIGN_DEFAULT_CONFIG` is set by
`./build.sh --default-config` — reading a panel config file and emitting it as a
Rust constant. That is how a panel gets a layout without a network. It declares
`cargo:rerun-if-env-changed` and `rerun-if-changed` so changing the config
actually rebuilds, which a naive `include_str!` of a fixed path would not.

The number that matters at the end:

```
main_crg_clkout0   61.04 MHz  against a 40 MHz target      ✅ ~52% margin
eth_clocks0_rx    130.01 MHz  against 125 MHz              ✅
```

**"Achieved" must exceed "target".** If it does not, the design may work, may
glitch, or may work only on a cold day — the failure is timing-dependent and
utterly maddening to debug, so treat a timing failure as a hard build failure.

Note the 61 MHz achieved against a 40 MHz target does **not** mean you can run at
61: see §3. Clock headroom and system correctness are different questions.

> **Place-and-route is randomised.** A design close to its limit may pass and
> then fail on an unchanged rebuild. Comfortable margin is not a luxury.

---

## 10. Debugging hardware you cannot printf

There is no serial console on this board. Everything below exists because of it.

**CSR counters are your print statements.** Nearly every `CSRStatus` in this
design was added to answer one debugging question, and they are cheap — a
counter is a handful of flip-flops. When you cannot explain a behaviour, the
first move is usually to add a counter and expose it in `/api/status`.

**Instrument the thing you cannot see.** `refresh_count` measures achieved SDRAM
read bandwidth. `stalls` distinguishes "the network dropped it" from "DRAM
back-pressured". `bad_magic` separates "not our protocol" from "our protocol,
malformed". Each of these turns an argument into a number.

**Hardware chunk-arrival tracking**: a 256-bit bitmap in `Hub75UdpDma` sets a bit
as each chunk *finishes writing*. It exists because the CPU's idea of "frame
complete" can lie — it knows what it parsed, not what the DMA actually landed.

**Breadcrumbs survive reset.** `breadcrumb.rs` writes progress markers into
*uncached* SDRAM that survives `soc_rst`, so `prev_mark`/`prev_mcause`/`prev_mepc`
come back after a reboot. This found a bug that bisection had failed on outright.

**Check your clock before you trust your numbers.** `mcycle` (`csrr 0xB00`) is
not implemented on this VexRiscv and reads back **0**, so `/api/bench` has
reported all-zero cycle counts since the day it was written — under a comment
explaining why it uses `mcycle` rather than `TIME_MS`. `Timer0` is real hardware
and keeps counting inside the trap handler, which is where `TIME_MS` cannot
advance. It also wraps every second, so a measurement longer than one period
aliases to `elapsed mod 1 s` — time long things in chunks and sum the deltas. A
sweep once reported the slowest setting as the fastest for exactly this reason.

**Some CSRs are tunable at runtime, which lets you test a gateware change before
you build one.** `LiteSPIPHY`'s `clk_divisor` is a `CSRStorage` whose *reset*
value is `default_divisor`, so firmware can retune the SPI clock while running.
`/api/flashbench` uses that to checksum the whole flash image at every divisor
and prove a faster clock reads correctly — then the bitstream is rebuilt once,
with the answer already known. Look for this shape before committing to a
10-minute place-and-route to test a hypothesis.

**Simulate before you place.** Migen has a simulator, and a 10-minute
place-and-route round trip makes it worth using for FSM logic.

**Verify what is running.** Three stale artifacts turned up in a single day — a
bitstream older than its gateware, a `boot.bin` older than its sources, and TFTP
daemons seven months old serving February firmware. `build.sh` guards the first
two now. The firmware version is drawn on the panel at boot for exactly this
reason.

---

## 11. Traps specific to this design

**Three configuration layers must agree.** Bitstream geometry
(`--panel`/`--outputs`/`--chain-length`), firmware constants (`OUTPUTS`,
`CHAIN_LENGTH` in `hub75.rs`), and the runtime TFTP layout. A mismatch between
the first two **crashes the SoC** — the firmware addresses panel CSRs that do
not exist. Const asserts now catch the layout case at build time; before them,
growing the wall silently dropped connectors.

**`n_outputs` and `chain_length_2` cost real fabric.** Each output instantiates
three `RowColorOutput`s and its own panel-position CSRs. This is a loop in
Python that becomes replicated hardware.

**Chaining halves the refresh rate; outputs do not.** Outputs shift in parallel.
Prefer wide over deep.

**The pixel word is `0x00BBGGRR` on this hardware — and that is a panel
property, not a constant.** Measured with solid frames: the old `0x00GGRRBB` lit
green for red, blue for green and red for blue. The changelog records an older
fix in the opposite direction, so the two cannot both be right and the panel
wiring is what differs. Send three solid frames before trusting any colour. In indexed mode
the palette index is that same low byte. This has been rediscovered once per
file that writes pixels, and `artnet.rs` had red and blue swapped for two
releases after the others were fixed.

**The build is not hermetic against your own stale outputs.** `pac` between
`bitstream` and `firmware`, every time.

**A default in `build.sh` is a third place for the configuration to disagree.**
`CHAIN_LENGTH=2` sat there from v1.7.0 until 2026-09-17, long after `hub75.rs`
moved to 1 — so any plain `./build.sh bitstream` built a gateware the firmware
contradicted. It builds, it boots, it looks fine. The released bitstreams were
only correct because someone passed `--chain 1` explicitly. `build_bitstream()`
now reads the constant out of `hub75.rs` and refuses to build a mismatch; do the
same for anything else that exists in two places.

**`NextValue` lands at the END of the cycle, and one cycle is a visible bug.**
The framebuffer swap is requested at `hdr_idx == 9`, and `pix_adr` for the
incoming frame is computed in that same cycle. Using the register that the swap
is about to change put the new frame's **first chunk** in the buffer being
displayed — a glitch across the top of every frame, continuously. If a value is
changing this cycle and you need the new one now, compute it combinationally
(`write_base_next` in `dma_writer.py`) rather than reading the register.

**Two consumers of the same state must read the same signal.** Scan-out and the
pixel DMA both derive from `fb_base_eff`, never one from the CSR and the other
from a copy. An earlier version wrote the display base and the DMA base as two
CSR writes and left a window in which a chunk landed in the live buffer.

**Memory-mapped SPI flash is not memory.** The `spiflash` region is
`cached=False` with `READ_1_1_1`, so every byte read is its own 40-clock
command+address+data transaction: **1142 cycles per byte** at LiteSPI's default
divisor of 9, against 14 from DRAM. Anything that walks the flash linearly — a
CRC, a copy, a search — costs a thousand times what the same loop over DRAM
costs. `flashboot()` walks it three times and took 18 seconds doing it. If you
must read a lot of flash, raise `default_divisor` (§8c of the hardware notes) or
copy to DRAM once and work there.

---

## Where to go next

| To learn | Read |
|---|---|
| What we found out about this specific board | **[HARDWARE.md](HARDWARE.md)** |
| How the firmware is structured | [../ARCH.md](../ARCH.md) |
| What any of it costs | [BENCHMARKS.md](BENCHMARKS.md) |
| LiteX itself | [github.com/enjoy-digital/litex](https://github.com/enjoy-digital/litex) — the wiki, then `litex_boards` for worked examples |
| Migen | [m-labs.hk/migen/manual](https://m-labs.hk/migen/manual/) — short, and §2 above is most of what you need |
| HUB75 at the signal level | [Adafruit's protocol notes](https://learn.adafruit.com/32x16-32x32-rgb-led-matrix) |
