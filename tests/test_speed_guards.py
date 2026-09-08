"""Guards for the two performance fixes, so neither regresses in silence.

WHY THIS IS A TEST AND NOT A COMMENT. `never_write_to_the_real_config` in
conftest.py is autouse over every test in this suite, and it used to ask
`tmp_path_factory.mktemp` for a directory each time. `mktemp` is not `mkdir`: it
calls pytest's `make_numbered_dir`, which SCANS the whole base temp directory to
find the highest number already there. One scan per test, over a directory that
grows by one entry per test, is quadratic.

Measured on this suite, with `os.scandir` counted rather than timed (the machine
it was measured on had a load average of 37, so wall clock said nothing):

    per-test mktemp   12222 numbered-dir scans   81,306,850 entries scanned
    session root       6112 numbered-dir scans   21,986,800 entries scanned

Same run, same load, concurrently: 500s against 271s.

Nothing else would notice the revert. The suite would still pass, and it would
get slower by a couple of minutes a run, which reads as "the suite grew".
"""

from __future__ import annotations


def test_the_config_redirect_is_one_numbered_dir_for_the_whole_session(tmp_path_factory):
    """Each test's redirect sits UNDER a shared root, not beside it.

    Checked by shape rather than by count, because a count would depend on how
    many tests had already run when this one did — and it would pass if this
    file happened to be collected first, which is the one case where a guard
    that cannot fail is worse than no guard.

    With the session root:   <basetemp>/config-redirect0/<n>/hosts.toml
    With a per-test mktemp:  <basetemp>/config-redirect<n>/hosts.toml

    So the redirect's grandparent is the base temp directory in the first case
    and the base temp directory's PARENT in the second.
    """
    from comfy_qa import config

    redirect = config.DEFAULT_CONFIG_PATH.parent
    base = tmp_path_factory.getbasetemp()

    assert redirect.parent.parent == base, (
        f"the config redirect is at {redirect}, directly under the pytest base "
        f"temp directory {base}. That means it was made by "
        f"`tmp_path_factory.mktemp` — one numbered directory per test, and "
        f"`make_numbered_dir` scans the whole base directory every time. It "
        f"cost this suite 59 million extra `os.scandir` entries and roughly "
        f"double the wall clock. Make the root once, session-scoped, and give "
        f"each test a plain `mkdir` underneath it."
    )


def test_only_one_config_redirect_root_is_ever_made(tmp_path_factory):
    """The direct count, which holds once anything else has run before it.

    Weaker than the shape check above on its own — it would pass trivially in a
    session where this was the first test — and kept because when it does fail
    it names the problem in one number.
    """
    # `not p.is_symlink()`: `make_numbered_dir` also drops a
    # `config-redirectcurrent` symlink beside the directory it makes, which is
    # one name, not one directory.
    roots = [p for p in tmp_path_factory.getbasetemp().iterdir()
             if p.name.startswith("config-redirect") and not p.is_symlink()]
    assert len(roots) <= 1, (
        f"{len(roots)} `config-redirect` numbered directories exist. There must "
        f"be one for the whole session; one per test is quadratic in the number "
        f"of tests."
    )


def test_every_region_is_probed_in_one_wave():
    """Latency probing must not queue behind a pool narrower than the region list.

    Counted, not timed. A timing assertion here would be flaky on a loaded
    machine and would be deleted the first time it went red, which is how the
    eight-worker pool would come back.

    What is counted is the high-water mark of probes in flight. Every probe is
    asleep on a TCP connect, so a pool of 8 against a live project's 43 regions
    is six sequential waves for no reason: measured at a 250 ms round trip, 1.52s
    against 0.26s. The case that pays for this guard is the degraded one — an
    unreachable region burns the full `PROBE_TIMEOUT`, and `_worth_keeping`
    refuses to cache it, so a bad network is charged on EVERY create rather than
    once a week: 12.02s against 2.01s with all 43 timing out.
    """
    import threading

    from comfy_qa import zones

    regions = [f"region-{i}" for i in range(43)]
    in_flight = 0
    high_water = 0
    lock = threading.Lock()
    gate = threading.Barrier(len(regions), timeout=5)

    def probe(region, timeout=None):
        nonlocal in_flight, high_water
        with lock:
            in_flight += 1
            high_water = max(high_water, in_flight)
        # Every probe waits for every other one, so the high-water mark can only
        # reach the full count if the pool is that wide. A narrower pool cannot
        # fill the barrier and it breaks — swallowed here rather than raised, so
        # the assertion below is what reports the failure instead of a
        # `BrokenBarrierError` from inside a worker thread.
        try:
            gate.wait()
        except threading.BrokenBarrierError:
            pass
        with lock:
            in_flight -= 1
        return 1.0

    zones.measure(regions, probe=probe)

    assert high_water == len(regions), (
        f"only {high_water} of {len(regions)} region probes were ever in flight "
        f"at once. Each one is a thread asleep on a socket, so a narrower pool "
        f"buys nothing and costs a wave: 8 workers made 43 regions take 1.52s "
        f"instead of 0.26s, and 12.02s instead of 2.01s when none of them "
        f"answered. Raise `zones.PROBE_WORKERS`."
    )


# --- one round trip per project, not one per box --------------------------


def _counting_gcloud(instances):
    """A Gcloud that records the argv of every call it is asked to make."""
    from comfy_qa.gcloud import Gcloud

    calls = []

    def runner(args, parse_json):
        calls.append(" ".join(args))
        if " ".join(args).startswith("compute instances list"):
            project = " ".join(args).rsplit("--project=", 1)[-1].split()[0]
            return [i for i in instances if i["_project"] == project]
        raise AssertionError(f"unexpected gcloud call: {' '.join(args)}")

    return Gcloud(runner=runner), calls


def _box(name, zone, project, status="RUNNING"):
    return {"name": name, "status": status, "_project": project,
            "zone": f"https://x/projects/{project}/zones/{zone}"}


def test_surveying_many_boxes_is_one_call_per_project():
    """`running_elsewhere` and `list --live` must not scale with the host list.

    Counted, not timed — the count IS the cost here, because each call was a
    separate `gcloud` process run serially. Measured at the `Gcloud.runner` seam
    before the change: `list --live` made exactly one `compute instances
    describe` per declared cloud box (1/2/4/8 boxes -> 1/2/4/8 calls), and
    `switch` surveyed through the same shape.

    `instances list` already returns every instance on a project WITH its status,
    which is what `discover`, `create` and `relocate` have always used.
    """
    boxes = [_box(f"box{i}", "us-central1-a", "proj") for i in range(8)]
    gc, calls = _counting_gcloud(boxes)

    states = gc.instance_statuses(
        [(b["name"], "us-central1-a", "proj") for b in boxes])

    assert len(calls) == 1, (
        f"eight boxes on one project cost {len(calls)} gcloud calls: {calls}. "
        f"One `instances list` per project carries all of them, and each extra "
        f"call is a whole gcloud process run serially."
    )
    assert all(states[(b["name"], "us-central1-a", "proj")] == "RUNNING"
               for b in boxes)


def test_boxes_on_different_projects_still_each_get_read():
    """One call PER PROJECT, not one call overall.

    A host list may legitimately name several `gce_project`s — `hosts.toml`
    carries the project per host precisely because they can differ. Collapsing
    the read to a single call would report every host on the other projects as
    unknown, which for `running_elsewhere` means a running box silently not
    counted, not stopped, and left billing.
    """
    boxes = [_box("a", "z1", "proj-one"), _box("b", "z2", "proj-two")]
    gc, calls = _counting_gcloud(boxes)

    states = gc.instance_statuses([("a", "z1", "proj-one"), ("b", "z2", "proj-two")])

    assert len(calls) == 2, calls
    assert states[("a", "z1", "proj-one")] == "RUNNING"
    assert states[("b", "z2", "proj-two")] == "RUNNING", (
        "a box on the second project was not read. One call per DISTINCT "
        "project; do not collapse this to one call overall."
    )


def test_a_box_that_is_not_on_the_project_reads_as_not_knowing():
    """Absent is `UNKNOWN_STATE`, which is not TERMINATED and so counts as running.

    That is the safe direction and it matches what the per-box `describe` did: a
    read it could not make was never taken as "the box is off". A box wrongly
    counted gets stopped; a box wrongly skipped keeps billing and breaks the
    switch it was blocking.
    """
    from comfy_qa.gcloud import Gcloud

    gc, _calls = _counting_gcloud([_box("a", "z1", "proj")])
    states = gc.instance_statuses([("a", "z1", "proj"), ("ghost", "z1", "proj")])

    assert states[("ghost", "z1", "proj")] == Gcloud.UNKNOWN_STATE
    assert states[("ghost", "z1", "proj")] != "TERMINATED"


def test_the_same_name_in_two_zones_is_two_machines():
    """Matched on name AND zone: an instance name is only unique within a zone."""
    gc, _calls = _counting_gcloud([
        _box("comfy-win", "us-central1-a", "proj", status="RUNNING"),
        _box("comfy-win", "europe-west4-a", "proj", status="TERMINATED"),
    ])
    states = gc.instance_statuses([("comfy-win", "us-central1-a", "proj"),
                                   ("comfy-win", "europe-west4-a", "proj")])

    assert states[("comfy-win", "us-central1-a", "proj")] == "RUNNING"
    assert states[("comfy-win", "europe-west4-a", "proj")] == "TERMINATED"
