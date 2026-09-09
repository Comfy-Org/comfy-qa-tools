"""Tunnels: opened once, recognised when already there, closed on request.

A recorded pid is checked against the process that holds it, so these use real
processes rather than a number. `os.getpid()` used to stand in for a live tunnel,
which quietly asserted the opposite of what we want: that any live process at
that number counts as one.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from comfy_qa.config import Host
from comfy_qa.tunnel import (
    TunnelError,
    close_tunnel,
    command,
    log_file,
    open_tunnel,
    pid_file,
    record_file,
    status,
)

WIN = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022", gpu="L4",
           gce_instance="comfy-win", gce_zone="us-central1-a", gce_project="proj")


def test_the_tunnel_forwards_through_ssh_to_the_box_own_loopback():
    """The change that finally made a cloud box reachable.

    `start-iap-tunnel` forwards to a port on the instance's network interface,
    so ComfyUI had to bind 0.0.0.0 and the port had to be allowed through the
    VPC firewall and the box's own — three things to get right, and any one of
    them wrong looks exactly like "ComfyUI is not running". `ssh -L` resolves
    the far address on the box, so loopback works and no firewall rule exists
    to get wrong: port 22 is already open, which is how every other command
    here already reaches the machine.

    The near end is `127.0.0.1` and not `localhost` on purpose. `localhost`
    resolves to `::1` first on macOS, so ssh bound IPv6 only and every probe of
    `http://127.0.0.1:8190` was refused while the forward worked perfectly over
    IPv6. That cost an hour of looking at the wrong end.
    """
    args = command(WIN)
    assert args[:4] == ["gcloud", "compute", "ssh", "comfy-win"]
    assert "--tunnel-through-iap" in args, "still IAP underneath: no open port"
    assert "127.0.0.1:8190:127.0.0.1:8188" in args
    assert "--zone=us-central1-a" in args
    assert "-N" in args, "forward only; do not run a shell"
    assert not any("firewall" in a for a in args)


@pytest.fixture
def processes():
    started = []
    yield started
    for process in started:
        try:
            process.kill()
            process.wait(timeout=5)
        except OSError:
            pass


def _looks_like_a_tunnel(processes, host=WIN):
    """A real process carrying a real tunnel's command line."""
    def launch(cmd, log):
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)", *cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        processes.append(process)
        return process.pid
    return launch


def _something_else(processes):
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    processes.append(process)
    return process


def test_opening_records_the_process_so_it_can_be_found_again(tmp_path):
    state = open_tunnel(WIN, tmp_path, launcher=lambda cmd, log: 4242)
    assert state.pid == 4242
    assert pid_file("comfy-win", tmp_path).read_text() == "4242"


def test_a_live_tunnel_is_reused_not_stacked(tmp_path, processes):
    """A second tunnel on the same port means you cannot tell which one answered."""
    first = open_tunnel(WIN, tmp_path, launcher=_looks_like_a_tunnel(processes))
    launched = []
    again = open_tunnel(WIN, tmp_path, launcher=lambda cmd, log: launched.append(1) or 1)
    assert launched == [], "must not launch a second tunnel"
    assert again.pid == first.pid


def test_a_pid_that_belongs_to_something_else_is_not_a_tunnel(tmp_path, processes):
    """Process ids are reused. A pid file that outlived its tunnel — a reboot, a
    crash — ends up naming whatever got the number next, and reporting that as a
    tunnel means reporting a route to a machine that nothing is connected to."""
    other = _something_else(processes)
    pid_file("comfy-win", tmp_path).parent.mkdir(parents=True, exist_ok=True)
    pid_file("comfy-win", tmp_path).write_text(str(other.pid))

    state = status("comfy-win", tmp_path)
    assert state.running is False
    assert state.stale is True

    launched = []
    open_tunnel(WIN, tmp_path, launcher=lambda cmd, log: launched.append(1) or 1)
    assert launched == [1], "a stale record must not stop a real tunnel being opened"


def test_closing_never_signals_a_process_that_is_not_our_tunnel(tmp_path, processes):
    """`down` sending SIGTERM to whatever inherited the number is the worst
    version of this: the tool kills something it has never heard of."""
    other = _something_else(processes)
    pid_file("comfy-win", tmp_path).parent.mkdir(parents=True, exist_ok=True)
    pid_file("comfy-win", tmp_path).write_text(str(other.pid))

    signalled = []
    assert close_tunnel("comfy-win", tmp_path, killer=lambda pid, sig: signalled.append(pid)) is False
    assert signalled == [], "it must not signal a process that is not the tunnel"
    assert not pid_file("comfy-win", tmp_path).exists()
    time.sleep(0.1)
    assert other.poll() is None


def test_when_the_process_cannot_be_read_the_pid_is_still_believed(tmp_path, processes):
    """On a machine where `ps` says nothing, the old behaviour is the safe one:
    an unrecognised tunnel would be stacked on top of a live one.

    The identity check has to degrade to "trust the record", not to "there is no
    tunnel" — the second is what stacks a second tunnel on a live port.
    """
    open_tunnel(WIN, tmp_path, launcher=_looks_like_a_tunnel(processes))

    state = status("comfy-win", tmp_path, identify=lambda pid: "")
    assert state.running is True


def test_a_dead_pid_is_not_mistaken_for_a_tunnel(tmp_path):
    pid_file("comfy-win", tmp_path).parent.mkdir(parents=True, exist_ok=True)
    pid_file("comfy-win", tmp_path).write_text("999999")
    assert status("comfy-win", tmp_path).running is False


def test_a_corrupt_pid_file_is_survivable(tmp_path):
    path = pid_file("comfy-win", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not a number")
    assert status("comfy-win", tmp_path).running is False


def test_no_pid_file_means_no_tunnel(tmp_path):
    assert status("comfy-win", tmp_path).running is False


def test_closing_signals_the_process_and_clears_the_file(tmp_path, processes):
    state = open_tunnel(WIN, tmp_path, launcher=_looks_like_a_tunnel(processes))
    signalled = []
    assert close_tunnel("comfy-win", tmp_path, killer=lambda pid, sig: signalled.append(pid))
    assert signalled == [state.pid]
    assert not pid_file("comfy-win", tmp_path).exists()


def test_the_tunnel_log_sits_beside_its_pid_file(tmp_path):
    """When a tunnel dies on startup, gcloud's reason is only ever in here, so
    every message about a dead tunnel has to be able to name it."""
    assert log_file("comfy-win", tmp_path).parent == pid_file("comfy-win", tmp_path).parent
    assert log_file("comfy-win", tmp_path).suffix == ".log"


def test_a_tunnel_that_dies_on_startup_never_gets_a_pid_file(tmp_path, real_spawn):
    """The failure this is written for. gcloud fails *immediately* far more often
    than it fails later — an expired credential is the common one — and because
    its output is captured it cannot prompt, so it exits rather than asking.
    Recording that pid turned a credential failure into "the box is up but
    ComfyUI is not answering", with the machine left running and billing.
    """

    log = tmp_path / "comfy-win.log"
    dying = [sys.executable, "-c",
             "import sys; sys.stderr.write('ERROR: There was a problem refreshing "
             "your current auth tokens: Reauthentication failed.\\n'); sys.exit(1)"]

    with pytest.raises(TunnelError) as caught:
        real_spawn(dying, log, grace=5)

    assert "closed as soon as it was opened" in str(caught.value)
    assert "Reauthentication failed" in str(caught.value), "gcloud's own words"
    assert "gcloud auth login" in caught.value.fix
    assert not pid_file("comfy-win", tmp_path).exists()


def test_a_tunnel_that_stays_up_is_handed_back(tmp_path, real_spawn):
    """The other half of the same check: a healthy tunnel must not be mistaken
    for a dead one just because it has not finished connecting yet."""
    import os
    import signal

    pid = real_spawn(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        tmp_path / "comfy-win.log", grace=0.2)
    try:
        assert pid > 0
    finally:
        os.kill(pid, signal.SIGTERM)


def test_each_open_starts_the_log_fresh_rather_than_appending(tmp_path, real_spawn):
    """The log was opened `"ab"`, and the append made the file LIE.

    `last_words` reads the whole log and returns its last six lines, and it is
    what fills in "gcloud said: ..." when a tunnel dies on startup. Appending
    meant a tunnel that died having written nothing of its own — or one line —
    had the PREVIOUS session's error quoted back as the explanation for this one.
    A confident wrong diagnosis, worse than the silence it was built to replace,
    and it needed no `disconnect` in between: two `open`s in a row are enough.

    It was also the only thing that made the file grow without bound, which is
    what makes keeping the log on `disconnect` affordable — the two halves of
    that decision are this test and the one in test_lifecycle_e2e.py.
    """
    log = tmp_path / "comfy-win.log"

    first = [sys.executable, "-c",
             "import sys; sys.stderr.write('ERROR: Reauthentication failed.\\n'); "
             "sys.exit(1)"]
    with pytest.raises(TunnelError) as caught:
        real_spawn(first, log, grace=5)
    assert "Reauthentication failed" in str(caught.value)

    # The second one dies saying nothing at all — the shape that used to inherit
    # the first one's words.
    silent = [sys.executable, "-c", "import sys; sys.exit(1)"]
    with pytest.raises(TunnelError) as second:
        real_spawn(silent, log, grace=5)

    assert "Reauthentication failed" not in str(second.value), (
        "this tunnel's failure was explained with the previous tunnel's error"
    )
    assert "Reauthentication failed" not in log.read_text(errors="replace")


def test_a_missing_gcloud_is_a_message_not_a_traceback(tmp_path, monkeypatch, real_spawn):
    """Popen against a binary that is not there raises FileNotFoundError, which
    reached the user as a traceback from `host open`."""
    import comfy_qa.tunnel as tunnel_module

    monkeypatch.setattr(tunnel_module.shutil, "which", lambda name: None)
    with pytest.raises(TunnelError) as caught:
        real_spawn(command(WIN), tmp_path / "x.log")
    assert "gcloud is not installed" in str(caught.value)
    assert caught.value.fix


def test_closing_nothing_is_not_an_error(tmp_path):
    assert close_tunnel("comfy-win", tmp_path, killer=lambda pid, sig: None) is False


def test_a_port_with_no_listener_is_named_as_that_and_not_as_a_broken_tunnel(tmp_path, real_spawn):
    """The ordering defect that made `host go` impossible on a fresh box.

    `gcloud compute start-iap-tunnel` tests the connection before it will serve
    and refuses when the far port has no listener:

        ERROR: (gcloud.compute.start-iap-tunnel) While checking if a connection
        can be made: Error while connecting [4003: 'failed to connect to
        backend']. (Failed to connect to port 8188)

    `go` opened the tunnel *before* starting ComfyUI, so on any box that was not
    already serving the tunnel refused — and the launch it was about to do was
    the very thing that would have fixed it. Reported as a broken tunnel, that is
    a dead end; reported as "nothing is listening yet", the caller can act.
    """
    from comfy_qa.tunnel import BACKEND_NOT_LISTENING

    log = tmp_path / "comfy-win.log"
    refusing = [sys.executable, "-c",
                "import sys; sys.stderr.write(\"ERROR: While checking if a "
                "connection can be made: Error while connecting [4003: 'failed "
                "to connect to backend']. (Failed to connect to port 8188)\\n\"); "
                "sys.exit(1)"]

    with pytest.raises(TunnelError) as caught:
        real_spawn(refusing, log, grace=5)

    assert caught.value.kind == BACKEND_NOT_LISTENING
    assert "nothing is listening" in str(caught.value)
    assert not pid_file("comfy-win", tmp_path).exists(), "no corpse recorded"


def test_the_grace_outlasts_gcloud_own_connection_test():
    """1.5s was shorter than the test gcloud runs before serving, so a tunnel
    that was about to refuse had its pid recorded as though it had opened."""
    from comfy_qa.tunnel import SPAWN_GRACE

    assert SPAWN_GRACE >= 5, "gcloud's own check takes seconds against Windows"


def test_an_interrupt_between_the_spawn_and_the_record_closes_the_tunnel(
        tmp_path, monkeypatch, processes):
    """The one leftover in this tool that cannot be handed over as a command.

    `open_tunnel` spawns ssh and then writes the record, and the OSError branch
    between them already says why the gap matters: "a tunnel nobody has a record
    of cannot be closed by `down`" — it holds the local port, points at a box
    that is billing, and nothing on screen says it is there. A Ctrl-C in the
    milliseconds between the two produces exactly that state and walked past the
    branch built to prevent it.

    Undone rather than reported, and that is why it needs no `inflight`
    registration: the pid is the only handle on the process and it is about to be
    lost, so there is nothing left to register by the time the exception carries
    on.
    """
    from comfy_qa import tunnel as tunnel_module

    process = _something_else(processes)
    real_write = tunnel_module._write

    def interrupt_the_first_write(path, text):
        if path.suffix == ".json":
            raise KeyboardInterrupt
        return real_write(path, text)

    monkeypatch.setattr(tunnel_module, "_write", interrupt_the_first_write)

    with pytest.raises(KeyboardInterrupt):
        open_tunnel(WIN, tmp_path, launcher=lambda cmd, log: process.pid)

    process.wait(timeout=5)
    assert process.poll() is not None, (
        "a live ssh with no record is unreachable by every command this tool has"
    )
    assert not pid_file("comfy-win", tmp_path).exists()


def test_the_interrupt_guard_covers_the_ps_call_that_dominates_the_window(
        tmp_path, processes):
    """The same state, reached through the slowest line the guard did not cover.

    The test above interrupts a write. Building the record calls `identify`,
    which shells out to `ps -p N -o lstart=,command=` with a ten-second timeout,
    and that line sat OUTSIDE the `try` — so the guard covered the fast half of
    the window and not the slow one. Measured here, median of seven, on two
    separate runs: ~3.6 ms and ~4.2 ms for the `ps` call against ~0.3 ms for the
    two writes. Thirteen to fourteen times the window that was protected — and
    the ratio is the durable part, not the figures, which are one machine's and
    move run to run. The ten-second `ps` timeout is the real ceiling.

    So the comment saying "the milliseconds between the spawn above and the two
    writes here" was wrong in both halves: the window is mostly the `ps` call,
    and it has no ceiling.

    Liveness by `poll()`, which reaps, and never `os.kill(pid, 0)`, which
    succeeds on a zombie — a child that was SIGTERMed and not yet waited on
    reads as alive, and the guard that works then looks broken. That instrument
    error reported both windows as leaking when only one did.
    """
    process = _something_else(processes)

    def interrupt_the_identity_lookup(pid):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        open_tunnel(WIN, tmp_path, launcher=lambda cmd, log: process.pid,
                    identify=interrupt_the_identity_lookup,
                    port_busy=lambda port: False)

    # Waited for, not asserted on immediately — the SIGTERM has to be delivered.
    # But the wait must not be what fails: `process.wait(timeout=5)` raises
    # `TimeoutExpired` on the leak, and a reader of that traceback learns that a
    # sleep did not finish rather than that a tunnel was left running. Swallow
    # the timeout so the assertion below is what speaks.
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    assert process.poll() is not None, (
        "a Ctrl-C during the `ps` lookup left a live ssh holding the port, "
        "pointing at a billing box, with no record naming it"
    )
    assert not pid_file("comfy-win", tmp_path).exists()
    assert not record_file("comfy-win", tmp_path).exists()

    # The guard's own reasoning, held to: the tunnel is undone here rather than
    # reported, because the pid was the only handle on it and it is gone. So
    # nothing may be left registered for `cli.main` to print either.
    from comfy_qa import inflight

    assert inflight.pending() == [], (
        "the process was killed, so there is no leftover to report"
    )


# --- the claim `disconnect --help` is not allowed to make again ---------------
#
# `test_a_dead_pid_is_not_mistaken_for_a_tunnel` above is the behaviour; this is
# the sentence about it. Typer prints a command's docstring verbatim as its
# `--help` body, and `disconnect_cmd`'s used to justify itself with "killing the
# ssh process by hand leaves the records behind, after which `list` reports a
# tunnel that is not there". It does not, and has not since the pid-identity work
# that the test above pins: a pid file naming a dead pid reads as stale and
# `list` says `not tunnelled`. The docstring outlived the defect it described.
#
# That was corrected in prose and NOTHING GRADED IT. Re-inserting the sentence as
# its own paragraph — a false claim shipped to every reader of `comfy-qat
# disconnect --help` — left all 8508 tests passing. A fix whose only evidence is
# that somebody edited the file is a fix that comes back.
#
# WHAT THIS CHECKS AND WHAT IT DOES NOT. The claim survives on the page once, on
# purpose, inside the sentence that refutes it — you cannot say what replaced a
# reason without saying the reason. So the rule is arithmetic rather than
# semantic: the phrase appears exactly once, and that occurrence sits inside the
# known refuting run. Re-asserting it anywhere else makes two. A light reword of
# the claim is not caught, which is the same limit `test_docs.py` records for its
# own message matching; what is caught is the thing that actually happened, which
# is the sentence coming back as it was.

THE_OLD_CLAIM = "reports a tunnel that is not there"

# The claim in the only setting that is not an assertion of it. Quoted in full so
# that trimming the refutation to the point where it stops refuting is a failure
# here rather than a change nobody sees.
REFUTED_AS = (
    'It said killing the ssh process by hand leaves the records behind, "after '
    'which `list` reports a tunnel that is not there". It does not:'
)


def _help_body_of(command) -> str:
    """A command's `--help` body, whitespace-flattened.

    Flattened because the docstring is wrapped for the source file and Typer
    re-wraps it for the terminal, so line breaks are not part of what anybody
    reads. Everything below is about the words.
    """
    import re

    return re.sub(r"\s+", " ", command.__doc__ or "").strip()


def test_disconnect_help_does_not_restate_the_reason_that_stopped_being_true():
    """The reason for the command, not a defect the tool no longer has."""
    from comfy_qa.host import disconnect_cmd

    body = _help_body_of(disconnect_cmd)
    said = body.count(THE_OLD_CLAIM)
    assert said == 1, (
        f"`disconnect --help` makes the claim {THE_OLD_CLAIM!r} {said} times. It "
        f"belongs there exactly once, inside the sentence that says it is no "
        f"longer true — a pid file naming a dead pid reads as stale and `list` "
        f"says `not tunnelled`, which `test_a_dead_pid_is_not_mistaken_for_a_"
        f"tunnel` in this file pins. Any other occurrence is the tool telling a "
        f"reader about a defect it does not have."
    )
    assert REFUTED_AS in body, (
        "the one permitted occurrence is no longer inside the sentence that "
        "refutes it, so `--help` now states it rather than retiring it."
    )


def test_that_check_would_have_caught_the_sentence_coming_back():
    """The guard on the guard: the mutation that survived, run through it here.

    A check with nothing left to find reads exactly like one that can no longer
    find anything. This is the paragraph that was re-inserted, and if it stops
    being counted the check above has stopped meaning anything.
    """
    from comfy_qa.host import disconnect_cmd

    came_back = (
        "Killing the ssh process by hand leaves the records behind, after which "
        "`list` reports a tunnel that is not there."
    )
    assert THE_OLD_CLAIM in came_back, "the mutation no longer contains the claim"

    body = _help_body_of(disconnect_cmd)
    assert (body + " " + came_back).count(THE_OLD_CLAIM) == 2, (
        "re-inserting the retired sentence no longer changes the count, so the "
        "check above would pass on the docstring it was written to reject"
    )
