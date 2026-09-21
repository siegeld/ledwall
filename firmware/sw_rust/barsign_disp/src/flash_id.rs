use litex_pac as pac;

/// Read the 64-bit unique ID from W25Q32JV SPI flash.
///
/// Uses command 0x4B followed by 4 dummy bytes, then reads 8 data bytes.
/// Must be called before img_flash::Flash takes ownership of the SPI peripheral.
pub fn read_flash_unique_id(spi: &pac::SpiflashMmap) -> [u8; 8] {
    // Configure SPI master: 8-bit transfers, single width, mask=1
    unsafe {
        spi.master_phyconfig()
            .write(|w| w.len().bits(8).width().bits(1).mask().bits(1));
    }

    // Assert CS
    spi.master_cs().write(|w| w.master_cs().set_bit());

    // Send command 0x4B (Read Unique ID)
    spi_transfer_byte(spi, 0x4B);

    // Send 4 dummy bytes
    for _ in 0..4 {
        spi_transfer_byte(spi, 0x00);
    }

    // Read 8 bytes of unique ID
    let mut uid = [0u8; 8];
    for byte in uid.iter_mut() {
        *byte = spi_transfer_byte(spi, 0x00);
    }

    // Deassert CS
    spi.master_cs().write(|w| w.master_cs().clear_bit());

    uid
}

/// Transfer a single byte over SPI master (full duplex: send + receive).
fn spi_transfer_byte(spi: &pac::SpiflashMmap, tx: u8) -> u8 {
    while spi.master_status().read().tx_ready().bit_is_clear() {}
    unsafe { spi.master_rxtx().write(|w| w.bits(tx as u32)) };
    while spi.master_status().read().rx_ready().bit_is_clear() {}
    spi.master_rxtx().read().bits() as u8
}

/// Derive a locally-administered unicast MAC from the 64-bit flash unique ID.
///
/// Format: 02:xx:xx:xx:xx:xx — the 0x02 prefix marks it as locally administered
/// per IEEE 802. The 5 payload bytes are produced by XOR-folding the 8-byte UID.
pub fn derive_mac(unique_id: &[u8; 8]) -> [u8; 6] {
    let mut mac = [0x02u8, 0, 0, 0, 0, 0];
    for i in 0..5 {
        mac[i + 1] = unique_id[i] ^ unique_id[i + 3];
    }
    mac
}

/// Read SPI flash status registers SR1 (0x05), SR2 (0x35) and SR3 (0x15).
///
/// Diagnostic for a boot fault that only shows at COLD POWER-ON: the ECP5's
/// master-SPI configuration read depends on the flash's non-volatile status
/// bits, and a JTAG operation hides the problem because openFPGALoader unlocks
/// and drives every pin itself before it does anything.
///
/// What to look for on a GD25Q32:
///   SR1  bit0 BUSY, bit1 WEL, bits2-6 BP0-4 (block protect), bit7 SRP0
///   SR2  bit0 SRP1, bit1 QE, bits3-5 LB1-3, bit6 CMP, bit7 SUS
///
/// **QE is the one that can stop a board booting.** With QE=0 the IO3 pin acts
/// as /HOLD; if nothing drives it high while the configuration engine is
/// reading, the flash simply ignores the read and the FPGA never configures --
/// dark panel, no network, and a flash whose CONTENTS verify perfectly.
pub fn read_status_regs(spi: &pac::SpiflashMmap) -> [u8; 3] {
    unsafe {
        spi.master_phyconfig()
            .write(|w| w.len().bits(8).width().bits(1).mask().bits(1));
    }
    let mut out = [0u8; 3];
    for (i, cmd) in [0x05u8, 0x35, 0x15].iter().enumerate() {
        spi.master_cs().write(|w| w.master_cs().set_bit());
        spi_transfer_byte(spi, *cmd);
        out[i] = spi_transfer_byte(spi, 0x00);
        spi.master_cs().write(|w| w.master_cs().clear_bit());
    }
    out
}

/// Write Status Register-3 (0x11), to restore the flash's output DRIVE STRENGTH.
///
/// SR3 bits 5-6 are DRV0/DRV1: 00 = 100%, 01 = 75%, 10 = 50%, 11 = 25%. This
/// board reads back SR3 = 0x20 (DRV0 set), i.e. a REDUCED driver, and the value
/// is NON-VOLATILE -- it survives power cycles.
///
/// That is the shape of the cold-boot fault. A weakened flash output still
/// satisfies every slow, buffered read -- openFPGALoader's JTAG bridge, and the
/// SoC's own LiteSPI window once configured -- while failing the one read that
/// has no margin: the ECP5's unbuffered power-on configuration fetch. It also
/// explains why this once worked and later stopped without any bitstream
/// changing.
///
/// Writes 0x00: DRV = 00 (full drive) and WPS clear. This is a NON-VOLATILE
/// write and is exposed on demand rather than run at boot -- flash status
/// registers have limited endurance and nothing should rewrite them every time.
pub fn write_status3(spi: &pac::SpiflashMmap, value: u8) -> u8 {
    write_status(spi, 0x11, value)
}

/// Write Status Register-2 (0x31). Bit 1 is **QE**.
///
/// With QE=0 the flash's IO2/IO3 pins keep their /WP and /HOLD functions. At
/// power-on nothing drives them -- the FPGA is unconfigured, which is the whole
/// point -- so if the board lacks pull-ups the flash ignores the configuration
/// read and the device never configures. Every JTAG path works because
/// openFPGALoader's bridge design drives those pins.
///
/// Setting QE=1 removes the /WP and /HOLD functions entirely, so nothing can
/// hold the flash off. Lattice's sysCONFIG note says a design must "drive
/// WPn=1 and HOLDn=1"; this achieves the same end in the flash instead.
pub fn write_status2(spi: &pac::SpiflashMmap, value: u8) -> u8 {
    write_status(spi, 0x31, value)
}

fn write_status(spi: &pac::SpiflashMmap, cmd: u8, value: u8) -> u8 {
    unsafe {
        spi.master_phyconfig()
            .write(|w| w.len().bits(8).width().bits(1).mask().bits(1));
    }

    // 0x06 WRITE ENABLE -- required before any status-register write.
    spi.master_cs().write(|w| w.master_cs().set_bit());
    spi_transfer_byte(spi, 0x06);
    spi.master_cs().write(|w| w.master_cs().clear_bit());

    // WRITE STATUS REGISTER-n, then the byte.
    spi.master_cs().write(|w| w.master_cs().set_bit());
    spi_transfer_byte(spi, cmd);
    spi_transfer_byte(spi, value);
    spi.master_cs().write(|w| w.master_cs().clear_bit());

    // Poll SR1 until BUSY clears. Bounded: a wedged flash must not hang the
    // HTTP handler, which runs inside the network ISR.
    let mut sr1 = 0xFFu8;
    for _ in 0..100_000 {
        spi.master_cs().write(|w| w.master_cs().set_bit());
        spi_transfer_byte(spi, 0x05);
        sr1 = spi_transfer_byte(spi, 0x00);
        spi.master_cs().write(|w| w.master_cs().clear_bit());
        if sr1 & 1 == 0 {
            break;
        }
    }
    sr1
}
