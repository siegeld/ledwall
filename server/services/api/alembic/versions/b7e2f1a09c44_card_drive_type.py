"""cards.drive: which LED driver chip the modules carry

The S-PWM output stage is one engine and the chip is a register table it loads
at boot. This is where the chip's name lives, served to the card in its TFTP
config alongside `mode` and `program`.

Nullable with no default on purpose: empty means "use whatever the bitstream
was built with", so every existing card keeps behaving exactly as it did.

Revision ID: b7e2f1a09c44
Revises: a1c4e7b90d31
Create Date: 2026-09-19

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'b7e2f1a09c44'
down_revision = 'a1c4e7b90d31'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("cards") as b:
        b.add_column(sa.Column("drive", sa.String(32)))


def downgrade() -> None:
    with op.batch_alter_table("cards") as b:
        b.drop_column("drive")
