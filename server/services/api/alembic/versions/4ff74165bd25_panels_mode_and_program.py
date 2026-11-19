"""panels.mode and panels.program for on-panel programs

A panel is either STREAMED to (marquee renders, the panel displays frames) or
runs an on-panel PROGRAM (the panel composes its own pixels from pushed values).
The two cannot share a panel: both write the framebuffer, and the result is
corruption rather than one winning.

Stored here rather than only in the panel's firmware because marquee cannot do
per-panel firmware -- tftp_resolve ignores the `firmware` field, so every panel
gets the same boot.bin. One firmware carries every program and this column picks
which runs, so a mixed fleet needs no bespoke build per panel.

Revision ID: 4ff74165bd25
Revises: 850020c8d3fd
Create Date: 2026-09-15

"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = '4ff74165bd25'
down_revision = '850020c8d3fd'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default so existing rows become explicitly "stream" rather than
    # NULL -- the player treats anything that is not "program" as streamable,
    # and a NULL that means "probably stream" is how a panel ends up being both.
    op.add_column('panels',
                  sa.Column('mode', sa.String(length=16),
                            nullable=False, server_default='stream'))
    op.add_column('panels', sa.Column('program', sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column('panels', 'program')
    op.drop_column('panels', 'mode')
