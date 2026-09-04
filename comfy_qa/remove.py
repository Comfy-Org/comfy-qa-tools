"""Deleting a box, which is the one thing here that cannot be undone.

Everything else this tool does is reversible. A box can be stopped and started,
a move leaves the original where it was, a bad host list has a backup beside it.
Deleting an instance and its disk destroys an install — the ComfyUI, the models
on it, and whatever a test run left behind — and nothing brings it back.

So this exists for one reason: the alternative was worse. Until now the only way
to remove a box was to retype

    gcloud compute instances delete <name> --zone=<zone> \\
        --project=<project> --delete-disks=all --quiet

off a screen, which is the same "read this and retype it correctly" problem the
`ssh` and `rdp` commands were added to solve — except this one ends in `--quiet`
and deletes a disk. Handing a person that line and hoping they edit it correctly
is not caution; it is the risk with extra steps.

The friction is deliberate and it is in one place: you type the box's name back.
A `[y/N]` is answered by reflex at 2am. A name is not.

Two refusals rather than warnings:

- **A running box is refused.** Stop it first. That is not really about GCE, which
  will delete a running instance quite happily — it is about making the state of
  the machine something you have looked at within the last few seconds.
- **`--delete-disks=all` is not optional.** Boot disks here are created
  `auto-delete=no`, so deleting the instance alone leaves 200-300 GB billing with
  nothing attached to it, which looks like nothing at all in a console. That is
  the leftover people actually get caught by, and a delete that leaves it behind
  would be the same defect this tool has already shipped three times.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from . import say
from .config import DEFAULT_CONFIG_PATH, ConfigError, load

app = typer.Typer()


def _refuse(message: str, fix: str | None = None) -> None:
    say.fail(message, fix=fix, code=2)


@app.command("delete")
def delete_cmd(
    name: Annotated[Optional[str], typer.Argument(help="Which machine to delete: its name.")] = None,
    config: Annotated[Optional[Path], typer.Option("--config")] = None,
    yes: Annotated[bool, typer.Option(
        "--yes", help="Skip the name confirmation. You have already decided.")] = False,
) -> None:
    """Delete a box and its disk permanently. Asks you to type its name.

    Stopping a box ends the expensive part of the bill; its disk keeps costing a
    few pounds a month. Deleting removes both, and the install with them.
    """
    from .gcloud import Gcloud, GcloudError, can_prompt

    if not name:
        _refuse("which machine? delete takes a name, never a description",
                fix="comfy-qat list")

    try:
        hosts = load(config)
    except ConfigError as exc:
        _refuse(str(exc))

    # By name only, and matched HERE rather than through `resolve` — which is the
    # whole point. `resolve` is deliberately generous: it takes an operating
    # system, a card, or both, because "the windows one" is a fine way to say
    # which machine you want to work on. It is a terrible way to say which machine
    # to destroy. `delete windows` resolving to comfy-win is survivable for `go`
    # and is not survivable here, and the first version of this command did
    # exactly that despite a comment claiming otherwise.
    host = next((h for h in hosts if h.name == name), None)
    if host is None:
        described = next((h for h in hosts if h.name.lower() == name.lower()), None)
        if described is not None:
            _refuse(
                f"no host is called {name!r}. Did you mean {described.name!r}?",
                fix=f"comfy-qat delete {described.name}",
            )
        _refuse(
            f"no host is called {name!r}. delete takes an exact name, never a "
            "description — a description can resolve to a machine you did not "
            "picture, and this cannot be undone",
            fix="comfy-qat list",
        )

    if not host.is_remote:
        _refuse(f"{host.name} is this machine, not a cloud box")

    gc = Gcloud()
    try:
        state = gc.instance_status(host.gce_instance, host.gce_zone, host.gce_project)
    except GcloudError as exc:
        say.fail(exc, code=2)

    # An allowlist of one, for the reason lifecycle.TERMINATED spells out: a box
    # in STAGING is not RUNNING and is not stopped either, and this command's
    # whole promise is that what you destroy is something you just looked at.
    if state != "TERMINATED":
        _refuse(
            f"{host.name} is {state.lower()}, not stopped. Stop it first, so that "
            "what you are deleting is something you have just looked at",
            fix=f"comfy-qat down {host.name}",
        )

    say.result(f"delete {host.gce_instance} in {host.gce_zone}, and its boot disk.")
    say.result("this cannot be undone: the ComfyUI on it and anything it holds go too.")

    if not yes:
        if not can_prompt():
            _refuse("this needs a terminal to confirm in", fix="add --yes if you are sure")
        typed = typer.prompt(f"\ntype {host.name} to confirm", default="", show_default=False)
        if typed.strip() != host.name:
            # 2, not 1. The rule this tool states is that 2 means nothing was
            # changed and 1 means the work started and failed — and declining a
            # confirmation is the first of those. It exited 1, which reads to a
            # script as "the delete was attempted and went wrong".
            say.result("nothing was deleted.")
            raise typer.Exit(code=2)

    removing = say.slow(f"deleting {host.name}", expect="up to a minute").start()
    try:
        gc.run([
            "compute", "instances", "delete", host.gce_instance,
            f"--zone={host.gce_zone}", f"--project={host.gce_project}",
            "--delete-disks=all", "--quiet",
        ], parse_json=False, timeout=300)
    except GcloudError as exc:
        removing.give_up()
        say.fail(exc, code=1)
    removing.done()

    # Taking the entry out is not tidying. `create` refuses a name that a host
    # list entry holds, and ports come from the same list, so leaving it reserves
    # both for a machine that does not exist — and the refusal arrives weeks later
    # with nothing to connect it to tonight.
    from .hostfile import HostFileError, apply, without

    path = config or DEFAULT_CONFIG_PATH
    try:
        text = without(path.read_text(encoding="utf-8"), host.name)
        apply(path, text, expect={h.name for h in hosts} - {host.name})
    except (HostFileError, OSError) as exc:
        say.result(f"\n{host.name} and its disk are gone.")
        say.warn(f"it is still in your host list and could not be removed: {exc}")
        say.warn(f"take [hosts.{host.name}] out by hand — while it is there, "
                 f"`create` will refuse the name {host.name} and its port stays "
                 "reserved for a machine that no longer exists")
        raise typer.Exit(code=1) from exc

    say.result(f"\n{host.name} and its disk are gone, and it is out of your host "
               "list.")
