# 验证记录索引

本页只概括已记录的范围；具体产物指纹、方法、失败与限制以链接报告为准。
不同报告的冻结产物不能混称为同一最终版本验收。CI 成功不代表硬件验收。

## 保留的实测记录

- [305/335L 各 30 分钟消费与短 ROSbag2 读回](reports/rust-service-validation-20261028.md)：
  含独立资源窗口、真实管理闭环及 SIGSTOP 恢复。资源/状态窗口分别为 29/28 分钟。
- [四机五分钟并行消费](reports/four-camera-5min-20261029.md)：
  含缺路 frameset 拒绝、USBFS 条件及真实 D2C 溢出失败；不等于生产三机长期验收。
- [录制、管理、质量历史和故障功能验收](reports/capture-completion-functional-20261029.md)：
  含原生 counts 录制、SQLite/MCAP 字节读回、停机摘要与中断观察器回归。
- [同网格 SDK 对齐差异](reports/sdk-grid-differences-20261029.json)：
  记录覆盖率和共同有效像素误差，不认证 SDK 等价或物理精度。

同名 JSON 保留机器可读结果。原始像素、序列号、标定、凭据与部署绑定不在仓库中。

- [SDK 异常退出后首次重开修复](reports/sdk-first-reopen-20261029.md)：
  原 SDK 不变，四台分别通过单次重开；含同镜像失败/成功对照及启用条件。

## 自动检查

CI 定义在 [.github/workflows](../../.github/workflows)：Rust 测试与 Clippy、
Python/前端/Shell 检查、Compose schema、Linux 合成管理生命周期、管理容器
冒烟、固定官方驱动构建、Rust 采集运行清单/ABI 及 ROS 合成跨容器交付。
这些检查不打开真实设备；执行入口见[贡献指南](../../CONTRIBUTING.md#开发检查)。

## 已知限制与待验收

- 旧镜像或未配置 `usb_recovery` 的独立配置仍可能在 SIGKILL 后首次重开无帧。
  新修复需重建 Rust 镜像、重新确认档位并生成新 generation；历史失败记录保留。
- D2C 尚未证明与官方 SDK 等价；真实溢出按契约失败，不截断或伪造完成。
- 最终发布产物的生产规模长稳、真实 USB 拔插/掉电、干净安装、systemd 开机、
  升级回退和业务消费者兼容仍需独立验收。
- 不认证物理深度精度、手眼标定、硬件曝光同步、跨发行版兼容或无内存泄漏。
- 未完成有效官方驱动性能对照，不宣称 CPU、内存或延迟收益。
