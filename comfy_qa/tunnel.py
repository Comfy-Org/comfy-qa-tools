"""Tunnels to cloud boxes: SSH forwarding, carried over Identity-Aware Proxy.

ComfyUI has no authentication, so a cloud box must never expose its port. Nothing
here opens one: IAP carries the connection over Google's own path, with no public
route to the instance, and the forward itself is an ordinary `ssh -L`.

That last part matters more than it sounds, and this file claimed the opposite of
it for a long time. A key *is* used — gcloud's own
`~/.ssh/google_compute_engine` — and choosing SSH forwarding over raw IAP TCP
forwarding is what makes a cloud box reachable at all:

  * `gcloud compute start-iap-tunnel` forwards to a **port on the instance**,
    reached over its network interface. ComfyUI then has to bind 0.0.0.0, and the
    port has to be allowed through the VPC firewall *and* the box's own. Three
    things to get right, none of them obvious, and any one of them wrong looks
    exactly like "ComfyUI is not running".
  * `ssh -L` resolves the far address **on the box**, so ComfyUI binds 127.0.0.1
    as it prefers, nothing is exposed on any interface, and no firewall rule is
    needed: port 22 is already open, which is how every other command in this
    tool reaches the machine.

This is the file that decides which machine a tester is actually talking to, so
everything here is one rule said in different ways: never claim a tunnel is open
unless it is, and never claim it goes somewhere unless it does. A tunnel outlives
the command that started it, so what it is gets recorded rather than assumed:

  * **A tunnel that died is not a tunnel.** gcloud fails *immediately* far more
    often than it fails later — an expired credential is the common case, and
    because its output is captured it cannot prompt, so it exits. A pid file
    written over that corpse turns a credential failure into "the box is up but
    ComfyUI is not answering", with the machine left running and billing. So a
    freshly started tunnel is watched for `SPAWN_GRACE` and a dead one is
    reported with the tail of gcloud's own log instead of being recorded.
  * **A pid is a number, not an identity.** The kernel hands numbers back out.
    A stale pid file whose number now belongs to an unrelated process reads as
    "tunnel already open" — so the tool reports a tunnel that is not there, and
    `down` sends SIGTERM to a stranger. What the process *is* — when it started
    and what it is running — is recorded beside the number, and asked again
    before that number is trusted or signalled. When the answer cannot be read
    at all, the pid alone is trusted: that is the older behaviour, and on a
    machine with no `ps` it is the safe one.
  * **The port is what actually answers.** A pid says nothing about who holds
    127.0.0.1:8190. If something else already has it, gcloud cannot bind, and the
    URL handed back reaches whatever is already there. That is the wrong-machine
    failure this tool exists to prevent, so the port is checked before launching.
  * **The name is not the machine.** A second host list can call a different box
    `comfy-win` too. The record names the instance, the zone, the project and the
    port, and a tunnel is only reused when all of them match.
  * **The name is also not a filename.** Host names are TOML table keys, so they
    are whatever someone typed: `../../x` wrote outside the tunnel directory, and
    four hundred characters raised ENAMETOOLONG.
  * **You are not the only `comfy-qat` running.** Opening claims the host with an
    exclusive lock, so two terminals cannot stack two tunnels on one port and
    leave one of them with no record that it exists.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import time
from dataclasses import dataclass
from hashlib import blake2s
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH, Host
from .gcloud import Relay

COMFYUI_PORT = 8188

# Beside the host list, not in /tmp: these belong to the user's configuration and
# a stale pid file must survive a reboot so it can be recognised and cleaned up.
TUNNEL_DIR = DEFAULT_CONFIG_PATH.parent / "tunnels"

# What a real tunnel's command line always contains. Used to tell our own process
# from whatever else has since been given that pid, when there is no record of
# what we started to compare against.
# `gcloud compute ssh --tunnel-through-iap` runs ssh with a ProxyCommand that
# still says `start-iap-tunnel`, so either marker identifies one of ours. The
# forward itself is the surer sign, and it is what the command line now carries.
TUNNEL_MARKER = "start-iap-tunnel"
TUNNEL_MARKERS = ("start-iap-tunnel", "--tunnel-through-iap", "127.0.0.1:")

# How long a freshly started tunnel is watched before its pid is believed. The
# failures that matter here are immediate — an expired credential, a port already
# taken, no permission on the instance — and every one of them is over in under a
# second. A healthy tunnel pays this once; it is establishing anyway.
# gcloud tests the connection before it will serve, and that test can take
# several seconds against a Windows box. 1.5s was shorter than the test, so a
# tunnel that was about to refuse got its pid recorded as if it had opened.
SPAWN_GRACE = 8.0

# gcloud will not open a tunnel to a port with no listener behind it. That is a
# statement about the box, not a broken tunnel, and the caller can fix it by
# starting ComfyUI and asking again.
BACKEND_NOT_LISTENING = "backend-not-listening"

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

    def __init__(self, message: str, fix: str | None = None,
                 kind: str = "tunnel") -> None:
        super().__init__(message)
        self.fix = fix
        # Most tunnel failures are the tunnel's fault and read the same. One is
        # not: gcloud refusing because the far port has no listener is a fact
        # about the box, and the caller can act on it.
        self.kind = kind


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
    def stale(self) -> bool:
        """A record with no tunnel behind it: gone, recycled, or unaccounted for.

        Something was written down for this host and it is no longer true, which
        is worth saying out loud before it is cleared — silently replacing a
        record is how a second tunnel gets stacked on a port.
        """
        return self.pid is not None and not self.running

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
    """Where gcloud's own output for this tunnel is kept.

    When a tunnel dies on startup — no permission, port already taken — the
    reason is only ever in here, so every message about a dead tunnel names it.
    """
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


def _identity(pid: int) -> str:
    """What this process is: when it started, and what it is running.

    One answer carrying both facts, because neither is enough on its own. A
    command line survives a reboot in a way that misleads — after one, any
    `gcloud compute start-iap-tunnel` that lands on the recorded number reads as
    ours. A start time cannot be forged but says nothing about *what* started
    then. Asked together, recorded when the tunnel is opened, and asked again
    before that pid is trusted or signalled, the pair is the process's identity.

    `""` means "could not tell" — no `ps` on this machine, or a process already
    gone — and never "not a tunnel". There the caller falls back to the number
    alone, which is the older behaviour and the safe one: an unrecognised tunnel
    would have a second one stacked on its port.
    """
    if pid <= 0:
        return ""
    try:
        done = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=,command="],
            capture_output=True, text=True, timeout=10,
            # `lstart` renders in the CALLER's locale and timezone, and this
            # string is compared for equality against one recorded earlier. One
            # live process, one instant, three answers:
            #
            #   en_GB   Fri  4 Sep 19:34:26
            #   C       Fri Sep  4 19:34:26
            #   TZ=LA   Fri  4 Sep 11:34:26
            #
            # So `recorded == now` was asking "same string", not "same process".
            # Open a tunnel from a terminal and close it from a script, a cron
            # job, a non-login ssh or an agent shell, and the identities differ:
            # `close_tunnel` returns False, does NOT signal the pid, and still
            # unlinks both records — so the forward stays alive holding the port,
            # nothing is printed because the caller only speaks on True, and the
            # next `open` reports a stranger on the port and sends you to lsof.
            # Measured: 12 of 16 environment pairs.
            #
            # Pinning both makes the comparison about the process again. It has
            # to be on every call, not just the recording one, or the two sides
            # disagree exactly as before.
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC"},
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    # ps pads its columns, and the padding is not part of the answer.
    return " ".join(done.stdout.split())


def _could_be_ours(identity: str) -> bool:
    """Is this process at least *a* tunnel, when there is nothing to compare to?

    The fallback for a pid file with no record beside it — an old one, or one
    edited by hand. Nothing said means the lookup failed, so the pid is trusted;
    anything said has to carry a tunnel's command line.
    """
    return not identity or any(mark in identity for mark in TUNNEL_MARKERS)


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
    identify = identify or _identity
    pid = _read_pid(host, directory)
    if pid is None or pid <= 0:
        return TunnelState(host=host, pid=None, alive=False)

    alive = _alive(pid)
    record = _read_record(host, directory)

    if record is None:
        # Nothing says where this tunnel goes, so it can never be reused as one:
        # `known` stays false and `running` with it. The process is still asked
        # what it is, because that is what decides whether `down` may signal it.
        return TunnelState(host=host, pid=pid, alive=alive,
                           verified=alive and _could_be_ours(identify(pid)))

    if record.get("pid") != pid:
        # The two files contradict each other — half-written, hand-edited, or two
        # tools sharing one directory. A contradiction is not a tunnel.
        return TunnelState(host=host, pid=pid, alive=alive)

    recorded = record.get("identity") or None
    now = identify(pid) if alive else ""
    if recorded is None:
        # Opened by a version that recorded no identity, or on a machine that
        # could not tell us one. All that is left to check is what it looks like.
        verified = alive and _could_be_ours(now)
    else:
        verified = alive and (not now or now == recorded)

    return TunnelState(
        host=host, pid=pid, alive=alive, known=True, verified=verified,
        port=record.get("port"), instance=record.get("instance"),
        zone=record.get("zone"), project=record.get("project"),
    )


def command(host: Host) -> list[str]:
    """The gcloud invocation. Pure, so it can be shown by --dry-run.

    SSH local forwarding, not `start-iap-tunnel`, and the difference is the whole
    reason a cloud box was unreachable for a day:

      * `start-iap-tunnel` forwards to a **port on the instance**, reached over
        its network interface. So ComfyUI had to bind 0.0.0.0, and the port had
        to be allowed through the VPC firewall *and* the box's own firewall —
        three things to get right, none of them obvious, and the failure of any
        one of them looks identical to "ComfyUI is not running".
      * `ssh -L` forwards through the SSH session, and the far address is
        resolved **on the box**. So ComfyUI binds 127.0.0.1 as it prefers,
        nothing is exposed on any interface, and no firewall rule is needed at
        all: port 22 is already open, which is how this tool has been running
        commands on the box the whole time.

    The near end is pinned to `127.0.0.1` rather than left as `localhost`.
    `localhost` resolves to `::1` first on macOS, so ssh bound IPv6 only and
    every probe of `http://127.0.0.1:<port>` was refused while the forward sat
    there working perfectly over IPv6. Verified both ways on a real box.

    `--quiet` because this is the worst place in the tool to be asked a question.
    The first `gcloud compute ssh` on a machine generates
    `~/.ssh/google_compute_engine` and prompts for a passphrase, and this one is
    detached with its output going to a file — so the prompt is not on any screen,
    `ssh-keygen` is waiting on `/dev/tty` for an answer nobody can see to give,
    and `_spawn` watches for `SPAWN_GRACE` seconds, sees a process still running,
    and records its pid as an open tunnel. A tunnel that forwards nothing,
    recorded as one that does, is the single thing this file exists to prevent.
    `open` can be the first command anyone runs, so it cannot rely on `go` having
    made the key first.
    """
    if not host.is_remote:
        raise TunnelError(
            f"{host.name} is local — there is nothing to tunnel. It is at {host.url}.",
        )
    return [
        "gcloud", "compute", "ssh", host.gce_instance or host.name,
        f"--zone={host.gce_zone}",
        f"--project={host.gce_project}",
        "--tunnel-through-iap",
        "--quiet",
        "--", "-N",
        "-L", f"127.0.0.1:{host.port}:127.0.0.1:{COMFYUI_PORT}",
    ]


def last_words(log: Path, lines: int = 6) -> str:
    """The end of gcloud's own output.

    A detached tunnel writes its only explanation here. When it dies on startup
    this is the difference between "ComfyUI is not answering" and "your gcloud
    session has expired", which are the same silence and opposite fixes.

    Read through `Relay`, which is not only about the paste. Six lines is the
    whole budget, and the tunnel is the one place gcloud's NumPy advisory is
    guaranteed to appear — it is advice about IAP forwarding, printed by every
    IAP forward. Four lines of it in a six-line tail pushes the sentence that
    names the cause off the top of the message meant to carry it.
    """
    try:
        text = log.read_text(errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    relay = Relay()
    kept = [line for raw in text.splitlines() for line in relay.line(raw)]
    kept += relay.rest()
    return "\n        ".join(kept[-lines:])


def _spawn(cmd: list[str], log: Path, grace: float = SPAWN_GRACE) -> int:
    """Start the real tunnel, detached, with its output in `log`.

    One named seam rather than a bare `subprocess.Popen` inside `open_tunnel`:
    a test that does not mean to reach Google replaces this, one that does can
    pass its own `launcher=`, and the "gcloud is not installed" case is answered
    here rather than surfacing as a FileNotFoundError traceback from Popen.

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
    if _nothing_listening(said):
        # Not a broken tunnel: gcloud tests the connection before it will serve,
        # and refuses when the far port has no listener. So a tunnel cannot be
        # opened to a box before ComfyUI is started on it — which is the order
        # `go` used, making the whole flow impossible on a box that was not
        # already serving. The caller starts ComfyUI and asks again.
        raise TunnelError(
            f"nothing is listening on port {COMFYUI_PORT} of the machine yet, so "
            f"there is nothing to tunnel to.",
            kind=BACKEND_NOT_LISTENING,
            fix="start ComfyUI on the machine first, then open the tunnel.",
        )
    raise TunnelError(
        f"the tunnel closed as soon as it was opened (gcloud exited "
        f"{process.returncode}). gcloud said:\n        {said or '(nothing in ' + str(log) + ')'}",
        fix=(f"read {log}. If it mentions credentials or reauthentication, your "
             f"session has expired:\n        gcloud auth login"),
    )


def _nothing_listening(said: str) -> bool:
    """Did gcloud refuse because the far end has no listener on that port?"""
    lowered = (said or "").lower()
    return "failed to connect to backend" in lowered or "4003" in lowered


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
                f"another `comfy-qat` is opening the tunnel to {host} right now.",
                fix="wait for it to finish, then run this again",
            ) from None
        lock.unlink(missing_ok=True)
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise TunnelError(
                f"another `comfy-qat` is opening the tunnel to {host} right now.",
                fix="wait for it to finish, then run this again",
            ) from None
    with os.fdopen(handle, "w") as writing:
        writing.write(str(os.getpid()))
    return lock


def _abandon(pid: int) -> None:
    """Close a tunnel we started and could not record. Never raises."""
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError, PermissionError):
        pass


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
    is already taken, when the tunnel recorded under this name goes to a
    different machine, or when gcloud dies as soon as it is started.
    """
    identify = identify or _identity
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
            fix=f"comfy-qat down {host.name}, then open this one",
        )

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise TunnelError(f"cannot write to {directory}: {exc}",
                          fix=f"check the permissions on {directory}") from exc

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

        try:
            # Inside the guard, not above it. Building the record calls
            # `identify`, which shells out to `ps -p N -o lstart=,command=` with
            # a ten-second timeout — so this line was both the slowest thing
            # between the spawn and the writes and the only one unprotected.
            # Measured here, median of seven, twice: ~3.6 ms and ~4.2 ms for
            # the `ps` call against ~0.3 ms for the two writes. The durable
            # claims are the RATIO — thirteen to fourteen times the window the
            # guard covered — and the ten-second timeout, which is the ceiling;
            # the figures themselves are one machine's and move by tenths of a
            # millisecond between runs, so the window has a floor in
            # milliseconds and no ceiling.
            #
            # The record goes down before the pid, so a reader never finds a pid
            # with nothing saying where it goes.
            record = {"pid": pid, "identity": identify(pid) or None,
                      "opened": time.time(), **_destination(host)}
            _write(record_file(host.name, directory), json.dumps(record))
            _write(pid_file(host.name, directory), str(pid))
        except OSError as exc:
            # A tunnel nobody has a record of cannot be closed by `down`: it holds
            # the local port, and points at a machine you are still paying for,
            # until someone finds it by hand. Better to not have started it.
            _abandon(pid)
            raise TunnelError(
                f"the tunnel to {host.name} started (pid {pid}) but could not be "
                f"recorded: {exc}, so it was closed again.",
                fix=f"check the permissions on {directory}",
            ) from exc
        except BaseException:
            # The same reasoning, through the door the OSError branch left open:
            # a Ctrl-C anywhere between the spawn above and the two writes here
            # produces exactly the state that branch exists to prevent — a live
            # ssh holding the port, pointing at a box that is billing, and no
            # file naming it, so `down` cannot close it and nothing on screen
            # says it is there.
            #
            # "The milliseconds between the spawn and the two writes" is what
            # this used to say, and it was wrong twice: the window is dominated
            # by the `ps` call in the record above — which sat OUTSIDE this try
            # until it was moved in — and `ps` has a ten-second timeout, so the
            # window has a floor in milliseconds and no ceiling.
            #
            # The only leftover in this tool that cannot be handed over as a
            # command: the pid is the only handle and it is about to be lost. So
            # it is undone here rather than reported, which is why this needs no
            # `inflight` registration — there is nothing left to register by the
            # time the exception carries on.
            _abandon(pid)
            raise
    finally:
        lock.unlink(missing_ok=True)

    return TunnelState(
        host=host.name, pid=pid, alive=_alive(pid), known=True, verified=True,
        **_destination(host),
    )


def close_tunnel(host: str, directory: Path | None = None, killer=os.kill, *,
                 identify=None) -> bool:
    """Stop a tunnel. Returns whether there was one to stop.

    Signals only a process the record still fits. A pid that is no longer our
    tunnel is never signalled — only forgotten — because `down` used to be able
    to kill whatever process had inherited the number, and SIGTERM is not a
    question you can take back.
    """
    identify = identify or _identity
    state = status(host, directory, identify=identify)
    path = pid_file(host, directory)
    recorded = _read_pid(host, directory)

    stopped = False
    if state.pid is not None and state.alive and state.verified:
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
