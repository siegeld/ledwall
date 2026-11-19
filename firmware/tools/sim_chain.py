#!/usr/bin/env python3
"""The chain paths: what passes, what is rewritten, what is dropped.

Downstream (ChainForwarder):
  * group-addressed frames do not come out at all, not even a preamble;
  * every other frame comes out byte-identical, preamble and FCS included -- a bit
    error on a hop must still be a CRC error at the next card;
  * a discovery frame comes out with hop + 1, the anti-hop byte - 1, a valid
    Ethernet FCS, and a UDP checksum that still verifies;
  * a discovery frame at hop 255 is left alone rather than wrapped.

Upstream (ChainUpstream), with port 1 and port 0 on separate clocks:
  * every unicast frame from port 1 and every frame from the CPU's MAC
    arrives intact, preamble regenerated;
  * group-addressed frames from port 1 are dropped -- that is what keeps a
    member's DHCP inside the chain;
  * at least 12 idle cycles separate any two frames.

`./build.sh sim-chain`
"""
import os, random, struct, sys, zlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gateware"))

from migen import *
from migen.sim import passive
from chain import ChainForwarder, ChainUpstream, DISC_PORT

PRE = bytes([0x55] * 7 + [0xd5])
HEAD_MAC = bytes.fromhex("10e2d5000001")
SRV_MAC = bytes.fromhex("020000000002")


def fcs(body):
    return struct.pack("<I", zlib.crc32(body) & 0xffffffff)


def csum16(data):
    if len(data) % 2:
        data += b"\0"
    s = sum(struct.unpack(f"!{len(data)//2}H", data))
    while s >> 16:
        s = (s & 0xffff) + (s >> 16)
    return s


def udp_frame(dport, payload, dst=HEAD_MAC, src=SRV_MAC, sip=b"\x0a\x00\x00\x02", dip=b"\x0a\x00\x00\x01"):
    udp_len = 8 + len(payload)
    ip = bytearray(struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + udp_len, 0, 0, 64, 17, 0, sip, dip))
    ip[10:12] = struct.pack("!H", 0xffff - csum16(bytes(ip)))
    udp = bytearray(struct.pack("!HHHH", 40000, dport, udp_len, 0)) + payload
    pseudo = sip + dip + struct.pack("!BBH", 0, 17, udp_len)
    c = 0xffff - csum16(pseudo + bytes(udp))
    udp[6:8] = struct.pack("!H", c or 0xffff)
    body = dst + src + b"\x08\x00" + bytes(ip) + bytes(udp)
    body = body.ljust(60, b"\0")
    return body + fcs(body)


def udp_ok(frame):
    """Does the UDP checksum of an un-padded frame body still verify?"""
    ip = frame[14:34]
    udp_len = struct.unpack("!H", frame[38:40])[0]
    udp = frame[34:34 + udp_len]
    pseudo = ip[12:16] + ip[16:20] + struct.pack("!BBH", 0, 17, udp_len)
    return csum16(pseudo + udp) == 0xffff


def disc(hop):
    return udp_frame(DISC_PORT, b"CH" + bytes([hop, 1, 255 - hop, 0]) + b"\0" * 10)


def raw(dst, n):
    body = dst + SRV_MAC + b"\x88\xb5" + bytes(random.randrange(256) for _ in range(n))
    body = body.ljust(60, b"\0")
    return body + fcs(body)


# Downstream ----------------------------------------------------------------

def run_forwarder(frames):
    dut = ChainForwarder()
    out, cur = [], bytearray()

    def drive():
        for f in frames:
            w = PRE + f
            for i, b in enumerate(w):
                yield dut.rx_valid.eq(1)
                yield dut.rx_data.eq(b)
                yield dut.rx_last.eq(i == len(w) - 1)
                yield
            yield dut.rx_valid.eq(0)
            yield dut.rx_last.eq(0)
            for _ in range(12):
                yield
        for _ in range(20):
            yield

    def watch():
        nonlocal cur
        for _ in range(sum(len(f) + 8 + 12 for f in frames) + 40):
            if (yield dut.tx_valid):
                cur.append((yield dut.tx_data))
            elif cur:
                out.append(bytes(cur)); cur = bytearray()
            yield

    run_simulation(dut, [drive(), watch()])
    return out


def test_forwarder():
    plain = [raw(HEAD_MAC, n) for n in (46, 100, 500, 1400)]
    corrupt = bytearray(raw(HEAD_MAC, 80)); corrupt[-1] ^= 0x01
    bcast = raw(b"\xff" * 6, 300)
    mcast = raw(bytes.fromhex("01005e0000fb"), 120)
    frames = plain + [bytes(corrupt), bcast, disc(0), disc(3), mcast, disc(255),
                      udp_frame(7000, b"BM" + bytes(40))]
    out = run_forwarder(frames)
    # Group-addressed frames are NOT forwarded -- not even their preamble.
    want = [f for f in frames if not f[0] & 1]
    assert len(out) == len(want), f"{len(out)} frames out, wanted {len(want)} (group frames must vanish)"
    frames = want
    for f, o in zip(frames, out):
        assert o[:8] == PRE, "preamble altered"
        o = o[8:]
        assert len(o) == len(f), "length altered"
        if f[36:38] == struct.pack("!H", DISC_PORT) and f[44] != 255:
            assert o[44] == f[44] + 1 and o[46] == f[46] - 1, "hop not rewritten"
            assert o[:44] == f[:44] and o[47:-4] == f[47:-4], "other bytes touched"
            assert o[-4:] == fcs(o[:-4]), "FCS not regenerated"
            assert udp_ok(o), "UDP checksum broken by the rewrite"
        else:
            assert o == f, "frame altered"
    print(f"  forwarder: {len(frames)} unicast frames byte-identical (discovery rewritten), 2 group frames dropped whole")


# Upstream ------------------------------------------------------------------

def run_upstream(up_frames, mac_frames, up_spacing=200, wr_period=8):
    dut = ChainUpstream()
    out, cur, idle_runs = [], bytearray(), []
    idle = 99

    def port1():
        for f in up_frames:
            for _ in range(random.randrange(0, up_spacing + 1)):
                yield
            w = PRE + f
            for i, b in enumerate(w):
                yield dut.rx_valid.eq(1)
                yield dut.rx_data.eq(b)
                yield dut.rx_last.eq(i == len(w) - 1)
                yield
            yield dut.rx_valid.eq(0)
            yield dut.rx_last.eq(0)
            for _ in range(12):
                yield

    @passive
    def mac():
        # The CPU side as SmolEth gates it: a frame only STARTS while `hold`
        # is low, and once started it runs to the end; `held` = hold and no
        # frame in flight. `ready` is constant -- the MAC is never stalled.
        # (The real gate is a sys-domain MultiReg away; one more cycle of
        # latency here changes nothing the arbiter relies on.)
        for f in mac_frames:
            for _ in range(random.randrange(0, 200)):
                yield dut.held.eq((yield dut.hold))
                yield
            while (yield dut.hold):
                yield dut.held.eq(1)
                # A frame is being kept back -- SmolEth says so, and only then
                # does the chain give the port back.
                yield dut.cpu_waiting.eq(1)
                yield
            yield dut.cpu_waiting.eq(0)
            yield dut.held.eq(0)
            w = PRE + f
            for i, b in enumerate(w):
                yield dut.mac.valid.eq(1)
                yield dut.mac.data.eq(b)
                yield dut.mac.last.eq(i == len(w) - 1)
                assert (yield dut.mac.ready), "the MAC was stalled"
                yield
            yield dut.mac.valid.eq(0)
            yield dut.mac.last.eq(0)
        while True:
            yield dut.held.eq((yield dut.hold))
            yield

    def watch():
        nonlocal cur, idle
        total = (sum(len(f) + 40 for f in up_frames + mac_frames) * 3
                 + len(up_frames) * up_spacing + len(mac_frames) * 200 + 2000)
        for _ in range(total):
            if (yield dut.tx_valid):
                if not cur:
                    idle_runs.append(idle)
                cur.append((yield dut.tx_data)); idle = 0
            else:
                if cur:
                    out.append(bytes(cur)); cur = bytearray()
                idle += 1
            yield

    # A real hop is the same frequency within 200 ppm; the overload test
    # runs port 1 14% fast to force the FIFO over.
    run_simulation(dut, {"wr": [port1()], "rd": [mac(), watch()]},
                   clocks={"wr": wr_period, "rd": 8})
    return out, idle_runs


def test_upstream():
    random.seed(1)
    bcast = b"\xff" * 6
    mcast = bytes.fromhex("01005e0000fb")
    up = [raw(HEAD_MAC, random.choice((46, 300, 1400, 1500))) for _ in range(12)]
    up.insert(3, raw(bcast, 300))     # a member's DHCP discover
    up.insert(7, raw(mcast, 100))     # mDNS
    macs = [raw(SRV_MAC, random.choice((46, 200, 1400))) for _ in range(10)]
    out, gaps = run_upstream(up, macs, up_spacing=1500)
    for o in out:
        assert o[:8] == PRE, "preamble not regenerated"
    got = [o[8:] for o in out]
    want_up = [f for f in up if not f[0] & 1]
    want_mac = macs
    missing = [i for i, f in enumerate(want_up) if f not in got]
    assert not missing, f"port 1 frames {missing} of {len(want_up)} lost"
    assert [g for g in got if g in want_up] == want_up, "port 1 frames reordered"
    assert [g for g in got if g in want_mac] == want_mac, "MAC frames lost or reordered"
    assert len(got) == len(want_up) + len(want_mac), "unexpected frame (group frame leaked?)"
    assert min(gaps[1:]) >= 12, f"inter-frame gap {min(gaps[1:])} < 12"
    print(f"  upstream: {len(want_up)} member + {len(want_mac)} CPU frames intact, "
          f"2 group frames dropped, min gap {min(gaps[1:])}")


def test_upstream_overload():
    """Port 1 back-to-back at 1.14x port 0's clock while the CPU also sends.

    The FIFO must overflow. What matters is HOW: a member frame is either
    delivered byte-identical or cut short so its FCS fails -- the next
    receiver drops it -- and the CPU's frames are never lost or damaged.
    """
    random.seed(2)
    up = [raw(HEAD_MAC, 1500) for _ in range(16)]
    macs = [raw(SRV_MAC, 1400) for _ in range(8)]
    out, gaps = run_upstream(up, macs, up_spacing=0, wr_period=7)
    got = [o[8:] for o in out]
    whole = [g for g in got if g in up or g in macs]
    cut = [g for g in got if not (g in up or g in macs)]
    for g in cut:
        assert len(g) >= 4 and g[-4:] != fcs(g[:-4]), "an altered frame has a VALID FCS"
    assert [g for g in got if g in macs] == macs, "CPU frames lost under overload"
    n_up = sum(g in up for g in got)
    assert 0 < n_up < len(up), f"expected some member frames lost, got {n_up}/{len(up)}"
    assert min(gaps[1:]) >= 12
    print(f"  upstream overload: {n_up}/{len(up)} member frames whole, {len(cut)} cut short "
          f"with a failing FCS; all {len(macs)} CPU frames intact")


if __name__ == "__main__":
    test_forwarder()
    test_upstream()
    test_upstream_overload()
    print("sim-chain: PASS")
