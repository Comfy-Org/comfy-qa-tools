"""Which places a quota actually covers.

`all regions` is this module's label for "several places at once" — it is what a
single allowance spanning forty-three regions collapses to. It was then read back
as "everywhere", so asking about a region the grant does not name returned the
grant anyway, relabelled as the region asked about. That is a quota report of a
grant the project does not have, and a quota request Google will refuse.
"""

from __future__ import annotations

import pytest

from comfy_qa.quota import readiness, resolve, summarise

SOME = ["us-central1", "us-east1", "europe-west4"]


def quota(quota_id, value, locations):
    return {
        "quotaId": quota_id,
        "dimensionsInfos": [{
            "dimensions": None,
            "details": {"value": str(value)},
            "applicableLocations": list(locations),
        }],
    }


MANY = quota("NVIDIA-L4-GPUS-per-project-region", 1, SOME)
ONE = quota("NVIDIA-T4-GPUS-per-project-region", 2, ["us-central1"])
GLOBAL = quota("GPUS-ALL-REGIONS-per-project", 4, ["global"])


@pytest.mark.parametrize("region", SOME)
def test_a_region_inside_a_many_region_grant_is_reported(region):
    rows = readiness([MANY], region=region)
    assert [(r.gpu, r.region, r.limit) for r in rows] == [("L4", region, 1)]


def test_a_region_outside_a_many_region_grant_is_not_invented():
    assert readiness([MANY], region="asia-east2") == []


def test_a_region_outside_a_single_region_grant_is_still_dropped():
    assert readiness([ONE], region="europe-west4") == []


def test_the_project_wide_ceiling_applies_wherever_you_ask():
    rows = readiness([GLOBAL], region="asia-east2")
    assert [(r.gpu, r.region) for r in rows] == [("any (global)", "global")]


def test_without_a_region_nothing_is_filtered_out():
    rows = readiness([MANY, ONE, GLOBAL])
    assert {r.gpu for r in rows} == {"L4", "T4", "any (global)"}


def test_the_summary_of_a_narrowed_read_names_the_region_asked_about():
    cards = summarise(readiness([MANY], region="us-east1"))
    assert [(c.gpu, c.where, c.limit) for c in cards] == [("L4", "us-east1", 1)]


def test_resolve_will_not_build_a_request_for_a_region_the_card_is_absent_from():
    """The id resolved fine, so the request went in with `--dimensions=region=`
    naming a region that quota does not cover."""
    assert resolve("l4", [MANY], region="asia-east2") is None
    assert resolve("l4", [MANY], region="us-east1") == "NVIDIA-L4-GPUS-per-project-region"


def test_resolve_still_answers_when_no_region_was_named():
    assert resolve("t4", [ONE]) == "NVIDIA-T4-GPUS-per-project-region"


def test_a_quota_record_with_no_places_at_all_is_still_resolvable():
    """`resolve` is how a request gets built; a record the API returned without
    dimensions is not a reason to refuse to ask for it."""
    bare = {"quotaId": "NVIDIA-L4-GPUS-per-project-region"}
    assert resolve("l4", [bare]) == "NVIDIA-L4-GPUS-per-project-region"
