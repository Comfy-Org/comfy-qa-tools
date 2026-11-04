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

import pytest

from fakes import FakeQaDesk
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
    assert "qa-desk login" in result.output


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
    assert command == ["qa-desk", "case", "pass", "1.60", "C-01", "--on", "local",
                       "--core-tag", "v1.60.1", "--note", stamp]
    writes = world.desk.writes()
    assert writes == [command + ["--dry-run", "--json"]], writes
    assert "the request it would send, not sent" in result.output
    assert "POST https://qadesk.engcomfy.com/agent/api/releases/1.60/cases/C-01/actions" \
        in result.output


def test_a_fail_puts_the_stamp_in_the_bug_text_because_case_fail_has_no_note(world):
    result = run(world, "fail", "1.60", "C-01", "--on", BOX, "--note",
                 "picking an unloaded tab shows Save failed", "--bug", "FE-1234")

    assert result.exit_code == 0, result.output
    (command,) = offered(result.output)
    stamp = kept(world)[0]["stamp_line"]
    assert command[:5] == ["qa-desk", "case", "fail", "1.60", "C-01"]
    assert command[command.index("--bug") + 1] == \
        f"picking an unloaded tab shows Save failed — {stamp}"
    assert "--note" not in command
    assert command[command.index("--on") + 1] == "local"
    assert command[command.index("--linear") + 1] == "FE-1234"
    assert all("--dry-run" in argv for argv in world.desk.writes())


def test_a_pass_on_testcloud_says_testcloud_and_carries_no_core_tag(world):
    result = run(world, "pass", "1.60", "C-01", "--on", "testcloud")

    assert result.exit_code == 0, result.output
    (command,) = offered(result.output)
    assert command[command.index("--on") + 1] == "testcloud"
    assert "--core-tag" not in command
    assert TESTCLOUD_SHA[:10] in command[command.index("--note") + 1]


def test_a_test_pr_report_is_drafted_and_run_only_as_a_dry_run(world):
    result = run(world, "report", "1.60", "22870", "--cases", "C-03,C-07")

    assert result.exit_code == 0, result.output
    (command,) = offered(result.output)
    assert command == ["qa-desk", "test", "report", "1.60", "22870", "--cases", "C-03,C-07"]
    assert world.desk.writes() == [command + ["--dry-run", "--json"]]
    assert "POST https://qadesk.engcomfy.com/agent/api/releases/1.60/tests" in result.output


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
