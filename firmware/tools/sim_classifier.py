#!/usr/bin/env python3
"""The pixel classifier: what it kills, and -- critically -- what it must not.

The filter exists to keep pixel packets out of the card's CPU so the receive
ISR is not the frame-rate ceiling. It matches on UDP destination port.

A PALETTE packet arrives on that same port, and the gateware DMA drops it on
the explicit assumption that the CPU will see it. So killing everything on the
port meant nobody wrote the palette: indices landed correctly and were looked
up in whatever colours were there already. An image with the right shape and
the wrong colours, with every counter clean.

These tests exist so that cannot come back. `./build.sh sim-classifier`
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateware"))

from migen import *
from litex.soc.interconnect import stream
from smoleth import SmolEthPixelClassifier, SmolEthGroupReject

DESC = [("data", 32), ("last_be", 4), ("error", 4)]
PORT = 7000


def packet(dport=PORT, magic=b"BM", ethertype=0x0800, proto=17, payload=64):
    """A minimal Ethernet/IPv4/UDP frame as 32-bit little-endian beats.

    Byte N of the wire is beat N//4, bits [8*(N%4) : 8*(N%4)+8] -- the layout
    the classifier documents.
    """
    b = bytearray(14 + 20 + 8 + payload)
    b[12:14] = ethertype.to_bytes(2, "big")   # ethertype
    b[14] = 0x45                              # IPv4, IHL 5
    b[23] = proto                             # protocol
    # UDP header at byte 34: src 34-35, DST 36-37, length 38-39, cksum 40-41,
    # payload from 42. The destination is what the filter matches.
    b[34:36] = (40000).to_bytes(2, "big")     # source port, arbitrary
    b[36:38] = dport.to_bytes(2, "big")       # destination port
    b[42:44] = magic                          # payload magic
    words = [int.from_bytes(b[i:i + 4].ljust(4, b"\x00"), "little")
             for i in range(0, len(b), 4)]
    return words


def run(words, enable=1, port=PORT):
    dut = SmolEthPixelClassifier(DESC)
    got = {"invalid_at_end": 0, "killed": 0}

    def bench():
        yield dut.enable.eq(enable)
        yield dut.port.eq(port)
        yield dut.sink.ready.eq(1)
        for i, w in enumerate(words):
            yield dut.sink.valid.eq(1)
            yield dut.sink.data.eq(w)
            yield dut.sink.last.eq(1 if i == len(words) - 1 else 0)
            yield
        got["invalid_at_end"] = (yield dut.invalid)
        yield dut.sink.valid.eq(0)
        yield dut.sink.last.eq(0)
        yield
        got["killed"] = (yield dut.killed)

    run_simulation(dut, bench())
    return got


def main():
    print("Pixel classifier (port %d)" % PORT)
    fails = 0

    cases = [
        ("pixel RGB888 'BM' on the port",      dict(magic=b"BM"),              True),
        ("pixel indexed 'BI' on the port",     dict(magic=b"BI"),              True),
        ("PALETTE 'BP' on the port",           dict(magic=b"BP"),              False),
        ("other magic on the port",            dict(magic=b"ZZ"),              True),
        ("right magic, WRONG port",            dict(magic=b"BM", dport=7001),  False),
        ("not UDP",                            dict(magic=b"BM", proto=6),     False),
        ("not IPv4",                           dict(magic=b"BM", ethertype=0x0806), False),
    ]
    for name, kw, want_kill in cases:
        r = run(packet(**kw))
        killed = bool(r["killed"])
        ok = killed == want_kill
        fails += 0 if ok else 1
        verb = "KILLED" if killed else "passed"
        print(f"  {'ok ' if ok else 'FAIL'} {name:34s} -> {verb}"
              f"{'' if ok else '  (wanted ' + ('KILLED' if want_kill else 'passed') + ')'}")

    # The filter must be a no-op when disabled, or an operator turning it off
    # to debug would change nothing and look mad.
    r = run(packet(magic=b"BM"), enable=0)
    ok = not r["killed"]
    fails += 0 if ok else 1
    print(f"  {'ok ' if ok else 'FAIL'} {'disabled filter kills nothing':34s} -> "
          f"{'passed' if not r['killed'] else 'KILLED'}")

    # A runt must never be truncated on a verdict never reached.
    r = run(packet(magic=b"BM")[:8])
    ok = not r["invalid_at_end"]
    fails += 0 if ok else 1
    print(f"  {'ok ' if ok else 'FAIL'} {'short frame reaches no verdict':34s} -> "
          f"{'no verdict' if ok else 'ASSERTED'}")

    fails += _group_reject_tests()

    if fails:
        print(f"\n{fails} FAILED")
        return 1
    print("\nAll passed.")
    return 0


def _run_group_reject(beats):
    """Push a frame through SmolEthGroupReject; return what came out."""
    dut = SmolEthGroupReject(DESC)
    out = {"beats": [], "rejected": 0}

    def gen():
        yield dut.source.ready.eq(1)
        for i, w in enumerate(beats):
            yield dut.sink.valid.eq(1)
            yield dut.sink.data.eq(w)
            yield dut.sink.last.eq(1 if i == len(beats) - 1 else 0)
            yield
            # Record only what the downstream FIFO would actually have seen.
            while not (yield dut.sink.ready):
                yield
            if (yield dut.source.valid):
                out["beats"].append((yield dut.source.data))
        yield dut.sink.valid.eq(0)
        yield
        yield
        out["rejected"] = (yield dut.rejected)

    run_simulation(dut, gen())
    return out


def _frame_words(dst_mac):
    """A frame as 32-bit little-endian beats, with `dst_mac` as bytes 0-5."""
    b = bytearray(64)
    b[0:6] = dst_mac
    b[6:12] = bytes.fromhex("020205378400")      # some source
    b[12:14] = (0x0800).to_bytes(2, "big")
    return [int.from_bytes(b[i:i + 4], "little") for i in range(0, len(b), 4)]


def _group_reject_tests():
    """The multicast reject on the hardware branch.

    It exists so mDNS stops sharing burst_fifo with pixel chunks. The whole
    value depends on two things being true at once: multicast dies, and the
    unicast pixel frames are byte-for-byte untouched. A filter that quietly
    ate pixels would look exactly like the packet loss it was added to fix.
    """
    print("\nGroup-address reject (hardware branch):")
    fails = 0
    cases = [
        # name,                         dst MAC,              reject?
        ("unicast to this card",        "0202053784 4c",      False),
        ("mDNS IPv4 multicast",         "01005e0000 fb",      True),
        ("Spotify/SSDP multicast",      "01005e7fff fa",      True),
        ("IPv6 multicast",              "3333000000 01",      True),
        ("broadcast must still pass",   "ffffffffff ff",      False),
        ("unicast, odd 2nd byte",       "0201020304 05",      False),
    ]
    for name, mac_hex, want_reject in cases:
        mac = bytes.fromhex(mac_hex.replace(" ", ""))
        words = _frame_words(mac)
        r = _run_group_reject(words)
        rejected = r["rejected"] > 0
        # A rejected frame must emit NOTHING -- not even its first beat, which
        # is the bug a registered-only decision would have.
        leaked = len(r["beats"]) > 0 and rejected
        passed_intact = (not rejected) and r["beats"] == words
        ok = (rejected == want_reject) and not leaked and (
            want_reject or passed_intact)
        fails += 0 if ok else 1
        verb = "REJECTED" if rejected else f"passed {len(r['beats'])}/{len(words)} beats"
        note = ""
        if leaked:
            note = f"  LEAKED {len(r['beats'])} beats"
        elif not want_reject and not passed_intact:
            note = f"  CORRUPTED/TRUNCATED"
        print(f"  {'ok ' if ok else 'FAIL'} {name:30s} -> {verb}{note}")
    return fails


if __name__ == "__main__":
    sys.exit(main())
