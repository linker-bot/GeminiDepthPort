FROM ros:humble-ros-base-jammy
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG ORBBEC_COMMIT
ARG YUYV_PATCH_SHA256=91e120410d29e1a6e886bfc37c7efd4b091613fe28871766b134b7b14c21287e
LABEL org.opencontainers.image.revision=$ORBBEC_COMMIT
LABEL org.geminidepthport.explicit-bindings="1"
LABEL org.geminidepthport.communication-profile="humble-fastdds-local-udp-v1"
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates build-essential cmake python3-colcon-common-extensions python3-rosdep \
    python3-yaml python3-opencv python3-numpy libgflags-dev nlohmann-json3-dev libdw-dev libssl-dev libgl1 \
    libgoogle-glog-dev libusb-1.0-0-dev libudev-dev \
    ros-humble-image-transport ros-humble-image-transport-plugins \
    ros-humble-image-publisher ros-humble-camera-info-manager \
    ros-humble-diagnostic-updater ros-humble-diagnostic-msgs ros-humble-statistics-msgs \
    ros-humble-xacro ros-humble-backward-ros ros-humble-rmw-fastrtps-cpp \
    ros-humble-tf2-ros ros-humble-tf2-msgs \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /ws
RUN [[ "$ORBBEC_COMMIT" =~ ^[0-9a-f]{40}$ ]] \
    && mkdir -p src/OrbbecSDK_ROS2 \
    && git -C src/OrbbecSDK_ROS2 init \
    && git -C src/OrbbecSDK_ROS2 remote add origin https://github.com/orbbec/OrbbecSDK_ROS2.git \
    && git -C src/OrbbecSDK_ROS2 fetch --depth 1 origin "$ORBBEC_COMMIT" \
    && git -C src/OrbbecSDK_ROS2 checkout --detach FETCH_HEAD \
    && test "$(git -C src/OrbbecSDK_ROS2 rev-parse HEAD)" = "$ORBBEC_COMMIT" \
    && git -C src/OrbbecSDK_ROS2 submodule update --init --recursive
COPY patches/public-orbbec /opt/geminidepthport/patches/public-orbbec
COPY scripts/apply_public_yuyv_patch.py /opt/geminidepthport/scripts/apply_public_yuyv_patch.py
RUN test "$(sha256sum /opt/geminidepthport/patches/public-orbbec/0001-publish-owned-native-yuyv.patch | cut -d' ' -f1)" = "$YUYV_PATCH_SHA256" \
    && python3 /opt/geminidepthport/scripts/apply_public_yuyv_patch.py \
         --source-root /ws/src/OrbbecSDK_ROS2 --upstream-commit "$ORBBEC_COMMIT" --action apply \
         > /opt/native-yuyv-source.json
RUN source /opt/ros/humble/setup.bash \
    && (test -e /etc/ros/rosdep/sources.list.d/20-default.list || rosdep init) \
    && rosdep update --rosdistro humble \
    && apt-get update \
    && rosdep install --from-paths src --ignore-src -y --rosdistro humble \
    && CMAKE_BUILD_PARALLEL_LEVEL=2 MAKEFLAGS=-j2 colcon build --parallel-workers 2 --cmake-args -DCMAKE_BUILD_TYPE=Release \
    && rm -rf build log /var/lib/apt/lists/*
RUN mkdir -p /opt/host-udev \
    && rule_file="$(find /ws/src/OrbbecSDK_ROS2 -name 99-obsensor-libusb.rules -type f -print -quit)" \
    && test -n "$rule_file" && cp "$rule_file" /opt/host-udev/ \
    && echo "$ORBBEC_COMMIT" > /opt/orbbec-commit
COPY tests/cpp /opt/geminidepthport/tests/cpp
RUN architecture="$(dpkg --print-architecture)" \
    && if [ "$architecture" = amd64 ]; then sdk_arch=x64; elif [ "$architecture" = arm64 ]; then sdk_arch=arm64; else exit 1; fi \
    && sdk="/ws/src/OrbbecSDK_ROS2/orbbec_camera/SDK/$sdk_arch" \
    && c++ -std=c++17 -Wall -Wextra -Werror -fsanitize=address,undefined \
         -I/ws/src/OrbbecSDK_ROS2/orbbec_camera/include \
         /opt/geminidepthport/tests/cpp/native_yuyv_buffer_test.cpp -o /opt/native-yuyv-buffer-test \
    && /opt/native-yuyv-buffer-test \
    && c++ -std=c++17 -I/ws/src/OrbbecSDK_ROS2/orbbec_camera/include -I"$sdk/include" \
         /opt/geminidepthport/tests/cpp/native_yuyv_sdk_test.cpp \
         -L"$sdk/lib" -lOrbbecSDK -Wl,-rpath,"$sdk/lib" -o /opt/native-yuyv-sdk-test \
    && /opt/native-yuyv-sdk-test
COPY runtime /app
COPY driver_profiles.json /app/driver_profiles.json
COPY tests/ros_native_yuyv_smoke.py /opt/geminidepthport/tests/ros_native_yuyv_smoke.py
RUN chmod +x /app/entrypoint.sh \
    && source /opt/ros/humble/setup.bash \
    && ROS_LOCALHOST_ONLY=1 python3 /opt/geminidepthport/tests/ros_native_yuyv_smoke.py
LABEL org.geminidepthport.native-yuyv="1"
LABEL org.geminidepthport.native-yuyv-patch=$YUYV_PATCH_SHA256
ENTRYPOINT ["/app/entrypoint.sh"]
