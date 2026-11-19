#!/usr/bin/env python3
"""Does the MAC/IP/UDP filter chain work at dw=32, and with what alignment?

THE QUESTION THIS ANSWERS. The hardware pixel path narrows the 32-bit MAC
stream to 8 bits (`Bytes32to8`) and parses byte-wise. That makes the drain
24.4 MB/s against a wire that fills at 125 MB/s, so `burst_fifo` is the only
elasticity and a long enough burst costs whole chunks. Measured 2026-10-21:
`c_fifo_max` 4097 of 4096.

`SmolEthMACFilter`, `SmolEthIP` and `SmolEthUDP` are all parameterised by `dw`
and built on LiteEth's depacketizers, which support 32 bits -- that is how
LiteEth itself runs gigabit. So the narrowing may be removable by passing 32
instead of 8. Two things have to be true, and neither can be settled by
reading the source with any confidence:

  1. the chain must still ACCEPT a matching frame and REJECT the others
  2. the payload's byte alignment must be known exactly

(2) is the one that matters for the pixel extractor. Headers are 14 + 20 + 8 =
42 bytes, which is NOT a multiple of 4, so whether LiteEth realigns the
payload to lane 0 or leaves it at a 2-byte offset decides what the extractor
has to be told. Getting it wrong does not make a subtle artefact -- it shears
the colour of every pixel. So this measures it.

Run: ./build.sh sim-hwpath32
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateware"))

from migen import *                                      # noqa: E402

from smoleth import SmolEthIP, SmolEthMACFilter, SmolEthUDP  # noqa: E402

try:
    from liteeth.common import udp_protocol
except ImportError:                                      # pragma: no cover
    udp_protocol = 0x11

PORT = 7000
MAC = 0x02020537844C
IP = 0x0A0B063E          # 192.168.1.62


def frame(payload, dst_mac=MAC, dst_ip=IP, dport=PORT, proto=17,
          ethertype=0x0800):
    """A complete Ethernet/IPv4/UDP frame, as bytes, in wire order."""
    b = bytearray(42 + len(payload))
    b[0:6] = dst_mac.to_bytes(6, "big")
    b[6:12] = (0x001122334455).to_bytes(6, "big")
    b[12:14] = ethertype.to_bytes(2, "big")
    b[14] = 0x45                                  # version 4, IHL 5
    b[16:18] = (20 + 8 + len(payload)).to_bytes(2, "big")   # total length
    b[22] = 64                                    # TTL
    b[23] = proto
    b[26:30] = (0x0A0B0641).to_bytes(4, "big")    # sender 192.168.1.10
    b[30:34] = dst_ip.to_bytes(4, "big")
    b[34:36] = (40000).to_bytes(2, "big")
    b[36:38] = dport.to_bytes(2, "big")
    b[38:40] = (8 + len(payload)).to_bytes(2, "big")
    b[42:] = payload
    return bytes(b)


def to_words(b):
    """Wire bytes to 32-bit beats, first byte in the LOW bits (LiteX order)."""
    return [int.from_bytes(b[i:i + 4].ljust(4, b"\x00"), "little")
            for i in range(0, len(b), 4)]


class Chain(Module):
    """The three filters wired as `SmolEth` wires them, at a chosen width."""

    def __init__(self, dw):
        self.submodules.mac = mac = SmolEthMACFilter(MAC, dw)
        self.submodules.ip = ip = SmolEthIP(IP, udp_protocol, dw)
        self.submodules.udp = udp = SmolEthUDP(PORT, dw)
        self.comb += [
            mac.source.connect(ip.sink),
            ip.source.connect(udp.sink),
        ]
        self.sink = mac.sink
        self.source = udp.source


def run(dw, raw, cycles_idle=40):
    """Push `raw` through the chain and collect the payload beats."""
    dut = Chain(dw)
    words = to_words(raw) if dw == 32 else list(raw)
    out = []

    def bench():
        yield dut.source.ready.eq(1)
        i = 0
        # Keep clocking for a while after the last beat: the depacketizers
        # hold a header's worth of pipeline, so the tail emerges late.
        for _ in range(len(words) + cycles_idle):
            if i < len(words):
                yield dut.sink.valid.eq(1)
                yield dut.sink.data.eq(words[i])
                yield dut.sink.last.eq(1 if i == len(words) - 1 else 0)
                if dw == 32:
                    yield dut.sink.last_be.eq(0x8 if i == len(words) - 1 else 0)
            else:
                yield dut.sink.valid.eq(0)
                yield dut.sink.last.eq(0)
            yield
            if (yield dut.sink.valid) and (yield dut.sink.ready):
                i += 1
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append(((yield dut.source.data), (yield dut.source.last)))

    run_simulation(dut, bench())
    return out


def payload_bytes(out, dw):
    """Flatten collected beats back into a byte string."""
    if dw == 8:
        return bytes(d & 0xFF for d, _ in out)
    b = bytearray()
    for d, _ in out:
        b += int(d).to_bytes(4, "little")
    return bytes(b)


def main():
    app = bytes([0x42, 0x4D] + [0] * 8)            # "BM" + 8 header bytes
    pixels = bytes((i * 7 + 13) & 0xFF for i in range(1461))
    payload = app + pixels
    raw = frame(payload)

    print(f"frame {len(raw)} bytes = {len(raw) / 4:.2f} words; "
          f"payload {len(payload)} bytes (10 app + {len(pixels)} pixel)")
    print()

    failures = []

    # --- the control: the 8-bit chain that is in the card today ------------
    out8 = run(8, raw)
    got8 = payload_bytes(out8, 8)
    ok8 = got8[:len(payload)] == payload
    print(f"  {'ok ' if ok8 else 'FAIL'} dw=8  (the chain in the card today): "
          f"{len(got8)} payload bytes out, match={ok8}")
    if not ok8:
        failures.append("dw=8 control")
        print(f"       first 16 got  {got8[:16].hex()}")
        print(f"       first 16 want {payload[:16].hex()}")

    # --- the question: the same chain at 32 bits --------------------------
    out32 = run(32, raw)
    got32 = payload_bytes(out32, 32)
    print(f"       dw=32: {len(out32)} beats = {len(got32)} bytes out")
    if not out32:
        print("  FAIL dw=32 produced NOTHING -- the chain does not accept at 32 bits")
        failures.append("dw=32 accepts nothing")
    else:
        # Find where the real payload starts within the emitted bytes. This is
        # the measurement the whole file exists for.
        off = got32.find(payload[:32])
        if off < 0:
            print("  FAIL dw=32 payload not found in the output at any offset")
            print(f"       first 24 got  {got32[:24].hex()}")
            print(f"       want         {payload[:24].hex()}")
            failures.append("dw=32 payload corrupted")
        else:
            exact = got32[off:off + len(payload)] == payload
            print(f"  {'ok ' if exact else 'FAIL'} dw=32 payload found at byte "
                  f"offset {off} of the emitted stream, full match={exact}")
            print()
            print(f"  >>> ALIGNMENT: the UDP payload begins in lane {off % 4} "
                  f"of emitted word {off // 4}.")
            if off % 4 == 0:
                print("      Payload is WORD-ALIGNED. The 10-byte app header then")
                print("      ends mid-word, so pixel data starts at lane 2 and the")
                print("      extractor must be seeded with a 2-byte residue.")
            else:
                print(f"      Payload is NOT word-aligned ({off % 4}-byte offset).")
            if not exact:
                failures.append("dw=32 payload mismatch")

    # --- rejection must still work at 32 bits ----------------------------
    print()
    for label, kw in [("wrong dest MAC", dict(dst_mac=0x010203040506)),
                      ("wrong dest IP", dict(dst_ip=0x0A0B0699)),
                      ("wrong UDP port", dict(dport=1234)),
                      ("not IPv4", dict(ethertype=0x86DD)),
                      ("not UDP", dict(proto=6))]:
        o = run(32, frame(payload, **kw))
        rejected = len(o) == 0
        print(f"  {'ok ' if rejected else 'FAIL'} dw=32 rejects {label:16} "
              f"({len(o)} beats emitted)")
        if not rejected:
            failures.append(f"dw=32 failed to reject {label}")

    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("all cases pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
