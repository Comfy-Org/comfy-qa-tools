"""The probe guard, held to the standard it exists to enforce.

`tests/tripwire.py` is for the code the suite never runs: the one-off probe, the
`python -c`, the triage script somebody writes at midnight. Those start OUTSIDE
conftest — which is the whole finding it was written from. A fixture is not
documentation; it is a local fact that protects only the code that requests it.

So the module itself needs a test, for the reason every guard here needed one: a
tripwire nobody has watched fire is indistinguishable from one that cannot.
"""

from __future__ import annotations

import socket
import subprocess

import pytest

# Plain import, not `from . import`: `tests/` is deliberately not a package —
# adding an __init__.py changes how pytest inserts paths for every other file
# here. Without one, pytest puts this directory on sys.path itself, which is the
# same way an ad-hoc probe beside it would find the module.
import tripwire


@pytest.fixture
def armed(monkeypatch):
    """Arm it, and put back what it replaced.

    `arm()` rebinds module attributes globally and has no disarm — correct for a
    probe that exits, wrong for a suite that keeps running. monkeypatch restores
    them whatever the test does.
    """
    monkeypatch.setattr(subprocess, "Popen", subprocess.Popen)
    monkeypatch.setattr(subprocess, "run", subprocess.run)
    monkeypatch.setattr(subprocess, "call", subprocess.call)
    monkeypatch.setattr(subprocess, "check_output", subprocess.check_output)
    monkeypatch.setattr(socket, "create_connection", socket.create_connection)
    tripwire.arm()


@pytest.mark.parametrize("name", ["Popen", "run", "call", "check_output"])
def test_every_spawn_entry_point_is_refused(armed, name):
    """All four, not the one the incident used.

    The probe that started a real `gcloud compute ssh` went through `Popen`, and
    the next one will not use the same door.

    MEASURED, because the redundancy here is easy to overstate: removing any ONE
    of the four bindings fails that param alone, so each is independently pinned.
    But `call` and `check_output` route through `Popen` internally, so with the
    `Popen` binding in place they would raise anyway — those two are
    belt-and-braces rather than load-bearing. Removing the `Popen` binding while
    keeping the other three fails only `[Popen]`, which is what proves it.

    Kept anyway: the cost is two lines, and a guard whose coverage depends on
    CPython's internal call graph is one refactor upstream from a hole.
    """
    with pytest.raises(AssertionError, match="PROCESS SPAWN ATTEMPTED"):
        getattr(subprocess, name)(["true"])


def test_a_non_loopback_connection_is_refused(armed):
    # TEST-NET-1 (RFC 5737), unroutable — so a MISS here cannot become a real
    # outbound connection while proving it missed.
    with pytest.raises(AssertionError, match="NON-LOOPBACK SOCKET"):
        socket.create_connection(("192.0.2.1", 443), 1)


def test_loopback_passes_through_to_a_real_connect(armed):
    """The exemption, asserted rather than assumed.

    The ComfyUI port check is a real connection to 127.0.0.1. A tripwire that
    fires on the honest case gets switched off, and a switched-off tripwire is
    the state this replaced — so the exemption is load-bearing, not a
    convenience. What must NOT happen is an AssertionError; a refused connection
    from the OS is the guard standing aside correctly.
    """
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", 1), 0.2)


def test_the_docstring_still_names_the_seam_that_caused_it(armed):
    """`tunnel.py` does not go through `Gcloud`, and that is the whole reason.

    Anyone patching `gcloud.Gcloud` and believing they have covered the process
    boundary repeats the incident exactly. If that sentence is ever edited out,
    the module keeps working and stops teaching.
    """
    assert "DOES NOT GO THROUGH THE" in tripwire.__doc__
    assert "TUNNEL_DIR" in tripwire.__doc__
