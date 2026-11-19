//! Card chains: one IP address, many cards, daisy-chained through port 1.
//!
//! The gateware (`gateware/chain.py`) does the moving: everything arriving on
//! port 0 is repeated out of port 1, and members' unicast replies are merged
//! back out of port 0. Broadcasts from members are DROPPED on the way up, so a
//! member's DHCP never leaves the chain -- a card behind another card cannot
//! get an IP address, by construction.
//!
//! This module decides what a card is. Every card runs the same firmware:
//!
//! * **head** (hop 0) -- port 0 faces the switch. An ordinary card: DHCP, its
//!   own config, API on port 80, pixels on 7000. A lone card is the head of a
//!   chain of one, so nothing changes for an unchained card.
//! * **member** (hop h >= 1) -- a SLAVE of the head. No IP of its own: it
//!   answers as the head (same MAC, same IP) on ports that belong to hop h
//!   only, and ignores everything else, because it sees ALL of the head's
//!   traffic and must not reset the head's connections.
//!
//! A card learns its hop from marquee's DISCOVERY frame: UDP to the head on
//! port 6990, payload "CH" hop. Each card's gateware adds one to the hop on
//! the way through, so the frame a member receives already names its own
//! position. The same frame names the head (destination MAC and IP) and the
//! server (source), which is everything a member needs to fetch its config.
//!
//! Ports, hop 0 = what every card has always used:
//!
//! | what                   | hop 0 | hop h >= 1      |
//! |------------------------|-------|-----------------|
//! | pixels, UDP            | 7000  | 7000 + h        |
//! | web UI + API, TCP      | 80    | 8000 + h        |
//! | config TFTP client     | 6900  | 20000 + 4h      |
//! | bitstream TFTP client  | 6901  | 20000 + 4h + 1  |

pub const DISC_PORT: u16 = 6990;
/// The longest chain the port map allows; `pixel_filter_span` in the gateware
/// kills this many pixel ports on every card's CPU branch.
pub const MAX_HOPS: u8 = 64;

const PIXEL_BASE: u16 = 7000;
const HTTP_BASE: u16 = 8000;
const CLIENT_BASE: u16 = 20000;

#[derive(Clone, Copy, PartialEq)]
pub enum Role {
    /// No discovery frame seen yet: behave as a lone card.
    Unknown,
    Head,
    Member,
}

pub struct State {
    pub role: Role,
    pub hop: u8,
    pub head_mac: [u8; 6],
    pub head_ip: [u8; 4],
    pub server_mac: [u8; 6],
    pub server_ip: [u8; 4],
    pub discoveries: u32,
    pub last_discovery_ms: i64,
}

pub static mut STATE: State = State {
    role: Role::Unknown,
    hop: 0,
    head_mac: [0; 6],
    head_ip: [0; 4],
    server_mac: [0; 6],
    server_ip: [0; 4],
    discoveries: 0,
    last_discovery_ms: 0,
};

#[inline]
pub fn hop() -> u8 {
    unsafe { if STATE.role == Role::Member { STATE.hop } else { 0 } }
}

#[inline]
pub fn is_member() -> bool {
    unsafe { STATE.role == Role::Member }
}

pub fn pixel_port() -> u16 {
    PIXEL_BASE + hop() as u16
}

pub fn http_port() -> u16 {
    match hop() {
        0 => 80,
        h => HTTP_BASE + h as u16,
    }
}

pub fn config_port() -> u16 {
    match hop() {
        0 => 6900,
        h => CLIENT_BASE + 4 * h as u16,
    }
}

pub fn bitstream_port() -> u16 {
    match hop() {
        0 => 6901,
        h => CLIENT_BASE + 4 * h as u16 + 1,
    }
}

pub fn role_str() -> &'static str {
    unsafe {
        match STATE.role {
            Role::Unknown => "none",
            Role::Head => "head",
            Role::Member => "member",
        }
    }
}

pub struct Discovery {
    pub hop: u8,
    /// marquee's millisecond clock (payload bytes 6..10, big-endian), the
    /// shared time base for programs across the cards of a wall. None from a
    /// sender that does not carry it.
    pub clock_ms: Option<u32>,
    pub head_mac: [u8; 6],
    pub server_mac: [u8; 6],
    pub server_ip: [u8; 4],
    pub head_ip: [u8; 4],
}

/// Is this marquee's discovery frame? Layout is fixed by the gateware's
/// forwarder, which rewrites byte 44 at exactly this offset (IHL must be 5).
pub fn parse_discovery(f: &[u8]) -> Option<Discovery> {
    if f.len() < 48 || f[12] != 0x08 || f[13] != 0x00 || f[14] != 0x45 || f[23] != 17 {
        return None;
    }
    if u16::from_be_bytes([f[36], f[37]]) != DISC_PORT || f[42] != b'C' || f[43] != b'H' {
        return None;
    }
    // The same well-formedness test the gateware applies before it rewrites
    // a discovery: anti-hop == 255 - hop, a hop it can still increment, and
    // not an IP fragment (MF clear, offset 0; DF allowed). One it refused went
    // past every card unchanged, so its hop byte is not our position -- and a
    // hop of 0 or >= 64 read from it would reboot a member.
    if f[46] != 255 - f[44] || f[44] == 0xFF || (f[20] & 0x3F) != 0 || f[21] != 0 {
        return None;
    }
    // The head is named by the frame's DESTINATION, which a member adopts as
    // its own MAC and IP. A broadcast or multicast one is not a head: handed
    // to smoltcp as our own address it panics (non-unicast hardware address),
    // and one such packet from anywhere on the LAN rebooted every lone card.
    let ip = [f[30], f[31], f[32], f[33]];
    if f[0] & 1 != 0 || ip == [255; 4] || ip[0] >= 224 || ip[0] == 0 || ip[0] == 127 {
        return None;
    }
    // ...and the server is who the member fetches its config from.
    if f[6] & 1 != 0 || f[26] >= 224 || f[26] == 0 {
        return None;
    }
    let mut d = Discovery {
        hop: f[44],
        // Version 2 frames carry the clock at payload bytes 6..10.
        clock_ms: if f[45] >= 2 && f.len() >= 52 {
            Some(u32::from_be_bytes([f[48], f[49], f[50], f[51]]))
        } else {
            None
        },
        head_mac: [0; 6],
        server_mac: [0; 6],
        server_ip: [0; 4],
        head_ip: [0; 4],
    };
    d.head_mac.copy_from_slice(&f[0..6]);
    d.server_mac.copy_from_slice(&f[6..12]);
    d.server_ip.copy_from_slice(&f[26..30]);
    d.head_ip.copy_from_slice(&f[30..34]);
    Some(d)
}

/// Destination (protocol, port) of an IPv4 TCP/UDP frame.
fn dst(f: &[u8]) -> Option<(u8, u16)> {
    if f.len() < 38 || f[12] != 0x08 || f[13] != 0x00 {
        return None;
    }
    let ihl = (f[14] & 0x0f) as usize * 4;
    let l4 = 14 + ihl;
    if ihl < 20 || f.len() < l4 + 4 {
        return None;
    }
    let proto = f[23];
    if proto != 6 && proto != 17 {
        return None;
    }
    Some((proto, u16::from_be_bytes([f[l4 + 2], f[l4 + 3]])))
}

/// Head side: does this frame belong to a member? Then the head must neither
/// process it nor answer it -- smoltcp would send a RST or an ICMP port
/// unreachable for a port it does not own, and that would kill the member's
/// connection from the far end.
pub fn for_a_member(f: &[u8]) -> bool {
    let top = MAX_HOPS as u16 - 1;
    match dst(f) {
        Some((6, p)) => (HTTP_BASE + 1..=HTTP_BASE + top).contains(&p),
        Some((17, p)) => {
            (PIXEL_BASE + 1..=PIXEL_BASE + top).contains(&p)
                || (CLIENT_BASE + 4..CLIENT_BASE + 4 * MAX_HOPS as u16).contains(&p)
        }
        _ => false,
    }
}

/// Member side: the only frames a member may act on are the ones addressed to
/// its own ports. Everything else it sees is the head's business. No ARP, no
/// ICMP: the head answers those for the whole chain.
pub fn member_accepts(f: &[u8]) -> bool {
    match dst(f) {
        Some((6, p)) => p == http_port(),
        Some((17, p)) => p == pixel_port() || p == config_port() || p == bitstream_port(),
        _ => false,
    }
}

/// Source IP and MAC of an IPv4 frame, for the member's neighbour cache.
pub fn source(f: &[u8]) -> Option<([u8; 4], [u8; 6])> {
    if f.len() < 34 || f[12] != 0x08 || f[13] != 0x00 {
        return None;
    }
    let mut ip = [0u8; 4];
    let mut mac = [0u8; 6];
    ip.copy_from_slice(&f[26..30]);
    mac.copy_from_slice(&f[6..12]);
    Some((ip, mac))
}

/// The gateware counters are gray code so they can cross clock domains.
pub fn ungray(mut g: u32) -> u32 {
    let mut b = g;
    while g != 0 {
        g >>= 1;
        b ^= g;
    }
    b
}

// Hardware access only on the card. Everything above is pure, which is what
// lets tools/test_chain_rs.rs compile this file on the host and test it.
// Whether the gateware counts chain frames at all (build.rs says why). When it
// does not, the counter registers read 0 and are reported as absent.
#[cfg(target_os = "none")]
include!(concat!(env!("OUT_DIR"), "/chain_counters.rs"));

#[cfg(target_os = "none")]
pub struct Counters {
    pub port1_link: bool,
    pub port1_speed: u8,
    pub fwd_frames: u32,
    pub fwd_rewrites: u32,
    pub fwd_dropped: u32,
    pub up_frames: u32,
    pub up_dropped: u32,
}

#[cfg(target_os = "none")]
pub fn counters() -> Counters {
    let p = unsafe { litex_pac::Peripherals::steal() };
    let st = p.ethphy1.rx_inband_status().read().bits();
    Counters {
        port1_link: st & 1 != 0,
        port1_speed: ((st >> 1) & 3) as u8,
        fwd_frames: ungray(p.chain.fwd_frames().read().bits()),
        fwd_rewrites: ungray(p.chain.fwd_rewrites().read().bits()),
        fwd_dropped: ungray(p.chain.fwd_dropped().read().bits()),
        up_frames: ungray(p.chain.up_frames().read().bits()),
        up_dropped: ungray(p.chain.up_dropped().read().bits()),
    }
}
