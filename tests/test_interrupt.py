"""Ctrl-C, and what it leaves behind.

The premise the whole feature rests on, and it was written down wrong before
this file existed, so it is stated here with the measurement rather than
repeated on trust.

**Ctrl-C does not cancel a mutating gcloud call, but not for the reason the
comments said.** `Gcloud.run` — the path every mutating call takes — is
`subprocess.run`, which on a KeyboardInterrupt kills the child and re-raises,
returning nothing. The double `process.wait()` is real but lives in
`relay_output`, which only `Gcloud.ssh` reaches, and `ssh` streams an install
log; it creates nothing. Measured against the real `Gcloud.run` with a shell
script standing in for gcloud, group-signalled exactly as a terminal Ctrl-C
signals: the interrupt reaches the caller at 0.41s, the child is killed, and no
exit code reaches the handler.

So the resource is at risk anyway — the API request has already gone and Compute
Engine builds the instance server-side whether or not the client is alive — and
the tool does NOT learn from the interrupt whether it succeeded. That is why the
report says "may", and why what makes it useful is the exact resource and the
exact command, not a claim of certainty it cannot support. `test_the_record`
below pins the shape; `test_gcloud_run` pins the premise itself, so a future
change to `Gcloud.run` that made the exit code available would turn this file red
rather than leaving the wording quietly over-cautious.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path

import pytest

from comfy_qa import inflight
from comfy_qa.gcloud import Gcloud

# --- sending yourself a signal without leaving one in flight ----------------
#
# THE DEFECT THIS REPLACES, because it is the reason for every awkward line
# below. The test underneath used to start a `sleep 2` subprocess and a thread
# that slept 0.4s and then signalled, and hoped the 0.4 landed inside the 2. It
# passes 5/5 alone. Under contention — twenty pytest processes on this machine,
# load average 37 — the 0.4s thread does not get scheduled until after the 2s
# child has exited, `Gcloud.run` returns normally, and `pytest.raises` sees
# nothing.
#
# The failure everyone looks at is `DID NOT RAISE`, and it is the harmless half.
# THE SIGINT IS NOT CANCELLED BY LOSING. It is delivered whenever the thread
# finally runs, which is somewhere inside a LATER test, and pytest treats a
# KeyboardInterrupt in a test body as a session abort. Measured with three
# innocent tests after it: four collected, one reported, three never ran, and
# the summary line said `1 failed in 2.95s` — typographically identical to a run
# in which one test failed and everything else passed.
#
# So the fix is not a longer sleep. A longer sleep makes the race rarer and the
# suite slower, and rarer is worse: the same corrupted session, arriving on a
# day nobody is looking for it. The fix is to stop racing.
#
#   1. The child ANNOUNCES itself — writes a file — before it blocks. The
#      signal is not sent until that file exists, so the child is provably
#      alive and the parent is provably inside `subprocess.run` at the moment
#      the signal is sent. There is no window left to lose.
#   2. The interrupter can STAND DOWN. If the announcement never comes, or the
#      call under test ends for its own reasons first, the thread returns
#      WITHOUT signalling. A lost race leaves nothing in flight, so it can cost
#      this test a failure and can never cost the session the rest of the run.
#   3. It signals at most once, under a lock the stand-down also takes, so
#      "check whether we were told to stop" and "signal" cannot be split.
#
# The polling loop below sleeps, and that is not the thing being removed. A
# poll interval is bounded by a PREDICATE — it ends when the file appears — and
# the same shape is already `wait_until` in tests/test_lifecycle_e2e.py. What
# was wrong before was a sleep used as a DEADLINE, standing in for a fact
# nobody checked.


class Interrupter:
    """One SIGINT to this process, after `announcement` appears, or none at all.

    Used as a context manager around the call being interrupted. On the way out
    it stands down and joins, so no thread outlives the test that started it and
    no signal outlives the call it was meant for.
    """

    def __init__(self, announcement: Path, timeout: float = 30.0) -> None:
        self.announcement = announcement
        self.timeout = timeout
        self.signalled = False
        self.late = False
        # True when SIGINT arrived here as SIG_IGN and had to be re-armed. Not a
        # failure — it is the normal state under any non-interactive launcher —
        # but it is the fact that explains a whole class of "only fails in CI".
        self.repaired_disposition = False
        self._restore_sigint = None
        self.why = "the interrupter never ran at all"
        self._lock = threading.Lock()
        self._stood_down = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._wait_then_signal, daemon=True)

    def _wait_then_signal(self) -> None:
        deadline = time.monotonic() + self.timeout
        while not self.announcement.exists():
            if self._stop.wait(0.005):
                self.why = (
                    "the call ended before the child ever announced itself, so "
                    "no signal was sent — nothing is in flight"
                )
                return
            if time.monotonic() >= deadline:
                self.why = (
                    f"{self.announcement} never appeared within {self.timeout}s, "
                    f"so the stand-in for gcloud never started and no signal was "
                    f"sent. This is a broken fixture, not a broken premise."
                )
                return
        with self._lock:
            if self._stood_down:
                self.why = (
                    "the child announced itself, but the call had already "
                    "returned by then, so the signal was withheld"
                )
                return
            self.signalled = True
            self.why = "SIGINT was sent while the child was provably running"
            os.kill(os.getpid(), signal.SIGINT)

    def __enter__(self) -> "Interrupter":
        # SIGINT HAS TO BE DELIVERABLE, AND IT IS NOT ALWAYS INHERITED THAT WAY.
        # A process started in the BACKGROUND from a non-interactive shell — `&`,
        # `nohup`, most CI runners, every agent harness — inherits SIGINT as
        # SIG_IGN, and CPython deliberately respects an inherited SIG_IGN rather
        # than installing `default_int_handler` over it. `os.kill` below is then
        # a NO-OP, and this whole test passes by doing nothing: the signal is
        # "sent", the call returns normally, and the assertion that a
        # KeyboardInterrupt came back is the only thing that notices.
        #
        # Measured on this file, one process, no concurrency and no load:
        #
        #     foreground     19 passed in 0.58s
        #     backgrounded   1 failed, 18 passed in 32.21s
        #
        # The 32s is the stand-in gcloud's `sleep 30` running to completion,
        # because nothing interrupted it. The only variable is how the process
        # was started — which is why this was reported for weeks as a test that
        # "only fails in the full run" and never reproduced for anyone who ran
        # it by hand in a terminal.
        #
        # The condition being modelled is a person pressing Ctrl-C at a
        # terminal, where the handler is always installed. So install it, and
        # put it back on the way out rather than leaving the session altered.
        self._restore_sigint = signal.getsignal(signal.SIGINT)
        if self._restore_sigint == signal.SIG_IGN:
            self.repaired_disposition = True
            signal.signal(signal.SIGINT, signal.default_int_handler)
        self._thread.start()
        return self

    def __exit__(self, *exception) -> bool:
        self._stop.set()
        try:
            with self._lock:
                self._stood_down = True
            self._thread.join(timeout=10)
        except KeyboardInterrupt:
            # Ours, arriving a beat after the call it was aimed at finished.
            # It stops HERE, inside the test that sent it, which is the whole
            # difference between a failing test and a truncated session.
            self.late = True
            self._thread.join(timeout=10)
        if self._restore_sigint is not None:
            signal.signal(signal.SIGINT, self._restore_sigint)
        assert not self._thread.is_alive(), (
            "the interrupter thread outlived the test that started it; it may "
            "still signal this process during a later one"
        )
        return False


# --- the premise ------------------------------------------------------------


def test_gcloud_run_gives_the_handler_no_exit_code_when_it_is_interrupted(tmp_path):
    """The measurement, as a test. No cloud: `gcloud` is a shell script here.

    The only way to observe what an interrupt does to a call in flight is to
    have a call in flight. A mock cannot: the behaviour under test belongs to
    `subprocess.run`, not to this codebase, and stubbing it out would test the
    stub.

    HOW THE TIMING IS PINNED, since a test that sends itself a signal is the one
    shape that can take the whole session down with it. The stand-in writes
    `announced` and only then blocks, and `Interrupter` will not signal until it
    sees that file. So by the time SIGINT is sent the child is running and this
    thread is inside `subprocess.run` waiting for it — both facts checked, not
    assumed — and the interrupt lands where it is meant to however starved the
    machine is.

    The 30s in the stand-in is NOT a deadline anybody has to beat; nothing races
    it. It is the ceiling on how long a broken fixture can hang, and it is never
    reached: in the healthy case this test costs the handshake, a few
    milliseconds, which is faster than the 0.4s it replaced.

    `pytest.raises` is deliberately not used. Its failure is `DID NOT RAISE`,
    which does not distinguish "the signal was sent and the call swallowed it"
    — a real finding, and the thing this test exists for — from "the stand-in
    never started". `Interrupter.why` says which, and the assertion prints it.
    """
    marker = tmp_path / "created"
    announced = tmp_path / "announced"
    fake = tmp_path / "gcloud"
    fake.write_text(
        f"#!/bin/sh\n: > {announced}\nsleep 30\n: > {marker}\necho '{{}}'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    gc = Gcloud()
    gc.require = lambda: str(fake)  # type: ignore[method-assign]

    returned = "not reached"
    interrupted = False
    with Interrupter(announced) as interrupter:
        try:
            returned = gc.run(["compute", "instances", "create", "x"],
                              parse_json=False)
        except KeyboardInterrupt:
            interrupted = True

    assert interrupted, (
        f"`Gcloud.run` did not hand the caller a KeyboardInterrupt. "
        f"{interrupter.why}."
        + (" The signal arrived after the call had already returned, which "
           "means `subprocess.run` stopped propagating it — re-read this "
           "file's header, because the premise the whole feature rests on has "
           "changed." if interrupter.late else "")
    )
    assert interrupter.signalled, interrupter.why
    assert returned == "not reached", (
        "if a value ever comes back from an interrupted call, the report can stop "
        "saying 'may' — and this test is where that gets noticed"
    )
    assert not marker.exists(), (
        "the child ran to completion, so the interrupt did not reach it"
    )


def test_a_killed_client_is_not_a_cancelled_request(tmp_path):
    """Why the report exists at all, said as plainly as a local test can say it.

    Locally the child dies with the client, and that is exactly the misleading
    part: nothing here can kill an instance Google has already been told to
    build. The child standing in for gcloud is the CLIENT, and killing it says
    nothing about the resource. Pinned so nobody reads the local behaviour as
    evidence that an interrupted create leaves nothing behind.
    """
    marker = tmp_path / "created"
    child = subprocess.Popen(["sh", "-c", f"sleep 2; : > {marker}"])
    try:
        child.kill()
        child.wait(timeout=5)
    finally:
        child.poll()

    assert not marker.exists(), "the local stand-in dies; the cloud resource does not"


# --- the record -------------------------------------------------------------


def test_a_call_that_returns_leaves_nothing_registered():
    with inflight.may_leave("the instance x in y", undo=["stop it"]):
        pass
    assert inflight.pending() == []


def test_an_ordinary_failure_leaves_nothing_registered():
    """A command that can name its own failure owns the message.

    The leftovers block was printed twice once, by two frames asking the same
    question of the same event, and this is the rule that stops it happening
    again: the record is about interrupts, and an `Exception` is somebody else's.
    """
    with pytest.raises(RuntimeError):
        with inflight.may_leave("the instance x in y"):
            raise RuntimeError("the create was refused")
    assert inflight.pending() == []


def test_an_interrupt_keeps_it_and_hands_click_something_it_cannot_swallow():
    with pytest.raises(inflight.Interrupted):
        with inflight.may_leave("the instance x in y", undo=["stop it"]):
            raise KeyboardInterrupt
    # Reported and cleared by `may_leave` itself now, so what survives the raise
    # is the MESSAGE, not the record. That is the point of moving it: the report
    # happens on the only frame every entry point goes through.
    assert inflight.pending() == []


def test_the_exit_code_is_130_and_this_test_writes_the_number():
    """The one place the number is a literal, and it has to stay one.

    Four tests assert the exit code and every one of them did
    `from comfy_qa.cli import INTERRUPTED` then `assert code == INTERRUPTED` —
    both sides of the `==` moving together, so `INTERRUPTED = 7` left the whole
    suite green. Measured. The literal 130 appeared in this repo's tests only in
    docstrings and comments: every place that stated it was prose, every place
    that checked it was a tautology.

    Comparing against an imported constant is right when the constant is an
    internal symbolic tag — `exc.kind == NO_QUOTA`, `classify(x) == REAUTH` —
    where the value is arbitrary and identity is the whole point. It is wrong
    when the constant encodes an EXTERNAL CONTRACT, and an exit code is one: 130
    is what a shell reports for a process killed by SIGINT, and nothing in this
    repository gets a say in it.

    `128 + SIGINT` is the derivation, written in `inflight` so the number is
    reasoned rather than remembered — which is exactly why the assertion here
    must not use it. `lifecycle.INTERRUPTED_EXIT` is already pinned this way at
    test_lifecycle.py:526-527, so this matches a house style rather than
    inventing one.
    """
    import signal

    assert inflight.INTERRUPTED == 130
    assert inflight.INTERRUPTED == 128 + signal.SIGINT, (
        "128 + SIGINT is the reason 130 is the right number; if the arithmetic "
        "and the literal ever disagree, the literal is the contract"
    )


def test_interrupted_is_not_an_exception():
    """The one property that makes the whole thing work.

    Click catches `KeyboardInterrupt` inside its own `main()` and turns it into
    `Exit(130)` before anything of ours runs (typer/core.py:203-204, which
    overrides Click's own main), so the real thing cannot be used. And every `except Exception` in this package means "this step
    failed" — an interrupt that any of them can swallow is an interrupt that goes
    unreported through whichever one it meets first.
    """
    import typer

    assert issubclass(inflight.Interrupted, typer.Exit), (
        "an Exit is handled by Typer's own _main in every embedding — which is "
        "what makes this survive `cli.register()`, where our own `main` is not "
        "on the stack at all"
    )

    # `typer.Exit`, not `click.exceptions.Exit`, and this assertion is the whole
    # reason to say so: typer VENDORS click, so the two are different classes
    # and Typer's handler matches only its own. Subclassing the click on PATH
    # compiles, imports, reads correctly, and propagates straight out of `app()`
    # — which is the failure `Interrupted` exists to prevent. Found by driving
    # the real entry point; no amount of reading would have shown it.
    #
    # WHY `click` IS A DECLARED TEST DEPENDENCY, in `pyproject.toml`'s `test`
    # extra, for these two lines and nothing else. The package does not import
    # click and must not start; the SUITE needs it, because the question here is
    # "are these two classes the same object", and it cannot be asked without
    # the outer click to compare against.
    #
    # This line was red in CI and green for everybody here, for months. Typer
    # vendors click, so `pip install -e . pytest` in a clean environment gets NO
    # top-level click — while every machine on this project has one, pulled in
    # by comfy-cli. Verified in a throwaway 3.12 venv, which is the only way it
    # is visible at all.
    #
    # The tempting fix — compare against `typer._click.exceptions.Exit` and drop
    # the dependency — is not weaker, it is IMPOSSIBLE, and measuring it turned
    # up something worth knowing. typer 0.27.1 (installed here) has `typer.Exit`
    # as `typer._click.exceptions.Exit`. typer 0.27.2 (what an unpinned
    # `typer>=0.12.5` installs today) has moved it: `typer.Exit` is
    # `typer.exceptions.Exit`, a plain RuntimeError subclass, and
    # `typer._click.exceptions` HAS NO `Exit` AT ALL. So the attribute that fix
    # would compare against does not exist on the version CI resolves, and any
    # other typer-derived spelling reduces to `typer.Exit is typer.Exit`, which
    # cannot detect anything. A canary that cannot detect un-vendoring is worse
    # than a declared dependency, so the dependency is declared.
    #
    # (`comfy_qa/inflight.py`'s docstring still states the 0.27.1 identity as
    # current. It is stale on 0.27.2. The MECHANISM is unaffected — typer/core
    # still catches its own `Exit` and still raises `Exit(130)`, and this file's
    # end-to-end interrupt tests pass on both — but the sentence naming the
    # class is now wrong on the newer one.)
    try:
        from click.exceptions import Exit as ClickExit
    except ModuleNotFoundError as missing:
        # Deliberately NOT `importorskip`. A skip here is a canary that stops
        # singing in the one environment — clean CI, unpinned typer — where
        # un-vendoring would first show up.
        raise AssertionError(
            "click is missing, so the one assertion that would notice typer "
            "un-vendoring click cannot run. It is a declared test dependency: "
            "install with `pip install -e \".[test]\"`, not `pip install -e .`."
        ) from missing

    assert typer.Exit is not ClickExit, (
        "typer has stopped vendoring click — re-read this class, because the "
        "distinction it is careful about may have stopped existing"
    )
    assert not issubclass(inflight.Interrupted, ClickExit)
    assert inflight.Interrupted().exit_code == 130
    assert not issubclass(inflight.Interrupted, KeyboardInterrupt), (
        "it must not be catchable as the thing it stands for: `_serve` and "
        "`logs` catch KeyboardInterrupt deliberately and mean their own phase"
    )


def test_nesting_holds_the_outer_entry_and_drops_the_inner_one():
    """`move` is three resources deep and the record has to be right at each."""
    with pytest.raises(inflight.Interrupted):
        with inflight.may_leave("the snapshot s"):
            with inflight.may_leave("the disk d"):
                pass
            assert [item.what for item in inflight.pending()] == ["the snapshot s"]
            raise KeyboardInterrupt
    # Both were reported by the inner registration — it reads the whole record,
    # not just its own entry — and the record is then empty.
    assert inflight.pending() == []


def test_reporting_says_may_and_never_claims_it_knows(capsys):
    with pytest.raises(inflight.Interrupted):
        with inflight.may_leave("the instance x in y", undo=["gcloud ... stop x"]):
            raise KeyboardInterrupt

    output = capsys.readouterr().err
    assert "may exist and be billing" in output, output
    assert "the instance x in y" in output
    assert "gcloud ... stop x" in output
    assert inflight.pending() == [], "reporting twice is the defect it replaced"
    assert inflight.report() is False, "and a second call has nothing to say"


def test_a_run_that_left_nothing_reports_nothing(capsys):
    assert inflight.report() is False
    assert capsys.readouterr().err == ""


def test_a_bill_and_a_stopped_machine_are_not_reported_under_one_heading(capsys):
    """`switch` is the only command with both, and they need different words.

    Its ceiling path stops the machine you were on BEFORE it starts the one you
    asked for. An interrupt in between leaves you on neither — nothing extra is
    billing, and calling that a bill would replace one false claim with another.
    """
    with pytest.raises(inflight.Interrupted):
        with inflight.may_leave("comfy-win already stopped",
                            heading="and this had already happened when you stopped it:"):
            with inflight.may_leave("comfy-linux, started"):
                raise KeyboardInterrupt

    output = capsys.readouterr().err
    assert "this may exist and be billing:" in output, output
    assert output.index("comfy-linux, started") < output.index("comfy-win already stopped")
    assert "already happened when you stopped it" in output


# --- through the whole tool -------------------------------------------------


def test_an_interrupt_exits_130_and_never_prints_aborted(run_main, monkeypatch,
                                                         tmp_path):
    """130 is what a shell reports for SIGINT, and the code is half the message.

    Typer already exits 130 for an interrupt on its own, so the code alone
    proves nothing about this design — what it proves is that the REPORT
    survives the whole path, from the registration through Typer's handling to
    the shell. The number is asserted as a literal here and derived in
    `inflight`; see `test_the_exit_code_is_130_and_this_test_writes_the_number`
    for why both.
    """
    from comfy_qa import gcloud as gcloud_module

    path = tmp_path / "hosts.toml"
    path.write_text(
        '[hosts.comfy-win]\nkind = "gce"\nport = 8190\nos = "windows"\ngpu = "L4"\n'
        'gce_instance = "comfy-win"\ngce_zone = "us-central1-a"\ngce_project = "p"\n',
        encoding="utf-8")

    class Interrupted:
        def instance_status(self, instance, zone, project):
            return "TERMINATED"

        def start_instance(self, instance, zone, project):
            raise KeyboardInterrupt

    monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: Interrupted())
    code, output = run_main(["up", "comfy-win", "--config", str(path)])

    assert code == 130
    # NOT `assert "Aborted!" not in output`. That assertion cannot fail, for two
    # independent reasons, and it sat in the commit whose headline was this
    # test's name. Typer overrides Click's main and converts KeyboardInterrupt to
    # `Exit(130)` before Click's Abort path is reached (typer/core.py:203-204);
    # and when an Abort DOES happen — EOFError at a prompt, which `create` and
    # `move` can reach with stdin closed — typer's rich branch prints `Aborted.`
    # with a full stop, not `Aborted!`. So the string was unreachable twice over.
    # What matters is that the report is there, and that is asserted below.
    assert "comfy-qat down comfy-win" in output


def test_the_probe_start_inside_move_is_registered_like_any_other(
        run_main, monkeypatch, tmp_path):
    """`move <host>` with no `--to` starts the box, and nothing recorded it.

    `_zone_with_capacity` starts the instance on purpose — nothing answers "where
    is there an L4 free", so the only way to ask is to try and read the zone out
    of the refusal, which means a try that is NOT refused leaves a GPU box
    running. It was the one billable start in the package outside `may_leave`,
    and it sits on the ordinary path: `move <host>` with no `--to` is how the
    command is meant to be used.

    Measured with a real SIGINT before the fix: exit 130, and the output ended at
    "asking Google where there is capacity" with nothing after it — no report, no
    "may be billing", no stop command. The exit code was never the missing half.

    Eleven interrupt tests across six files, and none reached this call.
    """
    from comfy_qa import gcloud as gcloud_module

    path = tmp_path / "hosts.toml"
    path.write_text(
        '[hosts.comfy-win]\nkind = "gce"\nport = 8190\nos = "windows"\ngpu = "L4"\n'
        'gce_instance = "comfy-win"\ngce_zone = "us-central1-a"\ngce_project = "p"\n',
        encoding="utf-8")

    class Interrupted:
        def start_instance(self, instance, zone, project):
            raise KeyboardInterrupt

    monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: Interrupted())
    code, output = run_main(["move", "comfy-win", "--yes", "--config", str(path)])

    assert code == 130
    # The box, the zone it is actually in — the ORIGINAL one, because the move
    # has not happened — and the way to stop paying for it.
    assert "comfy-win (comfy-win in us-central1-a)" in output, output
    assert "comfy-qat down comfy-win" in output, output


def test_a_command_that_finishes_is_untouched_by_any_of_this(run_main, tmp_path):
    """The record must be invisible on every path but one."""
    path = tmp_path / "hosts.toml"
    path.write_text('[hosts.local]\nkind = "local"\nport = 8188\n', encoding="utf-8")

    code, output = run_main(["list", "--config", str(path)])

    assert code == 0, output
    assert inflight.pending() == []
    assert "interrupted" not in output.lower()


# --- the wording the docs walk cannot see ---------------------------------
#
# `test_docs` harvests message literals at the CALL SITE: `say.error("...")`.
# `inflight.report` composes its text from a list and passes a variable, so the
# walk collects ZERO messages from inflight.py — measured, not assumed. Every
# other user-visible failure in this tool is pinned to troubleshooting.md by that
# walk; without this, the one printed over a billing GPU box is the one that
# could be reworded or deleted in a green suite.
#
# So it is pinned here, by the same contract the walk would have applied. Not
# by listing `Interrupted` in ERROR_TYPES: membership there means exactly one
# thing — collect the constructor's first argument — and it is raised with none,
# which is a listing that looks like coverage and is not.


def _troubleshooting() -> str:
    import re
    from pathlib import Path

    docs = Path(__file__).resolve().parent.parent / "docs" / "troubleshooting.md"
    return re.sub(r"\s+", " ", docs.read_text(encoding="utf-8"))


@pytest.mark.parametrize("phrase", [
    inflight.HEADLINE,
    "this may exist and be billing:",
    "and this had already happened when you stopped it:",
    # `rdp`'s, and the only heading about something that is not a resource: an
    # interrupted password reset leaves nothing to look up, only a password
    # nobody has.
    "this may already have happened, and it does not undo:",
])
def test_every_line_the_report_prints_has_a_troubleshooting_entry(phrase):
    assert phrase in _troubleshooting(), (
        f"`inflight.report` can print {phrase!r}, which is not in "
        f"troubleshooting.md. Quote it there verbatim, with what it means and "
        f"what to do — the same contract every other error in this tool is held "
        f"to, applied by hand because the walk in test_docs cannot see a message "
        f"composed from a list."
    )


def test_the_two_headings_are_the_ones_the_report_actually_uses(capsys):
    """The parametrize above is a hand-typed list, which is the shape that goes
    stale — so it is checked against a real report rather than against the source.

    Both branches are driven, because a heading only prints when its own kind of
    leftover is registered, and pinning a string nothing emits is the failure
    mode this whole file exists to avoid.
    """
    with pytest.raises(inflight.Interrupted):
        with inflight.may_leave("a stopped machine",
                                heading="and this had already happened when you stopped it:"):
            with inflight.may_leave("a billing instance"):
                raise KeyboardInterrupt

    printed = capsys.readouterr().err

    for phrase in (inflight.HEADLINE,
                   "this may exist and be billing:",
                   "and this had already happened when you stopped it:"):
        assert phrase in printed, printed


def test_the_commands_are_in_the_same_order_as_the_headings(capsys):
    """The fix block led with a command that STARTS a machine.

    The headings were grouped innermost-first and the commands were a flat walk
    of the record — registration order, the exact reverse. On `switch`'s ceiling
    path, which is the normal path on a project whose GPUS_ALL_REGIONS is 1, that
    put `comfy-qat up comfy-linux` above `comfy-qat down comfy-win`.

    Three things wrong at once and none of them cosmetic. It inverts this tool's
    own rule that stopping the bill comes first — the rule `create`'s OSError
    branch follows and 49a2889's own message quotes. The first command offered is
    the one most likely to FAIL, because the box that may now be running is
    exactly what a start would need room past at a ceiling of 1. And each entry's
    prose separator was orphaned from the commands it introduces, because the
    flat walk interleaved two entries' lists.
    """
    with pytest.raises(inflight.Interrupted):
        with inflight.may_leave(
            "comfy-linux already stopped to make room",
            undo=["comfy-qat up comfy-linux"],
            heading="and this had already happened when you stopped it:",
        ):
            with inflight.may_leave(
                "comfy-win, started",
                undo=["comfy-qat down comfy-win",
                      "or check first, if you would rather look:",
                      "comfy-qat list --live"],
            ):
                raise KeyboardInterrupt

    report = capsys.readouterr().err

    # The heading order and the command order are the same order.
    assert report.index("comfy-win, started") < report.index("comfy-linux already stopped")
    assert report.index("comfy-qat down comfy-win") < report.index("comfy-qat up comfy-linux"), (
        f"the first command handed over starts a machine, above the one that "
        f"stops the bill:\n{report}"
    )

    # And the separator still introduces its own entry's commands rather than
    # sitting between two entries' lists.
    separator = report.index("or check first, if you would rather look:")
    assert report.index("comfy-qat down comfy-win") < separator < report.index("comfy-qat list --live")
