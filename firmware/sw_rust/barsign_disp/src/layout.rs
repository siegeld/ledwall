/// Panel layout configuration for multi-panel virtual displays.
/// Maps physical J-connectors (outputs 0–5) and chain slots to grid positions.

pub const MAX_OUTPUTS: usize = 6;
pub const MAX_CHAIN: usize = 2;
const POS_UNIT: u16 = 16; // gateware multiplier

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
    /// Which logical colour each HUB75 pin group carries, as an index into
    /// RGB_ORDERS ("RGB", "RBG", "GRB", "GBR", "BRG", "BGR"). The panel's own
    /// wiring decides this; get it wrong and the hues are wrong while every
    /// counter stays clean. 0 (RGB) is standard and is what an unconfigured
    /// card runs, so existing panels are unaffected.
    pub rgb_order: u8,
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
            rgb_order: 0,
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
        hub75.set_rgb_order(self.rgb_order);
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
                    "panel_height" => {
                        if let Ok(h) = parse_u16(value) {
                            config.panel_height = h;
                        }
                    }
                    _ => {
                        // Try J1–J6
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

fn parse_pos(s: &str) -> Option<(u8, u8)> {
    let pos = s.find(',')?;
    let col = parse_u8(&s[..pos]).ok()?;
    let row = parse_u8(&s[pos + 1..]).ok()?;
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
