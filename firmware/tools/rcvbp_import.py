#!/usr/bin/env python3
"""Turn a Colorlight `.rcvbp` receiver profile into a marquee module type.

    tools/rcvbp_import.py factory/myled-COB-p1.25-640x320-1065L.rcvbp \\
        --key myled-p1.25-factory --preset 128x256-icnd1065l-hub320 \\
        --clk-div 4 --packet-delay 0.0006 -o myled-p1.25-factory.json

The file it writes goes in marquee's `custom/<site>/modules/` (or
`/data/modules/`); marquee then offers it as a module type and serves its
registers to the card as `chip_reg:` lines (firmware 5.6.0+).

WHAT COMES FROM THE PROFILE, and what does not. The format is decoded by
`decode_rcvbp.py`, which this imports; nothing here is guessed.

    from the profile        the driver-chip register table, per colour line
                            (record 0x84); the chip family (driverChipId);
                            one data group's columns and scan lines (the
                            extents of the pixel map, record 0x03); whether
                            the module mounts a quarter turn (written as
                            `rotate`, which marquee and the card act on);
                            gamma, refresh,
                            card area and data-group map (kept under "profile"
                            for reference -- marquee does not act on them)
    from YOU, as flags      where the groups sit in the module (--origins;
                            default stacked top to bottom), the bitstream
                            preset that drives this shape (--preset), and the
                            measured operating point (--clk-div etc.)

A profile cannot supply the second list: it describes a cabinet to Colorlight's
own firmware, whose output numbering is not ours, and the operating point is a
measurement on our gateware. And a NEW shape still needs a bitstream built for
it -- this writes the config for a module, it does not make the card drive one.
"""
import argparse
import json
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from decode_rcvbp import inflate, walk_records  # noqa: E402

# driverChipId (record 0x01, +233) -> the chip table name in spwm_chips.py.
# Only ids read off a profile for a module that then lit are listed.
CHIP_IDS = {88: "dp3264s", 146: "icnd1065"}


def read_profile(path):
    _, d, _, _ = inflate(path)
    rec = {tag: body for _, _, _, tag, body in walk_records(d)}
    for need in (0x01, 0x03, 0x84):
        if need not in rec:
            sys.exit(f"{path}: no record 0x{need:02x}; not a profile this reads")
    p = rec[0x01]

    t = rec[0x84]
    rows = [list(t[i:i + 4]) for i in range(0, len(t) - 3, 4)]
    while rows and not any(rows[-1]):
        rows.pop()
    if len({r[0] for r in rows}) != len(rows):
        sys.exit(f"{path}: the register table repeats an address")

    m = rec[0x03]
    n = struct.unpack_from("<H", m, 0)[0]
    pts = [(m[2 + 3 * i], m[3 + 3 * i] | m[4 + 3 * i] << 8) for i in range(n)]
    scan = max(y for y, _ in pts) + 1
    cols = max(x for _, x in pts) + 1
    if len(set(pts)) != n or scan * cols != n:
        sys.exit(f"{path}: the pixel map is not a full {cols}x{scan} rectangle")
    lw, lh = p[0], p[1]
    identity = all(pts[i] == (i // lw, i % lw) for i in range(n))
    quarter = all(pts[i] == (i % lw, cols - 1 - i // lw) for i in range(n))

    return {
        "regs": rows, "chip_id": p[233], "cols": cols, "scan": scan,
        "logical": [lw, lh],
        "mount": ("as scanned" if identity else
                  "quarter turn: shift direction runs bottom to top" if quarter
                  else "remapped (see decode_rcvbp.py --pixel-map)"),
        # Degrees clockwise the module hangs from its framebuffer, in the
        # terms marquee's `rotate` and the firmware's `rotate:` use. Only the
        # two cases read off real profiles are named; anything else is left
        # for a person to work out.
        # The quarter-turn map reads as 270, but with the groups in the
        # order our bitstream scans them the module hangs upright at 90
        # (Myled P1.25 on glass, 2026-11-18).
        "rotate": 0 if identity else 90 if quarter else None,
        "gamma": round(struct.unpack_from("<f", p, 28)[0], 3),
        "refresh_hz": struct.unpack_from("<H", p, 228)[0],
        "card_area": list(struct.unpack_from("<HH", p, 192)),
        "group_map": [g for g in p[90:106] if g != 0xFF],
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--key", required=True, help="the module type's name in marquee")
    ap.add_argument("--preset", required=True,
                    help="bitstream preset that drives this shape (build.sh --panel)")
    ap.add_argument("--label")
    ap.add_argument("--groups", type=int, default=4, help="RGB groups per module (4)")
    ap.add_argument("--origins", help="'x,y x,y ...' one per group; default stacked")
    ap.add_argument("--module", metavar="WxH", help="module size; default from the groups")
    ap.add_argument("--drive", help="chip table name, if driverChipId is not known here")
    ap.add_argument("--board", default="e320")
    ap.add_argument("--connector", default="hub320")
    ap.add_argument("--clk-div", type=int)
    ap.add_argument("--write-throttle", type=int)
    ap.add_argument("--packet-delay", type=float)
    ap.add_argument("-o", "--out", help="write here (default: stdout)")
    a = ap.parse_args()

    pr = read_profile(a.path)
    drive = a.drive or CHIP_IDS.get(pr["chip_id"])
    if not drive:
        sys.exit(f"driverChipId {pr['chip_id']} is not one this knows; pass --drive")

    if a.origins:
        origins = [tuple(int(v) for v in o.split(",")) for o in a.origins.split()]
        if len(origins) != a.groups:
            sys.exit(f"--origins has {len(origins)} entries for {a.groups} groups")
    else:
        origins = [(0, i * pr["scan"]) for i in range(a.groups)]
    if a.module:
        mw, mh = (int(v) for v in a.module.lower().split("x"))
    else:
        mw = max(x for x, _ in origins) + pr["cols"]
        mh = max(y for _, y in origins) + pr["scan"]
    if not a.preset.startswith(f"{mw}x{mh}"):
        sys.exit(f"preset {a.preset!r} is not for a {mw}x{mh} module")

    src = os.path.basename(a.path)
    pt = {
        "label": a.label or f"{a.key} ({mw}x{mh}, {drive}, imported)",
        "board": a.board, "connector": a.connector,
        "module_w": mw, "module_h": mh,
        "drive": drive,
        "group_origins": " ".join(f"{x},{y}" for x, y in origins),
        "bitstream_preset": a.preset,
        "notes": (f"Imported from {src}: {a.groups} groups of {pr['cols']}x{pr['scan']}, "
                  f"{len(pr['regs'])} chip registers, mounts {pr['mount']}."),
        "chip_regs": pr["regs"],
        **({"rotate": pr["rotate"]} if pr["rotate"] else {}),
        "profile": {"file": src, "gamma": pr["gamma"], "refresh_hz": pr["refresh_hz"],
                    "group_logical": pr["logical"], "mount": pr["mount"],
                    "card_area": pr["card_area"], "group_map": pr["group_map"],
                    "driver_chip_id": pr["chip_id"]},
    }
    for k, v in (("clk_div", a.clk_div), ("write_throttle", a.write_throttle),
                 ("packet_delay", a.packet_delay)):
        if v is not None:
            pt[k] = v

    text = json.dumps(pt, indent=2) + "\n"
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"wrote {a.out}: {pt['label']}", file=sys.stderr)
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
