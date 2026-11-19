# Colorlight HUB75 LED Controller

> **Flashing a card: `docs/FLASHING.md`** is the single authority. Any flashing
> command elsewhere in the docs is a convenience copy -- that file wins.


[![Version](https://img.shields.io/badge/version-5.7.0-brightgreen.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-BSD--2--Clause-blue.svg)](LICENSE)
[![FPGA](https://img.shields.io/badge/FPGA-Lattice%20ECP5-green.svg)](https://www.latticesemi.com/Products/FPGAandCPLD/ECP5)
[![Board](https://img.shields.io/badge/Board-5A--75E%20%7C%20E320-orange.svg)](http://www.colorlight-led.com/)

A complete FPGA-based LED panel controller for **HUB75** displays, built on the **Colorlight 5A-75E** receiver card. Features a LiteX SoC with VexRiscv CPU, Ethernet connectivity, and a Rust-based firmware with telnet management console.

> **Also runs on the Colorlight E320 V6.2** — same ECP5, package, flash and
> SDRAM, plus a factory-fitted JTAG header — and on it, **one card drives four
> fine-pitch S-PWM modules**. Its **HUB320** connectors (eight ports, 26-pin,
> four RGB groups each) were **measured** pin by pin, and the S-PWM output stage
> drives four **ARGO P1.25 256x128 DP3264S modules** — a 256x512 picture — at
> **66.7 Hz refresh, with a full four-module stream at 20 fps** and zero
> underruns, every connector proven on glass (first at gateware 1.46.0 /
> firmware 3.9.0; current: gateware 3.6.3 / firmware 5.8.5). A **second
> P1.25 module**, Myled's (Chipone ICND1065L, four stacked strips, hung a
> quarter turn), works too: two stacked on one E320, 256x256, streaming video at
> 20 fps (gateware 3.6.3 / firmware 5.8.5). Who makes which module, and the bitstream,
> firmware, layout and module type for each: [PANELS.md](docs/PANELS.md).
> Fitting four outputs into an LFE5U-25F took a rebuilt memory reader and a
> trimmed BIOS. **How the performance was found**, step by step with the
> measurements and the dead ends, is
> [E320-PERFORMANCE.md](docs/E320-PERFORMANCE.md); the resulting design is
> [ARCH.md §13](ARCH.md):
>
> ```bash
> docker build -t litex-hub75:out4 -f exp/Dockerfile.out4 exp   # once
> exp/build-out4.sh                                             # bitstream + firmware, matching CSR maps
> ```
>
> Build at 35 MHz, not 40 — the E320 is a **-6** part. The whole bring-up, wrong
> turns included, is [E320-BRINGUP.md](docs/E320-BRINGUP.md); the module itself
> is in [PANELS.md](docs/PANELS.md). See also
> [HARDWARE.md §1b](docs/HARDWARE.md) and [PINWALK.md](docs/PINWALK.md).

> ## 🔗 Card chains: one cable run, one IP address, many cards
>
> Every Colorlight card has two RJ45s. Here the second one **feeds the next
> card**, so a wall of any number of cards hangs off one switch port:
>
> ```
>   switch ── [card 1] eth0 │ eth1 ──── eth0 [card 2] eth1 ──── eth0 [card 3] …
>              head, hop 0          member, hop 1         member, hop 2
>              <ip>:80 / 7000       <ip>:8001 / 7001      <ip>:8002 / 7002
> ```
>
> - **Forwarding is in gateware**, cut-through on the receive clock: about
>   100 ns a hop, no CPU involved, and the card in front is unaware of it.
>   Replies from cards behind are merged back up at frame boundaries.
> - **A whole chain is one IP address**, the head's. Broadcasts are dropped in
>   both directions, so a card behind another never gets a DHCP lease, and that
>   is how it knows it is a member. A mis-cabled loop is not a broadcast storm.
> - **Each card works out its own position.** marquee sends a small discovery
>   packet once a second; every card's gateware adds one to its hop count on
>   the way through (and fixes the UDP checksum and FCS in flight), so the
>   packet a card receives tells it its hop, its head and its server. Card h
>   answers HTTP on port 8000+h and takes pixels on 7000+h.
> - **Same bitstream and firmware on every card**; the role is decided at
>   runtime. A member's own web page is reachable through the head, or through
>   marquee from anywhere.
> - **Firmware follows the server, for every card.** A head's BIOS netboots its
>   image. A member's cannot, so its firmware does: marquee has it fetch the
>   assigned image into RAM, check it and switch to it, writing nothing
>   (firmware 5.0.5, `src/chainload.rs`). A chain runs one image, and flash keeps
>   a **survival copy** that marquee rewrites only after an image has run
>   cleanly, so a chain with no server still comes up on one version.
>
> **Proven on two cabled E320s** (2026-11-13): discovery and hop ports, the
> member's page through the head, a firmware rollout to both cards, a new
> bitstream to both cards' flash live after a single power cycle (both back in
> about 21 s), and a member netbooting into a new image in 8 s. Simulated
> beforehand against 20 adversarial cases (`./build.sh sim-chain-deep`: drift,
> jumbo frames, runts, resets mid-frame, starvation). Design and test record:
> [ARCH.md §14](ARCH.md#14-card-chains--one-ip-many-cards); walkthrough:
> [Getting Started, Parts 4-5](docs/GETTING-STARTED.md).

> ### 👉 New here? **[Start with the Getting Started guide](docs/GETTING-STARTED.md)**
>
> It takes you from parts on a desk to a lit panel running your own program and
> playing a video, in about an hour. No FPGA experience needed.


> ## ⚙️ Once a card is programmed, all you need is marquee
>
> **Nothing in this repo runs at runtime.** It builds gateware and firmware,
> compiles on-panel programs, and flashes boards. After that the card is served
> entirely by **marquee** — boot, config, pixels, live values, stats.
>
> Come back here only to change what is *on* the board:
>
> | Task | Needs this repo |
> |---|---|
> | Run a panel, day to day | **no** |
> | Change which program a panel runs | **no** — marquee sets it |
> | Change a show, schedule or panel record | **no** — marquee |
> | Write a **new** on-panel program | **yes** — `panelc` compiles it in |
> | Change panel geometry, outputs or panels-per-connector | **yes** — it is in the bitstream |
> | Daisy-chain cards through their second RJ45 | **no** — every build does it; register the cards in marquee ([ARCH.md §14](ARCH.md#14-card-chains--one-ip-many-cards)) |
> | Update firmware | **yes** to build; then upload it to marquee and push it to the chain's head -- the chain follows |
> | First bring-up of a new board | **yes** — JTAG |
>
> The firmware carries *every* program and the config picks one, so switching a
> panel between them is a marquee change. Adding a new one is a firmware build.

Two ways to drive a panel, and it does both:

- **Stream to it** — send frames over UDP from anywhere. ~103 fps at 128×128.
- **Run code on it** — write YAML, compile it into the firmware, and the panel
  composes its own pixels from values pushed to it. Like ESPHome, for LED signs.
  Unplug the host and it keeps running.

## Supported hardware

What this repo drives today, and how sure we are. "Proven" means lit on the
bench, pixel-exact, with the configuration in `docs/PANELS.md`; "untested" means
code exists and no module has ever been connected to it.

| Receiver card | Connector | Output stage | Status |
|---|---|---|---|
| **Colorlight 5A-75E** | HUB75 (**16** ports, 2 RGB groups each = 32 groups) | bit-plane PWM (`gateware/hub75.py`) | **proven**, in daily use (the bench railboard, the walls marquee drives) |
| **Colorlight E320 V6.2** | HUB320 (8 ports, 4 RGB groups each = 32 groups) -- **the only HUB320 card supported** | S-PWM (`gateware/icn1065_soc.py`), **35 MHz**, `clk_div 4` | **proven** with the module below -- **four outputs** (J1-J4 each lit on glass), a full 256x512 stream at 20 fps, 66.7 Hz (gateware 1.46.0, firmware 3.9.0, `exp/build-out4.sh`); also runs the 5A-75E bitstream unchanged for HUB75 |

Both cards have **two RJ45s**, and every build uses the second as the
downstream port of a **card chain**: one IP per chain, the card at hop h on
8000+h / 7000+h, every card on the image marquee assigns its chain
([ARCH.md §14](ARCH.md#14-card-chains--one-ip-many-cards); on the E320 `eth0`,
the left jack, faces the switch and `eth1`, the right, feeds the next card).

Exact revisions, connector counts and what is NOT supported (the K-series):
**[docs/CARDS.md](docs/CARDS.md)**. Out of outputs and wondering about a
bigger FPGA: **[docs/SCALING.md](docs/SCALING.md)** -- measured ceiling, and
why more cards usually beats a port (and
**[docs/I9-PANEL-PINOUT.md](docs/I9-PANEL-PINOUT.md)** for the i9 pinout that
turned out to be public all along, filed under "DDR2").

> **Buying modules? Read [docs/PANELS.md](docs/PANELS.md) first.** The card and
> the module are two separate purchases with two separate risks, and the module
> is the one that decides whether any of this works. **Ask the vendor for the
> driver IC marking on the chip** -- pitch, module size and "scan rate" tell you
> almost nothing, and four supplier sheets gave four different part numbers for
> the single module on our bench. PANELS.md has the five questions to ask
> before ordering.

| Module | Driver IC | Preset | Status |
|---|---|---|---|
| Generic HUB75 128x64, 1/32 scan | shift-register constant-current (FM6126-class) | `128x64` | **proven** |
| **ARGO P1.25 320x160 mm, 256x128, 1/64 scan** (`XP1.25-5958S-256X128-64S`) | **DP3264S** S-PWM + 5958 row shift register | `256x128-dp3264s-hub320` on the E320 | **proven**: geometry, 13-bit colour, on-board programs, live value push, and streaming to **four outputs** (256x512, the modules stacked J1 to J4) at 20 fps with 0 underruns. A full four-module frame is 270 wide `'B','W'` chunks |
| **Myled P1.25 320x160 mm, 128x256 as scanned (mounts a quarter turn), 1/64 scan** | **ICND1065L** S-PWM + ICND1028AP row switch | `128x256-icnd1065l-hub320` on the E320 (`PANEL=128x256-icnd1065l-hub320 exp/build-rt.sh`; layout `custom/example/configs/myled-p1.25.yml`) | **working, seen on glass** (gateware 3.6.3 / firmware 5.8.3, 2026-11-18): test grid upright and whole at `rotate: 90`, streams with 0 underruns at 63 Hz, on the vendor's per-colour registers (a white balance, about 63% of our table's brightness; `docs/PANELS.md`). Two modules side by side on one card, 512x128; a larger wall of them is untested. Needs gateware >= 3.1.0 (serial bus on HUB320 pins 20/21) |
| Other ICND1065L / ICND1065S modules | ICND1065 | `256x128-icnd1065` and friends | **untested** -- those presets build, no module has been connected to them; `ICND1065(AP)` is not driven |
| P1.25 modules with DP3364S | DP3364S | `*-dp3364s` | **untested** -- a different part from the DP3264S (15 registers, 6 LE commands), table unverified |

The module registry with each one's identity, supply chain, chip-table values
and open questions is **[`docs/PANELS.md`](docs/PANELS.md)**. A module whose
vendor tunes the driver registers differently needs no chip-table change:
`chip_reg:` lines in the boot config override registers by address (firmware
5.6.0), and `tools/rcvbp_import.py` writes them, as a marquee module type,
from the vendor's `.rcvbp` receiver profile. The S-PWM path's
remaining inference is the DP3264S's DATA_LATCH commit position (`sweep_adv=1`,
`docs/E320-BRINGUP.md` 4.6a) -- derived, simulated and confirmed on glass, not
yet seen on a scope.

## Features

- **HUB75 LED Panel Driver** - 6 outputs (J1–J6) in the default build, of the 5A-75E's 16 connectors (the firmware layout takes J1–J8), with up to 2 panels chained per connector
- **DHCP Networking** - Automatic IP via DHCP with unique MAC from SPI flash
- **Card chaining** - The second RJ45 feeds the next card. A whole chain is one
  IP address (the head's); the card at hop h answers on 8000+h and takes pixels
  on 7000+h, learns its hop automatically, and needs no IP of its own. Repeating
  is in gateware, ~100 ns a hop. Same bitstream and firmware on every card; a
  card behind another starts its flash copy and is then netbooted into its
  chain's image by marquee (`POST /api/netboot`) -- tested on two cabled E320s,
  [ARCH.md §14](ARCH.md#14-card-chains--one-ip-many-cards)
- **TFTP Boot Config** - Per-board YAML layout config fetched at boot via `<mac>.yml`
- **S-PWM modules on HUB320** - Two P1.25 modules driven on the E320: ARGO's
  (DP3264S, four outputs, in daily use) and Myled's (ICND1065L, working).
  The driver chip is a table, not a circuit; per-module register values come
  from the boot config (`chip_reg:`), imported from the vendor's receiver
  profile by `tools/rcvbp_import.py` ([docs/PANELS.md](docs/PANELS.md))
- **HTTP REST API** - Web status page and JSON API on port 80 (8000+hop for a chained card)
- **Bitmap UDP Protocol** - Four wire formats on UDP port 7000:
  `'B','M'` RGB888, `'B','I'` **indexed** (1 byte/px, ~3x fewer packets and so
  ~3x the frame rate), `'B','P'` palette (256 entries in one packet), and
  `'B','W'` RGB888 with u16 chunk counters for frames over 255 chunks (a
  four-module wall). Every packet fits a 1500-byte MTU -- see
  [ARCH.md §4](ARCH.md)
- **Hardware Pixel DMA** - Streamed pixels go straight to SDRAM in gateware; the
  CPU never touches them, and since v1.38.0 never even takes an interrupt for
  them — a gateware classifier kills pixel packets before the MAC raises an event
- **~33 fps full RGB across 9 panels** (384×192), against 5.5 fps before that.
  The gateware also flips the framebuffer at the frame boundary, because a
  CPU-timed swap puts a band of the next frame across the top of the image
- **Telnet Console** - Remote configuration and management on port 23
- **Double-Buffered Animation** - Tear-free 30fps display updates
- **Multi-Panel Virtual Display** - Configurable grid layout across multiple panels
- **Rust Firmware** - Type-safe embedded development with smoltcp TCP/IP stack
- **Boots standalone in 6.2 seconds** - power to DHCP, entirely from SPI flash,
  with no server anywhere on the network. The BIOS tries the network first so
  `deploy` still works, and falls through to flash when nothing answers
- **Double-buffered in hardware** - `fb_base` flips in gateware at the frame
  boundary; scan-out and the pixel DMA follow the same signal, so they cannot
  disagree about which half is live

## Hardware Requirements

| Component | Specification |
|-----------|---------------|
| FPGA Board | Colorlight 5A-75E V8.2 or E320 V6.2 (Lattice ECP5-25F) |
| Programmer | USB Blaster, FTDI FT2232, or compatible JTAG |
| LED Panels | HUB75/HUB75E compatible (96x48, 128x64, 64x32, 64x64) |
| Network | Gigabit Ethernet; one link carries a whole card chain (~38 Mpx/s of RGB888) |

### JTAG Pinout

JTAG is available on a 4-pin header next to the FPGA (U33). VCC/GND are on a separate 2-pin header nearby.

| Pin | Function |
|-----|----------|
| J27 | TCK      |
| J31 | TMS      |
| J32 | TDI      |
| J30 | TDO      |
|     |          |
| J33 | 3.3V     |
| J34 | GND      |

Connect these to your USB Blaster or FTDI programmer's corresponding JTAG signals.

## Quick Start

### Prerequisites

- Docker (for reproducible builds)
- USB Blaster or compatible JTAG programmer
- Network connection to the board

### Build

All builds use Docker for reproducibility. Run `./build.sh --help` for full options.

#### First Time Setup

```bash
# Build the Docker environment (only needed once)
./build.sh docker

# Build everything: bitstream + firmware for default panel (128x64)
./build.sh
```

#### Common Workflows

```bash
# Development cycle - rebuild firmware and boot via TFTP
./build.sh firmware boot
# TFTP server auto-starts and stays running between boots

# Quick test - just reboot without rebuilding (uses existing .tftp/boot.bin)
./build.sh boot

# Stop TFTP server when done developing
./build.sh stop
```

#### Panel-Specific Builds

The `--panel` flag sets the size of **one individual panel** (shift register width and row count). This is baked into the FPGA bitstream. The total display size depends on how many panels you chain and arrange in a grid (see [Bitstream vs Layout](#bitstream-vs-layout)).

The default build is `chain_length=1` (one panel per output) with `outputs=6`. Chain length and output count are separate `build.sh` flags (`--chain-length`, `--outputs`). The 5A-75E has 16 connectors; the firmware's layout parser accepts J1–J8 (`layout::MAX_OUTPUTS`) and 2 panels per chain (`layout::MAX_CHAIN`). ("Chain" here is panels daisy-chained on one connector -- not the *card* chain of [ARCH.md §14](ARCH.md#14-card-chains--one-ip-many-cards).)

> `chain_length` must match `const CHAIN_LENGTH` in `sw_rust/barsign_disp/src/hub75.rs` — the firmware bakes it in at compile time. `build.sh` reads the constant and refuses to build a gateware that disagrees. Its own default said `2` from v1.7.0 until 2026-09-17, long after the firmware moved to `1`, which meant a plain `./build.sh bitstream` produced a mismatch that builds, boots and looks fine.

> **`const OUTPUTS` in the same file has the same problem and `--outputs N` does NOT update it.** `build.sh` compares and refuses, because the failure is silent rather than loud: `LayoutConfig::parse()` tests `n <= MAX_OUTPUTS` and drops surplus connectors with **no error**, so the extra panels show the unassigned default — which on a wall is indistinguishable from dead connectors.

> **The S-PWM path is limited by BLOCK RAM, not by logic: 3 outputs in a standard build, 4 in the trimmed `exp/build-out4.sh` build** (smaller burst FIFO, BIOS and SRAM; `ARCH.md` §13.6) that the four-module E320 wall runs. Each output's scan-out line buffer (gateware 1.29.0, `Icn1065LineBuffer`) costs 5 EBR, and `icn1065_soc.py` refuses to build past the budget rather than let place-and-route fail with a message that names the resource but not the reason. Measured on a standard build: **one output 47/56, two outputs 52/56, three 56/56 exactly** — and `burst_depth` is stepped down as outputs rise to pay for it. The "6 outputs, 12 panels" figures above are the HUB75 path, which has no line buffer. Figures in `docs/BENCHMARKS.md` showing nine outputs predate 1.29.0 and are superseded.

> **Timing closure gets harder with each output, and the clock that fails is the gigabit RX, not the system clock.** Two outputs needed a seed sweep: seeds 5 and 1 closed `sys` comfortably (39.41 and 41.32 MHz against 35) and failed `eth_clocks0_rx` by under 1.5% (124.13 and 123.11 against 125). Seed 3 closed both with margin. Sweep seeds before concluding a design does not fit.

```bash
# Build bitstream for a specific per-panel size
./build.sh -p 128x64 bitstream      # 128x64 per panel (default)
./build.sh -p 96x48 bitstream       # 96x48 per panel
./build.sh -p 64x32 bitstream       # 64x32 per panel
./build.sh -p 64x64 bitstream       # 64x64 per panel (square)

# Build bitstreams for ALL panel sizes at once (saved to bitstreams/)
./build.sh build-all

# Boot a specific panel (uses prebuilt bitstream from bitstreams/)
./build.sh -p 256x64 boot

# Full rebuild + boot for specific panel
./build.sh -p 96x48 bitstream firmware boot
```

#### Flashing vs Booting

```bash
# SRAM boot (temporary - lost on power cycle, fast for development)
./build.sh -p 128x64 boot

# Flash to SPI (permanent - survives power cycles)
./build.sh -p 128x64 flash

# Flash specific panel, then boot to verify
./build.sh -p 256x64 flash && ./build.sh -p 256x64 boot
```

#### After Gateware Changes

When you modify `gateware/*.py` files, you need to rebuild the bitstream. Whether you also need to regenerate the PAC depends on what changed:

| Change Type | Command |
|-------------|---------|
| CSR registers added/removed/renamed | `./build.sh -p 128x64 bitstream pac firmware` |
| Peripheral addresses changed | `./build.sh -p 128x64 bitstream pac firmware` |
| Internal logic only (no new registers) | `./build.sh -p 128x64 bitstream firmware` |

**Safe rule:** Always regenerate PAC when touching gateware. It only takes ~2 seconds and avoids hard-to-debug crashes from mismatched register addresses:

```bash
# RECOMMENDED: Always do all three together after gateware changes
./build.sh -p 128x64 bitstream pac firmware

# Then boot to test
./build.sh -p 128x64 boot

# Or do everything in one command
./build.sh -p 128x64 bitstream pac firmware boot
```

> **Warning:** If you rebuild bitstream without PAC after adding a register, the firmware will access wrong memory addresses and crash mysteriously. When in doubt, include `pac`.

#### TFTP Server Management

The TFTP server serves `boot.bin` (firmware) and `<mac>.yml` (config) files:

```bash
# Start TFTP server manually (auto-started by 'boot')
./build.sh start

# Stop TFTP server
./build.sh stop

# Check if TFTP server is running
pgrep -f tftpy

# Bind TFTP server to a specific host IP (auto-detected by default)
./build.sh --host-ip 192.168.1.100 boot
```

#### Changing the TFTP Server Address

There are two TFTP fetches at boot, and **both take the server from DHCP**:

| Fetch | Client | Server | How to Change |
|-------|--------|--------|---------------|
| `boot.bin` (firmware) | BIOS | DHCP Option 66, then BOOTP `siaddr`, then the compiled-in `TFTP_SERVER` (`192.168.1.10`) | Configure your DHCP server |
| `<mac>.yml` (config) | Firmware | DHCP Option 66, or the same fallback | Configure your DHCP server |

Configure your DHCP server to provide Option 66 (TFTP Server Name):
- **dnsmasq**: `dhcp-option=66,192.168.1.100`
- **Windows DHCP**: Set Option 066 "Boot Server Host Name"
- **ISC DHCP**: `option tftp-server-name "192.168.1.100";`

**To change the compiled-in fallback** (used only when DHCP names no server):
set `TFTP_SERVER` in `build.sh` and rebuild the bitstream
(`./build.sh -p 128x64 bitstream pac firmware`), then flash it.

A card chained behind another gets no DHCP; it fetches `<mac>.yml` from the
host that sent marquee's discovery packet
([ARCH.md §14.3](ARCH.md#143-discovery-and-roles)).

#### Test Patterns

The build includes a test pattern baked into the firmware:

```bash
# Build with different test patterns (shown at boot before streaming starts)
./build.sh -t grid firmware         # Grid pattern (default)
./build.sh -t rainbow firmware      # Rainbow gradient
./build.sh -t solid_white firmware  # Solid white
./build.sh -t solid_red firmware    # Solid red
```

### On-panel programs

A panel can compose its own pixels instead of being streamed to: you write YAML,
`panelc` compiles it into the firmware, and the panel draws from named values
pushed to it. Unplug the host and it keeps running.

- **[docs/PROGRAMMING.md](docs/PROGRAMMING.md)** — the programmer's guide:
  quick start, widget and lambda reference, the full API, performance rules,
  troubleshooting.
- [docs/DISPLAY-PROGRAMS.md](docs/DISPLAY-PROGRAMS.md) — the design reasoning.

```bash
$EDITOR panels/mysign.yaml
python3 tools/panelc.py panels/*.yaml
./build.sh firmware
```

#### A panel with no network at all

The program code is compiled into the firmware, so the only thing a standalone
panel still lacks is the config that says *which* program to run and how the
panels are wired. Bake that in too:

```bash
./build.sh --default-config custom/example/configs/panel.yml firmware
./build.sh --panel 128x64 --cable ft2232_b flash-all
```

The file is the same format the panel fetches over TFTP, parsed at boot by the
same parser, and applied **before the network comes up**. A TFTP config still
overrides it when one arrives, so this is safe on a fleet panel too — it just
means content appears a second after power-on instead of after DHCP.

Note that a standalone panel has **no data**: with no network there is no Home
Assistant, so a program meant to run offline should take no values, or be
written so that missing data looks deliberate.

#### Advanced Options

```bash
# Use a different JTAG cable
./build.sh -c ft2232 flash          # FTDI FT2232, JTAG on channel A
./build.sh -c ft2232_b flash        # FTDI FT2232, JTAG on channel B
./build.sh -c dirtyjtag flash       # DirtyJTAG

# An FT2232 is DUAL-channel and the two channels are different cables to
# openFPGALoader. Picking the wrong one does NOT report a bad cable -- the
# adapter opens, the frequency negotiates, and the chain scans `empty`, which
# looks exactly like an unpowered board. If `--detect` prints `empty`, try the
# other channel BEFORE suspecting power or the ribbon:
#   openFPGALoader --cable ft2232   --detect   # channel A
#   openFPGALoader --cable ft2232_b --detect   # channel B
# A live ECP5 answers with `idcode 0x41111043 ... LFE5U-25`.
# The bench board on the build host (FT2232H 0403:6010) is on channel B: use ft2232_b.
# Note the default cable is usb-blaster, which fails here with
# `unable to open ftdi device: -3 (device not found)`.

# Verbose output for debugging build issues
./build.sh -v bitstream

# Specify host IP for TFTP (auto-detected by default)
./build.sh --host-ip 192.168.1.10 boot
```

#### Build All Targets

```bash
# Build everything for current panel: docker (if needed) + bitstream + PAC + firmware
./build.sh all
./build.sh                          # 'all' is the default when no target specified

# Build bitstreams for ALL panel sizes (saved to bitstreams/) + firmware
./build.sh build-all
```

The `build-all` target is useful for creating a complete set of prebuilt bitstreams. Each panel size gets its own bitstream file in `bitstreams/` (e.g., `128x64.bit`, `256x64.bit`). The firmware binary is universal and works with all panels.

#### Quick Reference

| Command | Description |
|---------|-------------|
| `./build.sh` | Build everything: bitstream + PAC + firmware (default) |
| `./build.sh all` | Same as above (explicit) |
| `./build.sh build-all` | Build bitstreams for ALL panel sizes + firmware |
| `./build.sh firmware` | Rebuild firmware only (universal binary) |
| `./build.sh boot` | Program SRAM + start TFTP server |
| `./build.sh flash` | Write bitstream to SPI flash (permanent) |
| `./build.sh -p 256x64 boot` | Boot with specific panel bitstream |
| `./build.sh bitstream pac firmware` | Explicit full rebuild (same as `all`) |
| `./build.sh stop` | Stop background TFTP server |

### Supported Panels

| Panel | Scan Rate | Notes |
|-------|-----------|-------|
| 128x64 | 1/32 | Default configuration |
| 96x48 | 1/24 | Compact |
| 64x32 | 1/16 | Compact |
| 64x64 | 1/32 | Square format |

These are **per-panel** sizes. The total virtual display depends on chaining and grid layout — e.g., a 128x64 bitstream built with `--chain-length 2` supports a 256x64 display (two panels side by side) or larger grids across multiple outputs. The default is one panel per output.

The firmware binary is universal — it works with all panel sizes. Only the FPGA bitstream differs per panel. Panel grid layout is configured at runtime via TFTP config files (see below). Use `./build.sh build-all` to pre-build bitstreams for all panels, stored in `bitstreams/`.

### Test Connection

```bash
# Test ping (IP assigned by DHCP — check your DHCP server for the lease)
ping <board-ip>

# Connect via telnet
telnet <board-ip> 23

# View web status page
curl http://<board-ip>/
```

## Project Structure

```
colorlight/
├── build.sh               # Build script (run ./build.sh --help)
├── Dockerfile             # Build environment
├── gateware/              # FPGA gateware (Python/Migen)
│   ├── colorlight.py      # LiteX SoC definition
│   ├── chain.py           # card chains: port-2 forwarder, upstream merge (ARCH.md §14)
│   ├── colorlight_e320.py # E320 platform: the measured HUB320 connector table
│   ├── hub75.py           # HUB75 display driver; the framebuffer map
│   ├── icn1065_soc.py     # S-PWM output stage for HUB320 (ARCH.md §13)
│   ├── icn1065.py         # S-PWM building blocks: serialiser, line buffer, reader, arbiter
│   ├── spwm_chips.py      # per-driver-chip register tables
│   ├── dma_writer.py      # UDP pixel DMA straight to SDRAM; the wire formats
│   ├── words_to_pixels.py # 32-bit stream words to packed pixels, ahead of the DMA
│   ├── cdcfifo.py         # the Ethernet clock-crossing FIFO
│   ├── pinwalk.py         # connector-mapping bitstream (docs/PINWALK.md)
│   ├── gen_test_image.py  # Test pattern generator
│   ├── helper.py          # HUB75 / HUB320 connector pin definitions
│   └── smoleth.py         # Ethernet glue, pixel classifier, burst FIFO
├── exp/                   # The four-output E320 build and its BIOS-trimmed image
├── bitstreams/            # Pre-built bitstreams for all panel sizes
├── sw_rust/               # Rust firmware
│   ├── barsign_disp/      # Main application
│   ├── litex-pac/         # Peripheral Access Crate
│   └── smoltcp-0.8.0/     # Patched smoltcp (DHCP Option 66, fill_neighbor for chain members)
├── custom/                # your on-panel programs and baked-in configs (custom/README.md)
├── tools/                 # senders, simulations, the .rcvbp profile decoder and importer
├── .tftp/                 # TFTP-served config files (<mac>.yml)
├── legacy/                # Old scripts and experiments
└── CHANGELOG.md           # Version history
```

## Tools

Python tools in `tools/` send content to the panel over the bitmap UDP protocol (port 7000).

All tools accept `--host <ip>` (default: `192.168.1.50`), `--port` (default: `7000`), `--width` and `--height` (default: `128x64`). Video and animation tools also accept `--layout` (e.g., `2x1`) and `--panel-size` for multi-panel grids.

### send_image.py — Static Image

Send any image file (PNG, JPEG, etc.) to the panel. Auto-resized to panel dimensions. Requires Pillow.

```bash
python tools/send_image.py --host 192.168.1.70 photo.png
python tools/send_image.py --host 192.168.1.70 --layout 2x1 photo.png
```

### send_video.py — Video File

Stream a local video file to the panel. Requires ffmpeg. Chunk pacing is auto-calculated from fps and frame size (90% of frame budget), so explicit `--chunk-delay` is rarely needed.

```bash
python tools/send_video.py --host 192.168.1.70 clip.mp4
python tools/send_video.py --host 192.168.1.70 --fps 15 --loop clip.mp4
python tools/send_video.py --host 192.168.1.70 --layout 4x2 clip.mp4
python tools/send_video.py --host 192.168.1.70 --chunk-delay 0.003 clip.mp4  # manual override
```

### send_youtube.py — YouTube / Web Video

Stream a YouTube video (or any [yt-dlp supported URL](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md)) directly to the panel — no file is downloaded. Requires yt-dlp and ffmpeg. Chunk pacing is auto-calculated (same as `send_video.py`).

```bash
python tools/send_youtube.py --host 192.168.1.70 "https://youtube.com/watch?v=ID"
python tools/send_youtube.py --host 192.168.1.70 --loop "https://youtube.com/watch?v=ID"
python tools/send_youtube.py --host 192.168.1.70 --layout 4x2 "https://youtube.com/watch?v=ID"

# Age-gated / auth videos (export cookies.txt from your browser)
python tools/send_youtube.py --host 192.168.1.70 --cookies cookies.txt "URL"
```

### send_test_pattern.py — Test Patterns

Generate and send a test pattern. Available: `gradient`, `bars`, `rainbow`, `heart`.

```bash
python tools/send_test_pattern.py --host 192.168.1.70 rainbow
python tools/send_test_pattern.py --host 192.168.1.70 heart
```

### send_animation.py — Animated Patterns

Send a looping animated pattern. Available: `heart` (pulsing).

```bash
python tools/send_animation.py --host 192.168.1.70 heart
python tools/send_animation.py --host 192.168.1.70 --fps 30 --loops 0 heart
```

### decode_rcvbp.py, rcvbp_import.py — vendor receiver profiles

A module vendor's Colorlight `.rcvbp` profile holds the driver-chip register
table and the pixel map the stock receiver is given. `decode_rcvbp.py` prints
them (`--pixel-map` for the map); `rcvbp_import.py` writes a marquee
module-type JSON whose registers reach the card as `chip_reg:` lines. The group
arrangement, bitstream preset and operating point are flags -- a profile does
not carry them. Worked example: [docs/PANELS.md](docs/PANELS.md), "The vendor
profile".

```bash
python tools/decode_rcvbp.py --pixel-map <profile>.rcvbp
python tools/rcvbp_import.py <profile>.rcvbp --key <module-type> \
    --preset 128x256-icnd1065l-hub320 --clk-div 4 -o <module-type>.json
```

## Telnet Commands

Connect via `telnet <ip> 23` to access the management console:

| Command | Description |
|---------|-------------|
| `help` | Show available commands |
| `on` / `off` | Enable/disable display output |
| `reboot` | Restart the system |
| `get_image_param` | Show current image dimensions |
| `set_image_param <w> <h>` | Set image dimensions |
| `get_panel_param <out> <chain>` | Get panel configuration |
| `set_panel_param <out> <chain> <x> <y> <rot>` | Configure panel position |
| `load_spi_image` | Load image from flash |
| `save_spi_image` | Save image to flash |

## Memory Map

| Region | Address | Size | Description |
|--------|---------|------|-------------|
| ROM | 0x00000000 | 64KB | BIOS |
| SRAM | 0x10000000 | 8KB | Stack/heap |
| Main RAM | 0x40000000 | 4MB | SDRAM |
| SPI Flash | 0x80400000 | 4MB | Bitstream + firmware (W25Q32JV) |
| CSR | 0xF0000000 | 64KB | Peripheral registers |

## Multi-Panel Approaches

Three bitstream parameters control multi-panel capacity: `--panel WxH` (per-panel pixel size), `--chain-length N` (max panels daisy-chained per output, default 1 -- it must equal `CHAIN_LENGTH` in `hub75.rs`), and `--outputs N` (HUB75 connectors, default 6). Maximum total panels = outputs × chain_length. The virtual display size is set at runtime by the TFTP layout config.

There are two ways to drive multiple panels per HUB75 output:

| Approach | Gateware | Panels per output | Virtual width | Notes |
|----------|----------|-------------------|---------------|-------|
| **Bigger panel** | `--panel 256x64` with `chain_length_2=0` | 1 (wider) | 256 | Gateware reads 256 contiguous pixels in one shift register. Simpler, but treats the chain as a single wide panel — no independent positioning. |
| **Chaining** | `--panel 128x64` with `chain_length_2=1` | 2 (independent) | 128 × grid_cols | Gateware reads two 128-pixel blocks at independent (x,y) offsets via panel CSRs. Enables flexible layouts (e.g., 6×2 grid with non-adjacent regions). |

**Chaining is the recommended approach** for multi-panel setups. It uses the same BRAM budget, supports flexible grid layouts via TFTP config, and scales to 12 panels (6 outputs × 2 chains).

## Bitstream vs Layout

The bitstream and the layout config control different things:

| Layer | Set by | Controls | Can change at runtime? |
|-------|--------|----------|----------------------|
| **Bitstream** | `--panel`, `--chain-length`, `--outputs` at build | Per-panel pixel size, scan rate, max chain depth, output count | No — requires reflash |
| **Layout** | TFTP YAML config | Panel grid, connector mapping, virtual display size | Yes — applied at boot or via API |

The bitstream sets the **per-panel** pixel dimensions (shift register width and row count) and the **max chain depth** (panels per output). The layout config arranges those panels into a virtual display grid. The firmware sets the DMA `image_width` CSR to the virtual width, which the gateware uses as the framebuffer row stride. Each panel reads its pixels from (x, y) offsets in this framebuffer via per-panel CSRs.

The key constraints: `panel_width` in the YAML must match the bitstream's `columns`, `panel_height` must match `rows`, and the number of chain slots used per output cannot exceed `chain_length`.

### Example: Two 128x64 panels on J1

**Bitstream:** `./build.sh --chain-length 2 bitstream` — a 128x64 panel with chain_length=2, and `hub75.rs`'s `CHAIN_LENGTH` set to 2 to match. Hardware shifts two 128-pixel blocks per output.

**Layout (YAML):**
```yaml
grid: 2x1
panel_width: 128
panel_height: 64
J1: 0,0 1,0
```
Result: 128x64 panel at grid(0,0) + 128x64 panel at grid(1,0) = 256x64 virtual display.
Gateware reads two 128-pixel blocks at independent (x,y) offsets via panel CSRs.

### Example: Six independent 128x64 panels (3x2 grid)

**Bitstream:** `./build.sh -p 128x64 bitstream` (default chain_length=1 — one panel per output, which is what this layout uses)

**Layout:**
```yaml
grid: 3x2
panel_width: 128
panel_height: 64
J1: 0,0
J2: 1,0
J3: 2,0
J4: 0,1
J5: 1,1
J6: 2,1
```
Result: 384x128 virtual display across 6 outputs.

### What can go wrong?

If `panel_width`/`panel_height` in the YAML don't match the bitstream's per-panel size, pixels will be misaligned. If you assign more chain slots per output than `chain_length` allows, the extra panels are ignored. The web GUI shows both the bitstream parameters and the active layout so you can verify they're consistent.

## Configuration

The system is configured at three levels. The **bitstream** (FPGA) defines the per-panel pixel size, max chain depth, and HUB75 timing — this is fixed at build time. **Firmware constants** must match the bitstream. The **TFTP YAML config** maps physical panels to a virtual display grid — this is loaded at boot and can be changed without rebuilding.

For the default setup (two 128x64 panels on J1 forming a 256x64 display), no build flags are needed — just `./build.sh` and a YAML config file in `.tftp/`.

### HUB75 Output Count & Chain Length

The number of HUB75 outputs and chain length must be set consistently:

| File | Setting | Current value | Description |
|------|---------|---------------|-------------|
| `build.sh` | `OUTPUTS` | `6` | DEFAULT passed to the gateware build; override with `--outputs` / `-o` |
| `build.sh` | `CHAIN_LENGTH` | `1` | Panels per output chain (`--chain-length`) |
| `sw_rust/barsign_disp/src/hub75.rs` | `pub const OUTPUTS: u8` | **`4`** | Firmware output count — **set by hand**, `--outputs` does not touch it |
| `sw_rust/barsign_disp/src/hub75.rs` | `const CHAIN_LENGTH: u8` | `1` | Firmware chain length |

> These are read from the tree rather than remembered — check them before
> quoting them. `build.sh`'s `OUTPUTS=6` is only a default and is routinely
> overridden; the firmware constant is whatever the last board build needed,
> currently `4` for the four-module E320 P1.25 wall (`exp/build-out4.sh`).

Both outputs and chain length must match between gateware and firmware. A mismatch (e.g., firmware accessing panel CSRs that don't exist in the bitstream) will crash the SoC. The `layout.rs` constants `MAX_OUTPUTS` and `MAX_CHAIN` should also match.

After changing output count or chain length, rebuild everything:
```bash
./build.sh bitstream pac firmware
```

### IP Address (DHCP)

The firmware acquires its IP address via DHCP at boot. If no DHCP server responds within 10 seconds, it falls back to `192.168.1.50/24` (the `--ip` baked into the build). A card chained behind another never gets a lease, by design: it takes the head's IP once marquee's discovery reaches it ([ARCH.md §14.3](ARCH.md#143-discovery-and-roles)). Check your DHCP server's lease table to find the board's IP, or use the board's MAC address (`02:xx:xx:xx:xx:xx`) to assign a fixed lease.

### Panel Layout (TFTP Boot Config)

At boot, the firmware fetches a per-board YAML config file from the TFTP server (discovered via DHCP — see [TFTP Server IP](#tftp-server-ip)). The filename is the board's MAC address: e.g., `02-00-5e-00-53-03.yml`.

Example config for a single 128x64 panel:

```yaml
grid: 1x1
panel_width: 128
panel_height: 64
J1: 0,0
```

Example config for 2 daisy-chained panels on J1 (2x1 grid):

```yaml
grid: 2x1
panel_width: 128
panel_height: 64
J1: 0,0 1,0
```

Space-separated positions per output assign chain slots: the first position is chain slot 0 (directly connected), the second is chain slot 1 (daisy-chained). Each output supports up to 2 panels.

Place config files in your TFTP root directory. The layout is applied automatically at boot.

Every key the parser takes (`layout.rs`, `LayoutConfig::parse`; `key: value` or `key=value`, `#` comments, unknown keys ignored):

| Key | Value |
|---|---|
| `grid` | `CxR`, the virtual display in panels |
| `panel_width`, `panel_height` | one panel's size in pixels |
| `J1` … `J8` | grid positions `col,row`, one per chain slot, for that connector |
| `mode`, `program` | `mode: program` runs the on-panel program named by `program:`; anything else streams |
| `rgb_order` (`color_order`) | channel order by name (`RGB`, `GRB`, …); an unknown name leaves RGB |
| `wall`, `origin` | `wall: 512x256` and `origin: 256,0` -- the whole wall's size and this card's corner of it, for programs spanning cards |
| `drive` (`chip`) | S-PWM builds: the driver-chip table to load -- `dp3264s`, `icnd1065`, `dp3364s`. Ignored by a HUB75 bitstream |
| `chip_reg` | S-PWM builds, firmware 5.6.0: `chip_reg: <addr> <value>` or `<addr> <r> <g> <b>` (decimal or `0x` hex), one line per register, up to 48. Replaces that register in the chip's table; an address the table does not carry is ignored (`chip_regs` / `chip_regs_matched` in `/api/status`) |
| `rotate` | firmware 5.7.0: how the modules are mounted, degrees clockwise from the framebuffer (`0`, `90`, `180`, `270`). Turns what the card draws itself -- programs, the boot screen and its banner; streamed frames are turned by the sender. Anything else is read as 0 |
| `group_origins` | S-PWM builds: `x,y` per RGB group, where each group's strip sits in the module |
| `group_columns` (`chain_columns`), `group_scan` (`scan`) | runtime-geometry builds (`exp/build-rt.sh`, gateware 3.6.x / firmware 5.8.0; released in 5.8.0; both P1.25 modules run on it as of 5.8.3): one RGB group's size in columns (a multiple of 16) and scan lines (even), inside the bitstream's maxima. Absent keys mean the maxima. A value the bitstream cannot scan is refused and reported (`geom_columns`, `geom_scan`, `geom_max`, `geom_refused` in `/api/status`). [docs/RUNTIME-GEOMETRY.md](docs/RUNTIME-GEOMETRY.md) |
| `clk_div` | S-PWM builds: panel shift clock = system clock / N, i.e. the refresh rate |
| `write_throttle` | idle cycles between pixel DMA writes, 0-15 |

A boot config must fit **2048 bytes**; a longer one is refused whole rather than half-applied. The same text can be applied to a running card with `POST /api/layout` (512 bytes a request; nothing is stored, the next boot config replaces it), and the telnet console sets panel positions.

## Development

See [ARCH.md](ARCH.md) for architecture details, memory map, double buffering internals, and debugging tips.

### Building from Source

The Docker environment includes all dependencies:
- Yosys, nextpnr-ecp5, Trellis (FPGA toolchain)
- LiteX, Migen (SoC framework)
- Rust with riscv32i target (firmware)
- openFPGALoader (programming)

### Running Tests

```bash
# Test network connectivity
ping <board-ip>

# Test telnet
telnet <board-ip> 23
```

## Boot Workflow

1. **Power on** — the FPGA configures from SPI flash (or SRAM if loaded via `./build.sh boot`)
2. **BIOS DHCP** — the BIOS derives the card's MAC from the flash unique ID and asks DHCP for an address and a boot server (option 66, then BOOTP `siaddr`, then the compiled-in fallback); `patches/litex-bios-dhcp.patch`, since 2.3.0
3. **BIOS netboot, then flash** — it fetches `boot.bin` over TFTP on port 6969; if nothing answers it boots the firmware copy in flash (`patches/litex-bios-netboot-first.patch` puts netboot first)
4. **Firmware starts** — same MAC, displays a default pattern
5. **DHCP** — acquires its IP address; falls back to the baked `--ip` (`192.168.1.50/24` by default) after 10 seconds
6. **TFTP config** — fetches `<mac>.yml` from the TFTP server (DHCP option 66, or the fallback) on port 6969
7. **Layout applied** — panel grid configured from YAML, display redrawn at virtual resolution

**A card chained behind another** ([ARCH.md §14](ARCH.md#14-card-chains--one-ip-many-cards))
gets no lease at step 2 — its broadcasts never leave the chain — so it boots
its flash copy at step 3, waits for marquee's discovery packet instead of DHCP
at step 5, takes the head's IP, and fetches its own `<mac>.yml` from the
discovery's sender through the head. If it is not running its chain's assigned
image, marquee then netboots it into that image (`POST /api/netboot`: fetched
into RAM, length and CRC checked, started; flash untouched).

The TFTP server (`./build.sh start` or auto-started by `boot`) serves both the firmware binary and per-board YAML configs from the `.tftp/` directory. Use `./build.sh stop` to stop it. In production marquee is the TFTP server.

### TFTP Server IP

Both the BIOS (`boot.bin`) and firmware (`<mac>.yml`) fetch from the same TFTP server on port **6969**, and both take it from DHCP: **option 66** (Boot Server Host Name) if provided, else the BOOTP `siaddr`, else the compiled-in `TFTP_SERVER` from `build.sh` (`192.168.1.10` by default). Configure your DHCP server to provide it: for **Windows DHCP Server**, set **Option 066**; for **dnsmasq**, use `dhcp-boot=boot.bin,,<ip>`.

The web status page shows the active TFTP server and whether it came from Option 66 or the fallback.

## Updating Firmware Over the Network (OTA)

**You attach JTAG once, to a new board, and then never again.** Both artifacts
update over the network:

| | Lives | OTA | Live when |
|---|---|---|---|
| **Rust firmware** (`boot.bin`) | fetched from the server at every boot -- by the BIOS on a head, by the firmware itself on a chain member (`POST /api/netboot`, 5.0.5). Flash at `0x100000` holds a **survival copy**, for a boot with no server | yes, and an update never writes flash | on reboot (a member: in about 8 s, no reboot) |
| **FPGA bitstream** (`.bit`) | the card's SPI flash, offset 0 | yes (2.16.0) | next **power cycle** |

Each has its **own** version, because they change at very different rates:

```console
$ curl http://<card>/api/bitstream
{"bitstream": {"name":"e320-128x64-out6-v1.0.0", "version":"1.0.0",
               "board":"e320", "panel":"128x64", "outputs":6, "clock_mhz":35},
 "firmware":  {"version":"2.20.0"}}
```

The bitstream's semver is `gateware/VERSION` (see `gateware/VERSION.md` for when
to bump it); the firmware's is its Cargo.toml. Tying them together would make
every firmware release look like it shipped new FPGA logic.

The one-time JTAG step writes a **golden** bitstream at `0x200000` and the
application at offset 0. After that: `tftp_flash.rs` fetches a new bitstream over
TFTP and streams it into flash offset 0 a sector at a time, erasing, programming
and verifying as it goes, while still running the gateware it replaces. Nothing
reconfigures an FPGA from software, so it is live at the next power-on.

**A bad push cannot strand a board.** If the new image fails to configure, the
ECP5 scans forward, finds the golden, and boots that — measured with a corrupted
primary, a corrupted primary built `--bootaddr 0`, and a primary whose first
sector was erased (`docs/FLASHING.md` §2d), and again after the golden was
replaced with an E320-native image. `region_is_writable()` refuses the golden
region unconditionally and refuses an application write when no golden is present.

**Provisioning a new board is two JTAG writes, once** — golden then application.
After that it is network-managed for life. The procedure is
[FLASHING.md §1a](docs/FLASHING.md); updating it afterwards is §2e.

### The variants

Firmware is **not** one binary: the system clock is baked in from the gateware and
S-PWM support is a compile-time feature, so there are four. Bitstreams differ by
board, panel and output count. `tools/rebuild_all_variants.sh` builds and stages
every one in a single pass — the order is not incidental, because the S-PWM
variants need the PAC regenerated from an S-PWM bitstream's SVD first and the PAC
is one checked-in directory.

| | clock | S-PWM | for |
|---|---|---|---|
| `5a-75e-v<ver>.bin` | 40 MHz | no | plain HUB75 panels |
| `5a-75e-spwm-v<ver>.bin` | 40 MHz | yes | P1.25 S-PWM panels |
| `e320-v<ver>.bin` | 35 MHz | no | plain HUB75 panels |
| `e320-spwm-v<ver>.bin` | 35 MHz | yes | P1.25 S-PWM panels (DP3264S, ICND1065L) |

**P1.25 modules are 256×128 px** (320×160 mm, 1/64 scan), and the preset
follows the module, not the pitch. The four-module ARGO wall (DP3264S) runs
`--panel 256x128-dp3264s-hub320 --outputs 4`, built by `exp/build-out4.sh`;
Myled's module (ICND1065L, strips stacked) is the same script with
`PANEL=128x256-icnd1065l-hub320`. The older `256x128-icnd1065` (5A-75E) and
`256x256-icnd1065-hub320` (E320) presets still build but have never lit a
module, and the S-PWM path fits 3 outputs in a standard build, 4 in the
trimmed one.

Marquee owns that: it stages images, records which chain runs which, pushes one
to a chain, and rolls one across a wall. Its **firmware sync** then keeps every
card on its assigned image (a head found running something else is rebooted, a
member netbooted) and each card's flash survival copy equal to it, written once
the image has run cleanly for ten minutes -- marquee `ARCH.md` §2c.

```bash
# Build an image, then stage it (Marquee's Firmware page does this in a browser)
./build.sh firmware
curl -F file=@.tftp/boot.bin http://marquee/api/v1/firmware

# Point a card (for a chain: its head) at it and reboot into it, verifying
# it comes back; a head brings its members along
curl -X POST http://marquee/api/v1/cards/1/firmware \
     -H 'Content-Type: application/json' \
     -d '{"firmware":"boot.bin"}'

# What is each card ASSIGNED, and what is it actually RUNNING?
curl http://marquee/api/v1/firmware/running
```

Three things worth knowing:

- **Assigned is not running.** They differ exactly when it matters: an
  assignment nothing has rebooted into, or a card that fell back to the default
  image. `/api/status` reports the running version from **2.15.0** onward.
- **A bad image cannot brick a card.** An update writes nothing to the board,
  head or member, so recovery is to assign a different image. The survival copy
  in flash is only replaced by an image that has already run cleanly, and is
  written through a checked path (whole image into RAM, length and CRC checked,
  then erase, write, read back). A member left without bootable firmware is
  recovered by cabling it straight to the switch, where it netboots. A bad
  *bitstream* cannot strand a card either: the golden image takes over (above).
- **The firmware can also write its own flash** (`/api/flash/job`), which is
  what makes an A/B bitstream update possible without JTAG.
  `tools/ota_selftest.sh <card-ip>` proves that path end to end on real
  hardware.

## Pre-built Binaries

The repo includes pre-built binaries so you can flash and boot without rebuilding:

| File | Description |
|------|-------------|
| `bitstreams/128x64.bit` | FPGA bitstream for 128x64 panels (default) |
| `bitstreams/96x48.bit` | FPGA bitstream for 96x48 panels |
| `bitstreams/64x32.bit` | FPGA bitstream for 64x32 panels |
| `bitstreams/64x64.bit` | FPGA bitstream for 64x64 panels |
| `.tftp/boot.bin` | Rust firmware binary (universal, all panels) |

```bash
# Flash bitstream for your panel size
./build.sh flash                       # default (128x64)
./build.sh --panel 96x48 flash         # specific panel

# Serve firmware via TFTP
./build.sh start
```

## Known Issues

- **Art-Net**: Palette updates work, direct pixel writes commented out
- **TFTP is on non-standard port 6969**, set in the gateware (`TFTP_SERVER_PORT`).
  The BIOS has done its own DHCP with option 66 since 2.3.0 (the research
  behind that predates it; the TFTP boot research is not published)
- **`mcycle` is not implemented on this VexRiscv build** and reads 0, which is
  why `/api/bench` reported all zeros until it was rebuilt as a deferred job
  (2.6.0; GET twice). Time new code with `Timer0`, as `/api/flashbench` and
  `/api/isrprof` do
- **`bench_stream.py`'s loss column is meaningless with the pixel filter on** —
  it reads a CPU counter that is deliberately always zero. Judge by
  `frames_completed` / `frames_dropped` from the panel instead

## Architecture

**The CPU is not in the pixel path** — not the data, and since v1.38.0 not even
the interrupt. The receive stream is forked in gateware:

```
                    ┌─► Hub75UdpDma ──► SDRAM          (pixels, always)
   LiteEth core ────┤
                    └─► classifier ──► invalidator ──► CPU MAC
                          pixel packet?  kill it        (everything else)
```

`SmolEthPixelClassifier` recognises a UDP pixel packet by beat 9 and has
`SmolEthInvalidator` end the frame with an error, so the MAC discards it and
never raises a receive event. That moved full-RGB streaming from ~5.5 fps to
~33 fps on a nine-panel frame, because the bound was one interrupt per packet
(~10,000 cycles), not pixels.

The gateware also **flips the framebuffer at the frame boundary**. A CPU-timed
swap leaves the DMA writing the incoming frame into the half about to be
displayed — a band of the next frame across the top of the image, measured at up
to 6.6% even with the CPU polling flat out.

Everything else — DHCP, HTTP, telnet, Art-Net, ARP — still runs in the ISR via
smoltcp, as it has since v1.10.0. The main loop handles display refresh, program
ticks, and swapping finished frames onto the glass.

Full detail in [ARCH.md](ARCH.md); measurements in
[docs/BENCHMARKS.md](docs/BENCHMARKS.md).

See [CHANGELOG.md](CHANGELOG.md) for version history and fixes.

## Credits

This board ships as a closed appliance: no public pinout, no schematic, nothing.
Everything here stands on work published by other people.

**The reverse engineering, without which none of this is possible**

- [q3k/chubby75](https://github.com/q3k/chubby75) — Sergiusz Bazanski (q3k) and
  contributors. The connector and pin maps this gateware drives come from there.
- [DerFetzer/colorlight-litex](https://github.com/DerFetzer/colorlight-litex) —
  the original LiteX implementation on this board, and where this one started.
- [litex-boards](https://github.com/litex-hub/litex-boards) — the
  `colorlight_5a_75e` platform, including all three hardware revisions.

**The SoC is assembled, not written**

- [LiteX](https://github.com/enjoy-digital/litex) + Migen, with LiteEth,
  LiteDRAM and LiteSPI — SoC, Ethernet MAC, SDRAM and flash controllers.
- [VexRiscv](https://github.com/SpinalHDL/VexRiscv) — the CPU.
- [Yosys](https://github.com/YosysHQ/yosys),
  [nextpnr](https://github.com/YosysHQ/nextpnr) and
  [Project Trellis](https://github.com/YosysHQ/prjtrellis) — a complete
  open-source ECP5 toolchain, which is why a $15 board can be reprogrammed.
- [openFPGALoader](https://github.com/trabucayre/openFPGALoader) — flashing.
- [smoltcp](https://github.com/smoltcp-rs/smoltcp) — the slow-path TCP/IP stack.
- [DejaVu fonts](https://dejavu-fonts.github.io/) — everything the panels set.

See [THIRD-PARTY.md](THIRD-PARTY.md) for the full list with licences.

## License

BSD-2-Clause. See [LICENSE](LICENSE), and individual files for specific
attributions.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Submit a pull request

Please follow existing code style and include tests where applicable.

## References

### Hardware

| Component | Documentation |
|-----------|---------------|
| **Colorlight 5A-75E** | [q3k/chubby75 Reverse Engineering](https://github.com/q3k/chubby75/tree/master/5a-75e) |
| **Lattice ECP5 FPGA** | [ECP5 Family Datasheet](https://www.latticesemi.com/view_document?document_id=50461) · [Technical Reference](https://www.latticesemi.com/view_document?document_id=50462) |
| **HUB75 Protocol** | [HUB75 LED Matrix Panels](https://learn.adafruit.com/32x16-32x32-rgb-led-matrix/overview) · [Timing Diagrams](https://bikerglen.com/projects/lighting/led-panel-1up/) |
| **RTL8211FD PHY** | [Datasheet](https://www.realtek.com/en/products/communications-network-ics/item/rtl8211fd-cg) |

### FPGA Toolchain

| Tool | Description |
|------|-------------|
| **Yosys** | [Open synthesis suite](https://github.com/YosysHQ/yosys) |
| **nextpnr-ecp5** | [Place and route for ECP5](https://github.com/YosysHQ/nextpnr) |
| **Project Trellis** | [ECP5 bitstream documentation](https://github.com/YosysHQ/prjtrellis) |
| **openFPGALoader** | [Universal FPGA programmer](https://github.com/trabucayre/openFPGALoader) |

### SoC Framework

| Component | Documentation |
|-----------|---------------|
| **LiteX** | [SoC builder framework](https://github.com/enjoy-digital/litex) · [Wiki](https://github.com/enjoy-digital/litex/wiki) |
| **VexRiscv** | [RISC-V CPU core](https://github.com/SpinalHDL/VexRiscv) |
| **LiteEth** | [Ethernet MAC](https://github.com/enjoy-digital/liteeth) |
| **Migen** | [Python-to-HDL](https://github.com/m-labs/migen) |

### Firmware

| Component | Documentation |
|-----------|---------------|
| **Rust Embedded** | [Embedded Rust Book](https://docs.rust-embedded.org/book/) |
| **RISC-V Target** | [riscv32i-unknown-none-elf](https://doc.rust-lang.org/rustc/platform-support/riscv32-unknown-none-elf.html) |
| **smoltcp** | [TCP/IP stack](https://github.com/smoltcp-rs/smoltcp) · [Docs](https://docs.rs/smoltcp) |

### Protocols

| Protocol | Specification |
|----------|---------------|
| **DHCP** | [RFC 2131](https://datatracker.ietf.org/doc/html/rfc2131) · [Option 66 (TFTP)](https://datatracker.ietf.org/doc/html/rfc2132#section-9.4) |
| **TFTP** | [RFC 1350](https://datatracker.ietf.org/doc/html/rfc1350) |
| **Art-Net** | [Protocol Specification](https://art-net.org.uk/resources/art-net-specification/) |

### Project Documentation

| Document | Description |
|----------|-------------|
| **[docs/GETTING-STARTED.md](docs/GETTING-STARTED.md)** | **Start here.** Parts on a desk → a lit panel, in an hour |
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | Building, flashing and deploying, properly |
| [docs/RUNTIME-GEOMETRY.md](docs/RUNTIME-GEOMETRY.md) | One bitstream, the module's size as a setting (`group_columns:`, `group_scan:`) |
| [docs/PROGRAMMING.md](docs/PROGRAMMING.md) | Writing programs that run on the panel + full API |
| [docs/FPGA-GUIDE.md](docs/FPGA-GUIDE.md) | Beginner's guide to the gateware — no Verilog required |
| [docs/HARDWARE.md](docs/HARDWARE.md) | What we found out about the 5A-75E card itself |
| [docs/BENCHMARKS.md](docs/BENCHMARKS.md) | Every measured number, with its conditions |
| [API.md](API.md) | The panel's HTTP API and UDP pixel protocol |
| [ARCH.md](ARCH.md) | Architecture, memory map, ISR design, the two pixel paths |
| [docs/DISPLAY-PROGRAMS.md](docs/DISPLAY-PROGRAMS.md) | Design reasoning behind on-panel programs |
| [CHANGELOG.md](CHANGELOG.md) | Version history and release notes |
| [LICENSE](LICENSE) | BSD 2-Clause |
| [THIRD-PARTY.md](THIRD-PARTY.md) | What is redistributed here, and under what terms |
