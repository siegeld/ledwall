"""Data providers — the plug-in plane for live data.

A **provider** is a small piece of Python that fetches something from the
outside world and publishes it onto named **topics**. Marquee runs it on its own
thread and drops each payload into the same `LiveState` cache the panel-bus
writes to, so everything downstream works unchanged:

    {data:mnr.harlem|departures.0.destination}      text binding
    {data:mnr.harlem|departures.0.minutes}          "
    - {type: sparkline, data: "power.house|history"}

That reuse is the whole design. A provider does not learn a rendering API, a
wire format or a layer type — it returns JSON-shaped Python and the existing
binding, sparkline and preview paths pick it up.

**Providers vs `sources.py`.** Unrelated, despite the word. `sources.py`
resolves a layer's `src:` to something ffmpeg can open (`plex:`, `cam:`). A
provider supplies *values*, not media.

## Writing one

    from marquee_core.providers import Provider

    class Clock(Provider):
        name = "worldclock"
        interval_s = 30.0
        DEFAULTS = {"zones": ["Europe/London", "Asia/Tokyo"]}

        def topics(self):
            return ["worldclock.now"]

        def poll(self):
            import datetime, zoneinfo
            return {"worldclock.now": {
                z: datetime.datetime.now(zoneinfo.ZoneInfo(z)).strftime("%H:%M")
                for z in self.config["zones"]
            }}

Drop that in `custom/<site>/providers/` or `/data/providers/` and it is running
at the next player start. `doc/providers.md` is the user-facing contract.

## Rules this module enforces, and why

**A provider that fails must never blank a sign.** The same rule bindings
follow. `poll()` raising is caught, logged, counted and *the previous payload is
left in the cache* — a sign showing a three-minute-old train time is worth far
more than one showing nothing. Staleness is visible (`LiveState.age`) rather
than fatal.

**Polling here does not break the house "push, never poll" rule.** That rule is
about the *render path*, which must read memory rather than make a request — and
it still does. A provider runs on its own thread, off the render path, and a
sign redrawing 20 times a second still touches no network. A provider that has a
real push feed should override `run()` and subscribe instead; `poll()` exists
because most upstreams offer nothing better.

**A provider is trusted code.** It is imported into the player process and can
do anything that process can. There is no sandbox and this module does not
pretend to offer one: treat dropping a file into the providers directory exactly
like installing a plugin anywhere else.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import os
import sys
import threading
import time
from typing import Any

log = logging.getLogger("marquee.providers")


class ProviderError(RuntimeError):
    """Raised by a provider when a poll cannot produce data.

    Nothing catches this specially -- any exception is handled the same way.
    It exists so a provider can fail *deliberately* and readably rather than
    letting an IndexError stand in for "the feed was empty".
    """


class Provider:
    """Base class for a data provider. Subclass, set `name`, implement `poll`."""

    #: Topic namespace and identity. Must be unique across loaded providers,
    #: and is what `provider.<name>.*` settings keys are addressed by.
    name: str = ""

    #: Seconds between polls. Pick it from how fast the data actually changes:
    #: a train feed is 30s, an air-quality feed is 15 minutes. Polling faster
    #: than the upstream updates spends someone's rate limit on identical bytes.
    interval_s: float = 60.0

    #: Card PROGRAMS this provider can supply values for.
    #:
    #: A card in `mode: program` composes its own pixels from ~20 bytes/s of
    #: named values instead of being streamed to, and the on-card value table
    #: holds 24 keys -- after which `set()` IGNORES further keys rather than
    #: evicting, so an over-budget push silently loses whichever key went last.
    #: The pusher therefore cannot merge every source's values together; it
    #: sends the set that belongs to the program the card is actually running,
    #: and this is how a provider says which those are.
    PROGRAMS: tuple[str, ...] = ()

    #: Config defaults, merged under `provider.<name>.config` from settings.
    #: Ship values that make the provider work with no configuration at all --
    #: an example that needs setup before it does anything is a worse example.
    DEFAULTS: dict[str, Any] = {}

    def __init__(self, config: dict[str, Any] | None = None, cache_dir: str = "/data/providers-cache"):
        self.config = {**self.DEFAULTS, **(config or {})}
        # Somewhere to keep anything expensive to fetch and slow to change --
        # a static timetable, a geocode. Created on demand; a provider that
        # does not need it never causes the directory to exist.
        self.cache_dir = os.path.join(cache_dir, self.name or "unnamed")

    # -- the contract ------------------------------------------------------

    def topics(self) -> list[str]:
        """Topic names this provider publishes.

        Advisory: it feeds the catalogue the UI and `GET /providers` show, so
        an author can find the address to bind to without reading the source.
        Publishing a topic not listed here works; it is just undiscoverable.
        """
        return []

    def poll(self) -> dict[str, Any]:
        """Fetch once. Return `{topic: payload}`; payload must be JSON-shaped.

        Return `{}` to publish nothing this round without it counting as a
        failure -- the right answer when a feed is legitimately empty.
        """
        raise NotImplementedError

    def board_pages(self, live) -> list[dict[str, str]]:
        """Value sets for a program-mode card, one per page. `[]` by default.

        A program-mode card is not sent pixels, so a rotation cannot live in the
        show timeline -- and it must not live on the card either: rows outside
        the program's `dynamic` band are repainted only WHEN A VALUE ARRIVES, so
        a card cycling on its own frame counter would have to redraw everything
        every frame, which measures around 0.5 fps on this hardware. Making the
        page a pushed value is what keeps the repaint cheap.

        So the HOST drives the rotation: a provider returns the pages it can
        contribute and the pusher picks one per tick. Read your own topic out of
        `live` -- you know your own topic name.

        Keep each page within the 24-key table, and include every key the
        program binds: a key you omit keeps its PREVIOUS value on the card,
        which on a departures board means a stale train sitting under a fresh
        heading.
        """
        return []

    def run(self, publish, stop: threading.Event) -> None:
        """Own the loop, for a provider with a real push feed.

        Override this *instead of* `poll` to hold a websocket or a long-poll
        open and call `publish(topic, payload)` as events arrive. The default
        implementation is the polling loop, which is all most upstreams allow.
        """
        while not stop.is_set():
            started = time.monotonic()
            for topic, payload in (self.poll() or {}).items():
                publish(topic, payload)
            # Sleep the REMAINDER of the interval, not the whole of it, so a
            # slow fetch does not stretch the cadence -- a 20s poll on a 30s
            # interval should still land every 30s, not every 50s.
            stop.wait(max(1.0, self.interval_s - (time.monotonic() - started)))

    # -- helpers a provider may use ---------------------------------------

    def cache_path(self, filename: str) -> str:
        os.makedirs(self.cache_dir, exist_ok=True)
        return os.path.join(self.cache_dir, filename)

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"<Provider {self.name} every {self.interval_s:g}s>"


class ProviderHealth:
    """What a provider has been doing. Read by `GET /providers`.

    Health is kept per provider rather than per topic because the thing that
    fails is the fetch, and a provider publishing four topics off one request
    fails all four together.
    """

    def __init__(self, name: str, source: str = ""):
        self.name = name
        self.source = source          # file the provider was loaded from
        self.polls = 0
        self.failures = 0
        self.consecutive_failures = 0
        self.last_ok: float | None = None
        self.last_error: str | None = None
        self.published: list[str] = []
        self.running = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "source": self.source,
            "running": self.running,
            "polls": self.polls,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "published": sorted(self.published),
        }


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def _provider_classes(module) -> list[type[Provider]]:
    """Providers defined in a loaded module.

    An explicit `PROVIDER = MyClass` wins when present, because a module that
    defines two classes -- one of them a shared base -- would otherwise start
    the base as well. Without it, any Provider subclass *defined in this
    module* counts; subclasses imported from elsewhere are skipped so that
    `from marquee_core.providers import Provider` does not register Provider.
    """
    explicit = getattr(module, "PROVIDER", None)
    if explicit is not None:
        cls = explicit if inspect.isclass(explicit) else type(explicit)
        return [cls]
    out = []
    for obj in vars(module).values():
        if (inspect.isclass(obj) and issubclass(obj, Provider) and obj is not Provider
                and obj.__module__ == module.__name__):
            out.append(obj)
    return out


def load_file(path: str) -> list[type[Provider]]:
    """Import one .py file and return the provider classes it defines.

    The file's own directory is on `sys.path` for the duration, so a provider
    can `import _gtfs_rail` to share machinery with the provider beside it --
    which is what the `_`-prefixed skip in `discover` exists to allow. Without
    this the skip was documented and unusable: two providers over one upstream
    format had to duplicate the parsing, and duplicated parsing drifts.

    APPENDED, not prepended, so the standard library always wins. A plugin
    directory containing `json.py` must not become the `json` module for the
    whole player. That leaves helper modules sharing one namespace across
    plugin directories, so give them distinct names -- `_gtfs_rail`, not
    `_util`.

    Import failures are raised to the caller, which logs and moves on: one
    broken plugin must not stop the others from loading.
    """
    mod_name = "marquee_provider_" + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ProviderError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    here = os.path.dirname(os.path.abspath(path))
    added = here not in sys.path
    if added:
        sys.path.append(here)
    try:
        spec.loader.exec_module(module)
    finally:
        if added:
            try:
                sys.path.remove(here)
            except ValueError:
                pass
    return _provider_classes(module)


def discover(dirs: list[str]) -> list[tuple[type[Provider], str]]:
    """Find provider classes in every directory given, in order.

    Later directories override earlier ones by `name`, so a site copy of a
    shipped provider shadows the sample rather than colliding with it. Files
    starting with `_` are skipped -- that is how a plugin keeps a helper module
    beside itself without it being loaded as a provider.
    """
    found: dict[str, tuple[type[Provider], str]] = {}
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py") or fn.startswith("_"):
                continue
            path = os.path.join(d, fn)
            try:
                classes = load_file(path)
            except Exception as e:
                # A plugin with a syntax error, or one whose dependency is not
                # installed, is skipped loudly. The alternative -- letting the
                # exception out -- takes the whole player down, and every wall
                # with it, because of one file someone dropped in a directory.
                log.warning("provider %s not loaded: %s: %s", path, type(e).__name__, e)
                continue
            if not classes:
                log.warning("provider %s defines no Provider subclass", path)
            for cls in classes:
                nm = getattr(cls, "name", "") or ""
                if not nm:
                    log.warning("provider %s.%s has no name; skipped", path, cls.__name__)
                    continue
                if nm in found:
                    log.info("provider %r from %s overrides %s", nm, path, found[nm][1])
                found[nm] = (cls, path)
    return list(found.values())


def provider_dirs() -> list[str]:
    """Where providers are looked for, lowest precedence first.

    `custom/<site>/providers` is the versioned, reviewed home -- the same place
    and the same reasoning as `custom/<site>/shows`. `/data/providers` is the
    drop-in: it is a mounted volume, so a file placed there is live at the next
    restart with no image rebuild, which is what makes trying one out cheap.
    """
    site = os.environ.get("MARQUEE_SITE", "dashboard")
    override = os.environ.get("MARQUEE_PROVIDER_DIRS", "")
    if override:
        return [p.strip() for p in override.split(":") if p.strip()]
    return ["/custom/example/providers", f"/custom/{site}/providers", "/data/providers"]


# --------------------------------------------------------------------------
# running them
# --------------------------------------------------------------------------

class ProviderRunner(threading.Thread):
    """Drives one provider and publishes what it returns into LiveState."""

    def __init__(self, provider: Provider, live, health: ProviderHealth,
                 max_backoff_s: float = 300.0):
        super().__init__(daemon=True, name=f"provider-{provider.name}")
        self.provider = provider
        self.live = live
        self.health = health
        self.max_backoff_s = max_backoff_s
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def _publish(self, topic: str, payload: Any) -> None:
        self.live.put(topic, payload, owner=f"provider:{self.provider.name}")
        if topic not in self.health.published:
            self.health.published.append(topic)
        self.health.polls += 1
        self.health.last_ok = time.time()
        self.health.consecutive_failures = 0
        self.health.last_error = None

    def run(self) -> None:
        self.health.running = True
        try:
            while not self._stop.is_set():
                try:
                    self.provider.run(self._publish, self._stop)
                    # run() returning means the provider is finished -- a
                    # one-shot, or a push feed that closed cleanly. Either way
                    # there is nothing to retry immediately.
                    if self._stop.is_set():
                        break
                    self._stop.wait(self.provider.interval_s)
                except Exception as e:
                    self.health.failures += 1
                    self.health.consecutive_failures += 1
                    self.health.last_error = f"{type(e).__name__}: {e}"
                    # Back off geometrically on a feed that keeps failing. A
                    # provider retrying a dead upstream every 30s forever is
                    # how one broken plugin turns into a log nobody reads and
                    # a rate limit somebody notices.
                    delay = min(self.max_backoff_s,
                                self.provider.interval_s * (2 ** min(self.health.consecutive_failures, 6)))
                    log.warning("provider %s failed (%s); retrying in %.0fs",
                                self.provider.name, self.health.last_error, delay)
                    self._stop.wait(delay)
        finally:
            self.health.running = False


class ProviderHost:
    """Loads, configures and runs every provider. One per process."""

    def __init__(self, live, settings: dict[str, str] | None = None,
                 dirs: list[str] | None = None,
                 cache_dir: str = "/data/providers-cache"):
        self.live = live
        self.settings = settings or {}
        self.dirs = dirs if dirs is not None else provider_dirs()
        self.cache_dir = cache_dir
        self.health: dict[str, ProviderHealth] = {}
        self.runners: list[ProviderRunner] = []

    def _config_for(self, name: str) -> dict[str, Any]:
        raw = self.settings.get(f"provider.{name}.config")
        if not raw:
            return {}
        try:
            cfg = json.loads(raw)
        except ValueError:
            log.warning("provider %s: provider.%s.config is not valid JSON; ignored", name, name)
            return {}
        if not isinstance(cfg, dict):
            log.warning("provider %s: config must be a JSON object; ignored", name)
            return {}
        return cfg

    def _enabled(self, name: str) -> bool:
        # Opt-OUT, not opt-in. A provider is only present because somebody put
        # the file there, and making them then also flip a setting is a second
        # step whose only effect is a plugin that silently does nothing.
        v = self.settings.get(f"provider.{name}.enabled")
        return True if v is None else str(v).strip().lower() not in ("0", "false", "no", "off")

    def start(self) -> list[str]:
        started = []
        for cls, path in discover(self.dirs):
            name = cls.name
            health = ProviderHealth(name, source=path)
            self.health[name] = health
            if not self._enabled(name):
                health.last_error = "disabled by provider.%s.enabled" % name
                log.info("provider %s disabled", name)
                continue
            try:
                inst = cls(self._config_for(name), cache_dir=self.cache_dir)
            except Exception as e:
                health.last_error = f"{type(e).__name__}: {e}"
                log.warning("provider %s failed to construct: %s", name, health.last_error)
                continue
            runner = ProviderRunner(inst, self.live, health)
            runner.start()
            self.runners.append(runner)
            started.append(name)
            log.info("provider %s started (every %gs, from %s)", name, inst.interval_s, path)
        return started

    def stop(self) -> None:
        for r in self.runners:
            r.stop()

    @property
    def instances(self) -> list[Provider]:
        """The running provider objects, for callers that need to ask them
        something -- the value pusher asking for board pages, for instance."""
        return [r.provider for r in self.runners]

    def catalogue(self) -> list[dict[str, Any]]:
        """Health plus live staleness, for the API and the UI."""
        out = []
        for name, h in sorted(self.health.items()):
            row = h.as_dict()
            row["topics"] = [
                {"topic": t, "age_s": self.live.age(t)} for t in sorted(h.published)
            ]
            out.append(row)
        return out
