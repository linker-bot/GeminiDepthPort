# 管理服务开发参考

`geminidepthport` 是管理 CLI/Web 入口。管理镜像包含 Rust 二进制、Docker CLI/Compose
和部署 shell；ROS 健康与标定工具在采集容器执行，宿主机不需 Python 或 ROS。
操作命令与保护规则见[生命周期](../guides/lifecycle.md)，配置字段见[配置](../guides/configuration.md)。

## 模块

- `src/config.rs`：schema 校验、迁移和确认摘要。
- `src/generation.rs`：配置生成、产物清单与当前版本指针。
- `src/control.rs`：操作锁、设备发现、容器归属和受检生命周期。
- `src/server.rs`：本机 Web、请求保护、任务和预览。
- `src/calibration.rs`、`src/capture_snapshot.rs`：标定及原生快照读取。
- `src/recording.rs`、`src/managed_quality.rs`、`src/quality_history.rs`：保护与质量查询。
- `src/util.rs`：限制权限的原子写入、摘要和有界子进程。

## 本地运行

```sh
cargo build --locked --release -p geminidepthport
./target/release/geminidepthport --root "$PWD" validate --draft
./target/release/geminidepthport --root "$PWD" serve --port 8765
./target/release/geminidepthport --help
```

未指定 `--root` 时使用 `ORBBEC_DEPLOY_DIR`，否则使用当前目录。不要为同一部署同时
运行宿主机版和容器版 Web。开发检查见[贡献指南](../../CONTRIBUTING.md)。

## 持久化与并发约束

`state/web.lock` 保证单一 Web 管理者；变更使用 `state/control.lock` 非阻塞排他锁。
配置写入在原子替换前复核读取 revision，外部编辑导致 `INVENTORY_DRIFT` 时保留外部内容。
这不提供对不遵守文件锁的编辑器的文件系统级 CAS；直接编辑仍应在维护窗口进行。

配置与 generation 使用同文件系统临时文件、fsync 和原子替换。激活失败时保留可能
已被当前指针引用的版本，不为清理而制造悬空指针。序列化摘要兼容既有配置；原始
inventory 字节改变后需重新生成。

预览容器使用配置创建者 UID/GID，HOME/ROS_HOME 位于 `/tmp`。确认绑定当前配置、
镜像产物及固定容器实例，执行探针前续租。后台任务和内部命令有独立进程组及截止时间，
超时或关闭时清理所属后代。进程组约束见[清理契约](../contracts/worker-process-group-cleanup.md)。

`tests/legacy_reference/` 仅为合成回归的行为参考，不进入生产管理镜像。
[Linux 合成测试](linux-management-fixture.md)不访问真实设备或 Docker daemon；
它与真实硬件证据的区别见[验证索引](../validation/README.md)。
