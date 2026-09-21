# Changelog

## [0.28.2] - 2026-09-21

### Fixed

**A documented example could not be pasted.** The `board_pages` example in
`doc/providers.md` elided a dict with a bare `...`, which reads fine and is a
`SyntaxError` when run. It now returns two real pages.

### Added

**A test that every ```` ```python ```` block in `doc/providers.md` actually
imports.** A reference whose examples do not run teaches the wrong thing twice
— once when they fail, and once when the reader assumes the rest is equally
approximate. Proved to catch the defect rather than merely pass: restoring the
bare `...` fails the test with `SyntaxError: ':' expected after dictionary key`.

### Audited

`doc/providers.md` was checked claim by claim against the code rather than
against memory, the same way `colorlight`'s programmer's guide was in v2.12.1.
Everything else held: the `Provider` contract is complete (the only
undocumented members are loader internals — `discover`, `load_file`,
`provider_dirs` — which are not part of the author-facing surface), and every
behavioural claim verified — the 60s default interval, `_`-prefixed files
skipped, `sys.path` restored after loading, later directories winning,
malformed config JSON ignored rather than fatal, opt-out enablement, the
five-minute backoff ceiling, both provider directories, both API routes with
their `path` parameter and `binding`/`owner` fields, and the `{data:}`,
`{bus:}` and `data_series` bindings.

## [0.28.1] - 2026-09-20

### Fixed

**The clock pushed to a program-mode card was four hours off the train times
beside it.** A card showed **21:07** in its corner next to a train leaving at
**17:08**.

The containers run **UTC**. Train times went through the board's own
`ZoneInfo` and were correct; the clock came from the pusher's naive
`datetime.now()`, which is container-local. Both numbers were on the same card,
a centimetre apart, in different time zones.

Two changes, because there were two ways to get it wrong:

- **A board now supplies its own clock**, formatted in the same zone as the
  times it is publishing. That is the zone that must match — a board watching a
  railway in another timezone should show *that* railway's clock, not the
  site's. The pusher `setdefault`s rather than assigns, so the board's wins.
- **The fallback clock uses `MARQUEE_SITE_TZ`**, which marquee already records
  and which was being ignored. This also fixes the same latent bug for
  bus-driven programs such as `dashboard-home`, whose `hhmm` and `date` were
  container-local too.

A test asserts the pushed clock equals the board's own formatting of now, so a
naive `datetime.now()` creeping back in fails rather than shipping.

> Worth remembering how this survived a deploy: `custom/` is **mounted** into
> the player but `services/player/` is **baked into the image**. `make restart`
> reloaded the provider and kept the old pusher, so the fix looked like it had
> not worked. Rebuild, don't restart — the rule holds even when part of the
> tree is a mount.

## [0.28.0] - 2026-09-20

### Added

**Providers can feed a card that runs its own program.** A card in
`mode: program` is never sent a frame: it composes its own pixels from named
values — a few hundred bytes a second against the ~24 Mbit/s a streamed card
eats — and keeps showing the last ones when the host dies, marked stale. A
provider now declares `PROGRAMS` and returns `board_pages(live)`.

The rail providers feed **`railboard`**, the on-card departure board added in
colorlight v2.12.0, so the Metro-North, LIRR and Amtrak boards now exist in both
forms: streamed as shows, and composed on the card itself.

**Values are selected BY PROGRAM, never merged.** The on-card table holds 24
keys and `set()` **ignores** the 25th rather than evicting, so accumulating
every source's values would silently drop whichever key went last — one column
blank on the glass with nothing in any log to say why. The pusher reads which
program a card runs from `cards.program` and sends only the pages of providers
that declare it. The MODE is still asked of the card, which is the setting that
must not be configured twice; the program NAME is marquee's own, served over
TFTP, so the database is the source of truth.

**The rotation is host-side**, for the same reason the page is a value at all:
rows outside a program's `dynamic` band repaint only when a value arrives, so a
card cycling on its own frame counter would redraw the whole canvas every frame.
Derived from the clock rather than counted, so a pusher restart does not jump
the sequence.

### Fixed

**Amtrak published station CODES where a sign wants names.** The footer read
`NYP` and the line read `NYP-BOS`. Both are resolved from the feed's own call
data now — it carries the code and the name on every stop — and the corridor
label is configurable, because the honest description (`New York Penn - Boston
South`) is 200px of a 128px wall.

### Notes on shaping values for a card

Three constraints the card imposes, all now in `doc/providers.md`:

- **Send every key the program binds, every time.** A key left out keeps its
  previous value on the card, so a short list leaves the last page's train
  sitting under this page's heading.
- **An empty string is indistinguishable from a key that never arrived** — the
  card renders `-` for both, deliberately, because a program must never have to
  decide what *absent* looks like. An empty row on a departure board is not
  absent, it is "no train": push a space.
- **Padding a name IS the instruction to scroll**, because the card decides by
  measuring what it is given — and the padding has to carry its own gap, since
  the scroll wraps. Do not pad to a power of two without checking: it lets the
  card use a mask instead of a division, but `North White Plains` rounds from
  18 characters to 32 and drags 112px of blank past a 56px window, so the
  column reads as empty for most of the page it is on.

## [0.27.1] - 2026-09-20

### Added

**`custom/example/shows/rail-rotation.yaml`** — all three rail boards in one
show. Nine scenes, ten seconds each: a 90-second cycle giving each railroad
half a minute. The single-system boards stay useful on their own; this is what
a wall shows when it has no particular allegiance.

**Two things change when a board joins a rotation**, and both are the same
problem — a viewer arriving mid-cycle has no idea which railroad they are
looking at:

- the **header names the system**, not the scene. On its own board
  `DEPARTURES` is unambiguous; in a rotation it could be any of three.
  `METRO-NORTH` is 83px of the 93px header at size 10, `LIRR` 26 and `AMTRAK`
  49 — measured, and the reason the header is not `MNR DEPARTURES`, which is
  103px and clips.
- the **scene type moves to the footer**, left, with the station on the right.
  Measured too: `From Boston` needs 57px and `New York Penn` 68, which is 125
  of a 124px footer — so the rotation calls it `Penn Station`, the same
  building the LIRR row already names that way.

Everything else — the `rows` layers, the column widths, the scroll settings —
is unchanged from the single-system boards.

### Fixed

**A `rows` layer pointed at a bare topic draws nothing, silently.** The
rotation was first generated with `data: "mnr.harlem"` instead of
`data: "mnr.harlem|departures"`. The layer then reads the whole payload — a
dict, not a list — and renders an entirely blank board. That is the documented
behaviour for a path that is not a list, and it is correct: nothing raises,
because a feed that is merely late must not take a sign down. It is also
indistinguishable from a broken provider.

A test now walks every `rows` layer in every shipped show and asserts its
address carries a path, and that the path names a list the boards actually
publish.

## [0.27.0] - 2026-09-20

### Added

**Two more worked providers: the LIRR, and Amtrak between New York Penn and
Boston.** Three now ship, over **two unrelated upstreams**, and the point is
not that the plane holds three files — it is that a single example was hiding
things that only a second and a third could show.

All three publish the same payload shape, so the three shipped boards are the
same show at the same column widths with the topic changed. That portability is
a convention the providers keep rather than something the plane enforces, and
it is the clearest statement of what a provider is for: absorbing the
difference between upstreams so the show does not have to.

**Providers can now share a helper module.** A file named `_*.py` was already
skipped by discovery — documented as the way to keep machinery beside a
provider — but it did not work: the import failed with `ModuleNotFoundError`
because the plugin's own directory was not on `sys.path`. So the capability was
promised and unusable, and two providers over one feed format had no choice but
to duplicate the parsing. `load_file` now puts the directory on the path for
the duration of the import.

It **appends** rather than prepends, so the standard library always wins: a
plugin directory containing `json.py` must not become the `json` module for the
whole player. A test asserts exactly that.

```
custom/example/providers/
  _board.py        presentation policy — how a time is written, when a
                   countdown blanks
  _gtfs_rail.py    GTFS-realtime protobuf + static GTFS
  metro_north.py   \_ two configurations of _gtfs_rail
  lirr.py          /
  amtrak.py        a JSON API — shares _board only
```

The split is deliberate: **share presentation, not parsing.** What a viewer
notices is the part that must not differ between boards in one rotation — one
reading `Now` while the next reads `0m` is the sort of thing nobody files a bug
for and everybody sees. A protobuf feed and a JSON API have nothing in common
below the payload, and a base class that tried to unify them would be a pile of
flags.

### Fixed

**The Metro-North vehicle join was right for Metro-North and would have been
exactly wrong for the LIRR.** The two MTA feeds use opposite identity
conventions, measured on both live:

| | Metro-North | LIRR |
|---|---|---|
| join trip updates to vehicles on `FeedEntity.id` | **203 / 203** | **0 / 53** |
| join on `trip.trip_id` | **0 / 203** | **53 / 53** |

0.25.0 fixed a join that matched nothing by switching to the entity id, which
was correct — for one of the two systems. Vehicles are now indexed under every
identifier they carry and looked up by both, which is simply correct on either
rather than a flag to get wrong. Without it every LIRR train reports
`scheduled` with nothing in the log to say why.

**LIRR train numbers were internal schedule ids.** Half its feed has no vehicle
yet, and those rows fell back to `GO202_26_6158` — not something to put on a
sign. Its realtime trip ids *are* static timetable ids, so `trips.txt` resolves
a real number for them: 107/107, for 56 kB of cache. On Metro-North the same
table resolves **0 of 201** and costs 1750 kB, so it is opt-in per system
(`TRIP_NAMES`) rather than always on. `vehicle.label` is the timetable number on
both systems and stays the first choice.

**The LIRR board's footer read "Babylon Branch line"** — the feed's route name
already ends in Branch.

### Notes on the Amtrak board

Amtrak publishes no public GTFS-realtime feed; its own endpoint returns an
encrypted blob. This reads a free community JSON API instead, which is why it
shares no parsing with the other two. It polls every **two minutes**, not
thirty seconds: the response is the whole national network, about 1 MB, there
is no endpoint that returns one corridor, and intercity trains do not move
between stations in thirty seconds.

What the richer upstream buys: scheduled *and* actual times per stop, so
lateness is a subtraction rather than a guess, and a `platform` — which is
usually **blank**, correctly, because Amtrak assigns a track at the last minute
and the feed says so. A board that filled it in would be inventing it.

Two layout decisions came out of measurement rather than taste. `Northeast
Regional` is 98px of a 128px wall, so the provider publishes a short `service`
name and the board shows *that* where the commuter boards show a destination —
on a Penn-to-Boston board every departure goes to Boston, and Acela vs Regional
is the choice a passenger is actually making. And this board **keeps** the
train-number column the commuter boards drop: on the Harlem line a bare station
name is unambiguous, but the Northeast Corridor runs Boston to Washington, so a
row reading `Trenton 13:47` could belong to any of a dozen trains.

## [0.26.0] - 2026-09-20

### Added

**A `rows` layer — a table, one row per entry of a list in the live cache.**

```yaml
- type: rows
  data: "mnr.harlem|departures"
  box: [1, 17, 126, 90]
  row_height: 15
  columns:
    - {field: time,        w: 31}
    - {field: destination, w: 63, scroll: auto, color: "#C9A84C"}
    - {field: countdown,   w: 26, align: right, color: "#8899aa"}
```

**The row count comes from the data**, which is what a page of hand-written
text layers cannot do. The Metro-North board shipped in 0.25.0 spelled its
tables out as three text layers per row with the index baked into each path —
18 layers a scene, 69 in the file — and that form has three faults: eight
departures means writing six more layers rather than changing a number; moving
a column 2px is eighteen separate edits; and **every row draws whether or not
there is a train for it**, so a quiet hour rendered a column of no-value glyphs
that read as data. The same board is now 18 layers total, and three trains draw
three rows with the rest of the box dark.

A row that exists but is missing one field still renders that cell's `—`. The
row is real and the value is not, and hiding that would be a lie rather than a
blank.

Rows are capped by what fits (`box.h // row_height`), so the layer cannot draw
outside its own box however long a feed grows; `limit` caps it lower. Columns
flow left to right from accumulated widths, so changing one width shifts the
rest. Cells are rendered by `_text` on a synthetic layer rather than by drawing
in the new code — alignment and the continuous-scroll machinery (integer pixels
per frame, the double draw, the wrap on text+gap) are subtle enough that a
second copy would drift from the first, and on this board a scrolling cell sits
directly beside a scrolling text layer.

`sparkline` and `rows` both take `data` with `bus` as the older spelling.

### Fixed

**The Metro-North board clipped its time column to `13:4:` on the wall.** The
show was imported into the database *before* its columns were re-measured, so
the running copy still had `size: 11` in a 30px box where `13:42` needs 35.
Re-imported. Worth remembering that editing a file under `custom/` does not
reach a wall until `make shows-import` runs — the database is what the player
reads.

**Column widths are now measured rather than chosen by eye.** All 31
Harlem-line station names were measured against candidate widths at each font
size. Two results that were not obvious: the house font is DejaVu Sans **Bold**
and far wider than it looks (at size 9 `13:42` is 29px of a 128px wall), and a
train-number column on the "on the line" scene costs so much width that 77% of
station names scroll — dropping it takes that to 93% static and the board goes
from busy to calm. It is one line to put back, which is the point of the layer.

**A scrolling column needs a real gutter.** A scrolling cell fills its whole
width by definition, so at `col_gap: 1` the tail of a scrolling station name
sat hard against the time beside it and the two read as one word.

## [0.25.0] - 2026-09-20

### Added

**Data providers — a plug-in plane for live data.** A provider is a Python file
that fetches something and publishes it on named topics; shows read it with
`{data:topic|dotted.path}` bindings. Drop the file into a directory and restart
— no registration step, no schema to declare, nothing to rebuild.

```python
class Tides(Provider):
    name = "tides"
    interval_s = 900.0
    def poll(self):
        return {"tides.next": {"high": "14:12", "low": "20:41"}}
```

The design decision worth recording is that a provider **writes into the same
`LiveState` cache the panel-bus already wrote into**, so nothing downstream
needed changing: `{data:...}`, `{bus:...}`, the `sparkline` layer, the preview
path and `ValuePusher` all read topics and cannot tell the two writers apart.
Adding the plane cost a writer, not a stack.

Loaded from `custom/example/providers`, `custom/<site>/providers` and
`/data/providers`, later winning — the `custom/` extension point the public
release plan names, used for the second time and by the maintainers themselves.
Config is settings-only (`provider.<name>.config`, JSON, merged over the class's
`DEFAULTS`); a file beside the plugin would be a second place the same setting
can live. Providers are **opt-out**: a file is only in that directory because
somebody put it there.

Failure is contained at every level, because the rule is the one bindings
follow — a missing integration must never blank a sign. A file that will not
import is skipped loudly and the others still load; `poll()` raising is caught,
counted, and **the last good payload stays in the cache**; repeated failure
backs off geometrically to a five-minute ceiling. Tests assert each of those
rather than assuming them.

`doc/providers.md` is the contract. ARCH.md §8 says why the plane is shaped
this way.

**`{data:topic|path}` bindings, and `sparkline: {data: ...}`.** The same read as
`{bus:...}` against the same cache, under the name that is true now that the
panel-bus is not the only writer. `{bus:...}` and `sparkline: {bus: ...}` keep
working — breaking the scene language is not worth a rename — but write `data`
in anything new. A sparkline must give exactly one of the two.

**`GET /api/v1/providers` and `/providers/topics/{topic}`.** What is loaded,
what it publishes, how stale each topic is, and the last error. The second
answers the question that actually blocks a show author: knowing `mnr.harlem`
exists does not tell you the path is `departures.0.destination`, so it returns
the document and echoes back the exact binding string to paste into a show.

**`LiveState` records which writer owns each topic.** Two writers on one topic
present as a value *flickering between two plausible readings*, which is close
to undiagnosable from the sign. Now a log line and an API field.

**A Metro-North rail board — the worked example.**
`custom/example/providers/metro_north.py` publishes trains departing a station,
trains arriving into it, and every train already running on the line with its
next stop and the time it reaches there.
`custom/example/shows/metro-north.yaml` draws it as a three-scene paging board.

It ships in the tree on purpose: the MTA's GTFS-realtime feeds are public and
need no API key, so it runs out of the box, and it exercises the whole contract
— working defaults, a slow-changing lookup table cached on disk, a fast feed on
an interval, and a payload shaped for bindings rather than for the upstream.

Two findings from building it, both recorded in the file:

- **MNR puts two identifier spaces in one feed.** A `trip_update` carries the
  GTFS trip id (`3209015`); the matching `vehicle` carries the train number
  (`3030`). Joining on `trip_id` matches **0 of 203** — every train silently
  reported "scheduled" instead of its real status. The `FeedEntity` id is the
  join, and is also the number a passenger sees.
- **Absent protobuf fields are not zero.** GTFS-realtime's `current_status`
  defaults to `IN_TRANSIT_TO` (2), so a train with the field omitted is moving.
  Defaulting it to 0 made a tenth of the fleet look like it was pulling into a
  station it had left.

It reads GTFS-realtime protobuf in ~40 lines of standard library rather than
taking `gtfs-realtime-bindings`, because a provider that needs a `pip install`
is no longer something you can drop into a directory. That is safe here
specifically because the wire format is self-describing enough to walk without a
schema, and the v2.0 field numbers have been fixed since 2011. Verified
byte-for-byte against `google.transit.gtfs_realtime_pb2` on the live feed: 204
trips, 2,625 stop-time updates, 204 vehicle positions, zero differences.

The provider publishes a `countdown` field reading `"Now"`, `"7m"` or `""`
alongside the raw `minutes`, because the scene language has no conditionals by
design — so "blank it beyond an hour" has to be decided by the provider, and a
board reading `117m` is a number no passenger has ever wanted.

### Changed

- The player now mounts `custom/` (read-only). It did not before; providers are
  loaded from there.
- `ValuePusher` starts when **either** a bus URL or a provider is configured. It
  was gated on `MARQUEE_BUS_URL` alone, which would have left program-mode cards
  unfed on an install that uses providers only.

## [0.24.0] - 2026-09-20

### Added

**A test that the wire constants agree with the panel firmware.** `test_wire.py`
checked this side's arithmetic against literals in the same file, which cannot
catch the failure that matters: the firmware defines the same constants
independently, in Rust, and when they drift the symptom is a **sheared image**
rather than an error — nothing raises, nothing logs, every packet looks valid.

Four assertions read `bitmap_udp.rs` directly: the header size, `PIXELS_PER_CHUNK`,
`PIXELS_PER_CHUNK_INDEXED`, and that the firmware is self-consistent —
`MAX_PAYLOAD / 3` equals its own documented 487, indexed is exactly 3× that, and
`MAX_PAYLOAD` is *not* divisible by 3, which is the whole reason the indexed size
is `3 × 487` rather than 1462.

Proved to catch drift rather than merely pass: mutating the firmware's `// 487`
to `// 486` fails two of the four.

`make be-test` now mounts the firmware, because these skip without it and a
skipped test is not a test. `FIRMWARE` defaults to `../firmware`.

### Fixed

- `check_shows.py` hard-coded an internal bus hostname; it takes `MARQUEE_BUS_URL`
  from the environment with no host default, since the bus is site infrastructure.
- The nginx comment and a changelog entry named an internal host and another app
  while explaining why `/marquee` must redirect. The reason is general; those are
  not.

## [0.23.0] - 2026-09-19

### Added

**The Content editor now says what it is about to change.** Four gaps, all of
which let you act without the information you needed:

- **File/database drift is visible.** Files under `custom/<site>/shows/` are the
  source of truth and the database is what the player reads, but nothing keeps
  them in step — and the UI never said so. `GET /shows/files` reports `in-sync` /
  `db-newer` / `file-newer` / `no-file` per show, plus files on disk with no row
  at all, which were previously invisible and so looked like a show that did not
  exist. Export and import buttons do what the Makefile targets do.
- **Shows carry `walls[]`**, and the editor names them: *live on bench — saving
  changes what is showing*. You could previously rewrite a live wall believing
  you were editing a draft.
- **`POST /shows/validate` is wired up**, debounced as you type, reporting
  scenes, duration, size, fps and format — or the parse error. It already
  existed and nothing called it, so a malformed document was found by the
  preview failing or the save being rejected.
- **A real editor**: line-number gutter, YAML highlighting, and Tab that indents
  instead of leaving the field. No editor dependency — a highlighted `<pre>`
  behind a transparent `<textarea>`, so native editing, selection and undo are
  untouched.

### Changed

- `MARQUEE_SITE` (default `dashboard`) selects which `custom/<site>/shows`
  directory the UI exports to and imports from, mirroring the Makefile's `SITE`.

## [0.22.1] - 2026-09-19

### Changed

**`dashboard-home` streams `format: indexed` again.** It was `rgb` because the
card's palette was single-buffered while the framebuffer was gateware-swapped,
so a palette that changes — which is any scene with a clock in it — showed as
colour artifacts once per change. That is fixed in colorlight v2.7.0 (RB-5516):
the palette is two banks selected by the same signal the framebuffer follows.

Indexed buys roughly 3x the packet rate, which is what makes the nine-panel
P1.25 wall possible. On this one-panel wall either format works — rgb sustains
30 fps at 128x128 with zero drops — so the `display:` line is the only change
needed to go back.

Requires colorlight v2.7.0 **flashed as a bitstream**, not just firmware. An
older bitstream reports no palette banks, the firmware falls through to the
single-bank path, and the original artifact returns.

## [0.22.0] - 2026-09-19

### Added

**The values an on-panel dashboard needs.** A panel in `mode: program` composes
its own pixels and never receives a frame, so everything it draws has to arrive
as a named value. Adds `humidity`, `power`, `gallons`, `nextzone`, `zones` and
`running` to the mapping, plus three values the panel cannot work out for
itself:

- `hhmm` and `date` — the panel has no RTC and `ctx.time_ms` is uptime.
- `hist` — the power trace, encoded by `encode_series()` as 60 ASCII bytes
  scaled to the series' own min..max. One byte per sample means a trace column
  is a single index on the panel; sending `"43.2,42.8,.."` would mean parsing
  floats per pixel on a core with no FPU, which is a frame-rate cut rather than
  a style choice.
- `page` — which of the four pages is showing.

The page is pushed rather than cycled on the panel, and that is a deliberate
trade. Cycling on `ctx.frame` makes the whole canvas change between frames, so
every row must be redrawn every frame; the platform's answer is that rows are
repainted **when a value arrives**, so the page has to be a value. If marquee
dies the cycling stops, but the glass keeps its last values and raises the stale
marker — which is the thing a streamed panel structurally cannot do, because it
freezes on a frame that looks perfectly correct forever.

Needs colorlight v2.6.0 on the card: `hist` is 60 bytes and the card's value
slots were 24, and the table held 16 keys against the 16 this pushes.

## [0.21.0] - 2026-09-19

### Fixed
- **A film change stalled the whole wall for 3 to 9 frames.** Opening a
  multi-gigabyte file and initialising a decoder costs **131–434 ms measured**,
  and the render loop is single-threaded — so every transition was paid on the
  frame deadline and the picture froze and jumped.

  The next film's decoder is now opened **while the current one is still
  playing**, and the transition is a swap. Measured: spawning even 0.5 s early
  takes the first read from 120–469 ms to **0.1 ms**, and more lead does not
  help — so there is nothing to tune.

  Through the real code path, 10 transitions in 20 s of playback: worst frame
  **0.73 ms** (`cover`) and **0.91 ms** (`smart`), against a 50 ms budget, with
  **zero** frames over it.

- **`fit: smart` was running ffprobe on the render thread.** Introduced in
  0.20.0 and only visible once the transition stall was removed: building a
  decoder costs 0.3 ms at `fit: cover` and **110–483 ms at `fit: smart`**,
  all of it ffprobe. Now cached per file (dimensions do not change) *and* moved
  to the pre-warm worker, so neither a cold probe nor ffmpeg startup touches
  the render loop.

- `next_frame()` **never blocks**. If a film ends before the next is open it
  holds the final frame for that tick rather than waiting on ffmpeg — a held
  frame is a held frame, a blocked loop stalls the whole wall. Only reachable
  on a cold start.

- `close()` releases the pre-warmed decoder too; it is a live ffmpeg, and
  leaking it leaked a process and a file handle per show change.

## [0.20.0] - 2026-09-19

### Fixed
- **`fit` was ignored on every video layer.** `VideoDecoder`'s ffmpeg filter
  chain was hard-coded to `force_original_aspect_ratio=increase,crop=w:h` — a
  cover crop — so `contain` and `smart` were accepted and silently did nothing,
  and every video was cover-cropped whatever the show said. The framing has to
  happen in ffmpeg (the decoder reads a fixed number of bytes per frame, so
  what comes out is already final), and it now builds the chain from the
  layer's `fit`. Six tests pin it.

### Added
- **Fractional boxes and wall-native rendering** — one show, many walls. Omit
  `display.width`/`height` and write boxes as fractions (`0.5`, `"50%"`), and
  the player renders at the wall's own size instead of rendering small and
  resizing. A 128x128 show on a 768x384 wall used to be stretched 6x across and
  3x down. An int is always pixels and a float always a fraction, so `1` and
  `1.0` differ; booleans are rejected rather than quietly becoming 1px.
  Absolute boxes are unchanged.
- **`fit: smart`** — zoom a bit, and centre, decided from the two shapes rather
  than a number typed into a show that would be wrong on the next wall. Fills
  the box when `cover` would crop 15% or less; otherwise takes the geometric
  mean of the contain and cover scales, the one scale where crop and letterbox
  are exactly equal. Measured on one 16:9 film: 25%/25% on the square bench
  wall instead of 44% either way, and a clean fill on 128x64 and 768x384.
- `home-movies` is now wall-agnostic: no declared size, full-wall fractional
  box, `fit: smart`.

## [0.19.0] - 2026-09-19

### Fixed
- **Switching a card to program mode did nothing to the board.** It updated the
  database and the card's TFTP config and stopped there, so the card carried on
  doing whatever it was already doing until someone power-cycled it. The UI
  said `program: dashboard` while the board reported `running: false` — and
  because the player skips program-mode cards, nothing streamed to it either.
  The panel sat on a stale frame looking like a working sign.

  Mode and program are now pushed to the board when you change them. The boot
  config is still the record of intent and still what a cold card reads; this
  just makes the change take effect when you make it. If the board cannot be
  reached the API says which ones, rather than implying it worked.
- **Dead programs are restarted.** A program-mode card that is not actually
  running its program is an invisible failure: stale frame, nothing streaming
  to it, looks healthy. The player now checks each program-mode card every poll
  and starts the right program if it is stopped or running the wrong one —
  the same treatment the pixel DMA already got. Verified by stopping a program
  behind marquee's back and watching it come back within a poll.

### Changed
- **One content control, covering both kinds.** Content used to mean "a show",
  with programs set per card on a different page — an implementation detail
  leaking into the UI, since both answer "what is on this wall?".
  `GET/PUT /walls/{id}/content` lists and sets both, and **mode is no longer
  something you set**: choosing content is the mode decision, so the separate
  control added in 0.18.0 is gone.

  The two are not the same operation and the options say so. A show is rendered
  once and each card gets its crop — one picture across N cards. A program runs
  **on** each card from that card's own width and height (the firmware's
  program context carries `w`, `h`, a frame counter and values — no wall size,
  no origin), so N cards run N independent copies. The list labels this, and a
  multi-card wall says plainly that it is N displays rather than one picture.

  Programs offered for a wall are the **intersection** across its cards: one
  firmware carries every program and two cards need not run the same build, so
  a program only one card has is not something the wall can show.

  Per-card mode remains as an override, for the case it exists for.

## [0.18.0] - 2026-09-19

### Added
- **`make shows-check`** — renders every scene of every show and reports what
  is broken. Exists because "did that renderer change break a show?" is not
  answerable by reading: a show is YAML plus ffmpeg plus whatever the bus was
  serving, and the failure modes are deliberately silent — a layer that errors
  is caught and skipped so a broken integration never blanks a sign, which is
  right at runtime and useless when hunting a regression. Exits non-zero on a
  real failure, so it can gate a release.
- **A wall-level mode control.** Choosing content for a wall says the wall
  shows that picture; a card running an on-board program does not. Acting on
  that used to mean opening every card in turn — nine clicks on a nine-card
  wall to say one thing — and the Walls page did not even show that some cards
  were ignoring the content. `POST /walls/{id}/mode` sets every card at once,
  the Walls page shows the wall's mode, and a disagreement is named: *"bench
  runs an on-board program and will not show this content."*

  **Mode stays a per-card column and that is deliberate.** A program-mode card
  still has its pixels when the host dies, so one board can keep showing
  something true while its neighbours hold a frozen frame that looks perfectly
  healthy. The wall owns the bulk verb; the card keeps the override.
- `GET /walls/{id}/layout` returns a `mode` rollup — `stream`, `program`, or
  `mixed`.

### Fixed
- `video-test` rendered **all black**. Not a fault: `start: 600` seeks to a
  genuinely dark passage of the film, so the throughput numbers were fine but
  the panel showed nothing and the test looked broken every time. Seeks to
  1800s now, where there is picture, so it doubles as a visual check.
- Source errors are tagged `source:` in `Renderer.errors`, so a missing
  camera/Wowza/download (this install's configuration, true forever on a
  machine with no cameras) is distinguishable from a code fault. Without it any
  gate cries wolf permanently.

### Checked
All 18 shows render. No regressions from the renderer changes in 0.16/0.17.
Three shows report unconfigured sources — no cameras registered, no
`wowza_base`, YouTube blocked upstream — and all three still render lit, which
is the designed behaviour.

## [0.17.0] - 2026-09-19

### Added
- **`random:` source** — picks independently every time, so a film can come
  round again before the others have played. That is what random means, and it
  is what the bench wall's `home-movies` show now uses.

  `shuffle:` stays, with the same argument, for drawing without replacement.
  Which to use is taste, not correctness: with a few hundred films `random:`
  repeats sooner than most people expect (the birthday problem, not a fault),
  and `shuffle:` guarantees every film before any repeat. Swapping the scheme
  is the only change needed.

  Neither will play the same film twice **back to back**. In `random:` that is
  a 1-in-N event which reads as a stuck picker rather than as luck, so it is
  excluded; nothing else about the choice is constrained.

## [0.16.0] - 2026-09-19

### Added
- **`shuffle:` source** — `src: "shuffle:Home Movies"` plays one film to its
  end, picks another at random, forever. Scope to a folder with
  `shuffle:Home Movies/2019`.

  The randomness had to live in the **layer**, not the timeline: `scene_at()`
  is a stateless function of show-time, so a random source there would re-roll
  on every frame. A `shuffle:` layer owns its own clock, which means the
  scene's `duration` has no effect on what you see.

  A shuffled **bag** rather than an independent pick each time — with 339 films
  an independent pick repeats far more often than people expect, and a repeat
  inside ten minutes reads as a bug. Every film plays before any repeat, and
  the join between two bags never replays the same film back to back.
- **`home-movies` show** for the bench wall: the whole library, at random,
  `format: rgb` (video is the worst case for 256 colours, and a per-frame
  palette is exactly what triggers the colour-artifact bug).
- `Resolver.pool()` — every video under a library, sorted, with the same
  containment check as `plex:`. Skips DFS replication bookkeeping
  (`DfsrPrivate`, `__DFSR_DIAGNOSTICS_TEST_FOLDER__`, dotfiles, `._` stubs);
  `/share/homemovies` carries several of those beside the real folders.

### Changed
- `VideoDecoder.ended` — a finished file was previously indistinguishable from
  a stalled one, since both just keep returning the last frame. Nothing above
  could tell that a film had ENDED.

### Verified
339 films discovered with no junk; real ffmpeg decode confirmed; end-of-file
advancement proven with three 1-second clips — 10 plays, 3 distinct films, each
clip lasting exactly its length. 11 new tests.

## [0.15.0] - 2026-09-19

### Added
- **Colour order is now a per-card setting** — `RGB RBG GRB GBR BRG BGR`,
  chosen on the Cards page and served to the card in its boot config. In
  v0.14.0 the UI could only tell you this did not exist; colorlight v2.4.0
  added the gateware CSR and this is the control for it.

  The IC and the panel's own wiring decide which logical colour each HUB75 pin
  group carries. Get it wrong and the hues are wrong while **every counter on
  the card stays clean** — there is nothing in the statistics to find, which is
  what made it expensive before there was a setting at all.

  Per card, like the driver chip and for the same reason: all of a card's
  outputs run in lockstep off one engine, so there is one order for the board.
- The panel detail popover shows the order in force alongside the driver chip,
  and points at the card's own `/api/rgborder` for trying one live. Finding the
  right order is a look-and-see job; the card takes it instantly over that API,
  and this setting is where the answer is recorded so it survives a reboot.
- `GET /rgb-orders`, mirroring `RGB_ORDERS` in the firmware's `layout.rs` and
  the gateware CSR — same order, and the index IS the CSR value.

### Changed
- `cards.rgb_order` column (migration `c3d8b2e41f09`). Nullable, no default.
  Empty **and the literal `RGB`** mean the standard order and are not written
  into the boot config at all, so every existing card's config is
  byte-identical to before the column existed. Upgrade and downgrade both
  tested against the live database with the card record intact.

## [0.14.0] - 2026-09-19

### Added
- **Click a module for its hardware detail** (Cards page): connector, module
  size, grid position, and the driver chip in force. The panels drawn in
  v0.13.0 were read-only shapes; now they answer questions.
- The detail states two things the UI previously left implicit:
  - **The driver chip is set per CARD, not per module, and that is not an
    oversight.** The IC is physically on the module, but the gateware shares
    ONE register table across all of a card's outputs, so a card cannot drive
    two chip types at once — every module on a card must carry the same IC.
    Someone about to mix modules now reads that where they are looking.
  - **There is no per-module colour mapping.** RGB channel order is fixed by
    the bitstream (the board pin map plus `hub75.py`), with no runtime remap in
    gateware, firmware or here. Saying so beats letting someone hunt for a
    setting that does not exist — `panel-check`'s RGBW swatches detect a wrong
    channel order but nothing can currently correct it.

## [0.13.0] - 2026-09-19

The UI showed cards but never the **panels plugged into them**.

A card is one receiver board; the modules hanging off its connectors are a
different thing, and nothing in the UI said how many there were or what size.
The bench card read `128×128` and the two 128×64 modules making that up
appeared only as raw YAML inside the edit form — so "is this one panel or two,
and which connector is the top one?" could not be answered by looking at the
app.

### Added
- **Panel map on every card** (Cards page): the modules drawn to scale from the
  card's own `layout_yaml`, labelled by connector, with a summary line —
  `2 panels · 128×64 each · J1 J2`. Always visible, not hidden behind the edit
  pencil. Parsed from the same YAML the card is served at boot, so it cannot
  drift from what the board will actually do.
- **Module seams in the wall map** (Walls page): a card rectangle is no longer
  drawn undivided. Dashed lines mark where one module ends and the next begins,
  and the tooltip says how many modules are on that card. Drawn with
  `pointer-events: none` so they never interfere with dragging.

### Changed
- `GET /walls/{id}/layout` now returns each card's `layout_yaml`, which is what
  the wall map needs to place the seams.

## [0.12.2] - 2026-09-19

### Fixed
- `host-nginx/marquee.conf` had no `location = /marquee` redirect, so a bare
  `/marquee` fell through to the host's catch-all. Where that catch-all
  belongs to a different app, `/marquee` silently rendered
  **another app** rather than 404ing — which reads as Marquee being broken when
  it was simply never reached. Added the 301.

### Deployed
- Marquee is now on the the build host Homepage dashboard and served at
  `https://the build host.example.com/marquee/`. `host-nginx/marquee.conf` already
  existed and had never been installed on the host; it is now symlinked as
  `/etc/nginx/conf.d/_marquee.inc` and included from the build host's server block, so
  the routing stays versioned in this repo. The tile is `type: link` rather
  than a Homepage `type: proxy` tile because only `= /`, `/admin/` and
  `/assets/` reach that container on the build host (homepage v1.15.0).

## [0.12.1] - 2026-09-19

### Docs
- **Per-card firmware over TFTP now works**, on colorlight v2.3.0 or later.
  That BIOS derives its MAC from the card's flash unique ID and does its own
  DHCP, so `boot.bin` is requested from the card's own reserved address and
  `tftp_resolve`'s lookup by host succeeds. Measured on the bench card: the
  request moved from `192.168.1.50` to `192.168.1.70`, and the log went from
  `identified as bench ... via ARP` to a direct hit.

  `README.md`, `ARCH.md` and the docstring on `_mac_for_ip()` all stated flatly
  that this was impossible and that staged rollout meant flashing. That was
  true of every bitstream before today and is now wrong for anything current,
  so all three say which side of v2.3.0 they mean. Nothing in the code changed:
  the ARP fallback stays, because cards on older bitstreams still netboot from
  one shared address.

## [0.12.0] - 2026-09-19

The bench wall now runs a real home information display.

### Added
- **`sparkline` layer** — plots a numeric series the panel-bus has already
  pushed (`{type: sparkline, bus: "home.status|power.history"}`). Same drawing
  code as `graph`, different source. `graph` needs `ha.base_url` and `ha.token`;
  this install has only the base URL, so every `{ha:...}` renders the no-value
  glyph and every `graph` comes up empty. The bus carries the same facts, is
  already subscribed, and costs the render path no network call — which is the
  house rule for something redrawing 20 times a second. Non-numeric entries and
  booleans are dropped rather than plotted as zero: a `null` drawn as zero is a
  cliff to the floor that reads as real data.
- **`dashboard-home` show** — four scenes on the 128x128 bench wall (time and
  weather, security, power with a live load sparkline, irrigation), all off
  `{bus:home.status|...}`.

### Fixed
- The player no longer manages the card's hardware pixel filter. That
  workaround turned the filter off so palettes could reach the CPU, and turning
  it off also turns off the gateware buffer swap (they share one call in the
  firmware) — so the DMA wrote into the buffer being displayed. It traded a
  colour bug for a worse one. The real fix shipped in colorlight v2.1.0: the
  classifier spares the palette magic, so the filter stays on.

### Fixed (build)
- `make be-test` was permanently red: `tests/test_multicard_stream.py` imports
  `player.player`, and the api image ships `app/ alembic/ scripts/ tests/` only,
  so those two tests could never pass there. The suite now runs in the player
  container, which carries both `player` and `marquee_core`. 36 passed.

### Known issue
- **`format: indexed` shows colour artifacts on any scene with a clock or live
  data in it.** The palette is single-buffered on the card while the
  framebuffer is double-buffered and gateware-swapped, so the indices and the
  palette on screen can belong to different frames. Every counter stays clean
  while it happens. Tracked as RB-5516; `doc/scenes.md` carries the warning
  beside the benchmark that makes indexed look unconditionally better.
  Prefer `rgb` where it fits — at 128x128 it sustains 30 fps with zero drops.

## [0.11.0] - 2026-09-19

One picture may now span more than one receiver card. **`Panel` is split into
`Wall` and `Card`.**

### Added
- **`Wall`** — a logical display: a name, a pixel size, and the show assigned to
  it. **`Card`** — one receiver board driving a rectangle of a wall, with an
  `x`/`y` origin within it.

  A panel used to be both, which held for exactly as long as one board could
  drive a whole display. It cannot, for three reasons that all arrive together
  on a fine-pitch wall:

  | Limit | Value on current hardware |
  |---|---|
  | Output connectors per receiver | typically 8 — a nine-module wall needs two boards |
  | Framebuffer | 327,680 px, about ten 256x128 modules |
  | Memory bandwidth | sets refresh: 36.8 Hz for nine modules on one card, **49.1 Hz split across two** |

  The third is the one worth noticing: splitting a wall does not merely make a
  bigger wall possible, it makes the wall **faster**, because each card only
  reads its own region out of memory. That is how commercial LED systems scale
  too — by adding receivers rather than building bigger ones.

- **One render, N crops.** `WallPlayer` draws the whole canvas once per frame
  and sends each card `canvas.crop(card.box)`. Cheaper than rendering per card,
  and the only way two boards stay in step: they are handed the same frame, from
  the same render, in the same pass. Rendering twice would put two independent
  frame clocks on one picture.

  **Nothing in the firmware or the wire protocol changed.** A card receives a
  frame sized to its own region and has no idea it is part of anything larger,
  which is why this works identically for the HUB75 boards running today and for
  newer S-PWM hardware.

- **`GET /walls/{id}/layout`** reports gaps, overlaps and overhangs rather than
  rejecting them — an operator mid-edit will briefly have all three. The Walls
  page draws the wall to scale with each card's rectangle on it, because these
  failures are geometric: a gap is a black band on the glass and an overlap is
  two boards rendering the same pixels, and neither reads as a coordinate error
  when you are standing in front of it.

- **Walls and Cards pages**, replacing Panels. Content is set on the wall;
  boards, modes and health on the card. A streaming card shows which wall it
  belongs to and links there rather than offering a second, conflicting place to
  set the show.

### Changed
- Assignments and schedules moved from the card to the **wall**. Every board
  covering one picture must play the same show, and making that a property of
  the wall removes the possibility of them disagreeing. Stats stayed **per
  card** — on a two-card wall it is exactly the difference between the two
  series that says which board is struggling. Mode stayed per card too, so one
  board can run an on-board program while its neighbours stream.
- `panels` -> `cards`, `panel_stats` -> `card_stats`, `PanelStream` ->
  `CardStream` (the old name is kept as an alias).
- Every doc updated to use the two words precisely: `ARCH.md` gains section 2a,
  and the in-app Help gains a "Walls and cards" section.

### Migration
**Lossless, and single-card installs are a no-op.** Every existing panel becomes
a one-card wall of the same name and size at (0, 0), so nothing has to be
reconfigured. Verified against a copy of the live database, including a full
up-down-up round trip.

The downgrade needed fixing after it was actually run. `batch_alter_table`
rebuilds a table from a reflection taken at entry, so an index still in that
reflection is recreated against a column dropped in the same batch — it failed
with "no such column: wall_id" and left the database stranded halfway. Dropping
the index first fixes it, and running the downgrade rather than assuming it
worked is how it was found.

### Compatibility
`POST /api/v1/play` still accepts `panel` and resolves it by wall name and then
by card name, so other homelab apps calling it keep working; the response echoes
both keys. `GET /api/v1/panels` remains as a deprecated alias for `/cards`.

## [0.10.0] - 2026-09-16

### Added
- **`custom/` — an extension point, and shows as files.** Shows existed only as
  rows in an untracked SQLite file: no review, no history, no backup, and no way
  to ship a starting set with the project. A disk failure lost them.

  ```
  custom/README.md        what goes here                 SHIPS
  custom/example/shows/   a worked example               SHIPS
  custom/<site>/shows/    your shows                     NEVER SHIPS
  ```

- **`make shows-import` / `make shows-export`** (`SITE=` selects the directory).
  Import is **idempotent** — it only writes rows whose content actually changed,
  so re-running it is how a file edit reaches a panel. Export brings a change
  made in the web UI back into version control.

  A show file is a normal scene document with an optional `# name:` header
  comment. No header means the filename is the name, so a file dropped in by
  hand imports without ceremony rather than being silently skipped.

  All 16 existing shows exported, and the round trip verified: re-importing
  reports `0 created, 0 updated`.

### Documentation
- README, `ARCH.md` and the Help page cover where shows live and the
  files-vs-database split. The same division applies across the two repos:
  **runtime content here, build-time inputs in the firmware's `custom/`.**

## [0.9.1] - 2026-09-15

### Added
- **`LICENSE` — BSD 2-Clause.** There was none. Without a licence file, default
  copyright applies and nobody has permission to use, copy or modify the code,
  whatever a badge says.
- **`THIRD-PARTY.md`.** Nothing is vendored here, so this is mostly a dependency
  list — but it records the one thing that actually bites: Marquee **invokes**
  ffmpeg as a subprocess and does not link it, so this project's licence is
  unaffected by which ffmpeg is present. A distributed **image** is different,
  because it bundles one, and ffmpeg is LGPL or GPL depending on how it was
  compiled.

## [0.9.0] - 2026-09-15

### Changed
- **`MARQUEE_BUS_URL` and `MARQUEE_BUS_TOPICS` no longer default to a specific
  deployment's bus.** They were hardcoded to one site's websocket URL and its
  local feed names, so any other install would have retried a host that is not
  theirs, forever. Unset now means the live-data plane is simply **off** —
  bindings render the no-value glyph and nothing else changes. Both are
  documented in `.env.example`, and ours are set in `.env`, so nothing moves
  here.

  A missing integration must never blank a sign; an unreachable *default* is the
  same rule applied to configuration.

- `make push` checks for an `origin` remote rather than naming a specific
  canonical host.

### Fixed
- Docstrings, an HTTP-binding example, a camera placeholder in Settings and two
  documents no longer name internal hosts.

### Notes
These are the nine violations the publish generator refused on
(`doc/PUBLIC-RELEASE-PLAN.md` §5a). Fixed as **real edits** rather than
publish-time substitutions: a transform that rewrites a hostname in a sentence
still leaves a document describing somebody else's network, and the default was
a genuine bug for anyone who is not us.

## [0.8.1] - 2026-09-15

### Documentation
- **The rule, stated plainly everywhere it matters: once a Colorlight card is
  programmed, Marquee is all you need.** Nothing in the colorlight repo runs at
  runtime — it builds gateware and firmware, compiles on-panel programs and
  flashes boards. Marquee serves boot, config, pixels, values and stats.

  Added to the README, `ARCH.md` and the Help page (searchable under "what do I
  need running?"). The boundary is the useful part: switching a panel between
  programs its firmware already carries is a change *here*; adding a new program
  is a firmware build *there*.

- Recorded that **netboot takes precedence over flash** — while Marquee is
  reachable, the image Marquee serves is the one that runs, and a flashed image
  is the fallback. That is why `./build.sh deploy` rather than re-flashing is how
  a networked fleet is updated.

## [0.8.0] - 2026-09-15

### Fixed
- **`tftp_resolve` was never called for `boot.bin`.** tftpy was given
  `/data/firmware` as its root, and its RRQ handler serves a static file if one
  exists, consulting `dyn_file_func` **only when it does not**:

  ```python
  if os.path.exists(path):          # <- static file wins
      self.context.fileobj = open(path, "rb")
  elif self.context.dyn_file_func:
  ```

  `boot.bin` exists there, so every panel got it straight off disk and the
  `firmware` field on a panel record did nothing at all. `<mac>.yml` worked
  *because no such file exists on disk* — which is exactly what made this look
  like a database lookup bug for months rather than a routing one. tftpy now
  gets an empty root and every request goes through the resolver.

- **The firmware handoff is no longer silent.** Every served image is logged
  with its name, size and the panel it was for. A panel booting the wrong
  firmware shows nothing on the wire and nothing on the glass.

### Added
- **MAC identity via ARP** when the host lookup misses. The player runs with
  host networking, so `/proc/net/arp` is the host's table and holds the sender's
  MAC.

  It does not currently succeed, and the reason is now visible instead of
  hidden: the BIOS fetches `boot.bin` **before DHCP**, using the gateware's own
  IP and MAC — **both compile-time constants shared by every panel built from
  that bitstream** (`eth_mac_address = 0x10e2d5000001`). So the request carries
  no panel identity at all. The fallback is now loud —
  `not identified (arp=10:e2:d5:00:00:00); serving default firmware` — which is
  what made the root cause diagnosable. Staged rollout is a **flashing**
  operation; see colorlight TODO item 8.

## [0.7.1] - 2026-09-15

### Added
- **Help covers standalone panels.** A panel's firmware can now carry its own
  layout and program, so three entries were missing: what wins at boot, what a
  standalone panel is, and that an offline panel has **no data** — every key
  reads as a dash, so a program meant to run offline must be written for that.
- **Troubleshooting: "a panel came up as one small panel, not the full wall".**
  That is a panel with no layout — either marquee served none (the player log
  says `no layout for <mac>.yml`) or it booted with no network and nothing baked
  into its firmware, and fell back to a single panel expecting a stream.

### Documentation
- **Boot precedence written down** in README and ARCH: the TFTP config marquee
  serves **always wins when it answers**, then the panel's compiled-in default,
  then a bare single panel. A standalone panel is not a special case that
  escapes management — point it at marquee and marquee takes over.

## [0.7.0] - 2026-09-15

### Added
- **A real help system in the web UI.** The Help page was nine paragraphs
  written before on-panel programs existed; it is now seven categories —
  Concepts, Content, Panels, On-panel programs, Integration, Troubleshooting,
  Reference — browsable by category and searchable across all of them at once.

  **Troubleshooting is symptom-first**, because a symptom is what an operator
  actually has: "the panel shows a frozen or corrupt picture" rather than
  "hardware pixel DMA". Entries carry hidden search keywords, so searching
  "frozen", "black", "stuck" or "garbage" all reach the DMA explanation.

  The install's own numbers sit at the top, now including how many panels are
  running an on-panel program.

### Fixed
- **The README said a streamed panel goes black when the host dies. It does
  not.** Nothing in the firmware blanks a panel when the stream stops — no new
  frames simply means the last complete frame stays lit. That is materially
  worse than going black: a dead host looks exactly like a working sign until
  someone notices the clock has not moved, which is the argument for on-panel
  programs and was being stated backwards.

### Documentation
- README gains the failure-mode reasoning behind the mode choice, the Help
  page, and pointers to the colorlight repo's hardware and FPGA guides.

## [0.6.0] - 2026-09-15

### Added
- **Panel modes.** `panels.mode` (`stream` | `program`) and `panels.program`
  (Alembic `4ff74165bd25`). A panel is either streamed to or runs an on-panel
  program; never both, because each writes the framebuffer and the result is
  corruption rather than one winning. Served to the panel in its TFTP config so
  the choice survives a reboot.
- **Value pusher** (`services/player/valuepush.py`) — pushes named values from
  the existing panel-bus subscription to program-mode panels. A few hundred
  bytes a second against the ~24 Mbit/s a streamed panel eats to show a static
  clock face. It ASKS each panel what mode it is in rather than being told.
- **Mode and program pickers** on the Panels page. The program list is fetched
  from the panel (`GET /panels/{id}/programs`): one firmware carries every
  program, so the list belongs to the build on that card and a copy here would
  drift. The content dropdown is hidden in program mode, where a show means
  nothing.
- The player re-asserts the panel's pixel DMA every tick.

### Fixed
- **The panel card showed the wrong field and explained too much.** Mode is a
  two-way switch, so it reads `Network` or `Program` and nothing else, and
  exactly one follow-up field follows from it — `content` for a Network panel,
  `program` for a Program one. A show means nothing to a panel drawing its own
  pixels and a program means nothing to one being streamed to; offering both is
  what made the card confusing. Stored values stay `stream`/`program`.
- **`PATCH /panels/{id}` could not patch.** It validated against `PanelIn`,
  where `name` and `host` are required, so a partial body was rejected with 422
  — while the handler already used `exclude_unset`, i.e. it had always been
  written for partial updates. Changing one field meant resending every other.
- **A panel reboots with pixel DMA OFF**, and marquee only enabled it when the
  player started. Any reboot silently dropped that panel onto the ~600k px/s CPU
  path, where the receive ISR cannot drain the MAC FIFO: ~3 chunks of every 12
  are lost, so NO frame completes and the panel keeps showing whatever was on
  it. That reads as a frozen or corrupt display rather than a slow one, and was
  misdiagnosed twice before being found.

## [0.5.0] - 2026-09-14

### Added
- **`graph` layer** — a sparkline of a Home Assistant entity's recent history,
  backed by a new `ha_history_reader` (its own cache: history is a far heavier
  query than a state read, and a 24-hour window does not change between frames).

  Deliberately minimal — line, optional fill, optional baseline, no axes or
  gridlines. On a 64px-tall panel that furniture would eat the signal, which is
  the same lesson `HUB75.md` records about small text.

  Non-numeric states are dropped rather than plotted as zero, since
  `unavailable` graphed at zero draws a cliff that looks like data.
  Downsampling picks one sample per column rather than averaging, because on a
  sensor trace the spikes are usually the point.

- **`house-status` show** — Dashboard outside temperature and condition on the
  left panel, lights-on count and its 24-hour trace on the right. Sized
  **256x64** for two 128x64 panels side by side.

### Fixed
- **`make be-test` never worked.** `pytest` was absent from the api image, and
  `tests/` was empty — so the target errored, and would have run zero tests if
  it hadn't. pytest is now in the image, the Dockerfile copies tests in, and
  there are **17 tests** covering the wire protocol: chunk arithmetic, packet
  shapes and MTU bounds, palette edge cases, quantiser behaviour, scene-field
  validation.

- **Alembic was claimed but absent.** `db.py`'s docstring already said "Real
  schema CHANGES go through Alembic (homelab-app-standard §12)" and
  `alembic==1.14.0` was in requirements, but nothing was wired — so
  `Base.metadata.create_all()` was the only path, and it never ALTERs an
  existing table. A column added to a model would silently never reach the live
  database.

  Now scaffolded: `alembic.ini`, `env.py` reading the URL from the environment
  the way the services do (so a migration cannot be applied to a different
  database than the app uses), an initial revision autogenerated against an
  empty database capturing all 8 tables, and **the live database stamped** at
  it so it will not try to recreate. `render_as_batch=True` because SQLite
  cannot ALTER a column in place.

  New targets: `make migrate`, `make db-revision M="..."`, `make db-current`.

### Notes
- Home Assistant needs `ha.base_url` (set) and `ha.token` (**not set** — long-
  lived tokens can only be minted from HA's UI). Until the token is present
  every `{ha:...}` renders the no-value glyph and graphs draw only their
  baseline; nothing else is affected.

## [0.4.0] - 2026-09-14

### Added
- **Indexed wire format: ~3x the frame rate on graphics content.** A new
  `display.format: indexed` on a show quantises each frame to 256 colours and
  sends one byte per pixel instead of three, with the palette pushed separately.

  The win is packets, not bandwidth. The panel is bound by **per-interrupt
  cost**, not pixels — its CPU is already out of the pixel path because the
  gateware DMA consumes the UDP stream directly, and the panel repo measured
  ~1.13 packets per ISR entry against a ~20.6 fps ceiling at 256x192. Putting
  the same MTU to work on 3x the pixels takes a 256x192 frame from **101 chunks
  to 34** (verified), so the interrupt bound moves by the same factor.

  Deliberately a per-**scene** setting rather than per-panel, because it is a
  property of the content: text, charts and flat graphics quantise with no
  visible loss, photographs and video band. The same panel plays both.

  Quantisation is median cut with dithering **off** — dither noise that vanishes
  on a 200 dpi screen is individually visible lit pixels at this pitch, and it
  crawls on moving content.

  `PIXELS_PER_CHUNK_INDEXED` is 1461 (3 x 487), matching the panel and the
  gateware. Not 1462: that is not divisible by three, and would place every
  chunk after the first one pixel left of where the hardware writes it, showing
  as shearing rather than as an error.

- **Palette packets (`'B','P'`).** 256 entries in 768 bytes, one packet, with
  partial updates legal. The player sends a palette only when it changes, which
  on most content is one packet at scene start rather than one per frame.

### Requires
- Panel firmware speaking `'B','I'` / `'B','P'` (colorlight 2026-09-14 or
  later). Older firmware counts these as bad magic and shows nothing, so
  `format` defaults to `rgb` and existing shows are untouched.

### Known gaps
- `make be-test` cannot run: `pytest` is not installed in the api image. This
  predates the change. The indexed path was verified functionally instead
  (quantiser output sizes, packet magics, chunk counts, MTU bounds, payload
  totals, and scene-field validation).
- Nothing has been confirmed on a physical panel — the test board is powered
  down.

## [0.3.0] - 2026-09-10

### Added
- **Each panel card links to the board's own web interface.** Every Colorlight
  card serves an HTTP status page from its firmware -- layout, DMA state, packet
  and MAC counters, the crash breadcrumb -- and reaching it previously meant
  knowing the address and typing it by hand. The link sits in the card footer,
  opposite "last seen", opening in a new tab.

  The URL is built as `http://<host>/` and deliberately does **not** use
  `panel.port`. That field is 7000, the UDP pixel-stream port; the firmware's
  HTTP server listens on the default port, so deriving the link from `port`
  would have produced a dead link on every card in the fleet.

## [0.2.0] - 2026-09-07

### Added
- **Live data over panel-bus, no polling.** Marquee holds one websocket to a
  panel-bus, which already maintains a single upstream connection per feed and
  replays the last snapshot on subscribe. Bindings read a memory cache:
  `{bus:home.status|weather.temp}`.
  Two reasons this beats Marquee integrating each service itself: one place
  holds the credentials and the reconnect logic, and the render path never
  touches the network -- a sign redrawing 20 times a second must read a value
  from memory, not make a request.
- **Home Assistant + HTTP bindings** — `{ha:sensor.x}` via the REST API, and
  `{http:url|json.path}` for services with a plain HTTP API.
  Both cached; both render the no-value glyph on failure rather than raising.
- **Twelve demo shows** covering the real content shapes: a full-screen home
  movie, picture-in-picture with two decoders, a Times Square video-plus-crawl,
  a station clock, a quad camera wall, Wowza live, YouTube, HA sensors, HVAC
  status, split video/data, a three-lane ticker wall, and a multi-scene
  rotation.

### Fixed
- **Scrolling text juddered.** Three independent causes, all measured:
  - The renderer animated on the wall clock, so each frame advanced the scroll
    by however long the previous render happened to take. It now derives a frame
    index from elapsed time and renders at exactly `index/fps`, which also skips
    an index rather than letting the show drift behind real time.
  - Scroll speed was fractional pixels per frame. PIL draws at integer
    positions, so 40 px/s at 15 fps became a 3,3,2,3,2 stagger. Speed is now
    snapped to whole pixels per frame -- under half a pixel of speed error,
    perfectly even motion.
  - The pacing deadline was built from frames *sent* while the index came from
    elapsed time, so after any skipped frame the deadline was already in the
    past and the loop spun instead of sleeping.
- **Scrolling text restarted with a gap.** It now draws a second copy one period
  to the right, so the tail of one pass is immediately followed by the head of
  the next and the band never blanks.
- **The frontend could not reach the API.** Its nginx served static files and
  fell everything else through to the SPA catch-all, so `/api/v1/panels`
  returned index.html -- the browser got HTML where it expected JSON and every
  list rendered empty with no error. The container now proxies `/api`, `/auth`,
  `/ws` and `/healthz`.
- **The TFTP server truncated transfers.** The hand-rolled implementation
  accepted only an ACK for the exact block just sent, so a duplicate or delayed
  ACK -- routine in TFTP -- caused a retry storm and a partial image, and panels
  would not boot ("no ack for block 3"). Replaced with tftpy, keeping dynamic
  per-panel resolution. tftpy then `flock()`ed the object handed to it, which a
  BytesIO cannot support, so payloads spool to a real temp file.
- **A crashed TFTP thread took the boot service down silently.** Panels cannot
  boot without it, so it is now supervised and restarts.
- **A panel reboot silently stopped streaming.** The hardware pixel DMA resets
  to off, and the packet spacing Marquee streams at is far above what the slow
  CPU path absorbs, so every frame was dropped and the display went black. The
  player now asserts the fast path when it connects.
- **`{ha:...}` never resolved** — the stored callable shadowed the resolver
  method of the same name, so the lookup found `None` and left the token
  unrendered.

### Changed
- Stats polling is now disableable (`MARQUEE_POLL_INTERVAL_S=0`). Sampling costs
  the *panel*: it serves `/api/status` from the same interrupt handler that
  consumes the pixel stream, so each poll is a brief gap in frame processing.


## [0.1.0] - 2026-09-07

Initial scaffold.

### Added
- **Scene language** (`marquee_core.scene`) — declarative YAML: display, scenes,
  layers (solid/gradient/image/video/text), boxes, fit modes, z-order,
  durations, transitions. Validated with Pydantic; a layer placed entirely
  outside the canvas is an error rather than a silently blank sign.
- **Renderer** (`marquee_core.render`) — Pillow compositor, ffmpeg video decode,
  scrolling text, `cover`/`contain`/`stretch` fitting. A failing layer is logged
  and skipped rather than blanking the display.
- **Bindings** (`marquee_core.bindings`) — `{clock}`, `{date}`, `{file}`,
  `{http}`, `{ha}`, `{env}`. Every resolver is total: a failed lookup renders a
  marker, never an exception.
- **Sources** (`marquee_core.sources`) — local files, `plex:` libraries (the
  existing `/share/homemovies` and `/share/movies` mounts), `youtube:` via
  yt-dlp, `cam:` and `wowza:` live streams, and raw rtsp/rtmp/http. Live URLs
  live in a registry, not in scene documents, so shows are safe to share.
- **Panel protocol** (`marquee_core.stream`) — the UDP bitmap protocol with
  deadline-based pacing.
- **Fleet model** — panels, shows, assignments, dayparting schedules (including
  windows that wrap midnight), API tokens, users, and a panel stats time series.
- **Poller** — samples panel counters; a counter going backwards is recorded as
  a reboot rather than smoothed into a bogus rate.
- **Player** — one render/stream thread per panel, re-resolving the schedule
  every tick so a daypart change takes effect without a restart.
- **TFTP boot service** — serves each panel its firmware and layout on port 6969.
- **API** — panels/shows/schedules/stats CRUD, show validation, and
  `POST /api/v1/play` so other homelab apps can put content on a panel.

### Added (same release, second pass)
- **Web console** (Vite + React + Tailwind, `homelab-system-ui` Slate language):
  left-sidebar Shell, top-right account/status/logout cluster, version bottom-left,
  light+dark theming, colour-blind-safe status chips. Pages: Panels, Content,
  Schedule, Settings, Help, Login. Path-agnostic via runtime mount detection, so
  one build serves at a hostname root and under `/marquee/`.
- **Content editor with live preview** — the preview posts to the SAME renderer
  the player uses, so what an operator sees is literally what a panel receives.
- **Auth** — AD login (`ad_auth.py` shipped verbatim from the design skill) with
  the domain-admins-only policy for a system console, plus a local break-glass
  `admin` that authenticates through the password path and therefore still works
  when AD is unreachable. Remember-me included.
- **Scoped API tokens** — hashed at rest, shown once, individually revocable,
  with throttled last-used stamping.
- **Websocket** for live updates; queries have `refetchInterval: false` rather
  than polling.
- **TFTP boot service wired into the player** — serves each panel its firmware
  and its layout by identity; anything else is refused.
- **host-nginx config + Homepage tile** for same-origin `/marquee/` serving.
