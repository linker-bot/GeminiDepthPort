# Rust 采集服务的消费侧验收

这些工具订阅真实 ROS 消息，不打开 SDK 或相机，不修改采集配置。它们验证公开传输契约，不代表机器人业务消费方、物理深度精度、硬件曝光同步或手眼标定已通过验收。

## 两条独立验证路径

`runtime/rgbd_acceptance.py --wire-only` 对官方驱动和 Rust 服务执行相同消费工作：RELIABLE/VOLATILE Image 与 CameraInfo 订阅、尺寸/编码/步长/字节长度、真实标定数值、时间新鲜度/间隔，以及稍后创建的 RELIABLE/TRANSIENT_LOCAL `/tf_static` 订阅。此模式不要求官方驱动拥有 Rust metadata 接口。

共同 wire-only 模式使用 `tf2_ros.Buffer` 接收 `/tf_static` 的所有静态边，再以 `lookup_transform(color_optical, depth_optical, Time())` 求 depth→color 变换，使用原 spec 和 1e-6 数值容差检查。官方驱动经 camera_link 等中间节点的合法树与 Rust 的两光学帧直接边因此按相同标准 TF 语义比较；没有订阅动态 `/tf`，也没有自行重写旋转/平移组合算法。缺失链路最终仍报 `TF_NOT_RECEIVED`。严格 RGBD 模式保留服务约定的直接边检查，两种实现的性能对照必须使用同一份更新后的共同消费源码及摘要。

严格模式再关联两路 Image、CameraInfo、frame_metadata 及 pair_metadata。每路以真实 ROS stamp 关联，配对使用 pair_metadata 指向的两个独立 stamp 与两个源帧号，不制造相同时间戳，也不要求两路源帧号相同。核对源深度尺度、输出 1 mm、归一化标志、选定配对时钟与偏差。`publisher_qos_verified` 只是发布器声明；工具还检查图中可观察的 reliability/durability 和发布器唯一性。Humble 图中 UNKNOWN history/depth 不能当作实际发布器队列证据。

严格服务默认要求 32 位小写十六进制 session_id。两路 frame_metadata 与 pair_metadata 必须一致。默认 `single` 策略拒绝 session 变化；设置 `explicit-restart` 才允许新 session 重置源帧号，退休 session 不能重新进入。序号回退本身不会被解释为重启。所有 Unix 图像 stamp 仍要求严格前进。

## 受控 spec

只从当前目标机、当前 SDK/设备档位获取的标定生成私有 spec，不能使用合成测试矩阵。两份 calibration JSON 使用 ROS adapter 的 `frame_id/width/height/distortion_model/k/d/r/p` 字段；transform JSON 包含实际 depth→color 的 `translation_m[3]`，以及 `rotation_xyzw[4]` 或 `rotation_row_major[9]` 二者之一。工具校验 proper rotation 后按原方向转换，不猜测或反转 TF 方向。

```sh
python3 tools/validation/make_observer_spec.py \
  --color-calibration /private/color-calibration.json \
  --depth-calibration /private/depth-calibration.json \
  --transform /private/depth-to-color.json \
  --namespace /controlled_camera \
  --min-fps 25 --min-frames 100 --max-gap-seconds .2 \
  --freshness-seconds .5 --max-future-seconds .1 \
  --max-pair-delta-us 10000 --pair-clock device \
  --output /private/consumer-spec.json
```

以上数值是命令用法示意，须按目标档位、消费者 SLO 和测量窗口显式确定。`--allow-session-restart` 开启明确 session 重启；`--legacy-no-session` 只用于没有 session 的旧探针，正式服务不应使用。默认不进行全帧 SHA256，避免长稳/性能观察引入额外完整像素扫描；如启用 `--payload-digest`，官方和 Rust 对照必须同时启用。

spec 的 `max_pending` 限制每个关联映射，允许范围 1–4096，默认生成 128。`max_unmatched` 是窗口首尾未配齐和缓冲淘汰的合计容忍量，默认生成 30，须按测试目的调整。未配齐和源序号跳值都报告，不把它们直接宣称为已证实的 SDK/DDS 掉帧数。可为深度 stream 增加 `depth_reference:{x,y,distance_mm,tolerance_mm}` 来检查受控已知距离像素；这不能替代完整物理精度验收。

## 30 分钟观察

在与实际消费者一致的 ROS_DOMAIN_ID、RMW、通信 profile 中运行：

Humble/Fast DDS 的跨容器本机 UDP 验收必须在两端设置 `ROS_LOCALHOST_ONLY=0`，并使用相同的、禁止 builtin transports 且显式将 UDP interfaceWhiteList 限为 `127.0.0.1` 的 XML。Humble RMW 在 `ROS_LOCALHOST_ONLY=1` 时会额外加入 SHM；host network + private IPC 下，可能出现图发现成功但全部用户消息为零。不能只把变量改为 0 而遗漏回环白名单，否则不再是仅本机通信。官方基线的对照容器也必须显式使用相同设置。

```sh
python3 runtime/rgbd_acceptance.py --spec /private/consumer-spec.json \
  --seconds 1800 --discovery-seconds 8 --tf-delay-seconds 2 \
  --summary /private/rust-consumer-summary.json
```

共同性能对照在两种实现上均加 `--wire-only`，独立跑严格 Rust 验收。保持同 SDK 二进制、固件、设备、USB 拓扑、档位、彩深输出语义、消费者 QoS、操作系统和后台负载；不要把采集器不发布 ROS 的计数成绩与官方 ROS 驱动比较。采集和消费者应放入可分别采样的 cgroup；CPU、RSS、峰值、退出状态由外部资源监测器记录，观察器本身不会修改系统调度或功耗设置。

观察器只保留有界关联缓冲、固定长度 age 直方图和计数器。逐路输出接收/关联数量、源序号跳值、时间间隔、active FPS、契约错误与 age。age 是**消费回调接收 Unix 时间减发布的 SDK system Unix stamp**，包含 SDK system stamp 的实际语义及主机调度影响，不能标成曝光到算法延迟。P99 是固定直方图桶的上界，不是精确分位数；超出最大桶时为 null。

图像测量窗口结束后，程序最多再 drain 0.2 秒（且不超过 freshness 预算的一半），只补齐窗口内已接收图像的关联消息，不接收新图像扩展 FPS 窗口。剩余边界缺件与实际缓冲淘汰分别有计数，`max_unmatched` 是显式容忍预算，不能解释成已确认掉帧。

TF 订阅在观察到两路图像后延迟创建，检查静态变换迟订阅接收及数值。此动作应在服务已发布静态 TF 后进行。外部 watchdog 应覆盖 native 发布阻塞或进程挂起；观察器不修改采集进程。

## 私有 contract trace

可加 `--record /private/contract-trace.jsonl --max-record-mib 256`，随后：

```sh
python3 runtime/rgbd_acceptance.py --spec /private/consumer-spec.json \
  --replay /private/contract-trace.jsonl
```

trace 以 0600 创建，只保存契约投影、时间、可选摘要及受控参考像素，不保存原始图像。它包含真实话题关联元数据，仍属私有运行资料，不应提交仓库。回放要求相同 spec 摘要、完整结束记录、逐行长度上限；它重放的是原始接收时刻，不能拿当前时间计算历史图像的新鲜度。此工具不是业务录制格式，也不能证明 rosbag/后处理兼容。

## 真实 ROSbag2 录制、读回与后处理

在具有 `rosbag2_py`、`rosidl_runtime_py`、`rclpy` 的目标镜像运行独立短 smoke：

```sh
python3 tools/validation/rosbag_roundtrip.py \
  --spec /private/short-window-spec.json \
  --bag /private/bag-rust --seconds 5 --max-mib 1024
python3 tools/validation/rosbag_roundtrip.py \
  --spec /private/short-window-spec.json \
  --bag /private/bag-rust --read-only
```

官方基线加 `--wire-only`。短窗口 spec 必须显式使用适当的 min_frames/min_pairs，不应沿用 30 分钟的最低帧数。目录必须不存在，父目录必须存在。程序使用 umask 077，保存真实 CDR 像素/CameraInfo/TF/metadata 到私有 SQLite rosbag2，严格限时不超过 60 秒并限制数据量。为索引保留 10% 预算，结束后再核对实际目录大小；目录达到预算或录制错误会失败，不会静默截断后通过。

静态树消费可在 ROS 环境中独立运行合成检查：`python3 ros/tests/verify_static_tree_consumer.py`。它使用真实 tf2 Buffer 验证带 90° 旋转和平移的两边组合与直接边等价、反向 lookup 数值，以及缺链路失败；不创建发布器、不连接设备，也不提供物理标定证据。

读回使用 SequentialReader 和真实消息反序列化，逐 topic 核对消息数及序列化字节的流式 SHA256。后处理逐帧检查几何、header、标定和 metadata 配对；彩色读取 YUYV 首组像素并执行独立 BT.601 有限范围参考转换，深度读取 LE u16 毫米数据。摘要不输出像素、话题、标定或设备身份。它证明实际 rosbag2 字节录制与离线读回，不证明 ROS 重新发布、业务后处理程序兼容、色彩精度或物理距离精度。图中 QoS 与 TF 迟订阅证据仍由在线观察器提供，不会在离线回放中伪造。

## 可比较的结果

`tools/validation/compare_observers.py` 只接受两份成功的 `standard-wire` 观察结果，拒绝纯计数探针或严格模式混用。每次运行另存私有 manifest，包含实际测得的 `sdk_sha256`、`profile_sha256`、`consumer_workload_sha256`、`platform_sha256`、`trial_id`、`implementation`。前四个摘要必须完全一致，不能复制摘要来掩盖配置差异。

```sh
python3 tools/validation/compare_observers.py \
  --baseline /private/baseline-summary.json --candidate /private/rust-summary.json \
  --baseline-manifest /private/baseline-run.json --candidate-manifest /private/rust-run.json
```

工具仅描述同口径的 FPS、间隔和接收 age 差异，不输出未经资源采样支持的 CPU 收益，也不把一次运行当作统计显著性。应按 A/B/B/A 交替顺序重复，单独报告温度、有效频率、CPU 工作量、内存、错误与长稳结果。

`profile_sha256` 必须涵盖实际 SDK 滤波链、D2C/对齐、深度单位、时间来源、frameset 同步/聚合策略，以及几何与 FPS。仅运行相同 ROS 消费者不等于源头工作负载完全相同；未核对滤波/时间策略的实验只能报告共同订阅口径的观察值，不能据此宣称 Rust 的全栈性能收益。


## 受控真实故障恢复

`tools/validation/recovery_acceptance.py` 只接受显式 `gdp-service-*` 测试容器及 `controlled-service-validation` 合成 generation，必须传 `--execute`。它核对容器后端、实际配置摘要、session、worker PID 与 Linux 进程启动时间，向一个已就绪的 SDK worker 注入 SIGSTOP，要求正式健康检查报告失败。随后等待进程外 watchdog 清理旧 worker、重新打开真实 SDK 并创建新 session，再要求实际 ROS 健康恢复和旧 PID 消失。DDS 旧端点租约未过期时仍保持未就绪，最多在声明的恢复窗口内等待，不伪装健康。

工具不写 USB/sysfs，不修改相机配置，不用于录制中的业务部署。失败清理仅在原 session/PID/启动时间仍匹配时恢复自身注入的 STOP。它验证进程挂起恢复，不能替代实际 USB 拔插、供电故障或所有 SDK 错误场景的验收。

## 发布前无设备检查

CI 构建 `capture.Dockerfile` 并执行实际二进制 ABI/运行清单烟测；另有无 `--init` 的容器 PID1 回归，确认 worker 能在 supervisor 为 PID1 时初始化，而不存在的 SDK 会明确失败。跨容器烟测使用独立私有 IPC/PID、host 网络和 614400 字节合成 Image，逐帧核对 SHA256 与配对 String，防止只通过发现或小消息测试就误判图像交付成功。
