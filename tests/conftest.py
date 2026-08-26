"""Guards that keep the suite off the network and off the user's account.

Both of these exist because they already went wrong. A run of `pytest tests/`
was spawning detached `gcloud compute start-iap-tunnel` processes against the
signed-in project — the count climbed while the suite ran, and they outlived it.
And a test that reached the real `gcloud` passed on CI images that ship the SDK
and failed on the ones that do not, in both directions: a test asserting a
property of the runner rather than of the tool.

So no test starts gcloud. `_spawn` is replaced by one that launches a harmless
sleeping process instead: a real pid, a real process that can be signalled and
really dies, and nothing that talks to Google. Tests of the launcher itself ask
for the `real_spawn` fixture and get the original back.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from comfy_qa import tunnel as tunnel_module

_REAL_SPAWN = tunnel_module._spawn


@pytest.fixture
def real_spawn():
    """For the tests that are about `_spawn` itself."""
    return _REAL_SPAWN


@pytest.fixture(autouse=True)
def never_start_a_real_tunnel(monkeypatch, request):
    if "real_spawn" in request.fixturenames:
        return

    def harmless(command, log, **kwargs):
        # Carries no gcloud, reaches nothing, and stays alive long enough to be
        # found, checked and killed exactly like a tunnel would be.
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return process.pid

    monkeypatch.setattr(tunnel_module, "_spawn", harmless)


@pytest.fixture(autouse=True)
def never_write_to_the_real_config(monkeypatch, tmp_path):
    """The tunnel directory defaults beside the user's own host list."""
    monkeypatch.setattr(tunnel_module, "TUNNEL_DIR", tmp_path / "tunnels")
