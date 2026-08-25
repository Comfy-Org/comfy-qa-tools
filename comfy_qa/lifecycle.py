"""Bringing a machine up, and putting it away.

"Up" means ComfyUI answers. A VM that has booted but serves nothing is the
failure this is written to avoid: it looks like success, bills like success, and
you find out only when a test does something strange.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Host
from .gcloud import Gcloud, GcloudError
from .stamp import ProbeError, Stamp, fetch
from .tunnel import close_tunnel, open_tunnel, status as tunnel_status

BOOT_TIMEOUT = 300      # Windows is slower than Linux; both fit inside this.
COMFY_TIMEOUT = 180     # after the box is up, how long ComfyUI gets to answer
POLL_SECONDS = 5

RUNNING = "RUNNING"


# `go` continues past exactly one failure — ComfyUI not being there yet, which is
# what it is about to fix. Everything else must stop and be shown. Catching them
# all alike once hid a failed start and then tried SSH against a stopped machine.
COMFYUI_ABSENT = "comfyui-absent"

# Google's word when a zone has no capacity for the machine type you asked for.
# It is not a fault on your side and no amount of retrying in that zone helps.
STOCKOUT = "stockout"


class LifecycleError(Exception):
    """Something a person has to act on. `fix` says what."""

    def __init__(self, message: str, fix: str | None = None, kind: str = "error") -> None:
        super().__init__(message)
        self.fix = fix
        self.kind = kind


@dataclass
class Ready:
    host: Host
    stamp: Stamp | None
    started: bool
    tunnelled: bool


def _wait(check: Callable[[], bool], *, timeout: int, sleep=time.sleep, now=time.monotonic) -> bool:
    deadline = now() + timeout
    while True:
        if check():
            return True
        if now() >= deadline:
            return False
        sleep(POLL_SECONDS)


import re

_CAPACITY_SIGNS = (
    "stockout",
    "does not have enough resources",
    "zone_resource_pool_exhausted",
    "is currently unavailable in the",
)

_SUGGESTED_ZONES = re.compile(
    r"trying your request in the ([a-z0-9\-]+(?:[,\s]+[a-z0-9\-]+)*)\s+zone", re.IGNORECASE
)


def is_capacity_failure(message: str) -> bool:
    """Did the zone simply run out of the machine you asked for?

    GPU stockouts are routine and the raw message is long and alarming. Saying so
    plainly saves someone debugging their own account for an hour. This reads the
    *full* gcloud output, not the one-line summary — on compute errors that
    summary is literally `---`, which is how a stockout went unrecognised once.
    """
    lowered = message.lower()
    return any(sign in lowered for sign in _CAPACITY_SIGNS)


def suggested_zones(message: str) -> list[str]:
    """Zones Google itself says have capacity right now.

    The stockout message names them. Repeating that is the single most useful
    thing this tool can do with the error.
    """
    match = _SUGGESTED_ZONES.search(message or "")
    if not match:
        return []
    return [zone for zone in re.split(r"[,\s]+", match.group(1)) if zone]


def is_windows(host: Host) -> bool:
    return "windows" in (host.os or "").lower()


def how_to_get_in(host: Host) -> str:
    """How to reach a box's desktop or shell, which differs by operating system.

    Windows needs a password reset and RDP over the tunnel; anything else takes
    SSH through IAP. Printing the Windows recipe for a Linux box would send
    someone down a dead end.
    """
    where = f"--zone {host.gce_zone} --project {host.gce_project}"
    if is_windows(host):
        return (
            f"gcloud compute reset-windows-password {host.gce_instance} {where}\n"
            f"        gcloud compute start-iap-tunnel {host.gce_instance} 3389 "
            f"--local-host-port=localhost:33389 {where}\n"
            f"        then point Remote Desktop at localhost:33389"
        )
    return f"gcloud compute ssh {host.gce_instance} --tunnel-through-iap {where}"


def probe(host: Host) -> Stamp | None:
    try:
        return fetch(host.url, host=host.name)
    except ProbeError:
        return None


def bring_up(
    gc: Gcloud,
    host: Host,
    say: Callable[[str], None],
    *,
    tunnel_dir: Path | None = None,
    sleep=time.sleep,
    now=time.monotonic,
    probe_fn=probe,
    boot_timeout: int = BOOT_TIMEOUT,
    comfy_timeout: int = COMFY_TIMEOUT,
) -> Ready:
    """Start the machine, tunnel to it, and wait until ComfyUI answers."""
    if host.kind == "local":
        stamp = probe_fn(host)
        if stamp is None:
            raise LifecycleError(
                f"ComfyUI is not answering on {host.url}",
                kind=COMFYUI_ABSENT,
                fix="~/ComfyUI/venv/bin/python ~/ComfyUI/main.py --port 8188 --listen 127.0.0.1",
            )
        say(f"{host.name} is already up")
        return Ready(host=host, stamp=stamp, started=False, tunnelled=False)

    try:
        state = gc.instance_status(host.gce_instance, host.gce_zone, host.gce_project)
    except GcloudError as exc:
        raise LifecycleError(str(exc), fix=exc.fix) from exc

    started = False
    if state != RUNNING:
        # TERMINATED is Google's word for stopped. Saying so avoids alarm.
        say(f"{host.name} is {'stopped' if state == 'TERMINATED' else state.lower()} — starting it")
        try:
            gc.start_instance(host.gce_instance, host.gce_zone, host.gce_project)
        except GcloudError as exc:
            # Classify on everything gcloud printed. The one-line summary for a
            # compute error is `---`, which matches nothing.
            if is_capacity_failure(exc.raw):
                elsewhere = suggested_zones(exc.raw)
                advice = (
                    f"Google says {', '.join(elsewhere)} has capacity right now — "
                    f"a box there would start today."
                    if elsewhere else
                    "wait and try later, or use another zone. Capacity varies by "
                    "zone and by hour."
                )
                raise LifecycleError(
                    f"Google has no {host.gpu or 'GPU'} capacity in {host.gce_zone} "
                    f"right now, so {host.name} cannot start. This is not a fault on "
                    f"your side, and retrying in the same zone will not help.",
                    kind=STOCKOUT,
                    fix=advice,
                ) from exc
            raise LifecycleError(f"could not start {host.name}: {exc}", fix=exc.fix) from exc
        started = True

        up = _wait(
            lambda: gc.instance_status(host.gce_instance, host.gce_zone, host.gce_project) == RUNNING,
            timeout=boot_timeout, sleep=sleep, now=now,
        )
        if not up:
            raise LifecycleError(
                f"{host.name} did not reach RUNNING within {boot_timeout}s",
                fix=f"check it in the console, then try again",
            )
    say(f"{host.name} is running")

    existing = tunnel_status(host.name, tunnel_dir)
    if existing.running:
        say(f"tunnel already open on {host.url}")
    else:
        open_tunnel(host, tunnel_dir)
        say(f"tunnel open: {host.url}")

    stamp = None
    deadline = now() + comfy_timeout
    while True:
        stamp = probe_fn(host)
        if stamp is not None:
            break
        if now() >= deadline:
            break
        sleep(POLL_SECONDS)

    if stamp is None:
        raise LifecycleError(
            f"{host.name} is running and tunnelled, but ComfyUI is not answering on "
            f"{host.url}. The machine is up and billing; ComfyUI is not installed or "
            f"not started.",
            kind=COMFYUI_ABSENT,
            fix=(
                "get onto the machine and install or start ComfyUI:\n        "
                + how_to_get_in(host)
            ),
        )

    say(f"ComfyUI answering: {stamp.line()}")
    return Ready(host=host, stamp=stamp, started=started, tunnelled=True)


def wait_for_ssh(
    gc: Gcloud,
    host: Host,
    say: Callable[[str], None],
    *,
    timeout: int = 300,
    sleep=time.sleep,
    now=time.monotonic,
) -> None:
    """Wait until the box will actually run a command.

    RUNNING means the VM is powered on, not that its SSH server is listening.
    Windows takes minutes to get there, and connecting too early fails with
    "failed to connect to backend", which reads like a permissions problem and
    is not.
    """
    said_waiting = False
    deadline = now() + timeout
    while True:
        try:
            gc.ssh_output(host.gce_instance, host.gce_zone, host.gce_project, "echo ok")
            return
        except GcloudError as exc:
            if now() >= deadline:
                raise LifecycleError(
                    f"{host.name} is running but not accepting commands after {timeout}s: {exc}",
                    fix=how_to_get_in(host),
                ) from exc
            if not said_waiting:
                say("waiting for the machine to accept commands — Windows takes a few minutes")
                said_waiting = True
            sleep(POLL_SECONDS)


def ensure_installed(gc: Gcloud, host: Host, say: Callable[[str], None]) -> None:
    """Make sure ComfyUI exists on the box, installing it if it does not."""
    from .provision import check_command, install_command, root_for

    say(f"looking for ComfyUI in {root_for(host)}")
    try:
        answer = gc.ssh_output(host.gce_instance, host.gce_zone, host.gce_project,
                               check_command(host))
    except GcloudError as exc:
        raise LifecycleError(
            f"could not run a command on {host.name}: {exc}",
            fix=how_to_get_in(host),
        ) from exc

    if "INSTALLED" in answer:
        say("ComfyUI is already installed")
        return

    say("ComfyUI is not there — installing it. This takes a while; torch is the "
        "slow part.")
    installed = gc.ssh(host.gce_instance, host.gce_zone, host.gce_project,
                       install_command(host), stream=True)
    if installed != 0:
        raise LifecycleError(
            f"the ComfyUI install on {host.name} did not finish",
            fix=how_to_get_in(host),
        )


def serve(
    gc: Gcloud,
    host: Host,
    say: Callable[[str], None],
    *,
    open_browser: Callable[[str], None] | None = None,
    probe_fn=None,
    sleep=time.sleep,
    now=time.monotonic,
    timeout: int = COMFY_TIMEOUT,
) -> int:
    """Launch ComfyUI on the box with its log on this terminal.

    The browser is opened by a watcher rather than after the launch returns,
    because the launch does not return: ComfyUI runs in the foreground so you can
    read its startup log exactly as you would locally.
    """
    import threading

    from .provision import launch_command

    probe_fn = probe_fn or probe

    def watch() -> None:
        deadline = now() + timeout
        while now() < deadline:
            stamp = probe_fn(host)
            if stamp is not None:
                say(f"ComfyUI answering: {stamp.line()}")
                if open_browser is not None:
                    open_browser(host.url)
                return
            sleep(POLL_SECONDS)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()

    say(f"starting ComfyUI on {host.name} — its log follows. Ctrl-C to stop it.")
    return gc.ssh(host.gce_instance, host.gce_zone, host.gce_project,
                  launch_command(host), stream=True)


def put_away(
    gc: Gcloud,
    host: Host,
    say: Callable[[str], None],
    *,
    tunnel_dir: Path | None = None,
    keep_running: bool = False,
) -> None:
    """Close the tunnel and stop the machine, so it stops costing money."""
    if close_tunnel(host.name, tunnel_dir):
        say("tunnel closed")

    if host.kind == "local":
        say("local ComfyUI left running — this tool did not start it")
        return

    if keep_running:
        say(f"{host.name} left running — it is still billing")
        return

    try:
        gc.stop_instance(host.gce_instance, host.gce_zone, host.gce_project)
    except GcloudError as exc:
        raise LifecycleError(f"could not stop {host.name}: {exc}", fix=exc.fix) from exc
    say(f"{host.name} stopped")
