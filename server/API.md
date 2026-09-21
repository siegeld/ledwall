# Marquee HTTP API

Base: `/api/v1`. Health is at `/healthz` (unauthenticated).

## Integration — put content on a wall

The endpoint other homelab apps call.

    curl -X POST http://the build host:8410/api/v1/play \
      -H 'Content-Type: application/json' \
      -d '{"wall":"lobby","show":"evening-loop"}'

`panel` is still accepted as a deprecated alias for `wall` and resolves by wall
name first, then by card name — so callers written before walls existed keep
working. The response echoes both keys for the same reason.

Or inline, without saving a show:

    curl -X POST http://the build host:8410/api/v1/play -H 'Content-Type: application/json' -d '{
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
| PUT | `/walls/{id}/show/{show_id}` | point a content source at a wall |

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
| GET | `/panels` | **deprecated** alias for `/cards`, kept so older callers do not 404 |

`x` and `y` place the card's top-left corner within its wall; a single-card wall
is `0, 0`. The card is sent exactly `(x, y, x+width, y+height)` of the wall
canvas.

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
