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
    result = create("--dry-run", watching=True, answers="1\n4\n1\n")

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
    result = create("--dry-run", watching=True, answers="1\n4\n1\n")

    # The contiguous numbered block under `Which card?` and nothing else:
    # `create` prints a numbered zone order further down, and a filter that
    # collects every numbered line would be asserting about that too.
    lines = result.output.splitlines()
    after = lines[lines.index("Which card?") + 1:]
    offered = list(takewhile(lambda line: line.strip()[:1].isdigit(), after))
    cards = [line.split(".", 1)[1].split()[0] for line in offered]
    # The cards it can drive, in `drivable_cards()`'s order, and then the one
    # answer that is not a card: no GPU at all. Last, so the numbers the cards
    # have always had do not move.
    assert cards == [*drivable_cards(), "none"]
    for stranded in ("p100", "v100", "k80", "p4"):
        assert stranded not in cards


def test_a_supplied_flag_is_confirmed_where_its_prompt_would_have_been(create):
    """One flag given and one left off. The given one is stated in the position
    the question it answers would have occupied, so the two lines read as one
    pair however the command was invoked."""
    result = create("--os", "linux", "--dry-run", watching=True, answers="4\n1\n")

    lines = [line for line in result.output.splitlines()
             if line.strip().startswith(("OS:", "GPU:", "Which"))]
    assert lines[0].strip() == "OS: linux (from --os)"
    assert lines[1].strip() == "Which card?"
    assert lines[2].strip() == "GPU: l4"


def test_an_invalid_value_warns_and_asks_rather_than_throwing_the_run_away(create):
    """`--gpu p100` names a real card this tool will not order. Aborting taught
    the same lesson and cost the whole invocation; the reason is printed and the
    menu of cards that do work is offered under it."""
    result = create("--gpu", "p100", "--dry-run", watching=True, answers="1\n4\n1\n")

    assert result.exit_code == 0, result.output
    assert "--gpu p100 cannot be used — asking you instead" in result.output
    assert "GPU: l4" in result.output


def test_the_reason_an_invalid_value_was_refused_is_printed_in_full(create):
    """The refusal's own sentence, from the same function that would have
    raised it — not a shorter paraphrase written at the prompt. Somebody who
    picks a card from the menu should still learn why theirs was not on it."""
    result = create("--gpu", "p100", "--dry-run", watching=True, answers="1\n4\n1\n")

    prose = " ".join(line.split("┃", 1)[1].strip()
                     for line in result.output.splitlines() if "┃" in line)
    assert "GPU System Processor" in prose
    assert "never see its own GPU" in prose


def test_a_typo_is_refused_with_what_there_is_and_then_asked_for(create):
    """The other half of `card_for`: a card that does not exist at all, which
    gets a different sentence from one that exists and cannot be driven."""
    result = create("--gpu", "rtx4090", "--dry-run", watching=True, answers="1\n4\n1\n")

    assert result.exit_code == 0, result.output
    assert "no card called 'rtx4090'" in result.output
    assert "GPU: l4" in result.output


def test_an_unusable_answer_to_the_prompt_is_asked_again(create):
    """A number outside the list, and a word. Neither may be taken as a choice —
    picking the nearest option is how a box gets made on the wrong card."""
    result = create("--os", "linux", "--dry-run", watching=True, answers="9\nl4\n4\n1\n")

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
        app, ["create", "--dry-run", "--config", str(path)], input="1\n4\n1\n")

    assert result.exit_code == 0, result.output
    assert "Which card?" in result.stderr
    assert "GPU: l4" in result.stderr
    assert "Which card?" not in result.stdout
    assert "Reserve it?" in result.stderr
    assert "reserve: no" in result.stderr
    assert "Reserve it?" not in result.stdout
    assert "what this makes:" in result.stdout


# --- the third question: reserve it? -----------------------------------------
#
# The same parity rule as the two above it, for a choice that differs from
# them in one way: it has a safe answer. `--os` and `--gpu` left off with
# nobody to ask is a refusal; `--reserve` left off is "no", SAID, because "no"
# is the answer that cannot bill anybody by surprise and every script written
# before this flag existed has to keep making the box it always made.


def _reserve_lines(result) -> list[str]:
    """The `Reserve it?` block and the line that settles it, in order.

    A menu row whose note is longer than the prose width continues on the next
    line, under its own first word; the continuation is joined back on here so
    a row is read as the one sentence it is.
    """
    out: list[str] = []
    for line in result.output.splitlines():
        text = line.strip()
        if text.startswith(("Reserve it?", "reserve:", "1. no", "2. yes")):
            out.append(" ".join(text.split()))
        elif out and out[-1].startswith("2. yes") and line.startswith(" " * 11):
            out[-1] += f" {text}"
    return out


def test_reserve_given_as_a_flag_is_confirmed_where_the_question_would_be(create):
    result = create("--os", "linux", "--gpu", "l4", "--reserve", "--dry-run",
                    watching=True)

    assert result.exit_code == 0, result.output
    assert _reserve_lines(result) == ["reserve: yes (from --reserve)"]
    assert "Reserve it?" not in result.output, "a flag that was given is not asked"


def test_no_reserve_is_an_answer_and_is_never_asked_about(create):
    """`--no-reserve` is not "nothing was said". Three states stay three."""
    result = create("--os", "linux", "--gpu", "l4", "--no-reserve", "--dry-run",
                    watching=True)

    assert result.exit_code == 0, result.output
    assert _reserve_lines(result) == ["reserve: no (from --no-reserve)"]


def test_reserve_left_off_at_a_terminal_is_asked_with_what_each_answer_costs(create):
    """Both rows, typed out. The second is the bill in one line: who holds it,
    that it bills by the hour, that stopping does not stop it, and what does."""
    result = create("--os", "linux", "--gpu", "l4", "--dry-run", watching=True,
                    answers="2\n")

    assert result.exit_code == 0, result.output
    assert _reserve_lines(result) == [
        "Reserve it?",
        "1. no pay only while it runs. A stopped box keeps its disk, not its place",
        "2. yes Google holds the capacity and bills for it every hour, running or "
        "stopped, until you delete the box",
        "reserve: yes",
    ]


def test_answering_yes_at_the_prompt_plans_a_reserved_box(create):
    """The answer has to reach the plan, not only the transcript."""
    result = create("--os", "linux", "--gpu", "l4", "--dry-run", watching=True,
                    answers="2\n")

    assert "reservation comfy-linux-rsv" in result.stdout
    assert "list_reservations" in result.gc.calls


def test_answering_no_at_the_prompt_plans_an_ordinary_box(create):
    result = create("--os", "linux", "--gpu", "l4", "--dry-run", watching=True,
                    answers="1\n")

    assert _reserve_lines(result)[-1] == "reserve: no"
    assert "comfy-linux-rsv" not in result.output
    assert "what this makes:" in result.stdout


def test_reserve_left_off_with_yes_is_not_asked_and_says_it_is_not_reserved(create):
    """`--yes` is "do not ask", and it means this question too."""
    result = create("--os", "linux", "--gpu", "l4", "--yes", watching=True)

    assert result.exit_code == 0, result.output
    assert _reserve_lines(result) == [
        "reserve: no (default — pass --reserve to hold the capacity)"]
    assert "create_reservation" not in result.gc.calls
    assert "create_instance_from_image" in result.gc.calls


def test_reserve_left_off_with_nobody_to_ask_says_it_is_not_reserved(create):
    """A script that worked before this flag existed makes the same box, and
    its log now says which way the decision went."""
    result = create("--os", "linux", "--gpu", "l4", "--dry-run", watching=False)

    assert result.exit_code == 0, result.output
    assert _reserve_lines(result) == [
        "reserve: no (default — pass --reserve to hold the capacity)"]


def test_the_prompted_and_the_flagged_reserve_are_otherwise_the_same_transcript(create):
    """Everything on stdout — the quota, the plan, the bill sentence, the zone
    order — is identical whichever way the yes arrived."""
    asked = create("--os", "linux", "--gpu", "l4", "--dry-run", watching=True,
                   answers="2\n")
    told = create("--os", "linux", "--gpu", "l4", "--reserve", "--dry-run",
                  watching=True)

    # From the first line the command itself prints: above it, the runner
    # echoes the typed answer onto stdout, which is the test's input and not
    # the tool's output.
    def said(result):
        return result.stdout[result.stdout.index("\nquota checked:"):]

    assert said(asked) == said(told)
    assert "is reserved. Google holds its capacity" in told.stdout


def test_the_card_menu_offers_no_gpu_and_says_what_that_means(create):
    result = create("--os", "linux", "--dry-run", watching=True, answers="6\n")

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    at = next(index for index, line in enumerate(lines)
              if line.strip().startswith("6. none"))
    said = " ".join(" ".join(lines[at:at + 2]).split())
    assert said.startswith(
        "6. none n1-standard-8 — no GPU; ComfyUI on the CPU, slow, and not "
        "counted against GPU quota")
    assert max(len(lines[at]), len(lines[at + 1])) <= 96, lines[at:at + 2]
    assert "GPU: none" in result.output


def test_a_box_with_no_gpu_is_not_asked_about_reserving_and_says_why(create):
    """The reserve question is about a card. There is none, so it is skipped —
    out loud, because the transcript says which way every decision went."""
    result = create("--os", "linux", "--gpu", "none", "--dry-run", watching=True)

    assert result.exit_code == 0, result.output
    assert _reserve_lines(result) == [
        "reserve: no (a box with no GPU is not reserved)"]
    assert "Reserve it?" not in result.output


def test_the_reserve_question_comes_after_the_card(create):
    result = create("--dry-run", watching=True, answers="1\n4\n1\n")

    out = result.output
    assert out.index("Which operating system?") < out.index("Which card?") \
        < out.index("Reserve it?")


def test_no_row_of_the_reserve_menu_runs_past_the_prose_width(create):
    result = create("--os", "linux", "--gpu", "l4", "--dry-run", watching=True,
                    answers="1\n")

    lines = result.output.splitlines()
    start = lines.index("Reserve it?")
    block = list(takewhile(lambda line: line.startswith("  "), lines[start + 1:]))
    assert len(block) == 3, "two rows, and the second runs on to a line of its own"
    assert max(len(line) for line in block) <= 96, block
    # The continuation sits under the note's first word, not under the number,
    # so it cannot be read as a third option.
    assert block[2].startswith(" " * block[1].index("Google"))
    assert block[2].strip() == "delete the box"


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
    assert "reserve" not in result.output, "it was not typed, so it is not restated"


def test_that_refusal_keeps_reserve_when_it_was_typed(create):
    """Dropped from the rewritten command, `--reserve` is not a detail: with
    nobody to ask, the box the remedy makes would be an unreserved one."""
    result = create("--os", "linux", "--gpu", "l4", "--reserve",
                    "--zone", "us-central1-a", "--region", "europe-west2")

    assert result.exit_code == 2
    assert "comfy-qat create --os linux --gpu l4 --reserve --zone us-central1-a" \
        in result.output
    assert "comfy-qat create --os linux --gpu l4 --reserve --region europe-west2" \
        in result.output
