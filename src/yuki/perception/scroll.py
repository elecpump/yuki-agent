import ctypes
import threading
import time
from ctypes import wintypes
from typing import Callable

from yuki.logger import get_logger

logger = get_logger("yuki.perception.scroll")

# --- 低级钩子常量 ---
WH_MOUSE_LL = 14
WH_KEYBOARD_LL = 13
WM_MOUSEWHEEL = 0x020A
VK_PRIOR = 0x21  # PageUp
VK_NEXT = 0x22   # PageDown
HC_ACTION = 0

# --- 结构 ---
class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]


MSLLHOOKSTRUCT = None  # 鼠标钩子只需 vkCode 位于其内，用原始指针解析

HOOKPROC = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

_user32 = ctypes.windll.user32


class _ScrollHookState:
    def __init__(self) -> None:
        self.on_scroll: Callable[[], None] | None = None
        self.mouse_hook = None
        self.keyboard_hook = None
        self.thread = None


_state = _ScrollHookState()


def _callback(nCode: int, wParam: int, lParam: int) -> int:
    if nCode == HC_ACTION and _state.on_scroll is not None:
        if wParam == WM_MOUSEWHEEL:
            _state.on_scroll()
        elif wParam == VK_PRIOR or wParam == VK_NEXT:
            # 键盘钩子收到的 wParam 是 vkCode
            _state.on_scroll()
    return _user32.CallNextHookEx(None, nCode, wParam, lParam)


_callback_ref = HOOKPROC(_callback)


class ScrollHook:
    """ctypes 低级钩子：捕获 WM_MOUSEWHEEL 与 PageUp/Down，驱动滚动静止检测。

    薄适配器：真实钩子需交互式桌面会话；单测不启动，仅验证构造。
    """

    def __init__(self, on_scroll: Callable[[], None], logger=None) -> None:
        self._on_scroll = on_scroll

    def start(self) -> None:
        _state.on_scroll = self._on_scroll
        _state.thread = threading.Thread(target=self._run_hooks, daemon=True)
        _state.thread.start()

    def _run_hooks(self) -> None:
        mouse_hook = _user32.SetWindowsHookExW(
            WH_MOUSE_LL, _callback_ref, None, 0
        )
        keyboard_hook = _user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, _callback_ref, None, 0
        )
        _state.mouse_hook = mouse_hook
        _state.keyboard_hook = keyboard_hook
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def stop(self) -> None:
        if _state.mouse_hook:
            _user32.UnhookWindowsHookEx(_state.mouse_hook)
            _state.mouse_hook = None
        if _state.keyboard_hook:
            _user32.UnhookWindowsHookEx(_state.keyboard_hook)
            _state.keyboard_hook = None


class ScrollIdleDetector:
    """滚动静止检测：300ms 内无滚动活动即为静止（纯逻辑，可注入时钟）。"""

    def __init__(
        self,
        idle_ms: int = 300,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._idle_ms = idle_ms
        self._clock = clock
        self.last_activity: float | None = None

    def on_scroll_activity(self) -> None:
        self.last_activity = self._clock()

    def is_idle(self, now: float | None = None) -> bool:
        now = now if now is not None else self._clock()
        if self.last_activity is None:
            return True
        return now - self.last_activity >= self._idle_ms / 1000.0
