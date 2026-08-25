"""Root command surface.

`comfy-qat <feature> <action>`. Each feature is a Typer sub-app registered here
with one line, which is how a later feature is added without touching an
existing one.
"""

from __future__ import annotations

import typer

from . import auth, commands, host

app = typer.Typer(
    help="QA tooling for testing Comfy: know which machine you are testing, "
         "and stamp every result with it.",
    no_args_is_help=True,
)

app.add_typer(host.app, name="host")
app.add_typer(auth.app, name="auth")

# v0's environment check, carried forward so it stays reachable under the new
# binary. It is not part of release 1 and gets rewritten when its own release
# comes round; until then, losing it would be a regression nobody asked for.
app.command("env")(commands.env_cmd)


FIRST_RUN = """\
First run — four steps.

  1. gcloud auth login              sign in to Google Cloud
  2. comfy-qat auth status          check you are ready; fix what it names, repeat
  3. comfy-qat host init            write a starter host list
  4. comfy-qat host list            see your machines

Step 2 stops at the first problem and prints the command that fixes it. Run it
again after each fix until every line says ok.

Full docs: https://github.com/Comfy-Org/comfy-qa-tools/tree/main/docs
  getting-started.md   this, with the reasoning
  hosts.md             every field in the host list
  troubleshooting.md   every error and its fix
  cost.md              what a running box costs, and the one rule
"""


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
