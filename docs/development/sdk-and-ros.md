# SDK 与 ROS 开发参考

核心采集库与 ROS 发布适配分开。正式配置使用[采集后端指南](../guides/rust-backend.md)，
本页用于开发探针和定位边界问题，不作为生产采集入口。

## 模块与所有权

- `orbbec-sys`：显式绝对路径动态加载官方 SDK 的 C ABI。
- `orbbec-safe`：设备、Pipeline 和 Frame 的 RAII，数据借用不超过帧生命周期。
- `frame-core`：布局、深度表示、帧上下文和有界整对队列。
- `capture-probe`：单路探针、`rgbd-probe` 与常驻 `capture-service`。
- `ros-output` / `ros/`：同步发布标准消息的 C ABI bridge。

绑定最初依据 SDK 2.9.3 头文件整理；正式镜像固定 SDK 2.10.6 来源并核对运行二进制。
符号存在不证明完整 ABI 等价；只加载可信库，按实际固件与设备验证。
SDK 句柄不声明未经证明的跨线程 Send/Sync；原生 stop/delete 仍可能阻塞。

## 探针

单路探针由操作者提供设备绑定、精确档位、显式 SDK 路径和有限窗口：

```sh
cargo build --locked --release -p capture-probe
./target/release/capture-probe \
  --sdk-library "$SDK_LIBRARY" --serial "$CAMERA_SERIAL" \
  --stream color --width "$COLOR_WIDTH" --height "$COLOR_HEIGHT" --fps "$COLOR_FPS" \
  --seconds 10 --poll-ms 100 --capacity 2
```

运行前释放相机，不与正式服务或预览竞争设备。SDK 日志可能包含设备身份，需私有保存。
`rgbd-probe` 使用同一 Pipeline 的双路数据；完整参数与 ROS 发布示例见
[capture-probe 参考](../../crates/capture-probe/README.md)。

合成模式只验证布局与所有权 token，不代表真实像素、USB 或性能。
SDK 来源要求已知紧密布局，不根据载荷长度猜测行填充。
深度策略见[深度输出契约](../contracts/depth-output-v1.md)：显式归一化会生成独立毫米缓冲，
不能用修改标签代替数值转换。溢出拒绝整帧。

## ROS 边界

bridge 同步复制并发布 Image、CameraInfo 和帧/配对证据，SDK 缓冲不逃逸生命周期。
多话题发布不是原子事务；部分成功仍必须报告失败，发布调用次数不代表消费端交付。
配置、构建和无设备消息检查见 [ROS bridge](../../ros/README.md)。

内参和深度到彩色变换来自当前会话的实际帧 profile，保留两路各自时间戳与源帧号。
同一 SDK frameset 不认证曝光同步；配对与时间语义见[配对证据](../contracts/pair-evidence-v1.md)。
独立消费者验收见[消费验收](../validation/consumer-acceptance.md)。

## 检查与限制

构建、测试和最低工具链见[贡献指南](../../CONTRIBUTING.md)。
探针结束表示排队数据被排出并释放，不代表算法或 ROS 消费者已接收。
队列容量不包含 SDK 内部缓存；单路探针不提供自动重连。
常驻服务的进程监督、有限重试和会话保护由服务契约定义，不从探针成功推导。

## 许可证与离线组装

采集镜像在构建时收集完整 SDK 许可目录并核对固定许可文本。
[`scripts/capture-runtime/offline.Dockerfile`](../../scripts/capture-runtime/offline.Dockerfile) 的受控上下文也必须包含来自本仓库的
`licenses/orbbec-sdk-v2.10.6/`，放在上下文的 `sdk-license/`，以及匹配的
`capture-runtime/` 脚本。SDK 仍取自已验证的官方镜像，不复制宿主机 SDK。
许可包缺失会阻止组装；完整范围见[第三方说明](../../THIRD_PARTY_NOTICES.md)。
