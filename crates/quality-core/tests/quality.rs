use capture_contract::{
    CalibrationId, ClockDomain, ConfigurationId, FrameContext, GenerationId, MonoTime,
    QualityAction, QualityPolicy, SessionId, SnapshotId,
};
use quality_core::*;
use std::{cell::Cell, collections::BTreeMap, rc::Rc};

fn id(value: char) -> SessionId {
    SessionId::parse(value.to_string().repeat(32)).unwrap()
}
fn at(session: char, offset_ns: u64) -> MonoTime {
    MonoTime {
        session_id: id(session),
        offset_ns,
    }
}
fn policy() -> RuntimeQualityPolicy {
    RuntimeQualityPolicy {
        version: 1,
        quality: QualityPolicy {
            version: 1,
            max_source_age_ns: 100,
            max_future_ns: 10,
            max_pair_delta_us: 20,
            min_valid_fraction: 0.5,
            max_zero_fraction: 0.5,
            action: QualityAction::FailClosed,
        },
        stale_after_ns: 50,
        event_capacity: 16,
        window_capacity: 3,
        window_horizon_ns: 100,
        summary_capacity: 2,
        max_sessions: 8,
        rules: FrameViolation::ALL
            .into_iter()
            .map(|code| {
                (
                    code,
                    RulePolicy {
                        open_after: 1,
                        recover_after: 2,
                        severity: if matches!(
                            code,
                            FrameViolation::DeviceDisconnected
                                | FrameViolation::PublishPartialFailure
                        ) {
                            QualityState::Failed
                        } else {
                            QualityState::Degraded
                        },
                    },
                )
            })
            .collect(),
    }
}
fn engine() -> QualityEngine {
    QualityEngine::new(policy(), id('a'), vec!["color".into()]).unwrap()
}
fn frame(session: char, mono: u64, source: u64) -> FrameObservation {
    FrameObservation {
        context: FrameContext {
            version: 1,
            session_id: id(session),
            generation_id: GenerationId::parse("a".repeat(32)).unwrap(),
            configuration_id: ConfigurationId::parse("a".repeat(64)).unwrap(),
            calibration_id: CalibrationId::parse("a".repeat(64)).unwrap(),
            snapshot_id: SnapshotId::parse("a".repeat(64)).unwrap(),
            stream_id: "color".into(),
            sequence: None,
            device_timestamp_us: None,
            system_timestamp_us: None,
            received_mono_offset_ns: mono,
        },
        contract_valid: true,
        valid_fraction: None,
        zero_fraction: None,
        source: Some(SourceStamp {
            clock: SourceClock::ObservedHeaderNanoseconds,
            value: source,
        }),
        mapper: ClockMapper::DeclaredObservedUnixHeader,
        received_unix_ns: Some(source + 10),
    }
}
fn active(engine: &QualityEngine, code: FrameViolation) -> bool {
    engine
        .snapshot()
        .active_episodes
        .iter()
        .any(|v| v.violation == code)
}

#[test]
fn every_stable_violation_has_an_explicit_policy_and_no_normal_fault_severity() {
    let mut p = policy();
    p.rules.remove(&FrameViolation::StreamStale);
    assert!(p.validate().is_err());
    let mut p = policy();
    p.rules
        .get_mut(&FrameViolation::StreamStale)
        .unwrap()
        .severity = QualityState::Normal;
    assert!(p.validate().is_err());
    let codes = FrameViolation::ALL
        .iter()
        .map(|v| serde_json::to_string(v).unwrap())
        .collect::<Vec<_>>();
    assert!(codes.contains(&"\"PUBLISH_PARTIAL_FAILURE\"".into()));
    assert_eq!(codes.len(), 9);
}

#[test]
fn tick_without_frames_opens_and_accumulates_stale_and_requires_distinct_healthy_frames() {
    let mut e = engine();
    assert!(!e.snapshot().continue_allowed);
    e.tick(at('a', 49)).unwrap();
    assert_eq!(e.snapshot().state, QualityState::Normal);
    e.tick(at('a', 50)).unwrap();
    e.tick(at('a', 51)).unwrap();
    assert!(active(&e, FrameViolation::StreamStale));
    let page = e.events(None, 16).unwrap();
    assert_eq!(page.events[0].transition, EventTransition::Opened);
    assert_eq!(page.events.len(), 1);
    assert_eq!(e.snapshot().active_episodes[0].bad_observations, 2);
    e.record_frame(frame('a', 52, 1000)).unwrap();
    e.tick(at('a', 53)).unwrap();
    assert!(active(&e, FrameViolation::StreamStale));
    e.record_frame(frame('a', 54, 1001)).unwrap();
    assert!(!active(&e, FrameViolation::StreamStale));
    assert!(e.snapshot().continue_allowed);
    assert_eq!(
        e.events(None, 16)
            .unwrap()
            .events
            .last()
            .unwrap()
            .transition,
        EventTransition::Recovered
    );
}

#[test]
fn source_wall_mapping_and_raw_regression_are_not_receipt_staleness() {
    let mut e = engine();
    let mut old = frame('a', 0, 1000);
    old.received_unix_ns = Some(1200);
    e.record_frame(old).unwrap();
    assert!(active(&e, FrameViolation::TimeMappingInvalid));
    assert!(!active(&e, FrameViolation::StreamStale));
    e.record_frame(frame('a', 1, 900)).unwrap();
    assert!(active(&e, FrameViolation::TimestampRegression));
    e.tick(at('a', 20)).unwrap();
    assert!(!active(&e, FrameViolation::StreamStale));
    // High-water mark cannot be lowered by 900: 901 is still a regression.
    e.record_frame(frame('a', 21, 901)).unwrap();
    assert!(active(&e, FrameViolation::TimestampRegression));
    e.record_frame(frame('a', 22, 1001)).unwrap();
    e.record_frame(frame('a', 23, 1002)).unwrap();
    assert!(!active(&e, FrameViolation::TimestampRegression));
}

#[test]
fn mapping_is_explicit_checked_and_does_not_invent_sdk_provenance() {
    let observed = SourceStamp {
        clock: SourceClock::ObservedHeaderNanoseconds,
        value: 100,
    };
    assert_eq!(
        map_source_time(observed, ClockMapper::DeclaredSdkSystemUnix),
        Err(MappingError::ClockMismatch)
    );
    assert_eq!(
        map_source_time(observed, ClockMapper::Unknown),
        Err(MappingError::UnknownMapping)
    );
    assert_eq!(
        map_source_time(observed, ClockMapper::DeclaredObservedUnixHeader),
        Ok(100)
    );
    let sdk = SourceStamp {
        clock: SourceClock::Capture(ClockDomain::SdkSystemUnixMicroseconds),
        value: u64::MAX,
    };
    assert_eq!(
        map_source_time(sdk, ClockMapper::DeclaredSdkSystemUnix),
        Err(MappingError::Overflow)
    );
    let zero_device = SourceStamp {
        clock: SourceClock::Capture(ClockDomain::DeviceMicroseconds),
        value: 0,
    };
    assert_eq!(
        map_source_time(
            zero_device,
            ClockMapper::MeasuredDeviceOffset { offset_ns: 100 }
        ),
        Ok(100)
    );
    let mono = SourceStamp {
        clock: SourceClock::Capture(ClockDomain::SessionMonotonicNanoseconds),
        value: 100,
    };
    assert_eq!(
        map_source_time(mono, ClockMapper::DeclaredObservedUnixHeader),
        Err(MappingError::ClockMismatch)
    );
    assert_eq!(
        validate_source_age(
            observed,
            ClockMapper::DeclaredObservedUnixHeader,
            None,
            &policy().quality
        ),
        Err(MappingError::ReceiveUnixMissing)
    );
    assert_eq!(
        validate_source_age(
            observed,
            ClockMapper::DeclaredObservedUnixHeader,
            Some(80),
            &policy().quality
        ),
        Err(MappingError::SourceInFuture)
    );
}

#[test]
fn session_change_preserves_old_episode_and_unknown_first_stamp_cannot_clear_it() {
    let mut e = engine();
    e.record_frame(frame('a', 0, 1000)).unwrap();
    e.record_frame(frame('a', 1, 900)).unwrap();
    let original = e
        .snapshot()
        .active_episodes
        .iter()
        .find(|v| v.violation == FrameViolation::TimestampRegression)
        .unwrap()
        .clone();
    e.begin_session(id('b')).unwrap();
    let snapshot = e.snapshot();
    assert!(active(&e, FrameViolation::TimestampRegression));
    assert!(active(&e, FrameViolation::SessionChanged));
    assert!(snapshot.finalized_sessions[0].finalized);
    assert!(
        snapshot.finalized_sessions[0]
            .open_episode_sequences
            .contains(&original.episode_sequence)
    );
    e.record_frame(frame('b', 0, 1)).unwrap();
    assert!(active(&e, FrameViolation::TimestampRegression));
    e.record_frame(frame('b', 1, 2)).unwrap();
    assert!(active(&e, FrameViolation::TimestampRegression));
    e.record_frame(frame('b', 2, 3)).unwrap();
    assert!(!active(&e, FrameViolation::TimestampRegression));
    assert!(active(&e, FrameViolation::SessionChanged)); // explicit acknowledgement, never auto-clear.
    let recovered = e
        .events(None, 16)
        .unwrap()
        .events
        .into_iter()
        .find(|v| {
            v.violation == FrameViolation::TimestampRegression
                && v.transition == EventTransition::Recovered
        })
        .unwrap();
    assert_eq!(recovered.origin.session_id, id('a'));
    assert_eq!(recovered.at.session_id, id('b'));
}

#[test]
fn finalized_summary_does_not_close_episode_or_rewrite_validity() {
    let mut e = engine();
    e.evaluate(
        at('a', 1),
        Scope::Group,
        FrameViolation::DeviceDisconnected,
        Condition::Violated,
    )
    .unwrap();
    let summary = e.finalize_session();
    assert!(summary.finalized);
    assert_eq!(e.snapshot().state, QualityState::Failed);
    assert_eq!(e.tick(at('a', 2)), Err("SESSION_FINALIZED"));
    assert!(!e.snapshot().continue_allowed);
    assert_eq!(e.finalize_session(), summary);
    assert_eq!(e.snapshot().finalized_sessions.len(), 1);
}

#[test]
fn event_gap_pagination_is_explicit_and_wrong_feed_or_future_cursor_never_looks_complete() {
    let mut p = policy();
    p.event_capacity = 2;
    p.rules
        .get_mut(&FrameViolation::QueueOverflow)
        .unwrap()
        .recover_after = 1;
    let mut e = QualityEngine::new(p, id('a'), vec!["color".into()]).unwrap();
    for n in 0..4 {
        e.evaluate(
            at('a', n),
            Scope::Group,
            FrameViolation::QueueOverflow,
            if n % 2 == 0 {
                Condition::Violated
            } else {
                Condition::Healthy
            },
        )
        .unwrap();
    }
    let first = e.events(None, 1).unwrap();
    let gap = first.gap.unwrap();
    assert_eq!(gap.code, "EVENT_GAP");
    assert_eq!(gap.last_lost_sequence, 2);
    assert_eq!(first.events[0].sequence, 3);
    assert!(first.has_more);
    let second = e.events(Some(first.next_cursor), 1).unwrap();
    assert_eq!(second.events[0].sequence, 4);
    assert!(!second.has_more);
    assert!(second.gap.is_none());
    assert_eq!(
        e.events(
            Some(EventCursor {
                origin_session_id: id('b'),
                after_sequence: 0
            }),
            1
        ),
        Err("CURSOR_DOMAIN_MISMATCH")
    );
    assert_eq!(
        e.events(
            Some(EventCursor {
                origin_session_id: id('a'),
                after_sequence: 5
            }),
            1
        ),
        Err("CURSOR_AHEAD")
    );
    assert!(!active(&e, FrameViolation::QueueOverflow));
}

#[test]
fn bounded_window_expires_by_tick_releases_ownership_and_rejects_cross_domain() {
    #[derive(Debug)]
    struct Owned(Rc<Cell<u32>>);
    impl Drop for Owned {
        fn drop(&mut self) {
            self.0.set(self.0.get() + 1);
        }
    }
    let released = Rc::new(Cell::new(0));
    let mut w = RollingWindow::new(id('a'), 2, 5).unwrap();
    w.push(&at('a', 0), Owned(released.clone())).unwrap();
    w.push(&at('a', 1), Owned(released.clone())).unwrap();
    assert_eq!(
        w.push(&at('a', 2), Owned(released.clone()))
            .unwrap()
            .capacity,
        1
    );
    assert_eq!(released.get(), 1);
    assert_eq!(w.tick(&at('a', 7)).unwrap().expired, 1);
    assert_eq!(released.get(), 2);
    assert_eq!(w.len(), 1);
    assert_eq!(w.tick(&at('b', 10)), Err("MONOTONIC_DOMAIN_MISMATCH"));
    assert_eq!(w.tick(&at('a', 6)), Err("MONOTONIC_REGRESSION"));
    drop(w);
    assert_eq!(released.get(), 3);
}

#[test]
fn policy_hysteresis_unknown_does_not_recover_and_failed_severity_is_distinct() {
    let mut p = policy();
    p.rules
        .get_mut(&FrameViolation::FrameContractInvalid)
        .unwrap()
        .open_after = 2;
    let mut e = QualityEngine::new(p, id('a'), vec!["color".into()]).unwrap();
    e.evaluate(
        at('a', 0),
        Scope::Stream("color".into()),
        FrameViolation::FrameContractInvalid,
        Condition::Violated,
    )
    .unwrap();
    assert_eq!(e.snapshot().state, QualityState::Normal);
    e.evaluate(
        at('a', 1),
        Scope::Stream("color".into()),
        FrameViolation::FrameContractInvalid,
        Condition::Violated,
    )
    .unwrap();
    assert_eq!(e.snapshot().state, QualityState::Degraded);
    e.evaluate(
        at('a', 2),
        Scope::Group,
        FrameViolation::PublishPartialFailure,
        Condition::Violated,
    )
    .unwrap();
    assert_eq!(e.snapshot().state, QualityState::Failed);
    e.evaluate(
        at('a', 3),
        Scope::Group,
        FrameViolation::PublishPartialFailure,
        Condition::Unknown,
    )
    .unwrap();
    assert_eq!(e.snapshot().state, QualityState::Failed);
    assert!(!e.snapshot().continue_allowed);
}

#[test]
fn binding_changes_bad_fractions_and_pair_skew_have_stable_reasons() {
    let mut e = engine();
    e.record_frame(frame('a', 0, 1000)).unwrap();
    let mut changed = frame('a', 1, 1001);
    changed.context.snapshot_id = SnapshotId::parse("b".repeat(64)).unwrap();
    e.record_frame(changed).unwrap();
    assert!(active(&e, FrameViolation::FrameContractInvalid));
    let mut bad = frame('a', 2, 1002);
    bad.zero_fraction = Some(0.75);
    e.record_frame(bad).unwrap();
    assert!(active(&e, FrameViolation::FrameContractInvalid));
    e.observe_pair_delta(at('a', 3), 21).unwrap();
    assert!(active(&e, FrameViolation::PairSkewExceeded));
    e.observe_pair_delta(at('a', 4), 20).unwrap();
    assert!(active(&e, FrameViolation::PairSkewExceeded));
    e.observe_pair_delta(at('a', 5), 20).unwrap();
    assert!(!active(&e, FrameViolation::PairSkewExceeded));
}

#[test]
fn registration_and_clock_errors_do_not_clear_quality_or_create_unbounded_keys() {
    let mut e = engine();
    e.tick(at('a', 50)).unwrap();
    let snapshot = e.snapshot();
    assert_eq!(e.tick(at('a', 49)), Err("MONOTONIC_REGRESSION"));
    assert_eq!(e.snapshot(), snapshot);
    assert_eq!(e.tick(at('b', 50)), Err("MONOTONIC_DOMAIN_MISMATCH"));
    assert_eq!(
        e.evaluate(
            at('a', 50),
            Scope::Stream("not_registered".into()),
            FrameViolation::QueueOverflow,
            Condition::Violated
        ),
        Err("STREAM_UNREGISTERED")
    );
    assert_eq!(e.snapshot(), snapshot);
}

#[test]
fn summaries_are_bounded_and_session_reuse_is_rejected_even_after_summary_eviction() {
    let mut e = engine();
    for n in 0..7 {
        e.begin_session(SessionId::parse(format!("{n:032x}")).unwrap())
            .unwrap();
    }
    assert_eq!(e.snapshot().finalized_sessions.len(), 2);
    assert_eq!(e.snapshot().summaries_evicted, 5);
    assert_eq!(e.begin_session(id('a')), Err("SESSION_ID_REUSED"));
    assert_eq!(e.begin_session(id('f')), Err("SESSION_LIMIT_EXHAUSTED"));
}

#[test]
fn json_query_schema_uses_capture_contract_ids_and_explicit_unknowns() {
    let mut e = engine();
    let mut unknown = frame('a', 0, 1000);
    unknown.source = None;
    unknown.mapper = ClockMapper::Unknown;
    unknown.received_unix_ns = None;
    e.record_frame(unknown).unwrap();
    assert!(!e.snapshot().continue_allowed);
    let value = serde_json::to_value(e.snapshot()).unwrap();
    assert_eq!(value["version"], 1);
    assert_eq!(
        value["streams"]["color"]["recent_checks_passed_fraction"],
        0.0
    );
    let decoded: QualitySnapshot = serde_json::from_value(value).unwrap();
    assert_eq!(decoded.session_id, id('a'));
    let mut p = policy();
    p.quality.action = QualityAction::ReportOnly;
    let e = QualityEngine::new(p, id('b'), vec!["color".into()]).unwrap();
    assert!(e.snapshot().continue_allowed);
    let _: BTreeMap<FrameViolation, RulePolicy> = policy().rules;
}

#[test]
fn unknown_breaks_recovery_hysteresis_without_closing_the_episode() {
    let mut e = engine();
    for (offset, condition) in [
        (0, Condition::Violated),
        (1, Condition::Healthy),
        (2, Condition::Unknown),
        (3, Condition::Healthy),
    ] {
        e.evaluate(
            at('a', offset),
            Scope::Group,
            FrameViolation::DeviceDisconnected,
            condition,
        )
        .unwrap();
    }
    assert!(active(&e, FrameViolation::DeviceDisconnected));
    assert_eq!(e.snapshot().active_episodes[0].recovery_observations, 1);
    e.evaluate(
        at('a', 4),
        Scope::Group,
        FrameViolation::DeviceDisconnected,
        Condition::Healthy,
    )
    .unwrap();
    assert!(!active(&e, FrameViolation::DeviceDisconnected));
}

#[test]
fn unified_query_is_versioned_and_preserves_explicit_event_gap_schema() {
    let e = engine();
    let request: QualityQueryRequest =
        serde_json::from_str(r#"{"version":1,"query":{"kind":"snapshot"}}"#).unwrap();
    let response = e.query(request).unwrap();
    assert_eq!(
        serde_json::to_value(response).unwrap()["result"]["kind"],
        "snapshot"
    );
    let request = QualityQueryRequest {
        version: 2,
        query: QualityQuery::Snapshot,
    };
    assert_eq!(e.query(request), Err("QUALITY_QUERY_VERSION"));
    assert!(
        serde_json::from_str::<QualityQueryRequest>(
            r#"{"version":1,"query":{"kind":"snapshot"},"unexpected":true}"#
        )
        .is_err()
    );
}

#[test]
fn changing_clock_or_mapper_cannot_heal_an_old_regression_in_the_same_session() {
    let mut e = engine();
    e.record_frame(frame('a', 0, 1000)).unwrap();
    e.record_frame(frame('a', 1, 900)).unwrap();
    for n in 2..5 {
        let mut changed = frame('a', n, 1000 + n);
        changed.source = Some(SourceStamp {
            clock: SourceClock::Capture(ClockDomain::SdkSystemUnixMicroseconds),
            value: 1000 + n,
        });
        changed.mapper = ClockMapper::DeclaredSdkSystemUnix;
        changed.received_unix_ns = Some((1000 + n) * 1000 + 10);
        e.record_frame(changed).unwrap();
    }
    assert!(active(&e, FrameViolation::TimestampRegression));
    assert!(active(&e, FrameViolation::FrameContractInvalid));
    assert!(active(&e, FrameViolation::TimeMappingInvalid));
    assert!(!e.snapshot().continue_allowed);
}

#[test]
fn sustained_fault_does_not_flush_transition_history_from_the_event_ring() {
    let mut e = engine();
    for n in 0..10_000 {
        e.evaluate(
            at('a', n),
            Scope::Group,
            FrameViolation::DeviceDisconnected,
            Condition::Violated,
        )
        .unwrap();
    }
    let page = e.events(None, 16).unwrap();
    assert_eq!(page.events.len(), 1);
    assert!(page.gap.is_none());
    assert_eq!(e.snapshot().active_episodes[0].bad_observations, 10_000);
    e.evaluate(
        at('a', 10_000),
        Scope::Group,
        FrameViolation::DeviceDisconnected,
        Condition::Healthy,
    )
    .unwrap();
    e.evaluate(
        at('a', 10_001),
        Scope::Group,
        FrameViolation::DeviceDisconnected,
        Condition::Healthy,
    )
    .unwrap();
    let page = e.events(None, 16).unwrap();
    assert_eq!(page.events.len(), 2);
    assert_eq!(page.events[1].transition, EventTransition::Recovered);
    assert_eq!(page.events[1].bad_observations, 10_000);
}
