//! Explicit, fingerprinted pre-SDK USB transport recovery. No retry or camera-setting changes.
use anyhow::{Context, Result, ensure};
use ring::digest;
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    path::{Component, PathBuf},
    process::{Command, Stdio},
};
pub(crate) struct Recovery {
    helper: PathBuf,
    sha256: String,
}
impl Recovery {
    pub(crate) fn parse(value: &Value) -> Result<Self> {
        let object = value.as_object().context("USB recovery object")?;
        ensure!(
            object.len() == 2 && object.contains_key("helper") && object.contains_key("sha256"),
            "USB recovery schema"
        );
        let helper = PathBuf::from(value["helper"].as_str().context("USB recovery helper")?);
        ensure!(
            helper.is_absolute()
                && !helper
                    .components()
                    .any(|p| matches!(p, Component::ParentDir)),
            "absolute USB recovery helper required"
        );
        let sha256 = value["sha256"]
            .as_str()
            .context("USB recovery digest")?
            .to_owned();
        capture_contract::ConfigurationId::parse(&sha256)?;
        Ok(Self { helper, sha256 })
    }
    pub(crate) fn verify(&self) -> Result<()> {
        let mut text = String::with_capacity(64);
        const HEX: &[u8] = b"0123456789abcdef";
        for b in digest::digest(&digest::SHA256, &fs::read(&self.helper)?).as_ref() {
            text.push(HEX[(b >> 4) as usize] as char);
            text.push(HEX[(b & 15) as usize] as char);
        }
        ensure!(text == self.sha256, "USB recovery artifact mismatch");
        Ok(())
    }
    pub(crate) fn execute(&self, serial: &str) -> Result<Value> {
        self.verify()?;
        let mut child = Command::new(&self.helper)
            .arg("--parent-pid")
            .arg(std::process::id().to_string())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()?;
        // Identity is private stdin, never process arguments or public output.
        let operation = (|| -> Result<Value> {
            child
                .stdin
                .take()
                .context("USB recovery stdin")?
                .write_all(serial.as_bytes())?;
            let mut bytes = Vec::new();
            child
                .stdout
                .take()
                .context("USB recovery stdout")?
                .take(4097)
                .read_to_end(&mut bytes)?;
            ensure!(bytes.len() <= 4096, "USB recovery response limit");
            let status = child.wait()?;
            if !status.success() {
                let code = capture_contract::parse_strict(&bytes)
                    .ok()
                    .and_then(|v| v["code"].as_str().map(str::to_owned))
                    .filter(|s| {
                        s.len() <= 64 && s.bytes().all(|b| b.is_ascii_uppercase() || b == b'_')
                    })
                    .unwrap_or_else(|| "USB_RECOVERY_FAILED".into());
                anyhow::bail!("USB stream recovery failed: {code}");
            }
            let value = capture_contract::parse_strict(&bytes)?;
            ensure!(
                value.as_object().is_some_and(|m| m.len() == 4)
                    && value["version"] == 1
                    && value["complete"] == true
                    && value["interfaces"]
                        .as_u64()
                        .is_some_and(|n| (1..=255).contains(&n))
                    && value["endpoints"]
                        .as_u64()
                        .is_some_and(|n| (1..=255).contains(&n)),
                "USB recovery response invalid"
            );
            Ok(
                json!({"version":1,"state":"complete","action":"clear-video-bulk-in-endpoints","interfaces":value["interfaces"],"endpoints":value["endpoints"]}),
            )
        })();
        if operation.is_err() {
            let _ = child.kill();
            let _ = child.wait();
        }
        operation
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn policy_is_explicit_closed_and_fingerprinted() {
        assert!(Recovery::parse(&json!({"helper":"relative","sha256":"a".repeat(64)})).is_err());
        assert!(
            Recovery::parse(&json!({"helper":"/helper","sha256":"a".repeat(64),"retry":3}))
                .is_err()
        );
        assert!(Recovery::parse(&json!({"helper":"/helper","sha256":"bad"})).is_err());
        let path = std::env::current_exe().unwrap();
        let policy = Recovery::parse(&json!({"helper":path,"sha256":"a".repeat(64)})).unwrap();
        assert!(policy.verify().is_err());
    }
}
