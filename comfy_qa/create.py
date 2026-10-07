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

from . import config as host_list
from . import inflight
from . import reservation as rsv
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
    def is_gpu(self) -> bool:
        """Is there a card at all? False only for `NO_GPU`, below the table.

        Asked wherever a line was written when a box and a card were the same
        thing: the driver, the accelerator flag, the GPU quota, the zones a card
        is sold in. `has_gsp` is not this question — a box with no card has no
        GSP either, and is not a card this tool cannot drive.
        """
        return bool(self.accelerator)

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

# NO GPU AT ALL, and deliberately NOT a row in the table above. `setup`,
# `quota list` and the driver tests all read `CARDS` as "the cards Google
# meters", so a row for "none" there would be looked up in the quota records
# and asked for at Google. It is a `Card` only so that everything which carries
# a blueprint's card can carry this one, and every place that must treat it
# differently asks `is_gpu`.
#
# The machine is the T4's — `n1-standard-8`, 8 vCPU and 30 GB — with nothing
# attached to it: the one machine type in this file already proved on a live
# create, and the one whose CPU quota path `quota.py` documents (N1 falls back
# to the general `CPUS-per-project-region` pool). `count=0` is the fact the
# ceiling arithmetic needs: this box holds none of the GPU allowance.
# `attached=True` reads oddly and is only there so `accelerator_flag` is None.
NO_GPU = Card("none", "none", accelerator="", machine_type="n1-standard-8",
              attached=True, architecture="", count=0, vcpus=8)

# What a person may type to mean it is NOT written here. It is
# `config.NO_GPU_WORDS`, the same set the host list is read with, looked up when
# `card_for` is asked: there were two sets, one in each module, that happened to
# be equal, and a word added to one would have made a box `create` builds as
# having no GPU and every later command reads as having a card.

# The note beside it in a menu: the three things a person choosing needs to
# know about it that the word "none" does not say.
_NO_GPU_NOTE = (f"{NO_GPU.machine_type} — no GPU; ComfyUI on the CPU, slow, and "
                f"not counted against GPU quota")


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


def os_keys() -> list[str]:
    """The `--os` spellings, in the order the menu and the help offer them.

    The sibling of `drivable_cards`, and here for the same reason: `linux` and
    `windows` were typed out in the `--os` help, in `image_for`'s refusal, in
    two fix lines and in the docs, and `IMAGES` is where they are decided. Two
    is a short enough list to look safe to repeat, which is exactly how a third
    image would get added and advertised nowhere.

    The ALIASES are deliberately not in it: `ubuntu` and `win` are spellings the
    tool accepts, not choices it offers, and a menu of seven rows for two
    operating systems is a worse menu.
    """
    return [key for key, _note in os_menu()]


def os_menu() -> list[tuple[str, str]]:
    """Each `--os` spelling and the image it means, for a prompt or a help line.

    The KEY first and the display name second, and they are not
    interchangeable: `--os` takes `linux`, and `Ubuntu 22.04` is what `list`
    shows and what discovery reads back off a box. `create.build` once
    interpolated the second into a fix line and handed somebody
    `comfy-qat create --os Ubuntu 22.04`, which Typer exits 2 on — see
    `tests/test_os_families.py`. Pairing them here keeps a caller from having to
    know which is which.
    """
    return [(key, image.os) for key, image in IMAGES.items()]


def gpu_menu() -> list[tuple[str, str]]:
    """Each `--gpu` spelling and the machine type it brings with it.

    The sibling of `os_menu`, and the note is the machine type on purpose: it
    is the thing a person cannot look up from the card name and the thing that
    decides what the box costs. `create`'s own help calls the card "the only
    real decision", and this is the fact that decision turns on.
    """
    return [(key, CARDS[key].machine_type) for key in drivable_cards()]


def gpu_choices() -> list[tuple[str, str]]:
    """Everything `create --gpu` will take: the cards, then no card at all.

    `gpu_menu` beside it stays the drivable cards and nothing else, because that
    is the question the card lists ask. This is the question the PROMPT asks —
    what may I answer — and "none" is an answer.
    """
    return [*gpu_menu(), (NO_GPU.key, _NO_GPU_NOTE)]


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


def undrivable(card: Card, image: Image | None = None,
               asked: str = "") -> LifecycleError:
    """Why a real card with real quota is still refused, before anything exists.

    Raised by `plan`, which runs offline and before the quota read, so this
    costs a second and no money. The alternative — the behaviour this replaced —
    is a created, billing instance whose GPU never initialises and a run that
    reported success.

    `asked` is what else the refused command asked for — ` --reserve`, a name —
    as `_asked_for` builds it, so the command handed back is for the same box.
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
        fix=(f"comfy-qat create --os {os_key} --gpu t4{asked}, the same n1-standard-8 "
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
    # Is the capacity held for this box, and billed, until the box is deleted?
    # The flag, the prompt's answer and a leftover reservation being resumed all
    # become this one field before anything reads it.
    reserve: bool = False

    @property
    def machine_type(self) -> str:
        return self.card.machine_type

    @property
    def reservation(self) -> str | None:
        """The reservation this box is bound to, by name. None if not reserved.

        `<name>-rsv`, always — deterministic, so a rerun of the same create
        finds what an interrupted one left behind.
        """
        return rsv.name_for(self.name) if self.reserve else None

    @property
    def reservation_accelerator(self) -> str | None:
        """The `--accelerator` value its reservation is made with, or None."""
        return _reservation_accelerator(self.card)

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

        A Linux box with no GPU gets none at all. The script is the NVIDIA
        installer, and on a machine with no NVIDIA hardware it has nothing to
        install onto.
        """
        if self.image.windows:
            return "enable-windows-ssh=TRUE"
        if not self.card.is_gpu:
            return None
        return f"startup-script={LINUX_DRIVER}"

    def steps(self, zone: str) -> list[str]:
        """The plan, in the words `--dry-run` prints and the real run follows."""
        if not self.card.is_gpu:
            # Its own wording rather than the card's with the card left out:
            # "none (), built into the machine type" is three false things.
            lines = [
                f"create {self.name} in {zone}: {self.image.os}, no GPU — ComfyUI "
                f"will run on the CPU",
                f"machine type {self.machine_type} — nothing attached to it",
                f"{self.disk_gb} GB pd-balanced boot disk from {self.image.family}",
            ]
            if self.image.windows:
                lines.append("metadata enable-windows-ssh=TRUE, so this tool can reach it")
            lines.append(f"add {self.name} to the host list on the next free port")
            return lines
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
        if self.reserve:
            # First, because it is made first — and last, the sentence about
            # what it costs, so that it is the line a person reads before
            # answering the confirmation underneath.
            return [
                f"reserve the capacity first: reservation {self.reservation} in "
                f"{zone}, which only this box can use",
                *lines,
                rsv.bill(self.name),
            ]
        return lines


def _reservation_accelerator(card: Card) -> str | None:
    """What `reservations create` is given for this card's `--accelerator`.

    THE ONE PLACE, and it is one place on purpose. The same value the instance
    create is given: `type=...,count=N` for a card attached to an N1, and
    nothing for a card that is part of the machine type (G2, A2, A3) — on the
    reasoning that `--machine-type=g2-standard-8` already says which card.

    That second half is NOT settled against Google. Whether a built-in card's
    reservation must omit `--accelerator`, may carry it or requires it has not
    been read off a live project. If it turns out to be required, this is the
    line that changes, to `f"type={card.accelerator},count={card.count}"` —
    `reservation.fits` already accepts either read-back.
    """
    return card.accelerator_flag


# --- turning what somebody typed into a blueprint --------------------------


def card_for(gpu: str) -> Card:
    """`l4` -> the L4 card. Anything else names what there is."""
    key = (gpu or "").strip().lower().replace("_", "-").replace(" ", "")
    key = key.removeprefix("nvidia-").removeprefix("tesla-")
    if key in CARDS:
        return CARDS[key]
    if key in host_list.NO_GPU_WORDS:
        return NO_GPU
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


def _asked_for(reserve: bool, name: str | None) -> str:
    """` --reserve` and ` --name <name>`, for a remedy `plan` hands back.

    A REMEDY THAT REWRITES THE COMMAND REWRITES THE BOX. Each refusal in `plan`
    prints a corrected `comfy-qat create`, and each of them printed it without
    `--reserve`: pasted back under `--yes`, the fix for "that disk is too
    small" was a different box from the one asked for, with a different bill.
    The name goes with it for a reserved box, because a reservation is found
    again by its box's name.

    Nothing at all for a build that is not reserved, so those lines stay what
    they have always been. The name only when one was given: with none, one is
    picked, and it is picked again.
    """
    if not reserve:
        return ""
    return " --reserve" + (f" --name {_clean(name)}" if name else "")


def image_for(os_choice: str, asked: str = "") -> Image:
    """`linux` or `windows`, plus the spellings people actually type."""
    key = (os_choice or "").strip().lower()
    key = ALIASES.get(key, key)
    if key in IMAGES:
        return IMAGES[key]
    raise LifecycleError(
        f"no operating system called {os_choice!r}. Say --os linux or --os windows.",
        fix=f"comfy-qat create --os linux --gpu l4{asked}",
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
    reserve: bool = False,
) -> Blueprint:
    """Everything decided before anything is contacted. Offline, and total."""
    asked = _asked_for(reserve, name)
    image = image_for(os_choice, asked)
    card = card_for(gpu)
    # BEFORE the disk checks, and long before the quota read: a card this tool
    # cannot drive is not a detail of the box, it is the box. `plan` is the last
    # thing that runs while a create is still free.
    #
    # `card.is_gpu and`, because no card at all has no GSP either and is not a
    # card this tool cannot drive — there is no driver in a box with no GPU.
    if card.is_gpu and not card.has_gsp:
        raise undrivable(card, image, asked)
    if reserve and not card.is_gpu:
        # The limit on reservations is counted in GPU cards: it is the project's
        # GPU allowance. A reservation that holds no card is bounded by nothing
        # this tool reads, and would bill at machine rate with no ceiling on
        # how many of them a typo could make.
        raise LifecycleError(
            "a box with no GPU cannot be reserved. A reservation is held "
            "against the project's GPU allowance, and this box uses none of it. "
            "Nothing was created.",
            fix=output.fix(
                "make it without a reservation:",
                # Not reserved, on purpose: that is the alternative on offer.
                # The name is still the one asked for.
                f"comfy-qat create --os {image.key} --gpu {card.key}"
                f"{asked.removeprefix(' --reserve')}",
                "or reserve a box that has a card:",
                f"comfy-qat create --os {image.key} --gpu t4{asked}",
            ),
            kind=CREATE_FAILED,
        )
    if disk_gb < MIN_DISK_GB:
        raise LifecycleError(
            f"a {disk_gb} GB disk is too small — the image will not fit and models "
            f"will not either. Ask for at least {MIN_DISK_GB}.",
            fix=(f"comfy-qat create --os {image.key} --gpu {gpu}{asked} "
                 f"--disk {DEFAULT_DISK_GB}"),
            kind=CREATE_FAILED,
        )
    if disk_gb > MAX_DISK_GB:
        raise LifecycleError(
            f"a {disk_gb} GB disk is larger than anything this tool creates. The disk "
            f"bills by the gigabyte provisioned, from the moment the box exists and "
            f"whether or not anything is written to it, so a typo here is expensive "
            f"and silent. Ask for at most {MAX_DISK_GB}, or make a disk that size "
            f"deliberately in the console.",
            fix=(f"comfy-qat create --os {image.key} --gpu {gpu}{asked} "
                 f"--disk {DEFAULT_DISK_GB}"),
            kind=CREATE_FAILED,
        )
    chosen = choose_name(name, image, taken or set())
    if reserve and len(chosen) > rsv.MAX_BOX_NAME:
        # Offline, like every other name rule here. Google's names stop at 63
        # characters and the reservation is `<name>-rsv`, so the box may use 59
        # of them. Found at Google instead, this costs the quota read, the zone
        # ranking and a confirmation, and is answered about a reservation name
        # the user never typed.
        raise LifecycleError(
            f"{chosen} is {len(chosen)} characters, and the name of a reserved box "
            f"may be at most {rsv.MAX_BOX_NAME}: its reservation is called "
            f"{chosen}{rsv.SUFFIX}, and Google's names stop at {MAX_NAME_LEN}. "
            f"Nothing was created.",
            fix="pick a shorter one with --name, or drop --name and one is "
                "picked for you",
            kind=CREATE_FAILED,
        )
    return Blueprint(name=chosen, image=image, card=card, disk_gb=disk_gb,
                     reserve=reserve)


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
    reserved: tuple[tuple[str, str, int, str], ...] = ()
    """`(name, zone, cards, box)` for each reservation holding a card.

    From the LIVE list, never the host file: a reservation made in the console,
    or left by a create that stopped half-way, holds the allowance just the
    same. `box` is "" for one this tool did not make. A leftover being resumed
    by this very create is not in here — it is the card this box is about to
    use, not a card in its way.
    """
    reserved_cards: int = 0
    """Cards those reservations hold between them. Part of `held`, not beside it.

    `held` is everything spoken for: these, plus the cards on running boxes
    that are not consuming one of these. A reserved box that is running is its
    reservation's card, once.
    """
    boxed: "tuple[tuple[str, str, str], ...] | None" = ()
    """`(reservation, zone, instance)` for every instance bound to one of them.

    What decides the remedy, with `named` below. A reservation with a box on
    it is not released from under that box; one with nothing bound to it is
    released with Google's own command. None when the instances were not read.
    """
    named: tuple[tuple[str, str, str], ...] = ()
    """`(reservation, zone, label)` where the host list holds that box.

    The only thing that lets the remedy say `comfy-qat down <label>`. See
    `declared_boxes`.
    """
    reserving: bool = False
    """Is this create going to make a reservation of its own?"""
    unread: bool = False
    """Reserving, and the project's reservations could not be read.

    Not the same as read-and-empty, and the difference is a refusal: how many
    cards are already held is then not known, a refusal is free, and a
    reservation past the limit bills until somebody notices it.
    """
    reusing: tuple[str, str] | None = None
    """`(name, zone)` of a reservation left by an earlier run and being resumed."""
    project: str = ""
    box: str = ""
    regional: tuple[tuple[str, int], ...] = ()
    """`(region, limit)`: this card's own allowance, region by region.

    The per-card half of the limit. `card_limit` is the best of these, which is
    the right number to print and the wrong one to place a box by — a grant of
    1 in each of two regions is one card in each, not one anywhere.
    """

    def lines(self) -> list[str]:
        """What was checked and what it said — printed by a dry run and a real one."""
        from .quota import UNLIMITED
        from .quota import where_label as _where_label

        def amount(value: int | None) -> str:
            if value is None:
                return "not granted"
            # `== UNLIMITED`, not `< 0`. Correct either way, and spelled as a
            # magnitude comparison it is the one idiom this whole class is about
            # — invisible to the sweep, and the next edit here has no way to know
            # the sentinel is what the branch is for.
            return "unlimited" if value == UNLIMITED else str(value)

        def ceiling(value: int | None) -> str:
            # None here is not the same None as the card's. A card the project
            # holds nothing for reports no record, and that is a refusal. The
            # project-wide ceiling always exists, so no record means it was not
            # read — which does not gate anything, and must not read as "zero".
            if value is None:
                return "not reported by this project"
            return "unlimited" if value == UNLIMITED else str(value)

        out = [
            f"{self.card}: {amount(self.card_limit)}"
            # L14: the SAME phrasing `quota list` prints, from the same
            # function — "in 43 region(s)" beside that table's own label for the
            # identical span was the third vocabulary for one geography.
            + (f", in {_where_label(self.regions)}" if self.regions else ""),
            f"GPUS_ALL_REGIONS (every card, project-wide): {ceiling(self.global_limit)}",
        ]
        if self.reserved:
            cards = _cards(self.reserved_cards)
            out.append(f"reserved, and held whether its box runs or not: {cards} of "
                       f"it — {self._reserved_names}")
        if self.running:
            # The running boxes that are NOT on one of those reservations. With
            # no reservations this is `held`, as it always was.
            cards = _cards(self.held - self.reserved_cards)
            out.append(f"already running and holding {cards} of it: "
                       f"{', '.join(self.running_names)}")
        if self.reusing is not None:
            out.append(f"reusing reservation {self.reusing[0]} in {self.reusing[1]} — "
                       f"left by an earlier run, and already holding its card")
        elif self.reserving and not self.unread and self._fits:
            # Only when it is going to happen. Above a refusal, "reserving
            # takes 1 of the 1" describes a reservation nobody is making.
            out.append(self._reserving)
        return out

    @property
    def _reserved_names(self) -> str:
        """`a-rsv (us-central1-a), b-rsv (us-east1-b)`."""
        return ", ".join(f"{name} ({zone})" for name, zone, _cards_held, _box
                         in self.reserved)

    @property
    def _fits(self) -> bool:
        """Is there room under the ceiling for what this create needs?"""
        from .quota import meets

        return self.global_limit is None or meets(self.global_limit,
                                                   self.held + self.needed)

    @property
    def _reserving(self) -> str:
        """What reserving takes out of the ceiling, and what that leaves.

        Said on success and under `--dry-run`, because it is the consequence
        nobody expects: on a ceiling of 1 a reserved box is the whole GPU
        allowance for as long as it exists, running or not.
        """
        from .quota import UNLIMITED

        box = self.box or "this box"
        took = _cards(self.needed)
        if self.global_limit is None or self.global_limit == UNLIMITED:
            return (f"reserving holds {took} from the moment it is made until "
                    f"{box} is deleted, running or stopped")
        # The `is not None` is the line above, said again where the arithmetic
        # is: a subtraction on a limit carries its own guard, so the next edit
        # to the early return cannot leave this one reading a None.
        left = (self.global_limit - self.held - self.needed
                if self.global_limit is not None else 0)
        already = f", with {self.held} already held" if self.held else ""
        head = f"reserving takes {self.needed} of the {self.global_limit}{already} — "
        if left <= 0 and self.reserved_cards:
            # Another reserved box is the exception to "no other GPU box can
            # start": its own reservation holds its card, running or not.
            return (f"{head}none left. While {box} exists no GPU box without a "
                    f"reservation can start — a box with a reservation of its own "
                    f"can still start.")
        if left <= 0:
            return (f"{head}none left. While {box} exists no other GPU box can "
                    f"start, including a stopped one you already have.")
        return f"{head}{left} left for other GPU boxes while {box} exists."

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
            # rather than printing a placeholder that implies there is.
            #
            # WHAT IT DOES NOT SAY IS WHY. `elsewhere` is metered AND STOCKED
            # minus refused, so empty has two causes — metered nowhere else, or
            # metered widely and stocked nowhere else — and this sentence used to
            # assert the first. A card metered in forty-three regions and sold in
            # none of the other forty-two hits this branch, and "the only region
            # this project meters it in" is false about it. Naming the outcome
            # rather than a cause the message cannot distinguish.
            return (f"Google already refused {self.card} in {refused}, and no "
                    f"other region both meters it and sells it — so there is "
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
        from .quota import UNLIMITED

        # AN UNLIMITED GRANT IS NEVER SHORT. `card_limit > 0` excluded `-1` by
        # accident — the sentinel is not a small number, and the next reader
        # would have had to work that out from the arithmetic.
        if (self.card_limit is not None and self.card_limit != UNLIMITED
                and 0 < self.card_limit and self.card_limit < self.needed):
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
        if (self.global_limit is not None and self.global_limit != UNLIMITED
                and self.global_limit < self.needed):
            return LifecycleError(
                f"GPUS_ALL_REGIONS is {self.global_limit} on this project — that is the "
                f"ceiling across every card, whatever the {self.card} grant says, and "
                f"{self.needed} is needed. Nothing was created.",
                fix="comfy-qat quota request --gpu l4 --region us-central1 asks for "
                    "a card; raising the project-wide ceiling is a separate request at "
                    "https://console.cloud.google.com/iam-admin/quotas",
                kind=NO_QUOTA,
            )
        if (self.reserved and self.global_limit is not None
                and self.global_limit != UNLIMITED
                and self.global_limit < self.needed + self.reserved_cards):
            # BEFORE the "already running" branch, and a different remedy. That
            # one says "stop the one you are not using", which is right when a
            # running box holds the ceiling and useless here: a reservation
            # holds its card whether its box is running or stopped. Reached only
            # when the reserved cards ALONE leave no room — when stopping a
            # plain running box would be enough, the branch below says so.
            count = len(self.reserved)
            return LifecycleError(
                f"GPUS_ALL_REGIONS is {self.global_limit} on this project, and "
                f"{self.reserved_cards} of it is held by {count} "
                f"reservation{'' if count == 1 else 's'}: {self._reserved_names}. "
                f"A reservation holds its card whether its box is running or "
                f"stopped, so stopping a box frees nothing, and {self.needed} more "
                f"is needed. Nothing was created.",
                fix=_how_to_release(self.reserved, self.boxed, self.project,
                                    self.named),
                kind=NO_QUOTA,
            )
        if (self.running and self.global_limit is not None
                and self.global_limit != UNLIMITED
                and self.global_limit < self.needed + self.held):
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
                f"boxes are already running on it, holding "
                f"{self.held - self.reserved_cards} of it between "
                f"them: {', '.join(self.running_names)}. Nothing was created.",
                fix=f"{first} — stop the ones you are not using, then run this again",
                kind=NO_QUOTA,
            )
        if self.unread:
            # After everything that was READ, by the rule at the top of this
            # function: a running box holding the ceiling is a fact, and it
            # refuses whether or not the reservations could be listed. What is
            # left is a create about to reserve against a number nobody has.
            listing = "gcloud compute reservations list"
            return LifecycleError(
                "could not read this project's reservations, so how many it "
                "already holds is not known. Nothing was reserved and nothing was "
                "created.",
                fix=(f"{listing} --project={self.project}" if self.project
                     else listing),
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


def _cards(count: int) -> str:
    """`1 card`, `8 cards`."""
    return "1 card" if count == 1 else f"{count} cards"


def _how_to_release(holders, boxed, project: str, named=()) -> str:
    """The fix for "a reservation holds the card": how to let go of one.

    Four cases, and which applies is a fact about the project AND the host
    list, not a choice of wording:

      * a box is on it and is in the host list — `comfy-qat down` then
        `comfy-qat delete`, under the ENTRY'S OWN LABEL, because `delete`
        refuses a box that is running and takes the reservation with the box;
      * a box is on it and is in no entry — Google's commands for the box and
        then the reservation;
      * nothing is on it — Google's command for the reservation;
      * nobody could look — say so, and how to look, before the command.

    "ON IT" MEANS BOUND TO IT, by the instance's own record. It used to mean
    "the instance NAMED in the reservation's description is bound to it", so a
    reservation described as held for one box with a differently named box
    running on it was told "nothing is on it" and handed the command that
    releases the capacity from under that box. `boxed` is `_boxed`'s answer:
    `(reservation, zone, instance)` for every instance bound, whatever it is
    called — or None when the instances were not read, which is not "none".

    `named` is `(reservation, zone, label)`, from `declared_boxes`. It is the
    ONLY thing that puts a `comfy-qat` command in this fix. A host list is a
    hand-maintained file: the box may not be in it, and then both commands exit
    2 while the reservation bills. Worse, the list may hold an entry of that
    name for a DIFFERENT machine, and `comfy-qat delete <name>` pasted back
    deletes that one. So with nothing in `named`, which is what a caller that
    knows no host list passes, no `comfy-qat` command is printed.
    """
    labels = {(name, zone): label for name, zone, label in named}
    unread = boxed is None
    on: dict[tuple[str, str], list[str]] = {}
    for found in boxed or ():
        # Pairs are what this took before it asked the right question. One of
        # those carries no instance name, so the box is unknown rather than
        # assumed to be the one in the description.
        name, zone, *instance = found
        on.setdefault((name, zone), []).extend(instance)

    def box_commands(name: str, zone: str) -> list[str]:
        # `--delete-disks=all` IS NOT OPTIONAL, and it is in the same string as
        # the command so the two cannot be parted. This tool makes boot disks
        # that do not auto-delete: without the flag the box goes, the card is
        # freed, and 200 GB bills on with nothing attached to it. `delete` and
        # `remove.py`'s own printed command both carry it.
        #
        # AND `--quiet`, for the reason the release command under it carries
        # one. gcloud asks before deleting; pasted where nobody can answer — a
        # script, an agent — it exits 1 having deleted nothing, and the box, its
        # disk and its reservation all go on billing behind an error.
        where = f"--zone={zone} --project={project}" if project else f"--zone={zone}"
        # One line, flags and all, so a test can hold the three together.
        gone = "gcloud compute instances delete {box} {where} --delete-disks=all --quiet"
        return [*(gone.format(box=box, where=where) for box in on[(name, zone)]),
                rsv.delete_command(name, zone, project)]

    look = "gcloud compute instances list"
    look = f"{look} --project={project}" if project else look

    if len(holders) == 1:
        name, zone, _held, box = holders[0]
        if (name, zone) in labels:
            label = labels[(name, zone)]
            return output.fix("stop it, then delete it to release the card:",
                              f"comfy-qat down {label}",
                              f"comfy-qat delete {label}")
        if on.get((name, zone)):
            there = " and ".join(on[(name, zone)])
            return output.fix(
                f"{there} is on it and is not in your host list, so this tool "
                f"cannot name it. Check whose it is, then delete the box and "
                f"release the reservation with Google's own commands:",
                *box_commands(name, zone))
        if unread or (name, zone) in on:
            return output.fix(
                "whether a box is on it could not be read, so look before "
                "releasing it — a reservation released from under a running box "
                "takes its capacity away:",
                look,
                rsv.delete_command(name, zone, project))
        why = ("nothing is on it" if box else
               "nothing is on it, and it was not made by this tool, so check "
               "whose it is first")
        return output.fix(f"release it — {why}:", rsv.delete_command(name, zone, project))
    lines = ["release one of them. A box in your host list is stopped and then "
             "deleted, and its reservation goes with it; anything else is "
             "released with Google's own commands — check whose it is first:"]
    if unread:
        lines = ["release one of them — but which have a box on them could not "
                 "be read, so look first:", look]
    for name, zone, _held, _box in holders:
        if (name, zone) in labels:
            label = labels[(name, zone)]
            lines += [f"comfy-qat down {label}", f"comfy-qat delete {label}"]
        elif on.get((name, zone)):
            lines += box_commands(name, zone)
        else:
            lines.append(rsv.delete_command(name, zone, project))
    return output.fix(*lines)


def _boxed(holders, instances: list[dict] | None,
           ) -> "tuple[tuple[str, str, str], ...] | None":
    """`(reservation, zone, instance)` for every instance bound to one of these.

    THE QUESTION IS "IS ANYTHING BOUND TO IT", asked of each instance's own
    record with `reservation.bound_to` — the question `list --live` and
    `down --all` ask. Not "is the instance named in its description bound to
    it": a box renamed, re-made or bound by hand in the console is on the
    reservation just the same, and a remedy that misses it releases the
    capacity from under a running machine.

    None when `instances` is None. Not read is not "nothing bound", and the
    caller must not say "nothing is on it" about a project nobody looked at.

    A fact about the PROJECT. Whether this tool can name a box is a different
    one — see `declared_boxes`.
    """
    if instances is None:
        return None
    wanted = {(name, zone) for name, zone, _held, _box in holders}
    return tuple((rsv.bound_to(instance), rsv.zone_of(instance),
                  instance.get("name") or "")
                 for instance in instances
                 if (rsv.bound_to(instance), rsv.zone_of(instance)) in wanted
                 and instance.get("name"))


def declared_boxes(holders, instances: list[dict] | None, hosts, project: str,
                   ) -> tuple[tuple[str, str, str], ...]:
    """`(reservation, zone, label)` for each holder whose box the host list holds.

    "Its box" is the instance BOUND to it, as `_boxed` finds it — not the name
    in the description. "Holds" is the host list's own rule for one machine: an
    entry whose `machine_id` — kind, project, zone, instance — is that
    instance. Never the entry's name. The label returned is what the user
    called the machine, which is what `comfy-qat down` and `delete` take, and
    it need not be the instance's name at all.

    Empty when there is no host list to ask (`None`), no project to complete
    an identity with, or no instances read: an identity that cannot be
    established is not a match. A reservation with more than one instance on
    it is not named either — one `comfy-qat delete` would not empty it.
    """
    if not hosts or not project:
        return ()
    bound: dict[tuple[str, str], list[str]] = {}
    for name, zone, instance in _boxed(holders, instances) or ():
        bound.setdefault((name, zone), []).append(instance)
    found = []
    for (name, zone), boxes in bound.items():
        if len(boxes) != 1:
            continue
        wanted = ("gce", project, zone, boxes[0])
        label = next((host.name for host in hosts if host.machine_id == wanted), None)
        if label:
            found.append((name, zone, label))
    return tuple(found)


def _reusable(card: Card, leftover) -> "rsv.Reservation | None":
    """The leftover, if a box of this card can be put on it. None otherwise.

    One question asked in three places — the limit, the zone order and the
    build — so that a reservation is "this create's own" by the same test
    wherever it matters. A leftover that fails it is somebody's reservation
    that happens to carry this name, and it is counted, refused and left alone
    like any other.
    """
    if leftover is None:
        return None
    fits = rsv.fits(leftover, machine_type=card.machine_type,
                    accelerator=_reservation_accelerator(card))
    return leftover if fits else None


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
            zone = rsv.zone_of(instance)
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
    from functools import reduce

    from .quota import allowance, better_limit, regions_with_quota

    # `reduce(better_limit, ...)` rather than a hand-rolled `UNLIMITED in ...`
    # ahead of a `max`. Correct either way — this was the one site of the class
    # that already checked the sentinel — but the check lived in a statement
    # above the comparison it protects, which is the arrangement that hid the
    # other nine. One predicate, asked where the question is.
    limits = [allowance(name, quotas) for name in card.quota_names]
    granted = [value for value in limits if value is not None]
    limit: int | None = reduce(better_limit, granted) if granted else None

    regions: set[str] = set()
    for name in card.quota_names:
        regions |= set(regions_with_quota(name, quotas))
    return limit, sorted(regions)


def check_quota(card: Card, quotas: list[dict], instances: list[dict],
                asked_region: str = "", *,
                preferences: list[dict] | None = None,
                askable: "list[str] | None" = None,
                reservations: "list[rsv.Reservation] | None" = None,
                reserve: bool = False,
                leftover: "rsv.Reservation | None" = None,
                project: str = "", box: str = "",
                hosts: "list[Host] | None" = None) -> QuotaCheck:
    """Read the allowance. Pure — the caller does the gcloud reads.

    `preferences` is OPTIONAL and `None` means "could not be read", which must
    not block a create: it only ever removes advice, never adds a refusal.

    `reservations` is the project's live reservations, parsed, and `None` means
    NOT READ — which is not `[]`, read and empty. Not read, the count below is
    the running cards only: an under-count by exactly the reserved boxes that
    are stopped. A plain create goes ahead on that (the caller says so, and
    Google still refuses what does not fit); a create that is itself about to
    reserve does not, and `problem()` refuses it.

    `leftover` is a reservation an earlier run of this same create left behind.
    When this create is reserving and can use it, it is not counted: it is the
    card this box is about to sit on. `project` and `box` only complete the
    sentences — the command that releases a reservation, and the name in the
    line about what reserving leaves.

    `hosts` is the declared host list, and what it buys is one thing: a remedy
    may say `comfy-qat down <label>` / `comfy-qat delete <label>` about a
    reserved box only when an entry here IS that machine (project, zone,
    instance), and then by that entry's label. Not passed, or with no
    `project`, the remedy is Google's own commands.
    """
    from functools import reduce

    from .quota import allowance, better_limit, global_allowance

    limit, regions = card_grant(card, quotas)

    reuse = _reusable(card, leftover) if reserve else None
    counted = reservations
    if counted is not None and reuse is not None:
        counted = [found for found in counted
                   if (found.name, found.zone) != (reuse.name, reuse.zone)]
    holders = rsv.holders(counted or [])

    regional = []
    for region in regions:
        granted = [value for value in
                   (allowance(name, quotas, region=region) for name in card.quota_names)
                   if value is not None]
        if granted:
            regional.append((region, reduce(better_limit, granted)))

    return QuotaCheck(
        card=card.name,
        card_limit=limit,
        global_limit=global_allowance(quotas),
        needed=card.count,
        # The boxes whose cards are NOT already counted through a reservation.
        # Every non-TERMINATED GPU box when there are no reservations, or none
        # were read — which is what this always was.
        running=tuple(_gpu_boxes_running(rsv.outside(instances, counted))),
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
        # CARDS, and all of them: reserved, running or not, plus the running
        # cards outside any reservation. One function, the same one `switch` and
        # `move` count with.
        held=rsv.cards_held(instances, counted),
        reserved=holders,
        reserved_cards=sum(cards for _name, _zone, cards, _box in holders),
        boxed=_boxed(holders, instances),
        named=declared_boxes(holders, instances, hosts, project or ""),
        reserving=reserve,
        unread=reserve and reservations is None,
        reusing=(reuse.name, reuse.zone) if reuse is not None else None,
        project=project or "",
        box=box or "",
        regional=tuple(regional),
    )


# --- the allowance a box with no GPU actually spends -------------------------


@dataclass(frozen=True)
class CpuCheck:
    """What the project's CPU allowance says about a box with no GPU.

    The sibling of `QuotaCheck`, with the same three things a caller reads —
    `lines()`, `problem()` and `regions` — and none of its inputs. GPU quota
    and the GPU ceiling are not read into this at all: a box with no card
    spends neither, and routing it through the card's gate is how a project
    with one L4 in use came to refuse a machine that needs no GPU.

    Everything here is a LIMIT. The quota records carry no usage, so `200` is
    what the project may hold in a region and not what is free in it, and the
    output says so in as many words. Google's own refusal at the create is
    what catches a pool that is actually full, and nothing bills when it does.
    """

    machine_type: str
    needed: int
    # The largest allowance any one region reports. None when the project
    # reports no CPU quota record for this machine at all — which is "not
    # read", and is neither a refusal nor a zero.
    limit: int | None
    # `CPUS-ALL-REGIONS-per-project`. None when not reported; never gates then.
    ceiling: int | None
    # Regions whose allowance has room for the machine. Empty when `limit` is
    # None, and the caller then searches everywhere the machine is sold.
    regions: tuple[str, ...] = ()
    quota_id: str = ""

    @property
    def family(self) -> str:
        """`n1`, as the quota is named for people."""
        return self.machine_type.partition("-")[0]

    def lines(self) -> list[str]:
        """What was checked and what it said — printed by a dry run and a real one."""
        from .quota import UNLIMITED
        from .quota import where_label as _where_label

        def amount(value: int | None) -> str:
            return "unlimited" if value == UNLIMITED else str(value)

        out = ["GPU quota: not used — this box has no GPU"]
        if self.limit is None:
            out.append(f"CPUS ({self.family}): not reported by this project, so it "
                       f"was not checked — Google decides at the create")
        else:
            where = f" in {_where_label(self.regions)}" if self.regions else ""
            out.append(f"CPUS ({self.family}): {amount(self.limit)}{where} — a limit, "
                       f"not what is free")
        if self.ceiling is None:
            out.append("CPUS_ALL_REGIONS (every machine, project-wide): not reported "
                       "by this project")
        else:
            out.append(f"CPUS_ALL_REGIONS (every machine, project-wide): "
                       f"{amount(self.ceiling)} — a limit, not what is free")
        out.append(f"{self.machine_type} needs {self.needed} vCPU")
        return out

    def problem(self) -> LifecycleError | None:
        """The reason this cannot be created, or None. Nothing has happened yet.

        Only what was READ can refuse. A limit that was not reported does not
        gate, exactly as the GPU ceiling does not when it is not reported — and
        the comparisons go through `meets`, because an unlimited allowance is
        -1 and `-1 >= 8` is False.
        """
        from .quota import meets

        raise_it = ("raise it at https://console.cloud.google.com/iam-admin/quotas "
                    "— the quota is ")
        if self.ceiling is not None and not meets(self.ceiling, self.needed):
            return LifecycleError(
                f"CPUS_ALL_REGIONS is {self.ceiling} on this project — the ceiling "
                f"on vCPU across every region — and {self.machine_type} needs "
                f"{self.needed} vCPU. Nothing was created.",
                fix=f"{raise_it}CPUS-ALL-REGIONS-per-project",
                kind=NO_QUOTA,
            )
        if self.limit is not None and not self.regions:
            return LifecycleError(
                f"{self.machine_type} needs {self.needed} vCPU, and the most this "
                f"project may hold in any one region is {self.limit}. Nothing was "
                f"created.",
                fix=f"{raise_it}{self.quota_id}",
                kind=NO_QUOTA,
            )
        return None


def check_cpu(card: Card, quotas: list[dict]) -> CpuCheck:
    """Read the CPU allowance for a box with no GPU. Pure, like `check_quota`.

    `quotas` is `Gcloud.compute_quotas(project)` — every compute quota record,
    not the GPU subset `gpu_quotas` filters it down to, which holds no CPU
    quota at all.
    """
    from .quota import cpu_allowance, cpu_ceiling, cpu_regions, cpu_target

    target = cpu_target(card.machine_type, quotas)
    return CpuCheck(
        machine_type=card.machine_type,
        needed=card.vcpus,
        limit=cpu_allowance(card.machine_type, quotas),
        ceiling=cpu_ceiling(quotas),
        regions=tuple(cpu_regions(card.machine_type, quotas, card.vcpus)),
        quota_id=target.quota_id if target is not None else "",
    )


# --- the part that spends money -------------------------------------------


def create_in(gc: Gcloud, blueprint: Blueprint, zone: str, project: str) -> None:
    """One attempt, in one zone. Raises GcloudError exactly as gcloud refused it.

    An ordinary GPU box is created with the six keywords it always was. The two
    newer ones are passed only for the boxes that need them, which is not
    thrift: every double of `create_instance_from_image` in the suite is a
    second site, and a keyword sent on every call is a keyword each of them has
    to have been told about.
    """
    extra: dict = {}
    if not blueprint.card.is_gpu:
        # `--maintenance-policy=TERMINATE` is for a machine that cannot
        # live-migrate, which is a machine with a GPU.
        extra["terminate_on_maintenance"] = False
    if blueprint.reservation:
        extra["reservation"] = blueprint.reservation
    gc.create_instance_from_image(
        blueprint.name, zone, project,
        machine_type=blueprint.machine_type,
        image_family=blueprint.image.family,
        image_project=blueprint.image.project,
        disk_gb=blueprint.disk_gb,
        accelerator=blueprint.card.accelerator_flag,
        metadata=blueprint.metadata,
        **extra,
    )


def _as_reserved(blueprint: Blueprint) -> str:
    """` --reserve --name <name>` for a reserved build, and nothing otherwise.

    For any remedy that hands back a `comfy-qat create`: the box that was asked
    for was a reserved one, and a command that drops that is a different box.
    """
    return f" --reserve --name {blueprint.name}" if blueprint.reserve else ""


def _same_box(blueprint: Blueprint) -> str:
    """`comfy-qat create` for this box, complete up to where it goes.

    Reserved and named when the box is; the plain command when it is not.
    """
    return (f"comfy-qat create --os {blueprint.image.key} --gpu {blueprint.card.key}"
            f"{_as_reserved(blueprint)}")


def _wanted(card: Card) -> str:
    """What a zone is out of, as a word for a sentence: the card, or the machine.

    A stockout for a box with no GPU is a stockout of its machine type, and
    "no none free" names a card that does not exist.
    """
    return card.name if card.is_gpu else card.machine_type


def _thing(card: Card) -> str:
    """`the card` or `the machine`, for the same sentences."""
    return "the card" if card.is_gpu else "the machine"


def _instance_in_flight(blueprint: Blueprint, zone: str, project: str):
    """The registration around the one call that can bring a billing box into being.

    The wording is the OSError branch's in `create_cmd`, which has said the
    right thing about this exact state since before anything could reach it:
    the box is not in the host list, so `comfy-qat down` cannot reach it and
    the raw gcloud stop is the only thing that works.
    """
    return inflight.may_leave(
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
    )


def _refuse_the_leftover(blueprint: Blueprint, leftover, project: str) -> None:
    """A reservation under this box's name that this box cannot be put on.

    Not ours, the wrong shape, more than one machine, or already in use. It is
    refused rather than worked around: making the box unreserved is not what
    was asked for, a second reservation cannot share the name, and releasing
    something this tool did not make is not this tool's to do.
    """
    raise LifecycleError(
        f"a reservation called {leftover.name} is already on this project in "
        f"{leftover.zone}, and {blueprint.name} cannot be put on it: it was not "
        f"made by this tool for a box of this shape, or something is already "
        f"using it. Nothing was created.",
        fix=output.fix(
            "call this box something else:",
            f"comfy-qat create --os {blueprint.image.key} --gpu {blueprint.card.key} "
            f"--reserve --name <another-name>",
            "or release that reservation, if it is yours to release:",
            rsv.delete_command(leftover.name, leftover.zone, project),
        ),
        kind=CREATE_FAILED,
    )


def _reserve_and_create(gc: Gcloud, blueprint: Blueprint, zone: str, project: str,
                        say, reuse) -> None:
    """The reservation, then the box on it. One zone, and never a second.

    THE ORDER IS THE POINT. The reservation is made first because it is what
    holds the capacity: an instance made first could not be bound to a
    reservation that does not exist yet, and one made unbound would be an
    ordinary box sitting beside a reservation nothing is using.

    And it is the half that costs. From the moment `create_reservation` returns
    until the reservation is deleted, Google bills for it at the card's rate
    with or without a machine on it — so every exit from this function accounts
    for it out loud:

      * the reservation is refused for lack of capacity -> raised as it came,
        and the caller tries the next zone. Nothing was made.
      * it is refused for any other reason -> `_reservation_unaccounted`.
      * the box is refused -> `_box_unaccounted`, which finds out whether the
        box exists before it releases anything.

    The registration stays open across BOTH calls. Ctrl-C between them — the
    reservation exists, the box does not — is the one moment nothing else in
    this tool knows a reservation is there.

    Raises `GcloudError` only for a stockout at the reservation. Everything
    else is a `LifecycleError` that already says what is left.
    """
    name = blueprint.reservation
    with inflight.may_leave(
        f"the reservation {name} in {zone}",
        undo=["release it:", rsv.delete_command(name, zone, project)],
        note="it bills until deleted, with or without a box",
    ):
        if reuse is not None:
            say(f"  reusing reservation {name} in {zone} — left by an earlier run")
        else:
            say(f"  reserving {name} in {zone}")
            try:
                gc.create_reservation(
                    name, zone, project,
                    machine_type=blueprint.machine_type,
                    accelerator=blueprint.reservation_accelerator,
                    description=rsv.describe_for(blueprint.name),
                )
            except GcloudError as exc:
                # A STOCK-OUT IS READ BACK TOO. "Nothing was reserved" was a
                # belief about Google, true in every test because the fakes
                # made it so. One read turns it into a reading: only Google's
                # flat "no such reservation" lets this fall through to the next
                # zone and, at the end, say nothing is billing. If it is there
                # or Google will not say, it is still billing, and moving on
                # would abandon it and make a second one.
                if is_capacity_failure(exc.raw) and _nothing_was_reserved(
                        gc, blueprint, zone, project):
                    raise
                _reservation_unaccounted(gc, blueprint, zone, project, exc)
        say(f"  creating {blueprint.name} on it")
        try:
            with _instance_in_flight(blueprint, zone, project):
                create_in(gc, blueprint, zone, project)
        except GcloudError as exc:
            _box_unaccounted(gc, blueprint, zone, project, exc, say)


def _nothing_was_reserved(gc: Gcloud, blueprint: Blueprint, zone: str,
                          project: str) -> bool:
    """Does Google say, flatly, that the reservation is not there?

    False for "it is there" and for "Google would not say" alike.
    """
    try:
        return gc.reservation_absent(blueprint.reservation, zone, project) is True
    except GcloudError:
        return False


def _reservation_unaccounted(gc: Gcloud, blueprint: Blueprint, zone: str,
                             project: str, exc: GcloudError) -> None:
    """`reservations create` raised, and it was not a stockout. Is one there?

    A create that raised is not a create that did nothing: a timeout or a
    dropped connection loses the ANSWER, and the request may have landed. So
    Google is asked, and only its flat "no such reservation" is taken to mean
    nothing was made. Anything else — it is there, or Google would not say — is
    reported as a reservation that is still billing, because that is the answer
    that costs money to be wrong about.

    Nothing is released here. A reservation this function did not see being
    made is not one it can account for, and the rerun finds it and uses it.
    """
    name = blueprint.reservation
    release = rsv.delete_command(name, zone, project)
    try:
        absent = gc.reservation_absent(name, zone, project)
    except GcloudError:
        absent = None
    if absent is True:
        raise LifecycleError(
            f"Google refused to reserve {name} in {zone}: {exc}. Nothing was "
            f"created and nothing is billing.",
            fix=output.fix(exc.fix,
                           f"gcloud compute reservations list --project={project}"
                           f"   # to see for yourself that nothing was left"),
            kind=CREATE_FAILED,
        ) from exc
    found = ("the reservation is on the project anyway" if absent is False else
             "whether the reservation was made could not be checked")
    raise LifecycleError(
        f"Google refused to reserve {name} in {zone}: {exc} — but {found}, and a "
        f"reservation is still billing whether or not it has a box. "
        f"{blueprint.name} was not created.",
        fix=output.fix(
            "look:",
            f"gcloud compute reservations list --project={project}",
            "release it:",
            release,
            "or run the same create again — it finds a reservation an earlier "
            "run left and puts the box on it",
        ),
        kind=CREATE_FAILED,
    ) from exc


def _box_unaccounted(gc: Gcloud, blueprint: Blueprint, zone: str, project: str,
                     exc: GcloudError, say) -> None:
    """The instance create raised, with its reservation already made.

    Stockout or not, this never moves on to another zone: the capacity was
    held HERE. What it does depends on one read, because the reservation must
    not be taken from under a box that exists:

      absent    Google says there is no such instance. The reservation is
                released, and the refusal says nothing is left billing — or,
                if the release fails, that it is still billing and how to
                release it.
      present   the answer was lost and the box is there. Returns, so the
                caller records the box: it is running, reserved and billing,
                and the worst outcome is for it to be in no host list.
      unknown   Google would not say. Both are left as they are, and the
                refusal names both and hands over both commands.
    """
    name = blueprint.reservation
    box = blueprint.name
    release = rsv.delete_command(name, zone, project)
    try:
        absent = gc.confirms_absent(box, zone, project)
    except GcloudError:
        absent = None

    if absent is False:
        say(f"  warning: Google's answer was lost ({exc}), but {box} is there in "
            f"{zone} and on its reservation — carrying on")
        return

    if absent is None:
        raise LifecycleError(
            f"Google refused to create {box} in {zone}: {exc} — and whether the "
            f"box was made could not be checked. Its reservation ({name}) is still "
            f"billing, and {box} may exist and be billing too.",
            fix=output.fix(
                "look:",
                f"gcloud compute instances list --project={project}",
                "stop the box, if it is there:",
                f"gcloud compute instances stop {box} --zone={zone} "
                f"--project={project}",
                "and release the reservation, if the box is not:",
                release,
            ),
            kind=CREATE_FAILED,
        ) from exc

    try:
        gc.delete_reservation(name, zone, project)
    except GcloudError as unreleased:
        raise LifecycleError(
            f"Google refused to create {box} in {zone}: {exc}. The reservation "
            f"made for it ({name}) could not be released ({unreleased}), so it is "
            f"still billing with no box on it.",
            fix=output.fix("release it:", release, exc.fix),
            kind=CREATE_FAILED,
        ) from exc
    raise LifecycleError(
        f"Google refused to create {box} in {zone}: {exc}. The reservation made "
        f"for it ({name}) was released, so nothing is left billing.",
        fix=output.fix(
            exc.fix,
            f"check the console for a half-made {box} before trying again: "
            f"gcloud compute instances list --project={project}",
        ),
        kind=CREATE_FAILED,
    ) from exc


def build(
    gc: Gcloud, blueprint: Blueprint, ordering: Ordering, project: str, say,
    *, attempts: int = MAX_ATTEMPTS, leftover: "rsv.Reservation | None" = None,
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

    **`attempts`.** The same cap `zones.choose` applies to the ranked list, applied
    again here because the suggestions are not on that list. `choose` hands over
    six zones and every refusal can name a fresh one, so the queue refills as
    fast as it drains: an uncapped fall-through has no end and is a command that
    looks hung. Six attempts is `zones.MAX_ATTEMPTS`, which documented this cap
    long before anything enforced it.

    A RESERVED box is two things made in order — the reservation, then the
    instance bound to it — and `_reserve_and_create` is where that order and
    every way of stopping half-way through it are handled. What this loop sees
    of it is small: a zone with nothing to reserve falls through exactly as a
    zone with no card free does, and anything else is already a refusal that
    says what is left billing.

    `leftover` is a reservation an earlier run of this create left behind. A
    reserved build that is handed one goes to that reservation's zone and
    nowhere else, whatever the ordering says: a reservation cannot move, and
    trying the next zone would make a second one under the same name.
    """
    allowed = set(ordering.regions)
    queue = [zone.lower() for zone in ordering.zones]
    fall_through = ordering.fall_through
    reuse = leftover if blueprint.reserve else None
    if reuse is not None:
        if _reusable(blueprint.card, reuse) is None:
            _refuse_the_leftover(blueprint, reuse, project)
        queue = [reuse.zone.lower()]
        fall_through = False
    tried: list[str] = []
    capped = False
    while queue:
        if len(tried) >= attempts:
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
            if blueprint.reserve:
                _reserve_and_create(gc, blueprint, zone, project, say, reuse)
            else:
                with _instance_in_flight(blueprint, zone, project):
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
            # Only a stockout at the RESERVATION reaches here from a reserved
            # build, so nothing was reserved in this zone and nothing is left in
            # it. A stockout at the instance never does: by then the capacity
            # was held, and moving on would abandon a billing reservation.
            #
            # Two whole sentences rather than one with a word swapped in: each
            # is quoted by its own troubleshooting entry, and a sentence cut in
            # the middle by an interpolation has nothing left long enough to
            # find it by.
            if blueprint.reserve:
                say(f"  {zone} has no {_wanted(blueprint.card)} to reserve right now")
            else:
                say(f"  {zone} has no {_wanted(blueprint.card)} free right now")
            if fall_through:
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
            f"stopped after {attempts} zones, all out of {_wanted(blueprint.card)} capacity: "
            f"{', '.join(tried)}. Nothing was created and nothing is billing — this is "
            f"a cap, not the whole world, so there may be room somewhere untried.",
            # `--region` first, because it is the flag that matches what this
            # message says. The advice used to offer `--zone` alone, which asks
            # someone who has just been told a whole neighbourhood is short to
            # name one machine room in it.
            #
            # Two spellings. The plain one is a template and has always been;
            # for a reserved build a template that leaves the reservation out is
            # the command for a different box, so that one is written in full.
            fix=("wait and run the same command again, ask for a region this did not "
                 "reach: comfy-qat create --region <region>, or name a zone yourself: "
                 "comfy-qat create --zone <zone>"
                 if not blueprint.reserve else
                 f"wait and run the same command again, ask for a region this did "
                 f"not reach: {_same_box(blueprint)} --region <region>, or name a "
                 f"zone yourself: {_same_box(blueprint)} --zone <zone>"),
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
            f"every zone tried is out of {_wanted(blueprint.card)} capacity: "
            f"{', '.join(tried)}. That is {len(searched)} of the "
            f"{len(ordering.offering)} regions this project can use "
            f"{_thing(blueprint.card)} in, the "
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
            #
            # AND WHAT WAS ASKED FOR, for a reserved build. Without `--reserve`
            # this line, pasted back, makes an unreserved box — under `--yes`,
            # without a question. The name goes with it so the rerun is the same
            # box. Its siblings in `order_zones` already carried the flag.
            fix=(f"try somewhere this did not reach: comfy-qat create "
                 f"--os {blueprint.image.key} --gpu {blueprint.card.key}"
                 f"{_as_reserved(blueprint)} --region "
                 f"{untried[0]}; or wait and run the same command again — a stockout "
                 f"is usually minutes to hours"),
            kind=EXHAUSTED,
        )

    if ordering.offering and ordering.narrowed_to:
        # ONE REGION WAS TRIED BECAUSE ONE WAS NAMED. The branch below says
        # "every region this project can use the card in, so there is nowhere
        # left to try" — true when nothing narrowed the search, and printed on
        # a real project with 23 usable regions about the one `--region` named.
        # What is true is smaller, and the way out is to stop narrowing: the
        # same box, without `--region`.
        raise LifecycleError(
            f"every zone tried is out of {_wanted(blueprint.card)} capacity: "
            f"{', '.join(tried)}. That is every zone this could use in "
            f"{ordering.narrowed_to}, the region you named with --region — not "
            f"everywhere this project can use {_thing(blueprint.card)}. Nothing was "
            f"created and nothing is billing.",
            fix=(f"wait and run the same command again — a stockout is usually "
                 f"minutes to hours — or drop --region and let this pick: "
                 f"{_same_box(blueprint)}"),
            kind=EXHAUSTED,
        )

    if ordering.offering:
        raise LifecycleError(
            f"every zone tried is out of {_wanted(blueprint.card)} capacity: "
            f"{', '.join(tried)}. That is every region this project can use "
            f"{_thing(blueprint.card)} "
            f"in, so there is nowhere left to try right now. Nothing was created and "
            f"nothing is billing.",
            fix=("wait and run the same command again — a stockout is usually minutes "
                 "to hours — or ask for a different card: comfy-qat quota list"
                 if blueprint.card.is_gpu else
                 "wait and run the same command again — a stockout is usually "
                 "minutes to hours"),
            kind=EXHAUSTED,
        )

    # Nothing ranked anything, so this frame cannot say how wide the search was
    # and must not guess. `--zone` is the case: one zone, by request, and a
    # sentence about how many regions were considered would be an invention.
    raise LifecycleError(
        f"every zone tried is out of {_wanted(blueprint.card)} capacity: "
        f"{', '.join(tried) or 'none were offered'}. Nothing was created and nothing "
        f"is billing.",
        fix=("wait and run the same command again — a stockout is usually minutes to "
             "hours — or drop --zone and let this pick: comfy-qat create"
             if not blueprint.reserve else
             f"wait and run the same command again — a stockout is usually minutes "
             f"to hours — or drop --zone and let this pick: {_same_box(blueprint)}"),
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
        name=blueprint.name, os=blueprint.image.os,
        # Empty for no GPU, which is what discovery reads off an instance with
        # no accelerator. `to_toml` writes it as `none`.
        gpu=blueprint.card.name if blueprint.card.is_gpu else "",
        gce_instance=blueprint.name, gce_zone=zone, gce_project=project,
        running=True,
        reservation=blueprint.reservation or "",
    )


def taken_names(hosts: list[Host], instances: list[dict]) -> set[str]:
    """Every name already in use, in the host list or on the project."""
    names = {host.name.lower() for host in hosts}
    names |= {host.gce_instance.lower() for host in hosts if host.gce_instance}
    names |= {(instance.get("name") or "").lower() for instance in instances or []}
    return {name for name in names if name}


def reserved_stop_lines(name: str) -> list[str]:
    """The two commands that end a reserved box's bill, in the order they run.

    `reservation.stop_line` alone is `comfy-qat delete <name>`, and that is the
    command that ends the bill — but a box that has just been created, started
    or served is RUNNING, and `delete` refuses a running box: "is running, not
    stopped. Stop it first", exit 2. So the one line the ending offered was a
    command that is refused as printed. The refusals that hand over this pair
    have always printed both; the endings now do too.

    `down` is a STEP here and is labelled as one. It is never what stops this
    box's bill, and the line does not say it is.
    """
    return [_down_first(name), rsv.stop_line(name)]


def _down_first(name: str) -> str:
    """The step before `delete`, said as a step."""
    return f"  comfy-qat down {name}     # first — delete refuses a box that is running"


def next_steps(blueprint: Blueprint, zone: str) -> list[str]:
    """What to do with the box, and how to stop paying for it.

    Both, always. It is running from the moment it is created, and a message that
    says how to use a GPU box without saying how to stop it is how one bills all
    night.
    """
    lines = []
    if not blueprint.card.is_gpu:
        # No driver on either operating system, so neither driver paragraph.
        lines.append(
            f"{blueprint.name} has no GPU, so there is no driver to wait for. "
            f"ComfyUI will run on its CPU, which is slow.")
    elif blueprint.image.windows:
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
    if blueprint.reserve:
        # NOT `comfy-qat down ... # stop paying`. For a reserved box that line
        # is false: `down` stops the machine and the reservation goes on billing
        # for it. So the bill is stated, and the one command that ends it.
        lines.append(rsv.bill(blueprint.name))
        # The pair `reserved_stop_lines` returns, written out: the guard that
        # every command which can reserve ends on `rsv.stop_line` follows one
        # call from the command and no further, and this is that call. A test
        # holds these two lines equal to the helper's.
        lines.append(_down_first(blueprint.name))
        lines.append(rsv.stop_line(blueprint.name))
        return lines
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
    leftover: "rsv.Reservation | None" = None,
    instances: list[dict] | None = None,
    reservations: "list[rsv.Reservation] | None" = None,
    hosts: "list[Host] | None" = None,
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

    `leftover` is a reservation an earlier run of this create left behind, and
    it settles the question outright: the box goes where its reservation is.
    Asking for somewhere else is refused rather than quietly overridden.

    `instances` and `reservations` are what the per-card REGIONAL allowance is
    counted against. A grant of one T4 in each of two regions is one in each,
    so a region whose T4 is already held — by a reservation, running or not, or
    by a running box — is left out before anything is ranked, and said so.
    Handed neither, nothing is left out: not counted is "not asked", and the
    answer to that is today's ordering, not an invented shortage.

    `hosts` is the declared host list, for the one refusal here that hands
    over a way to let go of a reserved box. As in `check_quota`: a `comfy-qat`
    command only for a box an entry holds, by that entry's label.
    """
    from dataclasses import replace

    from .zones import choose, zones_offering, zones_with_machine_type

    # Google's zone and region names are lowercase, and so is everything read back
    # from it. `--zone US-CENTRAL1-A` is the same request; matching it against the
    # quota regions without lowering it refuses with a message about a region that
    # does not exist.
    zone = zone.strip().lower() if zone else zone
    region = region.strip().lower() if region else region

    # Whether `--region` is what narrowed this, for `build`'s last sentence.
    # `--zone` wins over it everywhere below, so it is only said without one.
    named = region if region and not zone else ""

    if not blueprint.card.is_gpu:
        return replace(
            _order_without_a_card(gc, project, blueprint, check, zone=zone,
                                  region=region, config=config, probe=probe,
                                  fleet=fleet),
            narrowed_to=named)

    if blueprint.reserve and leftover is not None:
        return _where_the_leftover_is(blueprint, leftover, project,
                                      zone=zone, region=region)
    full = _full_regions(blueprint.card, check, instances, reservations)

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
        if region_of(zone) in full:
            _refuse_full(blueprint, check, full, [region_of(zone)], project,
                         instances, reservations, hosts)
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

    # The per-card regional half of the limit. Regions with no room go before
    # anything is ranked: ranking one and then having Google refuse the create
    # there is a minute, a confirmation and a quota error for something that
    # was already known.
    taken = [name for name in regions if name in full]
    regions = [name for name in regions if name not in full]
    if taken and not regions:
        _refuse_full(blueprint, check, full, taken, project, instances,
                     reservations, hosts)

    ordering = choose(
        gc, project,
        accelerator=blueprint.card.accelerator,
        machine_type=blueprint.machine_type,
        regions=regions, config=config, probe=probe, fleet=fleet,
    )
    ordering = replace(ordering, narrowed_to=named)
    if not taken:
        return ordering
    # Said, because a region missing from a numbered list is otherwise
    # indistinguishable from one that was never in the running.
    skipped = tuple(
        f"{name} is left out: its {blueprint.card.name} allowance is already held "
        f"({_held_words(full[name])})" for name in taken)
    return replace(ordering, notes=(*ordering.notes, *skipped))


def _full_regions(card: Card, check: QuotaCheck, instances: list[dict] | None,
                  reservations) -> dict[str, tuple[int, int, tuple[str, ...]]]:
    """Regions where this card's own allowance has no room for one more box.

    `{region: (limit, held, who)}`. Empty when the caller handed over nothing
    to count against — see `order_zones`.

    Only a region where something is actually HELD is called full. A grant
    smaller than one box needs, with nothing on it, is a grant that is too
    small — `QuotaCheck.problem` says that, in its own words, and "full, held
    by nobody" would be a worse sentence about the same fact.
    """
    from .quota import meets

    if instances is None:
        return {}
    full = {}
    for region, limit in check.regional:
        held = rsv.held_in(region, card.accelerator, instances, reservations)
        # `meets`, not `<`: an unlimited grant is -1, and -1 is not a small
        # number. It has room for anything.
        if held and not meets(limit, held + card.count):
            full[region] = (limit, held, _who_holds(region, card.accelerator,
                                                    instances, reservations))
    return full


def _who_holds(region: str, accelerator: str, instances: list[dict],
               reservations) -> tuple[str, ...]:
    """The names holding one card's allowance in one region.

    Asked of `reservation.held_in` one holder at a time, so "holds this card
    here" is decided by the same function that produced the count — a
    reservation whose card is built into its machine type included.
    """
    names = [found.name for found in reservations or []
             if rsv.held_in(region, accelerator, [], [found])]
    names += [instance.get("name") or "an unnamed box"
              for instance in rsv.outside(instances, reservations)
              if rsv.held_in(region, accelerator, [instance], None)]
    return tuple(names)


def _held_words(entry: tuple[int, int, tuple[str, ...]]) -> str:
    """`1 of 1, held by comfy-linux-rsv`."""
    limit, held, who = entry
    return f"{held} of {limit}, held by {', '.join(who) or 'something unnamed'}"


def _refuse_full(blueprint: Blueprint, check: QuotaCheck, full, asked: list[str],
                 project: str, instances, reservations, hosts=None) -> None:
    """Every region this could go in has its allowance for the card held.

    Names the regions and who holds each, because "no quota" about a project
    that plainly has quota is the sentence that sends somebody to file a
    request with Google for something they already hold. The fix is whichever
    of two things is true: somewhere else has room, or something has to be let
    go of — and the commands for letting go are the ceiling refusal's own.
    """
    card = blueprint.card
    detail = "; ".join(f"{name} ({_held_words(full[name])})" for name in asked)
    reserve = _as_reserved(blueprint)
    lines: list[str] = []
    roomy = [name for name in check.regions if name not in full]
    if roomy:
        lines += ["somewhere with room:",
                  f"comfy-qat create --os {blueprint.image.key} --gpu {card.key}"
                  f"{reserve} --region {roomy[0]}"]
    mine = [found for found in reservations or []
            if any(rsv.held_in(name, card.accelerator, [], [found]) for name in asked)]
    holders = rsv.holders(mine)
    if holders:
        lines.append(_how_to_release(
            holders, _boxed(holders, instances), project,
            declared_boxes(holders, instances, hosts, project)))
    for instance in rsv.outside(instances, reservations):
        if any(rsv.held_in(name, card.accelerator, [instance], None) for name in asked):
            lines.append(f"{_stop_the_box(instance.get('name') or '', rsv.zone_of(instance))}"
                         f"   # a running box holding one")
    raise LifecycleError(
        f"this project's {card.name} allowance is already held in "
        f"{'every region it could go' if len(asked) > 1 else 'the region asked for'}: "
        f"{detail}. Nothing was created.",
        fix=output.fix(*lines) if lines else None,
        kind=NO_QUOTA,
    )


def _where_the_leftover_is(blueprint: Blueprint, leftover, project: str, *,
                           zone: str | None, region: str | None) -> Ordering:
    """The one zone a resumed create may use: where its reservation already is."""
    if _reusable(blueprint.card, leftover) is None:
        _refuse_the_leftover(blueprint, leftover, project)
    at = leftover.zone
    asked = zone if zone and zone != at else (
        region if region and region != region_of(at) else "")
    if asked:
        raise LifecycleError(
            f"{leftover.name} is already on this project in {at} — left by an "
            f"earlier run of this create — and this asks for {asked}. A "
            f"reservation cannot be moved. Nothing was created.",
            fix=output.fix(
                "carry on where it is:",
                f"comfy-qat create --os {blueprint.image.key} --gpu "
                f"{blueprint.card.key} --reserve --name {blueprint.name} --zone {at}",
                "or release it and start again:",
                rsv.delete_command(leftover.name, at, project),
            ),
            kind=CREATE_FAILED,
        )
    return Ordering(
        zones=(at,), regions=(region_of(at),),
        notes=(f"reusing reservation {leftover.name} in {at} — left by an earlier "
               f"run, so this zone or nothing",),
        fall_through=False,
    )


def _order_without_a_card(
    gc: Gcloud, project: str, blueprint: Blueprint, check, *,
    zone: str | None, region: str | None, config, probe, fleet,
) -> Ordering:
    """`order_zones` for a box with no GPU: the machine type is the whole question.

    The same three moves as the card path — refuse a zone or region outside
    what the quota covers, check a named zone against what Google offers there,
    otherwise rank — with the card half of each taken out. There is no
    accelerator to look for, so `accelerator-types` is never asked, and the
    refusals say "CPU quota" and name the machine, because "no none quota"
    names a card that does not exist and a `quota request --gpu` for it is a
    command that cannot run.

    `check` is a `CpuCheck`. Its region set is EMPTY in one case that is not a
    refusal: the project reported no CPU quota record at all, so there is
    nothing to narrow by. Then the search is everywhere the machine type is
    sold, not nowhere — not read is not zero.
    """
    from .zones import choose, zones_with_machine_type

    machine = blueprint.machine_type
    regions = list(check.regions)

    # ONE refusal for "outside where the CPU quota has room", reached from
    # `--zone` and from `--region`. The card path has this sentence twice, sixty
    # lines apart, and its own comment records the two drifting.
    outside: tuple[str, str, str, str] | None = None
    if zone and regions and region_of(zone) not in set(regions):
        outside = (region_of(zone), f"in {zone}", "zone",
                   f"zones list --filter=name={zone}")
    elif not zone and region and regions and region not in set(regions):
        outside = (region, "there", "region", f"regions list --filter=name={region}")
    if outside is not None:
        place, where, flag, listing = outside
        raise LifecycleError(
            f"this project has no CPU quota for {machine} in {place}, so nothing "
            f"can start {where}. It has room for it in {_grant_reaches(check)}. "
            f"Nothing was created.",
            fix=(f"check the {flag} name first — a typo reads as somewhere this "
                 f"project has no quota in: gcloud compute {listing}; then drop "
                 f"--{flag} and let this pick"),
            kind=NO_QUOTA,
        )

    if zone:
        offered = zones_with_machine_type(
            gc.machine_types(project, [zone], machine), machine)
        if not offered:
            raise LifecycleError(
                f"{zone} does not offer {machine}, so {blueprint.name} cannot be "
                f"made there. Nothing was created.",
                fix=(f"drop --zone and let this pick one, or pick a zone that has it: "
                     f"gcloud compute machine-types list "
                     f"--filter=name={machine} --project={project}"),
                kind=NO_ZONE,
            )
        return Ordering(zones=(zone,), regions=(region_of(zone),),
                        notes=("--zone was given, so there is no fall-through: this "
                               "zone or nothing",),
                        fall_through=False)

    if not regions:
        # Nothing to narrow by, so everywhere Google sells the machine. Asked
        # here rather than left to `choose`, which reads no regions as "ask
        # nothing" — the right reading for a card and the wrong one for this.
        sold = zones_with_machine_type(gc.machine_type_zones(project, machine), machine)
        regions = sorted({region_of(place) for place in sold})
    if region:
        regions = [name for name in regions if name == region]

    return choose(gc, project, accelerator=None, machine_type=machine,
                  regions=regions, config=config, probe=probe, fleet=fleet)


def nowhere(blueprint: Blueprint, ordering: Ordering, project: str) -> LifecycleError:
    """No zone at all fits, which is a refusal rather than a failed attempt."""
    if not blueprint.card.is_gpu:
        detail = ordering.notes[0] if ordering.notes else (
            f"no region this project has CPU quota in offers {blueprint.machine_type}")
        return LifecycleError(
            f"nowhere to put {blueprint.name}: {detail}. Nothing was created.",
            fix=(f"gcloud compute machine-types list --project={project} "
                 f"--filter=name={blueprint.machine_type} — where Google offers the "
                 f"machine at all"),
            kind=NO_ZONE,
        )
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
