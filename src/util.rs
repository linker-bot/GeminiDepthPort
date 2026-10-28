use anyhow::{Result, bail};
use ring::{
    digest,
    rand::{SecureRandom, SystemRandom},
};
use serde_json::Value;
use std::{
    collections::BTreeSet,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::{
        fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt},
        process::CommandExt,
    },
    path::Path,
    process::{Command, Stdio},
    sync::{
        Arc, Mutex, OnceLock,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
pub fn require(ok: bool, code: &str, message: &str) -> Result<()> {
    if !ok {
        bail!("{code}: {message}");
    }
    Ok(())
}
pub fn hash(data: impl AsRef<[u8]>) -> String {
    digest::digest(&digest::SHA256, data.as_ref())
        .as_ref()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}
pub fn file_hash(path: &Path) -> Result<String> {
    Ok(hash(fs::read(path)?))
}
pub fn random(bytes: usize) -> String {
    let mut b = vec![0; bytes];
    SystemRandom::new()
        .fill(&mut b)
        .expect("OS entropy unavailable");
    b.iter().map(|x| format!("{x:02x}")).collect()
}
pub fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
pub fn read_json(path: &Path) -> Result<Value> {
    Ok(serde_json::from_slice(&fs::read(path)?)?)
}
pub fn private_dir(path: &Path) -> Result<()> {
    fs::create_dir_all(path)?;
    fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    Ok(())
}
pub fn atomic(path: &Path, content: &[u8]) -> Result<()> {
    atomic_checked(path, content, || Ok(()))
}
pub fn atomic_checked(
    path: &Path,
    content: &[u8],
    check: impl FnOnce() -> Result<()>,
) -> Result<()> {
    let parent = path.parent().unwrap();
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(parent)?;
    let mut tmp = tempfile::NamedTempFile::new_in(parent)?;
    tmp.as_file()
        .set_permissions(fs::Permissions::from_mode(0o600))?;
    tmp.write_all(content)?;
    tmp.as_file().sync_all()?;
    check()?;
    tmp.persist(path)?;
    File::open(parent)?.sync_all()?;
    Ok(())
}
pub fn atomic_json(path: &Path, data: &Value) -> Result<()> {
    let mut b = serde_json::to_vec_pretty(data)?;
    b.push(b'\n');
    atomic(path, &b)
}
pub fn write_private(path: &Path, data: &[u8]) -> Result<()> {
    private_dir(path.parent().unwrap())?;
    let mut f = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?;
    f.write_all(data)?;
    f.sync_all()?;
    Ok(())
}
pub fn text(v: &Value) -> &str {
    v.as_str().unwrap_or("")
}
pub fn matches(pattern: &str, s: &str) -> bool {
    regex::Regex::new(pattern).unwrap().is_match(s)
}
// Python json.dumps(sort_keys=True, separators=(',', ':'), ensure_ascii=True).
// Existing confirmation, request and migration digests are shared with ROS helpers.
pub fn canonical(v: &Value) -> String {
    match v {
        Value::String(s) => {
            let encoded = serde_json::to_string(s).unwrap();
            let mut out = String::new();
            for c in encoded.chars() {
                if c <= '~' {
                    out.push(c);
                } else {
                    let mut units = [0; 2];
                    for u in c.encode_utf16(&mut units) {
                        out.push_str(&format!("\\u{u:04x}"));
                    }
                }
            }
            out
        }
        Value::Array(a) => format!(
            "[{}]",
            a.iter().map(canonical).collect::<Vec<_>>().join(",")
        ),
        Value::Object(m) => format!(
            "{{{}}}",
            m.iter()
                .map(|(k, v)| format!("{}:{}", canonical(&Value::String(k.clone())), canonical(v)))
                .collect::<Vec<_>>()
                .join(",")
        ),
        Value::Number(n) if n.is_f64() => python_float(&n.to_string()),
        _ => v.to_string(),
    }
}
fn python_float(s: &str) -> String {
    let (sign, unsigned) = if let Some(v) = s.strip_prefix('-') {
        ("-", v)
    } else {
        ("", s)
    };
    let (mantissa, power) = unsigned
        .split_once('e')
        .map(|(m, e)| (m, e.parse::<i32>().unwrap()))
        .unwrap_or((unsigned, 0));
    let decimal = mantissa.find('.').unwrap_or(mantissa.len()) as i32;
    let raw: String = mantissa.chars().filter(|c| *c != '.').collect();
    let Some(first) = raw.find(|c| c != '0') else {
        return format!("{sign}0.0");
    };
    let exponent = decimal - first as i32 - 1 + power;
    let digits = raw[first..].trim_end_matches('0');
    if !(-4..16).contains(&exponent) {
        let m = if digits.len() == 1 {
            digits.to_string()
        } else {
            format!("{}.{}", &digits[..1], &digits[1..])
        };
        format!(
            "{sign}{m}e{}{:02}",
            if exponent < 0 { "-" } else { "+" },
            exponent.abs()
        )
    } else {
        let point = exponent + 1;
        if point <= 0 {
            format!("{sign}0.{}{digits}", "0".repeat((-point) as usize))
        } else if point as usize >= digits.len() {
            format!(
                "{sign}{digits}{}.0",
                "0".repeat(point as usize - digits.len())
            )
        } else {
            format!(
                "{sign}{}.{}",
                &digits[..point as usize],
                &digits[point as usize..]
            )
        }
    }
}
static COMMANDS: OnceLock<Mutex<BTreeSet<i32>>> = OnceLock::new();
static CANCELLED: AtomicBool = AtomicBool::new(false);
extern "C" fn cancel_signal(_: libc::c_int) {
    CANCELLED.store(true, Ordering::Release);
}
pub fn install_worker_signals() {
    if std::env::var_os("GDP_JOB_WORKER").is_some() {
        unsafe {
            libc::signal(libc::SIGTERM, cancel_signal as *const () as usize);
            libc::signal(libc::SIGINT, cancel_signal as *const () as usize);
        }
    }
}
pub fn cancel_commands() {
    CANCELLED.store(true, Ordering::Release);
    for pid in COMMANDS
        .get_or_init(Default::default)
        .lock()
        .unwrap()
        .iter()
    {
        unsafe {
            libc::kill(-*pid, libc::SIGTERM);
        }
    }
}
pub fn reset_commands() {
    CANCELLED.store(false, Ordering::Release);
}
struct CommandGroup(i32);
impl Drop for CommandGroup {
    fn drop(&mut self) {
        if self.0 != 0 {
            unsafe {
                libc::kill(-self.0, libc::SIGKILL);
            }
            COMMANDS
                .get_or_init(Default::default)
                .lock()
                .unwrap()
                .remove(&self.0);
        }
    }
}
pub type LogSink = Arc<dyn Fn(&str) + Send + Sync>;
pub fn command(root: &Path, args: &[String], timeout: u64, input: Option<&[u8]>) -> Result<String> {
    command_logged(root, args, timeout, input, None)
}
pub fn command_logged(
    root: &Path,
    args: &[String],
    timeout: u64,
    input: Option<&[u8]>,
    sink: Option<LogSink>,
) -> Result<String> {
    require(!args.is_empty(), "COMMAND", "Missing executable")?;
    require(
        !CANCELLED.load(Ordering::Acquire),
        "COMMAND_CANCELLED",
        "Management is shutting down",
    )?;
    let mut c = Command::new(&args[0]);
    c.args(&args[1..])
        .current_dir(root)
        .env("ORBBEC_DEPLOY_DIR", root)
        .stdin(if input.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let in_worker = std::env::var_os("GDP_JOB_WORKER").is_some();
    c.process_group(0);
    if args.get(1).is_some_and(|v| v == "--root") {
        c.env("GDP_JOB_WORKER", "1");
    }
    let mut child = c.spawn()?;
    let pid = child.id() as i32;
    let _group = CommandGroup(pid);
    {
        let mut groups = COMMANDS.get_or_init(Default::default).lock().unwrap();
        groups.insert(pid);
        if CANCELLED.load(Ordering::Acquire) {
            unsafe {
                libc::kill(-pid, libc::SIGTERM);
            }
        }
    }
    let stdout = child.stdout.take().unwrap();
    let stderr = child.stderr.take().unwrap();
    fn drain(mut r: impl Read, sink: Option<LogSink>) -> Vec<u8> {
        let mut pending = Vec::new();
        let mut tail = Vec::new();
        let mut buf = [0; 8192];
        while let Ok(n) = r.read(&mut buf) {
            if n == 0 {
                break;
            }
            if let Some(ref sink) = sink {
                pending.extend_from_slice(&buf[..n]);
                while let Some(i) = pending.iter().position(|b| *b == b'\n') {
                    sink(&String::from_utf8_lossy(&pending[..i]));
                    pending.drain(..=i);
                }
                if pending.len() > 8192 {
                    sink(&String::from_utf8_lossy(&pending));
                    pending.clear();
                }
            }
            tail.extend_from_slice(&buf[..n]);
            if tail.len() > 512 * 1024 {
                tail.drain(..tail.len() - 512 * 1024);
            }
        }
        if let Some(sink) = sink {
            if !pending.is_empty() {
                sink(&String::from_utf8_lossy(&pending));
            }
        }
        tail
    }
    let combined = args
        .get(1)
        .is_some_and(|v| ["build", "logs", "--root"].contains(&v.as_str()));
    let sink = if in_worker && combined && sink.is_none() {
        Some(Arc::new(|line: &str| eprintln!("{line}")) as LogSink)
    } else {
        sink
    };
    let stdout_sink = sink.clone();
    let out = thread::spawn(move || drain(stdout, stdout_sink));
    let err = thread::spawn(move || drain(stderr, sink));
    let writer = input.map(|b| {
        let mut stdin = child.stdin.take().unwrap();
        let bytes = b.to_vec();
        thread::spawn(move || stdin.write_all(&bytes))
    });
    let deadline = Instant::now() + Duration::from_secs(timeout);
    let worker_command = args.get(1).is_some_and(|v| v == "--root");
    let mut reason = None;
    let mut terminate_at = None;
    let status = loop {
        let now = Instant::now();
        if reason.is_none() {
            if CANCELLED.load(Ordering::Acquire) {
                reason = Some("COMMAND_CANCELLED");
            } else if now >= deadline {
                reason = Some("COMMAND_TIMEOUT");
            }
        }
        if reason.is_some() && terminate_at.is_none() {
            // A Rust worker receives SIGTERM first and cancels its own external
            // command groups; then the outer supervisor enforces a bounded exit.
            unsafe {
                libc::kill(
                    -pid,
                    if worker_command {
                        libc::SIGTERM
                    } else {
                        libc::SIGKILL
                    },
                );
            }
            terminate_at = Some(now + Duration::from_secs(1));
        }
        if let Some(s) = child.try_wait()? {
            break s;
        }
        if terminate_at.is_some_and(|end| now >= end) {
            unsafe {
                libc::kill(-pid, libc::SIGKILL);
            }
            break child.wait()?;
        }
        thread::sleep(Duration::from_millis(20));
    };
    // Descendants must not keep stdout/stderr open after their leader exits.
    unsafe {
        libc::kill(-pid, libc::SIGKILL);
    }
    if let Some(w) = writer {
        let _ = w.join();
    }
    let output = out.join().unwrap_or_default();
    let errors = err.join().unwrap_or_default();
    if let Some(code) = reason {
        anyhow::bail!("{code}: External command interrupted");
    }
    if !status.success() {
        let tail = String::from_utf8_lossy(&errors);
        bail!(
            "COMMAND_FAILED: {}",
            tail.chars()
                .rev()
                .take(3000)
                .collect::<String>()
                .chars()
                .rev()
                .collect::<String>()
        );
    }
    let mut output = String::from_utf8_lossy(&output).into_owned();
    if combined {
        output.push_str(&String::from_utf8_lossy(&errors));
    }
    Ok(output)
}
pub fn args(values: &[&str]) -> Vec<String> {
    values.iter().map(|v| v.to_string()).collect()
}
