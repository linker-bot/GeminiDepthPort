# d2c-core

Dependency-free deterministic scalar camera geometry. There is no ROS, SDK,
Docker, MCAP, device, thread, clock, network or recording integration here.
Calibration snapshots are converted by the caller into these geometry types;
this crate does not depend on a capture-contract crate or invent calibration.

## Models and depth ray tables (algorithm v1)

`CameraModel::new(CameraIntrinsics, DistortionModel)` validates positive finite
focal lengths, finite principal point/skew/coefficients, and checked image limits.
Integer pixel coordinates identify pixel centers. The intrinsic matrix is
`[[fx,skew,cx],[0,fy,cy],[0,0,1]]`.

Supported forward distortion models are `PinholeZero`, Brown-Conrady
`[k1,k2,p1,p2,k3]`, and rational polynomial
`[k1,k2,p1,p2,k3,k4,k5,k6]`. Rational radial gain divides by
`1+k4*r²+k5*r⁴+k6*r⁶`. `Equidistant4` is explicitly rejected with
`UnsupportedDistortionModel` in v1, including a zero-coefficient equidistant
input; it is never silently treated as pinhole.

Depth ray construction first inverts K including skew, then uses bounded damped
Newton iterations with an analytic distortion Jacobian. `InverseOptions` controls
the iteration bound and normalized-coordinate residual tolerance. Each iteration
uses at most 12 deterministic half-step line-search attempts. Singular rational
denominators/Jacobians, nonfinite arithmetic and nonconvergence produce an
explicit `Ray::Invalid(RayFailure)` at that pixel, not a guessed pinhole ray.
An inverse root meeting the residual criterion is a local numerical solution;
this is not a certificate that an arbitrary folding distortion map is globally
invertible. Valid rays are `[x,y,1]`, not Euclidean-normalized directions: raw
depth is optical source-camera Z, not range along a unit vector.

```rust
use d2c_core::*;
let depth_model = CameraModel::new(CameraIntrinsics {
    width: 2, height: 2, fx: 200., fy: 200., cx: 0.5, cy: 0.5, skew: 0.,
}, DistortionModel::PinholeZero)?;
// Obtain this opaque identity from the validated calibration snapshot adapter.
let calibration = CalibrationId([7; 32]);
let mut cache = RayTableCache::new(RayTableCacheLimits::default())?;
let rays = cache.get_or_build(&depth_model, calibration, InverseOptions::default())?;
assert_eq!(rays.dimensions(), (2, 2));
assert_eq!(rays.stats().valid, 4);
# Ok::<(), GeometryError>(())
```

The exact cache key binds all intrinsic/distortion f64 bits, model kind, image
size, the caller-supplied 32-byte calibration identity, inverse policy and
`RAY_ALGORITHM_VERSION`. A reused ID alone cannot alias a different camera model.
The cache is LRU with explicit entry and ray-payload byte limits; it evicts before
building a replacement. Tables are borrowed from the cache, not returned as
externally retainable `Arc`s. Thus external references cannot secretly retain
evicted payloads. Ray allocation uses checked sizes and fallible reservation.
Header/allocator bookkeeping is not a process-RSS guarantee. Hard image bounds
are 16,384 per dimension and 16,777,216 total pixels; smaller caller limits can
be imposed independently.

Tests are artificial geometry fixtures, not SDK-derived goldens or physical
camera validation. `cargo test -p d2c-core` requires no device or external runtime.

## Offline warp (PR8)

`examples/offline.rs` is a complete dependency-free adapter entry point; run it
with `cargo run -p d2c-core --example offline --offline`. It uses artificial
calibration and samples, and performs no SDK comparison.

`RawDepthView::new` requires the exact `stride * height` bytes, including row
padding. Endianness and millimetres per raw count are explicit; SDK 0.1 mm counts
must use `DepthScale::millimeters_per_count(0.1)`, while already-normalized
millimetres use `1.0`. No SDK property or format inference occurs. Zero counts are
invalid and never become valid points through translation.

`DepthToColorTransform::new` accepts row-major rotation and metre translation,
checking finite values, orthonormal rows and determinant +1 within 1e-6. The
caller must bind the depth-to-color direction using its calibration snapshot;
a mathematically valid inverse rotation cannot be detected without that binding.
Coordinates obey `p_color = R * p_depth + t`, with source optical Z, not range.
Color forward distortion and skew are applied before nearest-pixel projection
`floor(u + 0.5)`. Signed bounds are checked before integer conversion. There is
no interpolation, hole filling or color-image resampling.

The z-buffer compares unquantized color optical Z in metres. Exact equal-Z ties
select the lower row-major source index. Only final in-bounds winners are
quantized to u16 millimetres, using half-up rounding. Nonzero values rounding to
zero become 1 mm and increment `clamped_nonzero`; a winning value exceeding
65535 after rounding rejects the entire frame. Far points that are behind,
outside, or occluded do not cause an output overflow. Invalid output has depth 0,
validity 0 and source index `usize::MAX`.

`WarpOutputBuffer` retains bounded owned storage for depth, validity, unquantized
Z and source ownership. Output borrows prevent mutation while a consumer holds a
frame. Growth releases old arrays before reserving new arrays; limits cover
allocated vector payload capacities, not allocator overhead or process RSS.
Every call invalidates the previous readiness state before validation, and all
winner depths are checked before output pixels are committed. On every error,
`output()` returns `None`, preventing partial or stale success. Allocation uses
fallible reservations. `write_packed_le` writes an exact-size caller buffer.

## Reference-validation boundary

The deterministic CPU tests cover analytical identity, translation, rotation,
optical color Z, prequantization occlusion, exact ties, distortion, failed inverse
rays, dimensions, row padding, endianness, quantization, overflow and signed pixel
edges. These fixtures are independent of an SDK and establish the stated scalar
algorithm only. They do not establish physical calibration correctness.

An external SDK comparison adapter must supply the *same immutable calibration
snapshot*, source frame bytes, depth scale, coordinate convention and transform
direction to this API and the external SDK. Record SDK version, calibration and
input hashes, units, projection mode, and SDK interpolation/hole-filling policy
alongside results. Compare valid-mask overlap and geometric depth/projection
residuals under a predeclared tolerance; SDK outputs are not pixel-exact goldens
for this nearest-splat implementation. No external SDK comparison has been run
by this crate. Equidistant and unsupported calibration models must fail before
comparison rather than silently substituting pinhole geometry.
