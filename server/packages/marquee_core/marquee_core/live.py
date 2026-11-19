"""Live data via panel-bus — subscribe, cache, never poll.

A panel-bus holds ONE upstream connection per feed and fans it out over a
websocket, publishing topics such as `home.status` (weather, presence, door and
window state). It replays the last snapshot the moment a client subscribes, so a
freshly started player is current immediately rather than blank until the next
event.

Configure it with MARQUEE_BUS_URL and MARQUEE_BUS_TOPICS. Unset, the live-data
plane is simply off.

Marquee subscribes once and fans out to every panel. Two reasons this beats
Marquee talking to each service itself:

  * one integration, one set of credentials, one place to fix a broken feed;
  * the render path never touches the network. A sign redrawing 20 times a
    second must read a value from memory, not make a request -- and the
    house rule is push, not poll.

Bindings read the cache: {bus:home.status|weather.temp}.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

log = logging.getLogger("marquee.live")


class LiveState:
    """Latest payload per topic. Written by the bus thread, read by renderers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}
        self._at: dict[str, float] = {}
        self._owner: dict[str, str] = {}
        self.connected = False
        self.frames = 0
        self.catalogue: list[str] = []
        self.upstream: dict[str, str] = {}
        self.last_event: dict[str, str | None] = {}

    def put(self, topic: str, payload: Any, owner: str = "bus") -> None:
        """Cache a payload for a topic.

        `owner` names the WRITER -- "bus", or "provider:<name>". Two writers on
        one topic is a misconfiguration that otherwise presents as a value
        flickering between two plausible readings, which is close to
        undiagnosable from the sign. Recording the owner makes it a line in
        the log and a field in `GET /providers` instead.
        """
        with self._lock:
            prev = self._owner.get(topic)
            if prev is not None and prev != owner:
                log.warning("topic %s written by %s and %s -- one will win at random",
                            topic, prev, owner)
            self._data[topic] = payload
            self._at[topic] = time.time()
            self._owner[topic] = owner
            self.frames += 1

    def owner(self, topic: str) -> str | None:
        with self._lock:
            return self._owner.get(topic)

    def get(self, topic: str) -> Any | None:
        with self._lock:
            return self._data.get(topic)

    def age(self, topic: str) -> float | None:
        with self._lock:
            t = self._at.get(topic)
        return None if t is None else time.time() - t

    def topics(self) -> list[str]:
        with self._lock:
            return sorted(self._data)


def dig(doc: Any, path: str) -> Any:
    """Walk a dotted path; list indices are numeric segments."""
    for part in [p for p in path.split(".") if p]:
        if doc is None:
            return None
        doc = doc[int(part)] if part.lstrip("-").isdigit() else doc.get(part)
    return doc


class BusClient(threading.Thread):
    """Maintains one websocket to panel-bus and keeps LiveState current."""

    def __init__(self, url: str, topics: list[str], state: LiveState,
                 retry_s: float = 5.0):
        super().__init__(daemon=True, name="panel-bus")
        self.url = url
        self.topics = topics
        self.state = state
        self.retry_s = retry_s
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        import websocket  # websocket-client

        while not self._stop.is_set():
            try:
                ws = websocket.create_connection(self.url, timeout=10)
                self.state.connected = True
                log.info("panel-bus connected: %s", self.url)
                for t in self.topics:
                    ws.send(json.dumps({"type": "subscribe", "topic": t}))
                ws.settimeout(30)
                last_ping = time.time()
                while not self._stop.is_set():
                    try:
                        raw = ws.recv()
                    except Exception:
                        # Idle timeout: prove the pipe is alive rather than
                        # assuming it, then keep waiting.
                        if time.time() - last_ping > 25:
                            ws.send(json.dumps({"type": "ping"}))
                            last_ping = time.time()
                        continue
                    if not raw:
                        break
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    kind = msg.get("type")
                    # panel-bus multiplexes control frames onto the same socket:
                    #   hello    -> greeting + the topic catalogue
                    #   ack      -> subscription accepted
                    #   upstream -> that feed's upstream connection state
                    #   event    -> THE DATA, in msg["data"], named by msg["name"]
                    # Caching anything but `event` overwrites a real payload
                    # with a status frame and every binding renders the
                    # no-value glyph.
                    if kind == "event" and msg.get("topic"):
                        self.state.put(msg["topic"], msg.get("data"))
                        self.state.last_event[msg["topic"]] = msg.get("name")
                    elif kind == "hello":
                        self.state.catalogue = list(msg.get("topics") or [])
                    elif kind == "upstream" and msg.get("topic"):
                        self.state.upstream[msg["topic"]] = msg.get("state")
            except Exception as e:
                log.warning("panel-bus: %s", e)
            finally:
                self.state.connected = False
            if not self._stop.is_set():
                time.sleep(self.retry_s)
