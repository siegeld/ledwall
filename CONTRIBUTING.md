# Contributing

Contributions are welcome. One thing is unusual here and you should know it
**before** you spend an evening on a patch, not after.

## This repo is generated

It is exported from two private repositories by a publish script, which strips
site-specific content — hostnames, addresses, one estate's own panel programs —
and refuses to publish if any of it survives. The private repos are the source
of truth.

That has a consequence for you: **a pull request is not merged with the merge
button.** It is exported from your PR with `git format-patch`, applied into the
private tree with `git am`, and appears here on the next publish. The PR is then
closed with a reference to the publish commit that contains your work.

**Your authorship survives this.** `git am` preserves the author field, so the
commit in this repo carries your name and email, not the maintainer's.

We are telling you up front because people accept this readily when they know in
advance and — reasonably — resent discovering it after a PR is closed.

If the project attracts sustained contribution, the intended end state is for
this repo to become the source of truth with only site content kept private.
The publish script is the path there: once it runs clean, the public tree *is*
what a public-as-source tree would look like.

## Before you open a PR

- **Say what hardware you tested on.** This drives one family of receiver card
  into HUB75 panels, and "works on mine" is the only evidence that exists for
  most of it. A panel type we have never seen is genuinely useful information.
- **Keep the wire protocol in step.** The 10-byte header and the 487-pixel
  chunk size appear in *both* `server/packages/marquee_core/stream.py` and
  `firmware/sw_rust/barsign_disp/src/bitmap_udp.rs`. Changing one without the
  other silently corrupts frames rather than failing.
- **Respect the pixel budget.** The panel CPU is a 40 MHz VexRiscv with no
  D-cache, and anything per-pixel happens 16384 times a frame. If you add work
  to a pixel loop, measure it — `/api/profile` reports per-row cost and
  `/api/bench` reports a whole frame.
- **Measure, do not estimate.** Most of the performance comments in this
  codebase exist because somebody's confident guess was wrong. Several of them
  say so.

## Style

Match the surrounding code. The one convention worth stating: comments here
explain **why**, and especially why the obvious alternative was rejected. A
comment that restates the code is noise; a comment recording the measurement
that ruled something out is the most valuable thing in the file.

## Reporting a bug

Include the panel type, the card, and what the counters say —
`GET /api/status` on the panel. If it is a *visual* fault, say so explicitly and
say what you see: several bugs in this project's history looked identical in
every counter while being obviously wrong on the glass, and that distinction is
usually the fastest route to the cause.
