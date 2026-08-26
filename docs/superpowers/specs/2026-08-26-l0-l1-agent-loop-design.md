# L0/L1 Agent Loop 设计

> 日期：2026-08-26
> 状态：已实现
> 范围：cognition 路由、云端 Agent Loop、回复过渡、中断、偏好记忆与 TTS 交互契约

## 目标

将固定的本地多路由管道收敛为：

1. L0 本地守门员：仅判断 `local` / `cloud`。
2. L1 云端循环：在时间和步数预算内完成模型调用、工具执行和最终回复。
3. 语音回合可发布 transition，并能在新 ASR utterance 到达时取消过期回合。

代码包名仍保留 `cognition/l2/`；本 spec 中的 L1 指 `AgentLoop`。

## L0 契约

- `LocalRouter` 返回 `GateRoute.LOCAL` 或 `GateRoute.CLOUD`。
- 危机关键词和显式偏好/纠正在规则层直接路由到 cloud，不调用本地分类模型。
- 低置信度、非法 JSON 或本地模型失败均 fail closed 到 cloud。
- `Intent`、本地工具路由、视觉路由和可扩展路由注册表不再属于 L0。

## AgentLoop 契约

- 每轮最多执行 `max_steps`，总时间不超过 `max_duration_s`。
- 每次模型调用的 `timeout_s` 必须使用当时的剩余预算；压缩耗时必须从该预算中扣除。
- 压缩前剩余预算不足 `SUMMARIZE_TIMEOUT_S` 时跳过压缩。压缩尝试后重新检查 deadline 和 interrupt，再调用主模型。
- 模型调用前后、transition 发布前后、每个工具调用前后、final 返回前都检查 interrupt/deadline。
- 工具参数必须是 JSON object；非法参数以 `invalid_tool_arguments` 结构化错误回填，不 dispatch。
- 工具结果序列化后限制为 `tool_result_max_chars`。
- 摘要模型返回空内容视为 `CloudError`，不生成空压缩节点。

## 危机模式

- `crisis=True` 使用专用 system prompt，不向模型暴露 tools schema，不发布 transition。
- 即使模型仍返回 `tool_calls`，也必须回填 `crisis_tool_calls_blocked`，绝不 dispatch。
- 危机回合失败、中断或返回空文本时，Hub 发布静态安全兜底；这是“中断后不发 final”的明确例外。

## 记忆与 persona

- 云端 `memory.write` schema 仅接受 `sensitivity=0`。
- 仅显式、可长期复用且非敏感的偏好/纠正可写入。健康、身份、财务等敏感内容不持久化。
- 删除 Sedimenter 的隐式多信号推断。persona 按 `refresh_every_utterances` 周期重建，并只使用云端可见的公开记忆。
- Tuner 只调整主动开口 cooldown；重复负反馈会提高并持久化 floor。

## 中断与回复协议

- `REPLY.kind` 取 `transition` / `final` / `cancel`，缺省为 `final`；同一回合共用 `reply_id`。
- 探针只接受有限数值 timestamp，用 `max(ts)` 记录，不取 decision lock。
- 仅语音回合（`publish_reply=True`）传入 `interrupt_check`；Gateway chat 不会被桌面语音静默打断。
- 已发 transition 的回合被中断时，Hub 发布同 id cancel；未发 transition 时保持静默。
- Gateway 历史仅将 final/缺省 kind 记为 assistant turn；Recorder 仍保留全部原始事件。

## TTS 调度

- transition 不得中断 active/pending final。
- 同 id final 到达时，pending 或合成中 transition 立即失效，迟到音频不播放。
- 只有已开始播放的同 id transition 可自然收尾至多 `transition_grace_s`，超时后停止并播放 final。
- cancel 只影响匹配非空 id 的 transition，不影响 final。

## 已知限制

- 同步工具和 GPU 合成不能安全强杀；实现保证过期结果不再发布或进入后续步骤。
- 当前中断信号来自完整 ASR utterance。TTS 播放期间 ASR 会暂停，因此本期不支持真正的 VAD barge-in；后续需要从 MIC/VAD 语音起始事件先停止 TTS。
