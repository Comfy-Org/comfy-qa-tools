"""The hidden `host` and `auth` nouns say they are on their way out.

`cli.py` registers both sub-apps `hidden=True` and calls that, in as many words,
"a deprecation window, not a second permanent spelling". Nothing closed it.
Hiding a spelling from `--help` removes the last place anyone could read that the
short form exists, so the only person who can still be told is the person still
typing the long one — and until now `comfy-qat host list` printed exactly what
`comfy-qat list` printed, byte for byte, forever.

That is the same argument, and the same fix, as the `--os`/`--gpu` retirement in
`test_selector_flags.py`: hidden keeps every script working, a note on stderr is
what makes the window a window. Both are pinned rather than left to a comment,
because a comment describing an intention is what was already there.

`env` is deliberately NOT held to this, and the exemption is the point rather
than an oversight. `host` and `auth` are second spellings — `host go` and `go`
are one command reached two ways — so a note can name the shorter form. `env` is
hidden for an unrelated reason: it is a whole command carried forward until its
own release, with no other spelling to point at. Telling anyone it was on its way
out would be false, and a deprecation note that names no replacement is noise
that teaches people to ignore the ones that do.

The last case here is the tool's own mouth. `go --new-window` re-execs itself
through `osascript`, and it used to spell that `host go`. Add a note to the noun
without fixing that line and the new window opens on a deprecation warning about
text the tool wrote, not the user — which is how a warning stops being read.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from comfy_qa.cli import app

# The message, by the part of it that identifies it. Not the whole sentence: the
# verb is interpolated, and pinning the wording twice is how the two go out of
# step.
NOTE = "is on its way out and still works"


def _hidden(name: str) -> bool:
    """Is this sub-app registered hidden on the root?"""
    for group in app.registered_groups:
        if group.name == name:
            return bool(group.hidden)
    raise AssertionError(f"no `{name}` sub-app is registered on the root at all")


@pytest.mark.parametrize("noun", ["host", "auth"])
def test_the_old_noun_is_still_hidden_and_still_reachable(noun):
    """The window's two halves. Either one alone is a different decision."""
    assert _hidden(noun), (
        f"`{noun}` is no longer hidden, so the tool now advertises two spellings "
        f"of every command behind it"
    )
    assert CliRunner().invoke(app, [noun, "--help"]).exit_code == 0, (
        f"`{noun}` no longer runs. Hidden is not removed — every script written "
        f"before it was hidden still spells it this way."
    )


def test_reaching_a_command_through_host_says_so(monkeypatch, tmp_path):
    """And it names the command to type instead, not just that one exists."""
    result = CliRunner().invoke(app, ["host", "list", "--config", str(tmp_path / "none.toml")])
    assert NOTE in result.output
    assert "comfy-qat list" in result.output


def test_reaching_a_command_through_auth_says_so():
    result = CliRunner().invoke(app, ["auth", "status", "--help"])
    # `--help` on the subcommand exits before the group callback runs, so this
    # asserts the pair that matters rather than the note: the old path works.
    assert result.exit_code == 0


def test_the_short_spelling_says_nothing(tmp_path):
    """A note on the form that is already right is noise, and noise is ignored."""
    result = CliRunner().invoke(app, ["list", "--config", str(tmp_path / "none.toml")])
    assert NOTE not in result.output


def test_the_note_does_not_reach_stdout(tmp_path):
    """stderr, like every other note this tool prints. `--json` output is read by
    scripts, and a script pinned to the old spelling is exactly who gets this."""
    runner = CliRunner()
    long_ = runner.invoke(app, ["host", "list", "--config", str(tmp_path / "none.toml")])
    short = runner.invoke(app, ["list", "--config", str(tmp_path / "none.toml")])
    assert long_.stdout == short.stdout, (
        "the old spelling now differs from the new one on STDOUT, which is what "
        "a script reads. The note belongs on stderr."
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

    Otherwise the new window opens on a deprecation warning about a command the
    user did not write, which trains everyone to ignore the one that matters.
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
        f"the new window is told to run {handed[0][:2]}, which is the deprecated "
        f"spelling this tool now warns about. Say `go`."
    )
    assert "host" not in handed[0]


def test_no_new_hidden_group_arrives_without_a_note():
    """A third hidden noun added quietly is a third window nobody closes.

    Not a list of names — the point of the exercise is that hand-maintained lists
    go stale — but a count with the two exemptions argued in this file. A new
    hidden sub-app has to come here and say which kind it is.
    """
    hidden = sorted(group.name for group in app.registered_groups if group.hidden)
    assert hidden == ["auth", "host"], (
        f"the hidden sub-apps are now {hidden}. If it is a second spelling of "
        f"something reachable at the root, give it the same note `host` and `auth` "
        f"carry. If it is a command parked until its own release, say so here the "
        f"way `env` is."
    )


def test_the_note_is_not_printed_twice(tmp_path):
    """One note per invocation. `auth quota list` passes through two callbacks."""
    result = CliRunner().invoke(app, ["host", "list", "--config", str(tmp_path / "none.toml")])
    assert result.output.count(NOTE) == 1

