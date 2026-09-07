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
    serve,
    start_detached,
    stop_paying,
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
        if "--command=echo ok" in key:
            # A box this run started is asked whether sshd is listening before a
            # tunnel is opened into it. RUNNING is the VM powered on, not sshd up.
            return "ok"
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
    assert "comfy-qat rdp" in caught.value.fix


def test_the_way_in_matches_the_operating_system():
    """Printing the Windows recipe for a Linux box sends someone down a dead end."""
    assert "comfy-qat rdp" in how_to_get_in(WIN)
    assert "Remote Desktop" in how_to_get_in(WIN)
    assert "comfy-qat ssh" in how_to_get_in(LINUX)
    assert "comfy-qat rdp" not in how_to_get_in(LINUX)
    assert "comfy-qat ssh" in how_to_get_in(LINUX)


def test_a_box_that_never_reaches_running_gives_up_rather_than_hanging(tmp_path):
    _, say = said()
    gc = gcloud(["TERMINATED"] + ["STAGING"] * 50)
    with pytest.raises(LifecycleError, match="did not reach RUNNING"):
        bring_up(gc, WIN, say, tunnel_dir=tmp_path, sleep=lambda _: None,
                 probe_fn=lambda host: STAMP, boot_timeout=0)


def test_a_failure_to_start_names_the_box(tmp_path):
    """Two reads: the one before the start, and the one that asks whether the
    box came up anyway after the start reported a failure."""
    _, say = said()
    gc = gcloud(["TERMINATED", "TERMINATED"], fail=GcloudError("quota exceeded"))
    with pytest.raises(LifecycleError, match="could not start comfy-win"):
        bring_up(gc, WIN, say, tunnel_dir=tmp_path, sleep=lambda _: None)


def test_a_start_whose_answer_was_lost_is_not_reported_as_nothing_happening(tmp_path):
    """The request reached Google; the reply did not come back. The obvious
    reading — "nothing happened, try again" — is the expensive one: the box is
    coming up and the client is the only party that does not know.

    The fixture taught this mistake too. fakes.py asserted in a comment that "a
    start that raised leaves the box off, and nothing is billing"."""
    _, say = said()
    gc = gcloud(["TERMINATED", "STAGING"], fail=GcloudError("gcloud timed out"))

    with pytest.raises(LifecycleError) as caught:
        bring_up(gc, WIN, say, tunnel_dir=tmp_path, sleep=lambda _: None)

    assert "it started, and it is billing" in str(caught.value)
    assert "comfy-qat down comfy-win" in caught.value.fix
    assert "nothing needs retrying" in caught.value.fix


def test_a_start_that_really_failed_still_says_a_timeout_may_have_landed(tmp_path):
    _, say = said()
    gc = gcloud(["TERMINATED", "TERMINATED"], fail=GcloudError("gcloud timed out"))

    with pytest.raises(LifecycleError) as caught:
        bring_up(gc, WIN, say, tunnel_dir=tmp_path, sleep=lambda _: None)

    assert "list --live" in caught.value.fix


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
    gc = gcloud(["RUNNING"])
    put_away(gc, WIN, say, tunnel_dir=tmp_path)
    assert any(key.startswith("compute instances stop") for key in gc.calls)
    assert any("was running — stopped it" in line for line in lines)


def test_keep_running_says_plainly_that_it_still_costs(tmp_path):
    lines, say = said()
    gc = gcloud(["RUNNING"])
    still = put_away(gc, WIN, say, tunnel_dir=tmp_path, keep_running=True)
    assert not any(key.startswith("compute instances stop") for key in gc.calls)
    assert any("still billing" in line for line in lines)
    assert still == "billing"


def test_keep_running_does_not_invent_a_bill_for_a_stopped_box(tmp_path):
    """A live run printed "left running — it is still billing" about four hosts
    while three of them were TERMINATED. The day before, the same command claimed
    it had stopped machines it had deliberately left on. Both are one defect: a
    statement about money the tool never checked."""
    lines, say = said()
    gc = gcloud(["TERMINATED"])
    still = put_away(gc, WIN, say, tunnel_dir=tmp_path, keep_running=True)
    assert still == "idle"
    assert not any("billing" in line for line in lines), lines
    assert any("already stopped" in line for line in lines)


def test_not_knowing_whether_it_is_running_is_said_out_loud(tmp_path):
    """Not knowing is its own answer, and it is not "it is fine"."""
    from comfy_qa.gcloud import GcloudError

    def refuses(args, mode):
        raise GcloudError("credentials expired")

    gc = Gcloud(runner=refuses)
    lines, say = said()
    still = put_away(gc, WIN, say, tunnel_dir=tmp_path, keep_running=True)
    assert still == "unknown"
    assert any("could not tell" in line for line in lines)
    assert any("list --live" in line for line in lines)


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
        bring_up(gcloud(["TERMINATED", "TERMINATED"],
                        fail=GcloudError("no capacity")), WIN, say,
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
    assert "comfy-qat rdp" in caught.value.fix


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


# --- Ctrl-C during an install leaves nothing ticking --------------------------
#
# `ensure_installed` and the torch repair are the only two `output.slow` calls in
# this module that run on a THREAD; every other one passes background=False and
# has nothing to stop. Both were guarded by `except GcloudError`, and Ctrl-C is a
# BaseException, so it walked past untouched and left the ticker running.
#
# Measured before the fix: the thread survives the interrupt and outlives the
# whole run. Daemon, so nothing hangs — what it CAN do is print "still going"
# over the tool's own last words. `Can`, not `does`: the real tick is
# STREAM_TICK_SECONDS, a minute, so it lands inside the report only when the
# Ctrl-C falls in the last milliseconds before a tick.
#
# So the case is the SHAPE, not the window — a thread with no owner, whose
# visibility depends on when somebody happened to press a key. These tests assert
# the shape, which is why they check `_stop.is_set()` rather than watching for a
# line, and why they hold at any tick interval.


class _Interrupting:
    """A Gcloud whose streaming ssh is interrupted, as a Ctrl-C makes it."""

    def __init__(self, answer="MISSING"):
        self.answer = answer

    def ssh_output(self, instance, zone, project, remote):
        return self.answer

    def ssh(self, instance, zone, project, remote, *, stream=True):
        raise KeyboardInterrupt


def _watch_tickers(monkeypatch):
    """Record every `output.slow` the module starts, so a test can inspect it."""
    from comfy_qa import lifecycle as lifecycle_module

    started = []
    real = lifecycle_module.output.slow

    def watched(*args, **kwargs):
        ticker = real(*args, **kwargs)
        started.append(ticker)
        return ticker

    monkeypatch.setattr(lifecycle_module.output, "slow", watched)
    return started


def test_an_interrupted_install_leaves_no_ticker_running(monkeypatch):
    """PINS: every background ticker is stopped on the way out, Ctrl-C included."""
    from comfy_qa.lifecycle import ensure_installed

    started = _watch_tickers(monkeypatch)
    _, say = said()

    with pytest.raises(KeyboardInterrupt):
        ensure_installed(_Interrupting(), WIN, say)

    # Selected on "would have started a thread", not on `_thread is not None`:
    # stopping one sets `_thread = None`, so the obvious filter excludes exactly
    # the tickers this is about and the test passes by checking none of them.
    threaded = [t for t in started if t._background and t._every > 0]
    assert threaded, "no background ticker was started — the test proves nothing"
    for ticker in threaded:
        assert ticker._stop.is_set(), (
            "a ticker is still running after the interrupt; it will print "
            "'still going' over the report the tool is trying to make"
        )


def test_an_interrupted_torch_repair_leaves_no_ticker_running(monkeypatch):
    """The sibling site, which is the more likely of the two to be interrupted."""
    from comfy_qa.lifecycle import ensure_installed

    started = _watch_tickers(monkeypatch)
    _, say = said()

    # INSTALLED, so the install is skipped and `_verify` reaches the torch fetch.
    with pytest.raises(KeyboardInterrupt):
        ensure_installed(_Interrupting("INSTALLED\nNO_TORCH"), WIN, say)

    threaded = [t for t in started if t._background and t._every > 0]
    assert threaded, "no background ticker was started — the test proves nothing"
    for ticker in threaded:
        assert ticker._stop.is_set(), "a ticker outlived the interrupt"


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


class _SshRaises:
    """A Gcloud whose `ssh` raises, as a vanished binary does. Port is free."""

    def __init__(self, exc):
        self.exc = exc

    def ssh_output(self, instance, zone, project, remote):
        return "PORT_FREE"

    def ssh(self, instance, zone, project, remote, *, stream=True):
        raise self.exc


# `serve` and `start_detached` both wrap their `gc.ssh` launch, so a local gcloud
# fault at the launch goes through `_give_up` like every other failure past the
# point the machine is on: tunnel closed, and the bill named. `serve`'s call is
# also inside a try/FINALLY, which tidies the remote process; the catch is
# outside that, so the tidy-up runs first and the refusal is still ours.
#
# Reachable by a LOCAL fault only: `Gcloud.ssh` raises GcloudError when the binary
# has left PATH mid-session, or OSError out of subprocess.run. A failed REMOTE ssh
# returns a non-zero exit code, and every non-zero code is handled.


@pytest.mark.parametrize("launch", (serve, start_detached))
def test_a_gcloud_failure_at_the_launch_leaves_the_tunnel_open(tmp_path, launch):
    """A launch that raises still closes the tunnel it opened.

    Otherwise the box is left running and billing with a local port held open
    onto it, and the exception carries gcloud's own advice rather than this
    tool's.
    """
    from comfy_qa import tunnel as tunnel_module

    _, say = said()
    tunnel_module.open_tunnel(WIN, tmp_path)
    assert tunnel_module.pid_file("comfy-win", tmp_path).exists()

    with pytest.raises(LifecycleError):
        launch(_SshRaises(GcloudError("gcloud is not installed or not on PATH.")),
               WIN, say, probe_fn=lambda host: None, sleep=lambda _: None,
               tunnel_dir=tmp_path)

    assert not tunnel_module.pid_file("comfy-win", tmp_path).exists()


@pytest.mark.parametrize("launch", (serve, start_detached))
def test_a_gcloud_failure_at_the_launch_never_says_how_to_stop_paying(tmp_path, launch):
    """Every failure after the machine is on names `stop_paying(host)` — the
    rule this module's own docstring states.

    And gcloud's own advice survives alongside it: signing in again is the right
    first move, and it says nothing about the box that is on while you make it.
    """
    lines, say = said()
    with pytest.raises(LifecycleError) as caught:
        launch(_SshRaises(GcloudError("your gcloud session has expired",
                                      fix="gcloud auth login")),
               WIN, say, probe_fn=lambda host: None, sleep=lambda _: None,
               tunnel_dir=tmp_path)

    whole = "\n".join(lines) + f"\n{caught.value}\n{caught.value.fix}"
    assert stop_paying(WIN) in whole
    assert "gcloud auth login" in whole
    assert "your gcloud session has expired" in str(caught.value)


class _EverythingRaises:
    """A Gcloud where every call fails, including the ones asked BEFORE the
    launch — which is where an OSError used to get out."""

    def __init__(self, exc):
        self.exc = exc

    def __getattr__(self, name):
        def call(*args, **kwargs):
            raise self.exc
        return call


@pytest.mark.parametrize("launch", (serve, start_detached))
def test_an_oserror_anywhere_in_the_launch_still_names_the_bill(tmp_path, launch):
    """The same failure by the other route out of `subprocess`.

    `Gcloud.ssh` raises GcloudError when the binary has gone, and OSError —
    EMFILE, ENOMEM — when the process cannot be spawned at all. The launch call
    itself catches both, but `_port_holder` runs first and caught only
    GcloudError, so an OSError escaped as a traceback past every handler in the
    tool: tunnel open, box billing, nothing said. The four best-effort asks on
    this path now treat a local fault as one more way for a box not to answer.
    """
    from comfy_qa import tunnel as tunnel_module

    _, say = said()
    tunnel_module.open_tunnel(WIN, tmp_path)

    with pytest.raises(LifecycleError) as caught:
        launch(_EverythingRaises(OSError(24, "Too many open files")),
               WIN, say, probe_fn=lambda host: None, sleep=lambda _: None,
               tunnel_dir=tmp_path)

    assert stop_paying(WIN) in (caught.value.fix or "")
    assert not tunnel_module.pid_file("comfy-win", tmp_path).exists()


# --- interrupting the one command whose job is stopping the bill -----------


def test_stopping_says_so_before_it_starts_rather_than_only_afterwards():
    """A real `down` was blank for 30 seconds and then printed one line.

    Measured by driving it against a fake gcloud that sleeps: nothing at all
    reaches the terminal while the stop is in flight. That silence is also why
    an interrupt here felt like nothing had happened.
    """
    from comfy_qa.lifecycle import put_away

    lines, say = said()
    put_away(gcloud(["RUNNING"]), WIN, say)

    def first(fragment: str) -> int:
        return next(i for i, line in enumerate(lines) if fragment in line)

    assert first("stopping comfy-win") < first("stopped it"), lines


def test_an_interrupted_stop_is_not_silent_about_the_machine(capsys):
    """The mirror of an interrupted create, and the more expensive direction.

    `down` exists to stop the bill, so the person who typed it believes the bill
    stopped — and an interrupt printed NOTHING: exit 130, stdout and stderr both
    empty, not even a progress line. The stop request has already gone to
    Google, Ctrl-C reaches only the local gcloud, and nothing here can say
    whether it landed.
    """
    from comfy_qa import inflight
    from comfy_qa.lifecycle import put_away

    class _Interrupted:
        def instance_status(self, *args):
            return "RUNNING"

        def stop_instance(self, *args):
            raise KeyboardInterrupt

    with pytest.raises(inflight.Interrupted):
        put_away(_Interrupted(), WIN, lambda line: None)

    # The report fires inside `may_leave` now rather than in `cli.main`, so what
    # survives the raise is the printed message rather than the record. Reading
    # the message is the stronger check anyway: it is what the person who pressed
    # Ctrl-C actually sees, and the record was only ever a proxy for it.
    report = capsys.readouterr().err

    assert "comfy-win (comfy-win in us-central1-a)" in report, report
    assert "this may still be running" in report, (
        "an interrupted `down` needs its own sentence, and neither of the other "
        "two headings is true here: the box certainly exists, and whether the "
        "stop landed is the unknown"
    )
    # Re-running is free — stopping an already-stopped box succeeds trivially —
    # so the first thing offered is the command that settles it.
    assert "comfy-qat down comfy-win" in report
    assert "comfy-qat list --live" in report


def test_a_stop_that_returns_leaves_nothing_registered():
    """The guard on the registration: a `down` that finished has nothing to say
    about a machine that may still be running."""
    from comfy_qa import inflight
    from comfy_qa.lifecycle import put_away

    put_away(gcloud(["RUNNING"]), WIN, lambda line: None)
    assert inflight.pending() == []


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


def test_a_box_this_run_started_is_not_tunnelled_before_ssh_answers(tmp_path):
    """RUNNING is the VM powered on, not sshd listening.

    A real run started an Ubuntu box, saw RUNNING in 25s, opened the tunnel
    immediately and got `failed to connect to backend ... Failed to connect to
    port 22`. The tunnel process died, `go` reported "the tunnel closed", and
    nothing about that message points at the actual cause. The wait already
    existed and was already used before the install; the tunnel just raced it.
    """
    from comfy_qa.tunnel import TunnelError

    order: list[str] = []
    gc = gcloud(["TERMINATED", "RUNNING"])
    real_ssh = gc.ssh_output

    def watched(*args, **kwargs):
        order.append("ssh")
        return real_ssh(*args, **kwargs)

    gc.ssh_output = watched
    lines, say = said()

    def launcher(*a, **k):
        order.append("tunnel")
        raise TunnelError("stop here — the ordering is the whole assertion")

    with pytest.raises(LifecycleError):
        bring_up(gc, WIN, say, tunnel_dir=tmp_path, launcher=launcher,
                 probe_fn=lambda host: STAMP, sleep=lambda _: None)

    assert order[:2] == ["ssh", "tunnel"], (
        f"the tunnel was opened before sshd was known to be up: {order}"
    )


def test_a_box_already_running_is_not_made_to_prove_ssh_again(tmp_path):
    """It has had its chance to finish booting. Paying an SSH round trip on
    every `go` to re-establish that is a cost with no failure behind it."""
    asked: list[str] = []
    gc = gcloud(["RUNNING"])
    real_ssh = gc.ssh_output

    def watched(*args, **kwargs):
        asked.append("ssh")
        return real_ssh(*args, **kwargs)

    gc.ssh_output = watched
    lines, say = said()
    bring_up(gc, WIN, say, tunnel_dir=tmp_path, sleep=lambda _: None,
             probe_fn=lambda host: STAMP)

    assert not asked


# --- a new box installs its driver, and reboots doing it ---------------------
#
# `create` announces this: "installing the NVIDIA driver from its startup
# script, which reboots it once or twice. `go` waits that out." It did not. A
# real zero-setup run began installing prerequisites, the reboot dropped the
# session mid-apt, and it was reported as "the ComfyUI install did not finish"
# about a box that was merely restarting.

LINUX_GPU = Host(name="comfy-linux-2", kind="gce", port=8194, os="Ubuntu 22.04",
                 gpu="L4", gce_instance="comfy-linux-2", gce_zone="europe-west4-c",
                 gce_project="proj")


def _driver(answers):
    """A box that refuses `nvidia-smi` until the driver install has finished."""
    seen = iter(answers)

    def runner(args, mode):
        key = " ".join(args)
        if "nvidia-smi -L" in key:
            nxt = next(seen)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        raise AssertionError(f"unexpected: {key}")

    return Gcloud(runner=runner)


def test_a_dropped_connection_means_rebooting_not_broken(tmp_path):
    """The exact failure: the driver reboot closes the session mid-command."""
    from comfy_qa.lifecycle import wait_for_driver

    dropped = GcloudError(
        "client_loop: send disconnect: Broken pipe",
        raw="Connection to compute.129506869159352883 closed by remote host.",
    )
    gc = _driver([dropped, dropped, "GPU 0: NVIDIA L4"])
    lines, say = said()
    wait_for_driver(gc, LINUX_GPU, say, sleep=lambda _: None, tunnel_dir=tmp_path)

    assert any("waiting for the NVIDIA driver" in line for line in lines), lines


def test_a_box_whose_driver_is_ready_is_not_made_to_wait(tmp_path):
    from comfy_qa.lifecycle import wait_for_driver

    gc = _driver(["GPU 0: NVIDIA L4"])
    lines, say = said()
    wait_for_driver(gc, LINUX_GPU, say, sleep=lambda _: None, tunnel_dir=tmp_path)
    assert not any("waiting" in line for line in lines), lines


def test_windows_is_not_waited_on_because_its_driver_is_manual(tmp_path):
    """Google documents no unattended method, so there is nothing to wait for."""
    from comfy_qa.lifecycle import wait_for_driver

    def refuses(args, mode):
        raise AssertionError("Windows must not be probed for a driver")

    lines, say = said()
    wait_for_driver(Gcloud(runner=refuses), WIN, say, tunnel_dir=tmp_path)
    assert lines == []


def test_giving_up_on_the_driver_says_the_box_is_billing(tmp_path):
    """It is running by definition — it was created and started to get here."""
    from comfy_qa.lifecycle import wait_for_driver

    clock = iter([0, 1, 10_000, 10_001, 10_002])
    dropped = GcloudError("Broken pipe", raw="closed by remote host")
    gc = _driver([dropped, dropped, dropped])
    lines, say = said()

    with pytest.raises(LifecycleError) as caught:
        wait_for_driver(gc, LINUX_GPU, say, sleep=lambda _: None,
                        now=lambda: next(clock), tunnel_dir=tmp_path)

    assert "running and billing" in str(caught.value)
    assert "comfy-qat down comfy-linux-2" in caught.value.fix
    assert "installer.log" in caught.value.fix, "the installer keeps its own log"


# --- a machine has eight states, not two ------------------------------------
#
# `down` asked "is it RUNNING?" to decide whether to stop it, which treats the
# other six states as safe. A box in STAGING is thirty seconds from billing. The
# suite already knew STAGING existed — three tests feed it to `bring_up` — but
# nothing had ever fed a transitional state to the STOP path, and that asymmetry
# is why this survived a commit written specifically about telling the truth
# about money.

@pytest.mark.parametrize("state", ["STAGING", "PROVISIONING", "REPAIRING",
                                   "STOPPING", "SUSPENDING"])
def test_a_box_that_is_not_terminated_is_stopped_not_waved_through(tmp_path, state):
    lines, say = said()
    gc = gcloud([state])
    found = put_away(gc, WIN, say, tunnel_dir=tmp_path)

    assert any(key.startswith("compute instances stop") for key in gc.calls), (
        f"a box in {state} was left running and called already stopped"
    )
    assert found == "caught", found
    assert not any("already stopped" in line for line in lines), lines


@pytest.mark.parametrize("state", ["STAGING", "PROVISIONING", "REPAIRING"])
def test_keep_running_counts_a_starting_box_as_billing(tmp_path, state):
    """The under-reporting half, surviving inside the fix for the over-reporting
    half: this branch was corrected today for claiming a stopped box was billing,
    and still claimed a starting box was not."""
    lines, say = said()
    found = put_away(gcloud([state]), WIN, say, tunnel_dir=tmp_path,
                     keep_running=True)

    assert found == "billing", found
    assert any("still billing" in line for line in lines), lines


def test_only_terminated_counts_as_already_stopped(tmp_path):
    lines, say = said()
    gc = gcloud(["TERMINATED"])
    assert put_away(gc, WIN, say, tunnel_dir=tmp_path) == "idle"
    assert not any(key.startswith("compute instances stop") for key in gc.calls)


def test_a_describe_that_says_nothing_is_not_evidence_the_box_started(tmp_path):
    """`instance_status` returned the literal "UNKNOWN" when the describe came
    back empty, and "UNKNOWN" is neither TERMINATED nor RUNNING — so it was read
    as a real transitional state and took the CONFIDENT branch: "it started, and
    it is billing", with "nothing needs retrying". That asserts a bill on a read
    that told us nothing, and the advice is actively wrong if the box is off.

    The empty answer is a third outcome, not a state."""
    _, say = said()
    gc = gcloud(["TERMINATED", ""], fail=GcloudError("gcloud timed out"))

    with pytest.raises(LifecycleError) as caught:
        bring_up(gc, WIN, say, tunnel_dir=tmp_path, sleep=lambda _: None)

    assert "it started, and it is billing" not in str(caught.value)
    assert "nothing needs retrying" not in (caught.value.fix or "")
    assert "list --live" in caught.value.fix, "it has to say how to find out"


# --- a read that succeeded and said nothing --------------------------------
#
# `instance_status` returns "" when the describe worked and carried no state.
# That is a third answer, and it is falsy where the old "UNKNOWN" sentinel was
# truthy — so every `if state:` and every `!= RUNNING` downstream changed
# behaviour silently rather than comparing wrong. Nine call sites; one was
# taught about it when the sentinel changed.

def test_keep_running_does_not_bill_you_on_a_read_that_said_nothing(tmp_path):
    """The honest branch existed two lines above and fired only on GcloudError,
    so an empty answer fell into the confident billing claim instead."""
    lines, say = said()
    found = put_away(gcloud([""]), WIN, say, tunnel_dir=tmp_path, keep_running=True)

    assert found == "unknown", found
    assert not any("still billing" in line for line in lines), lines
    assert any("could not tell" in line for line in lines)


def test_the_stop_path_does_not_leave_a_gap_where_the_state_goes(tmp_path):
    lines, say = said()
    put_away(gcloud(["", ""]), WIN, say, tunnel_dir=tmp_path)
    assert not any(" was  — " in line for line in lines), lines


def test_logs_does_not_claim_a_box_is_off_from_a_read_that_said_nothing(tmp_path):
    from comfy_qa.lifecycle import read_logs

    with pytest.raises(LifecycleError) as caught:
        read_logs(gcloud([""]), WIN, said()[1])

    assert "could not tell" in str(caught.value)
    assert "not running" not in str(caught.value)


def test_a_linux_box_is_not_told_that_windows_is_slow(tmp_path):
    """Observed on a real run: an Ubuntu box that came up in nine seconds printed
    "waiting for the machine to accept commands — Windows takes a few minutes".

    A sentence about a different operating system is the tool sounding like it
    does not know which machine it is talking to, in the one command whose whole
    job is being certain of that."""
    from comfy_qa.gcloud import GcloudError
    from comfy_qa.lifecycle import wait_for_ssh

    answers = iter([GcloudError("not up yet"), "ok"])

    def runner(args, mode):
        nxt = next(answers)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    for host, expected in ((LINUX_GPU, False), (WIN, True)):
        answers = iter([GcloudError("not up yet"), "ok"])
        lines, say = said()
        wait_for_ssh(Gcloud(runner=runner), host, say, sleep=lambda _: None,
                     tunnel_dir=tmp_path)
        said_windows = any("Windows takes" in line for line in lines)
        assert said_windows is expected, (host.name, lines)
