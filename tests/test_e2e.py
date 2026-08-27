import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from yuki.bus import BusError, BusNode
from yuki.supervisor import Supervisor
from yuki.supervisor.main import build_children_cmds

E2E_PORT = 6500


def _env(port: int):
    env = dict(os.environ)
    env["YUKI_BUS_BASE_PORT"] = str(port)
    env["YUKI_BUS_REGISTER_INTERVAL_S"] = "1"
    env["YUKI_VLM_ENABLED"] = "false"
    env["YUKI_STT_ENABLED"] = "false"
    env["YUKI_TTS_ENABLED"] = "false"
    env["YUKI_LOCAL_BRAIN_ENABLED"] = "false"
    env["YUKI_MEMORY_VECTOR_ENABLED"] = "false"
    env["YUKI_GATEWAY_ENABLED"] = "false"
    env["PYTHONPATH"] = "src"
    return env


def _child(supervisor, name):
    return next(child for child in supervisor._children if child.name == name)


def _wait_until(supervisor, bus, predicate, *, timeout=15.0):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            supervisor.tick(bus=bus, health_timeout_ms=250)
            if predicate():
                return
        except BusError as exc:
            last_error = exc
        time.sleep(0.05)
    pytest.fail(f"condition was not reached; last bus error: {last_error!r}")


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


@pytest.mark.e2e
def test_supervisor_recovers_each_process_without_restarting_the_other():
    port = E2E_PORT + 2
    env = _env(port)

    def popen_factory(cmd, **kwargs):
        return subprocess.Popen(
            cmd,
            cwd=".",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            **kwargs,
        )

    supervisor = Supervisor(
        build_children_cmds(),
        popen_factory=popen_factory,
        env=env,
        restart_base_delay=0.05,
        restart_max_delay=0.05,
        startup_grace_s=5.0,
        bus_host="yuki",
        bus_recovery_grace_s=5.0,
        async_restarts=True,
    )
    bus = BusNode(base_port=port, register_interval=1.0)
    try:
        _wait_until(
            supervisor,
            bus,
            lambda: (
                bus.request("health/yuki", {}, timeout_ms=250)["healthy"]
                and bus.request("health/model_worker", {}, timeout_ms=250)["healthy"]
                and bus.request("models/health", {}, timeout_ms=250)["healthy"]
            ),
        )

        yuki_pid = _child(supervisor, "yuki").proc.pid
        old_worker = _child(supervisor, "model_worker").proc
        old_worker_pid = old_worker.pid
        old_worker.kill()
        old_worker.wait(timeout=5.0)

        with pytest.raises(BusError):
            bus.request("models/health", {}, timeout_ms=250)
        _wait_until(
            supervisor,
            bus,
            lambda: (
                _child(supervisor, "model_worker").proc.pid != old_worker_pid
                and bus.request("health/model_worker", {}, timeout_ms=250)["healthy"]
                and bus.request("models/health", {}, timeout_ms=250)["healthy"]
            ),
        )
        assert _child(supervisor, "yuki").proc.pid == yuki_pid

        worker_pid = _child(supervisor, "model_worker").proc.pid
        old_yuki = _child(supervisor, "yuki").proc
        old_yuki.kill()
        old_yuki.wait(timeout=5.0)
        assert _child(supervisor, "model_worker").proc.poll() is None

        _wait_until(
            supervisor,
            bus,
            lambda: (
                _child(supervisor, "yuki").proc.pid != yuki_pid
                and bus.request("health/yuki", {}, timeout_ms=250)["healthy"]
                and bus.request("health/model_worker", {}, timeout_ms=250)["healthy"]
                and bus.request("models/health", {}, timeout_ms=250)["healthy"]
            ),
        )
        assert _child(supervisor, "model_worker").proc.pid == worker_pid
    finally:
        bus.close()
        supervisor.send_break_to_children()
        supervisor.terminate_children()
