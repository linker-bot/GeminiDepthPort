//! Offline directory source; no acquisition, ROS, SDK, recording gate, or clock.
pub mod recording;
use capture_contract::{
    FrameContext,
    snapshot::{SnapshotBinding, SnapshotEnvelope},
};
use frame_core::BoundPairEvidence;
use serde::{Deserialize, Serialize};
use std::{
    fs,
    io::{self, Read},
    path::{Component, Path, PathBuf},
};

#[derive(Debug)]
pub enum OfflineError {
    Invalid(&'static str),
    Io(io::Error),
    Contract(capture_contract::ContractError),
    Json(serde_json::Error),
    Geometry(d2c_core::GeometryError),
    Warp(d2c_core::WarpError),
    Output(depth_output::OutputError),
    Worker,
}
impl std::fmt::Display for OfflineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.code())
    }
}
impl std::error::Error for OfflineError {}
impl OfflineError {
    /// Additive public diagnostic; the existing category code remains stable.
    pub fn detail_code(&self) -> &'static str {
        match self {
            Self::Warp(d2c_core::WarpError::OutputOverflow) => "D2C_OUTPUT_OVERFLOW",
            _ => self.code(),
        }
    }
    pub fn code(&self) -> &'static str {
        match self {
            Self::Invalid(c) => c,
            Self::Io(_) => "INPUT_OUTPUT_IO",
            Self::Contract(_) => "CONTRACT_INVALID",
            Self::Json(_) => "JSON_SCHEMA",
            Self::Geometry(_) => "GEOMETRY_INVALID",
            Self::Warp(_) => "WARP_FAILED",
            Self::Output(depth_output::OutputError::CommitUncertain { .. }) => {
                "OUTPUT_COMMIT_UNCERTAIN"
            }
            Self::Output(_) => "OUTPUT_FAILED",
            Self::Worker => "OUTPUT_WORKER_FAILED",
        }
    }
}
macro_rules! from {
    ($t:ty,$v:ident) => {
        impl From<$t> for OfflineError {
            fn from(e: $t) -> Self {
                Self::$v(e)
            }
        }
    };
}
from!(io::Error, Io);
from!(capture_contract::ContractError, Contract);
from!(serde_json::Error, Json);
from!(d2c_core::GeometryError, Geometry);
from!(d2c_core::WarpError, Warp);
from!(depth_output::OutputError, Output);
pub type Result<T> = std::result::Result<T, OfflineError>;
fn require(ok: bool, code: &'static str) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(OfflineError::Invalid(code))
    }
}
pub fn sha256(bytes: &[u8]) -> String {
    ring::digest::digest(&ring::digest::SHA256, bytes)
        .as_ref()
        .iter()
        .flat_map(|b| {
            const HEX: &[u8; 16] = b"0123456789abcdef";
            [
                HEX[(b >> 4) as usize] as char,
                HEX[(b & 15) as usize] as char,
            ]
        })
        .collect()
}
fn digest_valid(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}
fn parse<T: serde::de::DeserializeOwned>(bytes: &[u8]) -> Result<T> {
    Ok(serde_json::from_value(capture_contract::parse_strict(
        bytes,
    )?)?)
}
fn read_bounded(path: &Path, max: usize) -> Result<Vec<u8>> {
    require(fs::metadata(path)?.is_file(), "SOURCE_NOT_FILE")?;
    let mut file = fs::File::open(path)?;
    let metadata = file.metadata()?;
    require(metadata.is_file(), "SOURCE_NOT_FILE")?;
    require(metadata.len() <= max as u64, "SOURCE_SIZE_LIMIT")?;
    let len = metadata.len() as usize;
    let mut out = Vec::new();
    out.try_reserve_exact(len)
        .map_err(|_| OfflineError::Invalid("ALLOCATION_FAILED"))?;
    out.resize(len, 0);
    file.read_exact(&mut out)?;
    let mut extra = [0; 1];
    require(file.read(&mut extra)? == 0, "SOURCE_CHANGED_DURING_READ")?;
    Ok(out)
}
/// Paths must remain under the canonical source directory; symlink escapes fail.
fn member(root: &Path, name: &str) -> Result<PathBuf> {
    let path = Path::new(name);
    require(
        !path.as_os_str().is_empty()
            && path.components().all(|c| matches!(c, Component::Normal(_))),
        "SOURCE_PATH",
    )?;
    let full = fs::canonicalize(root.join(path))?;
    require(full.starts_with(root), "SOURCE_PATH_ESCAPE")?;
    Ok(full)
}
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum StorageEndian {
    Little,
    Big,
}
/// Stored bytes have exactly the output representation in snapshot and pair
/// evidence. SDK counts require an explicit positive millimetres-per-count scale.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PairInput {
    pub pair_sequence: u64,
    pub evidence: String,
    pub evidence_sha256: String,
    pub depth: String,
    pub depth_sha256: String,
    pub endian: StorageEndian,
    pub storage_representation: capture_contract::DepthRepresentation,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DirectoryManifest {
    pub version: u32,
    pub source_kind: String,
    pub snapshot: String,
    pub binding: SnapshotBinding,
    pub pairs: Vec<PairInput>,
}
pub struct SourceFrame {
    pub evidence: BoundPairEvidence,
    pub evidence_sha256: String,
    pub evidence_json: Vec<u8>,
    pub depth: Vec<u8>,
    pub endian: StorageEndian,
}
/// Providers return owned pixels plus exact evidence bytes, in strictly advancing
/// source order. `DirectoryFrameSource` verifies file digests and progression.
/// Consumers recheck snapshot and evidence bindings independently.
pub trait FrameSource {
    fn snapshot(&self) -> &SnapshotEnvelope;
    fn next_frame(&mut self) -> Result<Option<SourceFrame>>;
}
pub struct DirectoryFrameSource {
    root: PathBuf,
    manifest: DirectoryManifest,
    snapshot: SnapshotEnvelope,
    next: usize,
    selected: Option<u64>,
    previous: Option<BoundPairEvidence>,
}
impl DirectoryFrameSource {
    pub fn open(root: &Path, selected: Option<u64>) -> Result<Self> {
        let root = fs::canonicalize(root)?;
        require(root.is_dir(), "SOURCE_NOT_DIRECTORY")?;
        let manifest: DirectoryManifest = parse(&read_bounded(
            &member(&root, "manifest.json")?,
            4 * 1024 * 1024,
        )?)?;
        require(
            manifest.version == 1 && manifest.source_kind == "bound-pair-directory-v1",
            "SOURCE_VERSION",
        )?;
        require(
            !manifest.pairs.is_empty() && manifest.pairs.len() <= 8192,
            "SOURCE_PAIR_LIMIT",
        )?;
        let mut last = 0;
        for p in &manifest.pairs {
            require(
                p.pair_sequence > last
                    && digest_valid(&p.depth_sha256)
                    && digest_valid(&p.evidence_sha256),
                "SOURCE_PAIR_MANIFEST",
            )?;
            last = p.pair_sequence;
        }
        if let Some(s) = selected {
            require(
                manifest.pairs.iter().any(|p| p.pair_sequence == s),
                "SELECTED_PAIR_MISSING",
            )?
        }
        let snapshot = SnapshotEnvelope::parse(
            &read_bounded(&member(&root, &manifest.snapshot)?, 4 * 1024 * 1024)?,
            &manifest.binding,
        )?;
        Ok(Self {
            root,
            manifest,
            snapshot,
            next: 0,
            selected,
            previous: None,
        })
    }
}
impl FrameSource for DirectoryFrameSource {
    fn snapshot(&self) -> &SnapshotEnvelope {
        &self.snapshot
    }
    fn next_frame(&mut self) -> Result<Option<SourceFrame>> {
        loop {
            let Some(input) = self.manifest.pairs.get(self.next) else {
                return Ok(None);
            };
            self.next += 1;
            if self.selected.is_some_and(|s| s != input.pair_sequence) {
                continue;
            }
            let bytes = read_bounded(&member(&self.root, &input.evidence)?, 4 * 1024 * 1024)?;
            require(sha256(&bytes) == input.evidence_sha256, "EVIDENCE_DIGEST")?;
            let e: BoundPairEvidence = parse(&bytes)?;
            e.validate()
                .map_err(|_| OfflineError::Invalid("PAIR_EVIDENCE_INVALID"))?;
            validate_binding(&self.snapshot, &e)?;
            require(
                e.pair_sequence == input.pair_sequence,
                "PAIR_SEQUENCE_BINDING",
            )?;
            if let Some(last) = &self.previous {
                require(
                    e.pair_sequence > last.pair_sequence
                        && e.clock_source == last.clock_source
                        && e.color.context.sequence > last.color.context.sequence
                        && e.depth.context.sequence > last.depth.context.sequence
                        && e.color_stamp_us > last.color_stamp_us
                        && e.depth_stamp_us > last.depth_stamp_us
                        && e.color.context.received_mono_offset_ns
                            >= last.color.context.received_mono_offset_ns
                        && e.depth.context.received_mono_offset_ns
                            >= last.depth.context.received_mono_offset_ns,
                    "SOURCE_PROGRESSION",
                )?;
            }
            require(
                input.storage_representation == self.snapshot.content.depth,
                "STORAGE_REPRESENTATION_BINDING",
            )?;
            let depth = read_bounded(&member(&self.root, &input.depth)?, 64 * 1024 * 1024)?;
            require(sha256(&depth) == input.depth_sha256, "DEPTH_DIGEST")?;
            let expected = e
                .depth
                .stride
                .checked_mul(e.depth.height as usize)
                .ok_or(OfflineError::Invalid("DEPTH_LAYOUT"))?;
            require(depth.len() == expected, "DEPTH_PAYLOAD_LENGTH")?;
            d2c_core::RawDepthView::new(
                &depth,
                e.depth.width,
                e.depth.height,
                e.depth.stride,
                match input.endian {
                    StorageEndian::Little => d2c_core::Endian::Little,
                    StorageEndian::Big => d2c_core::Endian::Big,
                },
                d2c_core::DepthScale::millimeters_per_count(representation_scale(
                    &input.storage_representation,
                ))?,
            )?;
            self.previous = Some(e.clone());
            return Ok(Some(SourceFrame {
                evidence: e,
                evidence_sha256: input.evidence_sha256.clone(),
                evidence_json: bytes,
                depth,
                endian: input.endian,
            }));
        }
    }
}
fn context_binding(context: &FrameContext, snapshot: &SnapshotEnvelope) -> bool {
    let b = &snapshot.content.binding;
    context.session_id == b.session_id
        && context.generation_id == b.generation_id
        && context.configuration_id == b.configuration_id
        && context.calibration_id == b.calibration_id
        && context.snapshot_id == snapshot.snapshot_id
}
fn validate_binding(snapshot: &SnapshotEnvelope, e: &BoundPairEvidence) -> Result<()> {
    require(
        context_binding(&e.color.context, snapshot) && context_binding(&e.depth.context, snapshot),
        "PAIR_SNAPSHOT_BINDING",
    )?;
    require(
        e.color.context.stream_id == "color" && e.depth.context.stream_id == "depth",
        "PAIR_STREAM_BINDING",
    )?;
    let calibration = &snapshot.content.calibration;
    for (name, frame) in [("color", &e.color), ("depth", &e.depth)] {
        let model = calibration
            .models
            .get(name)
            .ok_or(OfflineError::Invalid("CALIBRATION_STREAM_MISSING"))?;
        require(
            (model.width, model.height) == (frame.width, frame.height),
            "PAIR_MODEL_DIMENSIONS",
        )?;
    }
    require(
        e.delta_us <= snapshot.content.quality_policy.max_pair_delta_us,
        "PAIR_SKEW",
    )?;
    let output_scale = representation_scale(&snapshot.content.depth);
    require(
        e.depth.output_depth_scale_mm.map(f64::from) == Some(output_scale),
        "PAIR_DEPTH_UNIT_BINDING",
    )?;
    if !e.depth.normalized {
        require(
            e.depth.source_depth_scale_mm == e.depth.output_depth_scale_mm,
            "PAIR_NORMALIZATION_BINDING",
        )?
    } else {
        require(output_scale == 1., "PAIR_NORMALIZATION_BINDING")?
    }
    Ok(())
}
fn representation_scale(representation: &capture_contract::DepthRepresentation) -> f64 {
    match representation {
        capture_contract::DepthRepresentation::SdkCounts { scale_mm } => *scale_mm,
        capture_contract::DepthRepresentation::Millimetres { .. } => 1.,
    }
}
#[derive(Debug, Clone, Copy)]
pub enum Operation {
    Original { normalize_mm: bool },
    PreserveCounts,
    DepthToColor,
}
#[derive(Debug, Clone, Copy)]
pub struct RunOptions {
    pub operation: Operation,
    pub mode: depth_output::Mode,
    pub queue_capacity: usize,
}
#[derive(Debug, Serialize)]
pub struct RunReport {
    pub version: u32,
    pub complete: bool,
    pub validated_pairs: u64,
    pub written_frames: u64,
    pub d2c: bool,
    pub normalized_frames: u64,
}
fn model(model: &capture_contract::CameraModel) -> Result<d2c_core::CameraModel> {
    model.validate()?;
    let d = &model.distortion_coefficients;
    let distortion = match model.distortion_model {
        capture_contract::DistortionModel::None => d2c_core::DistortionModel::PinholeZero,
        capture_contract::DistortionModel::PlumbBob => d2c_core::DistortionModel::BrownConrady5(
            d.as_slice()
                .try_into()
                .map_err(|_| OfflineError::Invalid("DISTORTION_COUNT"))?,
        ),
        capture_contract::DistortionModel::RationalPolynomial => {
            d2c_core::DistortionModel::RationalPolynomial8(
                d.as_slice()
                    .try_into()
                    .map_err(|_| OfflineError::Invalid("DISTORTION_COUNT"))?,
            )
        }
        capture_contract::DistortionModel::Equidistant => {
            return Err(OfflineError::Invalid("D2C_MODEL_UNSUPPORTED"));
        }
    };
    Ok(d2c_core::CameraModel::new(
        d2c_core::CameraIntrinsics {
            width: model.width,
            height: model.height,
            fx: model.k[0],
            fy: model.k[4],
            cx: model.k[2],
            cy: model.k[5],
            skew: model.k[1],
        },
        distortion,
    )?)
}
struct Geometry {
    rays: d2c_core::RayTable,
    color: d2c_core::CameraModel,
    transform: d2c_core::DepthToColorTransform,
    buffer: d2c_core::WarpOutputBuffer,
}
fn geometry(snapshot: &SnapshotEnvelope) -> Result<Geometry> {
    let calibration = &snapshot.content.calibration;
    let depth = calibration
        .models
        .get("depth")
        .ok_or(OfflineError::Invalid("DEPTH_MODEL_MISSING"))?;
    let color = calibration
        .models
        .get("color")
        .ok_or(OfflineError::Invalid("COLOR_MODEL_MISSING"))?;
    let candidates: Vec<_> = calibration
        .transforms
        .iter()
        .filter(|t| t.source_frame == depth.frame_id && t.target_frame == color.frame_id)
        .collect();
    require(candidates.len() == 1, "D2C_TRANSFORM_MISSING_OR_AMBIGUOUS")?;
    let t = candidates[0];
    let digest = calibration.id()?;
    let mut id = [0; 32];
    for (i, chunk) in digest.as_str().as_bytes().chunks_exact(2).enumerate() {
        id[i] = u8::from_str_radix(
            std::str::from_utf8(chunk).map_err(|_| OfflineError::Invalid("CALIBRATION_ID"))?,
            16,
        )
        .map_err(|_| OfflineError::Invalid("CALIBRATION_ID"))?;
    }
    Ok(Geometry {
        rays: d2c_core::RayTable::build(
            &model(depth)?,
            d2c_core::CalibrationId(id),
            d2c_core::InverseOptions::default(),
            d2c_core::RayTableLimits::default(),
        )?,
        color: model(color)?,
        transform: d2c_core::DepthToColorTransform::new(t.rotation_row_major, t.translation_m)?,
        buffer: d2c_core::WarpOutputBuffer::new(d2c_core::WarpBufferLimits::default())?,
    })
}
fn normalize(
    bytes: &[u8],
    width: u32,
    height: u32,
    stride: usize,
    endian: StorageEndian,
    scale: f64,
) -> Result<Vec<u8>> {
    let pixels = depth_output::DepthView {
        bytes,
        width,
        height,
        stride,
        endian: match endian {
            StorageEndian::Little => depth_output::Endian::Little,
            StorageEndian::Big => depth_output::Endian::Big,
        },
    }
    .pixels()?;
    let mut out = Vec::new();
    out.try_reserve_exact(pixels.len() * 2)
        .map_err(|_| OfflineError::Invalid("ALLOCATION_FAILED"))?;
    for p in pixels {
        let rounded = (f64::from(p) * scale + 0.5).floor();
        require(
            rounded.is_finite() && rounded <= 65535.,
            "NORMALIZATION_OVERFLOW",
        )?;
        let value = if p == 0 {
            0
        } else if rounded < 1. {
            1
        } else {
            rounded as u16
        };
        out.extend_from_slice(&value.to_le_bytes());
    }
    Ok(out)
}
fn convert(
    frame: SourceFrame,
    snapshot: &SnapshotEnvelope,
    operation: Operation,
    geometry: &mut Option<Geometry>,
    ordinal: u64,
) -> Result<(depth_output::EncodedDepth, bool)> {
    let e = &frame.evidence;
    require(
        sha256(&frame.evidence_json) == frame.evidence_sha256,
        "EVIDENCE_DIGEST",
    )?;
    let parsed: BoundPairEvidence = parse(&frame.evidence_json)?;
    require(
        serde_json::to_value(&parsed)? == serde_json::to_value(e)?,
        "EVIDENCE_CONTENT_BINDING",
    )?;
    e.validate()
        .map_err(|_| OfflineError::Invalid("PAIR_EVIDENCE_INVALID"))?;
    validate_binding(snapshot, e)?;
    convert_material(
        &frame.depth,
        frame.endian,
        &e.depth.context,
        e.depth.width,
        e.depth.height,
        e.depth.stride,
        e.pair_sequence,
        &frame.evidence_sha256,
        &snapshot.content.depth,
        operation,
        geometry,
        ordinal,
    )
}
#[allow(clippy::too_many_arguments)]
fn convert_material(
    depth: &[u8],
    endian: StorageEndian,
    context: &FrameContext,
    source_width: u32,
    source_height: u32,
    source_stride: usize,
    sequence: u64,
    evidence_sha256: &str,
    representation: &capture_contract::DepthRepresentation,
    operation: Operation,
    geometry: &mut Option<Geometry>,
    ordinal: u64,
) -> Result<(depth_output::EncodedDepth, bool)> {
    let scale = representation_scale(representation);
    let (bytes, width, height, kind, normalized) = match operation {
        Operation::PreserveCounts => {
            require(
                matches!(
                    representation,
                    capture_contract::DepthRepresentation::SdkCounts { .. }
                ),
                "COUNTS_SOURCE_REQUIRED",
            )?;
            (
                normalize(
                    depth,
                    source_width,
                    source_height,
                    source_stride,
                    endian,
                    1.,
                )?,
                source_width,
                source_height,
                depth_output::DepthKind::OriginalSdkCounts { scale_mm: scale },
                false,
            )
        }
        Operation::Original { normalize_mm } => {
            require(
                scale == 1. || normalize_mm,
                "EXPLICIT_NORMALIZATION_REQUIRED",
            )?;
            (
                normalize(
                    depth,
                    source_width,
                    source_height,
                    source_stride,
                    endian,
                    scale,
                )?,
                source_width,
                source_height,
                depth_output::DepthKind::OriginalMillimetres,
                scale != 1.,
            )
        }
        Operation::DepthToColor => {
            let g = geometry
                .as_mut()
                .ok_or(OfflineError::Invalid("D2C_GEOMETRY_MISSING"))?;
            let raw = d2c_core::RawDepthView::new(
                depth,
                source_width,
                source_height,
                source_stride,
                match endian {
                    StorageEndian::Little => d2c_core::Endian::Little,
                    StorageEndian::Big => d2c_core::Endian::Big,
                },
                d2c_core::DepthScale::millimeters_per_count(scale)?,
            )?;
            let output =
                d2c_core::warp_depth_to_color(raw, &g.rays, &g.color, g.transform, &mut g.buffer)?;
            let mut bytes = vec![0; output.depth_mm.len() * 2];
            output.write_packed_le(&mut bytes)?;
            (
                bytes,
                output.dimensions.0,
                output.dimensions.1,
                depth_output::DepthKind::D2cColorZMillimetres,
                scale != 1.,
            )
        }
    };
    let record = depth_output::FrameRecord {
        ordinal,
        context: context.clone(),
        kind,
        pair_sequence: sequence,
        pair_evidence_sha256: evidence_sha256.to_owned(),
    };
    Ok((
        depth_output::encode(
            depth_output::DepthView {
                bytes: &bytes,
                width,
                height,
                stride: width as usize * 2,
                endian: depth_output::Endian::Little,
            },
            record,
        )?,
        normalized,
    ))
}
/// Validation includes all selected payloads and requested numerical conversion,
/// but creates no output files. It does not certify historical live provenance.
pub fn validate(source: &mut impl FrameSource, operation: Operation) -> Result<RunReport> {
    let snapshot = source.snapshot().clone();
    snapshot.verify(&snapshot.content.binding)?;
    let mut g = if matches!(operation, Operation::DepthToColor) {
        Some(geometry(&snapshot)?)
    } else {
        None
    };
    let mut report = RunReport {
        version: 1,
        complete: true,
        validated_pairs: 0,
        written_frames: 0,
        d2c: matches!(operation, Operation::DepthToColor),
        normalized_frames: 0,
    };
    while let Some(frame) = source.next_frame()? {
        let (_, normalized) = convert(frame, &snapshot, operation, &mut g, report.validated_pairs)?;
        report.validated_pairs += 1;
        report.normalized_frames += u64::from(normalized);
    }
    require(report.validated_pairs > 0, "SOURCE_EMPTY")?;
    Ok(report)
}
/// One bounded blocking producer/consumer queue. Only the caller commits the Job
/// after producer success and consumer drain; worker failure cannot mark COMPLETE.
pub fn run(
    source: &mut impl FrameSource,
    parent: &Path,
    name: &str,
    options: RunOptions,
) -> Result<RunReport> {
    require((1..=16).contains(&options.queue_capacity), "QUEUE_CAPACITY")?;
    let snapshot = source.snapshot().clone();
    snapshot.verify(&snapshot.content.binding)?;
    let mut g = if matches!(options.operation, Operation::DepthToColor) {
        Some(geometry(&snapshot)?)
    } else {
        None
    };
    let mut job =
        depth_output::Job::create(parent, name, options.mode, snapshot.snapshot_id.clone())?;
    job.attach_json("snapshot.json", &snapshot.to_bytes()?)?;
    let (tx, rx) = std::sync::mpsc::sync_channel::<(depth_output::EncodedDepth, Vec<u8>, u64)>(
        options.queue_capacity,
    );
    let worker = std::thread::Builder::new()
        .name("offline-depth-output".into())
        .spawn(
            move || -> std::result::Result<depth_output::Job, depth_output::OutputError> {
                while let Ok((frame, evidence, sequence)) = rx.recv() {
                    job.attach_json(&format!("pair-evidence-{sequence:020}.json"), &evidence)?;
                    job.append(&frame)?;
                }
                Ok(job)
            },
        )?;
    let mut report = RunReport {
        version: 1,
        complete: false,
        validated_pairs: 0,
        written_frames: 0,
        d2c: matches!(options.operation, Operation::DepthToColor),
        normalized_frames: 0,
    };
    let produced = (|| -> Result<()> {
        while let Some(frame) = source.next_frame()? {
            let evidence = frame.evidence_json.clone();
            let sequence = frame.evidence.pair_sequence;
            let (encoded, normalized) = convert(
                frame,
                &snapshot,
                options.operation,
                &mut g,
                report.validated_pairs,
            )?;
            report.validated_pairs += 1;
            report.normalized_frames += u64::from(normalized);
            tx.send((encoded, evidence, sequence))
                .map_err(|_| OfflineError::Worker)?;
        }
        require(report.validated_pairs > 0, "SOURCE_EMPTY")
    })();
    drop(tx);
    let consumed = worker.join().map_err(|_| OfflineError::Worker)?;
    produced?;
    let job = consumed?;
    job.finish()?;
    report.written_frames = report.validated_pairs;
    report.complete = true;
    Ok(report)
}
