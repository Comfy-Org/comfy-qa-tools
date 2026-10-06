"""What a reservation is, read out of Google's own records. No calls, no output.

A reserved box is a Compute Engine instance plus a *reservation*: a zonal record
that makes Google hold the capacity for one machine of one shape, and bill for
it every hour whether or not anything is running on it. This module is the
model of that record and the arithmetic over it — whose it is, how many cards
it holds, whether a box is using it — and the two sentences every command says
about a reserved box's bill.

It is deliberately the dull half. Nothing here talks to Google (`gcloud.py`
does), and nothing here refuses or prints: every function answers a question
and the caller words what follows from the answer. So there is no `raise` in
this file and no `say`, and `tests/test_reservation.py` holds it to that.

Three rules run through all of it, and each one is a mistake this tool has
already made somewhere else:

  * CARDS, NOT BOXES. The limit a reservation spends is the project's GPU
    allowance, which Google meters in cards. One H100 reservation holds eight.
  * ABSENT IS NOT ZERO. `in_use is None` means Google did not say; a
    reservations listing that is `None` was never read, and one that is `[]`
    was read and is empty.
  * GUESS HIGH. Where a record cannot be read fully, it is counted as holding
    more rather than less: an over-count costs a refusal, which is free, and an
    under-count costs a create Google then refuses — or a reservation billing
    past the limit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# `<box>-rsv`, always. Deterministic so that a rerun of the same `create` finds
# what an interrupted one left, and so a person reading the console can see
# which box a reservation was made for.
SUFFIX = "-rsv"

# How a reservation's description begins when this tool made it. The NAME is not
# the evidence — anybody can call a reservation `something-rsv` — and nothing
# this tool does to a reservation it did not make is safe to do on a name alone.
MARK = "comfy-qat:"

# Google's names stop at 63 characters and the suffix takes four of them.
MAX_BOX_NAME = 63 - len(SUFFIX)

_HELD_FOR = "held for "

# Google's word for "consume only the reservation named here".
_SPECIFIC = "SPECIFIC_RESERVATION"

# Machine families whose GPU is part of the machine type, and the MOST cards any
# machine type of each is known to carry. A reservation for one of these holds
# cards whether or not the record lists any — what the read-back carries for a
# built-in card is not settled — and a reservation made in the console can be
# for any machine type of the family, not only the four this tool orders.
#
# The number is the fallback for a variant nothing below can count: guess HIGH.
# It used to be a flat 1, which read an `a2-megagpu-16g` as one card of the
# project's allowance when it holds sixteen.
#
# NONE OF THESE NUMBERS WAS READ FROM GOOGLE BY THIS TOOL. They are Google's
# published machine-type tables as known when this was written (a2-megagpu-16g
# is the largest A2; the largest G2, G4, A3 and A4 carry eight), and nothing on
# this machine can check them without a call to the project: the SDK ships no
# machine-type table. `a4x-` is given A4's eight although the one A4X type
# known carries four — it is a guess, made upward.
_FAMILY_MOST = {"g2-": 8, "g4-": 8, "a2-": 16, "a3-": 8, "a4-": 8, "a4x-": 8}
_GPU_FAMILIES = tuple(_FAMILY_MOST)

# The A families say how many cards a machine type carries in its own name:
# `a2-highgpu-8g`, `a2-megagpu-16g`, `a3-ultragpu-8g`. That `-Ng` is Google's
# spelling of "N GPUs" and is read as stated.
_GPUS_IN_NAME = re.compile(r"-(\d+)g$")

# The G families do not: the number in `g2-standard-48` is vCPUs. So their
# counts are a table, by vCPU size — same provenance as `_FAMILY_MOST` above,
# recalled and not read. A size that is not here falls to the family's most.
_G_SIZES = {
    "g2-standard-": {4: 1, 8: 1, 12: 1, 16: 1, 24: 2, 32: 1, 48: 4, 96: 8},
    "g4-standard-": {48: 1, 96: 2, 192: 4, 384: 8},
}

# `a2-highgpu-8g` -> `a2-highgpu-`: the series, which is what fixes the card.
_SIZE = re.compile(r"\d+g?$")


def zone_of(where: dict | str | None) -> str:
    """The bare zone name — `us-central1-a` — off a record or a zone URL.

    Google gives a zone as a URL on every record it returns
    (`.../zones/us-central1-a`), and takes it bare on every command. `where` is
    an `instances list`, `reservations list` or `disks list` row, read by its
    `zone` key, or the URL (or the bare name) itself. "" when there is none.

    The ONE place this is written. It was four: here, `create`, `remove` and
    `inventory` each had their own copy of the same line.
    """
    if isinstance(where, dict):
        where = where.get("zone")
    return _tail(where)


def _tail(value: object) -> str:
    """The last part of a URL or path. Google names zones and types both ways."""
    return str(value or "").rstrip("/").rsplit("/", 1)[-1]


def _number(value: object) -> int | None:
    """An int64 as gcloud prints it — a string — or None if there is not one.

    None, not 0: a count that is missing or unreadable has not been reported,
    and what that means is the caller's decision, per field.
    """
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _region(zone: str) -> str:
    """`us-central1-a` -> `us-central1`."""
    return zone.rsplit("-", 1)[0] if "-" in zone else zone


@dataclass(frozen=True)
class Reservation:
    """One row of `gcloud compute reservations list`, as this tool reads it.

    `accelerator` and `accelerator_count` are what Google LISTED and nothing
    more: empty and 0 when the record carries no `guestAccelerators`, which is
    true of a reservation with no GPU and may also be true of one whose card is
    built into the machine type. `cards` is where that is resolved.

    `in_use` is None when Google omits `inUseCount`. Not 0.
    """

    name: str
    zone: str
    machine_type: str
    accelerator: str
    accelerator_count: int
    vm_count: int
    in_use: int | None
    status: str
    specific: bool
    description: str
    created: str

    @property
    def ours(self) -> bool:
        """Did this tool make it? Decided by the description, never the name."""
        return self.description.startswith(MARK)

    @property
    def box(self) -> str:
        """The box it was made for, or "" — and always "" when it is not ours.

        Somebody else's description is somebody else's sentence. Reading a box
        name out of one would hand a caller `comfy-qat down <their words>`.
        """
        if not self.ours:
            return ""
        _mark, found, rest = self.description.partition(_HELD_FOR)
        return rest.strip() if found else ""

    @property
    def cards(self) -> int:
        """How much of the GPU allowance this reservation holds. Cards, in all."""
        return self.vm_count * _cards_per_vm(self)


def _built_in(machine_type: str):
    """The card table's row for a machine type that carries its own card.

    Only rows where the card is part of the machine type. `n1-standard-8` is
    the T4's machine type as well, and matching on that would turn every
    reservation with no GPU into a GPU reservation.
    """
    # Imported here, not at the top: `create` imports this module.
    from .create import CARDS

    for card in CARDS.values():
        if card.attached and card.machine_type == machine_type:
            return card
    return None


def _family(machine_type: str) -> str:
    """The GPU machine family a machine type belongs to — `a2-` — or ""."""
    for family in _GPU_FAMILIES:
        if machine_type.startswith(family):
            return family
    return ""


def _cards_per_vm(found: Reservation) -> int:
    """Cards one VM of this reservation's shape holds. Never a guess downward.

    In order, and the first that answers wins:

      1. WHAT GOOGLE LISTED, when the record lists a card. A statement.
      2. THE CARD TABLE, for the four machine types this tool orders
         (`g2-standard-8`, `a2-highgpu-1g`, `a2-ultragpu-1g`, `a3-highgpu-8g`).
         Known: these are the rows `create` builds boxes from.
      3. THE NAME, for any other machine type of a GPU family that ends `-Ng`
         (`a2-highgpu-8g` is 8, `a2-megagpu-16g` is 16). Known as far as
         Google's naming goes; read as stated, even for a size never seen.
      4. THE SIZE TABLE, for `g2-standard-N` and `g4-standard-N`, whose N is
         vCPUs. Recalled from Google's published tables, not read by this tool.
      5. THE MOST THE FAMILY IS KNOWN TO CARRY, for anything else in a GPU
         family — a size the table lacks, a series nobody has seen. A GUESS,
         and deliberately the high one: an over-count is a refusal, which is
         free; an under-count admits a create Google then refuses.
      6. None, for a machine type in no GPU family. That includes a GPU family
         Google has not released yet, which nothing here can recognise.
    """
    if found.accelerator or found.accelerator_count:
        return max(1, found.accelerator_count)
    card = _built_in(found.machine_type)
    if card is not None:
        return max(1, card.count)
    family = _family(found.machine_type)
    if not family:
        return 0
    named = _GPUS_IN_NAME.search(found.machine_type)
    if named:
        return max(1, int(named.group(1)))
    for series, sizes in _G_SIZES.items():
        size = _number(found.machine_type.removeprefix(series))
        if found.machine_type.startswith(series) and size in sizes:
            return sizes[size]
    return _FAMILY_MOST[family]


def _series_card(machine_type: str):
    """The card table's row for another SIZE of this machine type's series.

    `g2-standard-48` is the series `g2-standard-`, which the table knows as the
    L4's; `a2-highgpu-8g` is the A100's. Same series, same card — only the
    number of them differs, and that is `_cards_per_vm`'s question.
    """
    # Imported here, not at the top: `create` imports this module.
    from .create import CARDS

    series = _SIZE.sub("", machine_type)
    for card in CARDS.values():
        if card.attached and _SIZE.sub("", card.machine_type) == series:
            return card
    return None


def _card_of(found: Reservation) -> str:
    """Which accelerator it holds, where that can be NAMED: the one listed,
    else the machine type's own, else its series' own.

    "" when none of those says — `a2-megagpu-16g`, or any G4 or A4. Such a
    reservation is still counted against the project's allowance by `cards`;
    what it contributes per card is `_counts_against`'s decision.
    """
    if found.accelerator:
        return found.accelerator
    card = _built_in(found.machine_type) or _series_card(found.machine_type)
    return card.accelerator if card is not None else ""


def _counts_against(found: Reservation, accelerator: str) -> bool:
    """Does this reservation spend the per-card quota of `accelerator`?

    Yes when its card can be named and is that one. When it CANNOT be named —
    a GPU family is all the record gives — it counts against every card whose
    own machine type is in that family: an `a2-megagpu-16g` against both A100s
    this tool orders. Guessing high again, and for the same reason. A family
    this tool orders no card from (G4, A4) therefore counts against none, and
    only in the project-wide total.
    """
    # Imported here, not at the top: `create` imports this module.
    from .create import CARDS

    named = _card_of(found)
    if named:
        return named == accelerator
    family = _family(found.machine_type)
    return bool(family) and any(
        card.attached and card.accelerator == accelerator
        and card.machine_type.startswith(family)
        for card in CARDS.values())


def _parse(record: dict) -> Reservation:
    specific = record.get("specificReservation") or {}
    properties = specific.get("instanceProperties") or {}
    accelerators = [entry for entry in properties.get("guestAccelerators") or []
                    if isinstance(entry, dict)]
    # A card that is listed is at least one card. The same rule, for the same
    # reason, as `create._cards_running`: an unreadable count is not evidence of
    # an empty machine.
    count = sum(max(1, _number(entry.get("acceleratorCount")) or 0)
                for entry in accelerators)
    vms = _number(specific.get("count"))
    return Reservation(
        name=str(record.get("name") or ""),
        zone=_tail(record.get("zone")),
        machine_type=_tail(properties.get("machineType")),
        accelerator=_tail(accelerators[0].get("acceleratorType")) if accelerators else "",
        accelerator_count=count,
        # A reservation that does not say how many VMs it holds holds one, not
        # none. Zero is kept when Google says zero.
        vm_count=1 if vms is None else vms,
        in_use=_number(specific.get("inUseCount")),
        status=str(record.get("status") or ""),
        specific=bool(record.get("specificReservationRequired")),
        description=str(record.get("description") or ""),
        created=str(record.get("creationTimestamp") or ""),
    )


def parse_all(records: list[dict]) -> list[Reservation]:
    """Every record of a `reservations list` payload, in order.

    A record missing keys is still a reservation and is kept: one this tool
    cannot read fully still holds whatever it holds. Only something that is not
    a record at all is passed over.
    """
    return [_parse(record) for record in records or [] if isinstance(record, dict)]


def name_for(box: str) -> str:
    """The reservation a box's capacity is held under."""
    return f"{box}{SUFFIX}"


def describe_for(box: str) -> str:
    """The description that marks a reservation as made by this tool, for a box."""
    return f"{MARK} {_HELD_FOR}{box}"


def bound_to(instance: dict) -> str | None:
    """The reservation an instance may only consume, by name. None if not bound.

    Read from the instance's own record in `instances list`. Anything other
    than a SPECIFIC affinity — `ANY_RESERVATION`, the default, included — is
    not bound: such a box takes whatever matches or nothing.
    """
    affinity = instance.get("reservationAffinity") or {}
    if affinity.get("consumeReservationType") != _SPECIFIC:
        return None
    for value in affinity.get("values") or []:
        if value:
            return _tail(value)
    return None


def cards_reserved(reservations: list[Reservation]) -> int:
    """Cards held by reservations, used or not, on every zone listed."""
    return sum(found.cards for found in reservations)


def outside(instances: list[dict] | None,
            reservations: list[Reservation] | None) -> list[dict]:
    """The instances whose cards are NOT already counted through a reservation.

    A box bound to a reservation that is in `reservations` is that
    reservation's card, and counting both would be counting it twice. Every
    other box is its own: unbound, bound to a reservation that was not listed —
    deleted from under it, or simply not in this listing — or bound by a name
    that exists only in another zone. When the reservations were not read at
    all, nothing is covered and every box is counted as what it is running.
    """
    covered = {(found.name, found.zone) for found in reservations or []}
    return [instance for instance in instances or []
            if (bound_to(instance), _zone_of(instance)) not in covered]


# The names these two were first written under, which other modules call. The
# same functions, not copies.
_zone_of = zone_of
_outside = outside


def cards_held(instances: list[dict], reservations: list[Reservation] | None) -> int:
    """How much of the project's GPU allowance is spoken for. Cards.

    Reserved cards — running, stopped or with no box at all — plus the cards on
    every non-TERMINATED instance that is not consuming one of those
    reservations.

    `reservations=None` means the reservations were NOT READ, and the answer is
    then the running cards only. That is an under-count by exactly the cards
    reserved and not running, and the caller is the one who knows it: it must
    say so rather than present this number as the whole.
    """
    # Imported here, not at the top: `create` imports this module.
    from .create import _cards_running

    return cards_reserved(reservations or []) + _cards_running(
        _outside(instances, reservations))


def held_in(region: str, accelerator: str, instances: list[dict],
            reservations: list[Reservation] | None) -> int:
    """`cards_held`, for one card in one region — the per-card regional quota.

    A reservation whose record names no card is attributed by
    `_counts_against`: to its series' card where the card table knows the
    series, and otherwise to EVERY card this tool orders from its machine
    family. So the sum of `held_in` over all cards can exceed `cards_held` —
    each per-card answer is an upper bound on its own, by design.
    """
    # Imported here, not at the top: `create` imports this module.
    from .create import _cards_running

    held = sum(found.cards for found in reservations or []
               if _region(found.zone) == region and _counts_against(found, accelerator))
    for instance in _outside(instances, reservations):
        if _region(_zone_of(instance)) != region:
            continue
        # One accelerator at a time through the same counting rule, so a status
        # or a count is never read two different ways.
        for entry in instance.get("guestAccelerators") or []:
            if _tail(entry.get("acceleratorType")) == accelerator:
                held += _cards_running([{**instance, "guestAccelerators": [entry]}])
    return held


def holders(reservations: list[Reservation]) -> tuple[tuple[str, str, int, str], ...]:
    """`(name, zone, cards, box)` for every reservation that holds a card.

    `box` is "" for one this tool did not make, which is how a caller knows to
    print Google's own delete command rather than `comfy-qat delete`.
    """
    return tuple((found.name, found.zone, found.cards, found.box)
                 for found in reservations if found.cards > 0)


def leftover_for(box: str, reservations: list[Reservation]) -> Reservation | None:
    """The reservation already on the project under this box's name, if any."""
    wanted = name_for(box)
    for found in reservations:
        if found.name == wanted:
            return found
    return None


def _asked(accelerator: str) -> tuple[str, int]:
    """`type=nvidia-tesla-t4,count=1`, in either order, as (type, count)."""
    fields = dict(part.split("=", 1) for part in accelerator.split(",") if "=" in part)
    return fields.get("type", ""), _number(fields.get("count")) or 1


def fits(found: Reservation, *, machine_type: str, accelerator: str | None) -> bool:
    """May a new box be put on this reservation instead of making another?

    Only one this tool made, for one VM, with nothing using it, of exactly the
    shape being asked for — and not one already on its way out.

    `accelerator` is the `--accelerator` value the box would be created with:
    `type=...,count=N` for a card that is attached, None for one built into the
    machine type. For None the machine type alone fixes the card, so a record
    that lists the built-in card and one that does not both fit.
    """
    if not found.ours or found.vm_count != 1 or found.in_use not in (0, None):
        return False
    if found.status == "DELETING" or found.machine_type != machine_type:
        return False
    if accelerator is None:
        return (not found.accelerator or _built_in(machine_type) is not None
                or machine_type.startswith(_GPU_FAMILIES))
    return (found.accelerator, found.accelerator_count) == _asked(accelerator)


def orphans(reservations: list[Reservation], instances: list[dict]) -> list[Reservation]:
    """Reservations no instance is bound to: billing, with no box.

    A stopped box still counts as a box — its reservation is doing what it was
    made for. Bound means by name AND zone; a reservation is zonal.
    """
    bound = {(bound_to(instance), _zone_of(instance)) for instance in instances or []}
    return [found for found in reservations if (found.name, found.zone) not in bound]


def delete_command(name: str, zone: str, project: str) -> str:
    """Google's own command to release a reservation, for a person to paste."""
    return f"gcloud compute reservations delete {name} --zone={zone} --project={project}"


def bill(name: str) -> str:
    """THE sentence about what a reserved box costs. One source, said everywhere."""
    return (f"{name} is reserved. Google holds its capacity and bills for it every "
            f"hour — running or stopped — until the box is deleted.")


def stop_line(name: str) -> str:
    """What takes the place of `comfy-qat down ...  # stop paying` for a reserved box."""
    return (f"  comfy-qat delete {name}   # the only thing that stops a reserved "
            f"box's bill — the box and its disk go too")
