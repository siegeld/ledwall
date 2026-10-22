"""Runtime value substitution inside text layers.

`{clock:%H:%M}` / `{date:%a %d %b}` / `{file:/path}` / `{http:url}` /
`{ha:sensor.x}` / `{env:NAME}` / `{data:topic|path}`.

Bindings exist so the scene language stays declarative. The alternative --
letting scenes run code -- turns a sign that several people edit over years into
an attack surface and a debugging problem. A fixed set of resolvers keeps the
document data.

Every resolver MUST be total: a failure renders a short marker rather than
raising, because a sign that goes black because a weather API timed out is worse
than one showing a stale value.
"""

from __future__ import annotations

import concurrent.futures as _f
import datetime as _dt
import os
import re
import threading as _th
import time as _time
from typing import Callable, Mapping
from zoneinfo import ZoneInfo

from .live import dig as _dig

_TOKEN = re.compile(r"\{([a-z_]+)(?::([^}]*))?\}")
_FAIL = "—"  # the house no-value glyph


def ha_reader(base_url: str, token: str, timeout: float = 3.0):
    """Return a resolver for {ha:entity_id} backed by the HA REST API.

    Cached for a few seconds: a sign may reference the same sensor in several
    layers and re-render many times a second, and none of that should turn into
    a request per frame.
    """
    import json as _json
    import time as _time
    import urllib.request as _u

    cache: dict[str, tuple[float, str]] = {}

    def read(entity: str) -> str:
        """`entity`, or `entity|attribute` for a value that is not the state.

        The attribute form exists because the numbers people actually want to
        put on a sign are often not the state. A weather entity's state is
        "sunny"; its temperature, humidity and wind are attributes. Without
        this, a banner asking for the outside temperature got the house
        no-value glyph and there was no spelling that would have worked.
        """
        entity, _, attr = entity.partition("|")
        entity, attr = entity.strip(), attr.strip()
        key = f"{entity}|{attr}"
        now = _time.time()
        hit = cache.get(key)
        if hit and now - hit[0] < 5.0:
            return hit[1]
        req = _u.Request(
            f"{base_url.rstrip('/')}/api/states/{entity}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with _u.urlopen(req, timeout=timeout) as fh:
            doc = _json.load(fh)
        if attr:
            # A missing attribute is the house no-value glyph, not the state:
            # silently showing "sunny" where a temperature was asked for is
            # worse than showing nothing.
            value = doc.get("attributes", {}).get(attr, "—")
        else:
            value = doc.get("state", "—")
        cache[key] = (now, str(value))
        return str(value)

    return read


def ha_history_reader(base_url: str, token: str, timeout: float = 8.0):
    """Return a reader for an entity's recent numeric history.

    `read(entity, hours) -> list[float]`, oldest first, non-numeric states
    dropped. HA reports `unavailable` / `unknown` as states like any other, and
    plotting those as zero would draw a cliff to the floor that looks like real
    data -- so they are skipped, leaving a shorter series rather than a lie.

    Cached for a minute. History is a far heavier query than a state read, and a
    sign re-renders many times a second; a 24-hour window does not change
    meaningfully between frames.
    """
    import json as _json
    import time as _time
    import urllib.parse as _up
    import urllib.request as _u

    cache: dict[tuple[str, float], tuple[float, list[float]]] = {}

    def read(entity: str, hours: float) -> list[float]:
        key = (entity, hours)
        now = _time.time()
        hit = cache.get(key)
        if hit and now - hit[0] < 60.0:
            return hit[1]

        start = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)
        url = (
            f"{base_url.rstrip('/')}/api/history/period/"
            f"{_up.quote(start.isoformat())}"
            f"?filter_entity_id={_up.quote(entity)}&minimal_response&no_attributes"
        )
        req = _u.Request(url, headers={"Authorization": f"Bearer {token}"})
        try:
            with _u.urlopen(req, timeout=timeout) as fh:
                doc = _json.load(fh)
        except Exception:
            # A history outage must not blank the sign; an empty series draws
            # nothing and the rest of the scene is unaffected.
            cache[key] = (now, [])
            return []

        series: list[float] = []
        for group in doc or []:
            for point in group or []:
                try:
                    series.append(float(point.get("state")))
                except (TypeError, ValueError):
                    continue
        cache[key] = (now, series)
        return series

    return read


class Bindings:
    # How long a fetched value is served without refetching, and how long a
    # STALE one is still served while its refresh is in flight before the field
    # gives up and shows the no-value glyph.
    _TTL = 10.0
    _GIVE_UP = 60.0

    def __init__(self, tz: str = "America/New_York", ha=None, http_get=None, live=None,
                 ha_history=None):
        self.tz = ZoneInfo(tz)
        self._live = live
        self._ha_get = ha
        self._http_get = http_get
        self._ha_history = ha_history
        self._cache: dict[str, tuple[float, str]] = {}
        # NETWORK FETCHES NEVER RUN ON THE RENDER THREAD.
        #
        # `_bus`/`_data` already say it: "rendering never makes a network
        # call." `_http` and `_ha` did, and it froze the wall. Measured
        # 2026-10-21 on the two-module bench: a single text layer took 609.5 ms
        # of a 50 ms frame budget, and the sender went 29 s without completing
        # a frame. `_http` cached for 10 s but a MISS did a blocking urlopen
        # with a 3 s timeout, and the banner carries one {http:} plus five
        # {ha:} bindings -- so every 10 s the wall could stall for seconds.
        #
        # On a wall this is not "a slow field", it is judder on everything
        # moving, because the whole composite waits. It is the same defect that
        # the video decoder had (a slow decode must not freeze the wall) and it
        # gets the same shape of fix: serve what we have, refresh behind.
        self._pool = _f.ThreadPoolExecutor(max_workers=4,
                                           thread_name_prefix="binding")
        self._inflight: set[str] = set()
        self._lock = _th.Lock()

    def _cached(self, key: str, fetch, empty=_FAIL):
        """Last known value for `key`, refreshing in the BACKGROUND if stale.

        Never blocks. A key with no value yet returns the no-value glyph and
        fills in on a later frame; a key whose last success is older than
        `_GIVE_UP` returns the glyph too, so a dead source shows as dead rather
        than as an indefinitely frozen number -- which is what the shows'
        own comments ask for.
        """
        now = _time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self._TTL:
            return hit[1]
        with self._lock:
            if key not in self._inflight:
                self._inflight.add(key)
                try:
                    self._pool.submit(self._refresh, key, fetch)
                except RuntimeError:
                    # Pool shut down (interpreter going away). Drop the claim
                    # so a later call can retry rather than wedging this key.
                    self._inflight.discard(key)
        if hit and now - hit[0] < self._GIVE_UP:
            return hit[1]
        return empty

    def _refresh(self, key: str, fetch) -> None:
        try:
            val = fetch()
            if val is not None:
                self._cache[key] = (_time.time(), val)
        except Exception:
            # Leave the previous value in place: _GIVE_UP decides when a
            # failing source stops being reported as a number.
            pass
        finally:
            with self._lock:
                self._inflight.discard(key)

    def history(self, entity: str, hours: float) -> list[float]:
        """Recent numeric history, or [] when HA is not configured.

        Returning [] rather than raising keeps an unconfigured or unreachable
        HA from taking down a sign that also shows a clock and a message.
        """
        if self._ha_history is None:
            return []
        # Off the render thread, same as `_http` and `_ha`. A history query is
        # the SLOWEST binding there is -- it asks Home Assistant for hours of
        # samples -- so a chart layer doing this inline is the worst version of
        # the stall measured on 2026-10-21. Returning [] until it lands is
        # already this method's documented behaviour for an unreachable HA.
        return self._cached(f"hist:{entity}:{hours}",
                            lambda: self._ha_history(entity, hours),
                            empty=[])

    # -- resolvers ---------------------------------------------------------

    def _clock(self, arg: str) -> str:
        return _dt.datetime.now(self.tz).strftime(arg or "%H:%M")

    def _date(self, arg: str) -> str:
        return _dt.datetime.now(self.tz).strftime(arg or "%a %d %b")

    def _env(self, arg: str) -> str:
        return os.environ.get(arg, _FAIL)

    def _file(self, arg: str) -> str:
        try:
            with open(arg, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read().strip()
        except OSError:
            return _FAIL

    def _http(self, arg: str) -> str:
        """{http:URL} or {http:URL|json.path} -- fetch, optionally dig into JSON.

        Cached briefly: a sign re-renders many times a second and must not turn
        that into a request per frame. Used for services with a plain HTTP API,
        e.g. {http:http://hvac.local:8400/api/zones|0.temp}
        """
        import json as _json
        import urllib.request as _u

        url, _, path = arg.partition("|")

        def fetch() -> str:
            if self._http_get:
                raw = str(self._http_get(url))
            else:
                with _u.urlopen(url, timeout=3.0) as fh:
                    raw = fh.read().decode("utf-8", "replace")
            out = raw.strip()
            if path:
                doc = _json.loads(raw)
                for part in path.split("."):
                    doc = doc[int(part)] if part.lstrip("-").isdigit() else doc[part]
                out = str(doc)
            return out

        # The 3 s timeout stays -- it just no longer happens on the render
        # thread. `_cached` serves the previous value meanwhile.
        return self._cached("http:" + arg, fetch)

    def _ha(self, arg: str) -> str:
        """Home Assistant value.

        `{ha:sensor.x}` is the entity state; `{ha:weather.dashboard|temperature}`
        is an attribute, for the many values that do not live in the state.
        """
        if not self._ha_get:
            return _FAIL
        # Off the render thread, for the reason in __init__: a Home Assistant
        # call is a network call, and five of them in one banner is five
        # chances to stall the whole composite.
        return self._cached("ha:" + arg, lambda: str(self._ha_get(arg)))

    def _bus(self, arg: str) -> str:
        """{bus:topic|dotted.path} -- read live data from the panel-bus cache.

        Purely a memory read: the bus thread keeps the cache current over a
        websocket, so rendering never makes a network call.
        """
        if not self._live:
            return _FAIL
        topic, _, path = arg.partition("|")
        doc = self._live.get(topic.strip())
        if doc is None:
            return _FAIL
        val = _dig(doc, path.strip()) if path.strip() else doc
        if val is None:
            return _FAIL
        if isinstance(val, (dict, list)):
            return str(val)[:120]
        return str(val)

    def _data(self, arg: str) -> str:
        """{data:topic|dotted.path} -- a value from the live cache.

        The same read as `{bus:...}` against the same cache, and the name a
        show should use. A topic in that cache may come from the panel-bus OR
        from a data provider (`marquee_core/providers.py`), and once it is
        cached there is no difference -- so binding by transport was always
        the wrong spelling. `{bus:...}` stays because shows in the field use
        it, and breaking the scene language is not worth a rename.
        """
        return self._bus(arg)

    def data_series(self, arg: str) -> list:
        """A LIST from the live cache -- `bus_series` under the right name."""
        return self.bus_series(arg)

    def bus_series(self, arg: str) -> list:
        """Read a LIST off the panel-bus cache, for the `sparkline` layer.

        `_bus` is the text binding and stringifies whatever it finds, which is
        useless for a series. This is the same address and the same cache read,
        returning the raw list. Anything that is not a list comes back empty,
        so a mistyped path draws a bare baseline -- the layer's own "no data"
        state -- rather than raising mid-frame and taking the sign down.
        """
        if not self._live:
            return []
        topic, _, path = arg.partition("|")
        doc = self._live.get(topic.strip())
        if doc is None:
            return []
        val = _dig(doc, path.strip()) if path.strip() else doc
        return val if isinstance(val, list) else []

    # -- entry point -------------------------------------------------------

    def resolve(self, text: str) -> str:
        def sub(m: re.Match) -> str:
            kind, arg = m.group(1), m.group(2) or ""
            fn: Callable[[str], str] | None = getattr(self, f"_{kind}", None)
            if fn is None:
                # Leave an unknown token visible rather than blanking it -- the
                # author needs to see the typo on the panel.
                return m.group(0)
            try:
                return fn(arg)
            except Exception:
                return _FAIL

        return _TOKEN.sub(sub, text)


def static_bindings(values: Mapping[str, str]) -> Bindings:
    """Bindings that resolve from a fixed dict -- used by tests and previews."""
    b = Bindings()
    for k, v in values.items():
        setattr(b, f"_{k}", (lambda val: (lambda _arg: val))(v))
    return b
