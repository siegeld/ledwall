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

/// Change the S-PWM pass count LIVE, on a configured panel.
///
/// `passes` is the chip's reg0x03 term read two ways and BOTH have to move
/// together (11.7.5): the chip's displayable grey range is
/// `4 * (reg0x04+1) * (reg0x03+1)`, and the scanner's W12 cadence is one wide
/// ROW pulse per `groups * scans` signals of the same count. Writing only the
/// CSR leaves the chip expecting a different range and every mixed colour
/// clips, which is the 2026-10-19 fault in reverse.
///
/// Why it is worth a live knob: the grey CYCLE is `pass rate / passes`, and
/// the pass rate is fixed by scan-out memory bandwidth at ~267 Hz. At the
/// factory 64 passes that is a 4.2 Hz cycle, so a 20 fps stream assembles
/// every pixel out of ~5 different frames -- visible as sparkle on detail and
/// stutter on a scroll. Halving passes doubles the cycle and halves the grey
/// range; 8-bit video has 256 levels, so there is a lot of range to spend.
/// Sweeping that trade needs eyes on glass, and a rebuild per step makes it
/// unaffordable.
///
/// Returns the levels the chip will now display, or None if `name` is unknown.
pub fn set_passes(hub75: &pac::Hub75, name: Option<&str>, scan: u16,
                  passes: u8) -> Option<u32> {
    let chip = by_name(name.unwrap_or(""))?;
    let p = passes.clamp(1, 64);
    let r03 = 0x0300u16 | ((p as u16 - 1) & 0x7f);

    // Rewrite the 0x03 row in place, leaving every other register alone.
    let mut idx = None;
    for (i, row) in chip.regs.iter().enumerate() {
        if (row.r >> 8) == 0x03 { idx = Some(i); break; }
    }
    let i = idx?;
    write_row(hub75, i, Row { r: r03, g: r03, b: r03 });
    let _ = scan;

    // And the scanner, which counts the passes the chip is now expecting.
    unsafe { hub75.latch_cfg().modify(|_, w| w.passes().bits(p - 1)) };

    // levels = 4 * (reg0x04 + 1) * (reg0x03 + 1)
    let r04 = chip.regs.iter().find(|r| (r.r >> 8) == 0x04).map(|r| r.r & 0x7f);
    r04.map(|v| 4 * (v as u32 + 1) * (p as u32))
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
            w.mid_latch().bit(chip.mid_latch);
            // Width of the ROW pulse that steps the scan counter. The DP3264S
            // reads this width as meaning (W12 then W4); get it wrong and the
            // panel is dark, not dim.
            w.row_pulse().bits(chip.row_pulse);
            w.row_pulse_first().bits(chip.row_pulse_first);
            w.data_latch().bit(chip.data_latch != 0);
            // Drive A-E with the row number for modules whose row chip is a
            // decoder rather than a counter.
            // 11.2: a line goes channel 15 -> 0 on this family.
            w.chan_rev().bit(chip.chan_rev != 0);
            w.row_address().bit(chip.row_address != 0);
            w.row_addr_shift().bits(chip.row_addr_shift)
        })
    };
    // Whether A/B/C are an address at all. On the RT5958D/5958 family they are
    // not: the row driver is a cascaded shift register ("wipe off 138
    // decoder"), so they carry DIN, LCK and BK and a row is selected by
    // walking a single 1 along the chain. `modify` rather than `write`,
    // because the pin mapping and polarity fields keep their gateware reset
    // values here and are searched at runtime over /api/rowser.
    unsafe {
        hub75.row_ser().modify(|_, w| w.enable().bit(chip.row_serial != 0))
    };

    // `chan_xor` and `sweep_adv` live in `latch_cfg`, NOT in the register
    // table, so nothing above applies them -- and the bitstream's reset values
    // for that CSR come from whatever chip the PRESET named, which for the
    // HUB320 panel is `icnd1065`, not this one. Without this write the panel
    // comes up after every power cycle with both at 0 and the 16-pitch dots
    // back, which is exactly what happened on 2026-10-19.
    //
    // `modify`, not `write`: latch_phase shares this register and is a live
    // diagnostic knob that must not be reset by a boot-time configure.
    unsafe {
        hub75.latch_cfg().modify(|_, w| {
            w.chan_xor().bits(chip.chan_xor);
            w.sweep_adv().bits(chip.sweep_adv);
            // Display passes per data frame, the chip's reg0x03. It sets the
            // W12 cadence AND the displayable grey range, so a mismatch
            // against the register table clips every mixed colour to full.
            w.passes().bits(chip.passes_m1)
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
