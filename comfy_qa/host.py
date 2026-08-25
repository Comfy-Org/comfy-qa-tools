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
