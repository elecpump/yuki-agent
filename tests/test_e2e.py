import os
import signal
import subprocess
import sys
import threading
import time

import pytest

E2E_PORT = 6500


def _env(port: int):
    env = dict(os.environ)
    env["YUKI_BUS_BASE_PORT"] = str(port)
    env["PYTHONPATH"] = "src"
    return env


@pytest.mark.e2e
def test_hotkey_trigger_flow_reaches_reply():
    port = E2E_PORT + 1
    env = _env(port)
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(
        [sys.executable, "-m", "yuki.supervisor", "--trigger-after", "1"],
        env=env,
        cwd=".",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=creationflags,
    )
    buffer = []
    stop = threading.Event()

    def reader():
        for line in proc.stdout:
            buffer.append(line)
            if stop.is_set():
                break

    threading.Thread(target=reader, daemon=True).start()
    try:
        deadline = time.time() + 12.0
        while time.time() < deadline:
            if any("[yuki] 我在，你说。" in line for line in buffer):
                return
            time.sleep(0.1)
        pytest.fail(f"did not receive reply, output so far: {''.join(buffer)!r}")
    finally:
        stop.set()
        if os.name == "nt":
            try:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            except OSError:
                pass
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    pass
        else:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
