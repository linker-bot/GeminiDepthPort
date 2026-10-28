use crate::{FrameLayout, LayoutError, PixelFormat};
use std::{error::Error, fmt};

/// Counts and extrema of the returned millimetre image, excluding row padding.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DepthStats {
    pub zero_samples: u64,
    pub nonzero_samples: u64,
    /// Nonzero source samples which rounded below 1 mm and were raised to 1 mm.
    pub clamped_samples: u64,
    /// Extrema include invalid zero samples. Valid frames always have samples.
    pub min_mm: Option<u16>,
    pub max_mm: Option<u16>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct NormalizedDepth {
    /// Tightly packed little-endian u16 millimetres; source memory is untouched.
    pub bytes: Vec<u8>,
    pub stats: DepthStats,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DepthNormalizationError {
    NotDepth16,
    InvalidScale,
    Layout(LayoutError),
    /// At least one nonzero source sample rounded outside u16 millimetres.
    OutputOverflow,
}

impl fmt::Display for DepthNormalizationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::NotDepth16 => f.write_str("depth normalization requires Depth16 layout"),
            Self::InvalidScale => f.write_str("depth scale must be finite and positive"),
            Self::Layout(error) => write!(f, "invalid depth layout: {error}"),
            Self::OutputOverflow => f.write_str("depth sample exceeds u16 millimetre range"),
        }
    }
}

impl Error for DepthNormalizationError {
    fn source(&self) -> Option<&(dyn Error + 'static)> {
        match self {
            Self::Layout(error) => Some(error),
            _ => None,
        }
    }
}

/// Convert trusted depth16 rows to packed LE u16 millimetres.
///
/// The supplied SDK scale is interpreted as millimetres per source unit. Its
/// f32 value is promoted to f64 before multiplication. Nonnegative values round
/// to nearest millimetre with exact halves rounding upward. Raw zero remains an
/// invalid zero; a nonzero value rounding to zero becomes 1 mm and is counted.
/// An out-of-range rounded value rejects the entire frame, never saturates it.
/// Padding is skipped, source bytes are never mutated, and no partially converted
/// output is returned on failure. This operation does not validate physical scale.
pub fn normalize_depth_mm(
    source: &[u8],
    layout: FrameLayout,
    source_big_endian: bool,
    source_scale_mm: f32,
) -> Result<NormalizedDepth, DepthNormalizationError> {
    if layout.format != PixelFormat::Depth16 {
        return Err(DepthNormalizationError::NotDepth16);
    }
    if !source_scale_mm.is_finite() || source_scale_mm <= 0.0 {
        return Err(DepthNormalizationError::InvalidScale);
    }
    layout
        .validate(source.len())
        .map_err(DepthNormalizationError::Layout)?;
    // These operations are bounded by the validated padded frame span.
    let row_bytes = layout.width as usize * 2;
    let output_len = row_bytes * layout.height as usize;
    let mut bytes = Vec::with_capacity(output_len);
    let mut stats = DepthStats {
        zero_samples: 0,
        nonzero_samples: 0,
        clamped_samples: 0,
        min_mm: None,
        max_mm: None,
    };
    let scale = f64::from(source_scale_mm);
    for row in source.chunks_exact(layout.stride) {
        for pair in row[..row_bytes].chunks_exact(2) {
            let raw = if source_big_endian {
                u16::from_be_bytes([pair[0], pair[1]])
            } else {
                u16::from_le_bytes([pair[0], pair[1]])
            };
            let mm = if raw == 0 {
                stats.zero_samples += 1;
                0
            } else {
                stats.nonzero_samples += 1;
                let rounded = (f64::from(raw) * scale + 0.5).floor();
                if rounded > f64::from(u16::MAX) {
                    return Err(DepthNormalizationError::OutputOverflow);
                }
                if rounded < 1.0 {
                    stats.clamped_samples += 1;
                    1
                } else {
                    rounded as u16
                }
            };
            stats.min_mm = Some(stats.min_mm.map_or(mm, |value| value.min(mm)));
            stats.max_mm = Some(stats.max_mm.map_or(mm, |value| value.max(mm)));
            bytes.extend_from_slice(&mm.to_le_bytes());
        }
    }
    debug_assert_eq!(bytes.len(), output_len);
    Ok(NormalizedDepth { bytes, stats })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn layout(width: u32, height: u32, stride: usize) -> FrameLayout {
        FrameLayout {
            width,
            height,
            stride,
            format: PixelFormat::Depth16,
        }
    }
    fn decode(bytes: &[u8]) -> Vec<u16> {
        bytes
            .chunks_exact(2)
            .map(|pair| u16::from_le_bytes([pair[0], pair[1]]))
            .collect()
    }

    #[test]
    fn exhaustive_identity_preserves_every_depth_value() {
        let source: Vec<u8> = (0..=u16::MAX).flat_map(u16::to_le_bytes).collect();
        let before = source.clone();
        let result =
            normalize_depth_mm(&source, layout(65_536, 1, source.len()), false, 1.0).unwrap();
        assert_eq!(result.bytes, source);
        assert_eq!(source, before);
        assert_eq!(
            result.stats,
            DepthStats {
                zero_samples: 1,
                nonzero_samples: 65_535,
                clamped_samples: 0,
                min_mm: Some(0),
                max_mm: Some(65_535)
            }
        );
    }

    #[test]
    fn exhaustive_tenth_mm_scale_rounds_and_counts_submillimetres() {
        let source: Vec<u8> = (0..=u16::MAX).flat_map(u16::to_le_bytes).collect();
        let result =
            normalize_depth_mm(&source, layout(65_536, 1, source.len()), false, 0.1).unwrap();
        for (raw, mm) in (0..=u16::MAX).zip(decode(&result.bytes)) {
            // Decimal tenth-mm reference is integer arithmetic independent of converter.
            let expected = if raw == 0 {
                0
            } else {
                ((u32::from(raw) + 5) / 10).max(1) as u16
            };
            assert_eq!(mm, expected, "raw sample {raw}");
        }
        assert_eq!(result.stats.clamped_samples, 4);
        assert_eq!(result.stats.zero_samples, 1);
        assert_eq!(result.stats.nonzero_samples, 65_535);
        assert_eq!(result.stats.min_mm, Some(0));
        assert_eq!(result.stats.max_mm, Some(6554));
    }

    #[test]
    fn skips_padding_handles_big_endian_and_preserves_source() {
        let source = [0, 1, 0, 3, 255, 255, 0, 5, 0, 0, 255, 255];
        let before = source;
        let result = normalize_depth_mm(&source, layout(2, 2, 6), true, 0.5).unwrap();
        assert_eq!(decode(&result.bytes), [1, 2, 3, 0]);
        assert_eq!(source, before);
        assert_eq!(result.stats.clamped_samples, 0);
        assert_eq!(result.stats.nonzero_samples, 3);
        assert_eq!(result.stats.max_mm, Some(3));
    }

    #[test]
    fn invalid_scale_and_layout_are_rejected() {
        for scale in [f32::NAN, f32::INFINITY, f32::NEG_INFINITY, 0.0, -0.0, -1.0] {
            assert_eq!(
                normalize_depth_mm(&[0, 0], layout(1, 1, 2), false, scale),
                Err(DepthNormalizationError::InvalidScale)
            );
        }
        assert_eq!(
            normalize_depth_mm(&[0], layout(1, 1, 2), false, 1.0),
            Err(DepthNormalizationError::Layout(LayoutError::PayloadLength))
        );
        assert_eq!(
            normalize_depth_mm(&[0, 0, 0], layout(1, 1, 3), false, 1.0),
            Err(DepthNormalizationError::Layout(LayoutError::InvalidStride))
        );
        assert_eq!(
            normalize_depth_mm(&[], layout(0, 1, 0), false, 1.0),
            Err(DepthNormalizationError::Layout(
                LayoutError::EmptyDimensions
            ))
        );
        let yuyv = FrameLayout {
            format: PixelFormat::Yuyv,
            ..layout(2, 1, 4)
        };
        assert_eq!(
            normalize_depth_mm(&[0; 4], yuyv, false, 1.0),
            Err(DepthNormalizationError::NotDepth16)
        );
    }

    #[test]
    fn overflow_rejects_whole_frame_and_never_saturates() {
        // The first pixel is representable; the second rejects the entire result.
        let source = [1, 0, 255, 255];
        assert_eq!(
            normalize_depth_mm(&source, layout(2, 1, 4), false, 2.0),
            Err(DepthNormalizationError::OutputOverflow)
        );
        assert_eq!(
            normalize_depth_mm(&[1, 0], layout(1, 1, 2), false, 65_535.5),
            Err(DepthNormalizationError::OutputOverflow)
        );
        assert_eq!(
            normalize_depth_mm(&[1, 0], layout(1, 1, 2), false, f32::MAX),
            Err(DepthNormalizationError::OutputOverflow)
        );
        assert_eq!(
            decode(
                &normalize_depth_mm(&[1, 0], layout(1, 1, 2), false, 65_535.0)
                    .unwrap()
                    .bytes
            ),
            [65_535]
        );
    }

    #[test]
    fn zero_invalids_and_nonzero_submillimetres_remain_distinct() {
        let source = [0, 0, 1, 0, 2, 0];
        let result =
            normalize_depth_mm(&source, layout(3, 1, 6), false, f32::MIN_POSITIVE).unwrap();
        assert_eq!(decode(&result.bytes), [0, 1, 1]);
        assert_eq!(result.stats.zero_samples, 1);
        assert_eq!(result.stats.nonzero_samples, 2);
        assert_eq!(result.stats.clamped_samples, 2);
        let all_zero = normalize_depth_mm(&[0; 4], layout(2, 1, 4), false, 0.1).unwrap();
        assert_eq!(all_zero.stats.min_mm, Some(0));
        assert_eq!(all_zero.stats.max_mm, Some(0));
    }
}
