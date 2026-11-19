/// Panel layout configuration for multi-panel virtual displays.
/// Maps physical J-connectors (outputs 0–5) and chain slots to grid positions.

// Eight, because the E320's HUB320 ports are eight and a 5A-75E's six. This
// is only the LAYOUT array bound -- `hub75::OUTPUTS` still says how many the
// GATEWARE has, and is per-variant. Sizing this to the larger board means a
// J7/J8 assignment is parsed and visible rather than silently discarded by
// the bound check below; whether it reaches a pin is then the gateware's
// business, and `OUTPUT_MISMATCH` reports when the two disagree.
pub use crate::turn::{turned_index, turned_size};

pub const MAX_OUTPUTS: usize = 8;
pub const MAX_CHAIN: usize = 2;
/// RGB groups one connector can carry: HUB75 has 2, HUB320 4.
pub const MAX_GROUPS: usize = 4;
const POS_UNIT: u16 = 16; // gateware multiplier

/// Set when a layout asked for a channel order this gateware cannot apply: the
/// S-PWM output stage (`icn1065_soc.py`) has no `rgb_order` CSR, while the plain
/// HUB75 stage does. Surfaced by `/api/status` so an unsupported setting is
/// visible rather than quietly dropped.
pub static mut RGB_ORDER_UNSUPPORTED: bool = false;

/// Gateware output count, read from `hw_config`, when it disagrees with the
/// firmware's compile-time `hub75::OUTPUTS`; 0 when they agree.
///
/// The two MUST match: `OUTPUTS` sizes the panel-address table the firmware
/// writes, so a firmware built for six against a gateware built for eight
/// leaves two outputs on their gateware defaults and shifts nothing else --
/// no error, no symptom but dark connectors. hub75.rs has said for a year that
/// this needs "the panel-count CSR check"; this is it. Surfaced by
/// `/api/status` so the mismatch is a number someone can read rather than a
/// wall of dead panels.
pub static mut OUTPUT_MISMATCH: u8 = 0;
/// 1 when the layout's `panel_width x panel_height` is not the module the
/// bitstream was built for (`hw_columns x hw_rows`). Every raw framebuffer
/// write -- the stream, the test screen -- is then scrambled, with every
/// counter clean; this is the flag that says why.
pub static mut MODULE_MISMATCH: u8 = 0;
/// 1 when the config asked for a `group_columns` / `group_scan` this
/// bitstream cannot run: not a runtime-geometry build, past its maxima, not a
/// whole number of 16-channel chips, an odd scan count, a value that is not
/// a number, or no chip table named to scan it with. The bitstream's own
/// (largest) geometry is then in force, whatever an earlier config set.
pub static mut GEOM_REFUSED: u8 = 0;

include!(concat!(env!("OUT_DIR"), "/rt_geom.rs"));

/// What drives the display. Carried in the per-MAC TFTP config rather than in
/// firmware, because marquee CANNOT do per-panel firmware -- tftp_resolve
/// ignores the `firmware` field, so every panel gets the same boot.bin. Putting
/// the mode in config sidesteps that entirely: one firmware carries every
/// program, and a mixed fleet (some streaming, some composing) needs no bespoke
/// build per panel.
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum DisplayMode {
    /// Receive frames from marquee. The default, and the fallback.
    Stream,
    /// Run an on-panel program; no frame is received.
    Program,
}

/// Longest program name the config can carry.
pub const MAX_PROGRAM_NAME: usize = 24;
/// `chip_reg:` lines a boot config may carry. The ICND1065 table is 38 rows.
pub const MAX_CHIP_REGS: usize = 48;

pub struct LayoutConfig {
    pub panel_width: u16,  // physical panel width (e.g., 96)
    pub panel_height: u16, // physical panel height (e.g., 48)
    pub grid_cols: u8,     // virtual grid columns
    pub grid_rows: u8,     // virtual grid rows
    /// For each output 0–5 and chain slot 0–1: Some((col, row)) if assigned, None if unused
    pub assignments: [[Option<(u8, u8)>; MAX_CHAIN]; MAX_OUTPUTS],
    pub mode: DisplayMode,
    program: [u8; MAX_PROGRAM_NAME],
    program_len: u8,
    /// Driver chip on the modules ("icnd1065", "dp3364s"). Empty means "use
    /// whatever the bitstream was built with", which is what an unconfigured
    /// card runs -- it lights rather than showing nothing while someone works
    /// out why. Only meaningful on an S-PWM bitstream.
    drive: [u8; MAX_PROGRAM_NAME],
    drive_len: u8,
    /// Panel shift clock divider (sys/N), and idle cycles between pixel DMA
    /// writes. Both 0 means "leave the bitstream's reset value".
    ///
    /// These are per-MODULE operating points, found by measurement on glass,
    /// so they belong in the config beside `drive` and `group_origins` rather
    /// than compiled into a bitstream. The P1.25 bench module needed `clk_div`
    /// 6 where the preset said 4, and the setting silently reverted on every
    /// reboot until it lived here.
    ///
    /// `write_throttle` buys read bandwidth back for the scan-out, which reads
    /// every pixel on demand with no row buffer: a chunk is 487 writes back to
    /// back and that burst starves the reads, which is a white dot on the
    /// glass. Measured 2026-10-20 -- no writes is perfectly clean, 1360
    /// chunks/s is heavy snow, and nothing but write traffic moves it.
    pub clk_div: u8,
    pub write_throttle: u8,
    /// Per-module overrides of the driver chip's register table, by register
    /// ADDRESS: `chip_reg: 0x0c 0x01 0x0e 0x0e` (address, then the R, G and B
    /// lines' values; one value means all three). The chip NAME picks the
    /// grammar and a starting table; these are what a vendor's receiver
    /// profile says THIS module wants, and they differ per colour. Applied by
    /// `spwm::configure`. An address the chip's table does not carry is
    /// counted and ignored, never appended.
    pub chip_regs: [(u8, u8, u8, u8); MAX_CHIP_REGS],
    pub chip_regs_len: u8,
    /// One RGB group's chain length in columns, and the module's scan lines
    /// (`group_columns:`, `group_scan:`). 0 means "what the bitstream was
    /// built for". Only a runtime-geometry bitstream can honour another
    /// value, and only inside its maxima, in whole 16-channel chips and an
    /// even scan count; anything else is refused and reported
    /// (`geom_refused` in /api/status), never rounded -- a chain clocked at
    /// the wrong length is a sawtooth on the glass with every counter clean.
    pub group_columns: u16,
    pub group_scan: u16,
    /// Which logical colour each HUB75 pin group carries, as an index into
    /// RGB_ORDERS ("RGB", "RBG", "GRB", "GBR", "BRG", "BGR"). The panel's own
    /// wiring decides this; get it wrong and the hues are wrong while every
    /// counter stays clean. 0 (RGB) is standard and is what an unconfigured
    /// card runs, so existing panels are unaffected.
    pub rgb_order: u8,
    /// How the modules are MOUNTED, in quarter turns clockwise from the way the
    /// framebuffer lies (`rotate: 0|90|180|270`). The framebuffer follows the
    /// electronics -- a row is a scan line -- and some modules hang a quarter
    /// turn from that. Streamed frames arrive already turned (marquee does
    /// it); this is for what the card draws ITSELF: programs, the boot test
    /// screen and its version banner. Electrical test patterns are not
    /// turned -- they exist to show the wiring as it is.
    pub turn: u8,
    /// Where each RGB group's strip starts inside the module, in pixels --
    /// the module's physical arrangement. The bench P1.25 module is four
    /// 128x64 groups in a 2x2 (g0 top-right, g1 bottom-right, g2 top-left,
    /// g3 bottom-left), which an S-PWM bitstream carries as the preset's
    /// default; `group_origins:` in the config overrides it for a module
    /// wired differently. Empty means "leave the bitstream's defaults".
    pub group_origins: [(u16, u16); MAX_GROUPS],
    pub group_origins_len: u8,
    /// The WALL this card is part of, and where this card's top-left sits in
    /// it (`wall: 512x256`, `origin: 256,0`). Only programs use them: a
    /// program draws in wall coordinates and each card renders its own
    /// rectangle, so a ticker runs ACROSS the cards instead of restarting on
    /// each. 0x0 means "the wall is this card", which is every single-card
    /// wall and every config written before this existed.
    pub wall_w: u16,
    pub wall_h: u16,
    pub origin_x: u16,
    pub origin_y: u16,
}

/// The channel orders the gateware's rgb_order CSR understands, in CSR order.
/// Named the way LED parts are: the value is what the PANEL expects.
pub const RGB_ORDERS: [&str; 6] = ["RGB", "RBG", "GRB", "GBR", "BRG", "BGR"];

/// Scan rows per frame for a layout: the panel's own height divided by the
/// RGB groups its connector carries. A HUB75 connector carries two.
pub fn scan_rows(cfg: &LayoutConfig) -> u16 {
    core::cmp::max(1, cfg.panel_height / 2)
}

impl LayoutConfig {
    pub fn single_panel(width: u16, height: u16) -> Self {
        let mut assignments = [[None; MAX_CHAIN]; MAX_OUTPUTS];
        assignments[0][0] = Some((0, 0));
        Self {
            panel_width: width,
            panel_height: height,
            grid_cols: 1,
            grid_rows: 1,
            assignments,
            mode: DisplayMode::Stream,
            program: [0; MAX_PROGRAM_NAME],
            program_len: 0,
            drive: [0; MAX_PROGRAM_NAME],
            drive_len: 0,
            clk_div: 0,
            write_throttle: 0,
            chip_regs: [(0, 0, 0, 0); MAX_CHIP_REGS],
            chip_regs_len: 0,
            group_columns: 0,
            group_scan: 0,
            rgb_order: 0,
            turn: 0,
            group_origins: [(0, 0); MAX_GROUPS],
            group_origins_len: 0,
            wall_w: 0,
            wall_h: 0,
            origin_x: 0,
            origin_y: 0,
        }
    }

    /// The program this panel should run, or "" if none was configured.
    pub fn program(&self) -> &str {
        core::str::from_utf8(&self.program[..self.program_len as usize]).unwrap_or("")
    }

    /// Configured driver chip, or "" for the bitstream's built-in default.
    pub fn drive(&self) -> &str {
        core::str::from_utf8(&self.drive[..self.drive_len as usize]).unwrap_or("")
    }

    pub fn virtual_width(&self) -> u16 {
        self.panel_width * self.grid_cols as u16
    }

    pub fn virtual_height(&self) -> u16 {
        self.panel_height * self.grid_rows as u16
    }

    pub fn virtual_length(&self) -> u32 {
        self.virtual_width() as u32 * self.virtual_height() as u32
    }

    /// Apply layout to HUB75 hardware: sets image_width and all panel_param CSRs.
    ///
    /// HUB75 shift order: hw chain 0 is shifted first and ends up on the
    /// far panel (end of daisy chain); hw chain 1 is shifted last and stays
    /// on the near panel (directly connected).  Config chain index maps
    /// directly to hw chain index — no reversal needed.
    /// Config example: `J1: 0,0 1,0` → chain 0 = far/left panel at (0,0),
    ///                                   chain 1 = near/right panel at (1,0).
    pub fn apply(&self, hub75: &mut crate::hub75::Hub75) {
        hub75.set_img_param(self.virtual_width(), self.virtual_length());
        // Applied on every layout change, not only at boot: a panel swapped
        // for one wired differently is a config edit, and requiring a power
        // cycle for it would be a trap.
        // On an S-PWM build there is no rgb_order CSR, so a non-default order
        // cannot be honoured. Say so instead of appearing to apply it: silently
        // ignoring it would show as wrong hues with every counter clean.
        if !hub75.set_rgb_order(self.rgb_order) {
            // Reported by /api/status: silently ignoring a configured channel
            // order would show as wrong hues with every counter on the card
            // clean, and the operator would check the panel, the cabling and the
            // scene, in that order, for a long time first.
            unsafe { RGB_ORDER_UNSUPPORTED = true };
        }
        // The module's group arrangement, when the config states one.
        if self.group_origins_len > 0 {
            hub75.set_group_origins(&self.group_origins[..self.group_origins_len as usize]);
        }
        for (output, chain_slots) in self.assignments.iter().enumerate() {
            for (chain, assignment) in chain_slots.iter().enumerate() {
                if let Some((col, row)) = assignment {
                    let x = (*col as u16 * self.panel_width / POS_UNIT) as u8;
                    let y = (*row as u16 * self.panel_height / POS_UNIT) as u8;
                    hub75.set_panel_param(output as u8, chain as u8, x, y, 0);
                }
            }
        }
    }

    /// Parse layout from text config (KEY=VALUE format).
    ///
    /// Supported keys:
    /// - `grid=2x1` — set grid dimensions
    /// - `panel_width=96` — physical panel width
    /// - `panel_height=48` — physical panel height
    /// - `J1=0,0` or `J1=0,0 1,0` — assign output chain slots to grid positions
    ///   (space-separated positions, second position is chain slot 1)
    pub fn parse(text: &str) -> Option<Self> {
        let mut config = Self::single_panel(96, 48);
        // Clear default assignment — config should be explicit
        config.assignments = [[None; MAX_CHAIN]; MAX_OUTPUTS];

        for line in text.lines() {
            let line = line.trim();
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            // Accept both "key=value" and YAML-style "key: value"
            let sep = line.find('=').or_else(|| line.find(": "));
            if let Some(pos) = sep {
                let skip = if line.as_bytes().get(pos) == Some(&b':') { 2 } else { 1 };
                let key = line[..pos].trim();
                let value = line[pos + skip..].trim();
                match key {
                    "grid" => {
                        if let Some((cols, rows)) = parse_grid(value) {
                            config.grid_cols = cols;
                            config.grid_rows = rows;
                        }
                    }
                    "panel_width" => {
                        if let Ok(w) = parse_u16(value) {
                            config.panel_width = w;
                        }
                    }
                    "mode" => {
                        // Anything that is not "program" means stream. An
                        // unrecognised mode must not black out a panel.
                        config.mode = if value.eq_ignore_ascii_case("program") {
                            DisplayMode::Program
                        } else {
                            DisplayMode::Stream
                        };
                    }
                    "program" => {
                        let b = value.as_bytes();
                        let n = b.len().min(MAX_PROGRAM_NAME);
                        config.program[..n].copy_from_slice(&b[..n]);
                        config.program_len = n as u8;
                    }
                    // Accepts the NAME ("GRB"), not a number: a digit here
                    // would be unreadable in a config file and easy to get
                    // wrong by one. Unknown names leave it at RGB rather than
                    // failing the whole layout -- a typo should cost you the
                    // colour order, not the panel.
                    "rgb_order" | "color_order" | "colour_order" => {
                        let want = value.trim();
                        config.rgb_order = RGB_ORDERS
                            .iter()
                            .position(|o| o.eq_ignore_ascii_case(want))
                            .unwrap_or(0) as u8;
                    }
                    // Which S-PWM driver chip the modules carry. Ignored by a
                    // HUB75 bitstream, which has no chip table to load.
                    "drive" | "drive_type" | "chip" => {
                        let b = value.as_bytes();
                        let n = b.len().min(MAX_PROGRAM_NAME);
                        config.drive[..n].copy_from_slice(&b[..n]);
                        config.drive_len = n as u8;
                    }
                    // Panel shift clock, sys/N. Clamped to N>=2 by the
                    // gateware. The right value is a glass measurement: too
                    // fast and the pixel reads cannot keep the shift register
                    // fed, which is snow, not dimness.
                    "clk_div" | "clock_div" => {
                        if let Ok(v) = parse_u16(value) {
                            config.clk_div = v.min(255) as u8;
                        }
                    }
                    // Degrees clockwise. Anything but a quarter-turn multiple is
                    // ignored: a typo should cost the turn, not the panel.
                    "rotate" | "rotation" => {
                        config.turn = match value {
                            "90" => 1,
                            "180" => 2,
                            "270" => 3,
                            _ => 0,
                        };
                    }
                    "group_columns" | "chain_columns" => {
                        // A value that does not parse is one no build can
                        // scan: the card refuses it and says so, where 0
                        // would have meant "not asked for".
                        config.group_columns = parse_u16(value).unwrap_or(u16::MAX);
                    }
                    "group_scan" | "scan" => {
                        config.group_scan = parse_u16(value).unwrap_or(u16::MAX);
                    }
                    "chip_reg" => {
                        if let Some(e) = parse_chip_reg(value) {
                            // A repeated address replaces the earlier line,
                            // like every other repeated key here.
                            let n = config.chip_regs_len as usize;
                            if let Some(slot) = config.chip_regs[..n].iter_mut().find(|r| r.0 == e.0) {
                                *slot = e;
                            } else if n < MAX_CHIP_REGS {
                                config.chip_regs[n] = e;
                                config.chip_regs_len += 1;
                            }
                        }
                    }
                    // Idle cycles between pixel DMA writes, 0..15.
                    "write_throttle" | "dma_throttle" => {
                        if let Ok(v) = parse_u16(value) {
                            config.write_throttle = v.min(15) as u8;
                        }
                    }
                    "panel_height" => {
                        if let Ok(h) = parse_u16(value) {
                            config.panel_height = h;
                        }
                    }
                    // `wall: 512x256` -- the whole wall's size, for programs.
                    "wall" => {
                        if let Some((a, b)) = value.trim().split_once(['x', 'X']) {
                            if let (Ok(w), Ok(h)) = (parse_u16(a.trim()), parse_u16(b.trim())) {
                                config.wall_w = w;
                                config.wall_h = h;
                            }
                        }
                    }
                    // `origin: 256,0` -- this card's top-left within the wall.
                    "origin" => {
                        if let Some((x, y)) = parse_pos16(value.trim()) {
                            config.origin_x = x;
                            config.origin_y = y;
                        }
                    }
                    // `group_origins: 128,0 128,64 0,0 0,64` -- one "x,y" per
                    // RGB group, in pixels, group 0 first. Where each group's
                    // strip sits in the module. Only an S-PWM bitstream has
                    // the CSRs; a HUB75 one ignores it.
                    "group_origins" | "groups" => {
                        let mut n = 0;
                        for pos_str in value.split_ascii_whitespace() {
                            if n >= MAX_GROUPS {
                                break;
                            }
                            if let Some(xy) = parse_pos16(pos_str) {
                                config.group_origins[n] = xy;
                                n += 1;
                            }
                        }
                        config.group_origins_len = n as u8;
                    }
                    _ => {
                        // J1..J<MAX_OUTPUTS>: a connector's place in the grid
                        if let Some(rest) = key.strip_prefix('J') {
                            if let Ok(n) = parse_u8(rest) {
                                if n >= 1 && n <= MAX_OUTPUTS as u8 {
                                    let idx = (n - 1) as usize;
                                    // Split value on whitespace for chain slots
                                    for (chain, pos_str) in value.split_ascii_whitespace().enumerate() {
                                        if chain >= MAX_CHAIN {
                                            break;
                                        }
                                        if let Some((col, row)) = parse_pos(pos_str) {
                                            config.assignments[idx][chain] = Some((col, row));
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        Some(config)
    }
}

fn parse_grid(s: &str) -> Option<(u8, u8)> {
    let pos = s.find('x')?;
    let cols = parse_u8(&s[..pos]).ok()?;
    let rows = parse_u8(&s[pos + 1..]).ok()?;
    Some((cols, rows))
}

fn parse_pos16(s: &str) -> Option<(u16, u16)> {
    let pos = s.find(',')?;
    // Trimmed: a hand-written `origin: 256, 0` must not be silently ignored.
    let x = parse_u16(s[..pos].trim()).ok()?;
    let y = parse_u16(s[pos + 1..].trim()).ok()?;
    Some((x, y))
}

fn parse_pos(s: &str) -> Option<(u8, u8)> {
    let pos = s.find(',')?;
    let col = parse_u8(s[..pos].trim()).ok()?;
    let row = parse_u8(s[pos + 1..].trim()).ok()?;
    Some((col, row))
}

/// Simple u8 parser for no_std (avoids pulling in core::str::parse for all int types).
fn parse_u8(s: &str) -> Result<u8, ()> {
    let mut result: u8 = 0;
    if s.is_empty() {
        return Err(());
    }
    for b in s.bytes() {
        if b < b'0' || b > b'9' {
            return Err(());
        }
        result = result.checked_mul(10).ok_or(())?.checked_add(b - b'0').ok_or(())?;
    }
    Ok(result)
}

/// Simple u16 parser for no_std.
/// A byte, decimal or `0x` hex.
fn parse_byte(s: &str) -> Option<u8> {
    let s = s.trim();
    if let Some(h) = s.strip_prefix("0x").or_else(|| s.strip_prefix("0X")) {
        if h.is_empty() || h.len() > 2 {
            return None;
        }
        u8::from_str_radix(h, 16).ok()
    } else {
        parse_u8(s).ok()
    }
}

/// `addr value` or `addr r g b`, whitespace separated.
fn parse_chip_reg(s: &str) -> Option<(u8, u8, u8, u8)> {
    let mut it = s.split_whitespace();
    let addr = parse_byte(it.next()?)?;
    let r = parse_byte(it.next()?)?;
    match it.next() {
        None => Some((addr, r, r, r)),
        Some(g) => {
            let g = parse_byte(g)?;
            let b = parse_byte(it.next()?)?;
            if it.next().is_some() {
                return None;
            }
            Some((addr, r, g, b))
        }
    }
}

fn parse_u16(s: &str) -> Result<u16, ()> {
    let mut result: u16 = 0;
    if s.is_empty() {
        return Err(());
    }
    for b in s.bytes() {
        if b < b'0' || b > b'9' {
            return Err(());
        }
        result = result.checked_mul(10).ok_or(())?.checked_add((b - b'0') as u16).ok_or(())?;
    }
    Ok(result)
}

/// Parse a "ColsxRows" grid spec. Public for use by menu commands.
pub fn parse_grid_spec(s: &str) -> Option<(u8, u8)> {
    parse_grid(s)
}

/// Parse a "col,row" position spec. Public for use by menu commands.
pub fn parse_pos_spec(s: &str) -> Option<(u8, u8)> {
    parse_pos(s)
}
