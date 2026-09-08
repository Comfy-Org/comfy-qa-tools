"""`--os` and `--gpu` appear in `--help` on two commands, and on no others.

Eleven commands — up, open, disconnect, down, go, ssh, rdp, logs, switch, move,
stamp — carried `--os` and `--gpu` that bought nothing. Every one of them already
takes the same value as a positional argument, both routes meet in
`host._selector`, and `_selector` refuses to be given both. Twenty-two options
that were a second spelling.

They are not removed. They are hidden — the deprecation window `cli.py` uses for
the `host` and `auth` sub-apps — so every script and every habit keeps working
while `--help` shows one way to say which machine you mean. `_selector` prints
a note to stderr naming the shorter form, because a deprecation nobody is told
about never ends.

Two commands keep theirs, for two DIFFERENT reasons, and that is the real
argument for taking the other eleven away: one flag name meant three unrelated
things, and nothing in `--help` said which one you were reading.

  - `create --os windows --gpu l4` — required, and describes a box to BUILD.
    Nothing exists yet to select.
  - the eleven above — optional, and SELECT an existing machine.
  - `quota request --gpu l4,a100` — a comma-separated LIST of cards to ask
    Google for. No selector accepts that value; neither does `create`.

WHY THIS IS DERIVED AND NOT TYPED OUT. A hand-written list of the eleven would
be right today and silent the moment somebody adds a twelfth command with the
same pair of options copied off its neighbour — which is exactly how there came
to be eleven. So the declarations are read out of the AST of every module in
`comfy_qa/`, the CLI paths they are reachable under are read off the real Typer
app (a command hoisted to the root AND left under the hidden `host` spelling is
checked at both), and the verdict comes from the help text the command actually
renders. A new `--os` is in scope the moment it is written, and there is nothing
for anyone to remember.
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
    one it replaces: a typed list at least goes stale loudly.
    """
    assert len(SITES) >= 23, f"only {len(SITES)} --os/--gpu declarations found"
    assert {"host.py", "auth.py"} <= {module for module, *_ in SITES}
    assert KEEP <= {function for _, function, *_ in SITES}, (
        "the two commands that legitimately take these flags were not found, so "
        "this file is no longer reading the source it thinks it is"
    )


def test_a_freshly_exposed_flag_reads_as_exposed():
    """The guard on the guard: the source that was wrong must still read wrong.

    Without this, hiding the flags and breaking the collector look identical —
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
                f"`comfy-qat {' '.join(path)} --help` offers {flag} ({module}:{line}). "
                f"Only `create` and `quota request` may: everywhere else the positional "
                f"argument already says it — `comfy-qat {path[-1]} windows/l4` — and a "
                f"second spelling of one idea is what this retired. Pass hidden=True on "
                f"the option; it keeps working, it stops advertising itself."
            )


@pytest.mark.parametrize("module,function,flag,line,hidden", SITES,
                         ids=[f"{module}:{line}:{flag}" for module, _, flag, line, _ in SITES])
def test_the_source_agrees_with_the_help(module, function, flag, line, hidden):
    """Help is the promise; `hidden=` is how it is kept. Say so at the site."""
    assert hidden is (function not in KEEP), (
        f"{module}:{line}: {flag} on {function} is "
        f"{'hidden' if hidden else 'exposed'}, and the rule says it should not be"
    )


def test_the_retired_flags_still_select_a_machine():
    """The window is the point: nothing written down before today may break."""
    assert host._selector(None, "windows", "l4") == "windows/l4"
    assert host._selector(None, "windows", None) == "windows"
    assert host._selector("comfy-win", None, None) == "comfy-win"


def test_a_hidden_option_is_still_accepted_by_the_parser():
    """Hidden is not removed. `up --os windows` must reach the host list.

    Pointed at a config that does not exist, so this gets as far as reading the
    host list and no further — the refusal is 2, and the thing it must not be is
    click's "No such option".
    """
    result = CliRunner().invoke(app, ["up", "--os", "windows", "--config", "/nowhere/hosts.toml"])
    assert "No such option" not in result.output
    assert result.exit_code == 2


def test_saying_it_twice_is_still_an_error():
    """`go windows --os linux` is a mistake, not a precedence puzzle."""
    with pytest.raises(Exception) as caught:
        host._selector("windows", "linux", None)
    assert "not both" in str(caught.value)


def test_using_a_retired_flag_says_which_form_replaces_it(capsys):
    """On stderr, and it names the exact string to type instead.

    The flags leave `--help` on the day this ships, so the only person who can
    still be told the shorter form is the person still typing the longer one.
    """
    host._selector(None, "windows", "l4")
    err = capsys.readouterr().err
    assert "--os/--gpu" in err
    assert "windows/l4" in err


def test_the_note_stays_off_stdout(capsys):
    """`--json` and `--dry-run` output is the other reason these flags existed."""
    host._selector(None, "windows", "l4")
    assert capsys.readouterr().out == ""


def test_naming_a_machine_says_nothing(capsys):
    """No note for the form that is already correct."""
    host._selector("comfy-win", None, None)
    assert capsys.readouterr().err == ""
