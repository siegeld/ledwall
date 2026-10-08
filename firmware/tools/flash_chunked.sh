#!/usr/bin/env bash
# Write a bitstream to flash over a JTAG link whose LONG transfers bit-slip.
#
# Why this exists (docs/FLASHING.md §2b): on this E320 a long JTAG read loses
# bit sync at a random offset and everything after it comes back shifted left
# one bit. Two reads of the same region disagree with each other. So a bulk
# write followed by a bulk verify can NEVER be trusted -- and for a whole
# session its false failures were misread as "the write truncates".
#
# The algorithm that survives an unreliable link:
#
#   * A bit-slipped verify always FAILS. It cannot produce a false pass.
#     So "VERIFY OK" on a chunk is trustworthy even though the link is not.
#   * Therefore: verify in chunks short enough to usually stay in sync, retry a
#     failing verify before concluding anything, and only rewrite a chunk that
#     fails repeatedly. Repeat until every chunk has passed at least once.
#
# This converges on a correct flash image without ever trusting a single read.
set -uo pipefail
cd "$(dirname "$0")/.."

BIT="${1:?usage: flash_chunked.sh <bitstream> [slot-hex]}"
SLOT="${2:-0x200000}"
CHUNK=4096
E=./.ecpprog/bin/ecpprog
O=./.ofl/bin/openFPGALoader
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
say() { printf '\n== %s\n' "$*"; }

pkill -x openFPGALoader 2>/dev/null || true
pkill -x ecpprog 2>/dev/null || true

# The ECP5 boots from a multiboot JUMP descriptor at 0x3FFF10 (§8j). Blank it
# first: while a valid descriptor is present the FPGA retries a boot from this
# same flash, and we would rather it sat still while we work.
say "parking the FPGA (blanking the boot descriptor)"
python3 -c 'open("'"$WORK"'/blank.bin","wb").write(b"\xff"*4096)'
"$E" -I B -p -o 0x3ff000 "$WORK/blank.bin" 2>&1 | tr '\r' '\n' \
    | grep -oE "VERIFY OK|Found difference" | tail -1
"$E" -I B -t -a >/dev/null 2>&1

# Strip the .bit ASCII header; the ECP5 wants the preamble at the slot (§8j).
say "extracting the payload"
python3 -c '
import sys
d = open(sys.argv[1],"rb").read()
i = d.find(bytes.fromhex("ffffffbdb3"))
if i < 0: sys.exit("no preamble -- not a .bit?")
p = d[i:]
open(sys.argv[2],"wb").write(p)
print("  payload %d bytes (header was %d)" % (len(p), i))' "$BIT" "$WORK/pay.bin" || exit 1
SIZE=$(stat -c%s "$WORK/pay.bin")
NCHUNK=$(( (SIZE + CHUNK - 1) / CHUNK ))

# Split once, so a chunk's verify compares against the exact same bytes we wrote.
split -b "$CHUNK" -d -a 4 "$WORK/pay.bin" "$WORK/c."

say "bulk write to ${SLOT} (gets most of it right; we repair the rest)"
"$O" --board colorlight --cable ft2232_b -f --unprotect-flash --skip-reset \
     --file-type raw --offset "$SLOT" "$WORK/pay.bin" 2>&1 | tr '\r' '\n' \
     | grep -oE "100.00%|Done|Fail|Error" | tail -2

verify_chunk() {  # $1=file $2=offset ; true if it verifies within 3 tries
    local f=$1 off=$2 t
    for t in 1 2 3; do
        if "$E" -I B -c -o "$off" "$f" 2>&1 | tr '\r' '\n' | grep -q "VERIFY OK"; then
            return 0
        fi
    done
    return 1
}

say "verify-and-repair, ${NCHUNK} chunks of ${CHUNK} bytes"
BAD_FINAL=0
for round in 1 2 3 4; do
    bad=0; idx=0
    for f in "$WORK"/c.*; do
        off=$(( $(printf '%d' "$SLOT") + idx * CHUNK ))
        if ! verify_chunk "$f" "$off"; then
            # Three verifies failed -- treat the chunk as genuinely wrong and
            # rewrite it alone, with no erase so neighbours are untouched.
            "$E" -I B -n -p -X -o "$off" "$f" >/dev/null 2>&1
            if ! verify_chunk "$f" "$off"; then
                bad=$((bad+1)); printf '  chunk %4d @ 0x%06x still bad\n' "$idx" "$off"
            fi
        fi
        idx=$((idx+1))
    done
    printf '  round %d: %d chunk(s) unrepaired of %d\n' "$round" "$bad" "$NCHUNK"
    BAD_FINAL=$bad
    [ "$bad" -eq 0 ] && break
done

if [ "$BAD_FINAL" -ne 0 ]; then
    say "STOPPING: ${BAD_FINAL} chunk(s) never verified -- not pointing the FPGA at a bad image"
    exit 1
fi

say "every chunk verified -- pointing the descriptor at ${SLOT}"
python3 -c '
import sys
slot = int(sys.argv[1],16)
s = bytearray(b"\xff"*4096)
s[0xF10:0xF20] = bytes.fromhex("ffffbdb3ffffffff7e000000") + bytes(
    [0x03,(slot>>16)&0xff,(slot>>8)&0xff,slot&0xff])
open(sys.argv[2],"wb").write(bytes(s))' "$SLOT" "$WORK/desc.bin"
"$E" -I B -p -o 0x3ff000 "$WORK/desc.bin" 2>&1 | tr '\r' '\n' \
    | grep -oE "VERIFY OK|Found difference" | tail -1

say "REFRESH -- the ECP5 loads it from flash by itself"
"$E" -I B -t -a 2>&1 | grep -E "Status" | tail -1
st=$("$E" -I B -t 2>&1 | grep -oE '0x[0-9a-f]{8}' | tail -1)
say "verdict  status=${st}"
if [ "${st}" = "0x00200100" ]; then
    echo "  DONE HIGH -- configured from flash. It will do this on every power-up."
else
    e=$(( (0x${st#0x} >> 23) & 7 ))
    case "$e" in
      0) w="none" ;; 1) w="ID: wrong device" ;; 2) w="CMD" ;;
      3) w="CRC: image corrupt" ;; 4) w="PREAMBLE: nothing valid at the slot" ;;
      5) w="ABORT" ;; *) w="code $e" ;;
    esac
    echo "  DONE low. BSE error ${e}: ${w}"
    echo "  (status is LATCHED -- read it again after a REFRESH, not live)"
fi
