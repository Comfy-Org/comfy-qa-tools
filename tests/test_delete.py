"""`delete` — the only thing this tool does that cannot be undone.

Everything else is reversible: a box stops and starts, a move leaves the original
where it was, a bad host list has a backup beside it. This destroys an install and
its disk, so what matters here is not the happy path — it is the refusals.

They shipped as prose in a docstring and nothing asserted any of them. That is the
gap `test_suite_integrity` cannot see, because it checks that known test modules
still exist and cannot know about a source module that never had one.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from comfy_qa.cli import app
from comfy_qa.gcloud import GcloudError

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

[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-c"
gce_project  = "proj"
port         = 8192
"""


class Cloud:
    """Records every call. Anything not expected is the failure being tested."""

    def __init__(self, status="TERMINATED", fail=None):
        self.calls: list[str] = []
        self._status = status
        self._fail = fail

    def instance_status(self, name, zone, project):
        self.calls.append(f"status {name}")
        if isinstance(self._status, Exception):
            raise self._status
        return self._status

    def run(self, args, **kwargs):
        joined = " ".join(str(a) for a in args)
        self.calls.append(joined)
        if self._fail is not None:
            raise self._fail
        return ""

    def __getattr__(self, name):
        def unexpected(*a, **k):
            raise AssertionError(f"{name} was not expected")
        return unexpected

    def deleted(self):
        return [c for c in self.calls if c.startswith("compute instances delete")]


@pytest.fixture
def cli(tmp_path, monkeypatch):
    from comfy_qa import gcloud as gcloud_module

    def invoke(*args, cloud=None, input=None, tty=True):
        cloud = cloud if cloud is not None else Cloud()
        path = tmp_path / "hosts.toml"
        path.write_text(HOSTS, encoding="utf-8")
        monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: cloud)
        monkeypatch.setattr(gcloud_module, "can_prompt", lambda: tty)
        result = CliRunner().invoke(app, [*args, "--config", str(path)], input=input)
        result.cloud = cloud            # type: ignore[attr-defined]
        return result

    return invoke


# --- the refusals, which are the product ----------------------------------

@pytest.mark.parametrize("state", ["RUNNING", "STAGING", "PROVISIONING",
                                   "REPAIRING", "SUSPENDED"])
def test_only_a_terminated_box_may_be_deleted(cli, state):
    """The promise is that what you destroy is something you just looked at. A
    box thirty seconds into booting is in STAGING, and nobody has looked at it."""
    result = cli("delete", "comfy-linux", cloud=Cloud(status=state),
                 input="comfy-linux\n")

    assert result.exit_code == 2
    assert not result.cloud.deleted(), f"a box in {state} was destroyed"


def test_a_running_box_is_refused_and_told_to_stop_first(cli):
    """Not because GCE minds — it will delete a running instance happily — but so
    that what you are destroying is something you looked at seconds ago."""
    result = cli("delete", "comfy-linux", cloud=Cloud(status="RUNNING"),
                 input="comfy-linux\n")

    assert result.exit_code == 2, "exit 2 means nothing was changed"
    assert not result.cloud.deleted()
    assert "comfy-qat down comfy-linux" in result.output


def test_a_description_is_refused_because_it_could_mean_another_box(cli):
    """`--os windows` is a fine way to say "the machine I want to work on" and a
    terrible way to say "the machine to destroy"."""
    result = cli("delete", "windows", input="windows\n")

    assert result.exit_code == 2
    assert not result.cloud.deleted()


def test_the_local_machine_cannot_be_deleted(cli):
    result = cli("delete", "local", input="local\n")
    assert result.exit_code == 2
    assert not result.cloud.deleted()


def test_no_name_is_refused_rather_than_guessed(cli):
    result = cli("delete")
    assert result.exit_code == 2
    assert not result.cloud.deleted()


def test_an_unknown_name_is_refused(cli):
    result = cli("delete", "nosuchbox", input="nosuchbox\n")
    assert result.exit_code == 2
    assert not result.cloud.deleted()


# --- the confirmation ------------------------------------------------------

def test_confirmation_is_the_name_not_a_yes(cli):
    """A [y/N] is answered by reflex at 2am. A name is not."""
    result = cli("delete", "comfy-linux", input="y\n")

    assert not result.cloud.deleted(), "'y' must not be enough"
    assert result.exit_code == 2, "nothing was changed, so 2 — not 1"
    assert "nothing was deleted" in result.output


def test_a_mistyped_name_deletes_nothing(cli):
    result = cli("delete", "comfy-linux", input="comfy-linx\n")
    assert not result.cloud.deleted()
    assert result.exit_code == 2


def test_the_right_name_goes_through(cli):
    result = cli("delete", "comfy-linux", input="comfy-linux\n")

    deleted = result.cloud.deleted()
    assert len(deleted) == 1, deleted
    assert "comfy-linux" in deleted[0]
    assert "--zone=us-central1-c" in deleted[0]
    assert result.exit_code == 0


def test_the_disk_goes_with_it(cli):
    """Boot disks here are created auto-delete=no, so deleting the instance alone
    leaves 200-300 GB billing with nothing attached — which looks like nothing at
    all in a console, and is the leftover people actually get caught by."""
    result = cli("delete", "comfy-linux", input="comfy-linux\n")
    assert "--delete-disks=all" in result.cloud.deleted()[0]


def test_yes_skips_the_prompt_for_someone_who_has_already_decided(cli):
    result = cli("delete", "comfy-linux", "--yes")
    assert len(result.cloud.deleted()) == 1
    assert result.exit_code == 0


def test_without_a_terminal_it_refuses_rather_than_blocking(cli):
    """A pipe or a script must not stop on a question nobody will see — and must
    not silently take the destructive branch either."""
    result = cli("delete", "comfy-linux", tty=False)

    assert not result.cloud.deleted()
    assert result.exit_code == 2
    assert "--yes" in result.output


# --- failures -------------------------------------------------------------

def test_a_status_that_cannot_be_read_stops_before_deleting(cli):
    """Not knowing whether it is running is not permission to destroy it."""
    result = cli("delete", "comfy-linux",
                 cloud=Cloud(status=GcloudError("credentials expired")),
                 input="comfy-linux\n")

    assert not result.cloud.deleted()
    assert result.exit_code == 2


def test_a_refused_delete_is_reported_as_work_that_failed(cli):
    """Exit 1: the work started and did not finish. Not 2, which means nothing
    was attempted."""
    result = cli("delete", "comfy-linux",
                 cloud=Cloud(fail=GcloudError("the instance is protected")),
                 input="comfy-linux\n")

    assert result.exit_code == 1


def test_a_deleted_box_leaves_the_host_list(cli, tmp_path):
    """The first version ended by inviting the user to remove the entry, or to
    "leave it as a note of what was there". That advice manufactures a later
    failure: `create` refuses a name a host list entry holds, and ports come from
    the same list — so the note reserves both for a machine that does not exist,
    and the refusal arrives weeks later with nothing to connect it to tonight."""
    import tomllib

    result = cli("delete", "comfy-linux", input="comfy-linux\n")
    assert result.exit_code == 0

    path = tmp_path / "hosts.toml"
    hosts = tomllib.loads(path.read_text(encoding="utf-8"))["hosts"]
    assert "comfy-linux" not in hosts
    assert {"local", "comfy-win"} <= set(hosts), "it took something else with it"
    assert hosts["comfy-win"]["port"] == 8190, "an unrelated host was rewritten"


def test_a_host_list_that_cannot_be_written_says_what_it_will_cost(cli, tmp_path):
    """The machine is already gone by then, so this is a warning, not a failure
    to act on — but silence would leave a name and a port reserved."""

    def readonly(*a, **k):
        raise OSError("read-only file system")

    import comfy_qa.hostfile as hostfile_module
    original = hostfile_module.apply
    hostfile_module.apply = readonly
    try:
        result = cli("delete", "comfy-linux", input="comfy-linux\n")
    finally:
        hostfile_module.apply = original

    assert result.cloud.deleted(), "the box was still deleted"
    assert result.exit_code == 1
    assert "still in your host list" in result.output


def test_a_state_that_read_back_empty_is_words_not_a_gap(cli):
    """`instance_status` returns "" when the describe succeeded and said nothing.
    Interpolated raw that read "qa-linux is , not stopped". The refusal was right;
    the sentence was not."""
    result = cli("delete", "comfy-linux", cloud=Cloud(status=""),
                 input="comfy-linux\n")

    assert result.exit_code == 2
    assert not result.cloud.deleted()
    assert "is , not stopped" not in result.output, result.output
    assert "unknown state" in result.output
