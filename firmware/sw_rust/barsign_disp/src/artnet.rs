//! Art-Net (ArtDmx) packet decoding.
//!
//! Art-Net is the right transport for interop with lighting consoles and
//! pixel-mapping tools (Resolume, MadMapper, QLC+), and the wrong one for
//! full-motion video on this panel: a universe carries 512 channels = 170 RGB
//! pixels, so 256x128 needs 193 universes per frame against 68 packets for the
//! native bitmap protocol. Use it to be driven BY a lighting desk, not to push
//! video.

use core::convert::TryInto;

/// Art-Net header is 18 bytes: magic(8) opcode(2) protver(2) seq(1) phys(1)
/// universe(2) length(2).
const HEADER_LEN: usize = 18;
const OP_DMX: u16 = 0x5000;
const MIN_PROT_VER: u16 = 14;
/// 512 DMX channels / 3 bytes per pixel.
pub const PIXELS_PER_UNIVERSE: usize = 170;

/// Decode an ArtDmx packet into (starting pixel index, HUB75 words).
///
/// Returns Err for anything that is not a valid ArtDmx packet. The validation
/// used to be commented out, which meant ANY UDP datagram of 18 bytes or more
/// reaching this port was decoded as pixel data and written to the display.
pub fn packet2hub75(data: &[u8]) -> Result<(usize, impl Iterator<Item = u32> + '_), ()> {
    if data.len() < HEADER_LEN {
        return Err(());
    }
    if !data.starts_with(b"Art-Net\0") {
        return Err(());
    }
    // OpCode is little-endian; ProtVer is big-endian. Yes, in the same header.
    if u16::from_le_bytes(data[8..10].try_into().unwrap()) != OP_DMX {
        return Err(());
    }
    if u16::from_be_bytes(data[10..12].try_into().unwrap()) < MIN_PROT_VER {
        return Err(());
    }

    let universe = u16::from_le_bytes(data[14..16].try_into().unwrap());
    let length = u16::from_be_bytes(data[16..18].try_into().unwrap()) as usize;
    if length > 512 || data.len() < HEADER_LEN + length {
        return Err(());
    }

    let iter = data[HEADER_LEN..HEADER_LEN + length]
        .chunks_exact(3)
        .map(|c| {
            // DMX carries R,G,B in channel order. The framebuffer word is
            // 0x00BBGGRR on this hardware, measured 2026-09-18 -- so this is
            // B<<16 | G<<8 | R.
            //
            // This file originally packed exactly that and was "corrected" onto
            // 0x00GGRRBB by the v1.10.1 colour-order change, along with every
            // other writer. TODO item 7 then listed it as the odd one out. It
            // had been right all along.
            ((c[2] as u32) << 16) | ((c[1] as u32) << 8) | (c[0] as u32)
        });

    Ok((universe as usize * PIXELS_PER_UNIVERSE, iter))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dmx(universe: u16, rgb: &[u8]) -> alloc::vec::Vec<u8> {
        let mut p = alloc::vec::Vec::new();
        p.extend_from_slice(b"Art-Net\0");
        p.extend_from_slice(&OP_DMX.to_le_bytes());
        p.extend_from_slice(&14u16.to_be_bytes());
        p.push(0);
        p.push(0);
        p.extend_from_slice(&universe.to_le_bytes());
        p.extend_from_slice(&(rgb.len() as u16).to_be_bytes());
        p.extend_from_slice(rgb);
        p
    }

    #[test]
    fn rejects_non_artnet() {
        assert!(packet2hub75(&[0u8; 32]).is_err());
    }

    #[test]
    fn packs_gg_rr_bb() {
        let p = dmx(0, &[0x11, 0x22, 0x33]);
        let (off, mut it) = packet2hub75(&p).unwrap();
        assert_eq!(off, 0);
        // R=0x11 G=0x22 B=0x33 -> 0x00_22_11_33
        assert_eq!(it.next().unwrap(), 0x0022_1133);
    }

    #[test]
    fn universe_offsets_by_170() {
        let p = dmx(3, &[1, 2, 3]);
        assert_eq!(packet2hub75(&p).unwrap().0, 510);
    }
}
