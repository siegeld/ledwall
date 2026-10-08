# Smol tcp ethernet stack
# Intended for a hybrid stack, where a lot of udp traffic is received and processed by gateware, while all other ethernet tasks are handled using the softcore
# Only the softcore can send data
# Benefits:
# - Uses less resources than LiteETH (less features)
# - Can share a single IP
# Disadvantages:
# - Only supports a single IP & UDP port for hardware processing
# - Hardware receiving circuitry needs to signal packets that shouldn't be received by the
#   softcore and some leftovers may still be received by the softcore
# - Needs a functioning software ethernet stack to provide ARP at least
# - Less configurable
# - No etherbone
# License: Same one as LiteETH (two clause BSD)
# 2021-2022 <d-smoleth@sawatzke.dev>
# SPDX-License-Identifier: BSD-2-Clause
from liteeth.common import *
from liteeth.core.udp import LiteEthUDPDepacketizer
from liteeth.core.ip import LiteEthIPV4Depacketizer
from liteeth.mac.common import LiteEthMACDepacketizer
from liteeth.mac.core import LiteEthMACCore
from liteeth.mac.wishbone import LiteEthMACWishboneInterface


class SmolEthUDP(Module):
    def __init__(self, udp_port, dw=8):
        self.sink = sink = stream.Endpoint(eth_ipv4_user_description(dw))
        self.source = source = stream.Endpoint(eth_udp_user_description(dw))

        # Depacketizer.
        self.submodules.depacketizer = depacketizer = LiteEthUDPDepacketizer(dw)

        # Data-Path.
        self.comb += [
            sink.connect(depacketizer.sink),
        ]

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")

        fsm.act(
            "IDLE",
            If(
                depacketizer.source.valid,
                NextState("DROP"),
                If(
                    (sink.protocol == udp_protocol)
                    & (depacketizer.source.dst_port == udp_port),
                    NextState("RECEIVE"),
                ),
            ),
        )
        fsm.act(
            "RECEIVE",
            depacketizer.source.connect(
                source, keep={"data", "last_be", "last", "valid", "ready"}
            ),
            source.ip_address.eq(sink.ip_address),
            source.length.eq(depacketizer.source.length - udp_header.length),
            If(source.valid & source.ready & source.last, NextState("IDLE")),
        )

        fsm.act(
            "DROP",
            depacketizer.source.ready.eq(1),
            If(depacketizer.source.valid & depacketizer.source.last, NextState("IDLE")),
        )


class SmolEthIP(Module):
    def __init__(self, ip_address, protocol, dw=8):
        self.sink = sink = stream.Endpoint(eth_mac_description(dw))
        self.source = source = stream.Endpoint(eth_ipv4_user_description(dw))

        # # #

        # Depacketizer.
        self.submodules.depacketizer = depacketizer = LiteEthIPV4Depacketizer(dw)
        self.comb += sink.connect(depacketizer.sink)

        # FSM.
        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act(
            "IDLE",
            If(
                depacketizer.source.valid,
                NextState("DROP"),
                If(
                    (depacketizer.source.target_ip == ip_address)
                    & (depacketizer.source.version == 0x4)
                    & (depacketizer.source.ihl == 0x5)
                    & (depacketizer.source.protocol == protocol),
                    NextState("RECEIVE"),
                ),
            ),
        )
        self.comb += [
            depacketizer.source.connect(
                source, keep={"last", "protocol", "data", "error", "last_be"}
            ),
            source.length.eq(depacketizer.source.total_length - (0x5 * 4)),
            source.ip_address.eq(depacketizer.source.sender_ip),
        ]
        fsm.act(
            "RECEIVE",
            depacketizer.source.connect(source, keep={"valid", "ready"}),
            If(source.valid & source.ready & source.last, NextState("IDLE")),
        )
        fsm.act(
            "DROP",
            depacketizer.source.ready.eq(1),
            If(
                depacketizer.source.valid
                & depacketizer.source.last
                & depacketizer.source.ready,
                NextState("IDLE"),
            ),
        )


class SmolEthMACFilter(Module):
    def __init__(self, mac_address, dw=8):

        self.sink = sink = stream.Endpoint(eth_phy_description(dw))
        self.source = source = stream.Endpoint(eth_mac_description(dw))

        # # #

        # Depacketizer.
        self.submodules.depacketizer = depacketizer = LiteEthMACDepacketizer(dw)
        self.comb += sink.connect(depacketizer.sink)

        # FSM.
        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act(
            "IDLE",
            If(
                depacketizer.source.valid,
                NextState("DROP"),
                If(
                    (depacketizer.source.target_mac == mac_address)
                    & (depacketizer.source.ethernet_type == ethernet_type_ip),
                    NextState("RECEIVE"),
                ),
            ),
        )
        self.comb += [
            depacketizer.source.connect(
                source, keep={"last", "data", "error", "last_be"}
            ),
        ]
        fsm.act(
            "RECEIVE",
            depacketizer.source.connect(source, keep={"valid", "ready"}),
            If(source.valid & source.ready & source.last, NextState("IDLE")),
        )
        fsm.act(
            "DROP",
            depacketizer.source.ready.eq(1),
            If(
                depacketizer.source.valid & depacketizer.source.last,
                NextState("IDLE"),
            ),
        )


class SmolEthStreamSplitter(Module):
    def __init__(self, description):
        self.sink = sink = stream.Endpoint(description)
        self.source1 = source1 = stream.Endpoint(description)
        self.source2 = source2 = stream.Endpoint(description)

        source1_already_read = Signal()
        source2_already_read = Signal()

        self.comb += [
            self.sink.connect(self.source1, omit={"ready", "valid"}),
            self.sink.connect(self.source2, omit={"ready", "valid"}),
            self.source1.valid.eq(self.sink.valid & ~source1_already_read),
            self.source2.valid.eq(self.sink.valid & ~source2_already_read),
            self.sink.ready.eq(
                (self.source1.ready | source1_already_read)
                & (self.source2.ready | source2_already_read)
            ),
        ]

        self.sync += [
            If(
                self.sink.valid & self.sink.ready,
                # Reset ready state
                source1_already_read.eq(0),
                source2_already_read.eq(0),
            ).Elif(
                self.sink.valid,
                If(
                    self.source1.ready,
                    source1_already_read.eq(1),
                ),
                If(
                    self.source2.ready,
                    source2_already_read.eq(1),
                ),
            )
        ]


class SmolEth(Module, AutoCSR):
    def __init__(self, phy, udp_port, mac_address, ip_address, dw,
                 nrxslots=2, ntxslots=2, with_hw_udp=False):
        """MAC for the CPU, optionally with a hardware UDP receive path.

        with_hw_udp splits the post-MAC RX stream: one copy goes to the CPU as
        before, the other through MAC/IP/UDP filters in gateware, exposing
        `self.udp_source` -- a payload stream for `udp_port` with no CPU
        involvement. That is the ingest half of taking pixels off the CPU.

        The hardware branch must never stall: the splitter only advances when
        BOTH consumers have taken a beat, so a blocked hardware path would
        backpressure the CPU's path and cost packets. See AlwaysReady below.
        """
        assert dw % 8 == 0
        # Add mac & ip registers
        self.ip_address = CSRStorage(32, reset=ip_address, atomic_write=True)
        self.mac_address = CSRStorage(48, reset=mac_address, atomic_write=True)

        self.submodules.core = LiteEthMACCore(
            phy=phy,
            dw=dw,
            with_sys_datapath=True,
            with_preamble_crc=True,
        )

        # Wishbone MAC
        self.rx_slots = CSRConstant(nrxslots)
        self.tx_slots = CSRConstant(ntxslots)
        self.slot_size = CSRConstant(2 ** bits_for(eth_mtu))

        wishbone_interface = LiteEthMACWishboneInterface(
            dw=dw,
            nrxslots=nrxslots,
            ntxslots=ntxslots,
            endianness="little",
        )

        self.submodules.interface = wishbone_interface
        self.ev, self.bus_rx, self.bus_tx = self.interface.sram.ev, self.interface.bus_rx, self.interface.bus_tx
        # Use this instead of AutoCSR to maintain the same names as in liteeth
        self.csrs = (
            self.interface.get_csrs()
            + self.core.get_csrs()
            + [self.ip_address, self.mac_address]
        )

        # CPU TX path is unconditional.
        self.comb += self.interface.source.connect(self.core.sink)

        if not with_hw_udp:
            # Straight through to the CPU, as before.
            self.comb += self.core.source.connect(self.interface.sink)
        else:
            # Duplicate the RX stream: one copy to the CPU, one to gateware.
            self.submodules.splitter = splitter = SmolEthStreamSplitter(
                eth_phy_description(dw))
            self.comb += self.core.source.connect(splitter.sink)

            # CPU branch: kill pixel packets before the MAC can raise an event.
            #
            # SmolEthInvalidator terminates the frame with error=0xFF and
            # last_be=0x01. LiteEthMACSRAMWriter tests `(sink.error &
            # sink.last_be) != 0` on `last` and takes DISCARD, which resets its
            # length and returns to WRITE WITHOUT pushing the slot into
            # stat_fifo -- and that push is what raises the receive event. So
            # the CPU takes no interrupt and the slot is immediately reusable.
            # Verified in liteeth/mac/sram.py, not assumed.
            #
            # DEFAULT OFF. The failure mode of a wrong verdict is a panel that
            # cannot ARP, cannot netboot and cannot be reached over the network,
            # recoverable only over a JTAG link that corrupts most of what it
            # reads. Firmware turns it on after boot, so any mistake costs a
            # power cycle instead.
            self.pixel_filter_enable = CSRStorage(1, reset=0,
                description="Kill UDP pixel packets before the CPU's MAC sees them")
            self.pixel_filter_port = CSRStorage(16, reset=udp_port,
                description="UDP destination port the CPU filter kills")
            self.pixel_filter_killed = CSRStatus(32,
                description="Packets killed on the CPU branch")

            self.submodules.classifier = classifier = SmolEthPixelClassifier(udp_port)
            self.submodules.invalidator = invalidator = SmolEthInvalidator(
                9, eth_phy_description(dw))

            self.comb += [
                # Observer only: taps the same beats the invalidator consumes.
                classifier.sink.data.eq(splitter.source1.data),
                classifier.sink.last.eq(splitter.source1.last),
                classifier.sink.valid.eq(splitter.source1.valid),
                classifier.sink.ready.eq(invalidator.sink.ready),
                classifier.enable.eq(self.pixel_filter_enable.storage),
                classifier.port.eq(self.pixel_filter_port.storage),
                invalidator.invalid.eq(classifier.invalid),
                self.pixel_filter_killed.status.eq(classifier.killed),
            ]
            self.comb += splitter.source1.connect(invalidator.sink)
            self.comb += invalidator.source.connect(self.interface.sink)
            self.csrs += [self.pixel_filter_enable, self.pixel_filter_port,
                          self.pixel_filter_killed]

            # Never let the hardware branch backpressure the CPU branch.
            self.submodules.gate = gate = AlwaysReady(eth_phy_description(dw))
            self.comb += splitter.source2.connect(gate.sink)

            # Narrow the hardware branch to 8 bits. The MAC and the CPU's
            # wishbone interface need dw=32, but the 10-byte header is not a
            # multiple of 4, so parsing at 32 bits leaves every RGB triple
            # straddling beats at a 2-byte offset. At 8 bits it is a plain
            # byte-wise FSM, and 1 byte/cycle at 40MHz is 40 MB/s against a
            # 12.5 MB/s line rate -- three times the headroom needed.
            # Absorb intra-packet bursts. Sustained rate is fine (12.5 MB/s of
            # line rate against 40 MB/s of 8-bit datapath at 40MHz), but the MAC
            # core delivers a packet faster than the 32->8 converter drains it,
            # and every beat the converter refuses costs a whole packet.
            # 2048, not 512. At 512 words this held about 1.4 packets (a
            # 1500-byte frame is 375 words), so ANY stall longer than one
            # packet cost a whole packet: the gate above abandons mid-flight
            # when this refuses a beat, and that is one lost chunk.
            #
            # With the hardware pixel filter on, a lost chunk is not
            # recoverable -- the CPU holds no pixel data, so it cannot repair
            # the frame, and the missing region keeps the frame from TWO
            # frames ago. Measured on the bench card that was 0.86 dropped
            # packets a second, about 4.3% of frames carrying a stale band.
            #
            # 2048 holds about 5.5 packets. RB-5524.
            self.submodules.burst_fifo = burst_fifo = stream.SyncFIFO(
                eth_phy_description(dw), 2048, buffered=True)
            self.comb += gate.source.connect(burst_fifo.sink)

            # Explicit byte serializer, NOT stream.StrideConverter.
            #
            # StrideConverter splits the RAW BIT VECTOR of the whole payload:
            # eth_phy_description(32) is data[32] + last_be[4] + error[4] = 40
            # bits, chopped into 4 x 10-bit chunks that do not land on byte
            # boundaries of `data`. Measured on hardware: a frame whose real
            # destination MAC was 01:00:5e:00:00:fb parsed as 5e:00:00:fb:01:00
            # -- bytes emerging in b2 b3 b4 b5 b0 b1 order.
            self.submodules.narrow = narrow = Bytes32to8()
            self.comb += burst_fifo.source.connect(narrow.sink)

            # Filter against the RUNTIME CSRs, not the build-time constants.
            # The firmware derives its MAC from the flash unique ID and gets its
            # IP from DHCP, so the compiled-in values (0x10e2d5000001 /
            # 192.168.1.50) match nothing on the wire and the hardware path sees
            # no packets at all. These CSRs already existed for this purpose.
            self.submodules.mac_filter = mac_filter = SmolEthMACFilter(
                self.mac_address.storage, 8)
            self.submodules.ip = ip = SmolEthIP(
                self.ip_address.storage, udp_protocol, 8)
            self.submodules.udp = udp = SmolEthUDP(udp_port, 8)
            self.comb += [
                narrow.source.connect(mac_filter.sink),
                mac_filter.source.connect(ip.sink),
                ip.source.connect(udp.sink),
            ]
            self.udp_source = udp.source
            self.dropped_packets = gate.dropped

            # Per-stage packet counters. The hardware path is a pipeline of
            # filters, each of which silently drops anything it does not match,
            # so "nothing arrives at the end" gives no clue which stage is
            # rejecting. One counter per stage turns that into a single reading.
            # What is the MAC depacketizer actually parsing? If the byte order
            # through the 32->8 conversion is wrong, every header field is
            # scrambled and the filter rejects everything with no other clue.
            self.dbg_mac = CSRStatus(48, description="Last parsed destination MAC")
            self.dbg_ethertype = CSRStatus(16, description="Last parsed ethertype")
            self.sync += If(mac_filter.depacketizer.source.valid,
                self.dbg_mac.status.eq(mac_filter.depacketizer.source.target_mac),
                self.dbg_ethertype.status.eq(mac_filter.depacketizer.source.ethernet_type))
            self.csrs += [self.dbg_mac, self.dbg_ethertype]

            # Beat-level counters upstream: a packet counter reads zero both when
            # nothing arrives and when packets arrive but never complete, and
            # those need telling apart.
            self.c_core = CSRStatus(32, description="Beats out of the MAC core")
            self.c_split2 = CSRStatus(32, description="Beats into the hardware branch")
            self.c_gate_out = CSRStatus(32, description="Beats out of the gate")
            self.c_narrow_out = CSRStatus(32, description="Beats out of the 32->8 converter")
            self.c_dropped = CSRStatus(32, description="Packets dropped by the gate")
            for csr, ep in [
                (self.c_core, self.core.source),
                (self.c_split2, splitter.source2),
                (self.c_gate_out, gate.source),
                (self.c_narrow_out, narrow.source),
            ]:
                self.sync += If(ep.valid & ep.ready, csr.status.eq(csr.status + 1))
            self.comb += self.c_dropped.status.eq(gate.dropped)
            self.csrs += [self.c_core, self.c_split2, self.c_gate_out,
                          self.c_narrow_out, self.c_dropped]

            self.n_gate = CSRStatus(32, description="Packets leaving the always-ready gate")
            self.n_narrow = CSRStatus(32, description="Packets leaving the 32->8 converter")
            self.n_mac = CSRStatus(32, description="Packets passing the MAC filter")
            self.n_ip = CSRStatus(32, description="Packets passing the IP filter")
            self.n_udp = CSRStatus(32, description="Payloads passing the UDP port filter")
            for csr, ep in [
                (self.n_gate, gate.source),
                (self.n_narrow, narrow.source),
                (self.n_mac, mac_filter.source),
                (self.n_ip, ip.source),
                (self.n_udp, udp.source),
            ]:
                self.sync += If(ep.valid & ep.ready & ep.last,
                                csr.status.eq(csr.status + 1))
            self.csrs += [self.n_gate, self.n_narrow, self.n_mac, self.n_ip, self.n_udp]

    def get_csrs(self):
        return self.csrs


# Drops the current packet for the ram interface if it's processed by the hardware
# length is the minimum length a packet has to be to be considered valid
class SmolEthPixelClassifier(Module):
    """Decide, early, whether a packet on the CPU branch is pixel traffic.

    A pure OBSERVER. It never sits in the data path and never drives ready or
    valid, because the stream splitter only advances when both consumers take a
    beat -- anything here that backpressures would throttle the DMA branch too,
    which is the path this whole exercise exists to protect.

    Why not reuse SmolEthUDP: that filter sits behind burst_fifo, 512 deep. By
    the time it reaches a verdict the CPU branch is several packets further on,
    so its answer would invalidate THE WRONG PACKET -- intermittent loss of real
    traffic, under load, on the path we are trying to keep clean.

    Byte lanes, from Bytes32to8's docstring: LiteX puts the first wire byte of a
    word in the LOW 8 bits, so wire byte 4N+k of beat N is data[8k:8k+8].

        beat 3   bytes 12,13  ethertype == 0x0800
                 byte  14     version/IHL == 0x45 (IPv4, 20-byte header, which
                              is what puts the UDP ports at 36/37)
        beat 5   byte  23     IP protocol == 17 (UDP)
        beat 9   bytes 36,37  UDP destination port == `port`
        beat 10  bytes 42,43  payload magic -- 'BP' (palette) is NOT killed,
                              because the CPU, not the DMA, writes the palette

    Ports are big-endian on the wire, so the high byte is the LOW lane.

    `invalid` is latched at beat 10 and held to the end of the packet, which is
    what SmolEthInvalidator needs; it is never asserted before beat 10, so a
    short frame cannot be truncated on a verdict that was never reached.
    """

    def __init__(self, port):
        self.sink = sink = stream.Endpoint(eth_phy_description(32))
        self.enable = Signal()
        self.port = Signal(16)
        self.invalid = Signal()
        self.killed = Signal(32)

        beat = Signal(8)
        is_ip = Signal()
        is_udp = Signal()
        matched = Signal()
        port_ok = Signal()
        beat_ok = Signal()

        self.comb += beat_ok.eq(sink.valid & sink.ready)

        self.sync += [
            If(beat_ok,
                If(sink.last,
                    # Ready for the next packet. Clearing on `last` rather than
                    # on the next first beat means a back-to-back packet cannot
                    # inherit the previous verdict.
                    beat.eq(0),
                    is_ip.eq(0),
                    is_udp.eq(0),
                    port_ok.eq(0),
                    matched.eq(0),
                ).Else(
                    beat.eq(beat + 1),
                    If(beat == 3,
                        is_ip.eq((sink.data[0:8] == 0x08)
                                 & (sink.data[8:16] == 0x00)
                                 & (sink.data[16:24] == 0x45)),
                    ),
                    If((beat == 5) & is_ip,
                        is_udp.eq(sink.data[24:32] == 17),
                    ),
                    If((beat == 9) & is_ip & is_udp
                       & (sink.data[0:8] == self.port[8:16])
                       & (sink.data[8:16] == self.port[0:8]),
                        port_ok.eq(1),
                    ),
                    # A PALETTE packet must reach the CPU.
                    #
                    # It arrives on the same port as pixel data, so matching on
                    # the port alone killed it -- and the DMA drops 'BP' too,
                    # on the explicit assumption that "the CPU sees the same
                    # stream". With the filter on, nobody wrote the palette:
                    # indices landed correctly and were looked up in whatever
                    # colours happened to be there already.
                    #
                    # The symptom was an image with the right SHAPE and the
                    # wrong COLOURS, which reads as a corrupt stream rather
                    # than a missing packet -- every counter stayed clean, so
                    # it cost an afternoon to find.
                    #
                    # The payload magic is at wire bytes 42,43, which is beat
                    # 10 -- one beat AFTER the port is known. So the verdict
                    # moves to beat 10 and everything on the port is killed
                    # except 'BP'. The invalidator tolerates this: it waits for
                    # `length_counter > length` before truncating.
                    If((beat == 10) & port_ok
                       & ~((sink.data[16:24] == 0x42)      # 'B'
                           & (sink.data[24:32] == 0x50)),  # 'P' -- palette
                        matched.eq(1),
                    ),
                )
            )
        ]

        self.comb += self.invalid.eq(matched & self.enable)
        self.sync += If(beat_ok & sink.last & matched & self.enable,
                        self.killed.eq(self.killed + 1))


class SmolEthInvalidator(Module):
    def __init__(self, length, description):
        self.sink = sink = stream.Endpoint(description)
        self.source = source = stream.Endpoint(description)

        self.invalid = Signal()

        length_counter = Signal(max=1500)

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act(
            "IDLE",
            NextValue(length_counter, 1),
            self.sink.connect(self.source),
            If(
                sink.valid & sink.ready,
                NextState("COPY"),
            ),
        )
        fsm.act(
            "COPY",
            self.sink.connect(self.source),
            If(
                self.invalid & (length_counter > length),
                NextState("INVALIDATE"),
            ),
            If(
                self.sink.valid & self.sink.ready,
                If(self.sink.last, NextState("IDLE")),
                NextValue(length_counter, length_counter + 1),
            ),
        )
        fsm.act(
            "INVALIDATE",
            self.source.error.eq(0xFF),
            self.source.last_be.eq(0x01),
            self.source.last.eq(1),
            self.source.valid.eq(1),
            # Discard the incoming bytes
            # With the normal interface this isn't an issue since it's always ready
            self.sink.ready.eq(~(self.sink.last & self.sink.valid)),
            If(self.source.ready, NextState("WAIT_TILL_DONE")),
        )
        fsm.act(
            "WAIT_TILL_DONE",
            self.sink.ready.eq(1),
            If(self.sink.valid & self.sink.last, NextState("IDLE")),
        )


class AlwaysReady(Module):
    """Never backpressures upstream, and never truncates a packet downstream.

    The stream splitter only advances when every consumer has taken the beat, so
    a hardware consumer that stalls throttles the CPU's copy of the traffic. This
    guarantees sink.ready and drops instead.

    The subtlety, learned the hard way: simply ceasing to forward mid-packet
    leaves every downstream FSM waiting for a `last` that never comes, and the
    whole pipeline deadlocks after the first stalled packet. Measured as 288
    beats forwarded out of 115,057 before everything stopped. So when a drop
    starts mid-packet we still emit one final beat with `last` set, which lets
    the depacketizers unwind and reject the short frame on their own length
    checks.
    """

    def __init__(self, description):
        self.sink = sink = stream.Endpoint(description)
        self.source = source = stream.Endpoint(description)
        self.dropped = Signal(32)

        in_packet = Signal()
        dropping = Signal()
        need_last = Signal()

        self.comb += [
            # Upstream is never held up.
            sink.ready.eq(1),
            sink.connect(source, omit={"valid", "ready", "last"}),
            If(need_last,
                # Synthetic terminator so downstream can unwind.
                source.valid.eq(1),
                source.last.eq(1),
            ).Else(
                source.valid.eq(sink.valid & ~dropping),
                source.last.eq(sink.last),
            ),
        ]

        self.sync += [
            # The synthetic terminator retires once downstream takes it.
            If(need_last & source.ready,
                need_last.eq(0),
            ),
            # Sink tracking is NOT under an Elif on need_last. sink.ready is
            # unconditionally 1, so beats keep arriving while the terminator is
            # still going out, and one of them can be the packet's `last`.
            # Skipping the tracking for those cycles left `dropping` set into
            # the following packet, and since source.valid is gated on
            # ~dropping, that entire next packet was silently swallowed -- one
            # drop quietly becoming two.
            If(sink.valid,
                If(sink.last,
                    in_packet.eq(0),
                    dropping.eq(0),
                ).Else(
                    in_packet.eq(1),
                    If(~dropping & ~need_last & ~source.ready,
                        # Downstream just refused a mid-packet beat: stop
                        # forwarding this packet, but terminate it first.
                        dropping.eq(1),
                        need_last.eq(1),
                        self.dropped.eq(self.dropped + 1),
                    ),
                ),
            ),
        ]
        # Ordering note: when a real `last` lands while need_last is pending,
        # need_last already forces valid+last on that same beat, so the
        # terminator is satisfied by the real one and the first If clears it.
        # The remaining edge -- a real `last` arriving while downstream is still
        # stalled -- leaves the terminator pending into the next packet, which
        # downstream then rejects on length. Rare, and it fails loudly rather
        # than silently.


class Bytes32to8(Module):
    """Serialize a 32-bit eth stream into bytes, first wire byte first.

    LiteX packs the first byte of a word in the low 8 bits, so bytes leave in
    data[0:8], [8:16], [16:24], [24:32] order. `last_be` marks which bytes of the
    final beat are real and `last` is asserted on the last of those; without that
    the padding bytes of a short final word would be emitted as payload and every
    length check downstream would be wrong.
    """

    def __init__(self):
        self.sink = sink = stream.Endpoint(eth_phy_description(32))
        self.source = source = stream.Endpoint(eth_phy_description(8))

        idx = Signal(2)
        data = Signal(32)
        last_be = Signal(4)
        last = Signal()
        loaded = Signal()
        final_idx = Signal(2)
        byte = Signal(8)

        self.comb += [
            If(last_be[3], final_idx.eq(3))
            .Elif(last_be[2], final_idx.eq(2))
            .Elif(last_be[1], final_idx.eq(1))
            .Else(final_idx.eq(0)),
        ]

        self.comb += [
            Case(idx, {
                0: byte.eq(data[0:8]),
                1: byte.eq(data[8:16]),
                2: byte.eq(data[16:24]),
                3: byte.eq(data[24:32]),
            }),
            sink.ready.eq(~loaded),
            source.valid.eq(loaded),
            source.data.eq(byte),
            source.last.eq(last & (idx == final_idx)),
            source.last_be.eq(source.last),
            source.error.eq(0),
        ]

        self.sync += [
            If(~loaded,
                If(sink.valid,
                    data.eq(sink.data),
                    last_be.eq(Mux(sink.last, sink.last_be, 0b1111)),
                    last.eq(sink.last),
                    idx.eq(0),
                    loaded.eq(1),
                ),
            ).Elif(source.ready,
                If(source.last | (idx == 3),
                    loaded.eq(0),
                ).Else(
                    idx.eq(idx + 1),
                ),
            ),
        ]
