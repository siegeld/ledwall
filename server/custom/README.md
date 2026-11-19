# `custom/` — your shows, data providers and module types

Everything in here is **yours**. The stock repo ships only this README and
`example/`.

```
custom/
  README.md            this file
  example/
    shows/             worked examples — copy one to start
    providers/         three worked data providers, over two upstreams
  <your-site>/
    shows/             scene documents (.yaml)
    providers/         data providers (.py)
    modules/           module types (.json)
```

## Files are the source of truth

Shows used to exist only as rows in an untracked SQLite file — no review, no
history, no backup, and no way to ship a starting set with the project. Now they
live here and are loaded into the database:

```bash
make shows-import SITE=<your-site>    # files  -> database  (idempotent)
make shows-export SITE=<your-site>    # database -> files
```

`shows-import` is safe to re-run; it only writes rows whose content actually
changed. Use `shows-export` after editing a show in the web UI, to bring the
change back into version control.

A file is a normal scene document with an optional `# name:` header comment. No
header just means the filename is the show name, so a file you drop in by hand
imports without ceremony.

See [`../doc/scenes.md`](../doc/scenes.md) for the scene language.

## `providers/` — your own live data

A **data provider** is a Python file that fetches something and publishes it on
named topics, which shows then read with `{data:topic|path}` bindings. Drop the
file in and restart; there is no registration step and nothing to rebuild.

```python
from marquee_core.providers import Provider

class Tides(Provider):
    name = "tides"
    interval_s = 900.0
    def poll(self):
        return {"tides.next": {"high": "14:12", "low": "20:41"}}
```

`example/providers/` ships three real ones end to end, all on public feeds with
no API key: **Metro-North**, the **LIRR**, and **Amtrak** between New York Penn
and Boston. The matching boards are in `example/shows/`.

The first two are configurations of one shared machinery module (`_gtfs_rail.py`
— a leading underscore means "helper, not a provider"); Amtrak reads a
completely different upstream and shares only the presentation base. All three
publish the same payload shape, so the three shows are the same board with the
topic changed.

A file here overrides a shipped one of the same `name`, so you can take the
example and change it without the original fighting you. `/data/providers/` is
also scanned, for trying something out without committing it.

A provider can also feed a card that runs its **own program** rather than being
streamed to — `_board.py` supplies the `railboard` program in the colorlight
repo, which draws the same three views on the card itself.

See [`../doc/providers.md`](../doc/providers.md) for the full contract.

## What goes here vs in the firmware

| | |
|---|---|
| **Here** | anything read at **runtime** — shows, data providers, module types, and the content a panel is sent |
| **The firmware** (`colorlight/custom/`) | anything compiled **into** the panel — on-panel programs, baked layouts |

The test: *does changing this require rebuilding firmware?* No → here.

## `modules/` — module types of your own

A **module type** tells a card what LED module is on its connectors: size,
where the RGB groups sit, the driver chip, the bitstream preset, the operating
point. The ones the project has driven are built in; any other is one JSON file
here, named `<key>.json`:

```json
{
  "label": "My P1.25 128x256",
  "board": "e320", "connector": "hub320",
  "module_w": 128, "module_h": 256,
  "drive": "icnd1065",
  "group_origins": "0,0 0,64 0,128 0,192",
  "bitstream_preset": "128x256-icnd1065l-hub320",
  "clk_div": 4,
  "rotate": 90,
  "chip_regs": [[12, 1, 14, 14], [13, 1, 5, 3]],
  "notes": "where this came from"
}
```

`rotate` is how the module is meant to be mounted, degrees clockwise from the
way its chips scan it (0, 90, 180, 270); a new card of the type takes it.
`group_columns` and `group_scan` (both optional) give one RGB group's size in
columns (a multiple of 16) and scan lines (even). They matter only to a card
on a runtime-geometry bitstream (colorlight 5.8.0), where one bitstream serves
every module no bigger than it was built for; other cards ignore them.
Give both or neither, up to 256 each, with `group_origins` (each a multiple of
16, the strip inside the module) and a `drive`; the groups' pixels must add up
to `module_w` x `module_h`.

`chip_regs` rows are `[register address, red, green, blue]` and replace those
registers in the chip's built-in table (card firmware 5.6.0+). The receiver
project's `tools/rcvbp_import.py` writes this file from a vendor's `.rcvbp`
profile. Files are read when a service starts -- the api and the player each
read them, so restart both; one that is not exactly right is
refused whole and listed under `refused` in `GET /api/v1/panel-types`. A file
cannot replace a built-in type. `/data/modules/` is read too, for trying one
without a rebuild, and `MARQUEE_MODULE_DIRS` replaces the whole list. The type
reaches a card in its boot config, so a card picks up a change at its next
boot.

A new module SHAPE still needs a bitstream built for it; this describes a
module to a card that can already drive that shape.
