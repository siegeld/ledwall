#!/usr/bin/env python3
"""Export shows to files, or import them back.

    python3 scripts/shows.py export custom/<site>/shows
    python3 scripts/shows.py import custom/<site>/shows

Run inside the api container (it has the package path and the database):
    docker compose exec -T api python /app/scripts/shows.py export /data/../custom/...
"""
import os
import sys

sys.path.insert(0, "/app")

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.shows_io import export_dir, import_dir
from marquee_core.models import Base

DB = os.environ.get("MARQUEE_DATABASE_URL", "sqlite:////data/marquee.db")


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in ("export", "import"):
        print(__doc__)
        return 2
    action, path = sys.argv[1], sys.argv[2]
    engine = create_engine(DB, connect_args={"check_same_thread": False}, future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, expire_on_commit=False)() as db:
        if action == "export":
            files = export_dir(db, path)
            print(f"exported {len(files)} show(s) -> {path}")
            for f in files:
                print("  ", os.path.basename(f))
        else:
            created, updated = import_dir(db, path)
            print(f"imported: {created} created, {updated} updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
