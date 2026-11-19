//! Modules mounted turned: where a pixel of the canvas a viewer sees lives in
//! the framebuffer. Pure arithmetic, no hardware -- `tools/test_turn_rs.rs`
//! compiles this file on the host (`./build.sh test-turn-rs`).

/// Where logical pixel `(lx, ly)` of a turned card lives in its framebuffer.
///
/// `turn` is quarter turns clockwise that the modules are mounted from the
/// framebuffer; the content goes the other way. `fw` x `fh` is the
/// FRAMEBUFFER. The logical canvas is `fw` x `fh` at 0 and 2 turns and
/// `fh` x `fw` at 1 and 3. This is the same map marquee's sender applies to
/// streamed frames (`to_framebuffer`), so both kinds of content agree:
///
///   1 (90):   fb (ly,            fh - 1 - lx)
///   2 (180):  fb (fw - 1 - lx,   fh - 1 - ly)
///   3 (270):  fb (fw - 1 - ly,   lx)
///
/// The caller keeps `(lx, ly)` inside the logical canvas; the result is then
/// inside the framebuffer.
#[inline]
pub fn turned_index(turn: u8, lx: usize, ly: usize, fw: usize, fh: usize) -> usize {
    let (x, y) = match turn & 3 {
        1 => (ly, fh - 1 - lx),
        2 => (fw - 1 - lx, fh - 1 - ly),
        3 => (fw - 1 - ly, lx),
        _ => (lx, ly),
    };
    y * fw + x
}

/// The logical canvas of a `fw` x `fh` framebuffer at `turn`.
#[inline]
pub fn turned_size(turn: u8, fw: usize, fh: usize) -> (usize, usize) {
    if turn & 1 == 1 { (fh, fw) } else { (fw, fh) }
}
