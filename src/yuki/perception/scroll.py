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
WM_QUIT = 0x0012
VK_PRIOR = 0x21  # PageUp
VK_NEXT = 0x22   # PageDown
HC_ACTION = 0

HOOKPROC = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

_user32 = ctypes.windll.user32
_user32.SetWindowsHookExW.restype = wintypes.HHOOK
_user32.SetWindowsHookExW.argtypes = [
    ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD
]
_user32.UnhookWindowsHookEx.restype = wintypes.BOOL
_user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
_user32.GetMessageW.restype = ctypes.c_int
_user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT
]
_user32.PostThreadMessageW.restype = wintypes.BOOL
_user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
]
_user32.CallNextHookEx.restype = ctypes.c_long
_user32.CallNextHookEx.argtypes = [
    wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
]

_kernel32 = ctypes.windll.kernel32


class _ScrollHookState:
    def __init__(self) -> None:
        self.on_scroll: Callable[[], None] | None = None
        self.mouse_hook = None
        self.keyboard_hook = None
        self.thread = None
        self.thread_id = None


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
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        _state.on_scroll = self._on_scroll
        _state.thread = threading.Thread(target=self._run_hooks, daemon=True)
        _state.thread.start()

    def _run_hooks(self) -> None:
        _state.thread_id = _kernel32.GetCurrentThreadId()
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
        if not self._running:
            return
        self._running = False
        thread = _state.thread
        thread_id = _state.thread_id
        if _state.mouse_hook:
            _user32.UnhookWindowsHookEx(_state.mouse_hook)
            _state.mouse_hook = None
        if _state.keyboard_hook:
            _user32.UnhookWindowsHookEx(_state.keyboard_hook)
            _state.keyboard_hook = None
        if thread_id is not None:
            _user32.PostThreadMessageW(thread_id, WM_QUIT, 0, 0)
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        _state.on_scroll = None
        _state.thread = None
        _state.thread_id = None


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
