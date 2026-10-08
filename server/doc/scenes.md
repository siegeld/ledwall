# The scene language

A show is YAML: a display, and a list of scenes played in order and looped.
Each scene is a stack of layers drawn into boxes.

```yaml
display: {width: 256, height: 128, fps: 24, format: rgb}
transition: {type: crossfade, duration: 0.5s}
scenes:
  - name: welcome
    duration: 10s
    layers:
      - {type: video, src: "plex:Home Movies/ski.mp4", box: [0,0,256,64], fit: cover, loop: true}
      - {type: image, src: logo.png, box: [8,72,48,48], fit: contain}
      - {type: text,  text: "DASHBOARD", box: [64,72,192,24], size: 20, color: "#C9A84C", align: center}
      - {type: text,  text: "{clock:%H:%M}", box: [64,100,192,20], size: 16, align: center}

  - name: cameras
    duration: 15s
    layers:
      - {type: video, src: "cam:front-door", box: [0,0,128,128], fit: cover}
      - {type: video, src: "wowza:lobby",    box: [128,0,128,128], fit: cover}

  - name: news
    duration: 15s
    layers:
      - {type: solid, color: "#0B0E13", box: [0,0,256,128]}
      - {type: text, text: "{file:/data/headlines.txt}", box: [0,54,256,20],
         scroll: left, speed: 30, color: "#e6edf3"}
```

## Display

| field | default | meaning |
|---|---|---|
| `width`, `height` | *the wall's own size* | omit them to fit any wall (see below) |
| `fps` | 24 | frames per second the show is rendered at |
| `format` | `rgb` | wire format: `rgb` or `indexed` |

### `format: indexed` — roughly 3x the frame rate, 256 colours

> **Needs colorlight v2.7.0 or later, flashed as a BITSTREAM.** Until then the
> panel's palette was single-buffered while the framebuffer was double-buffered
> and gateware-swapped, so the indices and the palette on screen could belong to
> different frames — a colour flash roughly once a second on any scene carrying
> a clock or live data, because the quantiser re-runs median cut and every entry
> shifts. **Every counter stayed clean while it happened**, so it was invisible
> in the stats; the test was to switch the same content to `format: rgb` and see
> it stop. Fixed in RB-5516: the palette is now two banks selected by the same
> signal the framebuffer follows.
>
> The firmware netboots but the bitstream lives in flash, so the two are NOT
> updated together. An older bitstream reports no palette banks, the firmware
> falls back to a single shared palette, and the artifact returns. Check with
> `curl http://<card>/api/palette` — `"banks": 2` means the fix is present.

`rgb` sends RGB888, three bytes a pixel. `indexed` quantises the frame to 256
colours and sends **one** byte a pixel, with the palette pushed separately.

The gain is not bandwidth, it is **packets**. The panel's ceiling is
per-interrupt cost, not pixels — its CPU is already out of the pixel path, since
the gateware DMA takes the UDP stream directly. Measured at 256x192:

| format | chunks/frame |
|---|---|
| `rgb` | 101 |
| `indexed` | **34** |

Roughly 3x fewer packets, so roughly 3x the frame rate, for the same content.

**Choose it per scene, by content.** Text, charts, logos and flat graphics
quantise to 256 colours with no visible loss. Photographs and video do not —
median cut on a photograph will band. The same panel can play both: the format
travels with the show, not with the panel.

Quantisation uses median cut with **dithering off**. Dither noise that vanishes
on a 200 dpi screen is individually visible lit pixels at this pitch, and it
crawls on moving content.

**Requires panel firmware that speaks `'B','I'`** (colorlight 2026-09-14 or
later). Older firmware counts those packets as bad magic and displays nothing,
so leave `format: rgb` until the panel is updated — and see the note above about
needing the v2.7.0 *bitstream*, not just the firmware, for correct colours.

## One show, many walls

**Omit `display.width`/`height` and use fractional boxes**, and the same file
works on a 128x128 bench panel and a 768x384 3x3 without being rewritten.

```yaml
display: {fps: 20, format: rgb}          # no size
layers:
  - {type: video, src: "random:Home Movies", box: [0, 0, 1.0, 1.0], fit: smart}
```

A box edge can be pixels or a fraction of the wall:

| written | means |
|---|---|
| `128` | 128 pixels — an **int** is always pixels |
| `0.5` | half the wall — a **float** is always a fraction |
| `"50%"` | half the wall |

`1` and `1.0` therefore mean different things: one pixel, and the whole wall.

Why it matters: a show that pins its own size is rendered at that size and the
canvas is then **resized to the wall**, so a 128x128 show on a 768x384 wall is
stretched 6x across and 3x down. A show with no size is rendered at the wall's
own size and nothing is stretched at all. Shows with absolute boxes keep
working exactly as before.

## `fit: smart` — zoom a bit, and centre

`cover` fills the box and crops the overflow; `contain` shows everything and
letterboxes the rest. On a well-matched box either is fine. On a badly matched
one **both are bad in the same measure** — 16:9 footage on a square wall is 44%
cropped by `cover` or 44% black by `contain`.

`smart` decides from the two shapes:

- if `cover` would crop **15% or less**, it fills the box — a thin black edge
  where a small crop would do looks like a fault;
- otherwise it takes the **geometric mean** of the two scales, which is the one
  scale where the crop and the letterbox are exactly equal.

Measured, same 16:9 film, one show file:

| wall | result |
|---|---|
| 128x128 square | 25% cropped **and** 25% black, instead of 44% of either |
| 128x64 one panel | fills, ~11% cropped |
| 768x384 3x3 | fills, ~11% cropped |
| 256x64 side by side | 33% each way |

Parameter-free on purpose: the right amount of zoom depends on the wall's
shape, which is not known when a show is written.

> **`fit` used to be ignored on video layers.** The decoder's filter chain was
> hard-coded to scale-up-and-crop, so every video was `cover` whatever the show
> asked for, and `contain` did nothing. Fixed in 0.20.0 — if a video show
> looked right before and looks different now, that is why.

## Layers

| type | key fields |
|---|---|
| `solid` | `color` |
| `gradient` | `start`, `end`, `angle` (`h`/`v`) |
| `image` | `src`, `fit` (`cover`/`contain`/`stretch`/`none`) |
| `video` | `src`, `fit`, `loop`, `start` |
| `text` | `text`, `size`, `color`, `align`, `valign`, `scroll` (`none`/`left`/`auto`), `speed`, `font` |
| `graph` | `entity`, `hours` — a Home Assistant series (needs a token) |
| `sparkline` | `data` — a series already in the live cache (no token) |
| `rows` | `data`, `columns` — a **table**, one row per list entry |

All layers take `box: [x, y, w, h]`, `opacity` (0–1) and `z` (higher draws on
top; ties break by declaration order).

## `rows` — a table, one row per entry of a list

```yaml
- type: rows
  data: "mnr.harlem|departures"
  box: [1, 17, 126, 90]
  row_height: 15
  size: 9
  col_gap: 3
  columns:
    - {field: time,        w: 31, color: "#e6edf3"}
    - {field: destination, w: 63, color: "#C9A84C", scroll: auto, speed: 16}
    - {field: countdown,   w: 26, color: "#8899aa", align: right}
```

| field | default | meaning |
|---|---|---|
| `data` | – | **required**, `topic\|dotted.path` pointing at a **list** |
| `bus` | – | the older name for `data`; give exactly one |
| `columns` | – | **required**, at least one |
| `row_height` | 14 | pixels per row, text included |
| `col_gap` | 2 | pixels between columns |
| `limit` | *what fits* | cap the rows drawn |
| `size`, `color`, `font` | 12, white | defaults every column inherits |

Each column takes `field` and `w`, plus any of `size`, `color`, `font`,
`align`, `valign`, `scroll`, `speed` — omitted, they inherit the layer's.

**`field` is a path into each ROW, not into the topic.** The layer already
addressed the list, so a column says `time`, not `departures.0.time`. Dotted
paths reach into a nested row.

### The row count comes from the data

That is the whole point, and it is what writing the rows out as text layers
cannot do. Spelling a six-row board out by hand bakes the six in:

- eight departures means writing six more layers, not changing a number;
- moving a column 2px is one edit per row — eighteen, on a three-column board;
- and every row draws whether or not there is a train for it, so a quiet hour
  renders a column of no-value glyphs that **look like data**.

Here, three trains draw three rows and the rest of the box stays dark, which is
what a station board actually does.

A row that *exists* but is missing one field still renders that cell's `—`: the
row is real and the value is not, and hiding that would be a lie rather than a
blank.

### Notes that matter in practice

- **Rows are capped by what fits** (`box.h // row_height`), so the layer can
  never draw outside its own box however long the feed grows. `limit` caps it
  lower.
- **Columns flow left to right from the accumulated widths**, so changing one
  width shifts the rest instead of needing every `x` recomputed. A fractional
  `w` scales with the wall like any other box edge.
- **Measure before choosing widths.** The house font is DejaVu Sans **Bold**
  and is far wider than it looks in an editor — at size 9 `13:42` is 29px of a
  128px wall. A column sized by eye clips to `13:4:`.
- **Give a scrolling column a real gutter.** A scrolling cell fills its whole
  width by definition, so at a 1px gap the tail of a scrolling name sits hard
  against the value beside it and the two read as one word.
- **A topic that has not arrived, or a path that is not a list, draws nothing.**
  An empty table is the honest rendering of "no data"; it is not an error.
- **Cells are rendered by the same code as a `text` layer**, so alignment and
  scrolling behave identically to one sitting beside it.

`custom/example/shows/metro-north.yaml` is a worked board: three scenes, three
tables, 18 layers where the hand-written version took 69.

## `graph` — a chart of a Home Assistant entity's history

```yaml
- {type: graph, entity: sensor.lumen_lights_on, hours: 24,
   box: [132, 16, 120, 47], color: "#c9a84c", fill: true}
```

| field | default | meaning |
|---|---|---|
| `entity` | – | **required**, HA entity_id |
| `hours` | 24 | how far back to plot |
| `color` | `#40d080` | line colour; the fill is a third of it |
| `fill` | false | fill under the line |
| `min`, `max` | autoscale | pin the range when you know it |
| `baseline` | true | 1px rule along the bottom |

Deliberately minimal: a line, an optional fill, an optional baseline. No axes,
gridlines or tick labels — on a 64px-tall panel those spend most of the pixels
on furniture and leave almost none for the signal. Put the current value beside
it with a `text` layer if the number matters as well as the shape.

Notes that matter in practice:

- **Autoscale is the default and is not always what you want.** A flat signal
  autoscales into a box full of noise. Pin `min`/`max` for anything with a
  known range — a percentage, a thermostat.
- **Non-numeric states are dropped, not plotted as zero.** HA reports
  `unavailable` and `unknown` as states like any other, and plotting them as
  zero draws a cliff to the floor that looks like real data.
- **Downsampled by picking one sample per column, not averaging.** Averaging
  smooths away spikes, which on a sensor trace are usually the point.
- **No HA configured, or history unreachable, draws just the baseline.** A
  missing integration must never blank a sign.

Requires `ha.base_url` and `ha.token` in settings. Without them every
`{ha:...}` renders the no-value glyph and graphs come up empty, but the rest of
the scene is unaffected.

## `sparkline` — a chart of a series the live bus already holds

```yaml
- {type: sparkline, data: "home.status|power.history",
   box: [0, 77, 128, 51], color: "#c9a84c", fill: true}
```

| field | default | meaning |
|---|---|---|
| `data` | – | **required**, `topic\|dotted.path` — the same address `{data:...}` takes |
| `bus` | – | the older name for `data`; give exactly one of the two |
| `color` | `#40d080` | line colour; the fill is a third of it |
| `fill` | false | fill under the line |
| `min`, `max` | autoscale | pin the range when you know it |
| `baseline` | true | 1px rule along the bottom |

Same drawing as `graph`, different source, and the difference is the point:

- **No Home Assistant credentials.** `graph` needs `ha.base_url` and `ha.token`;
  without them it draws an empty box. `sparkline` reads a feed the panel-bus has
  already pushed, so it works on an install where `ha.token` was never set.
- **No network call on the render path** — it is a memory read, which is the
  house rule for something redrawing 20 times a second.
- **No `hours`.** The window is whatever the publisher chose to send. If you
  need a different span, that is a change to the feed, not to the scene.

Non-numeric entries are dropped rather than plotted as zero (a `null` would
otherwise draw a cliff to the floor and read as real data), and booleans are
dropped too — a feed of flags is not a series. A path that isn't a list, or a
topic that hasn't arrived yet, draws the bare baseline rather than failing.

## Sources

| prefix | meaning |
|---|---|
| `/abs/path` | a file |
| `name.png` | relative to the media root (cannot escape it) |
| `plex:Home Movies/x.mp4` | a configured Plex library |
| `random:Home Movies` | a video from a library at random, forever — repeats possible |
| `shuffle:Home Movies` | the same, but every video plays before any repeat |
| `youtube:<id or url>` | resolved via yt-dlp, cached |
| `cam:front-door` | a registered camera (RTSP) |
| `wowza:lobby` | a stream on the configured Wowza server |
| `rtsp:// rtmp:// http(s)://` | passed to ffmpeg |

Camera and Wowza URLs live in the registry, not in shows — a show stays safe to
share and a re-addressed camera is one settings change.

## `random:` and `shuffle:` — a library on random, forever

```yaml
- {type: video, src: "random:Home Movies", box: [0, 0, 128, 128], fit: cover}
```

| scheme | picks | repeats |
|---|---|---|
| `random:` | independently, every time | possible — that is what random means |
| `shuffle:` | without replacement | none until every film has played |

Neither will play the same film twice **back to back**. In `random:` that is a
1-in-N event which reads as a stuck picker rather than as luck, so it is
excluded; nothing else about the choice is constrained.

Plays one film to its end, then picks the next. Scope it to a folder with
`shuffle:Home Movies/2019`.

**The randomness lives in the LAYER, not the timeline, and that is the point.**
Every other show is a fixed cycle: scenes with durations, mapped from show-time
by a modulo. That is stateless, so a random source there would re-roll on every
frame. Such a layer owns its own clock instead — so **the scene's `duration`
has no effect on what you see**. Give the scene any duration you like; it still
plays each film to the end.

Which to use is a taste question, not a correctness one. With a few hundred
films `random:` repeats sooner than most people expect — that is the birthday
problem, not a fault — so if the repeats grate, switch the scheme to
`shuffle:` and every film will play before any comes round again. Nothing else
changes.

`loop` and `start` are ignored — the playlist never ends, and seeking into a
film you did not choose has no meaning. Replication bookkeeping in a shared
library (`DfsrPrivate`, dotfiles, AppleDouble stubs) is skipped; a picker that
can land on one of those is a picker that occasionally shows nothing.

**Use `format: rgb`.** `indexed` re-quantises every frame, so the palette
changes at the frame rate — exactly the condition behind the colour-artifact
bug — and video is the worst possible content for 256 colours anyway.

## Bindings

Resolved at render time inside `text`:
`{clock:%H:%M}` `{date:%a %d %b}` `{file:/path}` `{http:url}` `{ha:sensor.x}`
`{env:NAME}` `{data:topic|path}`. A failed binding renders `—`; an unknown one
is left visible so the typo shows up on the panel.

### `{data:topic|dotted.path}` — a value from the live cache

```yaml
- {type: text, text: "{data:mnr.harlem|departures.0.destination}", box: [0,0,128,16]}
```

Reads a topic published by the panel-bus or by a **data provider** — a plug-in
that fetches something and publishes it. Numeric path segments index lists, so
`departures.0.time` is the first row's time. It is a memory read: the render
path makes no network call.

See [providers.md](providers.md) for writing one, and
`GET /api/v1/providers/topics/{topic}` to see what a topic actually holds.

> **`{bus:...}` is the same read.** It is the older spelling, from when the
> panel-bus was the only publisher. Once a payload is in the cache there is no
> difference between the two, so naming the binding after a transport was
> always wrong — but shows in the field use it, and breaking the scene language
> is not worth a rename. Both work; write `{data:...}` in anything new.

## Durations

`10s`, `500ms`, `2m`, `1h`, or a bare number meaning seconds.
