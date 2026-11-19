"""Bug hunt on card chains (feat/chain): each test here PROVES a defect.

They are written to FAIL while the defect is present -- each asserts the
behaviour the design (ARCH.md §2b and the update_card comments) promises.
"""
from __future__ import annotations

import os
import sqlite3
import subprocess

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from marquee_core.models import Base, Card, Wall
from app.routers import chain, fleet



@pytest.fixture
def db(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    s = sessionmaker(eng)()
    s.add(Wall(id=1, name="w", width=512, height=256))
    s.add(Wall(id=2, name="w2", width=512, height=256))
    s.commit()
    applied = []
    monkeypatch.setattr(fleet, "_apply_mode", lambda c: applied.append((c.name, c.mode, c.program)))
    s.applied = applied
    yield s
    s.close()


def add(db, **kw):
    base = dict(wall_id=1, width=256, height=256, x=0, y=0)
    base.update(kw)
    return fleet.create_card(fleet.CardIn(**base), db)


# --- PATCH with an explicit null ------------------------------------------------

def test_patch_chain_hop_null_is_a_422_not_a_500(db):
    """CardPatch allows chain_hop=None; the column is NOT NULL."""
    c = add(db, name="a", host="10.0.0.1")
    try:
        fleet.update_card(c.id, fleet.CardPatch(chain_hop=None), db)
    except HTTPException:
        return
    except ValidationError:
        return
    except Exception as e:  # IntegrityError -> 500 in the app
        pytest.fail(f"PATCH chain_hop=null raised {type(e).__name__}: {e}")


def test_patch_host_null_is_a_422_not_a_500(db):
    c = add(db, name="a", host="10.0.0.1")
    try:
        fleet.update_card(c.id, fleet.CardPatch(host=None), db)
    except (HTTPException, ValidationError):
        return
    except Exception as e:
        pytest.fail(f"PATCH host=null raised {type(e).__name__}: {e}")


# --- head move cascade onto a host that already has members ----------------------

def test_head_move_cascade_cannot_create_duplicate_positions(db):
    # An orphaned member at 10.0.0.9 hop 1 (its head record was deleted).
    old_head = add(db, name="oldhead", host="10.0.0.9")
    add(db, name="orphan", host="10.0.0.9", chain_hop=1)
    fleet.delete_card(old_head.id, db)
    # A live chain at 10.0.0.1, then its head is renumbered to 10.0.0.9.
    h = add(db, name="head", host="10.0.0.1")
    add(db, name="m1", host="10.0.0.1", chain_hop=1)
    try:
        fleet.update_card(h.id, fleet.CardPatch(host="10.0.0.9"), db)
    except HTTPException:
        return
    at = [(c.name) for c in db.query(Card).filter(Card.host == "10.0.0.9",
                                                  Card.chain_hop == 1)]
    assert len(at) == 1, f"two records at hop 1 behind 10.0.0.9: {at}"


# --- program propagation edges -------------------------------------------------

def test_wall_mode_program_gives_every_card_the_walls_one_program(db):
    """POST /walls/{id}/mode bypasses update_card's one-program-per-wall rule."""
    add(db, name="a", host="10.0.0.1", mode="program", program="clock")
    add(db, name="b", host="10.0.0.2", x=256, mode="stream", program="ticker")
    fleet.wall_mode(1, fleet.WallMode(mode="program"), db)
    progs = {c.name: c.program for c in db.query(Card)}
    assert len(set(progs.values())) == 1, f"one wall, two programs: {progs}"


def test_moving_a_program_card_to_another_wall_adopts_that_walls_program(db):
    add(db, name="a", host="10.0.0.1", wall_id=2, mode="program", program="ticker")
    b = add(db, name="b", host="10.0.0.2", wall_id=1, mode="program", program="clock")
    fleet.update_card(b.id, fleet.CardPatch(wall_id=2, x=256), db)
    progs = {c.name: c.program for c in db.query(Card) if c.wall_id == 2}
    assert len(set(progs.values())) == 1, f"one wall, two programs: {progs}"


# --- /chains with legacy duplicate hop-0 hosts ---------------------------------------

def test_existing_duplicate_hosts_make_every_ui_edit_409(db):
    """Two hop-0 cards on one host predate _check_position (it did not exist).
    The Cards page edit form always sends host+chain_hop, so ANY edit of either
    card -- even of its width -- is now refused."""
    db.add(Card(name="a", wall_id=1, host="10.0.0.1", port=7000))
    db.add(Card(name="b", wall_id=1, host="10.0.0.1", port=7100, x=256))
    db.commit()
    b = db.query(Card).filter_by(name="b").one()
    try:
        fleet.update_card(b.id, fleet.CardPatch(host="10.0.0.1", chain_hop=0, width=128), db)
    except HTTPException as e:
        pytest.fail(f"editing only the width of a legacy card: {e.status_code} {e.detail}")


# --- input validation --------------------------------------------------------------

def test_mac_without_separators_is_rejected_or_normalised(db):
    """The TFTP lookup is `<aa-bb-..>.yml` -> aa:bb:..; a MAC stored without
    separators never matches, and the card is silently not served its config."""
    try:
        c = add(db, name="a", host="10.0.0.1", mac="02AABBCCDDEE")
    except (HTTPException, ValidationError):
        return
    assert db.get(Card, c.id).mac == "02:aa:bb:cc:dd:ee", db.get(Card, c.id).mac


def test_head_port_cannot_collide_with_a_member_pixel_port(db):
    try:
        add(db, name="h", host="10.0.0.1", port=7001)
    except (HTTPException, ValidationError):
        return
    add(db, name="m1", host="10.0.0.1", chain_hop=1)
    ports = sorted(c.pixel_port for c in db.query(Card))
    assert len(set(ports)) == 2, f"head and hop 1 both stream to udp/{ports[0]}"


# --- the migration on existing data ----------------------------------------------

def _alembic(url, *args):
    env = dict(os.environ, MARQUEE_DATABASE_URL=url)
    return subprocess.run(["alembic", *args], cwd="/app", env=env,
                          capture_output=True, text=True)


def test_migration_mac_normalisation_collision(tmp_path):
    path = tmp_path / "m.db"
    url = f"sqlite:///{path}"
    r = _alembic(url, "upgrade", "e5f7a9c21b3d")
    assert r.returncode == 0, r.stderr
    con = sqlite3.connect(path)
    con.execute("INSERT INTO walls (id, name, width, height, enabled) VALUES (1,'w',256,128,1)")
    # Two records of one card typed two ways -- legal before the migration
    # (the unique constraint is case- and separator-sensitive).
    cols = [(r[1], r[2]) for r in con.execute("PRAGMA table_info(cards)")
            if r[3] and r[4] is None and not r[5]]   # NOT NULL, no default, not pk
    for i, mac in enumerate(["02:AA:BB:CC:DD:EE", "02-aa-bb-cc-dd-ee"]):
        row = {n: (0 if "INT" in t.upper() or "FLOAT" in t.upper() or "BOOL" in t.upper()
                   else "x") for n, t in cols}
        row.update(name=f"c{i}", wall_id=1, host=f"10.0.0.{i + 1}", mac=mac,
                   state="unknown")
        con.execute(f"INSERT INTO cards ({','.join(row)}) VALUES ({','.join('?' * len(row))})",
                    list(row.values()))
    con.commit(); con.close()
    # Refused, naming the clash -- and BEFORE any schema change, so the
    # database is exactly where it was and a retry works once it is fixed.
    r = _alembic(url, "upgrade", "head")
    assert r.returncode != 0 and "02:aa:bb:cc:dd:ee" in r.stderr, r.stderr[-400:]
    con = sqlite3.connect(path)
    cols = [x[1] for x in con.execute("PRAGMA table_info(cards)")]
    assert "chain_hop" not in cols
    assert con.execute("SELECT version_num FROM alembic_version").fetchall() == [("e5f7a9c21b3d",)]
    con.execute("UPDATE cards SET mac = NULL WHERE name = 'c1'")
    con.commit(); con.close()
    r = _alembic(url, "upgrade", "head")
    assert r.returncode == 0, r.stderr[-400:]


def test_downgrade_with_members_leaves_duplicate_hosts(tmp_path):
    """Downgrade drops chain_hop: every member silently becomes a hop-0 card on
    its head's IP -- N writers on udp/7000 of the head."""
    path = tmp_path / "d.db"
    url = f"sqlite:///{path}"
    assert _alembic(url, "upgrade", "head").returncode == 0
    eng = create_engine(url)
    with sessionmaker(eng)() as s:
        s.add(Wall(id=1, name="w", width=512, height=256))
        s.add(Card(name="head", wall_id=1, host="10.0.0.1", mac="02:00:00:00:00:01"))
        s.add(Card(name="m1", wall_id=1, host="10.0.0.1", chain_hop=1, x=256,
                   mac="02:00:00:00:00:02"))
        s.commit()
    eng.dispose()
    r = _alembic(url, "downgrade", "e5f7a9c21b3d")
    # Refused while members exist, rather than leaving them as extra writers.
    assert r.returncode != 0 and "chain members" in r.stderr, r.stderr[-500:]
    eng = create_engine(url)
    with sessionmaker(eng)() as s:
        s.query(Card).filter(Card.chain_hop > 0).delete()
        s.commit()
    eng.dispose()
    assert _alembic(url, "downgrade", "e5f7a9c21b3d").returncode == 0


def test_a_member_taken_onto_the_switch_does_not_drag_its_old_chain(db):
    """A MEMBER re-cabled to the switch (hop -> 0, own IP). The cascade checks
    `c.chain_hop == 0` AFTER the patch is applied, so it treats the member as a
    head that moved and re-hosts every OTHER member of the old chain."""
    add(db, name="head", host="10.0.0.1")
    m1 = add(db, name="m1", host="10.0.0.1", chain_hop=1)
    add(db, name="m2", host="10.0.0.1", chain_hop=2)
    fleet.update_card(m1.id, fleet.CardPatch(host="10.0.0.5", chain_hop=0), db)
    where = {c.name: (c.host, c.chain_hop) for c in db.query(Card)}
    assert where["m2"] == ("10.0.0.1", 2), f"m2 was dragged off its head: {where}"


def test_one_legacy_blank_host_does_not_500_the_whole_card_list(db):
    """CardOut inherits CardIn's new host validator, so a record that was legal
    before this branch (host '') now fails on the way OUT."""
    db.add(Card(name="placeholder", wall_id=1, host=""))
    db.add(Card(name="real", wall_id=1, host="10.0.0.1", x=256))
    db.commit()
    try:
        fleet.list_cards(db)
    except ValidationError as e:
        pytest.fail(f"GET /cards -> 500: {e.errors()[0]['msg']}")


def test_member_flash_refuses_an_image_without_chain_support(tmp_path, monkeypatch):
    """A member boots only its flash copy; a pre-chain image strands it."""
    from app.routers import firmware
    monkeypatch.setattr(firmware, "_path", lambda n: str(tmp_path / n))
    (tmp_path / "old.bin").write_bytes(b"\x00" * 1024 + b'"version":"2.22.1"')
    (tmp_path / "new.bin").write_bytes(b"\x00" * 1024 + b'"chain":{"role":"')
    (tmp_path / "huge.bin").write_bytes(b'"chain":{"role":"' + b"\x00" * firmware.FW_FLASH_LEN)
    with pytest.raises(ValueError, match="no chain support"):
        firmware._fbi("old.bin")
    with pytest.raises(ValueError, match="flash firmware region"):
        firmware._fbi("huge.bin")
    assert firmware._fbi("new.bin") == "new.bin.fbi"


def test_rollout_runs_as_a_job_and_finishes(db, monkeypatch, tmp_path):
    """A rollout outlasts any HTTP timeout, so it is a job the UI polls."""
    import time as _t
    from app.routers import firmware
    from app import db as appdb
    monkeypatch.setattr(firmware, "_path", lambda n: str(tmp_path / n))
    (tmp_path / "x.bin").write_bytes(b"x")
    add(db, name="a", host="10.0.0.1")
    add(db, name="b", host="10.0.0.2", x=256)
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db)
    db.close = lambda: None
    seen = []
    monkeypatch.setattr(firmware, "assign_firmware",
                        lambda cid, body, d: seen.append(cid) or {"card": cid, "back": True})
    firmware._ROLLOUTS.clear()
    j = firmware.rollout(firmware.RolloutIn(firmware="x.bin"), db)
    for _ in range(100):
        if firmware.rollout_status(j["job"])["done"]:
            break
        _t.sleep(0.02)
    r = firmware.rollout_status(j["job"])
    assert r["done"] and r["halted"] is False and len(r["completed"]) == 2, r


def test_live_push_fingerprint_sees_card_state_but_not_last_seen(db, monkeypatch):
    """/ws pushed nothing (broadcast() had no caller); now a DB watcher does."""
    import datetime as _dt
    from app.routers import ws
    from app import db as appdb
    monkeypatch.setattr(appdb, "SessionLocal", lambda: db)
    db.close = lambda: None
    c = add(db, name="a", host="10.0.0.1")
    a = ws._fingerprint()
    row = db.get(Card, c.id)
    row.last_seen = _dt.datetime.now(_dt.timezone.utc); db.commit()
    assert ws._fingerprint() == a, "last_seen alone must not push"
    row.state = "offline"; db.commit()
    assert ws._fingerprint()[0] != a[0], "a card going offline must push"
