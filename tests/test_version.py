"""A QA tool has to be able to say what it is.

Every result this thing produces gets pasted into a bug report, and a report that
cannot name the build that produced it is an anecdote. `comfy-qat --version` is
the line that gets pasted, so it is tested like the output it is: the number comes
from one place, the commit appears when there is one, and neither goes missing.
"""

from __future__ import annotations

import re

import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

import comfy_qa
from comfy_qa import BINARY, DIST_NAME, git_sha, version_string
from comfy_qa.cli import app

ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


def declared_version() -> str:
    """The version in pyproject.toml — the only place it is written down."""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def test_version_flag_prints_the_binary_and_the_version():
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0, result.output
    assert result.output.startswith(f"{BINARY} {declared_version()}")


def test_version_flag_needs_no_subcommand():
    """`--version` is eager: it answers before argument parsing can complain."""
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "Usage:" not in result.output


def test_the_version_is_read_from_metadata_not_restated_in_python():
    """Two copies of a version number means one of them is wrong, later."""
    assert comfy_qa.__version__ == declared_version()

    literal = declared_version()
    for source in (ROOT / "comfy_qa").glob("*.py"):
        assert literal not in source.read_text(), (
            f"{source.name} hard-codes the version; pyproject.toml is the one place for it"
        )


def test_the_version_is_a_real_number_not_the_unknown_fallback():
    assert comfy_qa.__version__ != "unknown"


def test_a_checkout_reports_its_commit():
    """The half a tester actually needs: which commit produced this result."""
    sha = git_sha()
    assert sha, "this checkout is a git repo, so --version must carry its commit"

    expected = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert sha == expected
    assert version_string() == f"{BINARY} {comfy_qa.__version__} ({sha})"


def test_an_installed_copy_reports_no_commit(tmp_path, monkeypatch):
    """site-packages is not a git repo. A released build prints a version, full stop."""
    assert git_sha(tmp_path) is None

    monkeypatch.setattr(comfy_qa, "_ROOT", tmp_path)
    assert version_string() == f"{BINARY} {comfy_qa.__version__}"


def test_git_sha_survives_a_directory_that_only_looks_like_a_repo(tmp_path):
    """A stray `.git` must not turn `--version` into a traceback."""
    (tmp_path / ".git").write_text("not a git file")

    assert git_sha(tmp_path) is None


def test_a_checkout_beats_stale_installed_metadata():
    """Editable installs freeze their metadata at install time.

    Everyone working on this runs one, so reading metadata first would make
    `--version` name whichever release happened to be current the day they ran
    `pip install -e .` — a wrong answer that looks exactly like a right one.
    """
    assert comfy_qa._version_from_checkout(ROOT) == declared_version()


@pytest.mark.parametrize("root_has", ["nothing", "someone else's pyproject"])
def test_a_non_checkout_falls_through_to_metadata(tmp_path, root_has):
    if root_has != "nothing":
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "something-else"\nversion = "9.9"\n')

    assert comfy_qa._version_from_checkout(tmp_path) is None


def test_the_module_entry_point_can_also_say_what_it_is():
    """`python -m comfy_qa --version` — the form that works before an install."""
    done = subprocess.run(
        [sys.executable, "-m", "comfy_qa", "--version"],
        capture_output=True, text=True, cwd=ROOT, check=False,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().startswith(f"{BINARY} {declared_version()}")


def test_the_distribution_name_matches_pyproject():
    """`--version` names the binary; `pip uninstall` needs the distribution."""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)

    assert DIST_NAME == pyproject["project"]["name"]
    assert BINARY in pyproject["project"]["scripts"]


def test_the_changelog_names_the_version_that_ships():
    """A release heading of 'Unreleased' next to a 1.0.0 build helps nobody."""
    text = (ROOT / "CHANGELOG.md").read_text()
    headings = [line for line in text.splitlines() if line.startswith("## ")]

    assert headings, "the changelog has no release headings"
    assert declared_version() in headings[0], (
        f"newest changelog heading is {headings[0]!r}, but this builds as "
        f"{declared_version()}"
    )


def _plain(text: str) -> str:
    """Help output as words, with the box drawing and the wrapping taken out.

    Rich renders `--help` into a panel whose width comes from the terminal, and
    CI has no terminal. This assertion passed locally and failed on all four CI
    jobs because the flag had been wrapped onto its own line inside a box. The
    test was checking the rendering, not the fact.
    """
    without_ansi = re.sub(r"\x1b\[[0-9;]*m", "", text)
    without_box = re.sub(r"[│╭╮╰╯─┃━]", " ", without_ansi)
    return " ".join(without_box.split())


def test_help_documents_the_version_flag():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "--version" in _plain(result.output)


def test_the_version_flag_works_at_any_terminal_width():
    """The flag itself, not its help rendering — this is the fact that matters."""
    for width in ("40", "80", "200"):
        result = runner.invoke(app, ["--version"], env={"COLUMNS": width})
        assert result.exit_code == 0, f"at COLUMNS={width}: {result.output}"
        assert "comfy-qat" in _plain(result.output)
