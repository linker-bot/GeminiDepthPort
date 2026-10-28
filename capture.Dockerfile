# Rust data plane; all SDK bytes come from the fixed public driver checkout.
FROM rust:1.85.1-bullseye AS rust-build
WORKDIR /build
COPY Cargo.toml Cargo.lock driver_profiles.json ./
COPY src ./src
COPY crates ./crates
RUN cargo build --locked --release -p capture-probe --bin capture-service

FROM ros:humble-ros-base-jammy
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG ORBBEC_COMMIT=c153462518ad674650bafa4464fda72c27ab797a
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates cmake g++ libusb-1.0-0 libudev1 libssl3 libgl1 \
    python3-yaml python3-numpy python3-opencv ros-humble-rclpy \
    ros-humble-sensor-msgs ros-humble-std-msgs ros-humble-geometry-msgs \
    ros-humble-tf2-msgs ros-humble-tf2-ros ros-humble-rmw-fastrtps-cpp \
    && rm -rf /var/lib/apt/lists/*
RUN test "$ORBBEC_COMMIT" = c153462518ad674650bafa4464fda72c27ab797a \
    && git init /tmp/orbbec-public \
    && git -C /tmp/orbbec-public remote add origin https://github.com/orbbec/OrbbecSDK_ROS2.git \
    && git -C /tmp/orbbec-public fetch --depth 1 origin "$ORBBEC_COMMIT" \
    && git -C /tmp/orbbec-public checkout --detach FETCH_HEAD \
    && test "$(git -C /tmp/orbbec-public rev-parse HEAD)" = "$ORBBEC_COMMIT" \
    && git -C /tmp/orbbec-public submodule update --init --recursive \
    && architecture="$(dpkg --print-architecture)" \
    && if [ "$architecture" = amd64 ]; then sdk_arch=x64; elif [ "$architecture" = arm64 ]; then sdk_arch=arm64; else exit 1; fi \
    && sdk="/tmp/orbbec-public/orbbec_camera/SDK/$sdk_arch" \
    && test -f "$sdk/lib/libOrbbecSDK.so.2.10.6" \
    && mkdir -p /opt/orbbec-sdk /opt/host-udev /opt/gdp-capture /opt/third-party/orbbec \
    && cp -a "$sdk/." /opt/orbbec-sdk/ \
    && rule_file="$(find /tmp/orbbec-public -name 99-obsensor-libusb.rules -type f -print -quit)" \
    && test -n "$rule_file" && cp "$rule_file" /opt/host-udev/ \
    && cp /tmp/orbbec-public/LICENSE /opt/third-party/orbbec/ROS2-LICENSE \
    && find /tmp/orbbec-public -type f \( -iname '*license*' -o -iname '*notice*' \) -exec cp --parents --target-directory=/opt/third-party/orbbec {} + \
    && printf '%s\n' "$ORBBEC_COMMIT" > /opt/orbbec-commit \
    && rm -rf /tmp/orbbec-public
COPY ros /tmp/gdp-ros-source
RUN source /opt/ros/humble/setup.bash \
    && cmake -S /tmp/gdp-ros-source -B /tmp/gdp-ros-build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/gdp-ros \
    && cmake --build /tmp/gdp-ros-build --parallel 2 \
    && cmake --install /tmp/gdp-ros-build \
    && rm -rf /tmp/gdp-ros-source /tmp/gdp-ros-build
COPY --from=rust-build /build/target/release/capture-service /usr/local/bin/capture-service
COPY runtime /app
RUN mv /app/health.py /app/ros_health.py
COPY scripts/capture-runtime /opt/gdp-capture
COPY scripts/capture-runtime/entrypoint.sh /app/entrypoint.sh
COPY scripts/capture-runtime/validate_runtime.py /app/validate_runtime.py
COPY scripts/capture-runtime/health.py /app/health.py
RUN chmod +x /app/entrypoint.sh \
    && python3 /opt/gdp-capture/make_manifest.py \
    && chmod -R a+rX /opt/orbbec-sdk /opt/gdp-ros /opt/gdp-capture /app
LABEL org.opencontainers.image.revision=$ORBBEC_COMMIT \
    org.geminidepthport.capture-backend="rust-v1" \
    org.geminidepthport.explicit-bindings="1" \
    org.geminidepthport.native-yuyv="1" \
    org.geminidepthport.communication-profile="humble-fastdds-local-udp-v1"
ENV ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
    FASTRTPS_DEFAULT_PROFILES_FILE=/app/fastdds.xml ROS_LOG_DIR=/tmp/ros-logs
ENTRYPOINT ["/app/entrypoint.sh"]
