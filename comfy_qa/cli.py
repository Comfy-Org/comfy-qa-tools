"""Root command surface.

`comfy-qat <feature> <action>`. Each feature is a Typer sub-app registered here
with one line, which is how a later feature is added without touching an
existing one.
"""

from __future__ import annotations

from typing import Annotated, Optional

import typer

from .gcloud import Gcloud, GcloudError

from . import auth, commands, host
from . import setup as setup_mod

app = typer.Typer(
    help="QA tooling for testing Comfy: know which machine you are testing, "
         "and stamp every result with it.",
    no_args_is_help=True,
)

app.add_typer(host.app, name="host")
app.add_typer(auth.app, name="auth")


def _version_callback(asked: bool) -> None:
    """Eager, so `--version` answers before any argument is validated."""
    if asked:
        from . import version_string

        typer.echo(version_string())
        raise typer.Exit()


@app.callback()
def root(
    version: Annotated[bool, typer.Option(
        "--version", callback=_version_callback, is_eager=True,
        help="Print the build — version, plus the commit in a checkout — and exit.")] = False,
) -> None:
    """QA tooling for testing Comfy: know which machine you are testing,
    and stamp every result with it."""

# v0's environment check, carried forward so it stays reachable under the new
# binary. It is not part of release 1 and gets rewritten when its own release
# comes round; until then, losing it would be a regression nobody asked for.
# Hidden, not removed. `env` belongs to a different tool — it reports the build
# and flag state of deployed environments, which has nothing to do with the
# machines this one operates — but it works, it was verified against all three
# cloud environments, and deleting it would take away the only way anyone has to
# check which build an environment is serving. So it stays reachable and stops
# advertising itself.
app.command("env", hidden=True)(commands.env_cmd)


FIRST_RUN = """\
First run — one command.

  comfy-qat setup

It signs you in to Google Cloud, picks your project, checks billing, sorts out GPU
quota and writes your host list. It asks only where the decision is genuinely
yours, and says what it is doing at each step.

Afterwards:

  comfy-qat host list      see your machines
  comfy-qat auth status    re-check readiness at any time

Full docs: https://github.com/Comfy-Org/comfy-qa-tools/tree/main/docs
  getting-started.md   this, with the reasoning
  hosts.md             every field in the host list
  troubleshooting.md   every error and its fix
  cost.md              what a running box costs, and the one rule
"""


@app.command("setup")
def setup_cmd(
    project: Annotated[Optional[str], typer.Option(
        "--project", help="Use this Google Cloud project instead of asking.")] = None,
    region: Annotated[Optional[str], typer.Option(
        "--region", help="Region for a GPU quota request, e.g. us-central1.")] = None,
    non_interactive: Annotated[bool, typer.Option(
        "--non-interactive", help="Never prompt. Fails with the command to run "
                                  "instead of opening a browser.")] = False,
) -> None:
    """Get this machine ready, in one command."""
    prompts = setup_mod.Prompts(
        confirm=typer.confirm,
        ask=typer.prompt,
        choose=_choose,
        say=lambda line: typer.echo(f"  {line}"),
    )
    try:
        path = setup_mod.run_setup(
            Gcloud(), prompts,
            interactive=not non_interactive,
            project=project, region=region,
        )
    except setup_mod.SetupStopped as stop:
        typer.echo(f"\nsetup stopped: {stop}", err=True)
        if stop.fix:
            typer.echo(f"to fix: {stop.fix}", err=True)
        raise typer.Exit(code=1)
    except GcloudError as exc:
        # A gcloud failure is a message, never a traceback. Tracebacks tell a
        # tester nothing they can act on.
        typer.echo(f"\nsetup stopped: {exc}", err=True)
        if exc.fix:
            typer.echo(f"to fix: {exc.fix}", err=True)
        raise typer.Exit(code=1)

    # The old sign-off told people to add cloud boxes by hand, which discovery
    # had just done for them.
    from .config import ConfigError, load

    try:
        hosts = load(path)
    except ConfigError:
        hosts = []

    remote = [host for host in hosts if host.is_remote]
    typer.echo(f"\nReady — {len(hosts)} machine(s), {len(remote)} in the cloud.")
    typer.echo("  comfy-qat host list        see them")
    typer.echo("  comfy-qat host stamp local what a machine is, exactly")
    if not remote:
        typer.echo(f"\nNo cloud boxes found. Add one by hand in {path}, or create one in")
        typer.echo("Google Cloud and run `comfy-qat host discover`.")


def _choose(question: str, options: list[str]) -> str:
    """Numbered pick. Typer has no list prompt, and a free-text guess is worse."""
    typer.echo(question)
    for index, option in enumerate(options, start=1):
        typer.echo(f"  {index}. {option}")
    while True:
        raw = typer.prompt("Number")
        try:
            picked = int(raw)
        except ValueError:
            picked = 0
        if 1 <= picked <= len(options):
            return options[picked - 1]
        typer.echo(f"Pick a number between 1 and {len(options)}.")


@app.command("guide")
def guide_cmd() -> None:
    """How to set this up, without leaving the terminal."""
    typer.echo(FIRST_RUN)


def register(parent: typer.Typer, name: str = "qa") -> None:
    """Attach this whole surface to another Typer app.

    This is the object a comfy-cli plugin entry point would hand over, if
    comfy-cli ever grows one. Nothing here depends on that happening.
    """
    parent.add_typer(app, name=name)


def main() -> None:
    app()
