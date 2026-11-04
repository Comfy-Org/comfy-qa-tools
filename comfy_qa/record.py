"""The frontend's own test recorder, started against a box — and a person records.

The recorder is ComfyUI_frontend's `comfy-test` (`pnpm comfy-test record`, its
`tools/test-recorder`, read on `core/1.60` on 3 Nov): it runs the frontend's dev
server against a backend and opens a browser for a person to click through,
then turns what they did into a Playwright test and offers to open the PR. This
tool does none of that. It gets the pieces ready and hands over:

  * **a frontend checkout of the matching version**, in comfy-qat's own folder
    beside the host list (`frontend/<ref>`), never anybody's own checkout. For
    release QA that is the release's `core/<version>` branch — the 2 Nov workshop
    notes: "Check out `core/1.60` branch" — and otherwise the release tag of the
    frontend the box is serving;
  * **the box**, serving that same frontend version (a mismatch is refused,
    with the command that fixes it), and running ComfyUI in multi-user mode while
    the recording runs ("multiuser mode required"), which this switches on, says
    so, and puts back afterwards;
  * **the recorder's own command**, `pnpm comfy-test record --backend
    http://127.0.0.1:<port>` — the box's tunnel — run in the checkout with this
    terminal, so the recorder's own prompts and its PR flow are the recorder's.
    Its dev server is told a free port on this machine, never 8188-8190, and is
    kept on loopback: Vite's own default, `localhost`, which the frontend's
    config changes only when `VITE_REMOTE_DEV=true`, so that variable is not
    passed on.

Recording is from the tester's own browser, whatever the box's OS: "Playwright
tests themselves don't need target-machine specificity (browser binaries only,
not OS)" (the same notes). There is no agent mode here — a person records.

Node and pnpm are checked before anything is cloned and, when missing, the
install commands are printed and never run: the recorder's own, in its order
(nvm, then the shell's start-up file read again — or a new terminal — so that
`nvm` is there, then `nvm install` and `nvm use` the major Node the checkout's
`engines` asks for, read off its package.json on GitHub; `corepack enable` for
pnpm). Whether the Node that IS there is that version is not checked here: it is
the recorder's own first check (`tools/test-recorder/src/checks/node.ts`), which
prints the same steps.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

FRONTEND_GIT = "https://github.com/Comfy-Org/ComfyUI_frontend.git"
FOLDER = "frontend"

# The recorder's own install advice (tools/test-recorder/src/checks/node.ts and
# pnpm.ts, core/1.60), printed when one is missing and never run. The install
# script adds nvm to the shell's start-up file, so `nvm` is not there until that
# file is read again: node.ts puts `source ~/.zshrc` (macOS) or `source
# ~/.bashrc` between the two, and without it `nvm install` was "command not
# found" when the lines were pasted in order (behaviour audit F11).
NVM_INSTALL = "curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash"
NVM_WINDOWS = "https://github.com/coreybutler/nvm-windows/releases"
PNPM_ENABLE = "corepack enable"
# When the checkout's own `engines` cannot be read: core/1.60 says ">=26.8.2 <27".
NODE_FALLBACK = "26"

# The recorder's dev server: Vite's own default port first, never a port a
# ComfyUI or a tunnel of this tool's lives on.
DEV_PORTS = range(5173, 5300)
NEVER = frozenset({8188, 8189, 8190})
# The one variable that moves the dev server off loopback (build/devAgentConfig.ts:
# `host: ... env.VITE_REMOTE_DEV === 'true' ? '0.0.0.0' : undefined`).
OFF_LOOPBACK = "VITE_REMOTE_DEV"


class RecordError(Exception):
    def __init__(self, message: str, fix: str | None = None, *, code: int = 2) -> None:
        super().__init__(message)
        self.fix = fix
        self.code = code
        self.kind = "record"
        self.refusal = code == 2


def checkout_dir(config: Path | None, ref: str) -> Path:
    from .config import DEFAULT_CONFIG_PATH

    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", ref).strip("-")
    return (config or DEFAULT_CONFIG_PATH).parent / FOLDER / safe


def ref_for(*, release: str | None, ref: str | None, served: str | None) -> str:
    """Which frontend to check out: the ref given, the release's `core/<version>`
    branch, or the release tag of the frontend the box serves."""
    if ref:
        return ref
    if release:
        return f"core/{release}"
    if served:
        return f"v{served}"
    raise RecordError("could not tell which frontend to check out: the box did not "
                      "say which frontend it serves, and no release or ref was given.",
                      fix=_fix("name the frontend, by its release:",
                               "comfy-qat record <box> <release>",
                               "or by a branch or tag of it:",
                               "comfy-qat record <box> --ref <branch-or-tag>"))


def _fix(*lines: str) -> str:
    from . import say

    return say.fix(*lines)


def missing_tools(which=shutil.which, node: str = NODE_FALLBACK,
                  platform: str | None = None) -> list[str]:
    """The install commands for Node and pnpm, for whichever is missing, each
    group under the sentence it belongs to. Empty when both are there. Printed,
    never run. `node` is the major version to install: `node_wanted`."""
    platform = platform or sys.platform
    lines = []
    if which("node") is None:
        if platform.startswith("win"):
            lines += ["Node.js is not installed. Install nvm-windows from its releases:",
                      NVM_WINDOWS,
                      "then, in a new terminal, the Node the frontend asks for:",
                      f"nvm install {node}", f"nvm use {node}"]
        else:
            start_up = "~/.zshrc" if platform == "darwin" else "~/.bashrc"
            lines += ["Node.js is not installed. Install nvm, load it, then the Node "
                      "the frontend asks for:",
                      NVM_INSTALL, f"source {start_up}", f"nvm install {node}",
                      f"nvm use {node}"]
    if which("pnpm") is None:
        lines += ["pnpm is not installed. It ships with Node.js, through corepack:",
                  PNPM_ENABLE]
    return lines


def node_wanted(package: dict | None) -> str:
    """The major Node version the frontend's `engines` asks for."""
    wanted = str(((package or {}).get("engines") or {}).get("node") or "")
    match = re.search(r"(\d+)", wanted)
    return match.group(1) if match else NODE_FALLBACK


def package_at(ref: str, ask=None) -> dict | None:
    """The frontend's package.json at `ref`, read from GitHub — so a version
    mismatch is refused before anything is cloned."""
    import base64

    from . import build

    ask = ask or build.github
    status, body = build._ask(ask, f"repos/{build.FRONTEND_REPO}/contents/"
                                   f"package.json?ref={ref}")
    if status != 200 or not isinstance(body, dict) or not body.get("content"):
        return None
    try:
        return json.loads(base64.b64decode(body["content"]).decode("utf-8"))
    except (ValueError, TypeError):
        return None


def local_package(directory: Path) -> dict | None:
    try:
        return json.loads((directory / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ensure_checkout(directory: Path, ref: str, say, run=None) -> None:
    """Clone `ref` into comfy-qat's own folder, or bring a clean one up to it.

    A checkout with changes in it is left exactly as it is, and said: what a
    recording writes lands in the checkout, and the recorder's PR flow commits
    it from there, so nothing here moves it under somebody's unpushed test.
    """
    run = run or subprocess.run
    if not (directory / ".git").exists():
        directory.parent.mkdir(parents=True, exist_ok=True)
        say(f"cloning the frontend at {ref} into {directory} (once; a few minutes)")
        done = run(["git", "clone", "--depth", "1", "--branch", ref, FRONTEND_GIT,
                    str(directory)], capture_output=True, text=True)
        if done.returncode != 0:
            raise RecordError(f"the frontend could not be cloned at {ref}: "
                              f"{_last(done.stderr)}",
                              fix="check the branch or tag, and that this machine can "
                                  "reach github.com", code=1)
        return
    status = run(["git", "-C", str(directory), "status", "--porcelain"],
                 capture_output=True, text=True)
    if status.returncode == 0 and status.stdout.strip():
        say(f"the frontend checkout in {directory} has changes in it — a recording, "
            f"perhaps — so it is used as it is, not updated")
        return
    say(f"bringing the frontend checkout up to {ref}")
    fetched = run(["git", "-C", str(directory), "fetch", "--depth", "1", "origin", ref],
                  capture_output=True, text=True)
    if fetched.returncode == 0:
        run(["git", "-C", str(directory), "checkout", "--detach", "FETCH_HEAD"],
            capture_output=True, text=True)
    else:
        say(f"could not fetch {ref} ({_last(fetched.stderr)}), so the checkout is "
            f"used as it is")


def ensure_dependencies(directory: Path, say, run=None) -> None:
    """`pnpm install` once, in the checkout: `pnpm comfy-test` runs through
    `tsx`, which the install puts there. The recorder installs again itself and
    that is quick once this has run."""
    run = run or subprocess.run
    if (directory / "node_modules").is_dir():
        return
    say("installing the frontend's dependencies in its checkout (pnpm install, once)")
    done = run(["pnpm", "install", "--frozen-lockfile"], cwd=str(directory),
               capture_output=True, text=True)
    if done.returncode != 0:
        raise RecordError(f"pnpm install failed in {directory}: {_last(done.stderr or done.stdout)}",
                          fix="check that Node.js and pnpm are the versions the frontend's "
                              "package.json engines asks for", code=1)


def _last(text: str | None) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1][:200] if lines else "no message"


def free_port(avoid: set[int] | None = None, ports=DEV_PORTS, busy=None) -> int:
    """A port for the recorder's dev server: free on loopback, and never one of
    the ports a ComfyUI or a tunnel of this tool lives on."""
    busy = busy or _busy
    for port in ports:
        if port in NEVER or port in (avoid or set()):
            continue
        if not busy(port):
            return port
    raise RecordError("no free port between 5173 and 5299 for the recorder's dev "
                      "server on this machine.",
                      fix="close what is listening there, then run this again")


def _busy(port: int) -> bool:
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def environment(port: int, base: dict | None = None) -> dict:
    """The recorder's environment: this one, with its dev server's port, and
    without the one variable that would take it off loopback."""
    env = dict(os.environ if base is None else base)
    env.pop(OFF_LOOPBACK, None)
    env["COMFY_TEST_DEV_PORT"] = str(port)
    return env


def command(backend: str, passthrough: list[str] | None = None) -> list[str]:
    return ["pnpm", "comfy-test", "record", "--backend", backend, *(passthrough or [])]


def multi_user(url: str, *, opener=None, timeout: float = 5.0) -> bool | None:
    """Is the ComfyUI at `url` in multi-user mode?

    ComfyUI's own answer: `GET /users` returns `{"storage": "server", "users":
    {...}}` with `--multi-user` and `{"storage": "server", "migrated": ...}`
    without it (app/user_manager.py, v0.43.0). None when it could not be asked.
    """
    import urllib.request

    from .stamp import USER_AGENT

    opener = opener or urllib.request.urlopen
    for path in ("/api/users", "/users"):
        request = urllib.request.Request(url.rstrip("/") + path,
                                         headers={"User-Agent": USER_AGENT})
        try:
            with opener(request, timeout=timeout) as response:
                body = json.loads(response.read(1 << 16) or b"null")
        except (OSError, ValueError):
            continue
        if isinstance(body, dict):
            return "users" in body
    return None


def recorded(directory: Path, run=None) -> list[str]:
    """The test files in the checkout that git does not have — a recording's,
    this session's or an earlier one's."""
    run = run or subprocess.run
    done = run(["git", "-C", str(directory), "status", "--porcelain",
                "--untracked-files=all"], capture_output=True, text=True)
    if done.returncode != 0:
        return []
    found = []
    for line in done.stdout.splitlines():
        path = line[3:].strip()
        if path.startswith("browser_tests/") and path.endswith((".spec.ts", ".ts", ".webm",
                                                                ".zip", ".json")):
            found.append(str(directory / path))
    return found


def snapshot(directory: Path, run=None) -> dict[str, tuple[int, int] | None]:
    """What `recorded` finds now, each with its size and modification time — so
    a file a later recording rewrites counts as written, and one it left alone
    does not."""
    found = {}
    for path in recorded(directory, run):
        try:
            facts = os.stat(path)
            found[path] = (facts.st_size, facts.st_mtime_ns)
        except OSError:
            found[path] = None
    return found


def written_since(before: dict, directory: Path, run=None) -> list[str]:
    """The test files this recording wrote: new since `before`, or changed.

    `recorded` alone counted every file git does not have, so a session that
    recorded nothing reported, and remembered for the bug draft, the file an
    earlier session left (behaviour audit F10)."""
    now = snapshot(directory, run)
    return [path for path, facts in now.items()
            if path not in before or before[path] != facts]
