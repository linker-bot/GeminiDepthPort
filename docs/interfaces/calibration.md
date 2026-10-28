# 标定快照接口

标定导出只订阅已运行采集容器的 CameraInfo 和静态 TF，不创建第二个 SDK
Context/Pipeline。快照描述相机内部深度光学坐标到彩色光学坐标的外参，
不包含相机到机器人、世界或手眼标定。

## 导出与适用性

相机组按当前非草稿 generation 运行后，在管理容器执行：

```sh
docker compose exec -T web geminidepthport calibration-export
```

控制端持有操作锁，核验配置、实际容器实例与出流，采集整组 CameraInfo/静态 TF，
再次复核绑定后原子保存到 `state/calibration/<snapshot_sha256>.json`。
失败不会导出部分成功，命令仅返回摘要；管理容器不需安装 ROS。

快照包含真实话题、光学帧、参数和标定，应通过受控文件通道交给消费者。
消费者开录前应核对 generation、配置摘要、档位、设备绑定及标定版本。
任一相关字段变化都不能继续使用旧快照；摘要用于一致性校验，不是身份认证。

## 几何契约

- CameraInfo 必须与实际尺寸、光学帧一致，焦距有效，所有数值有限。
- 支持 `plumb_bob`（5 系数）、`rational_polynomial`（8 系数）和 `equidistant`（4 系数）。
- K 底行为 `[0,0,1]`；拒绝非全帧 ROI 和未适配 binning。
- 双路启用时必须有深度到彩色外参；单流不伪造另一流标定。
- 平移单位为米，四元数顺序为 `x,y,z,w`，应用方向为
  `p_color_m = R_depth_to_color * p_depth_m + translation_m`。
- 毫米深度反投影前除以 1000；不能把米平移直接加到毫米坐标上。

静态变换通过标准 TF 图查询 `lookup_transform(color_optical, depth_optical)`。
动态 `/tf` 不在此接口支持范围；变化外参需要按图像时刻另行查询并验收。

## 与原生快照的区别

`calibration-export` 是 ROS 观测标定；Rust 后端的 `capture-snapshot` 保存同一 SDK
会话的不可变证据。二者来源和身份不能互换。录制使用哪个快照、如何解释计数或毫米
表示，应遵循[快照契约](../contracts/capture-contract-v1.md)及[录制保护](../contracts/recording-protection-v2.md)。

结构校验不证明物理精度；快照保留 `physical_accuracy_verified=false` 和
`robot_hand_eye_included=false`。实际距离、畸变和重投影误差需独立验收。
