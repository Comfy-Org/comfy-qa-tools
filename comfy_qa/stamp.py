"""What a machine actually is, in one line you can paste.

ComfyUI already knows its own build, interpreter, torch version and hardware, and
serves all of it on `/system_stats`. Almost nothing records it: a bug report says
"doesn't work on my machine" and a recorded test says nothing at all about what it
ran against. This turns that endpoint into the evidence line.

Key names match what `comfy-test` reads from the same endpoint — `comfyui_version`,
`cloud_version`, `deploy_environment` — so anything already speaking that vocabulary
can consume this without anyone negotiating a format.

A stamp is pasted into a report and believed, which is what makes both of the
rules below non-negotiable.

**The endpoint is not a contract.** Across the machines this tool is pointed at,
the same field arrives as a string, as `null`, as an empty string, or not at all:
Comfy Cloud reports `os`, `python_version` and `pytorch_version` as `""`; a
ComfyUI old enough has no `comfyui_version` at all. A wrong field is worse than a
missing one, so **a field we cannot read is absent — from the line and from the
JSON alike — never guessed, and never a placeholder.**

**The answerer is a stranger.** A port is answered by whoever holds it, and on
the near end of a tunnel that need not be the machine you named. So **nothing the
answer says is trusted further than it can be checked**:

  * a JSON body is not proof of ComfyUI — the payload has to carry enough of
    `/system_stats`'s own vocabulary, or the probe fails rather than stamping a
    health endpoint as a machine;
  * a redirect is refused — anything on that port can answer 302 and send the
    probe to the ComfyUI on 8188, which would record this Mac under a cloud box's
    name;
  * every value is text chosen by the machine that answered, so it is cleaned and
    bounded before it reaches a line someone pastes;
  * the body is capped — a wrong service can stream forever, and the timeout
    covers each read, not the total;
  * no shape of answer produces a traceback.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field

from .osfamily import family

USER_AGENT = "comfy-qa-tools/0.1 (+https://github.com/Comfy-Org/comfy-qa-tools)"
TIMEOUT = 10

# ComfyUI serves the endpoint bare; Comfy Cloud serves it only under `/api`, and
# answers the bare path with the frontend's HTML shell and a 200. Trying both is
# the difference between stamping a cloud deployment and being told, wrongly,
# that something else is listening on the port.
PATHS = ("/system_stats", "/api/system_stats")

# /system_stats is a few hundred bytes. Anything past this is not that endpoint,
# and a wrong service can stream for as long as it likes: the timeout covers each
# read, not the total.
MAX_BODY = 1 << 20

# The separator is structure, so no value may contain it — otherwise a field can
# forge extra segments and a line reads as facts the machine never claimed.
SEPARATOR = " · "

_FIELD_LIMIT = 64
_LINE_LIMIT = 300
_MAX_DEVICES = 8
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Enough of ComfyUI's own vocabulary that a health endpoint cannot pass for it.
# Every field the parser reads is optional, so without this check `{"ok": true}`
# stamps cleanly as the bare host name.
_SYSTEM_STATS_KEYS = (
    "comfyui_version", "python_version", "pytorch_version", "os", "ram_total",
    "argv", "embedded_python", "required_frontend_version",
)

# ComfyUI formats a CUDA device as `"{device} {name} : {allocator_backend}"`.
# The allocator backend is a torch build detail, not machine identity, and when
# it is empty the name ends in a dangling `" : "`. Neither belongs in a line
# somebody has to read at a glance.
_ALLOCATOR_SUFFIX = re.compile(r"\s:\s*\w*\Z")

# `cuda:0 NVIDIA L4` -> `NVIDIA L4`, used only to recognise that the four cards
# in a multi-GPU box are the same card. The ordinal is kept whenever a device
# is reported on its own.
_ORDINAL_PREFIX = re.compile(r"\A[a-z]+:\d+\s+")

# A VRAM figure the server already humanised, e.g. `"22.0 GB"`. Matched strictly,
# because the whole point is to tell a size the server wrote out from a string
# that is not a size at all.
_HUMAN_VRAM = re.compile(r"\A\d+(?:[.,]\d+)?\s*(?:[KMGTP]i?B|bytes?)\Z", re.IGNORECASE)


class ProbeError(Exception):
    """The machine could not be asked. `fix` is what to do about it."""

    def __init__(self, message: str, fix: str | None = None) -> None:
        super().__init__(message)
        self.fix = fix


def _clean(value: object, cap: int = _FIELD_LIMIT) -> str | None:
    """One field, as text safe to print, paste and read back — or `None`.

    Two jobs, and both are load-bearing.

    Unknown is unknown: `""`, `"   "`, `null` and a missing key all mean the same
    thing to a reader, and all come back `None`. Booleans are rejected outright —
    `True` is not a version string, and `isinstance(True, int)` would otherwise
    let it through as `"True"`. So is anything that is not scalar: a list or a
    table is not a field we can read, and `"['3.12.13']"` would be an invention
    rather than a fact.

    And the text is the answering machine's, not ours: control characters would
    move a terminal cursor, newlines would split the evidence line in two, the
    separator would forge a field, and an unbounded value would bury the report.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        value = str(value)
    elif not isinstance(value, str):
        return None
    text = _CONTROL.sub(" ", value).replace(SEPARATOR.strip(), " ")
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _mapping(value: object) -> dict:
    """`system` is supposed to be an object. Sometimes it is not."""
    return value if isinstance(value, dict) else {}


def _short_python(raw: object) -> str | None:
    """`3.12.13 (main, ...) [Clang]` -> `3.12.13`. The rest is noise in a report."""
    text = _clean(raw)
    if not text:
        return None
    return text.split()[0] or None


def _gigabytes(total: float) -> str | None:
    """Bytes as GB, or `None` when there is nothing honest to say.

    Rounding a real 256 MB device to `0GB` reads as "no VRAM", which is a
    different and wrong claim, so small devices keep their decimals.
    """
    if total <= 0:
        return None
    gb = total / 1024**3
    if gb >= 10:
        return f"{round(gb)}GB"
    if gb >= 1:
        return f"{gb:.1f}GB".replace(".0GB", "GB")
    return f"{gb:.2f}GB"


def _vram(total: object) -> str | None:
    """VRAM as the server reported it: raw bytes, or already humanised.

    A string that is a size is repeated verbatim, because re-deriving a number
    from `"22.0 GB"` would only be guessing at the units. A string that is not a
    size — `"lots"`, or whatever else a stranger on the port decides to send — is
    not a fact about any hardware, so the device is named without one.
    """
    if isinstance(total, bool):
        return None
    if isinstance(total, (int, float)):
        return _gigabytes(total)
    text = _clean(total, cap=24)
    if text is None:
        return None
    try:
        return _gigabytes(float(text))
    except ValueError:
        pass
    return text if _HUMAN_VRAM.match(text) else None


def _devices(payload: dict) -> list[str]:
    """Name each accelerator, with its VRAM when the server reports it.

    This is what proves a result is hardware-specific — the difference between
    "doesn't repro" and "doesn't repro on MPS". One entry per device, in the
    order ComfyUI reported them (primary first); `line()` is what collapses a
    row of identical cards.

    Shapes other than a list of tables happen: this is whatever answered on the
    port, not a schema. The count is capped because how many devices come back
    is the answerer's choice too.
    """
    entries = payload.get("devices")
    if not isinstance(entries, list):
        return []

    named = []
    for device in entries[:_MAX_DEVICES]:
        if not isinstance(device, dict):
            continue
        name = _clean(device.get("name")) or _clean(device.get("type"))
        if not name:
            continue
        name = _ALLOCATOR_SUFFIX.sub("", name).strip()
        if not name:
            continue
        vram = _vram(device.get("vram_total"))
        named.append(f"{name} ({vram})" if vram else name)
    return named


def _summarise(devices: list[str]) -> list[str]:
    """Collapse a run of identical cards. Eight GPUs are not eight facts.

    Four L4s otherwise spend 180 characters saying the same thing four times,
    and the line stops being something anyone pastes. A lone device keeps its
    ordinal, because with one card `cuda:0` is the whole of what is known.
    """
    groups: list[list] = []
    for label in devices:
        label = str(label)
        key = _ORDINAL_PREFIX.sub("", label)
        if groups and groups[-1][0] == key:
            groups[-1][1] += 1
        else:
            groups.append([key, 1, label])
    return [f"{count} x {key}" if count > 1 else first
            for key, count, first in groups]


@dataclass
class Stamp:
    """One machine, as it described itself.

    Every optional field is `None` when the server did not tell us, which is a
    different thing from telling us it is empty. Both arrive as `None` here,
    because for a reader of the evidence line they mean the same thing: unknown.
    """

    host: str
    url: str
    os: str | None = None
    devices: list[str] = field(default_factory=list)
    comfyui_version: str | None = None
    python_version: str | None = None
    pytorch_version: str | None = None
    cloud_version: str | None = None
    deploy_environment: str | None = None

    def line(self) -> str:
        """The paste-ready evidence line.

        Ordered by what a reader needs first: which machine, then what it runs,
        then the details that explain a hardware-specific result.
        """
        # The host name comes out of a config file and the url from a caller, so
        # neither is guaranteed to be printable text — hence `_clean`. But a bare
        # `"?"` when it cleans away is worse than the problem it solves: the first
        # segment is the only thing telling a reader which machine the rest of the
        # line is about, and a stamp that cannot name where it came from is not
        # evidence. Fall back to the url, which at least says where the answer was
        # fetched from; `"?"` is the last resort when even that is unreadable.
        parts = [_clean(self.host) or _clean(self.url, cap=_LINE_LIMIT) or "?"]
        if self.deploy_environment:
            parts.append(self.deploy_environment)
        if self.comfyui_version:
            parts.append(f"ComfyUI {self.comfyui_version}")
        if self.cloud_version:
            parts.append(f"cloud {self.cloud_version}")
        if self.os:
            parts.append(self.os)
        # Summarised *and* bounded: identical cards collapse to one fact, and
        # whatever survives that is still capped, because how many devices get
        # reported is the answering machine's choice.
        summary = _summarise(self.devices)[:_MAX_DEVICES]
        if summary:
            parts.append(" + ".join(summary))
        if self.pytorch_version:
            parts.append(f"torch {self.pytorch_version}")
        if self.python_version:
            parts.append(f"python {self.python_version}")
        line = SEPARATOR.join(str(part) for part in parts)
        return line if len(line) <= _LINE_LIMIT else line[: _LINE_LIMIT - 1] + "…"

    def as_dict(self) -> dict:
        """The same capture, machine-readable, under ComfyUI's own key names.

        Unknown fields are omitted rather than emitted as `null`, so the JSON
        and the line agree on exactly which facts this stamp is claiming.
        """
        return {
            key: value
            for key, value in asdict(self).items()
            if value is not None and value != []
        }


def looks_like_comfyui(payload: object) -> bool:
    """Is this ComfyUI's `/system_stats`, or just something that speaks JSON?

    Every field the parser reads is optional, so without this a health endpoint
    on the port stamps cleanly as the bare host name — a line that looks like a
    successful probe and says nothing true about any machine.
    """
    if not isinstance(payload, dict):
        return False
    system = payload.get("system")
    if not isinstance(system, dict):
        return False
    return any(key in system for key in _SYSTEM_STATS_KEYS)


def parse(payload: dict, *, host: str, url: str) -> Stamp:
    """Turn a /system_stats body into a Stamp. Tolerant of missing fields."""
    top = payload if isinstance(payload, dict) else {}
    system = _mapping(top.get("system"))
    return Stamp(
        host=host,
        url=url,
        os=_clean(system.get("os")),
        devices=_devices(top),
        comfyui_version=_clean(system.get("comfyui_version")),
        python_version=_short_python(system.get("python_version")),
        pytorch_version=_clean(system.get("pytorch_version")),
        # Present on cloud deployments, absent locally. Both are fine.
        cloud_version=(
            _clean(system.get("cloud_version")) or _clean(top.get("cloud_version"))
        ),
        deploy_environment=(
            _clean(system.get("deploy_environment"))
            or _clean(top.get("deploy_environment"))
        ),
    )


def _where(url: str) -> str:
    """The machine half of a URL. Which path answered does not matter; who did."""
    parsed = urllib.parse.urlsplit(url)
    return parsed.netloc or url


def fetch(url: str, *, host: str, opener=urllib.request.urlopen,
          timeout: float = TIMEOUT) -> Stamp:
    """Ask a running ComfyUI what it is.

    Tries the bare path first, then the `/api` alias, because a Comfy Cloud
    deployment answers the bare path with its HTML shell and a 200 — which is
    indistinguishable from "wrong port" unless you go on to ask properly.

    Everything the far end sends is checked before it is believed: it must not
    have redirected somewhere else, it must be small enough to be that endpoint,
    and it must speak `/system_stats`'s vocabulary rather than merely JSON.

    `timeout` exists for the one caller that is not stamping. `TIMEOUT` is ten
    seconds because a stamp is the point of the command that asked for it and
    waiting is better than failing; `list` asks the same question in passing,
    about a column, and a wedged ComfyUI — accepting the connection and never
    answering — would hold up a read command for ten seconds per host. Shortening
    it there is a different trade, not a different probe, so it is a parameter
    rather than a second function that would drift from this one.
    """
    base = url.rstrip("/")
    http_error: urllib.error.HTTPError | None = None

    for path in PATHS:
        asked = base + path
        try:
            request = urllib.request.Request(asked, headers={"User-Agent": USER_AGENT})
        except ValueError as exc:
            # A malformed url fails identically on both paths; nothing to retry.
            raise ProbeError(
                f"{url!r} is not a URL this can ask: {exc}",
                fix="give the host a scheme and a port, as in http://127.0.0.1:8188",
            ) from exc

        try:
            response = opener(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            # Something is definitely there — it just refused this path.
            http_error = exc
            continue
        except (OSError, http.client.HTTPException) as exc:
            # URLError, bare socket failures, and a connection closed before any
            # response at all: nothing usable is listening, and the `/api` alias
            # will not change that.
            raise ProbeError(
                f"nothing answered at {url}",
                fix="start ComfyUI on that machine, or check the port in your host list",
            ) from exc

        try:
            with response:
                # Where the answer actually came from — which urllib will have
                # followed a redirect to reach, without saying so.
                answered = getattr(response, "geturl", lambda: asked)()
                body = response.read(MAX_BODY + 1)
        except (OSError, http.client.HTTPException) as exc:
            # A read timeout is not a URLError, and a tunnel dropping mid-answer
            # is routine. Neither is a traceback.
            raise ProbeError(
                f"{url} stopped answering part-way through: {exc}",
                fix="check the tunnel is still open, then try again",
            ) from exc

        # Both of these say the far end is not the machine that was asked — not
        # that this was the wrong path — so they stop the probe rather than
        # falling through to the alias.
        if _where(answered) != _where(asked):
            raise ProbeError(
                f"{url} redirected to {_where(answered)} — that is a different machine, "
                "so anything it says would be recorded under the wrong name.",
                fix="check the port — something else may be listening on it",
            )

        if len(body) > MAX_BODY:
            raise ProbeError(
                f"{url} answered with more than {MAX_BODY // 1024}KB, which /system_stats "
                "never does",
                fix="check the port — something else may be listening on it",
            )

        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue  # answered, but not with JSON — try the other path

        if looks_like_comfyui(payload):
            return parse(payload, host=host, url=url)
        # JSON, but not this endpoint's vocabulary. The alias may still be real.

    if http_error is not None:
        code = http_error.code
        if code in (401, 403):
            raise ProbeError(
                f"{url} refused the request ({code})",
                fix="this machine wants credentials — a stamp needs an endpoint "
                    "you can reach unauthenticated",
            )
        raise ProbeError(
            f"{url} answered {code} for /system_stats",
            fix="check the port — this looks like a web server, but not a ComfyUI",
        )

    raise ProbeError(
        f"{url} answered, but not with ComfyUI's /system_stats. Something else is "
        "on that port, and a stamp from it would name the wrong machine.",
        fix="check the port — something else may be listening on it",
    )


# Which OS family a string names lives in `osfamily`, because five places in this
# package used to ask that and four of them answered it differently. The table
# and the whole of the reasoning that shaped it — why `nt` is not in it, why
# `posix` cannot be — moved there with it, unchanged.
#
# What matters at THIS call site is the `None` return, and it is the reason this
# half does not have the bug the card half had. The two sides are written by
# different systems: `discover.operating_system()` writes `Windows Server 2022`
# or falls back to a raw GCE licence name like `sles-15`, or to `unknown`;
# ComfyUI reports `sys.platform` on newer builds (`win32`, `linux`, `darwin`) and
# `os.name` on older ones (`nt`, `posix`), and Comfy Cloud sends `""`. An
# unrecognised string comes back `None` and the comparison is SKIPPED, rather
# than being read as a different family. Checked against every value both sides
# actually produce: no pairing of a machine with its own declaration is flagged.
#
# The cost of that is a gap rather than a false alarm: `nt` and `posix` name no
# family, so a stamp from an older ComfyUI is never compared at all. That is the
# right way round while the caller refuses on a complaint.


_TOKEN = re.compile(r"[a-z0-9]+")

# Words that turn up in a declaration or in a device name but not in both, so
# their absence proves nothing. `comfy-qat discover` writes the declaration from
# Google's own acceleratorType — `nvidia-h100-mega-80gb` becomes `H100-MEGA-80GB`
# — and ComfyUI calls that same card `NVIDIA H100 80GB HBM3`. Neither vocabulary
# is wrong; they simply do not overlap on these words.
_CARD_NOISE = frozenset({
    "nvidia", "tesla", "geforce", "rtx", "gtx", "graphics",
    "mega", "vws", "sxm", "sxm4", "sxm5", "pcie", "nvl",
    "hbm", "hbm2", "hbm2e", "hbm3", "hbm3e",
})

# A host that declares no card, or declares it has none, has nothing to compare.
# `config` already treats those two the same way when selecting a host by card.
_NO_CARD = frozenset({"", "none"})


def _card_tokens(text: str | None) -> list[str]:
    return [t for t in _TOKEN.findall((text or "").lower()) if t not in _CARD_NOISE]


def _gpu_contradicts(declared: str | None, device_names: list[str]) -> bool:
    """Can none of the cards that answered be the card that was declared?

    Compared as whole tokens rather than as a substring. The substring test asked
    whether the declared string appeared inside a device name, and the two names
    for one card do not contain each other: `A100-80GB` is not inside
    `NVIDIA A100-SXM4-80GB`, so a machine that *was* the declared card
    contradicted itself. That was noise while this only decorated a line. It
    blocks the stamp now, which means it costs a tester the command outright — on
    a string `comfy-qat discover` wrote and they never typed.

    Whole tokens fix the error running the other way too: `"l4" in "nvidia l40s"`
    was true, so an L40S passed as an L4.

    A declaration is contradicted only when no answering card carries all of its
    tokens. `A100` therefore accepts `A100-SXM4-80GB`: a narrower declaration
    matching a fuller name is one card described in more detail, which is the
    reading `config` already takes — "`a100` finds an `A100-80GB`, because nobody
    types the full SKU". Being wrong about 40GB versus 80GB is worth noticing and
    is not "that port is not reaching this machine", which is the only thing this
    refusal is entitled to claim.
    """
    if (declared or "").strip().lower() in _NO_CARD:
        return False
    wanted = _card_tokens(declared)
    if not wanted:
        return False
    return not any(
        all(token in set(_card_tokens(name)) for token in wanted)
        for name in device_names
    )


def mismatch(host, stamp: Stamp) -> str | None:
    """Does the machine that answered contradict the machine you declared?

    The host list says what a box is; the stamp says what answered on its port.
    When those disagree the port is not reaching the box you named — which is the
    wrong-machine failure, arriving as a line that otherwise looks like evidence.
    Returns None when there is nothing to compare, because `os` and `gpu` are
    optional for a local host.

    Both halves fail open, deliberately. `comfy-qat stamp` refuses to print an
    evidence line when this returns a complaint, so a false positive is not a
    warning someone can read past — it is the tool declining to describe a
    machine that is fine, over a declaration `comfy-qat discover` wrote rather than
    anyone typed. Anything this cannot be sure of has to pass.
    """
    declared, answering = family(getattr(host, "os", None)), family(stamp.os)
    if declared and answering and declared != answering:
        return (
            f"{host.name} is declared as {host.os}, but {stamp.url} answered as "
            f"{stamp.os}. That port is not reaching {host.name}."
        )

    accelerators = [d for d in stamp.devices if not d.lower().startswith("cpu")]
    if accelerators and _gpu_contradicts(getattr(host, "gpu", None), accelerators):
        return (
            f"{host.name} is declared with a {host.gpu}, but {stamp.url} answered with "
            f"{', '.join(accelerators)}. That port is not reaching {host.name}."
        )
    return None
