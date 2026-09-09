"""comfy-qa-tools — QA tooling for setting up and running testing across Comfy.

The version lives in exactly one place: `version` in `pyproject.toml`. Installed
copies read it back out of their own package metadata; a checkout reads it out of
the `pyproject.toml` sitting next to this file. Nothing here restates the number,
because the one thing worse than a tool with no version is a tool with two.
"""

from __future__ import annotations

import os
import subprocess
import tomllib
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version
from pathlib import Path

#: The private Typer switch that turns its Rich rendering off. Private, and set
#: here anyway, for the reason `say.py` gives for not reaching into Typer
#: elsewhere: there is no public seam. `rich_markup_mode=None` on every
#: `typer.Typer()` in the package takes the help LAYOUT with it and the next
#: sub-app added silently opts back in; this leaves the layout alone and removes
#: only the colour. Measured on a pty: 348 escapes to 0, and the help is the same
#: shape either way.
_TYPER_OFF = "_TYPER_FORCE_DISABLE_TERMINAL"

# NO_COLOR, honoured. https://no-color.org — any non-empty value, set by anyone
# who cannot read low-contrast text, and by nearly every CI runner.
#
# `say` never colours anything and `tests/test_no_colour.py` holds it to that,
# so this is about the one part of the output that does: `comfy-qat --help`,
# rendered by Typer through Rich, which came out in 348 SGR escapes on a
# terminal WITH NO_COLOR SET. Rich honours the variable; Typer builds its own
# Console and does not pass it on.
#
# Here, and not in `cli.py`, because `typer.rich_utils` reads its environment
# once at import into module-level constants. `cli.py` imports typer at the top
# of the file, so anything it sets afterwards is already too late — and this
# module is imported before any of its submodules, which makes it the only place
# early enough.
#
# Set rather than overwritten: someone who has already disabled Typer's terminal
# deliberately keeps that, and `NO_COLOR=` empty means unset per the standard.
if os.environ.get("NO_COLOR") and _TYPER_OFF not in os.environ:
    os.environ[_TYPER_OFF] = "1"

#: The distribution on PyPI/`pip list`.
DIST_NAME = "comfy-qa-tools"
#: The console script a tester actually types, and the name `--version` leads with.
BINARY = "comfy-qat"

_ROOT = Path(__file__).resolve().parent.parent


def _version_from_checkout(root: Path | None = None) -> str | None:
    """The version this tree *would* install as, read from its own pyproject.

    A checkout deliberately beats installed metadata. Everyone working on this
    runs an editable install, and editable metadata is frozen at whatever the
    version was on the day it was installed — so `--version` would otherwise name
    a release that is not the code in front of you. A wheel has no pyproject.toml,
    so installed copies fall through to metadata, which is correct for them.
    """
    root = root or _ROOT
    try:
        with (root / "pyproject.toml").open("rb") as handle:
            project = tomllib.load(handle)["project"]
        # Guard against picking up an unrelated pyproject.toml that happens to sit
        # above the package (site-packages, a vendored tree).
        if project["name"] != DIST_NAME:
            return None
        return str(project["version"])
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return None


def _resolve_version() -> str:
    from_checkout = _version_from_checkout()
    if from_checkout:
        return from_checkout
    try:
        return _installed_version(DIST_NAME)
    except PackageNotFoundError:
        # Neither installed nor a checkout — a directory someone copied. Say so,
        # rather than invent a number that would then be quoted in a bug report.
        return "unknown"


def git_sha(root: Path | None = None) -> str | None:
    """Short commit of the checkout this package is running from, or None.

    None is the normal answer for an installed copy: site-packages is not a git
    repo, so a released build prints its version and nothing else.
    """
    root = root or _ROOT
    # A worktree's .git is a file, not a directory, so test existence not type.
    if not (root / ".git").exists():
        return None
    try:
        done = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None  # No git on PATH, or it hung. Not worth failing --version over.
    if done.returncode != 0:
        return None
    return done.stdout.strip() or None


def version_string() -> str:
    """The line a tester pastes into a report: what ran, and which commit."""
    sha = git_sha()
    return f"{BINARY} {__version__}" + (f" ({sha})" if sha else "")


__version__ = _resolve_version()

from .cli import register  # noqa: E402,F401
