"""Turning Google's quota ids into something a person can act on."""

from __future__ import annotations

import pytest

from comfy_qa.quota import (
    Readiness,
    available_gpus,
    friendly_name,
    matches,
    readiness,
    resolve,
)


def quota(quota_id, *rows):
    return {
        "quotaId": quota_id,
        "dimensionsInfos": [
            {"dimensions": {"region": region}, "details": {"value": limit}}
            for region, limit in rows
        ],
    }


L4 = quota("NVIDIA_L4_GPUS-per-project-region", ("us-central1", 1))
A100 = quota("NVIDIA_A100_GPUS-per-project-region", ("us-central1", 0))
T4 = quota("NVIDIA_T4_GPUS-per-project-region", ("us-central1", 0))


@pytest.mark.parametrize("quota_id,expected", [
    ("NVIDIA_L4_GPUS-per-project-region", "L4"),
    ("NVIDIA_A100_GPUS-per-project-region", "A100"),
    ("NVIDIA_H100_GPU-per-project-zone", "H100"),
    ("GPUS_ALL_REGIONS-per-project", "any (global)"),
    ("CPUS-per-project-region", None),
])
def test_friendly_names(quota_id, expected):
    assert friendly_name(quota_id) == expected


@pytest.mark.parametrize("quota_id", [
    "PREEMPTIBLE_NVIDIA_L4_GPUS-per-project-region",
    "COMMITTED_NVIDIA_A100_GPUS-per-project-region",
])
def test_preemptible_and_committed_are_not_offered(quota_id):
    """Real quotas, but holding one does not let you start an ordinary box.

    Listing them as available would tell someone they can run a card they cannot.
    """
    assert friendly_name(quota_id) is None


@pytest.mark.parametrize("typed", ["l4", "L4", "  l4  ", "l-4", "l_4"])
def test_user_spelling_is_forgiven(typed):
    assert matches(typed, "NVIDIA_L4_GPUS-per-project-region")


def test_l4_does_not_match_a100():
    assert not matches("l4", "NVIDIA_A100_GPUS-per-project-region")


def test_readiness_says_what_you_can_run_today():
    rows = readiness([L4, A100, T4], preferences=[
        {"quotaId": "NVIDIA_A100_GPUS-per-project-region",
         "quotaConfig": {"preferredValue": 1}},
    ])
    by_gpu = {r.gpu: r for r in rows}
    assert by_gpu["L4"].status == "ready"
    assert by_gpu["A100"].status == "pending"
    assert by_gpu["T4"].status == "none"


def test_ready_sorts_first_so_the_useful_rows_are_at_the_top():
    rows = readiness([T4, A100, L4])
    assert rows[0].gpu == "L4" and rows[0].status == "ready"


def test_usable_is_only_true_when_granted():
    rows = readiness([L4, T4])
    assert [r.usable for r in rows] == [True, False]


def test_region_filter_keeps_global_rows():
    multi = quota("NVIDIA_L4_GPUS-per-project-region",
                  ("us-central1", 1), ("europe-west4", 0))
    globally = {"quotaId": "GPUS_ALL_REGIONS-per-project",
                "dimensionsInfos": [{"dimensions": {}, "details": {"value": 2}}]}
    rows = readiness([multi, globally], region="us-central1")
    assert {(r.gpu, r.region) for r in rows} == {("L4", "us-central1"), ("any (global)", "global")}


def test_resolve_turns_a_typed_name_into_an_id():
    assert resolve("l4", [L4, A100]) == "NVIDIA_L4_GPUS-per-project-region"


def test_resolve_returns_none_for_a_card_this_project_never_reports():
    assert resolve("h100", [L4, A100]) is None


def test_resolve_respects_region():
    assert resolve("l4", [L4], region="europe-west4") is None


def test_available_gpus_is_what_an_error_message_offers():
    assert available_gpus([L4, A100, quota("CPUS-per-project-region", ("x", 1))]) == ["A100", "L4"]


def test_a_missing_value_reads_as_zero_not_a_crash():
    broken = {"quotaId": "NVIDIA_L4_GPUS-per-project-region",
              "dimensionsInfos": [{"dimensions": {"region": "r"}, "details": {}}]}
    assert readiness([broken])[0].status == "none"


def test_no_dimensions_at_all_is_survivable():
    assert readiness([{"quotaId": "NVIDIA_L4_GPUS-per-project-region"}]) == []
