use embedded_hal;
use litex_hal as hal;
use litex_pac as pac;

hal::uart! {
    UART: pac::Uart,
}

hal::timer! {
    TIMER: pac::Timer0,
}

pub struct SpiMem {
    spi: pac::SpiflashMmap,
}

pub struct SpiCS {
    _dummy: (),
}

impl SpiMem {
    pub fn new(spi: pac::SpiflashMmap) -> (Self, SpiCS) {
        unsafe {
            spi.master_phyconfig()
                .write(|w| w.len().bits(8).width().bits(1).mask().bits(1))
        };
        (Self { spi }, SpiCS { _dummy: () })
    }
}

impl embedded_hal::blocking::spi::Transfer<u8> for SpiMem {
    type Error = ();
    fn transfer<'w>(&mut self, words: &'w mut [u8]) -> Result<&'w [u8], Self::Error> {
        for byte in words.iter_mut() {
            while self.spi.master_status().read().tx_ready().bit_is_clear() {}
            unsafe { self.spi.master_rxtx().write(|w| w.bits(*byte as u32)) };
            while self.spi.master_status().read().rx_ready().bit_is_clear() {}
            *byte = self.spi.master_rxtx().read().bits() as u8;
        }
        Ok(words)
    }
}

// The logic levels are flipped, since litex-spi uses logical levels and not
// the physical, inverted, ones
impl embedded_hal::digital::v2::OutputPin for SpiCS {
    type Error = ();
    fn set_low(&mut self) -> Result<(), Self::Error> {
        // Safe, because this register isn't used by the "main" class
        unsafe {
            (*pac::SpiflashMmap::ptr())
                .master_cs()
                .write(|w| w.master_cs().set_bit())
        };
        Ok(())
    }
    fn set_high(&mut self) -> Result<(), Self::Error> {
        // Safe, because this register isn't used by the "main" class
        unsafe {
            (*pac::SpiflashMmap::ptr())
                .master_cs()
                .write(|w| w.master_cs().clear_bit())
        };
        Ok(())
    }
}
