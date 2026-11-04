"""QA Desk, read through its own `qa-desk` CLI, and written to only as a dry run.

QA Desk is where release QA is recorded (qadesk.engcomfy.com). It has a CLI,
`qa-desk`, signed in by the person who runs it — `qa-desk login` in their own
terminal, which asks them to paste a token they made on their QA Desk page. This
module never touches that token. It runs `qa-desk` the way the person would, as
a subprocess under their own environment, and reads what it prints. It does not
open the CLI's config file, does not read or pass `QA_DESK_TOKEN`, and prints
nothing of QA Desk's answer that it did not ask for by name.

WHAT IS READ, AND WHAT IS NEVER SENT. Reads are `whoami`, `case show`, `case
list`, `release show` and `release list`, always with `--json`, which
`qa-desk describe` calls "stable; use this from agents". Besides those, only
the three writes this tool drafts — `case pass`, `case fail` and `test report`
— are ever run, and only as a dry run: "for writes: print the request instead
of sending it", in the CLI's own words, and read in its source: a dry run
returns the request (method, path, body) and never calls the server. So a
result drafted here is shown to the person, exactly as it would be sent, and
sending it is theirs to do: they paste the command. Ali, 3 Nov: "dry run". The
refusal is in `run` itself, before anything is started, and `tests/conftest.py`
holds every argv the suite hands `_spawn` to the same rule, failing the test at
teardown if one ever got through.

THE ARGV IS READ AS QA DESK'S CLI READS IT, because that is the only reading
that decides what it does. Its `parse` (main.ts) stops reading flags at a bare
`--`, so a `--dry-run` after one is a word and the write is sent; and it takes
a value that starts with `--` as no value at all. Both were reachable: `report
1.60 X --cases C-01 --note --` handed it `… --note -- --dry-run --json`, which
the old check — is `--dry-run` anywhere in the argv? — let through as a dry run
(behaviour audit F1, 4 Nov), and a note that began `--frontend-version …` was
dropped from a pass and made a fail exit 6 (code audit T2). So `parse` here is
the CLI's own, `unsent` asks it, and every value this tool puts in a command is
attached to its flag with `=` — `--note=<text>` — which the CLI keeps word for
word, a leading `--` or a lone `--` included. And `login`, `logout`, `token`
and `setup` run before the CLI ever looks at `--dry-run` (`logout` rewrites its
config either way), so a dry run of anything but the three drafts is refused
too: no call site sends one, and this is what keeps it so.

A LONG ANSWER IS READ FROM A FILE, NOT A PIPE. The CLI prints its answer and
exits without waiting for a pipe to drain, so through a pipe `release show 1.60
--json` stopped at exactly 65,536 bytes of its 128,474 (4 Nov), and `where 1.60`
said QA Desk had answered something that was not JSON. Into a file the write is
whole.

`qa-desk` MISSING OR SIGNED OUT IS NOT A FAILURE OF THIS TOOL. Everything here
that does not need QA Desk — "Test this" for a PR or a version, a local pass or
fail, a bug draft, the recorder — works without it. What needs it says so, with
the command that fixes it: the install line from QA Desk's own instructions, or
`qa-desk login`.

Exit codes are QA Desk's own (`qa-desk describe`, 3 Nov): 0 done, 1 network or
server error, 2 needs confirmation, 3 not signed in or the role cannot, 4 the
state does not allow it, 5 not found, 6 bad input. Each has its own fix: a
retry is the answer to 1 only.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import tempfile

PROGRAM = "qa-desk"

# The reads, by their command words, and the only writes this tool drafts. A
# draft is run only with DRY_RUN on it; nothing else is run at all.
READS = frozenset({("whoami",), ("case", "show"), ("case", "list"),
                   ("release", "show"), ("release", "list")})
DRAFTS = frozenset({("case", "pass"), ("case", "fail"), ("test", "report")})
DRY_RUN = "--dry-run"

# How QA Desk says to install its CLI: "the line on their QA Desk page (Agent
# tokens): curl -fsSL <agent API URL>/cli/install.sh | sh" (the qa-desk skill).
# The agent API URL is the one `qa-desk whoami` reports, and the script was
# there on 4 Nov (HTTP 200, text/x-shellscript).
INSTALL = "curl -fsSL https://qadesk.engcomfy.com/agent/cli/install.sh | sh"
LOGIN = "qa-desk login"

# Exit codes, as `qa-desk describe` lists them.
FAILED = 1
NEEDS_CONFIRMATION = 2
NOT_SIGNED_IN = 3
CONFLICT = 4
NOT_FOUND = 5
BAD_INPUT = 6
MISSING = 127

# Long enough for the CLI's own retries (it retries a dropped connection three
# times, backing off), short enough that a hung network is not a hung command.
TIMEOUT = 90


class QaDeskError(Exception):
    """QA Desk could not be asked, or said no — in words, with what to do."""

    def __init__(self, message: str, fix: str | None = None, *, code: int = 1,
                 exit: int | None = None) -> None:
        super().__init__(message)
        self.fix = fix
        self.code = code
        # QA Desk's own exit code, when it answered with one: `pass` and `fail`
        # keep no result for a case QA Desk says is not there (5), and keep one
        # for everything else that stopped it being read.
        self.exit = exit
        self.kind = "qa-desk"
        self.refusal = True


# `qa-desk`'s boolean flags, from its source (main.ts, `BOOLEAN`): these take
# no value.
BOOLEAN = frozenset({"json", "dry-run", "mine", "unclaimed", "confirm-other",
                     "needs-test", "clear", "preview", "help", "no-open",
                     "clear-github"})


def parse(argv: list[str]) -> tuple[list[str], dict]:
    """`qa-desk`'s own `parse` (main.ts), line for line: its words, and its
    flags.

    A bare `--` ends the flags, and every word after it is a word.
    `--name=value` keeps everything after the first `=`, an empty value
    included. Otherwise a flag takes the next word as its value — unless it is
    a boolean, or there is no next word, or the next word starts with `--`, when
    it is `True`.
    """
    words: list[str] = []
    flags: dict = {}
    index = 0
    while index < len(argv):
        word = argv[index]
        if word == "--":
            words.extend(argv[index + 1:])
            break
        if word.startswith("--"):
            name, equals, inline = word[2:].partition("=")
            if equals:
                flags[name] = inline
            elif (name in BOOLEAN or index + 1 >= len(argv)
                  or argv[index + 1].startswith("--")):
                flags[name] = True
            else:
                index += 1
                flags[name] = argv[index]
        else:
            words.append(word)
        index += 1
    return words, flags


def _own(argv: list[str]) -> list[str]:
    """The argv without the program in front of it."""
    return list(argv[1:] if argv and argv[0].endswith(PROGRAM) else argv)


def words(argv: list[str]) -> tuple[str, ...]:
    """The command words of a `qa-desk` argv — what comes before its arguments.

    Matched as `qa-desk` itself matches them (`find` in its source): two words
    first, then one.
    """
    plain, _ = parse(_own(argv))
    if tuple(plain[:1]) in READS:
        return tuple(plain[:1])
    return tuple(plain[:2])


def is_read(argv: list[str]) -> bool:
    """Is this argv one of the five reads?"""
    return words(argv) in READS


def is_dry_run(argv: list[str]) -> bool:
    """Would QA Desk's CLI read this argv as a dry run? `if (flags["dry-run"])`:
    the flag, before any bare `--`, and not given an empty value."""
    return bool(parse(_own(argv))[1].get("dry-run"))


def unsent(argv: list[str]) -> bool:
    """Would running this argv send QA Desk nothing? A read, or the dry run of
    one of the three writes this tool drafts — each as QA Desk's CLI reads it."""
    return is_read(argv) or (words(argv) in DRAFTS and is_dry_run(argv))


def flag(name: str, value: str) -> str:
    """One flag and its value, as one word: `--note=<text>`. QA Desk's CLI keeps
    everything after the first `=` as it is, so a value that starts with `--`,
    or is a lone `--`, is still the value and never a flag of its own."""
    return f"--{name}={value}"


def printed(command: list[str]) -> str:
    """A `qa-desk` command as a person pastes it: shell-quoted, with a flag's
    value quoted after its `=` (`--note='both tabs kept'`), which the shell
    joins back into the one word QA Desk's CLI reads."""
    shown = [PROGRAM]
    for word in command:
        name, equals, value = word.partition("=")
        if word.startswith("--") and equals and shlex.quote(name) == name:
            shown.append(f"{name}={shlex.quote(value)}")
        else:
            shown.append(shlex.quote(word))
    return " ".join(shown)


def _run_real(argv: list[str]) -> tuple[int, str, str]:
    """Run `qa-desk` itself. Its stdin is closed, so `login`'s prompt — which
    this module never runs — could not wait for a paste it will not get.

    Its answer goes to a file, not a pipe: the CLI exits without waiting for a
    pipe to drain, and through one a long answer stopped at 65,536 bytes (see
    the module's docstring). A file is written whole before the CLI exits."""
    if shutil.which(argv[0]) is None:
        return MISSING, "", f"{argv[0]} is not on PATH"
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            done = subprocess.run(argv, stdout=out, stderr=err, timeout=TIMEOUT,
                                  stdin=subprocess.DEVNULL)
            out.seek(0)
            err.seek(0)
            return (done.returncode, out.read().decode("utf-8", "replace"),
                    err.read().decode("utf-8", "replace"))
    except FileNotFoundError:
        return MISSING, "", f"{argv[0]} is not on PATH"
    except (OSError, subprocess.SubprocessError) as exc:
        return FAILED, "", str(exc)


# What executes an argv. The suite replaces this with a fake QA Desk; the
# tripwire in tests/conftest.py wraps `_spawn`, which every argv goes through
# whoever is answering it.
RUNNER = _run_real


def _spawn(argv: list[str]) -> tuple[int, str, str]:
    """The one place a `qa-desk` argv is executed."""
    return RUNNER(argv)


def missing_fix() -> str:
    from . import say

    return say.fix("install it with the line on your QA Desk page (Agent tokens), "
                   "then sign in, in your own terminal:", INSTALL, LOGIN)


def run(args: list[str]):
    """Run one `qa-desk` command and return what it answered, parsed.

    Only a read, or the dry run of a draft, is run — as QA Desk's CLI reads
    the argv — and anything else is refused here, before anything is started:
    this tool records nothing in QA Desk itself.
    """
    argv = [PROGRAM, *args]
    if not parse(args)[1].get("json"):
        argv.append("--json")
    if not unsent(argv):
        command = " ".join(words(argv))
        if words(argv) in DRAFTS:
            raise QaDeskError(
                f"This tool sends QA Desk nothing, so `{command}` is run only as a "
                f"dry run. This one had no --dry-run that qa-desk would read as "
                f"one, and was not run.",
                fix="run it yourself, in your own terminal, once you have read it",
                code=2)
        raise QaDeskError(
            f"This tool runs only QA Desk's reads and the dry runs of the three "
            f"writes it drafts (case pass, case fail, test report), so `{command}` "
            f"was not run.",
            fix="run it yourself, in your own terminal, if it is what you meant",
            code=2)
    code, out, err = _spawn(argv)
    said = _said(out, err)
    if code == MISSING:
        raise QaDeskError(
            "qa-desk is not installed here, or not on PATH, so QA Desk could not be "
            "asked. Nothing else in comfy-qat needs it.",
            fix=missing_fix(), code=2, exit=code)
    if code == NOT_SIGNED_IN:
        raise QaDeskError(
            f"qa-desk is not signed in, or your QA Desk role cannot do this ({said}), "
            f"so QA Desk could not be asked. Nothing else in comfy-qat needs it.",
            fix=_fix("sign in, in your own terminal — it asks you to paste a token "
                     "you make on your QA Desk page:", LOGIN), code=2, exit=code)
    if code == NOT_FOUND:
        raise QaDeskError(f"QA Desk has no such thing: {said}.",
                          fix=_fix("see the releases, and a release's cases:",
                                   "qa-desk release list",
                                   "qa-desk case list <version>"), code=2, exit=code)
    if code == BAD_INPUT:
        # Its own check of what it was given (`need`, a usage line): running
        # it again changes nothing, so the fix is the command, to read.
        raise QaDeskError(
            f"QA Desk's CLI refused what it was given, as bad input: {said}.",
            fix=_fix("this is what it was given; correct what its message names, "
                     "then run it again:", printed(argv[1:])), code=2, exit=code)
    if code == CONFLICT:
        raise QaDeskError(
            f"QA Desk says the state of things does not allow this: {said}.",
            fix=_fix("see where it stands:", _show(argv)), code=2, exit=code)
    if code == NEEDS_CONFIRMATION:
        raise QaDeskError(
            f"QA Desk asks for a confirmation first: {said}.",
            fix=_fix("see who holds it before you decide — recording over somebody "
                     "else's claim is QA Desk's --confirm-other, and yours to add:",
                     _show(argv)), code=2, exit=code)
    if code != 0:
        raise QaDeskError(f"qa-desk answered exit {code}: {said}.",
                          fix="QA Desk's CLI has already retried a dropped connection or "
                              "a server error; run the same command again once",
                          code=1, exit=code)
    try:
        return json.loads(out or "null")
    except ValueError as exc:
        if (out or "").lstrip().startswith(("{", "[")):
            # It began as JSON and stopped: an answer cut off on the way here,
            # not one in a shape this tool was not written for.
            raise QaDeskError(
                f"qa-desk's answer stopped part-way, after "
                f"{len(out.encode('utf-8')):,} bytes ({exc}), so nothing was read "
                f"from it.",
                fix=_fix("ask again once; if it stops again, see the whole answer "
                         "for yourself:", printed(argv[1:])), code=1) from exc
        raise QaDeskError(f"qa-desk answered something that is not JSON ({exc}).",
                          fix=_update(), code=1) from exc


def _show(argv: list[str]) -> str:
    """The read that shows where a case, or a release, stands."""
    plain, _ = parse(_own(argv))
    if plain[:1] == ["case"] and len(plain) >= 4:
        return printed(["case", "show", plain[2], plain[3]])
    if len(plain) >= 3:
        return printed(["release", "show", plain[2]])
    return printed(["whoami"])


def _update() -> str:
    """The fix for a `qa-desk` that answers in a shape this was not written for:
    the same install line, which updates it."""
    return _fix("update qa-desk — the install line updates it:", INSTALL)


def _fix(*lines: str) -> str:
    from . import say

    return say.fix(*lines)


def _said(out: str, err: str) -> str:
    """QA Desk's own message, one line, from `--json`'s `error` or from stderr."""
    try:
        body = json.loads(out or "null")
    except ValueError:
        body = None
    if isinstance(body, dict) and body.get("error"):
        text = str(body["error"])
    else:
        text = (err or out or "").strip()
    text = " ".join(text.split())
    return text.removeprefix("qa-desk: ")[:200] or "no message"


# --- the reads --------------------------------------------------------------


def case(version: str, key: str) -> dict:
    """`qa-desk case show <version> <case> --json`."""
    found = run(["case", "show", version, key])
    if not isinstance(found, dict) or not found.get("case_key"):
        raise QaDeskError(f"QA Desk answered case {key} in {version} without the case.",
                          fix=_update())
    return found


def release(version: str) -> dict:
    """`qa-desk release show <version> --json`."""
    found = run(["release", "show", version])
    if not isinstance(found, dict):
        raise QaDeskError(f"QA Desk answered release {version} with no release.",
                          fix=_update())
    return found


# --- the drafts -------------------------------------------------------------


def dry_run(command: list[str]) -> dict:
    """Run `command` — the words after `qa-desk` — as a dry run, and return the
    request QA Desk's CLI says it would have sent. Nothing is sent."""
    found = run([*command, DRY_RUN])
    if not isinstance(found, dict) or not found.get("dry_run"):
        raise QaDeskError("qa-desk did not answer the dry run as a dry run, so "
                          "nothing it said is shown.", fix=_update(), code=1)
    return found


def request_lines(answer: dict) -> list[str]:
    """What a dry run said it would send, as lines to read: the method and URL,
    then the body's fields. Only those: the answer carries no token (the CLI
    adds its Authorization header when it sends, never in a dry run), and a key
    that names one is dropped anyway rather than trusted to be absent."""
    request = answer.get("request") if isinstance(answer, dict) else None
    if not isinstance(request, dict):
        return []
    lines = [f"{request.get('method', '?')} {request.get('url') or request.get('path')}"]
    body = request.get("body")
    if isinstance(body, dict):
        for key, value in body.items():
            if value in (None, "", [], False) or "token" in key.lower() \
                    or "auth" in key.lower():
                continue
            lines.append(f"  {key}: {json.dumps(value, ensure_ascii=False)}")
    return lines
