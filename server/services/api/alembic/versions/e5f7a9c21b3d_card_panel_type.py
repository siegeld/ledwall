"""cards.panel_type: which module type sits on this card's connectors

A key of marquee_core.models.PANEL_TYPES. Choosing a type fills the module's
geometry (panel_width/panel_height), its RGB-group arrangement (group_origins)
and its driver chip into the card's boot config from one table, so the layout
YAML only places connectors and the same module never has its numbers typed
twice. The first entry beyond plain HUB75 is the Linsn P1.25 256x128 DP3264S
module on the E320, whose four groups sit in a 2x2 and need group_origins.

Nullable with no default: a card with no type is served exactly what it was
served before this column existed.

Revision ID: e5f7a9c21b3d
Revises: d4e9c3f52a1b
Create Date: 2026-10-19

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'e5f7a9c21b3d'
down_revision = 'd4e9c3f52a1b'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("cards") as b:
        b.add_column(sa.Column("panel_type", sa.String(64)))


def downgrade() -> None:
    with op.batch_alter_table("cards") as b:
        b.drop_column("panel_type")
