"""`comfy-qat create`, through the real CLI.

The rule this file exists to hold: **a dry run makes no billable call.** `move`
had the same promise and broke it — its `--dry-run` was consulted long after it
had started a GPU instance to find out where there was capacity, so the one
command that promises to change nothing was the one that could quietly cost the
most. The fake below refuses any call it was not told to expect, and the dry-run
tests assert on the whole call list rather than on the absence of one verb.

Nothing here opens a socket. `zones._connect` is replaced for the whole module:
a test that times a real connection to Google measures the runner's network, and
would make every assertion about zone order depend on where CI happens to be.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from comfy_qa import gcloud as gcloud_module
from comfy_qa import zones as zones_module
from comfy_qa.cli import app
from comfy_qa.gcloud import GcloudError

PROJECT = "stately-timing-504610-p1"
URL = f"https://www.googleapis.com/compute/v1/projects/{PROJECT}"

HOSTS = """\
[hosts.local]
kind = "local"
port = 8188
"""

REGIONS = ["europe-west4", "us-central1"]
ZONES = [f"{region}-{letter}" for region in REGIONS for letter in "ab"]

# Measured from the machine this was written on, 2026-08-28. Europe is nearer.
LATENCY = {"europe-west4": 208.1, "us-central1": 310.9}

QUOTAS = [
    {"quotaId": "NVIDIA-L4-GPUS-per-project-region",
     "dimensionsInfos": [{"details": {"value": "1"}, "applicableLocations": REGIONS}]},
    {"quotaId": "GPUS-ALL-REGIONS-per-project",
     "dimensionsInfos": [{"details": {"value": "1"}, "applicableLocations": ["global"]}]},
]

# Google's own wording, recorded from a real refusal on this project.
STOCKOUT = (
    "ERROR: (gcloud.compute.instances.create) Could not fetch resource:\n"
    " - A g2-standard-8 VM instance is currently unavailable in the europe-west4-a "
    "zone. Consider trying your request in the europe-west4-b zone.\n"
)

# Every gcloud call that costs money if it succeeds. A dry run may make none.
BILLABLE = {"create_instance_from_image"}


class FakeGcloud:
    """Every call this command makes, and a refusal for anything else.

    Deliberately not `tests/fakes.py`'s FakeGcloud: this command reads quota and
    accelerator types, which that one has never been asked for, and a fake that
    quietly answers None to an unexpected call is how a "no billable call" test
    passes while making one.
    """

    def __init__(self, *, project=PROJECT, quotas=None, instances=(),
                 accelerators=None, machines=None, refuse=None):
        self._project = project
        self._quotas = QUOTAS if quotas is None else list(quotas)
        self._instances = list(instances)
        self._accelerators = ([{"name": "nvidia-l4", "zone": zone} for zone in ZONES]
                              if accelerators is None else list(accelerators))
        self._machines = ([{"name": "g2-standard-8", "zone": f"{URL}/zones/{zone}"}
                           for zone in ZONES] if machines is None else list(machines))
        self.refuse = dict(refuse or {})
        self.calls: list[str] = []

    def current_project(self):
        self.calls.append("current_project")
        return self._project

    def list_instances(self, project):
        self.calls.append("list_instances")
        return list(self._instances)

    def gpu_quotas(self, project):
        self.calls.append("gpu_quotas")
        return list(self._quotas)

    def accelerator_types(self, project, name):
        self.calls.append("accelerator_types")
        return [entry for entry in self._accelerators if entry["name"] == name]

    def machine_types(self, project, zone_list, name):
        self.calls.append("machine_types")
        wanted = set(zone_list)
        return [entry for entry in self._machines
                if entry["name"] == name
                and entry["zone"].rsplit("/", 1)[-1] in wanted]

    def create_instance_from_image(self, name, zone, project, **kwargs):
        self.calls.append("create_instance_from_image")
        self.created = (name, zone, kwargs)
        problem = self.refuse.get(zone)
        if problem is not None:
            raise GcloudError("Could not fetch resource", raw=problem)

    def __getattr__(self, item):  # pragma: no cover - the guard, not the path
        raise AssertionError(f"create asked the fake for {item!r}")


@pytest.fixture(autouse=True)
def never_time_a_real_connection(monkeypatch):
    monkeypatch.setattr(zones_module, "_connect",
                        lambda region, timeout=None: LATENCY.get(region, 500.0))


@pytest.fixture
def cli(tmp_path, monkeypatch):
    def invoke(*args, declared=HOSTS, gc=None, answer=None):
        path = tmp_path / "hosts.toml"
        path.write_text(declared, encoding="utf-8")
        cloud = gc or FakeGcloud()
        monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: cloud)
        result = CliRunner().invoke(
            app, ["host", "create", *args, "--config", str(path)], input=answer)
        result.gc = cloud                                    # type: ignore[attr-defined]
        result.hosts = path.read_text(encoding="utf-8")      # type: ignore[attr-defined]
        return result

    return invoke


def billable(result) -> list[str]:
    return [call for call in result.gc.calls if call in BILLABLE]


# --- the dry run ----------------------------------------------------------


def test_a_dry_run_creates_nothing_and_makes_no_billable_call(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run")
    assert result.exit_code == 0
    assert billable(result) == []
    assert "--dry-run: nothing created" in result.stdout


def test_a_dry_run_leaves_the_host_list_exactly_as_it_found_it(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run")
    assert result.hosts == HOSTS


def test_a_dry_run_prints_the_plan_the_quota_and_the_zone_order(cli):
    """All three, because all three are decisions somebody would otherwise make
    by hand and would otherwise only find out about after paying."""
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run")
    out = result.stdout
    assert "g2-standard-8" in out                     # the plan
    assert "L4: 1" in out and "GPUS_ALL_REGIONS" in out   # the quota it checked
    assert "zone order" in out                        # the zones it would try
    assert "europe-west4-a" in out


def test_the_dry_run_shows_the_zone_order_the_real_run_then_uses(cli):
    """A preview that predicts a different zone from the run is worse than none."""
    preview = cli("--os", "linux", "--gpu", "l4", "--dry-run")
    first = preview.stdout.split("1. ")[1].split()[0]
    real = cli("--os", "linux", "--gpu", "l4", "--yes")
    assert real.gc.created[1] == first


def test_the_nearest_region_is_first_and_the_measurement_is_shown(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run")
    order = result.stdout[result.stdout.index("zone order"):]
    assert order.index("europe-west4-a") < order.index("us-central1-a")
    assert "208 ms to europe-west4" in order


# --- the real thing --------------------------------------------------------


def test_a_created_box_is_added_to_the_host_list_on_a_free_port(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--yes")
    assert result.exit_code == 0
    assert "[hosts.comfy-linux]" in result.hosts
    assert "port         = 8190" in result.hosts
    assert 'gce_zone     = "europe-west4-a"' in result.hosts


def test_the_box_is_created_with_the_machine_type_the_card_implies(cli):
    result = cli("--os", "linux", "--gpu", "t4", "--yes",
                 gc=FakeGcloud(
                     accelerators=[{"name": "nvidia-tesla-t4", "zone": zone}
                                   for zone in ZONES],
                     machines=[{"name": "n1-standard-8", "zone": f"{URL}/zones/{zone}"}
                               for zone in ZONES],
                     quotas=[{"quotaId": "NVIDIA-T4-GPUS-per-project-region",
                              "dimensionsInfos": [{"details": {"value": "1"},
                                                   "applicableLocations": REGIONS}]},
                             QUOTAS[1]]))
    name, zone, kwargs = result.gc.created
    assert kwargs["machine_type"] == "n1-standard-8"
    assert kwargs["accelerator"] == "type=nvidia-tesla-t4,count=1"


def test_a_windows_box_carries_the_ssh_metadata_and_is_told_about_the_driver(cli):
    result = cli("--os", "windows", "--gpu", "l4", "--yes")
    assert result.gc.created[2]["metadata"] == "enable-windows-ssh=TRUE"
    assert "install_gpu_driver.ps1" in result.stdout


def test_the_finish_says_how_to_use_it_and_how_to_stop_paying(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--yes")
    assert "comfy-qat go comfy-linux" in result.stdout
    assert "comfy-qat down comfy-linux" in result.stdout


def test_without_yes_it_asks_and_a_no_creates_nothing(cli):
    result = cli("--os", "linux", "--gpu", "l4", answer="n\n")
    assert billable(result) == []
    assert "nothing changed" in result.stdout
    assert result.hosts == HOSTS


def test_a_yes_at_the_prompt_creates_it(cli):
    result = cli("--os", "linux", "--gpu", "l4", answer="y\n")
    assert billable(result) == ["create_instance_from_image"]


def test_a_second_box_gets_the_next_name_and_the_next_port(cli):
    declared = HOSTS + (
        '\n[hosts.comfy-linux]\nkind         = "gce"\nos           = "Ubuntu 22.04"\n'
        'gpu          = "L4"\ngce_instance = "comfy-linux"\n'
        'gce_zone     = "us-central1-a"\ngce_project  = "p"\nport         = 8190\n')
    result = cli("--os", "linux", "--gpu", "l4", "--yes", declared=declared,
                 gc=FakeGcloud(quotas=[QUOTAS[0],
                                       {"quotaId": "GPUS-ALL-REGIONS-per-project",
                                        "dimensionsInfos": [
                                            {"details": {"value": "4"},
                                             "applicableLocations": ["global"]}]}]))
    assert "[hosts.comfy-linux-2]" in result.hosts
    assert "port         = 8191" in result.hosts


# --- falling through -------------------------------------------------------


def test_a_stockout_moves_on_and_says_which_zone_it_is_trying(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 gc=FakeGcloud(refuse={"europe-west4-a": STOCKOUT}))
    assert result.exit_code == 0
    assert result.gc.created[1] == "europe-west4-b"
    assert "trying europe-west4-a…" in result.stderr, "progress goes to stderr"
    assert "no L4 free right now" in result.stderr
    assert 'gce_zone     = "europe-west4-b"' in result.hosts


def test_running_out_of_capacity_everywhere_exits_one_and_writes_nothing(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 gc=FakeGcloud(refuse={zone: STOCKOUT for zone in ZONES}))
    assert result.exit_code == 1
    assert "nothing is billing" in result.output
    assert result.hosts == HOSTS


# --- refusals, which cost nothing -----------------------------------------


def test_no_quota_for_the_card_refuses_before_any_zone_is_looked_up(cli):
    result = cli("--os", "linux", "--gpu", "a100", "--dry-run")
    assert result.exit_code == 2
    assert "no A100 quota" in result.output
    assert "accelerator_types" not in result.gc.calls


def test_a_gpu_box_already_running_on_the_ceiling_refuses_with_the_box_to_stop(cli):
    running = [{"name": "comfy-win", "status": "RUNNING",
                "guestAccelerators": [{"acceleratorType": ".../nvidia-l4"}]}]
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 gc=FakeGcloud(instances=running))
    assert result.exit_code == 2
    assert "comfy-qat down comfy-win" in result.output
    assert billable(result) == []


def test_an_unknown_card_refuses_without_contacting_google_about_zones(cli):
    result = cli("--os", "linux", "--gpu", "rtx4090")
    assert result.exit_code == 2
    assert "no card called 'rtx4090'" in result.output


def test_nowhere_that_fits_is_a_refusal_not_a_failed_attempt(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 gc=FakeGcloud(machines=[]))
    assert result.exit_code == 2
    assert "nowhere to put comfy-linux" in result.output
    assert billable(result) == []


def test_an_explicit_zone_is_the_only_one_tried(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--zone", "us-central1-b", "--dry-run")
    order = result.stdout[result.stdout.index("zone order"):]
    assert "us-central1-b" in order
    assert "europe-west4" not in order
    assert "no fall-through" in result.stdout


def test_a_region_with_no_quota_is_refused_rather_than_widened(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--region", "me-west1", "--dry-run")
    assert result.exit_code == 2
    assert "no L4 quota in me-west1" in result.output


def test_a_region_with_quota_narrows_the_choice(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--region", "us-central1", "--dry-run")
    order = result.stdout[result.stdout.index("zone order"):]
    assert "us-central1-a" in order
    assert "europe-west4" not in order


def test_a_name_already_on_the_project_is_refused_rather_than_colliding(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--name", "comfy-linux",
                 gc=FakeGcloud(instances=[{"name": "comfy-linux", "status": "TERMINATED"}]))
    assert result.exit_code == 2
    assert "already taken" in result.output


def test_no_project_set_sends_you_to_setup(cli):
    result = cli("--os", "linux", "--gpu", "l4",
                 gc=FakeGcloud(project=None))
    assert result.exit_code == 2
    assert "comfy-qat setup" in result.output


def test_a_gcloud_refusal_while_reading_is_a_refusal_not_a_failed_create(cli):
    class Refusing(FakeGcloud):
        def gpu_quotas(self, project):
            raise GcloudError("no active gcloud account", fix="gcloud auth login")

    result = cli("--os", "linux", "--gpu", "l4", gc=Refusing())
    assert result.exit_code == 2
    assert "gcloud auth login" in result.output
    assert billable(result) == []


# --- the money boundary, end to end ---------------------------------------
#
# The unit-level versions of these live in tests/test_create_hostile.py. They are
# repeated here through the real CLI because the thing being asserted is a
# property of the whole command — that no combination of flags reaches
# `create_instance_from_image` — and a unit test of `build` cannot see the flags.


def test_an_explicit_zone_that_stocks_out_does_not_move_to_the_zone_google_names(cli):
    """The most expensive bug this command has had. `--zone` means this zone or
    nothing; the refusal for that zone names another one, and the box used to be
    created there — billing, and in the one place the caller ruled out."""
    result = cli("--os", "linux", "--gpu", "l4", "--zone", "europe-west4-a", "--yes",
                 gc=FakeGcloud(refuse={"europe-west4-a": STOCKOUT}))
    assert result.exit_code == 1
    assert result.gc.created[1] == "europe-west4-a"
    assert len([call for call in result.gc.calls if call in BILLABLE]) == 1
    assert result.hosts == HOSTS


def test_a_dry_run_makes_no_billable_call_even_with_yes_alongside_it(cli):
    """`--yes` answers a prompt a dry run never reaches. Together they must still
    be a dry run, because the person who typed both meant the safer one."""
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run", "--yes")
    assert result.exit_code == 0
    assert billable(result) == []
    assert result.hosts == HOSTS


def test_a_dry_run_makes_no_billable_call_when_every_zone_would_refuse(cli):
    """A dry run must not discover capacity by trying for it — the mistake `move`
    made, where `--dry-run` was consulted after a GPU instance had been started."""
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run",
                 gc=FakeGcloud(refuse={zone: STOCKOUT for zone in ZONES}))
    assert billable(result) == []


def test_a_disk_size_nobody_meant_to_type_is_refused_before_any_create(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--disk", "20000", "--yes")
    assert result.exit_code == 2
    assert "larger than anything this tool creates" in result.output
    assert billable(result) == []


def test_a_name_google_would_refuse_never_reaches_a_create(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--name", "9lives", "--yes")
    assert result.exit_code == 2
    assert "is not a name Compute Engine will take" in result.output
    assert billable(result) == []


def test_an_explicit_zone_in_a_region_with_no_quota_is_refused(cli):
    """`--region me-west1` was already refused with the reason. `--zone me-west1-a`
    meant the same thing and used to be found out by gcloud instead."""
    result = cli("--os", "linux", "--gpu", "l4", "--zone", "me-west1-a", "--yes")
    assert result.exit_code == 2
    assert "no L4 quota in me-west1" in result.output
    assert billable(result) == []

# --- the one message printed after money is being spent -------------------


def test_a_box_that_cannot_be_recorded_leads_with_the_command_that_stops_it(
        tmp_path, monkeypatch):
    """The host list could not be written, so the box exists and nothing knows.

    `comfy-qat host down` reads the host list, so at this moment it cannot reach
    the machine — the raw `gcloud ... stop` is the only thing that works, and it
    is the only thing that is urgent. It has to come before the adoption path and
    before the interpolated OSError, which can be long enough on its own to push
    a command at the end of a paragraph off the visible part of a terminal.
    """
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory", encoding="utf-8")
    cloud = FakeGcloud()
    monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: cloud)

    result = CliRunner().invoke(app, [
        "host", "create", "--os", "linux", "--gpu", "l4", "--yes",
        "--config", str(blocked / "hosts.toml")])

    assert result.exit_code == 1, "the work started and failed — not a refusal"
    assert cloud.created[0] == "comfy-linux"

    output = result.output
    stop = output.index("gcloud compute instances stop comfy-linux")
    assert stop < output.index("comfy-qat discover"), (
        "the adoption path is printed before the command that stops the billing")
    assert stop < output.index("could not be added to"), (
        "the write error is printed before the command that stops the billing")
    assert "--zone=europe-west4-a" in output
    assert f"--project={PROJECT}" in output


def test_the_stop_command_is_on_a_line_of_its_own(tmp_path, monkeypatch):
    """Buried mid-paragraph it is not copy-pastable, which is the whole point."""
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: FakeGcloud())

    result = CliRunner().invoke(app, [
        "host", "create", "--os", "linux", "--gpu", "l4", "--yes",
        "--config", str(blocked / "hosts.toml")])

    line = next(line for line in result.output.splitlines()
                if "gcloud compute instances stop" in line)
    assert line.strip().startswith("gcloud compute instances stop")


def test_zone_and_region_together_are_refused_not_silently_resolved(cli):
    """The two contradict each other by their own help text, so passing both is a
    mistake rather than an intention. `--zone` silently won, so `--region
    europe-west2 --zone us-central1-a` made a box in Iowa and never mentioned the
    region it threw away — 21 ms from this desk against 115 ms, billed either way.

    The tool already takes this position for --os/--gpu, where `_selector` says
    picking a winner "would carry that mistake out on a machine".
    """
    result = cli("--os", "linux", "--gpu", "l4",
                 "--zone", "us-central1-a", "--region", "europe-west2")

    assert result.exit_code == 2, result.output
    assert "cannot both be right" in result.output
    assert "us-central1-a" in result.output and "europe-west2" in result.output


def test_it_refuses_before_it_reads_anything_from_google(cli):
    """A string check that costs a minute of quota reads first is a string check
    nobody waits for."""
    result = cli("--os", "linux", "--gpu", "l4",
                 "--zone", "us-central1-a", "--region", "europe-west2")

    assert billable(result) == []
    assert "quota" not in result.output.lower(), result.output
