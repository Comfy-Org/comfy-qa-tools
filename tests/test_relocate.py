"""Moving a box out of a zone that has no capacity.

The instance fixture is shaped like a real `instances describe`, with full
resource URLs, because hand-written fixtures have already cost this project three
bugs.
"""

from __future__ import annotations

import pytest

from comfy_qa import inflight
from comfy_qa.config import Host
from comfy_qa.gcloud import GcloudError
from comfy_qa.relocate import (
    CREATE_DISK,
    CREATE_INSTANCE,
    DELETE_SNAPSHOT,
    REGISTER,
    SNAPSHOT,
    Action,
    Found,
    _inflight,
    blocked,
    boot_disk,
    machine_type,
    metadata_pairs,
    plan_move,
    remove_leftovers,
    suffix_for,
)

WIN = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022", gpu="L4",
           gce_instance="comfy-win", gce_zone="us-central1-a", gce_project="proj")

INSTANCE = {
    "name": "comfy-win",
    "machineType": "https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a/machineTypes/g2-standard-8",
    "disks": [
        {"boot": False, "source": "https://.../disks/scratch", "deviceName": "scratch"},
        {"boot": True, "source": "https://.../disks/comfy-win", "deviceName": "persistent-disk-0"},
    ],
    "metadata": {"items": [
        {"key": "enable-windows-ssh", "value": "TRUE"},
        {"key": "windows-startup-script-ps1", "value": "irrelevant"},
    ]},
}


def test_the_boot_disk_is_the_one_carrying_the_install():
    """Not simply the first disk — a scratch disk can come first."""
    assert boot_disk(INSTANCE) == "comfy-win"


def test_machine_type_is_carried_over_not_guessed():
    assert machine_type(INSTANCE) == "g2-standard-8"


def test_windows_ssh_metadata_survives_the_move():
    """Losing it leaves the moved box unreachable by every command here, which
    would look like the move having failed."""
    carried = metadata_pairs(INSTANCE)
    assert "enable-windows-ssh=TRUE" in carried
    assert "windows-startup-script-ps1" not in carried, "only what matters"


def test_the_box_keeps_its_name_and_the_disk_says_where_it_went():
    """GCE names are unique per zone, not per project, so the suffix was never
    Google's requirement — it existed to keep an appended host-list key unique,
    and that append is what left `go comfy-win` pointing at the old zone.

    The disk keeps a suffix: two copies of one install are what a half-finished
    move leaves lying around, and telling them apart matters."""
    plan = plan_move(WIN, INSTANCE, "us-central1-b")
    assert plan.new_instance == "comfy-win"
    assert plan.new_disk == "comfy-win-b"
    assert plan.to_zone == "us-central1-b"
    assert plan.machine_type == "g2-standard-8"


def test_the_box_left_behind_is_named_by_where_it_is():
    """`comfy-win-a` did not say where it was, and after two moves neither did
    `comfy-win-a-b`. It stays in the list because it still bills."""
    plan = plan_move(WIN, INSTANCE, "us-central1-b")
    assert plan.retired_name == "comfy-win-us-central1-a"


def test_suffix_is_the_zone_letter():
    assert suffix_for("us-central1-b") == "b"
    assert suffix_for("europe-west4-a") == "a"


def test_the_plan_is_readable_before_anything_changes():
    steps = plan_move(WIN, INSTANCE, "us-central1-b").steps()
    assert any("snapshot" in step for step in steps)
    assert any("us-central1-b" in step for step in steps)
    assert any("list" in step for step in steps)
    assert any("leave comfy-win stopped" in step for step in steps), (
        "the original must not be destroyed"
    )


def test_an_instance_with_no_disks_still_plans_something():
    plan = plan_move(WIN, {"name": "comfy-win"}, "us-central1-b")
    assert plan.new_instance == "comfy-win"
    # A REAL family: this value is passed to the create, so a sentinel here would
    # ask Google to build "unknown-machine-type". The degraded-payload guard
    # lives in `accelerator_of`, which reads the raw field — see below.
    assert plan.machine_type == "g2-standard-8"


def test_a_describe_with_no_machine_type_still_asks_for_the_card():
    """The consequence, stated where someone changing the fallback will see it."""
    from comfy_qa.relocate import accelerator_of

    degraded = {"name": "comfy-win", "guestAccelerators": [
        {"acceleratorType": ".../acceleratorTypes/nvidia-tesla-t4",
         "acceleratorCount": 1},
    ]}
    assert accelerator_of(degraded) == "type=nvidia-tesla-t4,count=1"


# --- what a move leaves behind, stated truthfully -----------------------------

RUNNING_SOURCE = {**INSTANCE, "status": "RUNNING"}


def test_a_running_source_is_not_described_as_stopped():
    """`leave X stopped` was printed unconditionally, including about a box that
    was on. A false statement about a billing GPU is the worst kind this tool
    can make, because the whole point of the tool is knowing what costs."""
    plan = plan_move(WIN, RUNNING_SOURCE, "us-central1-b")
    leave = plan.steps()[-1]
    assert "running" in leave and "keeps billing" in leave
    assert "stopped" not in leave


def test_a_stopped_source_still_reads_as_stopped():
    plan = plan_move(WIN, INSTANCE, "us-central1-b")
    assert "stopped" in plan.steps()[-1]


def test_leaving_the_source_alone_is_a_step_the_dispatch_knows_about():
    """LEAVE had no branch in run_move: it fell through, was recorded as done,
    and nothing ever executed it. Keeping it inert is correct — a move must not
    stop a box someone may be using — but it has to be inert on purpose."""
    import inspect

    from comfy_qa import relocate

    body = inspect.getsource(relocate.run_move)
    assert "LEAVE" in body, "the one step with no branch is the one that lied"


# --- interrupting a move, and interrupting a clean --------------------------
#
# Two separate defects with one root: the record of what a run may have left is
# built on rules written for steps that CREATE, and a move has one step that
# deletes and a `--clean` pass that is nothing but deletes.

MOVE_INSTANCE = {
    "name": "comfy-win",
    "machineType": "https://x/projects/p/zones/us-central1-a/machineTypes/g2-standard-8",
    "disks": [{"boot": True, "source": "https://x/disks/comfy-win-a",
               "deviceName": "persistent-disk-0"}],
}


def moving() -> object:
    return plan_move(WIN, MOVE_INSTANCE, "us-central1-b")


def interrupted_at(kind: str, done: list[str]):
    return _inflight(moving(), Found(), list(done), Action(kind=kind, line=""))


def test_a_snapshot_whose_delete_was_interrupted_is_still_reported():
    """The in-flight step is counted as done, which is right for a create and
    inverted for the one step that deletes.

    Counting a delete as done drops the resource out of the report — so the
    resource the interrupted step was in the middle of, the only one whose fate
    was actually in doubt, was the one thing not mentioned. Ctrl-C reaches the
    local gcloud, not the request, so an interrupted delete carries the same
    doubt as an interrupted create; the safe reading is "it may still be there".
    """
    left, extra = interrupted_at(
        DELETE_SNAPSHOT, [SNAPSHOT, CREATE_DISK, CREATE_INSTANCE])

    assert "the snapshot comfy-win-a-move" in left
    assert any("snapshots delete comfy-win-a-move" in line
               for line in extra["undo"])


def test_a_snapshot_delete_that_finished_is_not_reported():
    """The other direction, and the guard on the fix: not counting a delete as
    done must not mean never counting it."""
    left, extra = interrupted_at(
        REGISTER, [SNAPSHOT, CREATE_DISK, CREATE_INSTANCE, DELETE_SNAPSHOT])

    assert "the snapshot" not in left
    assert not any("snapshots delete" in line for line in extra["undo"])


def test_a_created_instance_is_still_counted_the_moment_its_step_begins():
    """The rule the fix narrows rather than reverses. A create in flight has
    reached Google, and assuming it did not is the assumption that costs money."""
    left, _ = interrupted_at(CREATE_INSTANCE, [SNAPSHOT, CREATE_DISK])

    assert "the instance comfy-win in us-central1-b" in left


@pytest.mark.parametrize("done,kind", [
    ([SNAPSHOT, CREATE_DISK], CREATE_INSTANCE),
    ([SNAPSHOT, CREATE_DISK, CREATE_INSTANCE], DELETE_SNAPSHOT),
    ([SNAPSHOT, CREATE_DISK, CREATE_INSTANCE, DELETE_SNAPSHOT], REGISTER),
])
def test_the_undo_block_names_only_what_its_own_commands_remove(done, kind):
    """The sentence introducing the cleanup was fixed text — "the disk and the
    snapshot go with it:" — above whatever commands happened to follow.

    Interrupted after the snapshot had gone, it named a snapshot and handed over
    a disk delete and an instance delete and nothing that touches a snapshot: a
    resource named in the one report whose whole job is naming what is billing,
    by a line whose commands do not remove it.
    """
    _, extra = interrupted_at(kind, done)
    undo = "\n".join(extra["undo"])
    lead = next((line for line in extra["undo"]
                 if line.startswith("then, if you do not want it")), None)
    assert lead is not None, undo

    assert ("the snapshot" in lead) == ("snapshots delete" in undo), undo
    assert ("the disk" in lead) == ("disks delete" in undo), undo


class _CleanGcloud:
    """A gcloud whose second delete is the one that never comes back."""

    def __init__(self, interrupt_at: int | None = None):
        self.calls: list[str] = []
        self.interrupt_at = interrupt_at

    def _step(self, what: str) -> None:
        self.calls.append(what)
        if self.interrupt_at is not None and len(self.calls) == self.interrupt_at:
            raise KeyboardInterrupt

    def run(self, args, **kwargs):
        self._step(" ".join(args))

    def delete_snapshot(self, name, project):
        self._step(f"snapshots delete {name}")


def clean_up(gc) -> list[str]:
    found = Found(disk={"name": "comfy-win-a-b"},
                  snapshot={"name": "comfy-win-a-move"})
    return remove_leftovers(gc, moving(), found, lambda line: None)


def test_a_clean_that_is_interrupted_says_which_of_the_three_it_was(capsys):
    """`--clean` deleted a list one item at a time and kept the answer in a
    local that went with the frame.

    Interrupted at item two of two, the tool KNOWS item one is gone and item two
    is in doubt — and it exited 130 in silence. Unlike the create paths, where
    "may" is the honest word because nothing has read the project back, here the
    precise answer already exists and was simply not printed.
    """
    with pytest.raises(inflight.Interrupted):
        clean_up(_CleanGcloud(interrupt_at=2))

    # The report fires inside `may_leave` now, so the record is cleared by the
    # time the raise arrives and the message is what survives. Reading the
    # message is the stronger check: it is what the person who pressed Ctrl-C
    # sees, and the record was only ever a proxy for it.
    report = capsys.readouterr().err

    assert "the snapshot comfy-win-a-move" in report, report
    assert "snapshots delete comfy-win-a-move" in report
    # Certain, and said in the note rather than under a heading about things
    # that may exist, which would misdescribe it.
    assert "already deleted before you stopped it" in report
    assert "comfy-win-a-b" in report
    assert "--clean" in report, "re-running is the answer that settles it"


def test_a_clean_interrupted_on_the_first_delete_names_the_one_it_never_reached(capsys):
    """The item still queued is not in doubt at all — it is certainly there and
    certainly billing, and nothing was going to mention it."""
    with pytest.raises(inflight.Interrupted):
        clean_up(_CleanGcloud(interrupt_at=1))

    report = capsys.readouterr().err

    assert "the disk comfy-win-a-b in us-central1-b" in report, report
    assert "the snapshot comfy-win-a-move" in report
    assert "already deleted" not in report, (
        "nothing had been deleted yet, so nothing may claim it was"
    )


def test_a_clean_that_finishes_leaves_nothing_registered():
    gc = _CleanGcloud()
    assert clean_up(gc) == ["comfy-win-a-b", "comfy-win-a-move"]
    assert inflight.pending() == []


# --- the ceiling move never checked ----------------------------------------
#
# `create` gates on GPUS_ALL_REGIONS and `switch` gates on it. `move` starts a
# GPU box too, and had no gate at all — so on a project whose ceiling is 1, a
# move of a running box took the snapshot, built the disk, and was refused by
# Google at the instance. The last and most expensive step.


def gpu_instance(name, zone="us-central1-a", status="RUNNING", cards=1):
    return {
        "name": name,
        "status": status,
        "zone": f"https://x/projects/p/zones/{zone}",
        "guestAccelerators": [{"acceleratorType": "https://x/nvidia-l4",
                               "acceleratorCount": cards}],
    }


def ceiling_quota(value):
    return [{"quotaId": "GPUS-ALL-REGIONS-per-project",
             "dimensionsInfos": [{"details": {"value": str(value)},
                                  "applicableLocations": ["global"]}]}]


class _Project:
    """The two reads `survey` makes, plus the quota read the gate may add."""

    def __init__(self, instances, ceiling=1):
        self.instances = instances
        self.ceiling = ceiling
        self.quota_reads = 0

    def run(self, args, **kwargs):
        # The target zone offers the machine type, so the only blocker these
        # tests can produce is the one under test.
        if "machine-types" in args:
            return [{"name": "g2-standard-8"}]
        return []

    def list_instances(self, project):
        return self.instances

    def gpu_quotas(self, project):
        self.quota_reads += 1
        if self.ceiling is None:
            raise GcloudError("quota could not be read")
        return ceiling_quota(self.ceiling)


def survey_with(gc):
    from comfy_qa.relocate import survey

    return survey(gc, moving())


def test_a_move_of_a_running_box_under_a_ceiling_of_one_is_refused_up_front():
    """The incident this whole module's acceptance pack was written around.

    The new box exists alongside the old one for the length of the move, so a
    running source needs two of an allowance of one. Google refuses that at the
    instance create — after the snapshot and a 300 GB disk are made and paid for.
    """
    gc = _Project([gpu_instance("comfy-win")], ceiling=1)
    problem = blocked(moving(), survey_with(gc))

    assert problem is not None
    assert "GPUS_ALL_REGIONS is 1" in str(problem)
    assert "comfy-win already holds 1 of it" in str(problem)
    assert "Nothing was created" in str(problem)
    assert "comfy-qat down comfy-win" in problem.fix


def test_a_move_of_a_stopped_box_under_the_same_ceiling_is_allowed():
    """The case the gate must not refuse. A stopped source spends nothing, so
    one of one is enough — and refusing it would block the ordinary move."""
    gc = _Project([gpu_instance("comfy-win", status="TERMINATED")], ceiling=1)

    assert blocked(moving(), survey_with(gc)) is None


def test_something_else_holding_the_ceiling_is_handed_raw_gcloud():
    """A GCE instance name, and `config.resolve` never matches on one — the box
    holding the slot is typically one started in the console."""
    gc = _Project([gpu_instance("comfy-win", status="TERMINATED"),
                   gpu_instance("console-box", zone="us-west4-b")], ceiling=1)
    problem = blocked(moving(), survey_with(gc))

    assert problem is not None
    assert ("gcloud compute instances stop console-box --zone=us-west4-b"
            in problem.fix)
    assert "comfy-qat down console-box" not in problem.fix


def test_a_ceiling_that_could_not_be_read_refuses_nothing():
    """"Not read" is not "zero" — the rule `create`'s own gate states. Refusing
    on a number nobody has blocks a move the project is entitled to."""
    gc = _Project([gpu_instance("comfy-win")], ceiling=None)

    assert blocked(moving(), survey_with(gc)) is None


def test_an_unlimited_ceiling_refuses_nothing():
    gc = _Project([gpu_instance("comfy-win")], ceiling=-1)

    assert blocked(moving(), survey_with(gc)) is None


def test_the_minute_long_quota_read_is_skipped_when_nothing_holds_a_card():
    """`gpu_quotas` is the ~58-second call and `move` is slow already. Nothing
    can be over the ceiling while nothing holds any of it, so the free half —
    counting cards in the list `survey` has already fetched — decides first."""
    gc = _Project([gpu_instance("comfy-win", status="TERMINATED")], ceiling=1)
    survey_with(gc)

    assert gc.quota_reads == 0


def test_the_quota_is_read_when_the_answer_could_be_no():
    gc = _Project([gpu_instance("comfy-win")], ceiling=1)
    survey_with(gc)

    assert gc.quota_reads == 1


def test_a_box_with_no_card_at_all_is_not_gated_on_a_gpu_ceiling():
    gc = _Project([{"name": "comfy-win", "status": "RUNNING",
                    "zone": "https://x/projects/p/zones/us-central1-a"}], ceiling=0)

    assert blocked(moving(), survey_with(gc)) is None
    assert gc.quota_reads == 0
