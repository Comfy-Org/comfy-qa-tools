"""v0's `env` check — the build and flag state of each deployed environment.

This command was carried into v1 untouched and never had a test. It still
works: probed live on 2026-08-26, all three cloud environments answered, the
`gh` SHA resolution resolved, and `--expect`, `--evidence` and `--flags` all
behaved as documented. What it did not do was fail honestly — an environment
that never answered still produced an evidence block that looked exactly like a
real capture, and a single environment reported its flags as "all identical
across environments" having compared them with nothing.

The flag payload is the shape `https://cloud.comfy.org/api/features` really
returns, secrets included, because the type whitelist is the only thing keeping
them out of a pasted evidence block.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest
import typer

from comfy_qa.commands import env_cmd
from comfy_qa.env import (
    CLOUD_ENVS,
    EnvReport,
    _boolean_flags,
    flag_diff,
    probe_cloud,
    probe_local,
    release_line,
)
from comfy_qa.render import as_dict, evidence, table

# Trimmed from the live /api/features payload. The nested dicts and the bare
# strings are the point: `mixpanel_token` and the Firebase `apiKey` are real
# credentials that the endpoint really serves next to the flags.
FEATURES = {
    "assets": True,
    "consolidated_billing_enabled": True,
    "team_workspaces_enabled": True,
    "mcp_server_access": False,
    "new_free_tier_subscriptions": True,
    "max_concurrent_jobs": 1,
    "max_upload_size": 104857600,
    "comfy_api_base_url": "https://api.comfy.org",
    "mixpanel_token": "c87ea58e03614bbdcc246387181924e0",
    "churnkey_app_id": "i8qpj3ycp",
    "firebase_config": {"apiKey": "AIzaSyC2-fomLqgCjb7ELwta1I9cEarPK8ziTGs",
                        "projectId": "dreamboothy"},
    "customer_io": {"site_id": "f87746f8c188c8ddcf41"},
}

# A real /system_stats body from a local ComfyUI, cut to what `env` reads.
LOCAL_STATS = {
    "system": {
        "comfyui_version": "0.28.3",
        "required_frontend_version": "1.45.21",
        "comfy_package_versions": [
            {"name": "comfyui-frontend-package", "installed": "1.45.21",
             "required": "1.45.21"},
            {"name": "comfyui-workflow-templates", "installed": "0.11.15"},
        ],
    },
    "devices": [],
}


class Body(io.BytesIO):
    """What `_get` hands back: readable, and a context manager."""

    def __init__(self, payload, headers=None):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        super().__init__(raw)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_get(responses):
    """Route each URL to a payload, or to an exception to raise."""
    def _get(url, *, head=False):
        for suffix, result in responses.items():
            if url.endswith(suffix):
                if isinstance(result, Exception):
                    raise result
                return result
        raise urllib.error.URLError("no route")
    return _get


# --------------------------------------------------------------------------
# Flags: a whitelist by type, because the endpoint also serves secrets
# --------------------------------------------------------------------------

def test_only_booleans_survive_so_no_secret_can_be_pasted():
    flags = _boolean_flags(FEATURES)
    assert flags["team_workspaces_enabled"] is True
    assert flags["mcp_server_access"] is False
    joined = json.dumps(flags)
    for secret in ("mixpanel_token", "AIzaSy", "churnkey_app_id",
                   "site_id", "api.comfy.org"):
        assert secret not in joined
    assert "max_concurrent_jobs" not in flags  # an int, not a flag


def test_a_features_endpoint_that_returns_a_list_is_not_a_crash():
    """`payload.items()` on a list raised AttributeError past every handler."""
    assert _boolean_flags([1, 2, 3]) == {}
    assert _boolean_flags(None) == {}
    assert _boolean_flags("nope") == {}


# --------------------------------------------------------------------------
# Probing a cloud environment
# --------------------------------------------------------------------------

def test_a_cloud_environment_reports_its_build_and_its_flags(monkeypatch):
    monkeypatch.setattr("comfy_qa.env._get", fake_get({
        "testcloud.comfy.org/": Body(b"", {"x-frontend-version": "c143e62b55cec754"}),
        "/api/features": Body(FEATURES),
    }))
    r = probe_cloud("testcloud", CLOUD_ENVS["testcloud"], resolve=False)
    assert r.error is None
    assert r.short_sha == "c143e62b"
    assert r.flags["assets"] is True


def test_an_environment_that_answers_403_is_not_called_unreachable(monkeypatch):
    """"unreachable" sends a tester to check a network that is working."""
    error = urllib.error.HTTPError("https://cloud.comfy.org/", 403, "no", {}, None)
    monkeypatch.setattr("comfy_qa.env._get", fake_get({"comfy.org/": error}))
    r = probe_cloud("cloud", CLOUD_ENVS["cloud"], resolve=False)
    assert r.error == "answered HTTP 403"


def test_an_environment_that_does_not_answer_at_all_says_unreachable(monkeypatch):
    monkeypatch.setattr("comfy_qa.env._get",
                        fake_get({"comfy.org/": urllib.error.URLError("refused")}))
    r = probe_cloud("cloud", CLOUD_ENVS["cloud"], resolve=False)
    assert r.error.startswith("unreachable:")


def test_missing_flags_do_not_fail_the_probe(monkeypatch):
    """Flags are a bonus. A build with no /api/features is still a good probe."""
    monkeypatch.setattr("comfy_qa.env._get", fake_get({
        "comfy.org/": Body(b"", {"x-frontend-version": "abc12345deadbeef"}),
        "/api/features": urllib.error.URLError("404"),
    }))
    r = probe_cloud("cloud", CLOUD_ENVS["cloud"], resolve=False)
    assert r.error is None
    assert r.flags == {}


# --------------------------------------------------------------------------
# Probing the local ComfyUI
# --------------------------------------------------------------------------

def test_the_local_probe_reads_the_installed_and_required_frontend(monkeypatch):
    monkeypatch.setattr("comfy_qa.env._get",
                        fake_get({"/system_stats": Body(LOCAL_STATS)}))
    r = probe_local()
    assert r.comfyui_version == "0.28.3"
    assert r.frontend_installed == "1.45.21"
    assert r.frontend_required == "1.45.21"
    assert r.frontend_mismatch is False


def test_a_frontend_the_core_does_not_want_is_flagged(monkeypatch):
    stats = json.loads(json.dumps(LOCAL_STATS))
    stats["system"]["required_frontend_version"] = "1.46.0"
    monkeypatch.setattr("comfy_qa.env._get",
                        fake_get({"/system_stats": Body(stats)}))
    assert probe_local().frontend_mismatch is True


@pytest.mark.parametrize("body", [
    {"system": None, "devices": []},          # the key is there, the value is not
    [],                                        # not an object at all
    {"devices": []},                           # no system key
    "unavailable",
])
def test_a_system_block_that_is_not_an_object_is_reported_not_raised(
        monkeypatch, body):
    """`json.load(resp).get("system", {})` returned None, and `.get` on None
    raised AttributeError straight past every handler in this module."""
    monkeypatch.setattr("comfy_qa.env._get",
                        fake_get({"/system_stats": Body(body)}))
    r = probe_local()
    assert r.error == "answered, but not with ComfyUI's /system_stats"


def test_package_versions_that_are_not_a_list_of_objects_do_not_crash(monkeypatch):
    stats = {"system": {"comfyui_version": "0.33.4",
                        "comfy_package_versions": ["comfyui-frontend-package"]}}
    monkeypatch.setattr("comfy_qa.env._get",
                        fake_get({"/system_stats": Body(stats)}))
    r = probe_local()
    assert r.error is None
    assert r.frontend_installed is None


def test_a_local_comfyui_that_is_not_running_says_so(monkeypatch):
    monkeypatch.setattr("comfy_qa.env._get",
                        fake_get({"/system_stats": urllib.error.URLError("refused")}))
    assert probe_local().error.startswith("not running")


# --------------------------------------------------------------------------
# Comparing environments
# --------------------------------------------------------------------------

def cloud_report(name, sha="abc12345deadbeef", **flags):
    return EnvReport(name=name, url=f"https://{name}.comfy.org", kind="cloud",
                     sha=sha, flags=flags)


def test_only_the_flags_that_actually_differ_are_reported():
    reports = [cloud_report("testcloud", a=True, b=True),
               cloud_report("cloud", a=True, b=False)]
    assert flag_diff(reports) == {"b": {"testcloud": True, "cloud": False}}


def test_one_environment_alone_is_never_a_comparison():
    assert flag_diff([cloud_report("cloud", a=True)]) == {}


def test_a_single_environment_does_not_claim_its_flags_match_anything():
    """"all identical across environments" off one probe is an unearned claim."""
    out = table([cloud_report("testcloud", a=True, b=False)])
    assert "nothing to compare against" in out
    assert "all identical" not in out


def test_two_matching_environments_may_say_so():
    out = table([cloud_report("testcloud", a=True), cloud_report("cloud", a=True)])
    assert "1 flags checked, all identical across environments" in out


def test_the_flag_count_is_the_union_not_the_first_environments_total():
    """When environments disagree about which flags exist, quoting one of their
    totals understates what was compared."""
    out = table([cloud_report("testcloud", a=True, b=True),
                 cloud_report("cloud", a=True, c=True)])
    assert "3 flags checked" in out


def test_a_local_build_with_no_version_says_unknown_not_none():
    """An older ComfyUI has no `comfyui_version`, and "ComfyUI None" has been
    pasted into a report as if it meant something."""
    out = table([EnvReport(name="local", url="http://127.0.0.1:8188", kind="local")])
    assert "ComfyUI unknown" in out
    assert "None" not in out


def test_the_release_line_is_read_from_the_commit_subject():
    r = cloud_report("cloud")
    r.commit_subject = "1.51.9 (#15398)"
    assert release_line([r]) == "1.51"


# --------------------------------------------------------------------------
# The evidence block
# --------------------------------------------------------------------------

def test_an_environment_that_never_answered_gets_no_evidence_block():
    """This is the one that matters. A failed probe used to render as
    "build None · unknown" — which looks exactly like a real capture and gets
    pasted into a bug report as one."""
    failed = EnvReport(name="cloud", url="https://cloud.comfy.org", kind="cloud",
                       error="unreachable: [Errno 8] nodename nor servname provided")
    blocks = evidence(failed)
    for shape in blocks.values():
        assert "NOT PROBED" in shape
        assert "None" not in shape


def test_a_local_environment_with_nothing_reported_says_unknown():
    blocks = evidence(EnvReport(name="local", url="http://127.0.0.1:8188",
                                kind="local"))
    assert "ComfyUI unknown · frontend unknown" in blocks["oneline"]
    assert "None" not in blocks["playbook"]


def test_named_flags_are_the_ones_that_appear():
    r = cloud_report("testcloud", team_workspaces_enabled=True,
                     mcp_server_access=False, assets=True)
    blocks = evidence(r, only=["team_workspaces_enabled", "mcp_server_access"])
    assert ("team_workspaces_enabled ON + mcp_server_access OFF"
            in blocks["oneline"])
    assert "assets" not in blocks["oneline"]


def test_an_environment_is_summarised_by_how_it_differs_from_production():
    """Listing all 33 flags is unusable in a Slack line."""
    baseline = cloud_report("cloud", a=True, b=True)
    other = cloud_report("testcloud", a=True, b=False)
    assert "b OFF (differs from cloud; 2 flags checked)" in (
        evidence(other, baseline=baseline)["oneline"])


def test_an_environment_matching_production_says_only_that():
    baseline = cloud_report("cloud", a=True, b=True)
    other = cloud_report("testcloud", a=True, b=True)
    assert "same as cloud (2 flags checked)" in evidence(other,
                                                         baseline=baseline)["oneline"]


def test_the_json_shape_drops_what_was_never_learned():
    data = as_dict([cloud_report("testcloud", a=True)])
    assert data["environments"][0]["short_sha"] == "abc12345"
    assert "commit_subject" not in data["environments"][0]
    assert "error" not in data["environments"][0]


# --------------------------------------------------------------------------
# The command itself
# --------------------------------------------------------------------------

def only_local(monkeypatch, report):
    monkeypatch.setattr("comfy_qa.commands.collect", lambda *a, **k: [report])


def test_an_unknown_environment_is_refused_before_any_network_call(monkeypatch):
    monkeypatch.setattr("comfy_qa.commands.collect",
                        lambda *a, **k: pytest.fail("should not probe"))
    with pytest.raises(typer.Exit) as caught:
        env_cmd(targets=["prodcloud"])
    assert caught.value.exit_code == 2


def test_asking_for_json_and_an_evidence_block_at_once_is_refused(monkeypatch):
    """One silently threw the other away."""
    only_local(monkeypatch, EnvReport(name="local", url="u", kind="local"))
    with pytest.raises(typer.Exit) as caught:
        env_cmd(as_json=True, evidence_for="local")
    assert caught.value.exit_code == 2


def test_expect_needs_one_cloud_environment_and_names_the_right_binary(
        monkeypatch, capsys):
    """The hint said `comfy qa env`, which is v0's binary and exits 2."""
    only_local(monkeypatch, EnvReport(name="local", url="u", kind="local"))
    with pytest.raises(typer.Exit) as caught:
        env_cmd(expect="deadbeef")
    assert caught.value.exit_code == 2
    err = capsys.readouterr().err
    assert "comfy-qat env" in err
    assert "comfy qa env" not in err


def test_expect_passes_when_the_environment_serves_that_sha(monkeypatch, capsys):
    only_local(monkeypatch, cloud_report("testcloud", sha="c143e62b55cec754"))
    env_cmd(targets=["testcloud"], expect="c143e62b", no_local=True)
    assert "OK" in capsys.readouterr().out


def test_expect_fails_when_the_deploy_did_not_land(monkeypatch):
    """The whole point: a failed deploy leaves the old build running."""
    only_local(monkeypatch, cloud_report("testcloud", sha="0000000000000000"))
    with pytest.raises(typer.Exit) as caught:
        env_cmd(targets=["testcloud"], expect="c143e62b", no_local=True)
    assert caught.value.exit_code == 1


def test_a_cloud_environment_that_failed_makes_the_command_fail(monkeypatch):
    only_local(monkeypatch, EnvReport(name="cloud", url="u", kind="cloud",
                                      error="unreachable: refused"))
    with pytest.raises(typer.Exit) as caught:
        env_cmd(targets=["cloud"], no_local=True)
    assert caught.value.exit_code == 1


def test_a_local_comfyui_that_is_down_does_not_fail_the_command(monkeypatch):
    """Not everyone running this has a ComfyUI up, and that is not a deploy fault."""
    only_local(monkeypatch, EnvReport(name="local", url="u", kind="local",
                                      error="not running (refused)"))
    env_cmd(targets=["local"])


def test_asking_for_an_environment_that_was_not_probed_is_an_error(monkeypatch):
    only_local(monkeypatch, EnvReport(name="local", url="u", kind="local"))
    with pytest.raises(typer.Exit) as caught:
        env_cmd(evidence_for="testcloud")
    assert caught.value.exit_code == 2
