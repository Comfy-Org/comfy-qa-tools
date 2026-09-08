"""`comfy-qat host` — operate the machines you test on.

Two halves. `list`, `init` and the config rules are offline and never call
anything: declaring a machine is not the same act as touching it, and the rules
that stop you reading the wrong box are worth enforcing before a network exists.
`discover`, `up`, `open`, `down`, `go`, `switch`, `move` and `stamp` reach out —
each one importing what it needs inside the function, so the offline half stays
usable when gcloud is not installed at all.

Every command that takes a machine takes it through `_lookup`, so `windows`,
`l4` and `windows/l4` work wherever a name works, and the machine a description
resolved to is printed rather than assumed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Optional

import typer

from .config import (
    COMFYUI_DEFAULT_PORT,
    SEPARATOR,
    DEFAULT_CONFIG_PATH,
    ConfigError,
    Host,
    describe,
    load,
    resolve,
)
from contextlib import ExitStack

from . import inflight, say
from .lifecycle import LifecycleError, is_windows
from .provision import RDP_PORT
from .stamp import ProbeError, fetch, mismatch

app = typer.Typer(
    help="Operate the machines you test on — local installs and cloud GPU boxes.",
    no_args_is_help=False,
)


# `--config` is declared once, at the root, and inherited from there.
#
# It used to be spelled out seventeen times — on all fifteen commands here, on
# `remove`'s `delete`, and on the `host` callback — which put an identical row in
# seventeen different `--help` screens and gave one sentence seventeen chances to
# drift. It already had: fifteen of the seventeen carried no `help=` at all until
# recently, and the fix was to write the sentence out fifteen more times.
# `comfy-cli` does not do this with `--workspace`; it declares it on the root and
# says subcommands inherit it. This is that shape.
#
# The option is still *accepted* after a command name, and that is deliberate.
# `comfy-qat list --config X` is what scripts, run sheets and this suite all type,
# and `go --new-window` re-execs itself with `--config` in exactly that position —
# so dropping it would break the tool inside a spawned Terminal window, on the
# command that starts a GPU box, where nobody would see the error. Hidden rather
# than removed: the deprecation window `cli.py` already uses for the `host` and
# `auth` spellings, and `host.py` for `--os`/`--gpu` on the eleven selectors.
CONFIG_INHERITED = "comfy_qa.config_path"


def remember_config(ctx: typer.Context, value: Optional[Path]) -> Optional[Path]:
    """One `--config` for the whole invocation, wherever in the line it was typed.

    `ctx.meta` is Click's own root-wide scratch dict — every context in the stack
    hands back the same object — so a value read at the root is still there when
    a subcommand is parsed, and a value read on a subcommand is visible to
    anything nested under it. Whichever spelling was typed last wins, which is
    the more specific one: `comfy-qat --config A move --config B` moves what B
    says.

    Filling the command's own `config` parameter, rather than leaving the value
    at the root for each body to go and fetch, is what keeps `go --new-window`
    honest: it builds the argv for the spawned window out of that parameter, so a
    root `--config` reaches the new window without `go` knowing anything about
    where the option was declared.
    """
    if value is not None:
        ctx.meta[CONFIG_INHERITED] = value
        return value
    return ctx.meta.get(CONFIG_INHERITED)


ConfigOption = Annotated[Optional[Path], typer.Option(
    "--config", hidden=True, callback=remember_config,
    help="Host list. Declared at the root — see `comfy-qat --help`.")]

STARTER = f"""\
# comfy-qat list.
#
# Every machine you test on is declared here, local or cloud. Naming them all
# means there is no invisible default, which is how you end up reading results
# from the wrong machine.
#
# Rules the tool enforces, all of them when this file is read:
#   - every host needs its own port
#   - a cloud host may never use {COMFYUI_DEFAULT_PORT}; that is the local ComfyUI's
#   - no two hosts may be the same cloud box, or differ only in case
#   - 'local' is this machine, so a cloud box may not take the name
#   - a local host may not carry gce_instance / gce_zone / gce_project, or
#     `host down` would leave a real instance running and billing

[hosts.local]
kind = "local"
port = {COMFYUI_DEFAULT_PORT}

# [hosts.comfy-linux]
# kind         = "gce"
# os           = "Ubuntu 22.04"
# gpu          = "L4"
# gce_instance = "comfy-linux"
# gce_zone     = "us-central1-a"
# gce_project  = "your-project-id"
# port         = 8190
"""


def _config_option() -> Path:
    return DEFAULT_CONFIG_PATH


def _states(hosts: list[Host], *, live: bool) -> dict[str, str]:
    """What each machine is doing right now.

    The tunnel is what makes a cloud box answer on 127.0.0.1, so "tunnelled" is
    the honest answer to "which box am I on?" — and reading a pid file costs
    nothing, so it is always shown. Whether the instance is *running* is a gcloud
    call per box, which is not free, so it waits to be asked for with --live.
    """
    from .tunnel import status as tunnel_status

    # One read for every cloud box, not one per box. `instance_statuses` asks
    # `instances list` once per distinct project; this loop used to spawn a
    # `gcloud compute instances describe` process per host, serially, so a host
    # list with eight boxes meant eight of them.
    live_states: dict[tuple[str, str, str], str] = {}
    if live:
        from .gcloud import Gcloud, GcloudError

        remote = [(host.gce_instance, host.gce_zone, host.gce_project)
                  for host in hosts if host.is_remote]
        if remote:
            try:
                live_states = Gcloud().instance_statuses(remote)
            except GcloudError:
                # Unchanged in kind: not knowing was never a reason to fail
                # `list`. It is now all-or-nothing per read rather than per box,
                # which is what one call instead of N means.
                live_states = {}

    states: dict[str, str] = {}
    for host in hosts:
        parts = []
        if host.is_remote:
            if live:
                # `or "unknown"`, so all three ways of not knowing land on the
                # same word: the read failed, the machine is not on the project
                # any more, or Google answered without a status. A declared box
                # that has been deleted used to raise and be reported "unknown";
                # it is now simply absent from the list, and must still say so.
                key = (host.gce_instance, host.gce_zone, host.gce_project)
                state = live_states.get(key) or "unknown"
                # TERMINATED is Google's word for stopped, and reads as broken.
                parts.append({"RUNNING": "running", "TERMINATED": "stopped"}.get(
                    state, state.lower()))
            if tunnel_status(host.name).running:
                parts.append("tunnelled")
            elif not live:
                # Without --live the instance state is unknown, not stopped, and
                # a bare "-" said all three of "not running", "not known" and
                # "does not apply" at once — so a running cloud box looked
                # identical to one that is off. Say only what was actually read.
                parts.append("not tunnelled")
        states[host.name] = ", ".join(parts) or "-"
    return states


@app.command("list")
def list_cmd(
    config: ConfigOption = None,
    live: Annotated[bool, typer.Option(
        "--live", help="Ask Google whether each cloud box is running. One call per box.")] = False,
) -> None:
    """Show every declared machine: what it is, where it answers, and what is up."""
    try:
        hosts = load(config)
    except ConfigError as exc:
        say.fail(exc, code=2, blank_line=False)

    state = _states(hosts, live=live)
    rows = [("NAME", "KIND", "OS", "GPU", "URL", "STATE")] + [
        (h.name, h.kind, h.os or "-", h.gpu or "-", h.url, state[h.name]) for h in hosts
    ]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        say.result("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())

    # Without --live the STATE column knows about tunnels and nothing else, so a
    # cloud box that is running looks the same as one that is off. Say which
    # question was not asked rather than letting the column imply an answer.
    if any(host.is_remote for host in hosts) and not live:
        say.result("\nSTATE is what this machine knows: whether a tunnel is open. "
                   "Add --live to ask Google what is actually running.")


@app.command("init")
def init_cmd(
    config: ConfigOption = None,
    force: Annotated[bool, typer.Option(
        "--force", help="Overwrite an existing host list.")] = False,
) -> None:
    """Write a starter host list you can edit.

    The one command where `--config` is a destination rather than a source: it
    says where to write the starter file, and nothing is read. Left off, it
    writes ~/.config/comfy-qa-tools/hosts.toml, and refuses if that exists.
    """
    from .hostfile import HostFileError, apply

    path = config or DEFAULT_CONFIG_PATH
    if path.exists() and not force:
        say.fail(f"{path} already exists", fix="--force overwrites it",
                 code=2, blank_line=False)
    # `--force` is the ONLY write of STARTER in this tool that lands on a file
    # that already exists — the three others (`discover`, `create`, `setup`) are
    # each guarded by `if not path.exists()`. So it is the only one that can
    # destroy a host list, and it used to be a bare `write_text`: no copy, no
    # read-back, no atomic replace, and nothing to restore afterwards.
    #
    # The sharper half was what it did to an EXISTING backup. `hostfile.apply`
    # keeps `hosts.toml.bak` one rewrite behind the live file, so after an
    # ordinary rewrite v1 -> v2 the pair reads live=v2, .bak=v1. `--force` then
    # replaced the live file and left the backup alone:
    #
    #     live = STARTER,  .bak = STILL v1
    #
    # v2 — the state the overwrite actually destroyed — was gone with no copy
    # anywhere, and `hosts.toml.bak` is the recovery path docs/machines.md,
    # docs/troubleshooting.md and R5d/R5f all point at. Restoring it handed back
    # a host list one rewrite too old AND LOOKED LIKE IT HAD WORKED. A missing
    # backup announces itself; a stale one does not.
    #
    # So this goes through the same path `move` and `delete` use. The file is
    # hand-maintained and has no other copy on this machine, which is the whole
    # argument in `hostfile`'s docstring, and it does not stop being true because
    # the caller typed `--force`. `--force` still means "overwrite it" — it now
    # means "overwrite it recoverably", and if a verified copy cannot be made it
    # refuses, exactly as a move does. Declining an overwrite is recoverable;
    # performing one that cannot be undone is not.
    #
    # A folder you cannot write to, and `--force` aimed at a directory, both
    # arrive here as an OSError. This is the command someone runs first, so a
    # traceback is the first thing the tool would ever show them.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # `expect` is what STARTER declares. Checking it here is not ceremony:
        # it means a STARTER edited into something the loader would refuse is
        # caught before it lands, rather than after, on the one file every other
        # command has to read.
        apply(path, STARTER, expect={"local"})
    except HostFileError as exc:
        # The message already says what was not done and that nothing was
        # written; it is documented where it is raised, in `hostfile`.
        say.fail(exc, code=2, blank_line=False)
    except OSError as exc:
        say.fail(f"could not write a host list to {path}: {exc}",
                 fix="give --config a path you can write to — the file itself, "
                     "not the folder it goes in",
                 code=2, blank_line=False)
    say.result(f"wrote {path}")
    say.result("add your cloud boxes to it, then: comfy-qat list")


@app.command("discover")
def discover_cmd(
    config: ConfigOption = None,
    dry_run: Annotated[bool, typer.Option(
        "--dry-run", help="Show what would be added without writing anything.")] = False,
) -> None:
    """Find cloud boxes on your project and add the ones you do not have yet.

    Google already knows each box's zone, card and operating system, so nothing
    here needs typing by hand. Existing entries are never touched.

    This one writes: your host list — the file `--config` names, and
    ~/.config/comfy-qa-tools/hosts.toml when it is left off — is read and then
    appended to. `--dry-run` shows what would be added and writes nothing.
    """
    from .gcloud import Gcloud, GcloudError
    from .discover import (
        clash_note, label_clashes, new_hosts, parse as parse_instance, to_toml,
    )
    from .hostfile import HostFileError, add

    path = config or DEFAULT_CONFIG_PATH
    gc = Gcloud()
    try:
        project = gc.current_project()
        if not project:
            say.fail("no project set", fix="comfy-qat setup", code=2,
                     blank_line=False)
        instances = gc.list_instances(project)
    except GcloudError as exc:
        _refused(exc)

    found = [parse_instance(instance, project) for instance in instances]
    if not found:
        say.result(f"no cloud boxes on {project}")
        return

    try:
        existing = load(path)
    except ConfigError as exc:
        if path.exists():
            # Treating an unreadable host list as an empty one is how this
            # command makes a bad file worse: the entry lands, the file still
            # does not load, and the one command whose job is telling you what
            # exists reports success. `setup` has refused this since it hit it;
            # the wording is deliberately the same, because it is the same
            # refusal about the same file.
            say.fail(f"could not read your host list ({exc}), so nothing was added to it",
                     fix="fix the file, then run this again", code=2, blank_line=False)
        existing = []

    additions = new_hosts(found, existing)
    clashes = label_clashes(found, existing)
    if not additions and not clashes:
        say.result(f"{say.count(len(found), 'cloud box', 'cloud boxes')}, "
                   f"all already in {path}")
        return

    for box, port in additions:
        state = "running" if box.running else "stopped"
        say.result(f"{box.name}  {box.os}  {box.gpu or 'no GPU'}  {state}  port {port}")
    for box, label in clashes:
        say.result(clash_note(box, label))

    if dry_run:
        say.result("\n--dry-run: nothing written")
        return
    if not additions:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Not `path.open("a")`. An append lands in the same hand-maintained file
        # a rewrite does, so it takes the same route: validated with the real
        # loader before it is written, a verified copy kept, and refused rather
        # than left unloadable. See `hostfile.add`.
        add(path, [to_toml(box, port) for box, port in additions], initial=STARTER)
    except HostFileError as exc:
        # Documented where it is raised, in `hostfile`, and it already says that
        # nothing was written.
        say.fail(exc, code=2, blank_line=False)
    say.result(f"\nadded {say.count(len(additions), 'host')} to {path}")


@app.command("create")
def create_cmd(
    os_choice: Annotated[str, typer.Option(
        "--os", help="linux or windows. One box per OS is the pattern here.")],
    gpu: Annotated[str, typer.Option(
        "--gpu", help="The card: l4, t4, a100... The machine type follows from it.")],
    name: Annotated[Optional[str], typer.Option(
        "--name", help="Name the box. Default: comfy-linux / comfy-win, numbered if taken.")] = None,
    zone: Annotated[Optional[str], typer.Option(
        "--zone", help="Use this zone and only this zone. Default: chosen for you.")] = None,
    region: Annotated[Optional[str], typer.Option(
        "--region", help="Narrow to one region; the zone inside it is still chosen.")] = None,
    disk: Annotated[int, typer.Option(
        "--disk", help="Boot disk in GB. Models live on it.")] = 200,
    config: ConfigOption = None,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask before creating.")] = False,
    dry_run: Annotated[bool, typer.Option(
        "--dry-run", help="Print the plan, the quota and the zone order. Create nothing.")] = False,
) -> None:
    """Create a GPU box, choosing the zone for you.

    The card is the only real decision. The machine type follows from it — an L4
    is a G2 with the GPU built in, a T4 is an N1 with one attached — and the zone
    is chosen: regions this project holds quota in, zones inside them that offer
    the card and the machine type, ranked by latency measured from here, and
    tried in order until one has capacity.

    Quota is checked before anything exists, because a refusal costs nothing and
    a quota failure after the instance exists costs money and a cleanup.

    This one writes: the new box is appended to your host list — the file
    `--config` names, and ~/.config/comfy-qa-tools/hosts.toml when it is left
    off — so the file is read and then rewritten. `--dry-run` writes nothing.
    """
    from .create import (
        build, check_quota, host_entry, next_steps, nowhere, order_zones,
        plan, summary, taken_names,
    )
    from .discover import next_ports, to_toml
    from .gcloud import Gcloud, GcloudError

    # Before anything is read from Google, because this is a string check and
    # `create` otherwise spends a minute on quota before saying anything.
    #
    # `--zone` is "use this zone and only this zone" and `--region` is "narrow to
    # one region; the zone inside it is still chosen". Together they are not an
    # expressible intention, they are a mistake — and `--zone` silently won, so
    # `--region europe-west2 --zone us-central1-a` created a box in Iowa without
    # ever mentioning the region it discarded.
    #
    # The rule, which generalises past this pair: refuse when the ignored flag
    # would have changed the outcome; ignore quietly when it could not. `--yes`
    # with `--dry-run` is the same shape and is fine, because `--yes` only
    # suppresses a prompt `--dry-run` never reaches.
    #
    # This is the position `_selector` already takes for --os/--gpu: "picking a
    # winner would carry that mistake out on a machine". Here the machine costs
    # money and can land on the wrong continent.
    if zone and region:
        say.fail(
            f"--zone {zone} and --region {region} cannot both be right: --zone "
            "pins one zone, --region asks for a choice within one region",
            fix=say.fix(
                "one or the other:",
                f"comfy-qat create --os {os_choice} --gpu {gpu} --zone {zone}",
                f"comfy-qat create --os {os_choice} --gpu {gpu} --region {region}",
            ),
            code=2,
        )

    path = config or DEFAULT_CONFIG_PATH
    try:
        hosts = load(path)
    except ConfigError:
        hosts = []

    # `plan` is offline and total — it decides everything before anything is
    # contacted — but it was called AFTER the project read, the instance list
    # and the minute of quota. So `--os freebsd` cost 64 seconds to be told
    # freebsd is not an operating system, and so did a 0 GB disk and a name
    # Compute Engine will not take. All three are string checks against a table
    # that ships with this tool.
    #
    # Run here first, against the host list alone, and thrown away. The name
    # COLLISION check deliberately stays below: "already taken" means taken on
    # the project too, and that genuinely needs the instance list. So a name
    # clashing with your own host list is refused in a second, and one clashing
    # with an undeclared box on the project still costs the read it requires.
    try:
        plan(os_choice=os_choice, gpu=gpu, name=name, disk_gb=disk,
             taken=taken_names(hosts, []))
    except _reportable() as exc:
        _refused(exc)

    gc = Gcloud()
    try:
        project = gc.current_project()
        if not project:
            say.fail("no project set", fix="comfy-qat setup", code=2,
                     blank_line=False)
        instances = gc.list_instances(project)
        # The first place `create` goes quiet, and long enough that silence reads
        # as a hang. Timed rather than announced once and then nothing.
        # `with`, not `try/finally: done()`. `done()` prints "done in 62s" —
        # a claim that the read finished — and `finally` ran it just as loudly
        # when `gpu_quotas` raised, so a failed quota read announced its own
        # completion one line above the refusal explaining that it had not
        # completed. `Slow.__exit__` already makes exactly this distinction:
        # `done()` when nothing was raised, `give_up()` when something was.
        with say.slow("reading quota", expect="about a minute"):
            quotas = gc.gpu_quotas(project)
    except GcloudError as exc:
        _refused(exc)

    try:
        blueprint = plan(os_choice=os_choice, gpu=gpu, name=name, disk_gb=disk,
                         taken=taken_names(hosts, instances))
        check = check_quota(blueprint.card, quotas, instances)
        say.result("\nquota checked:")
        for line in check.lines():
            say.result(f"  {line}")
        problem = check.problem()
        if problem is not None:
            _refused(problem)
        ordering = order_zones(gc, project, blueprint, check,
                               zone=zone, region=region, config=path)
    except _reportable() as exc:
        _refused(exc)
    except GcloudError as exc:
        _refused(exc)

    if not ordering:
        _refused(nowhere(blueprint, ordering, project))

    say.result("")
    for line in blueprint.steps(ordering.zones[0]):
        say.result(f"  - {line}")
    say.result("")
    for line in summary(blueprint, ordering):
        say.result(line)
    for note in ordering.notes:
        say.result(f"\nnote: {note}")

    if dry_run:
        say.result("\n--dry-run: nothing created")
        return
    if not yes and not typer.confirm(f"\nCreate {blueprint.name}?"):
        say.result("nothing changed")
        return

    say.result("")
    try:
        made_in = build(gc, blueprint, ordering, project, say.step)
    except _reportable() + (GcloudError,) as exc:
        say.fail(exc, code=1)
    # No `except KeyboardInterrupt` here, and that is the fix rather than an
    # omission. `create.build` registers the instance with `inflight` around the
    # one call that creates it, so the interrupt is reported by `cli.main` with
    # the zone the attempt was ACTUALLY in. This frame only ever knew
    # `ordering.zones[0]`, which is right until the first stockout moves the
    # create down the list — and a stockout-heavy day is exactly when the loop is
    # long enough to be interrupted, so the handler here was most likely to be
    # wrong precisely when it was most likely to fire.

    # Re-read rather than reusing the list from before the create: this command
    # takes minutes, and a `host discover` in another terminal in the meantime
    # would have taken the port this was about to hand out. Two hosts on one port
    # is the failure you cannot diagnose from the outside.
    try:
        port = next_ports(load(path), 1)[0]
    except ConfigError:
        port = next_ports(hosts, 1)[0]

    # Still a bare append, and deliberately, having been through `hostfile.add`
    # and taken back out.
    #
    # There is no defect here to fix. `create.taken_names` folds host labels,
    # gce_instances and the project's own instance names to lower case before
    # `choose_name` compares — and `_clean` lowercases `--name` too — so the case
    # clash `discover` walks into cannot be reached from this command. What
    # `apply` would add is the verified copy, on the one write in this tool that
    # happens after money is already being spent, which is a real thing to want.
    #
    # It costs more than it gives, and the measurement is the argument. `apply`
    # finishes with `os.replace`, which succeeds on a READ-ONLY hosts.toml
    # whenever the directory is writable; `open("a")` raises PermissionError. So
    # routing this through it makes
    # `test_a_box_that_exists_with_no_host_list_entry_is_told_how_to_be_stopped`
    # pass by having nothing left to test — the write that test pins as FAILING
    # now succeeds, and with it goes the only cover over the message printed
    # after the box is real and billing. A refactor that makes a test pass is not
    # automatically an improvement. Rewriting that test to keep a consistency
    # change is the wrong way round, and quietly widening what this tool will
    # overwrite is not something to do in passing.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(STARTER, encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(to_toml(host_entry(blueprint, made_in, project), port))
    except OSError as exc:
        # The only message in this command printed after money is being spent,
        # and the one place the order of the sentences matters. The box is real,
        # it is billing, and the host list has no record of it — so `comfy-qat down`
        # cannot reach it and the raw gcloud stop is the only thing that works.
        # It leads, on its own line, ahead of the adoption path and ahead of the
        # OSError text, which can be long enough on its own to push a command at
        # the end of a paragraph out of sight.
        say.error(f"{blueprint.name} exists in {made_in} and is billing",
                  say.fix("stop it now:",
                          f"gcloud compute instances stop {blueprint.name} "
                          f"--zone={made_in} --project={project}"))
        say.fail(f"{blueprint.name} could not be added to {path}: {exc}",
                 fix="add it by hand, or adopt it: comfy-qat discover", code=1)

    say.result(f"\n{blueprint.name} is up in {made_in}, on port {port}.")
    for line in next_steps(blueprint, made_in):
        say.result(line)


def _selector(name: str | None, os_: str | None, gpu: str | None) -> str:
    """One selector, from a positional or from `--os` / `--gpu`.

    `go windows/l4` is shorter and is what anyone types by hand; the flags are
    for a script, or for when a machine's name could be mistaken for a
    description. One flag each rather than `--windows` and `--gpu-l4`: two
    booleans can contradict each other, and a flag per card is a namespace that
    grows every time Google ships one.

    Giving both is an error rather than a precedence rule. Someone who typed
    `go windows --os linux` has made a mistake, and picking a winner would carry
    that mistake out on a machine.

    ---

    **`--os` and `--gpu` are hidden on all eleven commands that reach this
    function, and this is the deprecation window, not a second permanent
    spelling.** They still work, they no longer appear in `--help`, and anyone
    who uses one is told the shorter form below.

    Hidden rather than kept because the flags were never a second spelling of
    one idea — the name `--os`/`--gpu` carries three different meanings in this
    tool, and a reader of `--help` had no way to tell which one they were
    looking at:

      - `create --os windows --gpu l4` DESCRIBES A BOX TO BUILD. Both are
        required and neither selects anything; there is nothing to select yet.
      - the eleven commands here PICK AN EXISTING MACHINE, optionally, out of
        the host list — and every one of them already takes that same value as
        a positional argument.
      - `quota request --gpu l4,a100` names A COMMA-SEPARATED LIST of cards to
        ask Google for. No selector accepts that value, and `create` does not
        either.

    So `create` and `quota request` keep theirs, for two different reasons, and
    the eleven redundant pairs go. What is left is one way to say which machine
    you mean, on every command that takes one.

    The note goes to stderr, on purpose. A deprecation nobody is told about
    never ends: the flags vanish from `--help` on the day this ships, so the
    only person who can still discover the shorter form is the person still
    typing the longer one — and `warn` is where this tool already puts exactly
    this (`down --keep-running`). stderr keeps `--json` and `--dry-run` output
    clean for the script that is the other reason these flags existed.
    """
    described = SEPARATOR.join(part for part in (os_, gpu) if part)
    if name and described:
        raise typer.BadParameter(
            f"say the machine once: {name!r} as an argument, or --os/--gpu, not both."
        )
    if not name and not described:
        raise typer.BadParameter(
            "which machine? A name, an operating system, a card, or both as "
            "os/card — or --os and --gpu."
        )
    if described:
        # After both refusals, never instead of one: someone who typed a name
        # AND a flag has a mistake to fix, not a spelling to update, and two
        # messages about one command would bury the one that matters.
        say.warn("--os/--gpu are on their way out. They still work; say it as "
                 f"the argument instead: {described}")
    return name or described


def _lookup(name: str, config: Optional[Path]) -> tuple[list[Host], Host]:
    """The whole host list, and the one machine the argument meant.

    The resolution line goes to stderr so that `--json` and `--dry-run` keep
    printing only the thing you were going to paste, while you still see which
    machine `windows` turned out to be.
    """
    try:
        hosts = load(config)
        chosen = resolve(hosts, name)
    except ConfigError as exc:
        say.fail(exc, code=2, blank_line=False)
    if chosen.line() is not None:
        say.step(chosen.line())
    return hosts, chosen.host


def _host(name: str, config: Optional[Path]) -> Host:
    return _lookup(name, config)[1]


def _reportable() -> tuple[type, ...]:
    """The failures this tool answers with a message rather than a traceback.

    `TunnelError` is not a `LifecycleError` and cannot become one — `lifecycle`
    imports `tunnel`, so the dependency only runs one way — but it was given the
    same shape on purpose: a message, a `fix`, and a `kind`. Every handler here
    reads exactly those three, so naming both types is the whole of the work.
    Anything reaching a person through `bring_up` or `serve` can raise either.
    """
    from .lifecycle import LifecycleError
    from .tunnel import TunnelError

    return (LifecycleError, TunnelError)


def _refused(exc, code: int = 2) -> None:
    """A gcloud refusal, reported the way every other command in this group does.

    One shape for the whole `host` group, because a tester reads exit codes across
    commands: **2 means nothing was changed** — a refusal, a precondition, a bad
    argument — and **1 means the work started and failed.** `move` used to exit 1
    with no `to fix:` line where `auth quota list` and `host discover` exited 2
    with one, on the same gcloud error.
    """
    say.fail(exc, code=code, blank_line=False)


def _act(action, *args, **kwargs):
    """Run a lifecycle step, turning its failures into messages, never tracebacks.

    The exit code comes from the failure, not from this function. `say` states
    the rule: 2 is "the command could not start — bad input, or a precondition
    unmet", 1 is "the thing you asked for did not happen". Every reportable
    failure was flattened to 1 here, and the flattening was reported as one
    command's defect — `logs` on a stopped box exiting 1 where every other
    refusal in the tool exits 2.

    It was not one command's. Reading all three callees: `read_logs` raises five
    refusals and one real failure, `in_a_new_window` raises a platform check
    that says "Nothing was started" and two osascript failures, and `put_away`
    raises a host-list contradiction it refuses to act on, plus a stop that was
    attempted and failed. So `logs`, `go --new-window`, `disconnect`, `down` and
    `switch` were all affected, and fixing this at the `logs` call site would
    have left four of them — which is the shape that has cost the most tonight:
    a fix landing where the problem was noticed rather than where it lives.

    Only the raise knows which of the two it is, so the flag is set there and
    read here. `getattr` rather than an attribute access, because
    `_reportable()` is two unrelated classes — `TunnelError` is not a
    `LifecycleError` and cannot become one — and an unmarked failure keeping
    today's 1 is the safe default: it claims less.
    """
    try:
        return action(*args, **kwargs)
    except _reportable() as exc:
        say.fail(exc, code=2 if getattr(exc, "refusal", False) else 1)


# The four words `put_away` answers with, from nine `return` statements. Named
# here rather than inline at each of the two call sites, so that adding a fifth
# is one edit and not a search.
VERDICTS = ("caught", "billing", "idle", "unknown")


def _known_verdict(host: Host, found: str) -> str:
    """`put_away`'s answer, or "unknown" — out loud — if it is not one of them.

    Both callers used to read this through `dict.get(found, [])`, which appended
    to a throwaway list: a verdict neither of them named dropped the machine out
    of every count and every closing sentence in silence, in the one command that
    exists to answer "am I still paying for anything". An undercount is the
    expensive direction, so an answer nobody recognises is read as "I do not
    know", which is what it is, and is said rather than swallowed.
    """
    if found in VERDICTS:
        return found
    say.warn(f"{host.name} came back from stopping with an outcome this tool "
             f"does not recognise ({found!r}), so it is counted as unchecked")
    return "unknown"


@app.command("up")
def up_cmd(
    name: Annotated[Optional[str], typer.Argument(help="Which machine: a name, or what you want — windows, l4, windows/l4.")] = None,
    config: ConfigOption = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", hidden=True, help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", hidden=True, help="Pick by card: l4, t4, a100.")] = None,
) -> None:
    """Start a machine and wait until ComfyUI actually answers.

    "Up" means ComfyUI is serving, not that the VM booted. A machine that has
    booted and serves nothing looks like success and bills like success.
    """
    from .gcloud import Gcloud
    from .lifecycle import bring_up

    hosts, host = _lookup(_selector(name, os_, gpu), config)
    try:
        bring_up(Gcloud(), host, say.step)
    except _reportable() as exc:
        # A box that will not start ends the session unless you are told where
        # else you could work, and a GPU shortage is the usual reason.
        _failed(host, hosts, exc)
        raise typer.Exit(code=1)
    # The box is running and billing from here, and this was the end of the
    # command. Every other billable path in this tool says how to stop paying —
    # `go`, `switch`, `create`, `move`, `logs` and `down --all` all do — and the
    # rule those six follow, which nothing enforced, is: anything that STARTS a
    # machine or deliberately LEAVES one running names stop_paying before it
    # returns.
    say.result(f"\nopen {host.url}")
    say.result(f"  comfy-qat down {host.name}   # stop the box, stop paying")


@app.command("open")
def open_cmd(
    name: Annotated[Optional[str], typer.Argument(help="Which machine: a name, or what you want — windows, l4, windows/l4.")] = None,
    config: ConfigOption = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", hidden=True, help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", hidden=True, help="Pick by card: l4, t4, a100.")] = None,
    dry_run: Annotated[bool, typer.Option(
        "--dry-run", help="Print the tunnel command instead of running it.")] = False,
) -> None:
    """Open a tunnel to a machine that is already running.

    An `ssh -L` forward carried over Identity-Aware Proxy: no port is opened on
    the instance and no public route to it exists, which matters because ComfyUI
    has no authentication. It does use gcloud's own SSH key, and it forwards to
    the box's loopback — where ComfyUI listens, and where nothing else can reach.
    """
    from .tunnel import (
        TunnelError,
        command as tunnel_command,
        open_tunnel,
        status as tunnel_status,
    )

    host = _host(_selector(name, os_, gpu), config)
    if not host.is_remote:
        # Word for word what `tunnel.py` raises for the same host, so there is
        # one sentence for this and not two spellings of it.
        say.result(f"{host.name} is local — there is nothing to tunnel. "
                   f"It is at {host.url}.")
        return

    if dry_run:
        say.result(" ".join(tunnel_command(host)))
        return

    # There used to be a `if tunnel_status(host.name).running: return` here, and
    # it decided from the name alone. A name is not a machine: a second host list
    # can call a different box `comfy-win` too, and that early return printed
    # *this* host list's URL for *that* host list's tunnel. `open_tunnel` is the
    # only thing that knows — it compares the recorded instance, zone, project and
    # port against the host — so the question is asked there and nowhere else.
    # Reuse is still reported as reuse: `open_tunnel` hands back the live state
    # untouched when it really is the same machine.
    before = tunnel_status(host.name)
    try:
        state = open_tunnel(host)
    except TunnelError as exc:
        say.fail(exc, code=2)

    reused = before.running and before.pid == state.pid
    opened = "tunnel already open" if reused else "tunnel open"
    say.result(f"{opened} (pid {state.pid}): {state.url or host.url}")


@app.command("disconnect")
def disconnect_cmd(
    name: Annotated[Optional[str], typer.Argument(help="Which machine: a name, or what you want — windows, l4.")] = None,
    config: ConfigOption = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", hidden=True, help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", hidden=True, help="Pick by card: l4, t4, a100.")] = None,
) -> None:
    """Close the tunnel and leave the machine running.

    For the case `down` cannot serve: a long generation or a model download is
    running on the box, ComfyUI is detached and will keep going, and you want the
    local port back — or you are closing the laptop. Killing the ssh process by
    hand leaves the tunnel records behind, after which `list` reports a tunnel
    that is not there.

    This was `down --keep-running`, which is still accepted and still works. The
    flag was the negation of its own command, one word from the command whose
    documented purpose is to stop paying, and `down --all --keep-running` read as
    "stop everything except don't" — the most expensive outcome reachable from the
    cheapest-sounding command. It was also the only branch of `down` that nobody
    exercised, which is why it was wrong about money twice in one day, in opposite
    directions.

    The machine keeps billing. That is the point of the command and it says so.
    """
    from .gcloud import Gcloud
    from .lifecycle import put_away

    host = _host(_selector(name, os_, gpu), config)
    _act(put_away, Gcloud(), host, say.detail, keep_running=True)
    # The command whose whole purpose is leaving a box running is the one that
    # most needs to say how to stop it. It did not — and the test that states
    # the rule caught it on its first run, having been written for two other
    # commands.
    say.result(f"  comfy-qat down {host.name}   # when the work is finished")


@app.command("down")
def down_cmd(
    name: Annotated[Optional[str], typer.Argument(
        help="Which machine: a name, or what you want — windows, l4, windows/l4. "
             "Omit it with --all.")] = None,
    config: ConfigOption = None,
    # `down` was the only machine-taking command without these — go, up, open,
    # switch, logs, stamp, ssh, rdp and move all take them — so a session spent
    # typing `go --os windows` ended at `down --os windows`, which was refused
    # by the argument parser. Being refused is loud, but this is the one command
    # where not running is what costs money.
    os_: Annotated[Optional[str], typer.Option(
        "--os", hidden=True, help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", hidden=True, help="Pick by card: l4, t4, a100.")] = None,
    keep_running: Annotated[bool, typer.Option(
        "--keep-running",
        help="Deprecated: this is `comfy-qat disconnect`.")] = False,
    everything: Annotated[bool, typer.Option(
        "--all", help="Stop every cloud machine you have declared.")] = False,
) -> None:
    """Close the tunnel and stop the machine, so it stops costing money.

    `--all` exists because the question at the end of a session is never "is
    comfy-win stopped", it is "am I still paying for anything" — and answering
    that by naming each box in turn is how one gets missed.
    """
    from .gcloud import Gcloud
    from .lifecycle import put_away, stop_paying

    # Both paths, not just --all: the single-host form is the one someone types
    # out of habit.
    if keep_running:
        say.warn("`--keep-running` is now `comfy-qat disconnect <name>`. "
                 "The flag still works.")

    if everything:
        if name:
            say.fail("--all stops every machine, so it takes no name", code=2,
                     blank_line=False)
        # Kept apart from the name refusal above rather than folded into one
        # sentence: the two mistakes read differently, and `--all --os windows`
        # is a person narrowing what they meant, not naming a box.
        if os_ or gpu:
            say.fail("--all stops every machine, so it takes no --os or --gpu",
                     code=2, blank_line=False)
        try:
            hosts = [h for h in load(config) if h.is_remote]
        except ConfigError as exc:
            say.fail(exc, code=2, blank_line=False)
        gc = Gcloud()
        if not hosts:
            # "nothing can be billing" was a flat assertion about the PROJECT
            # made without asking it, on the one path where this tool knows
            # least — no declared cloud hosts is exactly when the project is
            # most likely to hold something nobody adopted.
            #
            # The survey was added for this and then placed after this return,
            # four lines below, so it never ran here. That is the shape of half
            # tonight's defects: the fix was real and did not reach the branch
            # that needed it most.
            strangers = _undeclared_and_running(gc, hosts)
            if strangers:
                say.result(
                    f"you have declared no cloud machines, but "
                    f"{say.count(len(strangers), 'machine')} on this project is "
                    f"running: {', '.join(n for n, _ in strangers)}."
                )
                say.result("  not stopped — this tool only operates what you "
                           "declare:")
                for name, zone in strangers:
                    say.result(f"  gcloud compute instances stop {name} "
                               f"--zone={zone}")
                say.result("  comfy-qat discover   # or adopt them first")
            elif strangers is None:
                say.result("no cloud machines are declared, and the project "
                           "could not be checked — so this is not an all-clear.")
                say.result("  comfy-qat list --live")
            else:
                say.result("no cloud machines are declared, and nothing is "
                           "running on the project either")
            return

        failed = []
        billing: list[Host] = []
        unknown: list[Host] = []
        stopped: list[Host] = []
        # Collected and deliberately never reported — see the mapping below. It
        # sits here with the other three so that the set of verdicts is visible
        # in one place rather than inferred from what is missing.
        idle: list[Host] = []
        for host in hosts:
            say.step(host.name)
            try:
                found = put_away(gc, host, say.detail, keep_running=keep_running)
                # `.get(found, []).append(host)` appended to a throwaway list, so
                # any verdict this did not name dropped the machine out of every
                # count and every closing sentence without a word.
                #
                # And one WAS unnamed. `put_away` returns four verdicts, from
                # nine places; this mapping knew three. `idle` — a box that was
                # already stopped, or a local install that costs nothing — was
                # being silently discarded on every run, and discarding it is
                # correct: an idle box belongs in none of these three counts, so
                # the summary came out right by way of a fall-through nobody had
                # written down. It is named here so that it is a decision.
                #
                # Which leaves the default to mean what it should: a verdict this
                # tool does not understand. That is counted as UNCHECKED, not as
                # stopped — the summary already has a sentence for machines it
                # could not settle, and "I do not know" is the honest reading of
                # a word nobody here recognises. Saying so out loud is the point:
                # the cost of the old silence was an undercount in the one
                # command that exists to answer "am I still paying for anything".
                bucket = {"unknown": unknown, "billing": billing,
                          "caught": stopped, "idle": idle}.get(
                              _known_verdict(host, found))
                bucket.append(host)
            except LifecycleError as exc:
                # One machine refusing to stop must not leave the rest running —
                # that is the whole reason for stopping them in one command.
                failed.append((host, exc))
                say.detail(str(exc))
        # Ask the PROJECT, not just the host list. Everything above surveys
        # `hosts`, so a running box nobody declared was invisible to every
        # sentence this command prints — and "nothing is now" is a bigger promise
        # than the "all N stopped" it replaced, so the blind spot got more
        # expensive when the wording got better.
        #
        # They are named, not stopped. Stopping a machine this tool does not
        # manage is beyond what `down` was asked to do, and the surprise would be
        # worse than the bill. Saying nothing about it is what makes the summary
        # a lie.
        strangers = _undeclared_and_running(gc, hosts)

        if failed:
            say.error(
                f"{len(failed)} of {len(hosts)} did not stop and may still be billing",
                say.fix(*[f"{other.name} — {exc.fix or 'stop it in the console'}"
                          for other, exc in failed]))
            raise typer.Exit(code=1)
        if keep_running:
            # The one command whose purpose is answering "am I still paying" has
            # now been wrong in both directions here: it claimed everything had
            # stopped, then claimed everything was billing. Count what was read.
            # Three sentences and not one, because "it is off" and "I could not
            # tell" are different answers and only one of them is free.
            if unknown:
                say.result(f"\n{say.count(len(unknown), 'machine')} could not be "
                           "checked — run `comfy-qat list --live`.")
            if billing:
                say.result(f"\n{say.count(len(billing), 'machine')} left running "
                           f"and billing: {', '.join(h.name for h in billing)}.")
                say.result("Run without --keep-running to stop them.")
            elif not unknown and strangers == []:
                say.result("\nnothing was running, so nothing is billing.")
        else:
            # "all N stopped." was printed whether five GPU boxes had been
            # billing all night or none, because stopping an already-stopped box
            # succeeds trivially. The one question this command exists to answer
            # was the one its output could not distinguish.
            caught = [h for h in hosts if h in stopped]
            if caught:
                names = ", ".join(h.name for h in caught)
                say.result(f"\nwas billing: {names}. Stopped. Nothing is now.")
            elif not unknown and strangers == []:
                say.result("\nnothing was running, so nothing was billing.")
            # `unknown` was collected here and never reported, so a run where
            # every read failed and every stop succeeded printed the all-clear —
            # an unearned one, contradicting the per-host line three lines above
            # it. The --keep-running branch had always said this and the default
            # branch had not, which is the same asymmetry in its last corner.
            if unknown:
                names = ", ".join(h.name for h in unknown)
                say.result(
                    f"\n{say.count(len(unknown), 'machine')} could not be checked "
                    f"before stopping, so it may have been billing: {names}. "
                    "Everything else was not running."
                )
                say.result("  comfy-qat list --live")

        if strangers is None:
            say.result("\nthe project could not be checked for machines you have "
                       "not declared, so this is not an all-clear.")
            say.result("  comfy-qat list --live")
        elif strangers:
            say.result(
                f"\n{say.count(len(strangers), 'machine')} on this project is "
                f"running and not in your host list: "
                f"{', '.join(n for n, _ in strangers)}."
            )
            say.result("  not stopped — this tool only operates what you declare:")
            for name, zone in strangers:
                say.result(f"  gcloud compute instances stop {name} --zone={zone}")
            say.result("  comfy-qat discover   # or adopt them and use `down --all`")
        return

    # Not `_selector` alone: its "which machine?" does not know about `--all`,
    # and `--all` is the answer half the people who get here wanted. Given a
    # selector, `_selector` takes over — including its refusal of a name and
    # --os together, which every sibling already gives.
    if not name and not os_ and not gpu:
        say.fail("say which machine, or --all for every one of them", code=2,
                 blank_line=False)

    host = _host(_selector(name, os_, gpu), config)
    found = _act(put_away, Gcloud(), host, say.step, keep_running=keep_running)

    # `down --all` ends with its money summary on stdout and the per-host story
    # on stderr. This form printed the story and stopped, so `comfy-qat down
    # comfy-win 2>/dev/null` said NOTHING AT ALL about money — two forms of one
    # command disagreeing about where the answer goes, and the mirror of the
    # `move --dry-run` defect where everything went to stdout and stderr was
    # empty. One rule catches both: a command about money has an answer and a
    # story, and neither stream is empty.
    #
    # Not a duplicate of `put_away`'s own line, for the reason `--all` is not:
    # that line is the story of one machine on stderr, this is the answer on
    # stdout, and someone who redirects either away still has the other.
    verdict = _known_verdict(host, found)
    say.result({
        "caught": f"\n{host.name} was billing. Stopped.",
        "idle": f"\n{host.name} was not running, so nothing was billing.",
        "billing": f"\n{host.name} is left running, and it is billing.",
        "unknown": f"\n{host.name} could not be checked before stopping, so it "
                   f"may have been billing.",
    }[verdict])
    # Read from the SETTLED verdict, not from the raw answer: an unrecognised
    # one becomes "unknown" above, and it needs the way to go and look more than
    # any of the others do.
    if verdict == "unknown":
        say.result("  comfy-qat list --live")
    elif verdict == "billing":
        say.result(f"  {stop_paying(host)}   # stop the box, stop paying")


@app.command("go")
def go_cmd(
    name: Annotated[Optional[str], typer.Argument(help="Which machine: a name, or what you want — windows, l4, windows/l4.")] = None,
    config: ConfigOption = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", hidden=True, help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", hidden=True, help="Pick by card: l4, t4, a100.")] = None,
    no_browser: Annotated[bool, typer.Option(
        "--no-browser", help="Do not open a browser when ComfyUI answers.")] = False,
    no_install: Annotated[bool, typer.Option(
        "--no-install", help="Fail rather than installing ComfyUI if it is absent.")] = False,
    follow: Annotated[bool, typer.Option(
        "--follow",
        help="Stream ComfyUI's log here, and Ctrl-C then stops ComfyUI itself. "
             "To watch without that, use `comfy-qat logs`.")] = False,
    new_window: Annotated[bool, typer.Option(
        "--new-window",
        help="Run this in a new macOS Terminal window instead. That window runs "
             "what you asked for and nothing more, so Ctrl-C in it stops nothing "
             "on the box — unless you passed --follow too, and then Ctrl-C "
             "there stops ComfyUI itself.")] = False,
) -> None:
    """Start the machine, make sure ComfyUI is on it, and hand the prompt back.

    The everyday command. If ComfyUI is already serving you get the URL straight
    away; otherwise it is installed if needed and launched *on the box*, where it
    keeps running after this returns — so a second machine can be brought up in
    this same terminal. `host logs` reads its log; `--follow` streams it here
    instead, and Ctrl-C then stops ComfyUI, which is what this used to do always.
    """
    from .gcloud import Gcloud
    from .lifecycle import in_a_new_window

    hosts, host = _lookup(_selector(name, os_, gpu), config)
    if new_window:
        # Before anything is started: a hand-off that fails must not leave a box
        # running behind a window that never opened.
        #
        # `go`, not `host go`. cli.py says in as many words that the `host` group
        # is a deprecation window and not a second permanent spelling, and this
        # line was the last caller inside the tool still using it — so deleting
        # that group would have broken `--new-window` and nothing else, inside a
        # spawned Terminal window, on the command that starts a GPU box, which is
        # the least visible place in this tool for anything to break.
        #
        # And `--follow` only when it was asked for. It used to be appended here
        # unconditionally, on the theory that a window needs something to hold it
        # open. It does not: Terminal's `do script` runs the command in a new
        # interactive shell and the shell outlives the command, so the window and
        # its scrollback stay either way. What the implication did buy was the
        # interrupt — under `--follow` the log is streamed over SSH and Ctrl-C
        # reaches ComfyUI on the box and stops it — so someone who spawned a
        # window and then interrupted it expecting to detach stopped the thing
        # they had just started, on a machine they are still paying for, having
        # never typed the flag that does that.
        rest = ["go", host.name]
        rest += ["--follow"] if follow else []
        rest += ["--config", str(config)] if config else []
        rest += ["--no-browser"] if no_browser else []
        rest += ["--no-install"] if no_install else []
        _act(in_a_new_window, rest, say.step)
        return
    gc = Gcloud()
    # Only `go` offers the rebuild. `up` and `switch` share _bring_up, and switch
    # stops the other boxes immediately afterwards — confirming a move there would
    # leave it half executed, old box not stopped and new box not up.
    ready = _bring_up(gc, host, hosts, offer_move=config or True)
    _serve(gc, host, ready, no_browser=no_browser, no_install=no_install,
           follow=follow)


@app.command("ssh")
def ssh_cmd(
    name: Annotated[Optional[str], typer.Argument(help="Which machine: a name, or what you want — linux, l4.")] = None,
    config: ConfigOption = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", hidden=True, help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", hidden=True, help="Pick by card: l4, t4, a100.")] = None,
) -> None:
    """Open a shell on a box, through the tunnel.

    The long form is `gcloud compute ssh <instance> --tunnel-through-iap --zone
    <zone> --project <project>`, and this tool already knows the last three. Every
    fix line that used to print that now says `comfy-qat ssh <box>`.
    """
    import os as os_module

    from .gcloud import Gcloud, GcloudError

    host = _host(_selector(name, os_, gpu), config)
    if not host.is_remote:
        say.fail(f"{host.name} is this machine — open a terminal", code=2)
    if is_windows(host):
        say.fail(f"{host.name} runs Windows, which has no ssh here",
                 fix=f"comfy-qat rdp {host.name}", code=2)

    gc = Gcloud()
    # Asked before the argv is handed to `execvp`, which has no way to report a
    # missing binary except by raising: nothing has started, so this is a 2.
    try:
        gc.require()
    except GcloudError as exc:
        _refused(exc)

    # And then whether there is anything to connect TO. Without this, the
    # everyday case — you forgot to `up` — was handed to gcloud, which answered
    # with a 36-line Python traceback and exit 255, and suggested
    # `--troubleshoot`, which fails the same way. `logs` asks this question on
    # the same box in the same second and says one sentence; the contrast was
    # inside one tool. One extra read on the way to a shell is the price, and it
    # is the same read `logs` already pays.
    from .lifecycle import RUNNING

    try:
        state = gc.instance_status(host.gce_instance, host.gce_zone, host.gce_project)
    except GcloudError as exc:
        _refused(exc)
    if not state:
        say.fail(
            f"could not tell whether {host.name} is running, so there is no "
            "saying whether it will take a shell.",
            fix=say.fix("ask Google again:", "comfy-qat list --live"),
            code=2,
        )
    if state != RUNNING:
        say.fail(
            f"{host.name} is not running, so there is nothing to open a shell "
            f"on. SSH needs the machine up, not just declared.",
            fix=f"comfy-qat up {host.name}   # start it, then ssh again",
            code=2,
        )

    argv = gc.ssh_argv(host.gce_instance, host.gce_zone, host.gce_project)
    # Replaced rather than spawned: an interactive shell wants this terminal, and
    # a subprocess wrapper would put a layer between the user and their own
    # Ctrl-C. Nothing after this line runs.
    say.step(f"opening a shell on {host.name}")
    os_module.execvp(argv[0], argv)


@app.command("rdp")
def rdp_cmd(
    name: Annotated[Optional[str], typer.Argument(help="Which machine: a name, or what you want — windows, l4.")] = None,
    config: ConfigOption = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", hidden=True, help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", hidden=True, help="Pick by card: l4, t4, a100.")] = None,
) -> None:
    """Reset the Windows password and forward RDP, then hand over the details.

    Two gcloud commands and a port number to remember. Google documents no way
    around the password reset, so this does the parts it can and prints the one
    thing only a person can do — typing the password into Remote Desktop.
    """
    from .gcloud import Gcloud, GcloudError

    host = _host(_selector(name, os_, gpu), config)
    if not host.is_remote or not is_windows(host):
        say.fail(f"{host.name} is not a Windows cloud box",
                 fix=f"comfy-qat ssh {host.name}", code=2)

    gc = Gcloud()
    try:
        credentials = gc.windows_password(host.gce_instance, host.gce_zone,
                                          host.gce_project)
    except GcloudError as exc:
        _refused(exc)

    # Read and checked before a single line is printed, because the next thing
    # after those lines is `execvp` and this process is gone. gcloud exiting 0
    # with nothing on stdout used to arrive here as an empty mapping, and
    # `.get(name, "")` laid a blank user over a blank password in exactly the
    # shape of a real pair, under a line saying the forward was starting — a
    # failure that looks like success, and whose only symptom is a Windows login
    # prompt that will not open, with nothing pointing back at us. A key renamed
    # on Google's side reads identically, so the shape is checked and not
    # assumed. Never print a credential pair that was not actually received.
    got = credentials if isinstance(credentials, dict) else {}
    user, password = got.get("username") or "", got.get("password") or ""
    if not user or not password:
        absent = " and no ".join(
            word for word, value in (("username", user), ("password", password))
            if not value)
        say.fail(f"gcloud reset the password on {host.gce_instance} but reported "
                 f"no {absent}, so there is nothing to sign in with",
                 fix=say.fix(
                     "run the reset yourself and read what comes back:",
                     f"gcloud compute reset-windows-password {host.gce_instance} "
                     f"--zone={host.gce_zone} --project={host.gce_project}"),
                 code=1)

    say.result(f"user     {user}")
    say.result(f"password {password}")
    say.result(f"address  localhost:{RDP_PORT}")
    say.step("forwarding RDP — Ctrl-C closes it")
    argv = gc.rdp_argv(host.gce_instance, host.gce_zone, host.gce_project, RDP_PORT)
    import os as os_module
    os_module.execvp(argv[0], argv)


@app.command("logs")
def logs_cmd(
    name: Annotated[Optional[str], typer.Argument(help="Which machine: a name, or what you want — windows, l4, windows/l4.")] = None,
    config: ConfigOption = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", hidden=True, help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", hidden=True, help="Pick by card: l4, t4, a100.")] = None,
    tail: Annotated[Optional[int], typer.Option(
        "--tail", help="Print this many lines and stop. Add --follow to keep reading.")] = None,
    follow: Annotated[Optional[bool], typer.Option(
        "--follow/--no-follow",
        help="Keep reading as it is written; Ctrl-C ends the reading only, "
             "never ComfyUI. The default unless --tail is given.")] = None,
) -> None:
    """Read the ComfyUI log on a box, since `go` no longer streams it here.

    With no arguments it follows, because "what is it doing now" is the question
    people have. `--tail N` is the other one — "what did it say" — and answers it
    and stops. Ctrl-C ends the reading and nothing else: ComfyUI keeps running,
    which is the whole point of it being detached.
    """
    from .gcloud import Gcloud
    from .lifecycle import read_logs, stop_paying

    host = _host(_selector(name, os_, gpu), config)
    try:
        _act(read_logs, Gcloud(), host, say.step,
             tail=200 if tail is None else tail,
             follow=(tail is None) if follow is None else follow)
    except KeyboardInterrupt:
        say.result(f"\nstopped reading. ComfyUI is still running on {host.name}, "
                   "and so is the machine.")
        say.result(f"  {stop_paying(host)}   # stop the box, stop paying")


def _unavailable(host: Host, hosts: list[Host], exc, kept: list[Host]) -> None:
    """A box that will not start is not the end of a test session. Say what is.

    Being told "no capacity in this zone" and nothing else is where testing
    stops: the next move is a four-step rebuild nobody has memorised. The
    tester's real question is "where can I work right now", so that is answered
    first, and the rebuild is offered second, for when it has to be that box.
    """
    from .lifecycle import alternatives

    # Every part of this is the one failure being reported, so it all goes where
    # errors go. The empty fix on each block is deliberate: the exception's own
    # fix is the rebuild, and it is held back to the end under "if it has to be
    # <box>" — after the answer to the question actually being asked, which is
    # where else can I work.
    say.error(exc, "")

    if kept:
        still = ", ".join(other.name for other in kept)
        say.error(f"{still} is untouched — you still have the machine you were on"
                  if len(kept) == 1 else
                  f"{still} are untouched — you still have the machines you were on",
                  "")

    options = alternatives(hosts, host)[:3]
    if options:
        commands = [f"comfy-qat switch {other.name}" for other in options]
        width = max(len(command) for command in commands)
        say.error("where you can test instead, easiest first:", "")
        for other, command in zip(options, commands):
            note = ""
            if other.gce_zone and other.gce_zone == host.gce_zone:
                note = ", same zone — it may hit the same shortage"
            say.detail(f"{command.ljust(width)}   # {describe(other)}{note}")
    else:
        say.error("no other machine is declared, so there is nowhere to switch to:",
                  "")
        say.detail("comfy-qat discover   # declare a box you already have")

    if exc.fix:
        say.error(f"if it has to be {host.name}:", "")
        for line in exc.fix.splitlines():
            say.detail(line.strip())


def _failed(host: Host, hosts: list[Host], exc, kept: list[Host] | None = None) -> None:
    """Report a machine that would not come up, in the most useful way there is."""
    from .lifecycle import STOCKOUT

    if getattr(exc, "kind", "") == STOCKOUT:
        _unavailable(host, hosts, exc, kept or [])
        return
    say.error(exc)


def _offer_move(host: Host, exc, config: Optional[Path]) -> bool:
    """Ask whether to rebuild the box in a zone that has capacity, and do it.

    `go` already detected the stockout, already read the zone Google named in the
    refusal, and already built the `move` command — and then handed it over to be
    typed. It had the answer and stopped one step short of using it.

    Asking rather than doing, because a move spends real money: it copies the
    whole boot disk, takes minutes, and can itself hit a stockout with the
    paid-for disk made and no machine. Never offered where it cannot be answered
    (a pipe, a script), and never from `switch`, which stops other boxes straight
    afterwards and would be left half-done.
    """
    from .gcloud import can_prompt

    zones = getattr(exc, "zones", ())
    if not zones or not can_prompt():
        return False

    target = zones[0]
    say.result("")
    if not typer.confirm(
        f"Rebuild {host.name} in {target}? It copies the boot disk, takes minutes, "
        f"and bills from the moment the new box exists"
    ):
        return False

    move_cmd(name=host.name, to=target, config=config, yes=True)
    return True


def _bring_up(gc, host: Host, hosts: list[Host], kept: list[Host] | None = None,
              *, offer_move: Optional[Path] | bool = False):
    """Get the machine up, with every failure turned into a next command.

    Returns None when the box is up but ComfyUI is absent — the one failure the
    steps after this one exist to fix.
    """
    from .lifecycle import COMFYUI_ABSENT, STOCKOUT, bring_up

    # No `except KeyboardInterrupt`, in either this or `up_cmd`: `bring_up`
    # registers the start with `inflight` around `start_instance` itself, and
    # `cli.main` reports it. Two frames asking the same question of the same
    # event is how a leftovers block came to be printed twice.
    try:
        return bring_up(gc, host, say.step, comfy_timeout=15)
    except _reportable() as exc:
        # Only "ComfyUI is not there yet" is worth continuing past. Anything else
        # (the box would not start, the tunnel failed) must be shown, not
        # swallowed — that once hid a failed start and then tried SSH against it.
        if exc.kind == COMFYUI_ABSENT:
            return None
        _failed(host, hosts, exc, kept)
        if exc.kind == STOCKOUT and offer_move is not False:
            config = offer_move if isinstance(offer_move, Path) else None
            if _offer_move(host, exc, config):
                say.result(f"\n{host.name} has moved — run the same command again:")
                say.result(f"  comfy-qat go {host.name}")
                raise typer.Exit(code=0)
        raise typer.Exit(code=1)


def _serve(gc, host: Host, ready, *, no_browser: bool = False,
           no_install: bool = False, follow: bool = False) -> None:
    """The rest of `go` once the machine is up: install if needed, then run it.

    Detached unless `--follow`. Both prove the same thing before returning —
    ComfyUI answering on the tunnel — and differ only in where its log goes and
    therefore in what Ctrl-C reaches.
    """
    import webbrowser

    from .gcloud import GcloudError
    from .lifecycle import (
        ensure_installed, serve, start_detached, stop_paying, wait_for_ssh,
        wrong_machine_fix,
    )
    from .stamp import mismatch

    browser = None if no_browser else (lambda url: webbrowser.open(url))

    if ready is not None and ready.stamp is not None:
        # The everyday route to a browser tab, and the one that never touches
        # `serve` or `start_detached`: a box that is ALREADY serving comes back
        # from `bring_up` with a stamp and is handed over right here. Guarding
        # only the launch would have covered the rare path — the machine that
        # had to be started — and left the ordinary one open, which is close to
        # no guard at all, because the person it protects is the one who
        # reconnects to a running box, which is everybody, most days.
        #
        # `bring_up` refuses to RETURN a machine it cannot identify; this refuses
        # to OPEN one. Two answers to the same question, deliberately, because
        # this is the line that actually points a browser at a host.
        problem = mismatch(host, ready.stamp)
        if problem is not None:
            say.fail(problem, fix=wrong_machine_fix(host), code=1, blank_line=False)
        say.result(f"\n{host.url}")
        say.result(ready.stamp.line())
        if browser:
            browser(host.url)
        say.result(f"\n  {stop_paying(host)}   # stop the box, stop paying")
        return

    if not host.is_remote:
        say.fail("ComfyUI is not running locally",
                 fix=say.fix("start it:",
                             "~/ComfyUI/venv/bin/python ~/ComfyUI/main.py "
                             "--port 8188 --listen 127.0.0.1"),
                 code=1, blank_line=False)

    if no_install:
        # The sibling refusal four lines up hands over a runnable command; this
        # one used to state the fact and stop. Being the DELIBERATE refusal is
        # the reason it needs a way out, not a reason to skip one: the tool is
        # doing exactly what it was told, so the question it leaves hanging is
        # not "what went wrong" but "then what". Both answers are one line, and
        # neither depends on which command got here — `go` and `switch` both do.
        say.fail("ComfyUI is not answering and --no-install was given",
                 fix=say.fix("drop --no-install to let it install, "
                             "or read the log to see why it is not answering:",
                             f"comfy-qat logs {host.name}"),
                 code=1, blank_line=False)

    try:
        wait_for_ssh(gc, host, say.step)
        ensure_installed(gc, host, say.step)
        if follow:
            # The last line of this tool's own voice before ComfyUI's output
            # takes the terminal, which makes it the last chance to say what
            # Ctrl-C will reach. `--help` says it too; nobody reads `--help`
            # from inside a running stream.
            say.step(f"streaming ComfyUI's log — Ctrl-C stops ComfyUI itself "
                     f"(`comfy-qat logs {host.name}` watches without that)")
        say.result("")
        code = (serve if follow else start_detached)(
            gc, host, say.step, open_browser=browser)
    except _reportable() + (GcloudError,) as exc:
        say.fail(exc, code=1)
    except KeyboardInterrupt:
        # Two sentences, not one, and only under `--follow`. Ctrl-C out of a
        # streamed log reaches ComfyUI and stops it, which is the opposite of
        # what the same key does in `comfy-qat logs` — and the line printed here
        # said only that the machine was still running, which is true and is not
        # the part that surprises anyone. The person reading it has just left a
        # log stream by reflex; what they need told is what went down with it,
        # and that the box did not.
        if follow:
            say.result(f"\nstopped, and ComfyUI stopped with it — that is what "
                       f"Ctrl-C does here. {host.name} is still running, and a "
                       f"stopped ComfyUI on a running box still bills.")
            say.result(f"  comfy-qat go {host.name}   # start ComfyUI again")
            say.result(f"  comfy-qat logs {host.name}   # watch it without "
                       "stopping it")
        else:
            say.result(f"\nstopped. {host.name} is still running.")
        say.result(f"  {stop_paying(host)}   # stop the box, stop paying")
        return

    if follow:
        say.result(f"\nComfyUI exited ({code}). {host.name} is still running.")
        say.result(f"  {stop_paying(host)}   # stop the box, stop paying")
        return

    # The URL is last on purpose. ComfyUI announces its own address — correct on
    # the box, wrong here — and whatever is said after it is what gets opened.
    say.result(f"\nComfyUI is running on {host.name} and this terminal is free.")
    say.result(f"  comfy-qat logs {host.name}   # follow its log, on the box")
    say.result(f"  {stop_paying(host)}   # stop the box, stop paying")
    say.result(f"\nopen {host.url}")


@app.command("switch")
def switch_cmd(
    # Optional here, required by `_selector`, exactly as everywhere else: the
    # positional and `--os`/`--gpu` are alternatives, so making the argument
    # mandatory made the flags on the same help panel unusable.
    name: Annotated[Optional[str], typer.Argument(
        help="Which machine: a name, or what you want — windows, l4, windows/l4.")] = None,
    config: ConfigOption = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", hidden=True, help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", hidden=True, help="Pick by card: l4, t4, a100.")] = None,
    keep_others: Annotated[bool, typer.Option(
        "--keep-others", help="Leave the other machines running. They keep billing.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the plan and stop.")] = False,
    no_browser: Annotated[bool, typer.Option(
        "--no-browser", help="Do not open a browser when ComfyUI answers.")] = False,
    no_install: Annotated[bool, typer.Option(
        "--no-install", help="Fail rather than installing ComfyUI if it is absent.")] = False,
) -> None:
    """Change machine: start the one you want, stop the one you were on.

    `go` with the step people forget on the front — a GPU box left running bills
    all night whether or not anything is tunnelled to it. Nothing here is new and
    nothing is remembered: there is still no current host, and `switch` names
    what it starts and what it stops before it does either.

    The target is brought up *first*. If it cannot start — a capacity shortage is
    routine on GPUs — you still have the machine you were on, and you are told
    where you can work instead.
    """
    from .gcloud import Gcloud, GcloudError
    from .lifecycle import put_away, running_elsewhere

    hosts, host = _lookup(_selector(name, os_, gpu), config)
    gc = Gcloud()

    try:
        others = [] if keep_others else running_elsewhere(gc, hosts, host)
    except GcloudError as exc:
        # Nothing has been started or stopped yet: this is only the survey of what
        # is running elsewhere, so it is a refusal (2), not a failed switch (1).
        _refused(exc)

    say.result("")
    say.result(f"  - go to {host.name} ({describe(host)}) on {host.url}")
    for other, why in others:
        say.result(f"  - then stop {other.name} ({describe(other)}) — {why}")
    if not others:
        say.result("  - leaving the other machines running (--keep-others)" if keep_others
                   else "  - nothing else is running, so nothing to stop")

    # The ceiling is checked BEFORE the dry run returns, because it decides the
    # ORDER, and the order is what a person runs `--dry-run` to see. It sat eleven
    # lines below this return, so the preview said "go to X, then stop Y" and the
    # real run did the opposite — stop Y, then start X. Someone typing `--dry-run`
    # to ask "will this kill the box I am on right now" was told no, and then it
    # did exactly that.
    first = _blocked_by_the_ceiling(gc, host, [other for other, _why in others])
    if first and others:
        say.result("")
        say.result(f"  your quota allows {say.count(first, 'GPU machine')} at a "
                   f"time, so the order is the other way round:")
        for other, _why in others:
            say.result(f"  - stop {other.name} FIRST")
        say.result(f"  - then go to {host.name} on {host.url}")

    if dry_run:
        say.result("\n--dry-run: nothing changed")
        return

    say.result("")

    # Normally the target comes up before anything is stopped, so a failure
    # leaves you on the machine you had. That is exactly backwards when the
    # ceiling is the reason you are switching: with GPUS_ALL_REGIONS at 1 and a
    # GPU box running, the target *cannot* start until the other one stops.
    # Watched that happen — the switch started the target, was refused with
    # "Quota 'NVIDIA_L4_GPUS' exceeded. Limit: 1.0", and reported it as a
    # failure. It was arithmetic, and it was knowable beforehand.
    others_stopped: list = []
    if first:
        say.step(f"your quota allows {say.count(first, 'GPU machine')} at a time, "
                 f"so {host.name} cannot start until the other one stops")
        for other, _why in others:
            _act(put_away, gc, other, say.step)
        # `others` is emptied on the next line and the record needs the names, so
        # what was stopped is kept rather than re-derived. `_failed` is handed the
        # same empty list and cannot say where to work instead; this is the half
        # of that gap an interrupt can close.
        others_stopped = list(others)
        others = []

    # `stopped_first` is not bookkeeping. On the ceiling path the old box is
    # already down by the time the target is started, so a failure here leaves
    # the user with the box they were on STOPPED and the new one possibly UP and
    # billing — while the command exits 1, which reads as "nothing happened".
    # `_failed` is handed an empty `kept` for the same reason and cannot say
    # where to work instead.
    # `bool(others_stopped)`, not `bool(first)`. Both mean "was anything stopped
    # before the target was started" and only one of them ASKS it: `first` is the
    # ceiling, and it is a proxy that holds solely because
    # `_blocked_by_the_ceiling` returns None when `others` is empty — a guard in
    # a DIFFERENT function, invisible from here, and one the switch tests already
    # stub out with a lambda that ignores `others`.
    #
    # When the proxy is wrong the registration below does `others_stopped[0][0]`
    # and raises IndexError. That is a crash on the ordinary path of `switch`,
    # not only in the interrupt report — the ExitStack is entered on every
    # ceiling switch. A crash inside the machinery for saying what a Ctrl-C left
    # running is the worst place in this tool to have one.
    #
    # Asking the list the message is built from makes the call site
    # self-sufficient, and makes the name true.
    stopped_first = bool(others_stopped)
    # The interrupt case needs the same clause as the failure case below, and
    # gets it from the record rather than from a second `except`: `_bring_up`
    # already registers the start, so this adds the half only `switch` knows —
    # that the machine you were on is down already. `billing=False` because it
    # is not a bill, it is a session; reporting it under the bill heading would
    # replace one false claim with another.
    #
    # Registered only on the ceiling path, because that is the only path where
    # anything has been stopped by now. On the ordinary path the target comes up
    # first and an interrupt leaves you exactly where you were.
    with ExitStack() as registered:
        if stopped_first:
            names = ", ".join(other.name for other, _why in others_stopped)
            registered.enter_context(inflight.may_leave(
                f"{names} already stopped to make room under the GPU ceiling",
                undo=[f"comfy-qat up {others_stopped[0][0].name}"],
                note=f"nothing is serving until one of them is up, and "
                     f"`comfy-qat switch {host.name}` retries the whole thing",
                heading="and this had already happened when you stopped it:",
            ))
        try:
            ready = _bring_up(gc, host, hosts, kept=[other for other, _why in others])
        except typer.Exit as exc:
            # Not on an interrupt. `inflight.Interrupted` IS a `typer.Exit` now —
            # that is what makes it survive `register()` — and the record has
            # already said this in its own heading, with the names. Printing here
            # too would be the same event twice, in two voices.
            if stopped_first and not isinstance(exc, inflight.Interrupted):
                say.error(
                    f"the machines you had are stopped and {host.name} did not come "
                    f"up, so you are on neither. Check what is running before "
                    f"retrying — {host.name} may have started and be billing",
                    say.fix("ask Google what is up:", "comfy-qat list --live"),
                )
            raise

    for other, _why in others:
        _act(put_away, gc, other, say.step)

    _serve(gc, host, ready, no_browser=no_browser, no_install=no_install)


def _probe_failed(gc, host: Host, exc) -> None:
    """A capacity probe that failed for any reason other than a stockout.

    Never returns — every path exits. The question it answers is the one
    `bring_up` and `put_away` both answer at their own mutating calls: a request
    that reached Google and whose ANSWER was lost is not a request that did not
    happen, so read the state back before saying anything about it.

    The exit code carries the finding rather than a habit. This tool's rule is
    that 2 means nothing was changed and 1 means the work started and failed, so
    a machine proved TERMINATED keeps 2 and everything else takes 1 — including
    "could not tell", because 2 there is the assertion nobody can make.

    The raw gcloud stop leads, and `comfy-qat down` is not offered at all. The
    box IS in the host list here — unlike `create`'s case, where the reason is
    that it is not — but this tool has just failed to read or act on it, so its
    own command is the one thing that has already been shown not to work.
    `create._stop_the_box` reached the same place from the other direction: a
    fix line that cannot run is worse than none.
    """
    from .gcloud import GcloudError
    from .lifecycle import TERMINATED, _raw_stop, is_auth_failure, readable_state

    # A credential failure is a REFUSAL: the request never got far enough to
    # start anything, so "nothing was changed" is true and 2 is right. Checked
    # first and separately because the re-read below would fail for the very
    # same reason — an expired session cannot answer `instances describe`
    # either — and this would then report "could not be read" and exit 1 about a
    # box that is certainly off.
    #
    # `bring_up` and `wait_for_ssh` both take this branch ahead of everything
    # else, for the same reason stated the same way: a credential will not come
    # back on its own, so waiting or re-asking spends time on what cannot work.
    if is_auth_failure(exc):
        _refused(exc)

    try:
        after = gc.instance_status(host.gce_instance, host.gce_zone,
                                   host.gce_project)
    except GcloudError:
        after = None

    # The start did not land. Nothing is billing, so "nothing was changed" is
    # true and 2 is the honest code — this is the only branch where it is.
    if after == TERMINATED:
        _refused(exc)

    # An empty status is a third answer and not a state: the read succeeded and
    # said nothing about the machine. `bring_up` makes the same distinction at
    # the same call, and taking it for a state is how a confident sentence gets
    # printed on no evidence.
    if after not in (None, ""):
        say.fail(
            f"asking Google where there is capacity did not report back ({exc}), "
            f"and {host.name} is {readable_state(after).lower()} — the probe "
            f"started it, and it is billing.",
            fix=say.fix("stop it with gcloud directly:", _raw_stop(host)),
            code=1, blank_line=False)

    say.fail(
        # Not "could not be read": that four-word run is one config.py also
        # builds, and test_docs then classifies this as a ConfigError and fails
        # it for not being one. Active voice, same meaning, no collision.
        f"asking Google where there is capacity failed ({exc}), and reading "
        f"{host.name}'s state afterwards failed too. The probe is a start, so "
        f"it may have landed — it may be running and billing.",
        fix=say.fix("stop it with gcloud directly:", _raw_stop(host),
                    "or look at what is running:", "comfy-qat list --live"),
        code=1, blank_line=False)


def _blocked_by_the_ceiling(gc, host: Host, others: list[Host]) -> int | None:
    """Would the project-wide GPU ceiling refuse this machine while those run?

    Returns the ceiling when it is the thing in the way, and None otherwise —
    including whenever the answer cannot be established. Not knowing must never
    reorder a switch: stopping first is the destructive order, and it is only
    correct when the arithmetic is certain.
    """
    if not others or not host.is_remote or not host.gpu:
        return None
    # WHY THE READ CANNOT BE SKIPPED HERE, checked 2026-09-08 and written down
    # because it looks exactly like the case `relocate._within_the_allowance`
    # already optimises — "nothing can be over the ceiling while nothing holds
    # any of it", pay the ~58-second `gpu_quotas` only when the answer could be
    # no. Tried, and reverted:
    #
    # `relocate` counts cards in a live instance list, which really can be zero.
    # `others` here cannot. It comes from `running_elsewhere`, which returns
    # only hosts whose `kind` is not "local" — and `Kind` is `local | gce`, so
    # every one of them is remote — while `config.py` REQUIRES a truthy `gpu` on
    # every gce host (`_REQUIRED_FOR_GCE`). So `running` below is always exactly
    # `len(others)`, and `others` is non-empty by the line above. The free half
    # is already known to be non-zero before it is computed, and hoisting it
    # buys nothing.
    #
    # The remaining question is whether `len(others) >= ceiling`, and nothing
    # answers that without the read.
    try:
        from .quota import global_allowance

        ceiling = global_allowance(gc.gpu_quotas(host.gce_project or ""))
    except Exception:
        return None
    if ceiling is None or ceiling < 0:      # -1 is Google's "unlimited"
        return None
    running = sum(1 for other in others if other.is_remote and other.gpu)
    return ceiling if running >= ceiling else None


def _zone_with_capacity(gc, host: Host, *, dry_run: bool) -> str | None:
    """Which zone Google says has capacity — found the only way there is.

    Nothing answers "where is there an L4 free". The only way to find out is to
    try to start the machine and read the zone out of the refusal, which means
    that when it is *not* refused the box is up and billing.

    That is why this cannot be part of a dry run, and why the guard lives here
    rather than at the call site: `--dry-run` used to be consulted long after
    this had already started a GPU instance, so the one command that promises to
    change nothing was the one that could quietly cost the most. Anything that
    replaces this function inherits the guard with it.

    Returns the zone to move to, or None when the machine started — in which case
    there was never anything to move, and it has been reported.
    """
    from .gcloud import GcloudError
    from .lifecycle import is_capacity_failure, stop_paying, suggested_zones

    if dry_run:
        say.fail(
            f"--dry-run cannot find a zone with capacity: the only way to ask is to "
            f"start {host.gce_instance}, and a box that starts is billing",
            fix=say.fix(
                "name the zone yourself and the plan prints without touching anything:",
                f"comfy-qat move {host.name} --to us-central1-b --dry-run"),
            code=2, blank_line=False)

    say.step("asking Google where there is capacity")
    try:
        # This start is as billable as any other and interrupts the same way,
        # and it was the one start in the tool that nothing registered. Measured
        # with a real SIGINT against a fake gcloud: `up comfy-win` exits 130 and
        # names the box and the way to stop it, while `move comfy-win` exited
        # 130 with its output ending at "asking Google where there is capacity"
        # and nothing after it — no report, no "may be billing", no stop
        # command, while a GPU box may have just started. `move <host>` with no
        # `--to` is the ordinary way to use the command, so that was the
        # ordinary path.
        #
        # The exit code was never the missing half: `cli.main` catches the bare
        # KeyboardInterrupt, so all three commands already exited 130 and none
        # said nothing at all — Typer converts the interrupt to Exit(130)
        # (typer/core.py:203-204), so it exited quietly. What was missing
        # is the sentence, not the code.
        #
        # Registered in the ORIGINAL zone, which is where a start that succeeds
        # leaves the box — the whole point of this probe is that the move has
        # not happened yet.
        with inflight.may_leave(
            f"{host.name} ({host.gce_instance} in {host.gce_zone}), started to "
            f"ask where there is capacity",
            undo=[
                f"comfy-qat down {host.name}",
                "or check first, if you would rather look:",
                "comfy-qat list --live",
            ],
        ):
            gc.start_instance(host.gce_instance, host.gce_zone, host.gce_project)
    except GcloudError as exc:
        if not is_capacity_failure(exc.raw):
            # THE MIRROR OF THE STOP FAILURE, and the same defect on the other
            # side of the same call. A capacity refusal means nothing started,
            # which is why that branch below says nothing about money and is
            # right to. This branch is everything else — the timeout, the lost
            # reply — and it called `_refused`, which exits 2.
            #
            # 2 is not a neutral code here. `_refused`'s own docstring defines it
            # as "nothing was changed", with 1 reserved for "the work started and
            # failed". So the command POSITIVELY ASSERTED that nothing happened,
            # at the one moment nobody can know that — twenty lines below, this
            # function's own comment says "the probe IS a start… a try that is
            # not refused leaves a GPU box running".
            #
            # `inflight` does not cover it either: its `except Exception` drops
            # the entry and re-raises, on the reasoning that "the command has its
            # own words for a failure it can name". Here the command had none.
            _probe_failed(gc, host, exc)
        zones = suggested_zones(exc.raw)
        if not zones:
            say.fail("Google did not name a zone with capacity",
                     fix="pick one with --to, e.g. --to us-central1-b",
                     code=2, blank_line=False)
        say.detail(f"{host.gce_zone} has none free; {zones[0]} does")
        return zones[0]

    # Reaching this line means the probe succeeded, and the probe IS a start:
    # nothing answers "where is there an L4 free", so the only way to ask is to
    # try, and a try that is not refused leaves a GPU box running. "No move
    # needed" was true and was also the whole message — the one billable start in
    # this tool that named no way to stop paying, where `create`, `move`'s own
    # finish and every lifecycle failure through `_with_the_bill` all do.
    say.result(f"{host.name} started in {host.gce_zone} and is billing — "
               f"no move needed.")
    say.result(f"  comfy-qat go {host.name}     # tunnel to it and serve")
    say.result(f"  {stop_paying(host)}   # stop the box, stop paying")
    return None


@app.command("move")
def move_cmd(
    name: Annotated[Optional[str], typer.Argument(help="Which machine to move: a name, or what you want — windows, l4.")] = None,
    to: Annotated[Optional[str], typer.Option(
        "--to", help="Zone to move it to. Default: whichever one Google says has capacity.")] = None,
    config: ConfigOption = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", hidden=True, help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", hidden=True, help="Pick by card: l4, t4, a100.")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask before making changes.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the plan and stop.")] = False,
    clean: Annotated[bool, typer.Option(
        "--clean", help="Delete what an earlier, half-finished move left behind, then move.")] = False,
) -> None:
    """Move a box to a zone that has capacity, keeping its ComfyUI install.

    A GPU stockout cannot be fixed where you are: the zone has none of that card
    and retrying will not change it. Done by hand this is a snapshot, a disk, an
    instance and a config edit — four chances to get it wrong.

    This one writes: that config edit is the fourth step, so your host list — the
    file `--config` names, and ~/.config/comfy-qa-tools/hosts.toml when it is
    left off — is read and then rewritten, with a backup left beside it.
    `--dry-run` shows the plan and writes nothing.
    """
    from .discover import Discovered, next_ports, to_toml
    from .gcloud import Gcloud, GcloudError
    from .gcloud import can_prompt
    from .hostfile import apply, rename_and_add
    from .lifecycle import stop_paying
    from .relocate import (
        MoveError, blocked, delete_instance_command, leftovers, prepare,
        remove_leftovers, run_move, split_leftovers, would_not_load,
    )

    hosts, host = _lookup(_selector(name, os_, gpu), config)
    if not host.is_remote:
        say.fail(f"{host.name} is local — there is nowhere to move it to", code=2,
                 blank_line=False)

    gc = Gcloud()
    target = to

    if target is None:
        # Everything a dry run must not do lives inside this call, guard included.
        target = _zone_with_capacity(gc, host, dry_run=dry_run)
        if target is None:
            return

    try:
        instance = gc.describe_instance(host.gce_instance, host.gce_zone, host.gce_project)
        plan, found = prepare(gc, host, instance, target)
    except GcloudError as exc:
        _refused(exc)

    # Whatever an earlier run left is billing right now, whether or not this one
    # goes ahead — and an unattached disk looks like nothing at all in a console.
    # Only this move's leftovers appear here: a stray snapshot from a different
    # box, with its own delete command, used to land three lines above the
    # confirm, and it is not something this command touches. It is reported at
    # the end instead.
    mine = leftovers(plan, found, unrelated=False)
    if mine:
        # stdout carries the answer, stderr carries the story — and this is the
        # story of an EARLIER run, not the answer to this one. It went to stdout,
        # and on the `--dry-run` path this command returns before anything
        # reaches stderr at all: `2>/dev/null` lost nothing and `1>/dev/null`
        # lost the whole block, in the command that prints more
        # `gcloud ... --quiet` delete commands than any other. Someone piping a
        # dry run into a file to read later captured three destructive commands
        # and no plan.
        #
        # `warn` rather than `step`, because "not fatal, and you need to know" is
        # exactly what it is: whatever this run does, that disk is billing.
        say.warn("an earlier run left this behind, and it is billing:")
        for line in mine:
            say.detail(line)

        # Reusing them is the default and usually right — that is what makes a
        # failed move cheap to retry. Deleting them starts the copy from scratch.
        #
        # `dry_run` FIRST, and it is the whole point of this line. `--clean`
        # short-circuits the confirm, so `move --clean --dry-run` reached
        # `remove_leftovers` and ran `disks delete --quiet` and `snapshots delete
        # --quiet` for real — then, twenty-five lines below, printed "--dry-run:
        # nothing changed". The one flag whose entire contract is "show me what
        # would happen" performed the only irreversible deletion in this command
        # and then denied it.
        #
        # The comment above `_zone_with_capacity` says "everything a dry run must
        # not do lives inside this call, guard included". That was true of the
        # thing it was written about, and this sat outside it. A guard scoped to
        # one call is a guard for one call.
        # `if dry_run`, full stop. The first version of this guard re-derived
        # "is there anything to delete" from `plan` and two of `found`'s five
        # fields, and got it wrong in BOTH directions.
        #
        # It missed `found.spare_snapshots`, which `remove_leftovers` deletes and
        # `leftovers` lists — so `mine` could be non-empty from spares alone, the
        # guard read False, and a DRY RUN asked "Delete these and start the move
        # fresh?" and destroyed a real snapshot on "y". That route needed no
        # `--clean` at all, so a967d37 closed one of two.
        #
        # And its report named `plan.new_disk` unconditionally — always a
        # non-empty string — so it announced a disk that does not exist while
        # omitting the snapshot that would actually go.
        #
        # `mine` is computed on the line above and is exactly "is there anything
        # here to act on". Both holes close by asking it instead of rebuilding it.
        if dry_run:
            # The list is four lines above and unchanged; printing it a second
            # time doubled the delete commands on screen and said nothing new.
            # What the flag adds is what happens to them, so that is all this
            # says.
            say.detail("--dry-run: these would be deleted first, and are not")
        elif clean or (not yes and can_prompt()
                       and typer.confirm("\nDelete these and start the move fresh?")):
            try:
                removed = remove_leftovers(gc, plan, found, say.step)
            except GcloudError as exc:
                say.fail(f"could not clean up: {exc}", code=1)
            say.result(f"\nremoved {len(removed)}.")
            # The plan was built from resources that no longer exist. Carrying on
            # with it would REUSE a deleted disk, or skip a snapshot it now needs,
            # and neither fails loudly — the move just builds from nothing.
            try:
                plan, found = prepare(gc, host, instance, target)
            except GcloudError as exc:
                _refused(exc)

    # Both refuse before anything is created. `would_not_load` is the one that
    # can see the host list, so it is asked here rather than inside `run_move`,
    # which is handed a plan and no file.
    problem = blocked(plan, found) or would_not_load(hosts, plan)
    if problem is not None:
        # 2, not 1. Both of these refuse BEFORE anything is created — the line
        # above says so, and both their docstrings say so — and this tool's rule
        # is that 2 means nothing was changed and 1 means the work started and
        # failed. It exited 1.
        #
        # The counter-argument, which does not survive: one `blocked` variant
        # carries `left=(...)` naming something that IS billing, so 1 could be
        # read as "there is a mess out there". But that mess is from an earlier
        # run, and the rule is about whether THIS invocation changed anything.
        say.fail(problem, code=2)

    say.result("")
    for line in plan.steps(found):
        say.result(f"  - {line}")
    # The plan goes to stdout above, because the plan IS the answer. A note is
    # not part of it, and the argument that it might be does not survive reading
    # one: `judge_disk` builds them with `output.fix`, the tool's own vocabulary
    # for "what to do about it", and the only note that exists says the moved box
    # gets a slower boot disk than the one it replaces and hands over a `gcloud
    # compute disks delete ... --quiet` to avoid that. A caveat and a remedy, not
    # a step this move takes — and the delete command is the same hazard that put
    # the leftovers block on the wrong stream, one line further down.
    for note in found.notes:
        say.warn(note)

    if dry_run:
        say.result("\n--dry-run: nothing changed")
        return
    if not yes and not typer.confirm(f"\nMove {host.name} to {target}?"):
        say.result("nothing changed")
        return

    path = config or DEFAULT_CONFIG_PATH
    ports: list[int] = []

    def register(done) -> None:
        """Rewrite the host list so the box keeps its name, its port and its URL.

        This used to append a second entry under a new name and a new port, which
        is why a successful move left `comfy-qat go <box>` still failing: the
        original entry was untouched and still named the zone with no capacity.

        The box being moved away from is not dropped — it exists in GCE and bills
        until somebody deletes it, so it stays reachable under a name that says
        where it is. Both halves land in one atomic write or neither does.
        """
        hosts = load(path)
        retired = done.retired_name
        freed = next_ports(hosts, 1)[0]
        moved = Discovered(
            name=host.name, os=host.os or "unknown", gpu=host.gpu or "",
            gce_instance=done.new_instance, gce_zone=done.to_zone,
            gce_project=host.gce_project, running=True,
        )
        text = rename_and_add(
            path.read_text(encoding="utf-8"),
            name=host.name, renamed=retired, renamed_port=freed,
            added=to_toml(moved, host.port),
        )
        apply(path, text,
              expect={h.name for h in hosts} - {host.name} | {retired, host.name})
        ports.append(host.port)

    say.result("")
    try:
        outcome = run_move(gc, plan, found, say.step, register=register)
    except MoveError as exc:
        say.error(exc, "")
        for item in exc.left:
            say.detail(f"this run left {item}, and it is billing")
        say.detail(f"{host.gce_instance} is untouched in {host.gce_zone}")
        say.write_fix(exc.fix or "")
        raise typer.Exit(code=1)

    for warning in outcome.warnings:
        say.warn(warning)

    port = ports[0] if ports else host.port
    # Creating an instance starts it. Saying "now run go" read as "now start it",
    # so a moved box billed silently from the moment the move finished.
    say.result(f"\n{host.name} is now in {target}, running and billing from now. "
               f"Same name, same port {port}.")
    say.result(f"  comfy-qat go {host.name}     # tunnel to it and serve")
    say.result(f"  {stop_paying(host)}   # stop the box, stop paying")

    was = "running and still billing" if plan.source_running else "stopped"
    say.result(f"\nthe old box is still in {host.gce_zone} ({was}), now called "
               f"{plan.retired_name}, with its disk {plan.source_disk}.")
    if plan.source_running:
        say.result(f"  comfy-qat down {plan.retired_name}")
    say.result(f"  {delete_instance_command(plan)}")

    # Reported here rather than before the confirm: it belongs to a box that no
    # longer exists, this command does not touch it, and it is not part of the
    # decision the user just made.
    # Both lists from ONE `found`, at the moment of printing. `mine` above was
    # computed before `--clean` replaced `found`, and reusing its length here is
    # what hid billing snapshots.
    _, stray = split_leftovers(plan, found)
    if stray:
        # Same stream rule as the block before the confirm, and for the same
        # reason: the answer to `move` is where the box is now, and somebody
        # else's billing snapshot is not that. It carries delete commands too.
        say.warn("also on the project, unrelated to this move and billing:")
        for line in stray:
            say.detail(line)


def _undeclared_and_running(gc, hosts: list[Host]) -> list[tuple[str, str]] | None:
    """Running instances on the project that no host entry names.

    `down --all` iterates the host list, so a box nobody declared is not merely
    unstopped — it is unreachable by every sentence the command prints. That was
    survivable while the closing line read "all N stopped". It is not survivable
    now that it reads "nothing is now", which is a promise about the project
    rather than about the file.

    Read-only, and a failure must not turn a successful `down` into an error —
    this runs after the work is done. But it returns None rather than an empty
    list when it could not look, because those are different facts and printing
    the same sentence for both is the defect this function was written to fix,
    wearing the fix's clothes: "nothing is running on the project either" is a
    claim, and an unread project does not support it.
    """
    from .gcloud import GcloudError

    project = next((h.gce_project for h in hosts if h.gce_project), None)
    if not project:
        # No declared cloud host to take it from — which is the case this
        # matters most in. Ask gcloud what project is configured.
        try:
            project = gc.current_project()
        except (GcloudError, AttributeError):
            return None
    if not project:
        return None
    declared = {h.gce_instance for h in hosts if h.gce_instance}
    try:
        instances = gc.list_instances(project)
    except (GcloudError, AttributeError):
        return None
    return [
        (i.get("name", ""), _tail_zone(i.get("zone", "")))
        for i in instances or []
        if i.get("name") not in declared and i.get("status") != "TERMINATED"
    ]


def _tail_zone(url: str) -> str:
    return (url or "").rstrip("/").rsplit("/", 1)[-1]


def _probe_fix(host: Host) -> str | None:
    """Advice for a machine that did not answer, told which machine it was.

    `fetch` is handed a URL and nothing else, so its advice — "start ComfyUI on
    that machine, or check the port in your host list" — is the local answer, and
    for a cloud box it names neither of the two things that are actually wrong.
    A `gce` host is reached through a tunnel, so nothing on 8190 usually means
    there is no tunnel, or the instance is stopped, and the port in the host list
    is fine.

    Only claimed when there is really no tunnel. With one open the port is being
    forwarded and the answer came from the far end, so `fetch` knows more about
    what went wrong than this does. Returns None to leave its advice alone.
    """
    if not host.is_remote:
        return None

    from .tunnel import status as tunnel_status

    if tunnel_status(host.name).running:
        return None

    return say.fix(
        f"no tunnel to {host.name} is open, so nothing here answers {host.url} — "
        f"and {host.gce_instance} may simply be stopped.",
        f"comfy-qat open {host.name}   # tunnel to a box that is already running",
        f"comfy-qat go {host.name}     # start it and tunnel, in one step",
        "comfy-qat list --live        # which of the two it is",
    )


@app.command("stamp")
def stamp_cmd(
    name: Annotated[Optional[str], typer.Argument(help="Which machine: a name, or what you want — windows, l4, windows/l4.")] = None,
    config: ConfigOption = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", hidden=True, help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", hidden=True, help="Pick by card: l4, t4, a100.")] = None,
    as_json: Annotated[bool, typer.Option(
        "--json", help="Machine-readable, for pasting into a report or a test.")] = False,
) -> None:
    """Ask a machine what it is, and print the line you paste into a report.

    This is the record nothing else keeps. A recorded test does not carry it, and
    a hand-written bug report usually does not either — which is how "cannot
    reproduce" happens between two machines that were never the same.
    """
    host = _host(_selector(name, os_, gpu), config)

    try:
        stamp = fetch(host.url, host=host.name)
    except ProbeError as exc:
        say.fail(exc, _probe_fix(host) or exc.fix, code=1, blank_line=False)

    # Refused, not warned. This line exists to be copied — it is pasted into a
    # bug report as the proof of which machine produced a result — and a warning
    # on stderr does not survive being copied. Printing the line at all is what
    # creates the artefact, so when the machine that answered contradicts the one
    # declared, no line is printed and the contradiction is what you get instead.
    problem = mismatch(host, stamp)
    if problem is not None:
        say.error(problem, "", blank_line=False)
        say.fail("no evidence line was printed, because this one would have named "
                 "the wrong machine",
                 fix="check the port in your host list and which tunnel is open, "
                     "then stamp it again",
                 code=1, blank_line=False)

    if as_json:
        say.result(json.dumps(stamp.as_dict(), indent=2))
    else:
        say.result(stamp.line())


@app.callback(invoke_without_command=True)
def default(
    ctx: typer.Context,
    config: ConfigOption = None,
) -> None:
    """With no subcommand, listing is the safe thing to do.

    `--config` is accepted here as well as at the root and on the subcommands.
    It is one option in three positions, not three options: `remember_config`
    keeps whichever was typed last, so `host --config x` is neither a usage
    error nor a value that quietly goes nowhere.

    And this is where the `host` noun says it is going. `cli.py` registers this
    whole sub-app `hidden=True` and calls that a deprecation window rather than a
    second permanent spelling — but a window nobody is told about never closes,
    and hiding it from `--help` is what removed the last way anyone had of
    finding out. Whoever still types `host go` is the only person who can be
    told, and this is the one line every `host <verb>` passes through.
    """
    if ctx.invoked_subcommand is not None:
        say.warn("`host` is on its way out and still works. The verb stands on "
                 f"its own now: comfy-qat {ctx.invoked_subcommand}")
    if ctx.invoked_subcommand is None:
        ctx.invoke(list_cmd, config=config)
