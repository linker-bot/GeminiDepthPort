# Python migration oracle

These retired management modules are test references, not supported CLI or server entrypoints. Production management uses the Rust `geminidepthport` binary.

Tests explicitly import `tests/legacy_support.py` to make the oracle available. It also configures child Python interpreters used by lock and optimization tests. Repository assets remain rooted at the project directory. `driver_build.py` remains at the root because CI still uses its patch identity helper.

Keep this oracle while `tests/rust/generate_contracts.py`, Python-created generation compatibility checks, and legacy regression tests depend on it. Remove modules only after transferring their relevant coverage to Rust contracts or Rust CLI/HTTP tests. Do not use this directory in production images.
