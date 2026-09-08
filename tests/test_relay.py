"""Output this tool did not write, held to the rule it wrote for itself.

`test_say.py` proves that nothing in the package prints an escape sequence or a
carriage return. It does that by walking every string constant in `comfy_qa`,
which is a complete proof about the half of the output we compose and no proof at
all about the half we relay — and the relayed half is most of what a `go` run
puts on the screen: an install log, a startup log, gcloud's own commentary.

The gap was not theoretical. A real run on a real L4 box put this in the middle
of an install, in yellow, and it was pasted onward with the escapes in it:

    \x1b[1;33mWARNING:\x1b[0m

    To increase the performance of the tunnel, consider installing NumPy. For
    instructions, please see https://cloud.google.com/iap/docs/using-tcp-...

So these tests do not mock the relay. They put a real executable on the other end
of `Gcloud.ssh`, have it write those exact bytes to the stream gcloud writes them
to, and read what a person would have seen. The one thing under test is the code
between a child process and a terminal, so replacing any of it would test
nothing.

The other half of the rule is the half that matters more: **nothing that could be
a failure is ever dropped.** A silenced error costs a testing session; a coloured
one costs a paste. So every case below that proves something disappears is
matched by one proving that something else did not.

The last two sections are the same boundary one step further in. There, a
subprocess's *output* reaches a person unexamined; here its *answer* does —
`reset-windows-password` exiting 0 with nothing, printed as a blank username and
a blank password laid out exactly like a real pair. That is the worse of the two,
because output that reads wrong gets noticed and a credential that reads right
does not.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager

import pytest

from comfy_qa import gcloud as gcloud_module
from comfy_qa import tunnel
from comfy_qa.config import Host
from comfy_qa.gcloud import (
    KNOWN_NOISE,
    NO_GCLOUD,
    Gcloud,
    GcloudError,
    Relay,
    is_noise,
    plain,
    readable,
    relay_output,
)

WIN = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022",
           gce_instance="win-instance", gce_zone="us-central1-a",
           gce_project="proj")

# What gcloud really printed, byte for byte, on 2026-09-03. The header carries a
# trailing space and nothing else; the advisory is two lines under a blank one.
ADVISORY = (
    "\033[1;33mWARNING:\033[0m \n"
    "\n"
    "To increase the performance of the tunnel, consider installing NumPy. For "
    "instructions,\n"
    "please see https://cloud.google.com/iap/docs/using-tcp-forwarding"
    "#increasing_the_tcp_upload_bandwidth\n"
)


def fake_gcloud(tmp_path, *, on_stdout: str = "", on_stderr: str = "", code: int = 0):
    """A real executable standing where gcloud stands.

    Not a stub of `Gcloud.ssh` — the actual binary it invokes. Everything under
    test happens after this process is started, so the only way to test it is to
    start one.

    The payloads are written to files and `cat`-ed rather than built into the
    script, so the escape sequences reach the pipe as the bytes they are and not
    as whatever this machine's `printf` makes of a backslash.
    """
    out = tmp_path / "on_stdout"
    err = tmp_path / "on_stderr"
    out.write_bytes(on_stdout.encode("utf-8"))
    err.write_bytes(on_stderr.encode("utf-8"))

    script = tmp_path / "gcloud"
    script.write_text(
        "#!/bin/sh\n"
        f"cat '{out}'\n"
        f"cat '{err}' >&2\n"
        f"exit {code}\n"
    )
    script.chmod(0o755)
    return script


def through_ssh(tmp_path, monkeypatch, capsys, **payload):
    """Run one command the whole way through `Gcloud.ssh` and read the screen."""
    script = fake_gcloud(tmp_path, **payload)
    monkeypatch.setattr(Gcloud, "available", lambda self: str(script))
    code = Gcloud().ssh("box", "zone", "project", "whatever")
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- the defect ---------------------------------------------------------------


def test_gclouds_own_colour_never_reaches_the_terminal(tmp_path, monkeypatch, capsys):
    """The bytes from the real run, through the real path, read back clean."""
    code, out, err = through_ssh(
        tmp_path, monkeypatch, capsys,
        on_stdout="using /opt/comfyui/venv/bin/python\nSTARTED\n",
        on_stderr=ADVISORY,
    )

    assert code == 0
    assert "\x1b" not in out + err, "an escape sequence survived the relay"
    assert "\r" not in out + err, "a carriage return survived the relay"


def test_the_numpy_advisory_is_dropped_whole(tmp_path, monkeypatch, capsys):
    """Header, blank line and both lines of prose, or it is worse than leaving it.

    Dropping only the prose would leave a bare `warning:` standing over nothing,
    which reads as something withheld rather than something handled.
    """
    _, _, err = through_ssh(
        tmp_path, monkeypatch, capsys,
        on_stdout="STARTED\n", on_stderr=ADVISORY,
    )

    assert "NumPy" not in err
    assert "increasing_the_tcp_upload_bandwidth" not in err
    assert "warning" not in err.lower(), f"a header was left standing: {err!r}"
    assert err.strip() == "", f"something of the advisory survived: {err!r}"


def test_what_the_box_said_is_untouched(tmp_path, monkeypatch, capsys):
    """The point of dropping gcloud's line is the log around it, so check it."""
    _, out, _ = through_ssh(
        tmp_path, monkeypatch, capsys,
        on_stdout="using /opt/comfyui/venv/bin/python\nSTARTED\n",
        on_stderr=ADVISORY,
    )

    assert out.splitlines() == ["using /opt/comfyui/venv/bin/python", "STARTED"]


def test_the_two_streams_stay_apart(tmp_path, monkeypatch, capsys):
    """`comfy-qat logs > run.log` collects the log, not the commentary."""
    _, out, err = through_ssh(
        tmp_path, monkeypatch, capsys,
        on_stdout="a line of ComfyUI log\n",
        on_stderr="gcloud is saying something\n",
    )

    assert out == "a line of ComfyUI log\n"
    assert err == "gcloud is saying something\n"


def test_the_exit_code_is_the_childs(tmp_path, monkeypatch, capsys):
    """Everything above the relay branches on this — NO_PYTHON, a failed install."""
    code, _, _ = through_ssh(
        tmp_path, monkeypatch, capsys, on_stdout="NO_PYTHON\n", code=3,
    )
    assert code == 3


# --- and nothing that mattered went with it -----------------------------------


def test_a_real_failure_is_never_dropped(tmp_path, monkeypatch, capsys):
    """The whole risk of this change, in one test.

    A tester who is told nothing is worse off than one who is told something in
    yellow. The colour goes; the sentence stays, every word of it.
    """
    shouting = (
        "\033[1;31mERROR:\033[0m Permission denied (publickey).\n"
        "\033[1;33mWARNING:\033[0m the box refused the key gcloud offered\n"
    )
    _, _, err = through_ssh(
        tmp_path, monkeypatch, capsys, on_stderr=shouting, code=255,
    )

    assert "ERROR: Permission denied (publickey)." in err
    assert "the box refused the key gcloud offered" in err
    assert "\x1b" not in err


def test_a_header_over_a_real_message_keeps_both(tmp_path, monkeypatch, capsys):
    """A bare header is held, not discarded — what follows decides it."""
    real = (
        "\033[1;33mWARNING:\033[0m \n"
        "\n"
        "Permissions 0644 for '/Users/x/.ssh/google_compute_engine' are too open.\n"
    )
    _, _, err = through_ssh(tmp_path, monkeypatch, capsys, on_stderr=real)

    lines = err.splitlines()
    assert lines[0].strip().lower() == "warning:", f"the header was lost: {err!r}"
    assert "are too open" in err


def test_a_header_at_the_very_end_is_still_printed(tmp_path, monkeypatch, capsys):
    """Held is not dropped. A stream that ends mid-block flushes what it held."""
    _, _, err = through_ssh(tmp_path, monkeypatch, capsys, on_stderr="warning:\n")
    assert err.strip().lower() == "warning:"


def test_only_the_documented_lines_are_dropped():
    """The drop list is short on purpose, and this is what keeps it short."""
    assert len(KNOWN_NOISE) == 2
    for line in (
        "ERROR: (gcloud.compute.ssh) Could not fetch resource:",
        "WARNING: The private SSH key file is too open.",
        "External IP address was not found; defaulting to IAP tunneling.",
        "To increase the disk size, run gcloud compute disks resize.",
        "please see https://cloud.google.com/compute/docs/troubleshooting",
    ):
        assert not is_noise(line), f"dropped a line that is not the advisory: {line}"


# --- the cleaning itself ------------------------------------------------------


@pytest.mark.parametrize("dirty", [
    "\033[1;33mcoloured\033[0m",
    "\033[2K\033[1Gerased and moved",
    "\033]0;a window title\007titled",
    "\033]0;unterminated title",
    "\033(B a two-character escape",
    "a bell \a and a null \x00",
])
def test_nothing_recognised_or_not_survives_as_an_escape(dirty):
    """The named patterns handle the common shapes; the sweep handles the rest.

    An escape sequence this tool has never seen still must not reach a paste, so
    the guarantee is closed by removing every remaining control character rather
    than by predicting them.
    """
    cleaned = plain(dirty)
    assert "\x1b" not in cleaned
    assert "\r" not in cleaned
    assert not any(character in cleaned for character in "\x00\a\b\x7f")


def test_a_progress_bar_arrives_as_its_final_state():
    """`\\r` is resolved, not deleted: what a terminal shows is the last redraw."""
    assert plain("10%\r50%\r100% done") == "100% done"


def test_a_windows_line_ending_does_not_eat_the_line():
    """`rsplit` on a trailing `\\r` would leave nothing at all."""
    assert plain("INSTALLED\r\n") == "INSTALLED"


def test_a_tab_is_left_alone():
    """Ordinary in a log, and it prints as itself."""
    assert plain("name\tvalue") == "name\tvalue"


def test_readable_keeps_every_line():
    """Used on everything that failed, where any line could be the one."""
    text = "\033[1;31mERROR:\033[0m one\n\ntwo\nthree\n"
    assert readable(text) == "ERROR: one\n\ntwo\nthree"


def test_the_relay_drops_the_advisory_and_holds_nothing_after_it():
    """The header is discarded with the block, not flushed onto the next line."""
    relay = Relay()
    printed = []
    for line in ADVISORY.splitlines() + ["using /opt/comfyui/venv/bin/python"]:
        printed += relay.line(line)
    printed += relay.rest()

    assert printed == ["using /opt/comfyui/venv/bin/python"]


# --- the same rule, on the tunnel's own log -----------------------------------


def test_the_advisory_cannot_crowd_the_reason_out_of_a_dead_tunnels_log(tmp_path):
    """`last_words` has six lines to explain a tunnel that died on startup.

    The tunnel is the one place the advisory is guaranteed to appear — it is
    advice about IAP forwarding, printed by every IAP forward. Four lines of it
    in a six-line tail is the sentence that names the cause pushed off the top of
    the message meant to carry it.
    """
    log = tmp_path / "comfy-linux.log"
    log.write_text(
        "opening the tunnel\n"
        + ADVISORY
        + "\033[1;31mERROR:\033[0m (gcloud.compute.ssh) Your credentials have "
        "expired.\n"
        "Please run: gcloud auth login\n"
    )

    said = tunnel.last_words(log)

    assert "Your credentials have expired." in said
    assert "gcloud auth login" in said
    assert "NumPy" not in said
    assert "\x1b" not in said


def test_a_tunnel_log_with_nothing_to_drop_is_unchanged(tmp_path):
    log = tmp_path / "box.log"
    log.write_text("one\ntwo\nthree\n")
    assert tunnel.last_words(log) == "one\n        two\n        three"


# --- what the child was handed ------------------------------------------------


class Closed:
    """A stream that has gone away — `comfy-qat logs | head` closes the pipe."""

    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


def test_a_reader_that_went_away_does_not_fail_the_command(tmp_path):
    """`| head` is not an error, and never was: the child still ran and exited."""
    script = fake_gcloud(tmp_path, on_stdout="a line\n" * 50)
    assert relay_output([str(script)], out=Closed(), err=Closed()) == 0


def test_the_child_keeps_this_processs_stdin(tmp_path, monkeypatch, capsys):
    """Only the two output streams are taken. stdin is left exactly where it was.

    gcloud asks about an unknown host key the first time it reaches a box, and a
    relay that piped stdin as well would answer nothing and sit there forever —
    trading a coloured warning for a hung command, which is the worse of the two.
    """
    answer = tmp_path / "answer"
    answer.write_text("yes\n")

    script = tmp_path / "gcloud"
    script.write_text("#!/bin/sh\nread line\necho \"stdin said: $line\"\n")
    script.chmod(0o755)
    monkeypatch.setattr(Gcloud, "available", lambda self: str(script))

    kept = os.dup(0)
    try:
        with open(answer, "rb") as reading:
            os.dup2(reading.fileno(), 0)
        Gcloud().ssh("box", "zone", "project", "whatever")
    finally:
        os.dup2(kept, 0)
        os.close(kept)

    assert "stdin said: yes" in capsys.readouterr().out


# --- what gcloud handed back, when it is not something we can use -------------
#
# The same boundary, one step further in. Above, a subprocess's *output* reaches
# a person; here its *answer* does. Both fail the same way — this tool passing on
# something it never looked at — and the second is the more expensive, because
# output that reads wrong is noticed and a credential that reads right is not.


def scripted(answer):
    """A `Gcloud` whose one call comes back with exactly this."""
    return Gcloud(runner=lambda args, mode: answer)


def test_a_password_reset_that_produced_nothing_is_a_failure():
    """`run` returns None on an empty stdout, and `or {}` made that a success.

    `rdp` then printed a blank username and a blank password laid out exactly
    like a real pair, said it was forwarding RDP, and execvp'd away. The tester
    found out at a Windows login prompt they could not get past, with nothing in
    our output pointing back at us.
    """
    with pytest.raises(GcloudError) as raised:
        scripted(None).windows_password("win-instance", "us-central1-a", "proj")

    assert "win-instance" in str(raised.value), "say which box"
    assert "no credentials" in str(raised.value)
    assert "nothing at all" in str(raised.value), "say what came back"
    assert "reset-windows-password" in (raised.value.fix or ""), "say how to look"


@pytest.mark.parametrize("answer", [
    {},                                        # exited 0, said nothing useful
    {"username": "ali"},                       # half a pair is not a pair
    {"password": "hunter2"},                   # nor is the other half
    {"username": "", "password": ""},          # present and blank is the trap
    {"ip_address": "10.0.0.2"},                # a table, wrong table
    "reset ok",                                # not a table at all
    ["ali", "hunter2"],
])
def test_only_a_complete_pair_is_a_password(answer):
    """Anything else is a failure. There is no half-usable credential."""
    with pytest.raises(GcloudError):
        scripted(answer).windows_password("win-instance", "z", "p")


def test_a_complete_pair_is_returned_as_it_came():
    credentials = {"username": "ali", "password": "hunter2",
                   "ip_address": "10.0.0.2"}
    assert scripted(credentials).windows_password("w", "z", "p") == credentials


def test_the_refusal_never_repeats_the_password():
    """An error is pasted into Slack. The one secret on the box does not go too.

    Half a pair still carries the half that matters, so the message names the
    keys that came back and never their values.
    """
    with pytest.raises(GcloudError) as raised:
        scripted({"password": "hunter2"}).windows_password("w", "z", "p")

    everything = f"{raised.value} {raised.value.fix} {raised.value.raw}"
    assert "hunter2" not in everything
    assert "password" in everything, "naming the key is the point"


# --- one missing binary, one sentence -----------------------------------------


def without_gcloud(monkeypatch):
    monkeypatch.setattr(Gcloud, "available", lambda self: None)
    return Gcloud()


@pytest.mark.parametrize("reach", [
    lambda gc: gc.run(["compute", "instances", "list"]),
    lambda gc: gc.run_interactive(["auth", "login"]),
    lambda gc: gc.preflight("proj"),
    lambda gc: gc.ssh("box", "z", "p", "echo ok"),
    lambda gc: gc.ssh_output("box", "z", "p", "echo ok"),
    lambda gc: gc.require(),
])
def test_every_way_in_says_the_same_thing_about_a_missing_gcloud(reach, monkeypatch):
    """`require()` unified two of the five. These are the other three.

    `ssh` and `ssh_output` each raised their own barer version with no `fix=`, so
    the same missing binary told two people two different things and only one of
    them where to get gcloud.
    """
    with pytest.raises(GcloudError) as raised:
        reach(without_gcloud(monkeypatch))

    assert str(raised.value) == "gcloud is not installed or not on PATH."
    assert raised.value.fix == "https://cloud.google.com/sdk/docs/install"
    assert raised.value.kind == NO_GCLOUD


# --- the prompt that piping makes invisible -----------------------------------
#
# These flags are here *because* of everything above. Reading a stream by line
# and showing it only when a newline arrives is right for a log and wrong for a
# question, and gcloud asks exactly one: on a machine that has never run
# `gcloud compute ssh`, the first one generates `~/.ssh/google_compute_engine`
# and prompts `Enter passphrase (empty for no passphrase):` with no newline after
# it. `ssh-keygen` then reads the answer from /dev/tty, so there is nothing to
# feed it either. Nothing in this tool creates that key and everything in it
# depends on the key existing, so this reproduces once per machine and never
# again — which is why it is pinned here rather than left to be met.


def argv_for(call):
    """The gcloud argv one method builds, without running anything."""
    seen = []
    gc = Gcloud(runner=lambda args, mode: seen.append(args) or "")
    call(gc)
    return seen[0]


@pytest.mark.parametrize("call,where", [
    (lambda gc: gc.ssh("box", "z", "p", "echo ok"), "ssh"),
    (lambda gc: gc.ssh_output("box", "z", "p", "echo ok"), "ssh_output"),
])
def test_a_command_run_on_a_box_never_waits_on_a_question(call, where):
    """Neither of these can show a prompt, so neither may be asked one.

    `ssh` streams through `_pump`, which reads by line — a prompt with no
    newline would never appear. `ssh_output` captures, so it could not appear
    even in principle: it would sit invisible for the full INSTANCE_TIMEOUT and
    come back as "gcloud timed out", naming the network for a question nobody
    was shown.
    """
    assert "--quiet" in argv_for(call), f"{where} can be asked to hold still"


def test_the_tunnel_is_never_asked_a_question_either():
    """The worst place of the three: detached, with its output going to a file.

    Nothing is on screen, ssh-keygen waits on /dev/tty, and `_spawn` watches for
    SPAWN_GRACE, sees a process still running and writes down its pid — a tunnel
    that forwards nothing, recorded as one that does. `open` can be the first
    command anyone runs, so it cannot lean on `go` having made the key first.
    """
    args = tunnel.command(WIN)

    assert "--quiet" in args
    assert args.index("--quiet") < args.index("--"), (
        "after the separator it is an argument to ssh, not a flag to gcloud"
    )


def test_the_interactive_shell_is_left_able_to_ask():
    """`ssh_argv` goes to execvp, so a prompt reaches a terminal and a person.

    The flag is about questions nobody can see. This one they can.
    """
    assert "--quiet" not in Gcloud().ssh_argv("box", "z", "p")


# --- output that stopped early says so ----------------------------------------


class Breaks:
    """A pipe that dies part way through, which is the only way to lose output."""

    def __init__(self, lines):
        self._lines = list(lines)

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        raise ValueError("I/O operation on closed file")


def collected():
    lines = []
    return lines, lines.append


def test_output_that_was_cut_short_leaves_a_mark():
    """A log that simply stops reads exactly like a command that finished.

    That is the whole reason this is not silent: the last line of a truncated
    install log looks like a result.
    """
    out, to_out = collected()
    err, to_err = collected()

    gcloud_module._pump(Breaks([b"installing torch\n", b"collecting nvidia-cudnn\n"]),
                        to_out, to_err)

    assert out == ["installing torch", "collecting nvidia-cudnn"], "keep what arrived"
    assert len(err) == 1 and err[0].startswith(gcloud_module.CUT_SHORT)


def test_the_mark_goes_to_stderr_even_for_the_stdout_pump():
    """Our own trouble is the story, not the answer. `logs > run.log` stays clean."""
    out, to_out = collected()
    err, to_err = collected()

    gcloud_module._pump(Breaks([b"a line of log\n"]), to_out, to_err)

    assert out == ["a line of log"]
    assert err and gcloud_module.CUT_SHORT in err[0]


def test_a_break_still_flushes_what_was_being_held():
    """Held is not dropped, and a failure is not a reason to start dropping."""
    out, to_out = collected()
    err, to_err = collected()

    gcloud_module._pump(Breaks([b"warning:\n"]), to_out, to_err)

    assert out == ["warning:"], "the header was held, then lost with the break"
    assert err and gcloud_module.CUT_SHORT in err[0]


# --- the one command that must NOT come through any of this -------------------
#
# Everything above exists because output nobody handles reaches a paste. The
# exception is the command that has to ask a person something. `gcloud auth
# login` opens a browser and prompts, and `_prove`'s reauth rescue exists for
# precisely the reason that piping hides a challenge: "gcloud will only ask when
# it owns stderr, which `run` does not give it". A prompt is not a log line, and
# `_pump` reads by line.
#
# So `run_interactive` hands the child this process's own stdio and reads nothing
# back. Route it through `relay_output` instead, or pass `capture_output=True`,
# and the prompt is never shown and the login sits there — the "hide the prompt
# and hang" failure the docstring names, which takes out every cloud path at
# once, because nothing else here can sign anybody in afterwards.
#
# Both spellings have been written by hand and neither made the suite red. What
# is checked below is not how the call is spelled — a check on the spelling is a
# check on the wrong thing, and the next way to pipe it will be spelled a third
# way. It is what the child ends up holding: the child reports the identity of
# its own descriptors 0, 1 and 2, and each must be the very file this process has
# open there. Any pipe, anybody's, is a different file.

_REPORTS_ITS_OWN_STDIO = '''
import json
import os


def identity(fd):
    """(device, inode) of whatever is on this descriptor. A pipe has its own."""
    handle = os.fstat(fd)
    return [handle.st_dev, handle.st_ino]


with open(REPORT, "w") as out:
    json.dump({"stdin": identity(0), "stdout": identity(1), "stderr": identity(2)}, out)
'''


def stdio_reporter(tmp_path):
    """A real executable standing where gcloud stands, which answers one question.

    It writes its answer to a file rather than to a stream, because the streams
    are the thing under test and a piped one would swallow the evidence.
    """
    report = tmp_path / "child-stdio.json"
    script = tmp_path / "gcloud"
    script.write_text(
        f"#!{sys.executable}\nREPORT = {str(report)!r}\n{_REPORTS_ITS_OWN_STDIO}"
    )
    script.chmod(0o755)
    return script, report


@contextmanager
def a_stdin_only_this_test_knows(tmp_path):
    """Put a file nobody else could name on descriptor 0, for the run.

    Without this the stdin case cannot fail. Under pytest fd 0 is ALREADY
    /dev/null, so a child handed `stdin=subprocess.DEVNULL` reports the very same
    device and inode and the check passes on a mutation that took the terminal
    away — measured, not assumed. Descriptors 1 and 2 need no such help: pytest's
    capture files are real files and are nobody's default.
    """
    stand_in = tmp_path / "on-stdin"
    stand_in.write_text("")
    saved = os.dup(0)
    with open(stand_in, "rb") as handle:
        os.dup2(handle.fileno(), 0)
    try:
        yield
    finally:
        os.dup2(saved, 0)
        os.close(saved)


def held_here(fd: int) -> list[int]:
    handle = os.fstat(fd)
    return [handle.st_dev, handle.st_ino]


def interactively(tmp_path, monkeypatch):
    """Run one `run_interactive` against that executable and read back the answer."""
    script, report = stdio_reporter(tmp_path)
    monkeypatch.setattr(Gcloud, "available", lambda self: str(script))

    with a_stdin_only_this_test_knows(tmp_path):
        code = Gcloud().run_interactive(["auth", "login"])
        mine = {name: held_here(fd)
                for name, fd in (("stdin", 0), ("stdout", 1), ("stderr", 2))}

    assert report.exists(), "the executable under test never ran"
    return code, json.loads(report.read_text()), mine


@pytest.mark.parametrize("name", ["stdin", "stdout", "stderr"])
def test_an_interactive_gcloud_is_given_this_terminal_and_not_a_pipe(
    name, tmp_path, monkeypatch,
):
    """One descriptor per case, so the failure names the one that was taken away.

    Under pytest these three are not a terminal, which changes nothing:
    inherited means the child holds the same file, piped means it does not, and
    whose file it is does not enter into it.
    """
    _, child, mine = interactively(tmp_path, monkeypatch)

    assert child[name] == mine[name], (
        f"gcloud was handed a {name} that is not this process's own, so it was "
        f"piped. `gcloud auth login` prompts and `_prove` reauths on the same "
        f"call; a piped prompt is never shown, the command hangs on an answer "
        f"nobody was asked for, and every path here that reaches the cloud goes "
        f"with it."
    )


def test_the_interactive_call_reads_nothing_back_but_the_exit_code(tmp_path, monkeypatch):
    """The exit code is the whole answer.

    A caller wanting output would need it captured, and capturing it is the
    defect. `setup` branches on `!= 0` and `_rescue` re-raises the original
    failure on one; neither looks at a word the child said.
    """
    code, _, _ = interactively(tmp_path, monkeypatch)
    assert code == 0
