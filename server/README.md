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

Marquee does five things:

1. **Owns the fleet.** Every card is a record: address, geometry, its position
   within a wall, layout, firmware, packet pacing. Cards are polled and their
   counters stored, so performance and availability have a history rather than
   a live number.
2. **Renders content.** A declarative *scene language* (YAML) composes video,
   images, text, live camera and data into frames, host-side. See `doc/scenes.md`.
3. **Streams it.** A wall's canvas is rendered **once** per frame and each card
   is sent its own crop of it, so several boards show one picture without
   drifting apart. Frames go out over the card's UDP bitmap protocol at up to
   the rate the hardware pixel DMA can absorb. Two wire formats: `rgb` (RGB888)
   and `indexed` (one byte per pixel plus a palette), the latter being ~3x fewer
   packets and so ~3x the frame rate on content that survives 256 colours. Set
   per scene via `display.format` — see `doc/scenes.md`.
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

## Card modes

A card is either **streamed** to or runs an **on-board program**. Never both —
each writes the framebuffer. On a multi-card wall this is per card: one board
can run a program while its neighbours stream.

| | Streamed | On-board program |
|---|---|---|
| Rendering | marquee, host-side | the card |
| Wire | ~24 Mbit/s of pixels | ~20 bytes/s of values |
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

    cp .env.example .env      # fill in secrets
    make up-build
    make health

## Stack

FastAPI + SQLAlchemy 2 + Alembic + SQLite, Pillow + ffmpeg for rendering,
React/Vite frontend on the `homelab-system-ui` design language.

## Docs

| Document | What it covers |
|---|---|
| [`doc/scenes.md`](doc/scenes.md) | the scene language reference — **read first if you touch rendering** |
| [`doc/providers.md`](doc/providers.md) | writing a data provider — the plug-in plane for live data |
| [`ARCH.md`](ARCH.md) | how the services fit together, walls vs cards, the wire protocol, card modes |
| [`API.md`](API.md) | HTTP API, including the `/api/v1/play` integration endpoint |
| [`CHANGELOG.md`](CHANGELOG.md) | version history |
| [`CLAUDE.md`](CLAUDE.md) | working in this repo |
| [`LICENSE`](LICENSE) | BSD 2-Clause |
| [`THIRD-PARTY.md`](THIRD-PARTY.md) | dependency licences, and the ffmpeg caveat |

In the **colorlight** repo (the card side):

| Document | What it covers |
|---|---|
| `docs/GETTING-STARTED.md` | lighting a wall from scratch |
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
