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


# Google's own 404, which is the sentence `classify` reads to answer NOT_FOUND.
# The quoted resource path is the part that matters: `gcloud` writes "External IP
# address was not found" as an ordinary warning on a healthy `ssh`, so the
# classifier matches `' was not found` and an invented paraphrase without the
# quote silently matches nothing — which reads as the tool refusing to prune.
NO_SUCH_INSTANCE = (
    "ERROR: (gcloud.compute.instances.describe) Could not fetch resource:\n"
    " - The resource 'projects/{project}/zones/{zone}/instances/{name}' was not "
    "found\n"
)


def cloud(*, listings=None, fail_projects=(), describes=None, fail_describes=()):
    """A Gcloud answering `instances list` per project, and `describe` per box.

    `describe` answers from the same listings by default, which is the ordinary
    world: what the project holds is what a direct read finds. The two extra
    arguments exist for the cases a listing CANNOT express, and they are the
    whole reason the second read is worth making —

      `describes`      names an instance a direct read finds even though the
                       listing did not carry it. A listing that came back short.
      `fail_describes` names one whose check does not get through at all.

    Anything else still raises, and that is the promise this fake is for: a
    `--dry-run` may make no billable call, and a fake that quietly answers None
    to a method nobody scripted is how that promise passes a test while being
    broken.
    """
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
        if key.startswith("compute instances describe"):
            name = args[3]
            zone = key.rsplit("--zone=", 1)[-1].split()[0]
            project = key.rsplit("--project=", 1)[-1].split()[0]
            if name in fail_describes:
                raise GcloudError(
                    f"could not reach Google Cloud ({name})",
                    raw=f"could not reach Google Cloud ({name})")
            for box in (describes or {}).get(project, []):
                if box.get("name") == name:
                    return box
            for box in (listings or {}).get(project, []):
                if box.get("name") == name and box.get("zone", "").endswith(zone):
                    return box
            raw = NO_SUCH_INSTANCE.format(project=project, zone=zone, name=name)
            raise GcloudError("Could not fetch resource", raw=raw,
                              kind=gcloud_module.classify(raw))
        raise AssertionError(f"unexpected: {key}")

    gc = gcloud_module.Gcloud(runner=runner)
    gc.calls = calls  # type: ignore[attr-defined]
    return gc


def _describes(gc) -> list[str]:
    """The instance names this run put to a per-instance `describe`."""
    return [call.split()[3] for call in gc.calls
            if call.startswith("compute instances describe")]


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


# --- 5. absent is a fact about the NAME, not about (name, zone) --------------
#
# The most expensive defect this command has had, and the one it was written to
# make impossible. `instance_statuses` keyed its listing on `(name, zone)` and
# answered GONE for any miss — so an entry whose zone was wrong was announced as
# "not on the project any more — the box is gone, the entry is not", removed, and
# the command exited 0, while the same listing it had just read positively held
# that machine RUNNING one zone over. The L4 went on billing with nothing left in
# the host list naming it, so `down` and `list` could no longer reach it.
#
# A hand-typed zone reaches this. So does a box recreated in another zone from
# the console, and a `move` that did not finish — which is to say the entries most
# likely to have the wrong zone are exactly the ones this used to delete.

# The same `comfy-win` the host list declares in us-central1-a, running in -b.
MOVED = [{"name": "comfy-win", "status": "RUNNING",
          "zone": "https://x/projects/proj/zones/us-central1-b"}]


def test_a_live_box_in_a_zone_the_entry_got_wrong_is_never_pruned(run):
    """The entry is wrong and the box exists. Only one of those may be acted on."""
    result = run("--prune", "--yes",
                 gc=cloud(listings={"proj": MOVED, "other-proj": ELSEWHERE}))

    assert "comfy-win" in _names(result.hosts), (
        "an entry naming a machine that is running was removed — the box goes on "
        "billing and nothing in the host list can reach it"
    )


def test_the_wrong_zone_is_reported_as_a_wrong_zone_and_not_as_a_gone_box(run):
    """Removing it was half the defect. Saying it was gone was the other half:
    the sentence was false, and it was the only thing the user was told."""
    result = run("--prune", "--yes",
                 gc=cloud(listings={"proj": MOVED, "other-proj": ELSEWHERE}))

    assert "not in the zone the entry gives" in result.output
    assert "us-central1-a" in result.output, "the zone the entry gets wrong"
    assert "correct gce_zone by hand" in result.output
    # The ghost block is still printed for the real ghosts, and `comfy-win` must
    # not be inside it.
    gone_block = result.output.split("not on the project any more")[1]
    assert "comfy-win" not in gone_block.split("not in the zone")[0]


def test_the_real_ghosts_still_go_in_the_same_run(run):
    """The two answers are separated, not merged into a refusal to do anything."""
    result = run("--prune", "--yes",
                 gc=cloud(listings={"proj": MOVED, "other-proj": ELSEWHERE}))

    assert "comfy-linux-2" not in _names(result.hosts)
    assert "comfy-linux-us-central1-c" not in _names(result.hosts)


def test_a_name_on_another_project_does_not_rescue_an_entry():
    """`ELSEWHERE` is scoped to the entry's OWN project, exactly as `GONE` is.

    A host list may name several projects, and a `comfy-win` on one of them says
    nothing about a `comfy-win` on another — reading it as a rescue would keep
    every ghost alive as soon as any project had a box by that name.
    """
    from comfy_qa.gcloud import ELSEWHERE as MISPLACED, GONE as ABSENT, Gcloud

    def runner(args, mode):
        key = " ".join(args)
        project = key.rsplit("--project=", 1)[-1].split()[0]
        return {"proj": [], "other-proj": MOVED}[project]

    states = Gcloud(runner=runner).instance_statuses(
        [("comfy-win", "us-central1-a", "proj")])

    assert states[("comfy-win", "us-central1-a", "proj")] == ABSENT
    assert states[("comfy-win", "us-central1-a", "proj")] != MISPLACED


# --- 6. gcloud saying nothing is not the project holding nothing -------------


def _silent_gcloud(monkeypatch):
    """A real `Gcloud` whose `instances list` exits 0 and prints not one byte.

    Deliberately below the runner seam every other test in this file uses. The
    seam hands back a Python list, so it cannot express "the process printed
    nothing" at all — which is exactly why this defect lived under a file with
    fourteen tests in it.

    The project read still answers, and that is not a detail. A `Gcloud` silent
    on EVERY call answers None to `config get-value project` too, so `discover`
    stops at "no project set" and never reaches the prune — a test written that
    way passes with the defect fully present and proves nothing. Only the listing
    is silent here.
    """
    class Answer:
        def __init__(self, stdout):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def ran(cmd, **kwargs):
        return Answer("" if "instances" in cmd else "proj\n")

    monkeypatch.setattr(gcloud_module.subprocess, "run", ran)
    monkeypatch.setattr(gcloud_module.Gcloud, "require", lambda self: "/usr/bin/true")
    return gcloud_module.Gcloud()


def test_an_empty_answer_from_gcloud_is_not_an_empty_project(monkeypatch):
    """`run` returns None for exit-0-with-no-output on purpose: `instances list`
    prints `[]` for a project with nothing in it, so nothing printed is a reply
    that never arrived. `or []` erased that, and every declared host on the
    project became a ghost."""
    gc = _silent_gcloud(monkeypatch)

    with pytest.raises(GcloudError) as refusal:
        gc.list_instances("proj")

    assert "printed nothing at all" in str(refusal.value)


def test_a_silent_gcloud_prunes_nothing_at_all(tmp_path, monkeypatch):
    """The end of the same thread, through the real command. Stubbed at the
    subprocess, this used to remove every entry in the file and exit 0."""
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    gc = _silent_gcloud(monkeypatch)
    monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: gc)

    result = CliRunner().invoke(
        app, ["discover", "--prune", "--yes", "--config", str(path)])

    assert _names(path.read_text(encoding="utf-8")) == _names(HOSTS), (
        "a host list was destroyed on an answer nobody read"
    )
    assert not (tmp_path / "hosts.toml.bak").exists(), "it wrote at all"


# --- 7. `.bak` is the file as it was before the command ran ------------------


def test_a_run_that_adds_and_prunes_leaves_the_pre_run_file_in_bak(run, tmp_path):
    """Two writes, one backup, and it is the state somebody would want back.

    `add` and `_forget` each went through `hostfile.apply`, so each took its own
    copy and the second overwrote the first. After a run that adopted one box and
    pruned another, `hosts.toml.bak` held the state BETWEEN them — the additions
    already in, the ghosts still there — which existed for milliseconds and is not
    what anybody reaching for `.bak` is looking for. The pre-run file survived
    only in `backups/`, under a timestamp, which is the copy nobody knows to look
    for.
    """
    adopted = REAL + [{"name": "comfy-new", "status": "RUNNING",
                       "zone": "https://x/projects/proj/zones/us-central1-a"}]
    result = run("--prune", "--yes",
                 gc=cloud(listings={"proj": adopted, "other-proj": ELSEWHERE}))

    assert "comfy-new" in _names(result.hosts), "the run has to have added one"
    assert "comfy-linux-2" not in _names(result.hosts), "and pruned one"

    kept = (tmp_path / "hosts.toml.bak").read_text(encoding="utf-8")
    assert kept == HOSTS, (
        "hosts.toml.bak is not the file this command started with — it holds the "
        "state left by the command's own earlier write"
    )
    assert "comfy-new" not in _names(kept)


def test_the_superseded_copies_are_still_archived(run, tmp_path):
    """One backup per command is not one backup ever: a SECOND run still archives
    the copy it supersedes, which is the guarantee `_archive` exists for."""
    listings = {"proj": REAL, "other-proj": ELSEWHERE}
    run("--prune", "--yes", gc=cloud(listings=listings))
    # A DIFFERENT starting file for the second run, because a copy identical to
    # the one it supersedes is not archived — there is nothing in it to lose.
    run("--prune", "--yes", gc=cloud(listings=listings),
        declared="# a note somebody wrote by hand\n" + HOSTS)

    archived = sorted((tmp_path / "backups").glob("hosts.toml.*.bak"))
    assert archived, "the superseded copy was destroyed rather than archived"


# --- 8. a permission problem is a read that failed, not a project with nothing


def test_a_listing_refused_for_permissions_prunes_nothing_on_that_project(run):
    """The shape a narrowed role or a service-account credential actually takes.

    Compute Engine authorises `compute.instances.list` on the PROJECT, so a
    credential that may not see the instances is refused the whole call rather
    than handed a shorter list — the 403 arrives as a `GcloudError`, the project
    is reported as one that could not be listed, and every entry naming it stays.
    That is the same door `--prune` already refuses to walk through for a network
    failure, and it is pinned here because the reasoning above is the only thing
    standing between a narrowed role and a deleted host list.
    """
    def runner(args, mode):
        key = " ".join(args)
        if key.startswith("config get-value"):
            return "proj"
        raise GcloudError(
            "Required 'compute.instances.list' permission for 'projects/proj'",
            raw="Required 'compute.instances.list' permission for 'projects/proj'")

    gc = gcloud_module.Gcloud(runner=runner)
    result = run("--prune", "--yes", gc=gc)

    assert _names(result.hosts) == _names(HOSTS), "a refusal was read as an absence"
    assert result.exit_code == 2, "discover cannot discover without that read"


# --- 9. absence is CONFIRMED before it is acted on ---------------------------
#
# A name failing to appear in a bulk `instances list` is an inference from a
# listing, and it is only ever as complete as that listing was. This command's
# own docstring promises it removes what Google POSITIVELY says is absent, and a
# non-appearance is not Google saying anything — so every candidate is put to a
# `describe` of its own before anything is written, and only a flat not-found
# removes an entry.
#
# The point of doing it this way is that it does not rest on any theory of how a
# listing could come back short. It removes the class: whatever the listing did
# or did not carry, the entry goes only on a statement about that machine.


def test_every_entry_removed_was_confirmed_gone_one_at_a_time(run):
    """The two ghosts are each asked about by name before either is removed."""
    gc = cloud(listings={"proj": REAL, "other-proj": ELSEWHERE})
    result = run("--prune", "--yes", gc=gc)

    assert sorted(_describes(gc)) == ["comfy-linux-2", "comfy-linux-us-central1-c"]
    assert _names(result.hosts) == {"local", "comfy-win", "comfy-quiet", "comfy-elsewhere"}


def test_nothing_is_asked_about_twice_and_nothing_extra_is_asked_about(run):
    """The cost is one call per CANDIDATE, not one per entry. `comfy-win` and
    `comfy-quiet` are in the listing, so neither is worth a second read."""
    gc = cloud(listings={"proj": REAL, "other-proj": ELSEWHERE})
    run("--prune", "--yes", gc=gc)

    assert "comfy-win" not in _describes(gc)
    assert "comfy-quiet" not in _describes(gc)
    assert "comfy-elsewhere" not in _describes(gc)


def test_a_host_list_with_nothing_stale_in_it_makes_no_extra_calls(run):
    """A clean host list pays nothing for this. Everything declared on `proj` is
    in the listing, so there is no candidate and no second read."""
    everything = REAL + [
        {"name": name, "status": "RUNNING",
         "zone": f"https://x/projects/proj/zones/{zone}"}
        for name, zone in (("comfy-linux-2", "us-central1-a"),
                           ("comfy-linux-us-central1-c", "us-central1-c"))]
    gc = cloud(listings={"proj": everything, "other-proj": ELSEWHERE})
    run("--prune", "--yes", gc=gc)

    assert _describes(gc) == []


def test_a_box_the_listing_missed_is_found_by_the_check_and_kept(run):
    """The case the second read exists for, and the one no listing can rule out.

    `comfy-linux-2` is absent from the listing and answers a direct read. That is
    a listing that came back short — for whatever reason it came back short — and
    the entry is the only thing in the host list naming a machine that is there.
    """
    gc = cloud(listings={"proj": REAL, "other-proj": ELSEWHERE},
               describes={"proj": [{"name": "comfy-linux-2", "status": "RUNNING",
                                    "zone": "https://x/projects/proj/zones/us-central1-a"}]})
    result = run("--prune", "--yes", gc=gc)

    assert "comfy-linux-2" in _names(result.hosts), (
        "an entry was removed for a machine the project describes on request"
    )
    assert "did not confirm they are gone" in result.output
    assert "the listing that did not carry it was incomplete" in result.output
    # And the genuine ghost beside it still goes: this narrows what is removed,
    # it does not stop the command doing its job.
    assert "comfy-linux-us-central1-c" not in _names(result.hosts)


def test_a_check_that_could_not_be_made_removes_nothing(run):
    """Refuting is not confirming, one layer in. The listing said absent and
    nobody could get a second answer, so the entry stays and the run says why."""
    gc = cloud(listings={"proj": REAL, "other-proj": ELSEWHERE},
               fail_describes=("comfy-linux-2",))
    result = run("--prune", "--yes", gc=gc)

    assert "comfy-linux-2" in _names(result.hosts)
    assert "the check could not be made" in result.output
    assert "comfy-linux-us-central1-c" not in _names(result.hosts)


def test_a_check_refused_for_permissions_is_not_a_confirmed_absence(run):
    """The ordering in `_SIGNS`, stated as behaviour rather than as a comment.

    Google answers a resource you may not see with a 403 naming the permission,
    never a 404 — so `DENIED` is matched before `NOT_FOUND`. Reverse those two and
    a narrowed role becomes a confirmed absence, which is the one mistake this
    command cannot take back.
    """
    denied = ("ERROR: (gcloud.compute.instances.describe) Could not fetch "
              "resource:\n - Required 'compute.instances.get' permission for "
              "'projects/proj/zones/us-central1-a/instances/comfy-linux-2'\n")

    def runner(args, mode):
        key = " ".join(args)
        if key.startswith("config get-value"):
            return "proj"
        if key.startswith("compute instances list"):
            project = key.rsplit("--project=", 1)[-1].split()[0]
            return {"proj": REAL, "other-proj": ELSEWHERE}[project]
        if key.startswith("compute instances describe"):
            raise GcloudError("Could not fetch resource", raw=denied,
                              kind=gcloud_module.classify(denied))
        raise AssertionError(f"unexpected: {key}")

    result = run("--prune", "--yes", gc=gcloud_module.Gcloud(runner=runner))

    assert _names(result.hosts) == _names(HOSTS), (
        "a permission refusal was read as Google confirming the box is gone"
    )
    assert "the check could not be made" in result.output


def test_a_dry_run_still_confirms_before_it_names_anything(run):
    """`--dry-run` promises to write nothing, not to guess. A preview that names
    an entry the real run would refuse to remove is a preview of the wrong run."""
    gc = cloud(listings={"proj": REAL, "other-proj": ELSEWHERE},
               describes={"proj": [{"name": "comfy-linux-2", "status": "RUNNING",
                                    "zone": "https://x/projects/proj/zones/us-central1-a"}]})
    result = run("--prune", "--dry-run", gc=gc)

    assert "--dry-run: nothing written" in result.output
    assert _names(result.hosts) == _names(HOSTS)
    removing = result.output.split("the box is gone, the entry is not:")[1]
    assert "comfy-linux-us-central1-c" in removing
    assert "comfy-linux-2" not in removing.split("did not confirm")[0]


def test_the_count_of_checks_is_said_before_they_are_made(run):
    """Each one is a gcloud process. Several silent seconds in a command that has
    printed nothing yet reads as a hang, which is the rule the whole tool keeps."""
    gc = cloud(listings={"proj": REAL, "other-proj": ELSEWHERE})
    result = run("--prune", "--yes", gc=gc)

    assert "checking 2 entries against the project one at a time" in result.output


# --- 10. what Google's own 404 looks like, and what it does not --------------


def test_googles_missing_resource_is_classified_as_not_found():
    from comfy_qa.gcloud import NOT_FOUND, classify

    assert classify(NO_SUCH_INSTANCE.format(
        project="proj", zone="us-central1-a", name="comfy-linux-2")) == NOT_FOUND


def test_the_iap_warning_is_not_a_missing_machine():
    """`gcloud` writes this on a perfectly healthy `ssh`. Matching a bare "was
    not found" would let a failure carrying it confirm a machine as gone."""
    from comfy_qa.gcloud import NOT_FOUND, classify

    assert classify(
        "External IP address was not found; defaulting to IAP tunneling."
    ) != NOT_FOUND


def test_a_permission_refusal_is_denied_and_not_not_found():
    """Both sentences can appear in one refusal, so the order in `_SIGNS` decides
    it. This is the assertion that pins the order."""
    from comfy_qa.gcloud import DENIED, classify

    assert classify(
        " - Required 'compute.instances.get' permission for "
        "'projects/p/zones/z/instances/n'\n"
        " - The resource 'projects/p/zones/z/instances/n' was not found\n"
    ) == DENIED


# --- 11. not-found about WHAT --------------------------------------------
#
# `describe` answers not-found about whatever it could not reach, and only one of
# those answers is about the machine. All three classify as NOT_FOUND, correctly
# — a not-found is a not-found — so the kind alone cannot decide a removal, and
# the resource path in Google's own sentence is what separates them.
#
# The one that costs a fleet is the middle one. `gce_project` is hand-typed into a
# hand-maintained file; mistype it and every entry naming it is answered "that
# project does not exist", each of those reads as a confirmed absent instance, and
# one `y` removes the only record of machines still running on the project that
# was meant. Reproduced end to end before the guard: three entries removed,
# announced as "the box is gone", exit 0.
#
# Worse on this path than anywhere else, because the second read exists to stop
# unverified removals — so a second read that endorses one hands back a run
# saying every entry was checked individually. And it was. It answered about
# something else.

NO_SUCH_PROJECT = (
    "ERROR: (gcloud.compute.instances.describe) Could not fetch resource:\n"
    " - The resource 'projects/no-such-project-comfy-qa-9271' was not found\n"
)
NO_SUCH_ZONE = (
    "ERROR: (gcloud.compute.instances.describe) Could not fetch resource:\n"
    " - The resource 'projects/proj/zones/us-centra1-a' was not found\n"
)
NO_PERMISSION = (
    "ERROR: (gcloud.compute.instances.describe) Could not fetch resource:\n"
    " - Required 'compute.instances.get' permission for "
    "'projects/bigquery-public-data/zones/us-central1-a/instances/denied'\n"
)


def test_the_three_sentences_a_describe_can_come_back_with():
    """Live wordings, from the three calls that produce them."""
    from comfy_qa.gcloud import DENIED, NOT_FOUND, classify

    assert classify(NO_PERMISSION) == DENIED
    assert classify(NO_SUCH_PROJECT) == NOT_FOUND
    assert classify(NO_SUCH_ZONE) == NOT_FOUND
    assert classify(NO_SUCH_INSTANCE.format(
        project="proj", zone="us-central1-a", name="ghostone")) == NOT_FOUND


def test_the_resource_named_by_each_not_found_is_read_back_out():
    """The kind is the same for all three; the path is what tells them apart."""
    from comfy_qa.gcloud import _missing_resource

    assert _missing_resource(NO_SUCH_PROJECT) == "projects/no-such-project-comfy-qa-9271"
    assert _missing_resource(NO_SUCH_ZONE) == "projects/proj/zones/us-centra1-a"
    assert _missing_resource(NO_SUCH_INSTANCE.format(
        project="proj", zone="us-central1-a", name="ghostone")
    ) == "projects/proj/zones/us-central1-a/instances/ghostone"
    # A permission refusal names a resource and does not say it was not found.
    assert _missing_resource(NO_PERMISSION) == ""
    assert _missing_resource(None) == ""


@pytest.mark.parametrize("raw,confirms", [
    (NO_SUCH_INSTANCE, True),
    (NO_SUCH_PROJECT, False),
    (NO_SUCH_ZONE, False),
    (NO_PERMISSION, False),
])
def test_only_a_not_found_about_the_instance_confirms_it_is_absent(raw, confirms):
    from comfy_qa.gcloud import Gcloud, classify

    body = raw.format(project="proj", zone="us-central1-a", name="ghostone") \
        if "{project}" in raw else raw

    def runner(args, mode):
        raise GcloudError("Could not fetch resource", raw=body,
                          kind=classify(body))

    gc = Gcloud(runner=runner)
    if confirms:
        assert gc.confirms_absent("ghostone", "us-central1-a", "proj") is True
    else:
        with pytest.raises(GcloudError):
            gc.confirms_absent("ghostone", "us-central1-a", "proj")


def test_a_not_found_about_another_instance_does_not_confirm_this_one():
    """The path has to name the machine that was ASKED about, not merely be
    instance-shaped — otherwise a stray sentence about a neighbour confirms it."""
    from comfy_qa.gcloud import Gcloud, classify

    other = NO_SUCH_INSTANCE.format(project="proj", zone="us-central1-a",
                                    name="somebody-else")

    def runner(args, mode):
        raise GcloudError("Could not fetch resource", raw=other,
                          kind=classify(other))

    with pytest.raises(GcloudError):
        Gcloud(runner=runner).confirms_absent("ghostone", "us-central1-a", "proj")


def test_a_host_list_naming_a_project_that_does_not_exist_loses_nothing(run):
    """The end of the thread, through the real command.

    Every entry on the mistyped project is a candidate — the listing came back
    empty for it — and every check answers about the project. Before the guard
    this removed all of them and said the boxes were gone.
    """
    def runner(args, mode):
        key = " ".join(args)
        if key.startswith("config get-value"):
            return "proj"
        if key.startswith("compute instances list"):
            project = key.rsplit("--project=", 1)[-1].split()[0]
            return REAL if project == "proj" else []
        if key.startswith("compute instances describe"):
            raise GcloudError("Could not fetch resource", raw=NO_SUCH_PROJECT,
                              kind=gcloud_module.classify(NO_SUCH_PROJECT))
        raise AssertionError(f"unexpected: {key}")

    result = run("--prune", "--yes", gc=gcloud_module.Gcloud(runner=runner))

    assert _names(result.hosts) == _names(HOSTS), (
        "one mistyped gce_project deleted the entries for machines that exist"
    )
    assert "the box is gone" not in result.output
    assert "does not exist, which is not a statement about the instance" \
        in result.output


def test_a_mistyped_zone_is_not_a_confirmed_absence_either(run):
    """The same class one field over, and `gce_zone` is hand-typed too."""
    def runner(args, mode):
        key = " ".join(args)
        if key.startswith("config get-value"):
            return "proj"
        if key.startswith("compute instances list"):
            project = key.rsplit("--project=", 1)[-1].split()[0]
            return REAL if project == "proj" else ELSEWHERE
        if key.startswith("compute instances describe"):
            raise GcloudError("Could not fetch resource", raw=NO_SUCH_ZONE,
                              kind=gcloud_module.classify(NO_SUCH_ZONE))
        raise AssertionError(f"unexpected: {key}")

    result = run("--prune", "--yes", gc=gcloud_module.Gcloud(runner=runner))

    assert _names(result.hosts) == _names(HOSTS)
    assert "did not confirm they are gone" in result.output
