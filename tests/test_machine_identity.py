"""Which machine an entry points at, and who gets to decide.

`kind` was a two-valued flag — `local` or `gce` — and everything that needed to
know *which* machine an entry meant went and read Google's three fields off the
host. Three consequences, all of them the wrong-machine failure this tool exists
to prevent, arriving through the one door nobody was watching:

  * the host list's same-machine rule built `(project, zone, instance)` and so
    collapsed every machine that is not a Google instance onto `("", "", "")`,
    refusing the SECOND one as a duplicate of the first;
  * `is_remote` was literally `kind == "gce"`, which is what made that
    collapse invisible — the rule skipped the hosts it would have broken;
  * a tunnel record was matched on `(port, instance, zone, project)` with the
    instance falling back to the host's NAME, and a name survives a machine
    being destroyed and recreated.

Identity is now supplied by the kind: a kind says what names one of its
machines, and everything that needs to know asks the host rather than Google.
Nothing here adds a provider. The kinds registered below are hypothetical and
invent nothing about any real one beyond the two things identity needs — that a
provider has its own name for a machine, and that the name may belong to this
incarnation of it rather than to the machine for ever.

The Google cases in this file are guards, not repros: they pin the shape GCE's
identity has today so that adding a provider cannot quietly change it.
"""

from __future__ import annotations

import os

import pytest

from comfy_qa import config
from comfy_qa.config import ConfigError, Host, parse
from comfy_qa.tunnel import TunnelError, open_tunnel, status

GCE = {
    "kind": "gce",
    "os": "Windows Server 2022",
    "gpu": "L4",
    "gce_instance": "comfy-win",
    "gce_zone": "us-central1-a",
    "gce_project": "proj",
    "port": 8190,
}

ME = os.getpid()


def started(token):
    """A stand-in for 'when did this process start' — what a pid alone lacks."""
    return lambda pid: token


def launched_by(record):
    def launcher(cmd, log):
        record.append(cmd)
        return ME
    return launcher


@pytest.fixture
def pod_kind(monkeypatch):
    """A second remote provider, registered the way a real one would be.

    One required field, which is also what names the machine. That is the whole
    of it: no placement, no allowance, no storage model, nothing about any
    vendor. If registering a kind is not enough to make the host list and the
    tunnel treat its machines correctly, the shape is not ready to hold a
    provider — which is what these tests ask.
    """
    from comfy_qa.config import MachineKind

    monkeypatch.setitem(config.KINDS, "pod", MachineKind(
        name="pod",
        remote=True,
        requires=("pod_id",),
        identifies_by=("pod_id",),
        in_words="pod {pod_id!r}",
    ))


def pod(name: str, port: int, pod_id: str) -> Host:
    return Host(name=name, kind="pod", port=port, extra=(("pod_id", pod_id),))


# --- D1/D2: one entry, one machine, for a machine Google never heard of -------


def test_two_machines_of_a_second_provider_are_two_machines(pod_kind):
    """The defect this file was opened for.

    Both entries name a machine, the two names differ, and the host list refused
    the second one as the first machine over again — because the key it built
    was Google's three fields, which neither entry carries, so both came out
    `("", "", "")`. The first machine of a second provider works and the failure
    waits for the second.
    """
    hosts = parse({"hosts": {
        "pod-a": {"kind": "pod", "port": 8190, "pod_id": "aaaa"},
        "pod-b": {"kind": "pod", "port": 8191, "pod_id": "bbbb"},
    }})

    assert [host.name for host in hosts] == ["pod-a", "pod-b"]
    assert hosts[0].machine_id == ("pod", "aaaa")
    assert hosts[1].machine_id == ("pod", "bbbb")
    assert hosts[0].machine_id != hosts[1].machine_id


def test_two_entries_for_one_machine_of_a_second_provider_are_still_refused(pod_kind):
    """The rule has to keep working, not stop firing.

    Widening it by deleting it would pass the test above and lose the thing the
    rule is for: two entries, two ports, two tunnels, one machine, and a result
    recorded against one of those names saying nothing about the other.
    """
    with pytest.raises(ConfigError) as caught:
        parse({"hosts": {
            "pod-a": {"kind": "pod", "port": 8190, "pod_id": "aaaa"},
            "pod-a-again": {"kind": "pod", "port": 8191, "pod_id": "aaaa"},
        }})

    message = str(caught.value)
    assert "are the same machine" in message
    assert "'aaaa'" in message, (
        "the refusal has to name the machine it means — the GCE one names the "
        "instance, the zone and the project, and a refusal that names nothing "
        "cannot be acted on")


def test_a_provider_that_cannot_name_a_machine_does_not_make_every_host_one(monkeypatch):
    """Not knowing which machine an entry means is not knowing they are the same.

    The old key answered `("", "", "")` for every host it could not describe,
    which is a positive claim that they are one machine. An identity that is not
    known is `None`, and `None` never matches `None`.
    """
    from comfy_qa.config import MachineKind

    monkeypatch.setitem(config.KINDS, "vague",
                        MachineKind(name="vague", remote=True))

    hosts = parse({"hosts": {
        "one": {"kind": "vague", "port": 8190},
        "two": {"kind": "vague", "port": 8191},
    }})

    assert [host.name for host in hosts] == ["one", "two"]
    assert [host.machine_id for host in hosts] == [None, None]


def test_two_providers_that_give_a_machine_the_same_name_are_not_one_machine(monkeypatch):
    """Identity carries the kind, and this is why.

    Two providers both calling a machine `box-1` is ordinary — the names are
    theirs to choose and neither knows about the other. An identity made only of
    the provider's own name for the machine makes those one machine, and the
    host list refuses the second one.
    """
    from comfy_qa.config import MachineKind

    for name in ("pod", "droplet"):
        monkeypatch.setitem(config.KINDS, name, MachineKind(
            name=name, remote=True,
            requires=("box_id",), identifies_by=("box_id",)))

    hosts = parse({"hosts": {
        "a": {"kind": "pod", "port": 8190, "box_id": "box-1"},
        "b": {"kind": "droplet", "port": 8191, "box_id": "box-1"},
    }})

    assert hosts[0].machine_id != hosts[1].machine_id
    assert hosts[0].machine_id == ("pod", "box-1")
    assert hosts[1].machine_id == ("droplet", "box-1")


def test_whether_a_machine_is_reached_through_a_tunnel_comes_from_its_kind(pod_kind):
    """`is_remote` gated tunnelling, `stamp`, `up`, `open` and `down` on one
    string comparison, so a provider's machines were all local — and the
    same-machine rule skipped exactly the hosts it would have broken, which is
    why the two defects are one."""
    (box,) = parse({"hosts": {"pod-a": {"kind": "pod", "port": 8190, "pod_id": "a"}}})

    assert box.is_remote is True


def test_a_google_machine_is_still_named_by_its_project_zone_and_instance():
    """A guard, not a repro: GCE's identity is the triple it has always been,
    and the order is the order the old key used."""
    (box,) = parse({"hosts": {"comfy-win": dict(GCE)}})

    assert box.machine_id == ("gce", "proj", "us-central1-a", "comfy-win")
    assert box.is_remote is True


def test_the_machine_on_this_computer_is_not_a_machine_to_tell_apart():
    """A guard. `local` means "where I am sitting", there is only ever one, and
    it has no provider identity to carry."""
    (here,) = parse({"hosts": {"local": {"kind": "local"}}})

    assert here.is_remote is False
    assert here.machine_id is None


# --- D3: a record outlives the machine it was written about -------------------


def test_a_record_for_a_machine_that_was_destroyed_and_recreated_is_not_reused(
        pod_kind, tmp_path):
    """On GCE a box keeps its identity across a stop and a start, so matching on
    the instance is right there. Where destroy-and-recreate is the ordinary
    operation it is not: the near-end port is the host list's and survives, the
    host name is the host list's and survives, and the record left by the
    machine that is gone therefore fits the machine that replaced it.

    What that costs is a tunnel to a machine that no longer exists, handed back
    as this machine's, with `running` true and a URL beside it.
    """
    before = pod("comfy-pod", 8190, "first")
    after = pod("comfy-pod", 8190, "second")

    open_tunnel(before, tmp_path, launcher=launched_by([]),
                identify=started("boot-A"))

    with pytest.raises(TunnelError) as caught:
        open_tunnel(after, tmp_path, launcher=launched_by([]),
                    identify=started("boot-A"))

    assert "is already open" in str(caught.value)


def test_a_tunnel_to_the_same_machine_is_still_reused(pod_kind, tmp_path):
    """The other direction, so the fix above cannot be "never reuse anything".

    Same machine, same incarnation, same port: this is a reuse and it has to
    stay one, or every second `open` kills a working tunnel.
    """
    box = pod("comfy-pod", 8190, "first")
    open_tunnel(box, tmp_path, launcher=launched_by([]), identify=started("boot-A"))

    again = open_tunnel(box, tmp_path, launcher=launched_by([]),
                        identify=started("boot-A"),
                        port_busy=lambda port: True)

    assert again.running is True
    assert again.machine == ("pod", "first")


def test_a_tunnel_record_says_which_machine_it_goes_to(pod_kind, tmp_path):
    """So the answer comes off the record rather than off the host list that is
    asking — which is the whole difference between checking and assuming."""
    open_tunnel(pod("comfy-pod", 8190, "first"), tmp_path,
                launcher=launched_by([]), identify=started("boot-A"))

    state = status("comfy-pod", tmp_path, identify=started("boot-A"))

    assert state.machine == ("pod", "first")


def test_a_google_tunnel_records_the_triple_it_always_matched_on(tmp_path):
    """A guard on the GCE path: the identity written down is the same three
    fields the old comparison used, in the same order."""
    box = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022",
               gpu="L4", gce_instance="comfy-win", gce_zone="us-central1-a",
               gce_project="proj")
    open_tunnel(box, tmp_path, launcher=launched_by([]), identify=started("boot-A"))

    state = status("comfy-win", tmp_path, identify=started("boot-A"))

    assert state.machine == ("gce", "proj", "us-central1-a", "comfy-win")
    assert state.instance == "comfy-win", "still printed, and still the instance"


def test_a_record_written_before_identity_existed_is_still_a_google_machine(tmp_path):
    """Records outlive a release: one is on disk for every tunnel open across an
    upgrade. A record carrying an instance, a zone and a project and no identity
    could only ever have been a Google instance, because `gce` was the only
    remote kind there was — so it is read as one, and a live tunnel is not
    refused as going somewhere else the first time it is asked for.
    """
    import json

    from comfy_qa.tunnel import pid_file, record_file

    box = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022",
               gpu="L4", gce_instance="comfy-win", gce_zone="us-central1-a",
               gce_project="proj")
    record_file("comfy-win", tmp_path).write_text(json.dumps({
        "pid": ME, "identity": "boot-A", "opened": 0.0, "port": 8190,
        "instance": "comfy-win", "zone": "us-central1-a", "project": "proj",
    }), encoding="utf-8")
    pid_file("comfy-win", tmp_path).write_text(str(ME), encoding="utf-8")

    again = open_tunnel(box, tmp_path, launcher=launched_by([]),
                        identify=started("boot-A"), port_busy=lambda port: True)

    assert again.running is True


# --- D4: a machine Google was never asked about is not a machine Google lost --


def _google(listings: dict[str, list[dict]]):
    """A runner that answers `instances list` for the projects it knows about,
    and answers nothing at all for anything else — which is what `gcloud` does
    with a project that is not a project."""
    def runner(args, mode):
        asked = [arg for arg in args if arg.startswith("--project=")]
        project = asked[0].split("=", 1)[1] if asked else ""
        return listings.get(project)
    return runner


LIVE = [{"name": "comfy-win", "status": "RUNNING",
         "zone": "https://x/projects/proj/zones/us-central1-a"}]


def test_a_host_google_cannot_be_asked_about_does_not_blind_the_rest():
    """One host of another kind cost every Google host its answer.

    The callers build `(instance, zone, project)` off the host, so a machine
    that is not a Google instance arrives as `(None, None, None)`. That became
    its own project group, `instances list --project=None` cannot succeed, and
    `GcloudError` came out of the WHOLE call — which every caller catches and
    treats as "nobody could tell me about any of these". `list --live` printing
    `-` for eight boxes because of a ninth it was never going to reach.
    """
    from comfy_qa.gcloud import UNADDRESSABLE, Gcloud

    asked = ("comfy-win", "us-central1-a", "proj")
    other = (None, None, None)

    states = Gcloud(runner=_google({"proj": LIVE})).instance_statuses([asked, other])

    assert states[asked] == "RUNNING", (
        "the Google host still gets its real state — that is the point of not "
        "letting the other one poison the call")
    assert states[other] == UNADDRESSABLE


def test_the_word_for_a_machine_nobody_asked_about_is_not_the_prunable_one():
    """`GONE` is the one word `discover --prune` acts on, and it means "the
    project was read and does not hold this name" — a statement about a machine
    that was looked for. A machine that is not a Google instance was never
    looked for, and answering `GONE` offers a live machine up for deletion on
    evidence nobody gathered. That is the failure that cost a billing L4 once
    already, arriving through a different door.
    """
    from comfy_qa.gcloud import ELSEWHERE, GONE, UNADDRESSABLE, Gcloud

    other = ("a-pod", None, None)

    states = Gcloud(runner=_google({"proj": LIVE})).instance_statuses([other])

    assert states[other] == UNADDRESSABLE
    assert states[other] not in (GONE, ELSEWHERE, "", "RUNNING", "TERMINATED")


def test_every_machine_asked_about_gets_an_answer():
    """Dropping the ones it cannot address would be the quieter bug: a caller
    reads the result with `.get`, so a missing key is `None`, and `None` is
    neither `GONE` nor a state — the host simply stops being reconciled and
    nothing says so."""
    from comfy_qa.gcloud import Gcloud

    wanted = [("comfy-win", "us-central1-a", "proj"), ("", "", ""), (None, None, None)]

    states = Gcloud(runner=_google({"proj": LIVE})).instance_statuses(wanted)

    assert sorted(states, key=str) == sorted(wanted, key=str)


def test_a_google_machine_absent_from_its_project_is_still_gone():
    """A guard. Narrowing `GONE` must not narrow it away: the word still has to
    be produced for the case `discover --prune` exists to act on."""
    from comfy_qa.gcloud import GONE, Gcloud

    ghost = ("comfy-linux-2", "us-central1-a", "proj")

    states = Gcloud(runner=_google({"proj": LIVE})).instance_statuses([ghost])

    assert states[ghost] == GONE


# --- the same collapse, at a second site ---------------------------------------
#
# `relocate.would_not_load` kept its own copy of the identity — a local helper
# building `(project or "", zone or "", instance or "")` — and its docstring
# claimed parity with the loader: "Checked against the identity `config` itself
# uses ... so this refuses exactly what that would refuse, and no more." Fixing
# the loader made that sentence false and left the collapse behind the
# `is_remote` that now admits other kinds, so two machines the loader accepts as
# distinct were declared one machine before anything was created.


def _plan(home: Host, to_zone: str):
    from comfy_qa.relocate import Plan

    return Plan(host=home, to_zone=to_zone, source_disk="comfy-win-a",
                new_instance="comfy-win", new_disk=f"comfy-win-{to_zone[-1]}",
                snapshot="comfy-win-snap", machine_type="g2-standard-8")


HOME = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022",
            gpu="L4", gce_instance="comfy-win", gce_zone="us-central1-a",
            gce_project="proj")


def test_a_move_does_not_declare_two_machines_of_another_provider_one_machine(pod_kind):
    """The loader accepts these three; the move check refused them.

    A move does not touch a host it is not moving, so two machines of another
    provider sitting in the same host list collapsed onto `("", "", "")` here
    exactly as they did in the loader — and the move was refused, before
    anything was created, with a sentence naming no machine at all.
    """
    from comfy_qa.relocate import would_not_load

    hosts = [HOME, pod("pod-a", 8191, "aaaa"), pod("pod-b", 8192, "bbbb")]
    assert len({h.machine_id for h in hosts}) == 3, "the loader sees three machines"

    assert would_not_load(hosts, _plan(HOME, "us-central1-b")) is None


def test_a_move_still_refuses_a_real_collision_of_another_provider(pod_kind):
    """The other direction, so this cannot be fixed by deleting the check — and
    the refusal has to name the machine it means, which the collapsed version
    could not: it printed `naming one machine —  in  —`."""
    from comfy_qa.relocate import would_not_load

    hosts = [HOME, pod("pod-a", 8191, "same"), pod("pod-b", 8192, "same")]

    problem = would_not_load(hosts, _plan(HOME, "us-central1-b"))

    assert problem is not None
    assert "'pod-a'" in str(problem) and "'pod-b'" in str(problem)
    assert "'same'" in str(problem), (
        "the refusal names no machine — which is what the empty-string collapse "
        "produced, and what made it unactionable")


def test_the_sentence_a_google_collision_prints_is_unchanged():
    """A guard, and the reason it is worth one: this sentence is quoted verbatim
    in troubleshooting.md, so the identity moving underneath it must not change
    a byte of what a person reads."""
    from comfy_qa.relocate import would_not_load

    moved_out = [
        Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022",
             gpu="L4", gce_instance="comfy-win", gce_zone="us-central1-b",
             gce_project="proj"),
        Host(name="comfy-win-us-central1-a", kind="gce", port=8194,
             os="Windows Server 2022", gpu="L4", gce_instance="comfy-win",
             gce_zone="us-central1-a", gce_project="proj"),
    ]
    home = moved_out[0]

    problem = would_not_load(moved_out, _plan(home, "us-central1-a"))

    assert problem is not None
    assert ("naming one machine — comfy-win in us-central1-a — and a host list"
            in str(problem))
