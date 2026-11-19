"""Metro-North rail board — a worked data provider.

Publishes one topic (`mnr.harlem` by default) holding everything a station
board needs: trains departing the focus station, trains arriving into it, and
every train already running on the line with its next stop and the time it
reaches there.

    {data:mnr.harlem|departures.0.time}          "13:47"
    {data:mnr.harlem|departures.0.destination}   "Southeast"
    {data:mnr.harlem|departures.0.countdown}     "6m"
    {data:mnr.harlem|trains.0.next_stop}         "White Plains"
    {data:mnr.harlem|trains.0.next_time}         "13:52"

`custom/example/shows/metro-north.yaml` draws a board from it, using the `rows`
layer so the row count comes from the feed.

**Everything real is in `_gtfs_rail.py`** — the protobuf reader, the timetable
cache, the identity join, the departures/arrivals shaping. This file is the
config, and `lirr.py` beside it is the same machinery pointed at a different
system. That is the shape a second provider over one upstream format should
have: the leading underscore keeps the shared module from being loaded as a
provider, and the loader puts this directory on `sys.path` so the import works.

## Why this is a good example

It needs nothing from the reader: the MTA's GTFS-realtime feeds are public and
take no API key, so it runs out of the box on any install with internet. It
also exercises the whole contract — config with working defaults, a
slow-changing lookup table cached on disk, a fast feed polled on an interval,
and a payload shaped for `{data:...}` paths rather than for the upstream.

## What this feed does NOT carry

**Track numbers.** MNR does not publish them in GTFS-realtime, and a board with
an empty or invented track column is worse than one without the column.
"""

from __future__ import annotations

from _gtfs_rail import RailBoard


class MetroNorthProvider(RailBoard):
    name = "metro-north"
    SYSTEM = "mnr"
    FEED_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/mnr%2Fgtfs-mnr"
    STATIC_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfsmnr.zip"

    BOARD_NAME = "METRO-NORTH"

    DEFAULTS = {
        **RailBoard.DEFAULTS,
        # Line by its GTFS `route_long_name`: Harlem, Hudson, New Haven,
        # New Canaan, Danbury, Waterbury. A bare route_id works too.
        "line": "Harlem",
        # The station the board is AT. Departures leave it, arrivals reach it.
        "station": "Grand Central",
    }


PROVIDER = MetroNorthProvider
