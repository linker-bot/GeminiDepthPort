# 消费方接入

先按[部署指南](deployment.md)安装服务，通过[Web 指南](web.md)完成各启用设备的
实际出帧确认，再启动相机组。每台设备由一个采集进程持有；消费方订阅 ROS 数据，不重新打开设备。
配置模板与设备数量见[配置指南](configuration.md)。

## 接入顺序

1. 在管理容器执行 `geminidepthport calibration-export`。控制端锁定配置并核验当前非草稿版本、实际运行实例与出流，再从已运行采集容器内订阅 CameraInfo 和静态 TF，验证后保存私有快照。命令仅返回允许公开的摘要，不输出标定数值、设备身份或话题绑定。
2. 在实际消费方 ROS/RMW 环境按 [通信、深度与时间验收](../validation/semantics.md) 执行全部必需流的验收。补齐已知距离测量、时间映射和实际通信证据，不能仅以驱动容器内部自检代替消费环境验证。
3. 正式录制前获取录制保护并核对配置版本与标定适用性。明确结束或取消后释放；请求超时或调用方崩溃时先确认实际录制状态，不自动解除保护。
4. 完成录制容器与后处理往返，并验收断流、设备重连、管理服务重启和时间回退。业务录制有效性由消费方根据保留的质量与保护事实判断。

## Rust 后端补充

可选常驻后端见 [部署说明](rust-backend.md)。它沿用相同 Image/CameraInfo 话题及录制保护入口，增加 frame_metadata 与 rgbd/pair_metadata；严格消费者应按显式 session 和各自 stamp 关联，不能仅靠两路到达顺序。

Rust 后端与消费容器必须使用相同的 RMW、domain 和 loopback UDP 配置，包含
`ROS_LOCALHOST_ONLY=0` 及仓库 `runtime/fastdds.xml`。完整设置和原因见
[Humble 跨容器通信约束](rust-backend.md#humble-跨容器通信约束)。

完整配对观察、标准 ROSbag2 读回和源时间检查见 [消费侧验收](../validation/consumer-acceptance.md)。这些公开工具不读取其他业务项目配置，也不代表其专用录制和后处理程序已认证兼容。

## QoS 与通信边界

schema v2 将彩色、深度及各自 CameraInfo 的 Orbbec QoS 参数显式生成为 `DEFAULT`。在固定官方驱动中它表示 RELIABLE/VOLATILE；字符串 `RELIABLE` 不是该驱动支持的 profile，不能依赖错误后的回退。实际端点必须恰好一个且满足可靠订阅，不能仅检查 YAML。RELIABLE 发布仍可供 BEST_EFFORT 预览订阅。

当前公开兼容档为 Humble/Fast DDS/同机 UDP。不同发行版、不同 RMW、跨主机均须在目标环境验证发现、实际大帧收发、QoS、CameraInfo/TF 和完整录制链路；不提供未经实测的兼容承诺。

## 私有信息边界

实际设备绑定、话题及光学帧、ROS 通信域、标定、时间映射、验收规格、配置版本和运行证据只保存在受控配置与 Git 忽略的 state/work 中。不得复制其他项目的配置到公开示例、测试或报告。公开测试仅用合成设备、图像和时钟，公开结果使用允许字段生成。

各项验收的适用范围与未验证项见[验证索引](../validation/README.md)。
