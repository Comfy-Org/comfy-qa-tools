"""`comfy-qat setup` — one command that gets a machine ready.

Setting up used to be four commands and a loop: sign in, check, fix what it named,
check again. That is fine as a diagnostic and wrong as an onboarding path, so this
walks the whole thing and only stops where a human genuinely has to decide.

The individual commands still exist. Anything setup does can also be done
non-interactively — a prompt-only feature is an incomplete one.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import DEFAULT_CONFIG_PATH
from .gcloud import Gcloud, GcloudError, console_quota_url, quota_request_command

# What a first GPU box needs. One card is enough to test with.
DEFAULT_GPU_REQUEST = 1


@dataclass
class Prompts:
    """Everything setup needs from a human. Replaced wholesale in tests."""

    confirm: Callable[[str], bool]
    ask: Callable[[str], str]
    choose: Callable[[str, list[str]], str]
    say: Callable[[str], None]


class SetupStopped(Exception):
    """Setup cannot continue. `fix` is what the human does about it."""

    def __init__(self, message: str, fix: str | None = None) -> None:
        super().__init__(message)
        self.fix = fix


def _needs_login(gc: Gcloud) -> bool:
    """True if nobody is signed in, or the stored credentials have expired.

    An expired session still lists an account, so asking `auth list` is not
    enough — a real call has to be attempted.
    """
    try:
        if not gc.active_account():
            return True
        gc.list_projects()
        return False
    except GcloudError as exc:
        return exc.fix == "gcloud auth login"


def ensure_signed_in(gc: Gcloud, p: Prompts, *, interactive: bool) -> str:
    if _needs_login(gc):
        if not interactive:
            raise SetupStopped(
                "not signed in to Google Cloud", fix="gcloud auth login",
            )
        p.say("Signing you in to Google Cloud — a browser will open.")
        if gc.run_interactive(["auth", "login"]) != 0:
            raise SetupStopped("sign-in did not complete", fix="gcloud auth login")

    account = gc.active_account()
    if not account:
        raise SetupStopped("still not signed in", fix="gcloud auth login")
    p.say(f"signed in as {account}")
    return account


def ensure_project(gc: Gcloud, p: Prompts, *, interactive: bool, wanted: str | None) -> str:
    if wanted:
        gc.set_project(wanted)
        p.say(f"project set to {wanted}")
        return wanted

    current = gc.current_project()
    if current:
        p.say(f"project {current}")
        return current

    projects = [proj.get("projectId") for proj in gc.list_projects() if proj.get("projectId")]
    if not projects:
        raise SetupStopped(
            "this account has no Google Cloud projects",
            fix="https://console.cloud.google.com/projectcreate",
        )
    if len(projects) == 1:
        chosen = projects[0]
    elif not interactive:
        raise SetupStopped(
            f"no project set and {len(projects)} to choose from",
            fix="comfy-qat setup --project <id>",
        )
    else:
        chosen = p.choose("Which project?", projects)

    gc.set_project(chosen)
    p.say(f"project set to {chosen}")
    return chosen


def ensure_billing(gc: Gcloud, p: Prompts, project: str) -> None:
    try:
        enabled = gc.billing_enabled(project)
    except GcloudError as exc:
        raise SetupStopped(str(exc), fix=exc.fix) from exc
    if not enabled:
        raise SetupStopped(
            f"no billing account is linked to {project}, so no instance can start",
            fix=f"https://console.cloud.google.com/billing/linkedaccount?project={project}",
        )
    p.say(f"billing linked to {project}")


def ensure_gpu_quota(
    gc: Gcloud, p: Prompts, project: str, *, interactive: bool, region: str | None,
) -> bool:
    """Returns True if quota exists or was requested. Never blocks setup on it."""
    from .auth import _value_of

    quotas = gc.gpu_quotas(project)
    granted = [q for q in quotas if _value_of(q) > 0]
    if granted:
        p.say(f"GPU quota: {', '.join(q.get('quotaId') for q in granted[:3])}")
        return True

    p.say(
        "GPU quota is zero on this project, so no GPU box can start. Requesting it "
        "is free; approval can take days and a brand-new account is often refused "
        "until it has been billed once."
    )
    if not interactive:
        p.say("skipping the request. Run: comfy-qat auth quota request --quota-id <id>")
        return False
    if not p.confirm("Request GPU quota now?"):
        return False

    ids = [q.get("quotaId") for q in quotas if q.get("quotaId")]
    if not ids:
        p.say("no GPU quota ids reported for this project; nothing to request.")
        return False
    quota_id = ids[0] if len(ids) == 1 else p.choose("Which quota?", ids)
    where = region or p.ask("Which region? (e.g. us-central1)")

    try:
        gc.run(quota_request_command(
            project=project, quota_id=quota_id,
            value=DEFAULT_GPU_REQUEST, region=where or None,
        ))
    except GcloudError as exc:
        p.say(f"the request was refused: {exc}")
        return False

    p.say(f"requested {quota_id} = {DEFAULT_GPU_REQUEST}")
    p.say(f"track it: {console_quota_url(project)}")
    return True


def ensure_host_list(p: Prompts, path: Path | None = None) -> Path:
    from .host import STARTER

    path = path or DEFAULT_CONFIG_PATH
    if path.exists():
        p.say(f"host list at {path}")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STARTER, encoding="utf-8")
    p.say(f"wrote a starter host list to {path}")
    return path


def run_setup(
    gc: Gcloud,
    p: Prompts,
    *,
    interactive: bool = True,
    project: str | None = None,
    region: str | None = None,
    config_path: Path | None = None,
) -> Path:
    """The whole flow. Raises SetupStopped where a human has to act."""
    if gc.available() is None:
        raise SetupStopped(
            "gcloud is not installed or not on PATH",
            fix="https://cloud.google.com/sdk/docs/install",
        )

    ensure_signed_in(gc, p, interactive=interactive)
    chosen = ensure_project(gc, p, interactive=interactive, wanted=project)
    ensure_billing(gc, p, chosen)
    ensure_gpu_quota(gc, p, chosen, interactive=interactive, region=region)
    return ensure_host_list(p, config_path)
