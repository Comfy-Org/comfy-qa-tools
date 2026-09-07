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

import pytest

from comfy_qa import inflight
from comfy_qa.gcloud import Gcloud

# --- the premise ------------------------------------------------------------


def test_gcloud_run_gives_the_handler_no_exit_code_when_it_is_interrupted(tmp_path):
    """The measurement, as a test. No cloud: `gcloud` is a shell script here.

    Marked slow by nothing and costing two seconds, because the only way to
    observe what an interrupt does to a call in flight is to have a call in
    flight. A mock cannot: the behaviour under test belongs to `subprocess.run`,
    not to this codebase, and stubbing it out would test the stub.
    """
    marker = tmp_path / "created"
    fake = tmp_path / "gcloud"
    fake.write_text(f"#!/bin/sh\nsleep 2\n: > {marker}\necho '{{}}'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)

    gc = Gcloud()
    gc.require = lambda: str(fake)  # type: ignore[method-assign]

    def interrupt_soon():
        time.sleep(0.4)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=interrupt_soon, daemon=True).start()

    returned = "not reached"
    with pytest.raises(KeyboardInterrupt):
        returned = gc.run(["compute", "instances", "create", "x"], parse_json=False)

    assert returned == "not reached", (
        "if a value ever comes back from an interrupted call, the report can stop "
        "saying 'may' — and this test is where that gets noticed"
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
    # reason to say so: typer 0.27.1 VENDORS click, so the two are different
    # classes and Typer's handler matches only its own. Subclassing the click on
    # PATH compiles, imports, reads correctly, and propagates straight out of
    # `app()` — which is the failure `Interrupted` exists to prevent. Found by
    # driving the real entry point; no amount of reading would have shown it.
    from click.exceptions import Exit as ClickExit

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
