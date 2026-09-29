"""`host create`, driven end to end against a fake cloud.

Written the way `test_lifecycle_e2e.py` is: a scripted `Gcloud`, the real CLI
through `CliRunner`, and assertions on what a person sees rather than on what a
function returns. `test_create_cli.py` covers the happy paths of the same
command; this file exists for the ones that only show up when you hold the
command to what its own docstrings promise.

The fake refuses any call it was not told to expect. That is not tidiness: the
promise `--dry-run` makes is *no billable call*, and a fake that quietly answers
`None` to a method nobody scripted is how that promise passes a test while being
broken. `move` broke exactly this way once — its `--dry-run` was consulted after
it had already started a GPU instance to find out where there was capacity.

Nothing here opens a socket. `zones._connect` is replaced for the whole module,
because a test that times a real connection to Google measures the runner's
network and moves every zone-order assertion to wherever CI happens to be.

The quota and instance payloads are the live shapes, read off
`stately-timing-504610-p1` on 2026-08-28: `guestAccelerators` carries an
`acceleratorCount` and is present for a G2's *built-in* card as well as for an
N1's attached one, `machine-types list` returns a full zone URL where
`accelerator-types list` returns a bare zone name, and the on-demand H100 is
metered as `NVIDIA-H100-GPUS` with no `80GB` anywhere in the id.
"""

from __future__ import annotations

import json
import os
import re
import stat

import pytest
from typer.testing import CliRunner

from comfy_qa import gcloud as gcloud_module
from comfy_qa import zones as zones_module
from comfy_qa.cli import app
from comfy_qa.create import CARDS, image_for
from comfy_qa.gcloud import GcloudError

PROJECT = "stately-timing-504610-p1"
URL = f"https://www.googleapis.com/compute/v1/projects/{PROJECT}"

HOSTS = """\
[hosts.local]
kind = "local"
port = 8188
"""

# Recorded from the machine this was written on, 2026-08-28. Europe is nearer
# than Iowa is nearer than Tokyo, and no API says so — which is the whole reason
# step 3 of the zone choice is a measurement.
LATENCY = {
    "europe-west4": 208.1,
    "europe-west1": 219.4,
    "us-east1": 295.1,
    "us-central1": 310.9,
    "asia-northeast1": 432.6,
}
REGIONS = list(LATENCY)
# Two zones a region rather than three: ten candidates against a cap of six, so
# the cap is exercised and the third-nearest region still survives it.
ZONES = [f"{region}-{letter}" for region in REGIONS for letter in "ab"]

# Every gcloud call that costs money if it succeeds. A dry run may make none.
BILLABLE = {"create_instance_from_image", "start_instance", "create_firewall_rule",
            "create_snapshot", "create_instance_from_disk"}

# Google's own wording, from a real refusal on this project. `is_capacity_failure`
# and `suggested_zones` both match on it, and an invented paraphrase of either
# silently matches nothing — which reads as the tool working.
STOCKOUT = (
    "ERROR: (gcloud.compute.instances.create) Could not fetch resource:\n"
    " - The zone 'projects/p/zones/{zone}' does not have enough resources "
    "available to fulfill the request. Try a different zone, or try again later.\n"
)
SUGGESTS = (
    "ERROR: (gcloud.compute.instances.create) Could not fetch resource:\n"
    " - A g2-standard-8 VM instance is currently unavailable in the {zone} zone. "
    "Consider trying your request in the {other} zone.\n"
)
DENIED = (
    "ERROR: (gcloud.compute.instances.create) Could not fetch resource:\n"
    " - Required 'compute.instances.create' permission for "
    "'projects/p/zones/us-central1-a/instances/comfy-linux'\n"
)


def quota(quota_id, value, locations):
    """One `quotas info list` record, in the shape the live API answers with.

    `dimensions` is null and the places live in `applicableLocations`: a single
    allowance covering forty-three regions is one row, not forty-three, and code
    that reads the region out of `dimensions` finds nothing at all.
    """
    return {"quotaId": quota_id,
            "dimensionsInfos": [{"dimensions": None,
                                 "details": {"value": str(value)},
                                 "applicableLocations": list(locations)}]}


def instance(name, *, running=True, cards=1, accelerator="nvidia-l4",
             zone="us-central1-a"):
    body = {"name": name, "status": "RUNNING" if running else "TERMINATED",
            "zone": f"{URL}/zones/{zone}"}
    if cards:
        body["guestAccelerators"] = [{
            "acceleratorCount": cards,
            "acceleratorType": f"{URL}/zones/us-central1-a/acceleratorTypes/{accelerator}",
        }]
    return body


L4 = quota("NVIDIA-L4-GPUS-per-project-region", 1, REGIONS)
CEILING = quota("GPUS-ALL-REGIONS-per-project", 1, ["global"])
QUOTAS = [L4, CEILING]


def ceiling(value):
    return quota("GPUS-ALL-REGIONS-per-project", value, ["global"])


class FakeGcloud:
    """Every call `host create` makes, scripted, and a refusal for anything else.

    Deliberately not `tests/fakes.py`'s FakeGcloud: this command reads quota and
    accelerator types, which that one has never been asked for.
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
        # `create` reads these to know where a card was already refused; the
        # default is empty, which gives the plain remedy these tests assert.
        self.preferences: list[dict] = []
        self.refuse = dict(refuse or {})
        self.calls: list[str] = []
        self.created: list[tuple[str, str, dict]] = []
        self.machine_type_zones: list[tuple[str, ...]] = []

    def current_project(self):
        self.calls.append("current_project")
        return self._project

    def list_instances(self, project):
        self.calls.append("list_instances")
        return list(self._instances)

    def gpu_quotas(self, project):
        self.calls.append("gpu_quotas")
        return list(self._quotas)

    def accelerator_types(self, project, name=""):
        self.calls.append("accelerator_types")
        return [entry for entry in self._accelerators
                if not name or entry["name"] == name]

    def machine_types(self, project, zone_list, name):
        self.calls.append("machine_types")
        self.machine_type_zones.append(tuple(zone_list))
        wanted = set(zone_list)
        return [entry for entry in self._machines
                if entry["name"] == name
                and entry["zone"].rsplit("/", 1)[-1] in wanted]

    def create_instance_from_image(self, name, zone, project, **kwargs):
        self.calls.append("create_instance_from_image")
        self.created.append((name, zone, kwargs))
        problem = self.refuse.get(zone)
        if problem is not None:
            raise GcloudError("Could not fetch resource", raw=problem)

    def quota_preferences(self, project):
        """`create` reads these so its refusal can say where NOT to ask again —
        `quota list` calls a card denied while `create` said "ask and wait" about
        the same card. Empty here: these tests are about grants and zones, and an
        empty list yields the plain remedy they already assert.

        The guard below is why this is a decision rather than an accident."""
        return list(self.preferences)

    def __getattr__(self, item):  # pragma: no cover - the guard, not the path
        # PUBLIC NAMES ONLY. This guard is about `create` reaching for a gcloud
        # METHOD nobody expected, and it earned its keep catching exactly that.
        # It also caught `getattr(gc, "_accelerator_cache", None)` — an attribute
        # PROBE with a default, which `__getattr__` sees and whose default a
        # raised AssertionError defeats. Private names are bookkeeping, not API.
        if item.startswith("_"):
            raise AttributeError(item)
        raise AssertionError(f"host create asked the fake for {item!r}")


def offers(zone, name):
    return {"name": name, "zone": zone}


def has_machine(zone, name):
    return {"name": name, "zone": f"{URL}/zones/{zone}"}


def card_cloud(key, *, zones=None, **kwargs):
    """A fake that offers one card and its machine type everywhere it is asked."""
    card = CARDS[key]
    where = list(zones or ZONES)
    kwargs.setdefault("accelerators", [offers(zone, card.accelerator) for zone in where])
    kwargs.setdefault("machines", [has_machine(zone, card.machine_type) for zone in where])
    kwargs.setdefault("quotas", [grant_for(card, 8), ceiling(8)])
    return FakeGcloud(**kwargs)


def grant_for(card, value, regions=None):
    """The quota record a REAL project reports for this card, in its own shape.

    This fake used to synthesise `NVIDIA-H100-GPUS-per-project-region` for every
    card, including the H100 — **and that row does not exist on a real project.**
    Google gives the H100 no standard per-model quota at all; its on-demand
    allowance is the family entry, `GPUS-PER-GPU-FAMILY-per-project-region` with
    `gpu_family=NVIDIA_H100`. So the H100 path was green in CI and dead in
    production: every test here proved the tool could read a grant nobody has.

    That is the exact shape `docs/tests-that-cannot-fail.md` is about — a double
    that answers differently from its subject — and it is why the shape is now
    DERIVED from `Card.quota_family` rather than assumed. A card the table says is
    metered by family gets the family record; every other card keeps the per-card
    one.
    """
    where = list(regions if regions is not None else REGIONS)
    if card.quota_family:
        return {
            "quotaId": "GPUS-PER-GPU-FAMILY-per-project-region",
            "dimensionsInfos": [{
                "dimensions": {"gpu_family": card.quota_family},
                "details": {"value": str(value)},
                "applicableLocations": where,
            }],
        }
    return quota(f"NVIDIA-{card.quota_names[-1]}-GPUS-per-project-region",
                 value, where)


@pytest.fixture(autouse=True)
def never_time_a_real_connection(monkeypatch):
    monkeypatch.setattr(zones_module, "_connect",
                        lambda region, timeout=None: LATENCY.get(region, 500.0))


@pytest.fixture
def probes(monkeypatch):
    """Every region the tool actually measured, in the order it asked."""
    asked: list[str] = []

    def probe(region, timeout=None):
        asked.append(region)
        return LATENCY.get(region, 500.0)

    monkeypatch.setattr(zones_module, "_connect", probe)
    return asked


@pytest.fixture
def cli(tmp_path, monkeypatch):
    def invoke(*args, declared=HOSTS, gc=None, answer=None, config=None):
        path = config or (tmp_path / "hosts.toml")
        if declared is not None:
            path.write_text(declared, encoding="utf-8")
        cloud = gc or FakeGcloud()
        monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: cloud)
        result = CliRunner().invoke(
            app, ["create", *args, "--config", str(path)], input=answer)
        result.gc = cloud                                     # type: ignore[attr-defined]
        result.path = path                                    # type: ignore[attr-defined]
        result.hosts = (path.read_text(encoding="utf-8")      # type: ignore[attr-defined]
                        if path.exists() else "")
        return result

    return invoke


def billable(result) -> list[str]:
    return [call for call in result.gc.calls if call in BILLABLE]


def order_of(result) -> str:
    """The zone-order block, which is the part a person reads before agreeing."""
    return result.stdout[result.stdout.index("zone order"):]


# --- the quota gate -------------------------------------------------------


def test_a_card_with_no_grant_at_all_never_reaches_a_zone_lookup(cli):
    # A T4, not the V100 this used to ask for. The V100 no longer reaches the
    # quota gate at all — it is refused offline for having no GSP, which is a
    # different refusal — so asking for one here stopped testing the quota gate.
    result = cli("--os", "linux", "--gpu", "t4", "--dry-run")
    assert result.exit_code == 2
    assert "this project has no T4 quota" in result.output
    assert "Nothing was created" in result.output
    assert "accelerator_types" not in result.gc.calls
    assert billable(result) == []


def test_a_grant_smaller_than_the_cards_block_is_refused_before_anything_exists(cli):
    """An H100 is sold in eights. A grant of 1 passes a per-card check that counts
    one and then fails at the create, after the zone order has been printed."""
    result = cli("--os", "linux", "--gpu", "h100", "--dry-run",
                 gc=FakeGcloud(quotas=[grant_for(CARDS["h100"], 1), ceiling(8)]))
    assert result.exit_code == 2
    assert "H100-80GB needs 8 of this project's GPU allowance and the grant is 1" \
        in result.output
    assert billable(result) == []


def test_the_h100_grant_is_found_under_the_name_google_meters_it_by(cli):
    """The card is `nvidia-h100-80gb` and the quota is `NVIDIA-H100-GPUS`.

    Read off the live project on 2026-08-28: there is no `-80GB-` H100 quota row
    anywhere, while `PREEMPTIBLE-NVIDIA-H100-GPUS` and `COMMITTED-NVIDIA-H100-GPUS`
    both exist. Looking the grant up under the card's own display name reported
    "this project has no H100-80GB quota" on a project holding eight of them.
    """
    result = cli("--os", "linux", "--gpu", "h100", "--dry-run",
                 gc=card_cloud("h100"))
    assert result.exit_code == 0
    assert "H100-80GB: 8" in result.stdout
    assert "no H100-80GB quota" not in result.output


def test_a_refusal_only_ever_suggests_a_gpu_name_the_tool_accepts(cli):
    """`--gpu h100-80gb` is not a card this tool knows. The fix line said it was."""
    result = cli("--os", "linux", "--gpu", "h100", "--dry-run",
                 gc=FakeGcloud(quotas=[ceiling(8)]))
    assert result.exit_code == 2
    assert "--gpu h100," in result.output
    assert "--gpu h100-80gb" not in result.output


def test_the_project_wide_ceiling_refuses_whatever_the_card_grant_says(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run",
                 gc=FakeGcloud(quotas=[L4, ceiling(0)]))
    assert result.exit_code == 2
    assert "GPUS_ALL_REGIONS is 0 on this project" in result.output
    assert "ceiling across every card" in result.output


def test_the_ceiling_held_by_a_running_box_is_a_box_to_stop_not_a_quota_to_raise(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 gc=FakeGcloud(instances=[instance("comfy-win")]))
    assert result.exit_code == 2
    assert "comfy-win is already running on it" in result.output
    # Not `comfy-qat down comfy-win`: nothing declares that box, so `resolve`
    # cannot find it. The zone comes from the payload that named it.
    assert ("gcloud compute instances stop comfy-win --zone=us-central1-a"
            in result.output)
    # The distinction that matters: nothing here is a quota request.
    assert "auth quota request" not in result.output
    assert billable(result) == []


def test_a_running_box_is_counted_in_cards_not_in_boxes(cli):
    """One a3-highgpu-8g holds eight of the ceiling on its own.

    Counting boxes said one, which fitted under a ceiling of 8 and was then
    refused by Google — the refusal this whole gate exists to make first.
    """
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 gc=FakeGcloud(quotas=[L4, ceiling(8)],
                               instances=[instance("comfy-h100", cards=8,
                                                   accelerator="nvidia-h100-80gb")]))
    assert result.exit_code == 2
    assert "comfy-h100 is already running on it" in result.output
    assert billable(result) == []


def test_several_running_boxes_are_named_and_counted_in_a_sentence_that_reads(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 gc=FakeGcloud(quotas=[L4, ceiling(8)],
                               instances=[instance("comfy-a", cards=4),
                                          instance("comfy-b", cards=4)]))
    assert result.exit_code == 2
    assert "2 GPU boxes are already running on it, holding 8 of it between them" \
        in result.output
    assert "comfy-a, comfy-b" in result.output
    # The bug this replaced: "comfy-a, comfy-b is already running on it".
    assert "comfy-b is already running" not in result.output


def test_a_stopped_box_does_not_hold_the_ceiling(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 gc=FakeGcloud(instances=[instance("comfy-win", running=False)]))
    assert result.exit_code == 0
    assert billable(result) == ["create_instance_from_image"]


def test_a_running_box_with_no_card_does_not_hold_the_ceiling(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 gc=FakeGcloud(instances=[instance("build-box", cards=0)]))
    assert result.exit_code == 0
    assert billable(result) == ["create_instance_from_image"]


def test_both_allowances_are_printed_whether_or_not_the_gate_refuses(cli):
    """Somebody who is about to spend money gets to see what was checked."""
    allowed = cli("--os", "linux", "--gpu", "l4", "--dry-run")
    refused = cli("--os", "linux", "--gpu", "l4", "--dry-run",
                  gc=FakeGcloud(quotas=[L4, ceiling(0)]))
    for result in (allowed, refused):
        assert "L4: 1, in 5 regions" in result.output
        assert "GPUS_ALL_REGIONS (every card, project-wide)" in result.output


def test_what_is_holding_the_ceiling_is_printed_in_cards_and_reads_as_english(cli):
    one = cli("--os", "linux", "--gpu", "l4", "--dry-run",
              gc=FakeGcloud(quotas=[L4, ceiling(4)],
                            instances=[instance("comfy-win")]))
    assert "already running and holding 1 card of it: comfy-win" in one.stdout
    many = cli("--os", "linux", "--gpu", "l4", "--dry-run",
               gc=FakeGcloud(quotas=[L4, ceiling(16)],
                             instances=[instance("comfy-h100", cards=8),
                                        instance("comfy-win")]))
    assert "already running and holding 9 cards of it: comfy-h100, comfy-win" \
        in many.stdout


# --- choosing the zone ----------------------------------------------------


def test_only_regions_the_project_holds_quota_in_are_offered(cli):
    """europe-west4 is nearest by a hundred milliseconds and Google offers L4
    there. With no grant it must not appear: ranking on distance alone picks a
    zone where nothing can start, and the refusal reads like a permissions bug."""
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run",
                 gc=FakeGcloud(quotas=[quota("NVIDIA-L4-GPUS-per-project-region", 1,
                                             ["us-central1"]), CEILING]))
    order = order_of(result)
    assert "europe-west4" not in order
    assert "us-central1-a" in order


def test_a_zone_needs_the_card_and_the_machine_type_not_either(cli):
    """us-central1-f has a T4 and no G2. Both halves are read, not one."""
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run",
                 gc=FakeGcloud(
                     accelerators=[offers(zone, "nvidia-l4") for zone in ZONES],
                     machines=[has_machine("europe-west4-b", "g2-standard-8")]))
    order = order_of(result)
    assert "europe-west4-b" in order
    assert "europe-west4-a" not in order


def test_the_measured_latency_is_the_order_and_it_is_shown(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run")
    order = order_of(result)
    assert order.index("europe-west4-a") < order.index("europe-west1-a")
    assert order.index("europe-west1-a") < order.index("us-east1-a")
    assert "208 ms to europe-west4" in order


def test_a_region_that_does_not_answer_sorts_last_rather_than_disappearing(
        cli, monkeypatch):
    """It may be the only region the project has quota in.

    Only two zones offer the card here, so the unreachable one survives the cap
    on attempts and can be seen sorting last instead of being dropped.
    """
    monkeypatch.setattr(zones_module, "_connect",
                        lambda region, timeout=None: (
                            zones_module.UNREACHABLE if region == "europe-west4"
                            else LATENCY[region]))
    only = ["europe-west4-a", "us-central1-a"]
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run",
                 gc=FakeGcloud(
                     quotas=[quota("NVIDIA-L4-GPUS-per-project-region", 1,
                                   ["europe-west4", "us-central1"]), CEILING],
                     accelerators=[offers(zone, "nvidia-l4") for zone in only],
                     machines=[has_machine(zone, "g2-standard-8") for zone in only]))
    order = order_of(result)
    assert "did not answer" in order
    assert order.index("us-central1-a") < order.index("europe-west4-a")


def test_a_zone_google_names_in_a_stockout_jumps_the_queue(cli):
    """Google's answer is fresher than anything this measured beforehand."""
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 gc=FakeGcloud(refuse={"europe-west4-a": SUGGESTS.format(
                     zone="europe-west4-a", other="us-central1-c")}))
    assert result.exit_code == 0
    assert "Google suggests us-central1-c" in result.stderr
    assert [made[1] for made in result.gc.created] == [
        "europe-west4-a", "us-central1-c"]


def test_the_number_of_attempts_is_capped_however_many_zones_google_suggests(cli):
    """The ranked list is capped at six. The suggestions are not on that list.

    A zone that answers a stockout by naming another zone can do it every time,
    and each attempt is a real create that takes most of a minute. Without a cap
    here the fall-through has no end and the command looks hung.
    """
    # Two constraints on what a spare may be called, and each of them is a way
    # to write this fixture so that it passes without exercising the cap.
    #
    # `us-central1-c` and not `nowhere3-1-c`: `suggested_zones` only repeats
    # things shaped like a zone — a region ending in a digit, then one letter —
    # and a fixture Google would never print matches nothing.
    #
    # And inside a region on the ordering, not `spare1-a`: a suggestion outside
    # the regions that were ranked is reported and skipped rather than followed,
    # so a made-up region never reaches a create and the queue drains instead of
    # refilling. The cap is what has to stop this, not the region guard.
    spares = [f"{region}-{letter}"
              for region in REGIONS for letter in "cdefgh"]
    chain = {zone: STOCKOUT.format(zone=zone) for zone in ZONES}
    chain[ZONES[0]] = SUGGESTS.format(zone=ZONES[0], other=spares[0])
    for current, following in zip(spares, spares[1:]):
        chain[current] = SUGGESTS.format(zone=current, other=following)
    chain[spares[-1]] = STOCKOUT.format(zone=spares[-1])
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 gc=FakeGcloud(refuse=chain))
    assert result.exit_code == 1
    assert len(result.gc.created) == zones_module.MAX_ATTEMPTS
    assert "stopping after 6 zones" in result.stderr


# The near set is `zones.NEAREST_REGIONS` wide and this module measures five
# regions, so nothing about narrowing can be exercised against the module's own
# quota — five regions all fit inside it. These are five more, and the probe
# above answers 500 ms for a region it does not know, so they rank behind every
# measured one and the last two of them fall outside the near set. That is the
# only place a widening test can put a card.
#
# Sorted names, because regions that tie at 500 ms are broken by name: the near
# set takes `africa-south1`, `asia-south2` and `australia-southeast2`, and
# leaves `me-west1` and `southamerica-west1` outside it.
FAR = ["africa-south1", "asia-south2", "australia-southeast2",
       "me-west1", "southamerica-west1"]
OUTSIDE_THE_NEAR_SET = "southamerica-west1"
WIDE = [quota("NVIDIA-L4-GPUS-per-project-region", 1, REGIONS + FAR), CEILING]


def test_only_the_nearest_regions_have_their_zones_looked_up(cli):
    """Asking `machine-types list` about a hundred and thirty zones is slow for
    an answer whose first few entries are the only ones ever used."""
    everywhere = [f"{region}-a" for region in REGIONS + FAR]
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run",
                 gc=FakeGcloud(
                     quotas=WIDE,
                     accelerators=[offers(zone, "nvidia-l4") for zone in everywhere],
                     machines=[has_machine(zone, "g2-standard-8")
                               for zone in everywhere]))
    asked = set(result.gc.machine_type_zones[0])
    assert {zones_module.region_of(zone) for zone in asked} == {
        *REGIONS, "africa-south1", "asia-south2", "australia-southeast2"}
    assert f"{OUTSIDE_THE_NEAR_SET}-a" not in asked
    assert "me-west1-a" not in asked


def test_the_search_widens_when_the_nearest_regions_offer_nothing(cli):
    card = f"{OUTSIDE_THE_NEAR_SET}-a"
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run",
                 gc=FakeGcloud(
                     quotas=WIDE,
                     accelerators=[offers(card, "nvidia-l4")],
                     machines=[has_machine(card, "g2-standard-8")]))
    assert result.exit_code == 0
    assert card in order_of(result)
    assert "looked further afield" in result.stdout


def test_the_widening_note_blames_the_half_that_was_actually_missing(cli):
    """The nearest regions here offer g2-standard-8 and no L4, and the note used
    to say they offered no g2-standard-8 — of the machine type they do offer."""
    card = f"{OUTSIDE_THE_NEAR_SET}-a"
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run",
                 gc=FakeGcloud(
                     quotas=WIDE,
                     accelerators=[offers(card, "nvidia-l4")],
                     machines=[has_machine(zone, "g2-standard-8")
                               for zone in [*ZONES, card]]))
    assert "offer no nvidia-l4, so this looked further afield" in result.stdout
    assert "offer no g2-standard-8" not in result.stdout


def test_a_note_about_one_region_is_not_written_in_the_plural(tmp_path):
    """`nearest` is a parameter, so this asks `choose` directly rather than
    monkeypatching a default that was bound when the function was defined.

    `path` is passed explicitly: left out, the cache lands beside the real
    `~/.config/comfy-qa-tools/hosts.toml` and the test writes to the machine.
    """
    gc = FakeGcloud(accelerators=[offers("asia-northeast1-a", "nvidia-l4")],
                    machines=[has_machine("asia-northeast1-a", "g2-standard-8")])
    ordering = zones_module.choose(
        gc, PROJECT, accelerator="nvidia-l4", machine_type="g2-standard-8",
        regions=REGIONS, probe=lambda region: LATENCY[region], nearest=1,
        path=tmp_path / "cache.json")
    assert ordering.notes
    assert "the nearest region offers no nvidia-l4" in ordering.notes[0]
    assert "the 1 nearest regions" not in ordering.notes[0]


def test_a_project_whose_regions_all_fit_in_the_near_set_is_asked_once(cli):
    """The widening loop ran both widths even when they were the same number, and
    made the identical `machine-types list` call twice before giving up."""
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run",
                 gc=FakeGcloud(
                     quotas=[quota("NVIDIA-L4-GPUS-per-project-region", 1,
                                   ["us-central1"]), CEILING],
                     machines=[]))
    assert result.exit_code == 2
    assert result.gc.calls.count("machine_types") == 1


def test_nowhere_that_fits_is_a_refusal_not_a_failed_attempt(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--yes", gc=FakeGcloud(machines=[]))
    assert result.exit_code == 2
    assert "nowhere to put comfy-linux" in result.output
    assert billable(result) == []


# --- falling through ------------------------------------------------------


def test_the_first_zone_stocks_out_and_the_second_one_takes_it(cli):
    """And the second one is in a different REGION, which is the point.

    It used to be `europe-west4-b`, the next zone of the region that had just
    said no. A GPU stockout is very often the whole region, so that attempt
    mostly bought another minute of the same answer — and six of them in a row
    is how a create spent its entire budget on one neighbourhood and reported
    that everywhere was full.
    """
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 gc=FakeGcloud(refuse={
                     "europe-west4-a": STOCKOUT.format(zone="europe-west4-a")}))
    assert result.exit_code == 0
    assert "trying europe-west4-a…" in result.stderr, "progress goes to stderr"
    assert "europe-west4-a has no L4 free right now" in result.stderr
    assert result.gc.created[-1][1] == "europe-west1-a"
    assert 'gce_zone     = "europe-west1-a"' in result.hosts


def test_every_zone_exhausted_exits_one_says_nothing_is_billing_and_writes_nothing(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 gc=FakeGcloud(refuse={zone: STOCKOUT.format(zone=zone)
                                       for zone in ZONES}))
    assert result.exit_code == 1
    assert "every zone tried is out of L4 capacity" in result.output
    assert "nothing is billing" in result.output
    assert result.hosts == HOSTS


def test_a_refusal_that_is_not_a_stockout_stops_at_the_first_zone(cli):
    """Nine more attempts against a permissions error costs five minutes and
    tells you nothing that the first one did not."""
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 gc=FakeGcloud(refuse={zone: DENIED for zone in ZONES}))
    assert result.exit_code == 1
    assert "Google refused to create comfy-linux in europe-west4-a" in result.output
    assert len(result.gc.created) == 1
    assert result.hosts == HOSTS


# --- the dry run ----------------------------------------------------------


def test_a_dry_run_makes_no_billable_call_of_any_kind(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run")
    assert result.exit_code == 0
    assert billable(result) == []
    assert set(result.gc.calls) <= {"current_project", "list_instances", "gpu_quotas",
                                    "accelerator_types", "machine_types"}
    assert "--dry-run: nothing created" in result.stdout


def test_a_dry_run_makes_no_billable_call_when_the_gate_would_have_refused(cli):
    for gc in (FakeGcloud(quotas=[ceiling(1)]),
               FakeGcloud(quotas=[L4, ceiling(0)]),
               FakeGcloud(machines=[]),
               FakeGcloud(instances=[instance("comfy-win")])):
        result = cli("--os", "linux", "--gpu", "l4", "--dry-run", gc=gc)
        assert billable(result) == []


def test_a_dry_run_makes_no_billable_call_through_any_override(cli):
    for extra in (["--zone", "us-central1-b"], ["--region", "us-east1"],
                  ["--name", "spare-box"], ["--disk", "500"]):
        result = cli("--os", "linux", "--gpu", "l4", "--dry-run", *extra)
        assert result.exit_code == 0, result.output
        assert billable(result) == []


def test_a_dry_run_leaves_the_host_list_byte_for_byte(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run")
    assert result.hosts == HOSTS


def test_the_dry_run_predicts_the_zone_the_real_run_then_uses(cli):
    """A preview that names a different zone from the run is worse than none."""
    preview = cli("--os", "linux", "--gpu", "l4", "--dry-run")
    predicted = preview.stdout.split("1. ")[1].split()[0]
    real = cli("--os", "linux", "--gpu", "l4", "--yes")
    assert real.gc.created[0][1] == predicted


def test_a_refusal_at_the_prompt_creates_nothing(cli):
    result = cli("--os", "linux", "--gpu", "l4", answer="n\n")
    assert billable(result) == []
    assert "nothing changed" in result.stdout
    assert result.hosts == HOSTS


# --- the machine type comes from the card ---------------------------------


@pytest.mark.parametrize("key", ["l4", "a100", "a100-80gb", "h100"])
def test_a_built_in_card_is_ordered_with_no_accelerator_flag(cli, key):
    """Passing `--accelerator` alongside a G2 or A2 is refused by Google, and is
    the most common way a create by hand fails."""
    card = CARDS[key]
    result = cli("--os", "linux", "--gpu", key, "--yes", gc=card_cloud(key))
    assert result.exit_code == 0, result.output
    _name, _zone, kwargs = result.gc.created[0]
    assert kwargs["machine_type"] == card.machine_type
    assert kwargs["accelerator"] is None
    assert "--accelerator" not in result.stdout


def test_an_attached_card_is_an_n1_plus_exactly_one(cli):
    """The T4 alone, because it is the only N1 card this tool will now order.

    This used to run over `t4, p4, p100, v100, k80` and assert exit 0 for each —
    which is to say it asserted, five times, the defect below: a create that
    succeeds and hands back a box whose GPU cannot initialise. The other four
    are Pascal, Volta and Kepler, they have no GSP, and
    `test_gpu_driver.py` is where they are held now.
    """
    card = CARDS["t4"]
    result = cli("--os", "linux", "--gpu", "t4", "--yes", gc=card_cloud("t4"))
    assert result.exit_code == 0, result.output
    _name, _zone, kwargs = result.gc.created[0]
    assert kwargs["machine_type"] == "n1-standard-8"
    assert kwargs["accelerator"] == f"type={card.accelerator},count=1"


def test_the_card_name_written_to_the_host_list_is_the_one_discovery_reads_back(cli):
    """Otherwise `host discover` finds this instance and adds it a second time
    under a different port, and two entries point at one machine."""
    from comfy_qa.discover import accelerator

    for key, card in CARDS.items():
        assert accelerator({"guestAccelerators": [
            {"acceleratorType": f"{URL}/zones/z/acceleratorTypes/{card.accelerator}"}
        ]}) == card.name, key


def test_a_windows_box_carries_the_ssh_metadata_and_a_linux_one_the_driver(cli):
    windows = cli("--os", "windows", "--gpu", "l4", "--yes")
    assert windows.gc.created[0][2]["metadata"] == "enable-windows-ssh=TRUE"
    linux = cli("--os", "linux", "--gpu", "l4", "--yes")
    assert linux.gc.created[0][2]["metadata"].startswith("startup-script=#!/bin/bash")
    assert "," not in linux.gc.created[0][2]["metadata"]


# --- after the box exists -------------------------------------------------


def test_the_created_box_lands_in_the_host_list_on_the_next_free_port(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--yes")
    assert result.exit_code == 0
    assert "[hosts.comfy-linux]" in result.hosts
    assert "port         = 8190" in result.hosts
    assert 'gce_instance = "comfy-linux"' in result.hosts
    assert 'gce_zone     = "europe-west4-a"' in result.hosts
    assert f'gce_project  = "{PROJECT}"' in result.hosts


def test_a_second_box_takes_the_next_name_and_the_next_port(cli):
    declared = HOSTS + (
        '\n[hosts.comfy-linux]\nkind         = "gce"\nos           = "Ubuntu 22.04"\n'
        'gpu          = "L4"\ngce_instance = "comfy-linux"\n'
        'gce_zone     = "us-central1-a"\ngce_project  = "p"\nport         = 8190\n')
    result = cli("--os", "linux", "--gpu", "l4", "--yes", declared=declared,
                 gc=FakeGcloud(quotas=[L4, ceiling(4)]))
    assert "[hosts.comfy-linux-2]" in result.hosts
    assert "port         = 8191" in result.hosts


def test_the_finish_says_how_to_use_it_and_how_to_stop_paying(cli):
    """It is running from the moment it is created, and a message that says how
    to use a GPU box without saying how to stop it is how one bills all night."""
    result = cli("--os", "linux", "--gpu", "l4", "--yes")
    assert "comfy-qat go comfy-linux" in result.stdout
    assert "comfy-qat down comfy-linux" in result.stdout


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes to a read-only file anyway")
def test_a_box_that_exists_with_no_host_list_entry_is_told_how_to_be_stopped(cli,
                                                                            tmp_path):
    """The one failure that happens *after* money is being spent.

    The box is real, it is billing, and the tool has no record of it — so `host
    down` cannot reach it. The message has to carry a command that works without
    the host list, and the exit code has to be a failure.
    """
    path = tmp_path / "readonly" / "hosts.toml"
    path.parent.mkdir()
    path.write_text(HOSTS, encoding="utf-8")
    path.chmod(stat.S_IRUSR)

    result = cli("--os", "linux", "--gpu", "l4", "--yes", declared=None, config=path)
    try:
        assert result.exit_code == 1
        assert result.gc.created, "the box was made; the failure is only the writing"
        assert "is billing" in result.output
        assert ("gcloud compute instances stop comfy-linux "
                f"--zone=europe-west4-a --project={PROJECT}") in result.output
    finally:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)


# --- the overrides --------------------------------------------------------


def test_an_explicit_zone_is_the_only_one_tried_and_says_so(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--zone", "us-central1-b", "--dry-run")
    order = order_of(result)
    assert "us-central1-b" in order
    assert "europe-west4" not in order
    assert "no fall-through" in result.stdout


def test_an_explicit_zone_does_not_claim_a_ranking_that_never_happened(cli):
    """One zone, nothing measured. The head used to name a "nearest" region out
    of a set of one and credit three decisions that were not made."""
    result = cli("--os", "linux", "--gpu", "l4", "--zone", "us-central1-b", "--dry-run")
    assert "nothing was ranked or measured" in result.stdout
    assert "measured latency (nearest:" not in result.stdout


def test_an_explicit_zone_without_the_machine_type_is_refused(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--zone", "us-central1-b", "--yes",
                 gc=FakeGcloud(machines=[]))
    assert result.exit_code == 2
    assert "us-central1-b does not offer g2-standard-8" in result.output
    assert billable(result) == []


def test_an_explicit_zone_without_the_card_is_refused_too(cli):
    """The half that actually varies. A T4 is an `n1-standard-8`, which nearly
    every zone offers, so checking only the machine type is not a check at all —
    it passed every zone on Earth through to a create that took a minute to fail.
    """
    result = cli("--os", "linux", "--gpu", "t4", "--zone", "us-east1-b", "--yes",
                 gc=card_cloud("t4", zones=["us-central1-a"],
                               machines=[has_machine(zone, "n1-standard-8")
                                         for zone in ZONES]))
    assert result.exit_code == 2
    assert "us-east1-b has never offered nvidia-tesla-t4" in result.output
    assert billable(result) == []


def test_a_region_with_no_quota_is_refused_rather_than_widened(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--region", "me-west1", "--dry-run")
    assert result.exit_code == 2
    assert "this project has no L4 quota in me-west1" in result.output
    assert "comfy-qat quota request --gpu l4 --region me-west1" in result.output


def test_a_region_with_quota_narrows_the_choice_and_still_falls_through(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--region", "us-east1", "--yes",
                 gc=FakeGcloud(refuse={"us-east1-a": STOCKOUT.format(zone="us-east1-a")}))
    assert result.exit_code == 0
    assert [made[1] for made in result.gc.created] == ["us-east1-a", "us-east1-b"]
    assert "europe-west4" not in order_of(result)


def test_a_name_already_on_the_project_is_refused_rather_than_colliding(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--name", "comfy-linux", "--yes",
                 gc=FakeGcloud(instances=[instance("comfy-linux", running=False,
                                                   cards=0)]))
    assert result.exit_code == 2
    assert "comfy-linux is already taken" in result.output
    assert billable(result) == []


def test_a_name_already_in_the_host_list_is_refused_the_same_way(cli):
    declared = HOSTS + (
        '\n[hosts.win]\nkind         = "gce"\nos           = "Windows Server 2022"\n'
        'gpu          = "L4"\ngce_instance = "comfy-win"\n'
        'gce_zone     = "us-central1-a"\ngce_project  = "p"\nport         = 8190\n')
    result = cli("--os", "windows", "--gpu", "l4", "--name", "comfy-win", "--yes",
                 declared=declared)
    assert result.exit_code == 2
    assert "already taken" in result.output


@pytest.mark.parametrize("name", ["9lives", "box-", "-box", "x" * 64, "--", "1"])
def test_a_name_compute_engine_would_reject_is_refused_here_instead(cli, name):
    """Google's own rule, checked before the quota read rather than after it.

    Letting it through spends a minute of gcloud and answers with Google's
    sentence about a regular expression, which does not say what to type instead.
    """
    result = cli("--os", "linux", "--gpu", "l4", "--name", name, "--yes")
    assert result.exit_code == 2
    assert "is not a name Compute Engine will take" in result.output
    assert billable(result) == []


@pytest.mark.parametrize("name,cleaned", [
    ("My Box", "my-box"), ("Comfy_Win", "comfy-win"), ("box2", "box2")])
def test_a_name_that_only_needs_tidying_is_tidied_rather_than_refused(cli, name,
                                                                     cleaned):
    result = cli("--os", "linux", "--gpu", "l4", "--name", name, "--yes")
    assert result.exit_code == 0, result.output
    assert result.gc.created[0][0] == cleaned


def test_an_unknown_operating_system_names_the_two_there_are(cli):
    result = cli("--os", "freebsd", "--gpu", "l4", "--yes")
    assert result.exit_code == 2
    assert "--os linux or --os windows" in result.output
    assert billable(result) == []


def test_an_unknown_card_lists_what_there_is_without_contacting_google(cli):
    result = cli("--os", "linux", "--gpu", "rtx4090", "--yes")
    assert result.exit_code == 2
    assert "no card called 'rtx4090'" in result.output
    assert "accelerator_types" not in result.gc.calls


def test_a_disk_too_small_for_the_image_is_refused_before_anything_exists(cli):
    result = cli("--os", "windows", "--gpu", "l4", "--disk", "20", "--yes")
    assert result.exit_code == 2
    assert "a 20 GB disk is too small" in result.output
    assert "at least 50" in result.output
    assert billable(result) == []


def test_the_disk_that_was_asked_for_is_the_disk_that_is_ordered(cli):
    result = cli("--os", "linux", "--gpu", "l4", "--disk", "500", "--yes")
    assert result.gc.created[0][2]["disk_gb"] == 500


# --- the latency cache ----------------------------------------------------


def test_the_cache_is_written_beside_the_host_list(cli, tmp_path):
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run")
    store = result.path.parent / zones_module.CACHE_NAME
    assert store.exists(), "a measurement nobody keeps is measured on every create"
    body = json.loads(store.read_text(encoding="utf-8"))
    assert body["regions"]["europe-west4"] == pytest.approx(208.1)


def test_a_cached_measurement_inside_its_window_is_not_measured_again(cli, probes):
    cli("--os", "linux", "--gpu", "l4", "--dry-run")
    assert sorted(probes) == sorted(REGIONS)
    probes.clear()
    cli("--os", "linux", "--gpu", "l4", "--dry-run")
    assert probes == [], "a cached region was measured again"


def test_a_cache_older_than_its_window_is_measured_again(cli, tmp_path, probes):
    store = tmp_path / zones_module.CACHE_NAME
    store.write_text(json.dumps({"version": zones_module.CACHE_VERSION,
                                 "at": 0.0, "regions": {"europe-west4": 1.0}}),
                     encoding="utf-8")
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run")
    assert "europe-west4" in probes
    assert "208 ms to europe-west4" in order_of(result)


@pytest.mark.parametrize("body", [
    "not json at all",
    "[]",
    '{"regions": {"europe-west4": 1.0}}',
    '{"at": "yesterday", "regions": {"europe-west4": 1.0}}',
    '{"at": 1e18, "regions": "everything"}',
])
def test_a_cache_that_cannot_be_read_is_a_miss_not_a_failed_create(cli, tmp_path, body):
    (tmp_path / zones_module.CACHE_NAME).write_text(body, encoding="utf-8")
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run")
    assert result.exit_code == 0
    assert "208 ms to europe-west4" in order_of(result)


def test_a_cache_that_cannot_be_written_is_a_miss_not_a_failed_create(cli, tmp_path):
    """The only cost of not caching a measurement is measuring it again."""
    (tmp_path / zones_module.CACHE_NAME).mkdir()
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run")
    assert result.exit_code == 0
    assert "208 ms to europe-west4" in order_of(result)


def test_the_cache_never_reaches_the_create_decision_on_its_own(cli, tmp_path):
    """A stale cache may reorder the zones. It must not invent one.

    europe-west9 is nearest in this cache and the project has no quota there, so
    it must not appear however fast it looks.
    """
    (tmp_path / zones_module.CACHE_NAME).write_text(
        json.dumps({"version": zones_module.CACHE_VERSION, "at": 1e18,
                    "regions": {"europe-west9": 1.0, "us-east1": 2.0}}),
        encoding="utf-8")
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run")
    order = order_of(result)
    assert "europe-west9" not in order
    assert order.index("us-east1-a") < order.index("europe-west4-a")


# --- every command a refusal prints has to be one you can run ---------------
#
# `create`'s refusals end in a command to paste, and one of them could not be
# pasted. The untried-regions stockout built its fix as
# `--os {blueprint.image.os}` — the DISPLAY name, `Ubuntu 22.04` — where `--os`
# takes `image.key`, `linux` or `windows`. Run unquoted, Typer exits 2 on the
# stray `22.04`; run quoted, `image_for` refuses it. Its two siblings in `plan`
# had `image.key` all along, so this was one line that drifted.
#
# It is also not a corner. `zones.choose` returns at most `MAX_ATTEMPTS` zones and
# `build`'s cap is the same number, so the queue drains before the cap is reached,
# `capped` stays False, and this branch is what an ordinary shortage lands on.
#
# So the assertion is about the SHAPE of every fix line rather than about that one
# line: split it like a shell would and require flag/value pairs. That is what
# catches an unquoted value with a space in it, which is the failure this was —
# `image_for` would have accepted `Ubuntu`, the first half of the broken value,
# and a check that only validated `--os` would have passed.

MANY_REGIONS = ["europe-west4", "europe-west1", "us-east1", "us-central1",
                "asia-northeast1", "us-west1", "europe-north1", "asia-south1"]
MANY_ZONES = [f"{region}-{letter}" for region in MANY_REGIONS for letter in "ab"]


def _invocations(text: str) -> list[list[str]]:
    """Every `comfy-qat ...` command in the output, split the way a shell would.

    A command ends where the prose around it resumes, and in this tool's fix
    lines that is a `;`, a `,` or the end of the line — `comfy-qat quota request
    --gpu v100 --region us-central1, then wait for Google` is one command and
    four words of advice.
    """
    import shlex

    return [shlex.split(run.strip().rstrip("."))
            for run in re.findall(r"comfy-qat [^,;\n]*", text)]


def _flag_pairs_only(parts: list[str]) -> None:
    """Every token past the verb is a flag or the value of the one before it.

    The verb is however many leading words there are before the first flag —
    `create` is one, `quota request` is two — so this makes no assumption about
    the shape of a command, only about what follows the flags.
    """
    first_flag = next((n for n, part in enumerate(parts) if part.startswith("-")),
                      len(parts))
    rest = parts[first_flag:]
    index = 0
    while index < len(rest):
        assert rest[index].startswith("-"), (
            f"{' '.join(parts)!r} has {rest[index]!r} sitting where no flag "
            f"introduced it. An interpolated value with a space in it looks "
            f"exactly like this, and Typer exits 2 on it."
        )
        index += 2 if index + 1 < len(rest) and not rest[index + 1].startswith("-") else 1


def test_the_command_an_untried_region_stockout_prints_can_be_run(cli, monkeypatch):
    """The branch itself, with more regions offering the card than the cap reaches."""
    latency = {region: 200.0 + 10 * n for n, region in enumerate(MANY_REGIONS)}
    monkeypatch.setattr(zones_module, "_connect",
                        lambda region, timeout=None: latency.get(region, 500.0))
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 gc=card_cloud("l4", zones=MANY_ZONES,
                               quotas=[quota("NVIDIA-L4-GPUS-per-project-region", 8,
                                             MANY_REGIONS), ceiling(8)],
                               refuse={zone: STOCKOUT.format(zone=zone)
                                       for zone in MANY_ZONES}))

    assert "Not tried, and possibly free" in result.output, "not this branch"
    offered = [parts for parts in _invocations(result.output)
               if parts[1:2] == ["create"]]
    assert offered, "a stockout with somewhere left to try has to say where"
    for parts in offered:
        _flag_pairs_only(parts)
        assert "--os" in parts
        # Raises if the value is not one `--os` takes, which is the other half:
        # a single token is not enough, it has to be a token this tool accepts.
        image_for(parts[parts.index("--os") + 1])


@pytest.mark.parametrize("args,gc_for", [
    (("--os", "linux", "--gpu", "v100", "--dry-run"), lambda: FakeGcloud()),
    (("--os", "freebsd", "--gpu", "l4", "--dry-run"), lambda: FakeGcloud()),
    (("--os", "linux", "--gpu", "l4", "--disk", "10", "--dry-run"), lambda: FakeGcloud()),
    (("--os", "linux", "--gpu", "l4", "--zone", "us-central1-a",
      "--region", "us-central1", "--dry-run"), lambda: FakeGcloud()),
    (("--os", "Ubuntu 22.04", "--gpu", "l4", "--zone", "us-central1-a",
      "--region", "us-central1", "--dry-run"), lambda: FakeGcloud()),
])
def test_every_command_a_create_refusal_prints_is_shaped_like_one(cli, args, gc_for):
    """The same check across the refusals `create` reaches before it builds.

    The last case is the one that reads as paranoid and is not: `--os` is echoed
    back verbatim by the two-flags refusal, which runs BEFORE anything has judged
    it, so an unquoted value with a space in it turns a refusal about `--zone` and
    `--region` into a parse error about a third flag.
    """
    result = cli(*args, gc=gc_for())

    assert result.exit_code != 0, "this case is supposed to be a refusal"
    for parts in _invocations(result.output):
        _flag_pairs_only(parts)


# --- the host list `create` could not read ----------------------------------


BROKEN = """\
[hosts.local]
kind = "local"
port = 8188

[hosts.spare]
kind = "local"
port = 8188
"""


def test_a_host_list_that_will_not_load_stops_create_before_it_spends(cli):
    """The one command that spends money used to treat an unreadable host list as
    an empty one, and everything downstream of that was decided on nothing.

    The name check ran against no names, so a name the file already holds passed.
    The port came out of no ports, so it could collide with one in the file. The
    box was created and billed. The block was appended to a file that still would
    not load. And the run signed off with `comfy-qat go <name>` and
    `comfy-qat down <name>`, both of which call `load` and exit 2 — so the GPU was
    billing, the tool's own stop command could not reach it, and nothing anywhere
    in the run had said the host list was broken.

    `discover` has refused this since it hit it. This is the same refusal about
    the same file, and it matters more here because the alternative is a bill.
    """
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 declared=BROKEN, gc=card_cloud("l4"))

    assert result.exit_code == 2
    assert "could not read your host list" in result.output
    assert "nothing was created" in result.output
    assert billable(result) == [], "it spent money against a file it had not read"
    assert result.hosts == BROKEN, "it appended to a file it could not read"


def test_the_refusal_names_what_the_loader_objected_to(cli):
    """A refusal that does not say what is wrong sends somebody to a file with
    fifty lines in it and no idea which one."""
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 declared=BROKEN, gc=card_cloud("l4"))

    assert "port 8188" in result.output
    assert "fix the file" in result.output


def test_no_host_list_at_all_is_still_fine(cli):
    """A file that does not exist is genuinely an empty host list, and this is
    the ordinary first-run path — the refusal above must not swallow it."""
    result = cli("--os", "linux", "--gpu", "l4", "--yes",
                 declared=None, gc=card_cloud("l4"))

    assert result.exit_code == 0
    assert "is up in" in result.output
    assert "[hosts.comfy-linux]" in result.hosts


@pytest.mark.parametrize("region, expect", [
    ("", "unset"),
    ("us-central1-a", "zone"),
    ("US-CENTRAL1", "lower case"),
])
def test_create_validates_the_region_like_every_other_command(cli, region,
                                                              expect):
    """U3. `create` is the command that SPENDS, and it was the one surface with
    no region check at all.

    An empty `--region` — from `--region "$REGION"` with the variable unset —
    silently built the box somewhere nobody chose. A zone was reported as an
    unknown region, and the remedy printed for it exits 2 when pasted. `quota
    list` and `setup` refuse all three.

    THROUGH THE COMMAND. My first attempt called `region_problem` directly and
    passed — the function has been right all along; `create` never called it.
    That is the same function-versus-command mistake as the round's other
    finding, made while writing the test for it.
    """
    result = cli("--os", "linux", "--gpu", "l4", "--region", region, "--dry-run")

    assert result.exit_code == 2, result.output
    assert expect in result.output, result.output
    assert not billable(result), billable(result)
    # AND WITHOUT THE MINUTE-LONG READ where the answer is decidable from the
    # string. `create` also validates after reading quota, so the refusal alone
    # cannot tell the cheap check from its absence — a sweep said so by
    # surviving. Empty and zone-shaped need no API call at all.
    if expect in {"unset", "zone"}:
        assert "gpu_quotas" not in result.gc.calls, result.gc.calls


def test_the_zone_typo_remedy_does_not_name_an_unvetted_region(cli):
    """FOUND BY THE COMPOSE SWEEP, not by the report. The zone-typo refusal ends:

        ... or ask for the card there:
        comfy-qat quota request --gpu l4 --region us-central9

    `region_of` turns the mistyped zone into a region string, and nothing checks
    that the project meters the card there or that Google sells it there — so the
    command it hands over exits 2. The comment two lines above this one is about
    exactly that trap ("asking Google for a region that does not exist is a slow
    way to learn you mistyped") and the line still names the region.

    Dropping `--region` is the honest fix: `quota request` derives one AND vets
    it, which is more than this path can do without another API call.
    """
    result = cli("--os", "linux", "--gpu", "l4", "--zone", "us-central9-a",
                 "--dry-run")

    assert result.exit_code == 2, result.output
    assert "--region us-central9" not in result.output, result.output
    assert "comfy-qat quota request --gpu l4" in result.output, result.output


def test_a_refused_cards_remedy_names_only_stocked_regions(cli):
    """Q1 THROUGH THE COMMAND. `check_quota` takes `askable` and the unit test
    proves it is honoured — but a mutation sweep showed the CALLER could stop
    computing it and nothing failed, because `check_quota` falls back to
    metered-only. The fallback is correct for a direct caller and wrong for this
    one, and only a test that drives `create` can tell them apart.

    The card is refused in the nearest region and metered everywhere; only one of
    the remaining regions stocks it. The remedy must name that one.
    """
    stocked_in = "europe-west4"
    gc = FakeGcloud(quotas=[quota("NVIDIA-L4-GPUS-per-project-region", 0, REGIONS),
                            CEILING],
                    accelerators=[{"name": "nvidia-l4",
                                   "zone": f"{stocked_in}-a"}])
    gc.preferences = [{
        "quotaId": "NVIDIA-L4-GPUS-per-project-region",
        "quotaConfig": {"grantedValue": "0", "preferredValue": "1",
                        "stateDetail": "Request denied"},
        "dimensions": {"region": REGIONS[0]},
        "name": "projects/p/locations/global/quotaPreferences/l4-denied",
    }]
    result = cli("--os", "linux", "--gpu", "l4", "--dry-run", gc=gc)

    assert result.exit_code == 2, result.output
    assert stocked_in in result.output, result.output
    unstocked = [r for r in REGIONS[1:] if r != stocked_in]
    for region in unstocked:
        assert f"--region {region}" not in result.output, (region, result.output)


def test_a_zone_shortage_names_the_region_that_was_looked_at(cli):
    """V1. `create --region us-east5` said:

        nowhere to put comfy-linux: no zone in the regions this project has
        quota in offers nvidia-l4

    and the same command without `--region` found six zones seconds later. It
    never names the region the user typed, and states as a PROJECT-WIDE fact
    something false of eighteen stocked regions out of forty-three metered.

    `create` narrows `regions` to the one `--region` named; the note describes
    the UNNARROWED set — the derived-set class in prose rather than in data. The
    sibling no-quota branch names the region, and is the shape copied here.
    """
    only_far = [{"name": "nvidia-l4", "zone": f"{REGIONS[0]}-a"}]
    gc = FakeGcloud(accelerators=only_far)
    narrowed = REGIONS[1]
    result = cli("--os", "linux", "--gpu", "l4", "--region", narrowed,
                 "--dry-run", gc=gc)

    assert result.exit_code == 2, result.output
    assert "the regions this project has quota in" not in result.output, (
        result.output)
    # THE SENTENCE, not the substring. `narrowed in output` is satisfied by
    # " or us-east5" — a dangling conjunction from the many-regions branch
    # reached with one region — so it could not tell the phrasing apart. A sweep
    # said so by surviving.
    assert f"no zone in {narrowed} offers" in result.output, result.output

