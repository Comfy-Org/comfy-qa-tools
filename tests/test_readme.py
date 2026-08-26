"""The README is the status page, so it is tested like one.

This file exists because of a specific failure: `auth`, `host up`, `open`, `down`
and `stamp` all shipped while the README still listed them under "still to come".
Nothing was broken, and nothing caught it — the page was simply a description of
an older tool, which is worse than no page at all, because it reads as current.

The rule these tests hold: every command the binary has appears in the README, and
every `comfy-qat` command the README shows exists in the binary.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer

from comfy_qa.cli import app

README = Path(__file__).resolve().parent.parent / "README.md"

# Documented as deliberately absent, with a reason, rather than silently missing.
NOT_BUILT = {"host create"}


def _surface(typer_app: typer.Typer, prefix: str = "") -> list[str]:
    """Every invocable command path, e.g. 'auth quota request'."""
    found = []
    for command in typer_app.registered_commands:
        found.append(f"{prefix}{command.name}".strip())
    for group in typer_app.registered_groups:
        found.extend(_surface(group.typer_instance, prefix=f"{prefix}{group.name} "))
    return found


COMMANDS = _surface(app)


def test_the_surface_is_not_empty():
    """A bug in _surface would make every other test here pass vacuously."""
    assert {"setup", "guide", "env", "host list", "auth quota request"} <= set(COMMANDS)


@pytest.mark.parametrize("command", COMMANDS)
def test_every_command_is_in_the_readme(command):
    text = README.read_text()
    assert f"comfy-qat {command}" in text, (
        f"`comfy-qat {command}` exists but the README never mentions it"
    )


def test_the_readme_invents_no_commands():
    """The other direction: a promised command that does not exist is a lie."""
    text = README.read_text()
    known = set(COMMANDS)
    for raw in re.findall(r"comfy-qat ([a-z][a-z-]*(?: [a-z][a-z-]*)?)", text):
        if raw in NOT_BUILT:
            continue
        head = raw.split()[0]
        assert raw in known or head in known or any(
            c.startswith(f"{raw} ") for c in known
        ), f"README shows `comfy-qat {raw}`, which the binary does not have"


def test_host_create_is_named_as_not_built():
    """It was 'coming next' for three releases. Say so plainly instead."""
    text = README.read_text()
    assert "host create" in text
    assert "not built" in text.lower()


def test_the_readme_points_at_the_real_config_path():
    """It showed ~/.config/comfy-qa/hosts.toml, which nothing ever writes."""
    from comfy_qa.config import DEFAULT_CONFIG_PATH

    text = README.read_text()
    assert "comfy-qa-tools/hosts.toml" in text
    assert DEFAULT_CONFIG_PATH.name == "hosts.toml"
    assert "comfy-qa/hosts.toml" not in text


def test_the_readme_describes_everything_the_tool_writes():
    """`What it writes` said 'one file' after tunnels started leaving two more."""
    text = README.read_text()
    section = text[text.index("## What it writes"):]
    for artefact in ["hosts.toml", "tunnels/", ".pid", ".log"]:
        assert artefact in section, f"{artefact} is written but not declared"
