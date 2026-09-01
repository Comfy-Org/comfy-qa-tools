"""Moving a box to a zone that actually has the card free.

A GPU stockout is not a fault you can fix where you are: the zone has none of
that machine, and no amount of retrying changes it. The way out is to be
somewhere else — which, done by hand, is a snapshot, a disk, an instance and a
config edit. That is four chances to get it wrong, so it is one command.

The existing disk is snapshotted rather than rebuilt, because the ComfyUI install
lives on it and reinstalling on Windows is the slow, fragile path.

Four things this file is careful about. Each of them has already cost money on a
real project, and the evidence is in the tests.

*The boot disk is read, never guessed.* A box's boot disk is not required to
share the instance's name — `comfy-win` boots from `comfy-win-a` — and the name
is right there in the instance description this already fetches. Deriving it by
chopping up the new disk's name happened to work for one box and would have
snapshotted the wrong disk, or nothing at all, for the next one.

*A half-finished move is resumable, not a wall.* The steps are separate cloud
resources created in order, so a run that dies part-way leaves real, billing
resources behind. Re-running looks first, says what it found, and carries on from
there. The alternative is what happened: a 300 GB orphan disk that blocked every
later attempt with a gcloud error about a name already in use.

*Nothing is left billing in silence.* The snapshot is deleted once the instance
exists — the new disk is a full independent copy and the original box is still
sitting untouched in the old zone, so the snapshot is a third copy nothing reads.
Anything found lying around is reported with its size and the exact command that
removes it. Deleting is never automatic: a 300 GB disk is somebody's install.

*The moved box is the same box.* Disk type is carried across explicitly, because
`gcloud compute disks create` defaults to pd-standard. A move that silently turns
a pd-balanced boot disk into a pd-standard one produces a machine that is slower
than the one it replaced, for no stated reason — which is the worst thing that
can happen to a box whose entire purpose is comparing behaviour between machines.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Callable

from .config import Host
from .gcloud import QUOTA, Gcloud, GcloudError

# The steps a move is made of. They are identifiers rather than free text so the
# printed plan and the executed run come off one list: a preview assembled
# separately from the run it previews is a preview that can drift from it.
SNAPSHOT = "snapshot"
REUSE_SNAPSHOT = "reuse-snapshot"
CREATE_DISK = "create-disk"
REUSE_DISK = "reuse-disk"
CREATE_INSTANCE = "create-instance"
REUSE_INSTANCE = "reuse-instance"
DELETE_SNAPSHOT = "delete-snapshot"
REGISTER = "register"
LEAVE = "leave"

# gcloud's own default when `--type` is omitted. Naming it here is the reason the
# default is never reached: the source disk's type is always passed through.
GCLOUD_DEFAULT_DISK_TYPE = "pd-standard"


class MoveError(Exception):
    """A move that stopped part-way. Says what exists now, and what to run next.

    `left` is what is on the project because of this run, in plain words, with
    what it costs. `cleanup` is the exact commands that remove it, for someone
    who would rather start over than resume. A move that fails at the last step
    has already paid for a snapshot and a disk; saying only "the move failed"
    invites running it again and hitting a name clash on top of the bill.
    """

    def __init__(
        self,
        message: str,
        *,
        fix: str | None = None,
        left: tuple[str, ...] = (),
        cleanup: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.fix = fix
        self.left = tuple(left)
        self.cleanup = tuple(cleanup)


@dataclass(frozen=True)
class Action:
    """One step, and the line describing it. Printed and executed from one object."""

    kind: str
    line: str


@dataclass(frozen=True)
class Outcome:
    """What a finished move did, as opposed to what it planned to do."""

    done: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def reused_disk(self) -> bool:
        return REUSE_DISK in self.done

    def reused_instance(self) -> bool:
        return REUSE_INSTANCE in self.done


def _tail(value: str | None) -> str:
    """gcloud returns full resource URLs; only the last segment is meaningful."""
    return (value or "").rstrip("/").rsplit("/", 1)[-1]


def boot_disk(instance: dict) -> str | None:
    """The disk the machine boots from — the one carrying the install.

    Read off the instance description, because a boot disk's name is its own:
    `comfy-win` boots from a disk called `comfy-win-a`. Any rule that derives one
    name from the other is a rule that is right by luck.
    """
    disks = instance.get("disks") or []
    for disk in disks:
        if disk.get("boot"):
            return _tail(disk.get("source")) or disk.get("deviceName")
    if disks:
        return _tail(disks[0].get("source")) or disks[0].get("deviceName")
    return None


def machine_type(instance: dict) -> str:
    return _tail(instance.get("machineType")) or "g2-standard-8"


def accelerator_of(instance: dict) -> str | None:
    """The `--accelerator` value the source box needs, or None if it carries none.

    Five of the nine cards this tool can order are attached by flag rather than
    built into the machine type — T4, P4, P100, V100, K80, all on N1. Not passing
    it produced an `n1-standard-8` with no GPU, from a move that reported success:
    the box booted, ComfyUI installed, and the only symptom was torch reporting no
    CUDA device, which reads as a broken test rather than a broken move.

    Read off the instance rather than rebuilt from `host.gpu`. `host.gpu` is a
    display string ("T4") and `discover.accelerator` is lossy — it uppercases and
    strips the vendor prefix, so nothing can turn it back into `nvidia-tesla-t4`
    without the card table. What the box actually reports cannot be wrong.

    `acceleratorType` comes back as a zonal URL, and the zone in it is the one
    being moved away from. Only the last segment travels.
    """
    cards = instance.get("guestAccelerators") or []
    if not cards:
        return None
    kind = _tail(cards[0].get("acceleratorType"))
    if not kind:
        return None
    return f"type={kind},count={cards[0].get('acceleratorCount') or 1}"


def metadata_pairs(instance: dict) -> str | None:
    """Carry across the metadata that matters, notably Windows SSH.

    Losing `enable-windows-ssh` would leave the moved box unreachable by every
    command this tool has, which would look like the move failing.
    """
    keep = []
    for item in (instance.get("metadata") or {}).get("items") or []:
        key = item.get("key")
        if key in ("enable-windows-ssh", "enable-oslogin"):
            keep.append(f"{key}={item.get('value')}")
    return ",".join(keep) or None


def network_of(instance: dict) -> dict:
    """The network the source instance is on, and whether it can reach out.

    A moved box with no external address and no Cloud NAT has no route to the
    internet at all. IAP covers getting *in*, which is what the no-address rule
    was reasoning about; nothing covered getting *out*. So the box could not pip
    install, could not download ComfyUI, could not fetch a model — `host go`
    could never provision a box `host move` had made. It only looked fine
    because the disk already carried an install.

    A move is supposed to produce the same machine somewhere else, so the answer
    is to copy what the source has rather than to impose a policy on the copy.
    """
    interfaces = instance.get("networkInterfaces") or []
    if not interfaces:
        return {}
    first = interfaces[0]
    return {
        "network": _tail(first.get("network")) or None,
        "subnet": _tail(first.get("subnetwork")) or None,
        "external": bool(first.get("accessConfigs")),
    }


def suffix_for(zone: str) -> str:
    """`us-central1-b` -> `b`, so a moved box reads as where it went."""
    return zone.rsplit("-", 1)[-1] or zone


def _gib(value: str | int | None) -> str:
    try:
        return f"{int(value) / (1024 ** 3):.0f} GB"
    except (TypeError, ValueError):
        return ""


def _size(resource: dict | None, *keys: str) -> str | None:
    for key in keys:
        value = (resource or {}).get(key)
        if value:
            return str(value)
    return None


def _when(resource: dict | None) -> datetime | None:
    stamp = (resource or {}).get("creationTimestamp")
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def describe_disk(disk: dict | None) -> str:
    """A disk in one phrase: how big, what kind, where it came from."""
    if not disk:
        return ""
    bits = []
    size = _size(disk, "sizeGb")
    if size:
        bits.append(f"{size} GB")
    kind = _tail(disk.get("type"))
    if kind:
        bits.append(kind)
    source = _tail(disk.get("sourceSnapshot"))
    if source:
        bits.append(f"from {source}")
    return ", ".join(bits)


def describe_snapshot(snapshot: dict | None) -> str:
    """A snapshot in one phrase. Storage is what bills, so storage is named."""
    if not snapshot:
        return ""
    bits = []
    size = _size(snapshot, "diskSizeGb")
    if size:
        bits.append(f"{size} GB disk")
    stored = _gib(snapshot.get("storageBytes"))
    if stored:
        bits.append(f"{stored} stored")
    source = _tail(snapshot.get("sourceDisk"))
    if source:
        bits.append(f"of {source}")
    return ", ".join(bits)


# --- the plan -------------------------------------------------------------

@dataclass(frozen=True)
class Plan:
    """What moving this box will do, before anything is done."""

    host: Host
    to_zone: str
    source_disk: str
    new_instance: str
    new_disk: str
    snapshot: str
    machine_type: str
    disk_type: str | None = None
    metadata: str | None = None
    # None for G2/A2/A3, where the machine type carries the card, and passing the
    # flag alongside one of those is refused by Google.
    accelerator: str | None = None
    # Whether the box being moved is on right now. A move does not touch it
    # either way, so this exists only so the plan and the summary can say what is
    # true rather than what is convenient.
    source_running: bool = False
    # Copied from the source rather than decided here: a move is meant to
    # produce the same machine somewhere else, and a box with no egress is not
    # the same machine — it cannot install, update or download anything.
    external_ip: bool = False
    network: str | None = None
    subnet: str | None = None

    @property
    def project(self) -> str:
        return self.host.gce_project or ""

    @property
    def retired_name(self) -> str:
        """What the box being moved away from is called in the host list after.

        The new box takes the canonical name and the canonical port, so the old
        one is renamed by where it is. `comfy-linux-us-central1-c` says what it
        is; `comfy-linux-a` did not, and after two moves neither did
        `comfy-linux-a-b`. It stays in the list so `down` can still reach it —
        the box is real and still billing until somebody deletes it.
        """
        return f"{self.host.name}-{self.host.gce_zone}"

    @property
    def snapshot_family(self) -> str:
        """The names this tool's own move snapshots take for this disk.

        Deliberately not tied to the destination zone. A move that fails on
        capacity in one zone is most often retried into another, and re-taking a
        300 GB snapshot to do that is the expensive half of the work repeated for
        nothing. Older runs named the snapshot per-zone, so the prefix has to
        match those too.
        """
        return f"{self.source_disk}-move"

    def actions(self, found: "Found | None" = None) -> list[Action]:
        """Every step this move will take, in order, each with its own sentence."""
        found = found or Found()
        out: list[Action] = []

        if found.instance is not None:
            out.append(Action(
                REUSE_INSTANCE,
                f"{self.new_instance} already exists in {self.to_zone} — keep it, "
                "there is nothing left to create",
            ))
        else:
            if found.reuse_disk:
                note = describe_disk(found.disk)
                out.append(Action(
                    REUSE_DISK,
                    f"reuse the disk {self.new_disk} already in {self.to_zone}"
                    + (f" ({note})" if note else "")
                    + " — no snapshot needed",
                ))
            else:
                if found.snapshot is not None:
                    out.append(Action(
                        REUSE_SNAPSHOT,
                        f"reuse the snapshot {found.snapshot_name}, already taken of "
                        f"{self.source_disk}"
                        + (f" ({describe_snapshot(found.snapshot)})"
                           if describe_snapshot(found.snapshot) else ""),
                    ))
                else:
                    out.append(Action(
                        SNAPSHOT,
                        f"snapshot the boot disk {self.source_disk} of "
                        f"{self.host.gce_instance} ({self.host.gce_zone}) as "
                        f"{self.snapshot}",
                    ))
                out.append(Action(
                    CREATE_DISK,
                    f"create disk {self.new_disk} in {self.to_zone} from that "
                    f"snapshot ({self.disk_type or GCLOUD_DEFAULT_DISK_TYPE}, "
                    f"matching {self.source_disk})",
                ))
            carried = (
                f" with {self.accelerator.replace('type=', '').replace(',count=', ' x')}"
                if self.accelerator else ""
            )
            out.append(Action(
                CREATE_INSTANCE,
                f"create {self.new_instance} in {self.to_zone} "
                f"({self.machine_type}{carried})",
            ))

        # A snapshot is deleted whenever the move would leave one behind, whether
        # this run took it or an earlier one did. The new disk is a full copy and
        # the original box still holds the original, so a third copy only bills.
        if found.snapshot is not None or any(a.kind == SNAPSHOT for a in out):
            out.append(Action(
                DELETE_SNAPSHOT,
                f"delete the snapshot {found.snapshot_name or self.snapshot} — the "
                f"new disk is a full copy and {self.host.gce_instance} still has "
                "the original",
            ))

        out.append(Action(REGISTER, f"add {self.new_instance} to your host list"))
        # This is a statement, not an action — see the LEAVE branch in run_move.
        # It said "stopped" unconditionally until 2026-09-01, which was a false
        # statement about a billing GPU whenever the source happened to be on.
        out.append(Action(
            LEAVE,
            f"leave {self.host.gce_instance} running in {self.host.gce_zone} — "
            "it keeps billing until you stop it"
            if self.source_running else
            f"leave {self.host.gce_instance} stopped in {self.host.gce_zone}",
        ))
        return out

    def steps(self, found: "Found | None" = None) -> list[str]:
        return [action.line for action in self.actions(found)]


def plan_move(host: Host, instance: dict, to_zone: str,
              source_disk: dict | None = None) -> Plan:
    """Name everything the move will create, from what the instance actually is.

    `source_disk` is the boot disk's own description, when it has been fetched.
    It carries the disk type, which has to be passed on explicitly or gcloud
    quietly builds the new disk as pd-standard.
    """
    disk = boot_disk(instance) or host.gce_instance
    tag = suffix_for(to_zone)
    # The box keeps its name. GCE names are unique per ZONE, not per project, so
    # `comfy-linux` can exist in both — the suffix was never Google's requirement,
    # only a way to keep the appended host-list key unique. Appending is what
    # broke the move: `go comfy-linux` still pointed at the empty zone afterwards.
    # The disk keeps a suffix because disks are what a half-finished move leaves
    # lying around, and telling two copies apart matters more than the name does.
    return Plan(
        host=host,
        to_zone=to_zone,
        source_disk=disk,
        new_instance=host.gce_instance or host.name,
        new_disk=f"{disk}-{tag}",
        snapshot=f"{disk}-move",
        machine_type=machine_type(instance),
        accelerator=accelerator_of(instance),
        source_running=(instance.get("status") == "RUNNING"),
        disk_type=_tail((source_disk or {}).get("type")) or None,
        metadata=metadata_pairs(instance),
        **{k: v for k, v in (
            ("external_ip", network_of(instance).get("external", False)),
            ("network", network_of(instance).get("network")),
            ("subnet", network_of(instance).get("subnet")),
        )},
    )


# --- looking before touching ---------------------------------------------

@dataclass(frozen=True)
class Found:
    """What is already on the project before this move starts.

    A move is four cloud resources created in order, so any of them can already
    be there from a run that died half-way. Looking first is what turns "that
    name is taken" from gcloud into "you already have that disk, carrying on".
    """

    source_disk: dict | None = None
    snapshot: dict | None = None
    disk: dict | None = None
    instance: dict | None = None
    spare_snapshots: tuple[dict, ...] = ()
    unrelated_snapshots: tuple[dict, ...] = ()
    reuse_disk: bool = False
    blocker: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def snapshot_name(self) -> str | None:
        """The snapshot to reuse, which may be an older run's per-zone name."""
        return (self.snapshot or {}).get("name")

    def anything(self) -> bool:
        return bool(self.snapshot or self.disk or self.instance
                    or self.spare_snapshots or self.unrelated_snapshots)


def judge_disk(plan: Plan, source_disk: dict | None, snapshot: dict | None,
               disk: dict | None) -> tuple[bool, str | None, tuple[str, ...]]:
    """Is a disk already sitting in the target zone safe to carry on with?

    Safe means: a move made it, from a snapshot of this boot disk, at this size,
    and nothing is using it. Anything less certain is refused rather than guessed
    at — carrying on with the wrong disk boots the wrong machine and looks
    exactly like a move that worked.

    Returns whether to reuse it, why not if not, and anything true about it that
    the person should hear even though it is not disqualifying.
    """
    if disk is None:
        return False, None, ()

    users = disk.get("users") or []
    if users:
        return False, (
            f"{plan.new_disk} already exists in {plan.to_zone} and is attached to "
            f"{_tail(users[0])}. That is a disk in use, not a leftover from a "
            "half-finished move."
        ), ()

    source = _tail(disk.get("sourceSnapshot"))
    if not source:
        return False, (
            f"{plan.new_disk} already exists in {plan.to_zone}, but nothing records "
            "what it was made from, so it cannot be confirmed as a copy of "
            f"{plan.source_disk}."
        ), ()
    if not source.startswith(plan.snapshot_family):
        return False, (
            f"{plan.new_disk} already exists in {plan.to_zone}, but it was made from "
            f"{source}, which is not a snapshot this move takes of "
            f"{plan.source_disk}."
        ), ()

    made = _when(disk)
    taken = _when(snapshot)
    if made and taken and made < taken:
        return False, (
            f"{plan.new_disk} already exists in {plan.to_zone}, but it predates the "
            f"snapshot {snapshot.get('name')} it claims to come from, so it is a "
            "copy of something older."
        ), ()

    want = _size(source_disk, "sizeGb")
    have = _size(disk, "sizeGb")
    if want and have and want != have:
        return False, (
            f"{plan.new_disk} already exists in {plan.to_zone} at {have} GB, but "
            f"{plan.source_disk} is {want} GB. It is not a copy of this boot disk."
        ), ()

    notes = []
    wanted_type = _tail((source_disk or {}).get("type"))
    got_type = _tail(disk.get("type"))
    if wanted_type and got_type and wanted_type != got_type:
        notes.append(
            f"{plan.new_disk} is {got_type} but {plan.source_disk} is {wanted_type}, "
            "so the moved box will have a slower boot disk than the one it "
            "replaces. To get a matching disk instead, delete it and run this "
            f"again:\n        {delete_disk_command(plan)}"
        )
    return True, None, tuple(notes)


def _in_zone(resource: dict, zone: str) -> bool:
    return _tail(resource.get("zone")) == zone


def survey(gc: Gcloud, plan: Plan) -> Found:
    """Read the state of everything the move touches, before touching any of it.

    Read-only, and it runs for a dry run too: a preview that has not looked
    cannot tell you it is about to hit a name that is already taken. Listing
    beats describing here because the same two calls also turn up the leftovers
    nobody asked about — an unattached disk and a snapshot from a move that died
    weeks ago both look like nothing at all in the console, and bill every day.
    """
    project = plan.project
    disks = gc.run(["compute", "disks", "list", f"--project={project}"]) or []
    snapshots = gc.run(["compute", "snapshots", "list", f"--project={project}"]) or []
    instances = gc.list_instances(project)

    source_disk = next(
        (d for d in disks
         if d.get("name") == plan.source_disk and _in_zone(d, plan.host.gce_zone)),
        None,
    )
    disk = next(
        (d for d in disks
         if d.get("name") == plan.new_disk and _in_zone(d, plan.to_zone)),
        None,
    )
    instance = next(
        (i for i in instances
         if i.get("name") == plan.new_instance and _in_zone(i, plan.to_zone)),
        None,
    )

    # Snapshots this tool took of this boot disk, newest first. Reuse the newest
    # ready one whatever it is called; older runs named them per-zone.
    family = [
        s for s in snapshots
        if (s.get("name") or "").startswith(plan.snapshot_family)
        and _tail(s.get("sourceDisk")) == plan.source_disk
    ]
    family.sort(key=lambda s: s.get("creationTimestamp") or "", reverse=True)
    usable = [s for s in family if s.get("status") in (None, "READY")]
    snapshot = usable[0] if usable else None
    spares = tuple(s for s in family if s is not snapshot)

    known = {d.get("name") for d in disks}
    unrelated = tuple(
        s for s in snapshots
        if not any(s is member for member in family)
        and _tail(s.get("sourceDisk")) not in known
    )

    reuse, blocker, notes = judge_disk(plan, source_disk, snapshot, disk)
    found = Found(
        source_disk=source_disk, snapshot=snapshot, disk=disk, instance=instance,
        spare_snapshots=spares, unrelated_snapshots=unrelated,
        reuse_disk=reuse, blocker=blocker, notes=notes,
    )

    # A disk attached to the target instance is that instance's boot disk, which
    # is the finished state rather than a blocker.
    if instance is not None:
        found = replace(found, reuse_disk=disk is not None, blocker=None)

    if instance is None:
        missing = zone_lacks_machine_type(gc, plan)
        if missing:
            found = replace(found, blocker=missing)
    return found


def prepare(gc: Gcloud, host: Host, instance: dict, to_zone: str) -> tuple[Plan, Found]:
    """Name what the move will create, and read what is already there. Read-only.

    One call, because the two halves depend on each other: the plan says which
    boot disk to look for, and the lookup supplies the disk type the plan has to
    carry across. A command that did this in two steps would have to know that,
    and would eventually get it wrong.
    """
    plan = plan_move(host, instance, to_zone)
    found = survey(gc, plan)
    if found.source_disk is not None:
        plan = replace(plan, disk_type=_tail(found.source_disk.get("type")) or None)
    return plan, found


def blocked(plan: Plan, found: Found) -> MoveError | None:
    """The reason this move cannot start, if there is one. Nothing has happened yet.

    Shared by the preview and the run so a dry run cannot report a plan the real
    run would refuse to carry out.
    """
    if not found.blocker:
        return None
    if found.disk is not None:
        return MoveError(
            found.blocker,
            fix=("check it is not something you want, then remove it and run this "
                 f"again:\n        {delete_disk_command(plan)}"),
            left=(f"the disk {plan.new_disk} in {plan.to_zone}",),
            cleanup=(delete_disk_command(plan),),
        )
    return MoveError(
        found.blocker,
        fix=("pick a zone that has this machine type:\n        "
             "gcloud compute machine-types list "
             f"--filter='name={plan.machine_type}' --project={plan.project}"),
    )


def zone_lacks_machine_type(gc: Gcloud, plan: Plan) -> str | None:
    """Is the machine type even offered in the destination? Seconds, not minutes.

    This is the only part of "will the instance create succeed" that can be
    checked in advance. Whether the zone has one *free* right now cannot: Google
    publishes no capacity API, and the zone it suggests in a stockout message can
    be out again by the time you get there — which is exactly how a move once
    paid for a 300 GB snapshot and a 300 GB disk and then failed on the last
    step. What this does catch is asking for a card in a zone that never has one,
    which fails instantly instead of after the slow half of the work.
    """
    try:
        offered = gc.run([
            "compute", "machine-types", "list",
            f"--zones={plan.to_zone}", f"--project={plan.project}",
            f"--filter=name={plan.machine_type}",
        ]) or []
    except GcloudError:
        # A check that cannot run is not a reason to refuse the move.
        return None
    if offered:
        return None
    return (
        f"{plan.to_zone} does not offer {plan.machine_type} at all, so creating "
        f"{plan.new_instance} there cannot work. Nothing has been snapshotted."
    )


# --- leftovers ------------------------------------------------------------

def delete_disk_command(plan: Plan) -> str:
    return (f"gcloud compute disks delete {plan.new_disk} --zone={plan.to_zone} "
            f"--project={plan.project} --quiet")


def delete_instance_command(plan: Plan) -> str:
    """Remove the box a move left behind, and its disk with it.

    `--delete-disks=all` is deliberate. A moved-from box is created with
    `auto-delete=no` on its boot disk, so deleting the instance alone leaves a
    200-300 GB disk billing with nothing attached to it — which looks like
    nothing at all in the console, and is the leftover people actually get
    caught by. Handed over, never run: this destroys an install.
    """
    return (f"gcloud compute instances delete {plan.host.gce_instance} "
            f"--zone={plan.host.gce_zone} --project={plan.project} "
            "--delete-disks=all --quiet")


def delete_snapshot_command(plan: Plan, name: str | None = None) -> str:
    return (f"gcloud compute snapshots delete {name or plan.snapshot} "
            f"--project={plan.project} --quiet")


def leftovers(plan: Plan, found: Found) -> list[str]:
    """What earlier runs left behind, what it is, and what removes it.

    A part-finished move bills in silence: an unattached 300 GB disk and a 21 GB
    snapshot look like nothing in the console. Naming the size and handing over
    the delete line is the least this can do, whether or not the move carries on.
    """
    lines: list[str] = []
    if found.disk is not None and found.instance is None:
        note = describe_disk(found.disk)
        lines.append(
            f"disk {plan.new_disk} in {plan.to_zone}"
            + (f" ({note})" if note else "")
            + " — attached to nothing, billing since "
            + (found.disk.get("creationTimestamp") or "an earlier run").split("T")[0]
        )
        lines.append(f"  {delete_disk_command(plan)}")
    for snap in ([found.snapshot] if found.snapshot is not None else []) + list(found.spare_snapshots):
        note = describe_snapshot(snap)
        lines.append(
            f"snapshot {snap.get('name')}" + (f" ({note})" if note else "")
            + " — from an earlier move, billing"
        )
        lines.append(f"  {delete_snapshot_command(plan, snap.get('name'))}")
    for snap in found.unrelated_snapshots:
        note = describe_snapshot(snap)
        lines.append(
            f"snapshot {snap.get('name')}" + (f" ({note})" if note else "")
            + " — its source disk no longer exists. Not this move's, not touched "
              "by --clean; delete it yourself if you do not want it."
        )
        lines.append(f"  {delete_snapshot_command(plan, snap.get('name'))}")
    if found.instance is not None:
        lines.append(
            f"{plan.new_instance} already exists in {plan.to_zone} — an earlier "
            "move got this far."
        )
    return lines


def remove_leftovers(gc: Gcloud, plan: Plan, found: Found,
                     say: Callable[[str], None]) -> list[str]:
    """Delete what earlier runs of *this* move left. Only ever because someone said so.

    Never automatic and never wider than this move: snapshots belonging to some
    other disk are reported and left alone. A 300 GB disk is somebody's install,
    and the cost of deleting one that mattered is far above the cost of leaving
    one that did not.
    """
    removed: list[str] = []
    if found.disk is not None and found.instance is None:
        say(f"deleting {plan.new_disk} in {plan.to_zone}")
        gc.run([
            "compute", "disks", "delete", plan.new_disk,
            f"--zone={plan.to_zone}", f"--project={plan.project}", "--quiet",
        ], parse_json=False)
        removed.append(plan.new_disk)
    for snap in ([found.snapshot] if found.snapshot is not None else []) + list(found.spare_snapshots):
        name = snap.get("name")
        say(f"deleting the snapshot {name}")
        gc.delete_snapshot(name, plan.project)
        removed.append(name)
    return removed


# --- doing it ------------------------------------------------------------

def _state_after(plan: Plan, found: Found, done: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """What exists because of this run, and the commands that remove it."""
    left: list[str] = []
    cleanup: list[str] = []
    if (SNAPSHOT in done or REUSE_SNAPSHOT in done) and DELETE_SNAPSHOT not in done:
        name = found.snapshot_name or plan.snapshot
        left.append(f"the snapshot {name}")
        cleanup.append(delete_snapshot_command(plan, name))
    if CREATE_DISK in done or REUSE_DISK in done:
        left.append(f"the disk {plan.new_disk} in {plan.to_zone} "
                    f"({plan.disk_type or GCLOUD_DEFAULT_DISK_TYPE})")
        cleanup.append(delete_disk_command(plan))
    if CREATE_INSTANCE in done or REUSE_INSTANCE in done:
        left.append(f"the instance {plan.new_instance} in {plan.to_zone}")
    return tuple(left), tuple(cleanup)


# The types that count against SSD_TOTAL_GB. pd-standard does not, which is why
# it is the fallback when that allowance is full.
_SSD_TYPES = ("pd-balanced", "pd-ssd", "pd-extreme", "hyperdisk-balanced")


def _create_disk(gc: Gcloud, plan: Plan, snapshot: str,
                 say: Callable[[str], None] | None = None) -> None:
    """Create the new disk, keeping the source disk's type where that is allowed.

    `gcloud compute disks create` defaults to pd-standard when `--type` is left
    off, so a pd-balanced boot disk used to come out of a move slower than it
    went in, silently. Passing the type fixed that and introduced a second
    failure: pd-balanced counts against SSD_TOTAL_GB, and a 300 GB copy of a
    300 GB disk needs 600 GB under an allowance that is 500 by default. The move
    then died at its most expensive step, having already made the snapshot.

    So: ask for the matching type, and if the only thing standing in the way is
    that allowance, take the slower type rather than failing — and say so, with
    the way to get the fast one. A finished move on a slower disk is worth more
    than no move at all, and the difference is now the user's to decide rather
    than something they discover.
    """
    def build(disk_type: str | None) -> list[str]:
        args = [
            "compute", "disks", "create", plan.new_disk,
            f"--zone={plan.to_zone}", f"--project={plan.project}",
            f"--source-snapshot={snapshot}",
        ]
        if disk_type:
            args.append(f"--type={disk_type}")
        return args

    try:
        gc.run(build(plan.disk_type), parse_json=False, timeout=300)
        return
    except GcloudError as exc:
        fits = plan.disk_type in _SSD_TYPES and getattr(exc, "kind", "") == QUOTA
        if not fits:
            raise

    if say:
        say(f"no room under this project's SSD allowance for a "
            f"{plan.disk_type} disk — using pd-standard instead")
        say("  the box will boot and load models more slowly than the original")
        say("  to get a matching disk: raise SSD_TOTAL_GB, delete "
            f"{plan.new_disk}, and run this again")
    gc.run(build("pd-standard"), parse_json=False, timeout=300)


def run_move(
    gc: Gcloud,
    plan: Plan,
    found: Found,
    say: Callable[[str], None],
    *,
    register: Callable[[Plan], None],
) -> Outcome:
    """Carry out exactly the actions the plan printed, in that order.

    Every failure raises MoveError carrying what now exists and what it costs.
    Re-running is the supported recovery: `survey` finds whatever got made and
    the next run carries on from there.
    """
    problem = blocked(plan, found)
    if problem is not None:
        raise problem

    project = plan.project
    host = plan.host
    snapshot_name = found.snapshot_name or plan.snapshot
    done: list[str] = []
    warnings: list[str] = []

    for action in plan.actions(found):
        say(action.line)
        try:
            if action.kind == SNAPSHOT:
                gc.snapshot_disk(plan.source_disk, host.gce_zone, project, plan.snapshot)
            elif action.kind == CREATE_DISK:
                _create_disk(gc, plan, snapshot_name, say)
            elif action.kind == CREATE_INSTANCE:
                gc.create_instance_from_disk(
                    plan.new_instance, plan.to_zone, project, plan.new_disk,
                    plan.machine_type, plan.metadata,
                    accelerator=plan.accelerator,
                    external_ip=plan.external_ip,
                    network=plan.network, subnet=plan.subnet,
                )
            elif action.kind == DELETE_SNAPSHOT:
                gc.delete_snapshot(snapshot_name, project)
            elif action.kind == REGISTER:
                register(plan)
            elif action.kind == LEAVE:
                # Deliberately nothing. A move leaves the source alone, and the
                # user may still be working on it. Before this branch existed the
                # step fell through the dispatch and was recorded as done, which
                # is how "leave comfy-win stopped" came to be printed about a box
                # that was running.
                pass
        except GcloudError as exc:
            # The box exists by this point, so a snapshot that will not delete is
            # a bill to hand over, not a reason to call a finished move a failure.
            if action.kind == DELETE_SNAPSHOT:
                warnings.append(
                    f"could not delete the snapshot {snapshot_name}: {exc}. It is "
                    f"still billing — {delete_snapshot_command(plan, snapshot_name)}"
                )
                continue
            raise _stopped(plan, found, done, action, exc) from exc
        done.append(action.kind)

    return Outcome(done=tuple(done), warnings=tuple(warnings))


def _stopped(plan: Plan, found: Found, done: list[str], action: Action,
             exc: GcloudError) -> MoveError:
    """Turn a failed step into a message that says what it left and what it cost."""
    from .lifecycle import is_capacity_failure, suggested_zones

    left, cleanup = _state_after(plan, found, done)

    if action.kind == CREATE_INSTANCE and is_capacity_failure(exc.raw or str(exc)):
        # The worst shape a move can fail in: the slow, paid-for half is done and
        # there is no machine. The zone Google suggested during the stockout is a
        # hint, not a reservation, and it goes stale — this is that failure.
        elsewhere = [z for z in suggested_zones(exc.raw or "") if z != plan.to_zone]
        retry = (
            f"comfy-qat move {plan.host.name} --to {elsewhere[0]}"
            if elsewhere else
            f"comfy-qat move {plan.host.name} --to <another zone>"
        )
        return MoveError(
            f"{plan.to_zone} has no {plan.host.gpu or 'GPU'} capacity either, so "
            f"{plan.new_instance} could not be created. The zone Google named was "
            "free when it said so and is not now; that is normal and not a fault "
            "on your side.",
            fix=(
                "try another zone — the snapshot is kept, so this repeats only the "
                f"disk, not the 300 GB copy:\n        {retry}"
                + ("\n        or stop here and take the cost off the bill:\n        "
                   + "\n        ".join(cleanup) if cleanup else "")
            ),
            left=left,
            cleanup=cleanup,
        )

    return MoveError(
        f"the move stopped at: {action.line} ({exc})",
        fix=(
            "run the same command again — it will find what already exists and "
            "carry on from there."
            + ("\n        to start over instead:\n        "
               + "\n        ".join(cleanup) if cleanup else "")
        ),
        left=left,
        cleanup=cleanup,
    )
