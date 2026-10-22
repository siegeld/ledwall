"""Amtrak Northeast Corridor board — New York Penn to Boston.

    {data:amtrak.nyp-bos|departures.0.time}          "14:05"
    {data:amtrak.nyp-bos|departures.0.destination}   "Boston South"
    {data:amtrak.nyp-bos|departures.0.train}         "2163"
    {data:amtrak.nyp-bos|departures.0.route}         "Acela"
    {data:amtrak.nyp-bos|departures.0.platform}      "7"     (often blank)
    {data:amtrak.nyp-bos|departures.0.late}          "+12m"  (blank when on time)

`custom/example/shows/amtrak.yaml` draws it.

## Why a third example, and why it shares almost nothing

`metro_north.py` and `lirr.py` are two configurations of one machinery module,
because they are the same feed format. **This one is not.** Amtrak publishes no
public GTFS-realtime feed at all — its own endpoint returns an encrypted blob —
so this reads a plain JSON API instead, and shares only `_board.py`: how a time
is written and when a countdown blanks.

That split is the point of having three. What is genuinely common between
boards is *presentation policy*, which a viewer notices when it differs. What is
not common is parsing, and a base class that tried to unify a protobuf feed
with a JSON API would be a pile of flags.

## What this feed has that the commuter feeds do not

| | MNR / LIRR | Amtrak |
|---|---|---|
| track / platform | not published at all | published, **sparsely** |
| lateness | derive it yourself | scheduled *and* actual times per stop |
| identity | two opposite conventions (see `_gtfs_rail`) | one honest `trainNum` |

**Platform is mostly blank, and that is correct.** Amtrak assigns a track at
the last minute and the feed reflects that, so a board shows a track for the
train that is boarding and nothing for the ones behind it. That is what the
real board in Penn Station does. Do not fill it in.

## Source

`api-v3.amtraker.com` — a free community API over Amtrak's own train data, no
key. The response is the **whole national network**, about 1 MB, which is why
this polls every 2 minutes rather than every 30 seconds: intercity trains do
not move between stations in 30 seconds, and there is no endpoint that returns
one corridor.

> It is a community service and not an Amtrak product. A provider that fails is
> already handled — the last good payload stays on the sign and the failure
> backs off — so an outage degrades to a stale board rather than a blank one.
"""

from __future__ import annotations

import datetime as dt
import json
import time
import urllib.request

from _board import StationBoard
from marquee_core.providers import ProviderError

# Where a train is relative to a stop, as this feed words it.
STATUS = {"Enroute": "moving", "Station": "stopped", "Departed": "departed"}

# A name that fits a sign. "Northeast Regional" is 98px at size 9 on a 128px
# wall -- three quarters of the width for a phrase every row repeats. The
# service is the thing a corridor passenger is actually choosing between, so it
# earns a column only once it is short enough to have one.
ROUTE_SHORT = {
    "northeast regional": "Regional",
    "acela": "Acela",
    "acela express": "Acela",
    "keystone service": "Keystone",
    "empire service": "Empire",
    "vermonter": "Vermonter",
    "carolinian": "Carolinian",
    "palmetto": "Palmetto",
}


def _short_route(name: str) -> str:
    """Shorten a route name for a sign, falling back to its first word.

    The fallback is what keeps this from being a lookup table somebody has to
    maintain: an unlisted route still renders something sensible rather than
    overflowing, and the table only exists for the handful where the first word
    is wrong ("Northeast" alone would be meaningless).
    """
    return ROUTE_SHORT.get(name.strip().lower(), name.split(" ")[0] if name else "")


def _iso(ts: str) -> int | None:
    """An ISO-8601 stamp with an offset -> epoch seconds.

    The feed carries a real offset per station (`-05:00`), so this never has to
    guess a timezone -- which matters on a corridor that crosses one.
    """
    if not ts:
        return None
    try:
        return int(dt.datetime.fromisoformat(ts).timestamp())
    except ValueError:
        return None


class AmtrakProvider(StationBoard):
    name = "amtrak"
    SYSTEM = "amtrak"
    interval_s = 120.0

    FEED_URL = "https://api-v3.amtraker.com/v3/trains"

    BOARD_NAME = "AMTRAK"
    # Every Penn-to-Boston departure goes to Boston, so the board shows the
    # SERVICE -- Acela or Regional -- exactly as the streamed version does.
    BOARD_DEST_FIELD = "service"
    BOARD_FROM_FIELD = "service"

    DEFAULTS = {
        **StationBoard.DEFAULTS,
        # The board's own station, and the far end of the corridor it watches.
        # Amtrak station codes: NYP New York Penn, BOS Boston South,
        # BBY Back Bay, RTE Route 128, PVD Providence, NHV New Haven,
        # STM Stamford, PHL Philadelphia, WAS Washington.
        "station": "NYP",
        "toward": "BOS",
        "line": "nyp-bos",
        # Routes to include; empty means every route that serves the corridor.
        "routes": ["Acela", "Northeast Regional"],
        # What the footer calls this corridor. The station PAIR is the honest
        # description and far too wide for a sign -- "New York Penn - Boston
        # South" is 200px of a 128px wall -- so the readable name is config and
        # the pair stays available as `line_detail`.
        "corridor": "Northeast Corridor",
        "feed_url": "",
        "tz": "America/New_York",
    }

    @property
    def feed_url(self) -> str:
        return self.config.get("feed_url") or self.FEED_URL

    # -- fetch -------------------------------------------------------------

    def _fetch(self) -> list[dict]:
        req = urllib.request.Request(
            self.feed_url, headers={"User-Agent": "marquee-amtrak-board/1.0"})
        with urllib.request.urlopen(req, timeout=float(self.config["timeout_s"])) as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict):
            raise ProviderError("unexpected response shape from the train feed")
        # Keyed by train number, each a list of runs of that number.
        return [run for runs in doc.values() for run in (runs or [])]

    # -- poll --------------------------------------------------------------

    def poll(self) -> dict:
        here = str(self.config["station"]).upper()
        far = str(self.config["toward"]).upper()
        want_routes = {r.lower() for r in (self.config.get("routes") or [])}

        trains_raw = self._fetch()
        now = int(time.time())
        horizon = now + int(self.config["horizon_min"]) * 60

        departures, arrivals, running = [], [], []
        names: dict[str, str] = {}

        for t in trains_raw:
            if want_routes and str(t.get("routeName", "")).lower() not in want_routes:
                continue
            stops = t.get("stations") or []
            codes = [s.get("code") for s in stops]
            if here not in codes or far not in codes:
                continue

            route = t.get("routeName", "")
            number = str(t.get("trainNum") or t.get("trainID") or "")
            i_here, i_far = codes.index(here), codes.index(far)

            # Flatten the calls, keeping scheduled AND actual so lateness is a
            # subtraction rather than a guess.
            def call(s):
                sch = _iso(s.get("schArr")) or _iso(s.get("schDep"))
                act = _iso(s.get("arr")) or _iso(s.get("dep")) or sch
                return {
                    "code": s.get("code", ""),
                    "stop": s.get("name", s.get("code", "")),
                    "sch": sch, "at": act,
                    "status": STATUS.get(s.get("status", ""), "scheduled"),
                    "platform": (s.get("platform") or "").strip(),
                }

            calls = [call(s) for s in stops]
            for c in calls:
                names.setdefault(c["code"], c["stop"])
            if any(c["at"] is None for c in calls):
                # A run with no usable times is not worth half-drawing.
                continue

            # The FLATTENED last call, not the raw feed entry -- those use
            # different key names, and mixing them is a KeyError mid-poll.
            terminus = calls[-1]
            mine = calls[i_here]
            delay = max(0, (mine["at"] - mine["sch"]) // 60) if mine["sch"] else 0

            if now - 120 <= mine["at"] <= horizon:
                mins = max(0, (mine["at"] - now) // 60)
                row = {
                    "train": number,
                    "route": route,
                    "service": _short_route(route),
                    "time": self.hhmm(mine["at"]),
                    "scheduled": self.hhmm(mine["sch"]) if mine["sch"] else "",
                    "epoch": mine["at"],
                    "minutes": mins,
                    "countdown": self.countdown(mins),
                    "delay_min": delay,
                    "late": self.lateness(delay),
                    # Blank for most rows, on purpose -- Amtrak assigns a track
                    # at the last minute and the feed says so.
                    "platform": mine["platform"],
                    "status": mine["status"],
                }
                # Direction is decided by where the far end sits in the call
                # list, not by a headsign: a train calling here BEFORE Boston
                # is leaving for Boston, one calling here after has come from it.
                if i_far > i_here:
                    row["destination"] = terminus["stop"]
                    row["calling_at"] = [c["stop"] for c in calls[i_here + 1:]]
                    departures.append(row)
                else:
                    row["origin"] = calls[0]["stop"]
                    row["from"] = calls[0]["stop"]
                    arrivals.append(row)

            # Running anywhere on the corridor: the next call still ahead of
            # us, where the train has already left its origin.
            nxt = next((c for c in calls if c["at"] >= now), None)
            if nxt is not None and nxt is not calls[0]:
                mins = max(0, (nxt["at"] - now) // 60)
                running.append({
                    "train": number,
                    "route": route,
                    "service": _short_route(route),
                    "destination": terminus["stop"],
                    "next_stop": nxt["stop"],
                    "next_time": self.hhmm(nxt["at"]),
                    "next_epoch": nxt["at"],
                    "minutes": mins,
                    "countdown": self.countdown(mins),
                    "status": nxt["status"],
                    "delay_min": delay,
                    "late": self.lateness(delay),
                    "terminates": self.hhmm(terminus["at"]),
                })

        # Station CODES are what the config and the feed use; a sign wants the
        # name. The feed carries both on every call, so resolve rather than
        # ship a lookup table that would go stale.
        here_name = names.get(here, here)
        far_name = names.get(far, far)

        departures.sort(key=lambda r: r["epoch"])
        arrivals.sort(key=lambda r: r["epoch"])
        running.sort(key=lambda r: r["next_epoch"])

        rows = int(self.config["rows"])
        return {self.topic: {
            "system": self.SYSTEM,
            "line": str(self.config.get("corridor") or f"{here_name} - {far_name}"),
            "line_detail": f"{here_name} - {far_name}",
            "station": here_name,
            "station_code": here,
            "toward": far_name,
            "toward_code": far,
            "updated": self.hhmm(now),
            "updated_epoch": now,
            "departures": departures[:rows],
            "arrivals": arrivals[:rows],
            "trains": running[:int(self.config["trains"])],
            "counts": {"departures": len(departures),
                       "arrivals": len(arrivals),
                       "trains": len(running)},
        }}


PROVIDER = AmtrakProvider
