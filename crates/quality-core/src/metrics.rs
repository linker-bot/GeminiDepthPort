use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
/// Numerical u16 samples only: nonzero is not physical depth certification.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DepthPixelMetrics {
    pub samples: u64,
    pub zero_samples: u64,
    pub nonzero_samples: u64,
    pub max_u16_samples: u64,
}
impl DepthPixelMetrics {
    pub fn validate(self) -> Result<(), &'static str> {
        if self.samples == 0
            || self.samples > u64::MAX / 100_000
            || self.zero_samples.checked_add(self.nonzero_samples) != Some(self.samples)
            || self.max_u16_samples > self.nonzero_samples
        {
            Err("PIXEL_METRICS_INVALID")
        } else {
            Ok(())
        }
    }
    pub fn valid_fraction(self) -> f64 {
        self.nonzero_samples as f64 / self.samples as f64
    }
    pub fn zero_fraction(self) -> f64 {
        self.zero_samples as f64 / self.samples as f64
    }
    pub fn max_u16_fraction(self) -> f64 {
        self.max_u16_samples as f64 / self.samples as f64
    }
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ScalarSummary {
    pub observations: usize,
    pub min: f64,
    pub max: f64,
    pub mean: f64,
}
pub(crate) fn summarize(values: impl Iterator<Item = f64>) -> Option<ScalarSummary> {
    let mut result: Option<ScalarSummary> = None;
    for value in values {
        match &mut result {
            None => {
                result = Some(ScalarSummary {
                    observations: 1,
                    min: value,
                    max: value,
                    mean: value,
                })
            }
            Some(s) => {
                s.observations += 1;
                s.min = s.min.min(value);
                s.max = s.max.max(value);
                s.mean += (value - s.mean) / s.observations as f64;
            }
        }
    }
    result
}
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StreamWindowMetrics {
    pub active_window_fps: Option<f64>,
    pub max_receive_gap_ns: Option<u64>,
    pub source_age_ns: Option<ScalarSummary>,
    pub depth_pixels: Option<DepthPixelMetrics>,
    pub valid_fraction: Option<f64>,
    pub zero_fraction: Option<f64>,
    pub max_u16_fraction: Option<f64>,
    pub camera_info_observed: Option<bool>,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum DiagnosticKind {
    MissingColor,
    MissingDepth,
    MissingBoth,
    MetadataInvalid,
    PixelPayloadInvalid,
    BindingChanged,
    DepthScaleChanged,
    PairSkew,
    PairRejected,
    QueueOverflow,
    PublicationFailed,
    PublicationPartial,
    RawRecordingFailed,
}
#[derive(Debug, Clone, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DiagnosticSnapshot {
    pub session_counts: BTreeMap<DiagnosticKind, u64>,
    pub window_counts: BTreeMap<DiagnosticKind, u64>,
}
#[derive(Debug, Clone)]
pub(crate) struct MetricSample {
    pub age_ns: Option<f64>,
    pub pixels: Option<DepthPixelMetrics>,
    pub base_passed: bool,
    pub contract_valid: bool,
}
