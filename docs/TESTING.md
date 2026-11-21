# 上传前的源码验证

验证日期：2026年11月21日。

本次发布保留70个项目文件，报告、截图、报告修订材料和Python缓存均已移出项目。使用项目目录外的独立Python虚拟环境，按 requirements-ui.txt 安装Streamlit后，在项目目录运行完整自动化套件。

| 检查项 | 实际结果 |
| --- | --- |
| 环境 | Windows、Python 3.14.4、Streamlit 1.55.0 |
| 依赖检查 | `python -m pip check`通过 |
| 空白初始化 | 任务0项、课程0门、提醒默认关闭 |
| 自动化命令 | `python -B -m unittest discover -s tests -q` |
| 测试结果 | 运行304项，全部通过，失败0、跳过0，退出码0 |
| 测试耗时 | 31.114秒 |
| 个人数据 | 测试使用临时目录，源码不包含真实课程通知或API Key |

测试覆盖任务管理、原子存储、异常输入、模型响应校验、相对日期、默认年份与钟点、课程推断、重复及修改关系、确认与撤销、提醒规划、界面交互和Windows辅助脚本预检。

模型响应和提醒时间均可模拟，不会请求收费模型、等待真实截止节点或注册系统任务。这些结果验证程序逻辑，不等同于所有真实通知的识别准确性、桌面通知实际可见或自然时间触发已完成验收。

真实API验收入口为 `scripts/acceptance_api.py` 与 `scripts/acceptance_relations.py`，先用 `--help` 查看参数，再自行配置账户连接；系统通知单独用 `python -m course_assistant remind test-notification` 观察。

当前仓库不包含开发者的真实使用记录。`examples`中的通知均为模拟材料。
