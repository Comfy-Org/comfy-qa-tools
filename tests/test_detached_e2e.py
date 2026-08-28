"""`host go` and `host logs`, driven through the real CLI.

The same rule as `test_lifecycle_e2e.py`: what is asserted is what the person
sees — the lines printed and the exit code — because on a tool that starts GPU
instances "it looked like it worked" is expensive.

What is being defended here is the point of detaching. `go` has to come back, and
it has to come back only once ComfyUI answers; the URL has to be the last thing
said, because ComfyUI announces its own address and whatever is said after it is
what gets opened; and the terminal being free has to be stated rather than
implied, since the previous version of this command never gave it back.
"""

from __future__ import annotations


import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from comfy_qa import lifecycle, tunnel
from comfy_qa.cli import app

from fakes import Clock, FakeComfyUI, FakeGcloud, fake_tunnel_launcher, free_port, hosts_toml

BOX = "comfy-win"


class Box(FakeGcloud):
    """A `FakeGcloud` that also answers the questions detaching added.

    Subclassed rather than edited into `fakes.py`: the three new questions — is
    ComfyUI still alive, what does its log say, is there a log at all — belong to
    this feature, and a shared fake that grows a field per feature stops being
    readable by anyone.
    """

    def __init__(self, *, log: str = "", alive: str = "ALIVE",
                 log_exit: int = 0, **rest) -> None:
        super().__init__(**rest)
        self.log = log
        self.alive = alive
        self.log_exit = log_exit

    @staticmethod
    def _reading(remote: str) -> bool:
        return "tail -n" in remote or "Get-Content -Path" in remote

    @staticmethod
    def _asking(remote: str) -> bool:
        return "pgrep -f" in remote or "Get-Process -Name python" in remote

    def ssh_output(self, instance, zone, project, remote):
        if self._asking(remote):
            self.calls.append(("ssh_output", instance, zone, project))
            self.remote.append(remote)
            return self.alive
        if self._reading(remote):
            self.calls.append(("ssh_output", instance, zone, project))
            self.remote.append(remote)
            return self.log
        return super().ssh_output(instance, zone, project, remote)

    def ssh(self, instance, zone, project, remote, *, stream: bool = True):
        if self._reading(remote):
            self.calls.append(("ssh", instance, zone, project))
            self.remote.append(remote)
            return self.log_exit
        return super().ssh(instance, zone, project, remote, stream=stream)


@dataclass
class World:
    comfy: FakeComfyUI
    config: Path
    tunnel_dir: Path
    clock: Clock
    processes: list
    opened: list = field(default_factory=list)
    gc: Box | None = None

    @property
    def url(self) -> str:
        return self.comfy.url

    def cloud(self, **kwargs) -> Box:
        self.gc = Box(**kwargs)
        return self.gc

    def pid_file(self) -> Path:
        return tunnel.pid_file(BOX, self.tunnel_dir)


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
                 processes=processes)
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
    runner = CliRunner()
    import comfy_qa.gcloud as gcloud_module

    gc = world.gc or world.cloud()
    original = gcloud_module.Gcloud
    gcloud_module.Gcloud = lambda *a, **k: gc
    try:
        return runner.invoke(app, [*args, "--config", str(world.config)])
    finally:
        gcloud_module.Gcloud = original


def no_traceback(result) -> None:
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"a traceback reached the user:\n{result.exception!r}"
    )
    assert "Traceback" not in result.output


def serving_now(world: World):
    """A launch that really starts something: the box answers afterwards."""
    def launch() -> None:
        world.comfy.mode = "serving"
    return launch


# ------------------------------------------------------------------- go


def test_go_detaches_and_hands_the_terminal_back(world):
    """The whole point. It comes back, and it says so."""
    world.cloud(statuses=["RUNNING"], installed=True, on_launch=serving_now(world))

    result = run(world, "host", "go", BOX)

    no_traceback(result)
    assert result.exit_code == 0, result.output
    assert "stays running on the box after this command returns" in result.output
    assert "this terminal is free" in result.output
    assert f"comfy-qat host logs {BOX}" in result.output
    assert world.opened == [world.url]


def test_the_launch_is_detached_on_the_box_not_streamed_here(world):
    world.cloud(statuses=["RUNNING"], installed=True, on_launch=serving_now(world))

    run(world, "host", "go", BOX)

    launched = [r for r in world.gc.remote if "main.py" in r and "pip" not in r]
    assert launched, "nothing was launched"
    assert any("Start-Process" in r or "nohup" in r for r in launched), (
        "the launch has to survive this command returning"
    )
    assert any("comfyui.log" in r for r in launched), "and write somewhere readable"


def test_the_url_is_the_last_thing_said(world):
    """ComfyUI announces `http://127.0.0.1:8188` — true on the box, and on this
    machine the local install. Whatever is said last is what gets opened."""
    world.cloud(statuses=["RUNNING"], installed=True, on_launch=serving_now(world))

    result = run(world, "host", "go", BOX)

    lines = [line for line in result.output.splitlines() if line.strip()]
    assert world.url in lines[-1]
    assert lines[-1].startswith("Open ")


def test_go_still_does_not_return_until_comfyui_answers(world):
    """Detaching moved where the log goes, not where "up" is decided. A launch
    that came back on the box's say-so would leave a GPU machine billing while
    ComfyUI failed to import something."""
    world.cloud(statuses=["RUNNING"], installed=True)   # never starts serving

    result = run(world, "host", "go", BOX)

    no_traceback(result)
    assert result.exit_code == 1, "exiting 0 here is how a dead box looks healthy"
    assert "without ever answering" in result.output
    assert "billing" in result.output
    assert f"comfy-qat host logs {BOX}" in result.output, "the log is on the box now"
    assert world.opened == [], "no browser onto a URL that never answered"
    assert not world.pid_file().exists(), "the tunnel is closed on the way out"


def test_a_failed_launch_quotes_the_log_off_the_box(world):
    """The tester can no longer watch it scroll past, so it is fetched."""
    world.cloud(statuses=["RUNNING"], installed=True,
                log="  File main.py, line 1\nRuntimeError: no CUDA device")

    result = run(world, "host", "go", BOX)

    assert result.exit_code == 1
    assert "the last of its log on" in result.output
    assert "RuntimeError: no CUDA device" in result.output


def test_go_on_a_box_that_is_already_serving_still_changes_nothing(world):
    world.comfy.mode = "serving"
    world.cloud(statuses=["RUNNING"], installed=True)

    result = run(world, "host", "go", BOX)

    no_traceback(result)
    assert result.exit_code == 0, result.output
    assert not world.gc.did("ssh"), "nothing was launched"
    assert "ComfyUI 0.3.44" in result.output, "the stamp is the evidence line"


def test_two_boxes_can_be_up_at_once(world, tmp_path):
    """The reason for all of this. One terminal, two machines, two tunnels.

    Each `go` returns, so the second one is reachable at all — which under the
    old foreground launch it simply was not.
    """
    second = FakeComfyUI(mode="serving")
    try:
        world.config.write_text(
            world.config.read_text()
            + "\n[hosts.comfy-linux]\nkind = 'gce'\nos = 'Ubuntu 22.04'\n"
            "gpu = 'A100'\ngce_instance = 'comfy-linux'\n"
            "gce_zone = 'us-central1-a'\ngce_project = 'comfy-qa'\n"
            f"port = {second.port}\n",
            encoding="utf-8",
        )
        world.comfy.mode = "serving"
        world.cloud(statuses=["RUNNING"], installed=True)

        first = run(world, "host", "go", BOX)
        other = run(world, "host", "go", "comfy-linux")

        assert first.exit_code == 0, first.output
        assert other.exit_code == 0, other.output
        assert world.opened == [world.url, second.url], "both, in order, in one shell"
    finally:
        second.stop()


def test_follow_keeps_the_old_behaviour(world):
    """Streamed here, and Ctrl-C reaches ComfyUI. Unchanged on purpose."""
    world.cloud(statuses=["RUNNING"], installed=True, launch_exit=130)

    result = run(world, "host", "go", BOX, "--follow", "--no-browser")

    no_traceback(result)
    assert "its log follows" in result.output
    assert "Ctrl-C to stop it" in result.output
    assert "ComfyUI exited (130)" in result.output
    assert "this terminal is free" not in result.output
    launched = [r for r in world.gc.remote if "main.py" in r and "pip" not in r]
    assert not any("nohup" in r or "Start-Process" in r for r in launched)


# ----------------------------------------------------------------- logs


def test_tail_prints_that_many_lines_and_stops(world):
    """`--tail 50` answers "what did it say", not "what is it doing now", so it
    does not then sit there following."""
    world.cloud(statuses=["RUNNING"], installed=True)

    result = run(world, "host", "logs", BOX, "--tail", "50")

    no_traceback(result)
    assert result.exit_code == 0, result.output
    assert any("-Tail 50" in r or "tail -n 50" in r for r in world.gc.remote)
    assert not any("-Wait" in r or "tail -n 50 -f" in r for r in world.gc.remote)


def test_tail_with_follow_asked_for_does_both(world):
    world.cloud(statuses=["RUNNING"], installed=True)

    run(world, "host", "logs", BOX, "--tail", "50", "--follow")

    assert any("-Tail 50 -Wait" in r or "tail -n 50 -f" in r for r in world.gc.remote)


def test_logs_follows_by_default(world):
    world.cloud(statuses=["RUNNING"], installed=True)

    run(world, "host", "logs", BOX)

    assert any("-Wait" in r or " -f " in r for r in world.gc.remote)


def test_logs_on_a_stopped_box_answers_rather_than_waiting(world):
    """A command that hung here would be silently waiting on a box you may be
    paying for."""
    world.cloud(statuses=["TERMINATED"])

    result = run(world, "host", "logs", BOX)

    no_traceback(result)
    assert result.exit_code == 1
    assert "no ComfyUI and no log to follow" in result.output
    assert f"comfy-qat host go {BOX}" in result.output
    assert not world.gc.did("ssh"), "nothing was run on a box that is off"


def test_logs_with_nothing_ever_launched_says_the_box_is_still_billing(world):
    from comfy_qa.provision import NO_LOG_EXIT

    world.cloud(statuses=["RUNNING"], log_exit=NO_LOG_EXIT)

    result = run(world, "host", "logs", BOX)

    no_traceback(result)
    assert result.exit_code == 1
    assert "nothing has started ComfyUI there" in result.output
    assert "running and billing" in result.output
    assert f"comfy-qat host down {BOX}" in result.output


def test_logs_on_the_local_machine_says_where_its_log_really_is(world):
    result = run(world, "host", "logs", "local")

    no_traceback(result)
    assert result.exit_code == 1
    assert "this tool did not start its ComfyUI" in result.output
    assert "main.py" in result.output


# ----------------------------------------------------------- --new-window


def test_new_window_off_macos_refuses_without_starting_anything(world, monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    world.cloud(statuses=["TERMINATED"])

    result = run(world, "host", "go", BOX, "--new-window")

    no_traceback(result)
    assert result.exit_code == 1
    assert "can only open a macOS Terminal window" in result.output
    assert "Nothing was started" in result.output
    assert world.gc.calls == [], "it must not touch the cloud before handing over"
    assert not world.pid_file().exists()


def test_new_window_hands_over_the_following_form_of_the_command(world, monkeypatch):
    import subprocess

    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    seen = {}

    class Ran:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(subprocess, "run",
                        lambda args, **kwargs: (seen.update(args=args), Ran())[1])
    world.cloud(statuses=["TERMINATED"])

    result = run(world, "host", "go", BOX, "--new-window")

    no_traceback(result)
    assert result.exit_code == 0, result.output
    script = seen["args"][-1]
    assert f"host go {BOX} --follow" in script, "the new window is the one that streams"
    assert str(world.config) in script, "and reads the same host list"
    assert world.gc.calls == [], "nothing was started in this terminal"

