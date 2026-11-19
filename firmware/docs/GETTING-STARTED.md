# Getting started

> **Flashing procedure lives in `FLASHING.md`, not here.** The commands below
> are a walkthrough for a first-time setup. When something will not program,
> or you need the authoritative procedure, go to **`docs/FLASHING.md`** --
> especially §2 (a corrupt image in flash blocks JTAG configuration) and §3
> (`--dump-flash` output is not evidence). Do not diagnose from this file.


> **Which boards and modules this works with:** the *Supported hardware* table
> in [`README.md`](../README.md) and the per-module registry in
> [`PANELS.md`](PANELS.md). This walkthrough is written for a 5A-75E and a
> HUB75 module; an E320 with an S-PWM module follows the same steps with the
> `--board e320 --panel ...` build flags from `PANELS.md`.

**You have a Colorlight board, some LED panels, and an afternoon.** By the end
of this page the panels will be lit, running a program you wrote, playing a
video, and managed by a server — and, if you have more than one card, the cards
will be daisy-chained into one wall on one IP address.

No FPGA experience needed. You will not write any Verilog. Everything builds in
Docker, so nothing gets installed on your machine.

Five parts, in order. The first three need nothing but this repo; stop after
any of them and you have something that works.

1. **[Light it up](#part-1--light-it-up)** — build, flash, see a test pattern *(~30 min, mostly waiting)*
2. **[Write a program](#part-2--write-a-program)** — YAML in, pixels out *(~15 min)*
3. **[Play a video](#part-3--play-a-video)** — stream from your laptop *(~5 min)*
4. **[Run the server](#part-4--run-the-server)** — marquee boots, configures and feeds every card *(~30 min)*
5. **[Chain more cards](#part-5--chain-more-cards)** — card to card through the second RJ45, one IP for the lot *(~10 min a card)*

Then: **[the FAQ](#faq--read-this-before-asking)**. If something is weird, the
answer is probably there.

---

## Before you start

You need:

- A **Colorlight 5A-75E, rev 8.2** receiver card (~$15 on AliExpress)
  - A **Colorlight E320 V6.2** works too, and has a JTAG header fitted from the
    factory. Its connectors are HUB320 (26-pin, four RGB groups each) rather
    than HUB75; build with `--board e320` and the panel flags in
    [PANELS.md](PANELS.md). The HUB320 pinout is in [HUB320.md](HUB320.md).
  - **Not** a K-series part (`K5+`, `K8`, `K9+`, `K10`). Those are mezzanine
    modules for a cabinet's HUB board: no panel connectors, no RJ45, and in the
    `K9+`/`K10` case not even an ECP5. See [HARDWARE.md](HARDWARE.md) section 1c.
- One or more **HUB75 LED panels** and the ribbon cables they came with
- A **5V power supply** — see the warning below
- A **JTAG programmer**: an FTDI FT2232 board (~$10) or a USB Blaster
- **Docker**, and an ethernet cable
- For Part 4: a Linux machine to run the server (the same one is fine), with
  Docker Compose
- For Part 5: a second card and a short ethernet cable per extra card

> ### ⚡ The power supply is the thing people get wrong
>
> A 128×64 panel at full white pulls about **4 amps at 5 volts**. Two panels and
> a controller need a 10 A supply, not the 2 A brick from a drawer.
>
> An undersized supply does not fail like a power problem. It fails like a
> *software* problem — scrambled pixels, a board that drops off the network, a
> flicker you will spend a day chasing. If anything in this guide behaves
> strangely and your supply is small, fix that first.

### Wire up JTAG

Six wires, on a 4-pin header next to the big chip (U33), plus a 2-pin power
header beside it:

| Board pin | Signal |
|---|---|
| J27 | TCK |
| J31 | TMS |
| J32 | TDI |
| J30 | TDO |
| J33 | 3.3V |
| J34 | GND |

Plug the panels into J1, J2, … and power everything on.

---

## Part 1 — Light it up

### Step 1: Build the toolchain

```bash
git clone <this repo>
cd colorlight
./build.sh docker
```

This builds a Docker image containing the entire FPGA toolchain. **It takes a
while** — start it and go get a coffee. You only ever do this once.

### Step 2: Check the programmer can see the board

```bash
openFPGALoader --cable ft2232 --detect
```

You want a line with `idcode 0x41111043` and `LFE5U-25`. That is your FPGA
saying hello.

**Got `empty` instead?** Almost certainly not a broken board:

- **An FT2232 has two channels and they are different devices.** Try
  `--cable ft2232_b`. This is the single most common false alarm — a wrong
  channel looks *identical* to an unpowered board.
- Using a USB Blaster? `--cable usb-blaster`.
- Still nothing: check the 3.3V and GND wires, then the power supply.

Whichever `--cable` worked, use it for every command below.

### Step 3: Build the gateware and the firmware

Tell it your panel size and the board's IP address. Pick an IP on your LAN that
your DHCP server will not hand out to anything else:

```bash
./build.sh --panel 128x64 --ip 192.168.1.50 bitstream
./build.sh firmware
```

`--panel` is the size of **one** panel. If you have two 128×64 panels, that is
still `--panel 128x64` — you tell it how they are *arranged* in Step 5.

### Step 4: Load it and see something

```bash
./build.sh --panel 128x64 --cable ft2232_b sram
```

This pushes the design into the FPGA over JTAG, starts a TFTP server, and the
board fetches its firmware from it. **The panels should light up within about
ten seconds.**

Check it is alive:

```bash
curl http://192.168.1.50/api/status
```

Then open `http://192.168.1.50/` in a browser — there is a real web UI on the
board with live stats and controls.

> `sram` is **temporary**. Power-cycle the board and it forgets everything. That
> is exactly what you want while experimenting — it is a ten-second edit-and-see
> loop. Step 7 makes it permanent.

### Step 5: Tell it how your panels are arranged

The board asks for a config file named after its own MAC address. Find the MAC
in `/api/status`, then create the file in the `.tftp/` directory with dashes
instead of colons — MAC `aa:bb:cc:dd:ee:ff` becomes `.tftp/aa-bb-cc-dd-ee-ff.yml`:

```yaml
grid: 1x2              # 1 panel across, 2 panels down
panel_width: 128       # the size of ONE panel
panel_height: 64
J1: 0,0 0,0            # connector J1 -> top cell
J2: 0,1 0,1            # connector J2 -> bottom cell
mode: stream
```

That makes two stacked 128×64 panels into one 128×128 canvas. Reload with
`./build.sh --panel 128x64 --cable ft2232_b sram` and it will pick up the file.

The `J<n>: col,row col,row` lines say which grid cell each connector drives. The
position is repeated once per panel in that connector's chain.

### Step 6: Prove the wiring is right

```bash
curl -X POST http://192.168.1.50/api/display/pattern -d '{"name":"grid"}'
```

You should see a white border all the way round, coloured lines through the
middle, two diagonals corner to corner, and the version number.

Read it like this:

| What you see | What it means |
|---|---|
| Border on all four sides, diagonals unbroken | 🎉 everything is right |
| A panel dark, or duplicating its neighbour | wrong `J<n>` line in the config |
| Diagonals kink at the seam | panels in the wrong order — swap the grid cells |
| All one colour, but shapes visible | see [the FAQ](#the-whole-panel-is-one-colour-but-i-can-see-the-shapes) |

### Step 7: Write it to flash

Right now everything lives in volatile memory. Write it to the board's flash:

```bash
./build.sh --panel 128x64 --cable ft2232_b flash-all
```

**Do this before you walk away from a build**, or the next reload brings back
whatever was on the board before you started.

Pull the power, put it back, and the sign returns on its own in about **6
seconds** — no build host, no server, nothing on the network.

> **This page claimed the opposite until 2026-09-17**, in a box headed "cold
> power-on does NOT boot from flash", blaming flash power-up timing or panel
> inrush sagging the rail.
>
> The board was fine the whole time. The firmware was being written to flash
> **without the 8-byte FBI header** the BIOS requires, so it read the first
> instruction of the firmware as a 1 GB image length and threw the image away.
> Every indicator anyone looked at came *after* that point, so a healthy board
> looked exactly like a dead one.


**You now have a working LED sign.** 🎉

> ### What runs the sign from here on
>
> Parts 2 and 3 keep using the build host's TFTP server and scripts — fine for
> one card on a desk. For anything permanent, [Part 4](#part-4--run-the-server)
> hands the card to **marquee**, the server half: it boots the card, serves its
> config, and sends it pixels or values. You then come back to this repo only to
> build new firmware, write a new on-panel program, or change panel geometry.

---

## Part 2 — Write a program

The board has a CPU on it, and it can run your code. That means the sign can
draw its own content and keep doing it with nothing else plugged in — like
ESPHome, but for LED panels. No laptop, no server, no video stream.

You write these in YAML. A file in `panels/` gets compiled to Rust, built into
the firmware, and runs natively on the board.

### Your first program

Create `panels/hello.yaml`:

```yaml
program: hello
display: {width: 128, height: 128}

assets:
  fonts:
    - {name: big, file: assets/fonts/DejaVuSans.ttf, size: 20}

widgets:
  - border: {thickness: 2, color: brass}
  - text: {text: "HELLO", x: 20, y: 60, font: big, color: white}
```

Compile it, build it, load it:

```bash
python3 tools/panelc.py panels/hello.yaml
./build.sh firmware
./build.sh --panel 128x64 --cable ft2232_b sram
```

Then tell the board to run it — add these two lines to your `.tftp/<mac>.yml`:

```yaml
mode: program
program: hello
```

Reload, and the board boots straight into your program.

### How it actually works

**A program answers one question: "what colour is this pixel?"** The board asks
it for every pixel, every frame. You never draw *into* anything — you are asked,
and you answer.

Widgets are checked **in order, and the first one that covers a pixel wins**. So
the first widget in the list is **on top**.

> This is the opposite of CSS and HTML, where later means on top. If your
> background is hiding your text, the background is listed first. Move it last.

### Dropping into real code

Any widget can be a `lambda:` — raw Rust, spliced in at that position in the
stacking order, with the whole drawing library available:

```yaml
widgets:
  - text: {text: "HELLO", x: 20, y: 60, font: big, color: white}
  - lambda: |
      // a rainbow that moves
      Some(RAINBOW + (hue(x + y + ctx.frame as usize) % 128) as u8)
```

Return `Some(colour)` to paint the pixel, or `None` to fall through to whatever
is underneath.

The library gives you borders, text, scrolling tickers, bar gauges, sprites,
rotation, plasma, sparkles and sweeps — all as one-liners. See the
**[programmer's guide](PROGRAMMING.md)** for the full list and the performance
rules that matter at 128×128.

### Live data

A program can display values pushed to it over the network (temperature, a
stock price, whatever), while still running entirely on the board:

```yaml
values: [temp]
widgets:
  - text: {value: temp, x: 10, y: 40, font: big, color: white}
```

Push one with `curl`:

```bash
curl -X POST http://192.168.1.50/api/values -d '{"temp":"72°"}'
```

The difference from streaming: you send **a number**, not pixels. The sign keeps
drawing on its own if whatever was sending values goes away — and it can *tell
you* it went away, with `stale_marker`.

### Making it truly standalone

Flashing the firmware puts the program on the board, but *which* program to run
still comes from the config it fetches at boot. Bake that in too and the panel
needs no network at all:

```bash
./build.sh --default-config custom/example/configs/panel.yml firmware
./build.sh --panel 128x64 --cable ft2232_b flash-all
```

That config is the same file format as `.tftp/<mac>.yml` — copy yours, add
`mode: program` and `program: <name>`, and you have a sign that comes up doing
its job with nothing plugged into it but power.

A config fetched over the network still overrides the baked one, so this does
not lock the panel down — it just gives it something to fall back on.

> Remember there is no data when there is no network. A standalone program should
> either use no values, or look right without them.

### Programs to steal from

| File | What it shows |
|---|---|
| `panels/blank.yaml` | the minimum possible program |
| `panels/ticker.yaml` | two scrolling tapes, and how to make a program cost almost nothing — 29 fps |
| `panels/timessquare.yaml` | everything at once: plasma, a spinning sprite, sparkle, a chasing border and two live tapes, full canvas every frame — 16 fps |
| `custom/example/panels/hello.yaml` | the worked example the README shows: text plus live values |

The two in `panels/` are written as arguments as much as demos: the panel is a
40 MHz VexRiscv with **no D-cache**, so a read costs about what a write does and
anything per-pixel happens 16384 times a frame. (It *does* have the M extension
— see [BENCHMARKS.md](BENCHMARKS.md) — so multiply and divide are instructions,
not software routines.) Both files say where their cycles go and why.

---

## Part 3 — Play a video

Put the board back in streaming mode — set `mode: stream` in your
`.tftp/<mac>.yml` and reload — then:

```bash
# turn on the pixel DMA first (see below!)
curl -X POST http://192.168.1.50/api/dma/on

python3 tools/send_video.py myvideo.mp4 \
    --host 192.168.1.50 --width 128 --height 128 --fps 15 --loop
```

That is it. It uses `ffmpeg` to decode, scales to your panel, and streams frames
over UDP.

Also available:

```bash
python3 tools/send_youtube.py 'https://youtube.com/watch?v=...' --host 192.168.1.50 --width 128 --height 128
python3 tools/send_image.py picture.png --host 192.168.1.50 --width 128 --height 128
python3 tools/send_animation.py --host 192.168.1.50 --width 128 --height 128
```

> ### ⚠️ Turn on the DMA, or nothing happens
>
> The board boots with its pixel DMA **off**. In that state the CPU handles
> every incoming packet and cannot keep up — it drops roughly a quarter of them,
> so **no frame ever completes** and the display just sits there showing the last
> thing on it.
>
> This does not look like dropped packets. It looks like a frozen sign.
>
> `curl -X POST http://192.168.1.50/api/dma/on`, and check `dma_enabled` in
> `/api/status`. **Do it again after every reboot** — it does not persist.

Two more things worth knowing:

- **The frame size must match the panel exactly.** Wrong-sized frames are
  rejected outright rather than drawn as a garbled part-frame. If `bad_size` is
  climbing in `/api/bitmap/stats`, your `--width`/`--height` are wrong.
- **Getting ~35 fps and want more?** Use indexed mode. One byte per pixel instead
  of three, ~3× fewer packets, ~3× the frame rate — and the panel's limit is
  packets, not pixels. Perfect for text and graphics; it bands on photographs.

## Part 4 — Run the server

Everything so far was one card on a desk, fed by the build host. **marquee**
(the `server/` half of the public repo) is what runs a sign for real: it boots
every card over the network, hands each its layout, renders shows and streams
each card its own part of the picture, pushes live values to on-panel programs,
and updates firmware and bitstreams with no JTAG. It is three containers and a
web UI.

### Step 1: Start it

On the machine that will drive the panels:

```bash
cd server
cp .env.example .env
```

Edit `.env`. At minimum set `MARQUEE_ADMIN_PASSWORD`, `MARQUEE_SESSION_SECRET`
(any long random string) and `MARQUEE_SITE_TZ`. Then:

```bash
make up-build
make health          # {"status":"ok","version":"..."}
```

The web UI is on port **8411** and the API on **8410**. The player runs on the
host network, because it has to send UDP straight onto the panel segment and
answer TFTP on port **6969** — not 69.

### Step 2: Point the cards' boot at it

A card's BIOS gets an address by DHCP, then fetches `boot.bin` by TFTP from the
server named in **DHCP option 66**. So the one network change is: **option 66 =
the marquee host**, on whatever DHCP server the cards use. (The address
compiled into the bitstream, `TFTP_SERVER` in `build.sh`, is only the fallback
when option 66 is missing.)

Two ways to do it:

- **Your existing DHCP server.** Add option 66 (often labelled "TFTP server" or
  "next-server") pointing at the marquee host, ideally with a reservation per
  card so each keeps its address.
- **A dedicated panel link (recommended for video).** A second NIC on the
  marquee host, a small switch carrying nothing but cards, and marquee's own
  `dnsmasq` serving that segment. Busy shared networks deliver a stream in
  bursts the card cannot absorb; a dedicated link removes them.
  [server/doc/dedicated-link.md](../../server/doc/dedicated-link.md) is the
  step-by-step.

### Step 3: Give it the firmware

On the **Firmware** page, upload the `boot.bin` you built in Part 1. An image
named `boot.bin` is the fleet default — what any card gets unless its record
names another. (Several firmware variants exist per board; see the
[README](../README.md) for which one your card needs.)

### Step 4: Make a wall and add the card

1. **Walls** page: create a wall the size of your whole picture — 128×128 for
   the two stacked panels of Part 1.
2. **Cards** page: add the card. Give it a name, its IP address, its **MAC**
   (from `curl http://<card>/api/status`), the wall it belongs to and its
   rectangle within that wall, the module type, and the layout — the same
   `J<n>:` lines you wrote in Step 5 of Part 1.
3. Reboot the card (the Cards page has a button, or power-cycle it).

Watch it come up: the card asks marquee for `boot.bin`, then for
`<its-mac>.yml`, and marquee answers both from the card's record. **A card
reads its layout only at boot**, so a geometry change in marquee needs a reboot
to reach the card.

> The `.tftp/<mac>.yml` file from Part 1 is no longer used once marquee is the
> boot server — the card's record in marquee *is* its config.

### Step 5: Put something on it

On the **Shows** page, pick or write a show — a YAML scene of layers: video,
images, text, clocks, live data — and play it to the wall. The preview renders
the exact frame a card will receive. The scene language is in
[server/doc/scenes.md](../../server/doc/scenes.md), and `custom/example/` in the
server has a worked show to copy.

For an on-panel program instead, set the card's **mode** to `program` and its
**program** to the name you built in Part 2. The mode is part of the card's
config, so it survives reboots; marquee stops streaming to that card and pushes
it values instead.

---

## Part 5 — Chain more cards

Every card has **two RJ45 jacks**. The first goes towards the network; the
second feeds the next card. So a row of cards needs **one** cable back to the
switch, and the whole row is **one IP address** — the first card's. Every card
runs the **same bitstream and the same firmware**; a card works out on its own
whether it is first in a chain or behind another one, and how far behind.

How it works, in one paragraph: marquee sends a small discovery packet once a
second. Each card's gateware passes every frame on to its second jack about
100 ns after it arrives, and adds one to a hop counter in the discovery
packet as it goes past. A card that hears hop *h* is the *h*-th card behind the
head. It takes the head's IP address, listens for pixels on UDP **7000+h**, and
serves its web page and API on TCP **8000+h**. Replies go back through the
cards in front of it. Nothing here is a separate network: it is the same
Ethernet segment, filtered by port in gateware. The design is in
[ARCH.md §14](../ARCH.md).

> **Status, honestly:** chaining is new. The gateware is covered by an
> adversarial simulation suite, and two E320s cabled together have now run it
> for real: discovery and hop numbering, the member's own page and API through
> the head, streaming to both, firmware and bitstream updates over the chain,
> and the member netbooting itself. Not yet run on two cabled cards: a program
> frame-synced across both. Treat this part as the newest path in the project.

### Step 1: Prepare each extra card once

Each card needs the chain-capable images — **gateware 3.0.5 or later and
firmware 5.0.0 or later**, and for the member netboot below **firmware 5.0.5**
(current: gateware 3.0.7 on the E320, 3.0.6 on the 5A-75E) — on every card,
head included. A new card is prepared exactly like Part 1, Step 7, on the
bench, with the same build:

```bash
./build.sh --panel 128x64 --cable ft2232_b flash-all
```

That one JTAG write puts three things in flash: the **golden** recovery image
(at `0x200000`), the application bitstream (at `0`) and the firmware (an FBI
image at `0x100000`; [FLASHING.md](FLASHING.md) has the offsets and the
E320's four-output build). The firmware copy matters more for a chained card
than for the head. A card behind another **never gets a DHCP lease** (it is
not on the switch, and broadcasts are not passed down the chain), so its BIOS
cannot netboot: it starts the firmware in its own flash, and marquee then
netboots it into the chain's image over HTTP. After this one JTAG write,
everything else arrives over the network.

> **The golden image matters too.** If a card's application bitstream is ever
> damaged, the FPGA falls back to the **golden** recovery image written once
> over JTAG ([FLASHING.md §1a](FLASHING.md)). A golden built before chaining
> existed does not forward anything to its second jack, so a card that falls
> back to one cuts off every card behind it. When you prepare a card that will
> head a chain, or sit in the middle of one, write a chain-capable golden too.

### Step 2: Cable it

Run a short cable from the head card's **second** jack to the new card's
**first** jack. A third card goes on the second card's second jack, and so on.
No switch between them: the cards themselves are the chain.

On the E320 the first jack is the one marked `eth0` (the left one, facing the
connectors) and the second is `eth1` (the right one): the switch goes into card
1's left jack, card 1's right jack into card 2's left jack. Not sure which jack is which on your
board? Plug the cable in and ask the head:

```bash
curl -s http://<head>/api/status | python3 -m json.tool | grep -A12 '"chain"'
```

`"port1_link": true` means the cable is in the head's downstream jack and the
link is up. `false` with a cable plugged in means you used the other jack.

### Step 3: Power up and check it found its place

Power the new card. It starts the firmware in its flash, hears marquee's
discovery within a second or two, and becomes hop 1:

```bash
curl http://<head>:8001/api/status
```

Look for `"role":"member","hop":1`. The card's own web page is at
`http://<head>:8001/`, and the head's page lists the chain behind it with a
link to each card.

No answer on `:8001`? In order:

1. **marquee's player is not running**, or discovery is turned off
   (`MARQUEE_CHAIN_DISCOVERY` in `.env`). With no discovery, a chained card has
   no idea it is chained.
2. **The card has older firmware in flash** — one without chain support. Redo
   Step 1.
3. **The head is on older gateware.** A head before 3.0.0 does not pass
   anything to its second jack at all. `curl http://<head>/api/bitstream`.

### Step 4: Register it in marquee

On the **Cards** page, find the head card's **chain** row and press **scan**. marquee
walks the chain — hop 1, hop 2, … until a card has nothing plugged into its
second jack — and lists every card that answers, matched to its record by MAC.
Press **add** next to a new one: it is registered with the head's address and
its hop number. Then give it what any card needs — its rectangle in the wall,
its module type and layout — and reboot it.

From then on it is an ordinary card in every way that matters:

- **Streaming:** marquee renders the wall once per frame and sends each card
  its own crop, to its own port. Nothing else to set.
- **Programs:** put every card of the wall in `program` mode with the **same
  program**. Each card renders the whole wall's coordinates and draws only its
  own rectangle, so a scroller crosses from card to card. The cards keep in
  step off a shared clock carried in the discovery packets: every card swaps to
  its next frame on the same 33 ms boundary.
- **Firmware updates:** a chain runs one image — push the **head** on the
  **Firmware** page and marquee brings the whole chain over: the head
  netboots it, and each member fetches it into memory and switches to it
  (about 8 s). Nothing is written to flash by an update. Flash keeps a
  *survival copy* for a boot with no marquee to reach, and marquee rewrites it
  by itself once the new image has run cleanly for ten minutes. A card that
  was off during an update is brought onto the image when it comes back.
- **Bitstream updates:** as for any card — written to each card's flash over
  the chain, live on all of them at one power cycle.

Moving a card to a different position is a re-cable and a re-scan: a card that
hears a different hop number for three discoveries in a row moves to it, and a
card that finds itself plugged straight into the switch again reboots as an
ordinary card.

### Limits worth knowing

- **Up to 63 cards behind one head.** In practice the limit is bandwidth: all
  the cards in a chain share the head's one gigabit link, so a chain of video
  cards needs their total stream to fit in about 900 Mbit/s.
- **Each hop adds a fixed delay of about 100 ns** in gateware, plus the
  PHYs' own latency — under a microsecond, far below anything visible.
- **A card that dies breaks the chain behind it.** Every frame for the cards
  further down passes through it. Keep chains as short as the wiring allows if
  that matters to you.

---

## FAQ — read this before asking

### The JTAG detect says `empty`

Try the other FT2232 channel: `--cable ft2232_b` instead of `--cable ft2232`.
A wrong channel is indistinguishable from a dead board. This gets people
constantly. If both fail, check 3.3V/GND, then the power supply.

### Nothing lights up at all

In order of how often it is each one:

1. Power supply too small (see the top of this page)
2. Ribbon cable in backwards, or in the wrong connector
3. `--panel` does not match your actual panel size
4. The `.tftp/<mac>.yml` names the wrong connectors

### The whole panel is one colour, but I can see the shapes

The panel is in **indexed** mode while something is sending it full-colour
frames. Each pixel's last byte is being read as a palette index instead of as
blue. `curl http://<board-ip>/api/display` tells you which mode it is really in.

### How long should it take to come up after a power cut?

**About 6 seconds**, power to the board's first DHCP. If you are watching the
panels rather than the network it can feel longer, because nothing is sent to
them until whatever is driving them notices the panel is back.

Most of those 6 seconds is the BIOS reading the firmware out of SPI flash, which
it does three times over — it CRCs the image twice and then copies it. That is
why `LiteSPIPHY(default_divisor=1)` matters: at LiteSPI's own default the same
boot took over 20 seconds. If you rebuild the gateware and cold boot suddenly
takes half a minute, that default is the first thing to check.

### My `deploy` did nothing

Check whether the panel booted from the network or from flash. A cold boot with
no server reachable comes up from **flash**, and flash still holds whatever you
last wrote there:

- `./build.sh deploy` + reboot changes what it runs **now**
- `./build.sh flash-firmware` changes what it runs **after a power cut**

marquee's TFTP log tells you which happened: `boot.bin` **and** `<mac>.yml` means
it netbooted; `<mac>.yml` alone means it came out of flash.

### It worked, then I power-cycled it and now it is broken

You used `sram` and never ran `flash-all`. `sram` is volatile — the board came
back on whatever was in flash from before, which may be old enough that today's
firmware crashes on it. Run `./build.sh --panel <size> --cable <cable> flash-all`.

### My video stream shows a frozen picture

The DMA is off. `curl -X POST http://<board-ip>/api/dma/on`. See the warning in
Part 3 — this is the single most common streaming issue, and it does not look
like a networking problem.

### My program's text is invisible

Your background is listed **before** the text. First match wins, so the first
widget is on top. Put backgrounds last.

### My program runs at 2 fps

You are writing too many pixels. A 128×128 full redraw has a hard ceiling near
**12 fps** — there is no cache, and every memory access costs ~190 cycles, so
the ceiling is arithmetic, not inefficiency.

The fix is to not redraw the whole panel: `display.dynamic` redraws only a band
of rows, `scroll:` shifts pixels instead of recomputing them, and animating the
**palette** recolours the entire wall for the cost of 256 words. A dashboard
goes from 1.2 to 8.5 fps with a band; a ticker goes from 10 to 25 fps with a
scroll. [The programmer's guide](PROGRAMMING.md) covers all three.

Also: **never use floating point.** This CPU has no FPU, so a float per pixel
is soft-float emulation and will take you under 1 fps on its own. Use
`ctx.int()`, not `ctx.num()`.

### I changed the YAML and nothing changed

`panelc` is a *compiler*, and it runs on your machine, not the board. Three
steps every time:

```bash
python3 tools/panelc.py panels/mine.yaml    # YAML -> Rust
./build.sh firmware                          # Rust -> binary
./build.sh --panel 128x64 --cable ft2232_b sram   # binary -> board
```

### A setting in my `.yml` does nothing

Check the spelling. Unknown keys are ignored silently — a typo is a no-op, not
an error message.

### Can I use panels that aren't 128×64?

Yes: `96x48`, `256x64`, `64x32` and `64x64` are supported. Pass the one you have
to `--panel`.

### How many panels can I drive?

Per card: the default build drives six HUB75 connectors (`--outputs`, up to
the 16 the 5A-75E has), and each connector can drive a chain of panels.
**Prefer wide over deep** — outputs shift in parallel and cost you nothing,
while daisy-chaining panels off one connector halves the refresh rate.

Past one card, add cards: [Part 5](#part-5--chain-more-cards) chains them
through their second RJ45 so the whole row is one IP address.

### Does it need a server running somewhere?

Only for streaming. A program written in Part 2 runs entirely on the board, with
nothing else on the network. That is the whole point of it.

A **chained** card is the exception worth knowing: it boots its firmware from
its own flash, but it learns its place in the chain, and fetches its config,
from marquee. With no server it still starts — on the program and layout baked
into it, or its last-flashed firmware — but it will not know it is chained.

### My second card never gets an IP address

If it is plugged into the first card's second RJ45, that is correct: a chained
card never gets a DHCP lease, by design — that is how it knows it is behind
another card. Reach it at `<head>:8001`, not at an address of its own. If it
also does not answer there, see [Part 5](#part-5--chain-more-cards) step 3.

---

## Where to go next

| I want to… | Read |
|---|---|
| Understand the build and deployment properly | **[INSTALLATION.md](INSTALLATION.md)** |
| Write serious on-panel programs | **[PROGRAMMING.md](PROGRAMMING.md)** |
| Know how the hardware works | **[ARCH.md](../ARCH.md)** |
| Talk to the board from my own code | **[API.md](../API.md)** |
| Understand how chaining works inside | **[ARCH.md §14](../ARCH.md)** |
