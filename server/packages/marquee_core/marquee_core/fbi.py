"""Flash Boot Image: how a card's firmware is stored in its SPI flash.

u32 length, u32 crc32 (both little-endian), then the image -- byte for byte
what LiteX's `crcfbigen.py -f -l` writes, and what the BIOS's flashboot()
checks before it jumps. A chain member cannot netboot, so this is the only
form its firmware ever runs from.
"""
from __future__ import annotations

import binascii


def fbi(image: bytes) -> bytes:
    return (len(image).to_bytes(4, "little")
            + (binascii.crc32(image) & 0xFFFFFFFF).to_bytes(4, "little")
            + image)
