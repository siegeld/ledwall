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

import datetime as _dt
import os
import re
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
        now = _time.time()
        hit = cache.get(entity)
        if hit and now - hit[0] < 5.0:
            return hit[1]
        req = _u.Request(
            f"{base_url.rstrip('/')}/api/states/{entity}",
            headers={"Authorization": f"Bearer {token}"},
        )
        with _u.urlopen(req, timeout=timeout) as fh:
            state = _json.load(fh).get("state", "—")
        cache[entity] = (now, str(state))
        return str(state)

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
    def __init__(self, tz: str = "America/New_York", ha=None, http_get=None, live=None,
                 ha_history=None):
        self.tz = ZoneInfo(tz)
        self._live = live
        self._ha_get = ha
        self._http_get = http_get
        self._ha_history = ha_history
        self._cache: dict[str, tuple[float, str]] = {}

    def history(self, entity: str, hours: float) -> list[float]:
        """Recent numeric history, or [] when HA is not configured.

        Returning [] rather than raising keeps an unconfigured or unreachable
        HA from taking down a sign that also shows a clock and a message.
        """
        if self._ha_history is None:
            return []
        try:
            return self._ha_history(entity, hours)
        except Exception:
            return []

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
        import time as _time
        import urllib.request as _u

        url, _, path = arg.partition("|")
        now = _time.time()
        hit = self._cache.get(arg)
        if hit and now - hit[0] < 10.0:
            return hit[1]
        try:
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
            self._cache[arg] = (now, out)
            return out
        except Exception:
            return _FAIL

    def _ha(self, arg: str) -> str:
        """Home Assistant entity state, e.g. {ha:sensor.outside_temperature}."""
        if not self._ha_get:
            return _FAIL
        try:
            return str(self._ha_get(arg))
        except Exception:
            return _FAIL

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
