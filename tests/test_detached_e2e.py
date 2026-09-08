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
    assert f"comfy-qat logs {BOX}" in result.output
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
    assert lines[-1].startswith("open ")


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
    assert f"comfy-qat logs {BOX}" in result.output, "the log is on the box now"
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
    # 2, not 1. This is a refusal: the box is off, nothing was read and nothing
    # was changed, and every other refusal in this tool exits 2. It was 1
    # because `_act` flattened every reportable failure to 1 regardless of
    # whether the work had started.
    assert result.exit_code == 2
    assert "no ComfyUI and no log to follow" in result.output
    assert f"comfy-qat go {BOX}" in result.output
    assert not world.gc.did("ssh"), "nothing was run on a box that is off"


def test_logs_with_nothing_ever_launched_says_the_box_is_still_billing(world):
    from comfy_qa.provision import NO_LOG_EXIT

    world.cloud(statuses=["RUNNING"], log_exit=NO_LOG_EXIT)

    result = run(world, "host", "logs", BOX)

    no_traceback(result)
    # The ssh ran, but the precondition it checks — that something started
    # ComfyUI on that box — is unmet, and no log was read. A refusal.
    assert result.exit_code == 2
    assert "nothing has started ComfyUI there" in result.output
    assert "running and billing" in result.output
    assert f"comfy-qat down {BOX}" in result.output


def test_logs_on_the_local_machine_says_where_its_log_really_is(world):
    result = run(world, "host", "logs", "local")

    no_traceback(result)
    # Refused before Google is asked anything at all.
    assert result.exit_code == 2
    assert "this tool did not start its ComfyUI" in result.output
    assert "main.py" in result.output


def test_a_read_that_was_attempted_and_failed_still_exits_1(world):
    """The other half of the exit-code fix, and the half nothing was holding.

    Making refusals exit 2 is only correct if the failures that are NOT refusals
    keep exiting 1. Otherwise the fix is "everything from `_act` is a 2", which
    is the same flattening in the other direction and would be just as invisible
    — the suite went green on the 1s for months.

    Here the box is running, the log exists, and the SSH that would read it
    fails. That is the one raise in `read_logs` that is not marked `refusal`:
    the work started and did not finish, which is what 1 means.
    """
    from comfy_qa.gcloud import GcloudError

    cloud = world.cloud(statuses=["RUNNING"])

    def refuse(instance, zone, project, remote, *, stream: bool = True):
        raise GcloudError("the connection was closed by the remote host")

    cloud.ssh = refuse

    result = run(world, "host", "logs", BOX)

    no_traceback(result)
    assert result.exit_code == 1, (
        "an attempted read that failed is not a refusal, and must not become one"
    )
    assert "could not read the ComfyUI log" in result.output


# ----------------------------------------------------------- --new-window


def test_new_window_off_macos_refuses_without_starting_anything(world, monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    world.cloud(statuses=["TERMINATED"])

    result = run(world, "host", "go", BOX, "--new-window")

    no_traceback(result)
    # A platform check, before osascript is called — and the message says
    # "Nothing was started" in as many words, which is the definition of a 2.
    # `logs` was the command this defect got reported against; `go --new-window`
    # had it too, and so did `disconnect`, `down` and `switch` through
    # `put_away`. Fixing it at the `logs` call site would have left all four.
    assert result.exit_code == 2
    assert "can only open a macOS Terminal window" in result.output
    assert "Nothing was started" in result.output
    assert world.gc.calls == [], "it must not touch the cloud before handing over"
    assert not world.pid_file().exists()


def test_new_window_hands_over_the_command_it_was_actually_given(world, monkeypatch):
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
    # `--follow` used to be appended here regardless, and this test asserted it.
    # It was the defect: nobody typed the flag whose Ctrl-C stops ComfyUI.
    assert f"go {BOX}" in script, "the new window runs the go it was handed"
    assert "--follow" not in script, "and only what was asked for"
    assert str(world.config) in script, "and reads the same host list"
    assert world.gc.calls == [], "nothing was started in this terminal"



# --- what the spawned window is actually told to run -------------------------
#
# `--new-window` is the one command in this tool whose argv is executed
# somewhere this process cannot see. osascript returns 0 once *Terminal* has
# accepted the script, not once the command inside it has worked, so anything
# wrong with that argv fails in a window the person may already have closed,
# while this terminal says "opened a new Terminal window running: ..." and exits
# 0. There is no runtime check that can be trusted to be read, so the checking
# happens here.


def _handed_over(world, monkeypatch, *args: str) -> str:
    """The AppleScript `--new-window` would run, with osascript stubbed out.

    No window is opened and no `osascript` is executed: `subprocess.run` is
    replaced before the command is invoked.
    """
    import subprocess

    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    seen = {}

    class Ran:
        returncode = 0
        stdout = ""
        stderr = ""

    def record(argv, **kwargs):
        seen["args"] = argv
        return Ran()

    monkeypatch.setattr(subprocess, "run", record)
    world.cloud(statuses=["TERMINATED"])

    result = run(world, *args, "--new-window")

    no_traceback(result)
    assert result.exit_code == 0, result.output
    assert world.gc.calls == [], "nothing was started in this terminal"
    return seen["args"][-1]


def test_new_window_re_execs_the_live_spelling_not_the_deprecated_group(world, monkeypatch):
    """`host` is a deprecation window, not a second permanent spelling.

    cli.py registers `host` hidden and says in as many words that 26 command
    paths is not a simplification of 13. This argv was the one caller inside the
    tool still spelling a command the deprecated way — so the day that group is
    deleted, the breakage lands in a spawned Terminal window, on the command that
    starts a GPU box, where nobody is looking.
    """
    script = _handed_over(world, monkeypatch, "go", BOX)

    assert f"go {BOX}" in script, "it still hands over the `go` it was asked for"
    assert "host go" not in script, (
        "--new-window re-execs `host go`, the hidden deprecated spelling. When "
        "the deprecation window closes, this breaks inside a Terminal window "
        "that may already be shut. Hand over the live spelling: `go <name>`."
    )


def test_the_handed_over_command_line_is_one_the_cli_still_understands(world, monkeypatch):
    """The argv, parsed by the real app rather than read by eye.

    `--help` is appended so the parse is all that happens — no box is started —
    and a command word this app no longer registers exits 2 here instead of in a
    window that may close before it is read.
    """
    import shlex

    script = _handed_over(world, monkeypatch, "go", BOX)
    line = script[script.index('do script "') + len('do script "'):-1]
    words = shlex.split(line.replace('\\"', '"').replace("\\\\", "\\"))
    # Drop however many words name the tool itself — one for an installed
    # `comfy-qat`, three for `python -m comfy_qa` — and keep the rest verbatim.
    # Anything cleverer than this (searching for the host name, say) would strip
    # a leading `host` along with the interpreter and hide the very thing this
    # test exists to catch.
    prefix = len(shlex.split(lifecycle._tool_invocation()))
    argv = words[prefix:]
    assert argv[0] != "--config", "the argv is nothing but flags — the parse is broken"

    parsed = CliRunner().invoke(app, [*argv, "--help"])

    assert parsed.exit_code == 0, (
        f"--new-window hands a Terminal window {argv!r}, which this CLI does not "
        f"accept:\n{parsed.output}"
    )


def test_new_window_does_not_quietly_turn_on_follow(world, monkeypatch):
    """--follow changes what Ctrl-C destroys, so nothing may switch it on for you.

    Under `--follow`, `serve` streams the log over SSH and its `finally` calls
    `_stop_ours`, so Ctrl-C in that terminal stops ComfyUI on the box. Someone
    who spawns a window and then interrupts it expecting to detach kills the
    thing they just started, on a machine they are paying for.
    """
    script = _handed_over(world, monkeypatch, "go", BOX)

    assert "--follow" not in script, (
        "--new-window turned on --follow, which nobody asked for. Ctrl-C in that "
        "window then stops ComfyUI on the box."
    )


def test_new_window_forwards_follow_when_it_was_asked_for(world, monkeypatch):
    script = _handed_over(world, monkeypatch, "go", BOX, "--follow")

    assert f"go {BOX} --follow" in script, "asked for, so it is handed over"


def test_new_window_forwards_the_other_flags_it_was_given(world, monkeypatch):
    script = _handed_over(world, monkeypatch, "go", BOX, "--no-browser", "--no-install")

    assert str(world.config) in script, "and reads the same host list"
    assert "--no-browser" in script
    assert "--no-install" in script


def test_new_window_help_says_what_ctrl_c_in_that_window_reaches(world, monkeypatch):
    """The disclosure, in the place someone reads before they type it.

    `--follow`'s own help states the consequence; `--new-window`'s said nothing
    about it while silently implying it. Whatever this flag ends up doing, the
    interrupt is the part that costs money to learn by accident, so it is stated
    where the flag is documented.
    """
    result = CliRunner().invoke(app, ["go", "--help"])
    assert result.exit_code == 0, result.output
    help_text = " ".join(result.output.split())

    where = help_text.find("--new-window")
    assert where != -1, "--new-window is not in `go --help` at all"
    # Its own row and nothing else: `--help` is the next option, and reading past
    # it would let `--follow`'s row two rows above satisfy this test instead.
    ends = help_text.find("--help", where)
    disclosure = help_text[where:ends if ends != -1 else len(help_text)]

    assert "Ctrl-C" in disclosure, (
        "--new-window's help does not say what Ctrl-C in the spawned window "
        "reaches. That window may be running --follow, where Ctrl-C stops "
        "ComfyUI on the box — the flag that opts you in must say so."
    )
    assert "--follow" in disclosure, (
        "--new-window's help does not mention --follow, so the reader cannot "
        "tell which of the two interrupt behaviours their window will have."
    )
