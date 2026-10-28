# 同一 Pipeline 的 RGB-D、真实 TF 与配对实验

`rgbd-probe` 使用一次设备打开和一个 SDK Pipeline 同时启用彩色 YUYV 与 Y16 深度档位。单路 `capture-probe` 保持兼容，不允许通过两个单路 worker 竞争同一设备来伪装双路采集。本实验仍不接管正式 Compose 相机服务或录制保护流程。

## 私有配置与运行

```bash
cargo build --locked --release -p capture-probe --bin rgbd-probe
./target/release/rgbd-probe --config "$PRIVATE_RGBD_CONFIG_JSON"
```

配置文件必须为绝对路径，其设备标识、话题和档位保留在受控环境。JSON 根字段为：

- `version`：1。
- `sdk_library`、`serial`：可信 SDK 绝对路径与显式设备绑定。
- `color`：`width`、`height`、`fps`、`frame_id`。
- `depth`：同样的四项，以及显式 `unit_policy`（`require-mm` 或 `normalize-mm`）。
- `pairing`：`clock`（`device` 或 `system`）与正数 `max_delta_us`。当前探针允许预算不超过一秒；这只是参数边界，不是业务验收目标。
- `seconds`、`poll_ms`、`capacity`：有效测量窗口、轮询超时和整对队列容量。
- 可选 `startup_discovery_seconds`：单独报告的启动丢弃窗口，默认 0。窗口内仍轮询、校验并统计缺帧/超差，不能把它们报告为已发布的有效数据。
- 可选 `ros`：`bridge`（绝对路径）、`namespace`（显式绝对命名空间）、`stamp_source`（只支持 `sdk-system-unix`）。

两路可以使用不同分辨率和帧率，但应用必须接受相应 frameset 完整性和配对结果；不静默改变档位或容差来获得通过。缺少必需字段、未知字段、错误单位或未知时间映射均拒绝启动。

## 配对定义

候选彩色和深度帧必须来自同一次 SDK `wait` 返回的同一 frameset。`RgbdPair<T,U>` 整对持有两路像素与各自元数据，队列淘汰时整对释放，不跨 frameset 拼接，不要求两路帧号相等。

`PairGuard` 检查编码、毫米深度、载荷布局、所选原始时钟的非零值、两路时间差与各自帧号/时间推进。全部检查通过后才原子推进两路状态。缺帧、超差或回退拒绝整对，不能只发布其中一路并报告配对成功。所选时钟在一个 guard 生命周期内保持不变。

`same_frameset` 表示采集端观察到的 SDK 交付关联；时间差预算表示声明时钟上的数值约束。二者均不证明硬件曝光同步，所有证据明确保留 `hardware_sync_verified=false`。接近零的 SDK 时间差也不能据此改成 true。

## 同一会话读取真实标定

首次完整 frameset 的实际 SDK stream profile 提供内参、畸变和深度到彩色外参，不重新打开设备，也不根据型号名称生成矩阵。档位、编码、尺寸和已请求配置必须一致。无法准确映射的畸变模型、非有限数、反射或非正交旋转明确拒绝。

SDK 标定结构按其固定头文件的 packed C ABI 读取；缺少可选标定符号时，原单路采集仍可使用，但 RGB-D ROS 标定发布失败，不回退成虚构数据。真实标定值只发布到受控 ROS 环境，不出现在公开探针摘要中。

## TF 方向与单位

SDK depth profile 的 `getExtrinsicTo(color profile)` 定义：

```text
p_color_m = R_depth_to_color × p_depth_m + t_depth_to_color_m
```

SDK 平移由毫米显式转换为米，旋转按 SDK 行排列解释并校验后转成单位四元数。发布 `/tf_static` 时使用彩色光学帧作为 parent，深度光学帧作为 child。TF 中存储的变换把 child 坐标的数据转换到 parent；`lookup_transform(color, depth)` 应与上述 SDK 方程一致，反向查询必须得到逆变换。

静态 TF 使用 RELIABLE、TRANSIENT_LOCAL、KEEP_LAST(1)，在创建时发布一次，迟加入的订阅者应能获取。它只描述相机内部两光学坐标系；不推导相机到机器人、世界或其他相机的外参，也不证明工厂参数的物理精度。

## ROS 交付与消费侧验收

两路分别沿用 Image、CameraInfo、frame_metadata。新增 `{namespace}/rgbd/pair_metadata` 的有界 JSON：配对序号、两路帧号、各自 ROS stamp、所选原始时钟值、时间差、SDK frameset 来源和未验证硬件同步标志。

保留两路各自时间戳，不把它们强行改成相同值来制造同步。配对证据在两路发布调用均成功后发送，但三次调用并非跨话题原子事务；部分失败会终止实验，消费方仍须检查所有关联消息是否完整到达。

消费侧应按 pair_metadata 中各自 stamp 关联两路图像、CameraInfo 与帧元数据，并核对实际单位、QoS、帧号与时间差。使用迟加入 TF 订阅者，验证正向、反向查询与独立 SDK 已知点转换一致。合成验收加入非单位旋转与非零平移，防止方向或单位错误被单位矩阵掩盖。

报告将启动丢弃、有效窗口、缺彩色/缺深度、拒绝原因、完整候选的时间差、整对淘汰和发布调用分别计数。发布者的 `delivery_verified` 仍为 false；只有独立订阅验收提供交付证据。探针不设置隐式绝对数据年龄上限，消费侧新鲜度与测距参考仍需按私有验收预算另行验证。
