"""Making the box, so nobody has to make it in the console.

Every machine this tool operates was, until now, created by hand: pick a region,
pick a zone, work out that an L4 is a G2 and a T4 is an N1 with a card bolted on,
remember the Windows SSH metadata, remember that the NVIDIA driver is not in the
image, and find out afterwards whether the zone had one free. Six decisions, each
with a way of failing that only shows up after something is billing.

Four of them are decided here, from what Google already knows.

*The machine type comes from the card.* An L4 is the G2 family and the GPU is
part of the machine type — you do not attach it, and passing `--accelerator`
alongside one is refused. A T4, P4, P100, V100 or K80 is N1 plus
`--accelerator=type=...,count=1`. Getting this the wrong way round is the most
common way a create by hand fails, so the card is the only thing anyone types.

*The zone is chosen, not typed.* `zones.choose` ranks them — quota first,
availability second, measured latency third — and this tries them in that order,
falling through when Google says a zone has none free. `--zone` is still there
for someone deliberately testing one zone.

The fall-through stays inside the ordering it was given, which it did not always.
Google names a zone in its stockout refusal, that answer is fresher than anything
measured beforehand, and `build` used to take it wherever it pointed — including
out of `--zone`, whose whole meaning is "this zone or nothing", and out of the
regions the project holds quota in. A suggestion is followed only when
`Ordering.fall_through` is set and only into a region already on the list, and no
more than `zones.MAX_ATTEMPTS` creates are attempted however many are offered.
Each of those three is a way of ending up with a running, billing box somewhere
nobody chose.

*The quota is checked before anything exists.* Both the card's own grant and
`GPUS_ALL_REGIONS`, the project-wide ceiling across every card, which is 1 on
this project and is the limit that actually bites. Refusing costs nothing. A
quota refusal after the instance exists costs money and a cleanup.

*The driver is not in the base image.* A GPU box without it looks healthy, and
ComfyUI runs on its CPU — which this tool now detects, but only after the box has
been paid for. On Linux that is fixed at boot with Google's own startup script.
On Windows it is not, and this says so plainly rather than inventing a recipe:
see `WINDOWS_DRIVER` below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import inflight
from .config import Host
from .gcloud import Gcloud, GcloudError
from .lifecycle import LifecycleError, is_capacity_failure, suggested_zones
from .zones import MAX_ATTEMPTS, Ordering, region_of

# Kinds of refusal, so a caller can tell them apart without matching on prose.
NO_QUOTA = "no-quota"          # the project's grant will not allow this
NO_ZONE = "no-zone"            # nowhere offers this combination at all
EXHAUSTED = "exhausted"        # every zone tried was out of capacity
CREATE_FAILED = "create-failed"  # Google refused for some other reason

DEFAULT_DISK_GB = 200

# The Windows Server image will not fit below this, and a GPU box with no room
# for models is a box you pay to re-create. Ubuntu would take 10.
MIN_DISK_GB = 50

# And a ceiling, because `--disk` has no unit and a typo has no upper bound.
# pd-balanced is billed by the provisioned gigabyte from the moment the instance
# exists, whether or not anything is ever written to it, so `--disk 20000` for
# `2000` is one keystroke and eighteen terabytes nobody notices until the bill.
# 4 TB is far more than a box for reproducing a bug has ever needed; asking for
# more is a decision worth making in the console, deliberately.
MAX_DISK_GB = 4000

MAX_NAME_LEN = 63


@dataclass(frozen=True)
class Card:
    """One GPU, and the machine it has to be ordered as.

    `attached` is the distinction that matters. On G2 and A2 the accelerator is
    part of the machine type and `--accelerator` is refused; on N1 the machine
    type has no GPU and the card is attached to it. Nothing about the names
    tells you which, so it is written down.

    Three different names for one card, and they are not interchangeable:

    * `key` is what a person types after `--gpu`, and the only one a fix line
      may ever suggest. `H100-80GB` is the card's name and `--gpu h100-80gb` is
      not a command — the tool refuses it.
    * `name` is what `host list` shows, and it is *not* free: `discover`
      derives it from the instance's accelerator type, so it has to be exactly
      what `discover.accelerator` would read back off a box holding this card.
      Otherwise `host discover` finds a box this tool created and adds it a
      second time.
    * `quota_aliases` are the other spellings Google meters the same card
      under. Empty for almost every card, and the reason it exists is H100:
      the accelerator is `nvidia-h100-80gb` and the quota is
      `NVIDIA-H100-GPUS`, with no 80GB anywhere in it.
    """

    key: str             # what a person types after --gpu
    name: str            # what `host list` shows, and what discovery reads back
    accelerator: str     # Google's own accelerator-type id
    machine_type: str
    attached: bool       # is the card part of the machine type?
    count: int = 1
    quota_aliases: tuple[str, ...] = ()

    @property
    def accelerator_flag(self) -> str | None:
        """The `--accelerator` value, or None when the machine type carries it."""
        if self.attached:
            return None
        return f"type={self.accelerator},count={self.count}"

    @property
    def quota_names(self) -> tuple[str, ...]:
        """Every friendly quota name this card could be metered under."""
        return (self.name, *self.quota_aliases)


# The cards this tool can order, and the family each one has to be ordered in.
# Sizes are the smallest that fits a GPU: this is a box for reproducing a bug,
# not for training, and the card is what costs.
CARDS: dict[str, Card] = {
    "l4": Card("l4", "L4", "nvidia-l4", "g2-standard-8", attached=True),
    "t4": Card("t4", "T4", "nvidia-tesla-t4", "n1-standard-8", attached=False),
    "p4": Card("p4", "P4", "nvidia-tesla-p4", "n1-standard-8", attached=False),
    "p100": Card("p100", "P100", "nvidia-tesla-p100", "n1-standard-8", attached=False),
    "v100": Card("v100", "V100", "nvidia-tesla-v100", "n1-standard-8", attached=False),
    # Retired by Google in most regions. Left in because asking for it should
    # fail with "no zone offers this", which is the truth, rather than with
    # "unknown card", which is not.
    "k80": Card("k80", "K80", "nvidia-tesla-k80", "n1-standard-8", attached=False),
    "a100": Card("a100", "A100", "nvidia-tesla-a100", "a2-highgpu-1g", attached=True),
    "a100-80gb": Card("a100-80gb", "A100-80GB", "nvidia-a100-80gb", "a2-ultragpu-1g",
                      attached=True),
    # Two things about the H100 that nothing in the name tells you. Both were
    # read off a live project on 2026-08-28.
    #
    # The smallest H100 machine type is eight cards, so this needs eight of the
    # project's GPU allowance, not one. Counting it as one would pass the quota
    # gate and fail at the create.
    #
    # And the card Google sells as `nvidia-h100-80gb` is metered as
    # `NVIDIA-H100-GPUS`, with no 80GB in it. `accelerator-types list` offers
    # `nvidia-h100-80gb` and no `nvidia-h100`; the quota list has
    # `PREEMPTIBLE-NVIDIA-H100-GPUS` and `COMMITTED-NVIDIA-H100-GPUS` and no
    # `-80GB-` H100 row at all. Looking the grant up under the card's own name
    # reports "no H100-80GB quota" on a project that holds one. A100 carries both
    # spellings, which is why this is a per-card alias rather than a rule.
    "h100": Card("h100", "H100-80GB", "nvidia-h100-80gb", "a3-highgpu-8g",
                 attached=True, count=8, quota_aliases=("H100",)),
}


@dataclass(frozen=True)
class Image:
    """A public image family, and the name `host discover` will read back off it.

    `os` matches what `discover.operating_system` derives from the boot disk's
    licence, so a box created here and a box found by discovery describe
    themselves identically. They stopped agreeing once, and `host switch
    windows` then matched one of them and not the other.
    """

    key: str
    os: str
    family: str
    project: str
    windows: bool


IMAGES: dict[str, Image] = {
    "linux": Image("linux", "Ubuntu 22.04", "ubuntu-2204-lts", "ubuntu-os-cloud", False),
    "windows": Image("windows", "Windows Server 2022", "windows-2022", "windows-cloud", True),
}

# Spellings that mean one of the two above. `ubuntu` is what people type; `win`
# is what people type when they are in a hurry.
ALIASES = {
    "ubuntu": "linux", "debian": "linux", "ubuntu-22.04": "linux", "ubuntu2204": "linux",
    "win": "windows", "windows-server": "windows", "windows-2022": "windows",
}


# --- the NVIDIA driver -----------------------------------------------------
#
# Google's word on this, read on 2026-08-28:
#
#   Install GPU drivers — https://cloud.google.com/compute/docs/gpus/install-drivers-gpu
#   "The recommended way to install NVIDIA GPU drivers and CUDA Toolkit for
#   Google Cloud Compute Engine instances is through the cuda_installer tool."
#
# and, for doing it without a person present, that page's "Automating the
# installation process" points at the repository it ships from:
#
#   https://github.com/GoogleCloudPlatform/compute-gpu-installation
#   linux/README.md: "You can automate the installation of the driver and/or
#   CUDA Toolkit by using a startup script for your Compute Engine instance.
#   See startup_script.sh for an example startup script."
#
# LINUX_DRIVER below is that file, copied byte for byte from
# https://raw.githubusercontent.com/GoogleCloudPlatform/compute-gpu-installation/main/linux/startup_script.sh
# and not edited. Confidence: high — it is Google's own script, fetched from the
# repository their documentation sends you to, and the two guards at the top make
# it safe to run on every boot.
#
# Two things it does not promise, and neither should this file. The install
# reboots the box at least once and continues on the next boot, so a fresh box is
# not ready the moment `create` returns — `host go` waits for SSH and then for
# ComfyUI, which is the wait that covers it. And Google states the script does
# not work on instances with Secure Boot enabled; nothing here turns Secure Boot
# on, and the images used are not Shielded-by-default in a way that would.
LINUX_DRIVER = """\
#!/bin/bash
if test -f /opt/google/cuda-installer
then
  exit
fi

mkdir -p /opt/google/cuda-installer
cd /opt/google/cuda-installer/ || exit

if test -f cuda_installation
then
  exit
fi

curl -fSsL -O https://storage.googleapis.com/compute-gpu-installation-us/installer/latest/cuda_installer.pyz
python3 cuda_installer.pyz install_driver
"""

# WINDOWS IS A SEAM, ON PURPOSE. Google documents exactly one way to install the
# driver on Windows Server, and it is a person at a PowerShell prompt:
#
#   https://cloud.google.com/compute/docs/gpus/install-drivers-gpu (Windows tab)
#   "Open a PowerShell terminal as an administrator, then complete the following
#    steps: Download the script.
#      Invoke-WebRequest https://github.com/GoogleCloudPlatform/compute-gpu-installation/raw/main/windows/install_gpu_driver.ps1 -OutFile C:\\install_gpu_driver.ps1
#    Run the script.
#      C:\\install_gpu_driver.ps1"
#
# There is no documented metadata key that does this at first boot. Compute
# Engine does have `windows-startup-script-url`, and running that script through
# it would probably work — it runs as SYSTEM, which is an administrator — but
# "probably" is how a box gets created, billed, and found on the CPU an hour
# later. Google does not document that combination, the script reboots partway
# through, and a startup script runs again on every boot. So this tool does not
# guess: it creates the box and hands over the two commands Google publishes,
# and they get run once, by a person, over the tunnel this tool already opens.
#
# Confidence: high on the commands (they are quoted from Google's page); low on
# any automatic route, which is why there is not one. Verify on a real box.
WINDOWS_DRIVER = (
    "Invoke-WebRequest https://github.com/GoogleCloudPlatform/compute-gpu-installation"
    "/raw/main/windows/install_gpu_driver.ps1 -OutFile C:\\install_gpu_driver.ps1\n"
    "C:\\install_gpu_driver.ps1"
)


@dataclass(frozen=True)
class Blueprint:
    """Everything about the box except where it goes. Pure data, so it prints."""

    name: str
    image: Image
    card: Card
    disk_gb: int = DEFAULT_DISK_GB

    @property
    def machine_type(self) -> str:
        return self.card.machine_type

    @property
    def metadata(self) -> str | None:
        """The `--metadata` value for this box.

        Windows gets `enable-windows-ssh=TRUE`, without which nothing in this
        tool can reach the machine: every command it runs goes over
        `gcloud compute ssh`, and on Windows that key is what puts an SSH server
        there at all.

        Linux gets Google's driver installer as a startup script. gcloud splits
        `--metadata` on commas, so a value containing one would be read as two
        keys and rejected — the script has none, and `test_create.py` holds it
        to that rather than trusting it.
        """
        if self.image.windows:
            return "enable-windows-ssh=TRUE"
        return f"startup-script={LINUX_DRIVER}"

    def steps(self, zone: str) -> list[str]:
        """The plan, in the words `--dry-run` prints and the real run follows."""
        card = f"{self.card.name} ({self.card.accelerator})"
        how = ("built into the machine type" if self.card.attached
               else f"attached with --accelerator={self.card.accelerator_flag}")
        lines = [
            f"create {self.name} in {zone}: {self.image.os}, {card}",
            f"machine type {self.machine_type} — {how}",
            f"{self.disk_gb} GB pd-balanced boot disk from {self.image.family}",
        ]
        if self.image.windows:
            lines.append("metadata enable-windows-ssh=TRUE, so this tool can reach it")
            lines.append("the NVIDIA driver is NOT installed — one command, printed after")
        else:
            lines.append("startup script installs the NVIDIA driver on first boot")
        lines.append(f"add {self.name} to the host list on the next free port")
        return lines


# --- turning what somebody typed into a blueprint --------------------------


def card_for(gpu: str) -> Card:
    """`l4` -> the L4 card. Anything else names what there is."""
    key = (gpu or "").strip().lower().replace("_", "-").replace(" ", "")
    key = key.removeprefix("nvidia-").removeprefix("tesla-")
    if key in CARDS:
        return CARDS[key]
    raise LifecycleError(
        f"no card called {gpu!r}. This tool can create: {', '.join(sorted(CARDS))}.",
        fix="comfy-qat quota list — the cards this project is allowed",
        kind=NO_QUOTA,
    )


def image_for(os_choice: str) -> Image:
    """`linux` or `windows`, plus the spellings people actually type."""
    key = (os_choice or "").strip().lower()
    key = ALIASES.get(key, key)
    if key in IMAGES:
        return IMAGES[key]
    raise LifecycleError(
        f"no operating system called {os_choice!r}. Say --os linux or --os windows.",
        fix="comfy-qat create --os linux --gpu l4",
        kind=NO_ZONE,
    )


def _clean(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in name.lower())


# What Compute Engine will accept as an instance name, from its own validation:
# a lowercase letter, then up to MAX_NAME_LEN - 1 more of letters, digits and
# hyphens, not ending in one. `_clean` turns anything into hyphens and lowercase,
# which is enough for `My Box` and not enough for `9lives` or `box_`. Checking
# here rather than letting the create fail costs nothing; letting it fail costs
# the quota read, the zone ranking, four TCP probes, a confirmation prompt and a
# minute of gcloud, and answers with Google's wording about a regular expression
# rather than with what to type instead. `plan` claims to be "offline, and
# total", and a name Google cannot use is something it knows offline.
_GCE_NAME = re.compile(rf"^[a-z]([-a-z0-9]{{0,{MAX_NAME_LEN - 2}}}[a-z0-9])?$")


def choose_name(preferred: str | None, image: Image, taken: set[str]) -> str:
    """A name nothing else is using, here or on the project.

    A default rather than a prompt, because the interesting decision is the card
    and the boring one should not interrupt it. `comfy-linux`, then
    `comfy-linux-2`: two boxes that differ only in zone and share a name is how
    you read a result off the wrong one.
    """
    if preferred:
        base = _clean(preferred)
        if not _GCE_NAME.match(base):
            # `_clean` keeps anything `str.isalnum()` calls alphanumeric, and that
            # includes é and ボ. Google's rule is narrower than Python's, so the
            # cleaned form is what gets checked, not what was typed.
            raise LifecycleError(
                f"{preferred!r} is not a name Compute Engine will take. A name "
                f"starts with a letter, then letters, digits or hyphens, up to "
                f"{MAX_NAME_LEN} characters, and does not end in a hyphen. Nothing "
                f"was created.",
                fix="drop --name and one is picked for you: comfy-linux or "
                    "comfy-win, numbered if it is taken",
                kind=CREATE_FAILED,
            )
        if base in taken:
            raise LifecycleError(
                f"{preferred} is already taken — a host list entry or an instance on "
                f"this project has that name. Pick another with --name.",
                fix="comfy-qat list",
                kind=CREATE_FAILED,
            )
        return base

    base = "comfy-win" if image.windows else "comfy-linux"
    if base not in taken:
        return base
    for suffix in range(2, 100):
        candidate = f"{base}-{suffix}"
        if candidate not in taken:
            return candidate
    raise LifecycleError(
        f"could not find an unused name starting {base}. Give one with --name.",
        fix="comfy-qat list",
        kind=CREATE_FAILED,
    )


def plan(
    *, os_choice: str, gpu: str, name: str | None = None,
    disk_gb: int = DEFAULT_DISK_GB, taken: set[str] | None = None,
) -> Blueprint:
    """Everything decided before anything is contacted. Offline, and total."""
    image = image_for(os_choice)
    card = card_for(gpu)
    if disk_gb < MIN_DISK_GB:
        raise LifecycleError(
            f"a {disk_gb} GB disk is too small — the image will not fit and models "
            f"will not either. Ask for at least {MIN_DISK_GB}.",
            fix=f"comfy-qat create --os {image.key} --gpu {gpu} --disk {DEFAULT_DISK_GB}",
            kind=CREATE_FAILED,
        )
    if disk_gb > MAX_DISK_GB:
        raise LifecycleError(
            f"a {disk_gb} GB disk is larger than anything this tool creates. The disk "
            f"bills by the gigabyte provisioned, from the moment the box exists and "
            f"whether or not anything is written to it, so a typo here is expensive "
            f"and silent. Ask for at most {MAX_DISK_GB}, or make a disk that size "
            f"deliberately in the console.",
            fix=f"comfy-qat create --os {image.key} --gpu {gpu} --disk {DEFAULT_DISK_GB}",
            kind=CREATE_FAILED,
        )
    return Blueprint(
        name=choose_name(name, image, taken or set()),
        image=image, card=card, disk_gb=disk_gb,
    )


# --- will the project allow it -------------------------------------------


@dataclass(frozen=True)
class QuotaCheck:
    """What the project's allowance says, before anything has been created."""

    card: str
    card_limit: int | None
    global_limit: int | None
    needed: int
    running: tuple[str, ...]
    regions: tuple[str, ...]
    # What a person types after `--gpu` to mean this card. Not the same string as
    # `card` for the H100, and a fix line that says `--gpu h100-80gb` names a card
    # this tool refuses.
    key: str = ""
    # Cards held by those running boxes, which is not len(running). One running
    # `a3-highgpu-8g` is one instance and eight of the ceiling, and counting boxes
    # lets a create through the gate that Google then refuses.
    held: int = 0

    def lines(self) -> list[str]:
        """What was checked and what it said — printed by a dry run and a real one."""
        def amount(value: int | None) -> str:
            if value is None:
                return "not granted"
            return "unlimited" if value < 0 else str(value)

        def ceiling(value: int | None) -> str:
            # None here is not the same None as the card's. A card the project
            # holds nothing for reports no record, and that is a refusal. The
            # project-wide ceiling always exists, so no record means it was not
            # read — which does not gate anything, and must not read as "zero".
            if value is None:
                return "not reported by this project"
            return "unlimited" if value < 0 else str(value)

        out = [
            f"{self.card}: {amount(self.card_limit)}"
            + (f", in {len(self.regions)} region(s)" if self.regions else ""),
            f"GPUS_ALL_REGIONS (every card, project-wide): {ceiling(self.global_limit)}",
        ]
        if self.running:
            cards = "1 card" if self.held == 1 else f"{self.held} cards"
            out.append(f"already running and holding {cards} of it: "
                       f"{', '.join(self.running)}")
        return out

    @property
    def typed(self) -> str:
        """The `--gpu` spelling to put in a fix line, never the display name."""
        return self.key or self.card.lower()

    def problem(self) -> LifecycleError | None:
        """The reason this cannot be created, or None. Nothing has happened yet.

        Order matters here. A quota payload can carry a LIMIT and no LOCATIONS —
        the live API leaves the per-entry `dimensions` null and puts the places
        in `applicableLocations`, which quota.py's own docstring says — and a
        limit of zero is indistinguishable from an unparsed one at this point.
        So the missing-region case is asked FIRST, because it names what is
        actually absent.

        It was asked last, so such a payload produced "this project has no L4
        quota" — a wrong cause, sending someone to request quota they already
        hold. It misled the agent that owns argument surfaces twice in one
        session, which is the argument for the reorder rather than a reword.
        """
        if not self.regions and self.card_limit:
            return LifecycleError(
                f"this project's {self.card} grant names no region, so there is "
                f"nowhere to put the box. The grant itself is "
                f"{self.card_limit}. Nothing was created.",
                fix=f"comfy-qat quota request --gpu {self.typed} "
                    f"--region us-central1",
                kind=NO_QUOTA,
            )
        if not self.card_limit:
            return LifecycleError(
                f"this project has no {self.card} quota, so a {self.card} box cannot "
                f"start anywhere. Nothing was created.",
                fix=f"comfy-qat quota request --gpu {self.typed} "
                    f"--region us-central1, then wait for Google",
                kind=NO_QUOTA,
            )
        if self.card_limit > 0 and self.card_limit < self.needed:
            return LifecycleError(
                f"{self.card} needs {self.needed} of this project's GPU allowance and "
                f"the grant is {self.card_limit}. Nothing was created.",
                fix=f"comfy-qat quota request --gpu {self.typed} "
                    f"--region us-central1, then wait for Google",
                kind=NO_QUOTA,
            )
        if self.global_limit is not None and 0 <= self.global_limit < self.needed:
            return LifecycleError(
                f"GPUS_ALL_REGIONS is {self.global_limit} on this project — that is the "
                f"ceiling across every card, whatever the {self.card} grant says, and "
                f"{self.needed} is needed. Nothing was created.",
                fix="comfy-qat quota request --gpu l4 --region us-central1 asks for "
                    "a card; raising the project-wide ceiling is a separate request at "
                    "https://console.cloud.google.com/iam-admin/quotas",
                kind=NO_QUOTA,
            )
        if self.running and self.global_limit is not None and 0 <= self.global_limit < (
            self.needed + self.held
        ):
            if len(self.running) == 1:
                return LifecycleError(
                    f"GPUS_ALL_REGIONS is {self.global_limit} and {self.running[0]} is "
                    f"already running on it, so a new GPU box cannot start until that "
                    f"one stops. Nothing was created.",
                    fix=f"comfy-qat down {self.running[0]} — stop the one you are "
                        f"not using, then run this again",
                    kind=NO_QUOTA,
                )
            return LifecycleError(
                f"GPUS_ALL_REGIONS is {self.global_limit} and {len(self.running)} GPU "
                f"boxes are already running on it, holding {self.held} of it between "
                f"them: {', '.join(self.running)}. Nothing was created.",
                fix=f"comfy-qat down {self.running[0]} — stop the ones you are not "
                    f"using, then run this again",
                kind=NO_QUOTA,
            )
        return None


def _gpu_boxes_running(instances: list[dict]) -> list[str]:
    """Instances holding a card, so they are spending the ceiling.

    Not "RUNNING". Google counts an accelerator against quota from the moment it
    is allocated, not from the moment the box finishes booting — so a GPU box in
    STAGING or PROVISIONING holds the ceiling and is billing, and skipping it let
    `create` proceed against a project whose single slot was already taken. That
    is a start against a full ceiling, which is the one thing this gate exists to
    prevent, and the window is the first 30-60 seconds of every box's life.

    Only TERMINATED is certainly spending nothing.
    """
    found = []
    for instance in instances or []:
        if instance.get("status") == "TERMINATED":
            continue
        if not instance.get("guestAccelerators"):
            # A G2 or A2 reports its built-in card here too, so this is not just
            # the N1 case. A box with no accelerator at all does not count
            # against the GPU ceiling.
            continue
        name = instance.get("name")
        if name:
            found.append(name)
    return found


def _cards_running(instances: list[dict]) -> int:
    """How much of the ceiling running boxes hold. Cards, not boxes.

    GPUS_ALL_REGIONS is metered in cards, and an a3-highgpu-8g holds eight of
    them. Counting boxes says one, which passes the gate on a ceiling of 8 and
    is then refused by Google — the refusal this whole module exists to make
    before anything bills, after the zone probing and after somebody has said
    yes to it.

    `acceleratorCount` is in the live payload for a built-in G2 card as well as
    an attached N1 one. A row that reports no count, or a count that reads as
    zero or less, is counted as one rather than dropped: an unfamiliar payload
    shape is not evidence of an empty machine, a card that is there is spending,
    and guessing low here is the direction that costs money.
    """
    held = 0
    for instance in instances or []:
        # See _gpu_boxes_running: a card is spending from allocation, not from
        # RUNNING. This function already refuses to guess low on an unparseable
        # accelerator count, for the reason in its docstring — and then dropped
        # the whole box.
        if instance.get("status") == "TERMINATED":
            continue
        for accel in instance.get("guestAccelerators") or []:
            try:
                held += max(1, int(accel.get("acceleratorCount")))
            except (TypeError, ValueError):
                held += 1
    return held


def card_grant(card: Card, quotas: list[dict]) -> tuple[int | None, list[str]]:
    """This card's allowance and the regions it covers, under every spelling.

    One card can be metered under more than one friendly name — the H100 is sold
    as `nvidia-h100-80gb` and metered as `NVIDIA-H100-GPUS` — so the grant is the
    best of them and the regions are the union. Best rather than first: a project
    that carries both spellings would otherwise be read off whichever one came
    back with a zero.
    """
    from .quota import UNLIMITED, allowance, regions_with_quota

    limits = [allowance(name, quotas) for name in card.quota_names]
    granted = [value for value in limits if value is not None]
    if not granted:
        limit: int | None = None
    elif UNLIMITED in granted:
        limit = UNLIMITED
    else:
        limit = max(granted)

    regions: set[str] = set()
    for name in card.quota_names:
        regions |= set(regions_with_quota(name, quotas))
    return limit, sorted(regions)


def check_quota(card: Card, quotas: list[dict], instances: list[dict]) -> QuotaCheck:
    """Read the allowance. Pure — the caller does the two gcloud reads."""
    from .quota import global_allowance

    limit, regions = card_grant(card, quotas)
    return QuotaCheck(
        card=card.name,
        card_limit=limit,
        global_limit=global_allowance(quotas),
        needed=card.count,
        running=tuple(_gpu_boxes_running(instances)),
        regions=tuple(regions),
        key=card.key,
        held=_cards_running(instances),
    )


# --- the part that spends money -------------------------------------------


def create_in(gc: Gcloud, blueprint: Blueprint, zone: str, project: str) -> None:
    """One attempt, in one zone. Raises GcloudError exactly as gcloud refused it."""
    gc.create_instance_from_image(
        blueprint.name, zone, project,
        machine_type=blueprint.machine_type,
        image_family=blueprint.image.family,
        image_project=blueprint.image.project,
        disk_gb=blueprint.disk_gb,
        accelerator=blueprint.card.accelerator_flag,
        metadata=blueprint.metadata,
    )


def build(
    gc: Gcloud, blueprint: Blueprint, ordering: Ordering, project: str, say,
    *, limit: int = MAX_ATTEMPTS,
) -> str:
    """Try the zones in order until one has room. Returns the zone that worked.

    Capacity is the one thing that cannot be checked in advance — Google
    publishes no endpoint for it — so this is try-and-see, and every attempt is
    announced before it is made. A silent thirty-second pause reads as a hang,
    and the pause is the normal case: a stockout refusal is not fast.

    A zone Google itself names in the refusal is moved to the front of what is
    left, because that answer is fresher than anything measured beforehand. It is
    *not* a licence to leave the ordering, and it used to be. Three limits, each
    of which was once absent and each of which spends money when it is:

    **`ordering.fall_through`.** `--zone` means this zone or nothing, and
    `order_zones` says exactly that in its note. A stockout in that zone naming
    another one used to create the box in the other one — billing, and in the one
    place the caller had ruled out.

    **`ordering.regions`.** A suggestion outside the regions that were ranked is
    outside `--region` when one was given, and outside the project's quota when
    one was not. Following it creates a box somewhere nobody chose, or burns a
    minute on a create that cannot succeed.

    **`limit`.** The same cap `zones.choose` applies to the ranked list, applied
    again here because the suggestions are not on that list. `choose` hands over
    six zones and every refusal can name a fresh one, so the queue refills as
    fast as it drains: an uncapped fall-through has no end and is a command that
    looks hung. Six attempts is `zones.MAX_ATTEMPTS`, which documented this cap
    long before anything enforced it.
    """
    allowed = set(ordering.regions)
    queue = [zone.lower() for zone in ordering.zones]
    tried: list[str] = []
    capped = False
    while queue:
        if len(tried) >= limit:
            capped = True
            break
        zone = queue.pop(0)
        if zone in tried:
            continue
        tried.append(zone)
        say(f"trying {zone}…")
        try:
            # Registered around the one call that can bring a billing GPU box
            # into existence, and registered HERE rather than in `create_cmd`
            # because this is the only frame that knows which zone the attempt
            # is in. `create_cmd` knows `ordering.zones[0]`, which is the right
            # answer only until the first stockout pushes the create down the
            # list — and a stockout-heavy day is exactly when this loop is long
            # enough to be interrupted.
            #
            # The wording is the OSError branch's, twenty lines below the caller,
            # which has said the right thing about this exact state since before
            # anything could reach it: the box is not in the host list, so
            # `comfy-qat down` cannot reach it and the raw gcloud stop is the
            # only thing that works. Stopping the bill comes first and adoption
            # second, in that order, for the same reason it does there.
            with inflight.may_leave(
                f"the instance {blueprint.name} in {zone}",
                undo=[
                    "stop it now:",
                    f"gcloud compute instances stop {blueprint.name} "
                    f"--zone={zone} --project={project}",
                    "or check first, if you would rather look:",
                    f"gcloud compute instances list --project={project}",
                ],
                note="it is in no host list, so `comfy-qat down` cannot reach it "
                     "— `comfy-qat discover` adopts it if you want to keep it",
            ):
                create_in(gc, blueprint, zone, project)
        except GcloudError as exc:
            if not is_capacity_failure(exc.raw):
                raise LifecycleError(
                    f"Google refused to create {blueprint.name} in {zone}: {exc}",
                    fix=(exc.fix or
                         f"check the console for a half-made {blueprint.name} before "
                         f"trying again: gcloud compute instances list "
                         f"--project={project}"),
                    kind=CREATE_FAILED,
                ) from exc
            say(f"  {zone} has no {blueprint.card.name} free right now")
            if ordering.fall_through:
                for named in suggested_zones(exc.raw):
                    # Lowered because `suggested_zones` hands the zone back in the
                    # case Google wrote it, and `US-CENTRAL1-C` is neither a zone
                    # gcloud accepts nor a string `tried` recognises.
                    suggested = named.lower()
                    if suggested in tried or suggested in queue:
                        continue
                    # No `allowed and ...` escape hatch. An ordering that names no
                    # region is one that cannot say where the box may go, and
                    # "cannot say" is not permission.
                    if region_of(suggested) not in allowed:
                        say(f"  Google suggests {suggested}, which is outside the "
                            f"regions this is allowed to use — not trying it")
                        continue
                    say(f"  Google suggests {suggested}")
                    queue.insert(0, suggested)
            continue
        return zone

    if capped:
        say(f"stopping after {len(tried)} zones — each attempt takes about a minute")
        raise LifecycleError(
            f"stopped after {limit} zones, all out of {blueprint.card.name} capacity: "
            f"{', '.join(tried)}. Nothing was created and nothing is billing — this is "
            f"a cap, not the whole world, so there may be room somewhere untried.",
            fix=("wait and run the same command again, or name a zone yourself: "
                 "comfy-qat create --zone <zone>"),
            kind=EXHAUSTED,
        )

    raise LifecycleError(
        f"every zone tried is out of {blueprint.card.name} capacity: "
        f"{', '.join(tried) or 'none were offered'}. Nothing was created and nothing "
        f"is billing.",
        fix=("wait and run the same command again — a stockout is usually minutes to "
             "hours — or ask for a different card: comfy-qat quota list"),
        kind=EXHAUSTED,
    )


def host_entry(blueprint: Blueprint, zone: str, project: str):
    """The discovered-box record `discover.to_toml` turns into a host list entry.

    Written through `discover` rather than by hand so a created box and a
    discovered one are the same shape — otherwise `host discover` finds this
    instance again and adds it a second time under a different port.
    """
    from .discover import Discovered

    return Discovered(
        name=blueprint.name, os=blueprint.image.os, gpu=blueprint.card.name,
        gce_instance=blueprint.name, gce_zone=zone, gce_project=project,
        running=True,
    )


def taken_names(hosts: list[Host], instances: list[dict]) -> set[str]:
    """Every name already in use, in the host list or on the project."""
    names = {host.name.lower() for host in hosts}
    names |= {host.gce_instance.lower() for host in hosts if host.gce_instance}
    names |= {(instance.get("name") or "").lower() for instance in instances or []}
    return {name for name in names if name}


def next_steps(blueprint: Blueprint, zone: str) -> list[str]:
    """What to do with the box, and how to stop paying for it.

    Both, always. It is running from the moment it is created, and a message that
    says how to use a GPU box without saying how to stop it is how one bills all
    night.
    """
    lines = []
    if blueprint.image.windows:
        lines.append(
            f"{blueprint.name} has no NVIDIA driver yet, and ComfyUI will run on its "
            f"CPU until it has one. Google documents one way to install it on Windows "
            f"and it is a person at a PowerShell prompt — open one on the box as "
            f"Administrator and run:")
        lines.extend(f"    {line}" for line in WINDOWS_DRIVER.splitlines())
    else:
        lines.append(
            f"{blueprint.name} is installing the NVIDIA driver from its startup "
            f"script, which reboots it once or twice. `host go` waits that out.")
    lines.append(f"  comfy-qat go {blueprint.name}     # install ComfyUI and serve it")
    lines.append(f"  comfy-qat down {blueprint.name}   # stop the machine, stop paying")
    return lines


def summary(blueprint: Blueprint, ordering: Ordering) -> list[str]:
    """The zone order, said the way it was decided.

    Two ways, and saying the wrong one is worse than saying nothing. With
    `--zone` there is one zone, nothing was ranked and nothing was measured;
    describing that as "quota first, then what is offered, then measured latency
    (nearest: us-central1)" claims three decisions that never happened and names
    a nearest region out of a set of one.
    """
    if not ordering.zones:
        return []
    if not ordering.latency:
        head = (f"zone order — {len(ordering.zones)} to try, and it is the one you "
                f"named with --zone: nothing was ranked or measured")
    else:
        first = region_of(ordering.zones[0])
        head = (f"zone order — {len(ordering.zones)} to try, quota first, then what is "
                f"offered, then measured latency (nearest: {first})")
    return [head] + [f"  {index}. {line}" for index, line
                     in enumerate(ordering.lines(), start=1)]


def order_zones(
    gc: Gcloud, project: str, blueprint: Blueprint, check: QuotaCheck, *,
    zone: str | None = None, region: str | None = None, config=None, probe=None,
) -> Ordering:
    """The zones to try, honouring an override. Read-only; nothing is created.

    `--zone` is one zone and no fall-through: it exists for someone deliberately
    testing that zone, and quietly moving them to another one would be the
    opposite of what they asked for. Both halves are still checked against what
    Google offers there — the machine type *and* the card. Checking only the
    machine type reads as a check and is not one for five of the nine cards: a
    T4, P4, P100, V100 or K80 is an `n1-standard-8`, which almost every zone on
    Earth offers, so the card is the half that actually varies and was the half
    not being looked at.

    `--region` narrows without naming a zone, which is the ordinary way to say
    "somewhere in Europe" — the zone inside it is still chosen and still falls
    through.
    """
    from .zones import choose, zones_offering, zones_with_machine_type

    # Google's zone and region names are lowercase, and so is everything read back
    # from it. `--zone US-CENTRAL1-A` is the same request; matching it against the
    # quota regions without lowering it refuses with a message about a region that
    # does not exist.
    zone = zone.strip().lower() if zone else zone
    region = region.strip().lower() if region else region

    if zone:
        # The same gate `--region` gets. `--region me-west1` is refused here with
        # the reason; `--zone me-west1-a` used to sail past and find out from
        # gcloud instead, a minute and a confirmation prompt later. Skipped when
        # the grant names no region at all, which `problem()` refuses on its own.
        if check.regions and region_of(zone) not in set(check.regions):
            raise LifecycleError(
                f"this project has no {blueprint.card.name} quota in "
                f"{region_of(zone)}, so nothing can start in {zone}. Nothing was "
                f"created.",
                fix=(f"comfy-qat quota request --gpu "
                     f"{blueprint.card.key} --region {region_of(zone)}, or "
                     f"drop --zone and let this pick"),
                kind=NO_QUOTA,
            )
        offered = zones_with_machine_type(
            gc.machine_types(project, [zone], blueprint.machine_type),
            blueprint.machine_type,
        )
        if not offered:
            raise LifecycleError(
                f"{zone} does not offer {blueprint.machine_type}, so a "
                f"{blueprint.card.name} box cannot be created there at all. Nothing "
                f"was created.",
                fix=(f"drop --zone and let this pick one, or pick a zone that has it: "
                     f"gcloud compute machine-types list "
                     f"--filter=name={blueprint.machine_type} --project={project}"),
                kind=NO_ZONE,
            )
        has_card = zones_offering(
            gc.accelerator_types(project, blueprint.card.accelerator),
            blueprint.card.accelerator,
        )
        if zone not in has_card:
            raise LifecycleError(
                f"{zone} has never offered {blueprint.card.accelerator}, so a "
                f"{blueprint.card.name} box cannot be created there at all. Nothing "
                f"was created.",
                fix=(f"drop --zone and let this pick one, or pick a zone that has the "
                     f"card: gcloud compute accelerator-types list "
                     f"--filter=name={blueprint.card.accelerator} --project={project}"),
                kind=NO_ZONE,
            )
        return Ordering(zones=(zone,), regions=(region_of(zone),),
                        notes=("--zone was given, so there is no fall-through: this "
                               "zone or nothing",),
                        fall_through=False)

    regions = list(check.regions)
    if region:
        regions = [name for name in regions if name == region]
        if not regions:
            raise LifecycleError(
                f"this project has no {blueprint.card.name} quota in {region}, so "
                f"nothing can start there. Nothing was created.",
                fix=(f"comfy-qat quota request --gpu {blueprint.card.key} "
                     f"--region {region}, or drop --region and let this pick"),
                kind=NO_QUOTA,
            )

    return choose(
        gc, project,
        accelerator=blueprint.card.accelerator,
        machine_type=blueprint.machine_type,
        regions=regions, config=config, probe=probe,
    )


def nowhere(blueprint: Blueprint, ordering: Ordering, project: str) -> LifecycleError:
    """No zone at all fits, which is a refusal rather than a failed attempt."""
    detail = ordering.notes[0] if ordering.notes else (
        f"no region this project has {blueprint.card.name} quota in offers "
        f"{blueprint.machine_type}"
    )
    return LifecycleError(
        f"nowhere to put {blueprint.name}: {detail}. Nothing was created.",
        fix=(f"gcloud compute accelerator-types list --project={project} "
             f"--filter=name={blueprint.card.accelerator} — where Google offers the "
             f"card at all; comfy-qat quota list --by-region — where you may use it"),
        kind=NO_ZONE,
    )
