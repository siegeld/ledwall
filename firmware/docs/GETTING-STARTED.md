# Getting started

**You have a Colorlight board, some LED panels, and an hour.** By the end of this
page the panels will be lit, running a program you wrote, and playing a video.

No FPGA experience needed. You will not write any Verilog. Everything builds in
Docker, so nothing gets installed on your machine.

Three parts, in order:

1. **[Light it up](#part-1--light-it-up)** — build, flash, see a test pattern *(~30 min, mostly waiting)*
2. **[Write a program](#part-2--write-a-program)** — YAML in, pixels out *(~15 min)*
3. **[Play a video](#part-3--play-a-video)** — stream from your laptop *(~5 min)*

Then: **[the FAQ](#faq--read-this-before-asking)**. If something is weird, the
answer is probably there.

---

## Before you start

You need:

- A **Colorlight 5A-75E, rev 8.2** receiver card (~$15 on AliExpress)
- One or more **HUB75 LED panels** and the ribbon cables they came with
- A **5V power supply** — see the warning below
- A **JTAG programmer**: an FTDI FT2232 board (~$10) or a USB Blaster
- **Docker**, and an ethernet cable

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

> ### From here on, you need marquee — not this repo
>
> Nothing in the colorlight repo runs at runtime. Once the card is programmed it
> is served entirely by **marquee**: boot, config, pixels, live values, stats.
>
> You come back here to build new firmware, write a new on-panel program, or
> change panel geometry — not to run the sign.

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
    - {name: big, file: assets/fonts/DejaVuSans-Bold.ttf, size: 20}

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
./build.sh --default-config configs/standalone-128x128.yml firmware
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
| `custom/dashboard/panels/dashboard-home.yaml` | a four-page dashboard built from pushed values |

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

### Doing this properly

Streaming from a laptop with a Python script is the demo. For something
permanent — scheduled content, multiple panels, Home Assistant data, a web UI
to manage it all — there is a companion service called **marquee** that does
all of it. This guide deliberately does not require it.

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

The rev 8.2 board has six HUB75 connectors, and each can drive a chain of
panels. **Prefer wide over deep** — outputs shift in parallel and cost you
nothing, while chaining panels off one connector halves the refresh rate.

### Does it need a server running somewhere?

Only for streaming. A program written in Part 2 runs entirely on the board, with
nothing else on the network. That is the whole point of it.

---

## Where to go next

| I want to… | Read |
|---|---|
| Understand the build and deployment properly | **[INSTALLATION.md](INSTALLATION.md)** |
| Write serious on-panel programs | **[PROGRAMMING.md](PROGRAMMING.md)** |
| Know how the hardware works | **[ARCH.md](../ARCH.md)** |
| Talk to the board from my own code | **[API.md](../API.md)** |
