"""`comfy-qat auth`, driven through the binary.

test_auth.py exercises the pieces. This drives the commands a tester actually
types — status, login, quota list, quota request — through `CliRunner`, so exit
codes, `--json` payloads and the wait loop are covered as shipped rather than as
functions. The clock is replaced, so the half-hour wait takes no time at all.
"""

from __future__ import annotations

import copy
import json

import pytest
from typer.testing import CliRunner

from comfy_qa.cli import app
from comfy_qa.create import CARDS
from comfy_qa.gcloud import Gcloud, GcloudError

runner = CliRunner()


class FakeCloud:
    """A scripted project. A granted request shows up in the next quota read."""

    def __init__(
        self, *, account="ali@comfy.org", project="proj-1", billing=True,
        quotas=(), preferences=(), approve=False, quota_error=None,
    ):
        self.account = account
        self.project = project
        self.billing = billing
        # Deep, so a test that grants a card cannot alter the fixtures every
        # other test in this file shares.
        self.quotas = copy.deepcopy(list(quotas))
        self.preferences = list(preferences)
        self.approve = approve
        self.quota_error = quota_error
        self.calls: list[str] = []
        self.requests: list[list[str]] = []
        self.quota_reads = 0

    def gcloud(self) -> Gcloud:
        return Gcloud(runner=self._run)

    def _run(self, args, mode):
        if " ".join(args).startswith("info --format=value(basic.python_location)"):
            # `status` reports whether gcloud's own python has numpy. A path that
            # does not exist makes the check a no-op, which is what a test wants.
            return "/no/such/python"

        key = " ".join(args)
        self.calls.append(key)

        if key.startswith("auth list"):
            return [{"account": self.account, "status": "ACTIVE"}] if self.account else []
        if key.startswith("config get-value project"):
            return self.project or "(unset)"
        if key.startswith("billing projects describe"):
            if isinstance(self.billing, Exception):
                raise self.billing
            return {"billingEnabled": self.billing}
        if key.startswith("quotas info list"):
            self.quota_reads += 1
            if self.quota_error:
                raise self.quota_error
            return copy.deepcopy(self.quotas)
        if key.startswith("compute accelerator-types list"):
            # `quota list --region` and `quota request --region` both check that
            # the region actually offers the card, because holding quota for it
            # is not the same thing. These tests are not about availability, so
            # the answer is "offered" — permissive on purpose, so an unrelated
            # assertion never turns on a fact this fixture was never written to
            # express.
            #
            # THE UNFILTERED CALL IS ANSWERED TOO. This used to echo whatever
            # `--filter=name=` asked for, which made it permissive only to a
            # caller that filtered. `quota request` reads the whole catalogue in
            # one call, so the echo produced rows named `""`, every card matched
            # nothing, and the command refused every region as selling no GPUs —
            # a fixture answering one shape of a question it claims to answer
            # generally.
            name = next((a.split("=")[-1] for a in args
                         if a.startswith("--filter=name=")), "")
            ids = ([name] if name else
                   [c.accelerator for c in CARDS.values()])
            return [{"name": i, "zone": f"https://x/zones/{z}"}
                    for i in ids
                    for z in ("us-central1-a", "us-east1-a", "africa-south1-a")]
        if key.startswith("quotas preferences list"):
            return list(self.preferences)
        if key.startswith("quotas preferences update"):
            self.requests.append(list(args))
            if self.approve:
                self._grant(args)
            return {}
        raise AssertionError(f"unexpected gcloud call: {key}")

    def _grant(self, args):
        wanted = next(a.split("=", 1)[1] for a in args if a.startswith("--quota-id="))
        value = next(a.split("=", 1)[1] for a in args if a.startswith("--preferred-value="))
        for record in self.quotas:
            if record["quotaId"] == wanted:
                record["dimensionsInfos"][0]["details"] = {"value": value}


class Clock:
    """A clock that only moves when something sleeps on it."""

    def __init__(self):
        self.t = 0.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def quota(quota_id, value, locations=("us-central1",)):
    return {
        "quotaId": quota_id,
        "dimensionsInfos": [{
            "details": {} if value is None else {"value": str(value)},
            "applicableLocations": list(locations),
        }],
    }


REGIONS = ["africa-south1", "asia-east1", "europe-west4", "us-central1",
           "us-east1", "us-west1"]

L4 = quota("NVIDIA-L4-GPUS-per-project-region", 1, REGIONS)
A100 = quota("NVIDIA-A100-GPUS-per-project-region", None, REGIONS)
T4 = quota("NVIDIA-T4-GPUS-per-project-region", 0, REGIONS)
K80_PER_REGION = {
    "quotaId": "NVIDIA-K80-GPUS-per-project-region",
    "dimensionsInfos": [
        {"dimensions": {"region": name}, "details": {"value": "1"},
         "applicableLocations": [name]}
        for name in REGIONS
    ],
}


def run(cloud, *args, clock=None):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("comfy_qa.auth.Gcloud", lambda *a, **k: cloud.gcloud())
        if clock is not None:
            patch.setattr("comfy_qa.auth.time", clock)
        return runner.invoke(app, [*args])


# --- status ------------------------------------------------------------------


def test_status_reports_every_check_when_everything_is_ready():
    result = run(FakeCloud(quotas=[L4]), "status")

    assert result.exit_code == 0, result.output
    for name in ["gcloud", "account", "project", "billing", "gpu quota"]:
        assert name in result.output
    assert "fail" not in result.output


@pytest.mark.parametrize("cloud,last,unseen", [
    (FakeCloud(account=None), "account", ["project", "billing", "gpu quota"]),
    (FakeCloud(project=None), "project", ["billing", "gpu quota"]),
    (FakeCloud(billing=False), "billing", ["gpu quota"]),
    (FakeCloud(quotas=[T4]), "gpu quota", []),
])
def test_status_stops_at_the_first_failure_rather_than_reporting_five(cloud, last, unseen):
    """Every later check depends on the earlier ones, so five failures for one
    broken thing is noise, not detail."""
    result = run(cloud, "status")

    assert result.exit_code == 1
    lines = [line for line in result.output.splitlines() if line.startswith(("ok", "fail"))]
    assert lines[-1].startswith("fail")
    assert last in lines[-1]
    assert sum(line.startswith("fail") for line in lines) == 1
    for name in unseen:
        assert name not in result.output, f"{name} was checked after a failure"


def test_status_prints_the_fix_for_the_check_that_failed():
    result = run(FakeCloud(account=None), "status")
    assert "to fix: gcloud auth login" in result.output


def test_status_json_is_the_same_facts_in_a_fixed_shape():
    result = run(FakeCloud(quotas=[L4]), "status", "--json")

    payload = json.loads(result.stdout)
    assert [c["name"] for c in payload] == [
        "gcloud", "account", "project", "billing", "gpu quota", "numpy",
    ]
    for check in payload:
        assert set(check) == {"name", "ok", "detail", "fix"}


def test_status_json_carries_no_credential_or_token_of_any_kind():
    """The tool never holds a credential, so nothing it prints may leak one.

    gcloud's raw output is kept on the exception for classifying failures, and
    that output is exactly where a token would appear if one ever did.
    """
    leaky = GcloudError(
        "your gcloud session has expired",
        fix="gcloud auth login",
        raw="ERROR: token ya29.a0SECRETVALUE refresh_token=1//0gSECRET failed",
    )
    result = run(FakeCloud(billing=leaky), "status", "--json")

    assert result.exit_code == 1
    body = result.output
    for secret in ["ya29.", "refresh_token", "SECRETVALUE"]:
        assert secret not in body
    for word in ["token", "credential", "password", "secret", "access_key"]:
        assert word not in body.lower()
    assert json.loads(result.stdout)[-1]["detail"] == "your gcloud session has expired"


def test_status_never_asks_gcloud_to_print_a_token():
    cloud = FakeCloud(quotas=[L4])
    run(cloud, "status")
    assert not any("print-access-token" in call for call in cloud.calls)
    assert not any("print-identity-token" in call for call in cloud.calls)


def test_status_does_not_count_quota_that_cannot_start_an_ordinary_box():
    """A committed or preemptible allowance is real and starts nothing. Setup was
    fixed for this; status was still reporting green beside it."""
    cloud = FakeCloud(quotas=[
        quota("COMMITTED-NVIDIA-L4-GPUS-per-project-region", 8, REGIONS),
        quota("PREEMPTIBLE-NVIDIA-A100-GPUS-per-project-zone", 4, REGIONS),
        quota("NVIDIA-L4-VWS-GPUS-per-project-region", 2, REGIONS),
    ])
    result = run(cloud, "status")

    assert result.exit_code == 1
    assert "zero GPU quota on this project" in result.output
    assert "COMMITTED" not in result.output


def test_status_does_not_call_the_project_wide_ceiling_a_card():
    """`any (global)` on its own grants no card, so no GPU instance can start."""
    cloud = FakeCloud(quotas=[quota("GPUS-ALL-REGIONS-per-project", 1, ["global"])])
    result = run(cloud, "status")

    assert result.exit_code == 1
    assert "a project-wide allowance only, no specific card granted" in result.output


def test_status_names_cards_not_raw_quota_ids():
    result = run(FakeCloud(quotas=[L4]), "status")
    assert "L4=1" in result.output
    assert "per-project-region" not in result.output


# --- login -------------------------------------------------------------------


def test_login_hands_over_the_commands_and_signs_nobody_in(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("login must not touch gcloud")

    monkeypatch.setattr("comfy_qa.auth.Gcloud", explode)
    result = runner.invoke(app, ["login"])

    assert result.exit_code == 0
    assert "gcloud auth login" in result.output
    assert "gcloud config set project" in result.output
    assert "comfy-qat status" in result.output


# --- quota list --------------------------------------------------------------


def test_quota_list_collapses_to_one_line_per_card():
    """K80 comes back as one entry per region on a live project. A row each ran
    the table to 130 lines."""
    result = run(FakeCloud(quotas=[K80_PER_REGION, L4]), "quota", "list")

    assert result.exit_code == 0, result.output
    rows = [line for line in result.output.splitlines() if line.startswith(("K80", "L4"))]
    assert len(rows) == 2
    assert f"{len(REGIONS)} regions" in result.output


def test_by_region_expands_the_same_answer():
    result = run(FakeCloud(quotas=[K80_PER_REGION]), "quota", "list", "--by-region")

    assert "REGION" in result.output
    rows = [line for line in result.output.splitlines() if line.startswith("K80")]
    assert len(rows) == len(REGIONS)
    for name in REGIONS:
        assert name in result.output


def test_a_region_narrows_to_that_region():
    result = run(FakeCloud(quotas=[K80_PER_REGION]), "quota", "list", "--region", "us-east1")

    rows = [line for line in result.output.splitlines() if line.startswith("K80")]
    assert len(rows) == 1
    assert "us-east1" in result.output
    assert "africa-south1" not in result.output


def test_a_region_the_project_has_no_grant_in_reports_nothing():
    """An all-regions row was relabelled as whatever region you asked about, so
    `--region` invented a grant the project does not have there."""
    # europe-west4 has to be a region the project is metered in SOMEWHERE, or
    # `quota list` now refuses it as a typo — which is a different behaviour and
    # has its own test. The point here is the relabelling, not the validation.
    only_central = quota("NVIDIA-L4-GPUS-per-project-region", 1,
                         ["us-central1", "us-east1"])
    elsewhere = quota("NVIDIA-T4-GPUS-per-project-region", 0, ["europe-west4"])
    result = run(FakeCloud(quotas=[only_central, elsewhere]), "quota", "list",
                 "--region", "europe-west4")

    assert result.exit_code == 0, result.output
    assert "L4" not in result.output, (
        "an all-regions row was relabelled as the region asked about")
    assert "L4" not in result.output


def test_quota_json_is_clean_on_stdout_with_the_slow_warning_on_stderr():
    result = run(FakeCloud(quotas=[L4, A100]), "quota", "list", "--json")

    payload = json.loads(result.stdout)
    assert set(payload) == {"project", "gpus", "by_region"}
    assert payload["project"] == "proj-1"
    assert {card["gpu"] for card in payload["gpus"]} == {"L4", "A100"}
    assert "reading quota (about a minute)" in result.stderr


# --- quota you hold and this tool cannot use ---------------------------------
#
# `ready` in this table has only ever meant "Google will let you start it". It
# said nothing about whether the driver this tool installs can then bring the
# card up, and for four of the cards in `create.CARDS` it cannot: the open NVIDIA
# kernel module needs a GSP and Pascal, Volta and Kepler have none. So a project
# holding P100 quota read `P100 1 ready` off this table, ran `create --gpu p100`,
# and got a billing box whose GPU never initialised. Both halves belong here.


P100 = quota("NVIDIA-P100-GPUS-per-project-region", 1, REGIONS)


def test_a_card_this_tool_cannot_drive_is_never_shown_as_plainly_ready():
    result = run(FakeCloud(quotas=[P100, L4]), "quota", "list")

    assert result.exit_code == 0, result.output
    p100 = next(line for line in result.output.splitlines() if line.startswith("P100"))
    l4 = next(line for line in result.output.splitlines() if line.startswith("L4"))
    assert "this tool cannot drive it" in p100
    assert "this tool cannot drive it" not in l4, "the L4 is fine and must read so"


def test_the_table_says_why_once_underneath_rather_than_in_every_row():
    result = run(FakeCloud(quotas=[P100, L4]), "quota", "list")

    assert "GPU System Processor (GSP)" in result.output
    assert "Turing and newer" in result.output
    assert result.output.count("GPU System Processor") == 1, "once, under the table"


def test_the_same_mark_survives_by_region():
    """`--by-region` is the same answer expanded, so it cannot be the view where
    the card looks usable again."""
    result = run(FakeCloud(quotas=[P100]), "quota", "list", "--by-region")

    rows = [line for line in result.output.splitlines() if line.startswith("P100")]
    assert rows, "the P100 rows are still shown — the quota is real"
    assert all("this tool cannot drive it" in row for row in rows)


def test_the_json_carries_the_same_fact_as_a_field():
    """A script reading `--json` gets the answer too, not just a person reading
    a table. `status` is about Google's grant; `drivable` is about this tool."""
    payload = json.loads(run(FakeCloud(quotas=[P100, L4]), "quota", "list", "--json").stdout)

    by_card = {card["gpu"]: card for card in payload["gpus"]}
    assert by_card["P100"]["status"] == "ready", "Google's answer is unchanged"
    assert by_card["P100"]["drivable"] is False
    assert by_card["L4"]["drivable"] is True
    assert all("drivable" in row for row in payload["by_region"])


def test_requesting_a_card_this_tool_cannot_drive_is_refused_before_it_is_sent():
    """Approval takes days, and at the end of it `create` would still refuse.
    Nothing reaches Google."""
    cloud = FakeCloud(quotas=[P100, L4])
    result = run(cloud, "quota", "request", "--gpu", "p100", "--region", "us-central1")

    assert result.exit_code == 2, result.output
    assert "no point asking for P100 quota" in result.output
    assert "GPU System Processor" in result.output
    assert cloud.requests == [], "it asked Google for a card it cannot use"


def test_a_drivable_card_is_still_requested_normally():
    """The other half. A refusal that fired on everything would pass the test
    above and break the command."""
    cloud = FakeCloud(quotas=[P100, T4])
    result = run(cloud, "quota", "request", "--gpu", "t4", "--region", "us-central1",
                 "--no-wait")

    assert result.exit_code == 0, result.output
    assert len(cloud.requests) == 1


def test_status_fails_when_every_granted_card_is_one_this_tool_cannot_drive():
    """The project has GPU quota, Google would start the instance, and nothing
    this tool can build with it will ever see a GPU. That is not a passing row."""
    result = run(FakeCloud(quotas=[P100]), "status")

    line = next(line for line in result.output.splitlines() if "gpu quota" in line)
    assert "cards this tool cannot drive" in line
    assert "comfy-qat quota request --gpu t4,l4" in result.output


def test_nothing_usable_prints_the_command_that_fixes_it():
    result = run(FakeCloud(quotas=[T4, A100]), "quota", "list")

    assert "nothing is usable yet" in result.output
    assert "comfy-qat quota request --gpu" in result.output


def test_a_pending_request_is_shown_as_pending_not_missing():
    cloud = FakeCloud(quotas=[A100], preferences=[
        {"quotaId": "NVIDIA-A100-GPUS-per-project-region",
         "quotaConfig": {"preferredValue": 1}},
    ])
    result = run(cloud, "quota", "list")
    assert "pending — waiting on Google" in result.output


def test_bare_quota_is_the_list():
    result = run(FakeCloud(quotas=[L4]), "quota")
    assert result.exit_code == 0, result.output
    assert "L4" in result.output and "ready" in result.output


def test_quota_with_no_project_says_which_command_sets_one():
    result = run(FakeCloud(project=None), "quota", "list")

    assert result.exit_code == 2
    assert "no project set" in result.stderr
    assert "to fix: comfy-qat setup" in result.stderr


def test_a_quota_read_that_fails_is_a_message_not_a_traceback():
    cloud = FakeCloud(quota_error=GcloudError("gcloud timed out after 240s"))
    result = run(cloud, "quota", "list")

    assert result.exit_code == 2
    assert "gcloud timed out after 240s" in result.stderr


# --- quota request -----------------------------------------------------------


def test_request_needs_to_be_told_what_to_ask_for():
    result = run(FakeCloud(quotas=[L4]), "quota", "request")

    assert result.exit_code == 2
    assert "--gpu l4,a100" in result.stderr


def test_several_cards_go_in_one_command():
    cloud = FakeCloud(quotas=[T4, A100], approve=True)
    result = run(cloud, "quota", "request", "--gpu", "t4,a100",
                 "--region", "us-central1", clock=Clock())

    assert result.exit_code == 0, result.output
    ids = [a for args in cloud.requests for a in args if a.startswith("--quota-id=")]
    assert ids == [
        "--quota-id=NVIDIA-T4-GPUS-per-project-region",
        "--quota-id=NVIDIA-A100-GPUS-per-project-region",
    ]
    assert "granted: t4" in result.output and "granted: a100" in result.output
    assert "track them:" in result.output


def test_a_raw_region_scoped_id_needs_a_region():
    """THIS TEST USED TO PIN THE DEFECT, and its reasoning was the appealing
    half of a true statement:

        assert not any(a.startswith("--dimensions") ...), (
            "no region was named, so none is invented")

    Inventing a region would indeed be wrong. Filing the request anyway is worse:
    `-per-project-region` DEFINES a region dimension, the API requires every
    defined dimension to be set, and a real submission came back
    `INVALID_ARGUMENT: Dimension values must be set for all the dimensions`. The
    third option — refuse and say which flag is missing — was the one nobody
    wrote down.

    `auth.py` read `if region and needs_region(id)`, so the guard could not fire
    when the region was absent, which is the only case it was for.
    """
    cloud = FakeCloud(quotas=[T4], approve=True)
    result = run(cloud, "quota", "request",
                 "--quota-id", "NVIDIA-T4-GPUS-per-project-region",
                 "--value", "4", clock=Clock())

    assert result.exit_code == 2, result.output
    assert cloud.requests == [], "filed a request with a dimension unset"
    assert "--region" in result.output


def test_an_id_that_defines_no_dimensions_still_carries_none():
    """The other half, unchanged and still load-bearing:
    `GPUS-ALL-REGIONS-per-project` defines no dimensions, so it must be sent with
    none at all. "No region was named, so none is invented" is right HERE — the
    old test applied it to an id where it was wrong."""
    # The project must REPORT the id, or the command stops at "reports no quota
    # called ..." and this asserts nothing about dimensions.
    ceiling = {"quotaId": "GPUS-ALL-REGIONS-per-project",
               "dimensionsInfos": [{"details": {"value": "1"},
                                    "applicableLocations": ["global"]}]}
    cloud = FakeCloud(quotas=[T4, ceiling], approve=True)
    result = run(cloud, "quota", "request",
                 "--quota-id", "GPUS-ALL-REGIONS-per-project",
                 "--value", "4", clock=Clock())

    assert result.exit_code == 0, result.output
    assert not any(a.startswith("--dimensions") for a in cloud.requests[0])


def test_the_value_asked_for_is_the_value_waited_for():
    """Approval to 1 when 4 was asked for is not the request being granted."""
    cloud = FakeCloud(quotas=[T4], approve=True)
    clock = Clock()
    for record in cloud.quotas:
        record["dimensionsInfos"][0]["details"] = {"value": "1"}
    cloud.approve = False
    result = run(cloud, "quota", "request", "--gpu", "t4", "--value", "4", clock=clock)

    assert result.exit_code == 75
    assert "still pending: t4" in result.output


def test_dry_run_prints_the_call_and_asks_google_for_nothing():
    cloud = FakeCloud(quotas=[T4])
    result = run(cloud, "quota", "request", "--gpu", "t4", "--dry-run")

    assert result.exit_code == 0
    assert "gcloud quotas preferences update" in result.output
    assert cloud.requests == []


def test_still_pending_is_exit_75_and_says_how_to_pick_it_up_again():
    """Not an error, not done either: EX_TEMPFAIL, so a script can tell."""
    cloud = FakeCloud(quotas=[T4, A100])
    result = run(cloud, "quota", "request", "--gpu", "t4,a100", clock=Clock())

    assert result.exit_code == 75
    assert "still pending: t4, a100" in result.output
    assert "Approval can take days" in result.output
    assert "comfy-qat quota" in result.output


def test_only_the_cards_still_waiting_are_named():
    cloud = FakeCloud(quotas=[T4, A100])
    granted = {"done": False}
    real_grant = cloud._grant

    def grant_t4_only(args):
        if "--quota-id=NVIDIA-T4-GPUS-per-project-region" in args:
            real_grant(args)
            granted["done"] = True

    cloud.approve = True
    cloud._grant = grant_t4_only
    result = run(cloud, "quota", "request", "--gpu", "t4,a100", clock=Clock())

    assert granted["done"]
    assert result.exit_code == 75
    assert "granted: t4" in result.output
    assert "still pending: a100" in result.output
    assert "still pending: t4" not in result.output


def test_the_wait_is_one_window_for_the_command_not_one_per_card():
    """Waiting on each card in turn turned the documented half hour into an hour,
    and left the second card unpolled until the first gave up."""
    from comfy_qa.auth import WAIT_TIMEOUT_SECONDS

    cloud = FakeCloud(quotas=[T4, A100])
    clock = Clock()
    result = run(cloud, "quota", "request", "--gpu", "t4,a100", clock=clock)

    assert result.exit_code == 75
    assert clock.t == WAIT_TIMEOUT_SECONDS, "the window is shared, not multiplied"
    assert clock.slept, "it really did wait — on a clock that costs nothing"


def test_the_wait_window_is_half_an_hour_and_that_is_written_down_here():
    """The sibling above calls it "the documented half hour". Nothing documented it.

    Its assertion is `clock.t == WAIT_TIMEOUT_SECONDS` — the measured value
    checked against the constant that produced it, so both sides move together
    and the comparison holds for any value at all. Measured: `30 * 60` changed
    to `3 * 60` left the whole suite identical, pass for pass. And until this
    line, no literal `1800`, "30 minutes" or "half an hour" appeared anywhere in
    `docs/`, `comfy_qa/` or `tests/` either — so the window `quota request`
    waits before handing back exit 75 could be cut to three minutes or pushed to
    six hours and nothing would notice.

    So here is the oracle, independent of its subject the way "stopping after 6
    zones" pins `MAX_ATTEMPTS`. Half an hour is the trade: long enough that a
    grant landing while you wait is caught by the command that asked for it —
    approvals do arrive in minutes — and short enough that a terminal is not
    held all afternoon for a decision that can take days. Change the number and
    change this line, deliberately; that is the point of it.
    """
    from comfy_qa.auth import WAIT_TIMEOUT_SECONDS

    assert WAIT_TIMEOUT_SECONDS == 1800, (
        f"half an hour, in seconds — this is {WAIT_TIMEOUT_SECONDS / 60:g} minutes")


def test_request_with_no_project_says_which_command_sets_one():
    """The exception carries the fix and `request` was throwing it away, so the
    same failure that `quota list` explains left this command silent."""
    result = run(FakeCloud(project=None), "quota", "request", "--gpu", "l4")

    assert result.exit_code == 2
    assert "no project set" in result.stderr
    assert "to fix: comfy-qat setup" in result.stderr


def test_a_card_this_project_does_not_offer_lists_what_it_does():
    result = run(FakeCloud(quotas=[L4, A100, T4]), "quota", "request", "--gpu", "h100")

    assert result.exit_code == 2
    assert "this project reports no quota for 'h100'" in result.stderr
    assert "ask for one of: A100, L4, T4" in result.stderr


def test_a_card_offered_elsewhere_says_where_rather_than_contradicting_itself():
    """"no quota for 'l4' … Available: L4" reads as a contradiction.

    It became "it is metered in all regions, us-central1" — `readiness` yields
    LABELS, not places, so that is two API dimension entries rendered as prose
    and reading as a two-item region list. It now names real regions and offers
    `--by-region` for the rest, and the fix is about the REGION rather than
    handing back a list of cards containing the one just typed.
    """
    metered = quota("NVIDIA-L4-GPUS-per-project-region", 1, ["us-central1"])
    other = quota("NVIDIA-T4-GPUS-per-project-region", 1, ["europe-west4"])
    result = run(FakeCloud(quotas=[metered, other]), "quota", "request",
                 "--gpu", "l4", "--region", "europe-west4", "--no-wait")

    assert result.exit_code == 2
    assert "no l4 quota in europe-west4" in result.output, result.output
    assert "us-central1" in result.output, "it does not say where the card is"
    assert "ask for one of" not in result.output, (
        "the fix offers cards when the region is the problem")

def test_a_project_with_no_gpu_quota_at_all_offers_nothing():
    result = run(FakeCloud(quotas=[]), "quota", "request", "--gpu", "l4")

    assert result.exit_code == 2
    assert "no GPU quota at all" in result.stderr


def test_a_request_google_refuses_is_not_a_success():
    """Every card failed and nothing was submitted; exiting 0 told a script it
    had worked."""
    cloud = FakeCloud(quotas=[T4])
    real = cloud._run

    def refuse(args, mode):
        if " ".join(args).startswith("quotas preferences update"):
            raise GcloudError("this project has no billing history")
        return real(args, mode)

    cloud.gcloud = lambda: Gcloud(runner=refuse)
    result = run(cloud, "quota", "request", "--gpu", "t4")

    assert result.exit_code == 2
    assert "request for t4 failed" in result.stderr


def test_no_wait_hands_you_back_the_moment_it_is_submitted():
    cloud = FakeCloud(quotas=[T4])
    result = run(cloud, "quota", "request", "--gpu", "t4", "--no-wait")

    assert result.exit_code == 0, result.output
    assert "track them:" in result.output
    assert cloud.quota_reads == 1, "it did not poll after submitting"


def test_the_status_quota_line_is_one_row_per_card_and_admits_truncation():
    """Live testing on a real project showed `K80=1, K80=1, K80=1, K80=1`.

    Google meters some cards region by region, so the raw rows repeat one card —
    and the silent cut at four then hid the L4 that was the only card anybody
    wanted to use. A truncation nobody is told about is worse than a long line:
    it reads as the whole answer.
    """
    cloud = FakeCloud(quotas=[
        # Per-region metering, on a card that is actually usable. This case used
        # to lean on the K80 for that, and the K80 is now filtered out of the
        # count for having no GSP — so the repetition it was written to catch
        # would have been hidden by the filter rather than caught by the test.
        {
            "quotaId": "NVIDIA-L4-GPUS-per-project-region",
            "dimensionsInfos": [
                {"dimensions": {"region": name}, "details": {"value": "1"},
                 "applicableLocations": [name]}
                for name in REGIONS
            ],
        },
        quota("NVIDIA-T4-GPUS-per-project-region", 1, REGIONS),
        quota("NVIDIA-A100-GPUS-per-project-region", 1, REGIONS),
        quota("NVIDIA-A100-80GB-GPUS-per-project-region", 1, REGIONS),
        # The family shape, because that is the only one an H100 grant comes in.
        # There is no `NVIDIA-H100-GPUS-...` row on a real project — Google gives
        # the H100 no standard per-model quota — and a fixture that invents one
        # proves the tool can read a grant nobody has.
        {
            "quotaId": "GPUS-PER-GPU-FAMILY-per-project-region",
            "dimensionsInfos": [{
                "dimensions": {"gpu_family": "NVIDIA_H100"},
                "details": {"value": "8"},
                "applicableLocations": REGIONS,
            }],
        },
        # And one the tool cannot drive, to prove it is named rather than
        # silently dropped — and that it does not eat one of the four slots.
        K80_PER_REGION,
    ])
    result = run(cloud, "status")

    line = next(line for line in result.output.splitlines() if "gpu quota" in line)
    assert result.exit_code == 0
    assert line.count("L4") == 1, f"one row per card, not per region: {line}"
    assert "L4=1" in line, "the card you would actually use must survive the cut"
    assert "+1 more" in line and "5 cards ready" in line
    assert "K80 granted but not drivable" in line
