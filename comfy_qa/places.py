""""Test this": everywhere a thing could be tested, and whether each is ready.

A tester starts from anything — a QA Desk case, a ComfyUI or frontend PR, a
release, a version (Ali, 3 Nov: "not just limited to qa desk") — and the
question is the same every time: where can I test this, right now, and if
nowhere, what would it take? This module answers it for every machine in the
host list and for the three cloud environments, each one **Ready**, **Can be
prepared** (with what would change and roughly how long) or **Not suitable**
(with the reason). Listing changes nothing: no box is started, no tunnel is
opened, no build is touched. It reads Google's instance list, what an
already-open tunnel answers, GitHub, and QA Desk.

WHAT "RIGHT" MEANS. The OS and the GPU, when they were asked for
(`--machine windows/l4`, the same description every machine command takes), and
the build: the backend and the frontend. For a QA Desk case or a
release, the backend is any ComfyUI and the frontend is the release's newest
core tag — a `v<release>.N` release made on `core/<release>` — because that is
what a local pass is recorded against (`case pass --core-tag`); QA Desk's own
plan says core cases wait for that tag, so until one exists no machine can be
prepared for one and each says so. A cloud environment serves its own backend
and one frontend, read off the `x-frontend-version` header the hidden `env`
command reads; for a release it is right when that commit is on the release's
`cloud/<release>` branch, which GitHub's compare answers.

THE CASE'S TARGET DECIDES WHICH KINDS COUNT. QA Desk tags each case with where
it is tested — `cloud` (testcloud), `core` (a local ComfyUI, at the core tag),
`shared` (either, once) and `shared-local` (both) — and its CLI records a result
`--on testcloud` or `--on local` only. So a cloud case is never tested on a
machine, a core case never on cloud, and for a case staging and prod are not
places QA Desk can record a result from.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .build import FRONTEND_REPO, Build

READY = "Ready"
PREPARE = "Can be prepared"
UNSUITABLE = "Not suitable"

# The cloud environments, by the name a tester uses, with the `env` command's
# own name for each — so this and `comfy-qat env` read the same three hosts.
CLOUD_PLACES = (("testcloud", "testcloud"), ("staging", "stagingcloud"),
                ("prod", "cloud"))

# What QA Desk's `--on` accepts for each kind of place (`qa-desk describe`).
ON_TESTCLOUD = "testcloud"
ON_LOCAL = "local"

# QA Desk's test targets, in its own words (the qa-desk skill, 3 Nov), and which
# kinds of place each one counts.
TARGETS = {
    "cloud": ("testcloud", {"cloud"}),
    "core": ("a local ComfyUI, at the release's core tag", {"machine"}),
    "shared": ("testcloud or a local ComfyUI, once", {"cloud", "machine"}),
    "shared-local": ("testcloud and on a local ComfyUI, a pass on each",
                     {"cloud", "machine"}),
}

# Rough times, for "Can be prepared". Each was MEASURED ONCE, live, on step 2's
# no-GPU boxes (live-s2/live.md, 3 Nov), or is said to be a guess: one reading,
# CPU boxes, one network. They are "about", never promises.
MINUTES = {
    # `go` on a stopped box that already has ComfyUI: not measured. A create
    # took 1.5 min to RUNNING (evidence/02), and a start does less.
    "start": "about 2 min",
    # `go --frontend-version` on a box with ComfyUI: a relaunch, and ComfyUI
    # fetches the release itself at startup. Not measured on its own.
    "frontend": "about 1 min",
    # `switch --pr 17497` on the Linux box: 58 s (evidence/07).
    "backend": "about 1 min on Linux, longer on Windows",
    # A frontend PR is built on the box by comfy-cli's builder. Not measured.
    "frontend-pr": "several minutes (not measured)",
    # First `go` on a fresh Linux box, v0.43.0: 4 min (evidence/03-04); a clean
    # Windows install: 10.5 min (evidence/51).
    "install": "about 4 min on Linux, 10 on Windows",
    # A running box with no tunnel: `go` opens one, and starts ComfyUI — or
    # installs it, which this cannot see from here without SSH.
    "connect": "about a minute if ComfyUI is on it; installing it is about 4 min "
               "on Linux, 10 on Windows",
}


@dataclass
class Target:
    """What to test, in the terms each kind of place is judged by."""

    label: str
    version: str | None = None          # a QA Desk release, e.g. "1.60"
    case: str | None = None             # a QA Desk case, e.g. "C-01"
    title: str | None = None
    test_target: str | None = None      # cloud / core / shared / shared-local
    build: Build | None = None          # what a machine must run, checked
    core_tag: str | None = None         # the release's newest core tag
    waiting: str | None = None          # why no machine can be prepared yet
    cloud_line: str | None = None       # cloud right when on this branch
    cloud_commit: str | None = None     # ... or when serving exactly this commit
    cloud_no: str | None = None         # why no cloud environment is right
    machine: str | None = None          # windows, l4, windows/l4, linux/none
    notes: list[str] = field(default_factory=list)

    def counts(self, kind: str) -> bool:
        if self.test_target not in TARGETS:
            return True
        return kind in TARGETS[self.test_target][1]


@dataclass
class Place:
    name: str
    kind: str
    state: str
    why: str
    command: str | None = None
    serves: str | None = None

    def as_dict(self) -> dict:
        found = {"place": self.name, "kind": self.kind, "state": self.state,
                 "why": self.why}
        if self.command:
            found["command"] = self.command
        if self.serves:
            found["serves"] = self.serves
        return found


class TargetError(Exception):
    def __init__(self, message: str, fix: str | None = None) -> None:
        super().__init__(message)
        self.fix = fix
        self.kind = "target"
        self.refusal = True


# --- what to test --------------------------------------------------------------

_RELEASE = re.compile(r"^v?(\d+\.\d+)$")
_MACHINE = re.compile(r"^[a-z0-9-]+(?:/[a-z0-9-]+)?$")
_CASE = re.compile(r"^[A-Za-z]+-\d+$")


def release_word(value: str) -> str:
    """`1.60` for `1.60` or `v1.60`; refused otherwise, in words."""
    match = _RELEASE.match((value or "").strip())
    if not match:
        raise TargetError(
            f"{value!r} is not a release: QA Desk's releases are a major and a minor "
            f"version, like 1.60.",
            fix="the releases QA Desk has: qa-desk release list")
    return match.group(1)


def core_tag(version: str, ask=None) -> str | None:
    """The newest frontend release made on `core/<version>` — the core tag a
    local pass of that release is recorded against — or None when there is none.

    A `v<version>.*` tag on `main` does not count: QA Desk's plan for 1.60 says
    core cases wait for "the first v1.60.* tag on core/1.60 made after the cut"
    and that "v1.60.* tags on main from before the cut (the 1.60.0 bump) don't
    count". GitHub's release list says which branch each was made from.
    """
    from . import build

    ask = ask or build.github
    status, body = build._ask(ask, f"repos/{FRONTEND_REPO}/releases?per_page=100")
    if status != 200 or not isinstance(body, list):
        return None
    branch = f"core/{version}"
    made = [release for release in body if isinstance(release, dict)
            and str(release.get("tag_name", "")).startswith(f"v{version}.")
            and release.get("target_commitish") == branch
            and not release.get("draft") and not release.get("prerelease")]
    made.sort(key=lambda release: str(release.get("published_at") or ""), reverse=True)
    return str(made[0]["tag_name"]) if made else None


def frontend_tag_commit(tag: str, ask=None) -> str | None:
    """The commit a frontend release tag points at, through a tag object when
    the tag is annotated."""
    from . import build

    ask = ask or build.github
    status, ref = build._ask(ask, f"repos/{FRONTEND_REPO}/git/ref/tags/{tag}")
    target = (ref or {}).get("object") if status == 200 and isinstance(ref, dict) else None
    sha = str((target or {}).get("sha") or "")
    if (target or {}).get("type") == "tag" and sha:
        status, body = build._ask(ask, f"repos/{FRONTEND_REPO}/git/tags/{sha}")
        target = (body or {}).get("object") if status == 200 and isinstance(
            body, dict) else None
        sha = str((target or {}).get("sha") or "")
    return sha.lower() or None


def target_for(release: str | None = None, case: str | None = None, *,
               version=None, pr=None, commit=None, frontend_version=None,
               frontend_pr=None, machine=None, qa=None, ask=None) -> Target:
    """Read what was asked for into a `Target`, asking QA Desk and GitHub what
    they know. Refused, in words, for a release or a case QA Desk does not have,
    a build GitHub does not have, or two things at once."""
    from . import build, qadesk

    qa = qa or qadesk
    if machine is not None and not _MACHINE.match(machine.strip().lower()):
        raise TargetError(
            f"{machine!r} is not a description of a machine: an OS, a card, or both "
            f"as os/card.", fix="for example: --machine windows, --machine l4, "
                                "--machine windows/l4, --machine linux/none")
    backend = [value for value in (version, pr, commit) if value not in (None, "")]
    frontend = [value for value in (frontend_version, frontend_pr) if value not in (None, "")]
    if release is None and case is None and not backend and not frontend:
        raise TargetError(
            "say what to test: a QA Desk case, a release, a PR or a version.",
            fix=_examples())
    if case is not None and release is None:
        raise TargetError("a QA Desk case needs its release in front of it.",
                          fix=_examples())
    if release is not None and (backend or frontend):
        raise TargetError(
            "a release (or a case) already says which frontend it is, so it is "
            "not given with a PR or a version as well.",
            fix=_examples())

    if release is None:
        chosen = build.check(build.choose(
            version=version, pr=pr, commit=commit, frontend_version=frontend_version,
            frontend_pr=frontend_pr), ask)
        target = Target(label=short(chosen), build=chosen, machine=machine)
        if chosen.frontend and not backend:
            target.cloud_commit = frontend_tag_commit(chosen.frontend, ask)
        elif chosen.frontend_pr and not backend:
            target.cloud_no = (f"cloud serves its own deploys; frontend PR "
                               f"{chosen.frontend_pr} is built on a machine")
        else:
            target.cloud_no = ("cloud runs its own backend, not a ComfyUI release, "
                               "PR or commit you choose")
        return target

    version_word = release_word(release)
    target = Target(label=f"release {version_word}", version=version_word,
                    machine=machine, cloud_line=f"cloud/{version_word}")
    if case is not None:
        if not _CASE.match(case.strip()):
            raise TargetError(f"{case!r} is not a QA Desk case: they read like C-01.",
                              fix=f"the release's cases: qa-desk case list {version_word}")
        found = qa.case(version_word, case.strip().upper())
        target.case = str(found.get("case_key") or case.upper())
        target.title = str(found.get("title") or "") or None
        target.test_target = str(found.get("test_target") or "") or None
        target.label = f"{version_word} {target.case}"
    else:
        qa.release(version_word)
    tag = core_tag(version_word, ask)
    target.core_tag = tag
    if tag:
        target.build = build.choose(frontend_version=tag)
    else:
        target.waiting = (f"release {version_word} has no core tag yet — QA Desk's "
                          f"plan waits for the first v{version_word}.* release made "
                          f"on core/{version_word}")
    return target


def short(chosen: Build) -> str:
    """A build in a few words, for a sentence: what was chosen and nothing else."""
    parts = []
    if chosen.version:
        parts.append(f"ComfyUI {chosen.version}")
    elif chosen.pr:
        parts.append(f"ComfyUI PR {chosen.pr}")
    elif chosen.commit:
        parts.append(f"ComfyUI commit {chosen.commit[:10]}")
    if chosen.frontend:
        parts.append(f"frontend {chosen.frontend}")
    elif chosen.frontend_pr:
        parts.append(f"frontend PR {chosen.frontend_pr}")
    return " and ".join(parts) or chosen.describe()


def _examples() -> str:
    from . import say

    return say.fix("for example:", "comfy-qat where 1.60 C-01",
                   "comfy-qat where 1.60", "comfy-qat where --pr 17497",
                   "comfy-qat where --frontend-version v1.58.5")


# --- the cloud environments ----------------------------------------------------

_BACKPORT = re.compile(r"\[backport (cloud/\d+\.\d+)\]")


def cloud_places(target: Target, *, probe=None, ask=None) -> list[Place]:
    """testcloud, staging and prod, each with the frontend it serves.

    `probe` is `env.probe_cloud`, which reads the commit off the
    `x-frontend-version` header; GitHub says which release branch it is on.
    """
    from . import build, env

    probe = probe or env.probe_cloud
    ask = ask or build.github
    places = []
    for name, key in CLOUD_PLACES:
        report = probe(key, env.CLOUD_ENVS[key], resolve=False)
        if report.error or not report.sha:
            places.append(Place(name, "cloud", UNSUITABLE,
                                f"could not read which frontend it serves "
                                f"({report.error or 'no answer'})"))
            continue
        sha = report.sha.lower()
        serves = f"{sha[:8]}{_line_of(sha, ask)}"
        place = Place(name, "cloud", UNSUITABLE, "", serves=serves)
        if not target.counts("cloud"):
            place.why = (f"serves {serves}; a {target.test_target} case is tested on "
                         f"{TARGETS[target.test_target][0]}, not on cloud")
        elif target.cloud_no:
            place.why = f"serves {serves}; {target.cloud_no}"
        elif target.cloud_commit:
            if sha == target.cloud_commit:
                place.state, place.why = READY, f"serves {serves}, which is {target.label}"
            else:
                place.why = f"serves {serves}, not {target.label} ({target.cloud_commit[:8]})"
        elif target.cloud_line:
            on, how = _on_branch(target.cloud_line, sha, ask)
            if on is None:
                place.why = (f"serves {serves}, and GitHub could not say whether that "
                             f"is on {target.cloud_line}")
            elif not on:
                place.why = f"serves {serves}, which is not on {target.cloud_line}"
            elif target.case and name != "testcloud":
                place.why = (f"serves {serves}, on {target.cloud_line}; QA Desk records "
                             f"a cloud result on testcloud only")
            else:
                place.state, place.why = READY, f"serves {serves}, on {target.cloud_line}{how}"
        places.append(place)
    return places


def _line_of(sha: str, ask) -> str:
    """`, on cloud/1.60` when the commit's subject says it is a backport there."""
    from . import build

    try:
        status, body = build._ask(ask, f"repos/{FRONTEND_REPO}/commits/{sha}")
    except build.BuildError:
        return ""
    message = str(((body or {}).get("commit") or {}).get("message") or "") \
        if status == 200 and isinstance(body, dict) else ""
    match = _BACKPORT.search(message.splitlines()[0] if message else "")
    return f" (a {match.group(1)} backport)" if match else ""


def _on_branch(branch: str, sha: str, ask) -> tuple[bool | None, str]:
    """Is `sha` on `branch`? GitHub's compare: identical or behind means the
    branch contains it. The second value says how far behind its tip."""
    from . import build

    try:
        status, body = build._ask(ask, f"repos/{FRONTEND_REPO}/compare/{branch}...{sha}")
    except build.BuildError:
        return None, ""
    if status != 200 or not isinstance(body, dict):
        return (False, "") if status == 404 else (None, "")
    said = body.get("status")
    if said == "identical":
        return True, ", its tip"
    if said == "behind":
        behind = body.get("behind_by")
        return True, (f", {behind} commit{'' if behind == 1 else 's'} behind its tip"
                      if isinstance(behind, int) else "")
    return False, ""


# --- the machines --------------------------------------------------------------


def machine_places(target: Target, hosts, *, gc=None, states=None,
                   answering=None, read=None) -> list[Place]:
    """Every machine in the host list, judged against `target`.

    `states` is `inventory.survey`'s rows (Google's word for each box); a box
    that is running is only Ready if ComfyUI already answers on its tunnel and
    says it is the build — `answering` and `read` are those two questions, so a
    listing never opens a tunnel or starts anything.
    """
    from .config import declares_no_gpu
    from .gcloud import GONE

    answering = answering or _answering
    read = read or _read_build
    places = []
    for host in hosts:
        kind = host.kind
        if not target.counts("machine"):
            places.append(Place(host.name, kind, UNSUITABLE,
                                "a cloud case is tested on testcloud, not on a machine"))
            continue
        wrong = _machine_mismatch(host, target, declares_no_gpu)
        if wrong:
            places.append(Place(host.name, kind, UNSUITABLE, wrong))
            continue
        if target.waiting:
            places.append(Place(host.name, kind, UNSUITABLE, target.waiting))
            continue
        flags = target.build.flags() if target.build is not None else ""
        command = f"comfy-qat go {host.name}{' ' + flags if flags else ''}"
        if not host.is_remote:
            places.append(_local(host, target, answering, read))
            continue
        row = (states or {}).get(host.name)
        state = getattr(row, "state", None) or "unknown"
        if state == GONE:
            places.append(Place(host.name, kind, UNSUITABLE,
                                "not on its project any more — it was deleted"))
            continue
        if state == "TERMINATED":
            places.append(Place(host.name, kind, PREPARE,
                                f"stopped: go starts it ({MINUTES['start']}) and "
                                f"{_putting(target)}", command=command))
            continue
        if state != "RUNNING":
            places.append(Place(host.name, kind, PREPARE,
                                f"Google could not say whether it is running "
                                f"({state.lower()}); go starts it if it is stopped and "
                                f"{_putting(target)}", command=command))
            continue
        stamp = answering(host)
        if stamp is None:
            places.append(Place(host.name, kind, PREPARE,
                                f"running, and ComfyUI is not answering here: go opens "
                                f"the tunnel and starts or installs it "
                                f"({MINUTES['connect']}), and {_putting(target)}",
                                command=command))
            continue
        places.append(_judged(host, target, stamp, read(gc, host, stamp, target),
                              command))
    return places


def _machine_mismatch(host, target: Target, declares_no_gpu) -> str | None:
    """Why the machine is not the one `--machine` describes, or None.

    Read part by part, the way a machine command reads `windows/l4`: a word the
    OS table knows is an OS, `none` is no GPU, anything else a card. The match
    itself is config's selector, so `where` and `go` agree about which machine
    `windows` is.
    """
    from .config import OS_KEYWORDS, _matches_gpu, _matches_os, describe

    if not target.machine:
        return None
    for part in target.machine.strip().lower().split("/"):
        if part in OS_KEYWORDS:
            fits = _matches_os(host, part)
        elif part in ("none", "cpu"):
            fits = declares_no_gpu(host)
        else:
            fits = _matches_gpu(host, part)
        if not fits:
            return f"it is {describe(host)}, and {target.machine} was asked for"
    return None


def _putting(target: Target) -> str:
    """What `go` would put on a box whose build is not known from here."""
    if target.build is None:
        return "confirms the build it is on"
    return (f"puts {short(target.build)} on it if that is not already there "
            f"({_minutes_for(target.build)})")


def _minutes_for(chosen: Build) -> str:
    if chosen.version or chosen.pr or chosen.commit:
        return MINUTES["backend"]
    if chosen.frontend_pr:
        return MINUTES["frontend-pr"]
    return MINUTES["frontend"]


def _local(host, target: Target, answering, read) -> Place:
    """This computer's own ComfyUI: Ready if it serves the build, and never
    prepared — this tool does not change the build of your own install."""
    stamp = answering(host)
    if stamp is None:
        return Place(host.name, host.kind, UNSUITABLE,
                     "not serving, and this tool does not start or change your own "
                     "install")
    wrong, unread = read(None, host, stamp, target)
    if not wrong and not unread:
        return Place(host.name, host.kind, READY, f"serves {_said(stamp)}")
    return Place(host.name, host.kind, UNSUITABLE,
                 f"serves {'; '.join(wrong) or _said(stamp)}"
                 f"{' — could not read ' + ', '.join(unread) if unread else ''}; "
                 f"this tool does not change your own install's build")


def _judged(host, target: Target, stamp, found, command: str) -> Place:
    wrong, unread = found
    if not wrong and not unread:
        return Place(host.name, host.kind, READY,
                     f"serving {_said(stamp)}, through its open tunnel")
    if wrong:
        return Place(host.name, host.kind, PREPARE,
                     f"serving {'; '.join(wrong)}: go changes it "
                     f"({_minutes_for(target.build) if target.build else ''}), keeping "
                     f"custom nodes and models", command=command)
    return Place(host.name, host.kind, PREPARE,
                 f"serving, and could not read {', '.join(unread)} from here: go "
                 f"checks it and changes what is not right", command=command)


def _said(stamp) -> str:
    parts = []
    if stamp.comfyui_version:
        parts.append(f"ComfyUI {stamp.comfyui_version}")
    if stamp.frontend_version:
        parts.append(f"frontend {stamp.frontend_version}")
    return ", ".join(parts) or "ComfyUI"


def _answering(host):
    """The stamp ComfyUI answers with on this host's port, or None. Loopback,
    on a short leash, and only through a tunnel that is already open."""
    import http.client

    from .stamp import ProbeError, fetch
    from .tunnel import status as tunnel_status

    if host.is_remote and not tunnel_status(host.name).running:
        return None
    try:
        return fetch(host.url, host=host.name, timeout=1.0)
    except (ProbeError, OSError, http.client.HTTPException):
        return None


def _read_build(gc, host, stamp, target: Target):
    """What the running ComfyUI says against the build asked for: (wrong,
    unread). The frontend and Manager over the tunnel; the checkout over SSH
    only when the backend was asked for, which is the one part only git on the
    box can answer."""
    from .lifecycle import observe

    chosen = target.build
    if chosen is None:
        return [], []
    backend = bool(chosen.version or chosen.pr or chosen.commit)
    seen = observe(gc if backend else None, host, stamp)
    # What was read, so a Ready row can say the frontend it confirmed.
    stamp.frontend_version = seen.frontend_version or stamp.frontend_version
    wrong, unread = chosen.contradictions(
        comfyui_version=seen.comfyui_version, commit=seen.comfyui_commit,
        pr=int(seen.comfyui_pr) if seen.comfyui_pr else None,
        frontend=seen.frontend_version, manager=seen.manager,
        torch=seen.pytorch_version)
    return wrong, unread


def summary(target: Target) -> list[str]:
    """What is being looked for, in words, above the table."""
    lines = [f"Test this: {target.label}" + (f" — {target.title}" if target.title else "")]
    if target.test_target in TARGETS:
        lines.append(f"  QA Desk tests it on {TARGETS[target.test_target][0]} "
                     f"({target.test_target})")
    if target.version:
        lines.append("  on a machine: "
                     + (f"frontend {target.core_tag}, release {target.version}'s newest "
                        f"core tag" if target.core_tag else target.waiting))
        lines.append(f"  on cloud: a frontend on {target.cloud_line}")
    elif target.build is not None:
        chosen = target.build
        backend = chosen.version or chosen.pr or chosen.commit
        lines.append(f"  on a machine: {short(chosen)}"
                     + ("" if backend else ", with whatever ComfyUI the machine has"))
    if target.machine:
        lines.append(f"  on a machine that is {target.machine}")
    return lines

