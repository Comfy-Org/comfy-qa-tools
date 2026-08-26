"""hosts.toml is a file a person edits, so it arrives in every shape a person makes.

`config.py` says of itself that loading the host list is "deliberately offline and
total: every rule that can be checked without gcloud is checked here". These are
the ones it does not check yet, and the two files that own them —
`comfy_qa/config.py` and `comfy_qa/host.py` — belong to someone else, so each
finding is recorded as a strict xfail rather than fixed here.

Strict, so that fixing one turns this file red until the marker comes off: a
recorded defect cannot be quietly outlived.

Two groups. The first is tracebacks — `load` promises a message and gives a
stack trace instead. The second is worse: host lists that load cleanly and then
point a tester at a machine they did not mean to read.
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


@pytest.mark.xfail(reason="config.load: read_text raises IsADirectoryError; catch OSError "
                          "and raise ConfigError", strict=True)
def test_a_host_list_that_is_a_directory_is_a_message(tmp_path):
    """`--config ~/.config/comfy-qa-tools` — the directory, not the file in it."""
    place = tmp_path / "hosts.toml"
    place.mkdir()
    with pytest.raises(ConfigError):
        load(place)


@pytest.mark.xfail(reason="config.load: read_text raises PermissionError; catch OSError "
                          "and raise ConfigError", strict=True)
def test_a_host_list_that_cannot_be_read_is_a_message(tmp_path):
    """Restored from a backup with the wrong owner is all it takes."""
    path = written(tmp_path, '[hosts.local]\nkind = "local"\n')
    os.chmod(path, 0o000)
    try:
        with pytest.raises(ConfigError):
            load(path)
    finally:
        os.chmod(path, 0o600)


@pytest.mark.xfail(reason="config.load: read_text(encoding='utf-8') raises "
                          "UnicodeDecodeError; catch it and raise ConfigError", strict=True)
def test_a_host_list_that_is_not_text_is_a_message(tmp_path):
    """A truncated write, a copied binary, an editor that saved UTF-16."""
    path = tmp_path / "hosts.toml"
    path.write_bytes(b'[hosts.local]\nkind = "local"\nos = "\xff\xfe"\n')
    with pytest.raises(ConfigError):
        load(path)


@pytest.mark.xfail(reason="config.parse: .get on a list raises AttributeError; check "
                          "isinstance(data, dict) first", strict=True)
def test_parsing_something_that_is_not_a_table_is_a_message():
    with pytest.raises(ConfigError):
        parse([])


# --- loads cleanly, points at the wrong machine -------------------------------


@pytest.mark.xfail(reason="config.parse: two hosts may name one instance. Reject a "
                          "duplicate (gce_project, gce_zone, gce_instance)", strict=True)
def test_two_hosts_cannot_be_the_same_cloud_box():
    """The port rule says every host answers on its own port. It does not say
    every host is its own machine.

    Two entries, two ports, two tunnels, one box. Both stamp as different hosts,
    a matrix records "reproduced on comfy-win, not on comfy-win-b", and the two
    were the same machine the whole time.
    """
    with pytest.raises(ConfigError):
        parse({"hosts": {
            "comfy-win": {"kind": "gce", "port": 8190, "os": "Windows Server 2022",
                          "gpu": "L4", "gce_instance": "comfy-win",
                          "gce_zone": "us-central1-a", "gce_project": "proj"},
            "comfy-win-b": {"kind": "gce", "port": 8191, "os": "Windows Server 2022",
                            "gpu": "L4", "gce_instance": "comfy-win",
                            "gce_zone": "us-central1-a", "gce_project": "proj"},
        }})


@pytest.mark.xfail(reason="config.parse: a cloud box may be called 'local'. Reserve the "
                          "name for kind='local'", strict=True)
def test_a_cloud_box_may_not_be_called_local():
    """`comfy-qat host stamp local` has one obvious meaning: this machine.

    Nothing stops a `gce` host taking the name, and the starter file teaches
    everyone that `local` is the Mac. Every rule in this file exists to remove
    invisible defaults; a cloud box wearing that name reinstates one.
    """
    with pytest.raises(ConfigError):
        parse({"hosts": {"local": {
            "kind": "gce", "port": 8190, "os": "Ubuntu 22.04", "gpu": "L4",
            "gce_instance": "comfy-linux", "gce_zone": "us-central1-a",
            "gce_project": "proj"}}})


@pytest.mark.xfail(reason="config.parse: names differing only in case both load and "
                          "find() is case-sensitive. Reject case-insensitive duplicates",
                   strict=True)
def test_two_hosts_cannot_differ_only_in_case():
    """`Comfy-Win` and `comfy-win` are two hosts on two ports, and which one you
    get depends on a shift key."""
    with pytest.raises(ConfigError):
        parse({"hosts": {
            "comfy-win": {"kind": "gce", "port": 8190, "os": "o", "gpu": "L4",
                          "gce_instance": "a", "gce_zone": "z", "gce_project": "p"},
            "Comfy-Win": {"kind": "gce", "port": 8191, "os": "o", "gpu": "L4",
                          "gce_instance": "b", "gce_zone": "z", "gce_project": "p"},
        }})


@pytest.mark.xfail(reason="config.parse: a local host may carry gce_* fields, so `host "
                          "down` never stops the box. Reject gce_* on kind='local'",
                   strict=True)
def test_a_local_host_cannot_carry_cloud_fields():
    """This one costs money.

    `put_away` branches on `kind`: for a local host it says "local ComfyUI left
    running" and returns without calling stop. A `gce` entry mistyped as `local`
    — or edited down to one after a move — reads as a successful `host down`
    while the GPU keeps billing.
    """
    with pytest.raises(ConfigError):
        parse({"hosts": {"local": {
            "kind": "local", "port": 8188, "os": "Ubuntu 22.04", "gpu": "L4",
            "gce_instance": "comfy-linux", "gce_zone": "us-central1-a",
            "gce_project": "proj"}}})


@pytest.mark.parametrize("name", ["a/b", "../evil", "", "   ", " comfy-win ", "--config",
                                  "-f", "comfy\nwin", "comfy\twin"])
@pytest.mark.xfail(reason="config.parse: host names are unrestricted TOML keys. Require "
                          "[A-Za-z0-9][A-Za-z0-9._-]* so a name is typeable, cannot look "
                          "like a flag, and cannot become a path", strict=True)
def test_a_host_name_has_to_be_something_a_person_can_type(name):
    """A name is an argument (`host stamp <name>`) and part of a filename
    (`tunnels/<name>.pid`). Names that are neither are accepted today: one
    starting with `-` is read as an option, one with a separator escapes the
    tunnel directory, and one with padding cannot be typed at all."""
    with pytest.raises(ConfigError):
        parse({"hosts": {name: {"kind": "local", "port": 9001}}})


# --- the CLI half -------------------------------------------------------------


def cli():
    from typer.testing import CliRunner

    from comfy_qa.host import app

    return CliRunner(), app


@pytest.mark.xfail(reason="host.init_cmd: mkdir/write_text raise PermissionError. Wrap "
                          "both in try/except OSError and exit 2 with the path",
                   strict=True)
def test_init_into_a_place_it_cannot_write_is_a_message(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    runner, app = cli()
    try:
        result = runner.invoke(app, ["init", "--config", str(locked / "d" / "hosts.toml")])
        assert result.exception is None, result.exception
        assert result.exit_code == 2
    finally:
        os.chmod(locked, 0o700)


@pytest.mark.xfail(reason="host.init_cmd: --force onto a directory raises "
                          "IsADirectoryError. Same try/except OSError", strict=True)
def test_init_forced_onto_a_directory_is_a_message(tmp_path):
    place = tmp_path / "hosts.toml"
    place.mkdir()
    runner, app = cli()
    result = runner.invoke(app, ["init", "--config", str(place), "--force"])
    assert result.exception is None, result.exception
    assert result.exit_code == 2


@pytest.mark.xfail(reason="host.open_cmd: `tunnel_status(host.name)` ignores which machine "
                          "the recorded tunnel goes to and prints host.url regardless. "
                          "Drop the early return and let open_tunnel decide — it now "
                          "checks the record against the host", strict=True)
def test_open_does_not_claim_a_tunnel_that_goes_somewhere_else(tmp_path, monkeypatch):
    """Two host lists, one name — the wrong-machine failure at the CLI.

    A tunnel is open for the `comfy-win` in one host list. Another host list
    calls a different box `comfy-win` on a different port. `host open` sees a
    live pid file under that name and reports the URL from *its* host list, which
    reaches the first box or nothing at all.
    """
    from comfy_qa import tunnel
    from comfy_qa.config import Host

    monkeypatch.setattr(tunnel, "TUNNEL_DIR", tmp_path / "tunnels")
    other = Host(name="comfy-win", kind="gce", port=8195, os="Ubuntu 22.04", gpu="L4",
                 gce_instance="comfy-win-2", gce_zone="us-west1-b", gce_project="proj")
    tunnel.open_tunnel(other, launcher=lambda cmd, log: os.getpid())

    path = written(tmp_path, GCE.format(name="comfy-win", instance="comfy-win", port=8190))
    runner, app = cli()
    result = runner.invoke(app, ["open", "comfy-win", "--config", str(path)])

    assert "8190" not in result.output, (
        "claimed a tunnel on 8190 while the open tunnel goes to comfy-win-2"
    )


@pytest.mark.xfail(reason="host.open_cmd: open_tunnel now raises TunnelError when the port "
                          "is taken or the record names another box. Catch TunnelError "
                          "beside ConfigError and exit 2 with its message and fix. `up` "
                          "and `go` reach open_tunnel through lifecycle.bring_up and need "
                          "the same: TunnelError carries .fix and .kind exactly as "
                          "LifecycleError does, so `_act` handles both with one except",
                   strict=True)
def test_open_onto_a_port_someone_else_holds_is_a_message(tmp_path, monkeypatch):
    from comfy_qa import tunnel

    monkeypatch.setattr(tunnel, "TUNNEL_DIR", tmp_path / "tunnels")
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        taken = held.getsockname()[1]

        path = written(tmp_path, GCE.format(name="comfy-win", instance="comfy-win",
                                            port=taken))
        runner, app = cli()
        result = runner.invoke(app, ["open", "comfy-win", "--config", str(path)])

    assert result.exception is None, result.exception
    assert result.exit_code != 0
    assert str(taken) in result.output


@pytest.mark.xfail(reason="host.stamp_cmd: prints the line whatever answered. Call "
                          "stamp.mismatch(host, stamp) and refuse to print an evidence "
                          "line for a machine that contradicts its declaration",
                   strict=True)
def test_stamp_refuses_to_certify_a_machine_that_contradicts_the_host_list(tmp_path,
                                                                          monkeypatch):
    """The host list says Windows and an L4. The port answers darwin and mps.

    Today that prints as a clean evidence line under the cloud box's name — the
    exact artefact someone pastes into a bug report to say which machine produced
    a result.
    """
    import comfy_qa.host as host_module
    from comfy_qa.stamp import Stamp

    monkeypatch.setattr(host_module, "fetch", lambda url, host: Stamp(
        host=host, url=url, os="darwin", devices=["mps (32GB)"],
        comfyui_version="0.33.0"))

    path = written(tmp_path, GCE.format(name="comfy-win", instance="comfy-win", port=8190)
                   .replace("Ubuntu 22.04", "Windows Server 2022"))
    runner, app = cli()
    result = runner.invoke(app, ["stamp", "comfy-win", "--config", str(path)])

    assert result.exit_code != 0, f"certified a mismatched machine: {result.output!r}"
