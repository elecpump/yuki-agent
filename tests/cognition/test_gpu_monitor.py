import types

from yuki.cognition.gpu_monitor import BYTES_PER_GB, GpuMemoryMonitor


def test_gpu_memory_monitor_reports_cuda_unavailable():
    fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: False))
    monitor = GpuMemoryMonitor(torch_module=fake_torch)

    snapshot = monitor.snapshot()

    assert snapshot["available"] is False
    assert snapshot["cuda_available"] is False
    assert snapshot["low_memory"] is False
    assert snapshot["reason"] == "cuda_unavailable"


def test_gpu_memory_monitor_marks_low_memory_and_clears_cache():
    calls = []

    class FakeCuda:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def mem_get_info():
            return int(0.25 * BYTES_PER_GB), int(8 * BYTES_PER_GB)

        @staticmethod
        def memory_allocated():
            return int(2 * BYTES_PER_GB)

        @staticmethod
        def memory_reserved():
            return int(3 * BYTES_PER_GB)

        @staticmethod
        def empty_cache():
            calls.append("empty_cache")

    monitor = GpuMemoryMonitor(
        min_free_gb=1.0,
        min_free_ratio=0.05,
        torch_module=types.SimpleNamespace(cuda=FakeCuda),
    )

    snapshot = monitor.snapshot()

    assert snapshot["available"] is True
    assert snapshot["cuda_available"] is True
    assert snapshot["low_memory"] is True
    assert snapshot["free_gb"] == 0.25
    assert snapshot["total_gb"] == 8.0
    assert snapshot["allocated_bytes"] == int(2 * BYTES_PER_GB)
    assert monitor.empty_cache() is True
    assert calls == ["empty_cache"]
