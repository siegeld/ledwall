# ledwall — a complete LED video wall system

Gateware, firmware and server for driving LED matrix panels from **Colorlight
receiver cards** — ~$15 boards that happen to carry a Lattice ECP5 FPGA, SDRAM, a
gigabit PHY and eight panel outputs each, because they are mass-produced for
commercial LED billboards.

**Two cards are supported, both first-class:**

| Card | Panel connectors | Build with |
|---|---|---|
| **Colorlight 5A-75E** | 16 x HUB75 (16-pin, one RGB group each) | `--board 5a-75e` (default) |
| **Colorlight E320 V6.2** | 8 x HUB320 (26-pin, four RGB groups each) | `--board e320` |

Same ECP5, same flash, same SDRAM, same PHYs; the panel connectors differ and the
E320 is a slower speed grade. Everything above the platform definition is shared —
one SoC, one firmware, one scene language.
[Hardware details →](firmware/docs/HARDWARE.md)

Build a wall out of as many cards as it takes, plug them into a switch, and
drive the whole thing as one picture.

## You attach JTAG once, to a new board, and then never again

**A card runs TWO separate things. Do not confuse them — they are different
artifacts, built differently, updated by different paths, and live at different
moments.**

| | **Firmware** | **Bitstream** |
|---|---|---|
| What it is | the **Rust program** on the card's RISC-V CPU | the **FPGA logic** (gateware) |
| File | `boot.bin` | `.bit` |
| Where it lives | **nowhere on the card** — refetched over TFTP on every boot | the card's **SPI flash** |
| Updated over the network | yes | yes — the firmware writes its own flash |
| Live when | immediately, on reboot | at the next **power cycle** |
| Has a version | yes, its own semver (`2.20.0`) | yes, its own semver — `e320-128x64-out6-v1.0.0`, read back out of the gateware itself |

Both update with **no JTAG**, and they are pushed separately. Ask a running card
which of each it has — the bitstream answers with a real version number, read out
of the gateware's own identifier ROM rather than from any record of what was sent:

```console
$ curl http://<card>/api/bitstream
{"bitstream": {"name":"e320-128x64-out6-v1.0.0", "version":"1.0.0",
               "board":"e320", "panel":"128x64", "outputs":6, "clock_mhz":35},
 "firmware":  {"version":"2.20.0"}}
```

A `.bit` has no version of its own — nothing in the FPGA toolchain emits one — so
the gateware build stamps one in from `gateware/VERSION`. It is **independent of
the firmware's version**, because the two change at very different rates: tying
them together would make every firmware release look like it shipped new FPGA
logic.

The name carries everything that is **compiled into** the image — board, panel
geometry and output count — because all three change what the gateware does and
two of them differing makes two bitstreams non-interchangeable. The panel sets the
shift register width and row count, so `128x64` and `256x64` are different logic,
not settings.

The bitstream is the one that mattered. A receiver card ends up bolted behind a
panel with its JTAG header facing the wall, so "update the gateware" used to mean
a ladder, two people and a laptop. Now the card fetches the image over TFTP and
streams it into its own flash a sector at a time — erase, program, verify — while
still running the gateware it is replacing. Measured on an E320: 426,374 bytes in
105 sectors, after which the board was **power-cycled for real** and came up on
the new gateware — 0% packet loss, with the golden image still untouched.

Nothing reconfigures an FPGA from software, so a pushed bitstream is live at the
next power-on rather than instantly. That is the one honest limit.

### A bad image cannot strand a board

A **golden** bitstream is written once, over JTAG, and never again. When
configuration from offset 0 fails, the ECP5 scans forward through flash, finds the
golden, and boots that instead — so the card comes back on the network and accepts
another image. Measured three ways: a corrupted primary, a corrupted primary built
with a different `--bootaddr`, and a primary whose first sector was erased. All
three booted the golden with 0% packet loss.

The firmware enforces the other half: its write-protection refuses the golden
region unconditionally, and refuses to write the application region at all when no
golden image is present — because without a recovery image that write is the one
operation that could really need a cable.

Verified again after the golden itself was replaced: corrupt the application, and
the board comes back on the golden with 0% packet loss and accepts another push.

### Provisioning is two JTAG writes, once

Golden, then application, while the board is on your bench. After that the card is
network-managed for life. [The procedure →](firmware/docs/FLASHING.md)

### Four firmware variants, not one

Firmware is not a single binary. The system clock is baked in from the gateware it
will run alongside, and S-PWM support for ICN1065 panels is a compile-time feature,
so board × S-PWM gives four:

| | clock | S-PWM | for |
|---|---|---|---|
| `5a-75e-v<ver>` | 40 MHz | no | plain HUB75 |
| `5a-75e-spwm-v<ver>` | 40 MHz | yes | P1.25 / ICN1065 |
| `e320-v<ver>` | 35 MHz | no | plain HUB75 |
| `e320-spwm-v<ver>` | 35 MHz | yes | P1.25 / ICN1065 |

One script builds and stages every one. The order is not incidental: the S-PWM
variants need the register map regenerated from an S-PWM bitstream first, and there
is one such map in the tree — so they build in passes, not in parallel. Getting
that wrong produces a firmware that compiles and cannot talk to the panel's driver
chip, which is exactly what happened here once: two supposedly different binaries
came out with the same sha256.

**P1.25 modules are 256×128 px** (320×160 mm, `ICN1065L`, 1/64 scan). On a 5A-75E
that is `--panel 256x128-icnd1065`; on an E320, `--panel 256x256-icnd1065-hub320
--outputs 8`, where a HUB320 port's four RGB groups drive two stacked modules.

| | |
|---|---|
| **[`firmware/`](firmware/)** | ECP5 gateware (Migen/LiteX) + RISC-V firmware (Rust, `no_std`): scan-out, a UDP pixel DMA, DHCP netboot, an HTTP status server, and an on-panel program runtime. |
| **[`server/`](server/)** | Fleet control and content: a declarative scene language, an ffmpeg-backed renderer, a TFTP boot service, **over-the-air (OTA) firmware updates**, a REST API and a web UI. |

They ship together because **the wire protocol is duplicated by hand across
them** — a 10-byte header, 487 pixels per RGB chunk. Two repos would let you
clone mismatched halves, and the failure mode is a sheared image, not an error.

---

## What you get

### Walls, not panels

A **wall** is one logical display with one picture on it. A **card** drives a
rectangle of that wall — you need several for anything large, because a
receiver has a fixed number of connectors, a finite framebuffer and finite
memory bandwidth.

The canvas is rendered **once per frame** and each card is sent its own crop, so
boards cannot drift apart or tear against one another. Content is assigned to
the wall; geometry, pacing and health belong to the card. Add a card, give it
its rectangle, and the picture gets bigger.

### The hardware it drives

**The boards:** a Colorlight 5A-75E or an E320 V6.2. In their day job these are
the receiver cards in an LED billboard — which is exactly why they are cheap, fast
and covered in output connectors.
[What we found inside them →](firmware/docs/HARDWARE.md)

Pick the card with `--board 5a-75e` (default) or `--board e320`. The E320 needs
one more thing: build it at **35 MHz**, not 40, because it is a `-6` part and the
design does not close 40 MHz on it.

> **A note on timing reports.** The E320 misses the toolchain's 125 MHz constraint
> on the RGMII receive clock, and that constraint is not the requirement the
> hardware has to meet: the clock is source-synchronous from the PHY and capture
> depends on the board's tuned 2 ns RX delay, which static timing analysis cannot
> model. Its pass/fail is also placement noise — the same 5A-75E design measured
> 139.92, 114.56 and 125.50 MHz across three builds with no functional change. So
> the build gates on the SYSTEM clock, which is real, and treats the Ethernet
> domain as advisory, backed by measurement: 9,849,178 frames at gigabit line rate
> with zero CRC and zero preamble errors. There is a script to re-measure it, and
> the rule is to re-run it after any change to that path. The build refuses a firmware whose clock
constant disagrees with the gateware's, because a mismatch gives a card that boots
and then fails at everything time-dependent — which presents as a network fault
and sends you looking in the wrong place.

Three 5A-75E hardware revisions are supported — `6.0`, `7.1` and `8.2` — selected
with `--revision`. They are not cosmetic variants: the pinouts differ and so does
the ECP5 speed grade (6.0 and 7.1 are `-6` parts, 8.2 is a `-7I`), so building for
the wrong one produces a bitstream that does nothing on the board.

### The Colorlight E320, and how its pinout was recovered

The **E320 V6.2** is a supported card, not a curiosity: `--board e320` builds for
it, and it also runs the stock 5A-75E bitstream unmodified if you ask it to. Same
ECP5 on the same package with the same flash and SDRAM, plus a JTAG header fitted
from the factory rather than soldered on by you. Verified end to end: DHCP, TFTP
netboot, HTTP, the gateware pixel DMA, 840 frames at 200 fps with nothing dropped,
and an over-the-air bitstream update followed by a power cycle onto the new
gateware.

Its **own** `--board e320` gateware is confirmed too: it configures from flash,
brings up Ethernet, netboots, serves its status API, and passes the firmware's
flash self-test over the network. Build it at **35 MHz, not 40** — the E320 is a
`-6` part and the design does not close 40 MHz on it. The build refuses a
firmware whose clock constant disagrees with the gateware's, because a 40 MHz
firmware on 35 MHz gateware boots and then fails at everything time-dependent,
which presents as a network fault and sends you looking in the wrong place.

Its connectors are the interesting part. The E320 carries **eight 26-pin
HUB320 ports of twelve data lines**, where the 5A-75E has sixteen 16-pin HUB75
ports of six — and that pinout is published nowhere. Two techniques recovered
it, and both generalise to any unknown card:

**1. Read the factory bitstream.** Dump the SPI flash, unpack it with
prjtrellis, and list the PIO sites with **`IOLOGIC`** configured — a genuinely
registered signal has it, an untouched pad does not. 36 of the E320's 45 bonded
pins landed exactly on 5A-75E signals, which predicted compatibility before the
card was ever powered up for a test. (Do **not** read `BASE_TYPE` instead:
Diamond assigns one to unused pins too, so half of every bank looks
"configured". That cost a day and produced two confident wrong answers.) The
method only sees *registered* IO, so it finds SDRAM, flash and Ethernet but
never the combinational panel data lines.

**2. Measure the rest with a multimeter.** `./build.sh pinwalk` drives every
safe FPGA ball simultaneously, each at its own frequency — 1000 Hz upward in
50 Hz steps across 108 balls. Touch a connector pin with a meter in Hz mode and
the reading names the ball. No CPU, no firmware, no network, no host in the
loop. [How →](firmware/docs/PINWALK.md)

About twenty probes mapped all eight ports, because the structure was guessed
first and then tested: **a HUB320 port is two 5A-75E connectors merged**, so
port *n* is `j(2n-1)` + `j(2n)`. Eight ports × twelve data lines is 96, exactly
the number of data balls `j1`..`j16` provide, with nothing left over. The result
is [`gateware/colorlight_e320.py`](firmware/gateware/colorlight_e320.py).

> Honest status: the map is measured and self-consistent, and the four-group
> HUB320 output stage is wired to it — but **no panel has yet been attached to
> an E320**, so none of it is confirmed by light on glass.

Nothing above the platform definition is tied to a particular card. The SoC is
generated by LiteX, the Rust firmware is built against the SVD that generation
emits, and the panel configuration is a preset — so a sibling receiver card is a
`litex_boards` platform swap and a pin map rather than a rewrite. The
[chubby75](https://github.com/q3k/chubby75) project has reverse-engineered
several of them. **The 5A-75E and the E320 V6.2 have both actually been run
here**; treat any other card as a port that should work rather than one that is
known to. The one part of the E320 still unproven is light on glass — no panel has
yet been attached to one — so its output stage is measured and self-consistent
rather than confirmed by a lit module.

**The panels:** two different output stages ship, because LED modules are not
one family.

| stage | for | driver ICs |
|---|---|---|
| `hub75` *(default)* | classic shift-register panels — the module shifts raw bits and the controller does the greyscale, as bit planes / BCM | MBI5124, ICN2038S and similar |
| `icn1065` | **S-PWM** panels, whose driver IC holds the greyscale itself and generates the PWM | Chipone **ICND1065**, NovaStar **DP3364S** |

Those are genuinely different conversations on the wire, not a faster version of
the same one: an S-PWM chip takes a 12-bit word per LED, there are no bit
planes, and OE steps a row counter *inside the chip* rather than the controller
addressing rows over A–E. The chip table is rewritable by the CPU at runtime, so
a preset only sets the power-on default.

Panel geometry is a preset: `128x64`, `256x64`, `256x128` (P1.25 320×160 mm
modules), with variants that trade shift clock, colour depth (32-bit or RGB565)
and gamma against refresh rate and FPGA timing. Per card you also set channel
order, scan rate, chain length, rotation and position.

> **Honest status:** the `hub75` stage drives panels daily. The S-PWM stage is
> synthesised, simulated and documented, but **no ICND1065 panel has been
> connected to a board yet** — the source says so at the preset, and
> [ICN1065.md](firmware/docs/ICN1065.md) says so at length.

### One card drives a lot of pixels

The card's CPU is not in the pixel path at all. A gateware **UDP pixel DMA**
parses the wire protocol and writes SDRAM directly, so the frame rate is bound
by memory bandwidth rather than by interrupts — before that, the CPU unpacking
pixels itself capped out at ~600,000 px/s no matter how hard you pushed it.

| wall | modules | cards | pixels | full RGB | indexed |
|---|---|---|---|---|---|
| **128 × 128** | 2 × (128×64) | 1 | 16,384 | 147 fps *est.* † | 417 fps *est.* † |
| **384 × 192** | 9 × (128×64) | 1 | 73,728 | **33 fps measured** | 98 fps *est.* |
| **768 × 384** | 9 × (256×128) P1.25 | 1 | 294,912 | 8 fps *est.* ‡ | **25 fps** *est.* ‡ |

Cold boot, power to DHCP: **6.2 s**.

**The bound is packets, not pixels or bandwidth** — that is the single most
useful fact here. A frame is `ceil(pixels / 487)` chunks in full RGB and
`ceil(pixels / 1461)` indexed, and the card sustains ~5,000 packets/s with zero
drops. Every estimate above is that arithmetic; it reproduces the one measured
row to within 0.3%, and the measured **2.84×** indexed speed-up matches the
chunk-count ratio almost exactly. Indexed carries three times the pixels in the
same MTU, so it costs three times fewer interrupts.

† Not a rate worth sending — at this size the packet ceiling is far above
anything useful and the panel's own refresh binds first. Shown for the
arithmetic. (Published measurements of **36 / 103 fps** at 128×128 predate the
hardware DMA and sit exactly on the old ~1,230 packets/s CPU-path ceiling.)
‡ This wall is synthesised and simulated — place-and-route plus a bandwidth
model, never run on glass. Its scan-out is also SDRAM-bound at roughly **30 Hz**,
so full RGB at 8 fps is packet-starved while indexed at 25 fps very nearly fills
what the panel can take. That is precisely why indexed exists. Section 4c of
[BENCHMARKS.md](firmware/docs/BENCHMARKS.md) shows the arithmetic and is explicit
about which figures are measurements and which are models.

Eight outputs per card, and the shift-register stage is driven from a merged
row-buffer design that fits both ping-pong banks in one EBR — six outputs cost
six block RAMs instead of twelve, at full 32-bit colour depth with no RGB565
banding. A larger 768×384 stage for P1.25 modules is synthesised and simulated
but **has not yet run on glass**, and the docs say so wherever it appears.

### Zero-touch provisioning

Cards configure and boot themselves over the network. **One bitstream fits every
card in the fleet.**

- The BIOS runs its own **DHCP** client, with a MAC derived from the card's
  flash unique ID — so every board asks from its own address and is individually
  addressable.
- **DHCP option 66** tells it where the boot server is, so the server's address
  is *not* compiled into the bitstream. Move the server and nothing needs
  reflashing.
- Firmware **and** panel layout arrive over TFTP, which makes a firmware
  rollout a fleet operation with a record, targetable per card.

### Over-the-air updates: firmware AND bitstream (OTA)

Updating the **Rust firmware** is OTA: pick an image in the web UI, press a
button, and the board reboots onto it. A card refetches its firmware on **every**
boot and keeps no copy of it, so an update is a server-side change plus a reboot
— no cable, no hands on the hardware.

The **bitstream** is OTA too, by a different route: the firmware fetches it over
TFTP and writes it into the card's own SPI flash a sector at a time — erase,
program, verify — while still running the gateware it is replacing. It is live at
the next power cycle, because nothing reconfigures an FPGA from software.

Either way the controller stages images, records which card was given which, and
can roll one across a wall one card at a time, stopping at the first board that
does not come back. It reports what each card is **actually running** for both
artifacts, read from the card rather than from its own records, because those two
differ exactly when it matters: an image nothing has rebooted into, a card that
fell back to a default, a bitstream written but not yet power-cycled.

Three design points worth stealing:

- **Assigned is not running.** The fleet view shows both, because they differ
  exactly when it matters: an assignment nothing has rebooted into, or a card
  that fell back to the default image. A view showing only the assignment calls
  both of those healthy.
- **Verify a reboot by watching the card go *down* first.** Polling for "does it
  answer" reports success instantly — the old firmware keeps serving HTTP in the
  moment between accepting the reboot and actually restarting, so "it came back"
  can mean "it never left".
- **A bad image cannot brick a board**, because nothing is written to it:
  recovery is to push a different image. A bad *bitstream* is a different matter
  and is recoverable only over JTAG, which is why the two are handled by
  different paths.

The firmware can also erase, program and verify **its own SPI flash** over HTTP,
which is what makes an A/B bitstream update possible without JTAG at all. The
trap that path exists to catch: a write-protected flash chip refuses an erase
*outright* rather than starting it, so it never reports busy — every poll returns
immediately and every layer above reports success while nothing was written.

### Every card is its own diagnostic tool

Each board runs an **HTTP server** with ~30 endpoints, so a panel is debuggable
from a browser with no toolchain, no JTAG and nothing installed:

| | |
|---|---|
| `/api/status` | counters: frames, drops, DMA stalls, refresh rate, ISR load, link state |
| `/api/profile` | **per-row cost of the running program**, in nanoseconds |
| `/api/bench` | whole-frame timing: fill, program, palette |
| `/api/fbdump` | the framebuffer itself, at full resolution |
| `/api/palette` | both palette banks and which is live |
| `/api/display`, `/api/program/on`, `/api/values` | control the panel directly |
| `/api/rgborder`, `/api/layout`, `/api/reboot` | per-card configuration |

When a wall looks wrong, you can ask the panel what it thinks it is showing
rather than inferring it.

### Content that is worth putting on a wall

A declarative **scene language** (YAML) composes layers into frames host-side:

| layers | sources | live data |
|---|---|---|
| `solid`, `gradient`, `image`, `video`, `text`, `graph`, `sparkline` | files, **Plex** libraries, **YouTube** (via yt-dlp), **RTSP cameras**, Wowza, any ffmpeg URL | `{clock:}`, `{date:}`, `{http:}`, `{ha:}` (Home Assistant), `{file:}`, `{env:}` |

Plus `random:`/`shuffle:` to play a whole library forever, scrolling text,
crossfades, per-layer `opacity`/`z`, `fit` modes, **fractional boxes so one show
fits any wall**, and dayparting so a wall shows different content by time of day.

The web UI has a **live server-rendered preview with a time scrubber** — you see
the frame a panel will receive, at any moment of the scene, while you type.

### Panels are not standardised, so the card adapts

Per-card **RGB channel order** (six permutations — modules with identical
connectors genuinely disagree), panel geometry, scan rate, chain length,
rotation and position, gamma correction in gateware, and per-card packet pacing
because a board on the CPU pixel path and one on the hardware DMA have very
different ceilings.

### Two wire formats

`rgb` (RGB888) or `indexed` (one byte per pixel plus a palette) — the latter is
~3× fewer **packets**, and packets are what the panel is limited by, so it is
~3× the frame rate on content that survives 256 colours. Chosen per scene.

---

## Two ways a panel gets its pixels

Both modes are first-class, and the difference matters more than it sounds.

**Streamed.** The server renders and sends frames over UDP. Anything ffmpeg can
decode is fair game. A 128×128 panel sustains 30 fps RGB888 with zero drops, or
~100 fps indexed.

**On-panel programs.** The panel composes its own pixels from a handful of
pushed *values* (`temp = 12`) — a few hundred bytes a second instead of
24 Mbit/s. Programs are written as YAML, compiled to Rust by
[`panelc`](firmware/tools/panelc.py), and linked into the firmware.

The second mode exists because of what happens when the server dies. **A
streamed panel freezes on a frame that still looks perfectly correct** — a sign
that lies. A program keeps running and can *say* its data went stale.

```yaml
program: hello
display: {width: 128, height: 128}
values: [hhmm, temp]

widgets:
  - text:  {at: [center, 8],  font: small, color: slate, value: "HELLO"}
  - value: {at: [center, 30], font: big,   color: white, key: hhmm}
```

See [the worked example](firmware/custom/example/panels/hello.yaml) and
[ON-PANEL-PROGRAMS.md](firmware/docs/ON-PANEL-PROGRAMS.md).

---

## Performance is the whole project

Two completely different ceilings, for two different reasons: **streaming is
limited by interrupts; drawing is limited by memory writes.** An optimisation
that helps one does nothing for the other, and
[BENCHMARKS.md](firmware/docs/BENCHMARKS.md) keeps them apart throughout.

Some of what that bought, all measured rather than reasoned about:

| change | effect |
|---|---|
| UDP pixel DMA in gateware — CPU out of the pixel path | 600k px/s ceiling → bound by SDRAM instead |
| Hardware buffer swap at the frame boundary | killed a band of the next frame across the top of video |
| `burst_fifo` 512 → 2048 words | ~0.86 dropped packets/s → **0.00** |
| Indexed wire format | ~3× fewer packets → ~3× the frame rate |
| `target-feature=+m` (the M extension was there all along) | **6×** on a program |
| Hoisting per-row work out of the pixel loop | on-panel dashboard **8.30 s → 0.34 s per frame** |
| Progressive repaint | removed a visible hitch on every value arrival |

### You can measure it yourself, on the card

The single most useful thing here is that **the panel will tell you where its
time goes**. `/api/profile` reports the cost of every row of the running program
in nanoseconds; `/api/bench` times a whole frame — a bare fill, the program, a
palette rewrite — so "this program is slow" becomes "rows 8–42 are 85% of the
frame".

That tooling exists because guessing failed repeatedly. Several comments in this
codebase record a confident diagnosis that measurement then demolished, and the
one that stings most: `/api/bench` read `mcycle`, which on this core always
returns 0 — **every cycle count it had ever printed was that zero**, and the
"facts" derived from it were fiction until someone checked.

### The rules that follow

The panel CPU is a 40 MHz VexRiscv with **no D-cache**, so a memory read costs
about what a write does — and anything per-pixel happens 16384 times a frame.
(It *does* have the M extension, so multiply and divide are instructions rather
than software routines; a divide is still iterative where a shift is not.)
Three rules run through the codebase, and most of the comments exist to defend
them: **O(1) per pixel**, **reject before you compute**, **hoist to the row**.

Two consequences worth knowing before reading any of it:

**Colour lives in the palette, not the pixels.** In indexed mode a glyph's 4-bit
coverage *is* the offset into a 16-entry ramp, so anti-aliased text costs one
add — and recolouring the entire sign costs 256 words a frame, **flat in wall
size**. A nine-panel wall animates for the price of one.

**Scrolling shifts, it does not redraw.** A scroll band moves what is already on
the glass and asks the program only for the column that uncovered.

Measured on one 128×128 panel:

| | fps |
|---|---|
| bare fill (one store per pixel) | 247 |
| two scrolling tapes, clock, animated palette, chasing bulbs | ~32 |
| full canvas: plasma, spinning sprite, sparkle, two live tapes | ~16 |

The gateware also double-buffers **both** the framebuffer and the palette and
swaps them together at the frame boundary, so neither tears — and the CPU is out
of the pixel path entirely, since the UDP pixel DMA writes SDRAM directly.

---

## Getting started

```bash
cd firmware
./build.sh docker          # the toolchain, containerised
./build.sh build-all       # gateware + firmware
./build.sh flash           # over JTAG, once per card
```

Then the server:

```bash
cd server
cp .env.example .env       # edit it
make up-build
```

From there the card boots itself over the network and the server owns it.

**[Full walkthrough →](firmware/docs/GETTING-STARTED.md)** — parts on a desk to
a lit panel running your own program and playing a video, in about an hour, with
no FPGA experience.

And if you like reading about things going wrong,
**[NOTES.md](firmware/NOTES.md)** is the debugging notebook: two sessions spent
chasing a failure that did not exist, a 20-second phantom created by measuring
from the wrong instant, and the discovery that `mcycle` returns 0 on this core
so every benchmark it fed had been reporting zeros for months.
[Hardware and wiring →](firmware/docs/HARDWARE.md) ·
[Scene language →](server/doc/scenes.md) ·
[Architecture →](firmware/ARCH.md)

## Making it yours

Both halves have a `custom/` directory that is an explicit extension point: your
programs, panel layouts and scene documents live there, and the stock repo ships
only a README and one worked example. See
[firmware/custom/README.md](firmware/custom/README.md).

## Status

Driving a wall continuously. The gateware, wire protocol, scene language and
program runtime are all in use rather than aspirational — but this is one
person's project driving one family of hardware, so expect sharp edges away from
the path described above.

## Contributing

Pull requests are welcome, with one wrinkle you should know before spending
time: this repo is **generated** from two private repos, so a PR is merged by
porting it rather than by pressing the button. Your authorship is preserved.
The workflow is in [CONTRIBUTING.md](CONTRIBUTING.md) — please read it first.

## Credits

This project exists because other people did the hard parts first.

**The board would be a brick without the reverse engineering.** A Colorlight
5A-75E ships as a closed appliance with no public documentation — no pinout, no
schematic, nothing. Every connector, every pin and the flash layout were worked
out and published by others:

- **[chubby75](https://github.com/q3k/chubby75)** — Sergiusz Bazanski (q3k) and
  contributors. The pin maps this gateware drives come from there.
- **[DerFetzer/colorlight-litex](https://github.com/DerFetzer/colorlight-litex)**
  — the original LiteX implementation on this board, and the starting point for
  this one.
- **[litex-boards](https://github.com/litex-hub/litex-boards)** — the
  `colorlight_5a_75e` platform definition, including the three hardware
  revisions.

**The SoC is assembled, not written.** Everything below the application is
somebody else's work:

- **[LiteX](https://github.com/enjoy-digital/litex)** and **Migen**, with
  **LiteEth**, **LiteDRAM** and **LiteSPI** — the SoC, the Ethernet MAC, the
  SDRAM controller and the flash controller.
- **[VexRiscv](https://github.com/SpinalHDL/VexRiscv)** — the CPU.
- **[Yosys](https://github.com/YosysHQ/yosys)**,
  **[nextpnr](https://github.com/YosysHQ/nextpnr)** and
  **[Project Trellis](https://github.com/YosysHQ/prjtrellis)** — a complete
  open-source ECP5 toolchain, which is the reason a $15 board can be
  reprogrammed at all.
- **[openFPGALoader](https://github.com/trabucayre/openFPGALoader)** — flashing.
- **[smoltcp](https://github.com/smoltcp-rs/smoltcp)** — the TCP/IP stack the
  firmware's slow path runs on.

**On the server side:** **[ffmpeg](https://ffmpeg.org/)** does every frame of
decoding — the scene language is a thin declarative layer over it — with
**[yt-dlp](https://github.com/yt-dlp/yt-dlp)** resolving `youtube:` sources and
**[Pillow](https://python-pillow.org/)** doing the compositing and the quantiser
behind `format: indexed`. The API is
**[FastAPI](https://fastapi.tiangolo.com/)**,
**[SQLAlchemy](https://www.sqlalchemy.org/)** and Pydantic; the console is
React, Vite and Tailwind; cards are booted over TFTP by
**[tftpy](https://github.com/msoulier/tftpy)**. Text on the panels is set in the
**[DejaVu fonts](https://dejavu-fonts.github.io/)**.

Full component lists with licences:
[firmware/THIRD-PARTY.md](firmware/THIRD-PARTY.md) ·
[server/THIRD-PARTY.md](server/THIRD-PARTY.md).

## Licence

BSD 2-Clause — see [LICENSE](LICENSE).
