use super::*;
use frame_core::{PairClock, PairEvidence};
use orbbec_safe::Extrinsics;

#[derive(Debug, Clone)]
pub struct AuxBinding {
    pub node_name: String,
    pub namespace: String,
    pub pair_metadata_topic: String,
}
#[derive(Debug, Clone)]
pub struct StaticTransform {
    /// TF parent=color optical, child=depth optical; stored transform maps child→parent.
    pub parent_frame: String,
    pub child_frame: String,
    pub rotation_row_major: [f64; 9],
    pub translation_m: [f64; 3],
    pub quaternion_xyzw: [f64; 4],
}
impl StaticTransform {
    pub fn from_depth_to_color(
        e: &Extrinsics,
        color_frame: &str,
        depth_frame: &str,
    ) -> Result<Self> {
        e.validate()?;
        ensure!(
            token(color_frame) && token(depth_frame) && color_frame != depth_frame,
            "distinct explicit optical frames required"
        );
        let r = e.rotation_row_major;
        let mut q = if r[0] + r[4] + r[8] > 0. {
            let s = (r[0] + r[4] + r[8] + 1.).sqrt() * 2.;
            [
                (r[7] - r[5]) / s,
                (r[2] - r[6]) / s,
                (r[3] - r[1]) / s,
                s / 4.,
            ]
        } else if r[0] > r[4] && r[0] > r[8] {
            let s = (1. + r[0] - r[4] - r[8]).sqrt() * 2.;
            [
                s / 4.,
                (r[1] + r[3]) / s,
                (r[2] + r[6]) / s,
                (r[7] - r[5]) / s,
            ]
        } else if r[4] > r[8] {
            let s = (1. + r[4] - r[0] - r[8]).sqrt() * 2.;
            [
                (r[1] + r[3]) / s,
                s / 4.,
                (r[5] + r[7]) / s,
                (r[2] - r[6]) / s,
            ]
        } else {
            let s = (1. + r[8] - r[0] - r[4]).sqrt() * 2.;
            [
                (r[2] + r[6]) / s,
                (r[5] + r[7]) / s,
                s / 4.,
                (r[3] - r[1]) / s,
            ]
        };
        let norm = q.iter().map(|x| x * x).sum::<f64>().sqrt();
        ensure!(norm.is_finite() && norm > 0., "invalid rotation quaternion");
        for x in &mut q {
            *x /= norm
        }
        Ok(Self {
            parent_frame: color_frame.into(),
            child_frame: depth_frame.into(),
            rotation_row_major: r,
            translation_m: e.translation_m,
            quaternion_xyzw: q,
        })
    }
}
#[repr(C)]
struct ConfigRaw {
    abi_version: u32,
    struct_size: u32,
    node_name: *const c_char,
    namespace_name: *const c_char,
    pair_metadata_topic: *const c_char,
    parent_frame: *const c_char,
    child_frame: *const c_char,
    stamp_sec: i32,
    stamp_nanosec: u32,
    rotation_row_major: [f64; 9],
    translation_m: [f64; 3],
}
#[repr(C)]
struct PairRaw {
    abi_version: u32,
    struct_size: u32,
    pair_sequence: u64,
    color_sequence: u64,
    depth_sequence: u64,
    color_stamp_ns: u64,
    depth_stamp_ns: u64,
    color_timestamp_us: u64,
    depth_timestamp_us: u64,
    delta_us: u64,
    clock: u32,
    same_frameset: u32,
    hardware_sync_verified: u32,
}
type Create = unsafe extern "C" fn(*const ConfigRaw, *mut *mut c_void) -> i32;
type Publish = unsafe extern "C" fn(*mut c_void, *const PairRaw) -> i32;
type Destroy = unsafe extern "C" fn(*mut c_void);
pub struct RosAuxPublisher {
    library: Rc<Library>,
    handle: NonNull<c_void>,
    publish: Publish,
    destroy: Destroy,
    last: Option<(PairEvidence, u64, u64)>,
    session_bound: bool,
    session_id: Option<String>,
}
unsafe fn symbol<T: Copy>(library: &Library, name: &'static [u8]) -> Result<T> {
    let raw = unsafe { libc::dlsym(library.handle.as_ptr(), name.as_ptr().cast()) };
    ensure!(!raw.is_null(), "missing auxiliary bridge symbol");
    ensure!(
        mem::size_of::<T>() == mem::size_of_val(&raw),
        "unsupported auxiliary ABI"
    );
    Ok(unsafe { mem::transmute_copy(&raw) })
}
fn stamp(ns: u64) -> Result<UnixStamp> {
    let s = UnixStamp {
        sec: i32::try_from(ns / 1_000_000_000)?,
        nanosec: (ns % 1_000_000_000) as u32,
    };
    s.validate()?;
    Ok(s)
}
impl RosAuxPublisher {
    pub fn load(
        path: impl AsRef<Path>,
        binding: AuxBinding,
        transform: StaticTransform,
        stamp_ns: u64,
    ) -> Result<Self> {
        ensure!(
            token(&binding.node_name) && absolute_name(&binding.namespace, true),
            "invalid auxiliary binding"
        );
        let prefix = if binding.namespace == "/" {
            "/".into()
        } else {
            format!("{}/", binding.namespace)
        };
        ensure!(
            absolute_name(&binding.pair_metadata_topic, false)
                && binding.pair_metadata_topic.starts_with(&prefix),
            "explicit pair topic inside namespace required"
        );
        let extrinsics = Extrinsics {
            rotation_row_major: transform.rotation_row_major,
            translation_m: transform.translation_m,
        };
        let checked = StaticTransform::from_depth_to_color(
            &extrinsics,
            &transform.parent_frame,
            &transform.child_frame,
        )?;
        let s = stamp(stamp_ns)?;
        let strings = [
            binding.node_name,
            binding.namespace,
            binding.pair_metadata_topic,
            checked.parent_frame,
            checked.child_frame,
        ]
        .into_iter()
        .map(CString::new)
        .collect::<std::result::Result<Vec<_>, _>>()?;
        let config = ConfigRaw {
            abi_version: 1,
            struct_size: u32::try_from(mem::size_of::<ConfigRaw>())?,
            node_name: strings[0].as_ptr(),
            namespace_name: strings[1].as_ptr(),
            pair_metadata_topic: strings[2].as_ptr(),
            parent_frame: strings[3].as_ptr(),
            child_frame: strings[4].as_ptr(),
            stamp_sec: s.sec,
            stamp_nanosec: s.nanosec,
            rotation_row_major: extrinsics.rotation_row_major,
            translation_m: extrinsics.translation_m,
        };
        let library = Library::load(path.as_ref())?;
        let version: unsafe extern "C" fn() -> u32 =
            unsafe { symbol(&library, b"gdp_ros_aux_abi_version\0")? };
        ensure!(unsafe { version() } == 1, "auxiliary ABI version differs");
        let create: Create = unsafe { symbol(&library, b"gdp_ros_aux_create\0")? };
        let publish = unsafe { symbol(&library, b"gdp_ros_aux_publish_pair\0")? };
        let destroy: Destroy = unsafe { symbol(&library, b"gdp_ros_aux_destroy\0")? };
        let mut raw = ptr::null_mut();
        let code = unsafe { create(&config, &mut raw) };
        if code != 0 {
            if !raw.is_null() {
                unsafe { destroy(raw) }
            }
            bail!("auxiliary publisher creation failed (code {code})")
        }
        Ok(Self {
            library,
            handle: NonNull::new(raw).context("null auxiliary publisher")?,
            publish,
            destroy,
            last: None,
            session_bound: false,
            session_id: None,
        })
    }
    /// Bind once before the first pair; session changes require a new publisher.
    pub fn set_session_id(&mut self, value: &str) -> Result<()> {
        ensure!(
            !self.session_bound && self.last.is_none(),
            "session already bound or publisher active"
        );
        unsafe {
            set_native_session(
                &self.library,
                self.handle,
                b"gdp_ros_aux_set_session_id\0",
                value,
            )?;
        }
        self.session_bound = true;
        self.session_id = Some(value.into());
        Ok(())
    }
    /// Separate additive topic `<pair_metadata_topic>/evidence`; legacy v1 is unchanged.
    /// Call only after both Image submissions and the matching legacy pair submission.
    pub fn publish_bound_evidence(
        &mut self,
        evidence: &frame_core::BoundPairEvidence,
        color_ns: u64,
        depth_ns: u64,
    ) -> Result<()> {
        evidence.validate()?;
        let (pair, cs, ds) = self.last.context("legacy pair submission required")?;
        ensure!(
            pair.pair_sequence == evidence.pair_sequence
                && cs == color_ns
                && ds == depth_ns
                && pair.color_sequence
                    == evidence
                        .color
                        .context
                        .sequence
                        .context("color sequence required")?
                && pair.depth_sequence
                    == evidence
                        .depth
                        .context
                        .sequence
                        .context("depth sequence required")?,
            "pair evidence differs from submitted images"
        );
        ensure!(
            evidence
                .color
                .context
                .system_timestamp_us
                .and_then(|s| s.checked_mul(1000))
                == Some(color_ns)
                && evidence
                    .depth
                    .context
                    .system_timestamp_us
                    .and_then(|s| s.checked_mul(1000))
                    == Some(depth_ns),
            "ROS source mapping differs"
        );
        ensure!(
            self.session_id.as_deref() == Some(evidence.color.context.session_id.as_str()),
            "pair session differs from publisher session"
        );
        ensure!(
            pair.color_timestamp_us == evidence.color_stamp_us
                && pair.depth_timestamp_us == evidence.depth_stamp_us
                && pair.delta_us == evidence.delta_us
                && evidence.clock_source
                    == match pair.clock {
                        PairClock::Device => "device_us",
                        PairClock::System => "sdk_system_us",
                    },
            "pair evidence clock differs from submitted pair"
        );
        let bytes = serde_json::to_vec(
            &serde_json::json!({"version":1,"pair":evidence,"color_stamp_ns":color_ns,"depth_stamp_ns":depth_ns,"image_submission":"submitted","consumer_receipt_verified":false}),
        )?;
        ensure!(bytes.len() <= 65536, "evidence size limit");
        type PublishEvidence = unsafe extern "C" fn(*mut c_void, *const u8, usize) -> i32;
        let publish: PublishEvidence =
            unsafe { symbol(&self.library, b"gdp_ros_aux_publish_evidence\0")? };
        let code = unsafe { publish(self.handle.as_ptr(), bytes.as_ptr(), bytes.len()) };
        ensure!(code == 0, "pair evidence submission failed (code {code})");
        Ok(())
    }
    pub fn publish_pair(
        &mut self,
        p: &PairEvidence,
        color_stamp_ns: u64,
        depth_stamp_ns: u64,
    ) -> Result<()> {
        stamp(color_stamp_ns)?;
        stamp(depth_stamp_ns)?;
        ensure!(
            p.same_frameset && !p.hardware_sync_verified,
            "explicit SDK frameset evidence required"
        );
        ensure!(
            p.color_timestamp_us > 0 && p.depth_timestamp_us > 0,
            "selected pairing clock timestamps must be positive"
        );
        ensure!(
            p.delta_us == p.color_timestamp_us.abs_diff(p.depth_timestamp_us),
            "pair delta mismatch"
        );
        if p.clock == PairClock::System {
            ensure!(
                p.color_timestamp_us.checked_mul(1000) == Some(color_stamp_ns)
                    && p.depth_timestamp_us.checked_mul(1000) == Some(depth_stamp_ns),
                "system pair stamps differ from actual Image stamps"
            )
        }
        if let Some((previous, cs, ds)) = self.last {
            ensure!(
                p.pair_sequence > previous.pair_sequence
                    && p.color_sequence > previous.color_sequence
                    && p.depth_sequence > previous.depth_sequence
                    && p.clock == previous.clock
                    && p.color_timestamp_us > previous.color_timestamp_us
                    && p.depth_timestamp_us > previous.depth_timestamp_us
                    && color_stamp_ns > cs
                    && depth_stamp_ns > ds,
                "pair clock changed or sequence/selected timestamp/stamp did not advance"
            )
        }
        let raw = PairRaw {
            abi_version: 1,
            struct_size: u32::try_from(mem::size_of::<PairRaw>())?,
            pair_sequence: p.pair_sequence,
            color_sequence: p.color_sequence,
            depth_sequence: p.depth_sequence,
            color_stamp_ns,
            depth_stamp_ns,
            color_timestamp_us: p.color_timestamp_us,
            depth_timestamp_us: p.depth_timestamp_us,
            delta_us: p.delta_us,
            clock: match p.clock {
                PairClock::Device => 1,
                PairClock::System => 2,
            },
            same_frameset: 1,
            hardware_sync_verified: 0,
        };
        let code = unsafe { (self.publish)(self.handle.as_ptr(), &raw) };
        ensure!(code == 0, "pair metadata publication failed (code {code})");
        self.last = Some((*p, color_stamp_ns, depth_stamp_ns));
        Ok(())
    }
}
impl Drop for RosAuxPublisher {
    fn drop(&mut self) {
        unsafe { (self.destroy)(self.handle.as_ptr()) };
        let _ = &self.library;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn tf_direction_quaternion_and_units_match_known_point() {
        let e = Extrinsics::from_sdk([0., -1., 0., 1., 0., 0., 0., 0., 1.], [100., 200., 300.])
            .unwrap();
        let t = StaticTransform::from_depth_to_color(&e, "color_optical", "depth_optical").unwrap();
        assert_eq!(t.parent_frame, "color_optical");
        assert_eq!(t.child_frame, "depth_optical");
        assert_eq!(t.translation_m, [0.1, 0.2, 0.3]);
        let z = t.quaternion_xyzw[2];
        let w = t.quaternion_xyzw[3];
        assert!((z - std::f64::consts::FRAC_1_SQRT_2).abs() < 1e-12);
        assert!((z - w).abs() < 1e-12);
        let point = [1., 2., 3.];
        let rotated = [
            (1. - 2. * z * z) * point[0] - 2. * z * w * point[1] + t.translation_m[0],
            2. * z * w * point[0] + (1. - 2. * z * z) * point[1] + t.translation_m[1],
            point[2] + t.translation_m[2],
        ];
        for (actual, expected) in rotated.into_iter().zip([-1.9, 1.2, 3.3]) {
            assert!((actual - expected).abs() < 1e-12)
        }
    }
}
