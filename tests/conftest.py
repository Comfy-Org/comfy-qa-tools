"""The test suite may not start a real tunnel to a real machine.

This is not hygiene, it was live: `bring_up` opens a tunnel, and
`tests/test_lifecycle.py` calls it with a real `Host` and no launcher, so every
run of `pytest tests/` spawned detached `gcloud compute start-iap-tunnel`
processes against whatever project gcloud is signed in to — and left them
running, because nothing in those tests closes them. Eleven were alive on the
machine this was written on, and five of those were from one run.

So the one seam that starts a process is replaced for the whole suite. A test
that means to exercise launching passes its own `launcher=`; a test that does
not, no longer reaches Google by accident.
"""

from __future__ import annotations

import pytest

from comfy_qa import tunnel


@pytest.fixture(autouse=True)
def never_start_a_real_tunnel(monkeypatch):
    """Every gcloud that would have been started, and not one that was."""
    launches = []

    def refuse(cmd, log):
        launches.append(cmd)
        return 2**22 + 7  # a number above any real pid, so `_alive` says no

    monkeypatch.setattr(tunnel, "_spawn", refuse)
    return launches
