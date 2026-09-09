"""What a mutating call might leave behind, recorded while it is still in flight.

Ctrl-C during a create, a start or a move does not undo the work. What the
interrupt reaches is the local `gcloud` process; the request it made has already
gone, and Compute Engine builds the instance server-side whether or not the
client that asked for it is still alive. Killing the client is not cancelling
the call.

**Measured, because the earlier account of this was wrong and the wrong account
is the more comfortable one.** `Gcloud.run` — the path every mutating call takes
— uses `subprocess.run`, which on a KeyboardInterrupt kills the child and
re-raises, returning nothing. Against a replica with a local child standing in
for gcloud: the interrupt reaches the caller at 0.41s, the child is killed, and
no exit code reaches the handler. The double `process.wait()` in `relay_output`
is real, but `relay_output` is only reached by `Gcloud.ssh` — it streams an
install log, it creates nothing. So the tool does **not** learn from the
interrupt whether the create succeeded, and a message that claims it did would
be inventing certainty. "may" is the honest word; what makes it actionable is
naming the exact resource and the exact command that removes it.

The shape, and why it is not four `try`/`except KeyboardInterrupt` blocks in
`host.py`: those drift, and they cannot cover a fifth command written next month.
They also sit at the wrong altitude — `create_cmd` knows the name of the box but
not which of six zones `build` was in when the interrupt landed, so its message
named the first zone and would have been wrong every time a stockout pushed the
create further down the list.

So the registration sits at the call site, where the facts are, and the
reporting sits in `cli.main`, where the process ends:

    with inflight.may_leave(f"the instance {name} in {zone}", undo=[...]):
        create_in(...)

Cleared when the call returns, and cleared when it raises an ordinary
`Exception` — a failure the command reports in its own words is not a leftover
this has anything to add to. Kept only when a `BaseException` unwinds it, which
in practice means Ctrl-C.
"""

from __future__ import annotations

import signal
import threading
from contextlib import contextmanager
from dataclasses import dataclass

import typer


# An interrupt is not a failure, and 1 is the code a failure uses. 130 is what a
# shell reports for a process killed by SIGINT — 128 + signal.SIGINT, which is 2.
# Written as the arithmetic so the number is derived rather than remembered.
INTERRUPTED = 128 + signal.SIGINT


class Interrupted(typer.Exit):
    """Ctrl-C, carried as an exit rather than as an exception.

    THE PREVIOUS VERSION OF THIS DOCSTRING WAS WRONG, and the correction is the
    reason for the current shape. It said Click catches `KeyboardInterrupt`,
    prints `Aborted!` and exits 1. Click does do that — but Typer overrides
    Click's `main`, and typer/core.py:203-204 gets there first:

        except KeyboardInterrupt as e:
            raise _click.exceptions.Exit(130) from e

    So an interrupt anywhere in this tool already exits 130, silently, and
    `Aborted!` is never printed for one. What is missing is not the exit code —
    it is that nobody says what the interrupted call may have left running.

    `Exit`, and not a `BaseException` of our own, because the BaseException was
    STRICTLY WORSE THAN NOTHING in the one configuration it was supposed to
    survive. `cli.register()` attaches this surface to somebody else's Typer
    app, and there `main` is not in the path: a BaseException Typer does not
    name escaped unhandled, so the host got a raw traceback, `report()` never
    ran, and a GPU box that may be billing went unnamed WHILE THE RECORD THAT
    WOULD HAVE NAMED IT WAS STILL POPULATED. Measured. Without it, Typer would
    have caught the plain KeyboardInterrupt and exited quietly — so the
    mechanism built to make an interrupt safe made it worse.

    `Exit` is handled by Typer's own `_main` in every embedding, which is what
    makes this work under `register()`, under `CliRunner`, and under `main`
    alike. Reporting therefore happens where the record is — in `may_leave` —
    rather than in a handler that only one entry point reaches.

    **`typer.Exit`, NOT `click.exceptions.Exit`, and they are not the same
    class.** Typer 0.27.1 vendors its own copy of click: `typer.Exit` is
    `typer._click.exceptions.Exit`, and `typer/core.py` catches that one.
    Subclassing the click on PATH produces an exception Typer's handler does not
    match, which propagates out of `app()` untouched — the exact failure this
    class exists to prevent, reintroduced by importing the obvious name. Caught
    by a test that drove the real entry point rather than by reading.
    """

    def __init__(self) -> None:
        super().__init__(code=INTERRUPTED)


@dataclass(frozen=True)
class Leftover:
    """One thing a call in flight may have brought into existence.

    `what` is a noun phrase — it is printed under "this may exist and be
    billing", so it reads as a thing and not as a sentence. `undo` is commands,
    in the order they should be run. `note` is the one line of context that is
    not a command, and it is last because everything above it is runnable.
    """

    what: str
    undo: tuple[str, ...] = ()
    note: str = ""
    heading: str = "this may exist and be billing:"
    """The sentence this leftover is listed under.

    A field rather than a `billing: bool` the centre interprets, and the reason
    is the argument this module's own docstring already makes about
    registration: the call site knows and the centre does not. A boolean let
    `inflight` decide what two call sites were allowed to say, and a third case
    then did not fit either sentence — an interrupted `down`, where the box
    certainly exists (so "may exist" is false in its first half) and the stop
    request may or may not have landed (so "had already happened" asserts the
    one thing nobody can say).

    Grouped in first-seen order by `report`, so the create case keeps its
    wording and its position with no call site passing anything.
    """


_lock = threading.Lock()
_entries: list[Leftover] = []


def pending() -> list[Leftover]:
    """What is registered right now, oldest first."""
    with _lock:
        return list(_entries)


def clear() -> None:
    """Forget everything. Used after reporting, and by tests between runs."""
    with _lock:
        _entries.clear()


def _drop(entry: Leftover) -> None:
    with _lock:
        for index in range(len(_entries) - 1, -1, -1):
            if _entries[index] is entry:
                del _entries[index]
                return


@contextmanager
def may_leave(what: str, *, undo=(), note: str = "", heading: str | None = None):
    """Register what this call may leave behind, for as long as it is in flight.

    Nested registrations are fine and are the normal case for `move`, which
    makes a snapshot, then a disk, then an instance: each is registered before
    the call that creates it and dropped when that call returns, so at any
    moment the record holds exactly what exists and is not yet accounted for.
    """
    entry = (Leftover(what, tuple(undo), note) if heading is None
             else Leftover(what, tuple(undo), note, heading))
    with _lock:
        _entries.append(entry)
    try:
        yield entry
    except KeyboardInterrupt as exc:
        # Reported HERE, not in `cli.main`. This is the only frame guaranteed to
        # be on the stack for every entry point — `main`, `CliRunner`, and a host
        # app that took this surface through `register()`. Putting the report in
        # `main` meant the one embedding that could not reach it was the one
        # where the escape did the most damage.
        #
        # `report` clears, and an outer registration's `except Exception` below
        # will then drop its own entry against an empty record, so nesting
        # reports once and only once.
        _report_safely()
        raise Interrupted() from exc
    except Interrupted:
        # Already reported by the registration that raised it. Named ahead of
        # `Exception` — which it now is, being an `Exit` — so it reads as
        # deliberate rather than as an ordinary failure falling through.
        raise
    except Exception:
        # The command has its own words for a failure it can name. Two reports
        # for one event is how the leftovers block ended up printed twice.
        _drop(entry)
        raise
    else:
        _drop(entry)


def _listed(items: list[Leftover]) -> list[str]:
    """One indented line each. `what` may carry several, which is what lets
    `move` hand over the whole of `_state_after` — the snapshot, the disk and
    the instance — as one registration instead of three that fall out of step."""
    return [f"  {line}" for item in items for line in item.what.splitlines()]


HEADLINE = ("interrupted — Ctrl-C stops this tool, it does not cancel a request "
            "Google has already accepted")

# The headline as it is printed. Named because `report` compares against it to
# decide where a group break goes, and re-spelling `f"{HEADLINE}."` at the
# comparison is how the two come to disagree about a full stop.
HEADLINE_LINE = f"{HEADLINE}."


def _report_safely() -> None:
    """`report`, with a second Ctrl-C unable to swallow the first one's message.

    Measured: an interrupt arriving inside `say.error` truncated the output
    mid-word — at *'it does not canc'* — losing everything after it INCLUDING
    the `to fix:` block with the stop command. A narrow window, and the cost is
    a message that stops rather than one that is wrong, but the whole purpose of
    this frame is to get that block onto the screen.

    A second interrupt still stops the tool. It just does not take the sentence
    with it.
    """
    try:
        report()
    except KeyboardInterrupt:
        try:
            report()
        except BaseException:
            pass


def report() -> bool:
    """Say what is still registered, and how to undo it. True if anything was.

    Deliberately says "may". At the moment of the interrupt nothing has read the
    project back, and a read here would be a gcloud call at the one moment the
    user has just said stop — so this states the possibility precisely and hands
    over the command that settles it.
    """
    from . import say

    left = pending()
    if not left:
        return False

    lines = [HEADLINE_LINE]
    # INNERMOST FIRST, which is last-registered first. Not a taste: the
    # registration nearest the interrupt is the call that was actually in
    # flight, and on `switch` that is the box being started — the half that
    # costs money while somebody reads. First-seen order put "already stopped"
    # above it, which leads with the loss that has already finished happening.
    order: list[str] = []
    grouped: dict[str, list[Leftover]] = {}
    for item in reversed(left):
        if item.heading not in grouped:
            order.append(item.heading)
            grouped[item.heading] = []
        grouped[item.heading].append(item)
    # ONE ordering, used for both halves. The headings were grouped innermost-
    # first and the commands were a flat walk of `left`, which is registration
    # order — the exact reverse. So on `switch`'s ceiling path the fix block led
    # with `comfy-qat up comfy-linux`, a command that STARTS a machine, above
    # `comfy-qat down comfy-win`, the one that may be billing. Three things
    # wrong with that at once, and none of them is cosmetic: it inverts this
    # tool's own rule that stopping the bill comes first; the first command
    # offered is the one most likely to FAIL, because GPUS_ALL_REGIONS is 1 on
    # this project and the box that may be running is the one it would need
    # room past; and each entry's prose separator — "or check first, if you
    # would rather look:" — was orphaned from the commands it introduces,
    # because the flat walk interleaved two entries' lists.
    #
    # Reading `ordered` for both is what makes the drift impossible rather than
    # merely fixed: there is now one sequence, not two that have to agree.
    ordered = [item for heading in order for item in grouped[heading]]
    for heading in order:
        # A blank line between the groups, because each heading is a different
        # claim about money and they were printed flush against each other:
        #
        #     this may exist and be billing:
        #       comfy-linux-snap-20260909 (a snapshot, in comfy-qa-testing-01)
        #     this may still be running — the stop request had gone, and ...
        #       comfy-win (comfy-win in us-central1-a)
        #
        # Five lines, two claims, one wall. The indent already says which items
        # belong to which heading; nothing said where one group ended, so the
        # heading that matters most — a GPU box that may still be running — read
        # as the fourth line of a list about a snapshot.
        #
        # Not before the first: the headline is directly above it and they are
        # one thought.
        if lines[-1] != HEADLINE_LINE:
            lines.append("")
        lines.append(heading)
        lines += _listed(grouped[heading])

    undo = [line for item in ordered for line in item.undo]
    notes = [item.note for item in ordered if item.note]
    say.error("\n".join(lines),
              say.fix(*undo, *notes) if (undo or notes) else None)
    clear()
    return True
