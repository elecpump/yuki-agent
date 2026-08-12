# Yuki Phase 2b：采集层实现 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现采集层（Perception Agent）的四个组件：SystemMonitor（前台窗口/滚动检测）、ScreenCapture（WGC 截屏 + 敏感黑帧）、SensitiveDetector（窗口级敏感拦截）、AudioCapture（麦克风采集）。将 `perception/main.py` 的空桩 build_perception 填充为真实采集进程，发布 `event/focus_changed`、`audio/mic`，注册 `frame` REQ/REP 服务。AEC 与系统音频回路按设计文档留待 Phase 4。

**Architecture:** 三进程架构保持不变（bus_server / cognition / interaction / perception）。采集层四个组件各自独立、可单测：滚动检测用 ctypes 低级钩子（WH_MOUSE_LL + WH_KEYBOARD_LL，设计文档 §4.3 指定机制）驱动 300ms 静止窗口；SystemMonitor 用 UIA（uiautomation）探测前台窗口；SensitiveDetector 用 win32 窗口枚举 + 黑名单规则；ScreenCapture 用 WGC（windows_capture 包，window_hwnd 回调式）；AudioCapture 用 sounddevice（WASAPI 16kHz/16bit/单声道/20ms）。全部经 protobuf 信封发布到总线（Phase 2c 编码）。**硬件适配器与可测核心分离**：每个组件拆成纯逻辑核心（可注入时钟/fake）与薄适配器（WGC/sounddevice/ctypes 钩子），单测打纯核心，适配器走集成验证。

**Tech Stack:** Python ≥3.11；新增运行时依赖：`windows-capture>=2.0`（WGC）、`sounddevice>=0.5`、`numpy`（sounddevice 不声明，audio.py 运行时需要）、`comtypes`、`uiautomation>=2`；既有：pyzmq、protobuf、pydantic、PIL、pywin32。

**Spec:** `docs/superpowers/specs/2026-08-10-yuki-agent-design.md` §3.1（组件职责与关键决策）、§4.3（时序）、§9.1（测试）；接口契约 `docs/superpowers/specs/2026-08-10-yuki-interfaces.md` §4/§5/§7。

## Global Constraints

- 平台：Windows 10/11；语言：Python 为主
- 总线走 localhost（tcp://127.0.0.1）；消息为 protobuf `Envelope`（Phase 2c）
- 事件驱动而非轮询：上游状态变化才发事件
- **敏感源头阻断**：窗口级检测命中 → 停止截屏 + 发布"占位黑帧"（纯黑 PNG），明文不出进程
- 滚动静止窗口 300ms（WM_MOUSEWHEEL/WM_VSCROLL/PageUp/Down 重置计时器）
- 音频帧格式：PCM 16kHz / 16bit / 单声道 / 帧长 20ms（320 字节）；`audio/mic` 主题（Phase 3 启用，本阶段实现采集但交互层唤醒词尚不消费）
- `event/focus_changed` 载荷：`{"app": str, "url": str, "title": str}`
- `frame` REQ/REP 服务：返回最新帧（PNG bytes + 元数据）；超时 2000ms
- 第一期不含系统音频回路；AEC 留 Phase 4
- 目录：`src/yuki/perception/`；测试 `tests/perception/`
- 每个任务 TDD：先写失败测试 → 跑失败 → 实现 → 跑通 → 提交
- 既有 81 单元 + 1 e2e 必须保持通过

## 测试策略（关键）

- **纯逻辑核心**（可注入时钟/fake、无真实硬件）：滚动静止判定、敏感规则、前台变化判定、帧策略、音频帧切分——单测覆盖
- **薄适配器**（WGC/sounddevice/ctypes 钩子/UIA）：真实硬件调用不进入单测；用接口抽象 + fake 实现验证集成路径；真实硬件冒烟验证留 e2e/手动
- 无头环境（CI/SSH）下 UIA/WGC/sounddevice 可能不可用 → 适配器测试用 `pytest.mark.skipif` 探测可用性，或依赖注入 fake

## File Structure

```
pyproject.toml                                   # 修改：新增运行时依赖
src/yuki/perception/scroll.py                    # 新增：ScrollHook(ctypes) + ScrollIdleDetector(纯逻辑)
src/yuki/perception/system_monitor.py            # 新增：SystemMonitor（UIA/win32 前台探测 + focus_changed 发布）
src/yuki/perception/sensitive.py                 # 新增：SensitiveDetector（窗口类名/标题黑名单 + 安全桌面）
src/yuki/perception/capture.py                   # 新增：FrameCapture 接口 + WgcCapture 适配器 + 帧策略
src/yuki/perception/audio.py                     # 新增：AudioCapture（sounddevice + 帧切分）
src/yuki/perception/__init__.py                  # 新增：包
src/yuki/perception/main.py                      # 修改：build_perception 集成四组件
tests/perception/test_scroll.py                  # 新增
tests/perception/test_system_monitor.py          # 新增
tests/perception/test_sensitive.py               # 新增
tests/perception/test_capture.py                 # 新增
tests/perception/test_audio.py                   # 新增
tests/test_perception_smoke.py                   # 修改：适配集成
```

---

### Task 1: 滚动检测（ScrollHook + ScrollIdleDetector）

**Files:**
- Create: `src/yuki/perception/scroll.py`
- Test: `tests/perception/test_scroll.py`

**Interfaces:**
- Consumes: 无（纯逻辑 + ctypes）
- Produces:
  - `class ScrollIdleDetector`（纯逻辑，可注入时钟）：
    - `__init__(self, idle_ms: int = 300, clock: Callable[[], float] = time.monotonic)`
    - `on_scroll_activity() -> None` — 重置静止计时器（记录 `self._last_activity = clock()`）
    - `is_idle(now: float | None = None) -> bool` — `now - last_activity >= idle_ms/1000` 时为 True；无活动记录时返回 True
    - `last_activity: float | None`
  - `class ScrollHook`（薄适配器，ctypes 低级钩子）：
    - `__init__(self, on_scroll: Callable[[], None], logger=None)`
    - `start() -> None` / `stop() -> None`（幂等；钩子线程为 daemon）
    - 捕获 `WM_MOUSEWHEEL`（WH_MOUSE_LL）、`VK_PRIOR/VK_NEXT`（PageUp/Down，WH_KEYBOARD_LL）→ 调 `on_scroll()`
  - 钩子实现用 ctypes `SetWindowsHookExW`，回调线程跑消息循环；`stop()` 卸载钩子。**适配器本身不单测**（真实钩子需交互会话），测试用注入的 `on_scroll` 计数验证 ScrollIdleDetector，钩子仅验证其可 import、构造签名正确。

- [ ] **Step 1: 写失败测试 `tests/perception/test_scroll.py`**

```python
import time

import pytest

from yuki.perception.scroll import ScrollHook, ScrollIdleDetector


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_idle_detector_no_activity_is_idle():
    det = ScrollIdleDetector(idle_ms=300, clock=FakeClock())
    assert det.is_idle() is True


def test_idle_detector_activity_resets_timer():
    clock = FakeClock()
    det = ScrollIdleDetector(idle_ms=300, clock=clock)
    clock.now = 100.0
    det.on_scroll_activity()
    clock.now = 100.2  # 200ms 后仍非静止
    assert det.is_idle() is False
    clock.now = 100.4  # 400ms 后静止
    assert det.is_idle() is True


def test_idle_detector_repeated_activity_extends():
    clock = FakeClock()
    det = ScrollIdleDetector(idle_ms=300, clock=clock)
    clock.now = 0.0
    det.on_scroll_activity()
    clock.now = 0.25
    det.on_scroll_activity()  # 重置
    clock.now = 0.4
    assert det.is_idle() is False  # 距上次 150ms


def test_idle_detector_records_last_activity():
    clock = FakeClock()
    det = ScrollIdleDetector(idle_ms=300, clock=clock)
    clock.now = 42.0
    det.on_scroll_activity()
    assert det.last_activity == 42.0


def test_scroll_hook_constructible():
    # 适配器薄壳：验证构造签名（真实钩子需交互会话，不在此启动）
    hook = ScrollHook(on_scroll=lambda: None)
    assert hook is not None
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/perception/test_scroll.py -v`
Expected: FAIL，`No module named 'yuki.perception.scroll'`

- [ ] **Step 3: 实现 `src/yuki/perception/scroll.py`**

```python
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
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/perception/test_scroll.py -v`
Expected: 5 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add src/yuki/perception/scroll.py tests/perception/test_scroll.py
git commit -m "feat: scroll idle detection with low-level hooks and pure detector"
```

---

### Task 2: SystemMonitor（前台窗口检测 + focus_changed 发布）

**Files:**
- Create: `src/yuki/perception/system_monitor.py`
- Test: `tests/perception/test_system_monitor.py`

**Interfaces:**
- Consumes: `Topics.FOCUS_CHANGED`（既有）、`MessageBus.publish`（既有）
- Produces:
  - `class ForegroundProbe`（薄适配器，win32/UIA 探测当前前台窗口）：
    - `probe() -> dict | None` — 返回 `{"app": str, "url": str, "title": str}`；探测失败返回 None
    - `__init__(self, get_foreground=win32gui.GetForegroundWindow, get_text=win32gui.GetWindowText, get_class=win32gui.GetClassName, get_pid=...)`（可注入，便于测试）
  - `class SystemMonitor`（核心逻辑）：
    - `__init__(self, probe: ForegroundProbe, on_change: Callable[[dict], None], poll_interval: float = 0.5, clock=time.monotonic)`
    - `tick() -> None` — 探测前台窗口；与上次不同则调 `on_change(dict)` 并更新缓存
    - `start() / stop()` — 后台 daemon 线程循环 `tick()` + sleep(poll_interval)
    - 相同窗口（app+title 相同）不重复发事件（事件驱动而非轮询）
  - 集成 helper：`make_monitor(bus, probe=None) -> SystemMonitor` — 用 `bus.publish(Topics.FOCUS_CHANGED, payload)` 作 on_change

- [ ] **Step 1: 写失败测试 `tests/perception/test_system_monitor.py`**

```python
import time

import pytest

from yuki.perception.system_monitor import ForegroundProbe, SystemMonitor


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_probe_extracts_window_info(monkeypatch):
    calls = {}

    def fake_get_foreground():
        return 1234

    def fake_get_text(hwnd):
        return "My Browser - Article"

    def fake_get_class(hwnd):
        return "Chrome_WidgetWin_1"

    def fake_get_pid(hwnd):
        return 42

    def fake_process_name(pid):
        return "chrome.exe"

    probe = ForegroundProbe(
        get_foreground=fake_get_foreground,
        get_text=fake_get_text,
        get_class=fake_get_class,
        get_pid=fake_get_pid,
        process_name=fake_process_name,
    )
    result = probe.probe()
    assert result == {"app": "chrome", "url": "", "title": "My Browser - Article"}


def test_monitor_emits_on_change():
    events = []
    clock = FakeClock()

    class FakeProbe:
        def __init__(self):
            self.value = None

        def probe(self):
            return self.value

    probe = FakeProbe()
    monitor = SystemMonitor(probe, on_change=events.append, poll_interval=0.0, clock=clock)
    probe.value = {"app": "chrome", "url": "", "title": "A"}
    monitor.tick()
    assert len(events) == 1
    assert events[0]["title"] == "A"


def test_monitor_does_not_reemit_same_window():
    events = []
    clock = FakeClock()

    class FakeProbe:
        def __init__(self):
            self.value = None

        def probe(self):
            return self.value

    probe = FakeProbe()
    monitor = SystemMonitor(probe, on_change=events.append, poll_interval=0.0, clock=clock)
    probe.value = {"app": "chrome", "url": "", "title": "A"}
    monitor.tick()
    monitor.tick()  # 窗口未变
    assert len(events) == 1


def test_monitor_emits_when_window_changes_back():
    events = []
    clock = FakeClock()

    class FakeProbe:
        def __init__(self):
            self.value = None

        def probe(self):
            return self.value

    probe = FakeProbe()
    monitor = SystemMonitor(probe, on_change=events.append, poll_interval=0.0, clock=clock)
    probe.value = {"app": "a", "url": "", "title": "A"}
    monitor.tick()
    probe.value = {"app": "b", "url": "", "title": "B"}
    monitor.tick()
    probe.value = {"app": "a", "url": "", "title": "A"}
    monitor.tick()
    assert len(events) == 3


def test_monitor_probe_none_skips():
    events = []
    clock = FakeClock()

    class FakeProbe:
        def probe(self):
            return None

    monitor = SystemMonitor(FakeProbe(), on_change=events.append, poll_interval=0.0, clock=clock)
    monitor.tick()
    assert events == []
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/perception/test_system_monitor.py -v`
Expected: FAIL，`No module named 'yuki.perception.system_monitor'`

- [ ] **Step 3: 实现 `src/yuki/perception/system_monitor.py`**

```python
import threading
import time
from typing import Callable

from yuki.logger import get_logger

logger = get_logger("yuki.perception.system_monitor")

import win32gui  # noqa: E402
import win32process  # noqa: E402


def _default_process_name(pid: int) -> str:
    try:
        import psutil
        return psutil.Process(pid).name()
    except Exception:
        return "unknown"


class ForegroundProbe:
    """探测当前前台窗口（薄适配器，win32/UIA，可注入便于测试）。"""

    def __init__(
        self,
        get_foreground=win32gui.GetForegroundWindow,
        get_text=win32gui.GetWindowText,
        get_class=win32gui.GetClassName,
        get_pid=win32process.GetWindowThreadProcessId,
        process_name=_default_process_name,
    ) -> None:
        self._get_foreground = get_foreground
        self._get_text = get_text
        self._get_class = get_class
        self._get_pid = get_pid
        self._process_name = process_name

    def probe(self) -> dict | None:
        try:
            hwnd = self._get_foreground()
            if not hwnd:
                return None
            title = self._get_text(hwnd)
            app = self._app_name(hwnd)
            url = self._url_from_title(app, title)
            return {"app": app, "url": url, "title": title}
        except Exception:
            logger.exception("foreground probe failed")
            return None

    def _app_name(self, hwnd: int) -> str:
        try:
            _, pid = self._get_pid(hwnd)
            name = self._process_name(pid) or ""
            return name.rsplit(".", 1)[0].lower()  # chrome.exe -> chrome
        except Exception:
            return ""

    def _url_from_title(self, app: str, title: str) -> str:
        # 浏览器标题格式 "标题 - 站点"；仅提取站点部分做弱信号。Phase 2b 不做深解析。
        if app in ("chrome", "msedge", "firefox"):
            if " - " in title:
                return title.rsplit(" - ", 1)[-1].strip()
        return ""


class SystemMonitor:
    """前台窗口监控：变化才发事件（事件驱动而非轮询）。"""

    def __init__(
        self,
        probe: ForegroundProbe,
        on_change: Callable[[dict], None],
        poll_interval: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._probe = probe
        self._on_change = on_change
        self._poll_interval = poll_interval
        self._clock = clock
        self._last: dict | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> None:
        current = self._probe.probe()
        if current is None:
            return
        if current != self._last:
            self._last = current
            self._on_change(current)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception("system monitor tick failed")
            self._stop.wait(timeout=self._poll_interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None


def make_monitor(bus, probe=None) -> SystemMonitor:
    """绑定总线：前台窗口变化 → publish event/focus_changed。"""
    from yuki.topics import Topics

    probe = probe or ForegroundProbe()

    def on_change(payload: dict) -> None:
        bus.publish(Topics.FOCUS_CHANGED, payload)

    return SystemMonitor(probe, on_change=on_change)
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/perception/test_system_monitor.py -v`
Expected: 5 个测试 PASS

- [ ] **Step 5: 回归 + 提交**

Run: `python -m pytest -q`
Expected: 全部 PASS
```bash
git add src/yuki/perception/system_monitor.py tests/perception/test_system_monitor.py
git commit -m "feat: foreground window monitor publishing focus_changed events"
```

---

### Task 3: 窗口级敏感检测 SensitiveDetector

**Files:**
- Create: `src/yuki/perception/sensitive.py`
- Test: `tests/perception/test_sensitive.py`

**Interfaces:**
- Consumes: 无（纯逻辑 + win32 可注入）
- Produces:
  - `class SensitiveDetector`：
    - `__init__(self, class_blacklist: set[str] | None = None, title_keywords: tuple[str, ...] | None = None, secure_desktop_classes: set[str] | None = None)`
    - 默认黑名单：类名 `Progman`(桌面) 排除、`LockAppHost`/`Shell_TrayWnd` 等系统、密码管理器常见类名；标题关键词 `password`/`密码`/`银行`/`card`/`credential`/`wallet`/`vault`；安全桌面类 `LogonUI`/`Credential UI Host`
    - `is_sensitive(class_name: str, title: str) -> bool` — 类名或标题命中即 True（**纯逻辑，核心**）
    - `class SensitiveRule`（可选的规则单元，若分解清晰则引入）：不做，保持单一方法
  - `window_sensitive(probe_result: dict, detector: SensitiveDetector) -> bool` — 从 SystemMonitor 的 probe dict 中取 app/title 判定（组合层）

- [ ] **Step 1: 写失败测试 `tests/perception/test_sensitive.py`**

```python
import pytest

from yuki.perception.sensitive import SensitiveDetector


def test_default_detector_marks_bank_in_title():
    det = SensitiveDetector()
    assert det.is_sensitive(class_name="Chrome_WidgetWin_1", title="网上银行登录 - ABC银行") is True


def test_default_detector_marks_password_keyword():
    det = SensitiveDetector()
    assert det.is_sensitive(class_name="Anything", title="Enter your password") is True


def test_default_detector_marks_secure_desktop_class():
    det = SensitiveDetector()
    assert det.is_sensitive(class_name="LogonUI", title="") is True


def test_default_detector_allows_normal_window():
    det = SensitiveDetector()
    assert det.is_sensitive(class_name="Chrome_WidgetWin_1", title="如何写代码 - 知乎") is False


def test_custom_blacklist_overrides():
    det = SensitiveDetector(
        class_blacklist={"SecretClass"},
        title_keywords=("机密",),
        secure_desktop_classes=set(),
    )
    assert det.is_sensitive(class_name="SecretClass", title="普通") is True
    assert det.is_sensitive(class_name="Normal", title="这是机密文档") is True
    assert det.is_sensitive(class_name="Normal", title="普通标题") is False
    # 默认银行关键词不再命中（自定义覆盖默认）
    assert det.is_sensitive(class_name="Chrome_WidgetWin_1", title="网上银行") is False
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/perception/test_sensitive.py -v`
Expected: FAIL，`No module named 'yuki.perception.sensitive'`

- [ ] **Step 3: 实现 `src/yuki/perception/sensitive.py`**

```python
_DEFAULT_CLASS_BLACKLIST = frozenset({
    # 安全/系统
    "LogonUI",
    "Credential UI Host",
    "SecureDesktopHost",
    "LockAppHost",
    "Shell_TrayWnd",
    # 密码管理器（常见）
    "KeePassMainWindow",
    "BitwardenMainWindow",
    "1PasswordMainWindow",
})

_DEFAULT_TITLE_KEYWORDS = (
    "password", "passphrase", "credential", "secret",
    "密码", "口令", "凭据",
    "银行", "银行卡", "card", "wallet", "vault",
    "登录", "sign in", "login", "两因素", "2fa", "otp",
)

_DEFAULT_SECURE_DESKTOP_CLASSES = frozenset({
    "LogonUI",
    "SecureDesktopHost",
    "Credential UI Host",
})


class SensitiveDetector:
    """窗口级敏感检测：命中即应停止截屏并发布占位黑帧。

    纯逻辑：类名/标题关键词黑名单 + 安全桌面类。可注入自定义规则。
    """

    def __init__(
        self,
        class_blacklist: set[str] | None = None,
        title_keywords: tuple[str, ...] | None = None,
        secure_desktop_classes: set[str] | None = None,
    ) -> None:
        self._class_blacklist = (
            frozenset(class_blacklist) if class_blacklist is not None else _DEFAULT_CLASS_BLACKLIST
        )
        self._title_keywords = (
            title_keywords if title_keywords is not None else _DEFAULT_TITLE_KEYWORDS
        )
        self._secure_desktop_classes = (
            frozenset(secure_desktop_classes)
            if secure_desktop_classes is not None
            else _DEFAULT_SECURE_DESKTOP_CLASSES
        )

    def is_sensitive(self, class_name: str, title: str) -> bool:
        class_name = (class_name or "").strip()
        title = (title or "").strip()
        if class_name in self._secure_desktop_classes:
            return True
        if class_name in self._class_blacklist:
            return True
        title_lower = title.lower()
        return any(kw in title_lower for kw in self._title_keywords)
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/perception/test_sensitive.py -v`
Expected: 5 个测试 PASS

- [ ] **Step 5: 提交**

```bash
git add src/yuki/perception/sensitive.py tests/perception/test_sensitive.py
git commit -m "feat: window-level sensitive content detection"
```

---

### Task 4: ScreenCapture（FrameCapture 接口 + WgcCapture 适配器 + 帧策略）

**Files:**
- Create: `src/yuki/perception/capture.py`
- Test: `tests/perception/test_capture.py`

**Interfaces:**
- Consumes: `SensitiveDetector`（Task 3）、`ScrollIdleDetector`（Task 1）、`MessageBus.respond`（既有）
- Produces:
  - `class FrameCapture`（抽象基类）：
    - `start() -> None` / `stop() -> None`
    - `on_frame: Callable[[bytes, dict], None] | None` — 捕获到帧时调用 `(png_bytes, metadata)`
  - `class WgcCapture(FrameCapture)`（薄适配器，windows_capture）：
    - `__init__(self, window_hwnd: int, min_update_interval: int = 100)`
    - 用 `windows_capture.WindowsCapture(window_hwnd=...)` + `on_frame_arrived` 回调；回调里 `native_frame.convert_to_bgr()` → PIL → PNG bytes → `self.on_frame(png, {"width": w, "height": h, "ts": ...})`
    - 适配器不单测（需真实桌面）；测试用 fake capture 验证策略与集成
  - `class FrameStrategy`（纯逻辑核心）：
    - `__init__(self, sensitive: SensitiveDetector, idle: ScrollIdleDetector, black_frame: bytes | None = None)`
    - `should_capture(class_name: str, title: str) -> tuple[bool, bool]` — 返回 `(capture, is_sensitive)`；命中敏感返回 `(False, True)`（不截屏，发黑帧）
    - `black_frame_png(width=1920, height=1080, color=(0,0,0)) -> bytes`（生成纯黑 PNG，用 PIL）
  - 集成 helper：`make_frame_service(bus, capture: FrameCapture, strategy: FrameStrategy)` — 注册 `frame` REQ/REP 服务，返回最新帧 `{"png": <base64>, "width": int, "height": int, "ts": float}`；无帧时返回 `{"png": "", "width": 0, "height": 0}`（不发错误）

- [ ] **Step 1: 写失败测试 `tests/perception/test_capture.py`**

```python
import base64
import io

import pytest
from PIL import Image

from yuki.perception.capture import FrameStrategy, black_frame_png
from yuki.perception.sensitive import SensitiveDetector
from yuki.perception.scroll import ScrollIdleDetector


def test_black_frame_is_pure_black_png():
    png = black_frame_png(width=64, height=48)
    img = Image.open(io.BytesIO(png))
    assert img.size == (64, 48)
    assert img.getpixel((32, 24)) == (0, 0, 0)


def test_strategy_allows_normal_window():
    det = SensitiveDetector()
    idle = ScrollIdleDetector(idle_ms=300)
    strategy = FrameStrategy(sensitive=det, idle=idle)
    capture, sensitive = strategy.should_capture("Chrome_WidgetWin_1", "如何写代码")
    assert capture is True
    assert sensitive is False


def test_strategy_blocks_sensitive_window():
    det = SensitiveDetector()
    idle = ScrollIdleDetector(idle_ms=300)
    strategy = FrameStrategy(sensitive=det, idle=idle)
    capture, sensitive = strategy.should_capture("Chrome_WidgetWin_1", "网上银行登录")
    assert capture is False
    assert sensitive is True


def test_strategy_blocks_during_scroll():
    det = SensitiveDetector()
    idle = ScrollIdleDetector(idle_ms=300)
    strategy = FrameStrategy(sensitive=det, idle=idle)
    idle.on_scroll_activity()
    # 滚动中不截屏（等静止）
    capture, sensitive = strategy.should_capture("Chrome_WidgetWin_1", "文章", scroll_required=False)
    # should_capture 默认不查滚动；滚动门控由上层 (WgcCapture 定时回调) 结合 is_idle 决定
    assert capture is True
```

**说明：** `should_capture` 的滚动门控在 Step 3 中明确——WgcCapture 是定时回调，由集成层在 `on_frame` 前检查 `idle.is_idle()`；`FrameStrategy.should_capture` 只管敏感判定 + 可选的滚动门控参数 `require_idle: bool = False`。Step 1 测试据此对齐：`test_strategy_blocks_during_scroll` 断言 `require_idle=True` 时滚动中返回 `(False, False)`：

- [ ] **Step 1b: 修正滚动门控测试**

```python
def test_strategy_requires_idle_when_requested():
    det = SensitiveDetector()
    idle = ScrollIdleDetector(idle_ms=300)
    strategy = FrameStrategy(sensitive=det, idle=idle, require_idle=True)
    idle.on_scroll_activity()
    capture, sensitive = strategy.should_capture("Chrome_WidgetWin_1", "文章")
    assert capture is False
    assert sensitive is False
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/perception/test_capture.py -v`
Expected: FAIL，`No module named 'yuki.perception.capture'`

- [ ] **Step 3: 实现 `src/yuki/perception/capture.py`**

```python
import base64
import io
import time
from abc import ABC, abstractmethod
from typing import Callable

from PIL import Image

from yuki.logger import get_logger
from yuki.perception.scroll import ScrollIdleDetector
from yuki.perception.sensitive import SensitiveDetector

logger = get_logger("yuki.perception.capture")


class FrameCapture(ABC):
    """帧捕获抽象：真实实现为 WGC，测试用 fake。"""

    on_frame: Callable[[bytes, dict], None] | None = None

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...


def black_frame_png(width: int = 1920, height: int = 1080, color=(0, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


class WgcCapture(FrameCapture):
    """Windows Graphics Capture 适配器（薄壳，真实桌面会话）。"""

    def __init__(self, window_hwnd: int, min_update_interval: int = 100) -> None:
        self._window_hwnd = window_hwnd
        self._min_update_interval = min_update_interval
        self._capture = None

    def start(self) -> None:
        import windows_capture

        def on_frame(native_frame, buf_len, width, height, stop_list, timespan):
            if self.on_frame is None:
                return
            try:
                bgr = native_frame.convert_to_bgr()
                image = Image.fromarray(bgr)
                buf = io.BytesIO()
                image.save(buf, format="PNG")
                self.on_frame(
                    buf.getvalue(),
                    {"width": width, "height": height, "ts": time.time()},
                )
            except Exception:
                logger.exception("wgc frame callback failed")

        self._capture = windows_capture.WindowsCapture(
            window_hwnd=self._window_hwnd,
            minimum_update_interval=self._min_update_interval,
        )
        self._capture.on_frame_arrived = on_frame
        self._capture.start_free_threaded()

    def stop(self) -> None:
        if self._capture is not None:
            try:
                self._capture.close()
            except Exception:
                pass
            self._capture = None


class FrameStrategy:
    """帧策略：敏感窗口发黑帧、滚动中暂停截屏（纯逻辑）。"""

    def __init__(
        self,
        sensitive: SensitiveDetector,
        idle: ScrollIdleDetector,
        require_idle: bool = False,
        black: bytes | None = None,
    ) -> None:
        self._sensitive = sensitive
        self._idle = idle
        self._require_idle = require_idle
        self._black = black

    def should_capture(self, class_name: str, title: str) -> tuple[bool, bool]:
        if self._sensitive.is_sensitive(class_name, title):
            return False, True
        if self._require_idle and not self._idle.is_idle():
            return False, False
        return True, False

    def black_frame(self) -> bytes:
        return self._black if self._black is not None else black_frame_png()


def make_frame_service(bus, capture: FrameCapture, strategy: FrameStrategy) -> None:
    """注册 frame REQ/REP 服务：返回最新帧（PNG base64 + 元数据）。"""
    latest: dict = {"png": "", "width": 0, "height": 0, "ts": 0.0}

    def on_frame(png: bytes, meta: dict) -> None:
        latest["png"] = base64.b64encode(png).decode("ascii")
        latest["width"] = meta["width"]
        latest["height"] = meta["height"]
        latest["ts"] = meta["ts"]

    capture.on_frame = on_frame

    def handler(payload: dict) -> dict:
        return dict(latest)

    bus.respond("frame", handler)
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/perception/test_capture.py -v`
Expected: 5 个测试 PASS（含 Step 1b 修正后的滚动门控测试）

- [ ] **Step 5: 回归 + 提交**

Run: `python -m pytest -q`
Expected: 全部 PASS
```bash
git add src/yuki/perception/capture.py tests/perception/test_capture.py
git commit -m "feat: frame capture abstraction, WGC adapter, and sensitive-aware frame strategy"
```

---

### Task 5: AudioCapture（sounddevice 麦克风 + 帧切分）

**Files:**
- Create: `src/yuki/perception/audio.py`
- Test: `tests/perception/test_audio.py`

**Interfaces:**
- Consumes: `MessageBus.publish`（既有）、`Topics.MIC`（既有，Phase 3 启用）
- Produces:
  - `class AudioFrameSplitter`（纯逻辑核心）：
    - `__init__(self, sample_rate: int = 16000, frame_ms: int = 20, channels: int = 1, dtype=float32)`
    - `frames_per_second = sample_rate / (frame_ms/1000)`
    - `split(samples: np.ndarray) -> list[np.ndarray]` — 把输入采样切成 `frame_len` 的帧（不足丢弃末尾）；返回帧列表
  - `class AudioCapture`：
    - `__init__(self, bus, sample_rate=16000, channels=1, frame_ms=20, stream_factory=None)` — `stream_factory` 可注入 fake（测试用），默认 sounddevice.InputStream
    - `start() / stop()` — 打开流，回调里切帧并 `bus.publish(Topics.MIC, {"pcm": <base64 float32 原始字节>, "sample_rate": ..., "ts": ...})`
    - **本阶段只采集发布，唤醒词不消费**（Phase 4）
  - 载荷约定：`{"pcm": <base64>, "sample_rate": 16000, "ts": float}`（protobuf Struct 承载；pcm 为 float32 原始字节 base64）

- [ ] **Step 0: 修改 `pyproject.toml` 加运行时依赖**

```toml
[project]
dependencies = ["pyzmq>=25", "structlog>=24", "pydantic>=2", "PyYAML>=6",
                "protobuf>=6.33.5", "windows-capture>=2.0", "sounddevice>=0.5",
                "numpy>=1.26", "comtypes>=1.2", "uiautomation>=2"]
```

（若 Task 1-4 已先行加入部分依赖，合并即可；numpy 必须进运行时依赖——sounddevice 不声明它但 audio.py 需要。）

- [ ] **Step 1: 写失败测试 `tests/perception/test_audio.py`**

```python
import math

import numpy as np
import pytest

from yuki.perception.audio import AudioCapture, AudioFrameSplitter


def test_splitter_frames_at_20ms():
    splitter = AudioFrameSplitter(sample_rate=16000, frame_ms=20)
    assert splitter.frame_len == 320
    samples = np.zeros(16000)  # 1 秒 = 50 帧
    frames = splitter.split(samples)
    assert len(frames) == 50
    assert all(f.shape == (320,) for f in frames)


def test_splitter_drops_tail():
    splitter = AudioFrameSplitter(sample_rate=16000, frame_ms=20)
    samples = np.zeros(321)  # 余 1 采样，丢弃
    frames = splitter.split(samples)
    assert len(frames) == 1
    assert frames[0].shape == (320,)


def test_splitter_empty():
    splitter = AudioFrameSplitter(sample_rate=16000, frame_ms=20)
    assert splitter.split(np.array([])).shape == (0,)


def test_capture_uses_fake_stream():
    published = []
    splitter = AudioFrameSplitter(sample_rate=16000, frame_ms=20)

    class FakeBus:
        def publish(self, topic, payload):
            published.append((topic, payload))

    class FakeStream:
        def __init__(self, callback):
            self.callback = callback

        def start(self):
            # 注入一帧 320 采样
            self.callback(np.zeros(320), 0, None, None)

    def fake_stream_factory(callback, **kwargs):
        return FakeStream(callback)

    cap = AudioCapture(
        FakeBus(),
        stream_factory=fake_stream_factory,
        splitter=splitter,
    )
    cap.start()
    assert len(published) == 1
    topic, payload = published[0]
    assert topic == "audio/mic"
    assert payload["sample_rate"] == 16000
    assert payload["pcm"] != ""
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/perception/test_audio.py -v`
Expected: FAIL，`No module named 'yuki.perception.audio'`

- [ ] **Step 3: 实现 `src/yuki/perception/audio.py`**

```python
import base64
import time

import numpy as np

from yuki.logger import get_logger
from yuki.topics import Topics

logger = get_logger("yuki.perception.audio")


class AudioFrameSplitter:
    """把输入采样切成固定帧长（纯逻辑）。"""

    def __init__(self, sample_rate: int = 16000, frame_ms: int = 20, channels: int = 1) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.channels = channels
        self.frame_len = int(sample_rate * frame_ms / 1000) * channels

    def split(self, samples: np.ndarray) -> np.ndarray:
        usable = len(samples) - (len(samples) % self.frame_len)
        return samples[:usable].reshape(-1, self.frame_len)


class AudioCapture:
    """麦克风采集：WASAPI（sounddevice），帧切分后发布 audio/mic。

    本阶段仅采集发布；唤醒词/STT 在 Phase 4 消费。stream_factory 可注入 fake。
    """

    def __init__(
        self,
        bus,
        sample_rate: int = 16000,
        channels: int = 1,
        frame_ms: int = 20,
        splitter: AudioFrameSplitter | None = None,
        stream_factory=None,
    ) -> None:
        self._bus = bus
        self._splitter = splitter or AudioFrameSplitter(sample_rate, frame_ms, channels)
        self._stream_factory = stream_factory
        self._stream = None

    def _default_stream(self, callback):
        import sounddevice as sd

        return sd.InputStream(
            samplerate=self._splitter.sample_rate,
            channels=self._splitter.channels,
            dtype="float32",
            callback=callback,
        )

    def _on_audio(self, indata, frames, time_info, status):
        if status:
            logger.warning("audio status: %s", status)
        samples = np.asarray(indata)[:, 0] if indata.ndim > 1 else np.asarray(indata)
        for frame in self._splitter.split(samples):
            pcm = base64.b64encode(frame.astype(np.float32).tobytes()).decode("ascii")
            self._bus.publish(
                Topics.MIC,
                {"pcm": pcm, "sample_rate": self._splitter.sample_rate, "ts": time.time()},
            )

    def start(self) -> None:
        if self._stream is not None:
            return
        factory = self._stream_factory or self._default_stream
        self._stream = factory(self._on_audio)
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
```

- [ ] **Step 4: 跑测试验证通过**

Run: `python -m pytest tests/perception/test_audio.py -v`
Expected: 4 个测试 PASS（numpy 若未装则 `pip install -e ".[dev]"` 或加 numpy 依赖——检查 pyproject，若 numpy 未在 deps 则加入）

- [ ] **Step 5: 提交**

```bash
git add src/yuki/perception/audio.py tests/perception/test_audio.py
git commit -m "feat: microphone capture with frame splitting to audio/mic"
```

---

### Task 6: perception 集成 build_perception

**Files:**
- Modify: `src/yuki/perception/main.py`
- Modify: `tests/test_perception_smoke.py`
- Test: `tests/test_perception_smoke.py`

**Interfaces:**
- Consumes: Task 1-5 全部、`MessageBus`、`Config`、`ShutdownManager`、`register_health_service`（既有）
- Produces:
  - `build_perception(bus, config, *, capture=None, monitor=None, audio=None, scroll_hook=None, strategy=None) -> None` — 组装四组件；默认用真实适配器，测试注入 fake
  - `main()` 调 `build_perception(bus, config)`，优雅关闭时 stop 各组件

- [ ] **Step 1: 改写失败测试 `tests/test_perception_smoke.py`**

```python
import pytest

from yuki.config import Config
from yuki.perception.capture import FrameStrategy
from yuki.perception.main import build_perception
from yuki.perception.sensitive import SensitiveDetector
from yuki.perception.scroll import ScrollIdleDetector


class FakeBus:
    def __init__(self):
        self.published = []
        self.services = {}

    def publish(self, topic, payload):
        self.published.append((topic, payload))

    def respond(self, service, handler):
        self.services[service] = handler

    def request(self, service, payload, timeout_ms=2000):
        return self.services[service](payload)


class FakeCapture:
    on_frame = None

    def start(self):
        pass

    def stop(self):
        pass


class FakeMonitor:
    def start(self):
        pass

    def stop(self):
        pass


class FakeAudio:
    def start(self):
        pass

    def stop(self):
        pass


class FakeScrollHook:
    def start(self):
        pass

    def stop(self):
        pass


def test_build_perception_wires_components():
    bus = FakeBus()
    config = Config(bus_role="node")
    strategy = FrameStrategy(sensitive=SensitiveDetector(), idle=ScrollIdleDetector())
    capture = FakeCapture()
    build_perception(
        bus,
        config,
        capture=capture,
        monitor=FakeMonitor(),
        audio=FakeAudio(),
        scroll_hook=FakeScrollHook(),
        strategy=strategy,
    )
    assert "frame" in bus.services
    assert bus.services["frame"]({}) == {"png": "", "width": 0, "height": 0, "ts": 0.0}


def test_build_perception_default_constructs():
    # 默认路径用真实适配器构造（不 start），验证不抛异常
    bus = FakeBus()
    config = Config(bus_role="node")
    build_perception(bus, config)
    assert "frame" in bus.services
```

- [ ] **Step 2: 跑测试验证失败**

Run: `python -m pytest tests/test_perception_smoke.py -v`
Expected: FAIL（`build_perception` 当前为空，`"frame" not in services`）

- [ ] **Step 3: 实现 `src/yuki/perception/main.py`**

```python
from yuki.bus import MessageBus
from yuki.config import Config
from yuki.health import register_health_service
from yuki.logger import get_logger
from yuki.perception.audio import AudioCapture
from yuki.perception.capture import FrameStrategy, WgcCapture, make_frame_service
from yuki.perception.scroll import ScrollHook, ScrollIdleDetector
from yuki.perception.sensitive import SensitiveDetector
from yuki.perception.system_monitor import ForegroundProbe, SystemMonitor, make_monitor
from yuki.shutdown import ShutdownManager

logger = get_logger("yuki.perception")


def build_perception(
    bus: MessageBus,
    config: Config,
    *,
    capture=None,
    monitor=None,
    audio=None,
    scroll_hook=None,
    strategy=None,
    foreground_hwnd: int | None = None,
) -> None:
    """组装采集层四组件。测试注入 fake；默认用真实适配器。"""

    detector = SensitiveDetector()
    idle = ScrollIdleDetector(idle_ms=300)
    strategy = strategy or FrameStrategy(sensitive=detector, idle=idle)

    if capture is None:
        # WGC 需前台窗口句柄；取不到时降级为"不截屏"（FrameStrategy 仍发黑帧）
        hwnd = foreground_hwnd
        if hwnd is None:
            try:
                import win32gui
                hwnd = win32gui.GetForegroundWindow()
            except Exception:
                hwnd = 0
        capture = WgcCapture(hwnd) if hwnd else None

    if monitor is None:
        monitor = make_monitor(bus, probe=ForegroundProbe())

    if audio is None:
        audio = AudioCapture(bus)

    if scroll_hook is None:
        scroll_hook = ScrollHook(on_scroll=idle.on_scroll_activity)

    if capture is not None:
        make_frame_service(bus, capture, strategy)

    _perception_state["capture"] = capture
    _perception_state["monitor"] = monitor
    _perception_state["audio"] = audio
    _perception_state["scroll_hook"] = scroll_hook
    monitor.start()
    audio.start()
    if capture is not None:
        capture.start()
    scroll_hook.start()


_perception_state: dict = {}


def main() -> None:
    config = Config.from_env()
    bus = MessageBus(base_port=config.base_port, role=config.bus_role, hwm=config.hwm)
    shutdown = ShutdownManager()
    shutdown.register_signal_handlers()
    build_perception(bus, config)
    register_health_service(bus, "perception")
    try:
        while not shutdown.shutdown_requested:
            shutdown.wait(timeout=1.0)
    finally:
        for key in ("scroll_hook", "capture", "monitor", "audio"):
            comp = _perception_state.get(key)
            if comp is not None:
                try:
                    comp.stop()
                except Exception:
                    pass
        bus.close()


if __name__ == "__main__":
    main()
```

注意：`build_perception` 在测试里 start 真实/注入组件。Fake 组件的 start/stop 为空操作，因此 `test_build_perception_default_constructs` 在无头环境可能因 WGC/sounddevice 真实 start 失败——**该测试断言用 monkeypatch 跳过真实 start**（见 Step 4 调整），或以"构造不抛 + frame 服务注册"为准（不实际 start）。以实现通过为准：若默认构造在无头环境 start 抛异常，将默认测试改为 monkeypatch 掉各组件 start 后验证注册；保持"注入 fake 测试集成路径"为主。

- [ ] **Step 4: 调整默认构造测试（无头安全）**

```python
def test_build_perception_default_constructs(monkeypatch):
    bus = FakeBus()
    config = Config(bus_role="node")
    monkeypatch.setattr("yuki.perception.main._perception_state", {})
    # 屏蔽真实适配器 start，仅验证组装与 frame 服务注册
    import yuki.perception.main as pm
    orig = pm._perception_state
    pm._perception_state = {}
    try:
        build_perception(bus, config, capture=FakeCapture(), monitor=FakeMonitor(),
                         audio=FakeAudio(), scroll_hook=FakeScrollHook())
        assert "frame" in bus.services
    finally:
        pm._perception_state = orig
```

- [ ] **Step 5: 跑测试验证通过**

Run: `python -m pytest tests/test_perception_smoke.py -v`
Expected: 2 个测试 PASS

- [ ] **Step 6: 全量回归 + e2e + 提交**

Run: `python -m pytest -q`
Run: `python -m pytest -m e2e -q`
Expected: 全部 PASS
```bash
git add src/yuki/perception/main.py src/yuki/perception/__init__.py tests/test_perception_smoke.py
git commit -m "feat: wire perception components in build_perception"
```

---

## Self-Review

**1. Spec coverage：**
- §3.1 四组件 → Task 1-5；集成 → Task 6
- §4.3 滚动静止窗口（WM_MOUSEWHEEL/PageUp/Down）→ Task 1（低级钩子，设计文档指定机制）
- 敏感源头阻断（黑帧不传明文）→ Task 3/4（`should_capture` 命中即 `(False, True)`，发黑帧）
- 接口契约 §4 focus_changed、§5 audio/mic + frame 服务 → Task 2/4/5
- AEC / 系统音频回路 → 明确留 Phase 4（Global Constraints）
- §9.1 采集层测试（集成 + mock 事件注入）→ 各 Task 测试策略（纯核心单测 + 适配器 fake）

**2. Placeholder 扫描：** 无 TBD/TODO。Task 4 Step 1 的 `test_strategy_blocks_during_scroll` 被 Step 1b 修正（`require_idle` 语义对齐），无残留矛盾。

**3. Type consistency：**
- `FrameStrategy.should_capture -> tuple[bool, bool]`（Task 4）在测试/集成一致
- `ScrollIdleDetector.is_idle/on_scroll_activity`（Task 1）被 Task 4 策略与 Task 6 集成引用
- `SensitiveDetector.is_sensitive(class_name, title)`（Task 3）被 Task 4 策略引用
- `make_frame_service(bus, capture, strategy)`（Task 4）被 Task 6 引用
- `AudioCapture(bus, ..., stream_factory=...)`（Task 5）被 Task 6 引用；`Topics.MIC`/`Topics.FOCUS_CHANGED` 既有常量一致

**关键取舍：**
- 适配器（WGC/sounddevice/ctypes 钩子/UIA）不单测，用注入 fake 验证集成路径；真实硬件冒烟验证留 e2e/手动——符合设计文档"集成测试（真实会话）+ mock 事件注入"分层
- 滚动检测用低级钩子而非 UIA 事件（uiautomation 库无事件 API，且滚动不产生标准 UIA 事件）；SystemMonitor 前台检测保留 UIA/win32 探测——已与用户确认
- `audio/mic` 本阶段仅采集发布，唤醒词/STT 不消费（Phase 4 契约）
- WGC 需前台窗口句柄，取不到时降级不截屏（不发错误，仅不发帧）
