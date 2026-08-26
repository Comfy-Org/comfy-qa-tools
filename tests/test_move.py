"""Carrying out a move, and surviving one that stops half-way.

Every fixture here is shaped like the real `gcloud ... list --format=json` output
from the project this was written against, full resource URLs and string sizes
included, because hand-written fixtures have already cost this project three
bugs. The state is the one that was actually on that project on 2026-08-26:

    disks       comfy-win-a    us-central1-a  300 GB  pd-balanced  (comfy-win)
                comfy-win-a-b  us-central1-b  300 GB  pd-standard  (nothing)
    snapshots   comfy-win-a-move-b   300 GB, 20.9 GB stored, of comfy-win-a
                comfy-win-snap       300 GB, 18.3 GB stored, of comfy-win

`comfy-win-a-b` is what a move that died left behind: the snapshot was taken at
07:33 and the disk made at 07:38 on 2026-08-25, and no instance was ever created.
It billed for a day and blocked every later attempt, because the second run tried
to create a disk whose name was already taken.

The fake cloud below is stateful on purpose. A move is judged by what exists when
it finishes — or when it stops — and a fake that only records calls cannot answer
that question.
"""

from __future__ import annotations

import pytest

from comfy_qa.config import Host
from comfy_qa.gcloud import Gcloud, GcloudError
from comfy_qa.relocate import (
    CREATE_DISK,
    CREATE_INSTANCE,
    DELETE_SNAPSHOT,
    REGISTER,
    REUSE_DISK,
    REUSE_SNAPSHOT,
    SNAPSHOT,
    MoveError,
    blocked,
    judge_disk,
    leftovers,
    plan_move,
    prepare,
    remove_leftovers,
    run_move,
    survey,
)

PROJECT = "stately-timing-504610-p1"
URL = f"https://www.googleapis.com/compute/v1/projects/{PROJECT}"

WIN = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022", gpu="L4",
           gce_instance="comfy-win", gce_zone="us-central1-a", gce_project=PROJECT)

# The boot disk is `comfy-win-a`, not `comfy-win`. That is the whole reason
# defect one existed, so it is the default everywhere in this file.
INSTANCE = {
    "name": "comfy-win",
    "status": "TERMINATED",
    "zone": f"{URL}/zones/us-central1-a",
    "machineType": f"{URL}/zones/us-central1-a/machineTypes/g2-standard-8",
    "disks": [
        {"boot": True, "source": f"{URL}/zones/us-central1-a/disks/comfy-win-a",
         "deviceName": "persistent-disk-0"},
    ],
    "metadata": {"items": [{"key": "enable-windows-ssh", "value": "TRUE"}]},
}


def disk(name, zone, *, size="300", kind="pd-balanced", from_snapshot=None,
         users=(), created="2026-08-05T07:46:02.586-07:00"):
    made = {
        "name": name,
        "zone": f"{URL}/zones/{zone}",
        "sizeGb": size,
        "type": f"{URL}/zones/{zone}/diskTypes/{kind}",
        "status": "READY",
        "creationTimestamp": created,
    }
    if from_snapshot:
        made["sourceSnapshot"] = f"{URL}/global/snapshots/{from_snapshot}"
    if users:
        made["users"] = [f"{URL}/zones/{zone}/instances/{u}" for u in users]
    return made


def snapshot(name, source, *, size="300", stored="22475608320",
             created="2026-08-25T07:33:34.373-07:00", status="READY"):
    return {
        "name": name,
        "sourceDisk": f"{URL}/zones/us-central1-a/disks/{source}",
        "diskSizeGb": size,
        "storageBytes": stored,
        "status": status,
        "creationTimestamp": created,
    }


SOURCE = disk("comfy-win-a", "us-central1-a", users=("comfy-win",))
ORPHAN_DISK = disk("comfy-win-a-b", "us-central1-b", kind="pd-standard",
                   from_snapshot="comfy-win-a-move-b",
                   created="2026-08-25T07:38:24.167-07:00")
ORPHAN_SNAPSHOT = snapshot("comfy-win-a-move-b", "comfy-win-a")
ANCESTOR_SNAPSHOT = snapshot("comfy-win-snap", "comfy-win", stored="19689640768",
                             created="2026-08-05T07:37:48.066-07:00")


class Cloud:
    """A scripted gcloud that keeps its own world, so state can be asserted.

    `fail` maps the start of a command to the error it raises, which is how a
    failure is put at one specific step without touching the others.
    """

    def __init__(self, *, disks=(), snapshots=(), instances=(), fail=None,
                 machine_types=("g2-standard-8",)):
        self.disks = [dict(d) for d in disks]
        self.snapshots = [dict(s) for s in snapshots]
        self.instances = [dict(i) for i in instances]
        self.machine_types = list(machine_types)
        self.fail = dict(fail or {})
        self.calls: list[str] = []

    # --- the seam -------------------------------------------------------
    def gcloud(self) -> Gcloud:
        return Gcloud(runner=self._run)

    def _run(self, args, mode):
        key = " ".join(args)
        self.calls.append(key)
        for prefix, exc in self.fail.items():
            if key.startswith(prefix):
                raise exc
        return self._dispatch(args, key)

    def _dispatch(self, args, key):
        if key.startswith("compute disks list"):
            return list(self.disks)
        if key.startswith("compute snapshots list"):
            return list(self.snapshots)
        if key.startswith("compute instances list"):
            return list(self.instances)
        if key.startswith("compute instances describe"):
            # What the command calls to learn the box's boot disk and machine type.
            there = self.find_instance(args[3])
            if there is None:
                raise GcloudError(f"The resource '{args[3]}' was not found")
            return there
        if key.startswith("compute machine-types list"):
            wanted = self._flag(args, "--filter").removeprefix("name=")
            zone = self._flag(args, "--zones")
            return ([{"name": wanted, "zone": zone}]
                    if wanted in self.machine_types else [])
        if key.startswith("compute disks snapshot"):
            return self._take_snapshot(args)
        if key.startswith("compute disks create"):
            return self._create_disk(args)
        if key.startswith("compute instances create"):
            return self._create_instance(args)
        if key.startswith("compute snapshots delete"):
            self.snapshots = [s for s in self.snapshots if s["name"] != args[3]]
            return ""
        if key.startswith("compute disks delete"):
            self.disks = [d for d in self.disks if d["name"] != args[3]]
            return ""
        raise AssertionError(f"unexpected gcloud call: {key}")

    # --- the world ------------------------------------------------------
    @staticmethod
    def _flag(args, name, default=""):
        for arg in args:
            if arg.startswith(f"{name}="):
                return arg.split("=", 1)[1]
        return default

    def _take_snapshot(self, args):
        source = self.find_disk(args[3])
        assert source is not None, f"snapshotting a disk that is not there: {args[3]}"
        name = self._flag(args, "--snapshot-names")
        assert not any(s["name"] == name for s in self.snapshots), "name already taken"
        self.snapshots.append(snapshot(name, source["name"],
                                       size=source["sizeGb"],
                                       created="2026-08-26T09:00:00.000-07:00"))
        return ""

    def _create_disk(self, args):
        name = args[3]
        zone = self._flag(args, "--zone")
        source = self._flag(args, "--source-snapshot")
        assert any(s["name"] == source for s in self.snapshots), (
            f"creating a disk from a snapshot that is not there: {source}"
        )
        if self.find_disk(name, zone):
            # Exactly what the real project answered on the second attempt.
            raise GcloudError(
                f"The resource 'projects/{PROJECT}/zones/{zone}/disks/{name}' "
                f"already exists",
                raw="ERROR: (gcloud.compute.disks.create) Could not fetch resource: "
                    f" - The resource '.../disks/{name}' already exists",
            )
        self.disks.append(disk(
            name, zone,
            kind=self._flag(args, "--type", "pd-standard"),
            from_snapshot=source,
            created="2026-08-26T09:05:00.000-07:00",
        ))
        return ""

    def _create_instance(self, args):
        name = args[3]
        zone = self._flag(args, "--zone")
        self.instances.append({
            "name": name, "status": "RUNNING", "zone": f"{URL}/zones/{zone}",
        })
        return ""

    # --- questions the tests ask ----------------------------------------
    def find_disk(self, name, zone=None):
        for d in self.disks:
            if d["name"] == name and (zone is None or d["zone"].endswith(zone)):
                return d
        return None

    def find_snapshot(self, name):
        return next((s for s in self.snapshots if s["name"] == name), None)

    def find_instance(self, name):
        return next((i for i in self.instances if i["name"] == name), None)

    def ran(self, prefix):
        return [c for c in self.calls if c.startswith(prefix)]


def recorder():
    lines: list[str] = []
    return lines, lines.append


def registrations():
    added: list[str] = []
    return added, lambda plan: added.append(plan.new_instance)


def stockout(zone="us-central1-b", suggest="us-central1-c"):
    return GcloudError(
        "---",
        raw=(
            "ERROR: (gcloud.compute.instances.create) Could not fetch resource:\n"
            " - The zone 'projects/p/zones/" + zone + "' does not have enough "
            "resources available to fulfill the request. Try a different zone, or "
            "try again later. Consider trying your request in the " + suggest +
            " zone."
        ),
    )


def prepared(cloud=None):
    """A surveyed plan, through the one call the command makes."""
    cloud = cloud or Cloud(disks=[SOURCE], instances=[INSTANCE])
    gc = cloud.gcloud()
    plan, found = prepare(gc, WIN, INSTANCE, "us-central1-b")
    return cloud, gc, plan, found


# --- the boot disk is read, not guessed ----------------------------------

def test_the_disk_that_is_snapshotted_is_the_real_boot_disk():
    """comfy-win boots from comfy-win-a. Chopping the new disk's name apart gave
    the right answer here by luck, and would have snapshotted nothing on a box
    whose disk is named anything else."""
    cloud, gc, plan, found = prepared()
    _, say = recorder()
    added, register = registrations()

    run_move(gc, plan, found, say, register=register)

    took = cloud.ran("compute disks snapshot")
    assert len(took) == 1
    assert took[0].split()[3] == "comfy-win-a", "the boot disk, not the instance name"


def test_a_boot_disk_named_nothing_like_the_instance_still_moves():
    """The case the old string surgery could not survive at all."""
    instance = dict(INSTANCE, disks=[
        {"boot": False, "source": f"{URL}/zones/us-central1-a/disks/comfy-win-scratch"},
        {"boot": True, "source": f"{URL}/zones/us-central1-a/disks/windows-install-2024"},
    ])
    source = disk("windows-install-2024", "us-central1-a", users=("comfy-win",))
    cloud = Cloud(disks=[source, disk("comfy-win-scratch", "us-central1-a")],
                  instances=[instance])
    gc = cloud.gcloud()
    plan, found = prepare(gc, WIN, instance, "us-central1-b")

    assert plan.source_disk == "windows-install-2024"
    assert plan.new_disk == "windows-install-2024-b"
    assert plan.new_instance == "comfy-win-b", "the instance keeps the box's name"

    _, say = recorder()
    added, register = registrations()
    run_move(gc, plan, found, say, register=register)

    assert cloud.ran("compute disks snapshot")[0].split()[3] == "windows-install-2024"
    assert cloud.find_disk("windows-install-2024-b", "us-central1-b")
    assert cloud.find_instance("comfy-win-b")


# --- a clean move --------------------------------------------------------

def test_a_clean_move_does_each_step_once_and_leaves_no_snapshot():
    cloud, gc, plan, found = prepared()
    lines, say = recorder()
    added, register = registrations()

    outcome = run_move(gc, plan, found, say, register=register)

    assert outcome.done == (SNAPSHOT, CREATE_DISK, CREATE_INSTANCE,
                            DELETE_SNAPSHOT, REGISTER, "leave")
    assert added == ["comfy-win-b"]
    assert cloud.find_disk("comfy-win-a-b", "us-central1-b")
    assert cloud.find_instance("comfy-win-b")
    assert cloud.find_snapshot("comfy-win-a-move") is None, (
        "a 300 GB snapshot nothing reads is a bill"
    )
    assert cloud.find_disk("comfy-win-a", "us-central1-a"), "the original is untouched"
    assert cloud.find_instance("comfy-win")


def test_the_new_disk_keeps_the_type_of_the_one_it_copies():
    """gcloud defaults to pd-standard. A move that quietly downgrades a
    pd-balanced boot disk hands back a slower box and never says why."""
    cloud, gc, plan, found = prepared()
    _, say = recorder()
    _, register = registrations()

    run_move(gc, plan, found, say, register=register)

    assert "--type=pd-balanced" in cloud.ran("compute disks create")[0]
    assert cloud.find_disk("comfy-win-a-b")["type"].endswith("pd-balanced")


def test_windows_ssh_metadata_reaches_the_new_box():
    cloud, gc, plan, found = prepared()
    _, say = recorder()
    _, register = registrations()
    run_move(gc, plan, found, say, register=register)
    assert "--metadata=enable-windows-ssh=TRUE" in cloud.ran("compute instances create")[0]


# --- the dry run is the run ----------------------------------------------

def test_the_dry_run_prints_exactly_what_the_real_run_then_does():
    """Two code paths describing one process is how a preview starts lying. The
    plan and the run come off one list of actions, and this is what holds them
    together."""
    cloud, gc, plan, found = prepared()
    preview = plan.steps(found)

    lines, say = recorder()
    _, register = registrations()
    run_move(gc, plan, found, say, register=register)

    assert lines == preview


def test_the_dry_run_previews_a_resumed_move_too():
    cloud, gc, plan, found = prepared(Cloud(
        disks=[SOURCE, ORPHAN_DISK], snapshots=[ORPHAN_SNAPSHOT], instances=[INSTANCE],
    ))
    preview = plan.steps(found)
    lines, say = recorder()
    _, register = registrations()
    run_move(gc, plan, found, say, register=register)
    assert lines == preview


def test_every_line_of_the_plan_is_a_step_that_happens():
    """Not decoration: each line maps to one action the runner dispatches on."""
    _, _, plan, found = prepared()
    kinds = [a.kind for a in plan.actions(found)]
    assert kinds == [SNAPSHOT, CREATE_DISK, CREATE_INSTANCE, DELETE_SNAPSHOT,
                     REGISTER, "leave"]
    assert all(a.line.strip() for a in plan.actions(found))


# --- resuming ------------------------------------------------------------

def test_the_disk_a_dead_run_left_is_reused_not_recreated():
    """The real case. Before this, the second attempt died on 'already exists'."""
    cloud, gc, plan, found = prepared(Cloud(
        disks=[SOURCE, ORPHAN_DISK], snapshots=[ORPHAN_SNAPSHOT, ANCESTOR_SNAPSHOT],
        instances=[INSTANCE],
    ))

    assert found.reuse_disk is True
    assert found.blocker is None

    lines, say = recorder()
    added, register = registrations()
    outcome = run_move(gc, plan, found, say, register=register)

    assert outcome.reused_disk()
    assert outcome.done == (REUSE_DISK, CREATE_INSTANCE, DELETE_SNAPSHOT, REGISTER,
                            "leave")
    assert cloud.ran("compute disks snapshot") == [], "no second 300 GB copy"
    assert cloud.ran("compute disks create") == []
    assert cloud.find_instance("comfy-win-b")
    assert added == ["comfy-win-b"]
    assert any("reuse the disk comfy-win-a-b" in line for line in lines)


def test_reusing_a_disk_of_the_wrong_type_says_so_rather_than_hiding_it():
    """The leftover is pd-standard and the original is pd-balanced. Reusing it is
    still the cheap, right default — but a QA box that is quietly slower than the
    one it replaces is the exact thing this tool exists to prevent."""
    _, _, _, found = prepared(Cloud(
        disks=[SOURCE, ORPHAN_DISK], snapshots=[ORPHAN_SNAPSHOT], instances=[INSTANCE],
    ))
    assert found.reuse_disk is True
    note = " ".join(found.notes)
    assert "pd-standard" in note and "pd-balanced" in note
    assert "slower" in note
    assert "gcloud compute disks delete comfy-win-a-b" in note


def test_a_snapshot_without_a_disk_is_reused_and_the_snapshot_is_not_retaken():
    """The run died between step one and step two — the slow half is already paid
    for."""
    cloud, gc, plan, found = prepared(Cloud(
        disks=[SOURCE], snapshots=[ORPHAN_SNAPSHOT, ANCESTOR_SNAPSHOT],
        instances=[INSTANCE],
    ))

    lines, say = recorder()
    _, register = registrations()
    outcome = run_move(gc, plan, found, say, register=register)

    assert outcome.done == (REUSE_SNAPSHOT, CREATE_DISK, CREATE_INSTANCE,
                            DELETE_SNAPSHOT, REGISTER, "leave")
    assert cloud.ran("compute disks snapshot") == []
    assert "--source-snapshot=comfy-win-a-move-b" in cloud.ran("compute disks create")[0]
    assert cloud.find_snapshot("comfy-win-a-move-b") is None, "cleaned up on the way out"
    assert cloud.find_snapshot("comfy-win-snap"), "someone else's snapshot is not ours"


def test_an_instance_that_already_exists_is_kept_and_only_registered():
    cloud, gc, plan, found = prepared(Cloud(
        disks=[SOURCE, ORPHAN_DISK], snapshots=[ORPHAN_SNAPSHOT],
        instances=[INSTANCE, {"name": "comfy-win-b", "status": "RUNNING",
                              "zone": f"{URL}/zones/us-central1-b"}],
    ))
    _, say = recorder()
    added, register = registrations()

    outcome = run_move(gc, plan, found, say, register=register)

    assert outcome.reused_instance()
    assert cloud.ran("compute instances create") == []
    assert added == ["comfy-win-b"]
    assert cloud.find_snapshot("comfy-win-a-move-b") is None


# --- refusing to reuse the wrong disk ------------------------------------

@pytest.mark.parametrize("bad, because", [
    (disk("comfy-win-a-b", "us-central1-b", from_snapshot="comfy-win-a-move-b",
          users=("something-else",)), "attached to"),
    (disk("comfy-win-a-b", "us-central1-b"), "nothing records"),
    (disk("comfy-win-a-b", "us-central1-b", from_snapshot="someone-elses-backup"),
     "not a snapshot this move takes"),
    (disk("comfy-win-a-b", "us-central1-b", size="200",
          from_snapshot="comfy-win-a-move-b",
          created="2026-08-25T07:38:24.167-07:00"), "not a copy of this boot disk"),
    (disk("comfy-win-a-b", "us-central1-b", from_snapshot="comfy-win-a-move-b",
          created="2026-08-01T00:00:00.000-07:00"), "predates the snapshot"),
])
def test_a_disk_that_cannot_be_confirmed_is_refused_with_the_command_to_remove_it(bad, because):
    """Carrying on with the wrong disk boots the wrong machine and looks exactly
    like a move that worked."""
    cloud, gc, plan, found = prepared(Cloud(
        disks=[SOURCE, bad], snapshots=[ORPHAN_SNAPSHOT], instances=[INSTANCE],
    ))

    assert found.reuse_disk is False
    assert because in found.blocker

    _, say = recorder()
    _, register = registrations()
    with pytest.raises(MoveError) as caught:
        run_move(gc, plan, found, say, register=register)

    assert because in str(caught.value)
    assert "gcloud compute disks delete comfy-win-a-b" in caught.value.fix
    assert cloud.calls[-1].startswith("compute machine-types list"), (
        "it refused before touching anything"
    )


def test_judge_disk_says_nothing_when_there_is_no_disk():
    plan = plan_move(WIN, INSTANCE, "us-central1-b", source_disk=SOURCE)
    assert judge_disk(plan, SOURCE, None, None) == (False, None, ())


def test_a_zone_that_does_not_offer_the_card_fails_before_the_slow_part():
    """No capacity API exists, so whether a zone has one *free* cannot be checked
    ahead. Whether it has the type at all can, and that check costs seconds."""
    cloud, gc, plan, found = prepared(
        Cloud(disks=[SOURCE], instances=[INSTANCE], machine_types=[]))

    assert "does not offer g2-standard-8" in found.blocker
    assert "Nothing has been snapshotted" in found.blocker
    assert "machine-types list" in blocked(plan, found).fix

    _, say = recorder()
    _, register = registrations()
    # `MoveError` has many raise sites, so catching one proves nothing about
    # which rule fired. Assert the message: another rule stopping the run first
    # would leave this test green while the check it is named for was gone.
    with pytest.raises(MoveError, match="does not offer g2-standard-8"):
        run_move(gc, plan, found, say, register=register)
    assert cloud.ran("compute disks snapshot") == [], "and before the slow part"


def test_what_stops_the_run_is_what_the_preview_reports():
    """A dry run that shows a tidy plan the real run then refuses is worse than
    no preview at all."""
    cloud, gc, plan, found = prepared(Cloud(
        disks=[SOURCE, disk("comfy-win-a-b", "us-central1-b")],
        snapshots=[ORPHAN_SNAPSHOT], instances=[INSTANCE],
    ))
    preview = blocked(plan, found)
    assert preview is not None

    _, say = recorder()
    _, register = registrations()
    with pytest.raises(MoveError) as caught:
        run_move(gc, plan, found, say, register=register)

    assert str(caught.value) == str(preview)
    assert caught.value.fix == preview.fix
    assert caught.value.cleanup == preview.cleanup


# --- failing at each step, and what is left -------------------------------

def test_a_failure_taking_the_snapshot_leaves_nothing_behind():
    cloud, gc, plan, found = prepared(Cloud(
        disks=[SOURCE], instances=[INSTANCE],
        fail={"compute disks snapshot": GcloudError("disk is busy")},
    ))
    _, say = recorder()
    added, register = registrations()

    with pytest.raises(MoveError) as caught:
        run_move(gc, plan, found, say, register=register)

    assert caught.value.left == ()
    assert caught.value.cleanup == ()
    assert "run the same command again" in caught.value.fix
    assert cloud.snapshots == [] and added == []
    assert cloud.find_disk("comfy-win-a"), "the original is untouched"


def test_a_failure_creating_the_disk_leaves_the_snapshot_and_names_its_cost():
    cloud, gc, plan, found = prepared(Cloud(
        disks=[SOURCE], instances=[INSTANCE],
        fail={"compute disks create": GcloudError("quota exceeded")},
    ))
    _, say = recorder()
    added, register = registrations()

    with pytest.raises(MoveError) as caught:
        run_move(gc, plan, found, say, register=register)

    assert caught.value.left == ("the snapshot comfy-win-a-move",)
    assert caught.value.cleanup == (
        f"gcloud compute snapshots delete comfy-win-a-move --project={PROJECT} --quiet",
    )
    assert cloud.find_snapshot("comfy-win-a-move"), "kept, so a retry is cheap"
    assert cloud.find_disk("comfy-win-a-b") is None
    assert added == []


def test_a_failure_creating_the_instance_leaves_both_and_says_so():
    """The failure that actually happened, and the worst shape there is: the slow
    half is paid for and there is no machine."""
    cloud, gc, plan, found = prepared(Cloud(
        disks=[SOURCE], instances=[INSTANCE],
        fail={"compute instances create": GcloudError("boom")},
    ))
    _, say = recorder()
    added, register = registrations()

    with pytest.raises(MoveError) as caught:
        run_move(gc, plan, found, say, register=register)

    assert caught.value.left == (
        "the snapshot comfy-win-a-move",
        "the disk comfy-win-a-b in us-central1-b (pd-balanced)",
    )
    assert any("disks delete comfy-win-a-b" in c for c in caught.value.cleanup)
    assert any("snapshots delete comfy-win-a-move" in c for c in caught.value.cleanup)
    assert cloud.find_disk("comfy-win-a-b", "us-central1-b"), "it really is there"
    assert cloud.find_instance("comfy-win-b") is None
    assert added == [], "nothing goes in the host list without a machine"


def test_a_stockout_in_the_destination_names_another_zone_and_keeps_the_snapshot():
    """Google's suggested zone is a hint that goes stale, not a reservation. When
    it goes stale at the last step, say what was paid for and what to do next."""
    cloud, gc, plan, found = prepared(Cloud(
        disks=[SOURCE], instances=[INSTANCE],
        fail={"compute instances create": stockout()},
    ))
    _, say = recorder()
    _, register = registrations()

    with pytest.raises(MoveError) as caught:
        run_move(gc, plan, found, say, register=register)

    message = str(caught.value)
    assert "no L4 capacity either" in message
    assert "not a fault on your side" in message
    assert "comfy-qat host move comfy-win --to us-central1-c" in caught.value.fix
    assert "the snapshot is kept" in caught.value.fix
    assert cloud.find_snapshot("comfy-win-a-move"), (
        "the expensive half survives, so another zone costs only a disk"
    )


def test_a_failure_writing_the_host_list_still_leaves_a_usable_machine():
    cloud, gc, plan, found = prepared()
    _, say = recorder()

    def register(plan):
        raise GcloudError("hosts.toml is read-only")

    with pytest.raises(MoveError) as caught:
        run_move(gc, plan, found, say, register=register)

    assert "the instance comfy-win-b in us-central1-b" in caught.value.left
    assert cloud.find_instance("comfy-win-b")


def test_a_snapshot_that_will_not_delete_is_a_bill_not_a_failed_move():
    """The box exists by then. Failing the whole move over its cleanup would send
    someone hunting for a machine that is already there."""
    cloud, gc, plan, found = prepared(Cloud(
        disks=[SOURCE], instances=[INSTANCE],
        fail={"compute snapshots delete": GcloudError("snapshot is in use")},
    ))
    _, say = recorder()
    added, register = registrations()

    outcome = run_move(gc, plan, found, say, register=register)

    assert added == ["comfy-win-b"]
    assert len(outcome.warnings) == 1
    assert "still billing" in outcome.warnings[0]
    assert "gcloud compute snapshots delete comfy-win-a-move" in outcome.warnings[0]


# --- reporting what is lying around --------------------------------------

def test_the_orphan_is_reported_with_its_size_and_the_command_that_removes_it():
    _, _, plan, found = prepared(Cloud(
        disks=[SOURCE, ORPHAN_DISK], snapshots=[ORPHAN_SNAPSHOT, ANCESTOR_SNAPSHOT],
        instances=[INSTANCE],
    ))
    report = "\n".join(leftovers(plan, found))

    assert "comfy-win-a-b" in report and "300 GB" in report and "pd-standard" in report
    assert "attached to nothing" in report
    assert (f"gcloud compute disks delete comfy-win-a-b --zone=us-central1-b "
            f"--project={PROJECT} --quiet") in report

    assert "comfy-win-a-move-b" in report and "21 GB stored" in report
    assert "comfy-win-snap" in report and "18 GB stored" in report
    assert "source disk no longer exists" in report, (
        "comfy-win-snap is of a disk that is gone; say so rather than deleting it"
    )


def test_nothing_lying_around_means_nothing_reported():
    _, _, plan, found = prepared()
    assert leftovers(plan, found) == []
    assert found.anything() is False


def test_cleaning_up_removes_this_move_s_leftovers_and_nobody_else_s():
    """Opt-in, and narrow. Someone else's snapshot is reported, never deleted."""
    cloud, gc, plan, found = prepared(Cloud(
        disks=[SOURCE, ORPHAN_DISK], snapshots=[ORPHAN_SNAPSHOT, ANCESTOR_SNAPSHOT],
        instances=[INSTANCE],
    ))
    _, say = recorder()

    removed = remove_leftovers(gc, plan, found, say)

    assert set(removed) == {"comfy-win-a-b", "comfy-win-a-move-b"}
    assert cloud.find_disk("comfy-win-a-b") is None
    assert cloud.find_snapshot("comfy-win-a-move-b") is None
    assert cloud.find_snapshot("comfy-win-snap"), "not this move's to delete"
    assert cloud.find_disk("comfy-win-a"), "the original boot disk is untouched"


def test_looking_at_the_project_changes_nothing_on_it():
    """survey runs for a dry run too, so it has to be provably read-only."""
    cloud, gc, plan, _ = prepared(Cloud(
        disks=[SOURCE, ORPHAN_DISK], snapshots=[ORPHAN_SNAPSHOT], instances=[INSTANCE],
    ))
    before = (list(cloud.disks), list(cloud.snapshots), list(cloud.instances))
    survey(gc, plan)
    assert (cloud.disks, cloud.snapshots, cloud.instances) == before
    assert all("list" in call for call in cloud.calls)
