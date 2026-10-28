# Rust 管理链路

`geminidepthport` 是默认管理入口。Web、CLI、Compose 和 systemd 引导共用 Rust 配置、保护与生命周期逻辑。管理镜像只包含 Rust 二进制、Docker CLI/Compose 和安装脚本需要的 shell，无 Python 解释器。

## 范围

- `src/config.rs`：schema v1/v2 校验、迁移、确认摘要及失效处理。
- `src/generation.rs`：不可变配置版本、私有文件、来源摘要、Compose 与原子激活。
- `src/control.rs`：USB sysfs 发现、跨进程 flock、录制保护、容器归属、逐台启动、整组探测和关闭。
- `src/server.rs`：原有前端 API、Host/Origin/CSRF、32 KiB 请求限制、预览租约、档位实际确认、后台任务及受限日志。
- `src/calibration.rs`：发布数据的标定请求、数值/几何检查、配置绑定和私有快照。
- `src/util.rs`：原子落盘、摘要、受限子进程输出、超时及进程组终止。
- `src/main.rs`：同一个二进制的 CLI、Web、开机入口及 HTTP 自检。

默认相机容器保留官方 C++ SDK/ROS 驱动。可显式选择 [Rust 常驻采集后端](rust-capture-deployment.md)，沿用同一 generation、保护和预览入口；两种后端的 `runtime/` ROS 消费工具保持独立。Rust 使用 Docker exec 在采集容器内运行健康与标定探针，宿主机和管理容器不需要 Python/ROS。`deploy.sh` 保留 Linux 安装、udev、systemd 及 Docker 引导操作。

旧 Python 管理模块已从项目根目录移至 `tests/legacy_reference/`，仅作为迁移对照测试的行为参考，不再提供可执行入口。原 `cameractl.py`、`web_server.py`、`deployment_control.py` 调用应分别改用 `geminidepthport`、`geminidepthport serve` 和 `geminidepthport up/boot`。测试通过 `tests/legacy_support.py` 显式启用参考实现，生产 Rust 入口不导入它。

## 使用

```bash
cargo build --locked --release -p geminidepthport
./target/release/geminidepthport --root "$PWD" validate --draft
./target/release/geminidepthport --root "$PWD" serve --port 8765
./target/release/geminidepthport --help
```

不指定 `--root` 时，优先使用 `ORBBEC_DEPLOY_DIR`，否则使用当前工作目录。安装到 PATH 后可以直接执行 `geminidepthport`。首次未配置时 `boot` 保留管理服务并正常返回，`up` 拒绝启动未验证配置。

部署仍执行 `sudo ./deploy.sh install`，仅管理界面执行 `./deploy.sh web`。管理镜像构建只选择 workspace 根包 `-p geminidepthport`，不链接实验 SDK crates。

## 兼容与保护

库存格式、三机公开接口及前端 API 保持兼容。已有 Python generation 可继续校验和读取。新生成的 `.yaml` 文件采用 JSON 表示，属于 YAML 的子集，Compose 和 Python `yaml.safe_load` 都能读取。

确认、迁移和标定摘要沿用 Python 的排序、ASCII 转义与浮点格式，测试包含中文、非 BMP 字符和科学计数法。inventory 原始字节发生改变仍会使 generation 失效，必须重新生成。

Web 进程通过 `state/web.lock` 保证单一管理者；所有变更获取 `state/control.lock` 的非阻塞排他锁。录制保护文件损坏或为悬空符号链接仍阻止变更。关闭按 Docker 归属标签发现容器，即使库存缺失也能执行；归属不明的容器拒绝操作。正式启动不会隐式停止预览或已有设备。配置及 generation 使用同文件系统临时文件、fsync、原子替换和私有权限。

后台任务使用独立 Rust 子进程；构建/启动/其他任务分别有截止时间。内部外部命令各自有进程组，Rust 工作任务接到终止信号后先清理内部命令；外层再执行限时退出。普通 Compose 命令限制为一分钟，构建仍有独立的一小时上限。任务输出保留最近 1200 行，历史保留最近 50 个记录。日志属于本机管理信息，不应公开。

## 验证

```bash
cargo fmt -p geminidepthport --check
cargo clippy --locked -p geminidepthport --all-targets -- -D warnings
cargo test --locked -p geminidepthport
cargo build --locked -p geminidepthport
python3 tests/rust/test_management.py
python3 -m unittest discover -s tests -v
```

Rust 测试包含 47 个由 Python 参考实现生成的合成校验场景、迁移与标定摘要对照、跨进程锁、保护、generation 篡改和进程组超时。黑盒测试执行真实 Rust CLI/HTTP，但以测试 Docker 程序替代真实设备操作。管理侧 Python 仅为开发测试依赖；正式相机容器的 ROS 探针、预览及启动工具仍需要 Python。

可在安装真实 Compose 插件的开发机加 `GDP_REAL_COMPOSE=1` 运行黑盒测试，额外校验三机部署文件；该步骤仍不访问相机。

```bash
docker build -f web.Dockerfile -t geminidepthport-web:ci .
bash tests/rust/smoke_container.sh geminidepthport-web:ci
GDP_TEST_IMAGE=geminidepthport-web:ci python3 tests/rust/test_linux_lifecycle.py
```

容器冒烟使用临时配置、只读根文件系统、非 root 用户，无 Docker socket 或 USB 挂载，检查页面/API、配置原子替换、HTTP 自检及 Python 解释器缺席。以上检查不代表真机出帧、热插拔、标定精度或 systemd 已验收，也没有语言迁移性能收益结论。

复查记录与已修复问题见 [验证记录](VALIDATION.md)。Linux 生命周期测试用合成 sysfs 和 Docker 测试程序覆盖顺序启动、复用、保护和失败分支，不访问真实设备或 Docker daemon。generation 激活失败可能留下未激活私有版本供诊断，不会为清理目录而删除可能已被激活指针引用的版本。

预览容器使用配置创建者的 UID/GID，HOME 与 ROS_HOME 位于 /tmp；私有配置目录和输出不需要放宽权限。预览确认绑定共享通信/镜像配置以及固定容器实例，确认前续租。修复前创建且未确认的预览需重启，已有 generation 与确认摘要格式保持兼容。

库存保存使用读取时的 revision，并在临时文件 fsync 后、原子替换前核对原文件。外部编辑导致 `INVENTORY_DRIFT` 时保留外部内容，调用方需重新读取再操作。该检查不等于对不遵守文件锁的编辑器提供文件系统级 CAS；直接编辑库存仍应在维护窗口进行。
