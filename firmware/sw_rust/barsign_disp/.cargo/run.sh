#!/usr/bin/env bash
set -euo pipefail



set -e

# Create bin file
riscv64-unknown-elf-objcopy $1 -O binary $1.bin

lxterm $2 --kernel $1.bin
