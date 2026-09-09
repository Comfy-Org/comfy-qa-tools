"""The no-colour claim in `say.py`, held to what is actually true.

The claim used to be unqualified — "what is deliberately absent: spinners,
progress bars, cursor movement, colour" — sitting in the module that owns this
tool's output vocabulary, and so reading as a claim about the tool. It is not one:
`comfy-qat --help` is rendered by Typer, Typer renders through Rich, and on a
terminal that help carries around three hundred SGR escapes.

The claim was narrowed rather than made true, and this file is why that is
defensible rather than a dodge. It asserts the narrowed claim in both directions:

  1. `say`'s own writers emit no escape at all, on either stream, in any of the
     seven kinds — which is the claim, and is worth a test on its own because
     it would be one `typer.style` call away from being false.
  2. Typer's help still colours, and only on a terminal. Both halves matter. The
     colour is why the docstring needs a caveat, and its being terminal-only is
     why the caveat is honest: the failure the docstring describes is output that
     "survives only the first reading", and a redirect — the only route by which
     an escape could reach a Slack paste — is already plain.

If Typer ever stops colouring, or starts colouring a pipe, the second test goes
red and the caveat can be rewritten from what it says. That is the point of
pinning a narrowed claim: the narrowing has to stay necessary.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
import typer

from comfy_qa import say

ROOT = Path(__file__).resolve().parent.parent

# Any escape sequence at all, not just SGR. Cursor movement and erase-line are
# the other things the docstring says are absent, and they share this prefix.
ESCAPE = re.compile(r"\x1b\[")


class _PretendTerminal(io.StringIO):
    """A capture that answers `isatty()` — which this test needs to say `True`.

    `typer.echo` is `click.echo`, and click STRIPS ansi from a stream that is not
    a terminal. Capture into a plain StringIO and a `typer.style` call anywhere in
    `say` comes back clean: the check passes, and passes for a reason that has
    nothing to do with the code being right. Measured — styling `say.warn` yellow
    and running this file was four green tests.

    A terminal is also what the claim is about. Nobody was ever going to see
    colour in a pipe; the docstring is about what a person reads.
    """

    def isatty(self) -> bool:
        return True


def _everything_say_can_write() -> tuple[str, str]:
    """Drive every writer in `say`, once each, and return (stdout, stderr)."""
    out, err = _PretendTerminal(), _PretendTerminal()
    with redirect_stdout(out), redirect_stderr(err):
        say.result("a result")
        say.result()
        say.check(True, "a row that passed")
        say.check(False, "a row that failed")
        say.step("a step")
        say.detail("a detail")
        say.warn("something worth knowing")
        say.error("something went wrong", say.fix("do this", "then this"))
        say.write_fix("a fix on its own")
        with pytest.raises(typer.Exit):
            say.fail("something went wrong and we stopped")

        # `Slow` on a hand-driven clock, so the tick and the closing line are
        # both produced without waiting on a thread.
        clock = iter([0.0, 0.0, 61.0, 61.0, 61.0, 61.0])
        slow = say.slow(
            "a long step",
            expect="2-5 minutes",
            every=30,
            clock=lambda: next(clock),
            background=False,
        )
        slow.start()
        slow.tick()
        slow.done()
    return out.getvalue(), err.getvalue()


def test_say_writes_no_escape_sequence_on_either_stream():
    stdout, stderr = _everything_say_can_write()
    assert stdout and stderr, "the writers produced nothing — the drive is broken"
    for name, text in (("stdout", stdout), ("stderr", stderr)):
        found = ESCAPE.findall(text)
        assert not found, (
            f"say wrote {len(found)} escape sequence(s) to {name}. The module "
            f"docstring says colour, spinners and cursor movement are absent from "
            f"these writers, and that is the half of the claim that is still "
            f"unqualified. Either take the styling out or rewrite the docstring."
        )


def _help(**env_extra: str) -> str:
    """`comfy-qat --help`, run as a subprocess so Typer's console is built fresh.

    `typer.rich_utils` reads `FORCE_COLOR` once, at import, into a module-level
    constant. Setting it in this process would be too late, so the help is run
    out of process. `python -m comfy_qa` rather than the console script, so this
    passes in a checkout that was never installed.
    """
    env = {**os.environ, "COLUMNS": "80", "LINES": "50"}
    env.pop("NO_COLOR", None)
    env.pop("_TYPER_FORCE_DISABLE_TERMINAL", None)
    env.update(env_extra)
    done = subprocess.run(
        [sys.executable, "-m", "comfy_qa", "--help"],
        cwd=ROOT, env=env, capture_output=True, timeout=120,
    )
    assert done.returncode == 0, done.stderr.decode("utf-8", "replace")
    return done.stdout.decode("utf-8", "replace")


def test_typer_still_colours_the_help_so_the_docstring_still_needs_its_caveat():
    coloured = _help(FORCE_COLOR="1")
    assert ESCAPE.search(coloured), (
        "comfy-qat --help no longer emits any escape sequence even with colour "
        "forced. That is the thing say.py's docstring carves out an exception "
        "for, so the exception is now telling people something untrue. Widen the "
        "claim back to the whole tool and delete this test."
    )


def test_the_help_escapes_never_reach_a_redirect():
    """Which is the reason narrowing the claim is honest rather than convenient.

    The docstring's worry is a paste that survives only one reading. An escape
    can only get into one via a redirect, and a redirect is not a terminal, so
    Rich writes none. Captured output here is a pipe, which is that case.
    """
    plain = _help()
    assert not ESCAPE.search(plain), (
        "comfy-qat --help now emits escape sequences into a pipe, not just a "
        "terminal, so `comfy-qat --help > help.txt` carries them and they can "
        "reach a paste. That is the failure say.py's docstring describes, and it "
        "is no longer excusable as terminal-only — turn Typer's colour off."
    )


def test_the_docstring_names_the_exception_it_depends_on():
    """The claim drifts back by losing its subject, which is how it got here.

    Both sentences below have to be present: whose output is being talked about,
    and what is excluded from it. A rewrite that drops either one restores the
    unqualified claim, and nothing else in the suite reads prose.
    """
    doc = say.__doc__ or ""
    assert "colour" in doc, "the docstring no longer says anything about colour"
    assert "--help" in doc, (
        "say.py's docstring claims colour is deliberately absent and no longer "
        "names `comfy-qat --help` as the exception. Typer colours that help — "
        "test_typer_still_colours_the_help_so_the_docstring_still_needs_its_caveat "
        "proves it — so the claim reads as one about the tool and is false. Say "
        "whose output the paragraph is about."
    )


# --- NO_COLOR ----------------------------------------------------------------
#
# The caveat above says Typer colours the help and argues that is tolerable
# because the escapes are terminal-only and cannot reach a paste. That argument
# covers the person who does not mind colour. It says nothing about the person
# who has asked for none — and NO_COLOR is how they ask.
#
# It was ignored. Measured on a pty before the fix: 348 escapes with NO_COLOR
# set, 348 without. Rich honours the variable; Typer builds its own Console and
# does not pass it on, so setting it did precisely nothing.


def _help_on_a_terminal(**env_extra: str) -> str:
    """`comfy-qat --help` with a pty on the other end, which is the only place
    Rich colours at all — a pipe is plain however the environment is set, so a
    captured run cannot tell whether NO_COLOR was honoured or irrelevant."""
    import pty
    import subprocess

    env = {**os.environ, "COLUMNS": "80", "LINES": "50"}
    for name in ("NO_COLOR", "FORCE_COLOR", "_TYPER_FORCE_DISABLE_TERMINAL"):
        env.pop(name, None)
    env.update(env_extra)

    main, worker = pty.openpty()
    process = subprocess.Popen(
        [sys.executable, "-m", "comfy_qa", "--help"],
        stdout=worker, stderr=worker, stdin=worker, env=env, cwd=ROOT)
    os.close(worker)
    seen = b""
    try:
        while True:
            chunk = os.read(main, 65536)
            if not chunk:
                break
            seen += chunk
    except OSError:
        # The child exited and the pty went away, which is the ordinary end.
        pass
    process.wait(timeout=120)
    os.close(main)
    return seen.decode("utf-8", "replace")


def test_no_colour_is_honoured_in_the_one_place_this_tool_has_colour():
    coloured = _help_on_a_terminal()
    assert ESCAPE.search(coloured), (
        "the help emits no colour on a terminal even without NO_COLOR, so this "
        "test can no longer tell an honoured request from an empty one"
    )
    plain = _help_on_a_terminal(NO_COLOR="1")
    assert not ESCAPE.search(plain), (
        "NO_COLOR is set and comfy-qat --help still writes escape sequences. "
        "comfy_qa/__init__.py sets Typer's disable switch for exactly this; "
        "either the switch was renamed by an upgrade or the import that reads "
        "it now happens before __init__ runs."
    )


def test_turning_the_colour_off_does_not_take_the_help_layout_with_it():
    """The reason this is done with Typer's switch rather than
    `rich_markup_mode=None`: the box, the columns and the option names all have
    to survive. Only the escapes go."""
    plain = _help_on_a_terminal(NO_COLOR="1")
    for expected in ("Usage:", "--config", "--version", "Commands", "setup"):
        assert expected in plain, f"{expected!r} went with the colour"


def test_an_empty_no_colour_is_not_a_request():
    """The standard is explicit that any non-empty value means yes, which makes
    the empty string mean nothing — and an exported-but-empty variable is a
    common shape in a shell profile."""
    assert ESCAPE.search(_help_on_a_terminal(NO_COLOR=""))
