# Yuki Agent

Windows 上的纯语音陪伴 agent（开发中）。第一期：浏览/阅读场景感知与陪伴。

## 架构

三进程分层 + 本地消息总线（ZMQ PUB/SUB + REQ/REP）：
- `src/yuki/perception` 采集层（Phase 2 实现）
- `src/yuki/cognition` 认知层
- `src/yuki/interaction` 交互层

## 运行

```bash
pip install -e ".[dev,windows]"
# 终端 1
python -m yuki.cognition
# 终端 2（触发一次呼叫）
python -m yuki.interaction --trigger-after 2
```

## 测试

```bash
pytest                        # 单元测试
pytest -m e2e                 # 端到端集成测试
```

## 文档

- 设计文档：`docs/superpowers/specs/2026-08-10-yuki-agent-design.md`
