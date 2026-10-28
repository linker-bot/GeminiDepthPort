//! Strict input parsing precedes content hashing. serde_json::Value alone would
//! discard duplicate keys, so callers must not parse untrusted JSON themselves.
use crate::{ContractError, ErrorClass, Result, check};
use serde::{
    Deserialize,
    de::{self, MapAccess, SeqAccess, Visitor},
};
use serde_json::{Map, Number, Value};
use std::fmt;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ContentKind {
    Configuration,
    Calibration,
    Snapshot,
}
impl ContentKind {
    fn name(self) -> &'static str {
        match self {
            Self::Configuration => "configuration",
            Self::Calibration => "calibration",
            Self::Snapshot => "snapshot",
        }
    }
    fn id_field(self) -> &'static str {
        match self {
            Self::Configuration => "configuration_id",
            Self::Calibration => "calibration_id",
            Self::Snapshot => "snapshot_id",
        }
    }
}
struct Strict(Value);
impl<'de> Deserialize<'de> for Strict {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = Strict;
            fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str("JSON with unique object keys and finite numbers")
            }
            fn visit_bool<E: de::Error>(self, v: bool) -> std::result::Result<Strict, E> {
                Ok(Strict(Value::Bool(v)))
            }
            fn visit_i64<E: de::Error>(self, v: i64) -> std::result::Result<Strict, E> {
                Ok(Strict(Value::Number(v.into())))
            }
            fn visit_u64<E: de::Error>(self, v: u64) -> std::result::Result<Strict, E> {
                Ok(Strict(Value::Number(v.into())))
            }
            fn visit_f64<E: de::Error>(self, v: f64) -> std::result::Result<Strict, E> {
                Number::from_f64(v)
                    .map(|n| Strict(Value::Number(n)))
                    .ok_or_else(|| E::custom("nonfinite"))
            }
            fn visit_str<E: de::Error>(self, v: &str) -> std::result::Result<Strict, E> {
                Ok(Strict(Value::String(v.into())))
            }
            fn visit_string<E: de::Error>(self, v: String) -> std::result::Result<Strict, E> {
                Ok(Strict(Value::String(v)))
            }
            fn visit_unit<E: de::Error>(self) -> std::result::Result<Strict, E> {
                Ok(Strict(Value::Null))
            }
            fn visit_none<E: de::Error>(self) -> std::result::Result<Strict, E> {
                self.visit_unit()
            }
            fn visit_seq<A: SeqAccess<'de>>(
                self,
                mut a: A,
            ) -> std::result::Result<Strict, A::Error> {
                let mut out = vec![];
                while let Some(v) = a.next_element::<Strict>()? {
                    out.push(v.0);
                }
                Ok(Strict(Value::Array(out)))
            }
            fn visit_map<A: MapAccess<'de>>(
                self,
                mut a: A,
            ) -> std::result::Result<Strict, A::Error> {
                let mut out = Map::new();
                while let Some(k) = a.next_key::<String>()? {
                    if out.contains_key(&k) {
                        return Err(de::Error::custom("duplicate key"));
                    }
                    out.insert(k, a.next_value::<Strict>()?.0);
                }
                Ok(Strict(Value::Object(out)))
            }
        }
        d.deserialize_any(V)
    }
}
/// Limit documents to 4 MiB; serde_json's recursion limit also remains enabled.
pub fn parse_strict(bytes: &[u8]) -> Result<Value> {
    check(
        bytes.len() <= 4 * 1024 * 1024,
        ErrorClass::InvalidInput,
        "JSON_SIZE",
    )?;
    serde_json::from_slice::<Strict>(bytes)
        .map(|v| v.0)
        .map_err(|_| ContractError::new(ErrorClass::InvalidInput, "JSON_INVALID_OR_DUPLICATE"))
}
/// Domain-separated SHA256, compact sorted JSON. This is deliberately not the
/// legacy generation hash and never changes existing generation manifests.
pub fn content_digest(kind: ContentKind, bytes: &[u8]) -> Result<String> {
    let value = parse_strict(bytes)?;
    check(
        value.is_object(),
        ErrorClass::InvalidInput,
        "CONTENT_OBJECT_REQUIRED",
    )?;
    check(
        value.get(kind.id_field()).is_none() && value.get("content_sha256").is_none(),
        ErrorClass::Integrity,
        "SELF_CONTAINING_DIGEST",
    )?;
    let mut context = ring::digest::Context::new(&ring::digest::SHA256);
    context.update(b"geminidepthport:capture-contract:v1:");
    context.update(kind.name().as_bytes());
    context.update(&[0]);
    context.update(
        &serde_json::to_vec(&value)
            .map_err(|_| ContractError::new(ErrorClass::InvalidInput, "JSON_INVALID"))?,
    );
    Ok(crate::lower_hex(context.finish().as_ref()))
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn rejects_nested_duplicates_and_nonfinite() {
        for raw in [
            br#"{"a":{"x":1,"x":2}}"#.as_slice(),
            br#"{"a":NaN}"#,
            br#"{"a":1e999}"#,
            br#"{"a":1} trailing"#,
        ] {
            assert!(parse_strict(raw).is_err());
        }
    }
    #[test]
    fn canonical_order_and_domains() {
        let a = br#"{"b":2,"a":1}"#;
        let b = br#"{ "a":1, "b":2 }"#;
        assert_eq!(
            content_digest(ContentKind::Snapshot, a).unwrap(),
            content_digest(ContentKind::Snapshot, b).unwrap()
        );
        assert_ne!(
            content_digest(ContentKind::Snapshot, a).unwrap(),
            content_digest(ContentKind::Calibration, a).unwrap()
        );
    }
    #[test]
    fn refuses_digest_in_own_preimage_but_allows_binding_ids() {
        assert!(content_digest(ContentKind::Snapshot, br#"{"snapshot_id":"abc"}"#).is_err());
        assert!(content_digest(ContentKind::Snapshot, br#"{"calibration_id":"abc"}"#).is_ok());
    }
}
