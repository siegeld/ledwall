# Data providers — plugging in your own live data

A **provider** is a small Python file that fetches something and publishes it
onto named **topics**. Marquee runs it on a thread, caches what it returns, and
shows read it with `{data:...}` bindings:

```yaml
- {type: text, text: "{data:mnr.harlem|departures.0.destination}", box: [0,0,128,16]}
```

Drop the file in a directory and restart. There is no registration step, no
schema to declare, and nothing to rebuild.

```
custom/
  example/providers/       shipped examples — three rail boards
  <your-site>/providers/   yours, version-controlled beside your shows
data/providers/            drop-in: a mounted volume, no image rebuild
```

A file whose name starts with `_` is a **helper**, not a provider: it is not
loaded as one, and the providers beside it can import it. See *Sharing code
between providers* below.

Later directories win, so a file named `metro_north.py` in your site directory
replaces the shipped one rather than colliding with it.

> **Providers are not `src:` sources.** `sources.py` resolves a layer's `src:`
> to something ffmpeg can open (`plex:`, `cam:`, `youtube:`). A provider
> supplies *values*. The two never meet.

---

## The whole contract

```python
from marquee_core.providers import Provider

class Tides(Provider):
    name = "tides"                       # topic namespace and identity
    interval_s = 900.0                   # how often poll() is called
    DEFAULTS = {"station": "8518750"}    # config, overridable from settings

    def topics(self):
        return ["tides.next"]            # advisory: what the catalogue shows

    def poll(self):
        import json, urllib.request
        url = f"https://api.example.gov/tides?station={self.config['station']}"
        with urllib.request.urlopen(url, timeout=10) as fh:
            doc = json.load(fh)
        return {"tides.next": {"high": doc["next_high"], "low": doc["next_low"]}}
```

That is the entire interface. `poll()` returns `{topic: payload}` and the
payload is anything JSON-shaped — dicts, lists, strings, numbers.

| member | required | meaning |
|---|---|---|
| `name` | **yes** | topic namespace, settings key, and identity. Unique across loaded providers. |
| `poll()` | yes* | fetch once, return `{topic: payload}`. Return `{}` to publish nothing without it counting as a failure. |
| `interval_s` | no (60) | seconds between polls |
| `DEFAULTS` | no | config defaults, merged under the settings value |
| `topics()` | no | what to advertise in the catalogue |
| `run(publish, stop)` | no | override *instead of* `poll` to own the loop — for a real push feed |
| `PROGRAMS` | no | card programs this provider can feed — see below |
| `board_pages(live)` | no | value sets for a program-mode card, one per page |

\* unless you override `run()`.

---

## Reading it from a show

A topic's payload is addressed by a dotted path, and numeric segments index
lists:

| binding | reads |
|---|---|
| `{data:tides.next}` | the whole payload, stringified |
| `{data:tides.next\|high}` | one field |
| `{data:mnr.harlem\|departures.0.time}` | the first list entry's field |
| `{type: sparkline, data: "power.house\|history"}` | a list, as a chart |

`{bus:...}` is the same read against the same cache. It is the older spelling,
from when the only publisher was the panel-bus; `{data:...}` is the one to use
now. Both keep working — see [scenes.md](scenes.md).

**A missing value renders `—`, never an error.** A topic that has not arrived
yet, a path that does not exist, a provider that is failing: all of them draw
the no-value glyph and leave the rest of the scene alone.

---

## Configuration

Config lives in the `settings` table under one key per provider, as JSON:

| key | example | effect |
|---|---|---|
| `provider.<name>.config` | `{"line": "Hudson", "rows": 6}` | merged over `DEFAULTS` |
| `provider.<name>.enabled` | `false` | stops it running |

```bash
curl -X PUT .../api/v1/settings/provider.metro-north.config \
     -d '{"value": "{\"line\": \"Hudson\"}"}'
```

**Providers are opt-out, not opt-in.** A file is only in that directory because
somebody put it there; making them then flip a second setting produces plugins
that silently do nothing. Ship `DEFAULTS` that work with no configuration at
all — an example that needs setup before it does anything is a worse example.

Malformed JSON in a config key is logged and ignored, not fatal.

---

## What the plane guarantees

**A broken provider never takes anything down.** This is the same rule bindings
follow, for the same reason: a sign that goes black because a weather API timed
out is worse than one showing a stale value.

| goes wrong | happens |
|---|---|
| syntax error in the file | logged, that file skipped, others load |
| imports a package that is not installed | logged, skipped, others load |
| `poll()` raises | logged, counted, **the last good payload stays in the cache** |
| keeps raising | geometric backoff, 2× per failure, capped at 5 minutes |
| publishes a topic another writer owns | logged by name — see below |

That fourth row is the one people hit. A provider retrying a dead upstream
every 30 seconds forever is how one broken plugin becomes a log nobody reads
and a rate limit somebody notices.

The fifth is worth knowing about because of how it presents: two writers on one
topic show up as a value *flickering between two plausible readings*, which is
close to undiagnosable from the sign. The cache records which writer last wrote
each topic and says so in the log and in `GET /api/v1/providers`.

---

## Finding out what you can bind to

```
GET /api/v1/providers                       every provider, its health, its topics
GET /api/v1/providers/topics/{topic}        the live payload
GET /api/v1/providers/topics/{topic}?path=departures.0
```

The second one answers the question that actually blocks you — knowing
`mnr.harlem` exists does not tell you the path is `departures.0.destination`.
It returns the document, and echoes back the exact binding string to paste into
a show.

---

## Things worth knowing before you write one

**Polling here does not break the "push, never poll" rule.** That rule is about
the *render path*, which must read memory rather than make a request, and it
still does — a provider runs on its own thread and a sign redrawing 20 times a
second touches no network. If your upstream has a real push feed, override
`run()` and hold the subscription instead.

**Pick `interval_s` from how fast the data actually changes.** Polling faster
than the upstream updates spends someone's rate limit on identical bytes. A
train feed is 30s; an air-quality feed is 15 minutes.

**Shape the payload for the show, not for the upstream.** A binding is a dotted
path and nothing else — the scene language has no expressions, deliberately, so
anything conditional has to be decided by you. `metro_north.py` publishes a
`countdown` field that reads `"Now"`, `"7m"` or `""` precisely because "blank
it beyond an hour" cannot be written in a binding. Publish the raw value too.

**Cache anything slow that changes slowly.** `self.cache_path("x.json")` gives
you a per-provider directory under `/data`. The Metro-North provider reduces a
5.8 MB timetable zip to a few kB of JSON and refreshes it weekly, rather than
re-fetching it every 30 seconds to learn that stop 1 is still Grand Central.

**Prefer the standard library.** A provider that needs a `pip install` is no
longer something you can drop into a directory. If you do need a package, it
has to go into the player and api images, and until it does the provider is
skipped with a log line rather than breaking anything.

**A provider is trusted code.** It is imported into the player process and can
do anything that process can. There is no sandbox here and this plane does not
pretend to offer one — treat dropping a file in that directory exactly like
installing a plugin anywhere else.

**It runs in two processes.** The player renders walls; the api renders
previews, and the preview is deliberately the *same code path* so that what an
operator previews is what the card shows. Both therefore run the provider, and
your upstream sees two clients.

---

## Feeding a card that runs its own program

A card in **`mode: program`** is never sent a frame. It composes its own pixels
from named values — a few hundred bytes a second against the ~24 Mbit/s a
streamed card eats — and it keeps showing the last ones when the host dies,
marked stale. A provider can supply those values:

```python
class Tides(Provider):
    name = "tides"
    PROGRAMS = ("tideboard",)          # card programs this provider can feed

    def board_pages(self, live):
        doc = live.get(self.topic)     # you know your own topic
        if not doc:
            return []                  # nothing fetched yet is not an error
        return [
            {"hdr": "High water", "r0a": doc["next_high"], "r0b": doc["high_ft"]},
            {"hdr": "Low water",  "r0a": doc["next_low"],  "r0b": doc["low_ft"]},
        ]
```

`PROGRAMS` is the match: the pusher reads which program a card runs from
`cards.program` and sends the pages of the providers that declare it. The MODE
still comes from asking the card — that is the setting that must not be
configured twice — but the program NAME is marquee's own, served over TFTP, so
the database is the source of truth.

### Three things the card imposes

**The value table holds 24 keys**, and `set()` **ignores** the 25th rather than
evicting it. Nothing raises: one column simply stays blank on the glass with
nothing in any log to say why. Keys are 16 bytes, values 64. This is why values
are selected by program rather than merged from every source.

**Send every key the program binds, every time.** A key you leave out keeps its
previous value on the card, so a short list leaves the last page's train sitting
under this page's heading.

**An empty string is indistinguishable from a key that never arrived** — the
card renders `-` for both, deliberately, because a program must never have to
decide what *absent* looks like. But an empty row on a departure board is not
absent, it is "no train": an assertion the host is making. Push a space.

### The rotation belongs to the host

The obvious design is to cycle on the card's own frame counter, and it does not
work. Rows outside a program's `dynamic` band are repainted only **when a value
arrives**, which is what makes a mostly-static board nearly free; a card cycling
on its own would have to redraw the whole canvas every frame, which measures
under 1 fps on this hardware. So the page is a pushed value, and
`board_pages()` returns the pages for the pusher to rotate through.

What that costs is honest: if marquee dies the rotation stops. The board stays
lit with its last values and the stale marker appears — which is the thing a
streamed card structurally cannot do, since it freezes on a frame that still
looks correct forever.

### Shape the values for the card, not for yourself

The same rule as the show payload, with sharper teeth, because the card cannot
measure or wrap anything cheaply:

- **The card decides to scroll by measuring the string it is given.** So
  padding a name IS the instruction to scroll — and the padding has to carry
  its own gap, because the scroll wraps and an unpadded name runs into itself.
- **Do not pad to a power of two** without checking. It lets the card use a
  mask instead of a division, but a name padded that way is mostly gap:
  "North White Plains" is 18 characters and rounds to 32, dragging 112px of
  blank past a 56px window. The column then reads as empty for most of the page
  it is on, which is a worse bug than a divide is a cost.

`custom/example/providers/_board.py` does all of this, and
`colorlight/custom/example/panels/railboard.yaml` is the program it feeds.

---

## Sharing code between providers

A file whose name starts with `_` is **not** loaded as a provider, and the
plugin's own directory is on `sys.path` while it imports — so two providers
over one upstream can share machinery instead of copying it:

```
custom/example/providers/
  _board.py        presentation policy: how a time is written, when a
                   countdown blanks          (not a provider)
  _gtfs_rail.py    GTFS-realtime protobuf + static GTFS
                   (not a provider)
  metro_north.py   \_ two configurations of _gtfs_rail
  lirr.py          /
  amtrak.py        a JSON API — shares _board only
```

`sys.path` is **appended** to, not prepended, so the standard library always
wins: a plugin directory containing `json.py` must not become the `json` module
for the whole player. That leaves helper modules sharing one namespace across
plugin directories, so give them distinct names — `_gtfs_rail`, not `_util`.

**Share presentation, not parsing.** What a viewer notices is the part that
must not differ between boards in one rotation — one reading `Now` while the
next reads `0m` is the sort of thing nobody files a bug for and everybody sees.
Parsing is the opposite: a protobuf feed and a JSON API have nothing in common
below the payload, and a base class that tried to unify them would be a pile of
flags.

---

## The worked examples

Three ship, over **two unrelated upstreams**, and the third is the one that
explains the first two.

| Provider | Upstream | Topic |
|---|---|---|
| `metro_north.py` | MTA GTFS-realtime (protobuf) | `mnr.harlem` |
| `lirr.py` | the same feed format, different system | `lirr.babylon-branch` |
| `amtrak.py` | a JSON API — Amtrak publishes no public GTFS feed | `amtrak.nyp-bos` |

All three publish the **same payload shape** — `departures`, `arrivals`,
`trains`, `counts` — so the three shipped shows are the same board at the same
column widths with the topic changed. That portability is a convention the
providers keep, not something the plane enforces, and it is the clearest
statement of what a provider is for: absorbing the difference between upstreams
so the show does not have to.

### Why two GTFS examples rather than one

Because **the two MTA feeds use opposite identity conventions**, and a single
example hides that:

| | Metro-North | LIRR |
|---|---|---|
| join trip updates to vehicles on `FeedEntity.id` | **203 / 203** | **0 / 53** |
| join on `trip.trip_id` | **0 / 203** | **53 / 53** |

Both measured live. The first version of the Metro-North provider joined on
`trip_id`, matched nothing, and silently reported every train as *scheduled*;
fixing it to the entity id was right for MNR and would have been exactly wrong
for LIRR. `_gtfs_rail` now indexes vehicles under every identifier they carry,
which is simply correct on both rather than a flag to get wrong.

A second per-system difference fell out of the same work: LIRR's realtime trip
ids *are* static timetable ids, so `trips.txt` resolves a train number for the
half of its feed with no vehicle yet (107/107, 56 kB of cache). On Metro-North
the same table resolves **0 of 201** and costs 1750 kB. So it is opt-in per
system — `TRIP_NAMES` — rather than always on.

### Why the third shares almost nothing

Amtrak has no public GTFS-realtime feed; its own endpoint returns an encrypted
blob. `amtrak.py` reads a community JSON API instead and shares only
`_board.py`. It also shows what a *richer* upstream buys: scheduled and actual
times per stop, so lateness is a subtraction rather than a guess, and a
`platform` — which is usually blank, correctly, because Amtrak assigns a track
at the last minute and the feed says so.

It polls every **two minutes**, not thirty seconds: the response is the whole
national network, about 1 MB, and there is no endpoint that returns one
corridor. Intercity trains do not move between stations in thirty seconds.

All three read their feeds with no third-party packages — the GTFS pair walks
protobuf in about forty lines of standard library, with the reasoning for why
that is safe written at the top of `_gtfs_rail.py`. Worth reading before you
reach for a dependency.
