"""cards.rotate: how a card's modules are mounted, degrees clockwise

Some modules are built to hang a quarter turn from the way their driver chips
scan them (Myled's P1.25: its vendor profile maps a logical row to a chain
column). The card's framebuffer still follows the electronics, so the sender
turns each frame instead. 0, the default, is every card that existed before.

Revision ID: a7b9c1e43d5f
Revises: f6a8b0d32c4e
Create Date: 2026-11-16

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'a7b9c1e43d5f'
down_revision = 'f6a8b0d32c4e'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("cards") as b:
        b.add_column(sa.Column("rotate", sa.Integer(), nullable=False,
                               server_default="0"))


def downgrade() -> None:
    # Without the column a turned card is sent upright frames and its place on
    # the wall changes shape. Refuse rather than do that quietly.
    n = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM cards WHERE rotate != 0")).scalar()
    if n:
        raise RuntimeError(f"{n} card(s) are mounted turned; set rotate to 0 first")
    with op.batch_alter_table("cards") as b:
        b.drop_column("rotate")
