# Quality core v1

`quality-core` 是不访问 SDK、ROS、Docker 或主机时钟的 Rust 规则库。身份、帧上下文和单调时间复用 `capture-contract`，不从设备序列号、ROS namespace 或日志推断身份。调用方传入逻辑 stream ID，必须在创建 engine 时固定注册；注册集合、窗口、事件、会话与摘要都有显式上限。

## 输入与状态

`RuntimeQualityPolicy` 包含 `capture_contract::QualityPolicy`，以及单调接收 stale 阈值、滚动窗口容量/时长、事件容量、摘要容量、最多会话数和全部九类规则。每条规则必须显式提供 `open_after`、`recover_after` 和 degraded/failed severity，没有内置业务阈值。条件连续违反达到 open 阈值时打开 episode，后续违反累计计数，连续 Healthy 达到 recovery 阈值才关闭。Unknown 中断恢复连续计数，不会清除现存异常。

稳定违例代码为 `STREAM_STALE`、`TIMESTAMP_REGRESSION`、`PAIR_SKEW_EXCEEDED`、`FRAME_CONTRACT_INVALID`、`QUEUE_OVERFLOW`、`DEVICE_DISCONNECTED`、`SESSION_CHANGED`、`PUBLISH_PARTIAL_FAILURE`、`TIME_MAPPING_INVALID`。

normal 表示当前没有打开的 episode；degraded 表示至少一个 degraded episode；failed 表示至少一个 failed episode。normal 不认证未观测的像素或物理属性。ReportOnly 只报告；FailClosed 的 `continue_allowed` 还要求每路当前有接收新鲜、已通过已声明检查的帧、滚动通过比例达标、会话未最终化。库只输出决策，不直接停止设备。

`record_frame(FrameObservation)` 校验上下文版本、当前会话与固定 stream 集合，检查同会话的 configuration/calibration/snapshot 绑定，评估调用方提供的帧契约及可选像素比例。None 表示未观测，不被填成一个虚构指标。每路只保留有界窗口，不保留图像载荷。`evaluate` 接受外部明确的违例/Healthy/Unknown 证据；只读 ROS 观察器不能把未收到 ROS 消息直接称为设备断开或 SDK 发布失败。

## 三种时间职责分开

- `MonoTime` 是 `capture-contract` 的 session-origin offset。`tick` 在没有新帧时仍能检测接收 stale；非 stale Tick 不算恢复帧。
- 原始 source timestamp 在同 source clock 内按高水位检查重复/回退。回退不会降低高水位，也不会自动建新会话。
- Unix 映射必须显式选择 SDK system Unix、测得的 device offset 或 observed ROS-header Unix。Observed header 不会冒充 SDK 时间；session-monotonic 不能被当成 Unix。未知映射、缺少接收 Unix 时间、溢出、过旧或过未来时间独立构成 mapping 检查失败。

engine 不读取 now。跨会话或回退的单调输入是调用错误，返回稳定代码且不清除状态。源墙钟跳变不影响单调接收 stale，二者可以独立出现。

## 会话与 episode 生命周期

`begin_session` 需要未用过的 `SessionId`，最终化旧统计、清空滚动窗口和新时钟基线，同时打开/累计 `SESSION_CHANGED`。它不关闭旧异常、不把第一个新 source stamp 当成已证明的单调恢复。旧 episode 的 origin 保留旧 session，后续事件的 `at` 标明新 session-domain；事件序号在同 engine 中跨会话连续。

`finalize_session` 只是生成有界的历史摘要，不关闭 episode、不改变异常有效性。最终化后同会话不能继续喂数据，必须显式开始新会话。摘要被淘汰时有计数；所有已用 session ID 仍在显式 `max_sessions` 上限内记录，超过上限返回错误，不遗忘旧 ID 后重新接纳它。

## 查询与事件流

`QualityQueryRequest` 和 `QualityQueryResponse` 的 `version` 均为 1。输入示例：

```json
{"version":1,"query":{"kind":"snapshot"}}
```

```json
{"version":1,"query":{"kind":"events","cursor":null,"limit":16}}
```

事件分页返回 `origin_session_id`、`latest_sequence`、`gap`、事件、`next_cursor` 与 `has_more`。cursor 由最初 session ID 和 after-sequence 组成；不是主机时钟，也不是 SDK 帧序号。不同 feed 的 cursor 或未来序号明确报错。保留窗口丢失前缀时返回非空 `gap.code=EVENT_GAP` 及丢失范围，不能返回一个空列表假装历史完整。活动 episode 独立于有界事件日志，事件被淘汰不会让故障消失。

事件 transition 仅为 opened、recovered；持续异常只更新活动 episode 的累计计数与最后观测时间，不逐帧刷事件环。opening event sequence 同时是该 feed 内 episode 的编号。恢复事件携带最终累计计数，活动期计数通过 snapshot 查询。事件带原始 origin、新观测的 `MonoTime`、违例计数和恢复计数，不携带 SDK 错误原文、设备标识或图像。

## 验证

```sh
cargo test --offline -p quality-core
cargo clippy --offline -p quality-core --all-targets -- -D warnings
```

测试使用纯合成身份与时间，覆盖无帧 Tick stale、恢复滞回、Unknown、源时间/接收时间分离、会话切换不清故障、摘要最终化、EVENT_GAP/cursor 隔离、窗口释放和绑定/像素比例异常。它们不认证真实设备、ROS 通信或物理精度。
