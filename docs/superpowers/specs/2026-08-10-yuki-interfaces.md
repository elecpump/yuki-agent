# Yuki Agent 进程间接口定义

> 日期：2026-08-10
> 状态：Phase 2a 定型；Phase 2c 已将 JSON 编码替换为 protobuf（信封字段不变）
> 传输：全部 localhost（tcp://127.0.0.1）

## 1. 总线拓扑与角色

- hub（bus_server 进程）：唯一绑定端口 base_port..base_port+2（XSUB/XPUB/ROUTER）
- node（cognition/interaction/perception）：只连接，不绑定；持 PUB/SUB/DEALER 三个套接字
- 角色由 YUKI_BUS_ROLE（Config.bus_role）决定；Supervisor 守护 bus_server 与三个层

## 2. 端口分配

| 端口 | 套接字 | 用途 |
|---|---|---|
| base_port | XSUB | 节点 PUB 连入 |
| base_port+1 | XPUB | 节点 SUB 连入 |
| base_port+2 | ROUTER | 节点 DEALER 连入（REQ/REP 经枢纽） |

## 3. 统一信封（protobuf）

所有总线消息为序列化后的 `Envelope`（`proto/yuki.proto`，生成 `yuki/proto/yuki_pb2.py`）。
`Envelope` 用 oneof 判别消息类型：`request` / `response` / `event`。
动态载荷用 `google.protobuf.Struct` 承载，handler 层仍以 dict 出入。

- 生成：`python scripts/generate_proto.py`（grpcio-tools，无系统 protoc）
- CI 兼容性检查：`tests/test_proto_uptodate.py` 重生成比对，不一致即失败
- 信封字段：`version`(uint32)、`trace_id`(string)

### PUB/SUB
- 帧：`[topic, Envelope(event)]`，`event={topic, payload:Struct}`
- 订阅：单 SUB 套接字多 SUBSCRIBE；同前缀多 handler 并存；重叠前缀均触发

### ROUTER/DEALER（REQ/REP）
- 注册：`["REGISTER", service]`（文本控制帧，不裹信封）
- 请求：`[service, Envelope(request)]`，`request={service, request_id, payload:Struct}`
- 响应：`[client_identity, Envelope(response)]`，`response={request_id, result:Struct|error:string}`（互斥）
- 服务未注册：hub 直回 `Envelope(response).error="service not found"`
- 响应方 handler 异常：`Envelope(response).error="handler error"`，error_count 递增
- 同一服务单提供者，后注册者胜出
- 默认超时 2000ms → BusTimeoutError；error → BusError

## 4. 事件主题与载荷

| 主题 | 方向 | 载荷 |
|---|---|---|
| event/awake | 交互层→总线 | {"source":"hotkey"\|"wakeword","ts":float,"confidence":0..1}（confidence/wakeword 为 Phase 3 预留；当前仅发 hotkey+ts） |
| event/reply | 认知层→总线 | {"text":str,"ts":float} |
| event/focus_changed | 采集层→总线 | {"app":str,"url":str,"title":str}（Phase 2b） |
| event/heartbeat | 各层→总线 | {"process":str,"ts":float}（可选） |

## 5. 帧主题与格式

### audio/mic（Phase 3 启用）
- PCM 16kHz、16bit、单声道、帧长 20ms（320 字节/帧）
- v1 用 JSON base64 传输；唤醒词检测本身全本地

### frame/request（REQ/REP，Phase 2b 启用）
- 服务名 `frame`；超时 2000ms；失败按降级链

## 6. 健康检查

- 服务名：`health/{process}`（cognition/interaction/perception 各自注册）
- 响应：{"process","pid","uptime_s","error_count"}
- Supervisor 定时 REQ 探活，超时视为卡死并重启
- bus_server 不注册 health 服务，只靠进程 poll 判定存活（不探活）
- bus_server 未存活时跳过对其他进程探活

## 7. 错误码枚举

| 码 | 常量名 | 含义 |
|---|---|---|
| 1000 | SCREEN_CAPTURE_FAILED | 截屏失败 |
| 2001 | VLM_TIMEOUT | 视觉理解超时 |
| 2002 | VLM_FAILED | 视觉理解失败 |
| 3001 | STT_EMPTY | 语音识别结果为空 |
| 4001 | BUS_TIMEOUT | 总线请求超时 |
| 4002 | SERVICE_NOT_FOUND | 服务未注册 |

降级链：VLM 失败 → 系统信息感知 → L1 本地快答 → 断网本地人格兜底

## 8. 背压与可靠性

- HWM：PUB SNDHWM / SUB RCVHWM / DEALER SND+RCV / ROUTER RCV = 1000
- 订阅/响应 handler 异常：记录日志 + error_count，线程不死
- 重启策略：指数退避（base*2^n，cap），restart_window 秒内窗口计数限流
