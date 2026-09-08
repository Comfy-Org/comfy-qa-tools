"""The `host` and `auth` nouns are gone, and this is what keeps them gone.

They were a deprecation window and the window closed. `cli.py` registered both
sub-apps `hidden=True` and called that, in as many words, "a deprecation window,
not a second permanent spelling" — then nothing closed it, because a window with
no closing date is a second permanent spelling. What closed it was a version: it
opened at the release that named it, `CHANGELOG.md` promised the next minor, and
that minor is this one. 42 command paths became 22.

The file is kept rather than deleted, and its name still reads `old_spellings`,
because the guards that survive the removal are the ones about the removal: that
nothing hides a second spelling again, that `env`'s exemption is still a decision
somebody made rather than a leftover, and that the tool does not type a retired
spelling at itself. What went with the aliases is everything that asserted they
were still REACHABLE — those tests existed to hold the window open.

`env` is deliberately NOT a second spelling, and the exemption is the point rather
than an oversight. `host` and `auth` were second spellings — `host go` and `go`
were one command reached two ways — so a note could name the shorter form. `env`
is hidden for an unrelated reason: it is a whole command carried forward until its
own release, with no other spelling to point at. Telling anyone it was on its way
out would be false, and a deprecation note that names no replacement is noise that
teaches people to ignore the ones that do. So `env` stays hidden and stays silent,
and is the only hidden thing here.

The last case is the tool's own mouth. `go --new-window` re-execs itself through
`osascript`, and it used to spell that `host go`. That line was fixed while the
window was open, so the new window would not print a deprecation warning about
text the tool wrote rather than the user. It matters more now, not less: the
spelling it used to type no longer parses at all, so the same bug today would
hand the new window a command that exits 2.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from comfy_qa.cli import app

# What the deprecation note used to say. Nothing should say it any more: the
# note existed to tell someone their spelling was going away, and it has gone.
NOTE = "is on its way out and still works"


@pytest.mark.parametrize("noun", ["host", "auth"])
def test_the_old_noun_is_gone_from_the_command_tree(noun):
    """Not hidden — gone. Hidden was the middle of the retirement, not the end."""
    names = ({group.name for group in app.registered_groups}
             | {command.name for command in app.registered_commands})
    assert noun not in names, (
        f"`{noun}` is registered on the root again. It was a second spelling of "
        f"every verb behind it, the window it was given closed at this release, "
        f"and re-adding it re-opens a window nobody closes."
    )


@pytest.mark.parametrize("argv", [["host", "up"], ["auth", "status"],
                                  ["host", "go"], ["auth", "quota", "list"]])
def test_the_old_spelling_is_refused_rather_than_half_working(argv):
    """It exits 2 with click's own "No such command", and prints no traceback.

    The whole user experience of closing this window is this message, so it is
    worth pinning rather than assuming: a removal that left a partly-registered
    group behind would fail somewhere further in, with a stack trace, which is
    how a user concludes the tool is broken rather than that they typed an old
    name.
    """
    result = CliRunner().invoke(app, argv)
    assert result.exit_code == 2, result.output
    assert f"No such command '{argv[0]}'" in result.output, result.output
    assert "Traceback" not in result.output


def test_nothing_still_prints_the_deprecation_note():
    """The note went with the thing it was about.

    A warning naming a replacement for a spelling that no longer parses is worse
    than silence: it describes a window to somebody who is already past it.
    """
    from pathlib import Path

    package = Path(__file__).resolve().parent.parent / "comfy_qa"
    guilty = [path.name for path in sorted(package.glob("*.py"))
              if NOTE in path.read_text(encoding="utf-8")]
    assert not guilty, (
        f"{', '.join(guilty)} still carries the deprecation note for a spelling "
        f"that has been removed."
    )


def test_env_is_hidden_and_deliberately_silent():
    """The exemption, held by a test so it reads as a decision.

    `env` is not a second spelling — there is no `comfy-qat` command it is the
    long form of — so there is nothing to tell anyone to type instead. If `env`
    ever gains a replacement, this is the test that should fail.
    """
    names = {command.name: command for command in app.registered_commands}
    assert "env" in names and names["env"].hidden, "env should stay hidden"

    result = CliRunner().invoke(app, ["env", "--help"])
    assert result.exit_code == 0
    assert NOTE not in result.output


class _Box:
    """The one host `go --new-window` resolves, with nothing else on it.

    A real `Host` is not needed: everything before the hand-over reads the name
    and nothing else, and the hand-over is the whole subject here.
    """

    name = "comfy-win"


def test_the_tool_does_not_type_the_old_spelling_at_itself(monkeypatch, tmp_path):
    """`go --new-window` re-execs `comfy-qat`, and must use the modern form.

    While the window was open this stopped the new window opening on a
    deprecation warning about a command the user did not write. Now that the
    window has closed it stops something worse: `comfy-qat host go` no longer
    parses, so the spawned window would exit 2 and start nothing, in a window
    nobody is watching, on the command that starts a GPU box.
    """
    from comfy_qa import host

    handed: list[list[str]] = []
    monkeypatch.setattr(host, "_lookup", lambda name, config: ([], _Box()))
    monkeypatch.setattr("comfy_qa.lifecycle.in_a_new_window",
                        lambda rest, say: handed.append(list(rest)))

    result = CliRunner().invoke(app, ["go", "comfy-win", "--new-window"])

    assert result.exit_code == 0, result.output
    assert handed, "nothing was handed over to the new window"
    assert handed[0][0] == "go", (
        f"the new window is told to run {handed[0][:2]}, which is the removed "
        f"spelling. Say `go`."
    )
    assert "host" not in handed[0]


def test_no_new_hidden_spelling_arrives_without_a_note():
    """A hidden noun added quietly is a window nobody closes.

    Not a list of names — the point of the exercise is that hand-maintained lists
    go stale — but the whole hidden surface, with the exemptions argued in this
    file. Anything new that hides itself has to come here and say which kind it
    is.

    GROUPS AND COMMANDS BOTH, and the second half was the gap. This read
    `registered_groups` alone while `test_env_is_hidden_and_deliberately_silent`,
    sixty lines up, was already reading `registered_commands` to do its job — so
    a quiet `@app.command("thing", hidden=True)` passed and a quiet
    `add_typer(..., hidden=True)` did not, for no reason anyone chose. `env` is
    now the ONLY hidden thing in the tool, which is what the removal of `host`
    and `auth` bought; the next one is what this is for.
    """
    hidden = sorted(
        [group.name for group in app.registered_groups if group.hidden]
        + [command.name or command.callback.__name__
           for command in app.registered_commands if command.hidden]
    )
    assert hidden == ["env"], (
        f"the hidden spellings are now {hidden}. If it is a second spelling of "
        f"something reachable at the root, it is a deprecation window: give it a "
        f"note on stderr naming the replacement, and a release at which it goes. "
        f"If it is a command parked until its own release with no shorter form to "
        f"name, say so here the way `env` is."
    )
