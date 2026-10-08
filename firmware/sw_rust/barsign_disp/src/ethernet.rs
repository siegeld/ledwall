// From  https://github.com/DerFetzer/colorlight-litex/blob/48f1d38a3fcdf51d0bced21897e245570c38a175/rust/eth_demo/src/ethernet.rs,
// Apache 2.0/MIT by DerFetzer
use litex_pac::{Ethmac, Ethmem};

use core::sync::atomic::{AtomicUsize, Ordering};
use smoltcp::phy::{self, DeviceCapabilities};
use smoltcp::time::Instant;
use smoltcp::{Error, Result};

// LiteEth buffer layout: nrxslots RX buffers followed by ntxslots TX buffers,
// each SLOT_SIZE bytes, starting at the Ethmem base address.
// NOTE: The LiteX SVD generator only describes 2 RX buffers regardless of nrxslots,
// so the PAC's tx_buffer offsets are wrong when nrxslots > 2.
// We compute addresses directly from the base.
const SLOT_SIZE: usize = 2048;
// MUST equal `nrxslots` in gateware/colorlight.py. build.sh refuses to build a
// mismatch -- see require_nrxslots_match().
//
// This is not a tuning knob, it is an ADDRESS. TX buffers live immediately after
// the RX buffers in the MAC's SRAM, so line ~207 locates them at
// (NRXSLOTS + slot) * SLOT_SIZE. Get it wrong and the firmware writes its
// outgoing frames past the end of the region: it comes up, takes interrupts,
// and is completely MUTE -- no DHCP, no ARP reply, no ping -- while the BIOS
// netboots perfectly because it has its own driver. Cost an hour on 2026-09-18
// when nrxslots went 8 -> 2 for the P1.25 wall and only this file was missed.
const NRXSLOTS: usize = 2;

// ============================================================================
// Ring buffer for ISR-captured packets
// ============================================================================
const RX_RING_SIZE: usize = 32;  // 32 slots (vs 8 in hardware)
const RX_SLOT_SIZE: usize = 2048;

static mut RX_RING: [[u8; RX_SLOT_SIZE]; RX_RING_SIZE] = [[0; RX_SLOT_SIZE]; RX_RING_SIZE];
static mut RX_RING_LEN: [usize; RX_RING_SIZE] = [0; RX_RING_SIZE];
static RX_WRITE_IDX: AtomicUsize = AtomicUsize::new(0);
static RX_READ_IDX: AtomicUsize = AtomicUsize::new(0);

// Counter for ring buffer overflows (ISR couldn't store packet)
// Using a plain static since this is only written from ISR context
static mut RX_RING_OVERFLOW: usize = 0;

// Counter for ISR invocations (for debugging)
static mut ISR_COUNT: usize = 0;

// Flag to track if interrupts have been explicitly enabled
static mut INTERRUPTS_ENABLED: bool = false;

// Debug: mtvec value
static mut DEBUG_MTVEC: usize = 0;
// Debug: trap handler address we tried to write
static mut DEBUG_TRAP_ADDR: usize = 0;

pub fn set_debug_mtvec(val: usize) {
    unsafe { DEBUG_MTVEC = val; }
}

pub fn debug_mtvec() -> usize {
    unsafe { DEBUG_MTVEC }
}

pub fn set_trap_addr(val: usize) {
    unsafe { DEBUG_TRAP_ADDR = val; }
}

pub fn trap_addr() -> usize {
    unsafe { DEBUG_TRAP_ADDR }
}

// Base address of ETHMEM (RX buffers start here)
pub const ETHMEM_BASE: usize = 0x8000_0000;

pub struct Eth {
    ethmac: Ethmac,
    ethbuf: Ethmem,
}

impl Eth {
    pub fn new(ethmac: Ethmac, ethbuf: Ethmem) -> Self {
        ethmac
            .sram_writer_ev_pending()
            .write(unsafe { |w| w.bits(1) });
        ethmac
            .sram_reader_ev_pending()
            .write(unsafe { |w| w.bits(1) });
        ethmac.sram_reader_slot().write(unsafe { |w| w.bits(0) });

        Eth { ethmac, ethbuf }
    }

    /// Get the base address of the Ethmem region
    fn buf_base(&self) -> *mut u8 {
        self.ethbuf.rx_buffer_0(0) as *const _ as *mut u8
    }

    /// Read MAC hardware error counters: (overflow, preamble_errors, crc_errors)
    pub fn mac_errors(&self) -> (u32, u32, u32) {
        (
            self.ethmac.sram_writer_errors().read().bits(),
            self.ethmac.rx_datapath_preamble_errors().read().bits(),
            self.ethmac.rx_datapath_crc_errors().read().bits(),
        )
    }

    /// Peek at the current MAC RX slot without consuming it.
    /// Returns the raw Ethernet frame if a packet is pending.
    /// Caller must finish using the data before calling `ack_rx()`.
    pub fn peek_rx(&self) -> Option<&[u8]> {
        if self.ethmac.sram_writer_ev_pending().read().bits() == 0 {
            return None;
        }
        unsafe {
            let slot = self.ethmac.sram_writer_slot().read().bits() as usize;
            let length = self.ethmac.sram_writer_length().read().bits() as usize;
            let buf = self.buf_base() as *const u8;
            Some(core::slice::from_raw_parts(buf.add(slot * SLOT_SIZE), length))
        }
    }

    /// Acknowledge the current RX slot, allowing the MAC to reuse it.
    pub fn ack_rx(&self) {
        self.ethmac
            .sram_writer_ev_pending()
            .write(unsafe { |w| w.bits(1) });
    }
}

impl<'a> phy::Device<'a> for Eth {
    type RxToken = EthRxToken<'a>;
    type TxToken = EthTxToken<'a>;

    fn receive(&'a mut self) -> Option<(Self::RxToken, Self::TxToken)> {
        if self.ethmac.sram_writer_ev_pending().read().bits() == 0 {
            return None;
        }
        let base = self.buf_base();
        Some((
            Self::RxToken {
                ethmac: &self.ethmac,
                base,
            },
            Self::TxToken {
                ethmac: &self.ethmac,
                base,
            },
        ))
    }

    fn transmit(&'a mut self) -> Option<Self::TxToken> {
        let base = self.buf_base();
        Some(Self::TxToken {
            ethmac: &self.ethmac,
            base,
        })
    }

    fn capabilities(&self) -> DeviceCapabilities {
        let mut caps = DeviceCapabilities::default();
        // 1500, NOT the 2048 MAC slot size. The slot is how much the hardware
        // buffer holds (header + payload + FCS); the MTU is what Ethernet will
        // actually carry. At 2048 smoltcp advertised MSS 1994 and built TCP
        // segments too big for a 1500-byte link, which were dropped before they
        // reached the wire -- so ANY HTTP response larger than one frame sent
        // only its tail and the client received nothing. The ~8 KiB status page
        // never loaded while every sub-1460-byte JSON endpoint worked fine, and
        // UDP was untouched because a bitmap chunk is 1472 bytes.
        caps.max_transmission_unit = 1500;
        caps.max_burst_size = Some(NRXSLOTS);
        caps
    }
}

pub struct EthRxToken<'a> {
    ethmac: &'a Ethmac,
    base: *mut u8,
}

impl<'a> phy::RxToken for EthRxToken<'a> {
    fn consume<R, F>(self, _timestamp: Instant, f: F) -> Result<R>
    where
        F: FnOnce(&mut [u8]) -> Result<R>,
    {
        unsafe {
            if self.ethmac.sram_writer_ev_pending().read().bits() == 0 {
                return Err(Error::Exhausted);
            }
            let slot = self.ethmac.sram_writer_slot().read().bits() as usize;
            let length = self.ethmac.sram_writer_length().read().bits() as usize;
            let buf = self.base.add(slot * SLOT_SIZE);
            let data = core::slice::from_raw_parts_mut(buf, length);
            let result = f(data);
            self.ethmac.sram_writer_ev_pending().write(|w| w.bits(1));
            result
        }
    }
}

pub struct EthTxToken<'a> {
    ethmac: &'a Ethmac,
    base: *mut u8,
}

impl<'a> phy::TxToken for EthTxToken<'a> {
    fn consume<R, F>(self, _timestamp: Instant, len: usize, f: F) -> Result<R>
    where
        F: FnOnce(&mut [u8]) -> Result<R>,
    {
        //#[link_section = ".main_ram"]
        static mut TX_BUFFER: [u8; 2048] = [0; 2048];
        static mut SLOT: u8 = 0;

        while self.ethmac.sram_reader_ready().read().bits() == 0 {}
        let result = f(unsafe { &mut TX_BUFFER[..len] });
        let current_slot = unsafe { SLOT } as usize;
        // TX buffers start after NRXSLOTS RX buffers
        unsafe {
            let tx_buf = self.base.add((NRXSLOTS + current_slot) * SLOT_SIZE);
            for i in 0..len {
                core::ptr::write_volatile(tx_buf.add(i), TX_BUFFER[i]);
            }
        }
        self.ethmac
            .sram_reader_slot()
            .write(unsafe { |w| w.bits(current_slot as u32) });
        self.ethmac
            .sram_reader_length()
            .write(unsafe { |w| w.bits(len as u32) });
        self.ethmac
            .sram_reader_start()
            .write(unsafe { |w| w.bits(1) });
        unsafe {
            SLOT = (SLOT + 1) % 2;
        }
        result
    }
}

// ============================================================================
// Interrupt-driven packet reception
// ============================================================================

/// Poll hardware FIFO and copy packets to ring buffer.
/// Call this frequently from the main loop to drain packets before hardware FIFO overflows.
/// Returns the number of packets transferred.
#[no_mangle]
pub extern "C" fn poll_rx_to_ring() -> usize {
    let ethmac = unsafe { &*litex_pac::Ethmac::ptr() };
    let mut count = 0;

    // Drain all pending packets from hardware FIFO
    while ethmac.sram_writer_ev_pending().read().bits() != 0 {
        let slot = ethmac.sram_writer_slot().read().bits() as usize;
        let len = ethmac.sram_writer_length().read().bits() as usize;

        // Clamp length to slot size
        let len = len.min(RX_SLOT_SIZE);

        // Get write index
        let write_idx = RX_WRITE_IDX.load(Ordering::Relaxed);
        let next_idx = (write_idx + 1) % RX_RING_SIZE;

        // Check for overflow (ring full)
        if next_idx != RX_READ_IDX.load(Ordering::Acquire) {
            // Copy packet from hardware FIFO to ring buffer
            let src = (ETHMEM_BASE + slot * SLOT_SIZE) as *const u8;
            unsafe {
                core::ptr::copy_nonoverlapping(src, RX_RING[write_idx].as_mut_ptr(), len);
                RX_RING_LEN[write_idx] = len;
            }
            RX_WRITE_IDX.store(next_idx, Ordering::Release);
            count += 1;
        } else {
            // Ring buffer full - count the overflow
            unsafe { RX_RING_OVERFLOW += 1; }
        }

        // Acknowledge packet (clears pending, advances hardware slot)
        ethmac.sram_writer_ev_pending().write(|w| unsafe { w.bits(1) });
    }
    count
}

// Debug counter for minimal ISR test
static mut POLL_MINIMAL_COUNT: usize = 0;

/// Minimal version for ISR debugging - only read hardware registers and ack.
/// No memory copy, no atomics. If this crashes, the issue is hardware access.
#[no_mangle]
pub extern "C" fn poll_rx_minimal() -> usize {
    let ethmac = unsafe { &*litex_pac::Ethmac::ptr() };
    let mut count = 0;

    // Drain all pending packets - just read and ack
    while ethmac.sram_writer_ev_pending().read().bits() != 0 {
        // Read hardware registers (but don't use the values)
        let _slot = ethmac.sram_writer_slot().read().bits();
        let _len = ethmac.sram_writer_length().read().bits();

        count += 1;

        // Acknowledge packet
        ethmac.sram_writer_ev_pending().write(|w| unsafe { w.bits(1) });
    }

    unsafe { POLL_MINIMAL_COUNT += count; }
    count
}

/// Get minimal poll count for debugging
pub fn poll_minimal_count() -> usize {
    unsafe { POLL_MINIMAL_COUNT }
}

/// Drain one packet - ack via raw pointer.
/// ev_enable should be disabled by caller before calling this.
#[no_mangle]
pub extern "C" fn poll_rx_one() {
    // Was a hardcoded 0xF000_1810, which the pixdma block displaced: that
    // address is now pixdma_chunks (read-only), while ethmac's ev_pending moved
    // to 0xF0002810. Go through the PAC so the address can never drift again.
    let ethmac = unsafe { &*litex_pac::Ethmac::ptr() };
    ethmac.sram_writer_ev_pending().write(|w| unsafe { w.bits(1) });
    unsafe {
        POLL_MINIMAL_COUNT += 1;
    }
}

/// ETHMAC interrupt handler - called when packets arrive.
#[no_mangle]
pub extern "C" fn ethmac() {
    unsafe { ISR_COUNT += 1; }
    poll_rx_to_ring();
}

/// Get ISR invocation count (for debugging).
pub fn isr_count() -> usize {
    unsafe { core::ptr::read_volatile(core::ptr::addr_of!(crate::ISR_COUNTER)) as usize }
}

/// Increment ISR count (called from trap handler)
pub fn increment_isr_count() {
    unsafe { ISR_COUNT += 1; }
}

/// Push a packet to the ring buffer.
/// Returns true if successful, false if ring is full.
pub fn push_to_ring(data: &[u8]) -> bool {
    let len = data.len().min(RX_SLOT_SIZE);
    let write_idx = RX_WRITE_IDX.load(Ordering::Relaxed);
    let next_idx = (write_idx + 1) % RX_RING_SIZE;

    // Check for overflow (ring full)
    if next_idx == RX_READ_IDX.load(Ordering::Acquire) {
        unsafe { RX_RING_OVERFLOW += 1; }
        return false;
    }

    // Copy packet to ring buffer
    unsafe {
        RX_RING[write_idx][..len].copy_from_slice(&data[..len]);
        RX_RING_LEN[write_idx] = len;
    }
    RX_WRITE_IDX.store(next_idx, Ordering::Release);
    true
}

/// Pop a packet from the ring buffer.
/// Returns a reference to the packet data, or None if the ring is empty.
/// The returned slice is valid until the next call to ring_pop().
pub fn ring_pop() -> Option<&'static [u8]> {
    let read_idx = RX_READ_IDX.load(Ordering::Acquire);
    let write_idx = RX_WRITE_IDX.load(Ordering::Acquire);

    if read_idx == write_idx {
        return None;  // Empty
    }

    let len = unsafe { RX_RING_LEN[read_idx] };
    let data = unsafe { &RX_RING[read_idx][..len] };

    RX_READ_IDX.store((read_idx + 1) % RX_RING_SIZE, Ordering::Release);
    Some(data)
}

/// Check if the ring buffer has packets available without consuming them.
pub fn ring_has_packets() -> bool {
    let read_idx = RX_READ_IDX.load(Ordering::Acquire);
    let write_idx = RX_WRITE_IDX.load(Ordering::Acquire);
    read_idx != write_idx
}

/// Get the number of ring buffer overflow events (packets lost because ring was full).
pub fn ring_overflow_count() -> usize {
    unsafe { RX_RING_OVERFLOW }
}

/// Enable ETHMAC RX interrupt at the peripheral level.
/// Must also enable CPU interrupts for this to work.
pub fn enable_rx_interrupt() {
    let ethmac = unsafe { &*litex_pac::Ethmac::ptr() };
    // Clear any pending interrupt first
    ethmac.sram_writer_ev_pending().write(|w| unsafe { w.bits(1) });
    // Enable the "available" event interrupt
    ethmac.sram_writer_ev_enable().write(|w| w.available().set_bit());
    // Set flag so check_and_reenable_interrupt knows interrupts are in use
    unsafe { INTERRUPTS_ENABLED = true; }
}

/// Disable ETHMAC RX interrupt.
pub fn disable_rx_interrupt() {
    let ethmac = unsafe { &*litex_pac::Ethmac::ptr() };
    ethmac.sram_writer_ev_enable().write(|w| w.available().clear_bit());
}

/// Check if interrupts have been enabled via enable_rx_interrupt().
pub fn interrupts_enabled() -> bool {
    unsafe { INTERRUPTS_ENABLED }
}

/// Check if the ISR fired (ev_enable was cleared by ISR).
/// If so, and if MAC is idle (ev_status=0), re-enable the interrupt.
/// Returns true if interrupt was re-enabled.
/// Only does anything if interrupts have been explicitly enabled.
pub fn check_and_reenable_interrupt() -> bool {
    // Only check if interrupts have been enabled
    if unsafe { !INTERRUPTS_ENABLED } {
        return false;
    }

    let ethmac = unsafe { &*litex_pac::Ethmac::ptr() };

    // Check if ev_enable is 0 (ISR disabled it)
    if ethmac.sram_writer_ev_enable().read().bits() == 0 {
        // Re-enable without clearing pending - if packet arrived while disabled,
        // the pending bit is still set and interrupt will fire immediately
        ethmac.sram_writer_ev_enable().write(|w| w.available().set_bit());
        return true;
    }
    false
}

/// Read the debug trap counter (written by the assembly trap handler).
pub fn debug_trap_count() -> u32 {
    unsafe { core::ptr::read_volatile(core::ptr::addr_of!(crate::ISR_COUNTER)) }
}
