use litex_pac as pac;

const CHAIN_LENGTH: u8 = 1;  // Matches chain_length_2=0 in gateware (1 panel per output)
const OUTPUTS: u8 = 6;

// Growing the wall means changing FOUR things together: the bitstream
// (`--outputs N --chain-length M`), the regenerated PAC, the two constants
// above, and `layout::MAX_OUTPUTS` / `layout::MAX_CHAIN`.
//
// Miss the layout ones and it fails SILENTLY: `LayoutConfig::parse()` tests
// `n <= MAX_OUTPUTS`, so J7+ are dropped with no error and those panels show the
// unassigned default -- which on the wall looks exactly like dead connectors or
// bad cabling, and this board has no serial console to tell you otherwise.
//
// These asserts turn that into a build failure. They cannot see the gateware, so
// they catch firmware-internal disagreement only; a bitstream/firmware mismatch
// still needs the panel-count CSR check noted in docs/DISPLAY-PROGRAMS.md.
const _: () = assert!(
    crate::layout::MAX_OUTPUTS >= OUTPUTS as usize,
    "layout::MAX_OUTPUTS is smaller than hub75::OUTPUTS -- connectors above      MAX_OUTPUTS would be silently dropped from every layout config"
);
const _: () = assert!(
    crate::layout::MAX_CHAIN >= CHAIN_LENGTH as usize,
    "layout::MAX_CHAIN is smaller than hub75::CHAIN_LENGTH -- chained panels      beyond MAX_CHAIN would be silently dropped from every layout config"
);

// Framebuffer layout in SDRAM (addresses in 32-bit words)
const FB_SDRAM_OFFSET_BYTES: u32 = 0x140000;   // = FB_BASE_BYTES in gateware/hub75.py
// 0x400000/2 = 2 MiB of SDRAM for the framebuffer region, /4 = words.
// These comments said 131072 and 65536 until 2026-09-14 -- both 4x low, and
// TODO.md item 4 had already corrected the arithmetic on 2026-09-09 without the
// fix reaching here. The wrong figure implied "8 panels of 128x64 and no
// headroom"; the real numbers leave 16 panels inside half a buffer.
const FB_TOTAL_WORDS: usize = 2 * (0x140000 / 4);  // 2 x FB_HALF_BYTES
const FB_HALF_WORDS: usize = FB_TOTAL_WORDS / 2;    // 327,680 words per buffer

// Sized for a 3x3 wall of 256x128 P1.25 modules -- 768x384 = 294,912 px, with
// 32,768 words spare. The old 262,144 was 12% short of that.
//
// The region moved DOWN rather than growing upward, to put 256 KB between the
// framebuffer and the stack. See gateware/hub75.py for the full map; both files
// must agree or the CPU and the scan-out address different memory.

/// Largest image one framebuffer can hold, in pixels. An image header
/// claiming more than this is not valid -- see img::load_image().
pub const MAX_IMG_PIXELS: usize = FB_HALF_WORDS;
const FB_BASE_WORDS: u32 = FB_SDRAM_OFFSET_BYTES / 4; // 0x80000 - gateware word address

pub struct Hub75 {
    hub75: pac::Hub75,
    /// Back buffer: CPU writes here via write_img_data()
    hub75_data: &'static mut [u32],
    /// Front buffer: HW reads from here for display
    display_buffer: &'static mut [u32],
    hub75_palette: pac::Hub75Palette,
    length: u32,
    /// Which half the HW is currently reading (0 or 1)
    active_buf: u8,
    /// The last palette written, and which generation each bank holds.
    ///
    /// The palette is double-buffered in gateware (RB-5516), and marquee sends
    /// one only WHEN IT CHANGES -- typically once at scene start. Writing just
    /// the back bank would therefore leave the other bank on the previous
    /// scene's colours indefinitely, and since the banks alternate every frame
    /// the panel would flip between right and wrong colours on every frame.
    /// That is worse than the tearing this replaced, so the bank that is not
    /// current gets caught up after the swap instead -- never while it is live.
    palette_shadow: [u32; 256],
    palette_gen: u32,
    bank_gen: [u32; 2],
    auto_swap: bool,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum OutputMode {
    FullColor,
    Indexed,
}

impl Hub75 {
    pub fn new(hub75: pac::Hub75, hub75_palette: pac::Hub75Palette) -> Self {
        let base = (0x90000000u32 + FB_SDRAM_OFFSET_BYTES) as *mut u32;
        let buf0 = unsafe {
            core::slice::from_raw_parts_mut(base, FB_HALF_WORDS)
        };
        let buf1 = unsafe {
            core::slice::from_raw_parts_mut(base.add(FB_HALF_WORDS), FB_HALF_WORDS)
        };

        // HW starts reading from buffer 0 (gateware reset value = FB_BASE_WORDS)
        unsafe { hub75.fb_base().write(|w| w.offset().bits(FB_BASE_WORDS)) };

        Self {
            hub75,
            hub75_data: buf1,       // back buffer (CPU writes here)
            display_buffer: buf0,   // front buffer (HW reads here)
            hub75_palette,
            length: 0,
            active_buf: 0,
            palette_shadow: [0; 256],
            palette_gen: 0,
            bank_gen: [0; 2],
            auto_swap: false,
        }
    }

    /// The raw CSR block, for code that configures the output stage itself
    /// rather than driving pixels through it.
    pub fn regs(&self) -> &pac::Hub75 {
        &self.hub75
    }

    pub fn on(&mut self) {
        self.hub75.ctrl().modify(|_, w| w.enabled().set_bit());
    }

    pub fn is_on(&self) -> bool {
        self.hub75.ctrl().read().enabled().bit()
    }

    pub fn off(&mut self) {
        self.hub75.ctrl().modify(|_, w| w.enabled().clear_bit());
    }

    pub fn set_mode(&mut self, mode: OutputMode) {
        self.hub75.ctrl().modify(|_, w| match mode {
            OutputMode::FullColor => w.indexed().clear_bit(),
            OutputMode::Indexed => w.indexed().set_bit(),
        });
    }

    pub fn get_mode(&mut self) -> OutputMode {
        match self.hub75.ctrl().read().indexed().bit() {
            false => OutputMode::FullColor,
            true => OutputMode::Indexed,
        }
    }

    /// Write pixel data to the back buffer (not yet displayed)
    /// Clamped like every other consumer of `self.length`. The old form indexed
    /// `hub75_data[offset..]` and computed `self.length - offset`, both of which
    /// are wrong for `offset > length`; it survived only because every caller
    /// passes 0 and the release profile leaves overflow checks off.
    /// The back buffer as a writable slice, clamped to the image length.
    ///
    /// For row-at-a-time rendering: a program fills `&mut [u32]` directly
    /// instead of being driven pixel by pixel through an iterator, which
    /// removes the iterator machinery AND the per-pixel indirect call. On a
    /// core with no D-cache the stores dominate, so the fewer layers wrapped
    /// around them the better.
    /// (front, back) at once, for a scroll: copy displayed pixels into the
    /// buffer being drawn. They are separate allocations, so handing out one
    /// shared and one exclusive borrow is sound.
    ///
    /// Shifting the BACK buffer in place would be wrong -- it holds the frame
    /// from two swaps ago, not what is on the glass.
    pub fn buffers(&mut self) -> (&[u32], &mut [u32]) {
        let limit = (self.length as usize)
            .min(self.hub75_data.len())
            .min(self.display_buffer.len());
        (&self.display_buffer[..limit], &mut self.hub75_data[..limit])
    }

    pub fn back_buffer(&mut self) -> &mut [u32] {
        let limit = (self.length as usize).min(self.hub75_data.len());
        &mut self.hub75_data[..limit]
    }

    pub fn write_img_data(&mut self, offset: usize, data: impl Iterator<Item = u32>) {
        let limit = (self.length as usize).min(self.hub75_data.len());
        if offset >= limit {
            return;
        }
        for (sdram, data) in self.hub75_data[offset..limit].iter_mut().zip(data) {
            *sdram = data;
        }
    }

    /// Write INDEXED bytes into the back buffer starting at pixel `offset`.
    ///
    /// One source byte per pixel, written as `0x000000II` -- the low byte is
    /// what the gateware's palette lookup reads (`ram_data & 0x000FF`), so the
    /// framebuffer layout is identical to full colour and nothing downstream
    /// changes.
    ///
    /// This is the CPU fallback; normally `Hub75UdpDma` has already written
    /// these pixels straight from the packet. It is still cheaper than the RGB
    /// path: four pixels are ONE aligned word load instead of three, against the
    /// same four stores, on a bus where every access is a full round-trip
    /// (VexRiscv_Lite, no D-cache).
    ///
    /// Returns the number of pixels written.
    pub fn write_img_indexed(&mut self, offset: usize, src: &[u8]) -> usize {
        let limit = (self.length as usize).min(self.hub75_data.len());
        if offset >= limit {
            return 0;
        }
        let n_px = src.len().min(limit - offset);
        if n_px == 0 {
            return 0;
        }
        let dst = &mut self.hub75_data[offset..offset + n_px];
        if src.as_ptr() as usize % 4 == 0 {
            let words = src.as_ptr() as *const u32;
            let groups = n_px / 4;
            for g in 0..groups {
                let w = unsafe { core::ptr::read_volatile(words.add(g)) };
                let d = &mut dst[g * 4..g * 4 + 4];
                d[0] = w & 0xFF;
                d[1] = (w >> 8) & 0xFF;
                d[2] = (w >> 16) & 0xFF;
                d[3] = (w >> 24) & 0xFF;
            }
            for i in (groups * 4)..n_px {
                dst[i] = src[i] as u32;
            }
        } else {
            for i in 0..n_px {
                dst[i] = src[i] as u32;
            }
        }
        n_px
    }

    /// Write RGB888 bytes into the back buffer starting at pixel `offset`.
    ///
    /// Reads the source 32 bits at a time. `src` points into the Ethernet MAC's
    /// slot buffer, and this SoC's VexRiscv_Lite has no data cache (only
    /// CONFIG_CPU_HAS_ICACHE), so every load is a full bus round-trip: reading
    /// byte-at-a-time costs three of them per pixel. Four pixels are twelve
    /// bytes -- exactly three aligned words -- so this form issues 3 loads + 4
    /// stores per 4 pixels instead of 12 loads + 4 stores.
    ///
    /// The word path needs 4-byte alignment. It always holds for a bitmap UDP
    /// packet read in place out of a 2048-aligned MAC slot: eth(14) + ip(4*IHL)
    /// + udp(8) + bitmap header(10) is a multiple of 4 for every legal IHL.
    /// The check is kept anyway so a caller passing anything else stays correct.
    ///
    /// Returns the number of pixels written.
    pub fn write_img_rgb888(&mut self, offset: usize, src: &[u8]) -> usize {
        let limit = (self.length as usize).min(self.hub75_data.len());
        if offset >= limit {
            return 0;
        }
        let n_px = (src.len() / 3).min(limit - offset);
        if n_px == 0 {
            return 0;
        }
        let dst = &mut self.hub75_data[offset..offset + n_px];
        if src.as_ptr() as usize % 4 == 0 {
            let words = src.as_ptr() as *const u32;
            let groups = n_px / 4;
            for g in 0..groups {
                // payload bytes, little-endian:
                //   w0 = R0 G0 B0 R1   w1 = G1 B1 R2 G2   w2 = B2 R3 G3 B3
                let (w0, w1, w2) = unsafe {
                    (
                        core::ptr::read_volatile(words.add(g * 3)),
                        core::ptr::read_volatile(words.add(g * 3 + 1)),
                        core::ptr::read_volatile(words.add(g * 3 + 2)),
                    )
                };
                // HUB75 wants 0x00BBGGRR -- B<<16 | G<<8 | R. Measured on the
                // bench panel: the old 0x00GGRRBB lit GREEN for pure red,
                // BLUE for pure green and RED for pure blue.
                let i = g * 4;
                dst[i] = ((w0 >> 16) & 0xFF) << 16 | ((w0 >> 8) & 0xFF) << 8 | (w0 & 0xFF);
                dst[i + 1] = ((w1 >> 8) & 0xFF) << 16 | (w1 & 0xFF) << 8 | ((w0 >> 24) & 0xFF);
                dst[i + 2] = (w2 & 0xFF) << 16 | ((w1 >> 24) & 0xFF) << 8 | ((w1 >> 16) & 0xFF);
                dst[i + 3] = ((w2 >> 24) & 0xFF) << 16 | ((w2 >> 16) & 0xFF) << 8 | ((w2 >> 8) & 0xFF);
            }
            for i in (groups * 4)..n_px {
                let c = &src[i * 3..i * 3 + 3];
                dst[i] = (c[2] as u32) << 16 | (c[1] as u32) << 8 | (c[0] as u32);
            }
        } else {
            for i in 0..n_px {
                let c = &src[i * 3..i * 3 + 3];
                dst[i] = (c[2] as u32) << 16 | (c[1] as u32) << 8 | (c[0] as u32);
            }
        }
        n_px
    }

    /// Copy a pixel range from the front (displayed) buffer into the back buffer.
    ///
    /// Used to patch chunks that never arrived before presenting a frame. Without
    /// this a lost packet leaves that band holding whatever the back buffer had,
    /// which under double buffering is the frame from *two* frames ago; after it,
    /// the band is one frame old and effectively invisible in motion.
    pub fn repair_from_front(&mut self, offset: usize, len: usize) {
        let limit = (self.length as usize)
            .min(self.hub75_data.len())
            .min(self.display_buffer.len());
        if offset >= limit || len == 0 {
            return;
        }
        let end = (offset + len).min(limit);
        self.hub75_data[offset..end].copy_from_slice(&self.display_buffer[offset..end]);
    }

    /// Configured image length in pixels.
    pub fn img_len(&self) -> usize {
        (self.length as usize).min(self.hub75_data.len())
    }

    /// Swap front and back buffers. The back buffer becomes visible and
    /// the old front buffer becomes available for writing.
    /// Hand the framebuffer swap to the gateware, at the frame boundary.
    ///
    /// With this on, `swap_buffers()` stops writing `fb_base` and only mirrors
    /// what the hardware already did. See the comment on `auto_swap` in
    /// `gateware/hub75.py`: no CPU polling rate is fast enough, because the
    /// correct instant is between the last pixel of one frame and the first of
    /// the next, and only the gateware is there.
    /// Which logical colour each HUB75 pin group carries (0..=5, see
    /// layout::RGB_ORDERS). The panel's wiring decides it; a wrong value shows
    /// as wrong hues with every counter clean, so it is worth being able to
    /// change without a rebuild.
    ///
    /// Clamped rather than trusted: this arrives from a config file, and a
    /// value past the end of the gateware's Case would select nothing and
    /// blank the colour entirely.
    pub fn set_rgb_order(&mut self, order: u8) {
        let o = if order > 5 { 0 } else { order };
        unsafe { self.hub75.rgb_order().write(|w| w.bits(o as u32)) };
    }

    pub fn rgb_order(&self) -> u8 {
        self.hub75.rgb_order().read().bits() as u8
    }

    pub fn set_auto_swap(&mut self, on: bool) {
        self.auto_swap = on;
        unsafe { self.hub75.auto_swap().write(|w| w.bits(if on { 1 } else { 0 })) };
    }

    pub fn auto_swap(&self) -> bool {
        self.auto_swap
    }

    /// Follow the hardware's choice of live half without writing the CSR.
    ///
    /// The software slices are what `repair_from_front()` and `read_img_data()`
    /// read, so they have to track whichever half the gateware put on screen or
    /// those read the wrong memory.
    pub fn sync_buffers_from_hw(&mut self) {
        let now = unsafe { self.hub75.fb_base_now().read().bits() };
        let hw_active = if now == FB_BASE_WORDS { 0 } else { 1 };
        if hw_active != self.active_buf {
            core::mem::swap(&mut self.hub75_data, &mut self.display_buffer);
            self.active_buf = hw_active;
        }
    }

    /// Flip the framebuffer from the CPU.
    ///
    /// ALWAYS writes `fb_base`, including when `auto_swap` is on. The gateware
    /// only flips on a pixel-DMA frame boundary, so it covers the streaming path
    /// and nothing else: on-panel programs, test patterns and animations all
    /// draw from the CPU and would never reach the glass if this deferred to
    /// hardware. Making it defer was a v1.39.0 regression that left program mode
    /// drawing into the back buffer forever.
    ///
    /// The streaming path does NOT call this -- `present()` calls
    /// `sync_buffers_from_hw()` instead, because by then the gateware has
    /// already flipped. A CSR write wins over `auto_swap` in `hub75.py`, so the
    /// two cannot fight.
    pub fn swap_buffers(&mut self) {
        core::mem::swap(&mut self.hub75_data, &mut self.display_buffer);
        self.active_buf ^= 1;
        let base = if self.active_buf == 1 {
            FB_BASE_WORDS + FB_HALF_WORDS as u32
        } else {
            FB_BASE_WORDS
        };
        // One register flips both: the gateware derives the pixel DMA's write
        // address from fb_base, so the display and the DMA can no longer
        // disagree about which half is live. Updating them as two separate CSR
        // writes left a window in which a chunk header latched into the buffer
        // being displayed.
        unsafe { self.hub75.fb_base().write(|w| w.offset().bits(base)) };
    }

    /// Read pixel data from the front buffer (what's currently displayed)
    pub fn read_img_data(&'_ self) -> impl Iterator<Item = u32> + '_ {
        // Clamp like every other consumer of self.length; unguarded this
        // panics on a bad image header, and a panic reboots the SoC.
        let limit = (self.length as usize).min(self.display_buffer.len());
        self.display_buffer[0..limit].iter().copied()
    }

    /// Overlay the firmware version in the top-left of the back buffer.
    ///
    /// Called at boot so the panel itself reports which build is running.
    pub fn draw_version_banner(&mut self, width: usize) {
        if width == 0 {
            return;
        }
        let limit = (self.length as usize).min(self.hub75_data.len());
        let text_w = (crate::patterns::VERSION_TEXT.len() * 6 + 4).min(width);
        for row in 0..11usize {
            for col in 0..text_w {
                let idx = row * width + col;
                if idx >= limit {
                    return;
                }
                if crate::patterns::boot_version_pixel(col, row) {
                    self.hub75_data[idx] = 0x00FF_FFFF;
                } else {
                    self.hub75_data[idx] = 0;
                }
            }
        }
    }

    /// Publish the image bound the hardware DMA clamps against.
    ///
    /// Must be re-published whenever `self.length` changes, not just when the
    /// DMA is switched on. `set_dma_enabled` used to be the only writer, so a
    /// layout change after enabling left the DMA clamping against the *old*
    /// image: every pixel past the stale bound was silently discarded while the
    /// CPU's arrival mask still reported the chunk as present, leaving a region
    /// of the display permanently dead.
    fn publish_dma_limit(&self) {
        let limit = (self.length as usize).min(self.hub75_data.len()) as u32;
        unsafe {
            let p = litex_pac::Peripherals::steal();
            p.pixdma.limit().write(|w| w.bits(limit));
        }
    }

    /// Enable/disable hardware pixel writes.
    pub fn set_dma_enabled(&mut self, on: bool) {
        self.publish_dma_limit();
        unsafe {
            let p = litex_pac::Peripherals::steal();
            p.pixdma.ctrl().write(|w| w.enable().bit(on));
        }
    }

    pub fn dma_enabled(&self) -> bool {
        unsafe {
            let p = litex_pac::Peripherals::steal();
            p.pixdma.ctrl().read().enable().bit_is_set()
        }
    }

    /// The gateware's own chunk-arrival bitmap, and the frame_id it describes.
    ///
    /// `None` when the DMA is off, because the bitmap is then stale.
    ///
    /// This is the DMA's record of chunks it actually WROTE to SDRAM, which is
    /// not the same set as the packets the CPU received. Under load the two
    /// diverge badly in BOTH directions: `AlwaysReady` in `smoleth.py` drops
    /// whole packets rather than backpressure the CPU's ethernet, and the DMA
    /// watchdog abandons a payload if the DRAM writer stalls -- neither visible
    /// to the CPU. Measured streaming 256x192: hardware wrote 114,702 chunks
    /// while the CPU saw 94,727.
    ///
    /// Read `frame_id` FIRST and check it against the frame being assembled.
    /// The bitmap is cleared by the gateware when a new frame_id arrives, so a
    /// bitmap read without that check can belong to the next frame.
    pub fn dma_arrival(&self) -> Option<([u32; 8], u16)> {
        if !self.dma_enabled() {
            return None;
        }
        unsafe {
            let p = litex_pac::Peripherals::steal();
            let frame_id = p.pixdma.frame_id().read().bits() as u16;
            let words = [
                p.pixdma.arrival0().read().bits(),
                p.pixdma.arrival1().read().bits(),
                p.pixdma.arrival2().read().bits(),
                p.pixdma.arrival3().read().bits(),
                p.pixdma.arrival4().read().bits(),
                p.pixdma.arrival5().read().bits(),
                p.pixdma.arrival6().read().bits(),
                p.pixdma.arrival7().read().bits(),
            ];
            Some((words, frame_id))
        }
    }

    /// The bitmap of the frame that just FINISHED, latched by the gateware.
    ///
    /// Returns (words, frame_id, total_chunks, indexed, seq).
    ///
    /// The live `arrival` bitmap is cleared the instant a new frame_id is
    /// parsed, so reading it means winning a race against the next packet --
    /// one inter-packet time, which at 13 fps on a 9-panel frame is 0.5 ms
    /// against a 1 kHz sampling loop. This snapshot is latched before that
    /// clear and stays put until the frame after it finishes, so there is no
    /// race to lose.
    ///
    /// `seq` increments per latch and is the only safe way to tell a new
    /// snapshot from one already presented: frame_id wraps at 16 bits.
    pub fn dma_done_frame(&self) -> Option<([u32; 8], u16, u8, bool, u16)> {
        if !self.dma_enabled() {
            return None;
        }
        unsafe {
            let p = litex_pac::Peripherals::steal();
            // seq first and last: if it moved under us the words are a mix of
            // two frames, so the caller should ignore this read and come back.
            let seq0 = p.pixdma.done_seq().read().bits() as u16;
            let words = [
                p.pixdma.done0().read().bits(),
                p.pixdma.done1().read().bits(),
                p.pixdma.done2().read().bits(),
                p.pixdma.done3().read().bits(),
                p.pixdma.done4().read().bits(),
                p.pixdma.done5().read().bits(),
                p.pixdma.done6().read().bits(),
                p.pixdma.done7().read().bits(),
            ];
            let fid = p.pixdma.frame_id_done().read().bits() as u16;
            let tot = p.pixdma.total_chunks_done().read().bits() as u8;
            let idx = p.pixdma.indexed_done().read().bits() != 0;
            if p.pixdma.done_seq().read().bits() as u16 != seq0 {
                return None;
            }
            Some((words, fid, tot, idx, seq0))
        }
    }

    /// Frame identity as the GATEWARE parsed it: (frame_id, total_chunks, indexed).
    ///
    /// `bitmap_udp.rs` normally learns these from packet headers it parses
    /// itself. That breaks the moment pixel packets stop reaching the CPU's MAC
    /// (RB-5486): `current_frame_id` would stay `u16::MAX`, `merge_dma_arrival`
    /// would reject every bitmap as describing another frame, and the display
    /// would freeze with correct pixels already in SDRAM.
    ///
    /// The gateware parses the same 10-byte header for its own use and commits
    /// all three together at `hdr_idx == 9`, so a reader can never pair one
    /// frame's `total_chunks` with another's `frame_id`.
    ///
    /// `total_chunks == 0` means the gateware has not parsed a header yet.
    pub fn dma_frame_identity(&self) -> Option<(u16, u8, bool)> {
        if !self.dma_enabled() {
            return None;
        }
        unsafe {
            let p = litex_pac::Peripherals::steal();
            Some((
                p.pixdma.frame_id().read().bits() as u16,
                p.pixdma.total_chunks().read().bits() as u8,
                p.pixdma.indexed().read().bits() != 0,
            ))
        }
    }

    /// Most recent chunk_index the DMA parsed. Read at swap time it says how
    /// far into the NEXT frame the hardware already is, i.e. how much of the
    /// buffer about to be displayed has already been overwritten.
    pub fn dma_last_chunk(&self) -> u8 {
        unsafe { litex_pac::Peripherals::steal().pixdma.last_chunk().read().bits() as u8 }
    }

    /// (pixels, chunks, bad_magic) written by the hardware DMA.
    pub fn dma_stats(&self) -> (u32, u32, u32) {
        unsafe {
            let p = litex_pac::Peripherals::steal();
            (p.pixdma.pixels().read().bits(),
             p.pixdma.chunks().read().bits(),
             p.pixdma.bad_magic().read().bits())
        }
    }

    /// Payloads the pixel DMA abandoned after the SDRAM write side stalled.
    ///
    /// Worth reading beside `c_dropped`, which counts packets the ethernet
    /// gate abandoned because the burst FIFO was full. The two answer
    /// different questions and want different fixes: gate drops with this at
    /// zero means the FIFO is too shallow for a momentary stall, while this
    /// climbing means the write side is genuinely starved -- most likely by
    /// the display re-reading the whole framebuffer every refresh, which is
    /// the dominant SDRAM load. RB-5524.
    pub fn dma_stalls(&self) -> u32 {
        unsafe { litex_pac::Peripherals::steal().pixdma.stalls().read().bits() }
    }

    pub fn set_img_param(&mut self, width: u16, length: u32) {
        unsafe { self.hub75.ctrl().modify(|_, w| w.width().bits(width)) };
        self.length = length;
        // The DMA clamps against this bound in hardware, so it has to follow
        // every image-size change -- layout apply, telnet `panel`, /api/layout.
        self.publish_dma_limit();
    }

    pub fn get_img_param(&self) -> (u16, u32) {
        let width = self.hub75.ctrl().read().width().bits();
        (width, self.length)
    }

    /// Completed framebuffer refreshes since reset.
    ///
    /// The display DMA re-reads the WHOLE framebuffer every refresh, so
    /// sampling this over a known interval gives the true refresh rate, and from
    /// it the SDRAM read bandwidth the display is actually achieving. That
    /// number decides how much headroom a write DMA can have.
    pub fn refresh_count(&self) -> u32 {
        self.hub75.refresh_count().read().bits()
    }

    pub fn get_panel_params(&self) -> impl Iterator<Item = u32> + '_ {
        use pac::hub75::Panel0_0;
        let panel_adr = self.hub75.panel0_0() as *const Panel0_0 as *const u32;
        let panel_reg: &[u32] =
            unsafe { core::slice::from_raw_parts(panel_adr, (OUTPUTS * CHAIN_LENGTH) as usize) };
        panel_reg.iter().copied()
    }

    pub fn set_panel_params(&mut self, params: impl Iterator<Item = u32>) {
        use pac::hub75::Panel0_0;
        let panel_adr = self.hub75.panel0_0() as *const Panel0_0;
        let panel_reg: &[Panel0_0] =
            unsafe { core::slice::from_raw_parts(panel_adr, (OUTPUTS * CHAIN_LENGTH) as usize) };
        for (reg, data) in panel_reg.iter().zip(params) {
            unsafe { reg.write(|w| w.bits(data)) };
        }
    }

    pub fn set_panel_param(&mut self, output: u8, chain_num: u8, x: u8, y: u8, rot: u8) {
        if output >= OUTPUTS || chain_num >= CHAIN_LENGTH {
            return;
        }
        use pac::hub75::Panel0_0;
        let chain_offset = (output * CHAIN_LENGTH + chain_num) as usize;
        let panel_adr = self.hub75.panel0_0() as *const Panel0_0;
        let panel_reg: &[Panel0_0] =
            unsafe { core::slice::from_raw_parts(panel_adr, (OUTPUTS * CHAIN_LENGTH) as usize) };
        unsafe { panel_reg[chain_offset].write(|w| w.x().bits(x).y().bits(y).rot().bits(rot)) };
    }

    pub fn get_panel_param(&mut self, output: u8, chain_num: u8) -> (u8, u8, u8) {
        if output >= OUTPUTS || chain_num >= CHAIN_LENGTH {
            return (255, 255, 255);
        }
        use pac::hub75::Panel0_0;
        let chain_offset = (output * CHAIN_LENGTH + chain_num) as usize;
        let panel_adr = self.hub75.panel0_0() as *const Panel0_0;
        let panel_reg: &[Panel0_0] =
            unsafe { core::slice::from_raw_parts(panel_adr, (OUTPUTS * CHAIN_LENGTH) as usize) };
        let data = panel_reg[chain_offset].read();
        (data.x().bits(), data.y().bits(), data.rot().bits())
    }

    /// Palette banks this bitstream has: 2 = double-buffered, 1 = shared.
    ///
    /// Read from the gateware rather than assumed, because the firmware
    /// netboots and the bitstream lives in flash -- the two are not updated
    /// together and a new firmware will meet old gateware. An absent CSR reads
    /// 0 on this SoC, which falls through to the single-bank path, so the old
    /// behaviour is what an old bitstream gets.
    pub fn palette_banks(&self) -> u8 {
        let n = unsafe { self.hub75.hw_palette_banks().read().bits() } as u8;
        if n == 0 { 1 } else { n }
    }

    /// Which 256-entry bank belongs to the buffer being DRAWN into.
    ///
    /// `active_buf` is the half on the glass; the palette bank follows
    /// `fb_base_eff` in gateware, so the bank for the back buffer is the other
    /// one. Writing there means the new colours become live at the same instant
    /// as the pixels that index them -- which is the entire point (RB-5516).
    fn back_palette_bank(&self) -> usize {
        if self.palette_banks() < 2 {
            return 0;
        }
        // Read the HARDWARE, not `active_buf`. With `auto_swap` on, the gateware
        // flips at a frame boundary and the CPU does not learn about it until
        // the next present(), so the cached flag can be one frame stale -- and a
        // palette written into the live bank is precisely the artifact this is
        // here to remove. One CSR read, eight cycles, once per palette write.
        let live = unsafe { self.hub75.fb_base_now().read().bits() };
        if live == FB_BASE_WORDS { 1 } else { 0 }
    }

    /// The bank the scan-out is reading right now.
    pub fn live_palette_bank(&self) -> usize {
        if self.palette_banks() < 2 { 0 } else { self.back_palette_bank() ^ 1 }
    }

    /// Which half is being DRAWN into: 0 or 1.
    ///
    /// Read from the gateware, not from `active_buf`, for the same reason the
    /// palette bank is: with auto_swap on, the flip happens without the CPU
    /// being told. A progressive repaint has to know which buffer it is filling
    /// or it fills one of them twice and leaves the other stale forever.
    pub fn back_index(&self) -> usize {
        if self.fb_base_now() == FB_BASE_WORDS { 1 } else { 0 }
    }

    pub fn fb_base_now(&self) -> u32 {
        unsafe { self.hub75.fb_base_now().read().bits() }
    }

    /// One bank as a readable slice, for diagnostics.
    pub fn palette_bank(&self, bank: usize) -> &'_ [u32] {
        const BANK: usize = 256;
        let total = BANK * self.palette_banks() as usize;
        use pac::hub75_palette::Hub75Palette;
        let adr = self.hub75_palette.hub75_palette() as *const Hub75Palette as *const u32;
        let all: &[u32] = unsafe { core::slice::from_raw_parts(adr, total) };
        let base = (bank * BANK).min(total);
        &all[base..(base + BANK).min(total)]
    }

    pub fn set_palette(&mut self, offset: u8, data: impl Iterator<Item = u32>) {
        let n = self.stash(offset, data);
        if n == 0 {
            return;
        }
        let bank = self.back_palette_bank();
        self.write_bank(bank, offset, n);
        // This bank is current; the other one is now behind and will be caught
        // up by sync_palette_bank() once it stops being displayed.
        self.bank_gen[bank & 1] = self.palette_gen;
    }

    /// Bring the bank being drawn into up to date with the last palette written.
    ///
    /// Called from the STREAMING present path, after the gateware has flipped.
    /// A no-op unless a palette arrived since this bank was last written, so on
    /// the usual case -- one palette per scene -- it costs one comparison a
    /// frame and 256 words once.
    ///
    /// Not called from `program_tick`: a program either writes an animated
    /// palette into its own frame's bank every tick, or writes a static one
    /// into every bank once. Neither needs catching up.
    pub fn sync_palette_bank(&mut self) {
        if self.palette_banks() < 2 || self.palette_gen == 0 {
            return;
        }
        let bank = self.back_palette_bank();
        if self.bank_gen[bank & 1] == self.palette_gen {
            return;
        }
        self.write_bank(bank, 0, 256);
        self.bank_gen[bank & 1] = self.palette_gen;
    }

    /// Record a palette into the shadow. Returns how many entries were taken.
    fn stash(&mut self, offset: u8, data: impl Iterator<Item = u32>) -> usize {
        let start = offset as usize;
        let mut n = 0;
        for (i, v) in data.take(256 - start.min(256)).enumerate() {
            self.palette_shadow[start + i] = v;
            n = i + 1;
        }
        if n > 0 {
            self.palette_gen = self.palette_gen.wrapping_add(1);
        }
        n
    }

    fn write_bank(&mut self, bank: usize, offset: u8, count: usize) {
        let start = offset as usize;
        let end = (start + count).min(256);
        let vals: [u32; 256] = self.palette_shadow;
        self.set_palette_bank(bank, offset, vals[start..end].iter().copied());
    }

    /// Write the same palette into EVERY bank.
    ///
    /// For a palette that does not change between frames. `set_palette` only
    /// fills the bank being drawn into, so a static palette written once would
    /// leave the other bank at whatever it held -- black on a fresh boot -- and
    /// the panel would alternate between the right colours and nothing. Two
    /// banks is 512 words, once, so there is no reason to be clever about it.
    pub fn set_palette_all(&mut self, offset: u8, f: impl Fn(usize) -> u32) {
        let n = self.stash(offset, (0..256usize).map(&f));
        for bank in 0..self.palette_banks() as usize {
            self.write_bank(bank, offset, n);
            self.bank_gen[bank & 1] = self.palette_gen;
        }
    }

    fn set_palette_bank(&mut self, bank: usize, offset: u8, data: impl Iterator<Item = u32>) {
        const BANK: usize = 256;
        use pac::hub75_palette::Hub75Palette;
        let total = BANK * self.palette_banks() as usize;
        let palette_adr = self.hub75_palette.hub75_palette() as *const Hub75Palette;
        let palette_data: &[Hub75Palette] =
            unsafe { core::slice::from_raw_parts(palette_adr, total) };
        let base = bank * BANK + offset as usize;
        if base >= total {
            return;
        }
        for (index, data) in data.take(total - base).enumerate() {
            unsafe { palette_data[base + index].write(|w| w.bits(data)) };
        }
    }

    /// Read bitstream hardware parameters from CSRStatus registers.
    /// Returns (columns, rows, scan, chain_length_2, n_outputs).
    pub fn get_hw_info(&self) -> (u16, u16, u8, u8, u8) {
        let columns = self.hub75.hw_columns().read().bits() as u16;
        let rows = self.hub75.hw_rows().read().bits() as u16;
        let config = self.hub75.hw_config().read().bits() as u16;
        let scan = (config & 0xFF) as u8;
        let chain_length_2 = ((config >> 8) & 0xF) as u8;
        let n_outputs = ((config >> 12) & 0xF) as u8;
        (columns, rows, scan, chain_length_2, n_outputs)
    }

    pub fn get_palette(&mut self) -> &'_ [u32] {
        const LENGTH: usize = 256;
        use pac::hub75_palette::Hub75Palette;
        let palette_adr = self.hub75_palette.hub75_palette() as *const Hub75Palette as *const u32;
        let palette_data: &[u32] = unsafe { core::slice::from_raw_parts(palette_adr, LENGTH) };
        palette_data
    }

}
