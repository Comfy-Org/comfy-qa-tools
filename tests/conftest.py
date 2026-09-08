"""The suite may not start a real tunnel, and may not ask this machine what a
process is.

The first half was live: `bring_up` opens a tunnel, and `tests/test_lifecycle.py`
calls it with a real `Host` and no launcher, so every run of `pytest tests/`
spawned detached `gcloud compute start-iap-tunnel` processes against whatever
project gcloud is signed in to — and left them running, because nothing in those
tests closes them. Eleven were alive on the machine this was written on, five of
them from one run. So the one seam that starts a process is replaced for the
whole suite. A test that means to exercise launching passes its own `launcher=`,
or asks for the `real_spawn` fixture and gets the original back.

The second half is why the same suite passed on macOS and failed on Ubuntu. The
stand-in has to read as a running tunnel, and whether it did depended on the
runner: on real `ps` output, and on where the platform's pid numbers stop. A test
that asserts a property of the runner rather than of the tool is worse than no
test. So this fixture *is* the process table: it remembers what it launched under
each pid and answers `tunnel._identity` from that record. Anything it did not
launch gets an answer that is stable and is plainly not a tunnel, which is what a
recycled pid looks like. The processes themselves stay real — the pid file, the
liveness check and the SIGTERM `down` sends all have to work against something —
but nothing about the result depends on what the OS would have said about them.
"""

from __future__ import annotations

import importlib
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from comfy_qa import tunnel as tunnel_module

_REAL_SPAWN = tunnel_module._spawn
_REAL_PORT_BUSY = tunnel_module._port_busy


@pytest.fixture
def real_spawn():
    """For the tests that are about `_spawn` itself."""
    return _REAL_SPAWN


@pytest.fixture
def real_port_busy():
    """For the test that is about the port probe itself."""
    return _REAL_PORT_BUSY


@pytest.fixture(autouse=True)
def never_start_a_real_tunnel(monkeypatch, request):
    """Every gcloud that would have been started, and not one that was."""
    launches: list[list[str]] = []
    processes: list[subprocess.Popen] = []
    launched: dict[int, str] = {}

    def harmless(command, log=None, **kwargs):
        # Reaches nothing and carries no gcloud, but is a real process: a pid
        # that can be signalled and really dies. What it *is* comes from the
        # table below rather than from `ps`, so it wears a tunnel's command line
        # by record instead of by argv.
        launches.append(list(command))
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        processes.append(process)
        launched[process.pid] = f"stand-in-{len(launches)} " + " ".join(command)
        return process.pid

    def identity(pid):
        """What this suite launched under that number, and nothing else.

        A pid nobody here started is exactly the case the tool has to survive —
        a recycled number — so it gets a stable answer that carries no tunnel
        marker. Never `""`: that means "could not tell", and would have the tool
        fall back to trusting the pid.
        """
        return launched.get(pid) or f"pid {pid} is not a tunnel this suite started"

    if "real_spawn" not in request.fixturenames:
        monkeypatch.setattr(tunnel_module, "_spawn", harmless)
    monkeypatch.setattr(tunnel_module, "_identity", identity)

    # The same argument for the other thing the tool asks the machine about.
    # Test hosts sit on 8190 and 8195; this Mac really does put a tunnel on 8190,
    # and the end-to-end tests deliberately run a fake ComfyUI on the port the
    # tunnel would forward. Either would make a run's result depend on what else
    # is listening. A test about the port check asks for `real_port_busy`.
    if "real_port_busy" not in request.fixturenames:
        monkeypatch.setattr(tunnel_module, "_port_busy",
                            lambda port, host="127.0.0.1": False)

    yield launches

    for process in processes:
        try:
            process.kill()
            process.wait(timeout=5)
        except OSError:
            pass


# The developer's own configuration directory, resolved ONCE before anything is
# redirected, so the tripwire below still knows where it is afterwards.
REAL_CONFIG_DIR = (Path.home() / ".config" / "comfy-qa-tools").resolve()

# Every module that bound `DEFAULT_CONFIG_PATH` at import time. Patching
# `config.DEFAULT_CONFIG_PATH` alone reaches NONE of them — `from .config import
# DEFAULT_CONFIG_PATH` copies the value — and that is precisely the mistake the
# old version of this fixture made one level down.
_BINDS_THE_DEFAULT_PATH = ("config", "host", "setup", "tunnel", "zones", "remove")

# Loopback is not a leak. The end-to-end tests run a real fake ComfyUI on
# 127.0.0.1 and really connect to it, which is the point of them.
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0.0.0.0", ""}


@pytest.fixture(autouse=True)
def never_write_to_the_real_config(monkeypatch, tmp_path, tmp_path_factory):
    """Nothing in this suite touches the user's own configuration directory.

    THE NAME IS THE PROMISE, AND IT USED TO BE FALSE. This fixture patched one
    attribute in one module — `tunnel.TUNNEL_DIR` — while its name and docstring
    said the directory was covered. `zone-latency.json` is written into that same
    directory by `zones.py`, and it went straight past: measured, a run under a
    cold HOME really created `~/.config/comfy-qa-tools/zone-latency.json`.
    Everyone downstream read the name and took the coverage, which is the same
    shape as a check whose remedy defeats it, sitting in the safety half of the
    suite.

    So it now redirects every name that resolves into that directory, in every
    module that bound one, and `no_test_writes_to_the_real_config` below fails
    the run if anything still gets there.

    WHAT THIS DOES NOT COVER, in the negative, because a docstring describing the
    mechanism instead of the promise is what let the first version sit:

    * A path a test builds from `Path.home()` itself rather than from one of
      these constants. The tripwire catches the write; nothing catches the read.
    * Anywhere else under the user's home. This is scoped to one directory, and
      `~/.ssh/google_compute_engine` is the other one this tool can create — via
      `gcloud compute ssh`, which `never_start_a_real_tunnel` above stops.
    * Reads. A test may still READ the real host list and quietly depend on it;
      that is what `test_readme.py::test_bare_comfy_qat_lists_your_machines` did,
      and only a cold HOME revealed it.
    """
    # `tmp_path_factory`, not `tmp_path`: a test's own tmp_path is something
    # tests assert on — one of them lists it and expects exactly one entry — so
    # the redirect must not appear inside it.
    redirected = tmp_path_factory.mktemp("config-redirect")

    for name in _BINDS_THE_DEFAULT_PATH:
        module = importlib.import_module(f"comfy_qa.{name}")
        if hasattr(module, "DEFAULT_CONFIG_PATH"):
            monkeypatch.setattr(module, "DEFAULT_CONFIG_PATH",
                                redirected / "hosts.toml")
    monkeypatch.setattr(tunnel_module, "TUNNEL_DIR", tmp_path / "tunnels")


@pytest.fixture(autouse=True)
def no_test_reaches_the_network(monkeypatch):
    """A unit suite may not open a socket to anything but this machine.

    Two tests did. `order_zones` with neither `config=` nor `probe=` measured
    real latency to `compute.{europe-west4,us-central1}.rep.googleapis.com:443`
    — three connections across the two — while asserting on a boolean and on a
    lowercased string. Neither wanted a latency number; that is what made it
    invisible.

    WHY THIS HAS TO EXIST RATHER THAN JUST FIXING THOSE TWO. A probe only fires
    for a region ABSENT from the cache, and the cache lives in the developer's
    home. So the first run on a machine and every run after it exercise
    different code, both green, the second far faster — and a full suite run on
    a warm machine writes nothing and connects to nothing. The leak is invisible
    exactly where people look for it. That also means THIS TRIPWIRE WILL NEVER
    FIRE ON A WARM MACHINE, so it will look like dead weight; it is not, and
    deleting it restores a defect nobody can see locally.

    Patched at `socket.create_connection`, which is low enough to catch
    `urllib`, `http.client` and anything built on them, and low enough that the
    suite's own fakes — `test_zones.py` patches this same attribute — replace it
    rather than being caught by it.

    NOT COVERED, deliberately and worth knowing: a raw `socket.socket().connect`
    that bypasses `create_connection`; `os.execvp`, which replaces this process
    entirely and is stubbed elsewhere; DNS, which `socket.getaddrinfo` can issue
    without connecting; and any subprocess, which has its own network namespace
    as far as this is concerned. `gcloud` is the one that matters there, and it
    is fenced by the fakes rather than by this.
    """
    real = socket.create_connection

    def guarded(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) and address else address
        if str(host) not in _LOOPBACK:
            raise AssertionError(
                f"this test opened a socket to {address!r}. A unit suite that "
                f"reaches the network measures the runner, not the tool — and "
                f"this one wrote its answer into the developer's home. Pass the "
                f"seam: `probe=` for latency, `config=` for where the cache goes."
            )
        return real(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", guarded)


@pytest.fixture(autouse=True)
def no_test_writes_to_the_real_config(monkeypatch):
    """The tripwire behind the redirect above, so the promise is enforced.

    The redirect is the fix; this is what says so. If a new module binds
    `DEFAULT_CONFIG_PATH` and is not added to `_BINDS_THE_DEFAULT_PATH`, the
    redirect silently stops covering it and only this notices.

    Every primitive this package actually writes through is wrapped:
    `Path.write_text`, `Path.open`, `Path.mkdir`, `Path.replace`, `os.replace`
    and `os.open` — the last two because `zones.write_cache` and `tunnel._claim`
    use them directly and would otherwise walk past a `pathlib`-only guard.
    """
    def refuse(path):
        try:
            resolved = Path(path).resolve()
        except (OSError, ValueError):
            return
        if resolved == REAL_CONFIG_DIR or REAL_CONFIG_DIR in resolved.parents:
            raise AssertionError(
                f"this test wrote {resolved} — inside the user's own "
                f"configuration directory. Use the `--config` seam, or "
                f"`config=`/`path=` where the call takes one."
            )

    def wrap(owner, name, index=0, when=lambda a, k: True):
        original = getattr(owner, name)

        def guarded(*args, **kwargs):
            if when(args, kwargs):
                refuse(args[index])
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, guarded)

    writing = lambda args, kwargs: (
        "w" in str(kwargs.get("mode", args[1] if len(args) > 1 else "r"))
        or "a" in str(kwargs.get("mode", args[1] if len(args) > 1 else "r"))
        or "x" in str(kwargs.get("mode", args[1] if len(args) > 1 else "r"))
    )

    wrap(Path, "write_text")
    wrap(Path, "write_bytes")
    wrap(Path, "mkdir")
    wrap(Path, "open", when=writing)
    wrap(Path, "replace", index=1)
    wrap(os, "replace", index=1)
    wrap(os, "open", when=lambda a, k: bool(
        (k.get("flags", a[1] if len(a) > 1 else 0)) & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)))


@pytest.fixture(autouse=True)
def an_empty_inflight_record():
    """The record is module state, and an interrupt is what leaves it non-empty.

    A test that interrupts a command and does not report leaves its leftover
    registered, and the next test's report would name a resource from a run that
    is already over. Cleared on the way in as well as the way out, so the order
    tests happen to run in cannot decide what one of them prints.
    """
    from comfy_qa import inflight

    inflight.clear()
    yield
    inflight.clear()


@pytest.fixture
def run_main(monkeypatch, capsys):
    """Drive the real entry point, which is where an interrupt is reported.

    `CliRunner` calls the Click command with `standalone_mode=False` and never
    reaches `cli.main`, so it cannot see the one handler this tool has for
    Ctrl-C — and it is the handler, not the command, that has to be right. This
    is also the only way to assert on the exit CODE the shell would see, which
    for an interrupt is the whole point: 130, not the 1 a failure uses.

    Returns `(code, combined output)`. Combined because the report is on stderr
    and everything leading up to it is on both, and a test that split them would
    be asserting on `say`'s stream rule rather than on the message. `test_say.py`
    owns that rule.
    """
    from comfy_qa.cli import main

    def run(argv):
        monkeypatch.setattr(sys, "argv", ["comfy-qat", *argv])
        code = 0
        try:
            main()
        except SystemExit as exit_:
            code = exit_.code if isinstance(exit_.code, int) else 1
        captured = capsys.readouterr()
        return code, captured.out + captured.err

    return run


# --- did the whole suite actually run? --------------------------------------
#
# A run that was CUT SHORT and a run that merely failed are typographically
# identical, and this is the only place in a pytest run that can tell them
# apart.
#
# Measured, on four tests with one stray SIGINT still in flight from the first:
# four collected, one reported, three never executed, and the last line pytest
# printed was `1 failed in 2.95s`. On a slightly different landing it was
# `no tests ran in 0.78s` with four unrun. Nothing in either says "three tests
# did not run" — not the summary, not the counts, and `!!! KeyboardInterrupt !!!`
# only appears for the one cause that happens to raise it.
#
# `test_suite_integrity.py` already records what a silent loss of cover costs
# here: 40 tests dropped by a merge, unnoticed for three commits, because the
# TOTAL WENT UP. This is the same loss through a different door. The tests are
# still on disk and still green in anyone's memory; they simply did not run, and
# "did not run" reads as "fine".
#
# WHY A SESSION HOOK AND NOT A TEST. A test executes DURING the session, so it
# cannot see how the session ended — the run it would need to describe is one in
# which later tests, possibly including itself, never start. There is nothing
# for a test to assert. `pytest_sessionfinish` runs on every exit path pytest
# has, INCLUDING the KeyboardInterrupt abort; checked rather than assumed, it is
# called from the `finally` in `_pytest.main.wrap_session`, which is the one
# path that loses tests without saying so.
#
# WHY NOT A DOCUMENTED SHELL COMMAND. `pytest --collect-only | tail -1` beside
# the summary line does reconcile, and it depends on somebody remembering to run
# it and reading two integers correctly. The header of `test_suite_integrity.py`
# has the receipts on where that ends: TWO mutation sweeps voided by shell-side
# result reading, both because `xfailed` contains the substring `failed`.
# Another arithmetic-in-a-shell ritual reproduces the defect class this is meant
# to close. The arithmetic below is in Python, runs itself, and cannot be
# forgotten.
#
# It is split into a pure function, a hook that decides, and a hook that prints,
# because only the first can be tested against counts no real run has to
# produce. `test_suite_integrity.py` owns those tests and owns the one that goes
# red if this is deleted.

# Every stats key that means "a collected test reached an outcome". NOT
# `deselected` — those are already subtracted from the collected total before it
# reaches here — and not `warnings` or `''`, which are not outcomes.
OUTCOMES = ("passed", "failed", "error", "skipped", "xfailed", "xpassed")

TRUNCATION = pytest.StashKey[int]()


def unaccounted_for(collected: int, stats: dict[str, int]) -> int:
    """How many collected tests the run never reported an outcome for.

    Zero is a session that finished. A POSITIVE number is a session that ended
    early, and those tests are unknown — not green.

    A NEGATIVE number is not truncation and is deliberately not reported as one:
    a test that fails in its call phase and then errors in teardown lands in two
    buckets, so the sum can honestly exceed the collected count.
    """
    return collected - sum(stats.get(name, 0) for name in OUTCOMES)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    """Decide. Printing is the next hook down; the exit status is decided here.

    Split that way because `pytest_terminal_summary` does not run for every exit
    code — `--no-summary` and an internal error both skip it — and the exit
    status is the half a script reads. It must not depend on the banner being
    printable.
    """
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None or session.config.option.collectonly:
        return

    missing = unaccounted_for(
        session.testscollected,
        {name: len(reports) for name, reports in reporter.stats.items()},
    )
    if missing <= 0:
        return

    session.config.stash[TRUNCATION] = missing

    # A truncated run that would otherwise have exited 0 is the dangerous one: a
    # green shell, a green eye, and part of the suite never executed. `-x` and
    # `--maxfail` truncate too, and the missing tests are just as unknown, but
    # the operator asked for that — it is reported and the status left alone.
    if exitstatus == 0 and not _asked_to_stop(session):
        session.exitstatus = 2


def _asked_to_stop(session) -> bool:
    return bool(
        getattr(session, "shouldstop", False)
        or getattr(session, "shouldfail", False)
        or session.config.option.maxfail
    )


@pytest.hookimpl(trylast=True)
def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Say it, as low on the screen as a plugin can put it.

    NOT `print`, and not `pytest_sessionfinish`. A bare `print` at session end
    goes into pytest's global capture, which is still installed, and is
    discarded — checked, after the first version of this produced nothing at all
    on the very run it was written for. And the terminal reporter implements
    `pytest_sessionfinish` as a WRAPPER, so anything written from an ordinary
    `sessionfinish` impl lands ABOVE the failure list however `trylast` is
    spelled; measured, it came out on line 1 of a 38-line run. From here it sits
    just above `short test summary info`, where the counts it is contradicting
    are.
    """
    missing = config.stash.get(TRUNCATION, 0)
    if not missing:
        return

    session = getattr(terminalreporter, "_session", None)
    collected = getattr(session, "testscollected", missing)
    asked_for = session is not None and _asked_to_stop(session)
    ran = collected - missing

    terminalreporter.write_line("")
    terminalreporter.write_sep("=", "SESSION TRUNCATED", red=not asked_for,
                               bold=True)
    terminalreporter.write_line(
        f"{missing} of {collected} collected tests never ran.")
    terminalreporter.write_line(
        "You asked for this (-x / --maxfail)." if asked_for
        else "They are UNKNOWN, not passed. Nothing else here says so.")
    terminalreporter.write_line(
        f"Every count below describes only the {ran} test"
        f"{'' if ran == 1 else 's'} that did run. Do not report a number from "
        f"this session; rerun it.")
    terminalreporter.write_sep("=", red=not asked_for, bold=True)
