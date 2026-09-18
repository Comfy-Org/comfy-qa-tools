"""`comfy-qat setup` — one command that gets a machine ready.

Setting up used to be four commands and a loop: sign in, check, fix what it named,
check again. That is fine as a diagnostic and wrong as an onboarding path, so this
walks the whole thing and only stops where a human genuinely has to decide.

The individual commands still exist. Anything setup does can also be done
non-interactively — a prompt-only feature is an incomplete one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import subprocess
from pathlib import Path
from typing import Callable, NamedTuple

from .config import DEFAULT_CONFIG_PATH
from .gcloud import Gcloud, GcloudError, console_quota_url, is_already_asked

# What a first GPU box needs. One card is enough to test with.
DEFAULT_GPU_REQUEST = 1

# The project-wide ceiling to ask for, and the one number in this file that is a
# judgement rather than a reading off the card table.
#
# `GPUS-ALL-REGIONS-per-project` caps the total GPUs running across every card at
# once, whatever the per-card grants say. It is **1** on this project, which is
# why holding both L4 and T4 quota still cannot run two boxes at the same time —
# and running two at once is the tool's whole reason for existing: reproduce a
# defect on one card, or one OS, and compare against another.
#
# So 2, not 1 and not 8. 1 is the number that is already there and already the
# binding constraint. 8 would be asking a human reviewer at Google for seven
# machines' worth of headroom to justify a comparison between two — and an
# over-ask is not free, because the reviewer reading it is deciding whether this
# project is serious. 2 is the smallest number that changes anything.
#
# It rises above 2 on its own where the card table says a card needs more: an
# H100 comes only as `a3-highgpu-8g`, eight cards, so a project that can ask for
# one needs a ceiling of 8 to start it. That is read from `Card.count` rather
# than typed here — see `_ceiling_wanted`.
CEILING_REQUEST = 2

# Where a region-scoped request is made when nobody said. Only reached when
# `--region` was not given AND the project holds no GPU grant anywhere to copy
# the region from, which is the genuinely-fresh-project case this step is for.
FALLBACK_REGION = "us-central1"

# Sent with every request this step makes, and printed verbatim before anything
# is submitted. It is what a human reviewer at Google reads, and "left blank" is
# not a thing to discover after waiting days for an answer — gcloud's `--email`
# help says a request is refused "in case further information is required to make
# a decision", so the cheapest way to avoid needing a follow-up is to answer the
# question before it is asked.
# It describes what the tool does, in the terms the reviewer is deciding about:
# how many, for how long, and what for.
DEFAULT_JUSTIFICATION = (
    "Automated QA of ComfyUI on GPU instances. One or two short-lived instances "
    "at a time, created and deleted per test run by comfy-qa-tools "
    "(github.com/Comfy-Org/comfy-qa-tools). Not for training or sustained load."
)


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

    An expired session still lists an account, so asking `gcloud auth list` is not
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
        # Say WHOSE choice this is. Everything after this line — the billing
        # check, the quota request, the boxes — happens on this project, and a
        # bare "project proj-1" reads as a report rather than as a decision the
        # tool just made on the user's behalf. Somebody who has been working in
        # another project all week gets their GPU quota requested somewhere they
        # did not intend, and the only clue was a noun.
        p.say(f"project {current} — gcloud's current project, used as-is. "
              f"To use another: comfy-qat setup --project <id>")
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


def _drivable(name: str) -> bool:
    """Can this tool actually bring a card of this name up?

    The one filter, used by every sentence in this file that names a card. The
    driver installed on every Linux box here is the open NVIDIA kernel module and
    it needs a GSP, so P100/V100/P4/K80 quota starts an instance that never sees
    its own GPU — see `create.GSP_ARCHITECTURES`. A card the table has never
    heard of is not refused; "not one of ours" is not "known to be broken".
    """
    from .create import no_gsp

    return not no_gsp(name)


def _stranded_note(stranded: list[str]) -> str:
    """"K80 is granted too" / "K80, P100 are granted too", agreeing in number.

    Live output read "(K80, P100, P4, V100 is granted too, and this tool cannot
    drive it — no GSP)": plural list, singular verb, singular pronoun, on the
    first screen a new user sees.
    """
    names = ", ".join(stranded)
    if len(stranded) == 1:
        return f"{names} is granted too, and this tool cannot drive it"
    return f"{names} are granted too, and this tool cannot drive them"


def ensure_gpu_quota(
    gc: Gcloud, p: Prompts, project: str, *, region: str | None,
) -> list[dict] | None:
    """Report what this project can run today. Never blocks setup on it.

    Returns the raw quota records so the request step does not pay for a second
    read — `quotas info list` takes about a minute, and running it twice in one
    `setup` is two minutes of a newcomer watching a spinner for one question.
    None means the read failed and there is nothing to plan from.
    """
    from .create import drivable_cards
    from .quota import GLOBAL_ALLOWANCE, readiness

    p.say("checking GPU quota — this takes about a minute")
    try:
        # EVERY compute quota, not only the GPU ones. The CPU quota a GPU VM
        # spends for an N1 box lives under `CPUS-per-project-region`, which is
        # invisible to a reader that keeps only ids containing "GPU". Same fetch,
        # same minute — `gpu_quotas` filtered this list client-side anyway.
        quotas = gc.compute_quotas(project)
    except GcloudError as exc:
        # Quota is explicitly allowed to fail: it can take days to change and is
        # never a reason to strand someone mid-setup. The first live run crashed
        # here with a traceback, which is the opposite of that intent.
        p.say(f"could not read GPU quota ({exc}). Check later: comfy-qat quota")
        return None

    # VALIDATED THE MOMENT THERE IS ENOUGH TO VALIDATE IT, and before `region`
    # is used for anything. Round nine hoisted this above every early return in
    # `ensure_quota_requests` — true of that function, and this CALLER runs one
    # frame earlier, so the live tool went on printing "a project-wide allowance
    # only, no specific card granted" for a typo'd region on a project holding
    # six granted cards. A fix that is real in the function and absent from the
    # command.
    #
    # It cannot move any earlier: validating a region needs the quota records,
    # and this is the call that fetches them. So it goes between the fetch and
    # the first use, which is the earliest point at which it can be asked.
    _require_real_region(gc, project, quotas, region)

    # Read through the same filter the rest of the tool uses. Reporting raw ids
    # here meant setup announced COMMITTED-NVIDIA-L4 as available quota — an
    # allowance that cannot start an ordinary box.
    rows = [row for row in readiness(quotas, region=region) if row.usable]
    # The project-wide allowance is not a card. Naming it alongside L4 and T4
    # reads as a GPU model nobody has heard of.
    named = [row.gpu for row in rows if row.gpu != GLOBAL_ALLOWANCE]
    cards = [name for name in named if _drivable(name)]
    stranded = [name for name in named if not _drivable(name)]
    if cards:
        line = f"GPU quota ready: {', '.join(dict.fromkeys(cards))}"
        if stranded:
            line += f" ({_stranded_note(list(dict.fromkeys(stranded)))} — no GSP)"
        p.say(line)
    elif stranded:
        p.say(
            f"GPU quota is only {', '.join(dict.fromkeys(stranded))}, which this "
            f"tool cannot drive: the open NVIDIA kernel module it installs needs a "
            f"GPU System Processor, and only Turing and newer cards have one. "
            f"Ask for one that works: comfy-qat quota request --gpu "
            f"{','.join(drivable_cards())}"
        )
    elif rows:
        p.say("GPU quota: a project-wide allowance only, no specific card granted")
    else:
        # Naming the project when only one region was inspected claimed something
        # about the other forty-two that had not been looked at.
        where_checked = f" in {region}" if region else " on this project"
        p.say(
            f"GPU quota is zero{where_checked}, so no GPU box can start. Requesting "
            "it is free; approval can take days and a brand-new account is often "
            "refused until it has been billed once."
        )
    return quotas


# --- asking for everything that is missing, once ----------------------------
#
# The goal, in the user's words: "if a user installs it, all GPUs are ready, and
# are usable by the tool, or pending being accepted, and the request should
# already be submitted". So this works out what is missing and asks for it,
# rather than printing a command and hoping.
#
# Everything below exists because a quota preference is PERMANENT — "The ability
# to delete a QuotaPreference is not supported" — and is read by a person at
# Google. Four properties, in the order they matter:
#
#   nothing is submitted without the exact list being printed first;
#   nothing already granted, pending or answered is asked for again;
#   a re-run UPDATES the same preference rather than filing a second one;
#   a card that cannot be asked for is a line of output, never an exception.

GRANTED = "granted"
PENDING = "pending"
DENIED = "denied"
REQUEST = "request"
UNAVAILABLE = "unavailable"
# GPU quota is fine and the card still cannot start. Reachable today only for N1
# cards, whose machines consume `CPUS-per-project-region`; every other family
# this tool orders is documented as needing no CPU quota at all, so none of them
# can arrive here. Its own outcome rather than a note on GRANTED, because
# "granted" is the word that made the GSP bug invisible for as long as it was: a
# card the tool reports as ready and then cannot bring up.
BLOCKED = "blocked"


@dataclass(frozen=True)
class QuotaAsk:
    """One card, and what setup would do about it. `value` is 0 unless asking."""

    card: str
    """The `--gpu` spelling, or "" for the project-wide ceiling."""
    label: str
    """What a person calls it — `L4`, `any (global)`."""
    outcome: str
    detail: str
    target: object | None = None
    """A `quota.Target`: the id AND the dimensions that pin it."""
    value: int = 0

    @property
    def submits(self) -> bool:
        return self.outcome == REQUEST


def _too_big_for(plan: list[QuotaAsk], ceiling: int, *, raising: bool = False) -> str:
    """The clause naming cards this ceiling cannot start, or "".

    ONE FUNCTION because there are two branches and only one of them had the
    sentence. Asking for a raise and being refused a raise need the same fact —
    that an `a3-highgpu-8g` is eight GPUs and the ceiling is one — and the
    refused branch is where it matters more, because nothing about it improves by
    running `setup` again.
    """
    from .create import card_named

    bigger = sorted({
        card.name for ask in plan if ask.outcome == REQUEST and ask.card
        for card in [card_named(ask.card)]
        if card is not None and card.count > ceiling
    })
    if not bigger:
        return ""
    needs = max(card_named(name).count for name in bigger)
    if raising:
        return (f". If {' or '.join(bigger)} is granted, run setup again to "
                f"raise this further — that card comes as more GPUs than one "
                f"machine")
    return (f". NOTE: {' and '.join(bigger)} needs {needs} of this ceiling and it "
            f"is {ceiling}, so that request cannot start a machine even if "
            f"Google grants it")


def _ceiling_wanted(cards: list[QuotaAsk]) -> int:
    """How much project-wide ceiling the cards this project HOLDS actually need.

    `Card.count` is how many GPUs one instance of that card comes as, and
    `GPUS-ALL-REGIONS` counts GPUs rather than machines. An H100 is
    `a3-highgpu-8g` — eight cards — so holding H100 quota and a ceiling of 2
    means a create that passes the per-card gate and fails at the ceiling.

    GRANTED ONLY, and this counted REQUEST too until it was measured on a fresh
    project. **Every GCP project reports `GPUS-PER-GPU-FAMILY-per-project-region`
    with `gpu_family=NVIDIA_H100` as service metadata** — present, `details: {}`,
    which is this API's zero — so H100 is a REQUEST on every project that has
    never held one, which is all of them. Counting requests therefore made this
    `max(2, 8)` = 8 UNIVERSALLY, and `CEILING_REQUEST` was dead code:

        fresh install, measured: any (global) request 8

    Which is the exact thing `CEILING_REQUEST`'s own comment argues against —
    asking a reviewer for seven machines' worth of headroom to justify a
    comparison between two. The code contradicted its stated design, and the test
    covering it pinned 8 as the exceptional case when it was the only case.

    Ceiling headroom is only worth anything for a card you can actually start, so
    the question is "what do I hold", not "what have I asked for". The gap this
    leaves is real and small: a project that is GRANTED an H100 while the ceiling
    is 2 cannot start it — and `create` refuses up front, saying so, rather than
    spending. The next `setup` run then sees H100 as GRANTED and raises the
    ceiling to 8, which is also the first moment a reviewer can be told something
    true: this project holds H100 quota and cannot use it.
    """
    from .create import card_named

    counts = [
        card.count
        for ask in cards
        if ask.outcome == GRANTED
        for card in [card_named(ask.card)]
        if card is not None
    ]
    return max([CEILING_REQUEST, *counts])


def request_region(
    quotas: list[dict], preferences: list[dict] | None, region: str | None,
) -> tuple[str, str]:
    """Which region a region-scoped request names, and why that one.

    Only the FAMILY shape needs this. Per-card quota is granted across every
    region in one grant, so a per-card request carries no region at all — see
    `quota.resolve_target`. `GPUS-PER-GPU-FAMILY-per-project-region` is genuinely
    per region, so one has to be chosen, and choosing it silently is how somebody
    ends up approved somewhere they will never use it.

    THE FIRST VERSION OF THIS TOOK THE FIRST REGION A CARD IS GRANTED IN, and on
    the live project it chose **africa-south1** — which is not a mistake in the
    lookup, it is the honest answer to the wrong question. The L4 grant covers 43
    regions, `regions_with_quota` sorts them, and `africa-south1` is first
    alphabetically. A grant spanning every region carries NO information about
    where anybody works; reading a preference out of it invents one.

    So a grant is only evidence when it is unambiguous — exactly one region — and
    the signal used otherwise is what the project has ASKED for before, which is
    an expressed intention rather than a side effect of how Google grants. On the
    live project that is `us-central1`, named by three of its five requests.

    Never a latency probe. This runs inside `setup`, where a socket to Google per
    region would add minutes to answer a question whose honest answer is
    "wherever you already work".
    """
    if region:
        return region, "you asked for it"

    from collections import Counter

    from .create import drivable_cards
    from .quota import asks, regions_with_quota

    # ACROSS EVERY CARD, not the first card that happens to have one region.
    # This asked `regions_with_quota` per card and returned as soon as any card
    # named exactly one — so a project holding A100 in europe-west4 and L4 in
    # us-central1 returned `europe-west4`, with the sentence "the only region
    # this project holds GPU quota in", which is FALSE. europe-west4 won because
    # `drivable_cards()` is sorted and `a100` precedes `l4`; reverse the two
    # grants and the answer flips.
    #
    # That is the africa-south1 bug again, one level up — a list order standing
    # in for a judgement — and this docstring claimed to have prevented it. The
    # claim is only true if the question is asked of the whole project, because
    # "the ONLY region" is a statement about the project and nothing less.
    held: set[str] = set()
    for key in drivable_cards():
        held |= set(regions_with_quota(key, quotas))
    if len(held) == 1:
        return next(iter(held)), "the only region this project holds GPU quota in"

    # And the tiebreak is explicit rather than inherited. `Counter.most_common`
    # breaks ties by INSERTION ORDER, so two regions asked for equally often gave
    # different answers depending on the order gcloud happened to list the
    # preferences in — which is not a contract gcloud offers. Most-asked-for
    # first, then alphabetical, so the same project always gets the same region.
    #
    # It matters more here than a tiebreak usually would: a preference's
    # dimensions are IMMUTABLE and a preference cannot be deleted, so a region
    # chosen by list order can never be corrected afterwards.
    wanted = Counter(ask.region for ask in asks(preferences) if ask.region)
    if wanted:
        best = min(wanted.items(), key=lambda item: (-item[1], item[0]))[0]
        return best, "where this project has asked for GPU quota before"
    return FALLBACK_REGION, "no GPU quota asked for anywhere yet, so the default"


# `x if x is not None else 0`, NEVER `x or 0`, on an optional integer.
#
# `value or DEFAULT_VALUE` in `quota request` turned an explicit `--value 0` into
# 1 and announced "LOWERING the standing request from 8 to 1" about a number
# nobody typed — on the one path that changes state at Google, two lines below a
# comment explaining that a default must never overwrite a choice.
#
# Every `(x or 0)` below it was correct: `allowance` returns None for "no such
# quota" and 0 for "reported at zero", and both mean the same for a `>=`. They
# are written out anyway, because the reader who copies the shape into a place
# where None and 0 differ gets no warning from it, and this file has now proved
# that twice.


def _cpu_shortfall(card, quotas: list[dict], region: str) -> str:
    """Why CPU quota stops this card, or "" if it does not.

    N1 ONLY. Google's resource-usage page says A2, A3, A4, G2 and G4 VMs need
    "only ... the required GPU quotas ... You don't need to request CPU quotas",
    so this is about T4, V100, P100, P4 and K80 — the cards that attach to N1 —
    and about nothing else.

    It was briefly a gate on every card, built from two real readings and an
    invented conclusion, and it told users an A100 was blocked by a quota that
    does not gate it. Reported when it is real, because "granted" about a card
    that cannot start is the shape of the GSP bug; silent when it is not, because
    naming the wrong gate sends the next person to fix the wrong thing.
    """
    from .quota import (
        UNLIMITED, cpu_allowance, cpu_ceiling, cpu_quota_applies, cpu_target,
    )

    # A2, A3, A4, G2 and G4 consume NO CPU quota — Google's resource-usage page
    # says so outright, and this module claimed otherwise until it was checked.
    # Only N1 (T4, V100, P100, P4, K80) is gated here.
    if not card.vcpus or not cpu_quota_applies(card.machine_type):
        return ""
    held = cpu_allowance(card.machine_type, quotas, region=region)
    target = cpu_target(card.machine_type, quotas, region=region)
    if held is not None and held != UNLIMITED and held < card.vcpus:
        return (f"{target.quota_id if target else 'CPU quota'} is {held} and "
                f"{card.machine_type} needs {card.vcpus} vCPU, so this card "
                f"cannot start even with GPU quota")

    ceiling = cpu_ceiling(quotas)
    if ceiling is not None and ceiling != UNLIMITED and ceiling < card.vcpus:
        return (f"{card.machine_type} needs {card.vcpus} vCPU and "
                f"CPUS-ALL-REGIONS-per-project is {ceiling} across the whole "
                f"project, so this card cannot start anywhere even with GPU quota")
    return ""


def _cpu_request(card, quotas: list[dict], preferences, region: str):
    """A CPU request to file alongside the GPU one, or None.

    Reached only for a family that genuinely consumes CPU quota — N1 today — and
    only for a family-specific id like `N1-CPUS-per-project-region` where the
    project has one, which is a small targeted request about one card.

    The general pool and `CPUS-ALL-REGIONS-per-project` are NOT asked for. They
    govern every VM on the project, GPU or not, so raising them is a different
    conversation with a different reviewer — not something a setup command should
    file on somebody's behalf. Those are reported and left to a human.
    """
    from .quota import UNLIMITED, cpu_allowance, cpu_target

    # No `cpu_quota_applies` check here on purpose: `cpu_allowance` returns
    # UNLIMITED for a waived family, so the `held == UNLIMITED` test below already
    # covers it. A second guard for the same thing is one nothing can exercise —
    # a mutation sweep proved it, surviving the removal of the copy that was here.
    if not card.vcpus:
        return None
    target = cpu_target(card.machine_type, quotas, region=region)
    if target is None or not target.quota_id.startswith(machine_family_of(card)):
        return None
    held = cpu_allowance(card.machine_type, quotas, region=region)
    if held == UNLIMITED or (held if held is not None else 0) >= card.vcpus:
        return None
    return target


def machine_family_of(card) -> str:
    from .quota import machine_family

    return machine_family(card.machine_type)


def _with_cpu_note(ask: QuotaAsk, quotas: list[dict], where: str) -> QuotaAsk:
    """Append the CPU gate to a card's line, wherever that line came from."""
    from .create import card_named

    card = card_named(ask.card) if ask.card else None
    if card is None:
        return ask
    short = _cpu_shortfall(card, quotas, where)
    if not short:
        return ask
    detail = f"{ask.detail}. Also: {short}" if ask.detail else short
    return QuotaAsk(ask.card, ask.label, ask.outcome, detail, ask.target, ask.value)


def _add_cpu_ask(plan, key, card, quotas, preferences, where, settled) -> None:
    """Put the card's CPU request on the plan, if it needs one and can have one."""
    target = _cpu_request(card, quotas, preferences, where)
    if target is None:
        return
    label = f"{card.name} CPU"
    blocked = settled(target, label)
    if blocked is not None:
        # `card=""`, like the ceiling. A CPU ask is about the same card but it is
        # NOT the card's own row, and sharing the key means anything grouping the
        # plan by card silently loses one of the two.
        plan.append(QuotaAsk("", label, blocked.outcome, blocked.detail, target))
        return
    plan.append(QuotaAsk(
        "", label, REQUEST,
        f"will ask Google for {card.vcpus} vCPU of {target.quota_id} in {where} "
        f"— GPU quota alone does not start {card.machine_type}",
        target, value=card.vcpus))


def _one_ask_per_pair(plan: list[QuotaAsk]) -> list[QuotaAsk]:
    """Collapse asks that address the same (quota id, dimensions) to the largest."""
    best: dict[tuple, QuotaAsk] = {}
    order: list[tuple] = []
    for ask in plan:
        if ask.target is None or not ask.submits:
            key: tuple = ("_", id(ask))
        else:
            key = (ask.target.quota_id, ask.target.dimensions)
        if key not in best:
            best[key] = ask
            order.append(key)
        elif ask.value > best[key].value:
            # Keep the bigger ask, and say who else is behind it, so the line a
            # person reads names every card the number is for.
            merged = best[key]
            best[key] = QuotaAsk(ask.card, ask.label, ask.outcome, ask.detail,
                                 ask.target, ask.value)
            del merged
    return [best[key] for key in order]


def plan_quota(
    quotas: list[dict], preferences: list[dict] | None, *, region: str | None = None,
) -> list[QuotaAsk]:
    """What setup would ask Google for, and what it would leave alone.

    Pure: no gcloud, no prompting, no submitting. Everything that decides whether
    a request is sent is decided here, so the decision can be tested without a
    fake that has to be trusted not to submit.

    The card set is `create.CARDS`, filtered to the ones this tool can drive.
    That is the same table `create --gpu` and `quota request --gpu` read, which is
    the point: a card added to the table is asked for by setup on the next run
    with nothing else edited, and a card this tool cannot bring up is never asked
    for at all — approval takes days and ends in `create` refusing anyway.

    ONE REGION FOR THE FAMILY CARDS, not a fan-out. Google is explicit that
    "batching requests can increase the amount of time it takes ... to review
    your request" and asks that requests be grouped by product and area, and the
    project ceiling is 300 increase requests a day. Five families across 43
    regions is 215 permanent, undeletable records to run one box. So the family
    cards are asked for where the tester works, and `comfy-qat quota request
    --gpu h100 --region <other>` covers the rest deliberately.
    """
    from .create import CARDS, drivable_cards, unspendable
    from .quota import (
        GLOBAL_ALLOWANCE, ON_DEMAND, UNLIMITED, allowance, asks, asks_about,
        best_pool, global_allowance, global_target, pools_for, resolve_target,
    )

    where, _why = request_region(quotas, preferences, region)
    standing = asks(preferences)

    def settled(target, label: str) -> QuotaAsk | None:
        """Every reason not to ask again, or None to go ahead.

        ABOUT THE CARD, not about the exact dimensions, and that distinction cost
        a real re-ask to find. `a100-80-euw4` on the live project was refused in
        europe-west4; an all-regions A100-80GB request has different dimensions,
        so an exact match found nothing and this re-asked for a card Google had
        already turned down. The exact match is still what ADDRESSES a preference
        — see `quota.request_plan` — but it is the wrong question here.

        `asks_about` still honours `gpu_family`, so a refused RTX PRO 6000 does
        not read as a refused H100: five cards share one quota id.
        """
        found = asks_about(target, preferences)
        waiting = next((a for a in found if a.state == "pending"), None)
        if waiting is not None:
            return QuotaAsk("", label, PENDING,
                            "already asked, and Google has not answered yet",
                            target)
        refused = next((a for a in found if a.state == "denied"), None)
        if refused is not None:
            where_refused = f" in {refused.region}" if refused.region else ""
            return QuotaAsk("", label, DENIED,
                            f"Google refused an earlier request{where_refused} "
                            f"({refused.preference_id}). Not asked again "
                            f"automatically", target)
        short = next((a for a in found if a.state == "partial"), None)
        if short is not None:
            return QuotaAsk("", label, DENIED,
                            f"Google answered with {short.granted} of the "
                            f"{short.preferred} asked for. Not asked again "
                            f"automatically", target)
        return None

    plan: list[QuotaAsk] = []
    for key in drivable_cards():
        card = CARDS[key]
        # Every spelling Google might meter this card under, from the table, and
        # BOTH shapes — per-card id first, `gpu_family` dimension second. Which
        # one a project uses is read from the project, never assumed: this one
        # has no `NVIDIA-H100-...` row at all, while Google's own CLI guide uses
        # the family form as its example.
        target = next(
            (found for spelling in (card.key, *card.quota_names)
             for found in [resolve_target(spelling, quotas, region=where,
                                          family=card.quota_family)]
             if found),
            None,
        )
        if target is None:
            # Requestability is a fact about the PROJECT, not about the card, so
            # it is read per run and reported per card rather than raised.
            #
            # AND THE SENTENCE SAYS WHAT IT KNOWS. `resolve_target` was called
            # with `region=where`, so a miss means "no row matched THAT REGION" —
            # and the line said "this project does not meter L4 quota", about a
            # card granted at 1 across forty-three regions. Same conflation as
            # reading a granted-regions list as a metered one, wearing different
            # clothes: the narrower fact was in hand and the broader claim was
            # printed. With the region named it degrades honestly instead.
            plan.append(QuotaAsk(
                key, card.name, UNAVAILABLE,
                f"this project meters no {card.name} quota in {where}, so there "
                f"is nothing to ask for there"))
            continue

        # ON-DEMAND FIRST, because it is the pool `create` actually spends.
        #
        # This block used to run BEFORE the on-demand check and to exclude
        # on-demand from its own search, which inverted the intent twice over. A
        # card holding BOTH grants took the Spot branch, so an ordinary L4 was
        # reported as "granted as Spot — reclaimable mid-run" while `quota list`
        # called it plainly ready in the same minute. And a card whose ONLY grant
        # was Spot was marked GRANTED with nothing filed — while `create` gates
        # on on-demand and this tool has no Spot support at all, so `setup`
        # permanently declined to file the one request that would make the card
        # usable, and reported success. Ali asked for "ready, or pending with the
        # request submitted"; that was a third state, neither, and told it is
        # fine.
        #
        # So a grant in another pool is a NOTE on the request, never a substitute
        # for it. Revisit when `create` can order Spot — `docs/spot-instances.md`.
        held = allowance(card.key, quotas, region=where)
        if held == UNLIMITED or (held if held is not None else 0) >= card.count:
            if _cpu_shortfall(card, quotas, where):
                # NOT "granted". An N1 card whose CPU quota cannot fit its
                # machine is one the tool would report as ready and then fail to
                # start — the GSP bug's exact shape. `_cpu_shortfall` is silent
                # for every family Google waives, so only N1 reaches this, and
                # `_with_cpu_note` below supplies the reason.
                plan.append(QuotaAsk(key, card.name, BLOCKED, "", target))
            else:
                plan.append(QuotaAsk(
                    key, card.name, GRANTED,
                    "unlimited" if held == UNLIMITED else f"{held} granted",
                    target))
            _add_cpu_ask(plan, key, card, quotas, preferences, where, settled)
            continue
        blocked = settled(target, card.name)
        if blocked is not None:
            # SAY BOTH GATES, even when the GPU one has already answered. A
            # card refused on GPU quota that its CPU quota would also refuse is
            # behind two doors, and knowing that changes what a person does next.
            # Only N1 can be in that position — `_cpu_shortfall` is silent for
            # every family Google waives.
            plan.append(QuotaAsk(key, card.name, blocked.outcome,
                                 blocked.detail, target))
            _add_cpu_ask(plan, key, card, quotas, preferences, where, settled)
            continue
        # Say the region whenever the request carries one, which is every
        # region-scoped quota id and not only the family ones. This said "across
        # all regions" for per-card cards — describing the GRANT, while the
        # REQUEST names a single region — so the plan told the user something
        # that was true of the allowance and false of what was about to be sent.
        where_named = target.dims.get("region")
        scope = f" in {where_named}" if where_named else " with no region"
        # Name an allowance held in a pool this tool cannot spend, and say why
        # the request is still going out. `unspendable` is shared with
        # `quota list` so the two surfaces cannot tell different stories again.
        elsewhere = best_pool([
            pool for pool in pools_for(card.name, quotas, preferences,
                                       region=where)
            if pool.name != ON_DEMAND and pool.status == "ready"
        ])
        if elsewhere is not None:
            scope += (f". You hold {elsewhere.limit} as {elsewhere.name} "
                      f"({elsewhere.cost}) — {unspendable(card.key, elsewhere.name)}")
        plan.append(QuotaAsk(key, card.name, REQUEST,
                             f"will ask Google for {card.count}{scope}",
                             target, value=card.count))
        _add_cpu_ask(plan, key, card, quotas, preferences, where, settled)

    # ONE PLACE. The CPU note was being appended at three of the four branches a
    # card can leave the loop by, which is how the fourth — a card being
    # requested — silently stopped mentioning the gate that decides it. The bug
    # is the structure, not the example that exposed it: a note appended at three
    # of four exits is one the fourth never carries.
    plan = [_with_cpu_note(ask, quotas, where) for ask in plan]

    # CARDS SHARE QUOTAS, so two cards can ask for the same one. Found when A100
    # and A100-80GB — both A2 — each produced a request for the same
    # (quota id, dimensions) pair. Google refuses the second outright ("Quota
    # Preference with dimension ... already exist"), and filing two is exactly the
    # duplicate this feature is built to avoid. The A2 case has since gone (CPU
    # quota does not gate A2), and the collapse stays because the shape does not:
    # any two cards sharing a quota id and region would do the same.
    #
    # Collapsed to one ask at the LARGEST value any card needs, because the quota
    # has to accommodate whichever card is started.
    plan = _one_ask_per_pair(plan)

    ceiling_target = global_target(quotas)
    wanted = _ceiling_wanted(plan)
    if ceiling_target is None:
        plan.append(QuotaAsk(
            "", GLOBAL_ALLOWANCE, UNAVAILABLE,
            "this project reports no project-wide GPU ceiling"))
        return plan

    ceiling = global_allowance(quotas)
    if ceiling == UNLIMITED or (ceiling if ceiling is not None else 0) >= wanted:
        plan.append(QuotaAsk(
            "", GLOBAL_ALLOWANCE, GRANTED,
            "unlimited" if ceiling == UNLIMITED else f"{ceiling} granted",
            ceiling_target))
        return plan

    # A SATISFIED PREFERENCE AT TOO LOW A NUMBER IS NOT A REASON TO STOP. The
    # ceiling on this project was explicitly requested at 1 and approved at 1 —
    # `gpus-all-regions-1`, "Quota request approved to 1" — so "there is already
    # a preference here" would read as "nothing to do" about the single limit
    # that makes two boxes at once impossible. `settled` only ever returns for
    # pending or answered-and-still-short, which is why this is safe.
    # THE CARDS THAT WILL NOT FIT, computed BEFORE the early return rather than
    # after it. This sentence was attached only to the branch that asks for a
    # raise, so on a project whose ceiling is DENIED — the one case that will not
    # fix itself on the next run — `settled` returned here and it was never
    # built. `setup` would file an 8-GPU H100 request against a ceiling of 1 and
    # print the two facts four lines apart with nothing joining them.
    # `ceiling`, NOT `wanted`. "this ceiling" means the limit in force; `wanted`
    # is what we would ASK for, floored at CEILING_REQUEST, so the sentence read
    # "needs 8 of this ceiling and it is 2" about a ceiling granted at 1 — the 2
    # being the refused request. The `raising=True` call below is right to pass
    # `wanted`, because that sentence IS about the ask; only this one is about
    # the limit. One argument, and the two calls want different ones.
    stuck = _too_big_for(plan, ceiling if ceiling is not None else 0)

    blocked = settled(ceiling_target, GLOBAL_ALLOWANCE)
    if blocked is not None:
        if stuck:
            blocked = replace(blocked, detail=blocked.detail + stuck)
        plan.append(blocked)
        return plan

    # No region dimension, ever. The ceiling is genuinely global and attaching a
    # region built a request Google rejects — from a menu this command offered.
    # Name the gap rather than quietly leaving it. A card being REQUESTED that
    # needs more ceiling than we are asking for is not a reason to inflate the
    # ask — see `_ceiling_wanted` — but it IS something the person reading this
    # needs, because the second run is what closes it.
    later = _too_big_for(plan, wanted, raising=True)
    plan.append(QuotaAsk(
        "", GLOBAL_ALLOWANCE, REQUEST,
        f"will ask Google for {wanted} — it is "
        f"{ceiling if ceiling is not None else 0} today, which caps "
        f"every card at once, so a second box cannot start while the first runs"
        + (f". {wanted} rather than {CEILING_REQUEST} because a card this project "
           f"already holds comes as {wanted} GPUs in one machine"
           if wanted > CEILING_REQUEST else "")
        + later,
        ceiling_target, value=wanted))
    return plan


def _missing_cards(
    gc: Gcloud, project: str, region: str | None, quotas: list[dict] | None,
) -> str:
    """The cards this project is actually short of, for a "to ask later" hint.

    TAKES THE QUOTAS IT IS GIVEN. Re-reading them here cost **56 seconds** on
    `setup --no-quota-request` — the command went from ~50s to 1:46, doubling the
    slowest step of the first thing a new user runs, for a call the caller had
    already paid for. `compute_quotas` is a minute against this project; nothing
    in this file may fetch it twice.

    Falls back to the drivable list when there is nothing to plan from: naming
    every card the tool can order is at worst noisy, while naming two the user
    already holds is wrong.
    """
    from .create import drivable_cards

    if quotas is None:
        return ",".join(drivable_cards())
    try:
        plan = plan_quota(quotas, gc.quota_preferences(project), region=region)
    except GcloudError:
        return ",".join(drivable_cards())
    wanted = [ask.card for ask in plan if ask.submits and ask.card]
    # NOTHING MISSING IS NOT THE SAME AS EVERYTHING MISSING. Falling back to the
    # full card list when the plan is empty printed all five — on a project where
    # every one is granted or refused, so the advice was to file five requests
    # nobody needs.
    return ",".join(wanted) if wanted else ""


def _prefs(gc: Gcloud, project: str) -> list[dict]:
    """The preference list, or an empty one — never a reason to fail a summary."""
    try:
        return gc.quota_preferences(project)
    except GcloudError:
        return []


def _held_and_stuck(plan: list[QuotaAsk]) -> tuple[list[str], list[str]]:
    """(cards granted, cards at zero that cannot be asked for)."""
    held = [ask.label for ask in plan if ask.outcome == GRANTED and ask.card]
    stuck = [ask.label for ask in plan
             if ask.outcome in (DENIED, BLOCKED, UNAVAILABLE) and ask.card]
    return sorted(held), sorted(stuck)


def _nothing_to_ask(held: list[str], stuck: list[str]) -> str:
    """What is true when there is nothing left to request.

    Three different situations shared one sentence — "Nothing was missing anyway"
    — and only the first of them was that.
    """
    if stuck and held:
        return (f"{', '.join(held)} granted; {', '.join(stuck)} at zero and "
                f"already refused, so there is nothing left to ask for. "
                f"Run comfy-qat quota to see where each card stands.")
    if stuck:
        return (f"{', '.join(stuck)} at zero and already refused, so there is "
                f"nothing that can be asked for. Run comfy-qat quota to see "
                f"where each card stands.")
    if held:
        return (f"nothing was missing — {', '.join(held)} granted. Run "
                f"comfy-qat quota to see where each card stands.")
    return "run comfy-qat quota to see where each card stands."


def _unsold_here(gc, project: str, plan: list[QuotaAsk], region: str) -> list[str]:
    """Cards this plan would ASK for that `region` does not sell.

    Only the cards being asked for: a card already granted, refused or
    unavailable is not about to have an irrevocable request filed for it, and
    stopping the whole run over one of those would be refusing to do the job.

    Reads `auth._regions_stocking`, so `setup` and `quota list` cannot diverge
    about what a region offers — which is how this surface came to be the only
    one without the check at all.
    """
    from .auth import _regions_stocking

    asking = [ask.label for ask in plan if ask.submits and ask.card]
    if not asking:
        return []
    sells = _regions_stocking(gc, project, set(asking))
    # A FAILED LOOKUP IS NOT A FACT, for the seventh time in this feature: None
    # means the catalogue could not be read, and refusing on it would invent an
    # absence and block a legitimate run.
    return [name for name in sorted(asking)
            if sells.get(name) is not None and region not in sells[name]]


def _require_real_region(gc, project: str, quotas: list[dict] | None,
                         region: str | None) -> None:
    """Stop unless `region` is a region. Called before anything can return early.

    The same question `quota list` and `quota request` ask, through the same
    function, so the three cannot drift — and hoisted out of
    `ensure_quota_requests` because a flag that skipped the request step was
    skipping the validation with it.
    """
    if region is None or quotas is None:
        return
    from .auth import region_problem

    wrong = region_problem(gc, project, quotas, region)
    if wrong:
        raise SetupStopped(
            wrong[0],
            fix=("\n".join(f"comfy-qat setup {line}" for line in wrong[1])
                 or "comfy-qat quota list --by-region  # every region this "
                    "project meters"),
        )


def ensure_quota_requests(
    gc: Gcloud, p: Prompts, project: str, *, quotas: list[dict] | None,
    interactive: bool, region: str | None, submit: bool = True,
    justification: str | None = None, dry_run: bool = False,
    validate_only: bool = False,
) -> list[QuotaAsk]:
    """Leave this project with every drivable card granted or asked for.

    THE DEFAULT IS TO SUBMIT, and the reasoning is worth having in one place.

    The requirement is that a fresh install ends with the requests already in,
    without the user having had to learn that quota is a thing. A default of
    "print a command and wait" is the behaviour that requirement exists to
    replace — it is what `setup` did before, and it is why a tester can hold L4
    quota for a month and not know A100 was never asked for.

    Against that: a quota preference is permanent, and a person at Google reads
    it. So the default submits, and the submission is fenced four ways rather
    than gated behind a flag nobody sets:

      * the EXACT list, with the value, the scope and the justification, is
        printed BEFORE anything is sent — not a count, not a summary;
      * in a terminal it takes one confirmation, defaulting to yes, so the
        requirement is met by pressing return and nothing is sent by a person
        who was not looking;
      * `--dry-run` prints the plan and reaches nothing at all, and
        `--validate-only` asks GOOGLE whether each request is valid and creates
        nothing. The two used to be one flag whose meaning changed between
        commands — `quota request --dry-run` printed, `setup --dry-run` called
        Google — which is a trap for whoever learns the flag on one and carries
        it to the other;
      * `--no-quota-request` skips the step entirely.

    `--non-interactive` submits without the confirmation, because there is no
    terminal to confirm at and a flag a person typed is itself the consent. It
    still prints the plan and the outcome.

    NOTHING IT PRINTS PROMISES APPROVAL. Three of the five standing requests on
    the project this was built against were refused, so "submitted" has to read
    as "asked", and the sign-off says where to watch for the answer.
    """
    from .gcloud import quota_request_command
    from .quota import request_plan, request_value

    # FIRST STATEMENT, ABOVE EVERY RETURN IN THIS FUNCTION. `run_setup` also
    # calls it, and that is not enough: this function is reachable directly, and
    # a validation that only runs when you arrive through the front door is the
    # very shape this round is about. `--no-quota-request` returned below, having
    # already used the unvalidated region to report six granted cards as "a
    # project-wide allowance only".
    _require_real_region(gc, project, quotas, region)

    if not submit:
        # DERIVED, not a literal. This said `--gpu l4,t4` — both already granted
        # on the project it was read from, while the cards actually missing went
        # unnamed. Advice to file two irrevocable requests for quota the user
        # holds. Its sibling eleven lines down derived the list correctly all
        # along, so one of a pair was fixed and the other left.
        missing = _missing_cards(gc, project, region, quotas)
        # THE REGION THIS RUN WAS PLANNING FOR. Without it `quota request`
        # derives one of its own — us-central1 on this project, where Google has
        # already refused several cards — while the plan printed in the same run
        # named somewhere else. The other remedies in this feature failed or were
        # withheld; this one succeeds at filing the wrong thing, permanently.
        # AND ONLY IF THAT REGION CAN WORK. `quota request` refuses a region
        # selling no such card, so naming one here hands over a command that
        # exits 2 — the eleventh printed remedy that cannot run, and the twin of
        # the region check one function over: both sat below this early return.
        #
        # Not a reason to stop the run: with `--no-quota-request` nothing
        # irrevocable happens, and refusing the whole setup over it would be
        # refusing to do the job. The remedy stops naming a region that cannot
        # work, and says why.
        unsold = _unsold_here(gc, project, [
            QuotaAsk(card, card.upper(), REQUEST, "")
            for card in (missing or "").split(",") if card], region) if region else []
        where = f" --region {region}" if region and not unsold else ""
        no_good = (f" — note that {region} does not offer "
                   f"{', '.join(unsold)}, so ask somewhere else"
                   if unsold else "")
        # "NOTHING WAS MISSING" WAS A CLAIM ABOUT THE PROJECT, and `missing` is a
        # list of cards that could be ASKED FOR — a refused card does not submit,
        # so three cards at zero came out as nothing missing. Different facts, on
        # the command a new user runs first, in the one place this feature's whole
        # premise is stated back to them.
        held, stuck = _held_and_stuck(plan_quota(quotas, _prefs(gc, project),
                                                 region=region))
        p.say("quota requests skipped (--no-quota-request). "
              + (f"To ask later: comfy-qat quota request --gpu {missing}{where}"
                 f"{no_good}"
                 if missing else _nothing_to_ask(held, stuck)))
        return []
    if quotas is None:
        return []

    try:
        preferences = gc.quota_preferences(project)
    except GcloudError as exc:
        # Same rule as the quota read above, and for a sharper reason: without
        # the preference list there is no way to tell a pending request from a
        # missing one, NOR which preference id an existing pair already holds —
        # and minting a fresh id for a pair that has one is refused by Google
        # outright. Refuse to guess.
        p.say(f"could not read existing quota requests ({exc}), so nothing was "
              f"asked for — a second request cannot be ruled out without them")
        return []

    # F1 AND F2 — THE TWO CHECKS EVERY OTHER SURFACE ALREADY HAD. `quota list
    # --region` and `quota request --region` both refuse a region that does not
    # exist, and `quota request` also refuses one that sells no such card. This
    # command had neither, and it is the one a new user runs first, unattended,
    # that files requests automatically.
    #
    # `setup --region us-centrall` planned an irrevocable request for eight
    # H100s into a region that does not exist, and in the same breath reported
    # L4, T4, A100 and A100-80GB as cards "this project does not meter" — four
    # false sentences and a doomed request, from one typo, at exit 0.
    #
    # Seventeenth second-site, and the comment on the sibling guard already
    # called itself the thirteenth. Raised rather than printed, because `setup`
    # continues past a printed line and this must stop it.
    plan = plan_quota(quotas, preferences, region=region)
    where, why = request_region(quotas, preferences, region)

    # `where`, NOT `region`. The guard was gated on the flag the USER typed, and
    # with no `--region` the plan DERIVES one and pins every region-scoped target
    # to it — so the derived region went unchecked, and on a project whose
    # standing preferences name africa-south1 that is where three irrevocable
    # requests would be filed, into a region selling no NVIDIA card at all.
    #
    # `_refuse_if_unsold` refuses exactly that through `quota request`, and its
    # own docstring says a guard reachable from one entry point and not another
    # is a second-site by construction. This was that second entry point.
    if where:
        blind = _unsold_here(gc, project, plan, where)
        if blind:
            raise SetupStopped(
                f"{where} does not offer {', '.join(blind)} — a granted "
                f"request there buys a box that can never start, and a quota "
                f"preference cannot be withdrawn",
                fix=f"comfy-qat quota list --region {where}  # what this "
                    f"region actually offers",
            )
    asking = [ask for ask in plan if ask.submits]

    p.say(f"quota plan for {project} — region {where} ({why}) for cards metered "
          f"by family; per-card grants cover every region and name none:")
    for ask in plan:
        p.say(f"  {ask.label:<12} {ask.detail}")

    if not asking:
        p.say("nothing to request — every card this tool can drive is granted, "
              "already asked for, or not offered by this project")
        return plan

    p.say(f'justification sent with each: "{justification or DEFAULT_JUSTIFICATION}"')
    p.say(f"{len(asking)} request(s). Asking is not being granted — requests on "
          f"this kind of project are refused routinely — and a quota preference "
          f"cannot be deleted once made, only lowered.")

    if dry_run:
        # PRINTS AND REACHES NOTHING. The plan above is the whole output.
        p.say("dry run — nothing was sent and Google was not contacted. "
              "To have Google check each request without creating anything: "
              "comfy-qat setup --validate-only")
        for ask in asking:
            # THE CARD AND THE REGION, not the bare id. This printed
            # `GPUS-PER-GPU-FAMILY-per-project-region = 8` — an id that meters
            # five cards on this project, with nothing saying which — while
            # `quota request` refuses that same id from a user on the grounds
            # that "a raw id cannot say which you mean". The plan line two rows
            # above already says it properly; this is the line somebody copies.
            where = dict(ask.target.dimensions).get("region", "")
            p.say(f"would ask for {ask.label or ask.target.quota_id} = "
                  f"{ask.value}{f' in {where}' if where else ''}"
                  f" ({ask.target.quota_id})")
        return plan

    if not validate_only and interactive and not p.confirm("Send them now?"):
        # AND ITS SIBLING, for the same reason — `where` is the region the plan
        # above was built for, not whatever a later run would derive.
        p.say("nothing was sent. To ask later: comfy-qat quota request --gpu "
              + ",".join(ask.card for ask in asking if ask.card)
              + (f" --region {where}" if where else ""))
        return plan

    sent = 0
    for ask in asking:
        target = ask.target
        assert target is not None
        # Rule 1 and Rule 2 in one call: update an existing preference under ITS
        # id and ITS dimensions, or mint a stable id for a pair nothing holds.
        addressed = request_plan(target, preferences)
        # THROUGH THE SAME GUARD as `quota request`. `setup` never lowers today —
        # `settled` declines to re-ask for anything pending or answered, so the
        # path is not reached — but that is incidental, not a guarantee: the
        # ceiling number moves with the card table, and nothing stops a future
        # plan computing a smaller one. Routing through `request_value` makes the
        # protection real rather than a property of another function's behaviour.
        #
        # `quotas=` IS UNREACHABLE TODAY AND A MUTATION SWEEP SAYS SO: deleting it
        # kills no test, and that is recorded here rather than papered over with a
        # test that fakes reachability. Every ask site already requires held <
        # wanted before it appends anything — `settled` drops a card that holds a
        # grant, the ceiling is appended only when `ceiling < wanted`, and a CPU
        # ask is only made when short — so no floor can bite. It is passed because
        # the day one of those three conditions changes, the alternative is the
        # `--gpu t4 --value 0` defect again, one caller over.
        send, note = request_value(target, preferences, ask.value,
                                   quotas=quotas)
        if note:
            p.say(f"  {ask.label}: {note}")
        if send is None:
            # UNREACHABLE TODAY, and kept deliberately. A preference with no
            # readable `preferredValue` reads as pending, so `settled` declines
            # to re-ask and the card never arrives here — defence behind a
            # policy rather than behind a structure, which is the case where a
            # guard earns its keep rather than the case where it is dead weight.
            p.say(f"  {ask.label}: skipped — its standing value could not be read")
            continue
        args = quota_request_command(
            project=project, quota_id=target.quota_id, value=send,
            preference_id=addressed.preference_id,
            dimensions=addressed.dimensions,
            justification=justification or DEFAULT_JUSTIFICATION,
            email=gc.active_account(),
            allow_missing=addressed.allow_missing,
            validate_only=validate_only,
        )
        try:
            gc.run(args)
        except GcloudError as exc:
            if is_already_asked(exc):
                p.say(f"{ask.label}: already requested, nothing sent")
                continue
            p.say(f"{ask.label}: the request was refused: {exc}")
            continue
        sent += 1
        if validate_only:
            p.say(f"would ask for {target.quota_id} = {send} — Google says "
                  f"the request is valid. Nothing was created.")
            continue
        p.say(f"requested {target.quota_id} = {send}"
              + (f" as {addressed.preference_id}" if not addressed.updates_existing
                 else f" (updating {addressed.preference_id})"))

    if sent and not validate_only:
        p.say(f"asked, not granted — watch for the answer: "
              f"{console_quota_url(project)}")
    return plan


def add_discovered_hosts(
    gc: Gcloud, p: Prompts, project: str, path: Path,
) -> int:
    """Add any cloud box that is not in the host list yet. Returns how many.

    Google already knows the zone, machine type, card and operating system of
    every instance. Making someone copy that across by hand is how a host list
    ends up quietly wrong.
    """
    from .config import ConfigError, load
    from .discover import (
        clash_note, label_clashes, new_hosts, parse as parse_instance, to_toml,
    )
    from .host import STARTER
    from .hostfile import HostFileError, add

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
    clashes = label_clashes(found, existing)
    for box, label in clashes:
        p.say(clash_note(box, label))
    if not additions:
        if not clashes:
            p.say(f"{len(found)} cloud box(es), all already in your host list")
        return 0

    try:
        # Not `path.open("a")`. `setup` calls the same `new_hosts` `discover`
        # does and writes the same blocks, so it carried the same brick on a
        # first run — and this is the command someone runs before they have a
        # host list worth losing, which is exactly when they cannot tell a tool
        # that refused from a tool that broke. `hostfile.add` validates with the
        # real loader and keeps a verified copy before it writes.
        add(path, [to_toml(box, port) for box, port in additions], initial=STARTER)
    except HostFileError as exc:
        p.say(f"your host list could not be updated ({exc}), so nothing was added to it")
        return 0

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
    quota_requests: bool = True,
    justification: str | None = None,
    quota_dry_run: bool = False,
    quota_validate_only: bool = False,
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
    # Before billing and quota, for the same reason NumPy goes before the account
    # sequence: it depends on nothing but the config path, and a newcomer stopped
    # at billing should still end the run owning something. They did not — and
    # getting-started.md:64 tells them "`setup` has already written your host
    # list", which was false for exactly the person most likely to be reading it.
    path = ensure_host_list(p, config_path)
    ensure_billing(gc, p, chosen)
    quotas = ensure_gpu_quota(gc, p, chosen, region=region)
    # ABOVE EVERY EARLY RETURN, and above every consumer. This check lived inside
    # `ensure_quota_requests`, BELOW its `if not submit:` return — so
    # `--no-quota-request --region us-centrall` exited 0 having already used the
    # unvalidated region to report six granted cards as "a project-wide allowance
    # only". Third time an early return has skipped a check the ordinary path
    # runs.
    #
    # A guard whose reachability depends on which flags were passed is a guard
    # that will be missed again. What is validated must not depend on how the
    # command was invoked, so validation happens before anything can return.
    _require_real_region(gc, chosen, quotas, region)
    ensure_quota_requests(
        gc, p, chosen, quotas=quotas, interactive=interactive, region=region,
        submit=quota_requests, justification=justification,
        dry_run=quota_dry_run, validate_only=quota_validate_only,
    )
    add_discovered_hosts(gc, p, chosen, path)
    return path
