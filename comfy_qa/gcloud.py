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


class GcloudError(Exception):
    """A gcloud call that failed. `fix` is a command the user can run."""

    def __init__(self, message: str, fix: str | None = None) -> None:
        super().__init__(message)
        self.fix = fix


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

    def run(self, args: list[str], *, parse_json: bool = True):
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
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout
            )
        except subprocess.TimeoutExpired as exc:
            raise GcloudError(f"gcloud timed out after {self.timeout}s: {' '.join(args)}") from exc

        if proc.returncode != 0:
            message, fix = explain_failure(proc.stderr, proc.stdout, proc.returncode)
            raise GcloudError(message, fix=fix)

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
        ]) or []
        return [q for q in infos if "GPU" in (q.get("quotaId") or "").upper()]

    def quota_preferences(self, project: str) -> list[dict]:
        return self.run([
            "quotas", "preferences", "list", f"--project={project}",
        ]) or []


def explain_failure(stderr: str | None, stdout: str | None, returncode: int):
    """Turn gcloud's multi-line output into one line plus the command that fixes it.

    gcloud writes long, friendly errors across many lines. Taking the last line
    yields a fragment like "to select an already authenticated account to use."
    — technically from the error, useless on its own. The ERROR: line is the one
    that says what actually went wrong.
    """
    text = (stderr or stdout or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return f"gcloud exited {returncode}", None

    message = next((line for line in lines if line.startswith("ERROR:")), lines[0])
    message = message.removeprefix("ERROR:").strip()
    # Drop the "(gcloud.billing.projects.describe)" breadcrumb; the caller knows.
    message = re.sub(r"^\(gcloud\.[^)]*\)\s*", "", message)

    fix = None
    lowered = text.lower()
    if "reauthentication failed" in lowered or "refreshing your current auth tokens" in lowered:
        message = "your gcloud session has expired"
        fix = "gcloud auth login"
    elif "do not currently have an active account" in lowered:
        message = "no active gcloud account"
        fix = "gcloud auth login"

    return message, fix


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
