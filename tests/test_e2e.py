import subprocess
import sys
import time

import pytest

from yuki.config import Config

E2E_PORT = 6500


def _env(port: int):
    env = {
        "YUKI_BASE_PORT": str(port),
        "PYTHONPATH": "src",
    }
    return env


@pytest.mark.e2e
def test_hotkey_trigger_flow_reaches_reply():
    port = E2E_PORT + 1
    env = _env(port)
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "yuki.cognition"], env=env, cwd="."
        ),
        subprocess.Popen(
            [sys.executable, "-m", "yuki.interaction", "--trigger-after", "1"],
            env=env,
            cwd=".",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ),
    ]
    try:
        deadline = time.time() + 8.0
        output = ""
        while time.time() < deadline:
            line = procs[1].stdout.readline()
            if line:
                output += line
                if "[yuki] 我在，你说。" in output:
                    return
            time.sleep(0.1)
        pytest.fail(f"did not receive reply, output so far: {output!r}")
    finally:
        for p in procs:
            p.terminate()
