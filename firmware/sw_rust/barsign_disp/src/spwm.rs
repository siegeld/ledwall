//! Configuring an S-PWM driver chip at boot.
//!
//! The gateware is one engine for the whole family; the chip is a register
//! table it reads. This writes that table over the `chip_table` CSR and the
//! grammar over `chip_cfg`, so one bitstream drives an ICND1065L/S or a
//! DP3364S depending only on what marquee serves in the boot config.
//!
//! The tables are GENERATED from `gateware/spwm_chips.py` (see
//! `spwm_tables.rs`) rather than copied, because a second copy is a place for
//! the gateware's default and the firmware's runtime table to drift silently --
//! the panel would light with the wrong configuration and look merely off.
//!
//! Only compiled when the bitstream has an S-PWM output stage: the CSRs do not
//! exist in a HUB75 build, and the PAC is regenerated per bitstream.
//!
//! UNTESTED ON HARDWARE. No S-PWM panel has been driven by this board.

use crate::spwm_tables::{by_name, Chip, Row, MAX_REGS};
use litex_pac as pac;

/// Result of a configure attempt, for the status page.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Configured {
    /// No chip name in the boot config; the gateware's power-on default stands.
    Default,
    /// Table written.
    Chip(&'static str),
    /// A name was configured that this firmware has no table for.
    Unknown,
}

/// Write one table row. Address 0..MAX_REGS-1 are the rotating registers,
/// MAX_REGS.. are the wrapper slots.
fn write_row(hub75: &pac::Hub75, addr: usize, row: Row) {
    let v = (addr as u64)
        | ((row.r as u64) << 16)
        | ((row.g as u64) << 32)
        | ((row.b as u64) << 48);
    // A 64-bit CSR is two 32-bit registers, and the LOW half commits the write
    // -- so the high half goes first or the table takes a row built from half
    // of this word and half of the previous one.
    unsafe {
        hub75.chip_table1().write(|w| w.bits((v >> 32) as u32));
        hub75.chip_table0().write(|w| w.bits(v as u32));
    }
}

/// Patch the scan-row count into a register word.
///
/// Every profile in the catalogue puts (rows - 1) in the low bits of this
/// register and leaves the upper bits as a chip constant, so this preserves
/// whatever the table carries there rather than overwriting the whole byte.
fn with_scan(chip: &Chip, row: Row, rows: u16) -> Row {
    if (row.r >> 8) as u8 != chip.scan_reg {
        return row;
    }
    let keep = (row.r as u8) & !chip.scan_mask;
    let v = ((chip.scan_reg as u16) << 8)
        | (keep as u16)
        | ((rows - 1) & chip.scan_mask as u16);
    Row { r: v, g: v, b: v }
}

/// Configure the output stage for `name`, at `scan` rows per frame.
///
/// `None` or an empty name leaves the gateware's built-in default in place,
/// which is what an unconfigured card runs -- it lights, rather than showing
/// nothing at all while someone works out why.
pub fn configure(hub75: &pac::Hub75, name: Option<&str>, scan: u16) -> Configured {
    let name = match name {
        None => return Configured::Default,
        Some(n) if n.is_empty() => return Configured::Default,
        Some(n) => n,
    };
    let chip = match by_name(name) {
        Some(c) => c,
        // Deliberately does NOT fall back to a different chip's table. Driving
        // a panel with the wrong register map is not a degraded picture, it is
        // an unknown state; leaving the default and reporting Unknown is the
        // honest failure.
        None => return Configured::Unknown,
    };

    for (i, row) in chip.regs.iter().enumerate() {
        write_row(hub75, i, with_scan(chip, *row, scan));
    }
    for (i, row) in chip.wrappers.iter().enumerate() {
        write_row(hub75, MAX_REGS + i, *row);
    }

    // Grammar last: the table is consistent before the engine is told how many
    // registers to rotate through, so a prefix that fires mid-write reads old
    // rows rather than a half-written table of a different length.
    unsafe {
        hub75.chip_cfg().write(|w| {
            w.reg_count().bits(chip.regs.len() as u8);
            w.slots().bits(chip.slots);
            w.rot_slot().bits(chip.rot_slot);
            w.mid_latch().bit(chip.mid_latch)
        })
    };
    Configured::Chip(chip.name)
}

impl Configured {
    pub fn as_str(&self) -> &'static str {
        match self {
            Configured::Default => "default",
            Configured::Chip(n) => n,
            Configured::Unknown => "unknown",
        }
    }
}
