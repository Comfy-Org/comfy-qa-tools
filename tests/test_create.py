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

from comfy_qa.config import ConfigError, Host, resolve
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
from comfy_qa.zones import Ordering, region_of

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


def instance(name, *, running=True, gpu=True, zone="us-central1-a"):
    # The zone arrives as a URL, as it does from `gcloud compute instances list`
    # — the refusal for a full ceiling has to hand over a command that stops the
    # box, and `--zone=` is half of that command.
    body = {"name": name, "status": "RUNNING" if running else "TERMINATED",
            "zone": f"https://www.googleapis.com/compute/v1/projects/p/zones/{zone}"}
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
    assert any("down" in line for line in next_steps(blueprint, "us-central1-a"))


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


# The ceiling metered twice, the way the L4 grant already is in this file: -1 in
# the zone-scoped copy, 1 in the region-scoped one. A live project carries both.
CEILING_ZONE_COPY = {
    "quotaId": "GPUS-ALL-REGIONS-per-project-zone",
    "dimensionsInfos": [{
        "dimensions": None,
        "details": {"value": "-1"},
        "applicableLocations": L4_ZONES,
    }],
}


def test_the_project_wide_ceiling_is_read_off_the_live_shape():
    assert global_allowance(LIVE) == 1
    assert global_allowance([L4_REGION_QUOTA]) is None


def test_the_zone_scoped_copy_of_the_ceiling_does_not_free_the_gate():
    """The unit is pinned in test_create_hostile.py; this is what it costs.

    The ceiling is 1 on this project, so it governs every create — and read as
    unlimited the gate goes quiet: no refusal, and no mention of the GPU box
    that is running and billing, which is the only place `create` says so. Both
    orderings, because the record order is gcloud's to choose.
    """
    for quotas in ([CEILING_ZONE_COPY, *LIVE], [*LIVE, CEILING_ZONE_COPY]):
        check = check_quota(CARDS["l4"], quotas, [instance("console-box")])
        assert check.global_limit == 1
        problem = check.problem()
        assert problem is not None, "a ceiling of 1 with a box on it must refuse"
        assert "console-box is already running on it" in str(problem)


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
    assert "quota request" in problem.fix
    assert problem.kind == NO_QUOTA


def test_a_card_that_needs_more_of_the_allowance_than_is_granted_is_refused():
    # The family shape. `NVIDIA-H100-80GB-GPUS-per-project-region` exists in no
    # form on any project — see `create.CARDS`, which records that the H100 has
    # no `-80GB-` quota row — so a fixture using it tested a grant nobody holds.
    h100 = {"quotaId": "GPUS-PER-GPU-FAMILY-per-project-region",
            "dimensionsInfos": [{"dimensions": {"gpu_family": "NVIDIA_H100"},
                                 "details": {"value": "1"},
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
    assert ("gcloud compute instances stop comfy-win --zone=us-central1-a"
            in problem.fix)


def test_the_ceiling_refusal_hands_over_a_command_that_runs():
    """The command a money refusal hands over is one that runs.

    `config.resolve` matches host-list names, then os/gpu descriptions, and never
    `gce_instance`. The box holding the only slot is usually one somebody started
    in the console, which is precisely the box absent from the host list — so
    `comfy-qat down <instance>` would exit "no host called that", and the zone
    that makes the raw gcloud stop runnable is right there in the payload.
    """
    problem = check_quota(CARDS["l4"], [L4_REGION_QUOTA, ceiling(1)],
                          [instance("console-box", zone="us-west4-b")]).problem()
    assert problem is not None

    assert ("gcloud compute instances stop console-box --zone=us-west4-b"
            in problem.fix)
    assert "comfy-qat down console-box" not in problem.fix

    declared = [Host(name="comfy-win", kind="gce", port=8190,
                     os="Windows Server 2022", gpu="L4",
                     gce_instance="console-box", gce_zone="us-central1-a",
                     gce_project="proj")]
    with pytest.raises(ConfigError):
        resolve(declared, "console-box")      # the command it just printed


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


# A grant whose limit parses and whose region set comes back empty. Two things
# look like this: a project that really holds a grant covering nowhere, and a
# payload whose shape was not read — which is why the refusals built on a number
# that WAS read are asked before it.
ORPHAN_L4 = {"quotaId": "NVIDIA-L4-GPUS-per-project-region",
             "dimensionsInfos": [{"details": {"value": "1"},
                                  "applicableLocations": []}]}


def test_a_grant_that_names_no_region_is_refused_rather_than_searched():
    problem = check_quota(CARDS["l4"], [ORPHAN_L4, ceiling(1)], []).problem()
    assert "names no region" in str(problem)
    assert "The grant itself is 1" in str(problem), (
        "the grant size is what tells this apart from holding no quota at all")


def test_a_box_holding_the_ceiling_is_named_even_when_the_grant_names_no_region():
    """Both are true and only one gets printed, so which one is a money decision.

    The ceiling refusal is the only sentence in `problem()` that says a GPU box
    is running right now, and its fix stops it tonight. The region wording sends
    you to Google to wait for a grant. Hiding the first behind the second loses
    the mention of a live box, and this project's ceiling is 1, so that is the
    refusal a tester meets most.

    Order-only, and deliberately not a docs echo: moving the missing-region
    block back to the front of `problem()` — the wording untouched, which is how
    it got there — turns this red.
    """
    problem = check_quota(CARDS["l4"], [ORPHAN_L4, ceiling(1)],
                          [instance("console-box")]).problem()

    assert "console-box is already running on it" in str(problem)
    assert "names no region" not in str(problem)
    assert "gcloud compute instances stop console-box" in problem.fix


def test_the_ceiling_itself_is_named_even_when_the_grant_names_no_region():
    """The same decision with no box running: GPUS_ALL_REGIONS is a number that
    was read, an empty region set is an absence, and the number wins."""
    problem = check_quota(CARDS["l4"], [ORPHAN_L4, ceiling(0)], []).problem()

    assert "GPUS_ALL_REGIONS is 0" in str(problem)
    assert "names no region" not in str(problem)


def test_a_project_with_no_record_of_the_card_still_says_so():
    """The case 3e6feb0 moved the region block to the front to fix, which was
    never broken. `not regions and card_limit` and `not card_limit` cannot both
    be true, so the two orderings are indistinguishable here — running the
    parent commit says exactly this. Pinned so the premise is not re-derived.
    """
    problem = check_quota(CARDS["l4"], [ceiling(1)], []).problem()

    assert "this project has no L4 quota" in str(problem)


@pytest.mark.parametrize("instances,expected", [
    ([], []),
    ([instance("a"), instance("b", running=False)], [("a", "us-central1-a")]),
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
        """Answered even though this file's own `order_zones` never asks.

        A `--zone` override has two halves to check, not one: a zone can offer
        `n1-standard-8` — nearly every zone does — and have no T4 in it at all,
        so checking only the machine type passes a zone where the card has never
        existed. A fake that answers only about machine types cannot tell the
        difference, and a create.py that closes that hole would fail here with an
        AttributeError rather than with a result. Answering both halves keeps
        this file honest against either version.
        """
        return [{"name": name, "zone": zone} for zone in
                (f"{region}-{letter}" for region in L4_REGIONS for letter in "abcf")]


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


def test_a_create_that_timed_out_keeps_the_half_made_box_advice():
    """A non-capacity refusal always tells you to look for an instance that may
    exist, whatever gcloud's own advice was.

    A timeout is the case that needs it most — the create may well have made the
    box — and a timeout is also the refusal that carries a `fix`, so a fallback
    dropped the console check from exactly the failures that could leave
    something billing.

    Deliberately not this file's own `Cloud`: that fake raises GcloudError with
    no `fix`, and a present `exc.fix` is the entire condition under test.
    """
    class _Timeout:
        def create_instance_from_image(self, *args, **kwargs):
            raise GcloudError(
                "gcloud timed out after 300s: compute instances create comfy-linux",
                fix="check your network, then try again",   # what TIMEOUT carries
                kind="timeout",
            )

    with pytest.raises(LifecycleError) as caught:
        build(_Timeout(), LINUX_L4, order("us-central1-a"), PROJECT,
              lambda line: None)

    # Both: gcloud's advice about the refusal, and the check that finds the box
    # the refusal may have left behind.
    assert "check your network, then try again" in caught.value.fix
    assert "half-made" in caught.value.fix


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

# The zone the override tests name. `-f` on purpose: it is the odd zone in
# us-central1 — it has T4 and no G2 on the live project — so a fake that only
# happens to cover a, b and c would pass these for the wrong reason.
OVERRIDE_ZONE = "us-central1-f"


def test_the_fake_offers_the_card_in_the_zone_the_override_tests_name():
    """A precondition of the two tests below, asserted rather than assumed.

    `order_zones(zone=...)` has two halves to satisfy: the zone must offer the
    machine type *and* the card. A `Cloud` whose `accelerator_types` does not
    cover OVERRIDE_ZONE turns the next test into "us-central1-f has never
    offered nvidia-l4" — it still fails, but for a reason that has nothing to do
    with what it is testing, and the message points at the tool rather than at
    the fixture. This is the line that says so.
    """
    offered = {entry["zone"] for entry in Cloud().accelerator_types(PROJECT, "nvidia-l4")}
    assert OVERRIDE_ZONE in offered, (
        f"the Cloud fake does not offer the card in {OVERRIDE_ZONE}, which the "
        f"--zone tests below rely on. Widen its accelerator_types."
    )


def test_an_explicit_zone_is_used_alone_with_no_fall_through():
    check = check_quota(CARDS["l4"], LIVE, [])
    ordering = order_zones(Cloud(), PROJECT, LINUX_L4, check, zone=OVERRIDE_ZONE)
    assert ordering.zones == (OVERRIDE_ZONE,)
    assert "no fall-through" in ordering.notes[0]


def test_an_explicit_zone_that_never_offers_the_machine_type_is_refused():
    class NoMachines(Cloud):
        def machine_types(self, project, zone_list, name):
            return []

    check = check_quota(CARDS["l4"], LIVE, [])
    with pytest.raises(LifecycleError) as raised:
        order_zones(NoMachines(), PROJECT, LINUX_L4, check, zone=OVERRIDE_ZONE)
    assert f"{OVERRIDE_ZONE} does not offer g2-standard-8" in str(raised.value)
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
    assert "quota list" in problem.fix


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


# --- a card is spending from allocation, not from RUNNING --------------------

def _box(status, count=1):
    return {"name": "b", "status": status, "zone": ".../zones/us-central1-a",
            "guestAccelerators": [{"acceleratorType": ".../nvidia-l4",
                                   "acceleratorCount": count}]}


@pytest.mark.parametrize("state", ["RUNNING", "STAGING", "PROVISIONING",
                                   "REPAIRING", "STOPPING", "SUSPENDED"])
def test_a_box_that_is_not_terminated_holds_the_ceiling(state):
    """Google counts an accelerator against quota from the moment it is
    allocated, not from the moment the box finishes booting. A GPU box in STAGING
    holds the single-GPU ceiling and is billing — and skipping it let `create`
    start a second one against a project whose only slot was already taken.

    That is a start against a full ceiling, which is the one thing this gate
    exists to prevent, and the window is the first 30-60 seconds of every box.
    """
    from comfy_qa.create import _cards_running, _gpu_boxes_running

    assert _cards_running([_box(state)]) == 1, state
    assert _gpu_boxes_running([_box(state)]) == [("b", "us-central1-a")], state


def test_a_terminated_box_holds_nothing():
    from comfy_qa.create import _cards_running, _gpu_boxes_running

    assert _cards_running([_box("TERMINATED")]) == 0
    assert _gpu_boxes_running([_box("TERMINATED")]) == []


def test_a_zone_in_an_ungranted_region_says_where_the_grant_does_apply():
    """A mistyped `--zone` and a real quota gap read identically, and nothing
    offline can tell them apart — so the message names the regions that DO work.

    `--zone us-central9-a` becomes `us-central9`, which this project genuinely
    holds no quota in: the sentence is true either way. There is deliberately no
    built-in list of Google's regions to check against, because Google adds
    regions and a stale list would refuse a real one. Naming the grant's own
    regions costs nothing — they are already read — and settles it at a glance:
    `us-central9` beside a list containing `us-central1` is its own diagnosis.
    """
    check = check_quota(CARDS["l4"], LIVE, [])
    with pytest.raises(LifecycleError) as caught:
        order_zones(Cloud(), PROJECT, LINUX_L4, check, zone="us-central9-a")

    message = str(caught.value)
    assert "no L4 quota in us-central9" in message
    assert "It holds L4 in" in message, message
    assert check.regions[0] in message, "a region it does hold, named"
    assert f"and {len(check.regions) - 3} more" in message, (
        "forty-three regions is not a sentence; three and a count is")
    # The cheap check first, and never a quota request for a region that may not
    # exist as the opening move.
    assert "gcloud compute zones list --filter=name=us-central9-a" in caught.value.fix


def test_a_region_with_no_grant_says_where_the_grant_does_apply_and_reads_the_fix():
    """The sibling above, reached through `--region`, which did not read this way.

    Two commits improved the `--zone` refusal — name the regions the grant DOES
    apply in, then lead the advice with a spelling check instead of a quota
    request — and both stopped at that branch. Sixty lines below it `--region`
    went on saying only "no L4 quota in me-west1", with a fix whose opening move
    was `quota request --region me-west1`: for a mistyped region, a request to
    Google for a place it has never had, and days of waiting to find that out.

    Nothing pinned it. Three tests reach this branch and all three assert the
    half that was never wrong — the reason sentence — and NOT ONE reads `.fix`,
    so the advice could have said anything at all. This one reads it, and reads
    the order, because leading with the cheap check is the whole of that fix.
    """
    check = check_quota(CARDS["l4"], LIVE, [])
    with pytest.raises(LifecycleError) as caught:
        order_zones(Cloud(), PROJECT, LINUX_L4, check, region="me-west1")

    message = str(caught.value)
    assert "no L4 quota in me-west1" in message
    assert "It holds L4 in" in message, message
    assert check.regions[0] in message, "a region it does hold, named"
    assert f"and {len(check.regions) - 3} more" in message, (
        "forty-three regions is not a sentence; three and a count is")
    # `me-west1` is real, and the payload cannot show that. Same bar as the
    # zone branch: never tell somebody their correct spelling is wrong.
    for wrong in ("does not exist", "no such", "not a region", "typo"):
        assert wrong not in message.lower(), message

    fix = caught.value.fix
    assert "gcloud compute regions list --filter=name=me-west1" in fix, fix
    assert fix.index("gcloud compute regions list") < fix.index("comfy-qat quota"), (
        f"the cheap check comes first — a quota request for a region Google may "
        f"never have had is a slow way to learn you mistyped: {fix}")


def test_a_real_region_with_no_grant_is_not_accused_of_being_a_typo():
    """The refusal reads the same for `me-west1-a` as for `us-central9-a`, and
    that sameness is the fix, not a gap in it.

    The standing idea for telling them apart is to treat the union of
    `applicableLocations` across the quota records as a live list of Google's
    real regions and call anything missing from it a typo. This pins why that is
    wrong: `Gcloud.gpu_quotas` keeps only GPU-mentioning records, and the
    region-scoped record lists where the grant APPLIES — so `me-west1`, a real
    region and the docs' own example of a genuine gap, is absent from the whole
    payload. Reading absence as "no such region" would tell someone their
    correct spelling is wrong, which is worse than saying nothing about it.
    """
    payload_mentions = {
        location
        for quota in LIVE
        for info in quota["dimensionsInfos"]
        for location in info.get("applicableLocations") or []
    }
    assert not any(place.startswith("me-west1") for place in payload_mentions), (
        "a real region the project holds no grant in is absent from the payload, "
        "so absence cannot mean the region does not exist")

    check = check_quota(CARDS["l4"], LIVE, [])
    with pytest.raises(LifecycleError) as caught:
        order_zones(Cloud(), PROJECT, LINUX_L4, check, zone="me-west1-a")

    message = str(caught.value)
    assert "no L4 quota in me-west1" in message
    assert "It holds L4 in" in message, message
    # Nothing in here may claim the zone or region is unreal. That claim is only
    # ever a guess, and it is wrong in exactly this case.
    for wrong in ("does not exist", "no such", "not a region", "typo"):
        assert wrong not in message.lower(), message


# --- what "every zone tried" is allowed to mean -----------------------------


def looked_at(*zones_, offering=()):
    """An `Ordering` that remembers how much of the world it drew from.

    `order` above leaves `offering` empty on purpose — that is the `--zone` case,
    where nothing was ranked — so a test about scope has to say so itself.
    """
    return Ordering(zones=tuple(zones_),
                    regions=tuple(dict.fromkeys(region_of(zone) for zone in zones_)),
                    offering=tuple(offering))


def test_running_out_of_the_zones_it_looked_at_is_not_running_out_everywhere():
    """The most expensive kind of true sentence.

    Six European zones stocked out and the refusal said "every zone tried is out
    of L4 capacity". Every word was true. `create --region us-central1` succeeded
    on its second zone immediately afterwards, in the region this project's whole
    fleet already lives in — so the conclusion a person draws from that sentence,
    that there is no L4 to be had, was false.
    """
    tried = ("europe-west2-a", "europe-west4-a")
    cloud = Cloud(refuse={zone: STOCKOUT for zone in tried})
    with pytest.raises(LifecycleError) as raised:
        build(cloud, LINUX_L4,
              looked_at(*tried, offering=["europe-west2", "europe-west4",
                                          "us-central1", "us-east1"]),
              PROJECT, lambda line: None)
    said = str(raised.value)
    assert "2 of the 4 regions this project can use the card in" in said
    assert "not everywhere" in said
    assert raised.value.kind == EXHAUSTED


def test_the_refusal_names_the_regions_it_never_reached():
    """"Somewhere else" is advice nobody can act on. A region name is."""
    cloud = Cloud(refuse={"europe-west2-a": STOCKOUT})
    with pytest.raises(LifecycleError) as raised:
        build(cloud, LINUX_L4,
              looked_at("europe-west2-a",
                        offering=["europe-west2", "us-central1", "us-east1"]),
              PROJECT, lambda line: None)
    assert "us-central1, us-east1" in str(raised.value)


def test_the_fix_names_the_flag_that_would_have_worked():
    """`--region` was the flag that found capacity, and it was not mentioned.

    The advice was "wait, or ask for a different card" — which sends someone to a
    card they may not have quota for, while the card they do have is sitting free
    two regions away.
    """
    cloud = Cloud(refuse={"europe-west2-a": STOCKOUT})
    with pytest.raises(LifecycleError) as raised:
        build(cloud, LINUX_L4,
              looked_at("europe-west2-a", offering=["europe-west2", "us-central1"]),
              PROJECT, lambda line: None)
    assert "--region us-central1" in raised.value.fix


def test_a_stockout_everywhere_the_card_may_be_had_says_exactly_that():
    """The other half of the same honesty. When it IS everywhere, say so."""
    tried = ("us-central1-a", "us-east1-a")
    cloud = Cloud(refuse={zone: STOCKOUT for zone in tried})
    with pytest.raises(LifecycleError) as raised:
        build(cloud, LINUX_L4,
              looked_at(*tried, offering=["us-central1", "us-east1"]),
              PROJECT, lambda line: None)
    said = str(raised.value)
    assert "every region this project can use the card in" in said
    assert "not everywhere" not in said


def test_one_named_zone_claims_nothing_about_how_wide_the_search_was():
    """`--zone` ranked nothing, so this frame cannot count regions and must not.

    A sentence about how much of the world was considered would be an invention
    here, and the invented version reads exactly like the measured one.
    """
    cloud = Cloud(refuse={"us-central1-f": STOCKOUT})
    with pytest.raises(LifecycleError) as raised:
        build(cloud, LINUX_L4, order("us-central1-f"), PROJECT, lambda line: None)
    said = str(raised.value)
    assert "every zone tried is out of L4 capacity: us-central1-f" in said
    assert "regions this project" not in said


def test_the_cap_offers_the_flag_that_widens_the_search_as_well_as_the_one_that_narrows_it():
    """Being told a neighbourhood is short and offered `--zone` asks you to name
    one machine room in it."""
    cloud = Cloud(refuse={zone: STOCKOUT for zone in
                          ("us-central1-a", "us-central1-b", "us-central1-c")})
    with pytest.raises(LifecycleError) as raised:
        build(cloud, LINUX_L4,
              order("us-central1-a", "us-central1-b", "us-central1-c"),
              PROJECT, lambda line: None, attempts=2)
    assert "stopped after 2 zones" in str(raised.value)
    assert "--region" in raised.value.fix
    assert "--zone" in raised.value.fix


# --- the reservation limit: cards a reservation holds, running or not ----------
#
# A reservation holds its card from the moment it is made until it is deleted,
# whether or not its box is running — so the ceiling arithmetic that counted
# "cards on boxes that are not TERMINATED" undercounts by exactly the reserved
# boxes that are stopped. On a project whose ceiling is 1 that is the difference
# between refusing for free and creating something Google then refuses.
#
# The reservation records are FIXTURES shaped on the SDK schema, not read live.

from comfy_qa import reservation as rsv  # noqa: E402

T4_CARD = "nvidia-tesla-t4"


def held_for(box, zone="us-central1-a", *, machine="n1-standard-8", card=T4_CARD,
             count=1, ours=True, in_use=None, name=None):
    """One `reservations list` row, parsed the way the tool parses it."""
    properties = {"machineType": machine}
    if card:
        properties["guestAccelerators"] = [{"acceleratorType": card,
                                            "acceleratorCount": count}]
    specific = {"count": "1", "instanceProperties": properties}
    if in_use is not None:
        specific["inUseCount"] = str(in_use)
    (found,) = rsv.parse_all([{
        "name": name or f"{box}-rsv",
        "zone": f"https://www.googleapis.com/compute/v1/projects/p/zones/{zone}",
        "status": "READY",
        "specificReservationRequired": True,
        "description": f"comfy-qat: held for {box}" if ours else "held by hand",
        "specificReservation": specific,
    }])
    return found


def bound(name, reservation_name, *, running=True, zone="us-central1-a"):
    """A box that may only consume one reservation, as `instances list` shows it."""
    return dict(instance(name, running=running, zone=zone),
                reservationAffinity={"consumeReservationType": "SPECIFIC_RESERVATION",
                                     "key": "compute.googleapis.com/reservation-name",
                                     "values": [reservation_name]})


def declared_box(name, zone="us-central1-a"):
    """A host list entry that IS the instance of that name, in that zone."""
    return Host(name=name, kind="gce", port=8191, os="Ubuntu 22.04", gpu="T4",
                gce_instance=name, gce_zone=zone, gce_project=PROJECT)


def gate(ceiling_value, instances=(), reservations=(), card="t4", **kwargs):
    quotas = [T4_QUOTA, L4_REGION_QUOTA]
    if ceiling_value is not None:
        quotas = quotas + [ceiling(ceiling_value)]
    return check_quota(CARDS[card], quotas, list(instances),
                       reservations=None if reservations is None else list(reservations),
                       project=PROJECT, **kwargs)


def test_a_reservation_holding_the_whole_ceiling_refuses_in_the_words_the_design_fixes():
    """The sentence, and the two commands, typed out. One reservation, made by
    this tool, with its box running on it."""
    check = gate(1, [bound("comfy-linux", "comfy-linux-rsv")],
                 [held_for("comfy-linux", in_use=1)], hosts=[declared_box("comfy-linux")])
    problem = check.problem()

    assert problem is not None and problem.kind == NO_QUOTA
    assert str(problem) == (
        "GPUS_ALL_REGIONS is 1 on this project, and 1 of it is held by 1 "
        "reservation: comfy-linux-rsv (us-central1-a). A reservation holds its "
        "card whether its box is running or stopped, so stopping a box frees "
        "nothing, and 1 more is needed. Nothing was created."
    )
    assert problem.fix.splitlines()[0] == "stop it, then delete it to release the card:"
    assert [line.strip() for line in problem.fix.splitlines()[1:]] == [
        "comfy-qat down comfy-linux",
        "comfy-qat delete comfy-linux",
    ]


def test_a_reserved_box_that_is_stopped_still_refuses_the_next_one():
    """THE CASE THE OLD ARITHMETIC MISSED. The box is TERMINATED, which counts
    for nothing as a running box — and its reservation still holds the card."""
    stopped = [bound("comfy-linux", "comfy-linux-rsv", running=False)]
    held = [held_for("comfy-linux", in_use=0)]

    assert gate(1, stopped, []).problem() is None, (
        "the fixture: without the reservation this is a project with a free ceiling")
    problem = gate(1, stopped, held, hosts=[declared_box("comfy-linux")]).problem()
    assert problem is not None
    assert "held by 1 reservation: comfy-linux-rsv (us-central1-a)" in str(problem)
    assert "comfy-qat delete comfy-linux" in problem.fix


def test_a_reserved_box_that_is_running_holds_its_card_once_not_twice():
    """Ceiling 2, one reserved box running. One card is held, so there is room
    for one more — counting the box AND its reservation would refuse it."""
    check = gate(2, [bound("comfy-linux", "comfy-linux-rsv")],
                 [held_for("comfy-linux", in_use=1)])

    assert check.held == 1
    assert check.problem() is None


def test_a_reservation_nobody_made_with_this_tool_is_released_with_googles_command():
    """There is no box to `comfy-qat delete`, and a name read out of somebody
    else's description is not a command."""
    theirs = held_for("x", ours=False, name="training-hold", zone="us-east1-b")
    problem = gate(1, [], [theirs]).problem()

    assert "held by 1 reservation: training-hold (us-east1-b)" in str(problem)
    assert "comfy-qat" not in problem.fix
    assert (f"gcloud compute reservations delete training-hold --zone=us-east1-b "
            f"--project={PROJECT}") in problem.fix


def test_our_own_reservation_with_no_box_on_it_is_not_given_a_command_for_a_box():
    """`comfy-qat delete comfy-linux` cannot work: there is no comfy-linux. A
    reservation left by a create that stopped half-way is released directly."""
    orphan = held_for("comfy-linux")
    problem = gate(1, [], [orphan]).problem()

    assert "comfy-qat down" not in problem.fix and "comfy-qat delete" not in problem.fix
    assert (f"gcloud compute reservations delete comfy-linux-rsv --zone=us-central1-a "
            f"--project={PROJECT}") in problem.fix


def test_two_reservations_are_counted_in_cards_and_both_named():
    held = [held_for("a", in_use=1), held_for("b", zone="asia-east1-a")]
    problem = gate(2, [bound("a", "a-rsv")], held, hosts=[declared_box("a")]).problem()

    assert "GPUS_ALL_REGIONS is 2 on this project, and 2 of it is held by 2 " \
           "reservations: a-rsv (us-central1-a), b-rsv (asia-east1-a)." in str(problem)
    # One of each kind of remedy, so the fixture reaches both: `a` has a box,
    # `b` does not.
    assert "comfy-qat down a" in problem.fix and "comfy-qat delete a" in problem.fix
    assert "gcloud compute reservations delete b-rsv --zone=asia-east1-a" in problem.fix


def test_an_h100_reservation_holds_eight_of_the_ceiling():
    """Cards, not reservations. One reservation, eight cards."""
    big = held_for("trainer", machine="a3-highgpu-8g", card="nvidia-h100-80gb", count=8)
    problem = gate(8, [], [big]).problem()

    assert "GPUS_ALL_REGIONS is 8 on this project, and 8 of it is held by 1 " \
           "reservation: trainer-rsv (us-central1-a)." in str(problem)
    assert gate(9, [], [big]).problem() is None, "and nine has room for one T4"


def test_a_ceiling_with_room_beside_a_reservation_does_not_refuse_and_says_who_holds_what():
    check = gate(2, [bound("comfy-linux", "comfy-linux-rsv")],
                 [held_for("comfy-linux", in_use=1)])

    assert check.problem() is None
    assert ("reserved, and held whether its box runs or not: 1 card of it — "
            "comfy-linux-rsv (us-central1-a)") in check.lines()
    assert not [line for line in check.lines() if line.startswith("already running")], (
        "the reserved box is not ALSO listed as a running one")


@pytest.mark.parametrize("ceiling_value", [-1, None], ids=["unlimited", "not-reported"])
def test_a_ceiling_that_does_not_gate_does_not_gate_with_reservations_either(ceiling_value):
    """The sentinel and the absence, both: neither is a small number."""
    held = [held_for("a", in_use=1), held_for("b"), held_for("c")]
    assert gate(ceiling_value, [bound("a", "a-rsv")], held).problem() is None
    assert gate(ceiling_value, [bound("a", "a-rsv")], held, reserve=True,
                box="new").problem() is None


def test_one_reserved_and_one_plain_running_box_names_the_one_stopping_would_free():
    """Mixed, so each branch is reached by something that is really its own.
    Ceiling 2: the reservation holds one and cannot be freed by stopping; the
    plain running box holds the other and can. The remedy is to stop THAT one."""
    instances = [bound("held", "held-rsv", running=False), instance("plain")]
    check = gate(2, instances, [held_for("held", in_use=0)])
    problem = check.problem()

    assert check.held == 2
    assert check.running == (("plain", "us-central1-a"),)
    assert "plain is already running on it" in str(problem)
    assert "gcloud compute instances stop plain --zone=us-central1-a" in problem.fix
    assert "held" not in problem.fix


def test_when_the_reservations_alone_fill_the_ceiling_stopping_a_box_is_not_offered():
    """The same two boxes on a ceiling of 1: the reservation is the whole of it,
    and "stop the one you are not using" would free nothing."""
    instances = [bound("held", "held-rsv", running=False), instance("plain")]
    problem = gate(1, instances, [held_for("held", in_use=0)]).problem()

    assert "held by 1 reservation: held-rsv" in str(problem)
    assert "instances stop" not in problem.fix


def test_reserving_says_what_it_takes_and_what_that_leaves():
    check = gate(1, [], [], reserve=True, box="comfy-linux")
    assert check.problem() is None
    assert ("reserving takes 1 of the 1 — none left. While comfy-linux exists no "
            "other GPU box can start, including a stopped one you already have."
            ) in check.lines()


def test_reserving_with_room_to_spare_says_how_much_is_left():
    lines = gate(4, [], [], reserve=True, box="comfy-linux").lines()
    assert "reserving takes 1 of the 4 — 3 left for other GPU boxes while " \
           "comfy-linux exists." in lines


def test_not_reserving_says_nothing_about_reserving():
    """The unreserved path prints what it always printed: the two allowance
    lines and nothing else, whether the reservations were read and empty or
    not read at all."""
    plain = check_quota(CARDS["t4"], [T4_QUOTA, CEILING], [])
    assert gate_lines(reservations=[]) == plain.lines()
    assert gate_lines(reservations=None) == plain.lines()
    assert len(plain.lines()) == 2


def gate_lines(**kwargs):
    return check_quota(CARDS["t4"], [T4_QUOTA, CEILING], [], **kwargs).lines()


def test_reserving_when_the_reservations_could_not_be_read_is_refused():
    """NOT READ IS NOT ZERO. A refusal is free; a reservation made past the
    limit bills until somebody notices. `None` is "not read"."""
    problem = gate(1, [], None, reserve=True, box="comfy-linux").problem()

    assert problem is not None and problem.kind == NO_QUOTA
    assert "could not read this project's reservations" in str(problem)
    assert "Nothing was reserved and nothing was created." in str(problem)
    assert f"gcloud compute reservations list --project={PROJECT}" in problem.fix


def test_reserving_when_the_reservations_were_read_and_empty_is_not_refused():
    """The pair: `[]` is "read, and there are none"."""
    assert gate(1, [], [], reserve=True, box="comfy-linux").problem() is None


def test_not_reserving_when_the_reservations_could_not_be_read_carries_on():
    """An optional read is not an optional behaviour — and for a plain create
    this one only ever adds a refusal, so failing to make it must not add one."""
    assert gate(1, [], None).problem() is None


def test_a_running_box_still_refuses_when_the_reservations_could_not_be_read():
    """What WAS read still gates. A read that failed removes knowledge; it does
    not grant permission."""
    problem = gate(1, [instance("plain")], None, reserve=True).problem()
    assert "plain is already running on it" in str(problem)


def test_a_leftover_being_resumed_is_not_counted_against_the_box_it_was_made_for():
    """Rerunning a create that stopped half-way finds its own reservation on
    the project. Counted, it would refuse the box it exists for."""
    leftover = held_for("comfy-linux")

    counted = gate(1, [], [leftover], reserve=True, box="comfy-linux")
    assert counted.problem() is not None, "the fixture: it fills the ceiling"

    resumed = gate(1, [], [leftover], reserve=True, box="comfy-linux", leftover=leftover)
    assert resumed.problem() is None
    assert resumed.held == 0
    assert ("reusing reservation comfy-linux-rsv in us-central1-a — left by an "
            "earlier run, and already holding its card") in resumed.lines()
    assert not [line for line in resumed.lines() if line.startswith("reserving takes")], (
        "it takes nothing new")


def test_a_leftover_of_the_wrong_shape_is_still_counted():
    """Only a reservation this create can actually use is its own. An L4
    reservation under the same name holds a card this box will not get."""
    wrong = held_for("comfy-linux", machine="g2-standard-8", card="nvidia-l4")
    assert gate(1, [], [wrong], reserve=True, box="comfy-linux",
                leftover=wrong).problem() is not None


def test_a_leftover_is_not_set_aside_for_a_box_that_is_not_being_reserved():
    leftover = held_for("comfy-linux")
    assert gate(1, [], [leftover], leftover=leftover).problem() is not None


def test_the_refusal_without_a_project_still_hands_over_a_command_that_parses():
    """`project` is optional, and a command ending `--project=` is not one."""
    theirs = held_for("x", ours=False, name="training-hold")
    check = check_quota(CARDS["t4"], [T4_QUOTA, ceiling(1)], [], reservations=[theirs])

    fix = check.problem().fix
    assert "gcloud compute reservations delete training-hold --zone=us-central1-a" in fix
    assert "--project" not in fix


# --- where a reserved box may go --------------------------------------------------

T4_BLUEPRINT = Blueprint(name="comfy-linux", image=IMAGES["linux"], card=CARDS["t4"],
                         reserve=True)


def test_a_leftover_pins_the_create_to_the_zone_it_is_in():
    leftover = held_for("comfy-linux", zone="asia-east1-b")
    check = gate(1, [], [leftover], reserve=True, leftover=leftover)
    ordering = order_zones(Cloud(), PROJECT, T4_BLUEPRINT, check, leftover=leftover)

    assert ordering.zones == ("asia-east1-b",)
    assert ordering.fall_through is False
    assert ordering.notes == (
        "reusing reservation comfy-linux-rsv in asia-east1-b — left by an earlier "
        "run, so this zone or nothing",)


@pytest.mark.parametrize("asked", [dict(zone="us-central1-a"), dict(region="us-central1")],
                         ids=["zone", "region"])
def test_a_leftover_somewhere_else_than_was_asked_for_is_refused(asked):
    """A reservation cannot move. Making a second one where the user pointed
    would leave the first billing under the same name."""
    leftover = held_for("comfy-linux", zone="asia-east1-b")
    check = gate(1, [], [leftover], reserve=True, leftover=leftover)

    with pytest.raises(LifecycleError) as caught:
        order_zones(Cloud(), PROJECT, T4_BLUEPRINT, check, leftover=leftover, **asked)

    message = str(caught.value)
    assert "comfy-linux-rsv is already on this project in asia-east1-b" in message
    assert "Nothing was created." in message
    fix = caught.value.fix
    assert ("comfy-qat create --os linux --gpu t4 --reserve --name comfy-linux "
            "--zone asia-east1-b") in fix
    assert (f"gcloud compute reservations delete comfy-linux-rsv --zone=asia-east1-b "
            f"--project={PROJECT}") in fix


def test_a_leftover_in_the_zone_that_was_asked_for_is_simply_used():
    leftover = held_for("comfy-linux", zone="asia-east1-b")
    check = gate(1, [], [leftover], reserve=True, leftover=leftover)
    ordering = order_zones(Cloud(), PROJECT, T4_BLUEPRINT, check, leftover=leftover,
                           zone="asia-east1-b")
    assert ordering.zones == ("asia-east1-b",)


def test_a_leftover_that_does_not_fit_is_refused_with_the_command_that_releases_it():
    wrong = held_for("comfy-linux", ours=False)
    check = gate(2, [], [wrong], reserve=True, leftover=wrong)

    with pytest.raises(LifecycleError) as caught:
        order_zones(Cloud(), PROJECT, T4_BLUEPRINT, check, leftover=wrong)

    assert "a reservation called comfy-linux-rsv is already on this project" in str(caught.value)
    assert (f"gcloud compute reservations delete comfy-linux-rsv --zone=us-central1-a "
            f"--project={PROJECT}") in caught.value.fix
    assert "--name" in caught.value.fix, "or call the new box something else"


# --- the per-card regional allowance, minus what already holds it ------------------
#
# T4_QUOTA grants 1 T4 in asia-east1 and 1 in us-central1.


class T4Cloud(Cloud):
    def accelerator_types(self, project, name):
        return [{"name": name, "zone": zone}
                for zone in ("asia-east1-a", "asia-east1-b", "us-central1-a", "us-central1-b")]


def ordered(instances, reservations, **kwargs):
    check = gate(4, instances, reservations)
    return order_zones(T4Cloud(), PROJECT, T4_BLUEPRINT, check, probe=lambda region: 10.0,
                       instances=instances, reservations=reservations, **kwargs)


def test_a_region_whose_allowance_is_held_by_a_reservation_is_left_out(tmp_path):
    held = [held_for("other", zone="us-central1-b")]
    ordering = ordered([], held, config=tmp_path / "hosts.toml")

    assert ordering.zones == ("asia-east1-a", "asia-east1-b")
    assert any("us-central1" in note and "other-rsv" in note for note in ordering.notes), (
        ordering.notes)


def test_a_region_whose_allowance_is_held_by_a_running_box_is_left_out(tmp_path):
    running = [dict(instance("plain", zone="asia-east1-a"),
                    guestAccelerators=[{"acceleratorType": f".../{T4_CARD}",
                                        "acceleratorCount": 1}])]
    ordering = ordered(running, [], config=tmp_path / "hosts.toml")

    assert ordering.zones == ("us-central1-a", "us-central1-b")
    assert any("asia-east1" in note and "plain" in note for note in ordering.notes)


def test_a_box_holding_a_different_card_does_not_use_up_this_cards_region(tmp_path):
    """`instance()` attaches an L4. One L4 in us-central1 is none of the T4s."""
    ordering = ordered([instance("an-l4", zone="us-central1-a")], [],
                       config=tmp_path / "hosts.toml")
    assert set(ordering.zones) == {"asia-east1-a", "asia-east1-b",
                                   "us-central1-a", "us-central1-b"}


def test_every_region_full_is_refused_naming_the_regions_and_who_holds_them(tmp_path):
    held = [held_for("one", zone="us-central1-b"), held_for("two", zone="asia-east1-a")]
    instances = [bound("one", "one-rsv", zone="us-central1-b")]

    with pytest.raises(LifecycleError) as caught:
        ordered(instances, held, config=tmp_path / "hosts.toml",
                hosts=[declared_box("one", zone="us-central1-b")])

    message = str(caught.value)
    assert caught.value.kind == NO_QUOTA
    assert "asia-east1 (1 of 1, held by two-rsv)" in message
    assert "us-central1 (1 of 1, held by one-rsv)" in message
    assert "Nothing was created." in message
    assert "comfy-qat delete one" in caught.value.fix
    assert "gcloud compute reservations delete two-rsv --zone=asia-east1-a" in caught.value.fix


def test_a_named_zone_in_a_full_region_is_refused_and_points_at_one_with_room(tmp_path):
    held = [held_for("other", zone="us-central1-b")]

    with pytest.raises(LifecycleError) as caught:
        ordered([], held, config=tmp_path / "hosts.toml", zone="us-central1-a")

    assert "us-central1 (1 of 1, held by other-rsv)" in str(caught.value)
    assert ("comfy-qat create --os linux --gpu t4 --reserve --name comfy-linux "
            "--region asia-east1") in caught.value.fix


def test_without_the_instances_the_regions_are_not_narrowed_at_all(tmp_path):
    """A caller that hands over nothing to count gets today's ordering. Not
    counted is not "nothing held" — it is "not asked", and the answer to that
    is to leave the regions alone rather than to invent room or the lack of it."""
    held = [held_for("other", zone="us-central1-b")]
    check = gate(4, [], held)
    ordering = order_zones(T4Cloud(), PROJECT, T4_BLUEPRINT, check,
                           probe=lambda region: 10.0, config=tmp_path / "hosts.toml")

    assert {zone.rsplit("-", 1)[0] for zone in ordering.zones} == {"asia-east1", "us-central1"}


# --- audit: a remedy names a comfy-qat command only for a box the host list holds ---
#
# The limit refusal printed `comfy-qat down <box>` / `comfy-qat delete <box>`
# built from the instance name in the reservation's description, without asking
# whether the host list has that machine. Two ways that goes wrong, and the
# second destroys something:
#
#   * the box is on the project and not in the list — both commands exit 2
#     while the reservation bills;
#   * the list has an entry of that NAME pointing at a DIFFERENT instance —
#     pasted back, `comfy-qat delete comfy-linux` deletes that other machine
#     and leaves the reservation where it was.
#
# So a comfy-qat command is printed only when an entry's machine identity —
# project, zone, instance — is that instance, and then under the entry's own
# label. Anything else gets Google's commands.


def entry(label, instance_name="comfy-linux", zone="us-central1-a", project=PROJECT):
    return Host(name=label, kind="gce", port=8191, os="Ubuntu 22.04", gpu="T4",
                gce_instance=instance_name, gce_zone=zone, gce_project=project)


def refused_by_one_reservation(**kwargs):
    return gate(1, [bound("comfy-linux", "comfy-linux-rsv")],
                [held_for("comfy-linux", in_use=1)], **kwargs).problem()


RAW_DELETE = (f"gcloud compute instances delete comfy-linux --zone=us-central1-a "
              f"--project={PROJECT} --delete-disks=all --quiet")
RAW_RELEASE = (f"gcloud compute reservations delete comfy-linux-rsv "
               f"--zone=us-central1-a --project={PROJECT} --quiet")


def test_a_box_the_host_list_does_not_hold_is_not_given_a_comfy_qat_command():
    """An empty host list: the box is real, bound and running, and nothing this
    tool can be told by name. `comfy-qat down comfy-linux` would exit 2."""
    problem = refused_by_one_reservation(hosts=[])

    assert "comfy-qat" not in problem.fix
    assert RAW_DELETE in problem.fix
    assert RAW_RELEASE in problem.fix
    assert problem.fix.index(RAW_DELETE) < problem.fix.index(RAW_RELEASE), (
        "the box first: the reservation is in use while it is there")
    assert "not in your host list" in problem.fix


def test_a_caller_that_hands_over_no_host_list_gets_googles_commands():
    """Not told is not "declared". The call as it was before `hosts` existed
    must not guess a name into a destructive command."""
    problem = refused_by_one_reservation()
    assert "comfy-qat" not in problem.fix
    assert RAW_RELEASE in problem.fix


def test_an_entry_of_the_same_name_for_a_different_machine_is_not_named():
    """THE DATA-LOSS CASE. `comfy-linux` in the list is `my-own-instance` in
    europe-west4-a. `comfy-qat delete comfy-linux` would delete THAT."""
    other = entry("comfy-linux", instance_name="my-own-instance", zone="europe-west4-a")
    problem = refused_by_one_reservation(hosts=[other])

    assert "comfy-qat" not in problem.fix
    assert RAW_DELETE in problem.fix and RAW_RELEASE in problem.fix


@pytest.mark.parametrize("wrong", [
    dict(zone="us-central1-b"),
    dict(project="another-project"),
    dict(instance_name="comfy-linux-2"),
], ids=["zone", "project", "instance"])
def test_an_entry_that_differs_in_one_part_of_the_identity_is_not_that_machine(wrong):
    problem = refused_by_one_reservation(hosts=[entry("comfy-linux", **wrong)])
    assert "comfy-qat" not in problem.fix


def test_the_entry_that_is_that_machine_is_named_by_its_own_label():
    """Renamed by hand in the host list. The command takes the LABEL."""
    problem = refused_by_one_reservation(hosts=[entry("my-t4-box")])

    assert [line.strip() for line in problem.fix.splitlines()] == [
        "stop it, then delete it to release the card:",
        "comfy-qat down my-t4-box",
        "comfy-qat delete my-t4-box",
    ]
    assert "comfy-linux" not in problem.fix


def test_an_identity_that_cannot_be_established_names_no_entry():
    """With no project in hand there is no identity to compare, and a match on
    name and zone alone is the guess this fix exists to stop."""
    check = check_quota(CARDS["t4"], [T4_QUOTA, ceiling(1)],
                        [bound("comfy-linux", "comfy-linux-rsv")],
                        reservations=[held_for("comfy-linux", in_use=1)],
                        hosts=[entry("comfy-linux")])
    assert "comfy-qat" not in check.problem().fix


def test_a_full_region_names_a_box_only_by_the_entry_that_holds_it(tmp_path):
    """The second caller of the same remedy, so the second site."""
    held = [held_for("one", zone="us-central1-b"), held_for("two", zone="asia-east1-a")]
    instances = [bound("one", "one-rsv", zone="us-central1-b")]

    with pytest.raises(LifecycleError) as undeclared:
        ordered(instances, held, config=tmp_path / "hosts.toml")
    assert "comfy-qat down" not in undeclared.value.fix
    assert "comfy-qat delete" not in undeclared.value.fix

    with pytest.raises(LifecycleError) as declared:
        ordered(instances, held, config=tmp_path / "hosts.toml",
                hosts=[entry("box-one", instance_name="one", zone="us-central1-b")])
    assert "comfy-qat delete box-one" in declared.value.fix


# --- audit: the stock-out remedy for a reserved build keeps --reserve ---------------


def test_the_stockout_remedy_for_a_reserved_build_still_reserves():
    """Pasted back as printed, the old line made an UNRESERVED box."""
    class Reserving(Cloud):
        def create_reservation(self, name, zone, project, **kwargs):
            raise GcloudError("Could not fetch resource", raw=STOCKOUT)

        def reservation_absent(self, name, zone, project):
            return True

    ordering = Ordering(zones=("us-central1-a",), regions=("us-central1", "us-east1"),
                        offering=("us-central1", "us-east1"))
    with pytest.raises(LifecycleError) as caught:
        build(Reserving(), T4_BLUEPRINT, ordering, PROJECT, lambda _line: None)

    assert ("comfy-qat create --os linux --gpu t4 --reserve --name comfy-linux "
            "--region us-east1") in caught.value.fix


def test_the_stockout_remedy_for_a_plain_build_is_the_line_it_always_was():
    ordering = Ordering(zones=("us-central1-a",), regions=("us-central1", "us-east1"),
                        offering=("us-central1", "us-east1"))
    with pytest.raises(LifecycleError) as caught:
        build(Cloud(refuse={"us-central1-a": STOCKOUT}), LINUX_T4, ordering, PROJECT,
              lambda _line: None)

    assert "comfy-qat create --os linux --gpu t4 --region us-east1;" in caught.value.fix
    assert "--reserve" not in caught.value.fix


# --- audit: what reserving leaves, said truthfully ----------------------------------


def test_another_reserved_box_is_not_told_it_cannot_start():
    """Ceiling 2, one stopped reserved box, reserving a second. "No other GPU
    box can start, including a stopped one you already have" is false about
    that box: its own reservation holds its card."""
    lines = gate(2, [bound("held", "held-rsv", running=False)],
                 [held_for("held", in_use=0)], reserve=True, box="new").lines()
    (line,) = [text for text in lines if text.startswith("reserving takes")]

    assert line.startswith("reserving takes 1 of the 2, with 1 already held — none left.")
    assert "including a stopped one you already have" not in line
    assert "a box with a reservation of its own can still start" in line


def test_reserving_what_does_not_fit_is_not_announced_as_taking_it():
    """Directly above a refusal, "reserving takes 1 of the 1" is a sentence
    about something that is not going to happen."""
    check = gate(1, [], [held_for("other")], reserve=True, box="new")

    assert check.problem() is not None
    assert not [text for text in check.lines() if text.startswith("reserving takes")]


# --- recheck: Google's delete for a box takes its disk with it ----------------------
#
# This tool makes boot disks that do NOT auto-delete, so `gcloud compute
# instances delete <box>` alone frees the card and leaves 200 GB billing with
# nothing attached to it. `delete` passes `--delete-disks=all`; the remedy that
# hands the same command to a person has to as well. Checked at every refusal
# in this module that reaches it.

WITH_ITS_DISK = (f"gcloud compute instances delete comfy-linux --zone=us-central1-a "
                 f"--project={PROJECT} --delete-disks=all --quiet")


def test_the_limit_refusal_deletes_an_undeclared_box_with_its_disk():
    problem = refused_by_one_reservation(hosts=[])
    assert WITH_ITS_DISK in [line.strip() for line in problem.fix.splitlines()]


def test_the_limit_refusal_with_several_holders_deletes_an_undeclared_box_with_its_disk():
    held = [held_for("comfy-linux", in_use=1), held_for("b", zone="asia-east1-a")]
    problem = gate(2, [bound("comfy-linux", "comfy-linux-rsv")], held, hosts=[]).problem()
    assert WITH_ITS_DISK in [line.strip() for line in problem.fix.splitlines()]


def test_the_limit_refusal_with_no_project_still_takes_the_disk():
    check = check_quota(CARDS["t4"], [T4_QUOTA, ceiling(1)],
                        [bound("comfy-linux", "comfy-linux-rsv")],
                        reservations=[held_for("comfy-linux", in_use=1)])
    assert ("gcloud compute instances delete comfy-linux --zone=us-central1-a "
            "--delete-disks=all --quiet") in [
                line.strip() for line in check.problem().fix.splitlines()]


@pytest.mark.parametrize("asked", [dict(zone="us-central1-a"), dict(region="us-central1"), {}],
                         ids=["zone", "region", "every-region"])
def test_the_full_region_refusal_deletes_an_undeclared_box_with_its_disk(asked, tmp_path):
    held = [held_for("one", zone="us-central1-b"), held_for("two", zone="asia-east1-a")]
    if not asked:
        instances = [bound("one", "one-rsv", zone="us-central1-b")]
    else:
        held = held[:1]
        instances = [bound("one", "one-rsv", zone="us-central1-b")]

    with pytest.raises(LifecycleError) as caught:
        ordered(instances, held, config=tmp_path / "hosts.toml", **asked)

    assert (f"gcloud compute instances delete one --zone=us-central1-b "
            f"--project={PROJECT} --delete-disks=all --quiet") in [
                line.strip() for line in caught.value.fix.splitlines()]


def test_no_instance_delete_this_module_hands_over_leaves_the_disk():
    """The sweep, kept. Every `instances delete` written in create.py carries
    the flag in the same string, so a new one cannot be added without it."""
    import inspect

    from comfy_qa import create as module

    lines = [line for line in inspect.getsource(module).splitlines()
             if "compute instances delete" in line and not line.strip().startswith("#")]
    assert lines, "the remedy this guards has gone, or moved out of this module"
    assert all("--delete-disks=all --quiet" in line for line in lines), (
        "and --quiet with it: without it gcloud asks, and a script that cannot "
        f"answer exits 1 with the box and its disk still billing — {lines}")


# --- recheck: the full-region refusal on the --zone path is handed the host list -----


def test_a_named_zone_in_a_full_region_names_a_declared_box_by_its_entry(tmp_path):
    """`order_zones` reaches `_refuse_full` from two places. The `--region` and
    every-region path was pinned; this one could drop `hosts` with the suite
    green — safe, since it falls back to Google's commands, but not held."""
    held = [held_for("one", zone="us-central1-b")]
    instances = [bound("one", "one-rsv", zone="us-central1-b")]

    with pytest.raises(LifecycleError) as caught:
        ordered(instances, held, config=tmp_path / "hosts.toml", zone="us-central1-a",
                hosts=[entry("box-one", instance_name="one", zone="us-central1-b")])

    lines = [line.strip() for line in caught.value.fix.splitlines()]
    assert "comfy-qat down box-one" in lines and "comfy-qat delete box-one" in lines
    assert not [line for line in lines if line.startswith("gcloud compute instances delete")]


# --- recheck: every `comfy-qat create` this module hands back is the box asked for ---
#
# A remedy that rewrites the command and drops `--reserve` makes, pasted back
# under `--yes`, an unreserved box. One site was fixed for that and its siblings
# were not. So each site that prints a `comfy-qat create` is driven here for a
# reserved build, and one test at the end counts the sites in the source so a
# new one cannot arrive unlisted.


def _creates(fix):
    """Every `comfy-qat create …` in a fix, each cut where its prose begins."""
    import re

    return [found.strip() for found in
            re.findall(r"comfy-qat create[^,;\n]*", fix or "")]


def _plan_refusal(**kwargs):
    with pytest.raises(LifecycleError) as caught:
        plan(os_choice="linux", reserve=True, **kwargs)
    return caught.value


@pytest.mark.parametrize("disk", [5, 99999], ids=["too-small", "too-large"])
def test_the_disk_size_remedy_for_a_reserved_build_still_reserves(disk):
    assert _creates(_plan_refusal(gpu="l4", disk_gb=disk, name="mybox").fix) == [
        "comfy-qat create --os linux --gpu l4 --reserve --name mybox --disk 200"]
    assert _creates(_plan_refusal(gpu="l4", disk_gb=disk).fix) == [
        "comfy-qat create --os linux --gpu l4 --reserve --disk 200"], (
        "no --name when none was given: one is picked")


@pytest.mark.parametrize("disk", [5, 99999], ids=["too-small", "too-large"])
def test_the_disk_size_remedy_for_a_plain_build_is_unchanged(disk):
    with pytest.raises(LifecycleError) as caught:
        plan(os_choice="linux", gpu="l4", disk_gb=disk, name="mybox")
    assert caught.value.fix == "comfy-qat create --os linux --gpu l4 --disk 200"


def test_the_card_that_cannot_be_driven_remedy_for_a_reserved_build_still_reserves():
    fix = _plan_refusal(gpu="p100", name="mybox").fix
    assert _creates(fix) == ["comfy-qat create --os linux --gpu t4 --reserve --name mybox"]
    assert fix.startswith("comfy-qat create --os linux --gpu t4 --reserve --name mybox, "), (
        "the comma still ends the command, for whoever parses it")


def test_the_card_that_cannot_be_driven_remedy_for_a_plain_build_is_unchanged():
    with pytest.raises(LifecycleError) as caught:
        plan(os_choice="linux", gpu="p100", name="mybox")
    assert caught.value.fix.startswith(
        "comfy-qat create --os linux --gpu t4, the same n1-standard-8 machine")


def test_the_unknown_os_remedy_for_a_reserved_build_still_reserves():
    with pytest.raises(LifecycleError) as caught:
        plan(os_choice="plan9", gpu="t4", reserve=True, name="mybox")
    assert _creates(caught.value.fix) == [
        "comfy-qat create --os linux --gpu l4 --reserve --name mybox"]
    with pytest.raises(LifecycleError) as plain:
        plan(os_choice="plan9", gpu="t4")
    assert plain.value.fix == "comfy-qat create --os linux --gpu l4"


def test_the_no_gpu_remedy_offers_a_reserved_box_under_the_name_asked_for():
    """One of its two commands is deliberately NOT reserved — that is the
    alternative being offered. The other is, and both keep the name."""
    with pytest.raises(LifecycleError) as caught:
        plan(os_choice="linux", gpu="none", reserve=True, name="mybox")
    assert _creates(caught.value.fix) == [
        "comfy-qat create --os linux --gpu none --name mybox",
        "comfy-qat create --os linux --gpu t4 --reserve --name mybox",
    ]


class _NothingToReserve(Cloud):
    def create_reservation(self, name, zone, project, **kwargs):
        raise GcloudError("Could not fetch resource", raw=STOCKOUT)

    def reservation_absent(self, name, zone, project):
        return True


@pytest.mark.parametrize("ordering,attempts,expected", [
    (Ordering(zones=("us-central1-a",), regions=("us-central1", "us-east1"),
              offering=("us-central1", "us-east1")), 6,
     ["comfy-qat create --os linux --gpu t4 --reserve --name comfy-linux --region us-east1"]),
    (Ordering(zones=("us-central1-a", "us-east1-b"), regions=("us-central1", "us-east1")), 1,
     ["comfy-qat create --os linux --gpu t4 --reserve --name comfy-linux --region <region>",
      "comfy-qat create --os linux --gpu t4 --reserve --name comfy-linux --zone <zone>"]),
    (Ordering(zones=("us-central1-a",), regions=("us-central1",), fall_through=False), 6,
     ["comfy-qat create --os linux --gpu t4 --reserve --name comfy-linux"]),
], ids=["untried-regions", "capped", "named-zone"])
def test_every_stockout_ending_hands_back_a_reserved_create(ordering, attempts, expected):
    with pytest.raises(LifecycleError) as caught:
        build(_NothingToReserve(), T4_BLUEPRINT, ordering, PROJECT, lambda _line: None,
              attempts=attempts)
    assert _creates(caught.value.fix) == expected


@pytest.mark.parametrize("ordering,attempts,expected", [
    (Ordering(zones=("us-central1-a", "us-east1-b"), regions=("us-central1", "us-east1")), 1,
     ["comfy-qat create --region <region>", "comfy-qat create --zone <zone>"]),
    (Ordering(zones=("us-central1-a",), regions=("us-central1",), fall_through=False), 6,
     ["comfy-qat create"]),
], ids=["capped", "named-zone"])
def test_the_stockout_endings_for_a_plain_build_are_unchanged(ordering, attempts, expected):
    refuse = {"us-central1-a": STOCKOUT, "us-east1-b": STOCKOUT}
    with pytest.raises(LifecycleError) as caught:
        build(Cloud(refuse=refuse), LINUX_T4, ordering, PROJECT, lambda _line: None,
              attempts=attempts)
    assert _creates(caught.value.fix) == expected


def test_the_full_region_remedy_keeps_the_name_as_well_as_the_reservation(tmp_path):
    with pytest.raises(LifecycleError) as caught:
        ordered([], [held_for("other", zone="us-central1-b")],
                config=tmp_path / "hosts.toml", zone="us-central1-a")
    assert _creates(caught.value.fix) == [
        "comfy-qat create --os linux --gpu t4 --reserve --name comfy-linux --region asia-east1"]


def test_the_two_leftover_remedies_are_reserved_creates(tmp_path):
    leftover = held_for("comfy-linux", zone="asia-east1-b")
    check = gate(1, [], [leftover], reserve=True, leftover=leftover)
    with pytest.raises(LifecycleError) as elsewhere:
        order_zones(Cloud(), PROJECT, T4_BLUEPRINT, check, leftover=leftover,
                    zone="us-central1-a")
    assert _creates(elsewhere.value.fix) == [
        "comfy-qat create --os linux --gpu t4 --reserve --name comfy-linux --zone asia-east1-b"]

    wrong = held_for("comfy-linux", ours=False)
    with pytest.raises(LifecycleError) as misfit:
        order_zones(Cloud(), PROJECT, T4_BLUEPRINT, gate(2, [], [wrong], reserve=True),
                    leftover=wrong)
    assert _creates(misfit.value.fix) == [
        "comfy-qat create --os linux --gpu t4 --reserve --name <another-name>"]


# The sites, by the function each is written in. A string in the source that
# builds a `comfy-qat create` and is in none of these fails the test below, so
# a new remedy has to be added here — beside a test above that drives it for a
# reserved build.
CREATE_REMEDIES = {
    "undrivable": 1,              # the card that cannot be driven
    "image_for": 1,               # no such operating system
    "plan": 4,                    # no-GPU-and-reserve (two), disk too small, too large
    "_refuse_the_leftover": 1,    # a reservation of this name that does not fit
    "build": 4,                   # plain: capped (two) and named zone; untried regions
    "_same_box": 1,               # reserved: capped (two) and named zone go through this
    "_refuse_full": 1,            # the region's allowance is held
    "_where_the_leftover_is": 1,  # the leftover is somewhere else
}


def test_every_create_remedy_in_the_package_modules_this_file_covers_is_listed():
    """Counted from the syntax tree — string constants in code, so comments and
    docstrings do not count — and compared with the list above BOTH ways."""
    import ast
    import inspect

    from comfy_qa import config, discover, lifecycle, provision, quota, stamp, zones
    from comfy_qa import create as create_module

    def sites(module):
        tree = ast.parse(inspect.getsource(module))
        found: dict[str, int] = {}
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            docstring = ast.get_docstring(function, clean=False)
            for node in ast.walk(function):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and "comfy-qat create" in node.value
                        and node.value != docstring):
                    found[function.name] = (found.get(function.name, 0)
                                            + node.value.count("comfy-qat create"))
        return found

    assert sites(create_module) == CREATE_REMEDIES
    for module in (config, discover, lifecycle, provision, quota, stamp, zones):
        assert sites(module) == {}, f"{module.__name__} hands back a create command"


# --- final check: "nothing is on it" only when nothing is ---------------------------
#
# The remedy looked for the instance NAMED in the reservation's description. A
# reservation whose description says `comfy-linux` while a running box called
# `renamed-box` is bound to it was therefore told "release it — nothing is on
# it", with the command that takes the capacity from under that box. `list
# --live` and `down --all` ask whether ANY instance is bound; this now asks the
# same question of the same records.

B_DELETE = (f"gcloud compute instances delete renamed-box --zone=us-central1-a "
            f"--project={PROJECT} --delete-disks=all --quiet")


def bound_by_another_name(**kwargs):
    """`comfy-linux-rsv` says it is held for comfy-linux; `renamed-box` is on it."""
    return gate(1, [bound("renamed-box", "comfy-linux-rsv")],
                [held_for("comfy-linux", in_use=1)], **kwargs).problem()


def test_a_reservation_with_a_differently_named_box_on_it_is_not_called_empty():
    fix = bound_by_another_name(hosts=[]).fix
    lines = [line.strip() for line in fix.splitlines()]

    assert "nothing is on it" not in fix
    assert "renamed-box is on it" in lines[0]
    assert lines[1:] == [B_DELETE, RAW_RELEASE], (
        "the box that is really there, then the reservation — never the "
        "reservation alone, and never the name in the description")
    assert "instances delete comfy-linux " not in fix


def test_the_box_that_is_really_on_it_is_named_by_its_own_host_list_entry():
    fix = bound_by_another_name(hosts=[entry("my-label", instance_name="renamed-box")]).fix
    assert [line.strip() for line in fix.splitlines()] == [
        "stop it, then delete it to release the card:",
        "comfy-qat down my-label",
        "comfy-qat delete my-label",
    ]


def test_an_entry_for_the_box_in_the_description_is_not_offered_for_the_box_on_it():
    """The list holds `comfy-linux` — the name in the description — and that
    machine is NOT the one on the reservation. Deleting it frees nothing."""
    fix = bound_by_another_name(hosts=[entry("comfy-linux")]).fix
    assert "comfy-qat" not in fix
    assert B_DELETE in fix


def test_somebody_elses_reservation_with_a_box_on_it_is_not_released_from_under_it():
    theirs = held_for("x", ours=False, name="training-hold", in_use=1)
    fix = gate(1, [bound("trainer", "training-hold")], [theirs], hosts=[]).problem().fix
    lines = [line.strip() for line in fix.splitlines()]

    assert "trainer is on it" in lines[0]
    assert lines[1].startswith("gcloud compute instances delete trainer ")
    assert lines[2].startswith("gcloud compute reservations delete training-hold ")


def test_with_several_holders_each_is_judged_by_what_is_bound_to_it():
    held = [held_for("comfy-linux", in_use=1), held_for("empty", zone="asia-east1-a")]
    fix = gate(2, [bound("renamed-box", "comfy-linux-rsv")], held, hosts=[]).problem().fix
    lines = [line.strip() for line in fix.splitlines()]

    assert B_DELETE in lines
    assert lines.index(B_DELETE) < lines.index(RAW_RELEASE)
    assert "instances delete empty" not in fix


def test_a_reservation_nothing_is_bound_to_is_still_called_empty():
    """The pair: the sentence is still said when it is true."""
    fix = gate(1, [instance("unrelated")], [held_for("comfy-linux")], hosts=[]).problem().fix
    assert fix.splitlines()[0] == "release it — nothing is on it:"


def test_when_the_instances_were_not_read_nothing_is_claimed_about_what_is_on_it():
    """NOT READ IS NOT EMPTY. `None` for the instances means nobody looked."""
    from comfy_qa.create import _boxed, _how_to_release

    holders = rsv.holders([held_for("comfy-linux")])
    assert _boxed(holders, None) is None
    assert _boxed(holders, []) == ()

    fix = _how_to_release(holders, _boxed(holders, None), PROJECT)
    assert "nothing is on it" not in fix
    assert "could not be read" in fix
    assert f"gcloud compute instances list --project={PROJECT}" in fix
    assert RAW_RELEASE in fix

    several = rsv.holders([held_for("a"), held_for("b")])
    assert "could not be read" in _how_to_release(several, None, PROJECT)


def test_the_full_region_refusal_names_the_box_that_is_really_on_it(tmp_path):
    """The second caller of the same remedy."""
    held = [held_for("one", zone="us-central1-b")]
    instances = [bound("another-name", "one-rsv", zone="us-central1-b")]

    with pytest.raises(LifecycleError) as caught:
        ordered(instances, held, config=tmp_path / "hosts.toml", zone="us-central1-a")

    assert "nothing is on it" not in caught.value.fix
    assert (f"gcloud compute instances delete another-name --zone=us-central1-b "
            f"--project={PROJECT} --delete-disks=all --quiet") in caught.value.fix


# --- review: one set of words for "no GPU", not two ---------------------------------


def test_what_create_takes_as_no_gpu_is_what_the_host_list_reads_as_no_gpu(monkeypatch):
    """There were two sets, in two modules, that happened to be equal. A word
    added to the host list's set is now a word `--gpu` takes, with nothing
    else edited."""
    from comfy_qa import config, create

    assert not hasattr(create, "_NO_GPU_SPELLINGS"), "the second copy is back"
    monkeypatch.setattr(config, "NO_GPU_WORDS", config.NO_GPU_WORDS | {"headless"})

    assert create.card_for("headless") is create.NO_GPU
    assert config.declares_no_gpu("headless") is True


@pytest.mark.parametrize("word", ["none", "cpu", "None", " CPU ", "no", "nogpu", "null",
                                  "off", "0", "", "t4", "l4", "n1"])
def test_the_two_readings_of_a_word_never_disagree(word):
    """Both ways. Every word the host list reads as no-GPU makes a no-GPU box,
    and nothing else does."""
    from comfy_qa import config, create

    try:
        made = create.card_for(word)
    except LifecycleError:
        made = None
    assert (made is create.NO_GPU) is config.declares_no_gpu(word)


def test_every_no_gpu_word_the_host_list_knows_is_accepted_by_create():
    from comfy_qa import config, create

    assert config.NO_GPU_WORDS, "the set this walks is empty"
    for word in config.NO_GPU_WORDS:
        assert create.card_for(word) is create.NO_GPU


# --- review: one helper for a zone's name, and E1's public name for `outside` -------


def test_this_module_keeps_no_copy_of_helpers_the_reservation_module_exports():
    import inspect

    from comfy_qa import create

    assert not hasattr(create, "_zone_name")
    source = inspect.getsource(create)
    assert "rsv._outside" not in source and "rsv._tail" not in source
    assert "rsv.outside(" in source and "rsv.zone_of(" in source


# --- live pass: the release command is one function's, flags and all ----------------


def test_the_release_command_this_module_prints_is_the_reservation_modules_own():
    """With and without a project. There was a trim here for a trailing
    `--project=`; the function that writes the command now leaves the flag out
    itself, so a second opinion about its shape is a second thing to drift."""
    from comfy_qa import create

    assert not hasattr(create, "_release_command")
    theirs = held_for("x", ours=False, name="training-hold")
    with_project = gate(1, [], [theirs]).problem().fix
    assert rsv.delete_command("training-hold", "us-central1-a", PROJECT) in with_project
    assert with_project.strip().endswith("--quiet")

    bare = check_quota(CARDS["t4"], [T4_QUOTA, ceiling(1)], [], reservations=[theirs])
    assert rsv.delete_command("training-hold", "us-central1-a", "") in bare.problem().fix
    assert "--project" not in bare.problem().fix


# --- live pass D1: one region tried because it was named is not "every region" ------
#
# Printed on a real project, 2026-10-06, by `create --os linux --gpu t4
# --reserve --name qatest-rsv --region us-central1 --yes`:
#
#     every zone tried is out of T4 capacity: us-central1-a, us-central1-b,
#     us-central1-c, us-central1-f. That is every region this project can use
#     the card in, so there is nowhere left to try right now.
#
# The same project, unnarrowed, four minutes earlier: 23 usable regions.

US_CENTRAL1 = ("us-central1-a", "us-central1-b", "us-central1-c", "us-central1-f")


def narrowed_to_one_region(blueprint, cloud):
    from dataclasses import replace

    ordering = replace(
        Ordering(zones=US_CENTRAL1, regions=("us-central1",), offering=("us-central1",)),
        narrowed_to="us-central1")
    with pytest.raises(LifecycleError) as caught:
        build(cloud, blueprint, ordering, PROJECT, lambda _line: None)
    return caught.value


def test_a_stockout_in_the_region_that_was_named_is_not_called_everywhere():
    qatest = Blueprint(name="qatest-rsv", image=IMAGES["linux"], card=CARDS["t4"],
                       reserve=True)
    problem = narrowed_to_one_region(qatest, _NothingToReserve())
    message = str(problem)

    assert problem.kind == EXHAUSTED
    assert message.startswith(
        "every zone tried is out of T4 capacity: us-central1-a, us-central1-b, "
        "us-central1-c, us-central1-f.")
    assert "every region this project can use" not in message
    assert "nowhere left to try" not in message
    assert "us-central1, the region you named with --region" in message
    assert "Nothing was created and nothing is billing." in message
    assert _creates(problem.fix) == [
        "comfy-qat create --os linux --gpu t4 --reserve --name qatest-rsv"]
    assert "drop --region and let this pick" in problem.fix
    assert "--region us-central1" not in problem.fix


def test_the_same_stockout_for_a_plain_box_offers_the_plain_command():
    refuse = {zone: STOCKOUT for zone in US_CENTRAL1}
    problem = narrowed_to_one_region(LINUX_T4, Cloud(refuse=refuse))

    assert "us-central1, the region you named with --region" in str(problem)
    assert _creates(problem.fix) == ["comfy-qat create --os linux --gpu t4"]
    assert "--reserve" not in problem.fix


def test_a_stockout_everywhere_with_no_region_named_still_says_everywhere():
    """The pair: the sentence is true when nothing narrowed the search."""
    ordering = Ordering(zones=US_CENTRAL1, regions=("us-central1",),
                        offering=("us-central1",))
    with pytest.raises(LifecycleError) as caught:
        build(Cloud(refuse={zone: STOCKOUT for zone in US_CENTRAL1}), LINUX_T4, ordering,
              PROJECT, lambda _line: None)
    assert "That is every region this project can use the card in" in str(caught.value)
    assert "--region" not in str(caught.value)


def test_naming_a_region_is_carried_into_the_ordering(tmp_path):
    """`build` can only say "the region you named" if `order_zones` told it."""
    check = gate(4, [], [])
    named = order_zones(T4Cloud(), PROJECT, LINUX_T4, check, region="us-central1",
                        probe=lambda region: 10.0, config=tmp_path / "hosts.toml")
    free = order_zones(T4Cloud(), PROJECT, LINUX_T4, check,
                       probe=lambda region: 10.0, config=tmp_path / "hosts.toml")

    assert named.narrowed_to == "us-central1"
    assert free.narrowed_to == ""


# --- live pass D2: the ending for a reserved box is two commands, in order ----------
#
# `comfy-qat delete qatest-rsv` straight after `create --reserve` answered, on a
# real box: "qatest-rsv is running, not stopped. Stop it first", exit 2. The
# one line the ending offered was a command that is refused as printed.


def test_the_ending_for_a_reserved_box_stops_it_before_it_deletes_it():
    from comfy_qa import create

    lines = create.next_steps(T4_BLUEPRINT, "us-central1-a")
    down = "  comfy-qat down comfy-linux     # first — delete refuses a box that is running"
    delete = rsv.stop_line("comfy-linux")

    assert lines[-2:] == [down, delete]
    assert lines[-3] == rsv.bill("comfy-linux")
    assert not [line for line in lines if "stop paying" in line], (
        "`down` is a step here, never the thing that stops this box's bill")
    assert create.reserved_stop_lines("comfy-linux") == [down, delete]
