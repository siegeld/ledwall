# `custom/` — your programs and configs

Everything in here is **yours**. The stock repo ships only this README and
`example/`; anything else you put here is not part of the upstream project.

```
custom/
  README.md        this file
  example/         worked examples — copy one to start
    panels/          hello, railboard
    configs/
  <your-name>/
    panels/        on-panel programs (.yaml)  -> compiled into the firmware
    configs/       panel layouts (.yml)       -> baked in with --default-config
```

The name of your directory does not matter; use one per site if you have
several, and point the build at the one you want.

## Build with it

```bash
# compile every program -- stock examples plus yours
./build.sh panelc

# build firmware, baking in one of your layouts so the panel needs no network
./build.sh --default-config custom/<your-name>/configs/<file>.yml firmware
```

`panelc` writes `src/assets.rs` and `src/generated.rs`, which are **generated
files** — edit the YAML, never those.

## Why the maintainers use this too

The site programs for the bench this project is developed on live in exactly
this directory, in exactly this shape. An extension point the maintainers do not
use is always subtly broken: an undocumented assumption, a path that only works
from the repo root, a flag nobody tested. This one is exercised on every build.

## What goes here vs in the server

| | |
|---|---|
| **Here** | anything compiled **into the firmware** — programs, baked layouts |
| **The server** (`marquee`) | anything read at **runtime** — shows, schedules, panel records |

The test is simple: *does changing this require rebuilding firmware?* Yes → here.

## The worked examples

`example/panels/hello.yaml` is the smallest thing that draws. `railboard.yaml`
is a real one: a station departure board fed entirely by pushed values, with a
scrolling destination column, and it is the program the marquee repo's rail
providers supply values for. Read it for the layout arithmetic — the seam rule,
the monospace cell budget, and why the scroll runs off `time_ms` rather than the
frame counter.

## Writing a program

See [../docs/PROGRAMMING.md](../docs/PROGRAMMING.md) for the widget and lambda
reference, the drawing library and the performance rules, and
[../docs/GETTING-STARTED.md](../docs/GETTING-STARTED.md) if you have not lit a
panel yet.
