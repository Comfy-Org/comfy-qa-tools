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

[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-c"
gce_project  = "proj"
port         = 8192
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

    def list_reservations(self, project):
        """The third question `down --all` asks, and the reason it is here.

        A reservation bills with its box stopped and with no box at all, so the
        all-clear is not earned until the project's own reservations have been
        read and there are none. Nothing reserved by default: the tests above
        this line are about machines, and each of their sentences is the true
        one only on a project where that is so."""
        self.calls.append("list_reservations")
        return []

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

    def invoke(*args, cloud=None, declared=None):
        if declared is not None:
            path.write_text(declared, encoding="utf-8")
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

    This used to assert lifecycle's own prose — `when the work is finished:
    comfy-qat down comfy-win`, on stderr beside the sentence that says the box is
    still billing — as distinct from the closing line `disconnect` prints itself.
    Both were unconditional, and a live run showed the result: the tool's most
    safety-critical block ended with one instruction in two phrasings, which is
    how a block stops being read.

    lifecycle's prose is gone and `disconnect`'s own line is the survivor, for
    the reason `test_host_costs` records: it is on stdout, and making it
    conditional instead left that stream empty on this path.

    So what this test defends is unchanged and is the thing that actually
    matters — the command that deliberately leaves a machine billing says how to
    stop it — while no longer pinning WHICH of two lines carries it.
    """
    result = cli("disconnect", "comfy-win", cloud=Cloud(status="RUNNING"))

    assert result.exit_code == 0
    assert "still billing" in result.output, result.output
    assert "comfy-qat down comfy-win" in result.output, result.output


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


# --- the last four the census found, at 321 literals -------------------------
#
# A whole-package census of every money/state literal left five survivors after
# tonight's fixes. Four under-report. These pin them; the fifth is over-reporting
# and is argued below.


def test_a_ctrl_c_while_reading_logs_says_the_box_is_still_on(cli, monkeypatch):
    """`logs` follows by default, so Ctrl-C is the ORDINARY way to leave it —
    not an error path. Stopping the reading stops nothing on the machine, and
    the moment a user is most likely to believe otherwise is the moment their
    terminal goes quiet again."""
    from comfy_qa import lifecycle as lifecycle_module

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(lifecycle_module, "read_logs", interrupted)
    result = cli("logs", "comfy-win", cloud=Cloud(status="RUNNING"))

    out = result.output
    assert "still running" in out, out
    assert "and so is the machine" in out, out
    assert "stop paying" in out, "it must offer the way out of the bill"


def test_ssh_that_cannot_read_the_state_claims_nothing_about_it(cli):
    """Refusing because the answer is unknown is right. Saying the box is off
    would be the under-reporting direction on a command that is about to be
    told there is nothing to connect to."""
    class Readable(Cloud):
        """`ssh` checks readiness before it reads the state; the base fake
        refuses any call it was not told to expect, which is the right default
        and not what this test is about."""

        def require(self, *a, **k):
            return None

    result = cli("ssh", "comfy-linux", cloud=Readable(status=""))

    out = result.output
    assert "could not tell whether" in out, out
    assert "is not running" not in out, "an unread state is not a stopped box"
    assert "list --live" in out, "it must say how to find out"


def test_the_all_clear_is_not_given_while_a_box_on_the_project_is_running(cli):
    """"Nothing is now." is the sentence someone reads before closing the laptop.

    It used to be printed unconditionally by the branch that had stopped
    something, while the paragraph two lines below it named a machine on the
    project that is running and was NOT stopped. An all-clear exists to be the
    last thing read, so the contradiction is not a tie — the reader stops at the
    all-clear and the undeclared box bills all night.

    The guard was already on the sibling branch, where nothing had been stopped.
    This holds it on the branch that can also be wrong.
    """
    class HasAStranger(Cloud):
        def current_project(self):
            return "proj"

        def list_instances(self, project):
            return [{"name": "orphan-box",
                     "zone": "https://x/zones/us-central1-f",
                     "status": "RUNNING"}]

    result = cli("down", "--all", cloud=HasAStranger())

    out = result.output
    assert "was billing: comfy-win, comfy-linux. Stopped." in out, out
    assert "Nothing is now" not in out, (
        "an all-clear was given while orphan-box was running on the project"
    )
    assert "orphan-box" in out, "and the box it is not clear about is named"


def test_the_all_clear_is_still_given_when_there_is_nothing_left(cli):
    """The guard above must not swallow the case it exists to protect."""
    class Clean(Cloud):
        def current_project(self):
            return "proj"

        def list_instances(self, project):
            return []

    result = cli("down", "--all", cloud=Clean())

    assert ("was billing: comfy-win, comfy-linux. Stopped. Nothing is now."
            in result.output)


# THE OTHER THREE WAYS THE ALL-CLEAR CAN BE UNEARNED, and none of them had a
# test. The pair above covers one shape — something was stopped, an undeclared
# box is running — and the guard it pins is a single expression covering four
# facts: `all_clear = not unknown and strangers == []`. Three of those four were
# held by nothing, and each fails to a different one-word edit.
#
# Written as the general property rather than three unrelated cases, because the
# defect was never about strangers in particular. It was that the sentence which
# ends the command claimed more than the paragraphs under it knew.


def _worlds():
    """Four `down --all` runs, each with something the tool cannot call clear."""

    class Stranger(Cloud):
        """Something was stopped, and a box nobody declared is still running."""

        def current_project(self):
            return "proj"

        def list_instances(self, project):
            return [{"name": "orphan-box", "zone": "https://x/zones/us-central1-f",
                     "status": "RUNNING"}]

    class Unreadable(Cloud):
        """Something was stopped, and the project would not list.

        The one that would come back from a plausible tidy-up. `strangers` is
        `None` here and `None` is FALSY, so rewriting the guard as `not
        strangers` — which reads identically and is what anybody would shorten it
        to — turns an unread project back into an all-clear. `== []` is load-
        bearing and this is what says so.
        """

        def current_project(self):
            return "proj"

        def list_instances(self, project):
            raise GcloudError("credentials expired")

    class HalfRead(Cloud):
        """One box stopped, one whose state could not be read before stopping.

        `put_away` returns "unknown" for that one, and an unknown is not an idle:
        it may have been billing all night and nothing looked. This is the
        `not unknown` half of the guard.
        """

        def __init__(self):
            super().__init__()
            self._asked = 0

        def instance_status(self, instance, zone, project):
            self.calls.append("instance_status")
            self._asked += 1
            if self._asked == 1:
                return "RUNNING"
            raise GcloudError("no answer from the project")

        def current_project(self):
            return "proj"

        def list_instances(self, project):
            return []

    class NothingCaughtStranger(Cloud):
        """Nothing to stop here, and an undeclared box running there.

        The branch the original defect was NOT in, and the one that only stays
        right while `elif all_clear` keeps its condition. Shortened to a bare
        `else`, this prints "nothing was running, so nothing was billing" over a
        live orphan — the same lie from the opposite side.
        """

        def __init__(self):
            super().__init__(status="TERMINATED")

        def current_project(self):
            return "proj"

        def list_instances(self, project):
            return [{"name": "orphan-box", "zone": "https://x/zones/us-central1-f",
                     "status": "RUNNING"}]

    return {"an undeclared box is running": Stranger,
            "the project could not be listed": Unreadable,
            "one box's state was never read": HalfRead,
            "nothing was stopped and an orphan runs": NothingCaughtStranger}


@pytest.mark.parametrize("why", sorted(_worlds()))
def test_no_all_clear_is_given_while_anything_is_unaccounted_for(cli, why):
    """The sentence that ends `down --all` may not claim more than it knows.

    It is read last and it is read alone: someone closing the laptop at 2am
    reads the all-clear and stops, which is what an all-clear is for. So it has
    to be false in every world where a machine may still be billing — whether the
    machine is undeclared, unreadable, or simply never looked at.

    Both wordings are checked, because there are two of them for two branches and
    only one was ever wrong. Pinning the branch that broke would leave the other
    free to break the same way, which is exactly how this arrived: the guard
    already existed one line down and was never applied upward.
    """
    result = cli("down", "--all", cloud=_worlds()[why]())

    out = result.output
    assert "Nothing is now" not in out, (
        f"an all-clear was given while {why}:\n{out}"
    )
    assert "nothing was running, so nothing was billing" not in out, (
        f"the other all-clear was given while {why}:\n{out}"
    )
    # And it must still be a successful `down`. These are reports about what is
    # left, not failures of the stopping — turning them into a non-zero exit
    # would make the honest answer look like a broken command.
    assert result.exit_code == 0, out


@pytest.mark.parametrize("why", sorted(_worlds()))
def test_what_is_unaccounted_for_is_named_and_not_merely_withheld(cli, why):
    """Withholding the all-clear is half the job. The other half is saying why.

    A summary that simply goes quiet is read as a rough all-clear anyway — the
    reader assumes nothing was worth mentioning. Each of these four has to leave
    a sentence naming what it could not settle, and a command to settle it.
    """
    out = cli("down", "--all", cloud=_worlds()[why]()).output

    named = ("orphan-box" in out
             or "not an all-clear" in out
             or "could not be checked before stopping" in out)
    assert named, f"nothing in the summary says what is unresolved:\n{out}"
    assert "comfy-qat" in out or "gcloud compute instances stop" in out, (
        f"it withheld the all-clear and offered no way to settle it:\n{out}"
    )


# --- a reserved box: stopping it does not stop its bill -----------------------
#
# Everything above was written when a stopped box was a box that cost nothing
# but its disk. A RESERVED box breaks that: Google holds its capacity and bills
# for it every hour, running or stopped, until the box is deleted. So the line
# every command here ends on — `comfy-qat down <name>   # stop the box, stop
# paying` — is false about it, and `was billing. Stopped.` and `Nothing is now.`
# are false with it. Somebody told those three things stops the box, closes the
# laptop, and pays for a GPU all night.
#
# The same two rules. Every sentence below is typed out here, and every one is
# read off a command that was driven. Where a step of a command is replaced —
# the boot, the install, the launch — it is replaced by its answer, because the
# answer is the whole input to the sentence under test.

RESERVED = HOSTS.replace(
    'gce_project  = "proj"\nport         = 8190',
    'gce_project  = "proj"\ngce_reservation = "comfy-win-rsv"\nport         = 8190')

# What the reserved box's own record of its bill looks like to Google: one
# reservation, for one machine, made by this tool for it. A FIXTURE in the SDK
# schema's shape, not a recorded payload.
ITS_RESERVATION = {
    "name": "comfy-win-rsv", "zone": "https://x/projects/proj/zones/us-central1-a",
    "status": "READY", "description": "comfy-qat: held for comfy-win",
    "specificReservation": {"count": "1", "instanceProperties": {
        "machineType": "g2-standard-8"}},
}

# The two endings, typed. Not `reservation.stop_line(...)` and not
# `lifecycle.stop_paying(...)`: a test that asserts a value against the thing
# that produced it cannot fail.
DELETE_LINE = ("  comfy-qat delete comfy-win   # the only thing that stops a "
               "reserved box's bill — the box and its disk go too")
DOWN_LINE = "  comfy-qat down comfy-win   # stop the box, stop paying"


class Held(Cloud):
    """`Cloud`, on a project that has reservations — or cannot say."""

    def __init__(self, *, reservations=(ITS_RESERVATION,), **kwargs):
        super().__init__(**kwargs)
        self._reservations = reservations

    def list_instances(self, project):
        return []

    def list_reservations(self, project):
        self.calls.append("list_reservations")
        if isinstance(self._reservations, BaseException):
            raise self._reservations
        return list(self._reservations)


def test_down_on_a_reserved_box_says_it_is_still_billing_and_how_that_stops(cli):
    result = cli("down", "comfy-win", cloud=Held(), declared=RESERVED)

    assert result.exit_code == 0, result.output
    assert ("comfy-win was running. Stopped — but it is reserved, so it is still "
            "billing.") in result.stdout
    assert DELETE_LINE in result.stdout.splitlines()
    assert result.cloud.calls.count("stop_instance") == 1, "and it did stop the machine"
    # The three things it must not say, each of which the unreserved box hears.
    assert "stop paying" not in result.stdout
    assert "was billing. Stopped." not in result.stdout
    assert "comfy-qat down" not in result.stdout


def test_down_on_a_reserved_box_that_was_already_stopped_does_not_say_nothing_was_billing(cli):
    """The cheerful one. "was not running, so nothing was billing" is true of
    every other stopped box and false of this one."""
    result = cli("down", "comfy-win", cloud=Held(status="TERMINATED"),
                 declared=RESERVED)

    assert ("comfy-win was not running — but it is reserved, so it is still "
            "billing.") in result.stdout
    assert DELETE_LINE in result.stdout.splitlines()
    assert "nothing was billing" not in result.stdout


def test_down_on_a_reserved_box_nobody_could_read_says_it_bills_either_way(cli):
    """"May have been billing" is the honest hedge for an ordinary box. For a
    reserved one there is nothing to hedge: it bills whichever it was."""
    result = cli("down", "comfy-win", declared=RESERVED,
                 cloud=Held(status=GcloudError("timed out")))

    out = result.stdout
    assert ("comfy-win could not be checked before stopping. It is reserved, so "
            "it is billing either way.") in out
    assert "  comfy-qat list --live" in out.splitlines()
    assert DELETE_LINE in out.splitlines()


def test_the_same_three_runs_on_an_ordinary_box_still_say_what_they_always_did(cli):
    """The control. If the reserved wording had simply replaced the other, the
    three tests above would pass and every unreserved box would be told its
    bill had not stopped."""
    stopped = cli("down", "comfy-win", cloud=Held(reservations=())).stdout
    idle = cli("down", "comfy-win", cloud=Held(status="TERMINATED",
                                               reservations=())).stdout

    assert "comfy-win was billing. Stopped." in stopped
    assert "comfy-win was not running, so nothing was billing." in idle
    for out in (stopped, idle):
        assert "reserved" not in out and "comfy-qat delete" not in out


def test_down_all_never_gives_the_all_clear_while_a_declared_box_is_reserved(cli):
    """Both boxes were running and both were stopped — the run that used to end
    `Stopped. Nothing is now.` One of them is reserved, so something is."""
    result = cli("down", "--all", cloud=Held(), declared=RESERVED)

    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "Nothing is now" not in out
    assert "nothing was billing" not in out
    assert "was running: comfy-win, comfy-linux. Stopped." in out
    assert "still billing, stopped or not — it is reserved: comfy-win." in out
    assert DELETE_LINE in out.splitlines()
    assert "comfy-qat delete comfy-linux" not in out, "the other box is not reserved"


def test_down_all_names_a_reservation_on_the_project_that_no_host_declares(cli):
    """Nothing in the host list is reserved, every machine stopped, and the
    project is still holding a card: a reservation made in the console, or left
    by a create that stopped half-way. It is in no list of machines, so this
    summary is the only place somebody closing the laptop can learn of it."""
    stray = dict(ITS_RESERVATION, name="stray-rsv", description="made in the console")
    result = cli("down", "--all", cloud=Held(reservations=(stray,)))

    out = result.stdout
    assert "Nothing is now" not in out
    assert ("1 reservation on this project is billing and not in your host list: "
            "stray-rsv (us-central1-a).") in out
    assert "  comfy-qat list --live   # which of them has no box, and how to release it" \
        in out.splitlines()
    # Named, not released: it is not this command's to release.
    assert "delete_reservation" not in result.cloud.calls


def test_down_all_does_not_call_an_unread_project_clear_of_reservations(cli):
    """A read that failed is not a project with nothing reserved."""
    result = cli("down", "--all",
                 cloud=Held(reservations=GcloudError("the API is disabled")))

    out = result.stdout
    assert "Nothing is now" not in out
    assert ("the project's reservations could not be checked, and a reservation "
            "bills with its box stopped — so this is not an all-clear.") in out
    assert "was billing: comfy-win, comfy-linux. Stopped." in out


def test_down_all_still_gives_the_all_clear_when_the_project_has_nothing_reserved(cli):
    """The other direction. Read, and empty, and nothing declared reserved: the
    all-clear is earned, and withholding it would teach people to ignore it."""
    result = cli("down", "--all", cloud=Held(reservations=()))

    assert "was billing: comfy-win, comfy-linux. Stopped. Nothing is now." \
        in result.stdout
    assert "list_reservations" in result.cloud.calls, "and it did ask"
    assert "reserv" not in result.stdout


# Every command that ends by saying how to stop the bill, driven to that ending
# with `comfy-win` as the box. Each entry takes the fixture, pytest's
# monkeypatch and a host list, and returns what the command printed on stdout.


def _stamp():
    from comfy_qa.stamp import Stamp

    return Stamp(host="comfy-win", url="http://127.0.0.1:8190",
                 comfyui_version="0.33.0")


def _go(cli, monkeypatch, declared, *args, ready=None, launched=0, interrupt=False):
    """`go`, with the box brought up by its answer and the launch by its exit."""
    from comfy_qa import host as host_module
    from comfy_qa import lifecycle

    def launch(*a, **k):
        if interrupt:
            raise KeyboardInterrupt
        return launched

    monkeypatch.setattr(host_module, "_bring_up", lambda *a, **k: ready)
    for step in ("wait_for_ssh", "ensure_installed"):
        monkeypatch.setattr(lifecycle, step, lambda *a, **k: None)
    monkeypatch.setattr(lifecycle, "serve", launch)
    monkeypatch.setattr(lifecycle, "start_detached", launch)
    return cli("go", "comfy-win", "--no-browser", *args, declared=declared)


def _up(cli, monkeypatch, declared):
    from comfy_qa import lifecycle

    monkeypatch.setattr(lifecycle, "bring_up", lambda *a, **k: None)
    return cli("up", "comfy-win", declared=declared)


def _logs_interrupted(cli, monkeypatch, declared):
    from comfy_qa import lifecycle

    def interrupted(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(lifecycle, "read_logs", interrupted)
    return cli("logs", "comfy-win", declared=declared)


def _down_left_running(cli, monkeypatch, declared):
    from comfy_qa import lifecycle

    monkeypatch.setattr(lifecycle, "put_away", lambda *a, **k: "billing")
    return cli("down", "comfy-win", declared=declared)


class _Ready:
    """What `_bring_up` hands back for a box that is already serving."""

    def __init__(self):
        self.stamp = _stamp()


ENDINGS = {
    "up": _up,
    "go, already serving": lambda c, m, d: _go(c, m, d, ready=_Ready()),
    "go, launched": lambda c, m, d: _go(c, m, d),
    "go --follow, ComfyUI exited": lambda c, m, d: _go(c, m, d, "--follow"),
    "go, interrupted": lambda c, m, d: _go(c, m, d, interrupt=True),
    "go --follow, interrupted": lambda c, m, d: _go(c, m, d, "--follow",
                                                    interrupt=True),
    "logs, interrupted": _logs_interrupted,
    "down, left running": _down_left_running,
}


@pytest.mark.parametrize("ending", sorted(ENDINGS))
def test_no_ending_tells_a_reserved_box_that_down_stops_its_bill(cli, monkeypatch,
                                                                 ending):
    """Nine endings used to carry the same line as a string of their own, and
    each was a place a reserved box could be told the wrong thing. They are
    driven one at a time, because a helper that is right is no use to an ending
    that does not call it."""
    result = ENDINGS[ending](cli, monkeypatch, RESERVED)
    out = result.stdout

    assert DELETE_LINE in out.splitlines(), f"{ending}:\n{result.output}"
    assert "stop paying" not in out, f"{ending}:\n{out}"
    assert "comfy-qat down" not in out, f"{ending}:\n{out}"


@pytest.mark.parametrize("ending", sorted(ENDINGS))
def test_every_ending_still_tells_an_ordinary_box_how_to_stop_paying(cli, monkeypatch,
                                                                    ending):
    """The control, per ending, and the reason the test above means something:
    the same run on the same box, unreserved, ends on the old line. An ending
    that printed neither would pass a test that only looked for the wrong one."""
    result = ENDINGS[ending](cli, monkeypatch, HOSTS)
    out = result.stdout

    assert DOWN_LINE in out.splitlines(), f"{ending}:\n{result.output}"
    assert "comfy-qat delete" not in out and "reserved" not in out, f"{ending}:\n{out}"


def test_the_endings_driven_are_the_endings_there_are():
    """`ENDINGS` is a hand-typed table, and nothing would notice a tenth ending
    being added to host.py and not to it. So the count is held against the
    source: every call to `_stop_line` in host.py, outside the two commands
    driven on their own above (`down`'s verdicts and `down --all`) and the two
    that a reserved box is refused from (`move`), is an entry here."""
    import ast
    import inspect

    from comfy_qa import host as host_module

    calls: dict[str, int] = {}
    for node in ast.walk(ast.parse(inspect.getsource(host_module))):
        if isinstance(node, ast.FunctionDef):
            count = sum(isinstance(part, ast.Call)
                        and getattr(part.func, "id", "") == "_stop_line"
                        for part in ast.walk(node))
            if count:
                calls[node.name] = count

    assert calls == {
        "up_cmd": 1,                 # up
        "_serve": 4,                 # the five `go` endings; two share a line
        "logs_cmd": 1,               # logs, interrupted
        "down_cmd": 2,               # the reserved verdicts, and "left running"
        "disconnect_cmd": 1,         # driven below
        "_say_reserved": 1,          # `down --all`, driven above
        "_zone_with_capacity": 1,    # `move` — refused for a reserved box
        "move_cmd": 1,               # likewise
    }, calls


def test_disconnect_from_a_reserved_box_says_down_will_not_stop_the_bill(cli):
    """`disconnect` leaves the box running on purpose and offers `comfy-qat
    down` for when the work is finished. That line stays — it is how the
    machine is stopped — and the reserved one follows it, so the first cannot
    be read as the end of the bill."""
    result = cli("disconnect", "comfy-win", cloud=Held(), declared=RESERVED)

    lines = result.stdout.splitlines()
    assert "  comfy-qat down comfy-win   # when the work is finished" in lines
    assert DELETE_LINE in lines
    assert lines.index(DELETE_LINE) > lines.index(
        "  comfy-qat down comfy-win   # when the work is finished")
    assert result.cloud.calls.count("stop_instance") == 0


# --- `create --reserve`: the sentence, where it has to be said ----------------


BILL = ("comfy-linux is reserved. Google holds its capacity and bills for it "
        "every hour — running or stopped — until the box is deleted.")


@pytest.fixture
def create(tmp_path, monkeypatch):
    """`comfy-qat create` against the create tests' own fake project."""
    from comfy_qa import gcloud as gcloud_module
    from comfy_qa import zones as zones_module
    from comfy_qa.cli import app as root

    import test_create_cli as fixtures

    monkeypatch.setattr(zones_module, "_connect",
                        lambda region, timeout=None: fixtures.LATENCY.get(region, 500.0))

    def invoke(*args):
        path = tmp_path / "created.toml"
        path.write_text(fixtures.HOSTS, encoding="utf-8")
        cloud = fixtures.FakeGcloud()
        monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: cloud)
        result = CliRunner().invoke(
            root, ["create", "--os", "linux", "--gpu", "l4", *args,
                   "--config", str(path)])
        result.cloud = cloud        # type: ignore[attr-defined]
        return result

    return invoke


def _one_line(text: str) -> str:
    """`say` wraps a sentence at 96 columns; it is the same sentence unwrapped."""
    return " ".join(text.split())


def test_a_reserved_create_ends_on_the_bill_and_the_only_way_to_stop_it(create):
    """The moment the reservation starts billing is the moment to say so, and
    to say what ends it. Typed out, both of them."""
    result = create("--reserve", "--yes")

    assert result.exit_code == 0, result.output
    assert "create_reservation" in result.cloud.calls
    ending = result.stdout[result.stdout.index("comfy-linux is up in"):]
    assert BILL in _one_line(ending)
    assert ("  comfy-qat delete comfy-linux   # the only thing that stops a reserved "
            "box's bill — the box and its disk go too") in ending.splitlines()
    assert "stop paying" not in ending
    assert "comfy-qat down" not in ending


def test_a_reserved_dry_run_says_the_bill_before_anything_exists(create):
    """Under `--dry-run` too: the plan is what somebody reads to decide."""
    result = create("--reserve", "--dry-run")

    assert result.exit_code == 0, result.output
    assert BILL in _one_line(result.stdout)
    assert "--dry-run: nothing created" in result.stdout
    assert "create_reservation" not in result.cloud.calls


def test_an_ordinary_create_still_ends_on_stop_paying_and_says_nothing_of_reserving(create):
    """The control for the two above."""
    result = create("--yes")

    ending = result.stdout[result.stdout.index("comfy-linux is up in"):]
    assert "  comfy-qat down comfy-linux   # stop the machine, stop paying" \
        in ending.splitlines()
    assert "reserved" not in result.stdout and "comfy-qat delete" not in result.stdout
    assert "create_reservation" not in result.cloud.calls


# --- and `--help`, which is read before any of the above ----------------------


def _help(command: str) -> str:
    """A command's help as declared, on one line — not as rendered, which is
    wrapped to whatever width the terminal happens to be."""
    import typer

    from comfy_qa.cli import app as root

    return " ".join((typer.main.get_command(root).commands[command].help or "").split())


def test_the_help_for_down_does_not_promise_a_reserved_box_that_its_bill_stops():
    """`down --help` opens "so it stops costing money". Found by running it and
    reading it, after the suite was green: every sentence the command PRINTS had
    been corrected and the one describing it had not."""
    said = _help("down")

    assert said.startswith("Close the tunnel and stop the machine")
    assert ("Except a reserved box. Its reservation bills for the card every hour "
            "whether the box is running or stopped") in said
    assert "`comfy-qat delete` is the only thing that ends it" in said


def test_the_help_for_delete_says_it_releases_the_reservation():
    """And `delete --help` said stopping a box "ends the expensive part of the
    bill" — true of every box but the one whose bill only `delete` ends."""
    said = _help("delete")

    assert ("A reserved box is the exception to the first half of that: stopping "
            "it ends nothing") in said
    assert "Deleting it releases the reservation as well" in said


@pytest.mark.parametrize("command, words", [
    ("move", "A reserved box is not moved."),
    ("switch", "Stopping a reserved box frees neither its card nor its bill"),
    ("create", "`comfy-qat down` does not stop that bill, and only `comfy-qat delete` does"),
    ("list", "a reserved box bills every hour, running or stopped, until it is deleted"),
])
def test_each_command_a_reserved_box_changes_says_so_in_its_help(command, words):
    assert words in _help(command), _help(command)
