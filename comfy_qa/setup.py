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


def ensure_tunnel_speed(gc: Gcloud, p: Prompts, *, interactive: bool = True) -> None:
    """Put NumPy where gcloud can import it, because every tunnel goes through it.

    gcloud says this itself, on every single tunnel:

        To increase the performance of the tunnel, consider installing NumPy.

    It is advice worth taking rather than noise worth hiding. IAP forwarding does
    its framing in Python, and NumPy moves that into compiled code — this tool
    opens a tunnel for every `go`, `up`, `open` and `logs`, and pushes
    multi-gigabyte torch downloads through them.

    Done without asking, because there is no question here worth a person's
    attention: it is a dependency of the thing they just asked to have set up, it
    goes into gcloud's OWN virtualenv rather than any environment of theirs, and
    it needs no sudo for exactly that reason. The reason nobody has ever done it
    by hand is that the advisory never says where — and the obvious `pip install
    numpy` puts it somewhere gcloud cannot see.

    Never fatal. A slower tunnel is a slower tunnel; it is not a reason to fail a
    setup that has otherwise worked.
    """
    import os
    import subprocess

    python = gc.python_location()
    # Must be a real interpreter on this machine before anything is run against
    # it. A fake gcloud answers this question with whatever it likes, and setup
    # is driven by one in every test.
    if not python or not os.path.exists(python):
        return
    try:
        if subprocess.run([python, "-c", "import numpy"],
                          capture_output=True, timeout=60).returncode == 0:
            return
    except (OSError, subprocess.SubprocessError):
        return

    p.say("installing NumPy into gcloud's python — it makes every tunnel faster")
    try:
        done = subprocess.run([python, "-m", "pip", "install", "--quiet", "numpy"],
                              capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        p.say(f"NumPy would not install, so tunnels stay slower than they "
              f"could be: {exc}")
        return
    if done.returncode == 0:
        p.say("NumPy installed")
    else:
        p.say(f"NumPy would not install, so tunnels stay slower than they "
              f"could be. By hand: {python} -m pip install numpy")


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
    from .quota import readiness

    p.say("checking GPU quota — this takes about a minute")
    try:
        quotas = gc.gpu_quotas(project)
    except GcloudError as exc:
        # Quota is explicitly allowed to fail: it can take days to change and is
        # never a reason to strand someone mid-setup. The first live run crashed
        # here with a traceback, which is the opposite of that intent.
        p.say(f"could not read GPU quota ({exc}). Check later: comfy-qat quota")
        return False

    # Read through the same filter the rest of the tool uses. Reporting raw ids
    # here meant setup announced COMMITTED-NVIDIA-L4 as available quota — an
    # allowance that cannot start an ordinary box.
    from .quota import GLOBAL_ALLOWANCE

    rows = [row for row in readiness(quotas, region=region) if row.usable]
    # The project-wide allowance is not a card. Naming it alongside L4 and T4
    # reads as a GPU model nobody has heard of.
    cards = [row.gpu for row in rows if row.gpu != GLOBAL_ALLOWANCE]
    if cards:
        p.say(f"GPU quota ready: {', '.join(dict.fromkeys(cards))}")
        return True
    if rows:
        p.say("GPU quota: a project-wide allowance only, no specific card granted")
        return True

    from .quota import available_gpus, resolve

    # Naming the project when only one region was inspected claimed something
    # about the other forty-two that had not been looked at.
    where_checked = f" in {region}" if region else " on this project"
    p.say(
        f"GPU quota is zero{where_checked}, so no GPU box can start. Requesting it "
        "is free; approval can take days and a brand-new account is often refused "
        "until it has been billed once."
    )
    if not interactive:
        # `--quota-id <id>` left a placeholder only another command could fill,
        # while this one is holding the ids already.
        offer = [name for name in available_gpus(quotas) if name != GLOBAL_ALLOWANCE]
        card = offer[0].lower() if offer else "<card>"
        hint = f"comfy-qat quota request --gpu {card}"
        hint += f" --region {region}" if region else " --region <region>"
        p.say(f"skipping the request. Run: {hint}")
        return False
    if not p.confirm("Request GPU quota now?"):
        return False

    cards = available_gpus(quotas)
    if not cards:
        p.say("no GPU quota ids reported for this project; nothing to request.")
        return False
    card = cards[0] if len(cards) == 1 else p.choose("Which card?", cards)
    quota_id = resolve(card, quotas)
    if quota_id is None:
        p.say(f"could not resolve a quota id for {card}.")
        return False
    if card == GLOBAL_ALLOWANCE:
        # The project-wide ceiling has no region dimension. Attaching one built a
        # request Google rejects, from a menu this command offered.
        where = None
    else:
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


def add_discovered_hosts(
    gc: Gcloud, p: Prompts, project: str, path: Path,
) -> int:
    """Add any cloud box that is not in the host list yet. Returns how many.

    Google already knows the zone, machine type, card and operating system of
    every instance. Making someone copy that across by hand is how a host list
    ends up quietly wrong.
    """
    from .config import ConfigError, load
    from .discover import new_hosts, parse as parse_instance, to_toml

    try:
        instances = gc.list_instances(project)
    except GcloudError as exc:
        p.say(f"could not list cloud boxes ({exc}). Add them by hand if needed.")
        return 0

    found = [parse_instance(instance, project) for instance in instances]
    if not found:
        p.say("no cloud boxes on this project yet")
        return 0

    try:
        existing = load(path)
    except ConfigError as exc:
        if path.exists():
            # Treating an unreadable host list as an empty one meant appending a
            # second [hosts.<name>] table for a box already declared in it — and
            # a duplicate table is not valid TOML. One fixable mistake in the
            # file became a file nothing can load, on a re-run that was supposed
            # to change nothing.
            p.say(f"could not read your host list ({exc}), so nothing was added to it")
            return 0
        existing = []

    additions = new_hosts(found, existing)
    if not additions:
        p.say(f"{len(found)} cloud box(es), all already in your host list")
        return 0

    with path.open("a", encoding="utf-8") as handle:
        for box, port in additions:
            handle.write(to_toml(box, port))

    for box, port in additions:
        state = "running" if box.running else "stopped"
        p.say(f"added {box.name} — {box.os}, {box.gpu or 'no GPU'}, {state}, port {port}")
    return len(additions)


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
    ensure_tunnel_speed(gc, p, interactive=interactive)
    path = ensure_host_list(p, config_path)
    add_discovered_hosts(gc, p, chosen, path)
    return path
