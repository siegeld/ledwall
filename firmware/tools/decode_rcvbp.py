#!/usr/bin/env python3
"""Decode a Colorlight/NovaStar `.rcvbp` receiver configuration.

Written 2026-10-18, when the reseller (ARGO) sent the receiver
configuration for the P1.25 module on the bench
(`factory/CLTp1.25-320-160-3264s-7559-64s-6.rcfgx.rcvbp`). WHO IS WHO, because
the attribution has been muddled before: the file is a stock Colorlight
LEDVISION/LEDSetting receiver profile, consumed by Colorlight's stock receiver
firmware -- nobody in this chain writes firmware. ARGO resells; the module
itself appears to be a Linsn 320x160 P1.25 (David matched the PCB back to
linsn-led.com on 2026-10-18 and wrote to support@linsnled.com; no reply yet).
Everything this prints was read out of that file; nothing is inferred from a
datasheet. Run it against a new `.rcvbp` and it will tell you what the stock
firmware is told about the module, which is the only authoritative answer to
"what are the driver registers for this module".

THE FORMAT, as measured:

    offset  0..31   header, 32 bytes
            16      u32  version-ish (4 on this file)
            20      u32  COMPRESSED payload length
            24      u32  UNCOMPRESSED length
    offset 32..      zlib stream (`78 9c`), inflating to the length at +24

So the whole thing is one deflate stream behind a tiny header that states both
sizes -- which is how it was identified: `78 9c` sat at offset 32 and the two
u32s at +20/+24 matched the file size and the inflated size exactly.

INSIDE the inflated blob: a chain of length-prefixed records (TLV), found
2026-10-19 by noticing that every "sparse" region began with the same 4-byte
shape and that walking it closed EXACTLY at the end of the blob:

    header   u16 length (INCLUDING these 4 bytes)   u8 type   u8 tag
    body     length - 4 bytes

Twenty records on this file. The ones that carry anything:

    tag 0x01  764 B  the receiver parameter block (geometry, data-group map,
                     chip control, blanking phase, refresh, chip ids ...)
    tag 0x8a  256 B  chip FUNCTION settings (gain curve knots, floats)
    tag 0x84  256 B  the driver REGISTER table, 13 x (addr, r, g, b)
    tag 0x03  24578  the per-pixel coordinate map: u16 count (8192) then
                     3-byte (y, x_lo, x_hi) records. PURE IDENTITY here.
    tag 0xca  256 B  u16 width 128, u16 height 64 -- ONE RGB group's area
    tag 0xe4   64 B  metadata; a timestamp (2026-10-17 11:04) at +10
    tag 0x8f  580 B  float tables: 8 knots 0.125..1.0 and a gamma-ish curve,
                     repeated per colour
    0x83/0x89 10 B   (255,255,255) and (128,128,128): colour triples
    0xe0 0xe2 49 KB  per-pixel correction tables, EMPTY on this file
    0x8d 0x91 0x95 0xd8 0xda 0x8e 0xcd 0x86 0x07   empty on this file

Field offsets below are BODY-relative (after the 4-byte header). Those marked
"matched" were pinned by matching the value against Colorlight's own named
decode of the sister profile CD-P1.2-...-9.51 (LEDSettingServer, kept in
factory/CD-P1.2-DP3264-profiles-decoded-by-LEDSETTING.txt); the rest are
unnamed until the vendor parser's layout is in hand.

The register table is the payload that matters. Seven of the thirteen disagreed
with the table this repo was using, because ours came from a DIFFERENT profile
(`CD-P1.2-256X128-DP3264+32020-64S--9.51.rcvbp`, same pitch class, not this
module). See docs/E320-BRINGUP.md.

Offsets other than the header are specific to this file's layout and are
located by signature where possible, so a differently-sized config still
decodes rather than silently printing the wrong bytes.
"""

import argparse
import struct
import sys
import zlib


HDR_LEN = 32
# Register addresses the DP3264S/DP3364S family uses, in table order.
REG_NAMES = {
    0x02: "LINE_SET (scan lines - 1)",
    0x03: "display passes per frame - 1 (10.7.1)",
    0x04: "PWM_WIDTH / GCLK per row",
    0x05: "eliminationTime",
    0x06: "PLL_DIV",
    0x07: "gamma fine/coarse",
    0x08: "IGAIN",
    0x09: "DECOUP_1",
    0x0A: "(unnamed)",
    0x0B: "eliminationLevel",
    0x0C: "SYNC_MODE ([7:6]: 0=frame sync, 2=hi-grey sync, 3=async)",
    0x0D: "coupledOptimizationLevel",
    0x11: "(unnamed)",
}


def inflate(path):
    """Return (header, inflated_bytes)."""
    raw = open(path, "rb").read()
    if len(raw) < HDR_LEN + 2:
        sys.exit(f"{path}: too short to be a .rcvbp")
    hdr = raw[:HDR_LEN]
    comp_len, raw_len = struct.unpack_from("<II", hdr, 20)
    body = raw[HDR_LEN:]
    if body[:2] != b"\x78\x9c":
        sys.exit(f"{path}: no zlib stream at offset {HDR_LEN} "
                 f"(found {body[:2].hex()}); format may have changed")
    out = zlib.decompressobj().decompress(body)
    if raw_len and len(out) != raw_len:
        print(f"  ! header says {raw_len} uncompressed, got {len(out)}",
              file=sys.stderr)
    return hdr, out, comp_len, raw_len


def find_reg_table(d):
    """Locate the (addr, r, g, b) register table by signature.

    The table is thirteen 4-byte records whose first bytes run
    0x02..0x0D then 0x11. Searching for that address sequence finds it
    without trusting an absolute offset.
    """
    want = list(range(0x02, 0x0E)) + [0x11]
    for off in range(len(d) - 4 * len(want)):
        if all(d[off + 4 * i] == a for i, a in enumerate(want)):
            return off
    return None


def decode_pixel_map(d, off, n):
    """(x, y) pairs, 3 bytes each: y as u8 FIRST, then x as u16 LE.

    The byte order matters: read as (x_lo, x_hi, y) from one byte later the
    table decodes to a near-identity with a bogus one-per-row "carry" at
    column 127 (see the module docstring). Verified by the 8192/8192
    identity count this order produces.
    """
    out = []
    for i in range(n):
        p = off + 3 * i
        if p + 3 > len(d):
            break
        out.append((d[p + 1] | (d[p + 2] << 8), d[p]))
    return out


def walk_records(d):
    """The record chain: [(offset, length, type, tag, body)], closing at len(d)."""
    out, off = [], 0
    while off + 4 <= len(d):
        ln, typ, tag = struct.unpack_from("<HBB", d, off)
        if ln < 4 or off + ln > len(d):
            print(f"  ! malformed record header at {off}: {d[off:off+4].hex(' ')}",
                  file=sys.stderr)
            break
        out.append((off, ln, typ, tag, d[off + 4:off + ln]))
        off += ln
    if off != len(d):
        print(f"  ! record chain ends at {off}, blob is {len(d)}", file=sys.stderr)
    return out


# tag 0x01, body-relative. "matched" = value pinned against the LEDSettingServer
# decode of the sister profile; everything else here is a plain observation.
TAG01_FIELDS = [
    (0,   "u8",  "columns per data group (128)                   matched: moduleInfo/scan geometry"),
    (1,   "u8",  "scan lines (64)                                matched: scanNumber"),
    (28,  "f32", "gamma coefficient                              matched: gammaCoefData"),
    (33,  "u8",  "lightGrade?                                    (8 in sister)"),
    (35,  "u8",  "blanking startTime                             matched (13)"),
    (38,  "u8",  "blanking endTime                               matched (113)"),
    (43,  "u8",  "decodeChipId                                   matched (3 = DM5953/5958)"),
    (48,  "u8",  "rowSwitching? / blanking?                      unconfirmed"),
    (49,  "u8",  "blanking? / rowSwitching?                      unconfirmed"),
    (83,  "f32", "frameRate                                      matched (60)"),
    (87,  "u8",  "moduleInfo.width (16 = one driver chip)        matched"),
    (88,  "u8",  "16                                             unconfirmed"),
    (90,  "u8x16", "DATA-GROUP MAP, 16 slots, 0xff unused         semantics NOT yet known"),
    (122, "u8x48", "table 16..63                                 unknown"),
    (180, "f32x3", "4051 phase percents                          matched (0.1 x3)"),
    (197, "u8",  "prefixCommand                                  matched (14)"),
    (198, "u8",  "registerNumber                                 matched (1)"),
    (199, "u8",  "writeRegister1                                 matched (5)"),
    (200, "u8",  "writeRegister2                                 matched (5)"),
    (201, "u8",  "dataLatching                                   matched (1)"),
    (202, "u8",  "globalLatching                                 matched (3)"),
    (203, "u8x3", "sigmaF / sigma / DH compensation              matched (127 x3)"),
    (207, "u8",  "GCLKNumberPerScan1                             matched by position (48)"),
    (209, "u8",  "GCLKNumberPerScan2                             matched by position (48)"),
    (228, "u16", "refresh rate Hz                                matched (3840)"),
    (233, "u8",  "driverChipId                                   matched (88 = DP3264)"),
    (243, "u8",  "brightPercent?                                 unconfirmed (100)"),
    (276, "u8x96", "table 0..95                                  unknown"),
    (410, "u8x64", "table 64..127                                unknown"),
]


def rd(body, off, kind):
    if kind == "u8":
        return body[off]
    if kind == "u16":
        return struct.unpack_from("<H", body, off)[0]
    if kind == "f32":
        return round(struct.unpack_from("<f", body, off)[0], 4)
    if kind.startswith("u8x"):
        n = int(kind[3:]); return list(body[off:off + n])
    if kind.startswith("f32x"):
        n = int(kind[4:]); return [round(struct.unpack_from("<f", body, off + 4 * i)[0], 4) for i in range(n)]
    raise ValueError(kind)


def decode_records(d):
    recs = walk_records(d)
    print(f"\nrecord chain: {len(recs)} records")
    for off, ln, typ, tag, body in recs:
        nz = sum(1 for b in body if b)
        print(f"  @{off:6d}  tag 0x{tag:02x} type 0x{typ:02x}  body {ln-4:6d} B  nonzero {nz:5d}")
    bytag = {tag: body for _, _, _, tag, body in recs}
    if 0x01 in bytag:
        body = bytag[0x01]
        print("\ntag 0x01 -- receiver parameters (body-relative offsets):")
        for off, kind, name in TAG01_FIELDS:
            v = rd(body, off, kind)
            if isinstance(v, list) and len(v) > 16:
                v = f"{v[:8]} .. {v[-4:]} ({len(v)} entries)"
            print(f"  +{off:3d} {kind:6s} {str(v):40s} {name}")
    if 0xca in bytag:
        b = bytag[0xca]
        print(f"\ntag 0xca: width {struct.unpack_from('<H', b, 0)[0]}  height "
              f"{struct.unpack_from('<H', b, 2)[0]}   (one RGB group's area)")
    if 0xe4 in bytag:
        b = bytag[0xe4]
        y, mo, da, h, mi = struct.unpack_from("<HBBBB", b, 10)
        print(f"\ntag 0xe4: saved {y:04d}-{mo:02d}-{da:02d} {h:02d}:{mi:02d} (sender's clock)")
    return recs


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("path")
    ap.add_argument("--save", metavar="FILE",
                    help="write the inflated blob here for further poking")
    ap.add_argument("--pixel-map", action="store_true",
                    help="verify the per-pixel coordinate map")
    args = ap.parse_args()

    hdr, d, comp_len, raw_len = inflate(args.path)
    print(f"{args.path}")
    print(f"  header     {hdr[:16].hex(' ')}")
    print(f"  compressed {comp_len}   uncompressed {raw_len}   inflated {len(d)}")
    if args.save:
        open(args.save, "wb").write(d)
        print(f"  saved inflated blob to {args.save}")

    print(f"\ngeometry (bytes 4,5): columns={d[4]}  scan={d[5]}")
    decode_records(d)

    groups = list(d[94:110])
    used = [g for g in groups if g != 0xFF]
    print(f"\ndata-group map (16 slots, 0xff = unused):")
    print(f"  {groups}")
    print(f"  {len(used)} used: {used}")

    off = find_reg_table(d)
    if off is None:
        print("\nno driver register table found (address signature absent)")
    else:
        print(f"\ndriver register table @ {off}  (addr, r, g, b)")
        print(f"  {'reg':5} {'r':>4} {'g':>4} {'b':>4}   name")
        for i in range(13):
            a, r, g, b = d[off + 4 * i: off + 4 * i + 4]
            print(f"  0x{a:02x}  0x{r:02x} 0x{g:02x} 0x{b:02x}   "
                  f"{REG_NAMES.get(a, '')}")
        print("\n  as 16-bit (addr<<8 | value) words, the shape"
              " gateware/spwm_chips.py stores:")
        for i in range(13):
            a, r, g, b = d[off + 4 * i: off + 4 * i + 4]
            trip = (f"(0x{(a<<8)|r:04x}, 0x{(a<<8)|g:04x}, 0x{(a<<8)|b:04x}),"
                    if not (r == g == b) else f"0x{(a<<8)|r:04x},")
            print(f"    {trip}")

    if args.pixel_map:
        # 128*64 on this module; the map sits well past the halfway mark.
        n = d[4] * d[5]
        # Find it by looking for the (y=0,x=0),(0,1),(0,2),(0,3) run as
        # (y, x_lo, x_hi) records. NOT the (x_lo, x_hi, y) run -- that
        # signature also matches, one byte later, and mis-frames every record.
        sig = bytes([0, 0, 0, 0, 1, 0, 0, 2, 0, 0, 3, 0])
        at = d.find(sig, len(d) // 2)
        if at < 0:
            print("\npixel map: signature not found")
        else:
            ents = decode_pixel_map(d, at, n)
            xs = {e[0] for e in ents}
            ys = {e[1] for e in ents}
            ident = sum(1 for i, e in enumerate(ents)
                        if e == (i % d[4], i // d[4]))
            print(f"\npixel coordinate map @ {at}: {len(ents)} entries")
            print(f"  x range {min(xs)}..{max(xs)}   y range {min(ys)}..{max(ys)}")
            print(f"  unique pairs {len({*ents})} of {len(ents)}")
            print(f"  identity entries {ident}/{len(ents)}"
                  f"  ({100.0*ident/len(ents):.1f}%)")
            if ident == len(ents):
                print("  => IDENTITY. The receiver applies no pixel remap for this")
                print("     module. Chip data order is the firmware's business, keyed")
                print("     by chip type, not this table's.")
            else:
                W = d[4]
                non = [(i // W, i % W, e) for i, e in enumerate(ents)
                       if e != (i % W, i // W)]
                cols = sorted({c for _, c, _ in non})
                print(f"  non-identity entries: {len(non)}, at columns {cols}")
                if cols == [W - 1]:
                    print("  ! every miss is in the LAST column: that is the record")
                    print("    mis-framed by one byte, not a remap. See the docstring.")
                print(f"  first non-identity: {non[:6]}")


if __name__ == "__main__":
    main()
