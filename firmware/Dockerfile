# LiteX build environment for hub75_colorlight75_stuff
FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# Install base dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    wget \
    curl \
    python3 \
    python3-pip \
    python3-venv \
    libevent-dev \
    libjson-c-dev \
    autoconf \
    flex \
    bison \
    libftdi1-dev \
    libusb-1.0-0-dev \
    pkg-config \
    cmake \
    libboost-all-dev \
    libeigen3-dev \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Install OSS CAD Suite (includes yosys, nextpnr-ecp5, prjtrellis, ecpprog)
RUN cd /opt && \
    wget -q https://github.com/YosysHQ/oss-cad-suite-build/releases/download/2024-02-14/oss-cad-suite-linux-x64-20240214.tgz && \
    tar -xzf oss-cad-suite-linux-x64-20240214.tgz && \
    rm oss-cad-suite-linux-x64-20240214.tgz

ENV PATH="/opt/oss-cad-suite/bin:${PATH}"

# Install RISC-V toolchain from xPack (has proper prefixes)
RUN mkdir -p /opt/xpack-riscv && \
    cd /tmp && \
    wget -q https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack/releases/download/v13.2.0-2/xpack-riscv-none-elf-gcc-13.2.0-2-linux-x64.tar.gz && \
    tar -xzf xpack-riscv-none-elf-gcc-13.2.0-2-linux-x64.tar.gz -C /opt/xpack-riscv --strip-components=1 && \
    rm xpack-riscv-none-elf-gcc-13.2.0-2-linux-x64.tar.gz

ENV PATH="/opt/xpack-riscv/bin:${PATH}"

# Install Python dependencies
RUN pip3 install --no-cache-dir pypng meson ninja

# Install LiteX
WORKDIR /litex
RUN wget -q https://raw.githubusercontent.com/enjoy-digital/litex/2025.12/litex_setup.py && \
    chmod +x litex_setup.py && \
    ./litex_setup.py --init --install --tag=2025.12

# Pin picolibc. --tag=2025.12 pins LiteX's OWN repos but NOT
# pythondata-software-picolibc, which tracks HEAD -- so this image is only
# reproducible by accident, and drifted.
#
# A 2026-06-05 picolibc (data 16ff442da) removed newlib/libc/tinystdio, which
# LiteX 2025.12's soc/software/common.mak hardcodes in INCLUDES. GCC ignores a
# -I that does not exist, so libc/stdio.c silently falls through to the xPack
# toolchain's NEWLIB stdio.h and dies on picolibc-only macros:
#
#   stdio.c:39: error: '_FDEV_SETUP_RW' undeclared here (not in a function)
#
# It stays hidden until something forces a software rebuild -- i.e. any change
# to the SoC config, which is exactly what gateware work does.
#
# a5e1122 (data f165dc22f) is the revision every working bitstream this project
# has ever produced was built against.
RUN cd /litex/pythondata-software-picolibc && \
    git checkout a5e1122 && \
    git submodule update --init --recursive && \
    test -f pythondata_software_picolibc/data/newlib/libc/tinystdio/stdio.h \
      || (echo "picolibc pin is wrong: tinystdio missing" && exit 1)

# Apply local patches to LiteX BIOS
#
# litex-bios-netboot-first: try netboot() BEFORE flashboot().
#
# LiteX's boot_sequence() is serial -> flash -> rom -> sdcard -> sata -> net.
# Flash-first is wrong for this estate: marquee is the fleet's source of truth
# for firmware, and `./build.sh deploy` + reboot is the whole iteration loop. With
# flash first, a panel with good firmware in flash NEVER fetches the deployed
# image, so deploy silently does nothing and you have to re-flash over JTAG to
# test any change.
#
# Net-first gives both properties:
#   developing   marquee reachable, link up  -> netboot wins, deploy works
#   power cut    no server, or PHY has no link yet -> falls through to flash,
#                and the panel comes up standalone anyway
#
# The cold-boot case is not luck: netboot() makes one attempt with no link wait
# (see TODO item 2), so at power-on it fails fast and flashboot catches it. On a
# warm /api/reboot the link is already up and netboot wins. Cost is a couple of
# seconds of TFTP timeout at cold boot before the fall-through.
COPY patches/ /litex/patches/
RUN cd /litex/litex && \
    patch -p1 < /litex/patches/litex-bios-broadcast.patch && \
    patch -p1 < /litex/patches/litex-bios-netboot-first.patch && \
    patch -p1 < /litex/patches/litex-bios-dhcp.patch

# Install Rust for firmware
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && \
    . /root/.cargo/env && \
    rustup target add riscv32i-unknown-none-elf && \
    rustup target add riscv32im-unknown-none-elf && \
    cargo install svd2rust form

ENV PATH="/root/.cargo/bin:${PATH}"

# panelc's asset pipeline: TrueType rasterising (Pillow) and SVG rasterising
# (cairosvg, which needs the cairo shared library). Without these, panelc cannot
# run inside the build image -- which would mean anyone wanting to compile their
# own on-panel program had to install Python packages on their host, breaking
# the one promise this repo makes about its toolchain.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libcairo2 \
        libpango-1.0-0 \
        libgdk-pixbuf-2.0-0 \
        shared-mime-info \
    && rm -rf /var/lib/apt/lists/* \
    && pip3 install --no-cache-dir pillow cairosvg pyyaml

WORKDIR /project

# No ENTRYPOINT - use bash -c in build script for flexibility
CMD ["/bin/bash"]
