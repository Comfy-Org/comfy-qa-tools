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
        if key.startswith("compute firewall-rules list"):
            # Already open, so nothing is created: a test about launching is not
            # a test about firewalls, and the rule is asked for on every launch.
            return [{"name": "comfy-qat-iap-comfyui", "network": ".../networks/default"}]
        if key.startswith("compute firewall-rules create"):
            return ""
        if "NetFirewallRule" in key or "ufw" in key:
            return "ALREADY"
        raise AssertionError(f"unexpected: {key}")

    gc = Gcloud(runner=runner)
    gc.calls = calls  # type: ignore[attr-defined]
    return gc


def said():
    lines = []
    return lines, lines.append


# No test here may start gcloud: `bring_up` opens a tunnel for real, and a plain
# `pytest` used to leave a handful of `gcloud compute start-iap-tunnel` processes
# behind, each retrying for a minute against a project that does not exist. The
# stand-in used to live here and launched a real process wearing a tunnel's argv,
# which made these tests depend on what `ps` says and on the platform's pid range
# — they passed on macOS and failed on Ubuntu. The one seam now lives in
# conftest.py, which answers the identity lookup from its own record instead.


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


def test_a_zone_with_no_capacity_is_named_as_such(tmp_path):
    """Google returns a long STOCKOUT message that reads like an account problem.

    It is not: the zone simply has none of that card free, and retrying there
    will not help. Saying so saves an hour of debugging the wrong thing.
    """
    from comfy_qa.lifecycle import STOCKOUT, is_capacity_failure

    assert is_capacity_failure("... does not have enough resources available ...")
    assert is_capacity_failure("'NULL:0/NULL:0 (state:STOCKOUT, sub-state:STOCKOUT ...)'")
    assert not is_capacity_failure("permission denied")

    _, say = said()
    gc = gcloud(["TERMINATED"], fail=GcloudError(
        "The zone does not have enough resources available to fulfill the request. "
        "'NULL:0/NULL:0/NULL:0 (state:STOCKOUT, sub-state:STOCKOUT, resource type:compute)'."
    ))
    with pytest.raises(LifecycleError) as caught:
        bring_up(gc, WIN, say, tunnel_dir=tmp_path, sleep=lambda _: None)

    assert caught.value.kind == STOCKOUT
    assert "no L4 capacity in us-central1-a" in str(caught.value)
    assert "not a fault on your side" in str(caught.value)
    assert "another zone" in caught.value.fix


def test_a_capacity_failure_stops_go_rather_than_becoming_an_ssh_error(tmp_path):
    """This is what actually happened: a stockout surfaced as "failed to connect
    to port 22", which sent us looking at firewalls and SSH keys for an hour."""
    from comfy_qa.lifecycle import COMFYUI_ABSENT

    _, say = said()
    gc = gcloud(["TERMINATED"], fail=GcloudError("state:STOCKOUT"))
    with pytest.raises(LifecycleError) as caught:
        bring_up(gc, WIN, say, tunnel_dir=tmp_path, sleep=lambda _: None)
    assert caught.value.kind != COMFYUI_ABSENT


# Exactly what gcloud printed when the zone ran out of L4s. The ERROR: line is
# literally `---`, which is how the stockout went unrecognised the first time.
STOCKOUT_OUTPUT = """Starting instance(s) comfy-win...
..........................................failed.
ERROR: (gcloud.compute.instances.start) ---
code: ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS
errorDetails:
- localizedMessage:
    locale: en-US
    message: A g2-standard-8 VM instance with 1 nvidia-l4 accelerator(s) is currently
      unavailable in the us-central1-a zone. Consider trying your request in the us-central1-b
      zone(s), which currently has capacity to accommodate your request.
"""


def test_the_real_stockout_output_is_recognised(tmp_path):
    """It was not, because classification ran on the summary — which is `---`."""
    from comfy_qa.lifecycle import STOCKOUT, is_capacity_failure, suggested_zones

    assert is_capacity_failure(STOCKOUT_OUTPUT)
    assert suggested_zones(STOCKOUT_OUTPUT) == ["us-central1-b"]

    _, say = said()
    gc = gcloud(["TERMINATED"], fail=GcloudError("---", raw=STOCKOUT_OUTPUT))
    with pytest.raises(LifecycleError) as caught:
        bring_up(gc, WIN, say, tunnel_dir=tmp_path, sleep=lambda _: None)

    assert caught.value.kind == STOCKOUT
    assert "no L4 capacity in us-central1-a" in str(caught.value)
    assert "us-central1-b has capacity right now" in caught.value.fix


def test_a_stockout_with_no_suggestion_still_advises_something_useful(tmp_path):
    _, say = said()
    gc = gcloud(["TERMINATED"], fail=GcloudError("---", raw="state:STOCKOUT"))
    with pytest.raises(LifecycleError) as caught:
        bring_up(gc, WIN, say, tunnel_dir=tmp_path, sleep=lambda _: None)
    assert "another zone" in caught.value.fix


def test_a_stockout_points_at_the_command_that_fixes_it(tmp_path):
    """Handing someone four raw gcloud commands is the failure this tool exists
    to prevent."""
    _, say = said()
    gc = gcloud(["TERMINATED"], fail=GcloudError("---", raw=STOCKOUT_OUTPUT))
    with pytest.raises(LifecycleError) as caught:
        bring_up(gc, WIN, say, tunnel_dir=tmp_path, sleep=lambda _: None)
    assert "comfy-qat move comfy-win --to us-central1-b" in caught.value.fix


# --- what the end-to-end harness turned up --------------------------------
#
# Every test below is a defect that only showed itself once the whole path was
# run against a real socket and a real tunnel process, rather than a stub.


def test_a_half_open_tunnel_is_not_answering_rather_than_a_crash():
    """An IAP tunnel to a box with nothing on 8188 accepts the connection and
    then drops it. That arrives as ConnectionResetError, not as a probe failure,
    and it used to come out of `host up` as a traceback."""
    import socket
    import threading

    from comfy_qa.lifecycle import probe

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(5)
    port = listener.getsockname()[1]

    def drop() -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            connection.close()

    threading.Thread(target=drop, daemon=True).start()
    try:
        assert probe(Host(name="x", kind="local", port=port)) is None
    finally:
        listener.close()


def test_the_local_start_command_names_the_port_that_host_actually_uses():
    """It always said 8188, so a second local install was told to start on the
    port the first one is already holding."""
    _, say = said()
    with pytest.raises(LifecycleError) as caught:
        bring_up(gcloud([]), Host(name="other", kind="local", port=8199), say,
                 probe_fn=lambda host: None)
    assert "--port 8199" in caught.value.fix


def test_a_gcloud_failure_while_waiting_for_the_box_is_a_message(tmp_path):
    """describe is polled every five seconds, and any one of those calls can
    fail. Uncaught, the boot wait ended as a GcloudError traceback."""
    def runner(args, mode):
        key = " ".join(args)
        if key.startswith("compute instances describe"):
            if runner.first:
                runner.first = False
                return {"status": "TERMINATED"}
            raise GcloudError("Permission denied on compute.instances.get")
        return ""
    runner.first = True

    _, say = said()
    with pytest.raises(LifecycleError) as caught:
        bring_up(Gcloud(runner=runner), WIN, say, tunnel_dir=tmp_path,
                 sleep=lambda _: None, boot_timeout=0)
    assert "could not tell whether comfy-win reached RUNNING" in str(caught.value)
    assert "Permission denied" in str(caught.value)


def test_a_tunnel_that_cannot_be_opened_is_a_message(tmp_path, monkeypatch):
    """gcloud missing made Popen raise FileNotFoundError in the middle of `up`."""
    from comfy_qa import tunnel as tunnel_module

    def refuse(cmd, log):
        raise tunnel_module.TunnelError("gcloud is not installed or not on PATH.",
                                        fix="install it")

    monkeypatch.setattr(tunnel_module, "_spawn", refuse)
    _, say = said()
    with pytest.raises(LifecycleError) as caught:
        bring_up(gcloud(["RUNNING"]), WIN, say, tunnel_dir=tmp_path,
                 sleep=lambda _: None, probe_fn=lambda host: STAMP)
    assert "could not open the tunnel to comfy-win" in str(caught.value)


def test_a_tunnel_that_dies_is_named_as_the_tunnel(tmp_path, monkeypatch):
    """Otherwise it reads as "ComfyUI is not answering", which sends someone onto
    the box to fix something that was never broken."""
    from comfy_qa import tunnel as tunnel_module
    from comfy_qa.lifecycle import TUNNEL_DOWN

    # A pid that is recorded and already gone, which is what gcloud leaves behind
    # when the tunnel cannot be established: it exits in under a second.
    monkeypatch.setattr(tunnel_module, "_spawn", lambda cmd, log: 999999)
    _, say = said()
    with pytest.raises(LifecycleError) as caught:
        bring_up(gcloud(["RUNNING"]), WIN, say, tunnel_dir=tmp_path,
                 sleep=lambda _: None, probe_fn=lambda host: None,
                 comfy_timeout=60)
    assert caught.value.kind == TUNNEL_DOWN
    assert "the tunnel to comfy-win closed" in str(caught.value)
    assert "ComfyUI is not answering" not in str(caught.value)
    assert str(tunnel_module.log_file("comfy-win", tmp_path)) in caught.value.fix


def test_a_launch_that_ends_badly_is_raised_not_returned():
    """`go` printed "ComfyUI exited (3)" and then exited 0, so everything reading
    the exit code — a script, CI — saw a success."""
    from comfy_qa.lifecycle import serve

    _, say = said()
    with pytest.raises(LifecycleError) as caught:
        serve(Gcloud(runner=lambda args, mode: 3), WIN, say,
              probe_fn=lambda host: STAMP, sleep=lambda _: None, timeout=0)
    assert "NO_PYTHON" in str(caught.value)


def test_a_launch_that_exits_cleanly_without_serving_is_still_a_failure():
    """Exit 0 is not the same as having served."""
    from comfy_qa.lifecycle import serve

    _, say = said()
    with pytest.raises(LifecycleError) as caught:
        serve(Gcloud(runner=lambda args, mode: 0), WIN, say,
              probe_fn=lambda host: None, sleep=lambda _: None, timeout=0)
    assert "without ever answering" in str(caught.value)


def test_ctrl_c_on_the_box_is_not_reported_as_a_fault():
    """130 is a person stopping ComfyUI, which is how you stop it."""
    from comfy_qa.lifecycle import serve

    _, say = said()
    assert serve(Gcloud(runner=lambda args, mode: 130), WIN, say,
                 probe_fn=lambda host: None, sleep=lambda _: None, timeout=0) == 130


def test_the_browser_is_not_opened_after_a_launch_that_failed():
    """The watcher outlived the launch, so a failed `go` still opened a tab onto
    a URL that never answered."""
    from comfy_qa.lifecycle import serve

    opened = []
    _, say = said()
    with pytest.raises(LifecycleError):
        serve(Gcloud(runner=lambda args, mode: 1), WIN, say,
              open_browser=opened.append, probe_fn=lambda host: None,
              sleep=lambda _: None, timeout=0)
    assert opened == []


def test_an_expired_credential_is_not_waited_out(tmp_path):
    """gcloud only offers to reauthenticate when stdin and stderr are terminals,
    and everything here captures output — so it does not ask, it fails, and it
    will fail again in five minutes. Retrying it burns 300s of GPU time on
    something that cannot succeed."""
    from comfy_qa.lifecycle import wait_for_ssh

    attempts = {"n": 0}

    def refuse(args, mode):
        attempts["n"] += 1
        raise GcloudError(
            "There was a problem refreshing your current auth tokens: "
            "Reauthentication failed.", fix="gcloud auth login")

    _, say = said()
    with pytest.raises(LifecycleError) as caught:
        wait_for_ssh(Gcloud(runner=refuse), WIN, say, sleep=lambda _: None,
                     tunnel_dir=tmp_path)
    assert attempts["n"] == 1, "it must not retry a credential that cannot recover"
    assert "not signed in" in str(caught.value)
    assert "running and billing" in str(caught.value)
    assert "gcloud auth login" in caught.value.fix


def test_the_classified_kind_is_believed_when_gcloud_provides_one():
    """`gcloud.classify` is the shared home for this; the text match is only a
    fallback for errors that predate it."""
    from comfy_qa.lifecycle import is_auth_failure

    plain = GcloudError("something went wrong")
    assert not is_auth_failure(plain)

    # `is_auth` is derived from the classified kind, not set by hand — the two
    # branches were written independently and met here.
    classified = GcloudError("something went wrong", kind="reauth")
    assert classified.is_auth
    assert is_auth_failure(classified)


def test_a_failure_after_the_tunnel_is_open_closes_it_again(tmp_path):
    """A forgotten tunnel is a detached process holding a local port open onto a
    machine you are still paying for."""
    from comfy_qa import tunnel as tunnel_module
    from comfy_qa.lifecycle import ensure_installed, wait_for_ssh

    def refuse(args, mode):
        raise GcloudError("failed to connect to backend")

    for act in (
        lambda: wait_for_ssh(Gcloud(runner=refuse), WIN, say, timeout=0,
                             sleep=lambda _: None, tunnel_dir=tmp_path),
        lambda: ensure_installed(Gcloud(runner=_installer("MISSING", exit_code=1)),
                                 WIN, say, tunnel_dir=tmp_path),
    ):
        lines, say = said()
        tunnel_module.open_tunnel(WIN, tmp_path)
        assert tunnel_module.pid_file("comfy-win", tmp_path).exists()

        with pytest.raises(LifecycleError):
            act()

        assert not tunnel_module.pid_file("comfy-win", tmp_path).exists()
        assert any("tunnel closed" in line for line in lines)


def _installer(answer: str, exit_code: int = 0):
    def runner(args, mode):
        return answer if mode == "output" else exit_code
    return runner


def test_an_install_that_reports_success_but_installed_nothing_is_caught():
    """PowerShell carries on after a failed step, so the script can print
    "install complete" and exit 0 having cloned nothing."""
    from comfy_qa.lifecycle import ensure_installed

    _, say = said()
    with pytest.raises(LifecycleError) as caught:
        ensure_installed(Gcloud(runner=_installer("MISSING")), WIN, say)
    assert "did not finish" in str(caught.value)
    assert "reported success" in str(caught.value)


def test_a_successful_install_is_confirmed_on_the_box_not_assumed():
    from comfy_qa.lifecycle import ensure_installed

    answers = iter(["MISSING", "INSTALLED"])

    def runner(args, mode):
        if mode != "output":
            return 0
        # The install asks the box which CUDA its driver supports, so that the
        # torch it fetches matches the card rather than a pinned number.
        if "nvidia-smi" in " ".join(args):
            return "CUDA Version: 13.0"
        return next(answers)

    lines, say = said()
    ensure_installed(Gcloud(runner=runner), WIN, say)
    assert any("installing it" in line for line in lines)


def test_every_failure_after_the_box_is_running_says_how_to_stop_paying(tmp_path):
    """A message that explains the fault but not the bill leaves a GPU box on all
    night. This is the rule, held across every post-start failure."""
    from comfy_qa.lifecycle import ensure_installed, wait_for_ssh

    def refuse(args, mode):
        raise GcloudError("failed to connect to backend")

    _, say = said()
    failures = []

    with pytest.raises(LifecycleError) as absent:
        bring_up(gcloud(["RUNNING"]), WIN, say, tunnel_dir=tmp_path,
                 sleep=lambda _: None, probe_fn=lambda host: None, comfy_timeout=0)
    failures.append(absent.value)

    with pytest.raises(LifecycleError) as ssh:
        wait_for_ssh(Gcloud(runner=refuse), WIN, say, timeout=0, sleep=lambda _: None)
    failures.append(ssh.value)

    with pytest.raises(LifecycleError) as install:
        ensure_installed(Gcloud(runner=_installer("MISSING", exit_code=1)), WIN, say)
    failures.append(install.value)

    with pytest.raises(LifecycleError) as boot:
        bring_up(gcloud(["TERMINATED", "STAGING"]), WIN, say, tunnel_dir=tmp_path,
                 sleep=lambda _: None, boot_timeout=0)
    failures.append(boot.value)

    from comfy_qa import tunnel as tunnel_module

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(tunnel_module, "_spawn", lambda cmd, log: 999999)
    try:
        with pytest.raises(LifecycleError) as gone:
            bring_up(gcloud(["RUNNING"]), WIN, say, tunnel_dir=tmp_path / "gone",
                     sleep=lambda _: None, probe_fn=lambda host: None,
                     comfy_timeout=0)
        failures.append(gone.value)
    finally:
        monkeypatch.undo()

    for failure in failures:
        assert "comfy-qat down comfy-win" in (failure.fix or ""), str(failure)


def test_a_local_host_that_names_a_cloud_instance_is_never_called_stopped(tmp_path):
    """The refusal is at the point of the decision, not only at the parse.

    `config.parse` rejects this host list, but a `Host` is also built in code —
    by `move`, by `switch`, by tests — and `put_away` branching on `kind` alone
    would print "local ComfyUI left running" about a GPU box that is still
    billing. That is the most expensive sentence this tool can say.
    """
    from comfy_qa.config import Host
    from comfy_qa.lifecycle import put_away

    lying = Host(name="comfy-win", kind="local", port=8188,
                 gce_instance="comfy-win", gce_zone="us-central1-a",
                 gce_project="a-project")

    with pytest.raises(LifecycleError) as caught:
        put_away(gcloud(["RUNNING"]), lying, lambda line: None, tunnel_dir=tmp_path)

    assert "names a cloud instance" in str(caught.value)
    assert "billing" in str(caught.value)
    assert "kind = 'gce'" in caught.value.fix


def test_a_box_not_yet_serving_is_absent_comfyui_not_a_broken_tunnel(tmp_path):
    """The classification that decides whether `go` can continue.

    A tunnel that refuses because nothing is listening is the box saying "no
    ComfyUI yet" — which is the one failure `go` is allowed to continue past,
    because starting ComfyUI is exactly what it does next. Reported as a tunnel
    failure it stops the run, and the run was one step from fixing it.
    """
    from comfy_qa.tunnel import BACKEND_NOT_LISTENING, TunnelError

    def refuses(cmd, log, **kwargs):
        raise TunnelError("nothing is listening on port 8188 of the machine yet",
                          kind=BACKEND_NOT_LISTENING)

    _, say = said()
    with pytest.raises(LifecycleError) as caught:
        bring_up(gcloud(["RUNNING"]), WIN, say, tunnel_dir=tmp_path,
                 sleep=lambda _: None, launcher=refuses, probe_fn=lambda host: None)

    from comfy_qa.lifecycle import COMFYUI_ABSENT

    assert caught.value.kind == COMFYUI_ABSENT, "go must be allowed to continue"


def test_a_port_held_by_a_working_comfyui_is_used_not_refused(tmp_path):
    """Watched this refuse a ComfyUI that was answering perfectly.

    The box was silent when `bring_up` probed it and answering by the time
    `serve` ran — a slow starter, or one somebody else had already launched. The
    run printed "ComfyUI answering: comfy-win · ... NVIDIA L4 (22GB)", printed
    the URL, then tore down its own tunnel and called it a port collision.

    A held port is only a problem when what holds it is not the thing you wanted.
    """
    from comfy_qa.lifecycle import serve

    def runner(args, mode):
        joined = " ".join(args)
        if mode == "output":
            return "2804 python" if "NetTCPConnection" in joined else "READY"
        return 0

    lines, say = said()
    code = serve(Gcloud(runner=runner), WIN, say, tunnel_dir=tmp_path,
                 sleep=lambda _: None, probe_fn=lambda host: STAMP, timeout=0)

    told = " ".join(lines)
    assert code == 0
    assert "using it rather than starting a second one" in told
    assert "tunnel closed" not in told, "it tore down a working tunnel"
    assert not any("starting ComfyUI on" in line for line in lines), "no second one"
