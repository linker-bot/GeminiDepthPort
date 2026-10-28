# 第三方组件与许可边界

本项目源码采用 Apache-2.0；采集镜像包含不同许可的 SDK、扩展及系统依赖。
以下说明基于仓库固定的构建输入，不将整个镜像重新许可为 Apache-2.0 或 MIT，
也不构成完整镜像 SBOM 或所有分发义务已经履行的声明。

## 实际依赖范围

- **管理镜像**（`web.Dockerfile`）：Rust 管理程序、Docker CLI/Compose 与 Alpine 组件，
  不包含 Orbbec SDK。宿主机通过该程序编排独立采集容器。
- **官方采集镜像**（`Dockerfile`）：构建官方 ROS 驱动并链接预编译 Orbbec SDK；
  上游安装规则也会复制扩展二进制。
- **Rust 采集镜像**（`capture.Dockerfile`）：通过 C ABI 动态加载同一预编译 SDK，
  复制所选架构的整个 SDK 包，包含扩展。Rust 实现不替代厂商库。
- **离线工具**：纯 Rust 深度/质量核心不打开 SDK；ROSbag、ROS 和图像边界工具的
  依赖以其运行环境为准。

当前固定采集版本为 **SDK 2.10.6**。没有发现 SDK v1 的构建或加载依赖。
早期 ABI 声明曾核对 SDK 2.9.3 头文件，这不表示部署镜像同时包含该版本。

## 官方驱动与 SDK 来源

官方驱动来源为 [OrbbecSDK_ROS2](https://github.com/orbbec/OrbbecSDK_ROS2)，
固定提交为 `c153462518ad674650bafa4464fda72c27ab797a`，驱动根许可为 Apache-2.0。
构建读取其中 `orbbec_camera/SDK/<architecture>/` 的预编译库与头文件，不从本地其他项目复制 SDK。
原生 YUYV 补丁只修改公开 ROS 驱动源码，来源及摘要见
`patches/public-orbbec/manifest.json`，不修改受限扩展二进制。

SDK 许可文本取自 [OrbbecSDK_v2 的固定提交](https://github.com/orbbec/OrbbecSDK_v2/blob/575347ca742d219ef61b1e214e97043f17f48d23/LICENSE.txt)
（对应 v2.10.6），在 `licenses/orbbec-sdk-v2.10.6/` 原样保存，并记录来源与 SHA-256。
该提交标识许可文本来源，不是对 ROS 包中预编译库可由该提交逐字节重建的证明。

## SDK 主体与受限扩展

SDK v2.10.6 的许可区分主体源码与扩展：

- 主体源码使用 MIT；保留其版权及许可声明。
- `extensions` 下厂商二进制有独立条款：允许个人、学术和商业用途，但限于使用 Orbbec 产品。
- 扩展条款禁止修改、反编译和逆向工程，也禁止用于非 Orbbec 产品，并要求保留声明。
- SDK 及其预编译库还依赖第三方组件，不能只依据主体源码的 MIT 标记推断整个分发包的条款。

采集镜像保留随架构 SDK 提供的 `depthengine`、`filters`、`frameprocessor` 和
`firmwareupdater` 扩展；部分架构还提供增强深度组件。打包某个扩展不表示每次采集都调用它。
默认构建不裁剪这些库；依赖分析与裁剪门槛见[扩展评估](docs/development/sdk-extensions.md)。
FFI、动态链接或容器封装不会取消这些组件自身的许可条件。

## 许可证打包与检查

两个正式采集镜像和受控离线组装镜像都在 `/opt/third-party/orbbec/` 保存：

- `ROS2-LICENSE`、`ROS2-NOTICE`：固定官方驱动的原始声明。
- `SDK-LICENSE.txt`、`SDK-LICENSE-SOURCE.json`：固定 SDK 许可文本与来源。
- `sdk-licenses/`：固定驱动包的**完整** SDK 第三方许可目录，保留文件名和内容，
  包括 libusb 的 `COPYING`、live555 的 `COPYING` 与 `COPYING.LESSER`。
- `license-inventory.json`：上述文件的相对路径及 SHA-256。

`sdk_licenses.py` 在必需文件缺失、SDK 许可来源变化或文件摘要不匹配时失败。
Rust 镜像将这些文件绑定到运行清单，冒烟测试同时检查完整性；官方镜像 CI 单独验证许可包。
这些检查证明打包文件与声明一致，不替代第三方源代码提供、重新链接等义务的逐项核对。

## 其他组件

SDK 的第三方清单包括 libusb、live555、libuvc、libjpeg-turbo、libyuv、spdlog 等；
Rust USB 恢复助手还直接链接系统 libusb-1.0；SDK 包和系统库是分别核对的组件。
libusb 随包许可为 LGPL-2.1；live555 同时携带 GPL-3.0 与 LGPL-3.0 文本。
这些文件属于其依赖许可材料，不表示本项目整体采用 GPL；实际链接和分发义务需按组件核对。

Python 边界使用 [PyYAML](https://github.com/yaml/pyyaml/blob/main/LICENSE)、
[NumPy](https://github.com/numpy/numpy/blob/main/LICENSE.txt) 和
[OpenCV](https://github.com/opencv/opencv/blob/4.x/LICENSE)。ROS 包、基础镜像及系统库
保留各自许可。[Docker CLI](https://github.com/docker/cli/blob/master/LICENSE) 与
[官方镜像](https://github.com/docker-library/docker)的许可亦独立适用。
Rust 依赖版本记录在 Cargo.lock；Web 前端使用系统字体，无 npm 运行时依赖。

## 镜像分发

分发前应基于实际镜像生成 SBOM，检查 SDK/扩展、ROS、系统库及 Rust 依赖的全部
版权、许可、NOTICE、源码提供和重新链接要求。基础镜像、APT/APK 及间接依赖尚未
全部按 digest/版本锁定。收集许可文本或通过无设备冒烟不等于完成整镜像许可审计。

Orbbec/Gemini 名称用于说明兼容设备，不表示官方背书或获得商标授权。
