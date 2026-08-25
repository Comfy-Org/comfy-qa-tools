"""The one place this tool shells out to gcloud.

Everything that talks to Google Cloud goes through `Gcloud.run`, for two reasons:
tests replace a single seam rather than patching subprocess everywhere, and every
failure can be turned into a message that names the command which fixes it.

gcloud is already on PATH on a machine that has it. Never prepend the SDK bin
directory — it bloats the command for no benefit.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field

COMPUTE_SERVICE = "compute.googleapis.com"
DEFAULT_TIMEOUT = 60

# Listing quotas returns every compute quota on the project — ~400 records and
# well over a megabyte — and measured at almost exactly 60 seconds on a live
# project. The default timeout sat right on that boundary, so the first real run
# timed out. There is no server-side filter for it; gcloud's --filter is applied
# after the fetch.
QUOTA_TIMEOUT = 240

# Starting or stopping an instance is not instant, and Windows is slower than
# Linux. gcloud blocks until the operation completes.
INSTANCE_TIMEOUT = 300


class GcloudError(Exception):
    """A gcloud call that failed.

    `fix` is a command the user can run. `raw` is everything gcloud printed —
    kept because the one-line summary is not always enough to classify a failure,
    and classifying on the summary meant a capacity stockout went unrecognised.
    """

    def __init__(self, message: str, fix: str | None = None, raw: str = "") -> None:
        super().__init__(message)
        self.fix = fix
        self.raw = raw or message


@dataclass
class Gcloud:
    """Thin wrapper. Swap `runner` in tests; nothing else needs mocking."""

    timeout: int = DEFAULT_TIMEOUT
    runner: object = field(default=None, repr=False)

    def available(self) -> str | None:
        # An injected runner stands in for the binary, so tests exercise the
        # real check order without needing gcloud installed.
        if self.runner is not None:
            return "<injected>"
        return shutil.which("gcloud")

    def run(self, args: list[str], *, parse_json: bool = True, timeout: int | None = None):
        """Run `gcloud <args>`. Returns parsed JSON, or raw text if parse_json is off."""
        if self.runner is not None:
            return self.runner(args, parse_json)

        exe = self.available()
        if exe is None:
            raise GcloudError(
                "gcloud is not installed or not on PATH.",
                fix="https://cloud.google.com/sdk/docs/install",
            )

        cmd = [exe, *args]
        if parse_json:
            cmd += ["--format=json"]
        limit = timeout or self.timeout
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=limit)
        except subprocess.TimeoutExpired as exc:
            raise GcloudError(
                f"gcloud timed out after {limit}s: {' '.join(args)}",
                fix="check your network, then try again",
            ) from exc

        if proc.returncode != 0:
            message, fix, raw = explain_failure(proc.stderr, proc.stdout, proc.returncode)
            raise GcloudError(message, fix=fix, raw=raw)

        out = proc.stdout.strip()
        if not parse_json:
            return out
        if not out:
            return None
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise GcloudError(f"gcloud returned output that is not JSON: {out[:200]}") from exc

    def run_interactive(self, args: list[str]) -> int:
        """Run gcloud with the terminal attached, for commands that need a human.

        `gcloud auth login` opens a browser and prompts. Capturing its output
        would hide the prompt and hang, so stdio is inherited rather than piped.
        Returns the exit code; nothing is parsed.
        """
        if self.runner is not None:
            return self.runner(args, "interactive")

        exe = self.available()
        if exe is None:
            raise GcloudError(
                "gcloud is not installed or not on PATH.",
                fix="https://cloud.google.com/sdk/docs/install",
            )
        return subprocess.run([exe, *args]).returncode

    def list_projects(self) -> list[dict]:
        return self.run(["projects", "list"]) or []

    def set_project(self, project: str) -> None:
        self.run(["config", "set", "project", project], parse_json=False)

    # --- the specific calls this tool makes -------------------------------

    def active_account(self) -> str | None:
        accounts = self.run(["auth", "list"]) or []
        for entry in accounts:
            if entry.get("status") == "ACTIVE":
                return entry.get("account")
        return None

    def current_project(self) -> str | None:
        value = self.run(["config", "get-value", "project"], parse_json=False)
        value = (value or "").strip()
        # gcloud prints this literal string when nothing is set.
        return None if value in ("", "(unset)") else value

    def billing_enabled(self, project: str) -> bool:
        info = self.run(["billing", "projects", "describe", project]) or {}
        return bool(info.get("billingEnabled"))

    def gpu_quotas(self, project: str) -> list[dict]:
        """Every compute quota whose id mentions GPUs, with its current value."""
        infos = self.run([
            "quotas", "info", "list",
            f"--service={COMPUTE_SERVICE}",
            f"--project={project}",
        ], timeout=QUOTA_TIMEOUT) or []
        return [q for q in infos if "GPU" in (q.get("quotaId") or "").upper()]

    def list_instances(self, project: str) -> list[dict]:
        """Every Compute Engine instance on the project, across all zones."""
        return self.run([
            "compute", "instances", "list", f"--project={project}",
        ]) or []

    def instance_status(self, name: str, zone: str, project: str) -> str:
        """RUNNING, TERMINATED, STAGING... TERMINATED is Google's word for stopped."""
        info = self.run([
            "compute", "instances", "describe", name,
            f"--zone={zone}", f"--project={project}",
        ]) or {}
        return info.get("status") or "UNKNOWN"

    def start_instance(self, name: str, zone: str, project: str) -> None:
        self.run([
            "compute", "instances", "start", name,
            f"--zone={zone}", f"--project={project}",
        ], parse_json=False, timeout=INSTANCE_TIMEOUT)

    def stop_instance(self, name: str, zone: str, project: str) -> None:
        self.run([
            "compute", "instances", "stop", name,
            f"--zone={zone}", f"--project={project}",
        ], parse_json=False, timeout=INSTANCE_TIMEOUT)

    def ssh(self, instance: str, zone: str, project: str, remote: str, *, stream: bool = True) -> int:
        """Run a command on the instance over IAP. Returns its exit code.

        Streaming inherits this terminal, so a remote ComfyUI's startup log
        appears exactly as it would if it were running locally. That is the whole
        point: a remote launch you cannot watch is a launch you cannot debug.
        """
        args = [
            "compute", "ssh", instance,
            f"--zone={zone}", f"--project={project}",
            "--tunnel-through-iap", f"--command={remote}",
        ]
        if self.runner is not None:
            return self.runner(args, "stream" if stream else True)

        exe = self.available()
        if exe is None:
            raise GcloudError("gcloud is not installed or not on PATH.")
        return subprocess.run([exe, *args]).returncode

    def ssh_output(self, instance: str, zone: str, project: str, remote: str) -> str:
        """Run a command on the instance and return what it printed."""
        args = [
            "compute", "ssh", instance,
            f"--zone={zone}", f"--project={project}",
            "--tunnel-through-iap", f"--command={remote}",
        ]
        if self.runner is not None:
            return self.runner(args, "output")

        exe = self.available()
        if exe is None:
            raise GcloudError("gcloud is not installed or not on PATH.")
        proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=INSTANCE_TIMEOUT)
        if proc.returncode != 0:
            message, fix, raw = explain_failure(proc.stderr, proc.stdout, proc.returncode)
            raise GcloudError(message, fix=fix, raw=raw)
        return proc.stdout.strip()

    def quota_preferences(self, project: str) -> list[dict]:
        return self.run([
            "quotas", "preferences", "list", f"--project={project}",
        ]) or []


def localized_message(text: str) -> str | None:
    """Pull the human-readable message out of gcloud's YAML error dump.

    Compute errors put the useful sentence under `localizedMessage.message`,
    wrapped across indented lines, while the ERROR: line itself can be nothing
    but `---`. That literal case is how a capacity stockout arrived as "---".
    """
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("message:"):
            continue
        indent = len(line) - len(line.lstrip())
        parts = [stripped[len("message:"):].strip()]
        for following in lines[index + 1:]:
            if not following.strip():
                break
            if len(following) - len(following.lstrip()) <= indent:
                break
            parts.append(following.strip())
        joined = " ".join(part for part in parts if part)
        if joined:
            return joined
    return None


def _is_separator(value: str) -> bool:
    return not value.strip("-_= ")


def explain_failure(stderr: str | None, stdout: str | None, returncode: int):
    """Turn gcloud's multi-line output into one line, a fix, and the raw text.

    gcloud writes long, friendly errors across many lines. Taking the last line
    yields a fragment like "to select an already authenticated account to use."
    The ERROR: line is usually the one that matters — except on compute errors,
    where it is literally `---` and the real sentence is further down.
    """
    text = (stderr or stdout or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return f"gcloud exited {returncode}", None, text

    message = next((line for line in lines if line.startswith("ERROR:")), lines[0])
    message = message.removeprefix("ERROR:").strip()
    # Drop the "(gcloud.billing.projects.describe)" breadcrumb; the caller knows.
    message = re.sub(r"^\(gcloud\.[^)]*\)\s*", "", message)

    if _is_separator(message):
        message = localized_message(text) or next(
            (line for line in lines if not _is_separator(line) and not line.startswith("ERROR:")),
            f"gcloud exited {returncode}",
        )

    fix = None
    lowered = text.lower()
    if "reauthentication failed" in lowered or "refreshing your current auth tokens" in lowered:
        message = "your gcloud session has expired"
        fix = "gcloud auth login"
    elif "do not currently have an active account" in lowered:
        message = "no active gcloud account"
        fix = "gcloud auth login"

    return message, fix, text


def quota_request_command(
    *, project: str, quota_id: str, value: int, region: str | None = None,
    justification: str | None = None,
) -> list[str]:
    """Build the quota-increase command. Kept pure so --dry-run can print it."""
    args = [
        "quotas", "preferences", "create",
        f"--service={COMPUTE_SERVICE}",
        f"--project={project}",
        f"--quota-id={quota_id}",
        f"--preferred-value={value}",
    ]
    if region:
        args.append(f"--dimensions=region={region}")
    if justification:
        args.append(f"--justification={justification}")
    return args


def console_quota_url(project: str) -> str:
    """Where a human can watch the request without this tool."""
    return f"https://console.cloud.google.com/iam-admin/quotas?project={project}"
