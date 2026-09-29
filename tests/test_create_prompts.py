"""`comfy-qat create` asks for the two things it cannot guess.

The defect: `--os` and `--gpu` were required options with a menu behind neither,
so bare `comfy-qat create` was Typer's own parse error —

    Usage: comfy-qat create [OPTIONS]
    Try 'comfy-qat create --help' for help.
    ╭─ Error ──────────────────────────────────────────────╮
    │ Missing option '--os'.                               │
    ╰──────────────────────────────────────────────────────╯

exit 2 — from the command whose help calls the card "the only real decision",
in a tool that already computes the list of cards it can drive
(`create.drivable_cards()`) and holds the two images in a table.

The rule and its two corollaries are written out in `comfy_qa/ask.py`. What this
file holds is the pair of them being the SAME transcript: a flag that was
supplied is confirmed where the prompt would have been, so a scripted run and an
interactive run read alike. That matters here more than in most tools, because
this output is pasted into bug reports, and a transcript silent about the two
decisions that determined everything in it is a transcript nobody can repeat.

Nothing here is interactive by accident. `say.watching` — which is
`gcloud.can_prompt`, stdin and stderr both terminals — is what decides, and
every test says which of the two it is testing.
"""

from __future__ import annotations

from itertools import takewhile

import pytest
from typer.testing import CliRunner

from comfy_qa import gcloud as gcloud_module
from comfy_qa.cli import app
from comfy_qa.create import drivable_cards

from test_create_cli import HOSTS, FakeGcloud, never_time_a_real_connection  # noqa: F401

runner = CliRunner()


@pytest.fixture
def create(tmp_path, monkeypatch):
    """`comfy-qat create`, with a fake project and a say on whether anyone is
    watching. `watching` defaults to False — that is what a test runner is."""

    def invoke(*args, answers=None, watching=False, gc=None):
        path = tmp_path / "hosts.toml"
        path.write_text(HOSTS, encoding="utf-8")
        cloud = gc or FakeGcloud()
        monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: cloud)
        # The one TTY test in the package, replaced at its one source. Patching
        # `say.watching` instead would leave `gcloud`'s own prompting rule
        # disagreeing with the tool's, which is the thing `say.watching`'s
        # docstring exists to prevent.
        monkeypatch.setattr(gcloud_module, "can_prompt", lambda: watching)
        result = runner.invoke(
            app, ["create", *args, "--config", str(path)], input=answers)
        result.gc = cloud                                    # type: ignore[attr-defined]
        return result

    return invoke


# --- nobody to ask ---------------------------------------------------------


def test_a_missing_os_is_refused_by_name_when_there_is_nobody_to_ask(create):
    """A script gets a refusal that names the flag and its answers, and the exit
    code it got from Typer's parse error. Never a prompt into a closed pipe,
    which is a hang."""
    result = create()

    assert result.exit_code == 2
    assert "--os is required and there is no terminal to ask at: linux or windows" \
        in result.output
    assert "to fix: comfy-qat create --os linux --gpu l4" in result.output


def test_a_missing_gpu_is_refused_by_name_when_there_is_nobody_to_ask(create):
    result = create("--os", "linux")

    assert result.exit_code == 2
    assert "--gpu is required and there is no terminal to ask at" in result.output
    for card in drivable_cards():
        assert card in result.output


def test_a_refusal_for_a_missing_flag_reads_nothing_from_google(create):
    """It cost a Typer parse error before, which is free. A refusal that first
    spends a minute on quota is a refusal nobody waits for."""
    result = create()

    assert result.gc.calls == []


def test_an_invalid_value_is_refused_exactly_as_before_when_there_is_nobody_to_ask(create):
    """Unchanged, deliberately. The fallback-to-a-prompt rule is for a person at
    a terminal; a script needs the exit code and the sentence, and `plan` is
    still the thing that says it."""
    result = create("--os", "linux", "--gpu", "p100")

    assert result.exit_code == 2
    assert "this tool cannot bring up a P100" in result.output
    assert "Which card?" not in result.output


def test_a_supplied_flag_is_confirmed_even_with_nobody_watching(create):
    """The parity is about the transcript, not about the terminal. A CI log that
    does not say which card was asked for is the log somebody reads three weeks
    later."""
    result = create("--os", "linux", "--gpu", "l4", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "OS: linux (from --os)" in result.output
    assert "GPU: l4 (from --gpu)" in result.output


# --- somebody to ask -------------------------------------------------------


def test_bare_create_asks_for_both_and_goes_on_with_the_answers(create):
    """The whole defect, end to end: the command that used to exit 2 on a parse
    error now builds a plan."""
    result = create("--dry-run", watching=True, answers="1\n4\n")

    assert result.exit_code == 0, result.output
    assert "Which operating system?" in result.output
    assert "Which card?" in result.output
    assert "OS: linux" in result.output
    assert "GPU: l4" in result.output
    assert "--dry-run: nothing created" in result.output


def test_the_menu_offers_exactly_what_the_tool_can_drive(create):
    """Not a hand-typed list. The cards come from `drivable_cards()`, which is
    the same source as the `--gpu` help and the "no card called" refusal — the
    four cards with no GSP are in the table and must never be offered."""
    result = create("--dry-run", watching=True, answers="1\n4\n")

    # The contiguous numbered block under `Which card?` and nothing else:
    # `create` prints a numbered zone order further down, and a filter that
    # collects every numbered line would be asserting about that too.
    lines = result.output.splitlines()
    after = lines[lines.index("Which card?") + 1:]
    offered = list(takewhile(lambda line: line.strip()[:1].isdigit(), after))
    cards = [line.split(".", 1)[1].split()[0] for line in offered]
    assert cards == drivable_cards()
    for stranded in ("p100", "v100", "k80", "p4"):
        assert stranded not in cards


def test_a_supplied_flag_is_confirmed_where_its_prompt_would_have_been(create):
    """One flag given and one left off. The given one is stated in the position
    the question it answers would have occupied, so the two lines read as one
    pair however the command was invoked."""
    result = create("--os", "linux", "--dry-run", watching=True, answers="4\n")

    lines = [line for line in result.output.splitlines()
             if line.strip().startswith(("OS:", "GPU:", "Which"))]
    assert lines[0].strip() == "OS: linux (from --os)"
    assert lines[1].strip() == "Which card?"
    assert lines[2].strip() == "GPU: l4"


def test_an_invalid_value_warns_and_asks_rather_than_throwing_the_run_away(create):
    """`--gpu p100` names a real card this tool will not order. Aborting taught
    the same lesson and cost the whole invocation; the reason is printed and the
    menu of cards that do work is offered under it."""
    result = create("--gpu", "p100", "--dry-run", watching=True, answers="1\n4\n")

    assert result.exit_code == 0, result.output
    assert "--gpu p100 cannot be used — asking you instead" in result.output
    assert "GPU: l4" in result.output


def test_the_reason_an_invalid_value_was_refused_is_printed_in_full(create):
    """The refusal's own sentence, from the same function that would have
    raised it — not a shorter paraphrase written at the prompt. Somebody who
    picks a card from the menu should still learn why theirs was not on it."""
    result = create("--gpu", "p100", "--dry-run", watching=True, answers="1\n4\n")

    prose = " ".join(line.split("┃", 1)[1].strip()
                     for line in result.output.splitlines() if "┃" in line)
    assert "GPU System Processor" in prose
    assert "never see its own GPU" in prose


def test_a_typo_is_refused_with_what_there_is_and_then_asked_for(create):
    """The other half of `card_for`: a card that does not exist at all, which
    gets a different sentence from one that exists and cannot be driven."""
    result = create("--gpu", "rtx4090", "--dry-run", watching=True, answers="1\n4\n")

    assert result.exit_code == 0, result.output
    assert "no card called 'rtx4090'" in result.output
    assert "GPU: l4" in result.output


def test_an_unusable_answer_to_the_prompt_is_asked_again(create):
    """A number outside the list, and a word. Neither may be taken as a choice —
    picking the nearest option is how a box gets made on the wrong card."""
    result = create("--os", "linux", "--dry-run", watching=True, answers="9\nl4\n4\n")

    assert result.exit_code == 0, result.output
    assert result.output.count("pick a number between 1 and") == 2
    assert "GPU: l4" in result.output


# --- where the conversation goes -------------------------------------------


def test_the_questions_and_the_answers_are_on_stderr(tmp_path, monkeypatch):
    """`say`'s stream rule: stdout carries the answer, stderr carries the story.
    A question is the story, and `create`'s stdout is a plan somebody reads
    back — `comfy-qat create --dry-run > plan.txt` must not end up with a
    half-finished prompt in it."""
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: FakeGcloud())
    monkeypatch.setattr(gcloud_module, "can_prompt", lambda: True)

    result = CliRunner().invoke(
        app, ["create", "--dry-run", "--config", str(path)], input="1\n4\n")

    assert result.exit_code == 0, result.output
    assert "Which card?" in result.stderr
    assert "GPU: l4" in result.stderr
    assert "Which card?" not in result.stdout
    assert "what this makes:" in result.stdout


# --- the flags that were already there --------------------------------------


def test_asking_for_two_zones_is_still_refused_before_any_question(create):
    """Both flags are on the command line, one of them will be thrown away
    whatever anybody answers, and a refusal that arrives after two questions is
    a refusal about a run the user has already been talked into."""
    result = create("--zone", "us-central1-a", "--region", "europe-west2",
                    watching=True)

    assert result.exit_code == 2
    assert "cannot both be right" in result.output
    assert "Which operating system?" not in result.output


def test_that_refusal_offers_only_the_flags_that_were_typed(create):
    """The two commands it hands back have to run as printed. `--os` and `--gpu`
    are optional now, so restating them would have put `None` in both — the
    eleventh printed remedy that cannot run."""
    result = create("--zone", "us-central1-a", "--region", "europe-west2",
                    watching=True)

    assert "comfy-qat create --zone us-central1-a" in result.output
    assert "comfy-qat create --region europe-west2" in result.output
    assert "None" not in result.output
