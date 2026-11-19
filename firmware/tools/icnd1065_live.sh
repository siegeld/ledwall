#!/bin/bash
# Load the working Myled P1.25 (ICND1065L) setup into a running card. docs/PANELS.md.
# Load the icnd1065 chip table into a running card over its live endpoints.
C=${1:-http://192.168.1.62}
post() { curl -s -m5 -X POST "$C$1" -H 'Content-Type: application/json' -d "$2"; echo; }
REGS=(0x00aa 0x01aa 0x023f 0x0335 0x0412 0x0500 0x0601 0x0720 0x0c18 0x0d01 0x0e86 0x0f01 0x1040 0x1127 0x1200 0x1300 0x1400 0x1500 0x1600 0x1800 0x1906 0x1c60 0x1dca 0x1e73 0x1f00 0x2000 0x2100 0x2200 0x2300 0x2400 0x2500 0x2600 0x2700 0x7000 0x7100 0x7200 0x7300 0x74a0)
WRAP=(0x00aa 0x01aa 0x0055 0x0155)
i=0; for v in "${REGS[@]}"; do post /api/chipreg "{\"index\":$i,\"value\":$((v))}" >/dev/null; i=$((i+1)); done
i=64; for v in "${WRAP[@]}"; do post /api/chipreg "{\"index\":$i,\"value\":$((v))}" >/dev/null; i=$((i+1)); done
post /api/chipcfg '{"reg_count":38,"slots":5,"rot_slot":2,"mid_latch":1,"chan_rev":0,"row_pulse":4,"row_pulse_first":12,"data_latch":1,"row_address":0,"row_addr_shift":0}'
post /api/sweepadv '{"n":0}'
post /api/chanxor '{"n":0}'
post /api/rowser '{"enable":1,"a_src":1,"b_src":2,"c_src":0,"inv":0}'
post /api/display/on '{}'
post /api/display/pattern "{\"name\":\"${PATTERN:-white}\"}"
