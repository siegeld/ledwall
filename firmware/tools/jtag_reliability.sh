#!/usr/bin/env bash
# How reliably can this card be configured, right now?
#
# Run it immediately after a power cycle, then again after a few failures, to
# see whether configuration degrades with use. On the E320 bench card on
# 2026-10-04 it went from first-try success to 0-of-30 over a session, and only
# a power cycle restored it -- so this measures the thing that actually decides
# whether you can program the board.
#
# Uses the SMALLEST bitstream on purpose (the pin walker, ~132 KB): it is the
# easiest thing to load, so a failure here is not about design size.
set -u
cd "$(dirname "$0")/.."
N="${1:-6}"
BIT="${2:-build/pinwalk/top.bit}"
pkill -x openFPGALoader 2>/dev/null
pkill -x ecpprog 2>/dev/null
ok=0
for i in $(seq 1 "$N"); do
    if timeout 200 ./.ofl/bin/openFPGALoader --cable ft2232_b "$BIT" >/dev/null 2>&1; then
        ok=$((ok+1)); printf "  %2d/%s LOADED\n" "$i" "$N"
    else
        st=$(timeout 60 ./.ecpprog/bin/ecpprog -I B -t 2>&1 | grep -oE '0x[0-9a-f]{8}' | tail -1)
        printf "  %2d/%s failed  status=%s\n" "$i" "$N" "${st:-?}"
    fi
done
echo "  => ${ok} of ${N} configured   ($(basename "$BIT"))"
[ "$ok" -eq 0 ] && cat <<'TXT'

  0 of N means the card will not take a bitstream at all. Everything software
  has been tried and ruled out on this board -- both tools, every cable
  definition, 100 kHz to 6 MHz, erased vs bootable flash, ecpprog reset and
  REFRESH beforehand, the nightly openFPGALoader, and with no orphaned
  processes or ModemManager running. See docs/FLASHING.md §6/§8.
  The one thing that has restored it is a POWER CYCLE of the card.
TXT
exit 0
