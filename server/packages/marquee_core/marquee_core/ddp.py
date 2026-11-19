"""DDP in: pixels pushed at us by other software become a scene layer.

DDP (Distributed Display Protocol, http://www.3waylabs.com/ddp/) is what WLED,
xLights, LedFx and friends speak to a pixel controller. This module makes the
player LOOK like one: it listens on UDP 4048, keeps the frame each sender
writes, and a `ddp` layer in a show draws it -- scaled, positioned and
composited like a video. The wall's cards are still fed by `stream.py`; nothing
here talks to a card.

The protocol, as far as a display needs it:

    byte 0   flags  V V x T S R Q P   (version 1 = 0x40)
    byte 1   sequence 1-15, 0 = unused
    byte 2   data type (0x0B = RGB, 8 bits a channel; 0x1B = RGBW)
    byte 3   ID: 1 default output, 246 control, 250 config, 251 status,
             255 every output
    4-7      byte offset into the output's buffer, MSB first
    8-9      data length, MSB first
    10-13    timecode, only when T is set
    then     data

A frame is any number of writes, and PUSH (P) means "show what you have". A
sender that never pushes is shown as it arrives, which the spec allows ("a
display not using synchronization can display data as it is received").

DISCOVERY is a status query: a packet with Q set and ID 251, unicast or
broadcast. We answer with `{"status": {...}}`, R and P set, and we announce
ourselves once at startup with `{"status":{"update":"change","state":"up"}}`.

DDP carries no geometry -- a frame is a run of bytes -- so the LAYER says how
wide the picture is. That is the one thing a sender and a show must agree on.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
import uuid

log = logging.getLogger("marquee.ddp")

PORT = 4048

VER1 = 0x40
VER_MASK = 0xC0
F_TIME = 0x10
F_STORAGE = 0x08
F_REPLY = 0x04
F_QUERY = 0x02
F_PUSH = 0x01

ID_DISPLAY = 1
ID_CONTROL = 246
ID_CONFIG = 250
ID_STATUS = 251
ID_DMX = 254
ID_ALL = 255

TYPE_RGB8 = 0x0B        # 0 0 001 011: RGB, 8 bits an element
TYPE_RGBW8 = 0x1B       # 0 0 011 011

# One output's buffer may grow to this and no further. A sender chooses the
# offset, so without a cap one packet claiming offset 0xFFFFFFFF would have us
# allocate 4 GB. 4096x1024 RGB is the largest wall this app describes.
MAX_BYTES = 4096 * 1024 * 3
# And this many outputs: IDs 1-249 are the sender's to invent.
MAX_OUTPUTS = 16
# The spec's suggested payload (480 RGB pixels), used for our replies.
MAX_DATA = 1440


def header(flags: int, ident: int, offset: int = 0, length: int = 0,
           dtype: int = 0, seq: int = 0) -> bytes:
    """A 10-byte DDP header (no timecode)."""
    return bytes([VER1 | (flags & 0x3F & ~F_TIME), seq & 0x0F, dtype & 0xFF,
                  ident & 0xFF]) + offset.to_bytes(4, "big") + length.to_bytes(2, "big")


def parse(packet: bytes):
    """(flags, seq, dtype, ident, offset, data) or None for a packet to ignore.

    `data` is exactly the declared length -- a packet shorter than its header
    claims is dropped whole rather than written short, because a truncated
    write lands the next packet's pixels in the wrong place.
    """
    if len(packet) < 10:
        return None
    flags = packet[0]
    if flags & VER_MASK != VER1:
        return None
    hl = 14 if flags & F_TIME else 10
    length = int.from_bytes(packet[8:10], "big")
    # A query's length is how much to READ; no data follows it.
    want = 0 if (flags & F_QUERY and not flags & F_REPLY) else length
    if len(packet) < hl + want:
        return None
    return (flags, packet[1] & 0x0F, packet[2], packet[3],
            int.from_bytes(packet[4:8], "big"), packet[hl:hl + want])


class _Output:
    """One DDP ID: the buffer being written and the frame last shown."""

    __slots__ = ("back", "front", "dtype", "pushed", "frames", "t", "src")

    def __init__(self):
        self.back = bytearray()
        self.front = b""
        self.dtype = TYPE_RGB8
        self.pushed = False      # has this output EVER been pushed?
        self.frames = 0
        self.t = 0.0             # monotonic time of the last frame
        self.src = ""


class Receiver:
    """The DDP display end. Thread-safe; `frame()` is what a renderer calls."""

    def __init__(self, version: str = "0", name: str = "marquee",
                 pixels: int = 0):
        self.version = version
        self.name = name
        # What a config query reports as this display's size. Informational:
        # we accept any length up to MAX_BYTES whatever this says.
        self.pixels = pixels
        self._out: dict[int, _Output] = {}
        self._lock = threading.Lock()
        self._last: dict[tuple, tuple] = {}     # sender -> last (seq, header)
        self.packets = 0
        self.ignored = 0
        self.queries = 0
        n = uuid.getnode()
        self.mac = ":".join(f"{(n >> s) & 0xFF:02x}" for s in range(40, -8, -8))
        self._sock: socket.socket | None = None

    # -- the display side ---------------------------------------------------

    def frame(self, ident: int = ID_DISPLAY, max_age: float | None = None):
        """(bytes, dtype) of the frame to show for `ident`, or None.

        None when nothing has arrived, or when the last frame is older than
        `max_age` seconds -- a sender that has gone away should uncover what
        is beneath the layer, not leave its last picture up for ever.
        """
        with self._lock:
            o = self._out.get(ident)
            if o is None or not o.t:
                return None
            if max_age is not None and time.monotonic() - o.t > max_age:
                return None
            # Never pushed: show what has arrived so far.
            return (o.front if o.pushed else bytes(o.back)), o.dtype

    def stats(self) -> dict:
        with self._lock:
            now = time.monotonic()
            return {
                "packets": self.packets, "ignored": self.ignored,
                "queries": self.queries,
                "outputs": {i: {"bytes": len(o.front if o.pushed else o.back),
                                "frames": o.frames, "from": o.src,
                                "age_s": round(now - o.t, 2) if o.t else None}
                            for i, o in self._out.items()},
            }

    # -- the wire -------------------------------------------------------------

    def handle(self, packet: bytes, addr=("", 0)) -> list[bytes]:
        """Take one datagram; return the datagrams to send back to `addr`."""
        p = parse(packet)
        if p is None:
            self.ignored += 1
            return []
        flags, seq, dtype, ident, offset, data = p
        self.packets += 1
        if flags & F_REPLY:
            # Another display's reply or announcement (ours included, when a
            # broadcast comes back). Not for us: answering would be a loop.
            return []
        if flags & F_QUERY:
            self.queries += 1
            return self._answer(ident, offset)
        if flags & F_STORAGE:
            self.ignored += 1        # "play file X from your disk": no such thing
            return []
        if seq:
            # A sender may repeat a packet for redundancy; back-to-back copies
            # carry the same sequence number and header.
            key = (seq, packet[:10])
            if self._last.get(addr) == key:
                return []
            self._last[addr] = key
            if len(self._last) > 64:
                self._last.clear()
        if ident in (ID_CONTROL, ID_CONFIG, ID_DMX):
            self.ignored += 1        # nothing here is settable over DDP
            return []
        with self._lock:
            if data and ident not in (ID_ALL, ID_STATUS, 0):
                self._write(ident, dtype, offset, data, addr)
            if flags & F_PUSH:
                now = time.monotonic()
                for i, o in self._out.items():
                    if ident in (i, ID_ALL):
                        o.front = bytes(o.back)
                        o.pushed = True
                        o.frames += 1
                        o.t = now
        return []

    def _write(self, ident, dtype, offset, data, addr):
        o = self._out.get(ident)
        if o is None:
            if len(self._out) >= MAX_OUTPUTS:
                self.ignored += 1
                return
            o = self._out[ident] = _Output()
        end = offset + len(data)
        if end > MAX_BYTES:
            self.ignored += 1
            return
        if end > len(o.back):
            o.back.extend(bytes(end - len(o.back)))
        o.back[offset:end] = data
        if dtype:
            o.dtype = dtype
        o.src = addr[0]
        if not o.pushed:
            o.t = time.monotonic()   # unsynchronised: shown as it arrives

    def status(self) -> dict:
        return {"status": {"man": "marquee", "mod": self.name,
                           "ver": self.version, "mac": self.mac,
                           "push": True, "ntp": False}}

    def config(self) -> dict:
        with self._lock:
            ports = [{"port": i, "ts": 0,
                      "l": len(o.front if o.pushed else o.back) // 3, "ss": 0}
                     for i, o in sorted(self._out.items())]
        return {"config": {"ports": ports or [
            {"port": ID_DISPLAY, "ts": 0, "l": self.pixels, "ss": 0}]}}

    def _answer(self, ident: int, offset: int) -> list[bytes]:
        if ident == ID_STATUS:
            body = json.dumps(self.status(), separators=(",", ":")).encode()
        elif ident == ID_CONFIG:
            body = json.dumps(self.config(), separators=(",", ":")).encode()
        else:
            # "If reading a device is not supported, Reply should be sent with
            # offset=0, length=0, Push flag."
            return [header(F_REPLY | F_PUSH, ident)]
        body = body[offset:offset + MAX_DATA]
        return [header(F_REPLY | F_PUSH, ident, offset, len(body)) + body]

    def announcement(self) -> bytes:
        body = b'{"status":{"update":"change","state":"up"}}'
        return header(F_REPLY | F_PUSH, ID_STATUS, 0, len(body)) + body

    # -- the socket -----------------------------------------------------------

    def start(self, port: int = PORT, bind: str = "") -> bool:
        """Bind and serve on a daemon thread. False if the port is taken --
        DDP is an extra, and its absence must never stop a wall playing."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        # A 256x512 frame is 393 kB in ~270 packets sent back to back.
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 << 20)
        except OSError:
            pass
        try:
            s.bind((bind, port))
        except OSError as e:
            log.warning("ddp: cannot bind udp/%d (%s); DDP input is off", port, e)
            s.close()
            return False
        self._sock = s
        threading.Thread(target=self._serve, daemon=True, name="ddp").start()
        try:
            s.sendto(self.announcement(), ("255.255.255.255", port))
        except OSError as e:
            log.info("ddp: startup announcement not sent (%s)", e)
        return True

    def _serve(self):
        s = self._sock
        # The spec asks a display to delay a discovery reply by the last byte
        # of its MAC, in milliseconds, so a broadcast query does not get every
        # answer at once.
        stagger = int(self.mac[-2:], 16) / 1000.0
        while True:
            try:
                packet, addr = s.recvfrom(2048)
                replies = self.handle(packet, addr)
                if replies:
                    time.sleep(stagger)
                    for r in replies:
                        s.sendto(r, addr)
            except Exception:
                log.exception("ddp: packet from %s", locals().get("addr"))
                time.sleep(0.05)


# The player has one receiver and every Renderer reads it; the API's preview
# has none and a `ddp` layer there simply draws nothing.
_default: Receiver | None = None


def set_default(r: Receiver | None) -> None:
    global _default
    _default = r


def default() -> Receiver | None:
    return _default
