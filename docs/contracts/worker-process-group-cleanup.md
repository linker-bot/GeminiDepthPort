# Worker process-group teardown

Linux supervisors observe worker exit with `waitid(WEXITED|WNOHANG|WNOWAIT)`.
They verify that the exited worker still leads its originally owned process
group, signal only that group, and retain the unreaped leader as a PID/PGID
identity anchor until cleanup finishes. A plain `try_wait` followed by `killpg`
would release that anchor and risk signalling a recycled group ID.

The supervisor is a Linux child subreaper. Cleanup enumerates only its own
`/proc/self/task/*/children`, waits only for adopted descendants in the anchored
worker group, and never sweeps unrelated host PIDs or groups. TERM-ignoring
helpers are killed before a new worker is opened. The original leader is reaped
last. Unexpected worker exit and requested shutdown use the same cleanup path.

Cleanup uses the existing shutdown timeout as a bounded wait budget. After an
unsuccessful graceful shutdown, forced-kill exit observation also has a bounded
wait using that same configured budget.
Failure to establish group ownership or finish cleanup is terminal; the
supervisor does not open another SDK worker over surviving owned descendants.
This does not alter startup, frame/pair/quality thresholds or restart counts.
A present recording gate still prevents reopen.

The SDK-free Linux black-box regression dynamically builds an ABI fixture whose
mock SDK forks a helper that ignores TERM. It kills the actual worker, requires
the helper to be killed/reaped before the next worker becomes ready, and checks
that graceful supervisor shutdown reaps the next helper too. An unrelated owned
test group must survive. Test cleanup uses pidfds rather than signalling stale
recorded PIDs. A second Linux test rejects a worker that does not lead its own
group while preserving it for its real owner to reap.

Run on Linux with the normal C/C++ fixture compilers:

```
cargo test -p capture-probe --offline --test service_supervision \
  killed_worker_group_helpers_are_reaped_before_reopen_and_unrelated_groups_survive
cargo test -p capture-probe --offline --lib \
  group_ownership_mismatch_is_rejected_before_reaping_or_signalling_other_group
cargo test -p capture-probe --offline --test service_supervision
```

These fixtures never access USB, a real SDK device or ROS. The fixtures verify
process ownership/lifecycle behavior only. It does not establish that an actual
Orbbec SDK creates helper processes, and is not a claimed fix for real-device
zero-frameset behavior after abrupt termination.
