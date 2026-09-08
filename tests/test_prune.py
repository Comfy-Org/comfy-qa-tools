"""`discover --prune` removes an entry Google says is gone, and never one it could not ask about.

Measured on Ali's real host list: **8 entries, 4 real machines.** Three of the
five ghosts arrived without this tool being involved — deleted in the console,
deleted by a colleague, deleted with raw gcloud — and one arrived *because* of it,
a `move` renaming the source box it left behind. `discover` has always
reconciled the host list against the project in one direction. It now does both,
which is one command doing one job rather than two commands splitting it.

**A stale entry is not untidiness.** `create` refuses a name an entry holds and
hands out ports from the same list, so a ghost reserves both for a machine that
does not exist — `delete` already says exactly this, and then tells you to edit
the file by hand. And target-by-description resolves against the host list: on
that real list `linux` matched 4, `l4` matched 6 and `windows` matched 2, so all
three refused as ambiguous, with the ambiguity entirely ghosts.

**The asymmetry is the whole design and it is what the second test pins.** A
listing that SUCCEEDED and does not contain the instance is Google saying the box
is gone. A listing that FAILED is nobody having asked. Only the first may remove
anything: a check that refutes is not a check that confirms, and an entry deleted
because the network was down is unrecoverable — `hosts.toml` is hand-maintained,
has no other copy, and is the file `init --force` was overwriting outside the
guarded path until tonight.

That distinction did not exist this morning. `instance_statuses` returned one word
for both, and `d7a873b` separated them so `list --live` could stop saying
`unknown` about a deleted box. This is what that separation was for.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from comfy_qa import gcloud as gcloud_module
from comfy_qa.gcloud import GcloudError
from comfy_qa.host import app

# Two projects on purpose. A host list may name several, and reconciling
# everything against the one gcloud happens to be pointed at would call every
# host on the others a ghost — and delete them.
HOSTS = """\
[hosts.local]
kind = "local"
port = 8188

[hosts.comfy-win]
kind         = "gce"
os           = "Windows Server 2022"
gpu          = "L4"
gce_instance = "comfy-win"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8190

[hosts.comfy-linux-2]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux-2"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8191

[hosts.comfy-linux-us-central1-c]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux-us-central1-c"
gce_zone     = "us-central1-c"
gce_project  = "proj"
port         = 8192

[hosts.comfy-quiet]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-quiet"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8194

[hosts.comfy-elsewhere]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "T4"
gce_instance = "comfy-elsewhere"
gce_zone     = "europe-west4-a"
gce_project  = "other-proj"
port         = 8193
"""

# Only comfy-win is real. comfy-linux-2 and comfy-linux-us-central1-c are the
# ghosts — the second is the shape a `move` leaves behind, named for the zone the
# source was in.
REAL = [
    {"name": "comfy-win", "status": "RUNNING",
     "zone": "https://x/projects/proj/zones/us-central1-a"},
    # There, and Google said nothing about what it is doing. That is
    # UNKNOWN_STATE — not knowing — and it is the answer `d7a873b` separated from
    # GONE. A prune that keys on "anything that is not a state I recognise" would
    # delete this one.
    {"name": "comfy-quiet",
     "zone": "https://x/projects/proj/zones/us-central1-a"},
]
ELSEWHERE = [{"name": "comfy-elsewhere", "status": "TERMINATED",
              "zone": "https://x/projects/other-proj/zones/europe-west4-a"}]


def cloud(*, listings=None, fail_projects=()):
    """A Gcloud answering `instances list` per project, and failing named ones."""
    calls: list[str] = []

    def runner(args, mode):
        key = " ".join(args)
        calls.append(key)
        if key.startswith("config get-value project") or key == "config get-value core/project":
            return "proj"
        if key.startswith("compute instances list"):
            project = key.rsplit("--project=", 1)[-1].split()[0]
            if project in fail_projects:
                raise GcloudError(f"could not reach Google Cloud ({project})")
            return (listings or {}).get(project, [])
        raise AssertionError(f"unexpected: {key}")

    gc = gcloud_module.Gcloud(runner=runner)
    gc.calls = calls  # type: ignore[attr-defined]
    return gc


@pytest.fixture
def run(tmp_path, monkeypatch):
    """The real CLI against a throwaway host list. Never the user's own file."""
    def invoke(*args, gc, declared=HOSTS, input=None):
        path = tmp_path / "hosts.toml"
        path.write_text(declared, encoding="utf-8")
        monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: gc)
        result = CliRunner().invoke(
            app, ["discover", *args, "--config", str(path)], input=input)
        result.hosts = path.read_text(encoding="utf-8")  # type: ignore[attr-defined]
        return result
    return invoke


def _names(text: str) -> set[str]:
    from comfy_qa.hostfile import declared as declared_in

    return declared_in(text)


# --- 1. Google says gone: the entry goes -------------------------------------


def test_an_entry_google_says_is_gone_is_named_and_removed(run):
    gc = cloud(listings={"proj": REAL, "other-proj": ELSEWHERE})
    result = run("--prune", "--yes", gc=gc)

    assert result.exit_code == 0, result.output
    assert "comfy-linux-2" in result.output and "comfy-linux-us-central1-c" in result.output, (
        "every entry being removed has to be named; a count is not something "
        "anyone can check against a file with no way back"
    )
    assert _names(result.hosts) == {"local", "comfy-win", "comfy-quiet", "comfy-elsewhere"}


def test_the_box_a_move_left_behind_is_the_case_it_has_to_cover(run):
    """`comfy-linux-us-central1-c` is a renamed source entry, which is the most
    common way a ghost appears — and it appears by this tool's own action."""
    result = run("--prune", "--yes", gc=cloud(listings={"proj": REAL, "other-proj": ELSEWHERE}))

    assert "comfy-linux-us-central1-c" not in _names(result.hosts)


def test_a_host_on_another_project_is_judged_by_its_own_project(run):
    """`comfy-elsewhere` is absent from `proj` and present on `other-proj`.

    Reconciling against `current_project()` would call it a ghost and delete it,
    which is why this reads one listing per DECLARED project.
    """
    result = run("--prune", "--yes", gc=cloud(listings={"proj": REAL, "other-proj": ELSEWHERE}))

    assert "comfy-elsewhere" in _names(result.hosts)


def test_a_project_with_no_boxes_at_all_still_prunes(run):
    """An empty listing is the strongest statement there is that the entries
    naming that project are stale, and the command used to return before looking."""
    result = run("--prune", "--yes", gc=cloud(listings={"proj": [], "other-proj": ELSEWHERE}))

    assert _names(result.hosts) == {"local", "comfy-elsewhere"}


# --- 2. nobody could ask: the entry stays. This is the one that matters -------


def test_a_host_on_a_project_that_could_not_be_read_is_left_alone(run):
    """The asymmetry, stated as a test: refuting is not confirming.

    `comfy-elsewhere` is the only entry on `other-proj`, and an unreadable
    listing treated as an empty one would report it gone and delete it. That is a
    host list destroyed because the network was down, and this file has no way
    back.
    """
    gc = cloud(listings={"proj": REAL}, fail_projects=("other-proj",))
    result = run("--prune", "--yes", gc=gc)

    assert "comfy-elsewhere" in _names(result.hosts)


def test_an_unreadable_project_is_said_out_loud_and_not_counted_as_clean(run):
    """Silence would read as "nothing else to remove", on no evidence."""
    gc = cloud(listings={"proj": REAL}, fail_projects=("other-proj",))
    result = run("--prune", "--yes", gc=gc)

    assert "other-proj could not be listed" in result.output
    assert "left alone" in result.output


def test_one_unreadable_project_does_not_hide_the_ghosts_on_another(run):
    """Separate questions, separate answers: `other-proj` failing says nothing
    about what `proj` does or does not have, so its ghosts still go."""
    gc = cloud(listings={"proj": REAL}, fail_projects=("other-proj",))
    result = run("--prune", "--yes", gc=gc)

    assert _names(result.hosts) == {"local", "comfy-win", "comfy-quiet", "comfy-elsewhere"}


def test_the_whole_command_refuses_when_its_own_project_cannot_be_listed(run):
    """Unchanged, and it is the safe shape: `discover` cannot discover without
    that read, so it exits 2 and writes nothing rather than pruning half-blind."""
    gc = cloud(listings={"other-proj": ELSEWHERE}, fail_projects=("proj",))
    result = run("--prune", "--yes", gc=gc)

    assert result.exit_code == 2
    assert _names(result.hosts) == _names(HOSTS)


def test_a_box_google_answered_about_without_a_state_is_not_gone(run):
    """`unknown` is not `gone`, and this is the whole reason they were separated.

    `comfy-quiet` is ON the project — the listing carries it — and Google said
    nothing about what it is doing. Not knowing what a box is doing is not
    evidence that it does not exist, and a prune that cannot tell the two apart
    deletes an entry for a machine that may be billing right now.
    """
    result = run("--prune", "--yes", gc=cloud(listings={"proj": REAL, "other-proj": ELSEWHERE}))

    assert "comfy-quiet" in _names(result.hosts)
    assert "comfy-quiet" not in result.output, "it was named as a thing to remove"


# --- 3. never silently, and never without --prune ----------------------------


def test_nothing_is_removed_without_the_flag(run):
    result = run(gc=cloud(listings={"proj": REAL}))

    assert _names(result.hosts) == _names(HOSTS)
    assert "not on the project" not in result.output


def test_declining_the_confirmation_removes_nothing(run):
    result = run("--prune", gc=cloud(listings={"proj": REAL, "other-proj": ELSEWHERE}),
                 input="n\n")

    assert _names(result.hosts) == _names(HOSTS), "answering no still rewrote the file"
    assert "nothing removed" in result.output


def test_accepting_the_confirmation_removes_them(run):
    result = run("--prune", gc=cloud(listings={"proj": REAL, "other-proj": ELSEWHERE}),
                 input="y\n")

    assert _names(result.hosts) == {"local", "comfy-win", "comfy-quiet", "comfy-elsewhere"}


def test_a_dry_run_names_them_and_writes_nothing(run):
    result = run("--prune", "--dry-run",
                 gc=cloud(listings={"proj": REAL, "other-proj": ELSEWHERE}))

    assert "comfy-linux-2" in result.output
    assert "--dry-run: nothing written" in result.output
    assert _names(result.hosts) == _names(HOSTS)


# --- 4. the write goes through the guarded path ------------------------------


def test_the_removal_leaves_a_backup_and_a_file_that_still_loads(run, tmp_path):
    """`hostfile.apply`, not `write_text`: atomic replace, a copy kept beside it,
    and read back with the real loader afterwards."""
    import tomllib

    from comfy_qa.config import parse

    result = run("--prune", "--yes", gc=cloud(listings={"proj": REAL, "other-proj": ELSEWHERE}))

    assert (tmp_path / "hosts.toml.bak").exists(), "no copy was kept"
    assert {host.name for host in parse(tomllib.loads(result.hosts))} == {
        "local", "comfy-win", "comfy-quiet", "comfy-elsewhere"}


def test_the_local_host_is_never_a_candidate(run):
    """It has no instance to be absent from anything."""
    result = run("--prune", "--yes", gc=cloud(listings={"proj": [], "other-proj": []}))

    assert "local" in _names(result.hosts)
