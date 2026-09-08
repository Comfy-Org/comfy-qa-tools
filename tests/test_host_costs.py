"""Three things `comfy-qat host` did that a live run against a real project found.

They have one thing in common: each was a command being confidently wrong about a
machine, and the first one was wrong in the direction that bills.

  1. `host move --dry-run` started a GPU instance. There is no API that answers
     "where is there an L4 free", so `move` finds out by trying to start the box
     and reading the suggested zone out of the refusal — and `--dry-run` was not
     consulted until long after that. The one command that promises to change
     nothing was the one that could quietly cost the most, and the tester who
     found it did not re-run it, precisely because it would have started the
     machine again.
  2. `host stamp` gave a cloud box the local machine's advice. Nothing answers on
     8190 because there is no tunnel and the instance is stopped; the fix line
     said to start ComfyUI and check the port, neither of which is the problem.
  3. `host move` exited 1 with no `to fix:` line where `auth quota list` and
     `host discover` exit 2 with one, on the same gcloud error.

The rule the third one settles, and that this file holds the whole group to:
**exit 2 means nothing was changed** — a refusal, a precondition, a bad argument.
**Exit 1 means the work started and failed.**
"""

from __future__ import annotations

import ast

import pytest
from typer.testing import CliRunner

from comfy_qa.gcloud import GcloudError
from comfy_qa.host import app

HOSTS = """\
[hosts.local]
kind = "local"
port = 8188

[hosts.comfy-win]
kind         = "gce"
os           = "Windows Server 2022"
gpu          = "L4"
gce_instance = "comfy-win"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8190
"""

# Google's own wording, which is what `suggested_zones` reads the zone out of.
STOCKOUT = (
    "ERROR: (gcloud.compute.instances.start) Could not fetch resource:\n"
    " - The zone 'projects/proj/zones/us-central1-a' does not have enough "
    "resources available to fulfill the request. '(resource type:compute)'. "
    "Try again later or try your request in a different zone/region. "
    "We suggest trying your request in the us-central1-b zone.\n"
)


class Cloud:
    """A gcloud that records every call and refuses to be surprised.

    Nothing here returns a plausible default. A command reaching a call this
    fixture was not told to expect is the failure being tested, so it raises
    rather than quietly answering.
    """

    def __init__(self, *, start=None, describe=None, stop=None, status="RUNNING"):
        self.calls: list[str] = []
        self._start = start
        self._describe = describe
        self._stop = stop
        self._status = status

    def instance_status(self, instance, zone, project):
        # `down` reads before it stops, so that "stopped" means something
        # happened rather than that the call did not raise. RUNNING is the
        # realistic default for a command someone runs to stop paying.
        self.calls.append("instance_status")
        if isinstance(self._status, Exception):
            raise self._status
        return self._status

    def stop_instance(self, instance, zone, project):
        self.calls.append("stop_instance")
        if self._stop is not None:
            raise self._stop
        return ""

    def start_instance(self, instance, zone, project):
        self.calls.append("start_instance")
        if self._start is not None:
            raise self._start
        return ""

    # The reads `relocate.survey` makes before anything is touched. They are
    # answered rather than refused because they cost nothing and change nothing —
    # which is the whole distinction this file is about. `start_instance` is the
    # call that must not happen under --dry-run, and it is still recorded.
    def describe_instance(self, instance, zone, project):
        self.calls.append("describe_instance")
        return self._describe or {
            "name": instance,
            "machineType": "zones/us-central1-a/machineTypes/g2-standard-8",
            "disks": [{"boot": True, "source": f"…/disks/{instance}-a"}],
        }

    def list_instances(self, project):
        self.calls.append("list_instances")
        return []

    def run(self, args, **kwargs):
        self.calls.append("run")
        joined = " ".join(str(part) for part in args)
        if "disks list" in joined:
            return [{"name": "comfy-win-a", "zone": ".../us-central1-a",
                     "sizeGb": "300", "type": ".../pd-balanced",
                     "users": [".../comfy-win"]}]
        if "snapshots list" in joined:
            return []
        if "machine-types list" in joined:
            return [{"name": "g2-standard-8"}]
        raise AssertionError(f"gcloud run was not expected: {joined}")

    def __getattr__(self, name):
        def unexpected(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"{name} was not expected in this test")
        return unexpected


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """The real CLI with a fake cloud, and the tunnel records kept in tmp_path."""
    from comfy_qa import gcloud as gcloud_module, tunnel as tunnel_module

    monkeypatch.setattr(tunnel_module, "TUNNEL_DIR", tmp_path / "tunnels")

    def invoke(*args, cloud=None, input=None):
        cloud = cloud if cloud is not None else Cloud()
        path = tmp_path / "hosts.toml"
        path.write_text(HOSTS, encoding="utf-8")
        monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: cloud)
        result = CliRunner().invoke(app, [*args, "--config", str(path)], input=input)
        result.cloud = cloud        # type: ignore[attr-defined]
        return result

    return invoke


# --- 1. a dry run may not start a GPU instance --------------------------------


def test_a_dry_run_without_a_target_zone_starts_nothing(cli):
    """The blocker, stated as plainly as it can be: no `start_instance`, ever.

    Not "it is guarded further down" and not "it only happens when the zone is
    short" — the call is not made at all, and the command says why rather than
    appearing to work.
    """
    result = cli("move", "comfy-win", "--dry-run")

    assert "start_instance" not in result.cloud.calls, (
        "--dry-run started a GPU instance, which is the one thing it promises not "
        "to do"
    )
    assert result.exit_code == 2
    assert "--to us-central1-b --dry-run" in result.output, (
        "a refusal has to name the command that works"
    )


def test_no_call_at_all_is_made_on_that_path(cli):
    """The fake raises on any call it was not told to expect, so an empty call
    list is the assertion that a dry run contacted nothing whatever."""
    result = cli("move", "comfy-win", "--dry-run")

    assert result.cloud.calls == []
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_a_dry_run_with_a_target_zone_still_previews(cli):
    """The refusal must not take `--dry-run` away. Given `--to` there is nothing
    to probe for, so the whole plan prints without starting anything."""
    cloud = Cloud()
    cloud.describe_instance = lambda *a, **k: (  # type: ignore[method-assign]
        cloud.calls.append("describe_instance") or INSTANCE)

    result = cli("move", "comfy-win", "--to", "us-central1-b", "--dry-run", cloud=cloud)

    assert "start_instance" not in cloud.calls
    assert result.exit_code == 0, result.output
    assert "--dry-run: nothing changed" in result.output


def test_a_real_move_still_asks_google_where_there_is_capacity(cli):
    """The guard is on the dry run, not on the probe. A real `move` with no `--to`
    has nothing else to go on, and the start it does is one the command was going
    to make anyway. It reads the zone out of the stockout and carries on."""
    cloud = Cloud(start=GcloudError("could not start comfy-win", raw=STOCKOUT))
    cloud.describe_instance = lambda *a, **k: (  # type: ignore[method-assign]
        cloud.calls.append("describe_instance") or INSTANCE)

    result = cli("move", "comfy-win", cloud=cloud)

    # The survey reads follow the probe: they cost nothing and change nothing,
    # which is exactly the distinction this file exists to hold.
    assert cloud.calls[0] == "start_instance"
    assert "stop_instance" not in cloud.calls
    assert "us-central1-a has none free; us-central1-b does" in result.output
    # It asked, so it goes on to ask whether to do it — nothing was moved here.
    assert "Move comfy-win to us-central1-b?" in result.output


INSTANCE = {
    "name": "comfy-win",
    "machineType": "https://www.googleapis.com/compute/v1/projects/proj/zones/"
                   "us-central1-a/machineTypes/g2-standard-8",
    "disks": [{"boot": True, "source": "https://www.googleapis.com/compute/v1/"
                                       "projects/proj/zones/us-central1-a/disks/"
                                       "comfy-win"}],
    "metadata": {"items": [{"key": "enable-windows-ssh", "value": "true"}]},
}


# --- 2. a cloud box that did not answer is not a local one --------------------


def test_a_cloud_box_with_no_tunnel_is_told_about_the_tunnel(cli, monkeypatch):
    """`fetch` is handed a URL, so its advice is the local advice: start ComfyUI,
    check the port. For a stopped box behind no tunnel both are wrong, and the
    tester following them looks in two places that were never the problem."""
    import comfy_qa.host as host_module
    from comfy_qa.stamp import ProbeError

    monkeypatch.setattr(host_module, "fetch", lambda url, host: (_ for _ in ()).throw(
        ProbeError(f"nothing answered at {url}",
                   fix="start ComfyUI on that machine, or check the port in your "
                       "list")))

    result = cli("stamp", "comfy-win")

    assert result.exit_code == 1
    assert "open comfy-win" in result.output
    assert "go comfy-win" in result.output
    assert "may simply be stopped" in result.output
    assert "start ComfyUI on that machine" not in result.output, (
        "the local advice was kept for a cloud box"
    )


def test_a_local_host_keeps_the_local_advice(cli, monkeypatch):
    """Which is correct for it, and is the only advice there is."""
    import comfy_qa.host as host_module
    from comfy_qa.stamp import ProbeError

    monkeypatch.setattr(host_module, "fetch", lambda url, host: (_ for _ in ()).throw(
        ProbeError(f"nothing answered at {url}",
                   fix="start ComfyUI on that machine, or check the port in your "
                       "list")))

    result = cli("stamp", "local")

    assert result.exit_code == 1
    assert "start ComfyUI on that machine" in result.output
    assert "open" not in result.output


def test_a_cloud_box_with_a_tunnel_open_keeps_the_probe_s_own_advice(cli, monkeypatch,
                                                                     tmp_path):
    """With a tunnel up the port is being forwarded and the answer came from the
    far end, so `fetch` knows more about what went wrong than the host list does.
    Telling someone to open a tunnel that is already open is the same defect
    pointing the other way."""
    import comfy_qa.host as host_module
    from comfy_qa import tunnel as tunnel_module
    from comfy_qa.config import Host
    from comfy_qa.stamp import ProbeError
    import os

    host = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022",
                gpu="L4", gce_instance="comfy-win", gce_zone="us-central1-a",
                gce_project="proj")
    tunnel_module.open_tunnel(host, launcher=lambda cmd, log: os.getpid(),
                              port_busy=lambda port: False)

    monkeypatch.setattr(host_module, "fetch", lambda url, host: (_ for _ in ()).throw(
        ProbeError(f"{url} answered 404 for /system_stats",
                   fix="check the port — this looks like a web server, but not a "
                       "ComfyUI")))

    result = cli("stamp", "comfy-win")

    assert result.exit_code == 1
    assert "this looks like a web server" in result.output
    assert "open comfy-win" not in result.output


# --- 3. one exit code, and never a dropped fix line ---------------------------


def test_a_gcloud_refusal_during_move_exits_2_with_its_fix(cli):
    """Nothing has been changed, so it is a refusal, and the fix line survives.

    This is the same class of failure that `auth quota list` and `host discover`
    already reported as exit 2 with a `to fix:` line. `move` reported it as exit 1
    with no fix at all, and an exit code that means something different per
    command is worse than no exit code.

    `status="TERMINATED"` was added when the probe learned to read the machine
    back, and it is what a refusal ACTUALLY looks like — the start was refused,
    so the box is still off. The fake said RUNNING before, which is the timeout
    shape and not this one: a start that was refused and a machine that is
    running is not a state the project can be in. Every assertion here is
    unchanged; the fixture now describes the case the name claims.
    """
    result = cli("move", "comfy-win", cloud=Cloud(
        status="TERMINATED",
        start=GcloudError(
            "your Google session has expired", raw="ERROR: reauthentication required",
            fix="comfy-qat login")))

    assert result.exit_code == 2
    assert "your Google session has expired" in result.output
    assert "to fix: comfy-qat login" in result.output


# --- 10. the probe is a start, so a probe whose answer was lost may be billing --
#
# The mirror of section 8, on the other side of the same `start_instance`, and
# the guard that catches this class could not see it: it read one module.
#
#     $ comfy-qat move comfy-win
#       asking Google where there is capacity
#     timed out waiting for the operation to complete
#     exit: 2
#
# Nothing about the bill, and exit 2 is not neutral — `_refused`'s own docstring
# defines it as "nothing was changed". So the command positively asserted that
# nothing happened, at the one moment nobody can know it, about a probe whose own
# comment says "a try that is not refused leaves a GPU box running".
#
# The capacity branch is fine and is checked first: a stockout refusal means
# nothing started, so saying nothing about money is correct there.


def test_a_probe_that_times_out_with_the_box_up_says_it_is_billing(cli):
    """The state was read back and it came back running, so nothing is hedged."""
    cloud = Cloud(status="RUNNING",
                  start=GcloudError("timed out waiting for the operation",
                                    raw="ERROR: operation timed out"))
    result = cli("move", "comfy-win", cloud=cloud)

    # 1, not 2. The work started and failed, which is what 1 is for; 2 would
    # assert that nothing changed about a box that is running.
    assert result.exit_code == 1, result.output
    assert "billing" in result.output, result.output
    assert ("gcloud compute instances stop comfy-win --zone=us-central1-a"
            in result.output), result.output


def test_a_probe_that_times_out_and_cannot_be_re_read_claims_nothing(cli):
    """Both calls failed. "It is running" asserts what was not read, and a bare
    "could not start" reads as though nothing happened."""
    # `Cloud` raises when `status` is an exception, and the probe's re-read is
    # the FIRST status call on this path — `_zone_with_capacity` starts the box
    # without reading it first, which is the whole reason the read happens here.
    cloud = Cloud(status=GcloudError("still timing out"),
                  start=GcloudError("timed out waiting for the operation",
                                    raw="ERROR: operation timed out"))
    result = cli("move", "comfy-win", cloud=cloud)

    assert result.exit_code == 1, result.output
    assert "may be running and billing" in result.output, result.output
    assert ("gcloud compute instances stop comfy-win --zone=us-central1-a"
            in result.output), result.output


def test_a_probe_refused_with_the_box_still_off_stays_a_refusal(cli):
    """The re-read proves TERMINATED, so nothing started and 2 is honest. This is
    the branch that keeps the rule above true rather than making it conditional."""
    cloud = Cloud(status="TERMINATED",
                  start=GcloudError("the zone does not exist",
                                    raw="ERROR: invalid zone", fix="check --to"))
    result = cli("move", "comfy-win", cloud=cloud)

    assert result.exit_code == 2, result.output
    assert "billing" not in result.output, result.output


def test_no_zone_suggested_is_also_a_refusal(cli):
    """It stopped before doing anything, so it exits the same way."""
    result = cli("move", "comfy-win", cloud=Cloud(start=GcloudError(
        "could not start comfy-win",
        raw="ERROR: does not have enough resources available to fulfill the request")))

    assert result.exit_code == 2
    assert "pick one with --to" in result.output


def test_moving_a_local_host_was_already_a_refusal_and_stays_one(cli):
    """The rule has to describe what the group already does, or it is not a rule."""
    assert cli("move", "local").exit_code == 2


def test_down_all_stops_every_cloud_machine(cli):
    """The question at the end of a session is never "is comfy-win stopped", it
    is "am I still paying for anything" — and answering that by naming each box
    in turn is how one gets missed. Asked for twice in one day."""
    result = cli("down", "--all")

    assert result.exit_code == 0, result.output
    assert result.cloud.calls.count("stop_instance") == 1, "the cloud box"
    assert "local" not in result.output, "a local install cannot be stopped"
    # Not the bare substring "stopped" — it matches all four put_away verdicts
    # and cannot tell "was already stopped" from "was running — stopped it",
    # which is the distinction this command exists to make.
    assert "was running — stopped it" in result.output
    # That line is `put_away`'s, printed once per host, so this test named for
    # `down --all` still held nothing `down --all` writes. Its own summary is
    # the answer to the question the command exists for — am I still paying for
    # anything — and it names the boxes that were costing money.
    assert "was billing: comfy-win. Stopped. Nothing is now." in result.output, (
        "the closing summary is the command's own line; put_away cannot write it"
    )


def test_down_all_keeps_going_when_one_refuses(cli):
    """Stopping the rest is the whole point, so one failure must not abandon
    the others — and the ones still billing are named at the end."""
    from comfy_qa.gcloud import GcloudError

    cloud = Cloud(stop=GcloudError("boom"))
    result = cli("down", "--all", cloud=cloud)

    assert result.exit_code == 1
    assert cloud.calls.count("stop_instance") == 1, "it tried"
    assert "may still be billing" in result.output


def test_down_all_refuses_a_name_as_well(cli):
    """"Stop everything" and "stop this one" are different instructions."""
    result = cli("down", "comfy-win", "--all")

    assert result.exit_code == 2
    assert "takes no name" in result.output


# --- 4. a summary about money may not contradict itself -----------------------
#
# `down --all --keep-running` deliberately left every machine on, and then the
# closing line said "all N stopped." Each host had already printed "still
# billing" immediately above it. The one command whose purpose is answering "am I
# still paying" answered it wrongly, in the direction that costs.
#
# That flag is gone and its half of this section went with it — the test named
# for it, and the summary arm it drove. What is left is the ordinary case, which
# was written as the GUARD on that test and is now the only one: `--all` still
# has to distinguish boxes it stopped from boxes that were already off, and that
# is the distinction the old closing line could not make.


def test_stopping_them_still_says_so(cli):
    """The guard above must not swallow the ordinary case."""
    class Stopped(Cloud):
        def instance_status(self, name, zone, project):
            self.calls.append("instance_status")
            return "RUNNING"

        def stop_instance(self, name, zone, project):
            self.calls.append(f"compute instances stop {name}")

    result = cli("down", "--all", cloud=Stopped())
    assert "was billing: comfy-win" in result.output
    assert "Nothing is now" in result.output
    assert result.exit_code == 0


def test_a_clean_session_and_a_dirty_one_do_not_look_the_same(cli):
    """The whole point. "all N stopped." was printed whether five GPU boxes had
    been billing all night or none, because stopping an already-stopped box
    succeeds trivially. Someone closing the laptop could not tell the two apart,
    on the one question they stayed up to answer."""
    class Idle(Cloud):
        def instance_status(self, instance, zone, project):
            self.calls.append("instance_status")
            return "TERMINATED"

    clean = cli("down", "--all", cloud=Idle())
    assert "nothing was running" in clean.output
    assert "stop_instance" not in clean.cloud.calls, (
        "an already-stopped box does not need stopping"
    )

    dirty = cli("down", "--all")
    assert "was billing: comfy-win" in dirty.output
    assert clean.output != dirty.output, (
        "the two sessions must not produce the same words"
    )


# --- 5. the rebuild is offered, never taken ----------------------------------
#
# `go` detected the stockout, read the zone out of Google's refusal and printed
# the `move` command — then stopped, leaving the user to type what the tool had
# already worked out. Offering it is the fix; doing it silently is not, because a
# move copies a whole boot disk, takes minutes, and bills from the moment the new
# box exists.


def _stuck(monkeypatch, *, tty: bool):
    from comfy_qa import gcloud as gcloud_module
    monkeypatch.setattr(gcloud_module, "can_prompt", lambda: tty)

    class Stuck(Cloud):
        def instance_status(self, name, zone, project):
            self.calls.append("instance_status")
            return "TERMINATED"

        def start_instance(self, name, zone, project):
            self.calls.append("start_instance")
            raise GcloudError("---", raw=STOCKOUT)

    return Stuck()


def test_a_stockout_offers_the_rebuild_rather_than_only_naming_it(cli, monkeypatch):
    moved = []
    from comfy_qa import host as host_module
    monkeypatch.setattr(host_module, "move_cmd",
                        lambda **kw: moved.append(kw))

    cli("go", "comfy-win", cloud=_stuck(monkeypatch, tty=True), input="y\n")

    assert moved, "the offer was accepted and nothing moved"
    assert moved[0]["to"] == "us-central1-b", "it must use the zone Google named"
    assert moved[0]["yes"] is True, "the user already answered the question"


def test_declining_the_rebuild_changes_nothing(cli, monkeypatch):
    moved = []
    from comfy_qa import host as host_module
    monkeypatch.setattr(host_module, "move_cmd", lambda **kw: moved.append(kw))

    result = cli("go", "comfy-win", cloud=_stuck(monkeypatch, tty=True), input="n\n")

    assert not moved
    assert result.exit_code == 1


def test_nothing_is_offered_where_it_cannot_be_answered(cli, monkeypatch):
    """A pipe or a script must not stop on a question nobody will see."""
    moved = []
    from comfy_qa import host as host_module
    monkeypatch.setattr(host_module, "move_cmd", lambda **kw: moved.append(kw))

    result = cli("go", "comfy-win", cloud=_stuck(monkeypatch, tty=False))

    assert not moved
    assert result.exit_code == 1
    assert "us-central1-b" in result.output, "it still says where to go"


def test_switch_never_offers_a_rebuild(cli, monkeypatch):
    """switch stops the other boxes straight after bringing one up. A move
    confirmed in the middle leaves it half executed — old box not stopped, new
    box not up."""
    moved = []
    from comfy_qa import host as host_module
    monkeypatch.setattr(host_module, "move_cmd", lambda **kw: moved.append(kw))

    cli("switch", "comfy-win", cloud=_stuck(monkeypatch, tty=True), input="y\n")
    assert not moved


def test_a_box_whose_state_could_not_be_read_is_not_an_all_clear(cli):
    """`unknown` was collected and never reported in the default branch, so a run
    where the read failed and the stop succeeded printed "nothing was running, so
    nothing was billing" — contradicting the honest per-host line three lines
    above it, and saying it in the one case where the tool could not see.

    The trigger is precise and real: `instance_status` raises, `stop_instance`
    then succeeds. A stale token, a network blip on a laptop being closed for the
    night, rate limiting across five sequential calls. `down --all` is by
    definition the end of a long session and takes one chance per box.
    """
    from comfy_qa.gcloud import GcloudError

    class Unreadable(Cloud):
        def instance_status(self, instance, zone, project):
            self.calls.append("instance_status")
            raise GcloudError("credentials expired")

    result = cli("down", "--all", cloud=Unreadable())

    assert "nothing was running" not in result.output, (
        "an all-clear about a box whose state was never established"
    )
    assert "could not be checked" in result.output
    assert "comfy-win" in result.output, "the machine has to be named"
    assert "list --live" in result.output


# --- the host list is not the project ----------------------------------------
#
# `down --all` iterated the host list, so a running box nobody had declared was
# invisible to every sentence it printed. That was survivable while the closing
# line read "all N stopped". It stopped being survivable when it became "nothing
# is now", which is a promise about the project rather than about a file.


def test_a_running_box_nobody_declared_is_named(cli):
    class Wider(Cloud):
        def instance_status(self, instance, zone, project):
            self.calls.append("instance_status")
            return "TERMINATED"

        def list_instances(self, project):
            self.calls.append("list_instances")
            return [
                {"name": "comfy-win", "status": "TERMINATED",
                 "zone": ".../zones/us-central1-a"},
                {"name": "somebody-elses-box", "status": "RUNNING",
                 "zone": ".../zones/europe-west4-c"},
            ]

    result = cli("down", "--all", cloud=Wider())

    assert "nothing was running" not in result.output, (
        "an all-clear while a GPU box on the project is up"
    )
    assert "somebody-elses-box" in result.output
    assert "europe-west4-c" in result.output, "the command has to be runnable"
    assert result.exit_code == 0


def test_it_names_them_rather_than_stopping_them(cli):
    """Stopping a machine this tool does not manage is beyond what `down` was
    asked to do, and the surprise would be worse than the bill. Saying nothing
    is what makes the summary a lie."""
    class Wider(Cloud):
        def instance_status(self, instance, zone, project):
            return "TERMINATED"

        def list_instances(self, project):
            return [{"name": "stranger", "status": "RUNNING",
                     "zone": ".../zones/us-central1-a"}]

    result = cli("down", "--all", cloud=Wider())
    stops = [c for c in result.cloud.calls if "stop" in c]
    assert not any("stranger" in c for c in stops), stops


def test_a_project_that_cannot_be_listed_does_not_break_a_good_run(cli):
    """This runs after the work is done. A project that will not list must not
    turn a successful `down` into a failure — the summary just says less."""
    from comfy_qa.gcloud import GcloudError

    class Blind(Cloud):
        def instance_status(self, instance, zone, project):
            return "RUNNING"

        def stop_instance(self, instance, zone, project):
            self.calls.append(f"compute instances stop {instance}")

        def list_instances(self, project):
            raise GcloudError("credentials expired")

    result = cli("down", "--all", cloud=Blind())
    assert result.exit_code == 0
    assert "was billing" in result.output


LOCAL_ONLY = """\
[hosts.local]
kind = "local"
port = 8188
"""


def test_no_declared_cloud_hosts_does_not_mean_nothing_is_billing(tmp_path, monkeypatch):
    """The worst path for the old assertion to survive on. "no cloud machines are
    declared, so nothing can be billing" is a claim about the PROJECT made without
    asking it — and no declared hosts is exactly when this tool knows least and
    the project is most likely to hold something nobody adopted.

    The survey was added for this case and then placed after the early return, so
    it never ran here. Built without the `cli` fixture on purpose: that fixture
    appends its own --config after the caller's, so it cannot express this case.
    """
    from comfy_qa import gcloud as gcloud_module

    class Wider(Cloud):
        def current_project(self):
            self.calls.append("current_project")
            return "proj"

        def list_instances(self, project):
            self.calls.append("list_instances")
            return [{"name": "somebody-elses-box", "status": "RUNNING",
                     "zone": ".../zones/europe-west4-c"}]

    cloud = Wider()
    path = tmp_path / "local-only.toml"
    path.write_text(LOCAL_ONLY, encoding="utf-8")
    monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: cloud)
    result = CliRunner().invoke(app, ["down", "--all", "--config", str(path)])

    assert "nothing can be billing" not in result.output, result.output
    assert "somebody-elses-box" in result.output
    assert "europe-west4-c" in result.output, "the command has to be runnable"
    assert "list_instances" in cloud.calls, "it never asked the project"
    assert result.exit_code == 0


def test_no_declared_hosts_and_a_quiet_project_says_both(tmp_path, monkeypatch):
    from comfy_qa import gcloud as gcloud_module

    class Quiet(Cloud):
        def current_project(self):
            return "proj"

        def list_instances(self, project):
            return []

    path = tmp_path / "local-only.toml"
    path.write_text(LOCAL_ONLY, encoding="utf-8")
    monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: Quiet())
    result = CliRunner().invoke(app, ["down", "--all", "--config", str(path)])

    assert "nothing is running on the project either" in result.output
    assert result.exit_code == 0


def test_disconnect_leaves_the_machine_running_and_says_so(cli):
    """The capability `--keep-running` existed for, under a name that says it.

    It is now the only way to reach it: the flag was removed, and this is where
    the branch it used to share with `down` is covered.
    """
    class Live(Cloud):
        def instance_status(self, instance, zone, project):
            self.calls.append("instance_status")
            return "RUNNING"

    result = cli("disconnect", "comfy-win", cloud=Live())

    assert result.exit_code == 0
    # Same shape: "still billing" is lifecycle's. `disconnect`'s own closing line
    # — the only place it says how to stop paying — was asserted by nothing, in
    # nine tests that ran it.
    assert "still billing" in result.output
    # And the substring `comfy-qat down comfy-win` did not fix that, because
    # `put_away` writes it too, four words earlier, as `when the work is
    # finished: comfy-qat down comfy-win`. Deleting host.py's line left this
    # test GREEN — and left no failure anywhere, because the only other thing
    # holding that line was a docs test parametrised over the string constants
    # in the package, which loses a CASE when one goes. A count that drops from
    # 118 to 117 with nothing red is the bug, not the alarm.
    #
    # So pin the rendering, not the words in it. `  <command>   # <why>` is this
    # tool's own shape for an offered command and is host.py's; `<why>:
    # <command>` is lifecycle's per-host prose. Nothing else can satisfy this.
    assert "comfy-qat down comfy-win   # when the work is finished" in result.output, (
        "disconnect's own stop-paying line, in its own shape — not put_away's"
    )
    assert "stop_instance" not in result.cloud.calls, "disconnect must not stop it"


def test_disconnect_says_how_to_stop_paying_when_the_state_is_unreadable(cli):
    """The path that is the whole reason the command writes its own line.

    `put_away` offers the way to stop paying only where it has established that
    the box is RUNNING. Where the read comes back empty it says so and stops —
    and that is exactly the case where someone may be paying for a box nobody
    can confirm. `disconnect`'s own closing line is then the ONLY stop-paying
    advice in the output, so the duplication on the RUNNING path is the price of
    covering this one.
    """
    class Unreadable(Cloud):
        def instance_status(self, instance, zone, project):
            self.calls.append("instance_status")
            return ""

    result = cli("disconnect", "comfy-win", cloud=Unreadable())

    assert result.exit_code == 0
    assert "could not tell whether comfy-win is running" in result.output
    assert "when the work is finished: " not in result.output, (
        "put_away says nothing about the bill here — that is what this covers"
    )
    assert "comfy-qat down comfy-win   # when the work is finished" in result.output
    assert "stop_instance" not in result.cloud.calls, "disconnect must not stop it"


def test_a_project_that_could_not_be_checked_is_not_an_all_clear(cli):
    """The fix's own shape turned against it. The survey fails silently so a
    project that will not list cannot break a successful `down` — but if it then
    says nothing, the all-clear prints with no survey behind it, which is the
    original defect wearing the fix's clothes.

    "Nothing is running on the project" is a claim. An unread project does not
    support it, and an unread project and an empty one must not produce the same
    sentence."""
    from comfy_qa.gcloud import GcloudError

    class Blind(Cloud):
        def instance_status(self, instance, zone, project):
            return "TERMINATED"

        def list_instances(self, project):
            raise GcloudError("credentials expired")

    result = cli("down", "--all", cloud=Blind())

    assert "nothing was running" not in result.output, result.output
    assert "not an all-clear" in result.output
    assert "list --live" in result.output
    assert result.exit_code == 0, "it still must not fail a successful down"


def test_the_same_holds_with_no_declared_cloud_hosts(tmp_path, monkeypatch):
    from comfy_qa import gcloud as gcloud_module
    from comfy_qa.gcloud import GcloudError

    class Blind(Cloud):
        def current_project(self):
            return "proj"

        def list_instances(self, project):
            raise GcloudError("credentials expired")

    path = tmp_path / "local-only.toml"
    path.write_text(LOCAL_ONLY, encoding="utf-8")
    monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: Blind())
    result = CliRunner().invoke(app, ["down", "--all", "--config", str(path)])

    assert "nothing is running on the project either" not in result.output
    assert "not an all-clear" in result.output
    assert result.exit_code == 0


# --- the success paths say how to stop paying, and something enforces it ------
#
# The codebase states this rule for FAILURES — lifecycle's module docstring says
# "every failure after the machine has been started says how to stop paying for
# it", and `_with_the_bill` enforces it. Nothing stated or enforced the SUCCESS
# side, so six commands hand-wrote it and two forgot: `up` ended on "open <url>"
# with a box running and billing, and the single-host form that left a box up
# said "still billing" and then offered `logs`. That form is `disconnect` now.
#
# A sweep of all eleven commands put the violation set at exactly those two,
# which is what makes this a calibrated rule rather than an invented one.

# Hand-maintained, and that is the third such collection in this codebase today.
# ERROR_TYPES silently DROPPED two members and had to be made self-checking;
# BUILT_IN_CARD could silently GAIN one. This one fails the second way: a new
# command that leaves a box running, never added here, is simply not checked.
#
# So the list is derived rather than typed. Anything in host.py that reaches
# `bring_up` — the one call that starts a machine — is a command that can leave
# one running, and has to name the bill.
# `down_cmd` is DECLARED rather than derived, and it used not to be: the
# derivation reached it through `put_away(..., keep_running=keep_running)`, and
# `--keep-running` was removed. It stays on the list because the rule still
# applies to it — its `billing` verdict says a box is left running — and dropping
# it would silently stop checking the one command whose entire subject is the
# bill. Keeping a command here is always safe; the list is what the rule applies
# to, not what is suspected.
BILLABLE_ENDINGS = ("up_cmd", "go_cmd", "switch_cmd", "create_cmd",
                    "disconnect_cmd", "down_cmd", "move_cmd")

# The two vocabularies, and they must stay disjoint.
#
# INCLUSION_VOCABULARY puts a command INTO the billable set. BILL_TOKENS is what
# clears it.
# Until today `_serve(` and `put_away` were in both, so a command was cleared by
# the very call that made it billable — the evidence of guilt accepted as the
# alibi. Three of the six passed on nothing else.
#
# What that costs is not theoretical. A command was added that starts a box,
# leaves it running and prints only "benchmark finished". The guard failed and
# printed its remedy: add it to BILLABLE_ENDINGS. Doing exactly that turned the
# suite green over a command that leaves a GPU billing and says nothing. A guard
# whose printed remedy defeats it is worse than none, because it is trusted.
# What makes a box exist and bill, named at the only layer where it is a fact
# rather than a convention: the gcloud calls themselves.
#
# This used to be a hand-typed list of the HELPERS that call them — bring_up,
# _serve, build, _bring_up — which is the same hand-maintained-collection defect
# as the two above, one layer down. It missed `move`. `_zone_with_capacity`
# starts a box through `gc.start_instance` (its own docstring: "when it is *not*
# refused the box is up and billing"), that is not a name anybody thought to
# type, and it sits one call below `move_cmd` rather than in it. So `move` was
# in NO list: not derived, not declared, and nothing checked that the one
# command which starts a GPU box to ask Google a question says how to stop it.
#
# Keyed on these three, every helper that starts a box is reached rather than
# remembered, and a fourth way to start one cannot be added without adding it
# here — where the compiler-ish check below insists it is a real gcloud method.
STARTS_A_BOX = frozenset({"start_instance", "create_instance_from_image",
                          "create_instance_from_disk"})

# Not a start: `put_away` LEAVES one up, and only under keep_running.
KEEPS_A_BOX_UP = "put_away"

INCLUSION_VOCABULARY = STARTS_A_BOX | {KEEPS_A_BOX_UP}

BILL_TOKENS = ("comfy-qat down", "stop_paying", "_with_the_bill")

# The same rule one layer out, for FAILURE handlers only, and kept separate from
# BILL_TOKENS on purpose: widening the vocabulary the ordinary-path guard reads
# would loosen that guard too, and it is the tight one.
#
# Three more ways a failure — and only a failure — says what is still costing
# money. Each is a function whose whole job is that sentence, so accepting the
# name is accepting the thing it builds, not a word near it:
#
#   `_raw_stop`      lifecycle's `gcloud compute instances stop ...`, complete
#                    with zone and project. Offered where `comfy-qat down` is
#                    the command that has just failed.
#   `_state_after`   relocate's "what exists because of this run, and the
#                    commands that remove it" — the bill for a half-done move.
#   "still billing"  said inline, by the one branch of `run_move` that reports a
#                    leftover rather than raising: a snapshot that would not
#                    delete, with its delete command on the same line.
FAILURE_BILL_TOKENS = BILL_TOKENS + ("_raw_stop", "_state_after", "still billing")


def _called_name(call: ast.Call) -> str:
    func = call.func
    return (func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute) else "")


def _leaves_it_running(call: ast.Call) -> bool:
    """Whether this call is one that can leave a box up.

    `put_away` is the odd one: it STOPS a box unless `keep_running=True`, and it
    prints the bill only on that branch. So the same condition governs both
    sides — it makes a command billable only when it keeps the box, and it can
    only clear one when it keeps the box. Asymmetry here would be the same
    defect wearing the other hat.
    """
    if _called_name(call) != "put_away":
        return True
    return any(kw.arg == "keep_running" for kw in call.keywords)


def _functions_by_name() -> dict[str, list[ast.FunctionDef]]:
    """Every function in the four modules this rule has to be able to see.

    `_serve` is in host.py, `put_away` in lifecycle.py, `build` in create.py,
    and the calls that actually start a box are methods of `Gcloud` in
    gcloud.py. A command that defers its ending to a function this does not read
    can never be cleared; a start that happens in a module this does not read is
    never found at all, which is exactly how `move` went unlisted.
    """
    import inspect

    from comfy_qa import create as create_module
    from comfy_qa import gcloud as gcloud_module
    from comfy_qa import host as host_module
    from comfy_qa import lifecycle as lifecycle_module

    found: dict[str, list[ast.FunctionDef]] = {}
    for module in (host_module, lifecycle_module, create_module, gcloud_module):
        source = inspect.getsource(module)
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef):
                # `ast.get_source_segment` needs the module text this node came
                # from, so it is resolved now rather than carried around.
                node._body_text = ast.get_source_segment(source, node) or ""
                found.setdefault(node.name, []).append(node)
    return found


def _bodies_by_name() -> dict[str, list[str]]:
    """The same functions, as source text, for the alibi scan."""
    return {name: [node._body_text for node in nodes]
            for name, nodes in _functions_by_name().items()}


def _names_the_bill(body: str) -> bool:
    """Whether this body tells the user how to stop paying ON THE ORDINARY PATH.

    The rule being enforced is about SUCCESS. lifecycle's docstring already
    states the failure half — "every failure after the machine has been started
    says how to stop paying for it" — and `_with_the_bill` enforces it. So an
    alibi drawn from a failure is no alibi for a success ending.

    Two things are therefore not evidence, and both were found by measuring
    rather than by reading:

    - An IMPORT. With all five of `_serve`'s stop_paying lines replaced by
      `say.result("")`, the sixth occurrence — `from .lifecycle import …
      stop_paying …` — still cleared it. Importing a function says nothing to
      anybody.
    - Advice attached to an abnormal exit: a `raise`, a `fix=`, an `undo=`.
      `bring_up` names the bill nine times and every one is a `fix=` or the
      `undo=` of an interrupt handler. Counting those cleared a `bench` command
      that starts a box, leaves it running and prints "benchmark finished" —
      the exact demonstration this guard exists to fail.
    """
    return any(token in _on_the_ordinary_path(body) for token in BILL_TOKENS)


# Keyword arguments that carry advice for an abnormal exit rather than output on
# the ordinary path.
ADVICE_ARGS = frozenset({"fix", "undo"})


def _on_the_ordinary_path(body: str) -> str:
    """`body` with prose, imports, raises and abnormal-exit advice removed.

    Prose is stripped — comments and docstrings — because a sentence ABOUT the
    rule is not the rule. `_zone_with_capacity` carries a comment reading "every
    lifecycle failure through `_with_the_bill`", and that comment on its own was
    enough to clear a command. Nothing clears on prose alone today, so this
    changes no verdict; it closes the door rather than a hole, and it is the same
    door the import was.
    """
    import io
    import textwrap
    import tokenize

    text = textwrap.dedent(body)
    try:
        text = tokenize.untokenize(
            token for token in tokenize.generate_tokens(io.StringIO(text).readline)
            if token.type != tokenize.COMMENT)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass                       # pragma: no cover - fall back to the raw text
    try:
        tree = ast.parse(text)
    except SyntaxError:            # pragma: no cover - a body we cannot parse
        return text

    skip: set[int] = set()

    def drop(node: ast.AST) -> None:
        first = getattr(node, "lineno", None)
        if first is not None:
            skip.update(range(first, (getattr(node, "end_lineno", first) or first) + 1))

    for node in ast.walk(tree):
        if isinstance(node, ast.Raise | ast.Import | ast.ImportFrom):
            drop(node)
        elif (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
              and isinstance(node.value.value, str)):
            drop(node)             # a docstring, or a bare string used as one
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in ADVICE_ARGS:
                    drop(keyword.value)

    return "\n".join(line for number, line in enumerate(text.splitlines(), 1)
                      if number not in skip)


def _commands_that_can_start_a_machine() -> set[str]:
    """Every command whose body reaches a call that starts or keeps a box up.

    Derived from the source, so a NEW command cannot be added without either
    naming the bill or failing this. That is the whole difference between this
    and a list somebody remembers to update.

    Followed TRANSITIVELY, unlike the alibi below, and the asymmetry is the
    point. Being wrong here is cheap in one direction and expensive in the
    other: an over-large billable set costs a command one line of output it
    probably should have anyway, while a set that is one short is a GPU box
    billing overnight with nothing said about it. So inclusion reaches as far as
    it can and exoneration reaches exactly one hop.

    Following calls also finds a starter the command does not call itself.
    `move_cmd` starts a box inside `_zone_with_capacity`, one level down, which
    no scan of `move_cmd`'s own body can see.

    WHAT THIS CANNOT SEE, stated because a guard that looks complete and is not
    is worse than one that admits its edges — the next person reads the name and
    stops there:

    - A start that does not go through one of the three `Gcloud` methods. A raw
      `subprocess` call, or a new gcloud wrapper, is invisible until its name is
      added to `STARTS_A_BOX` above.
    - A call made dynamically — `getattr(gc, name)()`, a method looked up in a
      dict, a callable passed in as an argument. The walk reads call syntax, not
      behaviour. `down_cmd` passes `put_away` to `_act` as an ARGUMENT, and it
      is only caught because it also calls `put_away` directly elsewhere.
    - A start in any module other than the four `_functions_by_name` reads.
    - Two different functions with the same name in different modules are not
      told apart; both are searched, so this errs toward including.

    `disconnect_cmd` is in BILLABLE_ENDINGS and is NOT derived: it leaves a box
    running without starting one, which is a fourth thing this does not model.
    It is declared by hand, and that is the honest description of it.
    """
    found = set()
    functions = _functions_by_name()
    for name in functions:
        if name.endswith("_cmd") and _reaches_a_start(name, functions):
            found.add(name)
    return found


def _reaches_a_start(name: str, functions: dict[str, list[ast.FunctionDef]],
                     seen: frozenset[str] = frozenset()) -> bool:
    """Whether this function starts a box, or calls something that does."""
    if name in seen:
        return False
    seen = seen | {name}
    for node in functions.get(name, []):
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            called = _called_name(call)
            if called in STARTS_A_BOX:
                return True
            if called == KEEPS_A_BOX_UP and _leaves_it_running(call):
                return True
            if called in functions and _reaches_a_start(called, functions, seen):
                return True
    return False


def test_the_list_of_billable_commands_is_not_missing_one():
    """The guard on the guard. The test below checks a hand-maintained tuple, and
    a hand-maintained tuple that nothing derives is how a new command gets missed
    — which is exactly how `disconnect` was missed until the test below was
    written, and it was written for two other commands."""
    derived = _commands_that_can_start_a_machine()
    unlisted = sorted(derived - set(BILLABLE_ENDINGS))
    assert not unlisted, (
        f"{', '.join(unlisted)} can leave a machine running and is not in "
        "BILLABLE_ENDINGS, so nothing checks that it names the bill."
    )
    # And the derivation still finds something. Its silent death is a starter
    # being RENAMED: the vocabulary would go on naming a call that no longer
    # exists, `derived` would quietly empty, and this test would pass by finding
    # nothing to complain about. A count that drops to zero with no failures is
    # the bug, not the pass.
    assert derived, "the derivation found no commands at all — it has stopped working"


def test_every_starter_is_still_a_real_function():
    """The vocabulary is a set of names matched against source, so nothing
    connects it to the calls it names. Rename `Gcloud.start_instance` and it
    keeps naming the old one: every command drops out of the derived set, and
    the guard above passes on an empty set rather than failing.

    That is the same shape as the two hand-maintained collections this file
    already had to make self-checking, arriving through the one door left open.
    """
    known = _functions_by_name()
    gone = sorted(name for name in INCLUSION_VOCABULARY if name not in known)
    assert not gone, (
        f"{', '.join(gone)} is in the inclusion vocabulary but is no longer a "
        "function in host.py, lifecycle.py, create.py or gcloud.py — it was "
        "renamed or removed, and the derivation has been silently finding fewer "
        "commands ever since."
    )


def test_no_token_that_makes_a_command_billable_can_also_clear_it():
    """The guard on the guard on the guard, and it is two lines because the class
    it closes is mechanical.

    A membership token and an exoneration token being the same string is how a
    command comes to be cleared by the evidence against it. Nothing about
    `_serve` or `put_away` made that likely to be noticed by eye: both readings
    are plausible sentences — "it starts a box" and "it defers to something that
    names the bill" — and they were written months apart.
    """
    assert INCLUSION_VOCABULARY & {token.rstrip("(") for token in BILL_TOKENS} == set()


def test_every_command_that_leaves_a_box_running_names_the_bill():
    """Reads the source rather than driving eleven commands, because the point is
    that a NEW one cannot be added without this. Driving them proves today; this
    proves tomorrow.

    A command clears by naming the bill itself, or by deferring to ONE call whose
    own body names it — `go` and `switch` end in `_serve`, `disconnect` in
    `put_away`. The alibi is the callee's real ending, so gutting that ending
    turns this red, which is the whole point and was not true before: with
    `_serve(` accepted as its own alibi, `_serve`'s ending could be replaced with
    `say.result("")` and both tests still passed.

    One hop, not the transitive closure. Following calls to exhaustion reaches
    `_give_up` and `_with_the_bill` from almost anywhere in these two modules,
    which clears every command and gives back a guard that cannot fail — the
    same disease as accepting the inclusion token, arriving from the other side.
    """
    import inspect

    from comfy_qa import host as host_module

    source = inspect.getsource(host_module)
    commands = {node.name: node for node in ast.walk(ast.parse(source))
                if isinstance(node, ast.FunctionDef)}
    bodies = _bodies_by_name()

    missing = []
    for name in BILLABLE_ENDINGS:
        node = commands.get(name)
        if node is None:
            missing.append(f"{name} no longer exists — update this test")
            continue

        if _names_the_bill(ast.get_source_segment(source, node) or ""):
            continue

        deferred = any(
            _leaves_it_running(call)
            and any(_names_the_bill(body)
                    for body in bodies.get(_called_name(call), []))
            for call in ast.walk(node) if isinstance(call, ast.Call)
        )
        if not deferred:
            missing.append(name)

    assert not missing, (
        f"{', '.join(missing)} can leave a machine running without saying how to "
        "stop paying for it. Every other billable path in this tool says it. "
        "Adding it to BILLABLE_ENDINGS is NOT the fix — that is the list of "
        "commands this rule applies to, not the list of exceptions to it."
    )


# --- a dry run may not delete anything, --clean included ----------------------
#
# `--clean` short-circuits the confirmation, so `move --clean --dry-run` reached
# `remove_leftovers` and ran `disks delete --quiet` and `snapshots delete --quiet`
# for real — then printed "--dry-run: nothing changed" twenty-five lines later.
# The one flag whose entire contract is "show me what would happen" performed the
# only irreversible deletion in the command and then denied it.
#
# This file already holds the same rule for the other half: `move --dry-run` must
# not START a box. That test exists because a dry run once did. This is the same
# promise, broken a different way.


def test_a_dry_run_deletes_nothing_even_with_clean(cli):
    """The preconditions are ordinary: an earlier failed move left a disk or a
    snapshot, which is the exact state `--clean` exists for."""
    class Littered(Cloud):
        def run(self, args, **kwargs):
            joined = " ".join(str(a) for a in args)
            self.calls.append(joined)
            if "disks list" in joined:
                return [{"name": "comfy-win-a-b", "zone": ".../us-central1-b",
                         "sizeGb": "300", "type": ".../pd-balanced", "users": [],
                         "sourceSnapshot": ".../comfy-win-a-move",
                         "creationTimestamp": "2026-09-04T10:00:00.000-07:00"}]
            if "snapshots list" in joined:
                return [{"name": "comfy-win-a-move", "diskSizeGb": "300",
                         "sourceDisk": ".../disks/comfy-win-a",
                         "storageBytes": "1000", "status": "READY",
                         "creationTimestamp": "2026-09-04T09:00:00.000-07:00"}]
            if "machine-types list" in joined:
                return [{"name": "g2-standard-8"}]
            raise AssertionError(f"unexpected: {joined}")

    result = cli("move", "comfy-win", "--to", "us-central1-b", "--clean",
                 "--dry-run", cloud=Littered())

    destructive = [c for c in result.cloud.calls
                   if "delete" in c or "snapshot" in c and "list" not in c]
    assert not [c for c in destructive if "delete" in c], (
        f"a dry run deleted something: {destructive}"
    )
    # NOT `"nothing changed" in output` — the PRE-FIX code printed that too,
    # which was the defect. Only an assertion about what was not deleted does
    # any work here.
    assert "these would be deleted first, and are not" in result.output


def test_a_dry_run_deletes_nothing_without_clean_either(cli, monkeypatch):
    """The other route, which `--clean` never touched.

    The old guard re-derived "is there anything to delete" from `plan` and two of
    `found`'s five fields, and missed `spare_snapshots` — which `remove_leftovers`
    deletes and `leftovers` lists. So `mine` was non-empty from spares alone, the
    guard read False, a DRY RUN asked "Delete these and start the move fresh?",
    and destroyed a real snapshot on "y". No `--clean` required.
    """
    class Spare(Cloud):
        def run(self, args, **kwargs):
            joined = " ".join(str(a) for a in args)
            self.calls.append(joined)
            if "disks list" in joined:
                return []
            if "snapshots list" in joined:
                # Not READY, so `usable` is empty and `snapshot` is None while
                # the family is not — which is exactly the spare-only state the
                # old guard could not see. A snapshot part-way through creation
                # from an interrupted move is the ordinary way to reach it.
                return [{"name": "comfy-win-a-move", "diskSizeGb": "300",
                         "sourceDisk": ".../disks/comfy-win-a",
                         "storageBytes": "1000", "status": "CREATING",
                         "creationTimestamp": "2026-09-04T09:00:00.000-07:00"}]
            if "machine-types list" in joined:
                return [{"name": "g2-standard-8"}]
            raise AssertionError(f"unexpected: {joined}")

    # THE LINE THAT MAKES THIS TEST DO ANY WORK, and without it the whole thing
    # was vacuous. Under `CliRunner` neither stdin nor stderr is a tty, so
    # `can_prompt()` is False, and the destructive `elif` — `clean or (not yes
    # and can_prompt() and typer.confirm(...))` — is unreachable whatever the
    # guard above it says. `assert "Delete these" not in output` was then true
    # because of the terminal, not because of the fix: the old guard passed this
    # test too, which the note that used to sit here recorded as an honest limit
    # rather than closed.
    #
    # `comfy_qa.gcloud.can_prompt`, NOT `host.can_prompt`. `move_cmd` imports the
    # name INSIDE the function, so the module attribute is looked up at call time
    # and patching the host module silently does nothing — the test goes on
    # passing and looks closed.
    from comfy_qa import gcloud as gcloud_module

    monkeypatch.setattr(gcloud_module, "can_prompt", lambda: True)

    result = cli("move", "comfy-win", "--to", "us-central1-b", "--dry-run",
                 cloud=Spare(), input="y\n")

    deleted = [c for c in result.cloud.calls if "delete" in c]
    assert not deleted, f"a dry run deleted something: {deleted}"
    assert "Delete these" not in result.output, (
        "a dry run must not ask a destructive question"
    )


# --- Ctrl-C does not cancel the work, and nothing said so --------------------
#
# Ctrl-C reaches the local gcloud and kills it. It does not reach Compute
# Engine: the request has already gone, and the instance is built server-side
# whether or not the client that asked for it is alive. `Gcloud.run` is
# `subprocess.run`, which re-raises without returning an exit code — measured at
# 0.41s against the real call path — so the tool cannot even learn whether it
# succeeded.
#
# Typer converts the interrupt to `Exit(130)` (typer/core.py:203-204), so the
# command already exited 130, silently. The defect was never the code: it was
# that nobody said a GPU box may now be running and billing.


def test_an_interrupt_while_starting_says_the_box_may_be_billing(
        monkeypatch, tmp_path, run_main):
    """Driven through `cli.main`, because that is where the report is.

    `CliRunner` calls the command with `standalone_mode=False` and stops short of
    `main`, so a test written against it can only see what the command printed on
    its way past — which for an interrupt is nothing. It also cannot see the exit
    code the shell would get, and that code is half the claim: 130 for an
    interrupt, not the 1 Click's `Abort` gave it.
    """
    from comfy_qa import gcloud as gcloud_module

    class Interrupted(Cloud):
        def instance_status(self, instance, zone, project):
            return "TERMINATED"

        def start_instance(self, instance, zone, project):
            raise KeyboardInterrupt

    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: Interrupted())

    code, output = run_main(["up", "comfy-win", "--config", str(path)])

    assert code == 130, output
    # NOT `assert "Aborted!" not in output`. That assertion cannot fail, for two
    # independent reasons, and it sat in the commit whose headline was this
    # test's name. Typer overrides Click's main and converts KeyboardInterrupt to
    # `Exit(130)` before Click's Abort path is reached (typer/core.py:203-204);
    # and when an Abort DOES happen — EOFError at a prompt, which `create` and
    # `move` can reach with stdin closed — typer's rich branch prints `Aborted.`
    # with a full stop, not `Aborted!`. So the string was unreachable twice over.
    # What matters is that the report is there, and that is asserted below.
    assert "may exist and be billing" in output, output
    assert "comfy-qat down comfy-win" in output
    assert "list --live" in output


# `create`'s interrupt is tested in test_create_cli.py, which already has a fake
# that can reach `build` — zone planning needs accelerator-types and
# machine-types answers this file's Cloud deliberately refuses.


# --- 8. a stop that FAILS must say the box is still billing --------------------
#
# The fourth thing a live run's worth of reading found, and the most expensive.
# `put_away`'s `except GcloudError` raised
#
#     LifecycleError(f"could not stop {host.name}: {exc}", fix=exc.fix)
#
# which was the ONLY mutating-call failure path in lifecycle.py that did not
# reach `_with_the_bill` — in the one command whose entire purpose is stopping
# the bill. `exc.fix` is gcloud's own advice and is `None` for the timeout that
# makes this matter, so the whole message was "could not stop comfy-win: ...".
# The box is still on, and the person who typed `down` believes it is not.
#
# `bring_up` has handled the mirror case since it was written, with a comment
# saying why: a request that reached Google and whose ANSWER was lost is not a
# request that did not happen, so it RE-READS the state and names the bill. A
# stop is the same call with the stakes reversed.
#
# `down --all` has aggregated into "may still be billing" for as long as it has
# existed. `down <name>` — the form someone types at the end of a session — had
# no equivalent, which is two forms of one command disagreeing about money.


class StopFails(Cloud):
    """A stop that refuses, and a state read that can answer differently after it.

    `Cloud` returns one fixed status forever, which cannot express the only
    question this section asks: what does the project say AFTER the stop went
    wrong. `after` is that second answer — a state, or an exception to raise.
    """

    def __init__(self, *, after, **kwargs):
        super().__init__(**kwargs)
        self._after = after
        self._read = 0

    def instance_status(self, instance, zone, project):
        self.calls.append("instance_status")
        self._read += 1
        if self._read == 1:
            return self._status
        if isinstance(self._after, Exception):
            raise self._after
        return self._after


def test_a_failed_stop_says_the_box_is_still_running_and_billing(cli):
    """The headline case: gcloud refused, and the project says the box is up.

    Nothing is hedged here, because nothing needs to be — the state was read and
    it came back RUNNING. The old message said neither that it was running nor
    that it was costing anything.
    """
    from comfy_qa.gcloud import GcloudError

    cloud = StopFails(stop=GcloudError("timed out"), after="RUNNING")
    result = cli("down", "comfy-win", cloud=cloud)

    assert result.exit_code == 1, result.output
    assert "billing" in result.output, result.output
    # The raw gcloud, complete and pasteable. `comfy-qat down comfy-win` is the
    # command that has just failed on this box, so it may not be the only one
    # offered — and a stop command with the zone missing is worth nothing.
    assert ("gcloud compute instances stop comfy-win --zone=us-central1-a"
            in result.output), result.output


def test_a_failed_stop_that_cannot_be_re_read_does_not_claim_anything(cli):
    """Both calls failed, so the one honest sentence is that nobody can say.

    Not "it is running" — that asserts what was not read. Not "could not stop",
    full stop, which is what it used to say and reads as though the box is off.
    """
    from comfy_qa.gcloud import GcloudError

    cloud = StopFails(stop=GcloudError("timed out"),
                      after=GcloudError("still timing out"))
    result = cli("down", "comfy-win", cloud=cloud)

    assert result.exit_code == 1, result.output
    assert "may still be running and billing" in result.output, result.output
    assert ("gcloud compute instances stop comfy-win --zone=us-central1-a"
            in result.output), result.output


def test_a_stop_whose_reply_was_lost_is_not_reported_as_a_failure(cli):
    """The re-read proves TERMINATED, so the bill HAS stopped and saying
    otherwise would feed `down --all`'s money summary the opposite of what the
    project just said. Proven, not assumed: TERMINATED is Google's own word and
    the read succeeded."""
    from comfy_qa.gcloud import GcloudError

    cloud = StopFails(stop=GcloudError("connection reset"), after="TERMINATED")
    result = cli("down", "comfy-win", cloud=cloud)

    assert result.exit_code == 0, result.output
    assert "was billing" in result.output, result.output


# --- 9. every mutating call's FAILURE handler names the bill -------------------
#
# The gap that let section 8 sit green for as long as it did, and it is a gap in
# this file rather than in the tool. `test_every_command_that_leaves_a_box_running
# _names_the_bill` checks the ORDINARY path by design, and delegates failures to
# `_with_the_bill` — lifecycle.py's docstring states the rule: "Every failure
# after the machine has been started says how to stop paying for it."
#
# NOTHING CHECKED THAT `_with_the_bill` WAS ACTUALLY REACHED. So a handler could
# skip it, and one did, for the most expensive call in the tool.
#
# Scoped to the failure handler that DIRECTLY wraps a mutating gcloud call, not
# to every raise in the module. That is the line where the resource either
# exists or is still running because of what just failed, and it is derivable
# rather than a list somebody keeps.


def _mutating_gcloud_methods() -> set[str]:
    """`Gcloud` methods that create, start, stop or destroy something, derived
    from the argv they send rather than from their names.

    Names lie in both directions here and both were measured: `snapshot_disk`
    mutates and has no mutating verb in its name, while `windows_password`
    contains `reset` and only reads. So the verb is read out of the gcloud
    command actually being sent, which is the only place it is a fact.
    """
    import inspect

    from comfy_qa import gcloud as gcloud_module

    verbs = {"create", "delete", "start", "stop", "resize", "snapshot",
             "add", "attach-disk", "detach-disk", "move"}
    source = inspect.getsource(gcloud_module)
    cls = next(node for node in ast.walk(ast.parse(source))
               if isinstance(node, ast.ClassDef) and node.name == "Gcloud")

    found = set()
    for function in cls.body:
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if not isinstance(node, ast.List):
                continue
            words = [element.value for element in node.elts
                     if isinstance(element, ast.Constant)
                     and isinstance(element.value, str)]
            if words[:1] == ["compute"] and any(w in verbs for w in words[:4]):
                found.add(function.name)
    return found


def test_the_mutating_method_derivation_still_finds_them():
    """The derivation above is load-bearing: if it returns nothing, the guard
    below passes by checking nothing at all. That is how a green suite comes to
    mean less than it looks like it means, and it is the failure mode every
    derived check in this file is written against."""
    found = _mutating_gcloud_methods()

    assert {"start_instance", "stop_instance", "create_instance_from_image"} <= found, (
        f"the mutating-method derivation has stopped working: {sorted(found)}"
    )


def _failure_bodies(modules) -> dict[str, list[str]]:
    """Every function in `modules`, as source text, for the one-hop alibi.

    Its own index rather than `_bodies_by_name`, because that one reads gcloud.py
    and not relocate.py, and this rule needs the opposite: `Gcloud`'s own methods
    are what make a handler GUILTY here, so letting one of them clear a handler
    would be the evidence-as-alibi defect `test_no_token_that_makes_a_command
    _billable_can_also_clear_it` exists to stop, arriving through the back door.
    """
    import inspect

    found: dict[str, list[str]] = {}
    for module in modules:
        source = inspect.getsource(module)
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.FunctionDef):
                found.setdefault(node.name, []).append(
                    ast.get_source_segment(source, node) or "")
    return found


def test_every_mutating_call_failure_handler_names_the_bill():
    """A gcloud call that starts, stops or creates something, and whose
    `except GcloudError` says nothing about the bill.

    The one this was written for: `put_away` wrapping `gc.stop_instance`. It
    raised "could not stop <name>" with gcloud's own `fix`, which is `None` on a
    timeout — leaving a GPU box running and the message silent about it, in the
    command people run specifically to stop paying.
    """
    import inspect

    from comfy_qa import create as create_module
    from comfy_qa import host as host_module
    from comfy_qa import lifecycle as lifecycle_module
    from comfy_qa import relocate as relocate_module

    mutating = _mutating_gcloud_methods()
    modules = (host_module, lifecycle_module, create_module, relocate_module)
    bodies = _failure_bodies(modules)

    def names_the_bill(text: str) -> bool:
        return any(token in text for token in FAILURE_BILL_TOKENS)

    naked = []
    for module in modules:
        source = inspect.getsource(module)
        name = module.__name__.split(".")[-1] + ".py"
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Try):
                continue
            called = {_called_name(call) for call in ast.walk(node)
                      if isinstance(call, ast.Call)} & mutating
            if not called:
                continue
            for handler in node.handlers:
                types = ast.unparse(handler.type) if handler.type else ""
                if "GcloudError" not in types:
                    continue
                body = "\n".join(ast.get_source_segment(source, stmt) or ""
                                 for stmt in handler.body)
                if names_the_bill(body):
                    continue
                # ONE HOP, exactly as the ordinary-path guard allows, and for the
                # same reason its docstring gives: a handler is entitled to defer
                # to a function whose whole job is that sentence. Both real
                # deferrals in this package are of that shape — `host` hands the
                # probe failure to `_probe_failed`, `relocate` hands a stopped
                # move to `_stopped` — and neither can be seen by reading the
                # handler alone.
                #
                # Not transitive. Following calls to exhaustion reaches
                # `output.fix` from almost anywhere in these four modules and
                # clears everything, which is the guard defeating itself from the
                # exoneration side — the disease this file already names.
                deferred = any(
                    names_the_bill(text)
                    for call in ast.walk(handler)
                    if isinstance(call, ast.Call)
                    for text in bodies.get(_called_name(call), []))
                if not deferred:
                    naked.append(f"{name}:{handler.lineno} (around "
                                 f"{', '.join(sorted(called))})")

    assert not naked, (
        f"a failure handler for a mutating gcloud call says nothing about the "
        f"bill: {'; '.join(naked)}. The call either started something that is "
        f"now running or failed to stop something that still is, and the "
        f"message is the only thing standing between that and an overnight "
        f"bill. Route the fix through `_with_the_bill`."
    )
