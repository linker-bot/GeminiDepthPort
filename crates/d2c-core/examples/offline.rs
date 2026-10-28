//! Entirely artificial fixture; no SDK output or physical calibration.
use d2c_core::*;
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let intrinsics = CameraIntrinsics {
        width: 2,
        height: 1,
        fx: 1.0,
        fy: 1.0,
        cx: 0.0,
        cy: 0.0,
        skew: 0.0,
    };
    let depth_model = CameraModel::new(intrinsics, DistortionModel::PinholeZero)?;
    let color_model = CameraModel::new(intrinsics, DistortionModel::PinholeZero)?;
    let mut cache = RayTableCache::new(RayTableCacheLimits::default())?;
    // The adapter supplies a stable digest of its complete calibration snapshot.
    let rays = cache.get_or_build(
        &depth_model,
        CalibrationId([0; 32]),
        InverseOptions::default(),
    )?;
    let transform = DepthToColorTransform::new([1., 0., 0., 0., 1., 0., 0., 0., 1.], [0., 0., 0.])?;
    let mut output = WarpOutputBuffer::new(WarpBufferLimits::default())?;
    let source = [0x10, 0x27, 0, 0]; // raw counts [10000, 0], 0.1 mm/count
    let raw = RawDepthView::new(
        &source,
        2,
        1,
        4,
        Endian::Little,
        DepthScale::millimeters_per_count(0.1)?,
    )?;
    let warped = warp_depth_to_color(raw, rays, &color_model, transform, &mut output)?;
    assert_eq!(warped.depth_mm, [1000, 0]);
    assert_eq!(warped.validity, [1, 0]);
    // Reuse `output` on the next frame, only after releasing this borrowed view.
    let mut packed = [0; 4];
    warped.write_packed_le(&mut packed)?;
    assert_eq!(packed, [0xe8, 0x03, 0, 0]);
    Ok(())
}
