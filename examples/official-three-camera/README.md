# 官方 ROS 三机配置意图

[inventory.json](inventory.json) 使用显式 `official` 后端，提供三个未绑定、禁用的
逻辑槽位。逻辑名称、命名空间及帧前缀为合成示例，不代表左右位置或安装角色。
型号可以在支持目录内调整，同一型号可绑定多台；实际启用数量为一至三台。

## 从草稿到采集

1. 按[示例总览](../README.md)离线校验，在专用部署副本选择模板。
2. 按[部署指南](../../docs/guides/deployment.md)启动管理界面；扫描并逐一绑定型号
   匹配的真实设备，不按 USB 枚举顺序分配逻辑槽位。
3. 保存真实支持的流尺寸、帧率、时间来源、标定声明和健康策略，启用需要的设备。
   参数范围见[配置指南](../../docs/guides/configuration.md)。
4. 构建/选择实际官方镜像，逐台启动预览并执行“检查出帧并确认此档位”。
   保存配置不是确认；不能把模板的 `profiles_verified` 改为真来绕过检查。
5. 停止全部预览，执行受管“检查并启动相机组”。管理器完成正式 generation 生成、
   逐台检查及整组复检；部分成功仍为未就绪。

在已经正确配置的部署根目录中，可以使用以下正式入口：

```sh
geminidepthport validate
geminidepthport render
geminidepthport up
geminidepthport check
```

这些命令不是离线演示，会读取实际 inventory，可能访问 Docker 和设备；只在完成
上述准备后运行。`render --draft` 也不会赋予采集就绪状态。

## 接入消费方

按[消费指南](../../docs/guides/consumer-integration.md)验证实际 Image、CameraInfo、TF、
QoS、源时间推进及录制读回。官方路径的按时间观测关联不等同于 SDK frameset 证据。
正式录制前取得保护并核对快照，结束后凭证释放；具体见
[生命周期](../../docs/guides/lifecycle.md)及[录制适配](../../tools/recording/README.md)。
模板未预设流尺寸与帧率；健康检查策略仅为候选：发现及采样窗口各 8 秒、数据新鲜度
1 秒、最低 FPS 比例 0.8，须按目标设备和消费契约审查并实际确认。模板未包含真实
标定，不能用它声明物理精度、三机长稳或业务消费者已通过。
