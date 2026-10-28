#!/usr/bin/env bash
# Compile only: no device access. Use SDK2.10.6 public headers/library.
set -euo pipefail
: "${SDK_ROOT:?Set SDK_ROOT to the fixed SDK root containing include/ and lib/}"
output=${1:?Usage: SDK_ROOT=/fixed/sdk bash build_sdk_ros_reference.sh /private/output/reference}
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
[[ "$SDK_ROOT" == /* && "$output" == /* ]] || exit 2
"${CXX:-c++}" -std=c++17 -O3 -DNDEBUG -Wall -Wextra -Werror \
  -I"$SDK_ROOT/include" -I"$root/ros/include" \
  "$root/tools/validation/sdk_ros_reference.cpp" \
  -L"$SDK_ROOT/lib" -Wl,-rpath,"$SDK_ROOT/lib" -lOrbbecSDK -ldl -o "$output"
"$output" --self-test
python3 - "$SDK_ROOT" "$output" "$root/tools/validation/sdk_ros_reference.cpp" <<'PY'
import hashlib, json, pathlib, sys
sdk, output, source = map(pathlib.Path, sys.argv[1:])
digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
manifest = dict(version=1, kind="minimal-cpp-reference-build", sdk_sha256=digest(sdk / "lib/libOrbbecSDK.so"),
                reference_sha256=digest(output), source_sha256=digest(source), hardware_verified=False)
output.with_name(output.name + ".build.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
PY
