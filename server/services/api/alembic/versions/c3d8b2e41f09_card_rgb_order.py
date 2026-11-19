"""cards.rgb_order: which channel order the modules on this card expect

The IC and the panel's own wiring decide which logical colour each HUB75 pin
group carries. Get it wrong and the hues are wrong while every counter on the
card stays clean -- there is nothing in the statistics to find, which is what
made it expensive before there was a setting at all.

Per CARD and not per module: all of a card's outputs run in lockstep off one
engine, so there is one order for the board.

Nullable with no default on purpose. Empty (and the literal "RGB") means the
standard order and is not written into the card's boot config at all, so every
existing card's config is byte-identical to before this column existed.

Revision ID: c3d8b2e41f09
Revises: b7e2f1a09c44
Create Date: 2026-09-19

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'c3d8b2e41f09'
down_revision = 'b7e2f1a09c44'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("cards") as b:
        b.add_column(sa.Column("rgb_order", sa.String(8)))


def downgrade() -> None:
    with op.batch_alter_table("cards") as b:
        b.drop_column("rgb_order")
