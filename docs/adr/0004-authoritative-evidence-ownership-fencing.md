# ADR-0004：权威 Evidence 使用数据库租约与 fencing epoch

- 状态：Accepted（仓库执行链已闭合；目标环境故障注入仍待验收）
- 日期：2026-09-02（生产收口复审）
- 工作项：`REALTIME-INTERVIEW-AGENT-001`

## 上下文

进程内 `LiveKitEvidenceIngressSupervisor` 可以保证单实例不重复订阅，但两个 API 进程仍可同时收到同一麦克风轨道。Redis Pub/Sub 只是通知 Adapter，无法证明哪个进程可以提交 final 或 CandidateAnswer，也无法阻止失去所有权的旧 STT 请求迟到返回。

## 决策

- 新增 provider-neutral `EvidenceOwnership` 持久事实，key 为 `organization_id + interview_id`。
- 租约时间只使用 transaction database clock；PostgreSQL Adapter 使用 `clock_timestamp()`。
- 首次 claim 创建 epoch 1；租约过期、条件释放或 owner 更换时 epoch 严格加一；同 owner 续租不改 epoch。
- 每次 control attach 另外递增 `control_generation`，旧连接命令和迟到 detach 失败关闭。
- Streaming STT 可以在事务外运行，但 `transcription.started/failed`、非答案 utterance 和 CandidateAnswer 提交事务必须先锁所有权记录并重新验证 `owner_instance_id + lease_id + epoch + expires_at`。
- Redis 不作所有权或命令真相。数据库 `EvidenceCommandJournal` 使用确定性命令 ID、请求指纹、control generation、owner fence、deadline、claim TTL 和安全 outcome；同 key 并发/重试只有一条事实，过期 claim 可由当前 epoch 重领。连接无关 owner executor 通过 DB polling 获取命令，Redis 只作低延迟 wake hint；remote controller 只等待持久 terminal receipt，不创建第二条 Evidence 链。
- Journal payload/result 使用逐类型 allow-list，禁止音频、转写、token、participant identity、私有 URI 和 Provider 对象。当前 owner 的幂等 effect receipt 必须先持久化，remote controller 才能投影完成；旧 epoch 的迟到结果不能覆盖 receipt。

## 备选方案

- Redis Redlock：拒绝。它不能与 CandidateAnswer 领域事务做同一个 fenced commit。
- PostgreSQL advisory lock：拒绝。连接生命周期和进程崩溃语义不适合需要可审计 epoch 的长生命周期媒体 owner。
- 仅依赖 InterviewSession version：拒绝。高频续租会与 Floor、评分、报告和事件竞争同一聚合版本。
- 粘性会话：拒绝为正确性保证；它只能优化路由，不能 fence 旧 owner。

## 结果

好处是即使短暂出现两个物理 subscriber，也只有当前 epoch 可以产生领域效果；候选人控制重连不再与媒体 owner 概念混合。代价是 PostgreSQL 成为正式 Evidence 提交的硬依赖，分区时选择暂停而不是双写答案。

当前 owner 按 capture revision 封存私有、带 checksum 的连续 segment/checkpoint；新 epoch 只能从完整 sealed prefix 执行 batch repair。浏览器 backfill 必须由服务端授权精确 sequence gap，每帧先私有落盘，journal 只引用 file ID/hash，再由当前 owner 去重注入。旧 revision 和未封存内存后缀不能冒充可恢复证据。

仓库合同已覆盖双 owner 竞争、旧 epoch/final/finish 拒绝、claim 重投、owner crash repair、backfill 重发与零重复 CandidateAnswer；正式模式已从 `embedded_singleton` 收敛为 `database_fenced`。多实例 production readiness 仍需目标 PostgreSQL/Redis/LiveKit/OSS 的真实并发与故障注入数据，缺少绑定当前 release 的签名 acceptance v2 report 时失败关闭。
