# Rust 帧包与 ROS 发布实验

这是单相机、单路采集探针的可选输出层，不接管默认 Compose 驱动，不代表整组相机就绪或录制验收通过。Rust 持有 SDK 帧或归一化后的自有缓冲，Humble C++ rclcpp 适配层通过版本化 C ABI 接收借用的数据，在调用返回前构造自有 ROS 消息。

## 像素与元数据共同入队

`frame-core::FramePacket<T>` 把拥有所有权的像素载荷与对应的 `PacketMetadata` 绑定。元数据包含实际布局、编码、帧号、原始 SDK 设备/系统时间、源深度尺度、输出深度尺度、是否已归一化和主机接收时刻。归一化只替换载荷和输出单位，不丢失原始帧身份或时间信息。队列淘汰时整包释放，不把另一帧的头信息拼到保留像素上。

## 发布接口

每路发布三个话题，均置于显式指定的命名空间：

- `{namespace}/{stream}/image_raw`：`sensor_msgs/msg/Image`，原生 `yuyv` 或毫米 `16UC1`。
- `{namespace}/{stream}/camera_info`：`sensor_msgs/msg/CameraInfo`，与图像使用相同时间戳和光学帧。
- `{namespace}/{stream}/frame_metadata`：`std_msgs/msg/String`，最多 4096 字节的 JSON，记录序列号、对应 ROS 时间戳、原始 SDK 时间戳、编码/尺寸/步长、源/输出深度尺度和归一化状态。这里的序列号是帧计数，不是设备序列号。

发布 QoS 配置为 RELIABLE、VOLATILE、KEEP_LAST(10)。三次发布不是跨话题原子事务；消费方应按时间戳关联图像、内参和元数据，并检查完整性。桥接层发生部分发布后仍可能报告失败，不能将调用计数当成消费者交付数量。独立订阅验收才提供交付证据。

单路 `capture-probe` 不发布 TF、不推导外参，也不做彩深配对。新增 [同一 Pipeline 的 RGB-D 与真实 TF 实验](rust-rgbd-tf-pairing.md)通过 `rgbd-probe` 同时采集双路，从实际 SDK profile 读取外参并发布配对证据。两个单路 worker 仍不得同时打开同一设备；两种探针都不认证硬件曝光同步，也不实现设备重连。

## 时间映射

调用者必须显式选择 `--ros-stamp-source sdk-system-unix`，声明 SDK system 微秒字段对应主机 Unix 时钟。ROS stamp 由该帧保存的原值转换，不使用出队、预览、发布或消费时刻替代。SDK device 时间保留在元数据中，不自动解释为 Unix 时间。

Rust 与 C++ 两侧均校验非零时间、ROS i32 秒范围、映射一致性和每路递增；重复、回退或无效时间会使实验失败，不通过修改 stamp 掩盖。此映射不证明硬件曝光时刻、跨设备同步或物理端到端延迟。

## 标定输入

必须提供私有 CameraInfo JSON，字段精确为 `version: 1`、`width`、`height`、`frame_id`、`distortion_model`、`k`、`d`、`r`、`p`。数组长度分别为 9、模型规定的系数个数、9 和 12。仅支持 plumb_bob、rational_polynomial 和 equidistant，校验有限数、焦距、齐次矩阵和合法旋转。尺寸与光学帧必须匹配当前实验；没有默认、虚构或未标定的后备内参。

从当前设备/档位的 SDK 内参或已有受控 CameraInfo 证据准备该文件；操作者仍须确认设备与档位适用性。文件几何校验不等于 generation 绑定、工厂标定精度或手眼标定验收。真实标定与运行绑定应留在受控目录，不能提交到公开仓库。

## 构建与离线 ROS 测试

```bash
docker build -f ros/Dockerfile -t geminidepthport-ros-bridge:test .
docker run --rm --network none --read-only --cap-drop ALL --tmpfs /tmp \
  geminidepthport-ros-bridge:test
```

合成 ROS 测试使用明确标记的 YUYV/深度小图，验证实际消息字节、缓冲复制、CameraInfo 与元数据关联及可观察的 QoS，不访问 SDK 或 USB。

在已有 Humble 开发环境也可以执行：

```bash
source /opt/ros/humble/setup.bash
cmake -S ros -B /tmp/gdp-ros-build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/tmp/gdp-ros-install
cmake --build /tmp/gdp-ros-build --parallel 2
cmake --install /tmp/gdp-ros-build
cargo build --locked --release -p capture-probe
```

## 受控真机运行

在 SDK、桥接库和 ROS 依赖均可加载的 Linux 环境中运行。以下变量由私有设备绑定和标定证据填写：

```bash
./target/release/capture-probe \
  --sdk-library "$SDK_LIBRARY" --serial "$CAMERA_SERIAL" \
  --stream depth --width "$DEPTH_WIDTH" --height "$DEPTH_HEIGHT" --fps "$DEPTH_FPS" \
  --depth-unit-policy normalize-mm --seconds 15 --capacity 2 \
  --ros-bridge "$ROS_BRIDGE_LIBRARY" --ros-namespace "$CAMERA_NAMESPACE" \
  --ros-frame-id "$DEPTH_OPTICAL_FRAME" --ros-calibration "$PRIVATE_CAMERA_INFO_JSON" \
  --ros-stamp-source sdk-system-unix
```

五个 ROS 参数必须一起提供。彩色模式去掉深度归一化策略并使用彩色对应标定。仅含 token 的 synthetic 模式拒绝发布，以免把没有像素的数据伪装成图像。

报告分别记录转换、队列等待、ROS 发布调用耗时；`delivery_verified` 保持 false，因为发布者不能替消费方证明实际接收。SDK 日志、标定文件和现场话题均应作为私有数据保存。要宣称正式接口兼容，仍须完成跨进程消费、时间/深度参考、录制保护与生命周期验收。
