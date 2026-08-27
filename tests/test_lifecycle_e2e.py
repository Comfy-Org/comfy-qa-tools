"""The whole cloud lifecycle, driven through the real CLI.

Everything here runs `comfy-qat host ...` as a person would: the real Typer app,
the real argument parsing, the real config loading, the real tunnel bookkeeping
with real processes and real pid files, and a real HTTP request to a real server
for every readiness probe. Only two things are stood in for — Google Cloud, which
costs money, and gcloud's tunnel process, which needs Google.

What each test asserts is what the person sees: the lines printed, and the exit
code. A lifecycle failure that prints a traceback, or that exits 0, is the thing
these are here to catch — because on a tool that starts GPU instances, "it looked
like it worked" is expensive.
"""

from __future__ import annotations

import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from comfy_qa import lifecycle, tunnel
from comfy_qa.cli import app
from comfy_qa.gcloud import GcloudError

from fakes import Clock, FakeComfyUI, FakeGcloud, fake_tunnel_launcher, free_port, hosts_toml

BOX = "comfy-win"

# Exactly what gcloud printed when us-central1-a ran out of L4s.
STOCKOUT_OUTPUT = """Starting instance(s) comfy-win...
..........................................failed.
ERROR: (gcloud.compute.instances.start) ---
code: ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS
errorDetails:
- localizedMessage:
    locale: en-US
    message: A g2-standard-8 VM instance with 1 nvidia-l4 accelerator(s) is currently
      unavailable in the us-central1-a zone. Consider trying your request in the us-central1-b
      zone(s), which currently has capacity to accommodate your request.
"""


@dataclass
class World:
    """One machine, one fake cloud, one tunnel directory."""

    comfy: FakeComfyUI
    config: Path
    tunnel_dir: Path
    clock: Clock
    processes: list
    opened: list = field(default_factory=list)
    gc: FakeGcloud | None = None

    @property
    def url(self) -> str:
        return self.comfy.url

    def cloud(self, **kwargs) -> FakeGcloud:
        self.gc = FakeGcloud(**kwargs)
        return self.gc

    def pid_file(self) -> Path:
        return tunnel.pid_file(BOX, self.tunnel_dir)

    def tunnel_pids(self) -> list[int]:
        return [p.pid for p in self.processes]


def _dead_tunnel_launcher(processes: list):
    """A tunnel that is already gone by the time its pid is recorded.

    gcloud does exactly this when the port is taken or IAP says no: it exits in
    under a second, leaving a pid file pointing at nothing.
    """

    def launch(cmd, log):
        process = subprocess.Popen([sys.executable, "-c", "pass", *cmd])
        process.wait()
        processes.append(process)
        return process.pid

    return launch


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
    """Invoke the CLI exactly as the shell would, with this world's gcloud."""
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
    """typer.Exit is a clean stop; anything else reached the user as a crash."""
    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"a traceback reached the user:\n{result.exception!r}"
    )
    assert "Traceback" not in result.output


def nothing_left_running(world: World, result) -> None:
    """The rule that costs money when it is broken.

    A failure may leave the tunnel closed, or leave it open and say so. What it
    may never do is leave one open silently: the tunnel holds a local port onto a
    machine that is still billing, and the next command to use that port cannot
    tell which box answered.
    """
    if world.pid_file().exists():
        assert f"comfy-qat host down {BOX}" in result.output, (
            "a tunnel was left open without saying so, or how to close it"
        )
    assert not world.gc.did("stop_instance"), "nothing here stops the box on its own"
    if world.gc.running_now:
        assert "billing" in result.output or f"host down {BOX}" in result.output, (
            "the box is running and the failure never mentions the bill"
        )


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --------------------------------------------------------------------- go


def test_go_on_a_stopped_box_starts_tunnels_installs_and_serves(world):
    """The everyday path, end to end: nothing on the box, everything after it."""
    def launch():
        """ComfyUI comes up, and the launch stays in the foreground as it does."""
        world.comfy.mode = "serving"
        wait_until(lambda: world.opened)

    world.cloud(statuses=["TERMINATED", "RUNNING"], installed=False, on_launch=launch)

    result = run(world, "host", "go", BOX)

    no_traceback(result)
    assert result.exit_code == 0, result.output
    assert "stopped — starting it" in result.output
    assert world.gc.did("start_instance")
    assert f"tunnel open: {world.url}" in result.output
    assert "ComfyUI is not there — installing it" in result.output
    assert any("clone" in remote for remote in world.gc.remote), "it must install"
    # 0.0.0.0, not loopback: an IAP tunnel arrives on the instance's network
    # interface, so a ComfyUI bound to 127.0.0.1 serves and is unreachable.
    assert any("--listen 0.0.0.0" in remote for remote in world.gc.remote)
    assert world.opened == [world.url], "the browser opens on the machine you asked for"
    assert not world.gc.did("stop_instance"), "`go` never stops the box"


def test_go_on_a_box_that_is_already_serving_changes_nothing(world):
    """No install, no restart, no SSH: just the URL and what is behind it."""
    world.comfy.mode = "serving"
    world.cloud(statuses=["RUNNING"], installed=True)

    result = run(world, "host", "go", BOX)

    no_traceback(result)
    assert result.exit_code == 0, result.output
    assert not world.gc.did("start_instance")
    assert not world.gc.did("ssh"), "nothing was launched"
    assert not world.gc.did("ssh_output"), "nothing was even looked for"
    assert world.url in result.output
    assert "ComfyUI 0.3.44" in result.output, "the stamp is the evidence line"
    assert "cuda:0 NVIDIA L4" in result.output
    assert f"comfy-qat host down {BOX}" in result.output


def test_go_when_the_box_never_reaches_running_says_so_and_stops(world):
    world.cloud(statuses=["TERMINATED", "STAGING"])

    result = run(world, "host", "go", BOX)

    no_traceback(result)
    assert result.exit_code == 1
    assert "did not reach RUNNING" in result.output
    assert f"comfy-qat host down {BOX}" in result.output, "it is billing by now"
    nothing_left_running(world, result)
    assert not world.gc.did("ssh"), "no SSH against a box that never came up"
    assert not world.pid_file().exists(), "and no tunnel to a box that never came up"


def test_go_when_the_tunnel_dies_blames_the_tunnel_not_comfyui(world, monkeypatch):
    """The two look identical from here — silence on a local port — and sending
    someone onto the box to fix ComfyUI is an hour spent on the wrong machine."""
    monkeypatch.setattr(tunnel, "_spawn", _dead_tunnel_launcher(world.processes))
    world.cloud(statuses=["RUNNING"], installed=True)

    result = run(world, "host", "go", BOX)

    no_traceback(result)
    assert result.exit_code == 1
    assert f"the tunnel to {BOX} closed" in result.output
    assert "ComfyUI is not answering" not in result.output, "wrong culprit"
    assert str(tunnel.log_file(BOX, world.tunnel_dir)) in result.output
    assert f"comfy-qat host open {BOX}" in result.output
    nothing_left_running(world, result)
    assert not world.gc.did("ssh"), "it must not go on to install over a dead tunnel"


def test_go_when_the_tunnel_never_opens_repeats_what_gcloud_said(world, monkeypatch):
    """gcloud fails immediately far more often than it fails later, and an expired
    credential is the common one. The pid file used to be written over that corpse,
    so a credential failure arrived as "the box is up but ComfyUI is not
    answering" — with the machine left running on the strength of it."""
    def dying(cmd, log):
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("ERROR: (gcloud.compute.start-iap-tunnel) There was a problem "
                       "refreshing your current auth tokens: Reauthentication failed.\n")
        raise tunnel.TunnelError(
            f"the tunnel closed as soon as it was opened (gcloud exited 1). "
            f"gcloud said:\n        {tunnel.last_words(log)}",
            fix=f"read {log}. If it mentions credentials or reauthentication, your "
                f"session has expired:\n        gcloud auth login")

    monkeypatch.setattr(tunnel, "_spawn", dying)
    world.cloud(statuses=["TERMINATED", "RUNNING"], installed=True)

    result = run(world, "host", "go", BOX)

    no_traceback(result)
    assert result.exit_code == 1
    assert f"could not open the tunnel to {BOX}" in result.output
    assert "Reauthentication failed" in result.output
    assert "ComfyUI is not answering" not in result.output, "wrong culprit"
    assert not world.pid_file().exists(), "a dead tunnel must not leave a pid file"
    assert not world.gc.did("ssh"), "nothing runs on the box over a tunnel that failed"
    nothing_left_running(world, result)


def test_go_when_the_install_exits_non_zero_stops_before_launching(world):
    world.cloud(statuses=["RUNNING"], installed=False, install_exit=1)

    result = run(world, "host", "go", BOX)

    no_traceback(result)
    assert result.exit_code == 1
    assert "did not finish" in result.output
    assert "exit 1" in result.output
    assert f"comfy-qat host down {BOX}" in result.output
    assert not any("--listen" in remote for remote in world.gc.remote), (
        "there is nothing to launch after a failed install"
    )
    assert not world.pid_file().exists(), "the tunnel is closed on the way out"
    nothing_left_running(world, result)


def test_go_when_the_install_claims_success_but_installed_nothing(world):
    """Windows carries on after a failed step, so the script can exit 0 having
    done nothing at all — and the next thing you see is a launch failure."""
    world.cloud(statuses=["RUNNING"], installed=False, install_exit=0,
                install_works=False)

    result = run(world, "host", "go", BOX)

    no_traceback(result)
    assert result.exit_code == 1
    assert "did not finish" in result.output
    assert "reported success" in result.output
    assert "C:\\ComfyUI" in result.output
    assert not world.pid_file().exists(), "the tunnel is closed on the way out"


def test_go_when_comfyui_never_answers_after_being_launched(world):
    """Exit 0 from the launch is not the same as ComfyUI being up."""
    world.cloud(statuses=["RUNNING"], installed=True, launch_exit=0)

    result = run(world, "host", "go", BOX)

    no_traceback(result)
    assert result.exit_code == 1, "exiting 0 here is how a dead box looks healthy"
    assert "without ever answering" in result.output
    assert world.url in result.output
    assert f"comfy-qat host down {BOX}" in result.output
    assert world.opened == [], "no browser onto a URL that never answered"
    assert not world.pid_file().exists(), "the tunnel is closed on the way out"
    nothing_left_running(world, result)


def test_go_when_the_box_has_no_python_says_which_failure_it_is(world):
    world.cloud(statuses=["RUNNING"], installed=True, launch_exit=3)

    result = run(world, "host", "go", BOX)

    no_traceback(result)
    assert result.exit_code == 1
    assert "NO_PYTHON" in result.output
    assert "reset-windows-password" in result.output, "the way onto a Windows box"
    assert not world.pid_file().exists(), "the tunnel is closed on the way out"


def test_go_when_the_box_never_accepts_commands(world):
    world.cloud(statuses=["RUNNING"], installed=False, ssh_ready=False)

    result = run(world, "host", "go", BOX)

    no_traceback(result)
    assert result.exit_code == 1
    assert "not accepting commands" in result.output
    assert f"comfy-qat host down {BOX}" in result.output
    assert not world.pid_file().exists(), "the tunnel is closed on the way out"
    nothing_left_running(world, result)


REAUTH = (
    "ERROR: (gcloud.compute.ssh) There was a problem refreshing your current auth "
    "tokens: Reauthentication failed. cannot prompt during non-interactive execution."
)


def test_go_when_the_credential_dies_mid_flow_stops_immediately(world):
    """gcloud only offers to reauthenticate when stdin and stderr are terminals,
    and this tool captures output — so it does not prompt, it fails. Retrying that
    for five minutes is five minutes of GPU time on something that cannot work."""
    world.cloud(statuses=["TERMINATED", "RUNNING"], installed=False,
                ssh_ready=GcloudError(REAUTH, fix="gcloud auth login"))

    result = run(world, "host", "go", BOX)

    no_traceback(result)
    assert result.exit_code == 1
    assert "not signed in" in result.output
    assert "gcloud auth login" in result.output
    assert "Reauthentication failed" in result.output, "gcloud's own words survive"
    # One retry, not a five-minute wait. The firewall check also asks the box a
    # question before the probe gives up, so count the SSH that matters: the one
    # that tried and found the credential dead.
    assert world.gc.count("ssh_output") <= 2, "an expired credential is not waited out"
    assert not world.pid_file().exists(), "the tunnel is closed on the way out"
    nothing_left_running(world, result)


def test_go_on_a_stockout_names_the_zone_and_the_way_out(world):
    world.cloud(statuses=["TERMINATED"],
                start=GcloudError("---", raw=STOCKOUT_OUTPUT))

    result = run(world, "host", "go", BOX)

    no_traceback(result)
    assert result.exit_code == 1
    assert "no L4 capacity in us-central1-a" in result.output
    assert f"comfy-qat host move {BOX} --to us-central1-b" in result.output
    nothing_left_running(world, result)
    assert not world.pid_file().exists(), "no tunnel to a box that never started"


def test_go_on_a_local_machine_that_is_down_hands_over_the_start_command(world):
    result = run(world, "host", "go", "local")

    no_traceback(result)
    assert result.exit_code == 1
    assert "main.py" in result.output


# ------------------------------------------------------------ up / open / down


def test_up_then_open_twice_then_down_twice(world):
    """The sequence a person actually types, and the one that leaks money."""
    world.comfy.mode = "serving"
    world.cloud(statuses=["TERMINATED", "RUNNING"])

    up = run(world, "host", "up", BOX)
    no_traceback(up)
    assert up.exit_code == 0, up.output
    assert f"Open {world.url}" in up.output
    assert world.pid_file().exists()
    assert len(world.processes) == 1

    first = run(world, "host", "open", BOX)
    no_traceback(first)
    assert first.exit_code == 0
    assert "tunnel already open" in first.output
    assert str(world.processes[0].pid) in first.output, "it names the tunnel it found"

    second = run(world, "host", "open", BOX)
    no_traceback(second)
    assert "tunnel already open" in second.output
    assert len(world.processes) == 1, "a second tunnel on the same port answers at random"

    down = run(world, "host", "down", BOX)
    no_traceback(down)
    assert down.exit_code == 0, down.output
    assert "tunnel closed" in down.output
    assert f"{BOX} stopped" in down.output
    assert world.gc.did("stop_instance")
    assert not world.pid_file().exists(), "a pid file outliving its tunnel is a trap"
    assert wait_until(lambda: world.processes[0].poll() is not None), "the tunnel is gone"

    again = run(world, "host", "down", BOX)
    no_traceback(again)
    assert again.exit_code == 0, "putting away a machine twice is not a failure"
    assert "tunnel closed" not in again.output


def test_down_keep_running_closes_the_tunnel_and_says_it_still_costs(world):
    world.comfy.mode = "serving"
    world.cloud(statuses=["RUNNING"])
    run(world, "host", "up", BOX)

    result = run(world, "host", "down", BOX, "--keep-running")

    no_traceback(result)
    assert result.exit_code == 0
    assert "tunnel closed" in result.output
    assert "still billing" in result.output
    assert not world.gc.did("stop_instance")
    assert not world.pid_file().exists()


def test_open_dry_run_shows_the_command_and_starts_nothing(world):
    result = run(world, "host", "open", BOX, "--dry-run")

    no_traceback(result)
    assert "start-iap-tunnel" in result.output
    assert f"--local-host-port=localhost:{world.comfy.port}" in result.output
    assert world.processes == []
    assert not world.pid_file().exists()


def test_a_recycled_pid_is_never_reported_as_a_tunnel(world):
    """A pid file outlives a reboot; the number gets reused. Believing it means
    reporting a tunnel that is not there — and then killing whatever has it."""
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    world.processes.append(other)
    world.tunnel_dir.mkdir(parents=True, exist_ok=True)
    world.pid_file().write_text(str(other.pid))

    result = run(world, "host", "open", BOX)

    no_traceback(result)
    assert "tunnel already open" not in result.output
    assert len(world.processes) == 2, "it must open a real one"
    assert other.poll() is None, "and leave the unrelated process alone"


def test_down_never_signals_a_process_that_is_not_our_tunnel(world):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    world.processes.append(other)
    world.tunnel_dir.mkdir(parents=True, exist_ok=True)
    world.pid_file().write_text(str(other.pid))
    world.cloud(statuses=["RUNNING"])

    result = run(world, "host", "down", BOX, "--keep-running")

    no_traceback(result)
    assert result.exit_code == 0
    assert "tunnel closed" not in result.output, "there was no tunnel to close"
    assert not world.pid_file().exists(), "the stale record is cleared"
    time.sleep(0.2)
    assert other.poll() is None, "`down` killed an unrelated process"


def test_up_reports_a_box_that_boots_but_serves_nothing_as_a_failure(world):
    """The failure this whole tool exists to prevent."""
    world.cloud(statuses=["RUNNING"])

    result = run(world, "host", "up", BOX)

    no_traceback(result)
    assert result.exit_code == 1
    assert "ComfyUI is not answering" in result.output
    assert "billing" in result.output
    assert f"comfy-qat host down {BOX}" in result.output
    assert world.pid_file().exists(), "`up` leaves the tunnel for you to retry on"
    nothing_left_running(world, result)


# ------------------------------------------------------------------- move

INSTANCE = {
    "name": BOX,
    "status": "TERMINATED",
    "machineType": "https://www.googleapis.com/compute/v1/projects/p/zones/"
                   "us-central1-a/machineTypes/g2-standard-8",
    "disks": [{"boot": True, "deviceName": "comfy-win",
               "source": "https://www.googleapis.com/compute/v1/projects/p/zones/"
                         "us-central1-a/disks/comfy-win"}],
    "metadata": {"items": [{"key": "enable-windows-ssh", "value": "TRUE"}]},
}


def test_move_dry_run_shows_the_plan_and_changes_nothing(world):
    world.cloud(describe=INSTANCE)

    result = run(world, "host", "move", BOX, "--to", "us-central1-b", "--dry-run")

    no_traceback(result)
    assert result.exit_code == 0
    assert "snapshot the boot disk" in result.output
    assert "comfy-win-b" in result.output
    assert "nothing changed" in result.output
    assert not world.gc.did("snapshot_disk")
    assert not world.gc.did("create_instance_from_disk")


def test_move_does_nothing_when_the_box_simply_starts(world):
    """`move` with no zone asks Google by trying. If it works, there is no
    stockout and nothing to move."""
    world.cloud(statuses=["TERMINATED"])

    result = run(world, "host", "move", BOX)

    no_traceback(result)
    assert result.exit_code == 0
    assert "no move needed" in result.output
    assert f"comfy-qat host go {BOX}" in result.output
    assert not world.gc.did("snapshot_disk")


def test_move_that_fails_partway_says_nothing_was_removed(world):
    world.cloud(describe=INSTANCE,
                create_disk=GcloudError("disk quota exceeded", fix="ask for more disk"))

    result = run(world, "host", "move", BOX, "--to", "us-central1-b", "--yes")

    no_traceback(result)
    assert result.exit_code == 1
    assert "the move stopped at" in result.output
    # "nothing was removed" was the old reassurance, and it was the incomplete
    # one: true, and silent about the disk and snapshot the run had just created
    # and left billing. The contract now is that a failure accounts for what it
    # made, not only for what it spared.
    assert "untouched" in result.output, "the original box is still there"
    assert "billing" in result.output, "say what this run left running up a bill"
    assert f"{BOX} is untouched in us-central1-a" in result.output
    assert world.gc.did("snapshot_disk"), "the snapshot is where it got to"
    assert not world.gc.did("create_instance_from_disk")


def test_move_with_no_zone_asks_google_by_trying_to_start(world):
    """The stockout message is the only place Google says where there is room.

    A real move pays for that answer by starting the box — which was going to
    happen anyway. See the test below for why a dry run must not.
    """
    world.cloud(statuses=["TERMINATED"], describe=INSTANCE,
                start=GcloudError("---", raw=STOCKOUT_OUTPUT))

    result = run(world, "host", "move", BOX, "--yes")

    no_traceback(result)
    assert "us-central1-a has none free; us-central1-b does" in result.output


def test_a_dry_run_with_no_zone_refuses_rather_than_starting_the_box(world):
    """This test replaces one that asserted the opposite, and was a live blocker.

    `move` learns which zone has capacity by starting the instance and reading
    the stockout out of the error. Under `--dry-run` that means a flag whose
    whole promise is "I will not do anything" boots a GPU box and bills for it.
    Found by running the criteria against a real project.
    """
    world.cloud(statuses=["TERMINATED"], describe=INSTANCE,
                start=GcloudError("---", raw=STOCKOUT_OUTPUT))

    result = run(world, "host", "move", BOX, "--dry-run")

    no_traceback(result)
    assert result.exit_code == 2, "nothing was changed, so it is a refusal"
    assert "--to" in result.output, "name the form that works"


# --------------------------------------------------------------- stockouts


REAL_STOCKOUTS = [
    STOCKOUT_OUTPUT,
    # The other shape Google uses, with no zone suggestion at all.
    "ERROR: (gcloud.compute.instances.start) Could not fetch resource:\n"
    " - The zone 'projects/comfy-qa/zones/us-central1-a' does not have enough "
    "resources available to fulfill the request. Try a different zone, or try "
    "again later.\n",
    # And the raw state code, which is all that survives some failures.
    "'NULL:0/NULL:0/NULL:0 (state:STOCKOUT, sub-state:STOCKOUT, resource type:compute)'",
    "code: ZONE_RESOURCE_POOL_EXHAUSTED_WITH_DETAILS",
]


@pytest.mark.parametrize("text", REAL_STOCKOUTS)
def test_every_real_stockout_shape_is_recognised(text):
    assert lifecycle.is_capacity_failure(text)


@pytest.mark.parametrize("text", [
    "PERMISSION_DENIED: compute.instances.start",
    "Quota 'NVIDIA_L4_GPUS' exceeded. Limit: 0.0 in region us-central1.",
    "",
    None,
])
def test_what_is_not_a_stockout_is_not_called_one(text):
    """Quota is a stockout's twin and the advice is the opposite: a quota problem
    is fixed on your account, and moving zone will not help."""
    assert not lifecycle.is_capacity_failure(text)


def test_only_zones_are_repeated_back_as_zones():
    """`--to or` is not a command anyone can run."""
    assert lifecycle.suggested_zones(STOCKOUT_OUTPUT) == ["us-central1-b"]
    assert lifecycle.suggested_zones(
        "Consider trying your request in the us-central1-b, us-central1-c zone(s)"
    ) == ["us-central1-b", "us-central1-c"]
    assert lifecycle.suggested_zones(
        "Consider trying your request in the us-central1-b or us-central1-c zone(s)"
    ) == ["us-central1-b", "us-central1-c"]
    assert lifecycle.suggested_zones("no zones here") == []
    assert lifecycle.suggested_zones(None) == []


def test_a_zone_is_never_a_word_from_the_sentence():
    for text in [
        "Consider trying your request in the other zone",
        "Consider trying your request in the same zone",
    ]:
        assert lifecycle.suggested_zones(text) == []


def test_a_launch_that_dies_on_a_missing_dependency_repairs_itself_once(world):
    """From a real box on 2026-08-27.

    `host go` said "ComfyUI is already installed" and then handed over
    `ModuleNotFoundError: No module named 'sqlalchemy'`. Both statements were
    true: main.py was there, and the snapshot predated the dependency. Being
    accurate about a box you cannot use is not the same as being useful.
    """
    world.cloud(statuses=["RUNNING"], installed=True, launch_exit=[1, 0])

    result = run(world, "host", "go", BOX, "--no-browser")

    told = result.output
    assert "installing its requirements" in told
    assert "pip install -r requirements.txt" in world.gc.remote_commands_joined()
    assert world.gc.repairs == 1, "repaired exactly once"


def test_the_repair_is_tried_once_and_not_in_a_loop(world):
    """A box that fails for some other reason must not reinstall forever."""
    world.cloud(statuses=["RUNNING"], installed=True, launch_exit=[1, 1])

    result = run(world, "host", "go", BOX, "--no-browser")

    assert result.exit_code == 1
    assert result.output.count("installing its requirements") == 1
    assert world.gc.repairs == 1, "one repair, not a loop"


def test_a_repair_that_fails_is_not_followed_by_the_same_traceback_again(world):
    """Observed on a real box: pip could not reach pypi, and the tool relaunched
    anyway and printed the identical ModuleNotFoundError a second time.

    The second traceback teaches nothing. What the tester cannot guess is why pip
    failed — everything reaches the box perfectly well, so nobody thinks to check
    whether the box can reach anything.
    """
    world.cloud(statuses=["RUNNING"], installed=True,
                launch_exit=[1, 0], repair_exit=1)

    result = run(world, "host", "go", BOX, "--no-browser")

    assert result.exit_code == 1
    assert result.output.count("ModuleNotFoundError") == 0, "the fake prints none"
    assert "installing its requirements failed" in result.output
    assert "no route out" in result.output
    assert "add-access-config" in result.output
    assert world.gc.repairs == 1


def test_a_cpu_only_torch_is_found_before_the_launch_not_during_it(world):
    """From a real L4 box on 2026-08-27.

    `pip install -r requirements.txt` on Windows fetches PyPI's torch, which is
    CPU-only, and ComfyUI then dies with "Torch not compiled with CUDA enabled"
    — after the box has booted, tunnelled and started billing. Asking the box
    first costs one SSH round trip and turns a launch failure into a fix.
    """
    world.cloud(statuses=["RUNNING"], installed=True, verify="TORCH_NO_CUDA")

    result = run(world, "host", "go", BOX, "--no-browser")

    told = result.output
    assert "cannot see the" in told and "CPU-only build" in told
    assert "download.pytorch.org/whl/cu128" in world.gc.remote_commands_joined()
    assert world.gc.repairs == 1, "repaired once, before launching"


def test_a_box_that_is_ready_is_not_reinstalled(world):
    """The check must not become a reason to reinstall torch on every run."""
    world.cloud(statuses=["RUNNING"], installed=True, verify="READY")

    run(world, "host", "go", BOX, "--no-browser")

    assert world.gc.repairs == 0
    assert "download.pytorch.org" not in world.gc.remote_commands_joined()


def test_a_box_that_cannot_be_asked_is_still_launched(world):
    """The verification is a convenience, not a gate. A box that will not answer
    the question is still worth trying — the launch says what happened."""
    world.cloud(statuses=["RUNNING"], installed=True, verify="")

    result = run(world, "host", "go", BOX, "--no-browser")

    assert world.gc.repairs == 0
    assert "starting ComfyUI" in result.output


def test_the_two_checks_disagreeing_is_reported_not_crashed(world):
    """The install check says main.py is there and the verify says it is not.

    Only reachable if something changed the box between the two questions, which
    is why no test covered it — and why the branch carried an undefined name
    that ruff found and 1064 tests did not.
    """
    world.cloud(statuses=["RUNNING"], installed=True, verify="NO_COMFYUI")

    result = run(world, "host", "go", BOX, "--no-browser")

    no_traceback(result)
    assert result.exit_code == 1
    assert "has no ComfyUI in" in result.output
    assert "C:\\ComfyUI" in result.output, "name the place it looked"


def test_the_cpu_torch_repair_forces_the_reinstall(world):
    """Detection without an effective repair is worse than no detection: it
    reports the problem as handled and changes nothing."""
    world.cloud(statuses=["RUNNING"], installed=True, verify="TORCH_NO_CUDA")

    run(world, "host", "go", BOX, "--no-browser")

    sent = world.gc.remote_commands_joined()
    assert "--force-reinstall" in sent


def test_the_url_that_is_right_for_this_machine_is_said_before_the_log(world):
    """ComfyUI announces its own address — `http://127.0.0.1:8188` — which is
    correct on the box and is the tester's own machine here, where 8188 is the
    local install. It is the last line they read before opening a browser, and
    it sends them to the wrong machine. Observed exactly that."""
    world.cloud(statuses=["RUNNING"], installed=True)

    result = run(world, "host", "go", BOX, "--no-browser")

    assert "on this machine that is" in result.output
    assert world.url in result.output


def test_a_first_launch_opens_the_port_through_both_firewalls(world):
    """The defect this exists for: a box serving ComfyUI on its GPU, a tunnel
    open, and a browser saying "refused" — because 8188 is allowed through
    neither the VPC firewall nor the box's own, and nothing joins those facts up.

    Expecting a tester to write a firewall rule before their first launch is not
    a setup step, it is a trap.
    """
    # Not answering — which is what a first launch looks like, and the state in
    # which the firewalls are the likeliest cause.
    world.cloud(statuses=["RUNNING"], installed=True, firewall=())

    result = run(world, "host", "go", BOX, "--no-browser")

    assert world.gc.did("create_firewall_rule"), "the VPC rule"
    assert world.gc.created_firewall["source_ranges"] == "35.235.240.0/20", (
        "scoped to Google's tunnel range — never the internet"
    )
    assert "NetFirewallRule" in world.gc.remote_commands_joined(), "the box's own"
    assert "tunnel range only" in result.output


def test_a_box_that_already_answers_has_its_firewalls_left_alone(world):
    """Cheap and correct: if ComfyUI is answering, nothing needs opening."""
    world.comfy.mode = "serving"
    world.cloud(statuses=["RUNNING"], installed=True, firewall=())

    run(world, "host", "go", BOX, "--no-browser")

    assert not world.gc.did("create_firewall_rule")
    assert not world.gc.did("firewall_rules"), "not even asked"
