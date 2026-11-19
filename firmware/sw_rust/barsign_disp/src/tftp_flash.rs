//! Streaming TFTP -> SPI flash sink, for over-the-air BITSTREAM updates.
//!
//! This is the piece that lets the JTAG cable come off for good. The firmware
//! already netboots itself over TFTP, so the *firmware* has been updatable over
//! the network for a long time; the FPGA bitstream lives in SPI flash and until
//! now could only be replaced with a programmer attached.
//!
//! # Why it streams instead of buffering
//!
//! `tftp_config.rs` fetches into a 2 KB array and parses it afterwards. That is
//! right for a layout YAML and impossible for a bitstream: a 128x64 E320 image
//! is ~426 KB, and this SoC's main RAM is for the framebuffer, not for holding a
//! whole second copy of the gateware. So each 512-byte TFTP block goes into a
//! one-sector staging buffer, and the sector is erased, programmed and verified
//! the moment the buffer fills. Peak RAM cost is one 4 KB sector.
//!
//! # Why the ACK comes after the flush
//!
//! TFTP's flow control is the ACK: the server sends the next block only once the
//! previous one is acknowledged. Erasing a sector takes tens of milliseconds and
//! programming it sixteen pages more, so ACKing first would invite the server to
//! send blocks we are too busy to receive, and they would be dropped and
//! retransmitted. ACKing *after* the flush makes the server wait for us, which
//! is exactly what the protocol is for. It costs nothing but wall-clock time.
//!
//! # Why this is safe to point at the running image
//!
//! The target is flash offset 0 -- the image the card booted from. Overwriting
//! it is safe because configuration is read into SRAM once at power-up and the
//! running design never reads flash for configuration again. And if anything
//! goes wrong at any point, the ECP5 falls back to the GOLDEN image at
//! `0x200000` on the next power-up: measured with a corrupted primary, a
//! corrupted primary built `--bootaddr 0`, and a primary whose first sector was
//! erased -- all three booted the golden and came up on the network.
//! `docs/FLASHING.md` section 2d.
//!
//! `flash_write::region_is_writable()` refuses the golden region
//! unconditionally and refuses an application write at all when no golden is
//! present, so the invariant is enforced one level down from here rather than
//! trusted to this state machine.
//!
//! # A FIRMWARE image is staged in RAM first
//!
//! The firmware copy is different: on a chain member it is the ONLY thing
//! that boots (no DHCP, so no netboot). Writing it sector by sector as blocks
//! arrive destroyed the old image within the first 4 KB, so any failure past
//! that point -- a TFTP timeout, the server restarting, a bad CRC at the end --
//! left a card that ran on from RAM and was dead at its next power cycle.
//!
//! So a firmware image is received WHOLE into the framebuffer's back half (a
//! 1.25 MB region nothing else touches while the update runs: the stream is
//! held and the program paused by the caller), its FBI length and CRC are
//! checked there, and only then is flash erased and written from RAM, one
//! sector per poll. Every network failure now leaves flash exactly as it was;
//! what remains is a power cut or reset during the local write, seconds long.
//!
//! # What this does NOT do
//!
//! It does not make the new bitstream take effect. Configuration is re-read on
//! power-up or a JTAG REFRESH, and this SoC exposes no self-reconfiguration
//! primitive, so a pushed bitstream is live at the next power cycle. That is
//! still no JTAG -- which was the point -- but it is not instant.

use smoltcp::socket::UdpSocket;
use smoltcp::wire::{IpEndpoint, Ipv4Address};

use crate::flash_write::{self, FlashError, PAGE, SECTOR};
use litex_pac as pac;

const TFTP_PORT: u16 = 6969;
const OPCODE_RRQ: u8 = 1;
const OPCODE_DATA: u8 = 3;
const OPCODE_ACK: u8 = 4;
const OPCODE_ERROR: u8 = 5;
const BLOCK: usize = 512;

/// Generous, because a flush between blocks can take a few hundred ms and the
/// deadline is re-armed per block, not per transfer.
const TIMEOUT_MS: i64 = 8000;

/// Local port. Distinct from `tftp_config`'s 6900 so a bitstream fetch and a
/// layout fetch can never land on the same socket.
const LOCAL_PORT: u16 = 6901;

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum State {
    Idle,
    SendRrq,
    WaitData,
    /// Firmware only: received and verified in RAM, now going into flash.
    Commit,
    /// Netboot only: received and verified in RAM, to be started in place of
    /// this firmware (chainload.rs). Flash is not touched.
    Staged,
    Done,
    Failed,
}

pub struct BitstreamLoader {
    pub state: State,
    server: Ipv4Address,
    server_tid: u16,
    block: u16,
    deadline_ms: i64,
    /// Generous, and bounded with an ERROR rather than a truncation.
    ///
    /// This was `[u8; 40]` and `start()` clamped with `.min()`, so
    /// "e320-256x256-icnd1065-hub320-out8-v1.1.0.bit" -- 44 characters, and the
    /// real name of a P1.25 bitstream -- lost its ".bit" and the card requested a
    /// file that does not exist. The push failed with `written: 0` and the only
    /// clue was a TFTP "not found" in the server log for a name that looked almost
    /// right. Silent truncation of an identifier is never the right failure.
    filename: [u8; 96],
    filename_len: usize,

    /// Where the next byte lands in flash.
    base: u32,
    /// Bytes accepted from the server so far.
    pub written: u32,
    /// Sectors erased+programmed+verified so far.
    pub sectors: u32,
    /// One sector in flight. The whole RAM cost of this module.
    stage: [u8; SECTOR as usize],
    stage_len: usize,

    pub error: Option<FlashError>,
    /// Set when the image did not begin with an ECP5 preamble. Refused BEFORE
    /// anything is erased -- a truncated .bit or a file that is not a bitstream
    /// at all would otherwise destroy a working image to install nothing.
    pub bad_preamble: bool,
    /// Writing the firmware copy (FBI image at FW_BASE) instead of the
    /// application bitstream.
    pub firmware: bool,
    /// Firmware staging area in RAM (see the module comment), and its size.
    ram: *mut u8,
    ram_cap: usize,
    /// Set when the image arrived whole but its FBI length or CRC was wrong:
    /// refused BEFORE flash was touched.
    pub bad_image: bool,
    /// Netboot: the image is started from RAM once verified, never written
    /// to flash (`POST /api/netboot`).
    pub netboot: bool,
}

impl BitstreamLoader {
    pub const fn new() -> Self {
        Self {
            state: State::Idle,
            server: Ipv4Address::UNSPECIFIED,
            server_tid: 0,
            block: 0,
            deadline_ms: 0,
            filename: [0u8; 96],
            filename_len: 0,
            base: flash_write::APP_BASE,
            written: 0,
            sectors: 0,
            stage: [0u8; SECTOR as usize],
            stage_len: 0,
            error: None,
            bad_preamble: false,
            firmware: false,
            ram: core::ptr::null_mut(),
            ram_cap: 0,
            bad_image: false,
            netboot: false,
        }
    }

    /// The FBI just written at FW_BASE: header length == bytes received - 8,
    /// and header crc32 == crc32 of the image as it reads back from flash.
    fn firmware_image_ok(&self) -> bool {
        let rd = |a: u32| -> u32 {
            u32::from_le_bytes([flash_write::read_byte(a), flash_write::read_byte(a + 1),
                                flash_write::read_byte(a + 2), flash_write::read_byte(a + 3)])
        };
        let len = rd(flash_write::FW_BASE);
        let crc = rd(flash_write::FW_BASE + 4);
        len > 0 && len + 8 == self.written
            && flash_write::crc32_region(flash_write::FW_BASE + 8, len) == crc
    }

    fn region_len(&self) -> u32 {
        if self.firmware { flash_write::FW_LEN } else { flash_write::APP_LEN }
    }

    /// Begin fetching an FBI-wrapped FIRMWARE image into the firmware copy.
    ///
    /// For a chain member this is the ONLY way to update firmware: it boots
    /// from flash every time. The BIOS checks the FBI header's length and
    /// CRC before it jumps, and this refuses an image whose length word is
    /// not plausible before erasing anything.
    ///
    /// `ram` is where the whole image is received and checked before flash is
    /// touched; the caller keeps every other writer off it until the loader is
    /// no longer active.
    pub fn start_firmware(&mut self, server: Ipv4Address, filename: &str,
                          ram: &'static mut [u8]) -> Result<(), FlashError> {
        flash_write::region_is_writable(flash_write::FW_BASE, flash_write::FW_LEN)?;
        if ram.len() < flash_write::FW_LEN as usize {
            return Err(FlashError::Forbidden);
        }
        self.firmware = true;
        let r = self.start_common(server, filename);
        self.base = flash_write::FW_BASE;
        self.ram = ram.as_mut_ptr();
        self.ram_cap = ram.len();
        r
    }

    /// Begin fetching an FBI-wrapped firmware image to START, not to write:
    /// the card's own netboot (chainload.rs). Received and checked exactly as
    /// for a flash write, in the same RAM; when it checks out the state is
    /// `Staged` and the caller hands over to it. Nothing is erased, so a card
    /// with no golden image (or a write-protected chip) can still do this.
    pub fn start_netboot(&mut self, server: Ipv4Address, filename: &str,
                         ram: &'static mut [u8]) -> Result<(), FlashError> {
        if ram.len() < flash_write::FW_LEN as usize {
            return Err(FlashError::Forbidden);
        }
        self.firmware = true;
        let r = self.start_common(server, filename);
        self.netboot = true;
        self.base = flash_write::FW_BASE;
        self.ram = ram.as_mut_ptr();
        self.ram_cap = ram.len();
        r
    }

    pub fn is_staged(&self) -> bool {
        self.state == State::Staged
    }

    /// The verified image (after its 8-byte FBI header) and its length.
    /// Only meaningful when `is_staged()`.
    pub fn staged_image(&self) -> (*const u8, usize) {
        let b = self.staged();
        if b.len() < 8 {
            return (core::ptr::null(), 0);
        }
        let len = u32::from_le_bytes([b[0], b[1], b[2], b[3]]) as usize;
        (b[8..].as_ptr(), len)
    }

    /// The staged image as received so far.
    fn staged(&self) -> &[u8] {
        if self.ram.is_null() {
            return &[];
        }
        // SAFETY: `ram` came from a `&'static mut [u8]` of `ram_cap` bytes in
        // start_firmware, and `written` never exceeds it.
        unsafe { core::slice::from_raw_parts(self.ram, (self.written as usize).min(self.ram_cap)) }
    }

    /// Is the staged image a whole, intact FBI? Checked in RAM, before flash.
    fn staged_ok(&self) -> bool {
        let b = self.staged();
        if b.len() < 9 {
            return false;
        }
        let len = u32::from_le_bytes([b[0], b[1], b[2], b[3]]);
        let crc = u32::from_le_bytes([b[4], b[5], b[6], b[7]]);
        len as usize + 8 == b.len() && flash_write::crc32_bytes(&b[8..]) == crc
    }

    /// Erase, program and verify ONE sector of the staged firmware image.
    fn commit_sector(&mut self, spi: &pac::SpiflashMmap) -> Result<(), FlashError> {
        let off = (self.sectors * SECTOR) as usize;
        let n = (self.written as usize - off).min(SECTOR as usize);
        // SAFETY: `ram` holds `written` staged bytes (see staged()), and
        // off + n <= written.
        let src = unsafe { core::slice::from_raw_parts(self.ram.add(off), n) };
        self.stage[..n].copy_from_slice(src);
        self.stage_len = n;
        self.flush(spi)
    }

    /// Begin fetching `filename` and writing it to the application region.
    ///
    /// Refuses up front if the region is not writable -- which includes "no
    /// golden image present" -- so a card with no safety net never starts an
    /// erase it cannot recover from.
    pub fn start(&mut self, server: Ipv4Address, filename: &str) -> Result<(), FlashError> {
        flash_write::region_is_writable(flash_write::APP_BASE, flash_write::APP_LEN)?;
        self.firmware = false;
        self.start_common(server, filename)
    }

    fn start_common(&mut self, server: Ipv4Address, filename: &str) -> Result<(), FlashError> {
        self.server = server;
        self.server_tid = 0;
        self.block = 1;
        self.base = flash_write::APP_BASE;
        self.written = 0;
        self.sectors = 0;
        self.stage_len = 0;
        self.error = None;
        self.bad_preamble = false;
        self.bad_image = false;
        self.netboot = false;
        self.ram = core::ptr::null_mut();
        self.ram_cap = 0;
        // Refuse rather than clamp: a truncated filename asks the server for the
        // wrong file, and the failure surfaces as an unexplained empty write.
        if filename.len() > self.filename.len() {
            return Err(FlashError::NameTooLong);
        }
        self.filename[..filename.len()].copy_from_slice(filename.as_bytes());
        self.filename_len = filename.len();
        self.state = State::SendRrq;
        Ok(())
    }

    pub fn is_active(&self) -> bool {
        matches!(self.state, State::SendRrq | State::WaitData | State::Commit)
    }

    pub fn is_done(&self) -> bool {
        self.state == State::Done
    }

    /// Progress as a percentage of the application region, for a status page.
    /// Deliberately against `APP_LEN` and not the file size: TFTP does not tell
    /// us the length up front, so a percentage of the file is not knowable.
    pub fn percent_of_region(&self) -> u32 {
        self.written / (self.region_len() / 100).max(1)
    }

    /// Erase the sector the staged bytes belong to, program it, and verify.
    ///
    /// Pads a final short sector with 0xFF -- the erased value -- so the tail of
    /// the image reads exactly as a freshly erased chip would. An ECP5 stops at
    /// the end of its own bitstream, so trailing 0xFF is inert.
    fn flush(&mut self, spi: &pac::SpiflashMmap) -> Result<(), FlashError> {
        if self.stage_len == 0 {
            return Ok(());
        }
        let addr = self.base + self.sectors * SECTOR;
        for b in self.stage[self.stage_len..].iter_mut() {
            *b = 0xFF;
        }

        flash_write::erase_sector(spi, addr)?;
        let mut off = 0u32;
        while off < SECTOR {
            let n = PAGE.min(SECTOR - off) as usize;
            flash_write::program_page(
                spi,
                addr + off,
                &self.stage[off as usize..off as usize + n],
            )?;
            off += n as u32;
        }

        // program_page already verifies its own write, so this is not a second
        // read-back of the same bytes; it is a check that the sector as a whole
        // reads back as the image, which catches a page silently skipped.
        for i in 0..self.stage_len {
            if flash_write::read_byte(addr + i as u32) != self.stage[i] {
                return Err(FlashError::Mismatch);
            }
        }

        self.sectors += 1;
        self.stage_len = 0;
        Ok(())
    }

    /// Poll the state machine. Returns true on the transition to Done.
    pub fn poll(
        &mut self,
        socket: &mut UdpSocket,
        spi: &pac::SpiflashMmap,
        time_ms: i64,
    ) -> bool {
        match self.state {
            State::SendRrq => {
                if !socket.is_open() && socket.bind(crate::chain::bitstream_port()).is_err() {
                    self.state = State::Failed;
                    return false;
                }
                if socket.can_send() {
                    let name = &self.filename[..self.filename_len];
                    let mode = b"octet";
                    // Must hold the longest request this can make:
                    // opcode(2) + filename + NUL + "octet" + NUL. With the
                    // filename buffer at 96 that is 105 bytes; at the old 56 a
                    // long name would have panicked the firmware on the
                    // copy_from_slice rather than merely failing the fetch.
                    let mut pkt = [0u8; 112];
                    debug_assert!(2 + self.filename.len() + 1 + 5 + 1 <= pkt.len());
                    pkt[1] = OPCODE_RRQ;
                    let mut pos = 2;
                    pkt[pos..pos + name.len()].copy_from_slice(name);
                    pos += name.len();
                    pos += 1; // NUL
                    pkt[pos..pos + mode.len()].copy_from_slice(mode);
                    pos += mode.len();
                    pos += 1; // NUL
                    let ep = IpEndpoint::new(self.server.into(), TFTP_PORT);
                    if socket.send_slice(&pkt[..pos], ep).is_ok() {
                        self.deadline_ms = time_ms + TIMEOUT_MS;
                        self.state = State::WaitData;
                    }
                }
            }

            State::WaitData => {
                if time_ms > self.deadline_ms {
                    socket.close();
                    self.state = State::Failed;
                    return false;
                }
                if !socket.can_recv() {
                    return false;
                }

                // recv() borrows the socket's buffer, so copy what is needed out
                // of it before doing anything that wants the socket again --
                // including the flush, which must not happen under that borrow.
                let mut opcode = 0u8;
                let mut blk = 0u16;
                let mut blk_be = [0u8; 2];
                let mut port = 0u16;
                let mut addr = smoltcp::wire::IpAddress::Ipv4(Ipv4Address::UNSPECIFIED);
                let mut payload = [0u8; BLOCK];
                let mut payload_len = 0usize;
                let mut got = false;

                if let Ok((data, ep)) = socket.recv() {
                    if data.len() >= 4 {
                        opcode = data[1];
                        blk = u16::from_be_bytes([data[2], data[3]]);
                        blk_be = [data[2], data[3]];
                        port = ep.port;
                        addr = ep.addr;
                        let body = &data[4..];
                        payload_len = body.len().min(BLOCK);
                        payload[..payload_len].copy_from_slice(&body[..payload_len]);
                        got = true;
                    }
                }
                if !got {
                    return false;
                }

                if opcode == OPCODE_ERROR {
                    socket.close();
                    self.state = State::Failed;
                    return false;
                }
                if opcode != OPCODE_DATA || blk != self.block {
                    return false; // duplicate or out of order; let the server retry
                }
                if self.server_tid == 0 {
                    self.server_tid = port;
                }

                // Refuse a file that is not a bitstream before touching flash.
                // The first block is the only chance to do this cheaply, and
                // getting it wrong means erasing a working image to install
                // garbage.
                if self.written == 0 {
                    let ok = if self.firmware {
                        // FBI: u32 LE length of the image that follows, then
                        // its crc32. A length of zero or past the region is
                        // not a firmware image.
                        payload_len >= 8 && {
                            let n = u32::from_le_bytes([payload[0], payload[1], payload[2], payload[3]]);
                            n > 0 && n <= flash_write::FW_LEN - 8
                        }
                    } else {
                        payload[..payload_len.min(8)].windows(2).any(|w| w == [0xBD, 0xB3])
                    };
                    if !ok {
                        self.bad_preamble = true;
                        socket.close();
                        self.state = State::Failed;
                        return false;
                    }
                }

                let last = payload_len < BLOCK;
                if self.firmware {
                    // Into RAM only. Flash is untouched until the whole image
                    // is here and checks out.
                    let at = self.written as usize;
                    if at + payload_len > self.ram_cap
                        || at + payload_len > self.region_len() as usize {
                        self.error = Some(FlashError::Forbidden);
                        socket.close();
                        self.state = State::Failed;
                        return false;
                    }
                    // SAFETY: bounds checked against ram_cap just above.
                    unsafe {
                        core::slice::from_raw_parts_mut(self.ram.add(at), payload_len)
                            .copy_from_slice(&payload[..payload_len]);
                    }
                    self.written += payload_len as u32;
                    let ack = [0, OPCODE_ACK, blk_be[0], blk_be[1]];
                    socket.send_slice(&ack, IpEndpoint::new(addr, self.server_tid)).ok();
                    if last {
                        if !self.staged_ok() {
                            // Truncated, padded or corrupted on the way: the
                            // old image is still in flash, intact.
                            self.bad_image = true;
                            self.error = None;
                            self.state = State::Failed;
                            return true;
                        }
                        if self.netboot {
                            self.state = State::Staged;
                            return true;
                        }
                        self.sectors = 0;
                        self.state = State::Commit;
                        return false;
                    }
                    self.block = self.block.wrapping_add(1);
                    self.deadline_ms = time_ms + TIMEOUT_MS;
                    return false;
                }

                let take = payload_len.min(SECTOR as usize - self.stage_len);
                self.stage[self.stage_len..self.stage_len + take]
                    .copy_from_slice(&payload[..take]);
                self.stage_len += take;
                self.written += take as u32;

                if self.written > self.region_len() {
                    self.error = Some(FlashError::Forbidden);
                    socket.close();
                    self.state = State::Failed;
                    return false;
                }

                if self.stage_len == SECTOR as usize || last {
                    if let Err(e) = self.flush(spi) {
                        self.error = Some(e);
                        socket.close();
                        self.state = State::Failed;
                        return false;
                    }
                }
                // A 512-byte block cannot overflow a 4 KB sector, so `take` is
                // always the whole payload and nothing is left unconsumed.

                let ack = [0, OPCODE_ACK, blk_be[0], blk_be[1]];
                let ack_ep = IpEndpoint::new(addr, self.server_tid);
                socket.send_slice(&ack, ack_ep).ok();

                if last {
                    // Do not close() here: it resets the TX buffer and would
                    // drop the ACK queued above, leaving the server retrying a
                    // transfer that actually finished. The caller closes.
                    self.state = State::Done;
                    return true;
                }
                self.block = self.block.wrapping_add(1);
                self.deadline_ms = time_ms + TIMEOUT_MS;
            }

            State::Commit => {
                // One sector per poll: an erase is tens of ms, and this runs
                // in the network ISR.
                if let Err(e) = self.commit_sector(spi) {
                    self.error = Some(e);
                    self.state = State::Failed;
                    return true;
                }
                if self.sectors * SECTOR >= self.written {
                    // And the image as it now reads back from flash -- through
                    // the mmap window, a different path from the SPI master
                    // that wrote it.
                    if !self.firmware_image_ok() {
                        self.error = Some(FlashError::Mismatch);
                        self.state = State::Failed;
                    } else {
                        self.state = State::Done;
                    }
                    return true;
                }
            }

            State::Idle | State::Done | State::Failed | State::Staged => {}
        }
        false
    }

    /// One line for `/api/status`, so a stuck update is diagnosable without a
    /// serial console -- which this board does not have wired.
    pub fn state_str(&self) -> &'static str {
        match self.state {
            State::Idle => "idle",
            State::SendRrq => "requesting",
            State::WaitData if self.firmware => "receiving",
            State::WaitData => "writing",
            State::Commit => "writing",
            State::Done => "done",
            State::Staged => "staged; starting it",
            State::Failed if self.bad_preamble && self.firmware => "failed: not an FBI firmware image",
            State::Failed if self.bad_preamble => "failed: not a bitstream",
            State::Failed if self.bad_image && self.netboot => "failed: image length or CRC wrong; still running this one",
            State::Failed if self.bad_image => "failed: image length or CRC wrong; flash not touched",
            State::Failed => "failed",
        }
    }
}

pub static mut LOADER: BitstreamLoader = BitstreamLoader::new();
