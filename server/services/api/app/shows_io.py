"""Import and export shows as files.

Shows are authored content. Until now they existed **only** as rows in an
untracked SQLite file: no review, no history, no backup, and no way for anyone
else to ship a starting set with the project. A disk failure lost them.

Files are the source of truth for anything under `custom/`; the database is
where the running system reads from. `import_dir` is idempotent, so re-running
it is safe and is how a change to a file reaches a wall.
"""
from __future__ import annotations

import datetime as dt
import os
import re

from sqlalchemy import select

from marquee_core.models import Show

SAFE = re.compile(r"[^A-Za-z0-9._-]")


def _filename(name: str) -> str:
    """A show name is free text; a filename is not."""
    return SAFE.sub("-", name).strip("-") + ".yaml"


def export_dir(db, path: str) -> list[str]:
    os.makedirs(path, exist_ok=True)
    written = []
    for show in db.scalars(select(Show).order_by(Show.name)).all():
        fn = os.path.join(path, _filename(show.name))
        body = show.body if show.body.endswith("\n") else show.body + "\n"
        # The name is carried in a header comment rather than inside the scene
        # document, so the file stays a valid show that the renderer can read
        # unchanged -- and so a hand-written file needs no extra ceremony.
        header = f"# name: {show.name}\n"
        if show.description:
            header += f"# description: {show.description}\n"
        with open(fn, "w") as fh:
            fh.write(header + body)
        written.append(fn)
    return written


def shows_dir() -> str:
    """Where this deployment's show files live, inside the container.

    Mirrors the Makefile's `SITE`, which defaults the same way, so the UI's
    export/import act on exactly the files `make shows-export` would.
    """
    site = os.environ.get("MARQUEE_SITE", "dashboard")
    return f"/custom/{site}/shows"


def _parse_file(text: str, fn: str) -> tuple[str, str | None, str]:
    """Split a show file into (name, description, body).

    Shared with `file_status` on purpose: drift is "does the file round-trip to
    what the database holds", and that question is only meaningful if both sides
    strip the header the same way.
    """
    name, desc, body_lines = None, None, []
    for line in text.splitlines(keepends=True):
        if name is None and line.startswith("# name:"):
            name = line.split(":", 1)[1].strip()
            continue
        if line.startswith("# description:"):
            desc = line.split(":", 1)[1].strip()
            continue
        body_lines.append(line)
    # No header: fall back to the filename, so a file someone just dropped in
    # still imports rather than being silently skipped.
    return (name or os.path.splitext(fn)[0], desc, "".join(body_lines))


def _nl(body: str) -> str:
    return body if body.endswith("\n") else body + "\n"


def file_status(db, path: str) -> dict:
    """Which shows differ from their files, and which way round.

    The database is what the running system reads; the files are what gets
    reviewed and backed up. Nothing keeps them in step automatically, so a UI
    edit lives only in the database until someone exports, and a file edit does
    not reach a wall until someone imports. Neither direction announced itself
    anywhere, which is how an edited show file plus a player restart looks
    exactly like marquee ignoring you.
    """
    files: dict[str, tuple[str, str, float]] = {}
    if os.path.isdir(path):
        for fn in sorted(os.listdir(path)):
            if not fn.endswith((".yaml", ".yml")):
                continue
            full = os.path.join(path, fn)
            with open(full) as fh:
                name, _desc, body = _parse_file(fh.read(), fn)
            files[name] = (fn, body, os.path.getmtime(full))

    shows, seen = [], set()
    for show in db.scalars(select(Show).order_by(Show.name)).all():
        seen.add(show.name)
        f = files.get(show.name)
        if f is None:
            shows.append({"id": show.id, "name": show.name, "file": None,
                          "state": "no-file"})
            continue
        fn, body, mtime = f
        if _nl(show.body) == _nl(body):
            state = "in-sync"
        else:
            updated = show.updated
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=dt.timezone.utc)
            state = "file-newer" if mtime > updated.timestamp() else "db-newer"
        shows.append({"id": show.id, "name": show.name, "file": fn, "state": state})

    # A file nobody has imported is invisible in the UI otherwise -- it looks
    # like the show simply does not exist.
    orphans = [{"name": n, "file": v[0]} for n, v in sorted(files.items()) if n not in seen]
    return {"dir": path, "site": os.environ.get("MARQUEE_SITE", "dashboard"),
            "shows": shows, "orphans": orphans}


def import_dir(db, path: str) -> tuple[int, int]:
    """Load *.yaml into the shows table. Returns (created, updated)."""
    created = updated = 0
    if not os.path.isdir(path):
        return (0, 0)
    for fn in sorted(os.listdir(path)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        full = os.path.join(path, fn)
        with open(full) as fh:
            text = fh.read()

        name, desc, body = _parse_file(text, fn)

        existing = db.scalar(select(Show).where(Show.name == name))
        if existing:
            if existing.body != body or (desc and existing.description != desc):
                existing.body = body
                if desc:
                    existing.description = desc
                existing.updated = dt.datetime.now(dt.timezone.utc)
                updated += 1
        else:
            db.add(Show(name=name, body=body, description=desc or "",
                        updated=dt.datetime.now(dt.timezone.utc),
                        created=dt.datetime.now(dt.timezone.utc)))
            created += 1
    db.commit()
    return (created, updated)
