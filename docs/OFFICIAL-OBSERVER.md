# Official ROS observed lifecycle

New schema-2 official generations include immutable `observer-policy.json` and
require an image declaring `org.geminidepthport.official-observer=v1`. The
production camera limit remains three. Existing imported generation bundles
remain readable. Regenerate/rebuild to use the observed lifecycle; an old image
cannot silently satisfy the new capability. Legacy schema-1 launch behavior is
unchanged. New official RGB-D bundles use the existing loopback-only Fast DDS
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
policy file. It will not substitute them for a native SDK snapshot. Build the
normal official Dockerfile; its separate Bullseye Rust stage produces Jammy-
compatible `capture-observer` and `observer-bootstrap`, without host SDK copies.

## Minimal private management intent

`tools/testing/make_management_inventory.py` reads only an explicit private
capture config and runtime manifest and creates a new 0600 **unconfirmed**
schema-2 Rust inventory. It never opens hardware or marks profiles verified.
Example (paths and model must be supplied from the actual selected test device):

```
python3 tools/testing/make_management_inventory.py --capture-config /private/capture.json --runtime /private/runtime.json --profile gemini_335l --image local/gdp-rust:service --output /private/unconfirmed-intent.json
```

Use that source inventory with `tools/validation/management_acceptance.py` in a
new private workspace to obtain real preview/confirm evidence through public
APIs. Do not edit `profiles_verified` or proof hashes to create a test fixture.
The helper reports source-only policy fields (raw-recording/quality options) so
its limited management-protection fixture scope is explicit. Geometry, serial,
namespace, host clock and depth intent come from the actual private input; the
subsequent live preview obtains calibration and snapshots afresh.

Tests cover real bootstrap validation, feeding decoded observations into the
actual Rust association core, unknown SDK identities, session/snapshot changes,
geometry/TF rejection, three-camera enforcement, private immutable publication,
EOF cleanup and a genuinely partial/hung child response. These are offline
synthetic checks, not a claim that this new official Docker/ROS path has already
passed a real-device run.
