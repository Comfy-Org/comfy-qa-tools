"""hosts.toml — the declared list of machines this tool operates.

Nothing here talks to the network. Loading and validating the host list is
deliberately offline and total: every rule that can be checked without gcloud is
checked here, so a bad config fails in milliseconds rather than half-way through
starting a GPU instance.
"""

from __future__ import annotations

from . import osfamily

import difflib
import re
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from .osfamily import DARWIN, FAMILY_WORDS, LINUX, WINDOWS

# ComfyUI's own default. The primary local install on this machine holds it, and
# forwarding a remote onto it is the single mistake this tool exists to prevent,
# so no remote host may ever claim it.
COMFYUI_DEFAULT_PORT = 8188

# A kind is a registry key, not one of two words. `Kind = Literal["local",
# "gce"]` is what it was, and the literal was the least of it: what made `kind` a
# two-valued flag was that everything needing to know WHICH machine an entry
# meant read Google's three fields off the host and built a tuple of them. See
# `KINDS` and `Host.machine_id`.
Kind = str

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "comfy-qa-tools" / "hosts.toml"


class ConfigError(Exception):
    """A hosts.toml that cannot be trusted. The message is shown to the user."""


@dataclass(frozen=True)
class MachineKind:
    """One sort of machine, and — the point of this type — what names one.

    **Identity used to be a GCE-shaped tuple read at each consumer.** The host
    list's same-machine rule built `(gce_project, gce_zone, gce_instance)`, the
    tunnel matched on `(port, instance, zone, project)`, and both of them read
    those fields straight off the `Host`. Three things followed, and all three
    are the wrong-machine failure this tool exists to prevent:

      * every machine that is not a Google instance came out `("", "", "")`, so
        the SECOND one was refused as a duplicate of the first;
      * `is_remote` was `kind == "gce"`, which hid that — the rule skipped
        exactly the hosts it would have broken, so the collapse waited for the
        day something widened it;
      * the tunnel's key fell back to the host's NAME, and a name survives a
        machine being destroyed and recreated.

    So a kind states what names its own machines and every consumer goes through
    `Host.machine_id`. Adding a provider is an entry in `KINDS` plus the fields
    it names its machines by; it is not a `kind` comparison at each call site,
    which is the shape that produced the direct `gce_*` reads in the first place.

    `in_words` is a `str.format` template over the identifying fields, so a
    refusal can name the machine it means the way a person would. GCE's reads
    exactly as the hand-written sentence it replaces, because a refusal's wording
    is part of the tool and troubleshooting.md quotes it.
    """

    name: str
    #: Reached through a tunnel on this computer, rather than being this
    #: computer. Gates tunnelling, `stamp`, `up`, `open` and `down`.
    remote: bool
    #: Fields an entry of this kind must carry, beyond `kind` and `port`.
    requires: tuple[str, ...] = ()
    #: Further fields an entry of this kind may carry.
    accepts: tuple[str, ...] = ()
    #: The fields that, together, name ONE machine — most general first.
    #:
    #: EMPTY MEANS "CANNOT NAME ONE", WHICH IS NOT "THEY ARE ALL THE SAME ONE".
    #: That difference is the whole of `Host.machine_id` returning `None`.
    #:
    #: A field here that changes when the machine is destroyed and recreated is
    #: a feature, not a problem: it is what stops a record outliving its
    #: machine. GCE's do not change, and must not — a GCE box keeps its identity
    #: across a stop and a start, and matching on it there is correct.
    identifies_by: tuple[str, ...] = ()
    #: How one of its machines is named in a sentence, over `identifies_by`.
    in_words: str = ""
    #: The same, short enough to sit inside a longer sentence. Two forms rather
    #: than one because two of this tool's refusals name a machine two different
    #: ways — the loader's `instance 'comfy-win' in us-central1-a (proj)` and the
    #: move check's `comfy-win in us-central1-a` — and troubleshooting.md quotes
    #: both verbatim, so neither wording is ours to change in passing. Falls back
    #: to `in_words` when a kind supplies only one.
    in_brief: str = ""

    @property
    def fields(self) -> tuple[str, ...]:
        """Every field this kind's entries may carry, beyond the universal ones."""
        return (*self.requires, *self.accepts)


# Fields every entry may carry whatever its kind. `os` and `gpu` are here rather
# than on `gce` because they are not Google's: `osfamily` reads `os` for the
# host's whole life and `stamp` compares against it, and a kind that only ever
# runs one operating system still has one.
_UNIVERSAL_FIELDS = ("kind", "port", "os", "gpu")

# The kinds that exist. Two, and the registry is the point rather than the count:
# `local` and `gce` are the only entries today and nothing here is shaped around
# there being two.
#
# Insertion order is the order the "kind must be ..." refusal names them, so
# `local` — the one every example and the starter host list teaches — comes
# first.
KINDS: dict[str, MachineKind] = {
    "local": MachineKind(name="local", remote=False),
    "gce": MachineKind(
        name="gce",
        remote=True,
        requires=("os", "gpu", "gce_instance", "gce_zone", "gce_project"),
        # The reservation holding this box's capacity, when it has one. It is
        # something the box HAS, not part of what it IS: `identifies_by` below
        # does not name it, so a reserved box and the same box with the line
        # taken out are one machine.
        accepts=("gce_reservation",),
        identifies_by=("gce_project", "gce_zone", "gce_instance"),
        in_words="instance {gce_instance!r} in {gce_zone} ({gce_project})",
        in_brief="{gce_instance} in {gce_zone}",
    ),
}


@dataclass(frozen=True)
class Host:
    """One machine: this computer, or one reached through a tunnel.

    `port` is where ComfyUI is reached *on this machine*: for a local host that
    is the port it actually serves on; for a remote host it is the near end of
    the SSH tunnel. Keeping them in one field is what lets `list` and `stamp`
    treat both kinds identically.
    """

    name: str
    kind: Kind
    port: int
    os: str | None = None
    gpu: str | None = None
    gce_instance: str | None = None
    gce_zone: str | None = None
    gce_project: str | None = None
    #: Values for the fields a kind declares that have no attribute here — which
    #: is everything a provider other than Google needs.
    #:
    #: Pairs rather than a mapping because `Host` is frozen, and a frozen
    #: dataclass with a `dict` field is unhashable. Read it with `declared`.
    #:
    #: The alternative was a named field per provider, which is how `gce_*` came
    #: to be read directly in three hundred places; adding `runpod_*` beside them
    #: would double that rather than stop it.
    extra: tuple[tuple[str, str], ...] = ()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def is_remote(self) -> bool:
        """Is ComfyUI on another machine, reached through a tunnel from here.

        The kind answers this. It used to be `self.kind == "gce"`, which made
        every machine of any other provider local — so `up`, `open`, `down`,
        `stamp` and tunnelling all declined to act on one — and, because the
        same-machine rule skips what is not remote, hid the collapse described on
        `MachineKind`.
        """
        kind = KINDS.get(self.kind)
        return bool(kind and kind.remote)

    def declared(self, field: str) -> str | None:
        """The value this entry gives for one field, wherever it is held."""
        value = getattr(self, field, None)
        if value is not None:
            return value
        return dict(self.extra).get(field)

    @property
    def reservation(self) -> str | None:
        """The reservation holding this box's capacity, or None if it has none.

        A reserved box bills every hour, running or stopped, until it is
        deleted — so this is the field every sentence about stopping the bill
        has to read before it says `comfy-qat down`.
        """
        return self.declared("gce_reservation")

    @property
    def machine_id(self) -> tuple[str, ...] | None:
        """What names the ONE physical machine this entry points at.

        `None` means this entry does not name a machine. **It does not mean it
        names the same machine as every other entry that does not** — which is
        what the old key claimed, by answering `("", "", "")` for all of them.
        Callers compare identities and an unknown identity matches nothing, not
        even another unknown one.

        The kind is part of it. Two providers both calling a machine `box-1` is
        ordinary, neither knows about the other, and an identity made only of the
        provider's own name for the machine would make those one machine.
        """
        return machine_identity(self.kind, self.declared)

    @property
    def machine_in_words(self) -> str:
        """The machine this entry points at, named the way a person would.

        Empty when there is no machine to name, so a caller can fall back rather
        than print a sentence about nothing.
        """
        return say_machine(self.kind, self.declared)

    @property
    def machine_in_brief(self) -> str:
        """The same machine, short enough to sit inside another sentence."""
        return say_machine(self.kind, self.declared, brief=True)


# The fields `Host` holds itself. A field a kind declares that is not one of
# these goes into `extra`, so the two can never both hold one field's value and
# disagree.
_HOST_ATTRIBUTES = frozenset(field.name for field in fields(Host))


# --- identity, for a machine that has no `Host` yet --------------------------
#
# `Host.machine_id` is the ordinary way in and these two functions are what it
# is. They exist separately because one caller asks about machines that do not
# exist as entries yet: `relocate.would_not_load` checks the host list a move is
# ABOUT to write, two of whose entries have no `Host` behind them, and it used to
# answer that question with a local copy of Google's three fields collapsed to
# `("", "", "")`. A second copy of an identity is a second thing to forget: that
# helper's own docstring claimed it "refuses exactly what `config` would refuse,
# and no more", and the claim quietly stopped being true the moment the loader's
# key changed.
#
# `declared` is any `field -> value | None` callable, so a `Host` passes
# `self.declared` and a caller holding loose values passes `some_dict.get`.
#
# Named `machine_identity` rather than `identify` deliberately: `tunnel.py` has
# its own local `identify`, which answers "what does this process look like".
# Two functions called `identify` in one tool, one about a machine and one about
# a pid, is a name waiting to be imported into the wrong module.


def machine_identity(kind: str, declared) -> tuple[str, ...] | None:
    """The identity of a machine of `kind` whose identifying fields hold these.

    `None` means these values do not name a machine, which is never the same
    claim as two of them naming the same one. See `Host.machine_id`.
    """
    spec = KINDS.get(kind)
    if spec is None or not spec.identifies_by:
        return None
    values = [declared(field) for field in spec.identifies_by]
    if not all(values):
        return None
    return (kind, *values)


def say_machine(kind: str, declared, *, brief: bool = False) -> str:
    """That machine, in words. Empty when there is no machine to name.

    `brief` is the form that sits inside a longer sentence — `comfy-win in
    us-central1-a` rather than `instance 'comfy-win' in us-central1-a (proj)`.
    Both are the kind's to supply, and both exist because this tool has two
    documented refusals that name a machine two different ways and neither
    wording is ours to change unilaterally: troubleshooting.md quotes them.
    """
    spec = KINDS.get(kind)
    if spec is None or machine_identity(kind, declared) is None:
        return ""
    values = {field: declared(field) for field in spec.identifies_by}
    template = (spec.in_brief or spec.in_words) if brief else spec.in_words
    if template:
        return template.format(**values)
    return ", ".join(f"{field} {value!r}" for field, value in values.items())


def _known_fields() -> frozenset[str]:
    """Every field any entry may carry, across every registered kind.

    Deliberately the union and not the entry's own kind's: the unknown-field
    check runs BEFORE `kind` is validated, because a typo'd field is the cause of
    every error underneath it — including `kind` itself being missing.
    """
    return frozenset({
        *_UNIVERSAL_FIELDS,
        *(field for kind in KINDS.values() for field in kind.fields),
    })

# The three fields that say which cloud box an entry is. They are what `up`,
# `open`, `down` and `move` operate on, so an entry carrying them is a machine
# that costs money whatever its `kind` says. The fourth says its capacity is
# reserved, which costs money whether or not the machine is even running.
_CLOUD_FIELDS = ("gce_instance", "gce_zone", "gce_project", "gce_reservation")

# How a host list says a box has no card. `discover` writes it for an instance
# with no accelerator and `create --gpu none` writes it for one made that way.
NO_GPU_WORD = "none"


def has_gpu(host) -> bool:
    """Does this box have a GPU, as far as its host list entry says?

    THE PREDICATE, because `"none"` is a non-empty string. `if host.gpu:` reads
    a box declared to have no card as a box that has one, and each place that
    asked it that way was its own defect: fifteen minutes waiting for
    `nvidia-smi` on a machine with no NVIDIA hardware, a CUDA torch
    force-installed on every `go`, and a box with no card counted as one card
    against the project's GPU ceiling.

    False for no declaration at all (`None`, `""`) and for `none` in any case.
    Takes a `Host`, or the bare `gpu` word for a caller holding a record that is
    not a host yet, or nothing.
    """
    return _declared_gpu(host) not in ("", NO_GPU_WORD)


def declares_no_gpu(host) -> bool:
    """Does the entry SAY the box has no GPU — the word `none`, in any case?

    Not `not has_gpu(host)`. That is also True for an entry that says nothing
    about its card, which is every local machine and any machine of a kind whose
    entries need not name one — and such a machine may have a card. So "is
    there a card to wait for" asks `has_gpu`, and anything that would actively
    put a machine on its CPU asks this: launching ComfyUI with `--cpu` and
    installing the CPU build of torch are things to do because a box was
    declared to have no card, never because nobody wrote one down.
    """
    return _declared_gpu(host) == NO_GPU_WORD


def _declared_gpu(host) -> str:
    """The `gpu` word, lowered and trimmed. Empty for none given or no host."""
    declared = host if isinstance(host, str) else getattr(host, "gpu", None)
    return (declared or "").strip().lower()

# The one name this tool reserves. The starter host list teaches it, every
# example uses it, and `comfy-qat stamp local` has exactly one obvious meaning.
LOCAL_NAME = "local"

# A host name is two things at once: an argument you type (`comfy-qat stamp <name>`)
# and part of a filename (`tunnels/<name>.pid`). Both want the same shape, and
# TOML table keys are otherwise unrestricted — `""`, `"   "`, `"--config"`,
# `"../evil"` and `"comfy\nwin"` are all valid keys and none of them is a name
# anyone can use.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _named(field: str) -> str:
    """A rejected field, with the field it was probably meant to be."""
    near = difflib.get_close_matches(field, sorted(_known_fields()), n=1, cutoff=0.6)
    return f"{field!r} (did you mean {near[0]!r}?)" if near else repr(field)


def _one_of(words: list[str]) -> str:
    """`'a'`, `'a' or 'b'`, `'a', 'b' or 'c'` — a list that reads as a sentence.

    The two-word form is byte-identical to what the refusal said when there were
    only ever two kinds, because troubleshooting.md quotes it.
    """
    quoted = [repr(word) for word in words]
    if len(quoted) < 2:
        return "".join(quoted)
    return f"{', '.join(quoted[:-1])} or {quoted[-1]}"


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
    unknown = sorted(set(raw) - _known_fields())
    if unknown:
        raise ConfigError(
            f"host {name!r}: unknown field(s) {', '.join(_named(f) for f in unknown)}. "
            f"Known fields: {', '.join(sorted(_known_fields()))}."
        )

    kind = raw.get("kind")
    if kind not in KINDS:
        raise ConfigError(
            f"host {name!r}: kind must be {_one_of(list(KINDS))}, got {kind!r}"
        )
    spec = KINDS[kind]

    if kind == "local":
        # This one costs money. `comfy-qat down` decides what to stop from `kind`
        # alone: for a local host it reports "local ComfyUI left running" and
        # returns without calling stop. A cloud box mistyped as local — or edited
        # down to one after a move — therefore reads as a successful `comfy-qat down`
        # while the GPU keeps billing all night.
        cloud = [key for key in _CLOUD_FIELDS if raw.get(key)]
        if cloud:
            raise ConfigError(
                f"host {name!r}: kind 'local' cannot carry {', '.join(cloud)}. Stopping "
                "a machine is decided from 'kind', so a cloud box declared local is "
                "never stopped and keeps billing. Set kind = \"gce\" if it is a cloud "
                "box, or delete those fields if it is not."
            )
    # Any kind that is reached through a tunnel, not `gce` alone: what makes the
    # name wrong is that the machine is somewhere else, which is true of every
    # remote kind.
    if spec.remote and name.lower() == LOCAL_NAME:
        raise ConfigError(
            f"host {name!r}: the name 'local' is reserved for the ComfyUI on this "
            "computer, which is what every example and the starter host list means by "
            "it. A cloud box wearing it puts an invisible default back. Rename the box, "
            "e.g. comfy-win or comfy-linux."
        )

    port = raw.get("port", COMFYUI_DEFAULT_PORT if not spec.remote else None)
    if port is None:
        raise ConfigError(f"host {name!r}: kind {kind!r} requires an explicit port")
    if not isinstance(port, int) or isinstance(port, bool):
        raise ConfigError(f"host {name!r}: port must be an integer, got {port!r}")
    if not 1024 <= port <= 65535:
        raise ConfigError(f"host {name!r}: port {port} is outside 1024-65535")

    if spec.remote:
        if port == COMFYUI_DEFAULT_PORT:
            raise ConfigError(
                f"host {name!r}: port {COMFYUI_DEFAULT_PORT} is reserved for the local "
                "ComfyUI. A tunnel on it would silently point you at the wrong machine — "
                "pick another port, e.g. 8190."
            )
    missing = [k for k in spec.requires if not raw.get(k)]
    # TWO SPELLINGS OF ONE SENTENCE, and the split is about the page rather than
    # about Google. troubleshooting.md quotes this error verbatim and
    # tests/test_docs.py matches the page's quotation against the LITERAL text of
    # the raise that builds it — the run it recognises is `kind 'gce' requires `.
    # Interpolating the kind into one message leaves `host `, `: kind ` and
    # ` requires `, none of them long enough to identify anything, so the entry
    # silently stops being attached to the error it documents while the sentence
    # on screen does not change by one byte.
    #
    # So `gce` keeps the spelling the page quotes, and every other kind gets the
    # same sentence with its own name in it, which is true rather than
    # approximately true. The second raise is declared in
    # `TOO_SHORT_TO_IDENTIFY` with that reasoning. A second kind with required
    # fields gets this message and its own entry written together, and then
    # these two collapse back into one.
    if missing and kind == "gce":
        raise ConfigError(
            f"host {name!r}: kind 'gce' requires {', '.join(missing)}"
        )
    if missing:
        raise ConfigError(
            f"host {name!r}: kind {kind!r} requires {', '.join(missing)}"
        )

    _check_os_is_not_a_typo(name, raw.get("os"))

    # The kind's own fields that `Host` has no attribute for. TOML holds
    # integers, booleans and dates too, and these values reach a filename, a URL
    # and a refusal — so they are made text here rather than wherever one of
    # those happens to be built. Required-and-empty is already refused above,
    # by the same truthiness check `gce_*` has always used.
    #
    # NOT a type refusal, and only because there is nowhere to document one:
    # every message raised here has to carry a verbatim entry in
    # troubleshooting.md (tests/test_docs.py walks for them), and that page is
    # not this change's to edit. A typed refusal and its entry are worth having
    # together.
    extra = [
        (field, str(raw[field]))
        for field in spec.fields
        if field not in _HOST_ATTRIBUTES and raw.get(field) is not None
    ]

    return Host(
        name=name,
        kind=kind,
        port=port,
        os=raw.get("os"),
        gpu=raw.get("gpu"),
        gce_instance=raw.get("gce_instance"),
        gce_zone=raw.get("gce_zone"),
        gce_project=raw.get("gce_project"),
        extra=tuple(sorted(extra)),
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
        # Refusing is deliberate and troubleshooting.md says why: a tool that
        # silently operates nothing is worse than one that stops. What was
        # missing is the way out — every other refusal in this file names one,
        # and this was four words with nothing to do about them.
        #
        # THE ADVICE HERE MUST BE SAFE IN BOTH CONTEXTS, and an earlier version
        # of it was not. `parse` validates two different things: a real file
        # being loaded, and a CANDIDATE REWRITE that `hostfile.apply` is about
        # to write. It cannot tell them apart, and it is quoted verbatim into
        # the rewrite's refusal.
        #
        # So `init --force` — which was here — reached the user at the one
        # moment it was destructive. `delete` removing the last cloud host
        # produces a candidate this function rejects, `apply` refuses and
        # re-raises this text, and remove.py prints it immediately above its own
        # "take the table out by hand". Two remedies, adjacent, disagreeing, the
        # overwriting one first — while the file is still intact and still holds
        # the hand-written comments this module's textual rewrite exists to
        # preserve.
        #
        # Nothing here suggests overwriting anything. Naming the shape that is
        # missing is true of a file and of a rewrite alike; `init --force`
        # documents itself for whoever actually wants it.
        raise ConfigError(
            "no [hosts.<name>] tables found — the file parses, and declares no "
            "machines. Every host is a table named for it, like [hosts.local]."
        )

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
    #
    # THE KEY IS THE HOST'S IDENTITY, NOT GOOGLE'S THREE FIELDS. It used to be
    # `(gce_project or "", gce_zone or "", gce_instance or "")`, read off the
    # host here, and the `if not host.is_remote: continue` above it was the only
    # reason that never fired wrongly: every machine that is not a Google
    # instance answers `("", "", "")`, so the SECOND one of any other kind was
    # refused as the first one over again — `the same machine: instance '' in
    # None (None)`. The loop skipped exactly the hosts it would have broken, and
    # `is_remote` being `kind == "gce"` is what did the skipping, so widening one
    # without fixing the other is the whole defect.
    #
    # AN ENTRY THAT NAMES NO MACHINE IS SKIPPED, AND THAT IS NOT THE SAME CLAIM.
    # `machine_id` is `None` when this entry does not name a machine; it is
    # never a stand-in value that two of them can share. "I cannot tell which
    # machine this is" and "these two are one machine" point opposite ways, and
    # the old key said the second while meaning the first.
    boxes: dict[tuple[str, ...], str] = {}
    for host in hosts:
        box = host.machine_id
        if box is None:
            continue
        clash = boxes.get(box)
        if clash is not None:
            raise ConfigError(
                f"hosts {clash!r} and {host.name!r} are the same machine: "
                f"{host.machine_in_words}. Two "
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
#
# THE FAMILY ROWS ARE NOT WRITTEN OUT HERE. Three of these words also had to be
# recognised by `stamp.mismatch`, which was keeping its own copy of them, and the
# two copies drifted: this one knew `rhel` and `suse` and that one did not, so a
# box declared `rhel-9` was linux to `switch` and unclassifiable to the stamp.
# `osfamily` holds them now and both read the same tuple.
#
# What is NOT shared, and is the reason this table survives rather than being
# replaced: THESE ARE SELECTOR WORDS, NOT FAMILIES. A family puts every host in
# exactly one bucket; a selector has to let one host answer to several words at
# several grains, so `ubuntu` and `linux` both find an Ubuntu box and both must
# keep working. And `local` is not an operating system at all — it means "the
# machine I am sitting at" and is matched on `kind`, below. Those three rows are
# this module's own and belong to nothing else.
OS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "windows": FAMILY_WORDS[WINDOWS],
    "linux": FAMILY_WORDS[LINUX],
    "ubuntu": ("ubuntu",),
    "debian": ("debian",),
    "macos": FAMILY_WORDS[DARWIN],
    "local": FAMILY_WORDS[DARWIN],
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


# Every word that makes an `os` field recognisable, from BOTH vocabularies that
# read it rather than written out again. `osfamily.FAMILY_WORDS` is the evidence
# side — what a machine reports about itself, and what `is_windows` dispatches on
# — and `OS_KEYWORDS` above is the selector side, what a person types. A value in
# this field is read by both, so a typo is a typo against the union. Derived, so
# a family added to either one is covered here without anybody remembering to.
_OS_VOCABULARY = sorted({
    word
    for source in (OS_KEYWORDS, osfamily.FAMILY_WORDS)
    for tokens in source.values() for token in tokens for word in token.split()
})

# How close a word has to be to one of those before it is called a typo rather
# than an operating system nobody has taught this tool about. Measured against
# every value either side actually produces: 22 real ones pass, and `windwos`,
# `windos`, `wnidows`, `widnows`, `linx`, `ubunut`, `ubunutu`, `debain`, `fedroa`
# and `centso` are all caught at 0.83 or above, while `sles-15`, `cos-101-lts`,
# `opensuse-leap-15` and `freebsd-14` sit below 0.7. Words shorter than four
# letters are not near-matched at all: nothing that long can reach 0.8 against a
# two-letter token, so `os` and `mac` cannot drag an unrelated word in.
_OS_TYPO_CUTOFF = 0.8
_OS_SHORTEST_WORD = 4


def _check_os_is_not_a_typo(name: str, declared: str | None) -> None:
    """Refuse an `os` that is a misspelling of one this tool acts on.

    `kind` and `port` were validated and `os` was not, and `os` has the widest
    blast radius of the three. `osfamily.is_windows` picks between two entirely
    different command sets and answers "not Windows" for anything it does not
    recognise — correctly, since an unfamiliar cloud image is POSIX. So
    `os = "Windwos Server 2022"` is not a near miss: it is a Windows box handed
    the whole Linux command set, `cd /opt/comfyui` and `apt-get` and all. It also
    swaps the two access commands over, because `ssh` refuses a Windows box and
    `rdp` refuses everything else — so the only command that can reach it is the
    one that says it cannot. Nothing prints a word about any of it.

    An UNRECOGNISED `os` is deliberately still allowed, and that is the half that
    matters more. `discover` writes this field from Google's licence names and
    falls back to a raw tail like `sles-15`, or to `unknown`. Rejecting
    everything outside the table would let `discover` write a host list that
    `load` then refuses — and a host list this tool will not read is a machine
    nobody can stop. That is worse than the defect being fixed here, and it is
    the same reasoning `hostfile.apply` exists on.

    So the rule is narrow on purpose: a word that is NEARLY one of ours is a
    typo, a word that is nothing like any of them is an operating system we have
    not met. One consequence worth knowing rather than hiding: a string with one
    good word and one typo — `Rocky Linx 9` — passes, because the good word
    classifies it correctly and there is nothing to save it from.
    """
    words = [word for word in re.split(r"[^a-z0-9]+", (declared or "").lower()) if word]
    if not words or any(word in _OS_VOCABULARY for word in words):
        return
    for word in words:
        if len(word) < _OS_SHORTEST_WORD:
            continue
        near = difflib.get_close_matches(
            word, _OS_VOCABULARY, n=1, cutoff=_OS_TYPO_CUTOFF)
        if near:
            raise ConfigError(
                f"host {name!r}: os {declared!r} looks like a misspelling of "
                f"{near[0]!r}. The os field decides which commands this box is "
                f"sent — anything not recognised as Windows is given the Linux "
                f"command set, and 'ssh' and 'rdp' swap over with it. Fix the "
                f"spelling, or use a name this tool does not recognise at all if "
                f"the box really is something else."
            )


def describe(host: Host) -> str:
    """How a host reads in a one-line answer: what it runs, and on what card."""
    detail = ", ".join(part for part in (host.os, host.gpu) if part and part != "none")
    return detail or ("local install" if host.kind == "local" else host.kind)


def _matches_os(host: Host, keyword: str) -> bool:
    # This branch is a SECOND MECHANISM, not a shortcut, and deleting it as
    # redundant breaks the one config every new user has. The starter hosts.toml
    # declares the local install as `kind = "local"` and `port = 8188` and
    # NOTHING ELSE — no `os` field at all — so `go local` and `go macos` resolve
    # off `kind` here and never reach the word table below. The `macos` row
    # looking like it already covers this is exactly the trap.
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
