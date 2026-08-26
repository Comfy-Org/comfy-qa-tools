"""`comfy-qat host` — operate the machines you test on.

Two halves. `list`, `init` and the config rules are offline and never call
anything: declaring a machine is not the same act as touching it, and the rules
that stop you reading the wrong box are worth enforcing before a network exists.
`discover`, `up`, `open`, `down`, `go`, `switch`, `move` and `stamp` reach out —
each one importing what it needs inside the function, so the offline half stays
usable when gcloud is not installed at all.

Every command that takes a machine takes it through `_lookup`, so `windows`,
`l4` and `windows/l4` work wherever a name works, and the machine a description
resolved to is printed rather than assumed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from .config import (
    COMFYUI_DEFAULT_PORT,
    DEFAULT_CONFIG_PATH,
    ConfigError,
    Host,
    describe,
    load,
    resolve,
)
from .stamp import ProbeError, fetch, mismatch

app = typer.Typer(
    help="Operate the machines you test on — local installs and cloud GPU boxes.",
    no_args_is_help=False,
)

STARTER = f"""\
# comfy-qat host list.
#
# Every machine you test on is declared here, local or cloud. Naming them all
# means there is no invisible default, which is how you end up reading results
# from the wrong machine.
#
# Rules the tool enforces, all of them when this file is read:
#   - every host needs its own port
#   - a cloud host may never use {COMFYUI_DEFAULT_PORT}; that is the local ComfyUI's
#   - no two hosts may be the same cloud box, or differ only in case
#   - 'local' is this machine, so a cloud box may not take the name
#   - a local host may not carry gce_instance / gce_zone / gce_project, or
#     `host down` would leave a real instance running and billing

[hosts.local]
kind = "local"
port = {COMFYUI_DEFAULT_PORT}

# [hosts.comfy-linux]
# kind         = "gce"
# os           = "Ubuntu 22.04"
# gpu          = "L4"
# gce_instance = "comfy-linux"
# gce_zone     = "us-central1-a"
# gce_project  = "your-project-id"
# port         = 8190
"""


def _config_option() -> Path:
    return DEFAULT_CONFIG_PATH


def _states(hosts: list[Host], *, live: bool) -> dict[str, str]:
    """What each machine is doing right now.

    The tunnel is what makes a cloud box answer on 127.0.0.1, so "tunnelled" is
    the honest answer to "which box am I on?" — and reading a pid file costs
    nothing, so it is always shown. Whether the instance is *running* is a gcloud
    call per box, which is not free, so it waits to be asked for with --live.
    """
    from .tunnel import status as tunnel_status

    gc = None
    states: dict[str, str] = {}
    for host in hosts:
        parts = []
        if host.is_remote:
            if live:
                from .gcloud import Gcloud, GcloudError

                gc = gc or Gcloud()
                try:
                    state = gc.instance_status(
                        host.gce_instance, host.gce_zone, host.gce_project)
                except GcloudError:
                    state = "unknown"
                # TERMINATED is Google's word for stopped, and reads as broken.
                parts.append({"RUNNING": "running", "TERMINATED": "stopped"}.get(
                    state, state.lower()))
            if tunnel_status(host.name).running:
                parts.append("tunnelled")
        states[host.name] = ", ".join(parts) or "-"
    return states


@app.command("list")
def list_cmd(
    config: Annotated[Optional[Path], typer.Option(
        "--config", help="Host list to read. Default: ~/.config/comfy-qa-tools/hosts.toml.")] = None,
    live: Annotated[bool, typer.Option(
        "--live", help="Ask Google whether each cloud box is running. One call per box.")] = False,
) -> None:
    """Show every declared machine: what it is, where it answers, and what is up."""
    try:
        hosts = load(config)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)

    state = _states(hosts, live=live)
    rows = [("NAME", "KIND", "OS", "GPU", "URL", "STATE")] + [
        (h.name, h.kind, h.os or "-", h.gpu or "-", h.url, state[h.name]) for h in hosts
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        typer.echo("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())


@app.command("init")
def init_cmd(
    config: Annotated[Optional[Path], typer.Option(
        "--config", help="Where to write the starter host list.")] = None,
    force: Annotated[bool, typer.Option(
        "--force", help="Overwrite an existing host list.")] = False,
) -> None:
    """Write a starter host list you can edit."""
    path = config or DEFAULT_CONFIG_PATH
    if path.exists() and not force:
        typer.echo(f"{path} already exists. Use --force to overwrite it.", err=True)
        raise typer.Exit(code=2)
    # A folder you cannot write to, and `--force` aimed at a directory, both
    # arrive here as an OSError. This is the command someone runs first, so a
    # traceback is the first thing the tool would ever show them.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STARTER, encoding="utf-8")
    except OSError as exc:
        typer.echo(
            f"could not write a host list to {path}: {exc}. Give `--config` a path you "
            "can write to — the file itself, not the folder it goes in.", err=True)
        raise typer.Exit(code=2)
    typer.echo(f"wrote {path}")
    typer.echo("Edit it to add your cloud boxes, then run `comfy-qat host list`.")


@app.command("discover")
def discover_cmd(
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    dry_run: Annotated[bool, typer.Option(
        "--dry-run", help="Show what would be added without writing anything.")] = False,
) -> None:
    """Find cloud boxes on your project and add the ones you do not have yet.

    Google already knows each box's zone, card and operating system, so nothing
    here needs typing by hand. Existing entries are never touched.
    """
    from .gcloud import Gcloud, GcloudError
    from .discover import new_hosts, parse as parse_instance, to_toml

    path = config or DEFAULT_CONFIG_PATH
    gc = Gcloud()
    try:
        project = gc.current_project()
        if not project:
            typer.echo("no project set. Run: comfy-qat setup", err=True)
            raise typer.Exit(code=2)
        instances = gc.list_instances(project)
    except GcloudError as exc:
        _refused(exc)

    found = [parse_instance(instance, project) for instance in instances]
    if not found:
        typer.echo(f"no cloud boxes on {project}")
        return

    try:
        existing = load(path)
    except ConfigError:
        existing = []

    additions = new_hosts(found, existing)
    if not additions:
        typer.echo(f"{len(found)} cloud box(es), all already in {path}")
        return

    for box, port in additions:
        state = "running" if box.running else "stopped"
        typer.echo(f"{box.name}  {box.os}  {box.gpu or 'no GPU'}  {state}  port {port}")

    if dry_run:
        typer.echo("\n--dry-run: nothing written")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(STARTER, encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        for box, port in additions:
            handle.write(to_toml(box, port))
    typer.echo(f"\nadded {len(additions)} to {path}")


def _lookup(name: str, config: Optional[Path]) -> tuple[list[Host], Host]:
    """The whole host list, and the one machine the argument meant.

    The resolution line goes to stderr so that `--json` and `--dry-run` keep
    printing only the thing you were going to paste, while you still see which
    machine `windows` turned out to be.
    """
    try:
        hosts = load(config)
        chosen = resolve(hosts, name)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)
    if chosen.line() is not None:
        typer.echo(chosen.line(), err=True)
    return hosts, chosen.host


def _host(name: str, config: Optional[Path]) -> Host:
    return _lookup(name, config)[1]


def _reportable() -> tuple[type, ...]:
    """The failures this tool answers with a message rather than a traceback.

    `TunnelError` is not a `LifecycleError` and cannot become one — `lifecycle`
    imports `tunnel`, so the dependency only runs one way — but it was given the
    same shape on purpose: a message, a `fix`, and a `kind`. Every handler here
    reads exactly those three, so naming both types is the whole of the work.
    Anything reaching a person through `bring_up` or `serve` can raise either.
    """
    from .lifecycle import LifecycleError
    from .tunnel import TunnelError

    return (LifecycleError, TunnelError)


def _refused(exc, code: int = 2) -> None:
    """A gcloud refusal, reported the way every other command in this group does.

    One shape for the whole `host` group, because a tester reads exit codes across
    commands: **2 means nothing was changed** — a refusal, a precondition, a bad
    argument — and **1 means the work started and failed.** `move` used to exit 1
    with no `to fix:` line where `auth quota list` and `host discover` exited 2
    with one, on the same gcloud error.
    """
    typer.echo(str(exc), err=True)
    if getattr(exc, "fix", None):
        typer.echo(f"to fix: {exc.fix}", err=True)
    raise typer.Exit(code=code)


def _act(action, *args, **kwargs):
    """Run a lifecycle step, turning its failures into messages, never tracebacks."""
    try:
        return action(*args, **kwargs)
    except _reportable() as exc:
        typer.echo(f"\n{exc}", err=True)
        if exc.fix:
            typer.echo(f"to fix: {exc.fix}", err=True)
        raise typer.Exit(code=1)


@app.command("up")
def up_cmd(
    name: Annotated[str, typer.Argument(help="Which machine: a name, or what you want — windows, l4, windows/l4.")],
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
) -> None:
    """Start a machine and wait until ComfyUI actually answers.

    "Up" means ComfyUI is serving, not that the VM booted. A machine that has
    booted and serves nothing looks like success and bills like success.
    """
    from .gcloud import Gcloud
    from .lifecycle import bring_up

    hosts, host = _lookup(name, config)
    try:
        bring_up(Gcloud(), host, lambda line: typer.echo(f"  {line}"))
    except _reportable() as exc:
        # A box that will not start ends the session unless you are told where
        # else you could work, and a GPU shortage is the usual reason.
        _failed(host, hosts, exc)
        raise typer.Exit(code=1)
    typer.echo(f"\nOpen {host.url} in your browser.")


@app.command("open")
def open_cmd(
    name: Annotated[str, typer.Argument(help="Which machine: a name, or what you want — windows, l4, windows/l4.")],
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    dry_run: Annotated[bool, typer.Option(
        "--dry-run", help="Print the tunnel command instead of running it.")] = False,
) -> None:
    """Open a tunnel to a machine that is already running.

    Traffic goes over Identity-Aware Proxy, so no port is ever opened and no SSH
    key is needed — which matters, because ComfyUI has no authentication.
    """
    from .tunnel import (
        TunnelError,
        command as tunnel_command,
        open_tunnel,
        status as tunnel_status,
    )

    host = _host(name, config)
    if not host.is_remote:
        typer.echo(f"{host.name} is local — nothing to tunnel. It is at {host.url}.")
        return

    if dry_run:
        typer.echo(" ".join(tunnel_command(host)))
        return

    # There used to be a `if tunnel_status(host.name).running: return` here, and
    # it decided from the name alone. A name is not a machine: a second host list
    # can call a different box `comfy-win` too, and that early return printed
    # *this* host list's URL for *that* host list's tunnel. `open_tunnel` is the
    # only thing that knows — it compares the recorded instance, zone, project and
    # port against the host — so the question is asked there and nowhere else.
    # Reuse is still reported as reuse: `open_tunnel` hands back the live state
    # untouched when it really is the same machine.
    before = tunnel_status(host.name)
    try:
        state = open_tunnel(host)
    except TunnelError as exc:
        typer.echo(f"\n{exc}", err=True)
        if exc.fix:
            typer.echo(f"to fix: {exc.fix}", err=True)
        raise typer.Exit(code=2)

    reused = before.running and before.pid == state.pid
    opened = "tunnel already open" if reused else "tunnel open"
    typer.echo(f"{opened} (pid {state.pid}): {state.url or host.url}")


@app.command("down")
def down_cmd(
    name: Annotated[str, typer.Argument(help="Which machine: a name, or what you want — windows, l4, windows/l4.")],
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    keep_running: Annotated[bool, typer.Option(
        "--keep-running", help="Close the tunnel but leave the machine on.")] = False,
) -> None:
    """Close the tunnel and stop the machine, so it stops costing money."""
    from .gcloud import Gcloud
    from .lifecycle import put_away

    host = _host(name, config)
    _act(put_away, Gcloud(), host, lambda line: typer.echo(f"  {line}"),
         keep_running=keep_running)


@app.command("go")
def go_cmd(
    name: Annotated[str, typer.Argument(help="Which machine: a name, or what you want — windows, l4, windows/l4.")],
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    no_browser: Annotated[bool, typer.Option(
        "--no-browser", help="Do not open a browser when ComfyUI answers.")] = False,
    no_install: Annotated[bool, typer.Option(
        "--no-install", help="Fail rather than installing ComfyUI if it is absent.")] = False,
) -> None:
    """Start the machine, make sure ComfyUI is on it, and run it where you can watch.

    The everyday command. If ComfyUI is already serving you get the URL straight
    away; otherwise it is installed if needed and launched in the foreground, with
    its startup log on this terminal exactly as a local `main.py` would print it.
    """
    from .gcloud import Gcloud

    hosts, host = _lookup(name, config)
    gc = Gcloud()
    ready = _bring_up(gc, host, hosts)
    _serve(gc, host, ready, no_browser=no_browser, no_install=no_install)


def _unavailable(host: Host, hosts: list[Host], exc, kept: list[Host]) -> None:
    """A box that will not start is not the end of a test session. Say what is.

    Being told "no capacity in this zone" and nothing else is where testing
    stops: the next move is a four-step rebuild nobody has memorised. The
    tester's real question is "where can I work right now", so that is answered
    first, and the rebuild is offered second, for when it has to be that box.
    """
    from .lifecycle import alternatives

    typer.echo(f"\n{exc}", err=True)

    if kept:
        still = ", ".join(other.name for other in kept)
        typer.echo(f"\n{still} is untouched — you still have the machine you were on."
                   if len(kept) == 1 else
                   f"\n{still} are untouched — you still have the machines you were on.",
                   err=True)

    options = alternatives(hosts, host)[:3]
    if options:
        commands = [f"comfy-qat host switch {other.name}" for other in options]
        width = max(len(command) for command in commands)
        typer.echo("\nWhere you can test instead, easiest first:", err=True)
        for other, command in zip(options, commands):
            note = ""
            if other.gce_zone and other.gce_zone == host.gce_zone:
                note = ", same zone — it may hit the same shortage"
            typer.echo(f"  {command.ljust(width)}   # {describe(other)}{note}", err=True)
    else:
        typer.echo("\nNo other machine is declared, so there is nowhere to switch to:",
                   err=True)
        typer.echo("  comfy-qat host discover   # declare a box you already have",
                   err=True)

    if exc.fix:
        typer.echo(f"\nIf it has to be {host.name}:", err=True)
        for line in exc.fix.splitlines():
            typer.echo(f"  {line.strip()}", err=True)


def _failed(host: Host, hosts: list[Host], exc, kept: list[Host] | None = None) -> None:
    """Report a machine that would not come up, in the most useful way there is."""
    from .lifecycle import STOCKOUT

    if getattr(exc, "kind", "") == STOCKOUT:
        _unavailable(host, hosts, exc, kept or [])
        return
    typer.echo(f"\n{exc}", err=True)
    if exc.fix:
        typer.echo(f"to fix: {exc.fix}", err=True)


def _bring_up(gc, host: Host, hosts: list[Host], kept: list[Host] | None = None):
    """Get the machine up, with every failure turned into a next command.

    Returns None when the box is up but ComfyUI is absent — the one failure the
    steps after this one exist to fix.
    """
    from .lifecycle import COMFYUI_ABSENT, bring_up

    try:
        return bring_up(gc, host, lambda line: typer.echo(f"  {line}"), comfy_timeout=15)
    except _reportable() as exc:
        # Only "ComfyUI is not there yet" is worth continuing past. Anything else
        # (the box would not start, the tunnel failed) must be shown, not
        # swallowed — that once hid a failed start and then tried SSH against it.
        if exc.kind == COMFYUI_ABSENT:
            return None
        _failed(host, hosts, exc, kept)
        raise typer.Exit(code=1)


def _serve(gc, host: Host, ready, *, no_browser: bool = False,
           no_install: bool = False) -> None:
    """The rest of `go` once the machine is up: install if needed, then run it."""
    import webbrowser

    from .gcloud import GcloudError
    from .lifecycle import ensure_installed, serve, wait_for_ssh

    say = lambda line: typer.echo(f"  {line}")
    browser = None if no_browser else (lambda url: webbrowser.open(url))

    if ready is not None and ready.stamp is not None:
        typer.echo(f"\n{host.url}")
        typer.echo(ready.stamp.line())
        if browser:
            browser(host.url)
        typer.echo(f"\nWhen you are done:  comfy-qat host down {host.name}")
        return

    if not host.is_remote:
        typer.echo("ComfyUI is not running locally. Start it with:", err=True)
        typer.echo("  ~/ComfyUI/venv/bin/python ~/ComfyUI/main.py --port 8188 "
                   "--listen 127.0.0.1", err=True)
        raise typer.Exit(code=1)

    if no_install:
        typer.echo("ComfyUI is not answering and --no-install was given.", err=True)
        raise typer.Exit(code=1)

    try:
        wait_for_ssh(gc, host, say)
        ensure_installed(gc, host, say)
        typer.echo("")
        code = serve(gc, host, say, open_browser=browser)
    except _reportable() as exc:
        typer.echo(f"\n{exc}", err=True)
        if exc.fix:
            typer.echo(f"to fix: {exc.fix}", err=True)
        raise typer.Exit(code=1)
    except GcloudError as exc:
        typer.echo(f"\n{exc}", err=True)
        raise typer.Exit(code=1)
    except KeyboardInterrupt:
        typer.echo(f"\nstopped. The machine is still running — "
                   f"`comfy-qat host down {host.name}` to stop paying.")
        return

    typer.echo(f"\nComfyUI exited ({code}). "
               f"`comfy-qat host down {host.name}` to stop the machine.")


@app.command("switch")
def switch_cmd(
    name: Annotated[str, typer.Argument(
        help="Which machine: a name, or what you want — windows, l4, windows/l4.")],
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    keep_others: Annotated[bool, typer.Option(
        "--keep-others", help="Leave the other machines running. They keep billing.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the plan and stop.")] = False,
    no_browser: Annotated[bool, typer.Option(
        "--no-browser", help="Do not open a browser when ComfyUI answers.")] = False,
    no_install: Annotated[bool, typer.Option(
        "--no-install", help="Fail rather than installing ComfyUI if it is absent.")] = False,
) -> None:
    """Change machine: start the one you want, stop the one you were on.

    `go` with the step people forget on the front — a GPU box left running bills
    all night whether or not anything is tunnelled to it. Nothing here is new and
    nothing is remembered: there is still no current host, and `switch` names
    what it starts and what it stops before it does either.

    The target is brought up *first*. If it cannot start — a capacity shortage is
    routine on GPUs — you still have the machine you were on, and you are told
    where you can work instead.
    """
    from .gcloud import Gcloud, GcloudError
    from .lifecycle import put_away, running_elsewhere

    hosts, host = _lookup(name, config)
    gc = Gcloud()

    try:
        others = [] if keep_others else running_elsewhere(gc, hosts, host)
    except GcloudError as exc:
        # Nothing has been started or stopped yet: this is only the survey of what
        # is running elsewhere, so it is a refusal (2), not a failed switch (1).
        _refused(exc)

    typer.echo("")
    typer.echo(f"  - go to {host.name} ({describe(host)}) on {host.url}")
    for other, why in others:
        typer.echo(f"  - then stop {other.name} ({describe(other)}) — {why}")
    if not others:
        typer.echo("  - leaving the other machines running (--keep-others)" if keep_others
                   else "  - nothing else is running, so nothing to stop")

    if dry_run:
        typer.echo("\n--dry-run: nothing changed")
        return

    typer.echo("")
    ready = _bring_up(gc, host, hosts, kept=[other for other, _why in others])

    for other, _why in others:
        _act(put_away, gc, other, lambda line: typer.echo(f"  {line}"))

    _serve(gc, host, ready, no_browser=no_browser, no_install=no_install)


def _zone_with_capacity(gc, host: Host, *, dry_run: bool) -> str | None:
    """Which zone Google says has capacity — found the only way there is.

    Nothing answers "where is there an L4 free". The only way to find out is to
    try to start the machine and read the zone out of the refusal, which means
    that when it is *not* refused the box is up and billing.

    That is why this cannot be part of a dry run, and why the guard lives here
    rather than at the call site: `--dry-run` used to be consulted long after
    this had already started a GPU instance, so the one command that promises to
    change nothing was the one that could quietly cost the most. Anything that
    replaces this function inherits the guard with it.

    Returns the zone to move to, or None when the machine started — in which case
    there was never anything to move, and it has been reported.
    """
    from .gcloud import GcloudError
    from .lifecycle import is_capacity_failure, suggested_zones

    if dry_run:
        typer.echo(
            f"--dry-run cannot work out which zone has capacity. The only way to ask "
            f"is to try to start {host.gce_instance}, and if it starts it is billing — "
            f"so a dry run that did it would be the most expensive command here. Say "
            f"where you want it and the rest of the plan is printed without touching "
            f"anything: comfy-qat host move {host.name} --to us-central1-b --dry-run.",
            err=True)
        raise typer.Exit(code=2)

    typer.echo("asking Google where there is capacity…")
    try:
        gc.start_instance(host.gce_instance, host.gce_zone, host.gce_project)
    except GcloudError as exc:
        if not is_capacity_failure(exc.raw):
            _refused(exc)
        zones = suggested_zones(exc.raw)
        if not zones:
            typer.echo("Google did not name a zone with capacity. Pick one with "
                       "--to, e.g. --to us-central1-b", err=True)
            raise typer.Exit(code=2)
        typer.echo(f"  {host.gce_zone} has none free; {zones[0]} does")
        return zones[0]

    typer.echo(f"{host.name} started in {host.gce_zone} — no move needed.")
    typer.echo(f"  comfy-qat host go {host.name}")
    return None


@app.command("move")
def move_cmd(
    name: Annotated[str, typer.Argument(help="Which machine to move: a name, or what you want — windows, l4.")],
    to: Annotated[Optional[str], typer.Option(
        "--to", help="Zone to move it to. Default: whichever one Google says has capacity.")] = None,
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask before making changes.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the plan and stop.")] = False,
) -> None:
    """Move a box to a zone that has capacity, keeping its ComfyUI install.

    A GPU stockout cannot be fixed where you are: the zone has none of that card
    and retrying will not change it. Done by hand this is a snapshot, a disk, an
    instance and a config edit — four chances to get it wrong.
    """
    from .discover import Discovered, next_ports, to_toml
    from .gcloud import Gcloud, GcloudError

    host = _host(name, config)
    if not host.is_remote:
        typer.echo(f"{host.name} is local — there is nowhere to move it to.", err=True)
        raise typer.Exit(code=2)

    gc = Gcloud()
    target = to

    if target is None:
        # Everything a dry run must not do lives inside this call, guard included.
        target = _zone_with_capacity(gc, host, dry_run=dry_run)
        if target is None:
            return

    try:
        instance = gc.describe_instance(host.gce_instance, host.gce_zone, host.gce_project)
    except GcloudError as exc:
        _refused(exc)

    from .relocate import metadata_pairs, plan_move

    plan = plan_move(host, instance, target)
    typer.echo("")
    for step in plan.steps():
        typer.echo(f"  - {step}")

    if dry_run:
        typer.echo("\n--dry-run: nothing changed")
        return
    if not yes and not typer.confirm(f"\nMove {host.name} to {target}?"):
        typer.echo("nothing changed")
        return

    disk = plan.new_disk.rsplit("-", 1)[0]
    try:
        typer.echo("  snapshotting the boot disk — this is the slow part")
        gc.snapshot_disk(disk, host.gce_zone, host.gce_project, plan.snapshot)
        typer.echo(f"  creating {plan.new_disk} in {target}")
        gc.create_disk_from_snapshot(plan.new_disk, target, host.gce_project, plan.snapshot)
        typer.echo(f"  creating {plan.new_instance}")
        gc.create_instance_from_disk(
            plan.new_instance, target, host.gce_project, plan.new_disk,
            plan.machine_type, metadata_pairs(instance),
        )
    except GcloudError as exc:
        typer.echo(f"\nthe move failed: {exc}", err=True)
        if exc.fix:
            typer.echo(f"to fix: {exc.fix}", err=True)
        typer.echo(f"nothing was removed — {host.gce_instance} is untouched in "
                   f"{host.gce_zone}.", err=True)
        raise typer.Exit(code=1)

    path = config or DEFAULT_CONFIG_PATH
    hosts = load(path)
    port = next_ports(hosts, 1)[0]
    moved = Discovered(
        name=plan.new_instance, os=host.os or "unknown", gpu=host.gpu or "",
        gce_instance=plan.new_instance, gce_zone=target,
        gce_project=host.gce_project, running=True,
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(to_toml(moved, port))

    typer.echo(f"\n{plan.new_instance} is in {target}, on port {port}.")
    typer.echo(f"  comfy-qat host go {plan.new_instance}")
    typer.echo(f"\n{host.gce_instance} is still in {host.gce_zone}, stopped. Delete it "
               f"when you are happy with the new one.")


def _probe_fix(host: Host) -> str | None:
    """Advice for a machine that did not answer, told which machine it was.

    `fetch` is handed a URL and nothing else, so its advice — "start ComfyUI on
    that machine, or check the port in your host list" — is the local answer, and
    for a cloud box it names neither of the two things that are actually wrong.
    A `gce` host is reached through a tunnel, so nothing on 8190 usually means
    there is no tunnel, or the instance is stopped, and the port in the host list
    is fine.

    Only claimed when there is really no tunnel. With one open the port is being
    forwarded and the answer came from the far end, so `fetch` knows more about
    what went wrong than this does. Returns None to leave its advice alone.
    """
    if not host.is_remote:
        return None

    from .tunnel import status as tunnel_status

    if tunnel_status(host.name).running:
        return None

    return (
        f"no tunnel to {host.name} is open, so nothing on this machine answers "
        f"{host.url} — and {host.gce_instance} may simply be stopped. "
        f"`comfy-qat host open {host.name}` tunnels to a box that is already "
        f"running; `comfy-qat host go {host.name}` starts it and tunnels in one "
        f"step. `comfy-qat host list --live` says which it is."
    )


@app.command("stamp")
def stamp_cmd(
    name: Annotated[str, typer.Argument(help="Which machine: a name, or what you want — windows, l4, windows/l4.")],
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    as_json: Annotated[bool, typer.Option(
        "--json", help="Machine-readable, for pasting into a report or a test.")] = False,
) -> None:
    """Ask a machine what it is, and print the line you paste into a report.

    This is the record nothing else keeps. A recorded test does not carry it, and
    a hand-written bug report usually does not either — which is how "cannot
    reproduce" happens between two machines that were never the same.
    """
    host = _host(name, config)

    try:
        stamp = fetch(host.url, host=host.name)
    except ProbeError as exc:
        typer.echo(str(exc), err=True)
        fix = _probe_fix(host) or exc.fix
        if fix:
            typer.echo(f"to fix: {fix}", err=True)
        raise typer.Exit(code=1)

    # Refused, not warned. This line exists to be copied — it is pasted into a
    # bug report as the proof of which machine produced a result — and a warning
    # on stderr does not survive being copied. Printing the line at all is what
    # creates the artefact, so when the machine that answered contradicts the one
    # declared, no line is printed and the contradiction is what you get instead.
    problem = mismatch(host, stamp)
    if problem is not None:
        typer.echo(problem, err=True)
        typer.echo(
            "No evidence line was printed, because this one would have named the "
            "wrong machine. Check the port in your host list and which tunnel is "
            "open, then stamp it again.", err=True)
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(stamp.as_dict(), indent=2))
    else:
        typer.echo(stamp.line())


@app.callback(invoke_without_command=True)
def default(
    ctx: typer.Context,
    config: Annotated[Optional[Path], typer.Option(
        "--config", help="Host list to read. Default: ~/.config/comfy-qa-tools/hosts.toml.")] = None,
) -> None:
    """With no subcommand, listing is the safe thing to do.

    `--config` is accepted here as well as on the subcommands: every other
    command takes it, so `host --config x` failing as a usage error is a
    surprise, and a surprise on the read-only default is a bad one.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(list_cmd, config=config)
