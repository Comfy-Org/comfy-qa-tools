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

# `{name}` is filled in by `_header_of`. Group 1 is the header's own indentation:
# matched so that an indented `[hosts.x]` is found at all, and captured so that
# `rename_and_add` can put it back rather than flattening the block to column 0.
_HEADER = r"^([ \t]*)\[hosts\.{name}\][ \t\r]*$"

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
PORT_LINE = re.compile(r"^([ \t]*port[ \t]*=[ \t]*)\d+([ \t]*\r?)$", re.MULTILINE)


def _header_of(name: str) -> re.Pattern[str]:
    """The `[hosts.<name>]` line, with its own indentation as group 1."""
    return re.compile(_HEADER.format(name=re.escape(name)), re.MULTILINE)


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
        # Group 1 is the header's own indentation; not writing it back flattened
        # the block to column 0 on the way out.
        + start.group(1)
        + f"[hosts.{renamed}]"
        + body
        + text[body_end:]
    )
    return out.rstrip("\n") + "\n" + added


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

    return (text[:begin].rstrip("\n") + "\n\n"
            + text[end:].lstrip("\n")).rstrip("\n") + "\n"


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
