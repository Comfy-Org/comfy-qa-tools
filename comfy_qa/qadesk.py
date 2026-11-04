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
`qa-desk describe` calls "stable; use this from agents". Every other `qa-desk`
command is a write as far as this module is concerned, and is run only with
`--dry-run` — "for writes: print the request instead of sending it", in the
CLI's own words, and read in its source: a dry run returns the request (method,
path, body) and never calls the server. So a result drafted here is shown to the
person, exactly as it would be sent, and sending it is theirs to do: they paste
the command. Ali, 3 Nov: "dry run". The refusal is in `run` itself, before
anything is started, and `tests/conftest.py` holds every argv the suite hands
`_spawn` to the same rule, failing the test at teardown if one ever got through.

`qa-desk` MISSING OR SIGNED OUT IS NOT A FAILURE OF THIS TOOL. Everything here
that does not need QA Desk — "Test this" for a PR or a version, a local pass or
fail, a bug draft, the recorder — works without it. What needs it says so, with
the command that fixes it: the install line from QA Desk's own instructions, or
`qa-desk login`.

Exit codes are QA Desk's own (`qa-desk describe`, 3 Nov): 0 done, 1 network or
server error, 2 needs confirmation, 3 not signed in or the role cannot, 4 the
state does not allow it, 5 not found, 6 bad input.
"""

from __future__ import annotations

import json
import shutil
import subprocess

PROGRAM = "qa-desk"

# The reads, by their command words. Everything else `qa-desk` takes is run only
# with DRY_RUN on it.
READS = frozenset({("whoami",), ("case", "show"), ("case", "list"),
                   ("release", "show"), ("release", "list")})
DRY_RUN = "--dry-run"

# How QA Desk says to install its CLI: "the line on their QA Desk page (Agent
# tokens): curl -fsSL <agent API URL>/cli/install.sh | sh" (the qa-desk skill).
# The agent API URL is the one `qa-desk whoami` reports, and the script was
# there on 4 Nov (HTTP 200, text/x-shellscript).
INSTALL = "curl -fsSL https://qadesk.engcomfy.com/agent/cli/install.sh | sh"
LOGIN = "qa-desk login"

# Exit codes, as `qa-desk describe` lists them.
NOT_SIGNED_IN = 3
NOT_FOUND = 5
MISSING = 127

# Long enough for the CLI's own retries (it retries a dropped connection three
# times, backing off), short enough that a hung network is not a hung command.
TIMEOUT = 90


class QaDeskError(Exception):
    """QA Desk could not be asked, or said no — in words, with what to do."""

    def __init__(self, message: str, fix: str | None = None, *, code: int = 1) -> None:
        super().__init__(message)
        self.fix = fix
        self.code = code
        self.kind = "qa-desk"
        self.refusal = True


def words(argv: list[str]) -> tuple[str, ...]:
    """The command words of a `qa-desk` argv — what comes before its arguments.

    Flags and their values are not command words; neither are the version and
    case that follow the command. Matched as `qa-desk` itself matches them
    (`find` in its source): two words first, then one.
    """
    plain = []
    rest = list(argv[1:] if argv and argv[0].endswith(PROGRAM) else argv)
    while rest:
        word = rest.pop(0)
        if word.startswith("--"):
            # `--flag value`, unless the flag is one of qa-desk's booleans or
            # carries its value after an `=`.
            if ("=" not in word and word not in _BOOLEANS and rest
                    and not rest[0].startswith("--")):
                rest.pop(0)
            continue
        plain.append(word)
    if tuple(plain[:1]) in READS:
        return tuple(plain[:1])
    return tuple(plain[:2])


# `qa-desk`'s boolean flags, from its source (`BOOLEAN`): these take no value.
_BOOLEANS = frozenset({"--json", "--dry-run", "--mine", "--unclaimed",
                       "--confirm-other", "--needs-test", "--clear", "--preview",
                       "--help", "--no-open", "--clear-github"})


def is_read(argv: list[str]) -> bool:
    """Is this argv one of the five reads? Anything else is treated as a write."""
    return words(argv) in READS


def unsent(argv: list[str]) -> bool:
    """Would running this argv send QA Desk nothing? A read, or a dry run."""
    return is_read(argv) or DRY_RUN in argv


def _run_real(argv: list[str]) -> tuple[int, str, str]:
    """Run `qa-desk` itself. Its stdin is closed, so `login`'s prompt — which
    this module never runs — could not wait for a paste it will not get."""
    if shutil.which(argv[0]) is None:
        return MISSING, "", f"{argv[0]} is not on PATH"
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        return MISSING, "", f"{argv[0]} is not on PATH"
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return done.returncode, done.stdout, done.stderr


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

    A write without `--dry-run` is refused here, before anything is started:
    this tool records nothing in QA Desk itself.
    """
    argv = [PROGRAM, *args]
    if "--json" not in argv:
        argv.append("--json")
    if not unsent(argv):
        raise QaDeskError(
            f"This tool sends QA Desk nothing, so `{' '.join(words(argv))}` is run "
            f"only as a dry run. This one had no --dry-run, and was not run.",
            fix="run it yourself, in your own terminal, once you have read it",
            code=2)
    code, out, err = _spawn(argv)
    said = _said(out, err)
    if code == MISSING:
        raise QaDeskError(
            "qa-desk is not installed here, or not on PATH, so QA Desk could not be "
            "asked. Nothing else in comfy-qat needs it.",
            fix=missing_fix(), code=2)
    if code == NOT_SIGNED_IN:
        raise QaDeskError(
            f"qa-desk is not signed in, or your QA Desk role cannot do this ({said}), "
            f"so QA Desk could not be asked. Nothing else in comfy-qat needs it.",
            fix=_fix("sign in, in your own terminal — it asks you to paste a token "
                     "you make on your QA Desk page:", LOGIN), code=2)
    if code == NOT_FOUND:
        raise QaDeskError(f"QA Desk has no such thing: {said}.",
                          fix=_fix("see the releases, and a release's cases:",
                                   "qa-desk release list",
                                   "qa-desk case list <version>"), code=2)
    if code != 0:
        raise QaDeskError(f"qa-desk answered exit {code}: {said}.", code=1)
    try:
        return json.loads(out or "null")
    except ValueError as exc:
        raise QaDeskError(f"qa-desk answered something that is not JSON ({exc}).",
                          code=1) from exc


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
        raise QaDeskError(f"QA Desk answered case {key} in {version} without the case.")
    return found


def release(version: str) -> dict:
    """`qa-desk release show <version> --json`."""
    found = run(["release", "show", version])
    if not isinstance(found, dict):
        raise QaDeskError(f"QA Desk answered release {version} with no release.")
    return found


# --- the drafts -------------------------------------------------------------


def dry_run(command: list[str]) -> dict:
    """Run `command` — the words after `qa-desk` — as a dry run, and return the
    request QA Desk's CLI says it would have sent. Nothing is sent."""
    found = run([*command, DRY_RUN])
    if not isinstance(found, dict) or not found.get("dry_run"):
        raise QaDeskError("qa-desk did not answer the dry run as a dry run, so "
                          "nothing it said is shown.", code=1)
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
