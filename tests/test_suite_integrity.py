"""Nothing quietly stops being tested.

`tests/test_stamp_hostile.py` — 40 tests, the only cover over the redirect
refusal, `looks_like_comfyui`, `MAX_BODY` and every no-traceback path in `fetch`
— was deleted by a merge and nobody noticed for three commits. It went in on one
branch, was removed by hand on another while a conflict in `stamp.py` was being
resolved, and when the two branches met again git saw "deleted by us, unmodified
by them" and resolved it silently. No conflict, no warning.

What hid it is the part worth writing a test about: **the total went up.** 841 to
869 across the merge that dropped those 40, because 28 new ones arrived in the
same commit. Every habit anyone has for noticing lost work — the number is
bigger, the suite is green, the diff is mostly additions — reported success. A
rising count is not evidence that nothing went missing, and neither is a green
run: deleted tests do not fail.

So the file list is asserted directly. It is hand-maintained, which is normally
the thing this suite avoids — `test_docs.py` exists partly to get rid of a
hand-maintained list. The difference is what maintenance costs: this list changes
only when a file is deliberately added or renamed, the fix is one line, and the
failure it catches is one that no amount of reading the numbers will.

---

**READING THE RESULT IS ITS OWN HAZARD, AND IT HAS NOW COST TWO SWEEPS.**
`xfailed` CONTAINS `failed`. Any shell test that matches the substring —
`grep failed`, `case "$out" in *failed*)`, `[[ $out == *failed* ]]` — matches a
run in which NOTHING failed, because the summary line always ends
`N passed, M xfailed`. It reports failure on a green suite, and if the habit is
inverted it reports success on a red one.

Both instances were mutation sweeps, where the whole method is "delete the fix
and watch the count move", so a result-reader that always says the same thing
voids the entire run rather than one case of it. The first void'd a day of
mutation results; the second cost verify3 a sweep. Neither was a Python problem
— `pytest`'s own `-q` summary is what is being read, and it is read in a shell.

Match the ANCHORED, uppercase form pytest prints one per failure:

    grep -c '^FAILED tests/'

or read the integers out of the summary. Never the bare word. The same trap sits
in `passed` vs `xpassed`, which has not bitten anyone yet only because nobody has
grepped for it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent

# Every test module that must exist. Adding a file? Add it here. Renaming one?
# Rename it here. Deleting one deliberately is a decision, and deleting the line
# is how you record having made it.
REQUIRED = {
    "test_auth",
    "test_auth_flow",
    "test_config",
    "test_config_hostile",
    "test_config_inheritance",
    "test_create",
    "test_create_cli",
    "test_create_e2e",
    "test_create_hostile",
    "test_detached",
    "test_detached_e2e",
    "test_discover",
    "test_doc_coverage",
    "test_docs",
    "test_env",
    "test_gcloud_auth",
    "test_delete",
    "test_host_costs",
    "test_interrupt",
    "test_hostfile",
    "test_lifecycle",
    "test_lifecycle_e2e",
    "test_move",
    "test_no_colour",
    "test_old_spellings",
    "test_option_help",
    "test_provision",
    "test_quota",
    "test_quota_regions",
    "test_readme",
    "test_relay",
    "test_relocate",
    "test_resolve",
    "test_say",
    "test_selector_flags",
    "test_setup",
    "test_setup_flow",
    "test_shell_access",
    "test_tripwires",
    "test_stamp",
    "test_stamp_hostile",
    "test_stamp_mismatch",
    "test_stamp_payloads",
    "test_suite_integrity",
    "test_speed_guards",
    "test_switch",
    "test_tripwire",
    "test_tunnel",
    "test_tunnel_identity",
    "test_version",
    "test_zones",
}

# conftest.py is deliberately not in that list, and does not need to be.
#
# It is not a `test_*.py`, so `present()` cannot see it — which raises the fair
# question of whether the same silent "deleted by us, unmodified by them" merge
# could drop it too. It could, and it would matter more than losing a test file:
# conftest.py holds the autouse fixture that stops the suite spawning real
# `gcloud compute start-iap-tunnel` processes, and eleven were once alive at once
# on the machine this was written on.
#
# Checked rather than assumed: with conftest.py removed the suite does not go
# quietly green, it goes to five failures and an error, and the error is
# `test_tunnel_identity.py::test_the_suite_never_starts_a_real_tunnel` — a guard
# that cannot even collect without the fixture it names. So that file already
# announces its own absence, loudly, and listing it here would add nothing.
#
# Do not "fix" this by adding "conftest" to REQUIRED: `present()` globs
# `test_*.py`, so it would report the file missing on every run.


def present() -> set[str]:
    return {path.stem for path in TESTS.glob("test_*.py")}


def test_no_test_module_has_gone_missing():
    missing = sorted(REQUIRED - present())
    assert not missing, (
        f"{', '.join(missing)} is in the required list and not on disk. A merge "
        f"resolving 'deleted by us, unmodified by them' does this silently, and the "
        f"suite stays green because deleted tests do not fail. Restore the file from "
        f"git history — or, if it was renamed or dropped on purpose, say so by "
        f"editing REQUIRED in this file."
    )


def test_the_required_list_has_not_gone_stale():
    """A list that stops describing the directory stops protecting it.

    A new file left out of REQUIRED is unprotected, and this is what says so
    while the omission is still one line to fix.
    """
    unlisted = sorted(present() - REQUIRED)
    assert not unlisted, (
        f"{', '.join(unlisted)} exists but is not in REQUIRED, so nothing would "
        f"notice it being deleted. Add it."
    )


@pytest.mark.parametrize("module", sorted(REQUIRED))
def test_every_required_module_still_holds_tests(module):
    """Emptied is the same as deleted, and looks even less like a loss.

    A merge that keeps a file and drops its contents leaves the filename in place,
    so the check above passes while the cover is gone.
    """
    path = TESTS / f"{module}.py"
    if not path.exists():
        pytest.skip("absence is reported by test_no_test_module_has_gone_missing")

    tree = ast.parse(path.read_text(encoding="utf-8"))
    tests = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    ]
    assert tests, f"{module}.py has no test functions left in it"


# --- and did the run that reported them actually finish? --------------------
#
# The list above catches a test file that stopped EXISTING. This half catches a
# test file that existed, was collected, and never ran — which is the same loss
# and leaves even less behind, because there is no diff to read afterwards.
#
# The mechanism lives in `tests/conftest.py`; the argument for it being a
# session hook rather than a test or a documented command is written there, at
# the top of the block. What is here is everything about it that CAN be a test:
# the arithmetic, against counts no real run has to produce, and one end-to-end
# run proving the hooks are wired in and that without them the loss is invisible.

from conftest import unaccounted_for  # noqa: E402


def test_a_finished_run_accounts_for_every_test_it_collected():
    assert unaccounted_for(10, {"passed": 7, "failed": 2, "skipped": 1}) == 0
    assert unaccounted_for(4, {"xfailed": 3, "xpassed": 1}) == 0
    assert unaccounted_for(2, {"passed": 1, "error": 1}) == 0


def test_tests_that_never_ran_are_counted_and_not_forgiven():
    """The whole point: four collected, one outcome, three unknown."""
    assert unaccounted_for(4, {"failed": 1}) == 3
    assert unaccounted_for(4, {}) == 4


def test_deselection_is_not_truncation():
    """`--deselect` and `-k` are subtracted before the count reaches the hook.

    Measured, not assumed: 113 collected with two deselected reports
    `testscollected == 111`, so `deselected` must NOT be added back or every
    filtered run would cry truncation. Held here because that is exactly the
    kind of off-by-a-category nobody would notice — it would fire on runs people
    already expect to be odd.
    """
    assert unaccounted_for(111, {"passed": 111, "deselected": 2}) == 0


def test_a_teardown_error_is_not_read_as_a_truncated_run():
    """One test, two reports. The sum legitimately exceeds the collected count.

    Reported as a negative rather than clamped, so the hook's `<= 0` guard is
    the thing under test and not an accident of this function.
    """
    assert unaccounted_for(1, {"failed": 1, "error": 1}) == -1


def test_warnings_and_phase_reports_are_not_outcomes():
    """`''` is every setup and teardown report — two per test — and `warnings`
    is not an outcome at all. Counting either would hide a truncation behind a
    number three times too big."""
    assert unaccounted_for(3, {"passed": 3, "": 6, "warnings": 4}) == 0


def test_the_truncation_check_is_registered_in_this_very_session(pytestconfig):
    """The arithmetic being right is worth nothing if nothing calls it.

    Deleting either hook from conftest.py leaves every test above green — they
    only exercise a pure function — and this is the one that goes red.
    """
    import conftest

    for hook_name in ("pytest_sessionfinish", "pytest_terminal_summary"):
        caller = getattr(pytestconfig.pluginmanager.hook, hook_name)
        ours = getattr(conftest, hook_name, None)
        assert ours is not None, f"conftest.py no longer defines {hook_name}"
        assert any(impl.function is ours for impl in caller.get_hookimpls()), (
            f"conftest.py defines {hook_name} but pytest is not calling it, so "
            f"a truncated run would go back to being unreportable"
        )


_A_RUN_THAT_ABORTS = '''
import os, signal, threading, time

def test_a_leaves_a_signal_in_flight():
    def boom():
        time.sleep(0.05)
        os.kill(os.getpid(), signal.SIGINT)
    threading.Thread(target=boom, daemon=True).start()
    time.sleep(0.5)

def test_b_innocent(): pass
def test_c_innocent(): pass
def test_d_innocent(): pass
'''

_LOAD_THE_HOOKS = '''
import importlib.util, os, sys
if os.environ.get("WITH_THE_CHECK"):
    spec = importlib.util.spec_from_file_location("_suite_conftest", {path!r})
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in dir(module):
        if name.startswith("pytest_"):
            globals()[name] = getattr(module, name)
'''


def test_a_truncated_run_says_so_and_without_the_hooks_says_nothing(tmp_path):
    """End to end, on a session that really is cut short, both ways round.

    The same four tests are run twice — once with conftest.py's hooks loaded and
    once without — because half of what is being pinned is the ABSENCE. Without
    them the run reports `1 failed` or `no tests ran` and mentions the three
    tests that never started nowhere at all; that is the state this check
    exists to end, and asserting the banner appears proves nothing unless it is
    also shown that nothing else would have.

    A subprocess, and it has to be: the thing under test is how a pytest SESSION
    ends, and this test is inside one. Nothing here imports `comfy_qa`, reaches
    a network or goes near gcloud — the generated tests use only `os`, `signal`,
    `threading` and `time`.
    """
    import os
    import subprocess
    import sys

    conftest_path = TESTS / "conftest.py"
    (tmp_path / "conftest.py").write_text(
        _LOAD_THE_HOOKS.format(path=str(conftest_path)), encoding="utf-8")
    (tmp_path / "test_aborts.py").write_text(_A_RUN_THAT_ABORTS, encoding="utf-8")

    def run(with_the_check: bool):
        environment = {**os.environ,
                       "PYTHONPATH": str(TESTS.parent),
                       "WITH_THE_CHECK": "1" if with_the_check else ""}
        finished = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
             str(tmp_path / "test_aborts.py")],
            cwd=tmp_path, capture_output=True, text=True, timeout=300,
            env=environment)
        return finished.stdout + finished.stderr

    silent = run(with_the_check=False)
    assert "4 items" in silent or "collected 4" in silent or silent, silent
    assert "never ran" not in silent, (
        f"something already reports unrun tests; this check may be redundant "
        f"— read the output before deleting it:\n{silent}"
    )
    assert "SESSION TRUNCATED" not in silent

    loud = run(with_the_check=True)
    assert "SESSION TRUNCATED" in loud, (
        f"the session was cut short and nothing said so:\n{loud}")
    assert "never ran" in loud, loud
    assert "UNKNOWN, not passed" in loud, loud
    # The arithmetic, on a real session rather than on a dictionary: four
    # collected, and however many of them got an outcome before the abort, the
    # rest are named as missing.
    import re

    stated = re.search(r"(\d+) of (\d+) collected tests never ran", loud)
    assert stated, loud
    missing, collected = int(stated.group(1)), int(stated.group(2))
    assert collected == 4, loud
    assert 1 <= missing <= 4, loud


# --- and nothing else may take the session down -----------------------------


def _signals_at_this_process(tree: ast.AST) -> list[ast.Call]:
    """Every call in `tree` that aims a signal at the pytest process itself.

    `signal.raise_signal` is always self-directed. `os.kill` and `os.killpg` are
    only self-directed when the target is this process, which in practice is
    spelled `os.getpid()` — so the test is "does the target expression mention
    getpid", which is deliberately loose: it over-matches rather than under-,
    and a false positive here costs one line in this file while a false negative
    costs a truncated run nobody can read.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func)
        if name.endswith("raise_signal"):
            found.append(node)
        elif name in ("os.kill", "os.killpg") and node.args:
            if "getpid" in ast.unparse(node.args[0]):
                found.append(node)
    return found


def test_only_the_one_guarded_interrupter_signals_the_test_runner():
    """A test that signals ITSELF is the one shape that can end the whole run.

    Not "can fail" — end. pytest treats a KeyboardInterrupt raised in a test
    body as a session abort, so every test after it is never executed, and the
    summary reports only what ran. Measured on the four-test reproduction: `1
    failed, 11 passed` printed over a session in which twelve tests never
    started.

    THIS IS DERIVED, not a list. The sweep that found the original defect looked
    for `os.kill`, `signal`, `threading.Timer`, bare `time.sleep` used as
    synchronisation and subprocesses whose duration was assumed. Everything else
    it turned up can produce a false FAILURE — a flat `time.sleep(0.2)` before
    reading `ps` in test_tunnel_identity.py, a `sleep 60` stand-in that a
    starved test could in principle outlive — and only self-signalling can
    produce a false SESSION. So that is the one property held here, by walking
    the directory rather than by anybody remembering to keep a list.

    If a second one is ever needed: give it the same two properties
    `Interrupter` has — it waits for the thing it is interrupting to ANNOUNCE
    itself rather than racing a clock, and it can STAND DOWN so a lost race
    leaves no signal in flight — and then say so here. Adding a name to the
    allowance is a decision; a check that quietly permitted the second one would
    be no check at all.
    """
    ALLOWED = {("test_interrupt.py", "Interrupter")}

    offenders = []
    for path in sorted(TESTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owners = [
            (node.lineno, node.end_lineno, node.name)
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
        ]
        for call in _signals_at_this_process(tree):
            owner = next((name for start, end, name in owners
                          if start <= call.lineno <= end), None)
            if (path.name, owner) not in ALLOWED:
                offenders.append(f"{path.name}:{call.lineno} in {owner or 'a test'}")

    assert not offenders, (
        f"{', '.join(offenders)} sends a signal to the pytest process. Losing "
        f"that race does not fail a test, it ENDS THE SESSION, and the summary "
        f"line for a truncated run is typographically identical to a run with "
        f"one failure. Use `Interrupter` from tests/test_interrupt.py, which "
        f"waits for the child to announce itself and stands down rather than "
        f"leaving a signal in flight — or, if this one is genuinely different, "
        f"add it to ALLOWED here and write down why."
    )
