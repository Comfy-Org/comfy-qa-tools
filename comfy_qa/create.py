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
availability second, measured latency and where your other boxes already are
third — and this tries them in that order, one region at a time, falling through
when Google says a zone has none free. `--zone` is still there for someone
deliberately testing one zone, and `--region` for somewhere this would not have
looked: six attempts cannot reach forty-three regions, and the refusal at the end
of this file says which ones it did reach rather than implying it reached them
all.

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
from . import say as output
from .config import Host
from .gcloud import Gcloud, GcloudError
from .lifecycle import LifecycleError, is_capacity_failure, suggested_zones
from .zones import MAX_ATTEMPTS, Ordering, region_of

# Kinds of refusal, so a caller can tell them apart without matching on prose.
NO_QUOTA = "no-quota"          # the project's grant will not allow this
NO_ZONE = "no-zone"            # nowhere offers this combination at all
EXHAUSTED = "exhausted"        # every zone tried was out of capacity
CREATE_FAILED = "create-failed"  # Google refused for some other reason
NO_DRIVER = "no-driver"        # the card is real; this tool cannot bring it up

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


# --- which cards this tool can actually bring up ---------------------------
#
# THE MOST EXPENSIVE THING IN THIS FILE, so it is written down before the table
# it constrains. Every Linux box created here installs its driver exactly one
# way: Google's own `cuda_installer.pyz install_driver`, as a startup script
# (LINUX_DRIVER, below). That installer lays down the OPEN NVIDIA kernel module,
# and the open module only works on a GPU that has a GSP — a GPU System
# Processor, the on-board microcontroller NVIDIA moved most of the driver onto
# with Turing. NVIDIA's own words, from the source of the modules themselves
# (https://github.com/NVIDIA/open-gpu-kernel-modules, read 2026-09-09): "The
# NVIDIA open kernel modules can be used on any Turing or later GPU."
#
# On anything older every visible sign is success and the card is dead. Read off
# a V100 box this tool created on 2026-09-09: `create` returned 0, the startup
# script finished 0 with the packages installed and `nvidia-open set on hold`,
# the instance booted, `lspci` showed the card — and no nvidia module loaded,
# `nvidia-smi` failed, and a clean reboot did not change it. The kernel says why:
#
#   NVRM: The NVIDIA GPU 0000:00:04.0 (PCI ID: 10de:1db1)
#   NVRM: installed in this system is not supported by open
#   NVRM: nvidia.ko because it does not include the required GPU
#   NVRM: System Processor (GSP).
#   NVRM: The NVIDIA probe routine failed for 1 device(s).
#   NVRM: None of the NVIDIA devices were initialized.
#
# Nothing in the run says that. The box is up, it is billing, and the only thing
# it was made for cannot work.
#
# So the line is drawn at GSP, and by ARCHITECTURE rather than by a list of card
# names — every card is described by its architecture anyway, and a name list is
# a thing somebody has to remember to extend. GSP is silicon: a Pascal card will
# not grow one, so this is not a version skew that waiting out will fix.
#
# WHY THIS IS A REFUSAL AND NOT A SECOND DRIVER FLAVOUR. The obvious repair is
# to install the proprietary module on the older cards instead of the open one.
# There is no way to ask for that here: `cuda_installer.pyz` takes
# `--installation-branch` (prod, nfb, lts) and no flavour switch at all, so
# driving a pre-Turing card would mean abandoning Google's documented installer
# for a hand-rolled one and pinning a driver branch old enough to still carry
# the card. That is exactly the "probably works" this file already refuses to
# build on for the Windows driver, and it cannot be verified from a laptop. If
# someone builds it and proves it on real hardware, THIS is the one place to
# change: give those cards a driver that suits them and the table below decides
# the rest — the refusal, the card lists and the help all read from it.
#
# Turing introduced the GSP and everything since has one. Kepler, Maxwell,
# Pascal and Volta predate it and never got one.
GSP_ARCHITECTURES = frozenset({"Turing", "Ampere", "Ada", "Hopper", "Blackwell"})


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
    * `name` is what `comfy-qat list` shows, and it is *not* free: `discover`
      derives it from the instance's accelerator type, so it has to be exactly
      what `discover.accelerator` would read back off a box holding this card.
      Otherwise `comfy-qat discover` finds a box this tool created and adds it a
      second time.
    * `quota_aliases` are the other spellings Google meters the same card
      under. Empty for almost every card, and the reason it exists is H100:
      the accelerator is `nvidia-h100-80gb` and the quota is
      `NVIDIA-H100-GPUS`, with no 80GB anywhere in it.
    """

    key: str             # what a person types after --gpu
    name: str            # what `comfy-qat list` shows, and what discovery reads back
    accelerator: str     # Google's own accelerator-type id
    machine_type: str
    attached: bool       # is the card part of the machine type?
    # The GPU's generation, and the field that decides whether this tool will
    # order the card at all — see GSP_ARCHITECTURES above. It has no default on
    # purpose: a card added without one will not construct, which is the only
    # way to be sure the question gets asked about the next card as well.
    architecture: str
    count: int = 1
    quota_aliases: tuple[str, ...] = ()
    # The `gpu_family` DIMENSION this card is metered under, where the project
    # meters it that way rather than under an id of its own. Empty for every
    # card that has a `NVIDIA-<card>-GPUS-...` id.
    #
    # Both shapes are live and a project reports one or the other, so this is a
    # spelling the table knows, NOT a decision about which shape to use — that is
    # read from the project by `quota.resolve_target`, per-card id first. On this
    # project there is no `NVIDIA-H100-...` row at all and H100 is reachable only
    # through `GPUS-PER-GPU-FAMILY-per-project-region` with
    # `gpu_family=NVIDIA_H100`; on another project the per-card id may exist.
    quota_family: str = ""
    # How many vCPU one instance of this card's machine type consumes.
    #
    # Only N1 actually spends CPU quota — Google's resource-usage page says A2,
    # A3, A4, G2 and G4 VMs need "only ... the required GPU quotas ... You don't
    # need to request CPU quotas". So this gates T4, V100, P100, P4 and K80 and
    # nothing else, and `quota.cpu_quota_applies` is where that is decided.
    #
    # It was briefly used to gate every card, on the strength of two real zeros
    # and an inference. Kept for every card anyway: the number is measured and
    # true, and the next person to wonder whether a machine fits a quota should
    # find it here rather than guess. No default, for the same reason
    # `architecture` has none — a card added without one will not construct, so
    # the question gets asked about the next card too.
    #
    # Read from `gcloud compute machine-types describe <type> --format=
    # "value(guestCpus)"` on 2026-09-17 rather than transcribed from docs.
    vcpus: int = 0

    @property
    def has_gsp(self) -> bool:
        """Can the open kernel module this tool installs drive this card at all?

        False means the box would be created, billed and useless: the driver
        installs, the startup script exits 0, and no module ever loads.
        """
        return self.architecture in GSP_ARCHITECTURES

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


# Every card this tool has a machine-type mapping for, and the family each one
# has to be ordered in. Sizes are the smallest that fits a GPU: this is a box for
# reproducing a bug, not for training, and the card is what costs.
#
# NOT every one of these can be ordered. The four pre-Turing cards stay in the
# table for the reason K80 was always kept in it — asking for one should be
# answered with the truth about that card rather than with "unknown card" — and
# `plan` refuses them by reading `has_gsp`. Two of them, T4 and L4, were proved
# on real hardware on 2026-09-09: driver up, ComfyUI generating in 8.6s and 6.3s.
CARDS: dict[str, Card] = {
    "l4": Card("l4", "L4", "nvidia-l4", "g2-standard-8", attached=True,
               architecture="Ada", vcpus=8),
    "t4": Card("t4", "T4", "nvidia-tesla-t4", "n1-standard-8", attached=False,
               architecture="Turing", vcpus=8),
    # Pascal and Volta: real cards, real quota, no GSP. Ordering one buys a box
    # whose GPU cannot initialise — see GSP_ARCHITECTURES.
    "p4": Card("p4", "P4", "nvidia-tesla-p4", "n1-standard-8", attached=False,
               architecture="Pascal", vcpus=8),
    "p100": Card("p100", "P100", "nvidia-tesla-p100", "n1-standard-8", attached=False,
                 architecture="Pascal", vcpus=8),
    "v100": Card("v100", "V100", "nvidia-tesla-v100", "n1-standard-8", attached=False,
                 architecture="Volta", vcpus=8),
    # Retired by Google in most regions, and Kepler besides, so it fails this
    # tool's own driver check before it ever reaches a zone.
    "k80": Card("k80", "K80", "nvidia-tesla-k80", "n1-standard-8", attached=False,
                architecture="Kepler", vcpus=8),
    "a100": Card("a100", "A100", "nvidia-tesla-a100", "a2-highgpu-1g", attached=True,
                 architecture="Ampere", vcpus=12),
    "a100-80gb": Card("a100-80gb", "A100-80GB", "nvidia-a100-80gb", "a2-ultragpu-1g",
                      attached=True, architecture="Ampere", vcpus=12),
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
                 attached=True, architecture="Hopper", count=8,
                 quota_aliases=("H100",), quota_family="NVIDIA_H100", vcpus=208),
}

# FOUR CARDS THIS PROJECT METERS AND THIS TABLE DELIBERATELY DOES NOT CARRY, so
# that their absence is a recorded decision rather than an oversight. All four are
# visible in `comfy-qat quota list` — `quota.rows` names a card from its
# `gpu_family` dimension, so the tool reports what the project holds whether or
# not this table has heard of it — and none of them is ordered or asked for.
#
# They are listed in `KNOWN_ELSEWHERE` below rather than only in this comment,
# because `create --gpu b200` answered "no card called 'b200'" — the same
# sentence as `--gpu banana` — while `quota list` printed a B200 row in the same
# minute. One surface saying a card does not exist while another lists it is what
# a comment cannot prevent and a constant can.
#
# The accelerator ids above were read from `gcloud compute accelerator-types
# list` on 2026-09-17 and are correct. What is missing is the MACHINE TYPE and
# the CARD COUNT, and those are the two fields that cost money to get wrong: the
# machine type is what `create` orders, and `count` is what the ceiling check
# measures a request against — H100 is 8 rather than 1 for exactly that reason.
# Neither can be confirmed without creating an instance, which is the one thing
# this work may not do. A card added here with a guessed machine type would pass
# the quota gate and fail at the create, after the wait for approval.
#
# H200 and B200 additionally have NO standard on-demand quota — Google's
# allocation-quota table lists A3 Ultra and A4 as "Not available", with only
# COMMITTED_* and PREEMPTIBLE_* variants — so asking for either is asking for a
# refusal from a human reviewer, and `--validate-only` passing says nothing about
# that. Those two need a reserved-capacity story, not a table row.
#
# To add one: fill in the machine type and count, give it `quota_family`, and
# everything else follows — the refusal, the card lists, the help, `quota list`
# and the requests `setup` files all read from this table.


# SPOT / PREEMPTIBLE, read live on 2026-09-17 and recorded because it is the one
# route this project has that is not behind a refusal. This tool does not order
# Spot instances today; the facts are here so that whoever adds it starts from
# measurements rather than from an inference.
#
# Values are `dimensionsInfos[].details.value`, scanned across EVERY row:
#
#   PREEMPTIBLE-CPUS-per-project-region            no value in any of 43 rows
#   PREEMPTIBLE-NVIDIA-L4-GPUS-per-project-region  1, one undimensioned row, 43 locations
#   PREEMPTIBLE-NVIDIA-RTX-PRO-6000-GPUS-...       1, one undimensioned row, 43 locations
#   PREEMPTIBLE-NVIDIA-T4-GPUS-per-project-region  1, across 25 rows / 43 locations
#   PREEMPTIBLE-NVIDIA-V100-GPUS-...               1, across 25 rows / 43 locations
#
# A Spot **L4** (G2) or **RTX PRO 6000** (G4) needs no CPU quota — both families
# are on Google's "you don't need to request CPU quotas" list — so
# `PREEMPTIBLE_CPUS` having no value does not stop either. Both already hold
# preemptible GPU quota project-wide.
#
# A Spot **T4 or V100** is N1, which DOES consume `PREEMPTIBLE_CPUS`, and that is
# where it stops. ONCE, not twice: their preemptible GPU quota is granted across
# 43 locations like the others.
#
# THE CORRECTION IS THE POINT. This block first said T4 and V100 were "1 in
# asia-east1 ONLY (1 location)" and therefore blocked twice over. That came from
# a measuring script that stopped at the FIRST row with a value in it — and
# asia-east1 is simply the first row alphabetically. The per-card quotas return
# one undimensioned row and the dimensioned ones return twenty-five, so a reader
# that takes row zero is right for one shape and silently wrong for the other.
# Same two-shapes problem `needs_region` and `matching_ask` exist to handle, met
# again in a throwaway script rather than in the product — where every reader
# does scan. Scan the rows; never read the first.
#
# "No value" is read as zero throughout this tool (`quota.rows`), which matches
# an independent reading of 0 for `PREEMPTIBLE_CPUS` — but it is an INFERENCE
# from an absence, and inferring from an absence is exactly what produced a CPU
# gate that does not exist. Named here so the next person weighs it as one.


KNOWN_ELSEWHERE: dict[str, str] = {
    # Cards Google sells and meters, and this table has no machine type for. The
    # accelerator ids were read from `gcloud compute accelerator-types list` on
    # 2026-09-17; see the note above for why the machine type cannot be guessed.
    "h100-mega": "nvidia-h100-mega-80gb",
    "rtx-pro-6000": "nvidia-rtx-pro-6000",
    "h200": "nvidia-h200-141gb",
    "b200": "nvidia-b200",
}


def known_card(gpu: str) -> bool:
    """Is this a real card at all — whatever this tool can do with it?

    The distinction `--gpu banana` and `--gpu b200` did not have. One is a typo;
    the other is a card this project meters and `quota list` prints.
    """
    key = (gpu or "").strip().lower()
    return card_named(gpu) is not None or key in KNOWN_ELSEWHERE


def _refused_regions(card: Card, preferences: list[dict] | None) -> list[str]:
    """Where Google has already said no to this card. Empty when unknown."""
    from .quota import asks, row_name

    if not preferences:
        return []
    found = set()
    for ask in asks(preferences):
        if ask.state != "denied":
            continue
        named = row_name(ask.quota_id, ask.dimensions)
        if named and named.upper() in {n.upper() for n in card.quota_names}:
            found.add(ask.region or "")
    return sorted(p for p in found if p)


def _ask_elsewhere(card: Card, quotas: list[dict],
                   preferences: list[dict] | None) -> list[str]:
    """Regions this card is metered in, minus the ones Google already refused.

    THE FALLBACK ONLY. Metered is where a request is POSSIBLE; the caller passes
    `askable`, which is metered AND STOCKED — because a remedy naming a region
    that sells nothing is a command that exits 2, and this function composing the
    answer by itself is how `create` came to print `africa-south1`.
    """
    from .quota import regions_metered

    refused = set(_refused_regions(card, preferences))
    found: set[str] = set()
    for name in card.quota_names:
        found |= set(regions_metered(name, quotas))
    return sorted(found - refused)


def _article(name: str) -> str:
    """`a` or `an`, by how the name is READ ALOUD rather than by its spelling.

    Card names start with digits and letters that are said as their own words:
    `A100` is "ay-hundred", `H100` is "aitch", `8` is "eight". So the rule is the
    initial SOUND, and for this table that is a short list of letters rather than
    a guess at English.
    """
    return "an" if name[:1].upper() in "AEFHILMNORSX8" else "a"


def offered(gpu: str) -> bool:
    """Will `comfy-qat create --gpu` accept a box of this card?

    THE one predicate. There were three — `auth.undrivable`, `auth.uncreatable`
    and `setup._drivable` — and they disagreed: `undrivable` read
    `card is not None and not card.has_gsp`, so a card the table has never heard
    of fell through to drivable, while `uncreatable` in the same file called the
    same card not creatable. `--json` marked B200, H200, H100-MEGA and
    RTX-PRO-6000 `"drivable": true` about cards `create` refuses by name.

    `CARDS` holds NINE cards, not five: K80, P4, P100 and V100 are in it
    deliberately so `create --gpu p100` answers with the GSP explanation instead
    of "unknown card". So "in the table" is a strictly larger set than "orderable",
    and reading the first as the second is how B200 slipped past.
    """
    card = card_named(gpu)
    return card is not None and card.has_gsp


def no_gsp(gpu: str) -> bool:
    """Is this a card the table knows and the driver cannot bring up?

    Distinct from `not offered`, and the distinction earns its keep: a card with
    no GSP gets the specific footnote about the open kernel module, while a card
    the table has never heard of has no such diagnosis and must not be given one.
    """
    card = card_named(gpu)
    return card is not None and not card.has_gsp


def unspendable(gpu: str, pool: str = "") -> str:
    """Why this tool cannot turn an allowance for `gpu` into a running box, or "".

    ONE PLACE, because `quota list` and `setup` both need it and they disagreed.
    `auth.py` grew ", no {pool} support yet" and `setup.py` never got it — the
    third correction in one night to land on one surface and miss its sibling —
    so `setup` reported "granted as Spot, no on-demand request needed" about a
    card `create` cannot order by any path, while the table said otherwise.

    Two reasons, and they are different facts:

    * the card is not in `CARDS`, so no `--gpu` value reaches it at all;
    * the card is orderable, but the only allowance is in a pool `create` cannot
      spend. Every pool except on-demand is in that position today — see
      `docs/spot-instances.md`, which is a plan and not an implementation.

    Derived from the table rather than restated, so a card added to `CARDS` stops
    being caveated with nothing else edited.
    """
    from .quota import GLOBAL_ALLOWANCE, ON_DEMAND

    # The project-wide ceiling is not a card and `create` was never going to
    # order one. Left out, it read "any (global) — not creatable by this tool",
    # which is true of nothing: applying a card rule to a row that is not a card.
    if gpu == GLOBAL_ALLOWANCE:
        return ""
    if no_gsp(gpu):
        # Its own sentence, and the GSP footnote explains it underneath.
        return ""
    if not offered(gpu):
        return "not creatable by this tool"
    if pool and pool != ON_DEMAND:
        return f"no {pool} support yet"
    return ""


def drivable_cards() -> list[str]:
    """The `--gpu` spellings this tool can actually bring up, in help order.

    The one source for every list of cards the tool shows anybody: the `--gpu`
    help, the "no card called" refusal, `quota list`, `setup`. They used to be
    typed out separately, which is how a card the provisioning cannot drive gets
    advertised in three places and refused in none.
    """
    return sorted(key for key, card in CARDS.items() if card.has_gsp)


def undrivable_cards() -> list[str]:
    """The cards this tool knows about and will not order. See GSP_ARCHITECTURES."""
    return sorted(key for key, card in CARDS.items() if not card.has_gsp)


def card_named(name: str) -> Card | None:
    """The card a QUOTA's friendly name means — `P100`, `H100`, `A100-80GB`.

    `card_for` answers for what a person types after `--gpu` and raises when
    there is no such card. This answers for what Google meters, which is not the
    same string: the H100 is metered as `H100` and ordered as `h100`, and a
    project can hold quota for cards this tool has never heard of. So it returns
    None rather than raising — "not one of ours" is an ordinary answer here.
    """
    wanted = (name or "").strip().lower().replace("_", "-").replace(" ", "")
    wanted = wanted.removeprefix("nvidia-").removeprefix("tesla-")
    if not wanted:
        return None
    for card in CARDS.values():
        spellings = {card.key, *card.quota_names}
        if wanted in {spelling.lower() for spelling in spellings}:
            return card
    return None


def undrivable(card: Card, image: Image | None = None) -> LifecycleError:
    """Why a real card with real quota is still refused, before anything exists.

    Raised by `plan`, which runs offline and before the quota read, so this
    costs a second and no money. The alternative — the behaviour this replaced —
    is a created, billing instance whose GPU never initialises and a run that
    reported success.
    """
    os_key = image.key if image else "linux"
    return LifecycleError(
        f"this tool cannot bring up a {card.name}, so nothing was created. The "
        f"driver it installs is the open NVIDIA kernel module, and that needs a "
        f"GPU System Processor — a GSP — which only Turing and newer cards have. "
        f"{card.architecture} has none, so no module loads at all: the box would "
        f"boot, bill, and never see its own GPU. Cards that do work: "
        f"{', '.join(drivable_cards())}.",
        # The comma is load-bearing: a fix line is read as a command up to the
        # first comma or semicolon, so prose after an em-dash would be parsed as
        # more flags. `tests/test_create_e2e.py::_invocations` is what reads it.
        fix=(f"comfy-qat create --os {os_key} --gpu t4, the same n1-standard-8 "
             f"machine and the cheapest card that works"),
        kind=NO_DRIVER,
    )


@dataclass(frozen=True)
class Image:
    """A public image family, and the name `comfy-qat discover` reads back off it.

    `os` matches what `discover.operating_system` derives from the boot disk's
    licence, so a box created here and a box found by discovery describe
    themselves identically. They stopped agreeing once, and
    `comfy-qat switch windows` then matched one of them and not the other.
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
# not ready the moment `create` returns — `comfy-qat go` waits for SSH and then for
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
    if (gpu or "").strip().lower() in KNOWN_ELSEWHERE:
        # M8: A REAL CARD, AND THIS TOOL HAS NO MACHINE TYPE FOR IT. Answering
        # "no card called 'b200'" said the card does not exist, in the same
        # minute `quota list` printed a B200 row — and left the reader with no
        # way to tell a typo from a gap in this table.
        raise LifecycleError(
            f"{gpu} is a real card and this tool has no machine type for it, so "
            f"it cannot create one. This tool can create: "
            f"{', '.join(drivable_cards())}.",
            fix="comfy-qat quota list — what this project holds, including "
                "cards this tool cannot order",
            kind=NO_QUOTA,
        )
    raise LifecycleError(
        # The DRIVABLE cards, not every key in the table. The table also holds
        # four cards this tool refuses to order at all (GSP_ARCHITECTURES), and
        # naming those here would answer "which card should I ask for instead"
        # with cards that produce a billing box whose GPU never comes up.
        f"no card called {gpu!r}. This tool can create: "
        f"{', '.join(drivable_cards())}.",
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
    # BEFORE the disk checks, and long before the quota read: a card this tool
    # cannot drive is not a detail of the box, it is the box. `plan` is the last
    # thing that runs while a create is still free.
    if not card.has_gsp:
        raise undrivable(card, image)
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
    # `(name, zone)` per box, because a refusal that names a box has to hand over
    # a command that stops it, and the zone is the half of that command this file
    # used to throw away. See `_stop_the_box`.
    running: tuple[tuple[str, str], ...]
    regions: tuple[str, ...]
    # What a person types after `--gpu` to mean this card. Not the same string as
    # `card` for the H100, and a fix line that says `--gpu h100-80gb` names a card
    # this tool refuses.
    key: str = ""
    # Cards held by those running boxes, which is not len(running). One running
    # `a3-highgpu-8g` is one instance and eight of the ceiling, and counting boxes
    # lets a create through the gate that Google then refuses.
    held: int = 0
    quota_name: str = ""
    """What `quota list` calls this card, which is not always its display name.

    The H100 is shown as `H100-80GB` and metered as `H100`, so a refusal naming
    the display name sent readers to a table row that does not exist.
    """
    elsewhere: tuple[str, ...] = ()
    """Regions this card is metered in and has NOT been refused in.

    Carried so the refusal can name somewhere to ask rather than `<one of them>`,
    which pointed at a table whose whole content for this card was the refused
    region and an unnamed bucket.
    """
    refused_in: tuple[str, ...] = ()
    """Regions Google has already refused this card in, if that could be read.

    `create` used to say "ask and wait" about a card `quota list` reported as
    denied, and the remedy it printed derived the very region the refusal was
    made in. Two surfaces, one card, opposite advice — and the one that spends
    money gave the futile half.
    """
    asked_region: str = ""
    """The region the USER named, for a fix line that sends them back to it.

    The refusal said `--region us-central1` whatever was typed, so on a project
    already refused there it sent somebody to re-file the exact request Google
    denied, in a region they had not asked about.
    """

    def lines(self) -> list[str]:
        """What was checked and what it said — printed by a dry run and a real one."""
        from .quota import where_label as _where_label

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
            # L14: the SAME phrasing `quota list` prints, from the same
            # function — "in 43 region(s)" beside that table's own label for the
            # identical span was the third vocabulary for one geography.
            + (f", in {_where_label(self.regions)}" if self.regions else ""),
            f"GPUS_ALL_REGIONS (every card, project-wide): {ceiling(self.global_limit)}",
        ]
        if self.running:
            cards = "1 card" if self.held == 1 else f"{self.held} cards"
            out.append(f"already running and holding {cards} of it: "
                       f"{', '.join(self.running_names)}")
        return out

    @property
    def running_names(self) -> tuple[str, ...]:
        """Just the names, for a sentence. The zones are for the fix line."""
        return tuple(name for name, _ in self.running)

    @property
    def typed(self) -> str:
        """The `--gpu` spelling to put in a fix line, never the display name."""
        return self.key or self.card.lower()

    @property
    def _ask_somewhere(self) -> str:
        """The remedy, which must not be "ask again where you were refused".

        `quota list` says "asking again will not help" about exactly this card
        while this line said "then wait for Google", and the command it prints
        derives the refused region when none is given. A refusal somewhere is not
        a refusal everywhere — `quota list` reports the same card never asked in
        forty-two other regions — so the useful remedy is a different region.

        BOTH BRANCHES read this, because the last time one of them was corrected
        the other was not, and that was the fourteenth instance of exactly that.
        """
        plain = (f"comfy-qat quota request --gpu {self.typed}"
                 f"{self._where}, then wait for Google")
        if not self.refused_in:
            return plain
        # NAME THEM. `<one of them>` sent the reader to `--by-region`, whose
        # entire content for this card is the refused region plus a bucket row
        # called `any of 42` — there is no "them" to pick one of. The regions
        # were in hand all along: this object is built from the quota records.
        refused = ", ".join(self.refused_in)
        if not self.elsewhere:
            # AND SOMETIMES THERE IS NOWHERE ELSE, which is worth saying outright
            # rather than printing a placeholder that implies there is. This
            # project meters the card only where it was refused.
            return (f"Google already refused {self.card} in {refused}, and that "
                    f"is the only region this project meters it in — so there is "
                    f"nowhere else to ask.\n"
                    f"comfy-qat quota list --by-region  # what this project does "
                    f"hold")
        picks = ", ".join(self.elsewhere[:3])
        return (f"Google already refused {self.card} in {refused}, so asking "
                f"there again will not help. Ask somewhere else:\n"
                f"comfy-qat quota request --gpu {self.typed} --region "
                f"{self.elsewhere[0]}  # or {picks}\n"
                f"comfy-qat quota list --by-region  # every region it is metered in")

    @property
    def _where(self) -> str:
        """` --region <what the user asked for>`, or nothing at all.

        NOT A FALLBACK LITERAL. This said `--region us-central1` when no region
        was typed, and on this project that is exactly where H100 was refused —
        so the remedy for "no H100 quota" was to re-file the request Google had
        already denied, in a region the user never named, and then wait for an
        answer already given. `create` cannot vet a region: it does not read
        preferences and does not know what a region sells. `quota request` does
        both, and derives one when none is given, so the honest thing is to hand
        the question over rather than guess at it.
        """
        return f" --region {self.asked_region}" if self.asked_region else ""

    def problem(self) -> LifecycleError | None:
        """The reason this cannot be created, or None. Nothing has happened yet.

        Order matters here, and the rule is one line: a refusal built on
        something READ beats a refusal built on something MISSING.

        The missing thing is the region set. A quota payload can carry a LIMIT
        and no LOCATIONS — the live API leaves the per-entry `dimensions` null
        and puts the places in `applicableLocations`, which quota.py's own
        docstring says — so an empty region set has two explanations: a project
        that really holds a grant covering nowhere, and a payload whose shape
        this did not read. The ceiling refusals have one. GPUS_ALL_REGIONS is a
        number that was read, and a box holding it is a box that was seen.

        And the ceiling refusal is the one that costs money to hide. It is the
        only sentence here that says a GPU box is running RIGHT NOW, and it hands
        over a stop command that works tonight; the region wording hands over a
        request to Google and a wait. Leading with the ceiling costs nothing even
        when the region gap is real too — the retry says so — while leading with
        the region gap loses the mention of the live box altogether. On a project
        whose ceiling is 1, that is the refusal a tester meets most.

        So the missing-region case is asked LAST, and there is a behavioural test
        for that in `test_create.py`. It was moved to the FRONT in 3e6feb0, to
        stop a limit-parses-no-regions payload reporting "this project has no L4
        quota". That payload never reported it: `not self.regions and
        self.card_limit` and `not self.card_limit` are mutually exclusive by
        construction, so those two can be in either order and neither can reach
        the other's case. Running the parent proves it. What the move did reach
        was the ceiling, which it hid — untested, and unmentioned in the commit.

        The `and self.card_limit` guard is redundant where this block now stands;
        nothing arrives here with a falsy limit. It is kept so that moving the
        block can never again resurrect the wrong cause.
        """
        if not self.card_limit:
            # THE NAME `quota list` SHOWS — which is now the card's own name,
            # and used to be its alias.
            #
            # THIS COMMENT USED TO ARGUE THE OPPOSITE: "the quota table has an
            # `H100` row and no `H100-80GB` row, so a reader following this
            # sentence to the table found nothing by that name." That was true
            # when it was written. It stopped being true when `family_name` began
            # returning the card table's name, and the code went on following the
            # dead rationale — so `create --gpu h100` printed "H100-80GB: 0" and
            # "this project has no H100 quota" four lines apart. It is kept, as a
            # correction rather than a deletion, because the reasoning is right
            # and only its premise moved: this name has to be whatever the table
            # prints, and asserting which one that is belongs in a test.
            metered = self.quota_name
            return LifecycleError(
                f"this project has no {metered} quota, so {_article(metered)} "
                f"{metered} box cannot "
                f"start anywhere. Nothing was created.",
                # THE REGION THE USER ASKED FOR, not a literal. This said
                # `--region us-central1` whatever was typed — which on a project
                # that has already been refused there sends somebody to re-file
                # the exact request Google denied, in a region they did not name.
                fix=self._ask_somewhere,
                kind=NO_QUOTA,
            )
        if self.card_limit > 0 and self.card_limit < self.needed:
            return LifecycleError(
                f"{self.card} needs {self.needed} of this project's GPU allowance and "
                f"the grant is {self.card_limit}. Nothing was created.",
                # THE SIBLING. The branch above was corrected to stop naming a
                # literal region and this one was not, so it went on sending
                # people to us-central1 whatever they typed — the fourteenth time
                # a fix landed on one site and missed the one beside it.
                fix=self._ask_somewhere,
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
            first = _stop_the_box(*self.running[0])
            if len(self.running) == 1:
                return LifecycleError(
                    f"GPUS_ALL_REGIONS is {self.global_limit} and "
                    f"{self.running_names[0]} is already running on it, so a new GPU "
                    f"box cannot start until that one stops. Nothing was created.",
                    fix=f"{first} — stop the one you are not using, then run this "
                        f"again",
                    kind=NO_QUOTA,
                )
            return LifecycleError(
                f"GPUS_ALL_REGIONS is {self.global_limit} and {len(self.running)} GPU "
                f"boxes are already running on it, holding {self.held} of it between "
                f"them: {', '.join(self.running_names)}. Nothing was created.",
                fix=f"{first} — stop the ones you are not using, then run this again",
                kind=NO_QUOTA,
            )
        # Last, and read the docstring before moving it: everything above is a
        # number this read, and an empty region set is also what an unread
        # payload looks like.
        if not self.regions and self.card_limit:
            return LifecycleError(
                f"this project's {self.card} grant names no region, so there is "
                f"nowhere to put the box. The grant itself is "
                f"{self.card_limit}. Nothing was created.",
                # THE THIRD SITE of the same invented literal, found by grep
                # after the second turned up beside the first.
                fix=f"comfy-qat quota request --gpu {self.typed}{self._where}",
                kind=NO_QUOTA,
            )
        return None


def _stop_the_box(name: str, zone: str) -> str:
    """The command that stops a GPU box holding the project-wide ceiling.

    Deliberately gcloud's and not `comfy-qat down <name>`. `config.resolve`
    matches host-list names, then os/gpu descriptions, and never `gce_instance` —
    so a refusal built from an instance name hands over a command that exits
    "no host called that", which is the tool refusing to spend money and then
    telling you to run something it cannot run. The box holding the only slot is
    usually one somebody started in the console, and that is precisely the box
    absent from the host list. `host._undeclared_and_running` reached the same
    conclusion for the same case.

    The zone is in the payload `_gpu_boxes_running` reads, and was being dropped
    on the floor. When it is genuinely absent, say how to find it rather than
    printing a command with a hole in it.
    """
    if not zone:
        return (f"gcloud compute instances list   # find {name}'s zone, then "
                f"gcloud compute instances stop {name} --zone=<zone>")
    return f"gcloud compute instances stop {name} --zone={zone}"


def _gpu_boxes_running(instances: list[dict]) -> list[tuple[str, str]]:
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
            # `zone` arrives as a URL — .../zones/us-central1-a — and the tail is
            # what gcloud takes.
            zone = str(instance.get("zone") or "").rstrip("/").rsplit("/", 1)[-1]
            found.append((name, zone))
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


def check_quota(card: Card, quotas: list[dict], instances: list[dict],
                asked_region: str = "", *,
                preferences: list[dict] | None = None,
                askable: "list[str] | None" = None) -> QuotaCheck:
    """Read the allowance. Pure — the caller does the gcloud reads.

    `preferences` is OPTIONAL and `None` means "could not be read", which must
    not block a create: it only ever removes advice, never adds a refusal.
    """
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
        # THE CARD'S OWN NAME. This was `quota_names[-1]` — the ALIAS — because
        # `quota list` used to print `H100` where this table says `H100-80GB`.
        # `family_name` now returns the card table's name, so the two agree and
        # the alias is the spelling nothing shows any more.
        quota_name=card.name,
        refused_in=tuple(_refused_regions(card, preferences)),
        elsewhere=tuple(askable if askable is not None
                        else _ask_elsewhere(card, quotas, preferences)),
        asked_region=asked_region or "",
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
                    # Both, never one. gcloud's advice was written for the
                    # refusal and this one was written for the box: a create that
                    # timed out may have made an instance anyway, and the console
                    # check is the only line that finds it. Reading `exc.fix or
                    # ...` meant the failures most likely to leave something
                    # billing — a timeout carries a fix, a flat refusal does not
                    # — were exactly the ones that dropped the check.
                    fix=output.fix(
                        exc.fix,
                        f"check the console for a half-made {blueprint.name} before "
                        f"trying again: gcloud compute instances list "
                        f"--project={project}",
                    ),
                    kind=CREATE_FAILED,
                ) from exc
            say(f"  {zone} has no {blueprint.card.name} free right now")
            if ordering.fall_through:
                for named in suggested_zones(exc.raw):
                    # `suggested_zones` lowers what it returns, so this is belt
                    # and braces rather than the repair it once was. Kept because
                    # `tried` and `queue` are compared with `in`: a zone arriving
                    # in the wrong case would not merely be an argument gcloud
                    # rejects, it would defeat the already-tried check and be
                    # attempted twice.
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
            # `--region` first, because it is the flag that matches what this
            # message says. The advice used to offer `--zone` alone, which asks
            # someone who has just been told a whole neighbourhood is short to
            # name one machine room in it.
            fix=("wait and run the same command again, ask for a region this did not "
                 "reach: comfy-qat create --region <region>, or name a zone yourself: "
                 "comfy-qat create --zone <zone>"),
            kind=EXHAUSTED,
        )

    # How much of the world this actually looked at. Every word of the old
    # message was true and the sentence it made was not: "every zone tried is out
    # of L4 capacity: <six European zones>" is read as "there is no L4 anywhere
    # you can have one", and the run that produced this change was followed
    # immediately by `create --region us-central1` succeeding on its second zone.
    # Capacity existed, in the region this project's whole fleet already lives
    # in, and nothing in the refusal suggested looking there — the fix line did
    # not even mention `--region`.
    searched = list(dict.fromkeys(region_of(zone) for zone in tried))
    untried = [region for region in ordering.offering if region not in set(searched)]

    if untried:
        raise LifecycleError(
            f"every zone tried is out of {blueprint.card.name} capacity: "
            f"{', '.join(tried)}. That is {len(searched)} of the "
            f"{len(ordering.offering)} regions this project can use the card in, the "
            f"nearest ones — not everywhere. Nothing was created and nothing is "
            f"billing. Not tried, and possibly free: {_a_few(untried)}.",
            # `image.key`, never `image.os`. `--os` takes the key — `linux`,
            # `windows` — and `os` is the DISPLAY name this tool reads back off a
            # box, `Ubuntu 22.04`. This line had drifted to the second one while
            # its two siblings in `plan` kept the first, and what it printed was
            # `comfy-qat create --os Ubuntu 22.04 --gpu l4 --region <x>`: run
            # unquoted, Typer exits 2 on the stray `22.04`; run quoted,
            # `image_for` refuses it. So the one command offered to somebody
            # holding a stockout could not be run either way — and this is the
            # ordinary stockout branch, not a corner, because `choose` returns at
            # most six zones against a cap of six, so the queue drains, `capped`
            # stays False and this is what a real shortage lands on.
            fix=(f"try somewhere this did not reach: comfy-qat create "
                 f"--os {blueprint.image.key} --gpu {blueprint.card.key} --region "
                 f"{untried[0]}; or wait and run the same command again — a stockout "
                 f"is usually minutes to hours"),
            kind=EXHAUSTED,
        )

    if ordering.offering:
        raise LifecycleError(
            f"every zone tried is out of {blueprint.card.name} capacity: "
            f"{', '.join(tried)}. That is every region this project can use the card "
            f"in, so there is nowhere left to try right now. Nothing was created and "
            f"nothing is billing.",
            fix=("wait and run the same command again — a stockout is usually minutes "
                 "to hours — or ask for a different card: comfy-qat quota list"),
            kind=EXHAUSTED,
        )

    # Nothing ranked anything, so this frame cannot say how wide the search was
    # and must not guess. `--zone` is the case: one zone, by request, and a
    # sentence about how many regions were considered would be an invention.
    raise LifecycleError(
        f"every zone tried is out of {blueprint.card.name} capacity: "
        f"{', '.join(tried) or 'none were offered'}. Nothing was created and nothing "
        f"is billing.",
        fix=("wait and run the same command again — a stockout is usually minutes to "
             "hours — or drop --zone and let this pick: comfy-qat create"),
        kind=EXHAUSTED,
    )


def _a_few(names: list[str], most: int = 3) -> str:
    """Three names and a count. Forty is not a sentence — see `_grant_reaches`."""
    listed = ", ".join(names[:most])
    others = len(names) - most
    return f"{listed} and {others} more" if others > 0 else listed


def host_entry(blueprint: Blueprint, zone: str, project: str):
    """The discovered-box record `discover.to_toml` turns into a host list entry.

    Written through `discover` rather than by hand so a created box and a
    discovered one are the same shape — otherwise `comfy-qat discover` finds this
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
            f"script, which reboots it once or twice. `comfy-qat go` waits that "
            f"out.")
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


def _grant_reaches(check: QuotaCheck, most: int = 3) -> str:
    """Where the grant DOES apply, as a phrase: three regions and a count.

    Both quota refusals in `order_zones` say this, because it is the sentence
    that separates the two readings of "no quota in <place>". For a typo the
    list settles it at a glance — `us-central9` beside a list containing
    `us-central1` is its own diagnosis. For a real gap it answers the question
    the refusal raises, which is "then where?". Three, because forty-three is
    not a sentence.

    One function rather than the same three lines twice: the two refusals were
    written as copies and then drifted, which is what this whole change is
    repairing.
    """
    granted = ", ".join(check.regions[:most])
    others = len(check.regions) - most
    return f"{granted} and {others} more" if others > 0 else granted


def order_zones(
    gc: Gcloud, project: str, blueprint: Blueprint, check: QuotaCheck, *,
    zone: str | None = None, region: str | None = None, config=None, probe=None,
    fleet: list[str] | None = None,
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

    `fleet` is the zones the host list already has boxes in. It is a preference
    and never a filter: it cannot reach past `--zone`, past `--region`, or past
    the regions the grant covers, because all three are applied before it is
    consulted. What it does is stop a laptop's round trip being the only thing
    that speaks for where a box should go.
    """
    from .zones import choose, zones_offering, zones_with_machine_type

    # Google's zone and region names are lowercase, and so is everything read back
    # from it. `--zone US-CENTRAL1-A` is the same request; matching it against the
    # quota regions without lowering it refuses with a message about a region that
    # does not exist.
    zone = zone.strip().lower() if zone else zone
    region = region.strip().lower() if region else region

    if zone:
        # The same gate `--region` gets — and it says so truthfully only now.
        # `--zone me-west1-a` used to sail past and find out from gcloud instead,
        # a minute and a confirmation prompt later; this branch closed that. But
        # the next two commits improved the wording HERE and nowhere else, while
        # this comment went on claiming parity, so `--region` kept the bare
        # reason and a fix that opened with a quota request for a region that may
        # not exist. Two commits, believed identical, sixty lines apart. The
        # refusals are one message in two places: change one, change the other.
        # Skipped when the grant names no region at all, which `problem()`
        # refuses on its own.
        if check.regions and region_of(zone) not in set(check.regions):
            # Naming where the grant DOES apply is what separates the two
            # readings, and it costs nothing — `check.regions` is already read.
            # `_grant_reaches` holds the wording and the reasons.
            #
            # And the payload cannot be made to separate them either, which is
            # the next idea and a worse message than this one. The tempting move
            # is to treat the union of `applicableLocations` across the quota
            # records as a live list of Google's real regions — no staleness,
            # derived from this project tonight — and call a zone whose region is
            # absent from it a typo. It does not hold: `Gcloud.gpu_quotas` keeps
            # only records whose id mentions GPUs, and the region-scoped record
            # lists the regions the grant APPLIES in, not every region Google
            # has. On the payload recorded off the live project, `me-west1` — a
            # real region, and the docs' own example of a genuine gap — appears
            # nowhere at all. So absence means "no grant here", the thing already
            # known, and reading it as "no such region" would tell someone their
            # correct spelling is wrong. Ambiguous and true beats specific and
            # backwards; there is a test for both zones below.
            raise LifecycleError(
                f"this project has no {blueprint.card.name} quota in "
                f"{region_of(zone)}, so nothing can start in {zone}. It holds "
                f"{blueprint.card.name} in {_grant_reaches(check)}. "
                f"Nothing was created.",
                # The zone name is checked FIRST, and that ordering is the
                # whole of this fix. `--zone us-central9-a` is a typo, not a
                # quota gap: `region_of` turns it into `us-central9`, this
                # refusal says the project holds no quota there — true, and
                # unhelpful — and the advice used to lead with a quota request
                # for a region Google has never had. Asking Google for a region
                # that does not exist is a slow way to learn you mistyped.
                #
                # Deliberately not reordered against the two zone reads below,
                # which would diagnose it properly: `machine-types list
                # --zones=<bogus>` makes gcloud refuse the argument outright, so
                # moving them ahead of this would replace a clear refusal with a
                # raw gcloud error for exactly the case this is about.
                # NO `--region`, and the comment above is why. `region_of` on a
                # mistyped zone yields a region string nobody has vetted — not
                # metered, possibly not real — so the command this hands over
                # exits 2. This path cannot vet it without another API call, and
                # `quota request` with no region derives one AND checks it, which
                # is strictly more than can be done here.
                #
                # Found by sweeping for suggestions composed rather than asked
                # for, two lines below a comment about this exact trap.
                fix=(f"check the zone name first — a typo reads as a region "
                     f"this project has no quota in: gcloud compute zones list "
                     f"--filter=name={zone}; then either drop --zone and let "
                     f"this pick, or ask for the card: comfy-qat quota "
                     f"request --gpu {blueprint.card.key}"),
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
            # The `--zone` gate above, said about a region, and deliberately in
            # the same two moves: name where the grant DOES apply, then lead the
            # advice with the spelling check.
            #
            # `--region us-central9` is as easy to mistype as `--zone
            # us-central9-a`, and this branch is where the mistyped one lands
            # first. It used to answer with the bare reason and open the fix
            # with `quota request --region us-central9` — asking Google for a
            # region it has never had, which is a slow way to learn you
            # mistyped. The zone branch was fixed for exactly that and this one
            # was not, for the reason its own comment gives.
            #
            # Nothing here calls the region unreal, and nothing can: the quota
            # payload lists where the grant APPLIES, not every region Google
            # has, so `me-west1` — a real region and the docs' example of a
            # genuine gap — is absent from it entirely. The advice says "check
            # the spelling", never "no such region", and there is a test for it.
            raise LifecycleError(
                f"this project has no {blueprint.card.name} quota in {region}, so "
                f"nothing can start there. It holds {blueprint.card.name} in "
                f"{_grant_reaches(check)}. Nothing was created.",
                fix=(f"check the region name first — a typo reads as a region "
                     f"this project has no quota in: gcloud compute regions "
                     f"list --filter=name={region}; then either drop --region "
                     f"and let this pick, or ask for the card there: "
                     f"comfy-qat quota request --gpu {blueprint.card.key} "
                     f"--region {region}"),
                kind=NO_QUOTA,
            )

    return choose(
        gc, project,
        accelerator=blueprint.card.accelerator,
        machine_type=blueprint.machine_type,
        regions=regions, config=config, probe=probe, fleet=fleet,
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
