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
    return [line.strip().split("   #")[0].strip() for line in output.splitlines()
            if line.strip().startswith("comfy-qat ")]


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
    assert (f"gcloud compute instances stop comfy-linux --zone=europe-west4-a "
            f"--project={PROJECT}") in " ".join(lines)
    assert (f"gcloud compute reservations delete comfy-linux-rsv "
            f"--zone=europe-west4-a --project={PROJECT}") in lines


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
