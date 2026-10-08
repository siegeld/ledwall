"""Saturate a card's Ethernet RX and see whether the MAC reports capture errors.

    CARD=192.168.1.62 tools/eth_rx_stress.py [seconds]

Measured on the E320, 2026-10-07: two runs, 9,849,178 frames at 914-936 Mbit/s,
mac_crc_errors 0 and mac_preamble_errors 0. See docs/HARDWARE.md section 6.


The question: nextpnr says the E320's RGMII RX path tops out at ~122 MHz against a
125 MHz constraint. If that shortfall is real, a saturated receive path should
produce CRC or preamble errors -- those are counted in the GATEWARE, at the MAC,
before any software sees a byte, so they isolate capture from everything above it.

Sent to a UDP port the firmware's software filter discards (is_unwanted_udp keeps
only 7000/6454/67/68/69/6900/6901), so the frames are received, counted and
CRC-checked but never reach the display path. The achieved rate also reveals the
negotiated link speed: sustained >100 Mbit/s means the link is gigabit, and the
125 MHz constraint is the one that actually applies.
"""
import json, socket, sys, time, urllib.request

import os
CARD = os.environ.get("CARD", "192.168.1.62")
PORT = 9999          # deliberately not in the firmware's allowlist
SIZE = 1400
SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0

def stat():
    with urllib.request.urlopen(f"http://{CARD}/api/status", timeout=5) as r:
        return json.loads(r.read().decode())

a = stat()
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 22)
buf = bytes(SIZE)
sent = 0
t0 = time.time()
while time.time() - t0 < SECS:
    for _ in range(2000):
        try:
            s.sendto(buf, (CARD, PORT))
            sent += 1
        except OSError:
            pass
el = time.time() - t0
b = stat()

d = lambda k: b.get(k, 0) - a.get(k, 0)
rx = d("n_mac")
print(f"  sent by host     : {sent:,} datagrams in {el:.1f}s "
      f"({sent*SIZE*8/el/1e6:.0f} Mbit/s offered)")
print(f"  n_mac delta      : {rx:,} frames  "
      f"({rx*SIZE*8/el/1e6:.0f} Mbit/s received at the MAC)")
print(f"  mac_crc_errors   : {d('mac_crc_errors'):,}   <-- capture errors")
print(f"  mac_preamble_err : {d('mac_preamble_errors'):,}   <-- capture errors")
print(f"  mac_overflow     : {d('mac_overflow'):,}   (CPU not draining; not a capture fault)")
print()
if rx * SIZE * 8 / el / 1e6 > 150:
    print("  LINK: gigabit -- so the 125 MHz RGMII RX constraint is the one that applies.")
else:
    print("  LINK: at or below 100 Mbit -- RGMII RX runs at 25 MHz, and the 125 MHz")
    print("        constraint nextpnr failed does not apply to this deployment.")
print("  VERDICT:", "RX capture CLEAN under saturation"
      if d("mac_crc_errors") == 0 and d("mac_preamble_errors") == 0
      else "RX CAPTURE ERRORS -- the timing shortfall is real")
