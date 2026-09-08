"""The sentences that tell you whether a machine is still costing you money.

Every line held here is one a person reads at the end of a session to decide
whether to close the laptop. An audit ran ten sentinels through the whole suite
and found that eight of these could be changed to anything at all — six with no
test failing anywhere, two held only by `test_docs`, which asks whether a
sentence has a troubleshooting entry and not whether the tool still says it.

That distinction is the whole reason this file exists. A documentation entry is
evidence the sentence was written down once. It is not evidence the command
still prints it, and for a sentence about money those are not close enough to
substitute. So both of the docs-only ones are pinned here too — the entry stays,
and this is what fails when the behaviour goes.

Two rules these tests keep, because the audit found tests elsewhere that broke
both:

  * the oracle is the literal text a user would read, typed out here. Never
    `lifecycle.stop_paying(host)` or a constant imported from the subject — a
    test that asserts a value against the thing that produced it cannot fail.
  * the command is DRIVEN. `down`, `disconnect` and `--new-window` go through
    the real CLI with a fake cloud underneath; the launch wait goes through
    `start_detached` with a scripted `Gcloud`, which is how the rest of the
    suite drives it. Nothing here asserts on a sentinel it inserted itself.
"""

from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from comfy_qa.config import Host
from comfy_qa.gcloud import Gcloud, GcloudError
from comfy_qa.host import app
from comfy_qa.lifecycle import (
    LifecycleError,
    bring_up,
    in_a_new_window,
    start_detached,
)
from comfy_qa.provision import GONE

HOSTS = """\
[hosts.comfy-win]
kind         = "gce"
os           = "Windows Server 2022"
gpu          = "L4"
gce_instance = "comfy-win"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8190
"""

WIN = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022",
           gpu="L4", gce_instance="comfy-win", gce_zone="us-central1-a",
           gce_project="proj")
LINUX = Host(name="comfy-linux", kind="gce", port=8191, os="Ubuntu 22.04",
             gpu="A100", gce_instance="comfy-linux", gce_zone="us-central1-a",
             gce_project="proj")


def said():
    lines: list[str] = []
    return lines, lines.append


class Cloud:
    """A cloud that answers the two questions `down` asks and records both.

    Anything else raises, for the reason the fixture in `test_host_costs` gives:
    a command reaching a call this was not told to expect is the failure, not
    something to answer plausibly.
    """

    def __init__(self, *, status="RUNNING", stop=None):
        self.calls: list[str] = []
        self._status = status
        self._stop = stop

    def instance_status(self, instance, zone, project):
        self.calls.append("instance_status")
        if isinstance(self._status, Exception):
            raise self._status
        return self._status

    def stop_instance(self, instance, zone, project):
        self.calls.append("stop_instance")
        if self._stop is not None:
            raise self._stop
        return ""

    def __getattr__(self, name):
        def unexpected(*args, **kwargs):
            raise AssertionError(f"{name} was not expected in this test")
        return unexpected


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """The real CLI, a fake cloud, and tunnel records kept out of the way."""
    from comfy_qa import gcloud as gcloud_module, tunnel as tunnel_module

    monkeypatch.setattr(tunnel_module, "TUNNEL_DIR", tmp_path / "tunnels")
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")

    def invoke(*args, cloud=None):
        cloud = cloud if cloud is not None else Cloud()
        monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: cloud)
        result = CliRunner().invoke(app, [*args, "--config", str(path)])
        result.cloud = cloud        # type: ignore[attr-defined]
        return result

    return invoke


def scripted(*, status="RUNNING", alive=GONE):
    """A real `Gcloud` whose box answers the way this test wants.

    The same shape `test_detached` uses: scripted per question rather than per
    call, because the launch wait asks several different things.
    """
    def runner(args, mode):
        joined = " ".join(args)
        if joined.startswith("compute instances describe"):
            return {"status": status}
        if joined.startswith("compute instances "):
            return ""
        if mode == "output":
            if "NetTCPConnection" in joined or "sport = :" in joined:
                return "PORT_FREE"
            if "pgrep -f" in joined or "Get-Process -Name python" in joined:
                return alive
            if "tail -n" in joined or "Get-Content -Path" in joined:
                return ""
            return "READY"
        return 0

    return Gcloud(runner=runner)


# --- `disconnect`: the machine is deliberately left on, and it is billing ----
#
# The one command whose purpose is leaving a box running. Both sentences below
# are what it says instead of stopping, and both were unheld.


def test_disconnect_says_the_comfyui_on_the_box_is_still_running_too(cli):
    """Stopping the tunnel stops nothing on the machine.

    `go` leaves ComfyUI running on the box, so closing the local forward leaves
    a generation running that nobody can see. This line is where you are told
    that, and where you are told how to look at it.
    """
    result = cli("disconnect", "comfy-win", cloud=Cloud(status="RUNNING"))

    assert result.exit_code == 0
    assert ("any ComfyUI on it is still running too: comfy-qat logs comfy-win"
            in result.output), result.output


def test_disconnect_says_how_to_stop_paying_when_the_work_is_finished(cli):
    """The bill, on the command that leaves it running.

    Not the closing line `disconnect` prints itself — that one reads
    `comfy-qat down comfy-win   # when the work is finished`, and lives in
    host.py. This is lifecycle's own, the story on stderr next to the sentence
    that says the box is still billing, and it went missing for six commands
    before it was written in one place.
    """
    result = cli("disconnect", "comfy-win", cloud=Cloud(status="RUNNING"))

    assert result.exit_code == 0
    assert ("when the work is finished: comfy-qat down comfy-win"
            in result.output), result.output


# --- `down`: what it FOUND, which is the question the command exists for -----
#
# Stopping an already-stopped box succeeds trivially, so "stopped" said nothing
# about whether five GPU boxes had been billing all night. These three lines are
# the difference, and each has to survive a rewording.


def test_down_on_a_box_that_was_already_stopped_says_it_was_already_stopped(cli):
    result = cli("down", "comfy-win", cloud=Cloud(status="TERMINATED"))

    assert result.exit_code == 0
    assert "comfy-win was already stopped" in result.output, result.output
    assert "stop_instance" not in result.cloud.calls, (
        "an already-stopped box is not stopped again"
    )


def test_down_on_a_box_on_its_way_up_says_what_state_it_caught_it_in(cli):
    """PROVISIONING is billing, or about to be — caught, not idle.

    The state is the news. "stopped it" without it reads the same whether the
    box had been up all night or was thirty seconds old.
    """
    result = cli("down", "comfy-win", cloud=Cloud(status="PROVISIONING"))

    assert result.exit_code == 0
    assert "comfy-win was provisioning — stopped it" in result.output, result.output
    assert "stop_instance" in result.cloud.calls


def test_down_that_could_not_read_the_state_first_says_so_rather_than_claiming(cli):
    """The stop still happens — that is the safe direction — but nothing is
    claimed about what it caught. An unreadable box that says "was running" is
    a number in the `--all` summary that nobody can see is wrong."""
    result = cli("down", "comfy-win",
                 cloud=Cloud(status=GcloudError("credentials expired")))

    assert result.exit_code == 0
    assert ("comfy-win stopped, though its state could not be read first"
            in result.output), result.output
    assert "stop_instance" in result.cloud.calls, "it stops it anyway"


# --- the launch wait: a ComfyUI that dies looks exactly like a slow one ------


def test_a_comfyui_that_died_before_answering_is_named_as_that(tmp_path):
    """The box is asked every ALIVE_EVERY seconds so the answer is not three
    minutes late, and three minutes here is GPU time on a machine that is
    billing. Something already holds that the wait ENDS; nothing held the
    sentence that says why it ended."""
    clock = {"t": 0.0}
    lines, say = said()

    with pytest.raises(LifecycleError):
        start_detached(scripted(alive=GONE), LINUX, say, tunnel_dir=tmp_path,
                       probe_fn=lambda host: None,
                       sleep=lambda seconds: clock.__setitem__("t", clock["t"] + seconds),
                       now=lambda: clock["t"], timeout=600)

    told = "\n".join(lines)
    assert ("ComfyUI is no longer running on comfy-linux — it stopped before it "
            "ever answered") in told, told


# --- the two that only the docs walk was holding -----------------------------
#
# `test_docs` requires each of these to have a troubleshooting entry, and both
# do. That is a different claim from the one worth making: the entry says the
# sentence was written down, not that the command still prints it, and a walk
# over the source cannot tell the difference between a message that moved and a
# message that was deleted and re-added elsewhere. For a sentence about a
# machine that is billing, "it is documented" is not enough. Both keep their
# entries; these are what fail when the behaviour goes.


def _tunnel_finds_nothing_listening(tmp_path):
    """Drive `up` onto a running box that has no ComfyUI on it yet.

    gcloud tests the far port before it will serve and refuses when nothing is
    listening, so a tunnel cannot exist before ComfyUI is started. Returns what
    was said and what was raised.
    """
    from comfy_qa.tunnel import BACKEND_NOT_LISTENING, TunnelError

    def refuses(cmd, log, **kwargs):
        raise TunnelError("nothing is listening on port 8190 of the machine yet",
                          kind=BACKEND_NOT_LISTENING)

    lines, say = said()
    with pytest.raises(LifecycleError) as caught:
        bring_up(scripted(status="RUNNING"), WIN, say, tunnel_dir=tmp_path,
                 sleep=lambda _seconds: None, launcher=refuses,
                 probe_fn=lambda host: None)
    return lines, caught.value


def test_a_box_with_no_comfyui_yet_says_it_is_running_and_billing(tmp_path):
    """`up` stops here, and the box was started to get this far.

    Something already checks that this failure is classified COMFYUI_ABSENT so
    that `go` may continue past it. Nothing checked the sentence a person
    reads — which is the only place `up` says the machine is on and costing
    money.
    """
    _, failure = _tunnel_finds_nothing_listening(tmp_path)

    assert ("comfy-win is running and billing, but ComfyUI is not started on it "
            "yet") == str(failure), str(failure)
    assert "comfy-qat down comfy-win" in failure.fix, "and how to stop paying"


def test_a_tunnel_with_nothing_to_reach_says_it_is_starting_comfyui_first(tmp_path):
    """Not a failure — an order-of-operations fact, and the only thing standing
    between a refusal and a person concluding the tunnel is broken."""
    lines, _ = _tunnel_finds_nothing_listening(tmp_path)

    assert ("nothing is listening on the machine yet, so there is nothing to "
            "tunnel to — starting ComfyUI first") in "\n".join(lines), lines


def test_a_terminal_that_could_not_be_opened_at_all_says_nothing_was_started(
        monkeypatch):
    """The sibling of the osascript-refused case, and the one nothing drove.

    `--new-window` is how a GPU box gets started from a window you then stop
    watching. If the window never opens, the promise that carries the money is
    "Nothing was started" — there is no machine billing behind this failure.
    A test exists for osascript returning non-zero; this is the branch where it
    could not be run at all.
    """
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)

    def cannot(*args, **kwargs):
        raise OSError("Too many open files")

    monkeypatch.setattr(subprocess, "run", cannot)
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        in_a_new_window(["go", "comfy-win"], say)

    assert ("could not open a new Terminal window: Too many open files. "
            "Nothing was started.") == str(caught.value), str(caught.value)
    assert "go comfy-win" in caught.value.fix, "print what to paste instead"
