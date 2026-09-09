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

from typing import Annotated, Optional

import typer

from . import say
from . import inflight
from .config import DEFAULT_CONFIG_PATH, ConfigError, load
from .host import ConfigOption

app = typer.Typer()


def _refuse(message: str, fix: str | None = None) -> None:
    say.fail(message, fix=fix, code=2)


def _boot_disk_phrase(gc, host) -> str:
    """"its 200 GB boot disk" — the number is what makes a person stop and read.

    This is the last irreversible thing the tool does, and the confirmation it
    prints is the only place a person can catch a mistake. "and its boot disk"
    is skimmed; a size is the difference between reading the line and passing
    over it, because 200 GB is a number you either recognise as the box you
    meant or do not.

    Best effort, and it degrades to the old wording rather than failing: a
    confirmation that cannot name a size still has to be shown, and refusing to
    delete because a cosmetic read failed would be the wrong trade on a command
    someone has already decided to run. One extra call, on a path that is
    interactive and destructive and can afford it.
    """
    from .gcloud import GcloudError
    from .relocate import boot_disk

    try:
        instance = gc.describe_instance(host.gce_instance, host.gce_zone,
                                        host.gce_project)
        name = boot_disk(instance or {})
        if not name:
            return "its boot disk"
        disk = gc.run([
            "compute", "disks", "describe", name,
            f"--zone={host.gce_zone}", f"--project={host.gce_project}",
        ]) or {}
        size = str((disk or {}).get("sizeGb") or "").strip()
    except (GcloudError, OSError):
        return "its boot disk"
    return f"its {size} GB boot disk" if size else "its boot disk"


@app.command("delete")
def delete_cmd(
    name: Annotated[Optional[str], typer.Argument(help="Which machine to delete: its name.")] = None,
    config: ConfigOption = None,
    yes: Annotated[bool, typer.Option(
        "--yes", help="Skip the name confirmation. You have already decided.")] = False,
) -> None:
    """Delete a box and its disk permanently. Asks you to type its name.

    Stopping a box ends the expensive part of the bill; its disk keeps costing a
    few pounds a month. Deleting removes both, and the install with them.

    This one writes: the box's entry goes out of your host list — the file
    `--config` names, and ~/.config/comfy-qa-tools/hosts.toml when it is left
    off — so the file is read and then rewritten, with a backup left beside it.
    """
    from .gcloud import Gcloud, GcloudError, can_prompt

    if not name:
        # `--live`, not the plain listing, and it is the same reason both times
        # below. The plain list reads the host file, where a box that is stopped
        # and a box that was destroyed last week look identical — and choosing
        # what to destroy is the one moment that difference matters.
        _refuse("which machine? delete takes a name, never a description",
                fix="comfy-qat list --live")

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
            fix="comfy-qat list --live",
        )

    if not host.is_remote:
        _refuse(f"{host.name} is this machine, not a cloud box")

    gc = Gcloud()
    already_gone = False
    try:
        state = gc.instance_status(host.gce_instance, host.gce_zone, host.gce_project)
    except GcloudError as exc:
        # A box that does not exist is not a reason to refuse; it is most of the
        # job already done. This read raises on a 404, so `delete` exited 2 with
        # Google's raw sentence and never reached the host list — and `discover`
        # only added. Between them the entry was unremovable by any command in
        # the tool, leaving hand-editing hosts.toml as the only route out, which
        # is the one thing this tool tells people not to do.
        #
        # It stays strict about everything else. The promise is that what you
        # destroy is something you have just looked at, and a project that says
        # it does not have the machine IS having looked. A read that failed for
        # any other reason is not, and still refuses.
        from .lifecycle import is_gone

        if not is_gone(gc, host):
            say.fail(exc, code=2)
        already_gone = True
        state = "TERMINATED"

    # An allowlist of one, for the reason lifecycle.TERMINATED spells out: a box
    # in STAGING is not RUNNING and is not stopped either, and this command's
    # whole promise is that what you destroy is something you just looked at.
    if state != "TERMINATED":
        from .lifecycle import readable_state

        _refuse(
            f"{host.name} is {readable_state(state)}, not stopped. Stop it first, "
            "so that what you are deleting is something you have just looked at",
            fix=f"comfy-qat down {host.name}",
        )

    if already_gone:
        say.result(f"{host.gce_instance} is not on {host.gce_project} — it has "
                   f"already been deleted, so only the host list entry is left.")
    else:
        say.result(f"delete {host.gce_instance} in {host.gce_zone}, and "
                   f"{_boot_disk_phrase(gc, host)}.")
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

    if already_gone:
        # Nothing to call: the instance is not there, its disk went with it, and
        # the remaining work is the entry. Falling through to `instances delete`
        # would 404 in exactly the place this branch exists to get past.
        say.result("")
    else:
        _delete_the_box(gc, host, config)

    _forget_the_entry(config, host, hosts, already_gone=already_gone)


def _delete_the_box(gc, host, config) -> None:
    from .gcloud import GcloudError

    removing = say.slow(f"deleting {host.name}", expect="up to a minute").start()
    try:
        # The only mutating call in this module, and the last one in the package
        # that was not registered. What makes it different from the others is
        # what is at risk: they leave a RESOURCE unaccounted for, and this leaves
        # THIS TOOL'S OWN RECORD wrong. The request carries `--delete-disks=all`,
        # so by the time it can be interrupted the box and its disk are being
        # destroyed server-side — while the host list below still says the
        # machine exists, because the code that takes the entry out is the code
        # that did not run.
        #
        # The cost is written down forty lines from here, in the comment on that
        # removal: a name and a port reserved for a machine that does not exist,
        # and a `create` refused weeks later with nothing to connect it to
        # tonight.
        #
        # Its own heading, and it is the only one of the four that is not about
        # a resource at all. Neither outcome here is spending — a deleted box
        # bills nothing, and this refuses to run unless the box is already
        # TERMINATED — so all three of the other sentences are about the wrong
        # thing. What is wrong is the RECORD.
        with inflight.may_leave(
            f"{host.gce_instance} in {host.gce_zone}, and its boot disk",
            undo=[
                "find out which of the two happened:",
                f"gcloud compute instances describe {host.gce_instance} "
                f"--zone={host.gce_zone} --project={host.gce_project}",
                # This used to end "running `comfy-qat delete <name>` again
                # will not do it — it refuses a box it cannot read", and that
                # was true and was a trap: with `discover` only ever adding, it
                # meant no command in the tool could remove the entry and
                # hand-editing hosts.toml was the only way out. `delete` now
                # asks the project when its own read fails, so if the box really
                # did go, running it again finishes the job.
                f"if it is gone, run it again — it now asks the project and "
                f"takes the entry out: comfy-qat delete {host.name}",
            ],
            note=(f"the entry is stale whichever way it went, and while "
                  f"[hosts.{host.name}] is there, `create --name {host.name}` "
                  f"refuses that name and its port stays reserved"),
            heading="this was probably destroyed, and the host list still names it:",
        ):
            # Inside the registration for the reason spelled out at the same
            # shape in `host.rdp_cmd`: `may_leave` prints the leftovers report
            # from its OWN handler and raises `Interrupted` afterwards, so an
            # `except inflight.Interrupted` further out closes this step after
            # the report rather than before it. `removing` is a background
            # `Slow` writing `still going, …` from a thread every second, and
            # what it can land on here is the heading saying the host list may
            # now name a box that no longer exists.
            try:
                gc.run([
                    "compute", "instances", "delete", host.gce_instance,
                    f"--zone={host.gce_zone}", f"--project={host.gce_project}",
                    "--delete-disks=all", "--quiet",
                ], parse_json=False, timeout=300)
            except BaseException:
                removing.give_up()
                raise
    except GcloudError as exc:
        removing.give_up()
        say.fail(exc, code=1)
    except inflight.Interrupted:
        # A no-op in the ordinary case: the step was closed inside the
        # registration, before the report printed. Kept for an `Interrupted`
        # arriving from a nested registration that never touched this ticker.
        removing.give_up()
        raise
    removing.done()


def _forget_the_entry(config, host, hosts, *, already_gone: bool) -> None:
    # Taking the entry out is not tidying. `create` refuses a name that a host
    # list entry holds, and ports come from the same list, so leaving it reserves
    # both for a machine that does not exist — and the refusal arrives weeks later
    # with nothing to connect it to tonight.
    from .hostfile import HostFileError, apply, read, without

    path = config or DEFAULT_CONFIG_PATH
    try:
        text = without(read(path), host.name)
        apply(path, text, expect={h.name for h in hosts} - {host.name})
    except (HostFileError, OSError) as exc:
        say.result(f"\n{host.name} was already deleted." if already_gone
                   else f"\n{host.name} and its disk are gone.")
        say.warn(f"it is still in your host list and could not be removed: {exc}")
        say.warn(f"take [hosts.{host.name}] out by hand — while it is there, "
                 f"`create --name {host.name}` will refuse it, and its port stays "
                 "reserved for a machine that no longer exists")
        raise typer.Exit(code=1) from exc

    if already_gone:
        say.result(f"\n{host.name} was already deleted, and it is now out of your "
                   "host list too.")
        return
    say.result(f"\n{host.name} and its disk are gone, and it is out of your host "
               "list.")
