//! Bitmap UDP frame receiver.
//!
//! Wire format, little-endian: magic "BM", frame_id u16, chunk_index u8,
//! total_chunks u8, width u16, height u16, then RGB888 pixel bytes. Chunk N
//! carries pixels `N * PIXELS_PER_CHUNK ..`.
//!
//! Loss handling: the receiver tracks *which* chunks arrived in a bitmask
//! rather than merely counting them, so nothing depends on one specific packet
//! turning up. A frame is presented when its mask is full, when the next frame
//! starts arriving, or when it goes stale -- whichever happens first. Chunks
//! that never arrived are patched from the displayed buffer before the swap, so
//! a lost packet costs one frame of staleness in that band instead of two.

use crate::hub75::Hub75;

const HEADER_SIZE: usize = 10;
const MAX_PAYLOAD: usize = 1462;
pub const PIXELS_PER_CHUNK: usize = MAX_PAYLOAD / 3; // 487

/// Pixels in an INDEXED chunk ('B','I'): one byte per pixel instead of three, so
/// the same MTU carries exactly 3x the pixels. That is the whole point -- the
/// streaming bound is per-interrupt cost, not pixels (TODO.md item 9), so a
/// frame going from ~101 chunks to ~34 moves the ceiling by the same 3x.
///
/// Must be `3 * PIXELS_PER_CHUNK`, matching `chunk_pixels` in
/// gateware/dma_writer.py. NOT `MAX_PAYLOAD` -- 1462 is not divisible by 3, so
/// MAX_PAYLOAD would be 1462 against the gateware's 1461 and every chunk past
/// the first would land one pixel further left than the hardware wrote it.
pub const PIXELS_PER_CHUNK_INDEXED: usize = 3 * PIXELS_PER_CHUNK; // 1461

/// chunk_index is a u8, so 256 bits of arrival mask covers the whole index space.
const MAX_CHUNKS: usize = 256;
const MASK_WORDS: usize = MAX_CHUNKS / 64;

/// Missing chunks patched from the front buffer before presenting a frame.
///
/// Each patch is a `PIXELS_PER_CHUNK` SDRAM-to-SDRAM copy -- roughly 0.4 ms on
/// this core -- and it runs in the ISR, which makes this the one place the loss
/// handling spends time exactly when the system is already behind: packets keep
/// arriving during the patch, so an over-generous budget under heavy loss feeds
/// back into more loss. Two caps it below a millisecond.
///
/// Two is also the threshold the pre-v1.10.8 code already used -- it swapped a
/// partial frame when `chunks_count >= total - 2` -- so nothing that used to
/// reach the panel stops reaching it. The difference is that those two chunks
/// are now patched rather than left showing a two-frame-old band. A frame
/// missing more is dropped and the previous frame stays up, as before.
/// Entries in the hardware palette (`gateware/hub75.py`, depth=256).
const PALETTE_LEN: usize = 256;

/// A bitmap frame seen more recently than this means we are STREAMING, so a
/// palette should wait for the swap. Older than this and nothing is going to
/// swap -- the host is doing palette-only animation, which the wire format
/// explicitly supports (a rotated palette is ~778 bytes and animates the whole
/// wall) -- so it must be applied at once or that animation would stall.
const PALETTE_DEFER_MS: i64 = 200;

const MAX_REPAIR_CHUNKS: u16 = 2;

/// Present an in-progress frame if nothing has arrived for it in this long.
///
/// This only has to catch one case: the stream *stops* with a frame part-sent.
/// A frame whose tail was lost while the sender keeps going is already flushed
/// promptly by the next frame's first packet. So the deadline wants to be
/// comfortably longer than any legitimate inter-chunk gap rather than tight --
/// a sender pacing at the old `--delay 0.1` puts 100 ms between chunks, and a
/// deadline below that would present after every single packet.
const STALE_MS: i64 = 250;

#[derive(Clone, Copy)]
pub struct BitmapStats {
    pub packets_total: u32,
    pub packets_valid: u32,
    /// 'B','P' palette packets applied. Counted because a palette that never
    /// arrived and a palette full of black look identical on the wall.
    pub palette_writes: u32,
    pub packets_bad_magic: u32,
    pub packets_bad_header: u32,
    /// Frames whose declared width x height does not match this panel's
    /// configured image. Counted separately from bad_header because a
    /// wrong-SIZED frame is a misconfiguration (a show authored for a
    /// different wall), not a corrupt packet, and the two need different fixes.
    pub packets_bad_size: u32,
    pub packets_duplicate: u32,
    /// Pixel packets the CPU deliberately ignored (see `set_cpu_pixels`).
    pub packets_cpu_skipped: u32,
    /// Worst observed overlap: how many chunks of the NEXT frame the DMA had
    /// already written into the buffer at the moment it was swapped onto the
    /// glass. Anything above 0 is visible as a band of the next frame.
    pub swap_overlap_max: u8,
    pub swap_overlap_last: u8,
    pub frames_completed: u32,
    pub frames_partial: u32,
    pub frames_dropped: u32,
    pub frames_stale: u32,
    pub chunks_repaired: u32,
    pub last_frame_id: u16,
    pub last_chunk_index: u8,
    pub last_total_chunks: u8,
    pub last_width: u16,
    pub last_height: u16,
    pub last_data_len: u16,
    pub last_missing: u16,
    pub chunks_received: u16,
    pub frame_interval_ms: u32,
    pub avg_interval_ms: u32,
    pub jitter_ms: u32,
}

impl BitmapStats {
    pub const fn new() -> Self {
        Self {
            packets_total: 0,
            packets_valid: 0,
            palette_writes: 0,
            packets_bad_magic: 0,
            packets_bad_header: 0,
            packets_bad_size: 0,
            packets_duplicate: 0,
            packets_cpu_skipped: 0,
            swap_overlap_max: 0,
            swap_overlap_last: 0,
            frames_completed: 0,
            frames_partial: 0,
            frames_dropped: 0,
            frames_stale: 0,
            chunks_repaired: 0,
            last_frame_id: 0,
            last_chunk_index: 0,
            last_total_chunks: 0,
            last_width: 0,
            last_height: 0,
            last_data_len: 0,
            last_missing: 0,
            chunks_received: 0,
            frame_interval_ms: 0,
            avg_interval_ms: 0,
            jitter_ms: 0,
        }
    }
}

pub struct BitmapReceiver {
    current_frame_id: u16,
    /// True once the gateware filter is killing pixel packets, which makes the
    /// completed-frame snapshot the ONLY source of frames. Without this the
    /// live path presents each frame a second time -- measured as 80 frames
    /// shown for 40 sent, i.e. two buffer swaps per frame.
    hw_filter_on: bool,
    /// Last completed-frame snapshot presented, so one is never shown twice.
    last_done_seq: u16,
    /// Did a pixel packet the CPU parsed contribute to the frame in progress?
    ///
    /// This is what decides who owns frame changes. While the CPU sees pixel
    /// traffic, `process_packet` flushes and re-identifies on a new frame_id and
    /// `adopt_dma_frame` must keep its hands off. When the CPU sees none -- the
    /// RB-5486 filtered case -- nothing else will ever advance the frame, so
    /// adoption has to. Keying on `received` instead does not work: the DMA
    /// bitmap merges into `received`, so it goes positive on a frame the CPU
    /// never saw a byte of, and adoption locks up one frame behind forever.
    cpu_saw_packet: bool,
    /// False makes the CPU ignore pixel packets, simulating the RB-5486 filter.
    cpu_pixels: bool,
    /// Bit N set once chunk N of the current frame has been written.
    chunk_mask: [u64; MASK_WORDS],
    /// Chunks the current frame expects. 0 means no frame in progress.
    total_chunks: u8,
    /// Wire format of the frame in progress: 'B','I' rather than 'B','M'.
    /// Held per frame because the repair path runs after the packet that set it.
    indexed: bool,
    /// Output mode last pushed to the hub75 ctrl register, so the mode follows
    /// the wire format without a CSR write on every packet. `None` until the
    /// first frame, so the first one always sets it.
    mode_indexed: Option<bool>,
    /// When set, streamed frames are counted but NOT drawn. Without this the
    /// panel's own test patterns are unusable: a pattern is overwritten by the
    /// next streamed frame within one frame period, so it flashes and vanishes.
    hold: bool,
    /// A palette that has arrived but has NOT been written to the palette RAM
    /// yet, so it can be applied against the buffer swap rather than against
    /// whatever is on screen right now. See `stash_palette`.
    pending_palette: [u32; PALETTE_LEN],
    pending_palette_start: u8,
    pending_palette_len: u16,
    pending_palette_valid: bool,
    received: u16,
    last_packet_ms: i64,
    last_complete_ms: i64,
    pub stats: BitmapStats,
}

impl BitmapReceiver {
    pub fn new() -> Self {
        Self {
            current_frame_id: u16::MAX,
            hw_filter_on: false,
            last_done_seq: 0,
            cpu_saw_packet: false,
            cpu_pixels: true,
            chunk_mask: [0; MASK_WORDS],
            total_chunks: 0,
            indexed: false,
            received: 0,
            last_packet_ms: 0,
            mode_indexed: None,
            hold: false,
            pending_palette: [0; PALETTE_LEN],
            pending_palette_start: 0,
            pending_palette_len: 0,
            pending_palette_valid: false,
            last_complete_ms: 0,
            stats: BitmapStats::new(),
        }
    }

    /// Pixels per chunk for the frame in progress. Depends on the wire format.
    fn ppc(&self) -> usize {
        if self.indexed {
            PIXELS_PER_CHUNK_INDEXED
        } else {
            PIXELS_PER_CHUNK
        }
    }

    fn mask_test(&self, idx: u8) -> bool {
        self.chunk_mask[idx as usize >> 6] & (1u64 << (idx & 63)) != 0
    }

    fn mask_set(&mut self, idx: u8) {
        self.chunk_mask[idx as usize >> 6] |= 1u64 << (idx & 63);
    }

    fn mask_clear(&mut self) {
        self.chunk_mask = [0; MASK_WORDS];
    }

    /// Fold the gateware DMA's arrival bitmap into ours, and re-derive
    /// `received` from the result.
    ///
    /// The CPU's mask records packets IT saw. The DMA's records chunks the
    /// hardware actually WROTE. They are not the same set, and the gap is not
    /// small: measured streaming 256x192, hardware wrote 114,702 chunks while
    /// the CPU saw 94,727. Those ~20k chunks were sitting correctly in SDRAM
    /// while the CPU believed they were missing, so `present()` exceeded
    /// `MAX_REPAIR_CHUNKS` on essentially every frame, nothing was ever
    /// swapped, and the wall froze with a correct image already in the
    /// framebuffer while the sender ran at 34 fps.
    ///
    /// Union rather than replacement, deliberately. Today the CPU still
    /// receives every pixel packet, so its mask carries real information and
    /// discarding it could only lose chunks. Once port-7000 traffic stops
    /// reaching the CPU's MAC the CPU's own mask goes empty and the union
    /// degenerates to the DMA's bitmap on its own -- the same code serves both
    /// arrangements, which is why this lands first and separately.
    ///
    /// `received` is recomputed by popcount rather than incremented, so the
    /// count and the mask cannot drift apart.
    /// Public entry for the tight main-loop poll. Only the snapshot path, so it
    /// is safe to call as often as the loop spins.
    pub fn present_done_now(&mut self, hub75: &mut Hub75, time_ms: i64) -> bool {
        if !self.hw_filter_on {
            return false;
        }
        self.present_done_frame(hub75, time_ms)
    }

    /// Make the output mode follow the wire format.
    ///
    /// This used to live in `process_packet`, keyed off the packet magic. With
    /// the gateware pixel filter on, `process_packet` never runs, so nothing
    /// set it and the panel kept whatever mode it was last left in -- RGB888
    /// content displayed as palette indices, which is a mess on the glass while
    /// every counter reads perfectly healthy. No counter can see colour.
    ///
    /// The gateware parses the magic itself and publishes it, so the wire format
    /// is still authoritative; only the source of the answer changed.
    ///
    /// Cached, never read back from the CSR: this is on the frame path and a CSR
    /// read here once slowed things enough to overflow the MAC FIFO.
    fn apply_mode(&mut self, hub75: &mut Hub75, indexed: bool) {
        if self.mode_indexed != Some(indexed) {
            hub75.set_mode(if indexed {
                crate::hub75::OutputMode::Indexed
            } else {
                crate::hub75::OutputMode::FullColor
            });
            self.mode_indexed = Some(indexed);
        }
    }

    /// Present a frame the gateware has already finished, from its snapshot.
    ///
    /// This is the path that makes the RB-5486 filter actually pay. Reading the
    /// LIVE arrival bitmap means racing the next packet: it is cleared the
    /// moment a new frame_id is parsed, so with a 1 kHz sampling loop and a
    /// 0.5 ms inter-packet gap the tail of a frame was missed about half the
    /// time -- 40/40 frames shown at a 10 ms gap, 1/40 at 0.5 ms, with every
    /// chunk sitting correctly in SDRAM. The snapshot is latched before the
    /// clear and holds until the NEXT frame finishes, so there is no deadline.
    ///
    /// Yields to the packet path: while the CPU still sees pixel traffic,
    /// process_packet owns completion and this would only duplicate its work.
    fn present_done_frame(&mut self, hub75: &mut Hub75, time_ms: i64) -> bool {
        let Some((words, fid, total, indexed, seq)) = hub75.dma_done_frame() else {
            return false;
        };
        if total == 0 || seq == self.last_done_seq {
            return false;
        }
        self.last_done_seq = seq;
        if self.cpu_saw_packet {
            return false;
        }
        // Adopt the finished frame wholesale: identity, mask and count all come
        // from the same latched snapshot, so they cannot disagree.
        self.mask_clear();
        let mut count = 0u32;
        for i in 0..MASK_WORDS {
            let w = (words[2 * i] as u64) | ((words[2 * i + 1] as u64) << 32);
            self.chunk_mask[i] = w;
            count += w.count_ones();
        }
        self.current_frame_id = fid;
        self.total_chunks = total;
        self.indexed = indexed;
        self.apply_mode(hub75, indexed);
        self.received = count.min(total as u32) as u16;
        self.stats.chunks_received = self.received;
        self.last_packet_ms = time_ms;
        // Sampled immediately before the swap: the DMA is already writing the
        // next frame into this very buffer, and everything it has written by
        // now will be on the glass.
        let overlap = hub75.dma_last_chunk();
        self.stats.swap_overlap_last = overlap;
        if overlap > self.stats.swap_overlap_max {
            self.stats.swap_overlap_max = overlap;
        }
        self.present(hub75, time_ms)
    }

    /// Take frame identity from the gateware when no packet has supplied it.
    ///
    /// Counterpart to `merge_dma_arrival`. That one answers "which chunks
    /// landed"; this one answers "of which frame, and how many chunks is it" --
    /// and until now both answers came from packets the CPU parsed. Once pixel
    /// traffic is filtered away from the CPU's MAC (RB-5486) no packet supplies
    /// either, and the bitmap alone is useless: `merge_dma_arrival` rejects it
    /// on the `frame_id` guard and nothing is ever presented.
    ///
    /// Deliberately yields to the packet path while it is still working:
    /// `received > 0` means the CPU is mid-frame on packets it parsed, and
    /// `process_packet` already flushes and re-identifies on a frame change. So
    /// this fires only when the CPU has nothing of its own -- which is exactly
    /// the filtered case, and a no-op today.
    fn adopt_dma_frame(&mut self, hub75: &mut Hub75, time_ms: i64) -> bool {
        let Some((hw_frame_id, hw_total, hw_indexed)) = hub75.dma_frame_identity() else {
            return false;
        };
        // The gateware has never parsed a header, so there is no frame to adopt.
        if hw_total == 0 {
            return false;
        }
        if hw_frame_id == self.current_frame_id {
            return false;
        }
        if self.cpu_saw_packet {
            return false;
        }
        // Flush whatever was pending first -- a no-op after a present, since
        // reset_frame() leaves total_chunks at 0 and present() returns early.
        // Runs BEFORE `indexed` is updated, so the outgoing frame is repaired
        // with its own pixels-per-chunk and not the incoming frame's.
        let presented = self.present(hub75, time_ms);
        self.mask_clear();
        self.received = 0;
        self.stats.chunks_received = 0;
        self.current_frame_id = hw_frame_id;
        self.total_chunks = hw_total;
        self.indexed = hw_indexed;
        self.apply_mode(hub75, hw_indexed);
        self.last_packet_ms = time_ms;
        presented
    }

    fn merge_dma_arrival(&mut self, hub75: &Hub75, time_ms: i64) {
        let Some((words, dma_frame_id)) = hub75.dma_arrival() else {
            return;
        };
        // The gateware clears the bitmap when a new frame_id arrives, so a
        // mismatch means it describes a different frame and must be ignored.
        if dma_frame_id != self.current_frame_id {
            return;
        }
        for i in 0..MASK_WORDS {
            let lo = words[2 * i] as u64;
            let hi = (words[2 * i + 1] as u64) << 32;
            self.chunk_mask[i] |= lo | hi;
        }
        let total = self.total_chunks as u32;
        let mut count = 0u32;
        for w in self.chunk_mask.iter() {
            count += w.count_ones();
        }
        // Chunk indices beyond total_chunks cannot legitimately be set, but a
        // corrupt header could set one; clamp so `received >= total` stays a
        // completion test rather than a way to present a short frame.
        let merged = count.min(total) as u16;
        if merged > self.received {
            // Hardware progress IS liveness. last_packet_ms used to be written
            // only by packets the CPU parsed, so with the gateware filter on
            // (RB-5486) it never advanced and tick() judged every frame stale
            // the instant it looked -- 30 frames sent came back 30 stale and 30
            // dropped, while the arrival bitmap showed all 34 chunks present.
            // A chunk landing in SDRAM is activity whoever noticed it.
            self.last_packet_ms = time_ms;
        }
        self.received = merged;
        self.stats.chunks_received = self.received;
    }

    fn update_timing(&mut self, time_ms: i64) {
        if self.last_complete_ms > 0 {
            let interval = (time_ms - self.last_complete_ms) as u32;
            self.stats.frame_interval_ms = interval;
            if self.stats.avg_interval_ms == 0 {
                self.stats.avg_interval_ms = interval;
            } else {
                // EMA: avg = (avg * 7 + new) / 8
                self.stats.avg_interval_ms = (self.stats.avg_interval_ms * 7 + interval) >> 3;
            }
            let avg = self.stats.avg_interval_ms;
            self.stats.jitter_ms = if interval > avg {
                interval - avg
            } else {
                avg - interval
            };
        }
        self.last_complete_ms = time_ms;
    }

    /// Present the frame currently in the back buffer.
    ///
    /// Patches any chunks that never arrived from the displayed buffer, then
    /// swaps. Returns true if a swap happened. Because every presented frame
    /// has each chunk either freshly written or patched, no partial frame can
    /// leave stale data behind for a later frame to inherit.
    fn present(&mut self, hub75: &mut Hub75, time_ms: i64) -> bool {
        if self.total_chunks == 0 {
            return false;
        }
        let expected = self.total_chunks as u16;
        let missing = expected.saturating_sub(self.received);
        self.stats.last_missing = missing;

        if missing > MAX_REPAIR_CHUNKS {
            // Too damaged to show. Leave the previous frame up; the next frame
            // will overwrite this buffer.
            self.stats.frames_dropped += 1;
            self.reset_frame();
            return false;
        }

        // Repair is only safe while the CPU owns the swap. With the gateware
        // swapping at the frame boundary, the "back" buffer is already being
        // filled with the NEXT frame by the time this runs, so patching it
        // would overwrite live pixels with two-frame-old ones -- a whole-frame
        // glitch rather than the band it was meant to hide.
        //
        // It is also largely unnecessary now. `arrival` is set when a chunk's
        // WRITE COMPLETES, so a bit that lands after the frame-change latch
        // leaves the bitmap one short for a frame whose pixels are all present.
        // Repairing that copies stale content over correct content.
        if missing > 0 && !hub75.auto_swap() {
            let img_len = hub75.img_len();
            let ppc = self.ppc();
            for idx in 0..expected {
                if self.mask_test(idx as u8) {
                    continue;
                }
                let offset = idx as usize * ppc;
                if offset >= img_len {
                    continue;
                }
                let len = ppc.min(img_len - offset);
                hub75.repair_from_front(offset, len);
                self.stats.chunks_repaired += 1;
            }
            self.stats.frames_partial += 1;
        } else {
            self.stats.frames_completed += 1;
        }

        // Apply any deferred palette in the same breath as the swap. The
        // palette RAM is single-buffered (depth=256, no spare bank) while the
        // framebuffer is double-buffered, so a palette written when it arrived
        // repainted the frame ALREADY on screen: with content whose palette
        // changes -- a ticking clock re-runs median cut and shifts every entry
        // -- that showed as a visible flicker once per change. Writing it here
        // shrinks the mismatch window from a whole frame period (100ms at
        // 10fps) to the ~256 stores this loop takes.
        self.apply_pending_palette(hub75);
        // With auto_swap the gateware already flipped at the frame boundary, so
        // the CPU only mirrors it. Calling swap_buffers() here would flip a
        // second time and put the frame being written back on screen.
        if hub75.auto_swap() {
            hub75.sync_buffers_from_hw();
        } else {
            hub75.swap_buffers();
        }
        self.update_timing(time_ms);
        self.reset_frame();
        true
    }

    /// Hold a palette until the next swap, or write it now if nothing is
    /// streaming. Returns nothing: the counter is bumped by the caller.
    fn stash_palette(&mut self, hub75: &mut Hub75, start: u8, body: &[u8], time_ms: i64) {
        let streaming = time_ms - self.last_packet_ms < PALETTE_DEFER_MS;
        let room = PALETTE_LEN - start as usize;
        let n = body.chunks_exact(3).count().min(room);
        for (i, c) in body.chunks_exact(3).take(n).enumerate() {
            // Framebuffer/palette word format is 0x00GGRRBB.
            self.pending_palette[i] =
                ((c[1] as u32) << 16) | ((c[0] as u32) << 8) | (c[2] as u32);
        }
        self.pending_palette_start = start;
        self.pending_palette_len = n as u16;
        self.pending_palette_valid = true;
        if !streaming {
            self.apply_pending_palette(hub75);
        }
    }

    fn apply_pending_palette(&mut self, hub75: &mut Hub75) {
        if !self.pending_palette_valid {
            return;
        }
        let n = self.pending_palette_len as usize;
        hub75.set_palette(
            self.pending_palette_start,
            self.pending_palette[..n].iter().copied(),
        );
        self.pending_palette_valid = false;
    }

    fn reset_frame(&mut self) {
        self.mask_clear();
        self.total_chunks = 0;
        self.received = 0;
        self.stats.chunks_received = 0;
        self.cpu_saw_packet = false;
    }

    /// Present an in-progress frame that has gone quiet. Called from the main
    /// loop; without it, a frame whose tail was lost -- or the last frame of a
    /// stream -- would never reach the panel.
    pub fn tick(&mut self, hub75: &mut Hub75, time_ms: i64) -> bool {
        // A palette stashed for a swap that never came -- the stream stopped
        // between the palette and the next frame -- would otherwise sit
        // unapplied forever, leaving the sign in the previous palette.
        if self.pending_palette_valid && time_ms - self.last_packet_ms >= PALETTE_DEFER_MS {
            self.apply_pending_palette(hub75);
        }
        // A frame the gateware has already finished takes priority: it is
        // complete by construction and carries no sampling deadline.
        let mut presented = self.present_done_frame(hub75, time_ms);

        // With the filter on there are no pixel packets to drive the live path,
        // and running it anyway presents every frame a SECOND time -- two swaps
        // per frame, for nothing. The snapshot is the whole story here.
        if self.hw_filter_on {
            return presented;
        }

        // Identity BEFORE the total_chunks guard below, which is the state the
        // CPU sits in permanently once pixel packets stop reaching its MAC
        // (RB-5486): no packet ever sets total_chunks, so tick() would return
        // here forever and the panel would never show a frame again.
        presented |= self.adopt_dma_frame(hub75, time_ms);

        if self.total_chunks == 0 {
            return presented;
        }
        // Fold in what the DMA wrote before judging the frame. A frame the
        // hardware finished but the CPU did not see the tail of would
        // otherwise sit here forever: `received` never reaches `total_chunks`,
        // so `process_packet` never presents it, and the guard below used to
        // discard it as "nothing received" even though its pixels were already
        // in SDRAM.
        self.merge_dma_arrival(hub75, time_ms);

        // Complete on the DMA's own evidence. `process_packet` owns this test
        // while the CPU still sees pixel traffic, but when it does not, no
        // packet will ever arrive to run it -- and falling through to the
        // staleness path below would hold every frame for STALE_MS and count
        // it as stale, which is both slow and a lie.
        if self.received >= self.total_chunks as u16 {
            return self.present(hub75, time_ms) || presented;
        }

        if self.received == 0 {
            return presented;
        }
        if time_ms - self.last_packet_ms < STALE_MS {
            return presented;
        }
        self.stats.frames_stale += 1;
        presented |= self.present(hub75, time_ms);
        presented
    }

    /// Stop (or resume) drawing streamed frames. Packets keep being counted so
    /// the status page can still show that a stream is arriving while held.
    pub fn set_hold(&mut self, on: bool) {
        self.hold = on;
    }

    pub fn is_held(&self) -> bool {
        self.hold
    }

    /// Drop pixel packets in the CPU, as the gateware filter eventually will.
    pub fn set_cpu_pixels(&mut self, on: bool) {
        self.cpu_pixels = on;
        if !on {
            // Whatever half-built frame the CPU held came from packets it will
            // now stop seeing; leaving it would block adopt_dma_frame on its
            // `received > 0` guard until something else cleared it.
            self.mask_clear();
            self.received = 0;
            self.total_chunks = 0;
            self.stats.chunks_received = 0;
            self.cpu_saw_packet = false;
        }
    }

    /// Tell the receiver the gateware filter is on, so it stops running the
    /// live path in parallel with the snapshot path.
    pub fn set_hw_filter(&mut self, on: bool) {
        self.hw_filter_on = on;
    }

    /// Drop any half-built frame, so hardware adoption can take over cleanly.
    pub fn forget_frame(&mut self) {
        self.mask_clear();
        self.received = 0;
        self.total_chunks = 0;
        self.stats.chunks_received = 0;
        self.cpu_saw_packet = false;
    }

    // Read-only views of the guards in present_done_frame(), so
    // /api/dma/arrival can say why it bailed without a firmware flash.
    pub fn dbg_last_done_seq(&self) -> u16 { self.last_done_seq }
    pub fn dbg_cpu_saw_packet(&self) -> bool { self.cpu_saw_packet }
    pub fn dbg_hw_filter_on(&self) -> bool { self.hw_filter_on }
    pub fn dbg_pending_palette(&self) -> bool { self.pending_palette_valid }

    pub fn cpu_pixels(&self) -> bool {
        self.cpu_pixels
    }

    /// Has a bitmap packet arrived recently enough to call the stream live?
    pub fn streaming_recently(&self, now_ms: i64) -> bool {
        self.last_packet_ms != 0 && now_ms.saturating_sub(self.last_packet_ms) < 2000
    }

    /// Forget which output mode we believe the hardware is in.
    ///
    /// Anything that writes the mode CSR outside this receiver must call this,
    /// or the cached guard above will never re-assert and indexed content will
    /// scan out as full colour.
    pub fn invalidate_mode_cache(&mut self) {
        self.mode_indexed = None;
    }

    /// Process one UDP payload. Returns true if a frame was presented.
    pub fn process_packet(&mut self, data: &[u8], hub75: &mut Hub75, time_ms: i64) -> bool {
        self.stats.packets_total += 1;
        self.stats.last_data_len = data.len() as u16;

        // Held: count it, draw nothing. Returning before the mode switch matters
        // too -- otherwise an arriving stream would keep yanking the output mode
        // out from under a test pattern the operator is trying to look at.
        if self.hold {
            return false;
        }

        if data.len() < HEADER_SIZE {
            self.stats.packets_bad_header += 1;
            return false;
        }
        // Pretend the gateware filter of RB-5486 is already there.
        //
        // When `cpu_pixels` is off the CPU drops pixel packets on the floor,
        // exactly as it would if `SmolEthInvalidator` were killing them before
        // the MAC raised an interrupt. Everything downstream then has to run on
        // the gateware's evidence alone: `adopt_dma_frame` for identity and
        // `merge_dma_arrival` for the bitmap. If the panel keeps displaying, the
        // whole filtered path is proven in firmware, over the network, before a
        // line of the classifier exists -- and the failure mode of getting this
        // wrong is a stuck picture rather than a board that cannot be reached.
        //
        // Palette packets ('B','P') are deliberately NOT dropped: the palette is
        // the CPU's job and the gateware filter must spare them too.
        if !self.cpu_pixels && data[0] == 0x42 && (data[1] == 0x4D || data[1] == 0x49) {
            self.stats.packets_cpu_skipped = self.stats.packets_cpu_skipped.wrapping_add(1);
            self.last_packet_ms = time_ms;
            return false;
        }

        // 'B','M' = RGB888, 'B','I' = indexed (1 byte/px). Same header layout;
        // only the stride and the pixel width differ.
        let indexed = match (data[0], data[1]) {
            (0x42, 0x4D) => false,
            (0x42, 0x49) => true,
            (0x42, 0x50) => {
                // 'B','P' -- palette. Header byte 4 is the FIRST entry to write,
                // payload is RGB triples, so a partial update is legal and a
                // full 256-entry palette is 768 bytes: one packet.
                //
                // That makes host-driven palette cycling a ~778-byte frame
                // instead of a full bitmap, which is the same O(1) animation
                // trick the panel can do locally -- see docs/DISPLAY-PROGRAMS.md.
                //
                // set_palette() caps its iterator at 256 - offset, so a
                // oversized or misaddressed packet cannot run off the end.
                let start = data[4];
                let body = &data[HEADER_SIZE..];
                // Held until the swap while a stream is running, so the palette
                // and the frame it belongs to appear together; written straight
                // through when nothing is streaming, so palette-only animation
                // still works. stash_palette() caps at 256 - start, so an
                // oversized or misaddressed packet cannot run off the end.
                self.stash_palette(hub75, start, body, time_ms);
                self.stats.palette_writes += 1;
                return false;
            }
            _ => {
                self.stats.packets_bad_magic += 1;
                return false;
            }
        };
        self.indexed = indexed;

        // The wire format drives the OUTPUT mode. Without this, sending 'B','I'
        // to a panel left in full colour renders every palette index as a blue
        // value -- a dim, plausible-looking picture rather than an obvious
        // fault, which is the worst way for this to fail. Making the magic
        // authoritative also means there is no second setting to get out of
        // sync, and no mode endpoint for a caller to forget.
        //
        // Cached, NOT read back from the CSR. This runs per packet inside the
        // receive ISR, and a CSR read here is bus traffic contending with the
        // pixel DMA: it slowed the ISR enough to overflow the MAC FIFO, so the
        // CPU missed chunks, the arrival mask never filled and NO frame ever
        // completed -- while the DMA went on drawing pixels, so the wire looked
        // perfect and only frames_completed showed it.
        //
        // The cache is only safe because nothing else clobbers the mode behind
        // our back any more; the one remaining external setter (the HTTP test
        // pattern) calls invalidate_mode_cache() so the next packet re-asserts.
        if self.mode_indexed != Some(indexed) {
            hub75.set_mode(if indexed {
                crate::hub75::OutputMode::Indexed
            } else {
                crate::hub75::OutputMode::FullColor
            });
            self.mode_indexed = Some(indexed);
        }

        let frame_id = u16::from_le_bytes([data[2], data[3]]);
        let chunk_index = data[4];
        let total_chunks = data[5];
        let width = u16::from_le_bytes([data[6], data[7]]);
        let height = u16::from_le_bytes([data[8], data[9]]);

        self.stats.last_frame_id = frame_id;
        self.stats.last_chunk_index = chunk_index;
        self.stats.last_total_chunks = total_chunks;
        self.stats.last_width = width;
        self.stats.last_height = height;

        if total_chunks == 0 || chunk_index >= total_chunks {
            self.stats.packets_bad_header += 1;
            return false;
        }

        // Bound the write offset against the framebuffer. Without this a packet
        // claiming a large total_chunks drives chunk_index * PIXELS_PER_CHUNK
        // past the buffer, and the resulting panic resets the SoC.
        let img_len = hub75.img_len();
        let ppc = self.ppc();

        // Reject a frame whose declared geometry is not this panel's. The write
        // paths already clamp, so a wrong size cannot corrupt memory -- but
        // unchecked it is drawn anyway, as a silently garbled or part-filled
        // picture with every counter reading healthy. That is the worst way for
        // a misconfiguration to present: marquee sending a show authored for a
        // different wall looks identical to a hardware fault. Refuse it and say
        // so, so `bad_size` climbing points straight at the show.
        if width != 0 && height != 0 {
            let declared = width as usize * height as usize;
            if declared != img_len {
                self.stats.packets_bad_size += 1;
                return false;
            }
        }

        let max_chunks = (img_len + ppc - 1) / ppc;
        if img_len == 0 || total_chunks as usize > max_chunks {
            self.stats.packets_bad_header += 1;
            return false;
        }

        self.stats.packets_valid += 1;

        let mut presented = false;
        if frame_id != self.current_frame_id || total_chunks != self.total_chunks {
            // Reject frames whose dimensions don't match the configured image.
            // With chain_length 2 layouts the image width is the virtual width
            // (e.g. 256) and the sender must match it, or SDRAM row addressing
            // breaks.
            let (cur_w, cur_len) = hub75.get_img_param();
            let incoming_len = width as u32 * height as u32;
            if cur_w != 0 && (width != cur_w || incoming_len != cur_len) {
                self.stats.packets_bad_header += 1;
                return false;
            }

            // Flush the previous frame before reusing the back buffer.
            presented = self.present(hub75, time_ms);

            self.current_frame_id = frame_id;
            self.total_chunks = total_chunks;
        }

        if self.mask_test(chunk_index) {
            // Duplicate or reordered retransmit. Writing it again would only
            // burn bus cycles, and counting it would corrupt the arrival count.
            self.stats.packets_duplicate += 1;
            self.last_packet_ms = time_ms;
            return presented;
        }

        // Tier 2: when the hardware pixel DMA is enabled it has already written
        // this chunk straight to SDRAM from the same packet. The CPU still does
        // everything else -- arrival mask, completion, repair, swap -- it just
        // stops touching pixels, which is the entire ~600 kpx/s bottleneck.
        if !hub75.dma_enabled() {
            let pixel_offset = chunk_index as usize * ppc;
            if indexed {
                hub75.write_img_indexed(pixel_offset, &data[HEADER_SIZE..]);
            } else {
                hub75.write_img_rgb888(pixel_offset, &data[HEADER_SIZE..]);
            }
        }

        self.mask_set(chunk_index);
        self.received += 1;
        self.cpu_saw_packet = true;
        self.last_packet_ms = time_ms;
        self.stats.chunks_received = self.received;

        // Ask the hardware what it actually wrote before deciding the frame is
        // incomplete. Without this the CPU's own count is the only vote and a
        // frame whose pixels are already in SDRAM can never be presented.
        //
        // NOT on every packet. Measured in the receive ISR: this call is 8 CSR
        // reads plus a full re-popcount of the 256-bit mask, and rv32i has no
        // popcount instruction so every count_ones() is a software loop. At
        // ~11,300 cycles per packet for the whole ISR, doing it per packet is a
        // large slice of the budget to answer a question that only matters when
        // the frame might be finished.
        //
        // The completion test below is the only consumer here, so merge when
        // this packet could complete the frame by the CPU's own reckoning.
        // Anything the CPU missed is still caught by tick(), which merges every
        // main-loop iteration and now completes frames on the DMA's evidence.
        if self.received + 1 >= self.total_chunks as u16 {
            self.merge_dma_arrival(hub75, time_ms);
        }

        if self.received >= self.total_chunks as u16 {
            return self.present(hub75, time_ms) || presented;
        }
        presented
    }
}
