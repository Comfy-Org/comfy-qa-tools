"""Bringing a machine up, and putting it away.

The rule these tests defend: "up" means ComfyUI answers. A VM that booted and
serves nothing looks like success, bills like success, and is only discovered
when a test behaves oddly.
"""

from __future__ import annotations

import pytest

from comfy_qa.config import Host
from comfy_qa.gcloud import Gcloud, GcloudError
from comfy_qa.lifecycle import (
    LifecycleError,
    bring_up,
    how_to_get_in,
    put_away,
)
from comfy_qa.stamp import Stamp

WIN = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022", gpu="L4",
           gce_instance="comfy-win", gce_zone="us-central1-a", gce_project="proj")
LINUX = Host(name="comfy-linux", kind="gce", port=8191, os="Ubuntu 22.04", gpu="L4",
             gce_instance="comfy-linux", gce_zone="us-central1-a", gce_project="proj")
LOCAL = Host(name="local", kind="local", port=8188)

STAMP = Stamp(host="comfy-win", url="http://127.0.0.1:8190", comfyui_version="0.33.0")


def gcloud(statuses, **extra):
    """A Gcloud whose describe returns each status in turn."""
    seen = iter(statuses)
    calls = []

    def runner(args, mode):
        key = " ".join(args)
        calls.append(key)
        if key.startswith("compute instances describe"):
            return {"status": next(seen)}
        if key.startswith("compute instances start") or key.startswith("compute instances stop"):
            if isinstance(extra.get("fail"), Exception):
                raise extra["fail"]
            return ""
        raise AssertionError(f"unexpected: {key}")

    gc = Gcloud(runner=runner)
    gc.calls = calls  # type: ignore[attr-defined]
    return gc


def said():
    lines = []
    return lines, lines.append


def test_a_stopped_box_is_started_tunnelled_and_confirmed(tmp_path):
    lines, say = said()
    gc = gcloud(["TERMINATED", "RUNNING"])

    ready = bring_up(gc, WIN, say, tunnel_dir=tmp_path, sleep=lambda _: None,
                     probe_fn=lambda host: STAMP)

    assert ready.started is True
    assert ready.tunnelled is True
    assert ready.stamp is STAMP
    assert any("stopped — starting it" in line for line in lines)
    assert any(key.startswith("compute instances start") for key in gc.calls)


def test_terminated_is_reported_as_stopped_not_as_a_fault(tmp_path):
    lines, say = said()
    bring_up(gcloud(["TERMINATED", "RUNNING"]), WIN, say, tunnel_dir=tmp_path,
             sleep=lambda _: None, probe_fn=lambda host: STAMP)
    assert not any("TERMINATED" in line for line in lines)


def test_a_running_box_is_not_started_again(tmp_path):
    lines, say = said()
    gc = gcloud(["RUNNING"])
    ready = bring_up(gc, WIN, say, tunnel_dir=tmp_path, sleep=lambda _: None,
                     probe_fn=lambda host: STAMP)
    assert ready.started is False
    assert not any(key.startswith("compute instances start") for key in gc.calls)


def test_a_booted_box_with_no_comfyui_is_a_failure_not_a_success(tmp_path):
    """The whole point: booting is not being up."""
    _, say = said()
    with pytest.raises(LifecycleError) as caught:
        bring_up(gcloud(["RUNNING"]), WIN, say, tunnel_dir=tmp_path,
                 sleep=lambda _: None, probe_fn=lambda host: None,
                 comfy_timeout=0)

    message = str(caught.value)
    assert "ComfyUI is not answering" in message
    assert "billing" in message, "say that it is costing money right now"
    assert "reset-windows-password" in caught.value.fix


def test_the_way_in_matches_the_operating_system():
    """Printing the Windows recipe for a Linux box sends someone down a dead end."""
    assert "reset-windows-password" in how_to_get_in(WIN)
    assert "Remote Desktop" in how_to_get_in(WIN)
    assert "compute ssh" in how_to_get_in(LINUX)
    assert "reset-windows-password" not in how_to_get_in(LINUX)


def test_a_box_that_never_reaches_running_gives_up_rather_than_hanging(tmp_path):
    _, say = said()
    gc = gcloud(["TERMINATED"] + ["STAGING"] * 50)
    with pytest.raises(LifecycleError, match="did not reach RUNNING"):
        bring_up(gc, WIN, say, tunnel_dir=tmp_path, sleep=lambda _: None,
                 probe_fn=lambda host: STAMP, boot_timeout=0)


def test_a_failure_to_start_names_the_box(tmp_path):
    _, say = said()
    gc = gcloud(["TERMINATED"], fail=GcloudError("quota exceeded"))
    with pytest.raises(LifecycleError, match="could not start comfy-win"):
        bring_up(gc, WIN, say, tunnel_dir=tmp_path, sleep=lambda _: None)


def test_local_up_means_is_it_answering(tmp_path):
    lines, say = said()
    ready = bring_up(gcloud([]), LOCAL, say, tunnel_dir=tmp_path,
                     probe_fn=lambda host: STAMP)
    assert ready.started is False
    assert ready.tunnelled is False
    assert any("already up" in line for line in lines)


def test_local_down_hands_over_the_start_command(tmp_path):
    _, say = said()
    with pytest.raises(LifecycleError) as caught:
        bring_up(gcloud([]), LOCAL, say, tunnel_dir=tmp_path, probe_fn=lambda host: None)
    assert "main.py" in caught.value.fix


def test_put_away_stops_the_box(tmp_path):
    lines, say = said()
    gc = gcloud([])
    put_away(gc, WIN, say, tunnel_dir=tmp_path)
    assert any(key.startswith("compute instances stop") for key in gc.calls)
    assert any("stopped" in line for line in lines)


def test_keep_running_says_plainly_that_it_still_costs(tmp_path):
    lines, say = said()
    gc = gcloud([])
    put_away(gc, WIN, say, tunnel_dir=tmp_path, keep_running=True)
    assert not any(key.startswith("compute instances stop") for key in gc.calls)
    assert any("still billing" in line for line in lines)


def test_put_away_never_stops_a_local_comfyui(tmp_path):
    lines, say = said()
    gc = gcloud([])
    put_away(gc, LOCAL, say, tunnel_dir=tmp_path)
    assert gc.calls == []
    assert any("did not start it" in line for line in lines)


def test_only_a_missing_comfyui_is_worth_continuing_past(tmp_path):
    """`go` swallowed every failure alike, so a box that would not start became
    an SSH attempt against a stopped machine and a confusing error."""
    from comfy_qa.lifecycle import COMFYUI_ABSENT

    _, say = said()
    with pytest.raises(LifecycleError) as absent:
        bring_up(gcloud(["RUNNING"]), WIN, say, tunnel_dir=tmp_path,
                 sleep=lambda _: None, probe_fn=lambda host: None, comfy_timeout=0)
    assert absent.value.kind == COMFYUI_ABSENT

    with pytest.raises(LifecycleError) as failed:
        bring_up(gcloud(["TERMINATED"], fail=GcloudError("no capacity")), WIN, say,
                 tunnel_dir=tmp_path, sleep=lambda _: None)
    assert failed.value.kind != COMFYUI_ABSENT, "a failed start must stop `go`"


def test_a_local_comfyui_that_is_down_is_also_absent_not_broken(tmp_path):
    from comfy_qa.lifecycle import COMFYUI_ABSENT

    _, say = said()
    with pytest.raises(LifecycleError) as caught:
        bring_up(gcloud([]), LOCAL, say, tunnel_dir=tmp_path, probe_fn=lambda host: None)
    assert caught.value.kind == COMFYUI_ABSENT


def test_ssh_is_waited_for_because_running_is_not_ready():
    """RUNNING means powered on. Windows takes minutes to start sshd, and
    connecting early fails with "failed to connect to backend" — which reads
    like a permissions problem and is not."""
    from comfy_qa.lifecycle import wait_for_ssh

    attempts = {"n": 0}

    def runner(args, mode):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise GcloudError("failed to connect to backend")
        return "ok"

    lines, say = said()
    wait_for_ssh(Gcloud(runner=runner), WIN, say, sleep=lambda _: None)
    assert attempts["n"] == 3
    assert any("few minutes" in line for line in lines)


def test_waiting_for_ssh_gives_up_with_the_manual_way_in():
    from comfy_qa.lifecycle import wait_for_ssh

    def runner(args, mode):
        raise GcloudError("failed to connect to backend")

    _, say = said()
    with pytest.raises(LifecycleError) as caught:
        wait_for_ssh(Gcloud(runner=runner), WIN, say, timeout=0, sleep=lambda _: None)
    assert "not accepting commands" in str(caught.value)
    assert "reset-windows-password" in caught.value.fix
