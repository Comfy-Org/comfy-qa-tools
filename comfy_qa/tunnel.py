"""SSH-less tunnels to cloud boxes, via Identity-Aware Proxy.

ComfyUI has no authentication, so a cloud box must never expose its port. IAP
forwards a local port to the instance over Google's own path: no public IP, no
firewall opening, and no SSH keys — which matters, because `gcloud compute ssh`
is unreliable against Windows images.

A tunnel outlives the command that started it, so its process id is recorded and
checked rather than assumed. The alternative — reconnecting blindly — quietly
stacks a second tunnel on the same port and you never learn which one answered.

A recorded pid is not proof on its own. Process ids are reused, so a pid file
that outlived its tunnel — a reboot, a crash, a `kill` by hand — can name some
unrelated process. Believing it means reporting a tunnel that is not there, and
then sending SIGTERM to whatever inherited the number. So the process is asked
what it is before it is trusted or signalled.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, Host

COMFYUI_PORT = 8188

# Beside the host list, not in /tmp: these belong to the user's configuration and
# a stale pid file must survive a reboot so it can be recognised and cleaned up.
TUNNEL_DIR = DEFAULT_CONFIG_PATH.parent / "tunnels"

# What a real tunnel's command line always contains. Used to tell our own process
# from whatever else has since been given that pid.
TUNNEL_MARKER = "start-iap-tunnel"

# How long a freshly started tunnel is watched before its pid is believed. The
# failures that matter here are immediate — an expired credential, a port already
# taken, no permission on the instance — and every one of them is over in under a
# second. A healthy tunnel pays this once; it is establishing anyway.
SPAWN_GRACE = 1.5


class TunnelError(Exception):
    """A tunnel that could not be started. `fix` is what to do about it."""

    def __init__(self, message: str, fix: str | None = None) -> None:
        super().__init__(message)
        self.fix = fix


@dataclass
class TunnelState:
    host: str
    pid: int | None
    alive: bool
    # A pid file naming a process that is gone, or one that is not a tunnel. The
    # record exists; the tunnel does not.
    stale: bool = False

    @property
    def running(self) -> bool:
        return self.alive and self.pid is not None


def pid_file(host: str, directory: Path | None = None) -> Path:
    return (directory or TUNNEL_DIR) / f"{host}.pid"


def log_file(host: str, directory: Path | None = None) -> Path:
    """Where gcloud's own output for this tunnel is kept.

    When a tunnel dies on startup — no permission, port already taken — the
    reason is only ever in here, so every message about a dead tunnel names it.
    """
    return (directory or TUNNEL_DIR) / f"{host}.log"


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


def _command_of(pid: int) -> str:
    """The process's command line, or "" when it cannot be read.

    Empty means "could not tell", never "not a tunnel": on a machine where `ps`
    is missing or refuses, the old behaviour — trust the pid — is the safe one.
    """
    try:
        proc = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip()


def status(host: str, directory: Path | None = None, inspect=None) -> TunnelState:
    inspect = inspect or _command_of
    path = pid_file(host, directory)
    if not path.exists():
        return TunnelState(host=host, pid=None, alive=False)
    try:
        pid = int(path.read_text().split()[0])
    except (ValueError, IndexError, OSError):
        return TunnelState(host=host, pid=None, alive=False, stale=True)

    if not _alive(pid):
        return TunnelState(host=host, pid=pid, alive=False, stale=True)

    command_line = inspect(pid)
    if command_line and TUNNEL_MARKER not in command_line:
        # The number was reused. Whatever holds it now is not ours to report as a
        # tunnel, and certainly not ours to kill.
        return TunnelState(host=host, pid=pid, alive=False, stale=True)

    return TunnelState(host=host, pid=pid, alive=True)


def command(host: Host) -> list[str]:
    """The gcloud invocation. Pure, so it can be shown by --dry-run."""
    return [
        "gcloud", "compute", "start-iap-tunnel",
        host.gce_instance or host.name, str(COMFYUI_PORT),
        f"--local-host-port=localhost:{host.port}",
        f"--zone={host.gce_zone}",
        f"--project={host.gce_project}",
    ]


def last_words(log: Path, lines: int = 6) -> str:
    """The end of gcloud's own output.

    A detached tunnel writes its only explanation here. When it dies on startup
    this is the difference between "ComfyUI is not answering" and "your gcloud
    session has expired", which are the same silence and opposite fixes.
    """
    try:
        text = log.read_text(errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    return "\n        ".join(text.splitlines()[-lines:])


def _spawn(cmd: list[str], log: Path, grace: float = SPAWN_GRACE) -> int:
    """Start the real tunnel, detached, with its output in `log`.

    Separated from `open_tunnel` so the launch is one seam: tests replace it, and
    the "gcloud is not installed" case is answered here rather than surfacing as
    a FileNotFoundError traceback from Popen.

    The child is watched for a moment before its pid is handed back. gcloud fails
    *immediately* far more often than it fails later — an expired credential is
    the common one, and because output is captured it cannot prompt, so it exits
    rather than asking — and a pid file written over that corpse turns a
    credential failure into "the box is up but ComfyUI is not answering", with
    the machine left running and billing on the strength of it.
    """
    if shutil.which(cmd[0]) is None:
        raise TunnelError(
            "gcloud is not installed or not on PATH, so no tunnel can be opened.",
            fix="https://cloud.google.com/sdk/docs/install",
        )
    try:
        with log.open("ab") as handle:
            process = subprocess.Popen(
                cmd, stdout=handle, stderr=handle,
                # Detach: the tunnel has to outlive this command.
                start_new_session=True,
            )
    except OSError as exc:
        raise TunnelError(f"could not start the tunnel: {exc}") from exc

    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        return process.pid  # still there, which is as much as can be known here

    said = last_words(log)
    raise TunnelError(
        f"the tunnel closed as soon as it was opened (gcloud exited "
        f"{process.returncode}). gcloud said:\n        {said or '(nothing in ' + str(log) + ')'}",
        fix=(f"read {log}. If it mentions credentials or reauthentication, your "
             f"session has expired:\n        gcloud auth login"),
    )


def open_tunnel(host: Host, directory: Path | None = None, launcher=None) -> TunnelState:
    """Start a tunnel unless one is already up. Returns its state either way."""
    existing = status(host.name, directory)
    if existing.running:
        return existing

    directory = directory or TUNNEL_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TunnelError(f"could not use the tunnel directory {directory}: {exc}",
                          fix=f"check the permissions on {directory}") from exc

    pid = (launcher or _spawn)(command(host), log_file(host.name, directory))
    try:
        pid_file(host.name, directory).write_text(str(pid))
    except OSError as exc:
        # A tunnel nobody has a record of cannot be closed by `down`: it holds the
        # local port, and points at a machine you are still paying for, until
        # someone finds it by hand. Better to not have started it.
        try:
            os.kill(pid, signal.SIGTERM)
        except (OSError, ProcessLookupError, PermissionError):
            pass
        raise TunnelError(
            f"the tunnel started but could not be recorded in "
            f"{pid_file(host.name, directory)}, so it was closed again: {exc}",
            fix=f"check the permissions on {directory}",
        ) from exc
    return TunnelState(host=host.name, pid=pid, alive=True)


def close_tunnel(host: str, directory: Path | None = None, killer=os.kill) -> bool:
    """Stop a tunnel. Returns whether there was one to stop.

    A pid that is no longer our tunnel is never signalled — only forgotten. This
    is why the file is checked rather than believed: `down` used to be able to
    kill whatever process had inherited the number.
    """
    state = status(host, directory)
    path = pid_file(host, directory)
    if state.running:
        try:
            killer(state.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    stopped = state.running
    path.unlink(missing_ok=True)
    return stopped
