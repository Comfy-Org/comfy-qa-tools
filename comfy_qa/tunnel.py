"""SSH-less tunnels to cloud boxes, via Identity-Aware Proxy.

ComfyUI has no authentication, so a cloud box must never expose its port. IAP
forwards a local port to the instance over Google's own path: no public IP, no
firewall opening, and no SSH keys — which matters, because `gcloud compute ssh`
is unreliable against Windows images.

A tunnel outlives the command that started it, so its process id is recorded and
checked rather than assumed. The alternative — reconnecting blindly — quietly
stacks a second tunnel on the same port and you never learn which one answered.
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, Host

COMFYUI_PORT = 8188

# Beside the host list, not in /tmp: these belong to the user's configuration and
# a stale pid file must survive a reboot so it can be recognised and cleaned up.
TUNNEL_DIR = DEFAULT_CONFIG_PATH.parent / "tunnels"


@dataclass
class TunnelState:
    host: str
    pid: int | None
    alive: bool

    @property
    def running(self) -> bool:
        return self.alive and self.pid is not None


def pid_file(host: str, directory: Path | None = None) -> Path:
    return (directory or TUNNEL_DIR) / f"{host}.pid"


def _alive(pid: int) -> bool:
    """Is this process still there? Signal 0 asks without disturbing it."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but belongs to someone else — still occupying the port.
        return True
    return True


def status(host: str, directory: Path | None = None) -> TunnelState:
    path = pid_file(host, directory)
    if not path.exists():
        return TunnelState(host=host, pid=None, alive=False)
    try:
        pid = int(path.read_text().strip())
    except (ValueError, OSError):
        return TunnelState(host=host, pid=None, alive=False)
    return TunnelState(host=host, pid=pid, alive=_alive(pid))


def command(host: Host) -> list[str]:
    """The gcloud invocation. Pure, so it can be shown by --dry-run."""
    return [
        "gcloud", "compute", "start-iap-tunnel",
        host.gce_instance or host.name, str(COMFYUI_PORT),
        f"--local-host-port=localhost:{host.port}",
        f"--zone={host.gce_zone}",
        f"--project={host.gce_project}",
    ]


def open_tunnel(host: Host, directory: Path | None = None, launcher=None) -> TunnelState:
    """Start a tunnel unless one is already up. Returns its state either way."""
    existing = status(host.name, directory)
    if existing.running:
        return existing

    directory = directory or TUNNEL_DIR
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / f"{host.name}.log"

    if launcher is not None:
        pid = launcher(command(host), log)
    else:
        with log.open("ab") as handle:
            process = subprocess.Popen(
                command(host), stdout=handle, stderr=handle,
                # Detach: the tunnel has to outlive this command.
                start_new_session=True,
            )
        pid = process.pid

    pid_file(host.name, directory).write_text(str(pid))
    return TunnelState(host=host.name, pid=pid, alive=True)


def close_tunnel(host: str, directory: Path | None = None, killer=os.kill) -> bool:
    """Stop a tunnel. Returns whether there was one to stop."""
    state = status(host, directory)
    path = pid_file(host, directory)
    if state.pid is not None and state.alive:
        try:
            killer(state.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    stopped = state.running
    path.unlink(missing_ok=True)
    return stopped
