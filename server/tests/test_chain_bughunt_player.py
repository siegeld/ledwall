"""Bug hunt on card chains, player side: the head-down hold (DOWN_HOSTS).

A member's stream is held while its HEAD is in DOWN_HOSTS (`link.head in
DOWN_HOSTS`, player.py), so the head's BIOS netboot is not drowned. These
show where that hold is wrong.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import player.player as pp
from marquee_core.models import Base, Card, Wall


class _NoPlayer:
    def __init__(self, *a, **k):
        import threading
        self.stop_flag = threading.Event()

    def start(self):
        pass



@pytest.fixture
def env(monkeypatch):
    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    sm = sessionmaker(eng, expire_on_commit=False)
    with sm() as s:
        s.add(Wall(id=1, name="w", width=512, height=256))
        s.add(Card(id=1, name="head", wall_id=1, host="10.0.0.1", width=256, height=256))
        s.add(Card(id=2, name="m1", wall_id=1, host="10.0.0.1", chain_hop=1,
                   x=256, width=256, height=256))
        s.commit()
    up: set[str] = {"10.0.0.1", "10.0.0.1:8001"}
    probed: list[str] = []

    def status(api):
        probed.append(api)
        return {"dma_enabled": 1} if api in up else None

    monkeypatch.setattr(pp, "WallPlayer", _NoPlayer)
    monkeypatch.setattr(pp, "_card_status", status)
    monkeypatch.setattr(pp, "_program_state", lambda api: ("x", True))
    monkeypatch.setattr(pp, "_start_program", lambda *a: None)
    monkeypatch.setattr(pp, "_stop_program", lambda *a: None)
    monkeypatch.setattr(pp, "_enable_card_dma", lambda *a: None)
    pp.DOWN_HOSTS.clear()
    sup = pp.Supervisor(sm, resolver=None, tz="UTC", poll_s=0)
    yield sm, sup, up, probed
    pp.DOWN_HOSTS.clear()


def _member_held() -> bool:
    # The sender's test, verbatim: link.host is the member's api, link.head its host.
    return "10.0.0.1:8001" in pp.DOWN_HOSTS or "10.0.0.1" in pp.DOWN_HOSTS


def test_a_head_that_leaves_stream_mode_while_down_does_not_hold_its_members_forever(env):
    sm, sup, up, _ = env
    up.discard("10.0.0.1")               # head reboots...
    sup.tick(); sup.tick()
    assert "10.0.0.1" in pp.DOWN_HOSTS   # ...members held, as designed
    with sm() as s:                      # operator switches the head to a program
        s.get(Card, 1).mode = "program"
        s.commit()
    up.add("10.0.0.1")                   # head is back and answering
    for _ in range(3):
        sup.tick()
    assert not _member_held(), (
        f"head answers again but DOWN_HOSTS={pp.DOWN_HOSTS}: member m1 stays dark "
        "until the player restarts")


def test_a_disabled_head_that_was_down_does_not_hold_its_members_forever(env):
    sm, sup, up, _ = env
    up.discard("10.0.0.1")
    sup.tick(); sup.tick()
    with sm() as s:
        s.get(Card, 1).enabled = False
        s.commit()
    up.add("10.0.0.1")
    for _ in range(3):
        sup.tick()
    assert not _member_held(), f"DOWN_HOSTS={pp.DOWN_HOSTS}"


def test_a_program_mode_head_is_still_watched_for_its_members(env):
    """The head runs an on-card program; its members stream. The head is never
    probed for liveness, so its reboot never lands in DOWN_HOSTS and the
    members' streams flood its BIOS netboot -- the bug the hold exists for."""
    sm, sup, up, probed = env
    with sm() as s:
        s.get(Card, 1).mode = "program"
        s.commit()
    up.discard("10.0.0.1")               # head reboots
    for _ in range(3):
        sup.tick()
    assert "10.0.0.1" in pp.DOWN_HOSTS, (
        f"head is down but not held; probed={sorted(set(probed))}")


def test_an_unregistered_member_is_not_served_a_mac_less_heads_config(monkeypatch):
    """MAC is optional on a card. A head registered without one, and a member
    just cabled behind it (not yet added from the scan): the member asks for
    `<its mac>.yml` from the head's IP, the MAC matches nothing, and the
    IP fallback -- `card is not None and not card.mac` -- names the HEAD."""
    import player.main as pm
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    sm = sessionmaker(eng)
    with sm() as s:
        s.add(Wall(id=1, name="w", width=512, height=256))
        s.add(Card(id=1, name="head", wall_id=1, host="10.0.0.1", mac=None,
                   layout_yaml="grid: 1x1\nJ1: 0,0\n", width=256, height=256))
        s.commit()
    monkeypatch.setattr(pm, "SessionLocal", sm)
    # A member at hop 1 asks from the head's IP on its own client port.
    got = pm.tftp_resolve("02-00-00-00-00-99.yml", "10.0.0.1", 20004)
    assert got is None, "a card nobody registered was served the head's config:\n" + got.decode()
    # The head itself, also without a MAC on record, still gets its own.
    assert b"J1: 0,0" in pm.tftp_resolve("02-00-00-00-00-01.yml", "10.0.0.1", 6900)
    # And a member recorded without a MAC is found by its position.
    with sm() as s:
        s.add(Card(id=2, name="m1", wall_id=1, host="10.0.0.1", chain_hop=1, mac=None,
                   layout_yaml="grid: 1x1\nJ2: 0,0\n", x=256, width=256, height=256))
        s.commit()
    assert b"J2: 0,0" in pm.tftp_resolve("02-00-00-00-00-99.yml", "10.0.0.1", 20004)
