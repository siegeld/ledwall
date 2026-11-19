"""Card-chain discovery: tell every chain who is where.

Colorlight cards daisy-chain through their second Ethernet port and the whole
chain is one IP address, the head card's. A card learns its position from this
frame: UDP to the head on port 6990, payload "CH" hop=0. Each card's gateware
adds one to the hop as the frame passes through it, so the card at hop h
receives hop h -- and, from the same frame, the head's MAC and IP (it answers
as the head) and this host's address (where it fetches its config).

Sent to every distinct card host once a second. A card on its own simply
becomes the head of a chain of one, which changes nothing for it; a card that
reboots behind another learns its place within a second of its firmware
starting. The send is cheap (one 60-byte frame per host per second) and
stateless, so there is nothing to keep in sync.

Payload: "CH", hop (byte 2), version (3), 255 - hop (4), 0, then from version
2 this host's millisecond clock (6..10). Byte 4 is decremented by the gateware
as it increments the hop, which keeps the UDP checksum valid without
recomputing it.
"""
from __future__ import annotations

import logging
import socket
import threading
import time

from sqlalchemy import select

from marquee_core.models import Card

log = logging.getLogger("marquee.chain")

DISC_PORT = 6990
# Version 2: payload bytes 6..10 carry this host's millisecond clock,
# big-endian. Every card of a wall gets it within a fraction of a millisecond
# of the others, and on-card PROGRAMS run their animation from it -- that is
# what keeps one ticker moving in step across several cards.
VERSION = 2


def payload(clock_ms: int | None = None) -> bytes:
    if clock_ms is None:
        clock_ms = time.monotonic_ns() // 1_000_000
    return (b"CH" + bytes([0, VERSION, 255, 0])
            + (clock_ms & 0xFFFFFFFF).to_bytes(4, "big") + bytes(8))


PAYLOAD = payload(0)  # the layout, for tests


class ChainDiscovery(threading.Thread):
    def __init__(self, sessionmaker, period: float = 1.0):
        super().__init__(daemon=True, name="chain-discovery")
        self.sm = sessionmaker
        self.period = period
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def targets(self) -> list[str]:
        """Every enabled card host, RESOLVED. Resolution happens here, once per
        refresh, not in the send loop: a slow DNS lookup inside the loop delays
        every later send of the round, and a stale clock is exactly what the
        cards' frame sync must not be fed."""
        with self.sm() as db:
            hosts = sorted({(c.host or "").strip() for c in db.scalars(
                select(Card).where(Card.enabled))} - {""})
        out = []
        for h in hosts:
            try:
                out.append(socket.gethostbyname(h))
            except (OSError, UnicodeError, ValueError) as e:
                log.debug("chain discovery: cannot resolve %r: %s", h, e)
        return sorted(set(out))

    def run(self):
        targets: list[str] = []
        refreshed = 0.0
        failed_at = 0.0
        while True:
            try:
                now = time.monotonic()
                if now - refreshed > 10:
                    targets = self.targets()
                    refreshed = now
                for ip in targets:
                    # The clock is read per send, so each card is told the
                    # instant its packet actually leaves.
                    try:
                        self.sock.sendto(payload(), (ip, DISC_PORT))
                    except OSError as e:
                        log.debug("chain discovery to %s: %s", ip, e)
            except Exception:
                # Never die: without discovery chained cards lose their role
                # and programs lose frame sync, silently. Log at most once a
                # minute.
                if time.monotonic() - failed_at > 60:
                    log.exception("chain discovery round failed; continuing")
                    failed_at = time.monotonic()
            time.sleep(self.period)
