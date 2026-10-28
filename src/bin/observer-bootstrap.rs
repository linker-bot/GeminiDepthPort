//! Private actual-observation bootstrap; no SDK opens, host writes or ROS imports.
use std::{fs, path::Path};
fn main() {
    let result = (|| -> anyhow::Result<()> {
        let a = std::env::args().skip(1).collect::<Vec<_>>();
        anyhow::ensure!(a.len() == 5, "OBSERVER_BOOTSTRAP_ARGUMENT");
        anyhow::ensure!(
            Path::new(&a[0]).is_absolute() && Path::new(&a[1]).is_absolute(),
            "OBSERVER_BOOTSTRAP_PATH"
        );
        let p = capture_contract::parse_strict(&fs::read(&a[0])?)?;
        let o = capture_contract::parse_strict(&fs::read(&a[1])?)?;
        let result =
            geminidepthport::official_observer::bootstrap(&p, &o, &a[2], &a[3], a[4].parse()?)?;
        println!("{}", serde_json::to_string(&result)?);
        Ok(())
    })();
    if result.is_err() {
        eprintln!("OBSERVER_BOOTSTRAP_FAILED");
        std::process::exit(1);
    }
}
