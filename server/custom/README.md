# `custom/` — your shows

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
| **Here** | anything read at **runtime** — shows, data providers, and the content a panel is sent |
| **The firmware** (`colorlight/custom/`) | anything compiled **into** the panel — on-panel programs, baked layouts |

The test: *does changing this require rebuilding firmware?* No → here.
