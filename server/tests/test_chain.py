"""Chained cards: one IP, many cards, told apart by port.

A card behind another card has no address of its own. It shares the head's
host and answers on its hop's ports, so every place marquee reaches a card has
to use `Card.api` / `Card.pixel_port`, and anything that identified a card by
its IP alone would find the HEAD. These pin both.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from marquee_core.models import Base, Card, Wall


def card(**kw):
    base = dict(id=1, name="c", wall_id=1, host="10.0.0.1", port=7000,
                mode="stream", layout_yaml="grid: 1x1\nJ1: 0,0\n", chain_hop=0)
    base.update(kw)
    return Card(**base)


def test_card_on_the_switch_is_unchanged():
    c = card()
    assert c.api == "10.0.0.1"
    assert c.pixel_port == 7000
    assert card(port=7100).pixel_port == 7100


def test_chained_card_is_the_heads_host_on_its_own_ports():
    c = card(chain_hop=3)
    assert c.api == "10.0.0.1:8003"
    assert c.pixel_port == 7003
    # `port` is ignored once the hop decides it: the firmware listens on
    # 7000 + hop and nothing else.
    assert card(chain_hop=3, port=7000).pixel_port == 7003


def test_tftp_config_follows_the_mac_not_the_shared_ip(monkeypatch):
    import player.main as pm

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    sm = sessionmaker(eng)
    with sm() as db:
        db.add(Wall(id=1, name="w"))
        db.add(card(id=1, name="head", mac="02:00:00:00:00:01",
                    layout_yaml="grid: 1x1\nJ1: 0,0\n"))
        db.add(card(id=2, name="member", mac="02:00:00:00:00:02", chain_hop=1,
                    layout_yaml="grid: 1x1\nJ2: 0,0\n"))
        db.commit()
    monkeypatch.setattr(pm, "SessionLocal", sm)

    # Both requests come from the head's IP; only the filename differs.
    head = pm.tftp_resolve("02-00-00-00-00-01.yml", "10.0.0.1").decode()
    member = pm.tftp_resolve("02-00-00-00-00-02.yml", "10.0.0.1").decode()
    assert "J1: 0,0" in head
    assert "J2: 0,0" in member


def test_discovery_payload_matches_the_gateware():
    """gateware/chain.py rewrites payload byte 2 (+1) and byte 4 (-1)."""
    from player.chain import DISC_PORT, PAYLOAD
    assert DISC_PORT == 6990
    assert PAYLOAD[:2] == b"CH"
    hop, anti = PAYLOAD[2], PAYLOAD[4]
    assert hop == 0 and anti == 255 - hop
    # Bytes 2 and 4 are both high bytes of 16-bit words (UDP payload starts at
    # an even offset), so +1 / -1 leaves the ones'-complement sum unchanged.
    assert len(PAYLOAD) % 2 == 0


def test_discovery_carries_the_clock():
    """Programs on several cards run off this clock (firmware chain.rs reads
    payload 6..10 big-endian when the version byte is >= 2)."""
    from player.chain import payload
    p = payload(0x12345678)
    assert p[3] >= 2
    assert p[6:10] == bytes.fromhex("12345678")
    assert payload(2**32 + 5)[6:10] == (5).to_bytes(4, "big")


def test_boot_config_places_a_card_in_its_wall():
    from marquee_core.models import boot_config
    w = Wall(id=1, name="w", width=512, height=256)
    left = card(id=1, width=256, height=256, x=0, y=0, mode="program", program="railboard")
    right = card(id=2, width=256, height=256, x=256, y=0, mode="program", program="railboard")
    left.wall = w; right.wall = w
    assert "wall: 512x256\norigin: 0,0\n" in boot_config(left)
    assert "wall: 512x256\norigin: 256,0\n" in boot_config(right)
    # A card that IS its wall says nothing new: its config is unchanged.
    solo = card(id=3, width=256, height=128)
    solo.wall = Wall(id=2, name="solo", width=256, height=128)
    assert "wall:" not in boot_config(solo) and "origin:" not in boot_config(solo)


def test_fbi_matches_crcfbigen():
    """The flash copy must be byte-for-byte what `crcfbigen.py -f -l` writes,
    or the BIOS's flashboot() rejects it and a chain member has nothing to run."""
    import binascii
    from marquee_core.fbi import fbi
    img = bytes(range(256)) * 37
    out = fbi(img)
    assert out[:4] == len(img).to_bytes(4, "little")
    assert out[4:8] == (binascii.crc32(img) & 0xFFFFFFFF).to_bytes(4, "little")
    assert out[8:] == img


def test_tftp_never_serves_a_member_its_heads_config(monkeypatch):
    """A MAC in any case finds its card; an unknown MAC from the head's IP gets
    NOTHING rather than the head's config."""
    import player.main as pm
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    sm = sessionmaker(eng)
    with sm() as db:
        db.add(Wall(id=1, name="w"))
        db.add(card(id=1, name="head", mac="02:00:00:00:00:01", layout_yaml="grid: 1x1\nJ1: 0,0\n"))
        db.add(card(id=2, name="m", mac="02:aa:00:00:00:02", chain_hop=1,
                    layout_yaml="grid: 1x1\nJ2: 0,0\n"))
        db.commit()
    monkeypatch.setattr(pm, "SessionLocal", sm)
    assert "J2" in pm.tftp_resolve("02-AA-00-00-00-02.yml", "10.0.0.1").decode()
    assert pm.tftp_resolve("02-99-00-00-00-09.yml", "10.0.0.1") is None


def test_normalize_mac():
    from marquee_core.models import normalize_mac
    assert normalize_mac(" 02-AA-bb-CC-dd-EE ") == "02:aa:bb:cc:dd:ee"
    assert normalize_mac("") is None and normalize_mac(None) is None
