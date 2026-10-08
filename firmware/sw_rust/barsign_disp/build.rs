use std::env;
use std::fs::File;
use std::io::Write;
use std::path::Path;

/// Put the linker script somewhere the linker can find it.
fn main() {
    let out_dir = env::var("OUT_DIR").expect("No out dir");
    let dest_path = Path::new(&out_dir);

    let mut f = File::create(dest_path.join("memory.x")).expect("Could not create file");
    f.write_all(include_bytes!("memory.x"))
        .expect("Could not write file");

    let mut f = File::create(dest_path.join("regions.ld")).expect("Could not create file");
    f.write_all(include_bytes!("regions.ld"))
        .expect("Could not write file");

    println!("cargo:rustc-link-search={}", dest_path.display());

    println!("cargo:rerun-if-changed=regions.ld");
    println!("cargo:rerun-if-changed=memory.x");
    println!("cargo:rerun-if-changed=build.rs");

    bake_default_config(dest_path);
    bake_sys_clk(dest_path);
}

/// Take the system clock from the GATEWARE rather than a constant in the source.
///
/// Every timer in the firmware derives from this, so a firmware built for one
/// clock and run on gateware built for another comes up and then fails at
/// everything time-dependent -- which presents as a network fault and sends you
/// looking in the wrong place. It is the only thing that makes one firmware binary
/// board-specific: the register maps of the 5A-75E and the E320 are byte-identical
/// (diff their csr.json), so with the clock supplied here the SAME source builds a
/// correct firmware for either, and `build.sh` can build every variant without
/// editing a source file.
///
/// `BARSIGN_SOC_H` names the LiteX-generated `soc.h` for the gateware this
/// firmware will run alongside; its `CONFIG_CLOCK_FREQUENCY` is authoritative.
/// Without it, falls back to `BARSIGN_SYS_CLK_HZ`, then to 40 MHz -- the 5A-75E's
/// clock, which is what this repo built for years before the E320 arrived.
fn bake_sys_clk(out: &Path) {
    println!("cargo:rerun-if-env-changed=BARSIGN_SOC_H");
    println!("cargo:rerun-if-env-changed=BARSIGN_SYS_CLK_HZ");

    let hz = env::var("BARSIGN_SOC_H")
        .ok()
        .filter(|p| !p.trim().is_empty())
        .and_then(|path| {
            println!("cargo:rerun-if-changed={}", path);
            let text = std::fs::read_to_string(&path)
                .unwrap_or_else(|e| panic!("BARSIGN_SOC_H {}: {}", path, e));
            text.lines()
                .find_map(|l| l.strip_prefix("#define CONFIG_CLOCK_FREQUENCY "))
                .and_then(|v| v.split_whitespace().next())
                .and_then(|v| v.parse::<u32>().ok())
        })
        .or_else(|| env::var("BARSIGN_SYS_CLK_HZ").ok().and_then(|v| v.trim().parse().ok()))
        .unwrap_or(40_000_000);

    assert!(
        (1_000_000..=200_000_000).contains(&hz),
        "implausible system clock {hz} Hz from the gateware; refusing to bake it in"
    );
    println!("cargo:warning=firmware system clock: {hz} Hz");
    let body = format!("pub const SYS_CLK_HZ: u32 = {hz};\n");
    let mut f = File::create(out.join("sys_clk.rs")).expect("Could not create file");
    f.write_all(body.as_bytes()).expect("Could not write file");
}

/// Bake a panel config into the firmware, so a board needs no network to know
/// what it is driving.
///
/// `BARSIGN_DEFAULT_CONFIG` names a file in the SAME format the panel fetches
/// over TFTP. That is deliberate: it is parsed at boot by the same
/// `LayoutConfig::parse()`, so there is one format and one parser, and a config
/// can be moved between the fleet server and the firmware unchanged.
fn bake_default_config(out: &Path) {
    println!("cargo:rerun-if-env-changed=BARSIGN_DEFAULT_CONFIG");

    let body = match env::var("BARSIGN_DEFAULT_CONFIG") {
        Ok(path) if !path.trim().is_empty() => {
            println!("cargo:rerun-if-changed={}", path);
            let text = std::fs::read_to_string(&path)
                .unwrap_or_else(|e| panic!("BARSIGN_DEFAULT_CONFIG {}: {}", path, e));
            // The parser takes &str; escape it into a Rust literal rather than
            // trying to guess a raw-string hash count that the content cannot
            // collide with.
            let escaped: String = text
                .chars()
                .flat_map(|c| match c {
                    '\\' => "\\\\".chars().collect::<Vec<_>>(),
                    '"' => "\\\"".chars().collect(),
                    '\n' => "\\n".chars().collect(),
                    '\r' => vec![],
                    c => vec![c],
                })
                .collect();
            format!("pub const DEFAULT_CONFIG: Option<&str> = Some(\"{}\");\n", escaped)
        }
        _ => "pub const DEFAULT_CONFIG: Option<&str> = None;\n".to_string(),
    };

    let mut f = File::create(out.join("default_config.rs")).expect("Could not create file");
    f.write_all(body.as_bytes()).expect("Could not write file");
}
