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

    def invoke(*args, cloud=None, input=None, tty=True, declared=HOSTS,
               keep=False):
        cloud = cloud if cloud is not None else Cloud()
        path = tmp_path / "hosts.toml"
        # `keep` is for a second run of the same command against the host list
        # the first one left — the rerun a failure tells the user to make.
        if not (keep and path.exists()):
            path.write_text(declared, encoding="utf-8")
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


# --- a reserved box: the reservation goes with it ----------------------------
#
# A reserved box has a second thing to destroy, and it is the one that costs.
# Its reservation bills for the card every hour, running or stopped, until it is
# released — so a `delete` that removed the box and left the reservation would
# leave the expensive half of the bill running with nothing in the host list
# naming it. "Reserved until the box is deleted" is a promise this command
# keeps or breaks.
#
# The fake below keeps STATE, not answers: a reservation that was released is
# gone the next time anything looks, and one that was not is still there to be
# found. A flow that skips the release therefore fails here the way it would
# fail live — by leaving something behind.
#
# Every record is a FIXTURE in the shape the SDK's API schema gives a
# reservation and an instance. None is a recorded live payload: no reserved box
# had been made on a real project when this was written, and whether Google
# will release a reservation that a STOPPED box still targets is not yet read
# off it. `RELEASE_FIRST` is the one line that changes if it will not, and both
# orders are driven below.

RESERVED = HOSTS.replace(
    'gce_zone     = "us-central1-c"\ngce_project  = "proj"\n',
    'gce_zone     = "us-central1-c"\ngce_project  = "proj"\n'
    'gce_reservation = "comfy-linux-rsv"\n')

RSV = "comfy-linux-rsv"
RSV_ZONE = "us-central1-c"
# Typed out whole, `--quiet` included: without it gcloud stops to ask, and
# pasted into a script it exits 1 with the reservation still billing. Found on
# a real project.
RELEASE = (f"gcloud compute reservations delete {RSV} --zone={RSV_ZONE} "
           f"--project=proj --quiet")


def held(name=RSV, *, zone=RSV_ZONE, box="comfy-linux", count="1"):
    """One reservation, as `reservations list` returns it. `box=""` is one this
    tool did not make: its description is somebody else's sentence."""
    return {
        "name": name, "zone": f"https://x/projects/proj/zones/{zone}",
        "status": "READY", "specificReservationRequired": True,
        "description": f"comfy-qat: held for {box}" if box else "made in the console",
        "specificReservation": {"count": count, "instanceProperties": {
            "machineType": "g2-standard-8"}},
    }


def box(name="comfy-linux", *, zone=RSV_ZONE, bound=RSV):
    """One instance, as `instances list` returns it, stopped."""
    row = {"name": name, "zone": f"https://x/projects/proj/zones/{zone}",
           "status": "TERMINATED"}
    if bound:
        row["reservationAffinity"] = {
            "consumeReservationType": "SPECIFIC_RESERVATION",
            "key": "compute.googleapis.com/reservation-name", "values": [bound]}
    return row


class Project(Cloud):
    """`Cloud`, on a project whose reservations and instances are state.

    `release=` is what releasing does instead of succeeding. `unreadable=` names
    the read that fails — `absent`, `reservations` or `instances` — because a
    read that failed and a reservation that is not there are the two answers
    this command must never give the same treatment.
    """

    def __init__(self, *, reservations=None, instances=None, release=None,
                 unreadable=None, **kwargs):
        super().__init__(**kwargs)
        self.reservations = [held()] if reservations is None else list(reservations)
        self.instances = [box()] if instances is None else list(instances)
        self._release = release
        self._unreadable = unreadable

    def _read(self, which):
        self.calls.append(f"read {which}")
        if self._unreadable == which:
            raise GcloudError("permission denied")

    def reservation_absent(self, name, zone, project):
        self._read("absent")
        return not any(row["name"] == name and row["zone"].endswith(f"/{zone}")
                       for row in self.reservations)

    def list_reservations(self, project):
        self._read("reservations")
        return list(self.reservations)

    def list_instances(self, project):
        self._read("instances")
        return list(self.instances)

    def delete_reservation(self, name, zone, project):
        self.calls.append(f"release {name} {zone} {project}")
        if self._release is not None:
            raise self._release
        self.reservations = [row for row in self.reservations
                             if not (row["name"] == name
                                     and row["zone"].endswith(f"/{zone}"))]

    def run(self, args, **kwargs):
        answer = super().run(args, **kwargs)
        joined = " ".join(str(a) for a in args)
        if joined.startswith("compute instances delete"):
            self.instances = [row for row in self.instances if row["name"] != args[3]]
        return answer

    def released(self):
        return [call for call in self.calls if call.startswith("release ")]


def gone_from_google(cloud: Project) -> Project:
    """The same project after the box was deleted with Google's own command:
    the status read is a not-found and the listing does not carry the name."""
    from comfy_qa.gcloud import GONE

    after = Project(reservations=cloud.reservations, instances=[
        row for row in cloud.instances if row["name"] != "comfy-linux"],
        status=GcloudError("The resource 'comfy-linux' was not found"),
        listing={("comfy-linux", RSV_ZONE, "proj"): GONE})
    return after


def flat(text: str) -> str:
    """`text` on one line. `say` wraps prose at 96 columns, and a sentence is
    the same sentence wherever it was broken."""
    return " ".join(text.split())


def entries(tmp_path) -> set[str]:
    import tomllib

    return set(tomllib.loads((tmp_path / "hosts.toml").read_text(
        encoding="utf-8"))["hosts"])


def test_deleting_a_reserved_box_releases_its_reservation(cli, tmp_path):
    cloud = Project()
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    assert result.exit_code == 0, result.output
    assert cloud.released() == [f"release {RSV} {RSV_ZONE} proj"]
    assert cloud.reservations == [], "the reservation is still on the project"
    assert cloud.deleted(), "the box was not deleted"
    assert "comfy-linux" not in entries(tmp_path)
    # What went, typed out: the box, its disk, and the half that was billing.
    assert ("comfy-linux and its disk are gone, and it is out of your host list. "
            "Its reservation comfy-linux-rsv was released, so nothing of it is "
            "billing.") in flat(result.stdout)
    # A sentence, so it is held to the width sentences are: it is 150
    # characters with these names in it, and reached the terminal unbroken.
    assert max(len(line) for line in result.stdout.splitlines()) <= 96, result.stdout


def test_the_reservation_is_released_before_the_box_is_deleted(cli):
    """It is the half that bills at a GPU's rate. Released first, a failure in
    between leaves a stopped box; the other way round it leaves a reservation
    billing with nothing in any list of machines."""
    cloud = Project()
    cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED, input="comfy-linux\n")

    release = cloud.calls.index(f"release {RSV} {RSV_ZONE} proj")
    delete = cloud.calls.index(cloud.deleted()[0])
    assert release < delete, cloud.calls


def test_the_other_order_is_one_switch_and_still_releases(cli, tmp_path, monkeypatch):
    """`RELEASE_FIRST` is the whole of the order. Flipped, the same two calls
    happen the other way round and nothing else about the command changes —
    which is what makes it cheap to change once Google has been asked."""
    from comfy_qa import remove

    monkeypatch.setattr(remove, "RELEASE_FIRST", False)
    cloud = Project()
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    assert result.exit_code == 0, result.output
    assert cloud.calls.index(cloud.deleted()[0]) < cloud.calls.index(
        f"release {RSV} {RSV_ZONE} proj"), cloud.calls
    assert cloud.reservations == [] and "comfy-linux" not in entries(tmp_path)


def test_the_confirmation_names_the_reservation_with_the_box_and_the_disk(cli):
    """All three go, so all three are named — before the name is typed back.
    The reservation is the one nobody thinks of, and the one that bills."""
    result = cli("delete", "comfy-linux", cloud=Project(), declared=RESERVED,
                 input="comfy-linux\n")

    assert ("delete comfy-linux in us-central1-c, its 200 GB boot disk, and its "
            "reservation comfy-linux-rsv.") in flat(result.stdout)


def test_nothing_is_released_until_the_name_is_typed_back(cli, tmp_path):
    """A release is as irreversible as the delete — the capacity it held may not
    be there to reserve again — so it waits for the same confirmation."""
    cloud = Project()
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-win\n")

    assert result.exit_code == 2
    assert cloud.released() == [] and not cloud.deleted()
    assert len(cloud.reservations) == 1, "it was released on a wrong answer"
    assert "comfy-linux" in entries(tmp_path)


def test_a_box_that_is_not_reserved_is_asked_nothing_about_reservations(cli):
    """Three reads and a release are for a box that has a reservation. One that
    does not is deleted exactly as it always was."""
    cloud = Project()
    result = cli("delete", "comfy-linux", cloud=cloud, input="comfy-linux\n")

    assert result.exit_code == 0, result.output
    assert cloud.deleted()
    assert not [call for call in cloud.calls if call.startswith(("read ", "release "))]
    assert "reservation" not in result.output


# --- absent is not the same as unread ----------------------------------------


def test_a_reservation_already_released_is_said_and_the_box_still_goes(cli, tmp_path):
    """Google says there is no such reservation: that is most of the job done,
    not a reason to refuse. Said in as many words, and nothing is released."""
    cloud = Project(reservations=[])
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    assert result.exit_code == 0, result.output
    assert ("its reservation comfy-linux-rsv is not on proj — already released."
            in flat(result.stdout))
    assert cloud.released() == []
    assert cloud.deleted() and "comfy-linux" not in entries(tmp_path)
    # And the confirmation did not promise to destroy something that is not there.
    assert "and its reservation" not in result.stdout
    assert "was released, so nothing" not in result.stdout


@pytest.mark.parametrize("read", ["absent", "reservations", "instances"])
def test_a_reservation_that_cannot_be_read_refuses_before_anything(cli, tmp_path, read):
    """A read that failed is not an absence. Treated as one, the box would be
    deleted and its reservation left billing with nothing naming it — so each
    of the three reads refuses, and refuses before the confirmation."""
    cloud = Project(unreadable=read)
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    assert result.exit_code == 2, result.output
    assert ("could not read comfy-linux's reservation comfy-linux-rsv (permission "
            "denied), so whether it would be released is not known. Nothing was "
            "deleted.") in flat(result.output)
    assert cloud.released() == [] and not cloud.deleted()
    assert len(cloud.reservations) == 1 and "comfy-linux" in entries(tmp_path)
    assert "type comfy-linux to confirm" not in result.output


# --- the release fails -------------------------------------------------------


def test_a_release_that_fails_keeps_the_box_and_the_entry_and_says_still_billing(
        cli, tmp_path):
    """Exit 1 — the work started and failed — and the sentence, typed out, that
    says which half did not happen and what it is costing."""
    cloud = Project(release=GcloudError("the reservation is in use"))
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    assert result.exit_code == 1, result.output
    assert ("could not release comfy-linux-rsv (the reservation is in use), so "
            "comfy-linux was not deleted and its reservation is still billing."
            ) in flat(result.output)
    assert not cloud.deleted(), "the box was deleted with its reservation still held"
    assert "comfy-linux" in entries(tmp_path), "the only record of it was removed"
    # Both commands, each whole on a line of its own.
    lines = [line.strip() for line in result.output.splitlines()]
    assert RELEASE in lines
    assert "comfy-qat delete comfy-linux" in lines


def test_the_rerun_a_failed_release_prints_finishes_the_job(cli, tmp_path):
    """The remedy is two commands, and the second is this tool's own — so it is
    run. After the first (Google's, done here by taking the reservation out of
    the project) the same `comfy-qat delete` has to find it gone and carry on."""
    cloud = Project(release=GcloudError("the reservation is in use"))
    first = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                input="comfy-linux\n")
    rerun = next(line.strip() for line in first.output.splitlines()
                 if line.strip() == "comfy-qat delete comfy-linux")

    after = Project(reservations=[], instances=cloud.instances)
    second = cli(*rerun.split()[1:], cloud=after, input="comfy-linux\n", keep=True)

    assert second.exit_code == 0, second.output
    assert after.deleted() and after.released() == []
    assert "already released" in second.stdout
    assert "comfy-linux" not in entries(tmp_path)


def test_a_release_that_fails_after_the_box_is_gone_says_nothing_is_on_it(
        cli, tmp_path, monkeypatch):
    """The other order's failure, which is the worse one and says so: the box
    is gone, the reservation bills with nothing on it, and the entry is kept as
    the only thing that still names it."""
    from comfy_qa import remove

    monkeypatch.setattr(remove, "RELEASE_FIRST", False)
    cloud = Project(release=GcloudError("timed out"))
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    assert result.exit_code == 1, result.output
    assert cloud.deleted()
    assert ("comfy-linux and its disk are gone, but its reservation "
            "comfy-linux-rsv could not be released (timed out), so it is still "
            "billing with no box on it.") in flat(result.output)
    assert "comfy-linux" in entries(tmp_path)
    assert RELEASE in [line.strip() for line in result.output.splitlines()]


def test_a_box_that_will_not_delete_after_the_release_says_which_half_happened(
        cli, tmp_path):
    """Half of it happened, and it is the half that mattered for the bill. "The
    delete failed" must not be read as "nothing changed"."""
    cloud = Project(fail=GcloudError("the instance is locked"))
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    assert result.exit_code == 1, result.output
    assert cloud.reservations == [], "the release did not happen"
    assert ("comfy-linux's reservation comfy-linux-rsv was released, so it is no "
            "longer billing for the card — but comfy-linux itself was not "
            "deleted, and its disk still bills.") in flat(result.output)
    assert "comfy-linux" in entries(tmp_path)
    assert "comfy-qat delete comfy-linux" in [
        line.strip() for line in result.output.splitlines()]


# --- the box is already gone -------------------------------------------------


def test_a_box_already_gone_still_has_its_reservation_released(cli, tmp_path):
    """Deleted in the console, and its reservation left behind — which is the
    half still billing. The path that only used to take the entry out has to
    release it too."""
    cloud = gone_from_google(Project())
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    assert result.exit_code == 0, result.output
    assert not cloud.deleted(), "there is no instance to delete"
    assert cloud.released() == [f"release {RSV} {RSV_ZONE} proj"]
    assert cloud.reservations == []
    assert "comfy-linux" not in entries(tmp_path)
    said = flat(result.stdout)
    assert ("it has already been deleted, so only its reservation comfy-linux-rsv "
            "and the host list entry are left.") in said
    assert "Its reservation comfy-linux-rsv was released" in said


def test_a_box_already_gone_and_already_released_is_only_the_entry(cli, tmp_path):
    cloud = gone_from_google(Project(reservations=[]))
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    assert result.exit_code == 0, result.output
    assert cloud.released() == [] and not cloud.deleted()
    assert "so only the host list entry is left." in flat(result.stdout)
    assert "already released" in result.stdout
    assert "comfy-linux" not in entries(tmp_path)


# --- somebody else's capacity ------------------------------------------------


SHARED = {
    "two machines": dict(reservations=[held(count="2")]),
    "another box": dict(instances=[box(), box("somebody-elses")]),
}


@pytest.mark.parametrize("case", sorted(SHARED))
def test_a_shared_reservation_is_never_released_and_the_box_is_not_deleted(
        cli, tmp_path, case):
    """This tool only ever makes a reservation for one machine. One that holds
    two, or that another box is sitting on, is somebody else's capacity as well
    — and releasing it would take it from under them."""
    cloud = Project(**SHARED[case])
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    assert result.exit_code == 2, result.output
    assert cloud.released() == [] and not cloud.deleted()
    assert len(cloud.reservations) == 1 and "comfy-linux" in entries(tmp_path)
    said = flat(result.output)
    assert ("comfy-linux's reservation comfy-linux-rsv holds capacity for 2 "
            "machines, where one this tool makes holds it for exactly one. "
            "Deleting comfy-linux would release capacity that is not this box's "
            "alone" if case == "two machines" else
            "comfy-linux's reservation comfy-linux-rsv is shared — other machines "
            "are on it: somebody-elses. Deleting comfy-linux would release it "
            "from under them") in said
    assert "nothing was deleted" in said


def test_a_reservation_this_box_has_no_claim_on_is_refused(cli, tmp_path):
    """Not made by this tool for this box, and the box is not bound to it. The
    host list names it and nothing else does — and a name in a file somebody
    edits is not a reason to release a reservation."""
    cloud = Project(reservations=[held(box="")], instances=[box(bound=None)])
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    assert result.exit_code == 2, result.output
    assert cloud.released() == [] and not cloud.deleted()
    said = flat(result.output)
    assert ("was not made by this tool for comfy-linux, and comfy-linux is not "
            "bound to it. Deleting comfy-linux would release a reservation this "
            "box has no claim on, so nothing was deleted.") in said
    # "From under them" is the shared refusal's, and here there is no "them".
    assert "from under them" not in said


def test_a_reservation_made_for_another_box_is_not_this_ones_by_name_alone(cli):
    """`comfy-qat: held for other-box`, under this box's reservation name. The
    description is the evidence, and it names somebody else."""
    cloud = Project(reservations=[held(box="other-box")], instances=[box(bound=None)])
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    assert result.exit_code == 2 and cloud.released() == []


def test_a_console_made_reservation_the_box_is_bound_to_is_its_own(cli, tmp_path):
    """Adopted with `discover`: nobody here made the reservation, but the box's
    own record says it may consume this one and no other, and nothing else is
    on it. That is this box's reservation, and "released on delete" covers it."""
    cloud = Project(reservations=[held(box="")])
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    assert result.exit_code == 0, result.output
    assert cloud.released() == [f"release {RSV} {RSV_ZONE} proj"]
    assert cloud.deleted() and "comfy-linux" not in entries(tmp_path)


def test_the_box_delete_a_shared_reservation_refusal_hands_over_does_not_prompt(cli):
    """`gcloud compute instances delete` stops to ask "Do you want to continue
    (Y/n)?" unless it is told `--quiet`. Pasted into a script or handed to an
    agent, a command that waits for an answer exits 1 and deletes nothing —
    and what it did not delete is still billing. The whole line, as printed:
    `--delete-disks=all` so the disk goes with the box, `--quiet` so it runs."""
    cloud = Project(instances=[box(), box("somebody-elses")])
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    assert result.exit_code == 2, result.output
    lines = [line.strip() for line in result.output.splitlines()]
    assert ("gcloud compute instances delete comfy-linux --zone=us-central1-c "
            "--project=proj --delete-disks=all --quiet") in lines
    assert RELEASE in lines
    for line in lines:
        if line.startswith("gcloud compute") and " delete " in line:
            assert line.endswith(" --quiet"), line


def test_the_way_out_of_a_shared_reservation_runs_and_leaves_it_alone(cli, tmp_path):
    """The refusal hands over three lines. The first and last are Google's; the
    middle one is this tool's, so it is run — after the first has been done —
    and it has to take the entry out WITHOUT releasing what is not this box's.

    This is the path that must not become a trap: a box deleted by hand whose
    entry no command can remove was a defect this file already records.
    """
    cloud = Project(instances=[box(), box("somebody-elses")])
    first = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                input="comfy-linux\n")
    lines = [line.strip() for line in first.output.splitlines()]
    assert ("gcloud compute instances delete comfy-linux --zone=us-central1-c "
            "--project=proj --delete-disks=all --quiet") in lines
    rerun = next(line for line in lines if line == "comfy-qat delete comfy-linux")

    after = gone_from_google(cloud)
    second = cli(*rerun.split()[1:], cloud=after, input="comfy-linux\n", keep=True)

    assert second.exit_code == 0, second.output
    assert after.released() == [], "it released a reservation another box is on"
    assert len(after.reservations) == 1
    assert "comfy-linux" not in entries(tmp_path)
    said = flat(second.stdout)
    assert "so it is left alone — it is still on proj and still billing" in said
    # Said again as the last thing printed, with the command whole on its line.
    tail = second.stdout.rstrip().splitlines()
    assert tail[-1].strip() == RELEASE


# --- Ctrl-C during the release -----------------------------------------------


def test_an_interrupted_release_says_it_may_or_may_not_have_gone(capsys, tmp_path,
                                                                 monkeypatch):
    """The request reaches Google before the interrupt reaches gcloud, so the
    reservation may be gone or may not — and it bills until it is. The report
    is the only thing that says so: the box has not been touched, and the entry
    is still in the host list."""
    from comfy_qa import cli as cli_module
    from comfy_qa import gcloud as gcloud_module
    from comfy_qa import inflight

    class Interrupted(Project):
        def delete_reservation(self, name, zone, project):
            self.calls.append(f"release {name} {zone} {project}")
            raise KeyboardInterrupt

    cloud = Interrupted()
    path = tmp_path / "hosts.toml"
    path.write_text(RESERVED, encoding="utf-8")
    monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: cloud)
    monkeypatch.setattr("sys.argv", ["comfy-qat", "delete", "comfy-linux", "--yes",
                                     "--config", str(path)])

    with pytest.raises(SystemExit) as stopped:
        cli_module.main()

    assert stopped.value.code == inflight.INTERRUPTED
    printed = capsys.readouterr()
    tail = (printed.out + printed.err).split("releasing comfy-linux-rsv")[-1]
    assert "this may or may not have been released:" in tail
    assert "the reservation comfy-linux-rsv in us-central1-c" in tail
    assert "gcloud compute reservations list --project=proj" in tail
    assert "comfy-qat delete comfy-linux" in tail
    assert "still billing" in tail
    assert not cloud.deleted(), "the box was deleted after an interrupted release"
    assert "comfy-linux" in entries(tmp_path)


# --- found by the audit ------------------------------------------------------


def test_a_reservation_that_holds_no_machines_is_not_called_shared(cli):
    """A count that reads 0. It is not one this tool made and is not released
    — and it is not "shared … capacity for 0 machines" either."""
    cloud = Project(reservations=[held(count="0")])
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    assert result.exit_code == 2 and cloud.released() == []
    said = flat(result.output)
    assert "holds capacity for 0 machines" in said
    assert "is shared" not in said and "from under them" not in said


def test_a_box_first_delete_that_fails_says_the_reservation_was_never_released(
        cli, tmp_path, monkeypatch):
    """The other order's OTHER failure. With the box deleted first, an instance
    delete that Google refuses means the release was never reached — and the
    only line printed used to be Google's reason for refusing the instance,
    which says nothing about a reservation at all. Typed out: what was not
    deleted, what was not released, and that it is still billing."""
    from comfy_qa import remove

    monkeypatch.setattr(remove, "RELEASE_FIRST", False)
    cloud = Project(fail=GcloudError("the instance is locked"))
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    assert result.exit_code == 1, result.output
    assert cloud.released() == [], "it released a reservation after a failed delete"
    assert len(cloud.reservations) == 1
    said = flat(result.output)
    assert ("comfy-linux was not deleted, so its reservation comfy-linux-rsv was "
            "not released either and is still billing.") in said
    assert "the instance is locked" in said, "and Google's own reason is still there"
    assert "was released" not in said
    assert "comfy-linux" in entries(tmp_path)
    assert "comfy-qat delete comfy-linux" in [
        line.strip() for line in result.output.splitlines()]


def test_the_two_orders_each_say_only_their_own_half(cli, monkeypatch):
    """The control for the test above: in the release-first order the same
    failed instance delete says the reservation WAS released, and must not say
    it is still billing."""
    cloud = Project(fail=GcloudError("the instance is locked"))
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    said = flat(result.output)
    assert "was released, so it is no longer billing for the card" in said
    assert "was not released either" not in said


class Bound(Project):
    """A project whose box says, in its OWN record, which reservation it is
    on — which is what `delete` reads when the host list entry does not."""

    def describe_instance(self, name, zone, project):
        described = super().describe_instance(name, zone, project)
        listed = next((row for row in self.instances if row["name"] == name), {})
        if "reservationAffinity" in listed:
            described["reservationAffinity"] = listed["reservationAffinity"]
        return described


def test_a_box_reserved_on_google_with_no_line_in_the_host_list_is_still_released(
        cli, tmp_path):
    """The entry has no `gce_reservation`: adopted before the field existed,
    reserved in the console, or typed in by hand after a create that could not
    write the file. The box is bound all the same and its reservation bills
    all the same — and `delete` used to remove the box, say "and its disk are
    gone", and leave the reservation billing with nothing naming it."""
    cloud = Bound()
    result = cli("delete", "comfy-linux", cloud=cloud, input="comfy-linux\n")  # HOSTS

    assert result.exit_code == 0, result.output
    assert cloud.released() == [f"release {RSV} {RSV_ZONE} proj"]
    assert cloud.reservations == [], "the reservation was left billing"
    assert cloud.deleted() and "comfy-linux" not in entries(tmp_path)
    said = flat(result.stdout)
    assert ("comfy-linux is reserved, though its host list entry does not say so: "
            "comfy-linux is bound to the reservation comfy-linux-rsv") in said
    assert "its 200 GB boot disk, and its reservation comfy-linux-rsv." in said
    assert "Its reservation comfy-linux-rsv was released" in said
    # Said BEFORE the name is typed back: it changes what a yes destroys.
    assert result.stdout.index("though its host list entry") < result.stdout.index(
        "type comfy-linux to confirm")


def test_an_undeclared_reservation_is_held_to_the_same_refusals(cli, tmp_path):
    """Read off the instance, it gets no shortcut: one another box is on is
    still not released, and the box is still not deleted."""
    cloud = Bound(instances=[box(), box("somebody-elses")])
    result = cli("delete", "comfy-linux", cloud=cloud, input="comfy-linux\n")

    assert result.exit_code == 2, result.output
    assert cloud.released() == [] and not cloud.deleted()
    assert "other machines are on it: somebody-elses" in flat(result.output)


def test_a_box_whose_record_cannot_be_read_is_deleted_as_its_entry_describes_it(cli):
    """Best effort, like the read that sizes the disk: a describe that fails
    is not a reason to refuse a delete somebody has decided on."""
    class Unreadable(Project):
        def describe_instance(self, name, zone, project):
            self.calls.append(f"describe {name}")
            raise GcloudError("timed out")

    cloud = Unreadable()
    result = cli("delete", "comfy-linux", cloud=cloud, input="comfy-linux\n")

    assert result.exit_code == 0, result.output
    assert cloud.deleted() and cloud.released() == []
    # Deleted — and NOT in silence. "Could not ask" is not "bound to nothing":
    # a reservation may be billing that this did not release, so it says the
    # check was not made and how to make it, before the name is typed back.
    said = flat(result.stdout)
    assert ("whether comfy-linux had a reservation was not checked — its own "
            "record could not be read. One left behind bills with nothing on it; "
            "look:") in said
    assert "  gcloud compute reservations list --project=proj" in result.stdout.splitlines()
    assert result.stdout.index("was not checked") < result.stdout.index(
        "type comfy-linux to confirm")


# --- gone, and the entry has no reservation line -------------------------------
#
# The box has no record left to ask, and `delete` used to stop there: one line
# saying the check was not made, and then "it has already been deleted, so only
# the host list entry is left" — a sentence about the PROJECT, printed without
# reading it. The project still knows: a reservation this tool made carries the
# box's name. Every entry below is the plain one, `HOSTS`, with no
# `gce_reservation` line.

ONLY_THE_ENTRY = "only the host list entry is left"


def test_a_gone_box_with_no_reservation_line_has_its_own_reservation_released(
        cli, tmp_path):
    """This tool's own reservation for the box, in the entry's zone, with
    nothing bound to it: released, by the rules a declared one is released by,
    and said to be there before the name is typed back."""
    cloud = gone_from_google(Project(reservations=[held()]))
    result = cli("delete", "comfy-linux", cloud=cloud, input="comfy-linux\n")

    assert result.exit_code == 0, result.output
    said = flat(result.stdout)
    assert ("comfy-linux's host list entry has no reservation line, but "
            "comfy-linux-rsv is on proj: this tool made it for comfy-linux, and "
            "it bills every hour with or without the box.") in said
    assert ("it has already been deleted, so only its reservation "
            "comfy-linux-rsv and the host list entry are left.") in said
    assert ONLY_THE_ENTRY not in said
    assert said.index("has no reservation line") < said.index(
        "type comfy-linux to confirm")
    assert cloud.released() == [f"release {RSV} {RSV_ZONE} proj"]
    assert cloud.reservations == []
    assert "Its reservation comfy-linux-rsv was released" in said
    assert "comfy-linux" not in entries(tmp_path)


def test_that_entry_is_kept_while_its_reservation_could_not_be_released(cli, tmp_path):
    """The entry is the only thing in this tool that still names what is
    billing, so it stays until the reservation is gone — as it does for a
    reservation the entry declares."""
    cloud = gone_from_google(Project(reservations=[held()]))
    cloud._release = GcloudError("the reservation is in use")
    result = cli("delete", "comfy-linux", cloud=cloud, input="comfy-linux\n")

    assert result.exit_code != 0, result.output
    assert cloud.reservations != [], "the fake released it anyway"
    assert "comfy-linux" in entries(tmp_path)
    assert ONLY_THE_ENTRY not in flat(result.output)
    assert RELEASE in result.output


def test_a_gone_box_does_not_release_its_reservation_from_under_another_box(
        cli, tmp_path):
    """Made by this tool for this box — and another machine is bound to it
    now. Named, with Google's command, and left alone."""
    cloud = gone_from_google(Project(
        reservations=[held()], instances=[box("somebody-else")]))
    result = cli("delete", "comfy-linux", cloud=cloud, input="comfy-linux\n")

    assert result.exit_code == 0, result.output
    said = flat(result.stdout)
    assert cloud.released() == []
    assert ("its reservation comfy-linux-rsv is shared — other machines are on "
            "it: somebody-else, so it is left alone") in said
    assert f"  {RELEASE}" in result.stdout.splitlines()
    assert ONLY_THE_ENTRY not in said
    assert ("so its host list entry is left, and a reservation this command "
            "does not release.") in said


def test_a_gone_box_does_not_release_a_reservation_that_only_has_its_name(
        cli, tmp_path):
    """`comfy-linux-rsv`, made in the console: the name is not a claim —
    anybody can call a reservation that. Named, with Google's own command, not
    released; and the entry goes, since nothing here is this tool's to wait
    for."""
    cloud = gone_from_google(Project(reservations=[held(box="")]))
    result = cli("delete", "comfy-linux", cloud=cloud, input="comfy-linux\n")

    assert result.exit_code == 0, result.output
    said = flat(result.stdout)
    assert cloud.released() == [] and cloud.reservations != []
    assert ("comfy-linux-rsv (us-central1-c) is on proj and may have been "
            "comfy-linux's, but nothing establishes that it was — it is not one "
            "this tool made for comfy-linux in us-central1-c — so it is left "
            "alone, and it is still billing. Check whose it is, then release "
            "it:") in said
    assert f"  {RELEASE}" in result.stdout.splitlines()
    assert ONLY_THE_ENTRY not in said
    assert "comfy-qat delete" not in result.stdout
    assert "comfy-linux" not in entries(tmp_path)
    # Said again as the last thing printed: it is what is still billing.
    assert result.stdout.rstrip().splitlines()[-1] == f"  {RELEASE}"


def test_this_tools_reservation_for_the_box_in_another_zone_is_named_not_released(cli):
    """The mark says it was made for a box of this name; the zone says not the
    one this entry points at. Not established, so not released."""
    cloud = gone_from_google(Project(reservations=[held(zone="us-east1-b")]))
    result = cli("delete", "comfy-linux", cloud=cloud, input="comfy-linux\n")

    assert result.exit_code == 0, result.output
    assert cloud.released() == []
    assert ("  gcloud compute reservations delete comfy-linux-rsv "
            "--zone=us-east1-b --project=proj --quiet") in result.stdout.splitlines()
    assert ONLY_THE_ENTRY not in flat(result.stdout)


def test_a_gone_box_whose_project_cannot_list_reservations_is_not_called_clear(
        cli, tmp_path):
    """Not read is not none. It says the check could not be made and prints
    the command that makes it — and does not say only the entry is left."""
    cloud = gone_from_google(Project(reservations=[held()]))
    cloud._unreadable = "reservations"
    result = cli("delete", "comfy-linux", cloud=cloud, input="comfy-linux\n")

    assert result.exit_code == 0, result.output
    said = flat(result.stdout)
    assert ("whether comfy-linux had a reservation was not checked — it is gone, "
            "and the project's reservations could not be listed. One left behind "
            "bills with nothing on it; look:") in said
    assert "  gcloud compute reservations list --project=proj" in result.stdout.splitlines()
    assert ONLY_THE_ENTRY not in said
    assert ("so its host list entry is left — and whether a reservation is too "
            "is not known.") in said
    assert cloud.released() == [], "it released on a guess"
    assert "comfy-linux" not in entries(tmp_path)


@pytest.mark.parametrize("reservations", [
    [],
    [held("other-rsv", box="other")],
], ids=["none at all", "only another box's"])
def test_only_the_entry_is_left_is_said_when_that_was_read(cli, tmp_path, reservations):
    """The control, and the one case the sentence is true in: the project's
    reservations were read and none of them is this box's."""
    cloud = gone_from_google(Project(reservations=reservations))
    result = cli("delete", "comfy-linux", cloud=cloud, input="comfy-linux\n")

    assert result.exit_code == 0, result.output
    said = flat(result.stdout)
    assert ("comfy-linux is not on proj — it has already been deleted, so only "
            "the host list entry is left.") in said
    assert "read reservations" in cloud.calls, "said without looking"
    assert "was not checked" not in said and "left alone" not in said
    assert cloud.released() == []
    assert "comfy-linux" not in entries(tmp_path)


def test_a_box_that_was_asked_and_is_bound_to_nothing_is_told_nothing_of_the_kind(cli):
    """The control: the record was read and names no reservation. That is an
    answer, and a nudge to go and look would be noise on every ordinary delete."""
    result = cli("delete", "comfy-linux", cloud=Project(), input="comfy-linux\n")

    assert result.exit_code == 0
    assert "was not checked" not in result.output


def test_an_already_gone_box_is_told_its_reservation_is_still_billing_before_it_goes(cli):
    """The sentence above the confirmation on the already-gone path. It could
    be deleted with every other test green: the release still happened and the
    closing line still said so. It is the one line that tells somebody, before
    they type the name, that something of this box is costing money right now."""
    cloud = gone_from_google(Project())
    result = cli("delete", "comfy-linux", cloud=cloud, declared=RESERVED,
                 input="comfy-linux\n")

    said = flat(result.stdout)
    assert ("its reservation comfy-linux-rsv in us-central1-c is still billing, "
            "and releasing it is the only way to stop that — so it will be "
            "released.") in said
    assert said.index("is still billing, and releasing it") < said.index(
        "type comfy-linux to confirm")
