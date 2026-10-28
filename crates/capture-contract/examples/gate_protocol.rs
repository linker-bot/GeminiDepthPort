//! Offline recording-gate protocol verifier. No SDK, ROS, Docker or management mutation.
use capture_contract::{
    parse_strict,
    recording::{GateAck, GateRequest},
};
use serde_json::json;
use std::{fs, io::Read, path::Path};
fn read(path: &str) -> Result<Vec<u8>, Box<dyn std::error::Error>> {
    if !Path::new(path).is_absolute() || !fs::metadata(path)?.is_file() {
        return Err("PRIVATE_INPUT_FILE".into());
    }
    let mut raw = Vec::new();
    fs::File::open(path)?.take(65537).read_to_end(&mut raw)?;
    if raw.len() > 65536 {
        return Err("INPUT_LIMIT".into());
    }
    Ok(raw)
}
fn run() -> Result<serde_json::Value, Box<dyn std::error::Error>> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    match args.first().map(String::as_str) {
        Some("request-digest") if args.len() == 2 => {
            let request = GateRequest::parse(&read(&args[1])?)?;
            Ok(json!({"version":1,"ok":true,"request_sha256":request.digest()?}))
        }
        Some("verify-ack") if args.len() == 4 => {
            let request = GateRequest::parse(&read(&args[1])?)?;
            let ack: GateAck = serde_json::from_value(parse_strict(&read(&args[2])?)?)?;
            let worker_pid = args[3].parse::<u32>()?;
            if worker_pid == 0 {
                return Err("WORKER_PID_REQUIRED".into());
            }
            request.verify_ack(&ack, worker_pid)?;
            Ok(json!({"version":1,"ok":true}))
        }
        _ => Err("GATE_PROTOCOL_ARGUMENTS".into()),
    }
}
fn main() {
    match run() {
        Ok(value) => println!("{value}"),
        Err(_) => {
            println!(
                "{}",
                json!({"version":1,"ok":false,"code":"GATE_PROTOCOL_REJECTED"})
            );
            std::process::exit(1);
        }
    }
}
