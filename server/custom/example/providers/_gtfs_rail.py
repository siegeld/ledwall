"""Shared machinery for a GTFS-realtime rail board.

Not a provider. The leading underscore keeps `discover` from loading it, and
`load_file` puts this directory on `sys.path` while a provider is imported, so
`metro_north.py` and `lirr.py` can both say `from _gtfs_rail import RailBoard`.

Two providers over one upstream format is exactly the case worth factoring:
without it the protobuf reader, the timetable cache and the departures/arrivals
shaping exist twice and drift apart the first time one is fixed.

## What a subclass supplies

    class MetroNorth(RailBoard):
        name = "metro-north"
        SYSTEM = "mnr"
        FEED_URL = "...mnr%2Fgtfs-mnr"
        STATIC_URL = "...gtfsmnr.zip"
        DEFAULTS = {**RailBoard.DEFAULTS, "line": "Harlem", "station": "Grand Central"}

That is all. Everything below is shared.

## Reading protobuf with no dependency

GTFS-realtime is protobuf and the obvious move is `gtfs-realtime-bindings`.
This reads the wire format directly instead, in about forty lines, because a
provider that needs a `pip install` is no longer something you can drop into a
directory -- and drop-in is the point of the plane.

That is safe here specifically because the wire format is self-describing
enough to walk without a schema: every field carries its number and type, and
unknown fields are skippable by design, which is the property the format exists
to guarantee. The field numbers used below are fixed by the GTFS-realtime v2.0
spec and have not changed since 2011. `_walk` was verified byte-for-byte
against `google.transit.gtfs_realtime_pb2` on both live feeds.

> **Absent fields are not zero.** GTFS-realtime's `current_status` defaults to
> `IN_TRANSIT_TO` (2), so a train with the field omitted is moving. Defaulting
> it to 0 made a tenth of the fleet look like it was pulling into a station it
> had left.

## The join, and why it is done twice

**The two MTA rail feeds use opposite identity conventions**, and a board that
picks one silently reports every train as `scheduled` on the other system:

| | Metro-North | LIRR |
|---|---|---|
| `FeedEntity.id` | `3038` — the same on both halves | `..._T` / `..._V` — differs |
| `trip.trip_id` | `3209015` vs `3038` — differs | `GO202_26_7713` — the same |
| join on entity id | **203 / 203** | **0 / 53** |
| join on trip id | **0 / 203** | **53 / 53** |

Measured on both live feeds. So vehicles are indexed under *every* identifier
they offer and looked up by both. Choosing per system would be a flag to get
wrong; indexing both is simply correct, and costs one dict insert.

`vehicle.label` is the passenger-facing train number on both (`3038`, `7081`),
which is why it is preferred over either id for display.
"""

from __future__ import annotations

import csv
import io
import json
import os
import time
import urllib.request
import zipfile

from _board import StationBoard
from marquee_core.providers import ProviderError

# --------------------------------------------------------------------------
# protobuf, read straight off the wire
# --------------------------------------------------------------------------

def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = val = 0
    while True:
        b = buf[i]
        i += 1
        val |= (b & 0x7F) << shift
        if not b & 0x80:
            return val, i
        shift += 7
        if shift > 63:
            raise ProviderError("malformed varint")


def _walk(buf: bytes) -> dict[int, list]:
    """Protobuf wire format -> {field_number: [raw values]}, no schema.

    Length-delimited fields come back as `bytes` -- a nested message, a string
    or a packed array depending on the schema, so the caller decides. Repeated
    fields are why every value is a list: `stop_time_update` appears thirty
    times in one trip and only the caller knows that is normal.
    """
    out: dict[int, list] = {}
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        field, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _varint(buf, i)
        elif wire == 1:
            v, i = buf[i:i + 8], i + 8
        elif wire == 2:
            ln, i = _varint(buf, i)
            v, i = buf[i:i + ln], i + ln
        elif wire == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            raise ProviderError(f"unsupported wire type {wire}")
        out.setdefault(field, []).append(v)
    return out


def _first(msg: dict[int, list], field: int):
    got = msg.get(field)
    return got[0] if got else None


def _str(msg: dict[int, list], field: int) -> str:
    raw = _first(msg, field)
    return raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else ""


def _sub(msg: dict[int, list], field: int) -> dict[int, list] | None:
    raw = _first(msg, field)
    return _walk(raw) if isinstance(raw, (bytes, bytearray)) else None


# GTFS-realtime v2.0 field numbers. Fixed by the spec; see the module docstring.
F_HEADER, F_ENTITY = 1, 2
H_TIMESTAMP = 3
E_ID, E_TRIP_UPDATE, E_VEHICLE = 1, 3, 4
TU_TRIP, TU_STOP_TIME_UPDATE = 1, 2
TD_TRIP_ID, TD_START_DATE, TD_ROUTE_ID = 1, 3, 5
STU_ARRIVAL, STU_DEPARTURE, STU_STOP_ID = 2, 3, 4
STE_TIME = 2
VP_TRIP, VP_CURRENT_STOP_SEQ, VP_CURRENT_STATUS, VP_STOP_ID, VP_VEHICLE = 1, 3, 4, 7, 8
VD_ID, VD_LABEL = 1, 2

# current_status. The default is IN_TRANSIT_TO, not 0 -- see the docstring.
STATUS = {0: "incoming", 1: "stopped", 2: "moving"}
STATUS_DEFAULT = 2

# Suffixes LIRR appends to distinguish the two halves of one train's entry.
# Stripped so an entity id can be tried as a join key on either system.
_ENTITY_SUFFIXES = ("_T", "_V")


def _bare(entity_id: str) -> str:
    for suf in _ENTITY_SUFFIXES:
        if entity_id.endswith(suf):
            return entity_id[:-len(suf)]
    return entity_id


class RailBoard(StationBoard):
    """A station board over any MTA GTFS-realtime rail feed."""

    # -- subclasses set these ---------------------------------------------
    SYSTEM = ""          # topic prefix, e.g. "mnr"
    FEED_URL = ""
    STATIC_URL = ""

    # Load `trips.txt` to resolve a train number for trips with no vehicle yet.
    # OFF by default because it is worth it on exactly one of the two systems:
    #
    #   LIRR  realtime trip ids ARE static trip ids -- 107/107 resolve, 56 kB
    #   MNR   they are a different id space entirely -- 0/201 resolve, 1750 kB
    #
    # So on Metro-North this would cost thirty times the cache to answer
    # nothing, while on LIRR it is the difference between a sign reading "7182"
    # and one reading "GO202_26_6158". Measured, not assumed.
    TRIP_NAMES = False

    interval_s = 30.0
    DEFAULTS = {
        **StationBoard.DEFAULTS,
        "feed_url": "",         # override the class default
        "static_url": "",
        "static_max_age_s": 7 * 86400,
    }

    def __init__(self, config=None, cache_dir="/data/providers-cache"):
        super().__init__(config, cache_dir)
        self._static: dict | None = None

    # -- addresses ---------------------------------------------------------

    @property
    def feed_url(self) -> str:
        return self.config.get("feed_url") or self.FEED_URL

    @property
    def static_url(self) -> str:
        return self.config.get("static_url") or self.STATIC_URL

    # -- the static timetable ---------------------------------------------

    def _load_static(self) -> dict:
        """`stop_id -> name` and `route_id -> name`, cached on disk.

        Reduced to a few kB of JSON rather than kept as the zip: the two files
        this needs are a fraction of a percent of a multi-megabyte archive, and
        re-reading it on every restart to pull station names out is wasted time
        and wasted disk.
        """
        path = self.cache_path("static.json")
        fresh = (os.path.exists(path)
                 and time.time() - os.path.getmtime(path) < float(self.config["static_max_age_s"]))
        if fresh:
            try:
                with open(path) as fh:
                    return json.load(fh)
            except (OSError, ValueError):
                pass  # corrupt cache: fall through and re-fetch

        with urllib.request.urlopen(self.static_url,
                                    timeout=float(self.config["timeout_s"])) as fh:
            blob = fh.read()
        zf = zipfile.ZipFile(io.BytesIO(blob))

        def rows(member):
            return csv.DictReader(io.TextIOWrapper(zf.open(member), "utf-8-sig"))

        doc = {
            "stops": {r["stop_id"]: r["stop_name"] for r in rows("stops.txt")},
            "routes": {r["route_id"]: (r.get("route_long_name")
                                       or r.get("route_short_name") or r["route_id"])
                       for r in rows("routes.txt")},
            "trips": {},
        }
        if self.TRIP_NAMES:
            doc["trips"] = {r["trip_id"]: r.get("trip_short_name", "")
                            for r in rows("trips.txt")
                            if r.get("trip_short_name")}
        try:
            with open(path, "w") as fh:
                json.dump(doc, fh)
        except OSError:
            pass  # an unwritable cache is a slow provider, not a broken one
        return doc

    def _static_maps(self) -> dict:
        if self._static is None:
            self._static = self._load_static()
        return self._static

    def _route_id(self, routes: dict[str, str]) -> str:
        want = str(self.config["line"]).strip().lower()
        for rid, nm in routes.items():
            if rid.lower() == want or nm.strip().lower() == want:
                return rid
        raise ProviderError(
            f"line {self.config['line']!r} not found; have: "
            + ", ".join(sorted(routes.values()))
        )

    def _station_ids(self, stops: dict[str, str]) -> set[str]:
        want = str(self.config["station"]).strip().lower()
        ids = {sid for sid, nm in stops.items() if nm.strip().lower() == want}
        if not ids:
            raise ProviderError(f"station {self.config['station']!r} not found in this feed")
        return ids

    # -- the realtime feed -------------------------------------------------

    def _fetch_feed(self) -> tuple[int, list[dict]]:
        req = urllib.request.Request(
            self.feed_url, headers={"User-Agent": "marquee-rail-board/1.0"})
        with urllib.request.urlopen(req, timeout=float(self.config["timeout_s"])) as fh:
            raw = fh.read()
        if not raw:
            raise ProviderError("empty response from the realtime feed")
        feed = _walk(raw)
        header = _sub(feed, F_HEADER) or {}
        ts = _first(header, H_TIMESTAMP) or int(time.time())
        return int(ts), [_walk(e) for e in feed.get(F_ENTITY, [])]

    def _vehicles(self, entities, stops) -> dict[str, dict]:
        """Index every vehicle under EVERY identifier it offers.

        See the module docstring: Metro-North joins on the entity id and LIRR
        on the trip id, so keying by one reports every train on the other
        system as `scheduled` with nothing in the log to say why.
        """
        out: dict[str, dict] = {}
        for ent in entities:
            vp = _sub(ent, E_VEHICLE)
            if not vp:
                continue
            trip = _sub(vp, VP_TRIP) or {}
            desc = _sub(vp, VP_VEHICLE) or {}
            rec = {
                "status": STATUS.get(_first(vp, VP_CURRENT_STATUS)
                                     if VP_CURRENT_STATUS in vp else STATUS_DEFAULT,
                                     "moving"),
                "at_stop": stops.get(_str(vp, VP_STOP_ID), ""),
                # The number on the timetable, on both systems.
                "label": _str(desc, VD_LABEL),
            }
            for key in (_bare(_str(ent, E_ID)), _str(trip, TD_TRIP_ID)):
                if key:
                    out[key] = rec
        return out

    # -- poll --------------------------------------------------------------

    def poll(self) -> dict:
        maps = self._static_maps()
        stops, routes = maps["stops"], maps["routes"]
        trip_names = maps.get("trips") or {}
        route_id = self._route_id(routes)
        station_ids = self._station_ids(stops)
        feed_ts, entities = self._fetch_feed()
        now = int(time.time())
        horizon = now + int(self.config["horizon_min"]) * 60
        vehicles = self._vehicles(entities, stops)

        departures, arrivals, trains = [], [], []

        for ent in entities:
            tu = _sub(ent, E_TRIP_UPDATE)
            if not tu:
                continue
            trip = _sub(tu, TU_TRIP) or {}
            if _str(trip, TD_ROUTE_ID) != route_id:
                continue
            trip_id = _str(trip, TD_TRIP_ID)
            entity_id = _bare(_str(ent, E_ID))
            veh = vehicles.get(entity_id) or vehicles.get(trip_id) or {}
            # The number on the timetable, best source first:
            #   1. the live vehicle's label -- present once a train is running;
            #   2. the static timetable's trip_short_name, which covers trips
            #      that have not been dispatched yet (LIRR only, see TRIP_NAMES);
            #   3. whichever id this system uses publicly, as a last resort.
            # Without (2) an LIRR board shows "GO202_26_6158" for every train
            # still an hour out, which is an internal schedule id and not
            # something to put on a sign.
            train_no = (veh.get("label") or trip_names.get(trip_id)
                        or entity_id or trip_id)

            # Flatten this trip's remaining calls, in order.
            calls = []
            for raw in tu.get(TU_STOP_TIME_UPDATE, []):
                stu = _walk(raw)
                sid = _str(stu, STU_STOP_ID)
                arr = _sub(stu, STU_ARRIVAL) or {}
                dep = _sub(stu, STU_DEPARTURE) or {}
                at = _first(arr, STE_TIME) or _first(dep, STE_TIME)
                if not sid or not at:
                    continue
                calls.append({
                    "stop_id": sid,
                    "stop": stops.get(sid, sid),
                    "arrival": int(_first(arr, STE_TIME) or at),
                    "departure": int(_first(dep, STE_TIME) or at),
                })
            if not calls:
                continue

            terminus = calls[-1]

            # -- does it call at our station, and in which direction? -------
            #
            # The LAST call being our station is what makes it an arrival; any
            # earlier call is a departure. Testing "is our station in the list"
            # alone would file every arriving train as a departure too, since a
            # train terminating here does call here.
            for idx, call in enumerate(calls):
                if call["stop_id"] not in station_ids:
                    continue
                when = call["departure"] if idx < len(calls) - 1 else call["arrival"]
                if when < now - 60 or when > horizon:
                    break
                mins = max(0, (when - now) // 60)
                row = {
                    "train": train_no,
                    "trip_id": trip_id,
                    "time": self.hhmm(when),
                    "epoch": when,
                    "minutes": mins,
                    "countdown": self.countdown(mins),
                    "status": veh.get("status", "scheduled"),
                }
                if idx < len(calls) - 1:
                    row["destination"] = terminus["stop"]
                    # Where it stops between here and there -- the "calling at"
                    # line a station board carries under the destination.
                    row["calling_at"] = [c["stop"] for c in calls[idx + 1:]]
                    departures.append(row)
                else:
                    row["origin"] = calls[0]["stop"]
                    row["from"] = calls[0]["stop"]
                    arrivals.append(row)
                break

            # -- in progress anywhere on the line ---------------------------
            #
            # The next call is the first one still ahead of us. If that is not
            # the trip's own first call, the train has already left its origin
            # and is running -- which is exactly "in progress on the line".
            nxt = next((c for c in calls if c["arrival"] >= now), None)
            if nxt is not None and nxt is not calls[0]:
                mins = max(0, (nxt["arrival"] - now) // 60)
                trains.append({
                    "train": train_no,
                    "trip_id": trip_id,
                    "destination": terminus["stop"],
                    "next_stop": nxt["stop"],
                    "next_time": self.hhmm(nxt["arrival"]),
                    "next_epoch": nxt["arrival"],
                    "minutes": mins,
                    "countdown": self.countdown(mins),
                    "status": veh.get("status", "scheduled"),
                    "at_stop": veh.get("at_stop", ""),
                    "terminates": self.hhmm(terminus["arrival"]),
                })

        departures.sort(key=lambda r: r["epoch"])
        arrivals.sort(key=lambda r: r["epoch"])
        trains.sort(key=lambda r: r["next_epoch"])

        rows = int(self.config["rows"])
        return {self.topic: {
            "system": self.SYSTEM,
            "line": routes.get(route_id, route_id),
            "station": self.config["station"],
            "updated": self.hhmm(feed_ts),
            "updated_epoch": feed_ts,
            # The MTA feed timestamp runs a minute or two behind real time, so
            # it must NOT be drawn where a viewer reads it as the clock -- it
            # sits beside a live countdown and makes both look wrong. Published
            # as an age instead.
            "feed_age_min": max(0, (now - feed_ts) // 60),
            "departures": departures[:rows],
            "arrivals": arrivals[:rows],
            "trains": trains[:int(self.config["trains"])],
            "counts": {"departures": len(departures),
                       "arrivals": len(arrivals),
                       "trains": len(trains)},
        }}
