use crate::{CameraModel, GeometryError, Ray, RayTable, camera::pixel_count};
use std::{fmt, mem::size_of};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WarpError {
    Geometry(GeometryError),
    InvalidScale,
    InvalidStride,
    PayloadLength,
    InvalidTransform,
    RayDimensions,
    MemoryLimit,
    AllocationFailed,
    OutputOverflow,
    OutputLength,
}
impl fmt::Display for WarpError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for WarpError {}
impl From<GeometryError> for WarpError {
    fn from(e: GeometryError) -> Self {
        Self::Geometry(e)
    }
}
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct DepthScale(f64);
impl DepthScale {
    pub fn millimeters_per_count(value: f64) -> Result<Self, WarpError> {
        if !value.is_finite()
            || value <= 0.0
            || !(value * 65535.0).is_finite()
            || value / 1000.0 == 0.0
        {
            return Err(WarpError::InvalidScale);
        }
        Ok(Self(value))
    }
    pub fn value(self) -> f64 {
        self.0
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Endian {
    Little,
    Big,
}
#[derive(Debug, Clone, Copy)]
pub struct RawDepthView<'a> {
    bytes: &'a [u8],
    width: u32,
    height: u32,
    stride: usize,
    endian: Endian,
    scale: DepthScale,
}
impl<'a> RawDepthView<'a> {
    pub fn new(
        bytes: &'a [u8],
        width: u32,
        height: u32,
        stride: usize,
        endian: Endian,
        scale: DepthScale,
    ) -> Result<Self, WarpError> {
        pixel_count(width, height)?;
        let row = (width as usize)
            .checked_mul(2)
            .ok_or(GeometryError::ArithmeticOverflow)?;
        if stride < row {
            return Err(WarpError::InvalidStride);
        }
        let len = stride
            .checked_mul(height as usize)
            .ok_or(GeometryError::ArithmeticOverflow)?;
        if bytes.len() != len {
            return Err(WarpError::PayloadLength);
        }
        Ok(Self {
            bytes,
            width,
            height,
            stride,
            endian,
            scale,
        })
    }
    pub fn dimensions(self) -> (u32, u32) {
        (self.width, self.height)
    }
    fn sample(self, index: usize) -> u16 {
        let offset =
            (index / self.width as usize) * self.stride + (index % self.width as usize) * 2;
        let pair = [self.bytes[offset], self.bytes[offset + 1]];
        match self.endian {
            Endian::Little => u16::from_le_bytes(pair),
            Endian::Big => u16::from_be_bytes(pair),
        }
    }
}
/// p_color (metres) = row-major R * p_depth (metres) + t (metres).
#[derive(Debug, Clone, Copy)]
pub struct DepthToColorTransform {
    rotation: [f64; 9],
    translation_m: [f64; 3],
}
impl DepthToColorTransform {
    pub fn new(rotation: [f64; 9], translation_m: [f64; 3]) -> Result<Self, WarpError> {
        if rotation
            .iter()
            .chain(translation_m.iter())
            .any(|v| !v.is_finite())
        {
            return Err(WarpError::InvalidTransform);
        }
        for a in 0..3 {
            for b in 0..3 {
                let dot = (0..3)
                    .map(|k| rotation[a * 3 + k] * rotation[b * 3 + k])
                    .sum::<f64>();
                if (dot - if a == b { 1.0 } else { 0.0 }).abs() > 1e-6 {
                    return Err(WarpError::InvalidTransform);
                }
            }
        }
        let r = rotation;
        let det = r[0] * (r[4] * r[8] - r[5] * r[7]) - r[1] * (r[3] * r[8] - r[5] * r[6])
            + r[2] * (r[3] * r[7] - r[4] * r[6]);
        if (det - 1.0).abs() > 1e-6 {
            return Err(WarpError::InvalidTransform);
        }
        Ok(Self {
            rotation,
            translation_m,
        })
    }
    fn apply(self, p: [f64; 3]) -> [f64; 3] {
        std::array::from_fn(|i| {
            self.translation_m[i] + (0..3).map(|j| self.rotation[i * 3 + j] * p[j]).sum::<f64>()
        })
    }
}
#[derive(Debug, Default, Clone, Copy, PartialEq, Eq)]
pub struct WarpStats {
    pub source_pixels: usize,
    pub zero_depth: usize,
    pub invalid_ray: usize,
    pub nonfinite: usize,
    pub behind: usize,
    pub projection_failure: usize,
    pub out_of_bounds: usize,
    pub candidates: usize,
    pub collisions: usize,
    pub replaced: usize,
    pub occluded: usize,
    pub equal_z_ties: usize,
    pub valid_output: usize,
    pub clamped_nonzero: usize,
}
#[derive(Debug, Clone, Copy)]
pub struct WarpBufferLimits {
    pub max_pixels: usize,
    pub max_bytes: usize,
}
impl Default for WarpBufferLimits {
    fn default() -> Self {
        Self {
            max_pixels: crate::MAX_PIXELS,
            max_bytes: 128 * 1024 * 1024,
        }
    }
}
#[derive(Debug)]
pub struct WarpOutputBuffer {
    limits: WarpBufferLimits,
    depth: Vec<u16>,
    valid: Vec<u8>,
    z: Vec<f64>,
    owner: Vec<usize>,
    dimensions: (u32, u32),
    stats: WarpStats,
    ready: bool,
}
#[derive(Debug, Clone, Copy)]
pub struct WarpOutput<'a> {
    pub dimensions: (u32, u32),
    pub depth_mm: &'a [u16],
    pub validity: &'a [u8],
    pub source_indices: &'a [usize],
    pub stats: WarpStats,
}
impl WarpOutput<'_> {
    pub fn write_packed_le(self, dst: &mut [u8]) -> Result<(), WarpError> {
        if dst.len()
            != self
                .depth_mm
                .len()
                .checked_mul(2)
                .ok_or(WarpError::OutputLength)?
        {
            return Err(WarpError::OutputLength);
        }
        for (v, out) in self.depth_mm.iter().zip(dst.chunks_exact_mut(2)) {
            out.copy_from_slice(&v.to_le_bytes())
        }
        Ok(())
    }
}
impl WarpOutputBuffer {
    pub fn new(limits: WarpBufferLimits) -> Result<Self, WarpError> {
        if limits.max_pixels == 0 || limits.max_bytes == 0 {
            return Err(WarpError::MemoryLimit);
        }
        Ok(Self {
            limits,
            depth: Vec::new(),
            valid: Vec::new(),
            z: Vec::new(),
            owner: Vec::new(),
            dimensions: (0, 0),
            stats: WarpStats::default(),
            ready: false,
        })
    }
    pub fn allocated_payload_bytes(&self) -> usize {
        self.depth.capacity() * size_of::<u16>()
            + self.valid.capacity()
            + self.z.capacity() * size_of::<f64>()
            + self.owner.capacity() * size_of::<usize>()
    }
    pub fn output(&self) -> Option<WarpOutput<'_>> {
        self.ready.then_some(WarpOutput {
            dimensions: self.dimensions,
            depth_mm: &self.depth,
            validity: &self.valid,
            source_indices: &self.owner,
            stats: self.stats,
        })
    }
    fn prepare(&mut self, dims: (u32, u32)) -> Result<(), WarpError> {
        self.ready = false;
        let count = pixel_count(dims.0, dims.1)?;
        let bytes = count
            .checked_mul(2 + 1 + 8 + size_of::<usize>())
            .ok_or(WarpError::MemoryLimit)?;
        if count > self.limits.max_pixels
            || bytes > self.limits.max_bytes
            || bytes > isize::MAX as usize
        {
            return Err(WarpError::MemoryLimit);
        }
        if [
            self.depth.capacity(),
            self.valid.capacity(),
            self.z.capacity(),
            self.owner.capacity(),
        ]
        .iter()
        .any(|c| *c < count)
        {
            self.depth = Vec::new();
            self.valid = Vec::new();
            self.z = Vec::new();
            self.owner = Vec::new();
            self.depth
                .try_reserve_exact(count)
                .map_err(|_| WarpError::AllocationFailed)?;
            self.valid
                .try_reserve_exact(count)
                .map_err(|_| WarpError::AllocationFailed)?;
            self.z
                .try_reserve_exact(count)
                .map_err(|_| WarpError::AllocationFailed)?;
            self.owner
                .try_reserve_exact(count)
                .map_err(|_| WarpError::AllocationFailed)?;
            if self.allocated_payload_bytes() > self.limits.max_bytes {
                self.depth = Vec::new();
                self.valid = Vec::new();
                self.z = Vec::new();
                self.owner = Vec::new();
                return Err(WarpError::MemoryLimit);
            }
        }
        self.depth.resize(count, 0);
        self.depth.fill(0);
        self.valid.resize(count, 0);
        self.valid.fill(0);
        self.z.resize(count, f64::INFINITY);
        self.z.fill(f64::INFINITY);
        self.owner.resize(count, usize::MAX);
        self.owner.fill(usize::MAX);
        self.dimensions = dims;
        self.stats = WarpStats::default();
        Ok(())
    }
}
/// Forward splat with no interpolation or hole filling. Overflow of any winning
/// in-bounds color-Z rejects the entire output; `buffer.output()` then returns None.
pub fn warp_depth_to_color<'a>(
    raw: RawDepthView<'_>,
    rays: &RayTable,
    color: &CameraModel,
    transform: DepthToColorTransform,
    buffer: &'a mut WarpOutputBuffer,
) -> Result<WarpOutput<'a>, WarpError> {
    buffer.ready = false;
    if raw.dimensions() != rays.dimensions() {
        return Err(WarpError::RayDimensions);
    }
    buffer.prepare(color.dimensions())?;
    let stats = &mut buffer.stats;
    stats.source_pixels = rays.rays().len();
    let (width, height) = color.dimensions();
    for (index, ray) in rays.rays().iter().enumerate() {
        let value = raw.sample(index);
        if value == 0 {
            stats.zero_depth += 1;
            continue;
        }
        let Ray::Valid { x, y } = *ray else {
            stats.invalid_ray += 1;
            continue;
        };
        let z = value as f64 * raw.scale.0 / 1000.0;
        let p = transform.apply([x * z, y * z, z]);
        if p.iter().any(|v| !v.is_finite()) {
            stats.nonfinite += 1;
            continue;
        }
        if p[2] <= 0.0 {
            stats.behind += 1;
            continue;
        }
        let Ok(uv) = color.project_normalized([p[0] / p[2], p[1] / p[2]]) else {
            stats.projection_failure += 1;
            continue;
        };
        let u = (uv[0] + 0.5).floor();
        let v = (uv[1] + 0.5).floor();
        if !u.is_finite() || !v.is_finite() {
            stats.projection_failure += 1;
            continue;
        }
        if u < 0.0 || v < 0.0 || u >= width as f64 || v >= height as f64 {
            stats.out_of_bounds += 1;
            continue;
        }
        let target = v as usize * width as usize + u as usize;
        stats.candidates += 1;
        let previous = buffer.owner[target];
        if previous != usize::MAX {
            stats.collisions += 1;
            if p[2] == buffer.z[target] {
                stats.equal_z_ties += 1
            }
        }
        if p[2] < buffer.z[target] || (p[2] == buffer.z[target] && index < previous) {
            if previous != usize::MAX {
                stats.replaced += 1
            }
            buffer.z[target] = p[2];
            buffer.owner[target] = index;
        } else {
            stats.occluded += 1
        }
    }
    // Preflight all winners before committing any output pixels.
    for (z, owner) in buffer.z.iter().zip(&buffer.owner) {
        if *owner != usize::MAX {
            let mm = (*z * 1000.0 + 0.5).floor();
            if !mm.is_finite() || mm > u16::MAX as f64 {
                return Err(WarpError::OutputOverflow);
            }
        }
    }
    for index in 0..buffer.depth.len() {
        if buffer.owner[index] != usize::MAX {
            let mm = (buffer.z[index] * 1000.0 + 0.5).floor();
            let mm = if mm < 1.0 {
                stats.clamped_nonzero += 1;
                1
            } else {
                mm as u16
            };
            buffer.depth[index] = mm;
            buffer.valid[index] = 1;
            stats.valid_output += 1
        }
    }
    buffer.ready = true;
    Ok(WarpOutput {
        dimensions: buffer.dimensions,
        depth_mm: &buffer.depth,
        validity: &buffer.valid,
        source_indices: &buffer.owner,
        stats: buffer.stats,
    })
}
