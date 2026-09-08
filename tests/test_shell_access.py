"""Getting onto a box: `ssh` and `rdp`.

Both end in `os.execvp`, which replaces this process. Nothing after that line
runs, so nothing after it can be asserted on a real invocation — and the argv is
the whole of what the user gets. It is therefore built by `Gcloud` as a pure
value and checked here against a stand-in for `execvp`, which records the call
and raises rather than replacing the test runner.

The refusals matter as much as the argv. `ssh local` and `rdp <linux box>` are
the two things a tired person types by mistake, and both must exit 2 — nothing
was changed — with the other command named as the fix.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from comfy_qa import gcloud as gcloud_module
from comfy_qa import inflight, say
from comfy_qa.cli import app

HOSTS = """\
[hosts.local]
kind = "local"
port = 8188

[hosts.comfy-win]
kind         = "gce"
os           = "Windows Server 2022"
gpu          = "T4"
gce_instance = "win-instance"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8190

[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "linux-instance"
gce_zone     = "us-central1-b"
gce_project  = "proj"
port         = 8191
"""


class Replaced(Exception):
    """`execvp` never returns. This is how a test sees that it was reached."""


@pytest.fixture
def execvp(monkeypatch):
    """Record what would have replaced this process, instead of doing it."""
    calls: list[list[str]] = []

    def stand_in(file, argv):
        calls.append([file, *list(argv)])
        raise Replaced(file)

    monkeypatch.setattr("os.execvp", stand_in)
    return calls


@pytest.fixture
def hosts(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    return str(path)


@pytest.fixture
def state(monkeypatch):
    """What Google says the box is doing, without asking Google.

    `ssh` reads the instance's state before it hands the terminal over, so every
    test that expects to reach `execvp` has to answer that question — and none
    of them may answer it by making a real call. The default is RUNNING, which
    is the case the argv tests are about; `state.answer` sets up the others.
    """
    class Answer:
        value = "RUNNING"

    answer = Answer()
    monkeypatch.setattr(
        gcloud_module.Gcloud, "instance_status",
        lambda self, instance, zone, project: answer.value,
    )
    # And say the binary is there. `available()` falls through to
    # `shutil.which("gcloud")`, so without this every test here asserts on
    # whatever the machine happens to have installed — they passed on a laptop
    # and on the ubuntu runner, which ships the SDK, and failed on macos-latest,
    # which does not. The module already intends this: `available` returns
    # "<injected>" for an injected runner precisely so tests "exercise the real
    # check order without needing gcloud installed".
    monkeypatch.setattr(gcloud_module.Gcloud, "available",
                        lambda self: "/usr/bin/gcloud")
    return answer


def run(*args):
    return CliRunner().invoke(app, list(args))


# ---------------------------------------------------------------- refusals


def test_ssh_refuses_this_machine(hosts):
    result = run("ssh", "local", "--config", hosts)

    assert result.exit_code == 2, "nothing was changed, so it is a 2"
    assert "local is this machine — open a terminal" in result.output


def test_ssh_refuses_a_windows_box_and_names_rdp(hosts):
    result = run("ssh", "comfy-win", "--config", hosts)

    assert result.exit_code == 2
    assert "comfy-win runs Windows, which has no ssh here" in result.output
    assert "comfy-qat rdp comfy-win" in result.output


def test_rdp_refuses_a_linux_box_and_names_ssh(hosts):
    result = run("rdp", "comfy-linux", "--config", hosts)

    assert result.exit_code == 2
    assert "comfy-linux is not a Windows cloud box" in result.output
    assert "comfy-qat ssh comfy-linux" in result.output


def test_rdp_refuses_this_machine(hosts):
    result = run("rdp", "local", "--config", hosts)

    assert result.exit_code == 2
    assert "local is not a Windows cloud box" in result.output


@pytest.mark.parametrize("command", ["ssh", "rdp"])
def test_no_machine_at_all_is_refused(command, hosts):
    result = run(command, "--config", hosts)

    assert result.exit_code == 2
    assert "which machine?" in result.output


@pytest.mark.parametrize("command", ["ssh", "rdp"])
def test_an_unknown_name_is_refused(command, hosts):
    result = run(command, "nosuchbox", "--config", hosts)

    assert result.exit_code == 2
    assert "unknown host 'nosuchbox'" in result.output


# ------------------------------------------------------------ what it execs


def test_ssh_execs_the_iap_command_for_that_box(hosts, execvp, state):
    result = run("ssh", "comfy-linux", "--config", hosts)

    assert isinstance(result.exception, Replaced)
    assert execvp == [[
        "gcloud", "gcloud", "compute", "ssh", "linux-instance",
        "--zone=us-central1-b", "--project=proj", "--tunnel-through-iap",
    ]]


def test_ssh_finds_the_box_by_description(hosts, execvp, state):
    """A card reaches the same machine as the box's name does."""
    run("ssh", "l4", "--config", hosts)

    assert execvp[0][4] == "linux-instance"


# ------------------------------------- a box that cannot take a shell yet


@pytest.mark.parametrize("stopped", ["TERMINATED", "STOPPING", "SUSPENDED",
                                     "PROVISIONING", "STAGING"])
def test_ssh_on_a_box_that_is_not_running_is_one_sentence(hosts, execvp, state,
                                                          stopped):
    """The everyday case: you forgot to `up`.

    gcloud's own answer to this is a 36-line Python traceback and exit 255,
    ending in a suggested `--troubleshoot` that fails identically. `logs` on the
    same box in the same second says one sentence and names the fix. Nothing was
    reached, so it is a 2 — and `execvp` must not have run at all.
    """
    state.value = stopped

    result = run("ssh", "comfy-linux", "--config", hosts)

    assert result.exit_code == 2
    assert "comfy-linux is not running, so there is nothing to open a shell " \
           "on" in result.output
    assert "comfy-qat up comfy-linux" in result.output
    assert execvp == [], "nothing may be handed to execvp on a box that is off"


def test_ssh_does_not_guess_when_the_state_is_unreadable(hosts, execvp, state):
    """An empty state is a third answer, not a quiet yes.

    `instance_status` can succeed and say nothing. Treating that as RUNNING
    hands the terminal to gcloud and gets the traceback back; treating it as
    stopped tells someone to start a box that may already be billing.
    """
    state.value = ""

    result = run("ssh", "comfy-linux", "--config", hosts)

    assert result.exit_code == 2
    assert "could not tell whether comfy-linux is running" in result.output
    assert "comfy-qat list --live" in result.output
    assert execvp == []


def test_rdp_forwards_the_desktop_port_for_that_box(hosts, execvp, monkeypatch):
    monkeypatch.setattr(
        gcloud_module.Gcloud, "windows_password",
        lambda self, instance, zone, project: {
            "username": "ali", "password": "hunter2"},
    )

    result = run("rdp", "comfy-win", "--config", hosts)

    assert isinstance(result.exception, Replaced)
    # 33389 as a literal, not `host_module.RDP_PORT`. Built from the constant,
    # both sides of this move together and there is no oracle: changing
    # RDP_PORT to 9999 left the whole suite green, measured. It is the port a
    # person types into an RDP client, on a machine whose own notes already
    # record what port confusion has cost here — 8188 against 8189 against 8190
    # — so a silent change to it does not fail a test, it sends someone to the
    # wrong machine.
    #
    # The general rule, and this file has two of the four known exceptions:
    # a test needs an oracle independent of its subject. `subject(...) ==
    # <literal>` is fine, because the literal IS the oracle. `<anything> ==
    # SUBJECT_CONSTANT` asserts a thing against itself.
    assert execvp == [[
        "gcloud", "gcloud", "compute", "start-iap-tunnel", "win-instance",
        "3389", "--local-host-port=localhost:33389",
        "--zone=us-central1-a", "--project=proj",
    ]]


def test_rdp_hands_over_the_credentials_before_it_forwards(hosts, execvp,
                                                           monkeypatch):
    """The password is the one thing only a person can use. It goes first."""
    monkeypatch.setattr(
        gcloud_module.Gcloud, "windows_password",
        lambda self, instance, zone, project: {
            "username": "ali", "password": "hunter2"},
    )

    result = run("rdp", "windows", "--config", hosts)

    assert "user     ali" in result.output
    assert "password hunter2" in result.output
    # The literal again, and this is the line that matters most: this is the
    # address a person reads off the screen and types into Remote Desktop.
    assert "address  localhost:33389" in result.output


@pytest.mark.parametrize("answered,missing", [
    ({}, "username"),                                  # exit 0, empty stdout
    (None, "username"),                                # `run` parsed nothing
    ({"username": "ali"}, "password"),                 # half a pair
    ({"password": "hunter2"}, "username"),
    ({"username": "ali", "password": ""}, "password"),  # present and empty
    ({"user": "ali", "pass": "hunter2"}, "username"),   # renamed on their side
    ([{"username": "ali"}], "username"),               # not even a mapping
])
def test_rdp_refuses_rather_than_printing_a_credential_pair_it_never_got(
        answered, missing, hosts, execvp, monkeypatch):
    """A blank user over a blank password is laid out exactly like a real pair.

    `reset-windows-password` exiting 0 with nothing on stdout raises nothing, so
    the refusal path never fired; `.get(name, "")` then turned every one of these
    shapes into an empty string and printed it under "forwarding RDP". The user
    found out at a Windows login prompt they could not pass, with nothing in our
    output pointing back at us. Whatever we did not receive, we do not print.
    """
    monkeypatch.setattr(
        gcloud_module.Gcloud, "windows_password",
        lambda self, instance, zone, project: answered,
    )

    result = CliRunner().invoke(app, ["rdp", "comfy-win", "--config", hosts])

    assert result.exit_code == 1, result.output
    assert execvp == [], "the forward must not start on credentials we do not have"
    assert "forwarding RDP" not in result.output
    assert missing in result.output, "it names what came back missing"
    assert "gcloud compute reset-windows-password win-instance" in result.output

    # The danger is the *layout*, not the words: a labelled column reads as a
    # real pair whatever is in it. The words "user" and "password" are free to
    # appear in the refusal, which is prose. No line may be laid out as one.
    laid_out = [line for line in result.output.splitlines()
                if line.startswith(("user ", "password ", "address "))]
    assert laid_out == [], laid_out


def test_rdp_prints_the_pair_it_did_get(hosts, execvp, monkeypatch):
    """The guard must not cost the working case its output."""
    monkeypatch.setattr(
        gcloud_module.Gcloud, "windows_password",
        lambda self, instance, zone, project: {
            "username": "ali", "password": "hunter2"},
    )

    result = CliRunner().invoke(app, ["rdp", "comfy-win", "--config", hosts])

    assert isinstance(result.exception, Replaced)
    assert "user     ali" in result.output
    assert "password hunter2" in result.output


# ------------------------------------------------------- gcloud is not there


@pytest.mark.parametrize("command,name", [("ssh", "comfy-linux"),
                                          ("rdp", "comfy-win")])
def test_without_gcloud_it_says_so_rather_than_raising(command, name, hosts,
                                                       execvp, monkeypatch):
    """`execvp` on a binary that is not on PATH is a `FileNotFoundError`.

    Every other command in this tool answers a missing gcloud with a sentence
    and an install link. `ssh` handed the same person a traceback, because the
    argv is built without ever asking whether the thing at the front of it
    exists. The check `run()` already makes is the check these two owe.
    """
    monkeypatch.setattr(gcloud_module.Gcloud, "available", lambda self: None)

    result = CliRunner().invoke(app, [command, name, "--config", hosts])

    assert execvp == [], "nothing should be exec'd when there is nothing to exec"
    assert result.exit_code == 2, "nothing was started, so it is a 2"
    assert "gcloud is not installed or not on PATH" in result.output
    assert "https://cloud.google.com/sdk/docs/install" in result.output


# ------------------------------------------- ten minutes of nothing, pinned
#
# FOUND ON REAL HARDWARE, AND NOT FINDABLE BY A TEST AS THE SUITE THEN STOOD.
# `comfy-qat rdp comfy-win` was run against a running Windows Server 2022
# instance and printed NOT ONE LINE for ten minutes, at which point it was
# killed. Every other long call in this tool narrates — `up` says the box is
# starting and how long that may take, `go` says ComfyUI stays running afterwards,
# `down` says a stop is usually under a minute, `move` prints each step as it
# reaches it. `rdp` said nothing, on the one operation here that CHANGES
# something: the Windows password on the box.
#
# The suite could not see it because a faked `reset-windows-password` returns
# instantly, so the silence never exists in a test. What follows makes the silence
# exist — on a hand-wound clock, so a ten-minute wait costs the suite nothing and
# no assertion depends on how loaded the machine is. `tests/test_say.py` gives the
# reason in one line: a real timing assertion "fails for reasons that have nothing
# to do with the code", and the first person it fails on deletes it.

TEN_MINUTES = 600


class Clock:
    """A hand-wound clock. Same shape as `test_say.py`'s, and for its reasons."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Wound:
    """Every `say.slow` the command starts, driven by hand rather than by a thread.

    `background=False`, so nothing sleeps and no thread has to be joined; the test
    then plays the part the thread plays in a real run, which is to call `tick()`
    while the call underneath is blocked. `asked` keeps the kwargs the CALL SITE
    passed, before they were overridden — that is what lets a test below check
    that a real run gets the thread this one stands in for.
    """

    def __init__(self) -> None:
        self.clock = Clock()
        self.lines: list[str] = []
        self.steps: list = []
        self.asked: list[dict] = []


@pytest.fixture
def wound(monkeypatch):
    from comfy_qa import say as say_module

    record = Wound()
    real = say_module.slow

    def watched(text, **kwargs):
        record.asked.append(dict(kwargs))
        step = real(text, **{**kwargs, "emit": record.lines.append,
                             "clock": record.clock, "background": False})
        record.steps.append(step)
        return step

    monkeypatch.setattr(say_module, "slow", watched)
    return record


def _credentials(self, instance, zone, project):
    return {"username": "ali", "password": "hunter2"}


def test_rdp_says_what_it_is_about_to_do_before_it_does_it(hosts, execvp,
                                                          monkeypatch):
    """The destructive line has to arrive on the way IN.

    The reset is not free: the password that was working stops working, for
    whoever else signs in to that box. gcloud says so in its own words — including
    that an account with encrypted data can lose it — and `--quiet`, which this
    tool passes, is exactly the flag that suppresses that warning. So it is said
    here or it is not said at all.

    And it is said first, not last. Somebody who watches nothing happen and
    presses Ctrl-C only ever gets to read lines printed BEFORE the wait.
    """
    monkeypatch.setattr(gcloud_module.Gcloud, "windows_password", _credentials)

    result = run("rdp", "comfy-win", "--config", hosts)

    assert isinstance(result.exception, Replaced)
    output = result.output
    assert "resetting the Windows password on comfy-win" in output
    assert "stops working" in output, "the destructive half, not just the verb"
    assert "--quiet suppresses" in output, "gcloud's own warning, passed on"
    assert output.index("resetting the Windows password") < output.index("password hunter2"), (
        "the announcement has to precede the wait it announces, not follow it"
    )


def test_rdp_does_not_go_ten_minutes_without_saying_anything(hosts, execvp,
                                                             monkeypatch, wound):
    """PINS THE DEFECT ITSELF: how long can this command be silent?

    The reset is driven for a full ten minutes on the wound clock — the duration
    that was actually observed — and the times at which anything was said are
    recorded. Before the fix `spoke` is empty over the whole ten minutes and the
    first line of any kind is the password, at the end.
    """
    spoke: list[float] = []
    silent: list[str] = []

    def a_slow_reset(self, instance, zone, project):
        # Reported rather than raised: an exception in here is swallowed by
        # `CliRunner` and reaches the assertions below as a bare `assert False`,
        # which describes the harness and not the defect.
        if not wound.steps or not wound.lines:
            silent.append("nothing was said before the reset began")
            wound.clock.now = TEN_MINUTES
            return _credentials(self, instance, zone, project)
        step = wound.steps[-1]
        # The headline is out before the call starts; that is the zero.
        spoke.append(wound.clock.now)
        seen = len(wound.lines)
        while wound.clock.now < TEN_MINUTES:
            wound.clock.now += 5
            step.tick()
            if len(wound.lines) > seen:
                seen = len(wound.lines)
                spoke.append(wound.clock.now)
        return _credentials(self, instance, zone, project)

    monkeypatch.setattr(gcloud_module.Gcloud, "windows_password", a_slow_reset)

    result = run("rdp", "comfy-win", "--config", hosts)

    assert not silent, (
        f"{silent[0]}: the reset started no step of any kind, so the whole of it "
        f"— ten minutes of it, measured — is silent. That is the defect."
    )
    assert isinstance(result.exception, Replaced)
    assert spoke[0] == 0, "the first line is printed before the wait, not into it"
    assert len(spoke) > 3, (
        f"only {len(spoke)} lines over ten minutes — the ticker is not running "
        f"and this test is proving nothing"
    )
    assert spoke[1] <= say.FIRST_TICK_SECONDS, (
        f"the first sign of life came at {spoke[1]}s. That is the line that stops "
        f"somebody concluding it has hung, on an operation that has already "
        f"changed their password."
    )
    silences = [after - before for before, after in zip(spoke, spoke[1:])]
    assert max(silences) <= say.PIPED_TICK_SECONDS, (
        f"{max(silences)}s with nothing said. A slow step and a hung one look "
        f"exactly alike, and this one may already have reset a password."
    )
    # And every one of them carries the elapsed time, so the reader can tell a
    # step that is progressing from one that is repeating itself.
    assert f"still going, {say.elapsed(spoke[1])}" in "\n".join(wound.lines)


def test_the_reset_step_is_a_background_one_so_a_real_run_ticks_by_itself(
        hosts, execvp, monkeypatch, wound):
    """NON-VACUITY for the test above, which winds the clock by hand.

    That test calls `tick()` itself, standing in for the thread. If the call site
    ever passed `background=False` — as every `slow` in `lifecycle` does, because
    each of those sits in a polling loop that ticks it — the test would keep
    passing and the real command would go silent again, because there is no loop
    here: `windows_password` is one blocking subprocess. So the kwargs the call
    site actually passed are checked, before this fixture overrides them.
    """
    monkeypatch.setattr(gcloud_module.Gcloud, "windows_password", _credentials)

    run("rdp", "comfy-win", "--config", hosts)

    assert len(wound.asked) == 1, "one slow step: the reset"
    asked = wound.asked[0]
    assert asked.get("background", True) is True, (
        "nothing else ticks this step — without a thread the real command is "
        "silent for the whole reset"
    )
    assert "expect" in asked, "a wait is worth announcing with how long it may be"


def test_an_interrupted_reset_says_the_password_may_already_be_reset(
        hosts, execvp, monkeypatch):
    """Ctrl-C at minute nine: was my password reset or not?

    It may have been. The request reaches Google before the interrupt reaches
    gcloud, and unlike every other registration in this tool there is no resource
    to look up afterwards — the evidence is a password nobody has. So the report
    says which box, and hands over the reset command as the only way to get a
    usable password back.
    """
    def interrupted(self, instance, zone, project):
        raise KeyboardInterrupt

    monkeypatch.setattr(gcloud_module.Gcloud, "windows_password", interrupted)

    result = run("rdp", "comfy-win", "--config", hosts)

    assert result.exit_code == inflight.INTERRUPTED
    assert execvp == [], "nothing is forwarded on an interrupt"
    assert inflight.HEADLINE.rstrip(".") in result.output
    assert "this may already have happened, and it does not undo:" in result.output
    assert "the Windows password on win-instance (comfy-win), reset" in result.output
    assert "gcloud compute reset-windows-password win-instance" in result.output


def test_an_interrupted_reset_leaves_no_ticker_running(hosts, execvp, monkeypatch):
    """The shape `test_lifecycle` pins for the install, on the second threaded step.

    A ticker whose owner has gone is a daemon thread that can print "still going"
    over the interrupt's own report — the one message this command has to get onto
    the screen intact.
    """
    from comfy_qa import say as say_module

    started: list = []
    real = say_module.slow

    def watched(*args, **kwargs):
        step = real(*args, **kwargs)
        started.append(step)
        return step

    monkeypatch.setattr(say_module, "slow", watched)

    def interrupted(self, instance, zone, project):
        raise KeyboardInterrupt

    monkeypatch.setattr(gcloud_module.Gcloud, "windows_password", interrupted)

    run("rdp", "comfy-win", "--config", hosts)

    threaded = [step for step in started if step._background and step._every > 0]
    assert threaded, "no background ticker was started — the test proves nothing"
    for step in threaded:
        assert step._stop.is_set(), (
            "a ticker is still running after the interrupt; it will print "
            "'still going' over the report the tool is trying to make"
        )


# ------------------------------------------ a reset that ran out of clock
#
# `move`'s lesson, applied to the command that had further to fall: A CLIENT
# TIMEOUT DOES NOT STOP THE SERVER-SIDE OPERATION. gcloud writes the key into the
# instance's metadata FIRST and polls the serial port for the answer afterwards,
# so a timeout anywhere past the write leaves the password changed on the box and
# unreadable from here. Both directions are pinned, as they are for `move`: a
# timeout must say what may have happened, and a refusal must not invent it.


def _fails(kind):
    def raising(self, instance, zone, project):
        raise gcloud_module.GcloudError(
            f"gcloud timed out after 180s: compute reset-windows-password "
            f"{instance}", fix="check your network, then try again", kind=kind)
    return raising


def test_a_reset_that_ran_out_of_clock_says_the_password_may_have_changed(
        hosts, execvp, monkeypatch):
    monkeypatch.setattr(gcloud_module.Gcloud, "windows_password",
                        _fails(gcloud_module.TIMEOUT))

    result = run("rdp", "comfy-win", "--config", hosts)

    assert execvp == [], "there is no password, so there is nothing to forward"
    # 1, not 2. This group's rule is that 2 means nothing was changed, and that
    # is the one thing a timeout on this call cannot promise.
    assert result.exit_code == 1, result.output
    assert "the reset ran out of clock, which settles nothing" in result.output
    assert "may have been changed anyway" in result.output
    assert "gcloud compute reset-windows-password win-instance" in result.output


@pytest.mark.parametrize("kind", [gcloud_module.DENIED, gcloud_module.QUOTA,
                                  gcloud_module.NO_ACCOUNT, gcloud_module.NETWORK])
def test_a_refusal_does_not_invent_a_password_that_may_have_changed(
        kind, hosts, execvp, monkeypatch):
    """A refusal is an answer. Google looked and did nothing, or never heard.

    Naming a change that did not happen is how a tool stops being believed about
    the changes that did — the same judgement `relocate.UNRESOLVED` makes about a
    snapshot, which is why this reads that list rather than a second one.
    """
    monkeypatch.setattr(gcloud_module.Gcloud, "windows_password", _fails(kind))

    result = run("rdp", "comfy-win", "--config", hosts)

    assert execvp == []
    assert result.exit_code == 2, "nothing was changed, so it is a 2"
    assert "may have been changed anyway" not in result.output


def test_the_unresolved_kinds_are_read_from_relocate_and_not_restated():
    """NON-VACUITY for the pair above: one list, in one place.

    If `rdp` grew its own copy, the parametrised refusals would keep passing while
    the two definitions drifted — and the drift is only visible on real hardware,
    at the moment somebody's password has silently changed.
    """
    from comfy_qa.relocate import UNRESOLVED

    assert gcloud_module.TIMEOUT in UNRESOLVED
    assert gcloud_module.DENIED not in UNRESOLVED
    source = (Path(__file__).resolve().parent.parent / "comfy_qa" / "host.py"
              ).read_text(encoding="utf-8")
    assert "from .relocate import UNRESOLVED" in source


def test_the_password_reset_carries_a_timeout_of_its_own(monkeypatch):
    """`DEFAULT_TIMEOUT` was the wrong clock for this call, and nothing said so.

    A reset is not one call. gcloud writes a public key into the instance's
    metadata, waits for that update, and only then polls the serial port for the
    guest agent to answer — `WINDOWS_PASSWORD_TIMEOUT_SEC` is 30 in the SDK and
    `POLLING_SEC` is 2, and that clock starts after the metadata round-trips. The
    default sixty seconds covers the polling and not much else, which is the same
    shape of mistake `QUOTA_TIMEOUT` was written to fix: a real operation measured
    against a number chosen for a different one.

    Pinned because removing the argument leaves the whole suite green. The only
    symptom is on hardware, at the end of a minute, on a call that may already
    have changed a password.
    """
    seen: dict = {}

    def recorded(self, args, *, parse_json=True, timeout=None):
        seen["args"], seen["timeout"] = args, timeout
        return {"username": "ali", "password": "hunter2"}

    monkeypatch.setattr(gcloud_module.Gcloud, "run", recorded)

    gcloud_module.Gcloud().windows_password("win-instance", "us-central1-a", "proj")

    assert "reset-windows-password" in seen["args"]
    assert seen["timeout"] == gcloud_module.PASSWORD_TIMEOUT
    assert seen["timeout"] > gcloud_module.DEFAULT_TIMEOUT, (
        "the reset is being judged by the default clock again"
    )
