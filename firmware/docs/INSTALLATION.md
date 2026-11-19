# Installation

> **For flashing, see `docs/FLASHING.md`** -- the single authority. This file
> covers one-time toolchain and wiring setup only.


Getting a Colorlight 5A-75E driving HUB75 panels, from parts on a desk to a
sign that boots on its own.

Read this once through before starting. Two steps have a trap that costs an
evening if you meet it blind, and both are flagged.

---

## 1. What you need

| | |
|---|---|
| **Controller** | Colorlight **5A-75E**, revision **8.2** (Lattice ECP5 `LFE5U-25`) |
| **Panels** | HUB75 / HUB75E. Supported sizes: `128x64`, `256x64`, `96x48`, `64x32`, `64x64` |
| **Programmer** | FTDI **FT2232**, USB Blaster, or DirtyJTAG |
| **Power** | 5V at several amps — **sized for the panels, not the board** |
| **Network** | Ethernet, same broadcast domain as the machine you build on. A card chain (§9b) needs **gigabit** on every hop |
| **Host** | Linux with Docker. Nothing else is installed on the host |

A 128×64 panel at full white draws on the order of **4 A at 5 V**. Two panels
and a controller on a 2 A supply will brown out, and a browning-out panel does
not look like a power problem — it looks like corrupt output, a hung board, or
a flaky network. Size the supply for every panel at once.

### JTAG wiring

JTAG is a 4-pin header beside the FPGA (U33); power is a separate 2-pin header.

| Pin | Signal |
|---|---|
| J27 | TCK |
| J31 | TMS |
| J32 | TDI |
| J30 | TDO |
| J33 | 3.3V |
| J34 | GND |

> **Trap — the FT2232 has two channels, and they are different cables.**
> `openFPGALoader` treats them as separate devices: `ft2232` is channel A,
> `ft2232_b` is channel B. **Picking the wrong one does not report a bad cable.**
> The adapter opens, the frequency negotiates, and the chain scans `empty` —
> which is indistinguishable from an unpowered board. Before suspecting power or
> wiring, try the other channel:
>
> ```bash
> openFPGALoader --cable ft2232   --detect    # channel A
> openFPGALoader --cable ft2232_b --detect    # channel B
> ```
>
> A live ECP5 answers `idcode 0x41111043 … LFE5U-25`. Every command below takes
> `--cable`; use whichever one answered.

---

## 2. Build the toolchain image

Everything builds in Docker, so the host stays clean. Once, and it takes a while
— it contains the whole FPGA toolchain:

```bash
git clone <this repo> && cd colorlight
./build.sh docker
```

---

## 3. Choose the panel geometry

The **bitstream** is built for a panel size, an output count, and a chain
length. These are hardware facts, not runtime settings:

```bash
./build.sh --panel 128x64 --outputs 6 --chain-length 1 bitstream
```

- `--panel` — the size of **one** physical panel
- `--outputs` — how many HUB75 connectors you drive (rev 8.2 has 16; the
  default build drives 6, and the firmware's layout parser accepts J1–J8)
- `--chain-length` — panels daisy-chained per connector (a *panel* chain, not
  the *card* chain of §9b). **Chaining halves the
  refresh rate**; outputs shift in parallel and cost nothing extra, so prefer
  **wide across connectors** over **deep down chains**.

Defaults produce `columns=128, rows=64, scan=32, chain_length=1, outputs=6`.
`chain_length` must match `const CHAIN_LENGTH` in `sw_rust/barsign_disp/src/hub75.rs`;
`build.sh` reads it from there and refuses to build a gateware that disagrees.
The same goes for `--outputs` and `pub const OUTPUTS` in the same file — which
is **4** on `main` (the four-output E320 build), so a build for another output
count must change it first.

The *arrangement* of those panels — which connector is where in the grid — is
**not** baked in. It comes from a config file fetched at boot (§6).

---

## 4. Addresses: DHCP first, baked-in fallbacks

**The card gets its address by DHCP.** Since 2.3.0 the BIOS does its own DHCP
(`patches/litex-bios-dhcp.patch`) with a MAC derived from the flash's unique
ID, and takes its boot server from the lease: **option 66** first, then the
BOOTP `siaddr`. So the normal setup is a DHCP reservation for the card's MAC
and option 66 pointing at the host that serves `boot.bin` (marquee). The
firmware then does DHCP again for its own address.

Two addresses are still compiled into the gateware, **as fallbacks only**:

```bash
./build.sh --ip <fallback-ip> --panel 128x64 bitstream
```

- `--ip` (`IP_ADDRESS` in `build.sh`) — the card's address when no DHCP
  server answers. The firmware falls back to its own static address after
  10 seconds without a lease (`network.rs`).
- `TFTP_SERVER` in `build.sh` — where the BIOS fetches `boot.bin` when the
  lease names no boot server. It is compiled into every bitstream, so keep the
  host it names on a stable address.

Pick a fallback address outside your DHCP pool.

> **Two addresses, and they are not always the same one.** The BIOS's lease is
> what it fetches firmware with; the **firmware** then brings up its own
> address. If your board fetches `boot.bin` from one IP and then answers HTTP
> on another, that is why — it is not a fault.

> **A card chained behind another gets no lease at all**, by design: its DHCP
> broadcast never leaves the chain. It takes the head's IP and answers on
> port 8000+hop, and its BIOS falls through to booting firmware from flash.
> See [ARCH.md §14](../ARCH.md#14-card-chains--one-ip-many-cards).

---

## 5. Build and load the firmware

```bash
./build.sh firmware
./build.sh --panel 128x64 --cable ft2232_b sram
```

`sram` loads the bitstream over JTAG and starts a TFTP server serving
`.tftp/boot.bin`. The board's BIOS fetches that and runs it. The whole loop
takes about **ten seconds**, which is the development cycle.

> **`sram` is VOLATILE.** The tool says so: *"This is temporary — configuration
> will be lost on power cycle."* It is the right thing while iterating and the
> wrong thing to walk away from — see §7.

Confirm it came up:

```bash
curl http://<board-ip>/api/status
curl http://<board-ip>/           # the status page
```

---

## 6. Tell it how the panels are arranged

The board fetches a per-MAC config over TFTP at boot, named for its MAC with
dashes: `aa-bb-cc-dd-ee-ff.yml`.

```yaml
grid: 1x2              # columns x rows OF PANELS
panel_width: 128       # one physical panel
panel_height: 64
J1: 0,0 0,0            # connector 1 -> grid cell (col,row), once per chain slot
J2: 0,1 0,1            # connector 2 -> the cell below it
mode: stream           # or: program   (see the programming guide)
```

`J<n>` maps a connector to a grid position, `col,row`, repeated per chain slot.
Two 128×64 panels stacked vertically make one 128×128 canvas, as above.

Unknown keys are **ignored silently**, so a typo in a key name does nothing at
all rather than complaining. If a setting appears to have no effect, check the
spelling first.

---

## 7. Deploy: make it boot on its own

Write **both** the bitstream and the firmware to SPI flash:

```bash
./build.sh --panel 128x64 --cable ft2232_b flash-all
```

Then power-cycle.

It comes back in about **6 seconds**, running entirely from flash.

> ### This page said the opposite until 2026-09-17
>
> It carried a warning that cold power-on *never* boots from flash — "no ping,
> no TFTP request, the BIOS never even starts" — and blamed flash power-up
> timing or panel inrush sagging the rail.
>
> **All of that was wrong, and none of it was the board.** The FPGA configured
> every time. The firmware was being written to flash **without its FBI header**,
> so the BIOS read the first RISC-V instruction as a 1 GB image length and
> rejected it, then fell through to a netboot that cannot work before the link is
> up. `build.sh` now wraps the image with `crcfbigen.py -f -l` and refuses to
> flash one whose header would not pass.
>
> Kept here because the *shape* of the mistake is worth remembering: every
> indicator in use sat downstream of the real failure, so a healthy board looked
> identical to a dead one, and two sessions went into hardware theories.


> **This is the step it is easiest to skip**, because `sram` works and you use it
> fifty times an afternoon. Skip it and the next power cut brings the board back
> on whatever gateware was in flash *before* you started — quite possibly old
> enough that today's firmware crashes on it, since the CSRs it expects are not
> there.

### After this, the build host is out of the picture

**Once the card is programmed, all you need is marquee.** Nothing in this repo
runs at runtime — it is a build and flashing toolkit. marquee serves the panel
its firmware and config at boot, then streams pixels or pushes values to it.

Deploying *new* firmware to a networked fleet is `./build.sh deploy`, which
copies the build into marquee's firmware root. **netboot takes precedence over
flash**: while marquee is reachable, the image marquee serves is the one that
runs, and the flashed copy is the fallback for when it is not.

That ordering is ours, not LiteX's — `patches/litex-bios-netboot-first.patch`
moves `netboot()` ahead of `flashboot()`. Stock LiteX tries flash first, which
would mean a panel with good firmware in flash never fetches a deployed image
and `deploy` silently does nothing. The cold-boot case still lands on flash,
because `netboot()` gives up in 0.32 s when nothing answers. A card **chained
behind another** always lands on flash — it can get no lease — and marquee then
netboots it into the assigned image over HTTP (§9b).

**So keep the two in step**: `deploy` changes what a panel runs *now*,
`flash-firmware` changes what it runs *after a power cut*. A panel whose flash is
older than its deployment will quietly change behaviour the next time the power
blinks. With marquee (0.45.0+) this is automatic: its firmware sync rewrites a
card's flash copy once the assigned image has run cleanly for ten minutes.

### A panel that needs no server

`flash-all` frees the board from the build host. It does **not** free it from the
fleet server: the mode, the program and the panel map still arrive over TFTP, so
a board with no network comes up as a single panel expecting a stream.

To close that, bake a config into the firmware first:

```bash
./build.sh --default-config custom/example/configs/panel.yml firmware
./build.sh --panel 128x64 --cable ft2232_b flash-all
```

Same format as `<mac>.yml` (§6), parsed by the same parser, applied at boot
before the network is up. A TFTP config still overrides it when one arrives, so
this is safe to bake into a fleet panel as well — it just means the panel is
drawing the right thing a second after power-on instead of after DHCP.

### Reading a cold boot

Which files appear in the TFTP log tells you **where the panel booted from**:

| Log shows | Booted from | |
|---|---|---|
| `boot.bin` then `<mac>.yml` | network | netboot won — the link was already up |
| `<mac>.yml` only | **flash** | normal for a cold power-on |
| `boot.bin`, no `<mac>.yml` | network, then died | firmware started and fell over before asking for its config — the gateware/firmware mismatch above |
| neither | it did not get that far | |

**Expect about 6 seconds** from power to the board's first DHCP. Roughly 4 of
those are the BIOS reading the firmware out of SPI flash, which it does three
times over — it CRCs the image twice and then copies it. If a rebuilt gateware
suddenly takes half a minute instead, check `LiteSPIPHY(default_divisor=...)`:
LiteSPI's own default of 9 gives a 2 MHz SPI clock and costs 1142 CPU cycles per
byte. See `docs/HARDWARE.md` §8c.

Do not conclude anything from the first second or two. And when you do time it,
time it from the moment the plug goes in — "last ping reply to first ping reply"
silently includes however long the board was unplugged, which is how a 6-second
boot got recorded as 30 for most of a session.

---

## 8. Verify

```bash
curl http://<board-ip>/api/status        # link, DMA, interrupts, display
curl http://<board-ip>/api/display       # size, output mode, source
curl -X POST http://<board-ip>/api/display/pattern -d '{"name":"grid"}'
```

The `grid` pattern is the geometry test. At 128×128 you should see:

- White horizontals at rows **0, 32, 64, 96, 127**
- Verticals at columns 0 **red**, 32 **green**, 64 **blue**, 96 **yellow**, 127 **magenta**
- **Cyan** and **magenta** diagonals, corner to corner
- The firmware version in white

Read it as a wiring test:

| What you see | What it means |
|---|---|
| Border complete on all four sides | the whole canvas is addressed |
| The diagonals cross the seam unbroken | panels are in the right order and aligned |
| A panel dark, or showing its neighbour's content | check `J<n>` mapping and chain slots |
| Whole thing one colour, shapes visible | output mode wrong (see below) |

> **"Everything is one colour but I can see the shapes"** is a specific fault,
> not a vague one: the panel is in **indexed** output mode while the content is
> full colour, so each pixel's low byte is being read as a palette index.
> `curl http://<board-ip>/api/display` reports the real mode.

---

## 9. Streaming to it (optional)

A board in `mode: stream` displays frames sent as UDP to **port 7000**. The wire
format is in [ARCH.md](../ARCH.md); a sender implementation is in the companion
`marquee` project.

Two things to know before debugging a stream:

- **Enable the pixel DMA.** A board boots with it **off**, on the CPU pixel
  path, where the receive ISR cannot drain the MAC FIFO: roughly 3 chunks of
  every 12 are lost, so **no frame ever completes** and the panel keeps showing
  whatever was on it. That reads as a frozen or corrupt display, not a slow one.
  `POST /api/dma/on`, and check `dma_enabled` in `/api/status`. **A sender
  should re-assert this after any board reboot**, not once at startup.
- **Frame size must match the panel exactly.** Mismatched frames are refused and
  counted as `bad_size` rather than drawn as a garbled part-frame.

If you would rather the board draw its own content and receive only values, see
the [programming guide](PROGRAMMING.md) — that path needs no pixel stream at all.

---

## 9b. More than one card: chain them

A card's second RJ45 feeds the next card, so a row of cards needs one switch
port. Cable the head to the switch as above and each further card's upstream
jack into the previous card's downstream jack. Nothing is configured on the
cards: every card runs the same bitstream and firmware, and each learns its
position from marquee's discovery packet. Only the head gets an IP; the card at
hop h is `http://<head>:8000+h/` and takes pixels on `7000+h`. A card behind
another starts from its FLASH (its BIOS cannot netboot), and marquee then
netboots it into the chain's image (card side: `POST /api/netboot`, firmware
5.0.5+, [API.md](../API.md)); a chain runs one image, so push the head. The
flash copy is the survival copy, kept equal to that image by marquee. Provision
a new card once over JTAG -- golden, application bitstream and firmware -- and
everything after that arrives over the chain. Design:
[ARCH.md §14](../ARCH.md#14-card-chains--one-ip-many-cards).

## 10. When something is wrong

Every signal except one reports **intent**. `/api/display` says the mode that
was *requested*; `frames_completed` counts what the *CPU* saw. When the glass
disagrees with all of them:

```bash
curl http://<board-ip>/api/fb
```

That dumps the **front buffer** — what is actually being scanned out — sampled
every 4 pixels, with the true output mode. It is the only thing that reports
what is *happening* rather than what was asked for. It once showed a
pixel-perfect test pattern with `mode: indexed`: correct pixels, wrong mode,
which no amount of reasoning from the other counters would have found.

| Symptom | Look at |
|---|---|
| Frozen or corrupt picture, counters healthy | `dma_enabled` in `/api/status` |
| Test pattern in wrong colours | `mode` in `/api/display` |
| `frames_completed` 0 while packets arrive | `mac_overflow`, `last_missing`, `dma_enabled` |
| `bad_size` climbing | sender geometry does not match the panel |
| Web page won't load but JSON endpoints do | firmware older than v1.15.1 (an MTU bug) |
| JTAG chain scans `empty` | the other FT2232 channel (§1), then power |
