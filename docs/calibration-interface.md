# 标定快照与只读 ROS 采集接口

标定读取只订阅已经运行的驱动发布的 `CameraInfo` 和 TF，不创建 SDK Context/Pipeline，不启动第二个设备访问进程。这里的 D2C 是深度光学坐标到彩色光学坐标的工厂外参；不包含相机到机器人、手眼或世界坐标标定。

## 管理 CLI

在相机组已按当前非草稿 generation 运行后，在管理容器执行 `geminidepthport calibration-export`。命令在共享锁中核验配置、实际运行实例和出流，通过现有采集容器只读订阅整组标定，再复核绑定并保存快照。无须在管理容器安装 ROS，也不会新开 SDK 设备。失败不导出部分成功；成功仅打印允许字段摘要。

## 私有证据流程

1. 控制端在共享操作锁中读取当前生效 inventory 与不可变 generation，并用 `calibration.capture_request(data, generation_id)` 创建私有请求。必须确认该 generation 正在运行，不能对草稿启动采集。
2. 在同一 ROS 通信域、受支持 RMW 环境执行 `python3 runtime/calibration_export.py --request <private-request.json> --output <private-evidence.json>`。运行环境需要 `rclpy`、`sensor_msgs`、`tf2_msgs` 与 `tf2_ros`。采集器等待本次订阅收到所有启用流的 CameraInfo，并只接收 `/tf_static` 中的静态变换，并通过静态 TF 图查询 `lookup_transform(color_optical, depth_optical)`。
3. 控制端重新核对生效 inventory/generation 未变化，再调用 `build_snapshot(data, generation_id, observations)` 和 `save_snapshot(root, snapshot, data, generation_id)`。共享锁应覆盖重新核对和写入；独立采集脚本本身不操作驱动，不代表整组就绪。
4. 消费方通过受控文件通道读取 `state/calibration/<snapshot_sha256>.json`，并在开录前使用 `validate_snapshot(snapshot, data, generation_id)` 核对适用性。录制元数据可保留版本摘要，完整快照按私有数据管理。

请求、观测和快照均包含受控的话题、光学帧、配置摘要或标定信息，不得提交、公开打印、附入公开日志或上传。CLI 成功只输出相机数量；失败只输出稳定错误分类。文件以原子替换方式写入，文件权限仅所有者可读写。公开反馈使用 `calibration.summary()` 的允许字段。

## 几何语义

每个启用流必须提供与实际档位尺寸和显式光学帧匹配的 CameraInfo。快照保存畸变模型和 K/D/R/P，仅接受 `plumb_bob`（5 个系数）、`rational_polynomial`（8 个系数）和 `equidistant`（4 个系数）；未知模型或系数长度不符明确拒绝。K 底行必须为 `[0,0,1]`。拒绝未标定焦距、非有限数、非全帧 ROI 及未适配的 binning。CameraInfo 的结构有效不证明标定物理精度。

同时启用彩色与深度时必须有 D2C 外参：`source_frame` 为深度光学帧，`target_frame` 为彩色光学帧；平移单位固定为米；旋转为单位四元数，顺序 `x,y,z,w`。应用公式是 `p_target_m = R_source_to_target * p_source_m + translation_m`。原始深度接口的毫米值在投影为三维点时先除以 1000；不得把 TF 的米平移直接当成毫米。单流相机只导出该流内参，不伪造 D2C。

generation、配置摘要、档位、绑定、声明的标定版本任一变化都会使旧快照不匹配。摘要用于一致性检测，不是认证；快照文件和输入证据仍必须受控。`physical_accuracy_verified=false` 和 `robot_hand_eye_included=false` 明确保留验证边界。

## 离线与真机边界

离线可以用 `source=offline-evidence` 的合成证据测试校验、方向、单位和漂移拒绝，不能冒充真实采集。ROS 来源标记也不是密码学证明，不能替代设备绑定、驱动来源或现场监督。

目标机器仍须验证 CameraInfo 与图像同档位、TF 发布与方向正确、动态 TF 的采样时刻适用、镜头畸变处理、深度尺度以及离线 D2C 重投影误差。当前采集器只接受静态 TF 图，不订阅动态 `/tf`；对于会随时间变化的外参，必须增加图像时刻的 TF 查询与误差门槛后才能用于投影。不得把这一接口的结构校验当成完整标定验收或录制就绪。
