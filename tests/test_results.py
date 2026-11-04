"""`pass`, `fail`, `results`, `report` and `bug`, through the real CLI (S3-4,
S3-5, S3-7, S3-8).

A result is kept here whatever QA Desk says; for a QA Desk case the exact
`qa-desk` command is printed and only its `--dry-run` is run; a bug is a draft.
Every argv the fake QA Desk is handed is also read by the conftest tripwire,
which fails a test that ever ran a QA Desk write without `--dry-run`.
"""

from __future__ import annotations

import json
import shlex
import sys

import pytest

from fakes import FakeQaDesk, _qa_desk_parse
from qaworld import BOX, FRONT, TAG_1_60_1, TESTCLOUD_SHA, flat, make, run


@pytest.fixture
def world(monkeypatch, tmp_path):
    made = make(monkeypatch, tmp_path)
    made.hub[f"{FRONT}/releases?per_page=100"].insert(0, {
        "tag_name": "v1.60.1", "target_commitish": "core/1.60",
        "published_at": "2026-11-04T00:00:00Z"})
    made.hub[f"{FRONT}/git/ref/tags/v1.60.1"] = {"object": {"sha": TAG_1_60_1}}
    made.serves("1.60.1")
    yield made
    made.comfy.stop()


def kept(world) -> list[dict]:
    folder = world.config.parent / "results"
    return [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(folder.glob("*.json"))]


def offered(output: str) -> list[list[str]]:
    """Every `qa-desk …` line the output offers, split as a shell splits it."""
    return [shlex.split(line.strip()) for line in output.splitlines()
            if line.strip().startswith("qa-desk ")]


def read_by_qa_desk(command: list[str]) -> tuple[list[str], dict]:
    """An offered command as QA Desk's own CLI reads it: its words and flags."""
    return _qa_desk_parse(command[1:])


def fixes(output: str) -> str:
    """What the output says to do: the lines from the first `to fix:` on —
    never the problem above it, which may quote QA Desk's own advice."""
    lines = output.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("to fix:")]
    assert starts, output
    return "\n".join(lines[starts[0]:])


# --- S3-4: a result is kept here, with or without QA Desk -------------------------


def test_a_pass_is_kept_here_with_what_where_the_stamp_the_time_and_a_note(world):
    result = run(world, "pass", "1.60", "C-01", "--on", BOX, "--note", "both tabs kept",
                 "--bug", "FE-1234")

    assert result.exit_code == 0, result.output
    (saved,) = kept(world)
    assert saved["outcome"] == "pass" and saved["what"] == "1.60 C-01"
    assert saved["place"] == BOX and saved["place_kind"] == "machine"
    assert saved["stamp"]["frontend_version"] == "1.60.1"
    assert saved["stamp"]["comfyui_version"] == "0.43.0"
    assert "frontend 1.60.1" in saved["stamp_line"]
    assert saved["note"] == "both tabs kept" and saved["bug"] == "FE-1234"
    assert saved["at"].endswith("Z") and saved["comfy_qat"].startswith("comfy-qat ")


def test_the_results_go_beside_the_host_list_never_into_a_repository(world):
    from pathlib import Path

    run(world, "pass", "1.60", "C-01", "--on", BOX)

    repository = Path(__file__).resolve().parent.parent
    assert (world.config.parent / "results").is_dir()
    assert not (repository / "results").exists()


def test_the_results_can_be_listed(world):
    run(world, "pass", "1.60", "C-01", "--on", BOX)
    run(world, "fail", "--pr", "17497", "--on", BOX, "--note", "workflow will not load",
        "--bug", "e18791")

    listed = run(world, "results")
    as_json = json.loads(run(world, "results", "--json").output)

    assert listed.exit_code == 0, listed.output
    lines = listed.output.splitlines()
    assert lines[0].split() == ["WHEN", "RESULT", "WHAT", "WHERE", "BUG", "NOTE"]
    assert any("pass" in line and "1.60 C-01" in line for line in lines)
    assert any("fail" in line and "ComfyUI PR 17497" in line and "e18791" in line
               for line in lines)
    assert [entry["outcome"] for entry in as_json] == ["pass", "fail"]


def test_a_result_with_no_qa_desk_case_is_kept_and_qa_desk_is_not_asked(world):
    result = run(world, "pass", "--pr", "17497", "--on", BOX)

    assert result.exit_code == 0, result.output
    assert kept(world)[0]["what"] == "ComfyUI PR 17497"
    assert world.desk.calls == []
    assert "no QA Desk case was named" in flat(result.output)


def test_a_result_is_kept_even_when_qa_desk_cannot_be_read(monkeypatch, tmp_path):
    """S3-3/S3-4: signed out, the case is still kept and the draft still printed;
    the dry run is said not to have run, with the command that fixes it."""
    world = make(monkeypatch, tmp_path, desk=FakeQaDesk(signed_in=False))
    try:
        world.serves("1.60.1")
        result = run(world, "pass", "1.60", "C-01", "--on", BOX)
    finally:
        world.comfy.stop()

    assert result.exit_code == 0, result.output
    assert kept(world)[0]["case"] == "C-01"
    assert offered(result.output)[0][:5] == ["qa-desk", "case", "pass", "1.60", "C-01"]
    assert "its dry run could not be run" in flat(result.output)
    # comfy-qat's own fix, not QA Desk's message ("Run qa-desk login…") quoted
    # in the warning above it (code audit T5).
    assert "qa-desk login" in [line.strip() for line in fixes(result.output).splitlines()]


def test_a_result_needs_where_it_was_tested_and_says_how_to_give_it(world):
    """S3-8: the refusal's fix is held, not only its words (behaviour audit F12)."""
    result = run(world, "pass", "1.60", "C-01")

    assert result.exit_code == 2, result.output
    assert "say where you tested it with --on" in flat(result.output)
    assert "--on testcloud" in fixes(result.output)
    assert kept(world) == []


@pytest.mark.parametrize("outcome", ["pass", "fail"])
def test_a_result_with_nothing_named_is_refused_and_not_kept(world, outcome):
    """S3-4: what was tested is half of a result; kept as "?" it says nothing
    (code audit T9)."""
    result = run(world, outcome, "--on", BOX, "--note", "it broke")

    assert result.exit_code == 2, result.output
    assert "say what you tested" in flat(result.output)
    assert f"comfy-qat {outcome} 1.60 C-01 --on <place>" in fixes(result.output)
    assert kept(world) == []


def test_a_case_qa_desk_does_not_have_keeps_no_result(world):
    """S3-4/S3-8: QA Desk answered that there is no such case (its exit 5), so a
    result kept under it is one nobody could send (behaviour audit F9)."""
    result = run(world, "fail", "1.60", "C-404", "--on", "testcloud", "--note", "broke")

    assert result.exit_code == 2, result.output
    assert "QA Desk has no such thing: No case C-404 in 1.60" in flat(result.output)
    assert "Nothing was kept" in flat(result.output)
    assert "qa-desk case list <version>" in fixes(result.output)
    assert kept(world) == []


def test_a_fail_needs_to_say_what_broke(world):
    result = run(world, "fail", "1.60", "C-01", "--on", BOX)

    assert result.exit_code == 2, result.output
    assert "say what broke with --note" in flat(result.output)
    assert "to fix:" in result.output
    assert kept(world) == []


def test_a_box_that_does_not_answer_keeps_no_result(world):
    world.comfy.mode = "reset"

    result = run(world, "pass", "1.60", "C-01", "--on", BOX)

    assert result.exit_code == 1, result.output
    assert "did not answer, so there is no stamp of what was tested" in flat(result.output)
    assert kept(world) == []


# --- S3-5: QA Desk drafts, dry run only -------------------------------------------


def test_a_pass_prints_the_exact_qa_desk_command_and_runs_only_its_dry_run(world):
    result = run(world, "pass", "1.60", "C-01", "--on", BOX)

    assert result.exit_code == 0, result.output
    (command,) = offered(result.output)
    stamp = kept(world)[0]["stamp_line"]
    assert command == ["qa-desk", "case", "pass", "1.60", "C-01", "--on=local",
                       "--core-tag=v1.60.1", f"--note={stamp}"]
    writes = world.desk.writes()
    assert writes == [command + ["--dry-run", "--json"]], writes
    assert world.desk.sent() == []
    assert "the request it would send, not sent" in result.output
    assert "POST https://qadesk.engcomfy.com/agent/api/releases/1.60/cases/C-01/actions" \
        in result.output


def test_a_fail_puts_the_stamp_in_the_bug_text_because_case_fail_has_no_note(world):
    result = run(world, "fail", "1.60", "C-01", "--on", BOX, "--note",
                 "picking an unloaded tab shows Save failed", "--bug", "FE-1234")

    assert result.exit_code == 0, result.output
    (command,) = offered(result.output)
    stamp = kept(world)[0]["stamp_line"]
    words, flags = read_by_qa_desk(command)
    assert words == ["case", "fail", "1.60", "C-01"]
    assert flags["bug"] == f"picking an unloaded tab shows Save failed — {stamp}"
    assert "note" not in flags
    assert flags["on"] == "local" and flags["linear"] == "FE-1234"
    assert world.desk.writes() and world.desk.sent() == []


def test_a_pass_on_testcloud_says_testcloud_and_carries_no_core_tag(world):
    result = run(world, "pass", "1.60", "C-01", "--on", "testcloud")

    assert result.exit_code == 0, result.output
    (command,) = offered(result.output)
    _, flags = read_by_qa_desk(command)
    assert flags["on"] == "testcloud"
    assert "core-tag" not in flags
    assert TESTCLOUD_SHA[:10] in flags["note"]


def test_a_test_pr_report_is_drafted_and_run_only_as_a_dry_run(world):
    result = run(world, "report", "1.60", "22870", "--cases", "C-03,C-07")

    assert result.exit_code == 0, result.output
    (command,) = offered(result.output)
    assert command == ["qa-desk", "test", "report", "1.60", "22870", "--cases=C-03,C-07"]
    assert world.desk.writes() == [command + ["--dry-run", "--json"]]
    assert world.desk.sent() == []
    assert "POST https://qadesk.engcomfy.com/agent/api/releases/1.60/tests" in result.output


@pytest.mark.parametrize("outcome, flag, field", [
    ("pass", "note", "note"),
    ("fail", "bug", "bug_summary"),
])
def test_a_note_that_starts_with_dashes_reaches_qa_desk_whole(world, outcome, flag, field):
    """S3-5: QA Desk's CLI reads `--note <text>` with a text that starts with
    `--` as a `--note` with no value — a pass lost its note and its stamp, and
    a fail was exit 6, "--bug is required" (code audit T2). Attached with `=`,
    the CLI keeps it word for word."""
    note = "--frontend-version is ignored by the picker"

    result = run(world, outcome, "1.60", "C-01", "--on", "testcloud", "--note", note)

    assert result.exit_code == 0, result.output
    assert "could not be run" not in result.output, result.output
    (command,) = offered(result.output)
    _, flags = read_by_qa_desk(command)
    assert flags[flag].startswith(note + " — testcloud · frontend"), flags
    assert f'{field}: "{note} — testcloud' in result.output, result.output
    assert world.desk.sent() == []


@pytest.mark.parametrize("args", [
    ("report", "1.60", "QATEST-PR", "--cases", "C-01", "--note", "--"),
    ("report", "1.60", "QATEST-PR", "--cases", "--"),
])
def test_a_lone_double_dash_in_a_value_is_still_only_a_dry_run(world, args):
    """S3-5: a bare `--` ends QA Desk's flags, so `… --note -- --dry-run`
    would have been SENT — the `--dry-run` after it is a word to qa-desk — while
    comfy-qat's guard and the suite's tripwire, asking only whether `--dry-run`
    was in the argv, let it through (behaviour audit F1). Attached with `=`, it
    is the value; and the tripwire now reads the argv as qa-desk does."""
    result = run(world, *args)

    assert result.exit_code == 0, result.output
    assert world.desk.writes(), "the dry run was not run at all"
    assert world.desk.sent() == [], world.desk.sent()
    assert "the request it would send, not sent" in result.output


def test_a_test_pr_that_starts_with_a_dash_is_refused_before_qa_desk_is_asked(world):
    """S3-5 and S3-8: the PR is the one word of the draft that is no flag's
    value, so a `--` there would end QA Desk's flags (behaviour audit F1)."""
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    # `--config` in front, at the root: after `--` every word is an argument.
    result = CliRunner().invoke(app, ["--config", str(world.config), "report", "1.60",
                                      "--cases", "C-01", "--", "--"])

    assert result.exit_code == 2, result.output
    assert "'--' is not a pull request" in flat(result.output)
    assert "to fix:" in result.output
    assert world.desk.calls == []


def test_a_write_without_dry_run_is_refused_before_anything_is_started(world):
    """S3-5: the package's own rule — `qadesk.run` refuses it, and nothing
    reaches the QA Desk CLI."""
    from comfy_qa import qadesk

    with pytest.raises(qadesk.QaDeskError, match="only as a dry run"):
        qadesk.run(["case", "pass", "1.60", "C-01", "--on", "local"])
    assert world.desk.calls == []


def test_no_non_dry_run_write_is_ever_executed_even_with_the_rule_broken(
        world, monkeypatch, no_test_runs_a_qa_desk_write):
    """S3-5's proof: with the package's own rule broken, the write that gets
    through is refused where it would run, and kept for teardown — so no test in
    this suite can ever execute one. Cleared here because it was aimed at."""
    from comfy_qa import qadesk

    monkeypatch.setattr(qadesk, "unsent", lambda argv: True)
    with pytest.raises(AssertionError, match="without --dry-run"):
        qadesk.run(["case", "pass", "1.60", "C-01", "--on", "local"])
    assert world.desk.calls == [], "the write reached QA Desk's CLI"
    assert len(no_test_runs_a_qa_desk_write) == 1
    no_test_runs_a_qa_desk_write.clear()


@pytest.mark.parametrize("argv", [
    ["logout", "--dry-run"],
    ["login", "--dry-run"],
    ["token", "--dry-run"],
    ["setup", "--dry-run"],
    ["case", "claim", "1.60", "C-01", "--dry-run"],
])
def test_only_the_three_drafts_are_ever_run_as_dry_runs(world, argv):
    """S3-5: QA Desk's CLI runs `login`, `logout`, `token` and `setup` before it
    ever looks at `--dry-run` (`logout` rewrites its config either way), so a
    dry run of anything but `case pass`, `case fail` and `test report` — the
    three this tool drafts — is refused before it starts (code audit T10)."""
    from comfy_qa import qadesk

    assert not qadesk.unsent(["qa-desk", *argv])
    with pytest.raises(qadesk.QaDeskError, match="was not run"):
        qadesk.run(argv)
    assert world.desk.calls == []


def test_the_dry_run_is_read_as_qa_desk_reads_it():
    """S3-5: `--dry-run` counts only as a flag before any bare `--`, and not with
    an empty value — QA Desk's `if (flags["dry-run"])`."""
    from comfy_qa import qadesk

    assert qadesk.unsent(["qa-desk", "case", "pass", "1.60", "C-01", "--note=--",
                          "--dry-run", "--json"])
    assert not qadesk.unsent(["qa-desk", "test", "report", "1.60", "X", "--note", "--",
                              "--dry-run", "--json"])
    assert not qadesk.unsent(["qa-desk", "test", "report", "1.60", "--", "--cases=C-01",
                              "--dry-run"])
    assert not qadesk.unsent(["qa-desk", "case", "pass", "1.60", "C-01", "--dry-run="])
    assert qadesk.unsent(["qa-desk", "case", "show", "1.60", "C-01", "--json"])


def test_the_suites_qa_desk_reads_flags_as_the_package_does():
    """The fake and the package each carry a copy of QA Desk's own `BOOLEAN`
    (main.ts) — the fake had five of its eleven (code audit T12) — and each its
    own `parse`; held to agree, so neither drifts alone."""
    import fakes

    from comfy_qa import qadesk

    assert fakes.QA_DESK_BOOLEAN == qadesk.BOOLEAN
    assert len(qadesk.BOOLEAN) == 11
    for argv in (["case", "pass", "1.60", "C-01", "--note", "--x", "--dry-run"],
                 ["test", "report", "1.60", "--", "--cases", "C-01"],
                 ["case", "fail", "1.60", "C-01", "--bug=", "--on", "local"],
                 ["case", "list", "1.60", "--mine", "x", "--needs-test", "y"]):
        assert fakes._qa_desk_parse(argv) == qadesk.parse(argv), argv


def test_qa_desk_refusing_its_input_says_what_it_was_given_not_to_retry(world):
    """S3-8: QA Desk's exit 6 is its own check of what it was given; running it
    again changes nothing (code audit T3)."""
    from comfy_qa import qadesk

    with pytest.raises(qadesk.QaDeskError) as raised:
        qadesk.dry_run(["case", "fail", "1.60", "C-01", "--on=local"])

    assert "--bug is required" in str(raised.value)
    assert "qa-desk case fail 1.60 C-01 --on=local --dry-run --json" in raised.value.fix
    assert "retried" not in raised.value.fix


@pytest.mark.parametrize("code, said", [
    (2, "asks for a confirmation first"),
    (4, "the state of things does not allow this"),
])
def test_qa_desk_exits_2_and_4_say_how_to_see_the_case(monkeypatch, tmp_path, code, said):
    """S3-8: exit 2 (somebody else holds the case) and 4 (its state does not
    allow it) are not a dropped connection; the fix is to look (code audit T3)."""
    world = make(monkeypatch, tmp_path, desk=FakeQaDesk(exits={("case", "show"): code}))
    try:
        result = run(world, "where", "1.60", "C-01")
    finally:
        world.comfy.stop()

    assert result.exit_code == 2, result.output
    assert said in flat(result.output)
    assert "qa-desk case show 1.60 C-01" in fixes(result.output)
    assert "retried" not in result.output


def test_a_cut_off_answer_is_said_to_be_cut_off_not_old(monkeypatch, tmp_path):
    """S3-8: an answer that began as JSON and stopped was told "update qa-desk",
    the wrong cause and the wrong fix (behaviour audit F2)."""
    world = make(monkeypatch, tmp_path, desk=FakeQaDesk(cut_off_at=30))
    try:
        result = run(world, "where", "1.60")
    finally:
        world.comfy.stop()

    assert result.exit_code == 1, result.output
    assert "qa-desk's answer stopped part-way" in flat(result.output)
    assert "update qa-desk" not in result.output
    assert "qa-desk release show 1.60 --json" in fixes(result.output)


# What `qa-desk` does with a long answer when its stdout is a pipe: it exits
# before the pipe drains, and the reader gets the first 65,536 bytes (4 Nov:
# `release show 1.60 --json` is 128,474 bytes into a file, 65,536 through a pipe).
_PIPED_QA_DESK = """
import json, os, stat, sys
answer = json.dumps({"version": "1.60", "cases": [{"key": "C-%02d" % n, "title": "x" * 900}
                                                   for n in range(140)]})
if stat.S_ISFIFO(os.fstat(1).st_mode):
    answer = answer[:65536]
sys.stdout.write(answer)
sys.stdout.flush()
"""


def test_a_long_qa_desk_answer_is_read_whole_not_through_a_pipe(tmp_path):
    """S3-1: `where <release>` against the real QA Desk said its answer was not
    JSON, because `release show` stopped at 64 KiB through the pipe it was read
    from (behaviour audit F2). The stand-in behaves as the CLI does: whole into
    a file, cut off into a pipe."""
    from comfy_qa import qadesk

    script = tmp_path / "qa_desk_stand_in.py"
    script.write_text(_PIPED_QA_DESK, encoding="utf-8")

    code, out, err = qadesk._run_real([sys.executable, str(script), "release", "show",
                                       "1.60", "--json"])

    assert code == 0, err
    assert len(out.encode("utf-8")) > 65536
    assert len(json.loads(out)["cases"]) == 140


def test_a_dry_run_answer_never_prints_a_token_even_if_one_came_back():
    from comfy_qa import qadesk

    lines = qadesk.request_lines({"dry_run": True, "request": {
        "method": "POST", "url": "https://qadesk.engcomfy.com/agent/api/x",
        "body": {"note": "ok", "token": "qad_secret", "authorization": "Bearer qad_x"}}})

    assert not any("qad_" in line for line in lines), lines
    assert lines[0] == "POST https://qadesk.engcomfy.com/agent/api/x"


@pytest.mark.parametrize("case, on, said", [
    ("C-05", BOX, "is a cloud case"),
    ("C-30", "testcloud", "is a core case"),
    ("C-01", "staging", "QA Desk records a cloud result on testcloud only"),
])
def test_a_result_qa_desk_could_not_take_is_refused_and_not_kept(world, case, on, said):
    """S3-8: what is wrong, what to do, and nothing kept."""
    result = run(world, "pass", "1.60", case, "--on", on)

    assert result.exit_code == 2, result.output
    assert said in flat(result.output)
    assert "to fix:" in result.output
    assert kept(world) == []


# --- S3-7: Raise bug, drafted and never sent -------------------------------------


def test_a_bug_is_drafted_in_the_house_format_from_the_stamp(world):
    result = run(world, "bug", "--pr", "17497", "--on", BOX, "--title",
                 "Picker toast on an unloaded tab", "--note",
                 "Picking an unloaded unsaved tab shows Save failed",
                 "--expected", "the chat switches to that workflow")

    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    start = lines.index("Picker toast on an unloaded tab")
    title, gap, paragraph, gap2, environment = lines[start:start + 5]
    assert gap == gap2 == ""
    assert paragraph.startswith("Picking an unloaded unsaved tab shows Save failed. "
                                "Expected: the chat switches to that workflow.")
    assert f"Found testing ComfyUI PR 17497 on {BOX}" in paragraph
    assert " I " not in f" {paragraph} " and not paragraph.startswith("I ")
    assert environment.startswith("comfy-qat ") and "frontend 1.60.1" in environment
    assert "ComfyUI 0.43.0" in environment
    assert "sent by you" in result.output
    assert world.desk.calls == []


def test_a_bug_names_the_recording_to_attach(world, tmp_path):
    from comfy_qa import results

    made = tmp_path / "picker.spec.ts"
    made.write_text("test('picker', () => {})", encoding="utf-8")
    results.remember_recording(world.config, BOX, [str(made)])

    result = run(world, "bug", "--on", BOX, "--title", "T", "--note", "N")

    assert f"recording to attach: {made}" in result.output


def test_a_bug_needs_a_title_and_what_happened(world):
    result = run(world, "bug", "--on", BOX)

    assert result.exit_code == 2, result.output
    assert "a bug needs --title and --note" in flat(result.output)
    assert "to fix:" in result.output
