"""GPU quota, in the terms a person thinks in.

The fixtures here are the shape a live project actually returns. The first version
of this file invented them with underscores — `NVIDIA_L4_GPUS-per-project-region` —
and every exclusion rule silently failed against the real hyphenated ids, while the
region never parsed at all. Nothing caught it until the tool was run for real.
"""

from __future__ import annotations

import pytest

from comfy_qa.quota import (
    available_gpus,
    summarise,
    friendly_name,
    matches,
    readiness,
    resolve,
)

# 43 regions, as the live API reports for a regional GPU quota.
REGIONS = [
    "africa-south1", "asia-east1", "asia-east2", "europe-west4",
    "us-central1", "us-east1", "us-west1",
]


def quota(quota_id, value=None, locations=None):
    """A record shaped like the live API: dimensions null, places in locations."""
    details = {} if value is None else {"value": str(value)}
    return {
        "quotaId": quota_id,
        "dimensions": ["region"],
        "dimensionsInfos": [{
            "dimensions": None,
            "details": details,
            "applicableLocations": REGIONS if locations is None else locations,
        }],
    }


L4 = quota("NVIDIA-L4-GPUS-per-project-region", 1)
A100 = quota("NVIDIA-A100-GPUS-per-project-region", None)   # details: {} — no grant
T4 = quota("NVIDIA-T4-GPUS-per-project-region", 0)
GLOBAL = quota("GPUS-ALL-REGIONS-per-project", 1, ["global"])


@pytest.mark.parametrize("quota_id,expected", [
    ("NVIDIA-L4-GPUS-per-project-region", "L4"),
    ("NVIDIA-L4-GPUS-per-project-zone", "L4"),
    ("NVIDIA-A100-80GB-GPUS-per-project-region", "A100-80GB"),
    ("NVIDIA-RTX-PRO-6000-GPUS-per-project-region", "RTX-PRO-6000"),
    ("GPUS-ALL-REGIONS-per-project", "any (global)"),
    ("CPUS-per-project-region", None),
])
def test_friendly_names_from_real_ids(quota_id, expected):
    assert friendly_name(quota_id) == expected


@pytest.mark.parametrize("quota_id", [
    "COMMITTED-NVIDIA-L4-GPUS-per-project-region",
    "PREEMPTIBLE-NVIDIA-A100-GPUS-per-project-zone",
    "NVIDIA-L4-VWS-GPUS-per-project-region",
    "PREEMPTIBLE-NVIDIA-T4-VWS-GPUS-per-project-zone",
    "GPUS-PER-GPU-FAMILY-per-project-region",
])
def test_allowances_that_cannot_start_an_ordinary_box_are_excluded(quota_id):
    """Committed, preemptible and virtual-workstation quotas are real but do not
    let you start a normal GPU instance. Offering them would be a lie.

    These are exactly the ids the underscore version failed to exclude.
    """
    assert friendly_name(quota_id) is None


@pytest.mark.parametrize("typed", ["l4", "L4", "  l4  ", "l-4", "l_4"])
def test_user_spelling_is_forgiven(typed):
    assert matches(typed, "NVIDIA-L4-GPUS-per-project-region")


def test_l4_does_not_match_a100():
    assert not matches("l4", "NVIDIA-A100-GPUS-per-project-region")


def test_a_string_value_is_a_number():
    """The live API returns `{"value": "1"}` — a string, not an int."""
    assert readiness([L4])[0].limit == 1


def test_an_absent_value_means_no_grant_not_a_crash():
    assert readiness([A100])[0].status == "none"


def test_one_allowance_over_many_regions_is_one_row():
    """43 regions sharing an allowance is one row. Forty-three would be noise.

    The label is the COUNT — `7 regions` for this fixture — not `all regions`.
    There were three phrasings for one geography (`all regions` from `Row.where`,
    `43 regions` from `Pool.where`, `in 43 region(s)` from `create`); the count
    form won because it never claims more than it knows, and `pools_for` really
    does see spans of 41 out of 43.
    """
    rows = readiness([L4])
    assert len(rows) == 1
    assert rows[0].region == "7 regions"


def test_a_single_location_is_named():
    assert readiness([GLOBAL])[0].region == "global"


def test_readiness_says_what_you_can_run_today():
    rows = readiness([L4, A100, T4], preferences=[
        {"quotaId": "NVIDIA-A100-GPUS-per-project-region",
         "quotaConfig": {"preferredValue": 1}},
    ])
    by_gpu = {r.gpu: r.status for r in rows}
    assert by_gpu["L4"] == "ready"
    assert by_gpu["A100"] == "pending"
    assert by_gpu["T4"] == "none"


def test_ready_sorts_first():
    rows = readiness([T4, A100, L4])
    assert rows[0].gpu == "L4" and rows[0].usable


def test_asking_for_one_region_keeps_an_all_regions_grant():
    """A grant covering 43 regions covers the one you asked about."""
    rows = readiness([L4], region="us-central1")
    assert [(r.gpu, r.region) for r in rows] == [("L4", "us-central1")]


def test_a_region_outside_the_grant_is_dropped():
    limited = quota("NVIDIA-L4-GPUS-per-project-region", 1, ["us-central1"])
    assert readiness([limited], region="europe-west4") == []


def test_resolve_turns_a_typed_name_into_a_real_id():
    assert resolve("l4", [L4, A100]) == "NVIDIA-L4-GPUS-per-project-region"


def test_resolve_never_returns_a_committed_allowance():
    committed = quota("COMMITTED-NVIDIA-L4-GPUS-per-project-region", 8)
    assert resolve("l4", [committed]) is None


def test_resolve_returns_none_for_a_card_this_project_never_reports():
    assert resolve("h100", [L4, A100]) is None


def test_resolve_accepts_a_region_inside_the_grant():
    assert resolve("l4", [L4], region="us-central1") == "NVIDIA-L4-GPUS-per-project-region"


def test_available_gpus_is_what_an_error_message_offers():
    assert available_gpus([L4, A100, quota("CPUS-per-project-region", 8)]) == ["A100", "L4"]


def test_no_dimensions_at_all_is_survivable():
    assert readiness([{"quotaId": "NVIDIA-L4-GPUS-per-project-region"}]) == []


def test_a_card_metered_per_region_and_per_zone_is_one_row():
    """Live projects carry both `-per-project-region` and `-per-project-zone` ids
    for the same card. Two identical rows read as a bug."""
    rows = readiness([
        quota("NVIDIA-L4-GPUS-per-project-region", 1),
        quota("NVIDIA-L4-GPUS-per-project-zone", 1),
    ])
    assert len(rows) == 1
    assert rows[0].gpu == "L4"


def test_the_region_scoped_row_binds_even_when_the_zone_one_is_larger():
    """THIS TEST USED TO ASSERT THE OPPOSITE, and it was pinning a disagreement
    between two surfaces rather than a property of the API.

        assert [(r.limit, r.status) for r in rows] == [(4, "ready")]

    `allowance()` — which is what `create` consults before ordering a box — reads
    the same two rows through `_for_card` and answers **0**, because
    `_prefer_region_scope` drops the zone-scoped copy: "this one has L4 at 1
    across 43 named regions and an unlimited per-zone allowance across the 130
    zones inside them. Read together, the card looks unlimited and available in a
    region the project has no grant in. The region-scoped rows are the ones that
    bind."

    So `quota list` printed "4 ready" about a card `create` refused to order at
    0. Measured, not argued: `allowance("t4", [region 0, zone 4])` returns 0
    today and did before this change. `readiness` was the one surface not
    applying the module's own rule, which is also why a card metered both ways
    produced two rows whose counts `summarise` then added together — the "never
    asked in 172" on a project with 43 regions.
    """
    rows = readiness([
        quota("NVIDIA-T4-GPUS-per-project-region", 0),
        quota("NVIDIA-T4-GPUS-per-project-zone", 4),
    ])
    assert [(r.limit, r.status) for r in rows] == [(0, "none")]

    from comfy_qa.quota import allowance

    assert allowance("t4", [
        quota("NVIDIA-T4-GPUS-per-project-region", 0),
        quota("NVIDIA-T4-GPUS-per-project-zone", 4),
    ]) == 0, "the surface this was made to agree with"


def test_a_card_metered_only_by_zone_is_still_seen():
    """The other half of the rule: preferring region scope must not hide a card
    that has no region-scoped row at all."""
    rows = readiness([quota("NVIDIA-T4-GPUS-per-project-zone", 4)])
    assert [(r.limit, r.status) for r in rows] == [(4, "ready")]


def test_a_card_metered_in_25_regions_is_still_one_line():
    """K80 comes back as 25 separate per-region entries on a live project.

    A row per region ran the table to 130 lines and told you nothing that one
    line per card does not.
    """
    per_region = {
        "quotaId": "NVIDIA-K80-GPUS-per-project-region",
        "dimensionsInfos": [
            {"dimensions": {"region": name}, "details": {"value": "1"},
             "applicableLocations": [name]}
            for name in REGIONS
        ],
    }
    cards = summarise(readiness([per_region]))
    assert len(cards) == 1
    assert cards[0].gpu == "K80"
    assert cards[0].where == f"{len(REGIONS)} regions"


def test_a_grant_covering_everywhere_says_so():
    cards = summarise(readiness([L4]))
    assert [(c.gpu, c.where, c.status) for c in cards] == [("L4", "7 regions", "ready")]


def test_one_region_is_named_rather_than_counted():
    single = quota("NVIDIA-T4-GPUS-per-project-region", 2, ["us-central1"])
    assert summarise(readiness([single]))[0].where == "us-central1"


def test_a_card_ready_somewhere_is_ready():
    """Granted in one region and absent elsewhere still means you can run it."""
    mixed = {
        "quotaId": "NVIDIA-T4-GPUS-per-project-region",
        "dimensionsInfos": [
            {"dimensions": {"region": "us-central1"}, "details": {"value": "4"},
             "applicableLocations": ["us-central1"]},
            {"dimensions": {"region": "europe-west4"}, "details": {},
             "applicableLocations": ["europe-west4"]},
        ],
    }
    card = summarise(readiness([mixed]))[0]
    assert card.status == "ready"
    assert card.limit == 4
    assert card.where == "us-central1", "the place it is ready, not the place it is not"


def test_summary_orders_ready_then_pending_then_none():
    cards = summarise(readiness([T4, L4, A100], preferences=[
        {"quotaId": "NVIDIA-A100-GPUS-per-project-region",
         "quotaConfig": {"preferredValue": 1}},
    ]))
    assert [c.status for c in cards] == ["ready", "pending", "none"]


def test_the_global_allowance_says_global_not_all_regions():
    """It is one project-wide ceiling, not a grant in every region."""
    assert summarise(readiness([GLOBAL]))[0].where == "global"


# --- a preference is a state, not a presence ---------------------------------
#
# THESE ARE SHAPES, NOT A SNAPSHOT — and the difference is the point.
#
# They were introduced as "the five rows really on this project, read on
# 2026-09-17". By the end of that same day the project had SEVEN, two of the four
# fixtures no longer matched the row they named (the ceiling had moved from
# approved-at-1 to denied-at-2; the A100 row that stood for "pending" had been
# refused), and nothing on the project was pending at all. A provenance claim
# that decays within hours of being written is the same defect class as every
# other confident sentence corrected tonight.
#
# So the claim is now what it should always have been: each fixture is a SHAPE
# the live API produces — satisfied, denied, denied-with-no-stateDetail,
# unanswered — chosen because the state machine has to tell them apart. Which
# rows a given project holds is a fact about that project on a given evening and
# belongs in a report, not in a test file.
#
# For the record at the time of writing: seven preferences, six of them denied.

from comfy_qa.quota import asks, denied_ids, global_quota_id, pending_ids

DENIED_PREF = {
    "quotaId": "NVIDIA-A100-80GB-GPUS-per-project-region",
    "dimensions": {"region": "europe-west4"},
    "quotaConfig": {"grantedValue": "0", "preferredValue": "1",
                    "stateDetail": "Quota request denied"},
}
APPROVED_PREF = {
    # Asked for and granted in full. The live ceiling was this shape when these
    # were written and has since become granted-1-of-2-denied; the shape is what
    # the state machine needs, so it stays.
    "quotaId": "GPUS-ALL-REGIONS-per-project",
    "quotaConfig": {"grantedValue": "1", "preferredValue": "1",
                    "stateDetail": "Quota request approved to 1"},
}
SILENTLY_GRANTED_PREF = {
    # The live L4 row carries NO stateDetail at all. Reading the state out of
    # that string alone would leave this one unclassifiable.
    "quotaId": "NVIDIA-L4-GPUS-per-project-region",
    "dimensions": {"region": "us-central1"},
    "quotaConfig": {"grantedValue": "1", "preferredValue": "1"},
}
WAITING_PREF = {
    # Unanswered. Nothing on the live project is in this state any more — every
    # request has been refused — which is exactly why it has to be a fixture:
    # the branch is real and the project stopped exercising it.
    "quotaId": "NVIDIA-A100-GPUS-per-project-region",
    "dimensions": {"region": "us-central1"},
    "quotaConfig": {"grantedValue": "0", "preferredValue": "1"},
}

LIVE_PREFS = [APPROVED_PREF, SILENTLY_GRANTED_PREF, DENIED_PREF, WAITING_PREF]

A100_80GB_ZERO = {
    "quotaId": "NVIDIA-A100-80GB-GPUS-per-project-region",
    "dimensionsInfos": [{"details": {"value": "0"},
                         "applicableLocations": ["europe-west4"]}],
}


def test_a_refusal_is_not_reported_as_still_waiting():
    """The defect: `preferredValue > 0` was the whole test, so a request Google
    answered on 2026-08-05 read as pending for a year."""
    assert denied_ids(LIVE_PREFS) == {"NVIDIA-A100-80GB-GPUS-per-project-region"}
    assert "NVIDIA-A100-80GB-GPUS-per-project-region" not in pending_ids(LIVE_PREFS)


def test_only_the_unanswered_request_is_pending():
    assert pending_ids(LIVE_PREFS) == {"NVIDIA-A100-GPUS-per-project-region"}


def test_a_granted_request_with_no_state_detail_is_still_granted():
    """Two of the four live shapes carry no `stateDetail`. Deciding the state
    from that string alone leaves the commonest row unreadable."""
    state = {a.quota_id: a.state for a in asks(LIVE_PREFS)}
    assert state["NVIDIA-L4-GPUS-per-project-region"] == "satisfied"
    assert state["GPUS-ALL-REGIONS-per-project"] == "satisfied"


def test_a_denial_at_a_number_you_now_hold_is_history():
    """Satisfied is decided before denied, deliberately. What Google said last
    month about a value the project has since reached is not an obstacle."""
    overtaken = dict(DENIED_PREF,
                     quotaConfig={"grantedValue": "1", "preferredValue": "1",
                                  "stateDetail": "Quota request denied"})
    assert asks([overtaken])[0].state == "satisfied"


def test_a_denied_row_is_no_longer_shown_as_waiting_on_google():
    """`readiness` reads pending through the same helper, so the correction has
    to reach the table a person actually looks at — not only the new caller."""
    rows = readiness([A100_80GB_ZERO], preferences=[DENIED_PREF])
    assert [r.status for r in rows] == ["denied"], (
        "quota list still says 'pending — waiting on Google' about a refusal")
    # It was `none` for a while, which stopped the "pending" lie and started a
    # worse one — `none` renders as "request it", so the table invited the user
    # to re-file a request Google had just refused.
    assert rows[0].status != "none"


def test_a_malformed_preference_does_not_take_the_read_down():
    assert asks([{"quotaConfig": {}}, {"quotaId": "X", "quotaConfig": None}])


def test_the_ceiling_id_never_resolves_to_the_zone_scoped_copy():
    """A project carries both, `friendly_name` maps them to the same label, and
    asking Google to raise the zone-scoped one is a different request."""
    both = [
        {"quotaId": "GPUS-ALL-REGIONS-per-project-zone",
         "dimensionsInfos": [{"details": {"value": "-1"}}]},
        {"quotaId": "GPUS-ALL-REGIONS-per-project",
         "dimensionsInfos": [{"details": {"value": "1"}}]},
    ]
    assert global_quota_id(both) == "GPUS-ALL-REGIONS-per-project"
    assert global_quota_id(list(reversed(both))) == "GPUS-ALL-REGIONS-per-project"


def test_resolve_never_returns_the_zone_scoped_copy_either():
    """Same shape one layer over, and this one decides what `quota request` and
    `setup` actually send. It returned whichever gcloud listed first."""
    both = [
        {"quotaId": "NVIDIA-L4-GPUS-per-project-zone",
         "dimensionsInfos": [{"details": {"value": "-1"},
                              "applicableLocations": ["us-central1-a"]}]},
        {"quotaId": "NVIDIA-L4-GPUS-per-project-region",
         "dimensionsInfos": [{"details": {"value": "1"},
                              "applicableLocations": ["us-central1"]}]},
    ]
    assert resolve("l4", both) == "NVIDIA-L4-GPUS-per-project-region"
    assert resolve("l4", list(reversed(both))) == "NVIDIA-L4-GPUS-per-project-region"
