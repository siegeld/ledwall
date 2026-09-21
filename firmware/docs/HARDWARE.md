# The Colorlight 5A-75E — what we found

The 5A-75E is a $15 LED receiver card that happens to contain a Lattice ECP5, a
megabyte-class SDRAM, gigabit-capable PHYs and twelve HUB75 connectors. It is
sold to drive LED billboards from a proprietary sender card, and nothing about
it is documented for the use we put it to.

This page is what we learned taking one apart and making it ours. Most of it is
not in any datasheet, and several entries cost days.

New to the FPGA side? Read **[FPGA-GUIDE.md](FPGA-GUIDE.md)** first — it explains
how the gateware works. This page is about the *board*.

---

## 0. What this board is actually for, and why that matters

In its day job the 5A-75E is a **receiver card** in an LED billboard. A sender
card somewhere upstream takes HDMI, chops the picture into panel-sized pieces,
and blasts them over Ethernet to a rack of these. Each one drives its slice of
the wall and does nothing else. It has no firmware you would recognise, no
console, and no reason to be general-purpose.

Which explains almost everything odd about it:

| Because it is a receiver card | The consequence for us |
|---|---|
| It is mass-produced for billboards | it costs ~$15 and has an ECP5, SDRAM and a gigabit PHY on it |
| It only ever had to receive and shift | **no serial console**, no debug header, no documentation |
| Its firmware is a trade secret | **nothing is documented** — the pinout was reverse-engineered by the community |
| It ships in volume from several factories | **the parts fitted are not guaranteed** (see §1) |
| It was never meant to be reprogrammed | JTAG is unpopulated pads, not a header |

We are using it far outside its intended role — running a CPU on it, serving
HTTP from it, executing user programs on it. That works remarkably well, and the
places it bites are the places the original design had no reason to care.

> **The community did the hard part.** The pinout, the revision differences and
> the basic LiteX platform come from the open-source work around
> `litex_boards`'s `colorlight_5a_75e` platform and Tomu/q3k's earlier
> reverse-engineering. Without it none of this would be approachable.

---

## 1. What is actually on the board

| Part | Marked / declared | **What the silicon says** |
|---|---|---|
| FPGA | Lattice ECP5-25F | `LFE5U-25F-6BG256C`, idcode `0x41111043` |
| SDRAM | — | `M12L16161A`, 2M × 16 bit |
| SPI flash | *declared* `GD25Q16` (2 MB), then `W25Q32JV` | **`GD25Q32`** — JEDEC `c8 40 16` |
| Ethernet PHY | — | `RTL8211FD`, RGMII |
| System clock | — | 40 MHz (from a PLL; see §6) |

### The flash is a GigaDevice, and the declared part was wrong twice

Reading the JEDEC ID over JTAG returns `c8 40 16`. Manufacturer `0xc8` is
**GigaDevice**, not Winbond (`0xef`); capacity `0x16` is 4 MB. So the part is a
**GD25Q32**.

The gateware originally declared a `GD25Q16` — right vendor, **half the size**.
The first correction swapped it for a `W25Q32JV` — right size, **wrong vendor**.
Both are 4 MB with 256-byte pages and the same `READ_1_1_1` opcode, so the
mistake was harmless in practice and therefore invisible for a long time. It is
now declared as what it is.

> Older rev 6.1 boards genuinely do carry a 2 MB `GD25Q16`. If you have one,
> swap the module back — and move the region origin with it (§2).

### Revisions are not interchangeable

The gateware takes `--revision`, defaulting to **7.0**, with 6.1 and 8.0 also
known. **This bench runs 8.2.** The differences are in pinout and fitted parts,
so the wrong revision produces a bitstream that loads happily and drives
nothing — or drives the wrong pins.

| Revision | Notes |
|---|---|
| 6.1 | 2 MB flash (`GD25Q16`); older pinout |
| 7.0 | the LiteX default |
| 8.0 / 8.2 | what we use; 4 MB flash, 6 HUB75 connectors |

**Check the silk screen before building anything.** The revision is printed on
the board. Guessing from appearance does not work — the boards look nearly
identical.

> **Same revision does not guarantee the same parts.** These come from volume
> production; vendors substitute. The flash is the known case, and the way to
> settle it is to read the JEDEC ID over JTAG rather than to trust any label —
> including the one in our own gateware, which was wrong twice.

---

## 2. Memory map, and a LiteX alignment rule that bites

| Region | Address | Size |
|---|---|---|
| ROM (BIOS) | `0x00000000` | 64 KB |
| SRAM | `0x10000000` | 8 KB |
| Main RAM (SDRAM) | `0x40000000` | 4 MB |
| EthMAC buffers | `0x80000000` | 20 KB — 8 RX + 2 TX slots × 2 KB |
| SPI flash (mapped) | `0x80400000` | 4 MB |
| Flash boot address | `0x80500000` | = chip offset `0x100000` |
| CSRs | `0xF0000000` | 64 KB |

> **A region's origin must be aligned to its own size**, or
> `SoCRegion.decoder()` raises `SoCError`. The flash lived at `0x80200000`,
> which is fine for a 2 MB part (`0x80200000 % 0x200000 == 0`) and *illegal* for
> a 4 MB one. Growing the declared flash therefore forces the origin to move —
> to `0x80400000`, clear of ethmac below and `main_ram_uncached` at `0x90000000`.
>
> The **chip** offset the boot address maps to is unchanged at `0x100000`, so
> how firmware gets written to flash does not change. Only the window moves.

The RX slot count must be a **power of two** — a LiteEth wishbone SRAM decoder
constraint. `NRXSLOTS` in `ethernet.rs` must match `nrxslots` in the gateware or
the firmware reads slots that are not there.

---

## 3. The framebuffer is four times bigger than we thought

For months the code, the notes and `CLAUDE.md` all agreed the framebuffer was
**65,536 words per buffer** — "exactly 8 panels of 128×64, with no headroom".

It is **262,144 words per buffer**, 524,288 total. `FB_TOTAL_WORDS =
0x00400000 / 2 / 4`. Sixteen panels of 128×64 is 131,072 pixels: **half of one
buffer**.

**Panel count has never been limited by memory.** The arithmetic was corrected
in `TODO.md` on 2026-09-09; the correction did not reach the code comments or
`CLAUDE.md` until 2026-09-14, and in between it was quoted as a constraint in
design discussions. A wrong number in three places is indistinguishable from a
verified one.

### The pixel word is `0x00BBGGRR` on this hardware — verify it on yours

**Measured end to end on the bench panel, 2026-09-18.** With the old
`0x00GGRRBB`, a solid RED frame lit GREEN, solid GREEN lit BLUE and solid BLUE
lit RED — one cyclic rotation, repeatable. Repacking as `B<<16 | G<<8 | R` gives
red/green/blue correctly, confirmed with a three-band test frame.

> **The history says the opposite, and that is worth knowing before you trust
> either.** The v1.9-era changelog records a fix in the *other* direction —
> "green showed as red, red as blue, blue as green" — standardising on
> `0x00GGRRBB`. Same framebuffer, same scan-out, so both cannot be right for the
> same panel: these panels must differ in channel wiring from whatever that was
> written against. HUB75 panels are not consistent about RGB pin order.
>
> So treat the word format as a **property of the panel in front of you**, not a
> constant. Send three solid frames before believing any colour.

The scan-out reads red from `dat_r[16:24]`, green from `[0:8]` and blue from
`[8:16]` (`gateware/hub75.py`), which with this panel's wiring comes out as
`0x00BBGGRR` at the glass.

Not `0x00RRGGBB`. Green is the high byte.

This has been re-discovered independently at least four times, once per file
that writes pixels — `patterns.rs`, `bitmap_udp.rs`, and then `artnet.rs`, which
was still packing `0x00BBGGRR` two releases after the others were fixed. Art-Net
had red and blue swapped the whole time.

**In indexed mode the palette index is the low byte** of that same word
(`0x000000II`). That is what makes indexed mode free downstream — the scan-out
path and the framebuffer format do not change at all.

---

## 4. The CPU is better than we were using

The SoC uses `cpu_variant="lite"` — chosen because **`minimal` does not support
external interrupts**, which this design needs.

### It has always had hardware multiply and divide

`lite` reports `-march=rv32i2p0_m`. The firmware was being built for plain
`riscv32i`, so **every multiply and divide was a software routine** — for
months, in every program, on every pixel.

```
-C target-feature=+m      0 → 184 hardware mul/div instructions,  6× on a program
```

Switching to the `riscv32im` *target* does not build (atomics). The target
feature flag on the `riscv32i` target is the fix.

### There is no D-cache and no FPU

| | |
|---|---|
| I-cache | yes |
| **D-cache** | **no** — every data access is a bare bus round trip |
| **FPU** | **no** — floats are soft-float library calls |

**A single memory access costs ~190 cycles.** Read or write, it makes no
difference. This one number explains essentially all of the panel's performance
behaviour: a full 128×128 redraw is ~3.1M cycles, which is ~12 fps at 40 MHz
*before your code does anything at all*. See [BENCHMARKS.md](BENCHMARKS.md).

The absent FPU is the more dangerous of the two, because it fails quietly: one
gauge calling a float helper per pixel took a panel **under 0.2 fps**, which
reads as *hung* rather than slow.

---

## 5. HUB75 outputs

Rev 8.2 exposes **six** connectors (J1–J6); the SoC supports up to 16. Each
connector can drive a chain, and each panel position is a CSR holding x (8-bit,
×16), y (8-bit, ×16) and a 2-bit rotation.

| Connector | Output index | Chain slots |
|---|---|---|
| J1 … J6 | 0 … 5 | `[0]`, `[1]` |

> **Prefer wide over deep.** Outputs shift in **parallel** and cost nothing
> extra. Chaining panels off one connector **halves the refresh rate** per panel
> added. Six panels on six connectors refresh at twice the rate of six panels
> chained in pairs on three.

Panel geometry (columns, rows, scan, chain length, output count) is **baked into
the bitstream** — it sets the shift-register timing and is not a runtime
setting. The *arrangement* of those panels into a grid is runtime, from the TFTP
config. Firmware constants `OUTPUTS` and `CHAIN_LENGTH` in `hub75.rs` must match
the gateware exactly, or the firmware addresses CSRs that do not exist and the
SoC crashes.

---

## 5b. Ethernet: an RGMII delay that is not optional

The PHY is an **RTL8211FD** on RGMII. One line in `colorlight.py` is load-bearing:

```python
self.submodules.ethphy = phy = LiteEthPHYRGMII(
    clock_pads=..., pads=...,
    tx_delay=0e-9,
    rx_delay=2e-9,          # critical for stable operation
)
```

RGMII clocks data on both edges, and the receive clock has to land in the middle
of the data eye. **2 ns is what works on this board**; the trace lengths decide
it, so it is a property of the PCB, not of the PHY.

Get it wrong and the link comes up, negotiates, and passes *some* traffic — with
CRC errors under load. That failure mode is indistinguishable from a bad cable
or a noisy switch, so it is worth knowing the constant exists before you go
looking for one.

`ETH_PHY_NO_RESET` is also set: the PHY's reset line is not wired the way LiteX
expects, and asserting it would take the link down permanently.

### Why `add_ethernet()` was not enough

LiteX's `add_ethernet()` builds the MAC for you, and this design does **not** use
it. `SmolEth` (`gateware/smoleth.py`) builds the same thing by hand.

The reason is a single ordering requirement: **the hardware UDP tap has to sit
*after* `LiteEthMACCore`**, which strips the preamble and checks CRC. That core
is buried inside `LiteEthMAC`, and `add_ethernet()` gives no access to it.
Without the tap there is no way to hand a validated UDP stream to the pixel DMA
in gateware — which is the whole basis of the streaming performance.

The CPU-visible side is deliberately unchanged: same Wishbone slots, same CSR
names, same IRQ. So the firmware cannot tell the difference, which is what makes
this a safe substitution rather than a fork.

### The MAC FIFO is eight slots, and that is the streaming budget

```python
nrxslots=8,      # must be a power of 2 (LiteEth wishbone SRAM decoder)
ntxslots=2,
```

Eight 2 KB receive slots. At streaming rates the ISR has roughly **8 ms** to
drain them before the ninth packet overwrites the first. That figure is the
entire justification for the fast-path filters in `network.rs`: a single
smoltcp slow-path packet costs ~500 µs, so ARP, multicast and stray UDP have to
be discarded *before* the stack sees them.

> `NRXSLOTS` in `ethernet.rs` must match `nrxslots` here, or the firmware reads
> slots that do not exist.

---

## 6. The system clock cannot simply be raised

60 MHz looked like a free 1.5× on everything. It is not, for two independent
reasons, both confirmed on hardware:

**Timing barely closes.** 61.15 MHz against a 60 MHz constraint — and the first
placement attempt failed outright at **51.56 MHz**. Not a margin to ship.

**The HUB75 shift clock is `sys_clk/2`, literally.** `hub75.py` does
`clk.eq(buffer_counter[0])`. At 60 MHz the panels are shifted at 30 MHz, past
the ~20 MHz these panels tolerate. **Confirmed: the image showed artifacts.**

And the divider cannot just be widened — the pixel data path is *indexed* on
`buffer_counter[0]`, so a /4 clock needs the whole pipeline reworked, and 60/4 =
15 MHz would be **slower** than today's 20 MHz.

The clock cannot move until the HUB75 shift is decoupled from `sys_clk` with a
proper CDC.

> `SYS_CLK_HZ` in `network.rs` must match `sys_clk_freq` in `colorlight.py`. It
> is the single constant that `TIMER_RELOAD`, `CYCLES_PER_MS` and the Timer0
> reload all derive from — a mismatch silently rescales **every deadline in the
> firmware** and nothing reports an error.

---

## 7. JTAG — and why `--detect` proves nothing

JTAG is a 4-pin header beside U33, with power on a separate 2-pin header:

| Pin | Signal | | Pin | Signal |
|---|---|---|---|---|
| J27 | TCK | | J33 | 3.3V |
| J31 | TMS | | J34 | GND |
| J32 | TDI | | | |
| J30 | TDO | | | |

### The FT2232 has two channels and they are different devices

`openFPGALoader` treats them separately: `ft2232` is channel A, `ft2232_b` is
channel B. Picking the wrong one does **not** report a bad cable — the adapter
opens, the frequency negotiates, and the chain scans `empty`, which is
indistinguishable from an unpowered board.

This cost most of a session, during which the board was declared dead and the
power supply was blamed. It was on channel B.

### A cheap USB Blaster silently corrupts bulk transfers

The Waveshare USB Blaster **passed `--detect` every single time** and corrupted
bulk JTAG transfers badly: two dumps of the same 1 MB of flash differed in
**29% of bytes** (BER ~9.7 × 10⁻²). It also silently ignored `--freq`.

It passed `--detect` because the first ~1037 bytes come back clean. This blocked
all flashing for **months**, presenting as a flash-boot problem rather than a
cable problem.

A Tigard (`--cable tigard`) gives byte-identical dumps, 4.6 s versus 36.3 s, and
honours `--freq`.

> **Acceptance test before trusting any programmer on this board: dump the same
> 1 MB twice and compare hashes.** `--detect` passing proves nothing.

### Verify flash with the board, not with the programmer

`openFPGALoader --verify` fails constantly on this bench, and it is lying. On one
write it failed seven times running at seven *different* offsets while the write
was correct every time. Six consecutive full dumps of the same 296 KB region
differed from the source by 0.54%, 1.25%, 1.75%, 4.39%, 7.85% and 8.77% — and
from each other. The write is fine; the **read** is what corrupts.

Dump-and-compare works, but only by retrying until one read happens to come back
clean, which can take many attempts and gets worse as the region grows. A
byte-wise majority vote across several dumps does **not** rescue it — the errors
are correlated enough that the vote converges on the wrong answer.

**Ask the board instead.** The FPGA's own LiteSPI path reads flash correctly —
`/api/flashbench` returns an identical checksum at every SPI clock from 2 MHz to
20 MHz, which is a strong statement that the reader is sound. So:

```bash
curl -s http://<board-ip>/api/flashbench | python3 -c \
  "import json,sys; d=json.load(sys.stdin); print(d['img_len'], d['sweep'][0]['chk'])"
```

and compute the same rotate-left-1-and-add over `.tftp/boot.fbi` locally, skipping
the 8-byte header. A match on both the header length and the payload checksum
proves the flash content byte for byte, using a reader that works, in seconds
rather than a dozen JTAG dumps.

The FBI header is **little-endian** (`<II`: length then crc32), which is the one
trap in doing this by hand.

### The Tigard's voltage switch decides whether JTAG works at all

**The same failure signature, from a *good* programmer in the wrong mode.**
Discovered 2026-09-16, and it will happen again to anyone who touches that
switch.

The Tigard is an FT2232H behind **directional level shifters**. Its controller
side is powered by USB; its **target side is powered by VTGT** — which is what
the board pad `J33` (3.3 V) feeds. The switch picks where VTGT comes from:

| Switch | `J33` wire | Result on this board |
|---|---|---|
| **`VTGT`** | **connected** | **Correct.** Byte-identical 4 MB dumps, verified writes, SRAM loads first attempt |
| `VTGT` | disconnected | **No JTAG at all** — shifters have no target-side supply, chain scans `empty` |
| `3V3` (self-powered) | disconnected | **The dangerous one.** `--detect` PASSES and returns the right IDCODE; every bulk transfer dies with `CRC ERR`. Lowering to `--freq 1000000` does not help |
| `3V3` | connected | **Do not.** Tigard drives the rail against the board's own regulator |

The third row is the trap, and it is the *same trap as the Waveshare above*:
a small transfer succeeds, so the link looks healthy, and the failure lands on
whatever you were trying to program. A known-good bitstream failed identically,
which is what proved it was the link rather than the image — **load a bitstream
you have loaded before whenever a CRC error appears, before suspecting your
own build.**

> **This collides with the cold-boot investigation** (§8b): `J33` must be
> **disconnected** for a POR test, because an unpowered adapter hanging on the
> 3.3 V rail is a variable, and **connected** to program. There is no single
> setting that does both — the self-powered mode looks like it should and does
> not work here. Connect to flash, disconnect to power-cycle.

---

## 8. Flash boot, and the simplest explanation

Flash boot "did not work" for a long time. The suspected cause was the
mis-declared flash size (§1), and that was fixed.

**But the simplest explanation fits the evidence better, and was never tested:
nothing had ever *written* the firmware to `FLASH_BOOT_ADDRESS`.** `build.sh`
had no target that did so until `flash-firmware` was added on 2026-09-06. Flash
boot cannot work if the firmware was never put where the BIOS looks for it,
whatever the flash module says.

Worth keeping as a pattern: an elaborate hypothesis was investigated and
partially fixed while a trivial one — *did we ever actually do the thing?* —
went unchecked.

### Write the bitstream before the firmware

`flash-all` does them in that order deliberately: writing the bitstream **may
erase sectors beyond its own length**, which would take the firmware at
`0x100000` with it.

### Firmware in flash needs an FBI header, or flashboot silently never runs

**Do not write `boot.bin` raw to `FLASH_BOOT_ADDRESS`.** The BIOS's
`check_image_in_flash()` expects `length` (u32), `crc32` (u32), then the image.
A raw binary makes the BIOS read the firmware's first RISC-V instruction as the
length — ours is `0x400000b7`, i.e. 1 GB — which fails its
`32 <= length <= 16 MiB` test. `flashboot()` prints *"Error: Invalid image
length"* and falls through to `netboot()`.

That is exactly what `flash-firmware` did from 2026-09-06 until 2026-09-17, so
**this board could never boot standalone** and always depended on the network.
`build.sh` now wraps the image with LiteX's `crcfbigen.py -f -l` and refuses to
flash an image whose header would not pass.

`netboot()` cannot compensate at power-on: **one attempt, no link wait, no
retry**. The PHY is still negotiating, ARP never resolves, nothing is
transmitted — so the TFTP server sees no request at all. The BIOS then idles,
and `set_idle_hook(udp_service)` answers pings once link appears. After a JTAG
`REFRESH` the PHY already has link, so netboot works. **That, and not any
configuration fault, is the whole "boots on REFRESH, never on power-on" story.**

**Since v1.36.0 the BIOS tries netboot *before* flash** — see `ARCH.md` §1 — and
that one-shot, no-link-wait netboot is now the thing that makes the cold path
work: it fails in a couple of seconds and flashboot catches it. Do not "fix"
netboot to wait for link; you would stall every cold boot behind PHY
negotiation. Both halves have to stay in step: `deploy` changes what a panel
runs now, `flash-firmware` changes what it runs after a power cut.

> **Diagnosing this class of fault: probe `.250`.** The BIOS answers on its
> compile-time IP with MAC `10:e2:d5:00:00:00` before the firmware takes over
> with its flash-derived MAC and DHCP address. If the compile-time IP answers
> and the DHCP one does not, **the FPGA configured and the BIOS is alive** — the
> failure is downstream. Every indicator that looks at the firmware (TFTP
> request, panel output, DHCP address) cannot tell those two cases apart.

### Reading a cold boot

Which files a panel fetches tells you **where it booted from**, and since
v1.36.0 that is the first thing to establish:

| TFTP log shows | It booted from | Why |
|---|---|---|
| `boot.bin` then `<mac>.yml` | **network** | netboot won — link was already up (warm reboot, or a cold boot on a fast port) |
| `<mac>.yml` **only** | **flash** | netboot's ARP found nothing, flashboot caught it |
| `boot.bin` but no `<mac>.yml` | network, then died | firmware started and fell over before asking for its config — a gateware/firmware mismatch, not a network problem |
| neither | it did not get that far | see the budget below before blaming the board |

The two source MACs are the other half of the story. The BIOS uses the
gateware's compile-time `10:e2:d5:00:00:00`; the firmware derives its own from
the flash unique ID. **On a cold power-on the BIOS transmits nothing at all** —
not a failed request, nothing — because its ARP runs before the link is usable.
On a warm reboot it is chatty. `tcpdump -e ether host 10:e2:d5:00:00:00` is
therefore a direct read of "did this boot come off the network or out of flash",
with no guessing.

---

## 8b. What is in flash, and what deliberately is not

| Chip offset | Holds |
|---|---|
| `0x000000` | bitstream (~461 KB with `--ecppack-compress`; ~712 KB without) |
| `0x100000` | firmware (`FLASH_BOOT_ADDRESS`) |
| `0x300000` | stored static image (`img_flash.rs`) |

The gap at `0x200000` is *intended* to be free, and left free **on purpose**.

> **It was not actually free.** A full-chip dump on 2026-09-16 found a complete
> factory Lattice Diamond bitstream at `0x200000`, a second at `0x2B0000`, and a
> multiboot JUMP descriptor in the chip's last sector pointing at `0x200000` —
> none of it ever written by us, and none of it ever erased, because
> openFPGALoader only clears the sectors it is about to write. `--bulk-erase`
> removed all of it; the chip now holds only the two regions above. See the
> cold-power-on section below.

It was tempting to cache the last TFTP config there, so a panel could ride out a
server outage. That is the wrong shape. A cached config is a **second source of
truth** that can silently disagree with what the fleet server is serving, and
the failure — a panel quietly ignoring a config change because it prefers its
own copy — is far more confusing than a panel that simply has no config.

The panel config is a **compile-time** constant instead
(`./build.sh --default-config`, v1.28.0). The bitstream already fixes panel
geometry at build time; the layout follows the same model. No runtime state, no
flash wear, no cache invalidation, and a TFTP config still wins when one arrives.

> The one thing worth remembering about this region: the image store at
> `0x300000` has already had a near-miss. An understated `FLASH_SIZE` once put
> stored images at `0x180000` — **512 KB above the firmware**, close enough that
> a growing `boot.bin` would eventually erase them. Anything new down here needs
> its offset checked against the *actual* sizes, not the intended ones.

### Cold power-on does not configure the FPGA — WRONG, and closed (2026-09-17)

> **The FPGA configured every single time.** This whole section was written
> against a symptom that had a different cause: the board booted, ran the BIOS,
> and stalled at netboot, which makes one attempt with no link wait. Every
> indicator in use — a TFTP request, a ping to the DHCP address, pixels on the
> panels — sits downstream of netboot, so a healthy board that stalled late was
> indistinguishable from one that never configured. Nobody probed `.250`, the
> BIOS's own compile-time IP, which was the discriminator all along.
>
> The ruled-out table below is still accurate **as facts** — none of those
> things makes any difference — but every one of them was testing
> configuration, which was never broken. **Do not buy scope time for this.**
> A cold power-on now boots in 6.2 seconds; see §8c.


**Tested exhaustively 2026-09-15.** A power cycle leaves the panel dark, with no
ping and no TFTP request — the BIOS never runs. `--detect` answers afterwards,
so the board has power. The FPGA never begins a configuration cycle.

**The constraining fact**: a JTAG `Refresh` configures the ECP5 from that same
flash *reliably*, every time. So the data is readable by the configuration
engine, the bitstream parses, and master-SPI mode works. Only the power-on
trigger fails.

Ruled out, each by direct test — none of these is worth re-testing:

| Hypothesis | Excluded by |
|---|---|
| Bad write | Readback byte-identical to source, both regions |
| Gateware regression | A **February** bitstream fails identically |
| Multiboot (`--bootaddr 0`) | Removed; no change |
| Long configuration window | Compressed 712K → 458K; no change |
| Flash block protection | Cleared with `--skip-reset`; no change |
| Wrong cable definition | Reflashed as `--cable tigard`; no change |
| Programmer holding the board | USB unplugged; no change — **but the wires stayed on**, see below |
| Multiboot / factory remnants in flash | Whole chip `--bulk-erase`d and reflashed (2026-09-16); no change |
| Tigard 3.3 V wire (`J33`) | Disconnected at the board (2026-09-16); no change |
| Power supply | Ample |

What remains is the POR sequence — INITN/PROGRAMN held, or rail ramp at
power-up — and settling it **needs a scope** on INITN / DONE / flash CS. Every
software experiment available from here has been run, and 2026-09-16 spent the
last two cheap hardware ones for two clean negatives.

Before buying bench time, two things are still worth doing, both instrument-free:
pull the four JTAG **signal** wires (`J27`/`J31`/`J32`/`J30`) and power-cycle —
`J33` and USB are now both excluded but the signal lines have never been tested —
and try a **second Colorlight card**, which is the only way to separate a faulty
board from a design/revision problem. The fleet has exactly one card, so that
distinction has never been testable.

The openFPGALoader maintainer runs a rev **8.0** board with `LFE5U-25F-7BG256C`
and reports flash boot working; ours is rev **8.2**, and he flags the pinout and
the `I`-vs-`C` grade as the differences. **Our part marking has never been
recorded** — IDCODE `0x41111043` gives only `LFE5U-25`, with no speed or
temperature grade. Read it off the chip.

Full detail of the bulk erase, including the JUMP descriptor found in the last
sector, is in `TODO.md` item 2. The pre-erase 4 MB image is kept at
`the build host:~/flash-backup-20260916/full-4MB-before-bulkerase.bin`.

Flash status registers at the time, via `/api/status` (`flash_sr1/2/3`):
SR1 `0x1c` (BP=7), SR2 `0x00` (QE=0), SR3 `0x20`.

**Consequence, as it actually stands**: a panel boots standalone from flash in
~6 seconds, and `--default-config` finally gets the chance to run it was built
for in v1.28.0. The two things that made this look like a configuration fault
were both in what we wrote, not in the board:

1. the firmware was flashed **without its FBI header**, so `flashboot()` rejected
   it every time and fell through to a netboot that could not work yet
   (fixed in `606c7db`, v1.36.0);
2. once flash *was* bootable, reading it took 18 seconds (fixed in v1.37.0, §8c).

The board is a widely-used commercial product and it behaved like one throughout.

---

## 8c. The cold boot budget, measured

A cold power-on is **6.2 seconds** (v1.37.0), measured on the bench card with a
marked plug-in and a packet capture. It was **~20.6 s** the day flash first
became bootable. Every stage below is measured except FPGA configuration, which
is computed from the image size:

| Stage | Cost | How it was measured |
|---|---|---|
| ECP5 configuration from flash | ~1.4 s | computed: 432 KB compressed ÷ 2.4 MHz MCLK — **not** measured |
| BIOS start, DRAM init, `serialboot()` | 0.53 s | warm reboot: CPU reset → the BIOS's first TFTP request on the wire |
| `netboot()` failing | 0.32 s | `/api/flashbench` |
| **`flashboot()`** | **3.90 s** | `/api/flashbench` (was 17.92 s) |
| firmware start → DHCP | 0.4 s | warm reboot: `boot.bin` served → `<mac>.yml` requested |
| | **≈ 6.55 s** | against **6.24 s** observed |

The budget closes to within 0.3 s, which is inside the error on a human-timed
plug-in. There is no missing time and nothing left to explain.

### Why flashboot was 18 seconds, and what fixed it

`flashboot()` reads the firmware image out of the memory-mapped SPI flash
**three times over**, and two of those are the same CRC:

```c
void flashboot(void) {
    length = check_image_in_flash(FLASH_BOOT_ADDRESS);   /* crc32 pass 1 */
    ...
    result = copy_image_from_flash_to_ram(...);          /* boot.c:623 -- */
}                                                        /* calls check_image_in_flash AGAIN */
```

`copy_image_from_flash_to_ram()` re-validates before copying, so the image is
CRC'd twice and then copied:

| Pass | At divisor 9 (2 MHz) | At divisor 1 (10 MHz) |
|---|---|---|
| crc32 over 292,976 bytes, byte at a time | 8.36 s | 1.81 s |
| crc32 over the same bytes, **again** | 8.36 s | 1.81 s |
| copy to DRAM, word at a time | 1.19 s | 0.28 s |
| | **17.92 s** | **3.90 s** |

The cost is entirely per-transaction. The `spiflash` SoCRegion is
**`cached=False`** with **`READ_1_1_1`**, so every access is its own
command-plus-address round trip — 8 command + 24 address + 8 data = 40 SPI
clocks — *before* it returns a single byte:

| Read | Cycles per byte |
|---|---|
| SPI flash at divisor 9 (2 MHz) | **1142** (28.5 µs) |
| SPI flash at divisor 1 (10 MHz) | **246** |
| DRAM | **14** |

78× slower than DRAM at the old setting, and it is not loop overhead: a
word-wise read costs 650 cycles for four bytes, so nearly all of it is the
transaction, not the data.

**The fix is `default_divisor` on `LiteSPIPHY`.** `spi_clk = sys_clk / (2 ×
(divisor + 1))`, so at 40 MHz the LiteSPI default of 9 gives 2 MHz. It is now 1,
giving 10 MHz and a measured 4.01×.

### The divisor can be swept at runtime, so prove it before you ship it

`clk_divisor` is a **`CSRStorage`** whose *reset* value is the gateware's
`default_divisor`. The firmware can retune the SPI clock while running, which
means a candidate clock can be validated on the actual board before it is baked
into a bitstream. `/api/flashbench` does exactly that — it checksums the **whole
292,976-byte image** at each divisor and compares:

| Divisor | SPI clock | Full-image read | vs divisor 9 | Data |
|---|---|---|---|---|
| 9 | 2.0 MHz | 1248.8 ms | 1.00× | identical |
| 4 | 4.0 MHz | 662.9 ms | 1.88× | identical |
| 2 | 6.6 MHz | 428.5 ms | 2.91× | identical |
| **1** | **10.0 MHz** | **311.3 ms** | **4.01×** | identical |
| 0 | 20.0 MHz | 194.1 ms | 6.43× | identical |

**20 MHz reads correctly on this board.** 10 MHz ships anyway, with 2× margin,
because the failure mode is a panel that will not boot standalone and has to be
recovered over a JTAG link that corrupts most of what it reads (§7). Sample a
short burst and a marginal setup passes; the sweep reads the full image for
exactly that reason.

> Only `default_divisor` changes **boot** time. The BIOS runs at the reset value,
> so a runtime write helps nothing at power-on — it is a test instrument, not a
> fix.

### Two instrument traps this work walked into

**`mcycle` reads back zero.** `csrr 0xB00` is not implemented on this VexRiscv
build, so it returns 0 forever. `/api/bench` has reported `fill_cycles:0`,
`program_cycles:0` and every other counter zero since the day it was written,
with a comment above it explaining at length why it uses `mcycle` instead of
`TIME_MS`. **Use the 1-second countdown timer** (`Timer0`, latch with
`update_value`, read `value`) — it is real hardware and keeps running inside the
trap handler, where `TIME_MS` cannot advance.

**That timer wraps every second.** A measurement longer than one period aliases
to `elapsed mod 1 s`, and the giveaway is an impossible result: the first full-image
sweep reported divisor 9 at **247 ms**, *faster* than every faster divisor, because
the real 1248.8 ms wrapped once. Time anything long in chunks and sum the deltas.

---

## 8d. Driving fine-pitch modules: the panel IC decides everything

Established 2026-09-18 while sizing a 3x3 wall of P1.25 320x160 mm modules
(256x128 px each, 768x384 total).

**The receiving card was never the limit.** Colorlight's own 5A-75E
specification V8.0 says *"supports up to 1/64 scan"* with 32 RGB groups. The
pinout exposes only five parallel row-address lines — `A`–`D` on buffered pins
plus `E` on a repurposed GND pin — which caps *parallel* addressing at 1/32, and
that looked like a hard blocker until the panel's driver IC was checked.

**These modules use `ICN1065L`, a Scramble-PWM driver, not a shift register.**
That single fact invalidates the whole output stage:

| | This repo's HUB75 driver | `ICN1065L` |
|---|---|---|
| Per LED, per refresh | one bit; we cycle bit-planes | **16-bit greyscale word**, PWM generated in the chip |
| Greyscale | our BCM plus a gamma LUT in fabric | 12 of those 16 bits, 4096 levels |
| Row advance | parallel `A`–`E` address | **OE pulses** step an internal counter; VSYNC resets it |
| Start-up | none | **38 config registers, one per frame**, after a `PRE_ACT` unlock of three LAT pulses (3, 11, 14 CLK) |

So a card with five address lines drives 1/64 scan happily: the row addressing
happens *inside the driver IC*. Ours cannot, because it shifts 1-bit planes.

**The lesson for buying panels**: the pixel pitch, the module size and the
receiving card tell you almost nothing about whether this repo can drive them.
**Ask for the driver IC part number first.** A shift-register panel (MBI5124,
ICN2038 in compatible mode, and similar) works with what is here. Anything
advertising 13-16 bit greyscale or refresh above about 3 kHz is almost certainly
S-PWM and needs its own output stage.

Reference implementation of the protocol, which is documented rather than
needing reverse engineering: https://github.com/nimakolahi/hub75-icn1065

**Status:** that output stage is built on branch `feat/icn1065-spwm` —
`gateware/icn1065.py`, 69 simulation checks, placing at 20% logic and **zero
block RAM** for nine outputs. It is not wired into the SoC and **no ICN1065
panel has been connected to this board**, so every number in `docs/ICN1065.md`
is transcribed from the reference rather than captured from a logic analyser.
See `docs/BENCHMARKS.md` §4c for what a 3×3 wall of these can actually run at,
and why the arbiter — not the SDRAM budget — turned out to be the thing that
decides it.

---

## 9. Debugging a board with no serial console

There is no serial console. Everything below exists because of that.

**Watch the ARP MAC.** The BIOS answers as `10:e2:d5:00:00:00`; the firmware
derives a unique locally-administered `02:xx:xx:xx:xx:xx` from the flash unique
ID. Which one replies tells you which is running.

**Watch TFTP.** Requests on port 69 are the BIOS. Requests on 6969 are the
firmware.

**Breadcrumbs.** `src/breadcrumb.rs` records how far execution got in
**uncached SDRAM, which survives `soc_rst`**. After a reboot, `prev_mark`,
`prev_mcause` and `prev_mepc` come back in `/api/status`. This found a bug that
bisection had failed on outright.

**`/api/fb` dumps the front buffer** — what is actually being scanned out,
sampled every 4 pixels, with the true output mode. Every other signal reports
*intent*; this one reports what is happening. It settled an argument by showing
a pixel-perfect test pattern tagged `mode: indexed` — correct pixels, wrong
mode, which no counter could have revealed.

---

## 10. Three bugs that were the machine, not the code

Worth knowing because each looked like something else entirely.

**The trap vector was overwriting its own program.** The assembly trap vector
incremented a counter at a *hardcoded* `0x40020000` — which is inside `.text`.
Every interrupt overwrote an instruction in the running firmware. It was
intermittent, roughly **1 run in 3**; three runs of the same binary gave 0, 6, 0
resets. Bisection is useless against that.

**Panics hung instead of rebooting.** `panic.rs` spun unbounded on the UART TX
FIFO — on a board with **no serial console**, so nothing ever drains it. A panic
is supposed to be the thing that tells you something went wrong.

**The MTU was the MAC slot size.** `max_transmission_unit = 2048` — the 2 KB
EthMAC slot, not the 1500-byte Ethernet MTU. smoltcp advertised MSS 1994 and
built segments too large for the link, which were dropped before reaching the
wire. So **every HTTP response larger than one frame delivered only its tail**:
the status page never loaded, while every sub-1460-byte JSON endpoint worked
perfectly. UDP was untouched — which is exactly why streaming always worked and
the web UI never did.

> The common shape: **anything writing to a fixed absolute address in RAM, or
> waiting forever on a peripheral, is suspect on this board.**

---

## 11. The network is not a quiet place

On a shared general-purpose /24, the panel logged **1.4M multicast drops** and
**806k MAC overflows**. One broadcast ARP per second landed in the same
interrupt handler that consumes streamed pixels — ~500 µs on the slow path
against ~50 µs on the fast one — and showed up as a periodic **stutter in
scrolling text**.

Fast-path filters now discard foreign ARP, multicast and unwanted UDP before
smoltcp sees them (`slow_arp` 15,553 → **0** reaching the stack). That is a
mitigation. **A dedicated panel VLAN removes the broadcast domain instead of
filtering it**, and is the real fix.

ARP *for* the panel is still handled — dropping it would let the sender's cache
expire and stop the stream entirely, which is far worse than a stutter.

---

## 12. Stale artifacts, which is how this board lies to you

In a single day, three separate stale artifacts were found serving plausible
wrong answers: a bitstream older than its gateware, a `boot.bin` older than its
sources, and — the best one — **eighteen `dnsmasq` TFTP daemons that had been
running since January**, seven months, all serving a deprecated checkout whose
`boot.bin` was dated February.

They accumulated because `build.sh` tracked only servers it had started itself,
through a pid file inside its own tree. Anything started by another checkout was
invisible to both `stop` and `ensure`.

> **"Is it running?" must be asked of the PORT, not of a pid file you wrote.**

`build.sh` now guards the first two cases. Before believing any measurement on
this board, **prove which binary is running** — the firmware version is drawn on
the panel at boot for exactly this reason.

---

## 13. Power, which is the most common "software" fault

A 128×64 HUB75 panel at full white draws on the order of **4 A at 5 V**. The
controller itself is negligible; **the panels are the entire load**.

| Load | Rough current at 5 V |
|---|---|
| One 128×64 panel, full white | ~4 A |
| One 128×64 panel, typical content | 1–2 A |
| The 5A-75E itself | < 0.5 A |

An undersized supply does not fail like a power problem. The rail sags, and what
you see is **scrambled pixels, dropped links, a board that reboots under bright
content, or a display that works until the picture gets white**. Every one of
those reads as a firmware or network bug.

Two specifics worth knowing:

- **Content-dependent faults are a power symptom until proven otherwise.** If a
  dark test pattern is stable and a white one is not, stop debugging software.
- **Power the panels and the card from a supply sized for all of it at once**,
  and check the voltage *at the far end of the chain* under load, not at the PSU.

---

## 14. Field reference card

Everything on this page that you would want on one screen.

### Before you trust anything

```bash
openFPGALoader --cable ft2232   --detect     # channel A
openFPGALoader --cable ft2232_b --detect     # channel B -- TRY BOTH
```
Live ECP5 answers `idcode 0x41111043` … `LFE5U-25`.

```bash
# Programmer acceptance test -- --detect passing proves NOTHING
# dump the same 1MB twice, compare hashes. 29% of bytes differed on a bad one.
```

### The numbers

| | |
|---|---|
| FPGA | `LFE5U-25F-6BG256C`, idcode `0x41111043` |
| System clock | 40 MHz (cannot be raised — §6) |
| HUB75 shift clock | sys_clk / 2 = 20 MHz (panel limit) |
| SDRAM | `M12L16161A`, 2M × 16 |
| Flash | **`GD25Q32`**, 4 MB, JEDEC `c8 40 16` |
| PHY | `RTL8211FD` RGMII, `rx_delay=2e-9` |
| MAC FIFO | 8 rx × 2 KB, 2 tx — ~8 ms to drain |
| Framebuffer | 262,144 words per buffer, double buffered |
| Memory access cost | **~190 cycles**, read or write |
| DRAM byte read | **14 cycles** |
| SPI flash byte read | **246 cycles** at divisor 1 (was 1142 at divisor 9) |
| SPI clock | `sys_clk / (2 × (divisor + 1))` — `default_divisor=1` → **10 MHz** |
| Cold boot, power to DHCP | **6.2 s** (§8c) |
| Pixel word | **`0x00BBGGRR`** on this panel — measured, see §3 (index still in the low byte) |
| HUB75 connectors | 6 (J1–J6), up to 16 in the SoC |
| TFTP | port **6969** |

### Symptom → cause

| What you see | Look at first |
|---|---|
| JTAG chain `empty` | the other FT2232 channel, then power |
| Corrupt flash writes, `--detect` fine | the programmer (§7) |
| Fails only on bright content | **power** (§13) |
| Link up, CRC errors under load | RGMII `rx_delay` (§5b) |
| Frozen picture, counters healthy | pixel DMA is off |
| Band of the NEXT frame across the top | the framebuffer swap is not at the frame boundary (`auto_swap`) |
| Whole frame occasionally wrong | `present()` repairing into a buffer the gateware already handed to the next frame |
| Every frame glitched at the top, continuously | the new frame's first chunk addressed with the outgoing base — `write_base_next` in `dma_writer.py` |
| One flat colour, shapes visible | output mode vs content (indexed/full) |
| Red and blue swapped | someone wrote `0x00BBGGRR` (§3) |
| Boots only with a TFTP server up | firmware never written to flash (§8) |
| `boot.bin` fetched but no `<mac>.yml` | gateware/firmware mismatch (§8) |
| Works, then dies after a power cut | firmware in flash has no FBI header, or none is flashed (§8) |
| Cold boot takes tens of seconds | `LiteSPIPHY default_divisor` — every flash byte is its own SPI transaction (§8c) |
| A benchmark reports 0 cycles | `mcycle` is not implemented; use `Timer0` (§8c) |
| A long timing is faster than a shorter one | the 1-second timer wrapped (§8c) |
| Cold boot fetches no `boot.bin`, panel fine | **correct** — it booted from flash (§8) |
| Panel boots, runs, and is completely MUTE (no DHCP/ARP/ping) while the BIOS netboots fine | `NRXSLOTS` in `ethernet.rs` disagrees with `nrxslots` in the gateware — TX buffers are at `(NRXSLOTS + slot) * 2048`, so frames are written past the region. `build.sh` guards this now |
| Display and pixel DMA address different framebuffer halves | the three-way `half_words` split; one definition in `hub75.py` now, guarded |
| Boots, but as ONE panel expecting a stream | no config: no TFTP, and none baked in (`--default-config`) |
| Periodic stutter in scrolling text | broadcast traffic on the segment (§11) |
| Firmware acting like old firmware | stale artifact — prove what is running (§12) |

### The three rules this board taught us

1. **`--detect` passing proves nothing.** Neither does a counter reading zero.
   Test the thing you actually depend on.
2. **Prove which binary is running** before believing any measurement.
3. **Anything writing to a fixed absolute address, or waiting forever on a
   peripheral, is a bug on this board** — there is no console to tell you.
4. **Measure the stage, not the gap.** Every "it takes 30 seconds" number in this
   repo's history was `last reply → first reply`, which silently contains however
   long the plug was out. Mark the power-on, capture the wire, and budget the
   stages until they add up — the answer here was hiding in a stage nobody had
   ever timed.

---

## See also

- **[FPGA-GUIDE.md](FPGA-GUIDE.md)** — how the gateware works, for beginners
- **[BENCHMARKS.md](BENCHMARKS.md)** — every measured number, with its conditions
- **[../ARCH.md](../ARCH.md)** — how the SoC and firmware are put together
- **[INSTALLATION.md](INSTALLATION.md)** — building, flashing, deploying
