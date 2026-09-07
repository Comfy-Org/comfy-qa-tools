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
  2. parse the result and compare it to what was expected
  3. copy the original to `hosts.toml.bak`
  4. write a temp file and `os.replace` it, which is atomic on one filesystem

Step 2 is the one that matters. A transform that produced a duplicate section, or
dropped a host, or renamed the wrong one, never reaches the file.
"""

from __future__ import annotations

import os
import re
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
        backup = path.with_name(path.name + ".bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
