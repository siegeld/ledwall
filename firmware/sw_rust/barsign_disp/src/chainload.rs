//! Start a firmware image that has been received into RAM -- the card doing
//! for itself what the BIOS's netboot does.
//!
//! Why it exists: marquee is the source of truth for what every card runs,
//! and a card gets that image from the server at boot. A head's BIOS netboots
//! it. A chain MEMBER's BIOS cannot -- its DHCP is a broadcast, and the card in
//! front drops broadcasts (ARCH.md §14) -- so it always comes up on its flash
//! copy, and the firmware then fetches the assigned image itself
//! (`POST /api/netboot`, tftp_flash.rs) and hands over to it here. Flash is not
//! written: it stays the survival copy (ARCH.md §8, "Firmware: what runs and
//! when flash is written").
//!
//! The hand-over is the BIOS's `boot()`: interrupts off, instruction cache
//! flushed, jump to the start of main RAM with a0-a2 zero. The difference is
//! that the image is not yet where it must run -- this firmware is running
//! there -- so a small routine is copied into the on-chip SRAM (free once the
//! BIOS is done; nothing in this firmware links there) and it does the copy
//! from the staging area to `MAIN_RAM_BASE`, flushes, and jumps. The copy goes
//! forward and the destination is below the source, so it is safe even where
//! the two overlap.
//!
//! This VexRiscv has an instruction cache and no data cache; the L2 in front
//! of SDRAM serves instruction and data fetches alike, so the copied code is
//! coherent once the instruction cache is flushed (`fence.i`, 0x100F).

/// Where the BIOS loads and starts a firmware image (memory.x `main_ram`).
pub const MAIN_RAM_BASE: usize = 0x4000_0000;
/// On-chip SRAM (memory.x `sram`); 4 KB on the four-output E320 build, of
/// which the routine below uses under 100 bytes.
pub const SRAM_BASE: usize = 0x1000_0000;

#[cfg(target_arch = "riscv32")]
core::arch::global_asm!(r#"
.section .text.chainload_tramp,"ax",@progbits
.balign 4
.global chainload_tramp_start
.global chainload_tramp_end
# a0 = source, a1 = destination (also the entry point), a2 = words
chainload_tramp_start:
    mv   t2, a1
    beqz a2, 2f
1:
    lw   t0, 0(a0)
    sw   t0, 0(a1)
    addi a0, a0, 4
    addi a1, a1, 4
    addi a2, a2, -1
    bnez a2, 1b
2:
    .word 0x0000100f
    nop
    nop
    nop
    nop
    nop
    li   a0, 0
    li   a1, 0
    li   a2, 0
    jr   t2
chainload_tramp_end:
"#);

#[cfg(target_arch = "riscv32")]
extern "C" {
    static chainload_tramp_start: u8;
    static chainload_tramp_end: u8;
}

/// Copy `len` bytes at `image` to `MAIN_RAM_BASE` and start them. Never
/// returns. The caller has already stopped every gateware writer into SDRAM
/// (pixel DMA and filter) -- they would land in the new image.
#[cfg(target_arch = "riscv32")]
pub unsafe fn start(image: *const u8, len: usize) -> ! {
    // Interrupts off at both levels: the LiteX IRQ mask (CSR 0xBC0, see
    // main.rs) and mstatus.MIE.
    core::arch::asm!("csrci mstatus, 8", "csrw 0xBC0, zero");

    let from = &chainload_tramp_start as *const u8;
    let n = &chainload_tramp_end as *const u8 as usize - from as usize;
    for i in 0..n {
        core::ptr::write_volatile((SRAM_BASE + i) as *mut u8, *from.add(i));
    }
    // The routine was just written as data; the instruction cache may hold
    // whatever was fetched from SRAM before.
    core::arch::asm!(".word 0x0000100f", "nop", "nop", "nop", "nop", "nop");

    core::arch::asm!(
        "jr {entry}",
        entry = in(reg) SRAM_BASE,
        in("a0") image,
        in("a1") MAIN_RAM_BASE,
        in("a2") (len + 3) / 4,
        options(noreturn),
    );
}
