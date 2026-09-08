"""Every option and argument says what it is.

`--config` is declared seventeen times. For most of the tool's life fifteen of
those carried no `help=` at all, so `comfy-qat up --help` printed

    --config PATH

and nothing else — no statement that it is the host list, and no mention of the
file it falls back to when you leave it off. The two declarations that were
written properly were on `list` and on the `host` callback, which is precisely
the shape of drift this suite keeps finding: the site somebody looked at is
right, and the fourteen copied from it are not.

Nothing failed while that was true. `--help` rendered, the option worked, and the
only cost was paid by a person reading the help of a command that quietly depends
on a file in a directory they have never opened.

So the list of sites is read out of the source rather than typed here. A
hand-maintained list would have to be extended by whoever adds the eighteenth
`--config`, which is the same person who just forgot the `help=` — the omission
and the thing that would catch it are the same act of forgetting. Walking the AST
of every module in `comfy_qa/` means a new `--config` is in scope the moment it is
written, and there is nothing to remember.

That was the first version of this file, and it collected only `--config`. Which
made it a guard that would catch the eighteenth bare `--config` and not the
fourth bare anything-else — and the fourth bare anything-else was already there,
three of it, in `auth.py`: `quota list --json`, and `quota request --region` and
`--justification`, each rendering as a flag with an empty description column. The
collector now walks every `typer.Option` and `typer.Argument` in the package,
because the defect was never about `--config`; `--config` is just where it was
noticed.

The bar is deliberately two things, not one:

  - a `help=` at all, which is the defect above; and
  - any help that names the host list names the *real* default, derived from
    `DEFAULT_CONFIG_PATH`. Help text that states a path is a promise about where
    the tool looks, and a promise nothing checks is how it comes to name a
    directory the tool stopped using two refactors ago.

`init` is held to the first bar and not the second: it writes the starter file
rather than reading one, and its help says so without quoting a path.

A `hidden=True` option is in scope, and that is a choice rather than an
oversight — nothing here reads `hidden=`. Hiding an option removes it from
`--help`; it does not remove it from the source, and it does not stop the
spelling working for everyone who already types it. A deprecation window is in
fact when the help matters most, because the one thing the reader needs is what
to use instead — which is why the hidden `--os` and `--gpu` kept theirs, and why
`stop --keep-running` spends its whole help naming its replacement. An option too
undocumented to describe should be deleted, not hidden.

Presence and readability are separate questions, and widening the collector is
what made the difference matter. `env`'s first argument builds its help from an
f-string, so there is no string literal in the AST to read; that is help, and
it passes the first bar. Only what can be read as a literal is held to the
second, so an f-string is skipped there rather than silently passing a path
claim nobody checked. A missing `help=` is the defect; an unreadable one is not.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple, Optional

import pytest

from comfy_qa.config import DEFAULT_CONFIG_PATH

PACKAGE = Path(__file__).resolve().parent.parent / "comfy_qa"

# What the help text is allowed to claim the default is. Spelled from the real
# constant, so moving the host list moves this and the help that disagrees fails.
DEFAULT_AS_WRITTEN = "~/" + DEFAULT_CONFIG_PATH.relative_to(Path.home()).as_posix()

# The anti-vacuity floors. A collector that matched nothing would make every
# parametrized test below pass by having no cases at all, which is the failure
# mode of a derived list and is worse than the hand-typed one it replaced. The
# package declares 106 options and arguments today, 17 of them `--config`.
#
# Both floors sit well under those counts on purpose. They are defending against
# a collector that has stopped working — a renamed `typer.Option`, a moved
# module, a glob that matches nothing — and they are deliberately NOT a census of
# the interface. Retiring a command, or hoisting `--config` onto the root
# callback so it is declared once instead of seventeen times, is a correct change
# and must not be reported as a broken guard. A floor set to the exact current
# count does exactly that: `CONFIG_FLOOR = 17` turned removing one `--config`
# into "the collector has stopped seeing them", which is the confusion this file
# exists to avoid.
DECLARATION_FLOOR = 90
CONFIG_FLOOR = 14


class Decl(NamedTuple):
    """One `typer.Option(...)` / `typer.Argument(...)` as it appears in the source.

    `has_help` is whether a `help=` keyword is written at all — the bar that
    matters. `text` is that help when it can be read as a literal, and None when
    it cannot, which is a different thing from absent and is treated as one.
    """

    module: str
    line: int
    flags: tuple[str, ...]
    has_help: bool
    text: Optional[str]

    @property
    def name(self) -> str:
        """What to call this site in a failure. A positional argument has no flag."""
        return self.flags[0] if self.flags else "<argument>"


def _is_typer_declaration(node: ast.Call) -> bool:
    """`typer.Option(...)` / `typer.Argument(...)` however they were imported.

    Both spellings are live in this package, and a check that only knew the
    dotted one would go quiet the day somebody writes `from typer import Option`.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr in ("Option", "Argument")
    return isinstance(func, ast.Name) and func.id in ("Option", "Argument")


def _help_of(node: ast.Call) -> tuple[bool, Optional[str]]:
    """(is there a `help=`, what does it say) for one declaration.

    An f-string help is present but only partly readable: the literal pieces are
    returned and the interpolations are not, which is enough to hold it to the
    path check for anything it spells out itself.
    """
    for keyword in node.keywords:
        if keyword.arg != "help":
            continue
        value = keyword.value
        if isinstance(value, ast.Constant):
            return True, value.value if isinstance(value.value, str) else None
        if isinstance(value, ast.JoinedStr):
            literal = "".join(
                part.value for part in value.values
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
            return True, literal or None
        # A name, a call, a conditional: help exists, this file cannot read it.
        return True, None
    return False, None


def declarations(tree: ast.AST, module: str) -> list[Decl]:
    """Every option and argument declared in one parsed module."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_typer_declaration(node):
            continue
        flags = tuple(arg.value for arg in node.args
                      if isinstance(arg, ast.Constant) and isinstance(arg.value, str))
        has_help, text = _help_of(node)
        found.append(Decl(module, node.lineno, flags, has_help, text))
    return found


def _sites() -> list[Decl]:
    found = []
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.extend(declarations(tree, path.name))
    return found


SITES = _sites()
CONFIG_SITES = [site for site in SITES if "--config" in site.flags]

IDS = [f"{site.module}:{site.line}:{site.name}" for site in SITES]
CONFIG_IDS = [f"{site.module}:{site.line}" for site in CONFIG_SITES]


def test_the_declarations_are_actually_being_found():
    """A collector that matches nothing would make every test below pass.

    That is the failure mode of a derived list, and it is worse than the defect
    it replaced: a hand-typed list at least fails loudly when it goes stale.
    """
    assert len(SITES) >= DECLARATION_FLOOR, (
        f"only {len(SITES)} option/argument declarations found, under the floor of "
        f"{DECLARATION_FLOOR} — the collector has stopped seeing them, not the "
        f"package that has shed a fifth of its interface"
    )
    assert {"auth.py", "commands.py", "host.py", "remove.py"} <= {s.module for s in SITES}


def test_the_config_declarations_are_still_being_found():
    """The `--config` subset has its own floor.

    It is a filter over the sweep above, and a filter that matched nothing would
    be invisible there: the total would still be in the hundreds while the two
    `--config` tests quietly ran on an empty list.
    """
    assert len(CONFIG_SITES) >= CONFIG_FLOOR, (
        f"only {len(CONFIG_SITES)} `--config` declarations found, under the floor of "
        f"{CONFIG_FLOOR}. This floor is a check on the collector, not a headcount of "
        f"the interface — if `--config` has genuinely been hoisted onto one callback "
        f"or a command has been retired, lower it (or key it on `--config` being "
        f"declared at all) rather than putting the declarations back"
    )
    assert {"host.py", "remove.py"} <= {s.module for s in CONFIG_SITES}


def test_a_bare_declaration_is_recognised_as_bare():
    """The guard on the guard: the exact source that was wrong must still read as
    wrong, or the sweep above passes because it stopped seeing anything."""
    tree = ast.parse(
        'config: Annotated[Optional[Path], typer.Option("--config")] = None\n'
        'name: Annotated[Optional[str], typer.Argument()] = None\n'
        'gpu: Annotated[Optional[str], typer.Option("--gpu", help="The card.")] = None\n'
    )
    assert declarations(tree, "sample.py") == [
        Decl("sample.py", 1, ("--config",), False, None),
        Decl("sample.py", 2, (), False, None),
        Decl("sample.py", 3, ("--gpu",), True, "The card."),
    ]


def test_a_computed_help_counts_as_help_but_is_not_read():
    """Present and unreadable is not the defect. `env`'s first argument is this."""
    tree = ast.parse(
        'targets: Annotated[Optional[list[str]], typer.Argument(\n'
        '    help="Environments to check. Default: all of "\n'
        '         f"{names} plus local.")] = None\n'
    )
    site, = declarations(tree, "sample.py")
    assert site.has_help
    assert site.text == "Environments to check. Default: all of  plus local."


@pytest.mark.parametrize("site", SITES, ids=IDS)
def test_every_option_carries_help(site: Decl):
    assert site.has_help, (
        f"{site.module}:{site.line} declares {site.name} with no help=, so `--help` "
        f"renders it with an empty description column. Say what it is and what "
        f"passing it does."
    )
    if site.text is not None:
        assert site.text.strip(), f"{site.module}:{site.line} gives {site.name} empty help"


@pytest.mark.parametrize("site", CONFIG_SITES, ids=CONFIG_IDS)
def test_help_that_names_the_host_list_names_the_real_one(site: Decl):
    """Only the sites that quote a path are held to this — see the module docstring."""
    if not site.text or DEFAULT_CONFIG_PATH.name not in site.text:
        pytest.skip("this help does not quote a path")
    assert DEFAULT_AS_WRITTEN in site.text, (
        f"{site.module}:{site.line} tells the reader the host list is somewhere other "
        f"than {DEFAULT_AS_WRITTEN}, which is where DEFAULT_CONFIG_PATH actually points"
    )
