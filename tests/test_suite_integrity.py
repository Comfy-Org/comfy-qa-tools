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
    "test_discover",
    "test_docs",
    "test_env",
    "test_gcloud_auth",
    "test_host_costs",
    "test_lifecycle",
    "test_move",
    "test_provision",
    "test_quota",
    "test_quota_regions",
    "test_readme",
    "test_relocate",
    "test_resolve",
    "test_setup",
    "test_setup_flow",
    "test_stamp",
    "test_stamp_hostile",
    "test_stamp_mismatch",
    "test_stamp_payloads",
    "test_suite_integrity",
    "test_switch",
    "test_tunnel",
    "test_tunnel_identity",
    "test_version",
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
