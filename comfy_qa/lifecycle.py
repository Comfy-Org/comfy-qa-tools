"""Bringing a machine up, and putting it away.

"Up" means ComfyUI answers. A VM that has booted but serves nothing is the
failure this is written to avoid: it looks like success, bills like success, and
you find out only when a test does something strange.

Every failure after the machine has been started says how to stop paying for it.
A message that only explains what went wrong leaves a GPU box running all night.
That line used to be hand-copied into ten fix strings, eight-space indent
included; it comes from `_with_the_bill` now.

Nothing here prints. Every one of these functions is handed a `say` and reports
through it, so the same flow reads the same whether it is driven by `go`, by
`switch`, or by a test collecting lines in a list.

**ComfyUI runs on the box, so the terminal does not have to.** The log used to be
streamed back over SSH, which is why `go` owned a terminal until Ctrl-C — and why
two machines could not be used at once, which is the ordinary case: Windows in
one browser tab, Linux in another. `start_detached` launches it on the box with
its output going to a file there, waits until it really answers, prints the URL
and hands the prompt back. `serve` is the old behaviour, kept under `--follow`,
where Ctrl-C still reaches ComfyUI.

Detaching moves one thing and one thing only: *where the log goes*. It does not
move where "up" is decided. A launch that returned as soon as the box said
STARTED would be the booted-VM lie again, one level down — started, billing, and
serving nothing — so a detached launch is not finished until ComfyUI has answered
on the tunnel, exactly as before.
"""

from __future__ import annotations

import http.client
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import say as output
from .config import Host
from .gcloud import Gcloud, GcloudError
from .stamp import ProbeError, Stamp, fetch
from .tunnel import (
    BACKEND_NOT_LISTENING,
    COMFYUI_PORT,
    TunnelError,
    close_tunnel,
    log_file,
    open_tunnel,
    status as tunnel_status,
)

# A step whose command streams its own log gets a rarer tick than a silent one:
# the log is already the evidence it is alive, and a line every half-minute on top
# of pip's output is noise. This is for the minutes when pip goes quiet fetching a
# two-gigabyte wheel, which is where "is it hung?" actually gets asked.
STREAM_TICK_SECONDS = 60

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

    def __init__(self, message: str, fix: str | None = None, kind: str = "error",
                 zones: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.fix = fix
        self.kind = kind
        # Where Google said there is capacity, carried rather than left for a
        # caller to read back out of the prose it just formatted.
        self.zones = zones


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


def _wait(check: Callable[[], bool], *, timeout: int, sleep=None, now=None,
          tick: Callable[[], None] | None = None) -> bool:
    """Poll until `check` passes or the window closes.

    `tick` is called on every pass so a wait that runs for minutes can say it is
    still running. It is a no-op cost when nothing is due — `Slow.tick` decides —
    which is why the interval lives there and not in this loop.
    """
    sleep = sleep or _pause
    now = now or _clock
    deadline = now() + timeout
    while True:
        if check():
            return True
        if now() >= deadline:
            return False
        if tick is not None:
            tick()
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
        # One command per line, no indent: `say.fix` puts every fix under the
        # same eight-space rule when it is printed, so writing the alignment in
        # here as well is how the two used to drift apart.
        return (
            f"gcloud compute reset-windows-password {host.gce_instance} {where}\n"
            f"gcloud compute start-iap-tunnel {host.gce_instance} 3389 "
            f"--local-host-port=localhost:33389 {where}\n"
            "then point Remote Desktop at localhost:33389"
        )
    return f"gcloud compute ssh {host.gce_instance} --tunnel-through-iap {where}"


def stop_paying(host: Host) -> str:
    """The command that stops the bill."""
    return f"comfy-qat down {host.name}"


def _with_the_bill(host: Host, *advice: str) -> str:
    """A fix that ends by saying how to stop paying for the machine.

    The machine is on and billing by the time most of these failures can happen,
    and a fix that does not say so is how a box runs all night. Ten sites carried
    that by copying `"\n        or stop paying for it:\n        "` into their own
    string; this is the one place it is written, so the wording and the alignment
    can no longer drift apart. The `# closes the tunnel and stops the box`
    comment went with them: the label already says what the command is for.
    """
    lines = [line for line in advice if line]
    tail = f"{'or ' if lines else ''}stop paying for it: {stop_paying(host)}"
    return output.fix(*lines, tail)


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
        # A boot is the first place this tool can go quiet for minutes, so the
        # step is timed: it says it is still waiting while it waits, and how long
        # it took when the box answers.
        waking = output.slow(
            f"{host.name} is {'stopped' if state == 'TERMINATED' else state.lower()}"
            " — starting it",
            expect=f"up to {boot_timeout}s",
            emit=say, clock=now, background=False,
        ).start()
        try:
            gc.start_instance(host.gce_instance, host.gce_zone, host.gce_project)
        except GcloudError as exc:
            waking.give_up()
            # Classify on everything gcloud printed. The one-line summary for a
            # compute error is `---`, which matches nothing.
            if is_capacity_failure(exc.raw):
                elsewhere = suggested_zones(exc.raw)
                if elsewhere:
                    advice = output.fix(
                        f"Google says {', '.join(elsewhere)} has capacity right now:",
                        f"comfy-qat move {host.name} --to {elsewhere[0]}",
                    )
                else:
                    advice = output.fix(
                        "wait and try later, or move the box to another zone:",
                        f"comfy-qat move {host.name}",
                    )
                raise LifecycleError(
                    # "not a fault on your side" is not reassurance for its own
                    # sake: without it people spend an hour auditing their quota
                    # and billing for a shortage that has nothing to do with
                    # either. It changes the next action, so it stays.
                    f"Google has no {host.gpu or 'GPU'} capacity in {host.gce_zone} "
                    f"right now, so {host.name} cannot start. This is not a fault on "
                    "your side, and retrying in the same zone will not help.",
                    kind=STOCKOUT,
                    fix=advice,
                    zones=tuple(elsewhere),
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

        up = _wait(is_running, timeout=boot_timeout, sleep=sleep, now=now,
                   tick=waking.tick)
        if not up:
            waking.give_up()
            if last_error is not None:
                raise LifecycleError(
                    f"could not tell whether {host.name} reached RUNNING: {last_error}",
                    fix=_with_the_bill(host, last_error.fix),
                ) from last_error
            raise LifecycleError(
                f"{host.name} did not reach RUNNING within {boot_timeout}s. It "
                "was asked to start, so it may be billing already.",
                fix=_with_the_bill(host, "check it in the console, then try again"),
            )
        waking.done("running")
    else:
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
            if getattr(exc, "kind", "") == BACKEND_NOT_LISTENING:
                # Not a failure to report — an order-of-operations fact. gcloud
                # tests the connection before it will serve and refuses when the
                # far port has no listener, so a tunnel cannot exist before
                # ComfyUI is started. This used to be raised, which made `go`
                # impossible on any box that was not already serving: it opened
                # the tunnel first, the tunnel refused, and the launch it was
                # about to do was the very thing that would have fixed it.
                say("nothing is listening on the machine yet, so there is nothing "
                    "to tunnel to — starting ComfyUI first")
                raise LifecycleError(
                    f"ComfyUI is not running on {host.name} yet",
                    kind=COMFYUI_ABSENT,
                    fix=f"comfy-qat go {host.name}",
                ) from exc
            raise LifecycleError(
                f"could not open the tunnel to {host.name}: {exc}",
                kind=TUNNEL_DOWN,
                fix=_with_the_bill(host, exc.fix),
            ) from exc
        say(f"tunnel open: {host.url}")

    stamp = None
    deadline = now() + comfy_timeout
    answering = output.slow(f"waiting for ComfyUI on {host.url}",
                            expect=f"up to {comfy_timeout}s",
                            emit=say, clock=now, background=False).start()
    while True:
        stamp = probe_fn(host)
        if stamp is not None:
            break
        # Silence here has three causes that look identical: nothing is running,
        # the tunnel died, or a firewall two hops away is dropping it. The third
        # is the one nobody guesses, so it is ruled out once — after a probe has
        # failed, never before, because a box that already answers should not
        # have its firewalls touched at all.
        # A dead tunnel and an absent ComfyUI look identical from here — both are
        # silence on the port — and only one of them is fixed on the box.
        if not tunnel_status(host.name, tunnel_dir).running:
            answering.give_up()
            raise LifecycleError(
                f"the tunnel to {host.name} closed, so nothing is listening on "
                f"{host.url}. ComfyUI was never reached.",
                kind=TUNNEL_DOWN,
                fix=_with_the_bill(
                    host,
                    f"read what gcloud said in {log_file(host.name, tunnel_dir)}, then:",
                    f"comfy-qat open {host.name}",
                ),
            )
        if now() >= deadline:
            break
        answering.tick()
        sleep(POLL_SECONDS)

    if stamp is None:
        answering.give_up()
        raise LifecycleError(
            f"{host.name} is running and tunnelled, but ComfyUI is not answering "
            f"on {host.url}. The machine is up and billing; ComfyUI is not "
            "installed or not started.",
            kind=COMFYUI_ABSENT,
            fix=_with_the_bill(
                host,
                "get onto the machine and install or start ComfyUI:",
                how_to_get_in(host),
            ),
        )

    # `give_up`, not `done`: the next line *is* the completion, and a separate
    # "answered in 2m10s" above it would be the same fact twice.
    answering.give_up()
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
                    # "Waiting will not fix this" went: nothing is waiting any
                    # more by the time this is printed. The bill stayed, because
                    # the machine is on and nobody reading this knows that yet.
                    f"gcloud is not signed in, so {host.name} cannot be reached: "
                    f"{exc}. The machine is running and billing.",
                    fix=_with_the_bill(host, exc.fix or "gcloud auth login"),
                ) from exc
            if now() >= deadline:
                stand_down(host, tunnel_dir, say)
                raise LifecycleError(
                    f"{host.name} is running but not accepting commands after {timeout}s: {exc}",
                    fix=_with_the_bill(host, how_to_get_in(host)),
                ) from exc
            if not said_waiting:
                say("waiting for the machine to accept commands — Windows takes a few minutes")
                said_waiting = True
            sleep(POLL_SECONDS)


def ensure_installed(gc: Gcloud, host: Host, say: Callable[[str], None],
                     *, tunnel_dir: Path | None = None) -> None:
    """Make sure ComfyUI exists on the box, installing it if it does not."""
    from .provision import (
        check_command, cuda_command, install_command, root_for, torch_index_for,
    )

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

    # The install streams its own log, so this ticks rarely: the log is the
    # evidence it is alive, and the tick is there for the minutes when pip has
    # gone quiet fetching a two-gigabyte wheel. It also reports how long the
    # whole thing took, which is the number people actually want afterwards.
    installing = output.slow("ComfyUI is not there — installing it",
                             expect="several minutes; torch is the slow part",
                             emit=say, every=STREAM_TICK_SECONDS).start()
    try:
        try:
            reported = gc.ssh_output(host.gce_instance, host.gce_zone,
                                     host.gce_project, cuda_command(host))
        except GcloudError:
            reported = None
        installed = gc.ssh(host.gce_instance, host.gce_zone, host.gce_project,
                           install_command(host, torch_index_for(str(reported or ""))),
                           stream=True)
    except GcloudError as exc:
        installing.give_up()
        raise give_up(
            f"the ComfyUI install on {host.name} did not finish: {exc}") from exc

    if installed != 0:
        installing.give_up()
        raise give_up(
            f"the ComfyUI install on {host.name} did not finish (exit {installed})")
    installing.done("installed")

    # An install script that exits 0 having installed nothing is not a theory:
    # on Windows a failed clone leaves every later step running in the wrong
    # directory, and the whole thing still ends "install complete".
    if "INSTALLED" not in look():
        raise give_up(
            f"the ComfyUI install on {host.name} did not finish: it reported success "
            f"but {root_for(host)} still has no ComfyUI in it")


# 130 is a program stopped with Ctrl-C. That is a person finishing, not a fault.
INTERRUPTED_EXIT = 130


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

    # Each possibility is a labelled block, because they used to run together:
    # the egress command was followed straight by the RDP recipe, so three
    # unrelated commands read as one four-step procedure.
    blocks: list[str] = []
    if egress and host.is_remote:
        # Not guessable from the box: everything reaches it fine, so nobody
        # thinks to check whether it can reach anything. IAP gets you in; an
        # instance with no external address and no Cloud NAT cannot get out,
        # which is why a pypi timeout is the sign to look for.
        blocks.append(output.fix(
            "if pypi timed out, the box has no route out — give it one and run "
            "this again:",
            f"gcloud compute instances add-access-config {host.gce_instance} "
            f"--zone={host.gce_zone} --project={host.gce_project}",
        ))
    if stop_first is not None and host.is_remote:
        from .provision import stop_command

        pid, _ = stop_first
        blocks.append(output.fix(
            "if that is a ComfyUI you no longer want, stop it and run this again:",
            f"gcloud compute ssh {host.gce_instance} --zone={host.gce_zone} "
            f"--project={host.gce_project} --tunnel-through-iap "
            f"--command='{stop_command(host, pid)}'",
        ))
    blocks.append(output.fix(
        f"{'or ' if blocks else ''}get onto the machine and look:",
        how_to_get_in(host),
    ))
    return LifecycleError(message, fix=_with_the_bill(host, *blocks))


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
        NO_COMFYUI, NO_TORCH, READY, TORCH_NO_CUDA, cuda_command, repair_command,
        root_for, torch_index_for, verify_command,
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
        say(f"torch on {host.name} is a CPU-only build and cannot see the "
            f"{host.gpu or 'GPU'} — installing the CUDA build instead")
    else:
        return

    # Which CUDA the box's driver supports decides which torch to fetch. Pinning
    # that number is how an L4 was told it "needs pytorch with cu130 or higher to
    # use optimized CUDA operations" — installed, working, and quietly slower
    # than the hardware allows.
    try:
        reported = gc.ssh_output(host.gce_instance, host.gce_zone,
                                 host.gce_project, cuda_command(host))
    except GcloudError:
        reported = None
    index = torch_index_for(str(reported or ""))
    fetching = output.slow(
        f"installing torch from {index.rsplit('/', 1)[-1]}, which is what this "
        "box's driver supports",
        expect="several minutes", emit=say, every=STREAM_TICK_SECONDS).start()

    try:
        code = gc.ssh(host.gce_instance, host.gce_zone, host.gce_project,
                      repair_command(host, force_torch=TORCH_NO_CUDA in state,
                                     index=index),
                      stream=True)
    except GcloudError as exc:
        fetching.give_up()
        raise give_up(f"could not install torch on {host.name}: {exc}", egress=True) from exc
    if code != 0:
        fetching.give_up()
        raise give_up(
            f"torch could not be installed on {host.name} (exit {code}), so "
            "ComfyUI cannot use its GPU. Its log is above.", egress=True)
    fetching.done("installed")


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
        announced = False
        while not done.is_set() and now() < deadline:
            # The tunnel can only exist once ComfyUI is listening — gcloud tests
            # the connection before it will serve — so it is opened here, while
            # ComfyUI starts, rather than before the launch. That ordering is the
            # difference between a box you can open in a browser and one that
            # runs perfectly and is unreachable.
            if host.is_remote and not tunnel_status(host.name, tunnel_dir).running:
                try:
                    open_tunnel(host, tunnel_dir)
                except TunnelError:
                    sleep(POLL_SECONDS)   # usually "not listening yet". Ask again.
                    continue
                if not announced:
                    announced = True
                    say(f"tunnel open: {host.url}")

            stamp = probe_fn(host)
            if stamp is not None:
                answered.set()
                say(f"ComfyUI answering: {stamp.line()}")
                say(f"open {host.url}")
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
        # A held port is only a problem when what holds it is not the thing you
        # wanted. Watched this refuse a ComfyUI that was answering perfectly:
        # the run printed "ComfyUI answering: ... NVIDIA L4", printed the URL,
        # and then tore down its own tunnel and called it a collision. If it
        # serves, it is not in the way — it is the answer.
        serving = probe_fn(host)
        if serving is not None:
            say(f"ComfyUI is already running on {host.name} ({name}, pid {pid}) "
                "— using it rather than starting a second one")
            say(f"ComfyUI answering: {serving.line()}")
            say(f"open {host.url}")
            if open_browser is not None:
                open_browser(host.url)
            return 0

        # ComfyUI's own message for this is "Port 8188 is already in use" plus a
        # database lock error, neither of which says what is holding it or that
        # this tool is usually the one that left it there.
        raise give_up(
            f"something else holds {host.name}'s ComfyUI port ({name}, pid {pid}) "
            "and is not answering as ComfyUI, so a second one cannot start.",
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
            f"there is no Python on {host.name} to run ComfyUI with (NO_PYTHON)")
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
            fix=_with_the_bill(host, "read the log above, then get onto the machine:",
                               how_to_get_in(host)),
        )
    return code


# How often, while waiting for a detached launch, the box is asked whether its
# ComfyUI is even still there. Every poll would be an SSH round trip for a
# question whose answer changes once; never asking is three minutes of GPU time
# spent waiting for a process that died in four seconds.
ALIVE_EVERY = 30

# What a ComfyUI that could not import something says on its way out. Read from
# the log on the box, because a detached launch's exit code is the *launcher's*,
# and the launcher succeeds perfectly at starting something that then dies.
_MISSING_MODULE = re.compile(r"ModuleNotFoundError|No module named|ImportError")


def _open_forward(host: Host, tunnel_dir: Path | None, say: Callable[[str], None]) -> bool:
    """Open the tunnel if it is not already open. Says so only when it opens one.

    Ordering, not tidiness: gcloud tests the connection before it will serve and
    refuses when the far port has no listener, so the forward cannot exist until
    ComfyUI is up. That is why this is called from inside the wait rather than
    before the launch — the same reason `serve` opens it from its watcher, and
    undoing it makes `go` impossible on any box that is not already serving.
    """
    if not host.is_remote:
        return True
    if tunnel_status(host.name, tunnel_dir).running:
        return True
    try:
        open_tunnel(host, tunnel_dir)
    except TunnelError:
        return False        # usually "not listening yet". Ask again next time.
    say(f"tunnel open: {host.url}")
    return True


def _still_alive(gc: Gcloud, host: Host) -> bool | None:
    """Is ComfyUI still running on the box? None means the box would not say.

    None is not False. A box that cannot be asked has told us nothing, and
    reporting nothing as "it died" would end a wait that was going to succeed.
    """
    from .provision import GONE, alive_command

    try:
        answer = gc.ssh_output(host.gce_instance, host.gce_zone, host.gce_project,
                               alive_command(host))
    except GcloudError:
        return None
    answer = str(answer or "").strip()
    if not answer:
        return None
    return GONE not in answer


def _log_tail(gc: Gcloud, host: Host, lines: int = 20) -> str:
    """The end of the detached ComfyUI's log, read off the box.

    The whole cost of detaching is that the startup log is no longer on this
    terminal, so a failure that says "it never answered" and nothing else is a
    worse failure than the one it replaced. This is what buys that back.
    """
    from .provision import logs_command

    try:
        text = gc.ssh_output(host.gce_instance, host.gce_zone, host.gce_project,
                             logs_command(host, tail=lines, follow=False))
    except GcloudError:
        # No log, or an unreachable box. Either way there is nothing to quote,
        # and failing to read a log is not worth replacing the real failure.
        return ""
    return str(text or "").strip()


def _repair(gc: Gcloud, host: Host, say: Callable[[str], None], give_up) -> None:
    """Install an existing checkout's requirements once, and only once.

    The same repair `serve` does on a non-zero exit, reached differently: a
    detached ComfyUI that dies on a missing import exits *after* the launcher has
    already returned 0, so the evidence is in the log on the box rather than in
    an exit code. Watched exactly that on 2026-08-27, on `sqlalchemy`.
    """
    from .provision import repair_command

    say("")
    say("that looks like a missing dependency rather than a broken "
        "install — installing its requirements and trying once more")
    try:
        repaired = gc.ssh(host.gce_instance, host.gce_zone, host.gce_project,
                          repair_command(host), stream=True)
    except GcloudError as exc:
        raise give_up(
            f"ComfyUI on {host.name} would not start, and its requirements could "
            f"not be installed either: {exc}") from exc
    if repaired != 0:
        # Relaunching after a failed repair prints the identical traceback again
        # and teaches nothing. The usual cause is that the box has no way out.
        raise give_up(
            f"ComfyUI on {host.name} is missing a dependency, and "
            f"installing its requirements failed (exit {repaired}). "
            f"Its log is above.", egress=True)


def start_detached(
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
    """Launch ComfyUI on the box, leave it running there, and give the prompt back.

    The everyday launch. ComfyUI has always run on the box; the terminal was
    occupied only because its log was streamed back over SSH, and that one
    convenience made two machines at once impossible — Windows in one browser tab
    and Linux in another is the ordinary case, not an exotic one. So the log goes
    to a file on the box and `host logs` reads it.

    What does **not** change is when this returns. "Started" is not "serving":
    a launch that came back on the box's say-so would leave a GPU machine billing
    while ComfyUI failed to import something, which is the booted-VM lie this
    whole module exists to refuse. So the tunnel is opened as soon as there is
    something to tunnel to, ComfyUI is asked until it answers, and only an answer
    counts.

    A failure here reads the log off the box and quotes it, because the tester
    can no longer see it scroll past — and stops the ComfyUI this run started, so
    the next attempt is not refused by a port its own predecessor is holding.
    """
    from .provision import NO_PYTHON_EXIT, launch_detached_command, log_for

    sleep = sleep or _pause
    now = now or _clock
    probe_fn = probe_fn or probe

    def give_up(message: str, *, egress: bool = False,
                stop_first: tuple[str, str] | None = None) -> LifecycleError:
        return _give_up(host, tunnel_dir, say, message, egress=egress,
                        stop_first=stop_first)

    holder = _port_holder(gc, host)
    if holder is not None:
        pid, name = holder
        # A held port is only a problem when what holds it is not the thing you
        # wanted. If it serves, it is not in the way — it is the answer.
        _open_forward(host, tunnel_dir, say)
        serving = probe_fn(host)
        if serving is not None:
            say(f"ComfyUI is already running on {host.name} ({name}, pid {pid}) "
                f"— using it rather than starting a second one")
            say(f"ComfyUI answering: {serving.line()}")
            if open_browser is not None:
                open_browser(host.url)
            return 0
        raise give_up(
            f"something is already listening on {host.name}'s ComfyUI port "
            f"({name}, pid {pid}), and it is not answering as ComfyUI, so a "
            f"second one cannot start.",
            stop_first=holder)

    say(f"starting ComfyUI on {host.name} — it stays running on the box after "
        f"this command returns")
    say(f"its log is {log_for(host)} on the box: comfy-qat logs {host.name}")
    if host.is_remote:
        # ComfyUI announces its own address — "To see the GUI go to
        # http://127.0.0.1:8188" — which is true on the box and wrong here, where
        # 8188 is the local install. It is the first line of the log `host logs`
        # will show, so say the right one alongside it.
        say(f"when it says 127.0.0.1:{COMFYUI_PORT}, on this machine that is "
            f"{host.url}")

    code = gc.ssh(host.gce_instance, host.gce_zone, host.gce_project,
                  launch_detached_command(host), stream=True)
    if code == NO_PYTHON_EXIT:
        raise give_up(
            f"there is no Python on {host.name} to run ComfyUI with (NO_PYTHON), so "
            "it could not be started.")
    if code != 0:
        # The launcher itself failed, which on Linux it essentially cannot: the
        # detached form exits as soon as the process is spawned. Treated as the
        # foreground launch treats it, because the one repair that fixes it is
        # cheap and the alternative is handing back an exit code and no advice.
        if repair:
            _repair(gc, host, say, give_up)
            return start_detached(
                gc, host, say, open_browser=open_browser, probe_fn=probe_fn,
                sleep=sleep, now=now, timeout=timeout, tunnel_dir=tunnel_dir,
                repair=False,
            )
        raise give_up(
            f"ComfyUI on {host.name} could not be launched (exit {code}).")

    stamp = None
    deadline = now() + timeout
    asked_alive = now()
    while True:
        if _open_forward(host, tunnel_dir, say):
            stamp = probe_fn(host)
            if stamp is not None:
                break
        if now() >= deadline:
            break
        if now() - asked_alive >= ALIVE_EVERY:
            asked_alive = now()
            if _still_alive(gc, host) is False:
                say(f"ComfyUI is no longer running on {host.name} — it stopped "
                    f"before it ever answered")
                break
        sleep(POLL_SECONDS)

    if stamp is None:
        return _never_answered(gc, host, say, tunnel_dir=tunnel_dir, repair=repair,
                               open_browser=open_browser, probe_fn=probe_fn,
                               sleep=sleep, now=now, timeout=timeout)

    say(f"ComfyUI answering: {stamp.line()}")
    if open_browser is not None:
        open_browser(host.url)
    return 0


def _never_answered(gc: Gcloud, host: Host, say: Callable[[str], None], *,
                    tunnel_dir, repair: bool, **again) -> int:
    """A detached launch that started something and never got an answer.

    Three things have to happen here and the order matters. The log is read off
    the box first, while the box is still reachable, because it is the only
    evidence left once the terminal has stopped carrying it. Then whatever this
    run started is stopped, so the next attempt is not refused by a port its own
    predecessor is holding — the tidy-up that used to be a courtesy and is now
    the thing standing between a tester and "Port 8188 is already in use".
    """
    def give_up(message: str, *, egress: bool = False,
                stop_first: tuple[str, str] | None = None) -> LifecycleError:
        return _give_up(host, tunnel_dir, say, message, egress=egress,
                        stop_first=stop_first)

    tail = _log_tail(gc, host)
    if tail:
        say("")
        say(f"the last of its log on {host.name}:")
        for line in tail.splitlines():
            say(f"  | {line}")
    _stop_ours(gc, host, say)

    if repair and _MISSING_MODULE.search(tail):
        _repair(gc, host, say, give_up)
        return start_detached(gc, host, say, tunnel_dir=tunnel_dir, repair=False,
                              **again)

    stand_down(host, tunnel_dir, say)
    raise LifecycleError(
        f"ComfyUI on {host.name} exited without ever answering on {host.url}. "
        "The machine is up and billing.",
        kind=COMFYUI_ABSENT,
        fix=_with_the_bill(
            host,
            "read its whole log on the box:",
            f"comfy-qat logs {host.name} --tail 100",
            "or get onto the machine:",
            how_to_get_in(host),
        ),
    )


def read_logs(
    gc: Gcloud,
    host: Host,
    say: Callable[[str], None],
    *,
    tail: int = 200,
    follow: bool = True,
) -> int:
    """Show a running box's ComfyUI log — the last lines, or as it is written.

    Reading a file and nothing else. Ending a follow stops reading and stops
    nothing else, which is the whole difference between this and `go --follow`,
    where Ctrl-C reaches ComfyUI itself.

    Every state that is not "there is a log" is answered rather than waited on. A
    command that hangs against a stopped box is the worst of the three, because
    the box it is silently waiting for is one you might still be paying for.
    """
    from .provision import NO_LOG_EXIT, log_for, logs_command

    if host.kind == "local":
        raise LifecycleError(
            f"{host.name} is this machine, and this tool did not start its ComfyUI, "
            f"so there is no log of its own to follow.",
            fix=output.fix(
                "read the terminal you started it in, or start it there:",
                f"~/ComfyUI/venv/bin/python ~/ComfyUI/main.py --port {host.port} "
                "--listen 127.0.0.1",
            ),
        )

    try:
        state = gc.instance_status(host.gce_instance, host.gce_zone, host.gce_project)
    except GcloudError as exc:
        raise LifecycleError(str(exc), fix=exc.fix) from exc
    if state != RUNNING:
        raise LifecycleError(
            f"{host.name} is not running, so it has no ComfyUI and no log to "
            f"follow. Whatever it was writing stopped when the machine did.",
            fix=f"comfy-qat go {host.name}   # start the box and ComfyUI on it",
        )

    try:
        code = gc.ssh(host.gce_instance, host.gce_zone, host.gce_project,
                      logs_command(host, tail=tail, follow=follow), stream=True)
    except GcloudError as exc:
        raise LifecycleError(
            f"could not read the ComfyUI log on {host.name}: {exc}",
            fix=_with_the_bill(host, how_to_get_in(host)),
        ) from exc

    if code == NO_LOG_EXIT:
        raise LifecycleError(
            f"there is no ComfyUI log at {log_for(host)} on {host.name}, so nothing "
            f"has started ComfyUI there. The machine is running and billing.",
            fix=_with_the_bill(
                host,
                f"comfy-qat go {host.name}   # start it, and this will have "
                "something to read",
            ),
        )
    return code


def _tool_invocation() -> str:
    """How to run this tool from a shell that is not this one.

    A new window is a login shell with its own PATH, so `comfy-qat` may not be on
    it — this tool is routinely installed in a venv that only the current shell
    has activated. The full path is used when there is one, and the module entry
    point when there is not, because a window that opens onto `command not found`
    is worse than being told plainly that no window can be opened.
    """
    import shlex
    import shutil
    import sys

    found = shutil.which("comfy-qat")
    if found:
        return shlex.quote(found)
    argv0 = Path(sys.argv[0]) if sys.argv and sys.argv[0] else None
    if argv0 is not None and argv0.name.startswith("comfy-qat") and argv0.exists():
        return shlex.quote(str(argv0.resolve()))
    return f"{shlex.quote(sys.executable)} -m comfy_qa"


def _applescript_string(text: str) -> str:
    """One AppleScript string literal. Backslash and quote are the only escapes."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def in_a_new_window(rest: list[str], say: Callable[[str], None]) -> None:
    """Run `comfy-qat <rest>` in a new terminal window instead of this one.

    A convenience, kept deliberately small. macOS Terminal through `osascript` is
    the one case worth supporting here, and everything else is told plainly that
    it cannot rather than being half-served: a window that silently does not
    appear, on a command whose job is to start a GPU box, is a machine you are
    paying for and cannot see. The refusal prints the exact command, so the
    fallback is one paste rather than a reconstruction.
    """
    import shlex
    import shutil
    import subprocess
    import sys

    line = " ".join([_tool_invocation(), *(shlex.quote(word) for word in rest)])
    by_hand = output.fix("open a terminal window and run:", line)

    if sys.platform != "darwin" or shutil.which("osascript") is None:
        raise LifecycleError(
            "--new-window can only open a macOS Terminal window, and this is not a "
            "Mac with osascript on it. Nothing was started.",
            fix=by_hand,
        )

    script = f"tell application \"Terminal\" to do script {_applescript_string(line)}"
    try:
        done = subprocess.run(["osascript", "-e", script],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise LifecycleError(
            f"could not open a new Terminal window: {exc}. Nothing was started.",
            fix=by_hand,
        ) from exc
    if done.returncode != 0:
        raise LifecycleError(
            "could not open a new Terminal window: "
            f"{done.stderr.strip() or 'osascript would not say why'}. "
            "Nothing was started.",
            fix=by_hand,
        )
    say(f"opened a new Terminal window running: {line}")


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
    """Close the tunnel and stop the machine, so it stops costing money.

    `go` now leaves a ComfyUI running on the box, so "down" has one more thing to
    be true about — and it is, without doing anything extra: stopping the
    instance stops everything on it, ComfyUI included. There is nothing to reach
    over SSH and nothing that can be missed, which is why this is the honest
    place for that to happen rather than a tidy-up somewhere earlier.

    `--keep-running` is the exception and says so. It leaves the machine on
    deliberately, so it leaves ComfyUI on with it; the next `host go` finds that
    ComfyUI and uses it rather than starting a second one.
    """
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
                fix=output.fix(
                    "fix the entry in your host list — a cloud box is kind = 'gce' "
                    "— then:",
                    f"comfy-qat down {host.name}",
                ),
            )
        say("local ComfyUI left running — this tool did not start it")
        return

    if keep_running:
        say(f"{host.name} left running — it is still billing")
        say(f"any ComfyUI on it is still running too: comfy-qat logs {host.name}")
        return

    try:
        gc.stop_instance(host.gce_instance, host.gce_zone, host.gce_project)
    except GcloudError as exc:
        raise LifecycleError(f"could not stop {host.name}: {exc}", fix=exc.fix) from exc
    say(f"{host.name} stopped")
