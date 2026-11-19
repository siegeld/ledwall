"""Long Island Rail Road board — the second worked provider.

    {data:lirr.babylon-branch|departures.0.destination}   "Babylon"
    {data:lirr.babylon-branch|departures.0.countdown}     "12m"
    {data:lirr.babylon-branch|trains.0.next_stop}         "Rockville Centre"

`custom/example/shows/lirr.yaml` draws it.

## Why a second one ships

Not to show that the plane can hold two files — that was never in doubt. It is
here because **the two MTA rail feeds use opposite identity conventions**, and
that is the kind of thing a single example hides.

| | Metro-North | LIRR |
|---|---|---|
| join on `FeedEntity.id` | **203 / 203** | **0 / 53** |
| join on `trip.trip_id` | **0 / 203** | **53 / 53** |

Both measured on the live feeds. The first version of the Metro-North provider
joined on `trip_id` and matched nothing; fixing it to use the entity id was
correct for MNR and would have been exactly wrong here. `_gtfs_rail` now
indexes vehicles under every identifier they carry and looks up by both, which
is simply correct on either system rather than a flag to get wrong.

LIRR also makes the display-name question obvious in a way MNR did not: its
entity ids look like `GO202_26_6156_T`, which is an internal schedule id and
not something to put on a sign. `vehicle.label` is the number on the timetable
on **both** systems (`3038`, `7081`), so that is what both boards show.

So the second example is not a copy. It is the reason the first one is right.

## Branches

`line` takes any `route_long_name` from the LIRR feed: Babylon Branch,
Hempstead Branch, Oyster Bay Branch, Ronkonkoma Branch, Montauk Branch, Long
Beach Branch, Far Rockaway Branch, West Hempstead Branch, Port Washington
Branch, Port Jefferson Branch, City Terminal Zone, Greenport Service.

`station` takes any stop name — Penn Station, Jamaica, Atlantic Terminal,
Grand Central, or any station on the branch.
"""

from __future__ import annotations

from _gtfs_rail import RailBoard


class LirrProvider(RailBoard):
    name = "lirr"
    SYSTEM = "lirr"
    FEED_URL = "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/lirr%2Fgtfs-lirr"
    STATIC_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfslirr.zip"
    # LIRR realtime trip ids ARE static trip ids, so trips.txt resolves a train
    # number for the half of the feed that has no vehicle yet: 107/107, for
    # 56 kB of cache. On Metro-North the same table resolves 0/201 and costs
    # 1750 kB, which is why this is per system rather than always on.
    TRIP_NAMES = True

    BOARD_NAME = "LIRR"

    DEFAULTS = {
        **RailBoard.DEFAULTS,
        "line": "Babylon Branch",
        "station": "Penn Station",
    }


PROVIDER = LirrProvider
