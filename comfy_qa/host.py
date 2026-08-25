"""`comfy-qat host` — operate the machines you test on.

Only the offline commands live here so far: listing what is declared, and writing
a starter host list. Everything that talks to gcloud lands in the next pass.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from .config import COMFYUI_DEFAULT_PORT, DEFAULT_CONFIG_PATH, ConfigError, find, load
from .stamp import ProbeError, fetch

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
# Rules the tool enforces:
#   - every host needs its own port
#   - a cloud host may never use {COMFYUI_DEFAULT_PORT}; that is the local ComfyUI's

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


@app.command("list")
def list_cmd(
    config: Annotated[Optional[Path], typer.Option(
        "--config", help="Host list to read. Default: ~/.config/comfy-qa-tools/hosts.toml.")] = None,
) -> None:
    """Show every declared machine: what it is, and where it answers."""
    try:
        hosts = load(config)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)

    rows = [("NAME", "KIND", "OS", "GPU", "URL")] + [
        (h.name, h.kind, h.os or "-", h.gpu or "-", h.url) for h in hosts
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STARTER, encoding="utf-8")
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
        typer.echo(str(exc), err=True)
        if exc.fix:
            typer.echo(f"to fix: {exc.fix}", err=True)
        raise typer.Exit(code=2)

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


def _host(name: str, config: Optional[Path]):
    try:
        return find(load(config), name)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)


def _act(action, *args, **kwargs):
    """Run a lifecycle step, turning its failures into messages, never tracebacks."""
    from .lifecycle import LifecycleError

    try:
        return action(*args, **kwargs)
    except LifecycleError as exc:
        typer.echo(f"\n{exc}", err=True)
        if exc.fix:
            typer.echo(f"to fix: {exc.fix}", err=True)
        raise typer.Exit(code=1)


@app.command("up")
def up_cmd(
    name: Annotated[str, typer.Argument(help="Which machine. See `host list`.")],
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
) -> None:
    """Start a machine and wait until ComfyUI actually answers.

    "Up" means ComfyUI is serving, not that the VM booted. A machine that has
    booted and serves nothing looks like success and bills like success.
    """
    from .gcloud import Gcloud
    from .lifecycle import bring_up

    host = _host(name, config)
    _act(bring_up, Gcloud(), host, lambda line: typer.echo(f"  {line}"))
    typer.echo(f"\nOpen {host.url} in your browser.")


@app.command("open")
def open_cmd(
    name: Annotated[str, typer.Argument(help="Which machine.")],
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    dry_run: Annotated[bool, typer.Option(
        "--dry-run", help="Print the tunnel command instead of running it.")] = False,
) -> None:
    """Open a tunnel to a machine that is already running.

    Traffic goes over Identity-Aware Proxy, so no port is ever opened and no SSH
    key is needed — which matters, because ComfyUI has no authentication.
    """
    from .tunnel import command as tunnel_command, open_tunnel, status as tunnel_status

    host = _host(name, config)
    if not host.is_remote:
        typer.echo(f"{host.name} is local — nothing to tunnel. It is at {host.url}.")
        return

    if dry_run:
        typer.echo(" ".join(tunnel_command(host)))
        return

    existing = tunnel_status(host.name)
    if existing.running:
        typer.echo(f"tunnel already open (pid {existing.pid}): {host.url}")
        return

    state = open_tunnel(host)
    typer.echo(f"tunnel open (pid {state.pid}): {host.url}")


@app.command("down")
def down_cmd(
    name: Annotated[str, typer.Argument(help="Which machine.")],
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
    name: Annotated[str, typer.Argument(help="Which machine.")],
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
    import webbrowser

    from .gcloud import Gcloud, GcloudError
    from .lifecycle import LifecycleError, bring_up, ensure_installed, serve

    host = _host(name, config)
    gc = Gcloud()
    say = lambda line: typer.echo(f"  {line}")
    browser = None if no_browser else (lambda url: webbrowser.open(url))

    # Already serving? Then there is nothing to install or launch.
    try:
        ready = bring_up(gc, host, say, comfy_timeout=15)
    except LifecycleError:
        ready = None

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
        ensure_installed(gc, host, say)
        typer.echo("")
        code = serve(gc, host, say, open_browser=browser)
    except LifecycleError as exc:
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


@app.command("stamp")
def stamp_cmd(
    name: Annotated[str, typer.Argument(help="Which machine. See `host list`.")],
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    as_json: Annotated[bool, typer.Option(
        "--json", help="Machine-readable, for pasting into a report or a test.")] = False,
) -> None:
    """Ask a machine what it is, and print the line you paste into a report.

    This is the record nothing else keeps. A recorded test does not carry it, and
    a hand-written bug report usually does not either — which is how "cannot
    reproduce" happens between two machines that were never the same.
    """
    try:
        host = find(load(config), name)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)

    try:
        stamp = fetch(host.url, host=host.name)
    except ProbeError as exc:
        typer.echo(str(exc), err=True)
        if exc.fix:
            typer.echo(f"to fix: {exc.fix}", err=True)
        raise typer.Exit(code=1)

    if as_json:
        typer.echo(json.dumps(stamp.as_dict(), indent=2))
    else:
        typer.echo(stamp.line())


@app.callback(invoke_without_command=True)
def default(ctx: typer.Context) -> None:
    """With no subcommand, listing is the safe thing to do."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(list_cmd, config=None)
