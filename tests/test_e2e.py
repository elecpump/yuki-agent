import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from yuki.bus import BusError, BusNode

E2E_PORT = 6500


def _env(port: int):
    env = dict(os.environ)
    env["YUKI_BUS_BASE_PORT"] = str(port)
    env["PYTHONPATH"] = "src"
    return env


@pytest.mark.e2e
def test_supervisor_two_processes_reach_healthy_state():
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
    bus = BusNode(base_port=port)
    try:
        deadline = time.time() + 12.0
        while time.time() < deadline:
            try:
                yuki = bus.request("health/yuki", {}, timeout_ms=500)
                worker = bus.request("health/model_worker", {}, timeout_ms=500)
            except BusError:
                time.sleep(0.1)
                continue
            assert not any("scheduling restart" in line for line in buffer)
            assert yuki["healthy"] is True
            assert worker["healthy"] is True
            return
        pytest.fail(f"processes did not become healthy, output so far: {''.join(buffer)!r}")
    finally:
        bus.close()
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
