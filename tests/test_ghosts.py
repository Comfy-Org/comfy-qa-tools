"""Three commands disagreeing about a machine that does not exist.

All three were found on the real fleet within minutes of each other, and they are
one defect wearing three coats: `list --live` asked Google, and `switch`, `down`
and `delete` each answered from something else.

**`switch` counted three deleted boxes as running.** The 1-GPU ceiling read as
full, so it chose the order `_blocked_by_the_ceiling` itself calls the
destructive one, every time, and could not start anything. At that same second
`list --live` printed `not on the project` about all three — same tool, same
call, opposite answers.

**`down` said a 404 "may still be running and billing".** A resource Google says
is not there cannot bill. That over-reports, which costs nothing at the bank and
everything in trust: a tool that cries "may still be billing" about a machine
that provably does not exist is a tool people stop reading, and the true warning
goes unread with it.

**`delete` refused a box it could not read**, exiting 2 with Google's raw 404 and
never reaching the host list — while `discover` only ever added. Between them the
entry was unremovable by any command in the tool, and hand-editing `hosts.toml`
was the only way out: the file with no other copy, that `init --force` overwrote
outside the guarded path until tonight.

**The asymmetry is the whole of it, and it is the second half of every test
here.** A listing that SUCCEEDED and does not contain the instance is Google
saying the box is gone. A listing that FAILED is nobody having asked. Only the
first may stop `switch` counting it, stop `down` warning about it, or let
`delete` remove its entry — and `is_gone` answers False on any doubt for exactly
that reason.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from comfy_qa import gcloud as gcloud_module
from comfy_qa.cli import app
from comfy_qa.config import Host
from comfy_qa.gcloud import GONE, GcloudError
from comfy_qa.lifecycle import is_gone, running_elsewhere

WIN = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022",
           gpu="L4", gce_instance="comfy-win", gce_zone="us-central1-a",
           gce_project="proj")
GHOST = Host(name="comfy-win-b", kind="gce", port=8192, os="Windows Server 2022",
             gpu="L4", gce_instance="comfy-win-b", gce_zone="us-central1-b",
             gce_project="proj")

KEY = (GHOST.gce_instance, GHOST.gce_zone, GHOST.gce_project)


class Project:
    """A gcloud whose project listing can answer, or refuse to."""

    def __init__(self, *, holds=(), listing_fails=False, status=None):
        self.holds = set(holds)
        self.listing_fails = listing_fails
        self.status = status
        self.calls: list[str] = []

    def instance_statuses(self, wanted):
        self.calls.append("statuses")
        if self.listing_fails:
            raise GcloudError("could not reach Google Cloud")
        return {key: ("RUNNING" if key[0] in self.holds else GONE) for key in wanted}

    def instance_status(self, name, zone, project):
        self.calls.append("status")
        if isinstance(self.status, Exception):
            raise self.status
        return self.status

    def __getattr__(self, name):
        def unexpected(*a, **k):
            raise AssertionError(f"{name} was not expected here")
        return unexpected


# --- the shared answer --------------------------------------------------------


def test_is_gone_is_true_only_when_the_project_answered_and_said_no():
    assert is_gone(Project(holds=["comfy-win"]), GHOST) is True


def test_is_gone_is_false_when_nobody_could_ask():
    """The half that matters. Every caller uses this to stop worrying about a
    bill or to remove a host list entry; refuting is not confirming."""
    assert is_gone(Project(listing_fails=True), GHOST) is False


def test_is_gone_is_false_when_the_box_is_there():
    assert is_gone(Project(holds=["comfy-win", "comfy-win-b"]), GHOST) is False


# --- 1. switch ----------------------------------------------------------------


def test_switch_does_not_count_a_box_the_project_does_not_have(tmp_path):
    """The defect, as one line: three ghosts held the 1-GPU ceiling."""
    found = running_elsewhere(Project(holds=["comfy-win"]), [WIN, GHOST], WIN,
                              tunnel_dir=tmp_path)

    assert found == [], (
        "a machine the project does not have was counted as running, which is "
        "what made every switch pick the destructive order"
    )


def test_switch_still_counts_a_box_it_could_not_read(tmp_path):
    """Not the same judgement. A box nobody could read might be running, and
    being wrong there costs a redundant stop; being wrong the other way leaves a
    GPU billing against the ceiling this command exists to respect."""
    unreadable = Project(holds=["comfy-win"])
    unreadable.instance_statuses = lambda wanted: {}

    found = running_elsewhere(unreadable, [WIN, GHOST], WIN, tunnel_dir=tmp_path)

    assert [host.name for host, _why in found] == ["comfy-win-b"]


# --- 2. down ------------------------------------------------------------------

HOSTS = """\
[hosts.local]
kind = "local"
port = 8188

[hosts.comfy-win-b]
kind         = "gce"
os           = "Windows Server 2022"
gpu          = "L4"
gce_instance = "comfy-win-b"
gce_zone     = "us-central1-b"
gce_project  = "proj"
port         = 8192
"""


@pytest.fixture
def cli(tmp_path, monkeypatch):
    from comfy_qa import tunnel as tunnel_module

    monkeypatch.setattr(tunnel_module, "TUNNEL_DIR", tmp_path / "tunnels")

    def invoke(*args, cloud, input=None):
        path = tmp_path / "hosts.toml"
        path.write_text(HOSTS, encoding="utf-8")
        monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: cloud)
        result = CliRunner().invoke(
            app, [*args, "--config", str(path)], input=input)
        result.hosts = path.read_text(encoding="utf-8")  # type: ignore[attr-defined]
        return result
    return invoke


class Stopping(Project):
    """A stop that 404s, over a project that can be asked — or cannot."""

    def stop_instance(self, name, zone, project):
        self.calls.append("stop")
        raise GcloudError(
            "HTTPError 404: The resource 'projects/proj/zones/us-central1-b/"
            "instances/comfy-win-b' was not found")


def test_a_stop_that_404s_on_a_missing_box_does_not_warn_about_a_bill(cli):
    cloud = Stopping(holds=[], status=GcloudError("not found"))
    result = cli("down", "comfy-win-b", cloud=cloud)

    assert "may still be running and billing" not in result.output, result.output
    assert "no longer exists" in result.output
    assert "nothing is billing" in result.output


def test_a_stop_that_fails_where_nobody_can_ask_still_says_it_cannot_say(cli):
    """The over-report is only wrong when it is provably wrong."""
    cloud = Stopping(listing_fails=True, status=GcloudError("timed out"))
    result = cli("down", "comfy-win-b", cloud=cloud)

    assert "may still be running and billing" in result.output, result.output


# --- 3. delete ----------------------------------------------------------------


class Deleting(Project):
    def run(self, args, **kwargs):
        self.calls.append(" ".join(str(part) for part in args))
        return ""


def test_delete_takes_the_entry_out_when_the_box_is_already_gone(cli):
    """The trap this closes: nothing in the tool could remove the entry."""
    cloud = Deleting(holds=[], status=GcloudError(
        "Could not fetch resource: The resource "
        "'projects/proj/zones/us-central1-b/instances/comfy-win-b' was not found"))
    result = cli("delete", "comfy-win-b", "--yes", cloud=cloud)

    assert result.exit_code == 0, result.output
    assert "already been deleted" in result.output
    assert "comfy-win-b" not in result.hosts, "the entry survived"
    assert not any("instances delete" in call for call in cloud.calls), (
        "it called delete on a machine that is not there"
    )


def test_delete_still_refuses_a_box_it_merely_could_not_read(cli):
    """Not knowing whether it is running is not permission to destroy it, and
    it is not permission to drop the record either."""
    cloud = Deleting(listing_fails=True, status=GcloudError("credentials expired"))
    result = cli("delete", "comfy-win-b", "--yes", cloud=cloud)

    assert result.exit_code == 2
    assert "comfy-win-b" in result.hosts, "an entry went on a read nobody could make"
