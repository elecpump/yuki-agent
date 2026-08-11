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
