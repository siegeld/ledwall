# A dedicated link for the panel: the sender's second NIC straight to the card

**Why.** The chunk loss on a shared network is packet TRAINS: the card's stream
crosses a busy uplink (in the case measured: gigabit, ~193 Mbit/s of other
traffic, one best-effort queue, 44-103 tail drops/s), queues behind bursts from
other hosts and arrives 40-60 packets back to back. Gateware 1.37.0 measures it
(`arrive_run_max` median 28 in FIFO-full seconds vs 3 otherwise); the host emits
runs of 2. Removing the shared uplink removes the trains at the source, for any
number of panels. It is a VLAN's job done with a cable.

## Topology

    Sender NIC 1  ---- house network (marquee UI/API, internet)
    Sender NIC 2  ---- [small dumb switch, panels only, or a direct cable] ---- card(s)

Nothing else goes on the panel segment. No uplink from that switch to anything.

## Addressing: one private /24 per sender

Give each sender its own panel bus: a /24 that is not routed anywhere and is not
a DHCP scope on your network. The examples below use `192.168.61.0/24`; pick your
own and set it in `.env` (see below). Convention used here: the sender is `.1`,
card k is `.1k`, and DHCP keeps `.100-.120` for a card it has not been told about.

Do not reuse the house subnet on NIC 2. The card's BIOS takes its boot server
from **DHCP option 66** (since colorlight 2.3.0, `patches/litex-bios-dhcp.patch`;
the compiled-in `REMOTEIP` is only the fallback when the option is absent), so
nothing about the bus address is baked into the bitstream; and two interfaces
carrying the same /24 on one host is a routing ambiguity, not a design.

| what | address | note |
|---|---|---|
| Sender NIC 2 (NetworkManager profile `panel-link`) | `192.168.61.1/24` | static, `never-default`, IPv6 off; marquee's TFTP binds 0.0.0.0:6969 (host network) |
| Card | `192.168.61.11` | dnsmasq reservation by the card's MAC; set the card's address in marquee to match |
| DHCP option 66 | `192.168.61.1` | the BIOS's `boot.bin` fetch and the firmware's `<mac>.yml` fetch |
| gateway / DNS | none | options 3 and 6 are served empty on purpose -- nothing routes to or from this segment |

Nothing else routes to the panel network, from anywhere. That is the point.

Create the NIC profile once (leave speed and duplex alone, see the traps below):

    nmcli c add type ethernet ifname <NIC2> con-name panel-link \
        ipv4.method manual ipv4.addresses 192.168.61.1/24 ipv4.never-default yes \
        ipv6.method disabled connection.autoconnect yes

## DHCP: dnsmasq, bound to NIC 2 only

marquee's compose file carries an opt-in dnsmasq under the `panel-link` profile.
Set these in `.env` (all have the documentation-range defaults shown):

    PANEL_IF=<NIC2>
    PANEL_SENDER_IP=192.168.61.1
    PANEL_CARD_IP=192.168.61.11
    PANEL_POOL_START=192.168.61.100
    PANEL_POOL_END=192.168.61.120
    PANEL_NETMASK=255.255.255.0

then `docker compose --profile panel-link up -d dnsmasq`. It binds the panel NIC
only (`--bind-interfaces`), leases exactly the reserved card plus the pool, and
sends option 66 = the sender. The compose service bypasses the image's wrapper
(`entrypoint: ["dnsmasq"]`), which otherwise ignores the command line and runs a
DNS-only config -- check `ss -ulnp | grep :67` shows dnsmasq listening on the
panel NIC. To reserve a second card add another `--dhcp-host=<mac>,<ip>,<name>`.

## marquee moves to the sender first

Once the card is on NIC 2's private segment, nothing on the house network can
reach it -- including a marquee left running on the old sender. The sender, API,
TFTP and DHCP all have to run ON the new sender:

    sudo mkdir -p /srv/docker && sudo chown $USER /srv/docker
    git clone <your marquee remote> /srv/docker/marquee
    # bring the state over: the DB (cards, walls, shows), firmware/bitstream
    # store, media. From the old sender:
    rsync -a /srv/docker/marquee/data/ <sender>:/srv/docker/marquee/data/
    rsync -a /srv/docker/marquee/.env <sender>:/srv/docker/marquee/.env   # if present
    # then set the PANEL_* values above in .env
    make up                       # api :8410, frontend :8411, player, tftp :6969

Stop the player on the old sender before the new one starts (`make down` there, or
at least `docker compose stop player`): ONE writer per panel, and the card
would otherwise see two senders the moment the cable is swapped.

The firmware/bitstream store carries the pinned images the card netboots, so the
rsync is not optional.

## Bring-up, one step at a time

1. Cable NIC 2 -> panel switch (or direct) -> card. Power the card OFF.
2. On the sender: `nmcli c up panel-link` if the profile is not active.
3. `make up` (api, player, tftp) then `docker compose --profile panel-link up -d dnsmasq`.
   `docker logs marquee-dnsmasq-1` should show it listening on the panel NIC.
4. Power the card ON. Watch: `ip neigh | grep <card ip>` -- the BIOS answers
   with a placeholder MAC while netbooting, the firmware with its own.
   The player's log shows boot.bin and the `<mac>.yml` transfer.
5. `curl -s http://<card ip>/api/status` -- `link_inband` 13 (gigabit),
   `isr_driven` true. marquee re-enables DMA and streams.
6. **The proof.** Clear peaks, wait 60 s, read: `arrive_run_max` should be **2**
   (our own pairs) and `c_dropped` / `frames_held` **0**. If `arrive_run_max`
   is ever above 2 on a dedicated link, the switch in step 1 is doing something.

## Three panels

With the trains gone the budget is pacing only: 202 chunks x `packet_delay`
must fit 50 ms, so `packet_delay <= 0.00024` (measured 48.5 ms at 240 us).
Set it on the card record before fitting the third module. Redundancy
(`MARQUEE_REDUNDANT_COPIES`) stays at 1 -- it was a two-panel stopgap for a
network this link no longer has.

## Four panels

270 chunks a frame -- over the narrow header's 255, so the frame goes out with
the wide `'B','W'` header at 486 pixels a chunk (ARCH.md §4). `packet_delay`
**0.00015** puts a frame on the wire in 40.5 ms. Measured on the four-output
E320 over this link, 3 minutes, a full 256x512 stream: 20.0 fps, 5,400
chunks/s, 0 underruns, 0 frames held, refresh 66.7 Hz, and no IP
fragmentation on the sender (`Ip: FragOKs` in `/proc/net/snmp` did not move).
`arrive_run_max` reached 7 and the card's burst FIFO filled once, dropping 2
chunks (`c_dropped`) with no frame held -- the margin at 5,400 packets/s is
thinner than at 2,700, and host-side burstiness is what spends it.

Check `FragOKs` on any new frame size. A datagram over 1500 bytes is split by
the kernel, the card reads only the first piece, and the picture loses pixels
at fixed positions with every card counter clean.

## Rollback

Unplug NIC 2, plug the card back into the house network, stop dnsmasq. The card gets
its old DHCP reservation again; nothing on it changed.

## Proven 2026-10-24, and two traps met on the way

`tools/linkproof.py`, run ON the sender, 120 s quiet: c_dropped 0, frames_held 0,
20.01 fps, underruns 0, arrive_run_max 3 (our own SEND_BATCH pairs plus one
merged by a sleep overshoot), c_fifo_max 949 of 3072. The shared switch gave 60.

1. **A streaming player kills the netboot.** The card's BIOS has no hardware
   pixel path, so every streamed packet lands in its two CPU RX slots; the 473
   KB TFTP transfer crawled at 45 kbps and the BIOS gave up at block 228. It
   looked like 1% link loss, and ping against the BIOS "confirmed" it. marquee
   0.42+ holds the stream while the card's firmware is not answering; on an
   older marquee, disable the card before a power cycle and re-enable it after.
2. **Never force the panel NIC's speed.** The gateware's RGMII is fixed at
   gigabit: a 100 Mb link passes nothing, and the card's PHY then stayed at 100
   Mb until the next power cycle. Leave autoneg alone; `ethtool` should read
   1000 Mb/s full and `link_inband` 13.
