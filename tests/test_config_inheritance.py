"""`--config` is declared once, at the root, and inherited from there.

It used to be declared seventeen times: on all fifteen commands in `host.py`, on
`remove`'s `delete`, and on the `host` callback. That was seventeen copies of one
option, each free to drift, and an identical row on seventeen `--help` screens —
and it had already drifted, fifteen of the seventeen carrying no `help=` at all
until `d224f15` wrote the sentence out fifteen more times. `test_option_help.py`
is the guard that came out of that, and it holds every declaration to having help
at all; this file holds the ones that exist to two declarations and no more.

The shape now:

  - `cli.py` — the root option. Visible, and the only place the sentence lives,
    including the line saying subcommands inherit it. `comfy-cli` does the same
    for `--workspace`, which is where the wording came from.
  - `host.py` — `ConfigOption`, the alias every command annotates with, and
    `remember_config`, the callback that carries one value across the whole
    invocation. Hidden, because the root already documents it.

The second one exists because the option is still *accepted* after a command
name. `comfy-qat list --config X` is what this suite types everywhere, what run
sheets say, and what `go --new-window` re-execs itself with — so removing it
would have broken the tool inside a spawned Terminal window, on the command that
starts a GPU box, where nobody would see the error. Accepted but not advertised
is the deprecation window `cli.py` already runs for the `host` and `auth`
spellings and `host.py` for `--os`/`--gpu` on the eleven selectors.

What this file pins, and why each one:

  - two declarations, named by module, so seventeen cannot come back one command
    at a time — which is exactly how there came to be seventeen;
  - the root's help says subcommands inherit it, because `comfy-qat go --help`
    no longer mentions `--config` and that sentence is the only way a reader
    finds out it takes one;
  - every command that took a host list before still takes one, compared between
    the source and the live Click tree with no list typed in between;
  - the read/update distinction. Readers used to say "Host list to read." and the
    four that write said "Host list to read and update."; one root option cannot
    say both, so the commands that write say so in their own docstring, which is
    what `--help` prints above the options. The list of writers is derived — a
    command that calls `write_text` or `hostfile.apply` writes the host list — so
    a sixteenth command that starts writing is in scope the day it does;
  - and that all of it actually works, in both positions, including through the
    re-exec.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import click
import pytest
import typer.main
from typer.testing import CliRunner

import comfy_qa.cli
from comfy_qa.config import DEFAULT_CONFIG_PATH
from fakes import hosts_toml

PACKAGE = Path(__file__).resolve().parent.parent / "comfy_qa"

# What help text is allowed to claim the default is. Spelled from the real
# constant, so moving the host list moves this and the text that disagrees fails.
DEFAULT_AS_WRITTEN = "~/" + DEFAULT_CONFIG_PATH.relative_to(Path.home()).as_posix()

# The two sites that are meant to exist, by module. Named rather than counted, so
# "there are two" cannot be satisfied by two of the wrong ones.
DECLARED_IN = {"cli.py", "host.py"}


def _is_typer_option(node: ast.Call) -> bool:
    """`typer.Option(...)` however it was imported.

    Both spellings are live in this package, and a check that only knew the
    dotted one would go quiet the day somebody writes `from typer import Option`.
    """
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "Option"
    return isinstance(func, ast.Name) and func.id == "Option"


def config_options(tree: ast.AST, module: str) -> list[tuple[str, int]]:
    """Every `--config` declaration in one parsed module, as `(module, line)`."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _is_typer_option(node):
            continue
        flags = [arg.value for arg in node.args
                 if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
        if "--config" in flags:
            found.append((module, node.lineno))
    return found


def _entry_points(tree: ast.AST, module: str):
    """Every function Typer turns into something a person can type.

    Both decorators, because both reach the command line: `@app.command("x")`
    names a command, and `@app.callback()` is what runs for the group itself —
    the `host` callback takes a host list too.

    Returns `(by name, by identity)`. Names are what a person types and are not
    unique — `list` is both `comfy-qat list` and `comfy-qat quota list` — so
    anything compared against the live Click tree goes through the identity,
    `(module, function name)`, which is what `command.callback` reports back.
    """
    by_name, by_identity = {}, {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            func = decorator.func if isinstance(decorator, ast.Call) else decorator
            if not (isinstance(func, ast.Attribute)
                    and func.attr in {"command", "callback"}):
                continue
            by_identity[(module, node.name)] = node
            named = [arg.value for arg in getattr(decorator, "args", [])
                     if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
            if named and func.attr == "command":
                by_name[named[0]] = node
    return by_name, by_identity


def _collect():
    sites, by_name, by_identity = [], {}, {}
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        sites.extend(config_options(tree, path.name))
        names, identities = _entry_points(tree, path.name)
        by_name.update(names)
        by_identity.update(identities)
    return sites, by_name, by_identity


SITES, COMMANDS, ENTRY_POINTS = _collect()

TAKES_CONFIG = {
    identity for identity, node in ENTRY_POINTS.items()
    if any(arg.arg == "config" for arg in node.args.args + node.args.kwonlyargs)
}

# A command "writes the host list" if its body puts bytes on disk: `write_text`
# for a starter file, `hostfile.apply` for an edit to an existing one. Read out of
# the source, so the sixteenth command to start writing is caught the day it does
# rather than by whoever remembers to extend a list.
WRITES_THE_LIST = sorted(
    name for name, node in COMMANDS.items()
    if any(isinstance(call, ast.Call)
           and ((isinstance(call.func, ast.Attribute) and call.func.attr == "write_text")
                or (isinstance(call.func, ast.Name) and call.func.id == "apply"))
           for call in ast.walk(node))
)


def _tree() -> dict[str, click.Command]:
    """Every command path in the live Click tree, by the words you would type.

    `hasattr(cmd, "commands")` rather than `isinstance(cmd, click.Group)`: under
    Click 8.4 `TyperGroup` no longer subclasses `click.Group`, so the isinstance
    form finds exactly one node and looks like it worked.
    """
    found = {}

    def walk(command, path):
        found[" ".join(path)] = command
        for name, sub in getattr(command, "commands", {}).items():
            walk(sub, path + [name])

    walk(typer.main.get_command(comfy_qa.cli.app), [])
    return found


TREE = _tree()


def _config_param(command: click.Command) -> click.Parameter | None:
    for param in command.params:
        if "--config" in getattr(param, "opts", []):
            return param
    return None


def _identity(command: click.Command) -> tuple[str, str] | None:
    """`(module.py, function name)` for a live Click command.

    Typer keeps the real function as the callback, so this is the same key the
    AST hands out and the two can be compared without a name typed in between.
    """
    callback = getattr(command, "callback", None)
    if callback is None:
        return None
    return (callback.__module__.rsplit(".", 1)[-1] + ".py", callback.__name__)


# --- the shape ---------------------------------------------------------------


def test_the_collector_still_recognises_a_declaration():
    """The guard on the guard.

    Every count below is derived, so a collector that quietly matched nothing
    would make them all pass. The source that used to be wrong must still read as
    a `--config` declaration.
    """
    tree = ast.parse(
        'config: Annotated[Optional[Path], typer.Option("--config")] = None\n'
        'other: Annotated[bool, typer.Option("--live", help="x")] = False\n'
    )
    assert config_options(tree, "sample.py") == [("sample.py", 1)]


def test_it_is_declared_once_at_the_root_and_once_for_inheriting():
    """Two sites, in the two modules meant to have one.

    This is what stops the hoist being undone a command at a time, and it is the
    anti-vacuity check too: a collector matching nothing reports no modules and
    fails here rather than passing quietly.
    """
    assert {module for module, _ in SITES} == DECLARED_IN, (
        f"`--config` is declared in {sorted({m for m, _ in SITES})}. It belongs in "
        f"exactly {sorted(DECLARED_IN)}: the root option in cli.py, and "
        f"host.ConfigOption for every command that inherits it."
    )
    assert len(SITES) == 2, (
        f"{len(SITES)} `--config` declarations, and there should be 2 — the root "
        f"in cli.py and the shared alias in host.py. A command that needs a host "
        f"list annotates `config: ConfigOption`; it does not declare its own.\n  "
        + "\n  ".join(f"{module}:{line}" for module, line in SITES)
    )


def test_the_root_says_subcommands_inherit_it():
    """The one line that makes the other sixteen unnecessary.

    Without it, a person reading `comfy-qat go --help` — which no longer mentions
    `--config` at all — has been given no way to find out that it takes one.
    """
    text = _config_param(TREE[""]).help
    assert "inherit" in text.lower(), (
        f"the root --config help has to say subcommands inherit it, or hiding it "
        f"on them leaves the option undiscoverable. It says: {text!r}"
    )
    assert DEFAULT_AS_WRITTEN in text, (
        f"the root --config help is the only place the default is now written "
        f"down, so it has to name {DEFAULT_AS_WRITTEN}. It says: {text!r}"
    )


def test_every_command_that_took_a_host_list_still_takes_one():
    """The hoist must not have quietly dropped a command.

    Derived twice and compared: the source says which entry points have a
    `config` parameter, and the live Click tree says which command paths parse
    `--config`. Neither list is typed here.
    """
    assert len(TAKES_CONFIG) >= 18, (
        f"only {len(TAKES_CONFIG)} entry points found taking a host list. There "
        f"should be at least eighteen: the seventeen that took one before the "
        f"hoist, and the root callback it moved to. Fewer means either a command "
        f"lost its host list or this collector stopped finding them, and the "
        f"second is the more likely."
    )
    missing = sorted(
        path or "<root>" for path, command in TREE.items()
        if _identity(command) in TAKES_CONFIG and _config_param(command) is None
    )
    assert missing == [], f"these command paths no longer accept --config: {missing}"
    stranded = sorted(
        path or "<root>" for path, command in TREE.items()
        if _config_param(command) is not None and _identity(command) not in TAKES_CONFIG
    )
    assert stranded == [], (
        f"these command paths parse --config with no `config` parameter behind "
        f"it, so the value goes nowhere: {stranded}"
    )


def test_only_the_root_advertises_it():
    """Hidden everywhere else, which is the surface this change exists to remove.

    Sixteen identical `--config PATH` rows across sixteen `--help` screens was
    the duplication. Leaving them visible would keep all of it and add a
    seventeenth at the root.
    """
    assert _config_param(TREE[""]).hidden is False, (
        "the root --config is hidden, so the option is now documented nowhere"
    )
    showing = sorted(path for path, command in TREE.items()
                     if path and (param := _config_param(command)) and not param.hidden)
    assert showing == [], (
        f"these commands advertise their own --config: {showing}. The root "
        f"documents it; a command annotates `config: ConfigOption`, which is hidden."
    )


# --- the read/update distinction ---------------------------------------------


def test_the_commands_that_write_are_actually_being_found():
    """A parametrized test over an empty list is not a passing test, it is no test
    at all — and it would take the read/update distinction with it."""
    assert set(WRITES_THE_LIST) >= {"init", "discover", "create", "move", "delete"}, (
        f"the writers were derived as {WRITES_THE_LIST}. Those five write the host "
        f"list — `init` a new one, the other four an edit to an existing one — so "
        f"a collector that misses any of them has stopped guarding."
    )


@pytest.mark.parametrize("name", WRITES_THE_LIST)
def test_the_commands_that_write_the_host_list_say_so(name):
    """Two sentences used to carry this: readers said "Host list to read." and the
    four that write said "Host list to read and update."

    One root option cannot say both, and the fact that a command rewrites a file
    somebody maintains by hand is exactly what a person needs to be told before
    they run it. So it moved into the docstrings, which is what `--help` prints
    above the options — where it is read by more people than the option row was.
    """
    doc = ast.get_docstring(COMMANDS[name]) or ""
    assert "--config" in doc and re.search(r"writ|rewrit", doc), (
        f"`comfy-qat {name}` writes the host list — it calls write_text or "
        f"hostfile.apply — and its docstring never says so. `--config` no longer "
        f"has per-command help to carry that, so the docstring has to: say that "
        f"the file `--config` names is written, not only read."
    )


@pytest.mark.parametrize("name", sorted(COMMANDS))
def test_a_docstring_that_names_the_host_list_names_the_real_one(name):
    """The same bar `test_option_help.py` holds help text to, applied where the
    sentences moved to. A path in a docstring is a promise about where the tool
    looks, and a promise nothing checks is how it comes to name a directory the
    tool stopped using two refactors ago.
    """
    doc = ast.get_docstring(COMMANDS[name]) or ""
    if DEFAULT_CONFIG_PATH.name not in doc:
        pytest.skip("this docstring does not quote a path")
    assert DEFAULT_AS_WRITTEN in doc, (
        f"`comfy-qat {name}` tells the reader the host list is somewhere other "
        f"than {DEFAULT_AS_WRITTEN}, which is where DEFAULT_CONFIG_PATH points"
    )


# --- and that it works, in every position it gets typed in -------------------


def _hosts(tmp_path) -> Path:
    return hosts_toml(tmp_path / "hosts.toml", remote_port=8189)


def test_it_is_taken_before_the_command_and_after_it(tmp_path):
    """Both spellings, because both are in use.

    The root position is what the hoist adds. The trailing position is what this
    suite, every run sheet and `go --new-window`'s own re-exec already type, so it
    stays — hidden, not removed.
    """
    hosts = _hosts(tmp_path)
    runner = CliRunner()
    before = runner.invoke(comfy_qa.cli.app, ["--config", str(hosts), "list"])
    after = runner.invoke(comfy_qa.cli.app, ["list", "--config", str(hosts)])
    assert before.exit_code == 0, before.output
    assert after.exit_code == 0, after.output
    assert before.output == after.output
    assert "comfy-win" in before.output


def test_the_nearer_spelling_wins(tmp_path):
    """`--config A list --config B` reads B. Where two say different things, the
    more specific one is the answer — the same rule a shell alias follows."""
    hosts = _hosts(tmp_path)
    result = CliRunner().invoke(
        comfy_qa.cli.app,
        ["--config", str(tmp_path / "nothing.toml"), "list", "--config", str(hosts)],
    )
    assert result.exit_code == 0, result.output
    assert "comfy-win" in result.output


def test_a_root_config_reaches_the_bare_listing(tmp_path):
    """`comfy-qat --config X` with no command lists X.

    The root callback lists when there is no subcommand, and it now has a host
    list of its own to pass down rather than the `None` it used to hard-code.
    """
    hosts = _hosts(tmp_path)
    result = CliRunner().invoke(comfy_qa.cli.app, ["--config", str(hosts)])
    assert result.exit_code == 0, result.output
    assert "comfy-win" in result.output


@pytest.mark.parametrize("form", [["go"], ["host", "go"]])
def test_a_root_config_reaches_the_window_go_re_execs_into(tmp_path, monkeypatch, form):
    """`go --new-window` re-execs the tool in a Terminal window, and has to carry
    the host list into it.

    This is where a root-only option would have failed silently. The argv for the
    new window is built from `go`'s own `config` variable, so a root value that
    stopped at the root would leave the spawned window reading a different host
    list — in a window nobody is watching, on the command that starts a GPU box.
    `remember_config` fills the parameter instead, so the argv `go` builds is
    right without `go` knowing where the option was declared.

    The rest of that argv belongs to `--new-window` and is pinned in
    `test_detached_e2e.py`; this asserts only the part that is `--config`'s.
    """
    hosts = _hosts(tmp_path)
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    seen = {}

    class Ran:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(subprocess, "run",
                        lambda args, **kwargs: (seen.update(args=args), Ran())[1])

    result = CliRunner().invoke(
        comfy_qa.cli.app,
        ["--config", str(hosts), *form, "comfy-win", "--new-window"],
    )
    assert result.exit_code == 0, result.output
    script = seen["args"][-1]
    assert "comfy-win" in script
    assert f"--config {hosts}" in script, (
        f"the new window was handed {script!r}, with no host list in it"
    )
