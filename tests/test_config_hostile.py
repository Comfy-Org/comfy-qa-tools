"""hosts.toml is a file a person edits, so it arrives in every shape a person makes.

`config.py` says of itself that loading the host list is "deliberately offline and
total: every rule that can be checked without gcloud is checked here". Each test
below was once a strict xfail recording a rule it did not check; every one of
them now asserts the real behaviour, and the file reads as the standard rather
than as a list of debts.

Two groups. The first is tracebacks — `load` promises a message and gave a stack
trace instead. The second is worse: host lists that loaded cleanly and then
pointed a tester at a machine they did not mean to read.

Each refusal below is asserted by its message, not merely by its type. `parse`
has twenty-four paths that raise `ConfigError`, so `pytest.raises(ConfigError)`
alone answers "something objected", which is a weaker question than the one being
asked. These fixtures are each valid but for the one property under test, so
today no neighbouring rule could satisfy them — but that is a property of the
fixtures, not something the assertion checks, and the next rule added to `parse`
could quietly start catching one of them first. Then the test passes, the rule it
names is gone, and nothing says so.

The rule, which cost a real finding to learn: **assert which failure, not merely
that one happened, wherever more than one path can raise the same type.** It is
one `caught.value` check at a place you already know two paths converge, and it
is far cheaper than rediscovering it.
"""

from __future__ import annotations

import os
import socket

import pytest

from comfy_qa.config import ConfigError, load, parse


def written(tmp_path, text, name="hosts.toml"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


GCE = """\
[hosts.{name}]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "{instance}"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = {port}
"""


# --- a message, not a stack trace ---------------------------------------------


def test_a_host_list_that_is_a_directory_is_a_message(tmp_path):
    """`--config ~/.config/comfy-qa-tools` — the directory, not the file in it."""
    place = tmp_path / "hosts.toml"
    place.mkdir()
    with pytest.raises(ConfigError) as caught:
        load(place)
    assert "could not be read" in str(caught.value)


def test_a_host_list_that_cannot_be_read_is_a_message(tmp_path):
    """Restored from a backup with the wrong owner is all it takes."""
    path = written(tmp_path, '[hosts.local]\nkind = "local"\n')
    os.chmod(path, 0o000)
    try:
        with pytest.raises(ConfigError) as caught:
            load(path)
        assert "could not be read" in str(caught.value)
    finally:
        os.chmod(path, 0o600)


def test_a_host_list_that_is_not_text_is_a_message(tmp_path):
    """A truncated write, a copied binary, an editor that saved UTF-16."""
    path = tmp_path / "hosts.toml"
    path.write_bytes(b'[hosts.local]\nkind = "local"\nos = "\xff\xfe"\n')
    with pytest.raises(ConfigError) as caught:
        load(path)
    assert "is not UTF-8 text" in str(caught.value)


def test_parsing_something_that_is_not_a_table_is_a_message():
    with pytest.raises(ConfigError) as caught:
        parse([])
    assert "expected a host list" in str(caught.value)


# --- loads cleanly, points at the wrong machine -------------------------------


def test_two_hosts_cannot_be_the_same_cloud_box():
    """The port rule says every host answers on its own port. It does not say
    every host is its own machine.

    Two entries, two ports, two tunnels, one box. Both stamp as different hosts,
    a matrix records "reproduced on comfy-win, not on comfy-win-b", and the two
    were the same machine the whole time.
    """
    with pytest.raises(ConfigError) as caught:
        parse({"hosts": {
            "comfy-win": {"kind": "gce", "port": 8190, "os": "Windows Server 2022",
                          "gpu": "L4", "gce_instance": "comfy-win",
                          "gce_zone": "us-central1-a", "gce_project": "proj"},
            "comfy-win-b": {"kind": "gce", "port": 8191, "os": "Windows Server 2022",
                            "gpu": "L4", "gce_instance": "comfy-win",
                            "gce_zone": "us-central1-a", "gce_project": "proj"},
        }})
    assert "are the same machine" in str(caught.value)


def test_a_cloud_box_may_not_be_called_local():
    """`comfy-qat host stamp local` has one obvious meaning: this machine.

    Nothing stops a `gce` host taking the name, and the starter file teaches
    everyone that `local` is the Mac. Every rule in this file exists to remove
    invisible defaults; a cloud box wearing that name reinstates one.
    """
    with pytest.raises(ConfigError) as caught:
        parse({"hosts": {"local": {
            "kind": "gce", "port": 8190, "os": "Ubuntu 22.04", "gpu": "L4",
            "gce_instance": "comfy-linux", "gce_zone": "us-central1-a",
            "gce_project": "proj"}}})
    assert "the name 'local' is reserved" in str(caught.value)


def test_two_hosts_cannot_differ_only_in_case():
    """`Comfy-Win` and `comfy-win` are two hosts on two ports, and which one you
    get depends on a shift key."""
    with pytest.raises(ConfigError) as caught:
        parse({"hosts": {
            "comfy-win": {"kind": "gce", "port": 8190, "os": "o", "gpu": "L4",
                          "gce_instance": "a", "gce_zone": "z", "gce_project": "p"},
            "Comfy-Win": {"kind": "gce", "port": 8191, "os": "o", "gpu": "L4",
                          "gce_instance": "b", "gce_zone": "z", "gce_project": "p"},
        }})
    assert "differ only in case" in str(caught.value)


def test_a_local_host_cannot_carry_cloud_fields():
    """This one costs money.

    `put_away` branches on `kind`: for a local host it says "local ComfyUI left
    running" and returns without calling stop. A `gce` entry mistyped as `local`
    — or edited down to one after a move — reads as a successful `host down`
    while the GPU keeps billing.
    """
    with pytest.raises(ConfigError) as caught:
        parse({"hosts": {"local": {
            "kind": "local", "port": 8188, "os": "Ubuntu 22.04", "gpu": "L4",
            "gce_instance": "comfy-linux", "gce_zone": "us-central1-a",
            "gce_project": "proj"}}})
    assert "kind 'local' cannot carry" in str(caught.value)


@pytest.mark.parametrize("name", ["a/b", "../evil", "", "   ", " comfy-win ", "--config",
                                  "-f", "comfy\nwin", "comfy\twin"])
def test_a_host_name_has_to_be_something_a_person_can_type(name):
    """A name is an argument (`host stamp <name>`) and part of a filename
    (`tunnels/<name>.pid`). Names that are neither used to be accepted: one
    starting with `-` is read as an option, one with a separator escapes the
    tunnel directory, and one with padding cannot be typed at all."""
    with pytest.raises(ConfigError) as caught:
        parse({"hosts": {name: {"kind": "local", "port": 9001}}})
    assert "cannot be used" in str(caught.value)


# --- and the other half: none of those rules may refuse a good host list ------
#
# A rule that over-refuses is worse than the defect it closes, because it stops
# someone testing at all. Each of these is a host list the rules above must let
# through untouched.


@pytest.mark.parametrize("name", ["comfy-win", "comfy_win", "comfy.win.2", "A1", "x",
                                  "local", "Local", "comfy-win-2026"])
def test_ordinary_names_are_still_names(name):
    assert [host.name for host in parse(
        {"hosts": {name: {"kind": "local", "port": 9001}}})] == [name]


def test_a_local_host_may_still_describe_itself():
    """`os` and `gpu` are not cloud fields. Only the three `gce_*` ones are, and a
    Mac that declares what it runs is how `host stamp local` catches a mismatch."""
    [host] = parse({"hosts": {"local": {
        "kind": "local", "port": 8188, "os": "macOS 15.2", "gpu": "mps"}}})
    assert (host.os, host.gpu) == ("macOS 15.2", "mps")


def test_two_boxes_in_two_zones_are_two_machines():
    """The duplicate rule compares project, zone and instance together. An
    instance name repeated in a different zone is a different box, and moving one
    with `host move` is exactly how that happens."""
    hosts = parse({"hosts": {
        "comfy-win": {"kind": "gce", "port": 8190, "os": "Windows Server 2022",
                      "gpu": "L4", "gce_instance": "comfy-win",
                      "gce_zone": "us-central1-a", "gce_project": "proj"},
        "comfy-win-b": {"kind": "gce", "port": 8191, "os": "Windows Server 2022",
                        "gpu": "L4", "gce_instance": "comfy-win",
                        "gce_zone": "us-west1-b", "gce_project": "proj"},
    }})
    assert [host.name for host in hosts] == ["comfy-win", "comfy-win-b"]


def test_the_starter_host_list_this_tool_writes_still_loads(tmp_path):
    """`host init` writes it and `host list` reads it, so it has to satisfy every
    rule the loader enforces — including the ones added after it was written."""
    from typer.testing import CliRunner

    from comfy_qa.host import app

    path = tmp_path / "hosts.toml"
    result = CliRunner().invoke(app, ["init", "--config", str(path)])
    assert result.exit_code == 0, result.output
    assert [host.name for host in load(path)] == ["local"]


# --- the CLI half -------------------------------------------------------------


def cli():
    from typer.testing import CliRunner

    from comfy_qa.host import app

    return CliRunner(), app


def spoke(result):
    """The command ended by choosing to, not by falling over.

    `result.exception is None` is the obvious way to write "no traceback" and it
    is wrong: click's runner records the `SystemExit` behind every non-zero exit,
    so that assertion cannot hold for any command that fails on purpose. What
    separates a message from a traceback is which exception it is — a deliberate
    exit, or the `OSError` nobody caught.
    """
    exc = result.exception
    assert exc is None or isinstance(exc, SystemExit), exc
    return result


def test_init_into_a_place_it_cannot_write_is_a_message(tmp_path):
    """A folder with no write permission — a restored backup is enough."""
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    runner, app = cli()
    try:
        result = spoke(
            runner.invoke(app, ["init", "--config", str(locked / "d" / "hosts.toml")]))
        assert result.exit_code == 2
        assert "could not write a host list" in result.output
    finally:
        os.chmod(locked, 0o700)


def test_init_forced_onto_a_directory_is_a_message(tmp_path):
    """`--force` aimed at the folder rather than the hosts.toml inside it."""
    place = tmp_path / "hosts.toml"
    place.mkdir()
    runner, app = cli()
    result = spoke(runner.invoke(app, ["init", "--config", str(place), "--force"]))
    assert result.exit_code == 2
    assert "could not write a host list" in result.output


def test_open_does_not_claim_a_tunnel_that_goes_somewhere_else(tmp_path, monkeypatch):
    """Two host lists, one name — the wrong-machine failure at the CLI.

    A tunnel is open for the `comfy-win` in one host list. Another host list
    calls a different box `comfy-win` on a different port. `host open` used to
    see a live pid file under that name and report the URL from *its* host list,
    which reaches the first box or nothing at all.

    The refusal names both ports, so "8190 must not appear" is no longer the
    question — the question is whether the command claimed that URL. It must
    exit non-zero, must not say the tunnel is already open, and must name the
    box the open tunnel actually goes to, because that is what tells you which
    of your two host lists you are looking at.
    """
    from comfy_qa import tunnel
    from comfy_qa.config import Host

    monkeypatch.setattr(tunnel, "TUNNEL_DIR", tmp_path / "tunnels")
    other = Host(name="comfy-win", kind="gce", port=8195, os="Ubuntu 22.04", gpu="L4",
                 gce_instance="comfy-win-2", gce_zone="us-west1-b", gce_project="proj")
    tunnel.open_tunnel(other, launcher=lambda cmd, log: os.getpid())

    path = written(tmp_path, GCE.format(name="comfy-win", instance="comfy-win", port=8190))
    runner, app = cli()
    result = spoke(runner.invoke(app, ["open", "comfy-win", "--config", str(path)]))

    assert result.exit_code != 0, (
        f"claimed a tunnel on 8190 while the open tunnel goes to comfy-win-2: "
        f"{result.output!r}"
    )
    assert "already open: " not in result.output
    assert "comfy-win-2" in result.output


def test_open_onto_a_port_someone_else_holds_is_a_message(tmp_path, monkeypatch, real_port_busy):
    """`open_tunnel` refuses a port it cannot bind, and `open` has to say so.

    `TunnelError` is not a `LifecycleError` — `lifecycle` imports `tunnel`, so it
    cannot be — but it carries the same message, `fix` and `kind`, which is what
    lets one `except` in `_act` cover both. Without that the refusal reaches the
    terminal as a traceback on a path a tester hits routinely.

    Asks for `real_port_busy` because the suite otherwise answers "the port is
    free" for every test — this Mac really does run a tunnel on 8190, and the
    end-to-end harness deliberately serves a fake ComfyUI on the port a tunnel
    would forward. This test is the one that is about the check itself.
    """
    from comfy_qa import tunnel

    monkeypatch.setattr(tunnel, "TUNNEL_DIR", tmp_path / "tunnels")
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        taken = held.getsockname()[1]

        path = written(tmp_path, GCE.format(name="comfy-win", instance="comfy-win",
                                            port=taken))
        runner, app = cli()
        result = spoke(runner.invoke(app, ["open", "comfy-win", "--config", str(path)]))

    assert result.exit_code != 0
    assert str(taken) in result.output
    assert "lsof" in result.output, "the fix has to be something you can run"


def test_stamp_refuses_to_certify_a_machine_that_contradicts_the_host_list(tmp_path,
                                                                          monkeypatch):
    """The host list says Windows and an L4. The port answers darwin and mps.

    That used to print as a clean evidence line under the cloud box's name — the
    exact artefact someone pastes into a bug report to say which machine produced
    a result. It is refused rather than warned about, because the line's whole
    purpose is to be copied and a warning on stderr does not survive being
    copied: printing it at all is what creates the false evidence.
    """
    import comfy_qa.host as host_module
    from comfy_qa.stamp import Stamp

    monkeypatch.setattr(host_module, "fetch", lambda url, host: Stamp(
        host=host, url=url, os="darwin", devices=["mps (32GB)"],
        comfyui_version="0.33.0"))

    path = written(tmp_path, GCE.format(name="comfy-win", instance="comfy-win", port=8190)
                   .replace("Ubuntu 22.04", "Windows Server 2022"))
    runner, app = cli()
    result = spoke(runner.invoke(app, ["stamp", "comfy-win", "--config", str(path)]))

    assert result.exit_code != 0, f"certified a mismatched machine: {result.output!r}"
    assert "ComfyUI 0.33.0" not in result.output, "printed the line anyway"
    assert "Windows Server 2022" in result.output and "darwin" in result.output, (
        "the refusal has to name both sides of the contradiction"
    )


def test_stamp_json_is_refused_on_the_same_contradiction(tmp_path, monkeypatch):
    """`--json` is the same artefact in another format, so it gets the same answer.

    Refusing the line and then emitting the JSON would leave the wrong-machine
    capture in the format most likely to be committed to a test record.
    """
    import comfy_qa.host as host_module
    from comfy_qa.stamp import Stamp

    monkeypatch.setattr(host_module, "fetch", lambda url, host: Stamp(
        host=host, url=url, os="darwin", devices=["mps (32GB)"],
        comfyui_version="0.33.0"))

    path = written(tmp_path, GCE.format(name="comfy-win", instance="comfy-win", port=8190)
                   .replace("Ubuntu 22.04", "Windows Server 2022"))
    runner, app = cli()
    result = spoke(
        runner.invoke(app, ["stamp", "comfy-win", "--json", "--config", str(path)]))

    assert result.exit_code != 0
    assert "0.33.0" not in result.output


def test_stamp_still_prints_when_the_machine_matches_what_was_declared(tmp_path,
                                                                      monkeypatch):
    """The refusal must not be a blanket one — a Windows box answering as Windows
    with the card it declared is exactly the evidence line this command is for."""
    import comfy_qa.host as host_module
    from comfy_qa.stamp import Stamp

    monkeypatch.setattr(host_module, "fetch", lambda url, host: Stamp(
        host=host, url=url, os="Windows 10", devices=["cuda:0 NVIDIA L4 (22GB)"],
        comfyui_version="0.33.0"))

    path = written(tmp_path, GCE.format(name="comfy-win", instance="comfy-win", port=8190)
                   .replace("Ubuntu 22.04", "Windows Server 2022"))
    runner, app = cli()
    result = runner.invoke(app, ["stamp", "comfy-win", "--config", str(path)])

    assert result.exit_code == 0, result.output
    assert "ComfyUI 0.33.0" in result.output
