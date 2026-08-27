"""v0's environment check, kept reachable while it waits for its own release.

This was the whole tool once, and its `main()` and `register()` were the entry
points. Both now live in `cli.py`; only `env_cmd` is still wired up, from there.
Nothing new should be added here — it gets rewritten when `env` ships properly.
"""

from __future__ import annotations

import json
from typing import Annotated, Optional

import typer

from .env import CLOUD_ENVS, LOCAL_DEFAULT, collect
from .render import as_dict, evidence, table

app = typer.Typer(help="QA helpers: check what each environment is actually running.")


@app.command("env")
def env_cmd(
    targets: Annotated[Optional[list[str]], typer.Argument(
        help="Environments to check. Default: all of "
             f"{', '.join(CLOUD_ENVS)} plus local.")] = None,
    expect: Annotated[Optional[str], typer.Option(
        "--expect", help="Fail (exit 1) unless the environment serves this SHA. "
                         "Accepts a short prefix.")] = None,
    platform: Annotated[Optional[str], typer.Option(
        "--platform", help="Platform/build tag for the evidence block, "
                           "e.g. 'macOS 15 · Desktop' or 'Windows 11 · Portable'.")] = None,
    local_url: Annotated[str, typer.Option("--local-url", help="Local ComfyUI address.")] = LOCAL_DEFAULT,
    no_local: Annotated[bool, typer.Option("--no-local", help="Skip the local check.")] = False,
    no_resolve: Annotated[bool, typer.Option("--no-resolve", help="Don't call gh to resolve SHAs.")] = False,
    evidence_for: Annotated[Optional[str], typer.Option(
        "--evidence", help="Print the paste-ready evidence block for this environment.")] = None,
    flags_only: Annotated[Optional[str], typer.Option(
        "--flags", help="Comma-separated flags to name explicitly in the evidence block, "
                        "e.g. 'team_workspaces_enabled,consolidated_billing_enabled'.")] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Report the build and feature-flag state of each environment.

    A failed deploy leaves the old version running and looks completely normal.
    This is the check that catches it.
    """
    unknown = [t for t in (targets or []) if t not in CLOUD_ENVS and t != "local"]
    if unknown:
        typer.echo(f"unknown environment(s): {', '.join(unknown)}. "
                   f"known: {', '.join(CLOUD_ENVS)}, local", err=True)
        raise typer.Exit(code=2)

    # `--json` and `--evidence` are two different answers to the same question,
    # and asking for both silently threw one away. Say so instead.
    if as_json and evidence_for:
        typer.echo("--json and --evidence produce different output; pick one",
                   err=True)
        raise typer.Exit(code=2)
    if flags_only and not evidence_for:
        typer.echo("note: --flags only affects the evidence block; add --evidence "
                   "<env> to see it", err=True)

    reports = collect(targets, local=not no_local, local_url=local_url, resolve=not no_resolve)

    if as_json:
        typer.echo(json.dumps(as_dict(reports), indent=2))
    elif evidence_for:
        match = next((r for r in reports if r.name == evidence_for), None)
        if match is None:
            typer.echo(f"not probed: {evidence_for}", err=True)
            raise typer.Exit(code=2)
        baseline = next((r for r in reports if r.name == "cloud" and r.flags), None)
        only = [f.strip() for f in flags_only.split(",")] if flags_only else None
        blocks = evidence(match, platform=platform, baseline=baseline, only=only)
        typer.echo("\n--- one-line (Slack) ---")
        typer.echo(blocks["oneline"])
        typer.echo("\n--- bug tracker row ---")
        typer.echo(blocks["tracker"])
        typer.echo("\n--- node playbook header ---")
        typer.echo(blocks["playbook"])
    else:
        typer.echo(table(reports))

    if expect:
        checked = [r for r in reports if r.kind == "cloud"]
        if len(checked) != 1:
            # The binary is `comfy-qat`; `comfy qa` was v0's, and pasting it
            # into a shell gets "no such command".
            typer.echo("--expect needs exactly one cloud environment, "
                       "e.g. `comfy-qat env testcloud --expect <sha>`", err=True)
            raise typer.Exit(code=2)
        got = checked[0].sha or ""
        if not got.startswith(expect):
            typer.echo(f"\nFAIL  {checked[0].name} serves {got[:8] or '(none)'}, expected {expect[:8]}",
                       err=True)
            raise typer.Exit(code=1)
        typer.echo(f"\nOK    {checked[0].name} serves {expect[:8]} as expected")

    if any(r.error for r in reports if r.kind == "cloud"):
        raise typer.Exit(code=1)


def register(parent: typer.Typer) -> None:
    """Attach this suite to a host Typer app as `qa`."""
    parent.add_typer(app, name="qa", help="QA helpers for testing Comfy.")


def main() -> None:
    """Standalone entry point, so this works before it is wired into comfy-cli."""
    standalone = typer.Typer(add_completion=False)
    standalone.add_typer(app, name="qa")
    standalone()


if __name__ == "__main__":
    main()
