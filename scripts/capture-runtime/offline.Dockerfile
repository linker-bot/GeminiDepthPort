# Test-only assembly from an already built, verified public driver image.
# Context must contain ONLY this file, capture-service, libgeminidepthport_ros_bridge.so,
# runtime/, capture-runtime/ and sdk-license/ copied from this checkout/build. Never copy a host SDK.
ARG VERIFIED_DRIVER_IMAGE
FROM ${VERIFIED_DRIVER_IMAGE}
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG EXPECTED_SDK_SHA256
COPY capture-runtime/sdk_licenses.py /opt/gdp-license-tools/sdk_licenses.py
COPY sdk-license /opt/gdp-license-tools/sdk-license
RUN test "$(cat /opt/orbbec-commit)" = c153462518ad674650bafa4464fda72c27ab797a \
    && test "${#EXPECTED_SDK_SHA256}" = 64 \
    && architecture="$(dpkg --print-architecture)" \
    && if [ "$architecture" = amd64 ]; then sdk_arch=x64; elif [ "$architecture" = arm64 ]; then sdk_arch=arm64; else exit 1; fi \
    && sdk="/ws/src/OrbbecSDK_ROS2/orbbec_camera/SDK/$sdk_arch" \
    && test -f "$sdk/lib/libOrbbecSDK.so.2.10.6" \
    && printf '%s  %s\n' "$EXPECTED_SDK_SHA256" "$sdk/lib/libOrbbecSDK.so.2.10.6" | sha256sum -c - \
    && mkdir -p /opt/orbbec-sdk /opt/gdp-ros/lib /opt/gdp-capture \
    && cp -a "$sdk/." /opt/orbbec-sdk/ \
    && rm -rf /opt/third-party/orbbec \
    && python3 /opt/gdp-license-tools/sdk_licenses.py \
         --source-root /ws/src/OrbbecSDK_ROS2 --license-root /opt/gdp-license-tools/sdk-license \
         --output /opt/third-party/orbbec
COPY capture-service /usr/local/bin/capture-service
COPY libgeminidepthport_ros_bridge.so /opt/gdp-ros/lib/libgeminidepthport_ros_bridge.so
COPY runtime /app
RUN mv /app/health.py /app/ros_health.py
COPY capture-runtime /opt/gdp-capture
COPY capture-runtime/entrypoint.sh /app/entrypoint.sh
COPY capture-runtime/validate_runtime.py /app/validate_runtime.py
COPY capture-runtime/health.py /app/health.py
RUN chmod +x /app/entrypoint.sh /usr/local/bin/capture-service \
    && python3 /opt/gdp-capture/make_manifest.py \
    && chmod -R a+rX /opt/orbbec-sdk /opt/gdp-ros /opt/gdp-capture /app
LABEL org.opencontainers.image.revision="c153462518ad674650bafa4464fda72c27ab797a" \
    org.geminidepthport.capture-backend="rust-v1" \
    org.geminidepthport.explicit-bindings="1" \
    org.geminidepthport.native-yuyv="1" \
    org.geminidepthport.communication-profile="humble-fastdds-local-udp-v1" \
    org.geminidepthport.build-mode="verified-existing-public-driver"
ENV ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp \
    FASTRTPS_DEFAULT_PROFILES_FILE=/app/fastdds.xml ROS_LOG_DIR=/tmp/ros-logs
ENTRYPOINT ["/app/entrypoint.sh"]
