"""The two tripwires in `conftest.py`, held to the standard they enforce.

Neither of these fires during a normal run, and that is exactly why they need
their own tests.

The socket tripwire cannot fire on a warm machine at all: a latency probe only
runs for a region ABSENT from `zone-latency.json`, and that file lives in the
developer's home, so the second run on any machine never reaches the network.
Measured — a full suite run on a warm machine opens no outbound socket and
writes nothing. A guard nobody has ever seen fire is a guard somebody deletes as
dead weight, and deleting this one restores a defect that is invisible locally
and only visible on a fresh machine or in CI.

The write tripwire never fires either, because the redirect in
`never_write_to_the_real_config` is supposed to make it unreachable. That makes
it a claim about a fixture rather than about the tool — and the only way to know
a claim like that is true is to aim something at it on purpose.
"""

from __future__ import annotations

import os

import pytest

from conftest import REAL_CONFIG_DIR

TARGET = REAL_CONFIG_DIR / "a-file-no-test-may-create"


@pytest.mark.parametrize("write", [
    pytest.param(lambda: TARGET.write_text("x"), id="Path.write_text"),
    pytest.param(lambda: TARGET.write_bytes(b"x"), id="Path.write_bytes"),
    pytest.param(lambda: TARGET.open("w"), id="Path.open(w)"),
    pytest.param(lambda: TARGET.open("a"), id="Path.open(a)"),
    pytest.param(lambda: (REAL_CONFIG_DIR / "sub").mkdir(), id="Path.mkdir"),
    pytest.param(lambda: os.replace("/tmp/nothing-here", TARGET), id="os.replace"),
    pytest.param(lambda: os.open(TARGET, os.O_CREAT | os.O_WRONLY), id="os.open"),
])
def test_every_write_primitive_into_the_real_config_dir_is_refused(write):
    """Each one is used by this package somewhere, so each one is wrapped.

    `os.replace` and `os.open` are the two a `pathlib`-only guard would miss —
    `zones.write_cache` and `tunnel._claim` call them directly.
    """
    with pytest.raises(AssertionError, match="configuration directory"):
        write()

    assert not TARGET.exists(), "the tripwire raised and the write happened anyway"


def test_a_write_anywhere_else_is_untouched(tmp_path):
    """A tripwire that refused everything would also be green."""
    (tmp_path / "fine.txt").write_text("ok", encoding="utf-8")
    assert (tmp_path / "fine.txt").read_text(encoding="utf-8") == "ok"


def test_reading_the_real_config_is_deliberately_still_possible():
    """Named here because the fixture's docstring names it as NOT covered.

    A test can still read the developer's host list and quietly depend on it —
    which is what `test_readme.py::test_bare_comfy_qat_lists_your_machines` did,
    passing only because that person had hosts declared. Nothing here catches
    that; only a cold HOME does. This test exists so the gap is written down in
    executable form rather than in prose somebody may not reach.
    """
    (REAL_CONFIG_DIR / "hosts.toml").exists()


def test_a_socket_to_anywhere_but_this_machine_is_refused():
    import socket

    with pytest.raises(AssertionError, match="opened a socket"):
        socket.create_connection(("compute.us-central1.rep.googleapis.com", 443))


def test_loopback_is_not_a_leak():
    """The end-to-end tests run a real fake ComfyUI on 127.0.0.1 and really
    connect to it. Refusing that would make the tripwire unusable, so the rule is
    non-loopback, not no-sockets."""
    import socket

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        socket.create_connection(listener.getsockname()).close()
    finally:
        listener.close()
