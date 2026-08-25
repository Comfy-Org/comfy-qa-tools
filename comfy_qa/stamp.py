"""What a machine actually is, in one line you can paste.

ComfyUI already knows its own build, interpreter, torch version and hardware, and
serves all of it on `/system_stats`. Almost nothing records it: a bug report says
"doesn't work on my machine" and a recorded test says nothing at all about what it
ran against. This turns that endpoint into the evidence line.

Key names match what `comfy-test` reads from the same endpoint — `comfyui_version`,
`cloud_version`, `deploy_environment` — so anything already speaking that vocabulary
can consume this without anyone negotiating a format.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field

USER_AGENT = "comfy-qa-tools/0.1 (+https://github.com/Comfy-Org/comfy-qa-tools)"
TIMEOUT = 10


class ProbeError(Exception):
    """The machine could not be asked. `fix` is what to do about it."""

    def __init__(self, message: str, fix: str | None = None) -> None:
        super().__init__(message)
        self.fix = fix


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
        parts = [self.host]
        if self.deploy_environment:
            parts.append(self.deploy_environment)
        if self.comfyui_version:
            parts.append(f"ComfyUI {self.comfyui_version}")
        if self.cloud_version:
            parts.append(f"cloud {self.cloud_version}")
        if self.os:
            parts.append(self.os)
        if self.devices:
            parts.append(" + ".join(self.devices))
        if self.pytorch_version:
            parts.append(f"torch {self.pytorch_version}")
        if self.python_version:
            parts.append(f"python {self.python_version}")
        return " · ".join(parts)

    def as_dict(self) -> dict:
        return asdict(self)


def _short_python(raw: str | None) -> str | None:
    """`3.12.13 (main, ...) [Clang]` -> `3.12.13`. The rest is noise in a report."""
    if not raw:
        return None
    return raw.split()[0] or None


def _devices(payload: dict) -> list[str]:
    """Name each accelerator, with its VRAM when the server reports it.

    This is what proves a result is hardware-specific — the difference between
    "doesn't repro" and "doesn't repro on MPS".
    """
    named = []
    for device in payload.get("devices") or []:
        name = device.get("name") or device.get("type")
        if not name:
            continue
        total = device.get("vram_total")
        if isinstance(total, (int, float)) and total > 0:
            named.append(f"{name} ({round(total / 1024**3)}GB)")
        else:
            named.append(str(name))
    return named


def parse(payload: dict, *, host: str, url: str) -> Stamp:
    """Turn a /system_stats body into a Stamp. Tolerant of missing fields."""
    system = payload.get("system") or {}
    return Stamp(
        host=host,
        url=url,
        os=system.get("os"),
        devices=_devices(payload),
        comfyui_version=system.get("comfyui_version"),
        python_version=_short_python(system.get("python_version")),
        pytorch_version=system.get("pytorch_version"),
        # Present on cloud deployments, absent locally. Both are fine.
        cloud_version=system.get("cloud_version") or payload.get("cloud_version"),
        deploy_environment=(
            system.get("deploy_environment") or payload.get("deploy_environment")
        ),
    )


def fetch(url: str, *, host: str, opener=urllib.request.urlopen) -> Stamp:
    """Ask a running ComfyUI what it is."""
    request = urllib.request.Request(
        f"{url.rstrip('/')}/system_stats", headers={"User-Agent": USER_AGENT},
    )
    try:
        with opener(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise ProbeError(
            f"nothing answered at {url}",
            fix=f"start ComfyUI on that machine, or check the port in your host list",
        ) from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProbeError(
            f"{url} answered, but not with ComfyUI's /system_stats",
            fix="check the port — something else may be listening on it",
        ) from exc

    if not isinstance(payload, dict):
        raise ProbeError(f"{url} answered, but not with ComfyUI's /system_stats")

    return parse(payload, host=host, url=url)
