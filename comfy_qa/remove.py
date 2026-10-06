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

A RESERVED BOX HAS A SECOND THING TO DESTROY, and it is the one that costs.
Its reservation bills for the card every hour, running or stopped, until it is
released — so a delete that removed the box and left the reservation would
leave the expensive half of the bill running with nothing in the host list
naming it. Deleting the box releases it: that is what "reserved until the box
is deleted" means. The reservation is released FIRST, so a failure between the
two leaves a cheap stopped box rather than an invisible billing reservation,
and the host list entry stays until both are gone. `RELEASE_FIRST` is that
order, in one place.

Only a reservation that is this box's own. One that other machines share, or
that was not made for this box and that the box is not bound to, is somebody
else's capacity: this refuses rather than release it from under them.

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

# Which of the two goes first when a reserved box is deleted: its reservation,
# or the instance. True is the reservation — it is the half that bills at a
# GPU's rate, so a failure in between then leaves a stopped box and not a
# reservation nobody can see.
#
# ONE SWITCH, because the order rests on something not yet read off Google:
# whether it will release a reservation that a STOPPED box still targets. If it
# will not, this becomes False and nothing else changes — `_destroy` runs the
# same two calls the other way round, each failure already says what is left
# for the order it happened in, and the entry is kept until the reservation is
# gone either way.
RELEASE_FIRST = True


def _refuse(message: str, fix: str | None = None) -> None:
    say.fail(message, fix=fix, code=2)


def _prose(text: str) -> None:
    """A sentence that is part of the answer, on stdout, broken at the prose width.

    `say.result` prints what it is given, which is right for a command and
    wrong for a sentence that names a box, a zone and a reservation and is over
    a hundred characters once it has. `say.wrapped` is the one wrapper and
    never breaks inside a word. The same helper, under the same name, as
    `host._prose`.
    """
    for line in say.wrapped(text):
        say.result(line)


# What `_bound_on_google` answers when the box's own record could not be read:
# not "bound to nothing", which is None. The caller says so rather than
# deleting in silence.
UNREAD = object()


def _bound_on_google(gc, host):
    """The reservation this box's OWN record says it is bound to, or None.

    For an entry that does not say. `gce_reservation` is one line in a file
    people edit, and it can be missing from a box that is reserved all the
    same: adopted before the field existed, reserved in the console, or added
    by hand after a `create --reserve` that could not write the host list.
    Deleting such a box on the entry's word alone removed the machine, said
    "and its disk are gone", and left its reservation billing with nothing
    anywhere naming it.

    The instance says which reservation it may consume, and `delete` is
    already describing the instance to size its disk — so it is asked. Best
    effort, like that read: a describe that fails does not stop the delete.
    It answers `UNREAD`, though, and not None — "could not ask" is not "bound
    to nothing", and the caller says which it was.
    """
    from . import reservation as rsv
    from .gcloud import GcloudError

    try:
        instance = gc.describe_instance(host.gce_instance, host.gce_zone,
                                        host.gce_project)
    except (GcloudError, OSError):
        return UNREAD
    return rsv.bound_to(instance or {})


def _left_behind(gc, host):
    """What a box that is already gone left on the project: `(name, lines)`.

    For an entry with no `gce_reservation` line, whose instance has no record
    left to ask. The project's own list is asked instead, and it answers one
    of four ways:

      (name, [])      this tool's own reservation for this box, in the entry's
                      zone — the caller hands it to `_held_for`, so it is
                      released, or left alone and named, or refused over, by
                      exactly the rules a declared one is.
      (None, lines)   a reservation that carries the box's name, or this
                      tool's mark for it somewhere else, and that nothing
                      establishes as this box's. Named, with Google's own
                      command, and not released.
      (None, [])      read, and nothing of the kind. The only answer after
                      which "only the host list entry is left" is true.
      UNREAD          the list did not come back. Not an absence.
    """
    from . import reservation as rsv
    from .gcloud import GcloudError

    zone, project, box = host.gce_zone, host.gce_project, host.gce_instance
    try:
        listed = rsv.parse_all(gc.list_reservations(project))
    except GcloudError:
        return UNREAD
    own = next((entry for entry in listed
                if entry.ours and entry.box == box and entry.zone == zone), None)
    if own is not None:
        return own.name, []
    lines: list[str] = []
    for entry in listed:
        if not (entry.name == rsv.name_for(box)
                or (entry.ours and entry.box == box)):
            continue
        lines += [
            f"{entry.name} ({entry.zone}) is on {project} and may have been "
            f"{host.name}'s, but nothing establishes that it was — it is not "
            f"one this tool made for {box} in {zone} — so it is left alone, "
            f"and it is still billing. Check whose it is, then release it:",
            f"  {rsv.delete_command(entry.name, entry.zone, project)}"]
    return None, lines


def _held_for(gc, host, name: str, *, already_gone: bool):
    """This box's reservation, as the project has it: `(reservation, lines)`.

    Read before anything is confirmed or destroyed, so the confirmation can
    name what will go and every refusal here is free. Three answers:

      (Reservation, [])   it is on the project and it is this box's own —
                          it will be released.
      (None, lines)       nothing to release, and `lines` say why, for the
                          user: Google says it is not there (already
                          released), or it is there and is NOT this box's to
                          release and the box itself is already gone — in
                          which case the last line is Google's own command to
                          release it, alone on its line so it can be pasted.
      refuses             everything else. A read that failed is not an
                          absence; a reservation other machines share is not
                          this box's; and one this tool did not make for this
                          box, that the box is not bound to, is somebody
                          else's capacity.

    ABSENCE IS ASKED OF GOOGLE BY NAME, with `reservation_absent`, and not
    inferred from a listing it did not turn up in. What rides on it is the
    bill: read as "already released" on a mistyped zone, the box would be
    deleted and its reservation left billing with nothing naming it.

    "THIS BOX'S OWN" is two tests, and either is enough. The description this
    tool wrote when it made it — `comfy-qat: held for <instance>` — or the
    box's own record saying it may consume this reservation and no other. The
    name alone is neither: anybody can call a reservation `something-rsv`.
    """
    from . import reservation as rsv
    from .gcloud import GcloudError

    zone, project = host.gce_zone, host.gce_project
    release = rsv.delete_command(name, zone, project)
    try:
        if gc.reservation_absent(name, zone, project):
            return None, [f"its reservation {name} is not on {project} — "
                          f"already released."]
        listed = rsv.parse_all(gc.list_reservations(project))
        instances = gc.list_instances(project)
    except GcloudError as exc:
        _refuse(
            f"could not read {host.name}'s reservation {name} ({exc}), so "
            f"whether it would be released is not known. Nothing was deleted.",
            fix=say.fix("see it yourself, then run this again:",
                        f"gcloud compute reservations list --project={project}",
                        f"comfy-qat delete {host.name}"))

    found = next((entry for entry in listed
                  if (entry.name, entry.zone) == (name, zone)), None)
    if found is None:
        # Google described it a moment ago and the listing does not carry it.
        # Whatever produced that gap, it is not an absence.
        _refuse(
            # Not "could not be read": that four-word run is one config.py also
            # builds, and the docs check then files this entry under config's
            # errors and fails it for not being one. host.py was reworded for
            # the same collision.
            f"{host.name}'s reservation {name} is on {project} but the project's "
            f"own listing did not include it, so what it holds is not known. "
            f"Nothing was deleted.",
            fix=say.fix("see it yourself, then run this again:",
                        f"gcloud compute reservations list --project={project}",
                        f"comfy-qat delete {host.name}"))

    on_it = [str(instance.get("name") or "") for instance in instances
             if rsv.bound_to(instance) == name
             and rsv.zone_of(instance) == zone]
    others = [box for box in on_it if box != host.gce_instance]
    ours = found.ours and found.box == host.gce_instance
    mine = ours or host.gce_instance in on_it

    # Why it is not this box's to release, and what releasing it would do —
    # two halves of one sentence, chosen together so the second is true of the
    # first. "From under them" needs a "them".
    why = taken = ""
    if others:
        why = f"is shared — other machines are on it: {', '.join(others)}"
        taken = "it from under them"
    elif found.vm_count != 1:
        # Zero as well as two: this tool only ever makes one for exactly one
        # machine, and "shared" is not the word for a reservation holding none.
        why = (f"holds capacity for {found.vm_count} machines, where one this "
               f"tool makes holds it for exactly one")
        taken = "capacity that is not this box's alone"
    elif not mine:
        why = (f"was not made by this tool for {host.gce_instance}, and "
               f"{host.gce_instance} is not bound to it")
        taken = "a reservation this box has no claim on"
    if not why:
        return found, []

    if already_gone:
        # The box is gone, so there is nothing left to refuse the deletion OF —
        # and refusing here would make the entry unremovable by any command in
        # this tool, which is the trap `delete` was changed to get out of. The
        # reservation is left exactly as it is, and that is said where it
        # cannot be missed: it is still billing.
        return None, [f"its reservation {name} {why}, so it is left alone — it "
                      f"is still on {project} and still billing. To release it:",
                      f"  {release}"]
    _refuse(
        f"{host.name}'s reservation {name} {why}. Deleting {host.name} would "
        f"release {taken}, so nothing was deleted.",
        fix=say.fix(
            "delete the box with Google's own command, which leaves the "
            "reservation alone:",
            f"gcloud compute instances delete {host.gce_instance} --zone={zone} "
            f"--project={project} --delete-disks=all",
            "then take the entry out of your host list:",
            f"comfy-qat delete {host.name}",
            "the reservation bills until it is released, by whoever it "
            "belongs to:",
            release))


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

    A reserved box is the exception to the first half of that: stopping it ends
    nothing, because its reservation bills for the card every hour whether the
    box runs or not. Deleting it releases the reservation as well, and is the
    only thing that stops that bill.

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

    # THE RESERVATION, read before the confirmation and before anything is
    # destroyed — on both paths, because a box that is already gone can have
    # left its reservation behind, and that is the half still billing.
    #
    # WHICH reservation is what the entry says, and failing that what the
    # instance itself says: a box can be reserved on Google with no
    # `gce_reservation` line in the host list, and its reservation bills just
    # the same. A box that is already gone has no record left to ask.
    held, not_released = None, []
    reserved_as = host.reservation
    undeclared = False
    left_behind = False
    unasked = ""
    if not reserved_as and not already_gone:
        reserved_as = _bound_on_google(gc, host)
        if reserved_as is UNREAD:
            reserved_as, unasked = None, "its own record could not be read"
        undeclared = bool(reserved_as)
    elif not reserved_as:
        # GONE, AND THE ENTRY DOES NOT SAY. The box has no record left to ask,
        # but the project still does: a reservation this tool made for it is
        # marked with its name. This used to print "only the host list entry
        # is left" without looking.
        found = _left_behind(gc, host)
        if found is UNREAD:
            unasked = ("it is gone, and the project's reservations could not "
                       "be listed")
        else:
            reserved_as, not_released = found
            left_behind = bool(reserved_as)
    if unasked:
        # NOT CHECKED, and said. Two ways an entry with no `gce_reservation`
        # line cannot be asked about: the box is already gone, or Google would
        # not describe it. Either way a reservation may be billing that this
        # delete will not release, and saying nothing reads as "there is none".
        # One line and the command that settles it — no refusal, because not
        # being able to look is not a reason to keep a box somebody has
        # decided to delete.
        _prose(f"whether {host.name} had a reservation was not checked — {unasked}. "
               f"One left behind bills with nothing on it; look:")
        say.result(f"  gcloud compute reservations list --project={host.gce_project}")
    if reserved_as:
        held, not_released = _held_for(gc, host, reserved_as,
                                       already_gone=already_gone)
    if left_behind:
        _prose(f"{host.name}'s host list entry has no reservation line, but "
               f"{reserved_as} is on {host.gce_project}: this tool made it for "
               f"{host.gce_instance}, and it bills every hour with or without "
               f"the box.")
    if undeclared:
        # Said before the confirmation, because it changes what a yes destroys
        # and the host list gave no warning of it.
        _prose(f"{host.name} is reserved, though its host list entry does not "
               f"say so: {host.gce_instance} is bound to the reservation "
               f"{reserved_as}, which bills every hour whether the box runs or "
               f"not.")

    if already_gone:
        # "ONLY the host list entry is left" is a claim about the project, and
        # it is made in exactly one case: the reservations were read and none
        # of them is this box's. Anything else names what is — or may be —
        # still there.
        if held is not None:
            left = (f"only its reservation {held.name} and the host list entry "
                    f"are left")
        elif len(not_released) > 1:
            left = ("its host list entry is left, and a reservation this "
                    "command does not release")
        elif unasked:
            left = ("its host list entry is left — and whether a reservation "
                    "is too is not known")
        else:
            left = "only the host list entry is left"
        _prose(f"{host.gce_instance} is not on {host.gce_project} — it has "
               f"already been deleted, so {left}.")
        if held is not None:
            _prose(f"its reservation {held.name} in {held.zone} is still "
                   f"billing, and releasing it is the only way to stop that "
                   f"— so it will be released.")
    elif held is not None:
        # All three named, because all three go. The reservation is the one a
        # person will not think of, and it is the one that bills.
        _prose(f"delete {host.gce_instance} in {host.gce_zone}, "
               f"{_boot_disk_phrase(gc, host)}, and its reservation "
               f"{held.name}.")
        say.result("this cannot be undone: the ComfyUI on it and anything it holds go too.")
    else:
        say.result(f"delete {host.gce_instance} in {host.gce_zone}, and "
                   f"{_boot_disk_phrase(gc, host)}.")
        say.result("this cannot be undone: the ComfyUI on it and anything it holds go too.")
    for line in not_released:
        _say_not_released(line)

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
        # No instance to call about: it is not there, its disk went with it.
        # Falling through to `instances delete` would 404 in exactly the place
        # this branch exists to get past. A reservation it left behind is still
        # there, though, and still released.
        say.result("")
        if held is not None:
            _release_the_reservation(gc, host, held, box_deleted=True)
    else:
        _destroy(gc, host, config, held)

    _forget_the_entry(config, host, hosts, already_gone=already_gone,
                      released=held)
    if len(not_released) > 1:
        # A reservation that was left alone, said again as the LAST thing this
        # command prints. Above the confirmation it is one line among several;
        # here it is what somebody reads before deciding they are finished, and
        # they are not: it is still billing.
        say.result("")
        for line in not_released:
            _say_not_released(line)


def _say_not_released(line: str) -> None:
    """One line of "there is nothing to release, and why": a sentence is
    wrapped, and a command — indented, as every offered command is — is left
    whole so it can be pasted."""
    if line.startswith(" "):
        say.result(line)
    else:
        _prose(line)


def _destroy(gc, host, config, held) -> None:
    """The box, and its reservation if it has one — in `RELEASE_FIRST`'s order.

    THE ONLY PLACE THE ORDER IS DECIDED. Each of the two calls reports its own
    failure for the state it happened in, so swapping them is this function
    and nothing else. Either way round, a failure exits before the host list
    is touched: the entry is the only record of whatever is left.
    """
    if held is None:
        _delete_the_box(gc, host, config)
    elif RELEASE_FIRST:
        _release_the_reservation(gc, host, held, box_deleted=False)
        _delete_the_box(gc, host, config, released=held)
    else:
        _delete_the_box(gc, host, config, unreleased=held)
        _release_the_reservation(gc, host, held, box_deleted=True)


def _release_the_reservation(gc, host, held, *, box_deleted: bool) -> None:
    """Release this box's reservation, or say exactly what is still billing.

    `box_deleted` is whether the instance is already gone when this runs, and
    it is the whole difference between the two failures: with the box still
    there, nothing was destroyed and running the command again does all of it;
    with the box gone, the reservation is billing with nothing on it and the
    entry is all that still names it.
    """
    from . import reservation as rsv
    from .gcloud import GcloudError

    release = rsv.delete_command(held.name, held.zone, host.gce_project)
    releasing = say.slow(f"releasing {held.name}", expect="up to a minute").start()
    try:
        # Registered for `_delete_the_box`'s reason: the request reaches Google
        # before an interrupt reaches gcloud, so by the time it can be
        # interrupted the reservation may be gone — or may not, and it bills
        # until it is. The entry is still in the host list either way, because
        # the code that takes it out has not run.
        with inflight.may_leave(
            f"the reservation {held.name} in {held.zone}, being released",
            undo=[
                "find out whether it went:",
                f"gcloud compute reservations list --project={host.gce_project}",
                f"then run it again — it carries on from whatever is left: "
                f"comfy-qat delete {host.name}",
            ],
            note=("if it is still there it is still billing, with or without "
                  f"{host.name} on it"),
            heading="this may or may not have been released:",
        ):
            try:
                gc.delete_reservation(held.name, held.zone, host.gce_project)
            except BaseException:
                releasing.give_up()
                raise
    except GcloudError as exc:
        releasing.give_up()
        if box_deleted:
            say.fail(
                f"{host.name} and its disk are gone, but its reservation "
                f"{held.name} could not be released ({exc}), so it is still "
                f"billing with no box on it. {host.name} is still in your host "
                f"list, as the only record of it.",
                fix=say.fix("release it:", release,
                            "then run this again to take the entry out:",
                            f"comfy-qat delete {host.name}"),
                code=1)
        say.fail(
            f"could not release {held.name} ({exc}), so {host.name} was not "
            f"deleted and its reservation is still billing.",
            fix=say.fix("release it yourself:", release,
                        "then run this again — it finds the reservation gone "
                        "and deletes the box:",
                        f"comfy-qat delete {host.name}"),
            code=1)
    except inflight.Interrupted:
        releasing.give_up()
        raise
    releasing.done(f"released {held.name}")


def _delete_the_box(gc, host, config, released=None, unreleased=None) -> None:
    """Delete the instance and its disks.

    `released` is a reservation this run has ALREADY released, so a failure
    here can say the box is the only thing left — and that it is no longer
    holding a place. `unreleased` is the other order's: a reservation this run
    was going to release AFTER the box, so a failure here means it was never
    reached and is still billing. One or neither, never both, and a failure
    says which — "the delete failed" alone reads as "nothing changed" in one
    order and hides a live bill in the other."""
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
        if released is not None:
            # Half of it happened, and it is the half that mattered for the
            # bill. Said before Google's own sentence so that "the delete
            # failed" is not read as "nothing changed": the reservation is
            # gone, and the stopped box that is left costs its disk.
            say.error(
                f"{host.name}'s reservation {released.name} was released, so it "
                f"is no longer billing for the card — but {host.name} itself "
                f"was not deleted, and its disk still bills.",
                say.fix("run it again to delete the box:",
                        f"comfy-qat delete {host.name}"))
        if unreleased is not None:
            # The box-first order's half. Nothing was deleted and nothing was
            # released: the release comes after the box in this order and was
            # never reached. Without this the only line printed is Google's
            # reason for refusing the instance delete, which says nothing
            # about a reservation at all.
            say.error(
                f"{host.name} was not deleted, so its reservation "
                f"{unreleased.name} was not released either and is still "
                f"billing.",
                say.fix("run it again once the box can be deleted:",
                        f"comfy-qat delete {host.name}"))
        say.fail(exc, code=1)
    except inflight.Interrupted:
        # A no-op in the ordinary case: the step was closed inside the
        # registration, before the report printed. Kept for an `Interrupted`
        # arriving from a nested registration that never touched this ticker.
        removing.give_up()
        raise
    removing.done()


def _forget_the_entry(config, host, hosts, *, already_gone: bool,
                      released=None) -> None:
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

    # What went, said by name — the reservation with it when there was one,
    # because "is the reservation gone too" is the question a reserved box's
    # owner has, and a sentence that does not answer it leaves them to go and
    # check.
    also = (f" Its reservation {released.name} was released, so nothing of it "
            f"is billing." if released is not None else "")
    if already_gone:
        _prose(f"\n{host.name} was already deleted, and it is now out of your "
               f"host list too.{also}")
        return
    _prose(f"\n{host.name} and its disk are gone, and it is out of your host "
           f"list.{also}")
