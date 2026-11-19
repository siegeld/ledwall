#!/bin/bash
# The runtime-geometry build of the four-output E320 (docs/RUNTIME-GEOMETRY.md):
# exp/build-out4.sh with the three knobs that make one bitstream serve every
# module of four RGB groups no bigger than the preset's.
#
#   EXP_BLOCK_SEQ=1  one RGB group per fetch run, whatever the arrangement
#   EXP_RT_GEOM=1    columns per chain and scan lines are the `geom` CSR
#   EXP_DMATEST=0    the SDRAM write tester's engine left out, to make room
#
# Usage as build-out4.sh: exp/build-rt.sh [targets...], SEED=, PANEL=.
# PANEL only chooses what the card wakes up as (chip table, group origins).
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1
exec env EXP_BLOCK_SEQ=1 EXP_RT_GEOM=1 EXP_DMATEST=0 exp/build-out4.sh "$@"
