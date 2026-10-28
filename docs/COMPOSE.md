# 相机组与管理服务部署

当前生成与启停机制见[三相机配置生成与生命周期编排](generation-lifecycle.md)。根目录 Compose 仅引导管理服务，相机服务来自受控状态目录中的不可变生成版本，不再由固定双机服务名决定。

## 安装与运行

宿主机需要原生 Linux、rootful Docker Engine、Compose、systemd、udev 和 flock。部署只支持本机 Docker daemon。管理服务有 Docker 管理权限，应仅向可信用户开放；保持回环访问或使用受控隧道。

```sh
sudo ./deploy.sh install
./deploy.sh web
./deploy.sh up
./deploy.sh status
./deploy.sh logs
```

安装构建镜像、安装设备规则并启用检查式开机编排。设备未绑定或档位未确认时，只保留管理入口，不打开相机。具体设备与档位由受控配置提供，不在公开文档中给出。

`up` 确保全部启用相机通过检查；相同健康版本不隐式先停机。正式采集与调试预览互斥，启动前须显式停止预览。部分启动失败保留已启动实例并报告未就绪。

schema v2 要求镜像与实际消息满足原生 YUYV 契约。Web 已支持三机配置与逐台调试预览；默认管理镜像运行 Rust 二进制，详见 [Rust 管理链路](rust-management.md)。这些功能不代表真机集成验收完成。

## 停止、移除与开机管理

```sh
./deploy.sh stop
./deploy.sh down
sudo ./deploy.sh enable-autostart
sudo ./deploy.sh disable-autostart
```

停止按实际实例标签操作，先停止相机和预览，再停止管理服务。停止失败时保留管理服务供诊断。移除只处理受管实例，不删除宿主机配置或未知目录。

systemd、CLI、Web 生命周期入口共用操作锁及录制保护。相机不会因 unhealthy 自动重启，也不会由 Docker 的重启策略绕过检查恢复。录制保护存在时，维护与恢复请求明确失败；控制端失联不会使保护自动过期。

不要并行部署另一套无条件驱动启动器。直接操作 Docker 会绕过应用保护，不能作为正常维护路径。

## 状态与权限

部署环境、状态、生成配置和备份属于受控数据，不能提交到仓库或附入公开报告。生成文件仅所有者可读写，容器使用匹配的受控身份读取配置；设备权限由宿主机规则提供，仍需目标机器验收。

查看 systemd 结果、容器私有日志及实际 USB/DDS 状态应在受控环境进行。本次验证不包含真实驱动镜像构建、设备启动或 systemd 开机实验，详见实现 PR 的验证说明。
