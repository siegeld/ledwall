#!/bin/bash
#
# Colorlight HUB75 LED Controller - Build Script
#
# Usage: ./build.sh [OPTIONS] [TARGETS]
#
# Run './build.sh --help' for full documentation
#

set -e

# =============================================================================
# Configuration
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCKER_IMAGE="${DOCKER_IMAGE:-litex-hub75}"
# The FIRMWARE builds in a pinned image, the gateware does not.
#
# litex-hub75:latest was rebuilt 2026-10-05 (to carry the LiteX BIOS link-wait
# patch) and picked up rustc 1.99.0, whose LLVM will not assemble riscv-rt
# 0.13.0's inline asm: "this directive must appear between .cfi_startproc and
# .cfi_endproc" then "Unfinished frame!". riscv-rt is pinned by Cargo.lock, so
# the firmware simply stopped building while every gateware target kept working
# -- which reads like a code error and is not one.
#
# Override either with an env var. Fix properly by pinning the Rust toolchain
# in the Dockerfile, after which these can be the same image again.
FIRMWARE_IMAGE="${FIRMWARE_IMAGE:-litex-hub75:pinned}"
REVISION="8.2"
# --ecppack-compress shrinks the bitstream and is the default. COMPRESS=0
# (./build.sh --no-compress) builds without it.
#
# It was briefly believed that compression caused JTAG configuration failures
# here. THAT WAS WRONG and is recorded so nobody repeats it: the run that
# "proved" it was confounded by an orphaned openFPGALoader holding the FTDI
# device, and the 3-of-3 successes credited to an uncompressed image were
# actually the COMPRESSED one. A 693 KB uncompressed build then failed where a
# 448 KB compressed build had worked, so if anything correlates it is SIZE, and
# that is confounded too. Configuration failures on this bench are intermittent
# -- check for stale processes first. docs/FLASHING.md.
COMPRESS="${COMPRESS:-1}"
# Which RGMII PHY to build against. Revision 8.2 defines two, and the E320's
# jack-to-PHY mapping is NOT established -- a cable in the jack wired to the
# other PHY gives a card that configures perfectly and never transmits a frame,
# because the BIOS waits for link before sending DHCP. ./build.sh --eth-phy 1
ETH_PHY="${ETH_PHY:-0}"

# nextpnr placement seed. Timing varies a LOT with it on this design: the same
# 4-group HUB320 source landed between 42.5 and 55.5 MHz on a -7 just by
# changing seed. So a seed is not cosmetic here, it decides whether the build
# meets timing at all. ./build.sh --seed N
SEED="${SEED:-1}"

# System clock. 40 MHz is the default the panel timing math is written around.
# The E320 is a -6 part and does NOT close 40 MHz (37.2-37.9 MHz measured), so
# a lower clock is the honest setting there until the design is faster.
# Lowering it reduces panel refresh -- see docs/HARDWARE.md §6 -- so it is a
# deliberate trade, not a default. ./build.sh --sys-clk 35000000
SYS_CLK="${SYS_CLK:-}"

# Make a timing failure FATAL. LiteX defaults to --timing-allow-fail, so a
# build that misses timing ships silently -- and on 2026-10-04 that is exactly
# what had been happening: timed honestly as the -6 part it is, the E320 build
# failed BOTH domains (eth RX 115.5 against 125 MHz, sys 37.2 against 40 MHz)
# and was flashed anyway. A card running out of spec configures, runs, drives
# panels, and does unreliable things like never completing DHCP.
# ./build.sh --allow-timing-fail to opt out deliberately.
TIMING_STRICT="${TIMING_STRICT:-1}"
IP_ADDRESS="192.168.1.50"
# Where the BIOS fetches boot.bin: the marquee host. Give it a DHCP RESERVATION
# -- this value is compiled into every bitstream, so a lease that moves silently
# breaks netboot for the whole fleet.
TFTP_SERVER="192.168.1.10"
# NOT usb-blaster: the Waveshare USB Blaster corrupts bulk JTAG transfers
# (TODO item 3 -- 29% of bytes wrong on a 1MB dump) while --detect passes, so it
# looks like it works right up until a flash is silently wrong. ft2232_b is the
# Tigard on this bench.
CABLE="ft2232_b"
PANEL="128x64"
# Panel presets whose output stage is S-PWM rather than HUB75 bit-plane. The
# firmware's chip-table code only compiles against a bitstream that has the
# chip CSRs, and the PAC is regenerated per bitstream, so this has to follow
# the panel choice rather than being always on.
# DERIVED from the PANELS dict, not hand-written. The list used to be a
# literal and had already drifted: 256x128-icnd1065-565 was missing, so
# building that panel produced S-PWM gateware with firmware compiled WITHOUT
# the chip-table code -- a silent mismatch of exactly the kind that
# build_bitstream() already guards against by reading CHAIN_LENGTH out of
# hub75.rs. A new preset must not be able to reintroduce it.
spwm_panels_from_source() {
    python3 - "${SCRIPT_DIR}/gateware/colorlight.py" <<'PYEOF' 2>/dev/null
import re, sys
src = open(sys.argv[1]).read()
block = re.search(r'^PANELS\s*=\s*\{(.*?)^\}', src, re.S | re.M)
if not block:
    sys.exit(1)
names = [m.group(1) for m in re.finditer(r'"([^"]+)"\s*:\s*\{[^{}]*\}', block.group(1))
         if '"driver"' in m.group(0) and 'icn1065' in m.group(0)]
if not names:
    sys.exit(1)
print(" ".join(names))
PYEOF
}
SPWM_PANELS="$(spwm_panels_from_source)" || SPWM_PANELS=""
if [[ -z "${SPWM_PANELS}" ]]; then
    echo "build.sh: could not read the S-PWM panel list out of gateware/colorlight.py" >&2
    echo "  Refusing to guess: a wrong list builds S-PWM gateware against firmware" >&2
    echo "  that cannot drive it, and nothing reports an error." >&2
    exit 1
fi
CARGO_FEATURES=""

# Decide whether this build needs the firmware's S-PWM support.
#
# MUST be called by every target that compiles firmware, not just the bitstream
# one. It used to live inside build_bitstream(), so `./build.sh --panel
# 256x128-icnd1065 firmware` compiled with the feature OFF and said nothing: the
# binary came out byte-identical to a plain build, and would have reached a P1.25
# panel unable to talk to its driver chip. Found 2026-10-07 while building the
# variants, by noticing two "different" firmwares had the same sha256.
select_cargo_features() {
    case " ${SPWM_PANELS} " in
        *" ${PANEL} "*) CARGO_FEATURES="--features spwm" ;;
        *) CARGO_FEATURES="" ;;
    esac
}
OUTPUTS=6
# Must match `const CHAIN_LENGTH` in sw_rust/barsign_disp/src/hub75.rs -- the
# firmware bakes it in at compile time and build_bitstream() refuses to build a
# gateware that disagrees. This default said 2 from v1.7.0 until 2026-09-17,
# long after the firmware moved to 1, so any plain `./build.sh bitstream` built
# a gateware the firmware contradicted.
CHAIN_LENGTH=1
PATTERN="grid"
BUILD_DIR="build/colorlight_5a_75e"
BITSTREAM="${BUILD_DIR}/gateware/colorlight_5a_75e.bit"
FIRMWARE_DIR="sw_rust/barsign_disp"
FIRMWARE_BIN="${FIRMWARE_DIR}/target/riscv32i-unknown-none-elf/release/barsign-disp"
TFTP_DIR="${SCRIPT_DIR}/.tftp"
TFTP_PORT=6969
# Must match FLASH_BOOT_ADDRESS in gateware/colorlight.py (spiflash origin + 0x100000)
FLASH_BOOT_OFFSET="0x100000"
# Receiver card. "e320" selects the HUB320 connector table (eight 26-pin
# ports, twelve data lines each) measured in gateware/colorlight_e320.py.
# Required by any --panel with four RGB groups.
BOARD="${BOARD:-5a-75e}"
ALLOW_STALE=0
HOST_IP=""
# Panel config compiled into the firmware, so a board needs no network to know
# what it is driving. Same format as the TFTP <mac>.yml -- same parser at boot.
DEFAULT_CONFIG=""
# Where marquee serves panel firmware from. The build produces .tftp/boot.bin;
# marquee serves /data/firmware/boot.bin. Nothing connected the two until the
# `deploy` target, so "why didn't my change take effect" was usually a forgotten
# copy -- marquee kept serving the PREVIOUS firmware.
MARQUEE_FW="${MARQUEE_FW:-/srv/docker/marquee/data/firmware}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# =============================================================================
# Helper Functions
# =============================================================================

print_header() {
    echo -e "${BLUE}════════════════════════════════════════════════════════════════${NC}"
    echo -e "${BLUE}  $1${NC}"
    echo -e "${BLUE}════════════════════════════════════════════════════════════════${NC}"
}

print_step() {
    echo -e "${GREEN}▶ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✖ $1${NC}"
}

print_success() {
    echo -e "${GREEN}✔ $1${NC}"
}

check_docker() {
    if ! command -v docker &> /dev/null; then
        print_error "Docker is not installed or not in PATH"
        exit 1
    fi

    if ! docker info &> /dev/null; then
        print_error "Docker daemon is not running or you don't have permission"
        exit 1
    fi
}

check_docker_image() {
    if ! docker image inspect ${DOCKER_IMAGE} &> /dev/null; then
        print_warning "Docker image '${DOCKER_IMAGE}' not found"
        echo "Run './build.sh docker' to build it first"
        exit 1
    fi

    # Guard the picolibc drift. litex_setup.py --tag pins LiteX's own repos but
    # NOT pythondata-software-picolibc, so an image rebuilt at the wrong moment
    # gets a picolibc without newlib/libc/tinystdio -- which LiteX 2025.12
    # hardcodes in its include path. GCC ignores a -I that does not exist, so
    # libc/stdio.c falls through to the toolchain's NEWLIB headers and dies on
    # '_FDEV_SETUP_RW undeclared'.
    #
    # It hides until something forces a software rebuild, i.e. any SoC change,
    # which is exactly when you are least expecting a toolchain failure. Ask the
    # image, not a log, and say what to do about it.
    if ! docker run --rm ${DOCKER_IMAGE} test -f \
        /litex/pythondata-software-picolibc/pythondata_software_picolibc/data/newlib/libc/tinystdio/stdio.h \
        2>/dev/null; then
        print_error "Image '${DOCKER_IMAGE}' has a picolibc LiteX 2025.12 cannot build against"
        echo "  newlib/libc/tinystdio is missing, so the BIOS build will fail with"
        echo "  \"'_FDEV_SETUP_RW' undeclared\" the moment the SoC config changes."
        echo "  The Dockerfile pins the working revision -- rebuild: ./build.sh docker"
        exit 1
    fi
}

docker_run() {
    docker run --rm -v "${SCRIPT_DIR}:/project" -e CARGO_HOME=/project/.cargo-cache ${DOCKER_IMAGE} bash -c "$@"
}

docker_run_usb() {
    docker run --rm -v "${SCRIPT_DIR}:/project" -v /dev/bus/usb:/dev/bus/usb --privileged ${DOCKER_IMAGE} bash -c "$@"
}

# =============================================================================
# JTAG tools -- FROM THE TREE, never from PATH
# =============================================================================
# Three openFPGALoader builds existed on the flash host at once and the NEWEST
# was the one nothing invoked:
#
#   .ofl/bin/openFPGALoader   v1.1.1  (latest release, carried here) -- unused
#   /usr/local/bin/...        v1.0.0  what a bare `openFPGALoader` picked up
#   inside ${DOCKER_IMAGE}    v0.11.0 what these targets used to run
#
# So "which version wrote this flash?" had three different answers depending on
# how you invoked it, and v0.11.0 is the one that does not recognise the
# GD25Q32 and leaves SR1 = 0x1c behind. Same trap as the stale bitstream, the
# stale boot.bin and the 210-day-old TFTP daemons: something plausible served
# silently.
#
# These are absolute paths into the checkout. A bare tool name must never
# appear in this file again -- if the tree copy is missing we stop, rather than
# quietly using whatever the host happens to have.
OFL="${SCRIPT_DIR}/.ofl/bin/openFPGALoader"
ECPPROG="${SCRIPT_DIR}/.ecpprog/bin/ecpprog"

require_tool() {
    local tool="$1" name="$2" hint="$3"
    if [[ ! -x "${tool}" ]]; then
        print_error "${name} not found in the tree at ${tool#${SCRIPT_DIR}/}"
        echo "  This repo carries its own ${name} on purpose -- see the comment"
        echo "  above OFL= in build.sh. It is NOT taken from PATH."
        echo "  ${hint}"
        exit 1
    fi
    if ! "${tool}" --Version >/dev/null 2>&1 && ! "${tool}" --help >/dev/null 2>&1; then
        print_error "${tool#${SCRIPT_DIR}/} will not run (missing libftdi1/libusb?)"
        exit 1
    fi
}

require_ofl()     { require_tool "${OFL}"     "openFPGALoader" "Rebuild it, or restore .ofl/ from git."; }
require_ecpprog() { require_tool "${ECPPROG}" "ecpprog"        "Rebuild it, or restore .ecpprog/ from git."; }

# -----------------------------------------------------------------------------
# recover_card -- erase the bitstream region, then configure over JTAG.
#
# THE one thing to try when a card will not configure. Full reasoning and the
# measurements are in docs/FLASHING.md §2; the short version:
#
# A corrupt or partially-written bitstream in flash STOPS the FPGA from being
# configured over JTAG. The ECP5 keeps retrying a master-SPI boot from the bad
# image, that attempt fails (status bit `SPIm Fail1`), and it aborts the JTAG
# configuration you are trying to perform. The symptoms all point elsewhere: a
# dead-looking board, a bitstream that reports `Abort ERR` or `CRC ERR`, and
# flash reads that come back as noise.
#
# Measured 2026-10-04 on the E320: SRAM configuration failed every time across
# three bitstreams, both tools and every clock from 200 kHz to 6 MHz. After the
# erase below, the next SRAM load succeeded on the FIRST attempt. Nothing else
# was changed.
#
# The erase is a tiny JTAG transfer, so it still works when bulk writes are
# marginal -- which is why it comes first.
#
# -p is NOT optional: without it the GD25Q32's write protection silently lets
# only the first few KB of an erase/write through.
recover_card() {
    print_header "Recovering a card that will not configure"
    require_ecpprog
    require_ofl

    local bit
    bit=$(get_bitstream_path)
    if [[ -z "${bit}" || ! -f "${bit}" ]]; then
        print_error "No bitstream for panel ${PANEL}; run './build.sh bitstream' first"
        exit 1
    fi

    # NOTE: never pipe these through tail -- the pipeline's exit status is the
    # tail's, so a failed program would be reported as success. That bug shipped
    # briefly and made a failed configuration look like a working card.
    local log
    log=$(mktemp); trap 'rm -f "${log}"' RETURN

    print_step "Erasing the bitstream region (0x00000..0x80000)"
    print_warning "The card cannot boot standalone until a bitstream is flashed again"
    if ! "${ECPPROG}" -I B -p -e 524288 >"${log}" 2>&1; then
        tail -3 "${log}"
        print_error "Erase failed -- this is a small transfer, so suspect the cable"
        exit 1
    fi
    print_success "Bitstream region erased"

    # Go STRAIGHT from the erase into the configuration. Do NOT insert a
    # `--detect` here: that is the right fix for the ecpprog -> openFPGALoader
    # handoff on a FLASH write (docs/FLASHING.md §5), but before an SRAM
    # configuration it leaves the device in a state that produces `Abort ERR`.
    # Measured both ways, 2026-10-04: erase->configure succeeds, and
    # erase->detect->configure fails.
    print_step "Configuring from ${bit#${SCRIPT_DIR}/} over JTAG (volatile)"
    local ok=0 attempt
    for attempt in 1 2 3; do
        if "${OFL}" --cable "${CABLE}" "${bit}" >"${log}" 2>&1; then ok=1; break; fi
        print_warning "Attempt ${attempt}/3 failed"
        (( attempt < 3 )) && "${ECPPROG}" -I B -p -e 524288 >/dev/null 2>&1
    done
    if (( ok )); then
        tr '\r' '\n' < "${log}" | grep -E "Disable configuration" | tail -1
        print_success "FPGA configured -- the card should now netboot from marquee"
        echo "  Watch it:  docker compose logs -f player | grep tftp   (on the marquee host)"
        echo "  This is VOLATILE. Persist it with './build.sh flash-all' once it boots."
    else
        tr '\r' '\n' < "${log}" | grep -vE "^\s*$" | tail -12
        print_error "Configuration still failed after erasing flash"
        echo "  That rules out docs/FLASHING.md §2, which is the usual cause."
        echo "  Do NOT start sweeping --freq or power-cycling; read FLASHING.md §6"
        echo "  for what has already been excluded, then §7 for what has not."
        exit 1
    fi
}

# =============================================================================
# Build Targets
# =============================================================================

build_docker() {
    print_header "Building Docker Image"
    cd "${SCRIPT_DIR}"
    docker build -t ${DOCKER_IMAGE} .
    print_success "Docker image '${DOCKER_IMAGE}' built successfully"
}

build_bitstream() {
    print_header "Building FPGA Bitstream"
    check_docker_image

    # The framebuffer geometry lives in gateware/hub75.py and is mirrored in the
    # firmware. If the two disagree, the CPU writes pixels to one half of SDRAM
    # while the scan-out reads the other -- and with the hardware buffer swap the
    # display and the pixel DMA can even disagree with each other.
    local gw_fb_base gw_fb_half fw_fb_base fw_fb_half
    gw_fb_base=$(sed -n 's/^FB_BASE_BYTES = \(0x[0-9A-Fa-f]*\)/\1/p' "${SCRIPT_DIR}/gateware/hub75.py")
    gw_fb_half=$(sed -n 's/^FB_HALF_BYTES = \(0x[0-9A-Fa-f]*\)/\1/p' "${SCRIPT_DIR}/gateware/hub75.py")
    fw_fb_base=$(sed -n 's/^const FB_SDRAM_OFFSET_BYTES: u32 = \(0x[0-9A-Fa-f]*\);.*/\1/p' \
        "${SCRIPT_DIR}/${FIRMWARE_DIR}/src/hub75.rs")
    fw_fb_half=$(sed -n 's/^const FB_TOTAL_WORDS: usize = 2 \* (\(0x[0-9A-Fa-f]*\) \/ 4);.*/\1/p' \
        "${SCRIPT_DIR}/${FIRMWARE_DIR}/src/hub75.rs")
    if [[ "${gw_fb_base,,}" != "${fw_fb_base,,}" || "${gw_fb_half,,}" != "${fw_fb_half,,}" ]]; then
        print_error "Framebuffer geometry mismatch between gateware and firmware"
        echo "  gateware/hub75.py     base=${gw_fb_base} half=${gw_fb_half}"
        echo "  ${FIRMWARE_DIR}/src/hub75.rs  base=${fw_fb_base} half=${fw_fb_half}"
        exit 1
    fi

    # The MAC's RX slot count is an ADDRESS, not a tuning knob: TX buffers sit
    # immediately after the RX buffers, so a firmware built against the wrong
    # count writes its outgoing frames off the end of the region. The panel then
    # boots, runs, and is completely mute -- and the BIOS still netboots, because
    # it has its own driver, which makes it look like a firmware hang.
    local gw_slots fw_slots
    gw_slots=$(sed -n 's/^ *nrxslots=\([0-9]*\),.*/\1/p' "${SCRIPT_DIR}/gateware/colorlight.py" | head -1)
    fw_slots=$(sed -n 's/^const NRXSLOTS: usize = \([0-9]*\);.*/\1/p' \
        "${SCRIPT_DIR}/${FIRMWARE_DIR}/src/ethernet.rs")
    if [[ -n "${gw_slots}" && -n "${fw_slots}" && "${gw_slots}" != "${fw_slots}" ]]; then
        print_error "MAC RX slot mismatch: gateware ${gw_slots}, firmware ${fw_slots}"
        echo "  TX buffers are located at (NRXSLOTS + slot) * 2048, so a mismatch"
        echo "  makes the panel boot and go silent. Set both the same:"
        echo "    gateware/colorlight.py   nrxslots=${gw_slots}"
        echo "    ${FIRMWARE_DIR}/src/ethernet.rs   const NRXSLOTS = ${gw_slots}"
        exit 1
    fi

    # The firmware's chain length is a compile-time constant; a gateware built
    # with a different one wires up panel slots the firmware will never address
    # (or addresses slots that do not exist). It boots and looks fine.
    local fw_chain
    fw_chain=$(sed -n 's/^const CHAIN_LENGTH: u8 = \([0-9]*\);.*/\1/p' \
        "${SCRIPT_DIR}/${FIRMWARE_DIR}/src/hub75.rs")
    if [[ -z "${fw_chain}" ]]; then
        print_error "Could not read CHAIN_LENGTH from ${FIRMWARE_DIR}/src/hub75.rs"
        exit 1
    fi
    if [[ "${fw_chain}" != "${CHAIN_LENGTH}" ]]; then
        print_error "Chain length mismatch: gateware ${CHAIN_LENGTH}, firmware ${fw_chain}"
        echo "  The firmware bakes CHAIN_LENGTH in at compile time. Build them to match:"
        echo "    ./build.sh --chain ${fw_chain} bitstream"
        echo "  or change const CHAIN_LENGTH in ${FIRMWARE_DIR}/src/hub75.rs."
        exit 1
    fi

    select_cargo_features
    local SYSCLK_FLAG=""
    [[ -n "${SYS_CLK}" ]] && SYSCLK_FLAG="--sys-clk-freq ${SYS_CLK}"
    local STRICT_FLAG=""
    [[ "${TIMING_STRICT}" == "1" ]] && STRICT_FLAG="--nextpnr-timingstrict"
    local COMPRESS_FLAG=""
    [[ "${COMPRESS}" == "1" ]] && COMPRESS_FLAG="--ecppack-compress"
    print_step "Running LiteX build (revision ${REVISION}, IP ${IP_ADDRESS}, panel ${PANEL}, chain ${CHAIN_LENGTH})"
    # --ecppack-compress is NOT the default, despite what LiteX's own function
    # signature suggests. `trellis.py` declares the CLI flag with
    # action="store_true", so trellis_argdict() passes compress=False unless it
    # is given, overriding the `compress = True` default in build().
    #
    # It matters at POWER-ON. An uncompressed image is ~712KB, and the ECP5
    # clocks configuration out of SPI flash at its default 2.4 MHz MCLK -- about
    # 2.4 seconds during which the rail must stay good, at exactly the moment a
    # wall of LED panels is drawing inrush. Compression roughly halves both the
    # image and that window.
    docker_run "./gateware/colorlight.py --revision ${REVISION} --board ${BOARD} --ip-address ${IP_ADDRESS} --tftp-server ${TFTP_SERVER} --panel ${PANEL} --outputs ${OUTPUTS} --chain-length ${CHAIN_LENGTH} ${COMPRESS_FLAG} --eth-phy ${ETH_PHY} --nextpnr-seed ${SEED} ${STRICT_FLAG} ${SYSCLK_FLAG} --build"

    if [[ -f "${SCRIPT_DIR}/${BITSTREAM}" ]]; then
        print_success "Bitstream built: ${BITSTREAM}"
        # Copy to bitstreams/ for multi-panel support
        local cache
        cache=$(bitstream_cache_path)
        mkdir -p "$(dirname "${cache}")"
        cp "${SCRIPT_DIR}/${BITSTREAM}" "${cache}"
        print_step "Copied to ${cache#${SCRIPT_DIR}/}"

        # Say what to call it when pushing it to a card.
        #
        # The cache path is keyed by PANEL, because that is what makes two builds
        # reusable here. marquee needs the opposite: the name a card will REPORT,
        # so its fleet view can compare "running" against "pushed" and tell
        # "already live" from "written, awaiting a power cycle". That name is the
        # bitstream's identity -- board plus gateware/VERSION -- and marquee
        # refuses any other shape. Printing it removes the guesswork that produced
        # files called "app-e320-128x64.bit".
        local bs_ver
        bs_ver=$(tr -d ' \n' < "${SCRIPT_DIR}/gateware/VERSION" 2>/dev/null || echo "0.0.0")
        # Must match marquee's SAFE_NAME exactly: board, panel and output count
        # are all compiled into the bitstream, so all three are part of its
        # identity -- and the name has to equal what the card will report running,
        # or the fleet view compares a filename against an identity.
        local upload_name="${BOARD}-${PANEL}-out${OUTPUTS:-6}-v${bs_ver}.bit"
        echo
        print_step "Push it with this name (marquee requires it):"
        echo "      ${upload_name}"
        echo "      curl -F file=@${cache#${SCRIPT_DIR}/};filename=${upload_name} \\"
        echo "           http://marquee/api/v1/bitstreams"
    else
        print_error "Bitstream not found at expected location"
        exit 1
    fi
}

build_all_panels() {
    print_header "Building All Panel Bitstreams"
    check_docker_image
    mkdir -p "${SCRIPT_DIR}/bitstreams"

    for panel in 256x64 128x64 96x48 64x32 64x64; do
        echo ""
        print_step "Building bitstream for ${panel}..."
        docker_run "./gateware/colorlight.py --revision ${REVISION} --board ${BOARD} --ip-address ${IP_ADDRESS} --tftp-server ${TFTP_SERVER} --panel ${panel} --outputs ${OUTPUTS} --chain-length ${CHAIN_LENGTH} --build"
        if [[ -f "${SCRIPT_DIR}/${BITSTREAM}" ]]; then
            local cache
            cache=$(bitstream_cache_path "${panel}")
            mkdir -p "$(dirname "${cache}")"
            cp "${SCRIPT_DIR}/${BITSTREAM}" "${cache}"
            print_success "${cache#${SCRIPT_DIR}/}"
        else
            print_error "Build failed for ${panel}"
            exit 1
        fi
    done

    # Firmware is universal — build once (PAC/CSRs are identical for all panels)
    echo ""
    build_firmware

    echo ""
    print_success "All panel bitstreams built:"
    ls -lh "${SCRIPT_DIR}/bitstreams/"
}

# Where a built bitstream is cached for reuse. MUST be board-qualified: the
# cache used to be `bitstreams/<panel>.bit` for every board, so building panel
# 128x64 for the E320 silently overwrote the 5A-75E's bitstream of the same
# name -- and get_bitstream_path() then handed that E320 image to a 5A-75E
# build. A wrong-board bitstream configures and does nothing useful, which is
# the stale-artifact failure docs/HARDWARE.md §12 is about.
#
# The default board keeps the flat path so existing files and docs still work.
bitstream_cache_path() {
    local panel="${1:-${PANEL}}"
    if [[ "${BOARD}" == "5a-75e" ]]; then
        echo "${SCRIPT_DIR}/bitstreams/${panel}.bit"
    else
        echo "${SCRIPT_DIR}/bitstreams/${BOARD}/${panel}.bit"
    fi
}

get_bitstream_path() {
    local panel_bit
    panel_bit=$(bitstream_cache_path)
    if [[ -f "${panel_bit}" ]]; then
        echo "${panel_bit}"
    elif [[ -f "${SCRIPT_DIR}/${BITSTREAM}" ]]; then
        echo "${SCRIPT_DIR}/${BITSTREAM}"
    else
        echo ""
    fi
}

run_panelc() {
    print_header "Compiling Panel Programs (panelc)"
    check_docker_image

    # Stock examples plus everything under custom/. A user's own programs live
    # in custom/<name>/panels/ and are picked up without editing this script --
    # which is the point: the maintainers' own site programs are found by
    # exactly the same glob, so the path is exercised on every build.
    local yamls
    yamls=$(cd "${SCRIPT_DIR}" && ls panels/*.yaml custom/*/panels/*.yaml 2>/dev/null | tr '\n' ' ')
    if [[ -z "${yamls}" ]]; then
        print_error "No panel programs found in panels/ or custom/*/panels/"
        exit 1
    fi
    print_step "Compiling: ${yamls}"
    docker_run "python3 /project/tools/panelc.py ${yamls}"
    print_success "Generated src/assets.rs and src/generated.rs"
    print_warning "Both are GENERATED -- edit the YAML, never those files"
}

build_firmware() {
    # The system clock is BAKED IN from the gateware, not compared against it.
    #
    # It used to be a constant in network.rs with a guard here that refused a
    # mismatch. The guard was right about the danger -- Timer0's reload, every
    # millisecond wait and the DHCP timeouts all derive from this, so a firmware
    # carrying 40 MHz on 35 MHz gateware boots, runs, and fails at anything
    # time-dependent, which looks exactly like a dead network stack. But it made
    # the clock a property of the SOURCE, so building for a second board meant
    # editing a file, and only one variant could exist at a time.
    #
    # build.rs now reads CONFIG_CLOCK_FREQUENCY out of the gateware's own soc.h,
    # so the two cannot disagree and every variant builds from one tree.
    local soc_h="${SCRIPT_DIR}/${BUILD_DIR}/software/include/generated/soc.h"
    local clk_env=""
    if [[ -f "${soc_h}" ]]; then
        clk_env="-e BARSIGN_SOC_H=/project/${BUILD_DIR}/software/include/generated/soc.h"
        print_step "System clock from ${BUILD_DIR}/.../soc.h: $(sed -n 's/^#define CONFIG_CLOCK_FREQUENCY \([0-9]*\).*/\1/p' "${soc_h}" | head -1) Hz"
    else
        print_warning "No soc.h for ${BOARD} -- build the bitstream first, or the"
        print_warning "firmware falls back to 40 MHz (the 5A-75E's clock)."
    fi

    # img.rs does `include_bytes!("../../../img_data.bin")`, and that file is
    # GENERATED and gitignored -- so a clean clone cannot compile the firmware, and
    # neither could the published repo. Generate it when missing.
    #
    # Found by the publish build gate, which exists precisely to catch "works on
    # the machine that has the untracked file". The content is a test pattern, so
    # regenerating it is not a behaviour change; what matters is that the
    # dependency is satisfied by the build rather than by someone's history.
    if [[ ! -f "${SCRIPT_DIR}/img_data.bin" ]]; then
        print_step "Generating img_data.bin (gitignored, required by img.rs)"
        docker_run "python3 /project/gateware/gen_test_image.py --panel ${PANEL} \
                    --pattern grid --output /project/img_data.bin" \
            || { print_error "could not generate img_data.bin"; exit 1; }
    fi

    # S-PWM panels need firmware support compiled in; plain HUB75 ones must not
    # have it. Selected from --panel, here as well as in build_bitstream(), because
    # firmware is built by its own target too.
    select_cargo_features
    [[ -n "${CARGO_FEATURES}" ]] && print_step "Firmware features: ${CARGO_FEATURES}"

    # A panel config baked in at build time, so a board needs no network to know
    # what it is driving. Same format the panel fetches over TFTP, parsed by the
    # same LayoutConfig::parse(), so there is one format and one parser.
    local cfg_env=""
    if [[ -n "${DEFAULT_CONFIG}" ]]; then
        if [[ ! -f "${DEFAULT_CONFIG}" ]]; then
            print_error "--default-config: no such file: ${DEFAULT_CONFIG}"
            exit 1
        fi
        # Resolve to a /project path so the build container can read it, and so
        # build.rs's rerun-if-changed points at something that exists there.
        local abs_cfg="$(cd "$(dirname "${DEFAULT_CONFIG}")" && pwd)/$(basename "${DEFAULT_CONFIG}")"
        local rel_cfg="${abs_cfg#${SCRIPT_DIR}/}"
        if [[ "${rel_cfg}" == "${abs_cfg}" ]]; then
            print_error "--default-config must be inside the repo: ${DEFAULT_CONFIG}"
            exit 1
        fi
        print_step "Baking default panel config: ${rel_cfg}"
        sed 's/^/      /' "${abs_cfg}"
        cfg_env="-e BARSIGN_DEFAULT_CONFIG=/project/${rel_cfg}"
    fi

    docker run --rm -v "${SCRIPT_DIR}:/project" -e CARGO_HOME=/project/.cargo-cache ${cfg_env} ${clk_env} \
        ${FIRMWARE_IMAGE} bash -c "cd /project/${FIRMWARE_DIR} && cargo build --release ${CARGO_FEATURES}"

    if [[ -f "${SCRIPT_DIR}/${FIRMWARE_BIN}" ]]; then
        print_success "Firmware built: ${FIRMWARE_BIN}"

        # Show firmware size
        SIZE=$(ls -lh "${SCRIPT_DIR}/${FIRMWARE_BIN}" | awk '{print $5}')
        echo "    Size: ${SIZE}"

        # Convert and copy to TFTP directory
        mkdir -p "${TFTP_DIR}"
        print_step "Converting ELF to raw binary for TFTP"
        docker run --rm -v "${SCRIPT_DIR}:/project" ${FIRMWARE_IMAGE} bash -c \
            "riscv-none-elf-objcopy -O binary /project/${FIRMWARE_BIN} /project/.tftp/boot.bin"
        local binsize=$(ls -lh "${TFTP_DIR}/boot.bin" | awk '{print $5}')
        print_success "TFTP ready: .tftp/boot.bin (${binsize})"
    else
        print_error "Firmware binary not found at expected location"
        exit 1
    fi
}

build_pac() {
    print_header "Regenerating Peripheral Access Crate (PAC)"
    check_docker_image

    print_step "Running svd2rust and form"
    docker_run "set -e && cd /project/sw_rust/litex-pac && \
        svd2rust -i colorlight.svd --target riscv && \
        rm -rf src && \
        form -i lib.rs -o src && \
        rm lib.rs && \
        echo 'PAC files generated:' && ls src/"

    print_success "PAC regenerated in sw_rust/litex-pac/src/"
}

# Refuse to program a bitstream older than the gateware that defines it.
#
# get_bitstream_path() only checks that the file EXISTS, so a failed or skipped
# build leaves the previous bitstream in place and 'sram'/'flash' will load it
# without complaint -- which silently tests, and can persist, the wrong SoC.
check_bitstream_fresh() {
    local bit="$1"
    local newer
    newer=$(find "${SCRIPT_DIR}/gateware" -name '*.py' -newer "${bit}" -print -quit 2>/dev/null)
    if [[ -n "${newer}" ]]; then
        if [[ "${ALLOW_STALE}" == "1" ]]; then
            print_warning "Bitstream is STALE (older than ${newer#${SCRIPT_DIR}/}) - continuing due to --allow-stale"
            return 0
        fi
        print_error "Bitstream is older than the gateware that defines it"
        print_error "  bitstream: ${bit#${SCRIPT_DIR}/}"
        print_error "  newer:     ${newer#${SCRIPT_DIR}/}"
        print_warning "Run './build.sh bitstream' first (or pass --allow-stale to override)"
        exit 1
    fi
}

# Refuse to serve or flash a boot.bin older than the firmware sources.
#
# Same failure mode as a stale bitstream, and it has bitten this project: a
# TFTP daemon spent 210 days serving a months-old boot.bin, so the board booted
# firmware nobody had built recently while the logs looked entirely normal.
# Nothing about a stale binary announces itself -- it just quietly serves.
check_firmware_fresh() {
    local bin="${TFTP_DIR}/boot.bin"
    [[ -f "${bin}" ]] || return 0
    local newer
    newer=$(find "${SCRIPT_DIR}/sw_rust/barsign_disp/src" \
                 "${SCRIPT_DIR}/sw_rust/barsign_disp/Cargo.toml" \
                 -newer "${bin}" -print -quit 2>/dev/null)
    if [[ -n "${newer}" ]]; then
        if [[ "${ALLOW_STALE}" == "1" ]]; then
            print_warning "boot.bin is STALE (older than ${newer#${SCRIPT_DIR}/}) - continuing due to --allow-stale"
            return 0
        fi
        print_error "boot.bin is older than the firmware sources"
        print_error "  binary: .tftp/boot.bin"
        print_error "  newer:  ${newer#${SCRIPT_DIR}/}"
        print_warning "Run './build.sh firmware' first (or pass --allow-stale to override)"
        exit 1
    fi
}

deploy_firmware() {
    print_header "Deploying Firmware to Marquee"

    local src="${TFTP_DIR}/boot.bin"
    if [[ ! -f "${src}" ]]; then
        print_error "No firmware at ${src} -- run './build.sh firmware' first"
        exit 1
    fi
    if [[ ! -d "${MARQUEE_FW}" ]]; then
        print_error "Marquee firmware root not found: ${MARQUEE_FW}"
        print_warning "Pass --marquee-fw <dir>, or run this on the host serving TFTP"
        exit 1
    fi

    local ver
    ver=$(grep -m1 '^version' "${FIRMWARE_DIR}/Cargo.toml" | cut -d'"' -f2)
    local stamped="boot-v${ver}.bin"

    # Keep the outgoing image under its own version name as well as the default,
    # so a panel record can pin one and a rollback is a copy rather than a build.
    if [[ -f "${MARQUEE_FW}/boot.bin" ]]; then
        local prev_md5
        prev_md5=$(md5sum "${MARQUEE_FW}/boot.bin" | cut -c1-8)
        cp -f "${MARQUEE_FW}/boot.bin" "${MARQUEE_FW}/boot-prev-${prev_md5}.bin"
        print_step "Previous kept as boot-prev-${prev_md5}.bin"
    fi

    cp -f "${src}" "${MARQUEE_FW}/${stamped}"
    cp -f "${src}" "${MARQUEE_FW}/boot.bin"
    local size
    size=$(stat -c%s "${src}")
    print_success "Deployed v${ver} (${size} bytes) -> ${MARQUEE_FW}/"
    echo "    boot.bin          served over TFTP at the panel's next boot"
    echo "    ${stamped}   pin this on a panel record for a staged rollout"
    echo
    echo "  Reboot the panel and it runs this: the BIOS tries netboot() FIRST"
    echo "  (patches/litex-bios-netboot-first.patch) and only falls through to"
    echo "  flash when the network cannot answer."
    echo
    print_warning "Flash still holds the OLD firmware -- that is what comes back after a power cut."
    echo "  To make this version survive one:   ./build.sh flash-firmware   (or flash-all)"
}

program_sram() {
    print_header "Programming FPGA (SRAM - Temporary)"
    check_docker_image

    local bit=$(get_bitstream_path)
    if [[ -z "${bit}" ]]; then
        print_error "No bitstream found for panel ${PANEL}"
        print_warning "Run './build.sh bitstream' or './build.sh build-all' first"
        exit 1
    fi

    check_bitstream_fresh "${bit}"

    local rel_bit="${bit#${SCRIPT_DIR}/}"
    print_step "Loading ${rel_bit} to SRAM via ${CABLE} (panel: ${PANEL})"
    print_warning "This is temporary - configuration will be lost on power cycle"

    # Probe JTAG chain first to wake up the TAP state machine
    print_step "Probing JTAG chain..."
    require_ofl
    "${OFL}" --cable "${CABLE}" --detect 2>&1

    local max_attempts=5
    for attempt in $(seq 1 ${max_attempts}); do
        if "${OFL}" --cable "${CABLE}" "${bit}" 2>&1; then
            print_success "Bitstream loaded to SRAM (attempt ${attempt}/${max_attempts})"
            return 0
        fi
        if [[ ${attempt} -lt ${max_attempts} ]]; then
            print_warning "Attempt ${attempt}/${max_attempts} failed, retrying..."
            sleep 2
        fi
    done

    print_error "Failed to program FPGA after ${max_attempts} attempts"
    exit 1
}

program_flash() {
    print_header "Programming FPGA (Flash - Persistent)"
    check_docker_image

    local bit=$(get_bitstream_path)
    if [[ -z "${bit}" ]]; then
        print_error "No bitstream found for panel ${PANEL}"
        print_warning "Run './build.sh bitstream' or './build.sh build-all' first"
        exit 1
    fi

    check_bitstream_fresh "${bit}"

    local rel_bit="${bit#${SCRIPT_DIR}/}"
    print_step "Flashing ${rel_bit} to SPI flash via ${CABLE} (panel: ${PANEL})"
    print_warning "This will persist across power cycles"

    require_ofl
    "${OFL}" --board colorlight --cable "${CABLE}" -f --unprotect-flash "${bit}"

    print_success "Bitstream written to flash"
}

program_flash_firmware() {
    print_header "Programming firmware (Flash - Persistent)"
    check_docker_image

    local bin="${TFTP_DIR}/boot.bin"
    if [[ ! -f "${bin}" ]]; then
        print_error "No firmware binary at .tftp/boot.bin"
        print_warning "Run './build.sh firmware' first"
        exit 1
    fi

    # gateware/colorlight.py sets FLASH_BOOT_ADDRESS to the spiflash region
    # origin + 0x100000, so the BIOS looks for the firmware 1MB into the chip --
    # clear of the bitstream, which lives at offset 0. 'flash' writes only the
    # bitstream; standalone boot needs both, so this writes the other half.
    # --offset is an offset into the FLASH CHIP (0x100000), not the CPU bus
    # address (0x80200000 + 0x100000) that FLASH_BOOT_ADDRESS reports.
    # --file-type stops openFPGALoader inferring a bitstream from the extension.
    # --skip-reset leaves the running design alone: newly written firmware takes
    # effect on the next reset, so pushing firmware on its own does not blank
    # the display mid-write.
    check_firmware_fresh

    # WRAP THE FIRMWARE IN A FLASH BOOT IMAGE (FBI) HEADER FIRST. Do not flash
    # boot.bin raw -- that is a silent no-op that cost two sessions.
    #
    # The BIOS's check_image_in_flash() (litex/soc/software/bios/boot.c) expects
    # at FLASH_BOOT_ADDRESS:
    #
    #     +0   uint32  length of the image
    #     +4   uint32  crc32 of the image
    #     +8   the image itself
    #
    # A raw boot.bin has no header, so the BIOS reads the firmware's FIRST
    # RISC-V INSTRUCTION as the length. Ours is 0x400000b7, i.e. 1 GB, which
    # fails the "32 <= length <= 16 MiB" test, so flashboot() prints
    # "Error: Invalid image length" and falls through to netboot() every single
    # time. That is why this board never booted standalone: from 2026-09-06,
    # when flash-firmware was added, it always wrote the raw file.
    #
    # And netboot() cannot cover for it at power-on: it makes ONE attempt with
    # no link wait and no retry, while the Ethernet PHY is still negotiating, so
    # nothing is even transmitted. After a JTAG REFRESH the PHY has had link for
    # minutes and netboot works -- which is the whole "boots on REFRESH, never
    # on power-on" asymmetry, and it was never a configuration problem.
    #
    # crcfbigen.py is LiteX's own tool for this. -f writes the FBI layout, -l
    # writes the fields little-endian, which is what MMPTR reads on this RISC-V.
    print_step "Wrapping boot.bin in an FBI header (length + crc32)"
    docker_run "python3 /litex/litex/litex/soc/software/crcfbigen.py -f -l /project/.tftp/boot.bin -o /project/.tftp/boot.fbi"

    if [[ ! -f "${TFTP_DIR}/boot.fbi" ]]; then
        print_error "crcfbigen.py produced no .tftp/boot.fbi"
        exit 1
    fi

    # Verify the header the way the BIOS will, before committing it to flash.
    if ! python3 - "${TFTP_DIR}/boot.bin" "${TFTP_DIR}/boot.fbi" <<'PYFBI'
import binascii, struct, sys
raw = open(sys.argv[1], "rb").read()
fbi = open(sys.argv[2], "rb").read()
length, crc = struct.unpack("<II", fbi[:8])
ok = True
if not (32 <= length <= 16 * 1024 * 1024):
    print("  FBI length 0x%08x fails the BIOS range test" % length); ok = False
if length != len(raw):
    print("  FBI length %d != boot.bin %d" % (length, len(raw))); ok = False
if crc != (binascii.crc32(raw) & 0xFFFFFFFF):
    print("  FBI crc32 mismatch"); ok = False
if fbi[8:] != raw:
    print("  FBI payload does not match boot.bin"); ok = False
print("  length=%d crc32=0x%08x" % (length, crc))
sys.exit(0 if ok else 1)
PYFBI
    then
        print_error "FBI header is invalid -- the BIOS would reject this image"
        exit 1
    fi
    print_success "FBI header verified"

    print_step "Flashing .tftp/boot.fbi to SPI flash at chip offset ${FLASH_BOOT_OFFSET}"
    print_warning "This will persist across power cycles"

    require_ofl
    # NO --verify. It reads back exactly the image's length (~304 KB), and
    # openFPGALoader flash reads of that size are unreliable on this bench --
    # so it reports a false failure on a perfectly good write. Three runs
    # "failed" at three different offsets before that was pinned down; the
    # danger is not the wasted time but that a false failure invites
    # re-flashing a chip that was already correct.
    #
    # SHORT reads are the unreliable ones, which is counter-intuitive.
    # Measured 2026-09-19, same board, same cable, comparing against the file
    # actually written:
    #
    #   >= 400000 bytes   28/28 reads clean   (400000, 524288, 1048576)
    #   <= 310000 bytes    5/17 reads clean   (304584, 304592, 305000, 310000)
    #
    # A re-test of the large sizes after the small ones still gave 8/8, so it
    # is length and not drift. The mechanism is NOT understood -- an earlier
    # "must be a power of two" reading of this data was wrong and is recorded
    # in TODO item 3 so nobody re-derives it.
    #
    # `verify-firmware` below reads long and truncates, which is reliable.
    "${OFL}" --board colorlight --cable "${CABLE}" \
        -f --unprotect-flash --skip-reset \
        --file-type raw --offset "${FLASH_BOOT_OFFSET}" "${TFTP_DIR}/boot.fbi"

    print_success "Firmware written to flash at ${FLASH_BOOT_OFFSET}"
    print_step "Verify it with: ./build.sh verify-firmware"
}

is_tftp_running() {
    if [[ -f "${TFTP_DIR}/tftpd.pid" ]]; then
        local pid=$(cat "${TFTP_DIR}/tftpd.pid" 2>/dev/null)
        if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

# Is ANYTHING listening on the TFTP port, ours or not?
#
# is_tftp_running() only ever knew about the server this tree started, tracked
# through a pid file inside this tree. A server started by another checkout --
# or by a pre-v1.3.0 build.sh, which used dnsmasq instead of tftpd.py -- was
# invisible to both `stop` and `ensure`, so `ensure` would cheerfully start
# another alongside it. Eighteen accumulated on the flash host and ran for seven
# months. A server you cannot see is a server you cannot prove is serving the
# binary you just built, which is the whole stale-artifact trap this project has
# already lost a night to.
tftp_port_busy() {
    # Local Address:Port is field 4 -- field 5 is the PEER address, and matching
    # that finds nothing, ever. `ss -uln` prints five fields per data row even
    # though the header reads as six columns.
    ss -uln 2>/dev/null | grep -qE "^[^ ]+([ ]+[^ ]+){2}[ ]+[^ ]*:${TFTP_PORT}[ ]"
}

# Orphaned TFTP daemons from ANY colorlight checkout, however they were started.
# Matched on the tftp-root path so nothing unrelated on the host is touched.
legacy_tftp_pids() {
    local pid
    for pid in $(pgrep -x dnsmasq 2>/dev/null) $(pgrep -f 'tools/tftpd\.py' 2>/dev/null); do
        [[ "${pid}" == "$$" ]] && continue
        if tr '\0' ' ' < "/proc/${pid}/cmdline" 2>/dev/null | grep -q 'colorlight/\.tftp'; then
            echo "${pid}"
        fi
    done | sort -u
}

stop_tftp() {
    if is_tftp_running; then
        local pid=$(cat "${TFTP_DIR}/tftpd.pid" 2>/dev/null)
        print_step "Stopping TFTP server (PID ${pid})"
        kill "${pid}" 2>/dev/null || true
        rm -f "${TFTP_DIR}/tftpd.pid"
        sleep 1
    fi

    # Reap TFTP daemons from other checkouts / older build.sh versions too.
    # Without this they simply accumulate, one per `boot`, forever.
    local orphans
    orphans=$(legacy_tftp_pids)
    if [[ -n "${orphans}" ]]; then
        print_warning "Orphaned colorlight TFTP daemons: $(echo ${orphans} | tr '\n' ' ')"
        # shellcheck disable=SC2086
        kill ${orphans} 2>/dev/null || sudo -n kill ${orphans} 2>/dev/null || true
        sleep 1
        orphans=$(legacy_tftp_pids)
        if [[ -n "${orphans}" ]]; then
            # shellcheck disable=SC2086
            kill -9 ${orphans} 2>/dev/null || sudo -n kill -9 ${orphans} 2>/dev/null || true
        fi
        print_success "Orphaned TFTP daemons cleared"
    fi
}

detect_host_ip() {
    if [[ -z "${HOST_IP}" ]]; then
        HOST_IP=$(ip -4 addr show | grep -oP '10\.11\.6\.\d+' | head -1)
        if [[ -z "${HOST_IP}" ]]; then
            HOST_IP=$(ip -4 addr show | grep -oP 'inet \K[\d.]+' | grep -v '^127\.' | head -1)
        fi
        if [[ -z "${HOST_IP}" ]]; then
            print_error "Could not auto-detect host IP. Use --host-ip option."
            exit 1
        fi
    fi
}

ensure_tftp() {
    check_firmware_fresh
    # Already running — nothing to do
    if is_tftp_running; then
        local pid=$(cat "${TFTP_DIR}/tftpd.pid" 2>/dev/null)
        print_step "TFTP server already running (PID ${pid})"
        return 0
    fi

    # Not ours, but something owns the port. Starting a second server here is
    # the bug that let daemons pile up: both bind, the kernel hands datagrams to
    # whichever it likes, and the board silently boots whatever the OTHER one is
    # serving. Refuse and say so instead.
    if tftp_port_busy; then
        local orphans holder
        orphans=$(legacy_tftp_pids)
        if [[ -n "${orphans}" ]]; then
            print_error "UDP port ${TFTP_PORT} held by an orphaned colorlight TFTP daemon"
            print_error "  PIDs: $(echo ${orphans} | tr '\n' ' ')"
            print_warning "Run './build.sh stop' to clear it, then retry"
            return 1
        fi
        # Not ours. Most likely Marquee, which now owns panel TFTP on the flash
        # host: /srv/docker/marquee serves per-panel boot.bin and <mac>.yml from
        # its database on this same port, deliberately (the gateware sets
        # TFTP_SERVER_PORT=6969). That is the supported path -- firmware rollout
        # is a fleet operation with a record of what each panel booted, rather
        # than a script serving out of somebody's checkout. Do NOT kill it.
        holder=$(sudo -n ss -ulnp 2>/dev/null | grep ":${TFTP_PORT} " \
                 | grep -oP 'users:\(\("\K[^"]+' | head -1)
        print_warning "UDP port ${TFTP_PORT} is owned by another service${holder:+ (${holder})}"
        print_warning "Marquee serves panel TFTP on this host; publish firmware there"
        print_warning "instead of serving it from this tree. Skipping local TFTP."
        return 1
    fi

    # Need boot.bin to serve
    if [[ ! -f "${TFTP_DIR}/boot.bin" ]]; then
        print_warning "No boot.bin — skipping TFTP server"
        return 1
    fi

    # Check python3 and tftpy are available
    if ! python3 -c "import tftpy" 2>/dev/null; then
        print_warning "python3 tftpy not installed — skipping TFTP server (pip install tftpy)"
        return 1
    fi

    detect_host_ip

    mkdir -p "${TFTP_DIR}"
    print_step "Starting TFTP server on ${HOST_IP}"
    python3 "${SCRIPT_DIR}/tools/tftpd.py" \
        --root "${TFTP_DIR}" --host "0.0.0.0" --port "${TFTP_PORT}" \
        --log "${TFTP_DIR}/tftpd.log" \
        --pid "${TFTP_DIR}/tftpd.pid" &
    disown

    sleep 1
    if is_tftp_running; then
        print_success "TFTP server started (PID $(cat ${TFTP_DIR}/tftpd.pid))"
    else
        print_warning "Could not start TFTP server (run manually: ./build.sh start)"
        return 1
    fi
}

do_boot() {
    print_header "Boot Sequence (SRAM + TFTP)"

    if [[ ! -f "${TFTP_DIR}/boot.bin" ]]; then
        print_error "No boot.bin found. Run './build.sh firmware' first."
        exit 1
    fi

    # Ensure TFTP server is running BEFORE programming SRAM
    ensure_tftp

    # Program SRAM (this triggers the board to request boot.bin)
    echo ""
    program_sram

    # Wait for TFTP transfer
    echo ""
    print_step "Waiting for firmware transfer..."
    sleep 3

    # Check if transfer happened
    if grep -q "boot.bin" "${TFTP_DIR}/tftpd.log" 2>/dev/null; then
        print_success "Firmware transferred successfully"
    else
        print_warning "Transfer not detected in log (may still have worked)"
    fi

    # Show log
    echo ""
    echo -e "${BLUE}TFTP Log:${NC}"
    cat "${TFTP_DIR}/tftpd.log" 2>/dev/null | tail -5

    # Test connectivity
    echo ""
    print_step "Testing connectivity..."
    sleep 2
    if ping -c 1 -W 2 "${IP_ADDRESS}" &>/dev/null; then
        print_success "Board responding at ${IP_ADDRESS}"
        echo ""
        echo -e "${GREEN}══════════════════════════════════════════════════════════════${NC}"
        echo -e "${GREEN}  Ready! Connect with: telnet ${IP_ADDRESS} 23${NC}"
        echo -e "${GREEN}══════════════════════════════════════════════════════════════${NC}"
    else
        print_warning "Board not responding to ping (may need more time)"
    fi

    echo ""
    print_success "Boot complete (TFTP server stays running — './build.sh stop' to stop)"
}

do_stop() {
    print_header "Stopping Services"
    stop_tftp
    print_success "Stopped"
}

do_start() {
    print_header "TFTP Server"
    ensure_tftp
}

build_all() {
    print_header "Building Everything"

    if ! docker image inspect ${DOCKER_IMAGE} &> /dev/null; then
        build_docker
    fi

    build_bitstream
    build_pac       # Always regenerate PAC after bitstream to avoid register mismatch
    build_firmware

    print_success "All builds completed successfully"
}

show_help() {
    cat << 'EOF'
Colorlight HUB75 LED Controller - Build Script

USAGE:
    ./build.sh [OPTIONS] [TARGETS...]

TARGETS:
    docker          Build the Docker build environment
    bitstream       Build FPGA bitstream for current --panel (saved to bitstreams/)
    build-all       Build bitstreams for ALL panel sizes + firmware
    firmware        Build the Rust firmware
    pac             Regenerate the Peripheral Access Crate (after SoC changes)
    sram            Program FPGA via JTAG (temporary, uses --panel to select bitstream)
    flash           Write BITSTREAM to SPI flash via JTAG (persistent, uses --panel)
    recover         A card that will not configure: erase the bitstream region,
                    then configure over JTAG. TRY THIS FIRST -- a corrupt image
                    in flash blocks JTAG configuration and every symptom it
                    causes points somewhere else. docs/FLASHING.md §2.
    verify-firmware Read the flashed firmware back and compare it to boot.fbi
    flash-firmware  Write FIRMWARE (.tftp/boot.bin) to SPI flash at FLASH_BOOT_ADDRESS
    flash-all       Both of the above -- what standalone (no-TFTP) boot needs
    pinwalk         Build + flash the pin walker: every safe FPGA ball driven at
                    its own frequency, so a multimeter in Hz mode names the ball
                    behind any connector pin. For mapping an UNKNOWN card's
                    connectors. Replaces the running gateware; see docs/PINWALK.md

  'sram', 'flash' and 'flash-all' refuse to program a bitstream older than
  gateware/*.py, and anything that serves or flashes .tftp/boot.bin refuses one
  older than sw_rust/barsign_disp/. Pass --allow-stale to override either.
    boot            Combined: program SRAM + ensure TFTP server
    start           Start TFTP server (if not already running)
    stop            Stop the background TFTP server
    all             Build docker (if needed), bitstream, PAC, and firmware

    If no target is specified, 'all' is assumed.

    The TFTP server is started automatically by 'boot'. It stays running
    in the background and is not restarted if already running. Use 'stop'
    to shut it down, or 'start' to launch it manually.

    Firmware is universal — one binary works for all panel sizes.
    Only bitstreams differ. Use 'build-all' to pre-build all panels.

OPTIONS:
    -h, --help              Show this help message
    -B, --board CARD        Receiver card: 5a-75e (default) or e320. 'e320'
                            selects the HUB320 connector table -- eight 26-pin
                            ports of twelve data lines -- measured with
                            ./build.sh pinwalk. Required for any --panel with
                            four RGB groups. See docs/HARDWARE.md section 1b.
    --no-compress           Build the bitstream WITHOUT --ecppack-compress.
                            Try this when a card will not take a bitstream over
                            JTAG: compressed images gave CRC ERR on 9 of 10
                            attempts on this bench, uncompressed worked first
                            try. docs/FLASHING.md.
    -r, --revision REV      Board revision (default: 8.2)
    -i, --ip IP             IP address for firmware (default: 192.168.1.50)
    -c, --cable CABLE       JTAG cable type (default: usb-blaster)
    -p, --panel PANEL       Panel type: 256x64, 128x64, 96x48, 64x32, 64x64 (default: 128x64)
    -o, --outputs N         Number of HUB75 outputs (default: 6)
    -l, --chain-length N    Panels per HUB75 output chain (default: 2)
    -t, --pattern PATTERN   Test pattern: grid, rainbow, solid_white, solid_red,
                            solid_green, solid_blue (default: grid)
    --host-ip IP            Host IP for TFTP server (auto-detected if not set)
    -v, --verbose           Enable verbose output

EXAMPLES:
    # Build everything (docker image if needed, bitstream, firmware)
    ./build.sh

    # Build bitstreams for ALL panel sizes + firmware
    ./build.sh build-all

    # Flash a specific panel's bitstream (no rebuild needed)
    ./build.sh --panel 96x48 flash

    # Build and boot a specific panel
    ./build.sh --panel 64x32 bitstream boot

    # Rebuild firmware only (universal, works with all panels)
    ./build.sh firmware

    # Regenerate PAC after modifying gateware/colorlight.py
    ./build.sh pac firmware

JTAG CABLES:
    usb-blaster     Intel/Altera USB Blaster (default)
    ft2232          FTDI FT2232-based cables, JTAG on channel A
    ft2232_b        FTDI FT2232-based cables, JTAG on channel B
    dirtyjtag       DirtyJTAG open-source probe

    An FT2232 is dual-channel and each channel is a separate cable name. The
    wrong one scans an EMPTY chain rather than reporting a bad cable, which is
    indistinguishable from an unpowered board -- try the other channel before
    suspecting hardware. The bench board on the build host is on channel B.

WORKFLOW:
    1. First time setup:
       ./build.sh docker

    2. Development cycle:
       ./build.sh firmware boot
       # TFTP server auto-starts and stays running
       telnet 192.168.1.50 23

    3. Full rebuild and test:
       ./build.sh all boot

    4. Deploy for standalone boot (no TFTP server needed at power-on):
       ./build.sh flash-all
       # 'flash' alone writes only the bitstream; the BIOS also needs the
       # firmware at FLASH_BOOT_ADDRESS, which is what flash-firmware writes.

    5. After modifying gateware/colorlight.py (SoC changes):
       ./build.sh bitstream pac firmware

    6. Stop TFTP server when done:
       ./build.sh stop

TROUBLESHOOTING:
    - If 'sram' or 'flash' fails with permission errors:
      Ensure your user is in the 'plugdev' group or run with sudo

    - If Docker build fails:
      Check Dockerfile exists and Docker daemon is running

    - If bitstream build fails with timing errors:
      This is usually due to complex logic; may need to reduce features

For more information, see:
    - README.md      Project overview and quick start
    - ARCH.md        Architecture and internals
    - CHANGELOG.md   Version history

EOF
}

# =============================================================================
# Main
# =============================================================================

TARGETS=()
VERBOSE=0

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -h|--help)
            show_help
            exit 0
            ;;
        --sys-clk)
            SYS_CLK="$2"
            shift 2
            ;;
        --seed)
            SEED="$2"
            shift 2
            ;;
        --allow-timing-fail)
            TIMING_STRICT=0
            shift
            ;;
        --eth-phy)
            ETH_PHY="$2"
            shift 2
            ;;
        --no-compress)
            COMPRESS=0
            shift
            ;;
        -r|--revision)
            REVISION="$2"
            shift 2
            ;;
        -i|--ip)
            IP_ADDRESS="$2"
            shift 2
            ;;
        -c|--cable)
            CABLE="$2"
            shift 2
            ;;
        --allow-stale)
            ALLOW_STALE=1
            shift
            ;;
        --host-ip)
            HOST_IP="$2"
            shift 2
            ;;
        --default-config)
            DEFAULT_CONFIG="$2"
            shift 2
            ;;
        --marquee-fw)
            MARQUEE_FW="$2"
            shift 2
            ;;
        -B|--board)
            BOARD="$2"
            shift 2
            ;;
        -p|--panel)
            PANEL="$2"
            shift 2
            ;;
        -o|--outputs)
            OUTPUTS="$2"
            shift 2
            ;;
        -l|--chain-length)
            CHAIN_LENGTH="$2"
            shift 2
            ;;
        -t|--pattern)
            PATTERN="$2"
            shift 2
            ;;
        -v|--verbose)
            VERBOSE=1
            set -x
            shift
            ;;
        -*)
            print_error "Unknown option: $1"
            echo "Run './build.sh --help' for usage"
            exit 1
            ;;
        *)
            TARGETS+=("$1")
            shift
            ;;
    esac
done

# Change to script directory
cd "${SCRIPT_DIR}"

# The build directory and bitstream name follow the PLATFORM, which follows
# --board: LiteX names its output after the platform class, so an e320 build
# lands in build/colorlight_e320/. These were hardcoded to the 5A-75E path
# when --board was added, which meant an e320 build wrote one bitstream and
# every later target read a different, stale one -- the silent-wrong-artifact
# failure this file warns about in three other places.
case "${BOARD}" in
    e320)
        PLATFORM_NAME="colorlight_e320"
        ;;
    5a-75e)
        PLATFORM_NAME="colorlight_5a_75e"
        ;;
    *)
        print_error "Unknown --board '${BOARD}' (expected 5a-75e or e320)"
        exit 1
        ;;
esac
BUILD_DIR="build/${PLATFORM_NAME}"
BITSTREAM="${BUILD_DIR}/gateware/${PLATFORM_NAME}.bit"

# Check Docker is available
check_docker

# Default target is 'all'
if [[ ${#TARGETS[@]} -eq 0 ]]; then
    TARGETS=("all")
fi

# Execute targets in order
for target in "${TARGETS[@]}"; do
    case $target in
        docker)
            build_docker
            ;;
        bitstream|gateware|bit)
            build_bitstream
            ;;
        build-all)
            build_all_panels
            ;;
        panelc|programs)
            run_panelc
            ;;
        firmware|rust|fw)
            build_firmware
            ;;
        deploy)
            deploy_firmware
            ;;
        pac)
            build_pac
            ;;
        recover)
            recover_card
            ;;
        spwm-tables)
            print_header "Regenerating S-PWM chip tables for the firmware"
            check_docker_image
            docker_run "python3 tools/gen_spwm_tables.py"
            ;;
        pinwalk)
            # Build the pin-walker and flash it. This REPLACES the running
            # gateware: the card stops being a panel controller until you
            # reflash it with `./build.sh flash`. It is useless without
            # someone at the bench with a multimeter -- see docs/PINWALK.md.
            print_header "Pin walker -- identify connector pins with a meter"
            check_docker_image
            docker_run "python3 gateware/pinwalk.py --build"
            pw_bit="${SCRIPT_DIR}/build/pinwalk/top.bit"
            [[ -f "${pw_bit}" ]] || { print_error "No ${pw_bit}"; exit 1; }
            require_ofl
            print_step "Flashing the pin walker (this replaces the normal gateware)"
            "${OFL}" --board colorlight --cable "${CABLE}" -f --unprotect-flash "${pw_bit}"
            print_success "Pin walker flashed"
            print_step "Lookup table: build/pinwalk/pinwalk-map.csv"
            print_step "Procedure:    docs/PINWALK.md"
            print_step "To restore:   ./build.sh --panel ${PANEL} flash"
            ;;
        sim-classifier)
            print_header "Simulating the pixel classifier"
            check_docker_image
            docker_run "python3 tools/sim_classifier.py"
            ;;
        sim-icn1065)
            print_header "Simulating the ICN1065 output stage"
            check_docker_image
            docker_run "python3 tools/sim_icn1065.py"
            ;;
        fit-icn1065)
            print_header "Fitting the ICN1065 output stage on the ECP5"
            check_docker_image
            docker_run "python3 tools/fit_icn1065.py --outputs 9 --columns 256 --rows 64"
            ;;
        sram|load)
            program_sram
            ;;
        flash|program)
            program_flash
            ;;
        flash-firmware)
            program_flash_firmware
            ;;
        verify-firmware)
            # Read the firmware region back and compare it against what we
            # wrote. Reads a POWER-OF-TWO length and truncates, because
            # openFPGALoader bit-slips on any other length -- see
            # program_flash_firmware.
            #
            # ONE exact match is proof: a slipped read cannot coincidentally
            # equal the source. So retry rather than concluding a bad flash.
            check_docker_image
            require_ofl
            fbi="${TFTP_DIR}/boot.fbi"
            [[ -f "${fbi}" ]] || { print_error "No ${fbi}; run ./build.sh firmware"; exit 1; }
            sz=$(stat -c%s "${fbi}")
            # Read LONG and truncate. Reads at or near the image's own length
            # bit-slip on this bench (5/17 clean); reads of 400 KB and up are
            # reliable (28/28). 512 KB is comfortably above the threshold.
            pow=524288; while (( pow < sz )); do pow=$(( pow * 2 )); done
            tmp=$(mktemp); trap 'rm -f "${tmp}" "${tmp}.trunc"' EXIT
            ok=0
            for attempt in 1 2 3 4 5; do
                print_step "Read-back attempt ${attempt} (${pow} bytes -- long read, then truncate)"
                "${OFL}" --cable "${CABLE}" --dump-flash \
                    -o "${FLASH_BOOT_OFFSET}" --file-size "${pow}" "${tmp}" >/dev/null 2>&1
                head -c "${sz}" "${tmp}" > "${tmp}.trunc"
                if cmp -s "${tmp}.trunc" "${fbi}"; then
                    print_success "Flash matches boot.fbi exactly (attempt ${attempt})"
                    ok=1; break
                fi
                print_warning "Read differs -- bit slip, retrying"
            done
            (( ok )) || { print_error "No clean read in 5 attempts. Either the flash IS wrong, or the link has degraded -- dump 1MB twice at offset 0 and compare to tell which."; exit 1; }
            ;;
        flash-all)
            # Bitstream FIRST: writing it may erase sectors beyond its own
            # length, which would take the firmware at 0x100000 with it.
            program_flash
            program_flash_firmware
            ;;
        tftp|serve|start)
            do_start
            ;;
        boot)
            do_boot
            ;;
        stop)
            do_stop
            ;;
        all)
            build_all
            ;;
        *)
            print_error "Unknown target: $target"
            echo "Run './build.sh --help' for available targets"
            exit 1
            ;;
    esac
done

echo ""
print_success "Done!"
