#!/usr/bin/env python3
"""Card-chain lab tests that need ONE card and no chain cabling.

Run on the sender host (it must be on the card's segment). Standard library
only. Each step prints PASS/FAIL with the evidence; the script never reflashes
or power-cycles anything -- that is done (and recorded) by whoever runs it.

    chain_lab_test.py head   <card-ip>               # a card on the switch
    chain_lab_test.py member <card-ip> <hop>         # fake a hop: slave mode
    chain_lab_test.py rehop  <card-ip> <hop>         # member -> another hop
    chain_lab_test.py unmember <card-ip>             # member -> head: reboots
    chain_lab_test.py clock  <card-ip> [port]        # shared program clock
    chain_lab_test.py pixels <card-ip> <udp-port> [http-port]
    chain_lab_test.py fbdump <card-ip> <out.json> [http-port]
    chain_lab_test.py groupdrop <card-ip> [http-port]  # broadcasts not forwarded
    chain_lab_test.py flashfw <card-ip> <http-port> <file> <expect: done|failed>

`member` makes the card believe it is hop h behind ITSELF: the discovery is
addressed to its own MAC/IP, exactly as a real head would forward it. A real
chain's head answers ARP for the member; here nothing does, so pin the card's
MAC in the sender's neighbour table first (`ip neigh replace <ip> lladdr
<mac> dev <if> nud permanent`) and remove it afterwards.
"""
import json
import socket
import struct
import sys
import time
import urllib.error
import urllib.request

DISC_PORT = 6990
FAILS = []


def say(ok, what, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {what}" + (f"  -- {detail}" if detail else ""))
    if not ok:
        FAILS.append(what)


def http(ip, port, path, method="GET", body=None, timeout=3):
    url = f"http://{ip}:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"} if data else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, dict(r.headers), r.read()


def status(ip, port=80, timeout=3):
    try:
        _, _, b = http(ip, port, "/api/status", timeout=timeout)
        return json.loads(b)
    except Exception:
        return None


def disc(ip, hop, version=2, clock_ms=None):
    if clock_ms is None:
        clock_ms = time.monotonic_ns() // 1_000_000
    p = (b"CH" + bytes([hop, version, 255 - hop, 0])
         + (clock_ms & 0xFFFFFFFF).to_bytes(4, "big") + bytes(8))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.sendto(p, (ip, DISC_PORT))
    s.close()


def tcp_refused(ip, port, timeout=2.0):
    """'refused' (a RST came back), 'open', or 'silent' (nothing answered)."""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return "open"
    except ConnectionRefusedError:
        return "refused"
    except (socket.timeout, OSError):
        return "silent"
    finally:
        s.close()


def frames(ip, port):
    st = status(ip, port)
    return None if st is None else st.get("bitmap_frames")


def send_bm(ip, udp_port, w, h, n_frames=10, fps=10):
    """RGB888 'BM' frames, 487 px a chunk -- the narrow wire format."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ppc = 487
    total = (w * h + ppc - 1) // ppc
    for fid in range(n_frames):
        px = bytes([(fid * 40) & 0xff, 0x40, 0x80]) * (w * h)
        for ci in range(total):
            body = px[ci * ppc * 3:(ci + 1) * ppc * 3]
            hdr = b"BM" + struct.pack("<HBBHH", fid, ci, total, w, h)
            s.sendto(hdr + body, (ip, udp_port))
            time.sleep(0.0004)
        time.sleep(1 / fps)
    s.close()


def t_head(ip):
    print(f"head / lone-card behaviour on {ip}")
    st = status(ip)
    say(st is not None, "API answers on :80")
    if st is None:
        return
    say("chain" in st, "status has a chain block", st.get("version"))
    _, hdr, _ = http(ip, 80, "/api/status")
    say(hdr.get("Access-Control-Allow-Origin") == "*", "JSON API sends Access-Control-Allow-Origin: *")
    before = st["chain"].get("discoveries", 0)
    for _ in range(3):
        disc(ip, 0)
        time.sleep(0.3)
    st = status(ip)
    ch = st["chain"]
    say(ch["role"] == "head", "hop-0 discovery makes it a head", ch["role"])
    say(ch["discoveries"] >= before + 3, "discoveries counted", f"{before} -> {ch['discoveries']}")
    say(ch["hop"] == 0 and ch["http_port"] == 80 and ch["pixel_port"] == 7000, "head keeps ports 80/7000")
    say(ch["port1_link"] is False, "downstream port reports no link (nothing cabled)",
        f"port1_link={ch['port1_link']}")
    # A head must ignore members' ports SILENTLY: a RST from the head would
    # kill the member's connection from the far end.
    r = tcp_refused(ip, 8001)
    say(r == "silent", "head stays silent on a member's TCP port 8001 (no RST)", r)
    r = tcp_refused(ip, 23)
    say(r in ("open", "refused"), "head still answers its own ports (telnet 23)", r)


def t_member(ip, hop):
    print(f"member (slave) mode on {ip} at hop {hop}")
    for _ in range(3):
        disc(ip, hop)
        time.sleep(0.4)
    time.sleep(1.0)
    st = status(ip, 8000 + hop, timeout=4)
    say(st is not None, f"member answers on :{8000 + hop}")
    if st is None:
        return
    ch = st["chain"]
    say(ch["role"] == "member" and ch["hop"] == hop, "role member at the right hop", f"{ch['role']} {ch['hop']}")
    say(ch["pixel_port"] == 7000 + hop and ch["http_port"] == 8000 + hop, "ports follow the hop",
        f"{ch['http_port']}/{ch['pixel_port']}")
    r = tcp_refused(ip, 80)
    say(r == "silent", "member is SILENT on the head's port 80 (no RST)", r)
    r = tcp_refused(ip, 8000 + hop + 1)
    say(r == "silent", "member is silent on the next hop's port", r)
    say(st.get("ip") in ("0.0.0.0", ip), "member reports the head's address (or none)", st.get("ip"))


def t_pixels(ip, udp_port, http_port):
    print(f"pixels to {ip}:{udp_port}, counted via :{http_port}")
    st = status(ip, http_port)
    w, h = st["display_width"], st["display_height"]
    a = frames(ip, http_port)
    send_bm(ip, udp_port, w, h, n_frames=10)
    time.sleep(0.5)
    b = frames(ip, http_port)
    say(b is not None and a is not None and b - a >= 8, f"frames on :{udp_port} land", f"{a} -> {b}")
    return a, b


def t_clock(ip, port=80):
    print(f"shared program clock on {ip}:{port}")
    base = 1_000_000_000
    t0 = time.monotonic()
    disc(ip, 0 if port == 80 else port - 8000, clock_ms=base)
    time.sleep(0.5)
    st = status(ip, port)
    t1 = time.monotonic()
    got = st["chain"]["clock_ms"]
    want_lo = base + int((0.5) * 1000) - 50
    want_hi = base + int((t1 - t0) * 1000) + 50
    say(want_lo <= got <= want_hi, "clock follows the discovery's", f"{got} in [{want_lo},{want_hi}]")
    # Re-anchor across the u32 wrap.
    disc(ip, 0 if port == 80 else port - 8000, clock_ms=0xFFFF_FF00)
    time.sleep(0.6)
    got = status(ip, port)["chain"]["clock_ms"]
    say(got < 0x2000 or got > 0xFFFF_FF00, "clock wraps cleanly past 2^32", hex(got))


def t_fbdump(ip, out, port=80):
    rows = []
    st = status(ip, port)
    h = st["display_height"]
    y0 = 0
    while y0 < h:
        # Up to 16 rows a reply, fewer on a wide card: step by what came back.
        _, _, b = http(ip, port, "/api/fbdump", "POST", {"y0": str(y0)})
        rows.append(json.loads(b))
        if rows[-1]["rows"] < 1:
            raise SystemExit(f"/api/fbdump returned no rows at y0={y0}")
        y0 += rows[-1]["rows"]
    json.dump({"status": st, "rows": rows}, open(out, "w"))
    print(f"  wrote {out}: {h} rows in {len(rows)} blocks, mode {rows[0].get('mode')}")


def t_groupdrop(ip, port=80):
    """The forwarder drops group-addressed frames: fwd_dropped must climb with
    the segment's own broadcast/multicast, and a broadcast we send ourselves."""
    print(f"group frames not forwarded, {ip}:{port}")
    a = status(ip, port)["chain"]
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    for _ in range(50):
        s.sendto(b"chain-lab broadcast", ("255.255.255.255", 9))
    s.close()
    time.sleep(1.0)
    b = status(ip, port)["chain"]
    dd = b.get("fwd_dropped", 0) - a.get("fwd_dropped", 0)
    df = b["fwd_frames"] - a["fwd_frames"]
    say("fwd_dropped" in b, "status reports fwd_dropped")
    say(dd >= 50, "our 50 broadcasts (and the segment's) were not forwarded", f"+{dd} dropped of +{df} seen")


def t_flashfw(ip, port, fname, expect):
    print(f"firmware to flash: {fname} on {ip}:{port}, expecting {expect}")
    _, _, b = http(ip, port, "/api/flash/firmware", "POST", {"file": fname})
    r = json.loads(b)
    say(r.get("ok") is True, "write queued", r.get("error", ""))
    st = None
    for _ in range(90):
        time.sleep(2)
        # The final read-back crc32 over ~570 KB runs in the card's network
        # ISR and holds HTTP off for a few seconds; a poll that lands then
        # times out, which is not a failure.
        try:
            _, _, b = http(ip, port, "/api/flash/firmware", timeout=10)
        except OSError:
            continue
        st = json.loads(b)
        if st["state"] == "done" or st["state"].startswith("failed"):
            break
    got = "done" if st["state"] == "done" else "failed"
    say(got == expect, f"write ends {expect}", f"{st['state']} {st.get('error', '')} written={st.get('written')}")


def main():
    cmd, ip = sys.argv[1], sys.argv[2]
    if cmd == "head":
        t_head(ip)
    elif cmd == "member":
        t_member(ip, int(sys.argv[3]))
    elif cmd == "rehop":
        t_member(ip, int(sys.argv[3]))
    elif cmd == "unmember":
        print(f"member -> head on {ip}: expect a reboot")
        disc(ip, 0)
        time.sleep(2)
        t0 = time.time()
        st = None
        while time.time() - t0 < 40 and st is None:
            st = status(ip, 80, timeout=2)
            time.sleep(1)
        say(st is not None, "comes back on :80 as an ordinary card", f"{time.time() - t0:.1f}s")
        if st:
            say(st["chain"]["role"] in ("none", "head"), "no longer a member", st["chain"]["role"])
    elif cmd == "clock":
        t_clock(ip, int(sys.argv[3]) if len(sys.argv) > 3 else 80)
    elif cmd == "pixels":
        t_pixels(ip, int(sys.argv[3]), int(sys.argv[4]) if len(sys.argv) > 4 else 80)
    elif cmd == "groupdrop":
        t_groupdrop(ip, int(sys.argv[3]) if len(sys.argv) > 3 else 80)
    elif cmd == "flashfw":
        t_flashfw(ip, int(sys.argv[3]), sys.argv[4], sys.argv[5])
    elif cmd == "fbdump":
        t_fbdump(ip, sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 80)
    print("RESULT:", "PASS" if not FAILS else f"FAIL ({len(FAILS)}): " + "; ".join(FAILS))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
