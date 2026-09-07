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


@pytest.fixture
def state(monkeypatch):
    """What Google says the box is doing, without asking Google.

    `ssh` reads the instance's state before it hands the terminal over, so every
    test that expects to reach `execvp` has to answer that question — and none
    of them may answer it by making a real call. The default is RUNNING, which
    is the case the argv tests are about; `state.answer` sets up the others.
    """
    class Answer:
        value = "RUNNING"

    answer = Answer()
    monkeypatch.setattr(
        gcloud_module.Gcloud, "instance_status",
        lambda self, instance, zone, project: answer.value,
    )
    return answer


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


def test_ssh_execs_the_iap_command_for_that_box(hosts, execvp, state):
    result = run("ssh", "comfy-linux", "--config", hosts)

    assert isinstance(result.exception, Replaced)
    assert execvp == [[
        "gcloud", "gcloud", "compute", "ssh", "linux-instance",
        "--zone=us-central1-b", "--project=proj", "--tunnel-through-iap",
    ]]


def test_ssh_finds_the_box_by_description(hosts, execvp, state):
    """`--os` and `--gpu` reach the same machine as its name does."""
    run("ssh", "--gpu", "l4", "--config", hosts)

    assert execvp[0][4] == "linux-instance"


# ------------------------------------- a box that cannot take a shell yet


@pytest.mark.parametrize("stopped", ["TERMINATED", "STOPPING", "SUSPENDED",
                                     "PROVISIONING", "STAGING"])
def test_ssh_on_a_box_that_is_not_running_is_one_sentence(hosts, execvp, state,
                                                          stopped):
    """The everyday case: you forgot to `up`.

    gcloud's own answer to this is a 36-line Python traceback and exit 255,
    ending in a suggested `--troubleshoot` that fails identically. `logs` on the
    same box in the same second says one sentence and names the fix. Nothing was
    reached, so it is a 2 — and `execvp` must not have run at all.
    """
    state.value = stopped

    result = run("ssh", "comfy-linux", "--config", hosts)

    assert result.exit_code == 2
    assert "comfy-linux is not running, so there is nothing to open a shell " \
           "on" in result.output
    assert "comfy-qat up comfy-linux" in result.output
    assert execvp == [], "nothing may be handed to execvp on a box that is off"


def test_ssh_does_not_guess_when_the_state_is_unreadable(hosts, execvp, state):
    """An empty state is a third answer, not a quiet yes.

    `instance_status` can succeed and say nothing. Treating that as RUNNING
    hands the terminal to gcloud and gets the traceback back; treating it as
    stopped tells someone to start a box that may already be billing.
    """
    state.value = ""

    result = run("ssh", "comfy-linux", "--config", hosts)

    assert result.exit_code == 2
    assert "could not tell whether comfy-linux is running" in result.output
    assert "comfy-qat list --live" in result.output
    assert execvp == []


def test_rdp_forwards_the_desktop_port_for_that_box(hosts, execvp, monkeypatch):
    monkeypatch.setattr(
        gcloud_module.Gcloud, "windows_password",
        lambda self, instance, zone, project: {
            "username": "ali", "password": "hunter2"},
    )

    result = run("rdp", "comfy-win", "--config", hosts)

    assert isinstance(result.exception, Replaced)
    # 33389 as a literal, not `host_module.RDP_PORT`. Built from the constant,
    # both sides of this move together and there is no oracle: changing
    # RDP_PORT to 9999 left the whole suite green, measured. It is the port a
    # person types into an RDP client, on a machine whose own notes already
    # record what port confusion has cost here — 8188 against 8189 against 8190
    # — so a silent change to it does not fail a test, it sends someone to the
    # wrong machine.
    #
    # The general rule, and this file has two of the four known exceptions:
    # a test needs an oracle independent of its subject. `subject(...) ==
    # <literal>` is fine, because the literal IS the oracle. `<anything> ==
    # SUBJECT_CONSTANT` asserts a thing against itself.
    assert execvp == [[
        "gcloud", "gcloud", "compute", "start-iap-tunnel", "win-instance",
        "3389", "--local-host-port=localhost:33389",
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
    # The literal again, and this is the line that matters most: this is the
    # address a person reads off the screen and types into Remote Desktop.
    assert "address  localhost:33389" in result.output


@pytest.mark.parametrize("answered,missing", [
    ({}, "username"),                                  # exit 0, empty stdout
    (None, "username"),                                # `run` parsed nothing
    ({"username": "ali"}, "password"),                 # half a pair
    ({"password": "hunter2"}, "username"),
    ({"username": "ali", "password": ""}, "password"),  # present and empty
    ({"user": "ali", "pass": "hunter2"}, "username"),   # renamed on their side
    ([{"username": "ali"}], "username"),               # not even a mapping
])
def test_rdp_refuses_rather_than_printing_a_credential_pair_it_never_got(
        answered, missing, hosts, execvp, monkeypatch):
    """A blank user over a blank password is laid out exactly like a real pair.

    `reset-windows-password` exiting 0 with nothing on stdout raises nothing, so
    the refusal path never fired; `.get(name, "")` then turned every one of these
    shapes into an empty string and printed it under "forwarding RDP". The user
    found out at a Windows login prompt they could not pass, with nothing in our
    output pointing back at us. Whatever we did not receive, we do not print.
    """
    monkeypatch.setattr(
        gcloud_module.Gcloud, "windows_password",
        lambda self, instance, zone, project: answered,
    )

    result = CliRunner().invoke(app, ["rdp", "comfy-win", "--config", hosts])

    assert result.exit_code == 1, result.output
    assert execvp == [], "the forward must not start on credentials we do not have"
    assert "forwarding RDP" not in result.output
    assert missing in result.output, "it names what came back missing"
    assert "gcloud compute reset-windows-password win-instance" in result.output

    # The danger is the *layout*, not the words: a labelled column reads as a
    # real pair whatever is in it. The words "user" and "password" are free to
    # appear in the refusal, which is prose. No line may be laid out as one.
    laid_out = [line for line in result.output.splitlines()
                if line.startswith(("user ", "password ", "address "))]
    assert laid_out == [], laid_out


def test_rdp_prints_the_pair_it_did_get(hosts, execvp, monkeypatch):
    """The guard must not cost the working case its output."""
    monkeypatch.setattr(
        gcloud_module.Gcloud, "windows_password",
        lambda self, instance, zone, project: {
            "username": "ali", "password": "hunter2"},
    )

    result = CliRunner().invoke(app, ["rdp", "comfy-win", "--config", hosts])

    assert isinstance(result.exception, Replaced)
    assert "user     ali" in result.output
    assert "password hunter2" in result.output


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
