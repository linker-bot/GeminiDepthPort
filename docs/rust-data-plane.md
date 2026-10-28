# Rust 数据链第一阶段：单机采集探针

本页保留单路探针的使用与边界。当前另有同一 Pipeline 的 RGB-D 探针，以及显式选择的常驻 Rust 采集后端；默认部署仍使用官方驱动。常驻服务、generation、镜像和健康检查见 [Rust 采集部署](rust-capture-deployment.md)，独立消费、录制与长期验收见 [消费侧验收](rust-service-acceptance.md)。功能验证不代表性能收益已经成立。

## 模块与边界

- `crates/orbbec-sys`：显式绝对路径动态加载 SDK，最小 C ABI 声明。
- `crates/orbbec-safe`：设备/Pipeline/Frame RAII，帧数据借用与帧生命周期绑定；不声明 SDK 句柄可跨线程。
- `crates/frame-core`：无 SDK/ROS 依赖的布局校验和有界队列。
- `crates/capture-probe`：单相机、单路 color 或 depth 的实验入口，以及无设备的合成队列测试。
- 同包 `rgbd-probe`：一个 Pipeline 的彩深双路、真实 SDK 标定/静态 TF 与配对验收，见 [双路实验](rust-rgbd-tf-pairing.md)。
- `crates/ros-output` 与 `ros/`：可选的 Rust 帧包到 Humble 标准消息的同步发布适配，见 [ROS 发布实验](rust-ros-output.md)。

ABI 声明依据相邻 OrbbecSDK_ROS2 工作区 SDK 2.9.3 头文件整理，尚未证明等于本项目固定上游 commit 所打包的 SDK。加载成功、版本可读和符号存在都不构成 ABI 或硬件兼容认证。首次真机实验应核对实际头文件、共享库、扩展和固件版本，并保存共享库摘要。只加载可信的厂商共享库；动态加载会执行库代码。

此阶段不引入设备回调，采用带超时的同步轮询。同一线程持有帧和有界队列，不引入未经证明的 `Send/Sync`。队列满时淘汰最旧帧，适用于探针/预览实验，不是无损录制策略。队列容量限制的是排队项，不包含 SDK 内部缓存、当前 frameset 和临时引用；不要将其报告为整个 SDK 的缓冲上限。

## 离线验证

在仓库根目录运行：

```bash
cargo test --locked -p frame-core -p orbbec-sys -p orbbec-safe -p capture-probe -p ros-output
cargo clippy --locked -p frame-core -p orbbec-sys -p orbbec-safe -p capture-probe -p ros-output --all-targets -- -D warnings
cargo run --locked -p capture-probe -- --synthetic \
  --stream color --width 640 --height 480 --fps 30 \
  --frames 20 --capacity 2 --drain-every 5
```

合成模式使用所有权 token 而非完整图像，不模拟 SDK、USB、图像内容、实际帧率或设备时间。上述尺寸只用于离线布局测试，不是相机支持档位声明。合成结果不可用于性能对比。

## 目标 Linux 主机上的单路实验

先停止目标相机的现有驱动和预览，避免两个进程竞争同一设备。使用已经验证的真实档位；以下变量由操作者在受控主机填写，不应提交真实设备配置：

```bash
cargo build --locked --release -p capture-probe
./target/release/capture-probe \
  --sdk-library "$SDK_LIBRARY" --serial "$CAMERA_SERIAL" \
  --stream color --width "$COLOR_WIDTH" --height "$COLOR_HEIGHT" --fps "$COLOR_FPS" \
  --seconds 10 --poll-ms 100 --capacity 2
```

深度实验改为 `--stream depth` 并填写深度档位。默认 `--depth-unit-policy require-mm` 只接受 Y16、每个原始计数对应 1 mm 的尺度；其他尺度直接拒绝，不静默将原始整数解释为毫米。本阶段对 SDK 来源只接受紧密布局，不根据载荷长度推断未知行填充。

需要把其他 SDK 深度尺度转为毫米时，显式指定 `--depth-unit-policy normalize-mm`。它将实际帧数据转换到独立拥有的、紧密小端 u16 毫米缓冲，再进入有界队列；即使输入尺度已经是 1 mm，该策略仍执行转换和复制。它不修改设备属性，也不只修改单位标签。

转换使用 SDK 报告的有限正尺度，乘法提升到 f64，四舍五入到整数毫米（半值向上）。原始零值保持无效零；非零样本舍入为零时提升到 1 mm，并单独计数。舍入结果超出 u16 时拒绝整帧，不饱和截断、不返回部分帧。该量化策略不是物理精度验证；已知距离实测仍是独立验收项。

报告区分原始 SDK 尺度、是否符合原始毫米契约，以及转换后输出单位。转换耗时与队列等待分别统计，不能将二者混为 SDK/USB/端到端延迟。彩色和仅含所有权 token 的 synthetic 模式拒绝 `normalize-mm`，防止没有转换真实像素却报告转换成功。

成功报告包含接收、校验、处理、主动淘汰、轮询超时与队列高水位。SDK 时间戳保持原值，不默认视为曝光时间或 Unix 时间。主机单调时钟统计仅覆盖接收后的应用排队/处理边界，不包含曝光、USB、DDS 或消费方延迟。`processed` 只表示探针排出并释放帧，不表示图像算法已经处理或 ROS 消费者已经接收。

程序正常有限时长结束会释放帧并停止 Pipeline；运行时长不包含 SDK 打开和停止操作，这些原生调用尚无进程外强制超时。设备错误以非零退出报告。探针 JSON 不输出序列号，但 SDK 自身日志可能包含设备信息，真机日志必须留在受控环境。单路探针不实现自动重连或彩深配对。新增的 `capture-service` 具有独立进程监督、有限重试、显式 session 和真实 SDK 标定；它仍不提供跨相机硬件同步或业务录制器。正式启用应选择 Rust 后端并完成实际消费验收。

## 从探针到正式后端的验收门槛

1. 在实际 SDK/固件组合上验证单路持续出帧、实际尺寸/编码/尺度、内存稳定和停止/拔线行为。
2. 增加接收帧号推进、重复帧、间隙和配置 FPS 的真机证据，区分应用淘汰与设备/传输丢帧。
3. 单独验证彩深双路生命周期及帧配对语义。
4. 扩展已有单路 ROS 实验的双路采集、真实 TF 和正式生命周期绑定，保留 YUYV/毫米深度、时间映射、CameraInfo 和 QoS 契约，复用现有消费侧验收。
5. 与同输出、同消费者的现有驱动比较全栈 CPU、RSS、P99 和恢复行为。仅 SDK 采集的探针不能与含 ROS/DDS 发布的驱动直接比较性能。
