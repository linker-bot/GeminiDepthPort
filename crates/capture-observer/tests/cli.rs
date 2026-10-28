mod common;
use serde_json::json;
use std::{
    io::Write,
    process::{Command, Stdio},
};
#[test]
fn persistent_cli_recovers_after_invalid_and_oversized_input_without_private_echo() {
    let path = std::env::temp_dir().join(format!("observer-config-{}.json", std::process::id()));
    std::fs::write(&path, serde_json::to_vec(&common::config()).unwrap()).unwrap();
    let mut child = Command::new(env!("CARGO_BIN_EXE_capture-observer"))
        .args(["--config", path.to_str().unwrap()])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut input = child.stdin.take().unwrap();
    input.write_all(b"{\"version\":1,\"version\":1}\n").unwrap();
    input.write_all(&vec![b'x'; 65537]).unwrap();
    input.write_all(b"\n").unwrap();
    writeln!(
        input,
        "{}",
        json!({"version":1,"command":{"kind":"tick","at":common::at(200),"received_unix_ns":null}})
    )
    .unwrap();
    writeln!(input,"{}",json!({"version":1,"command":{"kind":"query","request":{"version":1,"query":{"kind":"snapshot"}}}})).unwrap();
    drop(input);
    let output = child.wait_with_output().unwrap();
    std::fs::remove_file(path).unwrap();
    assert!(output.status.success());
    let text = String::from_utf8(output.stdout).unwrap();
    let lines = text
        .lines()
        .map(|line| serde_json::from_str::<serde_json::Value>(line).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(lines.len(), 4);
    assert_eq!(lines[0]["error"]["code"], "INPUT_INVALID");
    assert_eq!(lines[1]["error"]["code"], "INPUT_LIMIT");
    assert_eq!(lines[2]["ok"], true);
    assert_eq!(lines[3]["kind"], "query");
    assert!(!text.contains("synthetic_color_optical"));
    assert!(!text.contains("synthetic_depth_optical"));
}
