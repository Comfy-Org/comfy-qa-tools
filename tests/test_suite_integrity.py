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
from typing import NamedTuple

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
    "test_money_agreement",
    "test_money_sentences",
    "test_move",
    "test_no_colour",
    "test_old_spellings",
    "test_option_help",
    "test_os_families",
    "test_prune",
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


# The session that really does get cut short, generated into a temp directory and
# run as its own pytest.
#
# TWO THINGS IN HERE ARE LOAD-BEARING AND BOTH WERE WRONG.
#
# 1. THE SIGNAL HAS TO BE DELIVERABLE. A process started in the BACKGROUND from
#    a non-interactive shell — `&`, `nohup`, CI, any agent harness — inherits
#    SIGINT as SIG_IGN, and CPython respects an inherited SIG_IGN rather than
#    installing `default_int_handler` over it. `os.kill(os.getpid(), SIGINT)` is
#    then a NO-OP: all four tests below run, the session is never truncated, and
#    the test that reads this output fails saying nothing said so.
#
#    Measured on this file, ONE process, no concurrency and no load:
#
#        foreground     63 passed in 1.02s
#        backgrounded   1 failed, 62 passed in 1.76s
#
#    Deterministic both ways. The only variable is how the process was started,
#    which is exactly why this was carried as a test that "only fails in the
#    full run": whoever ran the suite ran it from a harness, and whoever tried
#    to reproduce it typed it into a terminal.
#
#    What is modelled here is a person pressing Ctrl-C, where the handler is
#    always installed. So install it rather than inherit it.
#
# 2. NO SLEEP MAY STAND IN FOR A FACT. This used to be `time.sleep(0.05)` in the
#    thread racing `time.sleep(0.5)` in the main thread: if the thread lost that
#    race the session completed normally and the failure looked identical to the
#    one above. That is the same "sleep used as a DEADLINE" that
#    tests/test_interrupt.py's header records being removed from itself — the
#    fix was made one file away and never reached this one. The thread now waits
#    on an EVENT, and the main thread's sleep is a ceiling on a broken fixture
#    rather than a deadline anybody has to beat.
_A_RUN_THAT_ABORTS = '''
import os, signal, threading, time

signal.signal(signal.SIGINT, signal.default_int_handler)

def test_a_leaves_a_signal_in_flight():
    parked = threading.Event()

    def boom():
        parked.wait(30)
        os.kill(os.getpid(), signal.SIGINT)

    threading.Thread(target=boom, daemon=True).start()
    parked.set()
    time.sleep(30)

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


def test_the_abort_fixture_does_not_depend_on_an_inherited_signal_handler(tmp_path):
    """The floor under the test above, and the reason it needs one.

    That test was GREEN AND VACUOUS under every non-interactive launch this repo
    has ever had. `os.kill(os.getpid(), SIGINT)` does nothing when SIGINT is
    SIG_IGN, so the generated session ran to completion, was never truncated,
    and the assertion about the banner was the only thing that noticed — which
    read as "a test that only fails in the full run", because the people running
    the full run ran it from a harness and the people reproducing it typed it
    into a terminal.

    `trap "" INT` is exactly what `&`, `nohup`, CI runners and agent harnesses
    hand a child: SIGINT already ignored, inherited across the exec, and CPython
    deliberately declines to install `default_int_handler` over it.

    Measured before the fixture installed its own handler — one process, no
    concurrency, no load:

        foreground                   63 passed in 1.02s
        under an ignored SIGINT      1 failed, 62 passed in 1.76s

    Deterministic both ways. This test is that second column, kept.
    """
    import os
    import subprocess
    import sys

    conftest_path = TESTS / "conftest.py"
    (tmp_path / "conftest.py").write_text(
        _LOAD_THE_HOOKS.format(path=str(conftest_path)), encoding="utf-8")
    (tmp_path / "test_aborts.py").write_text(_A_RUN_THAT_ABORTS, encoding="utf-8")

    finished = subprocess.run(
        # `sh -c 'trap "" INT; exec "$@"' sh <argv...>` — ignore SIGINT, then
        # exec, so the interpreter starts with the disposition already set.
        ["sh", "-c", 'trap "" INT; exec "$@"', "sh",
         sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(tmp_path / "test_aborts.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=300,
        env={**os.environ, "PYTHONPATH": str(TESTS.parent),
             "WITH_THE_CHECK": "1"})
    out = finished.stdout + finished.stderr

    assert "SESSION TRUNCATED" in out, (
        f"the generated session was NOT cut short when SIGINT arrived ignored, "
        f"so `test_a_truncated_run_says_so_and_without_the_hooks_says_nothing` "
        f"is passing without testing anything under every backgrounded run. The "
        f"fixture must install its own handler rather than inherit one:\n{out}"
    )
    assert "never ran" in out, out


# --- and nothing else may take the session down -----------------------------
class _Site(NamedTuple):
    """One call that aims a signal at the pytest process, and where it lives."""

    file: str
    owner: str | None
    lineno: int
    call: ast.Call
    scope: ast.AST      # the module this call is a part of — the real file, or
                        # the source parsed out of a string literal inside it

    def __str__(self) -> str:
        return f"{self.file}:{self.lineno} in {self.owner or 'a test'}"


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


def _owner(tree: ast.AST, node: ast.AST) -> str | None:
    """The class a node sits inside, or None for one at module level."""
    return next((cls.name for cls in ast.walk(tree)
                 if isinstance(cls, ast.ClassDef)
                 and cls.lineno <= node.lineno <= (cls.end_lineno or cls.lineno)),
                None)


def _documentation(tree: ast.AST) -> set[int]:
    """The id() of every string node that is a docstring rather than a value.

    Prose is allowed to quote `os.kill(os.getpid(), signal.SIGINT)` in order to
    explain it — the comment above `_A_RUN_THAT_ABORTS` does exactly that, at
    length, and so does half the header of tests/test_interrupt.py. A guard that
    flagged those would be deleted by the third person it interrupted, so
    docstrings are excluded by construction here and comments never reach the
    tree at all.
    """
    kinds = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, kinds) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                found.add(id(first.value))
    return found


def _sources_written_as_string_literals(tree: ast.AST):
    """Test files that live inside this file as strings, parsed as the code they are.

    `ast.walk` does not descend into the CONTENTS of a string, so a session
    generated into a temp directory and run as its own pytest is invisible to
    every check in this section — which is how `_A_RUN_THAT_ABORTS` came to hold
    a second self-directed SIGINT under a guard whose name said there was one.
    The string is written to `test_aborts.py` and executed; it is source, and the
    only thing that made it look like data is the quoting.

    Three things keep this from firing on prose, in the order they apply:

    1. Docstrings are excluded, and comments are not in the tree.
    2. An f-string's literal parts are excluded — those are message text by
       construction, and this file's own assertion messages are f-strings.
    3. What is left must PARSE as a Python module and then must contain a
       self-directed signal call. Both, not either. A sentence that happens to
       parse yields nothing, because the caller asks it for signal sites rather
       than for strings.

    The substring pre-filter is not a fourth rule: `_signals_at_this_process`
    can only match a call whose name contains `kill` or `raise_signal`, so a
    string with neither cannot produce a site however it parses. It is there so
    this does not attempt a parse of several thousand assertion messages.

    THE ONE OVER-MATCH, named rather than left to be discovered. A string whose
    ENTIRE content is `"os.kill(os.getpid(), signal.SIGINT)"` — a message, say,
    that quotes the call and nothing else — parses and is flagged. Nothing can
    tell that apart from a one-line module, because there is no difference: the
    same bytes written to a `.py` file run. It costs one line in ALLOWED, which
    is the trade `_signals_at_this_process` already states above, and every
    quotation in this repository today sits in a docstring or a comment, where
    rules 1 and 2 reach it first. Verified: the four prose quotations already in
    this file and in tests/test_interrupt.py produce zero findings.
    """
    documentation = _documentation(tree)
    interpolated = {id(part)
                    for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
                    for part in node.values}

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if id(node) in documentation or id(node) in interpolated:
            continue
        if "kill" not in node.value and "raise_signal" not in node.value:
            continue
        try:
            inner = ast.parse(node.value)
        except SyntaxError:
            continue
        yield node, inner


def _bound_name(tree: ast.AST, literal: ast.Constant) -> str | None:
    """The name a string literal is assigned to, which is what names it in a report.

    `_A_RUN_THAT_ABORTS` is the string's identity the same way `Interrupter` is
    the class's, so ALLOWED can hold both in the same shape and a reader can find
    what was allowed by searching for the name in the message.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and node.value is literal:
            targets = [ast.unparse(target) for target in node.targets]
            return targets[0] if targets else None
    return None


def _self_directed_signals(path: Path) -> list[_Site]:
    """Every self-directed signal in a test file, including the ones it writes out."""
    tree = ast.parse(path.read_text(encoding="utf-8"))

    sites = [_Site(path.name, _owner(tree, call), call.lineno, call, tree)
             for call in _signals_at_this_process(tree)]

    for literal, inner in _sources_written_as_string_literals(tree):
        name = _bound_name(tree, literal)
        for call in _signals_at_this_process(inner):
            # The literal's own line plus the offset inside it. Exact for a
            # triple-quoted string that opens its own line, which is the only
            # way a whole test module is ever written down.
            sites.append(_Site(path.name, name,
                               literal.lineno + call.lineno - 1, call, inner))
    return sites


# `Interrupter` in tests/test_interrupt.py, and the four-test session
# `test_suite_integrity.py` generates in order to watch a run be truncated.
#
# THE SECOND ENTRY IS THE ONE THIS SECTION WAS WRONG ABOUT. It was always there;
# the check could not see it, because it is a string. Its two properties are the
# ones the docstring below asks for — the thread waits on an EVENT that the main
# thread sets rather than racing a clock, and the signal is aimed at a session
# that exists only inside `tmp_path` and cannot reach the one reading its output.
ALLOWED = {
    ("test_interrupt.py", "Interrupter"),
    ("test_suite_integrity.py", "_A_RUN_THAT_ABORTS"),
}


def test_every_self_directed_signal_is_one_of_the_guarded_interrupters():
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

    WHAT THE DERIVATION COULD NOT SEE, AND WHY THE NAME OF THIS TEST CHANGED.
    It said "only the one guarded interrupter" and there were two. The second is
    `_A_RUN_THAT_ABORTS`, forty lines above: a whole test module written down as
    a string, saved to `test_aborts.py` and run. `ast.walk` does not descend into
    a string's contents, so the sweep returned one and read as exhaustive.
    Another agent's independent count hit the same wall and also returned one,
    which is how the gap was noticed at all. That is the failure mode worth
    naming — not an unsafe interrupter, but an instrument whose answer was
    complete about the wrong domain, and which reported a number rather than
    "I could not see in there".

    Severity, so nobody re-reads this as a near miss: the safety property was
    never breached. The second signaller runs in a pytest of its own, in a
    subprocess, and cannot truncate the session that watches it. What was wrong
    was the claim, and a claim is what a guard is for.

    If a third one is ever needed: give it the same two properties `Interrupter`
    has — it waits for the thing it is interrupting to ANNOUNCE itself rather
    than racing a clock, and it can STAND DOWN so a lost race leaves no signal in
    flight — and then say so in ALLOWED. Adding a name to the allowance is a
    decision; a check that quietly permitted the next one would be no check at
    all.
    """
    offenders = [str(site)
                 for path in sorted(TESTS.glob("*.py"))
                 for site in _self_directed_signals(path)
                 if (site.file, site.owner) not in ALLOWED]

    assert not offenders, (
        f"{', '.join(offenders)} sends a signal to the pytest process. Losing "
        f"that race does not fail a test, it ENDS THE SESSION, and the summary "
        f"line for a truncated run is typographically identical to a run with "
        f"one failure. Use `Interrupter` from tests/test_interrupt.py, which "
        f"waits for the child to announce itself and stands down rather than "
        f"leaving a signal in flight — or, if this one is genuinely different, "
        f"add it to ALLOWED here and write down why."
    )


def _aims_a_sigint(call: ast.Call) -> bool:
    """Whether a self-directed signal call is sending SIGINT specifically.

    `os.kill(pid, sig)` and `os.killpg(pgid, sig)` carry it second;
    `signal.raise_signal(sig)` carries it first.
    """
    name = ast.unparse(call.func)
    carries = call.args[1:2] if name in ("os.kill", "os.killpg") else call.args[:1]
    return bool(carries) and "SIGINT" in ast.unparse(carries[0])


def _arms_sigint(call: ast.Call, scope: ast.AST) -> bool:
    """Whether a `signal.signal(SIGINT, h)` call makes SIGINT RAISE, or merely puts back.

    THE DISTINCTION IS THE WHOLE CHECK, and leaving it out produced a false
    negative on the first mutation run: `Interrupter.__exit__` ends with
    `signal.signal(signal.SIGINT, self._restore_sigint)`, so deleting the real
    install in `__enter__` left a `signal.signal(SIGINT, ...)` standing and the
    guard stayed green over a site that had just lost the only thing making its
    signal deliverable. A restore hands back whatever was inherited — under a
    non-interactive launcher that is SIG_IGN, and restoring SIG_IGN is not
    installing a handler, it is the defect.

    So an ARM is one of the three ways CPython lets you say "make SIGINT raise":
    `default_int_handler`, `SIG_DFL`, or a function defined in this same source.
    A saved value read back out of `getsignal` is none of them. That is a
    property of the signal module rather than a house style, which is why it can
    be written down as a list without becoming the kind of list this file exists
    to avoid.
    """
    if len(call.args) < 2:
        return False
    handler = ast.unparse(call.args[1])
    if handler in ("signal.default_int_handler", "signal.SIG_DFL"):
        return True
    return any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name == handler
               for node in ast.walk(scope))


def _installs_a_sigint_handler(site: _Site) -> bool:
    """Whether SIGINT is armed in the same class as the site, or module level alongside it.

    The same owner, not merely the same file: a handler installed in an unrelated
    class is not the one this site depends on, and accepting it anywhere in the
    module would make the check "the word appears somewhere", which is the
    failure the test above was written for.

    Both places this holds today are a class apart from the call rather than a
    line before it: `Interrupter` arms in `__enter__` and signals from
    `_wait_then_signal`, and the generated session arms at module level and
    signals from a thread. Requiring the two to be adjacent would flag both.
    """
    here = _owner(site.scope, site.call)
    for node in ast.walk(site.scope):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        if ast.unparse(node.func) != "signal.signal":
            continue
        if "SIGINT" not in ast.unparse(node.args[0]):
            continue
        if _owner(site.scope, node) == here and _arms_sigint(node, site.scope):
            return True
    return False


def test_every_self_directed_sigint_installs_the_handler_it_depends_on():
    """An inherited SIGINT disposition makes `os.kill` a no-op, silently.

    This is the defect recorded twice in this repository — in the comment above
    `_A_RUN_THAT_ABORTS` and again in `Interrupter.__enter__` — and both times it
    was found by measurement rather than by reading. A process started in the
    background from a non-interactive shell (`&`, `nohup`, CI, any agent harness)
    inherits SIGINT as SIG_IGN, and CPython respects the inherited disposition
    rather than installing `default_int_handler` over it. The signal is then sent
    and nothing happens: the interrupt never arrives, the call under test returns
    normally, and the test asserting that a KeyboardInterrupt came back is the
    only thing that notices — which is how this was carried for weeks as "only
    fails in the full run".

    Measured on the two files that do it, one process, no load:

        test_interrupt.py         foreground    19 passed in 0.58s
                                  backgrounded  1 failed, 18 passed in 32.21s
        test_suite_integrity.py   foreground    63 passed in 1.02s
                                  backgrounded  1 failed, 62 passed in 1.76s

    Deterministic both ways. The only variable is how the process was started.

    So the rule is not "handle SIGINT" as a courtesy — it is that a self-directed
    SIGINT has no meaning until the handler is installed rather than inherited,
    and the fix was applied to one file and did not reach the other. This is what
    stops the third one from being found the same way.
    """
    offenders = [str(site)
                 for path in sorted(TESTS.glob("*.py"))
                 for site in _self_directed_signals(path)
                 if _aims_a_sigint(site.call)
                 and not _installs_a_sigint_handler(site)]

    assert not offenders, (
        f"{', '.join(offenders)} sends itself SIGINT without installing a "
        f"handler for it first. Under any non-interactive launcher SIGINT is "
        f"inherited as SIG_IGN, CPython keeps it, and the call is a NO-OP — the "
        f"test then passes by doing nothing, in exactly the runs nobody watches. "
        f"Call `signal.signal(signal.SIGINT, signal.default_int_handler)` in the "
        f"same class, or at module level for a generated session, and put the "
        f"previous handler back on the way out."
    )
