"""`comfy-qa-tools auth` — is this machine ready to reach Google Cloud, and as whom?

This tool never implements authentication and never holds a credential. gcloud
already owns Google Cloud auth, keeps credentials in its own store and handles
refresh; wrapping that would mean caching tokens on a QA laptop for no gain.

So `auth` answers a question and hands over the command that fixes the answer.
No token is ever printed, logged, or written to a file here.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Annotated, Callable, Optional

import typer

from . import say
from .quota import (
    GLOBAL_ALLOWANCE,
    available_gpus,
    matches,
    readiness,
    resolve,
    summarise,
)
from .gcloud import (
    Gcloud,
    GcloudError,
    console_quota_url,
    quota_request_command,
)

app = typer.Typer(help="Google Cloud sign-in, billing and GPU quota.")
quota_app = typer.Typer(help="GPU quota: what you have, and how to ask for more.")
app.add_typer(quota_app, name="quota")

# How long `request` will wait before handing you back — for the whole command,
# however many cards it asked for. Approval can take days, so waiting forever is
# not an option; the same command re-enters the wait. Both are read at call time,
# so a test can shorten them without a 30-minute suite.
WAIT_TIMEOUT_SECONDS = 30 * 60
POLL_SECONDS = 30


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str | None = None


def run_checks(gc: Gcloud) -> list[Check]:
    """Check readiness in order, stopping at the first failure.

    Stopping is deliberate: every later check depends on the earlier ones, so
    reporting five failures when one thing is wrong is noise.
    """
    results: list[Check] = []

    if gc.available() is None:
        results.append(Check(
            "gcloud", False, "not installed or not on PATH",
            "https://cloud.google.com/sdk/docs/install",
        ))
        return results
    results.append(Check("gcloud", True, "on PATH"))

    try:
        account = gc.active_account()
    except GcloudError as exc:
        results.append(Check("account", False, str(exc), exc.fix or "gcloud auth login"))
        return results
    if not account:
        results.append(Check("account", False, "nobody signed in", "gcloud auth login"))
        return results
    results.append(Check("account", True, account))

    try:
        project = gc.current_project()
    except GcloudError as exc:
        results.append(Check("project", False, str(exc), exc.fix or "gcloud config set project <id>"))
        return results
    if not project:
        results.append(Check(
            "project", False, "no project set", "gcloud config set project <id>",
        ))
        return results
    results.append(Check("project", True, project))

    try:
        billing = gc.billing_enabled(project)
    except GcloudError as exc:
        results.append(Check(
            "billing", False, str(exc),
            exc.fix or f"https://console.cloud.google.com/billing/linkedaccount?project={project}",
        ))
        return results
    if not billing:
        results.append(Check(
            "billing", False, f"no billing account linked to {project}",
            f"https://console.cloud.google.com/billing/linkedaccount?project={project}",
        ))
        return results
    results.append(Check("billing", True, f"linked to {project}"))

    try:
        quotas = gc.gpu_quotas(project)
    except GcloudError as exc:
        results.append(Check("gpu quota", False, str(exc), exc.fix or "comfy-qat quota"))
        return results

    # Read through the same filter the rest of the tool uses. Counting any quota
    # with a non-zero value meant a project holding nothing but a committed or
    # preemptible allowance passed this check — and then could not start a box.
    # Setup was fixed for exactly this; status was reporting green beside it.
    request_fix = "comfy-qat quota request --gpu <type> --region <region>"
    usable = [row for row in readiness(quotas) if row.usable]
    cards = [row for row in usable if row.gpu != GLOBAL_ALLOWANCE]
    if not usable:
        results.append(Check(
            "gpu quota", False,
            "zero GPU quota on this project — no GPU instance can start",
            request_fix,
        ))
        return results
    if not cards:
        # The project-wide ceiling is not a card. On its own it starts nothing.
        results.append(Check(
            "gpu quota", False,
            "a project-wide allowance only, no specific card granted",
            request_fix,
        ))
        return results
    # Collapsed per card, not per region: Google meters some cards region by
    # region, so the raw rows read `K80=1, K80=1, K80=1, K80=1` — four entries
    # for one card nobody wants — while the L4 you would actually use fell off
    # the end of a silent truncation at four. Say how many there are, and never
    # cut without saying so.
    #
    # And DRIVABLE, not merely granted. Quota for a pre-Turing card starts an
    # instance and cannot start a working one — the open kernel module this tool
    # installs needs a GSP (see `create.GSP_ARCHITECTURES`) — so counting one
    # here reported a project ready when nothing it holds can generate a pixel.
    # That is the same defect this block was already fixed for once, with
    # committed and preemptible allowances, one layer further down.
    from .create import card_named

    def drivable(name: str) -> bool:
        card = card_named(name)
        return card is None or card.has_gsp

    summary = [card for card in summarise(cards) if card.usable]
    stranded = [card.gpu for card in summary if not drivable(card.gpu)]
    summary = [card for card in summary if drivable(card.gpu)]
    if not summary:
        results.append(Check(
            "gpu quota", False,
            f"only {', '.join(stranded)} — cards this tool cannot drive, because "
            f"the open NVIDIA kernel module it installs needs a GPU System "
            f"Processor and only Turing and newer have one",
            "comfy-qat quota request --gpu t4,l4 --region <region>",
        ))
        return results
    shown = ", ".join(f"{card.gpu}={card.limit}" for card in summary[:4])
    if len(summary) > 4:
        shown += f" (+{len(summary) - 4} more)"
    detail = f"{shown} — {say.count(len(summary), 'card')} ready"
    if stranded:
        # Named, not silently dropped: the grant is real, and somebody looking at
        # the console will otherwise see a card here that this row does not.
        detail += f"; {', '.join(stranded)} granted but not drivable"
    results.append(Check("gpu quota", True, detail))

    # LAST, deliberately. Tunnel speed is readiness and belongs here — `setup`
    # installs NumPy into gcloud's own python now, so this row is the catch-up
    # for anyone who ran setup before it did. But `status` prints the fix for the
    # FIRST failing check, so a missing NumPy placed any earlier would mask a
    # missing account or an unlinked billing project behind advice about a
    # slower tunnel. It is the only check here that blocks nothing.
    from .setup import gcloud_numpy

    wanted = gcloud_numpy(gc)
    if wanted is None:
        results.append(Check("numpy", True, "gcloud's tunnel is on the fast path"))
    else:
        blocked = wanted.blocked
        # `ok=True` when setup CANNOT install it. `status` exits 1 on any failed
        # check, so a root-owned gcloud python — which setup deliberately skips
        # rather than escalating to sudo — gave a permanent non-zero exit with a
        # fix that loops back to the command that already declined.
        #
        # A slower tunnel is not a readiness failure. The row still says so, and
        # the row is the point; the exit code was claiming something it should
        # not.
        #
        # `setup --no-numpy` is NOT this case and deliberately still fails the
        # check: nothing declined there, the user did, and `comfy-qat setup`
        # without the flag installs it. A fix line that works is worth an exit
        # code; a fix line that cannot work is not.
        results.append(Check(
            "numpy", bool(blocked),
            f"not installed and {blocked} — tunnels are slower than they need "
            "to be" if blocked else
            "not installed — every tunnel is slower than it needs to be",
            "comfy-qat setup",
        ))

    return results


def _value_of(quota: dict) -> int:
    """Pull the effective limit out of a QuotaInfo, tolerating shape changes."""
    details = quota.get("dimensionsInfos") or []
    best = 0
    for entry in details:
        value = (entry.get("details") or {}).get("value")
        if isinstance(value, (int, str)):
            try:
                best = max(best, int(value))
            except (TypeError, ValueError):
                continue
    return best


@app.command("status")
def status_cmd(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Am I signed in, on which project, with billing and GPU quota?"""
    checks = run_checks(Gcloud())

    if as_json:
        say.result(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        for check in checks:
            say.check(check.ok, f"{check.name:<10} {check.detail}")
        failed = next((c for c in checks if not c.ok), None)
        if failed and failed.fix:
            # The rows are the answer and stay on stdout; the fix belongs to a
            # failure and goes where every other fix goes. This was the one
            # `to fix:` of fourteen that was printed on stdout.
            say.write_fix(failed.fix)

    if any(not c.ok for c in checks):
        raise typer.Exit(code=1)


@app.command("login")
def login_cmd() -> None:
    """Print the sign-in commands.

    `gcloud auth login` opens a browser and is interactive, so it is handed over
    rather than driven. Running it yourself also leaves you the repro trail.
    """
    say.result("run these, then `comfy-qat status`:\n")
    say.result("  gcloud auth login")
    say.result("  gcloud config set project <your-project-id>")
@quota_app.callback(invoke_without_command=True)
def quota_default(ctx: typer.Context) -> None:
    """Showing what you can run is the safe default."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(quota_list_cmd, as_json=False, region=None, by_region=False)


@quota_app.command("list")
def quota_list_cmd(
    region: Annotated[Optional[str], typer.Option("--region", help="Only this region.")] = None,
    by_region: Annotated[bool, typer.Option(
        "--by-region", help="One row per region instead of one per card.")] = False,
    as_json: Annotated[bool, typer.Option(
        "--json", help="Print JSON instead of the table: project, gpus (one per card), "
                       "by_region (one per card and place).")] = False,
) -> None:
    """What can I run today, what is waiting on Google, what did I never ask for."""
    gc = Gcloud()
    try:
        project = _require_project(gc)
        # Google's quota API is slow enough that a silent minute reads as a hang,
        # so the step says how long it should take and then keeps saying it is
        # still there. On stderr, which is what keeps `--json` a document.
        with say.slow("reading quota", expect="about a minute"):
            quotas = gc.gpu_quotas(project)
            prefs = gc.quota_preferences(project)
    except GcloudError as exc:
        say.fail(exc, code=2)

    rows = readiness(quotas, prefs, region=region)
    cards = summarise(rows)

    # "ready" here has only ever meant one thing: Google will let you start it.
    # It said nothing about whether this tool can then bring the card up, and for
    # four of the cards it can order, it cannot — the driver it installs is the
    # open kernel module and those cards have no GSP. So a project holding P100
    # quota read `P100 1 ready` off this table, and `create --gpu p100` built a
    # billing box whose GPU never initialised. The grant and the driver are two
    # different questions and this table now answers both.
    from .create import card_named

    def undrivable(name: str) -> bool:
        card = card_named(name)
        return card is not None and not card.has_gsp

    if as_json:
        say.result(json.dumps({
            "project": project,
            "gpus": [{**asdict(c), "drivable": not undrivable(c.gpu)} for c in cards],
            "by_region": [{**asdict(r), "drivable": not undrivable(r.gpu)} for r in rows],
        }, indent=2))
        return

    if not rows:
        say.result(f"{project}: no GPU quotas reported")
        return

    notes = {
        "ready": "ready",
        "pending": "pending — waiting on Google",
        "none": "none — request it",
    }

    def status_of(name: str, state: str) -> str:
        if undrivable(name):
            return f"{notes[state]} — this tool cannot drive it"
        return notes[state]

    # Once, under the table, rather than a repeated parenthesis in the STATUS
    # column. The column says which rows; this says why, and what it costs to
    # ignore, which is the half a person needs before asking for more of one.
    def footnote() -> None:
        stranded = sorted({c.gpu for c in cards if undrivable(c.gpu)})
        if not stranded:
            return
        say.result(
            f"\nnote: {', '.join(stranded)} — quota you hold and this tool will not "
            f"use. It installs the open NVIDIA kernel module, which needs a GPU "
            f"System Processor (GSP); only Turing and newer cards have one, so on "
            f"these the driver installs, no module loads, and nvidia-smi never "
            f"works. `create` refuses them rather than letting you pay to find out."
        )

    if by_region:
        width = max([len(r.region) for r in rows] + [6])
        say.result(f"{'GPU':<14} {'REGION':<{width}} {'LIMIT':>5}  STATUS")
        for row in rows:
            say.result(f"{row.gpu:<14} {row.region:<{width}} {row.limit:>5}  "
                       f"{status_of(row.gpu, row.status)}")
        footnote()
        return

    width = max([len(c.where) for c in cards] + [6])
    say.result(f"{'GPU':<14} {'LIMIT':>5}  {'WHERE':<{width}}  STATUS")
    for card in cards:
        say.result(f"{card.gpu:<14} {card.limit:>5}  {card.where:<{width}}  "
                   f"{status_of(card.gpu, card.status)}")

    footnote()

    if not any(c.usable for c in cards):
        say.result("\nnothing is usable yet. Ask for a card:")
        say.result("  comfy-qat quota request --gpu l4,a100 --region us-central1")


@quota_app.command("request")
def quota_request_cmd(
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", help="Cards to ask for, comma separated, e.g. l4,a100.")] = None,
    quota_id: Annotated[Optional[str], typer.Option(
        "--quota-id", help="Raw quota id, if you would rather name it exactly.")] = None,
    value: Annotated[int, typer.Option("--value", help="How many of each card.")] = 1,
    region: Annotated[Optional[str], typer.Option(
        "--region", help="The region to be granted the quota in — where the cards you are "
                         "approved for may then be started. Not `quota list --region`, "
                         "which only narrows what that table shows.")] = None,
    justification: Annotated[Optional[str], typer.Option(
        "--justification", help="Why you need it, passed to Google verbatim — this is what a "
                                "human reviewer reads. Left off, nothing is sent at all.")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for approval. On by default.")] = True,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the gcloud calls instead of running them.")] = False,
) -> None:
    """Ask Google for GPU quota — several cards at once — then wait for the answer.

    Requesting costs nothing, so asking for every card you might want up front is
    the right move when approval is the slow part. Submitting is instant; approval
    is not, and may go to a human. A brand-new account with no billing history is
    often refused until it has been billed once.
    """
    if not gpu and not quota_id:
        say.fail("name a card to ask for",
                 fix=say.fix("comfy-qat quota request --gpu l4,a100",
                             "or --quota-id, to name a raw quota id exactly"),
                 code=2)

    gc = Gcloud()
    try:
        project = _require_project(gc)
        quotas = gc.gpu_quotas(project)
    except GcloudError as exc:
        # The exception carries the command that fixes it — dropping it left
        # `no project set` with nowhere to go, while `quota list` said `comfy-qat
        # setup` for the same failure. `say.fail` reads `.fix` off the exception,
        # so it can no longer be lost by forgetting a line.
        say.fail(exc, code=2)

    # Asking Google for a card this tool cannot bring up is a request that can
    # only be granted into a dead end: approval takes days, and at the end of it
    # `create --gpu p100` still refuses, because the driver installed here is the
    # open kernel module and Pascal has no GSP. Refused at the point of asking,
    # where it costs nothing, rather than after the wait.
    from .create import card_named, drivable_cards

    wanted: list[tuple[str, str]] = []
    if quota_id:
        wanted.append((quota_id, quota_id))
    for name in (gpu or "").split(","):
        name = name.strip()
        if not name:
            continue
        card = card_named(name)
        if card is not None and not card.has_gsp:
            say.fail(
                f"there is no point asking for {card.name} quota: this tool cannot "
                f"bring that card up. The driver it installs is the open NVIDIA "
                f"kernel module, which needs a GPU System Processor, and only "
                f"Turing and newer cards have one.",
                fix=f"ask for one that works: {', '.join(drivable_cards())}",
                code=2,
            )
        resolved = resolve(name, quotas, region=region)
        if resolved is None:
            # Never a card this tool cannot drive. "ask for one of: P100" is
            # advice that ends in a billing box with a dead GPU.
            offer = ", ".join(
                found for found in available_gpus(quotas)
                if (card_named(found) is None or card_named(found).has_gsp)
            ) or "none"
            # A card this project does have, just not where you asked, used to
            # come back as "no quota for 'l4' … Available: L4" — which reads as
            # a contradiction. Say where it is metered instead.
            elsewhere = sorted({
                row.region for row in readiness(quotas) if matches(name, row.quota_id)
            }) if region else []
            say.fail(
                f"this project reports no quota for {name!r}"
                + (f" in {region}" if region else "")
                + (f", it is metered in {', '.join(elsewhere)}" if elsewhere else ""),
                fix=(f"ask for one of: {offer}" if offer != "none" else
                     "this project reports no GPU quota at all — "
                     "comfy-qat quota request --gpu l4 --region us-central1"),
                code=2,
            )
        wanted.append((name, resolved))

    submitted: list[tuple[str, str]] = []
    for name, resolved in wanted:
        args = quota_request_command(
            project=project, quota_id=resolved, value=value,
            region=region, justification=justification,
        )
        if dry_run:
            say.result("gcloud " + " ".join(args))
            continue
        try:
            gc.run(args)
        except GcloudError as exc:
            # `exc.fix` explicitly. Interpolating the exception into the
            # message leaves `say.error` looking for a `.fix` on a STRING, so
            # the one gcloud's classifier had already worked out — which region
            # to use, which quota id is the real one — was computed and thrown
            # away, on the command where a refusal is the ordinary outcome.
            say.error(f"request for {name} failed: {exc}", exc.fix,
                      blank_line=False)
            continue
        say.result(f"requested {name} = {value}" + (f" in {region}" if region else ""))
        submitted.append((name, resolved))

    if dry_run:
        return
    if not submitted:
        # Every request was refused. Exiting 0 told a script it had worked.
        raise typer.Exit(code=2)

    say.result(f"track them: {console_quota_url(project)}")
    if not wait:
        return

    # One window for the whole command, not one per card: waiting on l4,a100
    # serially meant the documented half-hour became an hour, and the second
    # card was not even polled until the first gave up.
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    still_waiting = []
    for name, resolved in submitted:
        granted = wait_for_quota(
            lambda rid=resolved: _current_value(gc, project, rid), wanted=value,
            timeout=max(0.0, deadline - time.monotonic()), interval=POLL_SECONDS,
        )
        if granted:
            say.result(f"granted: {name}")
        else:
            still_waiting.append(name)

    if still_waiting:
        # 75 is EX_TEMPFAIL: not an error and not done either. Nothing is broken,
        # so this is a result rather than a failure, and it names what to run
        # next because approval can take days and the wait has to be re-entered.
        say.result(f"still pending: {', '.join(still_waiting)}. Approval can take "
                   "days — run this again to keep waiting, or `comfy-qat quota` "
                   "to check.")
        raise typer.Exit(code=75)


def _require_project(gc: Gcloud) -> str:
    project = gc.current_project()
    if not project:
        raise GcloudError("no project set", fix="comfy-qat setup")
    return project


def _current_value(gc: Gcloud, project: str, quota_id: str) -> int:
    for quota in gc.gpu_quotas(project):
        if quota.get("quotaId") == quota_id:
            return _value_of(quota)
    return 0


def wait_for_quota(
    poll: Callable[[], int],
    *,
    wanted: int,
    timeout: float | None = None,
    interval: float | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
) -> bool:
    """Poll until the quota reaches `wanted`, or the window closes.

    Returns True if granted. Every default is resolved here rather than in the
    signature: bound as defaults they were captured at import, so neither the
    constants nor the clock could be replaced from outside, and driving the wait
    through the command itself meant a test that really slept for half an hour.
    """
    timeout = WAIT_TIMEOUT_SECONDS if timeout is None else timeout
    interval = POLL_SECONDS if interval is None else interval
    sleep = sleep or time.sleep
    now = now or time.monotonic

    deadline = now() + timeout
    while True:
        try:
            if poll() >= wanted:
                return True
        except GcloudError:
            pass  # a transient read failure is not a denial; keep waiting
        if now() >= deadline:
            return False
        sleep(interval)
