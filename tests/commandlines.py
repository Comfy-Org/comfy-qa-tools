"""What a line the tool prints as a command may carry: the command, and nothing else.

A command is printed to be pasted, and a shell runs every word on the line. The
first guard here read only one shape — two spaces and a `#` after a line that
started `comfy-qat `, `gcloud ` or an f-string's hole — and the shapes it did
not read were all still in the output (O25e, round two):

    to fix: comfy-qat setup --region us-central1   # the region that zone is in
      comfy-qat list        see them
      comfy-qat stamp local what a machine is, exactly
    to fix: comfy-qat quota list — the cards this project is allowed
    to fix: comfy-qat quota request --gpu t4, then wait for Google

Pasted, the first is refused by zsh (`no matches found: asia?` for its sibling)
or by `comfy-qat` (`Got unexpected extra argument(s)`), exit 2; so is every
other one. A note with one space before its `#`, an `ssh` line, a note aligned
into a column with spaces and a note after a dash are the same defect, and a
guard keyed on the one shape somebody wrote first cannot see them.

So the question is asked of the COMMAND, not of the note: once a line offers a
command, is everything after the program a word that command could take?

Two readings use this, because neither alone reaches everything:

* `tests/test_host_costs.py` walks every string in the package (`printed.py`).
  That reaches code no test drives, but it sees a value as `{}` and cannot see
  a line that is put together at run time — `comfy-qat list` and `see them`
  were two cells of one row.
* `tests/conftest.py` records every line the package prints while the suite
  runs and holds each test's output to the same rule, with the real values in.
  That is where `comfy-qat` lines are also parsed by the CLI itself: a word left
  over is an argument `comfy-qat` would refuse, whatever it looks like.

The same reading carries the two other rules a pasted gcloud line has to keep,
because they are the same question about the same lines: one that acts on a
project names it (`--project`), and one that would stop to ask carries
`--quiet` on the same physical line.
"""

from __future__ import annotations

import re
import shlex

# The programs a printed command starts with. Not every word in `say._RUNNABLE`:
# `python`, `pip`, `/` and `~/` start the lines of scripts this tool sends to a
# box, which are not printed for anybody to paste.
PROGRAMS = ("comfy-qat", "gcloud", "ssh", "scp")

# What can stand in front of a command on a printed line without being part of
# it: the indent, the label of a fix or a warning, and an explanation's gutter
# (either glyph — see `say.gutter`).
_FRAME = re.compile(r"^[ \t]*(?:(?:to fix|warning):[ \t]*)?(?:[┃|][ \t]+)?")

# A sentence that ends by introducing a command, as the house shape does —
# `run it yourself and see: gcloud compute instances list --project=…`.
_INTRODUCED = re.compile(r":[ \t]+(?=(?:comfy-qat|gcloud) )")

# gcloud's command groups. `gcloud is not installed or not on PATH.` starts with
# the program and is a sentence; a command's second word is one of these.
GCLOUD_GROUPS = frozenset({
    "alpha", "auth", "beta", "billing", "components", "compute", "config", "iam",
    "info", "init", "projects", "quotas", "services", "storage", "version",
})

# The gcloud surfaces that act on a project, so a line for pasting must say
# which: without `--project` it acts on whatever gcloud is pointed at, which
# since 1.3.1 need not be the project comfy-qat just read.
_SCOPED = re.compile(r"^gcloud (?:(?:alpha|beta) )?(?:compute|quotas|services|billing) ")

# THE ONE EXEMPTION, by name: genuinely global, so there is no project to name.
# `billing accounts` lists the accounts the signed-in user can see.
GLOBAL = ("gcloud billing accounts ",)

# The gcloud commands that stop for a "(Y/n)?" unless they are told `--quiet`.
_PROMPTS = re.compile(r"^gcloud compute (?:instances delete|reservations delete|disks "
                      r"delete|snapshots delete|reset-windows-password) \S")


def offered(line: str) -> list[str]:
    """The commands one printed line offers.

    The whole line, after its frame, when it starts with a program; and the rest
    of the line wherever a sentence on it ends with a colon and a command.
    """
    body = _FRAME.sub("", line, count=1)
    found = []
    if is_command(body):
        found.append(body)
    else:
        for match in _INTRODUCED.finditer(body):
            tail = body[match.end():]
            if is_command(tail):
                found.append(tail)
    return found


def is_command(text: str) -> bool:
    """Does `text` start the way a command does, rather than a sentence that
    happens to begin with a program's name?

    The second word decides. `gcloud is not installed or not on PATH.` and
    `comfy-qat on one project whatever gcloud is set to` (a wrapped sentence) are
    prose; `gcloud compute …` and `comfy-qat down …` are commands, and so is
    `comfy-qat {}`, a hole that holds one. `ssh` and `scp` take an option or a
    host first — `ssh process by hand leaves …` is a sentence in a help text.
    """
    words = text.split()
    if len(words) < 2 or words[0] not in PROGRAMS:
        return False
    second = words[1]
    if words[0] == "gcloud":
        return second in GCLOUD_GROUPS
    if words[0] == "comfy-qat":
        return (second in _root().commands or second.startswith("-")
                or second.startswith("{"))
    return second.startswith(("-", "{")) or "@" in second


def _unquoted(command: str) -> str:
    """`command` with its quoted spans blanked, so a space inside a quoted value
    is not read as a run of spaces between words."""
    return re.sub(r"'[^']*'|\"[^\"]*\"", lambda m: "Q" * len(m.group()), command)


def shape(command: str) -> str | None:
    """What, besides the command, the line carries — or None.

    Read off the shape alone, so it holds for a string with `{}` holes in it as
    well as for a printed line.
    """
    bare = _unquoted(command.rstrip())
    if re.search(r"\s#", bare):
        return "a # note"
    if re.search(r"\s[—–]\s|\s[—–]$", bare):
        return "a note after a dash"
    if re.search(r"\S[ \t]{2,}\S", bare):
        return "words set off by a run of spaces — a note aligned into a column"
    try:
        words = shlex.split(command)
    except ValueError:
        return "a quote that never closes"
    for word in words:
        if word.endswith((",", ";")) or (word.endswith(":") and len(word) > 1):
            return f"prose after the command ({word!r} ends a phrase, not a word)"
    if words and words[-1].endswith(".") and words[-1].strip(".") and \
            not words[-1].startswith((".", "/", "~")):
        return f"a full stop on the last word ({words[-1]!r})"
    return None


def bare_note(line: str) -> str | None:
    """A `#` note on a line that is a flag a fix offers to add — `--region
    us-central1   # …` — or a hole standing for a whole command — `{}   # …`.

    Only the note, for these two. A line that starts with a flag is as often a
    sentence about the flag (`--zone was given, so …`), and a line that starts
    with a hole is as often a sentence about what fills it (`{} is local — …`);
    only the printed output knows which, and `problems` reads the flag lines of
    a fix there in full.
    """
    body = _FRAME.sub("", line, count=1)
    if body.startswith(("--", "{}")) and re.search(r"\s#", _unquoted(body)):
        return "a # note"
    return None


def scope(command: str) -> str | None:
    """The two rules a pasted gcloud line keeps besides being only itself."""
    words = command.split()
    if _SCOPED.match(command) and not command.startswith(GLOBAL):
        if not any(word.startswith("--project") for word in words):
            return "acts on a project and does not say which (no --project)"
    if _PROMPTS.match(command) and "--quiet" not in words:
        return "stops to ask (no --quiet on this line)"
    return None


# A gcloud command that acts on a project, wherever it stands on a line.
_SCOPED_ANYWHERE = re.compile(r"(?<![\w-])gcloud (?:(?:alpha|beta) )?"
                              r"(?:compute|quotas|services|billing) ")
# Where a command named in a sentence ends: the full stop, comma or semicolon
# that resumes the prose, or a dash between spaces.
_PROSE_RESUMES = re.compile(r"[.,;](?=\s|$)|\s[—–]\s")


def named_in_passing(line: str) -> list[str]:
    """The project-scoped gcloud commands a line names anywhere in it, each cut
    where the prose around it resumes — offered or not.

    `offered` reads a command at the start of a line or after the colon that
    introduces it, and `prune` named one in the middle of a warning: `Find where
    each one really is with gcloud compute instances list, then correct
    gce_zone by hand`. That is a command a person is told to run, and as written
    it lists gcloud's own project. One quoted in backticks is prose about the
    command — `gcloud quotas preferences` has no delete — and is not read.
    """
    bare = re.sub(r"`[^`]*`", lambda m: "Q" * len(m.group()), line)
    found = []
    for match in _SCOPED_ANYWHERE.finditer(bare):
        end = _PROSE_RESUMES.search(line, match.end())
        found.append(line[match.start():end.start() if end else len(line)].rstrip())
    return found


def unnamed_project(line: str) -> str | None:
    """The first project-scoped gcloud command anywhere on `line` that does not
    say which project, or None."""
    for command in named_in_passing(line):
        if not command.startswith(GLOBAL) and "--project" not in command:
            return command
    return None


# --- the printed reading: real values, and the CLI's own parser ---------------


_ROOT = None


def _root():
    global _ROOT
    if _ROOT is None:
        import typer

        from comfy_qa.cli import app

        _ROOT = typer.main.get_command(app)
    return _ROOT


def leftover(words: list[str]) -> str | None:
    """What `comfy-qat` would refuse in `words` (everything after `comfy-qat`).

    Click's parser only — `make_parser(...).parse_args` — so no callback runs
    and no value is converted: a placeholder such as `<card>` is a value like
    any other, and asking changes no state. A group passes the first word left over to the command it names.
    Returns None when the line is not a command at all — `comfy-qat 1.3.1` is
    the version — or when every word is one the command takes.
    """
    command = _root()
    parent = None
    args = list(words)
    name = "comfy-qat"
    while True:
        # Not `resilient_parsing`: that makes the parser swallow an unknown
        # option and stop, which reads as nothing wrong.
        context = command.context_class(command, info_name=name, parent=parent)
        try:
            _, rest, _ = command.make_parser(context).parse_args(list(args))
        except Exception as exc:          # NoSuchOption, BadOptionUsage, …
            return str(exc)
        subcommands = getattr(command, "commands", None)
        if subcommands is None:
            return f"unexpected extra argument(s): {' '.join(rest)}" if rest else None
        if not rest:
            return None
        sub = subcommands.get(rest[0])
        if sub is None:
            # The first word is not a command, so this was never one: the
            # version line, or `comfy-qat` named in a sentence.
            return None if parent is None else f"no command {rest[0]!r}"
        command, parent, name, args = sub, context, rest[0], rest[1:]


def problems(lines: list[str]) -> list[tuple[str, str]]:
    """`(line, what is wrong)` for every line in `lines` — output as printed, in
    order — that offers a command carrying more than the command.

    In order, because a fix is a block: `to fix: ` opens one and the lines under
    it at the fix's indent belong to it, and a flag on its own there is a
    fragment offered for the command above it.
    """
    from comfy_qa.say import FIX_INDENT, FIX_LABEL

    found = []
    in_fix = False
    for line in lines:
        if line.startswith(FIX_LABEL):
            in_fix = True
        elif in_fix and not (line.startswith(FIX_INDENT) or not line.strip()):
            in_fix = False
        for command in offered(line):
            wrong = shape(command) or scope(command)
            if wrong is None and command.startswith("comfy-qat "):
                try:
                    words = shlex.split(command)
                except ValueError:
                    words = command.split()
                wrong = leftover(words[1:])
            if wrong:
                found.append((line, wrong))
        body = _FRAME.sub("", line, count=1)
        if in_fix and body.startswith("--"):
            wrong = shape(body)
            if wrong:
                found.append((line, wrong))
        elif bare_note(line):
            found.append((line, bare_note(line)))
        unnamed = unnamed_project(line)
        if unnamed and not (found and found[-1][0] == line):
            found.append((line, f"names {unnamed!r}, which acts on a project and "
                                f"does not say which (no --project)"))
    return found


def offered_argv(output: str, *start: str) -> list[list[str]]:
    """Every command `output` offers whose words begin with `start`, split the
    way a shell splits it — and cut at a word that ends a phrase, so the words
    are the ones a person would copy, whichever shape the line had."""
    found = []
    for line in output.splitlines():
        for command in offered(line):
            try:
                words = shlex.split(command)
            except ValueError:
                words = command.split()
            for index, word in enumerate(words):
                if word.endswith((",", ";")):
                    words = words[:index] + [word.rstrip(",;")]
                    break
            if tuple(words[:len(start)]) == start:
                found.append(words)
    return found


def flags(words: list[str]) -> dict[str, object]:
    """`--flag value` and `--switch` pairs, in a dict: what a create was asked."""
    pairs: dict[str, object] = {}
    rest = list(words)
    while rest:
        word = rest.pop(0)
        if word.startswith("--"):
            pairs[word] = rest.pop(0) if rest and not rest[0].startswith("--") else True
    return pairs


def in_source() -> list[tuple[str, str, str]]:
    """`(where, line, what is wrong)` for every line of every string in the
    package that offers a command carrying more than the command — the reading
    that reaches code no test drives. Shape only: a value is `{}` here."""
    from printed import strings

    found = []
    for where, text in strings():
        for line in text.splitlines():
            for command in offered(line):
                wrong = shape(command)
                if wrong:
                    found.append((where, line.strip(), wrong))
            if bare_note(line):
                found.append((where, line.strip(), bare_note(line)))
    return found
