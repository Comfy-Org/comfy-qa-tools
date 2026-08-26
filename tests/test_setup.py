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
        {"quotaId": "NVIDIA-L4-GPUS-per-project-region",
         "dimensionsInfos": [{"details": {"value": "1"},
                              "applicableLocations": ["us-central1"]}]},
    ],
    "compute instances list": [],
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
        "quotas info list": [{"quotaId": "NVIDIA-L4-GPUS-per-project-region",
                              "dimensionsInfos": [{"details": {"value": "0"},
                                                   "applicableLocations": ["us-central1"]}]}],
        "quotas preferences create": {},
    })
    p = prompts(confirm=True)
    path = run_setup(gcloud(**responses), p, config_path=tmp_path / "hosts.toml")
    assert path.exists()
    assert any("requested" in line for line in p.said)


def test_declining_the_quota_request_still_finishes_setup(tmp_path):
    responses = dict(READY, **{
        "quotas info list": [{"quotaId": "NVIDIA-T4-GPUS-per-project-region",
                              "dimensionsInfos": [{"details": {"value": "0"},
                                                   "applicableLocations": ["us-central1"]}]}],
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


def test_a_quota_read_that_fails_does_not_take_setup_down(tmp_path):
    """The first live run crashed here with a traceback.

    Quota is explicitly allowed to fail — it can take days to change and is never
    a reason to strand someone mid-setup. It must be reported and stepped over.
    """
    responses = dict(READY, **{
        "quotas info list": GcloudError("gcloud timed out after 240s: quotas info list"),
    })
    p = prompts()
    path = run_setup(gcloud(**responses), p, config_path=tmp_path / "hosts.toml")

    assert path.exists(), "setup must still finish"
    assert any("could not read GPU quota" in line for line in p.said)
    assert any("comfy-qat auth quota" in line for line in p.said)


COMFY_WIN = {
    "name": "comfy-win",
    "zone": "projects/p/zones/us-central1-a",
    "status": "TERMINATED",
    "guestAccelerators": [{"acceleratorType": "zones/z/acceleratorTypes/nvidia-l4"}],
    "disks": [{"boot": True, "licenses": ["global/licenses/windows-server-2022-dc"]}],
}


def test_setup_adds_cloud_boxes_without_anyone_typing_a_zone(tmp_path):
    config = tmp_path / "hosts.toml"
    responses = dict(READY, **{"compute instances list": [COMFY_WIN]})
    p = prompts()

    run_setup(gcloud(**responses), p, config_path=config)

    text = config.read_text()
    assert "[hosts.comfy-win]" in text
    assert 'gce_zone     = "us-central1-a"' in text
    assert "port         = 8190" in text, "8188 belongs to the local ComfyUI"
    assert any("added comfy-win" in line and "stopped" in line for line in p.said)


def test_running_setup_twice_does_not_add_the_box_twice(tmp_path):
    config = tmp_path / "hosts.toml"
    responses = dict(READY, **{"compute instances list": [COMFY_WIN]})

    run_setup(gcloud(**responses), prompts(), config_path=config)
    first = config.read_text()
    p = prompts()
    run_setup(gcloud(**responses), p, config_path=config)

    assert config.read_text() == first
    assert any("already in your host list" in line for line in p.said)


def test_setup_reports_cards_not_raw_quota_ids(tmp_path):
    """It announced COMMITTED-NVIDIA-L4 as available — an allowance that cannot
    start an ordinary box. Setup must read through the same filter as everything else."""
    responses = dict(READY, **{"quotas info list": [
        {"quotaId": "COMMITTED-NVIDIA-L4-GPUS-per-project-region",
         "dimensionsInfos": [{"details": {"value": "8"},
                              "applicableLocations": ["us-central1"]}]},
        {"quotaId": "NVIDIA-K80-GPUS-per-project-region",
         "dimensionsInfos": [{"details": {"value": "1"},
                              "applicableLocations": ["us-central1"]}]},
    ]})
    p = prompts()
    run_setup(gcloud(**responses), p, config_path=tmp_path / "hosts.toml")

    quota_lines = [line for line in p.said if "GPU quota" in line]
    assert quota_lines, "quota state must still be reported"
    assert not any("COMMITTED" in line for line in quota_lines)
    assert any("K80" in line for line in quota_lines)


def test_a_failure_listing_boxes_does_not_stop_setup(tmp_path):
    responses = dict(READY, **{
        "compute instances list": GcloudError("gcloud timed out after 60s"),
    })
    p = prompts()
    path = run_setup(gcloud(**responses), p, config_path=tmp_path / "hosts.toml")
    assert path.exists()
    assert any("could not list cloud boxes" in line for line in p.said)


def test_the_project_wide_allowance_is_not_listed_as_a_card(tmp_path):
    """`any (global)` is a ceiling across every card, not a GPU you can pick.

    Listing it beside L4 and T4 reads as a model nobody has heard of.
    """
    responses = dict(READY, **{"quotas info list": [
        {"quotaId": "GPUS-ALL-REGIONS-per-project",
         "dimensionsInfos": [{"details": {"value": "1"}, "applicableLocations": ["global"]}]},
        {"quotaId": "NVIDIA-L4-GPUS-per-project-region",
         "dimensionsInfos": [{"details": {"value": "1"}, "applicableLocations": ["us-central1"]}]},
    ]})
    p = prompts()
    run_setup(gcloud(**responses), p, config_path=tmp_path / "hosts.toml")

    line = next(said for said in p.said if said.startswith("GPU quota ready"))
    assert "L4" in line
    assert "global" not in line


def test_a_global_only_allowance_is_still_reported(tmp_path):
    responses = dict(READY, **{"quotas info list": [
        {"quotaId": "GPUS-ALL-REGIONS-per-project",
         "dimensionsInfos": [{"details": {"value": "1"}, "applicableLocations": ["global"]}]},
    ]})
    p = prompts()
    run_setup(gcloud(**responses), p, config_path=tmp_path / "hosts.toml")
    assert any("project-wide allowance" in line for line in p.said)
