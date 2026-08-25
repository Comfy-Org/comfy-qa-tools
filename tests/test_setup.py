"""One command that gets a machine ready.

Setup is the path a new tester walks exactly once, usually alone, so its failure
modes matter more than most. These tests drive the whole flow with gcloud and the
human both replaced.
"""

from __future__ import annotations

import pytest

from comfy_qa.gcloud import Gcloud, GcloudError
from comfy_qa.setup import Prompts, SetupStopped, run_setup


def gcloud(**responses):
    def runner(args, mode):
        key = " ".join(args)
        for prefix, value in responses.items():
            if key.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected gcloud call: {key}")

    return Gcloud(runner=runner)


def prompts(confirm=True, answer="us-central1", pick=0):
    said = []
    p = Prompts(
        confirm=lambda q: confirm,
        ask=lambda q: answer,
        choose=lambda q, options: options[pick],
        say=said.append,
    )
    p.said = said  # type: ignore[attr-defined]
    return p


READY = {
    "auth list": [{"account": "ali@comfy.org", "status": "ACTIVE"}],
    "projects list": [{"projectId": "proj-1"}],
    "config get-value project": "proj-1",
    "billing projects describe": {"billingEnabled": True},
    "quotas info list": [
        {"quotaId": "NVIDIA_L4_GPUS-per-project-region",
         "dimensionsInfos": [{"details": {"value": 1}}]},
    ],
}

EXPIRED = GcloudError("your gcloud session has expired", fix="gcloud auth login")


def test_a_ready_machine_needs_no_questions(tmp_path):
    asked = []
    p = prompts()
    p.confirm = lambda q: asked.append(q) or True  # type: ignore[assignment]

    path = run_setup(gcloud(**READY), p, config_path=tmp_path / "hosts.toml")

    assert path.exists()
    assert asked == []


def test_an_expired_session_triggers_a_real_login(tmp_path):
    calls = []

    def runner(args, mode):
        key = " ".join(args)
        if key == "auth login":
            assert mode == "interactive", "login must attach the terminal, not capture it"
            calls.append("login")
            return 0
        if key.startswith("projects list") and not calls:
            raise EXPIRED
        for prefix, value in READY.items():
            if key.startswith(prefix):
                return value
        raise AssertionError(key)

    run_setup(Gcloud(runner=runner), prompts(), config_path=tmp_path / "hosts.toml")
    assert calls == ["login"]


def test_non_interactive_never_opens_a_browser(tmp_path):
    gc = gcloud(**dict(READY, **{"auth list": [], "projects list": EXPIRED}))
    with pytest.raises(SetupStopped) as caught:
        run_setup(gc, prompts(), interactive=False, config_path=tmp_path / "hosts.toml")
    assert caught.value.fix == "gcloud auth login"


def test_a_single_project_is_chosen_without_asking(tmp_path):
    responses = dict(READY, **{"config get-value project": "(unset)"})
    asked = []
    p = prompts()
    p.choose = lambda q, o: asked.append(q) or o[0]  # type: ignore[assignment]

    run_setup(gcloud(**responses, **{"config set project": ""}), p,
              config_path=tmp_path / "hosts.toml")
    assert asked == []


def test_several_projects_and_no_terminal_names_the_flag(tmp_path):
    responses = dict(READY, **{
        "config get-value project": "(unset)",
        "projects list": [{"projectId": "a"}, {"projectId": "b"}],
    })
    with pytest.raises(SetupStopped) as caught:
        run_setup(gcloud(**responses), prompts(), interactive=False,
                  config_path=tmp_path / "hosts.toml")
    assert "--project" in caught.value.fix


def test_an_account_with_no_projects_stops_with_somewhere_to_go(tmp_path):
    responses = dict(READY, **{
        "config get-value project": "(unset)", "projects list": [],
    })
    with pytest.raises(SetupStopped) as caught:
        run_setup(gcloud(**responses), prompts(), config_path=tmp_path / "hosts.toml")
    assert "projectcreate" in caught.value.fix


def test_unbilled_project_stops_because_nothing_can_start(tmp_path):
    responses = dict(READY, **{"billing projects describe": {"billingEnabled": False}})
    with pytest.raises(SetupStopped) as caught:
        run_setup(gcloud(**responses), prompts(), config_path=tmp_path / "hosts.toml")
    assert "billing" in str(caught.value)


def test_zero_quota_is_reported_but_does_not_stop_setup(tmp_path):
    """Quota can take days. Blocking setup on it would strand the tester."""
    responses = dict(READY, **{
        "quotas info list": [{"quotaId": "NVIDIA_L4_GPUS-per-project-region",
                              "dimensionsInfos": [{"details": {"value": 0}}]}],
        "quotas preferences create": {},
    })
    p = prompts(confirm=True)
    path = run_setup(gcloud(**responses), p, config_path=tmp_path / "hosts.toml")
    assert path.exists()
    assert any("requested" in line for line in p.said)


def test_declining_the_quota_request_still_finishes_setup(tmp_path):
    responses = dict(READY, **{
        "quotas info list": [{"quotaId": "X-GPUS", "dimensionsInfos": [{"details": {"value": 0}}]}],
    })
    path = run_setup(gcloud(**responses), prompts(confirm=False),
                     config_path=tmp_path / "hosts.toml")
    assert path.exists()


def test_an_existing_host_list_is_never_overwritten(tmp_path):
    config = tmp_path / "hosts.toml"
    config.write_text("# mine\n")
    run_setup(gcloud(**READY), prompts(), config_path=config)
    assert config.read_text() == "# mine\n"


def test_missing_gcloud_stops_before_anything_else(tmp_path):
    gc = Gcloud()
    gc.available = lambda: None  # type: ignore[method-assign]
    with pytest.raises(SetupStopped) as caught:
        run_setup(gc, prompts(), config_path=tmp_path / "hosts.toml")
    assert "sdk/docs/install" in caught.value.fix
