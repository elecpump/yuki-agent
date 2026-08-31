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

`音频采集(20ms帧)` → `唤醒词检测(openWakeWord)` → `VAD(FSMN-VAD)` →
`STT(SenseVoice-Small)` → 结果发布 `event/perception/user_utterance`。
识别会话带状态机（idle/listening/speaking/processing）：唤醒后无语音超时回落、
一轮回复后进入续听窗口、过期 STT 结果丢弃。唤醒词与桌面 Gateway 均默认关闭。

### 认知决策与路由

用户发言由本地路由模型统一判断：1.7B 门卫一次推理同时输出 `route` / `crisis` /
`emotion` / `polarity` 四个信号，判断不依赖预设关键词：

- `crisis`：自伤/自杀等危机表达 → 强制走云端关怀路径，云端不可用时回退静态兜底话术
- 显式偏好、工具调用、多步推理 → 云端 L2 Agent 循环
- 简单闲聊/情感回应 → 本地模型直接回复
- `emotion` 驱动 TTS 语气；`polarity` 调节主动发言冷却（负向反馈自动降低发言频率）

模型输出缺失或非法时按中性值降级，路由失败统一回退云端。

## 安装

```bash
pip install -e ".[dev,windows]"   # 标准开发环境
pip install -e ".[ml]"            # 追加 VLM/STT 模型推理依赖
pip install -e ".[asr]"           # 追加唤醒词检测（openWakeWord）
pip install -e ".[tts]"           # 追加语音合成（IndexTTS-2.5）
pip install -e ".[desktop]"       # 追加 HTTP/WS Gateway（FastAPI/uvicorn）
```

桌面前端还需要 Node.js 20+；构建 Tauri 安装包需要 Rust stable 和 Windows WebView2
开发环境。纯浏览器开发不依赖 Rust。React 19 通过
`@ant-design/v5-patch-for-react-19` 兼容 Ant Design v5。

## 运行

```bash
cp config.example.yaml config.yaml   # 按需调整，环境变量 YUKI_<SECTION>_<FIELD> 可覆盖

python -m yuki.supervisor            # 推荐：启动 yuki + model_worker 并负责探活/重启
python -m yuki.app                   # 仅启动主进程（需要另行启动 model_worker）
python -m yuki.model_worker          # 仅启动模型进程
python -m yuki.memory list           # 记忆管理 CLI
python -m yuki.soul_cli list         # Soul 可恢复版本
```

启用唤醒词：`config.yaml` 中 `wake_word.enabled: true` 并配置 `model_path`（自训的
"yuki" onnx 模型）；启用桌面 Gateway：`gateway.enabled: true`（REST `:8765`）。

### 桌面前端

浏览器开发模式：

```bash
# 终端 A：config.yaml 中启用 gateway 后启动后端
python -m yuki.supervisor

# 终端 B
cd frontend
npm install
npm run dev
```

Tauri 开发模式默认只连接外部 supervisor，不自动启动后端：

```bash
cd frontend
npm run tauri dev
```

release 构建默认尝试自启动 supervisor，但 v1 不打包 Python。运行前必须提供包含
`config.yaml` 的绝对工作目录；推荐同时指定能 `import yuki` 的解释器：

```powershell
$env:YUKI_WORKDIR = "D:\code\yuki-agent"
$env:YUKI_PYTHON = "D:\code\yuki-agent\.venv\Scripts\python.exe"
cd frontend
npm run tauri build
```

`YUKI_DESKTOP_LAUNCH_BACKEND=true|false` 可覆盖 dev（默认 false）和 release（默认
true）的自启动策略。若 8765 已有健康 Yuki Gateway，桌面端进入 external 模式，退出
时不会关闭它。旧 `config.yaml` 若显式设置 `gateway.cors_origins`，Windows release 需
包含 `http://tauri.localhost`。

Windows 自有后端退出会先尝试向 supervisor 进程组投递 `CTRL_BREAK`。实测表明
`AttachConsole`/`GenerateConsoleCtrlEvent` 返回成功并不保证目标已处理信号，因此桌面端
只等待 2 秒；未退出时使用 `taskkill /T /F` 清理自有进程树，再以根进程 kill 作为最终
兜底。外部 supervisor 不受该逻辑影响。

## 配置

复制 `config.example.yaml` 为 `config.yaml`。主要分区：`bus`、`runtime_bus`、`models`、`supervisor`、
`memory`、`vlm`、`cloud`、`wake_word`、`gateway`、`persona` 等。密钥走环境变量，
如 `YUKI_CLOUD_API_KEY`；不要提交本地 `data/`、`logs/`。

### Soul 手动恢复

恢复前先停止 `yuki`/supervisor，避免运行进程与 CLI 同时写 `soul.json`。建议先备份
`data/soul.json` 和 `data/soul_snapshots/`，然后执行：

```bash
python -m yuki.soul_cli show          # 查看当前 Soul 和 revision
python -m yuki.soul_cli list          # 列出已提交且可恢复的 revision
python -m yuki.soul_cli restore 3     # 将 r3 内容恢复为一个新的 revision
python -m yuki.soul_cli show          # 核对结果后再重启 Yuki
```

自定义配置使用 `--config path/to/config.yaml`；也可用 `--path`、`--snapshots-dir`
覆盖存储位置。`restore` 默认要求输入 `yes` 确认，自动化恢复可显式添加 `--yes`。
恢复不会让 revision 倒退，因此恢复操作本身仍可审计和再次回滚。

快照文件名为 `soul_snapshot_rNNNNNN.json`，内容是
`{"saved_at": <Unix 时间>, "soul": <完整 Soul 对象>}`；整个快照文件不能直接替换
`soul.json`。CLI 无法启动时的紧急文件级恢复步骤如下：

1. 保持 Yuki 停止，并备份主文件与整个快照目录。
2. 从目标快照中只取 `soul` 对象，把其 `revision` 改为当前主文件 revision + 1，
   同时更新 `updated_at`。
3. 先将该对象包装为上述快照格式，写入对应的新 revision 快照文件；再把未包装的
   Soul 对象写入同目录临时文件，并用原子重命名替换 `soul.json`。
4. 运行 `python -m yuki.soul_cli show` 核验后再启动。手工恢复不会自动写审计事件，
   因此只作为 CLI 不可用时的应急方式。

`models.policies` 由 `model_worker` 统一托管本地模型（VLM / STT / 本地脑 / TTS / embedding）：
模型 ID、device、enabled 等来自 `vlm`/`stt`/`tts`/`local_brain`/`memory` 分区，运行策略
（优先级、warmup、可驱逐、固定常驻、空闲卸载、显存估算）来自 `models.policies`。

## 测试

```bash
pytest                        # 单元测试（800+）
pytest -m e2e                 # 端到端集成测试（spawn 真实进程）

cd frontend
npm test                      # Vitest
npm run typecheck
npm run lint
npm run build                 # 纯 Web 生产构建
```

## 文档

- 整体设计：`docs/superpowers/specs/2026-08-10-yuki-agent-design.md`
- ASR 全链路：`docs/superpowers/specs/2026-08-23-asr-fullchain-design.md`
- 桌面 Gateway：`docs/superpowers/specs/2026-08-23-desktop-gateway-design.md`
- 桌面前端：`docs/superpowers/specs/2026-08-30-yuki-desktop-frontend-design.md`
- 双进程架构：`docs/superpowers/specs/2026-08-27-single-main-model-worker-design.md`
