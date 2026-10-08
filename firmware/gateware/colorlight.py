#!/usr/bin/env python3

#
# Copyright (c) 2020 Florent Kermarrec <florent@enjoy-digital.fr>, David Sawatzke <david@sawatzke.dev>
# SPDX-License-Identifier: BSD-2-Clause

# Build/Use ----------------------------------------------------------------------------------------
#
# ./colorlite.py --revision=6.1 --build --load
#

import os
import argparse
import sys
import subprocess

from migen import *
from migen.genlib.resetsync import AsyncResetSynchronizer

from litex.build.io import DDROutput

from litex_boards.platforms import colorlight_5a_75e

from litex.build.lattice.trellis import trellis_args, trellis_argdict
from litex.build.lattice.programmer import EcpprogProgrammer

from litex.soc.cores.clock import *
from litex.soc.cores import uart
from litex.soc.integration.soc_core import *
from litex.soc.integration.soc import SoCRegion
from litex.soc.integration.builder import *
from litex.soc.interconnect.wishbone import SRAM, Interface
from litex.soc.interconnect import wishbone
from litex.soc.integration import export
from litedram.frontend.wishbone import LiteDRAMWishbone2Native

from litedram.modules import M12L16161A
from litedram.phy import GENSDRPHY, HalfRateGENSDRPHY

from liteeth.phy.ecp5rgmii import LiteEthPHYRGMII
from liteeth.mac import LiteEthMAC
from liteeth.core.arp import LiteEthARP
from liteeth.core.ip import LiteEthIP
from liteeth.core.icmp import LiteEthICMP
from liteeth.common import *

from litex.build.generic_platform import Subsignal, Pins, Misc, IOStandard

from litespi.modules import GD25Q32
from litespi.opcodes import SpiNorFlashOpCodes as Codes
from litespi.phy.generic import LiteSPIPHY
from litespi import LiteSPI

from smoleth import SmolEth  # Provides MAC access for CPU (telnet, ARP handled in firmware)

import hub75
from dma_writer import SdramWriteTester, Hub75UdpDma

# from artnet2ram import Artnet2RAM  # TODO: Re-add Art-Net hardware support

import helper

# Panel configurations: columns, rows, scan rate (rows per address cycle)
# "256x64" treats two daisy-chained 128x64 panels as a single wide panel
# (alternative to chaining — uses columns=256 with chain_length_2=0, no independent positioning)
# `driver` selects the output stage and defaults to "hub75" -- the bit-plane /
# BCM scan-out in hub75.py, which drives shift-register panels (MBI5124,
# ICN2038S and similar).
#
# "icn1065" selects the S-PWM stage in icn1065_soc.py, for panels whose driver
# IC holds the greyscale itself and generates the PWM. Those are a different
# conversation on the wire, not a faster version of the same one: the chip takes
# a 12-bit word per LED, there are no bit planes, and OE steps a row counter
# inside the chip rather than addressing rows over A-E.
#
# `clk_div` sets the panel shift clock to sys/N. It is a capacity decision: the
# shift clock is what sets the DATA refresh rate, so a wall that cannot be fed
# at 20 MHz must be clocked slower rather than left to drop frames. sys/4 gives
# a nine-module wall 36.8 Hz at a 60% SDRAM efficiency assumption; see
# `shift_divider()` in icn1065.py and docs/BENCHMARKS.md section 4c.
PANELS = {
    "256x64": {"columns": 256, "rows": 64, "scan": 32},
    "128x64": {"columns": 128, "rows": 64, "scan": 32},
    # P1.25 320x160 mm module. 128 rows -> 64 per half -> 1/64 scan.
    # A 3x3 of these is 768x384; a 2x2 is 512x256.
    "256x128": {"columns": 256, "rows": 128, "scan": 64},
    # The same module wired to its real driver. UNTESTED ON HARDWARE -- no
    # ICND1065 panel has been connected to this board.
    "256x128-icnd1065": {"columns": 256, "rows": 128, "scan": 64,
                         "driver": "icn1065", "clk_div": 4},
    # Four of them (512x256) have bandwidth to spare, so they can be clocked
    # twice as fast for twice the data refresh: 73.6 Hz rather than 36.8.
    "256x128-icnd1065-fast": {"columns": 256, "rows": 128, "scan": 64,
                              "driver": "icn1065", "clk_div": 2},
    # Pixel width is chosen AUTOMATICALLY by default: the widest that fits the
    # framebuffer, because colour is free until the memory runs out. This
    # preset forces 16-bit for a wall that would otherwise fit at 32, which is
    # only worth doing to buy refresh rate at the cost of visible banding in
    # bright gradients.
    "256x128-icnd1065-565": {"columns": 256, "rows": 128, "scan": 64,
                             "driver": "icn1065", "clk_div": 2, "pix_bits": 16},
    # The same module with a NovaStar DP3364S driver instead of the Chipone
    # part. Identical gateware -- only the chip table differs, and the CPU can
    # rewrite that at runtime, so this preset only sets the power-on default.
    "256x128-dp3364s": {"columns": 256, "rows": 128, "scan": 64,
                        "driver": "icn1065", "clk_div": 4, "chip": "dp3364s"},
    # Same wall with the gamma units removed, for when timing is tight. The
    # picture is linear 8->12 rather than a 2.8 curve, which crushes the dark
    # end -- a real quality cost, and the first thing to try if a build will
    # not close.
    "256x128-icnd1065-linear": {"columns": 256, "rows": 128, "scan": 64,
                                "driver": "icn1065", "clk_div": 4,
                                "gamma": False},
    # HUB320, four RGB groups per connector -- the E320's native wiring.
    # A P1.25 module is 256x128 at 1/64 scan, which is TWO groups, so a
    # four-group port drives two stacked modules: 256x256 per connector.
    # Needs --board e320, because the twelve data pins per connector only
    # exist in that card's table.
    "256x256-icnd1065-hub320": {"columns": 256, "rows": 256, "scan": 64,
                                "driver": "icn1065", "clk_div": 4,
                                "groups": 4},
    "96x48":  {"columns": 96,  "rows": 48, "scan": 24},
    "64x32":  {"columns": 64,  "rows": 32, "scan": 16},
    "64x64":  {"columns": 64,  "rows": 64, "scan": 32},
}


# CRG ----------------------------------------------------------------------------------------------


def _bitstream_version() -> str:
    """The BITSTREAM's own semver, from `gateware/VERSION`.

    Deliberately not the firmware's version. The two artifacts change at very
    different rates -- the Rust firmware is released constantly, the gateware
    rarely -- so tying this to the repo version made every firmware release look
    like it shipped new FPGA logic. That is a lie an operator acts on.

    See `gateware/VERSION.md` for when to bump it. Falls back to "0.0.0" rather
    than failing a build over a cosmetic string.
    """
    import os
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(here, "VERSION")) as fh:
            v = fh.read().strip()
            return v if v else "0.0.0"
    except OSError:
        return "0.0.0"


def _bitstream_ident(board, panel, n_outputs, sys_clk_freq) -> str:
    """The bitstream's self-description, and its ONLY identity at runtime.

    The firmware reads this back out of the gateware's identifier ROM and serves
    it from `/api/bitstream`, so a wrong string here is a card lying about itself.

    NAME FIRST: `<board>-<panel>-out<n>-v<version>`, e.g.
    `e320-128x64-out6-v1.0.0`. Everything in that name is COMPILED INTO the
    bitstream and changes what the gateware does:

      board      the pinout and connector map
      panel      the shift register width and row count -- baked in, which is why
                 build.sh keys its bitstream cache by panel and says "firmware is
                 universal, only bitstreams differ"
      outputs    how many HUB75/HUB320 connectors are driven
      version    gateware/VERSION, for source changes the above cannot express

    An earlier version of this named only `<board>-v<version>`, on the reasoning
    that panel geometry was "a build variant of the same gateware". That was
    wrong: two bitstreams built `--panel 128x64` and `--panel 256x64` are
    different, non-interchangeable logic, and under that scheme they shared a name.
    A name that cannot distinguish two incompatible images is not an identity.

    The system clock follows as a separate field. It is not in the name because it
    is a property of the board in practice -- the E320 is a -6 part built at
    35 MHz, the 5A-75E at 40 -- and build.sh refuses a firmware whose clock
    disagrees with the gateware's, so a mismatch cannot reach a card silently.
    Change it deliberately and bump the version; `gateware/VERSION.md` says so.

    After the name the fields are space separated in fixed order, because the
    firmware splits on spaces -- a wire format between the gateware build and the
    firmware, not prose. Changing the order means changing the parser with it.

      <board>-<panel>-out<n>-v<version> <clk>MHz   [+ " <date> <time>" from LiteX]
    """
    return (f"{board}-{panel}-out{n_outputs}-v{_bitstream_version()} "
            f"{int(sys_clk_freq) // 1000000}MHz")




class _CRG(Module):
    def __init__(
        self,
        platform,
        sys_clk_freq,
        use_internal_osc=False,
        with_usb_pll=False,
        with_rst=True,
        sdram_rate="1:1",
    ):
        self.rst = Signal()
        self.clock_domains.cd_sys = ClockDomain()
        if sdram_rate == "1:2":
            self.clock_domains.cd_sys2x = ClockDomain()
            self.clock_domains.cd_sys2x_ps = ClockDomain(reset_less=True)
        else:
            self.clock_domains.cd_sys_ps = ClockDomain(reset_less=True)

        # # #

        # Clk / Rst
        if not use_internal_osc:
            clk = platform.request("clk25")
            clk_freq = 25e6
        else:
            clk = Signal()
            div = 5
            self.specials += Instance("OSCG", p_DIV=div, o_OSC=clk)
            clk_freq = 310e6 / div

        rst_n = 1 if not with_rst else platform.request("user_btn_n", 0)

        # PLL
        self.submodules.pll = pll = ECP5PLL()
        self.comb += pll.reset.eq(~rst_n | self.rst)
        pll.register_clkin(clk, clk_freq)
        pll.create_clkout(self.cd_sys, sys_clk_freq)
        if sdram_rate == "1:2":
            pll.create_clkout(self.cd_sys2x, 2 * sys_clk_freq)
            pll.create_clkout(
                self.cd_sys2x_ps, 2 * sys_clk_freq, phase=180
            )  # Idealy 90° but needs to be increased.
        else:
            pll.create_clkout(
                self.cd_sys_ps, sys_clk_freq, phase=180
            )  # Idealy 90° but needs to be increased.

        # SDRAM clock
        sdram_clk = ClockSignal("sys2x_ps" if sdram_rate == "1:2" else "sys_ps")
        self.specials += DDROutput(1, 0, platform.request("sdram_clock"), sdram_clk)


# BaseSoC ------------------------------------------------------------------------------------------


class BaseSoC(SoCCore):
    def __init__(
        self,
        revision,
        eth_phy=0,
        sys_clk_freq=40e6,
        sdram_rate="1:1",
        no_ident_version=False,
        ip_address="192.168.1.50",
        tftp_server="192.168.1.10",
        panel="96x48",
        board="5a-75e",
        n_outputs=6,
        chain_length_2=1,
        **kwargs
    ):
        # The E320 is electrically a 5A-75E -- same die, package, flash and
        # SDRAM -- so its platform inherits all of that and overrides only the
        # connector table. The difference that matters is the panel side:
        # eight 26-pin HUB320 ports of twelve data lines, against sixteen
        # 16-pin HUB75 ports of six. See gateware/colorlight_e320.py.
        if board == "e320":
            import colorlight_e320
            platform = colorlight_e320.Platform(revision=revision)
        else:
            platform = colorlight_5a_75e.Platform(revision=revision)

        # DO NOT re-add `SYSCONFIG MASTER_SPI_PORT=ENABLE`.
        #
        # It was added 2026-09-15 with a comment claiming it let the board
        # "configure ITSELF from SPI flash at power-on". That is a misreading of
        # FPGA-TN-02039. Section 6.1.1 gives two ways to enable the Master SPI
        # port, and they are not interchangeable:
        #
        #   * the CFGMDN[2:0] pins = [0,1,0] -- this is what selects the
        #     configuration port AT POWER-ON, and it is a BOARD strap, not
        #     anything the bitstream can influence;
        #   * bitstream persistence bits -- checked only "when the device
        #     completes configuration and wakes up", and the guide is explicit
        #     that "both the DONE pin and the INITN pin must be high. If not,
        #     then the device is not in user mode. The persistent bits have no
        #     effect when the device is not in user mode."
        #
        # A board that never reaches DONE is never in user mode, so the
        # persistence bits cannot possibly affect the power-on fetch they were
        # added to fix.
        #
        # It is not merely inert. nextpnr warns on every build:
        #   "USRMCLK will not function correctly when MASTER_SPI_PORT is set to
        #    ENABLE."
        # USRMCLK is how the user design drives the flash clock after
        # configuration, which is exactly how the SoC reads flash at runtime.
        # So this bought nothing at power-on and put the runtime path at risk.
        sys_clk_freq = int(sys_clk_freq)
        # SoCCore ----------------------------------------------------------------------------------
        SoCCore.__init__(
            self,
            platform,
            sys_clk_freq,
            cpu_type="vexriscv",
            cpu_variant="lite",  # lite supports external interrupts; minimal does not
            cpu_freq=sys_clk_freq,
            # The bitstream's only self-identity, and the ONLY way a running
            # card can say which gateware it has.
            #
            # It used to be the literal string "LiteX SoC on Colorlight 5A-75E"
            # on every build, so an E320 bitstream claimed to be a 5A-75E and two
            # different panel builds were indistinguishable. The firmware reports
            # this over /api/status, so a wrong string here is a card lying about
            # itself in the fleet view.
            #
            # Unlike the Rust firmware there is no semver for a bitstream: what
            # identifies one is the board, the panel geometry, the output count
            # and the build timestamp that `ident_version` appends.
            ident=_bitstream_ident(board, panel, n_outputs, sys_clk_freq),
            ident_version=True,
            integrated_rom_size=0x10000,
            integrated_ram_size=0x0,
            # Use with `litex_server --uart --uart-port /dev/ttyUSB1`
            uart_name="serial",
            # uart_name="crossover+bridge",
            uart_baudrate=115200,
        )
        # SPI Flash: GigaDevice GD25Q32 (4MB).
        #
        # This board was declared as a Winbond W25Q32JV, but the silicon reports
        # JEDEC `c8 40 16`: c8 is GigaDevice, and 0x4016 decodes to 2^0x16 = 4MB.
        # Both parts are 4MB / 256-byte page / 8 dummy bits and both support
        # READ_1_1_1, which is why reads worked anyway -- flash_id's MAC
        # derivation has always succeeded. Declaring the part that is actually
        # fitted matters before anything WRITES to it (see TODO item 2): erase
        # granularity and status/protection handling are where vendors differ,
        # and `build.sh flash` has never been run.
        #
        # (Older v6.1 boards used a GD25Q16 (2MB) — swap the module for those.)
        flash = GD25Q32(Codes.READ_1_1_1)
        # default_divisor sets the SPI clock: sys_clk / (2 * (divisor + 1)).
        # LiteSPI's own default is 9, which at 40 MHz is 2 MHz -- and the flash
        # region is cached=False with READ_1_1_1, so EVERY access pays a full
        # 8 command + 24 address + 8 data transaction before it returns a byte.
        # Measured on the bench card at divisor 9: 28.5 us per byte, against 14
        # CPU cycles for the same read from DRAM.
        #
        # That is the cold boot. flashboot() crc32s the whole image TWICE --
        # once in check_image_in_flash(), again inside
        # copy_image_from_flash_to_ram() (boot.c:623) -- and then copies it:
        # 17.9 s of a 26 s power-on, about 69% of it. It only appeared in
        # v1.36.0, because before the FBI header the BIOS rejected the image
        # after 4 bytes and never read the rest.
        #
        # 1 -> 10 MHz, a measured 4.01x. /api/flashbench sweeps the divisor at
        # runtime (clk_divisor is a CSRStorage whose reset value is this) and
        # checksums the FULL 292,976-byte image at each: 9, 4, 2, 1 and 0 all
        # return identical data on this board, so 20 MHz is proven to read
        # correctly and 10 MHz ships with 2x margin against it. Margin wins here
        # because the failure mode is a board that will not boot standalone and
        # has to be recovered over JTAG.
        self.submodules.spiflash_phy = LiteSPIPHY(
            pads=platform.request("spiflash"), flash=flash, device=platform.device,
            default_divisor=1,
        )
        self.submodules.spiflash_mmap = LiteSPI(
            phy=self.spiflash_phy,
            mmap_endianness=self.cpu.endianness,
            with_master=True,
        )
        self.add_csr("spiflash_mmap")
        self.add_csr("spiflash_phy")
        # LiteX requires a region's origin to be aligned to its size
        # (SoCRegion.decoder(): `origin & (size_pow2 - 1)` must be 0, else SoCError).
        # The old 0x80200000 origin was chosen for the 2MB GD25Q16 and is NOT 4MB
        # aligned, so it must move with the part: 0x80200000 % 0x400000 == 0x200000.
        # 0x80400000 leaves room for ethmac at 0x80000000 (20KB) and the 4MB flash
        # spans 0x80400000-0x80800000; next region is main_ram_uncached at 0x90000000.
        # FLASH_BOOT_ADDRESS follows the origin, but the CHIP offset it maps to is
        # unchanged at 0x100000, so nothing about flashing the firmware changes.
        spiflash_region = SoCRegion(
            origin=0x80400000,
            size=flash.total_size,
            cached=False,
        )
        self.bus.add_slave(
            name="spiflash", slave=self.spiflash_mmap.bus, region=spiflash_region
        )

        self.add_constant(
            "FLASH_BOOT_ADDRESS", self.bus.regions["spiflash"].origin + 0x100000
        )
        self.add_constant("SPIFLASH_PAGE_SIZE", flash.page_size)

        # Internal Litex spi support, supports flashing & stuff via bios
        # Adapted from `add_spi_flash`
        # self.submodules.spiflash = spiflash = SpiFlash(
        #     pads=self.platform.request("spiflash"),
        #     div=2, with_bitbang=True, dummy=8,
        #     endianness=self.cpu.endianness)
        # spiflash.add_clk_primitive(self.platform.device)
        # spiflash_region = SoCRegion(origin=0x80000000, size=2 * 1024 * 1024)
        # self.bus.add_slave(name="spiflash", slave=spiflash.bus, region=spiflash_region)

        # CRG --------------------------------------------------------------------------------------
        with_rst = False
        # kwargs["uart_name"] not in [
        # "serial",
        # "bridge",
        # ]  # serial_rx shared with user_btn_n.
        self.submodules.crg = _CRG(platform, sys_clk_freq, with_rst=with_rst)

        # SDR SDRAM --------------------------------------------------------------------------------
        sdrphy_cls = HalfRateGENSDRPHY if sdram_rate == "1:2" else GENSDRPHY
        self.submodules.sdrphy = sdrphy_cls(platform.request("sdram"))
        sdram_cls = M12L16161A
        sdram_size = 4 * 1024 * 1024
        self.add_sdram(
            "sdram",
            phy=self.sdrphy,
            module=sdram_cls(sys_clk_freq, sdram_rate),
            origin=self.mem_map["main_ram"],
            size=sdram_size,
            l2_cache_size=8192,
            l2_cache_reverse=False,
            l2_cache_full_memory_we=False,
        )

        # Add special, uncached mirror of sdram
        port = self.sdram.crossbar.get_port()

        wb_sdram = wishbone.Interface()
        self.bus.add_slave(
            "main_ram_uncached",
            wb_sdram,
            SoCRegion(origin=0x90000000, size=sdram_size, cached=False),
        )
        self.submodules.wishbone_bridge = LiteDRAMWishbone2Native(
            wishbone=wb_sdram,
            port=port,
            base_address=self.bus.regions["main_ram_uncached"].origin,
        )

        # Add hub75 connectors
        # Four RGB groups per connector needs the HUB320 pin map; two needs
        # the HUB75 one. Asking for the wrong one raises rather than placing
        # a build that drives the wrong balls.
        groups = PANELS[panel].get("groups", 2)
        if groups == 4 and board != "e320":
            raise ValueError(
                f"--panel {panel} needs four RGB groups, which is HUB320 "
                f"wiring, and only the E320's connector table has twelve data "
                f"pins per connector. Add --board e320.")
        # Release the Ethernet PHY reset on the E320.
        #
        # litex_boards' 5A-75E revision 8.2 block has BOTH `eth` `rst_n`
        # entries commented out, so nothing drives the PHY reset -- fine on a
        # 5A-75E, where it is pulled on the board. On the E320 the card then
        # comes up with its PHY held in reset after a POWER CYCLE: the FPGA
        # configures perfectly, the CPU runs, and the card never emits a single
        # frame. It looks exactly like a dead network stack.
        #
        # That is what happened on 2026-10-04: the card netbooted the previous
        # evening, was power-cycled, and went silent for the rest of the day
        # while every bitstream loaded cleanly. `R6` is the `rst_n` ball in
        # both the 6.0 and (commented) 8.2 blocks and is otherwise unused here,
        # so drive it HIGH and hold it there.
        # DO NOT drive "the pins the factory drives but we do not".
        #
        # That list came from a BASE_TYPE diff of the factory bitstream and it
        # was NOISE -- exactly the trap docs/HARDWARE.md §1b warns about, since
        # Diamond assigns a BASE_TYPE to unused pins too. The two candidates it
        # produced were K12 (unknown, mentioned nowhere in the platform) and
        # M6, which is `eth 1`'s rx_data[3] -- an INPUT driven by the second
        # PHY. Driving it would have put the FPGA in output contention with a
        # PHY. Caught before loading, not before building.
        #
        # R6 (the `rst_n` ball in the 6.0 and commented-out 8.2 blocks) WAS
        # tried on its own: constrained, driven high, configured first attempt,
        # card still silent. So the PHY reset is not the fault either.
        #
        # Use tools/bitstream_pins.py --iologic for evidence; and before
        # driving any recovered pin, check it is not an input somewhere in the
        # platform map.

        if groups == 4:
            platform.add_extension(helper.hub320_conn(platform, n_outputs=n_outputs))
        else:
            platform.add_extension(helper.hub75_conn(platform, n_outputs=n_outputs))
        pins_common = platform.request("hub75_common")
        pins = [platform.request("hub75_data", i) for i in range(n_outputs)]

        # Get panel configuration
        panel_cfg = PANELS[panel]
        # Both stages expose the SAME CSRs under the same `hub75_` prefix, so
        # the firmware, the pixel DMA and the buffer-swap wiring below are
        # identical either way. The attribute keeps its name for exactly that
        # reason: renaming it would rename every CSR and break the Rust side
        # for no benefit.
        if panel_cfg.get("driver") == "icn1065":
            import icn1065_soc
            self.submodules.hub75 = icn1065_soc.Icn1065Display(
                pins_common, pins, self.sdram,
                columns=panel_cfg["columns"],
                rows=panel_cfg["rows"],
                scan=panel_cfg["scan"],
                n_outputs=n_outputs,
                clk_div=panel_cfg.get("clk_div", 4),
                groups=groups,
                gamma=panel_cfg.get("gamma", True),
                chip=panel_cfg.get("chip", "icnd1065"),
                pix_bits=panel_cfg.get("pix_bits", "auto"),
            )
        else:
            self.submodules.hub75 = hub75.Hub75(
                pins_common, pins, self.sdram,
                columns=panel_cfg["columns"],
                rows=panel_cfg["rows"],
                scan=panel_cfg["scan"],
                n_outputs=n_outputs,
                chain_length_2=chain_length_2
            )

        # SDRAM write-path measurement (Tier 2).
        #
        # The display read path measures ~79 MB/s, near the ceiling for a 16-bit
        # SDRAM at 40 MHz. Before building a UDP-fed write DMA it is worth
        # knowing what write bandwidth is actually left and what claiming it
        # costs the refresh rate. This engine answers both directly.
        self.submodules.dmatest = SdramWriteTester(self.sdram)
        self.add_csr("dmatest")

        # Ethernet / Etherbone ---------------------------------------------------------------------
        # Use phy0
        # RGMII PHY configuration for RTL8211 on Colorlight 5A-75E
        # rx_delay=2e-9 is critical for stable operation
        # Revision 8.2 defines TWO PHYs, `eth 0` and `eth 1`, and this SoC has
        # only ever instantiated phy 0. On a 5A-75E that is settled by the
        # silkscreen. On the **E320 the jack-to-PHY mapping is NOT established**
        # -- a cable in the jack wired to the PHY we do not instantiate gives a
        # card that configures perfectly, runs its CPU, drives panels, and never
        # transmits a single frame, because the LiteX BIOS waits for PHY link
        # before it will send DHCP. That is indistinguishable from a dead
        # network stack, and it cost most of 2026-10-04.
        #
        # `--eth-phy` makes it testable without moving the cable.
        self.submodules.ethphy = phy = LiteEthPHYRGMII(
            clock_pads=self.platform.request("eth_clocks", eth_phy),
            pads=self.platform.request("eth", eth_phy),
            tx_delay=0e-9,
            # rx_delay is a property of the PCB, not the PHY (docs/HARDWARE.md
            # §5b): 2 ns is the value measured on the 5A-75E. The E320 is a
            # different board and this value is inherited on faith.
            rx_delay=2e-9,
        )

        # Parse IP address string to integer
        ip_parts = [int(x) for x in ip_address.split(".")]
        eth_ip_address = (ip_parts[0] << 24) | (ip_parts[1] << 16) | (ip_parts[2] << 8) | ip_parts[3]
        eth_mac_address = 0x10e2d5000001

        # MAC for the CPU, plus a hardware UDP receive path (Tier 2).
        #
        # This was self.add_ethernet(). The tap has to sit AFTER LiteEthMACCore,
        # which strips the preamble and checks CRC, and that core is buried
        # inside LiteEthMAC where add_ethernet() gives no access to it. SmolEth
        # builds the core itself, which is exactly why it exists in this repo.
        # The CPU-visible side is unchanged: same wishbone slots, same CSR names,
        # same IRQ.
        ethmac = SmolEth(
            phy=phy,
            udp_port=7000,
            mac_address=eth_mac_address,
            ip_address=eth_ip_address,
            dw=32,
            # 2, not 8. Eight slots existed only to absorb pixel bursts, and
            # the gateware pixel filter (RB-5486) means pixel packets never
            # reach this MAC at all. Six freed EBRs are what let a 9-output
            # 256x128 wall fit at all. MUST match NRXSLOTS in ethernet.rs.
            nrxslots=2,
            ntxslots=2,
            with_hw_udp=True,
        )
        self.add_module(name="ethmac", module=ethmac)

        ethmac_rx_region_size = ethmac.rx_slots.constant * ethmac.slot_size.constant
        ethmac_tx_region_size = ethmac.tx_slots.constant * ethmac.slot_size.constant
        self.bus.add_region("ethmac", SoCRegion(
            origin = self.mem_map.get("ethmac", None),
            size   = ethmac_rx_region_size + ethmac_tx_region_size,
            linker = True,
            cached = False,
        ))
        self.bus.add_slave(name="ethmac_rx", slave=ethmac.bus_rx, region=SoCRegion(
            origin = self.bus.regions["ethmac"].origin,
            size   = ethmac_rx_region_size,
            mode   = "r", linker = False, cached = False,
        ))
        self.bus.add_slave(name="ethmac_tx", slave=ethmac.bus_tx, region=SoCRegion(
            origin = self.bus.regions["ethmac"].origin + ethmac_rx_region_size,
            size   = ethmac_tx_region_size,
            mode   = "rw", linker = False, cached = False,
        ))
        if self.irq.enabled:
            self.irq.add("ethmac", use_loc_if_exists=True)
        self.add_constant("ETH_PHY_NO_RESET")
        self.add_constant("LOCALIP1", ip_parts[0])
        self.add_constant("LOCALIP2", ip_parts[1])
        self.add_constant("LOCALIP3", ip_parts[2])
        self.add_constant("LOCALIP4", ip_parts[3])
        # REMOTEIP is where the BIOS fetches boot.bin from -- the marquee host.
        # It was a bare literal here, which meant the service's address was
        # welded into every bitstream with nothing naming it: moving marquee to
        # another host silently broke fleet netboot, and the address was a
        # DHCP lease until it was pinned as a reservation.
        #
        # A build flag at least makes it visible and overridable. The real fix
        # is for the BIOS to learn it from DHCP option 66, which is now served
        # per-reservation to the cards -- see RB-5517. Until that lands this
        # constant is what every card actually uses.
        for i, part in enumerate(tftp_server.split(".")):
            self.add_constant(f"REMOTEIP{i+1}", int(part))

        # Pixels off the CPU: hardware writes the streamed payload straight to
        # SDRAM. Defaults to DISABLED so the ethernet restructure above can be
        # validated on its own before hardware writes are switched on.
        # fb_base is the single source of truth for which half is on screen;
        # the DMA derives the other half from it, so one CSR write flips both.
        self.submodules.pixdma = Hub75UdpDma(
            self.sdram, ethmac.udp_source,
            display_base=self.hub75.fb_base_eff,
            fb_base=hub75.sdram_offset,
            half_words=hub75.half_words,
        )
        self.add_csr("pixdma")

        # Close the loop: the DMA says when a frame ended, hub75 flips on it.
        self.comb += self.hub75.swap_req.eq(self.pixdma.swap_req)
        self.comb += self.pixdma.auto_swap.eq(self.hub75.auto_swap.storage)

        # Enables the BIOS's broadcast send/receive path, which the DHCP
        # client in patches/litex-bios-dhcp.patch is built on: a client with
        # no address yet has to send to 255.255.255.255 and be answered by
        # broadcast, and process_udp() only delivers those when this is set.
        # It also gates the per-card MAC derivation in net_init().
        self.add_constant("ETH_UDP_BROADCAST")
        self.add_constant("TFTP_SERVER_PORT", 6969)

        # Timing constraints
        eth_rx_clk = getattr(phy, "crg", phy).cd_eth_rx.clk
        eth_tx_clk = getattr(phy, "crg", phy).cd_eth_tx.clk
        self.platform.add_period_constraint(eth_rx_clk, 1e9 / phy.rx_clk_freq)
        self.platform.add_period_constraint(eth_tx_clk, 1e9 / phy.tx_clk_freq)
        self.platform.add_false_path_constraints(
            self.crg.cd_sys.clk, eth_rx_clk, eth_tx_clk
        )
        ## Reduce bios size
        # Disable memtest, it takes a bit and is thus annoying
        self.add_constant("SDRAM_TEST_DISABLE")


# Build --------------------------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="LiteX SoC on Colorlight 5A-75X")
    builder_args(parser)
    soc_core_args(parser)
    trellis_args(parser)
    parser.add_argument(
        "--board", default="5a-75e", choices=["5a-75e", "e320"],
        help="Receiver card (default: 5a-75e). 'e320' selects the HUB320 "
             "connector table measured in gateware/colorlight_e320.py; it is "
             "required by any --panel with four RGB groups.")
    parser.add_argument("--build", action="store_true", help="Build bitstream")
    parser.add_argument("--load", action="store_true", help="Load bitstream")
    parser.add_argument("--flash", action="store_true", help="Flash bitstream")
    parser.add_argument(
        "--eth-phy",
        default=0, type=int, choices=[0, 1],
        help="Which RGMII PHY to use. Revision 8.2 has two. The E320's "
             "jack-to-PHY mapping is unestablished; a cable in the other "
             "jack gives a card that configures fine and never transmits.",
    )
    parser.add_argument(
        "--revision",
        default="8.2",
        type=str,
        choices=["6.0", "7.1", "8.2"],
        help="5A-75E hardware revision. These differ in PINOUT and in ECP5 "
             "speed grade (6.0/7.1 are -6 parts, 8.2 is a -7I), so the wrong "
             "one builds a bitstream that does nothing on the board. Default "
             "8.2, matching build.sh.",
    )
    parser.add_argument(
        "--ip-address",
        default="192.168.1.20",
        help="Ethernet IP address of the board (default: 192.168.1.20).",
    )
    parser.add_argument(
        "--tftp-server",
        default="192.168.1.10",
        help="TFTP/boot server the BIOS fetches boot.bin from -- the marquee "
             "host. Give it a DHCP reservation: this is baked into the bitstream.",
    )
    parser.add_argument(
        "--mac-address",
        default="0x726b895bc2e2",
        help="Ethernet MAC address of the board (defaullt: 0x726b895bc2e2).",
    )
    parser.add_argument(
        # TODO If the hub75 clock is > 20MHz (from a system 40 MHz) the image gets unstable
        # But the other parts can run at a higher frequency (especially the part loading the next line from SDRAM into blockram)
        # So to increase performance, maybe add a CDC?
        "--sys-clk-freq",
        default=40e6,
        help="System clock frequency (default: 40MHz)",
    )
    parser.add_argument(
        "--panel",
        default="96x48",
        choices=list(PANELS.keys()),
        help=f"Panel type (default: 96x48). Available: {', '.join(PANELS.keys())}",
    )
    parser.add_argument(
        "--outputs",
        default=6,
        type=int,
        help="Number of HUB75 outputs (default: 6)",
    )
    parser.add_argument(
        "--chain-length",
        default=2,
        type=int,
        help="Panels per HUB75 output chain (default: 2, log2 passed to gateware)",
    )
    args = parser.parse_args()

    # Convert chain length to log2 value for gateware
    import math
    chain_length_2 = int(math.log2(args.chain_length)) if args.chain_length > 1 else 0

    soc = BaseSoC(
        revision=args.revision,
        eth_phy=args.eth_phy,
        sys_clk_freq=args.sys_clk_freq,
        ip_address=args.ip_address,
        tftp_server=args.tftp_server,
        panel=args.panel,
        board=args.board,
        n_outputs=args.outputs,
        chain_length_2=chain_length_2,
        **soc_core_argdict(args)
    )
    builder_options = builder_argdict(args)
    # builder_options["csr_svd"] = "sw_rust/litex-pac/colorlight.svd"
    # builder_options["memory_x"] = "sw_rust/litex-pac/memory.x"
    builder_options["bios_console"] = "lite"
    builder = Builder(soc, **builder_options)
    builder.build(**trellis_argdict(args), run=args.build)

    # Generate svd
    csr_svd_contents = export.get_csr_svd(soc)
    # PATCH IT (only if ethmac is present)
    if "ethmac" in soc.mem_regions:
        ethmac_adr = soc.mem_regions["ethmac"].origin
        csr_svd_contents = modify_svd(csr_svd_contents, ethmac_adr)
    # Write it out!
    write_to_file("sw_rust/litex-pac/colorlight.svd", csr_svd_contents)

    # If requested load the resulting bitstream onto the 5A-75B
    if args.flash or args.load:
        prog = EcpprogProgrammer()
        if args.load:
            prog.load_bitstream(
                os.path.join(builder.gateware_dir, soc.build_name + ".bit")
            )
        if args.flash:
            prog.flash(
                0x00000000, os.path.join(builder.gateware_dir, soc.build_name + ".bit")
            )


def modify_svd(svd_contents, eth_addr):
    # Add Ethernet buffer peripheral to svd
    registers = (
        """        <peripheral>
            <name>ETHMEM</name>
"""
        + "            <baseAddress>"
        + hex(eth_addr)
        + """</baseAddress>
            <groupName>ETHMEM</groupName>
            <registers>
                <register>
                    <name>RX_BUFFER_0[%s]</name>
                    <dim>2048</dim>
                    <dimIncrement>1</dimIncrement>
                    <description><![CDATA[rx buffers]]></description>
                    <addressOffset>0x0000</addressOffset>
                    <resetValue>0x00</resetValue>
                    <size>8</size>
                    <access>read-only</access>
                    <fields>
                        <field>
                            <name>rx_buffer_0</name>
                            <msb>7</msb>
                            <bitRange>[7:0]</bitRange>
                            <lsb>0</lsb>
                        </field>
                    </fields>
                </register>
                <register>
                    <name>RX_BUFFER_1[%s]</name>
                    <dim>2048</dim>
                    <dimIncrement>1</dimIncrement>
                    <description><![CDATA[rx buffers]]></description>
                    <addressOffset>0x0800</addressOffset>
                    <resetValue>0x00</resetValue>
                    <size>8</size>
                    <access>read-only</access>
                    <fields>
                        <field>
                            <name>rx_buffer_1</name>
                            <msb>7</msb>
                            <bitRange>[7:0]</bitRange>
                            <lsb>0</lsb>
                        </field>
                    </fields>
                </register>
                <register>
                    <name>TX_BUFFER_0[%s]</name>
                    <dim>2048</dim>
                    <dimIncrement>1</dimIncrement>
                    <description><![CDATA[tx buffers]]></description>
                    <addressOffset>0x1000</addressOffset>
                    <resetValue>0x00</resetValue>
                    <size>8</size>
                    <access>read-write</access>
                    <fields>
                        <field>
                            <name>tx_buffer_0</name>
                            <msb>7</msb>
                            <bitRange>[7:0]</bitRange>
                            <lsb>0</lsb>
                        </field>
                    </fields>
                </register>
                <register>
                    <name>TX_BUFFER_1[%s]</name>
                    <dim>2048</dim>
                    <dimIncrement>1</dimIncrement>
                    <description><![CDATA[tx buffers]]></description>
                    <addressOffset>0x1800</addressOffset>
                    <resetValue>0x00</resetValue>
                    <size>8</size>
                    <access>read-write</access>
                    <fields>
                        <field>
                            <name>tx_buffer_1</name>
                            <msb>7</msb>
                            <bitRange>[7:0]</bitRange>
                            <lsb>0</lsb>
                        </field>
                    </fields>
                </register>
            </registers>
            <addressBlock>
                <offset>0</offset>
                <size>0x4000</size>
                <usage>buffer</usage>
            </addressBlock>
        </peripheral>
    </peripherals>"""
    )

    return svd_contents.replace("</peripherals>", registers)


if __name__ == "__main__":
    main()
