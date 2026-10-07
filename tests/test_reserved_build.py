"""`create --reserve`: two things are made, in order, and either can fail.

A reserved box is a reservation and an instance bound to it. The reservation
is the half that bills at GPU rate with or without a machine on it, so every
way this can stop half-way is a way of leaving one behind that nobody is
using and nobody was told about:

    reservation refused        nothing exists, nothing bills
    reservation, then refused  the reservation exists and bills — release it
    reservation, release fails it is STILL BILLING and the message must say so
    answer lost, box is there  carry on; deleting the reservation now would be
                               deleting it from under a running box
    answer lost, cannot tell   leave both, say both, hand over both commands

The cloud here is `fakes.FakeGcloud`, which keeps STATE: a reservation that
was made is listed until it is released, and a box bound to one is refused
unless that reservation already exists in the same zone. So these tests assert
what is left on the project afterwards, not only which calls were made — a
flow that forgets to release leaves the reservation in `cloud.reservations`.

Every payload is a FIXTURE shaped on the SDK schema. Nothing here has been
read off a live project, and the stockout sentence for `reservations create`
is the instance one: Google's wording for a reservation has not been seen.
"""

from __future__ import annotations

import pytest

from comfy_qa import create, inflight, reservation
from comfy_qa.create import CARDS, CREATE_FAILED, EXHAUSTED, IMAGES, Blueprint
from comfy_qa.gcloud import DENIED, GcloudError
from comfy_qa.lifecycle import LifecycleError
from comfy_qa.zones import Ordering
from fakes import FakeGcloud

PROJECT = "stately-timing-504610-p1"
A, B = "us-central1-a", "us-central1-b"

STOCKOUT = (
    "ERROR: (gcloud.compute.reservations.create) Could not fetch resource:\n"
    " - The zone 'projects/p/zones/us-central1-a' does not have enough resources "
    "available to fulfill the request.  '(resource type:compute)'.\n"
)
QUOTA = (
    "ERROR: (gcloud.compute.instances.create) Could not fetch resource:\n"
    " - Quota 'SSD_TOTAL_GB' exceeded.  Limit: 500.0 in region us-central1.\n"
)


def stockout():
    return GcloudError("Could not fetch resource", raw=STOCKOUT)


def refused():
    return GcloudError("Quota 'SSD_TOTAL_GB' exceeded", raw=QUOTA)


class Cloud(FakeGcloud):
    """`FakeGcloud`, plus the one read `build` makes that it does not have.

    `confirms_absent` is how `build` finds out, after a create that raised,
    whether the box exists anyway. Answered from the fake's own state — the
    instances it holds — so "the answer was lost and the box is there" is
    something a test sets up by putting the box there, not by scripting a
    return value.
    """

    def __init__(self, *, box_read=None, reservation_read=None, **kwargs):
        super().__init__(statuses=("TERMINATED",), **kwargs)
        self.box_read = box_read
        self.reservation_read = reservation_read

    def confirms_absent(self, name, zone, project):
        self.calls.append(("confirms_absent", name, zone, project))
        if self.box_read is not None:
            raise self.box_read
        return not [row for row in self.instances
                    if row.get("name") == name and row.get("zone", "").endswith(f"/{zone}")]

    def reservation_absent(self, name, zone, project):
        if self.reservation_read is not None:
            self.calls.append(("reservation_absent", name, zone, project))
            raise self.reservation_read
        return super().reservation_absent(name, zone, project)

    def verbs(self):
        return [call[0] for call in self.calls]

    def mutations(self):
        return [call[:3] for call in self.calls
                if call[0] in ("create_reservation", "delete_reservation",
                               "create_instance_from_image")]


def in_turn(*answers):
    """A scripted behaviour that differs call by call: raise this, then pass."""
    left = list(answers)

    def behave():
        answer = left.pop(0) if left else None
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return behave


def reserved(card="t4", name="comfy-linux"):
    return Blueprint(name=name, image=IMAGES["linux"], card=CARDS[card], reserve=True)


def to_try(*zones, fall_through=True):
    return Ordering(zones=tuple(zones), fall_through=fall_through, regions=tuple(
        dict.fromkeys(zone.rsplit("-", 1)[0] for zone in zones)))


def build(cloud, blueprint=None, ordering=None, **kwargs):
    lines: list[str] = []
    zone = create.build(cloud, blueprint or reserved(), ordering or to_try(A, B),
                        PROJECT, lines.append, **kwargs)
    return zone, lines


def refusal(cloud, blueprint=None, ordering=None, **kwargs):
    with pytest.raises(LifecycleError) as caught:
        build(cloud, blueprint, ordering, **kwargs)
    return caught.value


@pytest.fixture(autouse=True)
def nothing_left_registered():
    """`inflight` is process-wide. A test that leaves an entry registered would
    hand it to whichever test interrupts next."""
    inflight.clear()
    yield
    inflight.clear()


# --- what a reserved blueprint is ---------------------------------------------


def test_a_reserved_box_names_its_reservation_after_itself():
    assert reserved().reservation == "comfy-linux-rsv"
    assert reserved().reservation == reservation.name_for("comfy-linux")


def test_a_box_that_is_not_reserved_has_no_reservation_at_all():
    """None, not "". `create_in` passes it on only when there is one."""
    plain = Blueprint(name="comfy-linux", image=IMAGES["linux"], card=CARDS["t4"])
    assert plain.reserve is False
    assert plain.reservation is None


def test_an_attached_card_is_reserved_with_its_accelerator_and_a_built_in_one_without():
    """THE ONE OPEN FACT, isolated. What `reservations create` takes for a card
    that is part of the machine type has not been settled against Google, and
    the default is the same value the instance create is given: nothing."""
    assert reserved("t4").reservation_accelerator == "type=nvidia-tesla-t4,count=1"
    assert reserved("l4").reservation_accelerator is None
    assert reserved("h100").reservation_accelerator is None


def test_what_a_reservation_is_made_with_is_decided_in_exactly_one_place(monkeypatch):
    """If the probe says a G2 reservation needs `--accelerator`, one function
    changes. This proves that function is the one `build` actually reads."""
    monkeypatch.setattr(create, "_reservation_accelerator",
                        lambda card: f"type={card.accelerator},count={card.count}")
    cloud = Cloud()
    build(cloud, reserved("l4"))

    assert cloud.reserved_with["accelerator"] == "type=nvidia-l4,count=1"
    assert reserved("l4").reservation_accelerator == "type=nvidia-l4,count=1"


def test_planning_a_reserved_box_marks_it_reserved():
    assert create.plan(os_choice="linux", gpu="t4", reserve=True).reserve is True
    assert create.plan(os_choice="linux", gpu="t4").reserve is False


def test_a_name_too_long_to_carry_the_suffix_is_refused_only_when_reserving():
    """Google's names stop at 63 and `-rsv` takes four. Checked offline: found
    at Google it costs the quota read and a confirmation first, and answers in
    the vocabulary of a regular expression."""
    longest = "a" * 59
    too_long = "a" * 60

    assert create.plan(os_choice="linux", gpu="t4", name=longest, reserve=True).name == longest
    assert create.plan(os_choice="linux", gpu="t4", name=too_long).name == too_long, (
        "an unreserved box may still use all 63")

    with pytest.raises(LifecycleError) as caught:
        create.plan(os_choice="linux", gpu="t4", name=too_long, reserve=True)
    message = str(caught.value)
    assert "60 characters" in message and "at most 59" in message
    assert "Nothing was created." in message
    assert caught.value.kind == CREATE_FAILED


# --- the order of the two calls -------------------------------------------------


def test_the_reservation_is_made_before_the_box_and_in_the_same_zone():
    cloud = Cloud()
    zone, _ = build(cloud)

    assert zone == A
    assert cloud.mutations() == [
        ("create_reservation", "comfy-linux-rsv", A),
        ("create_instance_from_image", "comfy-linux", A),
    ]


def test_what_is_left_on_the_project_is_one_reservation_with_its_box_on_it():
    """State, not calls: the reservation exists, is ours, and is in use by a
    box that names it."""
    cloud = Cloud()
    build(cloud)

    (held,) = reservation.parse_all(cloud.reservations)
    assert (held.name, held.zone, held.in_use) == ("comfy-linux-rsv", A, 1)
    assert held.ours and held.box == "comfy-linux"
    (box,) = cloud.instances
    assert reservation.bound_to(box) == "comfy-linux-rsv"
    assert reservation.orphans([held], cloud.instances) == []


def test_the_reservation_is_made_for_exactly_the_machine_the_box_will_be():
    cloud = Cloud()
    build(cloud)

    assert cloud.reserved_with == {
        "machine_type": "n1-standard-8",
        "accelerator": "type=nvidia-tesla-t4,count=1",
        "description": "comfy-qat: held for comfy-linux",
    }
    assert cloud.created_with["reservation"] == "comfy-linux-rsv"
    assert cloud.created_with["machine_type"] == "n1-standard-8"
    assert cloud.created_with["accelerator"] == "type=nvidia-tesla-t4,count=1"
    assert "terminate_on_maintenance" not in cloud.created_with, "a GPU box keeps the default"


def test_a_built_in_card_is_reserved_by_its_machine_type_alone():
    cloud = Cloud()
    build(cloud, reserved("l4"))

    assert cloud.reserved_with["machine_type"] == "g2-standard-8"
    assert cloud.reserved_with["accelerator"] is None


def test_a_box_that_is_not_reserved_makes_no_reservation_and_binds_to_none():
    """The pair, on the same stateful cloud: the unreserved path is untouched."""
    cloud = Cloud()
    plain = Blueprint(name="comfy-linux", image=IMAGES["linux"], card=CARDS["t4"])
    zone, _ = build(cloud, plain)

    assert zone == A
    assert cloud.mutations() == [("create_instance_from_image", "comfy-linux", A)]
    assert "reservation" not in cloud.created_with
    assert cloud.reservations == []


def test_every_step_is_announced():
    _, lines = build(Cloud())
    assert lines == [f"trying {A}…", f"  reserving comfy-linux-rsv in {A}",
                     "  creating comfy-linux on it"]


# --- the zone has none to reserve -----------------------------------------------


def test_a_stockout_at_the_reservation_falls_through_and_leaves_nothing_behind():
    cloud = Cloud(reserve=in_turn(stockout(), None))
    zone, lines = build(cloud)

    assert zone == B
    assert f"  {A} has no T4 to reserve right now" in lines
    assert cloud.mutations() == [
        ("create_reservation", "comfy-linux-rsv", A),
        ("create_reservation", "comfy-linux-rsv", B),
        ("create_instance_from_image", "comfy-linux", B),
    ], "no box was attempted in the zone that had nothing to reserve"
    (held,) = reservation.parse_all(cloud.reservations)
    assert held.zone == B, "and nothing was left behind in the zone that had none"


def test_a_stockout_everywhere_leaves_no_reservation_and_says_nothing_is_billing():
    cloud = Cloud(reserve=in_turn(stockout(), stockout()))
    problem = refusal(cloud)

    assert problem.kind == EXHAUSTED
    assert "Nothing was created and nothing is billing" in str(problem)
    assert cloud.reservations == [] and cloud.instances == []
    assert "create_instance_from_image" not in cloud.verbs()


def test_a_named_zone_with_nothing_to_reserve_does_not_wander_off():
    """`--zone` is this zone or nothing, reserved or not."""
    cloud = Cloud(reserve=in_turn(stockout(), None))
    problem = refusal(cloud, ordering=to_try(A, fall_through=False))

    assert problem.kind == EXHAUSTED
    assert cloud.mutations() == [("create_reservation", "comfy-linux-rsv", A)]
    assert cloud.reservations == []


# --- the reservation is refused for some other reason ----------------------------


def test_a_reservation_refused_outright_made_nothing_and_says_so():
    denied = GcloudError("Required 'compute.reservations.create' permission",
                         raw="PERMISSION_DENIED", kind=DENIED)
    cloud = Cloud(reserve=denied)
    problem = refusal(cloud)

    message = str(problem)
    assert f"Google refused to reserve comfy-linux-rsv in {A}" in message
    assert "Nothing was created and nothing is billing." in message
    assert "still billing" not in message, "nothing is, and saying so would send somebody looking"
    assert cloud.reservations == []
    assert "create_instance_from_image" not in cloud.verbs()
    assert cloud.mutations() == [("create_reservation", "comfy-linux-rsv", A)], (
        "a refusal that is not a stockout is not a reason to try another zone")


def test_a_reservation_whose_answer_was_lost_is_reported_as_still_billing():
    """The create raised and the reservation is there anyway. `hold` puts it on
    the project; the exception is what the caller saw."""
    cloud = Cloud()

    def lost():
        cloud.hold("comfy-linux-rsv", A, PROJECT, machine_type="n1-standard-8",
                   accelerator="type=nvidia-tesla-t4,count=1",
                   description="comfy-qat: held for comfy-linux")
        raise GcloudError("timed out after 300s", raw="deadline exceeded")

    cloud.reserve = lost
    problem = refusal(cloud)

    assert "still billing" in str(problem)
    assert "comfy-linux was not created" in str(problem)
    assert (f"gcloud compute reservations delete comfy-linux-rsv --zone={A} "
            f"--project={PROJECT}") in problem.fix
    assert "create_instance_from_image" not in cloud.verbs()
    assert len(cloud.reservations) == 1, "this must not release what it cannot account for"


def test_a_reservation_that_cannot_be_checked_afterwards_is_treated_as_billing():
    """Not read is not absent. If Google will not say, the message assumes the
    expensive answer."""
    cloud = Cloud(reserve=GcloudError("timed out", raw="deadline exceeded"),
                  reservation_read=GcloudError("timed out", raw="deadline exceeded"))
    problem = refusal(cloud)

    assert "still billing" in str(problem)
    assert "could not be checked" in str(problem)
    assert (f"gcloud compute reservations delete comfy-linux-rsv --zone={A} "
            f"--project={PROJECT}") in problem.fix
    assert f"gcloud compute reservations list --project={PROJECT}" in problem.fix


# --- the box is refused after the reservation exists ------------------------------


@pytest.mark.parametrize("why", [refused, stockout], ids=["quota", "stockout"])
def test_a_box_refused_after_its_reservation_was_made_releases_the_reservation(why):
    """Both kinds of refusal, and the second is the one that matters: a
    stockout at the INSTANCE must not fall through to another zone. The
    capacity was already held here; moving on would leave this reservation
    billing and go and make a second one."""
    cloud = Cloud(create_instance=why())
    problem = refusal(cloud)

    assert cloud.mutations() == [
        ("create_reservation", "comfy-linux-rsv", A),
        ("create_instance_from_image", "comfy-linux", A),
        ("delete_reservation", "comfy-linux-rsv", A),
    ]
    assert cloud.reservations == [], "the reservation is really gone"
    assert cloud.instances == []
    message = str(problem)
    assert f"Google refused to create comfy-linux in {A}" in message
    assert ("The reservation made for it (comfy-linux-rsv) was released, so nothing "
            "is left billing.") in message
    assert problem.kind == CREATE_FAILED


def test_the_box_is_looked_for_before_anything_is_released():
    """A create that raised may have made the box. Releasing first would take
    the reservation from under a running machine."""
    cloud = Cloud(create_instance=refused())
    refusal(cloud)

    verbs = cloud.verbs()
    assert verbs.index("confirms_absent") < verbs.index("delete_reservation")


def test_a_release_that_fails_says_the_reservation_is_still_billing():
    cloud = Cloud(create_instance=refused(),
                  release=GcloudError("timed out after 300s", raw="deadline exceeded"))
    problem = refusal(cloud)

    message = str(problem)
    assert "could not be released" in message
    assert "still billing" in message
    assert "nothing is left billing" not in message
    assert (f"gcloud compute reservations delete comfy-linux-rsv --zone={A} "
            f"--project={PROJECT}") in problem.fix
    assert len(cloud.reservations) == 1, "and it really is still there"


def test_a_box_whose_answer_was_lost_but_which_exists_is_kept_with_its_reservation():
    """The instance create raised; the instance is there. Nothing is released
    and the build carries on to be recorded — the alternative is a running,
    billing, reserved box in no host list."""
    cloud = Cloud()
    made = FakeGcloud.create_instance_from_image

    def lost(name, zone, project, **kwargs):
        made(cloud, name, zone, project, **kwargs)
        raise GcloudError("timed out after 300s", raw="deadline exceeded")

    cloud.create_instance_from_image = lost
    zone, lines = build(cloud)

    assert zone == A
    assert "delete_reservation" not in cloud.verbs()
    assert len(cloud.reservations) == 1 and len(cloud.instances) == 1
    assert any("comfy-linux is there" in line and "timed out" in line for line in lines), lines


def test_a_box_that_cannot_be_checked_leaves_both_and_hands_over_both_commands():
    cloud = Cloud(create_instance=GcloudError("timed out", raw="deadline exceeded"),
                  box_read=GcloudError("timed out", raw="deadline exceeded"))
    problem = refusal(cloud)

    message = str(problem)
    assert "still billing" in message
    assert "may exist" in message
    assert "delete_reservation" not in cloud.verbs(), (
        "it may be in use by a box that is there")
    assert len(cloud.reservations) == 1
    assert (f"gcloud compute instances list --project={PROJECT}") in problem.fix
    assert (f"gcloud compute instances stop comfy-linux --zone={A} "
            f"--project={PROJECT}") in problem.fix
    assert (f"gcloud compute reservations delete comfy-linux-rsv --zone={A} "
            f"--project={PROJECT}") in problem.fix


# --- Ctrl-C between the two calls --------------------------------------------------


def test_an_interrupt_between_the_two_calls_reports_the_reservation(capsys):
    """The reservation exists and is billing, the box does not, and the person
    has just pressed Ctrl-C. The registration stays open across both calls so
    that this is the moment it speaks."""
    cloud = Cloud(create_instance=KeyboardInterrupt())

    with pytest.raises(inflight.Interrupted):
        build(cloud)

    said = capsys.readouterr().err
    assert f"the reservation comfy-linux-rsv in {A}" in said
    assert (f"gcloud compute reservations delete comfy-linux-rsv --zone={A} "
            f"--project={PROJECT}") in said
    assert "it bills until deleted, with or without a box" in said
    assert f"the instance comfy-linux in {A}" in said, "the box may exist too"
    assert len(cloud.reservations) == 1, "nothing was cleaned up behind the user's back"


def test_an_interrupt_while_reserving_reports_the_reservation(capsys):
    cloud = Cloud(reserve=KeyboardInterrupt())

    with pytest.raises(inflight.Interrupted):
        build(cloud)

    said = capsys.readouterr().err
    assert f"the reservation comfy-linux-rsv in {A}" in said
    assert f"the instance comfy-linux in {A}" not in said, "no box was asked for yet"


def test_nothing_stays_registered_after_a_build_that_worked():
    build(Cloud())
    assert inflight.pending() == []


def test_nothing_stays_registered_after_a_build_that_was_refused():
    """An ordinary failure has its own words; the interrupt report is not them."""
    refusal(Cloud(create_instance=refused()))
    assert inflight.pending() == []


# --- resuming: a reservation left by an earlier run ---------------------------------


def left_behind(cloud, zone=B, *, accelerator="type=nvidia-tesla-t4,count=1",
                description="comfy-qat: held for comfy-linux",
                machine_type="n1-standard-8"):
    row = cloud.hold("comfy-linux-rsv", zone, PROJECT, machine_type=machine_type,
                     accelerator=accelerator, description=description)
    (found,) = reservation.parse_all([row])
    return found


def test_a_reservation_left_by_an_earlier_run_is_used_rather_than_made_again():
    cloud = Cloud()
    leftover = left_behind(cloud, B)
    zone, lines = build(cloud, ordering=to_try(B, fall_through=False), leftover=leftover)

    assert zone == B
    assert cloud.mutations() == [("create_instance_from_image", "comfy-linux", B)]
    assert cloud.created_with["reservation"] == "comfy-linux-rsv"
    assert len(cloud.reservations) == 1, "one reservation, not two"
    assert f"  reusing reservation comfy-linux-rsv in {B} — left by an earlier run" in lines


def test_a_resumed_build_goes_only_where_the_reservation_is():
    """Whatever the ordering says. A reservation cannot move, and trying the
    next zone would make a second one under the same name."""
    cloud = Cloud()
    leftover = left_behind(cloud, B)
    zone, lines = build(cloud, ordering=to_try(A, B), leftover=leftover)

    assert zone == B
    assert f"trying {A}…" not in lines
    assert cloud.mutations() == [("create_instance_from_image", "comfy-linux", B)]


def test_a_resumed_box_that_is_refused_releases_the_reservation_it_resumed():
    """It is released for the same reason one made this run would be: nothing
    is using it, and the message is about to say nothing is left billing."""
    cloud = Cloud(create_instance=refused())
    leftover = left_behind(cloud, B)
    problem = refusal(cloud, ordering=to_try(B, fall_through=False), leftover=leftover)

    assert cloud.reservations == []
    assert "was released, so nothing is left billing" in str(problem)


@pytest.mark.parametrize("change,why", [
    (dict(description="held by hand"), "not made by this tool"),
    (dict(machine_type="n1-standard-4"), "a different machine"),
    (dict(accelerator="type=nvidia-tesla-p4,count=1"), "a different card"),
])
def test_a_leftover_that_does_not_fit_is_refused_before_anything_is_touched(change, why):
    cloud = Cloud()
    leftover = left_behind(cloud, B, **change)
    problem = refusal(cloud, ordering=to_try(B, fall_through=False), leftover=leftover)

    assert cloud.mutations() == [], why
    assert f"a reservation called comfy-linux-rsv is already on this project in {B}" in str(problem)
    assert "Nothing was created." in str(problem)
    assert (f"gcloud compute reservations delete comfy-linux-rsv --zone={B} "
            f"--project={PROJECT}") in problem.fix
    assert len(cloud.reservations) == 1, "somebody else's reservation is not ours to release"


def test_a_leftover_handed_to_a_box_that_is_not_being_reserved_is_ignored():
    """`leftover` means something only for a reserved build. An unreserved box
    must not be bound to a reservation because one of that name exists."""
    cloud = Cloud()
    leftover = left_behind(cloud, B)
    plain = Blueprint(name="comfy-linux", image=IMAGES["linux"], card=CARDS["t4"])
    zone, _ = build(cloud, plain, leftover=leftover)

    assert zone == A
    assert "reservation" not in cloud.created_with


# --- the record, and what is said -----------------------------------------------------


def test_a_reserved_box_is_recorded_with_its_reservation():
    from comfy_qa.discover import parse, to_toml

    cloud = Cloud()
    build(cloud)
    made = create.host_entry(reserved(), A, PROJECT)

    assert made.reservation == "comfy-linux-rsv"
    assert made.reservation == parse(cloud.instances[0], PROJECT).reservation, (
        "what create records is what discover would read back off the box")
    assert 'gce_reservation = "comfy-linux-rsv"' in to_toml(made, 8191)


def test_a_box_that_is_not_reserved_is_recorded_without_one():
    plain = Blueprint(name="comfy-linux", image=IMAGES["linux"], card=CARDS["t4"])
    assert create.host_entry(plain, A, PROJECT).reservation == ""


def test_what_is_said_after_a_reserved_box_exists_is_the_bill_and_what_stops_it():
    """The two sentences, typed out here rather than read from the module that
    writes them — a money sentence checked against itself checks nothing."""
    lines = create.next_steps(reserved(), A)

    assert lines == [
        "comfy-linux is installing the NVIDIA driver from its startup script, "
        "which reboots it once or twice. `comfy-qat go` waits that out.",
        "  comfy-qat go comfy-linux     # install ComfyUI and serve it",
        "comfy-linux is reserved. Google holds its capacity and bills for it every "
        "hour — running or stopped — until the box is deleted.",
        "  comfy-qat down comfy-linux     # first — delete refuses a box that is running",
        "  comfy-qat delete comfy-linux   # the only thing that stops a reserved "
        "box's bill — the box and its disk go too",
    ]
    assert not [line for line in lines if "stop paying" in line], (
        "`down` is a step towards `delete`, and is never offered as what stops "
        "this box's bill")


def test_the_plan_for_a_reserved_box_says_what_it_costs_before_it_is_made():
    steps = reserved().steps(A)
    text = "\n".join(steps)

    assert steps[0] == (f"reserve the capacity first: reservation comfy-linux-rsv in {A}, "
                        f"which only this box can use")
    assert ("comfy-linux is reserved. Google holds its capacity and bills for it every "
            "hour — running or stopped — until the box is deleted.") in steps
    assert f"create comfy-linux in {A}: Ubuntu 22.04, T4 (nvidia-tesla-t4)" in text


def test_the_plan_for_a_box_that_is_not_reserved_says_nothing_about_reserving():
    plain = Blueprint(name="comfy-linux", image=IMAGES["linux"], card=CARDS["t4"])
    assert "reserv" not in "\n".join(plain.steps(A))


# --- audit: a stock-out at the reservation is checked, not believed -----------------


def test_a_stockout_that_left_a_reservation_anyway_is_not_called_nothing_billing():
    """"A stock-out leaves nothing behind" was true only because the fakes made
    it so. If Google answers stock-out and the reservation is there, moving on
    to the next zone abandons it and ends on "nothing is billing"."""
    cloud = Cloud()

    def stocked_out_but_made():
        cloud.hold("comfy-linux-rsv", A, PROJECT, machine_type="n1-standard-8",
                   accelerator="type=nvidia-tesla-t4,count=1",
                   description="comfy-qat: held for comfy-linux")
        raise stockout()

    cloud.reserve = stocked_out_but_made
    problem = refusal(cloud)

    assert "still billing" in str(problem)
    assert "nothing is billing" not in str(problem)
    assert (f"gcloud compute reservations delete comfy-linux-rsv --zone={A} "
            f"--project={PROJECT}") in problem.fix
    assert cloud.mutations() == [("create_reservation", "comfy-linux-rsv", A)], (
        "it did not go on to make a second one in the next zone")


def test_a_stockout_that_left_nothing_is_read_before_it_falls_through():
    cloud = Cloud(reserve=in_turn(stockout(), None))
    zone, _ = build(cloud)

    assert zone == B
    verbs = cloud.verbs()
    assert verbs.index("reservation_absent") < verbs.index("create_reservation", 1)
