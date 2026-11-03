"""comfy-qat keeps its own Google project, in the host list.

Until now the project was gcloud's: `setup --project X` ran `gcloud config set
project X`, and every command that needed a project asked `gcloud config
get-value project`. So comfy-qat and every other gcloud user on the machine
shared one setting, and moving comfy-qat to a new project — the thing Ali asked
for on 3 November, "easily switch to a new google project once we have access"
— moved everything else on the machine with it, or was undone by the next
`gcloud config set` made for some other reason.

Now the host list says it: one `project = "<id>"` line at the top, which
`setup --project` writes and nothing else reads but comfy-qat. What each part
here holds, against the criteria frozen for 1.3.1 (S1-11):

  a. `setup --project <id>` saves it in the host list and never runs
     `gcloud config set project`;
  b. every gcloud call that acts on a project carries `--project`, so the
     choice reaches Google whatever gcloud is set to;
  c. with no project in the host list, gcloud's current one is used, as
     before, and the command says so;
  d. a box keeps the `gce_project` its entry names, and `list` shows it.

Every gcloud here is the real `Gcloud` with its runner replaced, so what is
asserted is the argv that would have been sent.
"""

from __future__ import annotations

import ast
import inspect
import re
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from comfy_qa import gcloud as gcloud_module
from comfy_qa.cli import app
from comfy_qa.config import ConfigError, load, parse
from comfy_qa.gcloud import Gcloud
from test_setup_flow import FakeCloud, L4_READY
from test_setup_flow import run as run_setup

PACKAGE = Path(__file__).resolve().parent.parent / "comfy_qa"


def declared_project(path):
    """`config.declared_project`, looked up when called, so a build without it
    fails the tests that need it rather than the whole file at collection."""
    from comfy_qa.config import declared_project as read

    return read(path)

CHOSEN = "qatest-chosen-1"       # what the host list says
GCLOUDS = "gclouds-own-1"        # what `gcloud config get-value project` says
OLD = "qatest-old-project"      # where an existing box already is

URL = "https://www.googleapis.com/compute/v1/projects"


def host_list(project: str | None = None, *, box_in: str | None = None) -> str:
    """A host list, with the project line when given and one cloud box when
    `box_in` names its project."""
    text = f'project = "{project}"\n\n' if project else ""
    text += '[hosts.local]\nkind = "local"\nport = 8188\n'
    if box_in:
        text += f"""
[hosts.old-box]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "old-box"
gce_zone     = "us-central1-a"
gce_project  = "{box_in}"
port         = 8190
"""
    return text


# --- a recording gcloud --------------------------------------------------------


# Calls that are about this machine or the account, not about a project.
LOCAL = (("auth",), ("config", "get-value"), ("info",), ("projects", "list"))


def _local(argv: list[str]) -> bool:
    return any(tuple(argv[:len(prefix)]) == prefix for prefix in LOCAL)


class Recorder:
    """The answers a project gives, keyed on the start of the argv.

    Anything not answered here is refused rather than answered with None: a
    command reaching for a call nobody expected is something to see.
    """

    def __init__(self, *, current: str | None = GCLOUDS, instances=()):
        self.current = current
        self.instances = list(instances)
        self.stopped: set[str] = set()
        self.calls: list[list[str]] = []

    def gcloud(self) -> Gcloud:
        return Gcloud(runner=self._run)

    def _run(self, args, mode):
        argv = [str(part) for part in args]
        self.calls.append(argv)
        key = " ".join(argv)
        project = next((part.split("=", 1)[1] for part in argv
                        if part.startswith("--project=")), "")
        if key.startswith("auth list"):
            return [{"account": "qa@example.com", "status": "ACTIVE"}]
        if key.startswith("config get-value project"):
            return self.current or "(unset)"
        if key.startswith("info"):
            return "/no/such/python"
        if key.startswith("billing projects describe"):
            return {"billingEnabled": True}
        if key.startswith("quotas info list"):
            return [L4_READY]
        if key.startswith("quotas preferences list"):
            return []
        if key.startswith("compute instances list"):
            return [dict(row, zone=f"{URL}/{project}/zones/us-central1-a")
                    for row in self.instances]
        if key.startswith("compute reservations list"):
            return []
        if key.startswith("compute accelerator-types list"):
            return [{"name": "nvidia-l4", "zone": f"{URL}/{project}/zones/us-central1-a"}]
        if key.startswith("compute machine-types list"):
            return [{"name": "g2-standard-8",
                     "zone": f"{URL}/{project}/zones/us-central1-a"}]
        if key.startswith("compute instances describe"):
            return {"name": argv[3],
                    "status": "TERMINATED" if argv[3] in self.stopped else "RUNNING"}
        if key.startswith("compute instances stop"):
            self.stopped.add(argv[3])
            return ""
        raise AssertionError(f"the recorder was not told to expect: {key}")

    def scoped(self) -> list[list[str]]:
        """Every call that acts on a project."""
        return [argv for argv in self.calls if not _local(argv)]


@pytest.fixture
def qat(tmp_path, monkeypatch):
    """`comfy-qat <argv>` against one host list and one recording gcloud."""
    from comfy_qa import zones

    path = tmp_path / "hosts.toml"
    monkeypatch.setattr(zones, "_connect", lambda region, timeout=None: 10.0)
    monkeypatch.setattr(gcloud_module, "can_prompt", lambda: False)
    # No gcloud on PATH, as far as anything here can tell. A module that holds
    # its own `Gcloud` name and was missed below then fails at once with "not
    # installed" instead of asking the real project — which is what the first
    # draft of this file did, through `auth`'s name, read-only, until stopped.
    monkeypatch.setattr(gcloud_module.shutil, "which", lambda name: None)

    def invoke(*argv, cloud: Recorder, declared: str):
        if not path.exists():
            path.write_text(declared, encoding="utf-8")
        for module in ("comfy_qa.gcloud", "comfy_qa.auth", "comfy_qa.cli"):
            monkeypatch.setattr(f"{module}.Gcloud", lambda *a, **k: cloud.gcloud())
        result = CliRunner().invoke(app, ["--config", str(path), *argv])
        result.hosts = path.read_text(encoding="utf-8")  # type: ignore[attr-defined]
        return result

    return invoke


# --- a: setup saves it, and leaves gcloud's own setting alone ------------------


def test_setup_with_a_project_saves_it_in_the_host_list_and_not_in_gcloud(tmp_path,
                                                                         monkeypatch):
    path = tmp_path / "hosts.toml"
    monkeypatch.setattr("comfy_qa.setup.DEFAULT_CONFIG_PATH", path)
    cloud = FakeCloud(project=GCLOUDS, quotas=[L4_READY])
    result = run_setup(cloud, "--project", CHOSEN, "--non-interactive",
                       "--no-quota-request")

    assert result.exit_code == 0, result.output
    assert not [call for call in cloud.calls if call.startswith("config set")], (
        "setup changed gcloud's own project")
    assert cloud.project == GCLOUDS
    assert declared_project(path) == CHOSEN
    assert f'project = "{CHOSEN}"' in path.read_text(encoding="utf-8")
    # And setup itself worked on it.
    assert f"billing projects describe {CHOSEN}" in " | ".join(cloud.calls)


def test_setup_with_a_new_project_replaces_the_old_line_and_keeps_the_boxes(
        tmp_path, monkeypatch):
    """Switching is the same command again. The boxes already listed stay, each
    with the project it names."""
    path = tmp_path / "hosts.toml"
    path.write_text(host_list(OLD, box_in=OLD), encoding="utf-8")
    monkeypatch.setattr("comfy_qa.setup.DEFAULT_CONFIG_PATH", path)
    cloud = FakeCloud(project=GCLOUDS, quotas=[L4_READY])
    result = run_setup(cloud, "--project", CHOSEN, "--non-interactive",
                       "--no-quota-request")

    assert result.exit_code == 0, result.output
    text = path.read_text(encoding="utf-8")
    assert text.count("project = ") == 1
    assert declared_project(path) == CHOSEN
    (box,) = [host for host in load(path) if host.name == "old-box"]
    assert box.gce_project == OLD


def test_setup_help_says_project_replaces_the_line_in_an_existing_list():
    """What the test above drives, said where it is read first. `setup --help`
    said an existing host list is "read, never replaced" and only "appended to
    by the discovery pass", and `--project` said "use this Google Cloud project
    instead of asking" — so nothing a person reads before running it said that
    `--project` writes the list's `project` line, over any already there."""
    import typer

    command = typer.main.get_command(app).commands["setup"]
    said = " ".join((command.help or "").split())
    (option,) = [param for param in command.params if param.name == "project"]
    flag = " ".join((option.help or "").split())

    assert "replacing any project already there" in said, said
    assert "never replaced" not in said, "the line is replaced"
    assert "host list's project line" in flag and "replacing" in flag, flag


def test_nothing_in_the_package_can_set_gclouds_project():
    """The method is gone and no argv anywhere spells the call, so nothing can
    bring it back by a second route."""
    assert not hasattr(Gcloud, "set_project")
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.List):
                words = [element.value for element in node.elts
                         if isinstance(element, ast.Constant)]
                assert words[:2] != ["config", "set"], f"{path.name}:{node.lineno}"


# --- b: every call that acts on a project names it -----------------------------


def _project_methods():
    """Every `Gcloud` method that takes a project, read off the class."""
    found = []
    for name, method in inspect.getmembers(Gcloud, inspect.isfunction):
        if name.startswith("_") or name in NOT_A_PROJECT_CALL:
            continue
        if "project" in inspect.signature(method).parameters:
            found.append(name)
    return found


# What each method needs besides `project`, to be called at all.
ARGUMENTS = {
    "instance": "box", "name": "box", "zone": "us-central1-a", "remote": "true",
    "disk": "box", "snapshot": "box-snap", "region": "us-central1",
    "zones": ["us-central1-a"], "local_port": 33389, "network": "default",
    "machine_type": "n1-standard-8", "image_family": "ubuntu-2204-lts",
    "image_project": "ubuntu-os-cloud", "disk_gb": 200, "metadata": None,
    "accelerator": None, "reservation": None, "description": "",
    "source_ranges": "35.235.240.0/20", "ports": "8188", "rules": "tcp:8188",
}

# Not a call to Google about a project: the credential gate, which asks who is
# signed in before anything else. The project it proves is sent by `_prove`, an
# argv literal the walk below this one holds to the same rule.
NOT_A_PROJECT_CALL = {"preflight"}


@pytest.mark.parametrize("method", _project_methods())
def test_every_gcloud_method_that_takes_a_project_sends_it(method):
    """Derived from the class, so a new method is in scope the day it exists.
    Each is called with the project and must send `--project=` with exactly that
    value on every call it makes — or, for the two that build an argv for
    `execvp`, carry it in the argv it returns."""
    sent: list[list[str]] = []

    def runner(args, mode):
        sent.append([str(part) for part in args])
        if args[:3] == ["compute", "reset-windows-password", "box"]:
            return {"username": "u", "password": "p"}
        return [] if mode is True else ""

    gc = Gcloud(runner=runner)
    bound = getattr(gc, method)
    kwargs = {}
    for name, parameter in inspect.signature(bound).parameters.items():
        if name == "project":
            kwargs[name] = CHOSEN
        elif parameter.default is inspect.Parameter.empty:
            kwargs[name] = ARGUMENTS[name]
    returned = bound(**kwargs)
    if method.endswith("_argv"):
        sent.append([str(part) for part in returned])

    assert sent, f"{method} sent nothing, so there is nothing to check"
    for argv in sent:
        assert f"--project={CHOSEN}" in argv, f"{method}: {' '.join(argv)}"


def test_the_method_list_is_the_whole_class():
    """Non-vacuity: the walk above found the methods that matter."""
    found = set(_project_methods())
    assert {"list_instances", "create_instance_from_image", "stop_instance",
            "delete_reservation", "compute_quotas", "billing_enabled",
            "ssh", "ssh_argv", "rdp_argv"} <= found, sorted(found)


_SCOPED_FIRST_WORDS = ("compute", "quotas", "billing")


def test_every_gcloud_argv_written_outside_the_class_names_its_project():
    """`gc.run([...])` is reachable from any module, and five of them build an
    argv of their own. An argv literal that starts the way a project-scoped
    gcloud call does — `compute`, `quotas`, `billing`, `projects describe` —
    must carry `--project` in the same literal."""
    found = 0
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.List) or not node.elts:
                continue
            first = node.elts[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue
            words = [element.value for element in node.elts[:2]
                     if isinstance(element, ast.Constant)]
            if not (first.value in _SCOPED_FIRST_WORDS
                    or words == ["projects", "describe"]):
                continue
            found += 1
            text = ast.unparse(node)
            assert "--project=" in text, f"{path.name}:{node.lineno} {text}"
    assert found >= 30, f"only {found} argv literals found — the walk is broken"


@pytest.mark.parametrize("argv", [
    ["discover", "--dry-run"],
    ["list", "--live"],
    ["create", "--os", "linux", "--gpu", "l4", "--no-reserve", "--dry-run"],
    ["down", "old-box"],
    ["quota", "list"],
    ["status"],
], ids=lambda argv: " ".join(argv[:2]))
def test_the_main_commands_act_on_the_host_lists_project(qat, argv):
    """With `project = "<id>"` in the host list and gcloud pointed somewhere
    else, every call that acts on a project names the host list's — or, for a
    box already in the list, that box's own — and gcloud's setting is never
    read at all."""
    cloud = Recorder(current=GCLOUDS)
    result = qat(*argv, cloud=cloud, declared=host_list(CHOSEN, box_in=OLD))

    assert result.exit_code in (0, 1), result.output
    assert cloud.scoped(), f"{argv[0]} acted on no project, so nothing was checked"
    for call in cloud.scoped():
        projects = [part for part in call if part.startswith("--project=")]
        assert projects in ([f"--project={CHOSEN}"], [f"--project={OLD}"]), (
            f"{' '.join(argv)} sent: {' '.join(call)}")
    assert ["config", "get-value", "project"] not in cloud.calls, (
        "gcloud's own project was consulted although the host list names one")
    assert GCLOUDS not in result.output


def test_discover_and_create_use_the_host_lists_project_for_what_is_new(qat):
    """A new box goes where the host list says, whatever gcloud is set to."""
    box = {"name": "found-box", "status": "TERMINATED",
           "disks": [{"boot": True, "licenses": ["x/licenses/ubuntu-2204-lts"]}]}
    cloud = Recorder(current=GCLOUDS, instances=[box])
    result = qat("discover", cloud=cloud, declared=host_list(CHOSEN))

    assert result.exit_code == 0, result.output
    assert f'gce_project  = "{CHOSEN}"' in result.hosts
    assert GCLOUDS not in result.hosts


# --- c: none in the host list is gcloud's, said out loud ------------------------


@pytest.mark.parametrize("argv", [
    ["discover", "--dry-run"],
    ["create", "--os", "linux", "--gpu", "l4", "--no-reserve", "--dry-run"],
    ["quota", "list"],
], ids=lambda argv: argv[0])
def test_with_no_project_in_the_host_list_gclouds_is_used_and_said(qat, argv):
    cloud = Recorder(current=GCLOUDS)
    result = qat(*argv, cloud=cloud, declared=host_list())

    assert result.exit_code == 0, result.output
    assert ["config", "get-value", "project"] in cloud.calls
    for call in cloud.scoped():
        assert f"--project={GCLOUDS}" in call, " ".join(call)
    said = " ".join(result.stderr.split())
    assert (f"project {GCLOUDS} — gcloud's current project, because your host "
            f"list names none") in said
    assert "comfy-qat setup --project <id>" in said


def test_status_says_the_project_is_the_host_lists(qat):
    cloud = Recorder(current=GCLOUDS)
    result = qat("status", cloud=cloud, declared=host_list(CHOSEN))
    assert f"{CHOSEN} (from your host list)" in result.output


def test_status_with_none_in_the_host_list_says_it_is_gclouds(qat):
    cloud = Recorder(current=GCLOUDS)
    result = qat("status", cloud=cloud, declared=host_list())

    assert f"{GCLOUDS} (gcloud's current project — your host list names none)" in (
        " ".join(result.output.split()))


def test_no_project_anywhere_is_refused_with_the_command_that_sets_one(qat):
    cloud = Recorder(current=None)
    result = qat("discover", cloud=cloud, declared=host_list())

    assert result.exit_code == 2, result.output
    assert "no project set" in result.output
    assert "comfy-qat setup --project <id>" in result.output


# --- d: a box keeps its own project, and list shows it ---------------------------


def test_a_box_is_stopped_in_its_own_project_after_the_host_list_moves_on(qat):
    cloud = Recorder(current=GCLOUDS)
    result = qat("down", "old-box", cloud=cloud, declared=host_list(CHOSEN, box_in=OLD))

    assert result.exit_code == 0, result.output
    stops = [call for call in cloud.calls if call[:3] == ["compute", "instances", "stop"]]
    assert stops and all(f"--project={OLD}" in call for call in stops)


def test_list_shows_each_boxs_project(qat):
    """Under the table, one line per project — a column of the same twenty-odd
    characters pushed the --live table past the width it is held to — and
    where a new box will go, which after a switch is not where the old ones
    are."""
    cloud = Recorder(current=GCLOUDS)
    result = qat("list", cloud=cloud, declared=host_list(CHOSEN, box_in=OLD))

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert f"on {OLD}: old-box" in lines
    assert f"new boxes go to {CHOSEN}, the project your host list names" in lines
    assert cloud.calls == [], "plain list asks Google nothing"


def test_list_says_nothing_about_projects_with_no_cloud_box_and_no_line(qat):
    result = qat("list", cloud=Recorder(), declared=host_list())
    assert result.exit_code == 0, result.output
    assert "on " not in result.stdout.split("STATE is only")[0].split("RESERVED", 1)[1]


# --- the line itself ------------------------------------------------------------


def test_the_project_line_is_read_and_checked():
    assert declared_project(None) is None or isinstance(declared_project(None), str)
    parse({"project": CHOSEN, "hosts": {"local": {"kind": "local", "port": 8188}}})
    for bad in ("", "Has Capitals", "has space", "1starts-with-digit", 42, "ends-"):
        with pytest.raises(ConfigError) as caught:
            parse({"project": bad, "hosts": {"local": {"kind": "local", "port": 8188}}})
        assert "project" in str(caught.value)


def test_a_misspelt_project_line_is_refused_rather_than_ignored():
    """Ignored, a typo would send every billable call to gcloud's project
    instead, which may be somebody else's."""
    with pytest.raises(ConfigError) as caught:
        parse({"projcet": CHOSEN, "hosts": {"local": {"kind": "local", "port": 8188}}})
    assert "projcet" in str(caught.value)
    assert re.search(r"\bproject\b", str(caught.value))


def test_a_legacy_domain_scoped_project_id_is_accepted():
    """`example.com:my-project` is a real, older shape of project id."""
    parse({"project": "example.com:qa-project",
           "hosts": {"local": {"kind": "local", "port": 8188}}})


# --- writing the line ---------------------------------------------------------


def test_the_first_project_line_goes_above_the_first_table_with_a_note():
    from comfy_qa.hostfile import with_project

    text = "# my machines\n\n[hosts.local]\nkind = \"local\"\nport = 8188\n"
    written = with_project(text, CHOSEN)

    assert written.startswith("# my machines\n\n# The Google project comfy-qat works in")
    assert written.index(f'project = "{CHOSEN}"') < written.index("[hosts.local]")
    assert parse(tomllib.loads(written))


def test_a_second_project_replaces_the_first_where_it_is_and_keeps_its_comment():
    from comfy_qa.hostfile import with_project

    text = (f'project = "{OLD}"   # the old one\n\n'
            '[hosts.local]\nkind = "local"\nport = 8188\n')
    written = with_project(text, CHOSEN)

    assert written == text.replace(OLD, CHOSEN)


def test_a_crlf_host_list_stays_crlf():
    from comfy_qa.hostfile import with_project

    text = '[hosts.local]\r\nkind = "local"\r\nport = 8188\r\n'
    written = with_project(text, CHOSEN)
    assert "\n" not in written.replace("\r\n", "")


def test_a_host_field_called_project_is_not_mistaken_for_the_line(tmp_path):
    """Below a table header the key belongs to that host, and the loader
    refuses it there — so the rewrite must not edit it as if it were the line."""
    from comfy_qa.hostfile import with_project

    text = '[hosts.local]\nkind = "local"\nport = 8188\nproject = "nope"\n'
    written = with_project(text, CHOSEN)
    assert written.index(f'project = "{CHOSEN}"') < written.index("[hosts.local]")
    assert 'project = "nope"' in written
