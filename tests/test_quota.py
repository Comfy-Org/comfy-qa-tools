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
    """43 regions sharing an allowance is one row. Forty-three would be noise."""
    rows = readiness([L4])
    assert len(rows) == 1
    assert rows[0].region == "all regions"


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


def test_collapsing_keeps_the_larger_grant():
    rows = readiness([
        quota("NVIDIA-T4-GPUS-per-project-region", 0),
        quota("NVIDIA-T4-GPUS-per-project-zone", 4),
    ])
    assert [(r.limit, r.status) for r in rows] == [(4, "ready")]
