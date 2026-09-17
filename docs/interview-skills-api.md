# 可选 Skill API（039）

## 2026-09-10 · 面试定制入口（044）

工作台不再要求从Skill库逐场选择。新的`GET/PATCH /api/v1/interview-customization`提供组织默认Skill正文与独立企业资料编辑；请求、清空、默认继承和并发合同见[接口设计](api-design.md)。旧Skill API保留不可变revision、显式工具范围与历史授权语义；自由正文保存仍无需审批。默认Skill停用或撤销后新计划略过，页面可读取原正文并明确显示状态；公司资料不受其状态影响。

Skill 是用户自由编写、按需启用的指令扩展。企业级可靠性、权限、评分与证据要求由 Agent 运行时承担；使用面试官不要求配置 Skill。企业资料 `company_context` 独立于 Skill，计划请求见[接口设计](api-design.md)。

## 自由包 interview_skill.v2

`POST /api/v1/interview-skills` 接收 `{package}`。省略 `package.schema_version` 默认 `interview_skill.v2`；显式旧版本才走下方 v1 兼容合同。新包仅必需 `name` 和 `instructions`：

| 字段 | 合同 |
| --- | --- |
| `schema_version` | 固定 `interview_skill.v2`，可省略 |
| `name` | 去除首尾空白后非空，最多80字符 |
| `instructions` | 自由文本/Markdown，非空白，最多6000字符；原文及首尾空白原样保存 |
| `description` | 可省略，默认空字符串，最多400字符 |
| `resources` | 可省略，默认空数组，最多6份；每项仅 `id/title/content` |
| `resources[].id` | 唯一本地身份，匹配 `^[a-z][a-z0-9_-]{0,47}$` |
| `resources[].title` | 非空，最多100字符 |
| `resources[].content` | 非空白，最多4000字符；原文保存 |
| `allowed_tools` | 可省略或null，表示沿用平台工具；提供数组时为显式工具范围，最多6项且唯一 |

工具名称为 `questions.search`、`questions.read`、`resume.read_evidence`、`company.read_reference`、`interview.read_context`、`specialists.consult`。缺省工具权限不会关闭总控理解或追问能力；实际可用工具仍由本场资料、计划和运行时权限共同限制。显式空数组只收窄可选工具。包中不再提供固定语言、交流风格或访谈方法枚举。

所有对象禁止额外字段，规范化JSON包总量不超过24000 UTF-8字节。普通链接、Markdown、代码块以及描述任何话题的文字均可保存；没有关键词、URL或代码围栏禁写规则。服务只保存/编译文本，不执行代码，不自动抓取链接，不把文本内容转换为新增权限。不存在资源名称的普通Markdown文字也不会触发网络访问。

创建返回 `status:active`、`version:1`、`revision:1`；保存即可冻结或加载。`POST /{id}/revisions` 接收 `{expected_version,package}`，创建新的active内容版本并推进聚合CAS版本，已有计划继续绑定原版本。**新包不需要validate/approve**，这两个端点仅保留旧包兼容。

GET列表返回元数据；GET `/{id}` 与 `/{id}/revisions/{revision_id}` 返回精确正文和版本元数据。正文详情沿用 `package`、`content_status`，正文不可读时返回 `package:null`、`content_status:unavailable`，仍保留紧急停用入口。元数据包含 `schema_version`、`activated_at`、`revision_id`、`revision`、聚合 `version`、`content_hash`、`compiler_version`、`authorization_epoch` 和实际 `allowed_tools`。

active版本可通过原 `POST /{id}/retire`（`expected_version,revision_id?,reason`）停止新计划使用，既有冻结快照继续；通过 `POST /{id}/revoke` 紧急撤销并推进epoch、原子暂停受影响会话。`POST /{id}/revoke-delivery` 可重试已撤销版本的通知。没有新增审批状态；删除仍仅适用于旧合同未发布历史，已可使用的版本保留可追溯记录。

租户来自认证上下文，管理员/面试官角色、CAS、加密、私有投影、审计及撤销事务保持不变。`freeze_snapshot` 接受active或旧approved；批准新计划时 `verify_current_authorization(...,for_new_plan=True)` 同样复核这两个状态，已有会话还可使用retired，revoked始终拒绝。

编译器按包版本路由：v2使用 `interview_skill_compiler.v2`；旧v1保留原 `enterprise_interview_skill_compiler.v1` 实现、原Prompt和规范化hash，已冻结旧快照继续加载，不能将新版Prompt冒充旧版本。新版本改写不替换既有快照。旧v1记录缺少新元数据 `schema_version` 时，读取投影识别为v1。

部署须执行 `004_optional_interview_skills.sql`，它在保留003历史语义的基础上增加active状态及其v2版本约束，继承现有租户RLS与唯一版本索引。PostgreSQL启动校验要求004标记约束存在；应用运行角色不自行执行DDL。

## 旧包 enterprise_interview_skill.v1 兼容合同（038）

以下写作约束及draft/validated/approved步骤仅用于显式v1旧包和旧客户端兼容，不适用于正常新建的v2自由Skill。旧版本可读取、按原编译器加载及停用；编辑界面可将旧正文保存成新的v2版本。

本合同先于实现登记。Skill 是受控声明式 JSON 包，修改正文始终创建新 revision，既有计划继续引用原 revision。 新草稿计划已经冻结精确批准版本，批准计划时再次复核该版本授权；审核过程中发布新版本不会替换待审计划引用。正文和资源以字段密文存储；列表、审计和冻结快照不包含正文。`version` 是 Skill 聚合 CAS 版本，`revision` 是不可变内容版本，两者语义不同。

所有接口位于 `/api/v1/interview-skills`，需要企业管理员或面试官角色。组织与操作者由受信认证上下文提供，不接收客户端的组织字段。候选人凭证没有这些接口的访问权。

| 方法 / 路径 | 请求 | 返回 |
| --- | --- | --- |
| GET `/` | 无 | 标准 collection envelope，当前 revision 元数据 |
| POST `/` | `{package}` | 新 Skill 详情；状态 draft |
| GET `/{skill_id}` | 无 | 元数据、当前 package、全部 revision 元数据 |
| GET `/{skill_id}/revisions/{revision_id}` | 无 | 精确历史版本正文、元数据和当前聚合version，供人工审核 |
| POST `/{skill_id}/revisions` | `{expected_version, package}` | 新 draft revision；旧版本不变 |
| POST `/{skill_id}/validate` | `{expected_version, revision_id?}` | 结构/资源/政策检查通过后 validated；不代表模型质量实测 |
| POST `/{skill_id}/approve` | `{expected_version, revision_id?, reason, review_confirmed:true}` | 明确人工审核后 approved |
| POST `/{skill_id}/retire` | `{expected_version, revision_id?, reason}` | 停止新计划使用；现存批准快照可继续 |
| POST `/{skill_id}/revoke` | `{expected_version, revision_id?, reason}` | 紧急撤销并推进该 revision 的 authorization_epoch |
| POST `/{skill_id}/revoke-delivery` | `{expected_version, revision_id?}` | 重发已撤销版本的安全暂停快照并重试媒体清理，不再推进epoch |
| DELETE `/{skill_id}` | `expected_version` 查询参数 | 仅无任何批准历史的全 draft/validated Skill 可删除 |

`revision_id` 缺省取当前版本；提供该字段可撤销旧版本。所有修改在一个租户事务内提交 CAS、版本事实与不含正文的审计。404 不区分跨组织资源和不存在；CAS/非法状态为409，无效包为422，撤销授权为409。

详情包含 `content_status: available|unavailable`。管理员读取详情或执行撤销/通知重试时，正文密文损坏、解密或内容hash校验失败会返回 `package:null` 和 `content_status:unavailable`，保留元数据、版本历史和撤销入口，不让当前草稿正文损坏阻断旧批准版本的紧急撤销。精确历史详情只读取该版本正文。校验、批准、编译及模型加载仍失败关闭，不能把不可读内容送入模型；前端禁用不可读版本的编辑/校验/批准并保留紧急撤销及通知重试。

包 Schema：`schema_version` 固定 `enterprise_interview_skill.v1`；`name` 1–80、`description` 1–400、`language` 为 `zh-CN/en-US`；`style` 为 `professional/warm/concise`；`interview_method` 为 `evidence_based/star/project_deep_dive`；`candidate_address` 1–30；`instructions` 1–6000；`allowed_tools` 为只读工具白名单（`questions.search`、`resume.read_evidence`、`company.read_reference`、`interview.read_context`、`questions.read`、`specialists.consult`）；`resources` 最多6个，每个仅 `id/title/content`，ID为本地标识符，正文1–4000；整个规范化包最多24000 UTF-8字节。所有对象禁止额外字段，所有字符串禁止空白内容、控制字符、脚本标记、远程URL和本地文件URI。工具项/资源ID唯一。资源作为资料而非权限或平台指令。引用只允许 `[[resource:ID]]`，必须可解析。

结构校验不是语义安全或听感验收。静态政策检查只拒绝显式越权、泄题、保护属性评价、自动录用/淘汰及覆盖平台规则等已知危险指令；批准命令要求人类已审阅指令、资料和企业使用场景。生产试点评测仍独立进行。

撤销使用与实时提交相同的锁序：先按ID锁定绑定该Skill的未完成会话，再锁Skill/revision。绑定被撤销精确 revision 的进行中会话通过 `InterviewSessionLifecycle.PAUSE` 暂停；计划中/等待/已暂停会话保留合法生命周期状态并记录 `skill_authorization_blocked`。全部受影响会话清除活动表达选择并将 floor 设为 none，返回 `affected_session_ids`，不执行媒体I/O。撤销epoch、会话改变和审计整体提交；PostgreSQL发生事务冲突时转为409，整体回滚后按CAS重试，不能显示已成功撤销。并发新建会话通过Skill锁及后续start/commit授权复核失败关闭。运行时必须在表达、工具和提交边界再次检查epoch，响应后可立即关闭本机媒体并发布安全快照；异步通知不替代持久授权门禁。

HTTP撤销成功后通过既有Redis Agent事件发送安全 `session.snapshot`，接收实例按数据库撤销事实停止本机在途AI表达/语音采集，浏览器收到paused状态立即停止播放。结果中的 `revocation_delivery` 分别报告每个会话的本机清理与跨实例发布状态；`published`只表示送到事件总线，不冒充所有远端已确认。媒体/网络失败保留已提交撤销，返回200及失败状态并保存metadata审计，可通过 `revoke-delivery` 重试通知。运行时单会话审计与路由汇总审计分别返回 `runtime_audit_status` 和 `delivery_audit_status`；审计失败标记 `failed` 并保留已经成功的发布结果，不回滚授权。已结束输入但仍在播放收尾话语的会话也清除播放，不改变其报告生命周期。

内部接口：`freeze_snapshot(tx, skill_id, revision_id=None)` 只接受 approved，返回 `skill_id/revision_id/revision/content_hash/compiler_version/authorization_epoch/allowed_tools`。`verify_current_authorization(tx, snapshot, for_new_plan=False)` 必须与决策提交共享事务；默认允许 approved 或 retired，拒绝 revoked、hash/epoch/身份/权限不匹配；新准入传 `for_new_plan=True`。`load_compiled(snapshot, organization_id)` 在读取事务再次检查授权并核验密文解密后的内容hash，返回受控 `instructions/resources/allowed_tools`。加载成功不能替代后续效果提交时复查。批准现有计划、开始/恢复、工具效果与表达边界由调用方复核快照，不将新 revision 热替换到活动会话。

HTTP路由只负责请求转换、角色检查和服务调用。`InterviewSkillService.revoke_with_delivery` 与 `retry_revocation_with_delivery` 统一负责先提交撤销事实、再通知、隔离媒体/传输故障及审计结果；服务支持注入异步 `revocation_publisher(interview_id, organization_id)`，默认运行时直到提交后才构造，避免供应商初始化故障阻断撤销。
