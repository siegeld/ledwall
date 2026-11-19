"""Send a row ruler to a card: inside every 64-row group, rows go in 8-row
bands of distinct colours, band 0 at the group's first row. A full-width white
row marks local row 0, and the group number is drawn as that many short white
ticks on the left of band 3, so a photo reads off the mapping exactly.

A third argument `plain` draws the bands alone, no white row and no ticks:
any line of another colour inside a band is then the panel's, not ours."""
import sys, time
from marquee_core.stream import CardStream

host = sys.argv[1]
PLAIN = len(sys.argv) > 3 and sys.argv[3] == "plain"
W, H, G = 128, 256, 64
BANDS = [(255, 0, 0), (255, 128, 0), (255, 255, 0), (0, 255, 0),
         (0, 255, 255), (0, 0, 255), (160, 0, 255), (255, 0, 160)]
buf = bytearray(W * H * 3)
for y in range(H):
    g, local = divmod(y, G)
    for x in range(W):
        c = BANDS[local // 8]
        if PLAIN:
            pass
        elif local == 0:
            c = (255, 255, 255)
        elif local // 8 == 3 and 4 <= local % 8 <= 5 and x < 12 * (g + 1) and x % 12 < 8:
            c = (255, 255, 255)
        i = (y * W + x) * 3
        buf[i:i + 3] = bytes(c)
with CardStream(host, 7000, packet_delay=0.0005) as s:
    for _ in range(int(sys.argv[2]) if len(sys.argv) > 2 else 3):
        s.send_frame(bytes(buf), W, H)
        time.sleep(0.05)
print("sent")
