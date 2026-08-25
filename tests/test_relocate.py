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


def test_the_new_names_say_where_it_went():
    plan = plan_move(WIN, INSTANCE, "us-central1-b")
    assert plan.new_instance == "comfy-win-b"
    assert plan.new_disk == "comfy-win-b"
    assert plan.to_zone == "us-central1-b"
    assert plan.machine_type == "g2-standard-8"


def test_suffix_is_the_zone_letter():
    assert suffix_for("us-central1-b") == "b"
    assert suffix_for("europe-west4-a") == "a"


def test_the_plan_is_readable_before_anything_changes():
    steps = plan_move(WIN, INSTANCE, "us-central1-b").steps()
    assert any("snapshot" in step for step in steps)
    assert any("us-central1-b" in step for step in steps)
    assert any("host list" in step for step in steps)
    assert any("leave comfy-win stopped" in step for step in steps), (
        "the original must not be destroyed"
    )


def test_an_instance_with_no_disks_still_plans_something():
    plan = plan_move(WIN, {"name": "comfy-win"}, "us-central1-b")
    assert plan.new_instance == "comfy-win-b"
    assert plan.machine_type == "g2-standard-8"
