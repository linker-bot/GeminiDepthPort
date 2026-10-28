use anyhow::Result;
use geminidepthport::{calibration, config, control::Controller, generation, server, util::*};
use serde_json::{Value, json};
use std::{
    env,
    io::{BufRead, BufReader, Read, Write},
    net::{SocketAddr, TcpStream},
    path::PathBuf,
    time::Duration,
};
fn flag(a: &[String], key: &str) -> bool {
    a.iter().any(|s| s == key)
}
fn option(a: &[String], key: &str) -> Option<String> {
    a.iter()
        .position(|s| s == key)
        .and_then(|i| a.get(i + 1))
        .cloned()
}
fn main() {
    install_worker_signals();
    if let Err(e) = run() {
        eprintln!("ERROR: {e}");
        std::process::exit(1)
    }
}
fn run() -> Result<()> {
    let mut a: Vec<String> = env::args().skip(1).collect();
    let root = if let Some(i) = a.iter().position(|v| v == "--root") {
        require(i + 1 < a.len(), "ARGUMENT", "--root requires path")?;
        let root = a.remove(i + 1);
        a.remove(i);
        PathBuf::from(root)
    } else {
        env::var_os("ORBBEC_DEPLOY_DIR")
            .map(PathBuf::from)
            .unwrap_or(env::current_dir()?)
    };
    let cmd = a.first().map(String::as_str).unwrap_or("help");
    if ["help", "--help", "-h"].contains(&cmd)
        || a.iter().any(|v| ["--help", "-h"].contains(&v.as_str()))
    {
        println!(
            "geminidepthport [--root DIRECTORY] COMMAND\nCommands: serve, scan, validate [--file FILE] [--draft], migrate [--bindings FILE] [--apply --expected-plan HASH], render [--draft], preflight, build, up, boot, status, check, down, stack-stop [--remove], devices, image-ref, bind NAME SERIAL, set NAME KEY JSON, confirm-profile NAME, protect, release-protection, logs, calibration-export, http-health"
        );
        return Ok(());
    }
    validate_arguments(&a)?;
    if cmd == "http-health" {
        let port: u16 = option(&a, "--port").unwrap_or("8765".into()).parse()?;
        let addr = SocketAddr::from(([127, 0, 0, 1], port));
        let mut s = TcpStream::connect_timeout(&addr, Duration::from_secs(3))?;
        s.set_read_timeout(Some(Duration::from_secs(3)))?;
        s.write_all(
            format!("GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n")
                .as_bytes(),
        )?;
        let mut line = String::new();
        BufReader::new(s).take(1024).read_line(&mut line)?;
        require(
            line.split_whitespace().take(2).collect::<Vec<_>>() == ["HTTP/1.1", "200"],
            "HEALTH_FAILED",
            "Management unavailable",
        )?;
        return Ok(());
    }
    let ctl = Controller::new(root)?;
    if cmd == "serve" {
        return server::serve(
            ctl,
            option(&a, "--host").as_deref().unwrap_or("127.0.0.1"),
            option(&a, "--port").unwrap_or("8765".into()).parse()?,
        );
    }
    let read_only = [
        "scan",
        "validate",
        "image-ref",
        "status",
        "logs",
        "check",
        "preflight",
        "calibration-export",
        "release-protection",
    ];
    let _lock = if (cmd == "migrate" && !flag(&a, "--apply"))
        || ["scan", "validate", "image-ref", "status", "logs"].contains(&cmd)
    {
        None
    } else {
        Some(ctl.lock(!read_only.contains(&cmd))?)
    };
    if cmd == "scan" {
        require(
            cfg!(target_os = "linux"),
            "PLATFORM_UNSUPPORTED",
            "Discovery requires Linux",
        )?;
        println!("{}", ctl.scan()?);
        return Ok(());
    }
    if cmd == "migrate" {
        let bindings = option(&a, "--bindings")
            .map(|p| read_json(&PathBuf::from(p)))
            .transpose()?
            .unwrap_or(Value::Null);
        println!(
            "{}",
            ctl.migrate(
                &bindings,
                flag(&a, "--apply"),
                &option(&a, "--expected-plan").unwrap_or_default()
            )?
        );
        return Ok(());
    }
    if cmd == "release-protection" {
        let mut token = String::new();
        std::io::stdin().take(4096).read_to_string(&mut token)?;
        ctl.release(token.trim())?;
        return Ok(());
    }
    // Shutdown uses discovered ownership labels even if inventory is missing or malformed.
    let independent = match cmd {
        "down" => Some(ctl.stop("camera", false)?),
        "stack-stop" => {
            ctl.stop("preview", true)?;
            ctl.stop("camera", flag(&a, "--remove"))?;
            Some(ctl.stop("management", flag(&a, "--remove"))?)
        }
        "status" => {
            print!("{}", ctl.compose(&["ps", "-a"])?);
            return Ok(());
        }
        "logs" => {
            for item in ctl.instances("camera")? {
                print!(
                    "{}",
                    ctl.run(&["docker", "logs", "--tail", "100", text(&item["Id"])])?
                );
            }
            return Ok(());
        }
        _ => None,
    };
    if let Some(result) = independent {
        println!("{}", serde_json::to_string(&result)?);
        return Ok(());
    }
    let (d, inventory_revision) = if cmd == "validate" && option(&a, "--file").is_some() {
        (
            read_json(&PathBuf::from(option(&a, "--file").unwrap()))?,
            None,
        )
    } else {
        let (data, revision) = ctl.load_revision()?;
        (data, Some(revision))
    };
    let result = match cmd {
        "validate" => {
            config::validate(&d, flag(&a, "--draft"))?;
            json!({"valid":true,"schema_version":d["schema_version"],"enabled_count":config::active(&d).len(),"hardware_verified":false})
        }
        "image-ref" => {
            config::validate(&d, true)?;
            println!("{}", text(&d["image"]));
            return Ok(());
        }
        "render" => {
            let s = generation::create(&ctl, &d, flag(&a, "--draft"))?;
            json!({"generated":true,"draft":s["draft"],"camera_count":s["cameras"].as_array().unwrap().len()})
        }
        "preflight" => {
            ctl.preflight(&d)?;
            json!({"ready":true})
        }
        "build" => ctl.build(&d)?,
        "up" | "boot" => {
            if let Err(e) = config::validate(&d, false) {
                ctl.record(&json!({"ready":false,"status":"configuration_required","error":"CONFIGURATION_REQUIRED"}))?;
                if cmd == "boot" {
                    println!("Management is available; configuration verification required.");
                    return Ok(());
                }
                return Err(e);
            }
            let mut job = json!({"id":random(6),"action":cmd,"status":"running","started_at":now(),"inventory_revision":ctl.revision()?,"log":["Checked startup requested."]});
            if cmd == "boot" {
                server::persist_job(&ctl, &job)?;
            }
            let outcome = ctl.ensure_started(&d);
            job["status"] = (if outcome.is_ok() { "passed" } else { "failed" }).into();
            job["finished_at"] = now().into();
            job["log"]
                .as_array_mut()
                .unwrap()
                .push(json!(if outcome.is_ok() {
                    "All required streams passed actual checks."
                } else {
                    "Startup failed; inspect controlled lifecycle status."
                }));
            if cmd == "boot" {
                server::persist_job(&ctl, &job)?;
            }
            outcome?
        }
        "protect" => ctl.protect()?,
        "calibration-export" => calibration::export(&ctl).map_err(|_| {
            anyhow::anyhow!("CALIBRATION_EXPORT_FAILED: Inspect controlled calibration status")
        })?,
        "check" => {
            let s = ctl.check_render(&d)?;
            for c in s["cameras"].as_array().unwrap() {
                ctl.probe(text(&c["service"]))?;
            }
            json!({"ready":true})
        }
        "devices" => {
            config::validate(&d, true)?;
            require(
                geminidepthport::capture_backend::selected(&d) != "rust",
                "RUST_DEVICES_UNSUPPORTED",
                "Use scan for Rust backend discovery; official ROS enumerator is not included",
            )?;
            ctl.assert_stopped()?;
            let output = ctl.run(&[
                "docker",
                "run",
                "--rm",
                "--network",
                "host",
                "--cap-drop",
                "ALL",
                "--device-cgroup-rule",
                "c 189:* rwm",
                "-v",
                "/dev/bus/usb:/dev/bus/usb:rw",
                "-v",
                "/run/udev:/run/udev:ro",
                text(&d["image"]),
                "ros2",
                "run",
                "orbbec_camera",
                "list_devices_node",
            ])?;
            print!("{output}");
            return Ok(());
        }
        "bind" | "set" => {
            require(
                a.len() == if cmd == "set" { 4 } else { 3 },
                "ARGUMENT",
                "Expected camera and values",
            )?;
            config::validate(&d, true)?;
            let mut data = d.clone();
            let c = data["cameras"]
                .as_array_mut()
                .and_then(|v| v.iter_mut().find(|c| c["name"] == a[1]))
                .ok_or_else(|| anyhow::anyhow!("CAMERA_ID: Unknown camera"))?;
            if cmd == "bind" {
                c["serial_number"] = a[2].clone().into();
            } else {
                c["parameters"][&a[2]] = serde_json::from_str(&a[3])?;
            }
            ctl.save_at_revision(&data, false, inventory_revision.as_deref().unwrap())?;
            json!({"saved":true})
        }
        "confirm-profile" => {
            require(a.len() == 2, "ARGUMENT", "Expected camera")?;
            server::confirm(&ctl, &a[1])?
        }
        _ => anyhow::bail!("ARGUMENT: Unknown command"),
    };
    println!("{}", serde_json::to_string(&result)?);
    Ok(())
}

fn validate_arguments(a: &[String]) -> Result<()> {
    let cmd = a[0].as_str();
    let (values, flags, positional): (&[&str], &[&str], usize) = match cmd {
        "serve" => (&["--host", "--port"], &[], 0),
        "http-health" => (&["--port"], &[], 0),
        "validate" => (&["--file"], &["--draft"], 0),
        "migrate" => (&["--bindings", "--expected-plan"], &["--apply"], 0),
        "render" => (&[], &["--draft"], 0),
        "stack-stop" => (&[], &["--remove"], 0),
        "bind" => (&[], &[], 2),
        "set" => (&[], &[], 3),
        "confirm-profile" => (&[], &[], 1),
        "scan" | "preflight" | "build" | "up" | "boot" | "status" | "check" | "down"
        | "devices" | "image-ref" | "protect" | "release-protection" | "logs"
        | "calibration-export" => (&[], &[], 0),
        _ => anyhow::bail!("ARGUMENT: Unknown command"),
    };
    if positional > 0 {
        return require(
            a.len() == positional + 1,
            "ARGUMENT",
            "Incorrect number of arguments",
        );
    }
    let mut seen = std::collections::BTreeSet::new();
    let mut i = 1;
    while i < a.len() {
        let key = a[i].as_str();
        require(seen.insert(key), "ARGUMENT", "Duplicate option")?;
        if values.contains(&key) {
            require(
                a.get(i + 1).is_some_and(|v| !v.starts_with("--")),
                "ARGUMENT",
                "Option requires a value",
            )?;
            i += 2;
        } else {
            require(
                flags.contains(&key),
                "ARGUMENT",
                "Unsupported option or argument",
            )?;
            i += 1;
        }
    }
    Ok(())
}
