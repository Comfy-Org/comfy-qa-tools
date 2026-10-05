"""What is actually sent to gcloud for a reservation, argv by argv.

A reservation bills at GPU rate from the moment it exists, used or not, so the
commands that make and remove one are checked for their VALUE — the whole argv,
typed out here — and not for the presence of a flag. `--vm-count=2` and
`--vm-count=1` both "have a vm-count".

Everything goes through the `Gcloud(runner=...)` seam: no subprocess, no
Google. The three sentences Google answers a `describe` with are FIXTURES
modelled on the instance wordings in `tests/test_prune.py`, which were read
live; the reservation spelling of the path has not been.
"""

from __future__ import annotations

import inspect

import pytest

from comfy_qa import gcloud as gc_mod
from comfy_qa import reservation
from comfy_qa.gcloud import (
    DENIED,
    ELSEWHERE,
    GONE,
    INSTANCE_TIMEOUT,
    NOT_FOUND,
    TIMEOUT,
    UNADDRESSABLE,
    Gcloud,
    GcloudError,
    classify,
)
from fakes import FakeGcloud


def statuses_from(wanted, listings):
    """Looked up at call time, so a test names itself when the function is gone."""
    return gc_mod.statuses_from(wanted, listings)


def recording(answer=None):
    """A Gcloud whose every call is written down as (argv, mode)."""
    calls: list[tuple[list[str], object]] = []

    def runner(args, mode):
        calls.append((list(args), mode))
        if isinstance(answer, BaseException):
            raise answer
        return answer

    return Gcloud(runner=runner), calls


class Gated(Gcloud):
    """A Gcloud with no runner injected, so the credential gate is live.

    `_ready_for` is a no-op under the runner seam on purpose, which makes the
    seam the wrong place to ask whether a call goes through the gate. This
    replaces the two things the gate and the call bottom out in instead, and
    records the order they happen in.
    """

    def __init__(self) -> None:
        super().__init__()
        self.events: list[tuple] = []

    def preflight(self, project=None):
        self.events.append(("preflight", project))

    def run(self, args, *, parse_json=True, timeout=None):
        self.events.append(("run", list(args), parse_json, timeout))
        return ""


# --- making one --------------------------------------------------------------


def test_a_reservation_for_an_attached_card_is_exactly_this_command():
    gc, calls = recording("")

    gc.create_reservation(
        "comfy-linux-rsv", "us-central1-a", "proj",
        machine_type="n1-standard-8",
        accelerator="type=nvidia-tesla-t4,count=1",
        description="comfy-qat: held for comfy-linux",
    )

    assert calls == [([
        "compute", "reservations", "create", "comfy-linux-rsv",
        "--zone=us-central1-a", "--project=proj",
        "--vm-count=1",
        "--machine-type=n1-standard-8",
        "--require-specific-reservation",
        "--accelerator=type=nvidia-tesla-t4,count=1",
        "--description=comfy-qat: held for comfy-linux",
    ], False)]


def test_a_reservation_for_a_built_in_card_sends_no_accelerator_and_no_description():
    """The whole argv again, so what IS there is asserted and not only what is
    gone: one VM, the machine type, and the flag that stops any other box
    consuming it."""
    gc, calls = recording("")

    gc.create_reservation("comfy-l4-rsv", "us-east4-a", "proj",
                          machine_type="g2-standard-8")

    assert calls == [([
        "compute", "reservations", "create", "comfy-l4-rsv",
        "--zone=us-east4-a", "--project=proj",
        "--vm-count=1",
        "--machine-type=g2-standard-8",
        "--require-specific-reservation",
    ], False)]


def test_making_a_reservation_proves_the_credential_first_and_waits_long_enough():
    """From this call on the project is charged, so it goes through the same
    gate as starting a box — and under the instance clock, not the 60s default."""
    gc = Gated()

    gc.create_reservation("comfy-linux-rsv", "us-central1-a", "proj",
                          machine_type="n1-standard-8")

    assert [event[0] for event in gc.events] == ["preflight", "run"]
    assert gc.events[0] == ("preflight", "proj")
    _verb, argv, parse_json, timeout = gc.events[1]
    assert argv[:3] == ["compute", "reservations", "create"]
    assert parse_json is False
    assert timeout == INSTANCE_TIMEOUT == 300


# --- releasing one -----------------------------------------------------------


def test_releasing_a_reservation_is_exactly_this_command():
    gc, calls = recording("")

    gc.delete_reservation("comfy-linux-rsv", "us-central1-a", "proj")

    assert calls == [([
        "compute", "reservations", "delete", "comfy-linux-rsv",
        "--zone=us-central1-a", "--project=proj", "--quiet",
    ], False)]


def test_releasing_a_reservation_waits_under_the_instance_clock():
    gc = Gated()

    gc.delete_reservation("comfy-linux-rsv", "us-central1-a", "proj")

    _verb, argv, parse_json, timeout = gc.events[-1]
    assert argv[:3] == ["compute", "reservations", "delete"]
    assert parse_json is False
    assert timeout == INSTANCE_TIMEOUT


def test_the_derivation_that_finds_mutating_calls_finds_these_two():
    """`test_host_costs.py` decides which `Gcloud` methods can leave something
    billing by reading the argv each one sends. A reservation call it could not
    see would be a failure handler nobody requires to name the bill.

    The real derivation is imported rather than copied: a copy would go on
    passing after the original changed.
    """
    from test_host_costs import _mutating_gcloud_methods

    found = _mutating_gcloud_methods()

    assert {"create_reservation", "delete_reservation"} <= found, sorted(found)
    # And it still tells a write from a read, or the line above proves nothing.
    assert not {"list_reservations", "reservation_absent", "machine_type_zones"} & found


# --- reading them ------------------------------------------------------------


def test_listing_reservations_asks_about_the_whole_project():
    rows = [{"name": "a-rsv"}, {"name": "b-rsv"}]
    gc, calls = recording(rows)

    assert gc.list_reservations("proj") == rows
    assert calls == [(["compute", "reservations", "list", "--project=proj"], True)]


def test_a_project_with_no_reservations_is_an_empty_list():
    gc, _calls = recording([])

    found = gc.list_reservations("proj")

    assert found == [] and found is not None


def test_a_listing_that_printed_nothing_is_not_a_project_with_no_reservations():
    """`run` answers None for an exit 0 with empty stdout. gcloud prints `[]`
    for an empty project, so nothing at all is a reply that never arrived — and
    reading it as "no reservations" would let a reserve go ahead past the limit."""
    gc, _calls = recording(None)

    with pytest.raises(GcloudError) as caught:
        gc.list_reservations("proj")

    assert "listed the reservations on proj and printed nothing at all" in str(caught.value)
    assert caught.value.fix.endswith("gcloud compute reservations list --project=proj")


def test_a_listing_that_failed_stays_a_failure():
    gc, _calls = recording(GcloudError("permission denied", kind=DENIED))

    with pytest.raises(GcloudError) as caught:
        gc.list_reservations("proj")

    assert caught.value.kind == DENIED


def test_what_the_listing_returns_is_what_the_model_reads():
    """The seam between the two halves of this work: the call's answer goes
    straight into `reservation.parse_all`, unwrapped."""
    gc, _calls = recording([{
        "name": "comfy-linux-rsv",
        "zone": "https://www.googleapis.com/compute/v1/projects/proj/zones/us-central1-a",
        "specificReservation": {"count": "1", "instanceProperties": {
            "machineType": "n1-standard-8",
            "guestAccelerators": [{"acceleratorType": "nvidia-tesla-t4",
                                   "acceleratorCount": 1}]}},
    }])

    found = reservation.parse_all(gc.list_reservations("proj"))

    assert [(r.name, r.zone, r.cards) for r in found] == [
        ("comfy-linux-rsv", "us-central1-a", 1)]


# --- is it really gone -------------------------------------------------------

NO_SUCH_RESERVATION = (
    "ERROR: (gcloud.compute.reservations.describe) Could not fetch resource:\n"
    " - The resource 'projects/proj/zones/us-central1-a/reservations/{name}' "
    "was not found\n"
)
NO_SUCH_INSTANCE_OF_THAT_NAME = (
    "ERROR: (gcloud.compute.reservations.describe) Could not fetch resource:\n"
    " - The resource 'projects/proj/zones/us-central1-a/instances/qatest-rsv' "
    "was not found\n"
)
NO_SUCH_ZONE = (
    "ERROR: (gcloud.compute.reservations.describe) Could not fetch resource:\n"
    " - The resource 'projects/proj/zones/us-centra1-a' was not found\n"
)
NO_SUCH_PROJECT = (
    "ERROR: (gcloud.compute.reservations.describe) Could not fetch resource:\n"
    " - The resource 'projects/no-such-project' was not found\n"
)
NO_PERMISSION = (
    "ERROR: (gcloud.compute.reservations.describe) Could not fetch resource:\n"
    " - Required 'compute.reservations.get' permission for "
    "'projects/proj/zones/us-central1-a/reservations/qatest-rsv'\n"
)


def refusing(body: str) -> Gcloud:
    def runner(args, mode):
        raise GcloudError("Could not fetch resource", raw=body, kind=classify(body))
    return Gcloud(runner=runner)


def test_the_fixture_sentences_are_classified_the_way_the_check_relies_on():
    assert classify(NO_SUCH_RESERVATION.format(name="qatest-rsv")) == NOT_FOUND
    assert classify(NO_SUCH_ZONE) == NOT_FOUND
    assert classify(NO_SUCH_PROJECT) == NOT_FOUND
    assert classify(NO_PERMISSION) == DENIED


def test_a_not_found_about_the_reservation_itself_confirms_it_is_absent():
    gc = refusing(NO_SUCH_RESERVATION.format(name="qatest-rsv"))

    assert gc.reservation_absent("qatest-rsv", "us-central1-a", "proj") is True


def test_a_reservation_that_is_described_is_not_absent():
    gc, calls = recording({"name": "qatest-rsv", "status": "READY"})

    assert gc.reservation_absent("qatest-rsv", "us-central1-a", "proj") is False
    assert calls == [([
        "compute", "reservations", "describe", "qatest-rsv",
        "--zone=us-central1-a", "--project=proj",
    ], True)]


@pytest.mark.parametrize("body", [
    NO_SUCH_ZONE,
    NO_SUCH_PROJECT,
    NO_PERMISSION,
    NO_SUCH_RESERVATION.format(name="somebody-elses-rsv"),
    NO_SUCH_INSTANCE_OF_THAT_NAME,
], ids=["zone", "project", "denied", "another-reservation", "an-instance"])
def test_nothing_else_confirms_a_reservation_is_absent(body):
    """Not-found about the zone, the project, a neighbour or a different kind
    of thing is not a statement about this reservation — and a mistyped
    `gce_zone` must not turn into "already released" for something still
    billing."""
    with pytest.raises(GcloudError):
        refusing(body).reservation_absent("qatest-rsv", "us-central1-a", "proj")


def test_a_not_found_about_something_larger_says_what_was_not_found():
    with pytest.raises(GcloudError) as caught:
        refusing(NO_SUCH_ZONE).reservation_absent("qatest-rsv", "us-centra1-a", "proj")

    assert caught.value.kind == NOT_FOUND
    assert str(caught.value) == (
        "Google says projects/proj/zones/us-centra1-a does not exist, which is "
        "not a statement about the reservation inside it"
    )
    assert caught.value.fix == (
        "see what the project holds: gcloud compute reservations list --project=proj")


def test_a_read_that_ran_out_of_clock_confirms_nothing():
    gc, _calls = recording(GcloudError("gcloud timed out after 60s", kind=TIMEOUT))

    with pytest.raises(GcloudError) as caught:
        gc.reservation_absent("qatest-rsv", "us-central1-a", "proj")

    assert caught.value.kind == TIMEOUT


# --- which zones have a machine type ------------------------------------------


def test_the_zones_of_a_machine_type_are_asked_for_without_naming_zones():
    rows = [{"name": "n1-standard-8", "zone": "us-central1-a"}]
    gc, calls = recording(rows)

    assert gc.machine_type_zones("proj", "n1-standard-8") == rows
    assert calls == [([
        "compute", "machine-types", "list",
        "--project=proj", "--filter=name=n1-standard-8",
    ], True)]


def test_a_machine_type_nowhere_offers_is_an_empty_list():
    gc, _calls = recording(None)

    assert gc.machine_type_zones("proj", "n1-standard-8") == []


# --- the two new choices on a create ------------------------------------------

BOX = dict(machine_type="n1-standard-8", image_family="ubuntu-2204-lts",
           image_project="ubuntu-os-cloud", disk_gb=200,
           accelerator="type=nvidia-tesla-t4,count=1", metadata="a=b",
           metadata_from_file="startup-script=/tmp/x")

# What `create_instance_from_image` sent at main @ 132afae, typed out. This is
# the baseline the two new keywords are measured against.
AS_TODAY = [
    "compute", "instances", "create", "comfy-linux",
    "--zone=us-central1-a", "--project=proj",
    "--machine-type=n1-standard-8",
    "--image-family=ubuntu-2204-lts", "--image-project=ubuntu-os-cloud",
    "--boot-disk-size=200GB", "--boot-disk-type=pd-balanced",
    "--boot-disk-device-name=comfy-linux",
    "--maintenance-policy=TERMINATE",
    "--accelerator=type=nvidia-tesla-t4,count=1",
    "--metadata=a=b",
    "--metadata-from-file=startup-script=/tmp/x",
]


def created(**extra) -> list[str]:
    gc, calls = recording("")
    gc.create_instance_from_image("comfy-linux", "us-central1-a", "proj",
                                  **BOX, **extra)
    assert len(calls) == 1 and calls[0][1] is False
    return calls[0][0]


def test_a_create_that_names_neither_choice_sends_what_it_always_sent():
    assert created() == AS_TODAY


def test_the_defaults_spelled_out_send_the_same_thing():
    assert created(reservation=None, terminate_on_maintenance=True) == AS_TODAY


def test_a_reserved_create_binds_the_box_to_its_reservation_by_name():
    argv = created(reservation="comfy-linux-rsv")

    assert argv == AS_TODAY + [
        "--reservation-affinity=specific", "--reservation=comfy-linux-rsv"]


def test_a_box_with_no_gpu_is_not_given_the_gpu_maintenance_policy():
    argv = created(terminate_on_maintenance=False)

    assert "--maintenance-policy=TERMINATE" in AS_TODAY, "the baseline must carry it"
    assert argv == [arg for arg in AS_TODAY if arg != "--maintenance-policy=TERMINATE"]
    assert not any(arg.startswith("--maintenance-policy") for arg in argv)
    assert not any(arg.startswith("--reservation") for arg in argv)


def test_the_two_choices_do_not_interfere():
    argv = created(reservation="comfy-linux-rsv", terminate_on_maintenance=False)

    assert argv == [arg for arg in AS_TODAY if arg != "--maintenance-policy=TERMINATE"] + [
        "--reservation-affinity=specific", "--reservation=comfy-linux-rsv"]


# --- statuses, from listings somebody else already has ------------------------


def listed(name, zone, status="RUNNING"):
    row = {"name": name, "zone": f"https://x/projects/p/zones/{zone}"}
    if status is not None:
        row["status"] = status
    return row


LISTINGS = {
    "proj-one": [listed("a", "z1"), listed("moved", "z2", "TERMINATED"),
                 listed("quiet", "z1", None)],
    "proj-two": [listed("b", "z9", "STAGING")],
}
WANTED = [
    ("a", "z1", "proj-one"),
    ("moved", "z1", "proj-one"),
    ("ghost", "z1", "proj-one"),
    ("quiet", "z1", "proj-one"),
    ("b", "z9", "proj-two"),
    ("a", "z1", "proj-two"),
    (None, None, None),
    ("local", "", "proj-one"),
]


def test_statuses_are_read_out_of_listings_by_name_zone_and_project():
    assert statuses_from(WANTED, LISTINGS) == {
        ("a", "z1", "proj-one"): "RUNNING",
        ("moved", "z1", "proj-one"): ELSEWHERE,
        ("ghost", "z1", "proj-one"): GONE,
        ("quiet", "z1", "proj-one"): Gcloud.UNKNOWN_STATE,
        ("b", "z9", "proj-two"): "STAGING",
        ("a", "z1", "proj-two"): GONE,
        (None, None, None): UNADDRESSABLE,
        ("local", "", "proj-one"): UNADDRESSABLE,
    }


def test_instance_statuses_answers_exactly_what_the_pure_half_answers():
    """`instance_statuses` is the listing plus `statuses_from`, and nothing
    else: the same wanted list against the same project contents gives the same
    answers, at one call per project."""
    calls = []

    def runner(args, mode):
        calls.append(" ".join(args))
        return LISTINGS[" ".join(args).rsplit("--project=", 1)[-1]]

    answered = Gcloud(runner=runner).instance_statuses(WANTED)

    assert answered == statuses_from(WANTED, LISTINGS)
    assert calls == ["compute instances list --project=proj-one",
                     "compute instances list --project=proj-two"]


def test_a_project_nobody_listed_is_unknown_and_never_gone():
    """`GONE` is the one word `discover --prune` deletes on, and it means the
    project WAS read. A project that is not in `listings` was not read, so its
    machines are answered — a missing key reads as None to a `.get` — with the
    word for "nobody could tell", which every caller already treats as possibly
    running."""
    answered = statuses_from([("a", "z1", "unread")], {"proj-one": LISTINGS["proj-one"]})

    assert answered == {("a", "z1", "unread"): Gcloud.UNKNOWN_STATE}
    assert answered[("a", "z1", "unread")] not in (GONE, ELSEWHERE, "TERMINATED")


def test_a_project_listed_and_empty_is_where_gone_comes_from():
    """The other half of the pair above: read-and-empty is a real answer."""
    assert statuses_from([("a", "z1", "proj")], {"proj": []}) == {
        ("a", "z1", "proj"): GONE}


def test_the_unknown_word_is_still_one_word():
    assert gc_mod.UNKNOWN_STATE == Gcloud.UNKNOWN_STATE == ""


# --- the fake cloud keeps a reservation's state -------------------------------


def test_the_fake_starts_with_the_reservations_it_was_given():
    fake = FakeGcloud(reservations=[{"name": "old-rsv"}])

    assert fake.list_reservations("proj") == [{"name": "old-rsv"}]
    assert FakeGcloud().list_reservations("proj") == []


def test_a_reservation_made_on_the_fake_is_listed_and_then_gone_when_released():
    fake = FakeGcloud()

    fake.create_reservation("comfy-linux-rsv", "us-central1-a", "proj",
                            machine_type="n1-standard-8",
                            accelerator="type=nvidia-tesla-t4,count=1",
                            description="comfy-qat: held for comfy-linux")
    held = reservation.parse_all(fake.list_reservations("proj"))

    assert [(r.name, r.zone, r.machine_type, r.accelerator, r.cards, r.ours, r.box)
            for r in held] == [("comfy-linux-rsv", "us-central1-a", "n1-standard-8",
                                "nvidia-tesla-t4", 1, True, "comfy-linux")]
    assert held[0].in_use is None, "a fresh reservation reports no use, like Google's"
    assert fake.reservation_absent("comfy-linux-rsv", "us-central1-a", "proj") is False

    fake.delete_reservation("comfy-linux-rsv", "us-central1-a", "proj")

    assert fake.list_reservations("proj") == []
    assert fake.reservation_absent("comfy-linux-rsv", "us-central1-a", "proj") is True
    assert [call[0] for call in fake.calls] == [
        "create_reservation", "list_reservations", "reservation_absent",
        "delete_reservation", "list_reservations", "reservation_absent"]


def test_the_fake_releases_only_the_reservation_in_the_zone_named():
    fake = FakeGcloud()
    for zone in ("us-central1-a", "us-east4-a"):
        fake.create_reservation("same-rsv", zone, "proj", machine_type="g2-standard-8")

    fake.delete_reservation("same-rsv", "us-east4-a", "proj")

    assert [r.zone for r in reservation.parse_all(fake.list_reservations("proj"))] == [
        "us-central1-a"]


def test_the_fake_refuses_to_release_what_is_not_there():
    """A flow that releases twice, or releases the wrong name, fails here the
    way it would at Google."""
    fake = FakeGcloud()

    with pytest.raises(GcloudError) as caught:
        fake.delete_reservation("never-made-rsv", "us-central1-a", "proj")

    assert caught.value.kind == NOT_FOUND


def test_a_reserve_scripted_to_fail_leaves_nothing_behind():
    stockout = GcloudError("no capacity", raw="ZONE_RESOURCE_POOL_EXHAUSTED")
    fake = FakeGcloud(reserve=stockout)

    with pytest.raises(GcloudError) as caught:
        fake.create_reservation("comfy-linux-rsv", "us-central1-a", "proj",
                                machine_type="n1-standard-8")

    assert caught.value is stockout
    assert fake.list_reservations("proj") == []
    assert fake.did("create_reservation")


def test_a_release_scripted_to_fail_leaves_the_reservation_billing():
    fake = FakeGcloud(release=GcloudError("backend error"))
    fake.create_reservation("comfy-linux-rsv", "us-central1-a", "proj",
                            machine_type="n1-standard-8")

    with pytest.raises(GcloudError):
        fake.delete_reservation("comfy-linux-rsv", "us-central1-a", "proj")

    assert [r["name"] for r in fake.list_reservations("proj")] == ["comfy-linux-rsv"]


def test_a_reservations_read_scripted_to_fail_raises_instead_of_answering_empty():
    """A listing that raises and a listing that is empty are the two cases the
    limit treats differently, so the fake has to be able to give either."""
    fake = FakeGcloud(reservations=GcloudError("permission denied", kind=DENIED))

    with pytest.raises(GcloudError):
        fake.list_reservations("proj")


def test_a_box_created_on_the_fake_records_its_reservation_and_consumes_it():
    fake = FakeGcloud(instances=[], statuses=("TERMINATED",))
    fake.create_reservation("comfy-linux-rsv", "us-central1-a", "proj",
                            machine_type="n1-standard-8",
                            accelerator="type=nvidia-tesla-t4,count=1")

    fake.create_instance_from_image(
        "comfy-linux", "us-central1-a", "proj", machine_type="n1-standard-8",
        image_family="ubuntu-2204-lts", image_project="ubuntu-os-cloud",
        disk_gb=200, accelerator="type=nvidia-tesla-t4,count=1",
        reservation="comfy-linux-rsv")

    assert fake.created_with["reservation"] == "comfy-linux-rsv"
    assert ("create_instance_from_image", "comfy-linux", "us-central1-a", "proj") in fake.calls
    instances = fake.list_instances("proj")
    assert [reservation.bound_to(i) for i in instances] == ["comfy-linux-rsv"]
    held = reservation.parse_all(fake.list_reservations("proj"))
    assert held[0].in_use == 1
    assert reservation.cards_held(instances, held) == 1
    assert reservation.orphans(held, instances) == []


def test_a_box_created_on_the_fake_without_a_reservation_is_bound_to_nothing():
    fake = FakeGcloud()

    fake.create_instance_from_image(
        "comfy-linux", "us-central1-a", "proj", machine_type="g2-standard-8",
        image_family="ubuntu-2204-lts", image_project="ubuntu-os-cloud", disk_gb=200)

    assert fake.created_with.get("reservation") is None
    assert [reservation.bound_to(i) for i in fake.list_instances("proj")] == [None]


def test_the_fake_refuses_a_box_bound_to_a_reservation_that_is_not_in_its_zone():
    """What makes "reservation first, same zone" a thing a test can fail on."""
    fake = FakeGcloud()
    fake.create_reservation("comfy-linux-rsv", "us-east4-a", "proj",
                            machine_type="g2-standard-8")

    with pytest.raises(GcloudError):
        fake.create_instance_from_image(
            "comfy-linux", "us-central1-a", "proj", machine_type="g2-standard-8",
            image_family="ubuntu-2204-lts", image_project="ubuntu-os-cloud",
            disk_gb=200, reservation="comfy-linux-rsv")

    assert fake.list_instances("proj") == []


def test_a_create_scripted_to_fail_on_the_fake_makes_no_box():
    fake = FakeGcloud(create_instance=GcloudError("Quota 'SSD_TOTAL_GB' exceeded"))

    with pytest.raises(GcloudError):
        fake.create_instance_from_image(
            "comfy-linux", "us-central1-a", "proj", machine_type="g2-standard-8",
            image_family="ubuntu-2204-lts", image_project="ubuntu-os-cloud", disk_gb=600)

    assert fake.list_instances("proj") == []
    assert fake.did("create_instance_from_image")


@pytest.mark.parametrize("method", [
    "list_reservations", "create_reservation", "delete_reservation",
    "reservation_absent", "create_instance_from_image",
])
def test_the_fake_takes_every_argument_the_real_call_takes(method):
    """A double that has drifted from its subject drives branches the real one
    cannot produce. Every parameter of the real method must be accepted by the
    fake, by the same name and in the same position."""
    real = inspect.signature(getattr(Gcloud, method)).parameters
    fake = inspect.signature(getattr(FakeGcloud, method)).parameters
    swallows = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in fake.values())

    assert len(real) > 1, "the walk must be reading a real signature"
    for name, parameter in real.items():
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
            assert name in fake or swallows, f"the fake's {method} has no `{name}`"
        else:
            assert list(fake).index(name) == list(real).index(name), (
                f"the fake's {method} takes `{name}` in a different position")
