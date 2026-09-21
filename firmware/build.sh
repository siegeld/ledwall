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
DOCKER_IMAGE="litex-hub75"
REVISION="8.2"
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
SPWM_PANELS="256x128-icnd1065 256x128-icnd1065-fast 256x128-icnd1065-linear 256x128-dp3364s"
CARGO_FEATURES=""
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

    case " ${SPWM_PANELS} " in
        *" ${PANEL} "*) CARGO_FEATURES="--features spwm" ;;
        *) CARGO_FEATURES="" ;;
    esac
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
    docker_run "./gateware/colorlight.py --revision ${REVISION} --ip-address ${IP_ADDRESS} --tftp-server ${TFTP_SERVER} --panel ${PANEL} --outputs ${OUTPUTS} --chain-length ${CHAIN_LENGTH} --ecppack-compress --build"

    if [[ -f "${SCRIPT_DIR}/${BITSTREAM}" ]]; then
        print_success "Bitstream built: ${BITSTREAM}"
        # Copy to bitstreams/ for multi-panel support
        mkdir -p "${SCRIPT_DIR}/bitstreams"
        cp "${SCRIPT_DIR}/${BITSTREAM}" "${SCRIPT_DIR}/bitstreams/${PANEL}.bit"
        print_step "Copied to bitstreams/${PANEL}.bit"
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
        docker_run "./gateware/colorlight.py --revision ${REVISION} --ip-address ${IP_ADDRESS} --tftp-server ${TFTP_SERVER} --panel ${panel} --outputs ${OUTPUTS} --chain-length ${CHAIN_LENGTH} --build"
        if [[ -f "${SCRIPT_DIR}/${BITSTREAM}" ]]; then
            cp "${SCRIPT_DIR}/${BITSTREAM}" "${SCRIPT_DIR}/bitstreams/${panel}.bit"
            print_success "bitstreams/${panel}.bit"
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

get_bitstream_path() {
    local panel_bit="${SCRIPT_DIR}/bitstreams/${PANEL}.bit"
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
    print_header "Building Rust Firmware"
    check_docker_image

    # Generate test image for current panel
    print_step "Generating test image for ${PANEL} panel (pattern: ${PATTERN})"
    docker_run "python3 /project/gateware/gen_test_image.py --panel ${PANEL} --pattern ${PATTERN} -o /project/img_data.bin"

    print_step "Compiling firmware with cargo"
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
    docker run --rm -v "${SCRIPT_DIR}:/project" -e CARGO_HOME=/project/.cargo-cache ${cfg_env} \
        ${DOCKER_IMAGE} bash -c "cd /project/${FIRMWARE_DIR} && cargo build --release ${CARGO_FEATURES}"

    if [[ -f "${SCRIPT_DIR}/${FIRMWARE_BIN}" ]]; then
        print_success "Firmware built: ${FIRMWARE_BIN}"

        # Show firmware size
        SIZE=$(ls -lh "${SCRIPT_DIR}/${FIRMWARE_BIN}" | awk '{print $5}')
        echo "    Size: ${SIZE}"

        # Convert and copy to TFTP directory
        mkdir -p "${TFTP_DIR}"
        print_step "Converting ELF to raw binary for TFTP"
        docker_run "riscv-none-elf-objcopy -O binary /project/${FIRMWARE_BIN} /project/.tftp/boot.bin"
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
    verify-firmware Read the flashed firmware back and compare it to boot.fbi
    flash-firmware  Write FIRMWARE (.tftp/boot.bin) to SPI flash at FLASH_BOOT_ADDRESS
    flash-all       Both of the above -- what standalone (no-TFTP) boot needs

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
        spwm-tables)
            print_header "Regenerating S-PWM chip tables for the firmware"
            check_docker_image
            docker_run "python3 tools/gen_spwm_tables.py"
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
