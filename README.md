# Yuki Agent

Windows 上的纯语音陪伴 agent（开发中）。面向浏览/阅读场景的感知与陪伴。

## 架构

多进程分层 + 本地消息总线（ZMQ PUB/SUB + REQ/REP，`bus_server` 充当枢纽）：

```
supervisor                    进程生命周期管理：拉起/健康检查/重启子进程
├── bus_server                消息总线枢纽（BusHub），可选内嵌 HTTP/WS Gateway
├── perception                采集层：屏幕捕获(WGC)、文本提取(OCR/DOM/UIA)、
│                             前台监控、音频采集、唤醒词检测
├── cognition                 认知层：VAD/语音缓冲、STT(SenseVoice-Small)、
│                             屏幕理解(快/深两档)、决策脑、记忆、灵魂/人格
└── interaction               交互层：热键触发、控制台 TTS
```

核心模块：
- `src/yuki/bus_server` — 消息总线 + Gateway（`gateway.enabled` 开启）
- `src/yuki/perception` — 屏幕/音频/文本采集
- `src/yuki/cognition` — 语音识别、屏幕理解、决策、记忆
- `src/yuki/interaction` — 对话触发与输出
- `src/yuki/memory`、`src/yuki/recorder`、`src/yuki/supervisor`、`src/yuki/health`

### ASR 全链路

`音频采集(20ms帧)` → `唤醒词检测(openWakeWord)` → `VAD(webrtcvad)` →
`STT(SenseVoice-Small)` → 结果发布 `event/perception/user_utterance`。
识别会话带状态机（idle/listening/speaking/processing）：唤醒后无语音超时回落、
一轮回复后进入续听窗口、过期 STT 结果丢弃。唤醒词与桌面 Gateway 均默认关闭。

## 安装

```bash
pip install -e ".[dev,windows]"   # 标准开发环境
pip install -e ".[ml]"            # 追加 VLM/STT 模型推理依赖
pip install -e ".[asr]"           # 追加唤醒词检测（openWakeWord）
pip install -e ".[desktop]"       # 追加 HTTP/WS Gateway（FastAPI/uvicorn）
```

## 运行

```bash
cp config.example.yaml config.yaml   # 按需调整，环境变量 YUKI_<SECTION>_<FIELD> 可覆盖

python -m yuki.supervisor            # 一键启动全部子进程
# 或单独调试：
python -m yuki.cognition
python -m yuki.interaction --trigger-after 2
python -m yuki.memory list           # 记忆管理 CLI
```

启用唤醒词：`config.yaml` 中 `wake_word.enabled: true` 并配置 `model_path`（自训的
"yuki" onnx 模型）；启用桌面 Gateway：`gateway.enabled: true`（REST `:8765`）。

## 配置

复制 `config.example.yaml` 为 `config.yaml`。主要分区：`bus`、`supervisor`、
`memory`、`vlm`、`cloud`、`wake_word`、`gateway`、`persona` 等。密钥走环境变量，
如 `YUKI_CLOUD_API_KEY`；不要提交本地 `data/`、`logs/`。

## 测试

```bash
pytest                        # 单元测试
pytest -m e2e                 # 端到端集成测试（spawn 真实进程）
```

## 文档

- 整体设计：`docs/superpowers/specs/2026-08-10-yuki-agent-design.md`
- ASR 全链路：`docs/superpowers/specs/2026-08-23-asr-fullchain-design.md`
- 桌面 Gateway：`docs/superpowers/specs/2026-08-23-desktop-gateway-design.md`
