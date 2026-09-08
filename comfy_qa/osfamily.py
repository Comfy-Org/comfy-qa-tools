"""Which operating system a string names. One table, one answer, everywhere.

Five places in this package asked "what operating system is this?" and answered
it four different ways, and two of them disagreed about the same machine:

  - `stamp` called a Mac `darwin`, `lifecycle` called it `macos`, and `config`
    accepted `macos` and `local` for it.
  - `lifecycle._family` took the FIRST WORD of `host.os`, so `Rocky Linux 9`
    became a family called `rocky` — its own kind of box, matching nothing —
    while `config` and `stamp` both called it linux.

`provision.py` has the closer precedent than `hostfile.py` does, and it is worth
being precise about why. `hostfile`'s three copies of one regex cost six defects,
but they were three copies of a PATTERN: the fix was to stop writing it out
again. This is the other shape — `provision`'s three copies of an interpreter
search DISAGREED ABOUT THE SAME BOX, so `verify` condemned a machine that
`launch` could start. That is what was happening here: two functions, given one
host, answered different things about what it runs, and the answers are acted on.

**Everything unrecognised comes back `None`, and that is a promise, not a gap.**
`host.os` is written by `discover.operating_system()` from Google's licence
names, and it falls back to a raw licence tail like `sles-15`, or to `unknown`.
A table strict enough to reject those would let `discover` write a host list that
`load` then refuses — and a host list the tool will not read is a machine nobody
can stop, which is worse than any classification bug. So this answers "I do not
know" freely, and every caller has to have an answer for that.

Matched as SUBSTRINGS, so every word here has to be one that cannot turn up
inside another operating system's name: `nt` alone reads `ubuntu` as Windows.

Do not add `"nt"` for the same reason. And `posix` cannot be added by any
matching technique: `os.name` is `posix` on macOS and on Linux alike, so the
string genuinely names two families and picking either is a coin toss — on a
tunnel that really has landed on this Mac, mapping it to `linux` would clear the
exact wrong-machine case `stamp.mismatch` refuses for. A word-boundary match
would make `posix` look safe to close, because it collides with no other OS name;
it would still be wrong.

The words below are the UNION of the two tables that existed before, and nothing
more — every one of them was already trusted by `stamp` or by `config`. The
union is not neutral in one direction and it is worth saying which: `rhel` and
`suse` came from `config` and are new to the stamp comparison, so a box declared
`rhel-9` is now classified where it was skipped. That can only turn a skipped
comparison into an agreeing one, or refuse a genuinely wrong machine — a
rhel-declared box cannot newly contradict itself, because it could only be
flagged by answering `windows` or `darwin`, which is exactly the failure the
refusal exists for.
"""

from __future__ import annotations

WINDOWS = "windows"
LINUX = "linux"

# The family is called `darwin`, not `macos`, because a family names what a
# MACHINE REPORTS ABOUT ITSELF — ComfyUI sends `sys.platform`, which is `darwin`.
# `macos` is what a PERSON TYPES, and that stays `config`'s word: those are two
# vocabularies doing two jobs, and the fix for three spellings is not to force
# them into one word but to make one set of evidence back both. `config` maps its
# `macos` and `local` selectors onto this family.
DARWIN = "darwin"

FAMILY_WORDS: dict[str, tuple[str, ...]] = {
    WINDOWS: ("windows", "win32", "winnt", "microsoft"),
    LINUX: ("linux", "ubuntu", "debian", "centos", "rocky", "fedora", "rhel", "suse"),
    DARWIN: ("darwin", "macos", "mac os", "osx"),
}


def family(text: str | None) -> str | None:
    """Which OS family a string names, or `None` when it names none of them.

    `None` is the important return. An unrecognised string is not quietly read
    as some other family, so a comparison between two of these can be skipped
    rather than decided wrongly — see the module docstring for why that has to
    stay true of anything `discover` can write.
    """
    lowered = (text or "").lower()
    for name, words in FAMILY_WORDS.items():
        if any(word in lowered for word in words):
            return name
    return None


def is_windows(host) -> bool:
    """Is this host a Windows box? Anything unrecognised is not.

    Callers use this to pick between two command sets — a path root, a log
    location, an interpreter search — so unlike `family` it has to return one of
    two answers and cannot say "I do not know". Falling to the non-Windows side
    is the right way round and is what both copies of this already did: the
    non-Windows branch is POSIX, which is what an unfamiliar cloud image is.
    """
    return family(getattr(host, "os", None)) == WINDOWS
