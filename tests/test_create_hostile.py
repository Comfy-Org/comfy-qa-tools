"""`host create` and the zone picker, pushed until they do something expensive.

`tests/test_create.py` asks whether the happy path is right. This asks what it
costs when an input is wrong, and it is a separate file because the answers are a
different kind: every failure here either spends money, creates a machine nobody
asked for, or refuses one the project is entitled to.

Three of them are worth stating outright, because they are the ones that reach
Google's billing:

  * **A stockout used to override `--zone`.** `order_zones` builds a one-zone
    ordering for `--zone` and says in its own note that there is "no fall-through:
    this zone or nothing". `build` never read that note. Google's refusal names
    another zone, `build` put it at the front of the queue, and the box appeared
    in a zone the caller had explicitly ruled out. The same mechanism walked out
    of `--region`, and out of the quota-bearing regions altogether.

  * **`MAX_ATTEMPTS` was documented and not enforced.** `zones.MAX_ATTEMPTS`
    says six, "because each attempt is a real instance create that takes the
    better part of a minute when it fails". `build` capped nothing: every refusal
    could name a fresh zone, and the queue grew as fast as it was drained.

  * **`--disk` had a floor and no ceiling.** `--disk 20000` for `2000` is one
    keystroke and eighteen terabytes of pd-balanced.

The rest are the permissive/strict pair the quota payload keeps producing: -1
means "no explicit limit" in one place and the same shape means "none" nowhere,
and the same card is metered at region scope and at zone scope with different
values. Reading it one way costs money; reading it the other way refuses a
machine the project is allowed.
"""

from __future__ import annotations

import json
import math

import pytest

from comfy_qa.create import (
    CARDS,
    CREATE_FAILED,
    EXHAUSTED,
    IMAGES,
    MAX_DISK_GB,
    NO_QUOTA,
    Blueprint,
    build,
    check_quota,
    choose_name,
    plan,
    order_zones,
)
from comfy_qa.gcloud import GcloudError
from comfy_qa.lifecycle import LifecycleError, suggested_zones
from comfy_qa.quota import UNLIMITED, global_allowance
from comfy_qa.zones import (
    MAX_ATTEMPTS,
    UNREACHABLE,
    Ordering,
    choose,
    latencies,
    region_of,
)

PROJECT = "stately-timing-504610-p1"

REGIONS = ["europe-west4", "us-central1"]
ZONES = [f"{region}-{letter}" for region in REGIONS for letter in "abc"]

# The zone every `--zone` test names. `f` on purpose: it is the odd zone in
# us-central1 on the live project, which makes it the right one for a test about
# naming one zone deliberately — and it is not in ZONES, so nothing else picks it.
ZONE_OVERRIDE = "us-central1-f"

# What the fake says Google offers the card in. Wider than ZONES, because a
# `--zone` override is checked against the card and ZONE_OVERRIDE is not ranked.
OFFERED = [*ZONES, ZONE_OVERRIDE]

QUOTAS = [
    {"quotaId": "NVIDIA-L4-GPUS-per-project-region",
     "dimensionsInfos": [{"details": {"value": "1"}, "applicableLocations": REGIONS}]},
    {"quotaId": "GPUS-ALL-REGIONS-per-project",
     "dimensionsInfos": [{"details": {"value": "1"}, "applicableLocations": ["global"]}]},
]

LINUX_L4 = Blueprint(name="comfy-linux", image=IMAGES["linux"], card=CARDS["l4"])
LINUX_H100 = Blueprint(name="comfy-h", image=IMAGES["linux"], card=CARDS["h100"])


def stockout(zone: str, suggesting: str | None = None) -> str:
    """Google's own wording. An invented paraphrase matches no pattern at all."""
    body = (f"ERROR: (gcloud.compute.instances.create) Could not fetch resource:\n"
            f" - A g2-standard-8 VM instance is currently unavailable in the {zone} "
            f"zone.")
    if suggesting:
        body += f" Consider trying your request in the {suggesting} zone."
    return body + "\n"


class Cloud:
    """Creates that refuse per zone, and a record of every attempt made."""

    def __init__(self, refuse=None):
        self.refuse = dict(refuse or {})
        self.created: list[tuple[str, str]] = []

    def create_instance_from_image(self, name, zone, project, **kwargs):
        self.created.append((name, zone))
        problem = self.refuse.get(zone)
        if problem is not None:
            raise GcloudError("Could not fetch resource", raw=problem)

    def machine_types(self, project, zone_list, name):
        return [{"name": name, "zone": zone} for zone in zone_list]

    def accelerator_types(self, project, name):
        # `--zone` checks the card as well as the machine type, so this has to
        # answer about every zone a `--zone` test names — not just the ranked
        # ZONES. The `--zone` tests all use `us-central1-f`, which is on purpose
        # (it is the odd zone in us-central1 on the live project) and is not in
        # ZONES; answering only for ZONES turns those tests into
        # "us-central1-f has never offered nvidia-l4", which reads as a bug in
        # `create.py` and sends you hunting in the wrong file.
        return [{"name": name, "zone": zone} for zone in OFFERED]

    @property
    def zones_created_in(self) -> list[str]:
        return [made[1] for made in self.created]


def order(*zones_, fall_through=True):
    return Ordering(
        zones=tuple(zones_),
        regions=tuple(dict.fromkeys(region_of(zone) for zone in zones_)),
        fall_through=fall_through,
    )


# --- money: a box in a zone nobody asked for -------------------------------


def test_an_explicit_zone_is_not_overridden_by_a_zone_google_suggests():
    """The one flag whose whole purpose is "this zone or nothing".

    Somebody passes `--zone us-central1-a` because that is the zone the bug
    reproduces in. It is out of L4s, Google's refusal names `us-central1-c`, and
    the box used to be created there instead — billing, in the wrong place, and
    reported as a success.
    """
    cloud = Cloud(refuse={"us-central1-a": stockout("us-central1-a", "us-central1-c")})
    with pytest.raises(LifecycleError) as raised:
        build(cloud, LINUX_L4, order("us-central1-a", fall_through=False),
              PROJECT, lambda line: None)
    assert cloud.zones_created_in == ["us-central1-a"]
    assert raised.value.kind == EXHAUSTED


def test_order_zones_marks_an_explicit_zone_as_having_no_fall_through():
    """The note said so already; `build` needs it as data, not as prose."""
    check = check_quota(CARDS["l4"], QUOTAS, [])
    ordering = order_zones(Cloud(), PROJECT, LINUX_L4, check, zone="us-central1-a")
    assert ordering.fall_through is False


def test_a_chosen_ordering_still_falls_through(tmp_path):
    """`config=` and `probe=`, and neither is decoration.

    Without them this test opened REAL TCP connections to
    `compute.{europe-west4,us-central1}.rep.googleapis.com:443` and wrote
    `zone-latency.json` into the developer's own `~/.config/comfy-qa-tools/`.
    Measured, with a cold HOME: two outbound connections from this test and one
    from the wrong-case test below, and the file really appeared.

    The assertion is about `fall_through`, a boolean decided by whether a zone
    was pinned. The network call was entirely incidental to it — which is what
    made it invisible: nothing here wants a latency number, so nothing here
    noticed it was measuring one.

    DO NOT ADD AN ASSERTION ABOUT ZONE ORDER HERE, or anywhere that measures for
    real. The two regions' relative latency is jitter: 60/160 ms on one run and
    121/122 on the next, measured. Nothing asserts on the order today, so this is
    not flaky — it is one assertion away from a coin flip, and the ordering is
    already tested properly against a fixed `probe=` elsewhere in this file.
    """
    check = check_quota(CARDS["l4"], QUOTAS, [])
    ordering = order_zones(Cloud(), PROJECT, LINUX_L4, check,
                           config=tmp_path / "hosts.toml",
                           probe=counting_probe([]))
    assert ordering.fall_through is True


def test_a_suggestion_outside_the_regions_that_were_ranked_is_not_followed():
    """`--region europe-west4` narrowed the ordering to Europe. Google's refusal
    naming an Iowa zone is not permission to create the box in Iowa — and outside
    the quota-bearing regions it could not start there anyway."""
    cloud = Cloud(refuse={
        "europe-west4-a": stockout("europe-west4-a", "us-central1-c"),
        "europe-west4-b": stockout("europe-west4-b"),
    })
    with pytest.raises(LifecycleError):
        build(cloud, LINUX_L4, order("europe-west4-a", "europe-west4-b"),
              PROJECT, lambda line: None)
    assert "us-central1-c" not in cloud.zones_created_in


def test_a_suggestion_inside_the_ranked_regions_is_still_followed():
    """The fix must not throw away the useful half: Google's answer about a zone
    we were already willing to use is fresher than anything measured here."""
    cloud = Cloud(refuse={"us-central1-a": stockout("us-central1-a", "us-central1-c")})
    zone = build(cloud, LINUX_L4, order("us-central1-a", "us-central1-b"),
                 PROJECT, lambda line: None)
    assert zone == "us-central1-c"


def test_a_zone_google_names_in_a_different_case_is_not_created_in_twice():
    """`lifecycle.suggested_zones` returns the zone in the case Google wrote it.
    Passed through unchanged, `US-CENTRAL1-C` is both an invalid zone argument and
    a string that `tried` does not recognise as the zone it already attempted."""
    cloud = Cloud(refuse={
        "us-central1-a": stockout("us-central1-a", "US-CENTRAL1-C"),
        "us-central1-c": stockout("us-central1-c"),
    })
    with pytest.raises(LifecycleError):
        build(cloud, LINUX_L4, order("us-central1-a", "us-central1-c"),
              PROJECT, lambda line: None)
    assert cloud.zones_created_in == ["us-central1-a", "us-central1-c"]


# --- money: an attempt cap that is documented and not enforced -------------


def test_the_number_of_creates_attempted_is_capped_however_many_are_suggested():
    """Every refusal naming a fresh zone refills the queue as fast as it drains.
    Each attempt is a real create that takes most of a minute to fail, so an
    uncapped fall-through is a command that looks hung and is not."""
    zones = [f"us-central1-{letter}" for letter in "abcdefghijklmnop"]
    refuse = {zone: stockout(zone, next_) for zone, next_ in zip(zones, zones[1:])}
    refuse[zones[-1]] = stockout(zones[-1])
    cloud = Cloud(refuse=refuse)
    with pytest.raises(LifecycleError) as raised:
        build(cloud, LINUX_L4, order(zones[0]), PROJECT, lambda line: None)
    assert len(cloud.created) <= MAX_ATTEMPTS
    assert raised.value.kind == EXHAUSTED


def test_giving_up_at_the_cap_says_it_was_a_cap_and_not_the_whole_world():
    """"every zone is out of capacity" and "I stopped after six" are different
    facts, and only one of them means waiting will not help."""
    zones = [f"us-central1-{letter}" for letter in "abcdefghij"]
    refuse = {zone: stockout(zone, next_) for zone, next_ in zip(zones, zones[1:])}
    refuse[zones[-1]] = stockout(zones[-1])
    cloud = Cloud(refuse=refuse)
    with pytest.raises(LifecycleError) as raised:
        build(cloud, LINUX_L4, order(zones[0]), PROJECT, lambda line: None)
    assert "nothing is billing" in str(raised.value)
    assert str(MAX_ATTEMPTS) in str(raised.value)


# --- money: a disk with a floor and no ceiling -----------------------------


def test_a_disk_far_larger_than_any_box_needs_is_refused_before_it_is_billed():
    """`--disk 20000` for `2000` is one keystroke. pd-balanced is billed by the
    provisioned gigabyte from the moment the instance exists, whether or not
    anything is written to it, and nothing downstream would question it."""
    with pytest.raises(LifecycleError) as raised:
        plan(os_choice="linux", gpu="l4", disk_gb=20000)
    assert raised.value.kind == CREATE_FAILED
    assert "20000" in str(raised.value)


def test_the_largest_disk_that_is_allowed_still_is():
    assert plan(os_choice="linux", gpu="l4", disk_gb=MAX_DISK_GB).disk_gb == MAX_DISK_GB


# --- names: refused here rather than by Google, a minute later -------------


@pytest.mark.parametrize("bad", [
    "9lives",          # GCE names must start with a letter
    "-leading",        # ...and not with a hyphen
    "trailing-",       # ...and not end with one
    "x" * 64,          # ...over 63 characters
    "café",            # str.isalnum() is True for é, so `_clean` keeps it
    "ボックス",          # ...and for these
])
def test_a_name_google_will_not_accept_is_refused_before_anything_is_contacted(bad):
    """Every one of these reaches `gcloud` today — after the minute-long quota
    read, after four TCP probes, and after the confirmation prompt. The whole
    claim of `plan` is that it is "offline, and total"."""
    with pytest.raises(LifecycleError) as raised:
        choose_name(bad, IMAGES["linux"], set())
    assert raised.value.kind == CREATE_FAILED


def test_a_name_that_is_only_punctuation_does_not_become_the_empty_name():
    with pytest.raises(LifecycleError):
        choose_name("!!!", IMAGES["linux"], set())


@pytest.mark.parametrize("good", ["comfy-win-2", "l4", "a" * 63, "x1"])
def test_a_name_google_would_accept_is_accepted(good):
    assert choose_name(good, IMAGES["linux"], set()) == good


def test_a_name_is_still_matched_against_the_host_list_case_blind():
    """The host list lowercases; GCE does not accept anything else."""
    with pytest.raises(LifecycleError) as raised:
        choose_name("Comfy-Linux", IMAGES["linux"], {"comfy-linux"})
    assert "already taken" in str(raised.value)


def test_the_name_a_blueprint_carries_is_the_one_that_was_checked():
    """`_clean` rewrites what was typed. Creating `my-box` after `my_box` was
    checked is how the collision check gets bypassed."""
    made = plan(os_choice="linux", gpu="l4", name="My_Box")
    assert made.name == "my-box"
    with pytest.raises(LifecycleError):
        plan(os_choice="linux", gpu="l4", name="My_Box", taken={"my-box"})


# --- quota: the permissive direction costs money ---------------------------


def h100_quota(value):
    """The record a real project reports for an H100 grant.

    NOT `NVIDIA-H100-80GB-GPUS-per-project-region`, which is what this said and
    which exists in NO form anywhere — `create.py`'s own comment records that
    there is "no `-80GB-` H100 row at all". Google gives the H100 no standard
    per-model quota; its on-demand allowance is the family entry. A fixture for a
    grant nobody can hold proves the tool can read something that never arrives.
    """
    return {"quotaId": "GPUS-PER-GPU-FAMILY-per-project-region",
            "dimensionsInfos": [{"dimensions": {"gpu_family": "NVIDIA_H100"},
                                 "details": {"value": str(value)},
                                 "applicableLocations": REGIONS}]}


def ceiling(value, quota_id="GPUS-ALL-REGIONS-per-project"):
    return {"quotaId": quota_id,
            "dimensionsInfos": [{"details": {"value": str(value)},
                                 "applicableLocations": ["global"]}]}


def gpu_box(name, *, count=1, running=True):
    return {"name": name, "status": "RUNNING" if running else "TERMINATED",
            "guestAccelerators": [{"acceleratorType": ".../nvidia-l4",
                                   "acceleratorCount": count}]}


def test_an_h100_block_of_eight_already_running_holds_eight_of_the_ceiling():
    """The ceiling is counted in cards, not in boxes. One `a3-highgpu-8g` spends
    eight of it, and counting it as one lets the gate pass a create that Google
    will refuse — after the zone probing and the confirmation."""
    check = check_quota(CARDS["l4"], [*QUOTAS[:1], ceiling(8)],
                        [gpu_box("comfy-h", count=8)])
    problem = check.problem()
    assert problem is not None
    assert problem.kind == NO_QUOTA


def test_a_single_card_box_still_holds_only_one():
    check = check_quota(CARDS["l4"], [*QUOTAS[:1], ceiling(8)],
                        [gpu_box("comfy-linux", count=1)])
    assert check.problem() is None


def test_a_running_box_reporting_no_count_is_read_as_holding_one():
    """A payload without `acceleratorCount` is a shape we have not seen, not a
    box with no cards. Reading the absence as zero is the permissive direction."""
    instance = {"name": "odd", "status": "RUNNING",
                "guestAccelerators": [{"acceleratorType": ".../nvidia-l4"}]}
    check = check_quota(CARDS["l4"], [*QUOTAS[:1], ceiling(1)], [instance])
    assert check.problem() is not None


def test_an_h100_is_refused_when_the_ceiling_cannot_hold_a_whole_block():
    check = check_quota(CARDS["h100"], [h100_quota(8), ceiling(4)], [])
    problem = check.problem()
    assert problem is not None
    assert "8" in str(problem)


def test_an_h100_with_an_unlimited_card_grant_still_meets_the_ceiling():
    """-1 on the card is "no explicit limit" for that card. It says nothing about
    GPUS_ALL_REGIONS, which is the one that bites."""
    check = check_quota(CARDS["h100"], [h100_quota(-1), ceiling(1)], [])
    assert check.problem() is not None


def test_a_card_grant_of_minus_one_is_unlimited_and_not_refused():
    """The strict direction: reading -1 as "none" refuses a card the project
    holds an unlimited grant for."""
    check = check_quota(CARDS["l4"], [
        {"quotaId": "NVIDIA-L4-GPUS-per-project-region",
         "dimensionsInfos": [{"details": {"value": "-1"},
                              "applicableLocations": REGIONS}]},
        ceiling(-1),
    ], [])
    assert check.card_limit == -1
    assert check.problem() is None


@pytest.mark.parametrize("value", [None, "", "unlimited", "1.5", [], {}])
def test_a_quota_value_that_is_not_a_number_refuses_rather_than_crashes(value):
    """A shape nobody has seen is a reason to stop, not a traceback and not a
    create. Every one of these has to end in a refusal a person can read."""
    quotas = [{"quotaId": "NVIDIA-L4-GPUS-per-project-region",
               "dimensionsInfos": [{"details": {"value": value},
                                    "applicableLocations": REGIONS}]},
              ceiling(1)]
    check = check_quota(CARDS["l4"], quotas, [])
    assert check.problem() is not None


def test_a_project_that_reports_no_quota_at_all_is_refused_not_waved_through():
    check = check_quota(CARDS["l4"], [], [])
    problem = check.problem()
    assert problem is not None
    assert problem.kind == NO_QUOTA


def test_a_ceiling_that_was_not_reported_does_not_read_as_a_grant_of_none():
    """The strict direction is not free either. `global_allowance` returns None
    when the project reports no GPUS_ALL_REGIONS record at all, and None is "not
    read", not "zero" — refusing on it would block a create the project is
    entitled to, on a project whose ceiling is 8. So it does not gate. But it
    printed identically to a card with no grant, where None *does* mean none and
    *is* a refusal, and the one line a person reads to decide whether to argue
    with Google said the same thing for both."""
    check = check_quota(CARDS["l4"], QUOTAS[:1], [gpu_box("comfy-linux")])
    assert check.global_limit is None
    assert check.problem() is None
    ceiling_line = [line for line in check.lines() if "GPUS_ALL_REGIONS" in line][0]
    assert "not granted" not in ceiling_line
    assert "not reported" in ceiling_line


def test_a_card_with_no_grant_still_says_not_granted():
    check = check_quota(CARDS["a100"], QUOTAS, [])
    assert "not granted" in check.lines()[0]


def test_the_zone_scoped_copy_of_the_ceiling_does_not_make_it_unlimited():
    """The shape that bites: a project carries BOTH scopes of this quota, and
    the zone-scoped copy is -1.

    Both orderings, because a bug that depends on the order gcloud listed the
    records in is not fixed by a fixture that happens to list them the other
    way. gcloud offers no ordering contract, so neither may this.
    """
    zone_scoped = ceiling(-1, "GPUS-ALL-REGIONS-per-project-zone")

    assert global_allowance([zone_scoped, ceiling(1)]) == 1
    assert global_allowance([ceiling(1), zone_scoped]) == 1


def test_an_unlimited_row_beside_a_real_limit_does_not_win():
    """The scope filter is not the whole fix. One correctly region-scoped record
    can carry an unlimited row AND a real one, and that read as unlimited too.

    -1 is Google's "this record sets no explicit limit" — an absence, not a
    grant of infinity — so it must never overrule a number that was read.
    """
    both = {"quotaId": "GPUS-ALL-REGIONS-per-project",
            "dimensionsInfos": [{"details": {"value": "-1"},
                                 "applicableLocations": ["global"]},
                                {"details": {"value": "1"},
                                 "applicableLocations": ["global"]}]}
    assert global_allowance([both]) == 1


def test_a_ceiling_that_really_is_unlimited_still_reads_unlimited():
    """The other direction of the same decision. -1 loses to a number, and to
    nothing else — a project that reports only an unlimited ceiling has one."""
    assert global_allowance([ceiling(-1)]) == UNLIMITED
    assert global_allowance([ceiling(0)]) == 0, "a real zero is not an absence"
    assert global_allowance([]) is None


def test_suggested_zones_returns_a_zone_google_can_be_given_back():
    """Google echoes the zone in whatever case the request used, and every
    caller takes the string at its word.

    This matched case-insensitively and then appended VERBATIM, so a stockout
    naming `US-CENTRAL1-C` produced a string that `host` hands straight back to
    gcloud as a zone, that `lifecycle` and `relocate` put in a `comfy-qat move
    --to` line for somebody to run, and that `zones.region_of` turns into
    `US-CENTRAL1`, which matches no quota. Only `create` lowered defensively,
    which is why it looked cosmetic.
    """
    message = "stockout. Consider trying your request in the US-CENTRAL1-C zone."
    assert suggested_zones(message) == ["us-central1-c"]


def test_one_zone_named_twice_in_two_cases_is_one_zone():
    """The dedupe was case-sensitive too, for the same reason: it compared the
    strings Google wrote rather than the zones they name. Two spellings of one
    zone came back as two suggestions, and the advice offered the second as an
    alternative to the first."""
    message = ("stockout. Consider trying your request in the US-CENTRAL1-C, "
               "us-central1-c zone.")
    assert suggested_zones(message) == ["us-central1-c"]


def test_a_suggested_zone_survives_region_of():
    """The downstream that fails silently rather than loudly: an upper-case zone
    gives an upper-case region, and a quota lookup keyed on the region name then
    finds nothing while looking like it asked."""
    from comfy_qa.zones import region_of

    zone = suggested_zones("trying your request in the US-CENTRAL1-C zone.")[0]
    assert region_of(zone) == "us-central1"


# --- the latency cache, written by something that is not this ---------------


def write_cache(path, body):
    path.write_text(body if isinstance(body, str) else json.dumps(body),
                    encoding="utf-8")


def never_probe(region):
    raise AssertionError(f"the cache should have answered for {region}")


def counting_probe(seen):
    def probe(region):
        seen.append(region)
        return 100.0
    return probe


def test_a_cache_written_by_an_older_endpoint_scheme_is_not_reused(tmp_path):
    """The module docstring records the mistake this exists to survive: the
    obvious `<region>-<service>.googleapis.com` names all resolve to one anycast
    address, so a cache written against them holds four identical numbers that
    look like measurements and rank nothing. A file with no version stamp is
    exactly that file."""
    store = tmp_path / "zone-latency.json"
    write_cache(store, {"at": 0, "regions": {"us-central1": 203.0,
                                             "europe-west1": 206.0}})
    seen = []
    latencies(["us-central1", "europe-west1"], path=store, now=0.0,
              probe=counting_probe(seen))
    assert sorted(seen) == ["europe-west1", "us-central1"]


def test_a_cache_this_version_wrote_is_still_reused(tmp_path):
    store = tmp_path / "zone-latency.json"
    latencies(["us-central1"], path=store, now=0.0, probe=lambda region: 42.0)
    assert latencies(["us-central1"], path=store, now=0.0,
                     probe=never_probe) == {"us-central1": 42.0}


@pytest.mark.parametrize("score", [-1.0, -9999.0])
def test_a_negative_round_trip_is_discarded_rather_than_ranked_first(tmp_path, score):
    """No connection takes less than no time. A file that says one does steers
    every future create at whichever region it names, and nothing else in this
    tool would ever question it."""
    store = tmp_path / "zone-latency.json"
    latencies(["europe-west4"], path=store, now=0.0, probe=lambda region: 1.0)
    poisoned = json.loads(store.read_text(encoding="utf-8"))
    poisoned["regions"]["europe-west4"] = score
    write_cache(store, poisoned)
    assert latencies(["europe-west4"], path=store, now=0.0,
                     probe=lambda region: 55.0) == {"europe-west4": 55.0}


def test_a_round_trip_that_is_not_a_number_does_not_break_the_ordering(tmp_path):
    """`json.loads` accepts a bare NaN, and `float(nan)` is happily a float. NaN
    compares false against everything, so `sorted` returns an order that depends
    on the input order — and the one thing the ranking promises is that a dry run
    and the real run that follows it try the same zones."""
    store = tmp_path / "zone-latency.json"
    latencies(["europe-west4", "us-central1"], path=store, now=0.0,
              probe=lambda region: 1.0)
    poisoned = json.loads(store.read_text(encoding="utf-8"))
    poisoned["regions"]["europe-west4"] = float("nan")
    write_cache(store, json.dumps(poisoned))
    scores = latencies(["europe-west4", "us-central1"], path=store, now=0.0,
                       probe=lambda region: 55.0)
    assert all(math.isfinite(score) for score in scores.values())


def test_a_region_that_did_not_answer_is_not_remembered_for_a_week(tmp_path):
    """One create run behind a dropped VPN would otherwise write 9999 for every
    region and keep it for seven days, and the ranking the module exists to
    provide would quietly be alphabetical until it expired."""
    store = tmp_path / "zone-latency.json"
    latencies(["europe-west4"], path=store, now=0.0,
              probe=lambda region: UNREACHABLE)
    seen = []
    latencies(["europe-west4"], path=store, now=0.0, probe=counting_probe(seen))
    assert seen == ["europe-west4"]


def test_a_region_that_did_answer_is_remembered_even_beside_one_that_did_not(tmp_path):
    store = tmp_path / "zone-latency.json"
    scores = {"europe-west4": 208.0, "us-central1": UNREACHABLE}
    latencies(list(scores), path=store, now=0.0, probe=scores.get)
    seen = []
    latencies(list(scores), path=store, now=0.0, probe=counting_probe(seen))
    assert seen == ["us-central1"]


def test_a_cache_that_is_a_directory_is_a_miss_and_not_a_failed_create(tmp_path):
    store = tmp_path / "zone-latency.json"
    store.mkdir()
    assert latencies(["us-central1"], path=store, now=0.0,
                     probe=lambda region: 12.0) == {"us-central1": 12.0}


def test_a_cache_that_cannot_be_replaced_leaves_no_half_written_file_behind(tmp_path):
    """A stray `.tmp` beside the host list is something somebody later has to
    decide about, and this tool's whole posture is not leaving those."""
    store = tmp_path / "zone-latency.json"
    store.mkdir()
    latencies(["us-central1"], path=store, now=0.0, probe=lambda region: 12.0)
    assert [child.name for child in tmp_path.iterdir()] == ["zone-latency.json"]


@pytest.mark.parametrize("body", [
    '{"version": 1, "at": "yesterday", "regions": {"us-central1": 1.0}}',
    '{"version": 1, "at": 0, "regions": ["us-central1"]}',
    '{"version": 1, "at": 0, "regions": {"us-central1": "fast"}}',
    '[1, 2, 3]',
    'null',
    '',
])
def test_a_cache_of_the_wrong_shape_is_measured_again_rather_than_trusted(tmp_path, body):
    store = tmp_path / "zone-latency.json"
    write_cache(store, body)
    assert latencies(["us-central1"], path=store, now=0.0,
                     probe=lambda region: 7.0) == {"us-central1": 7.0}


def test_a_cache_naming_a_region_that_no_longer_exists_is_simply_not_asked_for(tmp_path):
    """Google retires regions. A stale entry must not appear in an ordering for a
    card nobody asked about it for."""
    store = tmp_path / "zone-latency.json"
    latencies(["europe-west4", "us-central9"], path=store, now=0.0,
              probe=lambda region: 5.0)
    assert latencies(["europe-west4"], path=store, now=0.0,
                     probe=never_probe) == {"europe-west4": 5.0}


def test_two_creates_writing_the_cache_at_once_leave_a_readable_file(tmp_path):
    """Two `create`s in two terminals is the ordinary case here — one Linux box,
    one Windows box. A half-written JSON file is only a cache miss, but a cache
    that is a miss forever is a minute added to every create."""
    store = tmp_path / "zone-latency.json"
    latencies(["europe-west4"], path=store, now=0.0, probe=lambda region: 1.0)

    def writes_while_reading(region):
        latencies(["us-central1"], path=store, now=0.0, probe=lambda other: 2.0)
        return 3.0

    latencies(["asia-east1"], path=store, now=0.0, probe=writes_while_reading)
    body = json.loads(store.read_text(encoding="utf-8"))
    assert isinstance(body.get("regions"), dict)


# --- the zone loop, with nothing in it -------------------------------------


def test_an_ordering_with_no_zones_never_reaches_a_create():
    cloud = Cloud()
    with pytest.raises(LifecycleError) as raised:
        build(cloud, LINUX_L4, order(), PROJECT, lambda line: None)
    assert cloud.created == []
    assert raised.value.kind == EXHAUSTED


def test_choosing_from_no_regions_asks_google_nothing_and_offers_nothing():
    class Refuses(Cloud):
        def accelerator_types(self, project, name):
            raise AssertionError("nothing should have been asked")

    assert choose(Refuses(), PROJECT, accelerator="nvidia-l4",
                  machine_type="g2-standard-8", regions=[]).zones == ()


def test_an_explicit_zone_in_a_region_with_no_quota_is_refused_before_the_create():
    """`--region me-west1` is refused with the reason. `--zone me-west1-a` used to
    sail past the same gate and find out from `gcloud`, one minute and one
    confirmation prompt later."""
    check = check_quota(CARDS["l4"], QUOTAS, [])
    with pytest.raises(LifecycleError) as raised:
        order_zones(Cloud(), PROJECT, LINUX_L4, check, zone="me-west1-a")
    assert raised.value.kind == NO_QUOTA
    assert "me-west1" in str(raised.value)


def test_an_explicit_zone_in_a_region_with_quota_is_still_allowed():
    check = check_quota(CARDS["l4"], QUOTAS, [])
    assert order_zones(Cloud(), PROJECT, LINUX_L4, check,
                       zone="us-central1-f").zones == ("us-central1-f",)


@pytest.mark.parametrize("typed", ["US-CENTRAL1-F", " us-central1-f "])
def test_a_zone_typed_in_the_wrong_case_is_the_same_zone(typed):
    """Google's names are lowercase. Matching `--zone US-CENTRAL1-F` against the
    quota regions without lowering it refuses, citing a region that does not
    exist — and the refusal reads like a missing grant."""
    check = check_quota(CARDS["l4"], QUOTAS, [])
    assert order_zones(Cloud(), PROJECT, LINUX_L4, check,
                       zone=typed).zones == ("us-central1-f",)


def test_a_region_typed_in_the_wrong_case_is_the_same_region(tmp_path):
    """The other half of the same leak — see the test above for the measurement.

    This one asserts a string is lowercased. It reached us-central1 over the
    network to do it.
    """
    check = check_quota(CARDS["l4"], QUOTAS, [])
    ordering = order_zones(Cloud(), PROJECT, LINUX_L4, check, region="US-CENTRAL1",
                           config=tmp_path / "hosts.toml",
                           probe=counting_probe([]))
    assert ordering.regions == ("us-central1",)


def test_a_refusal_naming_nonsense_instead_of_a_zone_adds_nothing_to_the_queue():
    cloud = Cloud(refuse={
        "us-central1-a": stockout("us-central1-a", "a-much-better-one"),
        "us-central1-b": stockout("us-central1-b"),
    })
    with pytest.raises(LifecycleError):
        build(cloud, LINUX_L4, order("us-central1-a", "us-central1-b"),
              PROJECT, lambda line: None)
    assert cloud.zones_created_in == ["us-central1-a", "us-central1-b"]


def test_a_zone_named_twice_in_the_ordering_is_created_in_once():
    cloud = Cloud(refuse={"us-central1-a": stockout("us-central1-a")})
    with pytest.raises(LifecycleError):
        build(cloud, LINUX_L4, order("us-central1-a", "us-central1-a"),
              PROJECT, lambda line: None)
    assert cloud.zones_created_in == ["us-central1-a"]


def test_a_refusal_that_is_not_about_capacity_never_creates_a_second_box():
    """The one thing that must not happen twice. Anything that is not plainly a
    stockout stops, because a box that was created and then reported as failed is
    a box that fall-through would create again under the same name in another
    zone — GCE names are unique per zone, so both would exist and both would
    bill."""
    refused = ("ERROR: (gcloud.compute.instances.create) Could not fetch resource:\n"
               " - The resource 'projects/p/zones/us-central1-a/instances/comfy-linux' "
               "already exists\n")
    cloud = Cloud(refuse={"us-central1-a": refused})
    with pytest.raises(LifecycleError) as raised:
        build(cloud, LINUX_L4, order("us-central1-a", "us-central1-b"),
              PROJECT, lambda line: None)
    assert len(cloud.created) == 1
    assert raised.value.kind == CREATE_FAILED


def test_an_ordering_that_names_no_region_follows_no_suggestion():
    """"I cannot say which regions are allowed" is not permission to use any."""
    cloud = Cloud(refuse={"us-central1-a": stockout("us-central1-a", "us-central1-c")})
    with pytest.raises(LifecycleError):
        build(cloud, LINUX_L4, Ordering(zones=("us-central1-a",), regions=()),
              PROJECT, lambda line: None)
    assert cloud.zones_created_in == ["us-central1-a"]


def test_a_timeout_with_no_message_is_not_read_as_a_stockout():
    """`create_instance_from_image` runs under a timeout, and a timeout means the
    instance may well exist. Falling through would make a second one."""
    class TimesOut(Cloud):
        def create_instance_from_image(self, name, zone, project, **kwargs):
            self.created.append((name, zone))
            raise GcloudError("timed out", raw=None)

    cloud = TimesOut()
    with pytest.raises(LifecycleError) as raised:
        build(cloud, LINUX_L4, order("us-central1-a", "us-central1-b"),
              PROJECT, lambda line: None)
    assert len(cloud.created) == 1
    assert "half-made" in (raised.value.fix or "")
