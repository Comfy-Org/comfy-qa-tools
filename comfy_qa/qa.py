"""Testing, and saying what you found: `where`, `pass`, `fail`, `results`,
`report`, `bug` and `record`.

These are the commands a tester uses between "a box is up" and "QA Desk knows
the answer": where can this be tested, record what happened here, draft what
QA Desk would be told, draft a bug, and start the frontend's recorder against a
box. None of them sends anything anywhere. QA Desk is read, never written —
its writes are run only as `--dry-run`, and the command that would send one is
printed for a person to read and run (`qadesk.py`). A Linear issue is kept as
text. A bug is a draft to paste.

Each module below holds its half: `places` decides Ready, Can be prepared and
Not suitable; `results` keeps a result and builds the drafts; `record` gets the
recorder's pieces ready. This file is the surface, and it is where a failure
becomes a sentence and a fix.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from . import say
from .host import ConfigOption

app = typer.Typer(help="Test, and say what you found.", no_args_is_help=False)

ReleaseArgument = Annotated[Optional[str], typer.Argument(
    help="A QA Desk release, e.g. 1.60. With a case after it, that case.")]
CaseArgument = Annotated[Optional[str], typer.Argument(
    help="A QA Desk case in that release, e.g. C-01.")]
VersionOption = Annotated[Optional[str], typer.Option(
    "--version", help="A ComfyUI release, e.g. v0.43.0.")]
PrOption = Annotated[Optional[str], typer.Option(
    "--pr", help="A ComfyUI pull request, by number.")]
CommitOption = Annotated[Optional[str], typer.Option(
    "--commit", help="A ComfyUI commit, by hash.")]
FrontendVersionOption = Annotated[Optional[str], typer.Option(
    "--frontend-version", help="A frontend release, e.g. v1.58.5.")]
FrontendPrOption = Annotated[Optional[str], typer.Option(
    "--frontend-pr", help="A frontend pull request, by number.")]
OnOption = Annotated[Optional[str], typer.Option(
    "--on", help="Where it was tested: a machine in your host list (a name, or "
                 "windows, l4, windows/l4), or testcloud, staging or prod.")]
NoteOption = Annotated[Optional[str], typer.Option(
    "--note", help="What you saw. For a fail, what broke — it becomes QA Desk's bug "
                   "text, with the stamp after it.")]
BugOption = Annotated[Optional[str], typer.Option(
    "--bug", help="A Linear issue (FE-1234) or another bug reference, kept with the "
                  "result as text. Nothing is sent to Linear.")]
JsonOption = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]


def _target(release, case, *, version=None, pr=None, commit=None,
            frontend_version=None, frontend_pr=None, machine=None):
    from .build import BuildError
    from .places import TargetError, target_for
    from .qadesk import QaDeskError

    try:
        return target_for(release, case, version=version, pr=pr, commit=commit,
                          frontend_version=frontend_version, frontend_pr=frontend_pr,
                          machine=machine)
    except (TargetError, BuildError) as exc:
        say.fail(exc, exc.fix, code=2, blank_line=False)
    except QaDeskError as exc:
        say.fail(exc, exc.fix, code=exc.code, blank_line=False)


# --- Test this ------------------------------------------------------------------


@app.command("where")
def where_cmd(
    release: ReleaseArgument = None,
    case: CaseArgument = None,
    version: VersionOption = None,
    pr: PrOption = None,
    commit: CommitOption = None,
    frontend_version: FrontendVersionOption = None,
    frontend_pr: FrontendPrOption = None,
    machine: Annotated[Optional[str], typer.Option(
        "--machine", help="Only a machine that is this counts: an OS, a card, or "
                          "both — windows, l4, windows/l4, linux/none.")] = None,
    as_json: JsonOption = False,
    config: ConfigOption = None,
) -> None:
    """Test this: every place it could be tested, and whether each is ready.

    What to test is a QA Desk case (`where 1.60 C-01`), a QA Desk release
    (`where 1.60`), or a build: `--pr`, `--version`, `--commit`,
    `--frontend-version`, `--frontend-pr` — and `--machine windows/l4` for the
    OS and card it needs. Every machine in your host list and
    testcloud, staging and prod are listed, each Ready, Can be prepared — with
    what would change and roughly how long — or Not suitable, with the reason.
    A case's own target (cloud, core, shared, shared-local) decides which kinds
    of place count. Nothing is started: this reads Google's instance list, the
    tunnels already open, GitHub and QA Desk.
    """
    from . import inventory
    from .config import ConfigError, load
    from .places import cloud_places, machine_places, summary

    target = _target(release, case, version=version, pr=pr, commit=commit,
                     frontend_version=frontend_version, frontend_pr=frontend_pr,
                     machine=machine)
    try:
        hosts = load(config)
    except ConfigError as exc:
        say.warn(f"your host list did not load, so only the cloud environments are "
                 f"listed: {exc}")
        hosts = []
    gc = None
    states = {}
    if any(host.is_remote for host in hosts):
        from .gcloud import Gcloud

        gc = Gcloud()
        states = inventory.survey(gc, hosts).rows
    places = cloud_places(target) + machine_places(target, hosts, gc=gc, states=states)

    if as_json:
        say.result(json.dumps({"what": target.label, "case": target.case,
                               "release": target.version,
                               "test_target": target.test_target,
                               "core_tag": target.core_tag,
                               "places": [place.as_dict() for place in places]},
                              indent=2))
        return
    for line in summary(target):
        say.result(line)
    say.result("")
    for line in say.rows([("PLACE", "STATE", "WHY")]
                         + [(place.name, place.state, place.why) for place in places]):
        say.result(line)
    ready = [place for place in places if place.command]
    if ready:
        say.result("\nto prepare one — each starts the box if it is stopped, and bills "
                   "while it runs:")
        for place in ready:
            say.result(say.offer(place.command, place.name))
    say.result("\nnothing was started or changed.")


# --- a result -------------------------------------------------------------------


def _place(on: Optional[str], config: Optional[Path]):
    """`("cloud", name, None)` for testcloud, staging or prod; `("machine",
    name, host)` for a machine in the host list."""
    from .host import _host
    from .places import CLOUD_PLACES

    if not on:
        say.fail("say where you tested it with --on: a machine in your host list, or "
                 "testcloud, staging or prod.",
                 fix="for example: --on testcloud, or --on windows/l4", code=2,
                 blank_line=False)
    for name, _ in CLOUD_PLACES:
        if on.lower() == name:
            return "cloud", name, None
    host = _host(on, config)
    return "machine", host.name, host


def _stamp(kind: str, name: str, host) -> tuple[dict, str]:
    """The full stamp of a place, read now: the stamp a machine answers with,
    or the frontend a cloud environment serves."""
    if kind == "cloud":
        from .env import CLOUD_ENVS, probe_cloud
        from .places import CLOUD_PLACES
        from .stamp import served_frontend

        key = dict(CLOUD_PLACES)[name]
        report = probe_cloud(key, CLOUD_ENVS[key], resolve=False)
        if report.error or not report.sha:
            say.fail(f"{name} could not be asked which frontend it serves "
                     f"({report.error or 'no answer'}), so there is no stamp to keep.",
                     fix=f"see it for yourself: comfy-qat env {key}", code=1,
                     blank_line=False)
        version = served_frontend(report.url)
        stamp = {"place": name, "url": report.url, "frontend_commit": report.sha}
        if version:
            stamp["frontend_version"] = version
        line = f"{name} · frontend {report.sha[:10]}" + (f" ({version})" if version else "")
        return stamp, line

    from .host import _probe_fix
    from .lifecycle import observe
    from .stamp import ProbeError, fetch, mismatch

    try:
        stamp = fetch(host.url, host=host.name)
    except ProbeError as exc:
        say.fail(f"{host.name} did not answer, so there is no stamp of what was "
                 f"tested: {exc}", _probe_fix(host) or exc.fix, code=1,
                 blank_line=False)
    problem = mismatch(host, stamp)
    if problem is not None:
        say.fail(f"{problem} No result was kept: its stamp would name the wrong "
                 f"machine.", fix="check the port in your host list and which tunnel "
                                  "is open", code=1, blank_line=False)
    gc = None
    if host.is_remote:
        from .gcloud import Gcloud

        gc = Gcloud()
    stamp = observe(gc, host, stamp)
    return stamp.as_dict(), stamp.line()


def _keep(*, outcome: str, release, case, on, note, bug, config, **build) -> None:
    from . import results, version_string
    from .build import BuildError
    from .places import TARGETS, TargetError
    from .qadesk import QaDeskError

    if outcome == "fail" and not note:
        say.fail("say what broke with --note: for a QA Desk case it becomes the bug's "
                 "text.", fix='for example: --note "the picker shows a save-failed toast"',
                 code=2, blank_line=False)
    kind, name, host = _place(on, config)
    target = None
    unread = None
    if case is not None or release is not None or any(build.values()):
        try:
            from .places import target_for

            target = target_for(release, case, **build)
        except QaDeskError as exc:
            # A result is kept whether or not QA Desk can be read: the case is
            # still the case, and the draft below says what could not be done.
            unread = exc
        except (TargetError, BuildError) as exc:
            say.fail(exc, exc.fix, code=2, blank_line=False)
    if target is not None and target.test_target in TARGETS and not target.counts(
            "cloud" if kind == "cloud" else "machine"):
        say.fail(f"{target.label} is a {target.test_target} case — QA Desk tests it on "
                 f"{TARGETS[target.test_target][0]} — so a result on {name} is not one "
                 f"QA Desk can take. Nothing was kept.",
                 fix=f"where it can be tested: comfy-qat where {target.version} "
                     f"{target.case}", code=2, blank_line=False)
    if kind == "cloud" and name != "testcloud" and (case or (target and target.case)):
        say.fail(f"QA Desk records a cloud result on testcloud only, so a result for a "
                 f"QA Desk case on {name} is not one it can take. Nothing was kept.",
                 fix="--on testcloud", code=2, blank_line=False)

    stamp, line = _stamp(kind, name, host)
    what = (target.label if target is not None
            else f"{release} {case}".strip() if release else "?")
    result = results.Result(
        outcome=outcome, what=what, place=name, place_kind=kind, at=results.now(),
        stamp=stamp, stamp_line=line, note=note or "", bug=bug,
        release=(target.version if target else release),
        case=(target.case if target else (case.upper() if case else None)),
        test_target=target.test_target if target else None,
        comfy_qat=version_string())
    path = results.save(result, config)
    say.result(f"{outcome} kept: {what} on {name}")
    say.result(f"  {line}")
    say.result(f"  {path}")
    if result.case and result.release:
        _draft(results.pass_command(result) if outcome == "pass"
               else results.fail_command(result), unread=unread)
    else:
        say.result("\nno QA Desk case was named, so there is nothing to draft for QA "
                   "Desk. It is kept here; results lists it.")


def _draft(command: list[str], *, unread=None) -> None:
    """The exact `qa-desk` command, to read and paste — then its dry run."""
    from . import qadesk, results

    say.result("\nQA Desk, if you send it — this tool sends nothing:")
    say.result(f"  {results.printed(command)}")
    if unread is not None:
        say.warn(f"its dry run could not be run: {unread}")
        if unread.fix:
            say.write_fix(unread.fix)
        return
    try:
        answer = qadesk.dry_run(command)
    except qadesk.QaDeskError as exc:
        say.warn(f"its dry run could not be run: {exc}")
        if exc.fix:
            say.write_fix(exc.fix)
        return
    say.result("\nqa-desk's dry run — the request it would send, not sent:")
    for line in qadesk.request_lines(answer):
        say.result(f"  {line}")


@app.command("pass")
def pass_cmd(
    release: ReleaseArgument = None,
    case: CaseArgument = None,
    on: OnOption = None,
    note: NoteOption = None,
    bug: BugOption = None,
    version: VersionOption = None,
    pr: PrOption = None,
    commit: CommitOption = None,
    frontend_version: FrontendVersionOption = None,
    frontend_pr: FrontendPrOption = None,
    config: ConfigOption = None,
) -> None:
    """Keep a pass here: what was tested, where, the full stamp, the time.

    It works without QA Desk: what was tested is a case (`pass 1.60 C-01 --on
    windows`), a release, or a build (`--pr 17497`). For a QA Desk case it then
    prints the exact `qa-desk case pass` command — `--on local` or `--on
    testcloud`, the core tag from the stamp, and the stamp as its note — and
    runs only its `--dry-run`. Nothing is sent to QA Desk.
    """
    _keep(outcome="pass", release=release, case=case, on=on, note=note, bug=bug,
          config=config, version=version, pr=pr, commit=commit,
          frontend_version=frontend_version, frontend_pr=frontend_pr)


@app.command("fail")
def fail_cmd(
    release: ReleaseArgument = None,
    case: CaseArgument = None,
    on: OnOption = None,
    note: NoteOption = None,
    bug: BugOption = None,
    version: VersionOption = None,
    pr: PrOption = None,
    commit: CommitOption = None,
    frontend_version: FrontendVersionOption = None,
    frontend_pr: FrontendPrOption = None,
    config: ConfigOption = None,
) -> None:
    """Keep a fail here, with what broke (`--note`) and the full stamp.

    For a QA Desk case it prints the exact `qa-desk case fail` command — whose
    `--bug` text is what broke and then the stamp, because `case fail` has no
    `--note` — and runs only its `--dry-run`. A Linear issue given with `--bug`
    goes to QA Desk's `--linear`. Nothing is sent to QA Desk or Linear.
    """
    _keep(outcome="fail", release=release, case=case, on=on, note=note, bug=bug,
          config=config, version=version, pr=pr, commit=commit,
          frontend_version=frontend_version, frontend_pr=frontend_pr)


@app.command("results")
def results_cmd(
    as_json: JsonOption = False,
    config: ConfigOption = None,
) -> None:
    """List the passes and fails kept here, oldest first."""
    from . import results

    kept = results.load_all(config)
    if as_json:
        from dataclasses import asdict

        say.result(json.dumps([dict(asdict(result), file=str(path))
                               for path, result in kept], indent=2))
        return
    if not kept:
        say.result(f"no results kept yet in {results.folder(config)}.")
        return
    for line in say.rows([("WHEN", "RESULT", "WHAT", "WHERE", "BUG", "NOTE")] + [
            (result.at, result.outcome, result.what, result.place, result.bug or "-",
             result.note or "-") for _, result in kept]):
        say.result(line)
    for path in results.unreadable(config):
        say.warn(f"{path} does not read as a result, so it is not listed")


@app.command("report")
def report_cmd(
    release: Annotated[str, typer.Argument(help="The QA Desk release, e.g. 1.60.")],
    test_pr: Annotated[str, typer.Argument(
        help="The Playwright test's pull request, e.g. 22870 or '#22870'.")],
    cases: Annotated[str, typer.Option(
        "--cases", help="The cases it covers, e.g. C-03,C-07.")],
    bug: Annotated[Optional[str], typer.Option(
        "--bug", help="The QA Desk bug it tests, e.g. e123.")] = None,
    note: Annotated[Optional[str], typer.Option(
        "--note", help="What it checks.")] = None,
    config: ConfigOption = None,
) -> None:
    """Draft QA Desk's record of a recorded Playwright test PR, as a dry run.

    Prints the exact `qa-desk test report <release> <pr> --cases …` command and
    runs only its `--dry-run`. Nothing is sent to QA Desk.
    """
    from . import results
    from .places import TargetError, release_word

    try:
        version = release_word(release)
    except TargetError as exc:
        say.fail(exc, exc.fix, code=2, blank_line=False)
    _draft(results.report_command(version, test_pr, cases, bug=bug, note=note))


@app.command("bug")
def bug_cmd(
    release: ReleaseArgument = None,
    case: CaseArgument = None,
    on: OnOption = None,
    title: Annotated[Optional[str], typer.Option(
        "--title", help="The bug's title: what is wrong, in a few words.")] = None,
    note: Annotated[Optional[str], typer.Option(
        "--note", help="What happened, plainly — the paragraph's first sentence.")] = None,
    expected: Annotated[Optional[str], typer.Option(
        "--expected", help="What should have happened.")] = None,
    recording: Annotated[Optional[list[Path]], typer.Option(
        "--recording", help="A recording to attach. Left off, the last one `record` "
                            "made on that place, if there is one.")] = None,
    version: VersionOption = None,
    pr: PrOption = None,
    commit: CommitOption = None,
    frontend_version: FrontendVersionOption = None,
    frontend_pr: FrontendPrOption = None,
    config: ConfigOption = None,
) -> None:
    """Raise bug: draft it in the house format, for Linear or #bug-dump.

    A title, one impersonal paragraph (what happened, what was expected, where
    it was found), and the environment block from the place's stamp, read now —
    with the recording to attach when there is one. It is text to paste.
    Nothing is sent anywhere.
    """
    from . import results, version_string

    if not title or not note:
        say.fail("a bug needs --title and --note: what is wrong, and what happened.",
                 fix='for example: --title "Picker toast on an unloaded tab" '
                     '--note "Picking an unloaded tab shows Save failed"', code=2,
                 blank_line=False)
    kind, name, host = _place(on, config)
    what = None
    if case is not None or release is not None or any(
            (version, pr, commit, frontend_version, frontend_pr)):
        what = _target(release, case, version=version, pr=pr, commit=commit,
                       frontend_version=frontend_version, frontend_pr=frontend_pr).label
    _, line = _stamp(kind, name, host)
    attach = [str(path) for path in recording or []]
    for path in attach:
        if not Path(path).is_file():
            say.fail(f"there is no recording at {path}.", code=2, blank_line=False)
    if not attach:
        attach = results.last_recording(config, name)
    say.result("for Linear or #bug-dump — drafted here, sent by you:\n")
    for text in results.bug_draft(title=title, happened=note, expected=expected,
                                  what=what, place=name, stamp_line=line,
                                  at=results.now(), version=version_string(),
                                  recording=attach):
        say.result(text)


# --- the recorder ---------------------------------------------------------------


@app.command("record")
def record_cmd(
    name: Annotated[Optional[str], typer.Argument(
        help="The box to record against: a name, or windows, l4, windows/l4.")] = None,
    release: Annotated[Optional[str], typer.Argument(
        help="The release being tested, e.g. 1.60: the frontend checkout is its "
             "core/<release> branch.")] = None,
    ref: Annotated[Optional[str], typer.Option(
        "--ref", help="Check out this frontend branch or tag instead. Left off with no "
                      "release, the release tag of the frontend the box serves.")] = None,
    passthrough: Annotated[Optional[list[str]], typer.Argument(
        help="After --, options handed to the recorder as they are, e.g. "
             "-- --workflow default --tags @canvas.")] = None,
    config: ConfigOption = None,
) -> None:
    """Start the frontend's test recorder against a box — and you record.

    The recorder is the frontend's own `pnpm comfy-test record`, run with
    `--backend` set to the box's tunnel, from a checkout of the matching
    frontend kept in comfy-qat's own folder. The box has to be serving that
    same frontend version, and runs ComfyUI in multi-user mode while you record
    (this switches it on and back). You record in your own browser; the
    recorder's prompts and its PR flow are its own.
    """
    from . import record, results
    from .host import _host, _selector

    passthrough = list(passthrough or [])
    if release and release.startswith("-"):
        # `record <box> -- --workflow default`: what follows `--` fills the
        # positions in order, so the recorder's first option landed here.
        passthrough, release = [release, *passthrough], None
    host = _host(_selector(name), config)
    try:
        _record(host, release, ref, passthrough, config, record, results)
    except record.RecordError as exc:
        say.fail(exc, exc.fix, code=exc.code, blank_line=False)


def _record(host, release, ref, passthrough, config, record, results) -> None:
    import shutil
    import subprocess

    from .places import TargetError, release_word
    from .stamp import ProbeError, fetch, mismatch, served_frontend

    if not host.is_remote:
        raise record.RecordError(
            f"{host.name} is your own ComfyUI, and record switches the box's ComfyUI "
            f"into multi-user mode and back, which this tool does not do to your own "
            f"install.",
            fix="run the recorder yourself, from your frontend checkout: "
                "pnpm comfy-test record --distribution local")
    if release:
        try:
            release = release_word(release)
        except TargetError as exc:
            raise record.RecordError(str(exc), exc.fix) from exc

    missing = record.missing_tools(shutil.which)
    if missing:
        raise record.RecordError(
            "the recorder needs Node.js and pnpm on this machine. Nothing was started, "
            "and nothing is installed for you.", fix=say.fix(*missing))

    try:
        stamp = fetch(host.url, host=host.name)
    except ProbeError as exc:
        raise record.RecordError(
            f"{host.name} is not serving here, so there is nothing to record against.",
            fix=say.fix("start it, and its tunnel:", f"comfy-qat go {host.name}")) from exc
    problem = mismatch(host, stamp)
    if problem is not None:
        raise record.RecordError(problem, "check the port in your host list and which "
                                          "tunnel is open")
    served = served_frontend(host.url)
    chosen = record.ref_for(release=release, ref=ref, served=served)
    package = record.package_at(chosen)
    if package is None:
        raise record.RecordError(
            f"the frontend has no {chosen} to check out, or GitHub could not be asked "
            f"for its package.json.",
            fix="the release branches: https://github.com/Comfy-Org/ComfyUI_frontend/branches")
    wanted = str(package.get("version") or "")
    if not served:
        raise record.RecordError(
            f"could not read which frontend {host.name} serves, so it cannot be "
            f"checked against {chosen} ({wanted}). Nothing was started.",
            fix=say.fix("serve that frontend on it:",
                        f"comfy-qat go {host.name} --frontend-version v{wanted}"))
    if served.lstrip("v") != wanted.lstrip("v"):
        raise record.RecordError(
            f"{host.name} serves frontend {served}, and {chosen} is {wanted}: a test "
            f"recorded against one is checked against the other. Nothing was started.",
            fix=say.fix(f"serve {wanted} on the box:",
                        f"comfy-qat go {host.name} --frontend-version v{wanted}",
                        "or record against the frontend it serves:",
                        f"comfy-qat record {host.name} --ref v{served}"))

    directory = record.checkout_dir(config, chosen)
    record.ensure_checkout(directory, chosen, say.step)
    record.ensure_dependencies(directory, say.step)
    checked_out = str((record.local_package(directory) or {}).get("version") or "")
    if checked_out and checked_out != wanted:
        raise record.RecordError(
            f"the checkout of {chosen} in {directory} is {checked_out}, not {wanted}, "
            f"and {host.name} serves {served}. Nothing was started.",
            fix=f"move it aside and run this again: it clones {chosen} afresh")

    from .gcloud import Gcloud

    gc = Gcloud()
    switched = False
    if record.multi_user(host.url) is not True:
        say.step(f"switching ComfyUI on {host.name} to multi-user mode for the "
                 f"recording — the recorder needs it — and back when the recorder stops")
        _relaunch(gc, host, "--multi-user")
        switched = True
        if record.multi_user(host.url) is not True:
            _relaunch(gc, host, "")
            raise record.RecordError(
                f"ComfyUI on {host.name} did not come back in multi-user mode, so the "
                f"recorder was not started. It was put back as it was.",
                fix=say.fix("read why, in ComfyUI's own log on the box:",
                            f"comfy-qat logs {host.name}"), code=1)
    port = record.free_port({host.port})
    argv = record.command(host.url, passthrough)
    say.step(f"starting the recorder in {directory}, against {host.name} through its "
             f"tunnel — its dev server on 127.0.0.1:{port}. You record, in your own "
             f"browser:")
    say.detail(" ".join(argv))
    code = 1
    try:
        code = subprocess.run(argv, cwd=str(directory),
                              env=record.environment(port)).returncode
    except KeyboardInterrupt:
        code = 130
    finally:
        made = record.recorded(directory)
        results.remember_recording(config, host.name, made)
        if made:
            say.step(f"the recording left {say.count(len(made), 'file')} in the "
                     f"checkout: {', '.join(made)}")
        if switched:
            say.step(f"putting ComfyUI on {host.name} back in single-user mode")
            _relaunch(gc, host, "")
            if record.multi_user(host.url) is False:
                say.step(f"ComfyUI on {host.name} is back in single-user mode")
            else:
                say.warn(f"could not confirm ComfyUI on {host.name} left multi-user mode")
    if code:
        raise typer.Exit(code=code)


def _relaunch(gc, host, extra: str) -> None:
    """Stop the ComfyUI on the box and start it again, with `extra` on its
    command line — the build's own arguments are kept from the box's record."""
    from .host import _reportable
    from .lifecycle import read_facts, start_detached, stop_running

    facts = read_facts(gc, host)
    stop_running(gc, host, say.step)
    try:
        start_detached(gc, host, say.step, expect=facts.record, facts=facts, extra=extra)
    except _reportable() as exc:
        say.fail(exc, code=1)
