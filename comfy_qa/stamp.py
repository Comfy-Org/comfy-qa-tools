"""What a machine actually is, in one line you can paste.

ComfyUI already knows its own build, interpreter, torch version and hardware, and
serves all of it on `/system_stats`. Almost nothing records it: a bug report says
"doesn't work on my machine" and a recorded test says nothing at all about what it
ran against. This turns that endpoint into the evidence line.

Key names match what `comfy-test` reads from the same endpoint — `comfyui_version`,
`cloud_version`, `deploy_environment` — so anything already speaking that vocabulary
can consume this without anyone negotiating a format.

**The endpoint is not a contract.** Across the machines this tool is pointed at,
the same field arrives as a string, as `null`, as an empty string, or not at all:
Comfy Cloud reports `os`, `python_version` and `pytorch_version` as `""`; a
ComfyUI old enough has no `comfyui_version` at all. Because this line is pasted
into bug reports as evidence, a wrong field is worse than a missing one, so
everything here follows one rule: **a field we cannot read is absent — from the
line and from the JSON alike — never guessed, and never a placeholder.**
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field

USER_AGENT = "comfy-qa-tools/0.1 (+https://github.com/Comfy-Org/comfy-qa-tools)"
TIMEOUT = 10

# ComfyUI serves the endpoint bare; Comfy Cloud serves it only under `/api`, and
# answers the bare path with the frontend's HTML shell and a 200. Trying both is
# the difference between stamping a cloud deployment and being told, wrongly,
# that something else is listening on the port.
PATHS = ("/system_stats", "/api/system_stats")


class ProbeError(Exception):
    """The machine could not be asked. `fix` is what to do about it."""

    def __init__(self, message: str, fix: str | None = None) -> None:
        super().__init__(message)
        self.fix = fix


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
        parts = [self.host.strip() or self.url]
        if self.deploy_environment:
            parts.append(self.deploy_environment)
        if self.comfyui_version:
            parts.append(f"ComfyUI {self.comfyui_version}")
        if self.cloud_version:
            parts.append(f"cloud {self.cloud_version}")
        if self.os:
            parts.append(self.os)
        summary = _summarise(self.devices)
        if summary:
            parts.append(" + ".join(summary))
        if self.pytorch_version:
            parts.append(f"torch {self.pytorch_version}")
        if self.python_version:
            parts.append(f"python {self.python_version}")
        return " · ".join(parts)

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


def _text(value: object) -> str | None:
    """A field as text, or `None` when the server told us nothing usable.

    `""`, `"   "`, `null` and a missing key all mean the same thing to a reader:
    unknown. Booleans are rejected outright — `True` is not a version string,
    and `isinstance(True, int)` would otherwise let it through as `"True"`.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value.strip() or None
    return None


def _mapping(value: object) -> dict:
    """`system` is supposed to be an object. Sometimes it is not."""
    return value if isinstance(value, dict) else {}


def _short_python(raw: object) -> str | None:
    """`3.12.13 (main, ...) [Clang]` -> `3.12.13`. The rest is noise in a report."""
    text = _text(raw)
    if text is None:
        return None
    return text.split()[0]


# ComfyUI formats a CUDA device as `"{device} {name} : {allocator_backend}"`.
# The allocator backend is a torch build detail, not machine identity, and when
# it is empty the name ends in a dangling `" : "`. Neither belongs in a line
# somebody has to read at a glance.
_ALLOCATOR_SUFFIX = re.compile(r"\s:\s*\w*\Z")

# `cuda:0 NVIDIA L4` -> `NVIDIA L4`, used only to recognise that the four cards
# in a multi-GPU box are the same card. The ordinal is kept whenever a device
# is reported on its own.
_ORDINAL_PREFIX = re.compile(r"\A[a-z]+:\d+\s+")


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
    """VRAM as the server reported it: raw bytes, or already humanised."""
    if isinstance(total, bool):
        return None
    if isinstance(total, (int, float)):
        return _gigabytes(total)
    if isinstance(total, str):
        text = total.strip()
        if not text:
            return None
        try:
            return _gigabytes(float(text))
        except ValueError:
            # Already a human string like "22.0 GB". Repeat it verbatim rather
            # than trying to re-derive a number we would only be guessing at.
            return text
    return None


def _devices(payload: dict) -> list[str]:
    """Name each accelerator, with its VRAM when the server reports it.

    This is what proves a result is hardware-specific — the difference between
    "doesn't repro" and "doesn't repro on MPS". One entry per device, in the
    order ComfyUI reported them (primary first); `line()` is what collapses a
    row of identical cards.
    """
    entries = payload.get("devices")
    if not isinstance(entries, list):
        return []

    named = []
    for device in entries:
        if not isinstance(device, dict):
            continue
        name = _text(device.get("name")) or _text(device.get("type"))
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
        key = _ORDINAL_PREFIX.sub("", label)
        if groups and groups[-1][0] == key:
            groups[-1][1] += 1
        else:
            groups.append([key, 1, label])
    return [f"{count} x {key}" if count > 1 else first
            for key, count, first in groups]


def parse(payload: dict, *, host: str, url: str) -> Stamp:
    """Turn a /system_stats body into a Stamp. Tolerant of missing fields."""
    system = _mapping(payload.get("system"))
    return Stamp(
        host=host,
        url=url,
        os=_text(system.get("os")),
        devices=_devices(payload),
        comfyui_version=_text(system.get("comfyui_version")),
        python_version=_short_python(system.get("python_version")),
        pytorch_version=_text(system.get("pytorch_version")),
        # Present on cloud deployments, absent locally. Both are fine.
        cloud_version=(
            _text(system.get("cloud_version")) or _text(payload.get("cloud_version"))
        ),
        deploy_environment=(
            _text(system.get("deploy_environment"))
            or _text(payload.get("deploy_environment"))
        ),
    )


def _read(opener, url: str) -> object:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch(url: str, *, host: str, opener=urllib.request.urlopen) -> Stamp:
    """Ask a running ComfyUI what it is.

    Tries the bare path first, then the `/api` alias, because a Comfy Cloud
    deployment answers the bare path with its HTML shell and a 200 — which is
    indistinguishable from "wrong port" unless you go on to ask properly.
    """
    base = url.rstrip("/")
    http_error: urllib.error.HTTPError | None = None

    for path in PATHS:
        try:
            payload = _read(opener, base + path)
        except urllib.error.HTTPError as exc:
            # Something is definitely there — it just refused this path.
            http_error = exc
            continue
        except OSError as exc:
            # URLError and bare socket failures alike: nothing is listening.
            raise ProbeError(
                f"nothing answered at {url}",
                fix="start ComfyUI on that machine, or check the port in your host list",
            ) from exc
        except (ValueError, UnicodeDecodeError):
            continue  # answered, but not with JSON — try the other path
        if isinstance(payload, dict):
            return parse(payload, host=host, url=url)

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
        f"{url} answered, but not with ComfyUI's /system_stats",
        fix="check the port — something else may be listening on it",
    )


# Matched as substrings, so every token has to be one that cannot turn up inside
# another operating system's name: "nt" alone reads "ubuntu" as Windows.
_OS_FAMILIES = {
    "windows": ("windows", "win32", "winnt", "microsoft"),
    "linux": ("linux", "ubuntu", "debian", "centos", "rocky", "fedora"),
    "darwin": ("darwin", "macos", "mac os", "osx"),
}


def _family(text: str | None) -> str | None:
    lowered = (text or "").lower()
    for family, words in _OS_FAMILIES.items():
        if any(word in lowered for word in words):
            return family
    return None


def mismatch(host, stamp: Stamp) -> str | None:
    """Does the machine that answered contradict the machine you declared?

    The host list says what a box is; the stamp says what answered on its port.
    When those disagree the port is not reaching the box you named — which is the
    wrong-machine failure, arriving as a line that otherwise looks like evidence.
    Returns None when there is nothing to compare, because `os` and `gpu` are
    optional for a local host.
    """
    declared, answering = _family(getattr(host, "os", None)), _family(stamp.os)
    if declared and answering and declared != answering:
        return (
            f"{host.name} is declared as {host.os}, but {stamp.url} answered as "
            f"{stamp.os}. That port is not reaching {host.name}."
        )

    gpu = (getattr(host, "gpu", None) or "").strip().lower()
    accelerators = [d for d in stamp.devices if not d.lower().startswith("cpu")]
    if gpu and accelerators and not any(gpu in d.lower() for d in accelerators):
        return (
            f"{host.name} is declared with a {host.gpu}, but {stamp.url} answered with "
            f"{', '.join(accelerators)}. That port is not reaching {host.name}."
        )
    return None
