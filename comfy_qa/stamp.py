"""What a machine actually is, in one line you can paste.

ComfyUI already knows its own build, interpreter, torch version and hardware, and
serves all of it on `/system_stats`. Almost nothing records it: a bug report says
"doesn't work on my machine" and a recorded test says nothing at all about what it
ran against. This turns that endpoint into the evidence line.

Key names match what `comfy-test` reads from the same endpoint — `comfyui_version`,
`cloud_version`, `deploy_environment` — so anything already speaking that vocabulary
can consume this without anyone negotiating a format.

Everything here treats the answer as coming from a stranger, because on the near
end of a tunnel it might be. A port is answered by whoever holds it, and a stamp
is pasted into a report and believed, so:

  * a JSON body is not proof of ComfyUI — the payload has to look like
    `/system_stats`, or the probe fails rather than stamping a health endpoint as
    a machine;
  * a redirect is refused — anything on that port can answer 302 and send the
    probe to the ComfyUI on 8188, which would report this Mac under a cloud box's
    name;
  * every value is text chosen by the machine that answered, so it is cleaned
    before it reaches a line someone pastes;
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

USER_AGENT = "comfy-qa-tools/0.1 (+https://github.com/Comfy-Org/comfy-qa-tools)"
TIMEOUT = 10

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
_SYSTEM_STATS_KEYS = (
    "comfyui_version", "python_version", "pytorch_version", "os", "ram_total",
    "argv", "embedded_python", "required_frontend_version",
)


class ProbeError(Exception):
    """The machine could not be asked. `fix` is what to do about it."""

    def __init__(self, message: str, fix: str | None = None) -> None:
        super().__init__(message)
        self.fix = fix


def _clean(value: object, limit: int = _FIELD_LIMIT) -> str | None:
    """One field, as text safe to print, paste and read back.

    Control characters would move a terminal cursor, newlines would split the
    line in two, and the separator would invent a field. All three are things the
    answering machine chooses.
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, str):
        value = str(value)
    text = _CONTROL.sub(" ", value).replace(SEPARATOR.strip(), " ")
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass
class Stamp:
    """One machine, as it described itself."""

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
        parts = [_clean(self.host) or "?"]
        if self.deploy_environment:
            parts.append(self.deploy_environment)
        if self.comfyui_version:
            parts.append(f"ComfyUI {self.comfyui_version}")
        if self.cloud_version:
            parts.append(f"cloud {self.cloud_version}")
        if self.os:
            parts.append(self.os)
        if self.devices:
            parts.append(" + ".join(self.devices[:_MAX_DEVICES]))
        if self.pytorch_version:
            parts.append(f"torch {self.pytorch_version}")
        if self.python_version:
            parts.append(f"python {self.python_version}")
        line = SEPARATOR.join(str(part) for part in parts)
        return line if len(line) <= _LINE_LIMIT else line[: _LINE_LIMIT - 1] + "…"

    def as_dict(self) -> dict:
        return asdict(self)


def _short_python(raw: object) -> str | None:
    """`3.12.13 (main, ...) [Clang]` -> `3.12.13`. The rest is noise in a report."""
    text = _clean(raw)
    if not text:
        return None
    return text.split()[0] or None


def _devices(payload: dict) -> list[str]:
    """Name each accelerator, with its VRAM when the server reports it.

    This is what proves a result is hardware-specific — the difference between
    "doesn't repro" and "doesn't repro on MPS". Shapes other than a list of
    tables happen: this is whatever answered on the port, not a schema.
    """
    raw = payload.get("devices")
    if not isinstance(raw, list):
        return []
    named = []
    for device in raw[:_MAX_DEVICES]:
        if not isinstance(device, dict):
            continue
        name = _clean(device.get("name")) or _clean(device.get("type"))
        if not name:
            continue
        total = device.get("vram_total")
        if isinstance(total, (int, float)) and not isinstance(total, bool) and total > 0:
            named.append(f"{name} ({round(total / 1024**3)}GB)")
        else:
            named.append(name)
    return named


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
    system = payload.get("system") if isinstance(payload, dict) else None
    if not isinstance(system, dict):
        system = {}
    top = payload if isinstance(payload, dict) else {}
    return Stamp(
        host=host,
        url=url,
        os=_clean(system.get("os")),
        devices=_devices(top),
        comfyui_version=_clean(system.get("comfyui_version")),
        python_version=_short_python(system.get("python_version")),
        pytorch_version=_clean(system.get("pytorch_version")),
        # Present on cloud deployments, absent locally. Both are fine.
        cloud_version=_clean(system.get("cloud_version") or top.get("cloud_version")),
        deploy_environment=_clean(
            system.get("deploy_environment") or top.get("deploy_environment")
        ),
    )


def _where(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    return parsed.netloc or url


def fetch(url: str, *, host: str, opener=urllib.request.urlopen) -> Stamp:
    """Ask a running ComfyUI what it is."""
    asked = f"{url.rstrip('/')}/system_stats"
    try:
        request = urllib.request.Request(asked, headers={"User-Agent": USER_AGENT})
    except ValueError as exc:
        raise ProbeError(f"{url!r} is not a URL this can ask: {exc}") from exc

    try:
        with opener(request, timeout=TIMEOUT) as response:
            answered = getattr(response, "geturl", lambda: asked)()
            body = response.read(MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        # Something is there, it just is not ComfyUI. Saying "nothing answered"
        # here sends someone to start a server on a port that is already taken.
        raise ProbeError(
            f"{url} answered with HTTP {exc.code}, but not with ComfyUI's /system_stats",
            fix="check the port — something else may be listening on it",
        ) from exc
    except urllib.error.URLError as exc:
        raise ProbeError(
            f"nothing answered at {url}",
            fix="start ComfyUI on that machine, or check the port in your host list",
        ) from exc
    except (OSError, http.client.HTTPException) as exc:
        # A read timeout is not a URLError, and a tunnel dropping mid-answer is
        # routine. Neither is a traceback.
        raise ProbeError(
            f"{url} stopped answering part-way through: {exc}",
            fix="check the tunnel is still open, then try again",
        ) from exc

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
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProbeError(
            f"{url} answered, but not with ComfyUI's /system_stats",
            fix="check the port — something else may be listening on it",
        ) from exc

    if not looks_like_comfyui(payload):
        raise ProbeError(
            f"{url} answered, but not with ComfyUI's /system_stats. Something else is "
            "on that port, and a stamp from it would name the wrong machine.",
            fix="check the port — something else may be listening on it",
        )

    return parse(payload, host=host, url=url)


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
