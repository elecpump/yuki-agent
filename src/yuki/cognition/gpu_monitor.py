from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from yuki.logger import get_logger

logger = get_logger("yuki.cognition.gpu_monitor")

BYTES_PER_GB = 1024**3


@dataclass(frozen=True)
class GpuMemorySnapshot:
    available: bool
    cuda_available: bool
    low_memory: bool
    device: str
    free_bytes: int = 0
    total_bytes: int = 0
    used_bytes: int = 0
    allocated_bytes: int = 0
    reserved_bytes: int = 0
    free_gb: float = 0.0
    total_gb: float = 0.0
    free_ratio: float = 0.0
    reason: str = ""

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "cuda_available": self.cuda_available,
            "low_memory": self.low_memory,
            "device": self.device,
            "free_bytes": self.free_bytes,
            "total_bytes": self.total_bytes,
            "used_bytes": self.used_bytes,
            "allocated_bytes": self.allocated_bytes,
            "reserved_bytes": self.reserved_bytes,
            "free_gb": self.free_gb,
            "total_gb": self.total_gb,
            "free_ratio": self.free_ratio,
            "reason": self.reason,
        }


class GpuMemoryMonitor:
    """Small CUDA memory probe with graceful CPU-only behavior."""

    def __init__(
        self,
        *,
        min_free_gb: float = 1.0,
        min_free_ratio: float = 0.05,
        device: str | None = None,
        torch_module: Any = None,
    ) -> None:
        self._min_free_bytes = max(0.0, float(min_free_gb)) * BYTES_PER_GB
        self._min_free_ratio = max(0.0, float(min_free_ratio))
        self._device = device
        self._torch = torch_module

    def snapshot(self) -> dict:
        torch = self._resolve_torch()
        if torch is None:
            return GpuMemorySnapshot(
                available=False,
                cuda_available=False,
                low_memory=False,
                device=self._device or "cuda",
                reason="torch_unavailable",
            ).as_dict()
        cuda = getattr(torch, "cuda", None)
        if cuda is None or not cuda.is_available():
            return GpuMemorySnapshot(
                available=False,
                cuda_available=False,
                low_memory=False,
                device=self._device or "cuda",
                reason="cuda_unavailable",
            ).as_dict()
        try:
            free_bytes, total_bytes = self._mem_get_info(cuda)
            allocated_bytes = self._cuda_counter(cuda, "memory_allocated")
            reserved_bytes = self._cuda_counter(cuda, "memory_reserved")
        except Exception:
            logger.warning("gpu memory snapshot failed", exc_info=True)
            return GpuMemorySnapshot(
                available=False,
                cuda_available=True,
                low_memory=False,
                device=self._device or "cuda",
                reason="snapshot_failed",
            ).as_dict()

        total_bytes = int(total_bytes)
        free_bytes = int(free_bytes)
        free_ratio = free_bytes / total_bytes if total_bytes > 0 else 0.0
        low_memory = free_bytes < self._min_free_bytes or free_ratio < self._min_free_ratio
        return GpuMemorySnapshot(
            available=True,
            cuda_available=True,
            low_memory=low_memory,
            device=self._device or "cuda",
            free_bytes=free_bytes,
            total_bytes=total_bytes,
            used_bytes=max(0, total_bytes - free_bytes),
            allocated_bytes=allocated_bytes,
            reserved_bytes=reserved_bytes,
            free_gb=round(free_bytes / BYTES_PER_GB, 3),
            total_gb=round(total_bytes / BYTES_PER_GB, 3),
            free_ratio=round(free_ratio, 4),
        ).as_dict()

    def empty_cache(self) -> bool:
        torch = self._resolve_torch()
        cuda = getattr(torch, "cuda", None) if torch is not None else None
        if cuda is None or not cuda.is_available():
            return False
        try:
            cuda.empty_cache()
            return True
        except Exception:
            logger.debug("gpu cache cleanup skipped", exc_info=True)
            return False

    def _resolve_torch(self):
        if self._torch is not None:
            return self._torch
        try:
            import torch

            self._torch = torch
            return torch
        except Exception:
            return None

    def _mem_get_info(self, cuda) -> tuple[int, int]:
        if self._device is None:
            return cuda.mem_get_info()
        try:
            return cuda.mem_get_info(self._device)
        except TypeError:
            return cuda.mem_get_info()

    def _cuda_counter(self, cuda, name: str) -> int:
        counter = getattr(cuda, name, None)
        if not callable(counter):
            return 0
        try:
            if self._device is None:
                return int(counter())
            return int(counter(self._device))
        except TypeError:
            return int(counter())
