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
A frame over 255 chunks goes as 'B','W' instead -- u16 counters in a 12-byte
header and PIXELS_PER_CHUNK_WIDE pixels a chunk, so it still fits.

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

import os
import socket
import struct
import time

MAGIC = b"BM"
MAGIC_INDEXED = b"BI"
MAGIC_PALETTE = b"BP"
HEADER = struct.Struct("<2sHBBHH")

# 'B','W' -- the same frame, with WIDE chunk counters.
#
# HEADER's chunk_index and total are u8, so a frame may not exceed 255 chunks
# = 372,555 bytes = 124,185 RGB pixels. That is under four P1.25 panels: one
# card driving 4 x (256x128) is 131,072 px = 270 chunks, and the frame simply
# cannot be expressed. The card rejects a short frame too, so trimming rows is
# not a way round it.
#
# MAGIC_WIDE carries the same RGB888 pixels with u16 counters in a 12-byte
# header. It is a SEPARATE magic on purpose: the existing 'BM' path stays
# byte-for-byte identical, so cards running older firmware -- the bench
# 5A-75E among them -- are untouched and keep working.
#
# Its chunk is ONE PIXEL SHORTER (PIXELS_PER_CHUNK_WIDE). 487 px fills the
# MTU exactly behind the 10-byte header: 10 + 1461 + 8 UDP + 20 IP = 1499.
# Behind 12 bytes it was 1501, so the kernel split every wide chunk into two
# IP fragments, the card parses only the first, and the last pixel of every
# chunk never landed -- a fixed diagonal lattice of stale pixels with every
# card counter clean. Measured on the E320 at 256x512, 2026-10-26. Must match
# the card: gateware/dma_writer.py `chunk_pixels`, bitmap_udp.rs
# PIXELS_PER_CHUNK_WIDE (colorlight gateware 1.46.0 / firmware 3.9.0).
MAGIC_WIDE = b"BW"
HEADER_WIDE = struct.Struct("<2sHHHHH")
MAX_CHUNKS_NARROW = 255
PIXELS_PER_CHUNK = 487
BYTES_PER_CHUNK = PIXELS_PER_CHUNK * 3
PIXELS_PER_CHUNK_WIDE = PIXELS_PER_CHUNK - 1
BYTES_PER_CHUNK_WIDE = PIXELS_PER_CHUNK_WIDE * 3
# Largest UDP payload that leaves the IP datagram inside a 1500-byte MTU.
# Checked at import, because the failure it prevents is silent on the card.
MAX_UDP_PAYLOAD = 1500 - 20 - 8
assert HEADER.size + BYTES_PER_CHUNK <= MAX_UDP_PAYLOAD
assert HEADER_WIDE.size + BYTES_PER_CHUNK_WIDE <= MAX_UDP_PAYLOAD
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

    # Floor on the gap between two consecutive sends, in seconds.
    #
    # `packet_delay` is the TARGET spacing; this is the hard minimum the loop
    # will go down to when it has fallen behind. Without it, a late packet is
    # sent the instant the loop notices, which is a back-to-back pair on the
    # wire -- and measured 2026-10-21, those pairs are not spread evenly. They
    # cluster in the first dozen chunks of every frame, where the loop is still
    # cold, and that is exactly where the card loses them:
    #
    #   chunks    gaps < 100us (wire)     share of the card's lost chunks
    #   0-3            0.6%                        6.6%
    #   4-7            8.1%                       29.6%
    #   8-11           4.2%                       22.4%
    #   12-67          ~0.0%                      ~41% spread over 56 chunks
    #
    # 6000 packets captured at the host, `miss_buckets` read from the card over
    # the same show. The two distributions agree, which makes OUR OWN sender
    # the burst source -- not the switch, which is what the evidence in RB-5602
    # had pointed to.
    #
    # 200 us caps the instantaneous rate at 5000 packets/s, about 7.3 MB/s,
    # far under the card's measured 24 MB/s drain. It costs nothing in the
    # normal case because it is below `packet_delay` and so never binds there;
    # when it does bind the frame still finishes SOONER than fully-paced,
    # because the floor is the smaller interval. There is no trade here.
    MIN_SEND_GAP = 0.0002

    # How many packets go out between pacing waits.
    #
    # The per-packet cost of this loop is NOT the work -- measured on the host,
    # slicing the body is 0.32 us, packing the header 0.13, the concat 0.41 and
    # sendto 4.92, about 5.9 us all told. It is `time.sleep`, which overshoots a
    # 200 us request by a median of 53 us and up to 75. At one sleep per packet
    # that is ~10.5 ms added to a 40.5 ms frame, so 135 chunks take 51 ms
    # against a 50 ms budget at 20 fps.
    #
    # The consequence is not a stall -- delivery is even, 65 wire-measured
    # intervals with none over 70 ms -- it is a DEFICIT: 19.58 fps delivered
    # against 20.0 rendered is one frame short every 2.4 s, and the renderer
    # snaps motion to whole pixels per frame, so each missing frame is a double
    # step. That is the "glitch every couple of seconds".
    #
    # Pacing every SECOND packet halves the number of sleeps and so halves the
    # overshoot. The cost is a 2-packet burst, 3 KB into the card's 8 KB FIFO
    # -- well inside tolerance, and no worse than what the host sent before any
    # of this pacing work. Raising it further would buy less and burst more.
    #
    # This is also why a Rust or C rewrite is not the answer: it would remove
    # the 5.9 us of work and inherit the 53 us of overshoot, because nanosleep
    # overshoots the same in any language. The real escape is kernel pacing
    # (SO_MAX_PACING_RATE with an `fq` qdisc, or SO_TXTIME with ETF offload),
    # which needs a NIC and a qdisc this host does not currently have.
    # TWO, justified by the FRAME RATE -- which replicates -- and not by
    # chunk loss, which does not.
    #
    # Batching halves the number of pacing sleeps and so halves the ~53 us
    # overshoot each carries. That effect is solid and wire-measured:
    # delivery 19.58 -> 20.00 fps, interval median 51.0 -> 50.0 ms, p99
    # 60.9 -> 50.6. Removing the per-frame deficit is the whole point, and it
    # is reproducible.
    #
    # It was also briefly reverted on the theory that pairs give a congested
    # path something to concatenate. Do not redo that experiment without
    # reading this: `c_dropped` on a VIDEO show is far too noisy to compare
    # settings with one window each. The same configuration, same show, peaks
    # cleared, ~150 s windows, measured three times:
    #
    #   batch 2, 300 us   6.05 ... 7.68 ... 12.62 chunks/s
    #   batch 1, 300 us  10.86
    #   batch 1, 280 us  15.12
    #
    # The spread WITHIN one configuration covers the spread between all of
    # them. Every ordering anyone reads out of those numbers -- including
    # "pairs beat singles", which was written here as a finding before being
    # replicated -- is inside the noise.
    #
    # If this is ever tuned against loss rather than frame rate it needs
    # repeated windows per setting and a stated confidence, not one run each.
    # And measure the CARD: the host-side capture is clean and regular at
    # every setting tried, max run 2, never longer, so it cannot tell these
    # apart at all.
    SEND_BATCH = 2

    # SEND BUFFER. 1 MB, unchanged -- and a record of why a smaller one is NOT
    # the fix, because the mechanism argument for it is seductive and wrong.
    #
    # THE HYPOTHESIS. Pacing in this loop controls when a packet reaches the
    # KERNEL, not the wire. The card's burst_fifo holds 2048 words = 8192 B =
    # 5.4 packets; at SO_SNDBUF 1 MB the kernel can bank 1386. A video show
    # composes in 8-10 ms against 2.2 ms static, so on video the sender is
    # descheduled mid-frame, the kernel accumulates, then flushes at line rate
    # -- a run of back-to-back packets into a 5.4-packet buffer. `AlwaysReady`
    # drops a WHOLE packet when the FIFO refuses a mid-packet beat, which fits
    # the observed shape: losses arrive in bursts of ~26 chunks inside one
    # frame. It also explains the fact that made this look unexplainable -- the
    # wire is byte-identical between a video and a static show, so only arrival
    # timing was ever left as a variable.
    #
    # IT DOES NOT SURVIVE MEASUREMENT. Sized to the FIFO (4096 -> 8192 actual)
    # and to 3 packets (2048 -> 4608), 60 s windows on the live wall, peaks
    # cleared each time:
    #
    #   1 MB         6.05  7.68 12.62 10.86 15.12   chunks lost/s
    #   8 KB         5.58  5.70  5.80  6.65
    #   4.6 KB      11.05 13.95 10.08
    #   8 KB AGAIN   14.86 14.10  9.55     <-- same config as row 2
    #
    # The same configuration measured 5.58-6.65 and then 9.55-14.86. The
    # within-configuration spread is 9.54, which covers every difference
    # between configurations. There is no effect here to see.
    #
    # WHY CONSECUTIVE WINDOWS LIE. c_dropped tracks the VIDEO's scene
    # complexity, because render cost does, and render cost is what steals the
    # GIL from the sender. Scene complexity varies on a timescale of minutes,
    # so four consecutive 60 s windows sample one stretch of footage and
    # cluster tightly -- which reads exactly like a variance collapse and is
    # not one. The clustering is the correlation, not the signal.
    #
    # HOW TO ACTUALLY TEST THIS, if anyone tries again: fixed, looping content
    # so render cost is constant, A/B/A interleaved rather than sequential, and
    # windows long enough to cross several scenes. Four windows in a row prove
    # nothing, and this comment is the second time that has been written here --
    # the SEND_BATCH note below says the same thing about the same metric. I
    # read it, quoted it, and then did it anyway.
    #
    # `c_fifo_max` reached 2049 of 2048 at EVERY setting including 1 MB, so the
    # FIFO overflows regardless of how little the kernel can bank. The drain
    # stalls too -- `dma_stall_peak` ~3400 cycles, ~97 us at 35 MHz, while the
    # scan-out holds the DRAM port -- so the FIFO is rarely empty when anything
    # arrives. That points at DRAM arbitration between the pixel DMA and
    # scan-out, i.e. GATEWARE, not at the host's socket options.
    # THE OVERFLOW THRESHOLD IS A RUN OF 5, AND 8 KB GRANTED EXACTLY 5.4.
    #
    # That is why the first attempt at this failed. The card's burst_fifo is
    # 2048 words; a pixel frame is ~376 words; the steady-state high-water is a
    # measured 744 words (= the 2 packets SEND_BATCH leaves resident). The
    # drain through Bytes32to8 is 1 byte/cycle at 35 MHz = 43 us per packet,
    # while arrival at gigabit line rate is 12 us -- so the drain is 3.6x
    # SLOWER than a back-to-back arrival, and a run accumulates:
    #
    #   run of 4 -> 1828 words   ok
    #   run of 5 -> 2099 words   OVERFLOW
    #
    # (The gateware comment on c_fifo_max says it would take 8 consecutive.
    # That figure ignores the 3.6x drain deficit; the real threshold is 5, and
    # it matches the measured high-water distribution, which is BIMODAL -- 744
    # or full, nothing in between.)
    #
    # SO_SNDBUF is requested, then DOUBLED by the kernel:
    #   4096 -> 8192 = 5.4 packets   at the threshold, no help
    #   2048 -> 4608 = 3.0 packets   below it, but `sendto` then blocks often
    #                                enough that the catch-up path bursts
    #   3026 -> 6052 = 4.0 packets   under the threshold, above the blocking
    #
    # Settable at runtime so this can be A/B'd by restarting the player rather
    # than rebuilding, which is what made the earlier comparison impossible to
    # do properly.
    SND_BUF = int(os.environ.get("MARQUEE_SND_BUF", 1 << 20))
    REDUNDANT_COPIES = int(os.environ.get("MARQUEE_REDUNDANT_COPIES", 1))

    def __init__(self, host: str, port: int = DEFAULT_PORT, packet_delay: float = 0.0003):
        self.host, self.port = host, port
        self.packet_delay = packet_delay
        self._frame_id = 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self.SND_BUF)
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
        # Over 255 chunks the narrow header cannot express the index, so send
        # the SAME payload under MAGIC_WIDE with u16 counters. Frames that
        # still fit stay on the original magic and header, byte for byte, so
        # nothing changes for cards that only speak the narrow form.
        wide = total > MAX_CHUNKS_NARROW
        if wide and magic != MAGIC:
            raise ValueError(
                f"{total} chunks needs the wide header, which only carries "
                f"RGB frames; {magic!r} has no wide form")
        hdr = HEADER_WIDE if wide else HEADER
        magic = MAGIC_WIDE if wide else magic
        if wide:
            bytes_per_chunk = BYTES_PER_CHUNK_WIDE
            total = (len(body) + bytes_per_chunk - 1) // bytes_per_chunk
        if total > 0xFFFF:
            raise ValueError(f"{total} chunks exceeds even the u16 chunk_index")
        # REDUNDANT COPIES, the fix for packet TRAINS the host cannot prevent.
        #
        # Measured 2026-10-24 with gateware 1.37.0's instruments: in every
        # second burst_fifo filled, the card saw a back-to-back run of median
        # 28 (max 45) packets at its gate; in seconds it did not fill, median 3.
        # The drain and the CPU path were flat in both. The host emits runs of
        # at most 2 (NIC timestamps, serializer-modelled), so the trains form
        # in the switch's egress toward the card -- ~8 ms of our stream held
        # and released at line rate, which is what a multicast/broadcast burst
        # on that port does. The card loses ~30 chunks of whichever frame is in
        # flight, and gateware 1.36.0 then holds that frame off the glass.
        #
        # We cannot stop the switch from queueing. We CAN make the loss not
        # matter: send the frame again under the SAME frame_id. The gateware
        # deduplicates (received only counts a chunk whose arrival bit was
        # clear), a train lasts ~8 ms, and the second copy follows ~20 ms after
        # the first, so a chunk's two copies never share a train. A frame
        # completes if EITHER copy of each chunk arrives.
        #
        # Cost: 2x pixel bandwidth (8 MB/s, 6% of gigabit) and 2x DRAM writes
        # (21% -> ~42% of wall time, measured headroom exists). packet_delay
        # must halve so both copies fit the frame period: 270 packets at
        # 150 us = 40.5 ms at two panels. At THREE panels (404 packets) this
        # does not fit 20 fps and a NAK/retransmit protocol is the right tool.
        #
        # 1 = the old behaviour. Env-overridable so it can be A/B'd against
        # frames_held/s without a rebuild.
        copies = max(1, self.REDUNDANT_COPIES)
        deadline = time.perf_counter()
        for i in range(total * copies):
            i = i % total
            payload = body[i * bytes_per_chunk : (i + 1) * bytes_per_chunk]
            self.sock.sendto(
                hdr.pack(magic, self._frame_id, i, total, width, height) + payload,
                (self.host, self.port),
            )
            self.packets_sent += 1
            # NOT on the last chunk. `or i == total - 1` used to pace after
            # the final packet of a frame too, which waits for a packet that
            # does not exist: `deadline` is re-initialised at the top of the
            # next _send_chunked, so the value this computed was discarded.
            # Measured: one wait is 600 us median (p99 637), so a frame paid
            # ~600 us of dead time, 12 ms/s at 20 fps -- and it fell exactly on
            # the frame boundary, holding the GIL at the moment the render
            # thread wants it back.
            #
            # This does NOT change inter-packet spacing: every gap between two
            # real packets is still governed by the same deadline arithmetic.
            # It only stops waiting after the last one. Inter-FRAME spacing is
            # the sender thread's job (`earliest` in player._sender), not this
            # loop's.
            if self.packet_delay > 0 and i != total - 1 and (
                    (i + 1) % self.SEND_BATCH == 0):
                # (the final-chunk exclusion above is per copy: `i` wraps)
                # CLAMPED, so lost time is never repaid as a burst.
                #
                # This used to be a bare `deadline += packet_delay`. Any
                # hiccup -- the OS descheduling this thread, a GC pause, a
                # slow sendto -- left `deadline` behind real time, and the
                # loop then fired packets back to back until the accumulator
                # caught up. One 5 ms stall becomes ~8 packets at line rate.
                #
                # Measured on the wire, 499 gaps at a 600 us target: median
                # 599.9 us, but 7.0% of gaps under 100 us and a minimum of
                # 0.0. `burst_fifo` on the card holds 5.5 packets, so a burst
                # of 6 overflows it and the gate discards whole chunks
                # (c_dropped, RB-5524). The card was never the problem here:
                # its DMA writer's worst wait is 1.4 us against a 234 us
                # watchdog, measured with gateware 1.31.0's stall_peak.
                #
                # max(..., now) meant a late packet went out immediately ONCE
                # and spacing resumed from there, so a hiccup cost one close
                # gap instead of eight. Better than the accumulator, but "one
                # close gap" is still a back-to-back pair on the wire, and the
                # capture above shows those pairs land almost entirely in the
                # first dozen chunks of a frame -- precisely where the card
                # loses chunks. So the catch-up is FLOORED rather than
                # immediate: still one short interval instead of eight, but
                # MIN_SEND_GAP wide instead of zero.
                deadline = max(
                    deadline + self.packet_delay * self.SEND_BATCH,
                    time.perf_counter() + self.MIN_SEND_GAP)
                # SLEEP the bulk, spin only the tail.
                #
                # The old threshold was `rem > 0.002`, which at a 600 us
                # interval is never true -- so this busy-spun the WHOLE gap and
                # held a core at 100% for the 41 ms a frame takes to send. That
                # is why the sender collapsed when a gateware build wanted CPU
                # on this host: 68 spin-waits per frame have no slack to give.
                # Sleeping down to the last 120 us keeps the precision that
                # matters (median was already 599.9 us) and hands back most of
                # the core.
                rem = deadline - time.perf_counter()
                if rem > 0.00012:
                    time.sleep(rem - 0.0001)
                while time.perf_counter() < deadline:
                    pass
        self.frames_sent += 1


# The name this class had until v0.11.0, when "panel" was split into the wall
# (what a viewer sees) and the card (the board driving part of it).
PanelStream = CardStream
