"""Making a box: the mapping, the quota gate, and falling through a stockout.

The quota payloads here are the shapes read off a live project on 2026-08-28,
including the two details that a hand-written fixture would never have:

  * `GPUS-ALL-REGIONS-per-project` is **1**. That is the project-wide ceiling
    across every card, and it is the limit that actually bites — an L4 grant of 1
    in forty-three regions is worth nothing while a second GPU box is running.
  * The same card is metered twice, and the two disagree.
    `NVIDIA-L4-GPUS-per-project-region` is 1 across 43 named regions;
    `NVIDIA-L4-GPUS-per-project-zone` is **-1** across the 130 zones inside them.
    -1 is Google's "no explicit limit", not "none".
"""

from __future__ import annotations

import pytest

from comfy_qa.config import Host
from comfy_qa.create import (
    CARDS,
    CREATE_FAILED,
    EXHAUSTED,
    IMAGES,
    LINUX_DRIVER,
    NO_QUOTA,
    NO_ZONE,
    WINDOWS_DRIVER,
    Blueprint,
    _gpu_boxes_running,
    build,
    card_for,
    check_quota,
    choose_name,
    create_in,
    host_entry,
    image_for,
    next_steps,
    nowhere,
    order_zones,
    plan,
    taken_names,
)
from comfy_qa.gcloud import GcloudError
from comfy_qa.lifecycle import LifecycleError
from comfy_qa.quota import global_allowance, regions_with_quota
from comfy_qa.zones import Ordering

PROJECT = "stately-timing-504610-p1"

# 43 regions on the live project. Five is enough to test the shape.
L4_REGIONS = ["asia-east1", "europe-west1", "europe-west4", "us-central1", "us-east1"]
L4_ZONES = [f"{region}-{letter}" for region in L4_REGIONS for letter in "abc"]

L4_REGION_QUOTA = {
    "quotaId": "NVIDIA-L4-GPUS-per-project-region",
    "dimensionsInfos": [{
        "dimensions": None,
        "details": {"value": "1"},
        "applicableLocations": L4_REGIONS,
    }],
}

# -1 is what a live project reports for the zone-scoped copy of the same grant.
L4_ZONE_QUOTA = {
    "quotaId": "NVIDIA-L4-GPUS-per-project-zone",
    "dimensionsInfos": [{
        "dimensions": None,
        "details": {"value": "-1"},
        "applicableLocations": L4_ZONES + ["australia-southeast2-a"],
    }],
}

CEILING = {
    "quotaId": "GPUS-ALL-REGIONS-per-project",
    "dimensionsInfos": [{
        "dimensions": None,
        "details": {"value": "1"},
        "applicableLocations": ["global"],
    }],
}

# T4 comes back one dimensioned row per region, not one row listing many.
T4_QUOTA = {
    "quotaId": "NVIDIA-T4-GPUS-per-project-region",
    "dimensionsInfos": [
        {"dimensions": {"region": region}, "details": {"value": "1"},
         "applicableLocations": [region]}
        for region in ["asia-east1", "us-central1"]
    ],
}

LIVE = [L4_REGION_QUOTA, L4_ZONE_QUOTA, T4_QUOTA, CEILING]


def ceiling(value):
    return {"quotaId": "GPUS-ALL-REGIONS-per-project",
            "dimensionsInfos": [{"details": {"value": str(value)},
                                 "applicableLocations": ["global"]}]}


def instance(name, *, running=True, gpu=True):
    body = {"name": name, "status": "RUNNING" if running else "TERMINATED"}
    if gpu:
        body["guestAccelerators"] = [{"acceleratorType": ".../nvidia-l4",
                                      "acceleratorCount": 1}]
    return body


LINUX_L4 = Blueprint(name="comfy-linux", image=IMAGES["linux"], card=CARDS["l4"])
WIN_L4 = Blueprint(name="comfy-win", image=IMAGES["windows"], card=CARDS["l4"])
LINUX_T4 = Blueprint(name="comfy-linux", image=IMAGES["linux"], card=CARDS["t4"])


# --- the machine type comes from the card ---------------------------------


def test_an_l4_is_a_g2_with_the_card_built_in():
    """Passing --accelerator alongside a G2 is refused by Google.

    This is the most common way a create by hand fails, and the reason the card
    is the only thing anybody types here.
    """
    assert LINUX_L4.machine_type == "g2-standard-8"
    assert LINUX_L4.card.accelerator_flag is None


def test_a_t4_is_an_n1_with_the_card_attached():
    assert LINUX_T4.machine_type == "n1-standard-8"
    assert LINUX_T4.card.accelerator_flag == "type=nvidia-tesla-t4,count=1"


@pytest.mark.parametrize("gpu,machine_type", [
    ("l4", "g2-standard-8"),
    ("t4", "n1-standard-8"),
    ("p4", "n1-standard-8"),
    ("p100", "n1-standard-8"),
    ("v100", "n1-standard-8"),
    ("k80", "n1-standard-8"),
    ("a100", "a2-highgpu-1g"),
    ("a100-80gb", "a2-ultragpu-1g"),
    ("h100", "a3-highgpu-8g"),
])
def test_every_card_names_a_machine_type_that_can_hold_it(gpu, machine_type):
    assert card_for(gpu).machine_type == machine_type


def test_the_attached_cards_are_exactly_the_n1_ones():
    """If a family is ever added, this is what asks whether it attaches."""
    for card in CARDS.values():
        assert card.attached == (not card.machine_type.startswith("n1-")), card.name


def test_an_h100_costs_eight_of_the_allowance_not_one():
    """The smallest H100 machine type is eight cards. Counting one passes the
    gate and then fails at the create, after the zone has been printed."""
    assert card_for("h100").count == 8


@pytest.mark.parametrize("typed", ["L4", "l4", "nvidia-l4", "NVIDIA_L4", " l4 ", "tesla-t4"])
def test_a_card_is_recognised_however_it_is_spelt(typed):
    assert card_for(typed) in (CARDS["l4"], CARDS["t4"])


def test_an_unknown_card_lists_what_there_is():
    with pytest.raises(LifecycleError) as raised:
        card_for("rtx4090")
    assert "no card called 'rtx4090'" in str(raised.value)
    assert "l4" in str(raised.value)
    assert raised.value.kind == NO_QUOTA


@pytest.mark.parametrize("typed,key", [
    ("linux", "linux"), ("ubuntu", "linux"), ("debian", "linux"),
    ("windows", "windows"), ("win", "windows"), ("WINDOWS", "windows"),
])
def test_an_operating_system_is_recognised_however_it_is_spelt(typed, key):
    assert image_for(typed).key == key


def test_an_unknown_operating_system_names_the_two_there_are():
    with pytest.raises(LifecycleError) as raised:
        image_for("freebsd")
    assert "--os linux or --os windows" in str(raised.value)


def test_the_image_describes_itself_the_way_discovery_will_read_it_back():
    """`host discover` reads the OS off the boot disk's licence.

    A box created here and the same box found by discovery have to describe
    themselves identically, or `host switch windows` matches one and not the
    other.
    """
    from comfy_qa.discover import _OS_NAMES

    for image in IMAGES.values():
        assert _OS_NAMES.get(licence_for(image)) == image.os


def licence_for(image):
    """The licence name Google puts on a boot disk made from that image family.

    Not guessed. Read live on 2026-08-28:

        gcloud compute images describe-from-family ubuntu-2204-lts --project=ubuntu-os-cloud
            ubuntu-2204-jammy-v20260826  ubuntu-2204-lts  .../licenses/ubuntu-2204-lts
        gcloud compute images describe-from-family windows-2022 --project=windows-cloud
            windows-server-2022-dc-v20260814  windows-2022  .../licenses/windows-server-2022-dc

    The licence name and the image family are not the same string on Windows,
    which is exactly the sort of thing a hand-written fixture gets wrong.
    """
    return {"ubuntu-2204-lts": "ubuntu-2204-lts",
            "windows-2022": "windows-server-2022-dc"}[image.family]


# --- metadata --------------------------------------------------------------


def test_a_windows_box_gets_the_ssh_metadata_without_which_nothing_reaches_it():
    assert WIN_L4.metadata == "enable-windows-ssh=TRUE"


def test_a_linux_box_gets_googles_own_driver_startup_script():
    assert LINUX_L4.metadata == f"startup-script={LINUX_DRIVER}"


def test_the_startup_script_is_googles_file_unedited():
    """Copied from GoogleCloudPlatform/compute-gpu-installation, linux/startup_script.sh.

    The two guards at the top are what make it safe on every boot, and the
    installer is the one Google's own "Install GPU drivers" page points at. If
    somebody rewrites this by hand, these are the lines that must survive.
    """
    assert LINUX_DRIVER.startswith("#!/bin/bash\n")
    assert "if test -f /opt/google/cuda-installer" in LINUX_DRIVER
    assert "if test -f cuda_installation" in LINUX_DRIVER
    assert ("curl -fSsL -O https://storage.googleapis.com/compute-gpu-installation-us"
            "/installer/latest/cuda_installer.pyz") in LINUX_DRIVER
    assert "python3 cuda_installer.pyz install_driver" in LINUX_DRIVER


def test_the_startup_script_carries_no_comma():
    """gcloud splits `--metadata` on commas.

    One comma in the script body and the value is read as a second key, and the
    create fails on an argument error that says nothing about a startup script.
    """
    assert "," not in LINUX_DRIVER


def test_the_windows_driver_is_handed_over_rather_than_guessed_at():
    """Google documents one way to do this on Windows, and it needs a person.

    There is a `windows-startup-script-url` metadata key and pointing it at that
    script would probably work. This tool does not ship "probably" on the path
    where the alternative is a box that bills while running on its CPU.
    """
    assert "install_gpu_driver.ps1" in WINDOWS_DRIVER
    assert WIN_L4.metadata is not None
    assert "startup-script" not in WIN_L4.metadata
    told = "\n".join(next_steps(WIN_L4, "us-central1-a"))
    assert "install_gpu_driver.ps1" in told
    assert "CPU" in told


def test_a_linux_box_is_told_the_driver_install_reboots_it():
    told = "\n".join(next_steps(LINUX_L4, "us-central1-a"))
    assert "reboot" in told


@pytest.mark.parametrize("blueprint", [LINUX_L4, WIN_L4])
def test_anything_said_after_the_box_exists_says_how_to_stop_paying(blueprint):
    assert any("host down" in line for line in next_steps(blueprint, "us-central1-a"))


# --- naming ----------------------------------------------------------------


def test_the_default_name_says_what_the_box_is():
    assert choose_name(None, IMAGES["linux"], set()) == "comfy-linux"
    assert choose_name(None, IMAGES["windows"], set()) == "comfy-win"


def test_a_taken_default_is_numbered_rather_than_reused():
    """Two machines that differ only by zone and share a name is how you read a
    result off the wrong one."""
    assert choose_name(None, IMAGES["linux"], {"comfy-linux"}) == "comfy-linux-2"
    assert choose_name(None, IMAGES["linux"],
                       {"comfy-linux", "comfy-linux-2"}) == "comfy-linux-3"


def test_a_name_you_asked_for_that_is_taken_is_refused_not_renamed():
    with pytest.raises(LifecycleError) as raised:
        choose_name("comfy-win", IMAGES["windows"], {"comfy-win"})
    assert "already taken" in str(raised.value)
    assert raised.value.kind == CREATE_FAILED


def test_names_are_checked_against_the_project_as_well_as_the_host_list():
    hosts = [Host(name="local", kind="local", port=8188)]
    taken = taken_names(hosts, [instance("comfy-linux")])
    assert "comfy-linux" in taken
    assert choose_name(None, IMAGES["linux"], taken) == "comfy-linux-2"


def test_a_host_named_differently_from_its_instance_blocks_both_names():
    hosts = [Host(name="win", kind="gce", port=8190, os="Windows Server 2022", gpu="L4",
                  gce_instance="comfy-win", gce_zone="us-central1-a", gce_project=PROJECT)]
    assert taken_names(hosts, []) == {"win", "comfy-win"}


# --- the plan --------------------------------------------------------------


def test_a_plan_is_made_offline_and_completely():
    made = plan(os_choice="windows", gpu="l4", disk_gb=500)
    assert made.name == "comfy-win"
    assert made.machine_type == "g2-standard-8"
    assert made.disk_gb == 500


def test_a_disk_too_small_for_the_image_is_refused_before_anything_exists():
    with pytest.raises(LifecycleError) as raised:
        plan(os_choice="windows", gpu="l4", disk_gb=20)
    assert "too small" in str(raised.value)


def test_the_plan_names_how_the_card_is_ordered():
    steps = " ".join(LINUX_T4.steps("us-central1-a"))
    assert "n1-standard-8" in steps
    assert "--accelerator=type=nvidia-tesla-t4,count=1" in steps
    assert "us-central1-a" in steps


def test_the_plan_for_a_built_in_card_does_not_promise_an_accelerator_flag():
    steps = " ".join(LINUX_L4.steps("us-central1-a"))
    assert "built into the machine type" in steps
    assert "--accelerator" not in steps


def test_the_plan_names_the_driver_step_differently_per_os():
    assert any("startup script" in step for step in LINUX_L4.steps("z"))
    assert any("NOT installed" in step for step in WIN_L4.steps("z"))


# --- reading the allowance -------------------------------------------------


def test_the_live_region_grant_is_used_and_not_the_unlimited_zone_one():
    """Both scopes exist for the same card and they disagree.

    Taking the union would offer `australia-southeast2`, whose region allowance
    is not granted at all — a create that fails on quota after the zone has been
    chosen and printed.
    """
    assert regions_with_quota("L4", LIVE) == sorted(L4_REGIONS)


def test_a_card_metered_one_row_per_region_reads_the_same_way():
    assert regions_with_quota("T4", LIVE) == ["asia-east1", "us-central1"]


def test_a_zone_scoped_grant_is_used_when_it_is_the_only_one():
    assert regions_with_quota("L4", [L4_ZONE_QUOTA]) == sorted(
        L4_REGIONS + ["australia-southeast2"])


def test_a_card_the_project_has_never_asked_for_has_no_regions():
    assert regions_with_quota("A100", LIVE) == []


def test_the_project_wide_ceiling_is_read_off_the_live_shape():
    assert global_allowance(LIVE) == 1
    assert global_allowance([L4_REGION_QUOTA]) is None


# --- the gate --------------------------------------------------------------


def test_a_project_with_the_grant_and_the_ceiling_free_is_allowed():
    check = check_quota(CARDS["l4"], LIVE, [])
    assert check.problem() is None
    assert check.card_limit == 1 and check.global_limit == 1


def test_the_gate_prints_both_allowances_whether_or_not_it_refuses():
    lines = " ".join(check_quota(CARDS["l4"], LIVE, []).lines())
    assert "L4: 1" in lines
    assert "GPUS_ALL_REGIONS" in lines


def test_a_card_with_no_grant_is_refused_before_anything_is_created():
    problem = check_quota(CARDS["a100"], LIVE, []).problem()
    assert problem is not None
    assert "no A100 quota" in str(problem)
    assert "Nothing was created" in str(problem)
    assert "auth quota request" in problem.fix
    assert problem.kind == NO_QUOTA


def test_a_card_that_needs_more_of_the_allowance_than_is_granted_is_refused():
    h100 = {"quotaId": "NVIDIA-H100-80GB-GPUS-per-project-region",
            "dimensionsInfos": [{"details": {"value": "1"},
                                 "applicableLocations": ["us-central1"]}]}
    problem = check_quota(CARDS["h100"], [h100, ceiling(8)], []).problem()
    assert "needs 8 of this project's GPU allowance and the grant is 1" in str(problem)


def test_the_project_wide_ceiling_refuses_even_with_a_card_grant():
    """The limit that actually bites. A per-card grant of 4 is worth nothing
    behind a GPUS_ALL_REGIONS of 0."""
    problem = check_quota(CARDS["l4"], [L4_REGION_QUOTA, ceiling(0)], []).problem()
    assert "GPUS_ALL_REGIONS is 0" in str(problem)
    assert "ceiling across every card" in str(problem)


def test_a_gpu_box_already_running_on_the_ceiling_is_a_box_to_stop_not_a_quota_to_raise():
    check = check_quota(CARDS["l4"], LIVE, [instance("comfy-win")])
    problem = check.problem()
    assert "comfy-win is already running on it" in str(problem)
    assert "comfy-qat host down comfy-win" in problem.fix


def test_a_stopped_box_does_not_hold_the_ceiling():
    assert check_quota(CARDS["l4"], LIVE, [instance("comfy-win", running=False)]
                       ).problem() is None


def test_a_running_box_with_no_card_does_not_hold_the_ceiling():
    assert check_quota(CARDS["l4"], LIVE, [instance("build-box", gpu=False)]
                       ).problem() is None


def test_an_unlimited_ceiling_never_refuses():
    check = check_quota(CARDS["l4"], [L4_REGION_QUOTA, ceiling(-1)],
                        [instance("a"), instance("b")])
    assert check.problem() is None
    assert "unlimited" in " ".join(check.lines())


def test_a_grant_that_names_no_region_is_refused_rather_than_searched():
    orphan = {"quotaId": "NVIDIA-L4-GPUS-per-project-region",
              "dimensionsInfos": [{"details": {"value": "1"},
                                   "applicableLocations": []}]}
    problem = check_quota(CARDS["l4"], [orphan, ceiling(1)], []).problem()
    assert "names no region" in str(problem)


@pytest.mark.parametrize("instances,expected", [
    ([], []),
    ([instance("a"), instance("b", running=False)], ["a"]),
    ([instance("a", gpu=False)], []),
])
def test_only_running_gpu_boxes_count_against_the_ceiling(instances, expected):
    assert _gpu_boxes_running(instances) == expected


# --- trying the zones ------------------------------------------------------


STOCKOUT = (
    "ERROR: (gcloud.compute.instances.create) Could not fetch resource:\n"
    " - The zone 'projects/p/zones/us-central1-a' does not have enough resources "
    "available to fulfill the request. Try a different zone, or try again later.\n"
)

# Google's own wording, as recorded in tests/test_lifecycle.py from a real
# refusal on this project. `suggested_zones` matches "trying your request in the",
# and an invented paraphrase of it silently matches nothing.
SUGGESTS = (
    "ERROR: (gcloud.compute.instances.create) Could not fetch resource:\n"
    " - A g2-standard-8 VM instance is currently unavailable in the us-central1-a "
    "zone. Consider trying your request in the us-central1-c zone.\n"
)


class Cloud:
    """Creates that succeed or refuse, per zone, and a record of every attempt."""

    def __init__(self, refuse=None):
        self.refuse = dict(refuse or {})
        self.created: list[tuple[str, str]] = []

    def create_instance_from_image(self, name, zone, project, **kwargs):
        self.created.append((name, zone))
        problem = self.refuse.get(zone)
        if problem is not None:
            raise GcloudError("Could not fetch resource", raw=problem)

    def machine_types(self, project, zone_list, name):
        return [{"name": name, "zone": zone} for zone in zone_list]

    def accelerator_types(self, project, name):
        # `--zone` checks the card as well as the machine type, so a fake that
        # only answers about machine types answers half the question.
        return [{"name": name, "zone": zone} for zone in
                (f"us-central1-{letter}" for letter in "abcdef")]


def order(*zones_):
    return Ordering(zones=tuple(zones_), regions=tuple(dict.fromkeys(
        zone.rsplit("-", 1)[0] for zone in zones_)))


def test_the_first_zone_with_room_is_the_one_used():
    cloud = Cloud()
    said = []
    zone = build(cloud, LINUX_L4, order("us-central1-a", "us-central1-b"),
                 PROJECT, said.append)
    assert zone == "us-central1-a"
    assert cloud.created == [("comfy-linux", "us-central1-a")]


def test_a_stockout_falls_through_to_the_next_zone():
    cloud = Cloud(refuse={"us-central1-a": STOCKOUT})
    said = []
    zone = build(cloud, LINUX_L4, order("us-central1-a", "us-central1-b"),
                 PROJECT, said.append)
    assert zone == "us-central1-b"
    assert "no L4 free right now" in " ".join(said)


def test_every_zone_is_announced_before_it_is_tried():
    """A silent thirty-second pause reads as a hang, and the pause is normal."""
    said = []
    build(Cloud(refuse={"us-central1-a": STOCKOUT}),
          LINUX_L4, order("us-central1-a", "us-central1-b"), PROJECT, said.append)
    assert said[0] == "trying us-central1-a…"
    assert "trying us-central1-b…" in said


def test_a_zone_google_suggests_is_tried_before_the_ones_we_ranked():
    """Google's answer is fresher than anything measured beforehand."""
    cloud = Cloud(refuse={"us-central1-a": SUGGESTS})
    zone = build(cloud, LINUX_L4, order("us-central1-a", "us-central1-b"),
                 PROJECT, lambda line: None)
    assert zone == "us-central1-c"
    assert [made[1] for made in cloud.created] == ["us-central1-a", "us-central1-c"]


def test_a_suggested_zone_already_tried_is_not_tried_twice():
    cloud = Cloud(refuse={"us-central1-a": SUGGESTS, "us-central1-c": SUGGESTS})
    with pytest.raises(LifecycleError):
        build(cloud, LINUX_L4, order("us-central1-a"), PROJECT, lambda line: None)
    assert [made[1] for made in cloud.created] == ["us-central1-a", "us-central1-c"]


def test_running_out_of_zones_says_nothing_is_billing():
    cloud = Cloud(refuse={zone: STOCKOUT for zone in ("us-central1-a", "us-central1-b")})
    with pytest.raises(LifecycleError) as raised:
        build(cloud, LINUX_L4, order("us-central1-a", "us-central1-b"),
              PROJECT, lambda line: None)
    assert "every zone tried is out of L4 capacity" in str(raised.value)
    assert "nothing is billing" in str(raised.value)
    assert raised.value.kind == EXHAUSTED


def test_a_refusal_that_is_not_a_stockout_stops_rather_than_trying_everywhere():
    """Trying nine more zones against a bad argument wastes five minutes and
    tells you nothing new."""
    denied = "ERROR: (gcloud.compute.instances.create) Required 'compute.instances.create' permission"
    cloud = Cloud(refuse={"us-central1-a": denied})
    with pytest.raises(LifecycleError) as raised:
        build(cloud, LINUX_L4, order("us-central1-a", "us-central1-b"),
              PROJECT, lambda line: None)
    assert "Google refused to create comfy-linux in us-central1-a" in str(raised.value)
    assert raised.value.kind == CREATE_FAILED
    assert len(cloud.created) == 1


def test_the_create_passes_the_accelerator_only_for_an_attached_card():
    seen = {}

    class Recording(Cloud):
        def create_instance_from_image(self, name, zone, project, **kwargs):
            seen.update(kwargs)

    create_in(Recording(), LINUX_L4, "us-central1-a", PROJECT)
    assert seen["accelerator"] is None
    create_in(Recording(), LINUX_T4, "us-central1-a", PROJECT)
    assert seen["accelerator"] == "type=nvidia-tesla-t4,count=1"


def test_the_create_asks_for_the_image_family_and_the_disk_it_planned():
    seen = {}

    class Recording(Cloud):
        def create_instance_from_image(self, name, zone, project, **kwargs):
            seen.update(kwargs)

    create_in(Recording(), Blueprint(name="b", image=IMAGES["windows"],
                                     card=CARDS["l4"], disk_gb=500),
              "us-central1-a", PROJECT)
    assert seen["image_family"] == "windows-2022"
    assert seen["image_project"] == "windows-cloud"
    assert seen["disk_gb"] == 500
    assert seen["metadata"] == "enable-windows-ssh=TRUE"


# --- the overrides ---------------------------------------------------------


def test_an_explicit_zone_is_used_alone_with_no_fall_through():
    check = check_quota(CARDS["l4"], LIVE, [])
    ordering = order_zones(Cloud(), PROJECT, LINUX_L4, check, zone="us-central1-f")
    assert ordering.zones == ("us-central1-f",)
    assert "no fall-through" in ordering.notes[0]


def test_an_explicit_zone_that_never_offers_the_machine_type_is_refused():
    class NoMachines(Cloud):
        def machine_types(self, project, zone_list, name):
            return []

    check = check_quota(CARDS["l4"], LIVE, [])
    with pytest.raises(LifecycleError) as raised:
        order_zones(NoMachines(), PROJECT, LINUX_L4, check, zone="us-central1-f")
    assert "us-central1-f does not offer g2-standard-8" in str(raised.value)
    assert raised.value.kind == NO_ZONE


def test_a_region_with_no_quota_is_refused_rather_than_silently_widened():
    check = check_quota(CARDS["l4"], LIVE, [])
    with pytest.raises(LifecycleError) as raised:
        order_zones(Cloud(), PROJECT, LINUX_L4, check, region="me-west1")
    assert "no L4 quota in me-west1" in str(raised.value)


def test_nowhere_to_put_it_names_both_halves_of_the_answer():
    problem = nowhere(LINUX_L4, Ordering(zones=(), regions=()), PROJECT)
    assert "nowhere to put comfy-linux" in str(problem)
    assert "accelerator-types list" in problem.fix
    assert "auth quota list" in problem.fix


# --- the host list entry ---------------------------------------------------


def test_the_created_box_is_recorded_the_way_discovery_would_record_it():
    """Otherwise `host discover` finds this instance and adds it a second time
    under a different port, and two entries point at one machine."""
    from comfy_qa.discover import parse

    made = host_entry(WIN_L4, "us-central1-a", PROJECT)
    found = parse({
        "name": "comfy-win",
        "status": "RUNNING",
        "zone": ".../zones/us-central1-a",
        "disks": [{"boot": True, "licenses": [".../windows-server-2022-dc"]}],
        "guestAccelerators": [{"acceleratorType": ".../nvidia-l4"}],
    }, PROJECT)
    assert made == found
