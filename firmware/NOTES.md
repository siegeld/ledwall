# Development Notebook

A reverse-chronological log of things that went wrong and what they turned out
to be. It is kept because **the diagnosis is usually worth more than the fix** —
several entries below record a confident hypothesis that measurement then
demolished, and those are the parts most likely to save someone else a day.

Newest first. Not a reference: see `ARCH.md` for how the system works,
`docs/BENCHMARKS.md` for what it costs, and `docs/HARDWARE.md` for the board.

---

## 2026-09-17

### Cold boot: 20.6 s → 6.2 s, and two sessions of chasing the wrong thing

**The board always configured from flash at power-on.** The "cold power-on does
not configure the FPGA" investigation, which ran for two sessions and ended with
"needs a scope", was aimed at a failure that does not exist. The board booted,
ran the BIOS, and stalled at `netboot()` — one attempt, no link wait, no retry.

Everything used as an indicator sat *downstream* of netboot: a TFTP request, a
ping to the DHCP address, pixels on the panels. A healthy board that stalled one
step late is indistinguishable from one that never configured. The discriminator
was `.250` — the BIOS's own compile-time IP, MAC `10:e2:d5:00:00:00` — and it was
never probed.

**Defect 1: the firmware in flash had no FBI header.** `flash-firmware` wrote
the raw `boot.bin`, so `check_image_in_flash()` read the first RISC-V
instruction, `0x400000b7`, as a 1 GB length and rejected it. Every time, since
2026-09-06. `flashboot()` had therefore *never once succeeded*, and netboot only
looked like it "took precedence" because the path ahead of it was broken. Fixed
by wrapping with `crcfbigen.py -f -l` and refusing to flash a bad header.

**Then `deploy` stopped working**, because a panel with good firmware in flash
never fetches the deployed image. Fixed by reordering the BIOS to try netboot
first and fall through to flash — which works precisely *because* netboot fails
fast when the link is not up (0.32 s, measured).

**Defect 2: reading flash took 18 seconds.** `flashboot()` walks the image three
times — `check_image_in_flash()` runs in `flashboot()` and **again** inside
`copy_image_from_flash_to_ram()` at `boot.c:623`, so the CRC is computed twice,
then the image is copied. The `spiflash` region is `cached=False` with
`READ_1_1_1` at LiteSPI's `default_divisor=9` (2 MHz), so every byte is a full
40-clock transaction: **1142 cycles per byte**, against 14 from DRAM.
`default_divisor=1` → 10 MHz → flashboot 3.90 s, cold boot 6.2 s.

### What the method got wrong, which is the part worth keeping

- **Every "it takes 30 seconds" figure was `last ping reply → first ping reply`,
  which contains however long the plug was out.** That was treated as zero for
  hours and produced a 20-second phantom. The fix was a marked power-on: user
  says "in" at the instant of contact, and the packet capture supplies the rest.
- **Two confident hypotheses died on contact with measurement.** STP forwarding
  delay (30 s fits suspiciously well — and was ruled out immediately by someone
  who knew the switch port was not running STP);
  and `netboot()`'s ARP retries, which look enormous written out — 2 × 8 tries ×
  100,000 spins — and cost 320 ms, because idle `udp_service()` is one CSR read
  at 8 cycles.
- **`mcycle` reads back 0** on this VexRiscv, so `/api/bench` has reported all
  zeros since it was written, under a comment explaining why it uses `mcycle`
  rather than `TIME_MS`. Use `Timer0`.
- **`Timer0` wraps every second.** The first full-image divisor sweep reported
  divisor 9 at 247 ms — *faster* than every faster divisor — because the real
  1248.8 ms aliased. Time long things in chunks and sum.
- **`clk_divisor` is a `CSRStorage`**, so the SPI clock can be swept at runtime.
  The candidate clock was proven on real hardware — full 292,976-byte image,
  identical checksum at every divisor including 20 MHz — before a bitstream was
  built for it. 10 MHz shipped anyway, for margin.

### Also found on the way

- **`build.sh` defaulted to `CHAIN_LENGTH=2`** from v1.7.0 until today, while
  `hub75.rs` has had `const CHAIN_LENGTH: u8 = 1` since v1.34.0. Any plain
  `./build.sh bitstream` built a gateware the firmware contradicted — it builds,
  boots and looks fine. Now read out of `hub75.rs` and enforced.
- **The JTAG link corrupts reads in both directions.** `--verify` failed seven
  times running at seven different offsets; dump-and-compare showed the *write*
  was correct every time. Verify by dumping and comparing hashes until one read
  comes back clean — do not trust a failed verify, and do not trust a single
  passing read either.

---

## 2026-02-09

### HTTP Connection Close Fix (v1.10.5)

**Problem:** Chrome tab spinner never stopped after loading HTTP page. Connection stayed open.

**Root cause:** After `socket.close()`, smoltcp needs `iface.poll()` to send the TCP FIN packet. But poll() was only called when slow-path packets arrived. During streaming, slow-path packets are rare, so FIN was never sent.

**Bad fix attempted:** Always call `iface.poll()` every ISR — killed performance (back to 3.5% drops).

**Correct fix:** `handle_http()` now returns `bool` indicating if it closed a socket. Only call `iface.poll()` when:
- Slow-path packet arrived (as before), OR
- HTTP just closed a socket (needs to send FIN)

```rust
let http_needs_poll = handle_http(iface);
if had_slow_path || http_needs_poll {
    iface.poll(time).ok();
}
```

**Result:** Chrome spinner stops, drop rate stays at 0.79%.

---

### Selective Handler Dispatch (v1.10.4)

**Problem:** After v1.10.3 fixes, still seeing 3.5% frame drops during streaming. Debug counters showed:
- `slow_path: 279` (arp:254 tcp:22 udp:2 other:1)
- `frames_dropped: 763` out of 20,883

**Root cause:** When ANY slow-path packet (e.g., ARP) arrived, ALL 6 socket handlers ran inside ISR:
```rust
if had_non_bitmap {
    handle_dhcp(iface);      // UDP
    handle_tftp(iface);      // UDP
    handle_telnet(iface);    // TCP
    handle_artnet(iface);    // UDP
    handle_bitmap_smoltcp(iface);  // UDP
    handle_http(iface);      // TCP
}
```

A single ARP packet (most common slow-path traffic) triggered 6 handler calls, most doing nothing.

**Fix implemented:**
1. Track packet type with `had_arp`, `had_tcp`, `had_udp` flags
2. TCP handlers (telnet, http) run every ISR — cheap when idle, needed for reliable HTTP
3. UDP handlers only run when `had_udp` is true — main optimization

**Results:**
| Metric | Before | After |
|--------|--------|-------|
| frames_dropped | 763 (3.5%) | 12 (0.85%) |
| mac_overflow | 6,598 (growing) | ~1,034 (stable) |
| HTTP reliability | intermittent | fast & reliable |

**Key insight:** ARP packets (254 of 279 slow-path) now trigger 0 UDP handler calls instead of 4, reducing ISR time by ~50% for the most common slow-path traffic.

---

### MAC Overflow Fix (v1.10.3)

**Problem:** Video streaming showed jitter and frame drops despite ISR-driven architecture. Debug counters revealed:
- `max_batch: 3094` — ISR processed 3094 packets in ONE call, blocking everything
- `slow_udp: 23` — NetBIOS broadcasts going through slow smoltcp path
- `mcast_dropped: 354` — VRRP multicast already being filtered

**Root causes identified via debug counters:**

1. **No ISR batch limit** — Loop processed ALL packets until hardware FIFO empty. When packets arrived continuously, ISR never exited, starving the display refresh.

2. **Multicast going slow path** — VRRP (protocol 112) packets correctly rejected by `is_bitmap_udp()` but still processed by smoltcp, taking ~500μs each.

3. **Unwanted UDP broadcasts** — NetBIOS (port 138), mDNS, etc. going through smoltcp instead of being dropped.

**Fixes implemented:**

1. **ISR batch limit** — `MAX_PACKETS_PER_ISR = 64` breaks out of loop to prevent starvation

2. **Multicast filter** — `is_multicast()` drops packets with dst MAC `01:xx:xx:xx:xx:xx` (but allows broadcast `ff:ff:ff:ff:ff:ff` for DHCP)

3. **Unwanted UDP filter** — `is_unwanted_udp()` drops UDP except ports 7000 (bitmap), 6454 (artnet), 67/68 (DHCP), 69 (TFTP)

4. **Debug counters** — Added `slow_arp`, `slow_tcp`, `slow_udp`, `slow_other` breakdown to identify traffic sources

**Results:**
| Metric | Before | After |
|--------|--------|-------|
| max_batch | 3094 | 6 |
| frames_dropped | 2562 | 9 |
| mac_overflow | 1718 | 71 |

**Key insight:** The captured slow_path packet (`slow_pkt` in stats) showed VRRP multicast from the gateway router — network noise, not bitmap traffic. Filtering this at the fast path level eliminated the smoltcp overhead. Worth knowing if you put a panel on a shared VLAN: the router's own housekeeping is enough to matter.

---

### Interrupt-Driven Network Stack Complete (v1.10.0 → v1.10.2)

**Major milestone:** Entire network stack now runs in ISR context. Main loop does zero network code.

**Architecture:**
```
Packet arrives → Interrupt fires → ISR saves all GPRs (128 bytes)
    → network_handler() called
    → Loop: peek_rx() each packet in hardware FIFO
        → Bitmap UDP? → process_raw_bitmap() + ack_rx() (fast path)
        → Other? → iface.poll() (smoltcp)
    → If had non-bitmap: handle all sockets (DHCP, HTTP, Telnet, Art-Net)
    → Re-enable interrupt
    → ISR restores GPRs, mret
```

**Key files:**
- `network.rs` — 1000+ lines, all network state as statics, `network_handler()` ISR entry point
- `ethernet.rs` — `peek_rx()`, `ack_rx()` for fast packet inspection
- `main.rs` — Now only does timer tick updates, animation, display refresh

**Bugs fixed:**
1. **RGB color order (v1.10.1)** — `rgb()` in `patterns.rs` was packing BGR but hardware expects GRB. Also fixed in `bitmap_udp.rs` pixel conversion.
2. **Missing smoltcp bitmap drain (v1.10.2)** — When non-bitmap packet triggers `iface.poll()`, smoltcp may consume subsequent bitmap packets. Added `handle_bitmap_smoltcp()` to drain these from the UDP socket buffer.

**Performance:**
- Fast path: ~50μs per bitmap packet (direct SDRAM writes)
- smoltcp path: ~500μs per packet (full IP/UDP/socket processing)
- Hardware FIFO: 8 slots, needs drain within 8ms at streaming rates

**HTTP Dashboard:**
- Dark theme, 15px fonts, 320px min card width
- Cards: Network, Display, Interrupt Status, Streaming, MAC Diagnostics, Panel Assignments, Controls
- Test pattern dropdown: grid, rainbow, rainbow_anim, white, red, green, blue

---

## 2026-02-08

### 19:00 - Trap Handler Debugging Session

**Problem:** Full trap handler with mscratch stack swap crashed at boot.

**Debugging progression:**
1. Minimal handler (8 bytes, t0/t1 only) → WORKS
2. Caller-saved registers (72 bytes, ra + t0-t6 + a0-a7) → WORKS
3. All GPRs (128 bytes, no CSRs, no mscratch) → WORKS
4. All GPRs + Rust call (poll_rx_to_ring) → WORKS at boot, CRASHES with interrupts enabled
5. Full TrapFrame with mscratch swap (144 bytes) → CRASHES at boot

**Root cause of crash with Rust call:** Race condition between ISR's `poll_rx_to_ring()` and main loop's `drain_raw_bitmap!` macro. Both access the same hardware registers (ev_pending, slot, length) simultaneously.

**Solution:** Use ISR as wake-up signal only (disable ev_enable), don't drain packets in ISR.

**Current working handler:** 128 bytes (all GPRs), inline ISR logic, no Rust call.

---

### 18:30 - ETHMAC Interrupt Investigation

Attempted to implement interrupt-driven packet reception to reduce MAC FIFO overflow.

**Key Findings:**

1. **VexRiscv "minimal" variant doesn't support external interrupts** — Changed to "lite" variant which adds ~25% CPU size but enables full interrupt support.

2. **mtvec is WRITE_ONLY in both variants** — Writes work but reads return 0. This is expected VexRiscv behavior.

3. **LiteX EventManager is level-triggered** — The `ev_status` register shows real-time packet availability. If you clear `ev_pending` while `ev_status=1`, the pending bit immediately re-sets and interrupt fires again.

4. **Calling Rust code from trap handler:** Works IF main loop doesn't also access hardware directly. Crashes due to race condition with `drain_raw_bitmap!`.

5. **Interrupt storm from rapid re-enable** — If you re-enable the interrupt while a packet is waiting (`ev_status=1`), it fires immediately causing rapid oscillation. Solution: only re-enable when `ev_status=0`.

**Current Implementation:**
- Full-register trap handler (128 bytes, all GPRs) with inline ISR logic
- ISR disables `ev_enable` as wake-up signal
- Main loop checks if ISR fired (`ev_enable=0`) and re-enables when `ev_status=0`
- Device is stable with interrupts enabled
- Still has overflow at max speed (ISR is wake-up only, doesn't drain)

**To truly reduce overflow:** Would need to change main loop to consume from ring buffer instead of directly from hardware, allowing ISR to drain packets without race condition.

**Relevant addresses:**
- ETHMAC base: `0xF0001800`
- `sram_writer_ev_pending`: `0xF0001810` (W1C - read then write back to clear)
- `sram_writer_ev_enable`: `0xF0001814` (write 0 to disable, 1 to enable)
- `sram_writer_ev_status`: `0xF000180C` (read-only, shows raw packet availability)

**VexRiscv interrupt CSRs:**
- `mtvec`: trap handler address (WRITE_ONLY)
- `0xBC0` (IRQ_MASK): which interrupts are enabled (ETHMAC = bit 2)
- `0xFC0` (IRQ_PENDING): which interrupts are pending
- `mie`: machine interrupt enable (bit 11 = MEIE for external)
- `mstatus`: global interrupt enable (bit 3 = MIE)

---

### 10:53 - Test Pattern Delay Requirement

`--delay 0.002` (2ms) works - same pacing as video streaming. This matches the auto-calculated delay in `send_youtube.py`:
```
chunk_delay = 0.9 / fps / total_chunks  # ≈1.8ms for 15fps, 34 chunks
```

With `--delay 0` all 34 packets arrive instantly → MAC FIFO overflow (8 slots).
With `--delay 0.1` too slow for streaming mode detection (200ms timeout).

Test command: `python3 tools/send_test_pattern.py --smoke --host <panel> --width 256 --height 64 --delay 0.002`

Note: May need to send twice occasionally - timing-sensitive.

---

### 09:30 - Post-CDC Revert Analysis

Reverted dual clock domain CDC implementation back to v1.9.0 (40MHz baseline). The CDC approach caused display banding.

**What was tried:**
- Added `cd_hub75` at 20MHz separate from `cd_sys`
- Changed FrameController, RowModule, RowColorOutput to `sync.hub75`
- Added MultiReg/PulseSynchronizer for control signals
- Set row buffer Memory ports to different clock domains

**Why it failed:**
1. Row buffer dual-clock memory may not work correctly with Migen's Memory primitive on ECP5
2. PulseSynchronizer timing may drop pulses at the clock ratio difference
3. FSM state machine coordination between domains too complex
4. The `shifting_buffer` signal selection of Array elements crosses domains unsafely

**Key discovery:** The CPU speed limit (~60MHz) is due to SDRAM PHY critical path, not CPU logic:
- Achieved timing: 60.22 MHz (16.6ns critical path)
- Bottleneck: SDRAM address decode → data valid path through `GENSDRPHY`

**Next approach:** Try HalfRate SDRAM mode (1:2) instead of CDC. The code already supports it - just need to enable `sdram_rate="1:2"` and target 80MHz sys clock.

---
