#!/usr/bin/env bash
# RB-5554 step 1: prove the firmware can erase, program and verify its own flash.
#
# Runs entirely over the network -- no JTAG -- which is the whole point of the
# feature. Confined to the 128 KB scratch region the firmware's
# region_is_writable() enforces (0x360000..0x380000), clear of the bitstream at
# 0 and the firmware at 0x100000.
#
#   tools/ota_selftest.sh <card-ip> [addr] [len]
#
# What a PASS looks like:
#   job   -> {"state":"done", ...,"cursor":<len>}
#   verify-> {"selftest":"match", "blank":false}
#
# The strings come from network.rs: "match" on success, "differs" with a
# first_bad offset on mismatch. This harness originally looked for "ok", which
# no firmware ever returns, so a PASSING selftest was reported as FAIL -- the
# firmware is the contract, not this script.
#
# The failure this exists to catch, which happened on first contact with real
# hardware: a protected flash chip refuses an erase or program OUTRIGHT rather
# than starting it, so it never asserts BUSY. Every busy-poll returns
# immediately and every layer up the stack reports success while nothing is
# written. `verify` reporting `blank:true` after a `done` job is that bug.
set -uo pipefail

IP="${1:?usage: ota_selftest.sh <card-ip> [addr] [len]}"
ADDR="${2:-3538944}"   # 0x360000. The API wants a number, quoted or not.
LEN="${3:-4096}"

say() { printf '\n== %s\n' "$*"; }
get() { curl -s -m 8 "http://${IP}$1"; }

say "card reachable?"
get /api/status >/dev/null || { echo "FAIL: no HTTP on ${IP}"; exit 1; }
echo "ok"

say "flash status registers (SR1 BP bits = write protection)"
get /api/flash; echo

say "queue selftest at ${ADDR} (0x$(printf %x "${ADDR}")) len ${LEN}"
curl -s -m 10 -X POST "http://${IP}/api/flash/job" \
     -H 'Content-Type: application/json' \
     -d "{\"kind\":\"selftest\",\"addr\":${ADDR},\"len\":${LEN}}"; echo

say "poll until done or failed"
for _ in $(seq 1 60); do
    r=$(get /api/flash/job); echo "  ${r}"
    case "${r}" in
        *'"state":"done"'*)   break ;;
        *'"state":"failed"'*) echo "FAIL: job reported failed"; exit 1 ;;
    esac
    sleep 2
done

say "verify through the memory-mapped read path"
v=$(curl -s -m 15 -X POST "http://${IP}/api/flash/verify" \
        -H 'Content-Type: application/json' \
        -d "{\"addr\":${ADDR},\"len\":${LEN}}")
echo "  ${v}"

say "verdict"
case "${v}" in
    *'"selftest":"match"'*)
                          echo "PASS -- the firmware wrote and read back its own flash"; exit 0 ;;
    *'"selftest":"differs"'*)
                          echo "FAIL -- written and read-back bytes differ; see first_bad above"; exit 1 ;;
    *'"blank":true'*)     echo "FAIL -- region reads blank after a 'done' job."
                          echo "        The chip refused the write without ever going BUSY."
                          echo "        Check unprotect() cleared SR1's BP bits; see the"
                          echo "        status registers printed above."; exit 1 ;;
    *)                    echo "FAIL -- verify did not report ok"; exit 1 ;;
esac
