"""Root command surface.

`comfy-qat <feature> <action>`. Each feature is a Typer sub-app registered here
with one line, which is how a later feature is added without touching an
existing one.
"""

from __future__ import annotations

import typer

from . import commands, host

app = typer.Typer(
    help="QA tooling for testing Comfy: know which machine you are testing, "
         "and stamp every result with it.",
    no_args_is_help=True,
)

app.add_typer(host.app, name="host")

# v0's environment check, carried forward so it stays reachable under the new
# binary. It is not part of release 1 and gets rewritten when its own release
# comes round; until then, losing it would be a regression nobody asked for.
app.command("env")(commands.env_cmd)


def register(parent: typer.Typer, name: str = "qa") -> None:
    """Attach this whole surface to another Typer app.

    This is the object a comfy-cli plugin entry point would hand over, if
    comfy-cli ever grows one. Nothing here depends on that happening.
    """
    parent.add_typer(app, name=name)


def main() -> None:
    app()
