#!/usr/bin/env python3
"""Minimal TFTP server on a custom port (default 6969).

Uses tftpy to serve files from a root directory. Designed to run as a
background daemon started by build.sh.

Usage:
    python3 tools/tftpd.py --root .tftp --host 192.168.1.10 --port 6969
"""

import argparse
import logging
import os
import signal
import socket
import sys

import tftpy


def main():
    parser = argparse.ArgumentParser(description="TFTP server on custom port")
    parser.add_argument("--root", required=True, help="TFTP root directory")
    parser.add_argument("--host", default="0.0.0.0", help="Listen address")
    parser.add_argument("--port", type=int, default=6969, help="Listen port")
    parser.add_argument("--log", help="Log file path")
    parser.add_argument("--pid", help="PID file path")
    args = parser.parse_args()

    # Set up logging
    handlers = [logging.StreamHandler()]
    if args.log:
        handlers.append(logging.FileHandler(args.log))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        datefmt="%b %d %H:%M:%S",
        handlers=handlers,
    )
    # tftpy uses the 'tftpy' logger
    logging.getLogger("tftpy").setLevel(logging.INFO)

    # Probe the port before claiming success. tftpy's listen() blocks, so there
    # is no post-bind hook to log from -- without this, a bind failure still
    # printed "TFTP server listening" and then died silently, which reads as a
    # mystery instead of as EADDRINUSE. A stale daemon holding this port once
    # cost real debugging time, and worse, it went on serving an old boot.bin.
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((args.host, args.port))
    except OSError as exc:
        logging.error("cannot bind %s:%d - %s", args.host, args.port, exc)
        logging.error("something else is already serving this port; "
                      "check for an older tftpd.py (ss -ulpn | grep %d)", args.port)
        sys.exit(1)
    finally:
        probe.close()

    # Write PID file only once we know we can actually serve.
    if args.pid:
        with open(args.pid, "w") as f:
            f.write(str(os.getpid()))

    def remove_pidfile():
        if args.pid and os.path.exists(args.pid):
            os.remove(args.pid)

    def cleanup(signum, frame):
        remove_pidfile()
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    server = tftpy.TftpServer(args.root)
    # Log the absolute root: which TREE is being served is exactly the detail
    # that hid a daemon serving a stale boot.bin out of a deprecated checkout.
    logging.info("TFTP server listening on %s:%d, root=%s (pid %d)",
                 args.host, args.port, os.path.abspath(args.root), os.getpid())
    status = 0
    try:
        server.listen(args.host, args.port)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Previously the finally: cleanup() below exited 0 on ANY failure, so a
        # dead server looked like a clean shutdown to anything checking status.
        logging.error("TFTP server stopped: %s", exc)
        status = 1
    finally:
        remove_pidfile()
    sys.exit(status)


if __name__ == "__main__":
    main()
