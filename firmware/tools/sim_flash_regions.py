#!/usr/bin/env python3
"""The flash region map, checked against itself.

`flash_write.rs::region_is_writable()` is the guard that decides whether an OTA
is allowed to touch a given stretch of flash. It is the one piece of logic in
this firmware that can permanently brick a card -- erase the golden bitstream
and the scan chain has nothing to fall back on -- and it had **no test of any
kind** until 2026-10-23.

It cannot be unit-tested in place: it is `no_std`, it reads flash through `pac`,
and `golden_present()` does real SPI. So this checks the part where a mistake
would actually be made -- the CONSTANTS and the relationships between them --
by parsing them out of the Rust source, the same technique marquee's
tests/test_wire.py uses to compare wire constants across repos.

WHAT THIS COVERS: the regions do not overlap; the golden image is inside the
protected window; nothing runs past the end of the part; every base and length
is sector-aligned, which the guard requires of its callers and would otherwise
reject at runtime; and the one caller that passes a whole region
(tftp_flash.rs, APP_BASE/APP_LEN) is accepted by a faithful re-implementation
of the guard.

WHAT THIS DOES NOT COVER: the SPI sequencing, `golden_present()`, or anything
about the hardware. A green run here means the map is self-consistent, not that
a flash write works.
"""
from __future__ import annotations

import pathlib
import re
import sys

SRC = pathlib.Path(__file__).resolve().parents[1] / "sw_rust/barsign_disp/src/flash_write.rs"


def constants(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for m in re.finditer(r"pub const (\w+)\s*:\s*u32\s*=\s*([^;]+);", text):
        name, expr = m.group(1), m.group(2).split("//")[0].strip()
        if re.fullmatch(r"[0-9a-fA-FxX_ *+]+", expr):
            try:
                out[name] = eval(expr, {"__builtins__": {}}, {})  # noqa: S307 - digits only
            except Exception:
                pass
    return out


def guard(c: dict[str, int], addr: int, length: int, golden: bool) -> str:
    """A faithful re-implementation of region_is_writable, for the cases below.

    Kept deliberately literal rather than tidy: it is here to be compared
    against the Rust by eye when either changes.
    """
    end = min(addr + length, 0xFFFFFFFF)
    if end > c["FLASH_SIZE"] or length == 0:
        return "Forbidden"
    if addr % c["SECTOR"] or length % c["SECTOR"]:
        return "Unaligned"
    if addr < c["GOLDEN_BASE"] + c["GOLDEN_LEN"] and end > c["GOLDEN_BASE"]:
        return "Forbidden"
    if addr < c["MARGINAL_BASE"] + c["MARGINAL_LEN"] and end > c["MARGINAL_BASE"]:
        return "Forbidden"
    lo, hi = c["APP_BASE"], c["APP_BASE"] + c["APP_LEN"]
    if lo <= addr <= hi and lo <= end <= hi:
        return "Ok" if golden else "NoGolden"
    if addr < c["SCRATCH_BASE"] or end > c["SCRATCH_BASE"] + c["SCRATCH_LEN"]:
        return "Forbidden"
    return "Ok"


def main() -> int:
    c = constants(SRC.read_text())
    need = ["FLASH_SIZE", "SECTOR", "APP_BASE", "APP_LEN", "GOLDEN_BASE",
            "GOLDEN_LEN", "SCRATCH_BASE", "SCRATCH_LEN", "MARGINAL_BASE",
            "MARGINAL_LEN"]
    missing = [n for n in need if n not in c]
    if missing:
        print(f"FAIL: not found in flash_write.rs: {', '.join(missing)} "
              f"-- renamed, or no longer a plain u32 constant?")
        return 1

    checks: list[tuple[str, bool, str]] = []

    regions = {
        "app":      (c["APP_BASE"], c["APP_LEN"]),
        "golden":   (c["GOLDEN_BASE"], c["GOLDEN_LEN"]),
        "marginal": (c["MARGINAL_BASE"], c["MARGINAL_LEN"]),
        "scratch":  (c["SCRATCH_BASE"], c["SCRATCH_LEN"]),
    }

    for name, (base, length) in regions.items():
        checks.append((f"{name} is sector-aligned",
                       base % c["SECTOR"] == 0 and length % c["SECTOR"] == 0,
                       f"{name} {base:#x}+{length:#x} against sector {c['SECTOR']:#x}"))
        checks.append((f"{name} fits the part",
                       base + length <= c["FLASH_SIZE"],
                       f"{name} ends {base + length:#x}, part is {c['FLASH_SIZE']:#x}"))

    names = list(regions)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            ab, al = regions[a]
            bb, bl = regions[b]
            checks.append((f"{a} does not overlap {b}",
                           ab + al <= bb or bb + bl <= ab,
                           f"{a} {ab:#x}+{al:#x} vs {b} {bb:#x}+{bl:#x}"))

    # The guard's own behaviour on the cases that matter.
    cases = [
        ("whole app region, golden present -> Ok",
         (c["APP_BASE"], c["APP_LEN"], True), "Ok"),
        ("whole app region, NO golden -> refused",
         (c["APP_BASE"], c["APP_LEN"], False), "NoGolden"),
        ("the golden image itself -> never",
         (c["GOLDEN_BASE"], c["SECTOR"], True), "Forbidden"),
        ("last sector of golden -> never",
         (c["GOLDEN_BASE"] + c["GOLDEN_LEN"] - c["SECTOR"], c["SECTOR"], True), "Forbidden"),
        ("marginal region -> never",
         (c["MARGINAL_BASE"], c["SECTOR"], True), "Forbidden"),
        ("one sector past the app region is NOT app",
         (c["APP_BASE"] + c["APP_LEN"], c["SECTOR"], True), "Forbidden"),
        ("scratch is writable",
         (c["SCRATCH_BASE"], c["SECTOR"], True), "Ok"),
        ("running off the end of the part -> refused",
         (c["FLASH_SIZE"] - c["SECTOR"], 2 * c["SECTOR"], True), "Forbidden"),
        ("zero length -> refused",
         (c["SCRATCH_BASE"], 0, True), "Forbidden"),
        ("unaligned address -> refused",
         (c["SCRATCH_BASE"] + 1, c["SECTOR"], True), "Unaligned"),
        ("saturating add does not wrap into a pass",
         (0xFFFFF000, c["SECTOR"], True), "Forbidden"),
    ]
    for label, args, want in cases:
        got = guard(c, *args)
        checks.append((label, got == want, f"got {got}, want {want}"))

    bad = [(l, d) for l, ok, d in checks if not ok]
    for label, ok, detail in checks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {label}" + ("" if ok else f"   [{detail}]"))
    print()
    if bad:
        print(f"{len(bad)} of {len(checks)} checks FAILED")
        return 1
    print(f"all {len(checks)} checks pass")
    print(f"  app      {c['APP_BASE']:#09x} + {c['APP_LEN']:#09x}")
    print(f"  golden   {c['GOLDEN_BASE']:#09x} + {c['GOLDEN_LEN']:#09x}")
    print(f"  marginal {c['MARGINAL_BASE']:#09x} + {c['MARGINAL_LEN']:#09x}")
    print(f"  scratch  {c['SCRATCH_BASE']:#09x} + {c['SCRATCH_LEN']:#09x}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
