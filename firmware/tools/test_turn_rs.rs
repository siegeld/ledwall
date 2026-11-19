//! Host-side tests for the firmware's turned-module map (src/turn.rs).
//! `./build.sh test-turn-rs`.
//!
//! What is pinned here is the CONVENTION, because two programs must agree on
//! it: marquee's sender turns streamed frames with PIL transposes
//! (`models.to_framebuffer`), and this firmware turns what it draws itself.
#![allow(dead_code)]
#[path = "../sw_rust/barsign_disp/src/turn.rs"]
mod turn;
use turn::*;

fn main() {
    let mut fails = 0;
    let mut check = |ok: bool, what: &str| {
        println!("  {}  {}", if ok { "ok  " } else { "FAIL" }, what);
        if !ok { fails += 1; }
    };
    let (fw, fh) = (128usize, 256usize);

    // Every turn is a bijection from the logical canvas onto the framebuffer.
    for t in 0..4u8 {
        let (lw, lh) = turned_size(t, fw, fh);
        let mut seen = vec![false; fw * fh];
        let mut ok = lw * lh == fw * fh;
        for ly in 0..lh {
            for lx in 0..lw {
                let i = turned_index(t, lx, ly, fw, fh);
                if i >= seen.len() || seen[i] { ok = false; } else { seen[i] = true; }
            }
        }
        check(ok && seen.iter().all(|&b| b), &format!("turn {t}: bijection, canvas {lw}x{lh}"));
    }
    check(turned_size(1, fw, fh) == (256, 128) && turned_size(2, fw, fh) == (128, 256),
          "quarter turns swap the canvas, a half turn does not");

    // 270: Myled's vendor profile. Framebuffer (x = chain column, y = scan
    // line) holds logical (row 127 - x, column y) -- PIL's ROTATE_270.
    let mut ok = true;
    for y in 0..fh { for x in 0..fw {
        ok &= turned_index(3, y, fw - 1 - x, fw, fh) == y * fw + x;
    } }
    check(ok, "turn 3 (270): fb(x, y) = canvas(row fw-1-x, column y)");

    // 90: PIL's ROTATE_90, out(x, y) = in(column lw-1-y, row x).
    let (lw, _) = turned_size(1, fw, fh);
    let mut ok = true;
    for y in 0..fh { for x in 0..fw {
        ok &= turned_index(1, lw - 1 - y, x, fw, fh) == y * fw + x;
    } }
    check(ok, "turn 1 (90): fb(x, y) = canvas(row x, column lw-1-y)");

    // 180, and no turn.
    check(turned_index(2, 0, 0, fw, fh) == fw * fh - 1, "turn 2: the canvas's first pixel is the framebuffer's last");
    check(turned_index(0, 5, 7, fw, fh) == 7 * fw + 5, "turn 0 is row-major");

    println!("{}", if fails == 0 { "test-turn-rs: PASS" } else { "test-turn-rs: FAIL" });
    std::process::exit(if fails == 0 { 0 } else { 1 });
}
