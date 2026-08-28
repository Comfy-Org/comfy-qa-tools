"""hosts.toml — the declared list of machines this tool operates.

Nothing here talks to the network. Loading and validating the host list is
deliberately offline and total: every rule that can be checked without gcloud is
checked here, so a bad config fails in milliseconds rather than half-way through
starting a GPU instance.
"""

from __future__ import annotations

import difflib
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ComfyUI's own default. The primary local install on this machine holds it, and
# forwarding a remote onto it is the single mistake this tool exists to prevent,
# so no remote host may ever claim it.
COMFYUI_DEFAULT_PORT = 8188

Kind = Literal["local", "gce"]

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "comfy-qa-tools" / "hosts.toml"


class ConfigError(Exception):
    """A hosts.toml that cannot be trusted. The message is shown to the user."""


@dataclass(frozen=True)
class Host:
    """One machine, local or in Google Cloud.

    `port` is where ComfyUI is reached *on this machine*: for a local host that
    is the port it actually serves on; for a GCE host it is the near end of the
    SSH tunnel. Keeping them in one field is what lets `list` and `stamp` treat
    both kinds identically.
    """

    name: str
    kind: Kind
    port: int
    os: str | None = None
    gpu: str | None = None
    gce_instance: str | None = None
    gce_zone: str | None = None
    gce_project: str | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def is_remote(self) -> bool:
        return self.kind == "gce"


_REQUIRED_FOR_GCE = ("os", "gpu", "gce_instance", "gce_zone", "gce_project")

_KNOWN_FIELDS = frozenset({"kind", "port", *_REQUIRED_FOR_GCE})

# The three fields that say which cloud box an entry is. They are what `up`,
# `open`, `down` and `move` operate on, so an entry carrying them is a machine
# that costs money whatever its `kind` says.
_CLOUD_FIELDS = ("gce_instance", "gce_zone", "gce_project")

# The one name this tool reserves. The starter host list teaches it, every
# example uses it, and `host stamp local` has exactly one obvious meaning.
LOCAL_NAME = "local"

# A host name is two things at once: an argument you type (`host stamp <name>`)
# and part of a filename (`tunnels/<name>.pid`). Both want the same shape, and
# TOML table keys are otherwise unrestricted — `""`, `"   "`, `"--config"`,
# `"../evil"` and `"comfy\nwin"` are all valid keys and none of them is a name
# anyone can use.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _named(field: str) -> str:
    """A rejected field, with the field it was probably meant to be."""
    near = difflib.get_close_matches(field, sorted(_KNOWN_FIELDS), n=1, cutoff=0.6)
    return f"{field!r} (did you mean {near[0]!r}?)" if near else repr(field)


def _parse_host(name: str, raw: object) -> Host:
    # Before anything about the machine: is this a name at all. Everything below
    # reports errors under it, and a name that cannot be typed cannot be acted on
    # even when the rest of the entry is perfect.
    if not isinstance(name, str) or not _NAME.match(name):
        raise ConfigError(
            f"host name {name!r} cannot be used. A name has to start with a letter or "
            "a digit and hold only letters, digits, dots, dashes and underscores — it "
            "is typed as an argument and used as a filename, so a name that is blank, "
            "padded, or starts with a dash is read as an option or cannot be typed at "
            "all. Rename it, e.g. comfy-win."
        )

    if not isinstance(raw, dict):
        raise ConfigError(f"host {name!r}: expected a table, got {type(raw).__name__}")

    # Checked first, and deliberately so. A typo'd field is the *cause* of every
    # error underneath it: `gce_zoen` used to be reported as "kind 'gce' requires
    # os, gpu, gce_instance, gce_zone, gce_project", which names five fields that
    # are all present and never mentions the one that is misspelt.
    unknown = sorted(set(raw) - _KNOWN_FIELDS)
    if unknown:
        raise ConfigError(
            f"host {name!r}: unknown field(s) {', '.join(_named(f) for f in unknown)}. "
            f"Known fields: {', '.join(sorted(_KNOWN_FIELDS))}."
        )

    kind = raw.get("kind")
    if kind not in ("local", "gce"):
        raise ConfigError(
            f"host {name!r}: kind must be 'local' or 'gce', got {kind!r}"
        )

    if kind == "local":
        # This one costs money. `host down` decides what to stop from `kind`
        # alone: for a local host it reports "local ComfyUI left running" and
        # returns without calling stop. A cloud box mistyped as local — or edited
        # down to one after a move — therefore reads as a successful `host down`
        # while the GPU keeps billing all night.
        cloud = [key for key in _CLOUD_FIELDS if raw.get(key)]
        if cloud:
            raise ConfigError(
                f"host {name!r}: kind 'local' cannot carry {', '.join(cloud)}. Stopping "
                "a machine is decided from 'kind', so a cloud box declared local is "
                "never stopped and keeps billing. Set kind = \"gce\" if it is a cloud "
                "box, or delete those fields if it is not."
            )
    if kind == "gce" and name.lower() == LOCAL_NAME:
        raise ConfigError(
            f"host {name!r}: the name 'local' is reserved for the ComfyUI on this "
            "computer, which is what every example and the starter host list means by "
            "it. A cloud box wearing it puts an invisible default back. Rename the box, "
            "e.g. comfy-win or comfy-linux."
        )

    port = raw.get("port", COMFYUI_DEFAULT_PORT if kind == "local" else None)
    if port is None:
        raise ConfigError(f"host {name!r}: kind 'gce' requires an explicit port")
    if not isinstance(port, int) or isinstance(port, bool):
        raise ConfigError(f"host {name!r}: port must be an integer, got {port!r}")
    if not 1024 <= port <= 65535:
        raise ConfigError(f"host {name!r}: port {port} is outside 1024-65535")

    if kind == "gce":
        if port == COMFYUI_DEFAULT_PORT:
            raise ConfigError(
                f"host {name!r}: port {COMFYUI_DEFAULT_PORT} is reserved for the local "
                "ComfyUI. A tunnel on it would silently point you at the wrong machine — "
                "pick another port, e.g. 8190."
            )
        missing = [k for k in _REQUIRED_FOR_GCE if not raw.get(k)]
        if missing:
            raise ConfigError(
                f"host {name!r}: kind 'gce' requires {', '.join(missing)}"
            )

    return Host(
        name=name,
        kind=kind,
        port=port,
        os=raw.get("os"),
        gpu=raw.get("gpu"),
        gce_instance=raw.get("gce_instance"),
        gce_zone=raw.get("gce_zone"),
        gce_project=raw.get("gce_project"),
    )


def parse(data: dict) -> list[Host]:
    """Validate an already-decoded hosts.toml. Raises ConfigError on any problem.

    Three rules run across the whole list rather than one entry, and all three
    are the same rule underneath: **one entry, one machine, one way to reach it.**
    A host list that breaks any of them still loads, still lists, and still
    stamps — and then a test matrix records "reproduced on A, not on B" about two
    names for one box, or about whichever of two spellings a shift key produced.
    """
    if not isinstance(data, dict):
        raise ConfigError(
            f"expected a host list of [hosts.<name>] tables, got {type(data).__name__}"
        )

    hosts_table = data.get("hosts")
    if not isinstance(hosts_table, dict) or not hosts_table:
        raise ConfigError("no [hosts.<name>] tables found")

    hosts = [_parse_host(name, raw) for name, raw in hosts_table.items()]

    seen: dict[int, str] = {}
    for host in hosts:
        clash = seen.get(host.port)
        if clash is not None:
            raise ConfigError(
                f"hosts {clash!r} and {host.name!r} both use port {host.port}. "
                "Every host needs its own port, or you cannot tell which one you "
                "reached."
            )
        seen[host.port] = host.name

    # Two names that differ only in case are one machine typed two ways far more
    # often than they are two machines. Lookup already falls back to a
    # case-insensitive match, so with both declared which box you reach depends
    # on a shift key.
    folded: dict[str, str] = {}
    for host in hosts:
        clash = folded.get(host.name.lower())
        if clash is not None:
            raise ConfigError(
                f"hosts {clash!r} and {host.name!r} differ only in case. Which machine "
                "you reached would depend on a shift key, so they cannot both be "
                "declared. Rename one of them, or delete it if they are the same box."
            )
        folded[host.name.lower()] = host.name

    # The port rule says every host answers on its own port. It does not say
    # every host is its own machine — and two entries for one instance is the
    # wrong-machine failure this whole tool exists to prevent, arriving as a
    # host list that validates.
    boxes: dict[tuple[str, str, str], str] = {}
    for host in hosts:
        if not host.is_remote:
            continue
        box = (host.gce_project or "", host.gce_zone or "", host.gce_instance or "")
        clash = boxes.get(box)
        if clash is not None:
            raise ConfigError(
                f"hosts {clash!r} and {host.name!r} are the same machine: instance "
                f"{host.gce_instance!r} in {host.gce_zone} ({host.gce_project}). Two "
                "entries, two ports, two tunnels, one box — and a result recorded "
                "against one of those names says nothing whatever about the other. "
                "Delete one, or point it at a different instance."
            )
        boxes[box] = host.name

    return hosts


def load(path: Path | None = None) -> list[Host]:
    """Read and validate hosts.toml. Raises ConfigError if it is missing or bad."""
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(
            f"no host list at {path}. Run `comfy-qat init` to write a starter one."
        )
    # `exists()` is true of a directory, of a file owned by someone else, and of
    # a file that is not text at all. Each of those reaches `read_text` and, until
    # now, came back as a traceback from a function whose whole promise is a
    # message — `--config` pointed at the folder rather than the file in it is
    # enough to do it.
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(
            f"{path} is not UTF-8 text, so it cannot be a host list. Check it was not "
            "saved as UTF-16 by an editor, truncated by a half-finished write, or "
            "overwritten with something binary."
        ) from exc
    except OSError as exc:
        raise ConfigError(
            f"{path} could not be read: {exc}. Check that it is a file rather than a "
            "directory, and that you own it — `--config` pointed at the folder instead "
            "of the hosts.toml inside it looks exactly like this."
        ) from exc

    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    return parse(data)


# --- naming a machine -----------------------------------------------------
#
# You may name a host, or describe the one you want: its operating system, its
# card, or both — `windows`, `l4`, `windows/l4`. That is how people actually
# think about these machines, and it removes the step where you go and look up
# what you called the box.
#
# It is not a default. A description picks a host only when exactly one host
# fits it, and the host it picked is always printed. A description that fits two
# machines is refused with both named, because the one thing this tool must never
# do is quietly point you at the other box.

# What a person types, and the words that appear in an `os` field. `os` is
# written by discover from Google's licence names — "Ubuntu 22.04", "Debian 12",
# "Windows Server 2022" — so "linux" has to cover the distributions, because no
# host is ever labelled "Linux".
OS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "windows": ("windows",),
    "linux": ("linux", "ubuntu", "debian", "rocky", "centos", "rhel", "fedora", "suse"),
    "ubuntu": ("ubuntu",),
    "debian": ("debian",),
    "macos": ("macos", "mac os", "darwin"),
    "local": ("macos", "mac os", "darwin"),
}

# These two mean "the machine I am sitting at", so a `kind = "local"` host
# answers to them even though it usually declares no `os` at all.
_LOCAL_KEYWORDS = ("macos", "local")

# One separator, so there is nothing to remember. Anything else that reads like
# two selectors run together is refused by name rather than quietly missing.
SEPARATOR = "/"
_WRONG_SEPARATORS = re.compile(r"[-_,+\s]+")

# Most specific first: an Ubuntu box answers to both `ubuntu` and `linux`, and
# the narrower word is the more useful thing to suggest when two boxes clash.
_BY_SPECIFICITY = ("windows", "ubuntu", "debian", "macos", "linux", "local")


def describe(host: Host) -> str:
    """How a host reads in a one-line answer: what it runs, and on what card."""
    detail = ", ".join(part for part in (host.os, host.gpu) if part and part != "none")
    return detail or ("local install" if host.kind == "local" else host.kind)


def _matches_os(host: Host, keyword: str) -> bool:
    if keyword in _LOCAL_KEYWORDS and host.kind == "local":
        return True
    declared = (host.os or "").lower()
    return bool(declared) and any(sign in declared for sign in OS_KEYWORDS[keyword])


def _matches_gpu(host: Host, token: str) -> bool:
    """`a100` finds an `A100-80GB`, because nobody types the full SKU."""
    declared = (host.gpu or "").lower()
    if declared in ("", "none"):
        return False
    return declared == token or declared.startswith(token)


def _matching(hosts: list[Host], part: str) -> list[Host]:
    """One axis of a description. OS and card are the same operation."""
    if part in OS_KEYWORDS:
        return [host for host in hosts if _matches_os(host, part)]
    return [host for host in hosts if _matches_gpu(host, part)]


def _selector_for(host: Host) -> str:
    """The shortest description that would have picked this host on its own."""
    parts = [word for word in _BY_SPECIFICITY if _matches_os(host, word)][:1]
    if host.gpu and host.gpu.lower() != "none":
        parts.append(host.gpu.lower())
    return SEPARATOR.join(parts) or host.name


@dataclass(frozen=True)
class Resolution:
    """Which host was meant, and whether a description rather than a name found it."""

    host: Host
    selector: str | None = None

    def line(self) -> str | None:
        """What to print, so a description never resolves silently."""
        if self.selector is None:
            return None
        return f"{self.selector} -> {self.host.name} ({describe(self.host)})"


def resolve(hosts: list[Host], name: str) -> Resolution:
    """Find the host meant by a name, or by an OS, a card, or both.

    Names win outright: a host called `windows` is that host, never a description.
    """
    wanted = (name or "").strip()
    for host in hosts:
        if host.name == wanted:
            return Resolution(host=host)

    lowered = wanted.lower()
    for host in hosts:
        if host.name.lower() == lowered:
            return Resolution(host=host)

    parts = [part.strip() for part in lowered.split(SEPARATOR) if part.strip()]
    if any(part in OS_KEYWORDS or _matching(hosts, part) for part in parts):
        selector = SEPARATOR.join(parts)
        candidates = list(hosts)
        for part in parts:
            candidates = _matching(candidates, part)
        if len(candidates) == 1:
            return Resolution(host=candidates[0], selector=selector)
        if not candidates:
            raise ConfigError(
                f"nothing declared matches {selector!r}. Declared: {_inventory(hosts)}. "
                "Create the box in the Google Cloud console, then "
                "`comfy-qat discover` to add it to your host list."
            )
        listed = ", ".join(f"{h.name} ({describe(h)})" for h in candidates)
        # "Add the other half" is only advice when there is another half to add.
        # Told `windows/l4` against two Windows L4 boxes, the old message said
        # "add the other half, e.g. `windows/l4`" — telling a tester to type the
        # thing they had just typed. When both axes are already given, or when
        # adding one would not separate these machines anyway, the only answer
        # left is the name.
        narrower = _selector_for(candidates[0])
        can_narrow = (
            len(parts) < 2
            and narrower != selector
            and len({_selector_for(host) for host in candidates}) > 1
        )
        if can_narrow:
            raise ConfigError(
                f"{selector!r} matches {len(candidates)} hosts: {listed}. Say which "
                f"one: add the other half, e.g. `{narrower}`, or use the host's name."
            )
        raise ConfigError(
            f"{selector!r} matches {len(candidates)} hosts: {listed}. They are the "
            f"same operating system and the same card, so only the name tells them "
            f"apart: {', '.join(host.name for host in candidates)}."
        )

    run_together = [part for part in _WRONG_SEPARATORS.split(lowered) if part]
    if len(run_together) > 1 and all(
        part in OS_KEYWORDS or _matching(hosts, part) for part in run_together
    ):
        raise ConfigError(
            f"{wanted!r} is two descriptions run together. The separator is "
            f"{SEPARATOR!r}: {SEPARATOR.join(run_together)}"
        )

    known = ", ".join(h.name for h in hosts) or "none declared"
    # Three separate things — what went wrong, what exists, what the vocabulary
    # is — and run together on one line they took a second reading to untangle.
    # This is the first message a new tester meets, so it gets three lines.
    raise ConfigError(
        f"unknown host {wanted!r}.\n"
        f"  declared:  {known}\n"
        f"  or describe the machine: an operating system "
        f"({', '.join(OS_KEYWORDS)}), a card ({_cards(hosts)}), "
        f"or both as os{SEPARATOR}card"
    )


def _inventory(hosts: list[Host]) -> str:
    return "; ".join(f"{h.name} ({describe(h)})" for h in hosts) or "nothing"


def _cards(hosts: list[Host]) -> str:
    cards = sorted({h.gpu for h in hosts if h.gpu and h.gpu.lower() != "none"})
    return ", ".join(card.lower() for card in cards) or "none declared"


def find(hosts: list[Host], name: str) -> Host:
    """Look a host up by name or description, keeping only the machine."""
    return resolve(hosts, name).host
