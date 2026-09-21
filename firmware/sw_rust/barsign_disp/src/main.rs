#![no_std]
#![no_main]

use core::fmt::Write as _;

use barsign_disp::*;
use embedded_hal::blocking::serial::Write;
use embedded_hal::serial::Read;
use hal::*;
use layout::LayoutConfig;
use litex_pac as pac;
use riscv_rt::entry;

// ============================================================================
// VexRiscv External Interrupt Handler
// ============================================================================
// VexRiscv interrupt setup:
// 1. Write trap handler address to mtvec (WRITE_ONLY - reads return 0)
// 2. Set bit N in CSR 0xBC0 (IRQ_MASK) to enable IRQ N
// 3. Set mie.MEIE and mstatus.MIE
//
// The ISR calls network_handler() which does ALL network processing:
// - iface.poll() to process packets
// - DHCP, HTTP, Telnet, Artnet, Bitmap handling
// Main loop does ZERO network code - only display/animation.

// Assembly trap vector - save ALL GPRs, call network_handler(), restore
// 31 registers (skip x0) = 124 bytes, round to 128 for alignment
core::arch::global_asm!(r#"
.section .text.trap_handler
.global _trap_handler
.align 4
_trap_handler:
    # Save all GPRs except x0 (128 bytes for 8-byte alignment)
    addi sp, sp, -128
    sw ra,  0(sp)     # x1
    # x2 (sp) not saved - we're using it
    sw gp,  4(sp)     # x3
    sw tp,  8(sp)     # x4
    sw t0, 12(sp)     # x5
    sw t1, 16(sp)     # x6
    sw t2, 20(sp)     # x7
    sw s0, 24(sp)     # x8
    sw s1, 28(sp)     # x9
    sw a0, 32(sp)     # x10
    sw a1, 36(sp)     # x11
    sw a2, 40(sp)     # x12
    sw a3, 44(sp)     # x13
    sw a4, 48(sp)     # x14
    sw a5, 52(sp)     # x15
    sw a6, 56(sp)     # x16
    sw a7, 60(sp)     # x17
    sw s2, 64(sp)     # x18
    sw s3, 68(sp)     # x19
    sw s4, 72(sp)     # x20
    sw s5, 76(sp)     # x21
    sw s6, 80(sp)     # x22
    sw s7, 84(sp)     # x23
    sw s8, 88(sp)     # x24
    sw s9, 92(sp)     # x25
    sw s10, 96(sp)    # x26
    sw s11, 100(sp)   # x27
    sw t3, 104(sp)    # x28
    sw t4, 108(sp)    # x29
    sw t5, 112(sp)    # x30
    sw t6, 116(sp)    # x31
    # 120-124 = padding

    # === EXCEPTION CAPTURE ===
    # This vector is entered for BOTH interrupts and exceptions, and the code
    # below assumes an interrupt -- it calls network_handler unconditionally and
    # mrets. A CPU exception (misaligned/faulting load or store) therefore
    # re-enters the network path instead of being reported, which is invisible
    # on a board with no serial console. Record mcause/mepc to the breadcrumb
    # words and reset, so the fault becomes readable after the reboot.
    csrr t0, mcause
    bltz t0, 2f              # MSB set => interrupt: normal path
    li   t1, 0x902A0008
    sw   t0, 0(t1)           # mcause
    csrr t2, mepc
    sw   t2, 4(t1)           # mepc
    li   t3, 0x902A0000
    li   t4, 0xBEEF0063      # breadcrumb code 99 = EXCEPTION
    sw   t4, 0(t3)
    li   t5, 0xf0001000      # CSR_CTRL_RESET
    li   t6, 1
    sw   t6, 0(t5)           # soc_rst
1:  j 1b
2:

    # === ISR LOGIC ===
    # Increment the ISR counter.
    #
    # This used to be `li t0, 0x40020000` -- a hardcoded address that is NOT
    # reserved for anything. .text spans 0x40000000-0x40026938, so 0x40020000
    # lands INSIDE the firmware's own code, and every interrupt overwrote an
    # instruction there with the counter value. Whether that crashed depended on
    # whether the clobbered word was ever executed and whether the current count
    # happened to decode as a legal instruction -- which is why the failure was
    # intermittent and why it moved when the code layout changed.
    # Confirmed on hardware: mcause=2 (illegal instruction), mepc=0x40020000,
    # with 0x40020000 disassembling to an instruction inside present().
    la t0, ISR_COUNTER
    lw t1, 0(t0)
    addi t1, t1, 1
    sw t1, 0(t0)

    # NO hardcoded CSR write here. This used to be:
    #     li t0, 0xF0001814 ; sw zero, 0(t0)
    # meaning to clear ethmac_sram_writer_ev_enable "to prevent interrupt
    # storm". That address stopped being ethmac when the pixdma block was added
    # and shifted the CSR map: 0xF0001814 is now pixdma_bad_magic, and ethmac's
    # ev_enable moved to 0xF0002814. The write therefore landed on a read-only
    # status register and did nothing -- which is also why
    # check_and_reenable_interrupt() never fired, since it tests for the
    # ev_enable this was supposed to have cleared.
    #
    # It is deleted rather than re-pointed. The masking was never actually
    # active, so the firmware's real, working behaviour is the unmasked one, and
    # switching it on now would be an untested change to interrupt timing. What
    # had to go is the blind absolute write: it is harmless only for as long as
    # nothing writable occupies that address, and this is the second time a
    # hardcoded address in this vector has drifted (see the 0x40020000 counter
    # that was overwriting .text). Peripheral access belongs in Rust, through
    # the PAC, where the linker resolves it.

    # Call Rust network handler (does ALL network processing)
    call network_handler
    # === END ISR ===

    # Restore all GPRs
    lw ra,  0(sp)
    lw gp,  4(sp)
    lw tp,  8(sp)
    lw t0, 12(sp)
    lw t1, 16(sp)
    lw t2, 20(sp)
    lw s0, 24(sp)
    lw s1, 28(sp)
    lw a0, 32(sp)
    lw a1, 36(sp)
    lw a2, 40(sp)
    lw a3, 44(sp)
    lw a4, 48(sp)
    lw a5, 52(sp)
    lw a6, 56(sp)
    lw a7, 60(sp)
    lw s2, 64(sp)
    lw s3, 68(sp)
    lw s4, 72(sp)
    lw s5, 76(sp)
    lw s6, 80(sp)
    lw s7, 84(sp)
    lw s8, 88(sp)
    lw s9, 92(sp)
    lw s10, 96(sp)
    lw s11, 100(sp)
    lw t3, 104(sp)
    lw t4, 108(sp)
    lw t5, 112(sp)
    lw t6, 116(sp)
    addi sp, sp, 128
    mret
"#);

extern "C" {
    fn _trap_handler();
}

#[entry]
fn main() -> ! {
    let peripherals = unsafe { pac::Peripherals::steal() };

    let mut serial = UART {
        registers: peripherals.uart,
    };

    serial.bwrite_all(b"Hello world!\n").unwrap();

    // Latch any crash marker from the previous boot before we overwrite it.
    breadcrumb::capture_boot();

    let mut hub75 = hub75::Hub75::new(peripherals.hub75, peripherals.hub75_palette);

    // Read flash unique ID before Flash takes ownership of SPI peripheral
    let unique_id = flash_id::read_flash_unique_id(&peripherals.spiflash_mmap);
    // Diagnostic for the cold-boot fault: publish the flash's non-volatile
    // status bits. Read here, before img_flash::Flash takes the peripheral.
    let flash_sr = flash_id::read_status_regs(&peripherals.spiflash_mmap);
    network::set_flash_status(flash_sr);
    let mac_bytes = flash_id::derive_mac(&unique_id);
    // Tell the gateware's hardware UDP filter which MAC to accept.
    unsafe { network::publish_hw_filter_mac(&mac_bytes) };

    let mut flash = img_flash::Flash::new(peripherals.spiflash_mmap);
    // Print startup info
    writeln!(serial, "Flash UID: {:02x}{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}{:02x}",
        unique_id[0], unique_id[1], unique_id[2], unique_id[3],
        unique_id[4], unique_id[5], unique_id[6], unique_id[7]).ok();
    writeln!(serial, "MAC: {:02x}:{:02x}:{:02x}:{:02x}:{:02x}:{:02x}",
        mac_bytes[0], mac_bytes[1], mac_bytes[2],
        mac_bytes[3], mac_bytes[4], mac_bytes[5]).ok();

    let mut buffer = [0u8; 64];
    let out_data = heapless::Vec::new();
    let mut output = menu::Output { serial, out_data };

    // Initialize the ISR counter
    unsafe {
        core::ptr::write_volatile(core::ptr::addr_of_mut!(barsign_disp::ISR_COUNTER), 0);
    }

    // Set up trap handler infrastructure (using main stack, no mscratch)
    unsafe {
        // Write trap handler address to mtvec (WRITE_ONLY - reads return 0, but writes work)
        let trap_addr = _trap_handler as *const () as usize;
        core::arch::asm!("csrw mtvec, {}", in(reg) trap_addr);
        ethernet::set_trap_addr(trap_addr);

        // Read back mtvec for debugging (will be 0 due to WRITE_ONLY mode)
        let mtvec: usize;
        core::arch::asm!("csrr {}, mtvec", out(reg) mtvec);
        ethernet::set_debug_mtvec(mtvec);
    }

    // Always turn on HUB75 for debugging (shows firmware is running)
    hub75.on();

    // Load image from SPI flash if available, otherwise use default
    if let Ok(image) = img::load_image(flash.read_image()) {
        let w = image.0;
        hub75.set_img_param(image.0, image.1);
        hub75.write_img_data(0, image.3);
        // Stamp the running version on the panel itself.
        hub75.draw_version_banner(w as usize);
        hub75.swap_buffers();
    } else {
        let image = img::load_default_image();
        let w = image.0;
        hub75.set_img_param(image.0, image.1);
        hub75.write_img_data(0, image.3);
        hub75.draw_version_banner(w as usize);
        hub75.swap_buffers();
    }

    // Configure panel: single 128x64 panel, one chain position
    hub75.set_panel_param(0, 0, 0, 0, 0);  // x=0, y=0, no rotation

    // Debug: print panel params to verify they were set
    let (x0, y0, r0) = hub75.get_panel_param(0, 0);
    writeln!(output.serial, "Panel config set: p0_0=({},{},{})", x0, y0, r0).ok();

    // Read default panel size from bitstream CSRs (before hub75 is moved)
    let (hw_cols, hw_rows, _, _, _) = hub75.get_hw_info();

    let context = menu::Context {
        mac: mac_bytes,
        output,
        hub75,
        flash,
        animation: menu::Animation::None,
        quit: false,
        debug: false,
        bitmap_stats: bitmap_udp::BitmapStats::new(),
        layout: LayoutConfig::single_panel(hw_cols, hw_rows),
        reboot_pending: false,
        boot_server: None,
        mac_overflow: 0,
        mac_preamble_err: 0,
        mac_crc_err: 0,
        ring_overflow: 0,
    };

    let mut r = menu::Runner::new(&menu::ROOT_MENU, &mut buffer, context);

    // Initialize the network stack with static storage
    // Pass pointers to display state so ISR can access them
    network::init(
        peripherals.ethmac,
        peripherals.ethmem,
        mac_bytes,
        &mut r.context.hub75 as *mut _,
        &mut r.context.layout as *mut _,
        &mut r.context.animation as *mut _,
        &mut r.context.bitmap_stats as *mut _,
    );

    writeln!(r.context.output.serial, "Network stack initialized (ISR-driven)").ok();

    // Keep pixel traffic out of the CPU's MAC (RB-5486).
    //
    // The gateware classifier kills UDP packets to the pixel port before the MAC
    // can raise a receive event, so the CPU takes no interrupt for them. On a
    // 9-panel RGB frame that is the difference between ~5.5 fps and ~33: at
    // 3,333 packets/s the unfiltered CPU path collapses to 0.37 fps delivered,
    // and with the filter it holds 18-20 with nothing dropped.
    //
    // Enabled HERE, from firmware, rather than defaulted on in the gateware --
    // and that distinction is the whole safety story. pixel_filter_enable is a
    // CSRStorage with reset 0, so it clears on every FPGA configuration. The
    // BIOS therefore always runs with the filter OFF and can always netboot, no
    // matter how badly this firmware misbehaves: pull the power, the CSR
    // resets, the BIOS fetches whatever is deployed. If it were defaulted on in
    // the bitstream, a bad classifier would be a JTAG recovery over a link that
    // corrupts most of what it reads.
    unsafe { network::set_pixel_filter(true) };
    writeln!(r.context.output.serial, "Pixel filter enabled (CPU out of the pixel path)").ok();

    // Apply the config baked in at build time, if there is one. Done HERE --
    // after init(), which publishes the pointers apply_layout() needs, but
    // before the network is up -- so a standalone panel is drawing its own
    // content within a second of power-on rather than after DHCP has timed out
    // and TFTP has failed.
    //
    // A TFTP config still overrides it later, so a fleet panel is unaffected.
    // The default is simply what it shows until one arrives, and what it keeps
    // showing when there is no network to ask.
    if unsafe { network::apply_default_config() } {
        writeln!(r.context.output.serial, "Default config applied (compiled in)").ok();
    }

    // Process any pending packets before enabling interrupts
    // This ensures clean state and handles any stale packets in hardware FIFO
    network::network_handler();

    // Enable ETHMAC interrupts immediately - ISR handles all network from boot
    unsafe {
        // 1. Enable ETHMAC peripheral interrupt
        ethernet::enable_rx_interrupt();

        // 2. Set VexRiscv IRQ_MASK. ETHMAC is IRQ 2, Timer0 is IRQ 1
        //    (build/colorlight_5a_75e/csr.csv).
        //
        //    The timer bit was missing, which made the panel's clock a function
        //    of inbound ethernet: update_time_from_timer() is only reached from
        //    network_handler(), so TIME_MS advanced solely when a packet
        //    arrived, and any whole second that elapsed without one was lost
        //    outright -- ev_pending is a single latched bit, not a counter, so
        //    the clock ran slow and never caught up. It went unnoticed only
        //    because this segment carries constant broadcast traffic; the
        //    planned dedicated panel VLAN removes exactly that, which would
        //    have stopped the stale-frame flush working just as the quiet
        //    network made it necessary.
        core::arch::asm!("csrw 0xBC0, {}", in(reg) ((1u32 << 2) | (1u32 << 1)));

        // 3. Enable machine external interrupts and global interrupt enable
        riscv::register::mie::set_mext();
        riscv::register::mstatus::set_mie();
    }
    writeln!(r.context.output.serial, "Interrupts enabled").ok();

    let mut last_time_ms: i64 = 0;
    // Deadlines, not `time_ms % N == 0`. TIME_MS is resampled from the hardware
    // counter inside the ISR, so it advances by the inter-ISR gap rather than a
    // millisecond at a time -- every multiple that fell inside a jump was
    // skipped outright, which made the status refresh and the "30 fps"
    // animation fire erratically and stall entirely under sparse traffic.
    let mut last_status_ms: i64 = 0;
    let mut last_anim_ms: i64 = 0;

    // Configure timer0 for 1-second period
    // We read the countdown value to get millisecond precision
    // ev_pending only fires once per second (to track seconds)
    unsafe {
        let t = &*pac::Timer0::ptr();
        t.en().write(|w| w.bits(0));
        // Derived from the one clock constant, not repeated as a literal.
        t.reload().write(|w| w.bits(network::SYS_CLK_HZ - 1));
        t.load().write(|w| w.bits(network::SYS_CLK_HZ - 1));
        t.en().write(|w| w.bits(1));
        t.ev_pending().write(|w| w.bits(1));        // clear any pending event
        t.ev_enable().write(|w| w.bits(1));         // route the tick to the CPU
    }

    // ========================================================================
    // MAIN LOOP - ZERO NETWORK CODE
    // All network processing happens in ISR via network_handler()
    // Timer ticks are drained by ISR to maintain accurate TIME_MS
    // ========================================================================
    loop {
        // Get time (updated by ISR draining timer ticks)
        let time_ms = network::get_time_ms();
        let timer_tick = time_ms != last_time_ms;
        if timer_tick {
            last_time_ms = time_ms;
        }

        // Check if ISR fired and re-enable when idle
        ethernet::check_and_reenable_interrupt();

        // A scheduled reboot fires here, not in the HTTP handler: the response
        // is still in a TX buffer when that handler returns.
        unsafe { network::reboot_tick() };

        // Flush a bitmap frame that has gone quiet. The ISR can only finish a
        // frame when a packet arrives, so a frame whose tail was lost (or the
        // last frame before the sender stops) needs presenting from here.
        // Every iteration, not just on the tick: a finished frame must reach the
        // glass before the DMA writes much of the next one into the same
        // buffer. See bitmap_present_tick().
        network::bitmap_present_tick();

        if timer_tick {
            network::bitmap_tick();
        }

        // Skip processing on non-timer ticks or when streaming
        let streaming = network::is_streaming();
        if !timer_tick {
            continue;
        }
        // Re-sync rather than wedge if the clock ever steps backwards.
        if time_ms < last_status_ms {
            last_status_ms = time_ms;
            last_anim_ms = time_ms;
        }
        if time_ms - last_status_ms < 5 {
            continue;
        }
        last_status_ms = time_ms;

        // Update MAC error counters for display
        let (ovf, pre, crc) = network::mac_errors();
        r.context.mac_overflow = ovf;
        r.context.mac_preamble_err = pre;
        r.context.mac_crc_err = crc;
        r.context.ring_overflow = ethernet::ring_overflow_count() as u32;

        // Update boot server info from network module
        r.context.boot_server = network::boot_server();

        // Update animation at ~30fps (every 33ms), but skip during streaming
        if !streaming && time_ms - last_anim_ms >= 33 {
            last_anim_ms = time_ms;
            r.context.animation_tick();
        }

        // Handle serial input for menu
        if let Ok(data) = r.context.output.serial.read() {
            r.input_byte(if data == b'\n' { b'\r' } else { data });
        }
    }
}
