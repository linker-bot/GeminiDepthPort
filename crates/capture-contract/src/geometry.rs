use crate::{ErrorClass, Result, check, token};
use serde::{Deserialize, Serialize};
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum DistortionModel {
    None,
    PlumbBob,
    RationalPolynomial,
    Equidistant,
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CameraModel {
    pub frame_id: String,
    pub width: u32,
    pub height: u32,
    /// Pinhole intrinsic matrix, row-major. Image coordinates are pixels.
    pub k: [f64; 9],
    pub distortion_model: DistortionModel,
    /// ROS/OpenCV ordering: plumb [k1,k2,p1,p2,k3], rational adds [k4,k5,k6].
    /// Equidistant uses [k1,k2,k3,k4]; unsupported modified/inverse models fail.
    pub distortion_coefficients: Vec<f64>,
}
impl CameraModel {
    pub fn validate(&self) -> Result<()> {
        check(
            token(&self.frame_id)
                && self.width > 0
                && self.height > 0
                && self.width <= 16384
                && self.height <= 16384,
            ErrorClass::Geometry,
            "CAMERA_BINDING_INVALID",
        )?;
        check(
            self.k.iter().all(|v| v.is_finite())
                && self.distortion_coefficients.iter().all(|v| v.is_finite()),
            ErrorClass::Geometry,
            "CAMERA_NONFINITE",
        )?;
        check(
            self.k[0] > 0.
                && self.k[4] > 0.
                && self.k[3].abs() < 1e-9
                && self.k[6].abs() < 1e-9
                && self.k[7].abs() < 1e-9
                && (self.k[8] - 1.).abs() < 1e-9,
            ErrorClass::Geometry,
            "CAMERA_INTRINSICS_INVALID",
        )?;
        let count = match self.distortion_model {
            DistortionModel::None => 0,
            DistortionModel::PlumbBob => 5,
            DistortionModel::RationalPolynomial => 8,
            DistortionModel::Equidistant => 4,
        };
        check(
            self.distortion_coefficients.len() == count,
            ErrorClass::Geometry,
            "CAMERA_DISTORTION_COUNT",
        )
    }
}
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RigidTransform {
    pub source_frame: String,
    pub target_frame: String,
    /// p_target_m = rotation_row_major * p_source_m + translation_m.
    pub rotation_row_major: [f64; 9],
    pub translation_m: [f64; 3],
}
impl RigidTransform {
    pub fn validate(&self) -> Result<()> {
        check(
            token(&self.source_frame)
                && token(&self.target_frame)
                && self.source_frame != self.target_frame,
            ErrorClass::Geometry,
            "TRANSFORM_FRAMES_INVALID",
        )?;
        let r = &self.rotation_row_major;
        check(
            r.iter()
                .chain(self.translation_m.iter())
                .all(|v| v.is_finite()),
            ErrorClass::Geometry,
            "TRANSFORM_NONFINITE",
        )?;
        let orthogonal = (0..3).all(|i| {
            (0..3).all(|j| {
                ((0..3).map(|k| r[i * 3 + k] * r[j * 3 + k]).sum::<f64>()
                    - if i == j { 1. } else { 0. })
                .abs()
                    < 1e-6
            })
        });
        let det = r[0] * (r[4] * r[8] - r[5] * r[7]) - r[1] * (r[3] * r[8] - r[5] * r[6])
            + r[2] * (r[3] * r[7] - r[4] * r[6]);
        check(
            orthogonal && (det - 1.).abs() < 1e-6,
            ErrorClass::Geometry,
            "TRANSFORM_NOT_PROPER_ROTATION",
        )
    }
    pub fn transform_point(&self, point_m: [f64; 3]) -> Result<[f64; 3]> {
        self.validate()?;
        check(
            point_m.iter().all(|v| v.is_finite()),
            ErrorClass::Geometry,
            "POINT_NONFINITE",
        )?;
        let mut out = self.translation_m;
        for (row, value) in out.iter_mut().enumerate() {
            *value += (0..3)
                .map(|k| self.rotation_row_major[row * 3 + k] * point_m[k])
                .sum::<f64>();
        }
        check(
            out.iter().all(|v| v.is_finite()),
            ErrorClass::Geometry,
            "POINT_OVERFLOW",
        )?;
        Ok(out)
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    fn transform() -> RigidTransform {
        RigidTransform {
            source_frame: "depth".into(),
            target_frame: "color".into(),
            rotation_row_major: [0., -1., 0., 1., 0., 0., 0., 0., 1.],
            translation_m: [0.1, 0.2, 0.3],
        }
    }
    #[test]
    fn direction_is_child_to_parent_in_metres() {
        let t = transform();
        let p = t.transform_point([1., 0., 0.]).unwrap();
        assert_eq!(p, [0.1, 1.2, 0.3]);
    }
    #[test]
    fn rejects_reflection_shear_nonfinite() {
        let mut t = transform();
        t.rotation_row_major = [-1., 0., 0., 0., 1., 0., 0., 0., 1.];
        assert!(t.validate().is_err());
        t.rotation_row_major = [1., 0.1, 0., 0., 1., 0., 0., 0., 1.];
        assert!(t.validate().is_err());
        t = transform();
        t.translation_m[1] = f64::NAN;
        assert!(t.validate().is_err());
    }
}
