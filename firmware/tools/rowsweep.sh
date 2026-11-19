#!/bin/bash
# Sweep row-driver modes on a live card; log one timestamped line per step.
C=${C:-http://192.168.1.62}; HOLD=${HOLD:-4}
post() { curl -s -m4 -X POST "$C$1" -H 'Content-Type: application/json' -d "$2" >/dev/null; }
step() { echo "$(date +%H:%M:%S) step $n: $1"; n=$((n+1)); sleep "$HOLD"; }
n=1
post /api/display/on '{}'; post /api/display/pattern '{"name":"white"}'
# Address mode: A/B/C carry the row number.
post /api/rowser '{"enable":0}'
for sh in 0 3; do
  post /api/chipcfg "{\"row_address\":1,\"row_addr_shift\":$sh}"; step "address mode, shift $sh"
done
post /api/chipcfg '{"row_address":0,"row_addr_shift":0}'; step "rows held at 0 (no stepping)"
# Serial chain: every assignment of DIN/LCK/BK to A/B/C, every polarity.
names=(DIN LCK BK)
for p in "0 1 2" "0 2 1" "1 0 2" "1 2 0" "2 0 1" "2 1 0"; do
  set -- $p
  for inv in 0 1 2 3 4 5 6 7; do
    post /api/rowser "{\"enable\":1,\"a_src\":$1,\"b_src\":$2,\"c_src\":$3,\"inv\":$inv}"
    step "serial A=${names[$1]} B=${names[$2]} C=${names[$3]} inv=$inv"
  done
done
# Back to the default serial mapping.
post /api/rowser '{"enable":1,"a_src":1,"b_src":2,"c_src":0,"inv":0}'
echo "$(date +%H:%M:%S) done"
