# Yuki Agent

Windows 上的纯语音陪伴 agent（开发中）。面向浏览/阅读场景的感知与陪伴。

## 架构

运行时采用“单主进程 + 模型进程”双进程架构。主进程内使用内存事件/服务调用，
ZeroMQ 只保留在模型调用、健康探活和外部兼容边界：

```
supervisor                    进程生命周期管理：独立探活、退避和重启
├── yuki                      主进程（python -m yuki.app）
│   ├── LocalRuntimeBus       perception / cognition / interaction 的进程内通信
│   ├── BusHub + 兼容桥       跨进程及旧 wire 契约
│   ├── perception            屏幕、文本、音频、唤醒词
│   ├── cognition             VAD、决策、记忆、上下文、云桥
│   ├── interaction           热键、TTS 播放控制
│   └── Gateway（可选）       内嵌 HTTP/WS 服务
└── model_worker              VLM / STT / 本地脑 / TTS / embedding
                              模型生命周期、显存准入和异步管理操作
```

核心模块：
- `src/yuki/app` — 主进程装配与统一生命周期
- `src/yuki/runtime_bus.py` — 进程内事件总线和服务注册表
- `src/yuki/model_worker`、`src/yuki/model_client.py` — 模型进程及边界客户端
- `src/yuki/bus_server`、`src/yuki/bus_bridge.py` — 边界总线、兼容桥和 Gateway
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

python -m yuki.supervisor            # 推荐：启动 yuki + model_worker 并负责探活/重启
python -m yuki.app                   # 仅启动主进程（需要另行启动 model_worker）
python -m yuki.model_worker          # 仅启动模型进程
# 或单独调试：
python -m yuki.cognition
python -m yuki.interaction --trigger-after 2
python -m yuki.memory list           # 记忆管理 CLI
```

启用唤醒词：`config.yaml` 中 `wake_word.enabled: true` 并配置 `model_path`（自训的
"yuki" onnx 模型）；启用桌面 Gateway：`gateway.enabled: true`（REST `:8765`）。

## 配置

复制 `config.example.yaml` 为 `config.yaml`。主要分区：`bus`、`runtime_bus`、`models`、`supervisor`、
`memory`、`vlm`、`cloud`、`wake_word`、`gateway`、`persona` 等。密钥走环境变量，
如 `YUKI_CLOUD_API_KEY`；不要提交本地 `data/`、`logs/`。

`models.backend` 默认是 `remote`，由 `model_worker` 统一托管本地模型。旧的单模块入口仍保留，
需要在单独调试时使用 `models.backend: local`。

## 测试

```bash
pytest                        # 单元测试
pytest -m e2e                 # 端到端集成测试（spawn 真实进程）
```

## 文档

- 整体设计：`docs/superpowers/specs/2026-08-10-yuki-agent-design.md`
- ASR 全链路：`docs/superpowers/specs/2026-08-23-asr-fullchain-design.md`
- 桌面 Gateway：`docs/superpowers/specs/2026-08-23-desktop-gateway-design.md`
- 双进程架构：`docs/superpowers/specs/2026-08-27-single-main-model-worker-design.md`
