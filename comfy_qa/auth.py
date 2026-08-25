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

from .gcloud import (
    Gcloud,
    GcloudError,
    console_quota_url,
    quota_request_command,
)

app = typer.Typer(help="Google Cloud sign-in, billing and GPU quota.")
quota_app = typer.Typer(help="GPU quota: what you have, and how to ask for more.")
app.add_typer(quota_app, name="quota")

# How long `request` will wait before handing you back. Approval can take days,
# so waiting forever is not an option; the same command re-enters the wait.
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

    granted = [q for q in quotas if _value_of(q) > 0]
    if not granted:
        results.append(Check(
            "gpu quota", False,
            "zero GPU quota on this project — no GPU instance can start",
            "comfy-qat auth quota request --gpu <type> --region <region>",
        ))
        return results
    results.append(Check(
        "gpu quota", True,
        ", ".join(f"{q.get('quotaId')}={_value_of(q)}" for q in granted[:4]),
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


@quota_app.command("list")
def quota_list_cmd(
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show the GPU quota this project actually has."""
    gc = Gcloud()
    try:
        project = gc.current_project()
        if not project:
            typer.echo("no project set. Run: gcloud config set project <id>", err=True)
            raise typer.Exit(code=2)
        quotas = gc.gpu_quotas(project)
        pending = [
            p for p in gc.quota_preferences(project)
            if "GPU" in (p.get("quotaId") or "").upper()
        ]
    except GcloudError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)

    if as_json:
        typer.echo(json.dumps({"project": project, "quotas": quotas, "pending": pending}, indent=2))
        return

    if not quotas:
        typer.echo(f"{project}: no GPU quotas reported.")
    for quota in quotas:
        typer.echo(f"{quota.get('quotaId'):<45} {_value_of(quota)}")
    if pending:
        typer.echo(f"\n{len(pending)} pending request(s). Watch: {console_quota_url(project)}")


@quota_app.command("request")
def quota_request_cmd(
    quota_id: Annotated[str, typer.Option("--quota-id", help="e.g. NVIDIA_L4_GPUS-per-project-region. Run `auth quota list` to see the ids.")],
    value: Annotated[int, typer.Option("--value", help="How many GPUs you need.")] = 1,
    region: Annotated[Optional[str], typer.Option("--region", help="Region the quota applies to.")] = None,
    justification: Annotated[Optional[str], typer.Option("--justification")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for approval. On by default.")] = True,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the gcloud command instead of running it.")] = False,
) -> None:
    """Ask Google for more GPU quota, then wait for the answer.

    Submitting is instant; approval is not, and may go to a human. A brand-new
    account with no billing history often cannot be granted GPU quota at all
    until it has been billed once — if this is denied immediately, that is the
    usual reason.
    """
    gc = Gcloud()
    try:
        project = gc.current_project()
    except GcloudError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)
    if not project:
        typer.echo("no project set. Run: gcloud config set project <id>", err=True)
        raise typer.Exit(code=2)

    args = quota_request_command(
        project=project, quota_id=quota_id, value=value,
        region=region, justification=justification,
    )

    if dry_run:
        typer.echo("gcloud " + " ".join(args))
        return

    try:
        gc.run(args)
    except GcloudError as exc:
        typer.echo(f"request failed: {exc}", err=True)
        raise typer.Exit(code=1)

    typer.echo(f"requested {quota_id} = {value}" + (f" in {region}" if region else ""))
    typer.echo(f"track it: {console_quota_url(project)}")

    if not wait:
        return

    granted = wait_for_quota(
        lambda: _current_value(gc, project, quota_id), wanted=value,
    )
    if granted:
        typer.echo(f"granted: {quota_id} is now at least {value}")
    else:
        typer.echo(
            "still pending. Approval can take days — run this same command again "
            "to keep waiting, or `comfy-qat auth quota list` to check."
        )
        raise typer.Exit(code=75)  # EX_TEMPFAIL: not an error, not done either


def _current_value(gc: Gcloud, project: str, quota_id: str) -> int:
    for quota in gc.gpu_quotas(project):
        if quota.get("quotaId") == quota_id:
            return _value_of(quota)
    return 0


def wait_for_quota(
    poll: Callable[[], int],
    *,
    wanted: int,
    timeout: int = WAIT_TIMEOUT_SECONDS,
    interval: int = POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll until the quota reaches `wanted`, or the window closes.

    Returns True if granted. The clock and sleep are injectable so tests do not
    take half an hour.
    """
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
