#!/usr/bin/env python3
"""Which package balls does a bitstream actually drive?

Decodes an ECP5 bitstream (or an already-unpacked textcfg) into BALL names, by
joining prjtrellis' `iodb.json` ball-to-tile map against the PIO entries in the
config. Run inside the toolchain image, which carries the database.

Why this exists
---------------
Recovering what a vendor bitstream drives is the only evidence-based way to
answer "which pin is the PHY reset / the enable / that port's clock" on a board
whose schematic we do not have. `docs/HARDWARE.md` §1b established the method;
this script is that method, so it is not re-derived by hand each time.

Two traps it encodes, both of which have cost this repo real time:

* **`BASE_TYPE` is not evidence.** Diamond assigns one to *unused* pins too, so
  roughly half of every bank looks configured. Use `--iologic` for the reliable
  signal: a genuinely registered signal has IOLOGIC, an untouched pad does not.
* **IOLOGIC only reaches REGISTERED io.** A static output -- a reset held high,
  an enable -- leaves no IOLOGIC behind. For those, `--diff-lpf` is the useful
  mode: it lists balls the bitstream drives that a given LPF never constrains,
  which is where a pin we are failing to drive will show up.

Usage
-----
    bitstream_pins.py FILE.bin                    # balls with IOLOGIC
    bitstream_pins.py FILE.cfg --base-type        # ... with any BASE_TYPE too
    bitstream_pins.py FILE.bin --diff-lpf a.lpf   # driven here, absent there
"""
import argparse, json, os, re, subprocess, sys, tempfile

DB = "/opt/oss-cad-suite/share/trellis/database/ECP5/{part}/iodb.json"


def ball_map(part, package):
    path = DB.format(part=part)
    if not os.path.exists(path):
        sys.exit(f"no iodb for {part} at {path}; run inside the toolchain image")
    pk = json.load(open(path))["packages"]
    if package not in pk:
        sys.exit(f"package {package} not in {sorted(pk)}")
    return {(v["row"], v["col"], v["pio"]): b for b, v in pk[package].items()}


def unpack(path):
    """Accept either a .bin bitstream or an already-unpacked textcfg."""
    with open(path, "rb") as fh:
        head = fh.read(16)
    if head.startswith(b".device") or head.startswith(b"\n.device"):
        return path
    out = tempfile.NamedTemporaryFile(suffix=".cfg", delete=False).name
    subprocess.run(["ecpunpack", "--input", path, "--textcfg", out],
                   check=True, capture_output=True)
    return out


def driven(cfg, rc2ball, want_base_type):
    pat = (r"enum: (PIO[A-D])\.(?:BASE_TYPE|IOLOGIC)" if want_base_type
           else r"enum: IOLOGIC([A-D])\.")
    tile, found = None, {}
    for line in open(cfg):
        if line.startswith(".tile "):
            tile = line.split()[1]
            continue
        m = re.match(pat, line.strip())
        if not (m and tile):
            continue
        t = re.match(r"[A-Z]+_R(\d+)C(\d+)", tile)
        if not t:
            continue
        pio = m.group(1)[-1]
        ball = rc2ball.get((int(t.group(1)), int(t.group(2)), pio))
        if ball:
            found.setdefault(ball, line.strip())
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bitstream")
    ap.add_argument("--part", default="LFE5U-25F")
    ap.add_argument("--package", default="CABGA256")
    ap.add_argument("--base-type", action="store_true",
                    help="also count BASE_TYPE (UNRELIABLE -- see the docstring)")
    ap.add_argument("--diff-lpf", metavar="LPF",
                    help="print only balls this bitstream drives that LPF does not constrain")
    a = ap.parse_args()

    rc2ball = ball_map(a.part, a.package)
    found = driven(unpack(a.bitstream), rc2ball, a.base_type)

    if a.diff_lpf:
        lpf = set(re.findall(r'SITE "([A-Z]+[0-9]+)"', open(a.diff_lpf).read()))
        extra = sorted(set(found) - lpf)
        print(f"{len(found)} balls driven, {len(lpf)} constrained in LPF")
        print(f"driven but NOT in the LPF ({len(extra)}):")
        print("  " + (" ".join(extra) if extra else "(none)"))
        return
    print(f"{len(found)} balls ({'BASE_TYPE+IOLOGIC' if a.base_type else 'IOLOGIC'}):")
    print("  " + " ".join(sorted(found)))


if __name__ == "__main__":
    main()
