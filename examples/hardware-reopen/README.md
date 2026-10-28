# SDK 异常退出后首次重开

本例用于独立测试容器中的单机故障验证。先完成 [Rust 单机接入](../rust-single-camera/README.md)，
获取真实生成的私有 `capture.json`，再阅读[完整测试契约](../../tools/validation/native-gate-acceptance.md)。
本目录不提供伪造的运行配置、摘要或测试通过报告。

## 无设备检查

在仓库根目录执行；这些命令不打开设备：

```sh
python3 tools/validation/native_gate_acceptance.py --help
cargo build --release -p capture-contract --example gate_protocol
cargo build --release --bin geminidepthport
```

为 Linux 测试主机准备与当前镜像匹配的 `capture-service`、管理 CLI 和协议检查器。
macOS 构建结果不能直接拿到 Linux 执行。镜像构建及运行清单见
[Rust 后端指南](../../docs/guides/rust-backend.md)；USB 恢复需镜像包含已校验 helper，
并重新确认档位、生成配置，不能手填摘要。

## 人工确认后执行

以下是**参数模板，不是无设备计划命令**。操作者需先停止同一相机的生产、预览及其他 SDK
所有者，确认允许对测试 worker 注入 SIGKILL，再在测试主机设置变量。
配置和二进制使用规范绝对路径，输出目录尚不存在；使用下列 `sudo` 调用时，
其父目录须由 root 所有、权限为 0700（工具按实际执行 UID 核对）。
设备及运行绑定保留在仓库外，不把配置或日志提交到 Git。

```sh
sudo python3 tools/validation/native_gate_acceptance.py \
  --base-config "${CAPTURE_CONFIG:?set private generated capture config}" \
  --image "${CAPTURE_IMAGE:?set matching immutable local image}" \
  --service-binary "${SERVICE_BINARY:?set Linux capture-service}" \
  --manager-binary "${MANAGER_BINARY:?set Linux management CLI}" \
  --gate-program "${GATE_PROGRAM:?set Linux gate_protocol}" \
  --root "${NEW_TEST_ROOT:?set new private result directory}" \
  --ros-domain "${TEST_ROS_DOMAIN:?set isolated domain}" \
  --recovery-restarts 1 --execute --devices-released
```

CI 只检查无设备契约，不执行这段命令。没有两个执行标志时工具拒绝执行；它没有完整的
plan 模式。测试不自动停止其他工作负载，不代表生产部署、消费者交付或硬件拔插验收。

成功应同时核对 `ok=true`、`retry_budget=1`、`actual_restart_delta=1`、
`failed_reopen_attempts=0` 及清理结果。发布调用成功不能当作订阅者收到。
失败保留为失败，不通过增加重试或放宽时限改变“首次重开”的含义。

## 多机测试入口

正式管理最多启用三台。额外设备只用于独立测试；
[多机测试工具](../../tools/testing/README.md)支持先生成不调用 Docker 的计划：

```sh
python3 tools/testing/multicamera_runner.py \
  --manifest "${PRIVATE_TEST_MANIFEST:?set actual private test manifest}"
```

该 manifest 引用实际配置、运行清单及消费者规范，不能由本例的未绑定 inventory 替代。
计划通过不授权硬件执行，也不证明规模、长稳、性能或发布验收通过。
