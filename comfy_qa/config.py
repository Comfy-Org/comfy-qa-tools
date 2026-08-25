"""hosts.toml — the declared list of machines this tool operates.

Nothing here talks to the network. Loading and validating the host list is
deliberately offline and total: every rule that can be checked without gcloud is
checked here, so a bad config fails in milliseconds rather than half-way through
starting a GPU instance.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# ComfyUI's own default. The primary local install on this machine holds it, and
# forwarding a remote onto it is the single mistake this tool exists to prevent,
# so no remote host may ever claim it.
COMFYUI_DEFAULT_PORT = 8188

Kind = Literal["local", "gce"]

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "comfy-qa" / "hosts.toml"


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


def _parse_host(name: str, raw: object) -> Host:
    if not isinstance(raw, dict):
        raise ConfigError(f"host {name!r}: expected a table, got {type(raw).__name__}")

    kind = raw.get("kind")
    if kind not in ("local", "gce"):
        raise ConfigError(
            f"host {name!r}: kind must be 'local' or 'gce', got {kind!r}"
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

    unknown = set(raw) - {"kind", "port", *_REQUIRED_FOR_GCE}
    if unknown:
        raise ConfigError(
            f"host {name!r}: unknown field(s) {', '.join(sorted(unknown))}"
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
    """Validate an already-decoded hosts.toml. Raises ConfigError on any problem."""
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

    return hosts


def load(path: Path | None = None) -> list[Host]:
    """Read and validate hosts.toml. Raises ConfigError if it is missing or bad."""
    path = path or DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(
            f"no host list at {path}. Run `comfy-qa host init` to write a starter one."
        )
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    return parse(data)


def find(hosts: list[Host], name: str) -> Host:
    """Look a host up by name, with a message that lists the real options."""
    for host in hosts:
        if host.name == name:
            return host
    known = ", ".join(h.name for h in hosts) or "none declared"
    raise ConfigError(f"unknown host {name!r}. Declared: {known}")
