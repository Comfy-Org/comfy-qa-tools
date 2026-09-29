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
        if key.startswith("compute accelerator-types list"):
            # `setup` checks that the region it will file into actually sells the
            # cards it is about to ask for — including the region it DERIVES when
            # no `--region` is given, which is where three irrevocable requests
            # were going unchecked. These tests are not about availability, so
            # the answer is permissive: every card, in the usual regions.
            from comfy_qa.create import CARDS

            name = next((a.split("=")[-1] for a in args
                         if a.startswith("--filter=name=")), "")
            ids = [name] if name else [c.accelerator for c in CARDS.values()]
            return [{"name": i, "zone": f"https://x/zones/{z}"}
                    for i in ids
                    for z in ("us-central1-a", "europe-west4-a", "asia-east1-a")]
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
    # setup asks where gcloud's own python is, so it can put NumPy there — every
    # IAP tunnel is faster with it. A path that does not exist makes the step a
    # no-op, which is what a test wants.
    "info --format=value(basic.python_location)": "/no/such/python",
    "auth list": [{"account": "ali@comfy.org", "status": "ACTIVE"}],
    "projects list": [{"projectId": "proj-1"}],
    "config get-value project": "proj-1",
    "billing projects describe": {"billingEnabled": True},
    "quotas info list": [
        {"quotaId": "NVIDIA-L4-GPUS-per-project-region",
         "dimensionsInfos": [{"details": {"value": "1"},
                              "applicableLocations": ["us-central1"]}]},
    ],
    # setup reads the standing quota requests before it files any, so that a
    # card already waiting on Google is not asked for twice. Empty here: this
    # project has never asked for anything.
    "quotas preferences list": [],
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
        "quotas preferences update": {},
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
    assert any("comfy-qat quota" in line for line in p.said)


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
    responses = dict(READY, **{"quotas preferences update": {},
        "quotas info list": [
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
    responses = dict(READY, **{"quotas preferences update": {},
        "quotas info list": [
        {"quotaId": "GPUS-ALL-REGIONS-per-project",
         "dimensionsInfos": [{"details": {"value": "1"}, "applicableLocations": ["global"]}]},
    ]})
    p = prompts()
    run_setup(gcloud(**responses), p, config_path=tmp_path / "hosts.toml")
    assert any("project-wide allowance" in line for line in p.said)


# --- gcloud's python is a venv, and .resolve() walks out of it ----------------

def test_the_writability_check_looks_at_the_venv_not_its_base(tmp_path):
    """`Path(python).resolve()` follows bin/python OUT of a virtualenv to the
    base interpreter it was built from, so the check landed on Homebrew's Cellar
    rather than on gcloud's venv. Measured on the real install here.

    Both directions bite. A venv the user owns, built on a root-owned
    /usr/bin/python3, was judged UNWRITABLE and skipped — and the skip message
    advises `sudo <venv>/bin/python -m pip install`, which leaves root-owned
    files inside a user's virtualenv. And an unwritable venv built on a writable
    base was waved through, which is what this builds.
    """
    import subprocess
    import sys

    from comfy_qa.setup import gcloud_numpy

    venv = tmp_path / "venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True,
                   capture_output=True)
    python = venv / "bin" / "python"
    venv.chmod(0o555)
    try:
        class Fake:
            def python_location(self):
                return str(python)

        found = gcloud_numpy(Fake())
    finally:
        venv.chmod(0o755)

    assert found is not None, "an unwritable venv was waved through"
    assert found.blocked, "it should refuse rather than try"
    assert str(venv) in found.blocked, (
        f"it named the wrong directory: {found.blocked}"
    )
    # The sentence and the test are the SAME directory, not two derivations of
    # it. `setup` announces `found.prefix` when it installs, and this is what
    # stops that sentence naming a place the writability check never looked at.
    assert found.prefix == str(venv), (
        f"the announced directory is not the one that was tested: {found.prefix}"
    )


def test_setup_names_the_directory_it_tested_not_one_it_derived(monkeypatch):
    """The sentence a user reads must name the place the writability check
    looked at.

    The announcement used to compute its own `Path(python).parent.parent` while
    the gate asked the interpreter for `sys.prefix`. Two derivations of "where
    does pip put this" that agree on the common layout and disagree elsewhere —
    so the tool could announce a directory it had never tested, for the one step
    in `setup` that modifies software the user did not install.

    The python below is deliberately shaped so the two answers differ: the old
    derivation gives /opt/base, the tested prefix is /venv. Both are fabricated;
    nothing here runs an interpreter.
    """
    import subprocess as real_subprocess

    from comfy_qa import setup as setup_module

    monkeypatch.setattr(
        setup_module, "gcloud_numpy",
        lambda gc: setup_module.GcloudNumpy("/opt/base/bin/python3", "/venv", ""),
    )
    monkeypatch.setattr(
        real_subprocess, "run",
        lambda *a, **k: real_subprocess.CompletedProcess(a[0] if a else [], 0,
                                                         "", ""),
    )

    p = prompts()
    setup_module.ensure_tunnel_speed(gcloud(**READY), p)

    installing = next(line for line in p.said if "installing" in line)
    assert "/venv" in installing, (
        f"it announced a directory it never tested: {installing}"
    )
    assert "/opt/base)" not in installing, (
        "that is the base interpreter, not where pip would put NumPy"
    )


# --- the four ways the NumPy step could break unnoticed ----------------------
#
# Mutation testing found `--no-numpy` broken two independent ways, the guard
# that keeps pip out of the wrong interpreter droppable, and the argv it builds
# asserted nowhere. None of the four had a test.


def test_no_numpy_actually_skips_the_install(monkeypatch):
    """`if skip: return` neutered passed the whole suite. A documented flag that
    is ignored is worse than one that does not exist — the user believes they
    declined."""
    from comfy_qa import setup as setup_module

    called = []
    monkeypatch.setattr(setup_module, "gcloud_numpy",
                        lambda gc: called.append("looked") or None)

    p = prompts()
    setup_module.ensure_tunnel_speed(gcloud(**READY), p, skip=True)

    assert called == [], "--no-numpy still went looking for an interpreter"
    assert not any("installing" in line for line in p.said)


def test_the_no_numpy_flag_reaches_the_step_that_honours_it(monkeypatch):
    """The second, independent break: `no_numpy=no_numpy` dropped from the call
    leaves the parameter at its False default, so the flag is honoured by a
    function nobody passes it to."""
    from comfy_qa import setup as setup_module

    seen = {}
    monkeypatch.setattr(setup_module, "ensure_tunnel_speed",
                        lambda gc, p, *, skip=False: seen.setdefault("skip", skip))
    for name in ("ensure_gcloud", "ensure_signed_in", "ensure_project",
                 "ensure_billing", "ensure_host_list", "ensure_quota"):
        if hasattr(setup_module, name):
            monkeypatch.setattr(setup_module, name, lambda *a, **k: None)

    try:
        setup_module.run_setup(gcloud(**READY), prompts(), no_numpy=True)
    except SetupStopped:
        pass

    assert seen.get("skip") is True, (
        "--no-numpy was accepted by the CLI and never reached the step")


def test_an_interpreter_path_that_does_not_exist_is_not_used(monkeypatch):
    """`gcloud info` can report a bare name rather than a path. Dropping the
    os.path.exists half of the guard makes `gcloud_numpy` return a real target
    built from the CALLER's Python — and pip then installs into the user's own
    interpreter, not gcloud's. Demonstrated: the mutant returned
    GcloudNumpy(python='python3', prefix='/opt/homebrew/...python@3.14/...')."""
    from comfy_qa.setup import gcloud_numpy

    class BareName:
        def python_location(self):
            return "python3"

    assert gcloud_numpy(BareName()) is None, (
        "a bare interpreter name was treated as a path that exists")


def test_the_install_is_wheels_only(monkeypatch):
    """`--only-binary=:all:` is not tidiness — the docstring says so. Without it
    an interpreter with no wheel falls back to building from source. The argv
    was asserted nowhere."""
    import subprocess as real_subprocess

    from comfy_qa import setup as setup_module

    argv = []
    monkeypatch.setattr(
        setup_module, "gcloud_numpy",
        lambda gc: setup_module.GcloudNumpy("/opt/base/bin/python3", "/venv", ""))
    monkeypatch.setattr(
        real_subprocess, "run",
        lambda *a, **k: (argv.append(list(a[0])) if a else None) or
        real_subprocess.CompletedProcess(a[0] if a else [], 0, "", ""))

    setup_module.ensure_tunnel_speed(gcloud(**READY), prompts())

    install = next((c for c in argv if "install" in c), None)
    assert install is not None, "no pip install was run"
    assert "--only-binary=:all:" in install, (
        f"a source build can come back unnoticed: {install}")
