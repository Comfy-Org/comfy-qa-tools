"""The output vocabulary itself.

These are the rules the rest of the tool now leans on, so they are tested here
rather than inferred from whichever command happened to be under test. Three of
them are worth stating out loud, because each one was broken somewhere before
this module existed:

  - **the stream rule.** stdout is the answer, stderr is the story. It was
    decided per call site before, so one command put its progress on stderr and
    its result on stdout while its neighbour did the opposite.
  - **the `to fix:` alignment.** It used to be typed by hand as `"\\n        "`
    at every site that needed it, which is why ten of them carried the identical
    literal and one of them re-indented to two spaces instead.
  - **nothing redraws.** Output here gets pasted into Slack and into bug
    reports. A carriage return or an escape sequence survives a terminal and
    nothing else.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
import typer

from comfy_qa import say

ROOT = Path(__file__).resolve().parent.parent

# The modules that speak through `say`. `host.py` was the big one — around two
# thirds of the tool's output was in it — and it came over with `relocate.py`,
# which is the only other file `move` prints through. What is left is
# `tunnel.py`, which still composes fixes with a hand-typed indent, and the
# modules that never print at all: they raise, or report through a `say` their
# caller hands them. The rules below are held over the converted ones and this
# tuple is what grows as the rest follow.
CONVERTED = ("auth.py", "cli.py", "commands.py", "host.py", "lifecycle.py",
             "relocate.py", "render.py", "say.py")


# --- the stream rule -------------------------------------------------------


def test_the_answer_goes_to_stdout_and_everything_else_to_stderr(capsys):
    say.result("the answer")
    say.check(True, "a passing row")
    say.step("doing a thing")
    say.detail("a fact about it")
    say.warn("something to know")
    say.error("it broke", "do this")

    out, err = capsys.readouterr()
    assert out.splitlines() == ["the answer", "ok    a passing row"]
    assert err.splitlines() == [
        "  doing a thing",
        "    a fact about it",
        "warning: something to know",
        "",
        "it broke",
        "to fix: do this",
    ]


def test_a_failing_check_is_the_same_width_as_a_passing_one(capsys):
    say.check(True, "gcloud")
    say.check(False, "gcloud")
    ok, failed = capsys.readouterr().out.splitlines()
    assert ok.index("gcloud") == failed.index("gcloud")
    assert ok.startswith("ok") and failed.startswith("fail")


def test_fail_stops_with_the_code_it_was_given(capsys):
    with pytest.raises(typer.Exit) as caught:
        say.fail("no such environment: testclod", code=2)
    assert caught.value.exit_code == 2
    assert "no such environment: testclod" in capsys.readouterr().err


def test_an_exception_carries_its_own_fix(capsys):
    """The `echo(exc)`, `if exc.fix: echo(...)`, `Exit` triple, in one call.

    Fourteen sites wrote it out, and the ones that forgot the middle line left a
    failure with nowhere to go — which is exactly how `no project set` came to
    print a fix under `quota list` and nothing at all under `quota request`.
    """
    from comfy_qa.gcloud import GcloudError

    with pytest.raises(typer.Exit):
        say.fail(GcloudError("no project set", fix="comfy-qat setup"))
    assert "to fix: comfy-qat setup" in capsys.readouterr().err


def test_a_fix_passed_explicitly_beats_the_one_on_the_exception(capsys):
    from comfy_qa.gcloud import GcloudError

    say.error(GcloudError("no project set", fix="the wrong one"), "the right one")
    assert "to fix: the right one" in capsys.readouterr().err


# --- fixes -----------------------------------------------------------------


def test_a_multi_line_fix_lines_up_under_its_label(capsys):
    say.error("it broke", say.fix("first do this:", "then this", "and then this"))
    label, *rest = capsys.readouterr().err.splitlines()[2:]
    assert label == "to fix: first do this:"
    # Every later line starts where the first one's text does, under the label.
    assert {line.index(line.strip()[0]) for line in rest} == {len("to fix: ")}
    assert rest == ["        then this", "        and then this"]


def test_a_fix_written_the_old_way_by_hand_still_renders_the_same(capsys):
    """`tunnel.py` has not been converted yet.

    Its fix strings carry the eight-space indent inside them, and they are printed
    by handlers in modules that *have* converted. Both forms have to come out
    identical or the conversion could not happen one module at a time.
    """
    say.error("it broke", "first do this:\n        then this")
    by_hand = capsys.readouterr().err
    say.error("it broke", say.fix("first do this:", "then this"))
    assert capsys.readouterr().err == by_hand


def test_composing_a_fix_twice_does_not_indent_it_twice():
    once = say.fix("get onto the machine:", "gcloud compute ssh box")
    assert say.fix(once, "or stop paying for it: comfy-qat host down box") == say.fix(
        "get onto the machine:", "gcloud compute ssh box",
        "or stop paying for it: comfy-qat host down box")


def test_an_empty_fix_prints_no_label(capsys):
    say.error("it broke", "")
    assert capsys.readouterr().err.splitlines() == ["", "it broke"]


# --- counting --------------------------------------------------------------


@pytest.mark.parametrize("number,expected", [(0, "0 machines"), (1, "1 machine"),
                                             (2, "2 machines")])
def test_things_are_counted_without_a_form_to_fill_in(number, expected):
    assert say.count(number, "machine") == expected


def test_an_irregular_plural_can_be_given():
    assert say.count(2, "box", "boxes") == "2 boxes"


# --- elapsed time ----------------------------------------------------------


@pytest.mark.parametrize("seconds,expected", [
    (0, "0s"), (0.4, "0s"), (45, "45s"), (59.9, "59s"),
    (60, "1m00s"), (158, "2m38s"), (3599, "59m59s"),
    (3600, "1h00m"), (3900, "1h05m"),
    (-5, "0s"),      # a clock that went backwards is not a negative duration
])
def test_elapsed_reads_the_same_at_every_scale(seconds, expected):
    assert say.elapsed(seconds) == expected


# --- slow steps ------------------------------------------------------------


class Clock:
    """A hand-wound clock, so a three-minute step takes no time to test."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def collected():
    lines: list[str] = []
    return lines, lines.append


def test_a_slow_step_says_what_it_is_and_how_long_it_should_take():
    lines, emit = collected()
    clock = Clock()
    say.slow("snapshotting the disk", expect="2-5 minutes",
             emit=emit, clock=clock, background=False).start()
    assert lines == ["snapshotting the disk (2-5 minutes)"]


def test_a_step_that_runs_for_minutes_keeps_saying_so():
    lines, emit = collected()
    clock = Clock()
    step = say.slow("snapshotting the disk", emit=emit, clock=clock,
                    every=30, background=False).start()

    for _ in range(60):          # a poll every five seconds, for five minutes
        clock.now += 5
        step.tick()
    step.done()

    assert lines[0] == "snapshotting the disk"
    assert lines[1:-1] == [f"  still going, {say.elapsed(n)}" for n in
                           (30, 60, 90, 120, 150, 180, 210, 240, 270, 300)]
    assert lines[-1] == "  done in 5m00s"


def test_nothing_redraws_so_the_whole_thing_pastes():
    """The reason there is no spinner: a bug report is a code block, not a TTY."""
    lines, emit = collected()
    clock = Clock()
    step = say.slow("installing torch", emit=emit, clock=clock, every=30,
                    background=False).start()
    clock.now += 90
    step.tick()
    step.done()

    assert lines
    for line in lines:
        assert "\r" not in line and "\x1b" not in line


def test_a_quick_step_stays_one_line():
    """A timing on everything is noise; the point is the steps that are slow."""
    lines, emit = collected()
    clock = Clock()
    step = say.slow("reading quota", emit=emit, clock=clock, every=30,
                    background=False).start()
    clock.now += 2
    step.done()
    assert lines == ["reading quota"]


def test_a_step_that_failed_does_not_claim_it_finished():
    lines, emit = collected()
    clock = Clock()
    step = say.slow("starting the box", emit=emit, clock=clock, every=30,
                    background=False).start()
    clock.now += 300
    step.tick()
    step.give_up()
    assert not any("done" in line for line in lines)


def test_the_context_manager_times_a_success_and_stays_quiet_on_a_failure():
    lines, emit = collected()
    clock = Clock()
    with say.slow("one", emit=emit, clock=clock, background=False):
        clock.now += 60
    assert lines[-1] == "  done in 1m00s"

    lines2, emit2 = collected()
    clock2 = Clock()
    with pytest.raises(ValueError):
        with say.slow("two", emit=emit2, clock=clock2, background=False):
            clock2.now += 60
            raise ValueError("something went wrong")
    assert lines2 == ["two"]


def test_done_can_name_what_finished():
    lines, emit = collected()
    clock = Clock()
    step = say.slow("installing ComfyUI", emit=emit, clock=clock, background=False).start()
    clock.now += 372
    step.done("installed")
    assert lines[-1] == "  installed in 6m12s"


def test_without_an_emit_the_step_uses_the_module_indents(capsys):
    clock = Clock()
    step = say.slow("reading quota", expect="about a minute", clock=clock,
                    every=30, background=False).start()
    clock.now += 60
    step.tick()
    step.done()
    out, err = capsys.readouterr()
    assert out == ""
    assert err.splitlines() == [
        "  reading quota (about a minute)",
        "    still going, 1m00s",
        "    done in 1m00s",
    ]


def test_a_piped_run_says_the_same_thing_less_often(monkeypatch):
    """Degrading to fewer lines, never to different ones.

    Nobody watches a redirected log in real time, and a line every half-minute
    would be most of the file by the time a move finished.
    """
    monkeypatch.setattr(say, "watching", lambda: True)
    assert say.slow("x", background=False)._every == say.TICK_SECONDS
    monkeypatch.setattr(say, "watching", lambda: False)
    assert say.slow("x", background=False)._every == say.PIPED_TICK_SECONDS
    assert say.PIPED_TICK_SECONDS > say.TICK_SECONDS


def test_the_background_ticker_stops_when_the_step_does():
    """A daemon thread that outlived its step would talk over the next one."""
    step = say.slow("x", emit=collected()[1], every=0.01).start()
    assert step._thread is not None and step._thread.is_alive()
    step.done()
    assert step._thread is None


def test_the_tty_rule_is_gcloud_s_rule_and_not_a_second_one(monkeypatch):
    """One rule for "is anyone there", because two of them disagree eventually.

    `gcloud.can_prompt` already encodes it — stdin *and* stderr both terminals —
    and it is the rule the tool has to live with, since it is the one gcloud
    itself applies before deciding whether it may re-prompt for a credential.
    """
    from comfy_qa import gcloud

    monkeypatch.setattr(gcloud, "can_prompt", lambda: True)
    assert say.watching() is True
    monkeypatch.setattr(gcloud, "can_prompt", lambda: False)
    assert say.watching() is False


# --- the house style, held across the package ------------------------------


def _docstrings(tree: ast.AST) -> set[int]:
    """Every string that is documentation rather than output.

    A docstring is allowed to quote the old spelling in order to say it is gone —
    that is what the comments in `say.py` and `render.py` are for — so the style
    rules below are about what the tool *prints*, not about what it explains.
    """
    kinds = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, kinds) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                found.add(id(first.value))
    return found


def _package_string_constants(only: str | None = None):
    paths = [ROOT / "comfy_qa" / only] if only else sorted((ROOT / "comfy_qa").glob("*.py"))
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        documentation = _docstrings(tree)
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in documentation):
                yield f"{path.name}:{node.lineno}", node.value


def test_nothing_in_the_package_prints_an_escape_sequence_or_a_carriage_return():
    """Colour, cursor movement and progress bars all paste as garbage.

    This is the rule the whole module exists to serve, and it is cheap enough to
    hold over every string in the package rather than only the converted ones.
    """
    for where, text in _package_string_constants():
        assert "\x1b" not in text, f"{where}: an escape sequence"
        assert "\r" not in text, f"{where}: a carriage return"


def test_the_hand_copied_stop_paying_literal_is_gone():
    """It was in `lifecycle.py` ten times, eight-space indent and all.

    `"\\n        or stop paying for it:\\n        "` was pasted into ten fix
    strings, so the wording and the alignment could drift apart eleven ways.
    `_with_the_bill` is the one place it is written now.
    """
    carrying = [where for where, text in _package_string_constants("lifecycle.py")
                if "stop paying for it" in text]
    assert len(carrying) == 1, carrying

    # Held over the converted modules only. `tunnel.py` is the last one still
    # composing fixes by hand; add it here when it converts, and this becomes the
    # thing that stops the literal coming back.
    for name in CONVERTED:
        for where, text in _package_string_constants(name):
            assert "\n        " not in text, (
                f"{where}: a fix indented by hand — compose it with say.fix")


def test_no_converted_module_still_writes_to_stderr_by_hand():
    """`say` is the only way out, or the vocabulary is advisory.

    `host.py` is the one that mattered: it held roughly two thirds of the tool's
    `typer.echo` calls, including every place `move`, `switch` and `go` report a
    failure. Anything not listed here is a module that has not converted yet. Add
    a name when it does.
    """
    for name in CONVERTED:
        tree = ast.parse((ROOT / "comfy_qa" / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else ""
            if attr != "echo" or name == "say.py":
                continue
            assert not any(kw.arg == "err" for kw in node.keywords), (
                f"{name}:{node.lineno} writes to stderr without going through say")


def test_one_spelling_of_a_warning():
    """`WARNING ` and `warning: ` were both in the output at the same time."""
    for where, text in _package_string_constants():
        assert not re.search(r"\bWARNING\b", text), f"{where}: {text!r}"


def test_one_spelling_of_a_failure_mark():
    """`!!`, `<< MISMATCH`, `FAIL ` and `ok  ` were four notations for two ideas."""
    for where, text in _package_string_constants():
        assert "<< MISMATCH" not in text, f"{where}: {text!r}"
        assert not re.search(r"\bFAIL\b", text), f"{where}: {text!r}"
