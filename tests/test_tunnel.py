"""Tunnels: opened once, recognised when already there, closed on request."""

from __future__ import annotations

from comfy_qa.config import Host
from comfy_qa.tunnel import close_tunnel, command, open_tunnel, pid_file, status

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


def test_opening_records_the_process_so_it_can_be_found_again(tmp_path):
    state = open_tunnel(WIN, tmp_path, launcher=lambda cmd, log: 4242)
    assert state.pid == 4242
    assert pid_file("comfy-win", tmp_path).read_text() == "4242"


def test_a_live_tunnel_is_reused_not_stacked(tmp_path):
    """A second tunnel on the same port means you cannot tell which one answered."""
    import os

    open_tunnel(WIN, tmp_path, launcher=lambda cmd, log: os.getpid())
    launched = []
    again = open_tunnel(WIN, tmp_path, launcher=lambda cmd, log: launched.append(1) or 1)
    assert launched == [], "must not launch a second tunnel"
    assert again.pid == os.getpid()


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


def test_closing_signals_the_process_and_clears_the_file(tmp_path):
    import os

    open_tunnel(WIN, tmp_path, launcher=lambda cmd, log: os.getpid())
    signalled = []
    assert close_tunnel("comfy-win", tmp_path, killer=lambda pid, sig: signalled.append(pid))
    assert signalled == [os.getpid()]
    assert not pid_file("comfy-win", tmp_path).exists()


def test_closing_nothing_is_not_an_error(tmp_path):
    assert close_tunnel("comfy-win", tmp_path, killer=lambda pid, sig: None) is False
