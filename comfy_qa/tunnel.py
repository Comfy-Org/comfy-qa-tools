"""SSH-less tunnels to cloud boxes, via Identity-Aware Proxy.

ComfyUI has no authentication, so a cloud box must never expose its port. IAP
forwards a local port to the instance over Google's own path: no public IP, no
firewall opening, and no SSH keys — which matters, because `gcloud compute ssh`
is unreliable against Windows images.

A tunnel outlives the command that started it, so what it is gets recorded rather
than assumed. Recording the process id alone is not enough for that, and this is
the file where that matters most:

  * **A pid is a number, not an identity.** The kernel hands numbers back out.
    A stale pid file whose number now belongs to an unrelated process reads as
    "tunnel already open" — so the tool reports a tunnel that is not there, and
    `down` sends SIGTERM to a stranger. The start time of the process is recorded
    beside the number, and both have to match.
  * **The port is what actually answers.** A pid says nothing about who holds
    127.0.0.1:8190. If something else already has it, gcloud cannot bind, and the
    URL handed back reaches whatever is already there. That is the wrong-machine
    failure this tool exists to prevent, so the port is checked before launching.
  * **The name is not the machine.** A second host list can call a different box
    `comfy-win` too. The record names the instance, the zone, the project and the
    port, and a tunnel is only reused when all of them match.
  * **You are not the only comfy-qat running.** Opening claims the host with an
    exclusive lock, so two terminals cannot stack two tunnels on one port and
    leave one of them with no record that it exists.
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from hashlib import blake2s
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, Host

COMFYUI_PORT = 8188

# Beside the host list, not in /tmp: these belong to the user's configuration and
# a stale pid file must survive a reboot so it can be recognised and cleaned up.
TUNNEL_DIR = DEFAULT_CONFIG_PATH.parent / "tunnels"

# How long a claim on a host may be held before it is assumed abandoned — long
# enough for gcloud to start, short enough that a killed command does not lock a
# host out for the rest of the day.
LOCK_STALE_SECONDS = 120

_PID = re.compile(r"^[0-9]+$")
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_NAME_LIMIT = 60


class TunnelError(Exception):
    """A tunnel that cannot be opened honestly. `fix` is what to do about it.

    Raised rather than returned: every caller of `open_tunnel` goes on to print a
    URL, and a URL that reaches the wrong machine is worse than no URL at all.

    Deliberately the same shape as `LifecycleError` — message, `fix`, `kind` — so
    a caller handles both with one `except`. It cannot subclass it: `lifecycle`
    imports this module, so the dependency only runs one way.
    """

    def __init__(self, message: str, fix: str | None = None) -> None:
        super().__init__(message)
        self.fix = fix
        self.kind = "tunnel"


@dataclass
class TunnelState:
    """What is recorded for a host, and whether it is still true.

    `alive` is only that a process with that number exists. `running` is the
    question worth asking: is our tunnel, to this machine, still up.
    """

    host: str
    pid: int | None
    alive: bool
    known: bool = False       # a record says where this tunnel goes
    verified: bool = False    # ...and the process is still the one recorded
    port: int | None = None
    instance: str | None = None
    zone: str | None = None
    project: str | None = None

    @property
    def running(self) -> bool:
        return self.alive and self.known and self.verified and self.pid is not None

    @property
    def url(self) -> str | None:
        """Where this tunnel actually answers — not where a caller hoped it did."""
        return f"http://127.0.0.1:{self.port}" if self.port else None


def _safe(host: str) -> str:
    """A host name turned into one filename inside the tunnel directory.

    Names come from TOML table keys, so they are whatever someone typed: `../..`,
    a slash, four hundred characters, an empty string. An ordinary name is used
    unchanged, so `comfy-win` still writes `comfy-win.pid`. Anything else is
    folded into a safe form and given a digest, so it cannot leave this directory
    and cannot collide with another odd name that folds the same way.
    """
    if _SAFE_NAME.match(host) and len(host) <= _NAME_LIMIT:
        return host
    digest = blake2s(host.encode("utf-8"), digest_size=6).hexdigest()
    kept = re.sub(r"[^A-Za-z0-9._-]", "_", host)[:_NAME_LIMIT].lstrip(".-_")
    return f"{kept}-{digest}" if kept else digest


def _in(directory: Path | None) -> Path:
    return directory or TUNNEL_DIR


def pid_file(host: str, directory: Path | None = None) -> Path:
    return _in(directory) / f"{_safe(host)}.pid"


def record_file(host: str, directory: Path | None = None) -> Path:
    """Who the tunnel goes to, and which process is carrying it."""
    return _in(directory) / f"{_safe(host)}.json"


def log_file(host: str, directory: Path | None = None) -> Path:
    return _in(directory) / f"{_safe(host)}.log"


def _lock_file(host: str, directory: Path | None = None) -> Path:
    return _in(directory) / f"{_safe(host)}.lock"


def _alive(pid: int) -> bool:
    """Is this process still there? Signal 0 asks without disturbing it.

    Only real pids are asked about. `os.kill` reads 0 as "my whole process group"
    and -1 as "everything I am allowed to signal", so a truncated or hand-edited
    pid file must never reach it.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but belongs to someone else — still occupying the port.
        return True
    except OSError:
        return False
    return True


def _start_time(pid: int) -> str | None:
    """When this process started, as the OS reports it.

    Pid plus start time is a process's real identity: the number is reused, the
    pair is not. Returns None when it cannot be read — a process that has already
    gone, or a platform without `ps` — and the caller falls back to the number
    alone rather than pretending to know more than it does.
    """
    if pid <= 0:
        return None
    try:
        done = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout.strip() or None


def _port_busy(port: int, host: str = "127.0.0.1") -> bool:
    """Is something already answering there?

    A connect, not a bind: the tunnel binds to localhost, and asking whether we
    could bind would tell us nothing about a process that already has it.
    """
    try:
        with socket.create_connection((host, port), timeout=0.3):
            return True
    except OSError:
        return False


def _read_record(host: str, directory: Path | None) -> dict | None:
    try:
        raw = record_file(host, directory).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def _read_pid(host: str, directory: Path | None) -> int | None:
    try:
        raw = pid_file(host, directory).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return int(raw) if _PID.match(raw) else None


def _write(path: Path, text: str) -> None:
    """Replace a file in one step, so no one ever reads half of it."""
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def status(host: str, directory: Path | None = None, *, identify=None) -> TunnelState:
    """What, if anything, is tunnelling for this host right now."""
    identify = identify or _start_time
    pid = _read_pid(host, directory)
    if pid is None or pid <= 0:
        return TunnelState(host=host, pid=None, alive=False)

    alive = _alive(pid)
    record = _read_record(host, directory)
    if record is None or record.get("pid") != pid:
        # Nothing says where this tunnel goes, so it cannot be claimed as ours.
        # `close_tunnel` will still clean it up; `open_tunnel` will not trust it.
        return TunnelState(host=host, pid=pid, alive=alive)

    recorded_start = record.get("started")
    verified = alive and (recorded_start is None or identify(pid) == recorded_start)
    return TunnelState(
        host=host, pid=pid, alive=alive, known=True, verified=verified,
        port=record.get("port"), instance=record.get("instance"),
        zone=record.get("zone"), project=record.get("project"),
    )


def command(host: Host) -> list[str]:
    """The gcloud invocation. Pure, so it can be shown by --dry-run."""
    if not host.is_remote:
        raise TunnelError(
            f"{host.name} is local — there is nothing to tunnel. It is at {host.url}.",
        )
    return [
        "gcloud", "compute", "start-iap-tunnel",
        host.gce_instance or host.name, str(COMFYUI_PORT),
        f"--local-host-port=localhost:{host.port}",
        f"--zone={host.gce_zone}",
        f"--project={host.gce_project}",
    ]


def _spawn(cmd: list[str], log: Path) -> int:
    """Start gcloud detached, with everything it says kept in the log.

    One named seam rather than a bare `subprocess.Popen` inside `open_tunnel`:
    a test that does not mean to reach Google can replace this, and one that does
    can pass its own `launcher=`.
    """
    with log.open("ab") as handle:
        process = subprocess.Popen(
            cmd, stdout=handle, stderr=handle,
            # Detach: the tunnel has to outlive this command.
            start_new_session=True,
        )
    return process.pid


def _destination(host: Host) -> dict:
    return {
        "port": host.port,
        "instance": host.gce_instance or host.name,
        "zone": host.gce_zone,
        "project": host.gce_project,
    }


def _same_machine(state: TunnelState, host: Host) -> bool:
    wanted = _destination(host)
    return (
        state.port == wanted["port"]
        and state.instance == wanted["instance"]
        and state.zone == wanted["zone"]
        and state.project == wanted["project"]
    )


def _claim(host: str, directory: Path, now=time.time) -> Path:
    """Take this host for the length of one open. Raises if someone else has it.

    Two terminals opening at once is the ordinary case on a machine that runs
    several agents. Without this both see no pid file, both start gcloud, one
    binds the port and the other does not, and the pid file names whichever wrote
    last — leaving a tunnel running that nothing can find or stop.
    """
    lock = _lock_file(host, directory)
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            age = now() - lock.stat().st_mtime
        except OSError:
            age = LOCK_STALE_SECONDS + 1
        if age < LOCK_STALE_SECONDS:
            raise TunnelError(
                f"another comfy-qat is opening the tunnel to {host} right now.",
                fix="wait for it to finish, then run this again",
            ) from None
        lock.unlink(missing_ok=True)
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise TunnelError(
                f"another comfy-qat is opening the tunnel to {host} right now.",
                fix="wait for it to finish, then run this again",
            ) from None
    with os.fdopen(handle, "w") as writing:
        writing.write(str(os.getpid()))
    return lock


def open_tunnel(
    host: Host,
    directory: Path | None = None,
    launcher=None,
    *,
    identify=None,
    port_busy=None,
) -> TunnelState:
    """Start a tunnel unless ours is already up. Returns its state either way.

    Refuses, rather than reporting a tunnel it cannot stand behind, when the port
    is already taken or when the tunnel recorded under this name goes to a
    different machine.
    """
    identify = identify or _start_time
    port_busy = port_busy or _port_busy
    directory = _in(directory)

    if not host.is_remote:
        raise TunnelError(
            f"{host.name} is local — there is nothing to tunnel. It is at {host.url}.",
        )

    existing = status(host.name, directory, identify=identify)
    if existing.running:
        if _same_machine(existing, host):
            return existing
        raise TunnelError(
            f"a tunnel called {host.name!r} is already open (pid {existing.pid}), but it "
            f"goes to {existing.instance} in {existing.zone} on port {existing.port}, "
            f"not to {host.gce_instance} in {host.gce_zone} on port {host.port}.",
            fix=f"comfy-qat host down {host.name}, then open this one",
        )

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TunnelError(f"cannot write to {directory}: {exc}") from exc

    lock = _claim(host.name, directory)
    try:
        if port_busy(host.port):
            raise TunnelError(
                f"something is already listening on 127.0.0.1:{host.port}, and it is not "
                f"a tunnel this tool opened. A tunnel started now could not bind that "
                f"port, so {host.url} would answer for whatever is already there.",
                fix=(
                    f"find it with `lsof -nP -iTCP:{host.port} -sTCP:LISTEN`, or give "
                    f"{host.name} a different port in your host list"
                ),
            )

        log = log_file(host.name, directory)
        try:
            pid = (launcher or _spawn)(command(host), log)
        except OSError as exc:
            raise TunnelError(
                f"could not start the tunnel to {host.name}: {exc}",
                fix="check that gcloud is installed and on your PATH",
            ) from exc

        # The record goes down before the pid, so a reader never finds a pid with
        # nothing saying where it goes.
        record = {"pid": pid, "started": identify(pid), "opened": time.time(),
                  **_destination(host)}
        try:
            _write(record_file(host.name, directory), json.dumps(record))
            _write(pid_file(host.name, directory), str(pid))
        except OSError as exc:
            raise TunnelError(
                f"the tunnel to {host.name} started (pid {pid}) but could not be "
                f"recorded: {exc}. Stop it by hand — nothing here can find it again.",
            ) from exc
    finally:
        lock.unlink(missing_ok=True)

    return TunnelState(
        host=host.name, pid=pid, alive=_alive(pid), known=True, verified=True,
        **_destination(host),
    )


def close_tunnel(host: str, directory: Path | None = None, killer=os.kill, *,
                 identify=None) -> bool:
    """Stop a tunnel. Returns whether there was one to stop.

    Signals only a process the record still fits. A recycled pid belongs to
    someone else's work, and SIGTERM is not a question.
    """
    identify = identify or _start_time
    state = status(host, directory, identify=identify)
    path = pid_file(host, directory)
    recorded = _read_pid(host, directory)

    stale = state.alive and state.known and not state.verified
    stopped = False
    if state.pid is not None and state.alive and not stale:
        try:
            killer(state.pid, signal.SIGTERM)
            stopped = True
        except (ProcessLookupError, PermissionError):
            stopped = False

    # Only clear what we read. A `down` racing an `open` must not delete the
    # record of the tunnel that command just started.
    if _read_pid(host, directory) == recorded:
        try:
            path.unlink(missing_ok=True)
            record_file(host, directory).unlink(missing_ok=True)
        except OSError:
            # The tunnel is down either way; a read-only directory is not a
            # reason to fail the command that stopped it.
            pass
    return stopped
