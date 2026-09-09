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


def test_the_notes_beside_a_run_of_commands_are_in_one_column(capsys):
    """The gap used to be typed at the call site, so it was counted by eye.

    `comfy-qat stamp l4` against a box with no tunnel printed the three commands
    below with the last note two columns right of the other two — in the block
    someone reads when nothing is working.
    """
    say.error("nothing answered", say.fix(
        "comfy-qat open comfy-win   # tunnel to a box that is already running",
        "comfy-qat go comfy-win     # start it and tunnel, in one step",
        "comfy-qat list --live        # which of the two it is",
    ))
    rest = capsys.readouterr().err.splitlines()[3:]
    hashes = {line.index("#") for line in rest if "#" in line}
    assert len(hashes) == 1, f"the notes are in {len(hashes)} columns: {rest}"
    assert rest[-1].endswith("comfy-qat list --live      # which of the two it is")


def test_a_column_is_shared_by_the_commands_it_belongs_to_and_no_others(capsys):
    """A prose line ends a run. Two groups, two columns — a column shared across
    a paragraph break lines nothing up with anything."""
    say.error("it broke", say.fix(
        "comfy-qat down comfy-win   # a short one",
        "or, if you would rather look first:",
        "comfy-qat list --live --config some/rather/long/path   # a long one",
    ))
    # From the label line, which carries the first command and its note.
    rest = capsys.readouterr().err.splitlines()[2:]
    hashes = [line.index("#") for line in rest if "#" in line]
    assert len(set(hashes)) == 2, f"the two groups share a column: {rest}"


def test_a_sentence_among_the_commands_gets_a_line_of_its_own(capsys):
    """The Ctrl-C block was eight lines flush against each other at one indent —
    four commands, a sentence introducing two of them, and a remark about what a
    snapshot costs, all reading as things to paste."""
    say.error("interrupted", say.fix(
        "gcloud compute snapshots delete comfy-linux-snap",
        "comfy-qat down comfy-win",
        "or check first, if you would rather look:",
        "comfy-qat list --live",
        "a snapshot costs pennies a month, but it is not free",
    ))
    rest = capsys.readouterr().err.splitlines()[3:]
    assert rest == [
        "        comfy-qat down comfy-win",
        "",
        "        or check first, if you would rather look:",
        "        comfy-qat list --live",
        "",
        "        a snapshot costs pennies a month, but it is not free",
    ], rest


def test_a_short_fix_gets_no_blank_lines_because_it_has_no_groups(capsys):
    """Two lines are already legible; a break between them is only air."""
    say.error("it broke", say.fix("comfy-qat down comfy-win",
                                  "the machine is still billing."))
    assert capsys.readouterr().err.splitlines()[3:] == [
        "        the machine is still billing."]


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

    # 20 first, then every 30: the backoff's first interval is capped at `every`,
    # so a half-minute cadence reaches its steady state on the second tick and
    # the five minutes cost the same ten lines they always did.
    assert lines[0] == "snapshotting the disk"
    assert lines[1:-1] == [f"  still going, {say.elapsed(n)}" for n in
                           (20, 50, 80, 110, 140, 170, 200, 230, 260, 290)]
    assert lines[-1] == "  done in 5m00s"


# --- the ticks back off -------------------------------------------------------
#
# These count lines at chosen elapsed times on a hand-driven clock. Not one of
# them sleeps or measures a real duration: a timing assertion on a loaded laptop
# fails for reasons that have nothing to do with the code, and the first person
# it fails on deletes it. `Clock` is the whole harness.


def _tick_seconds(every, run_for, poll=1):
    """Elapsed times, in whole seconds, at which a `Slow` said "still going"."""
    lines, emit = collected()
    clock = Clock()
    step = say.slow("installing", emit=emit, clock=clock, every=every,
                    background=False).start()
    for _ in range(int(run_for // poll)):
        clock.now += poll
        step.tick()
    step.give_up()      # no closing line, so `lines` is ticks and the headline
    return [n for n in range(int(run_for) + 1)
            if f"  still going, {say.elapsed(n)}" in lines]


def test_the_first_sign_of_life_comes_long_before_the_full_interval():
    """The minute of silence is where somebody Ctrl-Cs a box that is billing.

    `go`'s install goes quiet for minutes fetching torch. At a flat 60s tick the
    first line landed a full minute after the output stopped, which is well past
    the point where a person has already decided it hung.
    """
    assert _tick_seconds(every=60, run_for=19) == []
    assert _tick_seconds(every=60, run_for=20) == [20]
    # And it is genuinely the first thing said, not the second.
    assert _tick_seconds(every=60, run_for=59) == [20]


def test_the_backoff_widens_to_the_plain_cadence_and_stays_there():
    """20, then 40, then 60 forever — the ticks rejoin the old schedule."""
    assert _tick_seconds(every=60, run_for=480) == [
        20, 60, 120, 180, 240, 300, 360, 420, 480]


def test_an_earlier_first_line_costs_one_line_on_a_long_install():
    """The count is the argument. A tick is a line someone scrolls past in Slack.

    Nine lines instead of eight over eight minutes buys a first sign of life
    three times sooner. A five-second tick would have bought it with ninety-six.
    """
    eight_minutes = 480
    flat = eight_minutes // 60
    assert len(_tick_seconds(every=60, run_for=eight_minutes)) == flat + 1
    assert len(_tick_seconds(every=5, run_for=eight_minutes)) > 90


def test_every_length_of_step_gets_the_early_line_and_pays_at_most_two():
    """Both halves of the bargain, for every duration and every cadence.

    Held over the whole range rather than the one example, because the numbers
    that make the argument — one line, three times sooner — are true of the
    eight-minute install and have to stay true of the twenty-minute one.
    """
    for run_for in range(30, 1201, 30):
        for every in (30, 60, 120):
            ticks = _tick_seconds(every=every, run_for=run_for)
            assert ticks[0] == min(say.FIRST_TICK_SECONDS, every), (
                f"{run_for}s at every={every}: first line at {ticks[0]}s")
            extra = len(ticks) - int(run_for // every)
            assert 0 <= extra <= 2, f"{run_for}s at every={every}: {extra} extra"


def test_a_piped_run_is_told_it_is_alive_as_early_as_a_watched_one():
    """The pipe gets FEWER lines, never a longer wait for the first one."""
    first_piped = _tick_seconds(every=say.PIPED_TICK_SECONDS, run_for=600)[0]
    first_watched = _tick_seconds(every=say.TICK_SECONDS, run_for=600)[0]
    assert first_piped == first_watched == say.FIRST_TICK_SECONDS


def test_a_step_faster_than_the_first_interval_is_not_slowed_by_it():
    """`every` is a ceiling the backoff climbs to, never a floor it starts above."""
    assert say.FIRST_TICK_SECONDS > 5
    assert _tick_seconds(every=5, run_for=20) == [5, 10, 15, 20]


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
        # A constant that IS a line ending is not output — `hostfile` composes
        # file content and has to write `\r\n` back into a CRLF host list, or the
        # rewritten line is the one LF in the file. This rule is about what gets
        # PRINTED, and the walk cannot tell printing from writing.
        #
        # The exemption is on the VALUE, not on a module or a name. A message can
        # never be exactly "\r\n", so this cannot widen by accident — which a
        # module list would, and this codebase has five hand-maintained lists
        # that each drifted before someone made them derive or fail.
        if text in ("\r\n", "\r"):
            continue
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


# --- the story reaches stderr even where it is handed to somebody else -----


def _false_names(tree: ast.AST) -> frozenset[str]:
    """Every name this module binds to a falsy constant.

    Collected across the whole module, not per-scope: a guard should rather
    flag a name that a nearer binding would have made true than miss one, and
    the `err=` value is the whole subject here. Nothing in the package binds a
    name to False today, so the widening is free of false positives now — the
    parametrized cases below are what keep it honest.
    """
    bound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        else:
            continue
        value = getattr(node, "value", None)
        if not isinstance(value, ast.Constant) or value.value:
            continue
        bound.update(t.id for t in targets if isinstance(t, ast.Name))
    return frozenset(bound)


def _means_stderr(node: ast.AST, false_names: frozenset[str]) -> bool:
    """Can we SEE that this `err=` value sends the line to stderr?

    THE LIMIT, stated rather than left silent: only a literal and a name bound
    to a falsy constant in this module are readable. `err=flag` where `flag` is
    a parameter, an import, a call or an expression is unknowable without
    running the code, so it is read as stderr and the call goes unflagged. A
    sink can still hide there. `KNOWN LIMIT` in SINK_SHAPES below pins that
    exact shape, so the day someone closes it the case turns red and says so.
    """
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, ast.Name):
        return node.id not in false_names
    return True


def _writes_to_stdout(call: ast.Call,
                      false_names: frozenset[str] = frozenset()) -> bool:
    """Does this call put something on stdout?

    Four routes, because the guard that only knew the first one let the other
    three past — measured, one shape at a time, against a real package module.

    `err` is read for its VALUE, not its presence. The old test asked
    `not any(kw.arg == "err")`, so `typer.echo(line, err=False)` — which says
    stdout out loud — satisfied a guard whose whole subject is the stream. The
    value is read through `_means_stderr`, which also resolves a name bound to
    False and names what it still cannot see.

    `secho` sits beside `echo` because it is the same function with colour and
    the same `err` keyword. Nothing in the package calls it today; a guard that
    only covers the spellings already in use is a guard the next commit gets
    past for free.
    """
    func = call.func
    name = getattr(func, "attr", None) or getattr(func, "id", None)
    if name in ("echo", "secho"):
        return not any(
            kw.arg == "err" and _means_stderr(kw.value, false_names)
            for kw in call.keywords
        )
    if name in ("print", "result"):
        # `say.result` is the answer and belongs on stdout — from a COMMAND. As a
        # progress sink it is the same defect wearing the vocabulary's own name.
        return True
    if name == "write":
        target = getattr(func, "value", None)
        return isinstance(target, ast.Attribute) and target.attr == "stdout"
    return False


def _handed_over(tree: ast.AST):
    """Callables passed to somebody else. That is what a sink IS.

    The distinction matters and the first attempt at widening this got it wrong:
    checking every function that writes to stdout flags all 26 commands, because
    printing the answer is their job. What makes a callable a progress sink is
    not what it writes, it is that it was handed to `lifecycle` to be called
    later — which is exactly what this section's heading has always said.

    Named functions are only counted when NESTED, for the same reason. Typer
    passes module-level commands around as values — `ctx.invoke(host.list_cmd)`,
    `callback=_version_callback`, `choose=_choose` — and every one of those is a
    command doing its job, not a sink. Measured: unrestricted, those four are the
    entire false-positive set.
    """
    lambdas, names, partials = [], set(), []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            if isinstance(arg, ast.Lambda):
                lambdas.append(arg)
            elif isinstance(arg, ast.Name):
                names.add(arg.id)
            elif isinstance(arg, ast.Call) and (
                    getattr(arg.func, "attr", None)
                    or getattr(arg.func, "id", None)) == "partial":
                partials.append(arg)
    return lambdas, names, partials


def _stdout_writing_sinks(path: Path) -> list[int]:
    """Every progress sink in one file that would write to stdout.

    `lambda line: typer.echo(f"  {line}")` — the shape this started with — reads
    as a local formatting detail and is nothing of the sort. It is handed to
    `lifecycle` as its `emit`, so the whole narrative of `go`, `up`, `down`,
    `switch`, `move`, `logs` and `create` went out on stdout through it,
    `say.Slow`'s ticks included. `say.step` is the same two spaces and the right
    stream.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lambdas, names, partials = _handed_over(tree)
    falsy = _false_names(tree)
    found: list[int] = []

    for lam in lambdas:
        if any(isinstance(call, ast.Call) and _writes_to_stdout(call, falsy)
               for call in ast.walk(lam.body)):
            found.append(lam.lineno)

    nested = {
        inner
        for outer in ast.walk(tree) if isinstance(outer, ast.FunctionDef)
        for inner in ast.walk(outer)
        if isinstance(inner, ast.FunctionDef) and inner is not outer
    }
    for node in nested:
        if node.name in names and any(
                isinstance(call, ast.Call) and _writes_to_stdout(call, falsy)
                for stmt in node.body for call in ast.walk(stmt)):
            found.append(node.lineno)

    for made in partials:
        if made.args and _writes_to_stdout(
                ast.Call(func=made.args[0], args=[], keywords=[]), falsy):
            found.append(made.lineno)

    return sorted(found)


# Every way of writing a sink that the old guard let through, kept as data so the
# test below can prove the guard still catches each one. Each was measured
# evading it before this list existed; the controls were measured passing. Ten
# shapes are caught, four controls must not be, and one row is a KNOWN LIMIT —
# a sink the guard still misses, written down so the miss is asserted rather
# than implied, and so closing it turns that row red.
SINK_SHAPES = {
    "echo without err": ("run(lambda line: typer.echo(line))", True),
    "echo with err=False": ("run(lambda line: typer.echo(line, err=False))", True),
    "print": ("run(lambda line: print(line))", True),
    "sys.stdout.write": ("run(lambda line: sys.stdout.write(line))", True),
    "say.result as a sink": ("run(lambda line: say.result(line))", True),
    "a nested def, not a lambda":
        ("def sink(line):\n        typer.echo(line)\n    run(sink)", True),
    "functools.partial": ("run(partial(typer.echo))", True),
    "err=a name bound to False":
        ("quiet = False\n    run(lambda line: typer.echo(line, err=quiet))", True),
    "secho without err": ("run(lambda line: typer.secho(line))", True),
    "secho with err=False": ("run(lambda line: typer.secho(line, err=False))", True),
    "CONTROL: err=True is correct": ("run(lambda line: typer.echo(line, err=True))", False),
    "CONTROL: secho with err=True": ("run(lambda line: typer.secho(line, err=True))", False),
    "CONTROL: err=a name bound to True":
        ("loud = True\n    run(lambda line: typer.echo(line, err=loud))", False),
    "CONTROL: say.step is the answer": ("run(say.step)", False),
    # Not a control — a limit. `_means_stderr` cannot read a value it never
    # sees bound, so this sink evades the guard, and this case says so out
    # loud. Close the limit and it goes red: that is the point of pinning it.
    "KNOWN LIMIT: err=a name bound elsewhere":
        ("run(lambda line: typer.echo(line, err=_verbosity))", False),
}


# The three numbers out of that paragraph, written down, because a parametrised
# TABLE CAN BE EMPTIED SILENTLY. Measured: deleting the three rows the secho
# commit added gives 6101 passed, nothing red — a drop of three in a total that
# moves every commit anyway, and every habit for noticing lost cover reports
# success. Same failure as the file list in test_suite_integrity.py, one level
# down: deleted cases do not fail, they stop existing.
#
# It also makes the natural regression a single act rather than two. Reverting
# `secho` from `_means_stderr` turns two rows red; the way to green is to delete
# them, and that is exactly the change this stops being free.
CATCHING_SHAPES, CONTROL_SHAPES, KNOWN_LIMITS = 10, 4, 1


def test_every_sink_shape_is_still_on_the_table():
    """The table's own contents, asserted rather than described in prose.

    Going UP is ordinary — a new evasion was found, raise the number. Going DOWN
    is the thing to look at, and the categories are counted separately because
    they fail in opposite directions: losing a catching shape narrows the guard,
    losing a control lets a rule that flags everything look green, and losing the
    limit means the guard has stopped stating its own edge.
    """
    catching = [shape for shape, (_, catch) in SINK_SHAPES.items() if catch]
    controls = [shape for shape in SINK_SHAPES if shape.startswith("CONTROL:")]
    limits = [shape for shape in SINK_SHAPES if shape.startswith("KNOWN LIMIT:")]

    assert (len(catching), len(controls), len(limits)) == (
        CATCHING_SHAPES, CONTROL_SHAPES, KNOWN_LIMITS), (
        f"the sink table holds {len(catching)} catching shapes, {len(controls)} "
        f"controls and {len(limits)} known limits, not "
        f"{CATCHING_SHAPES}/{CONTROL_SHAPES}/{KNOWN_LIMITS}. A row added is a "
        f"number to raise. A row REMOVED is cover that has gone without anything "
        f"going red — confirm it was deliberate before lowering this."
    )
    assert len(catching) + len(controls) + len(limits) == len(SINK_SHAPES), (
        "a row is in none of the three categories: a shape the guard must not "
        "catch has to say which it is, CONTROL: or KNOWN LIMIT:, or nobody "
        "reading the table can tell a deliberate blind spot from a bug."
    )


@pytest.mark.parametrize("shape", list(SINK_SHAPES), ids=list(SINK_SHAPES))
def test_the_sink_guard_can_actually_fire(shape, tmp_path):
    """The guard, held to the standard it holds the package to.

    It passed for a year while catching exactly ONE of the ten catching shapes
    below — green, specific, and unable to fail for the reason it exists. A
    guard that has never been shown to fire is a guard nobody has tested, so
    its own detection is the thing under test here, and the four controls are
    as load-bearing as the ten: a rule that flagged everything would also be
    green.

    The last row is neither. It is the one evasion left standing — an `err=`
    value bound out of sight — and it is here so the guard states its own edge
    instead of implying it. A limit asserted is a limit that tells you when it
    is gone.
    """
    source, should_catch = SINK_SHAPES[shape]
    module = tmp_path / "sample.py"
    module.write_text(f"def outer():\n    {source}\n", encoding="utf-8")

    caught = bool(_stdout_writing_sinks(module))
    assert caught is should_catch, (
        f"{shape}: guard said {caught}, expected {should_catch}"
    )


def test_no_progress_sink_writes_to_stdout():
    """A money defect, and the reason it hid: no test could see a stream.

    Fifteen of these lived in `host.py`. What they cost was not tidiness — it was
    `comfy-qat down comfy-win 1>/dev/null` silencing "still billing", "left
    running", and every `gcloud ... delete` command the tool hands over, because
    all of it was on the stream the user had just thrown away. The suite could not
    tell: fourteen files cover these commands and every one of them asserts on
    `result.output`, which merges stdout and stderr back together.

    Held over the whole package rather than the converted modules, because a sink
    is dangerous wherever it is written.
    """
    for path in sorted((ROOT / "comfy_qa").glob("*.py")):
        found = _stdout_writing_sinks(path)
        assert not found, (
            f"{path.name}:{found} — a progress sink writing to stdout. "
            f"Pass `say.step`: same indent, and the story belongs on stderr."
        )


DECLARED = (
    "[hosts.comfy-win]\n"
    "kind = 'gce'\nos = 'Windows Server 2022'\ngpu = 'L4'\n"
    "gce_instance = 'comfy-win'\ngce_zone = 'us-central1-a'\n"
    "gce_project = 'a-project'\nport = 8190\n"
)


class _RunningBox:
    """A cloud with one box that is on. Not a stand-in for `put_away`.

    That distinction is the whole of what was wrong here. The previous version
    replaced `lifecycle.put_away` with a two-line stub and then asserted on the
    streams — so the assertion was aimed at the double, and the function that
    writes the narrative never ran. Green, specific, and about nothing.

    Worse than not exercising the subject: the double had DRIFTED from it. The
    stub returned `False`, and the real `put_away` has returned one of
    "billing"/"caught"/"idle"/"unknown" since a bool was found unable to tell a
    box this stopped from a box already off. `down` looks the result up in
    `{"unknown": ..., "billing": ..., "caught": ...}.get(found, [])`, so `False`
    matched nothing and every host fell into a throwaway list. The test steered
    the command down a branch the real function can no longer produce, and the
    assertion it then made was WRONG — `stdout == ""` is false for `down --all`,
    which puts its money summary there on purpose.

    So: the real `put_away`, and only the cloud is faked.
    """

    def __init__(self, state: str = "RUNNING") -> None:
        self.state = state
        self.stopped: list[str] = []

    def instance_status(self, instance, zone, project):
        return self.state

    def stop_instance(self, instance, zone, project):
        self.stopped.append(instance)
        return ""

    def list_instances(self, project):
        return []

    def current_project(self):
        return "a-project"


def _run(argv, tmp_path, monkeypatch, cloud=None):
    from typer.testing import CliRunner

    from comfy_qa import gcloud as gcloud_module
    from comfy_qa import tunnel as tunnel_module
    from comfy_qa.cli import app

    monkeypatch.setattr(tunnel_module, "TUNNEL_DIR", tmp_path / "tunnels")
    config = tmp_path / "hosts.toml"
    config.write_text(DECLARED, encoding="utf-8")
    monkeypatch.setattr(gcloud_module, "Gcloud",
                        lambda *a, **k: cloud or _RunningBox())
    return CliRunner().invoke(app, [*argv, "--config", str(config)])


def test_the_narrative_of_a_command_that_costs_money_goes_to_stderr(tmp_path,
                                                                    monkeypatch):
    """The whole point, measured on the streams themselves rather than on both.

    `down` is the command whose entire purpose is answering "am I still paying".
    Its progress is the story and belongs on stderr; whatever it concludes is the
    answer and belongs on stdout. Before the conversion this was the wrong way
    round in full: stdout carried everything and stderr was empty.

    `--all`, because that is the form with a summary to put on stdout — and
    because the single-host form turns out to have none, which is pinned
    separately below.
    """
    cloud = _RunningBox()
    result = _run(["down", "--all"], tmp_path, monkeypatch, cloud)

    assert result.exit_code == 0, result.output
    assert cloud.stopped == ["comfy-win"], (
        "the real put_away did not run — if this ever passes with an empty list, "
        "the subject has been replaced by a double again"
    )

    # The story: the sentence the REAL put_away writes, not one a stub was told
    # to say.
    assert "was running — stopped it" in result.stderr, result.stderr

    # The answer, and the half the stubbed version could not see at all.
    assert "was billing" in result.stdout, (
        f"the money summary is the answer and belongs on stdout: "
        f"{result.stdout!r}"
    )
    assert "was running — stopped it" not in result.stdout, (
        "the story leaked onto stdout: a person who redirected it away would "
        "lose the progress of a command about money"
    )


# Every money command drivable offline with the fakes in this file. The LIST is
# the point, not its length: this test stated a rule about "a money command" and
# drove exactly ONE — `down --all` — while its own docstring claimed it caught
# both D78 (`move --dry-run`, everything on stdout) and D78's mirror
# (`down <name>`, everything on stderr), and it exercised NEITHER of them. A rule
# stated as a class and enforced on one remembered instance is a hand-maintained
# list wearing an invariant's clothes.
#
# `up`, `go`, `switch`, `create` and `logs` need richer fakes than this file has.
# When they get them they belong HERE, as rows, rather than as separate tests: a
# missing row is visible in a way a missing test is not.
#
# Found and built by triage, adopted whole.
STREAM_RULE_CASES = [
    (["down", "--all"], "running", "down --all"),
    (["down", "comfy-win"], "running", "down <name>"),
    (["disconnect", "comfy-win"], "running", "disconnect <name>"),
    (["move", "comfy-win", "--to", "us-central1-b", "--dry-run"],
     "half-finished move", "move --dry-run"),
]


@pytest.mark.parametrize("argv,cloud,label", STREAM_RULE_CASES,
                         ids=[case[2] for case in STREAM_RULE_CASES])
def test_every_money_command_says_something_on_each_stream(argv, cloud, label,
                                                           tmp_path, monkeypatch):
    """Neither stream may be empty, which is the shape both defects take.

    D78 is `move --dry-run` with EVERYTHING on stdout and stderr empty; its
    mirror is `down <name>` with everything on stderr and stdout empty. One
    invariant catches both, and it is the one the vocabulary already states:
    stdout carries the answer, stderr carries the story, and a command about
    money has both.

    Deliberately says nothing about WHICH words. The two tests below assert the
    specific text for the commands whose exact wording has been wrong; this says
    the thing that must hold for all of them, including the ones nobody has
    hand-written a test for yet.
    """
    fake = _HalfFinishedMove() if cloud == "half-finished move" else _RunningBox()
    result = _run(argv, tmp_path, monkeypatch, fake)

    assert result.stdout.strip(), (
        f"{label}: no answer on stdout — `1>/dev/null` loses everything"
    )
    assert result.stderr.strip(), (
        f"{label}: no story on stderr — `2>/dev/null` loses everything"
    )


def test_the_single_host_down_also_answers_on_stdout(tmp_path, monkeypatch):
    """`down --all` puts its money summary on stdout. `down <name>` puts nothing
    there, so the two forms of one command disagree about where the answer goes.

    Found by making the guard above drive the real `put_away` — which is the
    point of unstubbing it, and it turned up on the first run.

    Was a strict xfail while host.py belonged to another agent. Fixed there: the
    single-host form now ends with the same money summary its `--all` sibling
    prints, read off `put_away`'s verdict, while the per-host story stays on
    stderr. The marker came off with the fix, which is what its reason asked for.
    """
    result = _run(["down", "comfy-win"], tmp_path, monkeypatch)

    assert result.exit_code == 0, result.output
    assert result.stdout.strip(), (
        f"`down comfy-win 2>/dev/null` says nothing about money: "
        f"{result.stdout!r}"
    )
    # Specific, not merely non-empty: "something on stdout" is satisfied by any
    # stray line, and this test exists because the ANSWER was missing. It also
    # holds the story to its own stream, the way the `--all` guard above does.
    assert "was billing. Stopped." in result.stdout, (
        f"stdout has something, but not the answer about money: {result.stdout!r}"
    )
    assert "was running — stopped it" not in result.stdout, (
        "the per-host story leaked onto stdout"
    )


# The same invariant, pointed at the case that motivated all of this. D78 is
# `move --dry-run` writing its whole leftovers block — heading, each resource,
# and three `gcloud … --quiet` delete commands — to stdout, with stderr empty.
# `2>/dev/null` loses nothing; `1>/dev/null` loses everything, in the command
# that hands over the most delete commands in the tool.
#
# Verified by hand at HEAD before this was written: exit 0, stderr EMPTY,
# `--quiet` present in stdout.
#
# FIXED IN d70e6b1 while this was being written, by moving the block to
# `say.warn`. This went in as a strict xfail and turned red on the first run
# against the new HEAD — which is the mechanism working: the notice arrived
# without anyone having to remember to look. Kept as a live guard rather than
# deleted, because the fix is one word (`warn` back to `result`) and nothing
# else in the suite reads the streams of this command.

_PROJECT_URL = "https://www.googleapis.com/compute/v1/projects/a-project"


class _HalfFinishedMove:
    """A project carrying what an earlier, interrupted move left behind."""

    def _disk(self, name, zone, kind, **extra):
        return {"name": name, "zone": f"{_PROJECT_URL}/zones/{zone}",
                "sizeGb": "300", "status": "READY",
                "type": f"{_PROJECT_URL}/zones/{zone}/diskTypes/{kind}",
                "creationTimestamp": "2026-08-25T07:38:24.167-07:00", **extra}

    def run(self, args, **kwargs):
        joined = " ".join(str(part) for part in args)
        if "disks list" in joined:
            return [
                self._disk("comfy-win-a", "us-central1-a", "pd-balanced",
                           users=[f"{_PROJECT_URL}/zones/us-central1-a/instances/comfy-win"]),
                self._disk("comfy-win-a-b", "us-central1-b", "pd-standard",
                           sourceSnapshot=f"{_PROJECT_URL}/global/snapshots/comfy-win-a-move"),
            ]
        if "snapshots list" in joined:
            return [{"name": "comfy-win-a-move", "diskSizeGb": "300",
                     "sourceDisk": f"{_PROJECT_URL}/zones/us-central1-a/disks/comfy-win-a",
                     "storageBytes": "22475608320", "status": "READY",
                     "creationTimestamp": "2026-08-25T07:33:34.373-07:00"}]
        if "machine-types list" in joined:
            return [{"name": "g2-standard-8"}]
        if "regions describe" in joined:
            return None
        if "instances describe" in joined:
            return self.describe_instance("comfy-win", "us-central1-a", "a-project")
        raise AssertionError(f"unexpected gcloud call: {joined}")

    def describe_instance(self, instance, zone, project):
        return {"name": instance, "status": "TERMINATED",
                "zone": f"{_PROJECT_URL}/zones/us-central1-a",
                "machineType": f"{_PROJECT_URL}/zones/us-central1-a/machineTypes/g2-standard-8",
                "disks": [{"boot": True,
                           "source": f"{_PROJECT_URL}/zones/us-central1-a/disks/comfy-win-a"}]}

    def list_instances(self, project):
        return []

    def region_quotas(self, project, region):
        return {}

    def current_project(self):
        return "a-project"


def test_a_dry_run_that_hands_over_delete_commands_says_something_on_stderr(
        tmp_path, monkeypatch):
    result = _run(["move", "comfy-win", "--to", "us-central1-b", "--dry-run"],
                  tmp_path, monkeypatch, _HalfFinishedMove())

    assert result.exit_code == 0, result.output
    both = result.stdout + result.stderr
    assert "an earlier run left this behind" in both, (
        "fixture drifted — this test is about WHERE the leftovers block goes, "
        "so it is worth nothing if the block is not printed at all"
    )

    # Named by stream AND by command. The first draft of this asserted only
    # `result.stderr.strip()` and passed with the defect three-quarters
    # restored: the fix moved several lines, and putting ONE back left enough on
    # stderr to satisfy it. "Something was on stderr" is not the property.
    assert "an earlier run left this behind" in result.stderr
    for line in ("disks delete comfy-win-a-b", "snapshots delete comfy-win-a-move"):
        assert line in result.stderr, f"{line} is not on stderr: {result.stderr!r}"
    assert "an earlier run left this behind" not in result.stdout
    assert "snapshots delete comfy-win-a-move" not in result.stdout, (
        f"a `gcloud … --quiet` delete command is on stdout, so `1>/dev/null` "
        f"loses it: {result.stdout!r}"
    )


def test_no_delete_command_reaches_stdout_from_a_dry_run(tmp_path, monkeypatch):
    """The whole-command version of the assertion above.

    Kept separate, and kept honest: the leftovers block IS fixed, and this is the
    one line left. Rolling the two together would hide which half is which —
    which is exactly how the first draft of the test above came to pass for the
    wrong reason.

    That last line is now fixed too, and the marker came off with it. The note
    was the arguable one — a note could be read as part of the plan, and the plan
    is the answer — so it was decided on what the notes actually are rather than
    on the invariant: `judge_disk` builds them with `output.fix`, and the only
    one that exists is a caveat plus a `--quiet` delete command for avoiding it.
    That is advice, not a step the move takes.
    """
    result = _run(["move", "comfy-win", "--to", "us-central1-b", "--dry-run"],
                  tmp_path, monkeypatch, _HalfFinishedMove())

    assert result.exit_code == 0, result.output
    assert "--quiet" not in result.stdout, (
        f"{result.stdout.count('--quiet')} delete command(s) on stdout: "
        f"{result.stdout!r}"
    )
