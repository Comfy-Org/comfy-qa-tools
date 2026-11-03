"""Choosing the build, through the real CLI: `go`, `switch`, `create`, `stamp`.

What is asserted is what a person sees — the lines printed, the exit code, the
commands the box was sent — on a fake cloud and a real local HTTP server standing
in for ComfyUI (`fakes.py`). Each test names its criterion in criteria-step2.md
and was run red before the code it holds existed.
"""

from __future__ import annotations

import json
import re
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from comfy_qa import build as build_mod
from comfy_qa import lifecycle, tunnel
from comfy_qa.cli import app
from comfy_qa.provision import CHANGING, CLEANING, INSTALLING

from fakes import (
    SYSTEM_STATS, Clock, FakeComfyUI, FakeGcloud, fake_tunnel_launcher, free_port,
    hosts_toml,
)

BOX = "comfy-win"
TAG_SHA = "426f5eea52a7ece1dd599fff4cb6cd1f352ddd24"
PR_HEAD = "9350a08d80aaaaaabbbbbbbbccccccccdddddddd"
OTHER_SHA = "b0" * 20
FRONTEND_PR_HEAD = "e" * 40
REPO = "repos/Comfy-Org/ComfyUI"
FRONT = "repos/Comfy-Org/ComfyUI_frontend"

# What GitHub has, for these tests: two releases, one open PR, a frontend release.
GITHUB = {
    f"{REPO}/git/ref/tags/v0.43.0": {"object": {"sha": TAG_SHA}},
    f"{REPO}/git/ref/tags/v0.42.1": {"object": {"sha": "1" * 40}},
    f"{REPO}/pulls/17497": {"state": "open", "merged": False, "head": {"sha": PR_HEAD}},
    f"{FRONT}/releases/tags/v1.58.4": {"tag_name": "v1.58.4"},
    f"{FRONT}/pulls/5678": {"state": "open", "merged": False,
                            "head": {"sha": FRONTEND_PR_HEAD}},
}


def stats(version: str, torch: str = "2.12.1+cu130") -> dict:
    payload = json.loads(json.dumps(SYSTEM_STATS))
    payload["system"]["comfyui_version"] = version
    payload["system"]["pytorch_version"] = torch
    return payload


# The frontend as ComfyUI serves it: a page naming its entry script, which maps
# the main chunk, which carries the release it was built as.
def frontend_pages(version: str) -> dict:
    return {
        "/": ("text/html", '<!doctype html><html><head><script type="module" '
                           'crossorigin src="./assets/index-B08ReEXe.js"></script>'
                           "</head></html>"),
        "/assets/index-B08ReEXe.js": (
            "text/javascript",
            'const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=["./main-BJj46UXU.js",'
            '"./rolldown-runtime-CM1DJQSe.js"])))=>i.map(i=>d[i]);'),
        "/assets/main-BJj46UXU.js": (
            "text/javascript",
            "function initSentry({app:e,dsn:t,enabled:n,isCloud:r}){Ce({app:e,dsn:t,"
            f"enabled:n,release:`{version}`,normalizeDepth:8}})}}"),
    }


MANAGER_ON = {"/v2/manager/version": ("text/plain", "V4.3")}


class Box(FakeGcloud):
    """A box that keeps a record of its build, the way the scripts do.

    The install and the change write `Build.spec()` to the box; this reads it
    back out of the script it was sent, so a flow that installs one build and
    records another fails here as it would on a real machine. `branch` and
    `commit` are what git in the checkout would say; `frontend_root` is where a
    frontend PR's build landed, which only the box knows.
    """

    def __init__(self, *, record: str = "", commit: str = TAG_SHA, branch: str = "HEAD",
                 alive: str = "ALIVE", frontend_root: str = "", **rest) -> None:
        super().__init__(**rest)
        self.record = record
        self.commit = commit
        self.branch = branch
        self.alive = alive
        self.frontend_root = frontend_root

    def ssh_output(self, instance, zone, project, remote):
        if "COMMIT=" in remote and "RECORD=" in remote:
            self.calls.append(("ssh_output", instance, zone, project))
            self.remote.append(remote)
            return (f"COMMIT={self.commit}\nBRANCH={self.branch}\n"
                    f"RECORD={self.record}\nFRONTEND_ROOT={self.frontend_root}\n")
        if "pgrep -f" in remote or "Get-Process -Name python" in remote:
            self.calls.append(("ssh_output", instance, zone, project))
            self.remote.append(remote)
            return self.alive
        return super().ssh_output(instance, zone, project, remote)

    def ssh(self, instance, zone, project, remote, *, stream: bool = True):
        if "Stop-Process -Id" in remote or remote.startswith("kill "):
            # Stopping the ComfyUI that holds the port is not a launch.
            self.calls.append(("ssh", instance, zone, project))
            self.remote.append(remote)
            self.port_holder = ""
            return 0
        written = re.search(r"(comfyui=\S+ frontend=\S+ manager=\S+ torch=\S+"
                            r"(?: \w+_head=\w+)*)", remote)
        if written and (INSTALLING in remote or CHANGING in remote
                        or "build.txt" in remote):
            self.record = written.group(1)
            pr = re.search(r"comfyui=pr:(\d+)", self.record)
            if pr:
                self.branch = f"pr-{pr.group(1)}-solid-mode"
                self.commit = PR_HEAD
        return super().ssh(instance, zone, project, remote, stream=stream)


@dataclass
class World:
    comfy: FakeComfyUI
    config: Path
    tunnel_dir: Path
    clock: Clock
    processes: list
    hub: dict
    opened: list = field(default_factory=list)
    gc: Box | None = None
    asked: list = field(default_factory=list)

    def cloud(self, **kwargs) -> Box:
        self.gc = Box(**kwargs)
        return self.gc

    def serves(self, version: str, *, frontend: str | None = None,
               manager: bool = True, torch: str = "2.12.1+cu130") -> None:
        """What ComfyUI on the box answers once something is launched."""
        def launch() -> None:
            self.comfy.payload = stats(version, torch)
            self.comfy.pages.clear()
            if frontend:
                self.comfy.pages.update(frontend_pages(frontend))
            if manager:
                self.comfy.pages.update(MANAGER_ON)
            self.comfy.mode = "serving"
        self.gc.on_launch = launch


@pytest.fixture
def world(tmp_path, monkeypatch):
    comfy = FakeComfyUI(mode="reset")
    processes: list = []
    clock = Clock()
    tunnel_dir = tmp_path / "tunnels"
    config = hosts_toml(tmp_path / "hosts.toml", remote_port=comfy.port,
                        local_port=free_port())

    monkeypatch.setattr(tunnel, "TUNNEL_DIR", tunnel_dir)
    monkeypatch.setattr(tunnel, "_spawn", fake_tunnel_launcher(processes))
    monkeypatch.setattr(lifecycle, "_clock", clock.now)
    monkeypatch.setattr(lifecycle, "_pause", clock.pause)

    made = World(comfy=comfy, config=config, tunnel_dir=tunnel_dir, clock=clock,
                 processes=processes, hub=dict(GITHUB))

    def github(path):
        made.asked.append(path)
        return (200, made.hub[path]) if path in made.hub else (404, None)

    monkeypatch.setattr(build_mod, "github", github)
    monkeypatch.setattr(webbrowser, "open", lambda url: made.opened.append(url))
    yield made
    for process in processes:
        try:
            process.kill()
            process.wait(timeout=5)
        except OSError:
            pass
    comfy.stop()


def run(world: World, *args: str):
    import comfy_qa.gcloud as gcloud_module

    gc = world.gc or world.cloud()
    original = gcloud_module.Gcloud
    gcloud_module.Gcloud = lambda *a, **k: gc
    try:
        return CliRunner().invoke(app, [*args, "--config", str(world.config)])
    finally:
        gcloud_module.Gcloud = original


def flat(output: str) -> str:
    """What was printed, with its line wrapping undone."""
    return " ".join(output.split())


def sent(world: World, marker: str) -> list[str]:
    return [remote for remote in world.gc.remote if marker in remote]


# --- S2-1: the backend, on go -------------------------------------------------


def test_go_installs_a_named_release_and_says_which_build_it_chose(world):
    world.cloud(statuses=["RUNNING"], installed=False)
    world.serves("0.43.0", frontend="1.58.4")

    result = run(world, "go", BOX, "--version", "v0.43.0")

    assert result.exit_code == 0, result.output
    (install,) = sent(world, INSTALLING)
    assert "'--version', '0.43.0'" in install
    assert "build: ComfyUI v0.43.0" in result.output
    assert f"{REPO}/git/ref/tags/v0.43.0" in world.asked, "checked before anything"
    assert world.opened == [world.comfy.url]


def test_go_with_no_build_keeps_the_tip_of_the_default_branch_and_says_so(world):
    world.cloud(statuses=["RUNNING"], installed=False)
    world.serves("0.43.0")

    result = run(world, "go", BOX)

    assert result.exit_code == 0, result.output
    (install,) = sent(world, INSTALLING)
    for flag in ("--version", "--pr", "--commit"):
        assert f"'{flag}'" not in install
    assert "tip of its default branch" in result.output
    assert world.asked == [], "the default build asks GitHub nothing"


@pytest.mark.parametrize("args", [
    ("--version", "v0.43.0", "--pr", "17497"),
    ("--version", "v0.43.0", "--commit", TAG_SHA[:7]),
    ("--pr", "17497", "--commit", TAG_SHA[:7]),
])
def test_two_backends_are_refused_before_anything_reaches_google(world, args):
    world.cloud(statuses=["TERMINATED"], installed=False)

    result = run(world, "go", BOX, *args)

    assert result.exit_code == 2, result.output
    assert "only one of them can be right" in result.output
    assert world.gc.calls == [], f"Google was asked something: {world.gc.calls}"


def test_switch_takes_the_same_build_options(world):
    world.cloud(statuses=["RUNNING"], installed=False)
    world.serves("0.43.0")

    result = run(world, "switch", BOX, "--version", "v0.43.0", "--keep-others")

    assert result.exit_code == 0, result.output
    (install,) = sent(world, INSTALLING)
    assert "'--version', '0.43.0'" in install


# --- S2-2: the frontend, set separately -----------------------------------------


def test_a_frontend_release_reaches_comfyui_and_is_confirmed_as_served(world):
    world.cloud(statuses=["RUNNING"], installed=False)
    world.serves("0.43.0", frontend="1.58.4")

    result = run(world, "go", BOX, "--version", "v0.43.0", "--frontend-version", "v1.58.4")

    assert result.exit_code == 0, result.output
    launches = [r for r in world.gc.remote if "main.py --listen" in r]
    assert launches and all(
        "--front-end-version Comfy-Org/ComfyUI_frontend@v1.58.4" in r for r in launches)
    assert "frontend 1.58.4" in result.output


def test_a_frontend_change_alone_keeps_the_backend_the_box_has(world):
    world.cloud(statuses=["RUNNING"], installed=True,
                record="comfyui=v0.43.0 frontend=default manager=on torch=default")
    world.serves("0.43.0", frontend="1.58.4")

    result = run(world, "go", BOX, "--frontend-version", "v1.58.4")

    assert result.exit_code == 0, result.output
    assert "comfyui=v0.43.0 frontend=v1.58.4" in world.gc.record
    assert not sent(world, CHANGING), "a frontend release is a launch option, not an install"


def test_a_frontend_pr_is_built_on_the_box_and_served_from_where_it_landed(world):
    """S2-2: a frontend PR is built on the box by comfy-cli's own builder, the
    directory it landed in is read back off the box (FRONTEND_ROOT), and
    ComfyUI is started with `--front-end-root` at that directory."""
    landed = r"C:\comfy-qat\frontend-5678\dist"
    world.cloud(statuses=["RUNNING"], installed=False, frontend_root=landed)
    world.serves("0.43.0")

    result = run(world, "go", BOX, "--frontend-pr", "5678")

    assert result.exit_code == 0, result.output
    (install,) = sent(world, INSTALLING)
    assert "handle_temporary_frontend_pr" in install, "the PR is never built"
    assert "'#5678'" in install and "FRONTEND_ROOT=" in install
    launches = [r for r in world.gc.remote if "main.py --listen" in r]
    assert launches and all(f"--front-end-root {landed}" in r for r in launches)


# --- S2-3: Manager on by default --------------------------------------------------


def test_a_plain_go_installs_and_launches_with_manager(world):
    world.cloud(statuses=["RUNNING"], installed=False)
    world.serves("0.43.0")

    result = run(world, "go", BOX)

    assert result.exit_code == 0, result.output
    (install,) = sent(world, INSTALLING)
    assert "--skip-manager" not in install
    launches = [r for r in world.gc.remote if "main.py --listen" in r]
    assert launches and all("--enable-manager" in r for r in launches)
    assert "Manager on" in result.output


def test_no_manager_is_the_only_way_to_leave_it_out(world):
    world.cloud(statuses=["RUNNING"], installed=False)
    world.serves("0.43.0", manager=False)

    result = run(world, "go", BOX, "--no-manager")

    assert result.exit_code == 0, result.output
    (install,) = sent(world, INSTALLING)
    assert "'--skip-manager'" in install
    assert not [r for r in world.gc.remote if "--enable-manager" in r]


@pytest.mark.parametrize("command", ["go", "switch", "create"])
def test_help_says_manager_is_on_by_default(command):
    result = CliRunner().invoke(app, [command, "--help"], env={"COLUMNS": "200"})
    flat = " ".join(result.output.split())
    assert "--no-manager" in flat
    assert "on by default" in flat


# --- S2-7: ready means the build you asked for ---------------------------------


def test_a_box_serving_another_version_is_never_reported_ready(world):
    world.cloud(statuses=["RUNNING"], installed=False)
    world.serves("0.42.1")                      # asked for v0.43.0

    result = run(world, "go", BOX, "--version", "v0.43.0")

    assert result.exit_code == 1, result.output
    assert "came up on a different build from the one you asked for" in result.output
    assert "ComfyUI 0.42.1, not v0.43.0" in result.output
    assert f"comfy-qat go {BOX} --version v0.43.0" in result.output, "the fix"
    assert "billing" in result.output
    assert world.opened == [], "no browser onto the wrong build"
    assert "this terminal is free" not in result.output
    assert f"open {world.comfy.url}" not in result.output


def test_manager_asked_for_and_not_running_is_not_ready(world):
    world.cloud(statuses=["RUNNING"], installed=False)
    world.serves("0.43.0", manager=False)       # the default asks for Manager

    result = run(world, "go", BOX)

    assert result.exit_code == 1, result.output
    assert "Manager off, not on" in result.output
    assert world.opened == []


def test_a_served_frontend_that_is_not_the_one_asked_for_is_not_ready(world):
    world.cloud(statuses=["RUNNING"], installed=False)
    world.serves("0.43.0", frontend="1.57.7")   # ComfyUI fell back to its own

    result = run(world, "go", BOX, "--frontend-version", "v1.58.4")

    assert result.exit_code == 1, result.output
    assert "frontend 1.57.7, not v1.58.4" in result.output


def test_a_box_already_serving_the_wrong_build_is_not_handed_over(world):
    """The everyday path: a box that is already up never reaches a launch. Its
    record says v0.43.0 and it serves 0.42.1 — somebody moved the checkout —
    so asking for v0.43.0 puts it back rather than handing over 0.42.1."""
    world.comfy.payload = stats("0.42.1")
    world.comfy.pages.update(MANAGER_ON)
    world.comfy.mode = "serving"
    world.cloud(statuses=["RUNNING"], installed=True, port_holder="4242 python",
                record="comfyui=v0.43.0 frontend=default manager=on torch=default")
    world.serves("0.43.0")

    result = run(world, "go", BOX, "--version", "v0.43.0")

    assert result.exit_code == 0, result.output
    assert "ComfyUI 0.42.1, not v0.43.0" in result.output, "said, not hidden"
    assert sent(world, CHANGING), "the build was put back"
    assert world.opened == [world.comfy.url]


def test_a_plain_go_onto_a_serving_box_asks_the_box_nothing(world):
    """Nothing asked for, nothing checked: the everyday command stays one HTTP
    probe. A build option is how a box is held to a build."""
    world.comfy.payload = stats("0.42.1")
    world.comfy.mode = "serving"
    world.cloud(statuses=["RUNNING"], installed=True,
                record="comfyui=v0.43.0 frontend=default manager=on torch=default")

    result = run(world, "go", BOX)

    assert result.exit_code == 0, result.output
    assert not world.gc.did("ssh_output") and not world.gc.did("ssh")


def _printed_fix(output: str) -> list[str]:
    """The `comfy-qat go` line a wrong-build refusal printed, as argv."""
    lines = [line.strip() for line in output.splitlines()
             if line.strip().startswith(f"comfy-qat go {BOX}")]
    assert lines, f"no `comfy-qat go {BOX}` fix was printed:\n{output}"
    return lines[0].split()[1:]


def test_the_fix_for_manager_not_coming_up_names_it_and_puts_it_back(world):
    """S2-7 (S2-3), audit finding F2. The default build asks for Manager, so
    the fix printed when Manager does not come up has to name it: a bare
    `comfy-qat go <box>` onto a box that is serving asks the box nothing, by
    design, and handed the same Manager-less ComfyUI over as ready. Pasted
    exactly as printed, the fix must put Manager back."""
    world.cloud(statuses=["RUNNING"], installed=False)
    world.serves("0.43.0", manager=False)
    first = run(world, "go", BOX)
    assert first.exit_code == 1, first.output
    fix = _printed_fix(first.output)
    assert "--manager" in fix, f"the fix names no build: comfy-qat {' '.join(fix)}"

    # ComfyUI is still up on the box without Manager; the reinstall brings it.
    world.serves("0.43.0", manager=True)
    world.gc.remote.clear()
    world.opened.clear()
    second = run(world, *fix)

    assert second.exit_code == 0, second.output
    assert sent(world, CHANGING), "the build was not put back"
    assert "/v2/manager/version" in world.comfy.pages, "Manager is still off"
    assert world.opened == [world.comfy.url]


def test_the_printed_fix_never_hands_over_a_box_still_on_the_wrong_build(world):
    """The same fix, on a box where Manager still does not come up: refused
    again, never reported ready, nothing opened."""
    world.cloud(statuses=["RUNNING"], installed=False)
    world.serves("0.43.0", manager=False)
    first = run(world, "go", BOX)
    assert first.exit_code == 1, first.output

    world.opened.clear()
    second = run(world, *_printed_fix(first.output))

    assert second.exit_code == 1, second.output
    assert "Manager off, not on" in second.output
    assert world.opened == []


def test_go_in_a_new_window_hands_on_manager_when_it_was_named(world, monkeypatch):
    """`go --manager --new-window` used to hand the new window a plain `go`:
    `Build.flags` never wrote `--manager`, which is on by default. Named on the
    command line, it is named again in the command handed on."""
    handed: list[list[str]] = []
    monkeypatch.setattr("comfy_qa.lifecycle.in_a_new_window",
                        lambda rest, say: handed.append(list(rest)))
    world.cloud(statuses=["RUNNING"], installed=True)

    result = run(world, "go", BOX, "--manager", "--new-window")

    assert result.exit_code == 0, result.output
    assert handed and "--manager" in handed[0], handed


def test_a_torch_that_is_not_the_pin_is_never_reported_ready(world):
    """S2-7: torch is one of the six parts read back; the pin is held to it."""
    world.cloud(statuses=["RUNNING"], installed=False)
    world.serves("0.43.0", torch="2.13.0+cu130")

    result = run(world, "go", BOX, "--torch", "2.12.1+cu130")

    assert result.exit_code == 1, result.output
    assert "torch 2.13.0+cu130, not 2.12.1+cu130" in flat(result.output)
    assert world.opened == []


def test_a_checkout_on_another_commit_is_never_reported_ready(world):
    """S2-7: `--commit` asked for, and git on the box says another commit."""
    world.hub[f"{REPO}/commits/{OTHER_SHA[:10]}"] = {"sha": OTHER_SHA}
    world.cloud(statuses=["RUNNING"], installed=False)     # git stays on TAG_SHA
    world.serves("0.43.0")

    result = run(world, "go", BOX, "--commit", OTHER_SHA[:10])

    assert result.exit_code == 1, result.output
    assert f"commit {TAG_SHA[:10]}, not {OTHER_SHA[:10]}" in flat(result.output)
    assert world.opened == []


def test_a_box_on_another_pr_is_never_handed_over_as_the_pr_asked_for(world):
    """S2-7: the record says PR 17497 and git says the checkout is PR 999's
    branch. Asking for 17497 says so and puts 17497 back, rather than handing
    over PR 999."""
    world.comfy.payload = stats("0.43.0")
    world.comfy.pages.update(MANAGER_ON)
    world.comfy.mode = "serving"
    world.cloud(statuses=["RUNNING"], installed=True, port_holder="4242 python",
                branch="pr-999-another-change", commit="c" * 40,
                record="comfyui=pr:17497 frontend=default manager=on torch=default")
    world.serves("0.43.0")

    result = run(world, "go", BOX, "--pr", "17497")

    assert result.exit_code == 0, result.output
    assert "PR 999, not PR 17497" in flat(result.output), "said, not hidden"
    assert sent(world, CHANGING), "the PR asked for was put back"
    assert world.gc.branch.startswith("pr-17497-")


def test_a_part_that_cannot_be_read_is_said_and_does_not_block(world, monkeypatch):
    """machines.md and troubleshooting.md: a part that cannot be read is said
    to be unconfirmed and does not block. Manager's route that answers neither
    200 nor 404 is unread, never "off"."""
    from comfy_qa import stamp as stamp_mod

    monkeypatch.setattr(stamp_mod, "manager_state", lambda url, **kwargs: None)
    world.cloud(statuses=["RUNNING"], installed=False)
    world.serves("0.43.0", manager=False)

    result = run(world, "go", BOX)

    assert result.exit_code == 0, result.output
    assert ("could not read whether Manager is running off the running ComfyUI, so "
            "that part of the build is not confirmed") in flat(result.output)
    assert world.opened == [world.comfy.url]


# --- S2-8: changing the build on a box ------------------------------------------


def test_go_with_a_build_option_changes_the_build_and_keeps_the_nodes(world):
    world.cloud(statuses=["RUNNING"], installed=True,
                record="comfyui=v0.43.0 frontend=default manager=on torch=default")
    world.serves("0.42.1")

    result = run(world, "go", BOX, "--version", "v0.42.1")

    assert result.exit_code == 0, result.output
    (change,) = sent(world, CHANGING)
    assert "--restore" in change and "'--version', '0.42.1'" in change
    assert CLEANING not in change
    assert not sent(world, INSTALLING)
    assert "comfyui=v0.42.1" in world.gc.record


def test_a_box_already_serving_is_stopped_and_changed_when_asked(world):
    world.comfy.payload = stats("0.43.0")
    world.comfy.pages.update(MANAGER_ON)
    world.comfy.mode = "serving"
    world.cloud(statuses=["RUNNING"], installed=True, port_holder="4242 python",
                record="comfyui=v0.43.0 frontend=default manager=on torch=default")

    def relaunch():
        world.comfy.payload = stats("0.42.1")
        world.gc.port_holder = ""
    world.gc.on_launch = relaunch

    result = run(world, "go", BOX, "--version", "v0.42.1")

    assert result.exit_code == 0, result.output
    assert [r for r in world.gc.remote if "Stop-Process -Id 4242" in r], "stopped first"
    assert sent(world, CHANGING)
    assert world.opened == [world.comfy.url]


def test_switch_to_a_pr_shows_the_pr_and_its_commit(world):
    """S2-8 and the S2-11 shape: `switch` to a PR number, and the PR's commit is
    what the box is confirmed on."""
    world.cloud(statuses=["RUNNING"], installed=True,
                record="comfyui=v0.43.0 frontend=default manager=on torch=default")
    world.serves("0.43.0")

    result = run(world, "switch", BOX, "--pr", "17497", "--keep-others")

    assert result.exit_code == 0, result.output
    (change,) = sent(world, CHANGING)
    assert "'--pr', '#17497'" in change
    assert f"PR 17497, {PR_HEAD[:10]}" in result.output


def test_a_clean_install_is_only_on_request(world):
    world.cloud(statuses=["RUNNING"], installed=True,
                record="comfyui=v0.43.0 frontend=default manager=on torch=default")
    world.serves("0.42.1")

    result = run(world, "go", BOX, "--version", "v0.42.1", "--clean")

    assert result.exit_code == 0, result.output
    (install,) = sent(world, INSTALLING)
    assert CLEANING in install


def test_a_clean_install_does_not_say_comfyui_is_not_there(world):
    """Audit finding behaviour:F9. Live (evidence/51 line 7), `go --clean` on a
    box with v0.42.1 on it said `ComfyUI is not there — installing it`, two lines
    before it removed that ComfyUI. A clean install says what it is doing."""
    world.cloud(statuses=["RUNNING"], installed=True,
                record="comfyui=v0.43.0 frontend=default manager=on torch=default")
    world.serves("0.42.1")

    result = run(world, "go", BOX, "--version", "v0.42.1", "--clean")

    assert result.exit_code == 0, result.output
    assert "ComfyUI is not there" not in result.output
    assert ("ComfyUI is there and --clean was given — removing it and installing it "
            "from nothing") in flat(result.output)


# --- S2-9: bad input is refused before anything is created or charged ----------


@pytest.mark.parametrize("args, words", [
    (("--version", "v0.99.0"), "ComfyUI has no release v0.99.0"),
    (("--pr", "99999999"), "no pull request 99999999"),
    (("--commit", "deadbeefcafe"), "ComfyUI has no commit deadbeefcafe"),
    (("--frontend-version", "v9.9.9"), "the frontend has no release v9.9.9"),
])
def test_go_refuses_a_build_github_does_not_have_before_google_is_asked(world, args, words):
    world.cloud(statuses=["TERMINATED"], installed=False)

    result = run(world, "go", BOX, *args)

    assert result.exit_code == 2, result.output
    assert words in result.output
    assert world.gc.calls == [], f"Google was asked something: {world.gc.calls}"


def test_a_closed_pr_is_refused_by_switch_too(world):
    world.hub[f"{REPO}/pulls/12"] = {"state": "closed", "merged": False}
    world.cloud(statuses=["RUNNING"], installed=True)

    result = run(world, "switch", BOX, "--pr", "12")

    assert result.exit_code == 2, result.output
    assert "is closed" in result.output
    assert world.gc.calls == []


# --- S2-6: stamp shows the build that is running ---------------------------------


def test_stamp_shows_the_build_read_off_the_running_comfyui(world):
    world.comfy.payload = stats("0.43.0")
    world.comfy.pages.update(frontend_pages("1.58.4"))
    world.comfy.pages.update(MANAGER_ON)
    world.comfy.mode = "serving"
    world.cloud(statuses=["RUNNING"], installed=True, branch="pr-17497-solid-mode",
                commit=PR_HEAD,
                # What was ASKED for is not what is shown: the record says a
                # release, the checkout is on a PR, and the stamp says the PR.
                record="comfyui=v0.42.1 frontend=v1.57.7 manager=off torch=default")

    result = run(world, "stamp", BOX)

    assert result.exit_code == 0, result.output
    line = result.stdout.strip().splitlines()[-1]
    assert f"ComfyUI 0.43.0 (PR 17497, {PR_HEAD[:10]})" in line
    assert "frontend 1.58.4" in line
    assert "Manager on" in line
    assert "torch 2.12.1+cu130" in line
    assert "0.42.1" not in line and "1.57.7" not in line, "the record is not the box"


@pytest.mark.parametrize("record", [
    "comfyui=pr:17497 frontend=default manager=on torch=default",
    f"comfyui=commit:{OTHER_SHA} frontend=default manager=on torch=default",
], ids=["record-names-a-pr", "record-names-a-commit"])
def test_stamp_takes_the_commit_and_the_pr_from_git_never_from_the_record(world, record):
    """S2-6: a record that NAMES a PR or a commit is still only what was asked
    for. git on the box says master at the tag's commit, and that is what the
    stamp shows."""
    world.comfy.payload = stats("0.43.0")
    world.comfy.pages.update(MANAGER_ON)
    world.comfy.mode = "serving"
    world.cloud(statuses=["RUNNING"], installed=True, branch="master", commit=TAG_SHA,
                record=record)

    result = run(world, "stamp", BOX, "--json")

    assert result.exit_code == 0, result.output
    found = json.loads(result.stdout)
    assert found["comfyui_commit"] == TAG_SHA
    assert "comfyui_pr" not in found


def test_stamp_json_carries_the_build(world):
    world.comfy.payload = stats("0.43.0")
    world.comfy.pages.update(frontend_pages("1.58.4"))
    world.comfy.mode = "serving"
    world.cloud(statuses=["RUNNING"], installed=True)

    result = run(world, "stamp", BOX, "--json")

    found = json.loads(result.stdout)
    assert found["comfyui_version"] == "0.43.0"
    assert found["comfyui_commit"] == TAG_SHA
    assert found["frontend_version"] == "1.58.4"
    assert found["manager"] == "off"
    assert "comfyui_pr" not in found


def test_stamp_says_nothing_it_could_not_read(world):
    """A ComfyUI serving no frontend page and a box that would not say: no
    frontend, no commit — absent, never guessed."""
    world.comfy.mode = "serving"
    world.cloud(statuses=["RUNNING"], installed=True, commit="", branch="")

    result = run(world, "stamp", BOX, "--json")

    found = json.loads(result.stdout)
    assert "frontend_version" not in found and "comfyui_commit" not in found


# --- what the install says it is about to show --------------------------------


def test_a_windows_install_says_what_it_installs_and_not_apt(world):
    """Live on 2026-11-03 (live-s2, evidence/44): `go` on a Windows box said
    "what follows is the box's own output — apt, git, then pip" above a log
    that installs Git and Python from their publishers' installers. Windows
    has no apt, and a line that names the wrong steps is one more reason to
    believe the install has gone wrong while it is fine."""
    world.cloud(statuses=["RUNNING"], installed=False)
    world.serves("0.43.0")

    result = run(world, "go", BOX, "--version", "v0.43.0")

    assert result.exit_code == 0, result.output
    assert "apt" not in result.output
    assert "Git and Python, then comfy-cli and pip" in result.output


@pytest.mark.parametrize("os_name, steps", [
    ("Windows Server 2022", "Git and Python, then comfy-cli and pip"),
    ("Ubuntu 22.04", "apt, then comfy-cli and pip"),
])
def test_each_install_names_its_own_steps(os_name, steps):
    from comfy_qa.config import Host

    host = Host(name="b", kind="gce", port=8191, os=os_name, gpu="L4",
                gce_instance="b", gce_zone="z", gce_project="p")
    assert steps in lifecycle.install_preamble(host)
