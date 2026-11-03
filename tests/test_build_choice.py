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
    _LINUX_CLI, _WINDOWS_CLI, CHANGING, CLEANING, INSTALLING, LINUX_FRONTEND_ROOT,
    WINDOWS_FRONTEND_ROOT, change_command, facts_command, install_command,
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


@pytest.mark.parametrize("host", EVERY_BOX, ids=IDS)
def test_a_frontend_pr_is_built_on_the_box_by_comfy_clis_own_builder(host):
    """S2-2, on both operating systems: the install builds the PR with comfy-cli's
    builder, prints where it landed after a FRONTEND_ROOT= marker, and keeps
    that directory on the box for the launch. A build with no frontend PR
    removes what an earlier one left, so its directory is never served again."""
    command = install_command(host, build=Build(frontend_pr=5678))
    assert "handle_temporary_frontend_pr" in command, "the PR is never built"
    assert "'#5678'" in command and "'FRONTEND_ROOT='" in command
    root = WINDOWS_FRONTEND_ROOT if host.os.startswith("Windows") else LINUX_FRONTEND_ROOT
    written = (f"Set-Content -Path '{root}' -Value $root" if host.os.startswith("Windows")
               else f"printf '%s\\n' \"$root\" > {root}")
    assert written in command
    plain = install_command(host, build=Build(version="v0.43.0"))
    assert "handle_temporary_frontend_pr" not in plain
    assert root in plain and written not in plain, "the old directory is removed"


def test_where_a_frontend_pr_landed_is_read_back_off_the_box():
    facts = build_mod.facts("COMMIT=\nBRANCH=\nRECORD=\nFRONTEND_ROOT=/opt/x/dist\n")
    assert facts.frontend_root == "/opt/x/dist"


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
        assert f"Write-Output ('+ {_WINDOWS_CLI} ' + " in command
        assert "$a = @('--skip-prompt', '--workspace'" in command
    else:
        assert f"+ {_LINUX_CLI} --skip-prompt --workspace" in command
    assert hands_comfy(command, "--pr", "#17497")


# The builds whose `comfy install` line is echoed below: a PR (whose `#` is a
# comment to both shells unless quoted), a release with Manager off, a commit.
ECHOED = [Build(pr=17497), Build(version="v0.43.0", manager=False),
          Build(commit="426f5eea52", torch="2.12.1+cu130")]


def _sh(script: str) -> str:
    import subprocess

    return subprocess.run(["/bin/sh", "-c", script], capture_output=True, text=True,
                          check=True, timeout=20).stdout


@pytest.mark.parametrize("change", [False, True], ids=["install", "change"])
@pytest.mark.parametrize("chosen", ECHOED, ids=["pr", "release", "commit"])
@pytest.mark.parametrize("host", [LINUX, LINUX_CPU], ids=["linux", "linux-cpu"])
def test_the_echoed_comfy_command_is_the_one_that_runs_on_linux(host, chosen, change):
    """Audit findings code:F4 / behaviour:F10. The `+ comfy ...` line is there
    so the exact command is in the streamed log (machines.md). It said `comfy`
    where /opt/comfy-qat/venv/bin/comfy runs, and wrote `--pr #17497` bare, so
    pasted into sh the PR number was a comment. Both lines are handed to a real
    sh here: the line echoed, pasted back, must be the argv that ran."""
    import re

    script = (change_command(host, chosen) if change
              else install_command(host, build=chosen))
    echo = re.search(r'(echo "\+ [^"]*"); ', script).group(1)
    run = re.search(rf'(?<!\+ )({re.escape(_LINUX_CLI)} --skip-prompt [^;]*); ',
                    script).group(1)
    where = "WS=/opt/comfyui; RESTORE=--restore; " if change else "WS=/x/ComfyUI; RESTORE=; "

    printed = _sh(where + echo).strip()
    assert printed.startswith("+ ")
    pasted = _sh(f"set -- {printed[2:]}; printf '%s\\n' \"$@\"").splitlines()
    ran = _sh(f"{where}set -- {run}; printf '%s\\n' \"$@\"").splitlines()
    assert pasted == ran, f"the log says:\n  {printed}\nbut what ran was:\n  {ran}"
    assert ran[0] == _LINUX_CLI


def _powershell_words(line: str) -> list[str]:
    """`line` as PowerShell splits a command's arguments, for the shapes this
    tool prints: bare words and single-quoted ones. A `#` that starts a bare
    word starts a comment. No PowerShell on the machines the suite runs on, so
    this models the two rules that matter here, and nothing more."""
    import re

    words = []
    for quoted, bare in re.findall(r"'((?:[^']|'')*)'|(\S+)", line):
        if bare.startswith("#"):
            break
        words.append(quoted.replace("''", "'") if not bare else bare)
    return words


@pytest.mark.parametrize("change", [False, True], ids=["install", "change"])
@pytest.mark.parametrize("chosen", ECHOED, ids=["pr", "release", "commit"])
@pytest.mark.parametrize("host", [WIN, WIN_CPU], ids=["windows", "windows-cpu"])
def test_the_echoed_comfy_command_is_the_one_that_runs_on_windows(host, chosen, change):
    """The Windows half of the same finding. The echo is built at run time from
    the array `comfy.exe` is called with, so here that array is read off the
    script, the echo's own rule (quote a `#`-led item) is applied to it, and the
    line it makes is split as PowerShell would split it pasted."""
    import re

    script = (change_command(host, chosen) if change
              else install_command(host, build=chosen))
    assert (f"Write-Output ('+ {_WINDOWS_CLI} ' + (($a | ForEach-Object "
            "{ if ($_.StartsWith('#')) { '' + [char]39 + $_ + [char]39 } "
            "else { $_ } }) -join ' ')); ") in script
    assert f"& '{_WINDOWS_CLI}' @a; " in script, "the echo names what runs"

    items: list[str] = []
    for added in re.findall(r"\$a (?:=|\+=) (@\([^)]*\)|'[^']*')", script):
        items += re.findall(r"'([^']*)'", added)
    printed = f"{_WINDOWS_CLI} " + " ".join(
        f"'{item}'" if item.startswith("#") else item for item in items)
    assert _powershell_words(printed) == [_WINDOWS_CLI, *items]
    if chosen.pr:
        assert "'#17497'" in printed


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


# --- S2-7: ready means the build asked for ---------------------------------------

# What a running ComfyUI and its checkout would say, every part read: the
# release v0.43.0 at its tag's commit, frontend 1.58.4, Manager on, torch
# 2.12.1+cu130. Each case below changes one part.
SEEN = dict(comfyui_version="0.43.0", commit="426f5eea52a7ece1dd599fff4cb6cd1f352ddd24",
            pr=None, frontend="1.58.4", manager="on", torch="2.12.1+cu130")
PR_HEAD = "9350a08d80aaaaaabbbbbbbbccccccccdddddddd"


@pytest.mark.parametrize("asked, seen, said", [
    (Build(torch="2.12.1+cu130"), {"torch": "2.13.0+cu130"},
     "torch 2.13.0+cu130, not 2.12.1+cu130"),
    (Build(torch="2.12.1+cu130"), {"torch": "2.12.1+cu128"},
     "torch 2.12.1+cu128, not 2.12.1+cu130"),
    (Build(commit="b0b0b0b0b0b0"), {},
     "commit 426f5eea52, not b0b0b0b0b0"),
    (Build(pr=17497, pr_head=PR_HEAD), {"pr": 999, "commit": "c" * 40},
     "PR 999, not PR 17497"),
    (Build(pr=17497, pr_head=PR_HEAD), {},
     "commit 426f5eea52, not PR 17497"),
    # code:F3: the PR's own branch, at a commit that is not the head GitHub
    # reported — the PR has had commits since the box checked it out.
    (Build(pr=17497, pr_head=PR_HEAD), {"pr": 17497, "commit": "0" * 40},
     "PR 17497 at 0000000000, not its head 9350a08d80"),
], ids=["torch-version", "torch-cuda", "commit", "another-pr", "no-pr", "old-head"])
def test_each_part_that_contradicts_the_build_is_named(asked, seen, said):
    wrong, unread = asked.contradictions(**{**SEEN, **seen})
    assert wrong == [said]
    assert unread == []


@pytest.mark.parametrize("asked", [
    Build(torch="2.12.1+cu130"), Build(commit="426f5eea52"),
    Build(pr=17497, pr_head=PR_HEAD),
], ids=["torch", "commit", "pr"])
def test_a_part_that_agrees_is_not_a_contradiction(asked):
    seen = {**SEEN, "pr": 17497, "commit": PR_HEAD} if asked.pr else SEEN
    assert asked.contradictions(**seen) == ([], [])


def test_a_part_that_cannot_be_read_is_unconfirmed_never_a_contradiction():
    """The rule `stamp` keeps, and machines.md states: a part nobody can read
    is said to be unconfirmed and does not block. Manager unread is not off."""
    asked = Build(version="v0.43.0", commit="426f5ee", frontend="v1.58.4",
                  torch="2.12.1+cu130")
    wrong, unread = asked.contradictions(comfyui_version=None, commit=None, pr=None,
                                         frontend=None, manager=None, torch=None)
    assert wrong == []
    assert unread == ["the ComfyUI version", "the checkout's commit",
                      "the frontend it serves", "whether Manager is running",
                      "the torch version"]


@pytest.mark.parametrize("chosen, words", [
    (DEFAULT, "--manager"),
    (Build(manager=False), "--no-manager"),
    (Build(version="v0.43.0", torch="2.12.1+cu130"),
     "--version v0.43.0 --manager --torch 2.12.1+cu130"),
])
def test_the_options_that_ask_for_a_build_again_name_manager(chosen, words):
    """Audit finding F2: the fix for a wrong build is `comfy-qat go <box>` with
    these options, and with none of them a box that is serving is asked
    nothing. Manager is on by default, so it has to be said to be held to."""
    assert chosen.flags(explicit_manager=True) == words
    assert "--manager" not in chosen.flags().split(), "only when asked to"


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


def _refused(asked: dict | None = None, *, merged: bool = False) -> BuildError:
    with pytest.raises(BuildError) as caught:
        if merged:
            build_mod.check(Build(pr=12), Hub({f"{REPO}/pulls/12": {
                "state": "closed", "merged": True,
                "merge_commit_sha": "abcdef1234567890"}}))
        else:
            choose(**asked)
    return caught.value


@pytest.mark.parametrize("refusal", [
    lambda: _refused({"version": "v0.43.0", "pr": "17497"}),
    lambda: _refused({"frontend_version": "v1.58.4", "frontend_pr": "5678"}),
    lambda: _refused({"torch": "two"}),
    lambda: _refused({"torch": "2.12.1+cu999"}),
    lambda: _refused({"torch": "2.12.1+cu130", "no_gpu": True}),
    lambda: _refused(merged=True),
], ids=["two-backends", "two-frontends", "torch-not-a-version", "torch-unknown-cuda",
        "torch-cuda-no-gpu", "merged-pr"])
def test_a_refusal_never_prints_an_option_on_a_line_as_if_it_were_a_command(refusal):
    """Audit parking item P2, the bug class 1.3.1 fixed for printed commands:
    a line under `to fix:` that starts `--version v0.43.0` reads as something to
    paste, and pasted it is not a command. The option is named in the
    sentence that says to give it, on the same command."""
    fix = refusal().fix
    lines = [line.strip() for line in fix.splitlines()]
    assert not [line for line in lines if line.startswith("-")], fix
    assert " --" in fix, "the option is still named"


# --- Windows prerequisites: what is really on Windows Server 2022 -------------


@pytest.mark.parametrize("host", [WIN, WIN_CPU], ids=["windows", "windows-cpu"])
def test_the_windows_install_does_not_rely_on_winget(host):
    """Found live on 2026-11-03 (live-s2, evidence/41): `winget` is not on a
    windows-2022 box reached over SSH — `The term 'winget' is not recognized` —
    so the install got neither Git nor Python and stopped at "comfy-cli did not
    install". Nothing on record shows the winget route ever working on a real
    box. Git and Python come from their own publishers' installers instead,
    each run silently and judged by the file it should leave behind."""
    from comfy_qa.provision import GIT_INSTALLER, PYTHON_INSTALLER, WINDOWS_PYTHON

    for command in (install_command(host), change_command(host, Build(version="v0.42.1"))):
        assert "winget" not in command
        assert "py -" not in command, "the py launcher is not there either"
        assert PYTHON_INSTALLER in command and GIT_INSTALLER in command
        assert "InstallAllUsers=1" in command and "/VERYSILENT" in command
        assert f"'{WINDOWS_PYTHON}' -m venv" in command, "the interpreter by path"


def test_the_windows_installers_are_the_publishers_own():
    from comfy_qa.provision import GIT_INSTALLER, PYTHON_INSTALLER, WINDOWS_PYTHON

    assert PYTHON_INSTALLER == (
        "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe")
    assert GIT_INSTALLER == (
        "https://github.com/git-for-windows/git/releases/download/"
        "v2.56.0.windows.4/Git-2.56.0.4-64-bit.exe")
    assert WINDOWS_PYTHON == r"C:\Program Files\Python312\python.exe"


@pytest.mark.parametrize("host", [WIN, WIN_CPU], ids=["windows", "windows-cpu"])
def test_each_windows_installer_is_checked_before_it_runs_and_judged_after(host):
    """A download that stopped half way, or a page of HTML where the installer
    should be, is refused by its SHA-256 before anything runs; an installer
    that fails is refused by its exit code, then by the file it should have
    left. Each one is skipped on a box that already has what it installs, so
    a `switch` does not download them again."""
    from comfy_qa.provision import (
        GIT_INSTALLER_SHA256, PYTHON_INSTALLER_SHA256, WINDOWS_GIT, WINDOWS_PYTHON,
    )

    command = install_command(host)
    for digest, installed, word in (
        (GIT_INSTALLER_SHA256, WINDOWS_GIT, "/VERYSILENT"),
        (PYTHON_INSTALLER_SHA256, WINDOWS_PYTHON, "InstallAllUsers=1"),
    ):
        named = command.index(f"$i = '{installed}'; if (-not (Test-Path $i))")
        checked = command.index(digest, named)
        runs = command.index(word, checked)
        judged = command.index("ExitCode", runs)
        found = command.index("if (-not (Test-Path $i))", judged)
        assert named < checked < runs < judged < found
        following = command.find("$u = ", named)
        assert following == -1 or found < following, "all inside its own block"
    assert command.count("Get-FileHash -Algorithm SHA256") == 2
    assert command.count("-Wait -PassThru") == 3, (
        "Git, Python and the Visual C++ runtime: a GUI installer is not waited for by &")
    assert command.index("INSTALL_INCOMPLETE: the Git installer") > 0
    assert command.index("INSTALL_INCOMPLETE: the Python installer") > 0


def test_the_pinned_digests_are_the_ones_the_publishers_give():
    """Read 2026-11-03: Git's from the v2.56.0.windows.4 release notes (and
    GitHub's own asset digest); Python's computed here from python.org's file,
    whose MD5 matched the one python.org's release API publishes
    (5eddb0b6f12c852725de071ae681dde4) — python.org gives no SHA-256 for it."""
    from comfy_qa.provision import GIT_INSTALLER_SHA256, PYTHON_INSTALLER_SHA256

    assert GIT_INSTALLER_SHA256 == (
        "4961a4a8cc778d8b6e00a5819e5fe4d078b754555aff7aedc2d77a5d74c2de80")
    assert PYTHON_INSTALLER_SHA256 == (
        "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb")


@pytest.mark.parametrize("host", [WIN, WIN_CPU], ids=["windows", "windows-cpu"])
def test_no_windows_install_names_a_percent_variable(host):
    """The command reaches PowerShell through cmd.exe — the OpenSSH server's
    default shell on Windows — which expands `%NAME%` even inside double
    quotes; `%ProgramFiles%` is the likeliest way in. A guard, green before
    the installers were written, not a repro."""
    for command in (install_command(host), change_command(host, Build(version="v0.42.1"))):
        assert "%" not in command


@pytest.mark.parametrize("launch", [launch_command, launch_detached_command])
def test_a_windows_launch_can_find_git(launch):
    """Manager imports GitPython, which needs git on PATH when ComfyUI starts;
    a launch session that cannot see it starts ComfyUI with Manager off."""
    assert "C:\\Program Files\\Git\\cmd" in launch(WIN, DEFAULT.launch_args())


@pytest.mark.parametrize("host", [WIN, WIN_CPU], ids=["windows", "windows-cpu"])
def test_the_windows_install_puts_the_visual_cpp_runtime_on_before_torch_needs_it(host):
    """Found live on 2026-11-03 (live-s2, evidence/46): the install finished,
    and ComfyUI died at `import torch` with "Microsoft Visual C++
    Redistributable is not installed" and `[WinError 126] ... c10.dll or one
    of its dependencies`. Google's windows-2022 image does not carry it.

    Microsoft's permalink serves whatever is latest, so there is no digest to
    pin: the download is held to a valid Authenticode signature from
    Microsoft instead, run with the options Microsoft documents, and judged by
    its exit code and by the registry key Microsoft says records it. It is
    skipped where that key is already there, as Microsoft says to."""
    from comfy_qa.provision import VC_REDIST, VC_REDIST_KEY

    command = install_command(host)
    fetched = command.index(f"$u = '{VC_REDIST}'")
    skip = command.index(f"$i = '{VC_REDIST_KEY}'; if (-not (Test-Path $i))", fetched)
    signed = command.index("Get-AuthenticodeSignature", skip)
    runs = command.index("/install /quiet /norestart", signed)
    judged = command.index("ExitCode", runs)
    found = command.index("if (-not (Test-Path $i))", judged)
    assert fetched < skip < signed < runs < judged < found
    assert "O=Microsoft Corporation" in command
    assert found < command.index("comfy.exe' @a"), "before comfy install fetches torch"


def test_the_visual_cpp_runtime_is_microsofts_own_permalink():
    from comfy_qa.provision import VC_REDIST, VC_REDIST_KEY

    assert VC_REDIST == "https://aka.ms/vc14/vc_redist.x64.exe"
    assert VC_REDIST_KEY == (
        r"HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64")


def test_a_linux_install_has_no_visual_cpp_runtime():
    assert "vc_redist" not in install_command(LINUX)
