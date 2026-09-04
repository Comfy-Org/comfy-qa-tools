"""The one place this tool shells out to gcloud.

Everything that talks to Google Cloud goes through `Gcloud.run`, for two reasons:
tests replace a single seam rather than patching subprocess everywhere, and every
failure can be turned into a message that names the command which fixes it.

One seam is also why gcloud's output can be held to the same rule as our own.
`say` bans colour and anything that redraws, because a run gets read twice — in a
terminal and in a Slack code block — and only whole plain lines survive the
second reading. That rule stopped at the edge of this file until a live `go` put
gcloud's yellow `WARNING:` into the middle of an install log. It does not stop
there now: see `relay_output` and `Relay` below.

gcloud is already on PATH on a machine that has it. Never prepend the SDK bin
directory — it bloats the command for no benefit.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
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


# --- somebody else's output, on its way into a bug report --------------------
#
# `say` holds one rule over everything this tool writes: no colour, no cursor
# movement, nothing that redraws, because the output is read twice — once in a
# terminal and once in a Slack code block — and only whole plain lines survive
# the second reading. `test_say.py` proves it over every string constant in the
# package, which proves it about the half of the output we write and nothing at
# all about the half we relay.
#
# A real `go` run proved the gap. Between "starting ComfyUI" and "STARTED" the
# terminal carried this, in yellow:
#
#     <esc>[1;33mWARNING:<esc>[0m
#
#     To increase the performance of the tunnel, consider installing NumPy. For
#     instructions, please see https://cloud.google.com/iap/docs/...
#
# Two separate faults. The escape sequences are ours to fix and there is no
# argument for them. The advice is gcloud's, it is about gcloud's own transfer
# speed, and nobody reading it is in a position to act on it — but it lands in
# the middle of an install and reads like something went wrong.

# CSI (colour, cursor movement, erase), OSC (window titles), and the two-character
# escapes. Matched per line, so the OSC's lazy body cannot run away.
_ESCAPE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b[@-Z\\-_]"
)

# Whatever the pattern above did not recognise. An escape sequence this tool has
# never seen still must not reach a paste, so the guarantee is closed by removing
# every remaining control character rather than by predicting them. Tab stays: it
# is ordinary in a log and prints as itself.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# The line ending, and then everything a progress bar redrew over. Greedy, so it
# reaches the *last* carriage return: what a terminal ends up showing is only ever
# what came after it.
_LINE_END = re.compile(r"[\r\n]+\Z")
_REDRAWN = re.compile(r"^.*\r", re.DOTALL)

# Lines that are true, harmless, and about gcloud rather than about anything a
# user of this tool can do. Matched on a lowercased prefix of the whole cleaned
# line. Only advisories go here — never anything that could be a symptom, and
# never anything on a failure path. See `Relay` for why the list is this short.
KNOWN_NOISE = (
    "to increase the performance of the tunnel, consider installing numpy",
    "please see https://cloud.google.com/iap/docs/using-tcp-forwarding",
)

# A header with nothing after the colon is not a message, it is a label for the
# block underneath it. gcloud prints one before the advisory above, so it has to
# be decided together with what follows — see `Relay.line`. Lowercase because
# `test_say.py` holds the whole package to one spelling of the word.
_BARE_HEADER = ("warning:", "warn:")

# How long a finished command waits for the last of its output. Reaching this
# would mean a reader thread wedged on a pipe that never closed; the exit code is
# already known by then, and hanging on to it would be worse than the loss.
PUMP_TIMEOUT = 5.0


def plain(text: str) -> str:
    """One line of someone else's output, made safe to read and to paste.

    Carriage returns are resolved rather than deleted: a progress bar rewrites
    one line over and over with `\\r`, and what a terminal ends up showing is
    only ever the part after the last one. Taking that keeps the final state —
    `100%` — and drops the ninety-nine redraws in front of it, which is the same
    answer `say` gives for our own long steps.

    Both are patterns rather than the two-character literals they could be,
    because `test_say.py` holds every string constant in this package to "no
    escape sequence, no carriage return" — and it is right to: a rule with an
    exemption for the module that enforces it is not a rule. A raw pattern says
    the same thing without carrying one.
    """
    return _CONTROL.sub("", _ESCAPE.sub("", _REDRAWN.sub("", _LINE_END.sub("", text))))


def readable(text: str) -> str:
    """A whole captured block, cleaned — and complete.

    Every line survives. This is the one used on anything that failed, where the
    line that explains it could be any of them and dropping the wrong one costs
    a great deal more than a colour code ever did.
    """
    return "\n".join(plain(line) for line in (text or "").splitlines())


def is_noise(line: str) -> bool:
    """Is this one of the lines in `KNOWN_NOISE`?"""
    lowered = line.strip().lower()
    return any(lowered.startswith(sign) for sign in KNOWN_NOISE)


class Relay:
    """One stream of someone else's output, cleaned, with its noise dropped.

    Stateful for one reason: gcloud writes the advisory as a block — a bare
    header, a blank line, then the prose — and whether the header is worth
    printing is not knowable until the prose arrives. So a contentless header is
    held back, and the line that follows decides it: dropped with the noise it
    belonged to, or printed in front of the real message it announced.

    **Nothing on a failure path is ever dropped here.** A silenced error costs
    more than every coloured one put together, so this filters the live relay
    only; `explain_failure` cleans gcloud's words and keeps all of them.
    """

    def __init__(self) -> None:
        self._held: list[str] = []

    def line(self, text: str) -> list[str]:
        """What to print for one line in. Usually itself; sometimes nothing."""
        cleaned = plain(text)
        if is_noise(cleaned):
            # The header, if there is one, was this advisory's. It goes with it.
            self._held = []
            return []
        if cleaned.strip().lower() in _BARE_HEADER:
            self._held = [cleaned]
            return []
        if self._held and not cleaned.strip():
            self._held.append(cleaned)  # the blank line inside the block
            return []
        held, self._held = self._held, []
        return [*held, cleaned]

    def rest(self) -> list[str]:
        """Anything still held when the stream ended. Held is not dropped."""
        held, self._held = self._held, []
        return held


def _emit(stream, line: str) -> None:
    """Write one whole line, now.

    Flushed every time, because the point of streaming an install log is watching
    it arrive. A closed stream is not an error worth failing a command over:
    `comfy-qat logs | head` closes the pipe on purpose.
    """
    try:
        stream.write(line + "\n")
        stream.flush()
    except (OSError, ValueError):
        pass


# What a reader says when it could not finish. A log that simply stops reads
# exactly like a command that finished, which is the one thing it must not be
# mistaken for — a truncated install log has a last line that looks like a
# result. Lowercase, so `test_say.py`'s one spelling of the word still holds.
CUT_SHORT = "warning: the rest of this output was lost"


def _pump(reader, write, note) -> None:
    """Read one stream to its end, cleaning it a line at a time.

    `note` is where a failure to finish is reported, and it is always stderr —
    including for the stdout pump, because a line about our own trouble is the
    story and not the answer. Swallowing it silently was the earlier version and
    it was wrong: everything after the break is missing, and nothing said so.
    """
    relay = Relay()
    try:
        for raw in iter(reader.readline, b""):
            for line in relay.line(raw.decode("utf-8", "replace")):
                write(line)
    except (OSError, ValueError) as exc:
        for line in relay.rest():
            write(line)
        note(f"{CUT_SHORT} ({exc})")
        return
    for line in relay.rest():
        write(line)


def _writer(given, name: str):
    """Where a pumped stream goes.

    `sys.stdout` is looked up at write time rather than captured here, so a
    caller that replaced it — a test, a pipeline — is the one written to.
    """
    def write(line: str) -> None:
        _emit(given if given is not None else getattr(sys, name), line)
    return write


def relay_output(cmd: list[str], *, out=None, err=None) -> int:
    """Run `cmd` with its output coming *through* this tool, not past it.

    The alternative, and what this replaces, is letting the child inherit the
    terminal. That is one line of code and it is why gcloud's yellow reached a
    bug report: output nobody handles is output nobody can hold to the rule.

    Each stream keeps its own side — the box's log stays on stdout, gcloud's
    commentary on stderr — because `comfy-qat logs > run.log` has to collect the
    log and not the story. Two readers rather than one merged pipe, for the same
    reason.

    Ctrl-C is left exactly as it was. The child shares this terminal's process
    group, so the signal reaches it directly; all this does is wait for it to
    finish writing before the exception carries on up, so the last lines of a
    stopped ComfyUI are not lost.
    """
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
        to_out = _writer(out, "stdout")
        to_err = _writer(err, "stderr")
        pumps = [
            threading.Thread(target=_pump, args=(process.stdout, to_out, to_err),
                             daemon=True),
            threading.Thread(target=_pump, args=(process.stderr, to_err, to_err),
                             daemon=True),
        ]
        for pump in pumps:
            pump.start()
        try:
            code = process.wait()
        except KeyboardInterrupt:
            code = process.wait()
            for pump in pumps:
                pump.join(timeout=PUMP_TIMEOUT)
            raise
        for pump in pumps:
            pump.join(timeout=PUMP_TIMEOUT)
    return code


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

    def require(self) -> str:
        """The gcloud binary, or the refusal that names what is missing.

        `run` has always made this check on its way to a subprocess. `ssh` and
        `rdp` never reach `run` — they hand an argv to `os.execvp` — so without
        this they turned an absent gcloud into a `FileNotFoundError` traceback
        where every other command prints a sentence and the install link.

        Every site that needs the binary now asks here, which is the difference
        between one place and the first of several. There were five: two spelled
        the message with the install link, and two — `ssh` and `ssh_output` —
        spelled it without, so the same missing binary told two different people
        two different things and only one of them where to get it.
        """
        exe = self.available()
        if exe is None:
            raise GcloudError(
                "gcloud is not installed or not on PATH.",
                fix="https://cloud.google.com/sdk/docs/install",
                kind=NO_GCLOUD,
            )
        return exe

    def run(self, args: list[str], *, parse_json: bool = True, timeout: int | None = None):
        """Run `gcloud <args>`. Returns parsed JSON, or raw text if parse_json is off."""
        if self.runner is not None:
            return self.runner(args, parse_json)

        cmd = [self.require(), *args]
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

        return subprocess.run([self.require(), *args]).returncode

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

        self.require()

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

    def region_quotas(self, region: str, project: str) -> dict[str, tuple[float, float]]:
        """Usage and limit per metric for one region, keyed by metric name.

        Deliberately not `quotas info list`, which is where gpu_quotas reads
        from: that gives limits without usage, and "is there room" needs both.
        `regions describe` carries the pair.

        Returns an empty dict rather than raising. This exists to make a plan
        more honest, and a plan that cannot be improved is still a plan — it must
        never be the reason a move that would have worked is refused.
        """
        try:
            described = self.run([
                "compute", "regions", "describe", region,
                f"--project={project}",
            ], timeout=QUOTA_TIMEOUT) or {}
        except GcloudError:
            return {}
        out: dict[str, tuple[float, float]] = {}
        for quota in described.get("quotas") or []:
            metric = quota.get("metric")
            if metric:
                out[metric] = (float(quota.get("usage") or 0),
                               float(quota.get("limit") or 0))
        return out

    def list_instances(self, project: str) -> list[dict]:
        """Every Compute Engine instance on the project, across all zones."""
        return self.run([
            "compute", "instances", "list", f"--project={project}",
        ]) or []

    #: What `instance_status` returns when Google answered but said nothing about
    #: the machine's state. It is deliberately NOT a state name: callers compare
    #: against TERMINATED and against RUNNING, and any string that is neither gets
    #: treated as a real transitional state — so a describe that came back empty
    #: was reported as "the machine is unknown — it started, and it is billing",
    #: asserting a bill on no evidence at all.
    UNKNOWN_STATE = ""

    def instance_status(self, name: str, zone: str, project: str) -> str:
        """RUNNING, TERMINATED, STAGING... TERMINATED is Google's word for stopped.

        An empty string means the read succeeded and told us nothing, which is a
        third answer and not a state.
        """
        info = self.run([
            "compute", "instances", "describe", name,
            f"--zone={zone}", f"--project={project}",
        ]) or {}
        return info.get("status") or self.UNKNOWN_STATE

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

        A remote ComfyUI's startup log appears as it arrives, exactly as it would
        if it were running locally. That is the whole point: a remote launch you
        cannot watch is a launch you cannot debug.

        It arrives through `relay_output` rather than by handing the child this
        terminal. Inheriting was simpler and it is what put gcloud's yellow
        `WARNING:` — its own advice about its own transfer speed — into the middle
        of an install log that then got pasted into Slack. Output nobody handles
        is output nobody can hold to the rule in `say`.

        **`--quiet` is not tidiness, it is the condition for piping at all.** On a
        machine that has never run `gcloud compute ssh`, the first one generates
        `~/.ssh/google_compute_engine` and asks `Enter passphrase (empty for no
        passphrase):` — a prompt with no trailing newline, which `_pump` reads by
        line and would therefore never show, from `ssh-keygen`, which reads the
        answer from `/dev/tty` and not from anything we could feed. A `go` on a
        fresh machine would stop dead with no output and no explanation: exactly
        the "hide the prompt and hang" failure `run_interactive`'s docstring
        warns about, reintroduced one file over.

        `gcloud compute ssh --help` says what the flag does here, in its own
        words: "If the user does not have a public SSH key, one is generated
        using ssh-keygen(1) (if the --quiet flag is given, the generated key will
        have an empty passphrase)." Nothing in this tool creates that key, and
        everything in it depends on the key existing.
        """
        args = [
            "compute", "ssh", instance,
            f"--zone={zone}", f"--project={project}",
            "--tunnel-through-iap", "--quiet", f"--command={remote}",
        ]
        if self.runner is not None:
            return self.runner(args, "stream" if stream else True)

        return relay_output([self.require(), *args])

    def ssh_argv(self, instance: str, zone: str, project: str) -> list[str]:
        """The command that opens an interactive shell on a box.

        Returned rather than run: `ssh` replaces this process with it, so the
        shell owns the terminal and Ctrl-C reaches the remote side rather than a
        wrapper around it.

        **Where the output guarantee ends.** `relay_output` can hold gcloud's
        output to `say`'s rule because it is between the child and the terminal.
        `execvp` leaves nothing in between — this process is gone and the shell
        owns the terminal from that line — so colour, progress bars and gcloud's
        own advisories all reach the screen here, by construction. That is the
        right trade for an interactive shell, which wants the terminal it is
        being given; it is worth knowing rather than fixing. `require()` is
        called before the hand-off because a refusal is the one thing that must
        still be ours to word.
        """
        return [
            "gcloud", "compute", "ssh", instance,
            f"--zone={zone}", f"--project={project}", "--tunnel-through-iap",
        ]

    def rdp_argv(self, instance: str, zone: str, project: str,
                 local_port: int) -> list[str]:
        """The command that forwards Remote Desktop from the box to this Mac.

        Also handed to `execvp`, so the note on `ssh_argv` applies here too — and
        with more force, because this is the one IAP forward a tester watches
        directly, and the NumPy advisory `Relay` drops everywhere else will
        appear on this screen. It is gcloud's terminal by then.
        """
        return [
            "gcloud", "compute", "start-iap-tunnel", instance, "3389",
            f"--local-host-port=localhost:{local_port}",
            f"--zone={zone}", f"--project={project}",
        ]

    def windows_password(self, instance: str, zone: str, project: str) -> dict:
        """Reset the box's Windows password and return the new credentials.

        Google documents no way to read the existing one — resetting is the only
        route in, and it is what their own instructions tell you to do.

        Raises rather than answering with something falsy, and that is the whole
        of the fix. `run` returns `None` when gcloud exits 0 with an empty stdout,
        and this ended `or {}` — so no exception was raised, `rdp` printed a blank
        username and a blank password laid out exactly like a real pair, said it
        was forwarding RDP, and `execvp`'d away. The tester found out at a Windows
        login prompt they could not get past, with nothing in our output pointing
        back at us. A password this tool cannot produce is a failure; the only
        honest shapes to return are the credentials or an exception.

        The message names the keys that came back and never a value. If a
        password *is* in there, it is the one thing on this box worth not putting
        into an error that gets pasted somewhere.
        """
        answer = self.run([
            "compute", "reset-windows-password", instance,
            f"--zone={zone}", f"--project={project}", "--quiet",
        ])
        if isinstance(answer, dict) and answer.get("username") and answer.get("password"):
            return answer
        raise GcloudError(
            f"gcloud reset the Windows password on {instance} and exited without "
            f"an error, but the answer carried no credentials: {_shape(answer)}. "
            f"There is no password to hand over, so nothing was forwarded.",
            fix=(
                f"run it yourself and read what comes back: gcloud compute "
                f"reset-windows-password {instance} --zone={zone} "
                f"--project={project}"
            ),
        )

    def ssh_output(self, instance: str, zone: str, project: str, remote: str) -> str:
        """Run a command on the instance and return what it printed.

        Cleaned on the way back, because what a box printed is both branched on
        and quoted: `INSTALLED`, a CUDA version, the name of the process holding
        port 8188. A colour code around any of those is a word this tool then
        fails to recognise and a message it then puts in front of a person.

        `--quiet` for the reason given on `ssh`, and one worse here: this one
        captures its output, so a first-run key-generation prompt would not be on
        the screen even in principle. It would sit invisible for the full
        `INSTANCE_TIMEOUT` and come back as "gcloud timed out", which names the
        network for a question nobody was shown.
        """
        args = [
            "compute", "ssh", instance,
            f"--zone={zone}", f"--project={project}",
            "--tunnel-through-iap", "--quiet", f"--command={remote}",
        ]
        if self.runner is not None:
            return self.runner(args, "output")

        proc = subprocess.run([self.require(), *args], capture_output=True,
                              text=True, timeout=INSTANCE_TIMEOUT)
        if proc.returncode != 0:
            message, fix, raw = explain_failure(proc.stderr, proc.stdout, proc.returncode)
            raise GcloudError(message, fix=fix, raw=raw)
        return readable(proc.stdout).strip()

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
        metadata: str | None = None, *, accelerator: str | None = None,
        external_ip: bool = False,
        network: str | None = None, subnet: str | None = None,
    ) -> None:
        """Recreate a box in another zone from a copy of its disk.

        `accelerator` mirrors `create_instance_from_image`: passed only for the
        N1 families, where the card is attached rather than built into the
        machine type, and omitted for G2/A2/A3, where passing it is refused.
        Leaving it off entirely — which this did until 2026-09-01 — moves a T4
        box and hands back an `n1-standard-8` with no GPU, reporting success.

        `--maintenance-policy=TERMINATE` goes with it for the same reason it does
        there: an accelerator cannot live-migrate and Google refuses the create
        without it. It is scoped to the accelerator case rather than set always,
        so moving a CPU box keeps whatever policy it had.
        """
        self._ready_for(project)
        args = [
            "compute", "instances", "create", name,
            f"--zone={zone}", f"--project={project}",
            f"--machine-type={machine_type}",
            f"--disk=name={disk},boot=yes,auto-delete=no",
        ]
        if accelerator:
            args.append(f"--accelerator={accelerator}")
            args.append("--maintenance-policy=TERMINATE")
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

    def accelerator_types(self, project: str, name: str) -> list[dict]:
        """Every zone that offers one card. `name` is Google's own id, `nvidia-l4`.

        Filtered server-side to keep the payload small, and filtered again by the
        caller: gcloud warns on every call that its `=` operator is changing to
        match more than it does today, and the day it does, `nvidia-l4` also
        returns `nvidia-l4-vws`.
        """
        return self.run([
            "compute", "accelerator-types", "list",
            f"--project={project}", f"--filter=name={name}",
        ]) or []

    def machine_types(self, project: str, zones: list[str], name: str) -> list[dict]:
        """Whether these zones offer one machine type. Zone-scoped, so it is quick."""
        if not zones:
            return []
        return self.run([
            "compute", "machine-types", "list",
            f"--project={project}", f"--zones={','.join(zones)}",
            f"--filter=name={name}",
        ]) or []

    def create_instance_from_image(
        self, name: str, zone: str, project: str, *, machine_type: str,
        image_family: str, image_project: str, disk_gb: int,
        disk_type: str = "pd-balanced", accelerator: str | None = None,
        metadata: str | None = None, metadata_from_file: str | None = None,
    ) -> None:
        """Create a new box from a public image. From here the project is billed.

        `accelerator` is passed only for the machine families where the GPU is a
        separate thing you attach — N1. G2 and A2 have the card built into the
        machine type, and passing `--accelerator` alongside one of those is
        refused by Google, which is the most common way a create by hand fails.

        `--maintenance-policy=TERMINATE` is not optional on a GPU box: an
        accelerator cannot live-migrate, and Google refuses the create without
        it rather than choosing for you.

        No `--no-address` here, deliberately, and for the reason `move` learned:
        with no Cloud NAT on the project, a box with no external address has no
        egress at all, and a machine that cannot reach pypi cannot install
        ComfyUI. The tunnel does not need the address; the install does.
        """
        self._ready_for(project)
        args = [
            "compute", "instances", "create", name,
            f"--zone={zone}", f"--project={project}",
            f"--machine-type={machine_type}",
            f"--image-family={image_family}", f"--image-project={image_project}",
            f"--boot-disk-size={disk_gb}GB", f"--boot-disk-type={disk_type}",
            f"--boot-disk-device-name={name}",
            "--maintenance-policy=TERMINATE",
        ]
        if accelerator:
            args.append(f"--accelerator={accelerator}")
        if metadata:
            args.append(f"--metadata={metadata}")
        if metadata_from_file:
            args.append(f"--metadata-from-file={metadata_from_file}")
        self.run(args, parse_json=False, timeout=INSTANCE_TIMEOUT)

    def firewall_rules(self, project: str) -> list[dict]:
        return self.run(["compute", "firewall-rules", "list", f"--project={project}"]) or []

    def create_firewall_rule(self, name: str, project: str, *, network: str,
                             rules: str, source_ranges: str, description: str) -> None:
        """Open one port to one source range. Never to the internet."""
        self.run([
            "compute", "firewall-rules", "create", name,
            f"--project={project}", f"--network={network}",
            "--direction=INGRESS", "--action=allow",
            f"--rules={rules}", f"--source-ranges={source_ranges}",
            f"--description={description}",
        ], parse_json=False, timeout=120)

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


def _shape(answer: object) -> str:
    """What came back, described without repeating any of it.

    Used where the answer was the wrong shape and the message has to say so.
    Names the keys and never the values: the one call this is used on is the one
    that resets a Windows password, and an error goes into a paste.
    """
    if answer is None:
        return "nothing at all"
    if isinstance(answer, dict):
        keys = ", ".join(sorted(str(key) for key in answer)) or "no keys"
        return f"a table carrying {keys}"
    return f"a {type(answer).__name__}"


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

    Kept in full and merely cleaned. gcloud does not colour what it writes to a
    pipe, so in practice there is nothing here to strip — but `message` and `raw`
    are printed and pasted like everything else, and a guarantee with a hole in
    it for the failure path is not one.
    """
    text = readable(stderr or stdout or "").strip()
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
