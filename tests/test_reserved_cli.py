"""Reserved boxes, the reservation limit and boxes with no GPU — through the CLI.

The engine under these commands has tests of its own: `test_reservation.py`
holds the arithmetic, `test_reserved_build.py` the two calls and every way of
stopping between them, `test_cpu_box.py` the box with no card. Every one of
those passes with the command wired wrongly — the limit computed and never
consulted, the reservations never read, `--reserve` parsed and dropped on the
way to the plan. A function that is correct with a caller that is not is the
shape this file exists for, so everything here goes in through `comfy-qat`
and is judged on what was SENT and what was PRINTED.

Four rules, each of which a test below would fail without:

  * A dry run makes no mutating call. Asserted on the whole call list, against
    the set of mutating calls derived from `gcloud.py`'s own argv rather than
    typed here.
  * A refusal costs nothing: exit 2, and no mutating call before it.
  * Absent, zero and not-read are three answers. A reservations read that
    FAILED is not a project with nothing reserved, and the two creates —
    reserving and not — do different things about it.
  * A printed remedy runs. Every `comfy-qat ...` line in a refusal here is
    pasted back into the CLI and has to do what the refusal said it would.

The fake cloud keeps STATE: a reservation that was made is listed until it is
released, a box that was stopped is stopped the next time it is asked about.
Every payload is a FIXTURE in the shape the SDK's API schema gives; none is a
recorded live payload, because no reserved box had been made on a real project
when this was written. What Google does about quota for a reservation, and what
a built-in card's reservation looks like read back, are both unverified.
"""

from __future__ import annotations

import shlex

import pytest
from typer.testing import CliRunner

from comfy_qa import gcloud as gcloud_module
from comfy_qa.cli import app
from comfy_qa.gcloud import GcloudError, statuses_from

from test_create_cli import (  # noqa: F401  (the fixture is used by name)
    HOSTS,
    PROJECT,
    QUOTAS,
    REGIONS,
    STOCKOUT,
    URL,
    ZONES,
    FakeGcloud,
    never_time_a_real_connection,
    reservation_row,
)
from test_host_costs import _mutating_gcloud_methods

RSV = "comfy-linux-rsv"

# The sentence about what a reserved box costs, typed out. Not imported from
# `reservation.py`: a test that asserts a value against the thing that produced
# it cannot fail.
BILL = ("comfy-linux is reserved. Google holds its capacity and bills for it "
        "every hour — running or stopped — until the box is deleted.")
DELETE_LINE = ("  comfy-qat delete comfy-linux   # the only thing that stops a "
               "reserved box's bill — the box and its disk go too")

# A host list that already holds one reserved box, the way `create --reserve`
# writes it.
WITH_A_RESERVED_BOX = HOSTS + f"""
[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-a"
gce_project  = "{PROJECT}"
gce_reservation = "{RSV}"
port         = 8190
"""


def ceiling(value):
    return {"quotaId": "GPUS-ALL-REGIONS-per-project",
            "dimensionsInfos": [{"details": {"value": str(value)},
                                 "applicableLocations": ["global"]}]}


def grant(value, quota="NVIDIA-L4-GPUS-per-project-region"):
    return {"quotaId": quota,
            "dimensionsInfos": [{"details": {"value": str(value)},
                                 "applicableLocations": REGIONS}]}


def box(name="comfy-linux", *, zone="us-central1-a", status="TERMINATED", bound=RSV,
        card=True):
    """One instance row, as `instances list` returns it."""
    row = {"name": name, "zone": f"{URL}/zones/{zone}", "status": status,
           "disks": [{"boot": True, "source": f"{URL}/zones/{zone}/disks/{name}",
                      "diskSizeGb": "200"}]}
    if card:
        row["guestAccelerators"] = [{"acceleratorType": f"{URL}/nvidia-l4",
                                     "acceleratorCount": 1}]
    if bound:
        row["reservationAffinity"] = {
            "consumeReservationType": "SPECIFIC_RESERVATION",
            "key": "compute.googleapis.com/reservation-name", "values": [bound]}
    return row


def ours(name=RSV, zone="us-central1-a", made_for="comfy-linux"):
    """A reservation this tool made for a box: `comfy-qat: held for <box>`."""
    return reservation_row(name, zone, description=f"comfy-qat: held for {made_for}")


class Project(FakeGcloud):
    """The create tests' fake project, able to be stopped and deleted from too.

    A limit refusal prints `comfy-qat down` and `comfy-qat delete`, and those
    lines are run here — so the same project that refused the create has to
    answer the stop and the delete, and has to be different afterwards. One
    object, one state, across three commands.
    """

    # --- what `down` asks -------------------------------------------------

    def _box(self, name):
        return next((row for row in self._instances if row["name"] == name), None)

    def instance_status(self, name, zone, project):
        self.calls.append("instance_status")
        found = self._box(name)
        if found is None:
            raise GcloudError(f"The resource '{name}' was not found")
        return found["status"]

    def instance_statuses(self, wanted):
        self.calls.append("instance_statuses")
        return statuses_from(wanted, {PROJECT: list(self._instances)})

    def stop_instance(self, name, zone, project):
        self.calls.append("stop_instance")
        self._box(name)["status"] = "TERMINATED"

    # --- what `delete` asks -----------------------------------------------

    def describe_instance(self, name, zone, project):
        self.calls.append("describe_instance")
        return dict(self._box(name) or {})

    def run(self, args, **kwargs):
        joined = " ".join(str(part) for part in args)
        if joined.startswith("compute disks describe"):
            self.calls.append("disks describe")
            return {"sizeGb": "200"}
        if joined.startswith("compute instances delete"):
            self.calls.append("instances delete")
            self._instances = [row for row in self._instances if row["name"] != args[3]]
            return ""
        raise AssertionError(f"the fake was not told to expect: {joined}")


@pytest.fixture
def run(tmp_path, monkeypatch):
    """`comfy-qat <argv>` against one host list that persists across calls.

    The host list is written once — from `declared` on the first call — and
    then left alone, so a second command sees what the first one wrote. That is
    the whole point of running a printed remedy rather than reading it.
    """
    path = tmp_path / "hosts.toml"

    def invoke(*argv, cloud, declared=HOSTS, answer=None, tty=False):
        if not path.exists():
            path.write_text(declared, encoding="utf-8")
        monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: cloud)
        monkeypatch.setattr(gcloud_module, "can_prompt", lambda: tty)
        result = CliRunner().invoke(app, [*argv, "--config", str(path)], input=answer)
        result.hosts = path.read_text(encoding="utf-8")      # type: ignore[attr-defined]
        return result

    return invoke


def flat(text: str) -> str:
    """`text` on one line: `say` wraps prose at 96 columns, and a sentence is
    the same sentence wherever it was broken."""
    return " ".join(text.split())


def mutating(cloud) -> list[str]:
    """Every call this run made that changes something at Google.

    The set comes from `gcloud.py` itself — the methods whose argv carries a
    mutating verb — so a new one cannot be added without being counted here.
    The two deletes this fake answers through the generic `run` are named
    beside it, since that derivation reads methods and not `run`'s argument.
    """
    changing = _mutating_gcloud_methods() | {"instances delete"}
    return [call for call in cloud.calls if call in changing]


def offered(output: str) -> list[str]:
    """Every `comfy-qat ...` line a refusal handed over, in order, as printed."""
    # A fix block whose FIRST line is a command prints it after the label, so
    # the label is taken off: `to fix: comfy-qat create …` is an offer too, and
    # a reader that only saw indented lines never saw that one.
    lines = [line.strip().removeprefix("to fix:").strip() for line in output.splitlines()]
    return [line.split("  #")[0].strip() for line in lines
            if line.startswith("comfy-qat ")]


def test_the_set_of_mutating_calls_is_derived_and_knows_about_reservations():
    """Non-vacuity for `mutating()`: an empty set would make every "no mutating
    call" assertion below pass on a run that made all of them."""
    found = _mutating_gcloud_methods()
    assert {"create_reservation", "delete_reservation",
            "create_instance_from_image", "stop_instance"} <= found, sorted(found)


# --- create --reserve ---------------------------------------------------------


def test_a_reserved_dry_run_makes_no_mutating_call_and_says_what_it_would_cost(run):
    cloud = Project()
    result = run("create", "--os", "linux", "--gpu", "l4", "--reserve", "--dry-run",
                 cloud=cloud)

    assert result.exit_code == 0, result.output
    assert mutating(cloud) == []
    assert "create_reservation" not in cloud.calls
    # And it did read what the plan depends on — a dry run that skipped the
    # reservations read would also "make no mutating call".
    assert cloud.calls.count("list_reservations") == 1
    said = flat(result.stdout)
    assert BILL in said
    assert ("reserve the capacity first: reservation comfy-linux-rsv in "
            "europe-west4-a, which only this box can use") in said
    assert ("reserving takes 1 of the 1 — none left. While comfy-linux exists no "
            "other GPU box can start, including a stopped one you already have."
            ) in said
    assert "--dry-run: nothing created" in result.stdout
    assert result.hosts == HOSTS, "a dry run wrote to the host list"


def test_a_reserved_create_makes_the_reservation_first_in_the_zone_the_box_goes_to(run):
    cloud = Project()
    result = run("create", "--os", "linux", "--gpu", "l4", "--reserve", "--yes",
                 cloud=cloud)

    assert result.exit_code == 0, result.output
    assert mutating(cloud) == ["create_reservation", "create_instance_from_image"]
    name, zone, kwargs = cloud.created
    assert cloud.reserved_with["name"] == RSV
    assert cloud.reserved_with["zone"] == zone == "europe-west4-a"
    assert cloud.reserved_with["machine_type"] == kwargs["machine_type"] == "g2-standard-8"
    assert cloud.reserved_with["description"] == "comfy-qat: held for comfy-linux"
    assert kwargs["reservation"] == RSV, "the box was not bound to its reservation"


def test_a_reserved_box_is_recorded_as_reserved_in_the_host_list(run):
    """The one line every later sentence about this box's bill reads. Without
    it `down` tells its owner the bill has stopped."""
    result = run("create", "--os", "linux", "--gpu", "l4", "--reserve", "--yes",
                 cloud=Project())

    assert f'gce_reservation = "{RSV}"' in result.hosts
    assert "[hosts.comfy-linux]" in result.hosts


def test_a_reserved_create_ends_on_the_bill_and_not_on_stop_paying(run):
    result = run("create", "--os", "linux", "--gpu", "l4", "--reserve", "--yes",
                 cloud=Project())

    ending = result.stdout[result.stdout.index("comfy-linux is up in"):]
    assert BILL in flat(ending)
    assert DELETE_LINE in ending.splitlines()
    assert "stop paying" not in ending and "comfy-qat down" not in ending


def test_the_confirmation_for_a_reserved_box_says_what_a_yes_costs(run):
    """The last line anybody reads before a reservation starts billing. A `no`
    creates nothing — neither half."""
    cloud = Project()
    result = run("create", "--os", "linux", "--gpu", "l4", "--reserve", cloud=cloud,
                 answer="n\n")

    assert ("Create comfy-linux and reserve it? It bills every hour until deleted, "
            "running or stopped.") in result.output
    assert mutating(cloud) == []
    assert "nothing changed" in result.stdout
    assert result.hosts == HOSTS


def test_a_yes_to_that_confirmation_makes_both(run):
    cloud = Project()
    run("create", "--os", "linux", "--gpu", "l4", "--reserve", cloud=cloud, answer="y\n")

    assert mutating(cloud) == ["create_reservation", "create_instance_from_image"]


def test_an_ordinary_create_reserves_nothing_and_reads_the_reservations_once(run):
    """`--reserve` left off in a script: not reserved, and said. The read still
    happens — somebody else's reservation holds the ceiling just the same."""
    cloud = Project()
    result = run("create", "--os", "linux", "--gpu", "l4", "--yes", cloud=cloud)

    assert result.exit_code == 0, result.output
    assert mutating(cloud) == ["create_instance_from_image"]
    assert cloud.calls.count("list_reservations") == 1
    assert "reservation" not in cloud.created[2], "an unreserved box was bound"
    assert "gce_reservation" not in result.hosts
    assert "reserve: no (default — pass --reserve to hold the capacity)" in result.output
    assert "Create comfy-linux?" not in result.output, "--yes does not ask"


def test_a_name_too_long_to_carry_its_reservation_is_refused_before_google_is_asked(run):
    cloud = Project()
    result = run("create", "--os", "linux", "--gpu", "l4", "--reserve",
                 "--name", "a" * 60, "--yes", cloud=cloud)

    assert result.exit_code == 2
    assert cloud.calls == [], "a string check cost a round trip to Google"
    assert "the name of a reserved box may be at most 59" in flat(result.output)


def test_the_same_name_unreserved_is_fine(run):
    """The control: 60 characters is a legal box name. It is only the `-rsv`
    that does not fit."""
    cloud = Project()
    result = run("create", "--os", "linux", "--gpu", "l4", "--name", "a" * 60,
                 "--dry-run", cloud=cloud)

    assert result.exit_code == 0, result.output


# --- the reservation limit ----------------------------------------------------
#
# On a project whose GPUS_ALL_REGIONS is 1, one reserved box is the whole GPU
# allowance for as long as it exists — running or stopped.


def a_project_holding_one_reserved_box(**kwargs) -> Project:
    return Project(instances=[box()], reservations=[ours()], **kwargs)


LIMIT = ("GPUS_ALL_REGIONS is 1 on this project, and 1 of it is held by 1 "
         "reservation: comfy-linux-rsv (us-central1-a). A reservation holds its "
         "card whether its box is running or stopped, so stopping a box frees "
         "nothing, and 1 more is needed. Nothing was created.")


@pytest.mark.parametrize("flags", [("--reserve",), ()], ids=["reserving", "not reserving"])
def test_a_second_gpu_box_is_refused_while_a_reservation_holds_the_ceiling(run, flags):
    """Both kinds. The reserved box is STOPPED here, which is the case the old
    count — running boxes only — would have let straight through."""
    cloud = a_project_holding_one_reserved_box()
    result = run("create", "--os", "linux", "--gpu", "l4", "--name", "second",
                 *flags, "--yes", cloud=cloud, declared=WITH_A_RESERVED_BOX)

    assert result.exit_code == 2, result.output
    assert LIMIT in flat(result.output)
    assert mutating(cloud) == []
    assert "[hosts.second]" not in result.hosts
    # Not the running-box remedy, which cannot work here.
    assert "stop the one you are not using" not in result.output
    assert offered(result.output) == ["comfy-qat down comfy-linux",
                                      "comfy-qat delete comfy-linux"]


def test_the_two_commands_the_limit_refusal_prints_run_and_free_the_card(run):
    """A remedy is output, and output nobody executes is output nobody has
    tested. Pasted back in the order printed: `down` stops the box, `delete`
    releases its reservation, and the create that was refused then goes
    through. If either line could not run, this stops there."""
    cloud = a_project_holding_one_reserved_box()
    cloud._box("comfy-linux")["status"] = "RUNNING"
    refused = run("create", "--os", "linux", "--gpu", "l4", "--name", "second",
                  "--reserve", "--yes", cloud=cloud, declared=WITH_A_RESERVED_BOX)
    assert refused.exit_code == 2

    for line in offered(refused.output):
        again = run(*shlex.split(line)[1:], cloud=cloud, tty=True,
                    answer="comfy-linux\n")
        assert again.exit_code == 0, f"`{line}` did not run:\n{again.output}"

    assert cloud._instances == [] and cloud._reservations == [], (
        "the remedy ran and did not free what it said it would")
    retried = run("create", "--os", "linux", "--gpu", "l4", "--name", "second",
                  "--reserve", "--yes", cloud=cloud)
    assert retried.exit_code == 0, retried.output
    assert "[hosts.second]" in retried.hosts


def test_a_ceiling_of_two_has_room_beside_one_reservation(run):
    """The other side of the same arithmetic. A limit that refused here would
    be a limit of one box, not of the project's allowance."""
    cloud = a_project_holding_one_reserved_box(quotas=[grant(2), ceiling(2)])
    result = run("create", "--os", "linux", "--gpu", "l4", "--name", "second",
                 "--reserve", "--yes", cloud=cloud, declared=WITH_A_RESERVED_BOX)

    assert result.exit_code == 0, result.output
    assert mutating(cloud) == ["create_reservation", "create_instance_from_image"]
    assert "reserving takes 1 of the 2, with 1 already held — none left" in flat(
        result.stdout)


def test_a_reservation_nobody_declared_still_holds_the_ceiling(run):
    """Made in the console, or left by a create that stopped half-way. It is in
    no host list, and it is counted — from the project, never from the file —
    and the remedy is Google's own command, because there is no box for
    `comfy-qat delete` to be given."""
    stray = reservation_row("made-by-hand", "us-central1-a",
                            description="held for the demo")
    cloud = Project(reservations=[stray])
    result = run("create", "--os", "linux", "--gpu", "l4", "--yes", cloud=cloud)

    assert result.exit_code == 2, result.output
    assert mutating(cloud) == []
    assert "held by 1 reservation: made-by-hand (us-central1-a)" in flat(result.output)
    assert offered(result.output) == []
    assert (f"gcloud compute reservations delete made-by-hand --zone=us-central1-a "
            f"--project={PROJECT}") in result.output


# --- the reservations read fails ----------------------------------------------


def test_reserving_is_refused_when_the_reservations_cannot_be_read(run):
    """How many cards are already held is then not known. A refusal is free; a
    reservation made past the limit bills until somebody notices it."""
    cloud = Project(reservations=GcloudError("the Compute Engine API is disabled"))
    result = run("create", "--os", "linux", "--gpu", "l4", "--reserve", "--yes",
                 cloud=cloud)

    assert result.exit_code == 2, result.output
    assert ("could not read this project's reservations (the Compute Engine API is "
            "disabled), so how many it already holds is not known. Nothing was "
            "reserved and nothing was created.") in flat(result.output)
    assert f"gcloud compute reservations list --project={PROJECT}" in result.output
    assert mutating(cloud) == []
    assert result.hosts == HOSTS


def test_an_ordinary_create_goes_on_past_an_unread_reservations_list_and_says_so(run):
    """Not read is not zero — and it is not a reason to refuse a box that
    reserves nothing. It is warned about and the create is left to Google."""
    cloud = Project(reservations=GcloudError("the Compute Engine API is disabled"))
    result = run("create", "--os", "linux", "--gpu", "l4", "--yes", cloud=cloud)

    assert result.exit_code == 0, result.output
    assert mutating(cloud) == ["create_instance_from_image"]
    assert ("warning: could not read this project's reservations (the Compute "
            "Engine API is disabled), so cards held by a reservation are not "
            "counted — Google still refuses a create that does not fit"
            ) in flat(result.stderr)
    assert "warning" not in result.stdout


def test_a_running_box_on_the_ceiling_still_refuses_first_when_the_read_fails(run):
    """A refusal built on something READ beats one built on something missing.
    The box holding the only slot is a fact whether or not the reservations
    could be listed, and it is the refusal with a command that works tonight."""
    running = box("comfy-win", status="RUNNING", bound=None)
    cloud = Project(instances=[running], reservations=GcloudError("denied"))
    result = run("create", "--os", "linux", "--gpu", "l4", "--reserve", "--yes",
                 cloud=cloud)

    assert result.exit_code == 2
    assert "comfy-win is already running on it" in flat(result.output)
    assert "gcloud compute instances stop comfy-win --zone=us-central1-a" in result.output
    assert "could not read this project's reservations" not in result.output
    assert mutating(cloud) == []


def test_a_reservations_read_that_is_empty_refuses_nothing_and_warns_of_nothing(run):
    """Read, and empty: the third answer, and the only one that is quiet."""
    cloud = Project(reservations=[])
    result = run("create", "--os", "linux", "--gpu", "l4", "--reserve", "--dry-run",
                 cloud=cloud)

    assert result.exit_code == 0, result.output
    assert "warning" not in result.output
    assert "could not read" not in result.output
    assert "reserving takes 1 of the 1" in flat(result.stdout)


# --- a reservation an earlier run left behind ---------------------------------


def test_a_rerun_puts_the_box_on_the_reservation_an_interrupted_run_left(run):
    """The first run made the reservation and stopped. The name is the same the
    second time, so it is found — and used, not counted against the ceiling it
    already occupies and not made a second time."""
    cloud = Project(reservations=[ours(zone="us-central1-b")])
    result = run("create", "--os", "linux", "--gpu", "l4", "--reserve", "--yes",
                 cloud=cloud)

    assert result.exit_code == 0, result.output
    assert mutating(cloud) == ["create_instance_from_image"]
    name, zone, kwargs = cloud.created
    assert zone == "us-central1-b", "the box did not go where its reservation is"
    assert kwargs["reservation"] == RSV
    assert "reusing reservation comfy-linux-rsv in us-central1-b" in flat(result.output)
    assert 'gce_zone     = "us-central1-b"' in result.hosts


def test_a_reservation_of_that_name_this_tool_did_not_make_is_not_taken_over(run):
    """Same name, somebody else's description. It is counted like any other
    reservation, and on a ceiling of 1 that alone refuses the create — with
    Google's command for it, not this tool's."""
    stranger = reservation_row(RSV, "us-central1-b", description="made in the console")
    cloud = Project(reservations=[stranger])
    result = run("create", "--os", "linux", "--gpu", "l4", "--reserve", "--yes",
                 cloud=cloud)

    assert result.exit_code == 2, result.output
    assert mutating(cloud) == []
    assert len(cloud._reservations) == 1
    # What it SAID, not only what it did not do: the three assertions above are
    # also true of a build that does not know the flag at all. It was counted as
    # a holder, named where it is, and handed Google's command — not adopted.
    said = flat(result.output)
    assert "1 of it is held by 1 reservation: comfy-linux-rsv (us-central1-b)" in said
    assert (f"gcloud compute reservations delete comfy-linux-rsv "
            f"--zone=us-central1-b --project={PROJECT}") in result.output
    assert "reusing reservation" not in result.output
    assert offered(result.output) == [], "it offered `comfy-qat delete` for a stranger's"


def test_a_leftover_in_another_zone_than_the_one_asked_for_is_refused(run):
    cloud = Project(reservations=[ours(zone="us-central1-b")])
    result = run("create", "--os", "linux", "--gpu", "l4", "--reserve",
                 "--zone", "europe-west4-a", "--yes", cloud=cloud)

    assert result.exit_code == 2, result.output
    assert mutating(cloud) == []
    assert "A reservation cannot be moved" in flat(result.output)
    carry_on = next(line for line in offered(result.output) if "--zone" in line)

    again = run(*shlex.split(carry_on)[1:], "--yes", cloud=cloud)
    assert again.exit_code == 0, f"`{carry_on}` did not run:\n{again.output}"
    assert cloud.created[1] == "us-central1-b"


# --- the box exists and the host list could not be written --------------------


def test_a_reserved_box_that_could_not_be_recorded_says_two_things_are_billing(
        run, tmp_path):
    """The only message printed after money is being spent. For a reserved box
    the stop command is still handed over — and so is the release, because
    stopping the box ends neither bill."""
    cloud = Project()
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    path.chmod(0o444)
    try:
        result = run("create", "--os", "linux", "--gpu", "l4", "--reserve", "--yes",
                     cloud=cloud)
    finally:
        path.chmod(0o644)

    assert result.exit_code == 1, result.output
    said = flat(result.output)
    assert ("comfy-linux exists in europe-west4-a and is billing, and so is its "
            "reservation comfy-linux-rsv — which bills whether the box is running "
            "or stopped") in said
    lines = [line.strip() for line in result.output.splitlines()]
    stop = (f"gcloud compute instances stop comfy-linux --zone=europe-west4-a "
            f"--project={PROJECT}")
    gone = (f"gcloud compute instances delete comfy-linux --zone=europe-west4-a "
            f"--project={PROJECT} --delete-disks=all")
    release = (f"gcloud compute reservations delete comfy-linux-rsv "
               f"--zone=europe-west4-a --project={PROJECT}")
    assert stop in " ".join(lines)
    # ALL of what ends both bills, in the order it has to be run. The fix used
    # to say the reservation is released "after the box is deleted" and print
    # no command that deletes the box.
    assert gone in lines and release in lines
    assert lines.index(gone) < lines.index(release)
    # And the line somebody adding the entry by hand has to add, named — or the
    # entry they write describes a box whose bill `down` would say it stopped.
    assert 'gce_reservation = "comfy-linux-rsv"' in said


# --- a box with no GPU --------------------------------------------------------


def test_reserving_a_box_with_no_gpu_is_refused_before_google_is_asked(run):
    cloud = Project()
    result = run("create", "--os", "linux", "--gpu", "none", "--reserve", "--yes",
                 cloud=cloud)

    assert result.exit_code == 2, result.output
    assert cloud.calls == [], "the refusal read something from Google first"
    assert "a box with no GPU cannot be reserved" in flat(result.output)
    assert result.hosts == HOSTS


def test_both_remedies_that_refusal_prints_run(run):
    """`--gpu none` without the reservation, or a card with it."""
    refused = run("create", "--os", "linux", "--gpu", "none", "--reserve", "--yes",
                  cloud=Project())
    lines = offered(refused.output)
    assert len(lines) == 2, refused.output

    for line in lines:
        cloud = Project(quotas=[grant(1, "NVIDIA-T4-GPUS-per-project-region"),
                                ceiling(1)],
                        accelerators=[{"name": "nvidia-tesla-t4", "zone": zone}
                                      for zone in ZONES])
        again = run(*shlex.split(line)[1:], "--dry-run", cloud=cloud)
        assert again.exit_code == 0, f"`{line}` did not run:\n{again.output}"


def test_a_box_with_no_gpu_is_checked_against_cpu_quota_and_nothing_about_gpus(run):
    cloud = Project()
    result = run("create", "--os", "linux", "--gpu", "none", "--yes", cloud=cloud)

    assert result.exit_code == 0, result.output
    assert "compute_quotas" in cloud.calls
    for never in ("gpu_quotas", "list_reservations", "quota_preferences",
                  "accelerator_types"):
        assert never not in cloud.calls, f"a box with no GPU asked for {never}"
    assert "GPU quota: not used — this box has no GPU" in result.stdout
    name, zone, kwargs = cloud.created
    assert kwargs["machine_type"] == "n1-standard-8"
    assert kwargs["accelerator"] is None
    assert kwargs["terminate_on_maintenance"] is False
    assert "reservation" not in kwargs


def test_a_box_with_no_gpu_is_written_as_none_and_told_of_no_driver(run):
    result = run("create", "--os", "linux", "--gpu", "none", "--yes", cloud=Project())

    assert 'gpu          = "none"' in result.hosts
    assert "gce_reservation" not in result.hosts
    ending = result.stdout[result.stdout.index("comfy-linux is up in"):]
    assert "has no GPU, so there is no driver to wait for" in flat(ending)
    assert "NVIDIA" not in ending
    assert "  comfy-qat down comfy-linux   # stop the machine, stop paying" \
        in ending.splitlines()


def test_a_reservation_holding_the_whole_gpu_ceiling_does_not_refuse_a_box_with_no_gpu(run):
    """The reserved box is the entire GPU allowance, and this needs none of it."""
    cloud = a_project_holding_one_reserved_box()
    result = run("create", "--os", "linux", "--gpu", "none", "--name", "cpu-box",
                 "--yes", cloud=cloud, declared=WITH_A_RESERVED_BOX)

    assert result.exit_code == 0, result.output
    assert mutating(cloud) == ["create_instance_from_image"]
    assert "[hosts.cpu-box]" in result.hosts


def test_cpu_spelled_out_means_the_same_box(run):
    cloud = Project()
    result = run("create", "--os", "linux", "--gpu", "cpu", "--dry-run", cloud=cloud)

    assert result.exit_code == 0, result.output
    assert "no GPU — ComfyUI will run on the CPU" in flat(result.stdout)


# --- discover -----------------------------------------------------------------


def test_discover_adopts_a_reserved_box_with_its_reservation_and_says_what_it_costs(run):
    """Reserved in the console, or by a colleague. Adopted without the field,
    every sentence this tool prints about its bill would be wrong; adopted with
    it, the moment of adoption is the moment to say what that bill is."""
    found = box("their-box", status="TERMINATED", bound="their-rsv")
    found["disks"][0]["licenses"] = [f"{URL}/global/licenses/ubuntu-2204-lts"]
    cloud = Project(instances=[found])
    result = run("discover", cloud=cloud)

    assert result.exit_code == 0, result.output
    assert 'gce_reservation = "their-rsv"' in result.hosts
    assert "reserved (their-rsv)" in result.stdout
    assert ("their-box is reserved. Google holds its capacity and bills for it "
            "every hour — running or stopped — until the box is deleted."
            ) in flat(result.stdout)
    assert ("  comfy-qat delete their-box   # the only thing that stops a reserved "
            "box's bill — the box and its disk go too") in result.stdout.splitlines()


def test_discover_says_nothing_about_reserving_for_a_box_that_is_not(run):
    cloud = Project(instances=[box("plain-box", bound=None)])
    result = run("discover", cloud=cloud)

    assert result.exit_code == 0, result.output
    assert "[hosts.plain-box]" in result.hosts
    assert "gce_reservation" not in result.hosts
    assert "reserved" not in result.stdout


# --- move ---------------------------------------------------------------------


def test_every_line_the_refused_move_of_a_reserved_box_prints_runs(run):
    """`move` refuses a reserved box and prints the three commands that get
    what was wanted instead: stop it, delete it — which releases the
    reservation — and make it again, reserved, where it should be. Run in the
    order printed, against one project, each has to succeed, and the last has
    to leave a reserved box in the zone that was asked for."""
    cloud = a_project_holding_one_reserved_box()
    cloud._box("comfy-linux")["status"] = "RUNNING"
    refused = run("move", "comfy-linux", "--to", "europe-west4-b", cloud=cloud,
                  declared=WITH_A_RESERVED_BOX)

    assert refused.exit_code == 2, refused.output
    assert cloud.calls == [], "the refusal asked Google something first"
    lines = offered(refused.output)
    assert lines == [
        "comfy-qat down comfy-linux",
        "comfy-qat delete comfy-linux",
        "comfy-qat create --os linux --gpu l4 --reserve --name comfy-linux "
        "--zone europe-west4-b",
    ]

    for line in lines:
        again = run(*shlex.split(line)[1:], *(["--yes"] if " create " in line else []),
                    cloud=cloud, tty=" delete " in line, answer="comfy-linux\n")
        assert again.exit_code == 0, f"`{line}` did not run:\n{again.output}"

    assert cloud.created[1] == "europe-west4-b"
    assert cloud.created[2]["reservation"] == RSV
    assert [row["zone"].rsplit("/", 1)[-1] for row in cloud._reservations] == [
        "europe-west4-b"], "the old reservation was not released, or the new not made"
    assert 'gce_zone     = "europe-west4-b"' in again.hosts


# --- the remedy is only for a box the host list holds (audit-v1 F4) -----------
#
# The limit refusal used to print `comfy-qat down <box>` / `comfy-qat delete
# <box>` from the name in the reservation's description — the INSTANCE's name —
# without asking the host list. Two ways that goes wrong, and the second
# destroys a machine.


def test_a_reserved_box_that_is_in_no_host_list_gets_googles_commands_not_ours(run):
    """On the project, bound to its reservation, and not in the host list: a
    create that died before the entry was written, or a teammate's box. Both
    `comfy-qat` commands would exit 2 while the reservation bills."""
    cloud = a_project_holding_one_reserved_box()
    result = run("create", "--os", "linux", "--gpu", "l4", "--name", "second",
                 "--reserve", "--yes", cloud=cloud)          # declared=HOSTS: local only

    assert result.exit_code == 2, result.output
    assert mutating(cloud) == []
    assert offered(result.output) == [], "a comfy-qat command for a box it cannot name"
    lines = [line.strip() for line in result.output.splitlines()]
    box = (f"gcloud compute instances delete comfy-linux --zone=us-central1-a "
           f"--project={PROJECT}")
    release = (f"gcloud compute reservations delete {RSV} --zone=us-central1-a "
               f"--project={PROJECT}")
    assert box in lines and release in lines
    assert lines.index(box) < lines.index(release), "the box goes before its reservation"


NAMESAKE = HOSTS + f"""
[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "my-own-instance"
gce_zone     = "europe-west4-a"
gce_project  = "{PROJECT}"
port         = 8190
"""


def test_an_entry_of_that_name_for_another_machine_is_never_offered_for_deletion(run):
    """THE DATA-LOSS CASE. The host list has an entry called `comfy-linux` that
    points at `my-own-instance`; the reservation was made for an instance
    called `comfy-linux`. The old remedy printed `comfy-qat delete
    comfy-linux`, and pasted back that deleted `my-own-instance` and left the
    reservation billing. So nothing this refusal prints may be a `comfy-qat`
    command — and running everything it DOES offer through this CLI (which is
    nothing) leaves that machine where it was."""
    mine = box("my-own-instance", zone="europe-west4-a", bound=None)
    cloud = Project(instances=[box(), mine], reservations=[ours()],
                    quotas=[grant(2), ceiling(1)])
    result = run("create", "--os", "linux", "--gpu", "l4", "--name", "second",
                 "--reserve", "--yes", cloud=cloud, declared=NAMESAKE)

    assert result.exit_code == 2, result.output
    assert "held by 1 reservation: comfy-linux-rsv" in flat(result.output)
    assert offered(result.output) == [], result.output
    for line in offered(result.output):                       # pasted back: none
        run(*shlex.split(line)[1:], cloud=cloud, tty=True, answer="comfy-linux\n")
    assert cloud._box("my-own-instance") is not None, "the wrong machine was deleted"
    assert "instances delete" not in cloud.calls
    assert "gcloud compute instances delete comfy-linux --zone=us-central1-a" \
        in result.output


def test_a_box_declared_under_another_label_is_named_by_that_label_and_it_runs(run):
    """The entry is called `gpu-box` and points at the instance `comfy-linux`.
    The remedy takes the label, and pasted back it frees the card."""
    declared = WITH_A_RESERVED_BOX.replace("[hosts.comfy-linux]", "[hosts.gpu-box]")
    cloud = a_project_holding_one_reserved_box()
    refused = run("create", "--os", "linux", "--gpu", "l4", "--name", "second",
                  "--reserve", "--yes", cloud=cloud, declared=declared)

    assert offered(refused.output) == ["comfy-qat down gpu-box",
                                       "comfy-qat delete gpu-box"]
    for line in offered(refused.output):
        again = run(*shlex.split(line)[1:], cloud=cloud, tty=True, answer="gpu-box\n")
        assert again.exit_code == 0, f"`{line}` did not run:\n{again.output}"
    assert cloud._instances == [] and cloud._reservations == []


# --- the wrong-region remedy keeps --reserve (audit-v1 F14) -------------------


def test_the_remedy_for_a_region_that_is_not_one_still_reserves(run):
    """`--region US-CENTRAL1`: regions are lower case. The refusal rewrites the
    command, and a rewritten command without `--reserve` makes, in a script, an
    unreserved box. The line it offers has to carry the flag — and, pasted
    back, has to plan a RESERVED box."""
    cloud = Project()
    result = run("create", "--os", "linux", "--gpu", "l4", "--reserve",
                 "--region", "US-CENTRAL1", "--yes", cloud=cloud)

    assert result.exit_code == 2, result.output
    assert "regions are lower case" in result.output
    creates = [line for line in offered(result.output) if line.startswith("comfy-qat create")]
    assert creates == ["comfy-qat create --os linux --gpu l4 --reserve --region us-central1"]
    assert mutating(cloud) == []
    assert "list_reservations" not in cloud.calls, "refused before the limit was read"

    again = run(*shlex.split(creates[0])[1:], "--dry-run", cloud=cloud)
    assert again.exit_code == 0, again.output
    assert "reservation comfy-linux-rsv in us-central1-a" in flat(again.stdout)


def test_the_same_remedy_for_an_ordinary_box_says_nothing_of_reserving(run):
    result = run("create", "--os", "linux", "--gpu", "l4", "--region", "US-CENTRAL1",
                 "--yes", cloud=Project())

    creates = [line for line in offered(result.output) if line.startswith("comfy-qat create")]
    assert creates == ["comfy-qat create --os linux --gpu l4 --region us-central1"]


# --- a box with no GPU: the vCPU check is made, and obeyed (audit-v2 F11) -----


def cpus(per_region, everywhere, regions=None):
    return [
        {"quotaId": "CPUS-per-project-region",
         "dimensionsInfos": [{"details": {"value": str(per_region)},
                              "applicableLocations": list(regions or REGIONS)}]},
        {"quotaId": "CPUS-ALL-REGIONS-per-project",
         "dimensionsInfos": [{"details": {"value": str(everywhere)},
                              "applicableLocations": []}]},
    ]


def test_a_box_with_no_gpu_is_refused_when_the_vcpu_ceiling_is_too_small(run):
    """The DECISION, not the call. `compute_quotas` being read proves nothing
    about what was done with it: a command that read it and threw the answer
    away passes every assertion about calls."""
    cloud = Project(compute=cpus(200, 4))
    result = run("create", "--os", "linux", "--gpu", "none", "--yes", cloud=cloud)

    assert result.exit_code == 2, result.output
    assert ("CPUS_ALL_REGIONS is 4 on this project — the ceiling on vCPU across "
            "every region — and n1-standard-8 needs 8 vCPU. Nothing was created."
            ) in flat(result.output)
    assert mutating(cloud) == []
    assert result.hosts == HOSTS


def test_a_box_with_no_gpu_is_refused_when_no_region_has_room_for_it(run):
    cloud = Project(compute=cpus(4, 32))
    result = run("create", "--os", "linux", "--gpu", "none", "--yes", cloud=cloud)

    assert result.exit_code == 2, result.output
    assert ("n1-standard-8 needs 8 vCPU, and the most this project may hold in "
            "any one region is 4.") in flat(result.output)
    assert mutating(cloud) == []


def test_a_box_with_no_gpu_goes_only_where_the_vcpu_allowance_reaches(run):
    """What the check decided is what the zone order is built from. The nearer
    region has no vCPU allowance here, so the box must not go to it — which it
    would, by latency, if the check's regions were dropped on the way."""
    cloud = Project(compute=cpus(200, 32, regions=["us-central1"]))
    result = run("create", "--os", "linux", "--gpu", "none", "--yes", cloud=cloud)

    assert result.exit_code == 0, result.output
    assert cloud.created[1] == "us-central1-a"
    assert "europe-west4" not in result.stdout[result.stdout.index("zone order"):]
    assert "CPUS (n1): 200 in us-central1" in result.stdout


def test_what_the_vcpu_check_read_is_printed_as_what_it_read(run):
    """And the numbers on screen are the project's, not a default's."""
    result = run("create", "--os", "linux", "--gpu", "none", "--dry-run",
                 cloud=Project(compute=cpus(96, 24)))

    assert "CPUS (n1): 96 in 2 regions — a limit, not what is free" in result.stdout
    assert ("CPUS_ALL_REGIONS (every machine, project-wide): 24 — a limit, not what "
            "is free") in result.stdout


def test_a_box_with_no_gpu_and_a_region_still_reads_nothing_about_gpus(run):
    """`--region` used to bring the accelerator catalogue back in: the region
    check builds its list of regions from where cards are sold. The promise is
    that nothing about GPUs is read for this box, with the flag or without."""
    cloud = Project()
    result = run("create", "--os", "linux", "--gpu", "none", "--region",
                 "us-central1", "--dry-run", cloud=cloud)

    assert result.exit_code == 0, result.output
    for never in ("accelerator_types", "gpu_quotas", "list_reservations",
                  "quota_preferences"):
        assert never not in cloud.calls, f"a box with no GPU asked for {never}"
    assert "compute_quotas" in cloud.calls
    assert "us-central1-a" in result.stdout and "europe-west4" not in result.stdout[
        result.stdout.index("zone order"):]


def test_a_region_outside_the_vcpu_allowance_is_still_refused_for_such_a_box(run):
    """Taking the GPU-catalogue check away must not take the refusal away."""
    cloud = Project()
    result = run("create", "--os", "linux", "--gpu", "none", "--region", "me-west1",
                 "--yes", cloud=cloud)

    assert result.exit_code == 2, result.output
    assert "this project has no CPU quota for n1-standard-8 in me-west1" in flat(
        result.output)
    assert mutating(cloud) == [] and "accelerator_types" not in cloud.calls


# --- discover --prune and a reserved box that is gone (audit-v3 D2) -----------

A_GHOST = HOSTS + f"""
[hosts.plain-ghost]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "plain-ghost"
gce_zone     = "us-central1-a"
gce_project  = "{PROJECT}"
port         = 8191
"""


def test_prune_does_not_drop_a_reserved_box_whose_reservation_may_still_bill(run):
    """The box was deleted in the console; its reservation was not. The entry
    is the last thing on this machine that names that reservation, and
    `comfy-qat delete` — the command that releases it — needs the entry. Prune
    used to remove it without a word about the reservation."""
    cloud = Project(reservations=[ours()])          # the reservation, and no box
    result = run("discover", "--prune", "--yes", cloud=cloud,
                 declared=WITH_A_RESERVED_BOX)

    assert result.exit_code == 0, result.output
    assert "[hosts.comfy-linux]" in result.hosts, "a reserved ghost was pruned"
    said = flat(result.stdout)
    assert ("not on the project any more, and reserved — the box is gone, but its "
            "reservation may still be billing, so the entry was kept:") in said
    assert f"reservation {RSV})" in said
    assert ("  comfy-qat delete comfy-linux   # releases the reservation if it is "
            "still there, and takes the entry out") in result.stdout.splitlines()
    assert len(cloud._reservations) == 1, "prune is not what releases it"


def test_the_command_prune_offers_for_a_reserved_ghost_releases_it(run):
    """Pasted back: the reservation goes, and so does the entry."""
    cloud = Project(reservations=[ours()])
    pruned = run("discover", "--prune", "--yes", cloud=cloud,
                 declared=WITH_A_RESERVED_BOX)
    (line,) = [line for line in offered(pruned.output) if " delete " in line]

    again = run(*shlex.split(line)[1:], cloud=cloud, tty=True, answer="comfy-linux\n")

    assert again.exit_code == 0, again.output
    assert cloud._reservations == [], "the reservation is still billing"
    assert "[hosts.comfy-linux]" not in again.hosts
    assert "Its reservation comfy-linux-rsv was released" in flat(again.stdout)


def test_prune_still_removes_an_ordinary_ghost_beside_a_reserved_one(run):
    """The control, and the mixed case: the unreserved ghost goes exactly as it
    always did, and only the reserved one is kept."""
    declared = WITH_A_RESERVED_BOX + A_GHOST.split(HOSTS)[1]
    cloud = Project(reservations=[ours()])
    result = run("discover", "--prune", "--yes", cloud=cloud, declared=declared)

    assert result.exit_code == 0, result.output
    assert "[hosts.plain-ghost]" not in result.hosts
    assert "[hosts.comfy-linux]" in result.hosts
    assert "removed 1 entry" in result.stdout


def test_prune_with_only_an_ordinary_ghost_says_nothing_of_reservations(run):
    result = run("discover", "--prune", "--yes", cloud=Project(), declared=A_GHOST)

    assert "[hosts.plain-ghost]" not in result.hosts
    assert "reserv" not in result.stdout


def test_a_dry_run_prune_names_the_reserved_ghost_and_writes_nothing(run):
    cloud = Project(reservations=[ours()])
    result = run("discover", "--prune", "--dry-run", cloud=cloud,
                 declared=WITH_A_RESERVED_BOX)

    assert result.hosts == WITH_A_RESERVED_BOX
    assert "comfy-qat delete comfy-linux" in result.stdout
    assert mutating(cloud) == []
