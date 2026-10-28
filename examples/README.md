# 公开接入示例

这些示例从未绑定、禁用且未确认的配置意图开始，可供用户和 Agent 参考。
它们不包含可直接开机的实机配置，不证明档位支持、标定精度或硬件同步。
所有命令从仓库根目录执行；安装 Rust CLI 的构建方法见[贡献指南](../CONTRIBUTING.md)。

- [官方 ROS 三机](official-three-camera/README.md)：正式管理的一至三台设备流程。
- [Rust 单机](rust-single-camera/README.md)：显式后端策略、不可变运行清单、质量与录制保护。
- [消费与离线处理](consumer-offline/README.md)：从实际证据到消费查询及离线导出。
- [独立硬件测试](hardware-reopen/README.md)：首次重开故障验证与独立多机计划入口。

## 先做离线检查

```sh
geminidepthport validate --file examples/official-three-camera/inventory.json --draft
geminidepthport validate --file examples/rust-single-camera/inventory.json --draft
```

两份模板均应返回 `valid:true`、`enabled_count:0`、`hardware_verified:false`。
去掉 `--draft` 应以 `NO_ACTIVE_CAMERAS` 拒绝。草稿通过只证明配置结构成立。
这些命令不要求 Docker、SDK、ROS 或相机，也不生成档位确认。

## 接入与信息边界

在专用 Linux 部署副本中选择一份模板作为初始 `inventory.json`；已有部署必须先
结束录制、释放保护并停止采集和预览，再按[配置指南](../docs/guides/configuration.md)
维护，不能用示例覆盖现有绑定。模板的镜像标签和型号仅是公开选择示例，须依据实际
构建及设备更改；流档位必须由实机检查确认。生产启用数量上限仍为三台。

使用 Web/受管 CLI 保存真实绑定、参数和确认，按[部署指南](../docs/guides/deployment.md)
完成启动。`render` 读取部署根目录的 inventory，不接受 `--file`；不要将
`validate --file` 理解为已导入示例或已经生成服务。所有设备操作须在部署根目录或
显式正确的 `--root` 下运行。

真实 inventory（即使该路径原本被 Git 跟踪）、序列号、通信绑定、标定、原始图像、
凭据及 `state/`、`work/` 不得提交。不要从历史测试目录复制后替换几处字符串；
只从公开模板填写必要输入，并由真实运行生成确认、snapshot、session 和摘要。
`verification` 保持 `null`，不能手填确认、伪造摘要或把验证标记设为真。

## Agent 使用顺序

1. 先读取所选案例与对应后端约束，离线校验模板。
2. 在受控部署中发现实际设备；缺少身份、型号支持或档位时报告缺项，不选择第一台设备或猜测数值。
3. 按管理流程构建镜像、保存参数、逐台预览确认，停止预览，再生成并启动。
4. 读取健康与消费证据后才报告就绪；发布成功不能代替消费者收到。
5. 提交前只保留公开示例和源码改动，检查 diff；运行证据留在私有目录。

消费方接入见[通信与录制流程](../docs/guides/consumer-integration.md)，离线输入与导出
见[offline-depth](../crates/offline-depth/README.md)及[录制适配器](../tools/recording/README.md)。
公开验证范围与未完成项以[验证索引](../docs/validation/README.md)为准。
