"""`inventory` — what `list --live` reads off the project, one read at a time.

Two reads per project, an `instances list` and a `reservations list`, and every
cell of the three cost columns is decided from them: RESERVED, AGE and DISK.
What matters here is not the row where both reads came back; it is the row where
one of them did not, because that is where a column can say something it never
checked.

Three facts are kept apart everywhere below, and each is its own test:

  * Google answered and the thing is there;
  * Google answered and the thing is NOT there;
  * nobody established either.

Nothing here reaches Google. Every payload is a FIXTURE in the shape the SDK's
API schema gives an instance and a reservation (`creationTimestamp`,
`disks[].diskSizeGb`, `reservationAffinity`, `specificReservation`); none is a
recorded live payload, because no reserved box has been made on a real project.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from comfy_qa import inventory
from comfy_qa.config import load
from comfy_qa.gcloud import GONE, GcloudError
from comfy_qa.reservation import parse_all

PROJECT = "stately-timing-504610-p1"
OTHER = "another-project-7"
ZONE = "us-central1-a"
URL = "https://www.googleapis.com/compute/v1/projects"

# The moment every age below is measured from.
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)

HOSTS = f"""\
[hosts.local]
kind = "local"
port = 8188

[hosts.held]
kind            = "gce"
os              = "Ubuntu 22.04"
gpu             = "T4"
gce_instance    = "held"
gce_zone        = "{ZONE}"
gce_project     = "{PROJECT}"
gce_reservation = "held-rsv"
port            = 8191

[hosts.plain]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "plain"
gce_zone     = "{ZONE}"
gce_project  = "{PROJECT}"
port         = 8192

[hosts.cpu]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "none"
gce_instance = "cpu"
gce_zone     = "{ZONE}"
gce_project  = "{PROJECT}"
port         = 8193
"""


def instance(name, *, zone=ZONE, project=PROJECT, status="RUNNING",
             created="2026-10-02T12:00:00.000+00:00", disks=("200",), bound=None):
    row = {"name": name, "zone": f"{URL}/{project}/zones/{zone}", "status": status}
    if created is not None:
        row["creationTimestamp"] = created
    if disks is not None:
        row["disks"] = [{"diskSizeGb": size} for size in disks]
    if bound:
        row["reservationAffinity"] = {
            "consumeReservationType": "SPECIFIC_RESERVATION",
            "key": "compute.googleapis.com/reservation-name",
            "values": [bound],
        }
    return row


def reserved(name, *, zone=ZONE, project=PROJECT, box="", machine="n1-standard-8",
             card="nvidia-tesla-t4"):
    properties: dict = {"machineType": machine}
    if card:
        properties["guestAccelerators"] = [
            {"acceleratorType": card, "acceleratorCount": 1}]
    return {
        "name": name, "zone": f"{URL}/{project}/zones/{zone}", "status": "READY",
        "specificReservationRequired": True,
        "description": f"comfy-qat: held for {box}" if box else "made in the console",
        "specificReservation": {"count": "1", "instanceProperties": properties},
    }


class Cloud:
    """The two reads `survey` makes, per project, and a refusal for anything else."""

    def __init__(self, instances=(), reservations=()):
        self._instances = instances
        self._reservations = reservations
        self.calls: list[tuple[str, str]] = []

    def _answer(self, spec, project):
        answer = spec.get(project, []) if isinstance(spec, dict) else spec
        if isinstance(answer, BaseException):
            raise answer
        return list(answer)

    def list_instances(self, project):
        self.calls.append(("list_instances", project))
        return self._answer(self._instances, project)

    def list_reservations(self, project):
        self.calls.append(("list_reservations", project))
        return self._answer(self._reservations, project)

    def __getattr__(self, item):  # pragma: no cover - the guard, not the path
        if item.startswith("_"):
            raise AttributeError(item)
        raise AssertionError(f"the survey asked the fake for {item!r}")


@pytest.fixture
def hosts(tmp_path):
    def read(text=HOSTS):
        path = tmp_path / "hosts.toml"
        path.write_text(text, encoding="utf-8")
        return load(path)

    return read


def everything():
    """A project where all three boxes are there and one is on its reservation."""
    return Cloud(
        instances=[instance("held", bound="held-rsv", status="TERMINATED"),
                   instance("plain", created="2026-10-05T07:00:00.000+00:00",
                            disks=("200", "50")),
                   instance("cpu", created="2026-10-05T11:40:00.000+00:00")],
        reservations=[reserved("held-rsv", box="held")],
    )


# --- age ---------------------------------------------------------------------


@pytest.mark.parametrize("created, said", [
    ("2026-10-05T11:40:00.000+00:00", "<1h"),
    ("2026-10-05T11:00:00.000+00:00", "1h"),
    ("2026-10-05T07:00:00.000+00:00", "5h"),
    ("2026-10-04T12:30:00.000+00:00", "23h"),
    ("2026-10-04T12:00:00.000+00:00", "1d"),
    ("2026-10-02T12:00:00.000+00:00", "3d"),
])
def test_an_age_is_hours_under_a_day_and_days_after(created, said):
    assert inventory.age(created, NOW) == said


def test_an_age_is_measured_in_the_zone_google_wrote_it_in():
    """Google writes the creation time with the project's own offset. 04:00 at
    -07:00 is 11:00 UTC — an hour ago, not eight."""
    assert inventory.age("2026-10-05T04:00:00.000-07:00", NOW) == "1h"


@pytest.mark.parametrize("created", [None, "", "yesterday", "2026-13-45T99:00:00"])
def test_an_age_nobody_can_read_is_unknown_and_not_new(created):
    """A missing timestamp is not a box made a moment ago. `<1h` would be the
    cheerful reading of a field that was never there."""
    assert inventory.age(created, NOW) == "unknown"


def test_a_box_created_in_the_future_is_not_given_a_negative_age():
    """A clock that disagrees with Google's by a minute is ordinary."""
    assert inventory.age("2026-10-05T12:00:30.000+00:00", NOW) == "<1h"


# --- disk --------------------------------------------------------------------


def test_the_disk_is_the_sum_of_every_disk_on_the_box():
    assert inventory.disk(instance("b", disks=("200", "50"))) == "250 GB"


def test_one_disk_is_its_own_size():
    assert inventory.disk(instance("b", disks=("200",))) == "200 GB"


def test_a_box_that_reports_no_disks_is_unknown_and_not_zero():
    """Absent is not zero. `0 GB` is a statement about the box; a payload with
    no `disks` key is a statement about the payload."""
    assert inventory.disk(instance("b", disks=None)) == "unknown"


def test_a_disk_with_no_readable_size_makes_the_whole_sum_unknown():
    """Half a sum printed as the sum is the under-count nobody can see."""
    assert inventory.disk(instance("b", disks=("200", None))) == "unknown"


# --- what the host list alone says -------------------------------------------


def test_offline_the_reserved_column_is_what_the_host_list_says(hosts):
    by_name = {host.name: host for host in hosts()}
    assert inventory.declared(by_name["held"]) == "yes"
    assert inventory.declared(by_name["plain"]) == "no"
    assert inventory.declared(by_name["cpu"]) == "no"
    assert inventory.declared(by_name["local"]) == "-"


# --- the survey: both reads came back ---------------------------------------


def test_one_instances_read_and_one_reservations_read_per_project(hosts):
    """Three boxes on one project is two calls, not six and not four."""
    cloud = everything()
    inventory.survey(cloud, hosts(), now=NOW)
    assert cloud.calls == [("list_instances", PROJECT),
                           ("list_reservations", PROJECT)]


def test_two_projects_are_two_reads_each(hosts):
    declared = HOSTS.replace(f'gce_project  = "{PROJECT}"\nport         = 8192',
                             f'gce_project  = "{OTHER}"\nport         = 8192')
    cloud = Cloud(instances={PROJECT: [], OTHER: []},
                  reservations={PROJECT: [], OTHER: []})
    inventory.survey(cloud, hosts(declared), now=NOW)
    assert sorted(cloud.calls) == sorted([
        ("list_instances", PROJECT), ("list_reservations", PROJECT),
        ("list_instances", OTHER), ("list_reservations", OTHER)])


def test_a_host_list_with_no_cloud_boxes_asks_google_nothing(hosts):
    cloud = Cloud()
    found = inventory.survey(cloud, hosts('[hosts.local]\nkind = "local"\nport = 8188\n'),
                             now=NOW)
    assert cloud.calls == []
    assert found.rows == {} and found.orphans == () and found.unread == ()


def test_a_local_machine_has_no_row(hosts):
    found = inventory.survey(everything(), hosts(), now=NOW)
    assert "local" not in found.rows


def test_each_box_gets_its_state_its_age_and_its_disk(hosts):
    found = inventory.survey(everything(), hosts(), now=NOW)
    assert found.rows["held"].state == "TERMINATED"
    assert (found.rows["held"].age, found.rows["held"].disk) == ("3d", "200 GB")
    assert found.rows["plain"].state == "RUNNING"
    assert (found.rows["plain"].age, found.rows["plain"].disk) == ("5h", "250 GB")
    assert (found.rows["cpu"].age, found.rows["cpu"].disk) == ("<1h", "200 GB")


def test_a_box_on_its_reservation_is_reserved_and_the_others_are_not(hosts):
    found = inventory.survey(everything(), hosts(), now=NOW)
    assert found.rows["held"].reserved == "yes"
    assert found.rows["plain"].reserved == "no"
    assert found.rows["cpu"].reserved == "no"


def test_a_declared_reservation_that_is_not_on_the_project_is_missing(hosts):
    """The host list says reserved and the project holds no such reservation:
    released by hand, or never made. Neither `yes` nor `no` is true of it."""
    cloud = Cloud(instances=[instance("held", bound="held-rsv"), instance("plain"),
                             instance("cpu")],
                  reservations=[])
    found = inventory.survey(cloud, hosts(), now=NOW)
    assert found.rows["held"].reserved == "missing"


def test_a_reservation_of_the_same_name_in_another_zone_is_not_this_boxs(hosts):
    """A reservation is zonal. One called `held-rsv` in another zone holds
    nothing for a box in this one."""
    cloud = Cloud(instances=[instance("held", bound="held-rsv")],
                  reservations=[reserved("held-rsv", zone="us-east1-b", box="held")])
    found = inventory.survey(cloud, hosts(), now=NOW)
    assert found.rows["held"].reserved == "missing"


def test_a_box_bound_to_a_reservation_the_host_list_does_not_name_says_so(hosts):
    """Reserved in the console, or adopted before this field existed. The bill is
    real and the host list is silent about it, so every money sentence that
    reads the host list is wrong about this box until the entry says so."""
    cloud = Cloud(instances=[instance("plain", bound="made-by-hand")],
                  reservations=[reserved("made-by-hand")])
    found = inventory.survey(cloud, hosts(), now=NOW)
    assert found.rows["plain"].reserved == "yes (not in host list)"


def test_a_box_the_project_does_not_have_has_no_age_and_no_disk(hosts):
    cloud = Cloud(instances=[instance("plain")], reservations=[])
    found = inventory.survey(cloud, hosts(), now=NOW)
    assert found.rows["cpu"].state == GONE
    assert (found.rows["cpu"].age, found.rows["cpu"].disk) == ("-", "-")
    assert found.rows["cpu"].reserved == "no"


def test_a_deleted_box_whose_reservation_is_still_there_is_still_reserved(hosts):
    """The case that bills with nothing to show for it: the box went and its
    reservation did not. `no` here would be an all-clear about a live bill."""
    cloud = Cloud(instances=[], reservations=[reserved("held-rsv", box="held")])
    found = inventory.survey(cloud, hosts(), now=NOW)
    assert found.rows["held"].state == GONE
    assert found.rows["held"].reserved == "yes"


# --- the survey: one read did not come back ----------------------------------


def test_a_failed_instances_read_is_unknown_and_never_gone(hosts):
    """`GONE` is the word `discover --prune` deletes on. A read that failed
    established nothing, and the three cost columns say so."""
    cloud = Cloud(instances=GcloudError("permission denied"),
                  reservations=[reserved("held-rsv", box="held")])
    found = inventory.survey(cloud, hosts(), now=NOW)
    for name in ("held", "plain", "cpu"):
        assert found.rows[name].state == ""
        assert found.rows[name].age == "unknown"
        assert found.rows[name].disk == "unknown"
    assert found.rows["held"].reserved == "yes, unchecked"
    assert found.rows["plain"].reserved == "no, unchecked"


def test_a_failed_instances_read_still_reads_the_reservations(hosts):
    cloud = Cloud(instances=GcloudError("permission denied"), reservations=[])
    inventory.survey(cloud, hosts(), now=NOW)
    assert ("list_reservations", PROJECT) in cloud.calls


def test_a_failed_reservations_read_says_unchecked_and_names_the_project(hosts):
    cloud = Cloud(instances=[instance("held", bound="held-rsv"), instance("plain"),
                             instance("cpu")],
                  reservations=GcloudError("the reservations API is disabled"))
    found = inventory.survey(cloud, hosts(), now=NOW)
    assert found.rows["held"].reserved == "yes, unchecked"
    assert found.rows["plain"].reserved == "no, unchecked"
    # The other two columns came off the read that worked.
    assert (found.rows["plain"].age, found.rows["plain"].disk) == ("3d", "200 GB")
    assert found.unread == ((PROJECT, "the reservations API is disabled"),)


def test_a_box_bound_but_undeclared_is_still_yes_when_reservations_are_unread(hosts):
    """Its own record says it is bound. That much was read."""
    cloud = Cloud(instances=[instance("plain", bound="made-by-hand")],
                  reservations=GcloudError("denied"))
    found = inventory.survey(cloud, hosts(), now=NOW)
    assert found.rows["plain"].reserved == "yes, unchecked"


def test_a_reservations_read_that_worked_and_is_empty_is_not_unread(hosts):
    """Empty is an answer. `[]` and a failed read are the two facts this module
    exists to keep apart."""
    cloud = Cloud(instances=[instance("plain")], reservations=[])
    found = inventory.survey(cloud, hosts(), now=NOW)
    assert found.unread == ()
    assert found.rows["plain"].reserved == "no"


def test_one_unreadable_project_does_not_cost_the_other_its_answers(hosts):
    declared = HOSTS.replace(f'gce_project  = "{PROJECT}"\nport         = 8192',
                             f'gce_project  = "{OTHER}"\nport         = 8192')
    cloud = Cloud(instances={PROJECT: GcloudError("denied"), OTHER: [instance(
                      "plain", project=OTHER)]},
                  reservations={PROJECT: GcloudError("denied"), OTHER: []})
    found = inventory.survey(cloud, hosts(declared), now=NOW)
    assert found.rows["plain"].state == "RUNNING"
    assert found.rows["plain"].reserved == "no"
    assert found.rows["held"].state == ""


def test_the_survey_never_raises_whatever_the_reads_do(hosts):
    """`list` never exits non-zero for a failed live read."""
    cloud = Cloud(instances=GcloudError("down"), reservations=GcloudError("down"))
    found = inventory.survey(cloud, hosts(), now=NOW)
    assert set(found.rows) == {"held", "plain", "cpu"}


# --- reservations with no box ------------------------------------------------


def test_a_reservation_no_box_is_bound_to_is_an_orphan(hosts):
    cloud = Cloud(instances=[instance("plain")],
                  reservations=[reserved("qatest-rsv", box="qatest")])
    found = inventory.survey(cloud, hosts(), now=NOW)
    assert [(project, entry.name) for project, entry in found.orphans] == [
        (PROJECT, "qatest-rsv")]


def test_a_reservation_with_its_box_on_it_is_not_an_orphan_even_stopped(hosts):
    cloud = Cloud(instances=[instance("held", bound="held-rsv", status="TERMINATED")],
                  reservations=[reserved("held-rsv", box="held")])
    assert inventory.survey(cloud, hosts(), now=NOW).orphans == ()


def test_a_reservation_somebody_elses_box_is_on_is_not_an_orphan(hosts):
    """Bound to a box that is in nobody's host list here. It has a box, and
    handing over its delete command would be handing over somebody's capacity."""
    cloud = Cloud(instances=[instance("stranger", bound="theirs")],
                  reservations=[reserved("theirs")])
    assert inventory.survey(cloud, hosts(), now=NOW).orphans == ()


def test_no_orphans_are_claimed_when_the_instances_could_not_be_read(hosts):
    """"Has no box" needs the boxes. Without them every reservation on the
    project would be called an orphan and handed a delete command."""
    cloud = Cloud(instances=GcloudError("denied"),
                  reservations=[reserved("held-rsv", box="held")])
    assert inventory.survey(cloud, hosts(), now=NOW).orphans == ()


def test_the_orphan_block_names_it_and_hands_over_googles_own_delete(hosts):
    cloud = Cloud(instances=[], reservations=[reserved("qatest-rsv", box="qatest")])
    lines = inventory.orphan_lines(inventory.survey(cloud, hosts(), now=NOW))
    assert lines == [
        f"1 reservation on {PROJECT} has no box, and is billing:",
        f"  qatest-rsv in {ZONE} (T4)",
        f"  gcloud compute reservations delete qatest-rsv --zone={ZONE} "
        f"--project={PROJECT}",
    ]


def test_two_orphans_are_counted_as_two_and_each_gets_its_command(hosts):
    cloud = Cloud(instances=[], reservations=[
        reserved("a-rsv", box="a"),
        reserved("b-rsv", box="b", machine="g2-standard-8", card="")])
    lines = inventory.orphan_lines(inventory.survey(cloud, hosts(), now=NOW))
    assert lines[0] == f"2 reservations on {PROJECT} have no box, and are billing:"
    assert f"  b-rsv in {ZONE} (L4)" in lines
    assert sum(line.startswith("  gcloud compute reservations delete") for line in lines) == 2


def test_an_orphan_whose_card_the_table_does_not_know_is_named_by_its_machine(hosts):
    cloud = Cloud(instances=[], reservations=[
        reserved("odd-rsv", machine="n2-standard-4", card="")])
    lines = inventory.orphan_lines(inventory.survey(cloud, hosts(), now=NOW))
    assert f"  odd-rsv in {ZONE} (n2-standard-4)" in lines


def test_no_orphans_is_no_block_at_all(hosts):
    assert inventory.orphan_lines(inventory.survey(everything(), hosts(), now=NOW)) == []


def test_the_shape_of_a_reservation_is_read_through_the_shared_parser():
    """`shape` takes what `reservation.parse_all` returns, so a record this
    module names is a record the limit counted the same way."""
    (found,) = parse_all([reserved("x-rsv")])
    assert inventory.shape(found) == "T4"


def test_reservations_whose_boxes_could_not_be_read_are_counted_not_dropped(hosts):
    """No orphan is NAMED when the instances could not be read — and that
    silence must not be the whole of it. The survey says how many reservations
    it could not sort into "has a box" and "has none", by project."""
    cloud = Cloud(instances=GcloudError("denied"),
                  reservations=[reserved("held-rsv", box="held"),
                                reserved("stray-rsv", box="stray")])
    found = inventory.survey(cloud, hosts(), now=NOW)

    assert found.orphans == ()
    assert found.unsorted == ((PROJECT, 2),)


def test_nothing_is_unsorted_when_both_reads_worked_or_nothing_is_reserved(hosts):
    assert inventory.survey(everything(), hosts(), now=NOW).unsorted == ()
    none_reserved = Cloud(instances=GcloudError("denied"), reservations=[])
    assert inventory.survey(none_reserved, hosts(), now=NOW).unsorted == ()


# --- one zone helper -----------------------------------------------------------


def test_the_zone_of_a_record_is_read_by_the_one_helper():
    """`reservation.zone_of` is where "the bare zone off a record or a URL" is
    written. `inventory`, `remove` and `host` each had a private copy of the
    same line, and `relocate` reached into `reservation._outside`; a copy is a
    place the next change to that line does not reach."""
    import inspect

    from comfy_qa import host, inventory, relocate, remove

    for module in (inventory, remove, host):
        source = inspect.getsource(module)
        assert "def _zone(" not in source, module.__name__
        assert "def _tail_zone(" not in source, module.__name__
        assert "rsv.zone_of(" in source, module.__name__
    moving = inspect.getsource(relocate)
    assert "rsv._outside" not in moving
    assert "rsv.outside(" in moving
