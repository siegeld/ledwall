"""What every station board shares, whatever the upstream.

Not a provider — the leading underscore keeps `discover` from loading it, and
`load_file` puts this directory on `sys.path` so the providers beside it can
import it.

Three boards ship here over **two unrelated upstreams**:

    _board.py        this file — presentation policy and topic naming
      _gtfs_rail.py  GTFS-realtime protobuf + static GTFS  ->  metro_north, lirr
      amtrak.py      a plain JSON API                      ->  amtrak

What belongs at this level is the part a *viewer* sees and that must not differ
between boards sitting in the same rotation: how a time is written, and when a
countdown becomes a blank. Those look trivial and are exactly the kind of rule
that silently diverges once it exists in three files — one board reading "Now"
while the next reads "0m" is the sort of thing nobody files a bug for and
everybody notices.

Parsing does NOT belong here. A GTFS feed and a JSON API have nothing in common
below the payload, and pretending otherwise produces a base class full of
flags.
"""

from __future__ import annotations

import datetime as _dt
import time
from zoneinfo import ZoneInfo

from marquee_core.providers import Provider


class StationBoard(Provider):
    """Base for a board that publishes departures, arrivals and running trains.

    Subclasses set `SYSTEM` and implement `poll`. The payload shape is a
    convention rather than an enforced schema, but keeping to it is what lets
    one show file be repointed at another board by changing the topic and
    nothing else.
    """

    SYSTEM = ""          # topic prefix: "mnr", "lirr", "amtrak"

    # -- program-mode board ------------------------------------------------
    #: The `railboard` program (colorlight custom/example/panels/railboard.yaml)
    #: is generic: it binds sys/hdr/foot and eighteen row values and has no idea
    #: which railroad it is showing. These three say how THIS board fills it.
    PROGRAMS = ("railboard",)
    BOARD_NAME = ""              # what the header says: "METRO-NORTH"
    BOARD_DEST_FIELD = "destination"
    BOARD_FROM_FIELD = "from"
    BOARD_ROWS = 4               # rows the `railboard` program has room for
    #: Characters that fit the program's destination column: it is a monospace
    #: face with an 8px advance in a 56px column. A longer name is padded so the
    #: card scrolls it -- see `_scroll_pad`.
    BOARD_DEST_CELLS = 7

    DEFAULTS = {
        "line": "",
        "station": "",
        "topic": "",            # defaults to <system>.<line slug>
        "rows": 8,              # departures/arrivals kept
        "trains": 12,           # in-progress trains kept
        "horizon_min": 180,     # ignore anything further out than this
        "tz": "America/New_York",
        "timeout_s": 25.0,
    }

    def __init__(self, config=None, cache_dir="/data/providers-cache"):
        super().__init__(config, cache_dir)
        self.tz = ZoneInfo(self.config["tz"])

    # -- addressing --------------------------------------------------------

    @property
    def topic(self) -> str:
        if self.config.get("topic"):
            return str(self.config["topic"])
        slug = str(self.config["line"]).lower().replace(" ", "-")
        return f"{self.SYSTEM}.{slug}" if slug else self.SYSTEM

    def topics(self) -> list[str]:
        return [self.topic]

    # -- presentation ------------------------------------------------------

    def hhmm(self, epoch: int) -> str:
        return _dt.datetime.fromtimestamp(epoch, self.tz).strftime("%H:%M")

    @staticmethod
    def countdown(minutes: int) -> str:
        """A ready-to-draw countdown: "Now", "7m", or nothing.

        Presentation, deliberately computed HERE rather than in the show. The
        scene language has no conditionals on purpose — it is data, not code —
        so "blank the countdown beyond an hour" cannot be expressed in a
        binding. A provider is the right place to decide it: the alternative is
        a board reading "117m", which is a number no passenger has wanted.

        The raw `minutes` is published alongside for anyone who does want it.
        """
        if minutes <= 0:
            return "Now"
        if minutes <= 60:
            return f"{minutes}m"
        return ""

    # -- values for a program-mode card ------------------------------------

    def board_pages(self, live) -> list[dict[str, str]]:
        """Three pages -- departures, arrivals, on the line -- for `railboard`.

        The same three views the streamed board cycles as scenes, except a
        program-mode card is not sent scenes: the host pushes one page's worth
        of values and the card repaints.

        EMPTY ROWS ARE PUSHED AS A SPACE, and both halves of that matter.

        Pushed at all, because a key left OUT keeps its previous value on the
        card -- on a short list that leaves yesterday's train sitting under
        today's heading, which is the worst thing a departure board can do.

        A space rather than "", because the card's value table cannot tell an
        empty string from a key that never arrived (`text()` returns `-` for
        both) and deliberately so: a program must never have to decide what
        ABSENT looks like. But an empty row on a departure board is not absent,
        it is "no train" -- an assertion the host is making. A space is how the
        host says that in a table whose only vocabulary is text.
        """
        doc = live.get(self.topic)
        if not doc:
            return []

        def fit(text: str) -> str:
            """Pad a destination so the card scrolls it, or leave it alone.

            The program decides whether to scroll by measuring the string it is
            given -- anything wider than the column marches, anything narrower
            sits still. So padding IS the instruction to scroll, and it has to
            carry its own gap: `cover_scroll` wraps the string endlessly, and an
            unpadded name would run into itself with no space between the end
            and the start.

            THREE SPACES, not a power-of-two length. `cover_scroll` can use a
            mask instead of a division when the run length is a power of two,
            which is why the platform's ticker demos pad to 64 -- but a station
            name padded that way is mostly gap: "North White Plains" is 18
            characters and would round to 32, dragging 112px of blank past a
            48px window. The column then reads as EMPTY for most of the page it
            is on, which is a worse bug than a divide is a cost.
            """
            if len(text) <= self.BOARD_DEST_CELLS:
                return text
            return text + "   "

        # THE CLOCK IS THE BOARD'S, NOT THE HOST'S. It sits beside train times
        # that are formatted in `self.tz`, so it has to be formatted the same
        # way -- the pusher's naive `datetime.now()` is the container's local
        # time, which is UTC, and the corner of the board read four hours ahead
        # of every train on it.
        now_hhmm = self.hhmm(int(time.time()))

        def page(hdr: str, rows: list, foot: str, a: str, b: str, c: str) -> dict[str, str]:
            out = {"sys": self.BOARD_NAME or self.SYSTEM.upper(), "hdr": hdr,
                   "foot": foot, "hhmm": now_hhmm}
            for i in range(self.BOARD_ROWS):
                row = rows[i] if i < len(rows) else {}
                out[f"r{i}a"] = str(row.get(a, "") or "") or " "
                out[f"r{i}b"] = fit(str(row.get(b, "") or "")) or " "
                out[f"r{i}c"] = str(row.get(c, "") or "") or " "
            return out

        station = str(doc.get("station", ""))
        line = str(doc.get("line", ""))
        return [
            page("Departures", doc.get("departures") or [], station,
                 "time", self.BOARD_DEST_FIELD, "countdown"),
            page("Arrivals", doc.get("arrivals") or [], station,
                 "time", self.BOARD_FROM_FIELD, "countdown"),
            page("On the line", doc.get("trains") or [], line,
                 "next_time", "next_stop", "countdown"),
        ]

    @staticmethod
    def lateness(delay_min: int) -> str:
        """How late, as a board would put it. Empty when it is running to time.

        A board that prints "On time" on every row spends a column saying
        nothing; the interesting state is the exception, so only the exception
        is drawn. Under five minutes is not reported at all — that is inside
        the noise of these feeds and flagging it would cry wolf.
        """
        if delay_min >= 5:
            return f"+{delay_min}m"
        return ""
