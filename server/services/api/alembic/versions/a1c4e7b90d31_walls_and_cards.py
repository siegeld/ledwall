"""walls and cards: one picture may span more than one receiver board

Until now `panels` was both the board and the display, which held only while a
single board could drive the whole thing. It cannot, for three reasons that all
arrive together on a fine-pitch wall: a receiver has a fixed number of output
connectors (typically eight), its framebuffer tops out around 327,680 px, and
its memory bandwidth sets the refresh rate -- so splitting a wall across two
boards roughly doubles what it can do.

So `panels` becomes `cards`, a new `walls` table owns the canvas, and each card
carries an `x`/`y` origin within its wall. Assignments and schedules move from
the card to the WALL: two cards showing halves of one picture must play the same
show, and making that a property of the wall removes the possibility of them
disagreeing.

MIGRATION IS LOSSLESS. Every existing panel becomes a one-card wall of the same
name and size at (0, 0), so a single-board install behaves exactly as before and
nothing has to be reconfigured.

SQLite cannot drop or alter a column in place, hence batch_alter_table
throughout.

Revision ID: a1c4e7b90d31
Revises: 4ff74165bd25
Create Date: 2026-09-19

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'a1c4e7b90d31'
down_revision = '4ff74165bd25'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "walls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("width", sa.Integer(), nullable=False, server_default="256"),
        sa.Column("height", sa.Integer(), nullable=False, server_default="128"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("notes", sa.Text()),
        sa.Column("created", sa.DateTime(timezone=True)),
    )

    op.rename_table("panels", "cards")
    with op.batch_alter_table("cards") as b:
        b.add_column(sa.Column("wall_id", sa.Integer()))
        b.add_column(sa.Column("x", sa.Integer(), nullable=False, server_default="0"))
        b.add_column(sa.Column("y", sa.Integer(), nullable=False, server_default="0"))

    # One wall per existing card, same name and size. Done in SQL rather than
    # by loading the ORM: a migration must not import models that have already
    # moved on to a later shape than the row it is reading.
    op.execute("""
        INSERT INTO walls (name, width, height, enabled, notes, created)
        SELECT name, width, height, enabled, notes, created FROM cards
    """)
    op.execute("UPDATE cards SET wall_id = (SELECT id FROM walls WHERE walls.name = cards.name)")
    op.execute("UPDATE cards SET x = 0, y = 0")

    with op.batch_alter_table("cards") as b:
        b.alter_column("wall_id", existing_type=sa.Integer(), nullable=False)
        b.create_foreign_key("fk_cards_wall", "walls", ["wall_id"], ["id"],
                             ondelete="CASCADE")
        b.create_index("ix_cards_wall_id", ["wall_id"])

    # Assignments and schedules move from the card to its wall. Because every
    # card is currently alone on its own wall, this is a one-to-one remap and
    # cannot collide with the unique constraint.
    for table, uq in (("assignments", "uq_assignment_panel"), ("schedules", None)):
        with op.batch_alter_table(table) as b:
            b.add_column(sa.Column("wall_id", sa.Integer()))
        op.execute(f"""
            UPDATE {table} SET wall_id =
                (SELECT wall_id FROM cards WHERE cards.id = {table}.panel_id)
        """)
        # A row whose card vanished has nowhere to go; it was already dead.
        op.execute(f"DELETE FROM {table} WHERE wall_id IS NULL")
        with op.batch_alter_table(table) as b:
            b.alter_column("wall_id", existing_type=sa.Integer(), nullable=False)
            b.drop_column("panel_id")
            b.create_foreign_key(f"fk_{table}_wall", "walls", ["wall_id"], ["id"],
                                 ondelete="CASCADE")
    with op.batch_alter_table("assignments") as b:
        b.create_unique_constraint("uq_assignment_wall", ["wall_id"])

    op.rename_table("panel_stats", "card_stats")
    with op.batch_alter_table("card_stats") as b:
        b.alter_column("panel_id", new_column_name="card_id",
                       existing_type=sa.Integer())


def downgrade() -> None:
    with op.batch_alter_table("card_stats") as b:
        b.alter_column("card_id", new_column_name="panel_id",
                       existing_type=sa.Integer())
    op.rename_table("card_stats", "panel_stats")

    # Collapse back to one card per display. A multi-card wall cannot be
    # represented in the old schema at all, so this keeps the first card of
    # each wall and drops the rest -- destructive, and the reason to take a
    # copy of the database before downgrading.
    for table in ("assignments", "schedules"):
        with op.batch_alter_table(table) as b:
            b.add_column(sa.Column("panel_id", sa.Integer()))
        op.execute(f"""
            UPDATE {table} SET panel_id =
                (SELECT MIN(id) FROM cards WHERE cards.wall_id = {table}.wall_id)
        """)
        op.execute(f"DELETE FROM {table} WHERE panel_id IS NULL")
        with op.batch_alter_table(table) as b:
            b.drop_column("wall_id")

    op.execute("""
        DELETE FROM cards WHERE id NOT IN
            (SELECT MIN(id) FROM cards GROUP BY wall_id)
    """)
    # Drop the index BEFORE the column it covers. batch_alter_table rebuilds the
    # table from a reflection taken at entry, so an index still in that
    # reflection is recreated against a column that no longer exists -- which
    # fails with "no such column: wall_id" and strands the database halfway
    # through the downgrade. Found by actually running it.
    op.drop_index("ix_cards_wall_id", table_name="cards")
    with op.batch_alter_table("cards") as b:
        b.drop_column("wall_id")
        b.drop_column("x")
        b.drop_column("y")
    op.rename_table("cards", "panels")
    op.drop_table("walls")
