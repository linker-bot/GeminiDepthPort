> 默认工作台现在显示三个 schema v2 相机槽位，逐台绑定、启用并填写档位、命名空间、时间来源及检查策略。出帧确认只代表实际图像与 QoS 检查通过；标定和语义验收另行执行。旧版 head/wrist 配置仍兼容，以下截图与双机操作描述为旧版参考。

> 生命周期后端已支持按启用列表进行配置生成与编排，详见 [修改包 B](docs/generation-lifecycle.md)。设备列表来自 inventory，支持默认三机与旧版配置。启动不再隐式停止已有预览或健康相机。

# Web 相机工作台

默认提供三个相机槽位，采用 Web 引导与按启用列表生成的 Compose 部署，见 [一键部署与开机自启](docs/COMPOSE.md)。下面的宿主机 Rust 方式用于开发调试。

这是连接既有配置工具和 ROS 2 相机容器的本机管理程序，包含真实接口与预览采集实现。前端为原生 HTML/CSS/JavaScript，无需 Node.js 或 npm 构建；管理后端使用 Rust 二进制 `geminidepthport`，ROS/OpenCV/NumPy 仅放在相机镜像中。

## 容器部署

推荐使用仓库根 README 的 `sudo ./deploy.sh install`，或仅运行 `./deploy.sh web` 启动管理容器。Web 仅映射到宿主机回环地址，正式相机由独立容器采集。Web 容器挂载 Docker socket，具有主机管理能力；只供可信管理用户使用。

`deploy.sh install` 还会安装 udev 规则并启用 systemd。管理界面、脚本和开机流程共用 Rust 的受检相机启动路径。首次未绑定设备时，开机流程保留 Web 供配对，不直接启动相机。

## 宿主机直接启动（开发方式）

先按 README 完成 Linux Docker 环境、udev 规则和镜像构建。Web 服务需要以能访问 Docker 的普通管理用户运行；它直接使用宿主机 Docker CLI，不要求宿主机安装 ROS。

```bash
cargo build --locked --release -p geminidepthport
# 使用匹配当前接口的采集镜像
./target/release/geminidepthport build
./target/release/geminidepthport serve --port 8765
```

在该 Linux 主机浏览器打开 `http://127.0.0.1:8765`。端口被占用时可换端口，或用 `--port 0` 让系统分配空闲端口，使用终端打印的实际地址。

从开发电脑访问远端机器人，可以保持服务仅监听 Linux 主机的回环地址，通过 SSH 转发同一个端口：

```bash
ssh -L 8765:127.0.0.1:8765 用户@机器人主机
```

再在开发电脑打开 `http://127.0.0.1:8765`。该服务默认没有远程账户系统，仅供可信本机用户使用，不能直接暴露到公网/局域网。本机 Docker 权限与管理操作能力相当；代码校验 Host/Origin、CSRF token、可编辑字段与固定操作白名单，不接受任意命令或文件路径。

## 界面流程

1. **重新扫描**：读取 Linux USB sysfs 中厂商为 Orbbec 的设备，展示设备名、序列号、USB 端口与连接速度。按明确的产品名称识别 Gemini 335L / Gemini 2，无法确定型号时显示未知，不开放绑定。Gemini 2 L、335Lg、335Le 不会被混认为目标型号。如果固件的 USB 产品名是通用名称，应先用官方 list_devices_node 确认，再适配识别规则；当前没有猜测 PID 或自动降级为任意设备。
2. **选择角色并绑定**：头部下拉列表只包含识别为 Gemini 335L 的设备，腕部只包含 Gemini 2。使用序列号保存身份，不依赖可变的 USB 设备号。
3. **调整并保存**：修改彩色/深度开关、分辨率、FPS、格式，以及彩色/IR 自动曝光、曝光和增益。保存写入 inventory.json；受影响设备的档位确认失效，对应旧预览会停止。仅保存不会重启正式服务，也不会覆盖正在使用的生成配置。多窗口的旧配置会被 revision 检查拒绝，需重新载入。
4. **启动预览**：先停止正式服务。启动所选角色的独立临时容器，用保存的 YAML 启动型号对应驱动，订阅实际 ROS Image，输出彩色 JPEG 和深度伪彩色 JPEG。可以逐台启动独立预览并切换设备查看。
5. **检查出帧并确认档位**：要求当前预览和当前设备配置一致、启用的流都有新鲜图像、尺寸正确，再实际运行 runtime/health.py。尺寸、时间戳、接收帧率、新鲜度检查全部通过后，才保存 profiles_verified=true。修改另一台的参数不会使本台相同配置的预览失效。
6. **生成与启动**：全部启用设备确认后，点击“生成部署配置”或直接“检查并启动相机组”。后者要求先显式停止调试预览，再核对不可变生成版本，再执行 Linux 预检、按稳定逻辑顺序逐台启动并检查健康，以及全部启用设备运行中的复查。失败不会假报整组就绪，已启动容器保留以便诊断。
7. **运行检查/停止**：“检查运行画面”对正式 ROS 图像进行检查；“停止正式服务”不受未保存表单影响，不会先保存参数。此版正式服务运行时不提供浏览器实时预览，需要停止正式服务再进入调试预览。

构建、生成、预检、启动、运行检查和停止作为后台任务执行，界面显示逐行日志。任务一次只运行一个；CLI 和 Web 也通过同一 state/control.lock 排斥生命周期操作。同一部署通过独立文件锁保证只有一个 Web 管理进程，重复启动会返回 `WEB_BUSY`。

## 预览语义

- 采集按配置帧率运行；JPEG 编码最高每流 5 FPS，浏览器按需刷新，不使用相机浏览器权限，也不调用浏览器自带摄像头。
- 深度显示固定为 0.2–3.0 m 的伪彩色范围，超出范围截断显示，无效深度为黑色；不修改原始深度，不表示该范围均适合测量。16 位深度按驱动默认的毫米尺度显示，32FC1 按米换算；改变深度尺度时必须同步适配转换。
- 处理 ROS 行步长和大小端；图像超过 960 像素宽时仅缩放预览 JPEG，原始采集尺寸保持不变。显示的尺寸和 FPS 来自 ROS 实际接收统计，不是 JPEG 输出速率。
- 超过 3 秒没有新图像时停止展示旧帧，避免把静止截图当成实时采集。预览错误可通过“查看日志”排查。
- 浏览器不再请求状态后约 120 秒，Web 后台回收预览容器；即使 Web 进程崩溃，容器内的租约检查也会在约 125 秒后结束采集。正常停止会先给驱动退出时间，再移除临时容器。
- 临时画面与状态写在 work/web-preview/，仅保存每流最新图像，不进行录像；请不要把这个目录当成采集数据集。

预览会增加 ROS 消费与 JPEG 编码负载。CPU/USB/DDS 资源不足时，健康检查可能失败，应依据真实业务帧率和延迟排查；不能通过降低门槛掩盖问题。图像内容、深度质量、时间对齐与机器人标定仍需另外验收。

## 配置、状态和检查结果

- `inventory.json`：持久配置，唯一编辑入口，Web 与 CLI 共用。
- `config/head`、`config/wrist`、`compose.yaml`：点击生成/启动后产生的正式部署文件。
- `state/jobs/*.json`：检查/部署任务的时间、配置 revision、结果和日志，保留最近 50 个。
- `state/latest-job.json`：管理界面重启后展示的最近结果；中断的任务会标为失败/需检查，不会保持虚假的运行中状态。
- `state/control.lock`：跨进程生命周期操作锁。
- `work/web-preview/*`：临时预览配置、租约、状态和最新 JPEG。

Web 不是常驻 ROS 数据网关，正式相机容器可以在 Web 停止后独立运行。正式相机 restart: no、管理容器 unless-stopped；相机由统一脚本/systemd 顺序恢复。私有 IPC、UDP DDS 和同机 namespace 设计保持适用，详见 README。管理镜像已切换到 rust-v1，需重新构建，不能只替换前端文件。详见 [Rust 管理链路](docs/rust-management.md)。

## 测试与本次验证边界

```bash
cargo test --locked -p geminidepthport
cargo build --locked -p geminidepthport
python3 tests/rust/test_management.py
pip install -r requirements-test.txt
python3 -m unittest discover -s tests -v
node --check web/app.js  # 仅开发检查需要 Node.js，运行 Web 不需要
python3 -m compileall -q .
ORBBEC_DEPLOY_DIR="$PWD" docker compose -f compose.yaml --profile cameras config --quiet
```

历史 Python 原型的离线测试，覆盖配置、型号接口、模拟图像健康、Web 绑定/保存、型号混绑拒绝、旧 revision、操作锁、请求来源/CSRF、任务结果保存、预览命令生成、确认档位调用真实健康检查的流程，以及图像缓冲区行步长/大小端处理。后端 Docker/ROS 调用在这些流程测试中使用替身；预览 JPEG 编码依赖容器中的 OpenCV，未在本机完成实际编码或 ROS 联调。

最终版本还通过实际回环 HTTP 冒烟测试：页面/状态/配置保存返回成功，非法操作、缺少 CSRF 和外部 Host 被拒绝。

浏览器已实际检查页面呈现、头部/腕部切换、参数保存到文件，以及无相机时拒绝预览。测试后恢复了示例参数和未绑定状态，截图不会伪造相机画面。Web 管理运行环境镜像已构建并完成隔离 HTTP 测试，尚未构建双机采集镜像或接入两台物理相机；部署前需在目标 Linux 主机做 USB 识别、真实预览、档位确认和启动检查验收。
