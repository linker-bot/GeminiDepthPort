> 当前默认是三机 schema v2 + Rust 管理服务，部署步骤以 [Compose 部署](COMPOSE.md) 和 [Rust 管理链路](rust-management.md) 为准。下面保留旧版双机参数与验收背景，不代表当前默认档位。

# 头部 Gemini 335L + 腕部 Gemini 2：Linux 双相机服务

默认通过 [Compose 一键部署与开机自启](COMPOSE.md) 部署，本页保留相机参数、机器人 TF 和真机验收细节。

**Web 管理界面已加入：识别绑定、参数编辑、彩色/深度预览、档位确认和启动检查，参见 [WEB.md](../WEB.md)。**

本版替代之前的三至四台通用模板，只维护两台必需设备。部署环境为同一台原生 Linux 主机、USB/libuvc、Docker Engine、ROS 2 Humble 容器。两个容器共用一个镜像，各自使用独立配置、序列号和健康检查。

- `head`：Gemini 335L，服务 `camera_head`，启动文件 `gemini_330_series.launch.py`，ROS namespace `/head_camera`。
- `wrist`：Gemini 2，服务 `camera_wrist`，启动文件 `gemini2.launch.py`，ROS namespace `/wrist_camera`。Gemini 2 与 Gemini 2 L 不同，本版不接受混用启动文件。

两台默认都必需，缺任一设备时不能报告整组就绪。取消了第三、第四台槽位和 enable 命令。

## 配置与型号适配

编辑 `inventory.json`，再由 `geminidepthport render` 生成：

- `config/head/camera.yaml` 与 `config/head/health.json`。
- `config/wrist/camera.yaml` 与 `config/wrist/health.json`。
- `compose.yaml` 和配置快照 `render-lock.json`。

型号和 launch 文件由角色固定匹配。配置工具校验的是配置里的角色/型号；它不能仅凭序列号证明实物型号。部署前必须用官方枚举工具确认实物型号、固件与序列号，再贴标签并绑定。

本次检查固定源码 commit `c153462518ad674650bafa4464fda72c27ab797a`。此版本的 Gemini 2 launch **没有 config_file_path 参数**，也没有 enumerate_net_device、enable_left_ir、enable_right_ir 参数。上一版直接传 YAML 路径的入口不能原样用于 Gemini 2。新版入口读取每台 YAML，按安装镜像中的对应型号 launch 声明检查每个键，再转为独立 argv 参数执行 ros2 launch，两种型号使用同一套入口。布尔值规范为 true/false，带空格的 depth_work_mode 保持一个参数，不经过 shell 拼接。

YAML 是入口工具维护的平铺启动参数文件，不是 ros__parameters 嵌套文件。两个 namespace 与 camera_name 同时赋值，避免节点和 TF 不一致。

初始采集配置是待真机确认的起点：

- 头部：彩色 1280×720 / 15 FPS / MJPG，深度 640×480 / 15 FPS / Y16。先控制带宽，再依据观察距离、画质和推理负载调整。
- 腕部：彩色 640×480 / 30 FPS / MJPG，深度 640×400 / 30 FPS / Y16，depth_work_mode 为 Unbinned Dense Default。优先提高更新频率，具体曝光和处理延迟要实测。
- 均关闭点云、IR 和 IMU，使用 standalone 独立采集，关闭深度对齐。Gemini 2 的单路 IR 参数是 enable_ir=false，不套用 330 系列的左右 IR 参数。
- 彩色队列上限设为 1；这有助于控制队列积压，但不是端到端低延迟保证。

Gemini 2 官方规格列有 640×400 深度，不能把上一版通用的 640×480 深度直接沿用。实际可用的分辨率、格式、帧率仍以所选固件及 USB 连接下枚举/试采集为准。不得将配置起点当作已通过设备验证。

本版只支持原始彩色/深度健康检查，强制 depth_registration=false。两路分辨率不同，不能直接按同一像素索引取深度。抓取应用应使用 CameraInfo 和彩色/深度外参做投影；如需 D2C 对齐，必须同时适配健康检查中预期的实际输出尺寸，并重新验证选定档位。开启点云等额外输出时也需要增加对应检查。

## Linux 准备与构建

推荐原生 Ubuntu 22.04/24.04 Linux、rootful Docker Engine、Compose v2.20+。宿主机不需要安装 ROS 或 Python；直接开发管理服务需要 Rust 1.85+。macOS/Windows Docker Desktop、rootless Docker 不作为本模板真机目标。

在解压目录执行：

```bash
cargo build --locked --release -p geminidepthport
./target/release/geminidepthport build
```

共用镜像 `local/orbbec-head-wrist:c153462518ad-web-v3`，与旧模板标签区分，包含新版入口及预览桥接程序。构建需要 GitHub、Ubuntu 和 ROS 包源；离线现场提前构建/导入镜像。SDK/固件兼容性和 x86_64/arm64 支持需在目标平台确认。源码已锁 commit，基础镜像和 APT 包尚未按 digest/版本锁定；正式发布建议锁镜像 digest，保留构建产物。

从镜像导出匹配的 udev 规则，安装到 Linux 主机：

```bash
IMAGE=$(./target/release/geminidepthport image-ref)
CID=$(docker create "$IMAGE")
docker cp "$CID:/opt/host-udev/99-obsensor-libusb.rules" ./99-obsensor-libusb.rules
docker rm "$CID"
sudo install -m 0644 ./99-obsensor-libusb.rules /etc/udev/rules.d/99-obsensor-libusb.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
```

重新插拔相机。用 `lsusb -t` 确认两台均工作于预期 USB 速度，优先分配到不同 USB 3 控制器。带电源 Hub 不能增加上行带宽。腕部线缆需预留运动范围、固定应力释放点并验证整个工作空间下是否断流；图像健康无法替代线缆机械验证。

工具会报告 usbfs_memory_mb，不自动修改内核设置。官方多相机文档以 128 MB 为参考值，应按实测和内存情况调整：

```bash
cat /sys/module/usbcore/parameters/usbfs_memory_mb
# 确认需要后由管理员执行，以下为临时设置示例
# echo 128 | sudo tee /sys/module/usbcore/parameters/usbfs_memory_mb
```

## 首次配对与启动

先关闭其他 Viewer、宿主机采集进程和旧容器，避免争抢设备。本项目 devices 命令只检查同一 Compose 项目是否停止，无法排除外部进程占用。

```bash
geminidepthport scan
geminidepthport devices
geminidepthport bind head 头部335L的实际序列号
geminidepthport bind wrist 腕部Gemini2的实际序列号
```

scan 读取 Linux USB sysfs，报告设备名、序列号、端口、连接速度；devices 在一次性容器中运行官方 list_devices_node。若设备在 sysfs 中不提供序列号，预检会拒绝启动，需要扩展为 SDK 枚举检查，不会自动打开“第一台相机”。

分别在官方工具中枚举/试采集对应档位，退出工具释放设备，再确认：

```bash
geminidepthport confirm-profile head
geminidepthport confirm-profile wrist
geminidepthport validate
geminidepthport render
geminidepthport preflight
geminidepthport up
geminidepthport status
geminidepthport check
```

confirm-profile 是操作者对实际验证的记录，不会执行自动能力探测，也不代表双机吞吐或机器人任务验收完成。初始 REPLACE_* 序列号和 profiles_verified=false 会阻止启动。bind/set 后会清除档位确认，要求重新检查。render 要求本项目全部容器停止；render --draft 仅供离线生成草稿，不能通过 up 启动。

修改采集参数可以编辑 inventory.json，也可使用 JSON 值的 CLI：

```bash
geminidepthport down
geminidepthport set wrist color_fps 15
geminidepthport set wrist depth_fps 15
geminidepthport set wrist color_format '"MJPG"'
# 重新完成单台档位试采集后
geminidepthport confirm-profile wrist
geminidepthport render
geminidepthport up
```

修改配置统一采用维护窗口：down → 修改/验证 → render → up。render-lock.json 会检查配置漂移，但不是安全签名。

## 启动门槛与健康检查

up 依次执行：

1. 检查固定的 head/wrist 角色、型号/launch 匹配、两台序列号唯一、档位已确认、配置快照一致。
2. 检查 Linux USB/udev 挂载、主机规则文件、两台物理序列号各出现一次、Docker、镜像源码标签和 Compose 语法。
3. 在临时容器中校验两种实际安装的 launch 文件，确保全部参数存在。
4. 启动头部并等待实际出帧健康，再启动腕部；每台默认最多等待 150 秒。
5. 两台运行后再次逐台采样，全部通过才报告 Group ready，失败非零退出。

每次健康检查等待 DDS 发现 6 秒、采样 6 秒；对每个启用的彩色/深度流检查配置尺寸、frame 前缀、最近 2 秒收到帧、至少两个不同且非零时间戳，以及最低接收帧率。默认门槛为配置 FPS 的 80%：头部 12 FPS，腕部 24 FPS。它是基本流可用性门槛，不是抓取任务的性能保证。

输出示例 topic：

```text
/head_camera/color/image_raw
/head_camera/depth/image_raw
/wrist_camera/color/image_raw
/wrist_camera/depth/image_raw
```

检查器没有验证图像内容、深度有效像素比例、内参正确性、物理安装位置、实际采集延迟或跨设备同步。接收时间新鲜不代表原始采集没有延迟；时间戳有变化也不代表已经与关节时钟对齐。业务要额外监控数据年龄和处理链延迟，并设定符合任务要求的门槛。

失败时保留已启动实例方便诊断，不自动回滚。应用应以 up/check 的结果作为整组就绪门槛；不能把两个容器都运行当作两个相机都正常。单个容器故障应能独立处理；业务是否允许头部或腕部缺失时降级，需在业务层明确，默认整组不允许缺失。

## 机器人 TF 与时间

命名使用 head_camera、wrist_camera，避免与机器人常用 head_link、wrist_link 重名。相机驱动产生各自内部 TF，机器人描述/标定另行建立以下关系：

```text
base_link → 机器人头部关节链 → head_link
                                     → head_camera_link → 相机内部光学坐标系

base_link → 机械臂关节链 → tool0（或实际末端安装 link）
                                     → wrist_camera_link → 相机内部光学坐标系
```

- 安装 link 到相机 link 的固定变换来自安装测量或标定，不提供猜测的零外参。
- 如果头部有转动关节，base 到 head_link 也必须由关节状态驱动，不能当作固定外参。
- 腕部通常做手眼标定，得到实际末端安装 link 到 wrist_camera_link 的固定外参；base 到腕部相机的位置随关节运动变化。
- robot_state_publisher/TF2 提供动态机器人变换，避免同时由多个节点重复发布同一父子边。
- 把图像/深度变换到 base 时，应查询采集时间对应的 TF；运动过程中取“最新 TF”可能引入位置误差。

两台先使用 standalone，time_domain=global、timestamp_clock_type=realtime，但这不构成两机硬件同步或图像与关节硬同步。必须实测 SDK 时间映射、主机时间、关节状态时间戳、传输延迟和 TF 缓存范围。跨型号硬件同步不作为默认能力承诺。

Gemini 2 的彩色为滚动快门，腕部运动下需要评估运动畸变、自动曝光变化和模糊。先测量再决定曝光限制、照明、运动速度或是否停稳后采集；不要直接套用未经标定的固定曝光数字。工作距离也需结合末端安装偏移和相机有效范围试验，验证近距离抓取目标是否有有效深度。

## 容器网络、权限与恢复

host 网络 + ROS_DOMAIN_ID=42 + ROS_LOCALHOST_ONLY=1，默认供同机消费者使用。消费端也设置相同 domain、localhost-only 和 rmw_fastrtps_cpp，并加载同样的 runtime/fastdds.xml，路径使用消费端实际路径。跨主机部署需要另外配置发现与网络暴露。

容器私有 IPC，Fast DDS 明确只用 UDP，避免 host 网络相同但共享内存不可见的问题；大图吞吐应实测，不能默认等同于共享内存性能。上游设备初始化包含共享锁，本方案私有 IPC 不提供跨容器全局锁，首次由工具顺序启动；同时拔插/重连仍需压力验证。不要运行全局 cleanup_shm 作为日常恢复手段。

USB 挂载 /dev/bus/usb 并授权字符设备 189，以适应 USB 设备号变化后的重新发现。所有容器都可访问该范围内设备，序列号是功能选择而非安全隔离；适用于可信专用机器人主机。未使用 privileged，capabilities 全部移除、禁止提权、根文件系统只读；当前以容器 root 运行。非 root 模式需另行验证 GID 和 SDK 文件权限。

日志走 Docker 轮转，临时 ROS 文件位于 tmpfs，没有持久化录像。需要数据采集时单独规划存储卷、容量与保留策略。

```bash
docker compose -p orbbec-multicam logs --tail 100 camera_head
docker compose -p orbbec-multicam logs -f camera_wrist
# 故障诊断后仅重启对应实例，再复查整组
docker compose -p orbbec-multicam restart camera_wrist
geminidepthport check
geminidepthport down
```

当前正式相机使用 restart: no，由 deploy.sh / systemd 调用 Rust 管理入口顺序启动并检查，避免 Docker 自动并行恢复绕过门槛。管理容器使用 unless-stopped。unhealthy 本身不会触发 Docker 自动重启；运行中恢复仍需依据业务和真机结果处理。

## 真机验收与迁移

原三至四台模板的实际部署先用其原 compose 执行 down，再替换文件和重建镜像。此版本沿用 Compose 项目名 orbbec-multicam，但服务改为 camera_head/camera_wrist；不能让旧采集服务继续占用设备。

至少验证：

- 单台静止出帧，再双台同时采集；短期至少 30 分钟，随后按任务做更长稳定性验证。
- 分别拔插、单容器重启、主机重启、缺一台启动、错误序列号、错误档位；不能串设备或错误报告就绪。
- 腕部在整个运动空间持续采集，记录断流、端到端延迟、深度质量、CPU/内存和 USB 错误。
- 静止及运动状态下检查 CameraInfo、深度投影、TF/手眼标定结果；保存固件、镜像 ID、标定文件与配置快照。
- 两机视野重叠时观察深度质量，确认是否存在相互干扰。

按初始配置估算，解码后 RGB8 + Y16 的 ROS 原始图像有效载荷：头部约 50.7 MB/s，腕部约 43.0 MB/s，两台合计约 93.7 MB/s（750 Mb/s），未计 DDS 开销、复制、CameraInfo、点云等。USB 彩色 MJPG 大小可变，这不是 USB 总线带宽估算。双机仍需测试 UDP 接收丢帧；如负载过大，先降低头部彩色分辨率/FPS，或根据业务降低腕部彩色而保留深度频率。

本机未接相机、未运行 ROS、未完成双机采集镜像构建；管理镜像已构建并完成隔离 HTTP 测试。已完成 57 项配置/模拟健康/型号参数/Web/部署流程测试，以及 Compose、Python、Shell、JavaScript 静态检查，并实际验证浏览器参数保存，不代表真机验收通过。

```bash
pip install -r requirements-test.txt
python3 -m unittest discover -s tests -v
ORBBEC_DEPLOY_DIR="$PWD" docker compose -f compose.yaml --profile cameras config --quiet
```

官方参考：

- [固定驱动源码与支持型号](https://github.com/orbbec/OrbbecSDK_ROS2/tree/c153462518ad674650bafa4464fda72c27ab797a)
- [Gemini 2 启动文件](https://github.com/orbbec/OrbbecSDK_ROS2/blob/c153462518ad674650bafa4464fda72c27ab797a/orbbec_camera/launch/gemini2.launch.py)
- [Gemini 2 规格书](https://www.orbbec.com/wp-content/uploads/2023/04/ORBBEC_Datasheet_Gemini-2.pdf)
- [多相机部署](https://orbbec.github.io/OrbbecSDK_ROS2/en/source/camera_devices/5_advanced_guide/multi_camera/multi_camera.html)
- [Docker 设备权限](https://docs.docker.com/engine/containers/run/)
