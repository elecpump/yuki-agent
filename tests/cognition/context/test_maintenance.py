import json
import sqlite3
import threading

from yuki.cognition.context.consolidation import ConsolidationStore
from yuki.cognition.context.maintenance import SegmentSummarizer, ThreadMaintenanceScheduler
from yuki.cognition.context.sediment import Sedimenter
from yuki.cognition.context.snapshot import ContextProjector
from yuki.cognition.context.store import ThreadTurnStore
from yuki.cognition.context.working import WorkingContext
from yuki.memory.store import MemoryStore


class FakeCloudClient:
    def __init__(self, content: str = "这是一段摘要") -> None:
        self.content = content
        self.calls = []
        self.called = threading.Event()

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        self.called.set()
        return {"choices": [{"message": {"content": self.content}}]}


class FlakyCloudClient(FakeCloudClient):
    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if len(self.calls) == 1:
            raise RuntimeError("temporary outage")
        return {"choices": [{"message": {"content": self.content}}]}


class BlockingCloudClient(FakeCloudClient):
    def __init__(self) -> None:
        super().__init__("阻塞结束。")
        self.release = threading.Event()

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        self.called.set()
        assert self.release.wait(1.0)
        return {"choices": [{"message": {"content": self.content}}]}


class CloseTrackingConsolidationStore:
    def __init__(self) -> None:
        self.closed = threading.Event()

    def close(self) -> None:
        self.closed.set()


def test_maintenance_tick_summarizes_one_closed_segment(tmp_path):
    store = ThreadTurnStore(tmp_path / "memory.db", segment_max_turns=2)
    context = WorkingContext(store)
    user_turn_id = store.add_user("第一问", at=100.0)
    store.add_agent("第一答", at=101.0, reply_to_turn_id=user_turn_id)
    client = FakeCloudClient("用户提出第一问并得到回答。")
    scheduler = ThreadMaintenanceScheduler(
        store,
        SegmentSummarizer(client, model="test-model", timeout_s=2.0),
        summary_failures_max=3,
    )

    assert scheduler.tick() is True

    snapshot = ContextProjector().build(context)
    assert snapshot.summaries == ("用户提出第一问并得到回答。",)
    assert "第一问" in client.calls[0][0][-1]["content"]
    context.close()


def test_scheduler_processes_closed_segment_in_background_and_closes(tmp_path):
    store = ThreadTurnStore(tmp_path / "memory.db", segment_max_turns=1)
    context = WorkingContext(store)
    store.add_user("后台摘要", at=100.0)
    client = FakeCloudClient("后台摘要完成。")
    scheduler = ThreadMaintenanceScheduler(
        store,
        SegmentSummarizer(client, model="test-model", timeout_s=2.0),
        summary_failures_max=3,
        tick_s=0.01,
    )

    scheduler.start()
    assert client.called.wait(1.0)
    scheduler.close(timeout_s=1.0)
    scheduler.close(timeout_s=1.0)

    assert ContextProjector().build(context).summaries == ("后台摘要完成。",)
    context.close()


def test_scheduler_close_performs_bounded_flush_of_closed_segment(tmp_path):
    store = ThreadTurnStore(tmp_path / "memory.db", segment_max_turns=1)
    context = WorkingContext(store)
    store.add_user("关闭前摘要", at=100.0)
    scheduler = ThreadMaintenanceScheduler(
        store,
        SegmentSummarizer(
            FakeCloudClient("关闭前已完成。"),
            model="test-model",
            timeout_s=2.0,
        ),
        summary_failures_max=3,
    )

    scheduler.close(timeout_s=1.0)

    assert ContextProjector().build(context).summaries == ("关闭前已完成。",)
    context.close()


def test_scheduler_defers_consolidation_close_until_timed_out_worker_exits(tmp_path):
    store = ThreadTurnStore(tmp_path / "memory.db", segment_max_turns=1)
    store.add_user("阻塞摘要", at=100.0)
    client = BlockingCloudClient()
    consolidation = CloseTrackingConsolidationStore()
    scheduler = ThreadMaintenanceScheduler(
        store,
        SegmentSummarizer(client, model="test-model", timeout_s=2.0),
        summary_failures_max=3,
        tick_s=10.0,
        consolidation_store=consolidation,
    )
    scheduler.start()
    assert client.called.wait(1.0)

    scheduler.close(timeout_s=0.01)

    assert consolidation.closed.is_set() is False
    client.release.set()
    assert consolidation.closed.wait(1.0)
    store.close()


def test_maintenance_tick_closes_idle_episode_without_waiting_for_next_user(tmp_path):
    store = ThreadTurnStore(
        tmp_path / "memory.db",
        segment_max_turns=20,
        episode_idle_s=10,
    )
    store.add_user("暂时聊完", at=100.0)
    scheduler = ThreadMaintenanceScheduler(
        store,
        SegmentSummarizer(
            FakeCloudClient(),
            model="test-model",
            timeout_s=2.0,
        ),
        summary_failures_max=3,
        clock=lambda: 112.0,
    )

    scheduler.tick()
    proactive_turn_id = store.add_agent("稍后提醒", at=112.0)

    turns = {turn["id"]: turn for turn in store.items()}
    assert turns[proactive_turn_id]["episode_id"] is None
    store.close()


def test_summary_failure_backs_off_then_recovers_without_permanent_fuse(tmp_path):
    now = [100.0]
    store = ThreadTurnStore(tmp_path / "memory.db", segment_max_turns=1)
    context = WorkingContext(store)
    store.add_user("稍后重试", at=100.0)
    client = FlakyCloudClient("重试成功。")
    scheduler = ThreadMaintenanceScheduler(
        store,
        SegmentSummarizer(client, model="test-model", timeout_s=2.0),
        summary_failures_max=3,
        tick_s=1.0,
        clock=lambda: now[0],
    )

    assert scheduler.tick() is True
    assert scheduler.tick() is False
    assert len(client.calls) == 1

    now[0] = 101.0
    assert scheduler.tick() is True
    assert ContextProjector().build(context).summaries == ("重试成功。",)
    context.close()


def test_scheduler_health_reports_backlog_and_retry_state(tmp_path):
    now = [100.0]
    store = ThreadTurnStore(tmp_path / "memory.db", segment_max_turns=1)
    store.add_user("等待摘要", at=100.0)
    scheduler = ThreadMaintenanceScheduler(
        store,
        SegmentSummarizer(FlakyCloudClient(), model="test-model", timeout_s=2.0),
        summary_failures_max=3,
        tick_s=5.0,
        clock=lambda: now[0],
    )

    before = scheduler.health()
    assert before["worker_alive"] is False
    assert before["segments"]["pending"] == 1

    assert scheduler.tick() is True
    failed = scheduler.health()
    assert failed["summary_failure_streak"] == 1
    assert failed["summary_retry_in_s"] == 5.0
    assert failed["segments"]["pending"] == 1
    store.close()


def test_backoff_does_not_block_idle_episode_close(tmp_path):
    now = [100.0]
    store = ThreadTurnStore(
        tmp_path / "memory.db", segment_max_turns=1, episode_idle_s=10
    )
    store.add_user("第一段", at=100.0)
    client = FlakyCloudClient("重试成功。")
    scheduler = ThreadMaintenanceScheduler(
        store,
        SegmentSummarizer(client, model="test-model", timeout_s=2.0),
        summary_failures_max=3,
        tick_s=30.0,  # 退避 30s，覆盖 idle 窗口（10s）
        clock=lambda: now[0],
    )

    assert scheduler.tick() is True  # 摘要失败 → 进入退避
    assert len(client.calls) == 1

    now[0] = 115.0  # 超过 episode idle（10s）：退避期内 tick 仍应关闭 idle Episode
    assert scheduler.tick() is True
    assert len(client.calls) == 1  # 未再次调用 LLM
    store.close()


def test_running_summary_is_single_flight_and_keeps_raw_fallback(tmp_path):
    store = ThreadTurnStore(tmp_path / "memory.db", segment_max_turns=1)
    context = WorkingContext(store)
    store.add_user("摘要期间仍需可见", at=100.0)
    client = BlockingCloudClient()
    scheduler = ThreadMaintenanceScheduler(
        store,
        SegmentSummarizer(client, model="test-model", timeout_s=2.0),
        summary_failures_max=3,
    )
    worker = threading.Thread(target=scheduler.tick)
    worker.start()
    assert client.called.wait(1.0)

    assert scheduler.tick() is False
    snapshot = ContextProjector().build(context)
    assert [turn["content"] for turn in snapshot.fallback_turns] == [
        "摘要期间仍需可见"
    ]

    client.release.set()
    worker.join(timeout=1.0)
    assert worker.is_alive() is False
    context.close()


def test_scheduler_consolidates_closed_episode_without_user_intervention(tmp_path):
    path = tmp_path / "memory.db"
    memory = MemoryStore(path)
    store = ThreadTurnStore(path, episode_idle_s=10)
    user_turn_id = store.add_user("今天去了西湖", at=100.0)
    store.add_agent("听起来不错", at=101.0, reply_to_turn_id=user_turn_id)
    response = json.dumps(
        {
            "candidates": [
                {
                    "draft_key": "west-lake",
                    "proposed_op": "add",
                    "memory_type": "scenario",
                    "canonical_key": "西湖游览",
                    "content": "用户今天去了西湖",
                    "confidence": 0.8,
                    "sensitivity": 0,
                    "evidence": [{"turn_id": user_turn_id, "quote": "今天去了西湖"}],
                    "metadata": {},
                }
            ]
        },
        ensure_ascii=False,
    )
    client = FakeCloudClient(response)
    consolidation = ConsolidationStore(path)
    scheduler = ThreadMaintenanceScheduler(
        store,
        SegmentSummarizer(client, model="test-model", timeout_s=2.0),
        summary_failures_max=3,
        clock=lambda: 112.0,
        consolidation_store=consolidation,
        sedimenter=Sedimenter(client, model="test-model", timeout_s=2.0),
    )

    assert scheduler.tick() is True

    assert [item["content"] for item in memory.list()] == ["用户今天去了西湖"]
    consolidation.close()
    store.close()
    memory.close()


def test_invalid_candidate_response_marks_run_failed_without_retry(tmp_path):
    path = tmp_path / "memory.db"
    memory = MemoryStore(path)
    store = ThreadTurnStore(path, episode_idle_s=10)
    store.add_user("普通对话", at=100.0)
    client = FakeCloudClient("{}")
    consolidation = ConsolidationStore(path)
    scheduler = ThreadMaintenanceScheduler(
        store,
        SegmentSummarizer(client, model="test-model", timeout_s=2.0),
        summary_failures_max=3,
        clock=lambda: 112.0,
        consolidation_store=consolidation,
        sedimenter=Sedimenter(client, model="test-model", timeout_s=2.0),
    )

    assert scheduler.tick() is True
    assert scheduler.tick() is False

    with sqlite3.connect(path) as connection:
        run_state, error = connection.execute(
            "SELECT state, last_error FROM consolidation_runs"
        ).fetchone()
        assert run_state == "failed"
        assert "candidates" in error
    assert len(client.calls) == 1
    consolidation.close()
    store.close()
    memory.close()
