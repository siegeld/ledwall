# Colorlight HUB75 LED Controller

[![Version](https://img.shields.io/badge/version-1.40.0-brightgreen.svg)](CHANGELOG.md)
[![License](https://img.shields.io/badge/license-BSD--2--Clause-blue.svg)](LICENSE)
[![FPGA](https://img.shields.io/badge/FPGA-Lattice%20ECP5-green.svg)](https://www.latticesemi.com/Products/FPGAandCPLD/ECP5)
[![Board](https://img.shields.io/badge/Board-Colorlight%205A--75E-orange.svg)](http://www.colorlight-led.com/)

A complete FPGA-based LED panel controller for **HUB75** displays, built on the **Colorlight 5A-75E** receiver card. Features a LiteX SoC with VexRiscv CPU, Ethernet connectivity, and a Rust-based firmware with telnet management console.

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
> | Change panel geometry, outputs or chaining | **yes** — it is in the bitstream |
> | Update firmware | **yes** to build; then `./build.sh deploy` |
> | First bring-up of a new board | **yes** — JTAG |
>
> The firmware carries *every* program and the config picks one, so switching a
> panel between them is a marquee change. Adding a new one is a firmware build.

Two ways to drive a panel, and it does both:

- **Stream to it** — send frames over UDP from anywhere. ~103 fps at 128×128.
- **Run code on it** — write YAML, compile it into the firmware, and the panel
  composes its own pixels from values pushed to it. Like ESPHome, for LED signs.
  Unplug the host and it keeps running.

## Features

- **HUB75 LED Panel Driver** - 6 outputs (J1–J6) with 2-panel chaining (up to 12 panels total)
- **DHCP Networking** - Automatic IP via DHCP with unique MAC from SPI flash
- **TFTP Boot Config** - Per-board YAML layout config fetched at boot via `<mac>.yml`
- **HTTP REST API** - Web status page and JSON API on port 80
- **Bitmap UDP Protocol** - Three wire formats on UDP port 7000:
  `'B','M'` RGB888, `'B','I'` **indexed** (1 byte/px, ~3x fewer packets and so
  ~3x the frame rate), and `'B','P'` palette (256 entries in one packet)
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
| FPGA Board | Colorlight 5A-75E V8.2 (Lattice ECP5-25F) |
| Programmer | USB Blaster, FTDI FT2232, or compatible JTAG |
| LED Panels | HUB75/HUB75E compatible (96x48, 128x64, 64x32, 64x64) |
| Network | 100Mbps Ethernet |

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

The default build is `chain_length=1` (one panel per output) with `outputs=6`. Chain length and output count are separate `build.sh` flags (`--chain-length`, `--outputs`); the gateware supports up to 12 outputs and 2 panels per chain.

> `chain_length` must match `const CHAIN_LENGTH` in `sw_rust/barsign_disp/src/hub75.rs` — the firmware bakes it in at compile time. `build.sh` reads the constant and refuses to build a gateware that disagrees. Its own default said `2` from v1.7.0 until 2026-09-17, long after the firmware moved to `1`, which meant a plain `./build.sh bitstream` produced a mismatch that builds, boots and looks fine.

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

There are two TFTP fetches at boot — each uses a different server address:

| Fetch | Client | Default Server | How to Change |
|-------|--------|----------------|---------------|
| `boot.bin` (firmware) | BIOS | `192.168.1.10:6969` | Edit `gateware/colorlight.py` line 279, rebuild bitstream |
| `<mac>.yml` (config) | Firmware | DHCP Option 66, or `192.168.1.10` | Configure your DHCP server |

**To change the BIOS TFTP server:**

1. Edit `gateware/colorlight.py`:
   ```python
   remote_ip="192.168.1.100",  # Change from 192.168.1.10
   ```

2. Rebuild and flash:
   ```bash
   ./build.sh -p 128x64 bitstream pac firmware flash
   ```

**To change the firmware config server:**

Configure your DHCP server to provide Option 66 (TFTP Server Name):
- **dnsmasq**: `dhcp-option=66,192.168.1.100`
- **Windows DHCP**: Set Option 066 "Boot Server Host Name"
- **ISC DHCP**: `option tftp-server-name "192.168.1.100";`

If no Option 66 is provided, firmware falls back to `192.168.1.10`.

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
./build.sh --default-config configs/standalone-128x128.yml firmware
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
│   ├── hub75.py           # HUB75 display driver
│   ├── gen_test_image.py  # Test pattern generator
│   ├── helper.py          # HUB75 connector pin definitions
│   └── smoleth.py         # Ethernet module (legacy)
├── bitstreams/            # Pre-built bitstreams for all panel sizes
├── sw_rust/               # Rust firmware
│   ├── barsign_disp/      # Main application
│   ├── litex-pac/         # Peripheral Access Crate
│   └── smoltcp-0.8.0/     # Patched smoltcp (DHCP Option 66)
├── tools/                 # Python tools for sending content to the panel
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

Three bitstream parameters control multi-panel capacity: `--panel WxH` (per-panel pixel size), `--chain-length N` (max panels daisy-chained per output, default 2), and `--outputs N` (HUB75 connectors, default 6). Maximum total panels = outputs × chain_length. The virtual display size is set at runtime by the TFTP layout config.

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

| File | Setting | Description |
|------|---------|-------------|
| `build.sh` | `OUTPUTS=6` | Passed to gateware build (`--outputs`) |
| `build.sh` | `CHAIN_LENGTH=2` | Panels per output chain (`--chain-length`) |
| `sw_rust/barsign_disp/src/hub75.rs` | `const OUTPUTS: u8 = 6` | Firmware output count |
| `sw_rust/barsign_disp/src/hub75.rs` | `const CHAIN_LENGTH: u8 = 2` | Firmware chain length |

Both outputs and chain length must match between gateware and firmware. A mismatch (e.g., firmware accessing panel CSRs that don't exist in the bitstream) will crash the SoC. The `layout.rs` constants `MAX_OUTPUTS` and `MAX_CHAIN` should also match.

After changing output count or chain length, rebuild everything:
```bash
./build.sh bitstream pac firmware
```

### IP Address (DHCP)

The firmware acquires its IP address via DHCP at boot. If no DHCP server responds within 10 seconds, it falls back to `192.168.1.50/24`. Check your DHCP server's lease table to find the board's IP, or use the board's MAC address (`02:xx:xx:xx:xx:xx`) to assign a fixed lease.

### Panel Layout (TFTP Boot Config)

At boot, the firmware fetches a per-board YAML config file from the TFTP server (discovered via DHCP — see [TFTP Server IP](#tftp-server-ip)). The filename is the board's MAC address: e.g., `02-78-7b-21-ae-53.yml`.

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

Panel layout can also be configured at runtime via the HTTP API (`POST /api/layout`) or telnet commands.

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

1. **Power on** — BIOS loads bitstream from SPI flash (or SRAM if loaded via `./build.sh boot`)
2. **BIOS TFTP** — BIOS fetches `boot.bin` (firmware) from the TFTP server on port 6969
3. **Firmware starts** — Reads SPI flash unique ID, derives unique MAC (`02:xx:xx:xx:xx:xx`), displays default test pattern
4. **DHCP** — Acquires IP address; falls back to `192.168.1.50/24` after 10 seconds
5. **TFTP config** — Firmware fetches `<mac>.yml` from the TFTP server (DHCP Option 66 address, or fallback `192.168.1.10`) on port 6969
6. **Layout applied** — Panel grid configured from YAML, display redrawn at virtual resolution

The TFTP server (`./build.sh start` or auto-started by `boot`) serves both the firmware binary and per-board YAML configs from the `.tftp/` directory. Use `./build.sh stop` to stop it.

### TFTP Server IP

Both the BIOS (`boot.bin`) and firmware (`<mac>.yml`) fetch from the same TFTP server on port **6969**.

- **BIOS `boot.bin` fetch** — The BIOS fetches firmware from the hardcoded server `192.168.1.10:6969`.

- **Firmware `<mac>.yml` config fetch** — Uses DHCP Option 66 if provided, otherwise falls back to `192.168.1.10`. Configure your DHCP server to provide the TFTP server address: for **Windows DHCP Server**, set **Option 066** (Boot Server Host Name); for **dnsmasq**, use `dhcp-boot=boot.bin,,<ip>`.

  The web status page shows the active TFTP server and whether it came from Option 66 or the hardcoded fallback.

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
- **BIOS TFTP**: Uses hardcoded server `192.168.1.10` on non-standard port 6969
  (DHCP option 66 works for the *firmware's* config fetch; the BIOS stage is
  still compile-time — see [docs/tftp-boot-research.md](docs/tftp-boot-research.md))
- **`/api/bench` reports all zeros**: `mcycle` is not implemented on this
  VexRiscv build. Use `Timer0`, as `/api/flashbench` and `/api/isrprof` do
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
| [docs/PROGRAMMING.md](docs/PROGRAMMING.md) | Writing programs that run on the panel + full API |
| [docs/FPGA-GUIDE.md](docs/FPGA-GUIDE.md) | Beginner's guide to the gateware — no Verilog required |
| [docs/HARDWARE.md](docs/HARDWARE.md) | What we found out about the 5A-75E card itself |
| [docs/BENCHMARKS.md](docs/BENCHMARKS.md) | Every measured number, with its conditions |
| [API.md](API.md) | The panel's HTTP API and UDP pixel protocol |
| [ARCH.md](ARCH.md) | Architecture, memory map, ISR design, the two pixel paths |
| [docs/DISPLAY-PROGRAMS.md](docs/DISPLAY-PROGRAMS.md) | Design reasoning behind on-panel programs |
| [CHANGELOG.md](CHANGELOG.md) | Version history and release notes |
| [CLAUDE.md](CLAUDE.md) | Notes for AI agents working in this repo |
| [LICENSE](LICENSE) | BSD 2-Clause |
| [THIRD-PARTY.md](THIRD-PARTY.md) | What is redistributed here, and under what terms |
