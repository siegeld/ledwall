#!/usr/bin/env python3
"""Adversarial simulation of the card-chain gateware (gateware/chain.py).

tools/sim_chain.py proves the basics. This suite attacks the corners: chained
forwarders, shrunken preambles, near-miss discovery frames, runts and jumbos,
fuzz, clock drift, starvation, a stuck CPU, group addressing, overlong
frames, inter-frame gaps and resets. Each case asserts and prints one line.

    docker run --rm -v "$PWD:/project:ro" -w /project litex-hub75 \
        python3 tools/sim_chain_deep.py [case ...]

Cases run in parallel processes (migen's simulator manages ~1-2k cycles/s on
this design, so serial would take far too long). Case ids: 1 2 2x 3 3x 4 4x
5 6 7 7b 8 9 9c 10 11 12 13 14 15 16. Ids with a suffix are probes beyond the brief
that turned up something worth a line; they are reported like the others.

Upstream CPU model. SmolEth (gateware/smoleth.py) gates the CPU's TX stream
in the sys domain: `held = hold & ~in_frame`, where in_frame is set by the
first beat accepted into LiteEthMACCore and cleared by the last. The bytes
then cross LiteEth's TX CDC and appear on the 125 MHz `mac` stream some
cycles later. Two settings of the model are used:
  ideal      -- as tools/sim_chain.py: hold seen instantly, frame bytes
                appear the cycle they are committed;
  realistic  -- hold reaches sys after HD_IN cycles, held comes back after
                HD_OUT, the CPU commits RATE bytes per 125 MHz cycle (sys
                40 MHz x 32 bit = 1.28) and its bytes appear LAT cycles after
                the commit started.
LiteEth's own gap inserter is modelled: >= 12 idle between two CPU frames.
"""
import collections, multiprocessing, os, random, struct, sys, time, traceback, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "gateware"))
sys.path.insert(0, HERE)

from migen import *
from migen.sim import passive
from migen.fhdl.tools import list_targets

from chain import ChainForwarder, ChainUpstream, DISC_PORT, UP_MAX_FRAME, UP_DEPTH, UP_TX_LATENCY
from sim_chain import fcs, csum16, udp_frame, udp_ok, disc, raw, HEAD_MAC, SRV_MAC, PRE


class CaseFail(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise CaseFail(msg)


# Frame builders ------------------------------------------------------------

def wire(frame, pre=7):
    """What the PHY source delivers: `pre` 0x55, the SFD, the frame."""
    return bytes([0x55] * pre + [0xd5]) + frame


def disc_v2(hop, clock=b"\xff\x00\xd5\x55"):
    return udp_frame(DISC_PORT, b"CH" + bytes([hop, 2, 255 - hop, 0]) + clock + b"\0" * 6)


def ip_udp(dport=DISC_PORT, payload=b"CH\x00\x01\xff\x00" + b"\0" * 10, ihl=5, proto=17,
           ethertype=b"\x08\x00", frag=0, vlan=False):
    """A frame shaped like discovery, with one thing knobbed."""
    opts = b"\0" * (4 * (ihl - 5))
    udp_len = 8 + len(payload)
    ip = bytearray(struct.pack("!BBHHHBBH4s4s", 0x40 | ihl, 0, 4 * ihl + udp_len, 0, frag,
                               64, proto, 0, b"\x0a\0\0\x02", b"\x0a\0\0\x01")) + opts
    ip[10:12] = struct.pack("!H", 0xffff - csum16(bytes(ip)))
    udp = struct.pack("!HHHH", 40000, dport, udp_len, 0) + payload
    hdr = HEAD_MAC + SRV_MAC + (b"\x81\x00\x00\x05" if vlan else b"") + ethertype
    body = (hdr + bytes(ip) + udp).ljust(60, b"\0")
    return body + fcs(body)


def with_fcs(body):
    return body + fcs(body)


def fcs_ok(f):
    return len(f) >= 4 and fcs(f[:-4]) == f[-4:]


DISC_CHECKS = {12: 0x08, 13: 0x00, 14: 0x45, 23: 17, 36: DISC_PORT >> 8,
               37: DISC_PORT & 0xff, 42: ord("C"), 43: ord("H")}


def fwd_ref(frame):
    """What ChainForwarder is DESIGNED to emit for a frame (bytes after the
    SFD): discovery gets hop+1 / anti-1 and a fresh FCS, all else verbatim."""
    f = bytearray(frame)
    if len(f) <= 44 or any(f[i] != v for i, v in DISC_CHECKS.items()) or f[44] == 0xff:
        return bytes(f)
    f[44] = (f[44] + 1) & 0xff
    if len(f) > 46:
        f[46] = (f[46] - 1) & 0xff
    f[-4:] = fcs(bytes(f[:-4]))
    return bytes(f)


# The forwarder grew a group-address filter (frames whose first byte after
# the SFD has bit 0 set are not forwarded). Follow whichever version is on
# disk, so this suite judges the gateware as it stands.
FWD_DROPS_GROUP = hasattr(ChainForwarder(), "dropped")


def fwd_forwards(w):
    pre, f = split_wire(w)
    return not (FWD_DROPS_GROUP and f is not None and len(f) >= 1 and f[0] & 1)


def split_wire(w):
    """(preamble+SFD, frame) of a wire; frame None if there is no SFD."""
    i = 0
    while i < len(w) and w[i] == 0x55:
        i += 1
    if i < len(w) and w[i] == 0xd5:
        return w[:i + 1], w[i + 1:]
    return w, None


# Forwarder harness ---------------------------------------------------------

class FwdChain(Module):
    """n forwarders in series on one clock. Between two, a register stands
    in for the next card's RGMII RX, which derives `last` from the line
    going idle -- exactly what LiteEthPHYRGMIIRX does."""
    def __init__(self, n):
        self.rx_valid, self.rx_data, self.rx_last = Signal(), Signal(8), Signal()
        v, d, l = self.rx_valid, self.rx_data, self.rx_last
        self.fwds = []
        for i in range(n):
            f = ChainForwarder()
            self.submodules += f
            self.fwds.append(f)
            self.comb += [f.rx_valid.eq(v), f.rx_data.eq(d), f.rx_last.eq(l)]
            if i < n - 1:
                rv, rd, rl = Signal(), Signal(8), Signal()
                self.sync += [rv.eq(f.tx_valid), rd.eq(f.tx_data)]
                self.comb += rl.eq(rv & ~f.tx_valid)
                v, d, l = rv, rd, rl
        self.tx_valid, self.tx_data = f.tx_valid, f.tx_data


def run_fwd(items, n=1, reset_at=(), tail=80):
    """items: [(idle cycles before, wire bytes)]. Returns output segments
    [(start cycle, bytes, idle before)] and whether tx went quiet at the end."""
    if reset_at:
        dut = ResetInserter()(FwdChain(n))
    else:
        dut = FwdChain(n)
    segs = []
    total = sum(g + len(w) for g, w in items) + tail + 8 * n
    state = {"t": 0}

    def drive():
        for g, w in items:
            for _ in range(g):
                yield
            for i, b in enumerate(w):
                yield dut.rx_valid.eq(1)
                yield dut.rx_data.eq(b)
                yield dut.rx_last.eq(i == len(w) - 1)
                yield
            yield dut.rx_valid.eq(0)
            yield dut.rx_last.eq(0)

    def rst():
        for c in range(total):
            yield dut.reset.eq(1 if c in reset_at else 0)
            yield

    def watch():
        cur, idle = None, 0
        for c in range(total):
            if (yield dut.tx_valid):
                if cur is None:
                    cur = bytearray()
                    segs.append((c, cur, idle))
                cur.append((yield dut.tx_data))
                idle = 0
            else:
                cur, idle = None, idle + 1
            yield
        state["quiet"] = not (yield dut.tx_valid)

    gens = [drive(), watch()] + ([rst()] if reset_at else [])
    run_simulation(dut, gens)
    return [(c, bytes(b), i) for c, b, i in segs], state["quiet"]


def expect_fwd(items, segs, hops=1, what=""):
    """Every input wire out once, in order: preamble verbatim, frame per
    fwd_ref applied `hops` times. Group-addressed frames vanish if the
    forwarder filters them."""
    items = [it for it in items if fwd_forwards(it[1])]
    check(len(segs) == len(items), f"{what}: {len(segs)} segments out for {len(items)} frames in")
    for k, ((g, w), (_, o, _)) in enumerate(zip(items, segs)):
        pre, f = split_wire(w)
        exp = w
        if f is not None:
            for _ in range(hops):
                f = fwd_ref(f)
            exp = pre + f
        if o != exp:
            diff = [i for i in range(min(len(o), len(exp))) if o[i] != exp[i]]
            raise CaseFail(f"{what}: frame {k} (len {len(w)}) altered: out len {len(o)}, "
                           f"first diffs at wire offsets {diff[:6]}")


# Forwarder cases -----------------------------------------------------------

def case1():
    plain = [raw(HEAD_MAC, n) for n in (46, 200, 1400)]
    frames = [disc(0)] + plain + [disc(0), disc_v2(0), disc(250), disc(254)]
    items = [(12, wire(f)) for f in frames]
    info = []
    for n in (2, 3):
        segs, quiet = run_fwd(items, n=n)
        check(quiet, f"{n} hops: tx still valid at the end")
        expect_fwd(items, segs, hops=n, what=f"{n} hops")
        for (g, w), (_, o, _) in zip(items, segs):
            _, f = split_wire(w)
            _, of = split_wire(o)
            if f[36:38] == struct.pack("!H", DISC_PORT) and f[44] == 0:
                check(of[44] == n and of[46] == 255 - n, f"{n} hops: hop {of[44]} anti {of[46]}")
                check(fcs_ok(of), f"{n} hops: FCS invalid on discovery")
                check(udp_ok(of), f"{n} hops: UDP checksum invalid on discovery")
            if f[44] == 254:
                # 254 -> 255 at hop 1, then left alone: 255 is never wrapped.
                check(of[44] == 255, f"{n} hops: hop 254 became {of[44]}")
        lat = segs[0][0] - items[0][0]
        info.append(f"{n} hops latency {lat} cycles")
    return (f"hop 0 -> 2 and -> 3 with valid FCS + UDP checksum, hop 254 saturates at 255, "
            f"plain frames byte-identical; {', '.join(info)}")


def case2():
    frames = [disc(0), raw(HEAD_MAC, 46), disc(5), raw(b"\xff" * 6, 46), raw(HEAD_MAC, 1400), disc_v2(9),
              raw(HEAD_MAC, 46), disc(0), disc(1), disc(2)]
    for pre in (7, 5, 1):
        items = [(12, wire(f, pre)) for f in frames]
        segs, quiet = run_fwd(items)
        check(quiet, f"preamble {pre}: no idle at end")
        expect_fwd(items, segs, what=f"preamble {pre}")
        gaps = [i for _, _, i in segs[1:]]
        check(min(gaps) == 12, f"preamble {pre}: output gap {min(gaps)} != input 12")
        check(max(gaps) <= 12 + (len(wire(frames[3], pre)) + 12 if FWD_DROPS_GROUP else 0), "gap grew")
    # Mixed preamble lengths within one burst.
    items = [(12, wire(f, p)) for f, p in zip(frames, (7, 1, 5, 1, 7, 1, 1, 5, 1, 1))]
    segs, _ = run_fwd(items)
    expect_fwd(items, segs, what="mixed preambles")
    return f"{len(frames)} frames x preambles 7/5/1/mixed at the 12-byte minimum IFG: boundaries and gaps preserved"


def case2x():
    """Probe below the 12-byte IFG: how small may gap + preamble get after a
    discovery frame before its regenerated FCS is damaged? The FCS output mux
    uses `mod`, which is cleared at the NEXT frame's SFD, while this frame's
    FCS may still be inside the delay line. 802.3 lets IPG shrink to 8 bytes
    at a receiver; RGMII PHYs keep the 7-byte preamble."""
    worst = {}
    for pre in (0, 1, 3, 7):
        for gap in range(1, 15):
            items = [(5, wire(disc(0), 7)), (gap, wire(raw(HEAD_MAC, 46), pre))]
            segs, _ = run_fwd(items)
            try:
                expect_fwd(items, segs, what="")
            except CaseFail:
                worst[pre] = gap
    safe = {p: worst.get(p, 0) + 1 for p in (0, 1, 3, 7)}
    txt = ", ".join(f"pre {p}: gap >= {g}" for p, g in safe.items())
    check(all(g <= 12 for g in safe.values()),
          f"discovery FCS damaged at the legal 12-byte IFG: needs {txt}")
    tight = "" if safe[1] <= 8 else " -- NOT within 802.3's 8-byte receive IPG if the preamble is also cut to <= 1"
    return f"NOTE discovery FCS intact only if the next SFD is far enough: {txt}{tight}"


def case3():
    base = disc(0)

    def poke(i, v):
        f = bytearray(base[:-4]); f[i] = v
        return with_fcs(bytes(f))

    near = {
        "wrong port 6991": ip_udp(dport=DISC_PORT + 1),
        "port 6990 as SOURCE": (lambda f: with_fcs(
            f[:34] + struct.pack("!HH", DISC_PORT, 40000) + f[38:-4]))(ip_udp(dport=40000)),
        "wrong magic 'CX'": ip_udp(payload=b"CX\x00\x01\xff\x00" + bytes(10)),
        "wrong magic 'ch'": ip_udp(payload=b"ch\x00\x01\xff\x00" + bytes(10)),
        "IHL=6 (IP options)": ip_udp(ihl=6),
        "VLAN 0x8100": ip_udp(vlan=True),
        "ethertype 0x86dd": ip_udp(ethertype=b"\x86\xdd"),
        "proto 6 (TCP)": ip_udp(proto=6),
        "hop 0xFF": disc(255),
        "byte 13 != 0": poke(13, 0x01),
    }
    # Truncated before byte 47 -- a link cut mid-frame (no valid FCS), and a
    # runt that carries a valid FCS over its truncated body.
    trunc = {
        "cut at 45 bytes": base[:45],
        "cut at 46 bytes": base[:46],
        "cut at 47 bytes": base[:47],
        "46-byte body + FCS (50)": with_fcs(base[:46]),
    }
    items = [(12, wire(f)) for f in list(near.values()) + list(trunc.values())]
    segs, _ = run_fwd(items)
    check(len(segs) == len(items), f"{len(segs)} out of {len(items)}")
    names = list(near) + list(trunc)
    rewritten = []
    for name, (g, w), (_, o, _) in zip(names, items, segs):
        if o != w:
            rewritten.append(name)
    near_bad = [n for n in rewritten if n in near]
    check(not near_bad, f"near misses rewritten: {near_bad}")
    trunc_bad = [n for n in rewritten if n in trunc]
    if trunc_bad:
        # Characterise: did a bad-FCS fragment come out with a GOOD FCS?
        blessed = []
        for name, (g, w), (_, o, _) in zip(names, items, segs):
            if name in trunc_bad:
                _, fi = split_wire(w); _, fo = split_wire(o)
                if not fcs_ok(fi) and fcs_ok(fo):
                    blessed.append(name)
        raise CaseFail(f"{len(near)} near misses untouched, but truncated frames REWRITTEN: "
                       f"{trunc_bad}; bad-FCS fragments emitted with a VALID FCS: {blessed} "
                       f"(ChainForwarder commits at byte 44 -- is_hop/mod -- with no check that the frame reaches byte 48)")
    return f"{len(near)} near misses + {len(trunc)} truncations all byte-identical"


def case3x():
    """Further near misses the brief did not list."""
    notes = []
    # (a) hop/anti inconsistent: anti 0 borrows into the low byte's word and
    # the ones'-complement sum is off by one.
    f = udp_frame(DISC_PORT, b"CH" + bytes([3, 1, 0, 0]) + bytes(10))
    # (b) a NON-FIRST IP fragment (offset 1480) whose payload happens to sit
    # where a UDP header would: bytes 36..37 = 6990, 42..43 = "CH".
    frag_payload = b"\x00\x00" + struct.pack("!H", DISC_PORT) + b"\x00\x00\x00\x00" + b"CH\x07\x01\xf8\x00" + bytes(30)
    ipf = bytearray(struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(frag_payload), 1, 185,
                                64, 17, 0, b"\x0a\0\0\x02", b"\x0a\0\0\x01"))
    ipf[10:12] = struct.pack("!H", 0xffff - csum16(bytes(ipf)))
    frag = with_fcs((HEAD_MAC + SRV_MAC + b"\x08\x00" + bytes(ipf) + frag_payload).ljust(60, b"\0"))
    # (c) an SFD-less burst (false carrier / preamble-only fragment) right
    # after a discovery frame.
    sfdless = bytes([0x55] * 6) + b"\x12\x34\x56\x78\x9a\xbc"
    # (d) a discovery frame with a line bit error (clock byte 50 flipped, so
    # its FCS no longer matches): the regenerated FCS must not launder it.
    dv2 = bytearray(disc_v2(0)); dv2[50] ^= 0x10; dv2 = bytes(dv2)
    items = [(12, wire(f)), (12, wire(frag)), (12, wire(disc(0))), (12, sfdless), (12, wire(raw(HEAD_MAC, 46))),
             (12, wire(dv2))]
    segs, _ = run_fwd(items)
    check(len(segs) == len(items), f"{len(segs)} segments for {len(items)}")
    _, o = split_wire(segs[0][1])
    if not udp_ok(o):
        notes.append("anti=0 discovery: rewritten, UDP checksum now WRONG (forwarder does not check anti == 255-hop)")
    _, o = split_wire(segs[1][1])
    if o != frag:
        notes.append(f"non-first IP fragment REWRITTEN (bytes {[i for i in range(len(o)) if o[i] != frag[i]][:3]}..) "
                     f"and given a fresh valid FCS={fcs_ok(o)} -- silent payload corruption (no IP flags/fragment-offset check among the `expect`s)")
    if segs[3][1] != sfdless:
        d = [i for i in range(len(sfdless)) if segs[3][1][i] != sfdless[i]]
        notes.append(f"SFD-less burst after a discovery frame ALTERED at offsets {d} "
                     f"(`mod` survives `last`, cleared only at the next SFD, and the FCS mux uses it)")
    _, o = split_wire(segs[5][1])
    if fcs_ok(o):
        notes.append("discovery frame with a BAD incoming FCS (bit error in clock byte 50) "
                     "comes out with a fresh VALID FCS -- the error is laundered; the incoming FCS is "
                     "never checked (crc_reg is only ever used to GENERATE)")
    check(not notes, "; ".join(notes))
    return "anti=0, IP fragment, SFD-less burst, bad-FCS discovery: all handled"


def case4():
    frames = [b"", b"\x02", b"\x00\x02", b"\x10\x02\x03", b"\x01\x02", bytes(range(46)),
              raw(HEAD_MAC, 1500), raw(HEAD_MAC, 1504),
              with_fcs(HEAD_MAC + SRV_MAC + b"\x88\xb5" + bytes(random.Random(4).randrange(256) for _ in range(9000 - 18))),
              raw(HEAD_MAC, 46), disc(0)]
    check(len(frames[6]) == 1518 and len(frames[7]) == 1522 and len(frames[8]) == 9000, "sizes")
    items = [(12, wire(f)) for f in frames]
    segs, quiet = run_fwd(items)
    check(quiet, "tx did not go idle (hang)")
    expect_fwd(items, segs, what="runts/jumbo")
    return ("runts of 0..3 and 46 bytes, 1518, 1522, 9000-byte jumbo: identical; discovery after the jumbo "
            "still rewritten" + ("; group-addressed runt filtered" if FWD_DROPS_GROUP else ""))


def case5():
    frames = []
    for hop, clk in ((0, b"\x00\x00\x00\x00"), (1, b"\xff\xff\xff\xff"), (7, b"\xd5\x55\xd5\x55"),
                     (254, b"\x12\x34\x56\x78"), (255, b"\x87\x65\x43\x21")):
        frames.append(disc_v2(hop, clk))
    items = [(12, wire(f)) for f in frames]
    segs, _ = run_fwd(items)
    expect_fwd(items, segs, what="v2")
    for (g, w), (_, o, _) in zip(items, segs):
        _, f = split_wire(w); _, of = split_wire(o)
        check(of[45] == 2 and of[47:52] == f[47:52], "version / clock bytes changed")
        check(fcs_ok(of) and udp_ok(of), "FCS / UDP checksum broken on v2")
    return "v2 discovery: clock bytes 48..51 and version untouched, hop/anti/FCS/UDP correct"


def case6(part=0, parts=1):
    rng = random.Random(6 + 100 * part)
    frames = []
    for _ in range(300 // parts):
        r = rng.random()
        if r < 0.08:
            frames.append(rng.choice((disc, disc_v2))(rng.randrange(256)))
        elif r < 0.12:
            # discovery with random corruption somewhere in its header
            f = bytearray(disc(rng.randrange(255))[:-4]); f[rng.choice(list(DISC_CHECKS))] ^= 1 << rng.randrange(8)
            frames.append(with_fcs(bytes(f)))
        else:
            n = int(rng.triangular(1, 1600, 60))
            frames.append(bytes(rng.randrange(256) for _ in range(n)))
    items = [(rng.randrange(1, 41), wire(f)) for f in frames]
    segs, quiet = run_fwd(items)
    check(quiet, "no idle at the end")
    expect_fwd(items, segs, what="fuzz")
    n_rw = sum(fwd_ref(f) != f for f in frames)
    return f"{len(frames)} frames (1..1600 B, gaps 1..40): all match the reference, {n_rw} legitimate rewrites"


# Upstream harness ----------------------------------------------------------

def find_sig(dut, name):
    dut.finalize()
    for stmts in dut._fragment.sync.values():
        for s in list_targets(stmts):
            if getattr(s, "backtrace", None) and s.backtrace[-1][0] == name:
                return s
    return None


IDEAL = dict(lat=0, rate=1.0, hd_in=0, hd_out=0)
REAL = dict(lat=12, rate=1.28, hd_in=7, hd_out=2)


def run_up(members, cpus, wr_period=8, rd_period=8, cpu=IDEAL, max_rd=400000, settle=600,
           stuck=None, resets=()):
    """members: [(idle before, preamble len, frame)] on port 1 (wr domain).
    cpus: [(rd cycle it becomes ready, frame)] from this card's MAC.
    stuck: None | ("pause", at_byte, cycles) | ("stream", at_byte, cycles).
    resets: {"wr"|"rd": rd cycles at which that domain gets a one-cycle
    reset} -- each PHY's CRG has its own reset (LiteEthPHYRGMIICRG), and
    wr = port 1's, rd = port 0's, so they need not come together."""
    dut = ChainUpstream()
    if resets:
        dut = ResetInserter(["wr", "rd"])(dut)
    used = find_sig(dut, "used")
    S = dict(t=0, segs=[], lost=0, fifo_max=0, mem_done=False, cpu_done=False,
             mem_start={}, cpu_ready={}, hold_hi=0, stuck_span=None)

    def member_gen():
        for g, pre, f in members:
            for _ in range(g):
                yield
            w = wire(f, pre)
            S["mem_start"].setdefault(f, S["t"])
            for i, b in enumerate(w):
                yield dut.rx_valid.eq(1)
                yield dut.rx_data.eq(b)
                yield dut.rx_last.eq(i == len(w) - 1)
                yield
            yield dut.rx_valid.eq(0)
            yield dut.rx_last.eq(0)
        S["mem_done"] = True

    @passive
    def cpu_gen():
        lat, rate, hd_in, hd_out = cpu["lat"], cpu["rate"], cpu["hd_in"], cpu["hd_out"]
        pend = collections.deque(sorted(cpus, key=lambda x: x[0]))
        for r, f in pend:
            S["cpu_ready"][f] = r
        hold_h = collections.deque([0] * (hd_in + 1), maxlen=hd_in + 1)
        held_h = collections.deque([0] * (hd_out + 1), maxlen=hd_out + 1)
        commit = None              # [bytes left to commit]
        emitq = collections.deque()
        cur, ci, last_end, nfr = None, 0, -100, 0
        pause = 0
        t = 0
        while True:
            hold_h.append((yield dut.hold))
            hs = hold_h[0]
            if (commit is None and cur is None and not emitq and pend and pend[0][0] <= t
                    and not hs and t - last_end > 12):
                r, f = pend.popleft()
                commit = [len(f) - 4]
                emitq.append((t + lat, wire(f), nfr))
                nfr += 1
            if commit is not None and not pause:
                commit[0] -= rate
                if commit[0] <= 0:
                    commit = None
            held_h.append(int(hs and commit is None and not pause))
            yield dut.held.eq(held_h[0])
            # SmolEth's tx_waiting: held back with a frame ready to go.
            waiting = int(hs and commit is None and cur is None and not emitq
                          and bool(pend) and pend[0][0] <= t)
            yield dut.cpu_waiting.eq(waiting)
            if cur is None and emitq and emitq[0][0] <= t:
                _, cur, k = emitq.popleft()
                ci = 0
            v = 0
            if cur is not None:
                if pause:
                    pause -= 1
                    if stuck and stuck[0] == "stream":
                        v = 1
                        yield dut.mac.data.eq(0xa5)
                        yield dut.mac.last.eq(0)
                    if not pause:
                        S["stuck_span"] = (S["stuck_span"][0], t)
                else:
                    if stuck and k == 0 and ci == stuck[1] and S["stuck_span"] is None:
                        pause = stuck[2]
                        S["stuck_span"] = (t, None)
                        if stuck[0] == "stream":
                            v = 1
                            yield dut.mac.data.eq(0xa5)
                            yield dut.mac.last.eq(0)
                    else:
                        v = 1
                        yield dut.mac.data.eq(cur[ci])
                        yield dut.mac.last.eq(ci == len(cur) - 1)
                        ci += 1
                        if ci == len(cur):
                            cur, last_end = None, t
            yield dut.mac.valid.eq(v)
            if not v:
                yield dut.mac.last.eq(0)
            if not pend and commit is None and cur is None and not emitq:
                S["cpu_done"] = True
            yield
            t += 1

    def rst_gen(dom):
        def g():
            while True:
                r = 1 if S["t"] in resets.get(dom, ()) else 0
                yield getattr(dut, "reset_" + dom).eq(r)
                if dom == "wr":
                    # What Chain wires to ChainUpstream.wr_reset: the write
                    # side's synchronised reset, visible to the reader.
                    yield dut.wr_reset.eq(r)
                yield
        return passive(g)()

    def watch():
        cur, idle, quiet_for = None, 0, 0
        # A MAC beat must appear on port 0 exactly UP_TX_LATENCY cycles later.
        pend_mac = collections.deque([None] * UP_TX_LATENCY)
        while S["t"] < max_rd:
            tv = yield dut.tx_valid
            td = yield dut.tx_data
            mv = yield dut.mac.valid
            md = yield dut.mac.data
            due = pend_mac.popleft()
            if due is not None and (not tv or td != due):
                S["lost"] += 1
            pend_mac.append(md if mv else None)
            if used is not None:
                S["fifo_max"] = max(S["fifo_max"], (yield used))
            S["hold_hi"] += (yield dut.hold)
            if tv:
                if cur is None:
                    cur = bytearray()
                    S["segs"].append((S["t"], cur, idle))
                cur.append(td)
                idle, quiet_for = 0, 0
            else:
                cur = None
                idle += 1
                quiet_for += 1
            if S["mem_done"] and S["cpu_done"] and quiet_for > settle:
                break
            yield
            S["t"] += 1

    gens = {"wr": [member_gen()], "rd": [cpu_gen(), watch()]}
    if resets:
        gens["wr"].append(rst_gen("wr"))
        gens["rd"].append(rst_gen("rd"))
    run_simulation(dut, gens, clocks={"wr": wr_period, "rd": rd_period})
    S["segs"] = [(c, bytes(b), i) for c, b, i in S["segs"]]
    return S


def classify(S, members, cpus):
    """Label each output segment: cpu / member / cut (bad FCS) / split (no
    preamble: the tail of an underrun) / CORRUPT (valid FCS, not an input).
    A member frame seen twice is a duplicate; callers count those."""
    mset = {f for _, _, f in members}
    cset = {f for _, f in cpus}
    out = []
    for c, w, idle in S["segs"]:
        if w[:8] == PRE:
            body = w[8:]
            if body in cset:
                lab = "cpu"
            elif body in mset:
                lab = "member"
            elif fcs_ok(body):
                lab = "CORRUPT"
            else:
                lab = "cut"
        else:
            # No preamble: the reader stalled mid-frame and resumed. A
            # receiver cannot find an SFD in it, so it is lost, not altered.
            body = w
            lab = "split" if (body in mset or body in cset or not fcs_ok(w)) else "CORRUPT"
        out.append((c, lab, body, idle))
    return out


def common_checks(S, cls, members, cpus, what):
    labs = collections.Counter(l for _, l, _, _ in cls)
    check(labs["CORRUPT"] == 0, f"{what}: {labs['CORRUPT']} frames with a VALID FCS that match no input")
    check(S["lost"] == 0, f"{what}: {S['lost']} MAC beats not forwarded (overlap / CPU frame cut)")
    got_c = [b for _, l, b, _ in cls if l == "cpu"]
    want_c = [f for _, f in sorted(cpus, key=lambda x: x[0])]
    check(got_c == want_c, f"{what}: CPU frames {len(got_c)}/{len(want_c)} intact/in order")
    got_m = [b for _, l, b, _ in cls if l == "member"]
    want_m = [f for _, _, f in members if not f[0] & 1]
    it = iter(want_m)
    check(all(any(g == w for w in it) for g in got_m), f"{what}: member frames reordered/duplicated")
    return labs


def uniq_raw(rng, dst, total):
    """A unique frame of `total` bytes (FCS included)."""
    n = max(total - 18, 46)
    body = (dst + SRV_MAC + b"\x88\xb5" + bytes(rng.randrange(256) for _ in range(n)))
    return with_fcs(body)


# Upstream cases ------------------------------------------------------------

def case7():
    res = []
    for wr, rd, name in ((10000, 10002, "port 1 200 ppm FAST"), (10002, 10000, "port 1 200 ppm SLOW")):
        rng = random.Random(7)
        members = [(12, 7, uniq_raw(rng, HEAD_MAC, 1518)) for _ in range(50)]
        cpus = [(2000 + 7000 * k, uniq_raw(rng, SRV_MAC, rng.choice((64, 300, 1000, 1518)))) for k in range(10)]
        S = run_up(members, cpus, wr_period=wr, rd_period=rd)
        cls = classify(S, members, cpus)
        labs = common_checks(S, cls, members, cpus, name)
        check(labs["split"] == 0, f"{name}: {labs['split']} underrun splits")
        res.append(f"{name}: {labs['member']}/50 member whole, {labs['cut']} cut (bad FCS), "
                   f"10/10 CPU, fifo max {S['fifo_max']}/{UP_DEPTH}")
    return "; ".join(res)


def case7b():
    """No CPU traffic at all, port 1 back-to-back at line rate, equal clocks.
    ARCH.md §14.2: 'The FIFO only fills while this card's CPU holds port 1.'"""
    res, fails = [], []
    for size, n in ((1518, 50), (64, 200)):
        rng = random.Random(size)
        members = [(12, 7, uniq_raw(rng, HEAD_MAC, size)) for _ in range(n)]
        S = run_up(members, [])
        cls = classify(S, members, [])
        labs = common_checks(S, cls, members, [], f"{size}B")
        sp = sorted(b[0] - a[0] for a, b in zip(cls, cls[1:]) if a[1] == b[1] == "member")
        per = sp[len(sp) // 2]
        lin = size + 20
        res.append(f"{size}B x{n}: {labs['member']} whole, {labs['cut']} cut, {n - len(cls)} dropped at admission; "
                   f"out {per} cyc/frame vs in {lin} (sustains {100 * lin / per:.0f}% of line rate), "
                   f"fifo max {S['fifo_max']}")
        if labs["member"] != n:
            fails.append(size)
    check(not fails, "member frames LOST with NO CPU traffic -- " + "; ".join(res) +
          ". Root cause: every member frame costs PRE 8 + GAP 12 + COOL 64 + ~3 FSM cycles, "
          "COOL unconditional (rfsm COOL state), so output < input rate and the FIFO fills")
    return "; ".join(res)


def case8():
    res = []
    for mname, model in (("ideal", IDEAL), ("realistic", REAL)):
        rng = random.Random(8)
        members = [(12, 7, uniq_raw(rng, HEAD_MAC, 1518)) for _ in range(34)]
        cpus = [(1500, uniq_raw(rng, SRV_MAC, rng.choice((64, 300, 600)))) for _ in range(20)]
        S = run_up(members, cpus, cpu=model)
        cls = classify(S, members, cpus)
        common_checks(S, cls, members, cpus, mname)
        lat = []
        for c, l, b, _ in cls:
            if l == "cpu":
                lat.append(c - S["cpu_ready"][b])
        check(len(lat) == 20, f"{mname}: only {len(lat)}/20 CPU frames got out")
        # Fairness: one CPU frame per member frame -- the k-th CPU frame waits
        # at most k member slots (frame + PRE + GAP + COOL + CPU frame + slack).
        slot = 1518 + 8 + 12 + 64 + 600 + 8 + 12 + 40
        worst_ratio = max(l / ((k + 1) * slot) for k, l in enumerate(lat))
        check(worst_ratio <= 1.0, f"{mname}: CPU frame latency beyond the alternation bound ({worst_ratio:.2f})")
        labs = collections.Counter(l for _, l, _, _ in cls)
        res.append(f"{mname}: 20/20 CPU out, worst latency {max(lat)} cyc (first {lat[0]}), "
                   f"member {labs['member']} whole / {labs['cut']} cut")
    return "; ".join(res)


def case9():
    res = []
    for mode in ("pause", "stream"):
        rng = random.Random(9)
        STUCK = 20000
        members = [(rng.randrange(300, 900), 7, uniq_raw(rng, HEAD_MAC, 500)) for _ in range(40)]
        cpus = [(100, uniq_raw(rng, SRV_MAC, 600)), (30000, uniq_raw(rng, SRV_MAC, 200))]
        S = run_up(members, cpus, stuck=(mode, 300, STUCK), max_rd=60000)
        a, b = S["stuck_span"]
        check(b is not None, f"{mode}: stuck span never ended")
        # The stuck CPU frame comes out in pieces by construction; drop it.
        stuck_f = cpus[0][1]
        cls = classify(S, members, cpus)
        during = [l for c, l, _, _ in cls if a < c < b and l in ("member", "cut")]
        check(not during, f"{mode}: {len(during)} member frames emitted while the CPU was mid-frame")
        check(S["lost"] == 0, f"{mode}: {S['lost']} MAC beats not forwarded")
        check(not any(l == "CORRUPT" for _, l, _, _ in cls), f"{mode}: corrupt frame")
        after = [l for c, l, _, _ in cls if c > b and l == "member"]
        check(len(after) > 0, f"{mode}: member path never recovered")
        check(any(l == "cpu" and body == cpus[1][1] for _, l, body, _ in cls), f"{mode}: later CPU frame lost")
        n_whole = sum(l == "member" for _, l, _, _ in cls)
        res.append(f"{mode}: none emitted during {STUCK} stuck cycles, fifo max {S['fifo_max']}, "
                   f"{n_whole}/40 member whole after release, rest dropped at admission")
    return "; ".join(res)


def case9c():
    """Commit-to-wire latency tolerance. `held` = hold & ~in_frame, and
    in_frame ends when the CPU's last word ENTERS LiteEth, not when it leaves.
    For a short frame committed faster than it drains, held can come back
    while the frame is still inside the MAC pipeline. The arbiter's only
    protection is quiet >= 32. Sweep the pipeline latency."""
    rows, safe_at_real = [], None
    first_bad = None
    for lat in (0, 12, 24, 32, 40, 48, 64):
        rng = random.Random(90 + lat)
        members = [(rng.randrange(20, 400), 7, uniq_raw(rng, HEAD_MAC, 64)) for _ in range(60)]
        t, cpus = 200, []
        for _ in range(60):
            t += rng.randrange(60, 400)
            cpus.append((t, uniq_raw(rng, SRV_MAC, 64)))
        model = dict(REAL, lat=lat)
        S = run_up(members, cpus, cpu=model, max_rd=60000)
        if S["lost"] and first_bad is None:
            first_bad = lat
        rows.append(f"L={lat}:{'LOST ' + str(S['lost']) if S['lost'] else 'ok'}")
        if lat == REAL["lat"]:
            safe_at_real = S["lost"] == 0
    check(safe_at_real, "MAC beats lost at the realistic latency: " + " ".join(rows))
    return ("tolerance sweep (64-B CPU frames, 1.28 B/cyc commit, hold 7 / held 2 cyc sync): "
            + " ".join(rows) + (f" -> safe below L~{first_bad}; LiteEth TX path is ~10-15" if first_bad else ""))


def case10():
    rng = random.Random(10)
    dsts = {
        "broadcast": b"\xff" * 6,
        "IPv4 mcast 01:00:5e": bytes.fromhex("01005e0000fb"),
        "IPv6 mcast 33:33": bytes.fromhex("333300000001"),
        "group bit only 01:00:00..": bytes.fromhex("010000000000"),
        "unicast fe:ff:ff:ff:ff:ff": bytes.fromhex("feffffffffff"),
        "unicast 00:ff:ff:ff:ff:ff": bytes.fromhex("00ffffffffff"),
        "unicast 02:.. (local)": bytes.fromhex("020000000099"),
        "head unicast": HEAD_MAC,
    }
    members = [(200, 7, uniq_raw(rng, d, 120)) for d in dsts.values()]
    S = run_up(members, [])
    cls = classify(S, members, [])
    common_checks(S, cls, members, [], "group")
    got = {b for _, l, b, _ in cls if l == "member"}
    wrong = []
    for (name, d), (_, _, f) in zip(dsts.items(), members):
        if (f in got) != (not d[0] & 1):
            wrong.append(name)
    check(not wrong, f"misrouted: {wrong}")
    check(len(cls) == sum(not d[0] & 1 for d in dsts.values()), "extra output frames")
    return "broadcast + 3 multicast dropped; 4 unicasts incl. fe:ff:ff:ff:ff:ff delivered (bit 0 only)"


def case11():
    rng = random.Random(11)
    sizes = (2000, 1518, 1599, 1600, 1601, 64)
    members = [(12, 7, uniq_raw(rng, HEAD_MAC, s)) for s in sizes]
    S = run_up(members, [])
    cls = classify(S, members, [])
    common_checks(S, cls, members, [], "overlong")
    check(len(cls) == len(sizes), f"{len(cls)} out for {len(sizes)} in")
    labs = [l for _, l, _, _ in cls]
    lens = [len(b) for _, _, b, _ in cls]
    check(labs[0] == "cut" and lens[0] < 2000, f"2000-B frame not cut ({labs[0]}, {lens[0]})")
    check(labs[1] == "member", "frame after the overlong one damaged")
    check(labs[2] == "member" and labs[5] == "member", "1599 / 64 not delivered")
    note = ""
    if labs[3] != "member":
        note = (f"; NOTE 1600-B frame is CUT (out {lens[3]} B) although UP_MAX_FRAME's comment says 1600 is admitted "
                f"-- cut_short fires at count == max_frame-1: the real limit is 1599, harmless")
    return f"2000-B cut to {lens[0]} B with bad FCS, next frame intact; 1599 ok, 1601 cut{note}"


def case12():
    res = []
    for mname, model in (("ideal", IDEAL), ("realistic", REAL)):
        rng = random.Random(12)
        members = [(rng.choice((12, 12, 50, 300)), 7, uniq_raw(rng, HEAD_MAC, rng.choice((64, 300, 1518))))
                   for _ in range(40)]
        t, cpus = 100, []
        for _ in range(30):
            t += rng.randrange(0, 1500)
            cpus.append((t, uniq_raw(rng, SRV_MAC, rng.choice((64, 300, 1518)))))
        S = run_up(members, cpus, cpu=model)
        cls = classify(S, members, cpus)
        common_checks(S, cls, members, cpus, mname)
        mins = {}
        for (_, l0, _, _), (_, l1, _, idle) in zip(cls, cls[1:]):
            k = ("cpu" if l0 == "cpu" else "mem") + "->" + ("cpu" if l1 == "cpu" else "mem")
            mins[k] = min(mins.get(k, 1 << 30), idle)
        check(all(v >= 12 for v in mins.values()), f"{mname}: IFG < 12: {mins}")
        check(len(mins) == 4, f"{mname}: not every transition exercised: {mins}")
        res.append(f"{mname} min IFG " + " ".join(f"{k}={v}" for k, v in sorted(mins.items())))
    return "; ".join(res)


def case13():
    # Forwarder: reset in the middle of a discovery frame's header, and in
    # the middle of a plain frame.
    frames = [raw(HEAD_MAC, 200), disc(0), raw(HEAD_MAC, 300), disc(4), raw(HEAD_MAC, 46)]
    items = [(12, wire(f)) for f in frames]
    starts, t = [], 0
    for g, w in items:
        t += g; starts.append(t); t += len(w)
    resets = (starts[1] + 8 + 30, starts[2] + 100)
    segs, quiet = run_fwd(items, reset_at=resets)
    check(quiet, "forwarder: no idle at end")
    # Frames after the last reset must be exact.
    tail = [(c, o) for c, o, _ in segs if c > starts[3]]
    expect_fwd(items[3:], [(c, o, 0) for c, o in tail], what="forwarder after reset")
    hit = [o for c, o, _ in segs if c <= starts[3]]
    blessed = [o for o in hit if split_wire(o)[1] is not None and fcs_ok(split_wire(o)[1])
               and split_wire(o)[1] not in [fwd_ref(f) for f in frames]]
    check(not blessed, "forwarder: a damaged frame came out with a valid FCS")
    fwd_msg = "forwarder: frames after mid-frame resets exact, nothing damaged blessed"

    # Upstream. Each scenario: 16 unicast frames, a reset in the middle of
    # the 6th's arrival (which is also mid-output), one CPU frame late on.
    # A 0xd5 run in the reset frame's tail tempts the writer's PREAMBLE state.
    def scenario(resets):
        rng = random.Random(13)
        members = []
        for k in range(16):
            if k == 5:
                f = with_fcs(HEAD_MAC + SRV_MAC + b"\x88\xb5" + bytes([0xd5]) * 40 + bytes(rng.randrange(256) for _ in range(250)))
            else:
                f = uniq_raw(rng, HEAD_MAC, 300)
            members.append((100, 7, f))
        cpus = [(6500, uniq_raw(rng, SRV_MAC, 200))]
        S = run_up(members, cpus, resets=resets, max_rd=40000)
        cls = classify(S, members, cpus)
        got = [b for _, l, b, _ in cls if l == "member"]
        dup = sum(c - 1 for c in collections.Counter(got).values() if c > 1)
        labs = collections.Counter(l for _, l, _, _ in cls)
        late = [f for _, _, f in members[8:]]
        missing = sum(f not in got for f in late)
        return dict(dup=dup, corrupt=labs["CORRUPT"], damaged=labs["cut"] + labs["split"],
                    whole=labs["member"], missing_late=missing, cpu=labs["cpu"], lost=S["lost"])

    T = 2300        # mid member frame 5: arriving on port 1 AND going out of port 0
    out, fails = [], []
    # Chain gives both sides ONE reset (either PHY's reset resets both), so a
    # reset that starts on one side reaches the other within the synchroniser
    # latency. Modelled as both sides at T and at T+2.
    for name, resets in (("both", {"wr": {T}, "rd": {T}}),
                         ("port-1 PHY reset (both sides, per Chain)", {"wr": {T}, "rd": {T + 2}}),
                         ("port-0 PHY reset (both sides, per Chain)", {"rd": {T}, "wr": {T + 2}}),
                         # Since 3.0.5 Chain resets the READ side with port 0
                         # only: a port-1 PHY reset resets the writer alone,
                         # and the reader must resynchronise by itself.
                         ("port-1 PHY reset (write side only)", {"wr": {T}})):
        r = scenario(resets)
        out.append(f"{name}: {r['whole']} whole, {r['damaged']} bad-FCS, {r['dup']} DUPLICATES, "
                   f"{r['corrupt']} valid-FCS garbage, {r['missing_late']}/8 later frames missing, cpu {r['cpu']}/1")
        if r["dup"] or r["corrupt"] or r["missing_late"] or r["cpu"] != 1:
            fails.append(name)
    check(not fails, f"{fwd_msg}; UPSTREAM after a reset of {fails} is not clean -- " + "; ".join(out))
    return fwd_msg + "; upstream " + "; ".join(out)



def case14():
    """A CPU frame released during a SHORT hold-low pulse (GAP -> IDLE -> WAIT
    with `held` still stale) must not collide with the next member frame.
    Found by review: held_for did not see hold drop, so the 96-cycle guard was
    skipped. Pass 1 finds the short pulses for several member gaps; pass 2
    makes a CPU frame ready exactly as each pulse reaches the CPU."""
    worst = []
    for gap in (22, 26, 30, 35):
        rng = random.Random(3 + gap)
        members = [(gap, 7, uniq_raw(rng, HEAD_MAC, 64)) for _ in range(12)]
        dut = ChainUpstream()
        trace = []

        def mem():
            for g, pre, f in members:
                for _ in range(g):
                    yield
                w = wire(f, pre)
                for i, b in enumerate(w):
                    yield dut.rx_valid.eq(1)
                    yield dut.rx_data.eq(b)
                    yield dut.rx_last.eq(i == len(w) - 1)
                    yield
                yield dut.rx_valid.eq(0)
                yield dut.rx_last.eq(0)

        def rd():
            hist = collections.deque([0] * 10, maxlen=10)
            for _ in range(2000):
                h = yield dut.hold
                trace.append(h)
                hist.append(h)
                yield dut.held.eq(hist[0])
                yield

        run_simulation(dut, {"wr": [mem()], "rd": [rd()]}, clocks={"wr": 8, "rd": 8})
        pulses, t = [], 1
        while t < len(trace):
            if trace[t - 1] and not trace[t]:
                k = 0
                while t + k < len(trace) and not trace[t + k]:
                    k += 1
                if t + k < len(trace) and k < 14:
                    pulses.append((t, k))
                t += k
            t += 1
        for tf, k in pulses[:3]:
            cpus = [(tf + REAL["hd_in"], uniq_raw(rng, SRV_MAC, 64))]
            S = run_up(members, cpus, cpu=REAL, max_rd=4000)
            cls = classify(S, members, cpus)
            labs = collections.Counter(l for _, l, _, _ in cls)
            worst.append((gap, k, S["lost"], labs["cpu"]))
    bad = [w for w in worst if w[2] or w[3] != 1]
    check(not bad, f"CPU bytes lost or CPU frame missing (gap, pulse, lost, cpu): {bad}")
    return f"{len(worst)} CPU frames aligned with short hold-low pulses: none lost" if worst else \
        "no short hold-low pulses occur (IDLE dwell never exposed)"

def case15():
    """Shared reset with port 1's RX clock STOPPED. Chain resets both FIFO
    sides together, but each side applies it only on ITS OWN clock edges.
    The head's PHY reset pin (rst_n) is shared by both PHY chips on rev 8.x,
    and LiteEthPHYHWReset pulses it on every sys reset: both RXCs stop, and
    on release each side comes out of reset when its PHY's clock returns.
    If port 0's comes back first (or port 1's never does: no link and a PHY
    that gates RXC), the read side is reset and running while the write side
    still holds its stale wptr/wframes. Modelled with CEInserter on `wr`
    (clock stopped) and the reset held on `wr` until it is clocked again,
    which is what the AsyncResetSynchronizer's preset flops do."""
    def scenario(wr_stopped):
        rng = random.Random(15)
        members = [(100, 7, uniq_raw(rng, HEAD_MAC, 300)) for _ in range(10)]
        dut = CEInserter(["wr"])(ResetInserter(["wr", "rd"])(ChainUpstream()))
        T, RESUME = 6000, 9000
        S = dict(t=0, segs=[])

        def mem():
            for g, pre, f in members:
                for _ in range(g):
                    yield
                w = wire(f, pre)
                for i, b in enumerate(w):
                    yield dut.rx_valid.eq(1)
                    yield dut.rx_data.eq(b)
                    yield dut.rx_last.eq(i == len(w) - 1)
                    yield
                yield dut.rx_valid.eq(0)
                yield dut.rx_last.eq(0)

        @passive
        def wr_ctl():
            while True:
                t = S["t"]
                stopped = wr_stopped and T - 10 <= t < RESUME
                yield dut.ce_wr.eq(0 if stopped else 1)
                # The reset synchroniser holds rst until its clock returns.
                r = 1 if T <= t < (RESUME + 2 if wr_stopped else T + 2) else 0
                yield dut.reset_wr.eq(r)
                # The synchroniser's output is asynchronous: the reader sees it
                # with the write clock stopped (Chain -> ChainUpstream.wr_reset).
                yield dut.wr_reset.eq(r)
                yield

        def rd():
            cur = None
            while S["t"] < 14000:
                t = S["t"]
                yield dut.reset_rd.eq(1 if T <= t < T + 2 else 0)
                yield dut.held.eq(1)
                if (yield dut.tx_valid):
                    if cur is None:
                        cur = bytearray()
                        S["segs"].append((t, cur))
                    cur.append((yield dut.tx_data))
                else:
                    cur = None
                yield
                S["t"] += 1

        run_simulation(dut, {"wr": [mem(), wr_ctl()], "rd": [rd()]}, clocks={"wr": 8, "rd": 8})
        mset = {f for _, _, f in members}
        before = sum(1 for c, w in S["segs"] if c < T and w[:8] == PRE and bytes(w[8:]) in mset)
        replay = [c for c, w in S["segs"] if c >= T and w[:8] == PRE and bytes(w[8:]) in mset]
        junk = [c for c, w in S["segs"] if c >= T and not (w[:8] == PRE and bytes(w[8:]) in mset)]
        return before, replay, junk, sum(c >= RESUME for c in replay)

    b0, r0, j0, _ = scenario(False)
    b1, r1, j1, late = scenario(True)
    check(b0 == 10 and not r0, f"control (port 1 clock running) not clean: {b0}/10 before, {len(r0)} replayed")
    check(not r1, f"port 1's RX clock stopped across the shared reset: {b1}/10 frames delivered before the "
                  f"reset, then {len(r1)} copies REPLAYED out of port 0 ({len(r1) - late} while port 1 was stopped, {late} after it resumed) with their original valid FCS "
                  f"(+{len(j1)} junk segments) -- the read side reset and ran on the write side's stale "
                  f"wptr/wframes (chain.py Chain: up_rst is applied per domain, on each domain's own clock)")
    return f"stopped port-1 clock across the reset: no replay ({len(j1)} junk)"


def case16():
    """Port 1's RX clock has never run (nothing on port 2 and a PHY that
    gates RXC without link, or PHY1 held in reset). On ECP5 the reset
    synchroniser is two FD1S3BX preset flops, which configure to 1 and clear
    only on clock edges, so ResetSignal("eth1_rx") stays 1. Chain ORs it into
    the reset of BOTH FIFO sides -- including up_rd, which owns the
    registered port-0 TX output that the CPU's own frames go through. Built
    from the real Chain with stub PHYs and a GSR-faithful model of the
    synchroniser."""
    from migen.genlib.resetsync import AsyncResetSynchronizer
    from liteeth.common import eth_phy_description
    from litex.soc.interconnect import stream
    from chain import Chain

    class _ARS(Module):
        def __init__(self, cd, async_reset):
            r1 = Signal(reset=1, reset_less=True)
            r2 = Signal(reset=1, reset_less=True)
            getattr(self.sync, cd.name).__iadd__([r1.eq(async_reset), r2.eq(r1 | async_reset)])
            self.comb += cd.rst.eq(r2 | async_reset)

    class _ARSLower:
        @staticmethod
        def lower(dr):
            return _ARS(dr.cd, dr.async_reset)

    class _Phy:
        def __init__(self):
            self.source = stream.Endpoint(eth_phy_description(8))
            self.sink = stream.Endpoint(eth_phy_description(8))
            self.crg = None
            self.rx_clk_freq = self.tx_clk_freq = 125e6

    class Top(Module):
        def __init__(self):
            self.clock_domains.cd_sys = ClockDomain()
            self.clock_domains.cd_eth_rx = ClockDomain()
            self.clock_domains.cd_eth_tx = ClockDomain()
            self.clock_domains.cd_eth1_rx = ClockDomain()
            # The PHY CRGs' synchronisers (LiteEthPHYRGMIICRG), reset CSR 0.
            self.specials += [AsyncResetSynchronizer(self.cd_eth_rx, 0),
                              AsyncResetSynchronizer(self.cd_eth_tx, 0),
                              AsyncResetSynchronizer(self.cd_eth1_rx, 0)]
            self.phy0, self.phy1 = _Phy(), _Phy()
            self.submodules.chain = Chain(self.phy0, self.phy1)

    def run(p1_period):
        top = Top()
        frame = uniq_raw(random.Random(16), SRV_MAC, 64)
        w = wire(frame)
        out = bytearray()

        def cpu():
            for _ in range(200):
                yield
            snk = top.chain.phy.sink
            for i, b in enumerate(w):
                yield snk.valid.eq(1)
                yield snk.data.eq(b)
                yield snk.last.eq(i == len(w) - 1)
                yield
            yield snk.valid.eq(0)
            for _ in range(100):
                yield

        @passive
        def watch():
            while True:
                if (yield top.phy0.sink.valid):
                    out.append((yield top.phy0.sink.data))
                yield

        run_simulation(top, {"eth_tx": [cpu()], "up_rd": [watch()]},
                       clocks={"sys": 25, "eth_rx": 8, "eth_tx": 8, "up_rd": 8,
                               "eth1_rx": p1_period, "up_wr": p1_period},
                       special_overrides={AsyncResetSynchronizer: _ARSLower})
        return bytes(out) == w, len(out)

    ok_run, n_run = run(8)
    ok_dead, n_dead = run(10 ** 9)
    check(ok_run, f"control: with port 1's clock running the CPU frame did not go out ({n_run} bytes)")
    check(ok_dead, f"port 1's RX clock never started: this card's OWN CPU frame never left port 0 "
                   f"({n_dead}/{len(wire(b''))+64} bytes) -- ResetSignal('eth1_rx') is stuck at its "
                   f"configured 1 and Chain ORs it into up_rd's reset, freezing the port-0 TX register")
    return "CPU TX independent of port 1's clock"


CASES = [
    ("1", "forwarders in series (2 and 3 hops)", case1),
    ("2", "min IFG + preambles 7/5/1", case2),
    ("2x", "probe: IFG+preamble below legal after discovery", case2x),
    ("3", "discovery near misses + truncations", case3),
    ("3x", "probe: anti=0, IP fragment, SFD-less burst, bad-FCS discovery", case3x),
    ("4", "runts, 1518, 1522, 9000 jumbo", case4),
    ("5", "discovery v2 clock bytes", case5),
] + [("6", "random fuzz, 300 frames (4 parallel streams of 75)", lambda k=k: case6(k, 4)) for k in range(4)] + [
    ("7", "upstream 200 ppm drift both ways + CPU", case7),
    ("7b", "upstream sustained line rate, no CPU", case7b),
    ("8", "starvation / CPU fairness", case8),
    ("9", "held never asserted: CPU stuck mid-frame", case9),
    ("9c", "probe: CPU pipeline latency vs quiet>=32", case9c),
    ("10", "group addressing (bit 0 only)", case10),
    ("11", "overlong member frame", case11),
    ("12", "IFG >= 12 on every transition", case12),
    ("13", "reset mid-frame", case13),
    ("14", "CPU frame released in a short hold-low pulse", case14),
    ("15", "shared reset while port 1's RX clock is stopped", case15),
    ("16", "port 1's RX clock never started: CPU TX", case16),
]


def _run(idx):
    cid, name, fn = CASES[idx]
    random.seed(1000 + idx)
    t0 = time.time()
    try:
        msg = fn()
        st = "PASS"
    except CaseFail as e:
        st, msg = "FAIL", str(e)
    except Exception:
        st, msg = "ERROR", traceback.format_exc()
    return cid, name, st, msg, time.time() - t0


if __name__ == "__main__":
    want = set(sys.argv[1:])
    idxs = [i for i, (cid, _, _) in enumerate(CASES) if not want or cid in want]
    t0 = time.time()
    with multiprocessing.Pool(len(idxs)) as pool:
        results = pool.map(_run, idxs)
    merged = collections.OrderedDict()
    for cid, name, st, msg, dt in results:
        if cid in merged:
            m = merged[cid]
            m[1] = st if st != "PASS" else m[1]
            if st != "PASS" or m[1] == "PASS":
                m[2] = msg if st != "PASS" else m[2]
            m[3] = max(m[3], dt)
            m[4] += 1
        else:
            merged[cid] = [name, st, msg, dt, 1]
    bad = 0
    for cid, (name, st, msg, dt, n) in merged.items():
        print(f"[{st}] {cid:>3} {name} ({dt:.0f}s): {msg}")
        bad += st != "PASS"
    print(f"sim-chain-deep: {len(merged) - bad}/{len(merged)} PASS in {time.time() - t0:.0f}s")
    sys.exit(1 if bad else 0)
