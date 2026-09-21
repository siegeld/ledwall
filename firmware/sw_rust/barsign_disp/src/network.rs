//! Interrupt-driven network stack.
//!
//! All network state lives in statics so the ISR can access it.
//! The ISR calls network_handler() which does all socket processing.
//! Main loop does ZERO network code - only display/animation.

use core::mem::MaybeUninit;
use crate::ethernet::Eth;
use crate::bitmap_udp::{BitmapReceiver, BitmapStats};
use crate::hub75::Hub75;
use crate::tftp_config::TftpConfigLoader;
use crate::layout::LayoutConfig;
use crate::http::{HttpRequest, HttpResponse};

use smoltcp::iface::{Interface, InterfaceBuilder, NeighborCache, Routes, SocketHandle, SocketStorage};
use smoltcp::socket::{
    Dhcpv4Event, Dhcpv4Socket, TcpSocket, TcpSocketBuffer, UdpPacketMetadata, UdpSocket,
    UdpSocketBuffer,
};
use smoltcp::time::Instant;
use smoltcp::wire::{EthernetAddress, IpAddress, IpCidr, Ipv4Address, Ipv4Cidr};

use litex_pac as pac;

// ============================================================================
// Static Storage
// ============================================================================

// Neighbor cache entries for ARP
static mut NEIGHBOR_CACHE_ENTRIES: [Option<(smoltcp::wire::IpAddress, smoltcp::iface::Neighbor)>; 8] = [None; 8];

// IP address storage - initialized at runtime
static mut IP_ADDRS: MaybeUninit<[IpCidr; 1]> = MaybeUninit::uninit();
/// Our IPv4, cached for the ISR's ARP filter. Zero until DHCP (or the static
/// fallback) assigns one, and the filter is inert until then so we never drop
/// the ARP exchange that brings the interface up.
static mut OUR_IPV4: [u8; 4] = [0, 0, 0, 0];

// Routes storage
static mut ROUTES_STORAGE: [Option<(IpCidr, smoltcp::iface::Route)>; 1] = [None; 1];

// Socket set storage (7 sockets)
static mut SOCKETS_ENTRIES: [SocketStorage<'static>; 7] = [SocketStorage::EMPTY; 7];

// TCP server (telnet) buffers
static mut TCP_SERVER_RX_DATA: [u8; 256] = [0; 256];
static mut TCP_SERVER_TX_DATA: [u8; 256] = [0; 256];

// UDP server (artnet) buffers
static mut UDP_SERVER_RX_DATA: [u8; 2048] = [0; 2048];
static mut UDP_SERVER_TX_DATA: [u8; 2048] = [0; 2048];
static mut UDP_SERVER_RX_METADATA: [UdpPacketMetadata; 32] = [UdpPacketMetadata::EMPTY; 32];
static mut UDP_SERVER_TX_METADATA: [UdpPacketMetadata; 32] = [UdpPacketMetadata::EMPTY; 32];

// Bitmap UDP buffers
static mut BITMAP_UDP_RX_DATA: [u8; 65536] = [0; 65536];
static mut BITMAP_UDP_TX_DATA: [u8; 64] = [0; 64];
static mut BITMAP_UDP_RX_META: [UdpPacketMetadata; 48] = [UdpPacketMetadata::EMPTY; 48];
static mut BITMAP_UDP_TX_META: [UdpPacketMetadata; 1] = [UdpPacketMetadata::EMPTY; 1];

// TFTP UDP buffers
static mut TFTP_UDP_RX_DATA: [u8; 1024] = [0; 1024];
static mut TFTP_UDP_TX_DATA: [u8; 128] = [0; 128];
static mut TFTP_UDP_RX_META: [UdpPacketMetadata; 4] = [UdpPacketMetadata::EMPTY; 4];
static mut TFTP_UDP_TX_META: [UdpPacketMetadata; 4] = [UdpPacketMetadata::EMPTY; 4];

// HTTP TCP socket A buffers
static mut HTTP_TCP_RX_A: [u8; 512] = [0; 512];
static mut HTTP_TCP_TX_A: [u8; 2048] = [0; 2048];

// HTTP TCP socket B buffers
static mut HTTP_TCP_RX_B: [u8; 512] = [0; 512];
static mut HTTP_TCP_TX_B: [u8; 2048] = [0; 2048];

// The Interface itself (using UnsafeCell for interior mutability from ISR)
static mut IFACE: MaybeUninit<Interface<'static, Eth>> = MaybeUninit::uninit();
static mut IFACE_INITIALIZED: bool = false;

// Socket handles
static mut TCP_SERVER_HANDLE: MaybeUninit<SocketHandle> = MaybeUninit::uninit();
static mut UDP_SERVER_HANDLE: MaybeUninit<SocketHandle> = MaybeUninit::uninit();
static mut BITMAP_UDP_HANDLE: MaybeUninit<SocketHandle> = MaybeUninit::uninit();
static mut TFTP_UDP_HANDLE: MaybeUninit<SocketHandle> = MaybeUninit::uninit();
static mut DHCP_HANDLE: MaybeUninit<SocketHandle> = MaybeUninit::uninit();
static mut HTTP_HANDLE_A: MaybeUninit<SocketHandle> = MaybeUninit::uninit();
static mut HTTP_HANDLE_B: MaybeUninit<SocketHandle> = MaybeUninit::uninit();

// Bitmap receiver state
static mut BITMAP_RX: MaybeUninit<BitmapReceiver> = MaybeUninit::uninit();

// TFTP config loader state
static mut TFTP_LOADER: MaybeUninit<TftpConfigLoader> = MaybeUninit::uninit();

// HTTP server state
static mut HTTP_REQUESTS: [MaybeUninit<HttpRequest>; 2] = [MaybeUninit::uninit(), MaybeUninit::uninit()];
static mut HTTP_RESPONSES: [MaybeUninit<HttpResponse>; 2] = [MaybeUninit::uninit(), MaybeUninit::uninit()];
static mut HTTP_RESPONSE_SENT: [usize; 2] = [0; 2];

/// Values pushed by marquee for `mode: program`. See values.rs -- this is the
/// data plane that replaces the pixel stream, not an addition to it.
pub static mut VALUES: crate::values::ValueTable = crate::values::ValueTable::new();

/// Program mode: the panel composes its own pixels and no frame is ever drawn
/// from the wire. Implies hold -- a stream arriving while a program runs must
/// not reach the framebuffer, or the two fight for it at 10+ fps.
pub static mut PROGRAM_ACTIVE: bool = false;
static mut PROGRAM_FRAME: u32 = 0;
/// Which generated program is running. None means the first one -- there is
/// always at least one, because panelc emits the registry from the YAML.
static mut PROGRAM_FN: Option<&'static crate::program::Program> = None;
/// Rows this program redraws every frame; everything else is written only when
/// it can have changed. A framebuffer store costs ~190 cycles on this core (no
/// D-cache, no write buffer), so a full 128x128 redraw is ~3.1M cycles and caps
/// near 13 fps whatever it draws -- measured with `blank`. Not writing a word
/// is the only real speedup available.
static mut PROGRAM_DYNAMIC: Option<(usize, usize)> = None;
/// Full redraws still owed. TWO, because the framebuffer is double-buffered:
/// static content must reach BOTH buffers before partial frames are safe.
static mut PROGRAM_FULL_LEFT: u8 = 2;
/// VALUES.writes at the last full redraw.
static mut PROGRAM_SEEN_WRITES: u32 = 0;
static mut PROGRAM_NAME: &str = "";
static mut PROGRAM_NEXT_MS: i64 = 0;
/// 30 fps. The program is a pure per-pixel function, so this is the only place
/// the rate is decided.
const PROGRAM_PERIOD_MS: i64 = 33;
static mut HTTP_CLOSE_AT: [i64; 2] = [0; 2];
static mut HTTP_CONNECTED_AT: [i64; 2] = [0; 2];

// Telnet state
static mut TELNET_ACTIVE: bool = false;
static mut IAC_STATE: u8 = 0;

// Time tracking
static mut TIME_MS: i64 = 0;
static mut LAST_BITMAP_PACKET_MS: i64 = 0;
/// Last ISR that serviced the TCP sockets -- see SOCKET_POLL_MS in network_handler.
static mut LAST_SOCKET_POLL_MS: i64 = 0;

// Debug counters for diagnosing packet routing
static mut DBG_FAST_PATH: u32 = 0;      // Packets via is_bitmap_udp() fast path
static mut DBG_SLOW_PATH: u32 = 0;      // Packets via smoltcp slow path
static mut DBG_ISR_MAX_BATCH: u32 = 0;  // Max packets processed in single ISR
static mut STATS_PUBLISH_TICK: u32 = 0; // rate-limits the per-packet stats publish

// Capture first slow-path packet header for debugging
static mut DBG_SLOW_PKT: [u8; 64] = [0; 64];  // First 64 bytes of slow-path packet
static mut DBG_SLOW_PKT_LEN: usize = 0;       // Actual length captured
static mut DBG_SLOW_PKT_CAPTURED: bool = false;
static mut DBG_MULTICAST_DROPPED: u32 = 0;    // Multicast packets dropped

// Slow path traffic breakdown
static mut DBG_SLOW_ARP: u32 = 0;
static mut DBG_ARP_DROPPED: u32 = 0;
static mut DBG_SLOW_TCP: u32 = 0;
static mut DBG_SLOW_UDP: u32 = 0;
static mut DBG_SLOW_OTHER: u32 = 0;

// ============================================================================
// Timer-based accurate timing
// ============================================================================
// Timer0 configured as free-running 1-second counter.
// We track seconds from ev_pending ticks, and read countdown value for sub-second.

/// Seconds counter - incremented each time timer wraps (every 1 second)
static mut TIMER_SECONDS: i64 = 0;

/// System clock. MUST match gateware sys_clk_freq in colorlight.py -- every
/// millisecond in this firmware is derived from it, so a mismatch silently
/// rescales all timing (stale-frame deadlines, HTTP timeouts, frame intervals).
pub const SYS_CLK_HZ: u32 = 40_000_000;

/// Timer reload value (1 second)
const TIMER_RELOAD: u32 = SYS_CLK_HZ;

/// Cycles per millisecond
const CYCLES_PER_MS: u32 = SYS_CLK_HZ / 1000;

/// Update timer seconds counter and compute current time in ms.
/// Called from ISR to ensure second boundaries aren't missed.
#[inline]
fn update_time_from_timer() {
    unsafe {
        let t = &*pac::Timer0::ptr();
        // Count seconds from ev_pending (1 second period)
        while t.ev_pending().read().bits() != 0 {
            t.ev_pending().write(|w| w.bits(1)); // clear
            TIMER_SECONDS += 1;
        }
        // Update TIME_MS from seconds + current countdown position
        t.update_value().write(|w| w.bits(1)); // latch current value
        let countdown = t.value().read().bits();
        let elapsed_in_period = (TIMER_RELOAD - 1 - countdown) / CYCLES_PER_MS;
        TIME_MS = TIMER_SECONDS * 1000 + elapsed_in_period as i64;
    }
}

/// Get current time in milliseconds.
#[inline]
/// Current time in milliseconds, read from the TIMER rather than from whatever
/// the ISR last left behind.
///
/// TIME_MS used to be advanced only by update_time_from_timer() inside the
/// receive ISR. That is fine while every pixel packet interrupts the CPU, and it
/// is fatal the moment they stop: with the gateware filter on (RB-5486) the ISR
/// fires a handful of times a second, so TIME_MS freezes, `timer_tick` in the
/// main loop is almost never true, bitmap_tick() almost never runs, and frames
/// the DMA has already written whole sit unpresented. Measured before the fix:
/// 30 frames sent, 1 completed, 13 stale and 15 dropped, while the arrival
/// bitmap showed all 34 chunks present.
///
/// Masked because update_time_from_timer() clears the timer's pending-event bit
/// in a loop; letting the ISR run the same code concurrently would lose or
/// double-count a second. Costs a CSR write and a read per call.
pub fn get_time_ms() -> i64 {
    unsafe {
        without_interrupts(|| {
            update_time_from_timer();
            TIME_MS
        })
    }
}

// DHCP configuration storage
static mut DHCP_CONFIGURED: bool = false;
static mut BOOT_SERVER: Option<([u8; 4], crate::menu::BootServerSource)> = None;
static mut TFTP_STARTED: bool = false;

// Pointers to main-loop owned resources (set during init)
static mut HUB75_PTR: *mut Hub75 = core::ptr::null_mut();
static mut LAYOUT_PTR: *mut LayoutConfig = core::ptr::null_mut();
static mut ANIMATION_PTR: *mut crate::menu::Animation = core::ptr::null_mut();
static mut BITMAP_STATS_PTR: *mut BitmapStats = core::ptr::null_mut();
static mut MENU_RUNNER_PTR: *mut u8 = core::ptr::null_mut(); // Actually *mut menu::Runner but we avoid generics
static mut MAC_BYTES: [u8; 6] = [0; 6];

// ============================================================================
// Initialization
// ============================================================================

/// Initialize the network stack with static storage.
/// Must be called exactly once during startup, before enabling interrupts.
///
/// Returns the SocketHandle for the bitmap UDP socket (for binding).
pub fn init(
    ethmac: pac::Ethmac,
    ethmem: pac::Ethmem,
    mac_bytes: [u8; 6],
    hub75: *mut Hub75,
    layout: *mut LayoutConfig,
    animation: *mut crate::menu::Animation,
    bitmap_stats: *mut BitmapStats,
) {
    unsafe {
        // Store pointers to main-loop resources
        HUB75_PTR = hub75;
        LAYOUT_PTR = layout;
        ANIMATION_PTR = animation;
        BITMAP_STATS_PTR = bitmap_stats;
        MAC_BYTES = mac_bytes;

        // Initialize IP addresses array
        IP_ADDRS.write([IpCidr::new(IpAddress::Ipv4(Ipv4Address::UNSPECIFIED), 0)]);

        // Create Eth device
        let device = Eth::new(ethmac, ethmem);

        // Create neighbor cache
        let neighbor_cache = NeighborCache::new(&mut NEIGHBOR_CACHE_ENTRIES[..]);

        // Create routes
        let routes = Routes::new(&mut ROUTES_STORAGE[..]);

        // Build the interface
        let iface = InterfaceBuilder::new(device, &mut SOCKETS_ENTRIES[..])
            .hardware_addr(EthernetAddress::from_bytes(&mac_bytes).into())
            .neighbor_cache(neighbor_cache)
            .ip_addrs(&mut IP_ADDRS.assume_init_mut()[..])
            .routes(routes)
            .finalize();

        IFACE.write(iface);
        IFACE_INITIALIZED = true;

        let iface = IFACE.assume_init_mut();

        // Create and add TCP server socket (telnet, port 23)
        let tcp_rx_buffer = TcpSocketBuffer::new(&mut TCP_SERVER_RX_DATA[..]);
        let tcp_tx_buffer = TcpSocketBuffer::new(&mut TCP_SERVER_TX_DATA[..]);
        let tcp_server_socket = TcpSocket::new(tcp_rx_buffer, tcp_tx_buffer);
        TCP_SERVER_HANDLE.write(iface.add_socket(tcp_server_socket));

        // Create and add UDP server socket (artnet, port 6454)
        let udp_rx_buffer = UdpSocketBuffer::new(&mut UDP_SERVER_RX_METADATA[..], &mut UDP_SERVER_RX_DATA[..]);
        let udp_tx_buffer = UdpSocketBuffer::new(&mut UDP_SERVER_TX_METADATA[..], &mut UDP_SERVER_TX_DATA[..]);
        let udp_server_socket = UdpSocket::new(udp_rx_buffer, udp_tx_buffer);
        UDP_SERVER_HANDLE.write(iface.add_socket(udp_server_socket));

        // Create and add bitmap UDP socket (port 7000)
        let bitmap_rx = UdpSocketBuffer::new(&mut BITMAP_UDP_RX_META[..], &mut BITMAP_UDP_RX_DATA[..]);
        let bitmap_tx = UdpSocketBuffer::new(&mut BITMAP_UDP_TX_META[..], &mut BITMAP_UDP_TX_DATA[..]);
        let bitmap_udp_socket = UdpSocket::new(bitmap_rx, bitmap_tx);
        BITMAP_UDP_HANDLE.write(iface.add_socket(bitmap_udp_socket));

        // Create and add TFTP UDP socket
        let tftp_rx = UdpSocketBuffer::new(&mut TFTP_UDP_RX_META[..], &mut TFTP_UDP_RX_DATA[..]);
        let tftp_tx = UdpSocketBuffer::new(&mut TFTP_UDP_TX_META[..], &mut TFTP_UDP_TX_DATA[..]);
        let tftp_udp_socket = UdpSocket::new(tftp_rx, tftp_tx);
        TFTP_UDP_HANDLE.write(iface.add_socket(tftp_udp_socket));

        // Create and add DHCP socket
        let dhcp_socket = Dhcpv4Socket::new();
        DHCP_HANDLE.write(iface.add_socket(dhcp_socket));

        // Create and add HTTP TCP sockets
        let http_rx_a = TcpSocketBuffer::new(&mut HTTP_TCP_RX_A[..]);
        let http_tx_a = TcpSocketBuffer::new(&mut HTTP_TCP_TX_A[..]);
        let http_tcp_socket_a = TcpSocket::new(http_rx_a, http_tx_a);
        HTTP_HANDLE_A.write(iface.add_socket(http_tcp_socket_a));

        let http_rx_b = TcpSocketBuffer::new(&mut HTTP_TCP_RX_B[..]);
        let http_tx_b = TcpSocketBuffer::new(&mut HTTP_TCP_TX_B[..]);
        let http_tcp_socket_b = TcpSocket::new(http_rx_b, http_tx_b);
        HTTP_HANDLE_B.write(iface.add_socket(http_tcp_socket_b));

        // Bind bitmap UDP socket so poll() routes packets to its buffer
        {
            let socket = iface.get_socket::<UdpSocket>(*BITMAP_UDP_HANDLE.assume_init_ref());
            socket.bind(7000).ok();
        }

        // Initialize bitmap receiver
        BITMAP_RX.write(BitmapReceiver::new());

        // Initialize TFTP loader
        TFTP_LOADER.write(TftpConfigLoader::new());

        // Initialize HTTP state
        HTTP_REQUESTS[0].write(HttpRequest::new());
        HTTP_REQUESTS[1].write(HttpRequest::new());
        HTTP_RESPONSES[0].write(HttpResponse::new());
        HTTP_RESPONSES[1].write(HttpResponse::new());
    }
}

/// Update time from main loop's timer tick.
/// Call this from main loop when timer fires.
#[inline]
pub fn update_time_ms(ms: i64) {
    unsafe {
        TIME_MS = ms;
    }
}

/// Get the current time_ms value.
pub fn time_ms() -> i64 {
    unsafe { TIME_MS }
}

/// Check if currently streaming (for main loop to skip animation).
pub fn is_streaming() -> bool {
    const BOOT_GRACE_PERIOD_MS: i64 = 10_000;
    unsafe {
        TIME_MS > BOOT_GRACE_PERIOD_MS && TIME_MS - LAST_BITMAP_PACKET_MS < 200
    }
}

/// Get the boot server info (for display in status page).
pub fn boot_server() -> Option<([u8; 4], crate::menu::BootServerSource)> {
    unsafe { BOOT_SERVER }
}

/// Get bitmap stats for display.
pub fn bitmap_stats() -> BitmapStats {
    unsafe {
        if !BITMAP_STATS_PTR.is_null() {
            *BITMAP_STATS_PTR
        } else {
            BitmapStats::new()
        }
    }
}

/// Get MAC error counters from hardware.
pub fn mac_errors() -> (u32, u32, u32) {
    unsafe {
        if IFACE_INITIALIZED {
            IFACE.assume_init_ref().device().mac_errors()
        } else {
            (0, 0, 0)
        }
    }
}

/// Get debug counters for packet routing diagnostics.
/// Returns (fast_path, slow_path, max_batch).
pub fn debug_counters() -> (u32, u32, u32) {
    unsafe { (DBG_FAST_PATH, DBG_SLOW_PATH, DBG_ISR_MAX_BATCH) }
}

/// Get IP address for HTTP status page.
pub fn ip_addr() -> [u8; 4] {
    unsafe {
        if IFACE_INITIALIZED {
            match IFACE.assume_init_ref().ip_addrs()[0].address() {
                IpAddress::Ipv4(v4) => v4.0,
                _ => [0u8; 4],
            }
        } else {
            [0u8; 4]
        }
    }
}

// ============================================================================
// Network Handler - Called from ISR
// ============================================================================

/// Main network handler called from ISR.
/// Processes all incoming packets and socket events.
///
/// This function does:
/// - iface.poll() to process incoming packets
/// - DHCP socket handling
/// - HTTP socket handling
/// - Telnet socket handling
/// - Artnet UDP handling
/// - Bitmap UDP handling
/// Cycles spent inside the receive ISR, and the packets that work covered.
///
/// mcycle reads back 0 on this VexRiscv (see /api/flashbench), so this uses the
/// 1-second countdown timer. Two CSR reads per ISR entry, amortised over the
/// whole batch -- at a typical batch of ~10 that is noise against a per-packet
/// cost measured in thousands of cycles.
pub static mut ISR_CYCLES: u64 = 0;
pub static mut ISR_ENTRIES: u32 = 0;
pub static mut ISR_PACKETS: u32 = 0;

#[inline(always)]
unsafe fn prof_ticks() -> u32 {
    let t = &*pac::Timer0::ptr();
    t.update_value().write(|w| w.bits(1));
    TIMER_RELOAD - 1 - t.value().read().bits()
}

#[inline(always)]
fn prof_delta(a: u32, b: u32) -> u32 {
    if b >= a { b - a } else { b + TIMER_RELOAD - a }
}

#[no_mangle]
pub extern "C" fn network_handler() {
    unsafe {
        if !IFACE_INITIALIZED {
            return;
        }

        let prof_t0 = prof_ticks();

        // Update TIME_MS by draining timer ticks
        update_time_from_timer();

        let iface = IFACE.assume_init_mut();
        let time = Instant::from_millis(TIME_MS);
        let mut had_arp = false;
        let mut had_tcp = false;
        let mut had_udp = false;
        let mut batch_count = 0u32;

        // Process packets in hardware FIFO (limit to prevent ISR starvation)
        const MAX_PACKETS_PER_ISR: u32 = 64;
        loop {
            if batch_count >= MAX_PACKETS_PER_ISR { break; }
            let eth = iface.device();
            match eth.peek_rx() {
                Some(frame) if is_bitmap_udp(frame) => {
                    // Bitmap UDP: process directly (fast path)
                    DBG_FAST_PATH += 1;
                    batch_count += 1;
                    process_raw_bitmap(frame);
                    eth.ack_rx();
                }
                Some(frame) if is_foreign_arp(frame) => {
                    // Not our ARP: acknowledge and discard without waking
                    // smoltcp, which would run the whole stack in this ISR.
                    DBG_ARP_DROPPED += 1;
                    batch_count += 1;
                    eth.ack_rx();
                }
                Some(frame) if is_multicast(frame) => {
                    // Drop multicast packets (VRRP, mDNS, etc.) - we don't need them
                    DBG_MULTICAST_DROPPED += 1;
                    batch_count += 1;
                    eth.ack_rx();
                }
                Some(frame) if is_unwanted_udp(frame) => {
                    // Drop UDP packets we don't need (NetBIOS, mDNS, etc.)
                    DBG_MULTICAST_DROPPED += 1;  // Reuse counter for all dropped
                    batch_count += 1;
                    eth.ack_rx();
                }
                Some(frame) => {
                    // Non-bitmap: let smoltcp process (ARP, HTTP, etc.)
                    DBG_SLOW_PATH += 1;
                    batch_count += 1;

                    // Classify packet type and set appropriate flag
                    // Default to ARP flag for any unclassified packet (ensures handlers run)
                    if frame.len() >= 14 {
                        let ethertype = ((frame[12] as u16) << 8) | frame[13] as u16;
                        match ethertype {
                            0x0806 => { DBG_SLOW_ARP += 1; had_arp = true; }
                            0x0800 if frame.len() >= 24 => {
                                match frame[23] {
                                    6 => { DBG_SLOW_TCP += 1; had_tcp = true; }
                                    17 => { DBG_SLOW_UDP += 1; had_udp = true; }
                                    _ => { DBG_SLOW_OTHER += 1; had_arp = true; }  // ICMP, etc.
                                }
                            }
                            _ => { DBG_SLOW_OTHER += 1; had_arp = true; }
                        }
                    } else {
                        had_arp = true;  // Ensure handlers run for any packet
                    }

                    // Capture first slow-path packet after streaming starts (for debugging)
                    if !DBG_SLOW_PKT_CAPTURED && DBG_FAST_PATH > 100 {
                        let copy_len = frame.len().min(64);
                        DBG_SLOW_PKT[..copy_len].copy_from_slice(&frame[..copy_len]);
                        DBG_SLOW_PKT_LEN = frame.len();
                        DBG_SLOW_PKT_CAPTURED = true;
                    }
                    iface.poll(time).ok();
                }
                None => break,
            }
        }

        // Track max batch size for diagnostics
        if batch_count > DBG_ISR_MAX_BATCH {
            DBG_ISR_MAX_BATCH = batch_count;
        }

        // Handle socket events.
        //
        // These used to run unconditionally. "Cheap when idle" is true at 10 Hz
        // and false at 2000 Hz: measured 1.13 bitmap packets per ISR entry, so
        // the ISR is entered essentially once per packet and every one of them
        // was walking smoltcp's TCP socket state. Run them when there is actual
        // slow-path traffic, or on a deadline so an idle connection still gets
        // serviced promptly. SOCKET_POLL_MS bounds the added HTTP/telnet latency.
        const SOCKET_POLL_MS: i64 = 5;
        let sockets_due = had_tcp || had_arp
            || TIME_MS < LAST_SOCKET_POLL_MS                       // clock stepped back
            || TIME_MS - LAST_SOCKET_POLL_MS >= SOCKET_POLL_MS;
        let mut http_needs_poll = false;
        if sockets_due {
            LAST_SOCKET_POLL_MS = TIME_MS;
            handle_telnet(iface);
            http_needs_poll = handle_http(iface);
        }

        // UDP handlers + TFTP: run on any slow-path packet, or while TFTP
        // config is loading (iface.poll may have delivered packets to sockets
        // during processing of a different packet type in the batch loop)
        let had_slow_path = had_arp || had_tcp || had_udp;
        let tftp_active = TFTP_STARTED && !TFTP_LOADER.assume_init_ref().is_done();
        if had_slow_path || tftp_active {
            handle_dhcp(iface);
            handle_tftp(iface);
            handle_artnet(iface);
            handle_bitmap_smoltcp(iface);
        }

        // Poll to transmit queued responses
        if had_slow_path || http_needs_poll || tftp_active {
            iface.poll(time).ok();
        }

        crate::ethernet::check_and_reenable_interrupt();

        // Accounted last, so it covers everything the ISR did including the
        // re-enable above.
        ISR_CYCLES = ISR_CYCLES.wrapping_add(prof_delta(prof_t0, prof_ticks()) as u64);
        ISR_ENTRIES = ISR_ENTRIES.wrapping_add(1);
        ISR_PACKETS = ISR_PACKETS.wrapping_add(batch_count);
    }
}

/// Handle DHCP events.
/// Publish our MAC/IP to the gateware's hardware UDP filters.
///
/// The hardware receive path (Tier 2) matches destination MAC and IP itself.
/// Our MAC is derived from the flash unique ID and our IP comes from DHCP, so
/// neither is known at synthesis time -- if these CSRs are not kept current the
/// filters match nothing and the hardware path silently sees zero packets.
pub unsafe fn publish_hw_filter_mac(mac: &[u8; 6]) {
    // A 48-bit CSR splits across a 32-bit bus as
    //   word0 = bits 31:0 (LOW 32), word1 = bits 47:32 (HIGH 16)
    // and atomic_write commits on word0, so word1 must be written FIRST.
    //
    // Determined from hardware rather than assumed: a gateware-written
    // CSRStatus(48) holding a known-good parsed MAC read back rotated by two
    // bytes under the opposite convention. Getting this wrong is silent -- the
    // readback looks correct because it is wrong in the same direction as the
    // write, while the filter compares against a scrambled value and matches
    // nothing, leaving the hardware path idle with no error anywhere.
    let full: u64 = ((mac[0] as u64) << 40) | ((mac[1] as u64) << 32)
                  | ((mac[2] as u64) << 24) | ((mac[3] as u64) << 16)
                  | ((mac[4] as u64) << 8)  | (mac[5] as u64);
    let low32: u32 = (full & 0xFFFF_FFFF) as u32;
    let high16: u32 = ((full >> 32) & 0xFFFF) as u32;
    let p = litex_pac::Peripherals::steal();
    p.ethmac.mac_address1().write(|w| w.bits(high16));
    p.ethmac.mac_address0().write(|w| w.bits(low32));
}

pub unsafe fn publish_hw_filter_ip(ip: Ipv4Address) {
    let o = ip.0;
    OUR_IPV4 = o;
    let v: u32 = ((o[0] as u32) << 24) | ((o[1] as u32) << 16) | ((o[2] as u32) << 8) | (o[3] as u32);
    let p = litex_pac::Peripherals::steal();
    p.ethmac.ip_address().write(|w| w.bits(v));
}

unsafe fn handle_dhcp(iface: &mut Interface<'static, Eth>) {
    let dhcp_handle = *DHCP_HANDLE.assume_init_ref();
    let socket = iface.get_socket::<Dhcpv4Socket>(dhcp_handle);

    if let Some(event) = socket.poll() {
        match event {
            Dhcpv4Event::Configured(config) => {
                DHCP_CONFIGURED = true;
                iface.update_ip_addrs(|addrs| {
                    addrs[0] = IpCidr::Ipv4(config.address);
                });
                publish_hw_filter_ip(config.address.address());
                if let Some(router) = config.router {
                    iface.routes_mut().add_default_ipv4_route(router).ok();
                }
                // Start TFTP config load if not already done
                if !TFTP_STARTED {
                    use crate::menu::BootServerSource;
                    let (server, source) = if let Some(ip) = config.tftp_server_name {
                        (ip, BootServerSource::Option66)
                    } else {
                        (Ipv4Address([10, 11, 6, 65]), BootServerSource::Fallback)
                    };
                    BOOT_SERVER = Some((server.0, source));

                    // Build MAC-based filename
                    let m = &MAC_BYTES;
                    let mut fname = [0u8; 21];
                    const HEX: &[u8; 16] = b"0123456789abcdef";
                    for i in 0..6 {
                        fname[i * 3] = HEX[(m[i] >> 4) as usize];
                        fname[i * 3 + 1] = HEX[(m[i] & 0xf) as usize];
                        if i < 5 { fname[i * 3 + 2] = b'-'; }
                    }
                    fname[17..21].copy_from_slice(b".yml");
                    let fname_str = core::str::from_utf8(&fname).unwrap_or("config.yml");

                    TFTP_LOADER.assume_init_mut().start(server, fname_str);
                    TFTP_STARTED = true;
                }
            }
            Dhcpv4Event::Deconfigured => {
                DHCP_CONFIGURED = false;
                iface.update_ip_addrs(|addrs| {
                    addrs[0] = IpCidr::new(IpAddress::Ipv4(Ipv4Address::UNSPECIFIED), 0);
                });
                iface.routes_mut().remove_default_ipv4_route();
            }
        }
    }

    // Static IP fallback after 10 seconds
    if TIME_MS == 10_000 {
        if iface.ip_addrs()[0].address() == IpAddress::Ipv4(Ipv4Address::UNSPECIFIED) {
            let fallback = Ipv4Cidr::new(Ipv4Address([10, 11, 6, 250]), 24);
            iface.update_ip_addrs(|addrs| {
                addrs[0] = IpCidr::Ipv4(fallback);
            });
            publish_hw_filter_ip(fallback.address());
        }
    }
}

/// Handle TFTP config loading.
unsafe fn handle_tftp(iface: &mut Interface<'static, Eth>) {
    let tftp_loader = TFTP_LOADER.assume_init_mut();
    let tftp_handle = *TFTP_UDP_HANDLE.assume_init_ref();

    // Close socket after Done (ACK was transmitted by previous iface.poll())
    if tftp_loader.is_done() {
        let socket = iface.get_socket::<UdpSocket>(tftp_handle);
        if socket.is_open() {
            socket.close();
        }
        return;
    }
    if !tftp_loader.is_active() {
        return;
    }

    let socket = iface.get_socket::<UdpSocket>(tftp_handle);

    if tftp_loader.poll(socket, TIME_MS) {
        // Config loaded - parse and apply layout
        if let Some(layout) = tftp_loader.parse_config() {
            apply_layout(layout);
        }
    }
}

/// Apply a layout: panel geometry, then `mode:`/`program:`.
///
/// Shared by the TFTP config and the compile-time default (`apply_default_config`)
/// so a panel behaves identically whether its layout arrived over the network or
/// was baked into the firmware. One code path, so the offline case cannot drift
/// from the fleet case -- which it would, being exercised far less often.
/// What the last chip configuration attempt did, for the status page.
#[cfg(feature = "spwm")]
pub static mut SPWM_STATE: crate::spwm::Configured = crate::spwm::Configured::Default;

pub unsafe fn apply_layout(layout: LayoutConfig) {
    if HUB75_PTR.is_null() || LAYOUT_PTR.is_null() {
        return;
    }
    let hub75 = &mut *HUB75_PTR;
    let w = layout.virtual_width();
    let h = layout.virtual_height();
    // Read before the move: the config owns the name.
    let want_program = layout.mode == crate::layout::DisplayMode::Program;
    let prog = crate::generated::by_name(layout.program());
    let prog_dyn = prog.and_then(|p| p.dynamic);
    let prog_name = crate::generated::PROGRAMS
        .iter()
        .find(|p| p.name == layout.program())
        .map(|p| p.name);
    // Configure the driver chip BEFORE the geometry, so the panel is never
    // being scanned with a table that does not match what is about to be sent
    // to it. On a HUB75 bitstream there is no chip table and this is compiled
    // out entirely.
    #[cfg(feature = "spwm")]
    {
        let scan = crate::layout::scan_rows(&layout);
        let res = crate::spwm::configure(hub75.regs(), Some(layout.drive()), scan);
        unsafe { SPWM_STATE = res };
    }
    layout.apply(hub75);
    *LAYOUT_PTR = layout;

    // Redraw at new virtual size
    let total = (w as u32) * (h as u32);
    hub75.set_img_param(w, total);
    hub75.write_img_data(0, crate::patterns::grid(w, h));
    hub75.swap_buffers();

    // `mode: program` in the config, applied at boot -- so program
    // mode survives a reboot instead of needing an HTTP call. If the
    // named program is not in this firmware, FALL BACK TO STREAM
    // rather than showing nothing: a panel that boots dark after a
    // config typo is far worse than one showing the wrong thing.
    if want_program {
        match (prog, prog_name) {
            (Some(pr), Some(n)) => {
                PROGRAM_FN = Some(pr);
                PROGRAM_DYNAMIC = prog_dyn;
                PROGRAM_FULL_LEFT = 2;
                PROGRAM_PRIME_LEFT = 2;
                PROGRAM_NAME = n;
                PROGRAM_ACTIVE = true;
                PROGRAM_FRAME = 0;
                PROGRAM_NEXT_MS = 0;
                hub75.set_palette_all(0, crate::program::palette_default);
                // A program owns the framebuffer: stop the CPU path
                // AND the pixel DMA, or the stream fights it.
                set_stream_hold(true);
            }
            _ => {
                PROGRAM_ACTIVE = false;
                set_stream_hold(false);
            }
        }
    }
}

/// Apply the layout baked in at build time, if there is one.
///
/// Called once at startup, BEFORE the network comes up. A panel with a default
/// config is therefore drawing its own content within a second of power-on,
/// rather than after DHCP has timed out and TFTP has failed -- and it never
/// needed either.
///
/// This is the whole standalone story. The program code was always compiled into
/// the firmware; what came only from the network was the single line SELECTING
/// it, plus the panel-to-connector map. Baking a config at build time closes
/// that, with no runtime state, no flash wear and no second source of truth: a
/// cached copy that could disagree with the config you are serving is worse than
/// no copy at all.
///
/// A TFTP config still wins when one arrives, so a fleet panel is unaffected --
/// the default is what it shows until then, and what it falls back to when the
/// network is not there at all.
pub unsafe fn apply_default_config() -> bool {
    match crate::default_config::DEFAULT_CONFIG {
        Some(text) => match LayoutConfig::parse(text) {
            Some(layout) => {
                apply_layout(layout);
                true
            }
            // A malformed baked config is a BUILD error that reached a board, so
            // it cannot be reported anywhere useful. Fall through to the
            // single-panel default rather than leaving the panel unconfigured.
            None => false,
        },
        None => false,
    }
}

/// Handle Telnet (TCP port 23).
unsafe fn handle_telnet(iface: &mut Interface<'static, Eth>) {
    let tcp_handle = *TCP_SERVER_HANDLE.assume_init_ref();
    let socket = iface.get_socket::<TcpSocket>(tcp_handle);

    if !socket.is_open() {
        socket.listen(23).ok();
    }

    // Telnet handling is minimal in ISR - just keep socket open
    // Full menu handling would require too much state
    if !TELNET_ACTIVE && socket.is_active() {
        IAC_STATE = 0;
        TELNET_ACTIVE = true;
    }
    if !socket.is_active() {
        TELNET_ACTIVE = false;
    }

    // For now, just drain received data to prevent buffer overflow
    if socket.may_recv() {
        let mut buf = [0u8; 64];
        while socket.can_recv() {
            socket.recv_slice(&mut buf).ok();
        }
    }
}

/// Handle Artnet (UDP port 6454).
unsafe fn handle_artnet(iface: &mut Interface<'static, Eth>) {
    let udp_handle = *UDP_SERVER_HANDLE.assume_init_ref();
    let socket = iface.get_socket::<UdpSocket>(udp_handle);

    if !socket.is_open() {
        socket.bind(6454).ok();
    }

    if HUB75_PTR.is_null() {
        return;
    }
    let hub75 = &mut *HUB75_PTR;

    while let Ok((data, _endpoint)) = socket.recv() {
        if let Ok((offset, data)) = crate::artnet::packet2hub75(data) {
            let palette_offset = ((1 << 16) - 2) * 170;
            if offset >= palette_offset {
                hub75.set_palette((offset - palette_offset) as u8, data);
            }
        }
    }
}

/// Handle bitmap UDP packets that went through smoltcp.
unsafe fn handle_bitmap_smoltcp(iface: &mut Interface<'static, Eth>) {
    let bitmap_handle = *BITMAP_UDP_HANDLE.assume_init_ref();
    let socket = iface.get_socket::<UdpSocket>(bitmap_handle);

    if HUB75_PTR.is_null() {
        return;
    }
    let hub75 = &mut *HUB75_PTR;
    let bitmap_rx = BITMAP_RX.assume_init_mut();

    while let Ok((data, _endpoint)) = socket.recv() {
        LAST_BITMAP_PACKET_MS = TIME_MS;
        let presented = bitmap_rx.process_packet(data, hub75, TIME_MS);
        if presented {
            // The wire format is authoritative: process_packet() already selected
            // the output mode from the magic. Forcing FullColor here clobbered the
            // Indexed mode set moments earlier, so 'B','I' content scanned out as
            // full colour -- the index sits in the low byte of 0x00GGRRBB, so every
            // pixel became one channel at index brightness: the whole sign a single
            // colour with the shapes still legible. No counter can see colour, which
            // is why every counter-based test passed.
            hub75.on();
            if !ANIMATION_PTR.is_null() {
                *ANIMATION_PTR = crate::menu::Animation::None;
            }
        }
        if !BITMAP_STATS_PTR.is_null() {
            *BITMAP_STATS_PTR = bitmap_rx.stats;
        }
    }
}

/// Handle HTTP (TCP port 80). Returns true if poll() needed (socket closed).
unsafe fn handle_http(iface: &mut Interface<'static, Eth>) -> bool {
    let http_handles = [
        *HTTP_HANDLE_A.assume_init_ref(),
        *HTTP_HANDLE_B.assume_init_ref(),
    ];

    let http_ip = match iface.ip_addrs()[0].address() {
        IpAddress::Ipv4(v4) => v4.0,
        _ => [0u8; 4],
    };

    let mut needs_poll = false;
    for i in 0..2 {
        let socket = iface.get_socket::<TcpSocket>(http_handles[i]);
        let request = HTTP_REQUESTS[i].assume_init_mut();
        let response = HTTP_RESPONSES[i].assume_init_mut();

        // Recycle socket after graceful close
        if HTTP_CLOSE_AT[i] > 0 {
            if !socket.is_active() || TIME_MS >= HTTP_CLOSE_AT[i] {
                if socket.is_open() {
                    socket.abort();
                }
                HTTP_CLOSE_AT[i] = 0;
            }
        }

        if !socket.is_open() {
            request.reset();
            response.data.clear();
            HTTP_RESPONSE_SENT[i] = 0;
            HTTP_CONNECTED_AT[i] = 0;
            socket.listen(80).ok();
        }

        // Only process fully established connections
        // Skip if socket is still in handshake (is_active but not yet may_recv)
        if !socket.may_recv() && !socket.may_send() {
            // Not established yet or already closed - skip
            if !socket.is_active() {
                HTTP_CONNECTED_AT[i] = 0;
            }
            continue;
        }

        // Track connection time for timeout
        if socket.is_active() {
            if HTTP_CONNECTED_AT[i] == 0 {
                HTTP_CONNECTED_AT[i] = TIME_MS;
            }
            // Abort if request incomplete and timed out
            if !request.is_complete() && TIME_MS > 0 && TIME_MS - HTTP_CONNECTED_AT[i] > 5000 {
                socket.abort();
                HTTP_CONNECTED_AT[i] = 0;
                continue;
            }
        } else {
            HTTP_CONNECTED_AT[i] = 0;
        }

        // Receive request
        if socket.can_recv() && !request.is_complete() {
            let mut buf = [0u8; 128];
            if let Ok(n) = socket.recv_slice(&mut buf) {
                if request.feed(&buf[..n]) {
                    // Request complete - handle it
                    // Note: We need Context for handle_request, but we don't have it in ISR
                    // For now, just send a minimal response
                    handle_http_request(request, response, http_ip);
                    HTTP_RESPONSE_SENT[i] = 0;
                }
            }
        }

        // Send response
        if socket.can_send() && HTTP_RESPONSE_SENT[i] < response.data.len() {
            if let Ok(sent) = socket.send_slice(&response.data[HTTP_RESPONSE_SENT[i]..]) {
                HTTP_RESPONSE_SENT[i] += sent;
            }
            if HTTP_RESPONSE_SENT[i] >= response.data.len() && response.data.len() > 0 {
                socket.close();
                HTTP_CLOSE_AT[i] = TIME_MS + 50;
                needs_poll = true;  // Need poll to send FIN
            }
        }
    }
    needs_poll
}

/// Handle HTTP request in ISR context.
unsafe fn handle_http_request(req: &HttpRequest, resp: &mut HttpResponse, ip: [u8; 4]) {
    use core::fmt::Write;
    use crate::http::Method;

    match (req.method(), req.path()) {
        (Method::Get, "/") => page_status(resp, ip),
        (Method::Get, "/api/status") => api_status(resp, ip),
        (Method::Get, "/api/layout") => api_layout_get(resp),
        (Method::Get, "/api/display") => api_display_get(resp),
        (Method::Get, "/api/bitmap/stats") => api_bitmap_stats(resp),
        (Method::Get, "/api/dmatest") => api_dmatest(resp),
        (Method::Post, "/api/dma/on") => api_dma_set(resp, true),
        (Method::Get, "/api/dma/arrival") => api_dma_arrival(resp),
        (Method::Get, "/api/isrprof") => api_isr_prof(resp, false),
        (Method::Post, "/api/isrprof/reset") => api_isr_prof(resp, true),
        (Method::Post, "/api/rgborder") => api_rgb_order(req, resp),
        (Method::Get, "/api/rgborder") => api_rgb_order_get(resp),
        (Method::Get, "/api/hwfilter") => api_hw_filter(resp, None),
        (Method::Post, "/api/hwfilter/on") => api_hw_filter(resp, Some(true)),
        (Method::Post, "/api/hwfilter/off") => api_hw_filter(resp, Some(false)),
        (Method::Post, "/api/cpupix/on") => api_cpu_pixels(resp, true),
        (Method::Post, "/api/cpupix/off") => api_cpu_pixels(resp, false),
        (Method::Post, "/api/dma/off") => api_dma_set(resp, false),
        (Method::Post, "/api/program/on") => api_program_set(req, resp, true),
        (Method::Post, "/api/program/off") => api_program_set(req, resp, false),
        (Method::Get, "/api/programs") => api_programs(resp),
        (Method::Get, "/api/bench") => api_bench(resp),
        (Method::Get, "/api/profile") => api_profile(resp),
        (Method::Get, "/api/fb") => api_fb(resp),
        (Method::Post, "/api/fbdump") => api_fbdump(req, resp),
        (Method::Get, "/api/palette") => api_palette(resp),
        (Method::Get, "/api/flash") => api_flash(resp),
        (Method::Get, "/api/flashbench") => api_flashbench(resp),
        (Method::Post, "/api/flash/drive") => api_flash_drive(req, resp),
        (Method::Get, "/api/values") => api_values_get(resp),
        (Method::Post, "/api/values") => api_values_set(req, resp),
        (Method::Post, "/api/display/hold/on") => api_hold_set(resp, true),
        (Method::Post, "/api/display/hold/off") => api_hold_set(resp, false),
        (Method::Post, "/api/display/on") => api_display_on(resp),
        (Method::Post, "/api/display/off") => api_display_off(resp),
        (Method::Post, "/api/display/pattern") => api_display_pattern(req, resp),
        (Method::Post, "/api/reboot") => api_reboot(resp),
        _ => {
            resp.data.clear();
            resp.data.extend_from_slice(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\nNot Found").ok();
        }
    }
}

// ============================================================================
// HTTP Page and API Handlers
// ============================================================================

unsafe fn page_status(resp: &mut HttpResponse, ip: [u8; 4]) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: text/html;charset=utf-8\r\nConnection: close\r\n\r\n").ok();

    let m = &MAC_BYTES;
    let (w, len) = if !HUB75_PTR.is_null() { (*HUB75_PTR).get_img_param() } else { (0, 0) };
    let h = if w > 0 { len / w as u32 } else { 0 };
    let layout = if !LAYOUT_PTR.is_null() { &*LAYOUT_PTR } else { return; };
    let stats = if !BITMAP_STATS_PTR.is_null() { &*BITMAP_STATS_PTR } else { return; };
    let anim = if !ANIMATION_PTR.is_null() {
        match &*ANIMATION_PTR {
            crate::menu::Animation::None => "None",
            crate::menu::Animation::Rainbow { .. } => "Rainbow",
        }
    } else { "Unknown" };

    // Get hardware counters
    let (mac_ovf, mac_pre, mac_crc) = if IFACE_INITIALIZED {
        IFACE.assume_init_ref().device().mac_errors()
    } else { (0, 0, 0) };
    let ring_ovf = crate::ethernet::ring_overflow_count();
    let isr_count = crate::ethernet::isr_count();

    // Calculate FPS
    let avg = stats.avg_interval_ms;
    let fps = if avg > 0 { 1000 / avg } else { 0 };

    // Read CSRs for interrupt status
    let mstatus: u32;
    let mie: u32;
    let irq_mask: u32;
    core::arch::asm!("csrr {}, mstatus", out(reg) mstatus);
    core::arch::asm!("csrr {}, mie", out(reg) mie);
    core::arch::asm!("csrr {}, 0xBC0", out(reg) irq_mask);
    // This page is rendered from inside the trap handler (all network processing
    // runs in the ISR), where hardware has already cleared mstatus.MIE on trap
    // entry. Reading MIE here therefore always yields 0 and looks like a fault.
    // The pre-trap value is preserved in mstatus.MPIE (bit 7) — that is the bit
    // that actually says whether interrupts are enabled.
    let mie_enabled = (mstatus & 0x80) != 0;
    let meie_enabled = (mie & 0x800) != 0;
    let ethmac_masked = (irq_mask & 0x4) != 0;

    // HTML head with professional dark theme
    write!(resp, "\
<!DOCTYPE html><html><head>\
<meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>\
<link rel=icon href=\"data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 8 8'><rect width='8' height='8' rx='1.5' fill='%23111'/><rect x='1' y='1' width='2.4' height='2.4' rx='.5' fill='%23e04b4b'/><rect x='4.6' y='1' width='2.4' height='2.4' rx='.5' fill='%234bd07a'/><rect x='1' y='4.6' width='2.4' height='2.4' rx='.5' fill='%234b8fe0'/><rect x='4.6' y='4.6' width='2.4' height='2.4' rx='.5' fill='%23e0c34b'/></svg>\"><title>Colorlight {}</title>\
<style>\
*{{margin:0;box-sizing:border-box}}\
body{{font:15px/1.5 system-ui,sans-serif;background:#0a0a0f;color:#c0c0c8;padding:24px}}\
h1{{font-size:22px;color:#e8e8f0;margin:0 0 20px;padding-left:12px;border-left:4px solid #5050d0}}\
.g{{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}}\
.c{{background:#14141e;border:1px solid #202030;border-radius:8px;padding:16px}}\
.c h2{{font-size:11px;text-transform:uppercase;letter-spacing:1.5px;color:#6060a0;margin:0 0 12px;font-weight:600}}\
table{{width:100%;border-collapse:collapse}}\
td{{padding:4px 0;font-size:15px}}td:first-child{{color:#8080a0}}\
td+td{{text-align:right;color:#e0e0e8;font-variant-numeric:tabular-nums}}\
.ok{{color:#50d080}}.warn{{color:#f0a050}}.err{{color:#e05050}}\
select,button{{font:14px system-ui;background:#1c1c2c;color:#d0d0d8;border:1px solid #303048;padding:7px 12px;border-radius:6px}}\
button{{background:#2a2a40;color:#d8d8e4;border:1px solid #3a3a58;cursor:pointer}}\
button:hover{{background:#34344e;border-color:#4a4a70}}\
button.pri{{background:#4848b0;border-color:#4848b0;color:#fff}}button.pri:hover{{background:#5858c0}}\
button.dgr{{background:#2a1a1e;border-color:#5a2a32;color:#e09098}}button.dgr:hover{{background:#3a2226}}\
.r{{display:flex;align-items:center;gap:8px;flex-wrap:wrap;padding:9px 0;border-top:1px solid #1e1e2c}}\
.r:first-of-type{{border-top:0;padding-top:0}}\
.r .k{{flex:0 0 96px;color:#6a6a8a;font-size:12px;text-transform:uppercase;letter-spacing:.8px}}\
.hint{{color:#5a5a6a;font-size:12px;margin:6px 0 0 104px}}\
#m{{color:#8080a0;font-size:12px}}\
.mono{{font-family:monospace}}\
.ft{{margin-top:20px;text-align:center;font-size:13px;color:#4a4a6a}}\
a{{color:#7090d0;text-decoration:none}}\
</style></head><body>\
<h1>Colorlight v{} <span style='font-size:13px;color:#6a6a8a;font-weight:normal'>// Interrupt-Driven</span></h1><div class=g>",
        env!("CARGO_PKG_VERSION"), env!("CARGO_PKG_VERSION")).ok();

    // Network card
    write!(resp, "<div class=c><h2>Network</h2><table>\
<tr><td>MAC</td><td class=mono>{:02X}:{:02X}:{:02X}:{:02X}:{:02X}:{:02X}</td></tr>\
<tr><td>IPv4</td><td class=mono>{}.{}.{}.{}</td></tr>",
        m[0], m[1], m[2], m[3], m[4], m[5],
        ip[0], ip[1], ip[2], ip[3]).ok();
    if let Some((sip, source)) = BOOT_SERVER {
        let src = match source {
            crate::menu::BootServerSource::Option66 => "DHCP opt66",
            crate::menu::BootServerSource::Fallback => "fallback",
        };
        write!(resp, "<tr><td>TFTP Server</td><td class=mono>{}.{}.{}.{} <span style='color:#5a5a6a'>({})</span></td></tr>",
            sip[0], sip[1], sip[2], sip[3], src).ok();
    }
    write!(resp, "</table></div>").ok();

    // Display card
    let (hw_cols, hw_rows, hw_scan, hw_cl2, hw_outs) = if !HUB75_PTR.is_null() {
        (*HUB75_PTR).get_hw_info()
    } else { (0, 0, 0, 0, 0) };
    let hw_chain = 1u8 << hw_cl2;
    write!(resp, "<div class=c><h2>Display</h2><table>\
<tr><td>Hardware</td><td>{}\u{00d7}{} <span style='color:#5a5a6a'>(1/{} scan, {} out \u{00d7} {} chain)</span></td></tr>\
<tr><td>Resolution</td><td>{}x{}</td></tr>\
<tr><td>Virtual Grid</td><td>{}x{} = {}x{}</td></tr>\
<tr><td>Panel Size</td><td>{}x{}</td></tr>\
<tr><td>Animation</td><td>{}</td></tr>\
</table></div>",
        hw_cols, hw_rows, hw_scan, hw_outs, hw_chain,
        w, h,
        layout.grid_cols, layout.grid_rows, layout.virtual_width(), layout.virtual_height(),
        layout.panel_width, layout.panel_height, anim).ok();

    // Interrupt Status card
    write!(resp, "<div class=c><h2>Interrupt Status</h2><table>\
<tr><td>ISR Count</td><td class='{}'>{}</td></tr>\
<tr><td>mstatus.MPIE</td><td class='{}'>{}</td></tr>\
<tr><td>mie.MEIE</td><td class='{}'>{}</td></tr>\
<tr><td>IRQ_MASK[2]</td><td class='{}'>{}</td></tr>\
<tr><td>Mode</td><td class='ok'>ISR-driven</td></tr>\
</table></div>",
        if isr_count > 0 { "ok" } else { "warn" }, isr_count,
        if mie_enabled { "ok" } else { "err" }, if mie_enabled { "enabled" } else { "disabled" },
        if meie_enabled { "ok" } else { "err" }, if meie_enabled { "enabled" } else { "disabled" },
        if ethmac_masked { "ok" } else { "err" }, if ethmac_masked { "enabled" } else { "disabled" }).ok();

    // Streaming card
    let src = display_source();
    let src_held = !BITMAP_STATS_PTR.is_null() && BITMAP_RX.assume_init_ref().is_held();
    write!(resp, "<div class=c><h2>Streaming</h2><table>\
<tr><td>Source</td><td class='{}'><b>{}</b></td></tr>\
<tr><td>Program</td><td>{}</td></tr>\
<tr><td>Frames</td><td>{}</td></tr>\
<tr><td>Partial</td><td class='{}'>{}</td></tr>\
<tr><td>Dropped</td><td class='{}'>{}</td></tr>\
<tr><td>FPS</td><td>{} <span style='color:#5a5a6a'>({}ms avg)</span></td></tr>\
<tr><td>Jitter</td><td class='{}'>{}ms</td></tr>\
<tr><td>Stale-flushed</td><td class='{}'>{}</td></tr>\
<tr><td>Chunks repaired</td><td class='{}'>{}</td></tr>\
<tr><td>Duplicates</td><td>{}</td></tr>\
</table></div>",
        if src_held { "warn" } else { "ok" }, src,
        if PROGRAM_ACTIVE { PROGRAM_NAME } else { "-" },
        stats.frames_completed,
        if stats.frames_partial > 0 { "warn" } else { "" }, stats.frames_partial,
        if stats.frames_dropped > 0 { "err" } else { "" }, stats.frames_dropped,
        fps, avg,
        if stats.jitter_ms > 10 { "warn" } else { "" }, stats.jitter_ms,
        if stats.frames_stale > 0 { "warn" } else { "" }, stats.frames_stale,
        if stats.chunks_repaired > 0 { "warn" } else { "" }, stats.chunks_repaired,
        stats.packets_duplicate).ok();

    // MAC Diagnostics card
    let (dbg_fast, dbg_slow, dbg_batch) = debug_counters();
    write!(resp, "<div class=c><h2>MAC Diagnostics</h2><table>\
<tr><td>RX Overflow</td><td class='{}'>{}</td></tr>\
<tr><td>CRC Errors</td><td class='{}'>{}</td></tr>\
<tr><td>Preamble Errors</td><td class='{}'>{}</td></tr>\
<tr><td>Ring Overflow</td><td class='{}'>{}</td></tr>\
<tr><td>Fast Path</td><td class='ok'>{}</td></tr>\
<tr><td>Slow Path</td><td class='{}'>{} <span style='color:#5a5a6a'>(arp:{} tcp:{} udp:{} other:{})</span></td></tr>\
<tr><td>Multicast Drop</td><td>{}</td></tr>\
<tr><td>Max Batch</td><td class='{}'>{}</td></tr>\
</table></div>",
        if mac_ovf > 0 { "err" } else { "" }, mac_ovf,
        if mac_crc > 0 { "err" } else { "" }, mac_crc,
        if mac_pre > 0 { "warn" } else { "" }, mac_pre,
        if ring_ovf > 0 { "err" } else { "" }, ring_ovf,
        dbg_fast,
        if dbg_slow > 0 { "warn" } else { "" }, dbg_slow,
        DBG_SLOW_ARP, DBG_SLOW_TCP, DBG_SLOW_UDP, DBG_SLOW_OTHER,
        DBG_MULTICAST_DROPPED,
        if dbg_batch > 8 { "warn" } else { "" }, dbg_batch).ok();

    // Panels card - show all 6 connectors; only show chain slot [1] if chain_length > 1
    let max_chain_slots = if hw_cl2 > 0 { 2usize } else { 1usize };
    write!(resp, "<div class=c><h2>Panel Assignments</h2><table>").ok();
    for i in 0..6 {
        if i < layout.assignments.len() {
            for c in 0..max_chain_slots {
                if c < layout.assignments[i].len() {
                    match layout.assignments[i][c] {
                        Some((col, row)) => {
                            write!(resp, "<tr><td>J{}[{}]</td><td>{},{}</td></tr>", i + 1, c, col, row).ok();
                        }
                        None => {
                            write!(resp, "<tr><td>J{}[{}]</td><td style='color:#404060'>-</td></tr>", i + 1, c).ok();
                        }
                    }
                }
            }
        }
    }
    write!(resp, "</table></div>").ok();

    // Values card -- the data plane made visible. Without this a program-mode
    // panel is a black box: you cannot tell "marquee stopped pushing" from
    // "the program ignores that key" by looking at the glass.
    write!(resp, "<div class=c><h2>Values <span style='font-size:12px;color:#6a6a8a'>(pushed, not streamed)</span></h2><table>").ok();
    if VALUES.len() == 0 {
        write!(resp, "<tr><td colspan=2 style='color:#6a6a8a'>none pushed yet — POST /api/values</td></tr>").ok();
    } else {
        for v in VALUES.iter() {
            let age = TIME_MS.saturating_sub(v.updated_ms);
            write!(resp, "<tr><td>{}</td><td><b>{}</b> <span style='color:#5a5a6a'>{}s ago</span></td></tr>",
                v.key(), v.value(), age / 1000).ok();
        }
    }
    write!(resp, "</table></div>").ok();

    // Controls card
    write!(resp, "<div class=c><h2>Controls</h2>\
<div class=r><span class=k>Source</span>\
<button class=pri onclick=\"fetch('/api/program/on',{{method:'POST'}}).then(()=>location.reload())\">On-panel</button>\
<button onclick=\"fetch('/api/program/off',{{method:'POST'}}).then(()=>location.reload())\">Stream</button>\
</div>\
<div class=hint>Program composes pixels here from POST /api/values. Stream receives frames from marquee.</div>\
<div class=r><span class=k>Pattern</span>\
<select id=p>\
<option>grid<option>rainbow<option>rainbow_anim\
<option>white<option>red<option>green<option>blue\
</select>\
<button onclick=\"fetch('/api/display/pattern',{{method:'POST',headers:{{'Content-Type':'application/json'}},\
body:JSON.stringify({{name:p.value}})}}).then(r=>r.json()).then(j=>{{m.textContent=j.ok?'showing '+j.pattern:'error'}})\
.catch(()=>{{m.textContent='failed'}})\">Show</button>\
<span id=m></span>\
</div>\
<div class=hint>Loading a pattern takes over the display: program stops and the stream is held.</div>\
<div class=r><span class=k>Stream</span>\
<button onclick=\"fetch('/api/display/hold/on',{{method:'POST'}}).then(()=>location.reload())\">Hold</button>\
<button onclick=\"fetch('/api/display/hold/off',{{method:'POST'}}).then(()=>location.reload())\">Release</button>\
</div>\
<div class=r><span class=k>Device</span>\
<button class=dgr onclick=\"fetch('/api/reboot',{{method:'POST'}});this.textContent='Rebooting…';this.disabled=1\">Reboot</button>\
</div>\
</div>").ok();

    // Footer
    write!(resp, "</div><div class=ft>\
<a href=/api/status>status</a> · \
<a href=/api/layout>layout</a> · \
<a href=/api/display>display</a> · \
<a href=/api/bitmap/stats>bitmap/stats</a>\
</div></body></html>").ok();
}

unsafe fn api_status(resp: &mut HttpResponse, ip: [u8; 4]) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();

    let m = &MAC_BYTES;
    let (w, len) = if !HUB75_PTR.is_null() { (*HUB75_PTR).get_img_param() } else { (0, 0) };
    let h = if w > 0 { len / w as u32 } else { 0 };
    let layout = if !LAYOUT_PTR.is_null() { &*LAYOUT_PTR } else { return; };
    let stats = if !BITMAP_STATS_PTR.is_null() { &*BITMAP_STATS_PTR } else { return; };
    let anim = if !ANIMATION_PTR.is_null() {
        match &*ANIMATION_PTR {
            crate::menu::Animation::None => "none",
            crate::menu::Animation::Rainbow { .. } => "rainbow",
        }
    } else { "unknown" };

    let isr_count = crate::ethernet::isr_count();
    let (mac_ovf, mac_pre, mac_crc) = if IFACE_INITIALIZED {
        IFACE.assume_init_ref().device().mac_errors()
    } else { (0, 0, 0) };

    write!(resp, r#"{{"mac":"{:02x}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}","#,
        m[0], m[1], m[2], m[3], m[4], m[5]).ok();
    write!(resp, r#""ip":"{}.{}.{}.{}","#, ip[0], ip[1], ip[2], ip[3]).ok();
    let (hw_cols, hw_rows, hw_scan, hw_cl2, hw_outs) = if !HUB75_PTR.is_null() {
        (*HUB75_PTR).get_hw_info()
    } else { (0, 0, 0, 0, 0) };
    write!(resp, r#""hw_columns":{},"hw_rows":{},"hw_scan":{},"hw_chain_length":{},"hw_outputs":{},"#,
        hw_cols, hw_rows, hw_scan, 1u8 << hw_cl2, hw_outs).ok();
    write!(resp, r#""display_width":{},"display_height":{},"#, w, h).ok();
    write!(resp, r#""grid":"{}x{}","virtual_width":{},"virtual_height":{},"#,
        layout.grid_cols, layout.grid_rows, layout.virtual_width(), layout.virtual_height()).ok();
    write!(resp, r#""panel_width":{},"panel_height":{},"#, layout.panel_width, layout.panel_height).ok();
    write!(resp, r#""animation":"{}","bitmap_frames":{},"#, anim, stats.frames_completed).ok();
    write!(resp, r#""isr_count":{},"isr_driven":true,"#, isr_count).ok();
    let (dbg_fast, dbg_slow, dbg_batch) = debug_counters();
    write!(resp, r#""fast_path":{},"slow_path":{},"max_batch":{},"#, dbg_fast, dbg_slow, dbg_batch).ok();
    write!(resp, r#""slow_arp":{},"slow_tcp":{},"slow_udp":{},"slow_other":{},"#,
        DBG_SLOW_ARP, DBG_SLOW_TCP, DBG_SLOW_UDP, DBG_SLOW_OTHER).ok();
    write!(resp, r#""mcast_dropped":{},"arp_dropped":{},"#, DBG_MULTICAST_DROPPED, DBG_ARP_DROPPED).ok();
    if !HUB75_PTR.is_null() {
        let (dpx, dch, dbad) = (*HUB75_PTR).dma_stats();
        // Read back what the hardware filters are actually matching against.
        let pp = litex_pac::Peripherals::steal();
        let f_mac_hi = pp.ethmac.mac_address0().read().bits();
        let f_mac_lo = pp.ethmac.mac_address1().read().bits();
        let f_ip = pp.ethmac.ip_address().read().bits();
        write!(resp, r#""dbg_mac":{},"dbg_ethertype":{},"#,
            ((pp.ethmac.dbg_mac1().read().bits() as u64) << 32)
                | (pp.ethmac.dbg_mac0().read().bits() as u64),
            pp.ethmac.dbg_ethertype().read().bits()).ok();
        write!(resp, r#""c_core":{},"c_split2":{},"c_gate_out":{},"c_narrow_out":{},"c_dropped":{},"#,
            pp.ethmac.c_core().read().bits(), pp.ethmac.c_split2().read().bits(),
            pp.ethmac.c_gate_out().read().bits(), pp.ethmac.c_narrow_out().read().bits(),
            pp.ethmac.c_dropped().read().bits()).ok();
        write!(resp, r#""n_gate":{},"n_narrow":{},"n_mac":{},"n_ip":{},"n_udp":{},"#,
            pp.ethmac.n_gate().read().bits(), pp.ethmac.n_narrow().read().bits(),
            pp.ethmac.n_mac().read().bits(), pp.ethmac.n_ip().read().bits(),
            pp.ethmac.n_udp().read().bits()).ok();
        write!(resp, r#""hw_filter_mac":{},"hw_filter_ip":{},"#,
            ((f_mac_lo as u64) << 32) | (f_mac_hi as u64), f_ip).ok();
        write!(resp, r#""dma_stalls":{},"#, (*HUB75_PTR).dma_stalls()).ok();
        write!(resp, r#""refresh_count":{},"dma_enabled":{},"dma_pixels":{},"dma_chunks":{},"dma_bad_magic":{},"#,
            (*HUB75_PTR).refresh_count(),
            if (*HUB75_PTR).dma_enabled() {1} else {0}, dpx, dch, dbad).ok();
    }
    write!(resp, r#""mac_overflow":{},"mac_crc_errors":{},"mac_preamble_errors":{},"#,
        mac_ovf, mac_crc, mac_pre).ok();
    // Crash breadcrumb: how far execution got before the PREVIOUS boot ended.
    let bc_prev = crate::breadcrumb::prev_raw();
    write!(resp, r#""prev_mark":{},"prev_mark_valid":{},"bc_now":{},"bc_count":{}"#,
        bc_prev & 0xFFFF,
        if crate::breadcrumb::previous(bc_prev).is_some() { 1 } else { 0 },
        crate::breadcrumb::raw() & 0xFFFF,
        crate::breadcrumb::count()).ok();
    write!(resp, r#","prev_mcause":{},"prev_mepc":{}"#,
        crate::breadcrumb::prev_mcause(), crate::breadcrumb::prev_mepc()).ok();
    // Flash status registers, sampled at boot. See flash_id::read_status_regs --
    // QE (SR2 bit 1) is the bit that can stop the board configuring from cold.
    let sr = &*core::ptr::addr_of!(FLASH_SR);
    write!(resp, r#","flash_sr1":{},"flash_sr2":{},"flash_sr3":{}}}"#,
        sr[0], sr[1], sr[2]).ok();
}

/// Measure achievable SDRAM write bandwidth, and what it costs the display.
///
/// Arms a hardware burst through LiteDRAMDMAWriter and reports the cycles it
/// took, alongside the refresh counter sampled either side. Words/cycle at the
/// system clock gives write bandwidth directly; the refresh delta gives the
/// price the display pays. This is the measurement that decides whether a
/// UDP-fed write DMA (Tier 2) has room to exist.
/// Turn the hardware pixel DMA on or off at runtime.
///
/// Lets the same binary be measured both ways: CPU writing pixels (the old
/// ~600 kpx/s path) versus gateware writing them straight to SDRAM.
/// Render one frame of the on-panel program.
///
/// Whole-frame redraw into the back buffer, then swap -- the same path the test
/// patterns use. A partial-redraw optimisation would be premature: the program
/// is a pure function of (x, y, frame, values), so there is nothing to diff
/// against until a program exists that is expensive enough to need it.
unsafe fn program_tick() {
    if TIME_MS < PROGRAM_NEXT_MS {
        return;
    }
    PROGRAM_NEXT_MS = TIME_MS + PROGRAM_PERIOD_MS;
    let hub75 = &mut *HUB75_PTR;
    let (w, len) = hub75.get_img_param();
    if w == 0 || len == 0 {
        return;
    }
    let w = w as usize;
    let h = len as usize / w;
    let frame = PROGRAM_FRAME;

    let ctx = crate::program::Ctx {
        w, h, frame, time_ms: TIME_MS,
        values: &*core::ptr::addr_of!(VALUES),
    };

    // Single flat closure over a linear index: a nested flat_map would have to
    // capture `ctx` by reference from an FnMut body, which cannot escape it.
    let total = w * h;
    let prog = PROGRAM_FN.unwrap_or(&crate::generated::PROGRAMS[0]);
    let render = prog.row;

    // The program owns the swap; the gateware must not also be doing it. See
    // set_pixel_filter() -- the player can turn auto_swap back on underneath us
    // at any time by re-enabling pixel DMA, so this is re-asserted, not set once.
    if hub75.auto_swap() {
        hub75.set_auto_swap(false);
    }

    // Palette: 256 words, and the cost does not grow with the wall. A program
    // animating through colour rather than pixels gets motion for about a
    // sixty-fourth of a redraw.
    //
    // A palette that does not read `ctx` is written ONCE. The palette is single
    // buffered (RB-5516) so every rewrite is scanned out while it happens, and
    // for a static palette that artifact buys nothing at all.
    if let Some(pf) = prog.palette {
        if !prog.palette_static {
            // Animated: this frame's colours belong to the buffer this frame is
            // drawn into, so they land in that buffer's bank and go live with it.
            hub75.set_palette(0, (0..256usize).map(|i| pf(&ctx, i)));
        } else if PROGRAM_PALETTE_DIRTY {
            // Static: written into EVERY bank, once. Filling only the back bank
            // would leave the other one black until the second frame, so the
            // panel would flash the right colours and then nothing.
            hub75.set_palette_all(0, |i| pf(&ctx, i));
            PROGRAM_PALETTE_DIRTY = false;
        }
    }

    // A value landing can change any row, and only the program knows where
    // it drew it -- so re-arm a full pair rather than guessing.
    if VALUES.writes != PROGRAM_SEEN_WRITES {
        PROGRAM_SEEN_WRITES = VALUES.writes;
        // Both halves owe the whole canvas; it gets paid off a slice at a time.
        REPAINT_POS = [0, 0];
        REPAINT_LEFT = [h, h];
    }
    let repainting = REPAINT_LEFT[0] > 0 || REPAINT_LEFT[1] > 0;
    // Nothing to do. A program with no `dynamic` band, no scroll and a static
    // palette is a pure function of its VALUES, so between pushes there is not
    // one pixel to change -- yet this redrew the whole canvas every period
    // forever, and swapped buffers each time. That is pure heat, and a swap the
    // viewer can catch mid-render is exactly what makes a slow program look
    // unsteady rather than merely slow.
    //
    // PROGRAM_FULL_LEFT is the "owed frames" counter: a value push sets it to 2
    // (one per buffer), which is what wakes this up again.
    let animated = !prog.scroll.is_empty()
        || (prog.palette.is_some() && !prog.palette_static)
        || PROGRAM_DYNAMIC.is_some()
        || repainting;
    if PROGRAM_FULL_LEFT == 0 && !animated {
        return;
    }
    // Count frames DRAWN, not ticks taken. The counter is what /api/programs
    // reports and what a session reads to decide whether a program is alive, so
    // counting skipped ticks made an idle program look like it was running at
    // the tick rate -- 27 fps for something that renders three times a second.
    PROGRAM_FRAME = PROGRAM_FRAME.wrapping_add(1);

    // The per-frame band is now just what the program declared. A value arrival
    // no longer widens it to the whole canvas -- that is the progressive
    // repaint's job. PROGRAM_FULL_LEFT still forces whole frames while a
    // program is STARTING, when there is nothing on the glass to converge from.
    let band = match (PROGRAM_DYNAMIC, PROGRAM_FULL_LEFT) {
        (Some(b), 0) => b,
        _ => (0, h),
    };
    if PROGRAM_FULL_LEFT > 0 {
        PROGRAM_FULL_LEFT -= 1;
    }
    // Render OUTSIDE the critical section. The back buffer is not being scanned
    // out, so writing it needs no interrupt protection -- and holding interrupts
    // across a full 128x128 render at 30fps starved the network stack so badly
    // the HTTP server stopped answering entirely. Only the swap is atomic with
    // respect to the display.
    //
    // Row at a time, straight into the buffer: no iterator, and one indirect
    // call per row instead of one per pixel.
    let _ = total;
    // Scroll: shift what is on the glass and let the program fill only what
    // that exposed. ~2 memory accesses per pixel (one read, one write) instead
    // of the ~5 a re-render costs, and the expensive glyph lookups shrink to the
    // few columns actually uncovered.
    //
    // Exact, not approximate: the contract is that the content is a pure
    // function of `x + frame * speed`, so last frame's pixel at x+speed IS this
    // frame's pixel at x. Copy front -> back, never shift the back buffer, which
    // holds the frame from two swaps ago rather than what is displayed.
    let scrolling = !prog.scroll.is_empty() && PROGRAM_PRIME_LEFT == 0;
    if PROGRAM_PRIME_LEFT > 0 {
        PROGRAM_PRIME_LEFT -= 1;
    }
    if scrolling {
        for &(sy0, sy1, speed, right) in prog.scroll {
            if speed == 0 || speed >= w {
                continue;
            }
            let (y0, y1) = (sy0.min(h), sy1.min(h));
            {
                let (front, back) = hub75.buffers();
                for y in y0..y1 {
                    let base = y * w;
                    if base + w > back.len() || base + w > front.len() {
                        break;
                    }
                    if right {
                        // Content moves right: this frame's x is last frame's
                        // x-speed, so walk down to avoid overwriting sources.
                        for x in (speed..w).rev() {
                            back[base + x] = front[base + x - speed];
                        }
                    } else {
                        for x in 0..(w - speed) {
                            back[base + x] = front[base + x + speed];
                        }
                    }
                }
            }
            // The fill callback: the program's OWN per-pixel code, asked only
            // about the columns the shift uncovered.
            let buf = hub75.back_buffer();
            let x0 = if right { 0 } else { w - speed };
            for y in y0..y1 {
                let start = y * w + x0;
                if start + speed > buf.len() {
                    break;
                }
                render(&ctx, y, x0, &mut buf[start..start + speed]);
            }
        }
    }

    // Rows no scroll band covers still need drawing normally.
    let in_scroll = |y: usize| scrolling && prog.scroll.iter().any(|&(a, b, _, _)| y >= a && y < b);
    {
        let buf = hub75.back_buffer();
        let (y0, y1) = (band.0.min(h), band.1.min(h));
        for y in y0..y1 {
            if in_scroll(y) {
                continue;
            }
            let start = y * w;
            if start + w > buf.len() {
                break;
            }
            render(&ctx, y, 0, &mut buf[start..start + w]);
        }
    }

    // Pay off this buffer's share of the owed repaint -- at most REPAINT_SLICE
    // rows, so the frame stays the length of a frame.
    if REPAINT_LEFT[0] > 0 || REPAINT_LEFT[1] > 0 {
        let bi = hub75.back_index();
        let buf = hub75.back_buffer();
        let mut budget = REPAINT_SLICE;
        while budget > 0 && REPAINT_LEFT[bi] > 0 {
            let y = REPAINT_POS[bi];
            REPAINT_POS[bi] += 1;
            REPAINT_LEFT[bi] -= 1;
            // A scroll band costs nothing to skip: the shift already carries
            // its content into both halves every frame.
            if in_scroll(y) || y >= h {
                continue;
            }
            // Rows the declared band already drew this frame are done.
            if y >= band.0 && y < band.1 {
                continue;
            }
            let start = y * w;
            if start + w > buf.len() {
                break;
            }
            render(&ctx, y, 0, &mut buf[start..start + w]);
            budget -= 1;
        }
    }

    without_interrupts(|| {
        // Re-check the flag we were gated on. bitmap_tick() tested PROGRAM_ACTIVE
        // before the render loop above, and that loop walks every pixel -- ~16k
        // at 128x128. An ISR landing in that window (a pattern POST, say) can
        // hand the display to something else, and without this re-check we would
        // then assert INDEXED and swap our own buffer over the top of it.
        //
        // Observed: POSTing /api/display/pattern while a program ran left the
        // mode at "indexed" every time, so the pattern's fullcolor words were
        // read as palette indices against the program's ramp. It looked like a
        // broken colour map rather than two things fighting over the display,
        // and it reproduced 3 times out of 3 because the render loop is long.
        if !PROGRAM_ACTIVE {
            return;
        }
        // INDEXED, not full colour: a program returns palette indices so that an
        // anti-aliased glyph or icon is `ramp_base + coverage` -- one add, no
        // blend -- and recolouring is 16 palette words rather than a repaint.
        hub75.set_mode(crate::hub75::OutputMode::Indexed);
        hub75.swap_buffers();
        hub75.on();
    });
}

unsafe fn api_program_set(req: &HttpRequest, resp: &mut HttpResponse, on: bool) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    if on {
        // {"name":"dashboard"} selects; absent keeps whatever is already chosen.
        if let Some(n) = json_get_str(req.body_str(), "name") {
            if let Some(pr) = crate::generated::by_name(n) {
                PROGRAM_FN = Some(pr);
                PROGRAM_DYNAMIC = pr.dynamic;
                PROGRAM_NAME = pr.name;
            }
        }
        if PROGRAM_FN.is_none() {
            PROGRAM_FN = Some(&crate::generated::PROGRAMS[0]);
            PROGRAM_DYNAMIC = crate::generated::PROGRAMS[0].dynamic;
            PROGRAM_NAME = crate::generated::PROGRAMS[0].name;
        }
        // Owe two full frames on EVERY activation, not just when a value
        // arrives. Without this, selecting a program drew only its dynamic band
        // until a value happened to land -- the panel came up with a correct
        // gauge on an otherwise black screen, which looks like a broken program
        // rather than a missing full redraw.
        PROGRAM_FULL_LEFT = 2;
        PROGRAM_PRIME_LEFT = 2;
        PROGRAM_PALETTE_DIRTY = true;
    }
    PROGRAM_ACTIVE = on;
    // Leaving program mode hands the swap back to whatever the pixel path is
    // doing; entering it takes the swap away from the gateware immediately
    // rather than at the first tick.
    if !HUB75_PTR.is_null() {
        let filt = litex_pac::Peripherals::steal().ethmac.pixel_filter_enable().read().bits() != 0;
        (*HUB75_PTR).set_auto_swap(filt && !on);
    }
    // A program owns the framebuffer; nothing else may write it -- which means
    // the hardware DMA too, not just the CPU parser.
    set_stream_hold(on);
    if on {
        PROGRAM_FRAME = 0;
        PROGRAM_NEXT_MS = 0;
        // Install the program palette: flat colours low, then the 16-entry
        // ramps that make anti-aliased assets free to draw.
        (*HUB75_PTR).set_palette_all(0, crate::program::palette_default);
        // A streamed palette would otherwise be left in place and every index
        // would resolve to the wrong colour.
        BITMAP_RX.assume_init_mut().invalidate_mode_cache();
    }
    write!(resp, r#"{{"program":{},"name":"{}","source":"{}","available":{}}}"#,
        if on { "true" } else { "false" }, PROGRAM_NAME, display_source(),
        crate::generated::PROGRAMS.len()).ok();
}

/// What is actually driving the panel right now.
///
/// The counters could always tell you this, but only if you knew which three to
/// cross-reference. Naming the source outright is the difference between "is it
/// streaming or is it stuck?" taking a second or taking an evening.
unsafe fn display_source() -> &'static str {
    if BITMAP_STATS_PTR.is_null() {
        return "unknown";
    }
    if PROGRAM_ACTIVE {
        return "program (on-panel)";
    }
    let rx = BITMAP_RX.assume_init_ref();
    if rx.is_held() {
        return "held (native pattern)";
    }
    if rx.streaming_recently(TIME_MS) {
        return "streaming";
    }
    if !ANIMATION_PTR.is_null() {
        if let crate::menu::Animation::Rainbow { .. } = &*ANIMATION_PTR {
            return "animation";
        }
    }
    "idle (no frames)"
}

/// Walk a FLAT json object, storing every pair. Deliberately a scanner and not
/// a parser: the payload shape is fixed ({"k":"v", "n":12}), there is no
/// allocator, and this runs in the network ISR. Nested objects are skipped
/// rather than rejected, so a caller adding structure later degrades to
/// ignoring it instead of dropping the whole push.
unsafe fn api_values_set(req: &HttpRequest, resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();

    let body = req.body_str();
    let b = body.as_bytes();
    let len = b.len();
    let mut i = 0usize;
    let mut stored = 0u32;
    let mut depth = 0i32;

    while i < len {
        match b[i] {
            b'{' => { depth += 1; i += 1; }
            b'}' => { depth -= 1; i += 1; }
            b'"' if depth == 1 => {
                let ks = i + 1;
                let mut j = ks;
                while j < len && b[j] != b'"' { j += 1; }
                if j >= len { break; }
                let key = core::str::from_utf8(&b[ks..j]).unwrap_or("");
                let mut p = j + 1;
                while p < len && (b[p] == b' ' || b[p] == b':') { p += 1; }
                if p >= len { break; }
                if b[p] == b'"' {
                    let vs = p + 1;
                    let mut q = vs;
                    while q < len && b[q] != b'"' { q += 1; }
                    if q >= len { break; }
                    let val = core::str::from_utf8(&b[vs..q]).unwrap_or("");
                    VALUES.set(key, val, TIME_MS);
                    stored += 1;
                    i = q + 1;
                } else {
                    // bare number / true / false -- take it verbatim to the delimiter
                    let vs = p;
                    let mut q = vs;
                    while q < len && b[q] != b',' && b[q] != b'}' { q += 1; }
                    let val = core::str::from_utf8(&b[vs..q]).unwrap_or("").trim();
                    VALUES.set(key, val, TIME_MS);
                    stored += 1;
                    i = q;
                }
            }
            _ => { i += 1; }
        }
    }
    write!(resp, r#"{{"stored":{},"keys":{},"writes":{}}}"#,
        stored, VALUES.len(), VALUES.writes).ok();
}

unsafe fn api_values_get(resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    write!(resp, r#"{{"stale_ms":{},"values":{{"#, VALUES.stale_for_ms(TIME_MS)).ok();
    let mut first = true;
    for v in VALUES.iter() {
        if !first { resp.data.extend_from_slice(b",").ok(); }
        first = false;
        write!(resp, r#""{}":{{"v":"{}","age_ms":{}}}"#,
            v.key(), v.value(), TIME_MS.saturating_sub(v.updated_ms)).ok();
    }
    resp.data.extend_from_slice(b"}}").ok();
}

/// Hold (or release) the incoming pixel stream -- BOTH halves of it.
///
/// `BitmapReceiver::set_hold` only stops the CPU parser. The gateware pixel DMA
/// takes udp_sink directly and keeps writing the framebuffer regardless, so a
/// panel that reported itself "held" was still being overwritten ~10 times a
/// second while frames_completed sat at 0 -- a CPU-side counter cannot see a
/// hardware write. An on-panel program drawing into the same back buffer then
/// fought the stream for it, which looks like corruption rather than like two
/// writers.
///
/// It only ever appeared to work straight after a flash, because DMA defaults
/// off at boot until marquee turns it on.
unsafe fn set_stream_hold(on: bool) {
    BITMAP_RX.assume_init_mut().set_hold(on);
    if !HUB75_PTR.is_null() {
        // Held means held: nothing else may write the framebuffer.
        (*HUB75_PTR).set_dma_enabled(!on);
    }
}

unsafe fn api_hold_set(resp: &mut HttpResponse, on: bool) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    set_stream_hold(on);
    write!(resp, r#"{{"hold":{},"source":"{}"}}"#,
        if on { "true" } else { "false" }, display_source()).ok();
}

unsafe fn api_dma_set(resp: &mut HttpResponse, on: bool) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    if !HUB75_PTR.is_null() {
        (*HUB75_PTR).set_dma_enabled(on);
        let (px, ch, bad) = (*HUB75_PTR).dma_stats();
        write!(resp, r#"{{"dma_enabled":{},"pixels":{},"chunks":{},"bad_magic":{}}}"#,
            if on {1} else {0}, px, ch, bad).ok();
    } else {
        write!(resp, r#"{{"error":"no hub75"}}"#).ok();
    }
}

/// What the receive ISR actually costs, per entry and per packet.
///
/// The 19,800 cycles/packet in RB-5486 is a derived figure from 2026-09-09 and
/// has never been measured directly. With the pixel DMA on the CPU writes no
/// pixels at all (bitmap_udp.rs guards the write loop on dma_enabled), so that
/// cost is NOT payload copying -- it is whatever the ISR does around a 10-byte
/// header read. Worth knowing precisely before spending a gateware classifier
/// on it.
unsafe fn api_isr_prof(resp: &mut HttpResponse, reset: bool) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    // Where do the cycles actually go? DRAM reads measured 14 cycles/byte and a
    // CSR read 8, neither of which explains a 6,373-cycle process_packet. The
    // one region never timed is the MAC's own packet buffer, which is what
    // peek_rx() hands out a slice of -- every header byte the ISR reads comes
    // from here, over the wishbone, with no D-cache in front of it.
    const N: usize = 1024;
    let eth = crate::ethernet::ETHMEM_BASE;
    let ram = 0x9000_0000usize;
    let t0 = prof_ticks();
    let mut a: u32 = 0;
    for i in 0..N {
        a = a.wrapping_add(core::ptr::read_volatile((eth + i) as *const u8) as u32);
    }
    let eth_cy = prof_delta(t0, prof_ticks());
    let t0 = prof_ticks();
    let mut b: u32 = 0;
    for i in 0..N {
        b = b.wrapping_add(core::ptr::read_volatile((ram + i) as *const u8) as u32);
    }
    let ram_cy = prof_delta(t0, prof_ticks());

    let (cy, en, pk) = (ISR_CYCLES, ISR_ENTRIES, ISR_PACKETS);
    let per_entry = if en > 0 { cy / en as u64 } else { 0 };
    let per_packet = if pk > 0 { cy / pk as u64 } else { 0 };
    write!(
        resp,
        r#"{{"isr_cycles":{},"isr_entries":{},"isr_packets":{},"cycles_per_entry":{},"cycles_per_packet":{},"us_per_packet":{},"sys_clk_hz":{},"ethmem_cycles_per_byte":{},"dram_cycles_per_byte":{},"chk":{}}}"#,
        cy, en, pk, per_entry, per_packet,
        per_packet / (SYS_CLK_HZ as u64 / 1_000_000),
        SYS_CLK_HZ,
        eth_cy / N as u32,
        ram_cy / N as u32,
        a ^ b
    )
    .ok();
    if reset {
        ISR_CYCLES = 0;
        ISR_ENTRIES = 0;
        ISR_PACKETS = 0;
    }
}

/// The raw DMA arrival bitmap, so a missing chunk can be named rather than counted.
unsafe fn api_dma_arrival(resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    if HUB75_PTR.is_null() {
        write!(resp, r#"{{"error":"no hub75"}}"#).ok();
        return;
    }
    let hub75 = &*HUB75_PTR;
    let Some((words, fid)) = hub75.dma_arrival() else {
        write!(resp, r#"{{"error":"dma off"}}"#).ok();
        return;
    };
    let (_, total, indexed) = hub75.dma_frame_identity().unwrap_or((0, 0, false));
    let mut set = 0u32;
    for w in words.iter() {
        set += w.count_ones();
    }
    write!(resp, r#"{{"frame_id":{},"total_chunks":{},"indexed":{},"bits_set":{},"missing":["#,
        fid, total, if indexed {1} else {0}, set).ok();
    let mut first = true;
    for i in 0..(total as usize) {
        let w = words[i >> 5];
        if w & (1u32 << (i & 31)) == 0 {
            if !first { write!(resp, ",").ok(); }
            write!(resp, "{}", i).ok();
            first = false;
        }
    }
    write!(resp, "],").ok();
    // The gateware's LATCHED frame, and the guards present_done_frame() returns
    // on. Kept because the alternative is a firmware flash every time the
    // question comes up: with the pixel filter on, the CPU sees no pixel
    // packets, so `frames_completed` standing still is indistinguishable from
    // a healthy card until you can see WHICH guard is short-circuiting.
    //
    // Read it as a pair. `done.seq` advancing while `rx.last_done_seq` does not
    // means the gateware is finishing frames the CPU never adopts, and
    // `cpu_saw_packet` or `hw_filter_on` says why. `done: null` means the DMA
    // is off. `pending_palette` stuck at 1 means a palette is stashed that
    // nothing is applying -- see RB-5516.
    match hub75.dma_done_frame() {
        Some((_, dfid, dtot, didx, dseq)) => {
            write!(resp, r#""done":{{"seq":{},"frame_id":{},"total_chunks":{},"indexed":{}}},"#,
                dseq, dfid, dtot, if didx {1} else {0}).ok();
        }
        None => { write!(resp, r#""done":null,"#).ok(); }
    }
    let rx = BITMAP_RX.assume_init_ref();
    write!(resp, r#""rx":{{"last_done_seq":{},"cpu_saw_packet":{},"hw_filter_on":{},"pending_palette":{}}}}}"#,
        rx.dbg_last_done_seq(), if rx.dbg_cpu_saw_packet() {1} else {0},
        if rx.dbg_hw_filter_on() {1} else {0},
        if rx.dbg_pending_palette() {1} else {0}).ok();
}

/// Try a channel order live, without a reboot or a config round trip.
///
/// Finding the right order is a LOOK-AND-SEE job -- you change it, glance at
/// the panel, and change it again -- so making each attempt cost a TFTP config
/// edit and a reboot would make a two-minute task into a long one. This is the
/// knob for finding the answer; `rgb_order` in the card's config is where the
/// answer gets recorded so it survives a power cycle.
unsafe fn api_rgb_order(req: &HttpRequest, resp: &mut HttpResponse) {
    use core::fmt::Write;
    // Plain text body ("GRB"), or {"order":"GRB"} if something prefers JSON.
    let raw = req.body_str();
    let want = json_get_str(raw, "order").unwrap_or_else(|| raw.trim());
    let idx = crate::layout::RGB_ORDERS
        .iter()
        .position(|o| o.eq_ignore_ascii_case(want))
        .or_else(|| want.parse::<usize>().ok().filter(|n| *n < 6));
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    match (idx, HUB75_PTR.is_null()) {
        (_, true) => { write!(resp, r#"{{"error":"no hub75"}}"#).ok(); }
        (None, _) => {
            write!(resp, r#"{{"error":"unknown order","want":["RGB","RBG","GRB","GBR","BRG","BGR"]}}"#).ok();
        }
        (Some(i), false) => {
            (*HUB75_PTR).set_rgb_order(i as u8);
            write!(resp, r#"{{"rgb_order":{},"name":"{}"}}"#, i, crate::layout::RGB_ORDERS[i]).ok();
        }
    }
}

unsafe fn api_rgb_order_get(resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    if HUB75_PTR.is_null() {
        write!(resp, r#"{{"error":"no hub75"}}"#).ok();
        return;
    }
    let i = (*HUB75_PTR).rgb_order() as usize;
    write!(resp, r#"{{"rgb_order":{},"name":"{}"}}"#, i,
        crate::layout::RGB_ORDERS.get(i).copied().unwrap_or("?")).ok();
}

/// Turn the gateware pixel filter on or off, and keep the receiver in step.
///
/// The CSR resets to 0 on FPGA configuration, so the BIOS always netboots with
/// the filter off however this firmware behaves -- which is what makes enabling
/// it at boot recoverable with a power cycle.
pub unsafe fn set_pixel_filter(on: bool) {
    let p = litex_pac::Peripherals::steal();
    p.ethmac.pixel_filter_enable().write(|w| w.bits(if on { 1 } else { 0 }));
    // Hardware swap goes with it: once the CPU is out of the pixel path it is
    // also too slow to place the buffer flip, which shows as a band of the next
    // frame across the top of the image.
    //
    // NOT while a program is running, though. A program composes a whole frame
    // into the back buffer and swaps it itself when it is complete; auto_swap
    // hands the flip to the gateware, which knows nothing about that and flips
    // mid-render. The panel then alternates between the finished frame and a
    // half-drawn one -- which reads as flicker and smeared text, not as tearing.
    // marquee's player re-enables pixel DMA on any card it believes is
    // streaming, so this is reached with a program active as a matter of course.
    if !HUB75_PTR.is_null() {
        (*HUB75_PTR).set_auto_swap(on && !PROGRAM_ACTIVE);
    }
    let rx = BITMAP_RX.assume_init_mut();
    rx.set_hw_filter(on);
    if on {
        rx.forget_frame();
    }
}

/// The real thing: the gateware kills pixel packets before the CPU's MAC sees them.
///
/// `SmolEthPixelClassifier` watches the CPU branch and, at beat 9, decides
/// whether the packet is UDP to the pixel port. `SmolEthInvalidator` then
/// terminates the frame with error=0xFF, which makes LiteEthMACSRAMWriter take
/// its DISCARD path -- no stat_fifo push, so no receive event, so no interrupt,
/// and the slot is reusable immediately.
///
/// DEFAULT OFF in the gateware, and deliberately so. A wrong verdict takes out
/// ARP, HTTP, telnet and the BIOS's TFTP netboot, leaving a panel that can only
/// be recovered over JTAG. Turning it on from here means a power cycle undoes
/// any mistake, because the CSR resets to 0.
unsafe fn api_hw_filter(resp: &mut HttpResponse, set: Option<bool>) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    let p = litex_pac::Peripherals::steal();
    if let Some(on) = set {
        // The CPU is about to stop seeing pixel packets, so anything it half
        // built from them is stale and would block adopt_dma_frame().
        set_pixel_filter(on);
    }
    write!(
        resp,
        r#"{{"enabled":{},"port":{},"killed":{}}}"#,
        p.ethmac.pixel_filter_enable().read().bits(),
        p.ethmac.pixel_filter_port().read().bits(),
        p.ethmac.pixel_filter_killed().read().bits()
    )
    .ok();
}

/// Make the CPU ignore pixel packets, simulating the RB-5486 gateware filter.
///
/// Off means the CPU drops every 'B','M' and 'B','I' packet the moment it sees
/// the magic, as if `SmolEthInvalidator` had killed the frame before the MAC
/// raised an interrupt. The display then runs entirely on the gateware's
/// evidence: `adopt_dma_frame()` for frame identity and `merge_dma_arrival()`
/// for the chunk bitmap.
///
/// This exists to prove the filtered path in firmware -- deployable over the
/// network, reversible with one POST -- before committing to a gateware
/// classifier whose failure mode is a panel that cannot be reached at all.
/// Palette packets are spared, because the palette is the CPU's job.
unsafe fn api_cpu_pixels(resp: &mut HttpResponse, on: bool) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    let rx = &mut *BITMAP_RX.as_mut_ptr();
    rx.set_cpu_pixels(on);
    let hw = if !HUB75_PTR.is_null() {
        (*HUB75_PTR).dma_frame_identity()
    } else {
        None
    };
    let (fid, tot, idx) = hw.unwrap_or((0, 0, false));
    write!(
        resp,
        r#"{{"cpu_pixels":{},"dma_frame_id":{},"dma_total_chunks":{},"dma_indexed":{}}}"#,
        if on { "true" } else { "false" },
        fid, tot, if idx { 1 } else { 0 }
    )
    .ok();
}

/// Dump the FRONT buffer -- what is actually being scanned out right now.
///
/// Exists because every other signal on this panel reports INTENT: /api/display
/// says which mode was requested, frames_completed counts what the CPU saw. When
/// the glass disagrees with all of them there is nothing left to read, and the
/// alternative is guessing and reflashing, which is expensive and was wrong
/// three times running.
///
/// Sampled every STEP pixels so a 128x128 frame fits the response buffer: the
/// low byte of each word, which is the palette index in indexed mode and the
/// blue channel in full colour.
/// The programs this firmware carries, and which is running.
///
/// One firmware holds every program and the config picks which runs, so the
/// list is a property of the BUILD, not of the panel's configuration. marquee
/// asks the panel rather than being told, for the same reason it asks for the
/// mode: two places recording the same fact is how they drift.
unsafe fn api_programs(resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    // `frame` is the render counter: sample it twice to get the ACHIEVED frame
    // rate. A program that cannot hit its target rate scrolls in bigger jumps,
    // and there is otherwise no way to tell that from the outside.
    write!(resp, r#"{{"active":"{}","running":{},"frame":{},"period_ms":{},"programs":["#,
        PROGRAM_NAME, if PROGRAM_ACTIVE { "true" } else { "false" },
        PROGRAM_FRAME, PROGRAM_PERIOD_MS).ok();
    for (i, p) in crate::generated::PROGRAMS.iter().enumerate() {
        if i > 0 {
            resp.data.extend_from_slice(b",").ok();
        }
        write!(resp, r#""{}""#, p.name).ok();
    }
    resp.data.extend_from_slice(b"]}").ok();
}

/// Time the two halves of a frame: filling the framebuffer, and running the
/// program that decides what goes in it.
///
/// Exists because "the program is slow" is not actionable. `blank` already
/// showed that a program drawing NOTHING caps near 6 fps, so the cost is the
/// write path -- but a full-screen write should be ~16k bus transactions, which
/// at the documented ~17 cycles is milliseconds, not the ~160ms observed. This
/// says which it is. Run it with the display ON and OFF: if the fill is much
/// faster with the panel dark, the CPU is competing with HUB75 scan-out for
/// SDRAM, and the answer is to write FEWER WORDS rather than to write faster.
/// Per-ROW cost of the running program. A profiler, not a benchmark.
///
/// `/api/bench` asks "how fast is this panel"; this asks "WHERE is the time
/// going", which is the question that matters when a program is inexplicably
/// slow. Rows differ in which widgets they can hit, so a per-row profile names
/// the expensive band without a symbol-level tool, which this target has none of.
///
/// Two things force the shape of this code:
///
/// 1. **`mcycle` does not exist here.** `csrr 0xB00` assembles and executes but
///    always yields 0 on this VexRiscv build -- the same reason `main()` notes
///    that reading back `mtvec` gives 0. Every number `/api/bench` has ever
///    printed was that zero. So the only timebase is `TIME_MS`.
/// 2. **`TIME_MS` is frozen inside the trap handler**, where HTTP is serviced.
///    So the measurement cannot happen in the request. `/api/profile` queues a
///    job; the main loop runs it (interrupts on, tick advancing) and a second
///    GET collects it.
///
/// Resolution comes from repeating each row until it exceeds RESOLUTION_MS, so
/// a cheap row is measured just as accurately as an expensive one.
const PROFILE_MAX_ROWS: usize = 256;
const PROFILE_RESOLUTION_MS: u32 = 20;
/// Set when the palette must be re-sent: a program was selected or changed.
/// Frames still owed a repaint of the SCROLL BANDS as well.
///
/// Separate from PROGRAM_FULL_LEFT on purpose. A value arrival owes a full
/// repaint of the rows the program draws from values -- but NOT of a scroll
/// band, whose content is a pure function of `x + frame*speed` and which the
/// shift already propagates into both buffers every frame. Repainting the
/// bands too meant every push cost two full-canvas frames, and on a program
/// whose bands are two lines of 26px text that is ~250ms each: the frame rate
/// dropped from 32 fps to 16 once every ten seconds, which reads as the sign
/// pausing.
///
/// Activation is different: the buffers hold nothing yet, so there is nothing
/// for the shift to propagate and the bands must be painted outright.
static mut PROGRAM_PRIME_LEFT: u8 = 0;

/// PROGRESSIVE REPAINT: rows still owed to each framebuffer half, and where to
/// resume.
///
/// A value arrival changes rows the program draws from values, and only the
/// program knows which -- so the whole non-scroll canvas is owed a repaint. Done
/// in one frame that is ~75ms of work on top of a ~31ms frame, twice (once per
/// buffer), and the sign visibly hesitates once per push.
///
/// So it is spread. Each tick paints at most REPAINT_SLICE rows of whatever the
/// BACK buffer still owes, and the rest of the frame proceeds normally. The
/// canvas converges over a few hundred milliseconds instead of stopping for a
/// tenth of a second.
///
/// Indexed by buffer, because the two alternate: painting "the back buffer"
/// without tracking which one it is would fill one twice and leave the other
/// showing stale rows for ever.
///
/// The cost of spreading it is that a changed value appears as a wipe down the
/// panel rather than all at once. At this size that reads as a deliberate
/// transition; a hitch never does.
const REPAINT_SLICE: usize = 12;
static mut REPAINT_POS: [usize; 2] = [0, 0];
static mut REPAINT_LEFT: [usize; 2] = [0, 0];
static mut PROGRAM_PALETTE_DIRTY: bool = true;
static mut PROFILE_STATE: u8 = 0;
/// Which job is queued: 0 = per-row profile, 1 = whole-frame bench.
static mut PROFILE_KIND: u8 = 0;
static mut BENCH_FILL_NS: u32 = 0;
static mut BENCH_PROG_NS: u32 = 0;
static mut BENCH_PAL_NS: u32 = 0; // 0 idle, 1 queued, 2 running, 3 done
static mut PROFILE_US: [u32; PROFILE_MAX_ROWS] = [0; PROFILE_MAX_ROWS];
static mut PROFILE_H: usize = 0;
static mut PROFILE_W: usize = 0;
static mut PROFILE_BASE_US: u32 = 0;
static mut PROFILE_NAME: [u8; 32] = [0; 32];
static mut PROFILE_VAL_NS: u32 = 0;
static mut PROFILE_COVER_NS: u32 = 0;
static mut PROFILE_LH_NS: u32 = 0;
static mut PROFILE_SINK: u32 = 0;

/// Time one closure to +/-1ms by repeating it, and report nanoseconds per call.
///
/// Returns ns rather than us because a bare row fill is a few microseconds and
/// integer-dividing to us would floor the baseline to 0 -- which is exactly the
/// comparison the whole profile exists to make.
unsafe fn profile_time_ns(mut f: impl FnMut()) -> u32 {
    let mut n: u32 = 1;
    loop {
        let t0 = TIME_MS;
        for _ in 0..n { f(); }
        let dt = TIME_MS.wrapping_sub(t0).max(0) as u64;
        if dt >= PROFILE_RESOLUTION_MS as u64 || n >= 1 << 20 {
            return ((dt * 1_000_000) / n as u64) as u32;
        }
        n = n.saturating_mul(8);
    }
}

/// Best of three. A single sample is timed with interrupts ON -- TIME_MS
/// cannot advance otherwise -- so it can absorb a burst of network ISR work
/// that has nothing to do with what is being measured. The floor is the truth.
unsafe fn profile_best_ns(mut f: impl FnMut()) -> u32 {
    let mut best = u32::MAX;
    for _ in 0..3 {
        let v = profile_time_ns(&mut f);
        if v < best { best = v; }
    }
    best
}

/// Run the queued profile. Called from the main loop, NOT the trap handler.
pub unsafe fn profile_run() {
    if PROFILE_STATE != 1 || HUB75_PTR.is_null() { return; }
    PROFILE_STATE = 2;
    if PROFILE_KIND == 1 {
        bench_run();
        PROFILE_STATE = 3;
        return;
    }
    let hub75 = &mut *HUB75_PTR;
    let (w, len) = hub75.get_img_param();
    let (w, len) = (w as usize, len as usize);
    if w == 0 || len == 0 { PROFILE_STATE = 0; return; }
    let h = core::cmp::min(len / w, PROFILE_MAX_ROWS);
    PROFILE_W = w;
    PROFILE_H = h;

    let n = core::cmp::min(PROGRAM_NAME.len(), PROFILE_NAME.len());
    PROFILE_NAME = [0; 32];
    PROFILE_NAME[..n].copy_from_slice(&PROGRAM_NAME.as_bytes()[..n]);

    // Baseline: what one row costs to simply WRITE, drawing nothing. "Slow"
    // only means something measured against writing the pixels at all.
    PROFILE_BASE_US = profile_time_ns(|| {
        let buf = (&mut *HUB75_PTR).back_buffer();
        for p in buf[0..w].iter_mut() { core::ptr::write_volatile(p, 0); }
    });

    // Micro-costs of the two primitives the generated code calls per pixel.
    // The per-row numbers say WHERE; these say WHAT, so the fix is chosen by
    // measurement instead of by reading the generated code and guessing.
    {
        let ctx = crate::program::Ctx {
            w, h, frame: PROGRAM_FRAME, time_ms: TIME_MS,
            values: &*core::ptr::addr_of!(VALUES),
        };
        PROFILE_VAL_NS = profile_time_ns(|| {
            let v = ctx.val(core::hint::black_box("page"));
            core::ptr::write_volatile(core::ptr::addr_of_mut!(PROFILE_SINK), v.len() as u32);
        });
        PROFILE_COVER_NS = profile_time_ns(|| {
            let c = crate::assets::PROFILE_FONT.cover_at(
                core::hint::black_box("DASHBOARD"), core::hint::black_box(20), 8, 6, 4);
            core::ptr::write_volatile(core::ptr::addr_of_mut!(PROFILE_SINK), c.unwrap_or(0) as u32);
        });
        PROFILE_LH_NS = profile_time_ns(|| {
            let lh = core::ptr::read_volatile(core::ptr::addr_of!(crate::assets::PROFILE_FONT.line_height));
            core::ptr::write_volatile(core::ptr::addr_of_mut!(PROFILE_SINK), lh as u32);
        });
    }

    let prog = match PROGRAM_FN { Some(p) => p, None => { PROFILE_STATE = 3; return; } };
    let render = prog.row;
    for y in 0..h {
        PROFILE_US[y] = profile_best_ns(|| {
            let hub75 = &mut *HUB75_PTR;
            let ctx = crate::program::Ctx {
                w, h, frame: PROGRAM_FRAME, time_ms: TIME_MS,
                values: &*core::ptr::addr_of!(VALUES),
            };
            let buf = hub75.back_buffer();
            let start = y * w;
            render(&ctx, y, 0, &mut buf[start..start + w]);
        });
    }
    PROFILE_STATE = 3;
}

unsafe fn api_profile(resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    match PROFILE_STATE {
        0 => {
            if !PROGRAM_ACTIVE {
                write!(resp, r#"{{"status":"idle","error":"no program running"}}"#).ok();
                return;
            }
            PROFILE_KIND = 0;
            PROFILE_STATE = 1;
            write!(resp, r#"{{"status":"queued"}}"#).ok();
        }
        1 | 2 => { write!(resp, r#"{{"status":"running"}}"#).ok(); }
        _ => {
            let name = core::str::from_utf8(
                &PROFILE_NAME[..PROFILE_NAME.iter().position(|&c| c == 0).unwrap_or(0)]
            ).unwrap_or("?");
            let (w, h) = (PROFILE_W, PROFILE_H);
            write!(resp, r#"{{"status":"done","program":"{}","w":{},"h":{},"baseline_row_ns":{},"val_ns":{},"cover_ns":{},"line_height_ns":{},"rows_ns":["#,
                   name, w, h, PROFILE_BASE_US, PROFILE_VAL_NS, PROFILE_COVER_NS, PROFILE_LH_NS).ok();
            let mut total: u64 = 0;
            let mut worst: (usize, u32) = (0, 0);
            for y in 0..h {
                let c = PROFILE_US[y];
                total += c as u64;
                if c > worst.1 { worst = (y, c); }
                if y > 0 { write!(resp, ",").ok(); }
                write!(resp, "{}", c).ok();
            }
            let per_px = if w * h > 0 { total / (w * h) as u64 } else { 0 };
            write!(resp, r#"],"frame_us":{},"ns_per_px":{},"worst_row":{},"worst_ns":{}}}"#,
                   total / 1000, per_px, worst.0, worst.1).ok();
            PROFILE_STATE = 0;
        }
    }
}

/// Whole-frame costs: a bare fill, the active program, and a palette rewrite.
///
/// `/api/profile` answers "where is the time going" row by row; this answers
/// "how fast is this panel" in three numbers, which is the question you ask
/// first. Same deferred-job machinery, for the same two reasons: `mcycle` does
/// not exist on this core (it reads 0, which is what this endpoint used to
/// report for everything), and `TIME_MS` cannot advance inside the trap handler
/// where HTTP is serviced.
unsafe fn bench_run() {
    if HUB75_PTR.is_null() { return; }
    let hub75 = &mut *HUB75_PTR;
    let (w, len) = hub75.get_img_param();
    let (w, len) = (w as usize, len as usize);
    if w == 0 || len == 0 { return; }
    let h = len / w;

    // 1. raw fill: one constant store per pixel, nothing else. The floor.
    BENCH_FILL_NS = profile_best_ns(|| {
        for p in (&mut *HUB75_PTR).back_buffer().iter_mut() {
            core::ptr::write_volatile(p, 0);
        }
    });

    // 2. the active program over the same pixels: the same stores plus its logic.
    let render = PROGRAM_FN.map(|p| p.row).unwrap_or(crate::generated::PROGRAMS[0].row);
    BENCH_PROG_NS = profile_best_ns(|| {
        let hub75 = &mut *HUB75_PTR;
        let ctx = crate::program::Ctx {
            w, h, frame: PROGRAM_FRAME, time_ms: TIME_MS,
            values: &*core::ptr::addr_of!(VALUES),
        };
        let buf = hub75.back_buffer();
        for y in 0..h {
            let start = y * w;
            if start + w > buf.len() { break; }
            render(&ctx, y, 0, &mut buf[start..start + w]);
        }
    });

    // 3. a palette rewrite, for scale: 256 words against 16384 pixels.
    BENCH_PAL_NS = profile_best_ns(|| {
        (&mut *HUB75_PTR).set_palette_all(0, crate::program::palette_default);
    });
}

unsafe fn api_bench(resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    if HUB75_PTR.is_null() {
        write!(resp, r#"{{"error":"no hub75"}}"#).ok();
        return;
    }
    match PROFILE_STATE {
        0 => {
            PROFILE_KIND = 1;
            PROFILE_STATE = 1;
            write!(resp, r#"{{"status":"queued","note":"GET again for the result"}}"#).ok();
        }
        1 | 2 => { write!(resp, r#"{{"status":"running"}}"#).ok(); }
        _ => {
            let hub75 = &mut *HUB75_PTR;
            let (w, len) = hub75.get_img_param();
            let len = len as usize;
            let _ = w;
            // 40 MHz sys_clk: cycles = ns / 25, ms = ns / 1_000_000.
            let cy = |ns: u32| ns / 25;
            let per_px = |ns: u32| if len > 0 { cy(ns) / len as u32 } else { 0 };
            let fps_x100 = |ns: u32| if ns > 0 { (100_000_000_000u64 / ns as u64) as u32 } else { 0 };
            write!(resp,
                r#"{{"status":"done","pixels":{},"display_on":{},"program":"{}","fill_ns":{},"program_ns":{},"palette_ns":{},"fill_cycles":{},"program_cycles":{},"palette_cycles":{},"fill_cy_per_px":{},"program_cy_per_px":{},"fill_ms":{},"program_ms":{},"program_fps_x100":{}}}"#,
                len,
                if hub75.is_on() { "true" } else { "false" },
                PROGRAM_NAME,
                BENCH_FILL_NS, BENCH_PROG_NS, BENCH_PAL_NS,
                cy(BENCH_FILL_NS), cy(BENCH_PROG_NS), cy(BENCH_PAL_NS),
                per_px(BENCH_FILL_NS), per_px(BENCH_PROG_NS),
                BENCH_FILL_NS / 1_000_000, BENCH_PROG_NS / 1_000_000,
                fps_x100(BENCH_PROG_NS)).ok();
            PROFILE_STATE = 0;
        }
    }
}

/// Full-resolution framebuffer dump, 16 rows at a time.
///
/// `/api/fb` subsamples 1 pixel in 4, which is fine for "is anything lit" and
/// useless for "does this glyph look right" -- a 4x decimation of 13px text is
/// noise. This returns every pixel of a 16-row slice, which is what fits in the
/// 8 KiB response buffer, so a caller can reassemble the panel exactly as the
/// scan-out sees it and LOOK at it instead of inferring from counters.
///
/// POST {"y0":N}. A GET would be nicer but the router matches the full path
/// including the query string, so `?y0=16` simply does not resolve.
/// Both palette banks, so the double buffering can be SEEN rather than assumed.
///
/// RB-5516: the palette is what turns an index into a colour, and it used to be
/// one table the CPU rewrote while the scan-out was reading it. It is now two,
/// selected by the same signal the framebuffer follows. This dumps each bank
/// and says which one is live, so "they are independent" and "the live one
/// tracks fb_base" are both checkable from outside.
unsafe fn api_palette(resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    if HUB75_PTR.is_null() {
        write!(resp, r#"{{"error":"no hub75"}}"#).ok();
        return;
    }
    let hub75 = &mut *HUB75_PTR;
    let banks = hub75.palette_banks();
    let live = hub75.live_palette_bank();
    write!(resp, r#"{{"banks":{},"live_bank":{},"fb_base_now":{},"entries":["#,
           banks, live, hub75.fb_base_now()).ok();
    // All 256. Two banks of 256 six-hex entries is 3072 characters, which fits
    // the 8 KiB response with room to spare -- and 64 was not enough to see the
    // rainbow band at 64..191 or the bulbs at 208, which is exactly where the
    // demos do their animating.
    const SHOW: usize = 256;
    for bank in 0..banks as usize {
        if bank > 0 { write!(resp, ",").ok(); }
        write!(resp, "\"").ok();
        for v in hub75.palette_bank(bank).iter().take(SHOW) {
            write!(resp, "{:06x}", v & 0xFFFFFF).ok();
        }
        write!(resp, "\"").ok();
    }
    resp.data.extend_from_slice(b"]}").ok();
}

unsafe fn api_fbdump(req: &HttpRequest, resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    if HUB75_PTR.is_null() {
        write!(resp, r#"{{"error":"no hub75"}}"#).ok();
        return;
    }
    let y0: usize = json_get_str(req.body_str(), "y0")
        .and_then(|v| v.parse().ok())
        .unwrap_or(0);
    let hub75 = &mut *HUB75_PTR;
    let (w, len) = hub75.get_img_param();
    let w = w as usize;
    let h = if w > 0 { len as usize / w } else { 0 };
    let mode = match hub75.get_mode() {
        crate::hub75::OutputMode::Indexed => "indexed",
        crate::hub75::OutputMode::FullColor => "fullcolor",
    };
    let n = 16usize.min(h.saturating_sub(y0));
    write!(resp, r#"{{"w":{},"h":{},"y0":{},"rows":{},"mode":"{}","data":""#, w, h, y0, n, mode).ok();
    let lo = y0 * w;
    let hi = lo + n * w;
    for (i, px) in hub75.read_img_data().enumerate() {
        if i >= lo && i < hi {
            write!(resp, "{:02x}", (px & 0xFF) as u8).ok();
        }
    }
    resp.data.extend_from_slice(b"\"}").ok();
}

unsafe fn api_fb(resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    if HUB75_PTR.is_null() {
        write!(resp, r#"{{"error":"no hub75"}}"#).ok();
        return;
    }
    let hub75 = &mut *HUB75_PTR;
    let (w, len) = hub75.get_img_param();
    let w = w as usize;
    let h = if w > 0 { len as usize / w } else { 0 };
    const STEP: usize = 4;
    let mode = match hub75.get_mode() {
        crate::hub75::OutputMode::Indexed => "indexed",
        crate::hub75::OutputMode::FullColor => "fullcolor",
    };
    write!(resp, r#"{{"w":{},"h":{},"step":{},"mode":"{}","data":""#, w, h, STEP, mode).ok();
    for (i, px) in hub75.read_img_data().enumerate() {
        if (i % w) % STEP == 0 && (i / w) % STEP == 0 {
            write!(resp, "{:02x}", (px & 0xFF) as u8).ok();
        }
    }
    // Plain byte string, NOT a format string: `}}` here emits two braces and
    // the JSON will not parse.
    resp.data.extend_from_slice(b"\"}").ok();
}

unsafe fn api_dmatest(resp: &mut HttpResponse) {
    use core::fmt::Write;
    let p = litex_pac::Peripherals::steal();
    let dma = p.dmatest;

    // Scratch area well clear of the framebuffer (which occupies byte
    // 0x200000-0x280000, i.e. word 0x80000-0xA0000).
    // SDRAM is 1M words; the framebuffer occupies words 0x80000-0xA0000, so
    // 0xA0000 upward is free scratch. 8 x 256k words is long enough (~100ms of
    // sustained writing) to measure the refresh cost against a 200Hz display.
    // Word 0xA8000 holds the crash breadcrumb (byte 0x2A0000), and the
    // framebuffer occupies 0x80000-0xA0000. Start above both. Verified the hard
    // way: an earlier base of 0xA0000 marched through the breadcrumb and left
    // its address-pattern data there -- which incidentally proved the DMA writes
    // correct data to correct addresses.
    const SCRATCH_WORD_BASE: u32 = 0x0B_0000;
    const BURST_WORDS: u32 = 262144;
    const REPEATS: u32 = 8;

    let refresh_before = if !HUB75_PTR.is_null() { (*HUB75_PTR).refresh_count() } else { 0 };
    let t_before = TIME_MS;

    let mut guard: u32 = 0;
    let mut cycles: u64 = 0;
    let mut written: u64 = 0;
    for _ in 0..REPEATS {
        dma.base().write(|w| w.bits(SCRATCH_WORD_BASE));
        dma.length().write(|w| w.bits(BURST_WORDS));
        dma.ctrl().write(|w| w.start().set_bit());
        // Bounded spin -- a wedged DMA must not hang us the way the old
        // unbounded UART wait in panic.rs did.
        loop {
            if !dma.status().read().busy().bit_is_set() {
                break;
            }
            guard += 1;
            if guard > 40_000_000 {
                break;
            }
        }
        // No clear needed: `start` is a pulse field now. It used to latch, so
        // the FSM re-armed the instant the burst finished and this loop was
        // measuring an unknown number of bursts rather than REPEATS of them.
        cycles += dma.cycles().read().bits() as u64;
        written += dma.written().read().bits() as u64;
    }
    let cycles = cycles as u32;
    let written = written as u32;
    let refresh_after = if !HUB75_PTR.is_null() { (*HUB75_PTR).refresh_count() } else { 0 };
    let t_after = TIME_MS;

    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    write!(resp, r#"{{"burst_words":{},"cycles":{},"written":{},"guard":{},"#,
        BURST_WORDS * REPEATS, cycles, written, guard).ok();
    write!(resp, r#""refresh_before":{},"refresh_after":{},"elapsed_ms":{}}}"#,
        refresh_before, refresh_after, (t_after - t_before) as i32).ok();
}

unsafe fn api_layout_get(resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();

    let layout = if !LAYOUT_PTR.is_null() { &*LAYOUT_PTR } else { return; };
    write!(resp, r#"{{"grid":"{}x{}","panel_width":{},"panel_height":{},"#,
        layout.grid_cols, layout.grid_rows, layout.panel_width, layout.panel_height).ok();
    write!(resp, r#""virtual_width":{},"virtual_height":{},"panels":{{"#,
        layout.virtual_width(), layout.virtual_height()).ok();
    for i in 0..6 {
        if i > 0 { write!(resp, ",").ok(); }
        if i < layout.assignments.len() {
            write!(resp, r#""J{}":["#, i + 1).ok();
            let mut first_slot = true;
            for a in layout.assignments[i].iter() {
                if !first_slot { write!(resp, ",").ok(); }
                match a {
                    Some((col, row)) => { write!(resp, r#""{},{}""#, col, row).ok(); }
                    None => { write!(resp, "null").ok(); }
                }
                first_slot = false;
            }
            write!(resp, "]").ok();
        } else {
            write!(resp, r#""J{}":[null,null]"#, i + 1).ok();
        }
    }
    write!(resp, "}}}}").ok();
}

unsafe fn api_display_get(resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();

    let (w, len) = if !HUB75_PTR.is_null() { (*HUB75_PTR).get_img_param() } else { (0, 0) };
    let h = if w > 0 { len / w as u32 } else { 0 };
    let mode = if !HUB75_PTR.is_null() {
        match (*HUB75_PTR).get_mode() {
            crate::hub75::OutputMode::FullColor => "fullcolor",
            crate::hub75::OutputMode::Indexed => "indexed",
        }
    } else { "unknown" };
    let anim = if !ANIMATION_PTR.is_null() {
        match &*ANIMATION_PTR {
            crate::menu::Animation::None => "none",
            crate::menu::Animation::Rainbow { .. } => "rainbow",
        }
    } else { "unknown" };
    write!(resp, r#"{{"width":{},"height":{},"mode":"{}","animation":"{}","source":"{}","hold":{}}}"#,
        w, h, mode, anim, display_source(),
        if BITMAP_STATS_PTR.is_null() { "false" }
        else if BITMAP_RX.assume_init_ref().is_held() { "true" } else { "false" }).ok();
}

unsafe fn api_bitmap_stats(resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();

    let stats = if !BITMAP_STATS_PTR.is_null() { &*BITMAP_STATS_PTR } else { return; };
    let fps = if stats.avg_interval_ms > 0 { 1000 / stats.avg_interval_ms } else { 0 };
    let (mac_ovf, mac_pre, mac_crc) = if IFACE_INITIALIZED {
        IFACE.assume_init_ref().device().mac_errors()
    } else { (0, 0, 0) };
    let ring_ovf = crate::ethernet::ring_overflow_count();
    let (dbg_fast, dbg_slow, dbg_batch) = debug_counters();

    write!(resp, r#"{{"packets_total":{},"packets_valid":{},"palette_writes":{},"#,
           stats.packets_total, stats.packets_valid, stats.palette_writes).ok();
    write!(resp, r#""swap_overlap_max":{},"swap_overlap_last":{},"#,
        stats.swap_overlap_max, stats.swap_overlap_last).ok();
    write!(resp, r#""bad_magic":{},"bad_header":{},"bad_size":{},"duplicate":{},"#,
        stats.packets_bad_magic, stats.packets_bad_header, stats.packets_bad_size,
        stats.packets_duplicate).ok();
    write!(resp, r#""frames_completed":{},"frames_partial":{},"frames_dropped":{},"#,
        stats.frames_completed, stats.frames_partial, stats.frames_dropped).ok();
    write!(resp, r#""frames_stale":{},"chunks_repaired":{},"last_missing":{},"#,
        stats.frames_stale, stats.chunks_repaired, stats.last_missing).ok();
    write!(resp, r#""fps":{},"frame_interval_ms":{},"avg_interval_ms":{},"jitter_ms":{},"#,
        fps, stats.frame_interval_ms, stats.avg_interval_ms, stats.jitter_ms).ok();
    write!(resp, r#""last_frame_id":{},"#, stats.last_frame_id).ok();
    write!(resp, r#""last_chunk":"{}/{}","last_size":"{}x{}","last_data_len":{},"#,
        stats.last_chunk_index, stats.last_total_chunks,
        stats.last_width, stats.last_height, stats.last_data_len).ok();
    write!(resp, r#""mac_overflow":{},"mac_crc_errors":{},"mac_preamble_errors":{},"ring_overflow":{},"#,
        mac_ovf, mac_crc, mac_pre, ring_ovf).ok();
    write!(resp, r#""fast_path":{},"slow_path":{},"max_batch":{},"mcast_dropped":{},"#,
        dbg_fast, dbg_slow, dbg_batch, DBG_MULTICAST_DROPPED).ok();
    write!(resp, r#""slow_arp":{},"slow_tcp":{},"slow_udp":{},"slow_other":{},"#,
        DBG_SLOW_ARP, DBG_SLOW_TCP, DBG_SLOW_UDP, DBG_SLOW_OTHER).ok();

    // Include captured slow-path packet if available
    if DBG_SLOW_PKT_CAPTURED {
        write!(resp, r#""slow_pkt_len":{},"slow_pkt":""#, DBG_SLOW_PKT_LEN).ok();
        for i in 0..64.min(DBG_SLOW_PKT_LEN) {
            write!(resp, "{:02x}", DBG_SLOW_PKT[i]).ok();
        }
        write!(resp, r#"","#).ok();
    }

    write!(resp, r#""isr_count":{},"mtvec":"0x{:08x}","trap_addr":"0x{:08x}","time_ms":{}}}"#,
        crate::ethernet::isr_count(), crate::ethernet::debug_mtvec(), crate::ethernet::trap_addr(), TIME_MS).ok();
}

unsafe fn api_display_on(resp: &mut HttpResponse) {
    use core::fmt::Write;
    if !HUB75_PTR.is_null() { (*HUB75_PTR).on(); }
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    write!(resp, r#"{{"ok":true,"display":"on"}}"#).ok();
}

unsafe fn api_display_off(resp: &mut HttpResponse) {
    use core::fmt::Write;
    if !HUB75_PTR.is_null() { (*HUB75_PTR).off(); }
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    write!(resp, r#"{{"ok":true,"display":"off"}}"#).ok();
}

unsafe fn api_display_pattern(req: &HttpRequest, resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();

    // Parse JSON body for "name" field (simple parser)
    let body = req.body_str();
    let name = match json_get_str(body, "name") {
        Some(n) => n,
        None => {
            resp.data.extend_from_slice(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\nmissing \"name\"").ok();
            return;
        }
    };

    if HUB75_PTR.is_null() {
        resp.data.extend_from_slice(b"HTTP/1.1 500 Internal Server Error\r\nConnection: close\r\n\r\nno display").ok();
        return;
    }
    let hub75 = &mut *HUB75_PTR;
    let (w, len) = hub75.get_img_param();
    let h = if w > 0 { (len / w as u32) as u16 } else { 0 };
    if w == 0 || h == 0 {
        resp.data.extend_from_slice(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\nimage params not set").ok();
        return;
    }

    use crate::patterns;
    let mut anim = false;
    let ok = match name {
        "grid" => { hub75.write_img_data(0, patterns::grid(w, h)); true }
        "rainbow" => { hub75.write_img_data(0, patterns::rainbow(w, h)); true }
        "rainbow_anim" => {
            hub75.write_img_data(0, patterns::animated_rainbow(w, h, 0));
            anim = true;
            true
        }
        "white" => { hub75.write_img_data(0, patterns::solid_white(w, h)); true }
        "red" => { hub75.write_img_data(0, patterns::solid_red(w, h)); true }
        "green" => { hub75.write_img_data(0, patterns::solid_green(w, h)); true }
        "blue" => { hub75.write_img_data(0, patterns::solid_blue(w, h)); true }
        _ => false,
    };

    if ok {
        if !ANIMATION_PTR.is_null() {
            *ANIMATION_PTR = if anim {
                crate::menu::Animation::Rainbow { phase: 0 }
            } else {
                crate::menu::Animation::None
            };
        }
        hub75.swap_buffers();
        // A native pattern and a program cannot both own the framebuffer. Without
        // this, program_tick() re-asserted INDEXED every 33ms and the pattern's
        // full-colour words were reinterpreted as palette indices against the
        // program's ramp palette -- the pattern drew, but in the wrong colours
        // entirely, which looks like a broken colour map rather than two things
        // fighting over the display.
        PROGRAM_ACTIVE = false;
        hub75.set_mode(crate::hub75::OutputMode::FullColor);
        // Hold the stream too -- CPU path AND pixel DMA. Otherwise the pattern
        // is overwritten within one frame period and the buttons look dead.
        set_stream_hold(true);
        // This is the one place outside the bitmap receiver that writes the
        // mode CSR. Tell the receiver so its cached guard re-asserts on the
        // next 'B','I' frame instead of believing it is still in indexed mode.
        BITMAP_RX.assume_init_mut().invalidate_mode_cache();
        hub75.on();
        resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
        write!(resp, r#"{{"ok":true,"pattern":"{}"}}"#, name).ok();
    } else {
        resp.data.extend_from_slice(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\nunknown pattern").ok();
    }
}

/// TIME_MS at which to reset the SoC, or 0 for "not armed".
static mut REBOOT_AT_MS: i64 = 0;

/// SPI flash status registers SR1/SR2/SR3, sampled once at boot.
static mut FLASH_SR: [u8; 3] = [0; 3];

/// Record the flash status registers for `/api/status`.
pub fn set_flash_status(sr: [u8; 3]) {
    unsafe { FLASH_SR = sr };
}

/// Reset the SoC if a scheduled reboot has come due. Called from the main loop.
///
/// The reset CANNOT happen in the handler that answers the request: the
/// response is still sitting in a smoltcp TX buffer at that point, so resetting
/// there drops the connection and the caller sees a hang instead of an answer.
/// It waits for the response and its FIN to actually leave.
///
/// This endpoint used to be a stub that returned `{"ok":true,"rebooting":true}`
/// and did nothing at all -- a lie of exactly the kind this codebase keeps
/// finding, where the report of an action and the action are different things.
pub unsafe fn reboot_tick() {
    if REBOOT_AT_MS != 0 && TIME_MS >= REBOOT_AT_MS {
        (*pac::Ctrl::ptr()).reset().write(|w| w.soc_rst().set_bit());
    }
}

/// Dump the first bytes of SPI flash through the MEMORY-MAPPED window.
///
/// Independent verification path. Everything else that has checked the flash --
/// openFPGALoader's --dump-flash, its --verify -- reads back through the SAME
/// SPI bridge it wrote with, so a write that silently did nothing and a read
/// that silently returns what you expect are indistinguishable. This reads via
/// the SoC's own LiteSPI controller instead: different hardware path, different
/// code, no shared assumption.
///
/// Offset 0 is where the ECP5 looks for a bitstream at power-on. A correct image
/// starts with 0xFF padding then the sync word FF FF BD B3.
unsafe fn api_flash(resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    // Must match the spiflash SoCRegion origin in gateware/colorlight.py.
    const MMAP: usize = 0x8040_0000;
    write!(resp, r#"{{"base":"0x80400000","offset0":""#).ok();
    for i in 0..64usize {
        let b = core::ptr::read_volatile((MMAP + i) as *const u8);
        write!(resp, "{:02x}", b).ok();
    }
    // And the firmware region, so both can be checked at once.
    write!(resp, r#"","offset100000":""#).ok();
    for i in 0..32usize {
        let b = core::ptr::read_volatile((MMAP + 0x10_0000 + i) as *const u8);
        write!(resp, "{:02x}", b).ok();
    }
    write!(resp, r#""}}"#).ok();
}

/// Time reads out of the memory-mapped SPI flash.
///
/// Exists because `flashboot()` is the ONLY thing on the cold-boot path that
/// touches flash, and it reads the whole firmware image TWICE -- once byte at a
/// time for the CRC32 in `check_image_in_flash()`, then again to copy it into
/// DRAM. The region is `cached=False` with `READ_1_1_1` at a divisor-9 LiteSPI
/// clock, so every access is its own single-lane transaction. Before the FBI
/// header landed (v1.36.0) the BIOS rejected the image after 4 bytes and this
/// cost did not exist; a cold boot now takes ~30 s while a warm one takes 1.2 s.
///
/// Reads a fixed 64 KiB from the firmware region. Reports both the
/// byte-at-a-time and word-at-a-time cost, because the CRC pass does the former
/// and the copy does the latter, and they are not the same price.
unsafe fn api_flashbench(resp: &mut HttpResponse) {
    use core::fmt::Write;
    const IMG: usize = 0x8040_0000 + 0x10_0000;

    // NOT mcycle. `csrr 0xB00` reads back 0 on this VexRiscv build -- which is
    // why /api/bench has always reported fill_cycles:0 and nobody noticed. The
    // 1-second countdown timer is real hardware and keeps running inside the
    // trap handler, where TIME_MS cannot advance.
    #[inline(always)]
    unsafe fn ticks() -> u32 {
        let t = &*pac::Timer0::ptr();
        t.update_value().write(|w| w.bits(1));
        TIMER_RELOAD - 1 - t.value().read().bits()
    }
    #[inline(always)]
    fn delta(a: u32, b: u32) -> u32 {
        if b >= a { b - a } else { b + TIMER_RELOAD - a }
    }

    // 4 KiB so a pass cannot outlast the timer's 1-second period even if a
    // single-lane read turns out to cost ~100us/byte. Scale up from here.
    const N: usize = 4096;

    // Byte at a time -- what crc32() does over the whole image.
    let t0 = ticks();
    let mut sum: u32 = 0;
    for i in 0..N {
        sum = sum.wrapping_add(core::ptr::read_volatile((IMG + i) as *const u8) as u32);
    }
    let byte_cy = delta(t0, ticks());

    // Word at a time -- what the copy to DRAM does.
    let t0 = ticks();
    let mut wsum: u32 = 0;
    for i in 0..(N / 4) {
        wsum = wsum.wrapping_add(core::ptr::read_volatile((IMG + i * 4) as *const u32));
    }
    let word_cy = delta(t0, ticks());

    // Same loops over DRAM, as a control: it isolates loop overhead from the
    // cost of the SPI transaction itself.
    // main_ram_uncached at 0x90000000 (see the SoCRegion map in colorlight.py).
    let ram = 0x9000_0000usize;
    let t0 = ticks();
    let mut rsum: u32 = 0;
    for i in 0..N {
        rsum = rsum.wrapping_add(core::ptr::read_volatile((ram + i) as *const u8) as u32);
    }
    let ram_byte_cy = delta(t0, ticks());

    // Divisor sweep. LiteSPIPHY's clk_divisor is a CSRStorage whose RESET value
    // is the gateware's default_divisor, so the firmware can retune the SPI
    // clock at runtime -- which means the fast setting can be proven to read
    // correctly on this board BEFORE it is baked into a bitstream. The BIOS
    // still boots at the reset value, so only default_divisor changes boot time.
    //
    // spi_clk = sys_clk / (2 * (divisor + 1)):  9 -> 2 MHz, 1 -> 10 MHz, 0 -> 20 MHz.
    //
    // Each entry re-reads the SAME 4 KiB and reports a checksum. A divisor whose
    // checksum differs from the divisor-9 baseline read wrong and must not ship.
    let img_len = core::ptr::read_volatile(IMG as *const u32);
    let phy = &*pac::SpiflashPhy::ptr();
    let orig_div = phy.clk_divisor().read().bits();
    let mut sweep: [(u32, u32, u32); 5] = [(0, 0, 0); 5];
    for (slot, div) in [9u32, 4, 2, 1, 0].iter().enumerate() {
        phy.clk_divisor().write(|w| w.bits(*div));
        // One throwaway read so the first transaction at the new clock is not
        // the one being timed.
        let _ = core::ptr::read_volatile(IMG as *const u32);
        // The WHOLE image, not a 4 KiB sample: a marginal setup reads a short
        // burst fine and fails somewhere in 293 KB, which is precisely the
        // failure that would brick a cold boot while every bench test passed.
        // Timed in 8 chunks and summed. The timer period is 1 second and a
        // full-image pass at divisor 9 takes ~1.25 s, so timing it in one go
        // aliases it to 247 ms -- faster than every faster divisor, which is
        // how the wrap announces itself if you are paying attention.
        let words = img_len as usize / 4;
        let chunk = words / 8 + 1;
        let mut cs: u32 = 0;
        let mut cy: u32 = 0;
        let mut i = 0usize;
        while i < words {
            let end = core::cmp::min(i + chunk, words);
            let t0 = ticks();
            while i < end {
                cs = cs
                    .rotate_left(1)
                    .wrapping_add(core::ptr::read_volatile((IMG + 8 + i * 4) as *const u32));
                i += 1;
            }
            cy = cy.wrapping_add(delta(t0, ticks()));
        }
        sweep[slot] = (*div, cy, cs);
    }
    phy.clk_divisor().write(|w| w.bits(orig_div));

    // The BIOS's other cold-path cost: netboot() failing.
    //
    // tftp_get() calls udp_arp_resolve() first, which is 8 tries x 100,000
    // spins of udp_service(). netboot() calls tftp_get() twice (boot.json then
    // boot.bin), so a netboot with no answer costs 1,600,000 spins. When idle
    // udp_service() is exactly one CSR read of ethmac_sram_writer_ev_pending
    // plus a branch -- replicated here, so the per-spin cost is measured on the
    // real bus rather than guessed.
    const SPINS: u32 = 100_000;
    let eth = &*pac::Ethmac::ptr();
    let t0 = ticks();
    let mut esum: u32 = 0;
    for _ in 0..SPINS {
        esum = esum.wrapping_add(eth.sram_writer_ev_pending().read().bits());
    }
    let spin_cy = delta(t0, ticks());
    // 2 tftp_get x 8 ARP tries x 100,000 spins
    let netboot_cy = (spin_cy as u64) * 16;

    // What flashboot() actually pays: crc32 over the whole image TWICE, then a
    // word-wise copy. check_image_in_flash() runs once in flashboot() itself and
    // AGAIN inside copy_image_from_flash_to_ram() (boot.c:623), which is easy to
    // miss and is worth 8.4 s on its own.
    let scale = |cy: u32, per: usize| -> u64 {
        (cy as u64) * (img_len as u64) / (per as u64)
    };
    let boot_cy = 2 * scale(byte_cy, N) + scale(word_cy, N);

    resp.data.clear();
    resp.data
        .extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n")
        .ok();
    write!(
        resp,
        r#"{{"n":{},"byte_cycles":{},"word_cycles":{},"ram_byte_cycles":{},"img_len":{},"flashboot_cycles":{},"flashboot_ms":{},"spin_cycles":{},"netboot_fail_ms":{},"sys_clk_hz":{},"chk":{}"#,
        N, byte_cy, word_cy, ram_byte_cy, img_len, boot_cy,
        boot_cy / (SYS_CLK_HZ as u64 / 1000), spin_cy,
        netboot_cy / (SYS_CLK_HZ as u64 / 1000), SYS_CLK_HZ,
        sum ^ wsum ^ rsum ^ esum
    )
    .ok();
    write!(resp, r#","divisor_reset":{},"sweep":["#, orig_div).ok();
    for (i, (div, cy, cs)) in sweep.iter().enumerate() {
        let ok = *cs == sweep[0].2;
        write!(
            resp,
            r#"{}{{"div":{},"mhz_x10":{},"cycles":{},"ok":{},"chk":{}}}"#,
            if i > 0 { "," } else { "" },
            div,
            (SYS_CLK_HZ / 100_000) / (2 * (div + 1)),
            cy,
            ok,
            cs
        )
        .ok();
    }
    write!(resp, "]}}").ok();
}

/// Restore the flash's output drive strength (SR3 = 0x00, DRV = 100%).
///
/// One-shot, on demand. See flash_id::write_status3 -- this is a non-volatile
/// write, and the hypothesis it tests is that a weakened flash driver passes
/// every buffered read while failing the ECP5's power-on configuration fetch.
unsafe fn api_flash_drive(req: &HttpRequest, resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    // steal() rather than ptr(): these helpers take the peripheral wrapper, and
    // img_flash::Flash owns the real one. Single-threaded, and the SPI master is
    // idle whenever HTTP is being served.
    let p = litex_pac::Peripherals::steal();
    let spi = &p.spiflash_mmap;
    let before = crate::flash_id::read_status_regs(spi);
    // Value from the body: {"sr3":32}. Defaults to 0x00 (full drive) so the
    // common case needs no argument, but any value can be written back --
    // reverting a change to a non-volatile register must be as easy as making
    // it, or the experiment is not reversible and should not have been run.
    let body = req.body_str();
    let mut want: u8 = 0x00;
    if let Some(i) = body.find("\"sr3\"") {
        let mut n: u32 = 0;
        let mut seen = false;
        for c in body[i + 5..].chars() {
            if let Some(d) = c.to_digit(10) {
                n = n * 10 + d;
                seen = true;
            } else if seen {
                break;
            }
        }
        if seen && n <= 255 {
            want = n as u8;
        }
    }
    // {"sr2":2} writes SR2 (QE), {"sr3":N} writes SR3 (drive strength).
    let mut reg = 3u8;
    if body.contains("\"sr2\"") {
        reg = 2;
        want = 0x00;
        if let Some(i) = body.find("\"sr2\"") {
            let (mut n, mut seen) = (0u32, false);
            for c in body[i + 5..].chars() {
                if let Some(d) = c.to_digit(10) { n = n * 10 + d; seen = true; }
                else if seen { break; }
            }
            if seen && n <= 255 { want = n as u8; }
        }
    }
    let sr1 = if reg == 2 {
        crate::flash_id::write_status2(spi, want)
    } else {
        crate::flash_id::write_status3(spi, want)
    };
    let after = crate::flash_id::read_status_regs(spi);
    write!(resp,
        r#"{{"before":[{},{},{}],"after":[{},{},{}],"sr1_after_write":{},"reg":{},"wrote":{}}}"#,
        before[0], before[1], before[2], after[0], after[1], after[2], sr1, reg, want).ok();
}

unsafe fn api_reboot(resp: &mut HttpResponse) {
    use core::fmt::Write;
    resp.data.clear();
    resp.data.extend_from_slice(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n").ok();
    // 300ms is comfortably more than the response plus FIN needs on a 100Mbit
    // link, and short enough that a caller polling for the board to come back
    // does not conclude the request failed.
    REBOOT_AT_MS = TIME_MS + 300;
    write!(resp, r#"{{"ok":true,"rebooting":true,"in_ms":300}}"#).ok();
}

/// Simple JSON string extractor for "key":"value" patterns
fn json_get_str<'a>(json: &'a str, key: &str) -> Option<&'a str> {
    let b = json.as_bytes();
    let kb = key.as_bytes();
    let len = b.len();
    let mut i = 0;
    while i < len {
        if b[i] == b'"' {
            let ks = i + 1;
            let ke = ks + kb.len();
            if ke < len && &b[ks..ke] == kb && b[ke] == b'"' {
                let mut p = ke + 1;
                while p < len && b[p] == b' ' { p += 1; }
                if p < len && b[p] == b':' {
                    p += 1;
                    while p < len && b[p] == b' ' { p += 1; }
                    if p < len && b[p] == b'"' {
                        let vs = p + 1;
                        let mut j = vs;
                        while j < len && b[j] != b'"' { j += 1; }
                        if j < len {
                            return core::str::from_utf8(&b[vs..j]).ok();
                        }
                    }
                }
            }
        }
        i += 1;
    }
    None
}

// ============================================================================
// Raw fast path for bitmap UDP
// ============================================================================

/// Check if a frame is multicast but NOT broadcast.
/// Multicast: first byte LSB is 1 (01:xx:xx:xx:xx:xx)
/// Broadcast: all ff (ff:ff:ff:ff:ff:ff) - we need this for DHCP
#[inline]
/// ARP that is not addressed to us.
///
/// The panel serves ARP through smoltcp on the SLOW path, inside the same
/// interrupt handler that consumes the pixel stream, so every broadcast ARP on
/// the segment is a short gap in frame processing -- visible as a periodic
/// stutter in scrolling text. On a busy /24 almost all of it is asking about
/// other hosts.
///
/// ARP for US is still handled: a request we fail to answer means the sender's
/// cache expires and the stream stops entirely. Target protocol address sits at
/// offset 38 (14 ethernet + 24 into the ARP body).
pub fn is_foreign_arp(frame: &[u8]) -> bool {
    if frame.len() < 42 {
        return false;
    }
    if ((frame[12] as u16) << 8 | frame[13] as u16) != 0x0806 {
        return false;
    }
    let ours = unsafe { OUR_IPV4 };
    if ours == [0, 0, 0, 0] {
        return false; // no address yet -- never filter during bring-up
    }
    frame[38..42] != ours
}

pub fn is_multicast(frame: &[u8]) -> bool {
    if frame.len() < 6 { return false; }
    // Check multicast bit
    if (frame[0] & 0x01) == 0 { return false; }
    // Allow broadcast through (needed for DHCP)
    if frame[0] == 0xff && frame[1] == 0xff && frame[2] == 0xff
        && frame[3] == 0xff && frame[4] == 0xff && frame[5] == 0xff {
        return false;
    }
    true  // It's multicast but not broadcast - drop it
}

/// Check if frame is UDP but not a port we care about.
/// We keep: 7000 (bitmap), 6454 (artnet), 67/68 (DHCP), 69 (TFTP), 6900 (TFTP client)
#[inline]
pub fn is_unwanted_udp(frame: &[u8]) -> bool {
    if frame.len() < 44 { return false; }
    // EtherType IPv4
    if frame[12] != 0x08 || frame[13] != 0x00 { return false; }
    // Protocol UDP
    if frame[23] != 17 { return false; }
    // Get IP header length (IHL field) to find UDP header at variable offset
    let ihl = (frame[14] & 0x0F) as usize;
    if ihl < 5 { return false; }
    let udp_offset = 14 + ihl * 4;
    if frame.len() < udp_offset + 4 { return false; }
    // UDP destination port (bytes 2-3 of UDP header)
    let dst_port = ((frame[udp_offset + 2] as u16) << 8) | frame[udp_offset + 3] as u16;
    // Keep these ports, drop everything else
    !matches!(dst_port, 7000 | 6454 | 67 | 68 | 69 | 6900)
}

/// Check if frame is an ARP request not for us (can be dropped during streaming)
#[inline]
pub fn is_arp_not_for_us(frame: &[u8], our_ip: [u8; 4]) -> bool {
    if frame.len() < 42 { return false; }
    // EtherType ARP = 0x0806
    if frame[12] != 0x08 || frame[13] != 0x06 { return false; }
    // ARP operation: 1 = request
    if frame[20] != 0x00 || frame[21] != 0x01 { return false; }
    // Target IP is at offset 38-41
    frame[38] != our_ip[0] || frame[39] != our_ip[1]
        || frame[40] != our_ip[2] || frame[41] != our_ip[3]
}

/// Check if a raw Ethernet frame is a UDP packet destined for the bitmap port (7000).
/// Handles variable IP header length (IHL field).
#[inline]
pub fn is_bitmap_udp(frame: &[u8]) -> bool {
    // Min: eth(14) + ip(20) + udp(8) + bitmap header(10) = 52 bytes
    if frame.len() < 52 { return false; }

    // EtherType: IPv4
    if frame[12] != 0x08 || frame[13] != 0x00 { return false; }

    // Get IP header length (IHL field, in 32-bit words)
    let ihl = (frame[14] & 0x0F) as usize;
    if ihl < 5 { return false; }  // IHL must be at least 5 (20 bytes)
    let ip_header_len = ihl * 4;
    let udp_offset = 14 + ip_header_len;  // Ethernet header + IP header

    // Check we have enough bytes for UDP header + bitmap header
    if frame.len() < udp_offset + 18 { return false; }  // +8 UDP + 10 bitmap header

    // Protocol: UDP (at fixed offset 23 in IP header - 14 eth + 9)
    if frame[23] != 17 { return false; }

    // UDP destination port at variable offset (UDP header bytes 2-3)
    let port = u16::from_be_bytes([frame[udp_offset + 2], frame[udp_offset + 3]]);
    port == 7000
}

/// Run `f` with machine interrupts masked, restoring the previous state.
///
/// Needed wherever the main loop touches state the ISR owns: all network
/// processing runs inside the trap handler, so anything shared is effectively
/// concurrent.
#[inline]
unsafe fn without_interrupts<R>(f: impl FnOnce() -> R) -> R {
    let prev: u32;
    core::arch::asm!("csrrci {}, mstatus, 0b1000", out(reg) prev);
    let result = f();
    if prev & 0x8 != 0 {
        core::arch::asm!("csrrsi x0, mstatus, 0b1000");
    }
    result
}

/// Swap a finished frame onto the glass AS SOON AS the gateware has one.
///
/// Called every main-loop iteration, not on the 1 ms timer tick. The gateware
/// latches a completed frame the instant the next frame's header arrives, and
/// from then until the CPU swaps, the DMA is writing the NEXT frame into the
/// very buffer that is about to be displayed. Everything it writes in that
/// window appears on the glass as a band of the next frame across the top.
///
/// Measured at 25.8 fps on a 384x192 frame before this existed: up to 10 chunks
/// of the next frame already written at swap time, 6.6% of the image. That is
/// the "sometimes glitches" on a video feed -- sometimes, because it depends on
/// where the tick lands relative to the frame boundary.
///
/// Cheap enough to poll flat out: present_done_frame() reads done_seq first and
/// returns immediately when it has not moved, which is one CSR read at 8 cycles.
pub fn bitmap_present_tick() {
    unsafe {
        if HUB75_PTR.is_null() || !IFACE_INITIALIZED || PROGRAM_ACTIVE {
            return;
        }
        without_interrupts(|| {
            let hub75 = &mut *HUB75_PTR;
            let bitmap_rx = BITMAP_RX.assume_init_mut();
            if bitmap_rx.present_done_now(hub75, TIME_MS) {
                // The gateware has just flipped, so the bank that was live is
                // now the back bank -- catch it up before anything is drawn
                // into it. See sync_palette_bank().
                hub75.sync_palette_bank();
                hub75.on();
            }
        });
    }
}

/// Present an in-progress bitmap frame that has gone quiet.
///
/// Called from the main loop. The ISR can only finish a frame when a packet
/// arrives, so without this a frame whose tail was lost -- or simply the last
/// frame before a sender stops -- never reaches the panel.
pub fn bitmap_tick() {
    unsafe {
        if HUB75_PTR.is_null() || !IFACE_INITIALIZED {
            return;
        }
        // The profiler must run HERE, not in the HTTP handler: TIME_MS is the
        // only working timebase and it cannot advance inside the trap handler.
        if PROFILE_STATE == 1 {
            profile_run();
            return;
        }
        if PROGRAM_ACTIVE {
            program_tick();
            return;
        }
        without_interrupts(|| {
            let hub75 = &mut *HUB75_PTR;
            let bitmap_rx = BITMAP_RX.assume_init_mut();
            if bitmap_rx.tick(hub75, TIME_MS) {
                // Same catch-up as the fast present path: this is the other way
                // a streamed frame reaches the glass.
                hub75.sync_palette_bank();
                // The wire format is authoritative: process_packet() already selected
                // the output mode from the magic. Forcing FullColor here clobbered the
                // Indexed mode set moments earlier, so 'B','I' content scanned out as
                // full colour -- the index sits in the low byte of 0x00GGRRBB, so every
                // pixel became one channel at index brightness: the whole sign a single
                // colour with the shapes still legible. No counter can see colour, which
                // is why every counter-based test passed.
                hub75.on();
                if !ANIMATION_PTR.is_null() {
                    *ANIMATION_PTR = crate::menu::Animation::None;
                }
            }
            if !BITMAP_STATS_PTR.is_null() {
                *BITMAP_STATS_PTR = bitmap_rx.stats;
            }
        });
    }
}

/// Process a raw bitmap UDP packet from hardware.
/// Called from ISR for the fast path.
pub fn process_raw_bitmap(frame: &[u8]) -> bool {
    unsafe {
        if HUB75_PTR.is_null() {
            return false;
        }

        // Update streaming timestamp on every packet
        LAST_BITMAP_PACKET_MS = TIME_MS;

        let hub75 = &mut *HUB75_PTR;
        let bitmap_rx = BITMAP_RX.assume_init_mut();

        // Calculate UDP payload offset with variable IP header length
        let ihl = (frame[14] & 0x0F) as usize;
        let ip_header_len = ihl * 4;
        let udp_offset = 14 + ip_header_len + 8;  // Ethernet + IP + UDP header
        // The receiver owns the swap now: it patches missing chunks from the
        // displayed buffer first, so presenting can happen on completion, on
        // the next frame starting, or on a staleness deadline.
        let presented = bitmap_rx.process_packet(&frame[udp_offset..], hub75, TIME_MS);

        if presented {
            // The wire format is authoritative: process_packet() already selected
            // the output mode from the magic. Forcing FullColor here clobbered the
            // Indexed mode set moments earlier, so 'B','I' content scanned out as
            // full colour -- the index sits in the low byte of 0x00GGRRBB, so every
            // pixel became one channel at index brightness: the whole sign a single
            // colour with the shapes still legible. No counter can see colour, which
            // is why every counter-based test passed.
            hub75.on();
            if !ANIMATION_PTR.is_null() {
                *ANIMATION_PTR = crate::menu::Animation::None;
            }
        }

        // Publish the stats block, but not on every single packet.
        //
        // It must not sit inside `if presented` -- that froze the counters
        // exactly when frames stopped completing, which is the regime
        // bad_magic / bad_header / frames_dropped exist to diagnose. But it is
        // a ~22-field struct written to UNCACHED SDRAM, so doing it per packet
        // costs a few hundred cycles ~2000 times a second to publish numbers
        // nothing reads at that rate. Publish on every presented frame, and
        // otherwise every PUBLISH_EVERY packets so the counters still move
        // while frames are being dropped.
        const PUBLISH_EVERY: u32 = 64;
        STATS_PUBLISH_TICK = STATS_PUBLISH_TICK.wrapping_add(1);
        if !BITMAP_STATS_PTR.is_null()
            && (presented || STATS_PUBLISH_TICK % PUBLISH_EVERY == 0) {
            *BITMAP_STATS_PTR = bitmap_rx.stats;
        }

        presented
    }
}
