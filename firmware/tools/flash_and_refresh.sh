#!/usr/bin/env bash
# Put a bitstream in flash and let the ECP5 configure ITSELF from it.
#
# Why this path matters more than a JTAG SRAM load:
#
#  * It is how a deployed card comes up. An SRAM load is volatile and gone at
#    the next power cycle, so a card provisioned that way is not provisioned.
#  * It does not need JTAG CONFIGURATION to work. On 2026-10-04 the E320 reached
#    a latched state where every SRAM load failed (0 of 30+, both tools, every
#    clock, both flash states -- docs/FLASHING.md §0b) while SHORT JTAG
#    transfers stayed byte-perfect. Flash page writes and REFRESH are short
#    transfers, so this route can work when configuration cannot.
#
# The card does not boot from offset 0: a multiboot JUMP descriptor at 0x3FFF10
# names the address to load, so a bitstream means writing the image AND
# repointing the descriptor. docs/FLASHING.md §8j.
set -uo pipefail
cd "$(dirname "$0")/.."

BIT="${1:?usage: flash_and_refresh.sh <bitstream> [slot-hex]}"
SLOT="${2:-0x200000}"
E=./.ecpprog/bin/ecpprog
O=./.ofl/bin/openFPGALoader
say() { printf '\n== %s\n' "$*"; }

pkill -x openFPGALoader 2>/dev/null || true
pkill -x ecpprog 2>/dev/null || true

PAY=$(mktemp); DESC=$(mktemp)
trap 'rm -f "$PAY" "$DESC"' EXIT

# Strip the .bit's ASCII comment header and write the payload RAW.
#
# A .bit starts  ff 00 "Part: LFE5U-25F-7CABGA256" 00  and only then the real
# preamble ff ff ff bd b3. openFPGALoader strips that when it PARSES a .bit, but
# parsing and --offset do not go together (the write dies partway), and with
# --file-type raw it writes the header verbatim so the ECP5 finds no preamble
# and REFRESH fails with BSE error 4. Measured both ways. So cut it here.
say "extracting the bitstream payload"
python3 -c '
import sys
d = open(sys.argv[1],"rb").read()
i = d.find(bytes.fromhex("ffffffbdb3"))
if i < 0: sys.exit("no preamble (ff ff ff bd b3) -- not a .bit?")
open(sys.argv[2],"wb").write(d[i:])
print("  payload at offset %d, %d bytes" % (i, len(d)-i))' "$BIT" "$PAY" || exit 1

say "writing payload to ${SLOT}"
"$O" --board colorlight --cable ft2232_b -f --unprotect-flash --skip-reset \
     --file-type raw --offset "${SLOT}" "$PAY" 2>&1 | tr '\r' '\n' \
     | grep -E "Erasing|100.00%|Done|Fail|Error" | tail -3

say "pointing the multiboot descriptor at ${SLOT}"
python3 -c '
import sys
slot = int(sys.argv[1],16)
s = bytearray(b"\xff"*4096)
s[0xF10:0xF20] = bytes.fromhex("ffffbdb3ffffffff7e000000") + bytes(
    [0x03,(slot>>16)&0xff,(slot>>8)&0xff,slot&0xff])
open(sys.argv[2],"wb").write(bytes(s))' "$SLOT" "$DESC"
"$E" -I B -p -o 0x3ff000 "$DESC" 2>&1 | tr '\r' '\n' \
    | grep -E "VERIFY OK|Found difference|ABORT" | tail -1

say "REFRESH -- the ECP5 loads it from flash by itself"
"$E" -I B -t -a 2>&1 | tail -2

say "verdict"
st=$("$E" -I B -t 2>&1 | grep -oE '0x[0-9a-f]{8}' | tail -1)
echo "  status=${st}"
if [ "${st}" = "0x00200100" ]; then
    echo "  DONE HIGH -- configured from flash. It will do this on every power-up."
else
    e=$(( (st >> 23) & 7 ))
    case "$e" in
      0) w="none (not configured -- write may not have landed)" ;;
      1) w="ID: bitstream is for a different device" ;;
      2) w="CMD" ;;
      3) w="CRC: image corrupt or partially written" ;;
      4) w="PREAMBLE: no valid bitstream at the slot (header not stripped?)" ;;
      5) w="ABORT" ;;
      *) w="code $e" ;;
    esac
    echo "  DONE low. BSE error ${e}: ${w}"
fi
