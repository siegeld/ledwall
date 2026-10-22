//! Test pattern generators for HUB75 LED panels

/// Pack RGB into u32 (0x00GGRRBB format for HUB75 hardware)
// Framebuffer word is 0x00BBGGRR on this hardware: B<<16 | G<<8 | R.
//
// Measured end to end 2026-09-18 with the `grid` test pattern and solid frames.
// Under the previous 0x00GGRRBB the red/green/blue bars rendered GREEN/BLUE/RED
// -- one cyclic rotation -- and a solid red frame lit green.
//
// The v1.10.1 "colour order fix" moved every writer in this repo ONTO the wrong
// order, and each file's comment then cited the others as justification. Treat
// the word as a property of the panel in front of you: HUB75 panels differ in
// RGB pin order. See docs/HARDWARE.md section 3.
fn rgb(r: u8, g: u8, b: u8) -> u32 {
    (b as u32) << 16 | (g as u32) << 8 | (r as u32)
}

/// Simple 5x7 pixel font for version display
const FONT_5X7: &[(&[u8; 7], char)] = &[
    (&[0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110], '0'),
    (&[0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110], '1'),
    (&[0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0b01000, 0b11111], '2'),
    (&[0b11111, 0b00010, 0b00100, 0b00010, 0b00001, 0b10001, 0b01110], '3'),
    (&[0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010], '4'),
    (&[0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110], '5'),
    (&[0b00110, 0b01000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110], '6'),
    (&[0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000], '7'),
    (&[0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110], '8'),
    (&[0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00010, 0b01100], '9'),
    (&[0b00000, 0b00000, 0b00000, 0b00000, 0b00000, 0b01100, 0b01100], '.'),
    (&[0b00000, 0b00000, 0b10001, 0b10001, 0b10001, 0b01010, 0b00100], 'v'),
];

/// Firmware version string, as drawn on the panel at boot.
pub const VERSION_TEXT: &str = concat!("v", env!("CARGO_PKG_VERSION"));

/// Pixel height of the 5x7 font, and the drawn width of VERSION_TEXT.
///
/// `* 6` is five glyph columns plus a one-pixel gap; the trailing gap after
/// the last character is not drawn, hence the `- 1`.
pub const VERSION_TEXT_HEIGHT: usize = 7;

pub fn version_text_width() -> usize {
    VERSION_TEXT.len() * 6 - 1
}

/// Is (dx, dy) lit, where (0, 0) is the version text's OWN top-left corner?
///
/// Takes text-relative coordinates rather than panel ones so the caller
/// decides placement. It used to hard-code an origin of (2, 2), which is how
/// the banner ended up blanking the panel's border.
///
/// Exists because identifying the running firmware otherwise means correlating
/// a TFTP log with a build artifact -- which cost real debugging time when a
/// stale daemon served a months-old boot.bin and everything looked normal.
pub fn version_glyph_pixel(dx: usize, dy: usize) -> bool {
    if dy >= VERSION_TEXT_HEIGHT {
        return false;
    }
    let char_idx = dx / 6;
    let pixel_x = dx % 6;
    if pixel_x >= 5 {
        return false;
    }
    if let Some(ch) = VERSION_TEXT.chars().nth(char_idx) {
        for (glyph, c) in FONT_5X7 {
            if *c == ch {
                return (glyph[dy] >> (4 - pixel_x)) & 1 == 1;
            }
        }
    }
    false
}


/// Convert HSV to RGB. h in [0,360), s,v in [0,255]
fn hsv_to_rgb(h: u16, s: u8, v: u8) -> (u8, u8, u8) {
    if s == 0 {
        return (v, v, v);
    }

    let region = h / 60;
    let remainder = ((h % 60) as u32 * 255) / 60;

    let p = ((v as u32) * (255 - s as u32)) / 255;
    let q = ((v as u32) * (255 - (s as u32 * remainder) / 255)) / 255;
    let t = ((v as u32) * (255 - (s as u32 * (255 - remainder)) / 255)) / 255;

    match region {
        0 => (v, t as u8, p as u8),
        1 => (q as u8, v, p as u8),
        2 => (p as u8, v, t as u8),
        3 => (p as u8, q as u8, v),
        4 => (t as u8, p as u8, v),
        _ => (v, p as u8, q as u8),
    }
}

/// Generate grid pattern - white lines with colored verticals and diagonal X
pub fn grid(width: u16, height: u16) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    (0..h).flat_map(move |row| (0..w).map(move |col| grid_px(col, row, w, h)))
}

/// The test screen, drawn ONCE PER PANEL across a multi-panel layout.
///
/// `grid(w, h)` over the virtual display is the right picture for a 1x1 layout
/// and the wrong one for anything else: on a `4x1` card the border, the
/// diagonals and the quarter lines span four panels, so a single connected
/// panel shows one quarter of a grid -- no border on three sides, one diagonal,
/// no banner -- and looks broken when it is working. The test screen's job is
/// to tell you whether THIS panel is wired, oriented and coloured correctly,
/// which only a per-panel drawing can do. Each `tile_w x tile_h` cell of the
/// virtual image gets its own complete grid; `Hub75::draw_version_banners`
/// stamps the version on each of them.
pub fn grid_tiled(width: u16, height: u16, tile_w: u16, tile_h: u16) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    let tw = (tile_w as usize).max(1);
    let th = (tile_h as usize).max(1);
    (0..h).flat_map(move |row| (0..w).map(move |col| grid_px(col % tw, row % th, tw, th)))
}

/// One pixel of the grid test screen for a `w x h` drawing.
fn grid_px(col: usize, row: usize, w: usize, h: usize) -> u32 {
    // Horizontal lines at 0, 1/4, 1/2, 3/4, h-1
    let is_h_line = row == 0
        || row == h / 4
        || row == h / 2
        || row == 3 * h / 4
        || row == h - 1;

    // Vertical lines at 0, 1/4, 1/2, 3/4, w-1
    let v_pos = if col == 0 {
        Some(0)
    } else if col == w / 4 {
        Some(1)
    } else if col == w / 2 {
        Some(2)
    } else if col == 3 * w / 4 {
        Some(3)
    } else if col == w - 1 {
        Some(4)
    } else {
        None
    };

    // Diagonal lines
    let diag1 = col == row * w / h;
    let diag2 = col == w - 1 - row * w / h;

    // Priority: diagonals > verticals > horizontals > black.
    //
    // The grid used to paint the version here as well, which put it on
    // the panel TWICE at boot: once from this pattern and once from
    // `draw_version_banner()`, which overlays `boot_version_pixel()` on
    // whatever image is showing. The banner is the one to keep -- it
    // works over any image, not just this grid, and it does not paint
    // white over a test pattern whose whole job is to show you the
    // panel's geometry and colour order.
    if diag1 {
        rgb(0, 255, 255) // CYAN
    } else if diag2 {
        rgb(255, 0, 255) // MAGENTA
    } else if let Some(v) = v_pos {
        match v {
            0 => rgb(255, 0, 0),   // RED
            1 => rgb(0, 255, 0),   // GREEN
            2 => rgb(0, 0, 255),   // BLUE
            3 => rgb(255, 255, 0), // YELLOW
            _ => rgb(255, 0, 255), // MAGENTA
        }
    } else if is_h_line {
        rgb(255, 255, 255) // WHITE
    } else {
        rgb(0, 0, 0) // BLACK
    }
}

/// Generate rainbow diagonal wave pattern
pub fn rainbow(width: u16, height: u16) -> impl Iterator<Item = u32> {
    let w = width as u32;
    let h = height as u32;

    (0..h).flat_map(move |row| {
        (0..w).map(move |col| {
            // Diagonal wave - hue varies with position
            let hue = ((col + row) * 360 * 2 / (w + h)) % 360;
            let (r, g, b) = hsv_to_rgb(hue as u16, 255, 255);
            rgb(r, g, b)
        })
    })
}

/// Generate animated rainbow diagonal wave pattern with phase offset
pub fn animated_rainbow(width: u16, height: u16, phase: u32) -> impl Iterator<Item = u32> {
    let w = width as u32;
    let h = height as u32;

    (0..h).flat_map(move |row| {
        (0..w).map(move |col| {
            let hue = ((col + row).wrapping_add(phase)) * 360 * 2 / (w + h) % 360;
            let (r, g, b) = hsv_to_rgb(hue as u16, 255, 255);
            rgb(r, g, b)
        })
    })
}

/// Generate solid color pattern
pub fn solid(width: u16, height: u16, r: u8, g: u8, b: u8) -> impl Iterator<Item = u32> {
    let total = (width as usize) * (height as usize);
    let color = rgb(r, g, b);
    core::iter::repeat(color).take(total)
}

/// Label each GROUP band with its own colour, to discover how the module
/// arranges them physically.
///
/// The gateware stacks the four RGB groups VERTICALLY in the logical image:
/// group g owns logical rows `g*scan .. g*scan+scan-1`. The XP1.25 module
/// places those four 128x64 quadrants as a 2x2 on its 256x128 face, but
/// WHICH group lands in which corner is not documented anywhere we have.
///
/// So paint them: group 0 red, 1 green, 2 blue, 3 white. One photograph then
/// gives the mapping outright, instead of guessing one of the arrangements
/// and burning a build to find out.
pub fn quadrants(width: u16, height: u16) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    let band = if h >= 4 { h / 4 } else { 1 };
    (0..h).flat_map(move |row| {
        let c = match row / band {
            0 => rgb(255, 0, 0),     // group 0
            1 => rgb(0, 255, 0),     // group 1
            2 => rgb(0, 0, 255),     // group 2
            _ => rgb(255, 255, 255), // group 3
        };
        (0..w).map(move |_| c)
    })
}

/// A horizontal RAMP inside every group band, to reveal the fold.
///
/// `quadrants()` showed which corner each RGB group lands in. It cannot show
/// what happens WITHIN a corner: each group is handed `columns` logical pixels
/// per row but drives a half-width quadrant, so there is a 2:1 fold whose
/// shape decides the remap.
///
/// So paint each group a left-to-right ramp in its own colour, with a bright
/// marker every 32 columns. How the ramp lands says it outright:
///
///   ramp runs once across the quadrant  -> the halves stack (row-major fold)
///   ramp appears TWICE side by side     -> logical row splits into two rows
///   ramp runs but markers are doubled   -> column interleave
pub fn ramps(width: u16, height: u16) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    let band = if h >= 4 { h / 4 } else { 1 };
    (0..h).flat_map(move |row| {
        let g = (row / band).min(3);
        (0..w).map(move |col| {
            // 0..255 across the full logical width
            let v = if w > 1 { (col * 255 / (w - 1)) as u8 } else { 255 };
            // a white marker every 32 columns, so position is countable
            if col % 32 == 0 {
                return rgb(255, 255, 255);
            }
            match g {
                0 => rgb(v, 0, 0),
                1 => rgb(0, v, 0),
                2 => rgb(0, 0, v),
                _ => rgb(v, v, v),
            }
        })
    })
}

/// Colour-coded column markers on a BLACK field.
///
/// `ramps()` failed as an instrument twice over: its white markers are
/// invisible on the white group, and a brightness ramp washes out entirely
/// under the camera exposure a lit panel forces. Both failures come from
/// encoding the answer in BRIGHTNESS against a lit background.
///
/// So encode it in HUE against black. Every 64th logical column gets a
/// 2px vertical line in a distinct colour:
///
///   col   0 red     col 256 magenta
///   col  64 green    col 320 cyan
///   col 128 blue     col 384 white
///   col 192 yellow   col 448 orange
///
/// Each group band also gets a stub bar at its top-left, `g+1` blocks long,
/// so the band can be identified even if the colours surprise us.
///
/// Reading it: the colours that appear in a quadrant say WHICH logical
/// columns reach the glass, and their left-to-right order says whether the
/// mapping is straight, reversed, or interleaved.
pub fn colmarks(width: u16, height: u16) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    let band = if h >= 4 { h / 4 } else { 1 };
    (0..h).flat_map(move |row| {
        let g = (row / band).min(3);
        let row_in_band = row % band;
        (0..w).map(move |col| {
            // group-identifying stub: (g+1) blocks of 8px on the top 4 rows
            if row_in_band < 4 && col < (g + 1) * 8 {
                return rgb(255, 255, 255);
            }
            if col % 64 < 2 {
                return match (col / 64) % 8 {
                    0 => rgb(255, 0, 0),       // red
                    1 => rgb(0, 255, 0),       // green
                    2 => rgb(0, 0, 255),       // blue
                    3 => rgb(255, 255, 0),     // yellow
                    4 => rgb(255, 0, 255),     // magenta
                    5 => rgb(0, 255, 255),     // cyan
                    6 => rgb(255, 255, 255),   // white
                    _ => rgb(255, 128, 0),     // orange
                };
            }
            rgb(0, 0, 0)
        })
    })
}

/// Framebuffer coordinate -> source-image coordinate for one output's module.
///
/// IDENTITY since gateware 1.18.0, and kept only so the diagnostic patterns
/// that call it need no edit. The history matters, because this function is
/// where two layers of software compensation used to live:
///
/// * The QUADRANT PLACEMENT. The bench P1.25 module is four 128x64 groups in
///   a 2x2 (g0 top-right, g1 bottom-right, g2 top-left, g3 bottom-left,
///   measured on glass 2026-10-17), while the S-PWM stage addressed the
///   framebuffer as a HUB75-style vertical stack, 128 wide x 256 tall. This
///   function translated between the two -- for TEST PATTERNS ONLY. Every raw
///   framebuffer write (marquee's stream, the boot screen) bypassed it and
///   landed scrambled. The placement is now the gateware's job: each group
///   has an origin CSR (`group<g>_origin`), the framebuffer is the glass
///   (256x128), and the arrangement is data in the panel preset and the
///   layout config (`group_origins:`). See docs/PANELS.md.
/// * The SERIALISER ROTATION, 15-18 columns per group, a pipeline-latency
///   fault in the output stage that was compensated here until the stage was
///   fixed to latch after the word reached the chain (`854e8ae`, `d1f7b86`).
///
/// Neither belongs in a pattern. What remains: `col` walks the whole virtual
/// wall, one `logical_w`-wide module per output side by side, so reduce to
/// the column within this output's module. `scan` is unused and kept for the
/// callers' signature.
pub const QUAD_W: usize = 128;
pub const QUAD_H: usize = 64;

#[inline]
pub fn quad_src_xy(row: usize, col: usize, logical_w: usize, _scan: usize) -> Option<(usize, usize)> {
    if logical_w == 0 {
        return None;
    }
    Some((col % logical_w, row))
}

/// A 256x128 orientation reference: white border, magenta diagonal, and a
/// differently-coloured block in each corner. If the remap is right this
/// lands the right way up with green at the TOP-LEFT of the panel.
fn orient_px(x: usize, y: usize) -> u32 {
    if x == 0 || y == 0 || x == 255 || y == 127 {
        return rgb(255, 255, 255);
    }
    if x < 20 && y < 20 { return rgb(0, 255, 0); }        // top-left    GREEN
    if x > 235 && y < 20 { return rgb(255, 0, 0); }       // top-right   RED
    if x < 20 && y > 107 { return rgb(0, 0, 255); }       // bottom-left BLUE
    if x > 235 && y > 107 { return rgb(255, 255, 0); }    // bottom-right YELLOW
    if x / 2 == y { return rgb(255, 0, 255); }            // diagonal
    rgb(0, 0, 0)
}

/// The orientation reference, written through `quad_src_xy`.
pub fn remap_test(width: u16, height: u16, outputs: usize) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    let scan = if h >= 4 { h / 4 } else { 1 };
    let logical_w = if outputs > 0 { w / outputs } else { w };
    (0..h).flat_map(move |row| {
        (0..w).map(move |col| match quad_src_xy(row, col, logical_w, scan) {
            Some((ix, iy)) => orient_px(ix, iy),
            None => rgb(0, 0, 0),
        })
    })
}

/// Fine column markers INSIDE the surviving window, every 8 logical columns
/// from 128 to 255, in 16 distinct hues. `cols` established which 64-column
/// band survives; this resolves the mapping within it -- in particular
/// whether consecutive logical columns land consecutively on the glass.
pub fn colmarks_fine(width: u16, height: u16, outputs: usize) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    let logical_w = if outputs > 0 { w / outputs } else { w };
    let win = logical_w.saturating_sub(QUAD_W);
    (0..h).flat_map(move |_row| {
        (0..w).map(move |col| {
            // Per-STRIP window, same reason as quad_src_xy: `col` walks the
            // whole virtual framebuffer and `logical_w` is one output's width,
            // so an absolute test lights only J1.
            if logical_w == 0 {
                return rgb(0, 0, 0);
            }
            let within = col % logical_w;
            if within < win || within >= win + QUAD_W || (within - win) % 8 != 0 {
                return rgb(0, 0, 0);
            }
            // 16 hues around the wheel, one per 8-column step
            match ((within - win) / 8) % 16 {
                0 => rgb(255, 0, 0),      1 => rgb(255, 96, 0),
                2 => rgb(255, 192, 0),    3 => rgb(192, 255, 0),
                4 => rgb(0, 255, 0),      5 => rgb(0, 255, 128),
                6 => rgb(0, 255, 255),    7 => rgb(0, 128, 255),
                8 => rgb(0, 0, 255),      9 => rgb(128, 0, 255),
                10 => rgb(255, 0, 255),   11 => rgb(255, 0, 128),
                12 => rgb(255, 255, 255), 13 => rgb(128, 128, 128),
                14 => rgb(255, 128, 128), 15 => rgb(128, 255, 128),
                _ => rgb(0, 0, 0),
            }
        })
    })
}

/// Label EVERY column by its position within a 16-channel chip.
///
/// This measures the channel permutation directly instead of inferring it
/// from box edges and borders. Every earlier constant on this panel was fitted
/// to sparse features -- a corner block, a 1px border -- and each observation
/// was consistent with several different mappings, so a value that looked
/// right locally broke elsewhere. Labelling every column removes the guessing.
///
/// Column x gets the hue for `x % 16`, full height, across the whole width.
/// Read the hue ORDER off any 16-column run and that IS the chip's channel
/// mapping:
///
///   red orange yellow ... in order  -> identity, no permutation
///   order reversed                  -> channels run 15..0 (chan_rev)
///   two interleaved runs of 8       -> the halves are swapped or interleaved
///   one hue missing, another twice  -> a single displaced channel
///
/// Known from glass 2026-10-18: the red corner block loses the column at
/// x=240 (= 15*16, a chip boundary) and it reappears 8 columns on, so the
/// permutation has period 16 with an 8-column component.
pub fn chanmap(width: u16, height: u16, outputs: usize) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    let scan = if h >= 4 { h / 4 } else { 1 };
    let logical_w = if outputs > 0 { w / outputs } else { w };
    (0..h).flat_map(move |row| {
        (0..w).map(move |col| match quad_src_xy(row, col, logical_w, scan) {
            None => rgb(0, 0, 0),
            Some((ix, _iy)) => match ix % 16 {
                0 => rgb(255, 0, 0),      1 => rgb(255, 110, 0),
                2 => rgb(255, 220, 0),    3 => rgb(170, 255, 0),
                4 => rgb(0, 255, 0),      5 => rgb(0, 255, 140),
                6 => rgb(0, 230, 255),    7 => rgb(0, 110, 255),
                8 => rgb(40, 0, 255),     9 => rgb(150, 0, 255),
                10 => rgb(255, 0, 230),   11 => rgb(255, 0, 110),
                12 => rgb(255, 255, 255), 13 => rgb(90, 90, 90),
                14 => rgb(255, 160, 160), _ => rgb(160, 255, 160),
            },
        })
    })
}

/// Encode a column's position within its 16-channel chip as a BAR HEIGHT.
///
/// `chanmap` encodes it as hue, and hue does not survive a photograph of a
/// lit LED panel -- bloom, moire and white balance make a 16-colour sequence
/// unreadable, which is how several wrong constants got fitted on this panel.
/// Height is geometric: it survives any exposure.
///
/// In every 16-column group, the column at `x % 16 == k` lights from the top
/// of its quadrant down to `(k + 1) * 4` rows. So each chip draws a staircase:
///
///   clean ascending staircase, short -> tall  -> channels in order
///   staircase with one step out of place      -> that channel is displaced
///   two interleaved ramps                     -> the banks of 8 are swapped
///
/// Read the step heights left to right and that sequence IS the channel map.
/// Every 16-column group should draw an identical staircase; one that differs
/// marks a chip that is wired or addressed differently from its neighbours.
pub fn chanstair(width: u16, height: u16, outputs: usize) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    let scan = if h >= 4 { h / 4 } else { 1 };
    let logical_w = if outputs > 0 { w / outputs } else { w };
    (0..h).flat_map(move |row| {
        (0..w).map(move |col| match quad_src_xy(row, col, logical_w, scan) {
            None => rgb(0, 0, 0),
            Some((ix, iy)) => {
                let k = ix % 16;
                let y = iy % QUAD_H;
                if y < (k + 1) * 4 {
                    // every 4th chip tinted, so chip boundaries are countable
                    if (ix / 16) % 2 == 0 { rgb(255, 255, 255) } else { rgb(0, 200, 255) }
                } else {
                    rgb(0, 0, 0)
                }
            }
        })
    })
}

/// Light ONLY the first and last channel of each chip. Unmistakable.
///
/// The `stair` pattern proved too subtle to read: two photographs of opposite
/// mappings looked alike, so a staircase cannot settle this. Two thin lines
/// can. Source column `x % 16 == 0` is WHITE, `x % 16 == 15` is RED, and
/// everything else is black.
///
///   white at the LEFT of each 16-column group, red at the RIGHT -> identity
///   white at the RIGHT, red at the LEFT                        -> reversed
///
/// The pair also makes the chip pitch visible: white and red should alternate
/// every 16 columns with nothing between them lit.
pub fn chan0(width: u16, height: u16, outputs: usize) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    let scan = if h >= 4 { h / 4 } else { 1 };
    let logical_w = if outputs > 0 { w / outputs } else { w };
    (0..h).flat_map(move |row| {
        (0..w).map(move |col| match quad_src_xy(row, col, logical_w, scan) {
            None => rgb(0, 0, 0),
            Some((ix, _)) => match ix % 16 {
                0 => rgb(255, 255, 255),
                15 => rgb(255, 0, 0),
                _ => rgb(0, 0, 0),
            },
        })
    })
}

/// Colour the two HALVES of each chip differently: channels 0-7 white,
/// 8-15 red.
///
/// `chan0` lights only channels 0 and 15, and both are FIXED POINTS of a
/// half-chip interleave (0,8,1,9,...,7,15 maps 0->0 and 15->15). So chan0
/// reads perfectly while every channel between them is scrambled -- which is
/// exactly the state this panel was in: borders correct, but a column inside
/// each corner block displaced by +8, half a chip.
///
///   8 white then 8 red, repeating every 16  -> identity, halves contiguous
///   white/red ALTERNATING every column      -> the halves are interleaved
///   8 red then 8 white                      -> the halves are swapped
///
/// Blocks versus fine alternation is the easiest distinction to make on a lit
/// panel -- it survives bloom and a camera, unlike a 16-step staircase.
pub fn chanhalf(width: u16, height: u16, outputs: usize) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    let scan = if h >= 4 { h / 4 } else { 1 };
    let logical_w = if outputs > 0 { w / outputs } else { w };
    (0..h).flat_map(move |row| {
        (0..w).map(move |col| match quad_src_xy(row, col, logical_w, scan) {
            None => rgb(0, 0, 0),
            Some((ix, _)) => {
                if ix % 16 < 8 { rgb(255, 255, 255) } else { rgb(255, 0, 0) }
            }
        })
    })
}

/// Light a handful of KNOWN source columns, each a different colour, and see
/// where they land. Measures the raw mapping's SHAPE, not one parameter.
///
/// Everything before this fitted a parametric form (shift, then reversal,
/// then interleave) to two or three sparse observations -- a box edge, a
/// border. Each fit was locally right and wrong elsewhere, which is the
/// signature of a wrong model rather than wrong constants. And every earlier
/// pattern was read THROUGH the SERIALISER_ROT compensation, so it showed
/// hardware-map composed with our map and could not separate them. Run this
/// with SERIALISER_ROT = 0 to see the hardware alone.
///
/// Markers, by source column within the quadrant:
///
///   x=0    WHITE     x=1    RED       (adjacent: is the map locally +1?)
///   x=16   GREEN     x=17   BLUE      (one chip on: same local step?)
///   x=64   YELLOW                     (half way: is the step constant?)
///   x=127  MAGENTA                    (last column: where does the end go?)
///
/// Reading it:
///   white at 0, red at 1, green at 16 ...  -> identity, no offset at all
///   all six shifted by the same k          -> pure shift of k, one constant
///   white/red adjacent but green elsewhere -> not a shift; chip-level jumble
///   a marker missing                       -> that column is dropped, not moved
pub fn probe(width: u16, height: u16, outputs: usize) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    let scan = if h >= 4 { h / 4 } else { 1 };
    let logical_w = if outputs > 0 { w / outputs } else { w };
    (0..h).flat_map(move |row| {
        (0..w).map(move |col| match quad_src_xy(row, col, logical_w, scan) {
            None => rgb(0, 0, 0),
            Some((ix, _)) => match ix % QUAD_W {
                0 => rgb(255, 255, 255),
                1 => rgb(255, 0, 0),
                16 => rgb(0, 255, 0),
                17 => rgb(0, 80, 255),
                64 => rgb(255, 255, 0),
                127 => rgb(255, 0, 255),
                _ => rgb(0, 0, 0),
            },
        })
    })
}

/// Every column displays its own SOURCE index as a vertical binary barcode.
///
/// This measures all 128 mappings at once with no model assumed. Everything
/// before it fitted a parametric family -- shift, reversal, interleave -- to
/// two or three marker positions, and each fit was right locally and wrong
/// elsewhere, which is the signature of guessing the wrong family rather than
/// the wrong constants.
///
/// The quadrant is QUAD_H=64 rows, split into 7 bands of 8 rows with a 1-row
/// gap. Band k (k=0 at the TOP) lights when bit k of the column's source
/// index is set, so 7 bits covers 0..127 exactly:
///
///   top band    = bit 0 (value 1)
///   ...
///   bottom band = bit 6 (value 64)
///
/// Read a column's lit bands top to bottom, add the values, and that is the
/// source column being displayed there. Bit 0 alternates every column, which
/// also makes the chip pitch and any interleave obvious at a glance.
///
/// Run with SERIALISER_ROT = 0 to measure the hardware alone.
pub fn bincode(width: u16, height: u16, outputs: usize) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    let scan = if h >= 4 { h / 4 } else { 1 };
    let logical_w = if outputs > 0 { w / outputs } else { w };
    (0..h).flat_map(move |row| {
        (0..w).map(move |col| match quad_src_xy(row, col, logical_w, scan) {
            None => rgb(0, 0, 0),
            Some((ix, iy)) => {
                let y = iy % QUAD_H;
                let band = y / 9;          // 7 bands of 9 rows = 63 of 64
                if band >= 7 || y % 9 == 8 {
                    return rgb(0, 0, 0);   // 1-row gap between bands
                }
                if (ix % QUAD_W) >> band & 1 == 1 {
                    // tint every other band so they are countable
                    if band % 2 == 0 { rgb(255, 255, 255) } else { rgb(0, 220, 255) }
                } else {
                    rgb(0, 0, 0)
                }
            }
        })
    })
}

/// The rightmost 16 source columns, individually identifiable.
///
/// Reads the right edge directly instead of inferring it. The LAST column
/// (x=255, the border) is WHITE; the fifteen to its left alternate RED and
/// GREEN. Everything else is black.
///
/// Source, right to left:
///   255 WHITE | 254 RED | 253 GREEN | 252 RED | 251 GREEN | ... | 240 RED
///
/// Reading it on the panel, right to left:
///   white hard against the right edge, then red, green, red...
///       -> identity; the right edge is correct
///   red at the edge and white one column IN
///       -> columns are swapped in PAIRS (254<->255), i.e. chan ^ 1
///   white absent, red at the edge
///       -> shifted; the last column is pushed off and lost
///   alternation at the wrong pitch
///       -> the step between adjacent columns is not 1
pub fn redge(width: u16, height: u16, outputs: usize) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    let scan = if h >= 4 { h / 4 } else { 1 };
    let logical_w = if outputs > 0 { w / outputs } else { w };
    (0..h).flat_map(move |row| {
        (0..w).map(move |col| match quad_src_xy(row, col, logical_w, scan) {
            None => rgb(0, 0, 0),
            Some((ix, _)) => {
                if ix == 2 * QUAD_W - 1 {
                    rgb(255, 255, 255)          // x=255, the border column
                } else if ix >= 2 * QUAD_W - 16 {
                    if (2 * QUAD_W - 1 - ix) % 2 == 1 { rgb(255, 0, 0) }
                    else { rgb(0, 255, 0) }
                } else {
                    rgb(0, 0, 0)
                }
            }
        })
    })
}

/// Alternating white and black SCAN rows -- tests the "previous row" rule.
///
/// Solid white is clean, so the 16-pitch dots are not garbage: one pixel per
/// chip is showing a NEIGHBOURING pixel's value, which only shows when the
/// neighbour differs. Every observation fits one rule -- the stale value is
/// the PREVIOUS SCAN ROW at the same column:
///
///   y=0   (white border) <- y=63  black interior -> BLACK dot
///   y=64  (black)        <- y=127 white border   -> WHITE dot
///   y=127 (white border) <- y=126 black interior -> BLACK dot
///
/// Alternating rows make that maximally visible: if the rule holds, EVERY
/// row shows inverted dots at a 16-column pitch, because every row's
/// predecessor is the opposite colour. Clean rows would falsify it.
///
/// Rows alternate by SCAN row (iy within the quadrant), not image row, since
/// that is the order the hardware walks.
/// Row identity: four distinct hues cycling every four scan rows.
///
/// `hstripe` alternates white/black, so a wrong pixel can only say "this came
/// from an adjacent row" -- it cannot say WHICH adjacent row, and that is
/// exactly the bit needed to know whether the correction advances or retards.
/// Alternating black and white is also ambiguous two rows out.
///
/// Here row r takes hue r mod 4:
///
///   0 RED   1 GREEN   2 BLUE   3 WHITE
///
/// Column-independent, so the channel order and the pair swap cannot affect
/// it, and no `quad_src_xy` compensation is applied. Every pixel in a row is
/// the same colour, so a wrong one is unmistakable and NAMES ITS SOURCE ROW:
///
///   a GREEN dot in a RED row    -> the pixel came from the row BELOW (r+1)
///   a WHITE dot in a RED row    -> from the row ABOVE (r-1), since 3 precedes 0
///
/// So one photograph gives both the offset's DIRECTION and its SIZE mod 4,
/// which is what decides between `sweep_adv` and its opposite.
pub fn rowid(width: u16, height: u16) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    (0..h).flat_map(move |row| {
        let c = match row % 4 {
            0 => rgb(255, 0, 0),
            1 => rgb(0, 255, 0),
            2 => rgb(0, 0, 255),
            _ => rgb(255, 255, 255),
        };
        (0..w).map(move |_col| c)
    })
}

/// `rowid`, pre-shifted by ONE SWEEP in software.
///
/// This emulates the gateware's `sweep_adv = 1` WITHOUT a power cycle, which
/// matters because a bitstream only goes live on one and the correction is
/// otherwise untestable.
///
/// If the chip's write pointer runs one ahead of our latch index, shifting the
/// fetch ADDRESS forward by a sweep and shifting the framebuffer CONTENT
/// backward by a sweep are the same correction seen from opposite ends. The
/// gateware does the former; this does the latter, in the pattern.
///
/// The shift is `fb(r, 16s+k) = intended(r, 16s+k+1)`, carrying into the next
/// row at `k = 15`. For a row-indexed pattern every column within a chip holds
/// the same hue, so the ONLY column this changes is `k = 15`, which takes row
/// r+1's hue -- and that is precisely the pixel the chip is about to misplace.
///
/// So: display `rowid` and then `rowid_adv`. If the dots are present in the
/// first and gone in the second, the pointer-offset model is confirmed and
/// `sweep_adv = 1` in gateware 1.17.0 is the permanent fix. If both look the
/// same, the model is wrong and the gateware change will not help either.
pub fn rowid_adv(width: u16, height: u16) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    fn hue(r: usize) -> u32 {
        match r % 4 {
            0 => rgb(255, 0, 0),
            1 => rgb(0, 255, 0),
            2 => rgb(0, 0, 255),
            _ => rgb(255, 255, 255),
        }
    }
    (0..h).flat_map(move |row| {
        (0..w).map(move |col| {
            // one sweep forward in the fetch order, carrying into the row
            if col % 16 == 15 { hue(row + 1) } else { hue(row) }
        })
    })
}

/// Sweep-shift CALIBRATION: a different correction in each quadrant.
///
/// One glance gives both the answer and the value. `rowid` vs `rowid_adv` is an
/// A/B that tests ONE candidate and needs two renders; this tests four at once
/// and needs none -- whichever quadrant has no dots names the correct shift.
///
///   quadrant (group) 0 -> shift 0    (no correction: the control)
///                    1 -> shift 1    (the derived value)
///                    2 -> shift 2
///                    3 -> shift 15   (= -1, the opposite sign)
///
/// The shift is the software equivalent of the gateware's `sweep_adv`: if the
/// chip's write pointer runs ahead of our latch index, moving the fetch address
/// forward and moving the framebuffer content backward are the same correction
/// from opposite ends. So `fb(r, 16s+k) = intended(r, 16s+k+shift)`, carrying
/// into the next row when k+shift passes 15.
///
/// Rows are hued r mod 4 (red, green, blue, white) so a misplaced pixel NAMES
/// its source row rather than merely being "adjacent" -- giving direction as
/// well as presence.
///
/// Reading it:
///   group 0 has dots and group 1 does not -> shift 1 is the fix, confirmed
///   group 3 is the clean one             -> the sign is inverted, use 15
///   ALL FOUR look identical              -> the pointer-offset model is wrong
///                                           and no shift will help; stop and
///                                           put a scope on LE
///
/// It is a TEST PATTERN, so a wrong model costs nothing: normal content is
/// untouched. That is deliberate -- applying an unverified shift globally
/// would make every pixel wrong instead of one per chip if the model is bad.
pub fn sweepcal(width: u16, height: u16, scan: usize) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    let sc = if scan == 0 { 64 } else { scan };
    fn hue(r: usize) -> u32 {
        match r % 4 {
            0 => rgb(255, 0, 0),
            1 => rgb(0, 255, 0),
            2 => rgb(0, 0, 255),
            _ => rgb(255, 255, 255),
        }
    }
    const SHIFTS: [usize; 4] = [0, 1, 2, 15];
    (0..h).flat_map(move |row| {
        let g = (row / sc) & 3;
        let shift = SHIFTS[g];
        (0..w).map(move |col| {
            let k = col % 16;
            let ks = k + shift;
            // carry into the next row when the shift passes the chip boundary
            let src_row = if ks >= 16 { row + 1 } else { row };
            hue(src_row)
        })
    })
}

/// RAW chip-channel ends, with NO compensation of any kind.
///
/// Every other column pattern in this file goes through `quad_src_xy`, which
/// applies `SERIALISER_ROT` and the `p ^ 1` pair swap. Those were measured and
/// tuned WITH `chan_rev = 0`, so using any of them to judge `chan_rev` tests
/// the compensation and the channel order together and cannot separate them.
/// Every attempt at that question so far has been confounded that way.
///
/// This one writes the framebuffer position directly:
///
///   column 0 of each 16  -> RED
///   column 15 of each 16 -> BLUE
///   everything else      -> dim grey, so the blocks are countable
///
/// It is column-periodic and row-independent, so the quadrant/row mapping
/// cannot affect it either. On the panel each 16-wide chip then shows a red
/// end and a blue end, and WHICH END IS WHICH reads the channel order straight
/// off the glass:
///
///   red on the LEFT of each block  -> our column 0 reaches channel 0; the
///                                     order we send matches the panel
///   red on the RIGHT of each block -> the sixteen channels are reversed
///                                     inside every driver, which is what
///                                     DP3264S_REV2.0 11.2 says to expect
///                                     (it sends channel 15 first) and what
///                                     `chan_rev` exists to correct
///
/// docs/ICN1065.md records 11.2 as "not confirmed, and still suspect ... the
/// first thing to re-check once the panel lights". This is that check.
pub fn chanends(width: u16, height: u16) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    (0..h).flat_map(move |_row| {
        (0..w).map(move |col| match col % 16 {
            0 => rgb(255, 0, 0),
            15 => rgb(0, 0, 255),
            _ => rgb(24, 24, 24),
        })
    })
}

pub fn hstripe(width: u16, height: u16, outputs: usize) -> impl Iterator<Item = u32> {
    let w = width as usize;
    let h = height as usize;
    let scan = if h >= 4 { h / 4 } else { 1 };
    let logical_w = if outputs > 0 { w / outputs } else { w };
    (0..h).flat_map(move |row| {
        (0..w).map(move |col| match quad_src_xy(row, col, logical_w, scan) {
            None => rgb(0, 0, 0),
            Some((_, iy)) => {
                if (iy % QUAD_H) % 2 == 0 { rgb(255, 255, 255) } else { rgb(0, 0, 0) }
            }
        })
    })
}

/// Generate solid white pattern
pub fn solid_white(width: u16, height: u16) -> impl Iterator<Item = u32> {
    solid(width, height, 255, 255, 255)
}

/// Generate solid red pattern
pub fn solid_red(width: u16, height: u16) -> impl Iterator<Item = u32> {
    solid(width, height, 255, 0, 0)
}

/// Generate solid green pattern
pub fn solid_green(width: u16, height: u16) -> impl Iterator<Item = u32> {
    solid(width, height, 0, 255, 0)
}

/// Generate solid blue pattern
pub fn solid_blue(width: u16, height: u16) -> impl Iterator<Item = u32> {
    solid(width, height, 0, 0, 255)
}
