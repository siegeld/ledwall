# Architecture

Marquee renders shows and streams them to LED walls, and configures the boards
that render themselves. It is a `/srv/docker` service on the homelab standard —
see [README.md](README.md) to run it and [doc/scenes.md](doc/scenes.md) for the
scene language, which is the app's contract with its users.

**Two words, used precisely throughout.** A **wall** is what a viewer sees: one
logical display with one picture on it. A **card** is one receiver board driving
a rectangle of that wall. Section 2a says why they are not the same object.

The card side of this system lives in the **colorlight** repo — gateware,
firmware, and the on-board programs.

---

## 0. Marquee is the runtime

**Once a colorlight card is programmed, marquee is all you need.** Nothing in
the colorlight repo runs continuously — it is a build and flashing toolkit.
Marquee serves the card its firmware and config at boot, then either streams it
pixels or pushes it values, and records what it is doing.

The division: **colorlight builds what is on the board, marquee runs it.**

Netboot takes precedence over flash, so while marquee is reachable the image
marquee serves is the one that runs. A flashed image is the fallback for when it
is not — which is what makes a standalone card possible.

## 1. Three containers, one package

```
                          ┌──────────────┐
  browser ───────────────▶│   frontend   │  Next.js, :8411
                          └──────┬───────┘
                                 │ REST + WS
                          ┌──────▼───────┐
                          │     api      │  FastAPI, :8410
                          └──────┬───────┘
                                 │
                          ┌──────▼───────┐        SQLite
                          │   marquee.db │◀──────────────┐
                          └──────┬───────┘               │
                                 │                       │
                          ┌──────▼───────────────────────┴──┐
                          │            player                │  host network
                          │  supervisor · TFTP · valuepush   │
                          └───┬───────────────┬──────────┬───┘
                              │ UDP 7000      │ TFTP     │ HTTP
                              ▼               ▼ 6969     ▼
                        ┌───────────┐   ┌───────────┐  card /api/*
                        │ cards in  │   │ cards     │
                        │  STREAM   │   │ booting   │
                        └───────────┘   └───────────┘
```

| Service | Port | Does |
|---|---|---|
| `api` | 8410 | REST, auth, the fleet, previews, websocket fanout |
| `frontend` | 8411 | the operator UI |
| `player` | **host** | renders, streams, serves TFTP boot, pushes values |

**`packages/marquee_core/` is the single source of truth** for the scene model,
the renderer, the wire protocol and the DB models. `services/*` import it. A
model copied into a service is a bug waiting to happen — the protocol especially,
because the card will not complain, it will just draw garbage.

### Why `player` runs on the host network

Two independent reasons, both hard requirements:

- The pixel stream is **1250+ UDP packets a second** to cards on the local
  segment. NAT in that path is a cost nothing else pays for.
- **TFTP boot replies must come from the host address the card's BIOS was
  pointed at.** A card asks a specific IP for `boot.bin`; a reply from a
  container address is not accepted.

---

## 2. The data model

| Table | Holds |
|---|---|
| `walls` | a logical display: name, pixel size — **what content is assigned to** |
| `cards` | a receiver board: host, MAC, size, **position in its wall**, layout YAML, **mode**, program, pacing |
| `shows` | a scene document (YAML text) |
| `assignments` | the default show for a **wall** — one per wall |
| `schedules` | day-part overrides: show, time window, days, priority — per **wall** |
| `card_stats` | a time series sampled from each card's own counters |
| `settings` | key/value: HA credentials, cameras, Wowza base |
| `users`, `api_tokens` | operators and machine callers |

Shows are **authored in files** under `custom/<site>/shows/` and imported into
the `shows` table (`make shows-import`). The database is what the running system
reads; the files are what gets reviewed, versioned and backed up. `import_dir`
is idempotent and only writes rows whose content changed, so re-running it is
how a file edit reaches a wall.

Show selection is `resolve_show_for(wall, now)`: the highest-priority enabled
schedule whose window contains `now`, else the assignment. **It is re-evaluated
every tick**, so a day-part change or an override takes effect within one scene
frame — nothing restarts.

Schema changes go through **Alembic**, never hand-rolled ALTERs.

---

## 2a. Walls and cards — why they are two things

Until v0.11.0 there was one object, `panels`, that was both the board and the
display. That held for exactly as long as one board could drive a whole wall. It
cannot, and the three reasons arrive together as soon as a wall gets large:

| Limit | Value on current hardware |
|---|---|
| Output connectors per receiver | typically **8** — a nine-module wall needs two boards |
| Framebuffer | **327,680 px** — about ten 256×128 modules, then there is nowhere to put the image |
| Memory bandwidth | sets the refresh rate: **36.8 Hz** for nine modules on one card, **49.1 Hz** split across two |

The third is the interesting one: splitting a wall does not merely make a bigger
wall possible, it makes the wall **faster**, because each card only reads its own
region out of memory. That is also why commercial LED systems scale by adding
receivers rather than by building bigger ones.

So:

```
        wall "lobby"  768 x 384                     one picture, one show
   ┌──────────────────┬──────────────────┐
   │   card A         │   card B         │          two boards
   │   (0,0) 384x384  │   (384,0) 384x384│
   └──────────────────┴──────────────────┘
              │                  │
              ▼                  ▼
      crop (0,0,384,384)  crop (384,0,768,384)      one render, two crops
```

**The render happens once.** `WallPlayer` draws the whole canvas for a frame and
sends each card `canvas.crop(card.box)`. That is cheaper than rendering per card,
and it is the only way two boards stay in step — they are handed the same frame,
from the same render, in the same pass. Rendering twice would put two independent
frame clocks on one picture.

**A card does not know it is part of a wall.** It receives a frame sized to its
own region and displays it. Nothing in the firmware or the wire protocol changed
to make this work, which is why it applies equally to the HUB75 boards running
today and to newer S-PWM hardware.

**What belongs where.** Content is a property of the wall — every board covering
one picture must play the same show, and assigning per card would let them
disagree. Health is a property of the card: on a two-card wall it is precisely
the difference between the two stat series that says which board is struggling.
Mode is per card too, so one board can run an on-board program while its
neighbours stream.

**Gaps and overlaps are reported, not rejected.** An operator mid-edit will
briefly have both, so `GET /walls/{id}/layout` returns them as problems and the
UI draws them. A silent gap is a black band nobody can explain; a silent overlap
is two boards rendering the same pixels.

---

## 3. Rendering

`marquee_core/render.py` — **a Show plus a wall-clock time → one RGB frame.**

Pillow composites; ffmpeg decodes video. Deliberately **no numpy**: at 256×128 a
frame is 32k pixels and PIL's C paths are comfortably fast enough, and leaving
numpy out keeps the image small.

The renderer is a **pure function of `(show, t)`** apart from video decoders,
which necessarily hold position. That is what makes the web UI's preview the
*same code path* as the stream — so what an operator previews is what the card
shows, rather than an approximation that drifts.

Layer types, bindings and sources are documented in
[doc/scenes.md](doc/scenes.md). The scene language is a **breaking-change
surface**: read that before touching it.

**The `rows` layer is the one place the renderer loops.** A scene is a flat
stack of layers by design — no expressions, no iteration — but a table is data
whose *length* is not known when the show is written, and spelling one out as
N×M text layers bakes the N in and draws every slot whether or not data reached
it. `rows` takes a list from the live cache and draws one row per entry, capped
by what fits in its box. Its cells are rendered by `_text` on a synthetic layer
rather than by drawing there: alignment and the continuous-scroll machinery are
subtle enough that a second copy would drift, and on a departures board a
scrolling cell sits directly beside a scrolling text layer.

---

## 4. The wire protocol

`marquee_core/stream.py`. **Fixed by the firmware** — it must match
`sw_rust/barsign_disp/src/bitmap_udp.rs` in the colorlight repo. Changing one
without the other silently corrupts frames.

10-byte little-endian header `<2sHBBHH>`, then pixels:

```
0..1  magic          2..3  frame_id u16
4     chunk_index    5     total_chunks
6..7  width u16      8..9  height u16
```

| Magic | Format | Pixels/chunk |
|---|---|---|
| `BM` | RGB888, 3 B/px | 487 |
| `BI` | **indexed**, 1 B/px | **1461** |
| `BP` | palette — 256 entries in 768 bytes, one packet | — |

**Indexed is the fast path**, and the reason is not bandwidth. The card's bound
is **per-interrupt cost**: its CPU is already out of the pixel loop, and the
panel repo measured ~1.13 packets per ISR entry. One byte per pixel puts the
*same MTU* to work on three times the pixels, so the same frame is ~3× fewer
chunks and the interrupt bound moves by the same factor. Measured end to end:
**36.2 → 102.6 fps**.

> **1461 is `3 × 487`, and it is not negotiable.** It matches the card's
> `PIXELS_PER_CHUNK_INDEXED` and the gateware's chunk stride. Not 1462 — that is
> not divisible by three, and using it would place every chunk after the first
> **one pixel left** of where the hardware writes it. That shows up as *shearing*,
> not as an error.

Quantisation for indexed is **median cut with dithering off**. Dither noise that
vanishes on a 200 dpi screen is individually visible lit pixels at this pitch,
and it crawls on moving content. Choose the format **per show, by content** —
text and graphics quantise invisibly; photographs band.

### Pacing is the whole game

A bare `time.sleep()` per packet drifts badly: the OS rounds every sleep **up**,
so a requested 0.3 ms delivers more and the error compounds across 68 packets.
`CardStream` sleeps against an **absolute deadline** and busy-waits the last
stretch.

`packet_delay` is **per card**, not a constant, because the card's ceiling
depends on which pixel path it is running (~1250 pkt/s on the CPU path, far more
on the gateware DMA).

---

## 5. The player supervisor

**One thread per wall**, not per card. Rendering is CPU-bound in PIL/ffmpeg and
streaming is paced, so walls must not block each other.

Each tick, per wall:

1. resolve the show for now
2. render the **whole canvas** — once, however many cards cover it
3. for each streaming card: `canvas.crop(card.box)`
4. convert (`frame_to_rgb` or `frame_to_indexed`) and stream it, paced per card
5. sample each card's counters into `card_stats`

Step 3 is where a multi-card wall stays coherent. Steps 2 and 4 being on
opposite sides of the crop is also why an *indexed* wall quantises per card:
two crops of one canvas do not generally produce the same 256-colour palette,
so each card gets its own.

The set of cards is reconciled against the database every tick, so a card added
to, moved within, or removed from a wall takes effect without a restart.

### Re-asserting the DMA is not optional

A card **boots with its pixel DMA off**. Enabling it once at player startup is
therefore not enough — any reboot (power cycle, firmware reload, watchdog)
silently drops that card onto the CPU pixel path, where the receive ISR cannot
drain the MAC FIFO.

> The visible result is not "slow". Roughly **3 chunks of every 12 are lost, so
> no frame ever completes**, and the card keeps showing whatever was on it
> before. That looks like a frozen or corrupt display, not a performance
> problem. It cost most of an evening to find — twice.

So `_dma_is_off()` is checked and re-asserted **every supervisor tick**, not
once at startup.

---

## 6. Card modes — and why the player skips half the fleet

A card is in exactly one mode, and the DB is the source of truth:

| `cards.mode` | Card does | Marquee sends |
|---|---|---|
| `stream` | displays frames it receives | **pixels**, ~24 Mbit/s |
| `program` | **runs its own program**, composing pixels from values | **values**, ~20 bytes/s |

The supervisor **skips program-mode cards entirely** — no render, no stream. On
a multi-card wall this is per card: one board can run a program while its
neighbours stream.
`ValuePusher` serves them instead, pushing bus values to `POST /api/values` on
each one every 10 s.

`ValuePusher` starts unconditionally and **asks each card what mode it is in**,
skipping the ones that are streaming. That is deliberate: no second setting to
drift out of sync with the DB.

**WHICH program a card runs comes from the database, not from the card.**
`/api/display` reports `program (on-panel)` without naming it, and marquee is
what serves the name over TFTP in the first place. That matters because values
are **selected by program, never merged**: the on-card table holds 24 keys and
`set()` ignores the 25th rather than evicting, so accumulating every source's
values would silently drop whichever key went last. A provider declares the
programs it can feed (`PROGRAMS`) and returns `board_pages()`; anything else
gets the panel-bus mapping as before.

The **rotation is host-side** for the same reason the page is a value at all:
rows outside a program's `dynamic` band repaint only when a value arrives, so a
card cycling on its own frame counter would redraw the whole canvas every frame.
Measured on the bench card: a four-row board with a scrolling column runs at
**7.9 fps**, against **30.3** — the frame-period cap — with nothing moving.

Two writers on one framebuffer has no good failure mode — it looks like
corruption rather than like a conflict — so the modes are kept mutually
exclusive at every layer.

---

## 7. TFTP boot

`services/tftp/tftpd.py`, on port **6969** — *not* 69. The gateware sets
`TFTP_SERVER_PORT=6969`.

`tftp_resolve(filename, client_ip)` serves cards **by identity**. Anything
unrecognised is refused: this exists to boot cards, not to serve files.

| Request | Answer |
|---|---|
| `boot.bin` | the firmware named on that card's row, else the default |
| `<mac>.yml` | that card's `layout_yaml`, **plus its mode** |

The mode is **appended at serve time** rather than stored in the layout text:

```python
cfg += f"mode: {p.mode or 'stream'}\n"
if p.program:
    cfg += f"program: {p.program}\n"
```

One source of truth, and an operator editing the layout YAML cannot accidentally
strand a card in program mode.

The server thread **restarts itself on failure**. Cards cannot boot without it,
so a crashed thread must not silently leave the fleet unbootable.

> **tftpy is given an EMPTY root, deliberately.** Its RRQ handler serves a
> static file if one exists and only calls `dyn_file_func` when it does not. With
> the firmware directory as the root, `boot.bin` was served straight off disk and
> **`tftp_resolve` was never called for it** — so the `firmware` field on a card
> record did nothing. `<mac>.yml` worked only because no such file exists on
> disk. Do not "tidy" that back to passing the real root.

> **Per-card firmware works on colorlight v2.3.0 or later, and not before it.**
>
> That BIOS derives its MAC from the card's flash unique ID and does its own
> DHCP, so `boot.bin` is requested from the card's own (reserved) address and
> `tftp_resolve`'s lookup by host succeeds. Measured on the bench card: the
> request moved from `192.168.1.50` to `192.168.1.70`, and the log line went from
> `identified as bench … via ARP` to a direct hit. Staged rollout over TFTP is
> real on those cards.
>
> On **earlier** bitstreams it is not, for a reason outside marquee: that BIOS
> fetches `boot.bin` *before DHCP*, using the gateware's own IP and MAC — both
> compile-time constants shared by every card built from that bitstream, so
> every card asks from the same address. The resolver says so loudly
> (`not identified (arp=…); serving default`) rather than silently serving the
> default, and the ARP fallback in `_mac_for_ip()` exists for exactly them.
> For those cards staged rollout is still a **flashing** operation.

### Boot precedence

A card's firmware may carry a config baked in at build time. The order is:

1. the TFTP config marquee serves — **always wins when it answers**
2. the card's compiled-in default, applied before its network is even up
3. a single card at the bitstream's geometry, expecting a stream

So a standalone panel is not a special case that escapes management: point it at
marquee and marquee takes over. The baked config only covers the gap before
marquee answers, and the case where it never does.

---

## 8. Live data — one cache, two kinds of writer

`LiveState` in `marquee_core/live.py` is the whole live-data plane: the last
payload per **topic**, in memory. Bindings read it — `{data:mnr.harlem|...}` —
and the render path therefore makes no network call.

Two things write into it, and **nothing downstream can tell them apart**:

| writer | is | configured by |
|---|---|---|
| `BusClient` | a websocket to a **panel-bus** that fans out feeds | `MARQUEE_BUS_URL` |
| `ProviderHost` | **data providers** — plug-in Python, one thread each | files in a directory |

That indistinguishability is the point. A provider did not need a new binding, a
new layer type or a preview path: it publishes a topic, and `{data:...}`,
`sparkline` and `ValuePusher` already read topics. The cost of adding the
plug-in plane was a writer, not a stack.

> **`{bus:...}` and `{data:...}` are the same read.** `bus` is the older
> spelling, from when the panel-bus was the only writer. Naming a binding after
> a transport was wrong once there were two, but shows in the field use it, so
> both resolve identically and neither is going away.

`LiveState.put` records which writer owns each topic. Two writers on one topic
is a misconfiguration whose symptom is a value *flickering between two
plausible readings* — close to undiagnosable from the sign — so it is a log
line and an API field instead.

### 8a. Data providers

`marquee_core/providers.py`. A provider declares a `name`, an `interval_s` and
a `poll()` returning `{topic: payload}`; `ProviderHost` discovers, configures
and runs it. `doc/providers.md` is the user-facing contract.

Loaded from directories, later winning: `custom/example/providers` (shipped
samples), `custom/<site>/providers` (yours, version-controlled beside your
shows), `/data/providers` (drop-in, no rebuild). This is the `custom/`
extension point the public-release plan names, used for the second time — and
the shipped providers are the maintainers using it themselves, which is the
only way an extension point stays honest.

A file named `_*.py` is skipped by discovery, and the plugin's own directory is
on `sys.path` while it imports, so providers can share a helper module rather
than copying it. `sys.path` is **appended** to: a plugin directory holding
`json.py` must not become the `json` module for the player.

**Config is settings-only** (`provider.<name>.config`, JSON), merged over the
class's `DEFAULTS`. A file beside the plugin was the obvious alternative and is
the trap §6 warns about: a second place the same setting can live, and drift.

**Failure is contained at every level**, because the rule is the one bindings
follow — a missing integration must never blank a sign:

- a file that will not import is skipped, loudly; the others still load;
- `poll()` raising is caught, counted, and **the last good payload stays**;
- repeated failure backs off geometrically to a five-minute ceiling.

> A provider is **trusted code** running in the player process. There is no
> sandbox and the plane does not pretend to offer one.

**Both the player and the api run a host.** The api's exists so a *preview*
renders from the same cache a wall does — the preview being the same code path
is what makes it trustworthy, and that is only true if `{data:...}` resolves on
both sides. The cost is two clients on each upstream.

### 8b. The panel-bus

A panel-bus holds ONE upstream connection per feed and fans it out over a
websocket, publishing topics such as `home.status`. Unset, that half of the
plane is off and the rest of the system is unaffected.

Two reasons this beats Marquee talking to each upstream itself:

- **One integration**, one set of credentials, one place to fix a broken feed.
- **The render path never touches the network.** A sign redrawing 20 times a
  second must read a value from memory, not make a request.

The bus replays the last snapshot the moment a client subscribes, so a freshly
started player is current immediately rather than blank until the next event.

Home Assistant is separate: `ha.base_url` + `ha.token` in settings, with
`ha_reader` (5 s cache) and `ha_history_reader` (its own, much longer cache — a
history query is far heavier and stays useful far longer).

> **A missing integration must never blank a sign.** With no HA configured,
> `{ha:...}` renders the no-value glyph and graphs draw just their baseline. The
> rest of the scene is unaffected.

---

## 9. Key files

| Path | Purpose |
|---|---|
| `packages/marquee_core/scene.py` | the scene model — parses and validates shows |
| `packages/marquee_core/render.py` | the compositor: `(show, t) → frame` |
| **`packages/marquee_core/stream.py`** | **the wire protocol — must match the firmware** |
| `packages/marquee_core/models.py` | SQLAlchemy models (**`Wall`, `Card`**), `resolve_show_for` |
| `packages/marquee_core/bindings.py` | `{clock:}`, `{ha:}`, `{bus:}`, `{file:}` … |
| `packages/marquee_core/live.py` | the topic cache, and the panel-bus subscriber |
| **`packages/marquee_core/providers.py`** | **the data-provider plug-in plane** |
| `packages/marquee_core/sources.py` | media resolution: plex, youtube, cam, wowza |
| `packages/marquee_core/poller.py` | samples a card's counters |
| `services/player/main.py` | entrypoint: bus, TFTP, value push, supervisor |
| `services/player/player.py` | the per-**wall** render thread, per-card crop + stream, DMA re-assert |
| `services/player/valuepush.py` | values → program-mode cards |
| `services/tftp/tftpd.py` | boot server, port 6969 |
| `services/api/app/routers/fleet.py` | walls, cards, shows, schedules, programs |
| `services/api/alembic/` | migrations — **one head** |
| `services/api/app/routers/providers.py` | the provider catalogue and topic inspector |
| `custom/example/providers/` | three worked providers over two upstreams |
| `doc/scenes.md` | the scene language (user-facing contract) |
| `doc/providers.md` | how to write a data provider (user-facing contract) |

---

## 10. Working here

- **Read [doc/scenes.md](doc/scenes.md) first** if you are touching rendering.
  The scene language is the app's contract with its users; changes to it break
  people's shows.
- **Never copy a model out of `marquee_core`.** Import it.
- **`stream.py` and the firmware's `bitmap_udp.rs` are one protocol in two
  repos.** Change them together or not at all.
- **TFTP is 6969, not 69.**
- Build with `make up-build`, never raw docker — images bake source at build
  time, so `docker restart` runs old code.
- Never `git add -A` blindly; check `git status` first.
