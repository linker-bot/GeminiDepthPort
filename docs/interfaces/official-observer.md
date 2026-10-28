# Official ROS observed lifecycle

Schema-v2 official generations include immutable `observer-policy.json` and
require an image declaring `org.geminidepthport.official-observer=v1`. The
production camera limit remains three. Existing imported generation bundles
remain readable. Regenerate/rebuild to use the observed lifecycle; an old image
cannot silently satisfy the new capability. Legacy schema-1 launch behavior is
unchanged. Official RGB-D bundles use the existing loopback-only Fast DDS
XML with `ROS_LOCALHOST_ONLY=0`; the profile name is unchanged.

The camera entrypoint owns both the existing official ROS driver and a read-only
observation boundary. It first collects actual CameraInfo and factory static TF,
then the Rust bootstrap validates geometry/direction, seals a `RosCameraInfoTf`
CaptureSnapshot and creates the existing `capture-observer` configuration.
Private snapshot publication never overwrites a corrupt occupied address.

The rclpy boundary sends only decoded header/layout/CameraInfo facts and declared
receive clocks to the Rust JSONL core. It does not duplicate association or
quality rules. SDK sequence, device timestamp and SDK system timestamp remain
null. Association provenance is `observed_timestamp`, with SDK frameset identity
unknown and hardware sync unverified. A host header mapping is declared only
when the inventory explicitly selects host time; other header clocks remain
unknown. No physical accuracy certification is performed.

Each observation attempt gets a new random observer session and immutable
snapshot. EOF, invalid responses or a bounded core-response timeout terminate
and reap the core child. A partial JSON response cannot indefinitely block the
supervisor. A failed observer is retried at most three times with fresh
observations/session; exhaustion or driver exit ends the container and releases
its processes. Graceful stop cancels collection without replacing the owner's
signal policy, finalizes facts when possible, and terminates driver/core process
groups within bounded deadlines. This does not gate internal SDK reopen inside
the official ROS driver: recording protection remains explicitly v1/weak.

Private artifacts live under `/tmp/capture-observer`: session-bound status,
content-addressed snapshots and bounded current/events/final-summary records.
The manager binds these to the actual Docker container, generation and hashed
policy file. It will not substitute them for a native SDK snapshot. The published image must declare the observer capability; the manager rejects an
image that cannot provide it.

## Validation

Synthetic checks cover bootstrap geometry/TF validation, strict Rust association,
unknown SDK identities, session changes, immutable publication, EOF and interrupted
core responses. These are not real-device delivery or physical-accuracy evidence.
Hardware results and remaining acceptance work are listed in the
[validation index](../validation/README.md). Controlled fixture tools are documented
under [testing tools](../../tools/testing/README.md).
