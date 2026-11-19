# 统一数据模型（第5轮验收版，schema_version = 1 兼容扩展）

三个 JSON 文件均为 UTF-8，顶层为对象；未知版本、额外/缺失字段会被拒绝。兼容例外是旧任务可省略 `review` 和 `deleted_at`，旧台账可省略 `processed_batches`；分别按空标记、null、空列表处理。旧文件初始化不重写，正常保存后才带上扩展。旧程序不支持这些新字段，不应降级混用。读入拒绝 JSON 重复键、NaN 和 Infinity。

`deadlines.json`：`{"schema_version": 1, "tasks": []}`。任务数组的每项如下；程序通过 `Task.to_dict()` / `Task.from_dict()` 双向转换。

| 字段 | 类型 | 约定 |
| --- | --- | --- |
| `id` | string | 自动生成 UUID4，生命周期内不变；台账中唯一 |
| `course` | string | 非空课程名称，去除首尾空白 |
| `title` | string | 非空作业名称，去除首尾空白 |
| `description` | string | 作业内容，允许空字符串 |
| `deadline.date` | string / null | `YYYY-MM-DD`；未知时为 null |
| `deadline.time` | string / null | `HH:MM` 或 `HH:MM:SS`；日期精度/未知时为 null |
| `deadline.utc_offset` | string | 固定时区偏移 `±HH:MM`，最大 ±14:00，默认 +08:00 |
| `deadline.precision` | string | `unknown` / `date` / `minute` / `second` |
| `deadline.needs_confirmation` | boolean | unknown/date 强制为 true；精确时间也可人工设为 true |
| `submission.method` | string | 提交渠道/方式；未填写为空，不猜测 |
| `submission.format_requirements` | string | PDF、文件命名等要求；未填写为空 |
| `source.raw_notice` | string | 人工提供的原通知，保留正文与换行；未提供为空 |
| `source.sent_at` | string / null | 通知发送时间：仅知日期时为YYYY-MM-DD；确知钟点时为带时区的ISO 8601；未知为null。日期不补为00:00，旧时间戳兼容 |
| `status` | string | `pending` / `completed` |
| `created_at` | string | 创建时间，UTC ISO 8601，微秒精度 |
| `updated_at` | string | 最近修改时间，不早于创建时间 |
| `completed_at` | string / null | 完成时刻；pending 必须为 null，completed 必须有值 |
| `history` | array | 按时间顺序记录创建与修改，至少一项 |
| `deleted_at` | string / null | 软删除操作时间；默认null，保留原状态，不物理删除 |
| `review.needs_confirmation` | boolean | 是否存在待确认字段；必须与下面列表是否为空一致 |
| `review.uncertain_fields` | array[string] | course/title/description/deadline/submission.method/submission.format_requirements |
| `review.notes` | array[string] | 歧义、缺失信息、校历范围与模型警告，确认导入后仍保留 |

缺少课程时保存明确占位“待确认课程”，并标记 `course`，不会当成已识别课程。缺少要求保留空字符串并标记对应字段。人工同意导入不等同于信息已核实，不会清除不确定标记；`show` 和 `list --json` 可完整查看，普通 `list` 的“待确认”包含时间与其他字段。

`review` 记录待核实信息。第4轮人工 `set-deadline` 明确填写分钟/秒且不标待确认时，同时移除 `uncertain_fields` 的 deadline；原说明及其他字段不清除。仅日期或显式待确认仍不产生精确倒计时。修改、标记清除均进入同一历史，可撤销恢复。

0.7.4用户明确采用“只有日期时默认当天23:59”的个人约定。AI解析满足条件时保存为minute精度，review.notes说明该钟点来自用户默认而非原文；仍不改变source。已有任务的约定应用通过deadline_changed保存完整前后快照，清除仅因缺钟点产生的deadline标记，保留其他待确认字段；支持撤销。日期有歧义或已有明确钟点时不能用该规则覆盖。

`history` 每项包含 `at`、`action`、`changes`：

- `created`：保存初始状态由 null 变为 pending。
- `completed`：保存状态 pending → completed。
- `deadline_changed`：保存 deadline 整个对象的 before/after，包括精度与待确认标记。
- `task_updated`：通知确认后稀疏更新，保留字段差异、原因、原文及完整字段快照。
- `task_deleted`：确认后的软删除。
- `undone`：撤销最近修改，恢复字段并追加历史；不会删掉被撤销的记录。

第3轮新修改在 `changes._before` / `_after` 保存业务字段快照（不包含嵌套历史、ID及创建/最近修改时间），`changes.notice` 保存修改通知（若有），`reason` 保存匹配或恢复说明。任务顶层 `source` 始终是最早原通知，后续通知在历史中保留。恢复后的 `updated_at` 为实际恢复时刻，不倒填为旧时间。前两轮只有字段 before/after 的简单日期/完成历史仍支持撤销。

台账可选 `processed_batches` 数组，每项为：`id`（规范化通知文本+实际发送时刻的 SHA-256）、`at`（处理时间）、`task_ids`（关联任务UUID列表）、`outcomes`（new/duplicate/update列表）。凭据与任务在同一次原子写入中保存，ID不可重复、关联任务必须存在。普通修改/完成/删除/撤销均保留这些凭据；撤销不会让旧通知再次自动生效。

这些历史由应用服务写入，同一次修改同时保存在台账中。首项时刻必须等于 `created_at`，末项时刻必须等于 `updated_at`。原通知不因手动修正截止时间而改变。历史是本地操作记录，不是防篡改审计账本，也不构成真实使用证明。

`courses.json`：`{"schema_version": 1, "courses": []}`。每门课程含 `id`（UUID）、`name`（非空）、`created_at`（带时区时间）；ID 与课程名称（忽略英文字母大小写）均不得重复。课程目录与任务的课程字段独立，本轮不做跨文件同步。

`config.json`：

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `schema_version` | 1 | 文件版本 |
| `timezone_name` | Asia/Shanghai | 人类可读说明，不调用 IANA 时区数据库 |
| `utc_offset` | +08:00 | 新录入截止时间使用的固定偏移 |
| `backup_keep` | 20 | 各文件独立保留 1～1000 份备份 |
| `ai_enabled` | true | 第2轮允许布尔值；false 禁用 parse 和 AI 批次导入，人工任务功能仍可用 |
| `reminders_enabled` | false | 布尔开关；新目录默认关闭，当前项目已显式启用，不代表已经注册系统任务 |
| `reminder_preferences` | 见下文 | 第4轮兼容可选字段；旧文件读取补默认值，不因初始化改写 |

`reminder_preferences` 包含：`lead_days`（默认[3,1]，0～365唯一整数）、`daily_check_time`（默认09:00，HH:MM）、`channel`（auto/terminal，默认auto）、`course_overrides`（默认{}；完整课程名到{enabled,lead_days}映射）。更改daily_check_time只改配置，Windows任务触发器须另行人工确认更新。

仅日期排序的00:00临时键不写回任务、不视为真实截止时刻。提醒只对已确认分钟/秒级日期判断逾期；未知/日期精度或review.deadline不确定的任务只提示补充信息。

## 提醒状态与日志

`reminders.json` 首次实际检查时创建，结构为 `schema_version=1`、`receipts` 对象、可空 `last_check_at`、可空 `last_run`。dry-run不创建/消费状态；损坏状态拒绝重建后重发。原任务台账结构不受影响。

每项receipt以任务ID、截止计划指纹和节点的SHA-256为键，包含task_id、revision、node、planned_at、recorded_at、run_id、status、delivery。节点为lead:N / overdue / needs_deadline；状态为reserved / delivered / fallback / superseded / unknown。没有变化的任务不会因更名或重启生成新节点；截止变化生成新指纹，撤销到已提醒过的原指纹不会重复发送。

`last_check_at` 保存已执行检查时间，`last_run` 保存run_id、状态、日志路径、来源及结束信息；补检按当前时间和未消费节点计算，不因电脑关机错过精确分钟而永远漏掉。状态不包含API Key。

每次独立JSONL运行日志区分实际observed_at和effective_now，记录simulated/dry_run、计划、送达结果、结束/失败。Windows通知API提交或显示事件并不能自动证明用户看到，`user_visible` 默认null；本次实际测试后的用户确认另作追加日志证据。包装器的独立日志保留完整输出及退出码。

AI 客户端的结构化抽取响应不是正式台账格式。Schema单一来源为 `course_assistant/schema.py` 的PARSE_SCHEMA。支持服务端json_schema，或明确配置的json_object（DeepSeek诊断路径）；两种模式都经过完全相同的本地字段/类型/枚举/额外键/数组长度校验，随后检查原文证据与时间一致性，再转换TaskDraft。ID、状态、时间戳与历史均由Python生成，模型不能指定。

第5轮局部修改对实际变化字段传播新的不确定标记；本人显式提供已核实完整日期时可清除deadline标记。合法UTF-8 BOM文件可读，读取不改写原字节；仍拒绝重复键、NaN、未知字段和非法日期。

课程别名及教学周锚点在独立的 `settings/parsing.json`，不在三份业务台账内。它只由用户维护并在解析前校验，不经程序自动修改；三文件备份命令不包含此静态配置。密钥、模型名、接口地址均只读环境变量，不写入任何 JSON。
