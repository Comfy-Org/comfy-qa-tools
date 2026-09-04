"""Moving a box out of a zone that has no capacity.

The instance fixture is shaped like a real `instances describe`, with full
resource URLs, because hand-written fixtures have already cost this project three
bugs.
"""

from __future__ import annotations

from comfy_qa.config import Host
from comfy_qa.relocate import (
    boot_disk,
    machine_type,
    metadata_pairs,
    plan_move,
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
    # NOT "g2-standard-8". That was the old fallback, and g2 is in BUILT_IN_CARD,
    # so a describe with no machineType became a KNOWN built-in family and
    # `accelerator_of` omitted the flag — a box with no GPU, reporting success.
    # An absent machineType is the most unknown a family can be.
    assert plan.machine_type == "unknown-machine-type"


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
