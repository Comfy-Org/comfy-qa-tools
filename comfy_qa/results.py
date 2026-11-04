"""A pass or a fail, kept here — and what QA Desk would be told, as a draft.

A RESULT IS KEPT WHETHER OR NOT THERE IS A QA DESK CASE. Testing starts from
anything (a PR, a version, a release, a case), so the record of what was found
cannot depend on QA Desk: `pass` and `fail` write a local result — what was
tested, where, the full stamp of the place, the time, a note, and a Linear issue
or bug reference if one was given — into comfy-qat's own folder, beside the host
list (`results/`, one JSON file per result), never into a repository. `results`
lists them.

FOR A QA DESK CASE, THE COMMAND IS SHOWN AND ONLY ITS DRY RUN IS RUN. The draft
is the exact `qa-desk` command, built from the stamp:

    a pass   case pass <v> <case> --on=local|testcloud [--core-tag=<tag>] --note=<stamp>
    a fail   case fail <v> <case> --bug="<what broke> — <stamp>" --on=...

because `case fail` takes no `--note`, so the stamp goes into the bug's text,
and `--core-tag` is the frontend release the machine was serving, read off the
stamp. A recorded Playwright test PR is `test report <v> <pr> --cases=…`. Each is
printed whole, for the person to read and paste, and then run with `--dry-run`,
which QA Desk's CLI answers with the request it would have sent. Nothing is ever
sent to QA Desk from here (`qadesk.run` refuses it), and nothing to Linear: a
Linear issue is kept as text.

EVERY VALUE IS ATTACHED TO ITS FLAG WITH `=` (`qadesk.flag`). The text is the
tester's — "--frontend-version is ignored by the picker" is a fair note — and
QA Desk's CLI reads `--note <text>` with a text that starts with `--` as a
`--note` with no value: a pass sent no note and no stamp, and a fail was exit 6,
`--bug is required` (code audit T2, 4 Nov). After an `=` the CLI keeps the
value word for word.

A BUG IS DRAFTED, NEVER FILED. `bug` writes the house format — a title, one
impersonal paragraph, and an environment block built from the stamp at the
moment it is drafted — as text for Linear or #bug-dump, with the recording to
attach when there is one. A person sends it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

FOLDER = "results"
RECORDINGS = "recordings"

# A Linear issue id, as QA Desk's `--linear` takes one: FE-1234.
_LINEAR = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def folder(config: Path | None) -> Path:
    """comfy-qat's own folder for results: beside the host list in use."""
    from .config import DEFAULT_CONFIG_PATH

    return (config or DEFAULT_CONFIG_PATH).parent / FOLDER


@dataclass
class Result:
    outcome: str                        # "pass" or "fail"
    what: str                           # what was tested, in words
    place: str                          # where: a machine or a cloud environment
    place_kind: str                     # "machine" or "cloud"
    at: str                             # when, UTC, ISO 8601
    stamp: dict = field(default_factory=dict)
    stamp_line: str = ""
    note: str = ""
    bug: str | None = None              # a Linear issue or a bug reference, as text
    release: str | None = None
    case: str | None = None
    test_target: str | None = None
    comfy_qat: str = ""

    @property
    def linear(self) -> str | None:
        return self.bug if self.bug and _LINEAR.match(self.bug) else None


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def save(result: Result, config: Path | None) -> Path:
    """Write one result, whole or not at all. Returns where it went."""
    where = folder(config)
    where.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9.-]+", "-", f"{result.what}-{result.place}").strip("-")[:60]
    # Microseconds in the name, so the files sort in the order they were kept
    # even within one second; `at` is what the result says, to the second.
    written = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    stem = f"{written}-{result.outcome}-{slug}"
    path = where / f"{stem}.json"
    count = 1
    while path.exists():
        count += 1
        path = where / f"{stem}-{count}.json"
    partial = path.with_suffix(".json.partial")
    partial.write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False) + "\n",
                       encoding="utf-8")
    os.replace(partial, path)
    return path


def load_all(config: Path | None) -> list[tuple[Path, Result]]:
    """Every result kept, oldest first. A file that does not read is skipped
    and named by `unreadable`, rather than taking the list down with it."""
    found = []
    where = folder(config)
    if not where.is_dir():
        return found
    for path in sorted(where.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            found.append((path, Result(**data)))
        except (OSError, ValueError, TypeError):
            continue
    return found


def unreadable(config: Path | None) -> list[Path]:
    where = folder(config)
    if not where.is_dir():
        return []
    read = {path for path, _ in load_all(config)}
    return [path for path in sorted(where.glob("*.json")) if path not in read]


# --- the QA Desk drafts ---------------------------------------------------------


def on_for(result: Result) -> str:
    from .places import ON_LOCAL, ON_TESTCLOUD

    return ON_TESTCLOUD if result.place_kind == "cloud" else ON_LOCAL


def core_tag_of(result: Result) -> str | None:
    """The frontend release a machine was serving, as QA Desk's `--core-tag`."""
    served = str(result.stamp.get("frontend_version") or "")
    if result.place_kind != "machine" or not re.match(r"^\d+\.\d+\.\d+", served):
        return None
    return f"v{served}"


def pass_command(result: Result) -> list[str]:
    from .qadesk import flag

    command = ["case", "pass", result.release, result.case, flag("on", on_for(result))]
    tag = core_tag_of(result)
    if tag:
        command.append(flag("core-tag", tag))
    note = result.stamp_line
    if result.note:
        note = f"{result.note} — {note}"
    if result.bug:
        note = f"{note} — {result.bug}"
    return command + [flag("note", note)]


def fail_command(result: Result) -> list[str]:
    from .qadesk import flag

    broke = result.note or "failed"
    command = ["case", "fail", result.release, result.case,
               flag("bug", f"{broke} — {result.stamp_line}"), flag("on", on_for(result))]
    tag = core_tag_of(result)
    if tag:
        command.append(flag("core-tag", tag))
    if result.linear:
        command.append(flag("linear", result.linear))
    return command


def report_command(version: str, pr: str, cases: str, *, bug: str | None = None,
                   note: str | None = None) -> list[str]:
    from .qadesk import flag

    command = ["test", "report", version, pr, flag("cases", cases)]
    if bug:
        command.append(flag("bug", bug))
    if note:
        command.append(flag("note", note))
    return command


def printed(command: list[str]) -> str:
    """The command as a person pastes it."""
    from .qadesk import printed as shown

    return shown(command)


# --- the bug draft ---------------------------------------------------------------


def bug_draft(*, title: str, happened: str, expected: str | None, what: str | None,
              place: str, stamp_line: str, at: str, version: str,
              recording: list[str] | None = None) -> list[str]:
    """Title, one impersonal paragraph, and the environment block — the house
    format (a title, one paragraph saying what happened against what was
    expected, then the environment read at the moment of writing)."""
    sentences = [_sentence(happened)]
    if expected:
        sentences.append(_sentence(f"Expected: {expected}"))
    context = f"Found on {place}"
    if what:
        context = f"Found testing {what} on {place}"
    sentences.append(f"{context}, {at[:10]}.")
    lines = [title.strip(), "", " ".join(sentences), "",
             f"{version} · {stamp_line} · {at}"]
    for path in recording or []:
        lines.append(f"recording to attach: {path}")
    return lines


def _sentence(text: str) -> str:
    text = " ".join(str(text).split())
    return text if text.endswith((".", "!", "?")) else f"{text}."


# --- recordings, for the bug draft ------------------------------------------------


def recordings_file(config: Path | None) -> Path:
    from .config import DEFAULT_CONFIG_PATH

    return (config or DEFAULT_CONFIG_PATH).parent / RECORDINGS / "recordings.json"


def remember_recording(config: Path | None, place: str, paths: list[str]) -> None:
    """What the last recording on `place` wrote, so a bug drafted after it can
    name the file to attach."""
    if not paths:
        return
    where = recordings_file(config)
    where.parent.mkdir(parents=True, exist_ok=True)
    try:
        known = json.loads(where.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        known = {}
    if not isinstance(known, dict):
        known = {}
    known[place] = {"at": now(), "paths": list(paths)}
    partial = where.with_suffix(".json.partial")
    partial.write_text(json.dumps(known, indent=2) + "\n", encoding="utf-8")
    os.replace(partial, where)


def last_recording(config: Path | None, place: str) -> list[str]:
    try:
        known = json.loads(recordings_file(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entry = known.get(place) if isinstance(known, dict) else None
    paths = entry.get("paths") if isinstance(entry, dict) else None
    return [str(path) for path in paths if Path(str(path)).exists()] \
        if isinstance(paths, list) else []
