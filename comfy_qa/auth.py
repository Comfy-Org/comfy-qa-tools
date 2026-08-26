"""`comfy-qa-tools auth` — is this machine ready to reach Google Cloud, and as whom?

This tool never implements authentication and never holds a credential. gcloud
already owns Google Cloud auth, keeps credentials in its own store and handles
refresh; wrapping that would mean caching tokens on a QA laptop for no gain.

So `auth` answers a question and hands over the command that fixes the answer.
No token is ever printed, logged, or written to a file here.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Annotated, Callable, Optional

import typer

from .quota import (
    GLOBAL_ALLOWANCE,
    available_gpus,
    matches,
    readiness,
    resolve,
    summarise,
)
from .gcloud import (
    Gcloud,
    GcloudError,
    console_quota_url,
    quota_request_command,
)

app = typer.Typer(help="Google Cloud sign-in, billing and GPU quota.")
quota_app = typer.Typer(help="GPU quota: what you have, and how to ask for more.")
app.add_typer(quota_app, name="quota")

# How long `request` will wait before handing you back — for the whole command,
# however many cards it asked for. Approval can take days, so waiting forever is
# not an option; the same command re-enters the wait. Both are read at call time,
# so a test can shorten them without a 30-minute suite.
WAIT_TIMEOUT_SECONDS = 30 * 60
POLL_SECONDS = 30


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str | None = None


def run_checks(gc: Gcloud) -> list[Check]:
    """Check readiness in order, stopping at the first failure.

    Stopping is deliberate: every later check depends on the earlier ones, so
    reporting five failures when one thing is wrong is noise.
    """
    results: list[Check] = []

    if gc.available() is None:
        results.append(Check(
            "gcloud", False, "not installed or not on PATH",
            "https://cloud.google.com/sdk/docs/install",
        ))
        return results
    results.append(Check("gcloud", True, "on PATH"))

    try:
        account = gc.active_account()
    except GcloudError as exc:
        results.append(Check("account", False, str(exc), exc.fix or "gcloud auth login"))
        return results
    if not account:
        results.append(Check("account", False, "nobody signed in", "gcloud auth login"))
        return results
    results.append(Check("account", True, account))

    try:
        project = gc.current_project()
    except GcloudError as exc:
        results.append(Check("project", False, str(exc), exc.fix or "gcloud config set project <id>"))
        return results
    if not project:
        results.append(Check(
            "project", False, "no project set", "gcloud config set project <id>",
        ))
        return results
    results.append(Check("project", True, project))

    try:
        billing = gc.billing_enabled(project)
    except GcloudError as exc:
        results.append(Check(
            "billing", False, str(exc),
            exc.fix or f"https://console.cloud.google.com/billing/linkedaccount?project={project}",
        ))
        return results
    if not billing:
        results.append(Check(
            "billing", False, f"no billing account linked to {project}",
            f"https://console.cloud.google.com/billing/linkedaccount?project={project}",
        ))
        return results
    results.append(Check("billing", True, f"linked to {project}"))

    try:
        quotas = gc.gpu_quotas(project)
    except GcloudError as exc:
        results.append(Check("gpu quota", False, str(exc), exc.fix or "comfy-qat auth quota"))
        return results

    # Read through the same filter the rest of the tool uses. Counting any quota
    # with a non-zero value meant a project holding nothing but a committed or
    # preemptible allowance passed this check — and then could not start a box.
    # Setup was fixed for exactly this; status was reporting green beside it.
    request_fix = "comfy-qat auth quota request --gpu <type> --region <region>"
    usable = [row for row in readiness(quotas) if row.usable]
    cards = [row for row in usable if row.gpu != GLOBAL_ALLOWANCE]
    if not usable:
        results.append(Check(
            "gpu quota", False,
            "zero GPU quota on this project — no GPU instance can start",
            request_fix,
        ))
        return results
    if not cards:
        # The project-wide ceiling is not a card. On its own it starts nothing.
        results.append(Check(
            "gpu quota", False,
            "a project-wide allowance only, no specific card granted",
            request_fix,
        ))
        return results
    results.append(Check(
        "gpu quota", True,
        ", ".join(f"{row.gpu}={row.limit}" for row in cards[:4]),
    ))

    return results


def _value_of(quota: dict) -> int:
    """Pull the effective limit out of a QuotaInfo, tolerating shape changes."""
    details = quota.get("dimensionsInfos") or []
    best = 0
    for entry in details:
        value = (entry.get("details") or {}).get("value")
        if isinstance(value, (int, str)):
            try:
                best = max(best, int(value))
            except (TypeError, ValueError):
                continue
    return best


@app.command("status")
def status_cmd(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Am I signed in, on which project, with billing and GPU quota?"""
    checks = run_checks(Gcloud())

    if as_json:
        typer.echo(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        for check in checks:
            mark = "ok  " if check.ok else "FAIL"
            typer.echo(f"{mark}  {check.name:<10} {check.detail}")
        failed = next((c for c in checks if not c.ok), None)
        if failed and failed.fix:
            typer.echo(f"\nto fix: {failed.fix}")

    if any(not c.ok for c in checks):
        raise typer.Exit(code=1)


@app.command("login")
def login_cmd() -> None:
    """Print the sign-in commands.

    `gcloud auth login` opens a browser and is interactive, so it is handed over
    rather than driven. Running it yourself also leaves you the repro trail.
    """
    typer.echo("Run these, then `comfy-qat auth status`:\n")
    typer.echo("  gcloud auth login")
    typer.echo("  gcloud config set project <your-project-id>")


@quota_app.callback(invoke_without_command=True)
def quota_default(ctx: typer.Context) -> None:
    """Showing what you can run is the safe default."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(quota_list_cmd, as_json=False, region=None, by_region=False)


@quota_app.command("list")
def quota_list_cmd(
    region: Annotated[Optional[str], typer.Option("--region", help="Only this region.")] = None,
    by_region: Annotated[bool, typer.Option(
        "--by-region", help="One row per region instead of one per card.")] = False,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """What can I run today, what is waiting on Google, what did I never ask for."""
    gc = Gcloud()
    try:
        project = _require_project(gc)
        typer.echo("reading quota — this takes about a minute…", err=True)
        quotas = gc.gpu_quotas(project)
        prefs = gc.quota_preferences(project)
    except GcloudError as exc:
        typer.echo(str(exc), err=True)
        if exc.fix:
            typer.echo(f"to fix: {exc.fix}", err=True)
        raise typer.Exit(code=2)

    rows = readiness(quotas, prefs, region=region)
    cards = summarise(rows)

    if as_json:
        typer.echo(json.dumps({
            "project": project,
            "gpus": [asdict(c) for c in cards],
            "by_region": [asdict(r) for r in rows],
        }, indent=2))
        return

    if not rows:
        typer.echo(f"{project}: no GPU quotas reported.")
        return

    notes = {
        "ready": "ready",
        "pending": "pending — waiting on Google",
        "none": "none — request it",
    }

    if by_region:
        width = max([len(r.region) for r in rows] + [6])
        typer.echo(f"{'GPU':<14} {'REGION':<{width}} {'LIMIT':>5}  STATUS")
        for row in rows:
            typer.echo(f"{row.gpu:<14} {row.region:<{width}} {row.limit:>5}  {notes[row.status]}")
        return

    width = max([len(c.where) for c in cards] + [6])
    typer.echo(f"{'GPU':<14} {'LIMIT':>5}  {'WHERE':<{width}}  STATUS")
    for card in cards:
        typer.echo(f"{card.gpu:<14} {card.limit:>5}  {card.where:<{width}}  {notes[card.status]}")

    if not any(c.usable for c in cards):
        typer.echo("\nNothing is usable yet. Ask for one or more cards:")
        typer.echo("  comfy-qat auth quota request --gpu l4,a100 --region us-central1")


@quota_app.command("request")
def quota_request_cmd(
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", help="Card(s) to ask for, comma separated, e.g. l4,a100.")] = None,
    quota_id: Annotated[Optional[str], typer.Option(
        "--quota-id", help="Raw quota id, if you would rather name it exactly.")] = None,
    value: Annotated[int, typer.Option("--value", help="How many of each card.")] = 1,
    region: Annotated[Optional[str], typer.Option("--region")] = None,
    justification: Annotated[Optional[str], typer.Option("--justification")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for approval. On by default.")] = True,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the gcloud calls instead of running them.")] = False,
) -> None:
    """Ask Google for GPU quota — several cards at once — then wait for the answer.

    Requesting costs nothing, so asking for every card you might want up front is
    the right move when approval is the slow part. Submitting is instant; approval
    is not, and may go to a human. A brand-new account with no billing history is
    often refused until it has been billed once.
    """
    if not gpu and not quota_id:
        typer.echo("name what you want: --gpu l4,a100 (or --quota-id for a raw id)", err=True)
        raise typer.Exit(code=2)

    gc = Gcloud()
    try:
        project = _require_project(gc)
        quotas = gc.gpu_quotas(project)
    except GcloudError as exc:
        typer.echo(str(exc), err=True)
        # The exception carries the command that fixes it — dropping it left
        # `no project set` with nowhere to go, while `quota list` said `comfy-qat
        # setup` for the same failure.
        if exc.fix:
            typer.echo(f"to fix: {exc.fix}", err=True)
        raise typer.Exit(code=2)

    wanted: list[tuple[str, str]] = []
    if quota_id:
        wanted.append((quota_id, quota_id))
    for name in (gpu or "").split(","):
        name = name.strip()
        if not name:
            continue
        resolved = resolve(name, quotas, region=region)
        if resolved is None:
            offer = ", ".join(available_gpus(quotas)) or "none"
            # A card this project does have, just not where you asked, used to
            # come back as "no quota for 'l4' … Available: L4" — which reads as
            # a contradiction. Say where it is metered instead.
            elsewhere = sorted({
                row.region for row in readiness(quotas) if matches(name, row.quota_id)
            }) if region else []
            typer.echo(
                f"this project reports no quota for {name!r}"
                + (f" in {region}" if region else "")
                + (f". It is metered in {', '.join(elsewhere)}" if elsewhere else "")
                + f". Available: {offer}", err=True,
            )
            raise typer.Exit(code=2)
        wanted.append((name, resolved))

    submitted: list[tuple[str, str]] = []
    for name, resolved in wanted:
        args = quota_request_command(
            project=project, quota_id=resolved, value=value,
            region=region, justification=justification,
        )
        if dry_run:
            typer.echo("gcloud " + " ".join(args))
            continue
        try:
            gc.run(args)
        except GcloudError as exc:
            typer.echo(f"request for {name} failed: {exc}", err=True)
            continue
        typer.echo(f"requested {name} = {value}" + (f" in {region}" if region else ""))
        submitted.append((name, resolved))

    if dry_run:
        return
    if not submitted:
        # Every request was refused. Exiting 0 told a script it had worked.
        raise typer.Exit(code=2)

    typer.echo(f"track them: {console_quota_url(project)}")
    if not wait:
        return

    # One window for the whole command, not one per card: waiting on l4,a100
    # serially meant the documented half-hour became an hour, and the second
    # card was not even polled until the first gave up.
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    still_waiting = []
    for name, resolved in submitted:
        granted = wait_for_quota(
            lambda rid=resolved: _current_value(gc, project, rid), wanted=value,
            timeout=max(0.0, deadline - time.monotonic()), interval=POLL_SECONDS,
        )
        if granted:
            typer.echo(f"granted: {name}")
        else:
            still_waiting.append(name)

    if still_waiting:
        typer.echo(
            f"still pending: {', '.join(still_waiting)}. Approval can take days — "
            "run `comfy-qat auth quota` to check, or this command again to keep waiting."
        )
        raise typer.Exit(code=75)  # EX_TEMPFAIL: not an error, not done either


def _require_project(gc: Gcloud) -> str:
    project = gc.current_project()
    if not project:
        raise GcloudError("no project set", fix="comfy-qat setup")
    return project


def _current_value(gc: Gcloud, project: str, quota_id: str) -> int:
    for quota in gc.gpu_quotas(project):
        if quota.get("quotaId") == quota_id:
            return _value_of(quota)
    return 0


def wait_for_quota(
    poll: Callable[[], int],
    *,
    wanted: int,
    timeout: float | None = None,
    interval: float | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
) -> bool:
    """Poll until the quota reaches `wanted`, or the window closes.

    Returns True if granted. Every default is resolved here rather than in the
    signature: bound as defaults they were captured at import, so neither the
    constants nor the clock could be replaced from outside, and driving the wait
    through the command itself meant a test that really slept for half an hour.
    """
    timeout = WAIT_TIMEOUT_SECONDS if timeout is None else timeout
    interval = POLL_SECONDS if interval is None else interval
    sleep = sleep or time.sleep
    now = now or time.monotonic

    deadline = now() + timeout
    while True:
        try:
            if poll() >= wanted:
                return True
        except GcloudError:
            pass  # a transient read failure is not a denial; keep waiting
        if now() >= deadline:
            return False
        sleep(interval)
