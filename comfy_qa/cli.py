"""Root command surface.

`comfy-qat <feature> <action>`. Each feature is a Typer sub-app registered here
with one line, which is how a later feature is added without touching an
existing one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from .gcloud import Gcloud, GcloudError

from . import auth, commands, host, remove, say
from . import setup as setup_mod

app = typer.Typer(
    help="QA tooling for testing Comfy: know which machine you are testing, "
         "and stamp every result with it.",
    # Bare `comfy-qat` lists your machines rather than printing help. The
    # question someone has when they type the name of this tool and nothing
    # else is "what have I got, and what is running" — help answers a question
    # nobody asked, and listing is read-only, so the safe thing is also the
    # useful one. `--help` still prints help.
    no_args_is_help=False,
)

# The verbs, at the top level. `host` was a noun in front of every one of them
# and nothing else in this tool collides with `go`, `down`, `list` or `stamp`,
# so it was pure typing — and on a second operating system it is typing you do
# twice as often.
for _command in host.app.registered_commands:
    app.registered_commands.append(_command)
for _command in auth.app.registered_commands:
    app.registered_commands.append(_command)
# Its own module rather than another verb in host.py, because it is the only
# irreversible command here and its refusals are worth reading on their own.
for _command in remove.app.registered_commands:
    app.registered_commands.append(_command)
for _group in auth.app.registered_groups:
    app.add_typer(_group.typer_instance, name=_group.name)

# The old spellings still work and no longer advertise themselves. Anything
# written down before today — a script, a run sheet, muscle memory — keeps
# working; `--help` shows one way to do each thing rather than two.
#
# These are a deprecation window, not a second permanent spelling: 42 command
# paths is not a simplification of 23. Those two numbers are walked out of the
# live Click tree — 42 leaves behind 23 distinct implementations, 19 of the
# leaves being these hidden duplicates — rather than counted by hand, which is
# how the pair that stood here before (26 and 13) came to be wrong in both
# halves while reading as though somebody had checked.
app.add_typer(host.app, name="host", hidden=True)
app.add_typer(auth.app, name="auth", hidden=True)


def _version_callback(asked: bool) -> None:
    """Eager, so `--version` answers before any argument is validated."""
    if asked:
        from . import version_string

        say.result(version_string())
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def root(
    ctx: typer.Context,
    config: Annotated[Optional[Path], typer.Option(
        "--config", callback=host.remember_config,
        help="Host list to read, and to rewrite where a command changes it. "
             "Default: ~/.config/comfy-qa-tools/hosts.toml. Subcommands inherit "
             "this — no need to repeat it on each one.")] = None,
    version: Annotated[bool, typer.Option(
        "--version", callback=_version_callback, is_eager=True,
        help="Print the build — version, plus the commit in a checkout — and exit.")] = False,
) -> None:
    """QA tooling for testing Comfy: know which machine you are testing,
    and stamp every result with it."""
    if ctx.invoked_subcommand is None:
        # No host list yet is the one case where help is the better answer:
        # there is nothing to list and `setup` is what they need.
        from .config import ConfigError, load

        try:
            load(config)
        except ConfigError:
            typer.echo(ctx.get_help())
            typer.echo("\nNo machines yet. Start with:  comfy-qat setup")
            raise typer.Exit(code=0)
        ctx.invoke(host.list_cmd, config=config, live=False)

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

Sign-in, project, billing, GPU quota and your host list, in one pass. It asks only
where the decision is genuinely yours, and says what it is doing as it goes.

Afterwards:

  comfy-qat list      see your machines
  comfy-qat status    re-check readiness at any time

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
    no_numpy: Annotated[bool, typer.Option(
        "--no-numpy",
        help="Do not install NumPy into gcloud's Python.")] = False,
    region: Annotated[Optional[str], typer.Option(
        "--region", help="Region for a GPU quota request, e.g. us-central1.")] = None,
    non_interactive: Annotated[bool, typer.Option(
        "--non-interactive", help="Never prompt. Fails with the command to run "
                                  "instead of opening a browser.")] = False,
    config: host.ConfigOption = None,
) -> None:
    """Get this machine ready, in one command.

    This one writes: it makes sure you have a host list, so the file `--config`
    names — and ~/.config/comfy-qa-tools/hosts.toml when it is left off — is
    written if it is not there yet, and appended to by the discovery pass at the
    end. An existing list is read, never replaced.
    """
    prompts = setup_mod.Prompts(
        confirm=typer.confirm,
        ask=typer.prompt,
        choose=_choose,
        say=say.step,
    )
    try:
        path = setup_mod.run_setup(
            Gcloud(), prompts,
            interactive=not non_interactive,
            project=project, region=region, no_numpy=no_numpy,
            config_path=config,
        )
    except (setup_mod.SetupStopped, GcloudError) as exc:
        # One handler, because there was never a difference: both are a message
        # and a fix, and a gcloud failure is a message rather than a traceback,
        # which tells a tester nothing they can act on.
        say.fail(f"setup stopped: {exc}", exc.fix)

    # The old sign-off told people to add cloud boxes by hand, which discovery
    # had just done for them.
    from .config import ConfigError, load

    try:
        hosts = load(path)
    except ConfigError:
        hosts = []

    remote = [host for host in hosts if host.is_remote]
    say.result(f"\nready — {say.count(len(hosts), 'machine')}, "
               f"{len(remote)} in the cloud")
    say.result("  comfy-qat list        see them")
    say.result("  comfy-qat stamp local what a machine is, exactly")
    if not remote:
        # This used to send a newcomer to the Google Cloud console, or to editing
        # a TOML file by hand, at the end of the command whose whole job is
        # getting them ready — and it was written before `create` existed. The
        # first-run path pointing away from the tool is the worst place for that
        # to be out of date.
        say.result("\nno cloud boxes yet. Make one:")
        say.result("  comfy-qat create --os linux --gpu l4")
        say.result(f"\nor, if you already have one on the project, `comfy-qat "
                   f"discover` adopts it. {path} is the list either way.")


def _choose(question: str, options: list[str]) -> str:
    """Numbered pick. Typer has no list prompt, and a free-text guess is worse.

    Plain `typer.echo` rather than `say`: this is the body of a prompt, and it
    has to appear on the same stream as the question `typer.prompt` is about to
    ask. It is neither an answer nor a diagnostic.
    """
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
        typer.echo(f"pick a number between 1 and {len(options)}")


@app.command("guide")
def guide_cmd() -> None:
    """How to set this up, without leaving the terminal."""
    say.result(FIRST_RUN)


def register(parent: typer.Typer, name: str = "qa") -> None:
    """Attach this whole surface to another Typer app.

    This is the object a comfy-cli plugin entry point would hand over, if
    comfy-cli ever grows one. Nothing here depends on that happening.
    """
    parent.add_typer(app, name=name)


def main() -> None:
    """The entry point.

    Deliberately thin. The interrupt report used to live here, on the argument
    that one handler beats four — and the argument was right about the four and
    wrong about the place. `cli.register()` hands this whole surface to somebody
    else's Typer app, where `main` is not on the stack at all, so the one
    embedding that could not reach the handler was the one where an unreported
    interrupt did the most damage. Reporting now happens in `inflight.may_leave`,
    which every entry point goes through.

    The `KeyboardInterrupt` clause is the last resort rather than the mechanism:
    an interrupt landing outside any registration has nothing to report, and
    Typer already exits 130 for it (typer/core.py:203-204). This makes that true
    for the paths Typer is not on either.
    """
    from . import inflight

    try:
        app()
    except KeyboardInterrupt:
        inflight.report()
        raise SystemExit(inflight.INTERRUPTED) from None
