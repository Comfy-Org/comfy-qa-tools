"""Which paths a piece of user-facing text claims the tool looks at.

Two tests hold documentation to the same bar: a docstring or a `help=` that
names the host list must name the *real* one, derived from
`DEFAULT_CONFIG_PATH`. Both used to decide who was in scope like this:

    if DEFAULT_CONFIG_PATH.name not in text:
        pytest.skip("this docstring does not quote a path")
    assert DEFAULT_AS_WRITTEN in text

`DEFAULT_CONFIG_PATH.name` is the bare filename, `hosts.toml` — which is also a
substring of the thing being asserted. So the token that put a text on the list
was a token that could only come from the right answer, and **the only wrong
paths in scope were the ones that kept the right filename.** A docstring saying
the host list lives in `~/.config/comfy-qa-tools/config.toml`, or
`~/.comfyqat/machines.toml`, did not fail: it left the suite silently, as a skip.
That is `docs/tests-that-cannot-fail.md` section 7 — the inclusion criterion is
also the pass criterion — with section 3's disappearing act as the symptom, 15
skips in `test_config_inheritance.py` and 1 in `test_option_help.py`.

The remedy that section asks for is two vocabularies that do not overlap:

  - **inclusion is by SHAPE** — does this text point at a location on disk at
    all — which is what this module answers, and it knows nothing about where
    the host list actually is;
  - **the pass criterion is by IDENTITY** — is that location the real one —
    which is the caller's assertion, and it is the only place
    `DEFAULT_CONFIG_PATH` is read.

A wrong path can therefore no longer excuse itself. It is included on being
path-shaped, and then it is wrong.

Three shapes count as pointing at a location, and nothing else does:

  - home-relative — `~/.config/comfy-qa-tools/hosts.toml`;
  - absolute — `/etc/comfy-qa-tools/hosts.toml`;
  - a dotted configuration directory — `.comfyqat/machines.toml`, which is how a
    path gets written when the `~/` is left off.

What is deliberately NOT a path here is a bare relative token: `read/update` and
`--os/--gpu` are prose, and a collector that read them would put every command in
scope for a check about file locations. The cost of that boundary is that a
location written with no root at all — `comfy-qa-tools/hosts.toml` — is invisible
to this module. That is a known hole, recorded rather than papered over; widen
the pattern and its test together if one ever appears.

Trailing sentence punctuation is stripped, because the real help text ends
"Default: ~/.config/comfy-qa-tools/hosts.toml." and the full stop is not part of
the path.

`tests/test_pathnames.py` is the guard on this guard, and the case it exists for
is the one above: a wrong path that does not contain `hosts.toml` must come back
from here, or both callers go quiet again in exactly the way they already did.
"""

from __future__ import annotations

import re

# The closing characters a path can pick up from the sentence around it. A path
# never ends in one, so they come off the end rather than into the token.
_TRAILING = ".,;:!?)]}\"'`"

_PATH_SHAPED = re.compile(
    r"""
      ~/[^\s`'")\];,]+                        # ~/.config/comfy-qa-tools/hosts.toml
    | (?<![\w.~])/(?:[\w.\-]+/)+[\w.\-]+      # /etc/comfy-qa-tools/hosts.toml
    | (?<![\w/~.])\.[\w\-]+/[^\s`'")\];,]+    # .comfyqat/machines.toml
    """,
    re.VERBOSE,
)


def paths_named(text: str) -> list[str]:
    """Every location on disk `text` points at, in the order it names them.

    Empty means the text makes no claim about where anything lives — which is
    the only honest reason for one of the callers to have nothing to check.
    """
    return [match.group(0).rstrip(_TRAILING) for match in _PATH_SHAPED.finditer(text or "")]
