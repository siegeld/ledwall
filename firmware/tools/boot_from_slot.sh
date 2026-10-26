#!/usr/bin/env bash
# Point the ECP5 at a bitstream somewhere OTHER than flash offset 0, by writing
# a JUMP image at offset 0 that redirects to it.
#
# This is the mechanism bitstream OTA needs. The FPGA starts reading flash at
# address 0 on every power-up and REFRESH (docs/FLASHING.md §8j), so the only
# way to change which image it loads -- without JTAG -- is to change what is at
# offset 0. A whole bitstream there is ~420 KB and rewriting it is a long
# unprotected window; a JUMP image is 16 bytes in one sector, so the window in
# which a power cut leaves an unbootable card is about as small as it can be.
#
# The descriptor format is copied from the factory's own, found at 0x3FFF10 on
# a stock E320:
#
#   ff ff bd b3   ff ff ff ff   7e 00 00 00   03 20 00 00
#   preamble      dummy         JUMP          SPI read 0x03 @ 0x200000
#
#   tools/boot_from_slot.sh <slot-hex>        e.g. 0x200000
#
# Writes only the first sector. The bitstream already in the slot is untouched.
set -uo pipefail
cd "$(dirname "$0")/.."

SLOT="${1:?usage: boot_from_slot.sh <slot-hex>   (e.g. 0x200000)}"
E=./.ecpprog/bin/ecpprog
W=$(mktemp -d); trap 'rm -rf "$W"' EXIT
say() { printf '\n== %s\n' "$*"; }

pkill -x ecpprog 2>/dev/null || true
pkill -x openFPGALoader 2>/dev/null || true

say "building a JUMP image for ${SLOT}"
python3 -c '
import sys
slot = int(sys.argv[1], 16)
img = bytearray(b"\xff" * 4096)
img[0:18] = (bytes.fromhex("ffffffff") + bytes.fromhex("bdb3")
             + bytes.fromhex("ffffffff") + bytes.fromhex("7e000000")
             + bytes([0x03, (slot >> 16) & 0xff, (slot >> 8) & 0xff, slot & 0xff]))
open(sys.argv[2], "wb").write(bytes(img))
print("   ", img[:18].hex(" "))' "$SLOT" "$W/jump.bin" || exit 1

say "writing it to flash offset 0 (one sector)"
# -X: skip the verify. A read-back over JTAG bit-slips on this card and reports
# a difference on a perfectly good write (docs/FLASHING.md §2b); the board is
# the verifier.
"$E" -I B -p -X -o 0 "$W/jump.bin" 2>&1 | tr '\r' '\n' \
    | grep -E "programming.*/|Bye" | tail -1

say "what offset 0 holds now"
"$E" -I B -o 0 -R 32 "$W/rb.bin" >/dev/null 2>&1
python3 -c 'print("   ", open("'"$W"'/rb.bin","rb").read()[:18].hex(" "))'

say "REFRESH -- does the ECP5 follow the JUMP?"
"$E" -I B -t -a 2>&1 | grep -oE 'Status Register: 0x[0-9a-f]{8}' | tail -1
st=$("$E" -I B -t 2>&1 | grep -oE '0x[0-9a-f]{8}' | tail -1)
say "status ${st}"
python3 - "$st" <<'PY'
import sys
st = int(sys.argv[1], 16)
bse = (st >> 23) & 7
names = {0: "none", 1: "ID", 2: "CMD", 3: "CRC", 4: "PREAMBLE", 5: "ABORT"}
print(f"   DONE={(st>>8)&1}  preamble={(st>>21)&1}  SPIm_fail={(st>>22)&1}  "
      f"BSE={bse} ({names.get(bse,bse)})  exec={(st>>26)&1}")
print("   NOTE: this register is LATCHED. Confirm on the network, not here;"
      "\n         and ecpprog -t de-configures a running card.")
PY
