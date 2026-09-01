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


def _sections(text: str) -> list[str]:
    return re.findall(r"^\s*\[hosts\.([^\]]+)\]", text, flags=re.MULTILINE)


def rename_and_add(text: str, *, name: str, renamed: str, renamed_port: int,
                   added: str) -> str:
    """Rename one `[hosts.<name>]` block, give it a new port, append another.

    The renamed block keeps everything else it had — its zone, its comments, any
    key this tool does not know about. Only the header and the port line change.
    """
    header = re.compile(rf"^(\s*)\[hosts\.{re.escape(name)}\]\s*$", re.MULTILINE)
    if not header.search(text):
        raise HostFileError(f"{name} is not in the host list, so it cannot be moved.")

    start = header.search(text)
    assert start is not None
    body_start = start.end()
    following = re.compile(r"^\s*\[", re.MULTILINE).search(text, body_start)
    body_end = following.start() if following else len(text)

    body = text[body_start:body_end]
    body, swapped = re.subn(r"^(\s*port\s*=\s*)\d+\s*$", rf"\g<1>{renamed_port}",
                            body, count=1, flags=re.MULTILINE)
    if not swapped:
        # A host with no port line is not something this tool writes, but the
        # file is hand-maintained and a silent no-op here would collide ports.
        raise HostFileError(f"{name} has no port line, so its port cannot be freed.")

    out = (
        text[:start.start()]
        + f"[hosts.{renamed}]"
        + body
        + text[body_end:]
    )
    return out.rstrip("\n") + "\n" + added


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
