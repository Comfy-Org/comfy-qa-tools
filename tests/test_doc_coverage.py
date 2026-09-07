"""Every command the tool advertises is documented, and the pack actually runs it.

`tests/test_readme.py` has held one page to that rule since `auth`, `up`, `open`,
`down` and `stamp` all shipped while the README still listed them under "still to
come". It worked — the README is the only page that has stayed right. That is the
whole finding: the page with a test was correct and the pages without one drifted,
and by the time anyone looked, `ssh`, `rdp`, `delete` and `disconnect` were missing
from the command reference, missing from the everyday-loop page, and — worse —
missing from the acceptance pack, which meant a tester could complete it, sign off
a release, and never once exercise the only command here that cannot be undone.

Nothing failed when they were left out. That is what this file is for.

Two different bars, because the pages do different jobs:

  - `docs/commands.md` is a reference table, so the bar is the same as the
    README's: a table row, not a mention. A sentence saying a command does not
    exist satisfies a substring search just as well as one documenting it, which
    is exactly how the README check used to pass while being wrong.

  - `docs/test-criteria.md` is a run sheet, so the bar is that the pack **runs**
    the command — `qat <name>` inside a shell block. A criterion describing a
    command the block never types is the failure this file was written for, in
    its purest form: G6 required `down --all` for a release-1 sign-off while
    phase G's block contained no such command, so the box was ticked on faith.

`docs/machines.md` is deliberately NOT held to this. It is narrative — "how do I
get onto the box" — and demanding all 22 command paths appear in it would be a bar
that teaches people to paste command rows into prose to make a test pass. It gets
prose about the four commands it was missing, and no test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer

from comfy_qa.cli import app

DOCS = Path(__file__).resolve().parent.parent / "docs"
COMMANDS_PAGE = DOCS / "commands.md"
PACK = DOCS / "test-criteria.md"


def _surface(typer_app: typer.Typer, prefix: str = "") -> list[str]:
    """Every command path this tool advertises, e.g. 'quota request'.

    Hidden ones are skipped: `host go` and `auth status` are the old spellings,
    kept working so nothing written down before the verbs moved breaks, and `env`
    belongs to a different tool. Documenting either would document the same
    command twice.
    """
    found = []
    for command in typer_app.registered_commands:
        if getattr(command, "hidden", False):
            continue
        found.append(f"{prefix}{command.name}".strip())
    for group in typer_app.registered_groups:
        if getattr(group, "hidden", False):
            continue
        found.extend(_surface(group.typer_instance, prefix=f"{prefix}{group.name} "))
    return found


COMMANDS = _surface(app)


def test_the_surface_is_not_empty():
    """A bug in _surface would make every test here pass vacuously."""
    assert {"delete", "ssh", "rdp", "disconnect", "quota request"} <= set(COMMANDS)


@pytest.mark.parametrize("command", COMMANDS)
def test_every_command_has_a_row_in_the_command_reference(command):
    rows = [line for line in COMMANDS_PAGE.read_text().splitlines()
            if line.lstrip().startswith("|")]
    assert any(f"comfy-qat {command}" in row for row in rows), (
        f"`comfy-qat {command}` exists but docs/commands.md has no table row for it"
    )


def _pack_shell() -> str:
    """Everything the acceptance pack tells a tester to type.

    Only fenced `sh` blocks — prose about a command is not a run of it, and the
    gap this file exists for was precisely criteria that described more than
    their block executed.
    """
    return "\n".join(re.findall(r"```sh\n(.*?)```", PACK.read_text(), re.S))


@pytest.mark.parametrize("command", COMMANDS)
def test_the_acceptance_pack_runs_every_command(command):
    """`qat` is the alias the pack's own preamble defines."""
    assert f"qat {command}" in _pack_shell(), (
        f"docs/test-criteria.md never runs `qat {command}`, so a tester can "
        f"complete the pack without exercising it"
    )


def test_a_mention_is_not_a_run():
    """The guard on the guard: prose about a command must not satisfy the check
    above, since a criterion without a command to run is the whole defect."""
    prose = "- [ ] **N1** — `comfy-qat delete windows` is refused."
    assert not re.findall(r"```sh\n(.*?)```", prose, re.S)


def test_the_pack_does_not_tell_the_tester_to_stay_quiet():
    """A pack line converting a live defect into "do not report" is worse than a
    wrong criterion.

    This one is real: the pack carried "that is a cosmetic lag rather than a
    defect, but do not mark it as a failure" about the old `comfy-qat host ...`
    spellings, naming three examples that had all been fixed — while three that
    were still live went unnamed. A tester who found a real one had been
    pre-instructed to ignore it.

    Deciding a defect does not block a release is fine, and the sign-off says so
    for A8 by name. Telling the tester not to see it is not.
    """
    text = PACK.read_text().lower()
    for phrase in ("do not mark it as a failure",
                   "do not report",
                   "rather than a defect"):
        assert phrase not in text, f"the pack tells the tester to ignore something: {phrase!r}"
