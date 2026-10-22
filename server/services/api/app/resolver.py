from sqlalchemy import select

from marquee_core.models import Setting
from marquee_core.live import BusClient, LiveState
from marquee_core.providers import ProviderHost
from marquee_core.sources import Resolver

from .config import settings
from .db import SessionLocal


LIVE = LiveState()
_bus_started = False

# The api runs its own provider host so that PREVIEWS see the same data a wall
# does. The renderer is a pure function of (show, t) and the preview is the
# same code path -- that is only true if the cache behind `{data:...}` is
# populated on this side too, otherwise every preview of a data-driven show
# renders the no-value glyph and looks broken.
#
# Two hosts means two fetches of each upstream, which is the cost of the api
# and the player being separate containers. Keep provider intervals honest.
PROVIDERS = ProviderHost(LIVE)
_providers_started = False


def start_providers() -> list[str]:
    global _providers_started
    if _providers_started:
        return []
    _providers_started = True
    PROVIDERS.settings = _settings()
    return PROVIDERS.start()


def start_bus() -> None:
    global _bus_started
    if _bus_started:
        return
    import os
    # No default -- see services/player/main.py. Unset means no bus.
    url = os.environ.get("MARQUEE_BUS_URL", "")
    topics = [t.strip() for t in os.environ.get(
        "MARQUEE_BUS_TOPICS",
        "home.status").split(",") if t.strip()]
    if url:
        BusClient(url, topics, LIVE).start()
        _bus_started = True


def _settings() -> dict:
    with SessionLocal() as db:
        return {x.key: x.value for x in db.scalars(select(Setting)).all()}


def build_resolver() -> Resolver:
    with SessionLocal() as db:
        s = {x.key: x.value for x in db.scalars(select(Setting)).all()}
    cams = {k[7:].lower(): v for k, v in s.items() if k.startswith("camera.") and v}
    return Resolver(media_root=settings.media_root, cameras=cams,
                    wowza_base=s.get("wowza.base", ""))


def build_bindings(tz: str):
    """Bindings wired to Home Assistant when a token is configured.

    Settings keys: ha.base_url, ha.token. Without them {ha:...} renders the
    no-value glyph -- a missing integration must never blank a sign.
    """
    from marquee_core.bindings import Bindings, ha_history_reader, ha_reader

    s = _settings()
    base, token = s.get("ha.base_url"), s.get("ha.token")
    ha = ha_reader(base, token) if base and token else None
    # Same credentials, separate reader: history is a much heavier query with a
    # much longer useful cache life, so it gets its own cache rather than
    # sharing the 5-second one used for current states.
    hist = ha_history_reader(base, token) if base and token else None
    return Bindings(tz, ha=ha, live=LIVE, ha_history=hist)
