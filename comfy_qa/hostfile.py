"""Changing the host list in place, without losing it.

Every other write to `hosts.toml` in this tool appends. Appending cannot lose
anything, which is why it survived this long — but it is also why a moved box
arrived as a second entry under a new name and a new port, leaving the original
entry pointing at a zone with no capacity. `comfy-qat go comfy-linux` still
failed after a successful move.

Rewriting is the fix and it is the dangerous operation in this codebase: the file
is hand-maintained, carries comments, and names every machine the user can reach.
A truncated `hosts.toml` is worse than any move failure, because at that point no
command works at all.

So the rewrite is textual, not a parse-and-re-emit — tomllib would return dicts
and the comments would not come back. And it is checked before it lands:

  1. transform the text
  2. parse the result, compare it to what was expected, and load it with the
     real loader — whatever the tool will refuse to read, this refuses to write
  3. copy the original to `hosts.toml.bak`, fsync it, and READ IT BACK; archive
     the copy it supersedes to `backups/` rather than overwriting it. A backup
     that cannot be written, or that does not read back, stops the rewrite here
  4. write a temp file, fsync it, `os.replace` it — atomic on one filesystem —
     and fsync the directory, because atomic in ordering is not durable

Step 2 is the one that catches a bad transform: a duplicate section, a dropped
host, a renamed wrong one, none of it reaches the file. Step 3 is the one that
makes the whole thing reversible, and it is why this refuses rather than
proceeds — the file is hand-maintained and has no other copy.
"""

from __future__ import annotations

import os
import re
import stat
import tomllib
from pathlib import Path

from .config import ConfigError


class HostFileError(ConfigError):
    """The rewrite was not safe to apply, so nothing was applied."""


# The three shapes this module reads, defined once each.
#
# They used to exist by COPY rather than by name — the header three times, the
# "next block starts here" pattern twice — and that, not any single regex being
# wrong, is why this module broke six times in a day. Every fix had to be made in
# each copy separately, and D85 is what happens when one is missed: 756e794
# narrowed `\s` to `[ \t]` in `without`'s header, in both `following` patterns and
# in `rename_and_add`'s header, and NOT in the port pattern — which was silently
# coupled to where `body_end` fell. `move` broke on the real host list and the
# suite stayed green. Sharing them by name makes that class of miss impossible
# rather than caught next time.
#
# `[ \t]`, never `\s`, in all of them. `\s` MATCHES NEWLINES, and nearly every
# defect this module has had is some version of a pattern crossing a line it had
# no business crossing: `^\s*\[hosts\.x\]` starts its match on the blank line
# ABOVE the header and swallows it, and that blank line is the terminator the
# comment walk in `without` depends on. The `\r` is for CRLF files, where a
# trailing `[ \t]*$` alone fails, because `$` matches before the `\n` and the line
# still ends in `\r`.

# What may follow the significant part of a line, captured so it can be put back
# exactly: spaces or tabs, an optional TOML comment, and the `\r` of a CRLF file.
#
# The comment is the reason this exists. Both patterns used to end at the value,
# so `port = 8195  # main box` did not match and `move` refused with "comfy-win
# has no port line, so its port cannot be freed" — about a line that is right
# there. The header had it worse: `[hosts.comfy-win]  # the windows one` matched
# nothing, so BOTH `move` and `delete` answered "comfy-win is not in the host
# list" about a host that plainly is.
#
# Both are valid TOML, and the file's own preamble tells people to annotate their
# hosts. Captured rather than skipped: a rewrite must not eat the note.
_TRAILING = r"([ \t]*(?:#[^\r\n]*)?\r?)$"

# `{name}` is filled in by `_header_of`. Both groups exist so `rename_and_add`
# can put back what the match consumed: group 1 is the header's own indentation,
# without which an indented block came back at column 0, and group 2 is the
# trailing run including the `\r`, without which the one line this function
# rewrites is the one LF line in a CRLF file. The port line carries the same two
# groups for the same two reasons.
_HEADER = r"^([ \t]*)\[hosts\.{name}\]" + _TRAILING

# Where the block being read ends: the next line opening a table of any kind.
FOLLOWING = re.compile(r"^[ \t]*\[", re.MULTILINE)

# The port line, with its line ending as group 2. Two defects have lived in this
# one pattern. A trailing `\s*$` swallowed the newline after the port line,
# welding it to whatever came next —
#
#     port         = 8195[hosts.comfy-linux-a]
#
# which is not valid TOML, so `apply` refused the write and `move` failed on the
# real host list while the suite stayed green. And consuming the `\r` without
# writing it back left one `\n` line in a file whose every other line ends
# `\r\n` — that parses, so it is silent, and git then reports the whole file as
# changed. Hence group 2, and the replacement that puts it back.
#
# Both were invisible to the fixtures, which are spaced differently from a file
# somebody actually maintains. That difference is the whole of why.
PORT_LINE = re.compile(r"^([ \t]*port[ \t]*=[ \t]*)\d+" + _TRAILING, re.MULTILINE)


def _header_of(name: str) -> re.Pattern[str]:
    """The `[hosts.<name>]` line: indentation as group 1, ending as group 2."""
    return re.compile(_HEADER.format(name=re.escape(name)), re.MULTILINE)


def _line_ending(text: str) -> str:
    """The line ending this file uses — LF unless it is uniformly CRLF.

    A file that already mixes the two is not something this can repair, and
    guessing which half to follow makes it worse, so it is left as LF and the
    rewrite adds nothing new.

    This module is the only one in `comfy_qa` that writes the host list as TEXT.
    `discover.to_toml`, which builds the block appended below, takes structured
    input and hard-codes `\n` — it has no idea what file it is destined for and
    should not. So the line ending is decided here, once, and everything this
    module emits is put into it.

    That holds only if the text handed in still HAS its endings, which is the
    other half and is not a fact about this module at all: **every
    read-modify-write of the host list must read it as bytes.** `read_text` is
    universal newline mode and strips every `\r` before this function is ever
    called, so it answers LF for a CRLF file and the whole file is rewritten to
    match. Use `read`, which is the one correct way in.
    """

    if "\r\n" not in text:
        return "\n"
    mixed = any(char == "\n" and (index == 0 or text[index - 1] != "\r")
                for index, char in enumerate(text))
    return "\n" if mixed else "\r\n"


def _in(ending: str, text: str) -> str:
    """`text` with every line ending rewritten to `ending`."""
    return text.replace("\r\n", "\n").replace("\n", ending)


def rename_and_add(text: str, *, name: str, renamed: str, renamed_port: int,
                   added: str) -> str:
    """Rename one `[hosts.<name>]` block, give it a new port, append another.

    The renamed block keeps everything else it had — its zone, its comments, any
    key this tool does not know about. Only the header and the port line change.
    """
    header = _header_of(name)
    if not header.search(text):
        raise HostFileError(f"{name} is not in the host list, so it cannot be moved.")

    start = header.search(text)
    assert start is not None
    body_start = start.end()
    following = FOLLOWING.search(text, body_start)
    body_end = following.start() if following else len(text)

    body = text[body_start:body_end]
    body, swapped = PORT_LINE.subn(rf"\g<1>{renamed_port}\g<2>", body, count=1)
    if not swapped:
        # A host with no port line is not something this tool writes, but the
        # file is hand-maintained and a silent no-op here would collide ports.
        raise HostFileError(f"{name} has no port line, so its port cannot be freed.")

    out = (
        text[:start.start()]
        # Everything the header match consumed and this rewrite must restore:
        # the indentation, then the trailing run and its `\r`. Dropping either
        # one is a silent whole-file diff — the block moves to column 0, or the
        # line stops being CRLF in a CRLF file.
        + start.group(1)
        + f"[hosts.{renamed}]"
        + start.group(2)
        + body
        + text[body_end:]
    )
    ending = _line_ending(text)
    return out.rstrip(ending) + ending + _in(ending, added)


def without(text: str, name: str) -> str:
    """The host list with one `[hosts.<name>]` block taken out.

    Written for `delete`, which used to finish by inviting the user to remove the
    entry themselves, or to "leave it as a note of what was there". That advice
    manufactured a later failure: `create` refuses a name when a host list entry
    OR an instance on the project holds it, and ports are allocated from the same
    list. So the note reserves both a name and a port for a machine that no longer
    exists, and nobody connects the refusal weeks later to tonight's delete.
    """
    # The blank line above a header is the terminator the comment walk below
    # depends on. When this pattern used `\s`, it started its match on that blank
    # line and swallowed it, so the walk saw the previous host's comment where it
    # expected the separator and took it — on the real host list that destroyed
    # all eight lines of the commented-out example `init` writes into every new
    # file, and reported success. See `_HEADER`, which is line-local for this
    # reason.
    header = _header_of(name)
    start = header.search(text)
    if start is None:
        raise HostFileError(f"{name} is not in the host list.")
    following = FOLLOWING.search(text, start.end())
    end = following.start() if following else len(text)

    # Stop at the blank line before the next header, not at the header itself.
    # A block runs to the next `[`, so everything between the end of this one and
    # that bracket was being taken too — and in a hand-maintained file that is
    # exactly where the NEXT host's comments live:
    #
    #     [hosts.comfy-win]        <- deleting this
    #     port = 8190
    #
    #     # comfy-linux holds the 70B checkpoint. DO NOT DELETE.
    #     [hosts.comfy-linux]      <- took the comment with it
    #
    # Every guard passed: the file parsed, the host names matched `expect`
    # exactly, config.parse accepted it. The name check cannot see this, because
    # no name is lost. It ended in "and it is out of your host list" — unqualified
    # success, having destroyed a line saying DO NOT DELETE.
    #
    # This module's own docstring gives "the file is hand-maintained, carries
    # comments" as the reason the rewrite is textual rather than parse-and-emit.
    # The textual rewrite was what ate them.
    # A comment sitting directly above a block describes THAT block, so it goes
    # with it — otherwise deleting a host strands a note about a machine that no
    # longer exists. The run stops at the first blank line, which is what
    # separates one host's notes from the previous host's body.
    begin = start.start()
    before = text[:begin].split("\n")
    while len(before) >= 2 and before[-2].lstrip().startswith("#"):
        begin -= len(before[-2]) + 1
        before.pop(-2)

    # If that walk reached the top of the file, stop and keep the comments.
    #
    # A comment run at offset 0 is either the file's own preamble or the first
    # host's note, and NOTHING CAN TELL THEM APART from the text. The obvious
    # heuristic — "a run reaching the top is the preamble" — passes six cases and
    # destroys a legitimate seventh, where the note really does belong to the
    # first host.
    #
    # So this takes the same asymmetry the rest of the tool takes about money and
    # applies it to data: for an operation that cannot be undone, be wrong in the
    # direction that leaves something behind. A stranded comment about a deleted
    # host is cosmetic and a person removes it in two seconds. A destroyed one is
    # gone, and `delete` reports success while doing it.
    if begin == 0:
        begin = start.start()

    # A comment run directly above a header belongs to THAT header — the same
    # rule the walk above applies to the block being removed, applied to the
    # block that follows it. The end used to run to the next `[`, which took the
    # next host's notes whenever the two blocks touched, and two blocks touching
    # is how the real host list is written.
    #
    # Blank lines are not the discriminator and never were. This tried to stop at
    # the last blank line in the gap, which works only when there is one; with the
    # blocks touching there is none, and the run went straight through.
    if following is not None:
        above = text[:end].split("\n")
        while len(above) >= 2 and above[-2].lstrip().startswith("#"):
            end -= len(above[-2]) + 1
            above.pop(-2)

    # And a comment run touching NEITHER block belongs to neither, so it stays.
    #
    # The two rules above are both about a run that touches something: one that
    # touches this block's body goes with it, one that touches the next header
    # goes with that. Between them they cover a run at one end of the gap or the
    # other — and silently delete one sitting in the MIDDLE, with a blank line on
    # both sides, because it is what remains after both walks have finished.
    #
    # That is not a rare shape. It is the one `init` and `discover` produce
    # together: STARTER ends with the commented-out worked example, `to_toml`
    # begins with a newline, so on every populated host list the example sits in
    # a gap with blanks on both sides. On the real file, deleting the FIRST host
    # destroyed all eight lines of it and reported success.
    #
    # So the block ends where its own run of non-blank lines ends. A blank line
    # after the body is the end of this host, and everything past it belongs to
    # somebody else or to nobody — either way it is not ours to take. Only the
    # smaller of the two ends is used, so the walk above still wins when the
    # blocks touch, which is the case it was written for.
    # From the line AFTER the header: `start.end()` sits on the header's own line
    # ending, so starting there reads an empty line and stops instantly, taking
    # the header and leaving the body behind.
    body_end = text.find("\n", start.end())
    body_end = end if body_end == -1 else body_end + 1
    while body_end < end:
        line_end = text.find("\n", body_end)
        if line_end == -1 or line_end >= end:
            break
        if not text[body_end:line_end].strip():
            end = min(end, body_end)
            break
        body_end = line_end + 1

    # The separator and the final newline are the file's own, not `\n`. Hard-coded
    # they put a lone LF into a CRLF file exactly as the header did — the same
    # defect in the sibling function, found the same way.
    ending = _line_ending(text)
    return (text[:begin].rstrip(ending) + ending * 2
            + text[end:].lstrip(ending)).rstrip(ending) + ending


def read(path: Path) -> str:
    """The host list as text, with its line endings intact. The only way in.

    Not `path.read_text()`, and that is the whole function rather than a detail
    of it. `read_text` opens in UNIVERSAL NEWLINE MODE: every `\r\n` on disk
    becomes `\n` in the string before any of this module sees it. `_line_ending`
    then finds no `\r\n`, answers `"\n"`, `_in` rewrites nothing, and `apply`
    writes the whole file back as LF.

    **This is not a fact about two call sites. It is a property of every
    read-modify-write of the host list** — any function that reads this file as
    text and writes the whole thing back has this defect, whether or not it
    knows a line ending exists. Reach the file through here and the question
    cannot arise; open it yourself with `read_text` and it does, silently. That
    is why this is a named function with one spelling rather than a line each
    caller remembers: three copies of one truth is how this module got six
    defects in `without` alone.

    Measured on a uniformly-CRLF host list: `without` returned seven lone LFs
    and `rename_and_add` twenty, against zero for the same file read as bytes,
    and a 22-line CRLF host list came back entirely LF after one `discover`.

    Every generated case in `test_hostfile.py` passed throughout, because those
    build a CRLF string in the test rather than reading one the way the product
    reads one — 512 shapes, three victims, both functions, all green, all about
    a string no caller could produce.

    Worth keeping in proportion, and the honest version is sharper than
    "irreversible". `_keep_a_copy` means the file itself is recoverable; the
    damage is that NOTHING SAYS IT HAPPENED. TOML does not care about line
    endings, so the rewritten file parses, the loader is happy, every command
    still works, and the only signal is a hand-maintained file whose every line
    changed under a `.bak` nobody has a reason to look at.
    """
    return path.read_bytes().decode("utf-8")


def added(text: str, blocks: list[str]) -> str:
    """`text` with each block on the end, in the line ending the file already uses.

    The sibling of `rename_and_add` and `without`, and it exists for the same
    reason they do: `discover.to_toml` hard-codes `\n` and opens with a blank
    line, because it builds a block without knowing what file it is destined for.
    A lone LF appended to a CRLF file still parses, so nothing complains, and git
    then reports the whole file as changed. The ending is decided here, once.
    """
    if not blocks:
        return text
    ending = _line_ending(text)
    # `rstrip` then one ending: the block's own leading newline supplies the
    # blank line that separates it from the block above, so a file that already
    # ends in two newlines would otherwise gain a third.
    return text.rstrip(ending) + ending + "".join(_in(ending, block) for block in blocks)


def declared(text: str) -> set[str]:
    """Every host name this text declares. An empty set if it will not parse.

    `apply`'s `expect` for an append is "what is already in the file, plus what
    is being added", and the first half has to be read from the FILE rather than
    from a loaded host list. `load` refuses files this still reads — two entries
    for one instance, two names differing only in case — so passing the loaded
    names would report a file that was ALREADY unloadable as a rewrite that lost
    a machine. Different problem, different remedy, and the wrong one is the one
    that reads as "this tool just ate your host list".

    Text that will not parse is not this function's refusal to make. `apply`
    parses what it is about to write, an unparseable file cannot produce a
    parseable append, and the message there carries the position of the error.
    """
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return set()
    return set((parsed.get("hosts") or {}).keys())


def add(path: Path, blocks: list[str], *, initial: str) -> None:
    """Append blocks to the host list, through the checks a rewrite gets.

    Three commands used to append with a bare `path.open("a")`: `discover`,
    `create` and `setup`. Appending cannot lose a line, which is why that
    survived — but it is not why `apply` exists. `apply` exists because a write
    can leave behind a file that PARSES and the loader still REFUSES, after
    which no comfy-qat command works at all until somebody hand-edits it, and an
    append reaches that state as easily as a rewrite does: `discover` writing a
    block headed with Google's `comfy-win` into a file that already calls a
    machine `Comfy-Win` produced exactly that, reported "added 1 host", and left
    nothing to restore.

    The argument settled for `init --force` is the one that applies here: the
    file is hand-maintained and has no other copy, which is a property of the
    file and not of whether the write is an overwrite or an append.

    `initial` is what to append to when there is no file yet — the starter list —
    so the first write of a host list is one atomic replace rather than a
    `write_text` followed by an append.
    """
    # Through `read`, not a second copy of its one line. This was written
    # correctly inline and was still the second spelling of the same truth; the
    # third would have been whoever wrote the next appender.
    text = read(path) if path.exists() else initial
    fresh: set[str] = set()
    for block in blocks:
        fresh |= declared(block)
    apply(path, added(text, blocks), expect=declared(text) | fresh)


def apply(path: Path, text: str, *, expect: set[str]) -> None:
    """Check the new text, back the old one up, and swap it in atomically.

    `expect` is every host name the result must contain — exactly, no more and no
    fewer. Getting this wrong is how a rewrite quietly loses a machine.
    """
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise HostFileError(f"the rewritten host list would not parse: {exc}") from exc

    got = set((parsed.get("hosts") or {}).keys())
    if got != expect:
        missing = ", ".join(sorted(expect - got)) or "nothing"
        extra = ", ".join(sorted(got - expect)) or "nothing"
        raise HostFileError(
            "the rewritten host list does not hold what it should — "
            f"missing {missing}, unexpected {extra}. Nothing was written."
        )

    # Names being right is not the file being right. Comparing names let through
    # the worst outcome this module can produce: a file that parses, holds exactly
    # the expected names, and is then REFUSED by the loader — after which no
    # comfy-qat command works at all until somebody hand-edits it.
    #
    # The way in is the most ordinary sequence there is. A stockout pushes a box
    # out of us-central1-c, capacity comes back, you move it home. Move one
    # retires the old entry as `<name>-us-central1-c`, still naming instance
    # `<name>` in zone c. Move two puts `<name>` back in zone c. Two entries, two
    # distinct names, one machine — which `config.parse` rejects, correctly,
    # because reading results from the wrong box is what this tool exists to stop.
    #
    # It lands at the REGISTER step, so by then the box is moved, running and
    # billing, and `down` can no longer reach it.
    #
    # So validate with the real loader rather than a proxy for it. Whatever the
    # tool will refuse to read, this refuses to write.
    from .config import ConfigError, parse

    try:
        parse(parsed)
    except ConfigError as exc:
        raise HostFileError(
            f"the rewritten host list would not load: {exc} Nothing was written."
        ) from exc

    if path.exists():
        _keep_a_copy(path)

    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        _write_durably(temporary, text, like=path)
        os.replace(temporary, path)
        # And the rename itself. `os.replace` is atomic in ORDERING — you get the
        # old file or the new one, never a half-written one — which is not the
        # same as durable. The directory entry lives in the directory's own
        # metadata, so without this the rename can still be lost by a crash that
        # the data fsync above survived, and the file reverts to its old content
        # while `.bak` says the write happened.
        _sync_directory(path.parent)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


# How many superseded copies to keep, and where.
#
# `hosts.toml.bak` stays exactly where it is and keeps its name: it is quoted in
# docs/machines.md, docs/troubleshooting.md and Ali's own test criteria (R5d,
# R5f), so moving it would break a check somebody runs by hand. It is the most
# recent copy.
#
# Everything older goes in `backups/`, timestamped, capped. In a subdirectory
# rather than scattered beside the real file, because that directory is Ali's:
# anything this tool leaves there should be obviously the tool's and obviously
# disposable, and `hosts.toml.bak.1` through `.5` sitting next to the file he
# maintains by hand is neither.
BACKUP_GENERATIONS = 5
BACKUP_DIRECTORY = "backups"


def _keep_a_copy(path: Path) -> None:
    """Put the current content somewhere recoverable, and PROVE that it is.

    Three things were wrong here and they are one promise broken three ways.

    The copy was ONE DEEP and overwritten on every write, so the sequence that
    actually happens — a move, then noticing something is wrong, then another
    move — destroyed the only copy of the state you wanted back. Ali's host list
    is hand-maintained and has no other copy anywhere on this machine. A `.bak`
    whose content differs from the one about to replace it is now archived
    before it is overwritten, never destroyed.

    It was written with `write_text` and NEVER READ BACK. A backup nobody has
    verified is a belief, not a copy, and the very next step rewrites the only
    file there is. It is now fsynced and compared byte for byte.

    And a backup that cannot be written at all used to let the rewrite proceed
    anyway. It now refuses. Declining a move is recoverable; doing one that
    cannot be undone is not.
    """
    content = path.read_bytes()
    backup = path.with_name(path.name + ".bak")

    try:
        # The copy about to be overwritten is a state nobody has since
        # reproduced. Archived by RENAME, so it is never held in one place only.
        if backup.exists() and backup.read_bytes() != content:
            _archive(backup, path)

        _write_durably(backup, content, like=path)
    except OSError as exc:
        raise HostFileError(
            f"the previous {path.name} could not be copied ({exc.strerror or exc}), "
            f"so this rewrite could not be undone. Nothing was written."
        ) from exc

    if backup.read_bytes() != content:
        raise HostFileError(
            f"the backup of {path.name} did not read back the same as the file "
            f"it was copied from, so the previous host list is not recoverable. "
            f"Nothing was written."
        )

    # Pruned AFTER the new copy is on the disk, never before: a prune that runs
    # first and then fails to write leaves fewer copies than it started with.
    _prune(path)


def _archive(backup: Path, path: Path) -> None:
    """Move a superseded backup into `backups/`, named for when it was taken."""
    from datetime import UTC, datetime

    directory = path.parent / BACKUP_DIRECTORY
    directory.mkdir(exist_ok=True)
    stamp = datetime.fromtimestamp(backup.stat().st_mtime, UTC).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"{path.name}.{stamp}.bak"
    # Two rewrites inside one second is not a hypothetical — `move` retires one
    # entry and adds another — and a colliding name would silently drop a copy.
    suffix = 2
    while target.exists():
        target = directory / f"{path.name}.{stamp}-{suffix}.bak"
        suffix += 1
    os.replace(backup, target)


def _prune(path: Path) -> None:
    """Keep the newest `BACKUP_GENERATIONS` archived copies, drop the rest."""
    directory = path.parent / BACKUP_DIRECTORY
    if not directory.is_dir():
        return
    kept = sorted(directory.glob(f"{path.name}.*.bak"),
                  key=lambda item: item.stat().st_mtime, reverse=True)
    for stale in kept[BACKUP_GENERATIONS:]:
        stale.unlink(missing_ok=True)


def _write_durably(path: Path, content: str | bytes, *, like: Path | None = None) -> None:
    """Write, flush, and fsync — and give the file the mode of `like`.

    `write_text` returns once the bytes are in the page cache, which is why a
    crash could leave a truncated or empty host list behind a rewrite that had
    already reported success.

    `like` carries the mode across because a backup created at the default umask
    is more permissive than the file it copies. Nothing in a host list is a
    secret, but a file that quietly widens its own permissions on every write is
    the kind of thing that is true of something else later.
    """
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    with open(path, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    if like is not None and like.exists():
        os.chmod(path, stat.S_IMODE(like.stat().st_mode))


def _sync_directory(directory: Path) -> None:
    """fsync a directory, where the platform allows it."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:          # pragma: no cover - Windows cannot open a directory
        return
    try:
        os.fsync(fd)
    except OSError:          # pragma: no cover - some filesystems refuse
        pass
    finally:
        os.close(fd)
