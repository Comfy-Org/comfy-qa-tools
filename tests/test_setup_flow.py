"""`comfy-qat setup`, driven end to end the way a new tester runs it.

The existing setup tests call `run_setup` directly. These go through the binary —
`typer.testing.CliRunner` against the real command, with gcloud replaced by a
scripted project whose state changes as the CLI acts on it. That is the only way
to test what the first run actually shows someone: the prompts, the exit code,
and the sign-off printed after `run_setup` returns, which lives in the CLI rather
than in setup.py and was wrong for two releases.

Nothing here touches the network, a real project, or the real host list.
"""

from __future__ import annotations

import tomllib

import pytest
from typer.testing import CliRunner

from comfy_qa.cli import app
from comfy_qa.gcloud import Gcloud, GcloudError

runner = CliRunner()


class FakeCloud:
    """A scripted Google Cloud project. Signing in and setting a project stick."""

    def __init__(
        self, *, account="ali@comfy.org", projects=("proj-1",), project="proj-1",
        billing=True, quotas=(), instances=(), expired=False, login=0,
        quota_error=None, instances_error=None,
    ):
        self.account = account
        self.projects = list(projects)
        self.project = project
        self.billing = billing
        self.quotas = list(quotas)
        self.instances = list(instances)
        self.expired = expired
        self.login = login
        self.quota_error = quota_error
        self.instances_error = instances_error
        self.calls: list[str] = []
        self.requests: list[list[str]] = []

    def gcloud(self) -> Gcloud:
        return Gcloud(runner=self._run)

    def _run(self, args, mode):
        key = " ".join(args)
        self.calls.append(key)

        if key.startswith("info --format=value(basic.python_location)"):
            # setup asks where gcloud's own python is, to put NumPy there. A path
            # that does not exist makes the step a no-op, which is what a flow
            # test wants — nothing should be installed by running a test.
            return "/no/such/python"

        if key == "auth login":
            assert mode == "interactive", "sign-in must attach the terminal"
            if self.login == 0:
                self.expired = False
                self.account = self.account or "ali@comfy.org"
            return self.login

        if self.expired:
            raise GcloudError("your gcloud session has expired", fix="gcloud auth login")

        if key.startswith("auth list"):
            return [{"account": self.account, "status": "ACTIVE"}] if self.account else []
        if key.startswith("projects list"):
            return [{"projectId": name} for name in self.projects]
        if key.startswith("config get-value project"):
            return self.project or "(unset)"
        if key.startswith("config set project"):
            self.project = args[-1]
            return ""
        if key.startswith("billing projects describe"):
            return {"billingEnabled": self.billing}
        if key.startswith("quotas info list"):
            if self.quota_error:
                raise self.quota_error
            return list(self.quotas)
        if key.startswith("quotas preferences create"):
            self.requests.append(list(args))
            return {}
        if key.startswith("compute instances list"):
            if self.instances_error:
                raise self.instances_error
            return list(self.instances)
        raise AssertionError(f"unexpected gcloud call: {key}")


def quota(quota_id, value, locations=("us-central1",)):
    return {
        "quotaId": quota_id,
        "dimensionsInfos": [{
            "details": {} if value is None else {"value": str(value)},
            "applicableLocations": list(locations),
        }],
    }


L4_READY = quota("NVIDIA-L4-GPUS-per-project-region", 1)
L4_ZERO = quota("NVIDIA-L4-GPUS-per-project-region", 0)

COMFY_WIN = {
    "name": "comfy-win",
    "zone": "projects/p/zones/us-central1-a",
    "status": "TERMINATED",
    "guestAccelerators": [{"acceleratorType": "zones/z/acceleratorTypes/nvidia-l4"}],
    "disks": [{"boot": True, "licenses": ["global/licenses/windows-server-2022-dc"]}],
}


@pytest.fixture
def hosts(tmp_path, monkeypatch):
    """Point the CLI's default host list somewhere disposable.

    `setup` takes no --config, so without this the first run of this suite would
    rewrite the tester's own host list.
    """
    path = tmp_path / "hosts.toml"
    monkeypatch.setattr("comfy_qa.setup.DEFAULT_CONFIG_PATH", path)
    return path


def run(cloud, *args, input=None):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("comfy_qa.cli.Gcloud", lambda *a, **k: cloud.gcloud())
        return runner.invoke(app, ["setup", *args], input=input)


# --- a machine with nothing on it -------------------------------------------


def test_a_completely_fresh_machine_is_walked_all_the_way_to_a_host_list(hosts):
    """Not signed in, one project, billing on, zero quota, no boxes."""
    cloud = FakeCloud(account=None, quotas=[L4_ZERO], instances=[])
    result = run(cloud, input="n\n")

    assert result.exit_code == 0, result.output
    assert "auth login" in cloud.calls, "an interactive first run signs you in"
    assert "signed in as ali@comfy.org" in result.output
    assert "project proj-1" in result.output
    assert "billing linked to proj-1" in result.output
    assert "GPU quota is zero on this project" in result.output
    assert hosts.exists(), "a host list is written even with nothing else ready"
    assert "ready — 1 machine, 0 in the cloud" in result.output


def test_a_fresh_machine_with_no_terminal_stops_at_sign_in_and_says_the_command(hosts):
    cloud = FakeCloud(account=None, quotas=[L4_ZERO])
    result = run(cloud, "--non-interactive")

    assert result.exit_code == 1
    assert "not signed in to Google Cloud" in result.stderr
    assert "to fix: gcloud auth login" in result.stderr
    assert "auth login" not in cloud.calls, "--non-interactive never opens a browser"
    assert not hosts.exists(), "it stopped before writing anything"


def test_an_expired_session_is_a_sign_in_not_a_traceback(hosts):
    cloud = FakeCloud(account="ali@comfy.org", expired=True, quotas=[L4_READY])
    result = run(cloud)

    assert result.exit_code == 0, result.output
    assert "auth login" in cloud.calls


def test_a_cancelled_sign_in_stops_with_something_to_run(hosts):
    cloud = FakeCloud(account=None, login=1)
    result = run(cloud)

    assert result.exit_code == 1
    assert "sign-in did not complete" in result.stderr
    assert "to fix: gcloud auth login" in result.stderr


def test_an_account_with_no_projects_stops_at_the_create_link(hosts):
    cloud = FakeCloud(projects=[], project=None)
    result = run(cloud)

    assert result.exit_code == 1
    assert "this account has no Google Cloud projects" in result.stderr
    assert "projectcreate" in result.stderr


# --- choosing a project ------------------------------------------------------


def test_one_project_is_taken_without_a_question(hosts):
    cloud = FakeCloud(projects=["only-one"], project=None, quotas=[L4_READY])
    result = run(cloud)

    assert result.exit_code == 0, result.output
    assert "config set project only-one" in cloud.calls
    assert "Which project?" not in result.output


def test_several_projects_are_offered_as_a_numbered_choice(hosts):
    cloud = FakeCloud(projects=["alpha", "beta", "gamma"], project=None, quotas=[L4_READY])
    result = run(cloud, input="2\n")

    assert result.exit_code == 0, result.output
    assert "Which project?" in result.output
    assert "1. alpha" in result.output and "2. beta" in result.output
    assert "config set project beta" in cloud.calls


def test_a_bad_number_asks_again_rather_than_guessing(hosts):
    cloud = FakeCloud(projects=["alpha", "beta"], project=None, quotas=[L4_READY])
    result = run(cloud, input="9\nnope\n1\n")

    assert result.exit_code == 0, result.output
    assert "pick a number between 1 and 2" in result.output
    assert "config set project alpha" in cloud.calls


def test_several_projects_and_no_terminal_will_not_pick_for_you(hosts):
    cloud = FakeCloud(projects=["alpha", "beta", "gamma"], project=None)
    result = run(cloud, "--non-interactive")

    assert result.exit_code == 1
    assert "no project set and 3 to choose from" in result.stderr
    assert "comfy-qat setup --project <id>" in result.stderr
    assert not any(c.startswith("config set project") for c in cloud.calls)


def test_the_project_flag_is_taken_without_looking_anything_up(hosts):
    cloud = FakeCloud(project="ignored", quotas=[L4_READY])
    result = run(cloud, "--project", "chosen-one", "--non-interactive")

    assert result.exit_code == 0, result.output
    assert "config set project chosen-one" in cloud.calls
    assert not any(c.startswith("config get-value project") for c in cloud.calls), (
        "the flag wins outright; whatever gcloud had configured is not consulted"
    )
    assert "project set to chosen-one" in result.output
    assert "Which project?" not in result.output


# --- billing -----------------------------------------------------------------


def test_billing_not_linked_stops_and_links_that_exact_project(hosts):
    """A generic billing link sends people to whichever project the console
    happens to remember, which is how the wrong project gets a card attached."""
    cloud = FakeCloud(projects=["proj-2"], project="proj-2", billing=False)
    result = run(cloud)

    assert result.exit_code == 1
    assert "no billing account is linked to proj-2" in result.stderr
    assert "linkedaccount?project=proj-2" in result.stderr
    # The host list IS written, and that is the fix rather than a regression.
    # It depends on the config path and nothing else — not the project, not
    # billing, not quota — and getting-started.md tells the reader "`setup` has
    # already written your host list", which was false for exactly the person
    # most likely to be reading it: the newcomer setup stopped. A run that ends
    # in a refusal should still leave behind the one thing it could always have
    # produced. Nothing project-scoped is written, which is what "stopped"
    # has to keep meaning.
    assert hosts.exists(), "the local starter host list survives a stop at billing"
    assert "quota" not in result.output.lower(), "nothing past billing ran"


def test_a_billing_read_that_fails_stops_with_its_own_fix(hosts):
    cloud = FakeCloud()
    cloud.billing = None

    def explode(args, mode):
        if " ".join(args).startswith("billing"):
            raise GcloudError("billing API not enabled", fix="enable it in the console")
        return FakeCloud._run(cloud, args, mode)

    cloud.gcloud = lambda: Gcloud(runner=explode)
    result = run(cloud)

    assert result.exit_code == 1
    assert "billing API not enabled" in result.stderr
    assert "to fix: enable it in the console" in result.stderr


# --- quota -------------------------------------------------------------------


def test_quota_that_cannot_be_read_is_reported_and_stepped_over(hosts):
    cloud = FakeCloud(
        quota_error=GcloudError("gcloud timed out after 240s: quotas info list"),
    )
    result = run(cloud)

    assert result.exit_code == 0, "quota can take days; it never strands anyone"
    assert "could not read GPU quota" in result.output
    assert "comfy-qat quota" in result.output
    assert hosts.exists()


def test_zero_quota_offers_the_request_and_sends_it_for_the_named_region(hosts):
    cloud = FakeCloud(quotas=[L4_ZERO])
    result = run(cloud, "--region", "us-central1", input="y\n")

    assert result.exit_code == 0, result.output
    assert len(cloud.requests) == 1
    args = cloud.requests[0]
    assert "--quota-id=NVIDIA-L4-GPUS-per-project-region" in args
    assert "--preferred-value=1" in args
    assert "--dimensions=region=us-central1" in args
    assert "Which region?" not in result.output, "--region was already given"


def test_zero_quota_asks_for_a_region_when_none_was_given(hosts):
    cloud = FakeCloud(quotas=[L4_ZERO])
    result = run(cloud, input="y\nus-west1\n")

    assert result.exit_code == 0, result.output
    assert "--dimensions=region=us-west1" in cloud.requests[0]


def test_declining_the_request_still_finishes_setup(hosts):
    cloud = FakeCloud(quotas=[L4_ZERO])
    result = run(cloud, input="n\n")

    assert result.exit_code == 0
    assert cloud.requests == []
    assert hosts.exists()


def test_a_refused_quota_request_is_reported_not_raised(hosts):
    cloud = FakeCloud(quotas=[L4_ZERO])
    real = cloud._run

    def refuse(args, mode):
        if " ".join(args).startswith("quotas preferences create"):
            raise GcloudError("this project has no billing history")
        return real(args, mode)

    cloud.gcloud = lambda: Gcloud(runner=refuse)
    result = run(cloud, "--region", "us-central1", input="y\n")

    assert result.exit_code == 0, "a refused request is not a failed setup"
    assert "the request was refused" in result.output
    assert hosts.exists()


def test_no_terminal_names_a_card_it_can_see_rather_than_a_placeholder_id(hosts):
    """`--quota-id <id>` left a blank only another command could fill."""
    cloud = FakeCloud(quotas=[L4_ZERO])
    result = run(cloud, "--non-interactive", "--region", "us-central1")

    assert result.exit_code == 0
    assert "comfy-qat quota request --gpu l4 --region us-central1" in result.output
    assert cloud.requests == [], "--non-interactive asks Google for nothing"


def test_a_region_with_no_quota_is_not_reported_as_the_whole_project(hosts):
    """Only one region was inspected, so only that region can be spoken for."""
    cloud = FakeCloud(quotas=[quota("NVIDIA-L4-GPUS-per-project-region", 1, ["us-central1"])])
    result = run(cloud, "--region", "europe-west4", "--non-interactive")

    assert result.exit_code == 0
    assert "GPU quota is zero in europe-west4" in result.output
    assert "zero on this project" not in result.output


def test_the_project_wide_allowance_is_requested_without_a_region(hosts):
    """It is a ceiling across every region, so a region dimension is invalid —
    and it was offered from setup's own menu."""
    cloud = FakeCloud(quotas=[quota("GPUS-ALL-REGIONS-per-project", 0, ["global"])])
    result = run(cloud, input="y\n")

    assert result.exit_code == 0, result.output
    assert len(cloud.requests) == 1
    assert not any(a.startswith("--dimensions") for a in cloud.requests[0])


# --- discovery and the sign-off ---------------------------------------------


def test_discovery_adds_the_box_and_the_sign_off_does_not_ask_for_it_by_hand(hosts):
    """The sign-off told people to add cloud boxes by hand seconds after
    discovery had added them."""
    cloud = FakeCloud(quotas=[L4_READY], instances=[COMFY_WIN])
    result = run(cloud)

    assert result.exit_code == 0, result.output
    assert "added comfy-win" in result.output
    assert "ready — 2 machines, 1 in the cloud" in result.output
    assert "by hand" not in result.output
    # The command, not the word: the temp path in this output contains the
    # test's own name, which includes "discovery".
    assert "comfy-qat discover" not in result.output


def test_with_no_cloud_boxes_the_sign_off_says_how_to_get_one(hosts):
    cloud = FakeCloud(quotas=[L4_READY], instances=[])
    result = run(cloud)

    assert "no cloud boxes on this project yet" in result.output
    assert "ready — 1 machine, 0 in the cloud" in result.output
    assert "no cloud boxes yet" in result.output
    # The end of the command whose job is getting a newcomer ready must point at
    # this tool, not at the Google Cloud console. It said "create one in Google
    # Cloud and run `comfy-qat discover`" — written before `create` existed.
    assert "comfy-qat create" in result.output
    assert "in Google Cloud" not in result.output
    assert "comfy-qat discover" in result.output


def test_discovery_failing_still_finishes_and_says_so(hosts):
    cloud = FakeCloud(quotas=[L4_READY],
                      instances_error=GcloudError("gcloud timed out after 60s"))
    result = run(cloud)

    assert result.exit_code == 0
    assert "could not list cloud boxes" in result.output
    assert "ready — 1 machine, 0 in the cloud" in result.output


def test_a_discovered_box_lands_on_a_port_that_is_not_the_local_comfyui(hosts):
    cloud = FakeCloud(quotas=[L4_READY], instances=[COMFY_WIN])
    run(cloud)

    text = hosts.read_text()
    assert "[hosts.comfy-win]" in text
    assert "port         = 8190" in text
    assert 'gce_zone     = "us-central1-a"' in text
    assert 'os           = "Windows Server 2022"' in text


# --- running it again --------------------------------------------------------


def test_a_second_run_changes_nothing_and_asks_nothing(hosts):
    """The documented promise: safe to run again, skips what is done."""
    cloud = FakeCloud(quotas=[L4_READY], instances=[COMFY_WIN])
    first = run(cloud)
    assert first.exit_code == 0, first.output
    before = hosts.read_text()

    again = run(cloud)

    assert again.exit_code == 0, again.output
    assert hosts.read_text() == before, "a re-run rewrote the host list"
    assert "1 cloud box(es), all already in your host list" in again.output
    assert "list at" in again.output, "the starter is not written twice"
    assert "Which project?" not in again.output


def test_a_hand_edited_host_list_is_appended_to_never_reordered(hosts):
    """Someone's own comments, names and ordering survive discovery."""
    mine = (
        "# my machines, in the order I think about them\n"
        "\n[hosts.laptop]\nkind = \"local\"\nport = 8188\n"
        "\n[hosts.the-windows-one]\n"
        'kind         = "gce"\n'
        'os           = "Windows Server 2022"\n'
        'gpu          = "L4"\n'
        'gce_instance = "comfy-win"\n'
        'gce_zone     = "us-central1-a"\n'
        'gce_project  = "proj-1"\n'
        "port         = 8191\n"
    )
    hosts.write_text(mine)
    linux = dict(COMFY_WIN, name="comfy-linux", status="RUNNING",
                 disks=[{"boot": True, "licenses": ["global/licenses/ubuntu-2204-lts"]}])
    cloud = FakeCloud(quotas=[L4_READY], instances=[COMFY_WIN, linux])

    result = run(cloud)
    text = hosts.read_text()

    assert result.exit_code == 0, result.output
    assert text.startswith(mine), "existing content was rewritten or reordered"
    assert "added comfy-linux" in result.output
    assert "comfy-win" not in result.output.split("added")[-1], (
        "a renamed host must match on its instance name, not its label"
    )
    tables = [k for k in tomllib.loads(text)["hosts"]]
    assert tables == ["laptop", "the-windows-one", "comfy-linux"]
    assert tomllib.loads(text)["hosts"]["comfy-linux"]["port"] == 8190, (
        "the new box takes the first free port, never the local ComfyUI's"
    )


def test_a_host_list_that_does_not_load_is_left_completely_alone(hosts):
    """Treating an unreadable list as an empty one appended a second
    [hosts.comfy-win] table — and a duplicate table is not valid TOML, so one
    fixable mistake became a file nothing can load."""
    broken = (
        "[hosts.local]\nkind = \"local\"\nport = 8188\n"
        "\n[hosts.comfy-win]\n"
        'kind         = "gce"\n'
        'os           = "Windows Server 2022"\n'
        'gpu          = "L4"\n'
        'gce_instance = "comfy-win"\n'
        'gce_zone     = "us-central1-a"\n'
        'gce_project  = "proj-1"\n'
        "port         = 8188\n"          # clashes with local: valid TOML, invalid list
    )
    hosts.write_text(broken)
    cloud = FakeCloud(quotas=[L4_READY], instances=[COMFY_WIN])

    result = run(cloud)

    assert hosts.read_text() == broken, "a list it cannot read is not a list it may edit"
    assert "could not read your host list" in result.output
    tomllib.loads(hosts.read_text())  # still parses; no duplicate table was added
    assert result.exit_code == 0


def test_setup_does_not_write_over_a_starter_host_list_it_already_wrote(hosts):
    hosts.write_text("# mine\n")
    cloud = FakeCloud(quotas=[L4_READY], instances=[])
    run(cloud)
    assert hosts.read_text() == "# mine\n"


def test_missing_gcloud_stops_before_it_asks_for_anything(hosts, monkeypatch):
    from comfy_qa.gcloud import Gcloud as RealGcloud

    empty = RealGcloud()
    monkeypatch.setattr(empty, "available", lambda: None)
    monkeypatch.setattr("comfy_qa.cli.Gcloud", lambda *a, **k: empty)
    result = runner.invoke(app, ["setup"])

    assert result.exit_code == 1
    assert "gcloud is not installed or not on PATH" in result.stderr
    assert "sdk/docs/install" in result.stderr


def test_the_project_line_says_whose_choice_it_was(hosts):
    """`setup` adopts gcloud's current project without asking, which is right —
    but it used to announce it as a bare noun, `project proj-1`, indistinguishable
    from a report.

    Everything after that line happens on that project: the billing check, the
    GPU quota request, every box. Somebody who has spent the week in another
    project gets quota requested somewhere they did not intend, and the only
    clue was a word. The adoption is fine; the silence was not.
    """
    cloud = FakeCloud(projects=["proj-1"], project="proj-1")
    result = run(cloud)

    assert "gcloud's current project" in result.output, (
        "the line must say whose choice this was, not just name it"
    )
    assert "comfy-qat setup --project" in result.output, (
        "and how to choose a different one"
    )
