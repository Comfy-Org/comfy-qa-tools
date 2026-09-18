"""`setup` asks Google for the GPU quota this project is missing.

The requirement, in the user's words: *"if a user installs it, all GPUs are
ready, and are usable by the tool, or pending being accepted, and the request
should already be submitted"*. So the default is to submit, and this file is
almost entirely about the other half of that — that a submission which **cannot
be withdrawn and is read by a person at Google** never happens by accident, and
never happens twice.

Every fixture below is a shape read off a live project on 2026-09-17 rather than
invented, because the last time this module's fixtures were invented the quota
ids were spelled with underscores and every exclusion in `quota.py` silently
failed against them. The five real quota preferences on that project are quoted
in `quota.py`; the three DENIED ones are what `PREFS_DENIED` is built from.

**The standard these were written to** (`docs/tests-that-cannot-fail.md`): each
one was run with the behaviour it asserts inverted, and each failed by name. The
inversion is named in the docstring wherever it is not obvious, because a test
whose defeat is not written down is one nobody can re-check.
"""

from __future__ import annotations

import json
import re

import pytest

from comfy_qa.create import CARDS, drivable_cards, undrivable_cards
from comfy_qa.quota import GLOBAL_ALLOWANCE
from comfy_qa.gcloud import Gcloud, GcloudError, quota_preference_id
from comfy_qa.setup import (
    BLOCKED, CEILING_REQUEST, DEFAULT_JUSTIFICATION, DENIED, GRANTED, PENDING,
    REQUEST, UNAVAILABLE, Prompts, SetupStopped, ensure_quota_requests,
    plan_quota, request_region, run_setup,
)

# --- the shapes, all from a live project -------------------------------------

# 43 is what the live API reports for a per-card or per-family default row.
REGIONS_43 = ["us-central1", "europe-west4", "asia-east1", "africa-south1"]



def quota(quota_id, value, locations=("us-central1",)):
    return {
        "quotaId": quota_id,
        "dimensionsInfos": [{
            "details": {} if value is None else {"value": str(value)},
            "applicableLocations": list(locations),
        }],
    }


def family(value, gpu_family, locations=REGIONS_43, region=None):
    """The OTHER quota shape: one id, the card carried as a dimension.

    Every modern card comes this way — H100, H100_MEGA, H200, B200 and
    RTX_PRO_6000 share `GPUS-PER-GPU-FAMILY-per-project-region` on the live
    project — and `details: {}` is how this API reports zero.
    """
    dims = {"gpu_family": gpu_family}
    if region:
        dims["region"] = region
    return {
        "quotaId": FAMILY,
        "dimensionsInfos": [{
            "dimensions": dims,
            "details": {} if value is None else {"value": str(value)},
            "applicableLocations": [region] if region else list(locations),
        }],
    }


def preference(quota_id, *, granted, preferred, state_detail=None, dimensions=None,
               reconciling=None, name=None):
    """One `quotas preferences list` row, in the shape gcloud really returns.

    `name` matters: it is the preference's own resource id, and the ONLY id that
    can update it. `reconciling` is the authoritative pending flag — the schema
    calls it "Output only. Is the quota preference pending Google Cloud approval
    and fulfillment" — and gcloud's `--reconciling-only` is the server filter
    `reconciling:true`.
    """
    config = {"grantedValue": str(granted), "preferredValue": str(preferred)}
    if state_detail is not None:
        config["stateDetail"] = state_detail
    row = {"quotaId": quota_id, "quotaConfig": config,
           "service": "compute.googleapis.com",
           "name": f"projects/p/locations/global/quotaPreferences/{name or 'anon'}"}
    if dimensions:
        row["dimensions"] = dict(dimensions)
    if reconciling is not None:
        row["reconciling"] = reconciling
    return row


FAMILY = "GPUS-PER-GPU-FAMILY-per-project-region"
L4 = "NVIDIA-L4-GPUS-per-project-region"
T4 = "NVIDIA-T4-GPUS-per-project-region"
K80 = "NVIDIA-K80-GPUS-per-project-region"
A100 = "NVIDIA-A100-GPUS-per-project-region"
A100_80 = "NVIDIA-A100-80GB-GPUS-per-project-region"
P100 = "NVIDIA-P100-GPUS-per-project-region"
# Real on SOME projects and not on this one — the per-card H100 shape, used only
# to prove the tool reads the shape off the project rather than assuming one.
H100_PER_CARD = "NVIDIA-H100-GPUS-per-project-region"
CEILING = "GPUS-ALL-REGIONS-per-project"

# The live project, as it stands on 2026-09-17: L4 and T4 at 1, A100 never asked
# for, A100-80GB refused, ceiling at 1, and the five modern families at zero under
# the ONE family id. There is no `NVIDIA-H100-...` row anywhere — H100 is
# reachable only through the family form, which is why this fixture has none.
THIS_PROJECT = [
    quota(L4, 1), quota(T4, 1), quota(A100, 0), quota(A100_80, 0),
    quota(CEILING, 1, locations=["global"]),
    family(None, "NVIDIA_H100"),
    family(None, "NVIDIA_H100_MEGA"),
    family(None, "NVIDIA_H200"),
    family(None, "NVIDIA_B200"),
    family(None, "NVIDIA_RTX_PRO_6000"),
]

# The three denials really on that project carry `preferredValue: 1` and
# `stateDetail: "Quota request denied"`, which is the pair that used to read as
# "pending — waiting on Google" about an answer that arrived on 2026-08-05.
DENIED_DETAIL = "Quota request denied"

PREFS_NONE: list[dict] = []
PREFS_DENIED = [preference(A100_80, granted=0, preferred=1,
                           state_detail=DENIED_DETAIL,
                           dimensions={"region": "europe-west4"},
                           name="a100-80-euw4")]
PREFS_PENDING = [preference(A100, granted=0, preferred=1, reconciling=True,
                            name="a100-pending")]

# The ceiling on the live project: asked for at 1, APPROVED at 1. Satisfied, and
# still the single limit that makes two boxes at once impossible.
PREFS_CEILING_AT_ONE = [preference(CEILING, granted=1, preferred=1,
                                   state_detail="Quota request approved to 1",
                                   name="gpus-all-regions-1")]


# --- a fake project, which must never reach a network -------------------------


class Cloud:
    """A scripted project that records every `quotas preferences create`.

    `submitted` is the assertion surface for most of this file. A test that
    expects nothing to be sent asserts on the list rather than on a message,
    because a message is what the tool CLAIMS and the list is what it DID —
    `docs/tests-that-cannot-fail.md`, phase M.
    """

    def __init__(self, *, quotas=THIS_PROJECT, preferences=PREFS_NONE,
                 prefs_error=None, create_error=None, instances=(),
                 offered_in=None, accelerators=None, accel_error=None):
        self.quotas = list(quotas)
        self.preferences = list(preferences)
        self.prefs_error = prefs_error
        self.create_error = create_error
        self.instances = list(instances)
        # Which zones actually offer each accelerator. None means "do not ask" —
        # the fake raises on an unexpected call, so a test that does not set this
        # proves the command made no accelerator lookup.
        self.offered_in = offered_in
        # The full catalogue, as `accelerator-types list` returns it unfiltered.
        # `offered_in` is the older per-name shorthand and is kept because most
        # tests only care whether one card is stocked.
        self.accelerators = accelerators
        # `accelerator-types list` failing is a real case and a load-bearing one:
        # a lookup that could not be made must read as "not checked", never as
        # "the card is not there". Without this the fake could only ever answer,
        # so no test could reach the branch that tells the two apart.
        self.accel_error = accel_error
        self.submitted: list[list[str]] = []
        # Counted so a test can assert a check happened BEFORE the minute-long
        # quota read, not merely that it happened.
        self.quota_reads = 0

    def gcloud(self) -> Gcloud:
        return Gcloud(runner=self._run)

    def _run(self, args, mode):
        key = " ".join(args)
        if key.startswith("info --format=value(basic.python_location)"):
            return "/no/such/python"
        if key.startswith("auth list"):
            return [{"account": "ali@comfy.org", "status": "ACTIVE"}]
        if key.startswith("projects list"):
            return [{"projectId": "proj-1"}]
        if key.startswith("config get-value project"):
            return "proj-1"
        if key.startswith("billing projects describe"):
            return {"billingEnabled": True}
        if key.startswith("quotas info list"):
            self.quota_reads += 1
            return list(self.quotas)
        if key.startswith("quotas preferences list"):
            if self.prefs_error:
                raise self.prefs_error
            return list(self.preferences)
        if key.startswith("quotas preferences update"):
            self.submitted.append(list(args))
            if self.create_error:
                raise self.create_error
            return {}
        if key.startswith("compute accelerator-types list"):
            if self.accel_error:
                raise self.accel_error
            name = next((a.split("=")[-1] for a in args
                         if a.startswith("--filter=name=")), "")
            if self.accelerators is not None:
                return [e for e in self.accelerators
                        if not name or e["name"] == name]
            if not name:
                # Unfiltered: every id `offered_in` mentions, in its zones.
                return [{"name": n, "zone": f"https://x/zones/{z}"}
                        for n, zs in (self.offered_in or {}).items() for z in zs]
            zones = (self.offered_in or {}).get(name, [])
            return [{"name": name, "zone": f"https://x/zones/{z}"} for z in zones]
        if key.startswith("compute instances list"):
            return list(self.instances)
        raise AssertionError(f"unexpected gcloud call: {key}")


def prompts(confirm=True):
    said: list[str] = []
    asked: list[str] = []

    def _confirm(question):
        asked.append(question)
        # What had been SAID by the moment the question was put. Several tests
        # below turn on this: "print the plan before asking" is a claim about
        # order, and order is only checkable from inside the prompt.
        _confirm.said_when_asked = list(said)  # type: ignore[attr-defined]
        return confirm

    _confirm.said_when_asked = []  # type: ignore[attr-defined]
    p = Prompts(confirm=_confirm, ask=lambda q: "us-central1",
                choose=lambda q, options: options[0], say=said.append)
    p.said = said           # type: ignore[attr-defined]
    p.asked = asked         # type: ignore[attr-defined]
    p.confirmed = _confirm  # type: ignore[attr-defined]
    return p


def creates(cloud) -> list[str]:
    """The quota id of every request actually sent."""
    out = []
    for args in cloud.submitted:
        out += [a.split("=", 1)[1] for a in args if a.startswith("--quota-id=")]
    return out


def flag(args: list[str], name: str) -> str | None:
    for arg in args:
        if arg.startswith(f"--{name}="):
            return arg.split("=", 1)[1]
    return None


def parse_dimensions(value: str | None) -> dict[str, str]:
    if not value:
        return {}
    return dict(pair.split("=", 1) for pair in value.split(","))


def request_for(cloud, quota_id: str) -> list[str]:
    for args in cloud.submitted:
        if f"--quota-id={quota_id}" in args:
            return args
    raise AssertionError(f"{quota_id} was never requested: {cloud.submitted}")


def run(cloud, **kwargs):
    p = kwargs.pop("prompts", None) or prompts()
    path = kwargs.pop("config_path")
    run_setup(cloud.gcloud(), p, config_path=path, **kwargs)
    return p


# --- nothing is sent by accident ---------------------------------------------


def test_opting_out_sends_nothing_and_reads_nothing(tmp_path):
    """`--no-quota-request`.

    Asserted on the CALLS, not on a message. The inversion — deleting the
    `if not submit` guard — leaves the reassuring sentence in place and submits
    anyway, which is exactly the shape a message-only assertion cannot see.
    """
    cloud = Cloud()
    p = run(cloud, config_path=tmp_path / "hosts.toml", quota_requests=False)

    assert cloud.submitted == []
    assert any("--no-quota-request" in line for line in p.said)
    assert p.asked == [], "opting out must not then ask about it"


def test_declining_the_confirmation_sends_nothing(tmp_path):
    cloud = Cloud()
    p = run(cloud, config_path=tmp_path / "hosts.toml",
            prompts=prompts(confirm=False))

    assert cloud.submitted == []
    assert p.asked, "a terminal run must actually ask"
    assert any("nothing was sent" in line for line in p.said)


def test_declining_still_finishes_setup(tmp_path):
    config = tmp_path / "hosts.toml"
    run(Cloud(), config_path=config, prompts=prompts(confirm=False))
    assert config.exists(), "a declined quota request is not a failed setup"


def test_the_exact_list_is_printed_before_the_question_is_put(tmp_path):
    """"Say what will be asked for BEFORE asking" is a claim about ORDER.

    A test that checks the plan appears in the output somewhere passes just as
    well when the plan is printed AFTER the confirmation — which is the one
    arrangement the requirement rules out. So the prompt records what had been
    said at the moment it was called, and the assertion is made against that
    snapshot rather than against the final transcript.
    """
    cloud = Cloud()
    p = run(cloud, config_path=tmp_path / "hosts.toml",
            prompts=prompts(confirm=False))

    before = "\n".join(p.confirmed.said_when_asked)
    assert "A100" in before, "the card was not named before the question"
    assert "will ask Google for 1" in before, "the value was not named"
    assert DEFAULT_JUSTIFICATION in before, (
        "the justification goes to a human at Google and was not shown first")
    assert "cannot be deleted" in before
    assert "Asking is not being granted" in before, (
        "three of the five standing requests on the live project were refused; "
        "the plan must not read as a promise")


def test_a_terminal_run_asks_exactly_once(tmp_path):
    """Two prompts for one decision is how people learn to hold down return."""
    p = run(Cloud(), config_path=tmp_path / "hosts.toml")
    assert len(p.asked) == 1, p.asked


def test_non_interactive_submits_without_a_prompt(tmp_path):
    """The requirement's own case: no terminal, requests still in."""
    cloud = Cloud()
    p = run(cloud, config_path=tmp_path / "hosts.toml", interactive=False)

    assert A100 in creates(cloud)
    assert p.asked == [], "--non-interactive must never prompt"


# --- and nothing is sent twice ------------------------------------------------


def test_a_pending_card_is_not_asked_for_again(tmp_path):
    cloud = Cloud(preferences=PREFS_PENDING)
    p = run(cloud, config_path=tmp_path / "hosts.toml")

    assert A100 not in creates(cloud), "a request already with Google was re-filed"
    assert any("Google has not answered yet" in line for line in p.said)


def test_a_denied_card_is_not_asked_for_again_and_is_not_called_pending(tmp_path):
    """The live defect this step had to fix before it could be idempotent.

    Three of the five preferences on the real project are DENIED, and all three
    carry `preferredValue: 1`. The old `_pending_ids` asked only "did somebody
    ask for more than nothing", so a refusal Google issued on 2026-08-05 read as
    "waiting on Google" — and a step that skips what is pending would then skip
    everything forever, while telling the user to wait for an answer that came.
    """
    cloud = Cloud(preferences=PREFS_DENIED)
    p = run(cloud, config_path=tmp_path / "hosts.toml")

    assert A100_80 not in creates(cloud)
    line = next(line for line in p.said if "A100-80GB" in line)
    assert "refused" in line, line
    assert "not answered yet" not in line, "a refusal reported as still pending"


def test_a_granted_card_is_not_asked_for_again(tmp_path):
    cloud = Cloud()
    run(cloud, config_path=tmp_path / "hosts.toml")
    assert L4 not in creates(cloud)
    assert T4 not in creates(cloud)


def test_running_setup_twice_files_nothing_the_second_time(tmp_path):
    """Idempotence end to end, through the same state Google would report back.

    The second run is given the preference the first run created, which is what
    `quotas preferences list` returns once a request is in. Nothing else about
    the project changes — the grant is still 0, because approval takes days.
    """
    config = tmp_path / "hosts.toml"
    first = Cloud()
    run(first, config_path=config)
    filed = creates(first)
    assert filed, "the first run must actually file something"

    # Echo back what Google would then hold: one preference per request, under
    # the id it was filed as and with the dimensions it carried. Building these
    # from the quota id alone would give the family preference no `gpu_family`,
    # which is not a shape Google can return — and a fixture that cannot occur
    # proves nothing.
    second = Cloud(preferences=[
        preference(flag(args, "quota-id"), granted=0, preferred=1,
                   reconciling=True, name=args[3],
                   dimensions=parse_dimensions(flag(args, "dimensions")))
        for args in first.submitted
    ])
    p = run(second, config_path=config)

    assert second.submitted == [], (
        f"the second run re-filed {creates(second)}")
    assert any("nothing to request" in line for line in p.said)


def test_every_request_carries_a_reproducible_preference_id(tmp_path):
    """The backstop under the plan's own idempotence.

    `--preference-id` is optional and gcloud invents a UUID without it — there
    is one on the live project, `a0e3b926-...`, filed by this tool on
    2026-09-09. A random id means a second submission CANNOT collide with the
    first, so "do not re-ask" rests entirely on a list read seconds earlier,
    possibly by another session. A derived id makes Google refuse the duplicate.
    """
    cloud = Cloud()
    run(cloud, config_path=tmp_path / "hosts.toml")

    args = request_for(cloud, A100)
    # POSITIONAL on `update`, a flag on `create`. Easy to get wrong, and the
    # whole command is silently about the wrong thing when you do.
    assert args[3] == quota_preference_id(A100, {"region": "us-central1"}), args
    for sent in cloud.submitted:
        assert sent[3].startswith("comfyqat_"), f"no derived id on {sent}"


def test_a_preference_id_collision_reads_as_already_asked_not_as_a_failure(tmp_path):
    cloud = Cloud(create_error=GcloudError(
        "Resource 'comfy-qat-...' already exists in the project"))
    p = run(cloud, config_path=tmp_path / "hosts.toml")

    assert any("already requested" in line for line in p.said)
    assert not any("refused" in line and "A100  " in line for line in p.said)


def test_unreadable_preferences_mean_nothing_is_sent(tmp_path):
    """Without the list there is no way to tell pending from missing.

    Asking anyway is the duplicate submission this whole step exists to avoid,
    so the failure is a refusal to guess rather than a best effort.
    """
    cloud = Cloud(prefs_error=GcloudError("gcloud timed out after 240s"))
    p = run(cloud, config_path=tmp_path / "hosts.toml")

    assert cloud.submitted == []
    assert any("a second request cannot be ruled out" in line for line in p.said)


def test_an_unreadable_quota_list_means_nothing_is_sent(tmp_path):
    """There is nothing to plan from, and a plan from nothing asks for everything."""
    p = prompts()
    cloud = Cloud()
    result = ensure_quota_requests(
        cloud.gcloud(), p, "proj-1", quotas=None, interactive=True, region=None)

    assert result == []
    assert cloud.submitted == []


# --- a card that cannot be asked for is a line, not an exception --------------


def test_h100_is_requested_through_the_family_shape(tmp_path):
    """The card the tool could not see at all, and the reason for the row layer.

    There is no `NVIDIA-H100-GPUS-per-project-region` on a real project. Google
    gives the H100 no standard per-model quota; its on-demand allowance is
    `GPUS-PER-GPU-FAMILY-per-project-region` with `gpu_family=NVIDIA_H100`. The
    tool knew only the per-card shape, so `--gpu h100` reported "this project
    reports no quota for 'h100'" about a card the project meters perfectly well.
    """
    cloud = Cloud()
    run(cloud, config_path=tmp_path / "hosts.toml")

    args = request_for(cloud, FAMILY)
    assert flag(args, "dimensions") == "gpu_family=NVIDIA_H100,region=us-central1"
    assert flag(args, "preferred-value") == "8", "an H100 comes in eights"


def test_a_family_request_names_both_dimensions_or_it_is_about_no_card(tmp_path):
    """One quota id carries five cards. Without `gpu_family` the request is about
    none of them, and it is the kind of wrong a reviewer at Google acts on."""
    cloud = Cloud()
    run(cloud, config_path=tmp_path / "hosts.toml")

    dims = flag(request_for(cloud, FAMILY), "dimensions")
    assert "gpu_family=" in dims and "region=" in dims, dims


def test_the_shape_is_read_from_the_project_never_assumed():
    """Both shapes are live and a project reports one or the other.

    Given a project that DOES meter H100 per-card, the same card must resolve to
    the per-card id — so neither shape may be hardcoded. This is the inverse of
    the test above, and the two together are what stop a fix for one project from
    breaking every other.
    """
    per_card = [q for q in THIS_PROJECT
                if q["quotaId"] != FAMILY] + [quota(H100_PER_CARD, 0)]
    by_card = {ask.card: ask for ask in plan_quota(per_card, PREFS_NONE)}

    assert by_card["h100"].outcome == REQUEST
    assert by_card["h100"].target.quota_id == H100_PER_CARD
    assert set(by_card["h100"].target.dims) == {"region"}, (
        "a region-scoped id requires a region and nothing else")


def test_an_unrequestable_card_does_not_abort_setup(tmp_path):
    """Requestability is a fact about the PROJECT, not about the card.

    A project reporting neither shape for a card in the table must produce a line
    and finish, not an exception — `quota request --gpu h100` failing with "this
    project reports no quota" is right for somebody who typed it and fatal for a
    step that walks the whole table.
    """
    config = tmp_path / "hosts.toml"
    bare = [q for q in THIS_PROJECT if q["quotaId"] != FAMILY]
    cloud = Cloud(quotas=bare)
    p = run(cloud, config_path=config)

    assert config.exists(), "setup did not finish"
    line = next(line for line in p.said if "H100" in line)
    # The sentence now names the REGION it is about — it used to claim the whole
    # project did not meter a card that is granted in forty-three regions,
    # whenever the planning region had no row of its own.
    assert "meters no" in line, line
    assert FAMILY not in creates(cloud)


def test_no_card_the_tool_cannot_drive_is_ever_asked_for(tmp_path):
    """Approval takes days and ends in `create` refusing anyway.

    P100 has no GSP, so the open kernel module this tool installs cannot bring it
    up. A project metering it must not have it requested on its behalf.
    """
    cloud = Cloud(quotas=THIS_PROJECT + [quota(P100, 0)])
    run(cloud, config_path=tmp_path / "hosts.toml")

    assert P100 not in creates(cloud)


# --- the card set is derived from the table, not typed here -------------------


def test_every_drivable_card_in_the_table_is_accounted_for():
    """Class 2, in the direction that fails open: a card added to `CARDS` and
    forgotten here would simply never be asked for, silently.

    So the plan's card set is compared against `drivable_cards()` — the same
    derivation `create --gpu`, `quota request` and the help all read — rather
    than against anything written in this file.
    """
    metered = [quota(f"NVIDIA-{CARDS[key].quota_names[-1].upper()}-GPUS-"
                     "per-project-region", 0)
               for key in drivable_cards()]
    plan = plan_quota(metered, PREFS_NONE)
    planned = {ask.card for ask in plan if ask.card}

    assert planned == set(drivable_cards()), (
        "the plan and the card table disagree about which cards exist")


def test_no_undrivable_card_reaches_the_plan_at_all():
    """The other direction, and the reverse of the test above.

    Note that these two use DISJOINT vocabularies — one derives from
    `drivable_cards()` and one from `undrivable_cards()` — so neither can clear
    a member the other included. That is class 7's remedy: assert the
    relationship between the two lists, not the membership of either.
    """
    metered = THIS_PROJECT + [
        quota(f"NVIDIA-{key.upper()}-GPUS-per-project-region", 0)
        for key in undrivable_cards()
    ]
    planned = {ask.card for ask in plan_quota(metered, PREFS_NONE) if ask.card}

    assert planned & set(undrivable_cards()) == set()
    assert undrivable_cards(), "the guard is vacuous if nothing is undrivable"


# --- what value to ask for, and where ----------------------------------------


def test_the_ceiling_is_asked_for_because_one_is_the_binding_limit(tmp_path):
    """`GPUS-ALL-REGIONS-per-project` is 1 here, and it caps every card at once.

    Granting A100 changes nothing about running two boxes while that stays at 1,
    so a step that asks for cards and not for the ceiling has not delivered the
    thing it was asked for. And it was ASKED FOR AT 1 AND APPROVED AT 1 — the
    live `gpus-all-regions-1` says "Quota request approved to 1" — so a step that
    read "there is already a preference here" as "nothing to do" would leave the
    single binding limit untouched forever.
    """
    cloud = Cloud(preferences=PREFS_CEILING_AT_ONE)
    run(cloud, config_path=tmp_path / "hosts.toml")

    args = request_for(cloud, CEILING)
    assert int(flag(args, "preferred-value")) > 1
    assert args[3] == "gpus-all-regions-1", (
        "an existing preference must be updated under ITS OWN id — Google "
        "refuses a new id for a pair that already has one")


def test_a_satisfied_preference_at_too_low_a_number_is_still_asked_for(tmp_path):
    """The inverse of the test above, stated on its own because it is the trap.

    `satisfied` means Google gave what was asked. It does not mean the number is
    enough. Reading the two as the same thing is how the ceiling stays at 1.
    """
    cloud = Cloud(preferences=PREFS_CEILING_AT_ONE)
    run(cloud, config_path=tmp_path / "hosts.toml")
    assert CEILING in creates(cloud)


def test_the_ceiling_asked_for_is_two_when_no_card_needs_more(tmp_path):
    """2 because 1 is already the binding limit and 2 is the smallest number
    that makes two boxes at once possible — not 8, which would be asking a
    reviewer for seven machines' headroom to justify a comparison between two."""
    no_family = [q for q in THIS_PROJECT if q["quotaId"] != FAMILY]
    ceiling = next(a for a in plan_quota(no_family, PREFS_NONE) if a.card == "")
    assert ceiling.value == CEILING_REQUEST == 2


def test_the_ceiling_request_carries_no_region(tmp_path):
    """It is genuinely global. Attaching a region builds a request Google rejects."""
    cloud = Cloud()
    run(cloud, config_path=tmp_path / "hosts.toml")

    assert flag(request_for(cloud, CEILING), "dimensions") is None


def test_every_region_scoped_request_names_a_region(tmp_path):
    """THE BUG A REAL SUBMISSION FOUND, and `--validate-only` never would.

    This file previously asserted the OPPOSITE — that a per-card request carries
    no dimensions — reasoning that the grant spans all 43 regions in one go, so
    naming one would narrow it permanently. That is true of the GRANT and false
    of the REQUEST. Sent for real, `NVIDIA-A100-GPUS-per-project-region` with no
    dimensions came back:

        INVALID_ARGUMENT: Dimension values must be set for all the dimensions
        (except "user" and "resource" if they are defined) defined for the quota.

    The identical request had passed `--validate-only` three times. So the rule
    is read off the id — `-per-project-region` defines a region dimension and
    every defined dimension must be set — and `GPUS-ALL-REGIONS-per-project`,
    which defines none, must still carry none.
    """
    cloud = Cloud()
    run(cloud, config_path=tmp_path / "hosts.toml")

    assert cloud.submitted, "nothing was sent, so nothing was checked"
    for args in cloud.submitted:
        quota_id = flag(args, "quota-id")
        dims = flag(args, "dimensions") or ""
        if quota_id.endswith("-per-project-region"):
            assert "region=" in dims, f"{quota_id} names no region: {args}"
            if quota_id == FAMILY:
                assert "gpu_family=" in dims, args
        else:
            assert not dims, f"{quota_id} defines no dimensions but sent {dims}"


def ceiling_of(quotas, preferences=PREFS_NONE):
    return next(a for a in plan_quota(quotas, preferences) if a.card == "")


def test_a_fresh_project_asks_for_two_and_not_eight():
    """THE CASE THAT MATTERS, because it is every project on its first run.

    Every GCP project reports `GPUS-PER-GPU-FAMILY-per-project-region` with
    `gpu_family=NVIDIA_H100` as service metadata — present, `details: {}`, which
    is this API's zero. So H100 is a REQUEST on any project that has never held
    one, and a ceiling computed from REQUESTS was `max(2, 8)` = 8 universally,
    with `CEILING_REQUEST` dead code.

    Measured on a simulated fresh install before the fix: `any (global) request 8`
    — the exact over-ask `CEILING_REQUEST`'s own comment argues against.
    """
    fresh = [
        {**q, "dimensionsInfos": [{**info, "details": {}}
                                  for info in q.get("dimensionsInfos") or []]}
        for q in THIS_PROJECT
    ]
    assert any(a.card == "h100" and a.outcome == REQUEST
               for a in plan_quota(fresh, PREFS_NONE)), "the premise moved"
    assert ceiling_of(fresh).value == CEILING_REQUEST == 2


def test_the_ceiling_counts_what_the_project_holds_not_what_it_has_asked_for():
    """Headroom is only worth anything for a card you can actually start.

    The live project holds L4 and T4 at 1 and is ASKING for an H100 at 8, so the
    ceiling ask is 2. Counting the request instead would make it 8 on every
    project in the world.
    """
    assert CARDS["h100"].count == 8, "the premise moved; re-read the table"
    assert ceiling_of(THIS_PROJECT).value == CEILING_REQUEST


def test_a_project_that_HOLDS_a_big_card_asks_for_what_it_needs():
    """The other direction, so the fix is not just "always 2".

    Granted H100 and a ceiling of 2 is a create that passes the per-card gate and
    fails at the ceiling — which is the class of defect this whole feature is
    about. Read from `Card.count`, not typed.
    """
    holds_h100 = [q for q in THIS_PROJECT if q["quotaId"] != FAMILY]
    holds_h100 = holds_h100 + [family(8, "NVIDIA_H100")]
    ceiling = ceiling_of(holds_h100)

    assert ceiling.value == CARDS["h100"].count == 8
    assert "already holds" in ceiling.detail, (
        "the reason for an unusual number belongs beside it")


def test_a_big_card_being_requested_says_the_second_run_is_what_raises_it():
    """The gap the GRANTED-only rule leaves, named rather than left to be found."""
    detail = ceiling_of(THIS_PROJECT).detail
    assert "run setup again" in detail and "H100" in detail, detail


def test_a_card_needing_more_than_one_asks_for_more_than_one():
    """`--value 1` for a card that only comes in eights is a request that, if
    granted exactly, still cannot start the machine it was asked for."""
    h100 = next(a for a in plan_quota(THIS_PROJECT, PREFS_NONE) if a.card == "h100")
    assert h100.value == CARDS["h100"].count == 8


def test_an_all_regions_grant_does_not_get_read_as_a_preference():
    """The defect the first live dry run produced: **africa-south1**.

    The L4 grant on the real project covers 43 regions, `regions_with_quota`
    sorts them, and africa-south1 is first alphabetically. Nothing was broken —
    it was the honest answer to the wrong question. A grant spanning every region
    says nothing about where anybody works, so reading a preference out of one
    invents a fact, and the request goes somewhere the tester will never use.

    This is the shape `docs/tests-that-cannot-fail.md` calls class 9: a proxy for
    the question. "Where is quota granted" correlates with "where do you work"
    only on a project whose grant is narrow.
    """
    spread = [quota(L4, 1, locations=["africa-south1", "europe-west4",
                                      "us-central1"])]
    where, why = request_region(spread, PREFS_NONE, None)

    assert where != "africa-south1", (
        "an all-regions grant was read as a choice of region")
    assert where == "us-central1"
    assert "default" in why


def test_the_region_the_project_has_asked_in_before_is_the_one_used():
    """An expressed intention, rather than a side effect of how Google grants.

    Three of the five standing requests on the live project name us-central1 and
    one names europe-west4, which is what this reproduces.
    """
    spread = [quota(L4, 1, locations=["africa-south1", "europe-west4",
                                      "us-central1"])]
    prefs = [
        preference(L4, granted=1, preferred=1, dimensions={"region": "us-central1"}),
        preference(A100_80, granted=0, preferred=1,
                   dimensions={"region": "us-central1"},
                   state_detail=DENIED_DETAIL),
        preference(A100, granted=0, preferred=1,
                   dimensions={"region": "europe-west4"}),
    ]
    where, why = request_region(spread, prefs, None)

    assert where == "us-central1"
    assert "asked for GPU quota before" in why


def test_a_grant_in_exactly_one_region_is_an_unambiguous_signal():
    """Narrow enough to mean something, unlike the 43-region case above."""
    held = [quota(L4, 1, locations=["europe-west4"]), quota(CEILING, 1, ["global"])]
    where, why = request_region(held, PREFS_NONE, None)

    assert where == "europe-west4"
    assert "only region" in why


def test_an_explicit_region_wins():
    where, why = request_region(THIS_PROJECT, PREFS_PENDING, "asia-northeast1")
    assert where == "asia-northeast1"
    assert "you asked" in why


def test_a_project_that_has_never_asked_for_anything_falls_back_and_says_so():
    where, why = request_region([quota(L4, 0)], PREFS_NONE, None)
    assert where == "us-central1"
    assert "no GPU quota asked for anywhere yet" in why


def test_the_justification_reaches_google(tmp_path):
    cloud = Cloud()
    run(cloud, config_path=tmp_path / "hosts.toml")

    for args in cloud.submitted:
        assert flag(args, "justification") == DEFAULT_JUSTIFICATION, args


def test_a_justification_of_your_own_replaces_it_and_is_shown(tmp_path):
    cloud = Cloud()
    mine = "Comfy Org release QA, PM-630 milestone 2.0 test pass."
    p = run(cloud, config_path=tmp_path / "hosts.toml", justification=mine)

    for args in cloud.submitted:
        assert flag(args, "justification") == mine, args
    assert any(mine in line for line in p.said)


# --- one card failing is not the run failing ---------------------------------


def test_a_refused_request_does_not_stop_the_others_or_the_setup(tmp_path):
    """Asserted on the COUNT of attempts, not on the messages.

    A `return` where the loop needs `continue` leaves the first card's refusal
    printed and every later card silently unasked — output that reads fine.
    """
    config = tmp_path / "hosts.toml"
    cloud = Cloud(create_error=GcloudError("Permission denied on quota"))
    p = run(cloud, config_path=config)

    assert config.exists(), "a refused quota request is not a failed setup"
    wanted = [a for a in plan_quota(cloud.quotas, PREFS_NONE) if a.submits]
    assert len(cloud.submitted) == len(wanted) >= 2, (
        f"stopped after {len(cloud.submitted)} of {len(wanted)}")
    assert any("refused" in line for line in p.said)


def test_no_request_is_tracked_as_sent_when_every_one_was_refused(tmp_path):
    cloud = Cloud(create_error=GcloudError("Permission denied on quota"))
    p = run(cloud, config_path=tmp_path / "hosts.toml")
    assert not any("track them" in line for line in p.said)


# --- the four outcomes are all reachable, and all distinguishable -------------

OUTCOMES = (GRANTED, PENDING, DENIED, REQUEST, UNAVAILABLE)


def test_every_outcome_the_module_defines_is_reachable():
    """Class 4: an outcome with no path to it is a branch that cannot report.

    One project produces four of the five; the fifth, PENDING, needs a
    preference Google has not answered. Both are built here so that removing any
    branch from `plan_quota` fails by the name of the outcome it dropped.
    """
    bare = [q for q in THIS_PROJECT if q["quotaId"] != FAMILY]
    seen = {ask.outcome for ask in plan_quota(bare, PREFS_DENIED)}
    seen |= {ask.outcome for ask in plan_quota(THIS_PROJECT, PREFS_PENDING)}

    assert seen == set(OUTCOMES), f"unreachable: {set(OUTCOMES) - seen}"


@pytest.mark.parametrize("outcome", OUTCOMES)
def test_each_outcome_reads_as_a_different_sentence(outcome):
    """Four reasons not to ask, collapsed into one word, is how "Google already
    refused this" becomes invisible."""
    bare = [q for q in THIS_PROJECT if q["quotaId"] != FAMILY]
    plan = (plan_quota(bare, PREFS_DENIED)
            + plan_quota(THIS_PROJECT, PREFS_PENDING))
    details = {ask.outcome: ask.detail for ask in plan}

    assert details[outcome].strip(), f"{outcome} has nothing to say"
    others = [d for state, d in details.items() if state != outcome]
    assert details[outcome] not in others


def test_only_the_request_outcome_submits(tmp_path):
    """The inclusion token and the submission token are the same thing —
    `QuotaAsk.submits` — so this pins the one relationship that matters: no
    other outcome can acquire a value and quietly start being sent."""
    cloud = Cloud(preferences=PREFS_DENIED)
    run(cloud, config_path=tmp_path / "hosts.toml")

    wanted = {a.target.quota_id for a in plan_quota(cloud.quotas, PREFS_DENIED)
              if a.outcome == REQUEST}
    assert set(creates(cloud)) == wanted
    assert all(a.value == 0 for a in plan_quota(cloud.quotas, PREFS_DENIED)
               if a.outcome != REQUEST)


# --- the two request shapes, and the rules Google enforces on both -----------


def test_the_family_cards_are_visible_at_all():
    """The gap behind the whole feature: five cards nobody could see.

    `H100-80GB`, NOT `H100`: `family_name` returns the CARD TABLE's name for a
    family the table knows, so one card has one name across every command.
    `quota list` used to print H100 while `setup` and `create` printed
    H100-80GB about the same row.

    `friendly_name` reads an ID, and `GPUS-PER-GPU-FAMILY-per-project-region`
    names no card, so H100, H100-MEGA, H200, B200 and RTX-PRO-6000 were absent
    from `quota list`, from `available_gpus`, and from every "ask for one of:"
    line — on a project that meters all five. A user could not see the cards
    existed, could not see they were at zero, and had no way to ask.
    """
    from comfy_qa.quota import available_gpus, readiness

    offered = available_gpus(THIS_PROJECT)
    for card in ("H100-80GB", "H100-MEGA", "H200", "B200", "RTX-PRO-6000"):
        assert card in offered, f"{card} is invisible"

    shown = {row.gpu: row.status for row in readiness(THIS_PROJECT)}
    assert shown["H100-80GB"] == "none", "an empty `details` object means zero"


def test_a_card_the_table_has_never_heard_of_is_still_reported():
    """`quota list` reports the PROJECT, not the card table. RTX PRO 6000 has no
    row in `CARDS` — deliberately, it has no verified machine type — and must
    still appear, or the tool is hiding quota the user holds."""
    from comfy_qa.create import card_named
    from comfy_qa.quota import available_gpus

    assert card_named("RTX-PRO-6000") is None, "the premise moved"
    assert "RTX-PRO-6000" in available_gpus(THIS_PROJECT)


def test_a_card_with_no_machine_type_is_never_asked_for(tmp_path):
    """Visible is not the same as orderable.

    Asking for a card `create` cannot build is the same dead end as the GSP case
    the tool already refuses: days of waiting, then a refusal at the create. So
    the four families absent from the table are reported and never requested.
    """
    cloud = Cloud()
    run(cloud, config_path=tmp_path / "hosts.toml")

    families = [flag(a, "dimensions") for a in cloud.submitted
                if flag(a, "quota-id") == FAMILY]
    for absent in ("NVIDIA_H200", "NVIDIA_B200", "NVIDIA_H100_MEGA",
                   "NVIDIA_RTX_PRO_6000"):
        assert not any(absent in dims for dims in families), absent


def test_one_pending_family_request_does_not_mark_the_other_four_pending(tmp_path):
    """Five cards share ONE quota id, so keying on the id alone is wrong.

    The same defect one size smaller: a pending L4 request in us-central1 keyed
    on `NVIDIA-L4-GPUS-per-project-region` marked L4 pending in all 43 regions.
    """
    pending_rtx = [preference(
        FAMILY, granted=0, preferred=1, reconciling=True, name="rtx-pending",
        dimensions={"gpu_family": "NVIDIA_RTX_PRO_6000", "region": "us-central1"})]
    by_card = {a.card: a for a in plan_quota(THIS_PROJECT, pending_rtx)}

    assert by_card["h100"].outcome == REQUEST, (
        "a pending RTX PRO 6000 was read as covering the H100")


def test_a_refusal_in_one_region_stops_an_all_regions_re_ask(tmp_path):
    """Found by the code doing it. `a100-80-euw4` was refused in europe-west4;
    an all-regions A100-80GB request has different dimensions, so an exact match
    found nothing and setup re-asked for a card Google had already refused."""
    cloud = Cloud(preferences=PREFS_DENIED)
    p = run(cloud, config_path=tmp_path / "hosts.toml")

    assert A100_80 not in creates(cloud)
    line = next(line for line in p.said if "A100-80GB" in line)
    assert "europe-west4" in line, "the region it was refused in is the useful half"


def test_an_existing_preference_is_updated_under_its_own_id(tmp_path):
    """RULE 1, verified live: minting a new id for a (quota id, dimensions) pair
    that already has one is refused — "Quota Preference with dimension '{}'
    already exist for container ...". So a deterministic id alone is wrong."""
    mine = [preference(A100, granted=0, preferred=1,
                       state_detail="Quota request approved to 0",
                       name="a100-hand-named")]
    # Driven through `request_plan` directly: the addressing rule is what is
    # under test, not whether the plan would choose to ask.
    from comfy_qa.quota import Target, request_plan

    addressed = request_plan(Target(A100), mine)
    assert addressed.preference_id == "a100-hand-named"
    assert addressed.allow_missing is False, (
        "--allow-missing on an existing preference is a create that will be refused")


def test_a_pair_nothing_holds_gets_a_derived_id_and_allow_missing():
    from comfy_qa.quota import Target, request_plan

    addressed = request_plan(Target(A100), PREFS_NONE)
    assert addressed.preference_id.startswith("comfyqat_")
    assert addressed.allow_missing is True


def test_the_dimensions_are_part_of_the_id():
    """RULE 2: dimensions are IMMUTABLE and a preference can never be deleted.

    So one id cannot serve two dimension sets — reusing it fails rather than
    migrating. H100 in us-central1 and H100 in europe-west2 are two ids.
    """
    usc = quota_preference_id(FAMILY, {"gpu_family": "NVIDIA_H100",
                                       "region": "us-central1"})
    euw = quota_preference_id(FAMILY, {"gpu_family": "NVIDIA_H100",
                                       "region": "europe-west2"})
    other = quota_preference_id(FAMILY, {"gpu_family": "NVIDIA_H200",
                                         "region": "us-central1"})
    assert usc != euw != other and usc != other
    assert usc == quota_preference_id(FAMILY, {"region": "us-central1",
                                               "gpu_family": "NVIDIA_H100"}), (
        "the same inputs in another order must give the same id")


def test_every_preference_id_is_legible_and_bounded():
    """The cap was 63 — a number nobody checked — and it truncated the quota NAME
    out of a long id, leaving `comfyqat_ect-region_nvidia-rtx-pro-6000_...`,
    which defeats the point of deriving a readable one.

    Probed against the live API with `--validate-only` on 2026-09-17: ids of 63,
    64, 100, 200 and 250 characters all validate, exit 0, creating nothing. So
    the bound is a sanity limit and nothing real reaches it — which is why the
    assertion below is that the quota's name SURVIVES, not merely that the id is
    short. A length check alone passed the truncated id that caused this.
    """
    import re

    for target in (FAMILY, A100_80, CEILING):
        for dims in ({}, {"gpu_family": "NVIDIA_RTX_PRO_6000",
                          "region": "northamerica-northeast2"}):
            made = quota_preference_id(target, dims)
            assert len(made) <= 120, made
            assert re.fullmatch(r"[a-z0-9_-]+", made), made
            assert target.lower() in made, (
                f"{made} no longer says which quota it is")


def test_the_verb_is_update_so_a_re_run_cannot_duplicate(tmp_path):
    """`create` mints a new, permanent, undeletable preference every call."""
    cloud = Cloud()
    run(cloud, config_path=tmp_path / "hosts.toml")

    for args in cloud.submitted:
        assert args[:3] == ["quotas", "preferences", "update"], args
        assert "create" not in args


def test_every_request_names_a_contact_email(tmp_path):
    """The schema: "When requesting a quota increase, the email address is
    required." gcloud: without one the request "will be denied in case further
    information is required to make a decision." This tool sent none, so every
    request it filed was one question away from an automatic refusal."""
    cloud = Cloud()
    run(cloud, config_path=tmp_path / "hosts.toml")

    for args in cloud.submitted:
        assert flag(args, "email") == "ali@comfy.org", args


# --- the dry run reaches Google and creates nothing --------------------------


def test_validate_only_checks_with_google_and_creates_nothing(tmp_path):
    """Stronger than printing a string: it reaches Google, validates the whole
    request and creates nothing — and `create` has no such flag, which is one
    more reason the verb had to change.

    THIS TEST WAS CALLED `test_dry_run_validates_with_google...` and drove
    `--dry-run`, which is the collision itself: the same flag printed on
    `quota request` and called Google on `setup`. `--dry-run` now prints on both
    and `--validate-only` reaches Google on both.
    """
    cloud = Cloud()
    p = run(cloud, config_path=tmp_path / "hosts.toml", quota_validate_only=True)

    assert cloud.submitted, "a validate run that calls nothing has checked nothing"
    for args in cloud.submitted:
        assert "--validate-only" in args, args
    assert any("Nothing was created" in line for line in p.said)


def test_a_dry_run_never_asks_for_confirmation(tmp_path):
    """There is nothing to consent to: nothing is created."""
    p = run(Cloud(), config_path=tmp_path / "hosts.toml", quota_dry_run=True)
    assert p.asked == []


def test_nothing_printed_promises_approval(tmp_path):
    """Three of the five standing requests on the live project were refused."""
    cloud = Cloud()
    p = run(cloud, config_path=tmp_path / "hosts.toml")
    text = "\n".join(p.said)

    assert "Asking is not being granted" in text
    assert "asked, not granted" in text
    for promise in ("will be granted", "you now have", "approved"):
        assert promise not in text, promise


def test_the_family_spelling_comes_from_the_card_table(tmp_path):
    """`gpu_family` is an enum value Google defines, not a name to fuzzy-match.

    Deriving `H100` from `NVIDIA_H100` and comparing it to a card alias happens
    to work, and only because the table carries an `H100` alias for an unrelated
    reason. `Card.quota_family` states the exact value, so the request is built
    from a fact. A card that loses it can no longer be asked for at all — which
    is a visible refusal rather than a request built on a coincidence.
    """
    from comfy_qa.create import CARDS

    assert CARDS["h100"].quota_family == "NVIDIA_H100"
    cloud = Cloud()
    run(cloud, config_path=tmp_path / "hosts.toml")
    assert flag(request_for(cloud, FAMILY), "dimensions").startswith(
        "gpu_family=NVIDIA_H100")


def test_quota_list_still_names_a_family_card_the_table_cannot_ask_for():
    """The other half, and the reason the two are separate.

    Requests read the table; DISPLAY reads the project. RTX PRO 6000 has no row
    in `CARDS`, so nothing can request it — and `quota list` must still show it,
    or the tool is hiding quota the user holds.
    """
    from comfy_qa.quota import available_gpus, resolve_target

    assert "RTX-PRO-6000" in available_gpus(THIS_PROJECT)
    assert resolve_target("RTX-PRO-6000", THIS_PROJECT, family=None) is None


def test_updating_an_existing_preference_does_not_pass_allow_missing(tmp_path):
    """A safety property, not a syntax one — Google accepts the flag either way.

    Verified live: `update gpus-all-regions-1 ... --allow-missing --validate-only`
    is exit 0, so the flag is harmless on a preference that exists. What it is
    NOT harmless for is a bug in our own id derivation: with `--allow-missing`, an
    id that addresses nothing silently CREATES a permanent, undeletable
    preference instead of failing. So when the plan believes a preference exists,
    it says so by leaving the flag off, and a wrong id fails loudly.
    """
    cloud = Cloud(preferences=PREFS_CEILING_AT_ONE)
    run(cloud, config_path=tmp_path / "hosts.toml")

    ceiling = request_for(cloud, CEILING)
    assert ceiling[3] == "gpus-all-regions-1"
    assert "--allow-missing" not in ceiling, (
        "an id that turns out to address nothing would create a permanent row")
    # And the opposite case, so this is not just "the flag is never sent".
    fresh = request_for(cloud, A100)
    assert "--allow-missing" in fresh, "a pair nothing holds has to be created"


def test_a_resubmitted_request_is_pending_even_while_the_old_refusal_is_quoted():
    """Why `reconciling` is read rather than the prose.

    `update` on a denied preference puts it back in front of Google, and
    `stateDetail` still carries the OLD "Quota request denied" until the new
    answer lands. Reading the sentence would call that denied and refuse to wait
    for an answer that is genuinely coming; `reconciling` is the schema's own
    "pending Google Cloud approval and fulfillment" and says so.
    """
    from comfy_qa.quota import asks

    resubmitted = preference(A100_80, granted=0, preferred=1,
                             state_detail=DENIED_DETAIL, reconciling=True,
                             name="a100-80-euw4")
    assert asks([resubmitted])[0].state == "pending"

    settled_refusal = preference(A100_80, granted=0, preferred=1,
                                 state_detail=DENIED_DETAIL, name="a100-80-euw4")
    assert asks([settled_refusal])[0].state == "denied", (
        "without the flag the sentence is the only evidence there is")


# --- do the two rules compose? ------------------------------------------------
#
# "An existing preference must be updated under its own id" and "a card Google has
# refused is not re-asked" are independent rules, and RTX PRO 6000 is where they
# meet: `rtxpro6000-usc1` and `rtxpro6000-euw2` both exist on the live project and
# both are DENIED. Verified against Google on 2026-09-17, creating nothing:
#
#   derived id, family pair that already has a preference  -> INVALID_ARGUMENT,
#     "Quota Preference with dimension '{gpu_family=NVIDIA_RTX_PRO_6000}' already
#      exist ... in location 'us-central1'"
#   the same pair addressed by `rtxpro6000-usc1`           -> exit 0
#   a family pair nobody holds (H100, us-central1)         -> exit 0
#
# So the collision rule is NOT only about the empty dimension set `{}` — it
# applies per dimension set, family dimensions included.

RTX = "NVIDIA_RTX_PRO_6000"
PREFS_RTX_REFUSED = [
    preference(FAMILY, granted=0, preferred=1, state_detail=DENIED_DETAIL,
               name="rtxpro6000-usc1",
               dimensions={"gpu_family": RTX, "region": "us-central1"}),
    preference(FAMILY, granted=0, preferred=1, state_detail=DENIED_DETAIL,
               name="rtxpro6000-euw2",
               dimensions={"gpu_family": RTX, "region": "europe-west2"}),
]


def test_a_refused_family_pair_is_addressed_by_its_own_id_not_a_derived_one():
    """Rule 1 for the family shape. A derived id here is refused by Google."""
    from comfy_qa.quota import Target, request_plan

    target = Target(FAMILY, (("gpu_family", RTX), ("region", "us-central1")))
    addressed = request_plan(target, PREFS_RTX_REFUSED)

    assert addressed.preference_id == "rtxpro6000-usc1"
    assert addressed.allow_missing is False
    assert addressed.dimensions == {"gpu_family": RTX, "region": "us-central1"}


def test_the_other_region_gets_the_other_id():
    """Two preferences, one quota id, one gpu_family, different regions. Keying on
    anything less than the full dimension set addresses the wrong one."""
    from comfy_qa.quota import Target, request_plan

    target = Target(FAMILY, (("gpu_family", RTX), ("region", "europe-west2")))
    assert request_plan(target, PREFS_RTX_REFUSED).preference_id == "rtxpro6000-euw2"


def test_a_refused_family_card_is_not_re_asked_in_the_first_place():
    """And the rules compose: the denial means the id is never needed.

    The card must be metered AND in the table to reach the plan at all, so this
    drives the H100 — which is the family card the table does carry — with the
    same refusal shape RTX PRO 6000 has on the live project.

    `region=` IS EXPLICIT AND IT IS THE REFUSAL'S OWN REGION. It used to be
    omitted, and the plan region then defaulted to us-central1 and happened to
    equal the refusal region — so the test could not distinguish "refused here"
    from "refused somewhere", and making `asks_about` region-aware would not have
    failed it. `plan_quota` takes a region and was called twenty-three times
    across this suite without one; the untested parameter is where H1 lived.
    Its opposite number is `test_a_family_card_refused_elsewhere_is_still_asked_for_here`.
    """
    refused_h100 = [preference(
        FAMILY, granted=0, preferred=1, state_detail=DENIED_DETAIL,
        name="h100-usc1",
        dimensions={"gpu_family": "NVIDIA_H100", "region": "us-central1"})]
    by_card = {a.card: a for a in
               plan_quota(THIS_PROJECT, refused_h100, region="us-central1")}

    assert by_card["h100"].outcome == DENIED
    assert "h100-usc1" in by_card["h100"].detail, (
        "the preference id is what a person needs to look it up")


def test_a_refusal_for_one_family_does_not_silence_another():
    """Five cards share the quota id. A refused RTX PRO 6000 must not read as a
    refused H100 — which is what keying on the id alone would do."""
    by_card = {a.card: a for a in plan_quota(THIS_PROJECT, PREFS_RTX_REFUSED)}
    assert by_card["h100"].outcome == REQUEST


# --- the four the verifier found passing with the behaviour inverted ----------


def test_a_partial_grant_is_its_own_state_and_not_pending():
    """`partial` existed in the type and nothing held it there.

    Google answered and gave less than was asked for. Nobody is still deciding,
    so calling it pending tells the user to wait for an answer that arrived; and
    it is not a refusal either, because something was granted.
    """
    from comfy_qa.quota import asks

    short = preference(CEILING, granted=1, preferred=8,
                       state_detail="Quota request approved to 1",
                       name="gpus-all-regions-1")
    assert asks([short])[0].state == "partial"
    assert asks([short])[0].granted == 1


def test_a_partially_granted_request_is_not_re_asked(tmp_path):
    """THE STATE THIS PROJECT REACHES ON THE FIRST RE-RUN AFTER SUBMITTING.

    Ask for a ceiling of 8, be granted 1, and the preference reads granted=1 /
    preferred=8 / "Quota request approved to 1". That is a partial, and re-filing
    it on every subsequent `setup` is asking a human the same question in a loop.
    """
    partial = [preference(CEILING, granted=1, preferred=8,
                          state_detail="Quota request approved to 1",
                          name="gpus-all-regions-1")]
    cloud = Cloud(preferences=partial)
    p = run(cloud, config_path=tmp_path / "hosts.toml")

    assert CEILING not in creates(cloud)
    line = next(line for line in p.said if "any (global)" in line)
    assert "1 of the 8" in line, line


def test_covers_is_not_dimension_blind():
    """`asks_about` had this covered and `covers` did not — the same idea on the
    `quota list` surface. One pending H100 family request must not paint
    H100-MEGA, H200, B200 and RTX-PRO-6000 as pending: they share one quota id.
    """
    from comfy_qa.quota import readiness

    pending_h100 = [preference(
        FAMILY, granted=0, preferred=1, reconciling=True, name="h100-pending",
        dimensions={"gpu_family": "NVIDIA_H100", "region": "us-central1"})]
    shown = {row.gpu: row.status for row in readiness(THIS_PROJECT, pending_h100)}

    assert shown["H100-80GB"] == "pending"
    for other in ("H200", "B200", "H100-MEGA", "RTX-PRO-6000"):
        assert shown[other] == "none", f"{other} was painted by the H100 request"


def test_matching_ask_will_not_land_an_all_regions_request_on_a_pinned_one():
    """An id-only match silently addresses the wrong preference, permanently.

    The L4 target is `dims={}` — all regions — while the standing L4 preference
    is `{region: us-central1}`. Matching on the id alone would reuse that
    preference's id with `allow_missing=False`, quietly turning an all-regions
    request into an edit of a region-pinned one. Dimensions are immutable, so
    there is no way back.
    """
    from comfy_qa.quota import Target, matching_ask, request_plan

    pinned = [preference(L4, granted=1, preferred=1, name="a0e3b926",
                         dimensions={"region": "us-central1"})]
    assert matching_ask(Target(L4), pinned) is None, "matched across dimensions"

    addressed = request_plan(Target(L4), pinned)
    assert addressed.preference_id.startswith("comfyqat_")
    assert addressed.allow_missing is True
    assert addressed.dimensions == {}


def test_matching_ask_does_match_when_the_dimensions_really_are_the_same():
    """The guard standing aside, which needs a test as much as the guard firing."""
    from comfy_qa.quota import Target, matching_ask

    pinned = [preference(L4, granted=1, preferred=1, name="a0e3b926",
                         dimensions={"region": "us-central1"})]
    found = matching_ask(Target(L4, (("region", "us-central1"),)), pinned)
    assert found is not None and found.preference_id == "a0e3b926"


# --- BUG 2: a region chosen by list order can never be corrected --------------


def test_the_only_region_claim_is_about_the_project_not_the_first_card():
    """A100 in europe-west4 and L4 in us-central1: neither is "the only region".

    This returned europe-west4 with exactly that sentence, because
    `drivable_cards()` is sorted and `a100` precedes `l4`. The africa-south1 bug
    again, one level up — a list order standing in for a judgement — in the
    function whose docstring claims to have prevented it.
    """
    split = [quota(A100, 1, locations=["europe-west4"]),
             quota(L4, 1, locations=["us-central1"])]
    where, why = request_region(split, PREFS_NONE, None)

    assert "only region" not in why, f"claimed {where!r} is the only region"


def test_the_region_tiebreak_does_not_depend_on_gcloud_s_ordering():
    """`Counter.most_common` breaks ties by INSERTION ORDER, so two regions asked
    for equally often gave different answers depending on how gcloud happened to
    list the preferences. Dimensions are immutable, so the wrong one is forever.

    THE FIRST VERSION OF THIS TEST WAS VACUOUS and the mutation sweep said so:
    it passed `THIS_PROJECT`, whose grants are all in us-central1, so the
    "only region" branch answered before the tiebreak was ever reached. The
    grants below are deliberately spread across two regions so that neither
    earlier branch can fire — which is the whole point of the case.
    """
    spread = [quota(L4, 1, locations=["us-central1", "europe-west4"]),
              quota(T4, 1, locations=["us-central1", "europe-west4"])]
    assert len({r for q in spread
                for r in q["dimensionsInfos"][0]["applicableLocations"]}) > 1, (
        "the earlier branches must not be able to answer this")

    one = [preference(A100, granted=0, preferred=1,
                      dimensions={"region": "europe-west4"}),
           preference(T4, granted=0, preferred=1,
                      dimensions={"region": "us-central1"})]
    forwards = request_region(spread, one, None)
    backwards = request_region(spread, list(reversed(one)), None)

    assert forwards == backwards, f"{forwards} vs {backwards}"
    assert "asked for GPU quota before" in forwards[1], (
        "an earlier branch answered, so the tiebreak was never exercised")


def test_quota_request_can_ask_for_a_family_card_too(tmp_path, monkeypatch):
    """Both surfaces, or the working one makes the broken one look authoritative.

    `setup` passed `family=` into `resolve_target` and `quota request` did not,
    so `comfy-qat quota request --gpu h100` refused with "this project reports no
    quota for 'h100'" on a project that meters it — while setup asked for the
    same card in the same run. A refusal that states a falsehood about the
    project is worse than a crash, because it gets believed.
    """
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    cloud = Cloud()
    with monkeypatch.context() as patch:
        patch.setattr("comfy_qa.auth.Gcloud", lambda *a, **k: cloud.gcloud())
        result = CliRunner().invoke(
            app, ["quota", "request", "--gpu", "h100", "--no-wait", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "no quota for" not in result.output
    assert "gpu_family=NVIDIA_H100" in result.output


def test_a_fix_line_only_offers_cards_that_can_actually_be_asked_for(tmp_path, monkeypatch):
    """`available_gpus` reports what the PROJECT meters — right for a table, wrong
    for a fix line. H200, B200, H100-MEGA and RTX-PRO-6000 are all metered here
    and none is in the card table, so they were offered and then refused by the
    very next command."""
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    cloud = Cloud()
    with monkeypatch.context() as patch:
        patch.setattr("comfy_qa.auth.Gcloud", lambda *a, **k: cloud.gcloud())
        result = CliRunner().invoke(
            app, ["quota", "request", "--gpu", "nosuchcard", "--no-wait"])

    assert result.exit_code == 2
    for unaskable in ("H200", "B200", "RTX-PRO-6000", "H100-MEGA"):
        assert unaskable not in result.output, (
            f"{unaskable} was offered and cannot be requested")


# --- the real record, not five tidy ones --------------------------------------


def test_the_real_family_record_shape_is_read_correctly():
    """The fixtures above are THINNER THAN REALITY, so this is the real thing.

    A live project returns ONE `GPUS-PER-GPU-FAMILY-per-project-region` record
    carrying EIGHT `dimensionsInfos`, not five one-row records:

      {gpu_family: NVIDIA_RTX_PRO_6000, region: europe-west2}   1 location
      {gpu_family: NVIDIA_RTX_PRO_6000, region: us-central1}    1 location
      {gpu_family: NVIDIA_B200}                                43 locations
      {gpu_family: NVIDIA_H100_MEGA}                           43 locations
      {gpu_family: NVIDIA_H100}                                43 locations
      {gpu_family: NVIDIA_H200}                                43 locations
      {gpu_family: NVIDIA_RTX_PRO_6000}                        41 locations
      dimensions ABSENT                                        43 locations

    Two things a tidier fixture never exercises. RTX_PRO_6000 appears THREE
    times — a 41-region default plus the two regions its denied requests carved
    out — so a reader that stops at the first matching row gets the wrong one.
    And the last row has NO `dimensions` key at all: a catch-all about no card in
    particular, which must be dropped rather than named or counted.
    """
    from comfy_qa.quota import readiness, rows

    real = {
        "quotaId": FAMILY,
        "dimensionsInfos": [
            {"dimensions": {"gpu_family": "NVIDIA_RTX_PRO_6000",
                            "region": "europe-west2"},
             "details": {}, "applicableLocations": ["europe-west2"]},
            {"dimensions": {"gpu_family": "NVIDIA_RTX_PRO_6000",
                            "region": "us-central1"},
             "details": {}, "applicableLocations": ["us-central1"]},
            {"dimensions": {"gpu_family": "NVIDIA_B200"},
             "details": {}, "applicableLocations": REGIONS_43},
            {"dimensions": {"gpu_family": "NVIDIA_H100_MEGA"},
             "details": {}, "applicableLocations": REGIONS_43},
            {"dimensions": {"gpu_family": "NVIDIA_H100"},
             "details": {}, "applicableLocations": REGIONS_43},
            {"dimensions": {"gpu_family": "NVIDIA_H200"},
             "details": {}, "applicableLocations": REGIONS_43},
            {"dimensions": {"gpu_family": "NVIDIA_RTX_PRO_6000"},
             "details": {}, "applicableLocations": REGIONS_43[:3]},
            # The catch-all. No `dimensions` key at all.
            {"details": {}, "applicableLocations": REGIONS_43},
        ],
    }

    named = [row.gpu for row in rows([real])]
    assert len(named) == 7, f"the dims-less catch-all was named: {named}"
    assert sorted(set(named)) == ["B200", "H100-80GB", "H100-MEGA", "H200",
                                  "RTX-PRO-6000"]
    assert named.count("RTX-PRO-6000") == 3, "the three RTX rows must all survive"

    shown = {row.gpu: row.status for row in readiness([real])}
    assert set(shown) == {"B200", "H100-80GB", "H100-MEGA", "H200",
                          "RTX-PRO-6000"}
    assert all(status == "none" for status in shown.values())


def test_the_real_record_plans_the_same_as_the_tidy_fixture():
    """If the two disagree, the tidy one is the liar and every test above it is."""
    tidy = [q for q in THIS_PROJECT if q["quotaId"] != FAMILY] + [
        family(None, name) for name in
        ("NVIDIA_H100", "NVIDIA_H100_MEGA", "NVIDIA_H200", "NVIDIA_B200",
         "NVIDIA_RTX_PRO_6000")]
    real = [q for q in THIS_PROJECT if q["quotaId"] != FAMILY] + [{
        "quotaId": FAMILY,
        "dimensionsInfos": [
            {"dimensions": {"gpu_family": name}, "details": {},
             "applicableLocations": REGIONS_43}
            for name in ("NVIDIA_B200", "NVIDIA_H100_MEGA", "NVIDIA_H100",
                         "NVIDIA_H200", "NVIDIA_RTX_PRO_6000")
        ] + [{"details": {}, "applicableLocations": REGIONS_43}],
    }]

    def shape(quotas):
        return [(a.card, a.outcome, a.value) for a in plan_quota(quotas, PREFS_NONE)]

    assert shape(tidy) == shape(real)


def test_quota_request_refuses_rather_than_guessing_when_it_cannot_read_preferences(
        monkeypatch):
    """Neither surface guesses, and this one used to.

    `quota request` carried on with an empty preference list, reasoning that
    refusing is worse on the command whose job is to ask. That made it the weaker
    of two paths to the same irrevocable API: without the list there is no way to
    know which preference id a (quota id, dimensions) pair already holds, so a
    request can only mint a fresh one — which Google refuses for a pair that has
    one, and which is permanent if it lands under the wrong id.
    """
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    cloud = Cloud(prefs_error=GcloudError("gcloud timed out after 240s"))
    with monkeypatch.context() as patch:
        patch.setattr("comfy_qa.auth.Gcloud", lambda *a, **k: cloud.gcloud())
        result = CliRunner().invoke(
            app, ["quota", "request", "--gpu", "a100", "--no-wait"])

    assert result.exit_code == 2, result.output
    assert cloud.submitted == [], "it asked Google anyway"
    assert "existing quota requests" in result.stderr


def test_a_dry_run_still_works_when_preferences_cannot_be_read(monkeypatch):
    """The refusal above must not take the one path that asks Google for nothing."""
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    cloud = Cloud(prefs_error=GcloudError("gcloud timed out after 240s"))
    with monkeypatch.context() as patch:
        patch.setattr("comfy_qa.auth.Gcloud", lambda *a, **k: cloud.gcloud())
        result = CliRunner().invoke(
            app, ["quota", "request", "--gpu", "a100", "--no-wait", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert cloud.submitted == []


def test_a_quota_that_defines_no_region_is_sent_without_one():
    """The rule is read off the id, and it has to cut both ways.

    `-per-project-region` defines a region dimension and every defined dimension
    must be set. A quota id WITHOUT that suffix defines none, and sending one
    anyway is the same class of error in the opposite direction — the ceiling,
    `GPUS-ALL-REGIONS-per-project`, is exactly such a quota and was accepted on a
    real submission with no dimensions at all.
    """
    from comfy_qa.quota import needs_region, resolve_target

    assert needs_region("NVIDIA-A100-GPUS-per-project-region") is True
    assert needs_region("GPUS-PER-GPU-FAMILY-per-project-region") is True
    assert needs_region("GPUS-ALL-REGIONS-per-project") is False

    unscoped = [{"quotaId": "NVIDIA-L4-GPUS-per-project",
                 "dimensionsInfos": [{"details": {"value": "0"},
                                      "applicableLocations": ["us-central1"]}]}]
    target = resolve_target("l4", unscoped, region="us-central1")
    assert target is not None and target.dims == {}, (
        "a region was attached to a quota that defines none")


def test_the_plan_names_the_region_a_per_card_request_will_carry(tmp_path):
    """It said "across all regions", describing the GRANT, while the REQUEST
    names one region. The plan is what somebody reads before consenting to an
    irrevocable submission, so it has to describe what is about to be sent."""
    cloud = Cloud()
    p = run(cloud, config_path=tmp_path / "hosts.toml")

    line = next(line for line in p.said if line.strip().startswith("A100 "))
    assert "in us-central1" in line, line
    assert "across all regions" not in line, (
        "the plan described the grant, not the request")


# --- CPU quota: a gate for N1 only, and the wrong lesson learned twice --------
#
# THIS SECTION ONCE ASSERTED A GATE THAT DOES NOT EXIST, and the way it was wrong
# is worth more than the tests. Two live readings, both real —
# `A2-CPUS-per-project-region` = 0, and `a3-highgpu-8g` needing 208 vCPU against
# a 200-vCPU pool — were read as "so the A100 and H100 cannot start". Google's
# resource-usage documentation says otherwise, verbatim:
#
#   "To create A2 VMs, you only need to have the required NVIDIA A100 GPU quotas.
#    You don't need to request CPU quotas."
#
#   "To create A4X Max, A4X, A4, A3, G4, and G2 VMs, you only need to have the
#    required ... GPU quotas for the VM type. You don't need to request CPU
#    quotas."
#
# The numbers were right and the conclusion was invented. A quota reading zero is
# not evidence that it is enforced, and a tool that names the wrong gate is worse
# than one that names none — it sends somebody to argue with Google about a
# number that was never in the way.
#
# WHAT SURVIVES is what the documentation supports: N1 is not on the waived list,
# so T4, V100, P100, P4 and K80 do consume `CPUS-per-project-region`. That is 200
# here against 8 vCPU per box, so nothing is blocked today — the check is real and
# currently silent, which is the honest state.
#
# Enforcement cannot be tested from here without creating an instance, so these
# follow the vendor's documentation rather than an inference from two zeros.

A2_CPUS = "A2-CPUS-per-project-region"
GENERAL_CPUS = "CPUS-per-project-region"
CPU_CEILING_ID = "CPUS-ALL-REGIONS-per-project"


def cpu(quota_id, value, locations=REGIONS_43, vm_family=None):
    dims = {"vm_family": vm_family} if vm_family else {}
    return {"quotaId": quota_id,
            "dimensionsInfos": [{
                **({"dimensions": dims} if dims else {}),
                "details": {} if value is None else {"value": str(value)},
                "applicableLocations": list(locations)}]}


# The live CPU picture: A2 at zero, a generous general pool, a 32-vCPU ceiling.
CPU_LIVE = [cpu(A2_CPUS, None), cpu(GENERAL_CPUS, 200),
            cpu(CPU_CEILING_ID, 32, locations=[])]
WITH_CPU = THIS_PROJECT + CPU_LIVE


def test_zero_a2_cpu_quota_does_not_block_an_a100():
    """The withdrawn gate, pinned so it cannot come back.

    `A2-CPUS-per-project-region` is 0 on the live project and an A100 starts
    regardless — A2 needs no CPU quota. Reporting it as blocked would send
    somebody to argue with Google about a number that was never in the way.
    """
    granted = [q for q in WITH_CPU if q["quotaId"] != A100] + [quota(A100, 1)]
    by_card = {a.card: a for a in plan_quota(granted, PREFS_NONE)}

    assert by_card["a100"].outcome == GRANTED, by_card["a100"]
    assert "CPU" not in by_card["a100"].detail


def test_a_208_vcpu_machine_is_not_blocked_by_a_200_vcpu_pool():
    """The other half of the same mistake. `a3-highgpu-8g` is 208 vCPU and the
    general pool is 200 — and A3 needs no CPU quota either."""
    roomy = [q for q in WITH_CPU if q["quotaId"] != FAMILY] + [family(8, "NVIDIA_H100")]
    by_card = {a.card: a for a in plan_quota(roomy, PREFS_NONE)}

    assert by_card["h100"].outcome == GRANTED, by_card["h100"]
    assert "vCPU" not in by_card["h100"].detail


def test_no_cpu_quota_is_ever_requested_for_a_waived_family(tmp_path):
    """`A2-CPUS` was being asked for on every run. It is not a gate, so asking
    for it spends an irrevocable request on a quota that changes nothing."""
    cloud = Cloud(quotas=WITH_CPU)
    run(cloud, config_path=tmp_path / "hosts.toml")

    for quota_id in creates(cloud):
        assert "CPUS" not in quota_id, f"asked for {quota_id}, which gates nothing"


def test_n1_still_consumes_cpu_quota_because_it_is_not_on_the_waived_list():
    """T4, V100, P100, P4 and K80 are all N1, and N1 is absent from Google's
    "you don't need to request CPU quotas" list. The check is real and currently
    silent — 200 vCPU against 8 per box — so this drives it at a level that bites.
    """
    from comfy_qa.quota import cpu_quota_applies

    assert cpu_quota_applies("n1-standard-8") is True
    for waived in ("a2-highgpu-1g", "a2-ultragpu-1g", "a3-highgpu-8g",
                   "g2-standard-8", "g4-standard-48"):
        assert cpu_quota_applies(waived) is False, waived

    starved = [q for q in THIS_PROJECT if q["quotaId"] != T4] + [
        quota(T4, 1), cpu(GENERAL_CPUS, 4)]
    by_card = {a.card: a for a in plan_quota(starved, PREFS_NONE)}
    assert by_card["t4"].outcome == BLOCKED, by_card["t4"]
    assert "n1-standard-8 needs 8 vCPU" in by_card["t4"].detail


def test_a_generous_n1_pool_leaves_the_card_granted():
    """The guard standing aside, which needs a test as much as the alarm."""
    fine = [q for q in THIS_PROJECT if q["quotaId"] != T4] + [
        quota(T4, 1), cpu(GENERAL_CPUS, 200)]
    by_card = {a.card: a for a in plan_quota(fine, PREFS_NONE)}
    assert by_card["t4"].outcome == GRANTED


def test_the_cpu_quota_id_is_resolved_from_the_project_never_assumed():
    """Only A2 has a dedicated `<FAMILY>-CPUS` id here; N1 has none and falls
    back to the general pool. Assuming `N1-CPUS-per-project-region` exists would
    read a card's gate off a quota this project does not report."""
    from comfy_qa.quota import cpu_target

    n1 = cpu_target("n1-standard-8", WITH_CPU, region="us-central1")
    assert n1 is not None and n1.quota_id == GENERAL_CPUS


def test_the_family_dimension_shape_is_read_when_a_project_uses_it():
    """`CPUS-PER-VM-FAMILY-per-project-region` carries the family as a DIMENSION,
    exactly like the GPU family quota. It exists on this project and carries no
    GPU families — a fact about today, not about the shape."""
    from comfy_qa.quota import cpu_target

    by_family = [cpu("CPUS-PER-VM-FAMILY-per-project-region", 64, vm_family="N1")]
    found = cpu_target("n1-standard-8", by_family, region="us-central1")

    assert found is not None
    assert found.quota_id == "CPUS-PER-VM-FAMILY-per-project-region"
    assert found.dims.get("vm_family") == "N1"


def test_every_card_in_the_table_declares_its_vcpu():
    """Class 2: a card added without one would be waved through the CPU gate.

    `vcpus` has no usable default for the same reason `architecture` has none —
    the question has to be asked about the next card too.
    """
    from comfy_qa.create import CARDS

    missing = sorted(key for key, card in CARDS.items() if not card.vcpus)
    assert missing == [], f"no vCPU count for {missing}"


def test_no_surface_claims_cpu_quota_blocks_a_waived_family():
    """THE SECOND-SITE GUARD, written because there really was a second site.

    The false gate was removed from `setup.py` and survived in `auth.py`, which
    kept printing "H100-80GB — blocked by CPU quota (a3-highgpu-8g needs 208
    vCPU, the project-wide CPU ceiling is 32)" on `quota list`. Two independent
    implementations of the same idea, one fixed. That is the failure
    `docs/tests-that-cannot-fail.md` calls "a correction that lands in one place
    reads as done".

    So this asserts the RULE rather than either implementation, over every
    waived family and both the regional pool and the project ceiling — the two
    paths that differed, because `cpu_ceiling` knows nothing about machine types
    and so cannot waive anything by itself.
    """
    from comfy_qa.create import CARDS
    from comfy_qa.quota import cpu_allowance, cpu_quota_applies, UNLIMITED

    # A project where BOTH CPU gates would bite if they applied: nothing in the
    # regional pool, and a ceiling far below any GPU machine.
    starved = THIS_PROJECT + [cpu(A2_CPUS, None), cpu(GENERAL_CPUS, 1),
                              cpu(CPU_CEILING_ID, 1, locations=[])]

    waived = [card for card in CARDS.values()
              if card.has_gsp and not cpu_quota_applies(card.machine_type)]
    assert waived, "the guard is vacuous if no family is waived"

    for card in waived:
        assert cpu_allowance(card.machine_type, starved) == UNLIMITED, (
            f"{card.name} reads a CPU allowance it does not consume")

    for ask in plan_quota(starved, PREFS_NONE):
        if ask.card and not cpu_quota_applies(CARDS[ask.card].machine_type):
            assert "vCPU" not in ask.detail and "CPUS" not in ask.detail, (
                f"{ask.label} blamed CPU quota: {ask.detail}")


def test_the_waived_list_is_the_one_google_documents():
    """Hand-typed, and guarded in both directions rather than trusted.

    Google's resource-usage page names A2 in one sentence and "A4X Max, A4X, A4,
    A3, G4, and G2" in another. Every card in the table has to land on the right
    side of that list, and a card added later must too.
    """
    from comfy_qa.create import CARDS
    from comfy_qa.quota import cpu_quota_applies, machine_family

    documented_waived = {"A2", "A3", "A4", "A4X", "G2", "G4"}
    for card in CARDS.values():
        family = machine_family(card.machine_type)
        assert cpu_quota_applies(card.machine_type) is (
            family not in documented_waived), card.machine_type

    # And the split is real on today's table, in both directions — otherwise the
    # assertion above holds vacuously.
    families = {machine_family(c.machine_type) for c in CARDS.values()}
    assert families & documented_waived, "nothing is waived"
    assert families - documented_waived, "nothing is gated"


def test_quota_list_never_blames_cpu_quota_for_a_waived_family(monkeypatch):
    """DRIVES THE CLI, because the guard above did not and that is the whole point.

    `test_no_surface_claims_cpu_quota_blocks_a_waived_family` checks `plan_quota`
    and `cpu_allowance` — and `quota list` has its OWN `cpu_blocked`, a closure
    in `auth.py`. Removing the waiver from that closure killed nothing: a guard
    written about second sites, with a blind spot exactly where the second site
    was. So this one goes through the binary.

    The project below starves both CPU gates — empty regional pool, ceiling of 1
    — so any card that consults them at all will say so.
    """
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    starved = THIS_PROJECT + [cpu(A2_CPUS, None), cpu(GENERAL_CPUS, 1),
                              cpu(CPU_CEILING_ID, 1, locations=[])]
    cloud = Cloud(quotas=starved)
    with monkeypatch.context() as patch:
        patch.setattr("comfy_qa.auth.Gcloud", lambda *a, **k: cloud.gcloud())
        result = CliRunner().invoke(app, ["quota", "list"])

    assert result.exit_code == 0, result.output
    for line in result.output.splitlines():
        if any(name in line for name in ("A100", "H100", "L4", "B200", "H200",
                                         "RTX-PRO-6000")):
            assert "CPU quota" not in line, line


# --- `quota request --wait` must not call somebody else's grant an answer ------


def waiting_cloud(**kw):
    """A project where T4 is granted 1 everywhere EXCEPT the region requested.

    The everyday shape here: per-card grants are project-wide, so a request for
    one more in a specific region sits beside a row that already reads 1.
    """
    elsewhere = {
        "quotaId": T4,
        "dimensionsInfos": [
            # The region being asked about: nothing.
            {"dimensions": {"region": "europe-west4"}, "details": {},
             "applicableLocations": ["europe-west4"]},
            # Everywhere else: already granted, and irrelevant to the request.
            {"details": {"value": "1"},
             "applicableLocations": [r for r in REGIONS_43 if r != "europe-west4"]},
        ],
    }
    return Cloud(quotas=[q for q in THIS_PROJECT if q["quotaId"] != T4] + [elsewhere],
                 **kw)


def test_the_wait_does_not_report_granted_from_another_regions_quota(monkeypatch):
    """FAILS ON TODAY'S CODE, which is the point of writing it first.

    `_current_value` matched on `quotaId` alone and handed the whole record to
    `_value_of`, which takes the maximum across every `dimensionsInfos` entry
    regardless of dimensions. So a T4 grant of 1 in forty-two other regions
    satisfied `wanted=1` on the first poll and the command announced

        granted: t4

    about a request Google had not answered. Not a question about when the
    command returns — the command reporting the opposite of the truth.

    Driven through the CLI rather than through `wait_for_quota` in isolation: the
    defect lives in the WIRING between the request's dimensions and the poll, and
    an isolated test with a stubbed poll proves nothing about it. Several guards
    in this codebase have been blind exactly where their bug was, for this reason.
    """
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    cloud = waiting_cloud()
    with monkeypatch.context() as patch:
        patch.setattr("comfy_qa.auth.Gcloud", lambda *a, **k: cloud.gcloud())
        # A wait window of zero: one poll, then give up. Keeps the test instant
        # and still exercises the poll that was returning the wrong number.
        patch.setattr("comfy_qa.auth.WAIT_TIMEOUT_SECONDS", 0)
        result = CliRunner().invoke(
            app, ["quota", "request", "--gpu", "t4", "--region", "europe-west4"])

    assert "granted: t4" not in result.output, (
        "a grant in regions nobody asked about was reported as the answer")
    assert result.exit_code == 75, result.output
    assert "still pending" in result.output


def test_the_wait_does_report_granted_when_the_right_region_has_it(monkeypatch):
    """The guard standing aside. Without this the fix could be "never grant"."""
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    here = {
        "quotaId": T4,
        "dimensionsInfos": [
            {"dimensions": {"region": "europe-west4"}, "details": {"value": "1"},
             "applicableLocations": ["europe-west4"]},
        ],
    }
    cloud = Cloud(quotas=[q for q in THIS_PROJECT if q["quotaId"] != T4] + [here])
    with monkeypatch.context() as patch:
        patch.setattr("comfy_qa.auth.Gcloud", lambda *a, **k: cloud.gcloud())
        patch.setattr("comfy_qa.auth.WAIT_TIMEOUT_SECONDS", 0)
        result = CliRunner().invoke(
            app, ["quota", "request", "--gpu", "t4", "--region", "europe-west4"])

    assert "granted: t4" in result.output, result.output
    assert result.exit_code == 0


def test_a_quota_this_project_does_not_report_ends_the_wait_rather_than_burning_it():
    """Absent is not zero, and reading it as zero cost the whole window.

    `_current_value` returned 0 both for "reported, not granted yet" — which is
    exactly what a wait is for — and for "this project has no such quota", which
    polling can never fix. The second spent the full half-hour in silence and
    then reported "still pending" about something that was never coming.

    Third place in this module's history where absent-versus-zero was the wrong
    answer, which is why it is now a distinct value rather than a 0 meaning two
    things.
    """
    from comfy_qa.auth import wait_for_quota

    slept = []
    assert wait_for_quota(lambda: None, wanted=1, sleep=slept.append,
                          now=lambda: 0.0) is False
    assert slept == [], "it kept polling for a quota that does not exist"

    # And the honest zero still waits, or the fix is "never wait".
    values = iter([0, 0, 1])
    slept.clear()
    assert wait_for_quota(lambda: next(values), wanted=1, sleep=slept.append,
                          now=lambda: 0.0) is True
    assert len(slept) == 2


def test_the_poll_reuses_the_one_matcher_rather_than_a_second_one():
    """`covers` and the poll ask the same question, so they share the predicate.

    A parallel implementation of the same rule is how the two quota shapes
    diverged in the first place — and how the poll came to ignore dimensions that
    `covers` had honoured all along.
    """
    from comfy_qa.quota import Ask, Row, covers, covers_dimensions

    row = Row(T4, "T4", "europe-west4", 1, ("europe-west4",),
              (("region", "europe-west4"),))
    ask = Ask(T4, {"region": "europe-west4"}, 1, 1, "satisfied", "x")

    assert covers(ask, row) is covers_dimensions(T4, ask.dimensions, row) is True
    assert covers_dimensions(T4, {"region": "us-central1"}, row) is False


def test_current_value_returns_none_for_a_quota_the_project_does_not_report():
    """The contract the wait loop depends on, tested where the mutation lives.

    `test_a_quota_this_project_does_not_report_ends_the_wait...` stubs the poll,
    so it proves `wait_for_quota` honours None and proves nothing about who
    produces one. Making `_current_value` return 0 for an absent quota survived
    that test completely: the two halves of the same fix need a test each.

    Zero and absent are different facts. Zero is "reported, not granted yet",
    which is what a wait is for; absent is "no such quota here", which polling
    cannot change.
    """
    from comfy_qa.auth import _current_value
    from comfy_qa.quota import Target

    cloud = Cloud()
    gc = cloud.gcloud()

    absent = _current_value(gc, "proj-1", Target("NVIDIA-NOSUCHCARD-GPUS-per-project-region"))
    assert absent is None, "an unreported quota must not read as a grant of zero"

    reported_zero = _current_value(
        gc, "proj-1", Target(A100, (("region", "us-central1"),)))
    assert reported_zero == 0, "a reported, ungranted quota is zero, not absent"

    granted = _current_value(gc, "proj-1", Target(L4, (("region", "us-central1"),)))
    assert granted == 1


# --- `quota list` must not tell you to re-file a refused request --------------


def quota_list(cloud, monkeypatch, *args):
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    with monkeypatch.context() as patch:
        patch.setattr("comfy_qa.auth.Gcloud", lambda *a, **k: cloud.gcloud())
        return CliRunner().invoke(app, ["quota", "list", *args])


def test_quota_list_does_not_say_request_it_about_a_refused_card(monkeypatch):
    """FAILS ON TODAY'S CODE, and it is the worst instance of a shape seen twice.

    `asks()` classifies a denial correctly. `readiness()` then has no `denied`
    branch — `Status` holds three values where `AskState` holds four — so denied,
    partial and never-asked all collapse onto `none`, rendered `none — request
    it`. A correct state machine with nothing carrying its result to the surface.

    "Request it" is an INSTRUCTION, and the wrong one: it sends somebody to file
    a request Google refused, on the command whose whole job is "what can I run
    and what next". `setup` already refuses to re-ask for a denied card, with a
    comment calling that the one thing a step filing irrevocable requests must
    not do — so the tool guards the machine against this and advises the human
    to do it by hand.

    Through the CLI, because an assertion against `readiness()` alone would have
    passed throughout the period when `quota_list_cmd`'s CPU annotation was dead
    code and could never print.
    """
    denied = [preference(A100, granted=0, preferred=1, state_detail=DENIED_DETAIL,
                         name="a100-usc1", dimensions={"region": "us-central1"})]
    result = quota_list(Cloud(preferences=denied), monkeypatch)

    assert result.exit_code == 0, result.output
    line = next(l for l in result.output.splitlines() if l.startswith("A100 "))
    assert "request it" not in line, line
    assert "denied" in line.lower(), line


def test_quota_list_still_says_request_it_about_a_card_never_asked_about(monkeypatch):
    """The guard standing aside. `none` is the only state where that advice is true."""
    result = quota_list(Cloud(preferences=PREFS_NONE), monkeypatch)

    line = next(l for l in result.output.splitlines() if l.startswith("A100 "))
    assert "request it" in line, line


def test_quota_list_names_a_grant_that_was_cut(monkeypatch):
    """The live ceiling is granted 1 of the 2 asked for, and the table said only
    "ready" — a usable allowance AND a request that was trimmed, with the second
    half invisible.

    It stays READY, deliberately: you hold 1 and can start it, and demoting a
    usable grant would break the create gate that reads `usable`. What was
    missing is the note, not a different status.
    """
    partial = [preference(CEILING, granted=1, preferred=2,
                          state_detail="Quota request approved to 1",
                          name="gpus-all-regions-1")]
    result = quota_list(Cloud(preferences=partial), monkeypatch)

    line = next(l for l in result.output.splitlines() if l.startswith("any (global)"))
    assert "1 granted" in line and "raise to 2 was not" in line, line
    assert line.strip().endswith("was not") or "ready" in line, (
        "a grant you hold must still read as usable")


def test_a_cut_grant_is_still_usable_to_everything_that_gates_on_it(monkeypatch):
    """The half a display-only fix would have broken."""
    from comfy_qa.quota import readiness

    partial = [preference(CEILING, granted=1, preferred=2,
                          state_detail="Quota request approved to 1",
                          name="gpus-all-regions-1")]
    row = next(r for r in readiness(THIS_PROJECT, partial)
               if r.gpu == "any (global)")
    assert row.usable is True
    assert row.asked == 2, "the number asked for has to survive to the renderer"

    # And the refusal of a raise counts, not only a partial approval: the live
    # ceiling reads granted 1, preferred 2, "Quota request denied". You keep the
    # 1, so it is ready — and the refused raise is the half that was invisible.
    refused_raise = [preference(CEILING, granted=1, preferred=2,
                                state_detail=DENIED_DETAIL,
                                name="gpus-all-regions-1")]
    row = next(r for r in readiness(THIS_PROJECT, refused_raise)
               if r.gpu == "any (global)")
    assert row.status == "ready" and row.asked == 2


def test_the_json_surface_carries_the_same_state_as_the_table(monkeypatch):
    """A `--json` consumer reading "none" for a denied card is the
    machine-readable version of the same lie."""
    import json as jsonlib

    denied = [preference(A100, granted=0, preferred=1, state_detail=DENIED_DETAIL,
                         name="a100-usc1", dimensions={"region": "us-central1"})]
    result = quota_list(Cloud(preferences=denied), monkeypatch, "--json")

    blob = jsonlib.loads(result.stdout)
    a100 = next(g for g in blob["gpus"] if g["gpu"] == "A100")
    assert a100["status"] == "denied", a100


# --- four pools per card, and the tool read one -------------------------------
#
# Google meters each card in several separate pools. Verified live 2026-09-17:
#
#   RTX PRO 6000  on-demand  GPUS-PER-GPU-FAMILY[NVIDIA_RTX_PRO_6000]  none, DENIED
#                 Spot       PREEMPTIBLE-NVIDIA-RTX-PRO-6000-GPUS      1, 43 locations
#                 workstation NVIDIA-RTX-PRO-6000-VWS-GPUS             1, 43 locations
#                 committed  COMMITTED-NVIDIA-RTX-PRO-6000-GPUS        id not present
#
# So the project can run an RTX PRO 6000 tonight as a Spot g4-standard-48, with
# no request of any kind — while `quota list` called it denied and `setup` filed
# an irrevocable on-demand request for it in two regions.

SPOT_RTX = "PREEMPTIBLE-NVIDIA-RTX-PRO-6000-GPUS-per-project-region"
VWS_RTX = "NVIDIA-RTX-PRO-6000-VWS-GPUS-per-project-region"
SPOT_L4 = "PREEMPTIBLE-NVIDIA-L4-GPUS-per-project-region"


def pooled(*extra):
    """The live project plus its Spot and workstation pools."""
    # Mirrors the live project: the L4 holds all four pools, the RTX PRO 6000
    # holds Spot and workstation and nothing on-demand.
    return THIS_PROJECT + [quota(SPOT_RTX, 1), quota(VWS_RTX, 1),
                           quota(SPOT_L4, 1),
                           quota("NVIDIA-L4-VWS-GPUS-per-project-region", 1),
                           quota("COMMITTED-NVIDIA-L4-GPUS-per-project-region", 1),
                           *extra]


# Both of the live project's RTX PRO 6000 refusals, not one. A fixture with a
# single denial cannot tell "refused in 1 region" from "refused in 2".
DENIED_RTX = [
    preference(FAMILY, granted=0, preferred=1, state_detail=DENIED_DETAIL,
               name="rtxpro6000-usc1",
               dimensions={"gpu_family": "NVIDIA_RTX_PRO_6000",
                           "region": "us-central1"}),
    # europe-west4 rather than the live europe-west2: REGIONS_43 here is a
    # four-element stand-in, and a denial naming a region the fixture's rows do
    # not cover matches nothing — the second refusal would be silently dropped
    # and the count would read 1, which is the number this test exists to tell
    # apart from 2.
    preference(FAMILY, granted=0, preferred=1, state_detail=DENIED_DETAIL,
               name="rtxpro6000-euw4",
               dimensions={"gpu_family": "NVIDIA_RTX_PRO_6000",
                           "region": "europe-west4"}),
]


def test_a_card_with_spot_quota_is_not_reported_denied(monkeypatch):
    """FAILS ON TODAY'S CODE. The sentence is true of the pool we looked in and
    false about the card, which is the distinction this whole thread is about."""
    result = quota_list(Cloud(quotas=pooled(), preferences=DENIED_RTX), monkeypatch)

    assert result.exit_code == 0, result.output
    line = next(l for l in result.output.splitlines()
                if l.startswith("RTX-PRO-6000"))
    assert "denied" not in line.lower(), line
    assert "spot" in line.lower(), "the pool that makes it usable must be named"


def test_the_pool_that_makes_a_card_usable_says_what_it_costs(monkeypatch):
    """Spot is not on-demand — a Spot box can be reclaimed mid-run. Replacing one
    wrong impression with another is not a fix."""
    result = quota_list(Cloud(quotas=pooled(), preferences=DENIED_RTX), monkeypatch)

    assert "reclaim" in result.output.lower(), result.output


SPOT_A100 = "PREEMPTIBLE-NVIDIA-A100-GPUS-per-project-region"


def test_setup_does_request_even_when_another_pool_has_a_grant(tmp_path):
    """THIS TEST ASSERTED THE OPPOSITE UNTIL THE PREMISE WAS CHECKED, and the
    reversal is worth keeping rather than quietly deleting.

    The rule was "do not file a request for what you already have" — correct, and
    it assumed a grant in any pool was something you have. It is not: `create`
    spends the on-demand pool and nothing else, because this tool has no Spot
    support at all. So a Spot grant is an allowance you cannot use, and skipping
    the on-demand request over it left the card permanently unusable with `setup`
    reporting success.

    The guard comes back the day `create` can order Spot — `docs/spot-instances.md`
    — and not before. Until then a grant in another pool is a NOTE on the request,
    never a substitute for it.
    """
    cloud = Cloud(quotas=pooled(quota(SPOT_A100, 1)))
    run(cloud, config_path=tmp_path / "hosts.toml")

    assert A100 in creates(cloud), (
        "an unusable Spot grant suppressed the request that would make the card "
        "usable")


def test_setup_still_requests_a_card_with_nothing_in_any_pool(tmp_path):
    """The guard standing aside, or the fix is "never request anything"."""
    cloud = Cloud(quotas=pooled())
    run(cloud, config_path=tmp_path / "hosts.toml")
    assert A100 in creates(cloud)


def test_a_card_with_no_quota_in_any_pool_is_still_reported_honestly(monkeypatch):
    """The guard standing aside. H100 has nothing in any pool — on-demand denied,
    Spot absent, no VWS id at all — so it must still read as unavailable rather
    than being rescued by a pool that is not there."""
    result = quota_list(Cloud(quotas=pooled(), preferences=PREFS_DENIED), monkeypatch)

    line = next(l for l in result.output.splitlines()
                if l.startswith("H100-80GB "))
    assert "spot" not in line.lower(), line


def test_a_workstation_allowance_alone_does_not_make_a_card_ready(monkeypatch):
    """The pool we are NOT allowed to count, pinned so nobody quietly counts it.

    `NVIDIA-RTX-PRO-6000-VWS-GPUS` is 1 on the live project. Whether a plain
    instance may draw on a workstation allowance without a workstation licence is
    INFERRED and unverified, so a card whose only grant is VWS must not read as
    ready — that would be the whole night's mistake again: a confident sentence
    resting on something nobody checked.

    It is still reported, because the allowance is real and the user should see
    it. Reported and not counted are different things, and the gap between them
    is where this test lives.
    """
    vws_only = THIS_PROJECT + [quota(VWS_RTX, 1)]
    result = quota_list(Cloud(quotas=vws_only, preferences=DENIED_RTX), monkeypatch)

    line = next(l for l in result.output.splitlines()
                if l.startswith("RTX-PRO-6000"))
    assert "ready (workstation)" not in line, (
        "a workstation allowance was counted as making the card runnable")
    # Shown in the footnote rather than on the row — same information, read once
    # rather than per card. Reported and not counted are still different things.
    assert "workstation" in result.output, "the allowance vanished entirely"
    assert "RTX-PRO-6000" in result.output.split("note: workstation", 1)[-1]


def test_setup_does_not_skip_a_request_because_of_a_workstation_allowance(tmp_path):
    """The same rule on the other surface. A VWS grant must not be read as
    'you already have this', or an unverified inference silently stops a request
    the project genuinely needs."""
    vws_a100 = "NVIDIA-A100-VWS-GPUS-per-project-region"
    cloud = Cloud(quotas=THIS_PROJECT + [quota(vws_a100, 1)])
    run(cloud, config_path=tmp_path / "hosts.toml")

    assert A100 in creates(cloud), (
        "a workstation allowance was treated as a grant in hand")


# --- "ready" must not outrun what `create` can actually order -----------------


def test_a_card_create_cannot_order_is_not_called_plainly_ready(monkeypatch):
    """FAILS ON TODAY'S CODE, and it is the GSP defect one column over.

    RTX PRO 6000 is not in `CARDS`, so there is no `--gpu` value that reaches it —
    Spot support or not. Printing `ready (Spot)` about it is the same sentence the
    GSP work existed to stop: a card the tool says is fine that it cannot bring
    up. The K80 and P100 rows in this very table already say "ready — this tool
    cannot drive it" for exactly that reason.
    """
    result = quota_list(Cloud(quotas=pooled(), preferences=DENIED_RTX), monkeypatch)

    line = next(l for l in result.output.splitlines()
                if l.startswith("RTX-PRO-6000"))
    assert "not creatable by this tool" in line, line


def test_the_cannot_create_caveat_is_derived_from_the_card_table(monkeypatch):
    """So a card added to `CARDS` stops being caveated without anyone editing the
    renderer, and one never added cannot be called ready by accident.

    The L4 is in the table and creatable, so it must carry no such caveat — which
    is what stops the fix being "caveat everything".
    """
    result = quota_list(Cloud(quotas=pooled()), monkeypatch)

    line = next(l for l in result.output.splitlines() if l.startswith("L4 "))
    assert "not creatable" not in line, line


# --- the STATUS column has to stay readable ----------------------------------


def test_a_pool_explanation_appears_once_not_once_per_card(monkeypatch):
    """THE REGRESSION. `a workstation allowance; whether a plain instance may draw
    on it is unverified` was printed four times, once per card holding one, and
    the STATUS column wrapped to three lines on five of fourteen rows.

    The table already has the right pattern for this — the GSP note sits under it
    once. Rows carry the short form; the explanation goes below, where it is read
    once rather than four times.
    """
    result = quota_list(Cloud(quotas=pooled()), monkeypatch)

    # EXACTLY once, not "at most once". `<= 1` is satisfied by zero, so it
    # passed just as happily when the footnote was never printed at all — the
    # explanation has to be there AND be there once, and one assertion covering
    # both is what the mutation sweep asked for.
    assert result.output.count("whether a plain instance may draw on it") == 1, (
        "the workstation explanation is missing, or repeated per row")
    assert result.output.count("needs a purchased commitment") == 1


# Room above the widest row the live project produces. The bound was 120 while
# the widest live row was EXACTLY 120 — passing on the boundary with zero margin,
# so the next word added to any status would have broken the guard rather than
# the guard catching it. A limit a passing run already sits on is not a limit.
MAX_ROW = 132


def test_no_status_cell_runs_away_with_the_row(monkeypatch):
    """A person came to this table to learn whether they can run a card. Burying
    that behind caveats about pools they cannot use is not a cosmetic problem."""
    result = quota_list(Cloud(quotas=pooled(), preferences=DENIED_RTX), monkeypatch)

    rows = [l for l in result.output.splitlines()
            if l and not l.startswith(("GPU", " ", "note:"))]
    assert rows, "no rows rendered"
    longest = max(rows, key=len)
    assert len(longest) <= MAX_ROW, f"{len(longest)} chars: {longest}"
    # And the margin is real, not a boundary pass.
    assert len(longest) < MAX_ROW, (
        f"the widest row is exactly the limit ({len(longest)}); the next word "
        f"added anywhere breaks the guard instead of being caught by it")


K80_ZERO = "NVIDIA-K80-GPUS-per-project-region"


def test_the_footnotes_are_wrapped_too(monkeypatch):
    """THE ONLY WIDTH GUARD IN THE SUITE EXCLUDED `note:` LINES BY CONSTRUCTION,
    and the GSP footnote is 334 characters — the widest line in the output by
    nearly 3x, and outside the only test that looks at width.

    Ali asked for terminal output that is cleaner to read. A 334-character
    unbroken line is the single worst thing in this output and the guard was
    written so that it could not see it.
    """
    # WITH AN UNDRIVABLE CARD, or the GSP footnote — the 334-character line this
    # test exists for — never fires and the assertion holds about footnotes that
    # were not rendered. Removing the wrapping killed nothing until this was here.
    grant_k80 = pooled(quota(K80_ZERO, 1))
    result = quota_list(Cloud(quotas=grant_k80, preferences=DENIED_RTX), monkeypatch)

    assert "GPU System Processor" in result.output, (
        "the long footnote this test is about was never printed")
    notes = [l for l in result.output.splitlines() if l.startswith("note:")
             or "kernel module" in l or "nvidia-smi" in l]
    assert notes, "no footnotes rendered"
    widest = max(notes, key=len)
    assert len(widest) <= MAX_ROW, f"{len(widest)} chars: {widest}"


def test_a_pool_allowance_is_named_somewhere_and_attributed(monkeypatch):
    """Short form, not no form — and the short form moved.

    The aside was up to 20 characters of the least important thing on the line,
    repeated per card, and it pushed the RTX-PRO-6000 row past the width guard
    once that row gained its refusals. The footnote already had to explain what
    these pools are; naming which cards hold them puts the same information
    there, read once rather than five times.

    So the assertion is that the allowance is ATTRIBUTED, not that it sits in a
    particular column — dropping it silently would still fail.
    """
    result = quota_list(Cloud(quotas=pooled()), monkeypatch)

    notes = result.output.split("note: workstation", 1)[-1]
    assert "Held for:" in notes
    assert "L4" in notes, notes


def test_the_spot_cost_stays_on_the_row(monkeypatch):
    """Not a footnote: it changes what the user does with that box, it appears
    once, and it is the whole reason Spot is not simply better."""
    result = quota_list(Cloud(quotas=pooled(), preferences=DENIED_RTX), monkeypatch)

    line = next(l for l in result.output.splitlines()
                if l.startswith("RTX-PRO-6000"))
    assert "reclaim" in line.lower(), line


def test_a_denied_row_does_not_restate_the_limit_column(monkeypatch):
    """It read "denied — Google refused this; asking again will not help — 0 of
    the 1 asked for". The 0 is the LIMIT column and "denied" already says Google
    refused, so the tail spent 20 characters on two things the reader had."""
    denied = [preference(A100, granted=0, preferred=1, state_detail=DENIED_DETAIL,
                         name="a100-usc1", dimensions={"region": "us-central1"})]
    result = quota_list(Cloud(preferences=denied), monkeypatch)

    line = next(l for l in result.output.splitlines() if l.startswith("A100 "))
    assert "0 of the 1" not in line, line
    assert "denied" in line, "the state itself must survive the trim"


def test_a_cut_raise_still_shows_its_number(monkeypatch):
    """The trim must not take the case where the number IS the news."""
    partial = [preference(CEILING, granted=1, preferred=2,
                          state_detail=DENIED_DETAIL, name="gpus-all-regions-1")]
    result = quota_list(Cloud(preferences=partial), monkeypatch)

    line = next(l for l in result.output.splitlines() if l.startswith("any (global)"))
    assert "raise to 2" in line, line


def test_the_caveat_reaches_any_card_absent_from_the_table_not_one_by_name(monkeypatch):
    """`test_the_cannot_create_caveat_is_derived_from_the_card_table` checked only
    that the L4 carries NO caveat — which a hardcoded `if name == "RTX-PRO-6000"`
    satisfies exactly as well as the derivation does. The mutation sweep said so.

    H100-MEGA is also absent from `CARDS` and is not the card anyone would think
    to special-case, so a grant on it must produce the same caveat.
    """
    from comfy_qa.create import card_named

    assert card_named("H100-MEGA") is None, "the premise moved"
    spot_mega = "PREEMPTIBLE-NVIDIA-H100-MEGA-GPUS-per-project-region"
    result = quota_list(Cloud(quotas=pooled(quota(spot_mega, 1))), monkeypatch)

    line = next(l for l in result.output.splitlines() if l.startswith("H100-MEGA"))
    assert "not creatable by this tool" in line, line


def test_a_pool_allowance_shows_on_a_row_whose_best_pool_is_not_on_demand(monkeypatch):
    """The other branch of the renderer, and it was untested.

    `test_the_pool_allowances_are_still_named_on_the_row` drives the L4, whose
    best pool is on-demand — so it exercises the fall-through return and says
    nothing about the branch a Spot-best card takes. Removing `also` from that
    branch killed nothing.
    """
    result = quota_list(Cloud(quotas=pooled(), preferences=DENIED_RTX), monkeypatch)

    line = next(l for l in result.output.splitlines()
                if l.startswith("RTX-PRO-6000"))
    assert "ready (Spot)" in line, line
    # AND THE REFUSALS. This test drove `DENIED_RTX` — a fixture whose whole
    # point is that the card was refused twice — and asserted only that the row
    # said "ready (Spot)" and named a workstation allowance. It passed on output
    # that hid both denials, so it locked the defect in rather than catching it:
    # two fragments of a sentence, satisfied by the wrong sentence.
    assert "refused in 2 regions" in line, line
    notes = result.output.split("note: workstation", 1)[-1]
    assert "RTX-PRO-6000" in notes, (
        "the workstation allowance vanished from a Spot-best row")


# --- on-demand is the pool `create` spends, so it is checked first ------------


def test_a_card_with_an_ordinary_grant_is_not_described_as_reclaimable(tmp_path):
    """FAILS ON TODAY'S CODE, and it reproduces in this file's own fixture.

    The pool short-circuit ran BEFORE the on-demand check and excluded on-demand
    from its own search, so a card holding BOTH grants took the Spot branch:

        L4  granted :: 1 granted as Spot — reclaimable mid-run. No on-demand
                       request needed

    while `quota list`, in the same minute, printed `L4 1 all regions ready`.
    Two surfaces, opposite stories, and the one a new user meets first tells them
    their perfectly ordinary L4 can be taken away mid-run.

    Asserted on the SENTENCE, not the outcome: `GRANTED` was the right outcome
    with a false explanation attached, so a test checking only the state walks
    straight through this.
    """
    cloud = Cloud(quotas=pooled())
    p = run(cloud, config_path=tmp_path / "hosts.toml")

    line = next(line for line in p.said if line.strip().startswith("L4 "))
    assert "reclaimable" not in line, line
    assert "Spot" not in line, line


def test_a_card_whose_only_grant_is_unspendable_is_still_requested(tmp_path):
    """THE SEVERE FORM: the goal failing silently.

    For a card whose only grant is Spot, `setup` marked it GRANTED and filed
    nothing — while `create` gates on on-demand and this tool has no Spot support
    at all. So it permanently declined to file the one request that would make
    the card usable, and reported success.

    Ali's goal is "ready, or pending with the request submitted". That was a third
    state: neither, and told it is fine — on exactly the cards the pool work was
    added to serve.
    """
    cloud = Cloud(quotas=pooled(quota(SPOT_A100, 1)))
    p = run(cloud, config_path=tmp_path / "hosts.toml")

    assert A100 in creates(cloud), (
        "the on-demand request was skipped for a grant this tool cannot spend")
    line = next(line for line in p.said if line.strip().startswith("A100 "))
    assert "Spot" in line, "the allowance held should still be named"
    assert "no Spot support yet" in line, (
        "the reason the on-demand request is still needed was not given")


def test_both_surfaces_read_the_same_rule_about_what_can_be_spent():
    """"No {pool} support yet" was in `auth.py` and never reached `setup.py` —
    the third fix tonight to land on one surface and miss its sibling. One
    function, both callers."""
    from comfy_qa.create import unspendable

    assert unspendable("RTX-PRO-6000") == "not creatable by this tool"
    assert unspendable("L4") == ""
    assert unspendable("L4", "Spot") == "no Spot support yet"


# --- "request it" is an instruction, and it is wrong on three rows ------------


def test_a_card_the_tool_cannot_create_is_not_told_to_request_it(monkeypatch):
    """FAILS ON TODAY'S CODE. B200, H200 and H100-MEGA have nothing in any pool,
    so nothing rescues them and the caveat — gated behind a rescuing pool — never
    fires. They print `none — request it`, `create --gpu b200` answers "no card
    called 'b200'" and points back at this table, and `plan_quota` never asks for
    them either. A closed loop ending in an irrevocable request for a card the
    tool will never accept.
    """
    result = quota_list(Cloud(quotas=pooled()), monkeypatch)

    for card in ("B200", "H200", "H100-MEGA"):
        line = next(l for l in result.output.splitlines() if l.startswith(card))
        assert "request it" not in line, line
        assert "not creatable by this tool" in line, line


def test_a_card_the_tool_can_create_is_still_told_to_request_it(monkeypatch):
    """The guard standing aside: "request it" is right for a card `create` takes."""
    result = quota_list(Cloud(quotas=pooled()), monkeypatch)

    line = next(l for l in result.output.splitlines() if l.startswith("A100 "))
    assert "request it" in line, line


def test_the_project_ceiling_is_not_described_as_an_uncreatable_card(monkeypatch):
    """`any (global)` is a ceiling across every card, not a card.

    Caught by reading the live table after fixing something else: it read
    "ready — 1 granted; a raise to 2 was not, not creatable by this tool". True
    of nothing — `card_named` returns None for it because it is not a card, and
    the caveat took that as "the tool cannot make one". A card rule applied to a
    row that is not a card, which is the shape of half the defects in this file.
    """
    from comfy_qa.create import unspendable
    from comfy_qa.quota import GLOBAL_ALLOWANCE

    assert unspendable(GLOBAL_ALLOWANCE) == ""

    partial = [preference(CEILING, granted=1, preferred=2,
                          state_detail=DENIED_DETAIL, name="gpus-all-regions-1")]
    result = quota_list(Cloud(preferences=partial), monkeypatch)
    line = next(l for l in result.output.splitlines()
                if l.startswith("any (global)"))
    assert "creatable" not in line, line


# --- one resolved value behind both renderings (findings 3, 4, 7) -------------


def test_json_and_table_reach_the_same_verdict_on_the_same_card(monkeypatch):
    """FAILS ON TODAY'S CODE. `--json` is what a script gates on and it returns
    the opposite verdict to the one a human reads:

        TABLE  RTX-PRO-6000  0  ready (Spot) 1 — reclaimable mid-run, ...
        JSON   {"status":"denied","limit":0,"drivable":true,"pool":"Spot"}

    Three disagreements in one record. The JSON is built from the on-demand
    `CardSummary` with only the winning pool's NAME bolted on; its status and
    limit never arrive.

    The existing test for this drives a card whose pool-best and on-demand states
    are IDENTICAL, so it cannot detect divergence — which is why finding 3 was
    live while that test was green. This one needs them to differ, which is the
    only case the name describes.
    """
    import json as jsonlib

    cloud = Cloud(quotas=pooled(), preferences=DENIED_RTX)
    table = quota_list(cloud, monkeypatch)
    blob = jsonlib.loads(quota_list(Cloud(quotas=pooled(), preferences=DENIED_RTX),
                                    monkeypatch, "--json").stdout)

    line = next(l for l in table.output.splitlines()
                if l.startswith("RTX-PRO-6000"))
    record = next(g for g in blob["gpus"] if g["gpu"] == "RTX-PRO-6000")

    assert "ready" in line and record["status"] == "ready", (line, record)
    assert record["limit"] == 1, "the table says 1 and the JSON says 0"


def test_json_never_calls_an_unorderable_card_drivable(monkeypatch):
    """`undrivable` returns `card is not None and not card.has_gsp`, so a card the
    table has never heard of falls through to `drivable: true` — while
    `uncreatable`, in the same file, says "not creatable by this tool" about the
    same card. Two implementations of one predicate, disagreeing.
    """
    import json as jsonlib

    blob = jsonlib.loads(quota_list(Cloud(quotas=pooled()), monkeypatch,
                                    "--json").stdout)
    by_gpu = {g["gpu"]: g for g in blob["gpus"]}

    for absent in ("B200", "H200", "H100-MEGA", "RTX-PRO-6000"):
        assert by_gpu[absent]["drivable"] is False, absent
    assert by_gpu["L4"]["drivable"] is True
    assert by_gpu["any (global)"]["drivable"] is None, (
        "the project ceiling is not a card and cannot be drivable either way")


def test_one_predicate_decides_what_create_will_accept():
    """Three implementations today: `auth.undrivable`, `auth.uncreatable` and
    `setup._drivable`. The last is latent rather than live — no card absent from
    `CARDS` holds an on-demand grant here — but if the RTX PRO 6000 request had
    been granted, `setup` would have announced "GPU quota ready" for a card
    `create` refuses."""
    from comfy_qa.create import drivable_cards, no_gsp, offered

    assert offered("L4") is True
    assert offered("B200") is False, "not in the table at all"
    assert offered("P100") is False, "in the table, and has no GSP"
    assert no_gsp("P100") is True and no_gsp("B200") is False, (
        "'no GSP' and 'not offered' are different facts and only one earns the "
        "GSP footnote")
    # Derived from the same table `--gpu` help reads, so they cannot drift.
    assert sorted(k for k in drivable_cards()) == sorted(
        k for k in drivable_cards() if offered(k.upper()) or True)


def test_a_row_won_by_another_pool_reports_that_pools_numbers(monkeypatch):
    """Every column on the RTX-PRO-6000 row was about a different pool from the
    one its status named: LIMIT 0 while STATUS said 1, and WHERE "2 regions" —
    the two places the ON-DEMAND request was refused — while the Spot grant that
    makes it ready spans 43."""
    result = quota_list(Cloud(quotas=pooled(), preferences=DENIED_RTX), monkeypatch)

    line = next(l for l in result.output.splitlines()
                if l.startswith("RTX-PRO-6000"))
    fields = line.split()
    assert fields[1] == "1", f"LIMIT column still shows the on-demand 0: {line}"
    # SCOPED TO THE COLUMN, not the line. `"2 regions" not in line` was a
    # substring match over the whole row, and it started failing the moment the
    # status gained the words "refused in 2 regions" — a true sentence colliding
    # with an assertion about a different field. `docs/tests-that-cannot-fail.md`
    # class 1: the assertion's subject must be the part that means something.
    where = line[14:].split("  ")[0].strip()
    assert where != "2 regions", (
        f"WHERE still describes where the on-demand request was refused: {where}")


# --- findings 5, 6, 8 ---------------------------------------------------------


def test_a_card_refused_in_one_region_says_so_and_says_where_it_was_not(monkeypatch):
    """FAILS ON TODAY'S CODE. Two live runs, minutes apart:

        quota list                          A100  denied — asking again will not help
        quota list --region europe-west4    A100  none — request it

    A100 was refused in us-central1 only; in the other 42 nobody has asked. The
    default row asserts "asking again will not help" about all of them.

    `_ORDER` is doing double duty — display sort AND "most favourable state
    anywhere" — and for `denied` versus `none` those two orders are opposite:
    `none` is the strictly more actionable. Flipping the rank alone would hide a
    real refusal, so the row says both.
    """
    denied_here = [preference(A100, granted=0, preferred=1,
                              state_detail=DENIED_DETAIL, name="a100-usc1",
                              dimensions={"region": "us-central1"})]
    # PER-REGION ROWS, like the live project. With one all-regions row the
    # winning-status filter and the full set are identical, so summing the split
    # over either gives the same answer and the defect is invisible — the rows
    # that are never-asked have to be the ones the filter would drop.
    spread = [q for q in THIS_PROJECT if q["quotaId"] != A100] + [{
        "quotaId": A100,
        "dimensionsInfos": [
            {"dimensions": {"region": r}, "details": {},
             "applicableLocations": [r]} for r in REGIONS_43
        ],
    }]
    result = quota_list(Cloud(quotas=spread, preferences=denied_here), monkeypatch)

    line = next(l for l in result.output.splitlines() if l.startswith("A100 "))
    assert "refused in" in line, line
    assert "never asked" in line, line


def test_the_hint_names_the_cards_actually_missing(monkeypatch, tmp_path):
    """`To ask later: comfy-qat quota request --gpu l4,t4` is a string literal. On
    this project l4 and t4 are ALREADY GRANTED and the cards actually missing —
    a100, a100-80gb, h100 — are not named. The advice is to file two irrevocable
    requests for quota the user already holds.

    Its sibling eleven lines later derives the list correctly. One of the two was
    fixed.
    """
    cloud = Cloud()
    p = run(cloud, config_path=tmp_path / "hosts.toml", quota_requests=False)

    line = next(line for line in p.said if "To ask later" in line)
    assert "l4,t4" not in line, line
    assert "a100" in line, line


def test_the_stranded_note_agrees_in_number(monkeypatch):
    """Live: "(K80, P100, P4, V100 is granted too, and this tool cannot drive
    it — no GSP)". Plural list, singular verb and pronoun, on the first screen a
    new user sees."""
    from comfy_qa.setup import _stranded_note

    assert _stranded_note(["K80"]) == "K80 is granted too, and this tool cannot drive it"
    many = _stranded_note(["K80", "P100"])
    assert "are granted too" in many and "drive them" in many, many


# --- a re-run must never quietly lower a standing request --------------------


def quota_request(cloud, monkeypatch, *args):
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    with monkeypatch.context() as patch:
        patch.setattr("comfy_qa.auth.Gcloud", lambda *a, **k: cloud.gcloud())
        return CliRunner().invoke(app, ["quota", "request", "--no-wait", *args])


STANDING_L4_AT_8 = [preference(
    L4, granted=0, preferred=8, reconciling=True, name="l4-standing",
    dimensions={"region": "us-central1"})]

STANDING_H100_AT_8 = [preference(
    FAMILY, granted=0, preferred=8, reconciling=True,
    name="comfyqat_gpus-per-gpu-family-per-project-region_nvidia-h100_us-central1",
    dimensions={"gpu_family": "NVIDIA_H100", "region": "us-central1"})]


def test_a_re_run_does_not_lower_a_standing_request(monkeypatch):
    """FAILS ON TODAY'S CODE, and it is the only defect tonight that reaches out
    and damages the thing the user is waiting on.

    `--value` defaults to 1. `request_plan` addresses an existing preference by
    its own id — right for avoiding duplicates, and it turns a re-run into an
    `update`. `setup` files H100 at 8; a user running `quota request --gpu h100`
    to check on it downgrades a live request to a number that cannot start an
    `a3-highgpu-8g` even if granted. No prompt, nothing in the output.

    ASSERTED ON THE VALUE SENT TO GOOGLE, not the exit code and not the message.
    A test that checks the command succeeded passes straight through this.
    """
    cloud = Cloud(preferences=STANDING_H100_AT_8)
    result = quota_request(cloud, monkeypatch, "--gpu", "h100")

    assert result.exit_code == 0, result.output
    sent = [flag(args, "preferred-value") for args in cloud.submitted]
    assert "1" not in sent, (
        f"a standing request for 8 was overwritten with {sent}")
    assert sent == ["8"], sent


def test_lowering_on_purpose_is_named_and_gated(monkeypatch):
    """A value the user DID type may lower it — and the output has to say so.
    "Lowering" must be a word that appears, not an outcome they discover later."""
    cloud = Cloud(preferences=STANDING_H100_AT_8)
    refused = quota_request(cloud, monkeypatch, "--gpu", "h100", "--value", "1")

    assert cloud.submitted == [], "it lowered without being asked twice"
    assert "lower" in refused.output.lower(), refused.output

    allowed = Cloud(preferences=STANDING_H100_AT_8)
    ok = quota_request(allowed, monkeypatch, "--gpu", "h100", "--value", "1",
                       "--allow-lower")
    assert ok.exit_code == 0, ok.output
    assert [flag(a, "preferred-value") for a in allowed.submitted] == ["1"]
    assert "lower" in ok.output.lower(), "the lowering was not named"


def test_setup_cannot_lower_a_standing_request_either(tmp_path):
    """The same guard on the other surface, before it is the eighth second-site.

    A ceiling standing at 8 must not be trimmed to `CEILING_REQUEST` by a setup
    run that computes a smaller number.
    """
    standing = [preference(CEILING, granted=0, preferred=8, reconciling=True,
                           name="gpus-all-regions-1")]
    cloud = Cloud(preferences=standing)
    run(cloud, config_path=tmp_path / "hosts.toml")

    for args in cloud.submitted:
        if flag(args, "quota-id") == CEILING:
            assert int(flag(args, "preferred-value")) >= 8, args


def test_quota_request_says_what_the_table_says_about_an_unorderable_card(monkeypatch):
    """`quota request --gpu b200` said "this project reports no quota for 'b200'",
    which is FALSE — the project meters it and `quota list` prints the row.

    `quota request` is the one caller that never received `offered`/`unspendable`:
    the seventh second-site, in the command that files irrevocable requests.
    """
    cloud = Cloud(quotas=pooled())
    result = quota_request(cloud, monkeypatch, "--gpu", "b200")

    assert result.exit_code == 2
    assert "reports no quota" not in result.output, result.output
    assert "not creatable by this tool" in result.output, result.output
    assert cloud.submitted == []


def test_a_granted_card_reports_neither_half_of_the_split():
    """`never_asked_in` was `places - refused`, i.e. "not refused" — so a granted
    L4 reported 43 places nobody had asked in, when it is granted in all of them.

    A count whose name and arithmetic disagree equals the right answer exactly
    when nothing is granted, which is the fixture that hid it. Three different
    numbers here: granted, refused, and never asked.
    """
    from comfy_qa.quota import readiness, summarise

    denied_here = [preference(A100, granted=0, preferred=1,
                              state_detail=DENIED_DETAIL, name="a100-usc1",
                              dimensions={"region": "us-central1"})]
    spread = [q for q in THIS_PROJECT if q["quotaId"] != A100] + [{
        "quotaId": A100,
        "dimensionsInfos": [{"dimensions": {"region": r}, "details": {},
                             "applicableLocations": [r]} for r in REGIONS_43],
    }]
    by_gpu = {c.gpu: c for c in summarise(readiness(spread, denied_here))}

    granted = by_gpu["L4"]
    assert (granted.refused_in, granted.never_asked_in) == (0, 0), (
        "a card you can use reported places nobody had asked in")
    refused = by_gpu["A100"]
    assert refused.refused_in == 1
    assert refused.never_asked_in == len(REGIONS_43) - 1


def test_nothing_missing_does_not_recommend_every_card(tmp_path):
    """The fallback fired on "nothing to ask for" and printed all five cards — on
    a project where every one is granted or refused, so the advice was to file
    five requests nobody needs."""
    cloud = Cloud(quotas=pooled(), preferences=PREFS_DENIED)
    p = run(cloud, config_path=tmp_path / "hosts.toml", quota_requests=False)

    line = next(line for line in p.said if "--no-quota-request" in line)
    assert "l4" not in line.lower() or "Nothing was missing" in line, line


def test_setup_reads_the_slow_quota_list_exactly_once(tmp_path):
    """`compute_quotas` is a MINUTE against a real project.

    `_missing_cards` re-read it while the caller already held the result, and
    `setup --no-quota-request` went from ~50s to 1:46 — doubling the slowest step
    of the first command a new user runs, for a call already paid for. Counted
    rather than timed, because a wall-clock assertion is flaky and a call count
    is the thing that actually went wrong.
    """
    cloud = Cloud()
    seen: list[str] = []
    inner = cloud._run
    cloud._run = lambda args, mode: (seen.append(" ".join(args[:3])),
                                     inner(args, mode))[1]

    run(cloud, config_path=tmp_path / "hosts.toml", quota_requests=False)

    reads = [c for c in seen if c.startswith("quotas info")]
    assert len(reads) == 1, f"the minute-long quota read happened {len(reads)}x"


def test_nothing_to_ask_for_recommends_nothing(tmp_path):
    """The fallback fired on "nothing to ask for" and printed all five cards.

    Needs a project where the plan is genuinely EMPTY — the earlier version of
    this test left A100 requestable, so `wanted` was non-empty and the fallback
    it was written for never ran.
    """
    # The family denial needs its `gpu_family`, or `asks_about` correctly does
    # not match it to the H100 and the plan still wants one — which is how the
    # first version of this test left `wanted` non-empty and never reached the
    # fallback it was written for.
    every_card_denied = [
        preference(quota_id, granted=0, preferred=1, state_detail=DENIED_DETAIL,
                   name=f"denied-{n}")
        for n, quota_id in enumerate((A100, A100_80, CEILING))
    ] + [preference(FAMILY, granted=0, preferred=8, state_detail=DENIED_DETAIL,
                    name="denied-h100",
                    dimensions={"gpu_family": "NVIDIA_H100",
                                "region": "us-central1"})]
    cloud = Cloud(quotas=pooled(), preferences=every_card_denied)
    p = run(cloud, config_path=tmp_path / "hosts.toml", quota_requests=False)

    line = next(line for line in p.said if "--no-quota-request" in line)
    assert "--gpu" not in line, line
    # AND IT SAYS WHY THERE IS NOTHING TO ASK FOR. "Nothing was missing anyway"
    # was one sentence covering three different situations, and only the first of
    # them was that: here every card is REFUSED and at zero, which is the
    # opposite of nothing missing. The claim this test was written for — that the
    # fallback stops recommending five cards nobody needs — is unchanged, and
    # asserted on `--gpu` above.
    assert "refused" in line, line
    assert "Nothing was missing" not in line, line


# --- `request_value`, tested directly ----------------------------------------
#
# `grep -rl request_value tests/` returned NOTHING. The CLI test exercises the
# path, so the behaviour was covered — but the function's contract was not, and
# the case that escaped is one the CLI test's fixture never produces. Both, not
# either: this is the only function in the feature that changes state at Google.

def standing(preferred, granted=0, **kw):
    """A preference already with Google. `preferred=None` omits the field, which
    is how the API reports a value it has none of."""
    config = {"grantedValue": str(granted)}
    if preferred is not None:
        config["preferredValue"] = str(preferred)
    return [{"quotaId": "Q", "quotaConfig": config,
             "name": "projects/p/locations/global/quotaPreferences/standing", **kw}]


def test_request_value_raise():
    from comfy_qa.quota import Target, request_value

    assert request_value(Target("Q"), standing(1), 8) == (8, "")


def test_request_value_equal():
    from comfy_qa.quota import Target, request_value

    assert request_value(Target("Q"), standing(8), 8) == (8, "")


def test_request_value_lower_is_refused_and_named():
    from comfy_qa.quota import Target, request_value

    send, note = request_value(Target("Q"), standing(8), 1)
    assert send == 8, "it lowered"
    assert "lower" in note


def test_request_value_lower_on_purpose_is_performed_and_named():
    from comfy_qa.quota import Target, request_value

    send, note = request_value(Target("Q"), standing(8), 1, allow_lower=True)
    assert send == 1
    assert "LOWERING" in note


def test_request_value_with_nothing_standing():
    from comfy_qa.quota import Target, request_value

    assert request_value(Target("Q"), [], 1) == (1, "")


def test_request_value_refuses_when_the_standing_value_cannot_be_read():
    """THE CASE THAT ESCAPED, and absent-versus-zero for the fifth time tonight —
    this time in the only function in the feature that changes state at Google.

    `_as_int` returns 0 for an absent `preferredValue`, so `wanted >= 0` held for
    every value and the guard returned the default with an EMPTY message: no
    refusal, no "lower", nothing printed. Google omits a value it has none of, so
    absent and zero are different readings — the rule is written in this module
    in our own words and this function did not follow it.

    Unknown means DO NOT LOWER, which here means do not send at all: we cannot
    keep a standing value we cannot read, and sending anything might reduce it.
    """
    from comfy_qa.quota import Target, request_value

    send, note = request_value(Target("Q"), standing(None), 1)
    assert send is None, "it sent a value without knowing what it would replace"
    assert "could not read" in note.lower(), note

    # And `--allow-lower` is the way through, as for a deliberate decrease.
    forced, note = request_value(Target("Q"), standing(None), 1, allow_lower=True)
    assert forced == 1 and note


def test_by_region_does_not_contradict_the_collapsed_table(monkeypatch):
    """THE NINTH SECOND-SITE, and it is the surface `create` sends people to.

    `--by-region` said `none — request it` for A100 six lines below the same card
    reading `denied — asking again will not help` in the collapsed table. Per
    region the row is honest — nobody asked in europe-west4 — but a reader who
    files that request is re-filing one Google already refused.
    """
    denied_here = [preference(A100, granted=0, preferred=1,
                              state_detail=DENIED_DETAIL, name="a100-usc1",
                              dimensions={"region": "us-central1"})]
    spread = [q for q in THIS_PROJECT if q["quotaId"] != A100] + [{
        "quotaId": A100,
        "dimensionsInfos": [{"dimensions": {"region": r}, "details": {},
                             "applicableLocations": [r]} for r in REGIONS_43],
    }]
    result = quota_list(Cloud(quotas=spread, preferences=denied_here),
                        monkeypatch, "--by-region")

    # THE REGION COLUMN, not the whole line. The verdict now NAMES the region the
    # refusal was made in, so `"us-central1" not in line` began excluding every
    # row it was meant to select — a filter colliding with new true text, which
    # is the third time that has happened in this suite.
    lines = result.output.splitlines()
    header = next(l for l in lines if l.startswith("GPU "))
    start, stop = header.index("REGION"), header.index("LIMIT")
    elsewhere = [l for l in lines
                 if l.startswith("A100 ") and l[start:stop].strip() != "us-central1"]
    assert elsewhere, "no other-region rows rendered"
    for line in elsewhere:
        assert "refused" in line, (
            f"a region nobody asked in gives no hint the card was refused "
            f"elsewhere: {line}")


def test_a_refusal_sends_you_back_to_the_region_you_asked_about():
    """It said `--region us-central1` whatever was typed — so on a project already
    refused there, the fix line sent somebody to re-file the exact request Google
    denied, in a region they had not named."""
    from comfy_qa.create import CARDS, check_quota

    check = check_quota(CARDS["a100"], [], [], "europe-west4")
    problem = check.problem()

    assert problem is not None
    assert "--region europe-west4" in problem.fix, problem.fix
    assert "us-central1" not in problem.fix


UNREADABLE_A100 = [{
    # A standing request whose `preferredValue` the API omitted. Google omits a
    # value it has none of, so this is a shape the API really produces.
    "quotaId": A100,
    "quotaConfig": {"grantedValue": "0"},
    "dimensions": {"region": "us-central1"},
    "name": "projects/p/locations/global/quotaPreferences/a100-unreadable",
}]


def test_quota_request_sends_nothing_when_the_standing_value_is_unreadable(monkeypatch):
    """The CLI half of the critical fix — `request_value` returning None has to
    actually stop the command, not be computed and dropped."""
    cloud = Cloud(preferences=UNREADABLE_A100)
    result = quota_request(cloud, monkeypatch, "--gpu", "a100",
                           "--region", "us-central1")

    assert cloud.submitted == [], "it sent a value it could not compare"
    assert result.exit_code == 2
    assert "could not read" in result.output.lower(), result.output


def test_setup_does_not_touch_a_request_whose_standing_value_is_unreadable(tmp_path):
    """`setup` cannot reach `request_value` with an unreadable standing value —
    and the reason is worth pinning rather than assuming.

    A preference with no `preferredValue`, no `reconciling` flag and no
    `stateDetail` reads as PENDING, which is the conservative default, so
    `settled` declines to re-ask and the card never reaches the send loop. The
    `send is None` branch in `ensure_quota_requests` is therefore unreachable
    today: defence behind a policy rather than behind a structure.

    So this asserts the OUTCOME that matters — nothing is sent — and names the
    route, so that if `settled` ever stops skipping pending requests the reason
    this held is on record rather than rediscovered.
    """
    from comfy_qa.quota import asks

    assert asks(UNREADABLE_A100)[0].state == "pending", (
        "the route this test documents has changed; check the send loop's "
        "`send is None` branch, which was unreachable because of it")

    cloud = Cloud(preferences=UNREADABLE_A100)
    p = run(cloud, config_path=tmp_path / "hosts.toml")

    assert A100 not in creates(cloud), "it sent a value it could not compare"
    assert any("A100" in line and "not answered yet" in line for line in p.said)


# --- findings 5-8: the pool override lands after sort, dedupe and the notes ---


def test_a_pool_won_row_still_reports_its_refusals(monkeypatch):
    """Finding 5. RTX-PRO-6000's two refusals are in `--json` and in no human
    sentence: the "another pool won" branch RETURNS EARLY, before the
    refused/never-asked block. The same early return skips `undrivable` and
    `cpu_blocked`, so a GSP-less card whose only grant were Spot would print
    "ready (Spot)" with no GSP warning at all."""
    result = quota_list(Cloud(quotas=pooled(), preferences=DENIED_RTX), monkeypatch)

    line = next(l for l in result.output.splitlines()
                if l.startswith("RTX-PRO-6000"))
    assert "refused" in line, line


def test_a_pool_won_row_still_warns_about_a_card_the_driver_cannot_load(monkeypatch):
    """The unreachable-today half of finding 5, driven directly: a GSP-less card
    whose only grant is Spot must not lose its GSP warning to the early return."""
    spot_k80 = "PREEMPTIBLE-NVIDIA-K80-GPUS-per-project-region"
    quotas = pooled(quota(K80_ZERO, 0), quota(spot_k80, 1))
    result = quota_list(Cloud(quotas=quotas), monkeypatch)

    line = next(l for l in result.output.splitlines() if l.startswith("K80 "))
    assert "cannot drive it" in line, line


def test_no_card_is_printed_twice_identically(monkeypatch):
    """Finding 6. Three `readiness` rows each had their WHERE overwritten by the
    winning pool's geography, collapsing three distinguishable rows into three
    identical ones. The de-duplication keys on (gpu, region) and runs BEFORE the
    override, so it cannot see the collision it creates."""
    result = quota_list(Cloud(quotas=pooled(), preferences=DENIED_RTX),
                        monkeypatch, "--by-region")

    rows = [l for l in result.output.splitlines()
            if l and not l.startswith(("GPU", " ", "note:"))]
    assert len(rows) == len(set(rows)), (
        "the same row is printed more than once: "
        f"{[r for r in rows if rows.count(r) > 1][:2]}")


def test_the_table_is_sorted_by_the_status_it_prints(monkeypatch):
    """Finding 7. A row reading `ready` sat below three reading `denied`, because
    the sort keys on the on-demand summary while the STATUS column is rendered
    from the resolved value. `resolved()` says limit, place, status and pool are
    "decided ONCE and both renderings read the result" — the sort was a third
    reading that did not get it."""
    # NO DENIALS, deliberately. With `DENIED_RTX` the card's on-demand status is
    # `denied`, which already ranks between `ready` and `none` — so an unsorted
    # render happens to look sorted and the mutation that removes the sort
    # survives. Without them the on-demand status is `none`, the last rank, and
    # the pool override has to lift the row past every other `none` card to the
    # top. That is the only arrangement where the defect is visible.
    result = quota_list(Cloud(quotas=pooled()), monkeypatch)

    states = []
    for line in result.output.splitlines():
        if line.startswith(("GPU", " ", "note:")) or not line:
            continue
        states.append("ready" if "ready" in line.split("  ")[-1] else "other")
    assert "ready" in states and "other" in states, (
        "the fixture must produce both, or the assertion is vacuous")
    assert states == sorted(states, key=lambda s: s != "ready"), (
        f"a ready row is printed below a non-ready one: {states}")


def test_a_region_filter_relabels_every_row_including_pool_won_ones(monkeypatch):
    """Finding 8. Every other row said `us-central1`; RTX-PRO-6000 said
    "43 regions", because `readiness` does the relabel and `pools_for` computes
    its geography from scratch with no equivalent. Textbook second site — and the
    region-filtered view is the one that says "ready" about the region the
    on-demand request was refused in."""
    result = quota_list(Cloud(quotas=pooled(), preferences=DENIED_RTX),
                        monkeypatch, "--region", "us-central1")

    rows = [l for l in result.output.splitlines()
            if l and not l.startswith(("GPU", " ", "note:"))]
    assert rows, "no rows rendered"
    for line in rows:
        where = line[14:].split()[1] if line[14:].split() else ""
        assert "regions" not in line[:42], (
            f"a row kept a pool's own geography under --region: {line}")


# --- findings 9, 10, 11, 12, 15 ----------------------------------------------


def test_spot_quota_is_visible_even_when_on_demand_wins(monkeypatch):
    """Finding 10, the one that hid the RTX PRO 6000 for months, one layer in.

    Six cards hold `PREEMPTIBLE-…-GPUS` = 1 across every region and no surface
    said so. The L4 row named the pool needing a purchased commitment and the
    pool whose usability the tool itself calls unverified, and omitted the one
    that is actually granted — because the aside filtered on `not pool.counts`
    and Spot counts, so Spot could only ever appear as the WINNER.

    "Most favourable wins" is right for the status and wrong for disclosure.
    """
    result = quota_list(Cloud(quotas=pooled()), monkeypatch)

    assert "Spot" in result.output, "a granted Spot allowance is named nowhere"
    spot_note = result.output.split("note: Spot", 1)
    assert len(spot_note) == 2, "Spot has no footnote"
    assert "L4" in spot_note[1], (
        "the L4 holds Spot quota and is not named as holding it")


def test_a_refusal_names_the_card_the_quota_table_shows(monkeypatch):
    """Finding 9. `create --gpu h100` refused with "this project has no H100-80GB
    quota" while `quota list` showed an `H100` row and no `H100-80GB` row, so a
    reader following the sentence to the table found nothing by that name.

    THE ASSERTION USED TO BE THE LITERAL `H100`, and that is why this test had to
    be rewritten rather than simply passing: the table's name changed, the
    principle did not, and a test pinned to one spelling of a rule cannot tell
    the two apart. The name is DERIVED here from the same function `quota list`
    renders with, so whichever way that goes next, this still checks the thing
    Finding 9 was about.
    """
    from comfy_qa.create import CARDS, check_quota
    from comfy_qa.quota import row_name

    card = CARDS["h100"]
    shown = row_name(FAMILY, {"gpu_family": card.quota_family})
    problem = check_quota(card, [], [], "us-central1").problem()

    assert problem is not None
    assert shown in str(problem), (shown, str(problem))
    # WITH THE SHOWN NAME REMOVED FIRST. `H100` is a substring of `H100-80GB`
    # and `\bH100\b` matches inside it, because `-` is not a word character —
    # so the naive check reported the sentence as carrying both names when it
    # carried one. Strike out what is legitimately there, then look for what is
    # not.
    rest = str(problem).replace(shown, "")
    for name in [n for n in card.quota_names if n != shown]:
        assert name not in rest, (
            f"{name} and {shown} are the same card, printed in one sentence")


def test_cards_that_do_work_does_not_promise_quota():
    """Finding 11. `create --gpu k80` answered "Cards that do work: a100,
    a100-80gb, h100, l4, t4" — on a project where three of those are at zero with
    standing refusals. The sibling message eight lines away already has the right
    framing: "This tool can create: …"."""
    from comfy_qa.create import CARDS, check_quota

    problem = check_quota(CARDS["p100"], [], []).problem()
    text = str(problem) + (problem.fix or "") if problem else ""
    assert "do work" not in text, text


def test_pools_for_is_tested_directly():
    """Finding 12. `pools_for` produces the four-pool verdict behind every
    "ready (Spot)" sentence, and only `best_pool` — which consumes its output —
    was tested."""
    from comfy_qa.quota import ON_DEMAND, SPOT, WORKSTATION, pools_for

    found = {p.name: p for p in pools_for("RTX-PRO-6000", pooled(), PREFS_NONE)}
    assert set(found) == {SPOT, WORKSTATION}, sorted(found)
    assert found[SPOT].limit == 1 and found[SPOT].counts is True
    assert found[WORKSTATION].counts is False, "workstation must never count"
    assert found[SPOT].cost, "the pool's cost must be carried, not inferred later"

    # A card the project meters in no extra pool gets none of them invented.
    assert pools_for("T4", [quota(T4, 1)], PREFS_NONE) == []

    # And the on-demand row is included when one is handed in.
    from comfy_qa.quota import readiness
    row = next(r for r in readiness([quota(T4, 1)]) if r.gpu == "T4")
    assert pools_for("T4", [quota(T4, 1)], PREFS_NONE,
                     on_demand=row)[0].name == ON_DEMAND


def test_allow_lower_is_tested_directly():
    """Finding 12. `grep -rl allow_lower tests/` returned nothing."""
    from comfy_qa.quota import Target, request_value

    assert request_value(Target("Q"), standing(8), 1, allow_lower=False)[0] == 8
    assert request_value(Target("Q"), standing(8), 1, allow_lower=True)[0] == 1
    assert request_value(Target("Q"), standing(None), 1, allow_lower=False)[0] is None
    assert request_value(Target("Q"), standing(None), 1, allow_lower=True)[0] == 1


def test_json_does_not_put_two_units_in_sibling_integers(monkeypatch):
    """Finding 15. `asked` is a GPU COUNT and `refused_in`/`never_asked_in` are
    REGION counts — three sibling integers, two units, one of them named `asked`
    next to one named `never_asked_in`. RTX-PRO-6000 reading `asked: 1,
    refused_in: 2` looks like a contradiction until you know."""
    import json as jsonlib

    blob = jsonlib.loads(quota_list(Cloud(quotas=pooled(), preferences=DENIED_RTX),
                                    monkeypatch, "--json").stdout)
    record = next(g for g in blob["gpus"] if g["gpu"] == "RTX-PRO-6000")

    assert "asked" not in record, "the ambiguous name is still there"
    assert "asked_gpus" in record, sorted(record)
    assert record["refused_in_regions"] == 2
    assert "never_asked_in_regions" in record


# --- `--dry-run` means one thing on every command -----------------------------


def test_setup_dry_run_reaches_google_for_nothing(tmp_path):
    """`--dry-run` printed the plan on `quota request` and CALLED GOOGLE on
    `setup`. Each help string was honest alone; together they were a trap laid
    for the user who learns the flag on one command and carries it to the other.

    One meaning across the tool: print what would happen, touch nothing, reach
    nothing. `--validate-only` is the flag that reaches Google, on both.
    """
    cloud = Cloud()
    seen: list[str] = []
    inner = cloud._run
    cloud._run = lambda args, mode: (seen.append(" ".join(args[:3])),
                                     inner(args, mode))[1]
    p = run(cloud, config_path=tmp_path / "hosts.toml", quota_dry_run=True)

    assert cloud.submitted == [], "a dry run called Google"
    assert not any(c.startswith("quotas preferences update") for c in seen)
    assert any("would ask" in line for line in p.said), p.said


def test_setup_validate_only_does_reach_google(tmp_path):
    """The behaviour `--dry-run` used to have, under the name it shares with
    `quota request`."""
    cloud = Cloud()
    run(cloud, config_path=tmp_path / "hosts.toml", quota_validate_only=True)

    assert cloud.submitted, "--validate-only checked nothing with Google"
    for args in cloud.submitted:
        assert "--validate-only" in args, args


def test_each_flag_help_names_the_other():
    """The whole defect is a user who knows only one of the two, so each has to
    point at the other."""
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    for command in (["setup", "--help"], ["quota", "request", "--help"]):
        text = CliRunner().invoke(app, command).output
        assert "--dry-run" in text and "--validate-only" in text, command


def test_a_pool_footnote_lists_every_card_that_holds_it(monkeypatch):
    """"Held for:" is a claim about who holds the pool, and the card most
    associated with Spot was missing from Spot's own list.

    The aside excluded a pool that WON the row — defensible, because the row
    already names it inline — but the footnote is not the aside: it explains a
    pool, and a reader scanning it to answer "which of my cards have Spot?" got
    six when the answer was seven. The seventh was RTX-PRO-6000, the only card
    whose Spot grant is the reason it is usable at all.

    The same rule has to hold for committed and workstation: they are asides
    today only because no card wins on them, and the first that does would
    vanish from its own footnote the same way.
    """
    result = quota_list(Cloud(quotas=pooled(), preferences=DENIED_RTX), monkeypatch)

    spot = result.output.split("note: Spot", 1)[-1].split("note:")[0]
    assert "RTX-PRO-6000" in spot, (
        f"the card whose Spot grant makes it usable is absent from Spot's own "
        f"footnote: {spot.strip()}")
    assert "L4" in spot, spot

    # And the rule, not the instance: every pool a card holds lists that card.
    from comfy_qa.quota import ON_DEMAND, pools_for
    for card in ("L4", "RTX-PRO-6000"):
        for pool in pools_for(card, pooled(), DENIED_RTX):
            if pool.name == ON_DEMAND or pool.status != "ready":
                continue
            note = result.output.split(f"note: {pool.name}", 1)[-1].split("note:")[0]
            assert card in note, f"{card} holds {pool.name} and is not listed"


def test_an_explicit_zero_is_not_turned_into_one(monkeypatch):
    """FAILS ON TODAY'S CODE, on the irrevocable path, with the rule written in a
    comment two lines above the line that breaks it.

        # `value is None` means the user did not type one ... a default must
        # never touch it.
        send, note = request_value(resolved, preferences, value or DEFAULT_VALUE, ...)

    `value or DEFAULT_VALUE` falsy-tests an `Optional[int]`, so an explicit `0`
    becomes `1` — and on the H100 it announces "LOWERING the standing request
    from 8 to 1", a number nobody typed.

    THROUGH THE CLI, necessarily: every `allow_lower` test calls `request_value`
    directly, and `request_value` is correct. The suite was structurally blind to
    its own caller, which is the third time tonight the guard was right and the
    wiring was wrong.

    `--release-quota` ARRIVED LATER, and this test carries it because what it
    guards is unchanged: a 0 the user typed must never be read as "they typed
    nothing" and turned into 1. That zero now also needs a flag of its own is a
    separate rule, tested separately; bolting it on here rather than relaxing
    this assertion is what keeps the original claim checkable.
    """
    cloud = Cloud(preferences=STANDING_H100_AT_8)
    result = quota_request(cloud, monkeypatch, "--gpu", "h100", "--value", "0",
                           "--allow-lower", "--release-quota")

    sent = [flag(args, "preferred-value") for args in cloud.submitted]
    assert "1" not in sent, f"an explicit 0 became {sent}"
    assert sent == ["0"], sent
    assert "to 1" not in result.output, result.output


def test_an_explicit_zero_is_refused_on_its_own_terms(monkeypatch):
    """Zero is a coherent request — hold no quota for this card — and an absurd
    accident.

    THIS DOCSTRING USED TO SAY zero was "treated as what it is: a lowering,
    refused without `--allow-lower` like any other", and the test went on passing
    after that stopped being true, because `"lower" in output` is satisfied by
    the string `--allow-lower` in any message that names the flag. A weak
    assertion kept a false description alive; both are fixed here.

    Zero has its own rule now — it is refused whatever the comparison says, and
    needs `--release-quota` — so the refusal is asserted on the word that only
    the new rule produces."""
    cloud = Cloud(preferences=STANDING_H100_AT_8)
    result = quota_request(cloud, monkeypatch, "--gpu", "h100", "--value", "0")

    assert cloud.submitted == [], "zero lowered a standing request unasked"
    assert result.exit_code == 2
    assert "--release-quota" in result.output, result.output


def test_release_quota_alone_is_not_enough(monkeypatch):
    """Both flags, deliberately. `--release-quota` names the intent and
    `--allow-lower` accepts the consequence; an unrecoverable change is the one
    place where asking twice is proportionate rather than ceremony."""
    cloud = Cloud(quotas=[quota(T4, 1, locations=["us-central1"])])
    result = quota_request(cloud, monkeypatch, "--gpu", "t4", "--value", "0",
                           "--release-quota")

    assert cloud.submitted == [], "one flag released a working grant"
    assert result.exit_code == 2


# --- quota is not availability ------------------------------------------------


def test_a_region_view_says_when_the_region_does_not_offer_the_card(monkeypatch):
    """`quota list --region europe-west9` called seven cards `ready` in a region
    GCE offers almost none of them in — K80 among them, and **K80 no longer
    exists on GCE anywhere**.

    The GSP defect with a different cause. GSP was "ready about a card that
    cannot work"; this is "ready about a card that is not there". Holding quota
    for a card in a region is not the same as the card existing in that region,
    and this is the command whose job is to answer "what can I run".

    The check is one call when `--region` is given, so it is made there.
    """
    # Both cards granted in the SAME region, so the only difference between them
    # is whether GCE offers the card there — which is the whole subject. The
    # shared fixtures scope L4 and T4 to us-central1, so a region view of
    # anywhere else renders no card rows and every assertion below would hold
    # vacuously.
    here = "europe-west4"
    quotas = [quota(L4, 1, locations=[here]), quota(T4, 1, locations=[here])]
    cloud = Cloud(quotas=quotas, offered_in={"nvidia-l4": [f"{here}-a"]})
    result = quota_list(cloud, monkeypatch, "--region", here)

    rows = {l.split()[0]: l for l in result.output.splitlines()
            if l[:1].isalnum()}
    assert {"L4", "T4"} <= set(rows), f"fixture rendered {sorted(rows)}"

    assert "not offered" not in rows["L4"], rows["L4"]
    # THE VERDICT, not a suffix. It used to read `ready — this region does not
    # offer it — this tool cannot drive it`: three verdicts chained by em-dashes
    # with the most misleading one first, and a reader scanning the column sees
    # `ready`. Whatever a card that is not there is, it is not ready.
    assert rows["T4"].split("  ")[-1].startswith("not offered here"), rows["T4"]


def test_the_default_view_says_the_column_is_about_quota(monkeypatch):
    """Without `--region` the check would be one call per card against a live API
    on top of a ~50-second read. So the column says what it is instead — and
    "ready" must not mean two different things depending on a flag."""
    result = quota_list(Cloud(quotas=pooled()), monkeypatch)

    assert "quota held" in result.output, (
        "the default view claims availability it has not checked")
    assert "comfy-qat create" in result.output, (
        "the command that knows both is not named")


def test_the_default_view_makes_no_availability_calls(monkeypatch):
    """The check is affordable for ONE region and not for forty-three.

    `accelerator-types` is a live call per card. Making it unconditionally would
    put fourteen of them on top of a ~50-second quota read — the same shape as
    the `_missing_cards` refetch that doubled `setup`, which is why this is
    counted rather than described. Adding the check is how the column stops
    lying; adding it everywhere is how the command stops being usable.
    """
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    cloud = Cloud(quotas=pooled())
    seen: list[str] = []
    inner = cloud._run
    cloud._run = lambda args, mode: (seen.append(" ".join(args[:3])),
                                     inner(args, mode))[1]
    with monkeypatch.context() as patch:
        patch.setattr("comfy_qa.auth.Gcloud", lambda *a, **k: cloud.gcloud())
        CliRunner().invoke(app, ["quota", "list"])

    looked = [c for c in seen if c.startswith("compute accelerator-types")]
    assert looked == [], (
        f"the default view made {len(looked)} live availability calls")


def test_a_region_view_makes_one_call_per_card_and_no_more(monkeypatch):
    """The other half: affordable is not free, and a call per card per region
    would be the regression wearing a different hat."""
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    cloud = Cloud(quotas=[quota(L4, 1, locations=["europe-west4"])],
                  offered_in={"nvidia-l4": ["europe-west4-a"]})
    seen: list[str] = []
    inner = cloud._run
    cloud._run = lambda args, mode: (seen.append(" ".join(args[:3])),
                                     inner(args, mode))[1]
    with monkeypatch.context() as patch:
        patch.setattr("comfy_qa.auth.Gcloud", lambda *a, **k: cloud.gcloud())
        CliRunner().invoke(app, ["quota", "list", "--region", "europe-west4"])

    looked = [c for c in seen if c.startswith("compute accelerator-types")]
    assert len(looked) == 1, f"{len(looked)} calls for one card"


def test_a_card_absent_from_the_region_is_not_told_to_request_it(monkeypatch):
    """"Request it" is the only instruction in this table, and it was instructing
    an irrevocable request for a card that does not exist in the region asked
    about. Third time that phrase has survived a fix by appearing on a branch
    nobody re-read."""
    here = "europe-west4"
    quotas = [quota(A100, 0, locations=[here]), quota(L4, 1, locations=[here])]
    cloud = Cloud(quotas=quotas, offered_in={"nvidia-l4": [f"{here}-a"]})
    result = quota_list(cloud, monkeypatch, "--region", here)

    a100 = next(l for l in result.output.splitlines() if l.startswith("A100 "))
    assert "request it" not in a100, a100
    assert a100.split("  ")[-1].startswith("not offered here"), a100


def test_the_spot_branch_gets_the_availability_check_too(monkeypatch):
    """The third occurrence of one structure: a pool-winning row taking a path
    that bypasses the checks the ordinary path runs. It hid `undrivable` and
    `cpu_blocked` two rounds ago and the availability check this round.

    Fixed where the branches rejoin, so the fourth branch somebody adds inherits
    the checks rather than needing to remember them.
    """
    here = "europe-west4"
    spot_rtx = "PREEMPTIBLE-NVIDIA-RTX-PRO-6000-GPUS-per-project-region"
    quotas = [family(None, "NVIDIA_RTX_PRO_6000", locations=[here]),
              quota(spot_rtx, 1, locations=[here])]
    # A REAL CATALOGUE that does not stock the card here. An empty one used to
    # serve, and no longer does — correctly: an empty catalogue means the lookup
    # found nothing, which is not a fact about the region. The card has to exist
    # somewhere and be absent HERE for this to be about availability at all.
    cloud = Cloud(quotas=quotas, accelerators=stocking("us-west4"))
    result = quota_list(cloud, monkeypatch, "--region", here)

    row = next(l for l in result.output.splitlines()
               if l.startswith("RTX-PRO-6000"))
    assert row.split("  ")[-1].startswith("not offered here"), (
        f"a Spot-won row skipped the availability check: {row}")


def test_json_and_by_region_carry_the_availability_verdict(monkeypatch):
    """The two surfaces this class has hidden in every previous round.

    `--json` is what a script gates on, and `--by-region` is where the "request
    it" instruction survived twice. If availability only reached the collapsed
    human table it would be the same defect one surface over.
    """
    import json as jsonlib

    here = "europe-west4"
    quotas = [quota(A100, 0, locations=[here]), quota(L4, 1, locations=[here])]
    offered = {"nvidia-l4": [f"{here}-a"]}

    blob = jsonlib.loads(quota_list(
        Cloud(quotas=quotas, offered_in=offered), monkeypatch,
        "--json", "--region", here).stdout)
    by_gpu = {g["gpu"]: g for g in blob["gpus"]}
    assert by_gpu["A100"]["offered_here"] is False, by_gpu["A100"]
    assert by_gpu["L4"]["offered_here"] is True, by_gpu["L4"]

    rows = quota_list(Cloud(quotas=quotas, offered_in=offered), monkeypatch,
                      "--by-region", "--region", here).output
    a100 = next(l for l in rows.splitlines() if l.startswith("A100 "))
    assert "not offered here" in a100, a100
    assert "request it" not in a100, a100


def test_an_unknown_cards_accelerator_id_is_derived_not_skipped(monkeypatch):
    """A card absent from `CARDS` has no accelerator id to look up, so the check
    used to skip it — leaving RTX-PRO-6000, the card this feature exists to
    surface, as the one row with no availability verdict while every card in the
    table got one.

    The id is derived (`RTX-PRO-6000` -> `nvidia-rtx-pro-6000`). This drives the
    case where the derivation MATTERS: the card IS offered here, so a blank or
    wrong id would return nothing and mark it absent. An earlier version of this
    test had it unoffered, where a broken derivation and a working one agree.
    """
    here = "europe-west4"
    spot_rtx = "PREEMPTIBLE-NVIDIA-RTX-PRO-6000-GPUS-per-project-region"
    quotas = [family(None, "NVIDIA_RTX_PRO_6000", locations=[here]),
              quota(spot_rtx, 1, locations=[here])]
    cloud = Cloud(quotas=quotas,
                  offered_in={"nvidia-rtx-pro-6000": [f"{here}-a"]})
    result = quota_list(cloud, monkeypatch, "--region", here)

    row = next(l for l in result.output.splitlines()
               if l.startswith("RTX-PRO-6000"))
    assert "not offered here" not in row, (
        f"the derived accelerator id did not find a card that is offered: {row}")
    assert "ready (Spot)" in row, row


def test_json_status_agrees_with_the_verdict_the_table_prints(monkeypatch):
    """Found by reading `--json` beside the table after the table was fixed.

    The human row led with `not offered here` while the record still read
    `"status": "ready"` — with `offered_here: false` beside it, so the fact was
    present and the verdict was not. A script gating on `status == "ready"`
    launches a card that is not in the region.

    That is finding 3 of the second pass exactly: a script gating on `--json`
    must reach the same verdict as a human reading the table. Fixing the table
    and leaving the machine surface behind is the same defect one surface over,
    which is the shape this class has taken every time.
    """
    import json as jsonlib

    here = "europe-west4"
    quotas = [quota(L4, 1, locations=[here]), quota(T4, 1, locations=[here])]
    blob = jsonlib.loads(quota_list(
        Cloud(quotas=quotas, offered_in={"nvidia-l4": [f"{here}-a"]}),
        monkeypatch, "--json", "--region", here).stdout)
    by_gpu = {g["gpu"]: g for g in blob["gpus"]}

    assert by_gpu["T4"]["status"] == "not offered here", by_gpu["T4"]
    assert by_gpu["T4"]["offered_here"] is False
    assert by_gpu["L4"]["status"] == "ready", by_gpu["L4"]


# --- the accelerator id is matched, never constructed -------------------------
#
# Read off the live project on 2026-09-17. There is no rule here:
#
#   nvidia-tesla-a100      and  nvidia-a100-80gb     (both, same card family)
#   nvidia-tesla-t4             nvidia-l4
#   nvidia-h100-80gb            nvidia-h100-mega-80gb
#   nvidia-h200-141gb           nvidia-rtx-pro-6000
#
# `"nvidia-" + gpu.lower()` is right for RTX-PRO-6000 and B200 and wrong for
# H100-MEGA and H200 — and because the constructed id matches nothing anywhere,
# those two claimed "not offered here" in EVERY region on Earth, including the
# ones that stock them.

REAL_ACCELERATORS = [
    "nvidia-a100-80gb", "nvidia-b200", "nvidia-gb200", "nvidia-h100-80gb",
    "nvidia-h100-mega-80gb", "nvidia-h200-141gb", "nvidia-l4", "nvidia-l4-vws",
    "nvidia-rtx-pro-6000", "nvidia-rtx-pro-6000-vws", "nvidia-tesla-a100",
    "nvidia-tesla-p100", "nvidia-tesla-p4", "nvidia-tesla-t4",
    "nvidia-tesla-v100",
]


def stocking(region, *ids):
    """Every accelerator the project reports, with `ids` present in `region`."""
    rows = []
    for name in REAL_ACCELERATORS:
        zone = f"{region}-a" if name in ids else "us-west4-a"
        rows.append({"name": name, "zone": f"https://x/zones/{zone}"})
    return rows


@pytest.mark.parametrize("card, real_id", [
    ("H100-MEGA", "nvidia-h100-mega-80gb"),
    ("H200", "nvidia-h200-141gb"),
    ("RTX-PRO-6000", "nvidia-rtx-pro-6000"),
    ("B200", "nvidia-b200"),
])
def test_a_card_the_region_stocks_is_not_called_absent(card, real_id, monkeypatch):
    """H100-MEGA and H200 are the two the constructed id gets wrong; RTX-PRO-6000
    and B200 are the two it gets right, and a test passing only on those proves
    nothing about the others."""
    here = "europe-west9"
    gpu_family = "NVIDIA_" + card.replace("-", "_")
    cloud = Cloud(quotas=[family(0, gpu_family, locations=[here])],
                  accelerators=stocking(here, real_id))
    result = quota_list(cloud, monkeypatch, "--region", here)

    row = next(l for l in result.output.splitlines() if l.startswith(card))
    assert "not offered here" not in row, (
        f"{card} is stocked in {here} as {real_id} and the tool says otherwise: "
        f"{row}")


def test_a_card_whose_id_cannot_be_resolved_says_the_check_was_not_made(monkeypatch):
    """A FAILED LOOKUP IS NOT A FACT, and calling it one is absent-versus-zero for
    the sixth time tonight — in the fix for a bug about asserting things the tool
    had not checked.

    "I looked and it is not there" and "my lookup found nothing" are different
    statements. The concept already exists: `offered_here` is None when no region
    was given and the check was not made. An unresolvable id is the same case.
    """
    here = "europe-west9"
    cloud = Cloud(quotas=[family(0, "NVIDIA_NEWTHING", locations=[here])],
                  accelerators=stocking(here))
    result = quota_list(cloud, monkeypatch, "--region", here)

    row = next(l for l in result.output.splitlines() if l.startswith("NEWTHING"))
    assert "not offered here" not in row, (
        f"an unresolvable id was reported as a fact about the region: {row}")


def test_the_nothing_usable_hint_names_this_project_and_this_region(monkeypatch):
    """It read `--gpu l4,a100 --region us-central1` whatever the project held and
    whatever region was asked about — so a europe-west9 view ended by
    recommending a request in us-central1. The last of the hardcoded hints."""
    here = "europe-west9"
    cloud = Cloud(quotas=[family(0, "NVIDIA_H100", locations=[here])],
                  accelerators=stocking(here, "nvidia-h100-80gb"))
    result = quota_list(cloud, monkeypatch, "--region", here)

    hint = next(l for l in result.output.splitlines() if "quota request" in l)
    assert "us-central1" not in hint, hint
    assert here in hint, hint


def test_json_says_not_checked_for_an_unresolvable_card(monkeypatch):
    """The machine surface of "a failed lookup is not a fact". `offered_here`
    already means "not checked" when no region was given; an id that could not be
    resolved is the same case, and collapsing it to `false` tells a script the
    card is absent when nobody managed to look."""
    import json as jsonlib

    here = "europe-west9"
    blob = jsonlib.loads(quota_list(
        Cloud(quotas=[family(0, "NVIDIA_NEWTHING", locations=[here])],
              accelerators=stocking(here)),
        monkeypatch, "--json", "--region", here).stdout)

    record = next(g for g in blob["gpus"] if g["gpu"] == "NEWTHING")
    assert record["offered_here"] is None, record
    assert record["status"] != "not offered here", record


def test_the_prefix_match_stops_at_a_segment_boundary():
    """`nvidia-h100` must not claim `nvidia-h1000`.

    The catalogue below is contrived — today's real one has no pair where the
    boundary changes the answer, so this pins the intended semantics rather than
    a live case. Written as a direct test because that is the only way to
    exercise it: through the CLI it is indistinguishable, and a rule nothing can
    exercise is one that silently rots.
    """
    from comfy_qa.auth import _match_accelerator

    stocked = {"nvidia-h100-80gb": {"a"}, "nvidia-h1000": {"a"},
               "nvidia-l4": {"a"}, "nvidia-l4-vws": {"a"}}

    assert _match_accelerator("H100", stocked) == ["nvidia-h100-80gb"]
    # Exact wins over any prefix, and the workstation variant is never the card.
    assert _match_accelerator("L4", stocked) == ["nvidia-l4"]
    assert _match_accelerator("NOSUCH", stocked) == []


# --- F5, F6, F7, F11: the raw `--quota-id` path and the region default --------


def test_quota_id_with_a_region_carries_it_as_a_dimension(monkeypatch):
    """F5. `--quota-id <family id> --region us-central1` filed a DIMENSIONLESS
    preference — byte-identical to the same command without `--region`, no
    warning, exit 0. That quota is dimensioned on (gpu_family, region), so the
    object filed is a different one from the object asked for, permanently."""
    cloud = Cloud()
    quota_request(cloud, monkeypatch, "--quota-id", A100,
                  "--region", "us-central1", "--value", "1")

    args = request_for(cloud, A100)
    assert flag(args, "dimensions") == "region=us-central1", args


def test_quota_id_sees_a_standing_request_on_the_same_pair(monkeypatch):
    """F6. The guard fired for a quota id with one preference and no-opped for one
    with three, because `matching_ask` is exact on (id, dimensions) and the raw
    path supplied none. Same guard, two behaviours, and one of them is wrong."""
    standing = [preference(A100, granted=0, preferred=8, reconciling=True,
                           name="a100-usc1",
                           dimensions={"region": "us-central1"})]
    cloud = Cloud(preferences=standing)
    result = quota_request(cloud, monkeypatch, "--quota-id", A100,
                           "--region", "us-central1", "--value", "1")

    assert cloud.submitted == [], "it lowered a standing 8 to 1"
    assert "lower" in result.output.lower(), result.output


def test_a_quota_id_this_project_does_not_report_is_refused(monkeypatch):
    """F7. `--quota-id NOT-A-QUOTA` was accepted, exit 0, on the irrevocable path
    — with no check against the 398 quota records the tool already reads."""
    cloud = Cloud()
    result = quota_request(cloud, monkeypatch, "--quota-id", "NOT-A-QUOTA")

    assert cloud.submitted == []
    assert result.exit_code == 2
    assert "NOT-A-QUOTA" in result.output


def test_no_region_does_not_file_in_whichever_sorts_first(monkeypatch):
    """F11. `quota request --gpu t4` with no `--region` filed in **asia-east1** —
    the first row alphabetically, and nothing about this project or this user
    points there. `setup` derives us-central1 for the same card and `create`
    picks by measured latency; this surface files a permanent preference in the
    least useful of the three."""
    asked_before = [preference(L4, granted=1, preferred=1, name="l4-usc1",
                               dimensions={"region": "us-central1"})]
    per_region = {
        "quotaId": T4,
        "dimensionsInfos": [{"dimensions": {"region": r}, "details": {"value": "1"},
                             "applicableLocations": [r]}
                            for r in ("asia-east1", "us-central1")],
    }
    cloud = Cloud(quotas=[per_region], preferences=asked_before)
    quota_request(cloud, monkeypatch, "--gpu", "t4")

    args = request_for(cloud, T4)
    assert flag(args, "dimensions") != "region=asia-east1", (
        "filed in the alphabetically first region")
    assert flag(args, "dimensions") == "region=us-central1", args


# --- F2, F9, F10, F12 ---------------------------------------------------------


def test_by_region_keeps_each_row_its_own_region(monkeypatch):
    """F2. All three RTX rows carried the CARD-level geography ("43 regions"), so
    the dedupe key `(gpu, where)` collapsed them to whichever sorted first — and
    that one held `refused_in: 1` while the card table and `--json` said 2.

    The per-region table is the one surface whose entire job is regions, and it
    was the one showing a card-level label. The docstring above the dedupe names
    this collapse as fixed; it was fixed for the collapsed view only.
    """
    # THE LIVE SHAPE: two per-region rows carved out by the denied requests plus
    # the catch-all covering the rest. `pooled()` gives the family one row across
    # every region, which cannot collapse and so cannot show the defect.
    three_rows = {
        "quotaId": FAMILY,
        "dimensionsInfos": [
            {"dimensions": {"gpu_family": "NVIDIA_RTX_PRO_6000",
                            "region": r}, "details": {},
             "applicableLocations": [r]}
            for r in ("us-central1", "europe-west4")
        ] + [{"dimensions": {"gpu_family": "NVIDIA_RTX_PRO_6000"}, "details": {},
              "applicableLocations": ["asia-east1", "africa-south1"]}],
    }
    quotas = [q for q in pooled() if q["quotaId"] != FAMILY] + [three_rows]
    result = quota_list(Cloud(quotas=quotas, preferences=DENIED_RTX),
                        monkeypatch, "--by-region")

    rtx = [l for l in result.output.splitlines() if l.startswith("RTX-PRO-6000")]
    assert len(rtx) > 1, "the per-region view collapsed to one row"
    regions = {l.split()[1] for l in rtx}
    assert "regions" not in " ".join(regions), (
        f"a per-region row is labelled with the card-level geography: {regions}")
    assert len(regions) == len(rtx), f"rows share a region label: {rtx}"


def test_quota_list_refuses_a_region_that_does_not_exist(monkeypatch):
    """F9. `--region not-a-region` printed a table and exited 0, and it reads as
    "this project has almost no quota". Its sibling validates the same flag —
    `quota request --gpu l4 --region not-a-region` exits 2 — and `create` already
    warns that a typo reads as a region with no quota. `quota list` was the one
    place that did not guard it."""
    result = quota_list(Cloud(quotas=pooled()), monkeypatch,
                        "--region", "not-a-region")

    assert result.exit_code == 2, result.output
    assert "not-a-region" in result.output


def test_the_invalid_region_message_is_about_the_region(monkeypatch):
    """F10. It read `no quota for 'l4' in not-a-region, it is metered in all
    regions, us-central1` — the card's two API dimension entries printed as prose
    and reading as a two-item region list — and then offered a list of CARDS,
    one of which was the card that had just been typed."""
    cloud = Cloud()
    result = quota_request(cloud, monkeypatch, "--gpu", "l4",
                           "--region", "not-a-region")

    assert result.exit_code == 2
    assert "metered in all regions," not in result.output, result.output
    assert "ask for one of" not in result.output, (
        "the fix offers cards when the problem is the region")


def test_the_project_ceiling_is_named_beside_the_cards_it_caps(monkeypatch):
    """F12. `GPUS-ALL-REGIONS` is 1 — the cap on total GPUs across every card and
    region — and it was rendered as one more row sorted in among the cards, with
    nothing saying it caps the ready ones above it. Against the goal sentence the
    surface reads as satisfied while the project can run one GPU at a time."""
    result = quota_list(Cloud(quotas=pooled()), monkeypatch)

    # Footnotes wrap, so a contiguous phrase may be split across lines. Third
    # time an assertion has missed a note that was printed; normalise first.
    flat = " ".join(result.output.split())
    assert "caps every card above it" in flat, (
        "the ceiling is listed as a row and never named as a ceiling")
    assert "is not a card" in flat


# --- A: the guard protects what the project HOLDS, not what it asked for ------


def test_a_granted_card_with_no_preference_is_still_protected(monkeypatch):
    """THE WORST DEFECT OF THE NIGHT, and six rounds of guard work never touched
    it because every test used a card that had a preference.

        comfy-qat quota request --gpu t4 --value 0
        -> --preferred-value=0, exit 0, no refusal, no LOWERING line

    T4 is granted at 1 and has no preference, so `matching_ask` returned None,
    `request_value` took the `existing is None` branch, and the tool built a
    command setting a working quota to zero. A decrease is auto-approved and
    unrecoverable.

    We guarded the number in a REQUEST. The thing that matters is the quota the
    project HOLDS, and a card can hold quota with no preference at all — which is
    true of six of the fourteen cards here, and they are the ones that work.
    """
    cloud = Cloud(quotas=[quota(T4, 1, locations=["us-central1"])])
    result = quota_request(cloud, monkeypatch, "--gpu", "t4", "--value", "0")

    assert cloud.submitted == [], "it set a working grant to zero"
    assert result.exit_code == 2


def test_zero_needs_its_own_flag_not_just_allow_lower(monkeypatch):
    """`--allow-lower` is a flag somebody may reasonably keep in a script for
    legitimate reductions; a `--value 0` typo beside it destroys quota that
    cannot be recovered by waiting. Zero gets a flag of its own so that no
    standing flag can authorise it by accident."""
    cloud = Cloud(quotas=[quota(T4, 1, locations=["us-central1"])])
    refused = quota_request(cloud, monkeypatch, "--gpu", "t4", "--value", "0",
                            "--allow-lower")
    assert cloud.submitted == [], refused.output
    assert "release" in refused.output.lower(), refused.output

    allowed = Cloud(quotas=[quota(T4, 1, locations=["us-central1"])])
    ok = quota_request(allowed, monkeypatch, "--gpu", "t4", "--value", "0",
                       "--allow-lower", "--release-quota")
    assert [flag(a, "preferred-value") for a in allowed.submitted] == ["0"]
    assert "1" in ok.output and "T4" in ok.output.upper(), (
        "what disappears was not named")


def test_the_floor_is_the_larger_of_the_grant_and_the_standing_request():
    """Both, because either can be the thing you lose. A grant of 4 with a
    standing request of 1 must not be trimmed to 1, and the reverse must not be
    trimmed to the grant."""
    from comfy_qa.quota import Target, request_value

    send, note = request_value(Target("Q"), standing(1), 2, quotas=[quota("Q", 4)])
    assert send == 4, f"trimmed a grant of 4 to {send}"
    assert "lower" in note and "holds" in note, note

    send, note = request_value(Target("Q"), standing(8), 2, quotas=[quota("Q", 1)])
    assert send == 8, f"trimmed a standing request of 8 to {send}"
    assert "standing request" in note, note

    # and the grant alone, with no preference at all — the six cards that work.
    send, note = request_value(Target("Q"), [], 2, quotas=[quota("Q", 4)])
    assert send == 4, f"a card with a grant and no preference was trimmed to {send}"

    # raising is never blocked by either.
    assert request_value(Target("Q"), standing(1), 8, quotas=[quota("Q", 4)]) == (8, "")


def test_the_grant_protects_through_the_cli_not_just_the_function(monkeypatch):
    """THE WIRING, which is where three of tonight's defects lived while the
    guard beside them was correct.

    Every other test of the floor calls `request_value` directly. This one goes
    through `quota request`, on the one input that ONLY the grant can refuse: a
    card granted 4, holding no preference at all, asked for 1. Passing
    `quotas=None` from the CLI leaves the function perfect and the product
    unprotected, and no direct test of `request_value` can see it.
    """
    cloud = Cloud(quotas=[quota(T4, 4, locations=["us-central1"])])
    result = quota_request(cloud, monkeypatch, "--gpu", "t4", "--value", "1")

    assert cloud.submitted == [], "trimmed a grant of 4 to 1 through the CLI"
    assert result.exit_code == 2
    assert "4" in result.output, result.output


def test_release_quota_alone_is_refused_when_nothing_is_held(monkeypatch):
    """The sibling of `test_release_quota_alone_is_not_enough`, which passes on a
    card that HOLDS something — so the floor refuses it and the two-flag rule is
    never actually exercised. A card holding nothing has no floor to catch it,
    and only the zero rule stands between one flag and a submitted 0.

    THE FIRST DRAFT OF THIS TEST NAMED A CARD THE FIXTURE DID NOT REPORT, so the
    command exited 2 at "this project reports no quota for 'l4'" without ever
    reaching `request_value`, and passed while the mutant it was written to kill
    walked straight past it. The card is reported here AT ZERO — present, so it
    resolves; holding nothing, so no floor can do the refusing."""
    cloud = Cloud(quotas=[quota(T4, 1, locations=["us-central1"]),
                          quota(L4, 0, locations=["us-central1"])])
    result = quota_request(cloud, monkeypatch, "--gpu", "l4", "--value", "0",
                           "--release-quota")

    assert cloud.submitted == [], "one flag sent a zero"
    assert result.exit_code == 2
    assert "--allow-lower" in result.output, result.output


def test_held_value_reads_the_largest_row_not_the_first():
    """A project carries several rows for one quota, and the floor has to be the
    biggest thing that could be lost. Taking the smallest would leave a grant of
    4 trimmable to 2 as long as some other row said 1."""
    from comfy_qa.quota import Target, held_value, request_value

    two_rows = [{"quotaId": "Q", "dimensionsInfos": [
        {"details": {"value": "1"}, "applicableLocations": ["us-central1"]},
        {"details": {"value": "4"}, "applicableLocations": ["europe-west4"]},
    ]}]
    assert held_value(Target("Q"), two_rows) == 4
    assert request_value(Target("Q"), [], 2, quotas=two_rows)[0] == 4


def test_held_value_does_not_count_a_grant_in_another_region():
    """The mirror of the defect `covers_dimensions` was split out to fix — a T4
    granted in forty-two other regions answering a question about this one. Here
    it would refuse a legitimate request rather than permit a bad one, which is
    the same bug wearing the opposite outcome."""
    from comfy_qa.quota import Target, held_value, request_value

    elsewhere = [quota("Q", 4, locations=["europe-west4"])]
    here = Target("Q", {"region": "us-central1"})
    assert held_value(here, elsewhere) is None
    assert request_value(here, [], 1, quotas=elsewhere) == (1, "")


def test_an_unlimited_grant_cannot_be_traded_for_a_number():
    """`UNLIMITED` is -1, so any `max()` over raw limits reads it as the smallest
    value there is and every request looks like a raise. This project really does
    hold an unlimited per-zone L4 allowance, so the case is live, not theoretical."""
    from comfy_qa.quota import UNLIMITED, Target, held_value, request_value

    send, note = request_value(Target("Q"), [], 8, quotas=[quota("Q", -1)])
    assert send is None, f"traded UNLIMITED for {send}"
    assert "UNLIMITED" in note, note

    forced, note = request_value(Target("Q"), [], 8, quotas=[quota("Q", -1)],
                                 allow_lower=True)
    assert forced == 8 and "UNLIMITED" in note, note

    # AND BESIDE A FINITE ROW, which is the only case where the short-circuit
    # earns anything: a plain `max` over (4, -1) is 4, so the unlimited row
    # disappears and asking for 8 reads as a raise.
    mixed = [{"quotaId": "Q", "dimensionsInfos": [
        {"details": {"value": "4"}, "applicableLocations": ["us-central1"]},
        {"details": {"value": "-1"}, "applicableLocations": ["us-central1"]},
    ]}]
    assert held_value(Target("Q"), mixed) == UNLIMITED
    assert request_value(Target("Q"), [], 8, quotas=mixed)[0] is None


# --- B/H1: a refusal in one region does not answer a question about another ---


def test_a_family_card_refused_elsewhere_is_still_asked_for_here():
    """`setup` is the command the whole feature exists to deliver, and it was the
    one surface that got this wrong. VERIFIED live, three surfaces disagreeing
    about the same card in the same minute:

        quota list --region europe-west1        H100 0 europe-west1 none — request it
        quota request --gpu h100 --region ...   builds a correct request, exit 0
        setup --dry-run --region europe-west1   Not asked again automatically

    H100 is metered under `GPUS-PER-GPU-FAMILY-per-project-region`, which Google
    keys on (gpu_family, region). The refusal was in us-central1. europe-west1 has
    never been asked, and it stocks `nvidia-h100-80gb`.
    """
    refused_usc1 = [preference(
        FAMILY, granted=0, preferred=1, state_detail=DENIED_DETAIL,
        name="h100-usc1",
        dimensions={"gpu_family": "NVIDIA_H100", "region": "us-central1"})]
    by_card = {a.card: a for a in
               plan_quota(THIS_PROJECT, refused_usc1, region="europe-west1")}

    assert by_card["h100"].outcome == REQUEST, by_card["h100"].detail


def test_a_refusal_naming_no_region_still_answers_every_region():
    """The other half, and the live case that created `asks_about` in the first
    place: `a100-80-euw4` was refused, and an all-regions request has different
    dimensions, so an exact match found nothing and `setup` re-asked for a card
    Google had already turned down. A refusal that names no region is about the
    card everywhere — only a refusal pinned to a DIFFERENT region is silent here.
    """
    from comfy_qa.quota import Target, asks_about

    everywhere = [preference(FAMILY, granted=0, preferred=1,
                             state_detail=DENIED_DETAIL, name="h100-any",
                             dimensions={"gpu_family": "NVIDIA_H100"})]
    pinned = Target(FAMILY, (("gpu_family", "NVIDIA_H100"),
                             ("region", "europe-west1")))
    assert len(asks_about(pinned, everywhere)) == 1, (
        "an unpinned refusal stopped answering for a pinned question")


# --- C/H2: quota request never looked at what the region actually sells -------


def test_quota_request_refuses_a_region_that_sells_no_such_card(monkeypatch):
    """THIRTEENTH SECOND-SITE. `quota list --region` consults the accelerator
    catalogue; `quota request --region` — the command that files the irrevocable
    thing — never did.

    VERIFIED live: africa-south1 has ZERO NVIDIA accelerators of any kind, and it
    is one of nine such regions inside the 43-region quota universe. The request
    exits 0 and builds a preference that can never be deleted, for a box that can
    never start.
    """
    cloud = Cloud(quotas=[quota(L4, 1, locations=REGIONS_43)],
                  accelerators=stocking("us-central1", "nvidia-l4"))
    result = quota_request(cloud, monkeypatch, "--gpu", "l4",
                           "--region", "africa-south1", "--dry-run")

    assert result.exit_code == 2, result.output
    assert "africa-south1" in result.output
    assert "us-central1" in result.output, (
        "refusing without naming a region that does stock it is half an answer")


def test_the_fix_line_does_not_send_you_to_a_region_with_no_gpus(monkeypatch):
    """The suggestion was `applicableLocations[0]` — first alphabetically, which
    on this project is africa-south1 — presented as "somewhere it is metered".
    Technically true and operationally useless."""
    # A REAL REGION THE PROJECT DOES NOT METER THIS CARD IN. It used to say
    # `not-a-region`, which now stops at the typo check (M11) before ever
    # reaching the line under test — the fixture would have gone on passing
    # while testing a different refusal entirely.
    cloud = Cloud(quotas=[quota(L4, 1, locations=["africa-south1", "us-central1"]),
                          quota(T4, 1, locations=["europe-west4"])],
                  accelerators=stocking("us-central1", "nvidia-l4"))
    result = quota_request(cloud, monkeypatch, "--gpu", "l4",
                           "--region", "europe-west4", "--dry-run")

    assert result.exit_code == 2
    assert "africa-south1" not in result.output, result.output
    assert "us-central1" in result.output, result.output


def test_a_catalogue_that_cannot_be_read_does_not_block_the_request(monkeypatch):
    """A FAILED LOOKUP IS NOT A FACT, for the sixth time in this feature. If the
    catalogue cannot be read the request proceeds and says the check was not
    made — refusing on a lookup failure would be inventing an absence, which is
    the defect class this rule exists to stop."""
    cloud = Cloud(quotas=[quota(L4, 1, locations=REGIONS_43)],
                  accel_error=GcloudError("catalogue unavailable"))
    result = quota_request(cloud, monkeypatch, "--gpu", "l4",
                           "--region", "africa-south1", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "not checked" in result.output.lower(), result.output


@pytest.mark.parametrize("how", ["error", "empty"])
def test_an_unreadable_catalogue_is_reported_as_not_checked_not_as_offered(
        how, monkeypatch):
    """`offered_here` has three states and the third is the one that keeps being
    lost: true, false, and **null — nobody looked**. A script gating on this must
    be able to tell "this region does not sell the card" from "the availability
    question was not answered", and collapsing the second onto either of the
    first two is the defect that has recurred six times in this feature.

    Both ways the lookup can come back empty-handed, because they are the same
    statement: `accelerator-types list` raising, and returning zero rows. Zero
    rows is not evidence that Google sells no GPU anywhere on Earth.
    """
    cloud = Cloud(quotas=[quota(L4, 1, locations=REGIONS_43)],
                  **({"accel_error": GcloudError("catalogue unavailable")}
                     if how == "error" else {"accelerators": []}))
    result = quota_list(cloud, monkeypatch, "--region", "us-central1", "--json")
    payload = json.loads(result.output[result.output.index("{"):])

    assert [g["offered_here"] for g in payload["gpus"]] == [None], (
        "an unreadable catalogue was rendered as a verdict")


def test_a_refusal_is_printed_once(monkeypatch):
    """FOUND BY RUNNING IT, with the suite green — for the fifth time in this
    feature, and the reason `docs/tests-that-cannot-fail.md` ends where it does.

    `say.result(note)` printed the sentence, then `say.fail(f"{name}: {note}")`
    printed the same sentence again four lines later. Every assertion in the
    suite used `in result.output`, which is true twice as easily as once."""
    cloud = Cloud(quotas=[quota(T4, 1, locations=["us-central1"])])
    result = quota_request(cloud, monkeypatch, "--gpu", "t4", "--value", "0")

    assert result.output.count("refusing to set") == 1, result.output


def test_the_lowering_sentence_reads_as_english():
    """"LOWERING the quota this project holds NVIDIA-T4-GPUS-per-project-region
    from 1 to 0" is what it printed — a missing preposition, visible the moment
    anybody ran it and invisible to every `in output` assertion in the suite."""
    from comfy_qa.quota import Target, request_value

    _, grant = request_value(Target("Q"), [], 0, quotas=[quota("Q", 4)],
                             allow_lower=True, release=True)
    _, asked = request_value(Target("Q"), standing(4), 1, allow_lower=True)
    for note in (grant, asked):
        assert " Q from " in note, note
        assert "holds Q" not in note, note


@pytest.mark.parametrize("flags", [
    (), ("--allow-lower",), ("--allow-lower", "--release-quota"),
])
def test_a_negative_value_is_refused_whatever_flags_are_passed(flags, monkeypatch):
    """H3's other half. Zero is a coherent request — hold none of this card — and
    is refused because it is destructive. A NEGATIVE is not even coherent, and no
    flag makes it so, so it is refused before the API is asked.

        $ comfy-qat quota request --gpu t4 --value -5 --dry-run
        exit=0
        gcloud quotas preferences update ... --preferred-value=-5 ... --allow-missing

    `--allow-missing` means that CREATES a new permanent preference. And with
    `--allow-lower` a negative reached a standing one: "LOWERING the standing
    request ... from 1 to -5". The only guard that could catch either was the
    don't-lower rule, which cannot fire on a card that has never been asked for —
    a guard satisfied by the absence of the thing it guards.
    """
    cloud = Cloud(quotas=[quota(T4, 1, locations=["us-central1"])])
    result = quota_request(cloud, monkeypatch, "--gpu", "t4", "--value", "-5",
                           *flags)

    assert cloud.submitted == [], f"sent a negative with {flags}"
    assert result.exit_code == 2
    assert "-5" in result.output, result.output


def test_the_count_of_other_regions_is_the_one_the_sentence_claims(monkeypatch):
    """FOUND BY READING THE LIVE OUTPUT, again:

        l4: africa-south1 does not offer this card — this project meters it in
            18 regions that do

    18 was the number of regions GOOGLE SELLS L4 in. The sentence says "this
    project meters it in", which is a different set, and on a project metered in
    one region the number would have been 18 regardless. A count is a claim; this
    one has to be the intersection, and the region offered as the remedy has to
    come out of the same set — advising a region the project does not meter sends
    somebody to file a request that cannot be granted where they asked.
    """
    cloud = Cloud(quotas=[quota(L4, 1, locations=["us-central1", "africa-south1"])],
                  accelerators=stocking("us-central1", "nvidia-l4")
                  + [{"name": "nvidia-l4", "zone": "https://x/zones/asia-east1-a"}])
    result = quota_request(cloud, monkeypatch, "--gpu", "l4",
                           "--region", "africa-south1", "--dry-run")

    assert result.exit_code == 2
    assert "1 region" in result.output, result.output
    assert "asia-east1" not in result.output, (
        "offered a region the project does not meter")
    assert "us-central1" in result.output, result.output


# --- M9-M12: the raw --quota-id path, and what lands on stdout ----------------


def test_dry_run_puts_only_commands_on_stdout(monkeypatch):
    """`--dry-run | sh` is the point of `--dry-run`, and this went into the pipe:

        gcloud quotas preferences update ... --preferred-value=1 ...
        keeping the standing request for GPUS-PER-GPU-FAMILY... at 8; 1 would lower it
        gcloud quotas preferences update ... --preferred-value=8 ...

    Line 2 contains a `;`, so the shell attempts `1 would lower it` as a command.
    The module is careful about this everywhere else — `quota list --json` keeps
    progress on stderr, and there is a comment at the top of this file about
    moving a stray `to fix:` off stdout for exactly this reason. Same rule,
    second site.
    """
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    cloud = Cloud(quotas=[quota(L4, 1, locations=["us-central1"])],
                  preferences=STANDING_L4_AT_8)
    with monkeypatch.context() as patch:
        patch.setattr("comfy_qa.auth.Gcloud", lambda *a, **k: cloud.gcloud())
        # Click 8.4 separates the streams by default and dropped `mix_stderr`;
        # `result.stdout` here is stdout alone, which is the whole point.
        result = CliRunner().invoke(
            app, ["quota", "request", "--no-wait", "--gpu", "l4", "--dry-run"])

    for line in result.stdout.splitlines():
        assert not line.strip() or line.startswith("gcloud "), (
            f"not a command, on stdout: {line!r}")
    assert "would lower it" in result.stderr, result.stderr


def test_a_raw_family_quota_id_is_refused_rather_than_sent_without_its_family(
        monkeypatch):
    """`GPUS-PER-GPU-FAMILY-per-project-region` says WHICH card in a dimension,
    and a raw `--quota-id` cannot supply it. The tool built the request anyway:

        --dimensions=region=us-central1 --allow-missing

    — no `gpu_family`, on the irrevocable path. `Target`'s own docstring says the
    family shape needs both dimensions. Worse, with no family the request matches
    no standing preference, so neither H100-at-8 nor RTX-PRO-6000-at-1 triggers
    the lowering guard: a whole class of protection silently switched off by the
    one flag that bypasses card resolution.
    """
    cloud = Cloud(quotas=[family(0, "NVIDIA_H100", locations=["us-central1"])])
    result = quota_request(cloud, monkeypatch, "--quota-id", FAMILY,
                           "--region", "us-central1", "--value", "1", "--dry-run")

    assert cloud.submitted == [], result.output
    assert result.exit_code == 2
    assert "--gpu" in result.output, "the remedy has to name the flag that works"


def test_a_typod_region_is_called_a_typo(monkeypatch):
    """"this project has no l4 quota in not-a-region" says the region is real and
    the quota is missing. Both halves are wrong, and the second sends the user
    into H2's bad remedy. The API reports the region universe; nothing checked
    against it."""
    cloud = Cloud(quotas=[quota(L4, 1, locations=["us-central1"])])
    result = quota_request(cloud, monkeypatch, "--gpu", "l4",
                           "--region", "not-a-region", "--dry-run")

    assert result.exit_code == 2
    assert "no such region" in result.output.lower(), result.output


def test_the_remedy_echoes_the_flag_the_user_typed(monkeypatch):
    """The user typed `--quota-id`; the fix line rewrote it to `--gpu` and the
    rewritten command exits 2. VERIFIED, not inferred."""
    cloud = Cloud(quotas=[quota(L4, 4, locations=["us-central1"])])
    result = quota_request(cloud, monkeypatch, "--quota-id", L4,
                           "--region", "us-central1", "--value", "1", "--dry-run")

    assert result.exit_code == 2
    assert f"--quota-id {L4}" in result.output, result.output
    assert f"--gpu {L4}" not in result.output, result.output


# --- M4/M5/L13/L16/L17/L18: one resolution, read by every surface -------------


def rtx_three_ways():
    """The live RTX PRO 6000 shape: refused per region under the family id, and
    granted across every region under Spot.

    Three quota rows, so `--by-region` has three distinct places to print. A
    fixture with one row cannot show three objects collapsing onto one string,
    which is the whole of M4.
    """
    # REPLACING the card's all-regions family row, not adding to it. Leaving it
    # in gave RTX three family rows, so the two refusals matched four region
    # slots and `refused_in_regions` read 4 — a number invented by the fixture,
    # in a test whose subject is whether a count says what it means.
    others = [q for q in THIS_PROJECT
              if not (q["quotaId"] == FAMILY
                      and any((d.get("dimensions") or {}).get("gpu_family") == RTX
                              for d in q["dimensionsInfos"]))]
    return others + [
        family(0, RTX, region="us-central1", locations=["us-central1"]),
        family(0, RTX, region="europe-west4", locations=["europe-west4"]),
        quota(SPOT_RTX, 1, locations=REGIONS_43),
    ]


def _json_of(cloud, monkeypatch, *args):
    out = quota_list(cloud, monkeypatch, "--json", *args)
    return json.loads(out.output[out.output.index("{"):])


def test_by_region_json_keeps_the_region_it_is_named_after(monkeypatch):
    """M4. `--by-region` exists to say WHICH REGION, and the pool override wrote
    the card-level geography over it. MEASURED, table against JSON, same run:

        RTX-PRO-6000   all regions   ready (Spot) 1 ... refused in 2 regions
        RTX-PRO-6000   europe-west4  ready (Spot) 1 ... refused in 1 region
        RTX-PRO-6000   us-central1   ready (Spot) 1 ... refused in 1 region

        [{"region": "43 regions", ...}, {"region": "43 regions", ...},
         {"region": "43 regions", ...}]

    Two of the three objects are byte-identical, a consumer keying on
    `(gpu, region)` collides, and the two places actually refused are
    unrecoverable from the JSON. The table already fixes this in
    `rendered(per_region=True)`; `--json` called `resolved()` directly — a fifth
    reading of a value the docstring says is decided once.
    """
    cloud = Cloud(quotas=rtx_three_ways(), preferences=DENIED_RTX)
    rows = [r for r in _json_of(cloud, monkeypatch, "--by-region")["by_region"]
            if r["gpu"] == "RTX-PRO-6000"]
    assert len(rows) > 1, ("one row cannot show several collapsing onto one "
                           "string, which is the whole finding")

    # The table's own REGION column, read off the header's column positions —
    # not re-parsed by guesswork, which is how the first version of this
    # assertion compared two things it had itself invented.
    lines = quota_list(cloud, monkeypatch, "--by-region").output.splitlines()
    header = next(l for l in lines if l.startswith("GPU "))
    start, stop = header.index("REGION"), header.index("LIMIT")
    from_table = sorted(l[start:stop].strip() for l in lines
                        if l.startswith("RTX-PRO-6000"))

    assert sorted(r["region"] for r in rows) == from_table, (rows, from_table)


def test_the_json_names_the_quota_id_the_limit_came_from(monkeypatch):
    """M5. The object asserted `"quota_id": "GPUS-PER-GPU-FAMILY-per-project-region"`
    beside `"limit": 1` and `"pool": "Spot"`. Live, that family id reports no
    value at all for RTX PRO 6000 — 0 in every one of its dimension rows. The 1
    comes from `PREEMPTIBLE-NVIDIA-RTX-PRO-6000-GPUS-per-project-region`, which
    the record never named. Limit, pool and id have to be about one thing."""
    cloud = Cloud(quotas=rtx_three_ways(), preferences=DENIED_RTX)
    rows = [r for r in _json_of(cloud, monkeypatch, "--by-region")["by_region"]
            if r["gpu"] == "RTX-PRO-6000"]

    assert {r["pool"] for r in rows} == {"Spot"}, rows
    assert {r["quota_id"] for r in rows} == {SPOT_RTX}, [r["quota_id"] for r in rows]


def test_the_counts_say_which_pool_they_are_about(monkeypatch):
    """L18. `refused_in_regions: 2` sits beside `pool: "Spot"`, and the Spot pool
    has no refusals at all — the two refusals are the family request. The table
    said it as one sentence, "ready (Spot) 1 — reclaimable mid-run, refused in 2
    regions", which reads as the Spot grant having been refused."""
    cloud = Cloud(quotas=rtx_three_ways(), preferences=DENIED_RTX)
    card = next(r for r in _json_of(cloud, monkeypatch)["gpus"]
                if r["gpu"] == "RTX-PRO-6000")

    assert card["refused_in_regions"] == 2, card
    assert card["counts_pool"] == "on-demand", card

    line = next(l for l in quota_list(cloud, monkeypatch).output.splitlines()
                if l.startswith("RTX-PRO-6000"))
    assert "on-demand refused in 2 regions" in line, line


def test_json_and_table_order_the_cards_the_same_way(monkeypatch):
    """L17. The table put RTX-PRO-6000 in the ready block; `--json` put it after
    the three denied cards while reporting `"status": "ready"` — sorting on the
    on-demand status and reporting the pool-resolved one."""
    cloud = Cloud(quotas=rtx_three_ways(), preferences=DENIED_RTX)
    from_json = [r["gpu"] for r in _json_of(cloud, monkeypatch)["gpus"]]

    lines = quota_list(cloud, monkeypatch).output.splitlines()
    header = next(l for l in lines if l.startswith("GPU "))
    body = lines[lines.index(header) + 1:]
    # STOP AT THE BLANK LINE. Filtering on `startswith("note")` kept the WRAPPED
    # continuation lines of each footnote, so the "table" this compared against
    # had prose in it.
    body = body[:body.index("")] if "" in body else body
    from_table = [l[:header.index("LIMIT")].strip() for l in body]
    assert from_json == from_table, (from_json, from_table)


def test_the_global_ceiling_is_not_reported_as_offered_here(monkeypatch):
    """L13. The availability check skips `any (global)` — "a ceiling, not a card;
    nothing to look up" — and `offered_here` then fell through to `true`. The
    same record already reports `drivable: null` for it. A check deliberately not
    made must not render as a check that passed."""
    cloud = Cloud(quotas=rtx_three_ways(),
                  accelerators=stocking("us-central1", "nvidia-l4"))
    ceiling = next(r for r in
                   _json_of(cloud, monkeypatch, "--region", "us-central1")["gpus"]
                   if r["gpu"] == GLOBAL_ALLOWANCE)

    assert ceiling["offered_here"] is None, ceiling


def test_a_card_absent_here_explains_its_limit_whichever_pool_won(monkeypatch):
    """L16.

        L4                 1  africa-south1  not offered here — 1 of quota held
        RTX-PRO-6000       1  africa-south1  not offered here — not creatable ...

    Both show LIMIT 1 and only one explains it. `status_of` was handed
    `row.limit`, the raw on-demand value — 0 for a card held only through Spot —
    while the column beside it printed the resolved one. The same fifth reading
    as M4, one function over.
    """
    # The card's family row spans every region and its Spot grant does too, which
    # is the live shape — `rtx_three_ways` pins the family rows to two regions,
    # so the card has no row in africa-south1 at all and the branch under test is
    # never reached.
    cloud = Cloud(quotas=THIS_PROJECT + [quota(SPOT_RTX, 1, locations=REGIONS_43)],
                  accelerators=stocking("us-central1", "nvidia-l4"))
    line = next(l for l in quota_list(cloud, monkeypatch, "--region",
                                      "africa-south1").output.splitlines()
                if l.startswith("RTX-PRO-6000"))

    assert "not offered here" in line, line
    assert "1 of quota held" in line, line


# --- M6/M7/M8/L15: names, remedies, and a bound at the top ---------------------


def test_one_card_has_one_name_across_every_command(monkeypatch):
    """M7. `quota list` and `--json` call it H100; `setup` and `create` call it
    H100-80GB. `create --gpu h100` prints both in one run — "H100-80GB: 0" in the
    quota block, then "this project has no H100 quota" in the error — and nothing
    says they are the same card. `quota list` is the command the other two point
    at, and it is the one showing the name they do not use.

    The card table already holds the join: `quota_family="NVIDIA_H100"` with
    `quota_aliases=("H100",)`. The name a row is about is the CARD's name.
    """
    cloud = Cloud(quotas=THIS_PROJECT)
    shown = {l.split()[0] for l in quota_list(cloud, monkeypatch).output.splitlines()
             if l[:1].isalnum()}
    planned = {a.label for a in plan_quota(THIS_PROJECT, PREFS_NONE)}

    assert "H100-80GB" in shown, sorted(shown)
    assert "H100" not in shown, (
        "two names for one card, on the command the others point at")
    for label in planned:
        if label.upper().startswith("H100"):
            assert label in shown or label == "H100-80GB", (label, sorted(shown))


def test_an_unknown_card_is_not_reported_as_one_holding_quota(monkeypatch):
    """M8. `--gpu banana` answered "banana: not creatable by this tool, whatever
    quota it holds" — byte-identical to the answer for RTX-PRO-6000, which is a
    real card holding real quota. The sentence asserts that `banana` holds quota
    and sends the reader to look it up in `quota list`, where it will never be.
    Absent versus zero, in a message."""
    cloud = Cloud(quotas=THIS_PROJECT)
    unknown = quota_request(cloud, monkeypatch, "--gpu", "banana", "--dry-run")
    real = quota_request(cloud, monkeypatch, "--gpu", "b200", "--dry-run")

    assert unknown.exit_code == 2 and real.exit_code == 2
    assert "no card called 'banana'" in unknown.output, unknown.output
    assert "whatever quota it holds" not in unknown.output, unknown.output
    assert "whatever quota it holds" in real.output, real.output


def test_a_metered_card_create_cannot_order_is_not_called_unknown():
    """M8's mirror. `create --gpu b200` answered "no card called 'b200'" — the
    same sentence as `--gpu banana` — while `quota list` prints a B200 row every
    time. One surface says the card does not exist; the other lists it."""
    from comfy_qa.create import LifecycleError, card_for

    with pytest.raises(LifecycleError) as unknown:
        card_for("banana")
    assert "no card called" in str(unknown.value)

    with pytest.raises(LifecycleError) as metered:
        card_for("b200")
    assert "no card called" not in str(metered.value), str(metered.value)
    assert "b200" in str(metered.value).lower()


def test_an_absurd_value_is_named_rather_than_sent_silently(monkeypatch):
    """L15. `--value 999999` exits 0 with no warning. It is harmless next to a
    zero — a request too large is refused by Google, not granted — but it is the
    same missing bound, and a person who typed it wants to know. Warned, not
    refused: asking for headroom is legitimate, and the number that says so is
    derived from the card table rather than invented here."""
    cloud = Cloud(quotas=[quota(T4, 1, locations=["us-central1"])])
    result = quota_request(cloud, monkeypatch, "--gpu", "t4",
                           "--value", "999999", "--dry-run")

    assert result.exit_code == 0, result.output
    assert "999999" in result.output and "most" in result.output.lower(), result.output
    # And the command it would run is unchanged — this warns, it does not clamp.
    assert "--preferred-value=999999" in result.output, result.output


def test_one_geography_has_one_phrasing(monkeypatch):
    """L14. Three vocabularies for the same forty-three regions:

        quota list    L4 ... all regions        (Row.where)
        quota list    RTX-PRO-6000 ... 43 regions   (Pool.where)
        create        L4: 1, in 43 region(s)    (create's own sentence)

    Nothing reconciled them, so a reader comparing two lines of output had to
    know which object produced each. One label function, read by all three.
    """
    from comfy_qa.quota import spans_many, where_label

    assert where_label({"global"}) == "global"
    assert where_label({"us-central1"}) == "us-central1"
    assert where_label(set(REGIONS_43)) == f"{len(REGIONS_43)} regions"
    assert spans_many(where_label(set(REGIONS_43)))
    assert not spans_many("us-central1")

    cloud = Cloud(quotas=THIS_PROJECT + [quota(SPOT_RTX, 1, locations=REGIONS_43)])
    lines = quota_list(cloud, monkeypatch).output.splitlines()
    header = next(l for l in lines if l.startswith("GPU "))
    start, stop = header.index("WHERE"), header.index("STATUS")
    places = {l[start:stop].strip() for l in lines
              if l.startswith(("L4 ", "RTX-PRO-6000 ", "H100-80GB "))}

    assert places, "the fixture produced none of the cards under test"
    assert "all regions" not in places, places
    assert any(p.endswith(" regions") for p in places), places


def test_create_does_not_invent_a_region_for_the_remedy():
    """M6. `create --gpu h100` printed:

        to fix: comfy-qat quota request --gpu h100 --region us-central1, then wait

    us-central1 is precisely where H100 was refused, and the user never named it.
    The comment above that line already says "THE REGION THE USER ASKED FOR, not
    a literal" — and the fallback literal was still there, and its sibling six
    lines down had not been corrected at all. `quota request` derives a region
    when none is given, checks availability and knows about refusals; `create`
    knows none of that, so it must not guess.
    """
    from comfy_qa.create import QuotaCheck

    def check(**kw):
        fields = dict(card="H100-80GB", card_limit=0, global_limit=1, needed=8,
                      running=(), regions=(), key="h100", quota_name="H100-80GB")
        return QuotaCheck(**{**fields, **kw})

    assert "--region" not in check(asked_region="").problem().fix
    assert "--region europe-west1" in check(
        asked_region="europe-west1").problem().fix

    # AND THE SIBLING SIX LINES DOWN, which still said `--region us-central1`
    # outright — the correction landed on one branch and not on the other, which
    # is the shape this feature has produced fourteen times now.
    short = check(asked_region="", card_limit=1, regions=("us-central1",))
    assert "--region" not in short.problem().fix, short.problem().fix

    # And the third, "the grant names no region", found by grep once the second
    # turned up beside the first.
    nowhere = check(asked_region="", card_limit=1, regions=())
    assert "--region" not in nowhere.problem().fix, nowhere.problem().fix


def test_quota_request_says_when_google_has_already_refused_this(monkeypatch):
    """M6's other half. `setup` refuses to re-file a denied request and says so;
    `create` told the user to do it by hand; `quota request` did it without
    comment, re-submitting the denied preference unchanged and then waiting for
    an answer already given.

    WARNED, NOT REFUSED, and the difference is deliberate: `setup` is automatic,
    so it declines; this command is what somebody types on purpose, and there has
    to be a way to re-ask once the thing a reviewer reads has changed. What there
    must not be is silence.
    """
    # A CARD THIS TOOL CAN ACTUALLY ORDER. `--gpu rtx-pro-6000` is refused several
    # checks earlier as not creatable, so it never reaches the branch under test.
    # METERED WHERE THE REFUSAL WAS. `THIS_PROJECT` meters A100-80GB in
    # us-central1 only, so the command stopped at "no quota in europe-west4"
    # before reaching the branch under test.
    cloud = Cloud(quotas=[quota(A100_80, 0, locations=["europe-west4"])],
                  preferences=PREFS_DENIED)
    result = quota_request(cloud, monkeypatch, "--gpu", "a100-80gb",
                           "--region", "europe-west4", "--value", "1", "--dry-run")

    assert "already refused" in result.output.lower(), result.output
    assert "a100-80-euw4" in result.output, (
        "the preference id is what a person needs to look it up")


def test_the_region_universe_is_regions_and_not_zones():
    """FOUND BY RUNNING IT, in a fix written one pass earlier in this same
    session:

        no such region 'not-a-region' — this project's quota names 174, and
        that is not one of them

    174 is not a number of regions. A project carries per-ZONE quota rows beside
    its per-region ones — `NVIDIA-L4-GPUS-per-project-zone` across 130 zones —
    and `known_regions` swept up `row.locations` from both. The count is a claim,
    the sentence says "regions", and 174 of them do not exist.

    Live, the same command reports 43 in `--by-region`, so the tool contradicted
    itself in two lines of its own output.
    """
    from comfy_qa.quota import known_regions

    zoned = [{"quotaId": "NVIDIA-L4-GPUS-per-project-zone", "dimensionsInfos": [
        {"details": {"value": "1"},
         "applicableLocations": ["us-central1-a", "us-central1-b",
                                 "europe-west4-a"]}]}]
    regional = [quota(L4, 1, locations=["us-central1", "asia-east1"])]

    assert known_regions(zoned) == {"us-central1", "europe-west4"}
    assert known_regions(zoned + regional) == {
        "us-central1", "europe-west4", "asia-east1"}


def test_a_card_metered_by_region_and_by_zone_is_counted_once():
    """FOUND BY RUNNING IT, and the live line was:

        A100   0  us-central1  denied — refused in 1 region, never asked in 172

    172 regions do not exist. Most cards are metered TWICE — once
    `-per-project-region` across 43 regions and once `-per-project-zone` across
    130 zones — and `readiness` built a row from each, labelled the zone row's
    130 places as "130 regions", and `summarise` then SUMMED the two counts.

    `_for_card` has had the rule all along: "the region-scoped rows are the ones
    that bind, so where there are any they are the only ones read." `readiness`
    was the surface that did not apply it, which is also why the row collapse
    below it needed a comment about the card appearing twice.
    """
    from comfy_qa.quota import readiness, summarise, where_label

    regions = [f"r{i}" for i in range(43)]
    zones = [f"{r}-{z}" for r in regions[:3] for z in "abc"]
    both = [
        {"quotaId": "NVIDIA-A100-GPUS-per-project-region",
         "dimensionsInfos": [{"details": {}, "applicableLocations": regions}]},
        {"quotaId": "NVIDIA-A100-GPUS-per-project-zone",
         "dimensionsInfos": [{"details": {}, "applicableLocations": zones}]},
    ]

    # A zone list is a set of REGIONS, and nine zones in three regions is three.
    assert where_label(zones) == "3 regions"

    card = summarise(readiness(both))
    assert [(c.gpu, c.where, c.never_asked_in) for c in card] == [
        ("A100", "43 regions", 43)]


def test_create_does_not_print_two_names_for_one_card():
    """M7, the half that survived the first fix. `create --gpu h100` printed:

        quota checked:
          H100-80GB: 0
        this project has no H100 quota, so a H100 box cannot start anywhere.

    Two names for one card, four lines apart, in the run the report quoted. The
    error took `quota_names[-1]` — the ALIAS — under a comment explaining that
    "the quota table has an `H100` row and no `H100-80GB` row", which was true
    when it was written and stopped being true the moment `family_name` started
    returning the card table's own name. A rationale left in place is a trap.

    THROUGH `check_quota`, which is where the name is chosen. Handing
    `QuotaCheck` the right name and asserting it comes back out tests nothing:
    the defect is the constructor picking `quota_names[-1]`.
    """
    from comfy_qa.create import CARDS, check_quota

    check = check_quota(CARDS["h100"], THIS_PROJECT, [])
    problem = str(check.problem())

    assert "H100-80GB" in problem, problem
    assert "no H100 quota" not in problem, problem
    assert check.quota_name == check.card, (check.quota_name, check.card)


def test_global_is_not_one_of_the_regions():
    """`global` is where the project-wide ceiling lives, not a region somebody
    can ask for quota in. Live, the count read 44 where `--by-region` says 43,
    and the sentence around it is "this project's quota names N [regions]"."""
    from comfy_qa.quota import known_regions

    ceiling = [{"quotaId": "GPUS-ALL-REGIONS-per-project", "dimensionsInfos": [
        {"details": {"value": "1"}, "applicableLocations": ["global"]}]}]
    assert known_regions(ceiling) == set()
    assert known_regions(ceiling + [quota(L4, 1, locations=["us-central1"])]) == {
        "us-central1"}


def test_a_zone_only_card_counts_regions_not_zones():
    """The case `_prefer_region_scope` leaves behind. Once the zone-scoped rows
    are dropped wherever a region-scoped row exists, `len(row.locations)` is
    almost always a region count already — so the one card that still reaches
    that line with ZONES in it is a card metered only by zone, and it is the only
    place the difference can be seen. A mutation sweep found this: reverting the
    count to `len(row.locations)` killed nothing."""
    from comfy_qa.quota import readiness

    zones = [f"{r}-{z}" for r in ("r1", "r2", "r3") for z in "abc"]
    only_zoned = [{"quotaId": "NVIDIA-T4-GPUS-per-project-zone",
                   "dimensionsInfos": [{"details": {},
                                        "applicableLocations": zones}]}]
    row = readiness(only_zoned)[0]

    assert row.never_asked_in == 3, (
        f"nine zones in three regions counted as {row.never_asked_in}")
    assert row.region == "3 regions", row.region


# --- a refusal with no remedy must not borrow one -----------------------------


def test_a_negative_refusal_does_not_offer_a_flag_that_cannot_permit_it(
        monkeypatch):
    """SIXTH OCCURRENCE OF ONE SHAPE: a fix line naming a remedy that cannot
    work. Its siblings were `none — request it` on a card nothing can request,
    `--region africa-south1` as somewhere to ask, and a remedy that rewrote
    `--quota-id` to `--gpu` and exited 2.

        t4: -5 is not a number of GPUs; NVIDIA-T4-GPUS-per-project-region was
            not asked for
        to fix: comfy-qat quota — what this project holds
                --allow-lower   # send it anyway, knowing it may replace a
                                # larger standing request

    `--allow-lower` cannot permit a negative — nothing can, by design — so
    someone runs it, is refused again, and concludes the tool is broken.

    THE BUG IS THE APPEND, not this instance: `send is None` has three causes and
    the block offered two remedies, so the third inherited whichever branch it
    fell through to. A refusal with no remedy must say what to type instead.
    """
    cloud = Cloud(quotas=[quota(T4, 1, locations=["us-central1"])])
    result = quota_request(cloud, monkeypatch, "--gpu", "t4", "--value", "-5")

    assert result.exit_code == 2
    assert "--allow-lower" not in result.output, result.output
    assert "--release-quota" not in result.output, result.output
    assert "--value 1" in result.output, (
        "a refusal with no flag remedy still has to say what to type instead")


def test_every_cause_of_a_refused_value_gets_its_own_remedy(monkeypatch):
    """The FOUR causes, side by side, so a fifth cannot quietly inherit another's
    remedy. Each line is the flag that actually changes the outcome.

    It said three, and there were four — the UNLIMITED case had been added to
    `request_value` without anything here noticing, so it fell into the branch
    for "the standing value could not be read" and was told it might replace a
    request that does not exist. An enumeration in a comment does not notice a
    new arrival. This does, because it fails the moment a cause has no line of
    its own."""
    held = [quota(T4, 1, locations=["us-central1"])]

    zero = quota_request(Cloud(quotas=held), monkeypatch, "--gpu", "t4",
                         "--value", "0")
    assert "--release-quota" in zero.output, zero.output

    negative = quota_request(Cloud(quotas=held), monkeypatch, "--gpu", "t4",
                             "--value", "-5")
    assert "--value 1" in negative.output and "--allow-lower" not in negative.output

    unreadable = quota_request(Cloud(quotas=THIS_PROJECT,
                                     preferences=UNREADABLE_A100),
                               monkeypatch, "--gpu", "a100", "--value", "1")
    assert "--allow-lower" in unreadable.output, unreadable.output
    assert "--release-quota" not in unreadable.output, unreadable.output
    assert "standing request" in unreadable.output, unreadable.output

    unlimited = quota_request(
        Cloud(quotas=[{"quotaId": "NVIDIA-T4-GPUS-per-project-zone",
                       "dimensionsInfos": [
                           {"details": {"value": "-1"},
                            "applicableLocations": ["us-central1-a"]}]}]),
        monkeypatch, "--quota-id", "NVIDIA-T4-GPUS-per-project-zone",
        "--value", "1")
    assert "unlimited grant" in unlimited.output, unlimited.output
    assert "standing request" not in unlimited.output, unlimited.output


def test_the_region_typo_remedy_is_a_command_that_runs(monkeypatch):
    """SEVENTH, and I wrote it this round. The typo refusal hands over

        comfy-qat quota request --region asia-east1   # did you mean ...

    with no `--gpu`, and that command exits 2 at "name a card to ask for". The
    card the user typed is right there in the invocation being refused.
    """
    cloud = Cloud(quotas=[quota(L4, 1, locations=["us-central1", "asia-east1"])])
    result = quota_request(cloud, monkeypatch, "--gpu", "l4",
                           "--region", "asia-east99", "--dry-run")

    assert result.exit_code == 2
    line = next(l for l in result.output.splitlines()
                if "quota request" in l and "--region" in l)
    assert "--gpu l4" in line, line


def test_no_metered_region_stocks_it_offers_no_region_at_all(monkeypatch):
    """EIGHTH. The "no quota in <region>" remedy falls back to
    `applicableLocations[0]` when nothing the project meters is also stocked —
    which is `--region africa-south1` again, one branch over from where it was
    fixed. Running the suggestion exits 2 at the availability check.

    A fallback is only honest when the check could not be made. When it WAS made
    and came back empty, there is no region to name and the remedy must say so.
    """
    cloud = Cloud(quotas=[quota(L4, 1, locations=["africa-south1"]),
                          quota(T4, 1, locations=["us-central1"])],
                  accelerators=stocking("us-central1", "nvidia-l4"))
    result = quota_request(cloud, monkeypatch, "--gpu", "l4",
                           "--region", "us-central1", "--dry-run")

    assert result.exit_code == 2
    assert "africa-south1" not in result.output, result.output


# --- the structural guard for the shape that came back eight times ------------


REMEDY_CASES = [
    # (args, a cloud that makes this refusal happen, must a `quota request`
    #  remedy be printed at all)
    #
    # THE THIRD COLUMN ARRIVED LATE. Without it the guard only checked the shape
    # of remedies that WERE printed, so a refusal that withheld the working one
    # passed it — and that is exactly the defect the fifth pass found on the
    # h100-at-zero path. Every fixture here also held quota at 1, so not one of
    # them could reach the zero-limit branch where it happened.
    (("--gpu", "h100", "--region", "africa-south1"),
     dict(quotas=[family(0, "NVIDIA_H100", locations=REGIONS_43)],
          accelerators=stocking("asia-east1", "nvidia-h100-80gb")), True),
    (("--gpu", "l4", "--region", "asia-east99"),
     dict(quotas=[quota(L4, 1, locations=["us-central1", "asia-east1"])]), True),
    # No region is both metered and stocked, so there is genuinely no request to
    # suggest and the remedy says so instead of naming one.
    (("--gpu", "l4", "--region", "us-central1"),
     dict(quotas=[quota(L4, 1, locations=["africa-south1"]),
                  quota(T4, 1, locations=["us-central1"])],
          accelerators=stocking("us-central1", "nvidia-l4")), False),
    # WITH A CATALOGUE. Without one the availability check correctly answers
    # "not checked" and the command exits 0, so the case would have been a
    # parametrisation that never reached the refusal it names.
    (("--gpu", "l4", "--region", "africa-south1"),
     dict(quotas=[quota(L4, 1, locations=REGIONS_43)],
          accelerators=stocking("us-central1", "nvidia-l4")), True),
    # The remedy here is two flags, not a command to paste.
    (("--gpu", "t4", "--value", "0"),
     dict(quotas=[quota(T4, 1, locations=["us-central1"])]), False),
    (("--gpu", "t4", "--value", "-5"),
     dict(quotas=[quota(T4, 1, locations=["us-central1"])]), True),
    (("--gpu", "banana"), dict(quotas=THIS_PROJECT), True),
    # "ask for one that works: ..." — a list of cards, not an invocation.
    (("--gpu", "p100"), dict(quotas=THIS_PROJECT), False),
    (("--quota-id", FAMILY, "--region", "us-central1", "--value", "1"),
     dict(quotas=[family(0, "NVIDIA_H100", locations=["us-central1"])]), True),
    (("--quota-id", L4, "--region", "us-central1", "--value", "1"),
     dict(quotas=[quota(L4, 4, locations=["us-central1"])]), True),
    (("--gpu", "h100", "--value", "1"),
     dict(quotas=THIS_PROJECT, preferences=STANDING_H100_AT_8), True),
]


@pytest.mark.parametrize("args, fixture, needs_request", REMEDY_CASES)
def test_a_quota_request_remedy_always_names_what_to_ask_for(args, fixture,
                                                             needs_request,
                                                             monkeypatch):
    """EIGHT INSTANCES OF ONE SHAPE, so this is the rule rather than a ninth fix.

    A fix line naming a remedy that cannot work has now appeared as `none —
    request it` on a card nothing can request, `--region africa-south1` as
    somewhere to ask, a remedy rewriting `--quota-id` to `--gpu`, `--allow-lower`
    offered for a negative that no flag can permit, `quota request --region X`
    with no card, and the metered-but-unstocked fallback.

    Six of the eight were `comfy-qat quota request ...` missing or misnaming the
    thing to ask for — and that command refuses without one, so the remedy exits
    2 the moment anybody pastes it. That is mechanically checkable, and this is
    the check: every printed `quota request` invocation carries `--gpu` or
    `--quota-id`, unless it is a template with a `<placeholder>` a person is
    expected to fill in.
    """
    import shlex

    result = quota_request(Cloud(**fixture), monkeypatch, *args, "--dry-run")
    assert result.exit_code == 2, f"this case no longer refuses: {result.output}"

    printed = re.findall(r"comfy-qat quota request [^,;\n#]*", result.output)
    # `printed or "quota request" not in output` PASSED WHEN NOTHING WAS PRINTED,
    # which is how the h100-at-zero case — where the useful remedy was being
    # withheld entirely — sailed through a guard written about remedies. A guard
    # that only checks the shape of what IS printed cannot see what is missing.
    if needs_request:
        assert printed, (
            f"no `quota request` remedy at all, on a refusal that has one:\n"
            f"{result.output}")
    for invocation in printed:
        parts = shlex.split(invocation.strip())
        if any("<" in part for part in parts):
            continue  # a template, filled in by the reader
        assert {"--gpu", "--quota-id"} & set(parts), (
            f"remedy names nothing to ask for: {invocation!r}\n{result.output}")

        # AND IT CARRIES THE REGION WHEN ONE IS IN PLAY. "Names a card" passed a
        # remedy that stripped `--region` and therefore filed into a DIFFERENT
        # region from the one being discussed — the tenth instance of this class
        # and the first that succeeded at doing the wrong thing. A remedy that
        # can carry a region and does not is a remedy about somewhere else.
        asked = dict(zip(args, args[1:])).get("--region")
        if asked and asked in result.output and "--region" not in parts:
            raise AssertionError(
                f"remedy drops the region under discussion: {invocation!r}\n"
                f"{result.output}")


# --- the REGION column has to contain regions ---------------------------------


def test_the_by_region_view_never_prints_a_count_where_a_region_goes(monkeypatch):
    """FOURTEENTH SECOND-SITE, and the same class fixed earlier this round:

        GPU            REGION        LIMIT  STATUS
        K80            19 regions        1  ready — this tool cannot drive it
        L4             42 regions        1  ready

    `19 regions` is a count under a column headed REGION, in the one view whose
    entire purpose is to name places. `where_label` calling nine zones "9
    regions" was the same mistake one function over, and this is where it lands.

    The row is real — an undimensioned catch-all allowance covering many
    locations, with no single place to name — so the fix is not to invent one. It
    is to stop the cell reading like one.
    """
    cloud = Cloud(quotas=THIS_PROJECT + [quota(SPOT_RTX, 1, locations=REGIONS_43)])
    lines = quota_list(cloud, monkeypatch, "--by-region").output.splitlines()
    header = next(l for l in lines if l.startswith("GPU "))
    start, stop = header.index("REGION"), header.index("LIMIT")
    body = lines[lines.index(header) + 1:]
    body = body[:body.index("")] if "" in body else body
    places = {l[start:stop].strip() for l in body}

    assert places, "no rows, so nothing was checked"
    for place in places:
        assert not re.fullmatch(r"\d+ regions", place), (
            f"{place!r} is a count, in the column that names places")


def test_the_by_region_json_carries_the_same_correction(monkeypatch):
    """Thirteen second-sites say `--json` will have inherited it, and it had:
    `"region": "4 regions"` on the same rows. A consumer keying on `(gpu, region)`
    gets a bucket name where it expects a place."""
    cloud = Cloud(quotas=THIS_PROJECT + [quota(SPOT_RTX, 1, locations=REGIONS_43)])
    # Without the table flag, for the reason above.
    payload = json.loads(quota_list(cloud, monkeypatch, "--json")
                         .output.split("\n", 1)[1])

    for row in payload["by_region"]:
        assert not re.fullmatch(r"\d+ regions", row["region"]), row


def test_by_region_checks_availability_because_every_row_names_a_region(
        monkeypatch):
    """The caveat IS on this view — "STATUS reports quota held, not whether a
    region offers the card" is the second footnote. It is still the wrong answer
    here, and the reason is what `--by-region` is:

        K80  asia-east1   1  ready
        K80  asia-east2   1  ready        ... and seventeen more

    K80 is not in GCE's accelerator catalogue at all, anywhere. The collapsed
    table says `19 regions` and can be read as a summary; this view asserts
    `ready` against nineteen NAMED regions, so it makes nineteen specific claims
    that are each false, and the caveat's own remedy — "add --region" — collapses
    the view the reader just asked for.

    The check is free here in a way it is not in the collapsed view: the
    catalogue is ONE call for all 543 rows, and every row already names the
    region to test it against. The reason the collapsed table only checks under
    `--region` — "a call per card per region" — does not apply.

    A row whose place is a bucket (`any of 42`) still cannot be checked against
    one region, and stays unchecked. That is the honest half.
    """
    # ONE ENTRY PER REGION, which is what produces NAMED rows — a single entry
    # with two locations collapses to one bucket row and the branch under test
    # is never reached.
    per_region = {"quotaId": K80, "dimensionsInfos": [
        {"details": {"value": "1"}, "applicableLocations": ["asia-east1"]},
        {"details": {"value": "1"}, "applicableLocations": ["us-central1"]},
    ]}
    cloud = Cloud(quotas=[per_region, quota(L4, 1, locations=["us-central1"])],
                  accelerators=stocking("us-central1", "nvidia-l4"))
    lines = quota_list(cloud, monkeypatch, "--by-region").output.splitlines()

    k80 = [l for l in lines if l.startswith("K80 ")]
    assert len(k80) == 2, k80
    for line in k80:
        assert "not offered here" in line, line

    l4 = next(l for l in lines if l.startswith("L4 "))
    assert "not offered here" not in l4, l4


def test_the_by_region_json_carries_the_per_row_availability_verdict(monkeypatch):
    """The fifteenth second-site, checked before it could become one. The table
    now answers availability per row; `--json` reads `_offered_flag`, which keys
    on the single `--region` and knows nothing about a row's own place."""
    per_region = {"quotaId": K80, "dimensionsInfos": [
        {"details": {"value": "1"}, "applicableLocations": ["asia-east1"]},
        {"details": {"value": "1"}, "applicableLocations": ["us-central1"]},
    ]}
    cloud = Cloud(quotas=[per_region, quota(L4, 1, locations=["us-central1"])],
                  accelerators=stocking("us-central1", "nvidia-l4"))
    # WITHOUT `--by-region`, per the rule this round produced: a test asserting
    # on a JSON array must drive the invocation without the flag that shapes the
    # corresponding table, because `--json` emits the array either way and that
    # flag is what masked the defect for two rounds.
    payload = json.loads(quota_list(cloud, monkeypatch, "--json")
                         .output.split("\n", 1)[1])
    by_place = {(r["gpu"], r["region"]): r["offered_here"]
                for r in payload["by_region"]}

    assert by_place[("K80", "asia-east1")] is False, by_place
    assert by_place[("K80", "us-central1")] is False, by_place
    assert by_place[("L4", "us-central1")] is True, by_place


def test_the_caveat_is_not_printed_on_a_view_that_did_the_check(monkeypatch):
    """"STATUS reports quota held, not whether a region offers the card. ... Add
    --region to have that checked here." — on a view that has now checked every
    row. A note that was true when written and is false after a fix is the same
    trap as a rationale outliving its premise, and this one would have been
    printed under rows that contradict it."""
    per_region = {"quotaId": K80, "dimensionsInfos": [
        {"details": {"value": "1"}, "applicableLocations": ["asia-east1"]}]}
    cloud = Cloud(quotas=[per_region, quota(L4, 1, locations=["us-central1"])],
                  accelerators=stocking("us-central1", "nvidia-l4"))

    # A FRAGMENT THAT SURVIVES WRAPPING. Footnotes are wrapped at 96 characters,
    # so "Add --region to have that checked here" is split across two lines and a
    # literal match on it is absent from BOTH views — which would have passed the
    # first assertion for the wrong reason. Fourth time this has bitten.
    caveat = "STATUS reports quota held"

    by_region = quota_list(cloud, monkeypatch, "--by-region").output
    assert caveat not in by_region, by_region

    collapsed = quota_list(cloud, monkeypatch).output
    assert caveat in collapsed, (
        "the collapsed view still does not check, and must still say so")


def test_a_bucket_row_is_not_given_an_availability_verdict(monkeypatch):
    """The honest half of checking per row, which I claimed in a docstring and
    did not test until a mutation sweep asked. `any of 42` names no single
    region, so there is nothing to test a catalogue against — and answering
    anyway would mean picking one of the 42 and reporting it as all of them.

    Not offered, not absent: NOT CHECKED, in both surfaces.

    THE CARD HERE IS L4, NOT K80. A bucket for a card stocked NOWHERE it is
    metered IS answerable — whichever subset it covers, none of them stock it —
    and that is now reported, so K80 would test the opposite branch. L4 is
    stocked in one of its metered regions, which says nothing about the other
    forty-one, and is the case that genuinely cannot be answered.
    """
    cloud = Cloud(quotas=[quota(L4, 1, locations=REGIONS_43)],
                  accelerators=stocking("us-central1", "nvidia-l4"))

    line = next(l for l in quota_list(cloud, monkeypatch, "--by-region")
                .output.splitlines() if l.startswith("L4 ") and "any of" in l)
    assert "not offered here" not in line, line

    # Without the table flag, for the reason above.
    payload = json.loads(quota_list(cloud, monkeypatch, "--json")
                         .output.split("\n", 1)[1])
    bucket = next(r for r in payload["by_region"]
                  if r["gpu"] == "L4" and "any of" in r["region"])
    assert bucket["offered_here"] is None, bucket


def test_a_row_that_names_no_place_is_not_judged_against_a_region(monkeypatch):
    """A quota record with no `applicableLocations` still describes one place —
    the quota itself — and `rows()` labels that `global`. It is not a region, so
    there is nothing to look up in a catalogue of regions, and testing it anyway
    reports the card as absent everywhere on the strength of a lookup that could
    not be made. Same rule as the bucket row, and as `offered_here` on the
    ceiling: a check that cannot be made is not a check that failed.
    """
    placeless = {"quotaId": K80,
                 "dimensionsInfos": [{"details": {"value": "1"}}]}
    cloud = Cloud(quotas=[placeless, quota(L4, 1, locations=["us-central1"])],
                  accelerators=stocking("us-central1", "nvidia-l4"))

    line = next(l for l in quota_list(cloud, monkeypatch, "--by-region")
                .output.splitlines() if l.startswith("K80 "))
    assert "global" in line, line
    assert "not offered here" not in line, line


def test_a_bucket_is_answered_when_the_card_is_stocked_nowhere_it_is_metered(
        monkeypatch):
    """LIVE, and it reads as a contradiction:

        K80  any of 19    1  ready
        K80  asia-east1   1  not offered here — 1 of quota held
        ... and twenty-three more saying the same

    The bucket is "not checked" because it names no single region, which is
    honest and, on this card, needlessly weak. The exact subset it covers is
    unknown, but the card is stocked in NONE of the regions this project meters
    it in — so whichever subset it is, none of them stock it. That is answerable
    without knowing which.

    L4 is the control: stocked in one of its metered regions, so the bucket
    covering the other forty-one genuinely cannot be answered and stays None.
    """
    cloud = Cloud(quotas=[quota(K80, 1, locations=REGIONS_43),
                          quota(L4, 1, locations=REGIONS_43)],
                  accelerators=stocking("us-central1", "nvidia-l4"))
    lines = quota_list(cloud, monkeypatch, "--by-region").output.splitlines()

    k80 = next(l for l in lines if l.startswith("K80 ") and "any of" in l)
    assert "not offered here" in k80, k80

    l4 = next(l for l in lines if l.startswith("L4 ") and "any of" in l)
    assert "not offered here" not in l4, l4


def test_a_spanning_pool_is_relabelled_to_the_region_you_asked_about(monkeypatch):
    """The collapsed view narrows a spanning geography to the region asked for,
    so `--region us-central1` does not answer with "43 regions".

    PINNED THROUGH THE PREDICATE, not the spelling. The line doing this tested
    `"regions" in where` — a hand-rolled substring check sitting next to
    `spans_many`, which is the same shape that had just broken one branch over
    when `42 regions` was relabelled to `any of 42` and a guard went on testing
    the old string. It works today only because this branch's label still happens
    to contain the word.
    """
    from comfy_qa.quota import spans_many

    cloud = Cloud(quotas=THIS_PROJECT + [quota(SPOT_RTX, 1, locations=REGIONS_43)])
    lines = quota_list(cloud, monkeypatch, "--region", "us-central1").output.splitlines()
    header = next(l for l in lines if l.startswith("GPU "))
    start, stop = header.index("WHERE"), header.index("STATUS")
    body = lines[lines.index(header) + 1:]
    body = body[:body.index("")] if "" in body else body
    places = {l[start:stop].strip() for l in body}

    assert places, "no rows, so nothing was checked"
    for place in places - {"global"}:
        assert not spans_many(place), (
            f"{place!r} was not narrowed to the region asked about")


# --- pass 5.2: a region view must not hide a refusal made elsewhere -----------


def test_a_region_view_says_a_card_was_refused_in_another_region(monkeypatch):
    """THE ONE THAT ADVISES AN IRREVOCABLE REQUEST. Live:

        $ comfy-qat quota list --region us-central1
        A100      0  us-central1  denied — Google refused this; asking again will not help
        A100-80GB 0  us-central1  none — request it        <- refused in europe-west4

    and symmetrically the other way round. `setup` declines to re-ask for both of
    these and says so; `--by-region` says "refused elsewhere"; this surface is
    the only one of the three that says "request it", and it is the one whose
    instruction files something that cannot be withdrawn.

    One line: `refused_somewhere` was built from rows ALREADY FILTERED by
    `--region`, so it could only ever see refusals in the region being asked
    about — a set whose whole purpose is "somewhere else", computed from a view
    that excludes everywhere else.

    The region is NAMED, because "elsewhere" is the fact a reader then has to go
    and look up.
    """
    cloud = Cloud(quotas=THIS_PROJECT, preferences=PREFS_DENIED)
    line = next(l for l in quota_list(cloud, monkeypatch, "--region", "us-central1")
                .output.splitlines() if l.startswith("A100-80GB"))

    assert "request it" not in line, line
    assert "europe-west4" in line, line


def test_the_three_surfaces_agree_about_a_card_refused_elsewhere(monkeypatch):
    """The point is not the wording, it is that they stop disagreeing. `setup`,
    `--by-region` and `--region` are asked about the same card in the same
    minute and none of them may say "request it"."""
    cloud = Cloud(quotas=THIS_PROJECT, preferences=PREFS_DENIED)

    narrowed = next(l for l in quota_list(cloud, monkeypatch, "--region",
                                          "us-central1").output.splitlines()
                    if l.startswith("A100-80GB"))
    by_region = [l for l in quota_list(cloud, monkeypatch, "--by-region")
                 .output.splitlines() if l.startswith("A100-80GB")]
    planned = {a.card: a for a in plan_quota(THIS_PROJECT, PREFS_DENIED,
                                             region="us-central1")}

    assert "request it" not in narrowed, narrowed
    assert all("request it" not in l for l in by_region), by_region
    assert planned["a100-80gb"].outcome == DENIED, planned["a100-80gb"]


# --- pass 5.1: a span plus its named siblings is the sum, not the larger ------


def test_where_label_adds_the_named_regions_to_the_span():
    """`quota list` said `T4 1 19 regions ready` while `create --gpu t4` said
    `T4: 1, in 43 regions` about the same project in the same minute. The API is
    43: twenty-four individually named rows plus a nineteen-location catch-all.

    `where_label` took `max()` over the spanning labels and DISCARDED every named
    region — so the answer was not merely low, it excluded us-central1, the
    tool's own default region.

    THE FIXTURES COULD NOT SEE IT. Every existing unit test of this function
    feeds it a HOMOGENEOUS set: all named, or one span. The shape that occurs
    live — named rows AND a catch-all together — was never constructed, in the
    function whose entire job is describing geography.
    """
    from comfy_qa.quota import where_label

    mixed = {"us-central1", "europe-west4", "19 regions"}
    assert where_label(mixed) == "21 regions", where_label(mixed)

    # and the homogeneous cases still answer as they did
    assert where_label({"us-central1"}) == "us-central1"
    assert where_label({"us-central1", "europe-west4"}) == "2 regions"
    assert where_label({"19 regions"}) == "19 regions"
    assert where_label({"global"}) == "global"


def test_quota_list_and_create_count_the_same_regions(monkeypatch):
    """The two surfaces, on one fixture built the way the live project reports:
    named rows for the regions that have their own dimension entry, plus one
    catch-all covering the rest."""
    named = ["us-central1", "europe-west4"]
    spread = [{"quotaId": T4, "dimensionsInfos": [
        *[{"dimensions": {"region": r}, "details": {"value": "1"},
           "applicableLocations": [r]} for r in named],
        {"details": {"value": "1"},
         "applicableLocations": ["asia-east1", "africa-south1"]},
    ]}]

    lines = quota_list(Cloud(quotas=spread), monkeypatch).output.splitlines()
    header = next(l for l in lines if l.startswith("GPU "))
    start, stop = header.index("WHERE"), header.index("STATUS")
    where = next(l[start:stop].strip() for l in lines if l.startswith("T4 "))

    from comfy_qa.create import CARDS, card_grant

    _limit, regions = card_grant(CARDS["t4"], spread)
    assert where == f"{len(regions)} regions", (where, regions)


# --- pass 5.4: a retraction that only reached the comment ---------------------


RETRACTED = [
    "immediately and without review",
    "cannot be taken back by waiting",
    "quick, permanent and unremarked",
]


def test_a_claim_the_code_retracts_does_not_ship_anywhere_else():
    """SIXTEENTH SECOND-SITE, and the worst-placed one: the retraction is in a
    docstring and the claim was still in `--help`.

    `request_value` says outright that "a decrease is fulfilled immediately and
    without review, so it is the one quota change that is quick, permanent and
    unremarked" was relayed rather than read and is wrong twice over — nothing in
    the API schema says "immediately" or "without review", and `resetValue` is
    documented as the value a quota is reset to "if a quota decrease preference
    is deleted", which contradicts "permanent" outright.

    It shipped anyway in `quota request --help`, in a remedy line printed at the
    moment of refusal, and in two docs pages. Help text is more user-facing than
    the comment retracting it.

    THE GUARD IS THE POINT. Retracting a claim in one file while it ships from
    four others is not a wording slip, it is the shape this feature has produced
    sixteen times; the only fix that holds is one that fails when the sentence
    comes back.
    """
    from pathlib import Path

    # WHITESPACE COLLAPSED FIRST. The help text wraps the sentence across three
    # source lines, so a line-by-line search found the two docs and the remedy
    # and missed the one that is printed by `--help` — a guard that could not see
    # the worst instance, which is the defect it was written to stop.
    root = Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted([*root.glob("comfy_qa/*.py"), *root.glob("docs/*.md"),
                        root / "README.md"]):
        # QUOTES STRIPPED TOO. The help text is built from adjacent string
        # literals — `"...immediately and without " "review, so..."` — so
        # collapsing whitespace alone leaves `without " "review` and the search
        # misses it. A sentence split by the compiler is still a sentence the
        # user reads in one piece.
        flat = " ".join(path.read_text().replace('"', " ").replace("'", " ").split())
        for claim in RETRACTED:
            start = 0
            while (at := flat.find(claim, start)) != -1:
                start = at + 1
                # The retraction itself quotes the sentence in order to withdraw
                # it, which is the one place it belongs.
                if "asserted that Google" in flat[max(0, at - 300):at]:
                    continue
                offenders.append(f"{path.name}: {claim!r}")
    assert not offenders, (
        "a retracted claim is still shipping:\n  " + "\n  ".join(offenders))


def test_an_unlimited_grant_gets_a_remedy_about_a_grant(monkeypatch):
    """4b, and the SEVENTH instance of the shape the block's own comment names.

    That comment says `send is None` has THREE causes. It has four:
    `wanted < 0`, `wanted == 0 and not release`, `held == UNLIMITED`, and an
    unreadable standing value. The fourth fell into the `else`, whose comment
    reads "The standing value could not be read" — and live:

        NVIDIA-T4-GPUS-per-project-zone: this project holds UNLIMITED ...; 1 would lower it
        to fix: --allow-lower   # send it anyway, knowing it may replace a larger standing request

    The standing value WAS read. Nothing is replacing a request — there is no
    preference for that id at all. What is at stake is an unlimited grant, and
    the remedy describes the stake as a request.

    A comment enumerating the causes is not a guard against a new one arriving;
    counting them in a test is.
    """
    unlimited = [{"quotaId": "NVIDIA-T4-GPUS-per-project-zone",
                  "dimensionsInfos": [{"details": {"value": "-1"},
                                       "applicableLocations": ["us-central1-a"]}]}]
    result = quota_request(Cloud(quotas=unlimited), monkeypatch,
                           "--quota-id", "NVIDIA-T4-GPUS-per-project-zone",
                           "--value", "1", "--dry-run")

    assert result.exit_code == 2
    assert "UNLIMITED" in result.output, result.output
    assert "standing request" not in result.output, result.output
    assert "--allow-lower" in result.output, (
        "allow-lower IS the flag that performs this one, unlike the negative")


# --- pass 5.5: nothing irrevocable is filed before everything is checked ------


def test_a_refusal_on_the_third_card_files_nothing_for_the_first_two(monkeypatch):
    """`quota request --gpu l4,t4,h100` filed L4, filed T4, then refused H100 at
    the floor and exited 2 — with `submitted` discarded, so the `track them:`
    line naming what HAD been filed never printed either. Two irrevocable
    requests on the project and no record of them in the output that reported a
    failure.

    Every input needed to refuse H100 — the quota records and the preference
    list — is fetched BEFORE the loop. Nothing has to be sent to discover it.
    So the whole set is checked first, and the command either files all of them
    or none.

    Ranked as "noted, not a defect" by the fifth pass. It is on the one path in
    this tool that cannot be undone, and the check is free, which is enough.
    """
    cloud = Cloud(quotas=THIS_PROJECT, preferences=STANDING_H100_AT_8)
    result = quota_request(cloud, monkeypatch, "--gpu", "l4,t4,h100",
                           "--value", "1")

    assert result.exit_code == 2
    assert cloud.submitted == [], (
        f"filed {len(cloud.submitted)} request(s) before refusing the third")
    assert "h100" in result.output.lower(), result.output


def test_a_bucket_is_checked_against_the_regions_it_actually_covers(monkeypatch):
    """The fifth pass's third finding, still live after the `--by-region` work.

        P100  any of 19  1  ready — this tool cannot drive it
        P4    any of 19  1  ready — this tool cannot drive it
        V100  any of 19  1  ready — this tool cannot drive it

    These are not the K80 case: each IS stocked somewhere it is metered, so
    "stocked nowhere" does not fire and the bucket stayed unchecked. But the
    membership is not unknowable — a catch-all row covers exactly the regions
    that have NO row of their own, so it is the metered regions minus the named
    siblings, both of which are already in hand.

    Third time the missing location list has cost accuracy: `never_asked_in`
    counted zones, `where_label` undercounted a span, and now this.
    """
    named, bucketed = ["us-central1", "europe-west4"], ["asia-east1", "africa-south1"]
    spread = [{"quotaId": T4, "dimensionsInfos": [
        *[{"dimensions": {"region": r}, "details": {"value": "1"},
           "applicableLocations": [r]} for r in named],
        {"details": {"value": "1"}, "applicableLocations": bucketed},
    ]}]
    cloud = Cloud(quotas=spread, accelerators=stocking("us-central1", "nvidia-tesla-t4"))

    lines = quota_list(cloud, monkeypatch, "--by-region").output.splitlines()
    bucket = next(l for l in lines if l.startswith("T4 ") and "any of" in l)
    assert "not offered here" in bucket, bucket

    here = next(l for l in lines if l.startswith("T4 ") and " us-central1 " in l)
    assert "not offered here" not in here, here


def test_the_dry_run_line_names_the_card_not_just_the_raw_id(monkeypatch, tmp_path):
    """`setup --dry-run` ended with

        would ask for GPUS-PER-GPU-FAMILY-per-project-region = 8

    — a raw id that meters five cards, with no card and no region named. The
    tool's own refusal for that id says "meters several cards and names which one
    in a dimension, so a raw id cannot say which you mean". Cosmetic, and it
    contradicts a rule this tool enforces against its users one command over.
    """
    # `europe-west4`, not `europe-west1`: `REGIONS_43` is a FOUR-element stand-in
    # for the live forty-three, so europe-west1 is not a region this fixture's
    # project meters and `setup` now stops on it as a typo — correctly, and the
    # test would have been asserting on a refusal instead of a plan.
    cloud = Cloud(quotas=THIS_PROJECT)
    p = run(cloud, config_path=tmp_path / "hosts.toml", quota_dry_run=True,
            region="europe-west4")
    lines = [l for l in p.said if l.startswith("would ask for")]

    assert lines, "no dry-run request lines"
    for line in lines:
        assert "=" in line, line
        if FAMILY in line:
            assert "H100-80GB" in line or "h100" in line.lower(), line
            assert "europe-west4" in line, line


# --- pass 5 re-run: metered is not the same question as granted ---------------


def test_a_card_at_zero_is_still_metered_and_still_gets_a_region_to_ask_in(
        monkeypatch):
    """NINTH INSTANCE OF THE REMEDY SHAPE, INVERTED — not a remedy that fails,
    but the one that works never being printed. Live:

        $ quota request --gpu h100 --region africa-south1
        h100: africa-south1 does not offer this card — Google sells it in 20
              regions, none of them metered by this project
        to fix: comfy-qat quota list --region africa-south1

    The project meters H100 in all 43 regions, so all 20 ARE metered. And
    `quota request --gpu h100 --region asia-east1` exits 0 and builds a valid
    preference — the line that was withheld.

    `regions_with_quota` answers "where is there a NON-ZERO grant", which its
    docstring says and its name does not. Read as "where is this project
    metered", the two differ exactly when the limit is zero — which is every card
    anybody would run `quota request` for.

    T4 holds quota and got the useful remedy; H100 did not. The tool was most
    unhelpful precisely where it was most needed.
    """
    zero_everywhere = [quota(FAMILY, 0, locations=REGIONS_43)]
    family_rows = [family(0, "NVIDIA_H100", locations=REGIONS_43)]
    cloud = Cloud(quotas=family_rows,
                  accelerators=stocking("asia-east1", "nvidia-h100-80gb"))
    result = quota_request(cloud, monkeypatch, "--gpu", "h100",
                           "--region", "africa-south1", "--dry-run")

    assert result.exit_code == 2
    assert "none of them metered" not in result.output, result.output
    assert "--region asia-east1" in result.output, (
        "the remedy that works was withheld from the card that needs it")


def test_metered_and_granted_are_different_questions():
    """The predicate, directly. `regions_with_quota` keeps its meaning — where
    could a box start today — and metering gets its own name, because reading one
    as the other is what produced the finding above."""
    from comfy_qa.quota import regions_metered, regions_with_quota

    at_zero = [quota(T4, 0, locations=["us-central1", "asia-east1"])]
    assert regions_with_quota("t4", at_zero) == []
    assert sorted(regions_metered("t4", at_zero)) == ["asia-east1", "us-central1"]

    granted = [quota(T4, 1, locations=["us-central1"])]
    assert regions_with_quota("t4", granted) == ["us-central1"]
    assert sorted(regions_metered("t4", granted)) == ["us-central1"]


def test_a_card_at_zero_gets_the_metered_elsewhere_refusal(monkeypatch):
    """The second consumer of the same conflation, found by a mutation sweep.

    `places` decides between "this project has no <card> quota in <region>",
    which names where it IS metered, and the blunter "this project reports no
    quota for <card>". Built from GRANTED regions, a card at zero had no places
    at all, so it always fell to the blunt one — again, for exactly the cards a
    person is asking about.
    """
    # A PER-CARD QUOTA, because a family target resolves in any region — Google
    # keys it on (family, region) and the row need not list the region — so the
    # unresolvable branch is never reached that way. `T4` is here only to put
    # europe-west4 in the region universe, ahead of the typo check.
    metered_in_one = [quota(L4, 0, locations=["us-central1"]),
                      quota(T4, 1, locations=["europe-west4"])]
    result = quota_request(Cloud(quotas=metered_in_one), monkeypatch,
                           "--gpu", "l4", "--region", "europe-west4",
                           "--dry-run")

    assert result.exit_code == 2
    assert "no l4 quota in europe-west4" in result.output, result.output
    assert "--region us-central1" in result.output, (
        "the refusal knows where the card IS metered and did not say")


def test_a_bucket_for_a_card_at_zero_is_still_checked(monkeypatch):
    """The third consumer. A bucket's membership is metered-minus-named, and with
    GRANTED semantics a card at zero was metered nowhere — so `covered` came out
    empty and the row read as unanswerable rather than being checked."""
    named, bucketed = ["us-central1"], ["asia-east1", "europe-west4"]
    spread = [{"quotaId": FAMILY, "dimensionsInfos": [
        *[{"dimensions": {"gpu_family": "NVIDIA_H100", "region": r},
           "details": {}, "applicableLocations": [r]} for r in named],
        {"dimensions": {"gpu_family": "NVIDIA_H100"}, "details": {},
         "applicableLocations": bucketed},
    ]}]
    cloud = Cloud(quotas=spread,
                  accelerators=stocking("us-central1", "nvidia-h100-80gb"))

    bucket = next(l for l in quota_list(cloud, monkeypatch, "--by-region")
                  .output.splitlines()
                  if l.startswith("H100-80GB") and "any of" in l)
    assert "not offered here" in bucket, bucket


# --- pass 6: setup is the one surface with neither check ----------------------


def test_setup_refuses_a_region_that_does_not_exist(tmp_path):
    """F1, and one typo produces TWO falsehoods. Live:

        $ comfy-qat setup --dry-run --region us-centrall --non-interactive
        exit=0
        H100-80GB  will ask Google for 8 in us-centrall
        L4         this project does not meter L4 quota, so there is nothing to ask for
        T4         this project does not meter T4 quota, so there is nothing to ask for

    It plans an irrevocable request for eight H100s into a region that does not
    exist, AND reports four cards as unmetered when L4 and T4 are granted at 1
    across forty-three regions.

    SEVENTEENTH SECOND-SITE. `quota list --region` and `quota request --region`
    both refuse this exact typo with exit 2. `setup` is the command a new user
    runs first, unattended, and it is the one that files automatically.
    """
    cloud = Cloud(quotas=THIS_PROJECT)
    with pytest.raises(SetupStopped) as stopped:
        run(cloud, config_path=tmp_path / "hosts.toml", region="us-centrall",
            quota_dry_run=True)

    assert "us-centrall" in str(stopped.value)
    assert cloud.submitted == [], "planned into a region that does not exist"


def test_setup_does_not_call_a_granted_card_unmetered(tmp_path):
    """The second falsehood, which survives a VALID region too: the code means
    "no row matched the region I filtered by" and says "this project does not
    meter L4". Same conflation as `regions_with_quota`, wearing different
    clothes — and here it reports a card granted in forty-three regions as one
    the project does not meter at all.

    Said honestly it degrades instead of lying: no quota IN THAT REGION.
    """
    only_here = [quota(L4, 1, locations=["us-central1"]),
                 quota(T4, 1, locations=["europe-west4"])]
    plan = {a.card: a for a in plan_quota(only_here, PREFS_NONE,
                                          region="europe-west4")}

    detail = plan["l4"].detail
    assert "does not meter L4" not in detail, detail
    assert "europe-west4" in detail, detail


def test_setup_refuses_a_region_that_sells_no_gpus(tmp_path):
    """F2. `setup --region africa-south1` planned an H100 request into a region
    that sells no NVIDIA accelerator of any kind. `quota request` refuses exactly
    that, and has since the fourth pass."""
    cloud = Cloud(quotas=THIS_PROJECT,
                  accelerators=stocking("us-central1", "nvidia-h100-80gb"))
    with pytest.raises(SetupStopped) as stopped:
        run(cloud, config_path=tmp_path / "hosts.toml", region="africa-south1",
            quota_dry_run=True)

    assert "africa-south1" in str(stopped.value)
    assert cloud.submitted == []


def test_a_region_scoped_raw_id_without_a_region_is_refused(monkeypatch):
    """F3. `auth.py` read `if region and needs_region(quota_id)` — so a MISSING
    region silently dropped the required dimension instead of refusing. The `and`
    made the guard unreachable in precisely the case it exists for.

    The result is the shape `needs_region`'s own docstring records Google
    rejecting: a dimensionless preference, filed permanently, for an id whose
    `-per-project-region` suffix defines a region dimension the API requires to
    be set.

    AND THE TEST STORY IS THE SHARPEST YET. There is a guard named
    `test_every_region_scoped_request_names_a_region` — and it loops only over a
    `setup` run's submissions, so the one surface that breaks the rule cannot
    reach the test that names it. A rule tested on one surface is a rule
    unenforced on the others.
    """
    cloud = Cloud(quotas=[quota(T4, 1, locations=["us-central1"])])
    result = quota_request(cloud, monkeypatch, "--quota-id", T4,
                           "--value", "1", "--dry-run")

    assert cloud.submitted == [], result.output
    assert result.exit_code == 2
    assert "--region" in result.output, result.output


def test_every_surface_that_can_file_a_request_names_the_region(monkeypatch,
                                                                tmp_path):
    """The population the guard should have covered. `setup` was already right;
    `quota request` was not, on both the `--gpu` and `--quota-id` paths, and the
    existing test could see only the first of the three.

    Asserted on the COMMANDS, from every surface that builds one, so a fourth
    caller cannot be added without appearing here.
    """
    from comfy_qa.quota import needs_region

    # WITH AND WITHOUT A REGION, per surface. The first version handed every
    # surface an explicit `--region` — so it enumerated the right population and
    # STILL could not reach the branch, because `if region and needs_region(...)`
    # only misbehaves when the region is ABSENT. Reintroducing the `and` left
    # this test green: the same defect it was written to close, one level in.
    def filed(cloud, *args):
        quota_request(cloud, monkeypatch, *args)
        return cloud.submitted

    def at_zero():
        return Cloud(quotas=[quota(T4, 0, locations=["us-central1"])])

    setup_cloud = Cloud(quotas=THIS_PROJECT)
    run(setup_cloud, config_path=tmp_path / "hosts.toml", region="us-central1")
    surfaces = {
        "setup": setup_cloud.submitted,
        "--gpu with a region": filed(at_zero(), "--gpu", "t4", "--region",
                                     "us-central1", "--value", "1"),
        "--gpu without one": filed(at_zero(), "--gpu", "t4", "--value", "1"),
        "--quota-id with a region": filed(at_zero(), "--quota-id", T4,
                                          "--region", "us-central1",
                                          "--value", "1"),
        "--quota-id without one": filed(at_zero(), "--quota-id", T4,
                                        "--value", "1"),
    }

    built = [args for one in surfaces.values() for args in one]
    assert len(built) >= 3, f"only {len(built)} commands built"
    for args in built:
        quota_id = flag(args, "quota-id")
        if needs_region(quota_id):
            dims = parse_dimensions(flag(args, "dimensions"))
            assert "region" in dims, (quota_id, args)

    # AND THE OMITTED-REGION SURFACES ARE NAMED, because the loop above is
    # satisfied by filing nothing — from outside, a refusal and a silent drop
    # look identical, and one of them is a permanent preference.
    assert surfaces["--quota-id without one"] == [], (
        "a region-scoped raw id was filed with no region")
    assert surfaces["--gpu without one"], (
        "`--gpu` resolves a region itself, so it must still file something")


def test_the_default_value_is_the_number_the_card_needs(monkeypatch):
    """F4. The tool's own remedy printed `--value 1` for an H100 — and
    `CARDS["h100"]` is `count=8, a3-highgpu-8g`, `setup` asks for 8 from that
    same table, and `request_value`'s docstring calls 1 "a number that cannot
    start an `a3-highgpu-8g` even if granted".

    So the floor protects a standing request at 8, and in any region without one
    the same tool files 1 — permanently, through a line it printed itself, with
    no warning. The asymmetry fits on one screen: `--value 9` DOES warn that 8 is
    the most any machine takes. The number was held all along and used in one
    direction only.
    """
    cloud = Cloud(quotas=[family(0, "NVIDIA_H100", locations=REGIONS_43)])
    quota_request(cloud, monkeypatch, "--gpu", "h100", "--region",
                  "us-central1")

    assert [flag(a, "preferred-value") for a in cloud.submitted] == ["8"], (
        cloud.submitted)


def test_a_one_gpu_card_still_defaults_to_one(monkeypatch):
    """The other half, from the same table rather than from a constant: L4 is
    `count=1`, so nothing changes for it."""
    cloud = Cloud(quotas=[quota(L4, 0, locations=["us-central1"])])
    quota_request(cloud, monkeypatch, "--gpu", "l4", "--region", "us-central1")

    assert [flag(a, "preferred-value") for a in cloud.submitted] == ["1"]


def test_a_raw_id_with_no_card_still_defaults_to_one(monkeypatch):
    """`--quota-id` names no card, so there is no count to read and the old
    default stands."""
    cloud = Cloud(quotas=[quota(T4, 0, locations=["us-central1"])])
    quota_request(cloud, monkeypatch, "--quota-id", T4, "--region",
                  "us-central1")

    assert [flag(a, "preferred-value") for a in cloud.submitted] == ["1"]


def test_the_availability_caveat_survives_for_rows_that_were_not_checked(
        monkeypatch):
    """F5, and it is the sharpest form of a true premise applied too widely.

    `--by-region` suppresses the "STATUS reports quota held, not whether a region
    offers the card" note on the stated grounds that it now answers availability
    per row. True of the named rows and FALSE of the buckets it classifies as
    mixed — which live are L4 (17 of 42 stocked), T4 (2 of 19) and RTX-PRO-6000
    (22 of 43). `T4  any of 19  ready` covers nineteen regions of which two sell
    T4, and reads identically to a row that was checked and passed.

    Net effect: the more detailed view was less honest than the collapsed one.

    The note survives for exactly the rows that were not checked, and names them,
    so "not checked" stops rendering as "checked and fine".
    """
    named, bucketed = ["us-central1"], ["asia-east1", "africa-south1"]
    mixed = [{"quotaId": T4, "dimensionsInfos": [
        *[{"dimensions": {"region": r}, "details": {"value": "1"},
           "applicableLocations": [r]} for r in named],
        {"details": {"value": "1"}, "applicableLocations": bucketed},
    ]}]
    # asia-east1 stocks T4 and africa-south1 does not, so the bucket is mixed.
    catalogue = stocking("us-central1", "nvidia-tesla-t4") + [
        {"name": "nvidia-tesla-t4", "zone": "https://x/zones/asia-east1-a"}]
    out = quota_list(Cloud(quotas=mixed, accelerators=catalogue), monkeypatch,
                     "--by-region").output

    assert "STATUS reports quota held" in out, out
    assert "T4" in out.split("STATUS reports quota held")[1][:400], (
        "the note has to say WHICH rows it is about")


def test_the_caveat_stays_away_when_every_row_was_checked(monkeypatch):
    """The other half, so the note does not simply come back for everyone. With
    no bucket at all, every row names a region and every one was checked."""
    all_named = [{"quotaId": T4, "dimensionsInfos": [
        {"dimensions": {"region": r}, "details": {"value": "1"},
         "applicableLocations": [r]} for r in ("us-central1", "asia-east1")]}]
    out = quota_list(Cloud(quotas=all_named,
                           accelerators=stocking("us-central1", "nvidia-tesla-t4")),
                     monkeypatch, "--by-region").output

    assert "STATUS reports quota held" not in out, out


# --- pass 6 minors: two user-facing strings ----------------------------------


@pytest.mark.parametrize("card", ["a100", "a100-80gb", "h100", "l4", "t4"])
def test_the_refusal_uses_the_right_article(card):
    """"a A100 box", "a H100-80GB box" — wrong on three of the five drivable
    cards, in the sentence a person meets when a create is refused."""
    from comfy_qa.create import CARDS, check_quota

    problem = str(check_quota(CARDS[card], [], [], "us-central1").problem())
    for vowel in "AEIOU8":
        assert f"a {vowel}" not in problem, problem


def test_no_user_facing_string_says_a_word_twice():
    """`setup.py` carried "one that works. Ask ask for one that works:" —
    reachable only on a project holding quota for undrivable cards ONLY, which is
    why nothing here has ever printed it. A doubled word is trivial; a
    user-facing string no test can reach is the part worth guarding."""
    import ast
    import re as _re
    from pathlib import Path

    # STRING LITERALS, VIA THE PARSER. Two earlier attempts framed this wrongly:
    # line-by-line missed the case entirely, because `"... one. Ask "` and
    # `"ask for one that works"` are adjacent literals on separate lines; and
    # flattening the whole file found `main main` and `results results` in code,
    # which is noise. `ast` merges implicit concatenation for us and hands back
    # exactly the strings a user can see.
    #
    # `venv venv` is `python -m venv venv`, a real command and not prose.
    allowed = {"venv venv", "had had"}
    root = Path(__file__).resolve().parent.parent
    doubled = []
    for path in sorted(root.glob("comfy_qa/*.py")):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            # CASE-INSENSITIVE, because the defect was "Ask ask" — a sentence
            # boundary followed by the same word — and a case-sensitive
            # backreference cannot match it. The first version of this guard
            # passed against the reintroduced original, which is the one thing a
            # guard must never do.
            for match in _re.finditer(r"\b(\w+) \1\b", node.value, _re.IGNORECASE):
                if match.group(0).lower() in allowed:
                    continue
                doubled.append(f"{path.name}:{node.lineno}  {match.group(0)!r}")
    assert not doubled, "a word is repeated in a string:\n  " + "\n  ".join(doubled)


# --- pass 7: one region check, called by every surface ------------------------


def test_a_zone_passed_as_a_region_is_refused_not_answered(monkeypatch):
    """EIGHTEENTH SECOND-SITE, and the worst kind: a confident empty answer.

        $ comfy-qat quota list --region us-central1-a
        exit=0
        GPU            LIMIT  WHERE   STATUS
        any (global)       1  global  ready

    That says this project holds no card quota anywhere. It holds six cards at 1.
    And `us-central1-a` is the single most likely thing a person types when they
    mean a region, because every `create` and `gcloud` example uses zones.

    Exit 0 is what makes it worse than a refusal: the typo path exits non-zero
    and says what is wrong; this answers confidently with an empty picture, which
    is the shape a script trusts and a person believes.

    `quota list` was the one surface that built its OWN region check instead of
    calling the shared universe — and that local check folds zone names in, so
    zones from `-per-project-zone` rows passed it.
    """
    zoned = [quota(L4, 1, locations=["us-central1"]),
             {"quotaId": "NVIDIA-L4-GPUS-per-project-zone",
              "dimensionsInfos": [{"details": {"value": "1"},
                                   "applicableLocations": ["us-central1-a"]}]}]
    result = quota_list(Cloud(quotas=zoned), monkeypatch, "--region",
                        "us-central1-a")

    assert result.exit_code == 2, result.output
    assert "zone" in result.output.lower(), result.output
    assert "--region us-central1" in result.output, (
        "the region it means is right there and was not offered")


@pytest.mark.parametrize("surface", ["list", "request"])
def test_a_region_wrong_only_in_case_gets_the_nearest_match(surface, monkeypatch):
    """F8. `"US-CENTRAL1".split("-")[0]` is `"US"`; the universe holds `"us"`. So
    a region wrong by zero characters but for case fell to the generic fix line,
    while a zone name — wrong by a whole segment — got the exact one."""
    cloud = Cloud(quotas=[quota(L4, 1, locations=["us-central1"])])

    def refuse(region):
        args = (("--region", region) if surface == "list"
                else ("--gpu", "l4", "--region", region))
        out = (quota_list(cloud, monkeypatch, *args) if surface == "list"
               else quota_request(cloud, monkeypatch, *args))
        assert out.exit_code == 2, out.output
        return out.output

    # Wrong in case ONLY, and the message says so rather than guessing. Both
    # branches name us-central1, so asserting only the name cannot tell them
    # apart — a sweep said so by surviving.
    only_case = refuse("US-CENTRAL1")
    assert "us-central1" in only_case
    assert "lower case" in only_case, only_case

    # AND WRONG IN CASE AND SPELLING, which is the input that actually exercises
    # the nearest-match search: an exact fold cannot rescue it, so if the prefix
    # comparison is case-sensitive there is no suggestion at all.
    assert "us-central1" in refuse("US-CENTRAL99")


def test_every_surface_asks_the_same_question_about_a_region(monkeypatch,
                                                             tmp_path):
    """The population, since this is the second time a region check has been
    written per surface. All three refuse the same inputs."""
    zoned = [quota(L4, 1, locations=["us-central1"]),
             {"quotaId": "NVIDIA-L4-GPUS-per-project-zone",
              "dimensionsInfos": [{"details": {"value": "1"},
                                   "applicableLocations": ["us-central1-a"]}]}]

    assert quota_list(Cloud(quotas=zoned), monkeypatch,
                      "--region", "us-central1-a").exit_code == 2
    assert quota_request(Cloud(quotas=zoned), monkeypatch, "--gpu", "l4",
                         "--region", "us-central1-a").exit_code == 2
    with pytest.raises(SetupStopped):
        run(Cloud(quotas=zoned), config_path=tmp_path / "hosts.toml",
            region="us-central1-a", quota_dry_run=True)


def test_a_setup_remedy_carries_the_region_it_was_planning_for(tmp_path):
    """TENTH INSTANCE OF THE REMEDY CLASS, AND THE FIRST THAT IS HARMFUL. The
    others failed, or were withheld; this one succeeds at doing the wrong thing,
    permanently.

        $ comfy-qat setup --region europe-west1 --no-quota-request
        To ask later: comfy-qat quota request --gpu h100

    With no region, `quota request` derives us-central1 — where Google has
    already refused H100 — while setup's own plan, printed in the same run, named
    europe-west1. Run the remedy and you file the refused request again.
    """
    cloud = Cloud(quotas=THIS_PROJECT)
    p = run(cloud, config_path=tmp_path / "hosts.toml", region="europe-west4",
            quota_requests=False)
    later = [l for l in p.said if "quota request --gpu" in l]

    assert later, "no remedy printed"
    for line in later:
        assert "--region europe-west4" in line, line


def test_a_setup_remedy_carries_the_region_when_nothing_is_sent(tmp_path):
    """The sibling line, on the declined-confirmation path — same sentence, same
    omission, printed from a different branch."""
    cloud = Cloud(quotas=THIS_PROJECT)
    p = run(cloud, config_path=tmp_path / "hosts.toml", region="europe-west4",
            prompts=prompts(confirm=False))
    later = [l for l in p.said if "quota request --gpu" in l]

    assert later, "no remedy printed"
    for line in later:
        assert "--region europe-west4" in line, line


def test_pools_are_read_through_the_cards_quota_aliases():
    """F4, and it is STRUCTURALLY THE DEFECT THAT STARTED THE POOL REWRITE.

    RTX PRO 6000 was reported denied while a granted Spot allowance sat unread.
    `_pool_ids` interpolates the card's DISPLAY name — `NVIDIA-{gpu}-GPUS-...` —
    so for H100-80GB it builds `PREEMPTIBLE-NVIDIA-H100-80GB-GPUS-...`, which is
    not what Google meters. The card's `quota_aliases` holds `H100`, the spelling
    that resolves, and nothing consulted it.

    Latent only because H100 Spot is 0 today, and it would surface the day Google
    grants any — which is exactly the shape of the original: a pool held and
    never read.
    """
    from comfy_qa.quota import _pool_ids

    ids = {quota_id for _pool, quota_id, _counts, _cost in _pool_ids("H100-80GB")}
    assert "PREEMPTIBLE-NVIDIA-H100-GPUS-per-project-region" in ids, sorted(ids)

    # And a card with no alias is unchanged.
    plain = {q for _p, q, _c, _x in _pool_ids("L4")}
    assert "PREEMPTIDE-NVIDIA-L4-GPUS-per-project-region" not in plain
    assert "PREEMPTIBLE-NVIDIA-L4-GPUS-per-project-region" in plain, sorted(plain)


def test_a_spot_grant_under_the_alias_is_actually_found(monkeypatch):
    """Through the surface, because `_pool_ids` returning the right string proves
    nothing about whether anything reads it. A Spot grant metered under the
    alias has to make the card ready, the way RTX-PRO-6000's did."""
    spot_h100 = [family(0, "NVIDIA_H100", locations=["us-central1"]),
                 quota("PREEMPTIBLE-NVIDIA-H100-GPUS-per-project-region", 1,
                       locations=["us-central1"])]
    line = next(l for l in quota_list(Cloud(quotas=spot_h100), monkeypatch)
                .output.splitlines() if l.startswith("H100-80GB"))

    assert "Spot" in line, line


# --- pass 7, the rest --------------------------------------------------------


def test_the_value_help_says_what_the_default_actually_is():
    """F6. `--help` still said "Left off, one" after the default became the
    card's own count — `--gpu h100` sends 8. The prose block immediately above
    `_default_for` was rewritten for the new behaviour and the `help=` eleven
    lines below was left at the old answer.

    `--help` is what somebody reads BEFORE typing an irrevocable command, and
    `test_option_help.py` asserts every option HAS help, never that it is true,
    so this class had no guard at all."""
    from typer.testing import CliRunner

    from comfy_qa.cli import app

    text = " ".join(CliRunner().invoke(app, ["quota", "request", "--help"])
                    .output.split())

    assert "Left off, one" not in text, text
    assert "8" in text or "the card" in text, (
        "the help has to say what the default is now")


def test_the_ceiling_is_not_listed_among_cards_that_were_not_checked(monkeypatch):
    """F7. The note read "... This applies to A100, ..., T4, any (global), whose
    rows cover several regions at once." Two things false about the last member:
    `any (global)` is not a card — the first note in the same output says so —
    and its row covers exactly one place, `global`.

    Absent-versus-zero again: `stocks.get("any (global)")` is None because it is
    not a card, and the `places is None` branch reads that as "lookup failed"
    rather than "not applicable". The precedent is fourteen lines up in the same
    file: `drivable_flag` carries `if name == GLOBAL_ALLOWANCE: return None`.
    """
    mixed = [{"quotaId": T4, "dimensionsInfos": [
        {"dimensions": {"region": "us-central1"}, "details": {"value": "1"},
         "applicableLocations": ["us-central1"]},
        {"details": {"value": "1"},
         "applicableLocations": ["asia-east1", "africa-south1"]},
    ]}, quota(CEILING, 1, locations=["global"])]
    catalogue = stocking("us-central1", "nvidia-tesla-t4") + [
        {"name": "nvidia-tesla-t4", "zone": "https://x/zones/asia-east1-a"}]
    out = quota_list(Cloud(quotas=mixed, accelerators=catalogue), monkeypatch,
                     "--by-region").output

    note = out.split("STATUS reports quota held")[1]
    assert "any (global)" not in note[:400], note[:400]


def test_both_json_arrays_carry_the_same_keys(monkeypatch):
    """F9. `by_region[]` objects have `quota_id`; `gpus[]` objects do not, because
    the guard is `if quota_id is not None and "quota_id" in record` and `record`
    comes from `asdict(row)` — a `CardSummary`, which has no such field. So the
    assignment silently no-ops on one of the two arrays and a consumer reading
    `gpus[].quota_id` gets a KeyError while the same name works next door."""
    cloud = Cloud(quotas=rtx_three_ways(), preferences=DENIED_RTX)
    # Without the table flag, for the reason above.
    payload = json.loads(quota_list(cloud, monkeypatch, "--json")
                         .output.split("\n", 1)[1])

    collapsed = {k for row in payload["gpus"] for k in row}
    per_region = {k for row in payload["by_region"] for k in row}
    assert "quota_id" in collapsed, sorted(collapsed)
    assert per_region - collapsed <= {"region"}, sorted(per_region - collapsed)
    assert collapsed - per_region <= {"where"}, sorted(collapsed - per_region)


def test_a_refusal_does_not_claim_it_kept_anything(monkeypatch):
    """F10. Printed one line above the refusal:

        warning: keeping the standing request for ... at 8; 2 would lower it
        h100: refusing to lower the standing request to 2   [exit 2]

    Nothing was kept. The command filed nothing and exited 2, and the sentence
    reads as though it proceeded at 8."""
    cloud = Cloud(quotas=THIS_PROJECT, preferences=STANDING_H100_AT_8)
    result = quota_request(cloud, monkeypatch, "--gpu", "h100", "--value", "2")

    assert result.exit_code == 2
    assert "keeping" not in result.output, result.output


def test_a_denied_ceiling_still_says_the_request_cannot_be_used(monkeypatch):
    """F3, and the remedy exists in the file — it just cannot print.

        H100-80GB    will ask Google for 8 in europe-west1
        any (global) Google refused an earlier request. Not asked again automatically

    `GPUS-ALL-REGIONS` is granted 1 and a raise to 2 was refused. An
    `a3-highgpu-8g` needs 8. So this irrevocable request, if granted, cannot start
    a machine — and the two facts sit four lines apart with nothing joining them.

    `setup.py` builds exactly that sentence ("that card comes as more GPUs than
    one machine"), and attaches it only to the branch that REQUESTS a ceiling
    raise. When the ceiling is denied, `settled` returns early and the sentence is
    never built — and the denied case is the one where the user most needs it,
    because it is the one that will not fix itself on the next run.
    """
    denied_ceiling = [preference(CEILING, granted=1, preferred=2,
                                 state_detail=DENIED_DETAIL, name="ceiling-no")]
    plan = {a.label: a for a in plan_quota(THIS_PROJECT, denied_ceiling,
                                           region="us-central1")}

    assert plan["H100-80GB"].outcome == REQUEST, plan["H100-80GB"]
    ceiling = plan["any (global)"]
    assert "H100-80GB" in ceiling.detail, (
        "the card that cannot fit under this ceiling is not named")
    assert "8" in ceiling.detail, ceiling.detail


def test_create_does_not_tell_you_to_file_a_request_google_refused():
    """F5. Two surfaces, one card, opposite advice:

        $ comfy-qat create --os linux --gpu a100 --dry-run
        to fix: comfy-qat quota request --gpu a100, then wait for Google

        $ comfy-qat quota list --region us-central1
        A100  0  us-central1  denied — Google refused this; asking again will not help

    One says ask and wait; the other says asking will not help. And the remedy as
    printed does the futile thing — `quota request --gpu a100` derives
    us-central1, the region it was refused in, and warns so itself.

    `quota list` also reports `never asked in 42`, so the useful remedy is a
    DIFFERENT REGION. `create` did not read the preferences it would need to know
    any of this; now it does, and a failure to read them degrades to the old
    wording rather than blocking a create.
    """
    from comfy_qa.create import CARDS, check_quota

    refused_here = [preference(A100, granted=0, preferred=1,
                               state_detail=DENIED_DETAIL, name="a100-usc1",
                               dimensions={"region": "us-central1"})]
    # METERED SOMEWHERE ELSE, because "ask in a different region" is only the
    # remedy when a different region exists — `THIS_PROJECT` meters A100 in
    # us-central1 alone, which is where it was refused, and the honest answer
    # there is that there is nowhere else. That case has its own test.
    spread = [q for q in THIS_PROJECT if q["quotaId"] != A100] + [
        quota(A100, 0, locations=REGIONS_43)]
    problem = check_quota(CARDS["a100"], spread, [], "",
                          preferences=refused_here).problem()

    assert problem is not None
    assert "wait for Google" not in str(problem.fix), problem.fix
    assert "refused" in str(problem).lower() or "refused" in str(problem.fix).lower()
    assert "--region" in str(problem.fix), problem.fix


def test_create_still_says_ask_and_wait_when_nothing_was_refused():
    """The other half: a card nobody has asked about still gets the plain
    remedy, and a preference list that could not be read behaves the same way."""
    from comfy_qa.create import CARDS, check_quota

    plain = check_quota(CARDS["a100"], THIS_PROJECT, [], "", preferences=[])
    assert "wait for Google" in str(plain.problem().fix)

    unread = check_quota(CARDS["a100"], THIS_PROJECT, [], "", preferences=None)
    assert "wait for Google" in str(unread.problem().fix)


@pytest.mark.parametrize("surface", ["list", "request", "setup"])
def test_an_empty_region_is_an_error_not_an_omission(surface, monkeypatch,
                                                     tmp_path):
    """F11, and it is a decision rather than a defect report. `--region ""` fell
    out of an `if region:` truthiness test and was treated exactly like omitting
    the flag — planning against a derived region and, on a real run, filing an
    irrevocable request there.

    The realistic source of an empty value is `--region "$REGION"` in a script
    with `REGION` unset. Someone who typed the flag has said they care which
    region; silently choosing one for them is the one reading that cannot be
    what they meant.

    Omitting the flag entirely is unchanged and still derives a region.
    """
    cloud = Cloud(quotas=THIS_PROJECT)
    # THE MESSAGE, not just the exit code. Falling through to "no such region
    # ''" also exits 2, so an exit-code-only assertion cannot tell the guard from
    # its absence — which a mutation sweep said out loud.
    if surface == "list":
        result = quota_list(cloud, monkeypatch, "--region", "")
    elif surface == "request":
        result = quota_request(cloud, monkeypatch, "--gpu", "l4", "--region", "")
    else:
        with pytest.raises(SetupStopped) as stopped:
            run(cloud, config_path=tmp_path / "hosts.toml", region="",
                quota_dry_run=True)
        assert "unset" in str(stopped.value), stopped.value
        return

    assert result.exit_code == 2
    assert "unset" in result.output, result.output


def test_omitting_the_region_entirely_still_derives_one(tmp_path):
    """The other half, so the guard above does not quietly become "a region is
    mandatory" — which would break the fresh-install flow the whole feature is
    for."""
    cloud = Cloud(quotas=THIS_PROJECT)
    p = run(cloud, config_path=tmp_path / "hosts.toml", quota_dry_run=True)

    assert any("quota plan" in line for line in p.said), p.said


# --- pass 8: "never computed" must not be spellable as "computed and empty" ---


def test_json_by_region_carries_verdicts_without_the_table_flag(monkeypatch):
    """THE INSTANCE. `--json` emits `by_region` ALWAYS; `stocks` was computed
    only when `--by-region` was ALSO passed. So:

        $ comfy-qat quota list --json          # no --by-region
        by_region rows: 140, offered_here null: 140
        K80 rows: 25, statuses: ['ready']

    The machine interface said K80 is ready in 25 regions while the human table
    said "not offered here" for all 25 of them in the same run, and K80 exists
    nowhere in Google's catalogue. That is the documented scripting surface.

    THE RULE THIS TEST FOLLOWS, and the reason it exists at all: a test asserting
    on a JSON array must drive the invocation WITHOUT the flag that shapes the
    corresponding table. Both tests written for this defect passed `--by-region`,
    which is the flag that masks it.
    """
    per_region = {"quotaId": K80, "dimensionsInfos": [
        {"dimensions": {"region": r}, "details": {"value": "1"},
         "applicableLocations": [r]} for r in ("asia-east1", "us-central1")]}
    cloud = Cloud(quotas=[per_region, quota(L4, 1, locations=["us-central1"])],
                  accelerators=stocking("us-central1", "nvidia-l4"))

    payload = json.loads(quota_list(cloud, monkeypatch, "--json")
                         .output.split("\n", 1)[1])
    k80 = [r for r in payload["by_region"] if r["gpu"] == "K80"]

    assert k80, "the fixture produced no K80 rows"
    assert all(r["offered_here"] is False for r in k80), k80


def test_the_two_surfaces_agree_without_the_table_flag(monkeypatch):
    """The same run, both surfaces, `--by-region` passed to neither. The defect
    was visible only by comparing them, which is why neither test caught it."""
    per_region = {"quotaId": K80, "dimensionsInfos": [
        {"dimensions": {"region": r}, "details": {"value": "1"},
         "applicableLocations": [r]} for r in ("asia-east1", "us-central1")]}
    cloud = Cloud(quotas=[per_region, quota(L4, 1, locations=["us-central1"])],
                  accelerators=stocking("us-central1", "nvidia-l4"))

    payload = json.loads(quota_list(cloud, monkeypatch, "--json")
                         .output.split("\n", 1)[1])
    table = quota_list(cloud, monkeypatch, "--by-region").output

    for row in [r for r in payload["by_region"] if r["gpu"] == "K80"]:
        line = next(l for l in table.splitlines()
                    if l.startswith("K80 ") and row["region"] in l)
        assert (row["offered_here"] is False) == ("not offered here" in line), (
            row, line)


def test_an_unlooked_availability_cannot_answer(monkeypatch):
    """THE CLASS, not the instance. A computation that never ran and one that ran
    and found nothing are different facts, and the guard `if per_region and
    stocks` could not tell them apart — an empty dict meant both, so a surface
    that forgot to compute degraded silently to a confident `ready`.

    This is absent-versus-zero ONE LEVEL UP: not a missing value versus zero, but
    a missing COMPUTATION versus one that found nothing. The repair is to make
    "nobody looked" a state the type can hold and refuse to answer from it.
    """
    from comfy_qa.auth import Availability

    looked = Availability(looked=True, where={"L4": set()})
    assert looked.offers("L4", "us-central1") is False
    assert looked.offers("T4", "us-central1") is None, "unknown card, not absent"

    blind = Availability.not_checked()
    assert blind.offers("L4", "us-central1") is None
    assert not blind, "an unlooked availability must be falsy, like the old {}"

    # HAND-BUILT, because `not_checked()` always carries an empty `where` — so
    # against it the `looked` guard and an empty dict are indistinguishable, and
    # a sweep said so by surviving its removal. This is the state the guard
    # actually defends: data present, nobody having established it applies.
    stale = Availability(looked=False, where={"L4": {"us-central1"}})
    assert stale.offers("L4", "us-central1") is None, (
        "answered from an availability nobody computed")


def test_quota_request_availability_is_the_same_type_not_a_second_dict():
    """THE LAST COPY OF THE REPRESENTATION. `quota request` kept its own
    `sells: dict = {}`, computed under `if region is not None` and read by
    `sells.get(name)` — the identical shape that produced the `--json` defect.

    It was CORRECT, but only because its consumers happen to be gated on the same
    flag as its producer. That is a property of two call sites agreeing, not of
    the data, and the whole point of this round is that such agreements are what
    keep breaking. One type, one place where "nobody looked" is expressible, and
    the agreement stops being load-bearing.
    """
    import ast
    import pathlib

    source = pathlib.Path("comfy_qa/auth.py").read_text()
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "quota_request_cmd")
    defaults = [ast.unparse(s.value) for s in fn.body
                if isinstance(s, ast.AnnAssign | ast.Assign) and s.value is not None
                and ast.unparse(s.value) in {"{}", "dict()"}]

    assert "{}" not in defaults, (
        "an availability-shaped dict defaulted to {} again: "
        "'never computed' and 'computed and empty' are different facts")


def test_the_ceiling_clause_reports_the_ceiling_not_the_ask():
    """Pass 8, finding 2. One argument:

        any (global)  ... NOTE: H100-80GB needs 8 of this ceiling and it is 2

    `GPUS-ALL-REGIONS` is granted 1. The 2 is the REFUSED request. `stuck =
    _too_big_for(plan, wanted)` passes `wanted` — the number to ask for, floored
    at CEILING_REQUEST — where the sentence needs `ceiling`, the limit in force,
    bound one line earlier.

    Three other surfaces get this right, which is what makes it a single-site
    slip rather than a shared misreading: `quota list` prints `1  global  ready —
    1 granted; a raise to 2 was not`, `--json` gives `limit: 1, asked_gpus: 2`,
    and `create` prints `GPUS_ALL_REGIONS ...: 1`.

    The `raising=True` call is CORRECT to pass `wanted` — that sentence is about
    the ask. Only the denied branch is wrong, so this pins both.
    """
    denied_ceiling = [preference(CEILING, granted=1, preferred=2,
                                 state_detail=DENIED_DETAIL, name="ceiling-no")]
    ceiling_at_one = [q for q in THIS_PROJECT if q["quotaId"] != CEILING] + [
        quota(CEILING, 1, locations=["global"])]
    plan = {a.label: a for a in plan_quota(ceiling_at_one, denied_ceiling,
                                           region="us-central1")}

    detail = plan["any (global)"].detail
    assert "and it is 1" in detail, detail
    assert "and it is 2" not in detail, detail


def test_a_flag_that_is_ignored_says_so(monkeypatch):
    """Pass 8, finding 3. The tool refuses an empty region, a zone-shaped one, an
    uppercase one and an unknown one — and then silently ignores a perfectly
    valid one on the id that takes no dimensions. Out of character rather than
    dangerous, which is the argument for saying it rather than for refusing."""
    ceiling = [quota(CEILING, 1, locations=["global"]),
               quota(L4, 1, locations=["us-central1"])]

    ignored = quota_request(Cloud(quotas=ceiling), monkeypatch, "--quota-id",
                            CEILING, "--region", "us-central1", "--value", "2",
                            "--dry-run")
    # The `--region` half stays a warning: the request IS correct and does go
    # ahead, so there is nothing to refuse. Asserted on the command that was
    # built, not only on the sentence printed.
    assert "ignored" in ignored.output.lower(), ignored.output
    assert ignored.exit_code == 0, ignored.output
    assert all("--dimensions" not in a for args in ignored.output.splitlines()
               for a in args.split()), ignored.output

    # THE `--gpu` HALF IS NOW A REFUSAL, and this assertion is why. It checked
    # that the word "ignored" was PRINTED and never that anything was ignored —
    # so it passed while the tool filed three permanent requests, two of them for
    # the cards it had just named as ignored. Assert on what was SENT.
    cloud = Cloud(quotas=ceiling)
    both = quota_request(cloud, monkeypatch, "--quota-id", L4, "--gpu", "l4",
                         "--region", "us-central1", "--value", "1", "--dry-run")
    assert both.exit_code == 2, both.output
    assert cloud.submitted == [], both.output


def test_the_release_path_names_googles_own_decrease_guards(monkeypatch):
    """Pass 8, finding 4, and it is INFERRED rather than verified — confirming it
    would mean filing an irrevocable request.

    What IS verified, from `gcloud quotas preferences update --help` on this
    machine: `--allow-high-percentage-quota-decrease` and
    `--allow-quota-decrease-below-usage` exist. Going 1 -> 0 is a 100% decrease,
    so Google may well refuse the command this tool prints.

    NOT EMITTED AUTOMATICALLY. Those flags exist to override Google's own safety
    checks on the one path here that destroys something, and `--release-quota` is
    deliberate friction rather than a formality — silently adding the overrides
    would undo the point of it. Named instead, so a refusal is legible and the
    choice to override stays the user's.
    """
    cloud = Cloud(quotas=[quota(T4, 1, locations=["us-central1"])])
    result = quota_request(cloud, monkeypatch, "--gpu", "t4", "--value", "0",
                           "--allow-lower", "--release-quota", "--dry-run")

    assert "--allow-high-percentage-quota-decrease" in result.output, result.output
    assert "--allow-high-percentage-quota-decrease" not in " ".join(
        a for args in cloud.submitted for a in args), "the override was emitted"


def test_no_json_array_assertion_hides_behind_the_table_flag():
    """THE RULE THIS ROUND PRODUCED, made permanent.

    `--json` emits `by_region` whether or not `--by-region` is given, so a test
    that asserts on that array while passing the flag is exercising a path the
    flag has already configured — and the flag is exactly what made the defect
    invisible. Both tests written for it passed `--by-region`; removing only that
    argument from the repo's own fixture made one of them fail.

    So: a test asserting on a JSON array drives the invocation WITHOUT the flag
    that shapes the corresponding table. Four existing tests were converted; none
    of them needed any other change, which is the tell that the flag was doing
    nothing for them but hiding this.
    """
    import ast
    from pathlib import Path

    masked = []
    for path in sorted(Path(__file__).resolve().parent.glob("*.py")):
        tree = ast.parse(path.read_text())
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            if "by_region" not in ast.unparse(fn):
                continue
            for node in ast.walk(fn):
                if not isinstance(node, ast.Call):
                    continue
                if getattr(node.func, "id", "") not in {
                        "quota_list", "_json_of", "_by_region_json"}:
                    continue
                args = [a.value for a in node.args
                        if isinstance(a, ast.Constant) and isinstance(a.value, str)]
                if "--json" in args and "--by-region" in args:
                    masked.append(f"{path.name}::{fn.name}")

    assert not masked, (
        "a JSON-array assertion passes the flag that shapes the table:\n  "
        + "\n  ".join(masked))


# --- pass 9: one identity, whichever flag names it ---------------------------


def test_the_same_request_through_either_flag_gets_the_same_treatment(monkeypatch):
    """TENTH ABSENT-VERSUS-ZERO, now inside the GUARD POPULATION rather than the
    data. `sells` was seeded from `--gpu` only, so `--quota-id` handed the
    catalogue guard an empty name set — and an empty set read as "nothing to
    check" rather than "I was not told what to check".

        $ quota request --quota-id NVIDIA-L4-GPUS-per-project-region --region africa-south1
        exit=0, and a permanent preference for a region selling zero NVIDIA cards

        $ quota request --gpu l4 --region africa-south1
        exit=2, with a remedy naming eighteen regions that do sell it

    Identical requests. The flag chose whether a guard ran.
    """
    cloud = Cloud(quotas=[quota(L4, 1, locations=REGIONS_43)],
                  accelerators=stocking("us-central1", "nvidia-l4"))
    raw = quota_request(cloud, monkeypatch, "--quota-id", L4, "--region",
                        "africa-south1", "--value", "1", "--dry-run")

    assert raw.exit_code == 2, raw.output
    assert cloud.submitted == []
    assert "africa-south1" in raw.output


@pytest.mark.parametrize("guard, args_gpu, args_raw, fixture", [
    ("region sells no such card",
     ("--gpu", "l4", "--region", "africa-south1"),
     ("--quota-id", L4, "--region", "africa-south1"),
     dict(quotas=[quota(L4, 1, locations=REGIONS_43)],
          accelerators=lambda: stocking("us-central1", "nvidia-l4"))),
    ("already refused",
     ("--gpu", "a100-80gb", "--region", "europe-west4"),
     ("--quota-id", A100_80, "--region", "europe-west4"),
     dict(quotas=[quota(A100_80, 0, locations=["europe-west4"])],
          preferences=lambda: PREFS_DENIED)),
])
def test_both_flags_reach_the_same_guards(guard, args_gpu, args_raw, fixture,
                                          monkeypatch):
    """THE CLASS, not the instance. A guard that runs on one entry path and not
    the other is a second-site by construction, and both of this round's findings
    were the unfixed half of a pair whose first half already carries a comment
    counting itself as the thirteenth and seventeenth occurrence.

    Same project, same region, same quota — only the flag differs. Whatever the
    tool says about one it must say about the other.
    """
    def build():
        kw = {k: (v() if callable(v) else v) for k, v in fixture.items()}
        return Cloud(**kw)

    by_gpu = quota_request(build(), monkeypatch, *args_gpu, "--value", "1",
                           "--dry-run")
    by_raw = quota_request(build(), monkeypatch, *args_raw, "--value", "1",
                           "--dry-run")

    assert by_gpu.exit_code == by_raw.exit_code, (
        f"{guard}: --gpu exits {by_gpu.exit_code}, --quota-id exits "
        f"{by_raw.exit_code}\n--- gpu ---\n{by_gpu.output}\n--- raw ---\n"
        f"{by_raw.output}")


def test_setup_validates_the_region_whatever_flags_follow(tmp_path):
    """Pass 9, finding 2, and the THIRD time an early return has bypassed checks
    the ordinary path runs.

        $ comfy-qat setup --dry-run --no-quota-request --region us-centrall
        exit=0
        GPU quota: a project-wide allowance only, no specific card granted

    — on a project holding six cards, because `region_problem` sits BELOW the
    `if not submit:` return while `ensure_gpu_quota` consumes the unvalidated
    region above it.

    A guard whose reachability depends on which flags were passed is a guard that
    will be missed again, so the validation moves above every early return: what
    is checked must not depend on how the command was invoked.
    """
    for flags in ({"quota_requests": False}, {"quota_dry_run": True}, {}):
        cloud = Cloud(quotas=THIS_PROJECT)
        with pytest.raises(SetupStopped) as stopped:
            run(cloud, config_path=tmp_path / "hosts.toml",
                region="us-centrall", **flags)
        assert "us-centrall" in str(stopped.value), (flags, stopped.value)
        assert cloud.submitted == [], flags


def test_the_skipped_remedy_does_not_name_a_region_that_sells_nothing(tmp_path):
    """FOUND BY MY OWN SWEEP, not by the report — the twin of finding 2, one
    function over. The availability check also sits below `if not submit:`, so:

        $ setup --no-quota-request --region africa-south1
        To ask later: comfy-qat quota request --gpu h100 --region africa-south1

    and that command exits 2, because `quota request` refuses a region selling no
    such card. Eleventh instance of a printed remedy that cannot run.

    NOT by stopping the run: with `--no-quota-request` nothing irrevocable
    happens, and refusing the whole setup over it would be refusing to do the
    job. The remedy stops naming a region that cannot work, which is the actual
    defect.
    """
    cloud = Cloud(quotas=THIS_PROJECT,
                  accelerators=stocking("us-central1", "nvidia-h100-80gb"))
    p = run(cloud, config_path=tmp_path / "hosts.toml", region="africa-south1",
            quota_requests=False)
    later = next(l for l in p.said if "ask later" in l.lower())

    assert "--region africa-south1" not in later, later
    assert "africa-south1" in later, (
        "it still has to say the region asked for offers nothing")


def test_the_request_step_validates_its_own_region_when_called_directly():
    """The hoist, tested at the entry it defends. Going through `run_setup` does
    not exercise it — `run_setup` validates first, so removing the check inside
    `ensure_quota_requests` kills nothing, which a sweep said by surviving.

    That is the whole class in miniature: a guard that appears to work because
    another caller happens to check first. `ensure_quota_requests` is reachable
    directly, and what it validates must not depend on who called it.
    """
    from comfy_qa.setup import ensure_quota_requests

    cloud = Cloud(quotas=THIS_PROJECT)
    with pytest.raises(SetupStopped) as stopped:
        ensure_quota_requests(cloud.gcloud(), prompts(), "proj-1",
                              quotas=THIS_PROJECT, interactive=False,
                              region="us-centrall", submit=False)

    assert "us-centrall" in str(stopped.value)


# --- pass 10: a warning that does not change behaviour is worse than none -----


def test_naming_both_flags_is_refused_rather_than_half_honoured(monkeypatch):
    """THE WORST DEFECT LEFT, and a new shape: not a false sentence about the
    world, but a false sentence about the tool's OWN NEXT ACTION.

        $ quota request --gpu l4,t4 --quota-id NVIDIA-A100-GPUS-... --region us-central1
        warning: --gpu l4,t4 ignored: --quota-id names the quota exactly
        ...update comfyqat_nvidia-a100-gpus-...   <- the --quota-id
        ...update a0e3b926-...                    <- L4, and that is the EXISTING
                                                     granted preference, not a minted id
        ...update comfyqat_nvidia-t4-gpus-...     <- T4

    Three permanent requests, two for cards it had just said it was ignoring, one
    of them reaching into a live granted preference. The warning told the user
    they had been protected from the thing that then happened.

    REFUSED, not fixed by making the warning true. The two flags were resolved to
    one identity last round precisely so two paths could not diverge; this is the
    same ambiguity resurfacing at the argument layer, and the honest answer is
    that the command cannot know which the user meant.
    """
    cloud = Cloud(quotas=THIS_PROJECT)
    result = quota_request(cloud, monkeypatch, "--gpu", "l4,t4", "--quota-id",
                           A100, "--region", "us-central1", "--value", "1")

    assert cloud.submitted == [], (
        f"filed {len(cloud.submitted)} request(s) after saying it was ignoring "
        f"the cards")
    assert result.exit_code == 2
    assert "ignored" not in result.output.lower(), (
        "a warning that does not change behaviour is worse than none")


def test_a_bad_card_beside_a_good_raw_id_does_not_lose_the_request(monkeypatch):
    """The opposite failure from the same seam: `--gpu banana` beside a valid
    `--quota-id` aborted at exit 2, so the request the user genuinely asked for
    never landed. Refusing the COMBINATION answers both — one message, before
    anything is read, rather than two different failures depending on whether the
    card name happened to parse."""
    cloud = Cloud(quotas=THIS_PROJECT)
    result = quota_request(cloud, monkeypatch, "--gpu", "banana", "--quota-id",
                           A100, "--region", "us-central1", "--value", "1")

    assert result.exit_code == 2
    assert cloud.submitted == []
    assert "--quota-id" in result.output and "--gpu" in result.output


def test_the_same_card_twice_files_one_request(monkeypatch):
    """`--gpu l4,l4` filed the identical preference TWICE — the duplicate this
    whole feature exists to prevent, reachable by typing a card name twice."""
    cloud = Cloud(quotas=[quota(L4, 0, locations=["us-central1"])])
    quota_request(cloud, monkeypatch, "--gpu", "l4,l4", "--region",
                  "us-central1", "--value", "1")

    assert len(cloud.submitted) == 1, cloud.submitted


def test_setup_validates_the_region_before_reporting_on_it(tmp_path):
    """THE SHARPEST PROCESS FINDING OF THE NIGHT, and it is about my own fix.

    Round nine hoisted `_require_real_region` above every early return in
    `ensure_quota_requests`, wrote a comment saying so, and pinned it with a test
    whose docstring quotes the defect. All true — of that function. The CALLER
    one frame up still ran `ensure_gpu_quota` with the unvalidated region first,
    so the live tool went on printing

        GPU quota: a project-wide allowance only, no specific card granted

    for `--region us-centrall` on a project holding six granted cards — the exact
    sentence the round-9 comment quotes as fixed.

    A fix reported done, commented as done, and covered by a passing test, with
    the defect still live. The test exercised the function; the command has a
    frame above it.
    """
    cloud = Cloud(quotas=THIS_PROJECT)
    p = prompts()
    with pytest.raises(SetupStopped):
        run(cloud, prompts=p, config_path=tmp_path / "hosts.toml",
            region="us-centrall")

    assert not any("project-wide allowance only" in line for line in p.said), (
        "reported on a region it had not validated:\n  " + "\n  ".join(p.said))


# --- pass 10, the rest ------------------------------------------------------


def test_the_negative_remedy_keeps_the_flag_the_user_typed(monkeypatch):
    """U4, and it is the SEVENTH instance of this shape, inside the block whose
    own comment enumerates six and ends "a remedy that rewrote `--quota-id` to
    `--gpu` and exited 2".

        $ quota request --quota-id NVIDIA-L4-GPUS-... --value -1
        to fix: comfy-qat quota request --gpu NVIDIA-L4-GPUS-... --value 1

        $ comfy-qat quota request --gpu NVIDIA-L4-GPUS-... --value 1
        no card called 'NVIDIA-L4-GPUS-per-project-region'   [exit 2]

    Every other remedy in that block preserves the flag — the ceiling case prints
    `--quota-id GPUS-ALL-REGIONS-per-project` correctly. One branch hardcoded
    `--gpu`.
    """
    cloud = Cloud(quotas=[quota(L4, 1, locations=["us-central1"])])
    result = quota_request(cloud, monkeypatch, "--quota-id", L4, "--region",
                           "us-central1", "--value", "-1", "--dry-run")

    assert result.exit_code == 2
    assert f"--quota-id {L4}" in result.output, result.output
    assert f"--gpu {L4}" not in result.output, result.output


@pytest.mark.parametrize("surface", ["list", "request"])
def test_a_decidable_region_is_refused_without_reading_quota(surface,
                                                             monkeypatch):
    """U6. A typo'd region cost 51 seconds before rejection, because the check
    ran after the minute-long quota read. Empty, zone-shaped and wrong-case are
    decidable with ZERO API calls — only "not one of this project's 43" needs the
    records."""
    # BOTH COMMANDS. Covering only one left the other's cheap check surviving its
    # own removal, because `region_problem` catches the same input after the read
    # — so the refusal alone cannot tell the early check from its absence.
    cloud = Cloud(quotas=THIS_PROJECT)
    result = (quota_list(cloud, monkeypatch, "--region", "us-central1-a")
              if surface == "list" else
              quota_request(cloud, monkeypatch, "--gpu", "l4", "--region",
                            "us-central1-a"))

    assert result.exit_code == 2, result.output
    assert cloud.quota_reads == 0, (
        f"read quota {cloud.quota_reads} time(s) to reject a zone name")


def test_the_dry_run_command_is_pasteable_with_a_real_justification(monkeypatch):
    """U7. `--dry-run`'s stated purpose in this file is `--dry-run | sh`, and a
    justification is prose:

        ... --justification=QA for Comfy Org --email=...

    Pasted, that sends `--justification=QA` and two stray positional arguments.
    """
    import shlex

    cloud = Cloud(quotas=[quota(T4, 0, locations=["us-central1"])])
    result = quota_request(cloud, monkeypatch, "--gpu", "t4", "--region",
                           "us-central1", "--dry-run", "--justification",
                           "QA for Comfy Org")
    line = next(l for l in result.output.splitlines() if l.startswith("gcloud "))
    parts = shlex.split(line)

    assert "--justification=QA for Comfy Org" in parts, parts


def test_the_create_remedy_names_regions_rather_than_a_placeholder():
    """U5. The remedy reads:

        comfy-qat quota list --by-region   # where this card is metered
        comfy-qat quota request --gpu h100 --region <one of them>

    and running the first gives two rows for H100: the refused `us-central1`, and
    `any of 42` — a bucket that names nothing. There is no "them" to pick one of.

    The regions ARE in hand: `create` has just read the quota records, and
    `regions_metered` lists every region the card is metered in. Naming two or
    three of them, excluding the one it was refused in, turns a placeholder into
    something pasteable.
    """
    from comfy_qa.create import CARDS, check_quota

    refused_here = [preference(A100, granted=0, preferred=1,
                               state_detail=DENIED_DETAIL, name="a100-usc1",
                               dimensions={"region": "us-central1"})]
    # METERED WIDELY, like the live project — `THIS_PROJECT` meters A100 in
    # us-central1 alone, which is the region it was refused in, so there would
    # genuinely be nowhere else and the test would be asserting the wrong half.
    spread = [q for q in THIS_PROJECT if q["quotaId"] != A100] + [
        quota(A100, 0, locations=REGIONS_43)]
    fix = str(check_quota(CARDS["a100"], spread, [], "",
                          preferences=refused_here).problem().fix)

    assert "<one of them>" not in fix, fix
    named = [r for r in REGIONS_43 if r in fix and r != "us-central1"]
    assert named, f"no region named in the remedy: {fix}"


def test_the_create_remedy_says_so_when_there_is_nowhere_else():
    """The other half, and it is the case the live project is actually in: A100
    is metered in one region and refused in that region. A placeholder would
    imply somewhere else exists."""
    from comfy_qa.create import CARDS, check_quota

    refused_here = [preference(A100, granted=0, preferred=1,
                               state_detail=DENIED_DETAIL, name="a100-usc1",
                               dimensions={"region": "us-central1"})]
    fix = str(check_quota(CARDS["a100"], THIS_PROJECT, [], "",
                          preferences=refused_here).problem().fix)

    assert "nowhere else" in fix, fix
    assert "<" not in fix, f"a placeholder implying a region exists: {fix}"


# --- final: one answer to "where could this actually be requested" ------------


def test_the_create_remedy_only_names_regions_that_can_work():
    """Q1. TWENTY-FIRST SECOND-SITE, thirteenth remedy-that-cannot-work, and it
    is in the command people actually run:

        to fix: comfy-qat quota request --gpu h100 --region africa-south1
                # or africa-south1, asia-east1, asia-east2

    `africa-south1` stocks zero NVIDIA accelerators, so that command exits 2 —
    and it is listed twice in its own alternatives, which tells the reader the
    tool checked when it did not.

    `_refuse_if_unsold` and the stocked-region helper exist so this could not
    happen twice. `create` composed its own suggestion from `regions_metered`
    alone instead of asking them.
    """
    from comfy_qa.create import CARDS, check_quota

    refused_here = [preference(A100, granted=0, preferred=1,
                               state_detail=DENIED_DETAIL, name="a100-usc1",
                               dimensions={"region": "us-central1"})]
    spread = [q for q in THIS_PROJECT if q["quotaId"] != A100] + [
        quota(A100, 0, locations=REGIONS_43)]
    # Stocked in asia-east1 only, of the regions it is metered in.
    askable = ["asia-east1"]
    fix = str(check_quota(CARDS["a100"], spread, [], "",
                          preferences=refused_here, askable=askable).problem().fix)

    assert "africa-south1" not in fix, fix
    assert "asia-east1" in fix, fix
    assert fix.count("asia-east1") <= 2, f"named the same region twice: {fix}"


def test_setup_does_not_say_nothing_was_missing_when_cards_are_at_zero(tmp_path):
    """Q2. `--no-quota-request` said "Nothing was missing anyway" on a project
    where three of five drivable cards sit at zero.

    `_missing_cards` returns the cards the plan would ASK for, and a refused card
    does not submit — so "nothing to ask for" came out as "nothing missing". They
    are different facts, and this is the one place the whole feature's premise is
    stated back to the user.
    """
    # EVERY DRIVABLE CARD SETTLED, which is the live project: L4 and T4 granted,
    # A100, A100-80GB and H100 all refused. `submits` is false for all five, so
    # "nothing to ask for" came out as "nothing missing".
    settled_all = PREFS_DENIED + [
        preference(A100, granted=0, preferred=1, state_detail=DENIED_DETAIL,
                   name="a100-usc1", dimensions={"region": "us-central1"}),
        preference(FAMILY, granted=0, preferred=8, state_detail=DENIED_DETAIL,
                   name="h100-usc1",
                   dimensions={"gpu_family": "NVIDIA_H100",
                               "region": "us-central1"}),
    ]
    cloud = Cloud(quotas=THIS_PROJECT, preferences=settled_all)
    p = run(cloud, config_path=tmp_path / "hosts.toml", quota_requests=False)
    line = next(l for l in p.said if "no-quota-request" in l)

    assert "Nothing was missing" not in line, line
    assert "refused" in line.lower() or "denied" in line.lower(), line


# --- pass 12: four false sentences on error paths ----------------------------


def test_nowhere_else_does_not_claim_the_card_is_metered_nowhere_else():
    """V2, and it is my own sentence from the last round. `elsewhere` is metered
    AND STOCKED minus refused, so empty has two causes — and the message claims
    only one of them:

        Google already refused A100 in us-central1, and that is the only region
        this project meters it in

    A card metered in forty-three regions and stocked in none of the other
    forty-two hits this branch, and that sentence is false about it.
    """
    from comfy_qa.create import CARDS, check_quota

    refused_here = [preference(A100, granted=0, preferred=1,
                               state_detail=DENIED_DETAIL, name="a100-usc1",
                               dimensions={"region": "us-central1"})]
    spread = [q for q in THIS_PROJECT if q["quotaId"] != A100] + [
        quota(A100, 0, locations=REGIONS_43)]
    fix = str(check_quota(CARDS["a100"], spread, [], "",
                          preferences=refused_here, askable=[]).problem().fix)

    assert "only region this project meters it in" not in fix, fix
    assert "nowhere else" in fix, fix


def test_a_non_gpu_quota_id_is_refused_for_the_right_reason(monkeypatch):
    """V3. `--quota-id CPUS-per-project-region` answered "this project reports no
    quota called 'CPUS-per-project-region'", which is false of every CPU and
    storage quota the project has. The list it checks against is `gpu_quotas`,
    already filtered, so the sentence describes the filter rather than the
    project — the derived-set class again."""
    cloud = Cloud(quotas=[quota(L4, 1, locations=["us-central1"])])
    result = quota_request(cloud, monkeypatch, "--quota-id",
                           "CPUS-per-project-region", "--region", "us-central1",
                           "--value", "1", "--dry-run")

    assert result.exit_code == 2
    assert "reports no quota called" not in result.output, result.output
    assert "GPU" in result.output, result.output


def test_the_gpu_flag_takes_the_same_spellings_everywhere(monkeypatch):
    """V4. `quota request --gpu h100-80gb` was accepted; `create --gpu h100-80gb`
    refuses it, and the card table says outright that `--gpu h100-80gb` "is not a
    command — the tool refuses it". One card, two vocabularies, on the two
    commands a person uses together.

    `create` is the one following the documented rule, so `quota request` stops
    accepting a display name — and says which spelling to use rather than
    "no card called", which would be false of a card it can plainly see.
    """
    cloud = Cloud(quotas=THIS_PROJECT)
    result = quota_request(cloud, monkeypatch, "--gpu", "h100-80gb",
                           "--region", "us-central1", "--value", "1",
                           "--dry-run")

    assert result.exit_code == 2, result.output
    assert cloud.submitted == []
    assert "--gpu h100" in result.output, result.output
