# 消费方通信、深度和时间验收

`runtime/acceptance.py` 是独立 ROS 消费探针，不创建 SDK Context/Pipeline，不重新打开 USB 设备。它通过实际订阅 Image 与查询 DDS 发布端点，检查消费方真实可见的数据。工具输出 JSON 和退出码；退出码 0 只表示本次指定检查范围通过，不代表完整标定、原始深度几何、曝光同步或端到端录制验收通过。

## 使用方式

在目标 Linux 主机、与预期消费方相同的 ROS/RMW/通信域环境中执行。采集容器内部自检通过不能代替消费方环境的探针。

```sh
python3 runtime/acceptance.py --spec /受控目录/acceptance.json --validate-only
python3 runtime/acceptance.py --spec /受控目录/acceptance.json --seconds 5 --discovery-seconds 3
```

`--validate-only` 不加载 ROS，不接触硬件，只报告 `spec_valid` 与 `hardware_verified=false`。正式探针的发现窗口和采样窗口分离；只保存小型时间和参考深度记录，不缓存像素帧。每路独立创建与消费要求相符的 RELIABLE 或 BEST_EFFORT、VOLATILE 订阅，并查询实际发布端点。RELIABLE 消费要求必须由单一 RELIABLE 发布者满足；未知策略、重复发布者或缺少发布者失败。CameraInfo 的 QoS 在正式生成版本的健康探针中独立检查；完整标定另用标定快照接口验收。

下面只有合成名称、合成尺寸、合成距离和示意阈值，不能直接作为现场验收设置。所有实际话题、坐标系、时间映射、阈值和参考测量留在受控文件中，不提交、不附入公开报告。

```json
{
  "version": 1,
  "streams": [{
    "topic": "/synthetic_sensor/depth/image_raw",
    "encoding": "16UC1",
    "width": 2,
    "height": 2,
    "frame_id": "synthetic_depth_optical_frame",
    "required_reliability": "reliable",
    "min_frames": 2,
    "max_gap_seconds": 0.1,
    "max_offset_seconds": 0.2,
    "freshness_seconds": 0.2,
    "time": {
      "source": "device",
      "common_clock": "unix",
      "offset_ns": 1000000000,
      "mapping_evidence": "measured"
    },
    "depth_unit": "mm",
    "depth_alignment": "raw",
    "depth_reference": {
      "method": "known_distance",
      "x": 1,
      "y": 1,
      "distance_mm": 1000,
      "tolerance_mm": 10
    }
  }]
}
```

每条彩色流采用相同公共字段，`encoding` 改为 `yuyv`，移除全部 `depth_*` 字段。按实际启用列表加入全部流，不固定数量。关闭的流不应加入验收；不允许把缺少必需流的规格当作整组验收。

## 深度尺度与几何

探针检查实际 `16UC1` 编码、尺寸、frame_id、行步长、总载荷和 `is_bigendian`，按大小端读取指定像素并跳过行填充。零值视为无效，不参与参考距离中位数，且有效样本必须达到 `min_frames`。原始数值中位数必须接近已知毫米距离；仅写 `depth_unit=mm` 不会使检查通过。

参考平面和所选像素必须由操作者在受控环境中确认：距离应是沿相机深度轴的真实距离，参考面稳定、无遮挡，容差依据设备误差选取。单点已知距离仅能发现明显尺度错误，不能证明全视野精度、非线性、距离范围或空间对齐。需要在多个距离、多个位置重复测试并保留受控证据。

`depth_alignment=raw` 是受控声明；仅凭一个 Image 不能证明驱动未做几何变换。结果始终保留 `depth_alignment_verified=false`，必须结合生效驱动配置、CameraInfo、外参及几何参考测试单独验收。严禁把伪彩色图或归一化深度作为正式深度输入。

## 时间来源与映射

`time.source` 允许 `device`、`host`、`global`，必须明确声明；`common_clock=unix` 规定与探针接收端 Unix 系统时间比较。映射公式为 `公共时间纳秒 = header.stamp 纳秒 + offset_ns`。`mapping_evidence=measured` 表示已完成受控测量；工具不会替操作者证明该证据真实性。`driver_host_clock` 仅允许 `source=host`、零偏移，且仍需确认驱动确实使用该主机时钟。

探针检查时间戳非零、纳秒范围、严格递增、相邻帧间隔、每帧映射后与接收时刻的偏差，以及最后一帧的接收新鲜度和源时间新鲜度。设备时间重置或重连应使原检查失败，不能自动重写映射掩盖异常。长时间漂移、跨主机时钟同步和机器人状态时间对齐需扩大采样窗口并独立验收；本工具只支持固定偏移，不假装完成频率漂移校正。

所有结果声明 `hardware_exposure_sync_verified=false`。同一时钟、帧率相近或采样通过均不证明硬件曝光同步。

## 通信矩阵与输出契约

公开运行兼容档仍以项目声明的 Humble/Fast DDS/同机 UDP 为准。其他 ROS 发行版、不同 RMW 或跨主机组合需要在实际消费方环境执行本探针，再完成 CameraInfo、TF、录制与后处理往返。`cross_distribution_compatibility_certified=false` 始终保留，单次收到图像不构成跨发行版长期兼容保证。

正式 JSON 包含 `version`、`kind`、检查范围 `scope`、总 `ok` 和按 `stream_index` 排列的结果。每路包含 `ok`、稳定 `errors`、有效帧数和 QoS 观察状态；不会输出话题、frame_id、设备标识、绝对时间、时间映射或像素测量值。规格错误、ROS 缺失和运行异常只返回稳定错误分类，不回显路径或异常原文。ROS/DDS 本身可能写诊断输出，整个命令输出仍按受控运行记录管理。

主要失败分类：`QOS_INCOMPATIBLE`、`PUBLISHER_COUNT`、`INSUFFICIENT_FRAMES`、`ENCODING_MISMATCH`、`PAYLOAD_LAYOUT`、`FRAME_MISMATCH`、`STAMP_NOT_ADVANCING`、`STAMP_GAP`、`TIME_BASE_OFFSET`、`SOURCE_STALE`、`RECEIVE_STALE`、`DEPTH_REFERENCE_INVALID_SAMPLES`、`DEPTH_SCALE_REFERENCE_MISMATCH`。消费方应根据失败分类阻止正式开始，并保留明确的异常状态。

## 交付边界

自动化测试只用合成消息、虚拟时钟和模拟 ROS 图谱，覆盖大小端/行填充、错误尺度、零深度、冻结/跳变/陈旧时间戳、QoS 不匹配和真实探针调用路径。没有因此宣称物理三机、USB 带宽、跨发行版、标定或长时间采集已通过验收。
