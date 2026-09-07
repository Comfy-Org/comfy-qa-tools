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
from . import say
from .lifecycle import LifecycleError, is_windows
from .provision import RDP_PORT
from .stamp import ProbeError, fetch, mismatch

app = typer.Typer(
    help="Operate the machines you test on — local installs and cloud GPU boxes.",
    no_args_is_help=False,
)

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

    gc = None
    states: dict[str, str] = {}
    for host in hosts:
        parts = []
        if host.is_remote:
            if live:
                from .gcloud import Gcloud, GcloudError

                gc = gc or Gcloud()
                try:
                    state = gc.instance_status(
                        host.gce_instance, host.gce_zone, host.gce_project)
                except GcloudError:
                    state = "unknown"
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
    config: Annotated[Optional[Path], typer.Option(
        "--config", help="Host list to read. Default: ~/.config/comfy-qa-tools/hosts.toml.")] = None,
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
    config: Annotated[Optional[Path], typer.Option(
        "--config", help="Where to write the starter host list.")] = None,
    force: Annotated[bool, typer.Option(
        "--force", help="Overwrite an existing host list.")] = False,
) -> None:
    """Write a starter host list you can edit."""
    path = config or DEFAULT_CONFIG_PATH
    if path.exists() and not force:
        say.fail(f"{path} already exists", fix="--force overwrites it",
                 code=2, blank_line=False)
    # A folder you cannot write to, and `--force` aimed at a directory, both
    # arrive here as an OSError. This is the command someone runs first, so a
    # traceback is the first thing the tool would ever show them.
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(STARTER, encoding="utf-8")
    except OSError as exc:
        say.fail(f"could not write a host list to {path}: {exc}",
                 fix="give --config a path you can write to — the file itself, "
                     "not the folder it goes in",
                 code=2, blank_line=False)
    say.result(f"wrote {path}")
    say.result("add your cloud boxes to it, then: comfy-qat list")


@app.command("discover")
def discover_cmd(
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    dry_run: Annotated[bool, typer.Option(
        "--dry-run", help="Show what would be added without writing anything.")] = False,
) -> None:
    """Find cloud boxes on your project and add the ones you do not have yet.

    Google already knows each box's zone, card and operating system, so nothing
    here needs typing by hand. Existing entries are never touched.
    """
    from .gcloud import Gcloud, GcloudError
    from .discover import new_hosts, parse as parse_instance, to_toml

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
    except ConfigError:
        existing = []

    additions = new_hosts(found, existing)
    if not additions:
        say.result(f"{say.count(len(found), 'cloud box', 'cloud boxes')}, "
                   f"all already in {path}")
        return

    for box, port in additions:
        state = "running" if box.running else "stopped"
        say.result(f"{box.name}  {box.os}  {box.gpu or 'no GPU'}  {state}  port {port}")

    if dry_run:
        say.result("\n--dry-run: nothing written")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(STARTER, encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        for box, port in additions:
            handle.write(to_toml(box, port))
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
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
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

    gc = Gcloud()
    try:
        project = gc.current_project()
        if not project:
            say.fail("no project set", fix="comfy-qat setup", code=2,
                     blank_line=False)
        instances = gc.list_instances(project)
        # The first place `create` goes quiet, and long enough that silence reads
        # as a hang. Timed rather than announced once and then nothing.
        reading = say.slow("reading quota", expect="about a minute").start()
        try:
            quotas = gc.gpu_quotas(project)
        finally:
            reading.done()
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
    except KeyboardInterrupt:
        # Ctrl-C does NOT cancel the gcloud child. `Gcloud.run` catches the
        # interrupt and calls `process.wait()` a SECOND time, so the create
        # completes and only then unwinds — measured: interrupt at 0.4s, the
        # pattern returns at 2.02s, and the resource exists.
        #
        # KeyboardInterrupt is a BaseException, so it walks past every handler in
        # this file and Click prints "Aborted!". A GPU box is then running,
        # billing, and in no host list — invisible to `list`, reachable only by
        # `down --all`, `discover` or the console, none of which anyone runs after
        # a screen that said the command was aborted.
        #
        # "may" is the honest word: at the moment of the interrupt this tool does
        # not know how far the create got. Both outcomes are named because the
        # cost of checking is one read and the cost of not checking is a GPU.
        say.error(
            f"interrupted — {blueprint.name} may already exist and be billing",
            say.fix(
                "check, and stop it if it is there:",
                f"gcloud compute instances list --project={project}",
                f"gcloud compute instances stop {blueprint.name} "
                f"--zone={ordering.zones[0]} --project={project}",
                "it is not in your host list, so `comfy-qat down` cannot reach "
                "it — `comfy-qat discover` adopts it if you want to keep it",
            ),
        )
        raise

    # Re-read rather than reusing the list from before the create: this command
    # takes minutes, and a `host discover` in another terminal in the meantime
    # would have taken the port this was about to hand out. Two hosts on one port
    # is the failure you cannot diagnose from the outside.
    try:
        port = next_ports(load(path), 1)[0]
    except ConfigError:
        port = next_ports(hosts, 1)[0]

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
    """Run a lifecycle step, turning its failures into messages, never tracebacks."""
    try:
        return action(*args, **kwargs)
    except _reportable() as exc:
        say.fail(exc, code=1)


@app.command("up")
def up_cmd(
    name: Annotated[Optional[str], typer.Argument(help="Which machine: a name, or what you want — windows, l4, windows/l4.")] = None,
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", help="Pick by card: l4, t4, a100.")] = None,
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
    except KeyboardInterrupt:
        _interrupted_while_starting(host)
        raise
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
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", help="Pick by card: l4, t4, a100.")] = None,
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
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", help="Pick by card: l4, t4, a100.")] = None,
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
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
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
    from .lifecycle import put_away

    # Both paths, not just --all: the single-host form is the one someone types
    # out of habit.
    if keep_running:
        say.warn("`--keep-running` is now `comfy-qat disconnect <name>`. "
                 "The flag still works.")

    if everything:
        if name:
            say.fail("--all stops every machine, so it takes no name", code=2,
                     blank_line=False)
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
        for host in hosts:
            say.step(host.name)
            try:
                found = put_away(gc, host, say.detail, keep_running=keep_running)
                {"unknown": unknown, "billing": billing,
                 "caught": stopped}.get(found, []).append(host)
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

    if not name:
        say.fail("say which machine, or --all for every one of them", code=2,
                 blank_line=False)

    host = _host(name, config)
    _act(put_away, Gcloud(), host, say.step, keep_running=keep_running)


@app.command("go")
def go_cmd(
    name: Annotated[Optional[str], typer.Argument(help="Which machine: a name, or what you want — windows, l4, windows/l4.")] = None,
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", help="Pick by card: l4, t4, a100.")] = None,
    no_browser: Annotated[bool, typer.Option(
        "--no-browser", help="Do not open a browser when ComfyUI answers.")] = False,
    no_install: Annotated[bool, typer.Option(
        "--no-install", help="Fail rather than installing ComfyUI if it is absent.")] = False,
    follow: Annotated[bool, typer.Option(
        "--follow", help="Stream ComfyUI's log here. Ctrl-C then stops ComfyUI.")] = False,
    new_window: Annotated[bool, typer.Option(
        "--new-window", help="Run this in a new macOS Terminal window instead.")] = False,
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
        rest = ["host", "go", host.name, "--follow"]
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
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", help="Pick by card: l4, t4, a100.")] = None,
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
    argv = gc.ssh_argv(host.gce_instance, host.gce_zone, host.gce_project)
    # Replaced rather than spawned: an interactive shell wants this terminal, and
    # a subprocess wrapper would put a layer between the user and their own
    # Ctrl-C. Nothing after this line runs.
    say.step(f"opening a shell on {host.name}")
    os_module.execvp(argv[0], argv)


@app.command("rdp")
def rdp_cmd(
    name: Annotated[Optional[str], typer.Argument(help="Which machine: a name, or what you want — windows, l4.")] = None,
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", help="Pick by card: l4, t4, a100.")] = None,
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
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", help="Pick by card: l4, t4, a100.")] = None,
    tail: Annotated[Optional[int], typer.Option(
        "--tail", help="Print this many lines and stop. Add --follow to keep reading.")] = None,
    follow: Annotated[Optional[bool], typer.Option(
        "--follow/--no-follow",
        help="Keep reading as it is written. The default unless --tail is given.")] = None,
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

    try:
        return bring_up(gc, host, say.step, comfy_timeout=15)
    except KeyboardInterrupt:
        _interrupted_while_starting(host)
        raise
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
    )

    browser = None if no_browser else (lambda url: webbrowser.open(url))

    if ready is not None and ready.stamp is not None:
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
        say.fail("ComfyUI is not answering and --no-install was given", code=1,
                 blank_line=False)

    try:
        wait_for_ssh(gc, host, say.step)
        ensure_installed(gc, host, say.step)
        say.result("")
        code = (serve if follow else start_detached)(
            gc, host, say.step, open_browser=browser)
    except _reportable() + (GcloudError,) as exc:
        say.fail(exc, code=1)
    except KeyboardInterrupt:
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
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", help="Pick by card: l4, t4, a100.")] = None,
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
    if first:
        say.step(f"your quota allows {say.count(first, 'GPU machine')} at a time, "
                 f"so {host.name} cannot start until the other one stops")
        for other, _why in others:
            _act(put_away, gc, other, say.step)
        others = []

    # `stopped_first` is not bookkeeping. On the ceiling path the old box is
    # already down by the time the target is started, so a failure here leaves
    # the user with the box they were on STOPPED and the new one possibly UP and
    # billing — while the command exits 1, which reads as "nothing happened".
    # `_failed` is handed an empty `kept` for the same reason and cannot say
    # where to work instead.
    stopped_first = bool(first)
    try:
        ready = _bring_up(gc, host, hosts, kept=[other for other, _why in others])
    except typer.Exit:
        if stopped_first:
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


def _blocked_by_the_ceiling(gc, host: Host, others: list[Host]) -> int | None:
    """Would the project-wide GPU ceiling refuse this machine while those run?

    Returns the ceiling when it is the thing in the way, and None otherwise —
    including whenever the answer cannot be established. Not knowing must never
    reorder a switch: stopping first is the destructive order, and it is only
    correct when the arithmetic is certain.
    """
    if not others or not host.is_remote or not host.gpu:
        return None
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
        gc.start_instance(host.gce_instance, host.gce_zone, host.gce_project)
    except GcloudError as exc:
        if not is_capacity_failure(exc.raw):
            _refused(exc)
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
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", help="Pick by card: l4, t4, a100.")] = None,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask before making changes.")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Show the plan and stop.")] = False,
    clean: Annotated[bool, typer.Option(
        "--clean", help="Delete what an earlier, half-finished move left behind, then move.")] = False,
) -> None:
    """Move a box to a zone that has capacity, keeping its ComfyUI install.

    A GPU stockout cannot be fixed where you are: the zone has none of that card
    and retrying will not change it. Done by hand this is a snapshot, a disk, an
    instance and a config edit — four chances to get it wrong.
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
        say.result("\nan earlier run left this behind, and it is billing:")
        for line in mine:
            say.result(f"  {line}")

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
            say.result("\n--dry-run: these would be deleted first, and are not:")
            for line in mine:
                say.result(f"  {line}")
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
    for note in found.notes:
        say.result(f"\nnote: {note}")

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
        say.result("\nalso on the project, unrelated to this move and billing:")
        for line in stray:
            say.result(f"  {line}")


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


def _interrupted_while_starting(host: Host) -> None:
    """Say what a Ctrl-C during a start actually left behind.

    Ctrl-C does not cancel the gcloud child: `Gcloud.run` catches the interrupt
    and waits a SECOND time, so the start completes and only then unwinds —
    measured at interrupt 0.4s, return 2.02s, resource created. And
    KeyboardInterrupt is a BaseException, so it walks past `_reportable()` and
    every handler in this file, and Click prints "Aborted!".

    A box that is running and billing, under a word that means nothing happened,
    is the most expensive sentence this tool can print. `_serve` and `logs`
    already get this right for their own phase; the boot phase had nothing.
    """
    say.error(
        f"interrupted — {host.name} may have started before you stopped it, "
        "and a started box bills",
        say.fix("check what is actually running:", "comfy-qat list --live",
                f"comfy-qat down {host.name}"),
    )


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
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    os_: Annotated[Optional[str], typer.Option(
        "--os", help="Pick by operating system: windows, linux, macos.")] = None,
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", help="Pick by card: l4, t4, a100.")] = None,
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
    config: Annotated[Optional[Path], typer.Option(
        "--config", help="Host list to read. Default: ~/.config/comfy-qa-tools/hosts.toml.")] = None,
) -> None:
    """With no subcommand, listing is the safe thing to do.

    `--config` is accepted here as well as on the subcommands: every other
    command takes it, so `host --config x` failing as a usage error is a
    surprise, and a surprise on the read-only default is a bad one.
    """
    if ctx.invoked_subcommand is None:
        ctx.invoke(list_cmd, config=config)
