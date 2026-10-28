//! Validation adapter for sdk_align_capture.cpp; geometry library stays SDK-free.
use d2c_core::*;
use serde_json::{Value, json};
use std::{
    fs,
    io::{Read, Write},
    path::Path,
};
fn number(v: &Value, key: &str) -> Result<f64, Box<dyn std::error::Error>> {
    v[key]
        .as_f64()
        .filter(|x| x.is_finite())
        .ok_or_else(|| "DESCRIPTOR_NUMBER".into())
}
fn array<const N: usize>(v: &Value) -> Result<[f64; N], Box<dyn std::error::Error>> {
    let a = v.as_array().ok_or("DESCRIPTOR_ARRAY")?;
    let vals = a
        .iter()
        .map(|x| {
            x.as_f64()
                .filter(|x| x.is_finite())
                .ok_or("DESCRIPTOR_NUMBER")
        })
        .collect::<Result<Vec<_>, _>>()?;
    vals.try_into()
        .map_err(|_| "DESCRIPTOR_ARRAY_LENGTH".into())
}
fn model(v: &Value) -> Result<CameraModel, Box<dyn std::error::Error>> {
    let n = |key: &str| {
        v[key]
            .as_u64()
            .and_then(|x| u32::try_from(x).ok())
            .ok_or("DESCRIPTOR_SIZE")
    };
    let intrinsics = CameraIntrinsics {
        width: n("width")?,
        height: n("height")?,
        fx: number(v, "fx")?,
        fy: number(v, "fy")?,
        cx: number(v, "cx")?,
        cy: number(v, "cy")?,
        skew: number(v, "skew")?,
    };
    let distortion = match v["distortion_model"].as_str() {
        Some("none") => {
            if v["distortion_coefficients"]
                .as_array()
                .is_none_or(|x| !x.is_empty())
            {
                return Err("DESCRIPTOR_COEFFICIENTS".into());
            }
            DistortionModel::PinholeZero
        }
        Some("plumb_bob") => DistortionModel::BrownConrady5(array(&v["distortion_coefficients"])?),
        Some("rational_polynomial") => {
            DistortionModel::RationalPolynomial8(array(&v["distortion_coefficients"])?)
        }
        Some("equidistant") => DistortionModel::Equidistant4(array(&v["distortion_coefficients"])?),
        _ => return Err("DESCRIPTOR_MODEL".into()),
    };
    Ok(CameraModel::new(intrinsics, distortion)?)
}
fn run() -> Result<Value, Box<dyn std::error::Error>> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    if args.len() != 3 || args.iter().any(|p| !Path::new(p).is_absolute()) {
        return Err("ABSOLUTE_DESCRIPTOR_DEPTH_OUTPUT_REQUIRED".into());
    }
    let mut bytes = Vec::new();
    fs::File::open(&args[0])?
        .take(65537)
        .read_to_end(&mut bytes)?;
    if bytes.len() > 65536 {
        return Err("DESCRIPTOR_LIMIT".into());
    }
    let v = capture_contract::parse_strict(&bytes)?;
    if v["version"] != 1
        || v["association"] != "sdk_frameset"
        || v["hardware_sync_verified"] != false
    {
        return Err("DESCRIPTOR_PROVENANCE".into());
    }
    let depth = model(&v["depth"])?;
    if v["target_grid"] != "sdk-aligned-output-profile"
        || v["target_transform_source"] != "depth-profile-to-sdk-aligned-profile"
    {
        return Err("EXPLICIT_SDK_TARGET_GRID_REQUIRED".into());
    }
    // Original color calibration stays in descriptor.color. SDK Align may
    // output a zero-distortion grid; compare on its actual output profile.
    let color = model(&v["target"])?;
    let (w, h) = depth.dimensions();
    let stride = v["depth_stride"]
        .as_u64()
        .and_then(|x| usize::try_from(x).ok())
        .ok_or("DESCRIPTOR_STRIDE")?;
    let len = stride
        .checked_mul(h as usize)
        .filter(|x| *x <= 64 * 1024 * 1024)
        .ok_or("SOURCE_LIMIT")?;
    let mut source = Vec::new();
    fs::File::open(&args[1])?
        .take(len as u64 + 1)
        .read_to_end(&mut source)?;
    let endian = match v["depth_endian"].as_str() {
        Some("little") => Endian::Little,
        Some("big") => Endian::Big,
        _ => return Err("DESCRIPTOR_ENDIAN".into()),
    };
    let rays = RayTable::build(
        &depth,
        CalibrationId([0; 32]),
        InverseOptions::default(),
        RayTableLimits::default(),
    )?;
    let raw = RawDepthView::new(
        &source,
        w,
        h,
        stride,
        endian,
        DepthScale::millimeters_per_count(number(&v, "source_scale_mm")?)?,
    )?;
    let transform = DepthToColorTransform::new(
        array(&v["rotation_row_major"])?,
        array(&v["translation_m"])?,
    )?;
    let mut buffer = WarpOutputBuffer::new(WarpBufferLimits::default())?;
    let warped = warp_depth_to_color(raw, &rays, &color, transform, &mut buffer)?;
    let mut options = fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let mut output = options.open(&args[2])?;
    for pixel in warped.depth_mm {
        output.write_all(&pixel.to_le_bytes())?;
    }
    output.sync_all()?;
    Ok(
        json!({"version":1,"complete":true,"valid_output":warped.stats.valid_output,"source_pixels":warped.stats.source_pixels,"zero_depth":warped.stats.zero_depth,"out_of_bounds":warped.stats.out_of_bounds,"collisions":warped.stats.collisions,"occluded":warped.stats.occluded,"invalid_ray":warped.stats.invalid_ray,"output_semantics":"color-camera-Z-u16-millimetres","target_grid":"sdk-aligned-output-profile","physical_accuracy_verified":false}),
    )
}
fn main() {
    match run() {
        Ok(v) => println!("{v}"),
        Err(error) => {
            let code = if error.downcast_ref::<WarpError>() == Some(&WarpError::OutputOverflow) {
                "D2C_OUTPUT_OVERFLOW"
            } else {
                "REFERENCE_INPUT_OR_WARP_FAILED"
            };
            println!("{}", json!({"version":1,"complete":false,"code":code}));
            std::process::exit(1)
        }
    }
}
