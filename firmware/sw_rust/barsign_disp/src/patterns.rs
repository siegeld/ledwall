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

/// Is (col, row) lit for the boot version banner, drawn at a fixed top-left
/// origin? Independent of the grid pattern's layout so it can be overlaid on
/// whatever image is showing.
///
/// Exists because identifying the running firmware otherwise means correlating
/// a TFTP log with a build artifact -- which cost real debugging time when a
/// stale daemon served a months-old boot.bin and everything looked normal.
pub fn boot_version_pixel(col: usize, row: usize) -> bool {
    const ORIGIN_X: usize = 2;
    const ORIGIN_Y: usize = 2;
    if row < ORIGIN_Y || row >= ORIGIN_Y + 7 {
        return false;
    }
    let text_width = VERSION_TEXT.len() * 6;
    if col < ORIGIN_X || col >= ORIGIN_X + text_width {
        return false;
    }
    let char_idx = (col - ORIGIN_X) / 6;
    let pixel_x = (col - ORIGIN_X) % 6;
    let pixel_y = row - ORIGIN_Y;
    if pixel_x >= 5 {
        return false;
    }
    if let Some(ch) = VERSION_TEXT.chars().nth(char_idx) {
        for (glyph, c) in FONT_5X7 {
            if *c == ch {
                return (glyph[pixel_y] >> (4 - pixel_x)) & 1 == 1;
            }
        }
    }
    false
}

/// Check if pixel (col, row) should be lit for version text in second-row left square (avoids diagonals)
fn is_version_pixel(col: usize, row: usize, width: usize, height: usize) -> bool {
    const VERSION: &str = concat!("v", env!("CARGO_PKG_VERSION"));
    let text_width = VERSION.len() * 6;
    // Position in second row, leftmost square (between h/4 and h/2, left of w/4)
    // This area avoids both diagonal lines
    // The banner is NOT drawn when it will not fit the second-row cell.
    //
    // It used to compute `square_right - square_left - text_width` in usize,
    // which UNDERFLOWS at 128 wide (31 - 1 - 42) and wraps to ~1.8e19 in a
    // release build, so nothing drew -- by accident. Centring it across the
    // full width instead made it draw over a test pattern it has no business
    // covering: version pixels outrank the diagonals and verticals and are
    // painted white. Suppressing it is the honest behaviour, and the firmware
    // version is on the web UI where it belongs.
    let square_left = 1usize;
    let square_right = width / 4 - 1;
    let square_top = height / 4 + 1;
    let square_bottom = height / 2 - 1;
    if square_bottom <= square_top + 7 {
        return false;
    }
    let start_y = square_top + (square_bottom - square_top - 7) / 2;
    // The banner wants the second-row LEFT cell, which is the one region the
    // diagonals miss. At 128 wide that cell is 30px and the string needs 42, so
    // it has to cross something. Left-align it and it crosses only the vertical
    // at w/4 -- a 7px interruption in one straight line. Centring it across the
    // full width instead drops it onto BOTH diagonals, and since version pixels
    // outrank everything and paint white, that is what wrecked the pattern.
    let start_x = if square_right > square_left + text_width {
        square_left + (square_right - square_left - text_width) / 2
    } else {
        square_left + 1
    };

    if row < start_y || row >= start_y + 7 {
        return false;
    }
    if col < start_x || col >= start_x + text_width {
        return false;
    }

    let char_idx = (col - start_x) / 6;
    let pixel_x = (col - start_x) % 6;
    let pixel_y = row - start_y;

    if pixel_x >= 5 {
        return false; // spacing between chars
    }

    if let Some(ch) = VERSION.chars().nth(char_idx) {
        for (glyph, c) in FONT_5X7 {
            if *c == ch {
                let row_bits = glyph[pixel_y];
                return (row_bits >> (4 - pixel_x)) & 1 == 1;
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

    (0..h).flat_map(move |row| {
        (0..w).map(move |col| {
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

            // Priority: version text > diagonals > verticals > horizontals > black
            if is_version_pixel(col, row, w, h) {
                rgb(255, 255, 255) // WHITE for version text
            } else if diag1 {
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
        })
    })
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
