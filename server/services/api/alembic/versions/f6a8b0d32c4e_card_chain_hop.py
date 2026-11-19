"""cards.chain_hop: a card's position behind another card in a daisy chain

Colorlight cards chain through their second Ethernet port. The whole chain is
ONE IP address -- the head card's. A card behind it (hop h >= 1) has no address
of its own and answers as the head on its own ports: pixels on 7000 + h, HTTP
on 8000 + h. So such a card's `host` is the head's address and this column says
which hop it is. 0, the default, is every card that existed before: a head, or
a card on its own.

Revision ID: f6a8b0d32c4e
Revises: e5f7a9c21b3d
Create Date: 2026-11-08

"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = 'f6a8b0d32c4e'
down_revision = 'e5f7a9c21b3d'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Refuse BEFORE touching the schema if normalising would collide (one card
    # entered twice, `02:AA:..` and `02-aa-..`): SQLite does not roll back DDL,
    # so failing at the UPDATE below left chain_hop added and the version
    # unbumped -- and every retry then died on "duplicate column".
    clash = op.get_bind().execute(sa.text(
        "SELECT lower(replace(trim(mac), '-', ':')) AS m, group_concat(name, ', ') "
        "FROM cards WHERE mac IS NOT NULL AND trim(mac) != '' "
        "GROUP BY m HAVING count(*) > 1")).fetchall()
    if clash:
        raise RuntimeError(
            "these cards share a MAC once case and separators are ignored; give "
            "each card its own MAC (or clear one) and rerun: "
            + "; ".join(f"{m}: {names}" for m, names in clash))
    with op.batch_alter_table("cards") as b:
        b.add_column(sa.Column("chain_hop", sa.Integer(), nullable=False,
                               server_default="0"))
    # One stored form for a MAC (marquee_core.models.normalize_mac): the TFTP
    # lookup is by MAC, and every chained card shares its head's IP, so a MAC
    # stored in capitals or with dashes is a card served the wrong config.
    op.execute("UPDATE cards SET mac = lower(replace(trim(mac), '-', ':')) "
               "WHERE mac IS NOT NULL")
    op.execute("UPDATE cards SET mac = NULL WHERE mac = ''")


def downgrade() -> None:
    # Without chain_hop a member is just a card on its head's IP and port --
    # several writers on the head's udp/7000. Refuse rather than do that.
    n = op.get_bind().execute(sa.text(
        "SELECT count(*) FROM cards WHERE chain_hop > 0")).scalar()
    if n:
        raise RuntimeError(f"{n} card(s) are chain members; delete them or move "
                           "them onto the switch (hop 0, own address) first")
    with op.batch_alter_table("cards") as b:
        b.drop_column("chain_hop")
