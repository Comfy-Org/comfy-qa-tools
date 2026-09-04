"""Getting onto a box: `ssh` and `rdp`.

Both end in `os.execvp`, which replaces this process. Nothing after that line
runs, so nothing after it can be asserted on a real invocation — and the argv is
the whole of what the user gets. It is therefore built by `Gcloud` as a pure
value and checked here against a stand-in for `execvp`, which records the call
and raises rather than replacing the test runner.

The refusals matter as much as the argv. `ssh local` and `rdp <linux box>` are
the two things a tired person types by mistake, and both must exit 2 — nothing
was changed — with the other command named as the fix.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from comfy_qa import gcloud as gcloud_module
from comfy_qa import host as host_module
from comfy_qa.cli import app

HOSTS = """\
[hosts.local]
kind = "local"
port = 8188

[hosts.comfy-win]
kind         = "gce"
os           = "Windows Server 2022"
gpu          = "T4"
gce_instance = "win-instance"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8190

[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "linux-instance"
gce_zone     = "us-central1-b"
gce_project  = "proj"
port         = 8191
"""


class Replaced(Exception):
    """`execvp` never returns. This is how a test sees that it was reached."""


@pytest.fixture
def execvp(monkeypatch):
    """Record what would have replaced this process, instead of doing it."""
    calls: list[list[str]] = []

    def stand_in(file, argv):
        calls.append([file, *list(argv)])
        raise Replaced(file)

    monkeypatch.setattr("os.execvp", stand_in)
    return calls


@pytest.fixture
def hosts(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    return str(path)


def run(*args):
    return CliRunner().invoke(app, list(args))


# ---------------------------------------------------------------- refusals


def test_ssh_refuses_this_machine(hosts):
    result = run("ssh", "local", "--config", hosts)

    assert result.exit_code == 2, "nothing was changed, so it is a 2"
    assert "local is this machine — open a terminal" in result.output


def test_ssh_refuses_a_windows_box_and_names_rdp(hosts):
    result = run("ssh", "comfy-win", "--config", hosts)

    assert result.exit_code == 2
    assert "comfy-win runs Windows, which has no ssh here" in result.output
    assert "comfy-qat rdp comfy-win" in result.output


def test_rdp_refuses_a_linux_box_and_names_ssh(hosts):
    result = run("rdp", "comfy-linux", "--config", hosts)

    assert result.exit_code == 2
    assert "comfy-linux is not a Windows cloud box" in result.output
    assert "comfy-qat ssh comfy-linux" in result.output


def test_rdp_refuses_this_machine(hosts):
    result = run("rdp", "local", "--config", hosts)

    assert result.exit_code == 2
    assert "local is not a Windows cloud box" in result.output


@pytest.mark.parametrize("command", ["ssh", "rdp"])
def test_no_machine_at_all_is_refused(command, hosts):
    result = run(command, "--config", hosts)

    assert result.exit_code == 2
    assert "which machine?" in result.output


@pytest.mark.parametrize("command", ["ssh", "rdp"])
def test_a_name_and_a_description_together_are_refused(command, hosts):
    """`ssh comfy-linux --os windows` is a mistake, not a precedence question."""
    result = run(command, "comfy-linux", "--os", "windows", "--config", hosts)

    assert result.exit_code == 2
    assert "say the machine once" in result.output


@pytest.mark.parametrize("command", ["ssh", "rdp"])
def test_an_unknown_name_is_refused(command, hosts):
    result = run(command, "nosuchbox", "--config", hosts)

    assert result.exit_code == 2
    assert "unknown host 'nosuchbox'" in result.output


# ------------------------------------------------------------ what it execs


def test_ssh_execs_the_iap_command_for_that_box(hosts, execvp):
    result = run("ssh", "comfy-linux", "--config", hosts)

    assert isinstance(result.exception, Replaced)
    assert execvp == [[
        "gcloud", "gcloud", "compute", "ssh", "linux-instance",
        "--zone=us-central1-b", "--project=proj", "--tunnel-through-iap",
    ]]


def test_ssh_finds_the_box_by_description(hosts, execvp):
    """`--os` and `--gpu` reach the same machine as its name does."""
    run("ssh", "--gpu", "l4", "--config", hosts)

    assert execvp[0][4] == "linux-instance"


def test_rdp_forwards_the_desktop_port_for_that_box(hosts, execvp, monkeypatch):
    monkeypatch.setattr(
        gcloud_module.Gcloud, "windows_password",
        lambda self, instance, zone, project: {
            "username": "ali", "password": "hunter2"},
    )

    result = run("rdp", "comfy-win", "--config", hosts)

    assert isinstance(result.exception, Replaced)
    assert execvp == [[
        "gcloud", "gcloud", "compute", "start-iap-tunnel", "win-instance",
        "3389", f"--local-host-port=localhost:{host_module.RDP_PORT}",
        "--zone=us-central1-a", "--project=proj",
    ]]


def test_rdp_hands_over_the_credentials_before_it_forwards(hosts, execvp,
                                                           monkeypatch):
    """The password is the one thing only a person can use. It goes first."""
    monkeypatch.setattr(
        gcloud_module.Gcloud, "windows_password",
        lambda self, instance, zone, project: {
            "username": "ali", "password": "hunter2"},
    )

    result = run("rdp", "--os", "windows", "--config", hosts)

    assert "user     ali" in result.output
    assert "password hunter2" in result.output
    assert f"address  localhost:{host_module.RDP_PORT}" in result.output


# ------------------------------------------------------- gcloud is not there


@pytest.mark.parametrize("command,name", [("ssh", "comfy-linux"),
                                          ("rdp", "comfy-win")])
def test_without_gcloud_it_says_so_rather_than_raising(command, name, hosts,
                                                       execvp, monkeypatch):
    """`execvp` on a binary that is not on PATH is a `FileNotFoundError`.

    Every other command in this tool answers a missing gcloud with a sentence
    and an install link. `ssh` handed the same person a traceback, because the
    argv is built without ever asking whether the thing at the front of it
    exists. The check `run()` already makes is the check these two owe.
    """
    monkeypatch.setattr(gcloud_module.Gcloud, "available", lambda self: None)

    result = CliRunner().invoke(app, [command, name, "--config", hosts])

    assert execvp == [], "nothing should be exec'd when there is nothing to exec"
    assert result.exit_code == 2, "nothing was started, so it is a 2"
    assert "gcloud is not installed or not on PATH" in result.output
    assert "https://cloud.google.com/sdk/docs/install" in result.output
