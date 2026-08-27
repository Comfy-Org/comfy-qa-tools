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
import sys
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

# Proving the credential is one small API call. It should never take long, and if
# it does the network is the problem, which is worth knowing before a GPU starts.
PREFLIGHT_TIMEOUT = 20


# --- what kind of failure was that ------------------------------------------
#
# "Run `gcloud auth login`" is right for exactly two of these and misleading for
# the rest. Telling a tester to sign in again when the real problem is a missing
# project, a denied permission or a dropped Wi-Fi connection sends them to fix
# something that was never broken.

REAUTH = "reauth"            # Google asked for a fresh proof of identity
CREDENTIALS = "credentials"  # the stored sign-in could not be refreshed at all
NO_ACCOUNT = "no-account"    # nobody is signed in
NO_PROJECT = "no-project"    # signed in, but no project chosen
DENIED = "denied"            # signed in and reached Google, and refused
NETWORK = "network"          # never reached Google
TIMEOUT = "timeout"          # reached Google, or did not, but ran out of clock
QUOTA = "quota"              # reached Google, allowed, and over an allowance
NO_GCLOUD = "no-gcloud"      # the binary is not here
UNKNOWN = "unknown"

# Ordered: the first match wins, so the specific signs come before the vague
# ones. "reauthentication" is checked before anything else because gcloud wraps
# it inside a generic "problem refreshing your current auth tokens" sentence that
# also fronts network failures — matching the wrapper would mislabel both.
_SIGNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (REAUTH, ("reauthentication", "reauth is required")),
    # Before CREDENTIALS: gcloud reports a refresh that failed for want of a
    # network with the same "problem refreshing your current auth tokens"
    # wrapper as one that failed for want of a valid sign-in. Signing in again
    # does not fix a dropped connection.
    (NETWORK, (
        "unable to reach",
        "could not reach",
        "temporary failure in name resolution",
        "name or service not known",
        "network is unreachable",
        "failed to establish a new connection",
        "connection aborted",
        "connection reset by peer",
        "certificate verify failed",
    )),
    (CREDENTIALS, (
        "invalid_grant",
        "token has been expired or revoked",
        "refreshing your current auth tokens",
    )),
    (NO_ACCOUNT, (
        "do not currently have an active account",
        "does not have any valid credentials",
    )),
    (NO_PROJECT, (
        "required property [project] is not currently set",
        "the project property is set to the empty string",
    )),
    # Before DENIED: a quota refusal reads like a permission problem and is not
    # one — the account is allowed, the project is simply at its limit, and
    # "check which account you are using" sends the reader nowhere.
    (QUOTA, (
        "quota exceeded",
        "quota '",
        "exceeded.  limit:",
        "exceeded quota",
    )),
    (DENIED, (
        "permission denied",
        "does not have permission",
        "required 'compute.",
        "insufficient authentication scopes",
        "caller does not have permission",
        "permission_denied",
    )),
)

# What to say and what to do about it. A kind with no entry keeps gcloud's own
# sentence, which for a denied permission names the exact role that is missing —
# better than anything this tool could write.
_ADVICE: dict[str, tuple[str | None, str | None]] = {
    REAUTH: ("your gcloud session has expired", "gcloud auth login"),
    CREDENTIALS: ("gcloud could not refresh your sign-in", "gcloud auth login"),
    NO_ACCOUNT: ("no active gcloud account", "gcloud auth login"),
    NO_PROJECT: ("no project set", "gcloud config set project <your-project-id>"),
    NETWORK: ("could not reach Google Cloud", "check your network, then try again"),
    DENIED: (None, "comfy-qat auth status — check which account you are using"),
    # gcloud's own sentence names the metric, the limit and the region, which is
    # everything needed; only the fix is worth adding.
    QUOTA: (None, "raise the limit at https://console.cloud.google.com/iam-admin/quotas "
                  "or ask for less"),
}


def classify(text: str) -> str:
    """Name the kind of failure in everything gcloud printed.

    Reads the full output rather than the one-line summary, for the same reason
    the stockout check does: gcloud's summary line is sometimes `---`.
    """
    lowered = (text or "").lower()
    for kind, signs in _SIGNS:
        if any(sign in lowered for sign in signs):
            return kind
    return UNKNOWN


def can_prompt() -> bool:
    """Could gcloud ask this terminal a question right now?

    This mirrors gcloud's own rule, which is stricter than it looks: reauth is
    only attempted when `console_io.CanPrompt()` is true, and that requires
    *stderr* to be a terminal as well as stdin
    (googlecloudsdk/core/console/console_io.py, `IsInteractive(error=True)`).

    That single detail is why a session expiry is so much worse through this tool
    than at a bare prompt. `Gcloud.run` captures stderr, so gcloud sees a pipe,
    decides it cannot prompt, and turns a ten-second re-prompt into a hard
    failure — even with a human sitting in front of the machine.
    """
    try:
        return bool(sys.stdin.isatty() and sys.stderr.isatty())
    except (AttributeError, ValueError):  # a closed or replaced stream
        return False


class GcloudError(Exception):
    """A gcloud call that failed.

    `fix` is a command the user can run. `raw` is everything gcloud printed —
    kept because the one-line summary is not always enough to classify a failure,
    and classifying on the summary meant a capacity stockout went unrecognised.
    `kind` is that classification, so callers can tell an expired session from a
    missing project without matching on prose.
    """

    def __init__(
        self, message: str, fix: str | None = None, raw: str = "",
        kind: str = UNKNOWN,
    ) -> None:
        super().__init__(message)
        self.fix = fix
        self.raw = raw or message
        self.kind = kind

    @property
    def is_auth(self) -> bool:
        """Would signing in again fix this?"""
        return self.kind in (REAUTH, CREDENTIALS, NO_ACCOUNT)


@dataclass
class Gcloud:
    """Thin wrapper. Swap `runner` in tests; nothing else needs mocking."""

    timeout: int = DEFAULT_TIMEOUT
    runner: object = field(default=None, repr=False)

    # Set once anything has actually reached Google with these credentials.
    # A successful call is the best possible proof, so the preflight below
    # costs nothing on the common path.
    proven: bool = field(default=False, repr=False)

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
                kind=NO_GCLOUD,
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
                kind=TIMEOUT,
            ) from exc

        if proc.returncode != 0:
            message, fix, raw = explain_failure(proc.stderr, proc.stdout, proc.returncode)
            raise GcloudError(message, fix=fix, raw=raw, kind=classify(raw))

        if not _is_local_only(args):
            self.proven = True

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
                kind=NO_GCLOUD,
            )
        return subprocess.run([exe, *args]).returncode

    # --- is this going to work before we spend money on it? ----------------

    def preflight(self, project: str | None = None) -> None:
        """Prove the credentials work, before starting something slow or billable.

        Failing here costs a second. Failing fifteen minutes in costs a GPU box
        that is already running, already billing, and has nothing to show for it
        — and the message you get then describes the step that happened to be
        holding the credential, not the credential.

        Order matters: gcloud on PATH, somebody signed in, a project chosen, and
        only then one small call to Google. The first three are local and free,
        and each one distinguishes a failure the last one would blur together.
        """
        if self.proven:
            return

        if self.available() is None:
            raise GcloudError(
                "gcloud is not installed or not on PATH.",
                fix="https://cloud.google.com/sdk/docs/install",
                kind=NO_GCLOUD,
            )

        if not self.active_account():
            raise GcloudError(
                "no active gcloud account", fix="gcloud auth login", kind=NO_ACCOUNT,
            )

        project = project or self.current_project()
        if not project:
            raise GcloudError(
                "no project set",
                fix="gcloud config set project <your-project-id>",
                kind=NO_PROJECT,
            )

        self._prove(project)

    def _prove(self, project: str) -> None:
        """One small call, purely to make gcloud refresh the token and be judged.

        Any answer except an authentication failure counts as proof. A denied
        permission in particular is a *pass*: to be refused, the credential had
        to be accepted first. Treating it as a failure would block a tester whose
        account simply cannot read project metadata, which is a worse bug than
        the one this is preventing.
        """
        try:
            self.run(["projects", "describe", project], timeout=PREFLIGHT_TIMEOUT)
        except GcloudError as exc:
            if exc.kind == DENIED:
                self.proven = True
                return
            if exc.kind != REAUTH or not can_prompt():
                raise
            # Google is asking for a fresh proof of identity, and this terminal
            # can answer it. gcloud will only ask when it owns stderr, which
            # `run` does not give it — so hand the terminal over and let it.
            self._rescue(project, exc)

    def _rescue(self, project: str, failure: GcloudError) -> None:
        """Let gcloud put its reauth prompt on the terminal, once.

        This is the whole difference between a ten-second interruption and a dead
        command: the challenge was always answerable, it just had nowhere to
        appear. Nothing here signs anybody in — `gcloud auth login` stays the
        tester's own command — and this only ever runs before the billable work,
        never in the middle of it.
        """
        code = self.run_interactive(
            ["projects", "describe", project, "--format=none"]
        )
        if code != 0:
            raise failure
        self.proven = True

    def _ready_for(self, project: str | None) -> None:
        """The gate the slow and billable calls go through.

        A no-op when a runner is injected: that seam means there is no real
        gcloud and no real credential, so there is nothing a preflight could
        prove. The check belongs to the subprocess path only.
        """
        if self.runner is not None:
            return
        self.preflight(project)

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
        # From here on the project is being charged. Everything below this line
        # checks the credential first, and nothing above it needs to: a read that
        # fails costs a second and says so.
        self._ready_for(project)
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
            raise GcloudError("gcloud is not installed or not on PATH.", kind=NO_GCLOUD)
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
            raise GcloudError("gcloud is not installed or not on PATH.", kind=NO_GCLOUD)
        proc = subprocess.run([exe, *args], capture_output=True, text=True, timeout=INSTANCE_TIMEOUT)
        if proc.returncode != 0:
            message, fix, raw = explain_failure(proc.stderr, proc.stdout, proc.returncode)
            raise GcloudError(message, fix=fix, raw=raw)
        return proc.stdout.strip()

    def describe_instance(self, name: str, zone: str, project: str) -> dict:
        return self.run([
            "compute", "instances", "describe", name,
            f"--zone={zone}", f"--project={project}",
        ]) or {}

    def snapshot_disk(self, disk: str, zone: str, project: str, snapshot: str) -> None:
        self._ready_for(project)
        self.run([
            "compute", "disks", "snapshot", disk,
            f"--zone={zone}", f"--project={project}",
            f"--snapshot-names={snapshot}",
        ], parse_json=False, timeout=INSTANCE_TIMEOUT)

    def create_disk_from_snapshot(self, disk: str, zone: str, project: str, snapshot: str) -> None:
        self._ready_for(project)
        self.run([
            "compute", "disks", "create", disk,
            f"--zone={zone}", f"--project={project}",
            f"--source-snapshot={snapshot}",
        ], parse_json=False, timeout=INSTANCE_TIMEOUT)

    def create_instance_from_disk(
        self, name: str, zone: str, project: str, disk: str, machine_type: str,
        metadata: str | None = None, *, external_ip: bool = False,
        network: str | None = None, subnet: str | None = None,
    ) -> None:
        self._ready_for(project)
        args = [
            "compute", "instances", "create", name,
            f"--zone={zone}", f"--project={project}",
            f"--machine-type={machine_type}",
            f"--disk=name={disk},boot=yes,auto-delete=no",
        ]
        # `--no-address` used to be hardcoded here, reasoning that IAP does not
        # need a public IP. True for reaching the box; nothing about the box
        # reaching pypi. With no Cloud NAT on the project that left a machine
        # with no egress at all, so a moved box could never install or update
        # anything. The caller passes what the source instance actually has.
        # Omitting the flag is how you ask GCE for the ephemeral address it
        # gives by default; there is no affirmative flag to pass.
        if not external_ip:
            args.append("--no-address")
        if network:
            args.append(f"--network={network}")
        if subnet:
            args.append(f"--subnet={subnet}")
        if metadata:
            args.append(f"--metadata={metadata}")
        self.run(args, parse_json=False, timeout=INSTANCE_TIMEOUT)

    def delete_snapshot(self, snapshot: str, project: str) -> None:
        self.run([
            "compute", "snapshots", "delete", snapshot,
            f"--project={project}", "--quiet",
        ], parse_json=False, timeout=INSTANCE_TIMEOUT)

    def quota_preferences(self, project: str) -> list[dict]:
        return self.run([
            "quotas", "preferences", "list", f"--project={project}",
        ]) or []


# gcloud groups that answer from the local config and credential store without
# calling Google. Succeeding at one of these proves a file was readable, not that
# the credential still works — `gcloud auth list` prints a happy account list
# with a session that expired hours ago. Misjudging in this direction is safe:
# it costs one redundant preflight. Misjudging the other way is the bug.
_LOCAL_ONLY_GROUPS = frozenset({"config", "auth", "components", "topic", "version", "help"})


def _is_local_only(args: list[str]) -> bool:
    return bool(args) and args[0] in _LOCAL_ONLY_GROUPS


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

    Where `classify` recognises the failure, its plainer sentence and its fix
    replace gcloud's. Where it does not, gcloud's own words are kept: they are
    usually specific and this tool has nothing better to say.
    """
    text = (stderr or stdout or "").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return f"gcloud exited {returncode}", None, text

    message = next((line for line in lines if line.startswith("ERROR:")), lines[0])
    message = message.removeprefix("ERROR:").strip()
    # Drop the "(gcloud.billing.projects.describe)" breadcrumb; the caller knows.
    message = re.sub(r"^\(gcloud\.[^)]*\)\s*", "", message)

    # gcloud ends a summary with a colon and puts the reason underneath:
    #
    #   ERROR: (gcloud.compute.disks.create) Could not fetch resource:
    #    - Quota 'SSD_TOTAL_GB' exceeded.  Limit: 500.0 in region us-central1.
    #
    # Reporting only the first line gave "Could not fetch resource:" and nothing
    # else — a message that names no cause and suggests no action. It cost a
    # round of live testing to find out it meant a disk quota.
    if message.endswith(":"):
        detail = _detail_after(lines, message)
        if detail:
            message = f"{message.rstrip(':')}: {detail}"

    if _is_separator(message):
        message = localized_message(text) or next(
            (line for line in lines if not _is_separator(line) and not line.startswith("ERROR:")),
            f"gcloud exited {returncode}",
        )

    plainer, fix = _ADVICE.get(classify(text), (None, None))
    if plainer:
        message = plainer

    return message, fix, text


def _detail_after(lines: list[str], summary: str) -> str:
    """The explanation gcloud printed under a summary line ending in a colon.

    Stops at the pointers gcloud appends — "Try your request in another zone",
    a documentation URL — because those are advice, not the cause, and the fix
    line is where advice belongs.
    """
    try:
        start = next(i for i, line in enumerate(lines) if line.endswith(summary))
    except StopIteration:
        return ""

    detail = []
    for line in lines[start + 1:]:
        low = line.lower()
        if low.startswith(("try ", "see ", "http", "for more", "if you would like")):
            break
        detail.append(line.lstrip("- ").strip())
        if len(detail) >= 3:  # enough to name a cause; the rest is in `raw`
            break
    return " ".join(part for part in detail if part)


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
