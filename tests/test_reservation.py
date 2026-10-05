"""The reservation model: what Google's records mean, counted in cards.

`comfy_qa/reservation.py` makes no call and prints nothing. It turns the payload
of `gcloud compute reservations list` into the few facts the rest of the tool
asks about — whose is it, how many cards does it hold, is anything using it —
and holds the two sentences about a reserved box's bill.

Three things here are the reason the file exists, and each has a test that a
homogeneous fixture could not reach:

* **cards, not boxes and not reservations.** An H100 reservation holds eight of
  the project's allowance; a G2 one holds a card Google may not list at all.
* **absent is not zero.** `inUseCount` missing is "not reported", and a
  reservations read that never happened (`None`) is not a project with no
  reservations (`[]`).
* **a card is counted once.** A reserved box that is running holds ONE card,
  through its reservation — not one for the reservation and one for the box.

EVERY PAYLOAD BELOW IS A FIXTURE. The shapes follow the SDK's own API schema
(`specificReservation.count`, `.inUseCount`, `.instanceProperties`,
`reservationAffinity`); none was read from a live project, and names like
`us-central1-a` say nothing about what that zone holds.
"""

from __future__ import annotations

import ast
import dataclasses
import inspect

import pytest

from comfy_qa import create, reservation
from comfy_qa.reservation import Reservation

PROJECT = "proj"
ABSENT = object()


def zone_url(zone: str) -> str:
    return f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/zones/{zone}"


def record(name="comfy-linux-rsv", zone="us-central1-a", *,
           machine_type="n1-standard-8", accelerators=ABSENT, count="1",
           in_use=ABSENT, status="READY", description=ABSENT, specific=True):
    """One row of `reservations list`, able to LEAVE OUT what Google leaves out.

    A builder that writes every key unconditionally cannot make the shape the
    absent-versus-zero rule is about, so every optional field has a way to be
    missing: `ABSENT` means the key is not in the payload at all.

    int64 fields arrive from gcloud as STRINGS — `"count": "1"` — which is why
    the defaults here are strings and not numbers.
    """
    properties = {"machineType": machine_type}
    if accelerators is not ABSENT:
        properties["guestAccelerators"] = [
            {"acceleratorType": kind, "acceleratorCount": number}
            for kind, number in accelerators]
    specific_part = {"instanceProperties": properties}
    if count is not ABSENT:
        specific_part["count"] = count
    if in_use is not ABSENT:
        specific_part["inUseCount"] = in_use
    row = {
        "name": name,
        "zone": zone_url(zone),
        "status": status,
        "specificReservationRequired": specific,
        "creationTimestamp": "2026-10-05T09:00:00.000-07:00",
        "specificReservation": specific_part,
    }
    if description is not ABSENT:
        row["description"] = description
    return row


T4 = (("nvidia-tesla-t4", 1),)
L4 = (("nvidia-l4", 1),)
H100 = (("nvidia-h100-80gb", 8),)


def box(name, zone="us-central1-a", *, status="RUNNING", accelerators=T4,
        bound=None, affinity="SPECIFIC_RESERVATION"):
    """One row of `instances list`. `bound` is the reservation it targets."""
    row = {"name": name, "zone": zone_url(zone), "status": status}
    if accelerators:
        row["guestAccelerators"] = [
            {"acceleratorType": f"{zone_url(zone)}/acceleratorTypes/{kind}",
             "acceleratorCount": number}
            for kind, number in accelerators]
    if bound is not None:
        row["reservationAffinity"] = {
            "consumeReservationType": affinity,
            "key": "compute.googleapis.com/reservation-name",
            "values": [bound],
        }
    return row


def parsed(*rows) -> list[Reservation]:
    return reservation.parse_all(list(rows))


def one(**fields) -> Reservation:
    return parsed(record(**fields))[0]


# --- names and the two money sentences ---------------------------------------


def test_the_reservation_is_named_after_its_box():
    assert reservation.SUFFIX == "-rsv"
    assert reservation.name_for("comfy-linux") == "comfy-linux-rsv"


def test_the_description_is_what_marks_a_reservation_as_ours():
    assert reservation.MARK == "comfy-qat:"
    assert reservation.describe_for("comfy-linux") == "comfy-qat: held for comfy-linux"
    assert reservation.describe_for("comfy-linux").startswith(reservation.MARK)


def test_the_longest_box_name_still_makes_a_legal_reservation_name():
    """Google's names stop at 63 characters, and the suffix takes four of them.

    Asserted as a relationship as well as a number: 59 on its own would go on
    passing if the suffix grew.
    """
    assert reservation.MAX_BOX_NAME == 59
    assert len(reservation.name_for("x" * reservation.MAX_BOX_NAME)) == create.MAX_NAME_LEN


def test_the_bill_sentence_is_the_one_the_design_fixes():
    """Literal, typed here — the oracle for a money sentence is never the code."""
    assert reservation.bill("comfy-linux") == (
        "comfy-linux is reserved. Google holds its capacity and bills for it "
        "every hour — running or stopped — until the box is deleted."
    )


def test_the_stop_line_names_delete_and_not_down():
    line = reservation.stop_line("comfy-linux")

    assert line == (
        "  comfy-qat delete comfy-linux   # the only thing that stops a "
        "reserved box's bill — the box and its disk go too"
    )
    assert "comfy-qat down" not in line, (
        "`down` does not stop a reserved box's bill, and the tokens that clear "
        "an unreserved command must not appear in the reserved stop line"
    )


def test_the_delete_command_is_googles_own_and_complete():
    assert reservation.delete_command("qatest-rsv", "us-central1-a", "proj") == (
        "gcloud compute reservations delete qatest-rsv --zone=us-central1-a "
        "--project=proj"
    )


# --- reading Google's records ------------------------------------------------


def test_a_full_record_is_read_field_by_field():
    found = one(accelerators=(
        ("https://www.googleapis.com/compute/v1/projects/proj/zones/"
         "us-central1-a/acceleratorTypes/nvidia-tesla-t4", 1),),
        in_use="1", description="comfy-qat: held for comfy-linux")

    assert found == Reservation(
        name="comfy-linux-rsv",
        zone="us-central1-a",
        machine_type="n1-standard-8",
        accelerator="nvidia-tesla-t4",
        accelerator_count=1,
        vm_count=1,
        in_use=1,
        status="READY",
        specific=True,
        description="comfy-qat: held for comfy-linux",
        created="2026-10-05T09:00:00.000-07:00",
    )


def test_a_machine_type_given_as_a_url_is_read_by_its_tail():
    found = one(machine_type=f"{zone_url('us-central1-a')}/machineTypes/g2-standard-8")

    assert found.machine_type == "g2-standard-8"


def test_in_use_absent_is_not_in_use_zero():
    """Google leaves fields out. "Not reported" must stay tellable from "none"."""
    assert one(in_use=ABSENT).in_use is None
    assert one(in_use="0").in_use == 0
    assert one(in_use=0).in_use == 0
    assert one(in_use="1").in_use == 1


def test_a_count_that_cannot_be_read_is_not_in_use_zero():
    assert one(in_use="soon").in_use is None


@pytest.mark.parametrize("row", [
    {},
    {"name": "bare"},
    {"name": "half", "specificReservation": {}},
    {"name": "nulls", "zone": None, "specificReservation": None, "description": None},
    {"name": "odd", "specificReservation": {"instanceProperties": {
        "guestAccelerators": [{}]}}},
], ids=lambda row: row.get("name", "empty"))
def test_a_record_missing_keys_is_still_read(row):
    """`parse_all` never raises and never drops a row for being thin.

    Dropping it would be the quiet bug: a reservation the tool could not fully
    read still holds whatever it holds.
    """
    found = reservation.parse_all([row])

    assert len(found) == 1
    assert found[0].name == row.get("name", "")
    assert found[0].in_use is None
    assert found[0].zone == ""


def test_something_that_is_not_a_record_is_not_a_reservation():
    found = reservation.parse_all([record(name="real"), "noise", None, 7])

    assert [r.name for r in found] == ["real"]


def test_every_record_is_kept_and_in_order():
    rows = [record(name=f"r{i}-rsv") for i in range(3)]

    assert [r.name for r in reservation.parse_all(rows)] == ["r0-rsv", "r1-rsv", "r2-rsv"]


def test_a_count_of_vms_is_read_as_a_number_whichever_way_it_arrives():
    assert one(count="3").vm_count == 3
    assert one(count=3).vm_count == 3


def test_a_reservation_with_no_count_reported_still_holds_one_vm():
    """Absent is not zero here either — and guessing low is what costs money."""
    assert one(count=ABSENT).vm_count == 1
    assert one(count="0").vm_count == 0


# --- whose it is -------------------------------------------------------------


def test_ours_is_decided_by_the_description_and_nothing_else():
    assert one(description="comfy-qat: held for comfy-linux").ours is True
    assert one(description="held for the render team").ours is False
    assert one(description=ABSENT).ours is False
    # The name alone proves nothing: anybody can call a reservation `x-rsv`.
    assert one(name="comfy-linux-rsv", description="made in the console").ours is False


def test_the_box_is_read_out_of_our_own_description():
    assert one(description="comfy-qat: held for comfy-linux").box == "comfy-linux"
    assert one(description="comfy-qat: held for qatest-2 ").box == "qatest-2"


def test_a_reservation_that_is_not_ours_names_no_box():
    """Its description is somebody else's sentence, and a `comfy-qat down <box>`
    built from it would be a remedy that cannot work."""
    assert one(description="held for the render team").box == ""
    assert one(description="comfy-qat: something else").box == ""
    assert one(description=ABSENT).box == ""


# --- which reservation a box is bound to ------------------------------------


def test_a_box_bound_to_a_specific_reservation_names_it():
    assert reservation.bound_to(box("comfy-linux", bound="comfy-linux-rsv")) == "comfy-linux-rsv"


def test_a_reservation_given_as_a_path_is_named_by_its_tail():
    bound = box("comfy-linux", bound="projects/proj/reservations/comfy-linux-rsv")

    assert reservation.bound_to(bound) == "comfy-linux-rsv"


@pytest.mark.parametrize("instance", [
    box("plain"),
    box("any", bound="comfy-linux-rsv", affinity="ANY_RESERVATION"),
    box("none", bound="comfy-linux-rsv", affinity="NO_RESERVATION"),
    {"name": "empty-affinity", "reservationAffinity": {}},
    {"name": "no-values", "reservationAffinity": {
        "consumeReservationType": "SPECIFIC_RESERVATION"}},
    {"name": "null-affinity", "reservationAffinity": None},
], ids=lambda instance: instance["name"])
def test_anything_else_is_bound_to_nothing(instance):
    assert reservation.bound_to(instance) is None


# --- cards -------------------------------------------------------------------


def test_a_t4_reservation_holds_one_card():
    assert one(accelerators=T4).cards == 1


def test_an_h100_reservation_holds_eight_cards_not_one_box():
    """GPUS_ALL_REGIONS is metered in cards. Counting this as one passes a
    ceiling of 8 that Google then refuses."""
    assert one(machine_type="a3-highgpu-8g", accelerators=H100).cards == 8


def test_an_h100_reservation_google_lists_no_card_for_still_holds_eight():
    """The built-in-card case: whether the read-back lists `guestAccelerators`
    is not settled, so the count comes from the card table when it does not."""
    found = one(machine_type="a3-highgpu-8g", accelerators=ABSENT)

    assert found.accelerator == ""
    assert found.cards == create.CARDS["h100"].count == 8


def test_a_g2_reservation_with_no_card_listed_still_holds_one():
    found = one(machine_type="g2-standard-8", accelerators=ABSENT)

    assert found.accelerator_count == 0, "the fixture must reach the no-card-listed branch"
    assert found.cards == 1


@pytest.mark.parametrize("machine_type", [
    "g2-standard-96", "a2-highgpu-4g", "a3-megagpu-8g", "g4-standard-48", "a4-highgpu-8g",
])
def test_a_gpu_machine_this_tool_has_no_row_for_counts_one_not_zero(machine_type):
    """Fail closed: an unfamiliar GPU machine type is not an empty machine."""
    assert all(card.machine_type != machine_type for card in create.CARDS.values()), (
        "the fixture must be a machine type the card table does not know")
    assert one(machine_type=machine_type, accelerators=ABSENT).cards == 1


def test_a_reservation_with_no_gpu_holds_no_card():
    """`n1-standard-8` is the T4's machine type too — and without a card listed
    it is a CPU reservation, which spends none of the GPU allowance."""
    assert one(machine_type="n1-standard-8", accelerators=ABSENT).cards == 0
    assert one(machine_type="e2-standard-8", accelerators=ABSENT).cards == 0


def test_a_listed_card_with_an_unreadable_count_is_one_card_not_none():
    assert one(accelerators=(("nvidia-tesla-t4", None),)).cards == 1
    assert one(accelerators=(("nvidia-tesla-t4", "0"),)).cards == 1


def test_a_listed_card_is_read_as_at_least_one():
    """The reading and the counting each floor at one, and each needs its own
    observable: this is the reading's. Without it either floor can be deleted
    and the test above stays green on the other."""
    assert one(accelerators=(("nvidia-tesla-t4", None),)).accelerator_count == 1
    assert one(accelerators=(("nvidia-tesla-t4", "0"),)).accelerator_count == 1
    assert one(accelerators=(("nvidia-tesla-t4", "4"),)).accelerator_count == 4


def test_a_card_with_no_count_at_all_is_still_counted_as_one():
    """...and this is the counting's: a record that names a card and carries a
    count of zero, however it came to be built."""
    found = dataclasses.replace(one(accelerators=T4), accelerator_count=0)

    assert found.accelerator == "nvidia-tesla-t4"
    assert found.cards == 1


def test_cards_multiply_by_the_number_of_vms_held():
    assert one(accelerators=T4, count="3").cards == 3
    assert one(machine_type="a3-highgpu-8g", accelerators=H100, count="2").cards == 16


def test_cards_reserved_adds_every_reservation_up():
    held = parsed(
        record(name="a-rsv", accelerators=T4),
        record(name="b-rsv", machine_type="a3-highgpu-8g", accelerators=H100),
        record(name="c-rsv", machine_type="g2-standard-8", accelerators=ABSENT),
        record(name="cpu-rsv", machine_type="n1-standard-8", accelerators=ABSENT),
    )

    assert reservation.cards_reserved(held) == 1 + 8 + 1 + 0
    assert reservation.cards_reserved([]) == 0


# --- the ceiling arithmetic --------------------------------------------------


def test_a_reserved_box_that_is_running_holds_its_card_once():
    """The reservation and the box consuming it are ONE card."""
    held = parsed(record(accelerators=T4, in_use="1"))
    instances = [box("comfy-linux", bound="comfy-linux-rsv")]

    assert reservation.cards_held(instances, held) == 1


def test_a_reserved_box_that_is_stopped_still_holds_its_card():
    """Stopping a reserved box frees nothing — the reservation is what holds."""
    held = parsed(record(accelerators=T4, in_use="0"))
    instances = [box("comfy-linux", bound="comfy-linux-rsv", status="TERMINATED")]

    assert create._cards_running(instances) == 0, "the box itself must be spending nothing"
    assert reservation.cards_held(instances, held) == 1


def test_an_unreserved_running_box_is_added_to_the_reserved_cards():
    """The mixed case, which is the only one that occurs live."""
    held = parsed(record(accelerators=T4))
    instances = [
        box("comfy-linux", bound="comfy-linux-rsv"),
        box("comfy-win", zone="us-east4-a", accelerators=L4),
        box("parked", accelerators=T4, status="TERMINATED"),
        box("cpu-only", accelerators=()),
    ]

    assert reservation.cards_held(instances, held) == 2


def test_a_running_h100_box_adds_eight():
    instances = [box("big", accelerators=H100)]

    assert reservation.cards_held(instances, []) == 8


def test_reservations_not_read_is_not_reservations_read_and_empty():
    """`None` is "nobody looked"; `[]` is "looked, none". Neither raises, and
    with nothing reserved they agree — on the running cards, every one of them."""
    instances = [box("comfy-linux", bound="comfy-linux-rsv"), box("other")]

    assert reservation.cards_held(instances, None) == create._cards_running(instances) == 2
    assert reservation.cards_held([], []) == 0
    assert reservation.cards_held([], None) == 0


def test_not_reading_the_reservations_cannot_count_a_stopped_reserved_box():
    """The undercount that `None` stands for, pinned so nobody reads it as zero:
    the same project is 1 with the reservations read and 0 without."""
    instances = [box("comfy-linux", bound="comfy-linux-rsv", status="TERMINATED")]
    held = parsed(record(accelerators=T4))

    assert reservation.cards_held(instances, held) == 1
    assert reservation.cards_held(instances, None) == 0


def test_a_box_bound_to_a_reservation_nobody_listed_still_counts():
    """A reservation deleted from under a running box, or one that is not in
    this listing: the card is spending and nothing else here would count it."""
    instances = [box("comfy-linux", bound="gone-rsv")]

    assert reservation.cards_held(instances, []) == 1


def test_a_same_named_reservation_in_another_zone_does_not_cover_the_box():
    """Names are only unique within a zone, so this is two cards, not one."""
    held = parsed(record(zone="europe-west4-a", accelerators=T4))
    instances = [box("comfy-linux", zone="us-central1-a", bound="comfy-linux-rsv")]

    assert reservation.cards_held(instances, held) == 2


def test_cards_held_takes_none_for_instances_too():
    assert reservation.cards_held(None, parsed(record(accelerators=T4))) == 1


# --- one card in one region --------------------------------------------------


def test_held_in_counts_one_card_in_one_region():
    held = parsed(
        record(name="t4-rsv", zone="us-central1-a", accelerators=T4),
        record(name="l4-rsv", zone="us-central1-b", machine_type="g2-standard-8",
               accelerators=L4),
        record(name="far-rsv", zone="europe-west4-a", accelerators=T4),
    )
    instances = [
        box("bound", zone="us-central1-a", bound="t4-rsv"),
        box("loose", zone="us-central1-c", accelerators=T4),
        box("loose-l4", zone="us-central1-c", accelerators=L4),
        box("far", zone="europe-west4-b", accelerators=T4),
        box("parked", zone="us-central1-f", accelerators=T4, status="TERMINATED"),
    ]

    assert reservation.held_in("us-central1", "nvidia-tesla-t4", instances, held) == 2
    assert reservation.held_in("us-central1", "nvidia-l4", instances, held) == 2
    assert reservation.held_in("europe-west4", "nvidia-tesla-t4", instances, held) == 2
    assert reservation.held_in("europe-west4", "nvidia-l4", instances, held) == 0
    assert reservation.held_in("asia-east1", "nvidia-tesla-t4", instances, held) == 0


def test_held_in_is_not_fooled_by_a_region_that_is_a_prefix_of_another():
    held = parsed(record(zone="us-east1-b", accelerators=T4))

    assert reservation.held_in("us-east1", "nvidia-tesla-t4", [], held) == 1
    assert reservation.held_in("us-east", "nvidia-tesla-t4", [], held) == 0


def test_held_in_knows_a_built_in_card_google_did_not_list():
    """A G2 reservation with no `guestAccelerators` is still an L4 in that
    region — read from the card table, like its count."""
    held = parsed(record(machine_type="g2-standard-8", accelerators=ABSENT))

    assert reservation.held_in("us-central1", "nvidia-l4", [], held) == 1
    assert reservation.held_in("us-central1", "nvidia-tesla-t4", [], held) == 0


def test_held_in_counts_cards_not_boxes():
    held = parsed(record(machine_type="a3-highgpu-8g", accelerators=ABSENT))
    instances = [box("big", accelerators=H100)]

    assert reservation.held_in("us-central1", "nvidia-h100-80gb", instances, held) == 16


def test_held_in_without_the_reservations_read_counts_what_is_running():
    instances = [box("bound", bound="t4-rsv"), box("loose")]

    assert reservation.held_in("us-central1", "nvidia-tesla-t4", instances, None) == 2
    assert reservation.held_in("us-central1", "nvidia-tesla-t4", instances, []) == 2


# --- who holds what ----------------------------------------------------------


def test_holders_names_each_reservation_that_holds_a_card():
    held = parsed(
        record(name="comfy-linux-rsv", accelerators=T4,
               description="comfy-qat: held for comfy-linux"),
        record(name="console-rsv", zone="us-east4-a", machine_type="a3-highgpu-8g",
               accelerators=H100, description="made by hand"),
        record(name="cpu-rsv", machine_type="n1-standard-8", accelerators=ABSENT),
    )

    assert reservation.holders(held) == (
        ("comfy-linux-rsv", "us-central1-a", 1, "comfy-linux"),
        ("console-rsv", "us-east4-a", 8, ""),
    )
    assert reservation.holders([]) == ()


# --- a reservation left by an earlier run -------------------------------------


def test_the_leftover_is_the_one_named_for_this_box():
    held = parsed(record(name="other-rsv"), record(name="comfy-linux-rsv"),
                  record(name="comfy-linux-2-rsv"))

    assert reservation.leftover_for("comfy-linux", held).name == "comfy-linux-rsv"
    assert reservation.leftover_for("comfy-linux-2", held).name == "comfy-linux-2-rsv"
    assert reservation.leftover_for("comfy", held) is None
    assert reservation.leftover_for("comfy-linux", []) is None


OURS = "comfy-qat: held for comfy-linux"


def test_a_leftover_that_is_ours_unused_and_the_right_shape_fits():
    found = one(accelerators=T4, in_use="0", description=OURS)

    assert reservation.fits(found, machine_type="n1-standard-8",
                            accelerator="type=nvidia-tesla-t4,count=1") is True


def test_a_leftover_whose_use_google_did_not_report_still_fits():
    found = one(accelerators=T4, in_use=ABSENT, description=OURS)

    assert found.in_use is None
    assert reservation.fits(found, machine_type="n1-standard-8",
                            accelerator="type=nvidia-tesla-t4,count=1") is True


def test_the_accelerator_flag_is_read_in_either_order():
    found = one(accelerators=T4, description=OURS)

    assert reservation.fits(found, machine_type="n1-standard-8",
                            accelerator="count=1,type=nvidia-tesla-t4") is True


@pytest.mark.parametrize("changed,why", [
    ({"description": "made in the console"}, "not ours"),
    ({"description": ABSENT}, "no description at all"),
    ({"count": "2"}, "holds two VMs — this tool only ever makes one"),
    ({"in_use": "1"}, "something is already using it"),
    ({"machine_type": "n1-standard-4"}, "a different machine type"),
    ({"accelerators": (("nvidia-tesla-p4", 1),)}, "a different card"),
    ({"accelerators": (("nvidia-tesla-t4", 2),)}, "a different number of cards"),
    ({"accelerators": ABSENT}, "no card where one was asked for"),
    ({"status": "DELETING"}, "already on its way out"),
], ids=lambda value: value if isinstance(value, str) else None)
def test_a_leftover_that_differs_in_one_thing_does_not_fit(changed, why):
    fields = {"accelerators": T4, "in_use": "0", "description": OURS}
    assert reservation.fits(one(**fields), machine_type="n1-standard-8",
                            accelerator="type=nvidia-tesla-t4,count=1") is True, (
        "the unchanged fixture must fit, or this proves nothing")

    fields.update(changed)

    assert reservation.fits(one(**fields), machine_type="n1-standard-8",
                            accelerator="type=nvidia-tesla-t4,count=1") is False, why


@pytest.mark.parametrize("accelerators", [ABSENT, L4], ids=["not-listed", "listed"])
def test_a_built_in_card_fits_whether_or_not_google_lists_it(accelerators):
    """`accelerator=None` is how a G2 box is asked for. What Google reads back
    for one is not settled, so both read-backs fit: the machine type alone
    fixes the card."""
    found = one(machine_type="g2-standard-8", accelerators=accelerators, description=OURS)

    assert reservation.fits(found, machine_type="g2-standard-8", accelerator=None) is True


def test_no_accelerator_asked_for_does_not_fit_a_reservation_holding_an_attached_one():
    found = one(machine_type="n1-standard-8", accelerators=T4, description=OURS)

    assert reservation.fits(found, machine_type="n1-standard-8", accelerator=None) is False


# --- reservations with no box -------------------------------------------------


def test_a_reservation_nothing_is_bound_to_is_an_orphan():
    held = parsed(record(name="comfy-linux-rsv"), record(name="qatest-rsv"))
    instances = [box("comfy-linux", bound="comfy-linux-rsv"), box("loose")]

    assert [r.name for r in reservation.orphans(held, instances)] == ["qatest-rsv"]


def test_a_stopped_box_still_keeps_its_reservation_from_being_an_orphan():
    held = parsed(record(name="comfy-linux-rsv"))
    instances = [box("comfy-linux", bound="comfy-linux-rsv", status="TERMINATED")]

    assert reservation.orphans(held, instances) == []


def test_every_reservation_is_an_orphan_on_a_project_with_no_boxes():
    held = parsed(record(name="a-rsv"), record(name="b-rsv"))

    assert [r.name for r in reservation.orphans(held, [])] == ["a-rsv", "b-rsv"]
    assert reservation.orphans([], [box("loose")]) == []


def test_a_box_bound_by_name_in_another_zone_does_not_adopt_the_orphan():
    held = parsed(record(name="comfy-linux-rsv", zone="europe-west4-a"))
    instances = [box("comfy-linux", zone="us-central1-a", bound="comfy-linux-rsv")]

    assert [r.zone for r in reservation.orphans(held, instances)] == ["europe-west4-a"]


# --- the module stays pure ---------------------------------------------------


def _tree() -> ast.Module:
    return ast.parse(inspect.getsource(reservation))


def test_the_module_raises_nothing_and_prints_nothing():
    """`tests/test_docs.py` collects every message a module can raise or print
    and requires a troubleshooting entry for each. This module is contracted to
    contribute none: it answers questions, and the caller words the refusal."""
    tree = _tree()
    raises = [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Raise)]
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[-1] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {alias.name for alias in node.names}
            imported.add((node.module or "").split(".")[-1])

    assert raises == [], f"reservation.py raises at line(s) {raises}"
    assert not {"say", "output", "typer", "print"} & (names | imported), (
        "reservation.py reaches for something that prints")
    # The walk must have seen the module at all, or the two lines above pass on
    # an empty file.
    assert {"create"} <= imported and "Reservation" in {
        node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)}


def test_create_is_only_imported_where_it_is_used():
    """`create` is going to import this module, so this module importing
    `create` at the top would be a cycle that fails at import time."""
    top_level = set()
    for node in _tree().body:
        if isinstance(node, ast.Import):
            top_level |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            top_level |= {alias.name for alias in node.names}

    assert "create" not in top_level
    assert "dataclass" in top_level, "the walk must be reading the real imports"
