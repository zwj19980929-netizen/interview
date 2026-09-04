# ADR-0003：本地 VRM 作为自研数字人正式表达路径

- 状态：accepted，授权美术资产与质量验收仍为 environment_pending
- 日期：2026-09-02（生产收口复审）
- 决策者：Interviewer 工程团队

## 背景

静态肖像、CSS 音量柱和按音量开合嘴无法证明数字人在表达具体音素，也不能自然支持眨眼、注视、倾听、思考、打断和告别。正式面试要求低延迟可中断表达，并且 Avatar 不得自由聊天、改写题目或参与评分。

## 决策

自研正式路径采用拥有合法商业使用权的 VRM 1.0 专属模型，由 Three.js 与 @pixiv/three-vrm 在候选人浏览器本地渲染。唯一输入是服务端持久化的 ApprovedConversationAct 及其 AvatarPerformance：短期私有音频 URI、统一音频时钟、交付类型、15 个强类型 viseme cue、gesture cue 和 performance ID。播放、barge-in 和恢复均以该时钟与 ID 为准。

模型必须包含 15 个 viseme、眨眼、注视、呼吸、点头、倾听、思考、打断和告别；题目优先使用预生成音频/时间戳，动态话语使用流式 TTS 时间戳，缺失时进入本地 G2P 对齐 seam。VRM 表情采用 [VRM 1.0 expression 规范](https://github.com/vrm-c/vrm-specification/blob/master/specification/VRMC_vrm-1.0/expressions.md)，运行时采用 [three-vrm](https://github.com/pixiv/three-vrm)。

## 后果

- 口型、动作、打断和媒体时钟归一为可测试合同，Avatar Provider 不拥有对话决策。
- 浏览器承担 WebGL/GPU 成本，正式预检要求目标设备至少 30 FPS，并对 context loss 失败关闭。
- 必须管理模型授权清单、asset hash、版本、表达映射和客户端 bundle 体积。
- 云 Avatar 可作为实现同一 AvatarPerformance 的可选 adapter，但不能成为静态图或自由回复降级。

## 验收与降级

正式候选人页面不得引用静态面试官图片或 CSS 假口型。资产、商用许可 manifest、VRM 1.0、hash、15 个表情、30 FPS 或音频任一缺失时暂停/人工接管；仅音量开合嘴只可作为明确标注的无障碍模式，不能通过“自研数字人 ready”。目标验收为音频/viseme p95 偏差小于 80ms、说话冻结不超过 500ms。

仓库已实现对真实 GLB JSON chunk 的 VRM 1.0 结构检查，要求 15 个自定义 viseme（含 `sil`）、blink、lookAt 与基础 humanoid bones；license manifest 还必须匹配 hash/spec，并显式给出 commercial use、肖像授权、license ID 和权利人。浏览器 loader 重复执行同一表达/注视 gate，真实 VRM 的连续 FPS 窗口会覆盖邀请页的基础 WebGL 探测值。

动态 TTS 与逐字获批的 S2S 音频先写入私有 AgentExpressionAudio，领域只保存 `agent-expression://file_id`，每次角色安全投影才签发短期地址；生产对象必须通过存储端加密核验。当前确定性字符对齐器只用于合同基线；专属商用 VRM、真实中文/英文 G2P 或 TTS 时间戳、目标设备测量尚未提供，因此 production readiness 保持关闭。

## 被拒绝方案

- 静态图片叠加声音波形或 CSS 嘴型：不是可验证的数字人表达。
- Avatar Provider 自由生成回答：绕过受控对话动作与证据 gate。
- WebGL 失败后退回问卷：掩盖致命故障，破坏完整面试体验。
