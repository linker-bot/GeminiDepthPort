use capture_contract::{ClockDomain, QualityPolicy};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(
    tag = "kind",
    content = "domain",
    rename_all = "kebab-case",
    deny_unknown_fields
)]
pub enum SourceClock {
    Capture(ClockDomain),
    ObservedHeaderNanoseconds,
}
impl SourceClock {
    pub(crate) fn key(self) -> u8 {
        match self {
            Self::Capture(ClockDomain::DeviceMicroseconds) => 0,
            Self::Capture(ClockDomain::SdkSystemUnixMicroseconds) => 1,
            Self::Capture(ClockDomain::SessionMonotonicNanoseconds) => 2,
            Self::ObservedHeaderNanoseconds => 3,
        }
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SourceStamp {
    pub clock: SourceClock,
    pub value: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "kebab-case", deny_unknown_fields)]
pub enum ClockMapper {
    Unknown,
    DeclaredSdkSystemUnix,
    /// Caller measured/approved device-to-Unix offset; never inferred from now.
    MeasuredDeviceOffset {
        offset_ns: i64,
    },
    /// Explicit consumer assertion about a ROS header, not SDK provenance.
    DeclaredObservedUnixHeader,
    MeasuredObservedHeaderOffset {
        offset_ns: i64,
    },
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum MappingError {
    PolicyInvalid,
    UnknownMapping,
    ClockMismatch,
    Overflow,
    ZeroUnixTime,
    ReceiveUnixMissing,
    SourceTooOld,
    SourceInFuture,
}

pub fn map_source_time(source: SourceStamp, mapper: ClockMapper) -> Result<u64, MappingError> {
    let (base, offset) = match (source.clock, mapper) {
        (_, ClockMapper::Unknown) => return Err(MappingError::UnknownMapping),
        (
            SourceClock::Capture(ClockDomain::SdkSystemUnixMicroseconds),
            ClockMapper::DeclaredSdkSystemUnix,
        ) => (u128::from(source.value) * 1000, 0),
        (
            SourceClock::Capture(ClockDomain::DeviceMicroseconds),
            ClockMapper::MeasuredDeviceOffset { offset_ns },
        ) => (u128::from(source.value) * 1000, offset_ns),
        (SourceClock::ObservedHeaderNanoseconds, ClockMapper::DeclaredObservedUnixHeader) => {
            (u128::from(source.value), 0)
        }
        (
            SourceClock::ObservedHeaderNanoseconds,
            ClockMapper::MeasuredObservedHeaderOffset { offset_ns },
        ) => (u128::from(source.value), offset_ns),
        _ => return Err(MappingError::ClockMismatch),
    };
    let mapped = i128::try_from(base).map_err(|_| MappingError::Overflow)? + i128::from(offset);
    let unix = u64::try_from(mapped).map_err(|_| MappingError::Overflow)?;
    if unix == 0 {
        return Err(MappingError::ZeroUnixTime);
    }
    Ok(unix)
}
pub fn validate_source_age(
    source: SourceStamp,
    mapper: ClockMapper,
    received_unix_ns: Option<u64>,
    policy: &QualityPolicy,
) -> Result<u64, MappingError> {
    policy.validate().map_err(|_| MappingError::PolicyInvalid)?;
    let mapped = map_source_time(source, mapper)?;
    let received = received_unix_ns.ok_or(MappingError::ReceiveUnixMissing)?;
    if mapped > received {
        if mapped - received > policy.max_future_ns {
            return Err(MappingError::SourceInFuture);
        }
    } else if received - mapped > policy.max_source_age_ns {
        return Err(MappingError::SourceTooOld);
    }
    Ok(mapped)
}
