# 校园课程任务智能管家

把手动复制的课程通知整理成可维护的学习任务。大语言模型理解通知，Python校验日期与结构，用户确认后保存到本地JSON；后续通知可进行重复检测、修改预览和历史恢复。

当前版本：**0.7.4**。支持Windows桌面应用、Streamlit界面和命令行，三种入口共用本地数据。

## 已有功能

- 粘贴群聊通知或读取 `inbox.txt`，预览AI提取的课程、作业、截止时间及提交要求；多项作业拆分。
- 根据用户填写的课程全名理解简称；课程不明或信息有歧义时保留待确认项。
- 以上海时区及消息实际发送日期理解“明天”“下周三”等相对日期。明确月日省略年份时优先按发送年份，否则默认今年；跨年疑问需核对。
- 只有明确截止日期、没有钟点时，按个人约定默认当天23:59，并说明默认值来源；不覆盖原文明示的钟点或真实歧义。
- 综合课程、名称、内容和原文匹配已有任务，展示重复、修改或不明确的关系。涉及变更先预览和确认，重复批次不重复调用模型或新增任务。
- 查询、课程筛选、未来7天、截止排序、完成状态切换、撤销最近修改，以及原始通知和变更历史。
- 原子写入、排他写锁、备份及操作日志；损坏文件会报错，不通过初始化静默清空。
- 默认提前3天和1天提醒；过滤已完成任务、节点去重、延期重算及错过运行补检。Windows通知不可用时降级到终端和日志。

程序不自动读取微信群，不代写或自动提交作业。模型无法直接写数据库、执行命令或修改系统任务计划。

## Windows快速启动

要求Python 3.11+。在项目目录打开PowerShell：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[desktop]"
.\.venv\Scripts\python.exe -m course_assistant init
.\.venv\Scripts\python.exe .\desktop_app.pyw
```

桌面窗口使用系统WebView2。首次运行自动建立个人数据目录，不包含开发者的真实课程或作业。进入“个人设置”，登记课程全名，并在“AI连接”中填写自己的API Key、基础地址和模型名，再进入“通知导入”。

如仅使用浏览器界面：

```powershell
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port 8501
```

打开 <http://127.0.0.1:8501>。界面仅监听本机。仓库提供源码，独立exe可在Windows本地构建：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\.venv\Scripts\python.exe .\scripts\build_desktop.py --stage-only
```

产物位于 `dist/校园课程管家.exe`。运行时将个人JSON保存在程序旁的 `data`，构建资源不打包任务、日志和密钥。

## AI连接与解析

网页设置中的Key只留在本次会话，关闭或刷新后可能需要重填；不写入JSON或日志。命令行也支持进程环境变量：

| 环境变量 | 含义 |
| --- | --- |
| `OPENAI_API_KEY` | 用户自己的API Key |
| `OPENAI_MODEL` | 账户可用的模型标识 |
| `OPENAI_BASE_URL` | 兼容接口的基础地址 |
| `OPENAI_RESPONSE_MODE` | `json_schema`或`json_object` |
| `OPENAI_TIMEOUT_SECONDS` | 请求超时，默认60秒 |

DeepSeek连接可使用 `https://api.deepseek.com` 和 `json_object`，模型请填写自己账户可访问的标识。严格结构校验在两种模式下都会执行，不因只返回JSON而跳过。`.env.example`仅说明变量名，程序不自动加载`.env`。

命令行解析：

```powershell
.\.venv\Scripts\python.exe -m course_assistant parse --help
# 把选择后的通知保存到UTF-8 inbox.txt，再预览
.\.venv\Scripts\python.exe -m course_assistant parse --file inbox.txt --sent-at '2026-11-13' --preview-only
```

发送日期应为通知实际发出的日期，可输入中文日期或仅日期，不能用粘贴时间替代相对日期基准。核对课程、日期依据、提交方式及候选旧任务后，再确认保存。取消预览不写正式任务；没有可靠候选时不会擅自覆盖。

## 任务与提醒

```powershell
.\.venv\Scripts\python.exe -m course_assistant list
.\.venv\Scripts\python.exe -m course_assistant agenda
.\.venv\Scripts\python.exe -m course_assistant preferences show
.\.venv\Scripts\python.exe -m course_assistant preferences set --enable --lead-days 3 1 --check-time '09:00' --channel auto
# 只预览提醒计划
.\.venv\Scripts\python.exe -m course_assistant remind check --dry-run
# 真正检查一次，可能显示通知，并记录已处理节点
.\.venv\Scripts\python.exe -m course_assistant remind check
```

新数据目录默认关闭提醒，需由用户启用。未确认完整截止时间的任务只提示补充信息，不生成精确倒计时。提醒检查只读取本地任务，不调用AI。

配置Windows自动运行时先预览：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\configure_scheduler.ps1 -PythonExe "$PWD\.venv\Scripts\python.exe"
```

仅预览不会注册系统任务。核对解释器、项目路径、时间和普通用户权限后，自行追加 `-Apply` 并输入确认短语。更改每日时间后需要重新确认更新计划。电脑关机时无法即时提醒；再次启动后按补检规则处理。系统计划是否真正触发应结合任务计划程序运行记录与 `data/logs` 核对，不能只看设置界面。

## 数据与隐私

`init`只补齐缺失文件，重复执行不会清空已有任务。默认个人数据包括：

- `data/deadlines.json`：任务、原通知、处理批次和修改历史。
- `data/courses.json`：用户登记的课程。
- `data/config.json`：个人提醒等偏好。
- `data/reminders.json`：提醒节点状态，按需创建。
- `data/backups`、`data/logs`、`data/cache`：备份、运行日志和模型缓存。

上述个人数据、原始课程通知、报告、运行截图、缓存和密钥均不纳入Git提交。模型处理会将选中的通知及必要的课程/旧任务上下文发送到用户配置的服务商，请先删去无关个人信息。

`settings/parsing.json`提供不含课程名单的解析默认配置。`semester_week1_monday`需按真实校历填写；`date_only_deadline_time`可设为其他钟点，设为`null`则关闭自动补钟点。个性化修改该文件时不要把私密信息加入提交。

## 测试与验证范围

2026年11月21日，清理后的70个项目文件在Windows / Python 3.14.4、Streamlit 1.55.0环境重新验证：**304项测试全部通过，0失败、0跳过**。依赖安装在项目目录外，报告、缓存和个人数据不随源码发布。验证条件见[测试记录](docs/TESTING.md)。

安装界面依赖后运行：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用临时数据目录、模拟模型响应和可注入时钟，不依赖等待数天，不调用收费API，也不注册系统计划。真实模型连接与操作系统通知应分别验收：

```powershell
.\.venv\Scripts\python.exe .\scripts\acceptance_api.py --help
.\.venv\Scripts\python.exe .\scripts\acceptance_relations.py --help
.\.venv\Scripts\python.exe -m course_assistant remind test-notification
```

`examples`中的消息明确标注为模拟材料，不能作为个人真实课程使用证据。自动化测试通过也不代表模型理解、真实每日触发和用户实际看到提醒均已验证。

## 目录

```text
course_assistant/       解析、匹配、任务管理、存储、提醒及界面
tests/                 自动化测试
scripts/               API验收、桌面构建、Windows任务计划辅助脚本
settings/parsing.json  解析默认规则
examples/              明确标注的模拟通知
assets/                应用图标
docs/ARCHITECTURE.md    模块与权限边界
docs/DATA_MODEL.md      数据结构和历史格式
desktop_app.pyw         桌面启动入口
streamlit_app.py        网页启动入口
```

更多设计信息见[系统架构](docs/ARCHITECTURE.md)和[数据结构](docs/DATA_MODEL.md)。
