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
    """
    result = cli("move", "comfy-win", cloud=Cloud(start=GcloudError(
        "your Google session has expired", raw="ERROR: reauthentication required",
        fix="comfy-qat login")))

    assert result.exit_code == 2
    assert "your Google session has expired" in result.output
    assert "to fix: comfy-qat login" in result.output


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
# `down --all --keep-running` deliberately leaves every machine on, and then the
# closing line said "all N stopped." Each host had already printed "still
# billing" immediately above it. The one command whose purpose is answering "am I
# still paying" answered it wrongly, in the direction that costs.


def test_keeping_them_running_is_not_reported_as_stopping_them(cli):
    class Kept(Cloud):
        def instance_status(self, name, zone, project):
            self.calls.append("instance_status")
            return "RUNNING"

    result = cli("down", "--all", "--keep-running", cloud=Kept())

    assert "stopped." not in result.output, (
        "--keep-running stops nothing, so no closing line may say it did"
    )
    # "still billing" comes from lifecycle's PER-HOST line, not from this
    # command's summary — so a test named for `down --all --keep-running` was
    # holding nothing that command writes, and its own summary was free to be
    # wrong while this stayed green. Assert the summary too, by name.
    assert "still billing" in result.output
    assert "left running and billing: comfy-win" in result.output, (
        "the command's own summary is unasserted; it names WHICH boxes are billing"
    )
    assert result.exit_code == 0


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
    """The capability `--keep-running` existed for, under a name that says it."""
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


def test_the_old_flag_still_works_and_says_where_it_went(cli):
    """A rename, not a removal — nothing written down before today may break."""
    class Live(Cloud):
        def instance_status(self, instance, zone, project):
            return "RUNNING"

    result = cli("down", "comfy-win", "--keep-running", cloud=Live())

    assert result.exit_code == 0
    assert "comfy-qat disconnect" in result.output
    assert "still works" in result.output
    assert "stop_instance" not in result.cloud.calls


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
# with a box running and billing, and single-host `down --keep-running` said
# "still billing" and then offered `logs`.
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
# `down_cmd` is here because of `--keep-running`, which deliberately leaves a box
# on. The derivation below found it; it was not typed.
BILLABLE_ENDINGS = ("up_cmd", "go_cmd", "switch_cmd", "create_cmd",
                    "disconnect_cmd", "down_cmd")


def _commands_that_can_start_a_machine() -> set[str]:
    """Every command whose body reaches a call that starts or keeps a box up.

    Derived from the source, so a NEW command cannot be added without either
    naming the bill or failing this. That is the whole difference between this
    and a list somebody remembers to update.
    """
    import ast
    import inspect

    from comfy_qa import host as host_module

    starters = {"bring_up", "_bring_up", "put_away", "_serve", "build"}
    found = set()
    for node in ast.walk(ast.parse(inspect.getsource(host_module))):
        if not isinstance(node, ast.FunctionDef) or not node.name.endswith("_cmd"):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                func = inner.func
                called = (func.id if isinstance(func, ast.Name)
                          else func.attr if isinstance(func, ast.Attribute) else "")
                # put_away only leaves a box running under keep_running=True;
                # otherwise it is the command that stops one.
                if called == "put_away" and not any(
                    kw.arg == "keep_running" for kw in inner.keywords
                ):
                    continue
                if called in starters:
                    found.add(node.name)
    return found


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


def test_every_command_that_leaves_a_box_running_names_the_bill():
    """Reads the source rather than driving eleven commands, because the point is
    that a NEW one cannot be added without this. Driving them proves today; this
    proves tomorrow."""
    import ast
    import inspect

    from comfy_qa import host as host_module

    source = inspect.getsource(host_module)
    tree = ast.parse(source)
    functions = {node.name: node for node in ast.walk(tree)
                 if isinstance(node, ast.FunctionDef)}

    missing = []
    for name in BILLABLE_ENDINGS:
        node = functions.get(name)
        if node is None:
            missing.append(f"{name} no longer exists — update this test")
            continue
        body = ast.get_source_segment(source, node) or ""
        # Either it names the command itself, or it defers to something that
        # does: lifecycle's stop_paying / _with_the_bill, or _serve's ending.
        # Either it names the command itself, or it defers to something that
        # does. `put_away` names it on the keep_running branch, which is the only
        # branch of `down` that leaves a box up.
        says_it = ("comfy-qat down" in body or "stop_paying" in body
                   or "_with_the_bill" in body or "_serve(" in body
                   or "put_away" in body)
        if not says_it:
            missing.append(name)

    assert not missing, (
        f"{', '.join(missing)} can leave a machine running without saying how to "
        "stop paying for it. Every other billable path in this tool says it."
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


# --- Ctrl-C does not cancel the work, and "Aborted!" says it did --------------
#
# `Gcloud.run` catches KeyboardInterrupt and calls `process.wait()` a SECOND
# time, so a start or a create already under way COMPLETES and only then unwinds.
# Measured against a replica of that pattern: interrupt at 0.4s, return at 2.02s,
# resource created.
#
# KeyboardInterrupt is a BaseException, so it walks past `_reportable()` and every
# handler in host.py, and Click prints "Aborted!" — a word that means nothing
# happened, over a GPU box that is running and billing.


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
    from comfy_qa.cli import INTERRUPTED

    class Interrupted(Cloud):
        def instance_status(self, instance, zone, project):
            return "TERMINATED"

        def start_instance(self, instance, zone, project):
            raise KeyboardInterrupt

    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: Interrupted())

    code, output = run_main(["up", "comfy-win", "--config", str(path)])

    assert code == INTERRUPTED, output
    assert "Aborted!" not in output
    assert "may exist and be billing" in output, output
    assert "comfy-qat down comfy-win" in output
    assert "list --live" in output


# `create`'s interrupt is tested in test_create_cli.py, which already has a fake
# that can reach `build` — zone planning needs accelerator-types and
# machine-types answers this file's Cloud deliberately refuses.
