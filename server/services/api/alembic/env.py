"""Alembic environment for marquee.

The schema lives in `marquee_core.models`, which both the api and the player
import, so migrations are generated against that metadata rather than against
anything api-local.

The database URL comes from the environment, matching what the services use:
api reads `settings.database_url`, player reads `MARQUEE_DATABASE_URL`, and both
default to the same SQLite file. It is NOT taken from alembic.ini, so there is
one source of truth and no way for a migration to be applied to a different
database than the app is using.

SQLite cannot ALTER or DROP a column in place, so a revision touching an
existing column must use `op.batch_alter_table(...)` -- see
homelab-app-standard §12. `render_as_batch` below makes autogenerate emit that
form rather than a bare ALTER that would fail at apply time.
"""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from marquee_core.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

DEFAULT_URL = "sqlite:////data/marquee.db"


def _url() -> str:
    return os.environ.get("MARQUEE_DATABASE_URL", DEFAULT_URL)


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
