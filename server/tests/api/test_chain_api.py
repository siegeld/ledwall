"""Card chains, API side: what the routers must enforce.

Runs in the API image (`make be-test` runs this directory there), because the
player image -- where the rest of tests/ runs -- does not carry app/. Every
router is called directly against an in-memory database; no card is contacted
(the card calls are patched out).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from marquee_core.models import Base, Card, Wall, boot_config
from app.routers import chain, firmware, fleet


@pytest.fixture
def db(monkeypatch):
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)
    Base.metadata.create_all(eng)
    s = sessionmaker(eng)()
    s.add(Wall(id=1, name="w", width=512, height=256))
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


def test_mac_is_normalised_on_the_way_in(db):
    c = add(db, name="a", host="10.0.0.1", mac=" 02-AA-BB-CC-DD-EE ")
    assert db.get(Card, c.id).mac == "02:aa:bb:cc:dd:ee"


def test_two_cards_cannot_claim_one_chain_position(db):
    add(db, name="head", host="10.0.0.1")
    add(db, name="m1", host="10.0.0.1", chain_hop=1)
    with pytest.raises(HTTPException) as e:
        add(db, name="m1b", host="10.0.0.1", chain_hop=1)
    assert e.value.status_code == 409
    with pytest.raises(HTTPException):
        add(db, name="head2", host="10.0.0.1")          # second hop-0 card on one IP
    m2 = add(db, name="m2", host="10.0.0.1", chain_hop=2)
    with pytest.raises(HTTPException):                   # moving onto a taken hop
        fleet.update_card(m2.id, fleet.CardPatch(chain_hop=1), db)


def test_duplicate_mac_is_a_409_not_a_500(db):
    add(db, name="a", host="10.0.0.1", mac="02:00:00:00:00:01")
    with pytest.raises(HTTPException) as e:
        add(db, name="b", host="10.0.0.2", mac="02:00:00:00:00:01")
    assert e.value.status_code == 409


def test_a_head_that_moves_takes_its_chain(db):
    h = add(db, name="head", host="10.0.0.1")
    add(db, name="m1", host="10.0.0.1", chain_hop=1)
    add(db, name="m2", host="10.0.0.1", chain_hop=2)
    fleet.update_card(h.id, fleet.CardPatch(host="10.0.0.9"), db)
    hosts = {c.name: (c.host, c.chain_hop) for c in db.query(Card)}
    assert hosts == {"head": ("10.0.0.9", 0), "m1": ("10.0.0.9", 1), "m2": ("10.0.0.9", 2)}


def test_program_is_the_walls_and_mode_stays_per_card(db):
    a = add(db, name="a", host="10.0.0.1", mode="program", program="old")
    b = add(db, name="b", host="10.0.0.2", x=256, mode="program", program="old")
    c = add(db, name="c", host="10.0.0.3", y=256, mode="stream", program="stale")
    fleet.update_card(a.id, fleet.CardPatch(program="railboard"), db)
    got = {x.name: (x.mode, x.program) for x in db.query(Card)}
    assert got["a"] == ("program", "railboard") and got["b"] == ("program", "railboard")
    assert got["c"] == ("stream", "stale"), "a streaming card is left alone"
    # Rejoining program mode takes the WALL's program, over its own stale one.
    fleet.update_card(c.id, fleet.CardPatch(mode="program"), db)
    assert db.get(Card, c.id).program == "railboard"
    assert ("b", "program", "railboard") in db.applied


def test_boot_config_places_each_card(db):
    add(db, name="a", host="10.0.0.1")
    b = add(db, name="b", host="10.0.0.1", chain_hop=1, x=256)
    cfg = boot_config(db.get(Card, b.id))
    assert "wall: 512x256\norigin: 256,0\n" in cfg


class _Card(BaseHTTPRequestHandler):
    body = {}

    def do_GET(self):
        b = json.dumps(self.body.get(self.server.server_port, {})).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(b)

    def log_message(self, *a):
        pass


def _serve(port, body):
    _Card.body[port] = body
    srv = HTTPServer(("127.0.0.1", port), _Card)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _st(mac, link, role="member"):
    return {"mac": mac, "version": "4.0.1", "display_width": 128, "display_height": 128,
            "chain": {"role": role, "port1_link": link, "port1_speed": 1000}}


def test_chain_scan_walks_the_hardware(db):
    servers = [_serve(18080, _st("02:00:00:00:00:01", True, "head")),
               _serve(8001, _st("02:00:00:00:00:02", True)),
               _serve(8002, _st("02:00:00:00:00:03", False))]
    try:
        h = add(db, name="head", host="127.0.0.1", mac="02:00:00:00:00:01")
        db.get(Card, h.id).host = "127.0.0.1"
        add(db, name="m1", host="127.0.0.1", chain_hop=1, mac="02:00:00:00:00:02")
        add(db, name="stale", host="127.0.0.1", chain_hop=4, mac="02:00:00:00:00:09")
        # The head's API is on :80 in life; point the scan at the test server.
        orig = chain._status
        chain._status = lambda api, timeout=1.5: orig("127.0.0.1:18080" if api == "127.0.0.1" else api, timeout)
        r = chain.scan_chain(h.id, db)
    finally:
        chain._status = orig
        for s in servers:
            s.shutdown()
    assert [x["hop"] for x in r["hops"]] == [1, 2]
    assert r["hops"][0]["registered"]["name"] == "m1"
    assert r["hops"][1]["registered"] is None and r["hops"][1]["mac"] == "02:00:00:00:00:03"
    assert [m["name"] for m in r["missing"]] == ["stale"]


def test_a_member_runs_its_heads_image(db, monkeypatch, tmp_path):
    """A chain runs ONE image (ARCH.md §2c): a member is never assigned on its
    own, and assigning the head assigns the chain -- recorded, no flash write."""
    monkeypatch.setattr(firmware, "_root", lambda: str(tmp_path))
    (tmp_path / "e320-v9.9.9.bin").write_bytes(b'"chain":{"role":"' + b"\x01" * 64)
    h = add(db, name="head", host="10.0.0.1")
    m = add(db, name="m1", host="10.0.0.1", chain_hop=1)
    other = add(db, name="other", host="10.0.0.2", x=256)
    with pytest.raises(HTTPException) as e:
        firmware.assign_firmware(m.id, firmware.AssignIn(firmware="e320-v9.9.9.bin"), db)
    assert e.value.status_code == 409 and "head" in e.value.detail
    flashed = []
    monkeypatch.setattr(firmware, "_flash_firmware", lambda c, n, timeout_s=120: flashed.append(c.name))
    firmware.assign_firmware(h.id, firmware.AssignIn(firmware="e320-v9.9.9.bin", reboot=False), db)
    assert db.get(Card, m.id).firmware == "e320-v9.9.9.bin"
    assert firmware.chain_assignment(db.get(Card, m.id), db) == "e320-v9.9.9.bin"
    assert db.get(Card, other.id).firmware is None, "another chain is untouched"
    assert flashed == [], "an update never writes flash; the sync does, later"


def test_rollout_takes_one_entry_per_chain(db, monkeypatch, tmp_path):
    """The head brings its members; a member never appears on its own."""
    import time as _t
    from app import db as appdb
    monkeypatch.setattr(firmware, "_path", lambda n: str(tmp_path / n))
    (tmp_path / "x.bin").write_bytes(b"x")
    h = add(db, name="head", host="10.0.0.1")
    add(db, name="m1", host="10.0.0.1", chain_hop=1, y=128)
    solo = add(db, name="solo", host="10.0.0.2", x=256)
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
    assert sorted(seen) == sorted([h.id, solo.id])


def test_fbi_is_rebuilt_every_time(monkeypatch, tmp_path):
    monkeypatch.setattr(firmware, "_root", lambda: str(tmp_path))
    src = tmp_path / "x-v1.0.0.bin"
    tag = b'"chain":{"role":"'      # a chain-aware image (see _fbi)
    src.write_bytes(tag + b"A" * 100)
    n = firmware._fbi("x-v1.0.0.bin")
    src.write_bytes(tag + b"B" * 50)
    import os
    os.utime(src, (0, 0))                      # an "old" timestamp, as cp -p leaves
    firmware._fbi("x-v1.0.0.bin")
    out = (tmp_path / n).read_bytes()
    assert out[:4] == (len(tag) + 50).to_bytes(4, "little") and out[8:] == tag + b"B" * 50


# --- modules mounted turned ---------------------------------------------------

def test_rotate_is_checked_and_shapes_the_cards_box(db):
    with pytest.raises(HTTPException) as e:
        add(db, name="bad", host="10.0.0.9", width=128, height=256, rotate=45)
    assert e.value.status_code == 422
    c = add(db, name="turned", host="10.0.0.10", width=128, height=256, rotate=270)
    card = db.get(Card, c.id)
    assert card.rotate == 270 and card.box == (0, 0, 256, 128)
    with pytest.raises(HTTPException):
        fleet.update_card(c.id, fleet.CardPatch(rotate=None), db)
    with pytest.raises(HTTPException):
        fleet.update_card(c.id, fleet.CardPatch(rotate=91), db)
    fleet.update_card(c.id, fleet.CardPatch(rotate=0), db)
    assert db.get(Card, c.id).box == (0, 0, 128, 256)


def test_a_new_card_takes_its_module_types_mounting(db, monkeypatch):
    from marquee_core import models
    monkeypatch.setitem(models.PANEL_TYPES, "turned-type",
                        {**models.PANEL_TYPES["myled-p1.25-128x256-icnd1065"], "rotate": 270})
    c = add(db, name="t", host="10.0.0.11", width=128, height=256, panel_type="turned-type")
    assert db.get(Card, c.id).rotate == 270
    c = add(db, name="u", host="10.0.0.12", width=128, height=256,
            panel_type="turned-type", rotate=0)
    assert db.get(Card, c.id).rotate == 0
