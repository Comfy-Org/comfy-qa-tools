"""The questions this tool asks, and the one rule for when it may ask.

`say` is everything this tool says; this is the half where it waits for an
answer. They are separate modules on purpose — `say.py`'s vocabulary is seven
writers with no input among them, and a prompt is not a kind of output.

THE RULE, and it is a single sentence: a required choice that was not given is
asked for when somebody is there to answer, and refused by name when nobody is.
Before this, `comfy-qat create` did the second thing in both cases — no `--os`
was a Typer parse error, exit 2, `Missing option '--os'.` — while the tool
already knew the answer set: `create.drivable_cards()` is the list of cards it
can bring up, and `create.IMAGES` is the two operating systems. A tool that
holds the menu and prints a parse error instead is making the newcomer read
`--help` to learn something it could have offered.

Two further rules, both taken from the recorder's parity table
(`tools/test-recorder/AGENTS.md`), and both about making the two paths produce
the SAME transcript:

* **A flag that was supplied is confirmed where the prompt would have been.**
  `GPU: l4 (from --gpu)` sits exactly where `Which card?` would have been
  answered. A scripted run and an interactive run then read alike, which matters
  for a tool whose output is pasted into a bug report — otherwise the transcript
  of a scripted run is silent about the two decisions that determined everything
  else in it.

* **An invalid value warns and falls back to the prompt, when there is somebody
  to ask.** `--gpu p100` is a real card this tool cannot drive; aborting over it
  taught nothing and cost the whole invocation. The explanation is still
  printed — it is the same sentence the refusal would have carried, taken from
  the same function — and then the menu appears. With nobody there, it stays a
  refusal, because a script needs the exit code.

Nothing here has a dependency of its own. `typer.prompt` and a numbered list are
enough for a menu of two operating systems and five cards, and a prompt toolkit
would be a new runtime dependency for a package that has exactly one.
"""

from __future__ import annotations

from typing import Callable, Sequence

import typer

from . import say


def watching() -> bool:
    """Is there somebody to answer a question?

    `say.watching`, not a rule of this module's own — which is itself
    `gcloud.can_prompt`, and that is the point: stdin AND stderr both have to be
    terminals, because that is gcloud's own test and this tool has to agree with
    the program it drives. Three TTY tests in one package is how a prompt gets
    asked on a stream nobody is reading.
    """
    return say.watching()


def choose(question: str, options: Sequence[str],
           notes: Sequence[str] | None = None, *, err: bool = True) -> str:
    """A numbered pick. Returns the option, never the number.

    ONE implementation. `cli._choose` was this, on stdout, for `setup`'s project
    prompt, and a second copy here for `create` is the second-site shape this
    repo keeps finding — the next correction would have landed on one of them.
    `err` is the difference between the two callers and it is a parameter rather
    than a fork.

    stderr by default, which is where a new prompt should be: `say`'s stream
    rule is that stdout carries the answer and stderr carries the story, a
    question is the story, and `create`'s stdout is a plan somebody reads back.
    `setup` keeps the stream it has always had.
    """
    typer.echo(question, err=err)
    width = max((len(option) for option in options), default=0)
    for index, option in enumerate(options, start=1):
        note = (notes[index - 1] if notes and index - 1 < len(notes) else "")
        # The note column aligned the way `say.write_fix` aligns its `#` notes,
        # and for the same reason: the gap was counted by eye at the call site
        # once and was two columns out.
        row = f"  {index}. {option.ljust(width) if note else option}"
        typer.echo(f"{row}   {note}".rstrip(), err=err)
    while True:
        # `err=True` on the prompt as well as on the list. Click writes the
        # prompt to stdout unless told otherwise, so a question whose options
        # were on stderr would ask itself on the other stream — and under a
        # redirect the user would see a bare `Number:` with nothing above it.
        raw = typer.prompt("Number", err=err)
        try:
            picked = int(raw)
        except ValueError:
            picked = 0
        if 1 <= picked <= len(options):
            return options[picked - 1]
        typer.echo(f"pick a number between 1 and {len(options)}", err=err)


def settle(
    flag: str,
    value: str | None,
    *,
    label: str,
    question: str,
    options: Sequence[str],
    notes: Sequence[str] | None = None,
    problem: Callable[[str], str | None] = lambda _value: None,
    missing: str,
    fix: str,
) -> str:
    """The value of a required choice: given, or asked for, or refused by name.

    `problem` answers "why can this value not be used", in the words the refusal
    would have used, or None. It is a callable rather than a list of acceptable
    values because "acceptable" is not a membership test: `--gpu p100` names a
    card that exists, that this project can hold quota for, and that this tool
    will not order — and the sentence explaining that is worth more than
    "not one of: a100, h100, l4, t4".

    Returns what the caller asked for and validates nothing further. The command
    still runs its own planning over the result, which is what keeps this from
    becoming a second opinion about what a valid card is: with nobody to ask,
    a bad value is handed straight back and refused exactly where it was
    refused before.
    """
    if value is None:
        if not watching():
            # NOT a hang, and not Typer's parse error either. A script gets the
            # same exit code it got before (2, "the command could not start")
            # and a sentence naming the flag and the answers.
            say.fail(missing, fix=fix, code=2, blank_line=False)
        picked = choose(question, options, notes)
        say.detail(f"{label}: {picked}")
        return picked

    why = problem(value)
    if why is None:
        # WHERE THE PROMPT WOULD HAVE BEEN, so the two transcripts line up.
        #
        # `say.detail` and not a bare `typer.echo(err=True)`: this is a fact
        # under the question above it, which is what `detail` is for, and the
        # difference is not only tidiness. `tests/test_docs.py` collects every
        # `typer.echo(..., err=True)` in the package as a message needing a
        # troubleshooting entry, because that spelling is how failures reach
        # people — so writing a confirmation with it files a confirmation under
        # errors. The vocabulary is the classification.
        say.detail(f"{label}: {value} (from {flag})")
        return value
    if not watching():
        # Unchanged: the command's own planning refuses it, in its own words,
        # with its own exit code. Warning here and continuing would be a second
        # message about one failure.
        return value
    say.warn(f"{flag} {value} cannot be used — asking you instead")
    # The refusal's own words, in a block. `explain` rather than `warn` so the
    # reason is bound to the warning above it and cannot be mistaken for the
    # menu below it — this is the one place in the tool where prose, a warning
    # and a prompt land in the same three inches of terminal.
    say.explain(why)
    picked = choose(question, options, notes)
    say.detail(f"{label}: {picked}")
    return picked
