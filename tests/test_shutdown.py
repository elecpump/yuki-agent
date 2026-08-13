import threading

from yuki.shutdown import ShutdownManager


def test_initial_not_requested():
    mgr = ShutdownManager()
    assert not mgr.shutdown_requested


def test_request_shutdown_sets_flag():
    mgr = ShutdownManager()
    mgr.request_shutdown()
    assert mgr.shutdown_requested


def test_wait_returns_true_after_shutdown():
    mgr = ShutdownManager()

    def _signal():
        mgr.request_shutdown()

    t = threading.Timer(0.1, _signal)
    t.start()
    assert mgr.wait(timeout=2.0) is True
    t.cancel()


def test_wait_returns_false_on_timeout():
    mgr = ShutdownManager()
    assert mgr.wait(timeout=0.05) is False


def test_run_cleanups_executes_in_reverse_priority_order():
    mgr = ShutdownManager()
    order = []
    mgr.register_cleanup("low", lambda: order.append("low"), priority=10)
    mgr.register_cleanup("mid", lambda: order.append("mid"), priority=5)
    mgr.register_cleanup("high", lambda: order.append("high"), priority=0)
    mgr.run_cleanups()
    assert order == ["low", "mid", "high"]


def test_run_cleanups_swallows_handler_errors():
    mgr = ShutdownManager()
    called = []

    def boom():
        raise RuntimeError("cleanup failed")

    mgr.register_cleanup("boom", boom, priority=0)
    mgr.register_cleanup("ok", lambda: called.append(1), priority=1)
    mgr.run_cleanups()
    assert called == [1]
