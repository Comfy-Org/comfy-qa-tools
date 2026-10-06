"""What `list --live` reads off the project: two reads, and what each one proves.

`list` used to ask Google one question — is each box running — and answer one
column with it. A reserved box made that not enough: it bills stopped, so the
STATE column reading `stopped` is no longer the end of what a box costs. This is
the half that goes and looks, and it answers three more columns from the same
trip:

    RESERVED   is its capacity held, and so billing every hour, running or not
    AGE        how long the box has existed
    DISK       how much disk it has, which bills whether it runs or not

TWO READS PER PROJECT, and no third: `instances list`, which already carried
the state and also carries the creation time, the disk sizes and which
reservation a box is bound to; and `reservations list`. `gcloud.statuses_from`
answers STATE from the listing this module already holds, so `--live` did not
get a call more expensive for the two columns that come free with it.

NOTHING HERE RAISES AND NOTHING HERE PRINTS. `list` never exits non-zero for a
read that did not come back, so a failed read is carried as what it is — not
read — and every cell says so in its own word rather than borrowing the word
for something that was established:

    unknown      the instances read failed; nobody knows the state, age or disk
    -            Google answered, and this box is not on the project
    unchecked    one of the two reads failed, so RESERVED is the half that was
                 read and not the whole
    missing      both reads worked: the reservation is declared and not there

Age and disk size are not written to the host list. They are facts about the
cloud, a copy of them drifts the first time a disk is resized or a box is
adopted, and a stale copy printed offline reads exactly like a fresh one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import reservation as rsv
from . import say
from .config import Host
from .gcloud import UNKNOWN_STATE, GcloudError, statuses_from

# What a cell says when the read it comes from did not come back. Not `-`,
# which is the word for "Google answered and there is nothing here".
UNKNOWN = "unknown"
ABSENT = "-"


@dataclass(frozen=True)
class Row:
    """One cloud box, as the project described it — or failed to."""

    state: str
    reserved: str
    age: str
    disk: str


@dataclass(frozen=True)
class Survey:
    """Everything `list --live` learned, keyed by host name.

    `orphans` is `(project, reservation)` for each reservation no instance on
    its project is bound to. `unread` is `(project, why)` for each project
    whose reservations could not be listed — the one failure the table cannot
    show on its own, so the caller says it. It is data and not a sentence
    because the sentence belongs with every other warning this tool prints,
    where the docs check can find it.
    """

    rows: dict[str, Row] = field(default_factory=dict)
    orphans: tuple[tuple[str, rsv.Reservation], ...] = ()
    unread: tuple[tuple[str, str], ...] = ()
    # `(project, how many reservations)` for each project that HAS reservations
    # and whose instances could not be read. Which of them has no box cannot be
    # worked out without the boxes, so no orphan is named for it — and that
    # has to be said, or a table with no orphan block under it reads as a
    # project with no orphans.
    unsorted: tuple[tuple[str, int], ...] = ()


def _now() -> datetime:
    """The moment ages are measured from. A seam, so a test does not own a clock."""
    return datetime.now(timezone.utc)


def age(created: str | None, now: datetime | None = None) -> str:
    """`<1h`, `5h`, `3d` — since Google's own `creationTimestamp`.

    Hours under a day and whole days after, because the question the column
    answers is "is this the box I made this morning or the one from last
    week", and neither needs minutes.

    A timestamp that is missing or will not parse is `unknown`. Not `<1h`:
    that is the cheerful reading of a field that was never there, and it would
    make the oldest forgotten box on a project look like the newest.
    """
    try:
        made = datetime.fromisoformat(str(created or ""))
    except ValueError:
        return UNKNOWN
    if made.tzinfo is None:
        # Google always writes an offset. One without is not a moment anybody
        # can measure from, and guessing UTC would be inventing seven hours.
        return UNKNOWN
    seconds = ((now or _now()) - made).total_seconds()
    hours = int(seconds // 3600)
    if hours < 1:
        # Including a box "created in the future" by a clock a minute out.
        return "<1h"
    return f"{hours}h" if hours < 24 else f"{hours // 24}d"


def disk(instance: dict) -> str:
    """`200 GB` — every disk on the box, added up.

    All of them or nothing. A box that reports no `disks`, or one whose sizes
    cannot all be read, is `unknown`: `0 GB` would be a statement about the
    box, and half a sum printed as the sum is an under-count nobody can see.
    """
    disks = instance.get("disks")
    if not disks:
        return UNKNOWN
    total = 0
    for entry in disks:
        try:
            total += int((entry or {}).get("diskSizeGb"))
        except (TypeError, ValueError, AttributeError):
            return UNKNOWN
    return f"{total} GB"


def declared(host: Host) -> str:
    """The RESERVED cell of plain `list`: what the host list says, unchecked."""
    if not host.is_remote:
        return ABSENT
    return "yes" if host.reservation else "no"


def shape(found: rsv.Reservation) -> str:
    """What a reservation holds, in a word: the card, or failing that the machine.

    The card by the name `create --gpu` and `quota list` use for it, read from
    the accelerator Google listed or — for a card built into the machine type,
    which the record may not list — from the machine type. Nothing this tool
    has a row for is named by its machine type rather than guessed at.
    """
    # Imported here, not at the top: `create` is the heavy half of this package
    # and plain `list`, which is offline, imports this module for `declared`.
    from .create import CARDS

    for card in CARDS.values():
        if found.accelerator and card.accelerator == found.accelerator:
            return card.name
    for card in CARDS.values():
        if card.attached and card.machine_type == found.machine_type:
            return card.name
    return found.machine_type or found.accelerator or "unknown shape"


def _zone(record: dict) -> str:
    return str(record.get("zone") or "").rstrip("/").rsplit("/", 1)[-1]


def _reserved(host: Host, instance: dict | None,
              reservations: list[rsv.Reservation] | None, *, boxes_read: bool) -> str:
    """The RESERVED cell for one box, from whichever of the two reads came back.

    The reservation in question is the one the box's own record says it is
    bound to, and failing that the one the host list names. Then:

      both reads worked     `yes` when that reservation is on the project in
                            the box's zone, `missing` when it is not, `no` when
                            there is none to look for. `yes (not in host list)`
                            when the box is bound and the entry does not say so
                            — every sentence that reads the host list is wrong
                            about that box's bill until it does.
      either read failed    what was read, and `, unchecked`.
    """
    bound = rsv.bound_to(instance) if instance is not None else None
    name = bound or host.reservation
    if reservations is None or not boxes_read:
        return f"{'yes' if name else 'no'}, unchecked"
    if not name:
        return "no"
    if (name, host.gce_zone) not in {(found.name, found.zone) for found in reservations}:
        return "missing"
    return "yes" if host.reservation == name else "yes (not in host list)"


def survey(gc, hosts: list[Host], *, now: datetime | None = None) -> Survey:
    """Ask each project what its boxes and its reservations are. Never raises.

    One project at a time and one read at a time, so that a project nobody
    could reach costs only its own rows, and a reservations read that failed
    does not take the state, age and disk of a box with it.
    """
    now = now or _now()
    remote = [host for host in hosts if host.is_remote]
    wanted = [(host.gce_instance, host.gce_zone, host.gce_project) for host in remote]

    listings: dict[str, list[dict]] = {}
    reserved: dict[str, list[rsv.Reservation] | None] = {}
    unread: list[tuple[str, str]] = []
    for project in dict.fromkeys(host.gce_project for host in remote
                                 if host.gce_project):
        try:
            listings[project] = gc.list_instances(project)
        except GcloudError:
            # Said by the table and not here: STATE reads `unknown`, as it did
            # before there were any other columns to fill.
            pass
        try:
            reserved[project] = rsv.parse_all(gc.list_reservations(project))
        except GcloudError as exc:
            reserved[project] = None
            unread.append((project, str(exc)))

    # A project that is not in `listings` was not read, and `statuses_from`
    # answers UNKNOWN_STATE for its machines — never GONE, which is the word
    # `discover --prune` deletes on.
    states = statuses_from(wanted, listings)

    rows: dict[str, Row] = {}
    for host in remote:
        key = (host.gce_instance, host.gce_zone, host.gce_project)
        state = states.get(key, UNKNOWN_STATE)
        project = host.gce_project or ""
        if project not in reserved:
            # Not a Google address at all, so there was nobody to ask anything.
            rows[host.name] = Row(state, ABSENT, ABSENT, ABSENT)
            continue
        boxes_read = project in listings
        instance = next(
            (row for row in listings.get(project, [])
             if row.get("name") == host.gce_instance and _zone(row) == host.gce_zone),
            None)
        if not boxes_read:
            aged = sized = UNKNOWN
        elif instance is None:
            # Read, and not there — or there under another zone, which is a
            # different machine as far as age and disk go.
            aged = sized = ABSENT
        else:
            aged, sized = age(instance.get("creationTimestamp"), now), disk(instance)
        rows[host.name] = Row(
            state=state,
            reserved=_reserved(host, instance, reserved[project],
                               boxes_read=boxes_read),
            age=aged, disk=sized)

    # "Has no box" needs the boxes. A project whose instances could not be
    # read has no orphans this can name: every reservation on it would qualify,
    # and each would be handed a delete command for capacity that may be in use.
    orphans = tuple(
        (project, found)
        for project, found_all in reserved.items()
        if found_all is not None and project in listings
        for found in rsv.orphans(found_all, listings[project]))
    unsorted = tuple(
        (project, len(found_all)) for project, found_all in reserved.items()
        if found_all and project not in listings)
    return Survey(rows=rows, orphans=orphans, unread=tuple(unread), unsorted=unsorted)


def orphan_lines(found: Survey) -> list[str]:
    """The block under the table for reservations that have no box at all.

    A reservation with nothing on it is the one thing on a project that bills
    at a GPU's rate and appears in no list of machines. Each is named with
    what it holds and Google's own command to release it — Google's, because
    there is no box for `comfy-qat delete` to be given.

    Reservations, counted as reservations: one project's block says how many
    that project has, so the number beside the noun is a number of that noun.
    """
    lines: list[str] = []
    for project in dict.fromkeys(project for project, _entry in found.orphans):
        mine = [entry for owner, entry in found.orphans if owner == project]
        one = len(mine) == 1
        lines.append(
            f"{say.count(len(mine), 'reservation')} on {project} "
            f"{'has' if one else 'have'} no box, and {'is' if one else 'are'} "
            f"billing:")
        for entry in mine:
            lines.append(f"  {entry.name} in {entry.zone} ({shape(entry)})")
            lines.append(f"  {rsv.delete_command(entry.name, entry.zone, project)}")
    return lines
