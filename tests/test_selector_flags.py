"""`--os` and `--gpu` are declared on two commands, and on no others.

Eleven commands — up, open, disconnect, down, go, ssh, rdp, logs, switch, move,
stamp — carried `--os` and `--gpu` that bought nothing. Every one of them already
takes the same value as a positional argument, both routes met in
`host._selector`, and `_selector` refused to be given both. Twenty-two options
that were a second spelling.

They were hidden for a release and warned about themselves on stderr every time
one was used, which is what made the window a window rather than a quiet
demotion. This is the other end of it: the twenty-two declarations are gone, and
`comfy-qat up --os windows` is now refused by the parser with "No such option"
rather than accepted in silence. The capability is untouched — `comfy-qat up
windows` and `comfy-qat up windows/l4` go through the same `config.resolve` the
flags did.

Two commands keep theirs, for two DIFFERENT reasons, and that is the real
argument for having taken the other eleven away: one flag name meant three
unrelated things, and nothing in `--help` said which one you were reading.

  - `create --os windows --gpu l4` — required, and describes a box to BUILD.
    Nothing exists yet to select.
  - the eleven above — optional, and SELECTED an existing machine. Gone.
  - `quota request --gpu l4,a100` — a comma-separated LIST of cards to ask
    Google for. No selector accepts that value; neither does `create`.

WHY THIS IS DERIVED AND NOT TYPED OUT. A hand-written list of the two would be
right today and silent the moment somebody adds a command with the same pair of
options copied off its neighbour — which is exactly how there came to be eleven,
and the removal is worth nothing if the next one walks back in. So the
declarations are read out of the AST of every module in `comfy_qa/`, the CLI
paths they are reachable under are read off the real Typer app (a command
hoisted to the root AND left under the hidden `host` spelling is checked at
both), and the verdict comes from the help text the command actually renders. A
new `--os` is in scope the moment it is written, and there is nothing for anyone
to remember.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from typer.testing import CliRunner

from comfy_qa import host
from comfy_qa.cli import app

PACKAGE = Path(__file__).resolve().parent.parent / "comfy_qa"

FLAGS = ("--os", "--gpu")

# The two exemptions, as the names of the functions behind them rather than as
# command paths: a rename of the path is a rename of the command, and should not
# quietly re-open the door for the eleven.
KEEP = {"create_cmd", "quota_request_cmd"}


def _is_typer_option(node: ast.Call) -> bool:
    """`typer.Option(...)` however it was imported — see test_option_help.py."""
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr == "Option"
    return isinstance(func, ast.Name) and func.id == "Option"


def _is_command(node: ast.FunctionDef) -> bool:
    """Decorated with `<something>.command(...)`, whatever the app is called.

    `host.py` uses `@app.command`, `auth.py` also uses `@quota_app.command`, and
    the next feature will use its own. Matching on the attribute name rather than
    on the object means a new sub-app is covered without being registered here.
    """
    for decorator in node.decorator_list:
        if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute):
            if decorator.func.attr == "command":
                return True
    return False


def selector_flags(tree: ast.AST, module: str) -> list[tuple[str, str, str, int, bool]]:
    """Every `--os`/`--gpu` declared on a command in one parsed module.

    Returns `(module, function, flag, line, hidden)`. `hidden` is True only for a
    literal `hidden=True`: anything it cannot read — a name, an expression — is
    reported as exposed, because a guard that cannot see something must not pass
    it.

    `hidden` is kept now that nothing in the package is hidden, and that is
    deliberate rather than left over. Hiding is how the eleven were retired, so
    it is the shape a reintroduction would most plausibly take — a twelfth
    command with the pair copied off a neighbour and `hidden=True` copied with
    them, reading as "already deprecated" and never advertised to anyone. This
    file has to be able to see that and say no to it.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not _is_command(node):
            continue
        # The whole signature at once: annotations, defaults and every kind of
        # parameter. A declaration moved from `Annotated[...]` into a default is
        # still a declaration.
        for call in ast.walk(node.args):
            if not isinstance(call, ast.Call) or not _is_typer_option(call):
                continue
            flags = [arg.value for arg in call.args
                     if isinstance(arg, ast.Constant) and isinstance(arg.value, str)]
            for flag in FLAGS:
                if flag not in flags:
                    continue
                hidden = any(
                    keyword.arg == "hidden"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in call.keywords
                )
                found.append((module, node.name, flag, call.lineno, hidden))
    return found


def _sites() -> list[tuple[str, str, str, int, bool]]:
    found = []
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.extend(selector_flags(tree, path.name))
    return found


SITES = _sites()


def _paths(typer_app, prefix: tuple[str, ...] = ()) -> dict[str, list[tuple[str, ...]]]:
    """Function name -> every command path it can be reached by.

    A verb lives at the root AND under the hidden `host`/`auth` spelling, and
    both render help, so both are checked. Read off the real app rather than
    written down, for the same reason the declarations are.
    """
    reachable: dict[str, list[tuple[str, ...]]] = {}
    for command in typer_app.registered_commands:
        name = command.name or command.callback.__name__.replace("_", "-")
        reachable.setdefault(command.callback.__name__, []).append(prefix + (name,))
    for group in typer_app.registered_groups:
        for function, paths in _paths(group.typer_instance, prefix + (group.name,)).items():
            reachable.setdefault(function, []).extend(paths)
    return reachable


PATHS = _paths(app)


def test_the_declarations_are_actually_being_found():
    """A collector that matches nothing would make every test below pass.

    That is the failure mode of a derived list, and it is worse than the typed
    one it replaces: a typed list at least goes stale loudly. It matters more
    now than it did while there were twenty-five sites to find, because three is
    close enough to zero that a broken walk and a correct one look alike.
    """
    assert len(SITES) >= 3, f"only {len(SITES)} --os/--gpu declarations found"
    assert {"host.py", "auth.py"} <= {module for module, *_ in SITES}
    assert KEEP <= {function for _, function, *_ in SITES}, (
        "the two commands that legitimately take these flags were not found, so "
        "this file is no longer reading the source it thinks it is"
    )


def test_a_freshly_exposed_flag_reads_as_exposed():
    """The guard on the guard: the source that was wrong must still read wrong.

    Without this, removing the flags and breaking the collector look identical —
    every case skips, nothing fails, and the next `--os` gets in unnoticed.
    """
    source = (
        "@app.command('demo')\n"
        "def demo_cmd(\n"
        "    os_: Annotated[Optional[str], typer.Option('--os', help='pick')] = None,\n"
        "    gpu: Annotated[Optional[str], typer.Option('--gpu', hidden=True)] = None,\n"
        ") -> None:\n"
        "    ...\n"
    )
    assert selector_flags(ast.parse(source), "sample.py") == [
        ("sample.py", "demo_cmd", "--os", 3, False),
        ("sample.py", "demo_cmd", "--gpu", 4, True),
    ]


@pytest.mark.parametrize("module,function,flag,line,hidden", SITES,
                         ids=[f"{module}:{line}:{flag}" for module, _, flag, line, _ in SITES])
def test_only_create_and_quota_request_declare_these_at_all(module, function, flag, line, hidden):
    """The removal, held at the source. Hidden would satisfy the help check
    below while leaving the spelling alive, which is what it did for a release."""
    assert function in KEEP, (
        f"{module}:{line} declares {flag} on {function}. Only `create` and `quota "
        f"request` may: everywhere else the positional argument already says it — "
        f"`comfy-qat {function.removesuffix('_cmd')} windows/l4` — and the second "
        f"spelling of one idea was retired, not hidden. Delete the option and pass "
        f"the value as the argument."
    )


@pytest.mark.parametrize("module,function,flag,line,hidden", SITES,
                         ids=[f"{module}:{line}:{flag}" for module, _, flag, line, _ in SITES])
def test_only_create_and_quota_request_offer_these_in_help(module, function, flag, line, hidden):
    """The verdict comes from the help a person actually reads."""
    paths = PATHS.get(function)
    assert paths, f"{module}:{line} declares {flag} on {function}, which no command path reaches"

    for path in paths:
        rendered = CliRunner().invoke(app, [*path, "--help"]).output
        offered = flag in rendered
        if function in KEEP:
            assert offered, (
                f"`comfy-qat {' '.join(path)} --help` no longer offers {flag}. It is one "
                f"of the two commands that needs it: {flag} there is not a way of picking "
                f"a machine that exists."
            )
        else:
            assert not offered, (
                f"`comfy-qat {' '.join(path)} --help` offers {flag} ({module}:{line})."
            )


@pytest.mark.parametrize("module,function,flag,line,hidden", SITES,
                         ids=[f"{module}:{line}:{flag}" for module, _, flag, line, _ in SITES])
def test_what_is_left_is_advertised_rather_than_hidden(module, function, flag, line, hidden):
    """Help is the promise; nothing that survives may be hidden.

    Both survivors are REQUIRED or load-bearing arguments to their command. A
    hidden one would be a required flag nobody can read about, which is the one
    shape worse than the second spelling this file retired.
    """
    assert not hidden, (
        f"{module}:{line}: {flag} on {function} is hidden. The two that stay are "
        f"how you say what to build and what to ask Google for; hiding either "
        f"leaves a command that cannot be run from its own help."
    )


def test_the_positional_still_selects_a_machine():
    """The capability the flags were a second spelling of, under the one name."""
    assert host._selector("windows/l4") == "windows/l4"
    assert host._selector("windows") == "windows"
    assert host._selector("comfy-win") == "comfy-win"


@pytest.mark.parametrize("flag,value", [("--os", "windows"), ("--gpu", "l4")])
def test_the_retired_flag_is_now_refused_by_the_parser(flag, value):
    """Removed, not ignored. The failure this guards is the quiet one.

    Typer drops an unknown option on the floor only if someone reconfigures it
    to; by default click refuses. Pin the refusal anyway, because "silently
    ignored" is how `up --os windows` would come to run against whatever the
    positional defaulted to — and there is no positional default here, so it
    would become "which machine?" about a command that named one.
    """
    result = CliRunner().invoke(
        app, ["up", flag, value, "--config", "/nowhere/hosts.toml"])

    assert "No such option" in result.output, (
        f"`up {flag} {value}` did not report an unknown option: {result.output!r}"
    )
    assert result.exit_code == 2


def test_saying_nothing_at_all_is_still_a_refusal():
    """The one refusal `_selector` still carries, and the words it uses.

    `os/card` stays in it: the SHAPE survived the flags — `comfy-qat go
    windows/l4` is still how you describe a machine — and this is the only place
    anybody is told so.
    """
    with pytest.raises(Exception) as caught:
        host._selector(None)
    assert "which machine?" in str(caught.value)
    assert "os/card" in str(caught.value)
    assert "--os" not in str(caught.value), (
        "the refusal names a flag that no longer exists"
    )
