use std::{error::Error, fmt};

pub const MAX_DIMENSION: u32 = 16_384;
pub const MAX_PIXELS: usize = 16_777_216;
const MIN_DENOMINATOR: f64 = 1e-15;
const MIN_JACOBIAN_DETERMINANT: f64 = 1e-15;
const LINE_SEARCH_STEPS: u32 = 12;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum GeometryError {
    InvalidDimensions,
    PixelLimit,
    ArithmeticOverflow,
    NonFiniteIntrinsics,
    NonPositiveFocalLength,
    NonFiniteDistortion,
    UnsupportedDistortionModel,
    InvalidInverseOptions,
    InvalidMemoryLimits,
    MemoryLimit,
    AllocationFailed,
}
impl fmt::Display for GeometryError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::InvalidDimensions => "camera dimensions are outside supported bounds",
            Self::PixelLimit => "camera pixel count exceeds the supported bound",
            Self::ArithmeticOverflow => "geometry size arithmetic overflow",
            Self::NonFiniteIntrinsics => "intrinsic parameters must be finite",
            Self::NonPositiveFocalLength => "focal lengths must be positive",
            Self::NonFiniteDistortion => "distortion coefficients must be finite",
            Self::UnsupportedDistortionModel => "distortion model is unsupported by algorithm v1",
            Self::InvalidInverseOptions => "invalid bounded inverse-distortion policy",
            Self::InvalidMemoryLimits => "invalid memory limits",
            Self::MemoryLimit => "geometry allocation exceeds the configured memory bound",
            Self::AllocationFailed => "geometry allocation failed",
        })
    }
}
impl Error for GeometryError {}

pub(crate) fn pixel_count(width: u32, height: u32) -> Result<usize, GeometryError> {
    if width == 0 || height == 0 || width > MAX_DIMENSION || height > MAX_DIMENSION {
        return Err(GeometryError::InvalidDimensions);
    }
    let count = usize::try_from(width)
        .map_err(|_| GeometryError::ArithmeticOverflow)?
        .checked_mul(usize::try_from(height).map_err(|_| GeometryError::ArithmeticOverflow)?)
        .ok_or(GeometryError::ArithmeticOverflow)?;
    if count > MAX_PIXELS {
        return Err(GeometryError::PixelLimit);
    }
    Ok(count)
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CameraIntrinsics {
    pub width: u32,
    pub height: u32,
    pub fx: f64,
    pub fy: f64,
    pub cx: f64,
    pub cy: f64,
    /// K[0,1], in pixels. Other off-diagonal/homogeneous K entries are fixed.
    pub skew: f64,
}
impl CameraIntrinsics {
    pub fn validate(self) -> Result<(), GeometryError> {
        pixel_count(self.width, self.height)?;
        if ![self.fx, self.fy, self.cx, self.cy, self.skew]
            .iter()
            .all(|v| v.is_finite())
        {
            return Err(GeometryError::NonFiniteIntrinsics);
        }
        if self.fx <= 0. || self.fy <= 0. {
            return Err(GeometryError::NonPositiveFocalLength);
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum DistortionModel {
    PinholeZero,
    /// [k1, k2, p1, p2, k3]. Forward distorted normalized coordinates.
    BrownConrady5([f64; 5]),
    /// [k1, k2, p1, p2, k3, k4, k5, k6]; denominator 1+k4*r²+k5*r⁴+k6*r⁶.
    RationalPolynomial8([f64; 8]),
    /// Representable at an adapter boundary, but explicitly rejected in v1.
    Equidistant4([f64; 4]),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProjectionError {
    NonFinite,
    SingularDenominator,
}
impl fmt::Display for ProjectionError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::NonFinite => "nonfinite projection",
            Self::SingularDenominator => "singular rational distortion denominator",
        })
    }
}
impl Error for ProjectionError {}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RayFailure {
    NonFinite,
    SingularDenominator,
    SingularJacobian,
    NonConvergent,
}
impl fmt::Display for RayFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::NonFinite => "nonfinite inverse distortion",
            Self::SingularDenominator => "singular inverse distortion denominator",
            Self::SingularJacobian => "singular inverse distortion Jacobian",
            Self::NonConvergent => "inverse distortion did not converge within its bound",
        })
    }
}
impl Error for RayFailure {}
impl From<ProjectionError> for RayFailure {
    fn from(value: ProjectionError) -> Self {
        match value {
            ProjectionError::NonFinite => Self::NonFinite,
            ProjectionError::SingularDenominator => Self::SingularDenominator,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct InverseOptions {
    max_iterations: u32,
    residual_tolerance: f64,
}
impl Default for InverseOptions {
    fn default() -> Self {
        Self {
            max_iterations: 32,
            residual_tolerance: 1e-12,
        }
    }
}
impl InverseOptions {
    pub fn new(max_iterations: u32, residual_tolerance: f64) -> Result<Self, GeometryError> {
        if !(1..=128).contains(&max_iterations)
            || !residual_tolerance.is_finite()
            || !(1e-15..=1e-3).contains(&residual_tolerance)
        {
            return Err(GeometryError::InvalidInverseOptions);
        }
        Ok(Self {
            max_iterations,
            residual_tolerance,
        })
    }
    pub fn max_iterations(self) -> u32 {
        self.max_iterations
    }
    pub fn residual_tolerance(self) -> f64 {
        self.residual_tolerance
    }
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct CameraModel {
    intrinsics: CameraIntrinsics,
    distortion: DistortionModel,
}
impl CameraModel {
    pub fn new(
        intrinsics: CameraIntrinsics,
        distortion: DistortionModel,
    ) -> Result<Self, GeometryError> {
        intrinsics.validate()?;
        let finite = match distortion {
            DistortionModel::PinholeZero => true,
            DistortionModel::BrownConrady5(v) => v.iter().all(|x| x.is_finite()),
            DistortionModel::RationalPolynomial8(v) => v.iter().all(|x| x.is_finite()),
            DistortionModel::Equidistant4(_) => {
                return Err(GeometryError::UnsupportedDistortionModel);
            }
        };
        if !finite {
            return Err(GeometryError::NonFiniteDistortion);
        }
        Ok(Self {
            intrinsics,
            distortion,
        })
    }
    pub fn intrinsics(self) -> CameraIntrinsics {
        self.intrinsics
    }
    pub fn distortion(self) -> DistortionModel {
        self.distortion
    }
    pub fn dimensions(self) -> (u32, u32) {
        (self.intrinsics.width, self.intrinsics.height)
    }
    pub fn pixel_count(self) -> Result<usize, GeometryError> {
        pixel_count(self.intrinsics.width, self.intrinsics.height)
    }
    fn coefficients(self) -> [f64; 8] {
        match self.distortion {
            DistortionModel::PinholeZero => [0.; 8],
            DistortionModel::BrownConrady5(v) => [v[0], v[1], v[2], v[3], v[4], 0., 0., 0.],
            DistortionModel::RationalPolynomial8(v) => v,
            DistortionModel::Equidistant4(_) => [0.; 8],
        }
    }
    /// Forward distortion and its analytic 2×2 Jacobian. No clamping or hole filling.
    fn forward(self, x: f64, y: f64) -> Result<([f64; 2], [f64; 4]), ProjectionError> {
        if !x.is_finite() || !y.is_finite() {
            return Err(ProjectionError::NonFinite);
        }
        let [k1, k2, p1, p2, k3, k4, k5, k6] = self.coefficients();
        if [k1, k2, p1, p2, k3, k4, k5, k6].iter().all(|v| *v == 0.) {
            return Ok(([x, y], [1., 0., 0., 1.]));
        }
        let r2 = x * x + y * y;
        let r4 = r2 * r2;
        let r6 = r4 * r2;
        let n = 1. + k1 * r2 + k2 * r4 + k3 * r6;
        let d = 1. + k4 * r2 + k5 * r4 + k6 * r6;
        if ![r2, r4, r6, n, d].iter().all(|v| v.is_finite()) {
            return Err(ProjectionError::NonFinite);
        }
        if d.abs() <= MIN_DENOMINATOR {
            return Err(ProjectionError::SingularDenominator);
        }
        let radial = n / d;
        let derivative = ((k1 + 2. * k2 * r2 + 3. * k3 * r4) * d
            - n * (k4 + 2. * k5 * r2 + 3. * k6 * r4))
            / (d * d);
        let dx = 2. * x * derivative;
        let dy = 2. * y * derivative;
        let point = [
            x * radial + 2. * p1 * x * y + p2 * (r2 + 2. * x * x),
            y * radial + p1 * (r2 + 2. * y * y) + 2. * p2 * x * y,
        ];
        let jac = [
            radial + x * dx + 2. * p1 * y + 6. * p2 * x,
            x * dy + 2. * p1 * x + 2. * p2 * y,
            y * dx + 2. * p1 * x + 2. * p2 * y,
            radial + y * dy + 6. * p1 * y + 2. * p2 * x,
        ];
        if !point.iter().chain(jac.iter()).all(|v| v.is_finite()) {
            return Err(ProjectionError::NonFinite);
        }
        Ok((point, jac))
    }
    pub fn distort_normalized(self, point: [f64; 2]) -> Result<[f64; 2], ProjectionError> {
        Ok(self.forward(point[0], point[1])?.0)
    }
    pub fn project_normalized(self, point: [f64; 2]) -> Result<[f64; 2], ProjectionError> {
        let [x, y] = self.distort_normalized(point)?;
        let k = self.intrinsics;
        let result = [k.fx * x + k.skew * y + k.cx, k.fy * y + k.cy];
        if !result.iter().all(|v| v.is_finite()) {
            return Err(ProjectionError::NonFinite);
        }
        Ok(result)
    }
    /// Invert K including skew, then bounded damped Newton in normalized space.
    pub fn unproject_pixel(
        self,
        pixel: [f64; 2],
        options: InverseOptions,
    ) -> Result<[f64; 2], RayFailure> {
        let k = self.intrinsics;
        let yd = (pixel[1] - k.cy) / k.fy;
        let xd = (pixel[0] - k.cx - k.skew * yd) / k.fx;
        if ![xd, yd].iter().all(|v| v.is_finite()) {
            return Err(RayFailure::NonFinite);
        }
        if matches!(self.distortion, DistortionModel::PinholeZero) {
            return Ok([xd, yd]);
        }
        let mut estimate = [xd, yd];
        for _ in 0..options.max_iterations {
            let (forward, j) = self.forward(estimate[0], estimate[1])?;
            let residual = [forward[0] - xd, forward[1] - yd];
            let norm = residual[0].abs().max(residual[1].abs());
            if norm <= options.residual_tolerance {
                return Ok(estimate);
            }
            let determinant = j[0] * j[3] - j[1] * j[2];
            if !determinant.is_finite() {
                return Err(RayFailure::NonFinite);
            }
            if determinant.abs() <= MIN_JACOBIAN_DETERMINANT {
                return Err(RayFailure::SingularJacobian);
            }
            let step = [
                (j[3] * residual[0] - j[1] * residual[1]) / determinant,
                (-j[2] * residual[0] + j[0] * residual[1]) / determinant,
            ];
            if !step.iter().all(|v| v.is_finite()) {
                return Err(RayFailure::NonFinite);
            }
            let mut damping = 1.;
            let mut next = None;
            for _ in 0..LINE_SEARCH_STEPS {
                let candidate = [
                    estimate[0] - step[0] * damping,
                    estimate[1] - step[1] * damping,
                ];
                if let Ok((projected, _)) = self.forward(candidate[0], candidate[1]) {
                    let error = (projected[0] - xd).abs().max((projected[1] - yd).abs());
                    if error < norm {
                        next = Some(candidate);
                        break;
                    }
                }
                damping *= 0.5;
            }
            estimate = next.ok_or(RayFailure::NonConvergent)?;
        }
        let final_point = self.distort_normalized(estimate)?;
        if (final_point[0] - xd).abs().max((final_point[1] - yd).abs())
            <= options.residual_tolerance
        {
            Ok(estimate)
        } else {
            Err(RayFailure::NonConvergent)
        }
    }
    pub(crate) fn key_bits(self) -> (u8, [u64; 13]) {
        let k = self.intrinsics;
        let d = self.coefficients();
        let mut values = [0_u64; 13];
        for (slot, value) in values
            .iter_mut()
            .zip([k.fx, k.fy, k.cx, k.cy, k.skew].iter().chain(d.iter()))
        {
            *slot = value.to_bits();
        }
        (
            match self.distortion {
                DistortionModel::PinholeZero => 0,
                DistortionModel::BrownConrady5(_) => 1,
                DistortionModel::RationalPolynomial8(_) => 2,
                DistortionModel::Equidistant4(_) => 3,
            },
            values,
        )
    }
}
