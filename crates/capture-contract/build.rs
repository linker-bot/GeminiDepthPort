use std::process::Command;
fn main() {
    let root = std::path::PathBuf::from(std::env::var("CARGO_MANIFEST_DIR").unwrap()).join("../..");
    println!(
        "cargo:rerun-if-changed={}",
        root.join(".git/HEAD").display()
    );
    println!(
        "cargo:rerun-if-changed={}",
        root.join(".git/index").display()
    );
    for path in ["src", "crates", "Cargo.toml", "Cargo.lock"] {
        println!("cargo:rerun-if-changed={}", root.join(path).display());
    }
    let read = |args: &[&str]| {
        Command::new("git")
            .arg("-C")
            .arg(&root)
            .args(args)
            .output()
            .ok()
            .filter(|o| o.status.success())
            .and_then(|o| String::from_utf8(o.stdout).ok())
    };
    let top = read(&["rev-parse", "--show-toplevel"]);
    if top
        .as_deref()
        .and_then(|path| std::fs::canonicalize(path.trim()).ok())
        != std::fs::canonicalize(&root).ok()
    {
        return; // An extracted source tree must not inherit an unrelated parent repository ID.
    }
    let revision = read(&["rev-parse", "HEAD"])
        .unwrap_or_default()
        .trim()
        .to_string();
    if revision.len() == 40 && revision.bytes().all(|b| b.is_ascii_hexdigit()) {
        println!("cargo:rustc-env=GDP_SOURCE_REVISION={revision}");
        if let Some(status) = read(&["status", "--porcelain"]) {
            println!("cargo:rustc-env=GDP_SOURCE_DIRTY={}", !status.is_empty());
        }
    }
}
