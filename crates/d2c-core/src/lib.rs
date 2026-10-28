//! Deterministic scalar geometry for offline depth-to-color registration.
//!
//! This crate has no SDK, ROS, recording, transport, clock or serialization
//! dependencies. Pixel coordinates refer to pixel centers at integer positions.
mod camera;
mod rays;

pub use camera::{
    CameraIntrinsics, CameraModel, DistortionModel, GeometryError, InverseOptions, MAX_DIMENSION,
    MAX_PIXELS, ProjectionError, RayFailure,
};
pub use rays::{
    CalibrationId, RAY_ALGORITHM_VERSION, Ray, RayTable, RayTableCache, RayTableCacheLimits,
    RayTableKey, RayTableLimits, RayTableStats,
};
mod warp;
pub use warp::{
    DepthScale, DepthToColorTransform, Endian, RawDepthView, WarpBufferLimits, WarpError,
    WarpOutput, WarpOutputBuffer, WarpStats, warp_depth_to_color,
};
