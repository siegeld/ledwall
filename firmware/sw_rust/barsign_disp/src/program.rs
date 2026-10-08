//! On-panel programs -- code that composes pixels HERE, with no frame ever sent.
//!
//! The ESPHome bargain: you write the logic, it is COMPILED into the firmware
//! and runs at native speed. Not interpreted -- DISPLAY-PROGRAMS §5 measured a
//! per-pixel interpreter at ~0.3 fps, so a scripting VM is off the table.
//! `panelc` will generate these fns from YAML; this module is the API they are
//! written against.
//!
//! A program is a pure fn of (x, y, frame, values) -> PALETTE INDEX. Indices,
//! not colours, because that is what buys the two O(1) tricks: an anti-aliased
//! glyph or icon is `ramp_base + coverage` (one add, no blend), and recolouring
//! anything rewrites 16 palette words while the framebuffer never moves --
//! independent of how many panels are in the wall.

use crate::values::ValueTable;

// ---- palette layout -------------------------------------------------------
// Flat colours low, then 16-entry ramps running background -> foreground. A
// sprite's 4-bit coverage indexes directly into a ramp.
pub const BG: u8 = 0;
pub const SLATE: u8 = 1;
pub const BRASS: u8 = 2;
pub const ALERT: u8 = 3;
pub const RAMP_WHITE: u8 = 16;
pub const RAMP_BRASS: u8 = 32;
pub const RAMP_SLATE: u8 = 48;
/// The streamed show's irrigation blue.
///
/// 192, NOT 64. 64 is where RAINBOW starts and it runs 128 entries to 191, so a
/// ramp at 64 shadows the first sixteen hues -- `palette_default` tests the
/// ramps before the rainbow, so `hue()` would quietly return water-blue for the
/// start of every sweep. Nothing using only ramps would notice; every program
/// using `hue()` would, and the cause would look like a broken rainbow rather
/// than a ramp in the wrong place. 192..207 is past the end of the rainbow.
pub const RAMP_WATER: u8 = 192;
/// Theatre bulbs, 16 phases. A pixel on the ring takes the index of the bulb it
/// belongs to; rotating a bright group through these sixteen entries makes the
/// ring CHASE for no pixel writes at all.
pub const RAMP_BULB: u8 = 208;
/// 128 entries of full-saturation hue, 64..191. Ramps give one colour plus
/// alpha, which covers text and UI icons but cannot do vivid colour -- so
/// anything that wants the whole spectrum indexes here instead. Costs nothing
/// extra: it is the same palette RAM, and picking a hue is one add.
pub const RAINBOW: u8 = 64;
pub const RAINBOW_LEN: u16 = 128;

/// Framebuffer/palette word format is 0x00BBGGRR on this panel -- NOT 0xRRGGBB. Every colour
/// goes through here, because getting it wrong swaps red and blue and reads as
/// "the panel is broken" rather than "the constant is wrong".
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
pub const fn rgb(r: u8, g: u8, b: u8) -> u32 {
    ((b as u32) << 16) | ((g as u32) << 8) | (r as u32)
}

/// The 256-entry palette a program runs against. Written once on entering
/// program mode; after that colour changes are 16-word ramp rewrites.
pub fn palette() -> impl Iterator<Item = u32> {
    (0..256usize).map(palette_default)
}

/// The standard entry for index `i`.
///
/// Exposed so a program's own `palette:` can override the ranges it cares about
/// and defer the rest here, instead of restating a layout it does not own -- a
/// copy would silently diverge the first time the flats or ramps moved.
pub fn palette_default(i: usize) -> u32 {
    {
        match i {
        0 => rgb(0, 0, 0),
        1 => rgb(95, 127, 176),
        2 => rgb(201, 168, 76),
        3 => rgb(220, 40, 40),
        _ => {
            let (base, fg) = if i >= RAMP_WATER as usize && i < RAMP_WATER as usize + 16 {
                (RAMP_WATER as usize, (74u32, 159u32, 208u32))
            } else if i >= RAMP_SLATE as usize && i < RAMP_SLATE as usize + 16 {
                (RAMP_SLATE as usize, (95u32, 127u32, 176u32))
            } else if i >= RAMP_BRASS as usize && i < RAMP_BRASS as usize + 16 {
                (RAMP_BRASS as usize, (201, 168, 76))
            } else if i >= RAMP_WHITE as usize && i < RAMP_WHITE as usize + 16 {
                (RAMP_WHITE as usize, (255, 255, 255))
            } else if i >= RAINBOW as usize && i < RAINBOW as usize + RAINBOW_LEN as usize {
                // Six linear segments round the hue circle. No float, no divide
                // beyond the constant: this runs 128 times at startup, but the
                // same shape is what a program would use per pixel.
                let t = (i - RAINBOW as usize) as u32;      // 0..127
                let seg = t * 6 / RAINBOW_LEN as u32;       // 0..5
                let f = (t * 6 % RAINBOW_LEN as u32) * 255 / RAINBOW_LEN as u32;
                let (r, g, b) = match seg {
                    0 => (255, f, 0),
                    1 => (255 - f, 255, 0),
                    2 => (0, 255, f),
                    3 => (0, 255 - f, 255),
                    4 => (f, 0, 255),
                    _ => (255, 0, 255 - f),
                };
                return rgb(r as u8, g as u8, b as u8);
            } else {
                return rgb(0, 0, 0);
            };
            // Linear background->foreground. Gamma is applied downstream by the
            // gateware LUT, so ramping linearly here is correct.
            let step = (i - base) as u32;
            rgb((fg.0 * step / 15) as u8, (fg.1 * step / 15) as u8, (fg.2 * step / 15) as u8)
        }
        }
    }
}

/// sin(2*pi*i/64) scaled by 256, for integer rotation without a float unit.
/// 64 steps is plenty: at 128px the largest rounding error is under a pixel.
pub static SIN64: [i32; 64] = [0,25,50,74,98,121,142,162,181,198,213,226,237,245,251,255,256,255,251,245,237,226,213,198,181,162,142,121,98,74,50,25,0,-25,-50,-74,-98,-121,-142,-162,-181,-198,-213,-226,-237,-245,-251,-255,-256,-255,-251,-245,-237,-226,-213,-198,-181,-162,-142,-121,-98,-74,-50,-25];

#[inline]
pub fn sin64(a: usize) -> i32 {
    SIN64[a & 63]
}

#[inline]
pub fn cos64(a: usize) -> i32 {
    SIN64[(a + 16) & 63]
}

/// Inverse-rotate (x, y) about (cx, cy) by `a` (0..63) into source space.
///
/// Inverse, not forward: a program is asked "what belongs at this pixel", so it
/// maps the destination back to the source and samples. Rotating forward would
/// need a framebuffer to scatter into and would leave holes.
#[inline]
pub fn unrotate(x: usize, y: usize, cx: i32, cy: i32, a: usize) -> (i32, i32) {
    let (dx, dy) = (x as i32 - cx, y as i32 - cy);
    let (s, c) = (sin64(a), cos64(a));
    (((dx * c + dy * s) >> 8) + cx, ((dy * c - dx * s) >> 8) + cy)
}

/// A cheap 2D integer hash: stable per (x, y), well mixed, no table.
///
/// Sparkle effects need each pixel to have its own pseudo-random phase. Storing
/// one would cost a framebuffer-sized array; deriving it costs a few multiplies
/// and stays O(1) per pixel, which is the budget that matters here.
#[inline]
pub fn hash2(x: usize, y: usize) -> u32 {
    // Shifts and xors ONLY -- no multiply. Written when the firmware was built
    // for plain riscv32i and every multiply was a SOFTWARE routine; the build
    // now sets `target-feature=+m` and the `lite` VexRiscv has always had the M
    // extension, so that is no longer true (docs/BENCHMARKS.md). Kept as shifts
    // anyway: it is not slower, and a hash on every one of 16384 pixels is the
    // wrong place to spend instructions of any kind.
    let mut v = (x as u32) ^ ((y as u32) << 16) ^ ((y as u32) >> 3);
    v ^= v << 13;
    v ^= v >> 17;
    v ^= v << 5;
    v
}

/// A program renders one ROW at a time.
///
/// Was one call per pixel, which cost an indirect call and a closure 16384
/// times a frame and capped a full-screen redraw near 6 fps whatever it drew --
/// measured with the `blank` program, which draws nothing. Per row it is 128
/// calls, the per-pixel body inlines into the loop, and everything that depends
/// only on y is hoisted out of it by the compiler.
/// Render part of a row: `(ctx, y, x0, slice)` fills `slice` starting at
/// absolute x = `x0`. The x origin is what lets the scroll primitive redraw
/// only the newly exposed columns instead of the whole row.
pub type RowFn = fn(&Ctx, usize, usize, &mut [u32]);

/// Per-frame palette entry. Called for entries 0..256 once a frame.
///
/// This is the cheapest animation on the panel BY A LONG WAY: 256 words against
/// 16384 for a redraw, and -- unlike pixels -- the cost does not grow with the
/// wall. Nine panels recolour for what one costs. The demoscene canon (plasma,
/// fire, colour cycling, fades) is all this, with the framebuffer held still.
pub type PaletteFn = fn(&Ctx, usize) -> u32;

/// One compiled program. A struct rather than a tuple because it has grown
/// three times and every growth touched every call site.
pub struct Program {
    pub name: &'static str,
    pub row: RowFn,
    /// True when the `palette:` body never reads `ctx`, so the 256 words are
    /// the same every frame and only have to be written once.
    ///
    /// This is not a micro-optimisation. The palette is SINGLE-buffered while
    /// the framebuffer is double-buffered and gateware-swapped (RB-5516), so
    /// rewriting it every frame means the display scans out entries that are
    /// being changed underneath it. On a streamed panel that is avoidable by
    /// sending `format: rgb`; a program has no such escape, because indexed is
    /// the whole point -- a glyph's coverage IS the ramp offset. So a static
    /// palette must be written once, not 1.7 times a second.
    pub palette_static: bool,
    /// Rows redrawn every frame; None means all of them.
    pub dynamic: Option<(usize, usize)>,
    /// Horizontal scroll bands: `(y0, y1, pixels per frame, rightward)`.
    ///
    /// Valid ONLY where EVERYTHING in the band translates together -- a ticker
    /// line on a plain ground. Then last frame's pixel at x+speed is this
    /// frame's pixel at x and the shift is exact. A band that also contains an
    /// independently animating background (a plasma, a starfield) must NOT be
    /// declared here: the shift would drag that along with the text.
    ///
    /// A slice because a board usually wants more than one, often in opposite
    /// directions.
    pub scroll: &'static [(usize, usize, usize, bool)],
    /// Per-frame palette, if the program animates colour.
    pub palette: Option<PaletteFn>,
}

/// Everything a program is handed for one frame.
pub struct Ctx<'a> {
    pub w: usize,
    pub h: usize,
    pub frame: u32,
    pub time_ms: i64,
    pub values: &'a ValueTable,
}

impl<'a> Ctx<'a> {
    pub fn val(&self, key: &str) -> &str {
        self.values.text(key)
    }
    /// Integer value. USE THIS, not `num`, in anything per-pixel.
    ///
    /// There is no FPU: `num` parses and computes in SOFT FLOAT, which is fine
    /// once a frame and ruinous 16384 times. A gauge that called it per pixel
    /// took a panel from several fps to under one.
    pub fn int(&self, key: &str) -> Option<i32> {
        let t = self.values.text(key);
        let (neg, digits) = match t.strip_prefix('-') {
            Some(d) => (true, d),
            None => (false, t),
        };
        let mut v: i32 = 0;
        let mut any = false;
        for b in digits.bytes() {
            // Stop at the first non-digit so "14 on" reads as 14 rather than
            // failing -- values arrive as human strings.
            if !b.is_ascii_digit() {
                break;
            }
            any = true;
            v = v.saturating_mul(10).saturating_add((b - b'0') as i32);
        }
        if any {
            Some(if neg { -v } else { v })
        } else {
            None
        }
    }

    /// Floating point. Convenient, but SOFT FLOAT -- keep it out of per-pixel
    /// code. See `int`.
    pub fn num(&self, key: &str) -> Option<f32> {
        self.values.number(key)
    }
    /// A sign that lies is worse than a blank one: programs must show staleness
    /// rather than presenting an old number as current.
    pub fn stale_ms(&self) -> i64 {
        self.values.stale_for_ms(self.time_ms)
    }
}
