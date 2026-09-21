//! Drawing primitives for on-panel programs.
//!
//! A program is a pure fn of (x, y) -> palette index, so every primitive here is
//! a QUERY: "what, if anything, do you put at this pixel?" They return
//! `Option<u8>` and compose by `?`-style fallthrough, exactly like widgets --
//! the first one that answers wins.
//!
//! ## The rules these are built to respect
//!
//! Every primitive is **O(1) per pixel**, and every one takes its bounding box
//! FIRST so a pixel outside it costs one comparison. That is not style: this
//! runs 16384 times a frame on a 40MHz core with no D-cache, where a single
//! memory access -- read OR write -- costs ~190 cycles. The cheap primitives
//! here touch no memory at all; the text ones are the expensive ones precisely
//! because they must read glyph data.
//!
//! Cost, measured on the bench panel, worst to best:
//!
//! | Primitive | Per pixel |
//! |---|---|
//! | `text` / `scroll_text` | ~4 reads (ascii table, glyph, sprite byte) |
//! | `sprite` / `sprite_rotated` | 1 read |
//! | `bar`, `border`, `sparkle`, `plasma` | arithmetic only |
//!
//! And the cheapest motion is not here at all: animate the PALETTE and the
//! framebuffer never moves (256 words, flat in wall size). See `palette:` in
//! the YAML.

use crate::program::{hash2, sin64, unrotate, Ctx, RAINBOW, RAINBOW_LEN};
use crate::sprite::{Font, Sprite};

/// A rainbow index from any changing quantity. `hue(x + frame)` sweeps colour.
#[inline]
pub fn hue(v: usize) -> u8 {
    RAINBOW + (v % RAINBOW_LEN as usize) as u8
}

/// A ramp entry from 4-bit coverage: this is what makes anti-aliasing free.
#[inline]
pub fn shade(ramp_base: u8, coverage: u8) -> u8 {
    ramp_base + (coverage & 0x0F)
}

/// The panel's outer ring, `t` pixels deep.
///
/// Distance to the nearest edge, so the ring is ONE expression rather than four
/// rectangles that have to agree at the corners -- which is where hand-written
/// borders go wrong.
#[inline]
pub fn border(ctx: &Ctx, x: usize, y: usize, t: usize, color: u8) -> Option<u8> {
    let d = x.min(y).min(ctx.w - 1 - x).min(ctx.h - 1 - y);
    if d < t {
        Some(color)
    } else {
        None
    }
}

/// A border whose colour runs round the ring.
#[inline]
pub fn border_chase(ctx: &Ctx, x: usize, y: usize, t: usize, speed: usize) -> Option<u8> {
    let d = x.min(y).min(ctx.w - 1 - x).min(ctx.h - 1 - y);
    if d < t {
        Some(hue(x + y + ctx.frame as usize * speed))
    } else {
        None
    }
}

/// A filled rectangle.
#[inline]
pub fn rect(x: usize, y: usize, ox: usize, oy: usize, w: usize, h: usize, color: u8) -> Option<u8> {
    if x >= ox && x < ox + w && y >= oy && y < oy + h {
        Some(color)
    } else {
        None
    }
}

/// A horizontal bar gauge: `value` of `max`, filled `fg`, track `track`.
///
/// Integer proportion on purpose -- `ctx.num()` is soft float on this core and
/// ruinous per pixel. Feed it `ctx.int()`.
#[inline]
pub fn bar(
    x: usize, y: usize, ox: usize, oy: usize, w: usize, h: usize,
    value: i32, max: i32, fg: u8, track: u8,
) -> Option<u8> {
    if x < ox || x >= ox + w || y < oy || y >= oy + h {
        return None;
    }
    let v = value.clamp(0, max.max(1)) as usize;
    let lit = w * v / max.max(1) as usize;
    Some(if x - ox < lit { fg } else { track })
}

/// Static text, anti-aliased through a ramp.
///
/// Reads glyph data, so it is one of the expensive primitives -- but it rejects
/// the whole vertical band in one comparison, which is what keeps it affordable
/// for the ~90% of a panel that is nowhere near a given line.
#[inline]
pub fn text(font: &Font, s: &str, x: usize, y: usize, ox: usize, oy: usize, ramp: u8) -> Option<u8> {
    font.cover_at(s, x, y, ox, oy).map(|c| shade(ramp, c))
}

/// Text centred on its own advance width.
#[inline]
pub fn text_centered(ctx: &Ctx, font: &Font, s: &str, x: usize, y: usize, oy: usize, ramp: u8) -> Option<u8> {
    if y < oy || y >= oy + font.line_height as usize {
        return None;   // reject before measuring: width() walks the string
    }
    let tw = font.width(s);
    let ox = if ctx.w > tw { (ctx.w - tw) / 2 } else { 2 };
    font.cover_at(s, x, y, ox, oy).map(|c| shade(ramp, c))
}

/// An endlessly scrolling ticker line. `speed` is pixels per frame.
///
/// Needs a MONOSPACE face: the character under a pixel is then one divide
/// instead of walking the run, so cost stops depending on message length. The
/// message wraps, so no second copy is needed to cover the seam.
///
/// **The band you declare in `display.dynamic` must cover every row this can
/// ink**, not the rows it usually does -- rows outside it are written only on a
/// full frame, so anything drawn there stays as debris.
#[inline]
pub fn scroll_text(
    ctx: &Ctx, font: &Font, s: &str, x: usize, y: usize, oy: usize, speed: usize, ramp: u8,
) -> Option<u8> {
    font.cover_scroll(s, x + ctx.frame as usize * speed, y, oy)
        .map(|c| shade(ramp, c))
}

/// The same, travelling the other way.
#[inline]
pub fn scroll_text_rev(
    ctx: &Ctx, font: &Font, s: &str, x: usize, y: usize, oy: usize, speed: usize, ramp: u8,
) -> Option<u8> {
    let adv = font.fixed_advance as usize;
    if adv == 0 || s.is_empty() {
        return None;
    }
    // Add a whole period before subtracting so the usize cannot underflow.
    let period = s.len() * adv;
    let vx = x + period - (ctx.frame as usize * speed) % period;
    font.cover_scroll(s, vx, y, oy).map(|c| shade(ramp, c))
}

/// An icon at a fixed position.
#[inline]
pub fn sprite(sp: &Sprite, x: usize, y: usize, ox: usize, oy: usize, ramp: u8) -> Option<u8> {
    sp.cover_at(x, y, ox, oy).map(|c| shade(ramp, c))
}

/// An icon spinning about a centre. `angle` is 0..63.
///
/// Inverse rotation: a program is asked what belongs at THIS pixel, so it maps
/// back into source space and samples. Rotating forward would need a buffer to
/// scatter into and would leave holes.
#[inline]
pub fn sprite_rotated(
    sp: &Sprite, x: usize, y: usize, cx: usize, cy: usize, angle: usize, ramp: u8,
) -> Option<u8> {
    // Reject on the bounding disc FIRST. This ran for every pixel on the panel
    // to answer "no" for about 92% of them -- a 26px star occupies 1369 pixels
    // of a 16384-pixel frame -- and answering it meant a rotation, a glyph
    // lookup and a sprite read. With no D-cache those reads are the expensive
    // part; two comparisons now stand in front of all of it.
    //
    // The bound is the half-diagonal, w*sqrt(2)/2 ~= 0.707w, rounded UP to
    // 3w/4 so nothing is ever clipped -- a tight bound that is wrong by a pixel
    // shaves a corner off the sprite as it turns, which reads as a rendering
    // fault rather than as an over-eager reject.
    let r = (sp.w.max(sp.h) as usize * 3) / 4 + 1;
    if x.abs_diff(cx) > r || y.abs_diff(cy) > r {
        return None;
    }
    let (sx, sy) = unrotate(x, y, cx as i32, cy as i32, angle);
    if sx < 0 || sy < 0 {
        return None;
    }
    let (ox, oy) = (cx - sp.w as usize / 2, cy - sp.h as usize / 2);
    sp.cover_at(sx as usize, sy as usize, ox, oy).map(|c| shade(ramp, c))
}

/// Summed-sine plasma -- the cheapest full-screen effect there is.
///
/// SHIFTS, not divides. This used `x / 3`, `y / 3`, `(x+y) / 5` and `/ 12` --
/// four divides per pixel, 16384 pixels a frame. The CPU does have the M
/// extension (see docs/BENCHMARKS.md), so those are single instructions rather
/// than software routines, but a divide is iterative where a shift is not, and
/// four of them per pixel is not a rounding error. The spatial frequency
/// changes slightly (quarters rather than thirds) and nothing else does;
/// `hue()` masks rather than divides because RAINBOW_LEN is a power of two.
#[inline]
pub fn plasma(ctx: &Ctx, x: usize, y: usize) -> u8 {
    PlasmaRow::new(ctx, y).at(x)
}

/// Plasma with everything that depends only on the row already done.
///
/// The y term is constant across a row, so it is one table lookup per ROW
/// instead of one per pixel. Build it in a `lambda`'s `row:` block and sample
/// it in `px:` -- which is what the row/pixel split is for.
pub struct PlasmaRow {
    f: usize,
    ybase: i32,
    y: usize,
}

impl PlasmaRow {
    #[inline]
    pub fn new(ctx: &Ctx, y: usize) -> Self {
        let f = ctx.frame as usize;
        Self { f, ybase: sin64((y >> 2) + (f >> 1)), y }
    }
    #[inline]
    pub fn at(&self, x: usize) -> u8 {
        let v = sin64((x >> 2) + self.f)
            + self.ybase
            + sin64(((x + self.y) >> 2) + (self.f >> 2));
        hue((((v + 768) >> 4) as usize) + self.f)
    }
}

/// Twinkling sparkles. `one_in` sets density (higher = sparser).
///
/// Each pixel gets its own phase from a hash of its position, so the sparkle is
/// stable per pixel and needs no state -- storing a phase would cost a
/// framebuffer-sized array, deriving it costs a few shifts.
#[inline]
pub fn sparkle(ctx: &Ctx, x: usize, y: usize, one_in: u32, ramp: u8) -> Option<u8> {
    let seed = hash2(x, y);
    if seed % one_in != 0 {
        return None;
    }
    let period = 30 + (seed as usize >> 8) % 40;
    let phase = (ctx.frame as usize + (seed as usize >> 3) % period) % period;
    if phase < 3 {
        Some(shade(ramp, 15 - (phase as u8) * 5))
    } else {
        None
    }
}

/// A block sweeping across the panel -- a highlight or a wipe.
#[inline]
pub fn sweep(ctx: &Ctx, x: usize, y: usize, oy: usize, h: usize, width: usize, speed: usize, color: u8) -> Option<u8> {
    if y < oy || y >= oy + h {
        return None;
    }
    let head = (ctx.frame as usize * speed) % (ctx.w + width);
    if x + width > head && x < head {
        Some(color)
    } else {
        None
    }
}

/// Show something when the feed has gone quiet. A sign that lies is worse than
/// a blank one, and unlike a streamed panel this one CAN say so -- the pixels
/// are already here when the host dies.
#[inline]
/// Concatenate three fragments into a caller-owned buffer.
///
/// `panelc` uses this for a `value` widget's `prefix`/`suffix`, so a line like
/// "HUMIDITY 71%" is ONE string and can therefore be centred as a unit. The
/// buffer belongs to the row, so this costs nothing per pixel.
pub fn join3<'a>(buf: &'a mut [u8], a: &str, b: &str, c: &str) -> &'a str {
    let mut n = 0;
    for s in [a, b, c] {
        let by = s.as_bytes();
        let k = by.len().min(buf.len() - n);
        buf[n..n + k].copy_from_slice(&by[..k]);
        n += k;
        if n == buf.len() {
            break;
        }
    }
    // A truncated multi-byte char would not be valid UTF-8; an empty string is
    // a better answer than a panic on a panel nobody can attach a debugger to.
    core::str::from_utf8(&buf[..n]).unwrap_or("")
}

pub fn stale_marker(ctx: &Ctx, x: usize, y: usize, after_ms: i64, size: usize, color: u8) -> Option<u8> {
    if ctx.stale_ms() > after_ms && x >= ctx.w - size && y < size {
        Some(color)
    } else {
        None
    }
}

// ============================================================================
// Live tickers -- text built from values, and only when a value changes
// ============================================================================

/// How long a built ticker message may be. 128 chars at a 16px advance is 2048
/// virtual pixels, comfortably wider than a wall.
pub const MSG_LEN: usize = 128;

/// How many distinct live tickers a program may have. One per call site.
pub const MSG_SLOTS: usize = 4;

/// A fixed message buffer that implements `core::fmt::Write`.
///
/// Truncates rather than failing: a ticker that silently loses its tail is a
/// cosmetic problem, while a `write!` that errored mid-render would have to be
/// handled per pixel. No allocator here and none wanted.
pub struct Msg {
    buf: [u8; MSG_LEN],
    len: usize,
}

impl Msg {
    pub const fn new() -> Self {
        Self { buf: [b' '; MSG_LEN], len: 0 }
    }
    pub fn clear(&mut self) {
        self.len = 0;
    }
    pub fn as_str(&self) -> &str {
        core::str::from_utf8(&self.buf[..self.len]).unwrap_or("")
    }
    /// Pad with spaces out to `n` characters.
    ///
    /// **Use this whenever a value in the message can change width.**
    /// `cover_scroll` wraps modulo `len * advance`, so a message that changes
    /// LENGTH changes the scroll period -- the tape jumps. Pad to a fixed width
    /// and "5" and "-12" scroll identically.
    pub fn pad_to(&mut self, n: usize) {
        let n = n.min(MSG_LEN);
        while self.len < n {
            self.buf[self.len] = b' ';
            self.len += 1;
        }
    }
}

impl core::fmt::Write for Msg {
    fn write_str(&mut self, s: &str) -> core::fmt::Result {
        for &b in s.as_bytes() {
            if self.len >= MSG_LEN {
                break;
            }
            self.buf[self.len] = b;
            self.len += 1;
        }
        Ok(())
    }
}

struct MsgSlot {
    owner: usize,
    /// `ValueTable::writes` the text was built from. `u32::MAX` = never built.
    built: u32,
    msg: Msg,
}

static mut MSG_SLOTS_TAB: [MsgSlot; MSG_SLOTS] = [
    MsgSlot { owner: 0, built: u32::MAX, msg: Msg::new() },
    MsgSlot { owner: 0, built: u32::MAX, msg: Msg::new() },
    MsgSlot { owner: 0, built: u32::MAX, msg: Msg::new() },
    MsgSlot { owner: 0, built: u32::MAX, msg: Msg::new() },
];

/// The cached text for one call site, rebuilt only when a value has landed.
///
/// Keyed on the `build` function pointer, so two tickers in one program get
/// their own buffers automatically -- a single shared buffer would hand the
/// second tape the first one's text.
///
/// Keyed on `values.writes` rather than on the frame because **that is when the
/// text can actually change**. Rebuilding per frame would not cost much against
/// a ~3.1M-cycle redraw, but it would be pure waste, and tying the rebuild to a
/// value arrival buys a real property: the message can only change LENGTH on a
/// frame where a value landed, which is exactly when `program_tick` has already
/// re-armed two full redraws. So a length change can never leave a seam in a
/// shifted band -- the band is being fully repainted on those frames anyway.
#[inline]
unsafe fn cached_msg(ctx: &Ctx, build: fn(&Ctx, &mut Msg)) -> &'static str {
    let key = build as usize;
    let tab = &mut *core::ptr::addr_of_mut!(MSG_SLOTS_TAB);
    let mut idx = MSG_SLOTS - 1;
    for i in 0..MSG_SLOTS {
        if tab[i].owner == key || tab[i].owner == 0 {
            idx = i;
            break;
        }
    }
    let slot = &mut tab[idx];
    let writes = ctx.values.writes;
    if slot.owner != key || slot.built != writes {
        slot.owner = key;
        slot.built = writes;
        slot.msg.clear();
        build(ctx, &mut slot.msg);
    }
    slot.msg.as_str()
}

/// A scrolling ticker whose text is BUILT FROM LIVE VALUES.
///
/// `scroll_text` takes a `&'static str`, which is all a fixed slogan needs. A
/// tape that says what the temperature is cannot: the string has to be
/// formatted, and formatting it per pixel would run `write!` 16384 times a
/// frame on a core where a single memory access costs ~190 cycles.
///
/// So `build` runs only when a value has actually landed -- typically once
/// every several seconds, not once per frame. Every pixel in between reads the
/// cached text, at a cost over `scroll_text` of one `u32` comparison.
///
/// ```ignore
/// scroll_message(ctx, &FONT_TICK, |ctx, m| {
///     let _ = write!(m, "*** OUTSIDE {}F *** ", ctx.val("temp"));
///     m.pad_to(26);              // constant period -- see Msg::pad_to
/// }, x, y, 8, 1, RAMP_WHITE)
/// ```
#[inline]
pub fn scroll_message(
    ctx: &Ctx, font: &Font, build: fn(&Ctx, &mut Msg),
    x: usize, y: usize, oy: usize, speed: usize, ramp: u8,
) -> Option<u8> {
    // Band-reject before the cache lookup: a pixel nowhere near this line costs
    // one comparison, the same as every other primitive here.
    if y < oy || y >= oy + font.line_height as usize {
        return None;
    }
    // SAFETY: programs render from the main loop, single-threaded.
    let s = unsafe { cached_msg(ctx, build) };
    font.cover_scroll(s, x + ctx.frame as usize * speed, y, oy).map(|c| shade(ramp, c))
}

/// The same, travelling the other way.
#[inline]
pub fn scroll_message_rev(
    ctx: &Ctx, font: &Font, build: fn(&Ctx, &mut Msg),
    x: usize, y: usize, oy: usize, speed: usize, ramp: u8,
) -> Option<u8> {
    if y < oy || y >= oy + font.line_height as usize {
        return None;
    }
    let s = unsafe { cached_msg(ctx, build) };
    let adv = font.fixed_advance as usize;
    if adv == 0 || s.is_empty() {
        return None;
    }
    // Add a whole period before subtracting so the usize cannot underflow.
    let period = s.len() * adv;
    let vx = x + period - (ctx.frame as usize * speed) % period;
    font.cover_scroll(s, vx, y, oy).map(|c| shade(ramp, c))
}
