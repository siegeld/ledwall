"""Sample receiver-card counters into the stats table.

Runs in the player service. Every sample is compared with the previous one for
that card to derive rates; a counter that went backwards means the card
rebooted between samples, which is recorded rather than smoothed away -- an
unexplained reboot is exactly what an operator wants to see in a history graph.
"""

from __future__ import annotations

import datetime as dt
import json
import urllib.request

from .models import Card, CardState, CardStat

FIELDS = ("isr_count", "refresh_count", "bitmap_frames", "dma_pixels",
          "dma_chunks", "mac_overflow", "mac_crc_errors")


def fetch(card: Card, timeout: float = 4.0) -> dict | None:
    try:
        with urllib.request.urlopen(
            f"http://{card.host}/api/status", timeout=timeout
        ) as fh:
            return json.load(fh)
    except Exception:
        return None


def sample(card: Card, prev: CardStat | None = None, now: dt.datetime | None = None) -> CardStat:
    now = now or dt.datetime.now(dt.timezone.utc)
    data = fetch(card)
    if data is None:
        card.state = CardState.offline
        return CardStat(card_id=card.id, at=now, reachable=False)

    card.state = CardState.online
    card.last_seen = now
    st = CardStat(card_id=card.id, at=now, reachable=True)
    for f in FIELDS:
        v = data.get(f)
        setattr(st, f, int(v) if isinstance(v, (int, float)) else None)

    if prev and prev.reachable and prev.at:
        dtsec = (now - prev.at).total_seconds()
        if dtsec > 0:
            back = any(
                getattr(st, f) is not None and getattr(prev, f) is not None
                and getattr(st, f) < getattr(prev, f)
                for f in ("isr_count", "refresh_count")
            )
            st.rebooted = back
            if not back:
                if st.bitmap_frames is not None and prev.bitmap_frames is not None:
                    st.fps = (st.bitmap_frames - prev.bitmap_frames) / dtsec
                if st.refresh_count is not None and prev.refresh_count is not None:
                    st.refresh_hz = (st.refresh_count - prev.refresh_count) / dtsec
    return st
