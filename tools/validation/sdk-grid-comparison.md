# Same-frame SDK alignment comparison

The retained SDK FrameSet supplies both original depth/color images. Source
calibration is read from those actual frames' profiles, not a requested profile
list. The capture tool verifies actual dimensions, FPS and format, records raw
SDK sequence/device/system times, and selects with the explicit configured
clock/skew policy. It does not require the two stream indices to be equal.
Source depth bytes and their positive SDK counts-to-mm scale are retained;
SDK Align receives that same live FrameSet. A byte comparison checks that the
filter did not mutate retained source depth.

`descriptor.color` retains the **original color profile**. `descriptor.target`
records the **actual SDK aligned depth output profile**, including its intrinsic
and distortion parameters. The scalar validation example projects onto target,
using depth-profile-to-aligned-profile extrinsics. The formal D2C kernel is
unchanged. Both result and descriptor explicitly mark
`target_grid: sdk-aligned-output-profile`; older descriptors without that marker
are rejected for this comparison rather than silently treated as equivalent.

This distinction matters in pinned SDK 2.10.6: the Align implementation defaults
to disabling target distortion and binds a zero-distortion output profile; it
also selects a gap-fill policy from the profiles' field-of-view relationship.
See [pinned Align source](https://github.com/orbbec/OrbbecSDK_v2/blob/575347c/src/filter/publicfilters/Align.cpp).
An original distorted color grid and this SDK output grid are different pixel
coordinate systems. Prior measurements comparing them remain historical
**different-grid algorithm observations**, and cannot establish same-grid
accuracy or equivalence. The new tool does not reinterpret old measurements.

The SDK stores rotation row-major and `source.getExtrinsicTo(target)` maps source
coordinates to target coordinates; its translation is millimetres. Capture uses
that direction and converts translation once to metres. The scalar source sample
is multiplied by its actual SDK depth scale before geometry. See the
[SDK maintainers' convention clarification](https://github.com/orbbec/OrbbecSDK_v2/issues/128).
No transpose or guessed unit correction is introduced based on measured errors.

Even on the same recorded grid, algorithms can differ. The scalar kernel uses
nearest-pixel splats, color-camera Z and an explicit z-buffer without gap fill.
SDK Align rasterization, fill and Z semantics are not assumed equivalent.
Reports count common valid, one-sided valid and exact-equal samples, and absolute
millimetre differences on common-valid samples. They do not infer physical error
from pixelwise algorithm differences, alter thresholds, certify bitwise equality,
or prove business-consumer compatibility. SDK mm conversion overflow marks the
comparison incomplete/nonzero rather than presenting partial metrics as a
successful complete comparison.

A scalar failure, source digest change, missing explicit target grid, invalid
scale/layout, malformed JSON or incomplete capture fails the tool. Scalar output
is a new private mode-0600 file. `physical_accuracy_verified` and
`hardware_sync_verified` remain false.

Synthetic validation (no SDK/device required):

```sh
cargo build -p d2c-core --example descriptor_warp
python3 -m unittest discover -s tests -p 'test_sdk_grid_comparison.py'
```

The synthetic original color has deliberately different distortion from the
SDK-style target, proving that the actual example consumes the latter. Other
tests cover old-grid refusal, unit overflow, failed child reports and source
mutation. Real capture/compilation belongs to the separately authorized SDK
validation environment; synthetic results are not hardware certification.
