"""Changing machine, end to end.

`switch` is `go` with the step people forget on the front: a GPU box left
running bills all night whether or not anything is tunnelled to it.

These tests drive the real CLI through the real lifecycle. Only the two seams
that leave the machine are replaced — the subprocess that opens a tunnel, and
the HTTP probe that asks ComfyUI what it is — so the terminal output asserted
here is the terminal output a tester gets.

The case that matters most is the last kind: the box you asked for cannot start
because Google has no capacity for that card in that zone. That is routine on
GPUs, it is nobody's fault, and being told only that is where a test session
stops. The target is therefore brought up *before* anything else is stopped, so
a shortage leaves you exactly where you were, with somewhere to go next.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from comfy_qa import gcloud as gcloud_module
from comfy_qa import lifecycle
from comfy_qa import tunnel as tunnel_module
from comfy_qa.cli import app
from comfy_qa.config import Host
from comfy_qa.gcloud import GcloudError
from comfy_qa.lifecycle import alternatives, running_elsewhere
from comfy_qa.stamp import Stamp
from comfy_qa.tunnel import TunnelState

HOSTS = """\
[hosts.local]
kind = "local"
port = 8188

[hosts.comfy-win]
kind         = "gce"
os           = "Windows Server 2022"
gpu          = "L4"
gce_instance = "comfy-win"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8190

[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "A100"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-b"
gce_project  = "proj"
port         = 8191
"""

# A second Windows box, so `windows` alone is genuinely ambiguous.
TWO_WINDOWS = HOSTS + """
[hosts.comfy-win-2]
kind         = "gce"
os           = "Windows Server 2022"
gpu          = "A100-80GB"
gce_instance = "comfy-win-2"
gce_zone     = "us-central1-b"
gce_project  = "proj"
port         = 8192
"""

# One cloud box and nothing else to fall back to.
ONE_BOX = """\
[hosts.comfy-win]
kind         = "gce"
os           = "Windows Server 2022"
gpu          = "L4"
gce_instance = "comfy-win"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8190
"""

# Exactly what gcloud printed when us-central1-a ran out of L4s. The ERROR: line
# is literally `---`; the sentence a person can act on is further down.
STOCKOUT = """Starting instance(s) comfy-win...
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

WIN = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022", gpu="L4",
           gce_instance="comfy-win", gce_zone="us-central1-a", gce_project="proj")
LINUX = Host(name="comfy-linux", kind="gce", port=8191, os="Ubuntu 22.04", gpu="A100",
             gce_instance="comfy-linux", gce_zone="us-central1-b", gce_project="proj")
LOCAL = Host(name="local", kind="local", port=8188)

STAMP = Stamp(host="comfy-win", url="http://127.0.0.1:8190", comfyui_version="0.33.0")


def gcloud(statuses: dict[str, object], fail: Exception | None = None):
    """A Gcloud answering `describe` from a table, recording every call.

    A status may be a list, which is read one entry at a time and then holds —
    that is how a box that is TERMINATED and then RUNNING is described.
    """
    calls: list[str] = []
    table = {name: list(value) if isinstance(value, list) else [value]
             for name, value in statuses.items()}

    def runner(args, mode):
        key = " ".join(args)
        calls.append(key)
        if key.startswith("compute instances describe"):
            states = table.get(args[3]) or ["TERMINATED"]
            return {"status": states.pop(0) if len(states) > 1 else states[0]}
        if key.startswith("compute instances start"):
            if fail is not None:
                raise fail
            return ""
        if key.startswith("compute instances stop"):
            return ""
        raise AssertionError(f"unexpected: {key}")

    gc = gcloud_module.Gcloud(runner=runner)
    gc.calls = calls  # type: ignore[attr-defined]
    return gc


def tunnels(*open_for: str):
    """Stand in for the tunnel pid files, which live in the real config dir."""
    def status(name, directory=None):
        return TunnelState(host=name, pid=99 if name in open_for else None,
                           alive=name in open_for)
    return status


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """The real CLI, with only the tunnel subprocess and the HTTP probe replaced."""
    def invoke(*args, declared=HOSTS, statuses=None, open_tunnels=(), serving=(),
               fail=None):
        path = tmp_path / "hosts.toml"
        path.write_text(declared, encoding="utf-8")

        gc = gcloud(statuses or {}, fail=fail)
        opened: list[str] = []
        closed: list[str] = []

        monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: gc)
        monkeypatch.setattr(lifecycle, "tunnel_status", tunnels(*open_tunnels))
        monkeypatch.setattr(tunnel_module, "status", tunnels(*open_tunnels))
        monkeypatch.setattr(lifecycle, "open_tunnel", lambda host, directory=None, **kwargs: (
            opened.append(host.name)
            or TunnelState(host=host.name, pid=99, alive=True)))
        monkeypatch.setattr(lifecycle, "close_tunnel", lambda name, directory=None: (
            closed.append(name) or name in open_tunnels))
        monkeypatch.setattr(lifecycle, "probe",
                            lambda host: STAMP if host.name in serving else None)

        result = CliRunner().invoke(app, [*args, "--config", str(path)])
        result.calls = gc.calls        # type: ignore[attr-defined]
        result.opened = opened         # type: ignore[attr-defined]
        result.closed = closed         # type: ignore[attr-defined]
        return result

    return invoke


def stops(result) -> list[str]:
    return [call for call in result.calls if call.startswith("compute instances stop")]


# --- the six ways a switch ends -------------------------------------------

def test_the_target_is_stopped_and_startable(cli):
    result = cli("host", "switch", "comfy-win", "--no-browser",
                 statuses={"comfy-win": ["TERMINATED", "RUNNING"],
                           "comfy-linux": "RUNNING"},
                 serving=("comfy-win",))

    assert result.exit_code == 0
    assert "comfy-win is stopped — starting it" in result.output
    assert result.opened == ["comfy-win"], "no tunnel was opened to the new box"
    assert "http://127.0.0.1:8190" in result.output
    assert any("comfy-linux" in call for call in stops(result)), "the old box was left billing"
    assert "comfy-linux stopped" in result.output
    assert "comfy-qat host down comfy-win" in result.output


def test_the_target_is_already_serving(cli):
    """Nothing is restarted, and the tunnel that exists is reused, not stacked."""
    result = cli("host", "switch", "comfy-win", "--no-browser",
                 statuses={"comfy-win": "RUNNING"},
                 open_tunnels=("comfy-win",), serving=("comfy-win",))

    assert result.exit_code == 0
    assert result.opened == [], "a second tunnel was opened on the same port"
    assert not any(call.startswith("compute instances start") for call in result.calls)
    assert "ComfyUI answering" in result.output


def test_the_target_cannot_start_and_another_machine_can(cli):
    """The live failure this was written for: no L4 capacity in that zone.

    The box you were on is still up — the target is brought up first for exactly
    this reason — and the next command is on the screen rather than in the docs.
    """
    result = cli("host", "switch", "windows", "--no-browser",
                 statuses={"comfy-linux": "RUNNING"},
                 fail=GcloudError("---", raw=STOCKOUT))

    assert result.exit_code == 1
    assert "no L4 capacity in us-central1-a" in result.output
    assert "not a fault on your side" in result.output
    assert "comfy-linux is untouched" in result.output
    assert "Where you can test instead" in result.output
    assert "comfy-qat host switch comfy-linux   # Ubuntu 22.04, A100" in result.output
    assert "If it has to be comfy-win:" in result.output
    assert "comfy-qat host move comfy-win --to us-central1-b" in result.output
    assert stops(result) == [], "the machine you were working on was stopped anyway"


def test_up_gives_the_same_advice_as_switch_does(cli):
    """The advice belongs to the failure, not to one command that can hit it."""
    result = cli("host", "up", "windows", fail=GcloudError("---", raw=STOCKOUT))

    assert result.exit_code == 1
    assert "no L4 capacity in us-central1-a" in result.output
    assert "comfy-qat host switch comfy-linux" in result.output


def test_the_target_cannot_start_and_there_is_nowhere_else(cli):
    result = cli("host", "switch", "comfy-win", "--no-browser", declared=ONE_BOX,
                 fail=GcloudError("---", raw=STOCKOUT))

    assert result.exit_code == 1
    assert "no L4 capacity in us-central1-a" in result.output
    assert "No other machine is declared" in result.output
    assert "comfy-qat host discover" in result.output
    assert "comfy-qat host move comfy-win --to us-central1-b" in result.output


def test_an_ambiguous_selector_refuses_before_anything_is_touched(cli):
    result = cli("host", "switch", "windows", declared=TWO_WINDOWS,
                 statuses={"comfy-linux": "RUNNING"})

    assert result.exit_code == 2
    assert "comfy-win (Windows Server 2022, L4)" in result.output
    assert "comfy-win-2 (Windows Server 2022, A100-80GB)" in result.output
    assert "windows/l4" in result.output
    assert result.calls == [], "an ambiguous switch still called Google"


def test_an_unknown_selector_says_what_is_declared(cli):
    result = cli("host", "switch", "rtx4090")

    assert result.exit_code == 2
    assert "unknown host 'rtx4090'" in result.output
    assert "Declared: local, comfy-win, comfy-linux" in result.output
    assert result.calls == []


# --- what gets stopped, and what does not ---------------------------------

def test_a_dry_run_prints_the_plan_and_changes_nothing(cli):
    result = cli("host", "switch", "comfy-win", "--dry-run",
                 statuses={"comfy-linux": "RUNNING"})

    assert result.exit_code == 0
    assert "go to comfy-win (Windows Server 2022, L4)" in result.output
    assert "then stop comfy-linux (Ubuntu 22.04, A100) — running" in result.output
    assert "--dry-run: nothing changed" in result.output
    assert stops(result) == [], "a dry run stopped a machine"
    assert result.opened == [], "a dry run opened a tunnel"


def test_keep_others_leaves_them_running_and_says_so(cli):
    result = cli("host", "switch", "comfy-win", "--keep-others", "--no-browser",
                 statuses={"comfy-win": ["TERMINATED", "RUNNING"],
                           "comfy-linux": "RUNNING"},
                 serving=("comfy-win",))

    assert result.exit_code == 0
    assert stops(result) == []
    assert "--keep-others" in result.output


def test_a_box_that_is_already_stopped_is_not_stopped_again(cli):
    result = cli("host", "switch", "comfy-win", "--dry-run")

    assert "nothing else is running, so nothing to stop" in result.output


def test_the_target_is_never_in_its_own_stop_list(cli):
    result = cli("host", "switch", "comfy-win", "--dry-run",
                 statuses={"comfy-win": "RUNNING"})

    assert "stop comfy-win" not in result.output


def test_a_tunnel_left_open_counts_as_being_on_that_machine(cli):
    """The box is stopped but the tunnel still points at it — close it anyway."""
    result = cli("host", "switch", "comfy-win", "--dry-run",
                 open_tunnels=("comfy-linux",))

    assert "then stop comfy-linux" in result.output
    assert "tunnelled" in result.output


def test_switch_takes_a_description_just_like_go(cli):
    result = cli("host", "switch", "windows/l4", "--dry-run")

    assert result.exit_code == 0
    assert "windows/l4 -> comfy-win (Windows Server 2022, L4)" in result.output


# --- which machine am I on right now? -------------------------------------
#
# The other half of switching cheaply is seeing where you are. The tunnel is
# what makes a cloud box answer on 127.0.0.1, so it is the honest answer, and
# reading a pid file is free. Asking Google costs a call per box, so it is asked
# for rather than paid for on every list.

def test_the_state_column_shows_which_box_you_are_tunnelled_to(cli):
    result = cli("host", "list", open_tunnels=("comfy-linux",))

    assert result.exit_code == 0
    rows = {line.split()[0]: line for line in result.output.splitlines()[1:]}
    assert "tunnelled" in rows["comfy-linux"]
    assert "tunnelled" not in rows["comfy-win"]


def test_listing_asks_google_nothing_by_default(cli):
    result = cli("host", "list")

    assert result.calls == [], "listing made a cloud call"
    assert "STATE" in result.output


def test_live_asks_google_and_says_stopped_rather_than_terminated(cli):
    result = cli("host", "list", "--live", statuses={"comfy-win": "RUNNING"})

    rows = {line.split()[0]: line for line in result.output.splitlines()[1:]}
    assert "running" in rows["comfy-win"]
    assert "stopped" in rows["comfy-linux"]
    assert "TERMINATED" not in result.output
    assert any(call.startswith("compute instances describe") for call in result.calls)


# --- the pieces, without the CLI in the way -------------------------------

def test_the_local_install_is_never_something_switch_stops():
    """This tool did not start the local ComfyUI, so it does not get to stop it."""
    gc = gcloud({})
    assert running_elsewhere(gc, [LOCAL, WIN], WIN, tunnel_dir=None) == []
    assert not any("local" in call for call in gc.calls)


def test_both_signs_of_being_on_are_reported(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle, "tunnel_status", tunnels("comfy-linux"))
    gc = gcloud({"comfy-linux": "RUNNING"})

    found = running_elsewhere(gc, [WIN, LINUX], WIN, tunnel_dir=tmp_path)

    assert [(host.name, why) for host, why in found] == [
        ("comfy-linux", "running and tunnelled")
    ]


def test_the_same_operating_system_is_offered_first():
    """Someone who asked for Windows usually needs Windows; local is a last resort."""
    other_win = Host(name="win-b", kind="gce", port=8192, os="Windows Server 2022",
                     gpu="A100", gce_instance="win-b", gce_zone="us-central1-b",
                     gce_project="proj")

    ranked = alternatives([LOCAL, LINUX, other_win, WIN], WIN)

    assert [host.name for host in ranked] == ["win-b", "comfy-linux", "local"]


def test_a_machine_in_the_zone_that_just_refused_is_offered_last():
    """A shortage is a fact about one card in one zone, not about the account."""
    same_zone = Host(name="win-same", kind="gce", port=8193, os="Windows Server 2022",
                     gpu="A100", gce_instance="win-same", gce_zone="us-central1-a",
                     gce_project="proj")
    other_zone = Host(name="win-away", kind="gce", port=8194, os="Windows Server 2022",
                      gpu="A100", gce_instance="win-away", gce_zone="us-central1-b",
                      gce_project="proj")

    ranked = alternatives([same_zone, other_zone, WIN], WIN)

    assert [host.name for host in ranked] == ["win-away", "win-same"]
