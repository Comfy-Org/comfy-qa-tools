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
    assert [item.what for item in inflight.pending()] == ["the instance x in y"]


def test_interrupted_is_not_an_exception():
    """The one property that makes the whole thing work.

    Click catches `KeyboardInterrupt` inside its own `main()` and turns it into
    `Aborted!` and exit 1 before anything of ours runs, so the real thing cannot
    be used. And every `except Exception` in this package means "this step
    failed" — an interrupt that any of them can swallow is an interrupt that goes
    unreported through whichever one it meets first.
    """
    assert issubclass(inflight.Interrupted, BaseException)
    assert not issubclass(inflight.Interrupted, Exception)
    assert not issubclass(inflight.Interrupted, KeyboardInterrupt)


def test_nesting_holds_the_outer_entry_and_drops_the_inner_one():
    """`move` is three resources deep and the record has to be right at each."""
    with pytest.raises(inflight.Interrupted):
        with inflight.may_leave("the snapshot s"):
            with inflight.may_leave("the disk d"):
                pass
            assert [item.what for item in inflight.pending()] == ["the snapshot s"]
            raise KeyboardInterrupt
    assert [item.what for item in inflight.pending()] == ["the snapshot s"]


def test_reporting_says_may_and_never_claims_it_knows(capsys):
    with pytest.raises(inflight.Interrupted):
        with inflight.may_leave("the instance x in y", undo=["gcloud ... stop x"]):
            raise KeyboardInterrupt

    assert inflight.report() is True
    output = capsys.readouterr().err
    assert "may exist and be billing" in output, output
    assert "the instance x in y" in output
    assert "gcloud ... stop x" in output
    assert inflight.pending() == [], "reporting twice is the defect it replaced"


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
        with inflight.may_leave("comfy-win already stopped", billing=False):
            with inflight.may_leave("comfy-linux, started"):
                raise KeyboardInterrupt

    inflight.report()
    output = capsys.readouterr().err
    assert "this may exist and be billing:" in output, output
    assert output.index("comfy-linux, started") < output.index("comfy-win already stopped")
    assert "already happened when you stopped it" in output


# --- through the whole tool -------------------------------------------------


def test_an_interrupt_exits_130_and_never_prints_aborted(run_main, monkeypatch,
                                                         tmp_path):
    """130 is what a shell reports for SIGINT, and the code is half the message.

    `Aborted!` over a GPU box that is running and billing is the most expensive
    sentence this tool can print, and exit 1 is the same sentence said to a
    script. Both asserted, because fixing one and leaving the other is exactly
    what a partial fix here looks like.
    """
    from comfy_qa import gcloud as gcloud_module
    from comfy_qa.cli import INTERRUPTED

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

    assert code == INTERRUPTED
    assert "Aborted!" not in output, output
    assert "comfy-qat down comfy-win" in output


def test_a_command_that_finishes_is_untouched_by_any_of_this(run_main, tmp_path):
    """The record must be invisible on every path but one."""
    path = tmp_path / "hosts.toml"
    path.write_text('[hosts.local]\nkind = "local"\nport = 8188\n', encoding="utf-8")

    code, output = run_main(["list", "--config", str(path)])

    assert code == 0, output
    assert inflight.pending() == []
    assert "interrupted" not in output.lower()
