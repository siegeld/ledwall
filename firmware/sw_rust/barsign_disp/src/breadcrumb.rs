//! Crash breadcrumbs.
//!
//! There is no serial console on this board, and the failure under
//! investigation is intermittent (~1 run in 3) and ends in either a panic
//! (panic.rs writes soc_rst) or a hang. Bisecting that by rebuilding and
//! re-running is a 3-minute coin flip, so instead we leave a trail.
//!
//! `mark()` writes a code to a fixed word of uncached SDRAM that nothing else
//! touches: above the framebuffer (which ends at physical 0x280000) and well
//! below the stack. soc_rst resets the CPU but does not clear SDRAM, so after
//! a crash-reboot the firmware can read back the last code reached and report
//! it over HTTP. A hang leaves the value readable on the next manual reload.
const ADDR: usize = 0x902A_0000;
const COUNT_ADDR: usize = 0x902A_0004;
const MAGIC: u32 = 0xBEEF_0000;

/// Record that execution reached `code`.
#[inline(always)]
pub fn mark(code: u16) {
    unsafe { core::ptr::write_volatile(ADDR as *mut u32, MAGIC | code as u32) };
}

/// Bump a free-running counter, so we can tell "reached once" from "looping".
#[inline(always)]
pub fn tally() {
    unsafe {
        let c = core::ptr::read_volatile(COUNT_ADDR as *const u32);
        core::ptr::write_volatile(COUNT_ADDR as *mut u32, c.wrapping_add(1));
    }
}

const MCAUSE_ADDR: usize = 0x902A_0008;
const MEPC_ADDR: usize = 0x902A_000C;

static mut PREV_RAW: u32 = 0;
static mut PREV_MCAUSE: u32 = 0;
static mut PREV_MEPC: u32 = 0;

pub const EXCEPTION: u16 = 99;
pub const PANIC: u16 = 98;

pub fn prev_mcause() -> u32 {
    unsafe { PREV_MCAUSE }
}

pub fn prev_mepc() -> u32 {
    unsafe { PREV_MEPC }
}

/// Latch the marker left by the PREVIOUS boot, then start a fresh trail.
/// Must run before anything else calls mark().
pub fn capture_boot() {
    unsafe {
        PREV_RAW = core::ptr::read_volatile(ADDR as *const u32);
        PREV_MCAUSE = core::ptr::read_volatile(MCAUSE_ADDR as *const u32);
        PREV_MEPC = core::ptr::read_volatile(MEPC_ADDR as *const u32);
        core::ptr::write_volatile(MCAUSE_ADDR as *mut u32, 0);
        core::ptr::write_volatile(MEPC_ADDR as *mut u32, 0);
    }
    reset_count();
    mark(BOOT);
}

pub fn prev_raw() -> u32 {
    unsafe { PREV_RAW }
}

pub fn raw() -> u32 {
    unsafe { core::ptr::read_volatile(ADDR as *const u32) }
}

pub fn count() -> u32 {
    unsafe { core::ptr::read_volatile(COUNT_ADDR as *const u32) }
}

pub fn reset_count() {
    unsafe { core::ptr::write_volatile(COUNT_ADDR as *mut u32, 0) };
}

/// Last code from BEFORE this boot, or None if SDRAM held no valid marker.
pub fn previous(raw_at_boot: u32) -> Option<u16> {
    if raw_at_boot & 0xFFFF_0000 == MAGIC {
        Some((raw_at_boot & 0xFFFF) as u16)
    } else {
        None
    }
}

// Marker codes.
pub const BOOT: u16 = 1;
pub const PRESENT_ENTER: u16 = 10;
pub const PRESENT_GATE_PASSED: u16 = 11;
pub const REPAIR_LOOP: u16 = 12;
pub const REPAIR_DONE: u16 = 13;
pub const SWAP_DONE: u16 = 14;
pub const PRESENT_EXIT: u16 = 15;
pub const TICK_ENTER: u16 = 20;
pub const TICK_EXIT: u16 = 21;
pub const RGB_WORD_ENTER: u16 = 30;
pub const RGB_WORD_EXIT: u16 = 31;
pub const RX_ENTER: u16 = 40;
pub const RX_EXIT: u16 = 41;
pub const ISR_ENTER: u16 = 50;
pub const BITMAP_RETURNED: u16 = 51;
pub const BITMAP_ACKED: u16 = 52;
pub const LOOP_DONE: u16 = 53;
pub const ISR_EXIT: u16 = 54;
pub const SLOW_PATH: u16 = 55;
