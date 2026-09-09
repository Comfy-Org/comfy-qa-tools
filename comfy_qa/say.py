"""Everything this tool says out loud, in one place.

There was no such place. Output was two hundred `typer.echo` calls spread over
six modules, and they disagreed about nearly everything: `!!` in one table and
`FAIL` in another, `WARNING ` beside `warning: `, the same progress line on
stderr and its result on stdout in one command, and the literal
`"\\n        or stop paying for it:\\n        "` hand-copied ten times, trailing
whitespace included. None of that is visible while writing one line; all of it is
visible when someone pastes a whole run into Slack.

So: one vocabulary, seven kinds, each with one rendering and one stream.

    result   what the command was asked to produce      stdout
    check    one pass/fail row of a report              stdout, `ok  ` / `fail`
    step     a phase of a long operation starting       stderr, indented 2
    detail   a fact under the step it belongs to        stderr, indented 4
    warn     worth knowing, not fatal                   stderr, `warning: `
    error    what went wrong, and what to do            stderr, plus `to fix: `
    fail     the same, then stop                        stderr, plus an exit code

The stream rule in one sentence: **stdout carries the answer, stderr carries the
story**. `comfy-qat env --json | jq` never sees a progress line, and
`comfy-qat go 2>&1 | tee run.log` never loses one.

What is deliberately absent from these seven writers: spinners, progress bars,
cursor movement, colour. This tool's output is read twice — once in a terminal
and once in a Slack code block — and everything that redraws survives only the
first reading. A long step says so by printing another whole line, which is the
one shape that pastes.

Read that as being about `say`, because that is all it was ever true of. Without
a subject it read as a claim about the tool, and the tool does emit colour:
`comfy-qat --help` is rendered by Typer through Rich, and in a terminal it comes
out in several hundred SGR escapes. Two reasons that is a caveat rather than a
defect, both worth writing down because neither is obvious.

Rich emits them only when stdout is a terminal. Redirect the help and there is
not one escape in it, so they cannot reach the Slack paste this paragraph is
about — and colour does not redraw, which is the property the paragraph is
actually about. And there is no single seam to turn it off at:
`rich_markup_mode=None` on every `typer.Typer()` in the package would take the
help layout with it and the next sub-app added would silently opt back in, and
the alternative is assigning to a module-level constant inside Typer, where the
way you learn that an upgrade renamed it is that colour comes back with nothing
said.

`tests/test_no_colour.py` holds down all three: that `say` writes no escape even
to a terminal, that Typer's help still writes some, and that a redirect gets
none. If Typer ever stops, the second goes red and this caveat can go with it.

Exit codes, so the fourteen places that used to pick their own agree:

    1   the thing you asked for did not happen
    2   the command could not start — bad input, or a precondition unmet
    75  submitted, not finished; running it again resumes (EX_TEMPFAIL)
"""

from __future__ import annotations

import re
import threading
import time
from typing import Callable, NoReturn

import typer

# `to fix: ` is eight characters, so a fix's later lines are indented eight to
# sit under the first one. That number is the reason the literal being copied
# around was `"\n        "` — it was this alignment, written out by hand.
FIX_LABEL = "to fix: "
FIX_INDENT = " " * len(FIX_LABEL)

WARNING = "warning: "

# Four characters each, same case, so a column of them lines up and nothing has
# to be padded at the call site. Three spellings were in the output at once:
# `ok  `/`FAIL` in one column, `FAIL `/`OK   ` in another, `!!` in a third.
OK_MARK = "ok  "
FAIL_MARK = "fail"

STEP_INDENT = "  "
DETAIL_INDENT = "    "

# How often a long step says it is still going. A terminal gets a line every
# half-minute; a pipe gets a quarter as many, because nobody is watching a log
# file in real time and the file has to stay readable afterwards.
TICK_SECONDS = 30
PIPED_TICK_SECONDS = 120

# The first tick does not wait for the full interval. See `Slow` for why twenty
# seconds and why it then doubles rather than staying there.
FIRST_TICK_SECONDS = 20
TICK_BACKOFF = 2

# Below this, a step is not slow enough to be worth timing.
WORTH_TIMING_SECONDS = 5


# --- who is listening ------------------------------------------------------


def watching() -> bool:
    """Is a person looking at this as it happens?

    Deliberately `gcloud.can_prompt` and not a second rule of our own: it already
    encodes the one this tool has to live with — stdin *and* stderr both have to
    be terminals — and two TTY tests in one program is how output ends up
    disagreeing with itself about whether anyone is there. Imported inside the
    function so this module stays importable from anywhere in the package.
    """
    from .gcloud import can_prompt

    return can_prompt()


# --- the kinds -------------------------------------------------------------


def result(text: str = "") -> None:
    """The answer the command was run for. The only thing on stdout."""
    typer.echo(text)


def check(ok: bool, text: str) -> None:
    """One row of a pass/fail report — `comfy-qat status`, `env --expect`.

    On stdout: a readiness report is the answer to the question that was asked,
    not commentary on it. The exit code says the same thing to a machine.
    """
    typer.echo(f"{OK_MARK if ok else FAIL_MARK}  {text}")


def step(text: str) -> None:
    """A phase of a long operation, starting now."""
    typer.echo(f"{STEP_INDENT}{text}", err=True)


def detail(text: str) -> None:
    """A fact belonging to the step above it."""
    typer.echo(f"{DETAIL_INDENT}{text}", err=True)


def warn(text: str) -> None:
    """True, worth saying, and not a reason to stop.

    A leading blank line is a caller asking for air above the warning, not for
    the label to be left on a line by itself. Concatenated, it was the second
    thing — `comfy-qat discover --prune` printed

        warning:
        the project listing did not have these and a direct check did not ...

    with the word that says how to read the sentence stranded a line above it.
    The blank lines come out first and the label stays welded to its text.
    """
    body = str(text).lstrip("\n")
    for _ in range(len(str(text)) - len(body)):
        typer.echo("", err=True)
    typer.echo(f"{WARNING}{body}", err=True)


def error(problem: object, fix: str | None = None, *, blank_line: bool = True) -> None:
    """What went wrong, then what to do about it. Never one without the other.

    `problem` may be an exception; its own `.fix` is used unless one is passed.
    That is the whole of the shape that used to be written out at fourteen call
    sites as echo, echo, Exit — with two different streams and two different
    exit codes between them.
    """
    if blank_line:
        typer.echo("", err=True)
    typer.echo(str(problem), err=True)
    fix = fix if fix is not None else getattr(problem, "fix", None)
    if fix:
        write_fix(fix)


def fail(problem: object, fix: str | None = None, *, code: int = 1,
         blank_line: bool = True) -> NoReturn:
    """`error`, and then stop. See the module docstring for what each code means."""
    error(problem, fix, blank_line=blank_line)
    raise typer.Exit(code=code)


# --- fixes -----------------------------------------------------------------


def write_fix(fix: str) -> None:
    """Print a fix under its label, however it was indented on the way in.

    Each line is stripped and re-indented, so a fix composed by `say.fix` and one
    that still carries the old hand-written indent render identically. That is
    what lets the modules convert one at a time.
    """
    lines = _align_comments(
        [line.strip() for line in str(fix).splitlines() if line.strip()])
    if not lines:
        return
    typer.echo(f"{FIX_LABEL}{lines[0]}", err=True)
    for index, line in enumerate(lines[1:], start=1):
        if _opens_a_group(lines, index):
            typer.echo("", err=True)
        typer.echo(f"{FIX_INDENT}{line}", err=True)


# Below this a fix has no groups to show, so a blank line is only air. Two
# commands, or a command and the sentence about it, are already legible.
GROUPED_FIX_LINES = 3


def _opens_a_group(lines: list[str], index: int) -> bool:
    """Does a blank line belong above `lines[index]`?

    The block someone reads after a Ctrl-C on a billing box was eight lines
    flush against each other — four runnable commands, one sentence introducing
    two of them, and one about what a snapshot costs, all at the same indent
    and all reading as things to paste:

        to fix: gcloud compute snapshots delete comfy-linux-snap-20260909
                comfy-qat down comfy-win
                or check first, if you would rather look:
                comfy-qat list --live
                a snapshot costs pennies a month, but it is not free

    Prose interleaved with commands is what a fix looks like whenever more than
    one thing is offered, and the eight-space indent that holds the block
    together is also what flattens it. A blank line before each sentence puts
    the commands into the groups they were written in, and the indent still says
    they are one block.

    Only where there is grouping to show, and only for a sentence that FOLLOWS a
    command — a fix that opens with prose and lists commands under it is already
    one group, and the label does that job.
    """
    if len(lines) < GROUPED_FIX_LINES:
        return False
    return _is_prose(lines[index]) and not _is_prose(lines[index - 1])


# What a runnable line in a fix starts with. Everything this tool offers is one
# of its own commands, a gcloud call, or an absolute path to an interpreter —
# walked out of every `say.fix` in the package rather than guessed.
_RUNNABLE = ("comfy-qat ", "gcloud ", "ssh ", "scp ", "curl ", "python", "pip",
             "sudo ", "/", "~/")


def _is_prose(line: str) -> bool:
    """A sentence rather than something to paste.

    One test, in one place, so the blank lines and the `#` columns agree about
    where a group ends. Two ways of being prose, and both are needed:

    Punctuation first, because it is the reliable half — a line that ends in a
    full stop or in the colon that introduces the commands under it is a
    sentence whatever it starts with, which is what keeps `open a terminal
    window and run:` out of the runnable set.

    Then the opening word, for the case punctuation cannot reach: a trailing
    note like `a snapshot costs pennies a month, but it is not free` ends in no
    punctuation at all and is the line most likely to be pasted by mistake,
    being the last one under `to fix:`.

    The cost of the second half is a command starting with a word not in
    `_RUNNABLE` gaining a blank line above it. That is one line of air on a
    block that is already grouped, which is why the list is allowed to be short
    rather than exhaustive.
    """
    if _COMMENTED.match(line):
        return False
    if line.endswith((":", ".")):
        return True
    return not line.startswith(_RUNNABLE)


# A command with a trailing `#` note, split at the run of spaces between them.
# Two spaces at least, so a `#` inside a URL fragment or a shell word is not one.
_COMMENTED = re.compile(r"^(?P<command>\S.*?)\s{2,}(?P<note>#.*)$")


def _align_comments(lines: list[str]) -> list[str]:
    """Put the `#` notes in a run of commands into one column.

    The gap between a command and its note used to be typed at the call site,
    which means it was counted by eye and got it wrong. Three commands offered
    together came out as

        comfy-qat open comfy-win   # tunnel to a box that is already running
        comfy-qat go comfy-win     # start it and tunnel, in one step
        comfy-qat list --live        # which of the two it is

    — two columns out on the last one, in the block someone reads when nothing
    is working. Alignment is a property of the whole block and only the printer
    sees the whole block, so it belongs here and not in the twenty places that
    compose one.

    A RUN, not the file: the notes line up with the commands they are offered
    beside, and a prose line between two groups ends one run and starts another,
    because a shared column across a paragraph break lines nothing up with
    anything. Lines with no note are still part of the run — they set the column
    without taking one.
    """
    out = list(lines)
    run: list[int] = []

    def flush() -> None:
        commented = [i for i in run if _COMMENTED.match(out[i])]
        if not commented:
            return
        # Every line in the run, noted or not, argues for the column: a long
        # bare command with a short noted one under it would otherwise have the
        # note start left of where the command above it ended.
        width = max(len(_COMMENTED.match(out[i])["command"]) if _COMMENTED.match(out[i])
                    else len(out[i]) for i in run)
        for i in commented:
            parts = _COMMENTED.match(out[i])
            out[i] = f"{parts['command'].ljust(width)}   {parts['note']}"

    for index, line in enumerate(out):
        # A sentence is not a command, so it closes the run rather than joining
        # it. `_COMMENTED` cannot tell them apart — prose rarely carries a `#` —
        # so the test is the one thing a fix's prose reliably does: it reads as
        # a sentence, and ends in a full stop or in the colon that introduces
        # the commands under it.
        if _is_prose(line):
            flush()
            run = []
            continue
        run.append(index)
    flush()
    return out


def fix(*lines: str) -> str:
    """Compose a multi-line fix that aligns under `to fix: ` wherever it lands.

    The alignment is baked into the string rather than applied at print time
    because a fix travels on an exception and gets printed by whoever catches it,
    including code that has not been converted yet. One implementation, and every
    caller stops counting spaces.
    """
    parts: list[str] = []
    for line in lines:
        if not line:
            continue
        parts += [piece.strip() for piece in str(line).splitlines() if piece.strip()]
    return ("\n" + FIX_INDENT).join(parts)


# --- tables ----------------------------------------------------------------

# Two spaces between columns. Wide enough that the eye reads a gap rather than a
# word break, narrow enough that six columns of host still fit a laptop window.
COLUMN_GAP = "  "


def rows(cells: list[tuple[str, ...]]) -> list[str]:
    """Pad a table so its columns line up. Returns the lines; does not print.

    `list` had this written out inline and `discover` did not, so the same five
    facts about the same machine came out as a padded table under one command
    and as a ragged join under the other:

        comfy-linux  Ubuntu 22.04  L4  running  port 8192
        comfy-win-2  Windows Server 2022  L4  stopped  port 8193

    Nothing lines up, so nothing can be compared down the page — which is the
    only reason to put machines in rows at all.

    Returned rather than printed because the two callers are on different
    streams' worth of context: one is the answer to `list` and one sits inside a
    longer report. Padding is a shape, and the stream is the caller's decision.

    Trailing whitespace is stripped: the last column pads to nothing, and a
    trailing run of spaces is invisible in a terminal and very visible in a diff
    of a pasted log.
    """
    if not cells:
        return []
    width = [max(len(row[i]) for row in cells) for i in range(len(cells[0]))]
    return [COLUMN_GAP.join(cell.ljust(width[i]) for i, cell in enumerate(row)).rstrip()
            for row in cells]


# --- counting --------------------------------------------------------------


def count(number: int, singular: str, plural: str | None = None) -> str:
    """`1 machine`, `3 machines`. Written once so `machine(s)` can stop.

    `box(es)`, `card(s)`, `machine(s)` and `cloud box(es)` were all in the output
    at the same time, and every one of them reads as a form to fill in.
    """
    if number == 1:
        return f"{number} {singular}"
    return f"{number} {plural or singular + 's'}"


# --- time ------------------------------------------------------------------


def elapsed(seconds: float) -> str:
    """`45s`, `2m38s`, `1h04m`. Short enough to sit at the end of a line."""
    whole = int(max(0.0, seconds))
    if whole < 60:
        return f"{whole}s"
    minutes, remainder = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m{remainder:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


class Slow:
    """A step that can take minutes, saying so the whole time it runs.

    The failure this exists to stop: `move` prints "snapshotting the disk" and
    then nothing for four minutes, and a slow step and a hung one look exactly
    alike. So a line goes out every `every` seconds carrying the elapsed time,
    and a step that took long enough to notice says how long it took on the way
    out. A step that finishes quickly stays one line.

    Every line is a whole line — no redraw, nothing to erase — so a run of this
    pastes into Slack exactly as it appeared in the terminal.

    The ticks BACK OFF: the first comes after `FIRST_TICK_SECONDS`, and each
    interval after it doubles until it reaches `every`, which stays the cadence
    for the rest of the step. Two pressures pull opposite ways and a single
    interval can only satisfy one of them.

    Sooner, because the moment that matters is the FIRST one. `go`'s install
    streams a few hundred lines of apt and pip and then goes quiet while torch,
    torchvision, torchaudio and the CUDA runtime download with no progress lines
    of their own. That silence is where somebody decides it has hung and presses
    Ctrl-C on a box that is running and billing, and at `every` = 60 the first
    sign of life was a full minute after the output stopped. Twenty seconds is
    long enough to still read as a lull and short enough that nobody has
    concluded anything yet.

    Rarer, because every tick is a line someone scrolls past when this run is
    pasted into a Slack code block — the second reading the module docstring is
    about. A five-second tick over a several-minute install is dozens of lines
    and becomes the noise a reader learns to skip, which is worse than the
    silence it replaced.

    Doubling settles that without a third number to tune. At `every` = 60 the
    intervals are 20, 40, then 60 forever, so the ticks land at 0m20s, 1m00s,
    2m00s, 3m00s — from the second one onward, EXACTLY where the flat cadence
    already put them. An eight-minute install pays one extra line for a first
    sign of life three times sooner, and no run pays more than two.

    `emit` is where the lines go. It defaults to this module's own writers; the
    lifecycle functions pass the `say` they were handed, so their progress keeps
    coming out wherever their caller put it.
    """

    def __init__(
        self,
        text: str,
        *,
        expect: str | None = None,
        emit: Callable[[str], None] | None = None,
        every: float | None = None,
        clock: Callable[[], float] | None = None,
        background: bool = True,
    ) -> None:
        self.text = text
        self.expect = expect
        self._emit = emit
        self._clock = clock or time.monotonic
        self._every = every if every is not None else (
            TICK_SECONDS if watching() else PIPED_TICK_SECONDS)
        self._background = background
        # The ceiling is `_every`; `_interval` is what the next tick actually
        # waits for, and it climbs to the ceiling. `min` so a step configured to
        # tick faster than the first interval is not slowed down by it.
        self._interval = min(FIRST_TICK_SECONDS, self._every)
        self._started = 0.0
        self._last_tick = 0.0
        self._ticked = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # The two writers, so a caller that supplied `emit` gets both lines through
    # it and one that did not gets the module's own indents.
    def _headline(self, text: str) -> None:
        (self._emit or step)(text)

    def _under(self, text: str) -> None:
        if self._emit is None:
            detail(text)
        else:
            self._emit(f"{STEP_INDENT}{text}")

    @property
    def seconds(self) -> float:
        return self._clock() - self._started

    def start(self) -> "Slow":
        self._started = self._last_tick = self._clock()
        self._headline(f"{self.text} ({self.expect})" if self.expect else self.text)
        if self._background and self._every > 0:
            self._thread = threading.Thread(target=self._pulse, daemon=True)
            self._thread.start()
        return self

    def _pulse(self) -> None:
        # Wakes often, prints rarely: `tick` decides, so the interval is one
        # rule rather than two, and a test can drive it without a thread.
        while not self._stop.wait(1.0):
            self.tick()

    def tick(self) -> None:
        """Say we are still here, if it has been long enough. Safe to call often."""
        if self._every <= 0:
            return
        now = self._clock()
        if now - self._last_tick < self._interval:
            return
        self._last_tick = now
        self._ticked = True
        self._interval = min(self._interval * TICK_BACKOFF, self._every)
        self._under(f"still going, {elapsed(now - self._started)}")

    def done(self, note: str | None = None) -> None:
        """Close the step. Silent unless it took long enough to be worth a line."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        taken = self.seconds
        if self._ticked or taken >= WORTH_TIMING_SECONDS:
            self._under(f"{note or 'done'} in {elapsed(taken)}")

    def give_up(self) -> None:
        """Stop the ticking without claiming anything finished."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None

    def __enter__(self) -> "Slow":
        return self.start()

    def __exit__(self, kind, value, traceback) -> None:
        # A step that raised has a failure to report and does not need a second
        # line saying how long it took to get there.
        if kind is None:
            self.done()
        else:
            self.give_up()


def slow(text: str, **kwargs) -> Slow:
    """`with say.slow("snapshotting the disk", expect="2-5 minutes"): ...`"""
    return Slow(text, **kwargs)
