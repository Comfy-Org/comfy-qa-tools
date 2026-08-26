"""Tunnels: opened once, recognised when already there, closed on request.

A recorded pid is checked against the process that holds it, so these use real
processes rather than a number. `os.getpid()` used to stand in for a live tunnel,
which quietly asserted the opposite of what we want: that any live process at
that number counts as one.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from comfy_qa.config import Host
from comfy_qa.tunnel import (
    TunnelError,
    close_tunnel,
    command,
    log_file,
    open_tunnel,
    pid_file,
    status,
)

WIN = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022", gpu="L4",
           gce_instance="comfy-win", gce_zone="us-central1-a", gce_project="proj")


def test_the_tunnel_never_opens_a_port_and_needs_no_ssh_key():
    """ComfyUI has no authentication, so IAP is the only acceptable route."""
    args = command(WIN)
    assert args[:4] == ["gcloud", "compute", "start-iap-tunnel", "comfy-win"]
    assert "8188" in args, "the remote end is always ComfyUI's own port"
    assert "--local-host-port=localhost:8190" in args
    assert "--zone=us-central1-a" in args
    assert not any("firewall" in a or "--ssh" in a for a in args)


@pytest.fixture
def processes():
    started = []
    yield started
    for process in started:
        try:
            process.kill()
            process.wait(timeout=5)
        except OSError:
            pass


def _looks_like_a_tunnel(processes, host=WIN):
    """A real process carrying a real tunnel's command line."""
    def launch(cmd, log):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", *cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(process)
        return process.pid
    return launch


def _something_else(processes):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    processes.append(process)
    return process


def test_opening_records_the_process_so_it_can_be_found_again(tmp_path):
    state = open_tunnel(WIN, tmp_path, launcher=lambda cmd, log: 4242)
    assert state.pid == 4242
    assert pid_file("comfy-win", tmp_path).read_text() == "4242"


def test_a_live_tunnel_is_reused_not_stacked(tmp_path, processes):
    """A second tunnel on the same port means you cannot tell which one answered."""
    first = open_tunnel(WIN, tmp_path, launcher=_looks_like_a_tunnel(processes))
    launched = []
    again = open_tunnel(WIN, tmp_path, launcher=lambda cmd, log: launched.append(1) or 1)
    assert launched == [], "must not launch a second tunnel"
    assert again.pid == first.pid


def test_a_pid_that_belongs_to_something_else_is_not_a_tunnel(tmp_path, processes):
    """Process ids are reused. A pid file that outlived its tunnel — a reboot, a
    crash — ends up naming whatever got the number next, and reporting that as a
    tunnel means reporting a route to a machine that nothing is connected to."""
    other = _something_else(processes)
    pid_file("comfy-win", tmp_path).parent.mkdir(parents=True, exist_ok=True)
    pid_file("comfy-win", tmp_path).write_text(str(other.pid))

    state = status("comfy-win", tmp_path)
    assert state.running is False
    assert state.stale is True

    launched = []
    open_tunnel(WIN, tmp_path, launcher=lambda cmd, log: launched.append(1) or 1)
    assert launched == [1], "a stale record must not stop a real tunnel being opened"


def test_closing_never_signals_a_process_that_is_not_our_tunnel(tmp_path, processes):
    """`down` sending SIGTERM to whatever inherited the number is the worst
    version of this: the tool kills something it has never heard of."""
    other = _something_else(processes)
    pid_file("comfy-win", tmp_path).parent.mkdir(parents=True, exist_ok=True)
    pid_file("comfy-win", tmp_path).write_text(str(other.pid))

    signalled = []
    assert close_tunnel("comfy-win", tmp_path, killer=lambda pid, sig: signalled.append(pid)) is False
    assert signalled == [], "it must not signal a process that is not the tunnel"
    assert not pid_file("comfy-win", tmp_path).exists()
    time.sleep(0.1)
    assert other.poll() is None


def test_when_the_process_cannot_be_read_the_pid_is_still_believed(tmp_path, processes):
    """On a machine where `ps` says nothing, the old behaviour is the safe one:
    an unrecognised tunnel would be stacked on top of a live one."""
    other = _something_else(processes)
    pid_file("comfy-win", tmp_path).parent.mkdir(parents=True, exist_ok=True)
    pid_file("comfy-win", tmp_path).write_text(str(other.pid))

    state = status("comfy-win", tmp_path, inspect=lambda pid: "")
    assert state.running is True


def test_a_dead_pid_is_not_mistaken_for_a_tunnel(tmp_path):
    pid_file("comfy-win", tmp_path).parent.mkdir(parents=True, exist_ok=True)
    pid_file("comfy-win", tmp_path).write_text("999999")
    assert status("comfy-win", tmp_path).running is False


def test_a_corrupt_pid_file_is_survivable(tmp_path):
    path = pid_file("comfy-win", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a number")
    assert status("comfy-win", tmp_path).running is False


def test_no_pid_file_means_no_tunnel(tmp_path):
    assert status("comfy-win", tmp_path).running is False


def test_closing_signals_the_process_and_clears_the_file(tmp_path, processes):
    state = open_tunnel(WIN, tmp_path, launcher=_looks_like_a_tunnel(processes))
    signalled = []
    assert close_tunnel("comfy-win", tmp_path, killer=lambda pid, sig: signalled.append(pid))
    assert signalled == [state.pid]
    assert not pid_file("comfy-win", tmp_path).exists()


def test_the_tunnel_log_sits_beside_its_pid_file(tmp_path):
    """When a tunnel dies on startup, gcloud's reason is only ever in here, so
    every message about a dead tunnel has to be able to name it."""
    assert log_file("comfy-win", tmp_path).parent == pid_file("comfy-win", tmp_path).parent
    assert log_file("comfy-win", tmp_path).suffix == ".log"


def test_a_tunnel_that_dies_on_startup_never_gets_a_pid_file(tmp_path, real_spawn):
    """The failure this is written for. gcloud fails *immediately* far more often
    than it fails later — an expired credential is the common one — and because
    its output is captured it cannot prompt, so it exits rather than asking.
    Recording that pid turned a credential failure into "the box is up but
    ComfyUI is not answering", with the machine left running and billing.
    """

    log = tmp_path / "comfy-win.log"
    dying = [sys.executable, "-c",
             "import sys; sys.stderr.write('ERROR: There was a problem refreshing "
             "your current auth tokens: Reauthentication failed.\\n'); sys.exit(1)"]

    with pytest.raises(TunnelError) as caught:
        real_spawn(dying, log, grace=5)

    assert "closed as soon as it was opened" in str(caught.value)
    assert "Reauthentication failed" in str(caught.value), "gcloud's own words"
    assert "gcloud auth login" in caught.value.fix
    assert not pid_file("comfy-win", tmp_path).exists()


def test_a_tunnel_that_stays_up_is_handed_back(tmp_path):
    """The other half of the same check: a healthy tunnel must not be mistaken
    for a dead one just because it has not finished connecting yet."""
    import os
    import signal

    import comfy_qa.tunnel as tunnel_module

    pid = tunnel_module._spawn(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        tmp_path / "comfy-win.log", grace=0.2)
    try:
        assert pid > 0
    finally:
        os.kill(pid, signal.SIGTERM)


def test_a_missing_gcloud_is_a_message_not_a_traceback(tmp_path, monkeypatch, real_spawn):
    """Popen against a binary that is not there raises FileNotFoundError, which
    reached the user as a traceback from `host open`."""
    import comfy_qa.tunnel as tunnel_module

    monkeypatch.setattr(tunnel_module.shutil, "which", lambda name: None)
    with pytest.raises(TunnelError) as caught:
        real_spawn(command(WIN), tmp_path / "x.log")
    assert "gcloud is not installed" in str(caught.value)
    assert caught.value.fix


def test_closing_nothing_is_not_an_error(tmp_path):
    assert close_tunnel("comfy-win", tmp_path, killer=lambda pid, sig: None) is False
