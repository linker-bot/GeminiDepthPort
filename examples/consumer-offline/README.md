# 消费、录制证据与离线深度

先完成 [官方三机](../official-three-camera/README.md)或 [Rust 单机](../rust-single-camera/README.md)
接入，再按[消费方指南](../../docs/guides/consumer-integration.md)配置 domain、RMW、通信 XML 和 QoS。
真实话题与坐标系取自当前配置及消息，不从示例名称推断。

## 质量与录制上下文

在已安装管理 CLI 的环境设置实际部署根目录和逻辑相机名称：

```sh
geminidepthport --root "${DEPLOYMENT_ROOT:?set private deployment}" \
  quality-current "${CAMERA_NAME:?set configured logical name}"
geminidepthport --root "${DEPLOYMENT_ROOT:?set private deployment}" \
  quality-events "${CAMERA_NAME:?set configured logical name}" --limit 50
geminidepthport --root "${DEPLOYMENT_ROOT:?set private deployment}" recording-context
```

接口与分页/会话规则见[质量契约](../../docs/interfaces/quality.md)。上下文包含快照和运行绑定，
须存放在私有目录。Rust 强保护使用 `recording-context --require-strong`，但读取上下文本身
不取得录制保护；按[生命周期指南](../../docs/guides/lifecycle.md)取得并核对实际保护。
官方观察关联不能标记为 `sdk_frameset`，结构合法也不代表物理精度认证。

## 历史录制转换

[录制适配器](../../tools/recording/README.md)定义 ROSbag2/MCAP 的私有输入配置与证据要求。
必须使用同一次录制的真实快照、时间映射、话题和配对证据，不能补造身份或用摘要
`pair_metadata` 代替完整 `BoundPairEvidence`。原始 MCAP 不会因本例改变解释方式。

先构建无设备离线入口：

```sh
cargo build --release -p capture-observer -p offline-depth
```

录制适配器产出的完整 FrameSource 目录可按[离线格式契约](../../crates/offline-depth/README.md)
校验并导出；以下变量指向已有私有输入和输出父目录。下面的原始导出命令假定输入
已经明确声明为毫米深度；输入仍为 SDK counts 时，校验与原始导出都须显式追加
`--normalize-mm`，使用输入中真实保留的尺度。D2C 直接使用该源尺度，不能同时追加
`--normalize-mm`，也不能把 counts 原始字节改标签称为毫米。

```sh
cargo run --locked -p offline-depth -- \
  --input "${FRAME_SOURCE:?set complete private FrameSource}" --validate-only
cargo run --locked -p offline-depth -- \
  --input "${FRAME_SOURCE:?set complete private FrameSource}" \
  --output "${EXPORT_PARENT:?set private output parent}" \
  --job original --mode both --capacity 2
cargo run --locked -p offline-depth -- \
  --input "${FRAME_SOURCE:?set complete private FrameSource}" \
  --output "${EXPORT_PARENT:?set private output parent}" \
  --job registered --d2c --mode png --capacity 2
```

使用新 job 名称，保留失败的 partial 状态；已有输出不能覆盖复用。
`both` 复用同一份 PNG 编码写目录和 TAR；队列满阻塞上游。
D2C 仅离线运行，消费输入中已选择的关联帧，不重新配对、不在线补洞，输出彩色坐标系 Z。
数值/完整性通过不认证硬件同步、物理精度或业务 episode 有效性。
