"""Choosing a zone, and the one measurement that has to be measured.

The payloads here are the real shapes, read off a live project on 2026-08-28:
`accelerator-types list` answers `{"name": ..., "zone": ...}` with the zone as a
bare name, and `machine-types list` answers with a full `selfLink`-style zone URL.
Both are handled, because both arrive.

The latency numbers are the ones actually recorded from the machine this was
written on. They are here as fixtures rather than as a live probe — a test that
opens sockets to Google measures the runner's network, not the tool.
"""

from __future__ import annotations

import json

import pytest

from comfy_qa import zones
from comfy_qa.zones import (
    ENDPOINT,
    UNREACHABLE,
    Ordering,
    choose,
    latencies,
    measure,
    region_of,
    zones_offering,
    zones_with_machine_type,
)

PROJECT = "stately-timing-504610-p1"
URL = f"https://www.googleapis.com/compute/v1/projects/{PROJECT}"

# Recorded from this machine, 2026-08-28. The ordering is the point: Europe is
# nearer than Iowa is nearer than Tokyo, and no API says so.
RECORDED = {
    "europe-west4": 208.1,
    "europe-west1": 219.4,
    "us-east1": 295.1,
    "us-central1": 310.9,
    "asia-northeast1": 432.6,
}


class FakeGcloud:
    """Only the two read calls `choose` makes, and a record of how it made them."""

    def __init__(self, accelerators=(), machines=()):
        self._accelerators = list(accelerators)
        self._machines = list(machines)
        self.asked: list[tuple] = []

    def accelerator_types(self, project, name):
        self.asked.append(("accelerator_types", project, name))
        return list(self._accelerators)

    def machine_types(self, project, zone_list, name):
        self.asked.append(("machine_types", project, tuple(zone_list), name))
        wanted = set(zone_list)
        return [entry for entry in self._machines
                if entry.get("zone", "").rsplit("/", 1)[-1] in wanted]


def accel(zone, name="nvidia-l4"):
    return {"name": name, "zone": zone}


def machine(zone, name="g2-standard-8"):
    return {"name": name, "zone": f"{URL}/zones/{zone}"}


def flat(scores):
    return lambda region: scores.get(region, UNREACHABLE)


# --- the endpoint that is actually measured -------------------------------


def test_the_endpoint_is_the_regional_one_not_the_anycast_one():
    """`<region>-run.googleapis.com` and friends all resolve to one anycast GFE.

    Measured 2026-08-28: us-central1, europe-west1, asia-east1 and
    southamerica-east1 all resolved to 172.217.112.4 and timed within 6 ms of
    each other, because they are the same host. Only the `.rep.` regional
    endpoints resolve per region. If this constant ever goes back to the obvious
    spelling, every latency number this tool prints becomes noise.
    """
    assert ENDPOINT.format(region="us-central1") == "compute.us-central1.rep.googleapis.com"
    assert not ENDPOINT.startswith("{region}-")


def test_a_region_that_does_not_answer_sorts_last_rather_than_disappearing(monkeypatch):
    """It may be the only region the project has quota in.

    The socket is replaced rather than aimed at a name that does not resolve: a
    test that makes a real DNS query measures the runner's resolver, and takes
    however long that resolver takes to give up.
    """
    def refuse(address, timeout=None):
        raise OSError("no route to host")

    monkeypatch.setattr(zones.socket, "create_connection", refuse)
    assert zones._connect("nowhere-1") == UNREACHABLE

    scores = measure(["a1", "b1"], probe=lambda region: UNREACHABLE if region == "a1" else 5.0)
    assert scores == {"a1": UNREACHABLE, "b1": 5.0}


def test_a_region_that_answers_is_timed_in_milliseconds(monkeypatch):
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *exc): return False

    monkeypatch.setattr(zones.socket, "create_connection", lambda address, timeout=None: Socket())
    assert 0 <= zones._connect("us-central1") < UNREACHABLE


def test_measuring_nothing_asks_nothing():
    assert measure([]) == {}


def test_each_region_is_measured_once_even_if_named_twice():
    seen = []
    measure(["us-central1", "us-central1", "europe-west4"],
            probe=lambda region: seen.append(region) or 1.0)
    assert seen.count("us-central1") == 1


# --- the cache ------------------------------------------------------------


def test_a_measurement_is_written_and_then_reused(tmp_path):
    store = tmp_path / "zone-latency.json"
    calls = []

    def probe(region):
        calls.append(region)
        return RECORDED[region]

    first = latencies(list(RECORDED), probe=probe, now=1000.0, path=store)
    assert first == RECORDED
    assert sorted(calls) == sorted(RECORDED)

    calls.clear()
    again = latencies(list(RECORDED), probe=probe, now=1000.0, path=store)
    assert again == RECORDED
    assert calls == [], "a cached region was measured again"


def cached(**body):
    """A cache file this version will actually read.

    The version stamp is not optional in a fixture: without it every one of these
    would be a miss for that reason, and each test below would pass while
    exercising nothing it names.
    """
    return json.dumps({"version": zones.CACHE_VERSION, **body})


def test_only_the_regions_not_already_cached_are_measured(tmp_path):
    store = tmp_path / "zone-latency.json"
    store.write_text(cached(at=1000.0, regions={"europe-west4": 208.1}))
    asked = []
    latencies(["europe-west4", "us-central1"],
              probe=lambda region: asked.append(region) or 300.0,
              now=1001.0, path=store)
    assert asked == ["us-central1"]


def test_a_stale_cache_is_measured_again(tmp_path):
    store = tmp_path / "zone-latency.json"
    store.write_text(cached(at=0.0, regions={"europe-west4": 1.0}))
    scores = latencies(["europe-west4"], probe=lambda region: 208.1,
                       now=zones.CACHE_TTL + 1, path=store)
    assert scores == {"europe-west4": 208.1}


@pytest.mark.parametrize("body", [
    "not json at all",
    "[]",
    '{"version": 1, "regions": {"europe-west4": 1.0}}',          # no timestamp
    '{"version": 1, "at": "yesterday", "regions": {"a": 1.0}}',  # not a number
    '{"version": 1, "at": 1000.0, "regions": "everything"}',     # not a table
    '{"version": 1, "at": 1000.0, "regions": {"a": "quick"}}',   # not a number
    '{"at": 1000.0, "regions": {"a": 1.0}}',                     # no version stamp
])
def test_a_cache_that_cannot_be_read_is_a_miss_not_a_failure(tmp_path, body):
    """The only cost of an unreadable cache is measuring again. It is not an error."""
    store = tmp_path / "zone-latency.json"
    store.write_text(body)
    scores = latencies(["a"], probe=lambda region: 42.0, now=1000.0, path=store)
    assert scores == {"a": 42.0}


def test_a_directory_that_cannot_be_written_does_not_fail_a_create(tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    scores = latencies(["a"], probe=lambda region: 42.0, now=1000.0,
                       path=blocked / "sub" / "zone-latency.json")
    assert scores == {"a": 42.0}


# --- reading what Google offers -------------------------------------------


def test_the_virtual_workstation_variant_is_not_treated_as_the_card():
    """`--filter=name=nvidia-l4` is exact today, and gcloud warns it will not be.

    It prints, on every single call, that its `=` operator "currently does not
    match but will match in the near future". The day it does, this filter also
    returns `nvidia-l4-vws` — a virtual-workstation allowance that will not start
    an ordinary GPU instance. So the name is checked here too.
    """
    payload = [accel("us-central1-a"), accel("us-central1-a", "nvidia-l4-vws"),
               accel("us-central1-b", "nvidia-l4-vws")]
    assert zones_offering(payload, "nvidia-l4") == ["us-central1-a"]


def test_a_zone_url_is_reduced_to_its_name():
    assert zones_with_machine_type([machine("us-central1-a")], "g2-standard-8") == [
        "us-central1-a"]


def test_a_zone_offered_twice_appears_once():
    assert zones_offering([accel("us-central1-a"), accel("us-central1-a")],
                          "nvidia-l4") == ["us-central1-a"]


@pytest.mark.parametrize("zone,region", [
    ("us-central1-a", "us-central1"),
    ("northamerica-northeast1-c", "northamerica-northeast1"),
    ("us-central1", "us-central1"),          # already a region
    ("", ""),
])
def test_a_zone_is_reduced_to_its_region(zone, region):
    assert region_of(zone) == region


# --- the order ------------------------------------------------------------


def zone_choice(**kwargs):
    gc = kwargs.pop("gc")
    kwargs.setdefault("accelerator", "nvidia-l4")
    kwargs.setdefault("machine_type", "g2-standard-8")
    kwargs.setdefault("probe", flat(RECORDED))
    return choose(gc, PROJECT, **kwargs)


def test_the_nearest_region_measured_comes_first(tmp_path):
    gc = FakeGcloud(
        accelerators=[accel("us-central1-a"), accel("europe-west4-a"),
                      accel("asia-northeast1-a")],
        machines=[machine("us-central1-a"), machine("europe-west4-a"),
                  machine("asia-northeast1-a")],
    )
    order = zone_choice(gc=gc, regions=["us-central1", "europe-west4", "asia-northeast1"],
                        path=tmp_path / "cache.json")
    assert order.zones == ("europe-west4-a", "us-central1-a", "asia-northeast1-a")


def test_a_region_with_no_quota_is_never_offered_however_near_it_is(tmp_path):
    """The step that is easy to get wrong, and the reason it is step one.

    europe-west4 is the nearest region by a hundred milliseconds and Google
    offers L4 there. The project has no grant, so it must not appear — ranking on
    distance alone picks a zone where nothing can start, and the refusal arrives
    as a quota error that reads like a permissions problem.
    """
    gc = FakeGcloud(
        accelerators=[accel("europe-west4-a"), accel("us-central1-a")],
        machines=[machine("europe-west4-a"), machine("us-central1-a")],
    )
    order = zone_choice(gc=gc, regions=["us-central1"], path=tmp_path / "cache.json")
    assert order.zones == ("us-central1-a",)


def test_a_zone_that_offers_the_card_but_not_the_machine_type_is_dropped(tmp_path):
    """us-central1-f has T4 and no G2. Both halves are checked, not one."""
    gc = FakeGcloud(
        accelerators=[accel("us-central1-f"), accel("us-central1-a")],
        machines=[machine("us-central1-a")],
    )
    order = zone_choice(gc=gc, regions=["us-central1"], path=tmp_path / "cache.json")
    assert order.zones == ("us-central1-a",)


def test_no_overlap_at_all_is_reported_rather_than_returned_empty(tmp_path):
    gc = FakeGcloud(accelerators=[accel("us-central1-a")], machines=[])
    order = zone_choice(gc=gc, regions=["us-central1"], path=tmp_path / "cache.json")
    assert not order
    assert order.notes and "g2-standard-8" in order.notes[0]


def test_a_card_offered_nowhere_you_have_quota_says_so(tmp_path):
    gc = FakeGcloud(accelerators=[accel("asia-northeast1-a")], machines=[])
    order = zone_choice(gc=gc, regions=["us-central1"], path=tmp_path / "cache.json")
    assert not order
    assert "nvidia-l4" in order.notes[0]


def test_no_regions_asks_google_nothing(tmp_path):
    gc = FakeGcloud()
    assert not zone_choice(gc=gc, regions=[], path=tmp_path / "cache.json")
    assert gc.asked == [], "a read was made with nowhere to put the box"


def test_the_search_widens_when_the_nearest_regions_offer_nothing(tmp_path):
    """Only the nearest few regions get their zones looked up — 130 is slow.

    That shortcut must not turn into a wrong answer, so when the nearest few
    yield nothing the search widens, and says that it did.
    """
    gc = FakeGcloud(
        accelerators=[accel("asia-northeast1-a")],
        machines=[machine("asia-northeast1-a")],
    )
    order = zone_choice(gc=gc, regions=list(RECORDED), nearest=1,
                        path=tmp_path / "cache.json")
    assert order.zones == ("asia-northeast1-a",)
    assert order.notes and "further afield" in order.notes[0]


def test_only_the_nearest_regions_are_asked_about_machine_types(tmp_path):
    gc = FakeGcloud(
        accelerators=[accel(f"{region}-a") for region in RECORDED],
        machines=[machine(f"{region}-a") for region in RECORDED],
    )
    zone_choice(gc=gc, regions=list(RECORDED), nearest=2, path=tmp_path / "cache.json")
    asked = [call for call in gc.asked if call[0] == "machine_types"]
    assert len(asked) == 1
    assert set(asked[0][2]) == {"europe-west4-a", "europe-west1-a"}


def test_the_number_of_zones_tried_is_capped(tmp_path):
    """Every attempt is a real create that takes most of a minute when it fails."""
    many = [f"us-central1-{letter}" for letter in "abcdefghij"]
    gc = FakeGcloud(accelerators=[accel(zone) for zone in many],
                    machines=[machine(zone) for zone in many])
    order = zone_choice(gc=gc, regions=["us-central1"], limit=3,
                        path=tmp_path / "cache.json")
    assert len(order.zones) == 3


def test_the_order_is_stable_so_a_dry_run_predicts_the_real_run(tmp_path):
    gc = FakeGcloud(
        accelerators=[accel("us-central1-c"), accel("us-central1-a"), accel("us-central1-b")],
        machines=[machine("us-central1-c"), machine("us-central1-a"), machine("us-central1-b")],
    )
    first = zone_choice(gc=gc, regions=["us-central1"], path=tmp_path / "cache.json")
    second = zone_choice(gc=gc, regions=["us-central1"], path=tmp_path / "cache.json")
    assert first.zones == second.zones == ("us-central1-c", "us-central1-a", "us-central1-b")


def test_the_printed_order_carries_the_measurement_that_made_it():
    order = Ordering(zones=("europe-west4-a", "asia-northeast1-a"),
                     regions=("europe-west4", "asia-northeast1"), latency=RECORDED)
    lines = order.lines()
    assert "208 ms to europe-west4" in lines[0]
    assert "433 ms to asia-northeast1" in lines[1]


def test_an_unreachable_region_says_so_rather_than_printing_9999ms():
    order = Ordering(zones=("nowhere-1-a",), regions=("nowhere-1",),
                     latency={"nowhere-1": UNREACHABLE})
    assert "did not answer" in order.lines()[0]


# --- spending six attempts on six questions --------------------------------

# Ten regions, so the attempt budget cannot reach them all. `RECORDED` is five
# and every one of them fits inside the near set, which is the shape that hides
# a narrowed search rather than the shape that shows it.
MANY = {f"europe-north{index}": 100.0 + index for index in range(10)}


def test_the_attempts_are_spread_across_regions_rather_than_spent_on_one(tmp_path):
    """Six creates in one region is one question asked six times.

    A GPU stockout is very often the whole region, and the run that produced this
    change spent three of its six attempts on europe-west4-a, -b and -c. Each of
    those costs the better part of a minute and tells you what the one before it
    already did. So every region is asked once before any region is asked twice.
    """
    every = [f"{region}-{letter}" for region in RECORDED for letter in "abc"]
    gc = FakeGcloud(accelerators=[accel(zone) for zone in every],
                    machines=[machine(zone) for zone in every])
    order = zone_choice(gc=gc, regions=list(RECORDED), path=tmp_path / "cache.json")
    assert order.zones[:5] == ("europe-west4-a", "europe-west1-a", "us-east1-a",
                               "us-central1-a", "asia-northeast1-a")
    assert order.zones[5] == "europe-west4-b", "the second pass, nearest first"


def test_the_near_set_has_room_for_every_attempt():
    """It was 4 against a budget of 6, and that is now a contradiction.

    `_spread` gives each attempt a different region, so a near set smaller than
    the budget cannot fill it: the last attempts have nowhere to go but back to a
    region that has already said no. Four also meant that from London every zone
    a create ever tried was in Europe.
    """
    assert zones.NEAREST_REGIONS >= zones.MAX_ATTEMPTS


def test_a_region_that_already_runs_a_box_is_tried_before_nearer_ones(tmp_path):
    """Latency is measured from the laptop, and the laptop is not the fleet.

    Every existing box is in us-central1; the new one is 100 ms further from this
    desk and next door to its neighbours, its models and a region that has
    demonstrably had capacity for this project.
    """
    every = [f"{region}-a" for region in RECORDED]
    gc = FakeGcloud(accelerators=[accel(zone) for zone in every],
                    machines=[machine(zone) for zone in every])
    order = zone_choice(gc=gc, regions=list(RECORDED), fleet=["us-central1-a"],
                        path=tmp_path / "cache.json")
    assert order.zones[0] == "europe-west4-a", "the nearest region keeps the first try"
    assert order.zones[1] == "us-central1-a", "and the fleet gets the second"
    assert any("already runs a box" in note for note in order.notes)


def test_a_fleet_in_a_region_the_project_has_no_quota_in_changes_nothing(tmp_path):
    """A preference cannot become permission. Quota is still step one."""
    every = [f"{region}-a" for region in RECORDED]
    gc = FakeGcloud(accelerators=[accel(zone) for zone in every],
                    machines=[machine(zone) for zone in every])
    order = zone_choice(gc=gc, regions=["europe-west4", "us-central1"],
                        fleet=["me-west1-a"], path=tmp_path / "cache.json")
    assert order.zones == ("europe-west4-a", "us-central1-a")
    assert order.notes == ()


def test_the_order_says_how_little_of_the_world_it_looked_at(tmp_path):
    """The numbered list looks exhaustive and is not.

    Nothing on it admitted that six zones out of ten regions is a sample, so a
    stockout in all six read as "there is none anywhere". `--dry-run` is where
    somebody would want to be told, and it is named here because it is the flag
    that does something about it.
    """
    every = [f"{region}-a" for region in MANY]
    gc = FakeGcloud(accelerators=[accel(zone) for zone in every],
                    machines=[machine(zone) for zone in every])
    order = choose(gc, PROJECT, accelerator="nvidia-l4",
                   machine_type="g2-standard-8", regions=list(MANY),
                   probe=flat(MANY), path=tmp_path / "cache.json")
    assert len(order.zones) == 6
    note = " ".join(order.notes)
    assert "6 of the 10 regions" in note
    assert "--region" in note


def test_an_order_that_reaches_everywhere_it_may_says_nothing_about_scope(tmp_path):
    """The note is a caveat. A caveat that is always printed is furniture."""
    every = [f"{region}-a" for region in ("europe-west4", "us-central1")]
    gc = FakeGcloud(accelerators=[accel(zone) for zone in every],
                    machines=[machine(zone) for zone in every])
    order = zone_choice(gc=gc, regions=["europe-west4", "us-central1"],
                        path=tmp_path / "cache.json")
    assert order.notes == ()


def test_the_regions_that_offer_the_card_are_recorded_for_the_refusal(tmp_path):
    """`build` needs the honest denominator, and it is not `regions`.

    Quota reaches forty-three regions and Google offers an L4 in a fraction of
    them, so "6 of 43" would be a second true sentence with a false reading.
    """
    gc = FakeGcloud(
        accelerators=[accel("europe-west4-a"), accel("us-central1-a")],
        machines=[machine("europe-west4-a"), machine("us-central1-a")],
    )
    order = zone_choice(gc=gc, regions=list(RECORDED), path=tmp_path / "cache.json")
    assert order.offering == ("europe-west4", "us-central1")
    assert len(order.regions) == 5, "where quota reaches is a different question"
