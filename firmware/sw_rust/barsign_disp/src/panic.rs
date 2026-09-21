use core::fmt::Write;
use core::panic::PanicInfo;

use litex_pac as pac;

#[panic_handler]
fn panic(info: &PanicInfo) -> ! {
    // Mask interrupts first. This handler previously ran with interrupts still
    // enabled, so the network ISR kept firing while we sat in the UART spin
    // below -- re-entering the very code that was already failing.
    unsafe { core::arch::asm!("csrci mstatus, 0b1000") };
    // Leave a marker: distinguishes a Rust panic from a CPU exception (99).
    crate::breadcrumb::mark(crate::breadcrumb::PANIC);
    let mut writer = PanicWriter {};
    writeln!(writer, "{}", info).ok();
    // Write some more text, otherwise not all data gets through
    writeln!(writer, "Panic done!").ok();
    writeln!(writer, "Panic done!").ok();
    // And reboot!

    unsafe { (*pac::Ctrl::ptr()).reset().write(|w| w.soc_rst().set_bit()) };
    loop {
        unsafe { (*pac::Ctrl::ptr()).reset().write(|w| w.soc_rst().set_bit()) };
    }
}

struct PanicWriter {}

impl core::fmt::Write for PanicWriter {
    fn write_str(&mut self, s: &str) -> Result<(), core::fmt::Error> {
        let uart = unsafe { &(*pac::Uart::ptr()) };
        for byte in s.as_bytes() {
            // BOUNDED wait. This was `while txfull != 0 {}` with no limit, and
            // this board has no serial console -- nothing drains the TX FIFO, so
            // once it filled, the panic handler spun here forever and never
            // reached the soc_rst below. That turned every panic into an
            // unrecoverable hang instead of a reboot, and it is why the same
            // firmware appeared to "reset" on some runs and "hang" on others:
            // it depended purely on how full the FIFO happened to be.
            let mut spins: u32 = 0;
            while uart.txfull().read().bits() != 0 {
                spins += 1;
                if spins > 200_000 {
                    // Nobody is reading. Drop the message and get on with the
                    // reset -- rebooting matters more than the text.
                    return Ok(());
                }
            }
            unsafe {
                uart.rxtx().write(|w| w.rxtx().bits(*byte));
            }
        }
        Ok(())
    }
}
