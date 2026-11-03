"""Choosing the build: which ComfyUI, which frontend, Manager, PyTorch (1.4.0).

Step 2 of the comfy-qat roadmap. Each test here names the criterion it holds
(criteria-step2.md, S2-1 to S2-10) and was run red before the code it covers
existed. Nothing here reaches the network: GitHub is answered by `Hub`, below,
and `conftest.py` refuses any test that would ask the real one.
"""

from __future__ import annotations

import pytest

from comfy_qa import build as build_mod
from comfy_qa.build import (
    COMFY_CLI_VERSION, DEFAULT, RECOMMENDED_NVIDIA_TORCH, Build, BuildError, choose,
    parse,
)
from comfy_qa.config import Host
from comfy_qa.provision import (
    CHANGING, CLEANING, INSTALLING, change_command, facts_command, install_command,
    launch_command, launch_detached_command,
)

LINUX = Host(name="comfy-linux", kind="gce", port=8191, os="Ubuntu 22.04", gpu="L4",
             gce_instance="comfy-linux", gce_zone="us-central1-a", gce_project="p")
LINUX_CPU = Host(name="qatest-cpu", kind="gce", port=8192, os="Ubuntu 22.04",
                 gpu="none", gce_instance="qatest-cpu", gce_zone="us-central1-a",
                 gce_project="p")
WIN = Host(name="comfy-win", kind="gce", port=8193, os="Windows Server 2022", gpu="L4",
           gce_instance="comfy-win", gce_zone="us-central1-a", gce_project="p")
WIN_CPU = Host(name="qatest-win", kind="gce", port=8194, os="Windows Server 2022",
               gpu="none", gce_instance="qatest-win", gce_zone="us-central1-a",
               gce_project="p")
EVERY_BOX = [LINUX, LINUX_CPU, WIN, WIN_CPU]
IDS = ["linux", "linux-cpu", "windows", "windows-cpu"]


def hands_comfy(command: str, *args: str) -> bool:
    """Does this script pass `args`, in order, to `comfy install`?

    sh writes them as words (`#` quoted, since it starts a comment); the
    PowerShell script builds an array, `'--version', '0.43.0'`.
    """
    words = " ".join(f"'{a}'" if a.startswith("#") else a for a in args)
    listed = ", ".join(f"'{a}'" for a in args)
    return words in command or listed in command


# --- S2-1: the backend --------------------------------------------------------


@pytest.mark.parametrize("asked, expected", [
    ({"version": "v0.43.0"}, Build(version="v0.43.0")),
    ({"version": "0.43.0"}, Build(version="v0.43.0")),
    ({"pr": "17497"}, Build(pr=17497)),
    ({"pr": "#17497"}, Build(pr=17497)),
    ({"commit": "426F5EEA52A7"}, Build(commit="426f5eea52a7")),
])
def test_a_backend_is_a_release_a_pr_or_a_commit(asked, expected):
    assert choose(**asked) == expected


@pytest.mark.parametrize("asked", [
    {"version": "v0.43.0", "pr": "17497"},
    {"version": "v0.43.0", "commit": "426f5ee"},
    {"pr": "17497", "commit": "426f5ee"},
    {"version": "v0.43.0", "pr": "17497", "commit": "426f5ee"},
])
def test_two_backends_at_once_are_refused_with_the_ones_that_are_intentions(asked):
    with pytest.raises(BuildError) as caught:
        choose(**asked)
    assert "only one of them can be right" in str(caught.value)
    for flag, value in asked.items():
        assert f"--{flag} {value}" in caught.value.fix


def test_no_backend_keeps_the_tip_of_the_default_branch_and_says_so():
    chosen = choose()
    assert chosen == DEFAULT
    assert chosen.backend_args() == [], "comfy-cli's default is the tip of master"
    assert "tip of its default branch" in chosen.describe()


@pytest.mark.parametrize("asked, words", [
    ({"version": "nonsense"}, "is not a release tag"),
    ({"pr": "abc"}, "is not a pull request number"),
    ({"pr": "0"}, "is not a pull request number"),
    ({"commit": "xyz"}, "is not a commit"),
    ({"commit": "abc12"}, "is not a commit"),
])
def test_a_backend_that_is_not_even_shaped_like_one_is_refused(asked, words):
    with pytest.raises(BuildError) as caught:
        choose(**asked)
    assert words in str(caught.value)
    assert caught.value.fix


@pytest.mark.parametrize("chosen, args", [
    (Build(version="v0.43.0"), ["--version", "0.43.0"]),
    (Build(pr=17497), ["--pr", "#17497"]),
    (Build(commit="426f5ee"), ["--commit", "426f5ee"]),
])
def test_the_backend_is_handed_to_comfy_install_in_its_own_options(chosen, args):
    assert chosen.backend_args() == args


# --- S2-2: the frontend, independently ----------------------------------------


def test_a_frontend_release_is_chosen_without_touching_the_backend():
    chosen = choose(frontend_version="1.58.4")
    assert chosen.frontend == "v1.58.4"
    assert not chosen.names_a_backend
    assert chosen.backend_args() == []
    assert ("--front-end-version Comfy-Org/ComfyUI_frontend@v1.58.4"
            in chosen.launch_args())


def test_a_frontend_pr_is_served_from_the_directory_its_build_landed_in():
    chosen = choose(frontend_pr="5678", version="v0.43.0")
    assert chosen.frontend_pr == 5678 and chosen.version == "v0.43.0"
    assert "--front-end-root /opt/x/dist" in chosen.launch_args("/opt/x/dist")
    assert "--front-end-version" not in chosen.launch_args("/opt/x/dist")


def test_no_frontend_keeps_the_one_the_backend_pins():
    assert "--front-end" not in choose(version="v0.43.0").launch_args()
    assert "frontend that ComfyUI pins" in choose().describe()


def test_two_frontends_at_once_are_refused():
    with pytest.raises(BuildError) as caught:
        choose(frontend_version="v1.58.4", frontend_pr="5678")
    assert "only one of them can be right" in str(caught.value)


# --- S2-3: Manager on by default -------------------------------------------------


@pytest.mark.parametrize("host", EVERY_BOX, ids=IDS)
def test_the_default_install_includes_manager(host):
    """Ali, 3 Nov: "manager should always be on btw, by default"."""
    command = install_command(host)
    assert "--skip-manager" not in command
    assert DEFAULT.manager is True
    assert "--enable-manager" in launch_detached_command(host, DEFAULT.launch_args())


@pytest.mark.parametrize("host", EVERY_BOX, ids=IDS)
def test_manager_is_off_only_when_asked(host):
    chosen = choose(no_manager=True)
    assert "--skip-manager" in install_command(host, build=chosen)
    assert "--enable-manager" not in chosen.launch_args()
    assert "Manager off" in chosen.describe()


# --- S2-4: PyTorch ----------------------------------------------------------------


def test_torch_can_be_pinned_and_the_pin_reaches_every_pip_install_comfy_cli_runs():
    chosen = choose(torch=RECOMMENDED_NVIDIA_TORCH)
    assert chosen.torch == "2.12.1+cu130"
    assert chosen.constraint() == "torch==2.12.1"
    assert chosen.cuda_version == "13.0"
    for host in (LINUX, WIN):
        command = install_command(host, build=chosen)
        assert "torch==2.12.1" in command
        assert "PIP_CONSTRAINT" in command
        assert hands_comfy(command, "--cuda-version", "13.0")


def test_the_recommended_pin_is_miless_as_written():
    assert RECOMMENDED_NVIDIA_TORCH == "2.12.1+cu130"


def test_with_no_pin_nothing_is_constrained():
    for host in EVERY_BOX:
        assert "torch==" not in install_command(host)


def test_a_cuda_pin_on_a_box_with_no_gpu_is_refused():
    with pytest.raises(BuildError) as caught:
        choose(torch="2.12.1+cu130", no_gpu=True)
    assert "no GPU" in str(caught.value)
    assert choose(torch="2.12.1", no_gpu=True).torch == "2.12.1"


@pytest.mark.parametrize("value", ["latest", "2.12.1+cu999", "two"])
def test_a_torch_pin_that_cannot_be_installed_is_refused(value):
    with pytest.raises(BuildError):
        choose(torch=value)


# --- S2-5: comfy-cli does the installing ----------------------------------------


@pytest.mark.parametrize("host", EVERY_BOX, ids=IDS)
def test_the_install_is_comfy_cli_not_a_hand_rolled_clone(host):
    command = install_command(host, build=Build(version="v0.43.0"))
    assert "git clone" not in command
    assert f"comfy-cli=={COMFY_CLI_VERSION}" in command, "pinned on the box"
    assert "--skip-prompt" in command, "comfy-cli asks questions otherwise"
    assert hands_comfy(command, "--version", "0.43.0")
    assert INSTALLING in command


@pytest.mark.parametrize("host", EVERY_BOX, ids=IDS)
def test_the_exact_comfy_command_is_printed_before_it_runs(host):
    """S2-5: the exact commands are visible in the streamed log."""
    command = install_command(host, build=Build(pr=17497))
    if host.os.startswith("Windows"):
        assert "Write-Output ('+ comfy ' + ($a -join ' '))" in command
        assert "$a = @('--skip-prompt', '--workspace'" in command
    else:
        assert "+ comfy --skip-prompt --workspace" in command
    assert hands_comfy(command, "--pr", "#17497")


@pytest.mark.parametrize("host", EVERY_BOX, ids=IDS)
def test_comfy_cli_never_pays_its_five_second_exit_tax(host):
    """The atexit telemetry flush costs ~5 s a command unless DO_NOT_TRACK is
    set (reference-comfy-cli-defects.md, defect 1)."""
    for command in (install_command(host), change_command(host, Build(version="v0.42.1"))):
        assert "DO_NOT_TRACK" in command


@pytest.mark.parametrize("host", EVERY_BOX, ids=IDS)
def test_comfy_launch_background_is_never_relied_on(host):
    """It refuses a second background server even on a free port (defect 2), so
    ComfyUI is started by this tool's own launch, never by `comfy launch`."""
    for command in (install_command(host), change_command(host, Build(version="v0.42.1")),
                    launch_detached_command(host, DEFAULT.launch_args()),
                    launch_command(host, DEFAULT.launch_args())):
        assert "launch --background" not in command
        assert "comfy launch" not in command


@pytest.mark.parametrize("host", EVERY_BOX, ids=IDS)
def test_the_gpu_decides_comfy_cli_s_gpu_option(host):
    command = install_command(host)
    if host.gpu == "none":
        assert hands_comfy(command, "--cpu") and "--nvidia" not in command
    else:
        assert hands_comfy(command, "--nvidia") and "'--cpu'" not in command
        assert " --cpu" not in command


@pytest.mark.parametrize("host", EVERY_BOX, ids=IDS)
def test_the_build_installed_is_recorded_on_the_box(host):
    chosen = Build(version="v0.43.0", frontend="v1.58.4")
    assert chosen.spec() in install_command(host, build=chosen)
    assert chosen.spec() in change_command(host, chosen)


def test_the_box_s_facts_are_read_back():
    facts = build_mod.facts(
        "COMMIT=426f5eea52a7ece1dd599fff4cb6cd1f352ddd24\n"
        "BRANCH=pr-17497-solid-mode\n"
        "RECORD=comfyui=pr:17497 frontend=default manager=on torch=default\n"
        "FRONTEND_ROOT=\n")
    assert facts.commit == "426f5eea52a7ece1dd599fff4cb6cd1f352ddd24"
    assert facts.pr == 17497
    assert facts.record == Build(pr=17497)
    assert facts.frontend_root is None


def test_facts_from_a_box_that_has_none_are_none_not_invented():
    facts = build_mod.facts("COMMIT=\nBRANCH=\nRECORD=\nFRONTEND_ROOT=\n")
    assert facts.commit is None and facts.pr is None and facts.record is None


@pytest.mark.parametrize("host", EVERY_BOX, ids=IDS)
def test_the_facts_command_names_no_launch(host):
    """A reader of the box's commands (and the fakes) tells a launch by
    `main.py`; asking what the box runs must not look like starting it."""
    command = facts_command(host)
    assert "main.py" not in command
    assert "COMMIT=" in command and "RECORD=" in command


# --- S2-8: changing the build keeps custom nodes and models ---------------------


@pytest.mark.parametrize("host", EVERY_BOX, ids=IDS)
def test_a_build_change_keeps_the_checkout_and_everything_in_it(host):
    command = change_command(host, Build(version="v0.42.1"))
    assert CHANGING in command
    assert "--restore" in command, "comfy install on an existing checkout"
    assert CLEANING not in command
    assert "rm -rf" not in command and "Remove-Item -Recurse" not in command
    assert hands_comfy(command, "--version", "0.42.1")


@pytest.mark.parametrize("host", EVERY_BOX, ids=IDS)
def test_a_clean_install_is_the_one_that_removes_it(host):
    command = install_command(host, build=Build(version="v0.42.1"), clean=True)
    assert CLEANING in command
    assert 'rm -rf "$target"' in command or "Remove-Item -Recurse -Force 'C:\\ComfyUI' }; " in command
    plain = install_command(host, build=Build(version="v0.42.1"))
    assert CLEANING not in plain and 'rm -rf "$target"' not in plain


# --- the one-line form --------------------------------------------------------


@pytest.mark.parametrize("chosen", [
    DEFAULT, Build(version="v0.43.0", frontend="v1.58.4"), Build(pr=17497, manager=False),
    Build(commit="426f5ee", frontend_pr=5678, torch="2.12.1+cu130"),
])
def test_a_build_survives_being_written_down_and_read_back(chosen):
    assert parse(chosen.spec()) == chosen


@pytest.mark.parametrize("spec", ["comfyui=v0.43", "colour=blue", "manager=maybe"])
def test_a_build_line_that_does_not_parse_is_refused_not_read_as_default(spec):
    with pytest.raises(BuildError):
        parse(spec)


# --- S2-9: bad input refused before anything exists ---------------------------


class Hub:
    """GitHub, answered from a table. Paths it was not told about are 404s."""

    def __init__(self, answers: dict | None = None) -> None:
        self.answers = answers or {}
        self.asked: list[str] = []

    def __call__(self, path: str):
        self.asked.append(path)
        if path in self.answers:
            return 200, self.answers[path]
        return 404, None


REPO = "repos/Comfy-Org/ComfyUI"
FRONT = "repos/Comfy-Org/ComfyUI_frontend"


def test_a_release_that_does_not_exist_is_refused_in_plain_words():
    with pytest.raises(BuildError) as caught:
        build_mod.check(Build(version="v0.99.0"), Hub())
    assert "ComfyUI has no release v0.99.0" in str(caught.value)
    assert "Nothing was created" in str(caught.value)
    assert "releases" in caught.value.fix


def test_a_release_that_exists_passes():
    hub = Hub({f"{REPO}/git/ref/tags/v0.43.0": {"object": {"sha": "426f5ee"}}})
    assert build_mod.check(Build(version="v0.43.0"), hub) == Build(version="v0.43.0")


def test_a_pr_that_does_not_exist_is_refused():
    with pytest.raises(BuildError) as caught:
        build_mod.check(Build(pr=99999999), Hub())
    assert "no pull request 99999999" in str(caught.value)


def test_a_closed_pr_is_refused():
    hub = Hub({f"{REPO}/pulls/12": {"state": "closed", "merged": False}})
    with pytest.raises(BuildError) as caught:
        build_mod.check(Build(pr=12), hub)
    assert "is closed" in str(caught.value)


def test_a_merged_pr_is_refused_and_pointed_at_its_merge_commit():
    hub = Hub({f"{REPO}/pulls/12": {"state": "closed", "merged": True,
                                    "merge_commit_sha": "abcdef1234567890"}})
    with pytest.raises(BuildError) as caught:
        build_mod.check(Build(pr=12), hub)
    assert "already merged" in str(caught.value)
    assert "--commit abcdef1234" in caught.value.fix


def test_an_open_pr_passes_and_carries_its_head_commit():
    hub = Hub({f"{REPO}/pulls/17497": {"state": "open", "merged": False,
                                       "head": {"sha": "9350A08D80aaaaaa"}}})
    assert build_mod.check(Build(pr=17497), hub).pr_head == "9350a08d80aaaaaa"


def test_an_unknown_commit_is_refused():
    with pytest.raises(BuildError) as caught:
        build_mod.check(Build(commit="deadbeef"), Hub())
    assert "ComfyUI has no commit deadbeef" in str(caught.value)


def test_a_short_commit_is_read_back_whole():
    full = "426f5eea52a7ece1dd599fff4cb6cd1f352ddd24"
    hub = Hub({f"{REPO}/commits/426f5ee": {"sha": full}})
    assert build_mod.check(Build(commit="426f5ee"), hub).commit == full


def test_a_frontend_release_that_does_not_exist_is_refused():
    with pytest.raises(BuildError) as caught:
        build_mod.check(Build(frontend="v9.9.9"), Hub())
    assert "the frontend has no release v9.9.9" in str(caught.value)


def test_a_closed_frontend_pr_is_refused():
    hub = Hub({f"{FRONT}/pulls/5": {"state": "closed", "merged": False}})
    with pytest.raises(BuildError):
        build_mod.check(Build(frontend_pr=5), hub)


def test_the_default_build_asks_github_nothing():
    hub = Hub()
    build_mod.check(DEFAULT, hub)
    assert hub.asked == []


def test_github_that_cannot_be_reached_is_not_github_saying_no():
    def down(path):
        raise build_mod.Unreachable("Name or service not known")

    with pytest.raises(BuildError) as caught:
        build_mod.check(Build(version="v0.43.0"), down)
    assert "could not be asked" in str(caught.value)
    assert "no release" not in str(caught.value)
