"""A box with no GPU: `create --gpu none`, and everything that assumed a card.

A no-GPU box is an `n1-standard-8` — the T4's machine with nothing attached to
it. Almost every line that makes a box was written when a box and a card were
the same thing, and each of those lines is a way a CPU box goes wrong: an
`--accelerator` it must not have, a maintenance policy only a GPU needs, an
NVIDIA startup script on a machine with no NVIDIA hardware, a GPU quota check
that refuses a box which uses no GPU quota, and a zone search that asks where a
card called "" is sold.

Nothing here reaches Google. The quota payloads are FIXTURES in the shape
`tests/test_setup_quota.py` records for this project's CPU quotas (a general
pool of 200 vCPU per region, a project-wide ceiling of 32); the argv tests go
through the `Gcloud(runner=...)` seam.
"""

from __future__ import annotations

import pytest

from comfy_qa import create
from comfy_qa.create import CARDS, IMAGES, NO_QUOTA, NO_ZONE, Blueprint
from comfy_qa.gcloud import Gcloud, GcloudError
from comfy_qa.lifecycle import LifecycleError
from comfy_qa.zones import Ordering

PROJECT = "stately-timing-504610-p1"
REGIONS = ["europe-west4", "us-central1", "us-east1"]

STOCKOUT = (
    "ERROR: (gcloud.compute.instances.create) Could not fetch resource:\n"
    " - The zone 'projects/p/zones/us-central1-a' does not have enough resources "
    "available to fulfill the request.  '(resource type:compute)'.\n"
)


def cpu_quota(value, locations=tuple(REGIONS)):
    return {"quotaId": "CPUS-per-project-region",
            "dimensionsInfos": [{"details": {"value": str(value)},
                                 "applicableLocations": list(locations)}]}


def cpu_ceiling(value):
    return {"quotaId": "CPUS-ALL-REGIONS-per-project",
            "dimensionsInfos": [{"details": {"value": str(value)},
                                 "applicableLocations": []}]}


def gpu_ceiling(value):
    return {"quotaId": "GPUS-ALL-REGIONS-per-project",
            "dimensionsInfos": [{"details": {"value": str(value)},
                                 "applicableLocations": ["global"]}]}


# What `gcloud beta quotas info list --service=compute` holds for this project,
# as far as a box with no GPU is concerned — and a GPU ceiling of ZERO beside
# it, because the whole point is that it must not matter.
LIVE = [cpu_quota(200), cpu_ceiling(32), gpu_ceiling(0)]


def no_gpu():
    """Looked up at call time, so a test names itself when the constant is gone."""
    return create.NO_GPU


def cpu_box(os_key="linux", name="comfy-cpu"):
    return Blueprint(name=name, image=IMAGES[os_key], card=no_gpu())


# --- what "no GPU" is -------------------------------------------------------


def test_no_gpu_is_the_t4_machine_with_nothing_attached():
    card = no_gpu()
    assert card.key == "none"
    assert card.machine_type == "n1-standard-8"
    assert card.machine_type == CARDS["t4"].machine_type
    assert card.accelerator == ""
    assert card.accelerator_flag is None, "nothing to attach"
    assert card.vcpus == 8


def test_no_gpu_holds_none_of_the_gpu_allowance():
    """`count` is what the ceiling check measures a request against. A CPU box
    asks for nothing, and one that asked for 1 would be refused on a project
    whose only card is already in use."""
    assert no_gpu().count == 0


def test_no_gpu_is_not_a_gpu_and_every_card_in_the_table_is():
    assert no_gpu().is_gpu is False
    for key, card in CARDS.items():
        assert card.is_gpu is True, f"{key} is a card"


def test_no_gpu_is_not_a_row_in_the_card_table():
    """`setup` and `quota list` read `CARDS` as "cards Google meters". A row
    for "none" there would be asked for at Google."""
    assert "none" not in CARDS
    assert no_gpu() not in CARDS.values()
    assert "none" not in create.drivable_cards()
    assert create.card_named("none") is None
    assert create.offered("none") is False


@pytest.mark.parametrize("typed", ["none", "None", " NONE ", "cpu", "CPU"])
def test_none_and_cpu_both_mean_no_gpu(typed):
    assert create.card_for(typed) is no_gpu()


def test_a_real_card_still_resolves_to_itself():
    """The pair: the new spelling did not swallow the old ones."""
    assert create.card_for("t4") is CARDS["t4"]
    assert create.card_for("l4") is CARDS["l4"]


def test_the_menu_of_cards_is_unchanged_and_the_choices_add_one_row():
    """`gpu_menu()` is pinned elsewhere as the drivable cards and nothing else.
    `gpu_choices()` is that list with the no-GPU row after it, and the note is
    the sentence a person choosing needs: it is slow, and it does not use the
    GPU allowance."""
    menu = create.gpu_menu()
    assert menu == [(key, CARDS[key].machine_type) for key in create.drivable_cards()]

    choices = create.gpu_choices()
    assert choices[:-1] == menu
    assert choices[-1] == (
        "none",
        "n1-standard-8 — no GPU; ComfyUI on the CPU, slow, and not counted "
        "against GPU quota",
    )


# --- the plan ----------------------------------------------------------------


@pytest.mark.parametrize("os_key", ["linux", "windows"])
def test_planning_a_box_with_no_gpu_is_not_refused(os_key):
    """`plan` refuses any card with no GSP, and "no card" has no GSP either."""
    blueprint = create.plan(os_choice=os_key, gpu="none")
    assert blueprint.card is no_gpu()
    assert blueprint.machine_type == "n1-standard-8"


def test_a_pre_turing_card_is_still_refused():
    """The pair. Exempting no-GPU must not exempt a card that cannot be driven."""
    with pytest.raises(LifecycleError) as refusal:
        create.plan(os_choice="linux", gpu="p100")
    assert refusal.value.kind == create.NO_DRIVER


def test_a_linux_box_with_no_gpu_gets_no_startup_script():
    """The startup script is the NVIDIA driver installer. On a machine with no
    NVIDIA hardware it downloads the installer on first boot, fails, and leaves
    a failed-unit warning on a box that is otherwise fine."""
    assert cpu_box("linux").metadata is None


def test_a_windows_box_with_no_gpu_still_gets_the_ssh_key():
    """The one piece of metadata that is not about the card."""
    assert cpu_box("windows").metadata == "enable-windows-ssh=TRUE"


def test_a_gpu_box_still_gets_the_driver_script():
    linux_t4 = Blueprint(name="comfy-linux", image=IMAGES["linux"], card=CARDS["t4"])
    assert linux_t4.metadata == f"startup-script={create.LINUX_DRIVER}"


@pytest.mark.parametrize("os_key", ["linux", "windows"])
def test_the_plan_for_a_box_with_no_gpu_says_so_and_promises_no_driver(os_key):
    steps = cpu_box(os_key).steps("us-central1-a")
    text = "\n".join(steps)

    assert "no GPU — ComfyUI will run on the CPU" in text
    assert "machine type n1-standard-8" in text
    assert "NVIDIA" not in text and "driver" not in text
    assert "--accelerator" not in text
    assert "none (" not in text, "there is no card to name"
    assert steps[-1] == "add comfy-cpu to the host list on the next free port"


# --- what is sent to Google ---------------------------------------------------


def recording():
    calls: list[list[str]] = []

    def runner(args, mode):
        calls.append(list(args))
        return ""

    return Gcloud(runner=runner), calls


def test_the_create_for_a_box_with_no_gpu_names_no_card_and_no_gpu_policy():
    """VALUE, not presence: the whole argv, typed out. `--maintenance-policy=
    TERMINATE` is what a GPU instance needs because it cannot live-migrate; a
    machine with no GPU can, and should."""
    gc, calls = recording()
    create.create_in(gc, cpu_box("linux"), "us-central1-a", PROJECT)

    assert calls == [[
        "compute", "instances", "create", "comfy-cpu",
        "--zone=us-central1-a", f"--project={PROJECT}",
        "--machine-type=n1-standard-8",
        "--image-family=ubuntu-2204-lts", "--image-project=ubuntu-os-cloud",
        "--boot-disk-size=200GB", "--boot-disk-type=pd-balanced",
        "--boot-disk-device-name=comfy-cpu",
    ]]


def test_the_create_for_a_windows_box_with_no_gpu_keeps_only_the_ssh_metadata():
    gc, calls = recording()
    create.create_in(gc, cpu_box("windows", "comfy-cpu-win"), "us-central1-a", PROJECT)

    (argv,) = calls
    assert "--metadata=enable-windows-ssh=TRUE" in argv
    assert not [word for word in argv if word.startswith("--accelerator")]
    assert not [word for word in argv if word.startswith("--maintenance-policy")]
    assert not [word for word in argv if "startup-script" in word]


class Recorder:
    """A cloud that writes down exactly which keywords a create was given."""

    def __init__(self):
        self.keywords: dict = {}

    def create_instance_from_image(self, name, zone, project, **keywords):
        self.keywords = keywords


def test_a_gpu_create_is_called_with_exactly_what_it_always_was():
    """Six doubles of this call live in other test files, and a keyword they
    were never told about would break every one of them. So an ordinary GPU
    create passes the six it always passed, and the two new ones appear only
    for the boxes that need them."""
    cloud = Recorder()
    create.create_in(cloud, Blueprint(name="b", image=IMAGES["linux"], card=CARDS["t4"]),
                     "us-central1-a", PROJECT)

    assert cloud.keywords == {
        "machine_type": "n1-standard-8",
        "image_family": "ubuntu-2204-lts",
        "image_project": "ubuntu-os-cloud",
        "disk_gb": 200,
        "accelerator": "type=nvidia-tesla-t4,count=1",
        "metadata": f"startup-script={create.LINUX_DRIVER}",
    }


def test_a_no_gpu_create_turns_the_gpu_maintenance_policy_off():
    cloud = Recorder()
    create.create_in(cloud, cpu_box("linux"), "us-central1-a", PROJECT)

    assert cloud.keywords["terminate_on_maintenance"] is False
    assert cloud.keywords["accelerator"] is None
    assert cloud.keywords["metadata"] is None
    assert "reservation" not in cloud.keywords


# --- the quota a box with no GPU actually spends -------------------------------


def test_cpu_quota_is_what_is_checked_and_it_passes_on_this_project():
    check = create.check_cpu(no_gpu(), LIVE)

    assert check.problem() is None
    assert check.regions == tuple(REGIONS)


def test_a_gpu_ceiling_of_zero_does_not_refuse_a_box_with_no_gpu():
    """The defect this is the repro for: routed through the GPU check, a CPU box
    on a project with no GPU allowance at all is told it cannot start."""
    assert create.check_cpu(no_gpu(), [cpu_quota(200), gpu_ceiling(0)]).problem() is None
    # And that the fixture really is one the GPU gate refuses, so the line above
    # is not passing on a payload that would have let anything through.
    assert create.check_quota(CARDS["t4"], [cpu_quota(200), gpu_ceiling(0)],
                              []).problem() is not None


def test_a_running_gpu_box_holding_the_whole_gpu_ceiling_does_not_refuse_it():
    """`check_cpu` is not handed the instances at all — there is nothing about
    another box's card that bears on a machine with none."""
    import inspect

    assert list(inspect.signature(create.check_cpu).parameters) == ["card", "quotas"]


def test_what_was_checked_is_said_and_says_it_is_a_limit_not_what_is_free():
    """The quota list carries limits and no usage, so "200" is not "200 free",
    and the output must not let it read that way."""
    lines = create.check_cpu(no_gpu(), LIVE).lines()
    text = "\n".join(lines)

    assert "CPUS (n1): 200 in 3 regions — a limit, not what is free" in text
    assert "CPUS_ALL_REGIONS (every machine, project-wide): 32" in text
    assert "8 vCPU" in text and "n1-standard-8" in text
    assert "GPU quota: not used — this box has no GPU" in text


def test_one_region_is_named_rather_than_counted():
    lines = create.check_cpu(no_gpu(), [cpu_quota(200, ["us-central1"])]).lines()
    assert "CPUS (n1): 200 in us-central1 — a limit, not what is free" in lines


def test_a_cpu_allowance_too_small_for_the_machine_is_refused():
    check = create.check_cpu(no_gpu(), [cpu_quota(4), cpu_ceiling(32)])
    problem = check.problem()

    assert problem is not None and problem.kind == NO_QUOTA
    assert check.regions == ()
    assert "n1-standard-8 needs 8 vCPU" in str(problem)
    assert "is 4" in str(problem)
    assert "Nothing was created." in str(problem)
    assert "CPUS-per-project-region" in problem.fix


def test_an_allowance_of_exactly_the_machine_is_enough():
    """The boundary. `>=`, not `>`."""
    check = create.check_cpu(no_gpu(), [cpu_quota(8), cpu_ceiling(8)])
    assert check.problem() is None
    assert check.regions == tuple(REGIONS)


def test_only_the_regions_with_room_for_the_machine_are_offered():
    """Mixed, so the fixture reaches both branches: a region whose pool is too
    small sits beside one whose pool is not."""
    quotas = [{"quotaId": "CPUS-per-project-region", "dimensionsInfos": [
        {"dimensions": {"region": "us-central1"}, "details": {"value": "200"},
         "applicableLocations": ["us-central1"]},
        {"dimensions": {"region": "us-east1"}, "details": {"value": "4"},
         "applicableLocations": ["us-east1"]},
    ]}]
    check = create.check_cpu(no_gpu(), quotas)

    assert check.regions == ("us-central1",)
    assert check.problem() is None


def test_the_project_wide_cpu_ceiling_refuses_when_it_is_smaller_than_the_machine():
    problem = create.check_cpu(no_gpu(), [cpu_quota(200), cpu_ceiling(4)]).problem()

    assert problem is not None and problem.kind == NO_QUOTA
    assert "CPUS_ALL_REGIONS is 4" in str(problem)
    assert "8" in str(problem)
    assert "Nothing was created." in str(problem)


def test_an_unlimited_cpu_allowance_never_refuses():
    """`-1` is "no explicit limit", and `-1 >= 8` is False."""
    check = create.check_cpu(no_gpu(), [cpu_quota(-1), cpu_ceiling(-1)])
    assert check.problem() is None
    assert check.regions == tuple(REGIONS)
    assert "unlimited" in "\n".join(check.lines())


def test_a_ceiling_the_project_does_not_report_does_not_refuse():
    """Not read is not zero."""
    check = create.check_cpu(no_gpu(), [cpu_quota(200)])
    assert check.problem() is None
    assert "not reported by this project" in "\n".join(check.lines())


def test_a_project_that_reports_no_cpu_quota_at_all_is_not_refused_and_says_so():
    """NOT READ IS NOT ZERO, one level up. No CPU record at all is a payload
    this did not understand, not a project with no CPUs — every project has a
    pool. Refusing would block a create Google would have allowed, with nothing
    to fix; going ahead costs nothing, because Google's own quota refusal comes
    before anything exists. What it must not do is say the quota was checked."""
    check = create.check_cpu(no_gpu(), [gpu_ceiling(1)])

    assert check.problem() is None
    assert check.regions == ()
    assert "CPUS (n1): not reported by this project" in "\n".join(check.lines())
    assert "Google decides at the create" in "\n".join(check.lines())


# --- where it can go ----------------------------------------------------------


class Zones:
    """The reads a zone search for a CPU box may make — and the ones it may not.

    `accelerator_types` and `gpu_quotas` raise. A box with no GPU has no card to
    look for and no GPU quota to read, and asking either is the defect.
    """

    def __init__(self, offered=("us-central1-a", "us-central1-b", "us-east1-b",
                                "europe-west4-a", "asia-east1-a")):
        self.offered = list(offered)
        self.asked: list[tuple] = []

    def machine_type_zones(self, project, name):
        self.asked.append(("machine_type_zones", project, name))
        return [{"name": name, "zone": f"https://compute/zones/{zone}"}
                for zone in self.offered]

    def machine_types(self, project, zone_list, name):
        self.asked.append(("machine_types", project, tuple(zone_list), name))
        return [{"name": name, "zone": zone} for zone in zone_list
                if zone in self.offered]

    def accelerator_types(self, project, name=""):
        raise AssertionError("a box with no GPU asked where a card is sold")

    def gpu_quotas(self, project):
        raise AssertionError("a box with no GPU read the GPU quota")


def order(gc, check, tmp_path, **kwargs):
    return create.order_zones(gc, PROJECT, cpu_box(), check, config=tmp_path / "hosts.toml",
                              probe=lambda region: 100.0, **kwargs)


def test_zones_for_a_box_with_no_gpu_come_from_the_machine_type_alone(tmp_path):
    gc = Zones()
    ordering = order(gc, create.check_cpu(no_gpu(), LIVE), tmp_path)

    assert ordering.zones, "it found somewhere to put it"
    assert set(ordering.zones) <= {"us-central1-a", "us-central1-b", "us-east1-b",
                                   "europe-west4-a"}
    assert "asia-east1-a" not in ordering.zones, "no CPU quota was read for asia-east1"
    assert ("machine_type_zones", PROJECT, "n1-standard-8") in gc.asked


def test_a_named_zone_is_checked_for_the_machine_type_and_nothing_else(tmp_path):
    gc = Zones()
    ordering = order(gc, create.check_cpu(no_gpu(), LIVE), tmp_path, zone="us-central1-b")

    assert ordering.zones == ("us-central1-b",)
    assert ordering.fall_through is False
    assert gc.asked == [("machine_types", PROJECT, ("us-central1-b",), "n1-standard-8")]


def test_a_named_zone_that_does_not_offer_the_machine_is_refused(tmp_path):
    with pytest.raises(LifecycleError) as refusal:
        order(Zones(offered=["us-central1-a"]), create.check_cpu(no_gpu(), LIVE),
              tmp_path, zone="us-central1-f")

    assert refusal.value.kind == NO_ZONE
    assert "us-central1-f does not offer n1-standard-8" in str(refusal.value)
    assert "none" not in str(refusal.value), "there is no card called none to talk about"
    assert "Nothing was created." in str(refusal.value)
    assert "accelerator-types" not in refusal.value.fix


def test_a_named_zone_outside_the_cpu_quota_is_refused_with_cpu_words(tmp_path):
    with pytest.raises(LifecycleError) as refusal:
        order(Zones(), create.check_cpu(no_gpu(), LIVE), tmp_path, zone="asia-east1-a")

    message = str(refusal.value)
    assert refusal.value.kind == NO_QUOTA
    assert "no CPU quota for n1-standard-8 in asia-east1" in message
    assert "europe-west4, us-central1, us-east1" in message, "where it does hold it"
    assert "none quota" not in message
    assert "quota request --gpu" not in refusal.value.fix, (
        "there is no card to request, and `--gpu none` is not a quota request")


def test_a_named_region_outside_the_cpu_quota_is_refused_with_cpu_words(tmp_path):
    with pytest.raises(LifecycleError) as refusal:
        order(Zones(), create.check_cpu(no_gpu(), LIVE), tmp_path, region="asia-east1")

    assert refusal.value.kind == NO_QUOTA
    assert "no CPU quota for n1-standard-8 in asia-east1" in str(refusal.value)
    assert "quota request --gpu" not in refusal.value.fix


def test_a_named_region_narrows_the_search_to_it(tmp_path):
    ordering = order(Zones(), create.check_cpu(no_gpu(), LIVE), tmp_path,
                     region="us-central1")
    assert ordering.zones == ("us-central1-a", "us-central1-b")


def test_with_no_cpu_quota_reported_the_search_is_everywhere_the_machine_is_sold(tmp_path):
    """The other half of "not read is not zero": with no regions to narrow by,
    the search is not empty — it is un-narrowed."""
    ordering = order(Zones(), create.check_cpu(no_gpu(), [gpu_ceiling(1)]), tmp_path)
    assert "asia-east1-a" in ordering.zones


def test_nowhere_to_put_a_box_with_no_gpu_talks_about_the_machine_not_a_card(tmp_path):
    ordering = order(Zones(offered=[]), create.check_cpu(no_gpu(), LIVE), tmp_path)
    assert not ordering

    problem = create.nowhere(cpu_box(), ordering, PROJECT)
    assert problem.kind == NO_ZONE
    assert "nowhere to put comfy-cpu" in str(problem)
    assert "n1-standard-8" in str(problem)
    assert "accelerator-types" not in problem.fix
    assert f"gcloud compute machine-types list --project={PROJECT}" in problem.fix
    assert "--filter=name=n1-standard-8" in problem.fix


# --- when the zone has none ---------------------------------------------------


class Cloud:
    def __init__(self, refuse=None):
        self.refuse = dict(refuse or {})
        self.created: list[tuple[str, str]] = []

    def create_instance_from_image(self, name, zone, project, **kwargs):
        self.created.append((name, zone))
        problem = self.refuse.get(zone)
        if problem is not None:
            raise GcloudError("Could not fetch resource", raw=problem)


def zones_to_try(*names, offering=()):
    return Ordering(zones=tuple(names), offering=tuple(offering), regions=tuple(
        dict.fromkeys(name.rsplit("-", 1)[0] for name in names)))


def test_a_stockout_for_a_box_with_no_gpu_names_the_machine_not_a_card_called_none():
    cloud = Cloud(refuse={"us-central1-a": STOCKOUT})
    lines: list[str] = []
    zone = create.build(cloud, cpu_box(), zones_to_try("us-central1-a", "us-east1-b"),
                        PROJECT, lines.append)

    assert zone == "us-east1-b"
    assert "  us-central1-a has no n1-standard-8 free right now" in lines
    assert not [line for line in lines if "none" in line]


@pytest.mark.parametrize("ordering,attempts", [
    (zones_to_try("us-central1-a"), 6),
    (zones_to_try("us-central1-a", offering=("us-central1",)), 6),
    (zones_to_try("us-central1-a", offering=("us-central1", "us-east1")), 6),
    (zones_to_try("us-central1-a", "us-east1-b"), 1),
], ids=["named-zone", "everywhere", "some-untried", "capped"])
def test_running_out_of_zones_for_a_box_with_no_gpu_names_the_machine(ordering, attempts):
    """All four endings of `build`, because each one carries the card's name in
    its own sentence and a correction to one of them is not a correction."""
    cloud = Cloud(refuse={"us-central1-a": STOCKOUT, "us-east1-b": STOCKOUT})
    with pytest.raises(LifecycleError) as refusal:
        create.build(cloud, cpu_box(), ordering, PROJECT, lambda _line: None,
                     attempts=attempts)

    message = str(refusal.value)
    assert "out of n1-standard-8 capacity" in message
    assert "none" not in message.replace("none were offered", "")
    assert "Nothing was created and nothing is billing" in message
    assert "--gpu none" in (refusal.value.fix or "") or "--gpu" not in (refusal.value.fix or "")


def test_a_stockout_for_a_real_card_still_names_the_card():
    """The pair, and the sentence is today's to the letter."""
    cloud = Cloud(refuse={"us-central1-a": STOCKOUT})
    lines: list[str] = []
    t4 = Blueprint(name="comfy-linux", image=IMAGES["linux"], card=CARDS["t4"])
    create.build(cloud, t4, zones_to_try("us-central1-a", "us-east1-b"), PROJECT,
                 lines.append)
    assert "  us-central1-a has no T4 free right now" in lines


# --- the record, and what is said afterwards -----------------------------------


def test_a_box_with_no_gpu_is_recorded_the_way_discovery_would_record_it():
    """Otherwise `discover` finds this instance and adds it a second time."""
    from comfy_qa.discover import parse, to_toml

    made = create.host_entry(cpu_box(), "us-central1-a", PROJECT)
    found = parse({
        "name": "comfy-cpu",
        "status": "RUNNING",
        "zone": ".../zones/us-central1-a",
        "disks": [{"boot": True, "licenses": [".../ubuntu-2204-lts"]}],
    }, PROJECT)

    assert made == found
    assert made.gpu == ""
    assert 'gpu          = "none"' in to_toml(made, 8191)


def test_the_entry_written_for_a_box_with_no_gpu_loads_as_one_with_no_gpu():
    import tomllib

    from comfy_qa.config import has_gpu, parse
    from comfy_qa.discover import to_toml

    text = to_toml(create.host_entry(cpu_box(), "us-central1-a", PROJECT), 8191)
    (host,) = parse(tomllib.loads(text))
    assert host.gpu == "none"
    assert has_gpu(host) is False


def test_what_is_said_after_a_linux_box_with_no_gpu_exists():
    lines = create.next_steps(cpu_box(), "us-central1-a")

    assert lines == [
        "comfy-cpu has no GPU, so there is no driver to wait for. ComfyUI will "
        "run on its CPU, which is slow.",
        "  comfy-qat go comfy-cpu     # install ComfyUI and serve it",
        "  comfy-qat down comfy-cpu   # stop the machine, stop paying",
    ]


def test_what_is_said_after_a_windows_box_with_no_gpu_exists_hands_over_no_driver():
    text = "\n".join(create.next_steps(cpu_box("windows", "comfy-cpu-win"), "us-central1-a"))

    assert "install_gpu_driver" not in text
    assert "NVIDIA" not in text
    assert "comfy-cpu-win has no GPU" in text
    assert "comfy-qat down comfy-cpu-win   # stop the machine, stop paying" in text


def test_what_is_said_after_a_gpu_box_exists_is_unchanged():
    """Byte for byte. The unreserved GPU path is not this change's to touch."""
    t4 = Blueprint(name="comfy-linux", image=IMAGES["linux"], card=CARDS["t4"])
    assert create.next_steps(t4, "us-central1-a") == [
        "comfy-linux is installing the NVIDIA driver from its startup script, "
        "which reboots it once or twice. `comfy-qat go` waits that out.",
        "  comfy-qat go comfy-linux     # install ComfyUI and serve it",
        "  comfy-qat down comfy-linux   # stop the machine, stop paying",
    ]


# --- it cannot be reserved (D2) -------------------------------------------------


def test_a_box_with_no_gpu_cannot_be_reserved():
    """The reservation limit is counted in GPU cards, so a reservation holding
    none would be bounded by nothing. Refused offline, before any read."""
    with pytest.raises(LifecycleError) as refusal:
        create.plan(os_choice="linux", gpu="none", reserve=True)

    message = str(refusal.value)
    assert "a box with no GPU cannot be reserved" in message
    assert "Nothing was created." in message
    # Both ways out, and each is a command that parses.
    assert "comfy-qat create --os linux --gpu none" in refusal.value.fix
    assert "comfy-qat create --os linux --gpu t4 --reserve" in refusal.value.fix


def test_a_box_with_no_gpu_and_no_reservation_asked_for_is_planned():
    """The pair: `reserve=False` said out loud is the ordinary case."""
    assert create.plan(os_choice="linux", gpu="none", reserve=False).reserve is False
