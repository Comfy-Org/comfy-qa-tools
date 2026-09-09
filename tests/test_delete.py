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

    def __init__(self, status="TERMINATED", fail=None, disk_gb="200", listing=None):
        self.calls: list[str] = []
        self._status = status
        self._fail = fail
        self._disk_gb = disk_gb
        self._listing = listing

    def instance_status(self, name, zone, project):
        self.calls.append(f"status {name}")
        if isinstance(self._status, Exception):
            raise self._status
        return self._status

    def instance_statuses(self, wanted):
        """The question `delete` asks once its own read has failed: is it there?

        By default it fails the same way `instance_status` did, because a run
        where the per-box read is refused is a run where the listing is refused
        too — expired credentials do not expire for one call. `listing=` is how a
        test says the project answered and did not have the machine.
        """
        self.calls.append("statuses")
        if self._listing is not None:
            return self._listing
        if isinstance(self._status, Exception):
            raise self._status
        return {key: self._status for key in wanted}

    def describe_instance(self, name, zone, project):
        """Read for one thing: the boot disk's name, so the confirmation can
        say how big it is. `<name>-a`, the way a real one is named."""
        self.calls.append(f"describe {name}")
        return {"name": name, "status": self._status,
                "disks": [{"boot": True, "source": f"https://x/disks/{name}-a"}]}

    def run(self, args, **kwargs):
        joined = " ".join(str(a) for a in args)
        if joined.startswith("compute disks describe"):
            self.calls.append(joined)
            if self._disk_gb is None:
                from comfy_qa.gcloud import GcloudError

                raise GcloudError("the disk could not be read")
            return {"sizeGb": self._disk_gb}
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


def test_a_prefix_of_a_real_name_is_not_that_name(cli):
    """`delete win` must not reach `comfy-win`. The exact-match lookup is the
    only thing standing between a half-typed name and a destroyed box, and a
    substring match would take the FIRST host it happened to hit."""
    result = cli("delete", "comfy", input="comfy\n")

    assert result.exit_code == 2
    assert not result.cloud.deleted()
    assert "exact name" in result.output or "Did you mean" in result.output


@pytest.mark.parametrize("typed", ["COMFY-LINUX", "Comfy-Linux", "comfy-LINUX"])
def test_the_confirmation_is_case_sensitive(cli, typed):
    """The type-the-name gate is the last thing between a typo and a deleted
    disk. Accepting a different casing accepts something the user did not type,
    which is the one thing this prompt exists to prevent."""
    result = cli("delete", "comfy-linux", input=f"{typed}\n")

    assert result.exit_code == 2
    assert not result.cloud.deleted()
    assert "nothing was deleted" in result.output


def test_the_exact_name_typed_back_does_delete(cli):
    """The sibling of the two above: if they passed by refusing everything,
    they would be worthless."""
    result = cli("delete", "comfy-linux", input="comfy-linux\n")

    assert result.exit_code == 0
    assert result.cloud.deleted()


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


# --- interrupting the one thing that cannot be undone ----------------------


def test_an_interrupted_delete_says_the_record_may_now_be_wrong(capsys):
    """`remove.py` did not import `inflight` at all: exit was 130 and the report
    had nothing to report.

    This is the only one of these gaps that is not about a resource. The request
    carries `--delete-disks=all`, so by the time it can be interrupted the box
    and its disk are being destroyed server-side — and the host list still says
    the machine exists, because the code that takes the entry out is the code
    that did not run. `create` refuses a name a host list entry holds and ports
    come from the same list, so that entry reserves both for a machine that may
    no longer exist, and the refusal arrives weeks later with nothing to connect
    it to tonight.

    Asserted on the report's TAIL and on recovery words, not on the box name:
    the name is printed by the confirmation lines BEFORE the interrupt, so
    asserting it passes against a tool that reports nothing at all. That is
    triage's finding about its own first version of this test, and it is the
    only thing separating this from a test that cannot fail.
    """
    from comfy_qa import inflight

    class _Interrupted(Cloud):
        """Ctrl-C on the DELETE, not on the reads before it.

        The confirmation reads the boot disk's size first, and those reads are
        deliberately outside the registration — nothing has been mutated yet, so
        an interrupt there has nothing to report. Interrupting them instead
        would test the wrong call.
        """

        def run(self, args, **kwargs):
            joined = " ".join(str(a) for a in args)
            if not joined.startswith("compute instances delete"):
                return super().run(args, **kwargs)
            self.calls.append(joined)
            raise KeyboardInterrupt

    with pytest.raises(inflight.Interrupted):
        _delete_with(_Interrupted())

    printed = capsys.readouterr()
    tail = (printed.out + printed.err).split("deleting comfy-win")[-1]

    # Its own sentence. Neither outcome here is spending — a deleted box bills
    # nothing, and this refuses to run unless the box is already stopped — so
    # all three of the other headings are about the wrong thing.
    assert "this was probably destroyed, and the host list still names it:" in tail
    assert "gcloud compute instances describe comfy-win" in tail
    assert "[hosts.comfy-win]" in tail
    # The recovery it names has to be one that works. It used to say running the
    # command again "will not do it", which was true and was the trap: nothing
    # else could remove the entry either. `delete` now asks the project when its
    # own read fails, so running it again is the answer.
    assert "run it again" in tail


def test_the_ticker_is_stopped_before_the_interrupt_report_prints(monkeypatch):
    """The same defect `rdp` had, in the same shape, found by looking for it.

    `may_leave` prints the leftovers report from its OWN handler and raises
    `Interrupted` afterwards, so an `except inflight.Interrupted` sitting outside
    the registration closes the step AFTER the report rather than before it. Both
    sites carried a comment claiming the opposite; both were wrong.

    `removing` is a background `Slow`, so its thread writes `still going, …` down
    the same stream every second. Here it can land on the heading that says the
    box was probably destroyed while the host list still names it — the lines
    that tell somebody their `hosts.toml` now reserves a name and a port for a
    machine that is gone.

    Order, not final state: the equivalent in tests/test_shell_access.py explains
    why `_stop.is_set()` after the command cannot tell the two apart.
    """
    from comfy_qa import inflight
    from comfy_qa import say as say_module

    events: list[str] = []
    real_slow = say_module.slow
    real_report = inflight.report

    def watched(*args, **kwargs):
        step = real_slow(*args, **kwargs)
        closing = step.give_up

        def give_up():
            events.append("give_up")
            closing()

        step.give_up = give_up
        return step

    def reporting():
        events.append("report")
        return real_report()

    monkeypatch.setattr(say_module, "slow", watched)
    monkeypatch.setattr(inflight, "report", reporting)

    class _Interrupted(Cloud):
        def run(self, args, **kwargs):
            joined = " ".join(str(a) for a in args)
            if not joined.startswith("compute instances delete"):
                return super().run(args, **kwargs)
            raise KeyboardInterrupt

    with pytest.raises(inflight.Interrupted):
        _delete_with(_Interrupted())

    assert "report" in events, "the report never ran — this would pass vacuously"
    assert "give_up" in events, "no ticker was closed at all"
    assert events.index("give_up") < events.index("report"), (
        f"the ticker was still running while the report printed: {events}"
    )


def test_a_delete_that_finishes_leaves_nothing_registered(capsys):
    from comfy_qa import inflight

    cloud = Cloud()
    _delete_with(cloud)

    assert cloud.deleted(), "the delete should have run"
    assert inflight.pending() == []
    assert "probably destroyed" not in capsys.readouterr().err


def _delete_with(cloud, tmp=None):
    """`delete --yes` against one host list, with the cloud handed in.

    Not the `cli` fixture: `CliRunner` swallows the report's stream, and what is
    under test here is what reaches a terminal.
    """
    import tempfile
    from pathlib import Path

    from comfy_qa import gcloud as gcloud_module
    from comfy_qa.remove import delete_cmd

    directory = Path(tmp or tempfile.mkdtemp())
    path = directory / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    real = gcloud_module.Gcloud
    gcloud_module.Gcloud = lambda *a, **k: cloud
    try:
        return delete_cmd(name="comfy-win", config=path, yes=True)
    finally:
        gcloud_module.Gcloud = real


# --- the confirmation, which is the last thing between a person and a loss ---


def test_the_confirmation_says_how_big_the_disk_is(cli):
    """"and its boot disk" is skimmed. "its 200 GB boot disk" is read.

    This is the last irreversible thing the tool does and the confirmation is
    the only place a mistake can be caught, so the line has to carry something a
    person either recognises or does not. A size is that; a noun is not.
    """
    result = cli("delete", "comfy-win", cloud=Cloud(disk_gb="200"),
                 input="comfy-win\n")

    assert "its 200 GB boot disk" in result.output, result.output
    # Read off the boot disk, whose name is its own — `comfy-win` boots from
    # `comfy-win-a`, and deriving one from the other is right by luck.
    assert any("disks describe comfy-win-a" in call for call in result.cloud.calls)


def test_a_disk_size_that_cannot_be_read_still_asks_the_question(cli):
    """Degrades to the old wording rather than failing.

    A confirmation that cannot name a size still has to be shown: refusing to
    delete because a cosmetic read failed is the wrong trade on a command
    somebody has already decided to run.
    """
    result = cli("delete", "comfy-win", cloud=Cloud(disk_gb=None),
                 input="comfy-win\n")

    assert "and its boot disk." in result.output, result.output
    assert "GB" not in result.output
    assert result.cloud.deleted(), "the delete still went through"


def test_choosing_what_to_destroy_is_sent_to_the_live_listing(cli):
    """`comfy-qat list` reads the host file, where a box that is stopped and a
    box that was destroyed last week look identical. That difference does not
    matter until the moment you are choosing what to destroy."""
    for args in (("delete",), ("delete", "no-such-box")):
        result = cli(*args)
        assert result.exit_code == 2
        assert "comfy-qat list --live" in result.output, args
