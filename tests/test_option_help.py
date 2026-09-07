"""`--config` says what it is, on every command that takes it.

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

The bar is deliberately two things, not one:

  - non-empty `help=`, which is the defect above; and
  - any help that names the host list names the *real* default, derived from
    `DEFAULT_CONFIG_PATH`. Help text that states a path is a promise about where
    the tool looks, and a promise nothing checks is how it comes to name a
    directory the tool stopped using two refactors ago.

`init` is held to the first bar and not the second: it writes the starter file
rather than reading one, and its help says so without quoting a path.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from comfy_qa.config import DEFAULT_CONFIG_PATH

PACKAGE = Path(__file__).resolve().parent.parent / "comfy_qa"

# What the help text is allowed to claim the default is. Spelled from the real
# constant, so moving the host list moves this and the help that disagrees fails.
DEFAULT_AS_WRITTEN = "~/" + DEFAULT_CONFIG_PATH.relative_to(Path.home()).as_posix()


def _is_typer_option(node: ast.Call) -> bool:
    """`typer.Option(...)` however it was imported.

    Both spellings are live in this package, and a check that only knew the
    dotted one would go quiet the day somebody writes `from typer import Option`.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "Option"
    return isinstance(func, ast.Name) and func.id == "Option"


def config_options(tree: ast.AST, module: str) -> list[tuple[str, int, str | None]]:
    """Every `--config` declaration in one parsed module.

    Returns `(module, line, help)` per site, with `help` None when the option was
    declared without one — the case this file exists for. A `help=` that is not a
    plain string literal (an f-string, a name) reads as None too: it cannot be
    checked here, and silently passing something unreadable is how a guard stops
    guarding.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_typer_option(node):
            continue
        flags = [arg.value for arg in node.args
                 if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
        if "--config" not in flags:
            continue
        text = None
        for keyword in node.keywords:
            if keyword.arg == "help" and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, str):
                    text = keyword.value.value
        found.append((module, node.lineno, text))
    return found


def _sites() -> list[tuple[str, int, str | None]]:
    found = []
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.extend(config_options(tree, path.name))
    return found


SITES = _sites()


def test_the_declarations_are_actually_being_found():
    """A collector that matches nothing would make every test below pass.

    That is the failure mode of a derived list, and it is worse than the defect
    it replaced: a hand-typed list at least fails loudly when it goes stale.
    """
    assert len(SITES) >= 17, f"only {len(SITES)} `--config` declarations found"
    assert {"host.py", "remove.py"} <= {module for module, _, _ in SITES}


def test_a_bare_declaration_is_recognised_as_bare():
    """The guard on the guard: the exact source that was wrong must still read as
    wrong, or the sweep above passes because it stopped seeing anything."""
    tree = ast.parse(
        'config: Annotated[Optional[Path], typer.Option("--config")] = None\n'
    )
    assert config_options(tree, "sample.py") == [("sample.py", 1, None)]


@pytest.mark.parametrize("module,line,text", SITES,
                         ids=[f"{module}:{line}" for module, line, _ in SITES])
def test_every_config_option_carries_help(module, line, text):
    assert text and text.strip(), (
        f"{module}:{line} declares --config with no help, so it renders as a bare "
        f"`--config PATH` in --help. Say what it is: the host list, and where it "
        f"is read from when the flag is left off ({DEFAULT_AS_WRITTEN})."
    )


@pytest.mark.parametrize("module,line,text", SITES,
                         ids=[f"{module}:{line}" for module, line, _ in SITES])
def test_help_that_names_the_host_list_names_the_real_one(module, line, text):
    """Only the sites that quote a path are held to this — see the module docstring."""
    if not text or DEFAULT_CONFIG_PATH.name not in text:
        pytest.skip("this help does not quote a path")
    assert DEFAULT_AS_WRITTEN in text, (
        f"{module}:{line} tells the reader the host list is somewhere other than "
        f"{DEFAULT_AS_WRITTEN}, which is where DEFAULT_CONFIG_PATH actually points"
    )
