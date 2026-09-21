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
