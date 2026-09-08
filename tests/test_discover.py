"""Finding the cloud boxes that already exist.

The instance fixture is a real record from a live project — name, zone, machine
type, accelerator and licence exactly as gcloud returns them. Inventing this shape
is what produced the quota bugs.
"""

from __future__ import annotations

import pytest

from comfy_qa.config import Host
from comfy_qa.discover import (
    accelerator,
    clash_note,
    label_clashes,
    new_hosts,
    next_ports,
    operating_system,
    parse,
    to_toml,
)

COMFY_WIN = {
    "name": "comfy-win",
    "zone": "https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a",
    "status": "TERMINATED",
    "machineType": "https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a/machineTypes/g2-standard-8",
    "guestAccelerators": [{
        "acceleratorType": "https://www.googleapis.com/compute/v1/projects/p/zones/us-central1-a/acceleratorTypes/nvidia-l4",
        "acceleratorCount": 1,
    }],
    "disks": [{
        "boot": True,
        "licenses": ["https://www.googleapis.com/compute/v1/projects/windows-cloud/global/licenses/windows-server-2022-dc"],
    }],
}

LOCAL = Host(name="local", kind="local", port=8188)


def test_a_real_instance_fills_in_everything_you_would_have_typed():
    box = parse(COMFY_WIN, "stately-timing-504610-p1")
    assert box.name == "comfy-win"
    assert box.os == "Windows Server 2022"
    assert box.gpu == "L4"
    assert box.gce_zone == "us-central1-a"
    assert box.gce_project == "stately-timing-504610-p1"


def test_terminated_reads_as_stopped_not_broken():
    """TERMINATED is Google's word for stopped. Shown raw it reads as a fault."""
    assert parse(COMFY_WIN, "p").running is False
    assert parse(dict(COMFY_WIN, status="RUNNING"), "p").running is True


@pytest.mark.parametrize("state", [
    "STAGING", "PROVISIONING", "REPAIRING", "STOPPING", "SUSPENDING",
    "SUSPENDED", "DEPROVISIONING", "", "Running", "something-new",
])
def test_only_terminated_reads_as_stopped(state):
    """A machine has eight documented states and only one of them is stopped.

    This read `status == "RUNNING"`, so a box in STAGING — the first 30-60
    seconds of every start — was reported as stopped by `discover`, which is the
    one command whose whole job is telling you what exists in a project that
    nothing recorded. Fixed in b6f8d4f and, until now, unpinned: reverting the
    line left the entire suite green, so the only member of this class that could
    silently revert was the one nobody could see revert.

    Anything unrecognised counts as running. Being wrong that way makes someone
    look at a box that is off; being wrong the other way hides one that is on.
    """
    assert parse(dict(COMFY_WIN, status=state), "p").running is True, (
        f"a box in {state!r} was reported as stopped"
    )


@pytest.mark.parametrize("licence,expected", [
    ("windows-server-2022-dc", "Windows Server 2022"),
    ("ubuntu-2204-lts", "Ubuntu 22.04"),
    ("debian-12", "Debian 12"),
])
def test_licences_become_readable_names(licence, expected):
    disks = [{"boot": True, "licenses": [f"projects/x/global/licenses/{licence}"]}]
    assert operating_system(disks) == expected


def test_an_unknown_licence_is_reported_not_guessed():
    """Guessing 'Linux' would pick the wrong startup script later."""
    disks = [{"boot": True, "licenses": ["projects/x/global/licenses/some-new-distro"]}]
    assert operating_system(disks) == "some-new-distro"


def test_no_licences_at_all_is_survivable():
    assert operating_system(None) == "unknown"
    assert operating_system([{}]) == "unknown"


@pytest.mark.parametrize("accel_type,expected", [
    ("nvidia-l4", "L4"),
    ("nvidia-tesla-t4", "T4"),
    ("nvidia-a100-80gb", "A100-80GB"),
])
def test_accelerator_names_are_the_ones_people_say(accel_type, expected):
    instance = {"guestAccelerators": [{"acceleratorType": f"zones/z/acceleratorTypes/{accel_type}"}]}
    assert accelerator(instance) == expected


def test_a_box_with_no_gpu_is_not_a_crash():
    assert accelerator({"name": "plain"}) == ""
    assert parse({"name": "plain"}, "p").has_gpu is False


def test_ports_never_reuse_and_never_take_8188():
    assert next_ports([LOCAL], 3) == [8190, 8191, 8192]


def test_ports_step_over_anything_already_declared():
    taken = Host(name="other", kind="gce", port=8190, gce_instance="x")
    assert next_ports([LOCAL, taken], 2) == [8191, 8192]


def test_only_boxes_missing_from_the_host_list_are_added():
    box = parse(COMFY_WIN, "p")
    already = Host(name="win", kind="gce", port=8190, gce_instance="comfy-win")
    assert new_hosts([box], [LOCAL, already]) == []


def test_matching_is_on_the_instance_not_your_label():
    """Renaming a host in your own file must not make it reappear as a duplicate."""
    box = parse(COMFY_WIN, "p")
    renamed = Host(name="my-windows-box", kind="gce", port=8190, gce_instance="comfy-win")
    assert new_hosts([box], [renamed]) == []


def test_the_generated_block_is_valid_and_complete():
    import tomllib

    box = parse(COMFY_WIN, "p")
    parsed = tomllib.loads(to_toml(box, 8190))
    entry = parsed["hosts"]["comfy-win"]
    assert entry == {
        "kind": "gce", "os": "Windows Server 2022", "gpu": "L4",
        "gce_instance": "comfy-win", "gce_zone": "us-central1-a",
        "gce_project": "p", "port": 8190,
    }


def test_a_generated_block_survives_the_config_loader():
    """Whatever discovery writes has to pass the same validation as hand-written."""
    from comfy_qa.config import parse as parse_config
    import tomllib

    box = parse(COMFY_WIN, "p")
    text = '[hosts.local]\nkind = "local"\nport = 8188\n' + to_toml(box, 8190)
    hosts = parse_config(tomllib.loads(text))
    assert {h.name for h in hosts} == {"local", "comfy-win"}


def test_a_label_that_differs_only_in_case_is_not_something_to_add():
    """The block would be headed with GOOGLE's name, and the loader refuses both.

    You renamed your entry for a box by hand — which `new_hosts` invites, and the
    test above pins — and pointed it at the instance you renamed. Google's
    `comfy-win` is then genuinely unrecorded by instance, so this used to return
    it, and `to_toml` heads the block `[hosts.comfy-win]` beside your
    `[hosts.Comfy-Win]`. That parses and `config.parse` refuses it.
    """
    box = parse(COMFY_WIN, "p")
    mine = Host(name="Comfy-Win", kind="gce", port=8190, gce_instance="comfy-win-old")
    assert new_hosts([box], [LOCAL, mine]) == []


def test_a_box_left_out_over_a_label_is_reported_not_dropped():
    """Silently adding nothing looks exactly like finding nothing to do."""
    box = parse(COMFY_WIN, "p")
    mine = Host(name="Comfy-Win", kind="gce", port=8190, gce_instance="comfy-win-old")
    assert label_clashes([box], [LOCAL, mine]) == [(box, "Comfy-Win")]
    note = clash_note(box, "Comfy-Win")
    assert "comfy-win" in note and "Comfy-Win" in note


def test_an_ordinary_new_box_is_still_added():
    """The guard above must not swallow the case this command exists for."""
    box = parse(COMFY_WIN, "p")
    assert new_hosts([box], [LOCAL]) == [(box, 8190)]
    assert label_clashes([box], [LOCAL]) == []


# --- `discover`, through the real CLI ---------------------------------------
#
# The unit tests above pin what `new_hosts` returns. These pin the thing that
# actually went wrong, which is a PROPERTY OF THE FILE AFTERWARDS: `discover`
# appended with a bare `path.open("a")`, so a block it should not have written
# reached the disk with no validation and no backup, and every comfy-qat command
# was dead until somebody hand-edited the file. `added 1 host`, exit 0.


class _Cloud:
    """One project, one list of instances. Anything else is a call not expected."""

    def __init__(self, instances, project="p"):
        self._instances = list(instances)
        self._project = project

    def current_project(self):
        return self._project

    def list_instances(self, project):
        assert project == self._project
        return list(self._instances)


def _discover(monkeypatch, tmp_path, text, instances):
    from typer.testing import CliRunner

    from comfy_qa import gcloud as gcloud_module
    from comfy_qa.cli import app

    path = tmp_path / "hosts.toml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: _Cloud(instances))
    result = CliRunner().invoke(app, ["discover", "--config", str(path)])
    return result, path


def _loads(path):
    from comfy_qa.config import ConfigError, load

    try:
        return load(path)
    except ConfigError as exc:
        raise AssertionError(f"the host list no longer loads: {exc}") from exc


MINE = """\
# my machines
[hosts.local]
kind = "local"
port = 8188

[hosts.Comfy-Win]
kind         = "gce"
os           = "Windows Server 2022"
gpu          = "L4"
gce_instance = "comfy-win-old"
gce_zone     = "us-central1-a"
gce_project  = "p"
port         = 8190
"""


def test_discover_leaves_a_host_list_every_command_can_still_read(monkeypatch, tmp_path):
    """The whole defect in one assertion: run it, then load the file.

    Renaming an entry is invited by `new_hosts`'s own docstring, so this file is
    ordinary. Before the fix the run exited 0 saying `added 1 host` and left a
    file `config.load` refuses — after which no comfy-qat command works at all.
    """
    result, path = _discover(monkeypatch, tmp_path, MINE, [COMFY_WIN])

    assert result.exit_code == 0, result.output
    hosts = _loads(path)
    assert {host.name for host in hosts} == {"local", "Comfy-Win"}
    assert "was not added" in result.output
    assert "added 1 host" not in result.output


def test_a_refused_write_leaves_the_file_exactly_as_it_was(monkeypatch, tmp_path):
    """`hostfile.apply` refuses rather than writing something unloadable.

    The label guard is what stops this reaching the write at all, so it is forced
    here from the other side: a block naming an instance a second entry already
    claims. `config.parse` refuses that, `apply` refuses to write it, and the
    promise being pinned is that the ORIGINAL is still byte-identical afterwards.
    """
    from comfy_qa import hostfile

    path = tmp_path / "hosts.toml"
    path.write_text(MINE, encoding="utf-8")
    before = path.read_bytes()

    twin = to_toml(parse(dict(COMFY_WIN, name="another-name"), "p"), 8191)
    twin = twin.replace('gce_instance = "another-name"',
                        'gce_instance = "comfy-win-old"')
    with pytest.raises(hostfile.HostFileError) as refusal:
        hostfile.add(path, [twin], initial="")

    assert "Nothing was written" in str(refusal.value)
    assert path.read_bytes() == before, "the refused write changed the file anyway"


def test_discover_keeps_a_verified_copy_of_what_it_replaced(monkeypatch, tmp_path):
    """An append is a write to a hand-maintained file with no other copy.

    `init --force` settled this three commits before the append sites were
    looked at: the argument is a property of the FILE, and it does not care
    whether the write is an overwrite or an append. There was no `.bak`.
    """
    starter = '[hosts.local]\nkind = "local"\nport = 8188\n'
    result, path = _discover(monkeypatch, tmp_path, starter, [COMFY_WIN])

    assert result.exit_code == 0, result.output
    assert {host.name for host in _loads(path)} == {"local", "comfy-win"}
    backup = path.with_name("hosts.toml.bak")
    assert backup.exists(), "the append kept no copy of the file it changed"
    assert backup.read_text(encoding="utf-8") == starter


def test_an_append_to_a_crlf_host_list_is_still_crlf(tmp_path):
    """`read_text` opens in universal newline mode and strips the `\r` first.

    `hostfile.add` decides the line ending from the string it read and writes the
    WHOLE file back, so reading a CRLF host list that way converts it to LF on
    the first `discover`. Nothing refuses it and nothing reports it — LF parses
    perfectly well — and git then shows every line of a hand-maintained file as
    changed. The rest of `hostfile` goes to some length over exactly this; an
    append that reads the file wrongly undoes all of it in one line.
    """
    from comfy_qa import hostfile

    path = tmp_path / "hosts.toml"
    path.write_bytes(MINE.replace("\n", "\r\n").encode("utf-8"))
    box = parse(dict(COMFY_WIN, name="comfy-linux"), "p")

    hostfile.add(path, [to_toml(box, 8191)], initial="")

    data = path.read_bytes()
    assert b"[hosts.comfy-linux]" in data
    assert data.count(b"\r\n") == data.count(b"\n"), "a lone LF reached a CRLF file"
