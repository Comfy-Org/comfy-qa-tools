"""One question, one answer — and something that fails when a sixth copy appears.

Five places in this package answered "what operating system is this?" using four
different techniques, and nothing asserted they agreed. Two of them did not:
`stamp` called a Mac `darwin` while `lifecycle` called it `macos`, and
`lifecycle._family` made `Rocky Linux 9` a family called `rocky` — its own kind
of box, matching nothing — while `config` and `stamp` both called it linux.

The merge is in `comfy_qa/osfamily.py`. What is here is the part that keeps it
merged, because the defect was never the four techniques: it was that nothing
noticed a fifth being added, and nothing would have noticed a sixth.

**Every list in this file is derived from the source, not typed out.** A guard
against duplication that works by keeping its own copy of the thing it guards is
the defect wearing a hat. The AST walk below finds the readers; the assertions
compare what it found against what is expected AND what is expected against what
it found, so a classifier that appears fails, and one that is renamed or deleted
fails too.

Each derived set is checked for being non-empty first. A walk that silently
matches nothing passes every `set() == set()` comparison ever written, and that
is the way this kind of guard usually dies.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from comfy_qa import discover, osfamily
from comfy_qa.config import _BY_SPECIFICITY, OS_KEYWORDS, Host, resolve

PACKAGE = Path(__file__).resolve().parent.parent / "comfy_qa"


# --- who is allowed to ask ---------------------------------------------------
#
# Everything that classifies a machine's operating system has to read that
# machine's `os` field to do it, so this is the whole population — there is no
# way to write a sixth classifier that stays out of it.
#
# The reason each one is here matters as much as the name. THESE ARE NOT ALL THE
# SAME QUESTION, and the merge was only correct because they are not:
#
#   - a CLASSIFIER decides something from the string. There is now one, and the
#     other two call it.
#   - a RENDERER prints the string to a person. It has no vocabulary of its own
#     and nothing to agree with.
#   - a WRITER puts the string into the host list. `discover` writes it from
#     Google's licence names and falls back to a raw tail like `sles-15`, which
#     is why `family` must accept what it does not recognise.
EXPECTED_READERS = {
    ("osfamily", "is_windows"): "THE classifier. Everything else defers to it.",
    ("stamp", "mismatch"): "classifier — via osfamily.family, both sides of it",
    ("lifecycle", "rank"): "classifier — via osfamily.family, to order alternatives",
    ("config", "_matches_os"): "SELECTOR, not a family. Deliberately not merged.",
    ("config", "describe"): "renderer — one-line description of a host",
    ("create", "steps"): "renderer — what `create` says it is about to build",
    ("create", "host_entry"): "writer — records the image's own os in the host list",
    ("discover", "to_toml"): "writer — the [hosts.x] block, os from the licence",
    ("setup", "add_discovered_hosts"): "renderer — what was just added",
    ("host", "list_cmd"): "renderer — the os column of `host list`",
    ("host", "discover_cmd"): "renderer — what discovery found",
    ("host", "register"): "writer — hands a host to the stamp",
    ("stamp", "line"): "renderer — the os in a stamp's evidence line",
}


def _readers() -> set[tuple[str, str]]:
    """Every (module, function) in the package that reads a host's `os` field.

    Both spellings, because the two classifiers used different ones: `host.os`
    is an attribute, and `stamp.mismatch` uses `getattr(host, "os", None)`
    because it is duck-typed on the host rather than importing it.
    """
    found: set[tuple[str, str]] = set()
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {
            child: node
            for node in ast.walk(tree)
            for child in ast.iter_child_nodes(node)
        }

        def enclosing(node: ast.AST) -> str:
            while node in parents:
                node = parents[node]
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    return node.name
            return "<module>"

        for node in ast.walk(tree):
            reads_os = (
                isinstance(node, ast.Attribute) and node.attr == "os"
            ) or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value == "os"
            )
            if reads_os:
                found.add((path.stem, enclosing(node)))
    return found


FOUND = _readers()


def test_the_walk_that_finds_the_classifiers_finds_anything_at_all():
    """The guard on the guard.

    An AST walk that stops matching — a rename in `ast`, a package layout change,
    a glob that resolves to nothing — returns an empty set, and an empty set
    passes every comparison below. So the emptiness is the failure, checked on
    its own, against two entries that cannot go away without the merge itself
    being undone.
    """
    assert FOUND, (
        f"no reader of a host's `os` field was found anywhere in {PACKAGE}. "
        f"That is not possible while this tool has host lists — the walk has "
        f"stopped working, and every assertion in this file is now vacuous."
    )
    assert ("osfamily", "is_windows") in FOUND, "the shared classifier itself"
    assert ("discover", "to_toml") in FOUND, "the writer that puts `os` in the file"


def test_no_new_classifier_has_appeared():
    """A sixth copy of the question, and the name of whoever asked it."""
    extra = sorted(FOUND - set(EXPECTED_READERS))
    assert not extra, (
        f"{', '.join(f'{m}.{f}' for m, f in extra)} reads a host's `os` field and "
        f"is not accounted for in EXPECTED_READERS. If it CLASSIFIES the machine, "
        f"it must call `osfamily.family` or `osfamily.is_windows` rather than "
        f"matching words itself — five places used to do that and two of them "
        f"disagreed about the same box. If it only prints or writes the string, "
        f"add it here with which of those it is."
    )


def test_no_reader_has_quietly_gone_missing():
    """The other direction, so a rename fails here rather than going unnoticed.

    A list that stops describing the code stops protecting it, and the way that
    happens is a function being renamed while the list keeps the old name and
    keeps passing the check above.
    """
    gone = sorted(set(EXPECTED_READERS) - FOUND)
    assert not gone, (
        f"{', '.join(f'{m}.{f}' for m, f in gone)} is expected to read a host's "
        f"`os` field and no longer does. Renamed? Rename it here. Deleted on "
        f"purpose? Deleting the line is how you record having decided that."
    )


def _definitions_of(name: str) -> list[str]:
    return [
        path.stem
        for path in sorted(PACKAGE.glob("*.py"))
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]


def test_is_windows_is_defined_exactly_once():
    """It was defined twice, byte for byte, in `provision` and in `lifecycle`.

    Those two modules decide different halves of one session about one box —
    which path root to install into, and whether to tell the tester to open RDP
    or SSH. Two copies of one predicate is how they come to disagree.
    """
    where = _definitions_of("is_windows")
    assert where, "is_windows is not defined anywhere — the walk has stopped working"
    assert where == ["osfamily"], (
        f"is_windows is defined in {', '.join(where)}. There is one definition, in "
        f"osfamily, and everything else imports it."
    )


# --- the two tables, and what is deliberately not shared ---------------------


def test_the_selector_table_reads_the_family_words_rather_than_copying_them():
    """`config` and `stamp` each kept their own copy, and the copies drifted.

    `config` knew `rhel` and `suse` and `stamp` did not, so a box declared
    `rhel-9` was linux to `switch` and unclassifiable to the stamp. Sharing the
    tuple is what stops that happening again to the next distribution somebody
    adds.
    """
    assert osfamily.FAMILY_WORDS, "the family table is empty"
    for selector, family_name in (
        ("windows", osfamily.WINDOWS),
        ("linux", osfamily.LINUX),
        ("macos", osfamily.DARWIN),
        ("local", osfamily.DARWIN),
    ):
        assert OS_KEYWORDS[selector] is osfamily.FAMILY_WORDS[family_name], (
            f"OS_KEYWORDS[{selector!r}] is its own tuple again rather than the "
            f"{family_name} row of osfamily.FAMILY_WORDS. That is the drift."
        )


def test_ubuntu_and_debian_stay_the_selector_words_they_are():
    """Deliberately NOT merged, and the reason is the whole judgement.

    A family puts every host in exactly one bucket. A selector has to let one
    host answer to several words at several grains: an Ubuntu box is found by
    `ubuntu` AND by `linux`, and `_selector_for` offers the narrower one when two
    boxes clash. Folding these into the family table would make `ubuntu` a
    family, and then an Ubuntu box and a Debian box would stop being the same
    kind of machine — which is the `Rocky Linux 9` bug, reintroduced from the
    other end.
    """
    assert OS_KEYWORDS["ubuntu"] == ("ubuntu",)
    assert OS_KEYWORDS["debian"] == ("debian",)
    assert osfamily.family("Ubuntu 22.04") == osfamily.family("Debian 12")


def test_the_order_of_the_selector_keys_is_user_visible():
    """`config` prints `', '.join(OS_KEYWORDS)` in the unknown-host error.

    So insertion order is not an implementation detail here; it is a sentence
    somebody reads. Rebuilding the table from another module's rows is exactly
    the edit that reorders it by accident.
    """
    assert list(OS_KEYWORDS) == [
        "windows", "linux", "ubuntu", "debian", "macos", "local"]


def test_the_specificity_order_still_covers_every_selector():
    """The third copy of this vocabulary, connected rather than merged.

    `_BY_SPECIFICITY` orders the same keys by how narrow they are, so that
    `_selector_for` suggests `ubuntu` rather than `linux` when two boxes clash.
    The ORDER is its content, so it cannot be derived from a table that is
    grouped by family instead — merging it would destroy the only thing it says.
    What it can be is tied to the same key set, so a selector added to one and
    not the other fails here instead of silently becoming unsuggestable.
    """
    assert _BY_SPECIFICITY, "the specificity order is empty"
    assert set(_BY_SPECIFICITY) == set(OS_KEYWORDS), (
        f"_BY_SPECIFICITY and OS_KEYWORDS name different selectors: "
        f"{sorted(set(OS_KEYWORDS) ^ set(_BY_SPECIFICITY))}. A selector missing "
        f"from _BY_SPECIFICITY still resolves, but `_selector_for` will never "
        f"suggest it, so a clash gets the wrong disambiguator."
    )


# --- the constraint that must survive ----------------------------------------


def test_everything_discover_can_write_is_classified():
    """Derived from `discover`'s own table, so a licence added there is covered."""
    assert discover._OS_NAMES, "discover knows no licence names"
    unclassified = sorted(
        name for name in discover._OS_NAMES.values() if osfamily.family(name) is None)
    assert not unclassified, (
        f"{', '.join(unclassified)} is written into host lists by `discover` and "
        f"names no family, so `stamp` will never compare it and `alternatives` "
        f"will never group it."
    )


@pytest.mark.parametrize("value", ["sles-15", "unknown", "", None, "nt", "posix"])
def test_an_unfamiliar_os_is_answered_with_none_rather_than_a_guess(value):
    """`discover` falls back to a raw licence tail, or to `unknown`.

    A table strict enough to reject those would let `discover` write a host list
    that `load` then refuses — and a host list the tool will not read is a
    machine nobody can stop, which is worse than any classification bug. So
    unfamiliar is `None`, every caller has an answer for `None`, and nothing here
    raises.
    """
    assert osfamily.family(value) is None


def test_a_host_that_cannot_be_classified_is_still_not_windows():
    """`is_windows` has to return one of two answers and cannot say "I do not
    know" — it picks between two command sets. Unfamiliar falls to the POSIX
    side, which is what an unfamiliar cloud image is, and is what both of the
    copies it replaced already did."""
    unknown = Host(name="x", kind="gce", port=8190, os="sles-15", gpu="none",
                   gce_instance="x", gce_zone="z", gce_project="p")
    assert not osfamily.is_windows(unknown)


def test_local_and_macos_resolve_on_a_host_that_declares_no_os():
    """The starter hosts.toml is `kind = "local"` and `port = 8188`, nothing else.

    So `go local` and `go macos` resolve on `kind` in `_matches_os`, never on the
    word table — and that branch is a second mechanism, not a shortcut. Rebuild
    the `macos` row from a shared table, notice it now contains `darwin`, and
    delete the `kind` branch as redundant, and both selectors stop working on the
    one config every new user has.
    """
    bare = Host(name="local", kind="local", port=8188)
    assert bare.os is None
    for typed in ("local", "macos"):
        assert resolve([bare], typed).host.name == "local"
