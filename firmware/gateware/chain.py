"""Daisy-chaining cards through their second Ethernet port.

A chain is one IP address. The card whose port 0 faces the switch -- the HEAD
-- is an ordinary card: it DHCPs, serves its API and takes pixels on UDP 7000.
Every card behind it is a MEMBER, reached through the head and told apart by
port number: hop h takes pixels on 7000 + h (see SmolEth's `hw_udp_port`).
Bitstream and firmware are identical on every card; the role is decided at
runtime, so a lone card is just the head of a chain of one.

Two paths, both entirely in gateware -- the CPU pixel path tops out near 1250
packets/s, far below what a chain carries:

DOWNSTREAM, port 0 RX -> port 1 TX  (`ChainForwarder`)
    Every UNICAST frame, unmodified, cut-through (group-addressed frames are
    dropped: members need none, and repeating them made a mis-cabled loop a
    broadcast storm). Port 1's TX is clocked by port 0's
    RX clock, so this is a same-clock pass-through with no CDC FIFO and
    thirteen cycles (104 ns) of latency per hop. One exception: a DISCOVERY frame
    (IPv4 UDP to `disc_port`, payload "CH", any destination) has its hop byte incremented
    on the way through, which is how a card learns its position with no
    configuration. Discovery is sent by marquee UNICAST to the head, so the
    frame a member sees also names the head (destination MAC/IP), which the
    member then answers as, and marquee (source), where it fetches its
    config. Its Ethernet CRC is regenerated; the UDP checksum needs no
    fix-up because a second payload byte, in the same byte lane, is
    decremented by the same amount. Every other frame keeps its original FCS,
    so a bit error on a hop is still a CRC error at the next card.

UPSTREAM, port 1 RX -> port 0 TX  (`ChainUpstream`)
    Members' replies to the network, merged into port 0's transmit side with
    this card's own CPU traffic, cut-through via one block RAM (2048 x 9).
    The MAC is never stalled on the 125 MHz side: the CPU's next frame is held
    back in the sys domain (`hold`/`held`) while a member's frame goes out.
    GROUP-ADDRESSED frames are dropped here, never forwarded. That is the
    mechanism by which a member "does not get an IP address": its DHCP
    discover is a broadcast, so it never leaves the chain, and a card whose
    DHCP goes unanswered knows it is not the head.

Wire layout of a discovery frame, offsets from the first byte after the SFD:

     0..5   the head's MAC  6..11  marquee's MAC
    12..13  0x0800          14  0x45 (IPv4, no options)     23  17 (UDP)
    36..37  disc_port (big-endian)
    42..43  'C' 'H'         44  hop       45  version
    46      255 - hop       47  0
"""
import os

from migen import *
from migen.genlib.cdc import MultiReg
from migen.genlib.resetsync import AsyncResetSynchronizer
from litex.gen import Reduce

from litex.soc.interconnect import stream
from litex.soc.interconnect.csr import AutoCSR, CSRStatus, CSRConstant

from liteeth.common import eth_phy_description
from liteeth.mac.crc import LiteEthMACCRCEngine

DISC_PORT = 6990
# Upstream frames are cut at UP_MAX_FRAME - 1 bytes (1599), FCS included and
# preamble not (preamble is regenerated on the way out). 1522 is a VLAN-tagged
# maximum, so nothing legal is ever cut.
UP_MAX_FRAME = 1600
UP_DEPTH = 2048
# Cycles from a beat of the card's own MAC to port 0's TX: the arbiter's
# output register, and the stage that keeps the block RAM's output unmuxed.
UP_TX_LATENCY = 2


def _gray(value):
    return value ^ (value >> 1)


# EXPERIMENT knob for the tightest build: EXP_CHAIN_COUNTERS=0 compiles the
# chain's status counters out (their CSRs stay, reading 0, so the firmware and
# the register map are unchanged). The four-output E320 build is 99% LUT, and
# the counters' adders, gray coders and synchronisers are the cheapest logic
# to give back. exp/build-out4.sh sets it.
COUNTERS = os.environ.get("EXP_CHAIN_COUNTERS", "1") != "0"


class _GrayCounter(Module):
    """A frame counter that can be read from another clock domain.

    Only the gray code crosses, so a read that lands mid-increment is one
    count stale rather than garbage. The firmware converts it back.
    """
    def __init__(self, width=16):
        self.inc = Signal()
        self.gray = Signal(width)
        if not COUNTERS:
            self.comb += self.gray.eq(0)
            return
        count = Signal(width)
        self.sync += [
            If(self.inc, count.eq(count + 1)),
            self.gray.eq(_gray(count)),
        ]


class ChainForwarder(Module):
    """Port 0 RX -> port 1 TX, in port 0's RX clock domain (`sync` here).

    Observes the PHY source; it never drives ready, because the PHY ignores it
    and the MAC core is the other consumer of the same beats.
    """
    def __init__(self, disc_port=DISC_PORT):
        self.rx_valid = Signal()
        self.rx_data = Signal(8)
        self.rx_last = Signal()
        self.tx_valid = Signal()
        self.tx_data = Signal(8)

        self.submodules.frames = _GrayCounter()
        self.submodules.rewrites = _GrayCounter()

        # Input side: where are we in the frame, and is it a discovery frame.
        #
        # TWO STAGES. Stage 0 sees the byte as it arrives and only registers
        # flags -- one per header check, each from the byte and the index
        # alone. Stage 1 combines those flags into the verdict a cycle later.
        # Doing it all in one cycle (index decode, byte compare, the AND of a
        # dozen checks, then the hop/anti logic that depends on it) was eight
        # LUTs deep and the E320's 125 MHz path on a third of the seeds.
        # Only the TAGS on the delay line move a cycle later (they go in at
        # dhop[1]/dant[1], beside the byte they belong to); the data path and
        # its latency are unchanged.
        in_data = Signal()          # past the SFD
        idx = Signal(6)             # byte index after the SFD, saturating
        ok = Signal()               # every check so far has passed
        mod = Signal()              # hop byte was rewritten in this frame
        byte = Signal(8)            # the byte that goes into the delay line

        here = Signal()             # stage 0: a byte past the SFD
        self.comb += here.eq(self.rx_valid & in_data)

        # (index, test on the byte that must hold for a discovery frame)
        checks = [
            (12, self.rx_data == 0x08), (13, self.rx_data == 0x00),
            (14, self.rx_data == 0x45),
            # Not a fragment: MF clear and offset 0 (bytes 20-21, DF may be
            # set). A later fragment whose payload happens to read "CH" at
            # these offsets is not a discovery frame.
            (20, (self.rx_data & 0x3f) == 0), (21, self.rx_data == 0x00),
            (23, self.rx_data == 17),
            (36, self.rx_data == disc_port >> 8),
            (37, self.rx_data == disc_port & 0xff),
            (42, self.rx_data == ord("C")), (43, self.rx_data == ord("H")),
        ]
        bad1 = [Signal() for _ in checks]       # stage 1: check k failed
        hop1 = Signal()             # stage 1: byte 44, and not 0xff
        ant1 = Signal()             # stage 1: byte 46
        anteq1 = Signal()           # stage 1: that byte is 255 - hop
        hop_in = Signal(8)          # the hop byte as it ARRIVED
        anti_ok = Signal()          # anti-hop byte was 255 - hop
        self.sync += [
            [b.eq(here & (idx == i) & ~t) for b, (i, t) in zip(bad1, checks)],
            hop1.eq(here & (idx == 44) & (self.rx_data != 0xff)),
            ant1.eq(here & (idx == 46)),
            # 255 - x is x ^ 0xff on eight bits (not ~x: migen's simulator
            # sign-extends it). hop_in was captured two bytes before.
            anteq1.eq(self.rx_data == (hop_in ^ 0xff)),
            If(here & (idx == 44), hop_in.eq(self.rx_data)),
        ]

        is_hop = Signal()           # stage 1: tag this byte as the hop byte
        is_anti = Signal()          # stage 1: tag this byte as the anti byte
        self.comb += [
            # `ok` has absorbed the checks of every byte before this one.
            is_hop.eq(hop1 & ok),
            is_anti.eq(ant1 & mod),
            # The delay line carries the frame AS IT ARRIVED. The hop/anti bytes
            # are only TAGGED here and rewritten at the output (below), once
            # the input has shown the frame is long enough and its anti byte
            # right -- rewriting at the input altered short or malformed
            # look-alikes that could then never be made valid
            # (sim_chain_deep cases 3, 3x).
            byte.eq(self.rx_data),
        ]
        self.sync += [
            If(self.rx_valid,
                If(~in_data,
                    If(self.rx_data == 0xd5,
                        in_data.eq(1), idx.eq(0), ok.eq(1), mod.eq(0), anti_ok.eq(0),
                    ),
                ).Else(
                    If(idx != 63, idx.eq(idx + 1)),
                ),
                If(self.rx_last, in_data.eq(0)),
            ),
            # Stage 1. Never in the same cycle as an SFD: these flags belong
            # to bytes 12-46, a whole frame and gap before the next SFD.
            If(Reduce("OR", bad1), ok.eq(0)),
            If(is_hop, mod.eq(1)),
            If(is_anti, anti_ok.eq(anteq1)),
        ]

        # Did the frame ARRIVE intact? A second CRC engine over every byte after
        # the SFD, FCS included; an intact frame leaves the LFSR at the IEEE
        # residue. Without this, a discovery frame with a bit error was given
        # a fresh, VALID FCS -- laundered -- and the next card would trust it.
        self.submodules.crc_in = crc_in = LiteEthMACCRCEngine(8, 32, 0x04c11db7)
        crc_in_reg = Signal(32, reset=0xffffffff)
        self.comb += [crc_in.data.eq(self.rx_data), crc_in.crc_prev.eq(crc_in_reg)]
        self.sync += If(~in_data,
            crc_in_reg.eq(0xffffffff),
        ).Elif(self.rx_valid,
            crc_in_reg.eq(crc_in.crc_next),
        )
        # The residue of the reflected IEEE CRC over data + its own FCS, as
        # this engine computes it (checked against zlib in tools/sim_chain.py).
        CRC_RESIDUE = 0xc704dd7b

        # The verdict, latched at `last`: substitute a fresh FCS only for a
        # rewritten discovery frame that arrived intact, was long enough to be
        # one (48 payload-bearing bytes + FCS: idx >= 51 on the last byte), and
        # whose anti-hop byte was right. Anything else that was rewritten keeps
        # its ORIGINAL FCS, which no longer matches, so it is dropped downstream
        # rather than passed on altered. Cleared once its FCS has left, so it
        # cannot leak into a following burst.
        # Timing is counted in CYCLES from the hop byte, which equals bytes only
        # because gigabit RGMII RX_DV has no gaps inside a frame. A PHY that
        # inserted idle cycles between bytes 44 and 51 would break the
        # "rw lands before the hop byte leaves" margin below.
        #
        # `rw`: rewrite this frame's hop/anti bytes. Decided when the input
        # reaches byte 51 -- 7 bytes after the hop byte entered the 11-byte
        # line, so before it leaves -- and only for a header that matched, a
        # hop that can be incremented and an anti byte equal to 255 - hop.
        # `sub`: also give it a fresh FCS, which needs the WHOLE frame to have
        # arrived intact; known at `last`. A rewritten frame whose FCS was bad
        # keeps that bad FCS -- it was an error and stays one.
        rw = Signal()
        sub = Signal()
        sub_done = Signal()
        was_last = Signal()
        self.sync += [
            If(self.rx_valid & in_data & (idx == 51),
                rw.eq(mod & anti_ok),
            ).Elif(sub_done,
                rw.eq(0),
            ),
            # A cycle AFTER `last`, from the register: crc_in_reg then holds
            # the whole frame's residue (it is cleared at the end of this
            # cycle, not before). Comparing crc_next at `last` put the CRC
            # network and a 32-bit compare in one 8 ns cycle -- the E320's
            # 125 MHz critical path at 121 MHz. The verdict is needed only when
            # the FCS reaches the output, at least 7 cycles later.
            If(was_last,
                sub.eq(rw & (crc_in_reg == CRC_RESIDUE)),
            ).Elif(sub_done,
                sub.eq(0),
            ),
            was_last.eq(self.rx_valid & self.rx_last),
        ]
        self.comb += [
            self.frames.inc.eq(self.rx_valid & self.rx_last),
        ]

        # Delay line, DLY bytes. Two jobs need it:
        #
        # * The FCS is the last four bytes, known to be the FCS only once
        #   `last` arrives; each entry carries its `last` flag, so the four
        #   FCS positions are read off the flags as they reach the output.
        # * GROUP-ADDRESSED FRAMES ARE NOT FORWARDED. A member needs none of
        #   them (discovery, pixels, TFTP and TCP are all unicast to the head),
        #   and repeating them made a mis-cabled loop -- port 2 back into the
        #   switch -- a broadcast storm. The verdict comes at the first byte
        #   after the SFD; with a 7-byte preamble that is 8 bytes into the
        #   frame, so the line must hold more than that for the frame's FIRST
        #   preamble byte to still be waiting when the verdict lands. 11
        #   leaves margin; the frame then never starts on the wire at all.
        DLY = 11
        dv = [Signal() for _ in range(DLY)]
        dd = [Signal(8) for _ in range(DLY)]
        di = [Signal() for _ in range(DLY)]   # byte is past the SFD
        dl = [Signal() for _ in range(DLY)]   # byte is the frame's last
        dfs = [Signal() for _ in range(DLY)]  # byte is the frame's first
        dhop = [Signal() for _ in range(DLY)]  # byte is a candidate hop byte
        dant = [Signal() for _ in range(DLY)]  # byte is a candidate anti byte
        prev_valid = Signal()
        first = Signal()
        self.sync += prev_valid.eq(self.rx_valid)
        self.comb += first.eq(self.rx_valid & ~prev_valid)
        self.sync += [
            dv[0].eq(self.rx_valid), dd[0].eq(byte), di[0].eq(in_data & self.rx_valid),
            dl[0].eq(self.rx_valid & self.rx_last), dfs[0].eq(first),
            # The tags are stage 1's: they join their byte at entry 1.
            dhop[1].eq(is_hop), dant[1].eq(is_anti),
        ] + [
            [dv[n].eq(dv[n - 1]), dd[n].eq(dd[n - 1]), di[n].eq(di[n - 1]),
             dl[n].eq(dl[n - 1]), dfs[n].eq(dfs[n - 1])]
            for n in range(1, DLY - 1)
        ] + [
            [dhop[n].eq(dhop[n - 1]), dant[n].eq(dant[n - 1])]
            for n in range(2, DLY - 1)
        ] + [
            # The last stage takes the REWRITTEN byte, so the +/-1 is a cycle
            # ahead of the output CRC instead of in series with it (that
            # adder->CRC chain was the E320's 125 MHz path at 122 MHz). `rw`
            # is settled by now: it lands 7 bytes after the hop byte entered.
            dv[DLY - 1].eq(dv[DLY - 2]), di[DLY - 1].eq(di[DLY - 2]),
            dl[DLY - 1].eq(dl[DLY - 2]), dfs[DLY - 1].eq(dfs[DLY - 2]),
            dhop[DLY - 1].eq(dhop[DLY - 2]), dant[DLY - 1].eq(dant[DLY - 2]),
            dd[DLY - 1].eq(dd[DLY - 2]),
            If(dhop[DLY - 2] & rw, dd[DLY - 1].eq(dd[DLY - 2] + 1)),
            If(dant[DLY - 2] & rw, dd[DLY - 1].eq(dd[DLY - 2] - 1)),
        ]
        o = DLY - 1

        # The verdict, per frame. Cleared at each frame's start so a runt that
        # ends before its first address byte is not judged by the frame before.
        group = Signal()
        self.sync += If(first,
            group.eq(0),
        ).Elif(self.rx_valid & in_data & (idx == 0),
            group.eq(self.rx_data[0]),
        )
        out_drop = Signal()
        drop_now = Signal()
        self.comb += drop_now.eq(Mux(dfs[o], group, out_drop))
        self.sync += If(dv[o], out_drop.eq(drop_now))
        self.submodules.dropped = _GrayCounter()
        self.comb += self.dropped.inc.eq(dv[o] & dfs[o] & group)

        # Which FCS byte is at the output: the frame's last byte is 3, 2, 1 or
        # 0 entries behind it.
        fcs_pos = Signal(3)         # 0 = not FCS, else 1..4 = FCS byte 0..3
        self.comb += [
            If(dl[o - 3], fcs_pos.eq(1)),
            If(dl[o - 2], fcs_pos.eq(2)),
            If(dl[o - 1], fcs_pos.eq(3)),
            If(dl[o], fcs_pos.eq(4)),
        ]

        # The bare engine, not LiteEthMACCRC32: in the LiteEth this image
        # pins, LiteEthMACCRC32 declares `value` and never drives it, so a
        # forwarder built on it emits a constant where the FCS belongs. The
        # FCS is the register complemented and bit-reversed, low byte first --
        # checked against zlib.crc32 in tools/sim_chain.py.
        out_byte = Signal(8)
        self.comb += out_byte.eq(dd[o])

        self.submodules.crc = crc = LiteEthMACCRCEngine(8, 32, 0x04c11db7)
        crc_reg = Signal(32, reset=0xffffffff)
        crc_val = Signal(32)
        self.comb += [
            crc.data.eq(out_byte),
            crc.crc_prev.eq(crc_reg),
            crc_val.eq(~crc_reg[::-1]),
        ]
        self.sync += If(~(dv[o] & di[o]),
            crc_reg.eq(0xffffffff),
        ).Elif(fcs_pos == 0,
            crc_reg.eq(crc.crc_next),
        )

        fcs_byte = Signal(8)
        self.comb += Case(fcs_pos, {
            1: fcs_byte.eq(crc_val[0:8]),
            2: fcs_byte.eq(crc_val[8:16]),
            3: fcs_byte.eq(crc_val[16:24]),
            "default": fcs_byte.eq(crc_val[24:32]),
        })
        self.comb += [
            sub_done.eq(dv[o] & dl[o]),
            self.rewrites.inc.eq(sub_done & sub & ~drop_now),
        ]
        self.sync += [
            self.tx_valid.eq(dv[o] & ~drop_now),
            self.tx_data.eq(Mux((fcs_pos != 0) & sub, fcs_byte, out_byte)),
        ]


class ChainUpstream(Module):
    """Port 1 RX -> FIFO -> port 0 TX, merged with the card's own MAC TX.

    Clock domains: `wr` is port 1's RX clock, `rd` is port 0's TX clock. The
    caller renames them.
    """
    def __init__(self, depth=UP_DEPTH, max_frame=UP_MAX_FRAME, cut=32):
        aw = log2_int(depth)
        # Port 1 RX.
        self.rx_valid = Signal()
        self.rx_data = Signal(8)
        self.rx_last = Signal()
        # This card's own MAC, ahead of the PHY.
        self.mac = mac = stream.Endpoint(eth_phy_description(8))
        # Port 0 TX, registered: the arbiter's mux must not feed the PHY's
        # DDR output flops combinationally at 125 MHz.
        self.tx_valid = Signal()
        self.tx_data = Signal(8)
        tx_valid = Signal()
        tx_data = Signal(8)         # every source but the block RAM

        self.submodules.up_frames = ClockDomainsRenamer("wr")(_GrayCounter())
        self.submodules.up_dropped = ClockDomainsRenamer("wr")(_GrayCounter())

        mem = Memory(9, depth)
        wport = mem.get_port(write_capable=True, clock_domain="wr")
        rport = mem.get_port(clock_domain="rd")
        self.specials += mem, wport, rport

        # Pointers carry a wrap bit. Frame counts tell the reader a whole
        # frame is waiting; the write pointer tells it a frame in progress is
        # far enough ahead to start on.
        wptr = Signal(aw + 1)
        rptr = Signal(aw + 1)
        wframes = Signal(8)
        rframes = Signal(8)

        wptr_gray = Signal(aw + 1)
        rptr_gray = Signal(aw + 1)
        wframes_gray = Signal(8)
        rptr_gray_w = Signal(aw + 1)
        wptr_gray_r = Signal(aw + 1)
        wframes_gray_r = Signal(8)
        self.sync.wr += [wptr_gray.eq(_gray(wptr)), wframes_gray.eq(_gray(wframes))]
        self.sync.rd += rptr_gray.eq(_gray(rptr))
        self.specials += [
            MultiReg(rptr_gray, rptr_gray_w, "wr"),
            MultiReg(wptr_gray, wptr_gray_r, "rd"),
            MultiReg(wframes_gray, wframes_gray_r, "rd"),
        ]

        # Bit i of the binary value is the XOR of gray bits i..n-1. Written as
        # one independent reduction per bit -- a LUT tree two deep -- NOT as
        # the textbook ripple (b[i] = b[i+1] ^ g[i]). The ripple was the first
        # version and it was the critical path of the whole 125 MHz RX domain:
        # 11 chained LUTs, 8.6 ns against 8.0, on the 5A-75E build.
        #
        # That was not enough. Written as independent reductions it is still
        # ONE combinational function, and synthesis is free to share the XORs
        # back into the ripple -- which it did: gateware 3.5.0, seed 2, this
        # conversion was 10 LUTs deep, 8.3 ns, and the whole RX domain closed
        # at 120.98 MHz. So the conversion is now two REGISTERED steps, each
        # one 4-input LUT deep whatever synthesis does with it: step one XORs
        # within groups of four bits (and each group's parity), step two adds
        # the parities of the groups above.
        def ungray(g, n, sync):
            ng = (n + 3) // 4
            part = Signal(n)
            par = Signal(ng)
            out = Signal(n)
            for k in range(ng):
                hi = min(4 * k + 4, n)
                sync += par[k].eq(Reduce("XOR", [g[j] for j in range(4 * k, hi)]))
                for i in range(4 * k, hi):
                    sync += part[i].eq(Reduce("XOR", [g[j] for j in range(i, hi)]))
            for i in range(n):
                above = [par[m] for m in range(i // 4 + 1, ng)]
                sync += out[i].eq(Reduce("XOR", [part[i]] + above))
            return out

        # Registered conversions: stale by two cycles, which only ever makes
        # the writer's space test more conservative and the reader two cycles
        # later. The pointer and the frame count are delayed alike.
        rptr_w = ungray(rptr_gray_w, aw + 1, self.sync.wr)
        wptr_r = ungray(wptr_gray_r, aw + 1, self.sync.rd)
        wframes_r = ungray(wframes_gray_r, 8, self.sync.rd)

        # Write side ---------------------------------------------------------
        #
        # CUT-THROUGH, not store-and-forward. Store-and-forward was the first
        # version and the simulation killed it: a 1518-byte frame holds its
        # space until it has entirely LEFT, so the next back-to-back frame
        # failed a "room for a whole frame" test and was dropped -- exactly
        # the multi-segment TCP reply a member's web page is made of. Both
        # clocks of a hop are 125 MHz within 200 ppm, 0.3 bytes over a whole
        # frame, so the reader can safely start once CUT bytes are in. The
        # FIFO then only fills while this card's CPU holds port 0.
        #
        # If it does fill, the frame is ended early with a terminator. Its FCS
        # is then wrong and the next receiver drops it: a frame is delivered
        # whole or not at all, never altered. One slot is held back so the
        # terminator always fits.
        used = Signal(aw + 1)
        nearly_full = Signal()
        self.sync.wr += used.eq(wptr - rptr_w)
        # Registered, with two more slots of margin to pay for the extra cycle
        # of staleness: the compare fed the write FSM and the pointer's next
        # value (8.2 ns, 121.9 MHz, gateware 3.5.0 seed 7).
        self.sync.wr += nearly_full.eq(used >= depth - 6)
        count = Signal(max=max_frame + 1)
        # `count == max_frame - 1`, a cycle early and registered: the compare
        # sat in front of the write data's mux.
        count_last = Signal()
        wfsm = ClockDomainsRenamer("wr")(FSM(reset_state="PREAMBLE"))
        self.submodules.wfsm = wfsm
        # The write port is REGISTERED: the block RAM's data and enable pins
        # were the end of a path from the frame counter and the PHY's valid
        # through the terminator mux (8.1 ns, 123.5 MHz, same build). The
        # write lands a cycle after the pointer moves, which the reader cannot
        # see: the pointer reaches it through a gray code, a two-flop
        # synchroniser and the conversion above, five cycles at the least.
        w_we = Signal()
        w_dat = Signal(9)
        w_we_q = Signal()
        w_adr_q = Signal(aw)
        w_dat_q = Signal(9)
        self.sync.wr += [w_we_q.eq(w_we), w_adr_q.eq(wptr[:aw]), w_dat_q.eq(w_dat)]
        self.comb += [
            wport.adr.eq(w_adr_q),
            wport.dat_w.eq(w_dat_q),
            wport.we.eq(w_we_q),
            w_dat.eq(Cat(self.rx_data, self.rx_last)),
        ]
        wfsm.act("PREAMBLE",
            If(self.rx_valid & (self.rx_data == 0xd5) & ~self.rx_last,
                NextState("FIRST"),
            ),
        )
        # Byte 0 is the destination MAC's first byte; bit 0 is the group bit.
        # Deciding here, before anything is written, means a dropped frame
        # never needs rolling back.
        wfsm.act("FIRST",
            If(self.rx_valid,
                If(self.rx_data[0] | nearly_full | self.rx_last,
                    self.up_dropped.inc.eq(1),
                    If(self.rx_last, NextState("PREAMBLE")).Else(NextState("DROP")),
                ).Else(
                    w_we.eq(1),
                    NextValue(wptr, wptr + 1),
                    NextValue(count, 1),
                    NextValue(count_last, 0),
                    NextState("COPY"),
                ),
            ).Else(
                NextState("PREAMBLE"),
            ),
        )
        # Terminate early on: FIFO nearly full, an overlong frame, or the PHY
        # dropping valid without `last`. In every case `last` is written, so
        # the reader can never run on into the next frame.
        cut_short = Signal()
        self.comb += cut_short.eq(nearly_full | count_last | ~self.rx_valid)
        wfsm.act("COPY",
            w_we.eq(1),
            NextValue(wptr, wptr + 1),
            NextValue(count, count + 1),
            NextValue(count_last, count == max_frame - 2),
            If(cut_short,
                w_dat.eq(0x100),
                NextValue(wframes, wframes + 1),
                self.up_dropped.inc.eq(1),
                If(~self.rx_valid | self.rx_last,
                    NextState("PREAMBLE"),
                ).Else(
                    NextState("DROP"),
                ),
            ).Elif(self.rx_last,
                NextValue(wframes, wframes + 1),
                self.up_frames.inc.eq(1),
                NextState("PREAMBLE"),
            ),
        )
        wfsm.act("DROP",
            If(~self.rx_valid | self.rx_last, NextState("PREAMBLE")),
        )

        # Read side / arbiter ------------------------------------------------
        #
        # The MAC is NEVER stalled here. Its `ready` is a constant 1, as the PHY
        # always gave it -- which is what lets LiteEth's TX CDC FIFO read logic
        # collapse. A stallable `ready` put that logic in this 125 MHz domain
        # for real and, at 97% LUT on the E320, its routing alone was 6.4 ns:
        # 115 MHz. Instead, before a member's frame goes out, `hold` asks the
        # CPU side (SmolEth, sys domain, plenty of slack) to keep its NEXT frame
        # back at a frame boundary; `held` says it has. Once held and the MAC
        # stream has been idle long enough to have drained the CDC FIFO, the
        # member's frame goes. Afterwards `hold` stays low for COOL cycles so a
        # waiting CPU frame always gets through -- the CPU is never starved.
        #
        # Every decision is from a REGISTER. The first version's paths: the FIFO
        # level subtraction feeding empty/avail feeding the FSM (111.8 MHz on
        # the 5A-75E), and the block RAM's unregistered 5.8 ns output feeding
        # the end-of-frame decision (94 MHz on the E320).
        # in, asynchronous: the write side's reset, as its synchroniser drives
        # it -- asserted the moment either PHY resets, clock or no clock.
        self.wr_reset = Signal()
        self.hold = Signal()        # out, to the sys domain (rd domain here)
        self.held = Signal()        # in, synchronised into rd by the caller
        self.cpu_waiting = Signal() # in: the CPU has a frame held back
        self.comb += mac.ready.eq(1)
        level = Signal(aw + 1)
        avail = Signal()
        empty = Signal()
        last_r = Signal()
        quiet = Signal(6)           # cycles the MAC stream has been idle
        # Cycles `held` has been continuously true. A CPU frame committed just
        # before the hold took effect is still inside LiteEth's TX pipeline,
        # where `quiet` cannot see it yet; once held has stood for longer than
        # that pipeline's latency, anything committed before has reached the
        # MAC stream and reset `quiet` (sim_chain_deep case 9c).
        held_for = Signal(7)
        pre = Signal(3)
        cnt = Signal(7)
        # The read side trusts the write side's pointers only once it has SEEN
        # the write side reset. Both sides reset together, but a reset only
        # takes effect on its own domain's clock, and port 2's RX clock stops
        # while its PHY is in reset (on rev 8.x both PHYs share one reset pin)
        # or never runs at all with nothing cabled. The read side used to run
        # off the write side's stale pointers in that window and replayed old
        # member frames with their valid FCS (sim_chain_deep case 15).
        #
        # So while the write side is in reset -- its synchronised reset, which
        # asserts asynchronously and so is seen with its clock stopped -- and
        # for SETTLE cycles after, the reader ADOPTS the write side's pointers
        # instead of reading: the FIFO looks empty, nothing stale can go out,
        # and whatever the writer does next starts from where it really is.
        # The CPU's own frames pass through regardless (mac_through), so a
        # card whose port 2 never clocks still talks (case 16).
        wr_rst = Signal()
        self.specials += MultiReg(self.wr_reset, wr_rst, "rd", reset=1)
        settle = Signal(4, reset=15)
        # Latched until the adoption has actually HAPPENED, which is only ever
        # in IDLE or COOL: a writer reset seen mid-frame used to clear itself
        # (settle runs out in 15 cycles, a frame's stall watchdog takes 255)
        # before the reader got back to IDLE, and the reader then carried on
        # from its stale pointer and replayed the FIFO (case 13, write side
        # only).
        need_adopt = Signal(reset=1)
        adopt_now = Signal()
        synced = Signal()
        self.comb += synced.eq(~need_adopt)
        self.sync.rd += [
            If(wr_rst, settle.eq(15)).Elif(settle != 0, settle.eq(settle - 1)),
            If(wr_rst, need_adopt.eq(1)).Elif(adopt_now, need_adopt.eq(0)),
        ]
        self.comb += level.eq(wptr_r - rptr)
        # Registered, so one cycle stale while rptr advances by at most one a
        # cycle: hence the margin of 3. A frame already written whole is never
        # "empty" -- otherwise its last two bytes would wait for the NEXT frame.
        self.sync.rd += [
            empty.eq(~synced | ((level < 3) & (wframes_r == rframes))),
            avail.eq(synced & ((wframes_r != rframes) | (level >= cut))),
            If(mac.valid, quiet.eq(0)).Elif(quiet != 63, quiet.eq(quiet + 1)),
            # Reset while `hold` itself is low, not only `held`: after a short
            # IDLE dwell `held` is still the stale 1 (its round trip through the
            # sys domain is ~14 cycles), and a CPU frame released during that
            # dwell would otherwise collide with the next member frame (found by
            # review; sim_chain_deep case 14).
            If(~self.held | ~self.hold, held_for.eq(0)
            ).Elif(held_for != 127, held_for.eq(held_for + 1)),
        ]
        clear = Signal()
        self.comb += clear.eq(self.held & (held_for >= 96) & (quiet >= 32))

        rfsm = ClockDomainsRenamer("rd")(FSM(reset_state="IDLE"))
        self.submodules.rfsm = rfsm

        # The block RAM is read with NOTHING on either side of it but a
        # register. Its unregistered output takes 5.8 ns of the 8 ns cycle,
        # and the first versions put a mux after it (into tx_data) and a
        # pointer decision before it (stall -> last_r -> rptr + 1 -> address):
        # between them the E320's 125 MHz path on half the seeds.
        #
        # * `dq` takes the RAM output straight in. Port 0's output is one
        #   stage later for it, so every other source goes through a matching
        #   stage (v1/d1) and the stream stays aligned: one extra cycle, 8 ns,
        #   on everything port 0 sends.
        # * The address is a two-way choice between registers: `rptr`, or
        #   `rptr_p1` (always rptr + 1) when a byte goes out this cycle
        #   (`adv`), which needs only registered inputs.
        # * End of frame (`last_r`) is the stored flag of the byte that went
        #   out last cycle, read from `dq` -- no longer a register loaded from
        #   the RAM output through the FSM.
        rptr_p1 = Signal(aw + 1, reset=1)
        adv = Signal()              # a member byte goes out this cycle
        sent = Signal()             # ... and did last cycle (dq holds it)
        dq = Signal(9)
        v1 = Signal()
        d1 = Signal(8)
        stall_max = Signal()        # stall == 255, from a register
        self.comb += [
            rport.adr.eq(Mux(adv, rptr_p1[:aw], rptr[:aw])),
            last_r.eq(sent & dq[8]),
        ]
        self.sync.rd += [
            dq.eq(rport.dat_r),
            sent.eq(adv),
            v1.eq(tx_valid),
            d1.eq(tx_data),
            self.tx_valid.eq(v1),
            self.tx_data.eq(Mux(sent, dq[:8], d1)),
            If(adopt_now,
                rptr.eq(wptr_r), rptr_p1.eq(wptr_r + 1),
            ).Elif(adv,
                rptr.eq(rptr_p1), rptr_p1.eq(rptr_p1 + 1),
            ),
        ]

        # MAC frames pass straight through in every state but PRE/UP/GAP.
        mac_through = [tx_valid.eq(mac.valid), tx_data.eq(mac.data)]
        self.comb += adopt_now.eq(need_adopt & ~wr_rst & (settle == 0)
                                  & (rfsm.ongoing("IDLE") | rfsm.ongoing("COOL")))
        adopt = [If(adopt_now, NextValue(rframes, wframes_r))]
        rfsm.act("IDLE",
            *mac_through,
            If(~synced, *adopt).Elif(avail, NextState("WAIT")),
        )
        # 32 idle cycles after `held`: the CDC FIFO's latency is under ten, and
        # once a frame starts it streams without gaps (sys datapath 32 bits at
        # 35 MHz outruns 8 bits at 125), so nothing can still be on its way.
        rfsm.act("WAIT",
            *mac_through,
            self.hold.eq(1),
            If(~synced,
                NextState("IDLE"),
            ).Elif(clear,
                NextValue(pre, 0),
                NextState("PRE"),
            ),
        )
        # Seven 0x55 and the SFD. rport.adr already points at the first byte,
        # so its data is there the cycle after the SFD goes out.
        rfsm.act("PRE",
            self.hold.eq(1),
            tx_valid.eq(1),
            tx_data.eq(Mux(pre == 7, 0xd5, 0x55)),
            NextValue(pre, pre + 1),
            If(pre == 7, NextState("UP")),
        )
        # rport.dat_r is mem[rptr], the byte about to go out (`dq` has it a
        # cycle later, on its way to port 0). End of frame is
        # acted on the cycle AFTER the last byte left, from last_r -- by then
        # rptr already names the next frame's first byte, so nothing rewinds.
        # Running dry mid-frame cannot happen within 200 ppm after a CUT-byte
        # head start; if it ever does, the frame goes out split (two bad FCS,
        # both dropped) rather than with stale bytes in it.
        # Watchdog: a frame that stops arriving mid-way (port 2's link drops
        # with no terminator written) must not hold the CPU off port 1 for
        # ever. Its remainder, if it ever comes, goes out as a frame of its own
        # with a bad FCS and is dropped by the switch.
        stall = Signal(8)
        self.sync.rd += If(rfsm.ongoing("UP") & empty,
            If(stall != 255, stall.eq(stall + 1)),
            stall_max.eq(stall >= 254),
        ).Else(stall.eq(0), stall_max.eq(0))
        self.comb += adv.eq(rfsm.ongoing("UP") & ~stall_max & ~last_r & ~empty)
        rfsm.act("UP",
            self.hold.eq(1),
            tx_valid.eq(adv),
            If(stall_max,
                NextValue(cnt, 1),
                NextState("GAP"),
            ).Elif(last_r,
                NextValue(rframes, rframes + 1),
                NextValue(cnt, 1),
                NextState("GAP"),
            ),
        )
        # 12-byte inter-frame gap, still holding the MAC back. Then: the CPU
        # gets the port if it is waiting (COOL); else another member frame goes
        # straight out, keeping up with line rate -- a cool-down after EVERY
        # member frame cost 64 cycles each and a burst of small frames ran at
        # 56% of line rate and overflowed (sim_chain_deep case 7b).
        rfsm.act("GAP",
            self.hold.eq(1),
            NextValue(cnt, cnt + 1),
            If(cnt == 11,
                NextValue(cnt, 0),
                If(self.cpu_waiting,
                    NextState("COOL"),
                ).Elif(avail & clear,
                    NextValue(pre, 0),
                    NextState("PRE"),
                ).Else(
                    NextState("IDLE"),
                ),
            ),
        )
        # `hold` low long enough for the sys domain to see it (a sys cycle is
        # under four of these) and start a waiting CPU frame, which then holds
        # the stream itself until it ends.
        rfsm.act("COOL",
            *mac_through,
            If(~synced, *adopt),
            NextValue(cnt, cnt + 1),
            If(cnt == 63, NextValue(cnt, 0), NextState("IDLE")),
        )


class ChainPHY(Module):
    """What the MAC core sees in place of port 0's PHY.

    `LiteEthMACCore` only needs `dw`, `sink` and `source`: the source is port
    0's own RX, untouched; the sink feeds the upstream arbiter instead of the
    PHY directly.
    """
    dw = 8

    def __init__(self, phy0):
        self.sink = stream.Endpoint(eth_phy_description(8))
        self.source = phy0.source
        self.crg = phy0.crg
        self.rx_clk_freq = phy0.rx_clk_freq
        self.tx_clk_freq = phy0.tx_clk_freq


class Chain(Module, AutoCSR):
    """Both chain paths, wired between two RGMII PHYs.

    `phy0` is the upstream port (towards the switch), in domains eth_rx /
    eth_tx. `phy1` is the downstream port, its domains renamed eth1_rx /
    eth1_tx, and its TX clock MUST be phy0's RX clock (`tx_clk=` on the PHY):
    that is what makes the downstream path a same-clock pass-through.
    """
    def __init__(self, phy0, phy1, disc_port=DISC_PORT):
        self.submodules.forwarder = fwd = ClockDomainsRenamer("eth_rx")(
            ChainForwarder(disc_port))
        # The upstream FIFO's two sides. The WRITE side resets with either
        # PHY (a head reboot must not keep stale member frames). The READ
        # side resets with port 0's TX only: it carries the CPU's own frames
        # to the switch, and tying it to port 2's reset held it in reset --
        # every CPU frame silently lost -- whenever port 2's RX clock does not
        # run, because a reset synchroniser only clears on clock edges
        # (sim_chain_deep case 16). It does not need port 2's reset anyway: it
        # watches the write side's and resynchronises itself (ChainUpstream,
        # `synced`; cases 13 and 15).
        self.clock_domains.cd_up_wr = ClockDomain()
        self.clock_domains.cd_up_rd = ClockDomain()
        wr_rst = Signal()
        self.comb += [
            self.cd_up_wr.clk.eq(ClockSignal("eth1_rx")),
            self.cd_up_rd.clk.eq(ClockSignal("eth_tx")),
            wr_rst.eq(ResetSignal("eth1_rx") | ResetSignal("eth_tx")),
        ]
        self.specials += [
            AsyncResetSynchronizer(self.cd_up_wr, wr_rst),
            AsyncResetSynchronizer(self.cd_up_rd, ResetSignal("eth_tx")),
        ]
        self.submodules.upstream = up = ClockDomainsRenamer(
            {"wr": "up_wr", "rd": "up_rd"})(ChainUpstream())
        self.comb += up.wr_reset.eq(ResetSignal("up_wr"))
        self.phy = ChainPHY(phy0)

        # The forwarder gets its OWN copy of port 1's RX, one register later:
        # the discovery match then starts from flops placed beside it rather
        # than from the PHY's, across the die (the E320's 125 MHz path, 6 ns
        # of it routing).
        self.sync.eth_rx += [
            fwd.rx_valid.eq(phy0.source.valid),
            fwd.rx_data.eq(phy0.source.data),
            fwd.rx_last.eq(phy0.source.last),
        ]
        self.comb += [
            phy1.sink.valid.eq(fwd.tx_valid),
            phy1.sink.data.eq(fwd.tx_data),

            up.rx_valid.eq(phy1.source.valid),
            up.rx_data.eq(phy1.source.data),
            up.rx_last.eq(phy1.source.last),
            phy0.sink.valid.eq(up.tx_valid),
            phy0.sink.data.eq(up.tx_data),
        ]
        # The MAC is never stalled (see ChainUpstream): `hold` crosses to the
        # sys domain, where SmolEth keeps the CPU's next frame back, and `held`
        # crosses back.
        self.tx_hold = Signal()     # sys domain: give to SmolEth
        self.tx_held = Signal()     # sys domain: driven by SmolEth
        self.tx_waiting = Signal()  # sys domain: driven by SmolEth
        self.comb += self.phy.sink.connect(up.mac)
        # `hold` is FSM decode -- registered here so no glitch of it can
        # cross (a glitch low would let a CPU frame start mid member frame).
        hold_r = Signal()
        self.sync.up_rd += hold_r.eq(up.hold)
        self.specials += [
            MultiReg(hold_r, self.tx_hold, "sys"),
            MultiReg(self.tx_held, up.held, "up_rd"),
            MultiReg(self.tx_waiting, up.cpu_waiting, "up_rd"),
        ]

        # Status, all gray-coded (the firmware converts). Frame counts only:
        # enough to say which way traffic is or is not flowing on a hop.
        self.disc_port = CSRConstant(disc_port)
        self.fwd_frames = CSRStatus(16, description="Frames forwarded downstream (gray code)")
        self.fwd_rewrites = CSRStatus(16, description="Discovery frames whose hop was incremented (gray code)")
        self.fwd_dropped = CSRStatus(16, description="Group-addressed frames NOT forwarded downstream (gray code)")
        self.up_frames = CSRStatus(16, description="Frames queued upstream from port 1 (gray code)")
        self.up_dropped = CSRStatus(16, description="Port 1 frames dropped: group-addressed, FIFO full or runt (gray code)")
        self.specials += [
            MultiReg(fwd.frames.gray, self.fwd_frames.status),
            MultiReg(fwd.rewrites.gray, self.fwd_rewrites.status),
            MultiReg(fwd.dropped.gray, self.fwd_dropped.status),
            MultiReg(up.up_frames.gray, self.up_frames.status),
            MultiReg(up.up_dropped.gray, self.up_dropped.status),
        ]
