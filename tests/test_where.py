""""Test this" — `comfy-qat where` — through the real CLI (S3-1, S3-2, S3-3, S3-8).

What is asserted is what a tester reads: each place, its state and the reason,
for every kind of thing to test, on fakes of QA Desk, GitHub, the cloud
environments, Google and a ComfyUI behind a box's tunnel (`qaworld.py`). Each
test names its criterion in criteria-step3.md and was red before the command
existed.
"""

from __future__ import annotations

import json

import pytest

from fakes import FakeQaDesk
from qaworld import (BOX, FRONT, PROD_SHA, STAGING_SHA, TAG_1_58_5, TAG_1_60_1,
                     TESTCLOUD_SHA, flat, make, run)


@pytest.fixture
def world(monkeypatch, tmp_path):
    made = make(monkeypatch, tmp_path)
    yield made
    made.comfy.stop()


def rows(output: str) -> dict[str, str]:
    """{place: its whole table row}, from the table `where` prints."""
    found = {}
    for line in output.splitlines():
        for name in ("testcloud", "staging", "prod", BOX):
            if line.startswith(name + " "):
                found[name] = " ".join(line.split())
    return found


def give_1_60_a_core_tag(world) -> None:
    world.hub[f"{FRONT}/releases?per_page=100"].insert(0, {
        "tag_name": "v1.60.1", "target_commitish": "core/1.60",
        "published_at": "2026-11-04T00:00:00Z"})
    world.hub[f"{FRONT}/git/ref/tags/v1.60.1"] = {"object": {"sha": TAG_1_60_1}}


# --- S3-1: one command, every place, three states -----------------------------


def test_a_qa_desk_case_lists_every_place_with_a_state_and_a_reason(world):
    """S3-1 and S3-3: `where 1.60 C-01` reads the case with `qa-desk case show`
    and lists testcloud, staging, prod and the box, each with a state."""
    give_1_60_a_core_tag(world)
    world.serves("1.60.1")

    result = run(world, "where", "1.60", "C-01")

    assert result.exit_code == 0, result.output
    assert ["qa-desk", "case", "show", "1.60", "C-01", "--json"] in world.desk.calls
    found = rows(result.output)
    assert set(found) == {"testcloud", "staging", "prod", BOX}, result.output
    assert found["testcloud"].split()[1] == "Ready"
    assert "Not suitable" in found["staging"] and "Not suitable" in found["prod"]
    assert "Ready" in found[BOX], found[BOX]
    assert "frontend 1.60.1" in found[BOX], "a Ready row says the frontend it confirmed"
    assert "Picker loads an unopened draft" in result.output
    assert "shared-local" in result.output


def test_a_box_serving_another_frontend_can_be_prepared_with_what_changes_and_how_long(world):
    """S3-1: Can be prepared says what would change and roughly how long, and the
    command that does it is printed on its own line."""
    give_1_60_a_core_tag(world)
    world.serves("1.58.5")

    result = run(world, "where", "1.60", "C-01")

    assert result.exit_code == 0, result.output
    row = rows(result.output)[BOX]
    assert "Can be prepared" in row
    assert "frontend 1.58.5, not v1.60.1" in row
    assert "about 1 min" in row
    assert f"    comfy-qat go {BOX} --frontend-version v1.60.1" in result.output.splitlines()


def test_a_stopped_box_can_be_prepared_and_nothing_is_started(world):
    """S3-1: "No box is started by listing." Google is asked what the box is
    doing, and nothing else is asked of it."""
    give_1_60_a_core_tag(world)
    world.box("TERMINATED")

    result = run(world, "where", "1.60", "C-01")

    assert result.exit_code == 0, result.output
    row = rows(result.output)[BOX]
    assert "Can be prepared" in row and "stopped: go starts it (about 2 min)" in row
    acted = [call for call in world.gc.calls
             if call[0] not in ("list_instances", "list_reservations")]
    assert acted == [], acted
    assert "nothing was started or changed" in result.output


@pytest.mark.parametrize("asked, label", [
    (("--pr", "17497"), "ComfyUI PR 17497"),
    (("--version", "v0.43.0"), "ComfyUI v0.43.0"),
    (("--frontend-version", "v1.58.5"), "frontend v1.58.5"),
])
def test_a_pr_or_a_version_is_something_to_test_too(world, asked, label):
    """S3-1: not only QA Desk (Ali: "not just limited to qa desk"). A build is
    checked against GitHub first, and QA Desk is not asked anything."""
    world.serves("1.58.5")

    result = run(world, "where", *asked)

    assert result.exit_code == 0, result.output
    assert label in result.output
    assert world.desk.calls == []
    assert set(rows(result.output)) == {"testcloud", "staging", "prod", BOX}


def test_a_box_with_another_os_or_card_is_not_suitable_and_says_why(world):
    """S3-1: "Right" means the OS and the GPU as well as the build."""
    world.serves("1.58.5")

    windows = run(world, "where", "--frontend-version", "v1.58.5", "--machine", "windows")
    card = run(world, "where", "--frontend-version", "v1.58.5", "--machine", "linux/l4")
    fits = run(world, "where", "--frontend-version", "v1.58.5", "--machine", "linux/none")

    assert "Not suitable" in rows(windows.output)[BOX]
    assert "it is Ubuntu 22.04, and windows was asked for" in rows(windows.output)[BOX]
    assert "it is Ubuntu 22.04, and linux/l4 was asked for" in rows(card.output)[BOX]
    assert "Ready" in rows(fits.output)[BOX]


def test_a_release_with_no_core_tag_yet_says_so_on_every_machine(world):
    """S3-1, the real state of 1.60 on 3 Nov: its only v1.60.* release was made
    on main before the cut, and QA Desk's plan waits for one on core/1.60."""
    world.serves("1.60.0")

    result = run(world, "where", "1.60", "C-30")

    row = rows(result.output)[BOX]
    assert "Not suitable" in row and "release 1.60 has no core tag yet" in row


def test_the_json_carries_the_same_answer(world):
    give_1_60_a_core_tag(world)
    world.serves("1.60.1")

    result = run(world, "where", "1.60", "C-01", "--json")

    assert result.exit_code == 0, result.output
    found = json.loads(result.output)
    states = {place["place"]: place["state"] for place in found["places"]}
    assert states == {"testcloud": "Ready", "staging": "Not suitable",
                      "prod": "Not suitable", BOX: "Ready"}
    assert found["test_target"] == "shared-local" and found["core_tag"] == "v1.60.1"


# --- S3-1, the case's target decides which kinds count --------------------------


def test_a_cloud_case_counts_no_machine(world):
    give_1_60_a_core_tag(world)
    world.serves("1.60.1")

    found = rows(run(world, "where", "1.60", "C-05").output)

    assert "Ready" in found["testcloud"]
    assert "Not suitable" in found[BOX] and "a cloud case is tested on testcloud" in found[BOX]


def test_a_core_case_counts_no_cloud_environment(world):
    give_1_60_a_core_tag(world)
    world.serves("1.60.1")

    found = rows(run(world, "where", "1.60", "C-30").output)

    assert "Ready" in found[BOX]
    for cloud in ("testcloud", "staging", "prod"):
        assert "Not suitable" in found[cloud], found[cloud]
        assert "a core case is tested on a local ComfyUI" in found[cloud]


# --- S3-2: the cloud environments, with the frontend each serves ---------------


def test_the_cloud_environments_are_listed_with_the_frontend_each_serves(world):
    """S3-2: testcloud, staging and prod, each with the commit its
    `x-frontend-version` header names, read through the `env` reader."""
    result = run(world, "where", "1.60")

    found = rows(result.output)
    assert TESTCLOUD_SHA[:8] in found["testcloud"]
    assert "cloud/1.60 backport" in found["testcloud"]
    assert STAGING_SHA[:8] in found["staging"] and PROD_SHA[:8] in found["prod"]
    assert world.probed == ["testcloud", "stagingcloud", "cloud"]


def test_a_cloud_environment_on_the_wrong_frontend_is_not_suitable_with_the_reason(world):
    """S3-2: staging and prod serve cloud/1.59 backports; for release 1.60 they
    are Not suitable, and the reason names the branch."""
    found = rows(run(world, "where", "1.60").output)

    assert "Ready" in found["testcloud"] and "1 commit behind its tip" in found["testcloud"]
    assert "Not suitable" in found["staging"]
    assert "which is not on cloud/1.60" in found["staging"]
    assert "a cloud/1.59 backport" in found["staging"]


def test_cloud_is_judged_by_the_exact_commit_for_a_frontend_release(world):
    """S3-2: for `--frontend-version v1.58.5`, a cloud environment is right only
    when it serves that tag's commit."""
    world.clouds["stagingcloud"] = TAG_1_58_5

    found = rows(run(world, "where", "--frontend-version", "v1.58.5").output)

    assert "Ready" in found["staging"]
    assert "Not suitable" in found["testcloud"]
    assert f"not frontend v1.58.5 ({TAG_1_58_5[:8]})" in found["testcloud"]


def test_a_backend_build_is_never_a_cloud_environment(world):
    found = rows(run(world, "where", "--pr", "17497").output)

    for cloud in ("testcloud", "staging", "prod"):
        assert "cloud runs its own backend" in found[cloud]


def test_a_cloud_environment_that_does_not_answer_is_not_suitable(world):
    world.clouds.pop("cloud")

    found = rows(run(world, "where", "1.60").output)

    assert "Not suitable" in found["prod"] and "could not read which frontend" in found["prod"]


def test_staging_on_the_release_is_still_not_a_place_qa_desk_records_from(world):
    """For a QA Desk case, a cloud result is recorded `--on testcloud` only."""
    world.hub[f"{FRONT}/compare/cloud/1.60...{STAGING_SHA}"] = {"status": "identical"}

    found = rows(run(world, "where", "1.60", "C-01").output)

    assert "Not suitable" in found["staging"]
    assert "QA Desk records a cloud result on testcloud only" in found["staging"]


# --- S3-3: QA Desk read-only, and missing or signed out ------------------------


def test_reading_a_case_only_ever_runs_read_only_qa_desk_commands(world):
    """S3-3: the tool uses `qa-desk case show --json`, and nothing it runs is a
    write. The token is never an argument: there is nothing to print."""
    give_1_60_a_core_tag(world)
    run(world, "where", "1.60", "C-01")

    assert world.desk.calls and world.desk.writes() == []
    assert all("--json" in call for call in world.desk.calls)
    assert not any("token" in word.lower() for call in world.desk.calls for word in call)


@pytest.mark.parametrize("desk, said, fix", [
    (FakeQaDesk(installed=False), "qa-desk is not installed here",
     "curl -fsSL https://qadesk.engcomfy.com/agent/cli/install.sh | sh"),
    (FakeQaDesk(signed_in=False), "qa-desk is not signed in", "qa-desk login"),
])
def test_qa_desk_missing_or_signed_out_is_said_with_the_command_that_fixes_it(
        monkeypatch, tmp_path, desk, said, fix):
    """S3-3 and S3-8: what is wrong, and what to do, exit 2."""
    world = make(monkeypatch, tmp_path, desk=desk)
    try:
        result = run(world, "where", "1.60", "C-01")
    finally:
        world.comfy.stop()

    assert result.exit_code == 2, result.output
    assert said in flat(result.output)
    assert "Nothing else in comfy-qat needs it" in flat(result.output)
    assert fix in result.output.splitlines()[-1] or fix in result.output


def test_the_two_commands_that_fix_a_missing_qa_desk_are_one_block(monkeypatch, tmp_path):
    """The install line and `qa-desk login` are pasted one after the other, so
    nothing comes between them — a `qa-desk` line in a fix is a command, not
    prose that opens a new group."""
    world = make(monkeypatch, tmp_path, desk=FakeQaDesk(installed=False))
    try:
        result = run(world, "where", "1.60", "C-01")
    finally:
        world.comfy.stop()

    lines = [line.strip() for line in result.output.splitlines()]
    install = lines.index("curl -fsSL https://qadesk.engcomfy.com/agent/cli/install.sh | sh")
    assert lines[install + 1] == "qa-desk login", lines


def test_everything_else_still_works_without_qa_desk(monkeypatch, tmp_path):
    """S3-3: with no `qa-desk` at all, "Test this" for a PR still lists every
    place, and asks QA Desk nothing."""
    world = make(monkeypatch, tmp_path, desk=FakeQaDesk(installed=False))
    try:
        world.serves("1.58.5")
        result = run(world, "where", "--pr", "17497")
    finally:
        world.comfy.stop()

    assert result.exit_code == 0, result.output
    assert set(rows(result.output)) == {"testcloud", "staging", "prod", BOX}
    assert world.desk.calls == []


def test_a_case_qa_desk_does_not_have_is_said_plainly(world):
    result = run(world, "where", "1.60", "C-99")

    assert result.exit_code == 2, result.output
    assert "QA Desk has no such thing: No case C-99 in 1.60" in flat(result.output)
    assert "qa-desk case list <version>" in result.output


# --- S3-8: plain words, every refusal with a fix ---------------------------------


@pytest.mark.parametrize("args, said", [
    ((), "say what to test"),
    (("1.60", "C-01", "--pr", "17497"), "is not given with a PR or a version as well"),
    (("16",), "'16' is not a release"),
    (("1.60", "first"), "'first' is not a QA Desk case"),
    (("--machine", "windows l4", "--pr", "17497"), "is not a description of a machine"),
    (("--pr", "99999999"), "has no pull request 99999999"),
])
def test_every_refusal_says_what_is_wrong_and_what_to_do(world, args, said):
    result = run(world, "where", *args)

    assert result.exit_code == 2, result.output
    assert said in flat(result.output)
    assert "to fix:" in result.output
