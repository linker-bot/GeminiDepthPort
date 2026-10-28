# 第三方组件与许可边界

本文件记录项目来源及依赖边界，不是对构建镜像全部文件完成许可审计的声明。仓库不提交官方 SDK、固件、扩展二进制或真实设备数据。

## 本项目源码

新增管理、配置、预览和部署代码按仓库根 LICENSE 的 Apache-2.0 发布。相机启动参数、型号对应关系及 tests/launch-contracts.json 依据下列固定版本整理；适配涉及 Gemini 2 不支持 config_file_path 的接口差异。此项目不构成纯 Rust SDK，也不代表官方实现。

## Orbbec ROS 2 驱动

- 来源：https://github.com/orbbec/OrbbecSDK_ROS2
- 固定 commit：c153462518ad674650bafa4464fda72c27ab797a
- 根许可证：Apache-2.0。
- 根许可参考：https://github.com/orbbec/OrbbecSDK_ROS2/blob/v2-main/LICENSE
- 许可证文本副本：licenses/Apache-2.0.txt。

Dockerfile 在构建时获取上游源码；应保留所获取版本的版权声明、LICENSE 及适用 NOTICE。参数清单不是整个上游代码或二进制的授权清单。

本项目对上述公开 ROS 驱动应用原生 YUYV 发布补丁，保留原始版权上下文，并在 `patches/public-orbbec/manifest.json` 记录来源与修改摘要。新增复制函数采用 Apache-2.0；不包含官方 SDK 二进制。修改细节见 [原生 YUYV 说明](docs/native-yuyv.md)。

## SDK 与扩展二进制

- SDK v2：https://github.com/orbbec/OrbbecSDK_v2/blob/main/LICENSE.txt
- 旧 SDK：https://github.com/orbbec/OrbbecSDK/blob/main/LICENSE.txt

SDK v2 当前主体源码采用 MIT，但 extensions 下二进制有单独的产品使用及修改/逆向限制。旧 SDK 的 lib 二进制也有独立条款。实际 ROS 驱动打包或构建获取的 SDK 版本必须按具体文件验证，不能将整个 SDK 标为 MIT，也不能假定主分支许可证等于实际固定版本的许可证。

本项目仅面向 Orbbec 产品使用。Rust 的 FFI、动态链接或容器打包不会免除这些依赖的义务。

## Python、图像和 ROS 依赖

当前源码使用 PyYAML（MIT）及用于测试/容器预览的 NumPy（BSD-3-Clause）；容器预览使用 OpenCV，最终条款以安装版本为准。相机镜像另含 ROS 2 软件包、SDK 和系统库；其中可能包含 LGPL 等许可证组件。管理镜像使用 docker:27-cli 并安装 Alpine 包。

依赖原文来源：

- PyYAML：https://github.com/yaml/pyyaml/blob/main/LICENSE
- NumPy：https://github.com/numpy/numpy/blob/main/LICENSE.txt
- OpenCV：https://github.com/opencv/opencv/blob/4.x/LICENSE
- Docker CLI：https://github.com/docker/cli/blob/master/LICENSE
- Docker 官方镜像构建：https://github.com/docker-library/docker

Web 前端为本项目原生 HTML/CSS/JavaScript，使用系统字体，无 npm 前端运行时依赖。

## 镜像发布前

对实际镜像生成组件/版本清单，收集适用许可证和版权声明，核对源码提供及修改说明要求；构建中下载的 SDK/扩展须逐项检查。基础镜像、APT/APK 与间接依赖目前未全部按 digest/版本锁定，因此不能把本文件当作固定、完整的 SBOM。

项目名称及 Orbbec/Gemini 型号仅用于描述项目与兼容设备；上游许可不表示获得商标背书授权。
