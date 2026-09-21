#![no_std]

pub mod artnet;
pub mod bitmap_udp;
pub mod breadcrumb;

/// Interrupt counter, incremented by the assembly trap handler in main.rs.
///
/// Must be a linker-allocated object. The handler previously incremented a
/// hardcoded 0x40020000, which lands inside .text (which spans
/// 0x40000000-0x40026938), so every interrupt overwrote an instruction in the
/// firmware's own code.
#[no_mangle]
pub static mut ISR_COUNTER: u32 = 0;
pub mod default_config;
pub mod ethernet;
pub mod flash_id;
pub mod generated;
pub mod hal;
pub mod http;
pub mod hub75;
pub mod img;
pub mod img_flash;
pub mod layout;
pub mod menu;
pub mod network;
pub mod panic;
pub mod patterns;
pub mod pearson_hash;
pub mod tftp_config;
// Only in a bitstream whose output stage is S-PWM: the chip CSRs do not exist
// otherwise, and the PAC is regenerated per bitstream.
#[cfg(feature = "spwm")]
pub mod spwm;
#[cfg(feature = "spwm")]
pub mod spwm_tables;
pub mod assets;
pub mod prim;
pub mod program;
pub mod sprite;
pub mod values;
