"""Root command surface.

`comfy-qa <feature> <action>`. Each feature is a Typer sub-app registered here
with one line, which is how a later feature is added without touching an
existing one.
"""

from __future__ import annotations

import typer

from . import host

app = typer.Typer(
    help="QA tooling for testing Comfy: know which machine you are testing, "
         "and stamp every result with it.",
    no_args_is_help=True,
)

app.add_typer(host.app, name="host")


def register(parent: typer.Typer, name: str = "qa") -> None:
    """Attach this whole surface to another Typer app.

    This is the object a comfy-cli plugin entry point would hand over, if
    comfy-cli ever grows one. Nothing here depends on that happening.
    """
    parent.add_typer(app, name=name)


def main() -> None:
    app()
