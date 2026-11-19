# Marquee

Fleet control and content for LED walls driven by Colorlight receiver cards
(see the `colorlight` repo for the firmware/gateware side).

> **Two words, used precisely.** A **wall** is what a viewer sees: one logical
> display with one picture on it. A **card** is one receiver board driving a
> rectangle of that wall — a large wall needs several, because a receiver has a
> fixed number of output connectors, a finite framebuffer, and memory bandwidth
> that sets the refresh rate. Content is assigned to walls; boards are
> configured as cards. See [ARCH.md](ARCH.md) §2a.

> ### ⚙️ Once a colorlight card is programmed, marquee is all you need
>
> Nothing in the colorlight repo runs at runtime — it builds gateware and
> firmware, compiles on-board programs, and flashes cards. After that marquee
> is the entire runtime: boot, config, pixels, live values, stats.
>
> You go back to colorlight only to write a **new** on-board program, change
> card geometry, or build new firmware. Switching a card between the programs
> its firmware already carries is a change **here**.

> ### 🔗 One cable, one IP, a whole wall of cards
>
> Colorlight cards **daisy-chain** through their second RJ45, and marquee runs
> the chain as one thing: it finds every card behind a head by itself (a
> discovery packet each second; each card's gateware counts the hop), streams
> each card its own crop of the wall on its own port, reaches each card's
> web page through the head -- or through marquee from anywhere -- and keeps
> **the whole chain on one firmware image**. A head netboots it; a card behind
> another cannot, so marquee has it fetch the image into memory and switch to
> it, writing nothing. Each card's flash keeps a survival copy that marquee
> rewrites only after an image has proved itself, so a chain with no server
> still comes up on one version. Tested on two cabled E320s, 2026-11-13: scan,
> stream, member page, firmware and bitstream rollouts, member netboot in
> 8 s. [ARCH.md](ARCH.md) §2b and §2c.

Marquee does seven things:

1. **Owns the fleet.** Every card is a record: address, geometry, its position
   within a wall, module type, layout, firmware, packet pacing. Cards are
   polled and their counters stored, so performance and availability have a
   history rather than a live number.
2. **Renders content.** A declarative *scene language* (YAML) composes video,
   images, text, live camera and data into frames, host-side. See `doc/scenes.md`.
3. **Streams it.** A wall's canvas is rendered **once** per frame and each card
   is sent its own crop of it, so several boards show one picture without
   drifting apart. Frames go out over the card's UDP bitmap protocol at up to
   the rate the hardware pixel DMA can absorb. Two wire formats: `rgb` (RGB888)
   and `indexed` (one byte per pixel plus a palette), the latter being ~3x fewer
   packets and so ~3x the frame rate on content that survives 256 colours. Set
   per scene via `display.format` — see `doc/scenes.md`. A frame over 255
   packets (four modules on one card) goes out with a wider header
   automatically; every packet stays inside a 1500-byte MTU, because the card
   cannot reassemble fragments (ARCH.md §4).
4. **Plugs in live data.** A *data provider* is a Python file that fetches
   something and publishes it on named topics; shows read it with
   `{data:topic|path}` bindings and the `rows` layer draws a table from it.
   Drop the file into `custom/<site>/providers/` and restart — no registration,
   no rebuild. See `doc/providers.md`.
5. **Boots cards.** A TFTP service serves each card its firmware and its
   layout, so firmware rollout is a fleet operation with a record. (Per-card
   firmware **can** be targeted over TFTP on colorlight v2.3.0 or later, where
   the BIOS does its own DHCP with a MAC derived from the card's flash unique
   ID and so asks from its own address. On earlier bitstreams the BIOS fetched
   before it had any lease, from an address identical on every card, and staged
   rollout meant flashing. See ARCH.md.)
6. **Updates card firmware over the network, and keeps it in sync.** The
   **Updates** page stages images, shows each card's assigned image next to the
   version it is actually **running** and the state of its flash **survival
   copy**, pushes one to a card (for a chain, to its head; the members follow),
   and rolls one across a wall or the fleet a chain at a time. An update writes
   nothing to the board -- a head netboots the image, a chain member is
   netbooted into it by marquee (colorlight 5.0.5) -- so this needs **no JTAG
   and no hands on the hardware**, and a bad image cannot brick a board: push
   a different one. A **firmware sync** then keeps every card on its assigned
   image (a card found running something else is brought back) and writes each
   card's survival copy -- what it runs if it ever boots with no server -- once
   the image has run cleanly for ten minutes. ARCH.md §2c.
7. **Updates card FPGA bitstreams over the network too.** The gateware in a
   card's SPI flash — a different artifact from the firmware, on a different
   path. Upload a `.bit`, press Write to flash, and the card streams it into its
   own flash a sector at a time -- chain members too, through their head.
   **No JTAG**, which is the point: a receiver card ends up bolted behind a
   panel. It becomes live at the card's next **power cycle**, because nothing
   reconfigures an FPGA from software; one power cycle brings up a whole chain
   on it. A bad image cannot strand a card — a golden recovery bitstream is
   written once over JTAG and never again, and the ECP5 falls back to it.
   Needs colorlight **2.16.0**.

## What it drives

| Card | Outputs | Modules | Wall | Streams |
|---|---|---|---|---|
| Colorlight **5A-75E** (HUB75) | up to 16 connectors | HUB75 panels, e.g. 128x64 | per layout | RGB or indexed; on-board programs |
| Colorlight **E320** (HUB320, four RGB groups a connector) | **4** of its 8 ports, in the four-output build | ARGO P1.25 256x128, DP3264S S-PWM drivers, stacked | **256x512**, one card | 270-packet RGB frames at **20 fps**, panel refresh **66.7 Hz** |

What is on a card's connectors is its **module type**: geometry, RGB group
arrangement, driver chip, bitstream preset. The modules this project has
driven are built in -- the HUB75 128x64, ARGO's P1.25 above, and Myled's P1.25
128x256 (ICND1065L, its own bitstream preset) -- and any other is a JSON file
under `custom/<site>/modules/`, no code change (`custom/README.md`). Modules
that hang turned from the way their chips scan them are a card's `rotate`
(0/90/180/270): marquee turns each streamed frame to match (ARCH.md §2b; what
the card draws itself needs card firmware 5.7.0+). A module type may also give one RGB group's size
(`group_columns`, `group_scan`) for cards on a runtime-geometry bitstream
(colorlight 5.8.0), where one bitstream serves modules of several sizes.

The E320 numbers are measured on the bench: a full four-module 256x512 stream
with 0 underruns and 0 held frames. Getting there took gateware work on the
card (colorlight `docs/E320-BRINGUP.md` §6) and one protocol fix here: the
wide header for frames over 255 packets first made every datagram one byte
too large for the MTU, which dropped the last pixel of every packet. See
CHANGELOG 0.42.2.

**Cards chain.** Each card has two RJ45s; the second feeds the next card, so
one cable from the switch can run a whole row of cards. **A chain is one IP
address** -- the head card's. The card at hop h answers at `<head>:8000+h`
and takes pixels on `7000+h`; it finds its own position automatically from a
discovery packet the player sends once a second, and cannot get a DHCP lease
by design (that is how it knows it is behind another card). The Cards page
scans a head's chain and registers what it finds, and each card's own page is
one click away (`/cards/{id}/controller/`, relayed through the sender host).
Every card runs the same bitstream and firmware; a chain runs one firmware
image, its head's. One gigabit link carries the whole chain, about 38 Mpx/s
of RGB888. ARCH.md §2b and §2c; the card side is colorlight `ARCH.md` §14.

| Chain test, two E320s (2026-11-13) | Result |
|---|---|
| discovery, hop ports, chain scan | member found at hop 1, matched by MAC |
| member's own web page | through the head (`:8001`) and through marquee |
| firmware rollout to the chain | head netbooted, member brought over, both verified |
| bitstream to both cards' flash | live after one power cycle, both back in ~21 s |
| member netboot into another image | 8 s, flash untouched; a reboot brings back the survival copy |

**Content is authored for a wall's size.** Integer boxes in a show are
pixels, so a show written for a two-module 256x256 wall paints only the top
half of a 256x512 one -- the lower modules stay black and look dead. Use
fractional boxes, or a show authored for the wall (`doc/scenes.md`, "One show,
many walls").

## Why host-side rendering

The card's CPU is a VexRiscv with no D-cache. It cannot do fonts, codecs or
HTTP, and it should not try — measured, its per-pixel loop tops out around
600k px/s. Rendering on a host and streaming finished frames is what the
signage industry does (a media server feeding an LED processor), and it means
content changes never require touching the card.

That holds for anything with a codec or a photograph in it. It is **not** the
whole story any more: a card can also run an on-board program, where the
artwork is rasterised at build time and the card draws from named values. See
below.

## Other software can draw on it: DDP in

The player listens for **DDP** (UDP 4048) and answers like a pixel
controller, so WLED, xLights, LedFx and anything else that can "add a DDP
device" can be pointed at the marquee host and discover it. A show places
their picture with a `ddp` layer, scaled and composited like a video, and
whatever is under the layer shows whenever nothing is sending.
[doc/scenes.md](doc/scenes.md#ddp--let-other-software-draw-on-the-wall).

## Card modes

A card is either **streamed** to or runs an **on-board program**. Never both —
each writes the framebuffer. On a multi-card wall this is per card: one board
can run a program while its neighbours stream.

| | Streamed | On-board program |
|---|---|---|
| Rendering | marquee, host-side | the card |
| Wire | ~32 Mbit/s of pixels for 256x256 at 20 fps, ~63 for 256x512 | ~20 bytes/s of values |
| Host dies | **keeps showing the last frame**, which looks correct forever | keeps last values, and can mark them stale |
| Good for | video, photos, anything with a codec | dashboards, tickers, status |

Set it on the Cards page, or `PATCH /cards/{id}` with `mode` and `program`.
It is served to the card in its TFTP config, so it survives a reboot.

That failure row is the one that decides the choice as often as frame rate does.
Nothing in the firmware blanks a card when the stream stops — no new frames
simply means the last complete one stays lit. A dead host therefore looks
*exactly* like a working sign until someone notices the clock has not moved. A
program-mode card still has its pixels, so it can render the fact that its data
went quiet.

Programs are written and compiled in the **colorlight** repo — see
`docs/PROGRAMMING.md` there. marquee's job for those cards is to push
values (`services/player/valuepush.py`) and to stay off the pixel path.

### A card can also carry its own config

A card's firmware can have a layout and program baked in at build time, so it
runs standalone with no network at all. **marquee still wins when it answers** —
boot precedence is the TFTP config, then the card's baked default, then a bare
single card. A baked default can never make a card ignore a change made here;
it is what the card shows *before* marquee answers, and what it falls back to
if marquee never does.

## Your content lives in `custom/`

```
custom/<your-site>/
  shows/         scene documents (.yaml)
  providers/     data providers (.py)
  modules/       module types (.json)
```

Shows are files, loaded into the database:

```bash
make shows-import SITE=<your-site>    # files -> database (idempotent)
make shows-export SITE=<your-site>    # database -> files, after editing in the UI
```

> **The database is what the player reads.** Editing a show file does not reach
> a wall until `shows-import` runs — which is also why a preview can be right
> while the wall is still showing the previous version.

Before this they existed only as rows in an untracked SQLite file — no review,
no history, no backup. `custom/README.md` has the details; the same split
applies on the firmware side, where `colorlight/custom/` holds on-board programs
and baked layouts. The test is *does changing it require rebuilding firmware?*

`custom/example/` ships a worked show **and** a worked provider — a Metro-North
rail board off the MTA's public feeds, which needs no API key and so runs out
of the box. Copy either to start.

## Quick start

    cp .env.example .env      # set the admin password and session secret
    make up-build             # UI :8411, API :8410, TFTP :6969 (host network)
    make health

Then point **DHCP option 66** at this host so cards fetch their firmware from
it, upload a firmware image on the Updates page, make a wall, and add each card
(name, IP, MAC, module type, rectangle, layout) on the Cards page. Chained
cards are found for you: scan the head's chain on the Cards page and add what
answers.

Run **one** marquee per fleet. A second one pointed at a copy of the database
(a dev host) must set `MARQUEE_CHAIN_DISCOVERY=false` and
`MARQUEE_FIRMWARE_SYNC=false`, or both discover and reboot the same cards.

The whole walkthrough, from a bare card to a multi-card wall, is the
colorlight repo's `docs/GETTING-STARTED.md` — Part 4 is this server, Part 5 is
chaining.

## Stack

FastAPI + SQLAlchemy 2 + Alembic + SQLite, Pillow + ffmpeg for rendering,
React/Vite frontend on the `homelab-system-ui` design language.

## Docs

| Document | What it covers |
|---|---|
| [`doc/scenes.md`](doc/scenes.md) | the scene language reference — **read first if you touch rendering** |
| [`doc/providers.md`](doc/providers.md) | writing a data provider — the plug-in plane for live data |
| [`ARCH.md`](ARCH.md) | how the services fit together, walls vs cards, card chains (§2b), module types and turned modules, the firmware policy (§2c), rendering at the wall's size, the wire protocol and its MTU budget, pacing, card modes, boot |
| [`custom/README.md`](custom/README.md) | your shows, data providers and module-type files |
| [`doc/dedicated-link.md`](doc/dedicated-link.md) | putting the cards on their own link: topology, DHCP, bring-up, measured results for two to four modules |
| [`API.md`](API.md) | HTTP API, including the `/api/v1/play` integration endpoint |
| [`CHANGELOG.md`](CHANGELOG.md) | version history |
| [`LICENSE`](LICENSE) | BSD 2-Clause |
| [`THIRD-PARTY.md`](THIRD-PARTY.md) | dependency licences, and the ffmpeg caveat |

In the **colorlight** repo (the card side):

| Document | What it covers |
|---|---|
| `docs/GETTING-STARTED.md` | from a bare card to a managed, chained wall — Parts 4 and 5 are this server |
| `docs/PROGRAMMING.md` | writing programs that run on a card |
| `docs/BENCHMARKS.md` | measured frame rates for both pixel paths |
| `docs/HARDWARE.md` | what the Colorlight card actually is, and its traps |
| `docs/FPGA-GUIDE.md` | the gateware, for people who have not used an FPGA |

## Help

The web UI has a searchable **Help** page covering the scene language, walls and
cards, card modes, on-board programs, integration and a symptom-first troubleshooting
section. It is the fastest route to an answer for anything operational.

## Credits

The rendering and serving stack is other people's work:

- **[ffmpeg](https://ffmpeg.org/)** — every frame of video, every camera, every
  stream. The scene language is a thin declarative layer over it.
- **[Pillow](https://python-pillow.org/)** — compositing, text and the quantiser
  behind `format: indexed`.
- **[FastAPI](https://fastapi.tiangolo.com/)**, **Starlette**, **Pydantic**,
  **[SQLAlchemy](https://www.sqlalchemy.org/)** and **Alembic** — the API and
  the schema.
- **[React](https://react.dev/)** + **[Vite](https://vite.dev/)** +
  **[Tailwind](https://tailwindcss.com/)** — the console, with
  **[TanStack Query](https://tanstack.com/query)** for server state,
  **[Recharts](https://recharts.org/)** for the stats graphs and
  **[Lucide](https://lucide.dev/)** for icons.
- **[tftpy](https://github.com/msoulier/tftpy)** — the boot service that serves
  cards their firmware.
- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** — resolving `youtube:` sources.

The panel side stands on the reverse engineering of the Colorlight cards by
[q3k/chubby75](https://github.com/q3k/chubby75) and the LiteX ecosystem — see
the firmware repo's README for those credits in full.

See [THIRD-PARTY.md](THIRD-PARTY.md) for the complete list with licences.
