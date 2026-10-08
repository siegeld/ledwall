"""The panel wire protocol.

10-byte header, little-endian, then pixels:
    0..1  magic                2..3  frame_id u16
    4     chunk_index u8       5     total_chunks u8
    6..7  width u16            8..9  height u16

Three magics, same header:

    'B','M'   RGB888, 3 bytes/px, 487 px per chunk
    'B','I'   INDEXED, 1 byte/px, 1461 px per chunk
    'B','P'   PALETTE, byte 4 is the first entry, then RGB triples

INDEXED is the fast path. The panel's bound on streaming is per-interrupt cost,
not pixels -- its CPU is already out of the pixel loop (Hub75UdpDma takes the UDP
sink directly), and the panel repo measured ~1.13 packets per ISR entry with a
~20.6 fps ceiling at 256x192, where an RGB frame is ~101 chunks. One byte per
pixel puts the SAME MTU to work on 3x the pixels, so the same frame is ~34 chunks
and the interrupt bound moves by the same 3x.

1461 is deliberate: 3 * 487, matching the panel's PIXELS_PER_CHUNK_INDEXED and
the gateware's chunk stride. NOT 1462 -- that is not divisible by three, and
using it would place every chunk after the first one pixel left of where the
hardware writes it, which shows up as shearing rather than as an error.

PALETTE is 256 entries in 768 bytes: one packet. Partial updates are legal, so
re-sending a rotated palette animates the whole wall for ~778 bytes a frame,
regardless of how many panels it spans.

Chunk N carries pixels N*PIXELS_PER_CHUNK.., sized to sit inside a 1500-byte MTU.

A frame here is one CARD's worth of pixels -- its crop of the wall canvas, with
`width`/`height` its own output size. The card has no idea it is part of
anything larger, which is what lets one wall span several boards without the
firmware changing at all.

Pacing is the whole game. A bare time.sleep() per packet drifts badly: the OS
rounds every sleep up, so a requested 0.3 ms delivers more, and the error
compounds across 68 packets. Sleep against an absolute deadline and busy-wait
the last stretch.
"""

from __future__ import annotations

import socket
import struct
import time

MAGIC = b"BM"
MAGIC_INDEXED = b"BI"
MAGIC_PALETTE = b"BP"
HEADER = struct.Struct("<2sHBBHH")
PIXELS_PER_CHUNK = 487
BYTES_PER_CHUNK = PIXELS_PER_CHUNK * 3
# 3 * PIXELS_PER_CHUNK. Must match the panel; see the module docstring for why
# this is not 1462.
PIXELS_PER_CHUNK_INDEXED = PIXELS_PER_CHUNK * 3
PALETTE_ENTRIES = 256
DEFAULT_PORT = 7000


class CardStream:
    """A UDP sender for one receiver card.

    One stream per CARD, not per wall: pacing is a property of the board (a
    card on the CPU pixel path tops out near 1250 packets/s, one on the
    gateware DMA goes far higher), and on a multi-card wall the two may differ.
    """

    def __init__(self, host: str, port: int = DEFAULT_PORT, packet_delay: float = 0.0003):
        self.host, self.port = host, port
        self.packet_delay = packet_delay
        self._frame_id = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
        self.packets_sent = 0
        self.frames_sent = 0

    def close(self):
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def send_frame(self, rgb: bytes, width: int, height: int) -> None:
        """Send an RGB888 frame ('B','M'). The compatible path."""
        expect = width * height * 3
        if len(rgb) != expect:
            raise ValueError(f"frame is {len(rgb)} bytes, expected {expect}")
        self._send_chunked(MAGIC, rgb, BYTES_PER_CHUNK, width, height)

    def send_frame_indexed(self, indices: bytes, width: int, height: int) -> None:
        """Send a palette-indexed frame ('B','I'), one byte per pixel.

        Roughly 3x fewer packets than send_frame for the same image, which is
        what moves the panel's interrupt-bound frame rate. The panel must be in
        indexed output mode and must already hold a matching palette -- send
        send_palette() first, or every index renders as a blue value.
        """
        expect = width * height
        if len(indices) != expect:
            raise ValueError(f"frame is {len(indices)} bytes, expected {expect}")
        self._send_chunked(
            MAGIC_INDEXED, indices, PIXELS_PER_CHUNK_INDEXED, width, height
        )

    def send_palette(self, palette: bytes, start: int = 0) -> None:
        """Send palette entries ('B','P') as RGB triples, starting at `start`.

        A full 256-entry palette is 768 bytes and fits one packet. Partial
        updates are legal, so re-sending a rotated palette is an ~778-byte
        animation frame for the whole wall however many panels it spans -- the
        cost does not scale with pixels.
        """
        if len(palette) % 3:
            raise ValueError(f"palette is {len(palette)} bytes, not a multiple of 3")
        entries = len(palette) // 3
        if start < 0 or start + entries > PALETTE_ENTRIES:
            raise ValueError(
                f"{entries} entries at {start} runs past the {PALETTE_ENTRIES}-entry palette"
            )
        # frame_id is not advanced: a palette write is not a frame, and bumping
        # it would make the panel treat the next real frame as a repeat.
        self.sock.sendto(
            HEADER.pack(MAGIC_PALETTE, self._frame_id, start, 1, entries, 1) + palette,
            (self.host, self.port),
        )
        self.packets_sent += 1

    def _send_chunked(
        self, magic: bytes, body: bytes, bytes_per_chunk: int, width: int, height: int
    ) -> None:
        self._frame_id = (self._frame_id + 1) & 0xFFFF
        total = (len(body) + bytes_per_chunk - 1) // bytes_per_chunk
        if total > 255:
            raise ValueError(f"{total} chunks exceeds the u8 chunk_index")
        deadline = time.perf_counter()
        for i in range(total):
            payload = body[i * bytes_per_chunk : (i + 1) * bytes_per_chunk]
            self.sock.sendto(
                HEADER.pack(magic, self._frame_id, i, total, width, height) + payload,
                (self.host, self.port),
            )
            self.packets_sent += 1
            if self.packet_delay > 0:
                deadline += self.packet_delay
                rem = deadline - time.perf_counter()
                if rem > 0.002:
                    time.sleep(rem - 0.001)
                while time.perf_counter() < deadline:
                    pass
        self.frames_sent += 1


# The name this class had until v0.11.0, when "panel" was split into the wall
# (what a viewer sees) and the card (the board driving part of it).
PanelStream = CardStream
