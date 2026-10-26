#!/bin/bash
# The four-output E320 build that refreshes the P1.25 at clk_div 4 (66.7 Hz) with
# the stream running -- the same rate as three outputs -- and fits the LFE5U-25F.
#
# Measured on the bench card 2026-10-26 (gateware 1.45.0, firmware 3.8.2/3.8.3):
#   55/56 EBR, ~94% LUT4, sys 35 MHz closes at 47-50 MHz on every seed tried
#   row fill 3105 clocks paused / 3433 streaming, against 8192 at clk_div 4
#   0 underruns at clk_div 3, 4, 5, 6 and 8, streaming and paused; 5-minute soak
#   at clk_div 4 with a 20 fps stream: 0 underruns, 0 dropped chunks, 66.67 Hz
#
# The knobs (gateware/icn1065_soc.py) and why each is needed:
#   EXP_PIPELINE=1  a column's reads go out without waiting for the previous data
#   EXP_BLOCK=4     the bandwidth: 4 columns of one same-row group pair per run
#   EXP_TRIPLE=1    a third line-buffer slot: two row periods per fill, which
#                   absorbs the pixel DMA's write bursts
#   EXP_SRAM / EXP_BIOS_CONSOLE / EXP_EBR_FIXED  the block-RAM trims that let a
#                   fourth output fit at all (exp/run-matrix.sh)
#
# Produces the bitstream and the firmware together, from one tree, so their CSR
# maps match. hub75.rs OUTPUTS must already say 4 (build.sh refuses otherwise).
#
# Usage: exp/build-out4.sh [targets...]   (default: gateware svd pac firmware)
#        SEED=<n> to change the place-and-route seed (default 7)
cd "$(dirname "$(readlink -f "$0")")/.." || exit 1
targets=("$@")
[ ${#targets[@]} -eq 0 ] && targets=(gateware svd pac firmware)
# The image is exp/Dockerfile.out4 (see its header); the stock one's BIOS is too big.
exec env DOCKER_IMAGE="${DOCKER_IMAGE:-litex-hub75:out4}" EXP_SRAM=0x1000 EXP_BIOS_CONSOLE=lite EXP_EBR_FIXED=35 \
    EXP_PIPELINE=1 EXP_BLOCK=4 EXP_TRIPLE=1 \
    ./build.sh --board e320 --panel 256x128-dp3264s-hub320 --outputs 4 \
    --burst-depth 2048 --sys-clk 35000000 --seed "${SEED:-7}" "${targets[@]}"
