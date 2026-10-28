# 参与贡献

从 main 创建聚焦的工作分支。接口或配置变更应同时更新文档和相关测试。
第三方代码必须记录公开来源、固定版本、许可及修改，并保留适用声明。
贡献代码采用本项目 Apache-2.0，第三方组件保留原许可。

## 开发检查

宿主机需要 Rust 1.85+、C/C++ 编译器与 Python 测试依赖。Docker/ROS 检查另需对应环境。
在仓库根目录运行：

```sh
pip install -r requirements-test.txt
cargo test --locked --workspace
cargo clippy --locked --workspace --all-targets -- -D warnings
cargo fmt --all --check
cargo build --locked -p geminidepthport
python3 tests/rust/test_management.py
python3 -m unittest discover -s tests -v
python3 -m compileall -q driver_build.py scripts runtime tests
node --check web/app.js
node tests/test_web_form.js
bash -n deploy.sh runtime/entrypoint.sh
ORBBEC_DEPLOY_DIR="$PWD" docker compose --profile cameras config --quiet
```

Node.js 只用于前端开发检查，不是 Web 运行依赖。`tests/legacy_reference/` 仅作为合成
回归的行为参考，不进入管理镜像。Linux 隔离管理测试见[测试参考](docs/development/linux-management-fixture.md)，
ROS bridge 检查见 [ros/README.md](ros/README.md)，CI 是容器构建检查的执行依据。

## 文档与验证

文档按读者任务组织，每项行为只设一个完整定义位置：

- `README.md`：项目定位、最短启动路径和主要入口。
- `docs/guides/`：部署、配置、接入和故障处理步骤。
- `docs/interfaces/`：CLI、HTTP、ROS 和数据字段参考。
- `docs/contracts/`：版本化语义、不变量、状态转换和失败边界。
- `docs/development/`：模块职责、实现原因、构建与调试。
- `docs/validation/`：验收方法、冻结产物与历史结果。

crate 和工具 README 保留本地入口，用链接引用共享语义；指南不复制完整契约。
代码注释解释所有权、锁、单位、时钟和失败后的状态，不复述函数名或开发批次。
`python3 -m unittest discover -s tests -p test_documentation.py -v` 检查本地文件链接、
Markdown 标题锚点和 Shell 示例语法；完整 Python 测试与 CI 已包含该检查。
操作文档描述当前行为；接口语义写入对应版本化契约。开发批次、设计讨论与执行流水
不进入用户指南。测试报告记录冻结产物、方法、成功与失败、测量范围和未验证项，
不得以新的二进制指纹替代旧测试产物，也不得通过放宽门槛掩盖失败。

真实 inventory、设备身份、标定、原始图像、凭据和 `.env`、`state/`、`work/` 不得提交。
公开测试使用合成身份；报告只保留可复核的必要汇总和产物摘要。
