"""cards.bitstream: which FPGA bitstream image this card has been pushed

The record of what gateware a card is meant to be running, so a push is a fleet
operation with a history rather than a one-off action nobody can audit later.

Separate from `cards.firmware`, and the distinction is the whole point. They are
two different artifacts with two different update paths:

    firmware   boot.bin, refetched over TFTP on EVERY boot, never stored on the
               card -- so changing it is a server-side change plus a reboot
    bitstream  the FPGA image in the card's SPI flash at offset 0, rewritten by
               the firmware over TFTP, and live only at the next POWER CYCLE

Conflating them in one column would make "what is this card running?"
unanswerable, because the answer is two things that change independently.

Nullable with no default. NULL means "whatever was flashed over JTAG and nobody
has pushed since", which is true of every card that exists when this lands --
so no backfill, and no card's behaviour changes.

Revision ID: d4e9c3f52a1b
Revises: c3d8b2e41f09
Create Date: 2026-10-07

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'd4e9c3f52a1b'
down_revision = 'c3d8b2e41f09'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("cards") as b:
        b.add_column(sa.Column("bitstream", sa.String(128)))


def downgrade() -> None:
    with op.batch_alter_table("cards") as b:
        b.drop_column("bitstream")
