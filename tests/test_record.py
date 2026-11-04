"""`comfy-qat record`, through the real CLI (S3-6, S3-8).

The recorder itself — the frontend's `pnpm comfy-test record` — is a person at a
browser, so it is the one process here that is a stand-in: `subprocess.run` is
answered for `git`, `pnpm install` and the recorder, and everything around it is
real code on the suite's fakes. What is held: the checkout is the matching
frontend, in comfy-qat's own folder; a box serving another frontend is refused;
ComfyUI runs in multi-user mode while recording, is said to, and is put back;
the recorder is `pnpm comfy-test record --backend http://127.0.0.1:<port>`, on a
dev server port that is free and never 8188-8190, kept on loopback; and Node and
pnpm are checked with the install commands printed, never run.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from comfy_qa import lifecycle, tunnel
from fakes import Clock, FakeGcloud, fake_tunnel_launcher
from qaworld import BOX, FRONT, flat, make, package_json, run


class RecordBox(FakeGcloud):
    """A box whose ComfyUI is stopped and started the way `record` does it:
    the launch's own command line says whether `--multi-user` is on it."""

    def __init__(self, **rest):
        super().__init__(statuses=["RUNNING"], installed=True,
                         port_holder="4242 python", **rest)
        self.world = None
        self.launches: list[str] = []

    def ssh(self, instance, zone, project, remote, *, stream=True):
        if remote.startswith("kill ") or "Stop-Process -Id" in remote:
            self.calls.append(("ssh", instance, zone, project))
            self.remote.append(remote)
            self.port_holder = ""
            self.world.comfy.mode = "reset"
            return 0
        if "main.py" in remote:
            self.launches.append(remote)
            self.world.serves(self.world.serving, multi_user="--multi-user" in remote)
            self.port_holder = "4242 python"
            self.calls.append(("ssh", instance, zone, project))
            self.remote.append(remote)
            return 0
        return super().ssh(instance, zone, project, remote, stream=stream)


@pytest.fixture
def world(monkeypatch, tmp_path):
    box = RecordBox()
    made = make(monkeypatch, tmp_path, gc=box, fake_tunnels=False)
    box.world = made
    made.serving = "1.60.0"
    made.serves("1.60.0")
    made.hub[f"{FRONT}/contents/package.json?ref=core/1.60"] = package_json("1.60.0")
    made.hub[f"{FRONT}/contents/package.json?ref=v1.58.5"] = package_json("1.58.5")
    made.hub[f"{FRONT}/contents/package.json?ref=v1.60.0"] = package_json("1.60.0")
    processes: list = []
    clock = Clock()
    monkeypatch.setattr(tunnel, "_spawn", fake_tunnel_launcher(processes))
    monkeypatch.setattr(lifecycle, "_clock", clock.now)
    monkeypatch.setattr(lifecycle, "_pause", clock.pause)
    made.ran = []
    made.recorder = []
    # Node and pnpm are there, whatever the runner has: a test about `record` is
    # not a test of the CI image. The test about their absence takes them away.
    import shutil

    real_which = shutil.which
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: (
        f"/usr/local/bin/{name}" if name in ("node", "pnpm") else real_which(name, *a, **k)))
    real = subprocess.run

    def fake_run(argv, *args, **kwargs):
        words = [str(word) for word in argv] if isinstance(argv, (list, tuple)) else []
        if words[:2] == ["git", "clone"]:
            made.ran.append(words)
            target = Path(words[-1])
            (target / ".git").mkdir(parents=True)
            (target / "package.json").write_text(json.dumps({"version": made.serving}),
                                                 encoding="utf-8")
            return subprocess.CompletedProcess(words, 0, "", "")
        if words[:1] == ["git"] and "status" in words:
            made.ran.append(words)
            out = "?? browser_tests/tests/picker.spec.ts\n" if made.recorder else ""
            return subprocess.CompletedProcess(words, 0, out, "")
        if words[:1] == ["git"]:
            made.ran.append(words)
            return subprocess.CompletedProcess(words, 0, "", "")
        if words[:2] == ["pnpm", "install"]:
            made.ran.append(words)
            (Path(kwargs["cwd"]) / "node_modules").mkdir()
            return subprocess.CompletedProcess(words, 0, "", "")
        if words[:2] == ["pnpm", "comfy-test"]:
            from comfy_qa import record

            made.recorder.append({"argv": words, "cwd": kwargs.get("cwd"),
                                  "env": kwargs.get("env") or {},
                                  "multi_user": record.multi_user(made.comfy.url)})
            return subprocess.CompletedProcess(words, 0)
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", fake_run)
    yield made
    for process in processes:
        try:
            process.kill()
            process.wait(timeout=5)
        except OSError:
            pass
    made.comfy.stop()


def test_record_starts_the_frontends_own_recorder_against_the_box(world):
    result = run(world, "record", BOX, "1.60")

    assert result.exit_code == 0, result.output
    (started,) = world.recorder
    assert started["argv"][:5] == ["pnpm", "comfy-test", "record", "--backend",
                                   world.comfy.url]
    assert world.comfy.url.startswith("http://127.0.0.1:")


def test_the_checkout_is_the_releases_core_branch_in_comfy_qats_own_folder(world):
    """S3-6: the release's `core/<version>` branch ("Check out core/1.60
    branch"), cloned beside the host list — never the user's own checkout."""
    run(world, "record", BOX, "1.60")

    (clone,) = [words for words in world.ran if words[:2] == ["git", "clone"]]
    folder = world.config.parent / "frontend" / "core-1.60"
    assert clone == ["git", "clone", "--depth", "1", "--branch", "core/1.60",
                     "https://github.com/Comfy-Org/ComfyUI_frontend.git", str(folder)]
    assert world.recorder[0]["cwd"] == str(folder)
    assert ["pnpm", "install", "--frozen-lockfile"] in world.ran


def test_with_no_release_the_checkout_is_the_frontend_the_box_serves(world):
    run(world, "record", BOX)

    (clone,) = [words for words in world.ran if words[:2] == ["git", "clone"]]
    assert clone[5] == "v1.60.0"


def test_a_box_serving_another_frontend_is_refused_before_anything_is_cloned(world):
    """S3-6 and S3-8: the mismatch, the reason, and the command that fixes it."""
    world.serving = "1.58.5"
    world.serves("1.58.5")

    result = run(world, "record", BOX, "1.60")

    assert result.exit_code == 2, result.output
    said = flat(result.output)
    assert "serves frontend 1.58.5, and core/1.60 is 1.60.0" in said
    assert f"comfy-qat go {BOX} --frontend-version v1.60.0" in result.output
    assert world.ran == [] and world.recorder == []


def test_multi_user_is_switched_on_while_recording_said_and_put_back(world):
    """S3-6: "multiuser mode required" — on for the recording, and back after."""
    result = run(world, "record", BOX, "1.60")

    assert result.exit_code == 0, result.output
    assert world.recorder[0]["multi_user"] is True, "the recorder ran in single-user mode"
    assert ["--multi-user" in launch for launch in world.gc.launches] == [True, False]
    said = flat(result.output)
    assert f"switching ComfyUI on {BOX} to multi-user mode for the recording" in said
    assert f"ComfyUI on {BOX} is back in single-user mode" in said
    from comfy_qa import record

    assert record.multi_user(world.comfy.url) is False


def test_a_box_already_in_multi_user_mode_is_left_as_it_is(world):
    world.serves("1.60.0", multi_user=True)

    result = run(world, "record", BOX, "1.60")

    assert result.exit_code == 0, result.output
    assert world.gc.launches == []
    assert "switching ComfyUI" not in result.output


def test_the_recorders_dev_server_is_on_a_free_port_never_8188_to_8190_and_on_loopback(
        world, monkeypatch):
    monkeypatch.setenv("VITE_REMOTE_DEV", "true")

    run(world, "record", BOX, "1.60")

    env = world.recorder[0]["env"]
    port = int(env["COMFY_TEST_DEV_PORT"])
    assert port not in (8188, 8189, 8190) and 5173 <= port < 5300
    assert "VITE_REMOTE_DEV" not in env, "the one switch that binds 0.0.0.0 was passed on"


def test_options_after_a_double_dash_go_to_the_recorder_as_they_are(world):
    """Everything after `--` is the recorder's — including what looks like one
    of comfy-qat's own options, so `--config` goes in front, at the root."""
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    result = CliRunner().invoke(app, ["--config", str(world.config), "record", BOX, "--",
                                      "--workflow", "default", "--tags", "@canvas"])

    assert result.exit_code == 0, result.output

    assert world.recorder[0]["argv"][5:] == ["--workflow", "default", "--tags", "@canvas"]


def test_what_the_recording_left_is_remembered_for_the_bug_draft(world):
    from comfy_qa import results

    result = run(world, "record", BOX, "1.60")

    assert "the recording left 1 file in the checkout" in flat(result.output)
    assert results.recordings_file(world.config).exists()


@pytest.mark.parametrize("missing, shown", [
    ({"node"}, ["curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh "
                "| bash", "nvm install 26"]),
    ({"pnpm"}, ["corepack enable"]),
])
def test_node_and_pnpm_are_checked_and_the_install_commands_printed_never_run(
        world, monkeypatch, missing, shown):
    import shutil

    present = shutil.which
    monkeypatch.setattr(shutil, "which",
                        lambda name, *a, **k: None if name in missing else present(name, *a, **k))

    result = run(world, "record", BOX, "1.60")

    assert result.exit_code == 2, result.output
    lines = [line.strip() for line in result.output.splitlines()]
    for command in shown:
        assert command in lines, result.output
    assert "nothing is installed for you" in flat(result.output)
    assert world.ran == [] and world.recorder == []


def test_a_box_that_is_not_serving_is_refused_with_how_to_start_it(world):
    world.comfy.mode = "reset"

    result = run(world, "record", BOX, "1.60")

    assert result.exit_code == 2, result.output
    assert "is not serving here, so there is nothing to record against" in flat(result.output)
    assert f"comfy-qat go {BOX}" in result.output


def test_your_own_comfyui_is_not_switched_into_multi_user_mode(monkeypatch, tmp_path):
    from fakes import free_port

    path = tmp_path / "hosts.toml"
    path.write_text(f"[hosts.mine]\nkind = 'local'\nport = {free_port()}\n",
                    encoding="utf-8")
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    result = CliRunner().invoke(app, ["record", "mine", "--config", str(path)])

    assert result.exit_code == 2, result.output
    assert "does not do to your own install" in flat(result.output)
    assert "pnpm comfy-test record --distribution local" in result.output


def test_the_multi_user_reading_is_comfyuis_own_users_route():
    """`GET /users`: `users` with `--multi-user`, `migrated` without it
    (app/user_manager.py, v0.43.0)."""
    from fakes import FakeComfyUI

    from comfy_qa import record

    comfy = FakeComfyUI(pages={"/api/users": ("application/json",
                                              json.dumps({"storage": "server",
                                                          "users": {}}))})
    try:
        assert record.multi_user(comfy.url) is True
        comfy.pages["/api/users"] = ("application/json",
                                     json.dumps({"storage": "server", "migrated": True}))
        assert record.multi_user(comfy.url) is False
        comfy.mode = "reset"
        assert record.multi_user(comfy.url) is None
    finally:
        comfy.stop()


def test_a_free_port_is_never_one_a_comfyui_or_a_tunnel_lives_on():
    from comfy_qa import record

    assert record.free_port(ports=[8188, 8189, 8190, 8191], busy=lambda port: False) == 8191
    assert record.free_port({5173}, ports=[5173, 5174], busy=lambda port: False) == 5174
