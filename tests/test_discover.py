"""Finding the cloud boxes that already exist.

The instance fixture is a real record from a live project — name, zone, machine
type, accelerator and licence exactly as gcloud returns them. Inventing this shape
is what produced the quota bugs.
"""

from __future__ import annotations

import pytest

from comfy_qa.config import Host
from comfy_qa.discover import (
    accelerator,
    new_hosts,
    next_ports,
    operating_system,
    parse,
    to_toml,
)

COMFY_WIN = {
    "name": "comfy-win",
    "zone": "https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a",
    "status": "TERMINATED",
    "machineType": "https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a/machineTypes/g2-standard-8",
    "guestAccelerators": [{
        "acceleratorType": "https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a/acceleratorTypes/nvidia-l4",
        "acceleratorCount": 1,
    }],
    "disks": [{
        "boot": True,
        "licenses": ["https://www.googleapis.com/compute/v1/projects/windows-cloud/global/licenses/windows-server-2022-dc"],
    }],
}

LOCAL = Host(name="local", kind="local", port=8188)


def test_a_real_instance_fills_in_everything_you_would_have_typed():
    box = parse(COMFY_WIN, "stately-timing-504610-p1")
    assert box.name == "comfy-win"
    assert box.os == "Windows Server 2022"
    assert box.gpu == "L4"
    assert box.gce_zone == "us-central1-a"
    assert box.gce_project == "stately-timing-504610-p1"


def test_terminated_reads_as_stopped_not_broken():
    """TERMINATED is Google's word for stopped. Shown raw it reads as a fault."""
    assert parse(COMFY_WIN, "p").running is False
    assert parse(dict(COMFY_WIN, status="RUNNING"), "p").running is True


@pytest.mark.parametrize("state", [
    "STAGING", "PROVISIONING", "REPAIRING", "STOPPING", "SUSPENDING",
    "SUSPENDED", "DEPROVISIONING", "", "Running", "something-new",
])
def test_only_terminated_reads_as_stopped(state):
    """A machine has eight documented states and only one of them is stopped.

    This read `status == "RUNNING"`, so a box in STAGING — the first 30-60
    seconds of every start — was reported as stopped by `discover`, which is the
    one command whose whole job is telling you what exists in a project that
    nothing recorded. Fixed in b6f8d4f and, until now, unpinned: reverting the
    line left the entire suite green, so the only member of this class that could
    silently revert was the one nobody could see revert.

    Anything unrecognised counts as running. Being wrong that way makes someone
    look at a box that is off; being wrong the other way hides one that is on.
    """
    assert parse(dict(COMFY_WIN, status=state), "p").running is True, (
        f"a box in {state!r} was reported as stopped"
    )


@pytest.mark.parametrize("licence,expected", [
    ("windows-server-2022-dc", "Windows Server 2022"),
    ("ubuntu-2204-lts", "Ubuntu 22.04"),
    ("debian-12", "Debian 12"),
])
def test_licences_become_readable_names(licence, expected):
    disks = [{"boot": True, "licenses": [f"projects/x/global/licenses/{licence}"]}]
    assert operating_system(disks) == expected


def test_an_unknown_licence_is_reported_not_guessed():
    """Guessing 'Linux' would pick the wrong startup script later."""
    disks = [{"boot": True, "licenses": ["projects/x/global/licenses/some-new-distro"]}]
    assert operating_system(disks) == "some-new-distro"


def test_no_licences_at_all_is_survivable():
    assert operating_system(None) == "unknown"
    assert operating_system([{}]) == "unknown"


@pytest.mark.parametrize("accel_type,expected", [
    ("nvidia-l4", "L4"),
    ("nvidia-tesla-t4", "T4"),
    ("nvidia-a100-80gb", "A100-80GB"),
])
def test_accelerator_names_are_the_ones_people_say(accel_type, expected):
    instance = {"guestAccelerators": [{"acceleratorType": f"zones/z/acceleratorTypes/{accel_type}"}]}
    assert accelerator(instance) == expected


def test_a_box_with_no_gpu_is_not_a_crash():
    assert accelerator({"name": "plain"}) == ""
    assert parse({"name": "plain"}, "p").has_gpu is False


def test_ports_never_reuse_and_never_take_8188():
    assert next_ports([LOCAL], 3) == [8190, 8191, 8192]


def test_ports_step_over_anything_already_declared():
    taken = Host(name="other", kind="gce", port=8190, gce_instance="x")
    assert next_ports([LOCAL, taken], 2) == [8191, 8192]


def test_only_boxes_missing_from_the_host_list_are_added():
    box = parse(COMFY_WIN, "p")
    already = Host(name="win", kind="gce", port=8190, gce_instance="comfy-win")
    assert new_hosts([box], [LOCAL, already]) == []


def test_matching_is_on_the_instance_not_your_label():
    """Renaming a host in your own file must not make it reappear as a duplicate."""
    box = parse(COMFY_WIN, "p")
    renamed = Host(name="my-windows-box", kind="gce", port=8190, gce_instance="comfy-win")
    assert new_hosts([box], [renamed]) == []


def test_the_generated_block_is_valid_and_complete():
    import tomllib

    box = parse(COMFY_WIN, "p")
    parsed = tomllib.loads(to_toml(box, 8190))
    entry = parsed["hosts"]["comfy-win"]
    assert entry == {
        "kind": "gce", "os": "Windows Server 2022", "gpu": "L4",
        "gce_instance": "comfy-win", "gce_zone": "us-central1-a",
        "gce_project": "p", "port": 8190,
    }


def test_a_generated_block_survives_the_config_loader():
    """Whatever discovery writes has to pass the same validation as hand-written."""
    from comfy_qa.config import parse as parse_config
    import tomllib

    box = parse(COMFY_WIN, "p")
    text = '[hosts.local]\nkind = "local"\nport = 8188\n' + to_toml(box, 8190)
    hosts = parse_config(tomllib.loads(text))
    assert {h.name for h in hosts} == {"local", "comfy-win"}
