"""TFTP boot service for the panel fleet.

Panels have no firmware they can rely on locally: the LiteX BIOS fetches
boot.bin over TFTP at every boot. Serving it from Marquee -- rather than a
script on someone's workstation -- makes firmware rollout a fleet operation
with a record of which panel booted what.

Port 6969, not 69: the gateware sets TFTP_SERVER_PORT=6969.

This wraps tftpy rather than speaking the protocol directly. A hand-rolled
server here failed real transfers ("no ack for block 3"): it accepted only an
ACK for the exact block just sent, so a duplicate or delayed ACK -- routine in
TFTP -- caused a retry storm and a truncated image, and the panel never booted.
Retransmits, duplicate ACKs, timeouts and option negotiation are exactly the
parts worth taking from a library.

Per-panel files are resolved from the database through `resolve_file`:
    boot.bin   -> that panel's assigned firmware, else the default
    <mac>.yml  -> that panel's layout, generated from its record
Anything else is refused; this is not a general file server.
"""

from __future__ import annotations

import logging
import os
import tempfile

log = logging.getLogger("marquee.tftp")


class TftpBootServer:
    def __init__(self, resolve_file, host: str = "0.0.0.0", port: int = 6969,
                 root: str = "/data/firmware"):
        """resolve_file(filename, client_ip) -> bytes | None"""
        self.resolve_file = resolve_file
        self.host, self.port, self.root = host, port, root
        self.transfers = 0

    def _dyn(self, path: str, raddress: str | None = None, rport: int | None = None):
        """tftpy dynamic-file hook: return a file-like object, or None for 404."""
        import os

        name = os.path.basename(path)
        try:
            data = self.resolve_file(name, raddress or "")
        except Exception:
            log.exception("tftp resolve %s", name)
            return None
        if data is None:
            log.warning("tftp %s -> %s: not found", raddress, name)
            return None
        # tftpy flock()s whatever it is handed, so this must be a REAL file --
        # a BytesIO raises io.UnsupportedOperation: fileno and kills the server
        # thread. Spool to a temp file that unlinks itself on close.
        tmp = tempfile.NamedTemporaryFile(prefix="marquee-tftp-", delete=True)
        tmp.write(data)
        tmp.flush()
        tmp.seek(0)
        self.transfers += 1
        log.info("tftp %s -> %s: %d bytes", raddress, name, len(data))
        return tmp

    def serve_forever(self):
        import tftpy

        # tftpy is given an EMPTY root on purpose. Its RRQ handler is:
        #
        #     if os.path.exists(path):        <- static file wins
        #         self.context.fileobj = open(path, "rb")
        #     elif self.context.dyn_file_func:
        #         ...
        #
        # so a file present in the root is served straight off disk and the
        # resolver is NEVER CALLED. With root=/data/firmware that is exactly what
        # happened to boot.bin: every panel got the default image, `firmware` on
        # a panel record did nothing, and staged rollout was impossible -- while
        # <mac>.yml worked, because no such file exists on disk, which is what
        # made the fault look like a lookup bug rather than a routing one.
        #
        # An empty root sends every request through resolve_file, which reads
        # from self.root itself by absolute path. Do not "tidy" this back to
        # passing self.root.
        empty = tempfile.mkdtemp(prefix="marquee-tftp-root-")
        srv = tftpy.TftpServer(empty, dyn_file_func=self._dyn)
        log.info("tftp listening on %s:%d (serving %s via resolver)",
                 self.host, self.port, self.root)
        srv.listen(self.host, self.port)
