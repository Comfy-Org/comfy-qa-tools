"""`comfy-qat setup` — one command that gets a machine ready.

Setting up used to be four commands and a loop: sign in, check, fix what it named,
check again. That is fine as a diagnostic and wrong as an onboarding path, so this
walks the whole thing and only stops where a human genuinely has to decide.

The individual commands still exist. Anything setup does can also be done
non-interactively — a prompt-only feature is an incomplete one.
"""

from __future__ import annotations

from dataclasses import dataclass
import subprocess
from pathlib import Path
from typing import Callable, NamedTuple

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


class GcloudNumpy(NamedTuple):
    """Where gcloud's NumPy would go, and why it might not.

    Named rather than a bare tuple because BOTH strings are paths to the same
    place and a positional unpack cannot tell them apart. `prefix` exists so the
    sentence a user reads and the writability test they never see are derived
    ONCE: the announcement used to compute its own `Path(python).parent.parent`
    while the gate asked the interpreter for `sys.prefix`, and on any layout
    where those disagree the tool named a directory it had not tested.
    """

    python: str
    prefix: str
    blocked: str
    """"" if NumPy can be installed there; otherwise a sentence saying why not."""


def gcloud_numpy(gc: Gcloud) -> GcloudNumpy | None:
    """gcloud's own interpreter and why NumPy is or is not wanted there.

    Returns None when there is nothing to do.
    """
    import os

    python = gc.python_location()
    if not python or not os.path.exists(python):
        return None
    try:
        if subprocess.run([python, "-c", "import numpy"],
                          capture_output=True, timeout=60).returncode == 0:
            return None
    except (OSError, subprocess.SubprocessError):
        return None

    # Detect, never assume. gcloud's python is a virtualenv in the user's home on
    # this machine; other installs put it under /usr/lib, which is root-owned.
    # A setup command that asks for a root password is a different command, and
    # teaching people to type one into a QA tool is worth more than a fast tunnel.
    # ASK THE INTERPRETER, do not derive it. `Path(python).resolve()` follows
    # `bin/python3.x` OUT of the virtualenv to the base interpreter it was built
    # from, so the two `.parent`s then landed on Homebrew's Cellar rather than on
    # gcloud's venv. Measured here:
    #
    #   python_location  ~/.config/gcloud/virtenv/bin/python3.14
    #   derived          /opt/homebrew/Cellar/python@3.14/.../Versions/3.14
    #   sys.prefix       ~/.config/gcloud/virtenv        <- where pip installs
    #
    # Both directions bite. A venv the user owns, built on a root-owned
    # /usr/bin/python3, is judged UNWRITABLE and skipped — and the skip message
    # then advises `sudo <venv>/bin/python -m pip install`, which leaves
    # root-owned files inside a user's virtualenv. And an unwritable venv built
    # on a writable base was waved through.
    #
    # The probe below already runs this interpreter; ask it the same way.
    try:
        prefix = subprocess.run(
            [python, "-c", "import sys; print(sys.prefix)"],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not prefix:
        return None
    root = Path(prefix)
    if not os.access(root, os.W_OK):
        return GcloudNumpy(python, str(root),
                           f"its Python is not writable by you ({root})")
    return GcloudNumpy(python, str(root), "")


def ensure_tunnel_speed(gc: Gcloud, p: Prompts, *, skip: bool = False) -> None:
    """Put NumPy where gcloud can import it, because every tunnel goes through it.

    gcloud says this itself, on every tunnel it opens:

        To increase the performance of the tunnel, consider installing NumPy.

    Nobody acts on it because the advisory never says WHERE. gcloud runs its own
    virtualenv, so the obvious `pip install numpy` puts it somewhere gcloud
    cannot import from. This asks gcloud.

    Announced rather than asked. The test that puts this on the other side of the
    line from, say, the project: ASK when the answer changes WHAT HAPPENS,
    ANNOUNCE when it only changes HOW FAST. Same tunnels, same boxes, same bill —
    a duration. A prompt with no wrong answer is a keystroke tax. But it is said
    out loud and it names the path, because this modifies software the user did
    not install: if it goes wrong it breaks GCLOUD, not this tool, and nobody
    would connect the two.

    `--only-binary=:all:` is not tidiness. Without it, an interpreter with no
    wheel — gcloud ships 3.14 here — falls back to BUILDING NUMPY FROM SOURCE: a
    compiler, and minutes, at the very front of the command a newcomer meets
    first. With it, such a machine fails in about two seconds and gets a sentence.

    Never fatal, and never a reason to stop a setup. A slow tunnel is a working
    tunnel.
    """
    if skip:
        return
    found = gcloud_numpy(gc)
    if found is None:
        return
    python = found.python
    if found.blocked:
        p.say(f"gcloud's tunnels would be faster with NumPy, but {found.blocked}. "
              f"Skipping. To do it yourself: sudo {python} -m pip install numpy")
        return

    p.say(f"gcloud's tunnel is faster with numpy; installing into its own Python "
          f"({found.prefix})")
    try:
        done = subprocess.run(
            [python, "-m", "pip", "install", "--quiet", "--only-binary=:all:",
             "numpy"],
            capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        p.say(f"NumPy would not install, so tunnels stay slower than they "
              f"could be: {exc}")
        return
    if done.returncode == 0:
        p.say("numpy installed — every tunnel from here is on the fast path")
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
    no_numpy: bool = False,
) -> Path:
    """The whole flow. Raises SetupStopped where a human has to act."""
    if gc.available() is None:
        raise SetupStopped(
            "gcloud is not installed or not on PATH",
            fix="https://cloud.google.com/sdk/docs/install",
        )

    # Before the account sequence, because it is the only part of setup that
    # depends on gcloud alone — no sign-in, no project, no billing, no quota. A
    # newcomer stopped at billing has still had their tunnels made faster
    # forever, which is a real outcome from a run that otherwise produced nothing.
    ensure_tunnel_speed(gc, p, skip=no_numpy)
    ensure_signed_in(gc, p, interactive=interactive)
    chosen = ensure_project(gc, p, interactive=interactive, wanted=project)
    ensure_billing(gc, p, chosen)
    ensure_gpu_quota(gc, p, chosen, interactive=interactive, region=region)
    path = ensure_host_list(p, config_path)
    add_discovered_hosts(gc, p, chosen, path)
    return path
