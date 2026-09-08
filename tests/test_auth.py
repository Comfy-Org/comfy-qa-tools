"""Readiness checks and the quota wait.

Every gcloud call goes through one seam, so these tests replace that seam rather
than patching subprocess. Nothing here touches the network or a real project.
"""

from __future__ import annotations

import pytest

from comfy_qa.auth import _value_of, run_checks, wait_for_quota
from comfy_qa.gcloud import Gcloud, GcloudError, console_quota_url, quota_request_command


def fake(**responses):
    """Build a Gcloud whose calls are answered from a dict of arg-prefix -> value."""

    def runner(args, parse_json):
        key = " ".join(args)
        for prefix, value in responses.items():
            if key.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected gcloud call: {key}")

    return Gcloud(runner=runner)


# `status` reports whether gcloud's own python has numpy, because tunnel speed is
# readiness. A path that does not exist makes the check a no-op, which is what a
# test wants — nothing should be installed by running one.
GCLOUD_PY = {"info --format=value(basic.python_location)": "/no/such/python"}
SIGNED_IN = {"auth list": [{"account": "ali@comfy.org", "status": "ACTIVE"}]}
PROJECT = {"config get-value project": "proj-1"}
BILLED = {"billing projects describe": {"billingEnabled": True}}
QUOTA = {"quotas info list": [
    {"quotaId": "NVIDIA_L4_GPUS-per-project-region",
     "dimensionsInfos": [{"details": {"value": 1}}]},
]}


def test_all_green():
    checks = run_checks(fake(**GCLOUD_PY, **SIGNED_IN, **PROJECT, **BILLED, **QUOTA))
    assert [c.name for c in checks] == ["gcloud", "account", "project", "billing", "gpu quota", "numpy"]
    assert all(c.ok for c in checks)


def test_stops_at_the_first_failure():
    checks = run_checks(fake(**{"auth list": []}))
    assert [c.name for c in checks] == ["gcloud", "account"]
    assert checks[-1].ok is False
    assert checks[-1].fix == "gcloud auth login"


def test_unset_project_is_not_a_project():
    checks = run_checks(fake(**GCLOUD_PY, **SIGNED_IN, **{"config get-value project": "(unset)"}))
    assert checks[-1].name == "project"
    assert not checks[-1].ok


def test_unbilled_project_stops_before_quota():
    checks = run_checks(fake(
        **GCLOUD_PY, **SIGNED_IN, **PROJECT, **{"billing projects describe": {"billingEnabled": False}},
    ))
    assert checks[-1].name == "billing"
    assert not checks[-1].ok


def test_zero_gpu_quota_is_a_failure_with_the_fix():
    checks = run_checks(fake(
        **GCLOUD_PY, **SIGNED_IN, **PROJECT, **BILLED,
        **{"quotas info list": [{"quotaId": "NVIDIA_L4_GPUS-per-project-region",
                                 "dimensionsInfos": [{"details": {"value": 0}}]}]},
    ))
    assert checks[-1].name == "gpu quota"
    assert not checks[-1].ok
    assert "quota request" in checks[-1].fix


def test_non_gpu_quotas_are_ignored():
    gc = fake(**{"quotas info list": [
        {"quotaId": "CPUS-per-project-region", "dimensionsInfos": [{"details": {"value": 8}}]},
        {"quotaId": "NVIDIA_L4_GPUS-per-project-region", "dimensionsInfos": [{"details": {"value": 2}}]},
    ]})
    assert [q["quotaId"] for q in gc.gpu_quotas("p")] == ["NVIDIA_L4_GPUS-per-project-region"]


@pytest.mark.parametrize("quota,expected", [
    ({"dimensionsInfos": [{"details": {"value": 4}}]}, 4),
    ({"dimensionsInfos": [{"details": {"value": "7"}}]}, 7),
    ({"dimensionsInfos": [{"details": {}}]}, 0),
    ({}, 0),
    ({"dimensionsInfos": [{"details": {"value": 1}}, {"details": {"value": 5}}]}, 5),
])
def test_value_of_tolerates_shape(quota, expected):
    assert _value_of(quota) == expected


def test_request_command_is_built_not_guessed():
    args = quota_request_command(
        project="p", quota_id="NVIDIA_L4_GPUS-per-project-region", value=2,
        region="us-central1", justification="QA",
    )
    assert args[:3] == ["quotas", "preferences", "create"]
    assert "--service=compute.googleapis.com" in args
    assert "--preferred-value=2" in args
    assert "--dimensions=region=us-central1" in args
    assert "--justification=QA" in args


def test_request_command_omits_region_when_global():
    args = quota_request_command(project="p", quota_id="GPUS_ALL_REGIONS-per-project", value=2)
    assert not any(a.startswith("--dimensions") for a in args)


def test_console_url_names_the_project():
    assert "project=p" in console_quota_url("p")


def test_wait_returns_as_soon_as_granted():
    values = iter([0, 0, 3])
    slept = []
    assert wait_for_quota(
        lambda: next(values), wanted=2,
        sleep=slept.append, now=lambda: 0.0,
    ) is True
    assert len(slept) == 2


def test_wait_gives_up_at_the_deadline_rather_than_hanging():
    clock = iter([0.0, 10.0, 20.0, 30.0, 40.0])
    assert wait_for_quota(
        lambda: 0, wanted=1, timeout=25,
        sleep=lambda _: None, now=lambda: next(clock),
    ) is False


def test_a_transient_read_failure_is_not_a_denial():
    calls = {"n": 0}

    def poll():
        calls["n"] += 1
        if calls["n"] == 1:
            raise GcloudError("network blip")
        return 5

    assert wait_for_quota(poll, wanted=1, sleep=lambda _: None, now=lambda: 0.0) is True


REAUTH = """ERROR: (gcloud.billing.projects.describe) There was a problem refreshing your current auth tokens: Reauthentication failed. cannot prompt during non-interactive execution.
Please run:

  $ gcloud auth login

to obtain new credentials.

If you have already logged in with a different account, run:

  $ gcloud config set account ACCOUNT

to select an already authenticated account to use."""


def test_expired_session_is_named_not_quoted_from_the_last_line():
    """The real failure this caught: the last line is a useless fragment."""
    from comfy_qa.gcloud import explain_failure

    message, fix, _ = explain_failure(REAUTH, "", 1)
    assert message == "your gcloud session has expired"
    assert fix == "gcloud auth login"
    assert "already authenticated account" not in message


def test_error_line_wins_over_the_first_line():
    from comfy_qa.gcloud import explain_failure

    message, _, _ = explain_failure("Updates are available.\nERROR: (gcloud.foo) it broke", "", 1)
    assert message == "it broke"


def test_empty_output_still_says_something():
    from comfy_qa.gcloud import explain_failure

    message, fix, _ = explain_failure("", "", 7)
    assert "7" in message and fix is None


def test_expired_session_surfaces_its_own_fix_through_the_checks():
    gc = fake(**GCLOUD_PY, **SIGNED_IN, **PROJECT,
              **{"billing projects describe": GcloudError("your gcloud session has expired",
                                                          fix="gcloud auth login")})
    checks = run_checks(gc)
    assert checks[-1].name == "billing"
    assert checks[-1].fix == "gcloud auth login"


def test_a_compute_error_summary_of_dashes_is_not_the_message():
    """gcloud prints `ERROR: (gcloud.compute.instances.start) ---` and puts the
    real sentence further down. Reporting `---` tells nobody anything."""
    from comfy_qa.gcloud import explain_failure, localized_message

    raw = (
        "ERROR: (gcloud.compute.instances.start) ---\n"
        "code: ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS\n"
        "errorDetails:\n"
        "- localizedMessage:\n"
        "    locale: en-US\n"
        "    message: A g2-standard-8 VM instance is currently\n"
        "      unavailable in the us-central1-a zone.\n"
    )
    message, _, text = explain_failure(raw, "", 1)
    assert message.startswith("A g2-standard-8 VM instance")
    assert "unavailable in the us-central1-a zone." in message
    assert text == raw.strip(), "the full output is kept for classification"
    assert localized_message("nothing here") is None


def test_status_does_not_fail_forever_over_a_tunnel_speed_it_cannot_fix(monkeypatch):
    """`status` exits 1 on any failed check. A root-owned gcloud python — which
    setup deliberately SKIPS rather than escalating to sudo — made that a
    permanent non-zero exit, with a fix line pointing at the command that had
    already declined.

    A slower tunnel is not a readiness failure. The row still says so.

    This does NOT cover `setup --no-numpy`, and an earlier version of this
    docstring claimed it did. That user still fails the check, correctly: they
    declined, nothing declined for them, and `comfy-qat setup` without the flag
    installs it. The sibling below pins that difference so the claim cannot
    drift back.

    `monkeypatch`, not a hand-rolled try/finally: the first version of this test
    restored the wrong module and left `setup.gcloud_numpy` patched for the rest
    of the session, which broke a test in another file.
    """
    from comfy_qa import setup as setup_module

    monkeypatch.setattr(
        setup_module, "gcloud_numpy",
        lambda gc: setup_module.GcloudNumpy(
            "/usr/bin/python3", "/usr", "its Python is not writable by you"),
    )
    checks = run_checks(fake(**GCLOUD_PY, **SIGNED_IN, **PROJECT, **BILLED,
                             **QUOTA))

    numpy = next(c for c in checks if c.name == "numpy")
    assert numpy.ok, "an install setup declined to make must not fail status"
    assert "not writable" in numpy.detail, "the row still says what is wrong"


def test_a_tunnel_speed_setup_could_fix_still_fails_status(monkeypatch):
    """The sibling of the test above, and the reason it exists.

    The two cases differ by one empty string — `blocked` — and the whole exit
    code turns on it, so a reader who saw only the first test would reasonably
    conclude that `numpy` never fails `status`. It does, and it should: NumPy
    absent from a WRITABLE gcloud python means either nobody has run `setup`
    yet or somebody ran it with `--no-numpy`, and in both cases the fix line is
    a command that actually works.

    Without this, `ok=True` unconditionally passes the test above and nothing
    notices. That is the shape we keep finding: a fix pinned only on the side it
    changed.
    """
    from comfy_qa import setup as setup_module

    monkeypatch.setattr(
        setup_module, "gcloud_numpy",
        lambda gc: setup_module.GcloudNumpy(
            "/usr/bin/python3", "/usr", ""),
    )
    checks = run_checks(fake(**GCLOUD_PY, **SIGNED_IN, **PROJECT, **BILLED,
                             **QUOTA))

    numpy = next(c for c in checks if c.name == "numpy")
    assert not numpy.ok, (
        "NumPy that setup CAN install is a real failing check — the fix works"
    )
    assert numpy.fix == "comfy-qat setup", "and it names the command that fixes it"
