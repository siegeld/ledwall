use crate::hal;
use litex_pac as pac;
use spi_memory::{prelude::*, series25::Flash as Flash25};

/// Base of the memory-mapped SPI flash window.
///
/// MUST match the spiflash SoCRegion origin in gateware/colorlight.py. This was
/// hardcoded to 0x80000000, which stopped being the flash window a long time ago
/// -- that address is the EthMAC buffer region, so read_byte()/read_image() were
/// aimed at the wrong peripheral (and 0x80180000, where read_image() started, is
/// mapped to nothing at all).
const MMAP_BASE: usize = 0x8040_0000;

/// W25Q32JV: 32 Mbit = 4MB. Was 2MB, sized for the GD25Q16 that the gateware
/// used to declare; it drives img_offset below, so understating it put stored
/// images at 0x180000 -- only 512KB above the firmware at 0x100000, close enough
/// that a growing boot.bin would eventually be erased by write_image().
static FLASH_SIZE: usize = (32 / 8) * 1024 * 1024;
static SECTOR_SIZE: usize = 4 * 1024;
pub struct Flash {
    memory: Flash25<hal::SpiMem, hal::SpiCS>,
}

impl Flash {
    pub fn new(spi: pac::SpiflashMmap) -> Self {
        let spi = hal::SpiMem::new(spi);
        let memory = Flash25::init(spi.0, spi.1).unwrap();
        Self { memory }
    }

    pub fn read_byte(&mut self, offset: usize) -> u8 {
        let eeprom =
            unsafe { core::slice::from_raw_parts_mut(MMAP_BASE as *mut u8, FLASH_SIZE) };
        eeprom[offset]
    }

    pub fn read_manual_byte(&mut self, offset: usize) -> u8 {
        let mut data = [0];
        self.memory.read(offset as u32, &mut data).unwrap();
        data[0]
    }

    pub fn memory_read_test(&mut self) -> bool {
        for address in 0..FLASH_SIZE {
            if self.read_byte(address) != self.read_manual_byte(address) {
                return false;
            }
        }
        true
    }

    pub fn write_image(&mut self, data: impl Iterator<Item = u8>) {
        // Working around the fact that `chunks()` doesn't exist on iterators.
        let mut data_iter = data.enumerate();
        let mut count = 0;
        let mut done = false;
        let img_offset = (FLASH_SIZE / 4) * 3;
        while !done {
            let mut data = [0; 256];
            for i in 0..256 {
                if let Some((iter_count, data_byte)) = data_iter.next() {
                    data[i] = data_byte;
                    count = iter_count;
                } else {
                    // Fill the rest up
                    count += 1;
                    data[i] = 0;
                    done = true;
                }
            }
            let offset = count & !0xFF;
            if ((offset) & (SECTOR_SIZE - 1)) == 0 {
                self.memory
                    .erase_sectors((img_offset + count) as u32, 1)
                    .unwrap();
            }
            self.memory
                .write_bytes((img_offset + offset) as u32, &mut data)
                .unwrap();
        }
    }

    pub fn read_image(&mut self) -> &[u8] {
        let img_offset = (FLASH_SIZE / 4) * 3;
        // Small hack
        unsafe {
            core::slice::from_raw_parts(
                (MMAP_BASE + img_offset) as *const u8,
                FLASH_SIZE - img_offset,
            )
        }
    }
}
