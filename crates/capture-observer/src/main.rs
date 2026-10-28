//! Versioned JSONL decoded-message boundary; no ROS/SDK imports or device access.
use capture_contract::{MonoTime, parse_strict};
use capture_observer::*;
use quality_core::QualityQueryRequest;
use serde::Deserialize;
use serde_json::json;
use std::{
    fs,
    io::{self, BufRead, Read, Write},
};

const MAX_LINE: usize = 65536;
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    version: u32,
    command: Command,
}
#[derive(Deserialize)]
#[serde(tag = "kind", rename_all = "kebab-case", deny_unknown_fields)]
enum Command {
    Image {
        stream: StreamRole,
        at: MonoTime,
        received_unix_ns: Option<u64>,
        image: DecodedImage,
    },
    CameraInfo {
        stream: StreamRole,
        at: MonoTime,
        info: Box<DecodedCameraInfo>,
    },
    Metadata {
        stream: StreamRole,
        at: MonoTime,
        metadata: DecodedMetadata,
    },
    Tick {
        at: MonoTime,
        received_unix_ns: Option<u64>,
    },
    Watermark {
        at: MonoTime,
        unix_ns: u64,
    },
    Query {
        request: QualityQueryRequest,
    },
    Finalize,
}
fn reply(observer: &mut CaptureObserver, line: &[u8]) -> Result<serde_json::Value, &'static str> {
    let value = parse_strict(line).map_err(|_| "INPUT_INVALID")?;
    let request: Request = serde_json::from_value(value).map_err(|_| "INPUT_INVALID")?;
    if request.version != 1 {
        return Err("OBSERVER_REQUEST_VERSION");
    }
    let update = match request.command {
        Command::Image {
            stream,
            at,
            received_unix_ns,
            image,
        } => observer.image(stream, at, received_unix_ns, image)?,
        Command::CameraInfo { stream, at, info } => observer.camera_info(stream, at, *info)?,
        Command::Metadata {
            stream,
            at,
            metadata,
        } => observer.metadata(stream, at, metadata)?,
        Command::Tick {
            at,
            received_unix_ns,
        } => observer.tick(at, received_unix_ns)?,
        Command::Watermark { at, unix_ns } => observer.advance_watermark(at, unix_ns)?,
        Command::Query { request } => {
            return Ok(
                json!({"version":1,"ok":true,"kind":"query","result":observer.query(request)?}),
            );
        }
        Command::Finalize => {
            return Ok(
                json!({"version":1,"ok":true,"kind":"finalized","update":observer.finalize()?}),
            );
        }
    };
    Ok(json!({"version":1,"ok":true,"kind":"update","update":update}))
}
fn output(value: &serde_json::Value) -> io::Result<()> {
    let stdout = io::stdout();
    let mut lock = stdout.lock();
    serde_json::to_writer(&mut lock, value)?;
    lock.write_all(b"\n")?;
    lock.flush()
}
fn run() -> Result<(), &'static str> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    if args == ["--help"] {
        println!(
            "capture-observer --config PRIVATE_JSON; version-1 decoded-message JSONL stdin/stdout; no SDK access"
        );
        return Ok(());
    }
    if args.len() != 2 || args[0] != "--config" {
        return Err("OBSERVER_ARGUMENT");
    }
    let mut config = Vec::new();
    fs::File::open(&args[1])
        .map_err(|_| "CONFIG_UNAVAILABLE")?
        .take((MAX_LINE + 1) as u64)
        .read_to_end(&mut config)
        .map_err(|_| "CONFIG_UNAVAILABLE")?;
    if config.len() > MAX_LINE {
        return Err("CONFIG_LIMIT");
    }
    let value = parse_strict(&config).map_err(|_| "CONFIG_INVALID")?;
    let config: ObserverConfig = serde_json::from_value(value).map_err(|_| "CONFIG_INVALID")?;
    let mut observer = CaptureObserver::new(config)?;
    let stdin = io::stdin();
    let mut reader = stdin.lock();
    loop {
        let mut line = Vec::new();
        let mut oversized = false;
        loop {
            let buffer = reader.fill_buf().map_err(|_| "INPUT_IO")?;
            if buffer.is_empty() {
                break;
            }
            let consumed = buffer
                .iter()
                .position(|v| *v == b'\n')
                .map_or(buffer.len(), |i| i + 1);
            if !oversized {
                if line.len() + consumed > MAX_LINE {
                    oversized = true;
                    line.clear();
                } else {
                    line.extend_from_slice(&buffer[..consumed]);
                }
            }
            let ended = buffer[consumed - 1] == b'\n';
            reader.consume(consumed);
            if ended {
                break;
            }
        }
        if line.is_empty() && !oversized {
            break;
        }
        let result = if oversized {
            Err("INPUT_LIMIT")
        } else {
            reply(&mut observer, &line)
        };
        let response =
            result.unwrap_or_else(|code| json!({"version":1,"ok":false,"error":{"code":code}}));
        output(&response).map_err(|_| "OUTPUT_IO")?;
    }
    Ok(())
}
fn main() {
    if let Err(code) = run() {
        let _ = output(&json!({"version":1,"ok":false,"error":{"code":code}}));
        std::process::exit(1);
    }
}
