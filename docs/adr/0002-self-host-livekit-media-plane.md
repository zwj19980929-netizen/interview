# ADR-0002：自托管 LiveKit 作为实时面试媒体面

- 状态：accepted，生产落地仍为 environment_pending
- 日期：2026-09-02（生产收口复审）
- 决策者：Interviewer 工程团队

## 背景

完整实时面试需要同一媒体面承载候选人麦克风、按同意决定是否发布的摄像头、企业监看/人工接管、TURN 和分轨私有录像。业务系统仍须独占发言权、权威转写、追问、评分、同意、留存和审计。旧双 WebSocket PCM 可以完成本地闭环，但不能自然提供视频、NAT/TURN、分轨订阅与 Participant Egress。

## 决策

采用自托管 LiveKit 作为 remote-but-owned WebRTC/SFU media-plane adapter。候选人、数字人和人工接管者使用独立 participant/track；应用签发 60 秒一次性 Agent ticket 和短期、限房间/限 microphone/camera 的 LiveKit grant。未同意视频时 camera grant 必须为空。经明确同意的录制使用 Participant Egress 写入私有存储，InterviewMediaCapture 保存状态、hash、存储保护、可选加密、保留期与审计。

本地和生产的差异只存在于 `LiveKitMediaPlane + PrivateFileStorage` seam：`development + INTERVIEWER_LOCAL_MEDIA=true` 使用固定开发凭据和容器 `/recordings`，适配层将 Provider 返回的绝对路径规范化回逻辑 object key，本机 adapter 再验证目录边界、对象存在性与完整性；production 禁止该开关并只接受私有 OSS AES256/KMS 证明。这样业务服务无需分支，也不会把“本地可写”误称为“已加密”。仓库内 Compose 配置只用于开发，不是生产 TURN/Secret/容量方案。

面试业务状态仍由 InterviewAgentRuntime 与 InterviewSessionLifecycle 管理。正式权威音频由 receive-only 服务器 participant 关闭自动订阅，只选择服务端冻结 candidate identity 的 microphone audio publication，并将 16 kHz/mono/20 ms PCM 按序送入私有录音与 Evidence/STT 链；WebSocket 只传控制/事件，PCM 仅保留为显式 compatibility adapter。[LiveKit 自托管](https://docs.livekit.io/transport/self-hosting/)和 [Participant Egress](https://docs.livekit.io/transport/media/ingress-egress/egress/participant/)是选型依据。

subscriber 生命周期不得绑定 Agent control WebSocket。当前实现以 `authoritative_media_binding` 冻结 room/identity，并在控制断开后保留同一 subscriber、StreamingSTTSession、录音和 endpoint timer 30 秒；新控制连接只能接管命令。音频 sink 使用显式有界泵，积压时暂停面试而不是接受 LiveKit 正容量队列的静默旧帧丢弃。

## 后果

- 获得 WebRTC、SFU、TURN、按角色订阅和分轨录像，同时保持业务供应商无关。
- 部署必须运营 LiveKit、TURN、Egress、私有对象存储、凭据轮换和容量监控。
- 房间可连接不代表权威证据链可用；还必须通过 receive-only RTC 探针、单一 owner gate、重连/去重/补传和持久录音修复测试。
- 录像场次不能静默降级为无视频；必需 Egress 失败时暂停或人工接管。

## 当前落地与未决门禁

仓库已实现最小权限票据、浏览器发布/企业订阅、Egress adapter、签名 webhook、capture 状态/hash/存储保护核验、receive-only candidate microphone subscriber、连接独立 Evidence/STT、30 秒 control reconnect grace 和失败暂停；并提供固定版本的本地 LiveKit/Egress/Redis Compose 栈与单参数默认配置。正式票据只在 `INGRESS_ENABLED + MODE=database_fenced + SDK` 与原生 RTC healthcheck 同时通过时返回 `livekit_server_subscriber`；否则 admission/readiness 失败关闭，不能以 WebSocket PCM 冒充正式路径。

数据库时钟 ownership lease、单调 fencing epoch、control generation、CandidateAnswer commit fence、持久命令 journal、连接无关 owner executor、DB polling/Redis wake hint、remote receipt proxy、持久媒体 segment/checkpoint、owner-loss batch repair 与浏览器授权 gap backfill 已接成同一 interface。Egress `starting` 的未知崩溃窗口不会盲目再次 StartParticipantEgress：若 ListEgress 无法认领原任务则记录 `egress_start_outcome_unknown` 并暂停，避免重复或孤儿录像。

仓库合同完成不代表本 ADR 已通过目标环境落地。仍须在目标 PostgreSQL/RLS、Redis、LiveKit/TURN/Egress、私有 OSS 和真实浏览器上执行多实例并发、进程故障、30 秒媒体中断、重投去重、上行隐私与硬 SLO 验收，并用绑定当前 release 的 acceptance v2 报告放行。

## 被拒绝方案

- 继续扩展多条 WebSocket 承载正式音视频：缺少成熟 NAT/TURN、带宽控制、分轨和媒体运维语义。
- 托管 AI Agent 直接拥有对话和评分：破坏领域真相、审计和供应商无关边界。
- 录像失败后无提示继续：违背明确同意与完整证据要求。
