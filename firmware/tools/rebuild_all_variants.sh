#!/usr/bin/env bash
# Build every firmware and bitstream variant, at one version, and stage them.
#
# Why this is a script and not a list of commands in someone's history:
#
#  * There are four firmware variants (board x S-PWM) and the S-PWM ones need the
#    PAC regenerated from an S-PWM bitstream's SVD first, because the ICN1065
#    gateware's hub75 peripheral has different registers. The PAC is ONE
#    checked-in directory, so the variants cannot be built in parallel and the
#    order matters. Getting it wrong produces a firmware that compiles and cannot
#    drive the panel -- which is exactly what happened on 2026-10-07, when a
#    firmware-only build silently omitted --features spwm and two "different"
#    binaries came out with the same sha256.
#
#  * Every artifact must carry the same version, and the names must match what a
#    card reports running, or marquee's fleet view compares a filename against an
#    identity. build.sh prints the right name; this uses the same rule.
#
# Usage: tools/rebuild_all_variants.sh [marquee-url]
set -uo pipefail
cd "$(dirname "$0")/.."

MARQUEE="${1:-http://localhost:8410}"
FW_VER=$(sed -n 's/^version = "\(.*\)"/\1/p' sw_rust/barsign_disp/Cargo.toml | head -1)
BS_VER=$(tr -d ' \n' < gateware/VERSION)
OUT=/tmp/variants-$$
mkdir -p "$OUT"
say() { printf '\n== %s\n' "$*"; }
fail=0

# board : panel : outputs : firmware-suffix
VARIANTS=(
  "5a-75e:128x64:6:"
  "5a-75e:256x128-icnd1065:6:-spwm"
  "e320:128x64:6:"
  "e320:256x256-icnd1065-hub320:8:-spwm"
)

say "firmware ${FW_VER}, gateware ${BS_VER}"

for v in "${VARIANTS[@]}"; do
    IFS=: read -r board panel outs sfx <<< "$v"
    # --allow-timing-fail on EVERY board, and the real gate applied below.
    #
    # nextpnr's 125 MHz constraint on the RGMII RX domain is not the requirement
    # the hardware has to meet: that clock is source-synchronous from the PHY and
    # capture depends on this PCB's tuned 2 ns rx_delay, which static analysis
    # cannot model. Measured on the E320 at 936 Mbit/s: 9,849,178 frames,
    # mac_crc_errors 0, mac_preamble_errors 0 (tools/eth_rx_stress.py,
    # docs/HARDWARE.md §6).
    #
    # Worse, its pass/fail is placement NOISE. The same 5A-75E build passed at
    # 139.92 MHz and then failed at 114.56 MHz with no change but the identifier
    # string getting one character longer -- so gating on it fails builds at
    # random while saying nothing about whether Ethernet works.
    #
    # The SYSTEM clock is the meaningful constraint and it is gated, below, by
    # reading the report rather than by letting nextpnr abort: a sys-clock miss is
    # a real functional failure (every timer derives from it).
    clk="--allow-timing-fail"
    [[ "$board" == "e320" ]] && clk="--sys-clk 35000000 --allow-timing-fail"

    say "${board} ${panel} out${outs}"
    # Bitstream first: it regenerates the SVD that the PAC and the firmware's
    # register map come from, and publishes the clock the firmware bakes in.
    if ! ./build.sh --board "$board" --panel "$panel" --outputs "$outs" ${clk} \
            bitstream >"$OUT/${board}${sfx}.bitlog" 2>&1; then
        echo "  BITSTREAM FAILED -- see $OUT/${board}${sfx}.bitlog"; fail=1; continue
    fi
    grep -oE "Max frequency for clock +'[^']+': [0-9.]+ MHz \((PASS|FAIL)[^)]*\)" \
        "$OUT/${board}${sfx}.bitlog" | tail -2 | sed 's/^/    /'

    # The gate: the SYSTEM clock must pass. Read from the report, because the
    # build is deliberately not aborting on the Ethernet domain.
    # The LAST report, not any report. nextpnr prints one per placement
    # iteration, so an intermediate miss on the way to a passing placement is
    # normal -- grepping the whole log for FAIL refused a variant whose final
    # system clock was 50.82 MHz against 35.00 (PASS).
    sysline=$(grep -oE "main_crg_clkout0': [0-9.]+ MHz \((PASS|FAIL)[^)]*\)" \
              "$OUT/${board}${sfx}.bitlog" | tail -1)
    case "${sysline}" in
        *FAIL*) echo "  SYSTEM CLOCK MISSED (${sysline}) -- refusing this variant"
                fail=1; continue ;;
    esac

    # PAC from THIS bitstream's SVD, then firmware against it. Both, every time:
    # skipping the PAC is how a firmware gets built against the other variant's
    # register map.
    ./build.sh --board "$board" --panel "$panel" pac >"$OUT/${board}${sfx}.paclog" 2>&1 \
        || { echo "  PAC FAILED"; fail=1; continue; }
    if ! ./build.sh --board "$board" --panel "$panel" firmware \
            >"$OUT/${board}${sfx}.fwlog" 2>&1; then
        echo "  FIRMWARE FAILED -- see $OUT/${board}${sfx}.fwlog"; fail=1; continue
    fi
    grep -oE "firmware system clock: [0-9]+ Hz|Firmware features: [^ ]+ [^ ]+" \
        "$OUT/${board}${sfx}.fwlog" | sort -u | sed 's/^/    /'

    cp .tftp/boot.bin "$OUT/${board}${sfx}-v${FW_VER}.bin"
    bit=$([[ "$board" == "5a-75e" ]] && echo "bitstreams/${panel}.bit" \
                                     || echo "bitstreams/${board}/${panel}.bit")
    cp "$bit" "$OUT/${board}-${panel}-out${outs}-v${BS_VER}.bit"
    echo "    fw  $(sha256sum "$OUT/${board}${sfx}-v${FW_VER}.bin" | cut -c1-16)"
    echo "    bit $(sha256sum "$OUT/${board}-${panel}-out${outs}-v${BS_VER}.bit" | cut -c1-16)"
done

say "distinct firmware binaries"
n=$(sha256sum "$OUT"/*.bin 2>/dev/null | awk '{print $1}' | sort -u | wc -l)
echo "  ${n} of $(ls "$OUT"/*.bin 2>/dev/null | wc -l)"
[[ "$n" -lt 4 ]] && { echo "  REFUSING TO STAGE: variants are not distinct."; exit 1; }

say "staging to ${MARQUEE}"
for f in "$OUT"/*.bin; do
    printf '  %-32s ' "$(basename "$f")"
    curl -s -m 60 -X POST -F "file=@${f};filename=$(basename "$f")" \
         "${MARQUEE}/api/v1/firmware" -o /dev/null -w "%{http_code}\n"
done
for f in "$OUT"/*.bit; do
    printf '  %-48s ' "$(basename "$f")"
    curl -s -m 90 -X POST -F "file=@${f};filename=$(basename "$f")" \
         "${MARQUEE}/api/v1/bitstreams" -o /dev/null -w "%{http_code}\n"
done

say "done (artifacts in ${OUT})"
exit "$fail"
