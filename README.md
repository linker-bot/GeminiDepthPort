# GeminiDepthPort


面向机器人三台 RGB-D 相机的独立采集服务和本地 Web 配置工作台。

- **默认三机**：一个 Gemini 335L 槽位与两个 Gemini 2 槽位，位置与话题显式绑定。
- **首次配置**：三个 RGB-D 槽位均未绑定、未启用，不提供虚构档位或标定。
- **部署**：同一台原生 Linux 主机，每台相机独立容器，Web 管理容器提供配置入口。
- **许可**：[Apache-2.0](LICENSE)。社区项目，非 Orbbec 官方产品。

管理链路已迁移为 **Rust CLI/Web 服务 + 原生 Web 前端 + Docker Compose**，配置校验、设备发现、generation、操作保护、生命周期、预览编排和标定快照导出统一使用 `geminidepthport`。正式采集继续使用官方 Orbbec C++ ROS 2 驱动；容器内 ROS 探针和图像预览桥仍为 Python。新增 [Rust 单机采集探针](docs/rust-data-plane.md)独立用于 SDK 实验，尚未接管正式采集。管理迁移细节见 [Rust 管理链路](docs/rust-management.md)。物理相机和 systemd 启动仍需在目标 Linux 主机验收。

## 三相机配置与编排

已提供 schema v2 配置、兼容迁移、不可变生成版本和逐台健康编排，详见[配置说明](docs/configuration-v2.md)与[生命周期说明](docs/generation-lifecycle.md)。[官方驱动的原生 YUYV 发布适配](docs/native-yuyv.md)已加入构建流程，包含来源校验与合成 SDK/ROS 测试。三机 Web 已支持逐台配置、预览和实际出帧确认；新增只读标定快照与消费侧语义验收工具。缺少接口能力或补丁摘要不符的镜像不能启动 schema v2 采集。出帧确认不代表标定精度、深度尺度或端到端真机验收。

## 能做什么

- 扫描 USB 设备，按型号和序列号绑定头部、腕部位置，防止相近型号混绑。
- 调整彩色/深度流、分辨率、帧率、格式、曝光和增益。
- 启动独立调试容器，查看彩色及深度伪彩色预览。
- 保存配置，检查实际出帧、尺寸、帧率、时间戳和新鲜度。
- 逐台启动正式相机服务并检查整组就绪，支持 systemd 开机编排。
- CLI 和 Web 共用操作锁；预览与正式采集互斥；保留最近检查结果。

## 在 Linux 主机部署

完整的管理服务、相机组部署与开机编排说明见 [Compose 与开机自启](docs/COMPOSE.md)。

需要原生 Linux、rootful Docker Engine、Compose 插件和 USB 连接；不要求宿主机安装 ROS。使用本机 `/var/run/docker.sock`，不支持远程或 rootless Docker context。

```bash
git clone https://github.com/soulteary/GeminiDepthPort.git
cd GeminiDepthPort
sudo ./deploy.sh install
```

`install` 会构建管理/采集镜像、安装宿主机 udev 规则，并启用 systemd。首次未绑定相机时只提供管理界面，不自动采集。

打开 `http://127.0.0.1:8765`，依次扫描、绑定、保存参数、逐台启用、填写实机档位、时间来源和健康策略并预览三台相机，并分别执行“检查出帧并确认此档位”。随后点击“检查并启动相机组”，或运行：

```bash
./deploy.sh up
./deploy.sh status
./deploy.sh logs
```

只启动管理界面、不安装规则或开机服务：

```bash
./deploy.sh web
```

首次安装后建议重新插拔设备。相机档位和固件须按实机确认；未绑定设备、未填写有效档位或未通过实际检查均会阻止正式启动。

远程访问建议使用 SSH 隧道，保留回环地址绑定：

```bash
ssh -L 8765:127.0.0.1:8765 用户@机器人主机
```

Web 管理容器通过 Docker socket 管理其他容器，具有主机管理能力，仅供可信管理用户使用，不应直接向网络开放。

## 本地开发 Web / CLI

无需 Node.js 构建前端。宿主机开发需要 Rust 1.85+（镜像构建使用 Rust 1.90）：

```bash
cargo build --locked --release -p geminidepthport
./target/release/geminidepthport validate --draft
./target/release/geminidepthport serve --port 8765
```

离线环境可查看、编辑已有示例配置；识别 USB、真实预览和部署检查需要 Linux/Docker 环境。不要同时启动宿主机版和容器版 Web。

## 配置与代码

```text
inventory.json                 默认三机 schema v2 配置入口
state/generations/             私有不可变配置版本（Git 忽略）
src/config.rs                 配置校验、迁移和档位确认摘要
src/generation.rs             不可变部署版本与 Compose 生成
src/control.rs                操作保护、预检和生命周期
src/server.rs                 本机 Web API、任务与预览编排
src/calibration.rs            已发布标定的校验与快照
src/main.rs                   CLI、Web 和开机启动入口
web/                           原生 HTML / CSS / JavaScript
runtime/                       ROS 启动、图像检查和 JPEG 预览桥接
compose.yaml                   管理服务引导；采集容器按启用列表生成
deploy.sh                      Linux 部署、规则安装和开机编排
systemd/                       systemd 服务模板
tests/                         离线配置、健康检查与 Web 流程测试
```

修改 `inventory.json` 后重新生成，避免手工修改生成文件。运行数据位于 `state/` 和 `work/`，由 Git 忽略；`.env` 包含本机部署路径，也不提交。

## 消费方接入

正式数据保持原生 `yuyv` 彩色和毫米 `16UC1` 原始深度；默认四类 Image/CameraInfo 发布 QoS 使用官方 `DEFAULT`（RELIABLE），健康检查核对实际端点。预览可使用 BEST_EFFORT，正式消费需求仍须单独匹配。

运行相机组后，可在管理容器执行 `geminidepthport calibration-export`，只从已有 ROS CameraInfo 与静态 TF 读取标定，私有快照与当前配置/generation 绑定，不重新打开设备。消费方通信环境另执行 `runtime/acceptance.py` 验证实际消息、QoS、时间映射和已知距离深度参考。详见[标定接口](docs/calibration-interface.md)、[通信与语义验收](docs/acceptance.md)及[集成说明](docs/consumer-integration.md)。

## 开发检查

```bash
cargo test --locked -p geminidepthport
cargo clippy --locked -p geminidepthport --all-targets -- -D warnings
cargo build --locked -p geminidepthport
python3 tests/rust/test_management.py
pip install -r requirements-test.txt
python3 -m unittest discover -s tests -v
python3 -m compileall -q driver_build.py scripts runtime tests
node --check web/app.js
bash -n deploy.sh runtime/entrypoint.sh
ORBBEC_DEPLOY_DIR="$PWD" docker compose --profile cameras config --quiet
```

Node.js 仅用于开发时的 JavaScript 语法检查。Python 为旧实现对照与 ROS 工具测试依赖，管理镜像不安装 Python。CI 执行离线、管理容器与合成 SDK/ROS 检查，不宣称验证真实相机或机器人标定。

## 文档

- [Compose 一键部署与开机自启](docs/COMPOSE.md)
- [Web 配置和预览](WEB.md)
- [Linux 部署与相机参数说明](docs/DEPLOYMENT.md)
- [13900K 级平台性能评估与优化优先级](docs/PERFORMANCE.md)
- [基准测试指标、对照组与验收方案](docs/BENCHMARKS.md)
- [Rust 管理链路](docs/rust-management.md)
- [可选 Rust 常驻采集后端与部署](docs/rust-capture-deployment.md)
- [消费、ROSbag2 与长期验收](docs/rust-service-acceptance.md)
- [Rust 帧包与 ROS 发布实验](docs/rust-ros-output.md)
- [同一 Pipeline 的 RGB-D、真实 TF 与配对实验](docs/rust-rgbd-tf-pairing.md)
- [Rust 迁移路线](docs/ROADMAP.md)
- [验证记录与真机验收](docs/VALIDATION.md)
- [第三方许可与来源](THIRD_PARTY_NOTICES.md)
- [参与贡献](CONTRIBUTING.md)

[初始化前的 Web 原型界面](docs/images/web-console.png)为无相机状态的真实截图，不包含模拟采集画面。

## 开源与上游

本项目新增代码采用 Apache-2.0。上游固定为 OrbbecSDK_ROS2 commit `c153462518ad674650bafa4464fda72c27ab797a`，运行时 SDK、扩展二进制和第三方库保留各自许可证。参见 [NOTICE](NOTICE) 和 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

正式分发镜像前需要核对实际构建产物中的 SDK/扩展版本、第三方许可及对应源代码提供义务；本仓库的主许可证不会替代这些依赖的条款。
