"""End-to-end: one render reaches two cards, each with the correct half.

This is the test that actually proves the v0.11.0 claim. The unit tests check
that `Card.box` is the right rectangle and that crops reassemble; this runs the
REAL `WallPlayer` against a real database and two real UDP sockets, and looks at
what comes off the wire.

It is worth the machinery because the failure it guards against is invisible in
review and nearly invisible on glass: if both cards were sent `cards[0]`'s crop,
a wall of identical modules showing similar content looks plausible. Here the
left half is red and the right half is blue, so the two packets cannot be
confused.

No hardware needed -- the cards are sockets on localhost.
"""
from __future__ import annotations

import socket
import struct
import threading
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from marquee_core.models import Assignment, Base, Card, Show, Wall
from marquee_core.sources import Resolver
from marquee_core.stream import HEADER

SHOW = """
display: {width: 256, height: 128, fps: 5, format: rgb}
scenes:
  - name: halves
    duration: 10s
    layers:
      - {type: solid, color: "#ff0000", box: [0, 0, 128, 128]}
      - {type: solid, color: "#0000ff", box: [128, 0, 128, 128]}
"""


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class FakeCard:
    """A UDP socket standing in for a receiver board."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(8.0)
        self.port = self.sock.getsockname()[1]
        self.packets: list[bytes] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._rx, daemon=True)
        self._t.start()

    def _rx(self):
        while not self._stop.is_set():
            try:
                data, _ = self.sock.recvfrom(2048)
            except (socket.timeout, OSError):
                return
            self.packets.append(data)

    def close(self):
        self._stop.set()
        self.sock.close()

    def first_chunk(self) -> tuple[tuple, bytes] | None:
        """Header fields and payload of chunk 0 of any frame."""
        for pkt in self.packets:
            magic, fid, ci, total, w, h = HEADER.unpack(pkt[:HEADER.size])
            if magic == b"BM" and ci == 0:
                return (w, h, total), pkt[HEADER.size:]
        return None


@pytest.fixture
def db():
    # StaticPool and check_same_thread=False, both required: an in-memory SQLite
    # database belongs to its CONNECTION, so the default pool would hand the
    # player thread a fresh, empty database, and the player runs in a thread of
    # its own. Without these the test fails with "no such table: walls" and
    # looks like a schema bug rather than a fixture one.
    engine = create_engine("sqlite://", future=True, poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_two_cards_each_get_their_own_half(db):
    left, right = FakeCard(), FakeCard()
    try:
        with db() as s:
            show = Show(name="halves", body=SHOW)
            wall = Wall(name="w", width=256, height=128, enabled=True)
            s.add_all([show, wall]); s.flush()
            s.add_all([
                Card(name="left", wall_id=wall.id, host="127.0.0.1", port=left.port,
                     x=0, y=0, width=128, height=128, enabled=True, mode="stream",
                     packet_delay=0.0),
                Card(name="right", wall_id=wall.id, host="127.0.0.1", port=right.port,
                     x=128, y=0, width=128, height=128, enabled=True, mode="stream",
                     packet_delay=0.0),
                Assignment(wall_id=wall.id, show_id=show.id),
            ])
            s.commit()
            wall_id = wall.id

        # Imported here so the module imports cleanly even without the player
        # package on the path.
        from player.player import WallPlayer

        pl = WallPlayer(wall_id, db, Resolver(media_root="/tmp"), "UTC",
                        make_bindings=lambda tz: None)
        pl.start()
        deadline = time.time() + 15
        while time.time() < deadline and not (left.packets and right.packets):
            time.sleep(0.2)
        pl.stop_flag.set()
        pl.join(timeout=5)

        assert left.packets, "left card received nothing"
        assert right.packets, "right card received nothing"

        lh, lpx = left.first_chunk()
        rh, rpx = right.first_chunk()

        # Each card is told its OWN size, not the wall's. A card told 256x128
        # would write past its framebuffer.
        assert lh[0] == 128 and lh[1] == 128, f"left header {lh}"
        assert rh[0] == 128 and rh[1] == 128, f"right header {rh}"

        # And the pixels are its own region. This is the whole point.
        assert lpx[0:3] == bytes((0xFF, 0x00, 0x00)), f"left first pixel {lpx[0:3]!r}"
        assert rpx[0:3] == bytes((0x00, 0x00, 0xFF)), f"right first pixel {rpx[0:3]!r}"
        assert lpx[0:3] != rpx[0:3], "both cards got the same crop"
    finally:
        left.close(); right.close()


def test_program_mode_card_is_not_streamed_to(db):
    """A card drawing its own pixels must receive nothing.

    Both writers would target the same framebuffer, and the result is
    corruption rather than one of them winning.
    """
    streamed, program = FakeCard(), FakeCard()
    try:
        with db() as s:
            show = Show(name="halves", body=SHOW)
            wall = Wall(name="w", width=256, height=128, enabled=True)
            s.add_all([show, wall]); s.flush()
            s.add_all([
                Card(name="a", wall_id=wall.id, host="127.0.0.1", port=streamed.port,
                     x=0, y=0, width=128, height=128, enabled=True, mode="stream",
                     packet_delay=0.0),
                Card(name="b", wall_id=wall.id, host="127.0.0.1", port=program.port,
                     x=128, y=0, width=128, height=128, enabled=True, mode="program",
                     packet_delay=0.0),
                Assignment(wall_id=wall.id, show_id=show.id),
            ])
            s.commit()
            wall_id = wall.id

        from player.player import WallPlayer

        pl = WallPlayer(wall_id, db, Resolver(media_root="/tmp"), "UTC",
                        make_bindings=lambda tz: None)
        pl.start()
        deadline = time.time() + 12
        while time.time() < deadline and not streamed.packets:
            time.sleep(0.2)
        time.sleep(1.0)          # give the other card every chance to be wrong
        pl.stop_flag.set()
        pl.join(timeout=5)

        assert streamed.packets, "the streaming card received nothing"
        assert not program.packets, (
            f"program-mode card received {len(program.packets)} packets")
    finally:
        streamed.close(); program.close()
