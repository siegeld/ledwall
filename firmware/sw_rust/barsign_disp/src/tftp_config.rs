/// Minimal TFTP GET client (RFC 1350) for loading layout.cfg at boot.
///
/// State machine polled from the main loop. Uses a smoltcp UDP socket.
/// Fetches a single file from the DHCP gateway and parses it as layout config.

use smoltcp::socket::UdpSocket;
use smoltcp::wire::{IpEndpoint, Ipv4Address};

const TFTP_PORT: u16 = 6969;
const OPCODE_RRQ: u8 = 1;
const OPCODE_DATA: u8 = 3;
const OPCODE_ACK: u8 = 4;
const OPCODE_ERROR: u8 = 5;
const TIMEOUT_MS: i64 = 3000;
// One TFTP block is 512 bytes, and this used to be exactly that -- which meant a
// layout config silently lost everything past the first block. Measured
// 2026-09-14: a 6-panel layout is 311 bytes and a 9-panel 3x3 is 347, so the old
// cap was already within ~165 bytes of overflowing before `mode:`/`program:`/
// `params:` were added to the same file. 2048 = four blocks, enough for a
// 16-panel wall plus program selection.
const MAX_CONFIG_SIZE: usize = 2048;

#[derive(Clone, Copy, PartialEq)]
pub enum TftpState {
    Idle,
    SendRrq,
    WaitData,
    Done,
    Failed,
}

pub struct TftpConfigLoader {
    pub state: TftpState,
    gateway: Ipv4Address,
    server_tid: u16,
    block: u16,
    deadline_ms: i64,
    buf: [u8; MAX_CONFIG_SIZE],
    len: usize,
    /// Set if the server sent more than `MAX_CONFIG_SIZE`. A half-read layout is
    /// worse than no layout: it applies to some connectors and not others, which
    /// on the wall is indistinguishable from dead panels or bad cabling. So this
    /// makes `parse_config()` refuse the config rather than apply part of it.
    overflowed: bool,
    filename: [u8; 32],
    filename_len: usize,
}

impl TftpConfigLoader {
    pub fn new() -> Self {
        Self {
            state: TftpState::Idle,
            gateway: Ipv4Address::UNSPECIFIED,
            server_tid: 0,
            block: 0,
            deadline_ms: 0,
            buf: [0u8; MAX_CONFIG_SIZE],
            overflowed: false,
            len: 0,
            filename: [0u8; 32],
            filename_len: 0,
        }
    }

    /// Start a TFTP config fetch from the given gateway.
    pub fn start(&mut self, gateway: Ipv4Address, filename: &str) {
        self.gateway = gateway;
        self.server_tid = 0;
        self.block = 1;
        self.len = 0;
        let n = filename.len().min(32);
        self.filename[..n].copy_from_slice(&filename.as_bytes()[..n]);
        self.filename_len = n;
        self.state = TftpState::SendRrq;
    }

    pub fn is_active(&self) -> bool {
        matches!(self.state, TftpState::SendRrq | TftpState::WaitData)
    }

    pub fn is_done(&self) -> bool {
        self.state == TftpState::Done
    }

    /// Poll the TFTP state machine. Returns true when config is ready.
    pub fn poll(&mut self, socket: &mut UdpSocket, time_ms: i64) -> bool {
        match self.state {
            TftpState::SendRrq => {
                if !socket.is_open() {
                    if socket.bind(6900).is_err() {
                        self.state = TftpState::Failed;
                        return false;
                    }
                }
                if socket.can_send() {
                    // Build RRQ: opcode(2) + filename + NUL + "octet" + NUL
                    let filename = &self.filename[..self.filename_len];
                    let mode = b"octet";
                    let mut pkt = [0u8; 48];
                    pkt[0] = 0;
                    pkt[1] = OPCODE_RRQ;
                    let mut pos = 2;
                    pkt[pos..pos + filename.len()].copy_from_slice(filename);
                    pos += filename.len();
                    pkt[pos] = 0;
                    pos += 1;
                    pkt[pos..pos + mode.len()].copy_from_slice(mode);
                    pos += mode.len();
                    pkt[pos] = 0;
                    pos += 1;

                    let endpoint = IpEndpoint::new(self.gateway.into(), TFTP_PORT);
                    if socket.send_slice(&pkt[..pos], endpoint).is_ok() {
                        self.deadline_ms = time_ms + TIMEOUT_MS;
                        self.state = TftpState::WaitData;
                    }
                }
            }
            TftpState::WaitData => {
                // Check timeout
                if time_ms > self.deadline_ms {
                    socket.close();
                    self.state = TftpState::Failed;
                    return false;
                }

                // Process one packet at a time to avoid borrow conflicts.
                // recv() borrows the socket buffer, so we must extract all
                // needed data before calling send_slice().
                if socket.can_recv() {
                    // Extract packet info into locals before releasing borrow
                    let mut pkt_opcode: u8 = 0;
                    let mut pkt_block: u16 = 0;
                    let mut pkt_block_bytes = [0u8; 2];
                    let mut pkt_port: u16 = 0;
                    let mut pkt_addr = smoltcp::wire::IpAddress::Ipv4(Ipv4Address::UNSPECIFIED);
                    let mut payload_len: usize = 0;
                    let mut got_packet = false;

                    match socket.recv() {
                        Ok((data, endpoint)) => {
                            if data.len() >= 4 {
                                pkt_opcode = data[1];
                                pkt_block = u16::from_be_bytes([data[2], data[3]]);
                                pkt_block_bytes = [data[2], data[3]];
                                pkt_port = endpoint.port;
                                pkt_addr = endpoint.addr;

                                if pkt_opcode == OPCODE_DATA && pkt_block == self.block {
                                    let payload = &data[4..];
                                    payload_len = payload.len();
                                    let copy_len = payload_len.min(MAX_CONFIG_SIZE - self.len);
                                    if copy_len < payload_len {
                                        self.overflowed = true;
                                    }
                                    self.buf[self.len..self.len + copy_len]
                                        .copy_from_slice(&payload[..copy_len]);
                                    self.len += copy_len;
                                }
                                got_packet = true;
                            }
                        }
                        Err(_) => {}
                    }

                    // Now socket borrow is released — safe to send
                    if got_packet {
                        match pkt_opcode {
                            OPCODE_DATA if pkt_block == self.block => {
                                if self.server_tid == 0 {
                                    self.server_tid = pkt_port;
                                }

                                let ack = [0, OPCODE_ACK, pkt_block_bytes[0], pkt_block_bytes[1]];
                                let ack_ep = IpEndpoint::new(pkt_addr, self.server_tid);
                                socket.send_slice(&ack, ack_ep).ok();

                                if payload_len < 512 {
                                    // Don't close socket here — close() resets the TX buffer,
                                    // which drops the ACK we just queued. Let iface.poll()
                                    // transmit the ACK first; the caller closes after.
                                    self.state = TftpState::Done;
                                    return true;
                                }
                                self.block += 1;
                                self.deadline_ms = time_ms + TIMEOUT_MS;
                            }
                            OPCODE_ERROR => {
                                socket.close();
                                self.state = TftpState::Failed;
                                return false;
                            }
                            _ => {}
                        }
                    }
                }
            }
            TftpState::Done | TftpState::Failed | TftpState::Idle => {}
        }
        false
    }

    /// Parse the loaded config into a LayoutConfig.
    /// Did the fetched config exceed `MAX_CONFIG_SIZE`? Reported by /api/status
    /// so an oversized config is diagnosable without a serial console.
    pub fn overflowed(&self) -> bool {
        self.overflowed
    }

    pub fn parse_config(&self) -> Option<crate::layout::LayoutConfig> {
        if self.len == 0 || self.overflowed {
            return None;
        }
        let text = core::str::from_utf8(&self.buf[..self.len]).ok()?;
        crate::layout::LayoutConfig::parse(text)
    }
}
