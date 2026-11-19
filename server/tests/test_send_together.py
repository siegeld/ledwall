"""The pacing of `send_together`, measured where it is decided: at sendto.

Arrival times on a loopback socket depend on when the test's reader thread
runs, so a delayed reader looks like a burst the sender never made. These
tests record the moment each datagram is handed to the socket instead.
"""
from __future__ import annotations

import time

from marquee_core.stream import CardStream, send_together

DELAY = 0.0004
GAP = DELAY * CardStream.SEND_BATCH


class _Sock:
    def __init__(self, log, name):
        self.log, self.name = log, name

    def sendto(self, data, addr):
        self.log.append((time.perf_counter(), self.name))

    def close(self):
        pass


def _streams(log, *heights):
    out = []
    for i, h in enumerate(heights):
        s = CardStream("127.0.0.1", 7000 + i, packet_delay=DELAY)
        s.sock.close()
        s.sock = _Sock(log, i)
        out.append((s, h))
    return out


def _send(log, *heights):
    streams = _streams(log, *heights)
    send_together([(s, s.frame_packets(bytes(256 * h * 3), 256, h))
                   for s, h in streams])
    return time.perf_counter()


def _chunks(h):
    from marquee_core.stream import BYTES_PER_CHUNK
    return -(-256 * h * 3 // BYTES_PER_CHUNK)


def _batch_starts(log, name):
    times = [t for t, n in log if n == name]
    return times[::CardStream.SEND_BATCH]


def test_a_lone_card_is_paced_as_before():
    log = []
    _send(log, 64)
    starts = _batch_starts(log, 0)
    assert len(log) == _chunks(64) > 20
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert min(gaps) >= GAP * 0.95, gaps


def test_each_card_of_a_chain_keeps_its_own_spacing():
    log = []
    _send(log, 64, 64)
    for name in (0, 1):
        starts = _batch_starts(log, name)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        assert min(gaps) >= GAP * 0.95, (name, gaps)


def test_the_cable_never_carries_two_batches_back_to_back():
    """The head's port sees every card's packets: a batch, then a wait."""
    log = []
    _send(log, 64, 64)
    times = [t for t, _ in log]
    starts = times[::CardStream.SEND_BATCH]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert min(gaps) >= GAP / 2 * 0.85, gaps


def test_a_chain_costs_the_longer_card_not_the_sum():
    log = []
    _send(log, 64, 64)
    span = log[-1][0] - log[0][0]
    one_card = (_chunks(64) // CardStream.SEND_BATCH - 1) * GAP
    assert span < one_card * 1.4, (span, one_card)


def test_a_card_left_alone_is_not_sent_at_the_rate_meant_for_two():
    log = []
    _send(log, 8, 64)
    assert _chunks(8) < _chunks(64) / 4
    t_short_done = max(t for t, n in log if n == 0)
    starts = [t for t in _batch_starts(log, 1) if t > t_short_done]
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(gaps) > 5
    assert min(gaps) >= GAP * 0.95, gaps


def test_nothing_waits_after_the_last_packet():
    log = []
    done = _send(log, 64, 64)
    assert done - log[-1][0] < GAP / 4
