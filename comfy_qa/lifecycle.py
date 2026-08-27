"""Bringing a machine up, and putting it away.

"Up" means ComfyUI answers. A VM that has booted but serves nothing is the
failure this is written to avoid: it looks like success, bills like success, and
you find out only when a test does something strange.

Every failure after the machine has been started says so, and says how to stop
paying for it. A message that only explains what went wrong leaves a GPU box
running all night.
"""

from __future__ import annotations

import http.client
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Host
from .gcloud import Gcloud, GcloudError
from .stamp import ProbeError, Stamp, fetch
from .tunnel import (
    COMFYUI_PORT,
    TunnelError,
    close_tunnel,
    log_file,
    open_tunnel,
    status as tunnel_status,
)

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

# The tunnel went away underneath us. Reported separately because the obvious
# reading — "ComfyUI is not answering" — sends you onto the box to fix something
# that was never broken.
TUNNEL_DOWN = "tunnel-down"


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


# The clock, behind one name each. Bound at call time rather than as a default
# argument, so a test can run a whole flow without waiting out real timeouts.
def _clock() -> float:
    return time.monotonic()


def _pause(seconds: float) -> None:
    time.sleep(seconds)


def _wait(check: Callable[[], bool], *, timeout: int, sleep=None, now=None) -> bool:
    sleep = sleep or _pause
    now = now or _clock
    deadline = now() + timeout
    while True:
        if check():
            return True
        if now() >= deadline:
            return False
        sleep(POLL_SECONDS)


_CAPACITY_SIGNS = (
    "stockout",
    "does not have enough resources",
    "resource_pool_exhausted",
    "is currently unavailable in the",
)

_SUGGESTED_ZONES = re.compile(
    r"trying your request in the ([a-z0-9\-]+(?:[,\s]+[a-z0-9\-]+)*)\s+zone", re.IGNORECASE
)

# us-central1-b, europe-west4-a, northamerica-northeast1-c: a region that ends in
# a digit, then a single letter. Anything else caught between two zone names —
# "us-central1-b or us-central1-c" — is a word, not a zone.
_ZONE = re.compile(r"[a-z]+(?:-[a-z]+)*\d+-[a-z]$")


def is_capacity_failure(message: str | None) -> bool:
    """Did the zone simply run out of the machine you asked for?

    GPU stockouts are routine and the raw message is long and alarming. Saying so
    plainly saves someone debugging their own account for an hour. This reads the
    *full* gcloud output, not the one-line summary — on compute errors that
    summary is literally `---`, which is how a stockout went unrecognised once.
    """
    lowered = (message or "").lower()
    return any(sign in lowered for sign in _CAPACITY_SIGNS)


def suggested_zones(message: str | None) -> list[str]:
    """Zones Google itself says have capacity right now.

    The stockout message names them. Repeating that is the single most useful
    thing this tool can do with the error — as long as it repeats zones and only
    zones: `--to or` is not a command anyone can run.
    """
    match = _SUGGESTED_ZONES.search(message or "")
    if not match:
        return []
    found = []
    for zone in re.split(r"[,\s]+", match.group(1)):
        if _ZONE.fullmatch(zone.lower()) and zone not in found:
            found.append(zone)
    return found


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
            "        then point Remote Desktop at localhost:33389"
        )
    return f"gcloud compute ssh {host.gce_instance} --tunnel-through-iap {where}"


def stop_paying(host: Host) -> str:
    """The one line every post-start failure has to end with.

    The machine is on and billing by the time most of these can happen, and a
    message that does not say so is how a box runs all night.
    """
    return f"comfy-qat host down {host.name}   # closes the tunnel and stops the box"


def is_auth_failure(exc: GcloudError) -> bool:
    """Is this a credential problem rather than a machine problem?

    Waiting cannot fix one. gcloud only offers an interactive reauthentication
    when stdin and stderr are both terminals, and everything here captures its
    output — so it does not ask, it fails, and it will fail again in five seconds
    and in five minutes.

    `kind`/`is_auth` come from `gcloud.classify`; read defensively so this works
    either side of that landing.
    """
    if getattr(exc, "is_auth", False):
        return True
    lowered = f"{exc} {getattr(exc, 'raw', '')}".lower()
    return any(sign in lowered for sign in (
        "reauthentication failed",
        "refreshing your current auth tokens",
        "do not currently have an active account",
        "your gcloud session has expired",
    ))


def stand_down(host: Host, tunnel_dir: Path | None, say: Callable[[str], None]) -> None:
    """Close the tunnel this run opened, on the way out of a failure.

    Nothing after this point is going to use it, and a forgotten tunnel is a
    detached process holding a local port open onto a machine you are still
    paying for — the kind of leak nobody notices until two of them disagree about
    which box answered.
    """
    if host.kind == "local":
        return
    if close_tunnel(host.name, tunnel_dir):
        say("tunnel closed")


def probe(host: Host) -> Stamp | None:
    """Ask a host what it is, treating every way of not answering as "not yet".

    A tunnel with nothing behind it accepts the connection and then resets it,
    which arrives as ConnectionResetError rather than as a probe failure. Letting
    that escape turned a half-open tunnel into a traceback.
    """
    try:
        return fetch(host.url, host=host.name)
    except (ProbeError, OSError, http.client.HTTPException):
        return None


def bring_up(
    gc: Gcloud,
    host: Host,
    say: Callable[[str], None],
    *,
    tunnel_dir: Path | None = None,
    sleep=None,
    now=None,
    probe_fn=None,
    # The last seam without one. Without it, any test that reaches this function
    # starts a real `gcloud compute start-iap-tunnel` — which passed on CI images
    # that ship the SDK and failed on the ones that do not, in both directions.
    launcher=None,
    boot_timeout: int = BOOT_TIMEOUT,
    comfy_timeout: int = COMFY_TIMEOUT,
) -> Ready:
    """Start the machine, tunnel to it, and wait until ComfyUI answers."""
    # Resolved here rather than bound as defaults, so a test can replace the
    # HTTP probe or the clock without replacing this function. Binding at import
    # made the quota wait untestable without really sleeping, which cost two
    # minutes of a verification run before anyone noticed.
    probe_fn = probe_fn or probe
    sleep = sleep or _pause
    now = now or _clock


    if host.kind == "local":
        stamp = probe_fn(host)
        if stamp is None:
            raise LifecycleError(
                f"ComfyUI is not answering on {host.url}",
                kind=COMFYUI_ABSENT,
                fix=f"~/ComfyUI/venv/bin/python ~/ComfyUI/main.py --port {host.port} "
                    "--listen 127.0.0.1",
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
                if elsewhere:
                    advice = (
                        f"Google says {', '.join(elsewhere)} has capacity right now.\n"
                        f"        comfy-qat host move {host.name} --to {elsewhere[0]}"
                    )
                else:
                    advice = (
                        "wait and try later, or move the box to another zone:\n"
                        f"        comfy-qat host move {host.name}"
                    )
                raise LifecycleError(
                    f"Google has no {host.gpu or 'GPU'} capacity in {host.gce_zone} "
                    f"right now, so {host.name} cannot start. This is not a fault on "
                    "your side, and retrying in the same zone will not help.",
                    kind=STOCKOUT,
                    fix=advice,
                ) from exc
            raise LifecycleError(f"could not start {host.name}: {exc}", fix=exc.fix) from exc
        started = True

        # A describe that fails mid-poll is not fatal on its own — the API is
        # allowed a bad minute — but if it never recovers, the reason it gave is
        # the useful thing to print, not "did not reach RUNNING".
        last_error: GcloudError | None = None

        def is_running() -> bool:
            nonlocal last_error
            try:
                return gc.instance_status(
                    host.gce_instance, host.gce_zone, host.gce_project) == RUNNING
            except GcloudError as exc:
                last_error = exc
                return False

        up = _wait(is_running, timeout=boot_timeout, sleep=sleep, now=now)
        if not up:
            if last_error is not None:
                raise LifecycleError(
                    f"could not tell whether {host.name} reached RUNNING: {last_error}",
                    fix=last_error.fix or stop_paying(host),
                ) from last_error
            raise LifecycleError(
                f"{host.name} did not reach RUNNING within {boot_timeout}s. It was "
                "asked to start, so it may be billing already.",
                fix=("check it in the console, then try again, or:\n        "
                     + stop_paying(host)),
            )
    say(f"{host.name} is running")

    existing = tunnel_status(host.name, tunnel_dir)
    if existing.running:
        say(f"tunnel already open on {host.url}")
    else:
        if existing.stale:
            say("clearing a tunnel record whose process is gone")
        try:
            open_tunnel(host, tunnel_dir, launcher=launcher)
        except TunnelError as exc:
            raise LifecycleError(
                f"could not open the tunnel to {host.name}: {exc}",
                kind=TUNNEL_DOWN,
                fix=((exc.fix + "\n        or stop paying for it:\n        ") if exc.fix
                     else "") + stop_paying(host),
            ) from exc
        say(f"tunnel open: {host.url}")

    stamp = None
    cleared = False
    deadline = now() + comfy_timeout
    while True:
        stamp = probe_fn(host)
        if stamp is not None:
            break
        # Silence here has three causes that look identical: nothing is running,
        # the tunnel died, or a firewall two hops away is dropping it. The third
        # is the one nobody guesses, so it is ruled out once — after a probe has
        # failed, never before, because a box that already answers should not
        # have its firewalls touched at all.
        if not cleared:
            cleared = True
            _open_the_way(gc, host, say)
            continue
        # A dead tunnel and an absent ComfyUI look identical from here — both are
        # silence on the port — and only one of them is fixed on the box.
        if not tunnel_status(host.name, tunnel_dir).running:
            raise LifecycleError(
                f"the tunnel to {host.name} closed, so nothing is listening on "
                f"{host.url} any more. ComfyUI was never reached.",
                kind=TUNNEL_DOWN,
                fix=(f"read what gcloud said in {log_file(host.name, tunnel_dir)}, "
                     f"then:\n        comfy-qat host open {host.name}"
                     "\n        or stop paying for it:\n        "
                     + stop_paying(host)),
            )
        if now() >= deadline:
            break
        sleep(POLL_SECONDS)

    if stamp is None:
        raise LifecycleError(
            f"{host.name} is running and tunnelled, but ComfyUI is not answering on "
            f"{host.url}. The machine is up and billing; ComfyUI is not installed or "
            "not started.",
            kind=COMFYUI_ABSENT,
            fix=(
                "get onto the machine and install or start ComfyUI:\n        "
                + how_to_get_in(host)
                + "\n        or stop paying for it:\n        "
                + stop_paying(host)
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
    sleep=None,
    now=None,
    tunnel_dir: Path | None = None,
) -> None:
    """Wait until the box will actually run a command.

    RUNNING means the VM is powered on, not that its SSH server is listening.
    Windows takes minutes to get there, and connecting too early fails with
    "failed to connect to backend", which reads like a permissions problem and
    is not.

    Waiting is only right for a machine that is still waking up. A credential
    that has expired will not come back on its own, and retrying it for five
    minutes is five minutes of GPU time spent on something that cannot succeed.
    """
    sleep = sleep or _pause
    now = now or _clock
    said_waiting = False
    deadline = now() + timeout
    while True:
        try:
            gc.ssh_output(host.gce_instance, host.gce_zone, host.gce_project, "echo ok")
            return
        except GcloudError as exc:
            if is_auth_failure(exc):
                stand_down(host, tunnel_dir, say)
                raise LifecycleError(
                    f"gcloud is not signed in, so {host.name} cannot be reached: {exc}. "
                    "Waiting will not fix this, and the machine is running and billing.",
                    fix=((exc.fix or "gcloud auth login")
                         + "\n        or stop paying for it:\n        "
                         + stop_paying(host)),
                ) from exc
            if now() >= deadline:
                stand_down(host, tunnel_dir, say)
                raise LifecycleError(
                    f"{host.name} is running but not accepting commands after {timeout}s: {exc}",
                    fix=(how_to_get_in(host) + "\n        or stop paying for it:\n        "
                         + stop_paying(host)),
                ) from exc
            if not said_waiting:
                say("waiting for the machine to accept commands — Windows takes a few minutes")
                said_waiting = True
            sleep(POLL_SECONDS)


def ensure_installed(gc: Gcloud, host: Host, say: Callable[[str], None],
                     *, tunnel_dir: Path | None = None) -> None:
    """Make sure ComfyUI exists on the box, installing it if it does not."""
    from .provision import check_command, install_command, root_for

    def give_up(message: str, *, egress: bool = False) -> LifecycleError:
        return _give_up(host, tunnel_dir, say, message, egress=egress)

    def look() -> str:
        try:
            return gc.ssh_output(host.gce_instance, host.gce_zone, host.gce_project,
                                 check_command(host))
        except GcloudError as exc:
            raise give_up(f"could not run a command on {host.name}: {exc}") from exc

    say(f"looking for ComfyUI in {root_for(host)}")
    if "INSTALLED" in look():
        say("ComfyUI is already installed")
        _verify(gc, host, say, give_up)
        return

    say("ComfyUI is not there — installing it. This takes a while; torch is the "
        "slow part.")
    try:
        installed = gc.ssh(host.gce_instance, host.gce_zone, host.gce_project,
                           install_command(host), stream=True)
    except GcloudError as exc:
        raise give_up(
            f"the ComfyUI install on {host.name} did not finish: {exc}") from exc

    if installed != 0:
        raise give_up(
            f"the ComfyUI install on {host.name} did not finish (exit {installed})")

    # An install script that exits 0 having installed nothing is not a theory:
    # on Windows a failed clone leaves every later step running in the wrong
    # directory, and the whole thing still ends "install complete".
    if "INSTALLED" not in look():
        raise give_up(
            f"the ComfyUI install on {host.name} did not finish: it reported success "
            f"but {root_for(host)} still has no ComfyUI in it")


# 130 is a program stopped with Ctrl-C. That is a person finishing, not a fault.
INTERRUPTED_EXIT = 130


def _network_of(host: Host, rules: list[dict]) -> str:
    """The network this project's rules are on. `default` unless told otherwise."""
    for rule in rules:
        network = (rule.get("network") or "").rsplit("/", 1)[-1]
        if network:
            return network
    return "default"


def _open_the_way(gc: Gcloud, host: Host, say: Callable[[str], None]) -> None:
    """Make sure the tunnel can actually reach ComfyUI, through both firewalls.

    A tunnel that connects proves nothing. The port it forwards to sits behind
    the VPC firewall and then behind the box's own, and neither allows 8188 by
    default — so a brand-new machine runs ComfyUI on its GPU and the browser
    says "refused". That cost an afternoon to trace, because every layer looked
    healthy from where it stood: the instance was up, the tunnel was open, the
    process was serving, and nothing joined those three facts together.

    Expecting anyone to write a firewall rule by hand before their first launch
    is not a setup step, it is a trap. Both rules are made once and recognised
    by name afterwards, so this runs every launch and does nothing after the
    first.

    Neither opens anything to the internet. The VPC rule is scoped to Google's
    IAP range, so reaching the port still requires a tunnel authenticated as
    somebody with access to the project.
    """
    from .provision import FIREWALL_RULE, IAP_RANGE, firewall_command

    if not host.is_remote:
        return

    project = host.gce_project or ""
    try:
        existing = gc.firewall_rules(project)
    except GcloudError:
        existing = None            # cannot tell; not a reason to stop

    if existing is not None and not any(
            rule.get("name") == FIREWALL_RULE for rule in existing):
        say(f"opening ComfyUI's port to Google's tunnel range only ({IAP_RANGE})")
        try:
            gc.create_firewall_rule(
                FIREWALL_RULE, project,
                network=_network_of(host, existing),
                rules=f"tcp:{COMFYUI_PORT}", source_ranges=IAP_RANGE,
                description="comfy-qat: IAP TCP forwarding to ComfyUI. "
                            "Not open to the internet.",
            )
        except GcloudError as exc:
            # Someone may have created it a moment ago, or this account may not
            # be allowed to. Say it, then let the launch have its chance.
            say(f"could not add the firewall rule ({exc}) — if the browser "
                f"cannot reach {host.url}, this is why")

    # The second firewall, and the one nobody remembers: Windows blocks inbound
    # TCP by default, so the VPC rule alone still leaves the port refused.
    try:
        gc.ssh_output(host.gce_instance, host.gce_zone, host.gce_project,
                      firewall_command(host))
    except GcloudError:
        return


def _port_holder(gc: Gcloud, host: Host) -> tuple[str, str] | None:
    """Who, if anyone, already holds ComfyUI's port on the box.

    Returns (pid, process name), or None when the port is free or the question
    could not be asked — an unanswerable box is not a reason to refuse to launch.
    """
    from .provision import PORT_FREE, port_holder_command

    try:
        answer = gc.ssh_output(host.gce_instance, host.gce_zone, host.gce_project,
                               port_holder_command(host))
    except GcloudError:
        return None
    # Whatever the box said, as text: this asks a question and the only wrong
    # answer is one that stops a launch which would otherwise have worked.
    answer = str(answer or "").strip()
    if not answer or PORT_FREE in answer:
        return None
    # The command prints "<pid> <name>" or PORT_FREE, so anything else means the
    # question was not answered — not that the port is held. Reading a stray
    # number as a pid would refuse a launch that was going to work, which is a
    # worse failure than the one this check exists to prevent.
    parts = answer.split()
    if len(parts) < 2 or not parts[0].isdigit():
        return None
    return parts[0], parts[1]


def _stop_ours(gc: Gcloud, host: Host, say: Callable[[str], None]) -> None:
    """Stop the ComfyUI this run started, if it outlived the launch.

    A launch that dies after binding the port leaves a process behind, and the
    next launch fails with ComfyUI's own "Port 8188 is already in use" — which
    names neither the process nor the tool that left it there. Watched that
    happen three times on one box before anyone looked with `Get-NetTCPConnection`.

    Best effort: the box may be unreachable by now, and a failure to tidy up is
    not worth replacing the failure the caller is already reporting.
    """
    from .provision import stop_command

    holder = _port_holder(gc, host)
    if holder is None:
        return
    pid, name = holder
    try:
        gc.ssh(host.gce_instance, host.gce_zone, host.gce_project,
               stop_command(host, pid), stream=False)
    except GcloudError:
        say(f"could not stop the ComfyUI left on {host.name} (pid {pid}) — "
            f"it still holds the port")
        return
    say(f"stopped the ComfyUI this run started on {host.name} ({name}, pid {pid})")


def _give_up(host: Host, tunnel_dir, say: Callable[[str], None], message: str,
             *, egress: bool = False,
             stop_first: tuple[str, str] | None = None) -> LifecycleError:
    """Stop, close the tunnel, and say what to do — the one place that decides.

    Written once because it was written twice: the second copy grew the egress
    advice and the first did not, so the path that needed it most raised a
    TypeError instead of printing it.
    """
    stand_down(host, tunnel_dir, say)
    advice = how_to_get_in(host)
    if stop_first is not None and host.is_remote:
        from .provision import stop_command

        pid, _ = stop_first
        advice = (
            "if that is a ComfyUI you no longer want, stop it:\n        "
            f"gcloud compute ssh {host.gce_instance} --zone={host.gce_zone} "
            f"--project={host.gce_project} --tunnel-through-iap "
            f"--command='{stop_command(host, pid)}'"
            f"\n        then run the same command again\n        " + advice
        )
    if egress and host.is_remote:
        # Not guessable from the box: everything reaches it fine, so nobody
        # thinks to check whether it can reach anything.
        advice = (
            "if pypi timed out, the box has no route out — IAP reaches it, "
            "but an instance with no external address and no Cloud NAT "
            "cannot reach the internet:\n        "
            f"gcloud compute instances add-access-config {host.gce_instance} "
            f"--zone={host.gce_zone} --project={host.gce_project}"
            "\n        then run the same command again\n        "
            + advice
        )
    return LifecycleError(
        message,
        fix=(advice + "\n        or stop paying for it:\n        "
             + stop_paying(host)),
    )


def _verify(gc: Gcloud, host: Host, say: Callable[[str], None], give_up) -> None:
    """Ask the box whether ComfyUI could start, and fix the one thing we can.

    Installed is not the same as usable, and the gap costs money: a box that
    cannot start has already booted, tunnelled and begun billing by the time
    anyone finds out. Two real failures on one machine in one afternoon —
    a dependency added after the disk was imaged, and a torch that could not see
    the card — both of which `main.py exists` answered "yes" to.

    A CPU-only torch on a GPU box is repaired here rather than reported, because
    the repair is exactly the command the installer would have run and the
    alternative is a tester watching a 122 MB download fail at launch instead.
    """
    from .provision import (
        NO_COMFYUI, NO_TORCH, READY, TORCH_NO_CUDA, repair_command, root_for,
        verify_command,
    )

    try:
        state = gc.ssh_output(host.gce_instance, host.gce_zone, host.gce_project,
                              verify_command(host)).strip()
    except GcloudError:
        # The check is a convenience, not a gate. A box that cannot be asked is
        # still worth trying to launch; the launch will say what happened.
        return

    if READY in state or not state:
        return
    if NO_COMFYUI in state:
        raise give_up(
            f"{host.name} has no ComfyUI in {root_for(host)}, though the install "
            "check said it did.")

    if NO_TORCH in state:
        say("torch is not installed on this box — installing it before launching")
    elif TORCH_NO_CUDA in state:
        # The specific failure: PyPI's Windows torch wheel is CPU-only, so any
        # `pip install -r requirements.txt` on Windows quietly produces a box
        # that cannot use the card it is rented for.
        say(f"torch on {host.name} cannot see the {host.gpu or 'GPU'} — it is a "
            "CPU-only build, so ComfyUI would start and refuse to run")
        say("  installing the CUDA build instead; this is the slow part")
    else:
        return

    try:
        code = gc.ssh(host.gce_instance, host.gce_zone, host.gce_project,
                      repair_command(host, force_torch=TORCH_NO_CUDA in state),
                      stream=True)
    except GcloudError as exc:
        raise give_up(f"could not install torch on {host.name}: {exc}", egress=True) from exc
    if code != 0:
        raise give_up(
            f"torch could not be installed on {host.name} (exit {code}), so "
            "ComfyUI cannot use its GPU. Its log is above.", egress=True)


def serve(
    gc: Gcloud,
    host: Host,
    say: Callable[[str], None],
    *,
    open_browser: Callable[[str], None] | None = None,
    probe_fn=None,
    sleep=None,
    now=None,
    timeout: int = COMFY_TIMEOUT,
    tunnel_dir: Path | None = None,
    repair: bool = True,
) -> int:
    """Launch ComfyUI on the box with its log on this terminal.

    The browser is opened by a watcher rather than after the launch returns,
    because the launch does not return: ComfyUI runs in the foreground so you can
    read its startup log exactly as you would locally.

    A launch that ends badly is raised, not returned: returning the code left
    `go` exiting 0 after printing "ComfyUI exited (3)", so a box with no
    interpreter on it looked, to anything reading exit codes, like a success.
    """
    import threading

    from .provision import NO_PYTHON_EXIT, launch_command

    sleep = sleep or _pause
    now = now or _clock
    probe_fn = probe_fn or probe
    done = threading.Event()
    answered = threading.Event()

    def watch() -> None:
        deadline = now() + timeout
        while not done.is_set() and now() < deadline:
            stamp = probe_fn(host)
            if stamp is not None:
                answered.set()
                say(f"ComfyUI answering: {stamp.line()}")
                if open_browser is not None:
                    open_browser(host.url)
                return
            sleep(POLL_SECONDS)

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()

    def give_up(message: str, *, egress: bool = False,
                stop_first: tuple[str, str] | None = None) -> LifecycleError:
        return _give_up(host, tunnel_dir, say, message, egress=egress,
                        stop_first=stop_first)

    holder = _port_holder(gc, host)
    if holder is not None:
        pid, name = holder
        # ComfyUI's own message for this is "Port 8188 is already in use" plus a
        # database lock error, neither of which says what is holding it or that
        # this tool is usually the one that left it there.
        raise give_up(
            f"something is already listening on {host.name}'s ComfyUI port "
            f"({name}, pid {pid}), so a second one cannot start. If it is a "
            f"ComfyUI, {host.url} already reaches it.",
            stop_first=holder)

    say(f"starting ComfyUI on {host.name} — its log follows. Ctrl-C to stop it.")
    if host.is_remote:
        # ComfyUI will announce its own address a minute from now — "To see the
        # GUI go to http://127.0.0.1:8188" — which is true on the box and wrong
        # on this machine, where 8188 is the local install. It is the last line
        # a tester reads, so say the right one alongside it.
        say(f"when it says 127.0.0.1:{COMFYUI_PORT}, on this machine that is "
            f"{host.url}")
    try:
        code = gc.ssh(host.gce_instance, host.gce_zone, host.gce_project,
                      launch_command(host), stream=True)
    finally:
        # Otherwise the watcher outlives a failed launch and opens a browser onto
        # a URL that never answered.
        done.set()
        # And otherwise the ComfyUI we started outlives us: a launch that dies
        # after binding leaves a process holding port 8188 on the box, and every
        # later launch fails with a port conflict that names neither the process
        # nor the tool that left it. Only ever a process we started ourselves.
        _stop_ours(gc, host, say)

    if code == NO_PYTHON_EXIT:
        raise give_up(
            f"there is no Python on {host.name} to run ComfyUI with (NO_PYTHON), so "
            "it could not be started.")
    if code not in (0, INTERRUPTED_EXIT):
        # An install is not the same as a working install. `ensure_installed`
        # asks whether main.py is on the box, so a machine built from a snapshot
        # taken before a dependency was added reports "already installed" and
        # then dies importing it — which is what happened on 2026-08-27, on
        # `sqlalchemy`. Reporting that accurately is not the same as being
        # usable, so try the one repair that fixes it, once, and say so.
        if repair:
            from .provision import repair_command

            say("")
            say("that looks like a missing dependency rather than a broken "
                "install — installing its requirements and trying once more")
            try:
                repaired = gc.ssh(host.gce_instance, host.gce_zone, host.gce_project,
                                  repair_command(host), stream=True)
            except GcloudError as exc:
                raise give_up(
                    f"ComfyUI on {host.name} exited with {code}, and its "
                    f"requirements could not be installed either: {exc}") from exc
            if repaired != 0:
                # Relaunching after a failed repair prints the identical
                # traceback a second time and teaches nothing. The usual cause
                # is that the box has no way out: IAP gets you in, and an
                # instance with no external address and no Cloud NAT cannot
                # reach pypi at all.
                raise give_up(
                    f"ComfyUI on {host.name} is missing a dependency, and "
                    f"installing its requirements failed (exit {repaired}). "
                    f"Its log is above.", egress=True)
            return serve(
                gc, host, say, open_browser=open_browser, probe_fn=probe_fn,
                sleep=sleep, now=now, timeout=timeout, tunnel_dir=tunnel_dir,
                repair=False,
            )
        raise give_up(f"ComfyUI on {host.name} exited with {code} instead of starting.")

    # Exit 0 is not the same as having served. A launch that ends immediately —
    # the wrong directory, a missing requirement printed and gone — returns 0,
    # and reporting that as success is the same lie as calling a booted VM "up".
    # The watcher polls, so it can miss a short life: ask once more before saying
    # it never happened.
    if code == 0 and not answered.is_set() and probe_fn(host) is None:
        stand_down(host, tunnel_dir, say)
        raise LifecycleError(
            f"ComfyUI on {host.name} exited without ever answering on {host.url}. "
            "The machine is up and billing.",
            fix=("read the log above, then get onto the machine:\n        "
                 + how_to_get_in(host)
                 + "\n        or stop paying for it:\n        "
                 + stop_paying(host)),
        )
    return code


def _family(host: Host) -> str:
    """"Windows Server 2022" -> "windows". Enough to say "the same kind of box"."""
    words = (host.os or "").lower().split()
    return words[0] if words else ""


def alternatives(hosts: list[Host], unavailable: Host) -> list[Host]:
    """Where else a tester could work, when one machine will not start.

    A capacity shortage is a fact about one card in one zone, so it says nothing
    about the other boxes — and being told "no capacity" without being told where
    to go next is the moment a test session stops. Same operating system first,
    because someone who asked for Windows usually needs Windows; then anywhere
    but the zone that just refused; local last, since MPS is not CUDA and it
    answers a different question.
    """
    def rank(host: Host) -> tuple:
        return (
            1 if host.kind == "local" else 0,
            0 if _family(host) == _family(unavailable) else 1,
            1 if host.gce_zone and host.gce_zone == unavailable.gce_zone else 0,
            host.name,
        )

    return sorted((h for h in hosts if h.name != unavailable.name), key=rank)


def running_elsewhere(
    gc: Gcloud,
    hosts: list[Host],
    target: Host,
    *,
    tunnel_dir: Path | None = None,
) -> list[tuple[Host, str]]:
    """The other cloud boxes that are on, and why we say so.

    Changing machine is two acts, and the one people forget is the first: a box
    left RUNNING bills whether or not anything is tunnelled to it. Both signs are
    read, because they fail differently — a tunnel opened here is local evidence,
    while a box someone started in the console has no tunnel and is the expensive
    case. Local hosts never appear: this tool did not start the local ComfyUI and
    does not get to stop it.
    """
    found: list[tuple[Host, str]] = []
    for host in hosts:
        if host.name == target.name or host.kind == "local":
            continue
        reasons = []
        if gc.instance_status(host.gce_instance, host.gce_zone, host.gce_project) == RUNNING:
            reasons.append("running")
        if tunnel_status(host.name, tunnel_dir).running:
            reasons.append("tunnelled")
        if reasons:
            found.append((host, " and ".join(reasons)))
    return found


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
        # A host list that says `local` while naming a cloud instance is refused
        # when it is parsed — but a Host is also built in code, by `move`, by
        # `switch` and by every test, and this is where the money is decided.
        # Saying "left running" about a GPU box that is still billing is the most
        # expensive thing this function could get wrong, so it is checked here
        # too rather than trusted from upstream.
        named = [field for field in (host.gce_instance, host.gce_zone, host.gce_project)
                 if field]
        if named:
            raise LifecycleError(
                f"{host.name} says kind = 'local' but names a cloud instance "
                f"({', '.join(named)}). Refusing to report it as stopped: if that "
                f"machine is running, it is billing.",
                fix=(f"fix the entry in your host list — a cloud box is "
                     f"kind = 'gce' — then:\n        comfy-qat host down {host.name}"),
            )
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
