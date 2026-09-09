"""The guard on the path detector, and it exists for one case in particular.

`tests/pathnames.py` decides who is in scope for the two tests that hold
documentation to naming the real host list. Before it, both decided that by
looking for `DEFAULT_CONFIG_PATH.name` — the bare filename `hosts.toml`, which is
a substring of the right answer — so a docstring naming a *different* file
skipped instead of failing. Sixteen cases across the two files were resolved by
that skip.

So the case this file is built around is not "does the detector find the real
path". It is: **a wrong path that does not contain `hosts.toml` must come back
from `paths_named`**, because that is precisely the text that used to leave the
suite quietly. `docs/tests-that-cannot-fail.md` section 4 is the reason it is
written down as a test rather than checked once by hand: an instrument nobody
points at the absent case has only ever been seen agreeing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from comfy_qa.config import DEFAULT_CONFIG_PATH
from pathnames import paths_named

# The wrong answers, spelled out. Every one of these is a plausible thing for a
# docstring to say after a rename, and not one of them contains `hosts.toml`.
WRONG = [
    "~/.config/comfy-qa-tools/config.toml",
    "~/.comfyqat/machines.toml",
    "/etc/comfy-qa-tools/hosts.yaml",
    ".comfyqat/machines.toml",
    "/opt/comfy/qa/machines.json",
]


@pytest.mark.parametrize("path", WRONG)
def test_a_wrong_path_is_seen_even_though_it_is_not_the_host_list(path):
    """The regression, stated directly.

    The old inclusion test was `DEFAULT_CONFIG_PATH.name in text`. Each of these
    fails that test, which is how each of them used to be excused.
    """
    text = f"Reads the host list from {path} unless --config says otherwise."
    assert DEFAULT_CONFIG_PATH.name not in text, (
        "this case is only meaningful while the text does NOT contain the real "
        "filename — that is the exact text the old inclusion criterion skipped"
    )
    assert paths_named(text) == [path]


def test_the_real_path_is_seen_too():
    """Both halves of an instrument, per section 4: it has to say yes as well."""
    real = "~/" + DEFAULT_CONFIG_PATH.relative_to(Path.home()).as_posix()
    assert paths_named(f"Default: {real}.") == [real]


@pytest.mark.parametrize(
    "prose",
    [
        "Host list. Declared at the root — see `comfy-qat --help`.",
        "Host list to read and update.",
        "Show what would be added without writing anything.",
        "Pass --os/--gpu to narrow the selection.",
        "The read/update distinction is documented per command.",
        "",
    ],
)
def test_prose_that_points_at_no_location_is_not_a_path_claim(prose):
    """The exclusion has to be narrow, or it swallows the mismatch it is for.

    `read/update` and `--os/--gpu` are the reason the pattern wants a root: a
    detector that called those paths would drag every command into a check about
    where files live, and the failures would train people to skip again.
    """
    assert paths_named(prose) == []


def test_being_included_does_not_depend_on_being_right():
    """The disjointness assertion section 7 asks for, on the relationship.

    Inclusion is by shape and the pass criterion is by identity, so a text can be
    in scope and wrong at the same time. While this holds, no token that puts a
    text on the list can also take it off it.
    """
    wrong = "The host list lives at ~/.comfyqat/machines.toml."
    real = "~/" + DEFAULT_CONFIG_PATH.relative_to(Path.home()).as_posix()
    assert paths_named(wrong), "in scope"
    assert real not in wrong, "and failing"


def test_several_paths_in_one_sentence_all_come_back():
    """A right path standing next to a wrong one must not hide it."""
    text = "Reads ~/.config/comfy-qa-tools/hosts.toml, writes /var/tmp/out.toml."
    assert paths_named(text) == [
        "~/.config/comfy-qa-tools/hosts.toml",
        "/var/tmp/out.toml",
    ]
