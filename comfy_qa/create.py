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

from dataclasses import dataclass

from .config import Host
from .gcloud import Gcloud, GcloudError
from .lifecycle import LifecycleError, is_capacity_failure, suggested_zones
from .zones import Ordering, region_of

# Kinds of refusal, so a caller can tell them apart without matching on prose.
NO_QUOTA = "no-quota"          # the project's grant will not allow this
NO_ZONE = "no-zone"            # nowhere offers this combination at all
EXHAUSTED = "exhausted"        # every zone tried was out of capacity
CREATE_FAILED = "create-failed"  # Google refused for some other reason

DEFAULT_DISK_GB = 200

# The Windows Server image will not fit below this, and a GPU box with no room
# for models is a box you pay to re-create. Ubuntu would take 10.
MIN_DISK_GB = 50


@dataclass(frozen=True)
class Card:
    """One GPU, and the machine it has to be ordered as.

    `attached` is the distinction that matters. On G2 and A2 the accelerator is
    part of the machine type and `--accelerator` is refused; on N1 the machine
    type has no GPU and the card is attached to it. Nothing about the names
    tells you which, so it is written down.
    """

    name: str            # what a person types, and what `host list` shows
    accelerator: str     # Google's own accelerator-type id
    machine_type: str
    attached: bool       # is the card part of the machine type?
    count: int = 1

    @property
    def accelerator_flag(self) -> str | None:
        """The `--accelerator` value, or None when the machine type carries it."""
        if self.attached:
            return None
        return f"type={self.accelerator},count={self.count}"


# The cards this tool can order, and the family each one has to be ordered in.
# Sizes are the smallest that fits a GPU: this is a box for reproducing a bug,
# not for training, and the card is what costs.
CARDS: dict[str, Card] = {
    "l4": Card("L4", "nvidia-l4", "g2-standard-8", attached=True),
    "t4": Card("T4", "nvidia-tesla-t4", "n1-standard-8", attached=False),
    "p4": Card("P4", "nvidia-tesla-p4", "n1-standard-8", attached=False),
    "p100": Card("P100", "nvidia-tesla-p100", "n1-standard-8", attached=False),
    "v100": Card("V100", "nvidia-tesla-v100", "n1-standard-8", attached=False),
    # Retired by Google in most regions. Left in because asking for it should
    # fail with "no zone offers this", which is the truth, rather than with
    # "unknown card", which is not.
    "k80": Card("K80", "nvidia-tesla-k80", "n1-standard-8", attached=False),
    "a100": Card("A100", "nvidia-tesla-a100", "a2-highgpu-1g", attached=True),
    "a100-80gb": Card("A100-80GB", "nvidia-a100-80gb", "a2-ultragpu-1g", attached=True),
    # The smallest H100 machine type is eight cards, so this needs eight of the
    # project's GPU allowance, not one. Counting it as one would pass the quota
    # gate and fail at the create.
    "h100": Card("H100-80GB", "nvidia-h100-80gb", "a3-highgpu-8g", attached=True, count=8),
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
        fix="comfy-qat auth quota list — the cards this project is allowed",
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
        fix="comfy-qat host create --os linux --gpu l4",
        kind=NO_ZONE,
    )


def _clean(name: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in name.lower())


def choose_name(preferred: str | None, image: Image, taken: set[str]) -> str:
    """A name nothing else is using, here or on the project.

    A default rather than a prompt, because the interesting decision is the card
    and the boring one should not interrupt it. `comfy-linux`, then
    `comfy-linux-2`: two boxes that differ only in zone and share a name is how
    you read a result off the wrong one.
    """
    if preferred:
        base = _clean(preferred)
        if base in taken:
            raise LifecycleError(
                f"{preferred} is already taken — a host list entry or an instance on "
                f"this project has that name. Pick another with --name.",
                fix="comfy-qat host list",
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
        fix="comfy-qat host list",
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
            fix=f"comfy-qat host create --os {image.key} --gpu {gpu} --disk {DEFAULT_DISK_GB}",
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

    def lines(self) -> list[str]:
        """What was checked and what it said — printed by a dry run and a real one."""
        def amount(value: int | None) -> str:
            if value is None:
                return "not granted"
            return "unlimited" if value < 0 else str(value)

        out = [
            f"{self.card}: {amount(self.card_limit)}"
            + (f", in {len(self.regions)} region(s)" if self.regions else ""),
            f"GPUS_ALL_REGIONS (every card, project-wide): {amount(self.global_limit)}",
        ]
        if self.running:
            out.append(f"already running and using it: {', '.join(self.running)}")
        return out

    def problem(self) -> LifecycleError | None:
        """The reason this cannot be created, or None. Nothing has happened yet."""
        if not self.card_limit:
            return LifecycleError(
                f"this project has no {self.card} quota, so a {self.card} box cannot "
                f"start anywhere. Nothing was created.",
                fix=f"comfy-qat auth quota request --gpu {self.card.lower()} "
                    f"--region us-central1, then wait for Google",
                kind=NO_QUOTA,
            )
        if self.card_limit > 0 and self.card_limit < self.needed:
            return LifecycleError(
                f"{self.card} needs {self.needed} of this project's GPU allowance and "
                f"the grant is {self.card_limit}. Nothing was created.",
                fix=f"comfy-qat auth quota request --gpu {self.card.lower()} "
                    f"--region us-central1, then wait for Google",
                kind=NO_QUOTA,
            )
        if self.global_limit is not None and 0 <= self.global_limit < self.needed:
            return LifecycleError(
                f"GPUS_ALL_REGIONS is {self.global_limit} on this project — that is the "
                f"ceiling across every card, whatever the {self.card} grant says, and "
                f"{self.needed} is needed. Nothing was created.",
                fix="comfy-qat auth quota request --gpu l4 --region us-central1 asks for "
                    "a card; raising the project-wide ceiling is a separate request at "
                    "https://console.cloud.google.com/iam-admin/quotas",
                kind=NO_QUOTA,
            )
        if self.running and self.global_limit is not None and 0 <= self.global_limit < (
            self.needed + len(self.running)
        ):
            names = ", ".join(self.running)
            return LifecycleError(
                f"GPUS_ALL_REGIONS is {self.global_limit} and {names} is already running "
                f"on it, so a new GPU box cannot start until that one stops. Nothing was "
                f"created.",
                fix=f"comfy-qat host down {self.running[0]} — stop the one you are not "
                    f"using, then run this again",
                kind=NO_QUOTA,
            )
        if not self.regions:
            return LifecycleError(
                f"this project's {self.card} grant names no region, so there is nowhere "
                f"to put the box. Nothing was created.",
                fix=f"comfy-qat auth quota request --gpu {self.card.lower()} "
                    f"--region us-central1",
                kind=NO_QUOTA,
            )
        return None


def _gpu_boxes_running(instances: list[dict]) -> list[str]:
    """Instances that are RUNNING and hold a card, so they are spending the ceiling."""
    found = []
    for instance in instances or []:
        if instance.get("status") != "RUNNING":
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


def check_quota(card: Card, quotas: list[dict], instances: list[dict]) -> QuotaCheck:
    """Read the allowance. Pure — the caller does the two gcloud reads."""
    from .quota import allowance, global_allowance, regions_with_quota

    return QuotaCheck(
        card=card.name,
        card_limit=allowance(card.name, quotas),
        global_limit=global_allowance(quotas),
        needed=card.count,
        running=tuple(_gpu_boxes_running(instances)),
        regions=tuple(regions_with_quota(card.name, quotas)),
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
) -> str:
    """Try the zones in order until one has room. Returns the zone that worked.

    Capacity is the one thing that cannot be checked in advance — Google
    publishes no endpoint for it — so this is try-and-see, and every attempt is
    announced before it is made. A silent thirty-second pause reads as a hang,
    and the pause is the normal case: a stockout refusal is not fast.

    A zone Google itself names in the refusal is moved to the front of what is
    left, because that answer is fresher than anything measured beforehand.
    """
    queue = list(ordering.zones)
    tried: list[str] = []
    while queue:
        zone = queue.pop(0)
        if zone in tried:
            continue
        tried.append(zone)
        say(f"trying {zone}…")
        try:
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
            for suggested in suggested_zones(exc.raw):
                if suggested not in tried and suggested not in queue:
                    say(f"  Google suggests {suggested}")
                    queue.insert(0, suggested)
            continue
        return zone

    raise LifecycleError(
        f"every zone tried is out of {blueprint.card.name} capacity: "
        f"{', '.join(tried)}. Nothing was created and nothing is billing.",
        fix=("wait and run the same command again — a stockout is usually minutes to "
             "hours — or ask for a different card: comfy-qat auth quota list"),
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
    lines.append(f"  comfy-qat host go {blueprint.name}     # install ComfyUI and serve it")
    lines.append(f"  comfy-qat host down {blueprint.name}   # stop the machine, stop paying")
    return lines


def summary(blueprint: Blueprint, ordering: Ordering) -> list[str]:
    """The zone order, said the way it was decided."""
    if not ordering.zones:
        return []
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
    opposite of what they asked for. It is still checked against what Google
    offers there, because finding out that a zone has never had an L4 is worth a
    second and not worth a create.

    `--region` narrows without naming a zone, which is the ordinary way to say
    "somewhere in Europe" — the zone inside it is still chosen and still falls
    through.
    """
    from .zones import choose, zones_with_machine_type

    if zone:
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
        return Ordering(zones=(zone,), regions=(region_of(zone),),
                        notes=("--zone was given, so there is no fall-through: this "
                               "zone or nothing",))

    regions = list(check.regions)
    if region:
        regions = [name for name in regions if name == region]
        if not regions:
            raise LifecycleError(
                f"this project has no {blueprint.card.name} quota in {region}, so "
                f"nothing can start there. Nothing was created.",
                fix=(f"comfy-qat auth quota request --gpu {blueprint.card.name.lower()} "
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
             f"card at all; comfy-qat auth quota list --by-region — where you may use it"),
        kind=NO_ZONE,
    )
