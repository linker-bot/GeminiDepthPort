# 三机服务与消费方集成

默认 inventory 是三个未绑定、禁用的 schema v2 RGB-D 槽位；首个型号为 Gemini 335L，另两个为 Gemini 2。槽位的名称、命名空间和帧前缀是公开合成模板，不代表部署位置。用户须绑定真实设备并填写对应固件支持的彩色/深度尺寸、帧率、时间来源和健康策略，再逐台实际预览确认。不同数量的启用设备仍由 schema v2 支持；默认三槽不代表未经确认自动打开三台设备。

## 接入顺序

1. 通过 Web 保存配置并逐台预览、检查档位。原生 YUYV 镜像能力与补丁摘要不符时拒绝预览；实际图像和 CameraInfo QoS 不符时拒绝确认。正式采集与调试预览互斥。
2. 停止调试预览，生成版本并启动相机组。只有全部启用流通过实际检查才报告整组就绪。每台设备由一个驱动持有，消费方只订阅 ROS 数据。
3. 在管理容器执行 `geminidepthport calibration-export`。控制端锁定配置并核验当前非草稿版本、实际运行实例与出流，再从已运行采集容器内订阅 CameraInfo 和静态 TF，验证后保存私有快照。命令仅返回允许公开的摘要，不输出标定数值、设备身份或话题绑定。
4. 在实际消费方 ROS/RMW 环境按 [acceptance.md](../validation/semantics.md) 执行全部必需流的验收。补齐已知距离测量、时间映射和实际通信证据，不能仅以驱动容器内部自检代替消费环境验证。
5. 正式录制前获取录制保护并核对配置版本与标定适用性。明确结束或取消后释放；请求超时或调用方崩溃时先确认实际录制状态，不自动解除保护。
6. 完成录制容器与后处理往返，并验收断流、设备重连、管理服务重启和时间回退。业务录制有效性由消费方根据保留的质量与保护事实判断。

## Rust 后端补充

可选常驻后端见 [部署说明](rust-backend.md)。它沿用相同 Image/CameraInfo 话题及录制保护入口，增加 frame_metadata 与 rgbd/pair_metadata；严格消费者应按显式 session 和各自 stamp 关联，不能仅靠两路到达顺序。

Humble 的 `ROS_LOCALHOST_ONLY=1` 会在 XML 配置之后追加 SHM transport。Rust 后端与消费容器使用 `ROS_LOCALHOST_ONLY=0`，同时加载仓库 `runtime/fastdds.xml`：仅启用 UDPv4、显式白名单 `127.0.0.1`、禁用内建 transport。这样仍限制为本机通信，并使私有 IPC 的跨容器大帧交付可重复验收。两端必须同时使用该组合；只改一个环境变量不能等价替代完整通信配置。对应行为见 [Humble RMW 的 participant 初始化实现](https://github.com/ros2/rmw_fastrtps/blob/humble/rmw_fastrtps_shared_cpp/src/participant.cpp#L151-L160)。

完整配对观察、标准 ROSbag2 读回和源时间检查见 [消费侧验收](../validation/consumer-acceptance.md)。这些公开工具不读取其他业务项目配置，也不代表其专用录制和后处理程序已认证兼容。

## QoS 与通信边界

schema v2 将彩色、深度及各自 CameraInfo 的 Orbbec QoS 参数显式生成为 `DEFAULT`。在固定官方驱动中它表示 RELIABLE/VOLATILE；字符串 `RELIABLE` 不是该驱动支持的 profile，不能依赖错误后的回退。实际端点必须恰好一个且满足可靠订阅，不能仅检查 YAML。RELIABLE 发布仍可供 BEST_EFFORT 预览订阅。

当前公开兼容档为 Humble/Fast DDS/同机 UDP。不同发行版、不同 RMW、跨主机均须在目标环境验证发现、实际大帧收发、QoS、CameraInfo/TF 和完整录制链路；不提供未经实测的兼容承诺。

## 私有信息边界

实际设备绑定、话题及光学帧、ROS 通信域、标定、时间映射、验收规格、配置版本和运行证据只保存在受控配置与 Git 忽略的 state/work 中。不得复制其他项目的配置到公开示例、测试或报告。公开测试仅用合成设备、图像和时钟，公开结果使用允许字段生成。

标定结构有效不代表物理精度，单点深度尺度通过不证明全视野准确，时间检查通过不证明硬件曝光同步。离线测试、模拟 ROS 图谱和 Compose schema 通过均不等于真实三机持续运行验收。
