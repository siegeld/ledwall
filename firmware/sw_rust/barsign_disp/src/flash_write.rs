//! Writing SPI flash from the running firmware, so a card never needs JTAG.
//!
//! RB-5554. Today every change to what is *on* a card needs a programmer: a
//! ladder for a card in a wall, and a soldering iron for a `5A-75E`, which has
//! no JTAG header fitted. JTAG is also the least reliable thing on this bench
//! (`docs/HARDWARE.md` section 7, and `CLAUDE.md`). This module is the first
//! half of removing it from the loop.
//!
//! # What was already here
//!
//! Almost all of it, which is why this is small:
//!
//! * `flash_id::write_status()` already issues `0x06` WRITE ENABLE and polls
//!   SR1 for BUSY, in production use. Raw bidirectional SPI from the running
//!   SoC is proven, not hoped for.
//! * The flash is **memory-mapped** at `MMAP_BASE`, `cached=False`, so reading
//!   it back to verify is volatile loads -- no command sequencing, and no
//!   cache coherency problem to get wrong.
//! * The firmware runs from SDRAM (the BIOS copies it out of flash), so
//!   erasing flash while the card is live is safe. It is not running from the
//!   thing it is erasing.
//!
//! Missing, and added here: sector erase, page program, and a job that can run
//! them without wedging the network.
//!
//! # Timing is the design constraint, not the SPI
//!
//! A 4 KB sector erase on a GD25Q32 is tens of milliseconds typical and
//! hundreds worst case, and a full bitstream is ~110 sectors. **HTTP handlers
//! run inside the network ISR**, so none of this can happen in one: it would
//! stall the interrupt for a minute and drop the link.
//!
//! So the house pattern applies -- the same one `reboot_tick()` uses, for the
//! same reason: the HTTP handler *queues*, the main loop *performs*, and a
//! second request collects the result. `tick()` does **one sector per call**
//! so the main loop keeps breathing. Interrupts stay enabled throughout, so
//! the network survives an update; what suffers is the display, which is an
//! acceptable thing to lose while a card is being reflashed.
//!
//! # The guard rail is the point
//!
//! A bad firmware is recoverable -- the BIOS is netboot-first, so a card with
//! a corrupt flashed firmware still comes up. **A bad bitstream is not**: a
//! card that fails to configure has no CPU, no network and no way back except
//! a programmer. That asymmetry is why this module refuses by default and
//! permits by exception, rather than the other way round.
//!
//! Until the A/B slot layout exists (RB-5554 step 3), the only writable region
//! is `SCRATCH`, which holds nothing and boots nothing. Widening this is a
//! deliberate act, not a parameter.

use litex_pac as pac;

use crate::flash_id::spi_transfer_byte;

/// Memory-mapped window onto the flash. Matches the `spiflash` region origin
/// in `gateware/colorlight.py`; if that moves, this must move with it.
pub const MMAP_BASE: usize = 0x8040_0000;

/// GD25Q32: 4 MB, 4 KB sectors, 256-byte pages. Declared in the gateware as
/// `GD25Q32` after the part was mis-declared twice -- see `docs/HARDWARE.md`
/// section 1.
pub const FLASH_SIZE: u32 = 4 * 1024 * 1024;
pub const SECTOR: u32 = 4096;
pub const PAGE: u32 = 256;

const CMD_WRITE_ENABLE: u8 = 0x06;
const CMD_READ_STATUS1: u8 = 0x05;
const CMD_PAGE_PROGRAM: u8 = 0x02;
const CMD_SECTOR_ERASE: u8 = 0x20;
const CMD_READ: u8 = 0x03;
const CMD_READ_STATUS2: u8 = 0x35;
const CMD_WRITE_STATUS1: u8 = 0x01;

/// Scratch region: the ONLY thing this firmware will write today.
///
/// 0x360000..0x380000 -- 128 KB that holds nothing, boots nothing, and is
/// clear of this bench card's marginal silicon at 0x2C0000..0x2FFFFF (which
/// reads differently on every attempt; see RB-5554). Chosen so that every
/// erase/program experiment is survivable without a programmer.
pub const SCRATCH_BASE: u32 = 0x36_0000;
pub const SCRATCH_LEN: u32 = 0x2_0000;

// ---------------------------------------------------------------------------
// Bitstream layout for over-the-air updates.
//
// CORRECTED 2026-10-07. This module used to be built on the idea that the card
// boots via a multiboot JUMP descriptor at 0x3FFF10, so an update meant writing
// the slot the card was NOT running and then repointing the descriptor. That
// model is wrong and the code implementing it is gone.
//
// What the hardware actually does, measured (docs/FLASHING.md §2b, §2d):
//
//   * The ECP5 starts reading flash at OFFSET 0 on power-up and on REFRESH. A
//     bitstream there is what boots. The descriptor at 0x3FFF10 is a redirect
//     taken BY a primary image and is no substitute for having one at 0.
//   * If configuration from offset 0 FAILS, the ECP5 scans forward through
//     flash, finds the next valid bitstream, and loads that instead -- with no
//     JTAG and no intervention. Verified three ways: a corrupted primary, a
//     corrupted primary built with --bootaddr 0, and a primary whose first
//     sector was erased. All three booted the image at 0x200000 and came up on
//     the network.
//
// So the fail-safe is not a pointer, it is a GOLDEN image parked where the scan
// will find it:
//
//     0x000000  application bitstream   <- OTA rewrites this
//     0x100000  firmware copy
//     0x200000  GOLDEN bitstream        <- written ONCE over JTAG, never again
//
// The single invariant: NOTHING may ever write the golden region. Everything
// else is recoverable, because a card that fails to configure its application
// boots the golden, joins the network, and can be pushed another image.
//
// Writing the application region while RUNNING from it is safe, and that is
// what makes this work at all: configuration is read once at power-up into
// SRAM, and the running design never reads flash for configuration again. The
// new image takes effect at the next power cycle.
pub const APP_BASE: u32 = 0x00_0000;
pub const APP_LEN: u32 = 0x10_0000;        // up to the firmware copy at 0x100000

/// The recovery image. **Never writable.** See the invariant above.
pub const GOLDEN_BASE: u32 = 0x20_0000;
pub const GOLDEN_LEN: u32 = 0x0B_0000;     // up to the third factory copy

/// **Never write here.** `0x2C0000`-`0x2FFFFF` reads differently on every
/// attempt on this part and no amount of retrying converges -- measured across
/// nine full-chip dumps (`factory/README.md`). It holds the tail of a redundant
/// third copy of the factory primary, so nothing unique lives there, but a
/// verify against it can never pass and an OTA that lands in it would look
/// like a corrupt write forever.
pub const MARGINAL_BASE: u32 = 0x2C_0000;
pub const MARGINAL_LEN: u32 = 0x4_0000;

/// Does flash offset 0 currently hold something that looks like a bitstream?
///
/// Only a preamble check -- it says "the ECP5 will try this", not "this is
/// good". Cheap, and the honest limit of what can be known without configuring.
pub fn app_has_preamble() -> bool {
    // A payload begins ff ff ff bd b3; the sync word is bd b3 after a run of
    // 0xff, so scan the first few bytes rather than demanding a fixed offset.
    for off in 0..8u32 {
        if read_byte(APP_BASE + off) == 0xBD && read_byte(APP_BASE + off + 1) == 0xB3 {
            return true;
        }
    }
    false
}

/// Is the golden image present? If this is false the card has no safety net and
/// an application write would be a genuine brick risk, so callers should refuse.
pub fn golden_present() -> bool {
    for off in 0..8u32 {
        if read_byte(GOLDEN_BASE + off) == 0xBD && read_byte(GOLDEN_BASE + off + 1) == 0xB3 {
            return true;
        }
    }
    false
}

/// Why a write was refused. Returned rather than panicking: a panic here takes
/// the panel down, and `panic.rs` has its own history on this board.
#[derive(Copy, Clone, PartialEq, Eq)]
pub enum FlashError {
    /// Outside the permitted region. The default answer.
    Forbidden,
    /// Not on a sector boundary, or not a whole number of sectors.
    Unaligned,
    /// The flash never cleared BUSY. A wedged part, or a missing supply.
    Timeout,
    /// Read-back did not match what was written.
    Mismatch,
    /// No golden image is present, so an application write has no safety net.
    NoGolden,
    /// The requested filename does not fit the request buffer. Refused rather
    /// than truncated, because a truncated name fetches the wrong file.
    NameTooLong,
    /// The erase reported success but the region did not read back blank.
    NotErased,
    /// The program reported success but the first byte did not read back.
    NotWritten,
    /// Asked to commit an update that was never read back and verified.
    NotVerified,
}

impl FlashError {
    pub fn as_str(self) -> &'static str {
        match self {
            FlashError::Forbidden => "forbidden: outside the writable region",
            FlashError::Unaligned => "address or length is not sector-aligned",
            FlashError::Timeout => "flash stayed busy",
            FlashError::Mismatch => "read-back did not match",
            FlashError::NotErased => "erase did not take (chip still protected?)",
            FlashError::NotWritten => "program did not take (chip still protected?)",
            FlashError::NotVerified => "refusing to accept an unverified image",
            FlashError::NoGolden => "no golden bitstream at 0x200000: refusing to write the application without a recovery image",
            FlashError::NameTooLong => "bitstream filename too long for the request buffer",
        }
    }
}

/// The single policy decision in this module.
///
/// The rule is the inverse of what it used to be, and the inversion is the
/// point. The old rule was "never the slot the card is running", which assumed
/// a pointer chose the boot image. There is no such choice: the card boots
/// offset 0, so an application update MUST overwrite the image it is running.
/// That is safe because configuration was read into SRAM at power-up and the
/// running design never reads flash for configuration again.
///
/// What must be protected instead is the GOLDEN image, because it is the only
/// thing standing between a bad application write and a card that needs a
/// cable. Golden is refused unconditionally -- there is no flag, no override
/// and no caller that may write it.
pub fn region_is_writable(addr: u32, len: u32) -> Result<(), FlashError> {
    let end = addr.saturating_add(len);
    if end > FLASH_SIZE || len == 0 {
        return Err(FlashError::Forbidden);
    }
    if addr % SECTOR != 0 || len % SECTOR != 0 {
        return Err(FlashError::Unaligned);
    }

    // The golden image. Never, under any circumstances.
    if addr < GOLDEN_BASE + GOLDEN_LEN && end > GOLDEN_BASE {
        return Err(FlashError::Forbidden);
    }

    // The marginal region: a verify there can never pass, so an OTA landing in
    // it would read as a permanently corrupt write.
    if addr < MARGINAL_BASE + MARGINAL_LEN && end > MARGINAL_BASE {
        return Err(FlashError::Forbidden);
    }

    // The application bitstream region -- the thing OTA exists to replace.
    // Refused outright when no golden image is present, because without a
    // safety net this write is the one operation here that can really brick the
    // card.
    if addr >= APP_BASE && end <= APP_BASE + APP_LEN {
        if !golden_present() {
            return Err(FlashError::NoGolden);
        }
        return Ok(());
    }

    // The scratch region, for erase/program self-tests.
    if addr < SCRATCH_BASE || end > SCRATCH_BASE + SCRATCH_LEN {
        return Err(FlashError::Forbidden);
    }
    Ok(())
}

fn configure_master(spi: &pac::SpiflashMmap) {
    unsafe {
        spi.master_phyconfig()
            .write(|w| w.len().bits(8).width().bits(1).mask().bits(1));
    }
}

fn write_enable(spi: &pac::SpiflashMmap) {
    spi.master_cs().write(|w| w.master_cs().set_bit());
    spi_transfer_byte(spi, CMD_WRITE_ENABLE);
    spi.master_cs().write(|w| w.master_cs().clear_bit());
}

/// Poll SR1 until BUSY clears. **Bounded**, like every other wait on this
/// board: `panic.rs` once spun forever on a UART FIFO that nothing drained,
/// on a card with no serial console, so a hang here would be a repeat of a
/// bug this repo has already paid for.
fn read_sr1(spi: &pac::SpiflashMmap) -> u8 {
    spi.master_cs().write(|w| w.master_cs().set_bit());
    spi_transfer_byte(spi, CMD_READ_STATUS1);
    let v = spi_transfer_byte(spi, 0x00);
    spi.master_cs().write(|w| w.master_cs().clear_bit());
    v
}

pub fn read_sr2(spi: &pac::SpiflashMmap) -> u8 {
    configure_master(spi);
    spi.master_cs().write(|w| w.master_cs().set_bit());
    spi_transfer_byte(spi, CMD_READ_STATUS2);
    let v = spi_transfer_byte(spi, 0x00);
    spi.master_cs().write(|w| w.master_cs().clear_bit());
    v
}

/// Clear the GD25Q32's block-protection bits so an erase or program can take.
///
/// **Without this nothing is written and NOTHING reports an error.** Measured
/// on the bench card 2026-10-04: a self-test job ran a sector erase plus
/// sixteen page programs, every `wait_not_busy` returned immediately, the job
/// reported `done` with a CRC -- and the region read back blank. BUSY is never
/// set because a protected chip refuses the operation outright rather than
/// starting it, so a busy-poll is not a check that anything happened.
///
/// This is also why the JTAG tools appear to behave differently: `ecpprog -p`
/// and `openFPGALoader --unprotect-flash` clear these bits for us, and the
/// firmware had no equivalent. docs/FLASHING.md.
///
/// SR1 bits 2..4 are BP0..BP2 and bit 7 is SRP0. Writing 0x00 clears all of
/// them. Returns the previous SR1 so a caller can tell whether the chip had
/// been protected.
pub fn unprotect(spi: &pac::SpiflashMmap) -> Result<u8, FlashError> {
    configure_master(spi);
    let before = read_sr1(spi);
    if before & 0b1001_1100 == 0 {
        return Ok(before); // already writable; do not spend a SR write cycle
    }
    write_enable(spi);
    spi.master_cs().write(|w| w.master_cs().set_bit());
    spi_transfer_byte(spi, CMD_WRITE_STATUS1);
    spi_transfer_byte(spi, 0x00);
    spi.master_cs().write(|w| w.master_cs().clear_bit());
    wait_not_busy(spi, 100)?;
    Ok(before)
}

/// Milliseconds since the 1-second countdown timer last reloaded.
///
/// NOT `mcycle`: `csrr 0xB00` reads back 0 on this VexRiscv build, which is why
/// every cycle count `/api/bench` ever printed was zero. Timer0 is real
/// hardware. It wraps once a second, which `wait_not_busy` accounts for.
#[inline(always)]
fn timer_ticks() -> u32 {
    unsafe {
        let t = &*pac::Timer0::ptr();
        t.update_value().write(|w| w.bits(1));
        (crate::network::SYS_CLK_HZ - 1) - t.value().read().bits()
    }
}

/// Poll SR1 until BUSY clears, bounded by REAL TIME rather than a spin count.
///
/// The first version counted to 8,000,000 iterations. Each iteration is a
/// four-register SPI exchange, so that bound is tens of seconds of blocked
/// main loop -- long enough that the card answers ICMP (interrupt-driven) and
/// stops answering HTTP, which is exactly how it looked when it went quiet on
/// the bench. A wait must be bounded by something a human can reason about.
///
/// GD25Q32 datasheet maxima: page program 3 ms, 4 KB sector erase 300 ms.
fn wait_not_busy(spi: &pac::SpiflashMmap, timeout_ms: u32) -> Result<(), FlashError> {
    let per_ms = crate::network::SYS_CLK_HZ / 1000;
    let budget = timeout_ms.saturating_mul(per_ms);
    let start = timer_ticks();
    let mut spent: u32 = 0;
    let mut last = start;
    loop {
        if read_sr1(spi) & 1 == 0 {
            return Ok(());
        }
        let now = timer_ticks();
        // The timer wraps every second; accumulate deltas instead of
        // subtracting raw values, which would go negative across a reload.
        spent = spent.saturating_add(if now >= last {
            now - last
        } else {
            now + (crate::network::SYS_CLK_HZ - 1) - last
        });
        last = now;
        if spent > budget {
            return Err(FlashError::Timeout);
        }
    }
}

fn send_addr(spi: &pac::SpiflashMmap, addr: u32) {
    spi_transfer_byte(spi, (addr >> 16) as u8);
    spi_transfer_byte(spi, (addr >> 8) as u8);
    spi_transfer_byte(spi, addr as u8);
}

/// Erase one 4 KB sector. Blocks until the part reports done.
pub fn erase_sector(spi: &pac::SpiflashMmap, addr: u32) -> Result<(), FlashError> {
    region_is_writable(addr, SECTOR)?;
    unprotect(spi)?;
    configure_master(spi);
    write_enable(spi);
    spi.master_cs().write(|w| w.master_cs().set_bit());
    spi_transfer_byte(spi, CMD_SECTOR_ERASE);
    send_addr(spi, addr);
    spi.master_cs().write(|w| w.master_cs().clear_bit());
    wait_not_busy(spi, 400)?;
    // A protected chip refuses the erase without ever going BUSY, so the poll
    // above proves nothing on its own. Confirm against the device.
    if !region_is_blank(addr, SECTOR) {
        return Err(FlashError::NotErased);
    }
    Ok(())
}

pub fn program_page(
    spi: &pac::SpiflashMmap,
    addr: u32,
    data: &[u8],
) -> Result<(), FlashError> {
    if data.is_empty() {
        return Ok(());
    }
    // A page program that crosses a page boundary WRAPS to the start of the
    // same page rather than continuing -- a silent corruption, and the classic
    // SPI-NOR mistake. Refuse instead.
    if data.len() as u32 > PAGE || (addr % PAGE) + data.len() as u32 > PAGE {
        return Err(FlashError::Unaligned);
    }
    region_is_writable(addr & !(SECTOR - 1), SECTOR)?;

    unprotect(spi)?;
    configure_master(spi);
    write_enable(spi);
    spi.master_cs().write(|w| w.master_cs().set_bit());
    spi_transfer_byte(spi, CMD_PAGE_PROGRAM);
    send_addr(spi, addr);
    for b in data {
        spi_transfer_byte(spi, *b);
    }
    spi.master_cs().write(|w| w.master_cs().clear_bit());
    wait_not_busy(spi, 10)?;
    // Read ONE byte back through the mmap window. A silent no-op write is the
    // failure this whole module exists to avoid, and it has already happened
    // once on hardware.
    if data[0] != 0xFF && read_byte(addr) != data[0] {
        return Err(FlashError::NotWritten);
    }
    Ok(())
}

/// Read a byte back through the memory-mapped window.
///
/// Reading does NOT go through the master interface: the flash is mapped at
/// `MMAP_BASE` with `cached=False`, so a volatile load fetches from the device
/// every time. That also makes verification immune to the short-read bit-slip
/// that afflicts JTAG dumps of this board -- a different path entirely.
#[inline(always)]
pub fn read_byte(addr: u32) -> u8 {
    unsafe { core::ptr::read_volatile((MMAP_BASE + addr as usize) as *const u8) }
}

/// CRC-32 (IEEE, reflected) over a flash region, read through the mmap window.
///
/// Nibble-table rather than a 256-entry one: a 1 KB table is real space on a
/// board whose firmware is copied out of flash at every boot, and the flash
/// read dominates the cost anyway.
pub fn crc32_region(addr: u32, len: u32) -> u32 {
    const TBL: [u32; 16] = [
        0x0000_0000, 0x1db7_1064, 0x3b6e_20c8, 0x26d9_30ac,
        0x76dc_4190, 0x6b6b_51f4, 0x4db2_6158, 0x5005_713c,
        0xedb8_8320, 0xf00f_9344, 0xd6d6_a3e8, 0xcb61_b38c,
        0x9b64_c2b0, 0x86d3_d2d4, 0xa00a_e278, 0xbdbd_f21c,
    ];
    let mut crc: u32 = 0xFFFF_FFFF;
    for i in 0..len {
        let b = read_byte(addr + i);
        crc ^= b as u32;
        crc = (crc >> 4) ^ TBL[(crc & 0x0F) as usize];
        crc = (crc >> 4) ^ TBL[(crc & 0x0F) as usize];
    }
    !crc
}

/// Is every byte in this region erased (0xFF)? Cheap post-erase check that
/// does not need the host to send anything.
pub fn region_is_blank(addr: u32, len: u32) -> bool {
    for i in 0..len {
        if read_byte(addr + i) != 0xFF {
            return false;
        }
    }
    true
}

// ---------------------------------------------------------------------------
// The deferred job
// ---------------------------------------------------------------------------
//
// An HTTP handler may only touch the fields below. `tick()` is the only thing
// that talks to the flash, and it runs from the main loop with interrupts on.

#[derive(Copy, Clone, PartialEq, Eq)]
pub enum JobKind {
    None,
    /// Erase `len` bytes from `addr`, one sector per tick.
    Erase,
    /// Fill `len` bytes from `addr` with a generated pattern, so write and
    /// verify can be exercised end to end without a host sending data.
    SelfTest,
}

#[derive(Copy, Clone, PartialEq, Eq)]
pub enum JobState {
    Idle,
    Queued,
    Running,
    Done,
    Failed,
}

pub struct Job {
    pub kind: JobKind,
    pub state: JobState,
    pub addr: u32,
    pub len: u32,
    /// Bytes completed so far, so a caller can show progress rather than hang.
    pub cursor: u32,
    pub error: Option<FlashError>,
    /// CRC of the region once the job finishes, for the caller to compare.
    pub crc: u32,
    /// Whether the sector currently being filled has already been erased, so
    /// the tick does one erase per sector rather than one per page.
    pub erased: bool,
}

pub static mut JOB: Job = Job {
    kind: JobKind::None,
    state: JobState::Idle,
    addr: 0,
    len: 0,
    cursor: 0,
    error: None,
    crc: 0,
    erased: false,
};

/// The byte a self-test writes at a given offset.
///
/// Deliberately position-dependent and non-uniform: a uniform fill verifies
/// clean even if the read path is slipping bytes, which is precisely how a
/// JTAG dump of this card fooled a whole session. A pattern that changes every
/// byte cannot pass a shifted read.
#[inline(always)]
pub fn selftest_byte(offset: u32) -> u8 {
    (offset ^ (offset >> 8) ^ 0x5A) as u8
}

/// Queue a job. Called from an HTTP handler; does no flash I/O itself.
pub fn queue(kind: JobKind, addr: u32, len: u32) -> Result<(), FlashError> {
    region_is_writable(addr, len)?;
    unsafe {
        if JOB.state == JobState::Queued || JOB.state == JobState::Running {
            return Err(FlashError::Forbidden);
        }
        JOB = Job {
            kind,
            state: JobState::Queued,
            addr,
            len,
            cursor: 0,
            error: None,
            crc: 0,
            erased: false,
        };
    }
    Ok(())
}

/// Is there anything for `tick()` to do?
///
/// A plain read of one static, and the ONLY thing the main loop should call
/// when idle. The first version of this hooked `tick()` into the loop
/// unconditionally, which meant `Peripherals::steal()` ran thousands of times
/// a second; the card came up, configured fine, and then went silent on the
/// network. Gate the peripheral access, not just the work.
#[inline(always)]
pub fn job_pending() -> bool {
    unsafe {
        matches!(
            core::ptr::addr_of!(JOB).read().state,
            JobState::Queued | JobState::Running
        )
    }
}

/// Do a bounded slice of the queued job. Call from the main loop.
///
/// One sector per call. An erase blocks for tens of milliseconds inside this
/// function, which is why it must not be reached from the ISR -- but
/// interrupts stay enabled, so the network keeps running and the card stays
/// reachable for the whole update. The display stutters; that is the trade.
pub unsafe fn tick(spi: &pac::SpiflashMmap) {
    let job = &mut *core::ptr::addr_of_mut!(JOB);
    match job.state {
        JobState::Queued => {
            job.state = JobState::Running;
        }
        JobState::Running => {}
        _ => return,
    }

    if job.cursor >= job.len {
        job.crc = crc32_region(job.addr, job.len);
        job.state = JobState::Done;
        return;
    }

    let addr = job.addr + job.cursor;
    // ONE unit of work per tick -- a sector erase OR a single page program.
    // The first version did a full erase plus sixteen page programs in one
    // call, seventeen busy-waits deep, which blocked the main loop long enough
    // that the card answered ICMP and stopped answering HTTP. The main loop is
    // the network; do not hold it.
    let result = match job.kind {
        JobKind::Erase => erase_sector(spi, addr).map(|()| SECTOR),
        JobKind::SelfTest => {
            if addr % SECTOR == 0 && !job.erased {
                erase_sector(spi, addr).map(|()| {
                    job.erased = true;
                    0 // nothing programmed yet; the next tick does a page
                })
            } else {
                // Clamp to the request: a 4 KB job must not program a whole
                // sector just because that is the erase granularity.
                let remaining = job.len - job.cursor;
                let n = core::cmp::min(PAGE, remaining);
                let mut buf = [0u8; PAGE as usize];
                for (i, b) in buf.iter_mut().enumerate().take(n as usize) {
                    *b = selftest_byte(job.cursor + i as u32);
                }
                program_page(spi, addr, &buf[..n as usize]).map(|()| {
                    if (addr + n) % SECTOR == 0 {
                        job.erased = false; // next sector needs its own erase
                    }
                    n
                })
            }
        }
        JobKind::None => Ok(0),
    };

    match result {
        Ok(advance) => job.cursor += advance,
        Err(e) => {
            job.error = Some(e);
            job.state = JobState::Failed;
        }
    }
}

/// Verify a completed self-test by reading the region back and comparing it
/// against the same generator. Separate from `tick()` so it can be called on
/// demand, and so a failure says *where*.
pub fn verify_selftest(addr: u32, len: u32) -> Result<(), u32> {
    for i in 0..len {
        if read_byte(addr + i) != selftest_byte(i) {
            return Err(i);
        }
    }
    Ok(())
}
