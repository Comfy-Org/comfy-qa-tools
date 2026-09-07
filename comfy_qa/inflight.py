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

import threading
from contextlib import contextmanager
from dataclasses import dataclass


class Interrupted(BaseException):
    """Ctrl-C, wearing a name Click does not recognise.

    Click catches `KeyboardInterrupt` inside its own `main()`, prints a blank
    line and `Aborted!`, and exits 1 — all of it before anything of ours runs,
    and `Aborted!` over a GPU box that is running and billing is the most
    expensive sentence this tool can print. A `BaseException` Click does not
    name walks straight through to `cli.main`, which is where the record is read.

    A `BaseException` and not an `Exception`, for the same reason the interrupt
    it stands for is one: every `except` in this codebase that means "this step
    failed" must not swallow it.
    """


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
    billing: bool = True
    """Is this a thing that now exists and costs money?

    Almost always yes, and that is the whole point of the record. `switch` is
    the exception: on the ceiling path it STOPS the machine you were on before
    it starts the one you asked for, so an interrupt in between leaves you on
    neither — nothing extra is billing, and the session you were mid-way through
    is gone. That is worth the same sentence, under a different heading, and
    reporting it as a bill would be a second false claim in place of the first.
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
def may_leave(what: str, *, undo=(), note: str = "", billing: bool = True):
    """Register what this call may leave behind, for as long as it is in flight.

    Nested registrations are fine and are the normal case for `move`, which
    makes a snapshot, then a disk, then an instance: each is registered before
    the call that creates it and dropped when that call returns, so at any
    moment the record holds exactly what exists and is not yet accounted for.
    """
    entry = Leftover(what, tuple(undo), note, billing)
    with _lock:
        _entries.append(entry)
    try:
        yield entry
    except KeyboardInterrupt as exc:
        # Converted here rather than left alone, because Click is between this
        # and `cli.main` and it swallows the real thing.
        raise Interrupted() from exc
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

    lines = [f"{HEADLINE}."]
    billing = [item for item in left if item.billing]
    already = [item for item in left if not item.billing]
    if billing:
        lines.append("this may exist and be billing:")
        lines += _listed(billing)
    if already:
        lines.append("and this had already happened when you stopped it:")
        lines += _listed(already)
    undo = [line for item in left for line in item.undo]
    notes = [item.note for item in left if item.note]
    say.error("\n".join(lines),
              say.fix(*undo, *notes) if (undo or notes) else None)
    clear()
    return True
