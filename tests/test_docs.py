"""The docs are part of the tool, so they are tested like it.

The rule we hold ourselves to: an error and its troubleshooting entry get written
together. A command whose failure modes cannot be documented is a command that is
not understood yet. This test is what stops that rule quietly lapsing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parent.parent / "docs"

# Phrases the code can actually print. Each must be findable by someone who pasted
# the error into the troubleshooting page.
ERROR_PHRASES = [
    "no host list at",
    "reserved for the local ComfyUI",
    "both use port",
    "kind must be",
    "unknown field",
    "requires an explicit port",
    "outside 1024-65535",
    "gcloud is not installed",
    "your gcloud session has expired",
    "no active gcloud account",
    "no project set",
    "no billing account linked",
    "zero GPU quota",
    "still pending",
    "gcloud timed out",
    # setup
    "not signed in to Google Cloud",
    "sign-in did not complete",
    "this account has no Google Cloud projects",
    "no project set and",
    "no billing account is linked",
    # gpu quota
    "reports no quota for",
    "still pending",
    # stamping
    "nothing answered at",
    "not with ComfyUI",
    # installing
    "command not found",
    "could not read GPU quota",
    "could not list cloud boxes",
    # starting and stopping
    "ComfyUI is not answering",
    "did not reach RUNNING",
    "could not start",
    "could not run a command on",
    "did not finish",
]


@pytest.mark.parametrize("name", ["getting-started", "hosts", "troubleshooting", "cost"])
def test_page_exists_and_is_not_a_stub(name):
    page = DOCS / f"{name}.md"
    assert page.exists(), f"docs/{name}.md is missing"
    assert len(page.read_text().split()) > 100, f"docs/{name}.md is a stub"


@pytest.mark.parametrize("phrase", ERROR_PHRASES)
def test_every_error_has_a_troubleshooting_entry(phrase):
    text = (DOCS / "troubleshooting.md").read_text()
    assert phrase in text, (
        f"{phrase!r} can be printed by the tool but is not in troubleshooting.md"
    )


def test_docs_do_not_reference_the_old_command_name():
    """The binary is comfy-qat; comfy-qa is a different project's binary."""
    for page in DOCS.glob("*.md"):
        for line in page.read_text().splitlines():
            assert "comfy-qa " not in line, f"{page.name}: stale command name in {line!r}"


def test_guide_leads_with_the_single_setup_command():
    """Setup is one command. If guide ever lists steps again, this fails."""
    from comfy_qa.cli import FIRST_RUN

    assert "comfy-qat setup" in FIRST_RUN
    for follow_up in ["host list", "auth status"]:
        assert follow_up in FIRST_RUN


def test_getting_started_leads_with_setup_not_a_command_list():
    text = (DOCS / "getting-started.md").read_text()
    assert "comfy-qat setup" in text
    assert text.index("comfy-qat setup") < text.index("comfy-qat host list")


def test_the_module_entry_point_exposes_the_current_surface():
    """`python -m comfy_qa` ran v0's surface long after v0 stopped being the tool.

    An entry point that quietly points at old code is the kind of thing nobody
    notices until they use it.
    """
    from comfy_qa import cli

    # Importing __main__ would run the CLI, so read it instead.
    entry = (DOCS.parent / "comfy_qa" / "__main__.py").read_text()
    assert "from .cli import main" in entry, "the module entry point still points at v0"

    names = {command.name for command in cli.app.registered_commands}
    groups = {group.name for group in cli.app.registered_groups}
    assert {"setup", "guide", "env"} <= names
    assert {"host", "auth"} <= groups


def test_the_package_register_is_the_current_surface_not_v0():
    import comfy_qa
    from comfy_qa.cli import register

    assert comfy_qa.register is register


def test_no_superseded_planning_documents_remain():
    """ROADMAP.md and DEVELOPMENT.md described a scope that no longer exists.

    Features are documented when they ship; a stale plan in the repo root reads as
    current to anyone who has not been in the conversation.
    """
    root = DOCS.parent
    for name in ("ROADMAP.md", "DEVELOPMENT.md"):
        assert not (root / name).exists(), f"{name} is superseded and should be gone"
