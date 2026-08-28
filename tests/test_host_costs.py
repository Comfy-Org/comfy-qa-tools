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

    def __init__(self, *, start=None, describe=None, stop=None):
        self.calls: list[str] = []
        self._start = start
        self._describe = describe
        self._stop = stop

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

    def invoke(*args, cloud=None):
        cloud = cloud if cloud is not None else Cloud()
        path = tmp_path / "hosts.toml"
        path.write_text(HOSTS, encoding="utf-8")
        monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: cloud)
        result = CliRunner().invoke(app, [*args, "--config", str(path)])
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
                       "host list")))

    result = cli("stamp", "comfy-win")

    assert result.exit_code == 1
    assert "host open comfy-win" in result.output
    assert "host go comfy-win" in result.output
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
                       "host list")))

    result = cli("stamp", "local")

    assert result.exit_code == 1
    assert "start ComfyUI on that machine" in result.output
    assert "host open" not in result.output


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
    assert "host open comfy-win" not in result.output


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
        fix="comfy-qat auth login")))

    assert result.exit_code == 2
    assert "your Google session has expired" in result.output
    assert "to fix: comfy-qat auth login" in result.output


def test_no_zone_suggested_is_also_a_refusal(cli):
    """It stopped before doing anything, so it exits the same way."""
    result = cli("move", "comfy-win", cloud=Cloud(start=GcloudError(
        "could not start comfy-win",
        raw="ERROR: does not have enough resources available to fulfill the request")))

    assert result.exit_code == 2
    assert "Pick one with --to" in result.output


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
    assert "stopped" in result.output


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
