# 系统架构与运行边界

调用关系：`__main__ → cli → service → models / storage`。

AI 路径：`cli.parse → workflow（查处理凭据） → parsing/cache/llm → TaskDraft → matching（本地多信息+必要的语义判断） → 关系/差异预览 → 人工确认 → workflow.apply（版本检查） → service/storage（任务+凭据一次原子保存）`。

提醒路径：`手动CLI / 用户确认注册后的Windows任务计划 → run_reminders.ps1 → ReminderRunner → 读取配置/任务/提醒凭据 → DeadlineReminderPlanner → 原子预留节点 → Windows通知或终端降级 → 保存送达结果与完整运行日志`。提醒路径不调用模型，也不注册系统任务。

- `models.py`：数据对象、字段校验、完成与人工修正的状态转换，无文件 IO。
- `storage.py`：访问范围限原三份JSON及提醒状态文件；同目录临时文件、fsync、原子替换、备份、排他锁和日志。修复Windows锁交接时偶发PermissionError的有界重试。
- `service.py`：应用用例；所有修改在同一个目录锁内重新读、验证、写，防止两个 CLI 进程覆盖彼此的数据。单文件保存成功后再写日志。
- `cli.py`：保留人工命令；新增文本/粘贴/inbox 输入、预览和随机短语人工确认。
- `interfaces.py`：保留前三轮类型兼容；第4轮实际提醒结构在 `reminders.py` 定义。
- `llm.py`：兼容HTTP接口，显式json_schema/json_object两种模式都做严格本地校验；DeepSeek后者已做真实连接诊断。HTTPS、超时、拒绝重定向、脱敏错误，无工具调用/自动重试，Key只从环境读取。
- `schema.py`：请求附带 strict JSON Schema；本地使用等价严格校验，不信任服务商只返回 JSON 的承诺。
- `parsing.py`：模型语义抽取与 Python 确定性操作之间的边界；先验证结构及原文引用，再转换候选任务。
- `time_understanding.py`：检查模型参数与被引用时间词是否一致，相对日期根据 Notice.sent_at 计算且不使用粘贴时间；明确月日缺年时优先使用发送年份，无发送日期时按上海当前年补齐，跨年疑问继续核对。
- `cache.py`：按输入/上下文/Schema/提示词缓存响应，缓存命中前不创建 API 配置；阶段锁阻止相同输入并发重复请求。
- `matching.py`：课程、标题、内容、原通知及字段综合比较；关系模型仅提供给定候选 ID、类别、变更字段和证据。Python 构造稀疏修改，缺失字段不覆盖。
- `workflow.py`：付费解析前校验明显无效的目标/参数；生成四类计划、展示差异、拒绝不明确项、校验台账版本并原子保存。修改要求时传播对应待确认标记，用户明确核实完整日期时清除deadline标记。
- `preferences.py`：配置默认3/1天、每日时间、渠道和课程覆盖；旧配置没有新增字段时补默认值但不重写文件。
- `reminders.py`：可注入时钟的纯计划、补检合并、节点凭据、状态、运行日志、未来任务查询；网络通知前持锁预留，外部通知期间不长期占任务写锁。
- `notifications.py`：使用隐藏的Windows PowerShell子进程调用原生NotifyIcon；内容由stdin JSON传输，不拼进代码。始终保留终端/日志，桌面不可用时降级。
- `scripts/configure_scheduler.ps1`：配置时间换算、绝对路径和当前交互用户预览；只有用户运行 `-Apply` 并再次输入确认短语才注册/更新任务。
- `scripts/run_reminders.ps1`：保存完整stdout/stderr、实际起止时间和退出码，区分manual和scheduled来源。

## 预留接口

| 接口 | 输入 | 输出 | 后续接入位置 |
| --- | --- | --- | --- |
| `NotificationParser.parse` | `Notice`、`ParseContext`（可空参考时间、偏移、课程名） | `Sequence[TaskDraft]` | 已由 AINotificationParser 实现；analyze 另返回分类与批次警告 |
| `DuplicateDetector.assess` | 候选 `TaskDraft`、已有任务 | `DuplicateDecision`，含 new/duplicate/possible_update/uncertain、相关 ID、理由、确认标记 | 第3轮 SemanticMatcher 实现，analyze 额外给出原文及变更字段 |
| `ReminderPlanner.plan` | 早期任务/时间/偏好原型 | `Sequence[ReminderSuggestion]` | 保留旧类型；当前实现使用DeadlineReminderPlanner/ReminderPlan，额外接收历史节点凭据 |

`TaskDraft` 无 ID/状态/历史。解析器不能直接写 JSON。CLI 使用 NoticeWorkflow 生成计划，在人工确认后提交；不明确项阻止整批修改，过时预览不能覆盖并发变更。保留 `TaskService.import_drafts` 作为旧调用兼容入口并加入精确候选去重，复杂语义关系应使用 workflow。人工 `set-deadline` 的 CLI 同样要求差异确认。删除/撤销支持版本检查，任务原文及历史不丢失。

提醒偏好现存于 config.json 的兼容可选字段，节点状态存于 reminders.json，不改变任务自身ID、状态或历史。精确倒计时以完整且已核实的截止时间为前提；待确认信息只发送补充提示。人工修改完整时间可以清除deadline待确认标记，修改及清除可一起撤销。

## 持久化边界

正常写入在持锁范围完成；读者读取单个原子替换文件。任务及通知处理凭据仍一起保存于deadlines.json；提醒只写自己的reminders.json，不修改任务台账。手动备份顺序创建三个核心快照及已存在的提醒状态快照，不是跨文件提交事务。

提醒凭据先持久化预留，再发送外部通知，随后保存送达状态。这提供正常运行及并发下的节点去重，但磁盘与操作系统通知无法形成同一事务；进程强制中断时可能留下reserved，系统提示送达不明且不自动重发。将来其他节点不受影响。每次运行的实际时间、模拟/预览标记、计划、结果/失败分别写独立日志，不自动删除。

原始通知只存在任务台账及其备份中；操作日志不重复存储正文。备份失败会阻止覆盖；保存后辅助日志或旧备份清理失败会警告。文件系统未提供硬件级持久性保证，外部编辑器也不遵守本项目的锁；使用范围限定为本机本地磁盘。

运行抽取时发送原通知、实际发送时间、课程/校历上下文；运行语义比较时还会发送最多20个相关旧任务的字段摘要及截断原文，不发送完整历史文件、密钥或其他环境变量。接口地址来自进程环境，或网页用户在个人设置中明确填写的会话配置，群聊无法更改；Key只进入认证请求头。所有通知文本均为 JSON user 数据，固定规则在 system，且不提供工具。返回结构外字段拒绝，字段值不执行 shell/eval 或模型指定路径。该隔离限制副作用，但不能保证模型语义永不误判，仍需核对预览。

## 本轮验收边界

第5轮184项自动化测试通过，含两种输出模式的完整链路和备份恢复；真实DeepSeek连接诊断成功，不等于本人课程语义已验收。真实Windows通知既有用户可见确认。系统任务已根据用户授权注册并由服务启动成功，首次每日自然触发仍待观察。正式台账无模拟任务；用户真实课程通知、人工核对、耗时与字段准确性仍待提供。没有微信直连。当前新增界面验证见 `VERIFICATION_UI.md`。

## Streamlit入口（0.6.1）

`streamlit_app.py → web_ui.py → dashboard.py → 原有TaskService / NoticeWorkflow / ReminderRunner`。界面不直接改JSON，复用原子写入、备份、日志和版本检查。TaskService仅新增set_completed双向状态切换方法，旧complete入口保持兼容。

页面每次重绘读取文件快照；提醒区域只使用纯规划器，不能消费节点。真正提醒检查仅由明确按钮执行。通知计划存在当前会话中，勾选核对后才能提交；后台账变化时提交被拒绝。每次新预览产生新确认标识；取消和保存通过重绘前回调清除旧表单。通知/任务文本用纯文本或表格显示，不插入自定义HTML。

网页Key以密码框输入并存入当前会话的ApiSettings（repr隐藏Key），不写全局环境、JSON、缓存键或日志；保存后清空密码框。工厂仅在缓存未命中时创建客户端，同批重放在解析前被拦截。网页连接配置优先于环境变量；其他会话与CLI不会继承网页Key。默认仅监听127.0.0.1。

课程名单由用户登记课程及已有已确认课程构成，待确认课程不自动进入候选名单。AI将原文课程简称对应至名单内全名，Python校验原文证据、候选归属和待确认标记；原文全名可直接与名单核对。未对应、歧义、缺少证据或名单外结果保留待确认，不自动新增课程或持久化别名。旧自定义course_aliases可读取，但不覆盖已有课程名单。默认settings/parsing.json不再预置课程别名。

导航为主页面顶部的分段选择，保留原有会话page状态，取消折叠侧栏。标题/间距收紧，窄屏换行；业务按钮均保持原有服务调用。
