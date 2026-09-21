"""Player entrypoint: render+stream every wall, poll card stats, serve TFTP boot."""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

sys.path.insert(0, "/app")

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from marquee_core.models import Card, Setting, boot_config
from marquee_core.live import BusClient, LiveState
from marquee_core.providers import ProviderHost


from marquee_core.sources import Resolver
from player.player import Supervisor
from player.valuepush import ValuePusher
from tftp.tftpd import TftpBootServer

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("marquee.main")

DB = os.environ.get("MARQUEE_DATABASE_URL", "sqlite:////data/marquee.db")
TZ = os.environ.get("MARQUEE_SITE_TZ", "America/New_York")
MEDIA = os.environ.get("MARQUEE_MEDIA_ROOT", "/data/media")
FW = os.environ.get("MARQUEE_FIRMWARE_ROOT", "/data/firmware")
TFTP_PORT = int(os.environ.get("MARQUEE_TFTP_PORT", "6969"))
TFTP_ON = os.environ.get("MARQUEE_TFTP_ENABLED", "true").lower() == "true"
POLL = float(os.environ.get("MARQUEE_POLL_INTERVAL_S", "15"))

engine = create_engine(DB, connect_args={"check_same_thread": False}, future=True)
# NO create_all here. Alembic owns the schema.
#
# This used to call Base.metadata.create_all(engine), which looks harmless and
# is not. On the v0.11.0 deploy the player started before the migration ran,
# create_all saw `walls`/`cards`/`card_stats` missing and created them EMPTY,
# and `alembic upgrade head` then died with "table walls already exists" --
# leaving the real data in the old `panels` table and the app showing an empty
# fleet. create_all only ever creates; it never alters and it never populates,
# so against a migrated schema it can only race the migration and lose.
#
# The api container now runs `alembic upgrade head` before serving, so the
# schema exists before anything opens a session against it.
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


LIVE = LiveState()
# No default: a bus URL is site-specific, and an unreachable default would
# have every install retrying a host that is not theirs. Unset means the
# live-data plane is simply off -- bindings render the no-value glyph and
# nothing else changes. Set MARQUEE_BUS_URL in .env to enable it.
BUS_URL = os.environ.get("MARQUEE_BUS_URL", "")
# Data providers write into the SAME cache the bus does, so a topic is a topic
# whichever published it and `{data:...}` reads them identically. See
# marquee_core/providers.py.
PROVIDERS = ProviderHost(LIVE)
BUS_TOPICS = [t.strip() for t in os.environ.get(
    "MARQUEE_BUS_TOPICS",
    "home.status"
).split(",") if t.strip()]


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


def _settings() -> dict:
    with SessionLocal() as db:
        return {s.key: s.value for s in db.scalars(select(Setting)).all()}


def build_resolver() -> Resolver:
    """Cameras and the Wowza base come from settings, not from scene documents."""
    s = _settings()
    cams = {}
    for k, v in s.items():
        if k.startswith("camera.") and v:
            cams[k[len("camera."):].lower()] = v
    return Resolver(media_root=MEDIA, cameras=cams,
                    wowza_base=s.get("wowza.base", ""))


def _mac_for_ip(ip: str) -> str | None:
    """Resolve an IP to a MAC from the host ARP table.

    A FALLBACK since colorlight v2.3.0, not the main path any more.

    Cards on v2.3.0 or later do DHCP in the BIOS, with a MAC derived from their
    flash unique ID, so boot.bin is now requested from the card's own reserved
    address and the lookup by host above succeeds. Verified on the bench card:
    the request moved from 192.168.1.50 to 192.168.1.70.

    This stays for cards whose bitstream PREDATES that. Their BIOS fetches
    boot.bin before it has any DHCP lease, so the request arrives from the
    gateware's compiled-in fallback address -- the same address for every card
    in the fleet. The source IP therefore cannot identify the
    card, and a lookup by host silently fell through to the default firmware.
    That is how a panel whose record pinned `boot-v1.10.11-22b0a86.bin` booted
    the default instead, with nothing logged (colorlight TODO item 8).

    The player runs with host networking, so /proc/net/arp is the HOST's table
    and already holds an entry for whoever just sent us this request -- the
    incoming packet created it.
    """
    try:
        with open("/proc/net/arp") as fh:
            next(fh, None)  # header
            for line in fh:
                f = line.split()
                # f[2] is flags; 0x2 is a complete entry. An incomplete one has
                # a zero MAC and would match nothing, but checking is cheaper
                # than a pointless query.
                if len(f) >= 4 and f[0] == ip and f[2] == "0x2":
                    mac = f[3].lower()
                    return mac if mac != "00:00:00:00:00:00" else None
    except OSError:
        pass
    return None


def tftp_resolve(filename: str, client_ip: str) -> bytes | None:
    """Serve a card its firmware and its layout, by identity.

    Anything not recognised is refused: this exists to boot cards, not to serve
    files.
    """
    with SessionLocal() as db:
        card = db.scalar(select(Card).where(Card.host == client_ip))
        if filename == "boot.bin":
            if card is None:
                # Fall back to MAC identity. Without this, per-panel firmware
                # and staged rollout do not work at all: every panel looks like
                # an unknown host and gets the default image.
                mac = _mac_for_ip(client_ip)
                if mac:
                    card = db.scalar(select(Card).where(Card.mac == mac))
                    if card:
                        log.info("tftp: %s identified as %s (%s) via ARP",
                                 client_ip, card.name, mac)
                if card is None:
                    log.warning("tftp: %s not identified (arp=%s); serving default "
                                "firmware", client_ip, mac or "miss")
            name = (card.firmware if card and card.firmware else "boot.bin")
            path = os.path.join(FW, os.path.basename(name))
            if os.path.exists(path):
                with open(path, "rb") as fh:
                    blob = fh.read()
                # Say what was served, to whom. A panel booting the wrong
                # firmware is silent on the wire and on the glass.
                log.info("tftp: %s -> %s (%d bytes)%s", client_ip, name, len(blob),
                         f" for {card.name}" if card else "")
                return blob
            log.warning("tftp: no firmware at %s", path)
            return None
        if filename.endswith(".yml"):
            mac = filename[:-4].replace("-", ":").lower()
            p = card or db.scalar(select(Card).where(Card.mac == mac))
            if p and p.layout_yaml:
                # Assembled by marquee_core.boot_config, which the API also
                # serves, so what an operator reads is what the card gets.
                return boot_config(p).encode()
            log.warning("tftp: no layout for %s", filename)
            return None
    return None


def main():
    if BUS_URL:
        BusClient(BUS_URL, BUS_TOPICS, LIVE).start()
        log.info("panel-bus subscriber -> %s topics=%s", BUS_URL, ",".join(BUS_TOPICS))
    PROVIDERS.settings = _settings()
    started = PROVIDERS.start()
    log.info("data providers: %s", ", ".join(started) if started else "none")
    if TFTP_ON:
        srv = TftpBootServer(tftp_resolve, port=TFTP_PORT, root=FW)

        def _tftp_forever():
            # Restart on failure: panels cannot boot without this, so a crashed
            # thread must not silently leave the fleet unbootable.
            while True:
                try:
                    srv.serve_forever()
                except Exception:
                    log.exception("tftp server died; restarting in 5s")
                    time.sleep(5)

        threading.Thread(target=_tftp_forever, daemon=True, name="tftp").start()
        log.info("tftp boot service on :%d, firmware from %s", TFTP_PORT, FW)
    # Cards running an on-board program get VALUES, not pixels. Started
    # unconditionally: it asks each card what mode it is in and skips the ones
    # that are streaming, so there is no second setting to keep in sync.
    # A provider feeds this cache exactly as the bus does, so either one is
    # reason enough to run the pusher -- gating it on BUS_URL alone would
    # leave program-mode cards unfed on an install that uses providers only.
    if BUS_URL or started:
        ValuePusher(SessionLocal, LIVE, providers=PROVIDERS).start()
        log.info("value pusher -> program-mode cards every 10s")
    Supervisor(SessionLocal, build_resolver(), TZ, POLL, build_bindings).run()


if __name__ == "__main__":
    main()

