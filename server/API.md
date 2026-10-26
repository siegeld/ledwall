# Marquee HTTP API

Base: `/api/v1`. Health is at `/healthz` (unauthenticated).

**Authentication.** The fleet, show, firmware, bitstream, preview and provider
endpoints below are **open on the LAN** -- marquee is meant to sit on a trusted
network next to the cards it drives. The admin endpoints (settings, API tokens;
see the end of this page) need an admin principal: a session cookie from
`POST /auth/login`, or `Authorization: Bearer mrq_...` with a token carrying the
`admin` scope. Put the API behind your reverse proxy's access control if the
network is not trusted.

## Integration — put content on a wall

The endpoint other homelab apps call.

    curl -X POST http://SENDER:8410/api/v1/play \
      -H 'Content-Type: application/json' \
      -d '{"wall":"lobby","show":"evening-loop"}'

`panel` is still accepted as a deprecated alias for `wall` and resolves by wall
name first, then by card name — so callers written before walls existed keep
working. The response echoes both keys for the same reason.

Or inline, without saving a show:

    curl -X POST http://SENDER:8410/api/v1/play -H 'Content-Type: application/json' -d '{
      "wall": "lobby",
      "body": "display: {width: 256, height: 128}\nscenes:\n  - name: alert\n    duration: 30s\n    layers:\n      - {type: solid, color: \"#7c3aed\", box: [0,0,256,128]}\n      - {type: text, text: \"DOORBELL\", box: [0,40,256,48], size: 36, align: center}\n"
    }'

## Walls

A **wall** is what a viewer sees: one logical display with one picture. Content
is assigned here, not to a card. See `ARCH.md` §2a.

| Method | Path | Purpose |
|---|---|---|
| GET | `/walls` | list, with resolved current show, card ids, covered pixels |
| POST | `/walls` | create — `name`, `width`, `height` |
| PATCH | `/walls/{id}` | update — **partial body** |
| DELETE | `/walls/{id}` | remove, **cascading to its cards' records** |
| GET | `/walls/{id}/cards` | the cards covering this wall |
| GET | `/walls/{id}/layout` | geometry plus any gaps, overlaps or overhangs |
| PUT | `/walls/{id}/show/{show_id}` | point a show at a wall (its standing assignment) |
| GET | `/walls/{id}/content` | everything the wall can show: its shows, and the on-board programs **every** card on it carries |
| PUT | `/walls/{id}/content` | choose content **and let the mode follow**: `{"kind":"show","show_id":N}` puts every card in `stream`; `{"kind":"program","name":"..."}` puts every card in `program` running it |
| POST | `/walls/{id}/mode` | `{"mode":"stream"\|"program"}` for every card on the wall; reports cards that did not take it as `unreachable` |

**A wall's pixel size must match what its cards cover**, and a show's boxes
are drawn at the wall's size: integer boxes are absolute pixels, so a show
authored for a smaller wall leaves the rest black (doc/scenes.md, "One show,
many walls"). Changing `width`/`height` here does not reconfigure any card --
see `PATCH /cards/{id}` below.

`/walls/{id}/layout` reports problems rather than rejecting them — an operator
mid-edit will briefly have a gap or an overlap:

```json
{"wall": {"id": 1, "name": "lobby", "width": 768, "height": 384},
 "cards": [{"id": 1, "name": "left", "box": [0, 0, 384, 384], "enabled": true}],
 "covered": 147456,
 "problems": [{"kind": "gap", "detail": "147456 px not covered by any card"}],
 "ok": false}
```

## Cards

A **card** is one receiver board driving a rectangle of a wall.

| Method | Path | Purpose |
|---|---|---|
| GET | `/cards?wall_id=` | list, with each card's wall and position |
| POST | `/cards` | register a card — needs `wall_id`, and `x`/`y` within it |
| PATCH | `/cards/{id}` | update — **partial body**, send only the fields you are changing |
| DELETE | `/cards/{id}` | remove |
| GET | `/cards/{id}/programs` | on-board programs this card's firmware carries, and which is running |
| GET | `/cards/{id}/stats?hours=24` | history: fps, refresh Hz, errors, reboots, availability |
| GET | `/cards/{id}/bootconfig` | exactly the `<mac>.yml` this card is served at boot, rendered by the same code the TFTP server uses |
| GET | `/panel-types` | module types a card can carry (`panel_type`): geometry, RGB group arrangement, driver chip, bitstream preset |
| GET | `/drive-types` | driver chips a card can be told to drive (`drive`); empty = the bitstream's own |
| GET | `/rgb-orders` | channel orders a module can expect (`rgb_order`); the index is the gateware CSR value |
| GET | `/panels` | **deprecated** alias for `/cards`, kept so older callers do not 404 |

`x` and `y` place the card's top-left corner within its wall; a single-card wall
is `0, 0`. The card is sent exactly `(x, y, x+width, y+height)` of the wall
canvas.

**Geometry reaches a card only at boot.** `layout_yaml`, `panel_type`,
`drive`, `rgb_order`, `mode` and `program` are served in the card's TFTP boot
config, so a `PATCH` that changes any of them takes effect at the card's next
boot -- reboot it (`POST /cards/{id}/firmware` with its current image and
`"reboot": true`) or power-cycle it. `width`/`height` are not sent to the card:
they are the size of the crop marquee streams to it, and must equal what the
layout makes the card (grid x module size). A four-output card driving four
stacked 256x128 modules is:

```bash
curl -X PATCH .../walls/2 -H 'Content-Type: application/json' -d '{"width":256,"height":512}'
curl -X PATCH .../cards/2 -H 'Content-Type: application/json' -d '{
  "width": 256, "height": 512, "packet_delay": 0.00015,
  "layout_yaml": "grid: 1x4\nJ1: 0,0\nJ2: 0,1\nJ3: 0,2\nJ4: 0,3\n"}'
```

`packet_delay` is the pacing between pixel packets, in seconds. Size it so
`chunks per frame x packet_delay` is about 80% of the frame period: 0.0003 for
a 135-chunk 256x256 frame at 20 fps, 0.00015 for a 270-chunk 256x512 one
(ARCH.md §4).

### Card mode

A card is either **streamed** to or runs an **on-board program**. Never both:
each writes the framebuffer, so running both corrupts the picture rather than
one winning. On a multi-card wall this is per card — one board can run a program
while its neighbours stream.

| Field | Values | Meaning |
|---|---|---|
| `mode` | `stream` (default) \| `program` | what drives the card |
| `program` | a program name | which on-board program, when `mode: program` |

```bash
curl -X PATCH .../cards/1 -d '{"mode":"program","program":"dashboard"}'
```

Both are served to the card in its TFTP config, so the choice survives a
reboot; the card applies it at boot with no HTTP call. The player does not
start a stream for a `program` card, and `GET /cards/{id}/programs` asks the
CARD what it can run rather than keeping a copy here — one firmware carries
every program, so the list belongs to the build on that board.

A program-mode card is fed **values**, not pixels: `services/player/valuepush.py`
maps panel-bus fields to keys and pushes them, a few hundred bytes a second
against the ~24 Mbit/s a streamed card needs for a static clock face. Writing
the programs themselves is documented in the colorlight repo
(`docs/ON-PANEL-PROGRAMS.md`).

## Firmware and OTA rollout

A card netboots its firmware from marquee on every boot, so updating one needs
no JTAG and no hands. These endpoints are the place to put images, the record
of which card runs which, and a rollout that checks the card came back.

**They do not touch the card's SPI flash.** Flash holds the bitstream and a
survival copy of the firmware; a bad bitstream is recoverable only over JTAG,
which is the dependency we are removing. So "rollout" here means serve it and
reboot — safe, because a card that fails to boot netboots again and can be
pointed back at the previous image.

| Method | Path | Does |
| --- | --- | --- |
| GET | `/api/v1/firmware` | every image, with size, sha256, and which cards use it |
| GET | `/api/v1/firmware/running` | per card: the image ASSIGNED vs the version actually RUNNING |
| POST | `/api/v1/firmware` | upload an image (multipart `file`). Points nothing at it |
| DELETE | `/api/v1/firmware/{name}` | remove one. Refuses the fleet default and any image still assigned |
| POST | `/api/v1/cards/{id}/firmware` | assign to one card and reboot into it, verifying it returns |
| POST | `/api/v1/firmware/rollout` | roll across a wall or the fleet, one card at a time |

### Bitstreams — the FPGA image in the card's flash

A **different artifact** from firmware, on a different path. Firmware is
refetched over TFTP on every boot and never stored on the card; the bitstream
lives in SPI flash and is live only after a **power cycle**.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v1/bitstreams` | staged images, with size and which cards were pushed them |
| POST | `/api/v1/bitstreams` | upload a `.bit` (multipart `file`). The ASCII header is stripped and the ECP5 preamble validated; a file that is not a bitstream is refused here |
| DELETE | `/api/v1/bitstreams/{name}` | remove one. Refuses an image still assigned |
| GET | `/api/v1/cards/{id}/bitstream` | what the card reports: flash state, golden present, update in flight |
| POST | `/api/v1/cards/{id}/bitstream` | write it into the card's flash and wait |

```sh
curl -F file=@bitstreams/e320/128x64.bit http://marquee/api/v1/bitstreams
curl -X POST http://marquee/api/v1/cards/2/bitstream \
     -H 'Content-Type: application/json' -d '{"bitstream":"128x64.bit"}'
```

- **No reboot, and that is deliberate.** Writing flash does not reconfigure the
  FPGA, so rebooting would restart only the firmware while leaving an operator
  believing the new gateware was live. It takes effect at the next power cycle.
- **No rollback, and that is also deliberate.** A golden bitstream is written
  once over JTAG and never again; if a pushed image fails to configure the ECP5
  falls back to it, so the card returns on the network to accept another.
- **A card with no golden image is refused**, with an explanation — without a
  recovery image a failed write is the one thing here that really needs a cable.
- A push takes minutes: every 4 KB is a sector erase, and the card keeps running
  its current gateware throughout. The response when it is done:
  `{"card":"e320","bitstream":"e320-256x128-dp3264s-hub320-out4-v1.46.0.bit","written":567413,"sectors":139,"state":"done","note":"written to flash; it becomes live at the next power cycle. Nothing reconfigures the FPGA from software."}`
- **Names are identities**: `<board>-<panel>-out<n>-v<semver>.bit`, enforced at
  upload, because the fleet view compares what was pushed against the
  identifier the running gateware reports about itself.

**Gateware and firmware as a pair.** When a gateware change moves the CSR map,
the firmware must move with it -- the firmware addresses registers by number.
The order that gets a card onto both with **one** boot:

1. upload both (`POST /firmware`, `POST /bitstreams`);
2. pin the firmware **without** rebooting (`"reboot": false`, above);
3. `POST /cards/{id}/bitstream` and wait for `"state": "done"`;
4. power-cycle the card;
5. verify on the card: its `/api/status` `gateware` string and `version`, or
   `GET /firmware/running`.

When the CSR map did not change (compare the build's `csr.csv` with the
previous one), either half can go first.

```bash
# stage a build
curl -F file=@.tftp/boot.bin http://marquee/api/v1/firmware

# put it on one card and wait for it to come back
curl -X POST http://marquee/api/v1/cards/1/firmware \
     -H 'Content-Type: application/json' \
     -d '{"firmware":"boot.bin","reboot":true,"verify_s":90}'

# or PIN it without rebooting: the card picks it up at its next boot. The
# right form when a bitstream push and a power cycle are coming anyway, so
# the card goes through one boot instead of two.
curl -X POST http://marquee/api/v1/cards/1/firmware \
     -H 'Content-Type: application/json' \
     -d '{"firmware":"boot.bin","reboot":false}'
# -> {"card":"...","previous":"...","firmware":"boot.bin","rebooted":false,
#     "back":null,"status":null}

# roll it across a wall, halting if a card does not return
curl -X POST http://marquee/api/v1/firmware/rollout \
     -H 'Content-Type: application/json' \
     -d '{"firmware":"boot.bin","wall_id":1}'
```

Things worth knowing:

- **Upload never changes what the fleet runs.** Assignment is a separate call,
  so staging a build cannot surprise you.
- **Rollout is serial and halts by default.** The failure that matters is an
  image that boots nowhere, and pushing that to twenty cards in parallel turns
  a one-card problem into a dark wall. Pass `stop_on_failure=false` only when
  a card is already known down.
- **No automatic rollback.** A card that does not return is still netbooting
  on every power cycle, so recovery is to reassign and reboot. Rolling back
  automatically would hide a firmware that crashes *late*, which is worse than
  a card that is visibly down.
- **Assigned is not running.** `/firmware/running` reports both, because they
  differ exactly when it matters: an assignment nothing has rebooted into, or a
  card that fell back to the default image. `version` comes from the card's own
  `/api/status` and needs colorlight **2.15.0 or newer**; an older image answers
  without it, so `version: null` with `online: true` means "running something
  older", not "unreachable".
- **A reboot is verified by watching the card go DOWN first.** Polling straight
  for "does it answer" reported success instantly, because the old firmware
  kept serving HTTP between accepting `/api/reboot` and actually restarting --
  so `back: true` could mean "never rebooted" and a push that had not taken
  effect looked verified. The response now carries `was_running`, `now_running`
  and `went_down`; `went_down: false` with `back: true` means the card never
  stopped answering and may still be on the old image.
- **A card marquee does not know still boots**, on the fleet default, logging
  `not identified ... serving default firmware`. A successful netboot is not
  evidence a card is registered.

## Shows

| Method | Path | Purpose |
|---|---|---|
| GET | `/shows` | list; each carries `walls[]`, the walls it plays on |
| POST | `/shows` | create (validated) |
| PUT | `/shows/{id}` | update (validated) |
| POST | `/shows/validate` | check a document without saving |
| GET | `/shows/files` | which shows differ from their files, and which way round |
| POST | `/shows/export` | database → files (what `make shows-export` does) |
| POST | `/shows/import` | files → database (what `make shows-import` does) |

### Shows on disk

Files under `custom/<site>/shows/` are the source of truth; the database is what
the running system reads. **Nothing keeps them in step.** A UI edit lives only in
the database until an export, and a file edit does not reach a wall until an
import — so both directions are exposed here, not just in the Makefile.

`GET /shows/files` returns a `state` per show:

| `state` | meaning |
|---|---|
| `in-sync` | the file round-trips to what the database holds |
| `db-newer` | edited in the UI and not exported — the file is stale |
| `file-newer` | the file was edited and not imported — the wall has not seen it |
| `no-file` | never exported; exists only in the database |

plus `orphans[]`, files on disk with no matching row — invisible in the UI
otherwise, which looks like the show simply does not exist.

The directory comes from `MARQUEE_SITE` (default `dashboard`), mirroring the
Makefile's `SITE`.

### Preview

| Method | Path | Purpose |
|---|---|---|
| POST | `/preview.png` | `{"body": "<show yaml>", "t": 12.5, "scale": 3, "width": 256, "height": 512}` -> one frame as PNG, nearest-neighbour scaled so the pixel grid is visible |

Render problems come back in the `X-Render-Errors` header rather than failing
the request. **`width`/`height` are the WALL's size** (8-4096, both or
neither): pass them and the frame is rendered exactly as the player renders it
for that wall -- integer boxes absolute, the rest black. Omit them and it
renders at the show's declared `display` size (256x128 when it declares none),
which for a show sized differently from its wall is not the wall's picture
(ARCH.md §3). The editor sends the size of the first wall the show is live on.

## Schedules (dayparting)

| Method | Path | Purpose |
|---|---|---|
| GET | `/schedules?wall_id=` | list |
| POST | `/schedules` | create |
| DELETE | `/schedules/{id}` | remove |

`start_min`/`end_min` are minutes past local midnight; `end < start` wraps
midnight. `days` is a string of weekday digits, `0` = Monday. Highest
`priority` covering window wins; otherwise the wall's standing assignment.

## Data providers

| Method | Path | Purpose |
|---|---|---|
| GET | `/providers` | every loaded provider, its health, and every cached topic |
| GET | `/providers/topics/{topic}` | the live payload for one topic |
| GET | `/providers/topics/{topic}?path=a.0.b` | dig into it with a binding path |

A **data provider** is a plug-in that publishes live data onto named topics,
which shows read with `{data:topic|path}`. See
[`doc/providers.md`](doc/providers.md) for writing one.

These two endpoints exist to answer the question that blocks a show author:
`/providers` says which topics exist, and `/providers/topics/{topic}` shows what
one actually contains — knowing `mnr.harlem` is there does not tell you the path
is `departures.0.destination`. The response echoes the exact binding string:

```json
{
  "topic": "mnr.harlem",
  "path": "departures.0.destination",
  "owner": "provider:metro-north",
  "age_s": 12.4,
  "binding": "{data:mnr.harlem|departures.0.destination}",
  "value": "Southeast"
}
```

`owner` is which writer last wrote the topic — `bus`, or `provider:<name>`. Two
writers on one topic present as a value flickering between two plausible
readings, so it is reported rather than left to be discovered from the sign.

Provider config lives in settings, as JSON, one key per provider:
`provider.<name>.config` and `provider.<name>.enabled`.

## Sessions, settings and API tokens

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/auth/login` | -- | `{"username","password","remember"}` -> session cookie. Local accounts, or Active Directory when `ad_*` settings are present |
| POST | `/auth/logout` | -- | clears the cookie |
| GET | `/auth/me` | session or token | who you are |
| GET | `/api/v1/settings` | admin | every setting; values whose key contains `password` or `secret` come back masked |
| PUT | `/api/v1/settings` | admin | `{"key","value"}` -- HA credentials, cameras, provider config |
| GET | `/api/v1/tokens` | admin | API tokens: name, prefix, scopes, created, last used, revoked |
| POST | `/api/v1/tokens` | admin | `{"name","scopes","expires_days"}` (`scopes` a string, default `"read"`; include `admin` for admin rights) -> the raw `mrq_...` token, **shown once**; only its hash is stored |
| DELETE | `/api/v1/tokens/{id}` | admin | revoke |

Note the session routes are at `/auth/...`, not under `/api/v1`. A local
`admin` account is seeded on first boot from `MARQUEE_ADMIN_PASSWORD`.
