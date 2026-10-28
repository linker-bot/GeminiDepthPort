# SDK 扩展保留与裁剪边界

采集镜像使用固定官方驱动包中的 SDK 2.10.6。扩展属于独立许可的厂商二进制，
具体条款见[第三方说明](../../THIRD_PARTY_NOTICES.md)。本评估核对固定公开源码和
实际分发目录；未对删除插件后的各型号、固件、架构进行真机回归，**默认保留全部扩展**。

## 版本与证据

二进制目录来自 [OrbbecSDK_ROS2 固定提交](https://github.com/orbbec/OrbbecSDK_ROS2/tree/c153462518ad674650bafa4464fda72c27ab797a/orbbec_camera/SDK)。
加载逻辑参考 [SDK v2.10.6 源码](https://github.com/orbbec/OrbbecSDK_v2/tree/575347ca742d219ef61b1e214e97043f17f48d23)。
源码版本用于依赖分析，不证明预编译包与该源码逐字节一致；`ldd` 也不能列出所有运行时加载的插件。

## 各类扩展

- **`frameprocessor/libob_frame_processor.so`：保留。**
  Gemini 2、305、330 系列的设备/传感器路径注册或获取 FrameProcessorFactory，
  工厂从扩展目录加载库。原始 RGB-D 不代表绕过帧处理器，删除可能影响处理、属性或深度语义。
  依据：[FrameProcessor](https://github.com/orbbec/OrbbecSDK_v2/blob/575347ca742d219ef61b1e214e97043f17f48d23/src/device/component/frameprocessor/FrameProcessor.cpp)、
  [Gemini 2](https://github.com/orbbec/OrbbecSDK_v2/blob/575347ca742d219ef61b1e214e97043f17f48d23/src/device/gemini2/G2Device.cpp)、
  [Gemini 330](https://github.com/orbbec/OrbbecSDK_v2/blob/575347ca742d219ef61b1e214e97043f17f48d23/src/device/gemini330/G330Device.cpp)。
- **`filters/libFilterProcessor.so`、`libob_priv_filter.so`：保留。**
  SDK 会枚举插件目录以建立滤波器工厂；Gemini 推荐滤波策略也会查询处理器和创建滤波器。
  管理器没有显式启用某滤波器，不等于 SDK 不枚举、不加载或不依赖对应库。
  依据：[PrivFilterLoader](https://github.com/orbbec/OrbbecSDK_v2/blob/575347ca742d219ef61b1e214e97043f17f48d23/src/filter/privatefilters/PrivFilterLoader.cpp)、
  [Gemini 330 策略](https://github.com/orbbec/OrbbecSDK_v2/blob/575347ca742d219ef61b1e214e97043f17f48d23/src/device/component/recommended_post_filters/Gemini330PostFilterStrategy.cpp)。
- **`firmwareupdater/libfirmwareupdater.so`：可研究的裁剪候选，尚不裁剪。**
  Rust 管理入口没有固件升级命令，但官方镜像保留上游升级工具；SDK 组件还支持可选
  深度预设更新等操作。需先明确精简镜像放弃哪些功能，不能将“只用于升级固件”当作完整依赖分析。
  依据：[FirmwareUpdater](https://github.com/orbbec/OrbbecSDK_v2/blob/575347ca742d219ef61b1e214e97043f17f48d23/src/device/component/firmwareupdater/FirmwareUpdater.cpp)。
- **`depthengine/libdepthengine.so*`：Gemini 专用镜像的裁剪候选，尚不裁剪。**
  可见源码加载路径位于 Femto Bolt raw-phase 深度引擎，与当前 Gemini 支持范围不同。
  这支持缩小后续验证范围，但不构成预编译库在全部固件和错误路径上无依赖的证明。
  依据：[DepthEngineLoader](https://github.com/orbbec/OrbbecSDK_v2/blob/575347ca742d219ef61b1e214e97043f17f48d23/src/device/femtobolt/rawphase/depthengine/DepthEngineLoader.cpp)。
- **增强深度组件：保留。**
  arm64 包还包含 `enhanced_depth_filter` 与其引擎库。不同架构的包不相同；
  amd64 短测不能认证 arm64 裁剪安全，也不能将架构缺省差异当作项目已完成的裁剪。

## 裁剪的验收门槛

候选必须在独立、明确限制功能的镜像中测试，不改变默认采集行为：

1. 固定 SDK/固件/架构、实际档位和配置，与完整镜像做同口径对照。
2. 核对实际进程映射、动态加载诊断、属性及滤波行为；仅无设备加载或符号存在不够。
3. 对全部声明型号验证发现、冷启动、RGB-D 布局、深度尺度、CameraInfo/TF、质量证据、
   录制保护、停止及有界故障恢复；不得修改阈值使删库版本通过。
4. 检查镜像最终文件系统和分发层均无被裁剪库。官方镜像还保留上游源码中的 SDK，
   在后续层 `rm` 运行时副本不足以从镜像层中移除原始二进制。
5. 重新生成镜像与 runtime 清单并重新确认档位，保留剩余组件的全部许可。

当前没有被验证为可从默认镜像安全删除的扩展。保留插件不表示每次采集均调用它；
裁剪部分插件也不等于整个采集链已摆脱受限二进制。
