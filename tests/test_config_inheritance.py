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
  - and, separately, that nothing TOUCHES a host list without taking one. Those
    are not the same check, and the difference cost a real defect. The one above
    compares two derivations that both start from the commands which already
    have a `config` parameter, so a command outside that set is outside both
    directions of it. `setup` was outside it: it had no `--config`, because
    before the hoist nobody expected one there, and it went on writing
    ~/.config/comfy-qa-tools/hosts.toml while the root option told the reader it
    had inherited the flag it was ignoring. Hoisting to the root turns "these
    sixteen take a host list" into a promise about every subcommand, and a
    promise has to be checked against what the commands DO rather than against
    which of them already agreed with it;
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

# Which commands write the host list — derived by following CALLS to the file,
# not by recognising spellings in a command body.
#
# THE WALK THIS REPLACES READ THE COMMAND'S OWN BODY FOR `write_text` OR A BARE
# `apply(...)`, AND IT HAD ALREADY GONE BLIND. Twice, both times in silence:
#
#   - `discover` appended with `path.open("a")` and got into the list on the
#     `write_text` beside it, the starter-file write. Routing the append through
#     `hostfile.add` took that call away, and `discover` LEFT the derived set
#     without any command having stopped writing. Nothing about a list that gets
#     SHORTER says anything is wrong: a collector matching the spellings in use
#     today reports its own blindness as good news.
#
#     THE FLOOR CAUGHT IT — it named `discover`, and the suite went red on this
#     file. Which is the lesson, and it is not "we got away with one": the
#     hand-written half is the half that worked, and the DERIVED half is the one
#     that quietly stopped covering a command. That is the wrong way round for a
#     file whose whole argument is that hand-maintained lists rot.
#
#   - `setup` is the same event with nothing underneath it. It writes the host
#     list TWICE — `ensure_host_list` writes STARTER, `add_discovered_hosts`
#     calls `hostfile.add` — and both are one hop out of `setup_cmd`, which is
#     the only thing the old walk read. So `setup` was in neither the derived set
#     NOR the floor, and no collector and no sibling would have said a word. That
#     is not a hazard this file was at risk of; it is one it was already in.
#
# So: a function writes the host list if it puts bytes on disk inside
# `hostfile.py`, or writes `STARTER`. A command writes it if it REACHES one.
# That is what `_functions` and `_calls` were built for and what the readers
# below already do; the writers were the half that never got it. A helper
# renamed, added or folded in is then transparent, because nothing here is
# matching on the helper's name.
#
# NAMES ARE RESOLVED PER MODULE, through that module's own imports. Resolving a
# bare name against the whole package is not a shortcut here, it is a flood:
# `hostfile._in` and `tunnel._in` share a name, `taken.add(...)` shares one with
# `hostfile.add`, `text.replace(...)` shares one with `os.replace`. Measured,
# bare-name resolution pulled 13 of the 22 commands in through `tunnel` and
# `lifecycle`. That is not over-collection in a safe direction; it is the list
# agreeing with whatever it finds, which is the failure this walk exists to stop.
#
# Where the two directions really are unequal, be wrong the safe way: a command
# named here that turns out not to write costs one sentence in a docstring, and
# one that writes and is not named retires a guard with nothing going red.


def _bytes_on_disk(node: ast.AST) -> bool:
    """A real write to a file — not `str.replace`, not `set.add`.

    `os.replace` is qualified deliberately. Matching the bare attribute `replace`
    put `hostfile._in` — `text.replace("\r\n", "\n")` — into the seed, and `_in`
    is also a `tunnel.py` function, which is how one wrong character opened the
    whole tunnel graph.
    """
    for call in ast.walk(node):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        func = call.func
        if func.attr in ("write_text", "write_bytes"):
            return True
        if func.attr == "replace" and isinstance(func.value, ast.Name) and func.value.id == "os":
            return True
        if func.attr == "open" and call.args:
            mode = call.args[0]
            if isinstance(mode, ast.Constant) and isinstance(mode.value, str) \
                    and any(letter in mode.value for letter in "aw"):
                return True
    return False


def _writes_starter(node: ast.AST) -> bool:
    """Writes the starter host list, wherever it lives.

    This is the other way a command puts a host list on disk, and it is not in
    `hostfile.py` — `setup.ensure_host_list` does it, and that is one of the two
    writes that made `setup` invisible.
    """
    return any(isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
               and call.func.attr in ("write_text", "write_bytes") and call.args
               and isinstance(call.args[0], ast.Name) and call.args[0].id == "STARTER"
               for call in ast.walk(node))


def _outgoing(node: ast.AST) -> set[tuple[str | None, str]]:
    """Calls that could be one of ours, as `(module alias, name)`.

    Bare names, and `<module alias>.<name>` for a module of this package —
    `setup_mod.run_setup(...)` is how `cli.setup_cmd` reaches the writes, so a
    walk that reads bare names only cannot see `setup` write anything.

    Everything else is dropped, and that is the point: `taken.add(...)` and
    `text.replace(...)` are attribute calls on ordinary objects, and counting
    them is what turned this walk into a flood.
    """
    out: set[tuple[str | None, str]] = set()
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Name):
            out.add((None, func.id))
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            out.add((func.value.id, func.attr))
    return out


def writers_among(sources: dict[str, str]) -> list[str]:
    """Every command in `sources` that reaches a write of the host list.

    Takes its sources rather than reading the package, so the walk itself can be
    put in front of a shape that does not exist yet — which is the only way to
    show it survives one.
    """
    defs: dict[tuple[str, str], ast.FunctionDef] = {}
    imports: dict[str, dict[str, str]] = {}
    aliases: dict[str, dict[str, str]] = {}
    entry: dict[str, tuple[str, ast.FunctionDef]] = {}
    seed: set[tuple[str, str]] = set()

    for module, text in sorted(sources.items()):
        tree = ast.parse(text, filename=module)
        names, modules = {}, {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module:                      # from .config import load
                    for alias in node.names:
                        names[alias.asname or alias.name] = f"{node.module.split('.')[-1]}.py"
                else:                                # from . import setup as setup_mod
                    for alias in node.names:
                        modules[alias.asname or alias.name] = f"{alias.name}.py"
            if isinstance(node, ast.FunctionDef):
                defs[(module, node.name)] = node
                if (module == "hostfile.py" and _bytes_on_disk(node)) or _writes_starter(node):
                    seed.add((module, node.name))
        imports[module], aliases[module] = names, modules
        for command, node in _entry_points(tree, module)[0].items():
            entry[command] = (module, node)

    def resolve(module, call):
        alias, name = call
        if alias is not None:
            home = aliases[module].get(alias)
            return (home, name) if home and (home, name) in defs else None
        if (module, name) in defs:
            return (module, name)
        home = imports[module].get(name)
        return (home, name) if home and (home, name) in defs else None

    # The closure does not run THROUGH another command. `go` and `switch` can
    # both offer a move, so both reach `move_cmd` and its `apply` — but the
    # command that rewrites the file there is `move`, which is in this set and
    # says so in its own `--help`. Following that edge would make two commands
    # writers on the strength of handing you to one.
    commands = {(module, node.name) for module, node in entry.values()}
    writers = set(seed)
    changed = True
    while changed:
        changed = False
        for site, node in defs.items():
            if site in writers:
                continue
            reached = {resolve(site[0], call) for call in _outgoing(node)}
            if any(hit in writers and hit not in commands for hit in reached):
                writers.add(site)
                changed = True

    return sorted(
        command for command, (module, node) in entry.items()
        if (module, node.name) in writers
        or any(resolve(module, call) in writers for call in _outgoing(node))
    )


def _package_sources() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in sorted(PACKAGE.glob("*.py"))}


WRITES_THE_LIST = writers_among(_package_sources())

# Every command that writes the host list, named. Compared BOTH ways below, so
# one leaving fails whether or not anybody thought to name it — which is exactly
# what `discover` did, and what `setup` had already done before anyone looked.
WRITES_THE_HOST_LIST = {"init", "discover", "create", "move", "delete", "setup"}


# Reading a host list looks like exactly two things in this package: naming
# `DEFAULT_CONFIG_PATH`, or calling `load` / `ensure_host_list`. Bare-name calls
# only — `json.load(resp)` is an attribute call and `env` is full of them, which
# is the one false positive this distinction removes.
HOST_LIST_READERS = {"load", "ensure_host_list"}
HOST_LIST_NAME = "DEFAULT_CONFIG_PATH"


def _functions() -> dict[str, list[ast.FunctionDef]]:
    """Every function in the package, by name.

    By name and not by module, because a command reaches its helpers through
    plain names after a function-local import — `from .config import load` then
    `load(config)` — and resolving those properly would mean an import graph for
    a question two hops of names already answer.
    """
    found: dict[str, list[ast.FunctionDef]] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                found.setdefault(node.name, []).append(node)
    return found


FUNCTIONS = _functions()


def _calls(node: ast.FunctionDef) -> set[str]:
    """Every name this function calls, however it was spelled."""
    names = set()
    for call in ast.walk(node):
        if isinstance(call, ast.Call):
            func = call.func
            names.add(func.id if isinstance(func, ast.Name)
                      else getattr(func, "attr", None))
    return {name for name in names if name}


def reaches_a_host_list(node: ast.FunctionDef, hops: int = 2) -> list[str]:
    """What this command touches that is a host list, following its helpers.

    Two hops, because that is what the shape of this package costs: `ssh` calls
    `_host`, and `_host` calls `load`. One hop finds twelve of the eighteen and
    would have found `setup` — but a guard set to the exact depth of the defect
    that prompted it is a guard for that defect. Deeper is nearly free here and
    the failure mode is benign: an extra hop can only ever ADD a command to the
    set, and every command in the set is one that must take `--config` anyway.
    """
    seen, frontier, found = set(), [node], set()
    for _ in range(hops + 1):
        following = []
        for func in frontier:
            if id(func) in seen:
                continue
            seen.add(id(func))
            if HOST_LIST_NAME in ast.unparse(func):
                found.add(HOST_LIST_NAME)
            found |= {call.func.id for call in ast.walk(func)
                      if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                      and call.func.id in HOST_LIST_READERS}
            for name in _calls(func):
                following.extend(FUNCTIONS.get(name, []))
        frontier = following
    return sorted(found)


TOUCHES_A_HOST_LIST = {
    identity: reaches_a_host_list(node)
    for identity, node in ENTRY_POINTS.items()
    if reaches_a_host_list(node)
}


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


def test_the_sweep_for_commands_that_touch_a_host_list_finds_them():
    """A derived set that quietly went empty would make the test below vacuous,
    and that test is the one holding the root option's promise up."""
    assert len(TOUCHES_A_HOST_LIST) >= 15, (
        f"only {len(TOUCHES_A_HOST_LIST)} entry points look like they touch a "
        f"host list, and there are eighteen. The walk has stopped following "
        f"calls, not the tool stopped reading its own config file."
    )
    assert ("cli.py", "setup_cmd") in TOUCHES_A_HOST_LIST, (
        "`setup` writes the host list — it is the command that creates one — and "
        "if this walk cannot see that, it cannot see the case it exists for"
    )


def test_nothing_touches_a_host_list_without_taking_config():
    """The promise the root option makes, checked against what commands DO.

    `test_every_command_that_took_a_host_list_still_takes_one` compares two
    derivations and both of them start from the commands that already have a
    `config` parameter, so a command outside that set is outside both directions
    of the comparison. This asks the other question, and it is the one a root
    option makes necessary: `comfy-qat --help` now tells every reader that
    subcommands inherit `--config`, so a subcommand that reads or writes a host
    list and does not take one turns that sentence into a lie the tool acts on.

    `setup` was exactly that. It had no `--config` because before the hoist
    nobody expected one there, and it wrote ~/.config/comfy-qa-tools/hosts.toml
    — a file kept by hand with no other copy — while the flag the user passed was
    accepted, described as inherited, and ignored.
    """
    missing = sorted(
        f"{module}:{name} (reaches {', '.join(markers)})"
        for (module, name), markers in TOUCHES_A_HOST_LIST.items()
        if not any(arg.arg == "config"
                   for arg in ENTRY_POINTS[(module, name)].args.args
                   + ENTRY_POINTS[(module, name)].args.kwonlyargs)
    )
    assert missing == [], (
        f"these reach a host list and take no `--config`, so the root option "
        f"promises them a flag they ignore: {missing}. Give each one "
        f"`config: ConfigOption = None` and pass it down to whatever reads or "
        f"writes the file."
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
    """The derived set and the written-down set, compared BOTH ways.

    A parametrized test over an empty list is not a passing test, it is no test
    at all — and it would take the read/update distinction with it. That is why
    a floor was here. `>=` was the wrong comparison for it, in the one direction
    that matters: it fails when a named command leaves, and says nothing when a
    command that writes was never named. `setup` sat in that gap, writing the
    host list twice, for as long as this test has existed.

    Equality closes it. A command joining the derived set now has to be written
    down here, which is a sentence of thought at the moment somebody teaches a
    new command to touch the file — not a discovery six months later.
    """
    assert set(WRITES_THE_LIST) == WRITES_THE_HOST_LIST, (
        f"the writers were derived as {WRITES_THE_LIST}, and the list of commands "
        f"that write the host list says {sorted(WRITES_THE_HOST_LIST)}. "
        f"Derived and not named: {sorted(set(WRITES_THE_LIST) - WRITES_THE_HOST_LIST) or 'none'}. "
        f"Named and not derived: {sorted(WRITES_THE_HOST_LIST - set(WRITES_THE_LIST)) or 'none'}. "
        f"A command that has started writing the host list has to be named here; "
        f"one that has stopped being derived has stopped being guarded, and that "
        f"is what this catches."
    )


# --- and that the walk itself cannot go blind again -------------------------
#
# The two tests above read the real package, so they can only report what is
# true of it today. Neither can show that the WALK survives a shape the package
# does not have yet — and "a shape the package does not have yet" is precisely
# what took `discover` out of the derived set. So these two put it in front of
# both shapes directly, in sources written here.

HOSTFILE = """\
import os
from pathlib import Path

def _in(text, ending):
    return text.replace("\\r\\n", "\\n")

def _write_durably(path, data):
    with open(path, "wb") as handle:
        handle.write(data)

def apply(path, text, *, expect):
    _write_durably(path, text.encode())
    os.replace(path, path)

def add(path, blocks, *, initial):
    apply(path, initial, expect=set())
"""


def test_the_writer_walk_survives_a_helper_it_has_never_seen():
    """The refactor that would have taken `move` and `delete` out next.

    `discover` left the derived set because its write moved behind a name the
    collector did not know. The same move is available to every other writer,
    and the obvious next one is folding read-transform-apply into a helper per
    site — which is exactly what `hostfile.add` already is. A walk that matches
    helper names would lose both commands to it and report a shorter list.

    Nothing here is named `apply`, `add` or `write_text`, and both commands are
    still derived, because what is followed is the call to the file.
    """
    sources = {
        "hostfile.py": HOSTFILE + """
def rewrite(path, **kw):
    apply(path, "", expect=set())

def drop_host(path, name, *, expect):
    apply(path, "", expect=expect)
""",
        "host.py": """\
from .hostfile import rewrite

@app.command("move")
def move_cmd(config=None):
    rewrite(config, renamed="x")
""",
        "remove.py": """\
from .hostfile import drop_host

@app.command("delete")
def delete_cmd(config=None):
    drop_host(config, "x", expect=set())
""",
    }
    assert writers_among(sources) == ["delete", "move"], (
        "a command's write moved behind a helper this walk had never seen, and "
        "the walk stopped reporting it as a writer — which is the exact way "
        "`discover` left the derived set, arriving at the next two commands."
    )


def test_the_writer_walk_is_not_fooled_by_a_name_it_shares():
    """The other way this walk fails, and the one that looks like more coverage.

    Resolving a called name against the whole package, or counting `x.add(...)`
    as a call to `hostfile.add`, does not lose commands — it gains them.
    `hostfile._in` and `tunnel._in` share a name; `set.add` shares one with
    `hostfile.add`; `text.replace` shares one with `os.replace`. Measured against
    the real package, that pulled 13 of 22 commands in through `tunnel` and
    `lifecycle`, and a list that says almost every command writes the host list
    is not a stricter guard, it is one that has stopped discriminating — and the
    floor above, being an equality, is then wrong in a way somebody will fix by
    widening the floor.

    `list` reads the host list and writes nothing. It must not be derived here.
    """
    # `host.py` imports `add` from `hostfile` for real — `discover` uses it — so
    # the name IS in scope here, and only "this is an attribute call on some
    # object" keeps `taken.add(...)` from resolving to it.
    shares_a_method_name = {
        "hostfile.py": HOSTFILE,
        "host.py": """\
from .hostfile import add

@app.command("list")
def list_cmd(config=None):
    taken = set()
    taken.add(config)
    return sorted(taken)
""",
    }
    assert writers_among(shares_a_method_name) == [], (
        "`list` writes nothing. It was derived as a writer because `taken.add(...)`"
        " was read as a call to `hostfile.add`, which is the name `host.py` really"
        " does import."
    )

    # `_in` is defined in BOTH `hostfile.py` and `tunnel.py`, and `hostfile._in`
    # is `text.replace(...)` — a `replace` that is not `os.replace`. Reading it
    # as a write, or resolving `_in` against the package rather than against the
    # importing module, is the pair that pulled 13 of 22 commands in.
    shares_a_function_name = {
        "hostfile.py": HOSTFILE,
        "tunnel.py": """\
def _in(value, ending):
    return value
""",
        "host.py": """\
from .tunnel import _in

@app.command("logs")
def logs_cmd(config=None):
    return _in("a", "b")
""",
    }
    assert writers_among(shares_a_function_name) == [], (
        "`logs` writes nothing. It was derived as a writer through `_in`, a name "
        "`hostfile` and `tunnel` both define — and `hostfile._in` is a `str.replace`"
        " that only looks like `os.replace`."
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

    This is the whole of that finding now. It was found by running the end-to-end
    criteria rather than by reasoning about them — `comfy-qat --config x` was a
    usage error while `host list --config x` worked — and it used to be held in
    two places, here and against the `host` sub-app's own callback in
    `test_config.py`. That callback went with the `host` noun. The bare listing
    people actually reach for is this one, and the default command is the worst
    place to have an option that only looks like it is there.
    """
    hosts = _hosts(tmp_path)
    result = CliRunner().invoke(comfy_qa.cli.app, ["--config", str(hosts)])
    assert result.exit_code == 0, result.output
    assert "comfy-win" in result.output


class _StoppedAtBilling:
    """Just enough Google Cloud to reach the host list and stop just after it.

    `run_setup` writes the host list *before* it checks billing, deliberately: a
    newcomer stopped at billing should still end the run owning the one thing the
    command could always produce. That ordering is what makes a refusal the
    shortest honest path to the write this test is about — the file is written,
    and nothing project-scoped is.

    Written here rather than imported from `test_setup_flow.py`, because a
    `--config` test that borrows another module's fake inherits that module's
    future edits, and this one exists to be stable.
    """

    def available(self):
        return "/usr/bin/gcloud"

    def python_location(self):
        return "/no/such/python"          # so the NumPy step is a no-op

    def active_account(self):
        return "ali@comfy.org"

    def list_projects(self):
        return [{"projectId": "proj-1"}]

    def current_project(self):
        return "proj-1"

    def billing_enabled(self, project):
        return False                      # stop, having already written the list


@pytest.mark.parametrize("form", [
    ["--config", "{path}", "setup"],      # the root spelling, which is the promise
    ["setup", "--config", "{path}"],      # the trailing one, which run sheets type
])
def test_setup_writes_the_host_list_config_named_and_not_the_default(
    tmp_path, monkeypatch, form,
):
    """`--config` on `setup` has to REACH the file, not merely be accepted.

    Every other check in this file reads the source or the Click tree, and none
    of them can see this. Give `setup_cmd` a `config` parameter and forget to
    hand it to `run_setup` and all of them still pass: the parameter is there,
    the live tree parses `--config`, it is hidden, it is not a new declaration.
    What the user gets is the original defect exactly — the flag accepted, the
    root help promising it is inherited, and their own host list written anyway.
    Measured: with the parameter in place and `config_path=` dropped, the two
    derived guards above pass and only this fails. So this one drives the
    command and then looks at the disk.

    `setup` is the reason the distinction is worth a test rather than a comment.
    Twenty of the twenty-one subcommands only READ the file `--config` names;
    this one writes it, and the default it falls back to is a host list people
    keep by hand with no other copy on the machine.

    `DEFAULT_CONFIG_PATH` is redirected to a sentinel under `tmp_path` instead of
    being left alone, and the sentinel is asserted absent. A test of a bug whose
    whole nature is writing the real host list must not be able to write it even
    when the fix under it is wrong, and "the default was not touched" is a
    stronger statement than "the wanted path exists".
    """
    wanted = tmp_path / "mine.toml"
    default = tmp_path / "default" / "hosts.toml"
    monkeypatch.setattr("comfy_qa.setup.DEFAULT_CONFIG_PATH", default)
    monkeypatch.setattr("comfy_qa.cli.Gcloud", lambda *a, **k: _StoppedAtBilling())

    argv = [word.format(path=wanted) for word in form]
    result = CliRunner().invoke(comfy_qa.cli.app, argv)

    assert result.exit_code == 1, result.output
    assert "no billing account" in result.stderr, result.output
    assert wanted.exists(), (
        f"`comfy-qat {' '.join(argv)}` wrote no host list where it was told to. "
        f"Output: {result.output}"
    )
    assert "[hosts.local]" in wanted.read_text(encoding="utf-8")
    assert not default.exists(), (
        f"`--config` was accepted and then dropped: setup wrote "
        f"DEFAULT_CONFIG_PATH rather than {wanted}. On a real machine that is "
        f"the tester's own hand-maintained host list, and nothing said so."
    )


def test_a_root_config_reaches_the_window_go_re_execs_into(tmp_path, monkeypatch):
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

    It ran over two spellings until the `host` noun was removed — `go` and
    `host go` were one command reached two ways, which is why this test was left
    on the old spelling when the rest of the suite migrated off it. There is one
    spelling now, so there is one case.
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
        ["--config", str(hosts), "go", "comfy-win", "--new-window"],
    )
    assert result.exit_code == 0, result.output
    script = seen["args"][-1]
    assert "comfy-win" in script
    assert f"--config {hosts}" in script, (
        f"the new window was handed {script!r}, with no host list in it"
    )
