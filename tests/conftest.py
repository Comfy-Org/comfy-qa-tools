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

import subprocess
import sys

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


@pytest.fixture(autouse=True)
def never_write_to_the_real_config(monkeypatch, tmp_path):
    """The tunnel directory defaults beside the user's own host list."""
    monkeypatch.setattr(tunnel_module, "TUNNEL_DIR", tmp_path / "tunnels")


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
