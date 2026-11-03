"""Which ComfyUI a box runs: a build you chose, rather than whatever master was.

Until the build choice every install cloned ComfyUI's default branch and took whatever was
on it that minute. A QA tool is asked a narrower question than that — "does this
release do X", "does this PR fix Y" — and a box that cannot be told which build
to run answers a different question from the one asked, while looking exactly
like it answered the right one.

A build is five independent choices, and nothing here couples them:

  * **the backend** — one of a release tag (`--version v0.43.0`), a pull request
    (`--pr 17497`) or a commit (`--commit 426f5ee`), or none, which keeps what
    the tool always did: the tip of ComfyUI's default branch;
  * **the frontend** — a frontend release (`--frontend-version v1.58.4`) or a
    frontend pull request (`--frontend-pr 5678`), or none, which keeps the
    frontend the backend pins in its own requirements;
  * **ComfyUI-Manager** — ON unless asked otherwise (Ali, 3 Nov: "manager should
    always be on btw, by default"), so `--no-manager` is the only spelling;
  * **PyTorch** — unpinned unless `--torch` names a version, optionally with the
    CUDA build after a `+`, as PyTorch writes it: `--torch 2.12.1+cu130`.

**comfy-cli does the installing, on the box.** These are its own options —
`comfy install --version/--pr/--commit`, `--skip-manager`, `--cuda-version` —
read off its source (comfy_cli/cmdline.py, `install`, 1.24.0 and 1.25.0 alike)
rather than guessed. What it cannot be told is a torch version, so the pin goes
in as a pip constraint (`PIP_CONSTRAINT`), which every `pip install` comfy-cli
runs then honours — the torch it fetches and the `torch` line in ComfyUI's
requirements alike. The frontend is not an install at all: ComfyUI fetches a
frontend release itself at startup (`--front-end-version`), and a frontend PR is
built on the box by comfy-cli's own builder — the one `comfy launch
--frontend-pr` calls — and served with `--front-end-root`.

**Bad input is refused before anything exists.** A tag that is not there, a PR
that is closed or never was, a commit nobody has: each is asked of GitHub first,
read-only, so the refusal costs a second rather than a box booted, billed and
half-installed before comfy-cli says the same thing in a traceback.

The one-line form (`Build.spec`) is what the host list's `build` field holds and
what the box keeps as its record of what it was last asked to run. Plain words,
space separated, because a person reads both.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace

# Where each half of a build comes from. `comfyanonymous/ComfyUI` is what
# comfy-cli clones (comfy_cli/constants.py, COMFY_GITHUB_URL); GitHub answers it
# as Comfy-Org/ComfyUI, and asking the canonical name saves a redirect.
BACKEND_REPO = "Comfy-Org/ComfyUI"
FRONTEND_REPO = "Comfy-Org/ComfyUI_frontend"

# The comfy-cli the box installs. PINNED, because every flag this module hands it
# was read off this version's source, and a box that installs whatever PyPI says
# today is running a tool nobody has read. 1.24.0 rather than 1.25.0 (released
# the morning this was written) because the install path is byte-identical in
# the two and the older one has had a week in the open.
COMFY_CLI_VERSION = "1.24.0"

# Miles's recommendation for every NVIDIA machine, quoted from his "Desktop
# hardware QA Process" page as written: "Run all NVIDIA machines on PyTorch
# 2.12.1 for CUDA 13.0, the version most users run." His, not this tool's
# default: nothing is pinned unless `--torch` is given. The wheels exist — the
# PyTorch index listed torch-2.12.1+cu130 for cp310 to cp314, Linux x86_64 and
# Windows amd64, on 2026-11-03.
RECOMMENDED_NVIDIA_TORCH = "2.12.1+cu130"

# The CUDA builds comfy-cli's `--cuda-version` accepts (comfy_cli/constants.py,
# CUDAVersion), as PyTorch tags them. A `+cu` suffix outside this set is refused
# here rather than by comfy-cli on the box, after the box is billing.
CUDA_TAGS = {"cu130": "13.0", "cu129": "12.9", "cu128": "12.8", "cu126": "12.6",
             "cu124": "12.4", "cu121": "12.1", "cu118": "11.8"}

# A release tag as both repositories write them, `v` optional on the way in.
_TAG = re.compile(r"^v?(\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?)$")
# A commit: hex, at least seven characters (git's own short form), at most forty.
_COMMIT = re.compile(r"^[0-9a-fA-F]{7,40}$")
# A torch version, with PyTorch's own `+cuXXX` / `+cpu` local label if given.
_TORCH = re.compile(r"^(\d+\.\d+(?:\.\d+)?)(?:\+(cu\d{3}|cpu))?$")
# How comfy-cli names the branch it checks a PR out onto: `pr-<n>-<branch>`
# (comfy_cli/git_utils.py, checkout_pr). Read back off the box to say which PR a
# checkout came from.
_PR_BRANCH = re.compile(r"^pr-(\d+)-")


class BuildError(Exception):
    """A build that cannot be asked for, said plainly, with what to do instead.

    Always a refusal: nothing has been created or started when one is raised,
    which is the whole point of checking first.
    """

    def __init__(self, message: str, fix: str | None = None) -> None:
        super().__init__(message)
        self.fix = fix
        self.kind = "build"
        self.refusal = True


@dataclass(frozen=True)
class Build:
    """One build. Every field `None` (and Manager on) is the default build."""

    version: str | None = None       # a ComfyUI release tag, always `v`-prefixed
    pr: int | None = None            # a ComfyUI pull request
    commit: str | None = None        # a ComfyUI commit, lower-case hex
    frontend: str | None = None      # a frontend release tag, `v`-prefixed
    frontend_pr: int | None = None   # a frontend pull request
    manager: bool = True
    torch: str | None = None         # `2.12.1` or `2.12.1+cu130`
    # What GitHub said the PR's head commit was when it was checked. Not a
    # choice — a fact the check found, carried so a PR build can be confirmed
    # against the commit the box actually checked out.
    pr_head: str | None = None
    frontend_pr_head: str | None = None
    # Which of the four choices a command line actually made: "backend",
    # "frontend", "manager", "torch". Not part of the build — two builds that
    # ask for the same thing are equal however they were asked — but it is what
    # lets `go --no-manager` on a box running v0.43.0 leave it on v0.43.0,
    # rather than reading "nothing said about the backend" as "the default".
    named: frozenset = field(default=frozenset(), compare=False)

    # --- what it is, in words -------------------------------------------

    @property
    def names_a_backend(self) -> bool:
        return bool(self.version or self.pr or self.commit)

    def backend_words(self) -> str:
        if self.version:
            return f"ComfyUI {self.version}"
        if self.pr:
            return f"ComfyUI PR #{self.pr}"
        if self.commit:
            return f"ComfyUI commit {self.commit[:10]}"
        return "ComfyUI at the tip of its default branch"

    def frontend_words(self) -> str:
        if self.frontend:
            return f"frontend {self.frontend}"
        if self.frontend_pr:
            return f"frontend PR #{self.frontend_pr}"
        return "the frontend that ComfyUI pins"

    def manager_words(self) -> str:
        return "Manager on" if self.manager else "Manager off"

    def torch_words(self) -> str:
        if self.torch:
            return f"torch {self.torch}"
        return "torch as comfy-cli picks it for the box"

    def describe(self) -> str:
        """The build in one line: what `create`, `go` and `switch` say they chose."""
        return " · ".join((self.backend_words(), self.frontend_words(),
                           self.manager_words(), self.torch_words()))

    # --- the one-line form ----------------------------------------------

    def spec(self) -> str:
        """`comfyui=v0.43.0 frontend=v1.58.4 manager=on torch=2.12.1+cu130`.

        Every choice is written, defaults included, so a record read back says
        what was asked for rather than leaving a reader to know the defaults.
        """
        if self.version:
            backend = self.version
        elif self.pr:
            backend = f"pr:{self.pr}"
        elif self.commit:
            backend = f"commit:{self.commit}"
        else:
            backend = "default"
        if self.frontend:
            frontend = self.frontend
        elif self.frontend_pr:
            frontend = f"pr:{self.frontend_pr}"
        else:
            frontend = "default"
        words = [f"comfyui={backend}", f"frontend={frontend}",
                 f"manager={'on' if self.manager else 'off'}",
                 f"torch={self.torch or 'default'}"]
        if self.pr_head:
            words.append(f"pr_head={self.pr_head}")
        if self.frontend_pr_head:
            words.append(f"frontend_pr_head={self.frontend_pr_head}")
        return " ".join(words)

    def over(self, base: "Build | None") -> "Build":
        """This request laid over `base`: what it names wins, the rest is kept.

        `base` is what the box was last asked to run, or the host list's build
        for a box that has none yet. A request that named nothing at all is
        `base` itself; with no `base` the defaults fill in.
        """
        result = base or DEFAULT
        if "backend" in self.named:
            result = replace(result, version=self.version, pr=self.pr,
                             commit=self.commit, pr_head=self.pr_head)
        if "frontend" in self.named:
            result = replace(result, frontend=self.frontend,
                             frontend_pr=self.frontend_pr,
                             frontend_pr_head=self.frontend_pr_head)
        if "manager" in self.named:
            result = replace(result, manager=self.manager)
        if "torch" in self.named:
            result = replace(result, torch=self.torch)
        return replace(result, named=frozenset())

    def differs_only_in_frontend_release(self, other: "Build") -> bool:
        """Is the frontend RELEASE the one thing between these two builds?

        ComfyUI downloads a frontend release itself at startup, so moving
        between two of them — or between one and the pinned default — needs
        nothing installed; a frontend PR has to be built on the box and does.
        """
        if self.frontend_pr or other.frontend_pr:
            return False
        return (replace(self, frontend=None, pr_head=None)
                == replace(other, frontend=None, pr_head=None)
                and self.frontend != other.frontend)

    def flags(self, *, explicit_manager: bool = False) -> str:
        """The options that ask for this build again, for a command printed or
        handed on.

        `explicit_manager` writes `--manager` when Manager is on, which is the
        default and so otherwise goes unsaid. It is what a fix line needs: for
        the default build every other option is absent, and `comfy-qat go <box>`
        with no build option onto a box that is serving asks the box nothing at
        all — by design, it is the everyday command — so a fix that names
        nothing hands the same wrong ComfyUI over as ready (audit finding F2,
        3 Nov). Named, the box is held to the build and it is put back.
        """
        words: list[str] = []
        if self.version:
            words += ["--version", self.version]
        elif self.pr:
            words += ["--pr", str(self.pr)]
        elif self.commit:
            words += ["--commit", self.commit[:12]]
        if self.frontend:
            words += ["--frontend-version", self.frontend]
        elif self.frontend_pr:
            words += ["--frontend-pr", str(self.frontend_pr)]
        if not self.manager:
            words.append("--no-manager")
        elif explicit_manager:
            words.append("--manager")
        if self.torch:
            words += ["--torch", self.torch]
        return " ".join(words)

    def contradictions(self, *, comfyui_version: str | None, commit: str | None,
                       pr: int | None, frontend: str | None, manager: str | None,
                       torch: str | None) -> tuple[list[str], list[str]]:
        """What the running ComfyUI says that this build contradicts, and what
        could not be read at all. Both in words, for the sentence that says so.

        Read-off values only: `comfyui_version` and `torch` from /system_stats,
        `frontend` from the bundle being served, `manager` from Manager's own
        route, `commit` and `pr` from git in the checkout. A value that is None
        is UNREAD, never a contradiction.
        """
        wrong: list[str] = []
        unread: list[str] = []
        if self.version:
            if not comfyui_version:
                unread.append("the ComfyUI version")
            elif comfyui_version.lstrip("v") != self.version.lstrip("v"):
                wrong.append(f"ComfyUI {comfyui_version}, not {self.version}")
        if self.pr:
            on_head = bool(self.pr_head and commit and commit == self.pr_head)
            if pr is None and not commit:
                unread.append("which PR the checkout is on")
            elif pr != self.pr and not on_head:
                said = f"PR {pr}" if pr else f"commit {commit[:10]}" if commit else "?"
                wrong.append(f"{said}, not PR {self.pr}")
        if self.commit:
            if not commit:
                unread.append("the checkout's commit")
            elif not (commit.startswith(self.commit) or self.commit.startswith(commit)):
                wrong.append(f"commit {commit[:10]}, not {self.commit[:10]}")
        if self.frontend:
            if not frontend:
                unread.append("the frontend it serves")
            elif frontend.lstrip("v") != self.frontend.lstrip("v"):
                wrong.append(f"frontend {frontend}, not {self.frontend}")
        if manager is None:
            unread.append("whether Manager is running")
        elif (manager == "on") != self.manager:
            wrong.append(f"Manager {manager}, not {'on' if self.manager else 'off'}")
        if self.torch:
            pinned = self.torch_version or ""
            label = self.torch.split("+", 1)[1] if "+" in self.torch else None
            if not torch:
                unread.append("the torch version")
            elif not (torch == pinned or torch.startswith(pinned + "+")) or (
                    label and f"+{label}" not in torch):
                wrong.append(f"torch {torch}, not {self.torch}")
        return wrong, unread

    def same_choice(self, other: "Build | None") -> bool:
        """Do two builds ask for the same thing? The heads GitHub reported are
        facts about when each was checked, not part of the choice."""
        if other is None:
            return False
        return (replace(self, pr_head=None, frontend_pr_head=None)
                == replace(other, pr_head=None, frontend_pr_head=None))

    # --- what comfy-cli and ComfyUI are handed --------------------------

    def backend_args(self) -> list[str]:
        """`comfy install`'s own backend options for this build.

        `--version` takes the number without its `v` (comfy-cli adds it back:
        `checkout_stable_comfyui`), and `--pr` takes `#<n>` — one of the three
        forms `parse_pr_reference` accepts. Quoted by the caller: `#` starts a
        comment in sh and in PowerShell alike.
        """
        if self.version:
            return ["--version", self.version.lstrip("v")]
        if self.pr:
            return ["--pr", f"#{self.pr}"]
        if self.commit:
            return ["--commit", self.commit]
        return []

    @property
    def torch_version(self) -> str | None:
        """The version half of the pin: `2.12.1` out of `2.12.1+cu130`."""
        return self.torch.split("+", 1)[0] if self.torch else None

    @property
    def cuda_version(self) -> str | None:
        """comfy-cli's `--cuda-version` for a `+cuXXX` pin: `13.0` for `cu130`."""
        if not self.torch or "+" not in self.torch:
            return None
        return CUDA_TAGS.get(self.torch.split("+", 1)[1])

    def constraint(self) -> str | None:
        """The pip constraint that pins torch, or None when nothing is pinned."""
        return f"torch=={self.torch_version}" if self.torch else None

    def launch_args(self, frontend_root: str | None = None) -> str:
        """What ComfyUI's own command line gets, after `--listen` and `--port`.

        `--enable-manager` is ComfyUI's switch for the Manager package
        (comfy/cli_args.py), and is what `comfy launch` itself injects. A
        frontend release is ComfyUI's own `--front-end-version` — it downloads
        the release on startup — and a frontend PR is the directory its build
        landed in, which only the box knows.
        """
        args = []
        if self.manager:
            args.append("--enable-manager")
        if self.frontend:
            args += ["--front-end-version", f"{FRONTEND_REPO}@{self.frontend}"]
        elif self.frontend_pr and frontend_root:
            args += ["--front-end-root", frontend_root]
        return " ".join(args)


DEFAULT = Build()


def _tag(value: str, flag: str) -> str:
    match = _TAG.match((value or "").strip())
    if not match:
        raise BuildError(
            f"{flag} {value!r} is not a release tag. Releases are numbered, as in "
            f"v0.43.0.",
            fix=_releases_fix(flag))
    return f"v{match.group(1)}"


def _number(value, flag: str) -> int:
    text = str(value).strip().lstrip("#")
    if not text.isdigit() or int(text) <= 0:
        raise BuildError(
            f"{flag} {value!r} is not a pull request number. Give the number from "
            f"the PR's address, as in {flag} 17497.",
            fix=f"the open ones: https://github.com/"
                f"{BACKEND_REPO if flag == '--pr' else FRONTEND_REPO}/pulls")
    return int(text)


def _releases_fix(flag: str) -> str:
    repo = FRONTEND_REPO if flag.startswith("--frontend") else BACKEND_REPO
    return f"the releases there are: https://github.com/{repo}/releases"


def choose(*, version: str | None = None, pr=None, commit: str | None = None,
           frontend_version: str | None = None, frontend_pr=None,
           no_manager: bool | None = None, torch: str | None = None,
           no_gpu: bool = False) -> Build:
    """The build a command line asked for, or a refusal before anything else.

    Two backends at once is not an intention, it is a mistake, and comfy-cli
    would refuse it on the box ("--pr cannot be used with --version or
    --commit") — after the box had booted and started billing. Refused here, in
    the same words, with the two commands that ARE intentions. The same for two
    frontends.
    """
    backends = [(flag, value) for flag, value in
                (("--version", version), ("--pr", pr), ("--commit", commit))
                if value not in (None, "")]
    if len(backends) > 1:
        named = " and ".join(flag for flag, _ in backends)
        raise BuildError(
            f"{named} each choose the backend, so only one of them can be right: a "
            f"release, a PR or a commit. Nothing was started.",
            fix=_one_of(backends))
    frontends = [(flag, value) for flag, value in
                 (("--frontend-version", frontend_version),
                  ("--frontend-pr", frontend_pr)) if value not in (None, "")]
    if len(frontends) > 1:
        raise BuildError(
            "--frontend-version and --frontend-pr each choose the frontend, so "
            "only one of them can be right. Nothing was started.",
            fix=_one_of(frontends))

    named = set()
    if backends:
        named.add("backend")
    if frontends:
        named.add("frontend")
    if no_manager is not None:
        named.add("manager")
    if torch not in (None, ""):
        named.add("torch")
    chosen = Build(manager=not no_manager, named=frozenset(named))
    if version not in (None, ""):
        chosen = replace(chosen, version=_tag(version, "--version"))
    if pr not in (None, ""):
        chosen = replace(chosen, pr=_number(pr, "--pr"))
    if commit not in (None, ""):
        text = str(commit).strip()
        if not _COMMIT.match(text):
            raise BuildError(
                f"--commit {commit!r} is not a commit. A commit is its hash, seven "
                f"characters or more, as in --commit 426f5ee.",
                fix=f"the history: https://github.com/{BACKEND_REPO}/commits/master")
        chosen = replace(chosen, commit=text.lower())
    if frontend_version not in (None, ""):
        chosen = replace(chosen, frontend=_tag(frontend_version, "--frontend-version"))
    if frontend_pr not in (None, ""):
        chosen = replace(chosen, frontend_pr=_number(frontend_pr, "--frontend-pr"))
    if torch not in (None, ""):
        chosen = replace(chosen, torch=_torch(str(torch), no_gpu=no_gpu))
    return chosen


def _one_of(choices: list[tuple[str, object]]) -> str:
    """Which options to choose between, in one sentence.

    One line of prose, never one option to a line: a line under `to fix:`
    that starts `--version v0.43.0` reads as a command to paste, and pasted it
    is not one (audit parking item P2, the class 1.3.1 fixed for printed
    commands). The same holds for every option a fix below names.
    """
    named = [f"{flag} {value}" for flag, value in choices]
    listed = ", ".join(named[:-1]) + f" or {named[-1]}"
    return f"give only one of these options, on the same command: {listed}"


def _torch(value: str, *, no_gpu: bool) -> str:
    text = value.strip()
    match = _TORCH.match(text)
    if not match:
        raise BuildError(
            f"--torch {value!r} is not a PyTorch version. Give the version, with "
            f"the CUDA build after a + if you want one, as in --torch "
            f"{RECOMMENDED_NVIDIA_TORCH}.",
            fix=f"give the option a version, on the same command: --torch "
                f"{RECOMMENDED_NVIDIA_TORCH}, for example")
    label = match.group(2)
    if label and label != "cpu" and label not in CUDA_TAGS:
        raise BuildError(
            f"--torch {value!r} names a CUDA build comfy-cli cannot install. It "
            f"installs {', '.join(sorted(CUDA_TAGS, reverse=True))}.",
            fix=f"give the option a CUDA build it installs, on the same command: "
                f"--torch {match.group(1)}+cu130, for example")
    if label and label != "cpu" and no_gpu:
        raise BuildError(
            f"--torch {value!r} is a CUDA build, and this box has no GPU to run it "
            f"on. A box with no GPU installs the CPU build of torch.",
            fix=f"give the option the version alone, on the same command: --torch "
                f"{match.group(1)}")
    if label == "cpu":
        # The CPU build is what a box with no card gets anyway; `+cpu` says the
        # same thing twice, and only the version is a choice.
        return match.group(1)
    return text


def parse(spec: str | None) -> Build | None:
    """A `Build.spec` line read back — from the host list, or from the box.

    None for nothing at all. A line that does not parse is refused rather than
    read as the default: a host list that says `comfyui=v0.43` and quietly gets
    master is the failure this whole module is here to prevent.
    """
    text = (spec or "").strip()
    if not text:
        return None
    fields: dict[str, str] = {}
    for word in text.split():
        key, sep, value = word.partition("=")
        if not sep or key not in ("comfyui", "frontend", "manager", "torch",
                                  "pr_head", "frontend_pr_head"):
            raise BuildError(
                f"the build {text!r} has a word this tool does not read: {word!r}. "
                f"A build is comfyui=, frontend=, manager= and torch=.",
                fix='as in: build = "comfyui=v0.43.0 frontend=default manager=on '
                    'torch=default"')
        fields[key] = value
    backend = fields.get("comfyui", "default")
    frontend = fields.get("frontend", "default")
    manager = fields.get("manager", "on").lower()
    if manager not in ("on", "off", "true", "false"):
        raise BuildError(f"the build {text!r} says manager={manager!r}; it is on or off.",
                         fix="manager=on")
    chosen = choose(
        version=None if backend == "default" or ":" in backend else backend,
        pr=backend[3:] if backend.startswith("pr:") else None,
        commit=backend[7:] if backend.startswith("commit:") else None,
        frontend_version=None if frontend == "default" or ":" in frontend else frontend,
        frontend_pr=frontend[3:] if frontend.startswith("pr:") else None,
        no_manager=manager in ("off", "false"),
        torch=None if fields.get("torch", "default") == "default" else fields["torch"],
    )
    return replace(chosen, pr_head=fields.get("pr_head") or None,
                   frontend_pr_head=fields.get("frontend_pr_head") or None,
                   named=frozenset())


def pr_from_branch(branch: str | None) -> int | None:
    """The PR a checkout came from, read off the branch comfy-cli made for it."""
    match = _PR_BRANCH.match((branch or "").strip())
    return int(match.group(1)) if match else None


# --- asking GitHub ---------------------------------------------------------
#
# Read-only, and the only network this module does. `gh api` when the GitHub
# CLI is installed and signed in, because it is authenticated and the anonymous
# REST API allows sixty requests an hour per address — a QA afternoon can spend
# that. The public REST API otherwise, which needs nothing.


class Unreachable(Exception):
    """GitHub could not be asked at all — which is not GitHub saying no."""


def _gh(path: str):
    """`gh api <path>`: (status, body). 404 is an answer, not a failure."""
    try:
        done = subprocess.run(["gh", "api", path], capture_output=True, text=True,
                              timeout=20)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unreachable(str(exc)) from exc
    if done.returncode == 0:
        try:
            return 200, json.loads(done.stdout or "null")
        except ValueError as exc:
            raise Unreachable(f"gh answered something that is not JSON: {exc}") from exc
    said = f"{done.stderr} {done.stdout}"
    for code in (404, 422):
        if f"HTTP {code}" in said:
            return code, None
    raise Unreachable(" ".join(said.split())[:200] or f"gh exited {done.returncode}")


def _rest(path: str):
    """The public REST API: (status, body). 404 and 422 are answers."""
    from .stamp import USER_AGENT

    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.status, json.loads(response.read(1 << 20) or b"null")
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 422):
            return exc.code, None
        raise Unreachable(f"GitHub answered {exc.code}") from exc
    except (OSError, ValueError) as exc:
        raise Unreachable(str(exc)) from exc


def github(path: str):
    """Ask GitHub one read-only question. Replaced wholesale by the test suite."""
    if shutil.which("gh"):
        try:
            return _gh(path)
        except Unreachable:
            pass
    return _rest(path)


def check(chosen: Build, ask=None) -> Build:
    """Ask GitHub whether everything this build names exists, before anything else.

    Returns the build with what GitHub reported filled in: a PR's head commit,
    and a short commit's full hash. Raises `BuildError` with the plain reason
    and the fix for anything that does not exist, is closed, or was merged.
    Nothing to check — the default build — asks nothing.
    """
    ask = ask or github
    if chosen.version:
        _exists(ask, f"repos/{BACKEND_REPO}/git/ref/tags/{chosen.version}",
                f"ComfyUI has no release {chosen.version}, so there is nothing to "
                f"install. Nothing was created or started.",
                _releases_fix("--version"))
    if chosen.pr:
        head = _open_pr(ask, BACKEND_REPO, chosen.pr, "--pr", "ComfyUI")
        chosen = replace(chosen, pr_head=head)
    if chosen.commit:
        body = _exists(ask, f"repos/{BACKEND_REPO}/commits/{chosen.commit}",
                       f"ComfyUI has no commit {chosen.commit}, so there is nothing "
                       f"to install. Nothing was created or started.",
                       f"the history: https://github.com/{BACKEND_REPO}/commits/master")
        sha = str((body or {}).get("sha") or "")
        if _COMMIT.match(sha):
            chosen = replace(chosen, commit=sha.lower())
    if chosen.frontend:
        _exists(ask, f"repos/{FRONTEND_REPO}/releases/tags/{chosen.frontend}",
                f"the frontend has no release {chosen.frontend}, so ComfyUI would "
                f"have nothing to download. Nothing was created or started.",
                _releases_fix("--frontend-version"))
    if chosen.frontend_pr:
        head = _open_pr(ask, FRONTEND_REPO, chosen.frontend_pr, "--frontend-pr",
                        "the frontend")
        chosen = replace(chosen, frontend_pr_head=head)
    return chosen


def _ask(ask, path: str):
    try:
        return ask(path)
    except Unreachable as exc:
        raise BuildError(
            f"GitHub could not be asked whether that build exists ({exc}), so "
            f"nothing was created or started.",
            fix="check this machine can reach github.com, then run it again") from exc


def _exists(ask, path: str, missing: str, fix: str):
    status, body = _ask(ask, path)
    if status != 200:
        raise BuildError(missing, fix=fix)
    return body


def _open_pr(ask, repo: str, number: int, flag: str, what: str) -> str | None:
    status, body = _ask(ask, f"repos/{repo}/pulls/{number}")
    if status != 200 or not isinstance(body, dict):
        raise BuildError(
            f"{what} has no pull request {number}, so there is nothing to install. "
            f"Nothing was created or started.",
            fix=f"the open ones: https://github.com/{repo}/pulls")
    if body.get("merged") or body.get("merged_at"):
        merged = str(body.get("merge_commit_sha") or "")
        if flag == "--pr" and _COMMIT.match(merged):
            fix = (f"its change landed as commit {merged[:10]}; to install that, give "
                   f"the option --commit {merged[:10]} in place of --pr {number}, on "
                   f"the same command")
        else:
            fix = f"its change is in a later release: https://github.com/{repo}/releases"
        raise BuildError(
            f"{what} pull request {number} is already merged, so its branch is not "
            f"something to test on its own. Nothing was created or started.",
            fix=fix)
    if body.get("state") != "open":
        raise BuildError(
            f"{what} pull request {number} is closed, so its branch may be gone and "
            f"is not under review. Nothing was created or started.",
            fix=f"the open ones: https://github.com/{repo}/pulls")
    head = str(((body.get("head") or {}).get("sha")) or "")
    return head.lower() if _COMMIT.match(head) else None


# --- what the box says it runs ----------------------------------------------


@dataclass(frozen=True)
class OnTheBox:
    """What a box's own checkout and record say, read over SSH.

    `commit` and `branch` come from git in the checkout ComfyUI runs from, not
    from anything this tool wrote — they are what is there. `record` is what the
    box was last asked to run, written by the install that did it; it is a
    statement of intent and is never reported as what is running.
    """

    commit: str | None = None
    branch: str | None = None
    record: Build | None = None
    frontend_root: str | None = None

    @property
    def pr(self) -> int | None:
        return pr_from_branch(self.branch)


def facts(text: str | None) -> OnTheBox:
    """`facts_command`'s answer, as fields. Anything unreadable is None.

    A record that does not parse is read as no record: it was written by this
    tool, so a line it cannot read back means the file was damaged, and the
    honest reading of a damaged record is "nothing is known about what this box
    was asked to run".
    """
    fields: dict[str, str] = {}
    for line in str(text or "").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key in ("COMMIT", "BRANCH", "RECORD", "FRONTEND_ROOT"):
            fields[key] = value.strip()
    commit = fields.get("COMMIT") or ""
    branch = fields.get("BRANCH") or ""
    try:
        record = parse(fields.get("RECORD"))
    except BuildError:
        record = None
    return OnTheBox(
        commit=commit.lower() if _COMMIT.match(commit) else None,
        branch=branch if branch and branch != "HEAD" else None,
        record=record,
        frontend_root=fields.get("FRONTEND_ROOT") or None,
    )
