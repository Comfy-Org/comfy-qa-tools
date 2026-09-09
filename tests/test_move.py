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

from comfy_qa.config import ConfigError, Host
from comfy_qa.hostfile import HostFileError
from comfy_qa.gcloud import Gcloud, GcloudError
from comfy_qa.relocate import (
    BUILT_IN_CARD,
    accelerator_of,
    describe_snapshot,
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
    would_not_load,
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
                 machine_types=("g2-standard-8",), region=None, quotas=()):
        self.disks = [dict(d) for d in disks]
        self.snapshots = [dict(s) for s in snapshots]
        self.instances = [dict(i) for i in instances]
        self.quotas = list(quotas)
        self.machine_types = list(machine_types)
        self.region = region
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
        if key.startswith("quotas info list"):
            # The project-wide GPU ceiling, which `move` now reads whenever the
            # box it would create wants a card — every move in this file. What
            # comes back here is a project that reports no GPUS-ALL-REGIONS
            # record, which is a real shape and the one that refuses nothing:
            # these tests are about what a move creates and leaves behind, and a
            # ceiling that refused them would be testing something else. The
            # arithmetic itself is held up in tests/test_relocate.py.
            return list(self.quotas)
        if key.startswith("compute regions describe"):
            # The SSD allowance, read before the plan is printed. `None` here
            # means "could not tell", which must leave the plan alone.
            return self.region
        if key.startswith("compute disks snapshot"):
            return self._take_snapshot(args)
        if key.startswith("compute disks create"):
            return self._create_disk(args)
        if key.startswith("compute instances create"):
            return self._create_instance(args)
        if key.startswith("compute instances start"):
            # `move` with no `--to` starts the box to read a zone out of the
            # refusal, so the status has to MOVE — a fake that answers TERMINATED
            # either side of a start cannot tell a box that was put back from one
            # that was never touched, which is the whole of what is under test.
            return self._set_status(args[3], "RUNNING")
        if key.startswith("compute instances stop"):
            return self._set_status(args[3], "TERMINATED")
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

    def _set_status(self, name, status):
        there = self.find_instance(name)
        assert there is not None, f"no such instance: {name}"
        there["status"] = status
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
    assert plan.new_instance == "comfy-win", "the instance keeps the box's name"

    _, say = recorder()
    added, register = registrations()
    run_move(gc, plan, found, say, register=register)

    assert cloud.ran("compute disks snapshot")[0].split()[3] == "windows-install-2024"
    assert cloud.find_disk("windows-install-2024-b", "us-central1-b")
    assert cloud.find_instance("comfy-win")


# --- a clean move --------------------------------------------------------

def test_a_clean_move_does_each_step_once_and_leaves_no_snapshot():
    cloud, gc, plan, found = prepared()
    lines, say = recorder()
    added, register = registrations()

    outcome = run_move(gc, plan, found, say, register=register)

    assert outcome.done == (SNAPSHOT, CREATE_DISK, CREATE_INSTANCE,
                            DELETE_SNAPSHOT, REGISTER, "leave")
    assert added == ["comfy-win"]
    assert cloud.find_disk("comfy-win-a-b", "us-central1-b")
    assert cloud.find_instance("comfy-win")
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
    assert cloud.find_instance("comfy-win")
    assert added == ["comfy-win"]
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
        instances=[INSTANCE, {"name": "comfy-win", "status": "RUNNING",
                              "zone": f"{URL}/zones/us-central1-b"}],
    ))
    _, say = recorder()
    added, register = registrations()

    outcome = run_move(gc, plan, found, say, register=register)

    assert outcome.reused_instance()
    assert cloud.ran("compute instances create") == []
    assert added == ["comfy-win"]
    assert cloud.find_snapshot("comfy-win-a-move-b") is None


# --- refusing to reuse the wrong disk ------------------------------------

# The disk in use is deliberately not here: it is refused too, but answering it
# with a delete command is the defect
# `test_a_disk_in_use_is_not_answered_with_a_command_to_delete_it` covers.
@pytest.mark.parametrize("bad, because", [
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
    assert all(("list" in call or "describe" in call) for call in cloud.calls), (
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
    # The box keeps its name now, so "was it created" is a question about the
    # destination zone, not about the name — exactly as it is for gcloud.
    assert not [i for i in cloud.instances
                if i["zone"].endswith("us-central1-b")], "nothing was created"
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
    assert "comfy-qat move comfy-win --to us-central1-c" in caught.value.fix
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

    assert "the instance comfy-win in us-central1-b" in caught.value.left
    assert cloud.find_instance("comfy-win")


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

    assert added == ["comfy-win"]
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
    assert "no longer exists" in report, (
        "comfy-win-snap is of a disk that is gone; say so rather than deleting it"
    )


def test_a_disk_in_use_is_never_reported_as_a_leftover_to_delete():
    """Seen on a live project: `move` offered to delete a running box's boot disk.

    `comfy-qat move comfy-win --to us-central1-b` printed, on stdout,
    "disk comfy-win-a-b in us-central1-b (300 GB, pd-standard, ...) — attached to
    nothing, billing since 2026-08-27" followed by `gcloud compute disks delete
    comfy-win-a-b ... --quiet`, while stderr said the opposite: "attached to
    comfy-win-b. That is a disk in use, not a leftover from a half-finished move."
    The disk really was comfy-win-b's boot disk.

    Two streams contradicting each other is bad enough; the one that is wrong is
    the one carrying a delete command, and `--clean` runs it without asking.
    A disk somebody is booting from is not a leftover, whatever its name is.
    """
    in_use = disk("comfy-win-a-b", "us-central1-b", kind="pd-standard",
                  from_snapshot="comfy-win-a-move-b", users=("comfy-win-b",))
    _, _, plan, found = prepared(Cloud(
        disks=[SOURCE, in_use], snapshots=[ORPHAN_SNAPSHOT],
        instances=[INSTANCE],
    ))
    report = "\n".join(leftovers(plan, found))

    assert "attached to nothing" not in report, report
    assert "gcloud compute disks delete comfy-win-a-b" not in report, report


def test_cleaning_up_never_asks_google_to_delete_a_disk_in_use():
    """`--clean` runs `remove_leftovers` with no prompt at all.

    Google refuses to delete an attached disk, so this could not destroy data —
    it spent the run on a request that could only fail and surfaced the refusal
    as "could not clean up". The same `users` check keeps the request unsent.
    """
    in_use = disk("comfy-win-a-b", "us-central1-b", kind="pd-standard",
                  from_snapshot="comfy-win-a-move-b", users=("comfy-win-b",))
    cloud, gc, plan, found = prepared(Cloud(
        disks=[SOURCE, in_use], snapshots=[ORPHAN_SNAPSHOT],
        instances=[INSTANCE],
    ))
    _, say = recorder()

    removed = remove_leftovers(gc, plan, found, say)

    assert "comfy-win-a-b" not in removed, removed
    assert cloud.ran("compute disks delete") == []
    assert cloud.find_disk("comfy-win-a-b") is not None


def test_what_this_move_left_is_a_prefix_of_everything_lying_around():
    """`host move` prints the two lists as one, then splits them by counting.

    `stray = leftovers(plan, found, unrelated=True)[len(mine):]` is only correct
    while `unrelated=False` yields a strict prefix of `unrelated=True`. It did
    not: the unrelated snapshots were emitted *before* the "already exists" line,
    so once the target instance existed — the half-finished move this reporting
    is for — the slice ate one line too many. The line naming an unrelated
    billing snapshot went nowhere, its `gcloud compute snapshots delete` was
    printed on its own with no subject, and "already exists" was printed twice,
    the second time under "unrelated to this move".

    Holding the prefix is what makes the arithmetic at the call site true.
    """
    made = {"name": "comfy-win", "status": "RUNNING",
            "zone": f"{URL}/zones/us-central1-b"}
    _, _, plan, found = prepared(Cloud(
        disks=[SOURCE, ORPHAN_DISK],
        snapshots=[ORPHAN_SNAPSHOT, ANCESTOR_SNAPSHOT],
        instances=[INSTANCE, made],
    ))
    assert found.instance is not None, "the case only arises once the box exists"

    mine = leftovers(plan, found, unrelated=False)
    everything = leftovers(plan, found, unrelated=True)

    assert everything[:len(mine)] == mine
    stray = everything[len(mine):]
    assert "comfy-win-snap" in "\n".join(stray)
    assert any("from a box that no longer exists" in line for line in stray), stray
    # Nothing said twice, and nothing dropped between the two lists.
    assert len(set(mine) & set(stray)) == 0, set(mine) & set(stray)
    assert set(mine) | set(stray) == set(everything)


def test_a_disk_in_use_is_not_answered_with_a_command_to_delete_it():
    """The refusal and its fix have to agree, or the fix is the one that gets run.

    `blocked` chose one fix for every reason `judge_disk` can refuse a disk, so
    the "attached to comfy-win-b. That is a disk in use, not a leftover from a
    half-finished move." refusal was answered with `gcloud compute disks delete
    ... --quiet` for that same disk — a sentence and a command that contradict
    each other inside one message. Google refuses to delete an attached disk, so
    the handed-over command cannot work either. The move is blocked by a machine
    that is using the disk; the ways out are that machine or another zone.
    """
    in_use = disk("comfy-win-a-b", "us-central1-b", from_snapshot="comfy-win-a-move-b",
                  users=("comfy-win-b",))
    _, _, plan, found = prepared(Cloud(
        disks=[SOURCE, in_use], snapshots=[ORPHAN_SNAPSHOT], instances=[INSTANCE],
    ))

    problem = blocked(plan, found)

    assert problem is not None
    assert "attached to comfy-win-b" in str(problem)
    assert "disks delete" not in (problem.fix or ""), problem.fix
    assert "comfy-win-b" in (problem.fix or ""), problem.fix
    # Nothing here is this move's to clean up, so nothing is offered as such.
    assert problem.cleanup == (), problem.cleanup

    # Still refused, and still before anything is touched.
    cloud, gc, plan, found = prepared(Cloud(
        disks=[SOURCE, in_use], snapshots=[ORPHAN_SNAPSHOT], instances=[INSTANCE],
    ))
    assert found.reuse_disk is False
    _, say = recorder()
    _, register = registrations()
    with pytest.raises(MoveError) as caught:
        run_move(gc, plan, found, say, register=register)
    assert "attached to comfy-win-b" in str(caught.value)
    assert "disks delete" not in caught.value.fix
    assert all(("list" in call or "describe" in call) for call in cloud.calls)


def test_an_unattached_disk_this_move_cannot_reuse_still_offers_the_delete():
    """The other refusals are unchanged: that disk really is nobody's."""
    _, _, plan, found = prepared(Cloud(
        disks=[SOURCE, disk("comfy-win-a-b", "us-central1-b")],
        snapshots=[ORPHAN_SNAPSHOT], instances=[INSTANCE],
    ))

    problem = blocked(plan, found)

    assert problem is not None
    assert "nothing records" in str(problem)
    assert "gcloud compute disks delete comfy-win-a-b" in (problem.fix or "")


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
    assert all(("list" in call or "describe" in call) for call in cloud.calls), (
        "reading a quota or an instance is fine; changing anything is not"
    )


SSD_QUOTA_REFUSAL = (
    "ERROR: (gcloud.compute.disks.create) Could not fetch resource:\n"
    " - Quota 'SSD_TOTAL_GB' exceeded.  Limit: 500.0 in region us-central1.\n"
)


def test_a_disk_type_that_will_not_fit_falls_back_rather_than_failing():
    """From a real move on 2026-08-27.

    Matching the source disk's type is right, and it turned a working move into
    a failed one: pd-balanced counts against SSD_TOTAL_GB, and a 300 GB copy of
    a 300 GB disk needs 600 under an allowance of 500. The move died at its most
    expensive step, having already paid for the snapshot. A finished move on a
    slower disk beats no move.
    """
    from comfy_qa.gcloud import QUOTA, GcloudError
    from comfy_qa.relocate import _create_disk

    attempts = []

    class Cloud:
        def run(self, args, **kwargs):
            attempts.append(" ".join(args))
            if "--type=pd-balanced" in args:
                raise GcloudError("Quota 'SSD_TOTAL_GB' exceeded", kind=QUOTA,
                                  raw=SSD_QUOTA_REFUSAL)
            return []

    said = []
    plan = plan_move(WIN, INSTANCE, "us-central1-b", source_disk=SOURCE)
    assert plan.disk_type == "pd-balanced", "the fixture must be the interesting case"
    _create_disk(Cloud(), plan, "snap", said.append)

    assert len(attempts) == 2, "it asked for the matching type first"
    assert "--type=pd-standard" in attempts[1]
    told = " ".join(said)
    assert "SSD allowance" in told and "more slowly" in told
    assert "SSD_TOTAL_GB" in told, "name the thing to raise"


def test_a_failure_that_is_not_about_the_allowance_still_stops_the_move():
    """The fallback is for one specific refusal. Anything else is a real failure
    and quietly downgrading the disk would hide it."""
    from comfy_qa.gcloud import DENIED, GcloudError
    from comfy_qa.relocate import _create_disk

    class Cloud:
        def run(self, args, **kwargs):
            raise GcloudError("permission denied", kind=DENIED, raw="")

    with pytest.raises(GcloudError, match="permission denied"):
        plan = plan_move(WIN, INSTANCE, "us-central1-b", source_disk=SOURCE)
        _create_disk(Cloud(), plan, "snap", lambda line: None)


# --- the card travels with the box ---------------------------------------
#
# A move used to build the new instance without `--accelerator`, so the five
# cards that are attached by flag rather than built into the machine type — T4,
# P4, P100, V100, K80, all N1 — were left behind. The box booted, ComfyUI
# installed, the move reported success, and torch reported no CUDA device.

N1_INSTANCE = {
    **INSTANCE,
    "name": "comfy-t4",
    "machineType": f"{URL}/zones/us-central1-a/machineTypes/n1-standard-8",
    "disks": [
        {"boot": True, "source": f"{URL}/zones/us-central1-a/disks/comfy-t4-a",
         "deviceName": "persistent-disk-0"},
    ],
    "guestAccelerators": [{
        "acceleratorType": f"{URL}/zones/us-central1-a/acceleratorTypes/nvidia-tesla-t4",
        "acceleratorCount": 1,
    }],
}

T4 = Host(name="comfy-t4", kind="gce", port=8195, os="Ubuntu 22.04", gpu="T4",
          gce_instance="comfy-t4", gce_zone="us-central1-a", gce_project=PROJECT)


def test_an_attached_card_is_read_off_the_box_not_rebuilt_from_the_name():
    """`host.gpu` says "T4"; only the instance knows `nvidia-tesla-t4`."""
    assert accelerator_of(N1_INSTANCE) == "type=nvidia-tesla-t4,count=1"
    assert accelerator_of(INSTANCE) is None, "a G2 carries its card in the type"


@pytest.mark.parametrize("family,card", [
    ("g2-standard-8", "nvidia-l4"),
    ("a2-highgpu-1g", "nvidia-tesla-a100"),
    ("a3-highgpu-8g", "nvidia-h100-80gb"),
])
def test_a_built_in_card_is_not_asked_for_twice(family, card):
    """A REAL G2 reports its built-in card in guestAccelerators, and passing
    `--accelerator` alongside that machine type is refused by Google — at the
    LAST step, after the snapshot and the disk are made and paid for.

    The test above passes only because its fixture omits the field. A real one
    does not, which is the fixture-shaped blind spot this file has produced all
    day."""
    reported = {
        "machineType": f"{URL}/zones/us-central1-a/machineTypes/{family}",
        "guestAccelerators": [{
            "acceleratorType": f"{URL}/zones/us-central1-a/acceleratorTypes/{card}",
            "acceleratorCount": 1,
        }],
    }
    assert accelerator_of(reported) is None, family


def test_the_built_in_card_families_are_exactly_these_three():
    """The parametrised guard below holds the families it names. It cannot hold
    the ones it does not — and adding `g4`, `c3` or `n2` to BUILT_IN_CARD each
    left the whole suite green, because none of those three is in its list.

    So this pins MEMBERSHIP rather than behaviour, deliberately, and it is the
    only assertion in this file that will fail on a one-word edit. Each member
    is here because Google attaches its card as part of the machine type and
    passing --accelerator as well is an error at create time:

        g2  L4          a2  A100        a3  H100

    Adding a family means asserting that of Google's catalogue. If that is true,
    change this test and say why in the commit. If it is not, the flag is
    omitted, the move produces a box with no GPU, and it reports success — which
    is the defect `accelerator_of` exists for.
    """
    assert BUILT_IN_CARD == frozenset({"g2", "a2", "a3"}), (
        "BUILT_IN_CARD changed. A family added here stops getting --accelerator; "
        "a family removed here gets it twice and Google refuses the create.")


@pytest.mark.parametrize("family", [
    "n1-standard-8", "g6-standard-4", "a5-highgpu-1g", "unknown-1", "",
])
def test_an_unfamiliar_family_gets_the_flag_whatever_it_is_called(family):
    """The asymmetry, pinned rather than the three families we happen to know.

    Removing the guard fails the built-in tests, so the defect is held. But
    ADDING a family to BUILT_IN_CARD left the whole suite green — so the decision
    that unknown families get the flag had nothing protecting it, and the failure
    mode is the original defect: --accelerator omitted, a box with no GPU, the
    move reporting success.

    BUILT_IN_CARD is a hand-maintained frozenset of Google machine families. That
    is the second hand-maintained collection in this codebase whose membership
    nothing checked; ERROR_TYPES was the first, and it silently DROPPED two
    classes. This one can silently GAIN one — Google ships new families, and
    adding one you believe is built-in is a plausible edit rather than a careless
    one.
    """
    reported = {
        "machineType": f"{URL}/zones/us-central1-a/machineTypes/{family}",
        "guestAccelerators": [{
            "acceleratorType": f"{URL}/zones/us-central1-a/acceleratorTypes/nvidia-l4",
            "acceleratorCount": 1,
        }],
    }
    assert accelerator_of(reported) == "type=nvidia-l4,count=1", family


def test_an_unfamiliar_family_still_gets_its_card():
    """Wrong in the direction that fails loudly and costs a disk, rather than the
    one that produces a box with no GPU and reports success."""
    reported = {
        "machineType": f"{URL}/zones/us-central1-a/machineTypes/n4-something-8",
        "guestAccelerators": [{
            "acceleratorType": f"{URL}/zones/us-central1-a/acceleratorTypes/nvidia-l4",
            "acceleratorCount": 2,
        }],
    }
    assert accelerator_of(reported) == "type=nvidia-l4,count=2"


def test_the_zone_does_not_travel_with_the_card():
    """acceleratorType is a URL naming the zone being moved away from."""
    assert "us-central1-a" not in (accelerator_of(N1_INSTANCE) or "")


def test_moving_an_n1_box_actually_creates_it_with_the_card():
    cloud = Cloud(instances=[dict(N1_INSTANCE)],
                  disks=[disk("comfy-t4-a", "us-central1-a")],
                  machine_types=["n1-standard-8"])
    gc = cloud.gcloud()
    plan, found = prepare(gc, T4, N1_INSTANCE, "us-central1-b")
    run_move(gc, plan, found, recorder()[1], register=lambda _: None)

    created = cloud.ran("compute instances create")
    assert created, "nothing was created"
    assert "--accelerator=type=nvidia-tesla-t4,count=1" in created[0]
    # An accelerator cannot live-migrate; Google refuses the create without this.
    assert "--maintenance-policy=TERMINATE" in created[0]


def test_moving_a_g2_box_does_not_pass_a_flag_google_would_refuse():
    cloud, gc, plan, found = prepared()
    run_move(gc, plan, found, recorder()[1], register=lambda _: None)

    created = cloud.ran("compute instances create")
    assert created and "--accelerator" not in created[0]


def test_the_plan_says_which_card_the_new_box_gets():
    """So a card being dropped is visible before the money is spent."""
    plan = plan_move(T4, N1_INSTANCE, "us-central1-b")
    line = next(s for s in plan.steps() if s.startswith("create comfy-t4"))
    assert "n1-standard-8" in line and "nvidia-tesla-t4" in line


# --- cleaning up and then moving, in one run ------------------------------
#
# `--clean` used to delete and stop, so a half-finished move could not be
# cleaned and retried in one command. Letting it carry on introduced a trap:
# the plan was built from `found`, and after a deletion `found` describes
# resources that are gone. Carrying on with it would REUSE a deleted disk or
# skip a snapshot it now needs, and neither fails loudly — the move just builds
# from nothing.


def test_cleaning_up_replans_before_moving_on():
    """The plan is rebuilt from the project as it is after the deletion."""
    left = disk("comfy-win-a-b", "us-central1-b", from_snapshot="comfy-win-a-move")
    cloud = Cloud(disks=[SOURCE, left], instances=[INSTANCE],
                  snapshots=[snapshot("comfy-win-a-move", "comfy-win-a")])
    gc = cloud.gcloud()

    _, before = prepare(gc, WIN, INSTANCE, "us-central1-b")
    assert before.disk is not None, "the fixture has to start with something to clean"

    remove_leftovers(gc, plan_move(WIN, INSTANCE, "us-central1-b"), before,
                     lambda line: None)
    _, after = prepare(gc, WIN, INSTANCE, "us-central1-b")

    assert after.disk is None, "the replan still believes in the deleted disk"
    assert after.snapshot is None
    assert not after.reuse_disk, (
        "reusing a disk that was just deleted is the silent failure this guards"
    )


# --- the disk type is decided before the user agrees to it ----------------
#
# The plan promised "pd-balanced, matching comfy-win-a". The user said yes. The
# create then failed on SSD_TOTAL_GB, took pd-standard instead, and announced a
# slower box after the fact — offering a remedy (raise the quota, delete the
# disk, run again) that throws away a copy already paid for and waited on.

def _region(usage, limit):
    return {"quotas": [{"metric": "SSD_TOTAL_GB", "usage": usage, "limit": limit}]}


def test_a_disk_that_will_not_fit_is_planned_as_the_slower_one():
    cloud = Cloud(disks=[SOURCE], instances=[INSTANCE], region=_region(400, 500))
    _, plan, _ = (cloud, *prepare(cloud.gcloud(), WIN, INSTANCE, "us-central1-b"))

    assert plan.disk_type == "pd-standard"
    line = next(s for s in plan.steps() if s.startswith("create disk"))
    assert "100 GB left" in line and "300 GB" in line
    assert "more slowly" in line, "the consequence belongs in the plan, not after it"


def test_a_disk_that_fits_still_matches_the_original():
    cloud = Cloud(disks=[SOURCE], instances=[INSTANCE], region=_region(0, 5000))
    _, plan, _ = (cloud, *prepare(cloud.gcloud(), WIN, INSTANCE, "us-central1-b"))

    assert plan.disk_type == "pd-balanced"
    assert "matching comfy-win-a" in next(
        s for s in plan.steps() if s.startswith("create disk"))


def test_an_unreadable_allowance_changes_nothing():
    """Advisory only. A check that refuses a move which would have succeeded is
    worse than the surprise it prevents, and the runtime fallback still catches
    the real thing."""
    cloud = Cloud(disks=[SOURCE], instances=[INSTANCE], region=None)
    _, plan, _ = (cloud, *prepare(cloud.gcloud(), WIN, INSTANCE, "us-central1-b"))

    assert plan.disk_type == "pd-balanced"
    assert plan.disk_note is None


# --- the host list rewrite is the last step, and it can fail --------------

@pytest.mark.parametrize("raised", [
    # HostFileError, which is a ConfigError. An inline comment on the port line
    # is what a hand-maintained host list actually looks like.
    HostFileError("comfy-win has no port line, so its port cannot be freed."),
    ConfigError("the rewritten host list would not parse: bad TOML"),
    # These two reach the caller raw, which is why catching HostFileError alone
    # would have closed neither: a read-only config directory, and os.replace.
    PermissionError(13, "Permission denied"),
    OSError(18, "Invalid cross-device link"),
])
def test_a_host_list_that_cannot_be_rewritten_is_a_bill_not_a_traceback(raised):
    """The box exists and is billing; only a text rewrite failed.

    `run_move` caught `GcloudError` and nothing else and `move_cmd` caught
    `MoveError` and nothing else, so anything `register` raised unwound through
    both and reached the user as a stack trace — at the one moment this command
    is otherwise careful, with a GPU instance created, running and costing money.
    """
    cloud, gc, plan, found = prepared()
    _, say = recorder()

    def register(_plan):
        raise raised

    with pytest.raises(MoveError) as caught:
        run_move(gc, plan, found, say, register=register)

    message = str(caught.value)
    assert "comfy-win" in message and "us-central1-b" in message
    assert "running and billing" in message, "the bill leads, whatever else is said"
    assert str(raised) in message, "gcloud's or the file's own words survive"

    # `comfy-qat down` reads the host list, and the host list is exactly what did
    # not get written — so the entry it would read still names the old zone. The
    # raw stop for the box that really exists is the only thing that works.
    assert "gcloud compute instances stop comfy-win --zone=us-central1-b" in (
        caught.value.fix or "")
    assert "the instance comfy-win in us-central1-b" in caught.value.left


def test_the_move_is_not_undone_by_a_failed_registration():
    """Recoverable, and re-running is the recovery. Nothing is torn down."""
    cloud, gc, plan, found = prepared()
    _, say = recorder()

    with pytest.raises(MoveError):
        run_move(gc, plan, found, say,
                 register=lambda _plan: (_ for _ in ()).throw(OSError("read-only")))

    assert cloud.find_instance("comfy-win"), "the box stays; it is the useful half"
    assert cloud.ran("compute instances delete") == []
    assert cloud.ran("compute disks delete") == []


# --- moving a box home again ---------------------------------------------

def moved_out() -> list[Host]:
    """The host list after one move: comfy-win is in b, and a lives on renamed.

    This is what `move` itself writes. The retired entry stays deliberately, so
    `down` can still reach a box that exists and bills until somebody deletes it.
    """
    return [
        Host(name="local", kind="local", port=8188),
        Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022",
             gpu="L4", gce_instance="comfy-win", gce_zone="us-central1-b",
             gce_project=PROJECT),
        Host(name="comfy-win-us-central1-a", kind="gce", port=8194,
             os="Windows Server 2022", gpu="L4", gce_instance="comfy-win",
             gce_zone="us-central1-a", gce_project=PROJECT),
    ]


def test_moving_a_box_back_where_it_came_from_is_refused_before_it_costs_anything():
    """Out of a stocked-out zone, then home when capacity returns.

    Move one leaves `comfy-win-us-central1-a` naming the box in us-central1-a.
    Move two puts the box back there under its own name, and now two entries name
    one machine — which `config.load` refuses for the WHOLE file, so every command
    exits 2, `down` included, while the box runs and bills.

    Caught here it costs nothing. Caught at the end it costs a snapshot, a disk
    and an instance, all of them already paid for.
    """
    home = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022",
                gpu="L4", gce_instance="comfy-win", gce_zone="us-central1-b",
                gce_project=PROJECT)
    plan = plan_move(home, dict(INSTANCE, zone=f"{URL}/zones/us-central1-b"),
                     "us-central1-a")

    problem = would_not_load(moved_out(), plan)

    assert problem is not None, "this is the sequence that bricked a host list"
    assert "comfy-win-us-central1-a" in str(problem)
    assert "us-central1-a" in str(problem)
    assert "Nothing was created" in str(problem)
    assert "comfy-qat list --live" in (problem.fix or "")


def test_an_ordinary_move_to_a_zone_nothing_else_claims_is_not_refused():
    """The check must only refuse what `config.load` would refuse."""
    plan = plan_move(WIN, INSTANCE, "us-central1-b")
    assert would_not_load([WIN, Host(name="local", kind="local", port=8188)],
                          plan) is None


def test_the_box_being_moved_is_not_counted_against_itself():
    """`comfy-win`'s own entry is replaced by this move, not kept beside it."""
    plan = plan_move(WIN, INSTANCE, "us-central1-b")
    assert would_not_load([WIN], plan) is None, "it would collide only with itself"


def test_a_snapshot_leads_with_the_number_that_bills():
    """Snapshots are compressed and incremental, so storage is the cost and the
    source disk size is not. This named the disk first — and reading it off, the
    lead told the user a month-old orphan cost about eight pounds a month when it
    stores 18 GB and costs about fifty pence, and argued for deleting it on that
    basis. The tool's own ordering produced the wrong recommendation."""
    phrase = describe_snapshot({
        "name": "comfy-win-snap", "diskSizeGb": "300",
        "storageBytes": str(18 * 1024 ** 3), "sourceDisk": ".../disks/comfy-win",
    })
    assert phrase.index("stored") < phrase.index("300 GB"), phrase
    assert "of a 300 GB disk" in phrase, "the disk size has to say what it is"


# --- the split survives a --clean, because it is not arithmetic ---------------
#
# `move` printed both leftover lists and separated them by counting:
# `leftovers(..., unrelated=True)[len(mine):]`. That is only correct while both
# calls see the same `found` — and `--clean` REPLACES `found` without recomputing
# `mine`, so the slice removed a stale number of lines and ate the front of the
# unrelated section: the billing resources the report exists to name.
#
# It errs one way only. The slice can remove lines, never invent them, so every
# line printed was true and the omissions were the whole defect.


def test_the_two_reports_do_not_depend_on_a_remembered_length():
    """The property the caller needs, stated so it cannot drift: what belongs to
    this move and what does not, from ONE `found`, at one moment."""
    from comfy_qa.relocate import split_leftovers

    cloud = Cloud(disks=[SOURCE, ORPHAN_DISK],
                  snapshots=[ORPHAN_SNAPSHOT, ANCESTOR_SNAPSHOT],
                  instances=[INSTANCE])
    gc = cloud.gcloud()
    plan, found = prepare(gc, WIN, INSTANCE, "us-central1-b")

    mine, stray = split_leftovers(plan, found)
    everything = leftovers(plan, found, unrelated=True)

    assert mine + stray == everything, "a line went missing between the two"
    assert not set(mine) & set(stray), "a line was reported twice"
    assert all("no longer exists" in line or line.startswith("  gcloud")
               for line in stray), stray


def test_a_clean_does_not_shorten_the_unrelated_report():
    """The defect itself: `mine` computed before the clean, the split after it.

    An odd-length `mine` is the NORMAL case — every resource is two lines except
    the "already exists" instance line, which is one, and an instance that got
    created is exactly what `--clean` is for. So the old cut landed between a
    resource and its delete command."""
    from comfy_qa.relocate import split_leftovers

    littered = Cloud(disks=[SOURCE, ORPHAN_DISK],
                     snapshots=[ORPHAN_SNAPSHOT, ANCESTOR_SNAPSHOT],
                     instances=[INSTANCE])
    gc = littered.gcloud()
    plan, before = prepare(gc, WIN, INSTANCE, "us-central1-b")
    stale_length = len(leftovers(plan, before, unrelated=False))

    remove_leftovers(gc, plan, before, lambda line: None)
    plan, after = prepare(gc, WIN, INSTANCE, "us-central1-b")

    _, stray = split_leftovers(plan, after)
    counted = leftovers(plan, after, unrelated=True)[stale_length:]

    assert "comfy-win-snap" in "\n".join(stray), stray
    assert len(stray) > len(counted), (
        "the remembered length still hides lines the split keeps"
    )


# --- an abandoned move, not just a resumed one ----------------------------
#
# `relocate` is the one place in this tool designed for being interrupted — "a
# half-finished move is resumable, not a wall" — and its leftovers report was
# reachable only on the NEXT `move` run, which is the run somebody who has just
# pressed Ctrl-C is least likely to make. So "nothing is left billing in
# silence" was true of a resumed move and false of an abandoned one.
#
# No new wording for any of this: the record is handed the same `_state_after`
# the MoveError path uses, with the step in progress counted as done. Counting it
# as done is the honest reading — the request has reached Google by then, and
# Ctrl-C reaches only the local gcloud, so assuming it did NOT happen is the
# assumption that costs money.


def test_an_interrupted_move_names_the_snapshot_it_had_already_paid_for(capsys):
    from comfy_qa import inflight

    cloud, gc, plan, found = prepared()
    cloud.fail["compute disks create"] = KeyboardInterrupt()
    _, say = recorder()
    added, register = registrations()

    with pytest.raises(inflight.Interrupted):
        run_move(gc, plan, found, say, register=register)

    report = capsys.readouterr().err

    assert plan.snapshot in report, report
    assert plan.new_disk in report, "the disk in flight is counted as made"
    assert "snapshots delete" in report
    assert "disks delete" in report
    assert f"comfy-qat move {plan.host.name} --to {plan.to_zone}" in report, (
        "resuming is the cheap way out and it has to be on the same screen as "
        "the delete commands"
    )
    assert added == [], "nothing reached the host list"


def test_an_interrupt_at_the_instance_leads_with_the_stop_not_the_delete(capsys):
    """The instance is the only part billing by the minute, and `_state_after`
    deliberately lists it with no cleanup command — on the failure paths a move
    that got this far is a move that succeeded, and the finished-move output
    hands the stop and the delete over separately. An interrupted run has nothing
    printing them, so the record adds them, stop first."""
    from comfy_qa import inflight

    cloud, gc, plan, found = prepared()
    cloud.fail["compute instances create"] = KeyboardInterrupt()
    _, say = recorder()
    added, register = registrations()

    with pytest.raises(inflight.Interrupted):
        run_move(gc, plan, found, say, register=register)

    report = capsys.readouterr().err
    stop = report.index("instances stop")
    delete = report.index("instances delete")
    assert stop < delete, report


def test_a_move_that_finishes_leaves_the_record_empty():
    """Every step registers, and every step that returns clears."""
    from comfy_qa import inflight

    cloud, gc, plan, found = prepared()
    _, say = recorder()
    added, register = registrations()

    run_move(gc, plan, found, say, register=register)

    assert inflight.pending() == []


# --- the closing report, driven through the command ------------------------
#
# 0891903 fixed `move`'s closing report and pinned only the helper: both its
# tests call `split_leftovers` directly, and NOTHING in the suite drove
# `move_cmd` far enough to print it. Putting the defect back at the call site —
# `stray = leftovers(plan, found, unrelated=True)[len(mine):]`, with `mine` still
# in scope from before `--clean` replaced `found` — left the whole suite green.
# So the report that names a billing snapshot could regress to the arithmetic
# tomorrow and nothing would say so.
#
# `--clean` is not incidental. It is the state change the arithmetic cannot
# survive: it replaces `found` and does not recompute `mine`, so the stale length
# is subtracted from a list that no longer begins with those lines and eats the
# front of the unrelated section — the resources the report exists to name.


def _cli_move(tmp_path, monkeypatch, cloud, *args, hosts=None):
    """Drive `move` itself, rather than `run_move`.

    Everything else in this file tests the engine directly, which is why the
    closing report went unpinned: it is printed by the COMMAND, after `run_move`
    has returned, and no test in the suite had ever reached it.
    """
    from typer.testing import CliRunner

    from comfy_qa import gcloud as gcloud_module
    from comfy_qa.host import app

    path = tmp_path / "hosts.toml"
    path.write_text(hosts or CLI_HOSTS, encoding="utf-8")
    gc = cloud.gcloud()
    monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: gc)
    return CliRunner().invoke(app, ["move", *args, "--config", str(path)])


CLI_HOSTS = f"""\
[hosts.comfy-win]
kind         = "gce"
os           = "Windows Server 2022"
gpu          = "L4"
gce_instance = "comfy-win"
gce_zone     = "us-central1-a"
gce_project  = "{PROJECT}"
port         = 8190
"""

# A snapshot of THIS move's disk that is not usable — status CREATING — so it is
# a spare rather than the one to reuse. That makes `mine` non-empty before
# `--clean` and empty after it, which is the whole mechanism.
SPARE = snapshot("comfy-win-a-move-old", "comfy-win-a", status="CREATING",
                 created="2026-08-01T07:33:34.373-07:00")


def test_the_closing_report_names_a_billing_snapshot_that_is_not_this_moves(
        tmp_path, monkeypatch):
    cloud = Cloud(disks=[SOURCE], instances=[INSTANCE],
                  snapshots=[SPARE, ANCESTOR_SNAPSHOT])

    result = _cli_move(tmp_path, monkeypatch, cloud,
                       "comfy-win", "--to", "us-central1-b", "--clean", "--yes")

    assert result.exit_code == 0, result.output
    assert "also on the project, unrelated to this move and billing" in result.output, (
        "the whole heading went missing, not just its contents"
    )
    assert "comfy-win-snap" in result.output, (
        f"a billing snapshot from a box that no longer exists was not named: "
        f"{result.output}"
    )
    # The delete command has to arrive WITH its subject. The arithmetic could cut
    # between a resource and its command, leaving a bare `gcloud compute snapshots
    # delete` under a heading, with nothing saying what it would delete.
    named = result.output.index("comfy-win-snap")
    assert "snapshots delete comfy-win-snap" in result.output[named:], result.output


# --- refusing before deleting, driven through the command ---------------------
#
# `--clean` deleted first and refused second. `remove_leftovers` ran at the top
# of `move_cmd` and the guards sat fifteen lines below it, so a move that was
# going to be refused destroyed the earlier run's snapshot and its 200-300 GB
# disk on the way to refusing — the two artifacts that make a stalled move cheap
# to resume, deleted in service of a move that never started.
#
# The refusal used here is the GPU ceiling, because it is the one that actually
# happens: a project whose GPUS_ALL_REGIONS is 1 refuses the move of a box while
# anything else holds the card, and that is the ordinary state of the project
# this tool was written against. Nothing in the suite had ever paired `--clean`
# with a refusal of any kind.

CEILING_OF_ONE = [{"quotaId": "GPUS-ALL-REGIONS-per-project",
                   "dimensionsInfos": [{"details": {"value": "1"},
                                        "applicableLocations": ["global"]}]}]

# A GPU box on the project that is not this move's, holding the only card there
# is. `_over_the_ceiling` names it and hands over raw gcloud for it.
CONSOLE_BOX = {
    "name": "console-box",
    "status": "RUNNING",
    "zone": f"{URL}/zones/us-west4-b",
    "guestAccelerators": [{"acceleratorType": f"{URL}/nvidia-l4",
                           "acceleratorCount": 1}],
}


def test_a_clean_that_is_going_to_be_refused_deletes_nothing(tmp_path, monkeypatch):
    """The whole finding, in one command: nothing is deleted before every
    refusal has been computed."""
    cloud = Cloud(disks=[SOURCE, ORPHAN_DISK],
                  snapshots=[ORPHAN_SNAPSHOT],
                  instances=[INSTANCE, CONSOLE_BOX],
                  quotas=CEILING_OF_ONE)

    result = _cli_move(tmp_path, monkeypatch, cloud,
                       "comfy-win", "--to", "us-central1-b", "--clean", "--yes")

    assert result.exit_code == 2, result.output
    assert "GPUS_ALL_REGIONS is 1" in result.output

    # The point of the test. Both of these were deleted, for nothing.
    assert cloud.find_disk("comfy-win-a-b", "us-central1-b"), (
        "the 300 GB disk an earlier run paid for was deleted by a move that "
        "then refused to happen"
    )
    assert cloud.find_snapshot("comfy-win-a-move-b"), (
        "the snapshot a resumed move would have reused was deleted by a move "
        "that then refused to happen"
    )
    assert not cloud.ran("compute disks delete"), cloud.calls
    assert not cloud.ran("compute snapshots delete"), cloud.calls
    # And nothing was created either, which is what exit 2 asserts.
    assert not cloud.ran("compute disks snapshot")
    assert not cloud.ran("compute instances create")


def test_a_clean_still_clears_the_leftover_it_exists_to_clear(tmp_path, monkeypatch):
    """The other half of the same rule, and the reason `unclearable` is not
    simply `blocked`.

    A disk sitting in the target zone that this move cannot reuse IS a refusal —
    and it is the one a `--clean` removes. Refusing on it before the delete would
    turn the supported recovery, clean and move in one run, into a wall.
    """
    # Made from a snapshot of something else entirely, so `judge_disk` refuses to
    # reuse it and `blocked` would refuse the move.
    stranger = disk("comfy-win-a-b", "us-central1-b", from_snapshot="somebody-else",
                    created="2026-08-25T07:38:24.167-07:00")
    cloud = Cloud(disks=[SOURCE, stranger], instances=[INSTANCE])

    _, _, plan, found = prepared(Cloud(disks=[SOURCE, stranger], instances=[INSTANCE]))
    assert blocked(plan, found) is not None, "the fixture has to start refused"

    result = _cli_move(tmp_path, monkeypatch, cloud,
                       "comfy-win", "--to", "us-central1-b", "--clean", "--yes")

    assert result.exit_code == 0, result.output
    assert cloud.find_instance("comfy-win"), "the move went ahead after cleaning"


def test_a_host_list_the_rewrite_cannot_edit_is_refused_before_anything_is_made(
        tmp_path, monkeypatch):
    """`register` is the SEVENTH action of a move and nothing rehearsed it.

    `[hosts."comfy-win"]` is valid TOML, `config.parse` accepts it, and every
    command that reads the host list works. `rename_and_add` matches
    `[hosts.<name>]` as text and does not recognise the quoted spelling, so it
    raised "comfy-win is not in the host list" — at the step after the snapshot,
    the disk and the instance, with the box running and billing and
    `comfy-qat down comfy-win` still pointing at the box in the OLD zone.

    Every one of those refusals reads the local file and nothing else, so the
    whole rewrite is now rehearsed before the first gcloud call that spends
    anything.
    """
    quoted = CLI_HOSTS.replace("[hosts.comfy-win]", '[hosts."comfy-win"]')
    cloud = Cloud(disks=[SOURCE], instances=[INSTANCE])

    result = _cli_move(tmp_path, monkeypatch, cloud, "comfy-win",
                       "--to", "us-central1-b", "--yes", hosts=quoted)

    assert result.exit_code == 2, result.output
    assert "Nothing was created" in result.output
    assert not cloud.ran("compute disks snapshot"), "not even the cheap half"
    assert not cloud.ran("compute disks create")
    assert not cloud.ran("compute instances create")
    # The file is named, because the fix is an edit to it and nothing else.
    assert "hosts.toml" in result.output


def test_the_rehearsal_does_not_write_the_host_list(tmp_path, monkeypatch):
    """It runs on every move, including a dry run, so it has to be a rehearsal
    and not a rewrite happening early."""
    path = tmp_path / "hosts.toml"
    cloud = Cloud(disks=[SOURCE], instances=[INSTANCE])

    result = _cli_move(tmp_path, monkeypatch, cloud,
                       "comfy-win", "--to", "us-central1-b", "--dry-run")

    assert result.exit_code == 0, result.output
    assert path.read_text(encoding="utf-8") == CLI_HOSTS, "the file was rewritten"
    assert not path.with_name("hosts.toml.bak").exists(), "not even a backup"


# --- the clock the snapshot runs under ----------------------------------------

def test_the_snapshot_is_not_run_under_the_clock_written_for_starting_a_box(
        monkeypatch):
    """A 200 GB snapshot exceeded 300s on a real move, and 200 GB is the DEFAULT
    boot disk this tool creates.

    `snapshot_disk` passed `INSTANCE_TIMEOUT` — the number written for how long
    Windows takes to boot — to a call that copies a disk. gcloud was killed at
    five minutes while Google went on holding the snapshot in UPLOADING, so the
    tool reported a failure about a resource that was being created and would
    bill. `relocate._stopped` was taught to report that doubt honestly, which is
    the right answer to a call whose outcome is unknown and no answer at all to a
    clock set below the work: on the ordinary case it fires every time.

    Pinned as a number rather than as an inequality against `INSTANCE_TIMEOUT`,
    which could be raised for its own reasons and quietly satisfy this.
    """
    from comfy_qa import gcloud as gcloud_module
    from comfy_qa.gcloud import INSTANCE_TIMEOUT, SNAPSHOT_TIMEOUT

    assert SNAPSHOT_TIMEOUT >= 1800, (
        f"tens of minutes is normal for a full copy of a 200-300 GB disk; "
        f"{SNAPSHOT_TIMEOUT}s does not clear that"
    )
    assert SNAPSHOT_TIMEOUT > INSTANCE_TIMEOUT

    class Finished:
        returncode = 0
        stdout = ""
        stderr = ""

    seen: list[tuple[str, object]] = []

    def fake_run(cmd, **kwargs):
        seen.append((" ".join(cmd), kwargs.get("timeout")))
        return Finished()

    monkeypatch.setattr(gcloud_module.subprocess, "run", fake_run)
    gc = gcloud_module.Gcloud()
    gc.proven = True                      # skip the credential preflight
    monkeypatch.setattr(gc, "require", lambda: "gcloud")

    gc.snapshot_disk("comfy-win-a", "us-central1-a", PROJECT, "comfy-win-a-move")

    took = [timeout for command, timeout in seen if "disks snapshot" in command]
    assert took == [SNAPSHOT_TIMEOUT], seen


# --- the path that moves nothing ----------------------------------------------
#
# `move` with no `--to` starts the box to ask Google where there is capacity,
# because nothing answers that question any other way. When the start is NOT
# refused there is no stockout and nothing to move — and that path used to leave
# the box running and skip everything else the command was asked to do.
#
# Both findings below were reproduced on real hardware, twice, with the state
# confirmed either side.


def test_a_box_the_probe_started_is_stopped_again(tmp_path, monkeypatch):
    """`down comfy-linux`, then `move comfy-linux --clean --yes`, and the box was
    RUNNING again — started by a command that moved nothing.

    The user stopped it deliberately to stop paying, and the pack tells them to
    stop the source before a move precisely because a TERMINATED box holds no GPU
    allowance. So the tool asked for that state and then took it away behind
    them, on the one path where it had nothing to show for it.
    """
    cloud = Cloud(disks=[SOURCE], instances=[dict(INSTANCE)])

    result = _cli_move(tmp_path, monkeypatch, cloud, "comfy-win", "--yes")

    assert result.exit_code == 0, result.output
    assert cloud.find_instance("comfy-win")["status"] == "TERMINATED", (
        "a box the user had stopped was left running by a move that moved nothing"
    )
    assert cloud.ran("compute instances start"), "the probe is still a start"
    assert cloud.ran("compute instances stop"), "and it is put back"
    assert "has been stopped again" in result.output
    assert not cloud.ran("compute disks snapshot"), "nothing was moved"


def test_a_box_found_running_is_left_running_by_the_same_path(tmp_path, monkeypatch):
    """Only a state that was positively read is restored. Someone is using this
    box; stopping it on the way past would be the same defect with the sign
    reversed."""
    cloud = Cloud(disks=[SOURCE], instances=[dict(INSTANCE, status="RUNNING")])

    result = _cli_move(tmp_path, monkeypatch, cloud, "comfy-win", "--yes")

    assert result.exit_code == 0, result.output
    assert cloud.find_instance("comfy-win")["status"] == "RUNNING"
    assert not cloud.ran("compute instances stop"), "not this command's to stop"
    assert "is already running" in result.output
    assert "changed nothing" in result.output


def test_clean_cleans_on_the_path_where_there_is_nothing_to_move(
        tmp_path, monkeypatch):
    """`--clean` is "delete what an earlier, half-finished move left behind, then
    move", and on this path it did neither.

    The run that found this went in with a 14.3 GB snapshot and a 200 GB disk
    from a failed move, both billing, and came out with both still there and a
    message saying everything was fine. There is no target zone here, so the
    leftovers are identified by what they were made FROM rather than by the names
    a move would build — see `relocate.stranded`.
    """
    cloud = Cloud(disks=[SOURCE, ORPHAN_DISK],
                  snapshots=[ORPHAN_SNAPSHOT, ANCESTOR_SNAPSHOT],
                  instances=[dict(INSTANCE)])

    result = _cli_move(tmp_path, monkeypatch, cloud, "comfy-win", "--clean", "--yes")

    assert result.exit_code == 0, result.output
    assert "an earlier run left this behind, and it is billing" in result.output
    assert cloud.find_disk("comfy-win-a-b", "us-central1-b") is None, (
        "the 200 GB disk a failed move left was still billing after --clean"
    )
    assert cloud.find_snapshot("comfy-win-a-move-b") is None

    # Narrow, exactly as `--clean` is on the move path. The boot disk of the box
    # itself and a snapshot of something else are not this command's to delete.
    assert cloud.find_disk("comfy-win-a", "us-central1-a"), "the live boot disk"
    assert cloud.find_snapshot("comfy-win-snap"), "not from a move of this box"


def test_the_same_path_reports_the_leftovers_without_clean_and_deletes_none(
        tmp_path, monkeypatch):
    """A part-finished move bills in silence, so the report is not conditional on
    the flag. The deleting is."""
    cloud = Cloud(disks=[SOURCE, ORPHAN_DISK], snapshots=[ORPHAN_SNAPSHOT],
                  instances=[dict(INSTANCE)])

    result = _cli_move(tmp_path, monkeypatch, cloud, "comfy-win", "--yes")

    assert result.exit_code == 0, result.output
    assert "comfy-win-a-b" in result.output, "the disk is named"
    assert "us-central1-b" in result.output, "and the zone it is in"
    assert "disks delete comfy-win-a-b" in result.output, "with its delete line"
    assert cloud.find_disk("comfy-win-a-b", "us-central1-b"), "and nothing deleted"
    assert cloud.find_snapshot("comfy-win-a-move-b")
