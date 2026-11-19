//! 4bpp alpha sprites and fonts -- the ONE asset type, per DISPLAY-PROGRAMS §7.
//!
//! An anti-aliased SVG icon and a large glyph are the same thing: a rectangle of
//! 4-bit coverage values. They are produced by the same host-side rasteriser and
//! consumed by the same code here, which is the whole point of the design.
//!
//! ## Why coverage, and why a ramp
//!
//! Reserve 16 consecutive palette entries running background -> foreground. A
//! sprite's nibble IS the offset into that ramp:
//!
//! ```text
//! index = ramp_base + nibble        // one add. no blend, no multiply
//! ```
//!
//! So anti-aliasing costs nothing at draw time, and recolouring an icon rewrites
//! 16 palette words while the framebuffer never moves -- the same O(1)-in-wall-
//! size property as palette cycling.
//!
//! ## Why lookup instead of blit
//!
//! A program is a pure fn of (x, y) -> colour, so sprites are QUERIED rather
//! than blitted into a buffer. That keeps programs composable and free of
//! framebuffer aliasing, and it costs the same: a blit writes each pixel once,
//! this reads each pixel once.

/// A rectangle of 4-bit coverage, two pixels per byte, high nibble first.
pub struct Sprite {
    pub w: u16,
    pub h: u16,
    pub data: &'static [u8],
}

impl Sprite {
    /// Coverage at (x, y), or None outside the rectangle. 0 means fully
    /// transparent -- callers should treat it as "draw nothing", not "draw the
    /// darkest ramp entry", or every sprite gets a box around it.
    #[inline]
    pub fn cover(&self, x: usize, y: usize) -> Option<u8> {
        if x >= self.w as usize || y >= self.h as usize {
            return None;
        }
        let i = y * self.w as usize + x;
        let b = self.data[i >> 1];
        Some(if i & 1 == 0 { b >> 4 } else { b & 0x0F })
    }

    /// Coverage at an absolute position given the sprite's origin.
    #[inline]
    pub fn cover_at(&self, x: usize, y: usize, ox: usize, oy: usize) -> Option<u8> {
        if x < ox || y < oy {
            return None;
        }
        match self.cover(x - ox, y - oy) {
            Some(0) | None => None,
            c => c,
        }
    }
}

pub struct Glyph {
    pub ch: char,
    pub sprite: Sprite,
    /// Pen offsets from the text origin, so glyphs sit on a shared baseline
    /// instead of each being top-aligned in its own box.
    pub xoff: i16,
    pub yoff: i16,
    pub advance: u16,
}

pub struct Font {
    pub glyphs: &'static [Glyph],
    /// Distance from the text origin down to the baseline.
    pub ascent: u16,
    pub line_height: u16,
    /// Advance width when every glyph shares one, else 0. A monospace face is
    /// what makes SCROLLING text affordable: the character under a pixel is one
    /// divide instead of walking the run, so cost stops depending on message
    /// length. A 40-character ticker in a proportional face would be ~40 glyph
    /// lookups per pixel, 16384 pixels a frame.
    pub fixed_advance: u16,
    /// log2(fixed_advance) when that is a power of two, else 0.
    ///
    /// `cover_scroll` did three div/mod per pixel, and the advance was already
    /// 16 -- the compiler simply cannot know that from a runtime struct field,
    /// so it emitted real divides. Stating it turns two of the three into a
    /// shift and a mask. (The CPU has the M extension, so these were hardware
    /// divides, not software routines -- but iterative ones, per pixel.)
    pub adv_shift: u8,
    /// ASCII 32..127 -> glyph index, 255 for absent. Turns glyph lookup from a
    /// linear scan of ~90 glyphs into an array read.
    pub ascii: &'static [u8; 96],
}

impl Font {
    pub fn glyph(&self, ch: char) -> Option<&Glyph> {
        let c = ch as usize;
        if (32..128).contains(&c) {
            let i = self.ascii[c - 32];
            if i != 255 {
                return self.glyphs.get(i as usize);
            }
            return None;
        }
        self.glyphs.iter().find(|g| g.ch == ch)
    }

    /// Coverage for an endlessly repeating, horizontally scrolled run.
    ///
    /// `vx` is the virtual x: pass `x + offset` and the text marches left. The
    /// message wraps, so a ticker needs no second copy drawn to cover the seam.
    /// O(1) -- requires a fixed-advance face.
    pub fn cover_scroll(&self, s: &str, vx: usize, y: usize, oy: usize) -> Option<u8> {
        let adv = self.fixed_advance as usize;
        // Reject the whole band in ONE comparison, both sides. Without the
        // upper bound every pixel BELOW the line still did the full lookup --
        // a divide, an ascii read, a glyph read, a sprite read -- to discover
        // there is no text there. cover_at got this fix; this did not, and it
        // cost a third of the frame rate on a program with two ticker lines,
        // where ~80% of the panel is below both of them.
        if adv == 0 || s.is_empty() || y < oy || y >= oy + self.line_height as usize {
            return None;
        }
        let b = s.as_bytes();
        // Shifts and masks where the numbers allow it. Measured on the two-tape
        // demo, the tapes were 85% of a full frame at ~1140 cycles per pixel.
        let (cell, off) = if self.adv_shift > 0 {
            (vx >> self.adv_shift, vx & (adv - 1))
        } else {
            (vx / adv, vx % adv)
        };
        let n = b.len();
        // A message padded to a power-of-two length costs a mask here instead
        // of a third division -- which is why the demos pad to 64.
        let ci = if n & (n - 1) == 0 { cell & (n - 1) } else { cell % n };
        let g = self.glyph(b[ci] as char)?;
        // Glyph box sits at the pen plus its bearings, same as cover_at.
        let gx = off as i32 - g.xoff as i32;
        let gy = y as i32 - (oy as i32 + self.ascent as i32 + g.yoff as i32);
        if gx < 0 || gy < 0 {
            return None;
        }
        match g.sprite.cover(gx as usize, gy as usize) {
            Some(0) | None => None,
            c => c,
        }
    }

    /// Advance width of a string, for centring without a second pass.
    pub fn width(&self, s: &str) -> usize {
        s.chars().map(|c| self.glyph(c).map_or(0, |g| g.advance as usize)).sum()
    }

    /// Coverage of `s` drawn with its origin at (ox, oy), at absolute (x, y).
    ///
    /// Walks the run rather than indexing it: strings here are short, and a
    /// position cache would have to be invalidated per frame anyway.
    pub fn cover_at(&self, s: &str, x: usize, y: usize, ox: usize, oy: usize) -> Option<u8> {
        // Reject the whole vertical band in ONE comparison. Without the upper
        // bound every pixel BELOW the text still walked the entire run looking
        // for a glyph that could not be there -- and most of a panel is below
        // any given line, so a dashboard with three labels spent most of its
        // frame budget proving that the background is not text.
        if y < oy || y >= oy + self.line_height as usize {
            return None;
        }
        if x < ox {
            return None;
        }
        // MONOSPACE GOES STRAIGHT TO THE GLYPH. The walk below is O(run length)
        // per pixel, and for a fixed-advance face the index is arithmetic --
        // exactly what `cover_scroll` already does. A five-character clock at
        // 26px was up to five glyph iterations for every pixel across its
        // width, and it is repainted on every value arrival, which is where the
        // sign's remaining hitch came from.
        if self.fixed_advance > 0 {
            let adv = self.fixed_advance as usize;
            let dx = x - ox;
            let (cell, off) = if self.adv_shift > 0 {
                (dx >> self.adv_shift, dx & (adv - 1))
            } else {
                (dx / adv, dx % adv)
            };
            let b = s.as_bytes();
            if cell >= b.len() {
                return None;
            }
            let g = self.glyph(b[cell] as char)?;
            let gx = off as i32 - g.xoff as i32;
            let gy = y as i32 - (oy as i32 + self.ascent as i32 + g.yoff as i32);
            if gx < 0 || gy < 0 {
                return None;
            }
            return match g.sprite.cover(gx as usize, gy as usize) {
                Some(0) | None => None,
                c => c,
            };
        }
        let mut pen = ox as i32;
        for ch in s.chars() {
            let g = match self.glyph(ch) {
                Some(g) => g,
                None => continue,
            };
            let gx = pen + g.xoff as i32;
            let gy = oy as i32 + self.ascent as i32 + g.yoff as i32;
            if (x as i32) >= gx && (y as i32) >= gy {
                if let Some(c) = g.sprite.cover((x as i32 - gx) as usize, (y as i32 - gy) as usize) {
                    if c != 0 {
                        return Some(c);
                    }
                }
            }
            pen += g.advance as i32;
            if pen > x as i32 + 64 {
                break; // run has passed this pixel
            }
        }
        None
    }
}
