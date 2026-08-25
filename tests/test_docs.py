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


def test_guide_names_the_four_first_run_steps():
    from comfy_qa.cli import FIRST_RUN

    for step in ["gcloud auth login", "auth status", "host init", "host list"]:
        assert step in FIRST_RUN
