# Rust 采集后端接入管理流程

Rust 数据链是显式可选后端。未填写 `capture_backend` 或填写 `official` 时，继续使用原有官方 ROS 驱动；不会因检测到 Rust 程序而自动切换。

## 配置边界

仅 schema v2 可以选择 `capture_backend: "rust"`。需要在 inventory 根对象同时填写完整 `rust_capture` 策略，例如下面的**策略示例**；它不是实机档位、硬件同步或性能承诺：

```json
{
  "capture_backend": "rust",
  "rust_capture": {
    "depth_unit_policy": "normalize-mm",
    "pairing": {"clock": "device", "max_delta_us": 20000},
    "poll_ms": 100,
    "capacity": 4,
    "startup_discovery_seconds": 2,
    "service": {
      "idle_timeout_ms": 2000,
      "startup_timeout_ms": 15000,
      "heartbeat_ms": 500,
      "heartbeat_timeout_ms": 10000,
      "max_restarts": 3,
      "restart_backoff_ms": 1000,
      "shutdown_timeout_ms": 5000,
      "ready_min_pairs": 5
    }
  }
}
```

每台启用的相机需要独立的真实绑定、命名空间、光学帧前缀和实际验证过的彩色/深度宽高帧率。当前常驻 Rust 服务要求同时启用两路；正式彩色为 YUYV，深度为原始未配准 Y16，并通过显式策略验证或归一化为毫米 `16UC1`。`timestamp_source` 必须填写 `host`，对应 SDK system Unix 时间；它不代表设备曝光时间或跨设备硬件同步。

输入参数只接受两路启用标志、宽高帧率、YUYV/Y16 格式、`depth_registration:false` 和四项 `DEFAULT` QoS。曝光、增益、滤波、翻转、旋转、SDK 同步等官方参数当前均拒绝，包括填写一个看似默认的值；不会静默忽略或假装已经下发。USB 后端、曝光/滤波等仍沿用 SDK 自身行为，未实现的设置不能通过管理入口承诺改变。

`gemini_305` 为 Rust 专用型号档案，精确识别 Gemini 305，不把相近型号混绑。选择官方后端时明确拒绝该档案。原有 Gemini 335L/Gemini 2 档案保持兼容；某型号能通过配置校验不等于完成该设备、固件及档位的真机验收。

`shutdown_timeout_ms` 在管理流程中最多 5000 ms，以容纳现有预览容器的有界停止流程。其他 supervisor 限制与常驻服务一致，超限或相互矛盾的 deadline 会被拒绝。

## 构建与不可变生成版本

运行现有 `geminidepthport build` 或 Web 构建操作时，管理层根据后端选择 `capture.Dockerfile`。该文件从公开 OrbbecSDK_ROS2 固定提交 `c153462518ad674650bafa4464fda72c27ab797a` 获取 SDK，校验预期版本为 2.10.6，再构建本仓库 Rust 常驻服务及薄 ROS bridge。不会复制宿主机任意 SDK 目录。对应源码许可、NOTICE 随 SDK 一起保留并记录摘要；这不是完整镜像许可证审计或完全锁定 APT/base-image 的可重现构建声明。

镜像包含 `/opt/gdp-capture/runtime.json`，记录固定公开来源、SDK 版本、许可证文件摘要、SDK/bridge/service 三个二进制摘要及运行文件清单。生成配置时，管理层通过无网络、无设备挂载的临时容器读取该清单，并绑定不可变 image ID。

每个生成版本为相机写入私有 `capture.json`、`runtime.json`，与原有 `camera.yaml`、`health.json`、`binding.json` 一起纳入摘要清单。常驻服务的 generation、状态文件路径、SDK 和 bridge 路径由管理层生成，不能通过官方参数绕过。启动前验证文件摘要和三个实际运行二进制摘要，再打开设备。

Web 预览使用同一常驻服务，JPEG bridge 仅订阅其实际 ROS 输出。确认档位时保留原有真实出帧探测，并额外绑定 image ID 和 runtime 摘要。即使镜像 tag 不变，只要重建后运行产物变化，旧确认也不能用于正式启动，必须重新预览确认。

## 现有管理能力

逐台启动、整组复检、停止、录制保护、CLI/Web 共用操作锁和 systemd 流程保持原有入口。Rust 镜像保留 `/app/health.py` 入口，但先后两次检查私有服务 status 的 generation、配置摘要、session、工作/父进程存活、heartbeat 与最近配对时间；两次检查之间执行 `/app/ros_health.py` 实际订阅 Image，检查布局/尺寸、帧率、时间戳推进、源 Unix 时间的新鲜度（含最多 100 ms 未来容差）及 QoS。session/PID 切换或 heartbeat 冻结会失败；`status.json` 中的在线标志不能代替消费侧健康证明。官方镜像保持原有默认时间策略。

标定导出继续读取已经发布的 CameraInfo 和静态 TF，不重新打开 SDK。深度到彩色外参由同一 SDK 会话的实际帧档案提供，不使用型号常量。预览服务的 TERM 会交给常驻 supervisor，由它有界停止工作进程和 SDK。

CLI `scan` 可用于设备发现；Rust 镜像不包含官方 `list_devices_node`，因此 `devices` 命令会明确要求使用 `scan`。

回退时先停止相机和预览，移除 `rust_capture`，将后端改回 `official`，重新构建/选择官方镜像并完成档位确认。切换后端会使旧确认失效；录制保护存在时不能切换或维护。

## 无设备验证

```bash
cargo test -p geminidepthport
cargo clippy -p geminidepthport --all-targets -- -D warnings
GDP_REAL_DOCKER="$(command -v docker)" cargo test -p geminidepthport \
  --test rust_capture_management
```

新增测试使用合成身份和 mock Docker 检查显式配置、官方参数拒绝、Rust 专用型号、runtime 绑定、私有文件权限和篡改检测。设置 `GDP_REAL_DOCKER` 后，生成的 Compose 还会经过真实 Compose parser；这些测试均不打开相机。

`scripts/capture-runtime/offline.Dockerfile` 仅供受控验证：基于已由同一公开固定提交构建的官方镜像，要求匹配显式 SDK 摘要，再装配本分支构建的服务、bridge 和运行脚本。SDK 仍取自已核验的 base image，不从其他宿主机项目复制；正式公开构建入口仍是根目录 `capture.Dockerfile`。

镜像在无 USB、无网络、只读根目录和 `/tmp` tmpfs 下可运行无设备 smoke：

```bash
docker run --rm --network none --read-only --tmpfs /tmp --cap-drop ALL \
  --entrypoint python3 <Rust采集镜像> /opt/gdp-capture/smoke_image.py
```

该检查验证实际 SDK/bridge/service 字节与源/许可证清单、运行服务的宿主 ABI 兼容性、`--validate-config` 成功且不宣称硬件验收，以及篡改摘要的失败行为；不会打开 SDK 设备。

## Humble 跨容器通信约束

Rust 后端生成的正式服务和预览容器使用 `ROS_LOCALHOST_ONLY=0`，同时必须加载本镜像的 `/app/fastdds.xml`：关闭 builtin transport，显式 UDPv4 且 interface whitelist 仅允许 `127.0.0.1`。因此保持本机 loopback UDP 契约，不把 DDS 开放到外部网卡。消费者也必须使用相同 XML、RMW、domain 和 `ROS_LOCALHOST_ONLY=0`。

该组合用于避免 Humble RMW 在 `ROS_LOCALHOST_ONLY=1` 时隐式添加 SHM 导致 private IPC 容器间“图可见但消息不可达”的问题。不能只改环境变量而遗漏 loopback whitelist。官方默认管理生成行为保留兼容；同口径对照测试需显式配置两侧实际 transport。XML 纳入 runtime 摘要，修改后必须重新构建运行清单及完成档位确认。

## 正式管理验收脚本

`tools/validation/management_acceptance.py` 在目标 Linux 主机运行，输入只能是当前本项目已有的私有 schema v2 inventory。它保留真实绑定与实机档位，不生成标定、不猜型号、不静默删除官方参数；不支持的参数会直接拒绝。脚本显式将 Rust 时间映射声明为 `host`，并清除旧确认，随后只有实际预览确认 API 可以重新记录正向证据。

默认只建立一个不存在的新私有目录并执行 CLI draft validation，不访问 SDK 或设备。真正执行时，先确保其他采集试验已释放所选设备、没有本项目的现存 Compose 实例和录制保护，再在**另一个新目录**显式运行：

```bash
python3 tools/validation/management_acceptance.py \
  --source-inventory /private/current-inventory.json \
  --source-deployment-root /private/current-project \
  --workspace /private/new-management-acceptance \
  --manager-binary /private/build/geminidepthport \
  --image local/gdp-rust:service \
  --policy-file /private/rust-capture-policy.json \
  --execute --devices-released
```

策略文件是 `rust_capture` 对象本身；若源 inventory 已包含完整策略，可省略该文件。脚本依次调用真实 Web preview/start、读取实际帧、confirm、preview/stop，再通过公开 CLI 完成正式 generation/up、实际健康、标定导出、录制保护阻止 CLI 停机和 Web 预览、错误凭证拒绝、健康维持、正确释放及 down。它不会直接写入 `profiles_verified:true`，也不会为启动而伪造确认摘要。

凭证只保留在内存并经 stdin 释放，不写日志。所有库存、运行输出和标定快照留在 0700/0600 私有工作区，终端仅给出脱敏阶段结果；失败时不宣称正式管理通过。清理只使用本次取得的公开控制流程，不删除保护记录或接管已有 Compose 项目。该脚本也不替代独立消费者长稳、物理精度或业务录制兼容验收。

## 多机 USBFS 缓冲与失败诊断

当 SDK 报 `UVC_ERROR_NO_MEM` 并提示增加 usbfs buffer 时，在宿主机读取
`/sys/module/usbcore/parameters/usbfs_memory_mb`。四机实测中，16 MiB 上限导致
第四路启动退出；管理员显式调到 256 MiB 后，四路均可启动。该值是本次
640×480 彩色、640×480/640×400 深度、30 FPS 组合的实测设置，不是所有档位
的固定容量公式。

```sh
cat /sys/module/usbcore/parameters/usbfs_memory_mb
sudo sh -c 'printf 256 > /sys/module/usbcore/parameters/usbfs_memory_mb'
```

此命令即时修改当前内核参数，不写永久配置。项目不会从采集容器自动修改
宿主机参数。正式部署仍按逐台启动、实际就绪后启动下一台，再进入并行采集。

工作进程失败时，采集服务会尽力在既有私有状态目录写入
`failure-<session>.json`：0600 文件，包含会话、最多 4096 字符的错误链与截断
标志。SDK 错误可能包含设备身份，文件仅供私有诊断；公开 stderr 与状态继续
使用脱敏错误码。诊断写入失败不会替代原来的采集错误，也不会解除录制保护。

多相机 `/tf_static` 有多个独立发布者。消费验收与 ROSbag 工具统一使用有界
100 条订阅历史，避免迟订阅期间其他发布者的样本被深度为 1 的读队列淘汰。
发布器的可靠性、持久性和历史设置保持原约定。
