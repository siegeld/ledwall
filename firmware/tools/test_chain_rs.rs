//! Host-side unit tests for the firmware's pure chain logic (src/chain.rs):
//! discovery parsing at the gateware's fixed offsets, the port map, the
//! head/member frame filters, gray decoding. `./build.sh test-chain-rs`.
//!
//! chain.rs is compiled as-is via #[path]; only its hardware accessors are
//! gated to the card (`target_os = "none"`).
#![allow(dead_code)]
#[path = "../sw_rust/barsign_disp/src/chain.rs"]
mod chain;
use chain::*;

/// A frame laid out the way the Linux kernel sends marquee's discovery: Ethernet,
/// IPv4 IHL 5, UDP to 6990, payload "CH" hop ver 255-hop 0 clock(4) pad.
fn disc(hop: u8, ver: u8, clock: u32) -> Vec<u8> {
    let mut f = vec![0u8; 14 + 20 + 8 + 18];
    f[0..6].copy_from_slice(&[0x02, 0, 0, 0, 0, 0x01]); // head MAC
    f[6..12].copy_from_slice(&[0x02, 0, 0, 0, 0, 0x99]); // server MAC
    f[12] = 0x08; f[13] = 0x00; f[14] = 0x45; f[23] = 17;
    f[26..30].copy_from_slice(&[192, 168, 61, 1]); // server IP
    f[30..34].copy_from_slice(&[192, 168, 61, 11]); // head IP
    f[36..38].copy_from_slice(&DISC_PORT.to_be_bytes());
    f[42] = b'C'; f[43] = b'H'; f[44] = hop; f[45] = ver; f[46] = 255 - hop;
    f[48..52].copy_from_slice(&clock.to_be_bytes());
    f
}

fn udp(dport: u16, proto: u8) -> Vec<u8> {
    let mut f = vec![0u8; 60];
    f[12] = 0x08; f[13] = 0x00; f[14] = 0x45; f[23] = proto;
    f[36..38].copy_from_slice(&dport.to_be_bytes());
    f
}

fn as_member(h: u8) { unsafe { STATE.role = Role::Member; STATE.hop = h; } }
fn as_head() { unsafe { STATE.role = Role::Head; STATE.hop = 0; } }

fn main() {
    // --- discovery parsing --------------------------------------------------
    let d = parse_discovery(&disc(3, 2, 0x1234_5678)).expect("v2 discovery parses");
    assert_eq!(d.hop, 3);
    assert_eq!(d.head_mac, [0x02, 0, 0, 0, 0, 0x01]);
    assert_eq!(d.server_mac, [0x02, 0, 0, 0, 0, 0x99]);
    assert_eq!(d.server_ip, [192, 168, 61, 1]);
    assert_eq!(d.head_ip, [192, 168, 61, 11]);
    assert_eq!(d.clock_ms, Some(0x1234_5678));
    assert_eq!(parse_discovery(&disc(1, 1, 7)).unwrap().clock_ms, None, "v1 carries no clock");
    let mut bad = disc(0, 2, 0); bad[14] = 0x46; // IP options move the payload
    assert!(parse_discovery(&bad).is_none(), "IHL 6 must not parse at fixed offsets");
    let mut bad = disc(0, 2, 0); bad[37] ^= 1;
    assert!(parse_discovery(&bad).is_none(), "wrong port");
    let mut bad = disc(0, 2, 0); bad[43] = b'X';
    assert!(parse_discovery(&bad).is_none(), "wrong magic");
    assert!(parse_discovery(&disc(0, 2, 0)[..47]).is_none(), "short frame");
    // Frames the gateware passes through UNREWRITTEN must not parse either:
    // their hop byte is not this card's position.
    let mut bad = disc(2, 2, 0); bad[46] = 0;
    assert!(parse_discovery(&bad).is_none(), "anti-hop mismatch");
    let mut bad = disc(0xFF, 2, 0); bad[46] = 0;
    assert!(parse_discovery(&bad).is_none(), "hop 255 cannot be incremented");
    let mut bad = disc(1, 2, 0); bad[20] = 0x20;
    assert!(parse_discovery(&bad).is_none(), "IP fragment (MF)");
    let mut bad = disc(1, 2, 0); bad[21] = 1;
    assert!(parse_discovery(&bad).is_none(), "IP fragment (offset)");
    let mut ok = disc(1, 2, 0); ok[20] = 0x40;
    assert!(parse_discovery(&ok).is_some(), "DF is fine");
    // A broadcast / multicast "head" would be adopted as our own MAC/IP, and
    // smoltcp panics on a non-unicast hardware address.
    let mut bad = disc(1, 2, 0); bad[0..6].copy_from_slice(&[0xFF; 6]);
    assert!(parse_discovery(&bad).is_none(), "broadcast head MAC");
    let mut bad = disc(1, 2, 0); bad[0] = 0x01;
    assert!(parse_discovery(&bad).is_none(), "multicast head MAC");
    let mut bad = disc(1, 2, 0); bad[30..34].copy_from_slice(&[255; 4]);
    assert!(parse_discovery(&bad).is_none(), "broadcast head IP");
    let mut bad = disc(1, 2, 0); bad[30] = 239;
    assert!(parse_discovery(&bad).is_none(), "multicast head IP");
    let mut bad = disc(1, 2, 0); bad[6] = 0x01;
    assert!(parse_discovery(&bad).is_none(), "multicast server MAC");
    println!("  discovery parse: offsets, versions, near-misses ok");

    // --- port map ------------------------------------------------------------
    as_head();
    assert_eq!((pixel_port(), http_port(), config_port(), bitstream_port()), (7000, 80, 6900, 6901));
    as_member(5);
    assert_eq!((pixel_port(), http_port(), config_port(), bitstream_port()), (7005, 8005, 20020, 20021));
    as_member(63);
    assert_eq!((pixel_port(), http_port()), (7063, 8063));
    // No member port may collide with any other hop's, or with the head's.
    let mut seen = std::collections::HashSet::new();
    for h in 0..MAX_HOPS {
        if h == 0 { as_head() } else { as_member(h) }
        for p in [pixel_port() as u32 | 0x1_0000, http_port() as u32, config_port() as u32 | 0x1_0000,
                  bitstream_port() as u32 | 0x1_0000] {
            assert!(seen.insert(p), "port {} reused at hop {}", p & 0xffff, h);
        }
    }
    // ...nor with discovery or the head's fixed listeners.
    for h in 1..MAX_HOPS {
        as_member(h);
        for p in [pixel_port(), config_port(), bitstream_port()] {
            assert!(![DISC_PORT, 6454, 67, 68, 69, 6969].contains(&p), "hop {} UDP {} collides", h, p);
        }
        assert!(![80u16, 23].contains(&http_port()));
    }
    println!("  port map: 64 hops, no collisions");

    // --- head filter: must ignore exactly what belongs to members ------------
    as_head();
    for (port, proto, want) in [(7000, 17, false), (7001, 17, true), (7063, 17, true), (7064, 17, false),
                                (80, 6, false), (8001, 6, true), (8063, 6, true), (8064, 6, false),
                                (20004, 17, true), (20255, 17, true), (20003, 17, false), (20256, 17, false),
                                (6900, 17, false), (6901, 17, false), (8001, 17, false), (7001, 6, false)] {
        assert_eq!(for_a_member(&udp(port, proto)), want, "head: proto {} port {}", proto, port);
    }
    println!("  head filter: member ports ignored, own ports kept");

    // --- member filter: ONLY its own ports -----------------------------------
    as_member(2);
    for (port, proto, want) in [(7002, 17, true), (8002, 6, true), (20008, 17, true), (20009, 17, true),
                                (7000, 17, false), (7003, 17, false), (80, 6, false), (8001, 6, false),
                                (67, 17, false), (6990, 17, false), (8002, 17, false), (7002, 6, false)] {
        assert_eq!(member_accepts(&udp(port, proto)), want, "member 2: proto {} port {}", proto, port);
    }
    let mut arp = vec![0u8; 60]; arp[12] = 0x08; arp[13] = 0x06;
    assert!(!member_accepts(&arp), "a member never handles ARP");
    let mut icmp = udp(0, 1); icmp[23] = 1;
    assert!(!member_accepts(&icmp), "a member never handles ICMP");
    println!("  member filter: own ports only, no ARP/ICMP");

    // --- source learning -----------------------------------------------------
    let f = disc(1, 2, 0);
    assert_eq!(source(&f), Some(([192, 168, 61, 1], [0x02, 0, 0, 0, 0, 0x99])));
    assert_eq!(source(&arp), None);

    // --- gray decode (the gateware's counters) --------------------------------
    for v in [0u32, 1, 2, 3, 0x7fff, 0xffff, 0x1234, 0xa5a5] {
        let g = v ^ (v >> 1);
        assert_eq!(ungray(g), v);
    }
    println!("  gray decode ok");
    println!("test-chain-rs: PASS");
}
