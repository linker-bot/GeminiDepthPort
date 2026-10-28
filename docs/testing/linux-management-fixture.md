# Linux lifecycle Docker-double

This fixture tests the actual management binary on Linux with synthetic sysfs
and a shell Docker executable. It does not need ROS, SDK libraries, a real
Docker socket inside the container, USB devices, or network access. Its quality
facts are deliberately synthetic and cannot certify a real producer.

## Standard image

From the current checkout (including all integrated management/quality fixes):

```
docker build -f web.Dockerfile -t geminidepthport-management:observer-fixture .
GDP_TEST_IMAGE=geminidepthport-management:observer-fixture python3 tests/rust/test_linux_lifecycle.py
```

`web.Dockerfile` builds the management binary with Alpine/musl and copies it into
the Docker-CLI Alpine image. It already provides the shell utilities used by the
double (`sed`, `sha256sum`, `stat`, `date`, `id`). The fixture supplies the actual
workspace assets from its private host directory; Python is only required on
the outer test host.

## Minimal image using an existing Linux binary

A binary may be copied only into an ABI-compatible image. For example, if it was
built inside the project's Jammy `local/gdp-rust:four-fixed` image, use that same
image as the base, not Alpine and not an older glibc distribution:

```
# Private build directory contains only this Dockerfile and the new management binary.
FROM local/gdp-rust:four-fixed
COPY geminidepthport /usr/local/bin/geminidepthport
ENTRYPOINT []
CMD ["geminidepthport", "--help"]
```

Build with a new local fixture tag and run the same Python command above with
`GDP_TEST_IMAGE` set to that tag. Runtime ROS/SDK files may remain in this base
but are never invoked by the double. Do not copy an Ubuntu 26/glibc 2.39 host
binary into Jammy or Alpine. To obtain a binary built in the test image, compile
it there or copy it from that image's build output using an explicitly selected
container; no other project files or SDK directories are required.

The Python test launches each container with `--network none`, a read-only root,
`--cap-drop ALL`, a temporary `/tmp`, explicit checkout-owner UID/GID, and only
synthetic USB/udev/sysfs mounts. Its PATH selects its own shell Docker double;
no host Docker socket is mounted. Cleanup removes only its temporary directory
and the short-lived `--rm` test containers.

Before the Docker run, the shell-schema regression can run without Docker:

```
PYTHONPATH=. python3 -m unittest discover -s tests -p test_linux_observer_fixture.py -v
```

It exercises the real shell double and requires the post-review observer
quality schema (`at`, `current_session_finalized`, and `processes_alive`). This
prevents incomplete simulated evidence from being confused with a production
observer failure. Real official ROS lifecycle and physical accuracy require
separate evidence; physical accuracy remains unverified.
