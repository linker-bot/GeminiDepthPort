"""Explicit SDK-system Unix source-time validation; no inferred clock mapping."""
import math


def host_stamp_fresh(sec, nanosec, now_ns, freshness_seconds, future_tolerance_ns=100_000_000):
    if (type(sec) is not int or type(nanosec) is not int or type(now_ns) is not int
            or not 0 <= sec <= 2_147_483_647 or not 0 <= nanosec < 1_000_000_000
            or not sec and not nanosec or now_ns <= 0
            or type(freshness_seconds) not in (int, float)
            or not math.isfinite(freshness_seconds) or freshness_seconds <= 0):
        return False
    age = now_ns - (sec * 1_000_000_000 + nanosec)
    return -future_tolerance_ns <= age <= freshness_seconds * 1_000_000_000
