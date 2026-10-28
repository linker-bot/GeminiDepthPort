use crate::{CameraModel, GeometryError, InverseOptions, RayFailure};
use std::{collections::VecDeque, mem::size_of};

/// Changes whenever distortion inversion, coordinate conventions or cache policy
/// affecting ray values changes. Calibration identities are supplied by adapters.
pub const RAY_ALGORITHM_VERSION: u32 = 1;
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct CalibrationId(pub [u8; 32]);
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub struct RayTableKey {
    calibration: CalibrationId,
    width: u32,
    height: u32,
    model_kind: u8,
    model_bits: [u64; 13],
    iterations: u32,
    tolerance_bits: u64,
    algorithm_version: u32,
}
impl RayTableKey {
    pub fn new(model: &CameraModel, calibration: CalibrationId, options: InverseOptions) -> Self {
        let (model_kind, model_bits) = model.key_bits();
        let (width, height) = model.dimensions();
        Self {
            calibration,
            width,
            height,
            model_kind,
            model_bits,
            iterations: options.max_iterations(),
            tolerance_bits: options.residual_tolerance().to_bits(),
            algorithm_version: RAY_ALGORITHM_VERSION,
        }
    }
    pub fn calibration_id(self) -> CalibrationId {
        self.calibration
    }
    pub fn algorithm_version(self) -> u32 {
        self.algorithm_version
    }
    pub fn dimensions(self) -> (u32, u32) {
        (self.width, self.height)
    }
}
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum Ray {
    Valid { x: f64, y: f64 },
    Invalid(RayFailure),
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RayTableLimits {
    pub max_pixels: usize,
    pub max_bytes: usize,
}
impl Default for RayTableLimits {
    fn default() -> Self {
        Self {
            max_pixels: crate::MAX_PIXELS,
            max_bytes: 128 * 1024 * 1024,
        }
    }
}
impl RayTableLimits {
    fn validate(self) -> Result<(), GeometryError> {
        if self.max_pixels == 0 || self.max_bytes < size_of::<Ray>() {
            Err(GeometryError::InvalidMemoryLimits)
        } else {
            Ok(())
        }
    }
}
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct RayTableStats {
    pub valid: usize,
    pub invalid: usize,
    pub nonfinite: usize,
    pub singular_denominator: usize,
    pub singular_jacobian: usize,
    pub nonconvergent: usize,
}
#[derive(Debug)]
pub struct RayTable {
    key: RayTableKey,
    rays: Vec<Ray>,
    stats: RayTableStats,
}
impl RayTable {
    pub fn build(
        model: &CameraModel,
        calibration: CalibrationId,
        options: InverseOptions,
        limits: RayTableLimits,
    ) -> Result<Self, GeometryError> {
        limits.validate()?;
        let pixels = model.pixel_count()?;
        if pixels > limits.max_pixels {
            return Err(GeometryError::PixelLimit);
        }
        let bytes = pixels
            .checked_mul(size_of::<Ray>())
            .ok_or(GeometryError::ArithmeticOverflow)?;
        if bytes > limits.max_bytes || bytes > isize::MAX as usize {
            return Err(GeometryError::MemoryLimit);
        }
        let mut rays = Vec::new();
        rays.try_reserve_exact(pixels)
            .map_err(|_| GeometryError::AllocationFailed)?;
        let mut stats = RayTableStats::default();
        let (width, height) = model.dimensions();
        for y in 0..height {
            for x in 0..width {
                match model.unproject_pixel([f64::from(x), f64::from(y)], options) {
                    Ok([x, y]) => {
                        stats.valid += 1;
                        rays.push(Ray::Valid { x, y });
                    }
                    Err(error) => {
                        stats.invalid += 1;
                        match error {
                            RayFailure::NonFinite => stats.nonfinite += 1,
                            RayFailure::SingularDenominator => stats.singular_denominator += 1,
                            RayFailure::SingularJacobian => stats.singular_jacobian += 1,
                            RayFailure::NonConvergent => stats.nonconvergent += 1,
                        };
                        rays.push(Ray::Invalid(error));
                    }
                }
            }
        }
        let allocated = rays
            .capacity()
            .checked_mul(size_of::<Ray>())
            .ok_or(GeometryError::ArithmeticOverflow)?;
        if allocated > limits.max_bytes {
            return Err(GeometryError::MemoryLimit);
        }
        Ok(Self {
            key: RayTableKey::new(model, calibration, options),
            rays,
            stats,
        })
    }
    pub fn key(&self) -> RayTableKey {
        self.key
    }
    pub fn dimensions(&self) -> (u32, u32) {
        self.key.dimensions()
    }
    pub fn rays(&self) -> &[Ray] {
        &self.rays
    }
    pub fn stats(&self) -> RayTableStats {
        self.stats
    }
    pub fn allocated_payload_bytes(&self) -> usize {
        self.rays.capacity() * size_of::<Ray>()
    }
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RayTableCacheLimits {
    pub max_entries: usize,
    pub max_bytes: usize,
    pub max_table_pixels: usize,
}
impl Default for RayTableCacheLimits {
    fn default() -> Self {
        Self {
            max_entries: 2,
            max_bytes: 64 * 1024 * 1024,
            max_table_pixels: crate::MAX_PIXELS,
        }
    }
}
/// LRU references are borrowed, not shared/cloned: retained external Arc handles
/// cannot escape the cache's payload budget. Keys/entry overhead is also bounded
/// by max_entries; allocator bookkeeping is not a process-RSS guarantee.
#[derive(Debug)]
pub struct RayTableCache {
    limits: RayTableCacheLimits,
    tables: VecDeque<RayTable>,
    hits: u64,
    builds: u64,
}
impl RayTableCache {
    pub fn new(limits: RayTableCacheLimits) -> Result<Self, GeometryError> {
        if limits.max_entries == 0
            || limits.max_entries > 64
            || limits.max_bytes < size_of::<Ray>()
            || limits.max_table_pixels == 0
        {
            return Err(GeometryError::InvalidMemoryLimits);
        }
        Ok(Self {
            limits,
            tables: VecDeque::new(),
            hits: 0,
            builds: 0,
        })
    }
    pub fn len(&self) -> usize {
        self.tables.len()
    }
    pub fn is_empty(&self) -> bool {
        self.tables.is_empty()
    }
    pub fn allocated_payload_bytes(&self) -> usize {
        self.tables
            .iter()
            .map(RayTable::allocated_payload_bytes)
            .sum()
    }
    pub fn hits(&self) -> u64 {
        self.hits
    }
    pub fn builds(&self) -> u64 {
        self.builds
    }
    pub fn get_or_build(
        &mut self,
        model: &CameraModel,
        calibration: CalibrationId,
        options: InverseOptions,
    ) -> Result<&RayTable, GeometryError> {
        let key = RayTableKey::new(model, calibration, options);
        if let Some(index) = self.tables.iter().position(|t| t.key == key) {
            let table = self
                .tables
                .remove(index)
                .ok_or(GeometryError::ArithmeticOverflow)?;
            self.tables.push_front(table);
            self.hits = self.hits.saturating_add(1);
            return self.tables.front().ok_or(GeometryError::ArithmeticOverflow);
        }
        let pixels = model.pixel_count()?;
        if pixels > self.limits.max_table_pixels {
            return Err(GeometryError::PixelLimit);
        }
        let expected = pixels
            .checked_mul(size_of::<Ray>())
            .ok_or(GeometryError::ArithmeticOverflow)?;
        if expected > self.limits.max_bytes {
            return Err(GeometryError::MemoryLimit);
        }
        // Evict BEFORE allocation to bound the live cache payload during builds.
        while self.tables.len() >= self.limits.max_entries
            || self
                .allocated_payload_bytes()
                .checked_add(expected)
                .ok_or(GeometryError::ArithmeticOverflow)?
                > self.limits.max_bytes
        {
            self.tables.pop_back();
        }
        let table = RayTable::build(
            model,
            calibration,
            options,
            RayTableLimits {
                max_pixels: self.limits.max_table_pixels,
                max_bytes: self.limits.max_bytes - self.allocated_payload_bytes(),
            },
        )?;
        self.tables
            .try_reserve(1)
            .map_err(|_| GeometryError::AllocationFailed)?;
        self.tables.push_front(table);
        self.builds = self.builds.saturating_add(1);
        self.tables.front().ok_or(GeometryError::ArithmeticOverflow)
    }
}
