"""The docs are part of the tool, so they are tested like it.

The rule we hold ourselves to: an error and its troubleshooting entry get written
together. A command whose failure modes cannot be documented is a command that is
not understood yet. This test is what stops that rule quietly lapsing.

It used to enforce that against a hand-written list of phrases, which only worked
while somebody remembered to extend it — an error added without touching the list
was invisible, so the rule held for the errors we had already thought about and
nowhere else. The list is now read out of the source instead, by walking the AST
of every module in `comfy_qa/` for the ways this tool tells someone that something
went wrong:

  1. `typer.echo(..., err=True)`, and `say.error` / `say.fail` / `say.warn`,
     which are the same thing once a module has been converted to the shared
     output vocabulary — anything written to stderr
  2. `ConfigError`, `GcloudError`, `LifecycleError`, `ProbeError`, `SetupStopped`
     — the message argument of every failure this tool raises at a person
  3. `Check(..., False, ...)` and a `say(...)` inside an `except` handler — the
     two places a failure is reported without being raised
  4. anything assigned to a local called `message`, because gcloud's failure
     classifier builds its message that way and the raise site carries no literal

A message is then cut at each interpolation, and every run of literal text long
enough to identify it has to appear in troubleshooting.md — verbatim, because the
point of the page is that a pasted error finds its own entry. A message with no
run that long is held to its longest, so a two-word error cannot slip through on
a technicality.

Adding an error without documenting it fails. Nothing has to be remembered.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PACKAGE = ROOT / "comfy_qa"

# Every exception whose message is shown to a person rather than raised into a
# traceback. Each takes that message as its first positional argument.
#
# `TunnelError` joined the list when `host open`, `up` and `go` started catching
# it: until they did, it reached a terminal as a traceback rather than as a
# message, so the page had nothing to say about it and this test had no reason to
# ask. An error class becomes documentable the moment a command reports it.
# The walk matches the CONSTRUCTOR name, not the base class, so a subclass is
# invisible until it is named here. `HostFileError` subclasses `ConfigError` and
# was missed on exactly that basis — which left every message in the module that
# rewrites the host list during a move undocumented and unnoticed.
ERROR_TYPES = ("BadParameter", "ConfigError", "GcloudError", "HostFileError",
               "LifecycleError", "MoveError", "ProbeError", "SetupStopped",
               "TunnelError")

# `comfy_qa/say.py` is where stderr output goes now. A converted module writes
# `say.fail("...", fix=...)` rather than two `typer.echo(..., err=True)` calls and
# an `Exit`, and if this walk only knew the echo form, converting a module would
# have quietly emptied it out of the page. Both forms are read, because the
# conversion is happening one module at a time and both are live.
SAY_FAILURES = ("error", "fail", "warn")

# Literal text that is deliberately *not* a troubleshooting entry. There are only
# two kinds, and both have to be argued for in a comment before being added:
#
#   - a wrapper that prints an error raised somewhere else. The entry belongs at
#     the place the error is raised, not on every line that reprints it.
#   - a progress line that happens to be printed from inside an `except` handler.
#
# Anything else added here is the hand-maintained list coming back, so keep it
# short and keep the reasons honest.
NOT_AN_ENTRY = {
    # `setup` and `host` reprint a SetupStopped / GcloudError / LifecycleError
    # message and its fix under these two prefixes. Both are documented where
    # they are raised.
    "setup stopped": "prefix on an error raised elsewhere",
    "to fix": "prefix on an error's own fix line",
    # Not a failure: SSH is retried until the box answers, and this says why the
    # wait is long. The Windows half is appended only on Windows, which is the
    # whole point — this used to tell an Ubuntu box that Windows is slow — so the
    # exemption is keyed on the part that is always printed.
    "waiting for the machine to accept commands":
        "progress while retrying, not a failure",
}

# A run this long identifies the message on its own, so every one of them has to
# be findable in the page. A message made only of shorter runs — `f"{name}: no"` —
# still has to be documented, by its longest run, or a two-word error would slip
# through on a technicality.
IDENTIFYING = 12


def _normalise(text: str) -> str:
    """One space between words, no glue punctuation at the ends.

    Messages are stitched together from literals and interpolations, so a run of
    literal text routinely starts or ends mid-sentence — `". The machine is up"`.
    The docs quote the sentence, not the glue.
    """
    return re.sub(r"\s+", " ", text).strip().strip(" .,;:—-")


def _literal_runs(node: ast.AST | None) -> list[str]:
    """Every uninterrupted run of literal text in a message expression.

    An f-string yields one run per gap between interpolations, so
    `f"could not start {name}: {exc}"` yields `["could not start ", ": "]`.
    """
    if node is None:
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        runs: list[str] = []
        current = ""
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                current += part.value
            elif current:
                runs.append(current)
                current = ""
        if current:
            runs.append(current)
        return runs
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _literal_runs(node.left) + _literal_runs(node.right)
    if isinstance(node, ast.IfExp):
        return _literal_runs(node.body) + _literal_runs(node.orelse)
    if isinstance(node, ast.BoolOp):
        # `something or "a fallback message"` — both branches can be printed.
        return [run for value in node.values for run in _literal_runs(value)]
    # A call such as `str(exc)` or `", ".join(...)` carries no message of its own.
    return []


@dataclass(frozen=True)
class Message:
    """One thing the tool can say when something has gone wrong."""

    where: str
    phrase: str


def _called_name(call: ast.Call) -> str:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _is_false(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is False


def _is_true(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is True


def _message_argument(call: ast.Call, *, in_except: bool) -> ast.AST | None:
    """The user-facing message this call prints, if it prints one at all."""
    name = _called_name(call)

    # `secho` as well as `echo`, and the difference between them is why. A guard
    # that only covers the spellings already in use is one the next commit gets
    # past for free — and this one fails SILENTLY: an error written with
    # `typer.secho(..., err=True)` was never collected, so no case was generated,
    # so the count did not move. Measured in an extract: the echo spelling failed
    # by name, the secho spelling was byte-identical to a clean run. A vanishing
    # case at least changes a number; an absence does not. `secho` has no call
    # site in the package today, which is the cheapest moment to close it.
    if name in ("echo", "secho"):
        if any(kw.arg == "err" and _is_true(kw.value) for kw in call.keywords):
            return call.args[0] if call.args else None
        return None

    if name in SAY_FAILURES:
        return call.args[0] if call.args else None

    if name in ERROR_TYPES:
        return call.args[0] if call.args else None

    # auth's readiness report: Check(name, ok, detail, fix). A failed check is
    # printed as `FAIL  <name>  <detail>`; a passing one is not a failure.
    if name == "Check" and len(call.args) >= 3 and _is_false(call.args[1]):
        return call.args[2]

    # setup reports rather than raises, through the injected `say`. Only the ones
    # in an except handler are failures; the rest are progress.
    if name == "say" and in_except:
        return call.args[0] if call.args else None

    return None


def _calls_inside_except(tree: ast.AST) -> set[int]:
    inside: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ExceptHandler):
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    inside.add(id(child))
    return inside


# KNOWN GAP, stated rather than left to be discovered. This walk reads the MESSAGE
# argument and not the `fix=` keyword, so advice that lives in a fix is printed to
# the user and is not required to appear on the troubleshooting page. The say
# conversion moved text that way — host.py's covered messages fell from 35 to 25
# while every one of them was still printed.
#
# Collecting `fix=` was tried and reverted: it demands ~64 new entries, most of
# them command lines, and requiring `gcloud compute instances delete ... --quiet`
# to appear verbatim in prose is a bar that teaches people to paste commands into
# documentation to make a test pass. Closing it properly means separating advice
# from commands and writing the entries deliberately — worth doing, not worth
# doing badly at the end of a long day.
#
# So the guarantee this file provides is: every failure MESSAGE is documented.
# Not: every word the user sees.


def _local_reporters(tree: ast.AST) -> set[str]:
    """Module-level helpers that exist to emit a failure, by name.

    A module may wrap its own reporting — `def _refuse(message, fix=None):
    say.fail(message, fix=fix, code=2)` — and then every call site carries the
    literal while the recognised name is the wrapper's. `remove.py`, which holds
    the only irreversible command in this tool, was written entirely that way and
    contributed exactly ZERO messages to this walk: not a hole in the net, a blind
    side, because nothing looked wrong anywhere.

    So the guarantee was never "every error is documented"; it was "every error
    not routed through a local wrapper is documented", and nobody had said so.
    One level of indirection is followed — a wrapper that calls a wrapper is not,
    deliberately, because at that point the module should be using `say` directly.
    """
    reporters: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            emits = (
                isinstance(func, ast.Attribute)
                and func.attr in SAY_FAILURES
                and isinstance(func.value, ast.Name)
                and func.value.id == "say"
            )
            if emits:
                reporters.add(node.name)
                break
    return reporters


def _message_expressions(tree: ast.AST):
    """Every expression in a module that becomes a user-facing failure message."""
    handled = _calls_inside_except(tree)
    reporters = _local_reporters(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            message = _message_argument(node, in_except=id(node) in handled)
            if message is None and node.args and isinstance(node.func, ast.Name) \
                    and node.func.id in reporters:
                message = node.args[0]
            if message is not None:
                yield node.lineno, message

        # gcloud's failure classifier builds its message in a local and raises
        # that, so the raise carries no literal at all. Following the variable is
        # the only way those branches are visible here.
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "message" for target in node.targets
        ):
            yield node.lineno, node.value


def collect_messages() -> list[Message]:
    """Every failure message in comfy_qa/, read out of the source."""
    found: list[Message] = []
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, expression in _message_expressions(tree):
            runs = [_normalise(run) for run in _literal_runs(expression)]
            runs = [run for run in runs if run and run not in NOT_AN_ENTRY]
            if not runs:
                # Nothing but interpolation and glue: this line reprints an error
                # raised somewhere else, and that is where its entry lives.
                continue
            for phrase in [run for run in runs if len(run) >= IDENTIFYING] or [max(runs, key=len)]:
                found.append(Message(f"{path.name}:{lineno}", phrase))
    return sorted(set(found), key=lambda m: (m.where, m.phrase))


MESSAGES = collect_messages()


# Raised things that carry no message of their own, written down rather than
# inferred from a naming pattern. This list started as a suffix match — collect
# names ending in Error, Stopped or Parameter — which is an allowlist by
# convention, so an unfamiliar name was SILENTLY SKIPPED. That is the same shape
# as the tuple which dropped two classes today: a completeness check whose default
# for the unfamiliar is "fine". `click.ClickException` is the realistic case, since
# typer is built on click and `typer.BadParameter` is already raised two lines away.
#
# Inverted, an unfamiliar name fails and has to be argued for here, and the list
# becomes the written record of what carries no message — knowledge that was
# previously held only in a regex.
EXCUSED = frozenset({
    "Exit", "Abort",                      # control flow: no message at all
    # Ctrl-C, and the code it exits with. Also control flow with no message:
    # `Interrupted` is deliberately empty, because what an interrupt has to say
    # is not a property of the exception — it is whatever `inflight` had
    # registered at the moment it was raised, which is different for a create,
    # a start and a move. `cli.main` prints that, and `report`'s own wording is
    # documented in troubleshooting.md like any other message.
    "Interrupted", "SystemExit",
    "_stopped", "_unregistered", "give_up",  # helpers that BUILD an exception;
                                          # their literals are caught at the
                                          # constructor call inside them
})


def test_every_exception_this_package_defines_is_named_here():
    """The tuple above is hand-maintained, and it has now silently dropped two
    classes: `HostFileError`, invisible because the walk matches the constructor
    name and not the base class, and `MoveError` — which meant `relocate.py`
    contributed ZERO messages while being fully converted to the say vocabulary.

    `move` is the command that takes snapshots, creates disks and creates
    instances, so its failures are precisely where money gets left behind. One of
    the messages this exposed says a box is "running and billing, but your host
    list could not be updated" — an orphan the tool can no longer stop — and it
    had no required entry.

    So the tuple is no longer trusted to be complete. Every exception defined in
    this package has to be named in it, or deliberately excused here.
    """
    defined = set()
    for path in PACKAGE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef) and any(
                isinstance(base, ast.Name) and (
                    base.id in ("Exception", "BaseException")
                    or base.id in ERROR_TYPES
                    or base.id.endswith("Error"))
                for base in node.bases
            ):
                defined.add(node.name)

    # `BaseException` is not decoration. This walk read `Exception`, a name
    # ending in Error, or a name already in the tuple — so a class based on
    # `BaseException` matched none of the three and was invisible here.
    # `inflight.Interrupted` is the first this package has had, and only the
    # sibling below caught it: two tests written to cover each other's holes,
    # and the hole was in the one nobody re-read. Measured before widening —
    # `Interrupted` is the only name this adds.
    #
    # And the same EXCUSED as the sibling, rather than none. This test had no
    # way to say "carries no message", so the only way past it was ERROR_TYPES
    # — which for `Interrupted` would harvest nothing, because membership means
    # exactly one thing (collect the constructor's first argument) and it is
    # raised with none. That is a listing that looks like coverage and is not.
    missing = sorted(defined - set(ERROR_TYPES) - EXCUSED)
    assert not missing, (
        f"{', '.join(missing)} is raised by this package and is not in "
        "ERROR_TYPES, so every message it carries is invisible to this walk "
        "and nothing looks wrong anywhere."
    )


def test_every_exception_this_package_RAISES_is_named_here():
    """The sibling of the test above, and the hole it cannot see.

    That one enumerates classes DEFINED in comfy_qa/, so it closes the class for
    our own exceptions and is blind to an imported one. `typer.BadParameter` is
    raised twice in host.py and is defined by typer, so it fell outside however
    complete that enumeration became — and both selector refusals stayed
    undocumented while nothing looked wrong.

    This one reads what is RAISED rather than what is defined, which catches the
    third-party case without anyone having to think of it in advance.
    """
    raised = set()
    for path in PACKAGE.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Raise) or node.exc is None:
                continue
            call = node.exc
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = (func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else None)
            if name:
                raised.add(name)

    missing = sorted(raised - set(ERROR_TYPES) - EXCUSED)
    assert not missing, (
        f"{', '.join(missing)} is raised in this package and is not in "
        "ERROR_TYPES, so the messages it carries are invisible here."
    )


def _troubleshooting_text() -> str:
    return re.sub(r"\s+", " ", (DOCS / "troubleshooting.md").read_text(encoding="utf-8"))


def test_the_message_list_was_actually_found():
    """A walker that silently matches nothing would pass every test below it."""
    assert len(MESSAGES) > 40, f"only found {len(MESSAGES)} messages — the walk is broken"
    files = {message.where.split(":")[0] for message in MESSAGES}
    assert {"auth.py", "commands.py", "config.py", "gcloud.py", "host.py",
            "lifecycle.py", "setup.py"} <= files


def test_a_message_routed_through_say_is_still_collected():
    """The conversion to `say` must not be a way to leave the page behind.

    `auth.py` and `commands.py` no longer contain a single `typer.echo(err=True)`,
    so if this walk had kept reading only for that form, their errors would have
    dropped out of the required set and nobody would have been told. This is the
    check that the new form is really being read, rather than the file list above
    happening to hold for some other reason.
    """
    tree = ast.parse('say.fail("a message long enough to identify", fix="do this")')
    found = [phrase for _, expression in _message_expressions(tree)
             for phrase in _literal_runs(expression)]
    assert "a message long enough to identify" in found

    routed = [m for m in MESSAGES if m.where.split(":")[0] in ("auth.py", "commands.py")]
    assert routed, "no auth/commands messages collected — the say rule is not firing"


@pytest.mark.parametrize("message", MESSAGES, ids=lambda m: f"{m.where} {m.phrase[:40]}")
def test_every_error_has_a_troubleshooting_entry(message):
    assert message.phrase in _troubleshooting_text(), (
        f"{message.where} can print {message.phrase!r}, which is not in "
        f"troubleshooting.md. Quote it there, verbatim, with what it means and "
        f"what to do — or, if it is not a failure, say so in NOT_AN_ENTRY."
    )


# --- and the commands we hand over to gcloud instead of running -------------
#
# The sibling of the check above. That one asks whether a `comfy-qat ...` we
# offer is a command that exists; this asks whether a raw `gcloud ...` we hand
# over is one we should be handing over at all.
#
# D29 filed this as seven sites where "the tool could run it and does not". Every
# one turned out to have a reason, and two of them are the REPAIR for a real
# defect: `create.py` used to print `comfy-qat down <gce instance>` for a box
# absent from the host list — a command that cannot resolve — and printing raw
# gcloud with the zone is the fix. Acting on the row as filed would have put that
# defect back.
#
# So this does not forbid hand-overs. It pins the set, so a NEW one is a decision
# somebody makes on purpose and writes a reason for, rather than a habit that
# spreads. Reading the strings without the surrounding reasons is exactly how the
# row came to be filed twice.

# The gcloud verbs that CHANGE something. `list` and `describe` are absent on
# purpose: offering somebody a read-only look at their own project is advice, not
# work this tool declined to do.
MUTATING_GCLOUD = re.compile(
    r"gcloud (?:compute (?:instances (?:stop|start|delete|create|add-access-config)"
    r"|disks delete|snapshots delete|reset-windows-password|ssh)"
    r"|auth login|config set)")

# Every hand-over, and why it is handed over rather than run. Five reasons, and
# each site says the same thing at greater length where it lives.
GCLOUD_BY_DESIGN = {
    "auth.py": "gcloud auth login and config set project are interactive and belong to the user",
    "setup.py": "the same two, from the setup walkthrough",
    "gcloud.py": "the same two again, as the `fix` carried by the errors this module "
                 "raises; and reset-windows-password, which is credential-bearing",
    "tunnel.py": "gcloud auth login, when a tunnel dies because the session expired",
    "create.py": "the box is in no host list, so `comfy-qat down` cannot reach it; "
                 "and the interrupt undo, where the process is dying and can run nothing",
    "host.py": "boxes this tool did not declare — it only operates what you declare — "
               "and reset-windows-password, which is credential-bearing",
    "lifecycle.py": "a ComfyUI this run did not start, and add-access-config, which "
                    "changes the box's networking on a hypothesis the tool cannot confirm",
    "relocate.py": "a box the host list cannot name after a failed rewrite; the instance "
                   "delete that destroys an install ('handed over, never run'); and the "
                   "disk and snapshot deletes, which `remove_leftovers` DOES run under "
                   "--clean — the printed form is the manual alternative",
}


def _gcloud_handovers() -> set[str]:
    """Every module OFFERING a raw gcloud command that changes something.

    `_in_command_position`, borrowed from the check above, and for the same
    reason it exists there: `gcloud auth login` appears in prose as often as in a
    fix line — "`gcloud auth login` opens a browser and prompts" is a sentence
    about the command, not an offer of it. Matching anywhere in the string finds
    four modules that hand over nothing.
    """
    found = set()
    for where, text in _offered_strings():
        for line in text.splitlines():
            match = MUTATING_GCLOUD.search(line)
            if match and _in_command_position(line, match.start()):
                found.add(where.split(":")[0])
    return found


def _offered_strings() -> list[tuple[str, str]]:
    """String constants MINUS docstrings.

    Command position is not enough on its own here. A docstring explaining a
    command quotes it in backticks — "`gcloud auth login` opens a browser and
    prompts" — and a backtick is exactly the punctuation that marks an offer, so
    prose about the tool reads as the tool offering something. Four modules
    joined the list that way and hand over nothing.

    A docstring documents; a fix line offers. That is the line.
    """
    docstrings = set()
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef
                              | ast.ClassDef):
                continue
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(f"{path.name}:{first.value.lineno}")
    return [(where, text) for where, text in _string_constants()
            if where not in docstrings]


def test_every_module_handing_over_raw_gcloud_has_a_reason():
    """A new hand-over is a decision, not a habit — so it fails until somebody
    writes down why the tool is not doing the work itself."""
    undeclared = sorted(_gcloud_handovers() - set(GCLOUD_BY_DESIGN))
    assert not undeclared, (
        f"{', '.join(undeclared)} hands over a raw gcloud command that changes "
        f"something, and GCLOUD_BY_DESIGN does not say why. Either run it — the "
        f"tool has a Gcloud and a host list — or add the reason it cannot."
    )


def test_no_module_is_listed_as_handing_over_and_does_not():
    """And the other direction, so the list shrinks when a hand-over becomes a
    thing the tool does itself. D64 closed one that way."""
    stale = sorted(set(GCLOUD_BY_DESIGN) - _gcloud_handovers())
    assert not stale, (
        f"{', '.join(stale)} is in GCLOUD_BY_DESIGN and no longer hands over "
        f"anything. Remove it — the exemption is describing something gone."
    )


# --- and the other direction: an entry that describes nothing real -----------
#
# The guard above checks that every message the package can print has an entry.
# Nothing checked the reverse, and the reverse fails too: A FIX CAN LEAVE THE
# PAGE ASSERTING THE DEFECT. troubleshooting.md went on telling people that a
# trailing comment on a port line "is a limitation of the rewrite rather than a
# problem with your file" for six commits after that was fixed — advertising a
# defect that no longer existed, and telling people to work around something
# that had started working.
#
# So: every entry heading has to quote something the package can still say.
# Matching is against the LITERAL RUNS of real messages — the constant parts of
# the f-strings the tool builds — not against the package text at large. That
# distinction is the whole guard. Measured: matching entries against any
# sufficiently long run of package source catches one deleted message in three,
# because entries are long and a large package repeats its vocabulary. Matching
# against identified message runs catches five in six.
#
# WHAT THIS DOES NOT CATCH, because a guard that looks complete and is not is
# worse than one that admits its edges:
#
#   - PROSE INSIDE AN ENTRY. The stale sentence above was body text under a
#     heading that is still perfectly valid, so this would not have caught the
#     instance that prompted it. Only the heading is mechanical.
#   - A light reword. Removing a message is caught; rewording it while keeping
#     twelve characters of any one run is not.
#   - Whether the entry's ADVICE is still right. Only that the message exists.

# A run this long identifies a message, the same threshold the forward guard
# uses on the same strings.
ENTRY_RUN = IDENTIFYING

# Two lists, because there are two reasons an entry can be unmatchable and they
# have opposite futures.
#
# These are somebody else's words, quoted so a user can recognise them. They are
# permanent: no change to this package will ever make them ours.
NOT_OUR_MESSAGE = {
    "comfy-qat: command not found":
        "the shell says this, not this tool",
    "NO_PYTHON":
        "a sentinel this tool makes a remote script print, not a message it prints",
    "AssertionError: Torch not compiled with CUDA enabled":
        "torch's own words, quoted from a ComfyUI log",
    "Required 'compute.instances.start' permission":
        "gcloud's own refusal, quoted so it can be recognised",
}

# These ARE our messages. They are excused because the MATCHER cannot see them —
# every literal run is shorter than ENTRY_RUN — not because the tool does not say
# them. Keeping them under the other name was a real hole rather than a tidiness
# problem: an entry excused as "not ours" is excused for ever, so deleting one of
# these messages would leave its entry behind saying nothing had changed. That is
# the exact failure this guard exists to catch, sitting inside its own escape
# hatch, under a name that says the opposite.
#
# So this list is a known LIMIT with a shrinking membership. Anyone who improves
# ENTRY_RUN or the run-splitting knows exactly which entries to re-test, and the
# hygiene test below ejects one the moment it becomes matchable — which is how
# the capacity entry left: it was a truncation, and quoting four more words of
# the message it documents made it checkable like everything else.
TOO_SHORT_TO_MATCH = {
    "request for l4 failed":
        "f-string: 'request for' is 11 characters, 'failed:' is 7",
    "testcloud serves 4f2a1b9c":
        "f-string: 'serves' and 'expected' are 6 and 8",
}

EXEMPTED = {**NOT_OUR_MESSAGE, **TOO_SHORT_TO_MATCH}


def _message_runs() -> set[str]:
    """Every literal run of every string the package builds, ENTRY_RUN or longer.

    The constant parts of an f-string are what survive into what a user sees, so
    they are what an entry can be checked against.
    """
    found: set[str] = set()
    for path in sorted(PACKAGE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            pieces: list[str] = []
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                pieces = [node.value]
            elif isinstance(node, ast.JoinedStr):
                pieces = [part.value for part in node.values
                          if isinstance(part, ast.Constant)
                          and isinstance(part.value, str)]
            for piece in pieces:
                for run in re.split(r"\{[^}]*\}", piece):
                    run = _normalise(run)
                    if len(run) >= ENTRY_RUN:
                        found.add(run)
    return found


def _entry_headings() -> list[str]:
    """The quoted message at the head of each troubleshooting entry."""
    raw = (DOCS / "troubleshooting.md").read_text(encoding="utf-8")
    return [_normalise(entry)
            for entry in re.findall(r"^\*\*`(.+?)`\*\*", raw, flags=re.M | re.S)]


ENTRIES = _entry_headings()
MESSAGE_RUNS = _message_runs()


def test_the_entry_list_was_actually_found():
    """A parse that silently matched nothing would pass the test below it."""
    assert len(ENTRIES) > 150, f"only found {len(ENTRIES)} entries — the parse is broken"
    assert len(MESSAGE_RUNS) > 500, f"only {len(MESSAGE_RUNS)} runs — the walk is broken"


@pytest.mark.parametrize("entry", ENTRIES, ids=lambda e: e[:40])
def test_every_troubleshooting_entry_still_describes_something_real(entry):
    if any(key in entry for key in EXEMPTED):
        pytest.skip("see NOT_OUR_MESSAGE / TOO_SHORT_TO_MATCH")
    assert any(run in entry for run in MESSAGE_RUNS), (
        f"troubleshooting.md documents {entry!r}, and nothing in comfy_qa/ can "
        f"still say it. Either it was reworded — quote the new wording verbatim "
        f"— or it is gone, and this entry describes a defect that no longer "
        f"exists. Delete it rather than leaving it to be believed."
    )


def test_no_exempted_entry_has_quietly_become_checkable():
    """The allowlist is the part that rots. A message that grows a longer run
    should rejoin the check rather than stay exempt for ever."""
    stale = sorted(
        key for key in EXEMPTED
        if any(key in entry and any(run in entry for run in MESSAGE_RUNS)
               for entry in ENTRIES))
    assert not stale, (
        f"{', '.join(stale)} now quotes something the package says, so it no "
        f"longer needs an exemption. Remove it from NOT_OUR_MESSAGE or "
        f"TOO_SHORT_TO_MATCH."
    )


def test_no_exempted_entry_has_left_the_page():
    """And an exemption for an entry nobody has any more is just clutter."""
    absent = sorted(key for key in EXEMPTED
                    if not any(key in entry for entry in ENTRIES))
    assert not absent, (
        f"{', '.join(absent)} is exempted but is not in troubleshooting.md."
    )


# --- the config errors the page quotes, checked against how they are BUILT --
#
# The two guards above are both substring guards, and between them they cannot
# see a quoted message change. Measured, on the `no [hosts.<name>] tables found`
# entry: lengthening the message and then shortening it back was invisible to
# both. The forward guard is parametrised over the package's messages, so
# removing one REMOVES ITS CASE — 679 passed became 678 passed, nothing red, a
# test vanishing rather than failing. The backward guard passed because the old
# short message is a substring of the new long heading. Two guards over one
# string, neither able to report it moving.
#
# So config.py's messages get a third check of a different shape. It is the one
# module where it is affordable and where it matters most: every ConfigError is
# built by interpolation, so what a user actually sees is never written down
# anywhere, and troubleshooting.md quotes twenty-five of them with the values
# filled in. "Does the page still quote what the tool says" is not a question a
# substring can answer.
#
# What this does instead: rebuild each `ConfigError(...)` as a regex — literal
# text verbatim, each `{...}` as "something" — and require the quotation to match
# it END TO END. A reword now fails, because the literal either survives in full
# or it does not. Substring luck is gone.
#
# Its edges, named:
#
#   - It reads config.py only. Every other module's messages are still held by
#     the substring guards alone. This is the module whose messages are quoted
#     most, and doing it everywhere would need a way to name the entry each
#     message belongs to; that does not exist yet.
#   - It cannot know an interpolation is REACHABLE. `port 80 is outside
#     1024-65535` is checked to be built that way, not that 80 gets there.
#   - Trailing glue is forgiven, the same way _normalise forgives it, because a
#     page that quotes a sentence should not have to carry its full stop.

CONFIG = PACKAGE / "config.py"

# The punctuation a quotation may drop from the end of a sentence it quotes.
GLUE = " .,;:—-"


def _collapse(text: str) -> str:
    """`_normalise` without the glue-stripping.

    A quotation is compared to the message END TO END, so its own punctuation has
    to survive: `is not valid TOML: ...` quotes the colon, and _normalise would
    take the colon off along with the `...` and leave a string the construction
    cannot match.
    """
    return re.sub(r"\s+", " ", text).strip()


def _message_parts(node: ast.AST) -> list[tuple[bool, str]]:
    """A message expression as `(is_literal, text)` pieces, in order."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [(True, node.value)]
    if isinstance(node, ast.JoinedStr):
        return [(True, part.value)
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
                else (False, "")
                for part in node.values]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _message_parts(node.left) + _message_parts(node.right)
    return [(False, "")]


def _literal_pattern(text: str) -> str:
    """Literal text, verbatim, but tolerant of the page having re-wrapped it.

    troubleshooting.md wraps at eighty columns, so a message's single space can
    be a newline on the page. Nothing else about the text is allowed to differ.
    """
    words = text.split()
    if not words:
        return r"\s*"
    pattern = r"\s+".join(re.escape(word) for word in words)
    if text[:1].isspace():
        pattern = r"\s*" + pattern
    if text[-1:].isspace():
        pattern = pattern + r"\s*"
    return pattern


def _construction(parts: list[tuple[bool, str]]) -> str:
    """The regex for one built message: literals verbatim, `{...}` as anything.

    The page writes an interpolation it does not want to invent a value for as
    `...`, so that spelling is accepted too, and a trailing interpolation may be
    left off the end entirely — `is not valid TOML: ...` quotes the sentence
    without the parser's own words after it.
    """
    parts = [part for part in parts if not (part[0] and part[1] == "")]
    out: list[str] = []
    for index, (literal, text) in enumerate(parts):
        last = index == len(parts) - 1
        if literal:
            out.append(_literal_pattern(text.rstrip(GLUE) if last else text))
            if last:
                out.append(f"[{re.escape(GLUE)}\\s]*")
        else:
            out.append(r"(?:.+?|\.\.\.)" + ("?" if last else ""))
    return "".join(out)


def _message_lines(parts: list[tuple[bool, str]]) -> list[list[tuple[bool, str]]]:
    """One part-list per line of a multi-line message.

    `unknown host {wanted!r}` is three lines on purpose and the page quotes them
    as three code spans, so each line has to be checkable on its own.
    """
    lines: list[list[tuple[bool, str]]] = []
    current: list[tuple[bool, str]] = []
    for literal, text in parts:
        if not literal:
            current.append((literal, text))
            continue
        chunks = text.split("\n")
        current.append((True, chunks[0]))
        for chunk in chunks[1:]:
            lines.append(current)
            current = [(True, chunk)]
    lines.append(current)
    return lines


@dataclass(frozen=True)
class Construction:
    """One way config.py can build a message a user sees."""

    where: str
    pattern: str


def _config_constructions() -> list[Construction]:
    constructions: list[Construction] = []
    for node in ast.walk(ast.parse(CONFIG.read_text(encoding="utf-8"))):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ConfigError"
                and node.args):
            continue
        parts = _message_parts(node.args[0])
        where = f"config.py:{node.lineno}"
        constructions.append(Construction(where, _construction(parts)))
        lines = _message_lines(parts)
        if len(lines) > 1:
            for number, line in enumerate(lines, start=1):
                constructions.append(
                    Construction(f"{where} line {number}", _construction(line)))
    return constructions


def _config_runs() -> set[str]:
    """The identifying literal runs of config.py's messages, and only those.

    Used to decide which of the page's quotations are config's. Matching against
    the whole module would drag in every path and URL it also builds.
    """
    runs: set[str] = set()
    for node in ast.walk(ast.parse(CONFIG.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ConfigError"
                and node.args):
            runs |= {run for run in
                     (_normalise(text) for text in _literal_runs(node.args[0]))
                     if len(run) >= ENTRY_RUN}
    return runs


# A heading, plus the code spans joined to it by ` / ` — one entry documenting
# one message, whether that message is one line or three. Nothing may cross a
# blank line, or a heading holding inline backticks swallows the entries under
# it: `**`comfy-qa` runs but `comfy-qat` does not**` runs to the next `**, which
# is four entries later.
SPAN = r"`(?:[^\n]|\n(?!\n))+?`"
HEADING_GROUP = re.compile(
    rf"^\*\*({SPAN})\*\*((?:\s*/\s*\*\*{SPAN}\*\*)*)", re.M)
ONE_SPAN = re.compile(r"\*\*`((?:[^\n]|\n(?!\n))+?)`\*\*")


def _quoted_config_errors() -> list[tuple[int, str]]:
    """Every code span in troubleshooting.md that quotes a ConfigError.

    Selected per heading GROUP rather than per span, so the two shorter lines of
    a three-line message come along with the line long enough to identify it.
    `declared: local, comfy-win` carries no run of its own and would otherwise
    have to be excused; grouped, it is checked like the rest.
    """
    raw = (DOCS / "troubleshooting.md").read_text(encoding="utf-8")
    runs = _config_runs()
    found: list[tuple[int, str]] = []
    for match in HEADING_GROUP.finditer(raw):
        spans = [_collapse(match.group(1).strip("`"))]
        spans += [_collapse(span) for span in ONE_SPAN.findall(match.group(2))]
        if any(run in span for span in spans for run in runs):
            line = raw.count("\n", 0, match.start()) + 1
            found += [(line, span) for span in spans]
    return found


CONFIG_CONSTRUCTIONS = _config_constructions()
QUOTED_CONFIG_ERRORS = _quoted_config_errors()


# How many quoted config errors there are, written down, because a parametrised
# guard CANNOT REPORT A CASE IT NO LONGER GENERATES. Both other guards on this
# page were measured failing that way, and so was this one: rewording a message
# takes its run out of config.py, the entry stops being recognised as config's,
# and its case disappears rather than going red. The number is what notices.
#
# It is hand-maintained on purpose, for the reason REQUIRED is in
# test_suite_integrity.py: it moves only when someone deliberately documents a
# config error or stops, the fix is one line, and it catches the one failure that
# reading a rising test count never will.
QUOTED_CONFIG_ERRORS_EXPECTED = 26


def test_the_config_error_quotations_were_actually_found():
    """A parse that found nothing would make the check below vacuously green."""
    assert len(CONFIG_CONSTRUCTIONS) > 20, (
        f"only {len(CONFIG_CONSTRUCTIONS)} ConfigError constructions — the walk "
        f"over config.py is broken")
    assert len(QUOTED_CONFIG_ERRORS) == QUOTED_CONFIG_ERRORS_EXPECTED, (
        f"troubleshooting.md quotes {len(QUOTED_CONFIG_ERRORS)} config errors, "
        f"not {QUOTED_CONFIG_ERRORS_EXPECTED}. Going UP is fine — a new error was "
        f"documented, raise the number. Going DOWN is the thing to look at: an "
        f"entry stops being recognised as config's when the message it quotes is "
        f"reworded far enough, and its case then vanishes from the check below "
        f"instead of failing. Confirm the entry was deleted on purpose before "
        f"lowering this."
    )


@pytest.mark.parametrize("line,quotation", QUOTED_CONFIG_ERRORS,
                         ids=lambda value: str(value)[:40])
def test_every_quoted_config_error_is_still_built_that_way(line, quotation):
    matches = [c.where for c in CONFIG_CONSTRUCTIONS
               if re.fullmatch(c.pattern, quotation, flags=re.S)]
    assert matches, (
        f"troubleshooting.md line {line} quotes a config error that config.py "
        f"can no longer build end to end:\n\n  {quotation}\n\n"
        f"Every literal word of a message has to survive into the page, because "
        f"the page's promise is that a pasted error finds its own entry. Nothing "
        f"in config.py matches this one all the way through — it was reworded, "
        f"or the interpolations moved. Re-quote it from the message as it is "
        f"built now. The substring guards above will not tell you this; both "
        f"were measured passing through a message being changed and changed back."
    )


# --- the commands our own messages tell people to run ---------------------


def _command_tree(app) -> dict:
    """The real command surface, as nested names, straight off the Typer app."""
    tree: dict = {}
    for command in app.registered_commands:
        tree[command.name] = {}
    for group in app.registered_groups:
        tree[group.name] = _command_tree(group.typer_instance)
    return tree


def _string_constants() -> list[tuple[str, str]]:
    found = []
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                found.append((f"{path.name}:{node.lineno}", node.value))
    return found


# Three words is the depth of the deepest path we have (`auth quota request`);
# past that it is prose, or an argument.
_INVOCATION = re.compile(r"comfy-qat((?:[ \t]+[a-z][a-z0-9-]*){1,3})")


def _in_command_position(line: str, start: int) -> bool:
    """Is this `comfy-qat` a command being offered, or the tool being talked about?

    The tool's own name appears in prose as well as in fix lines — "you are not
    the only comfy-qat running", "another comfy-qat is opening the tunnel" — and
    read as an invocation those become `comfy-qat running` and `comfy-qat is
    opening the`, two commands nobody ever suggested. What separates them is
    position: an offered command starts a line, or follows a backtick or the
    punctuation that introduces it (`Run: `, `e.g. `, `done:  `). Prose has an
    ordinary word in front of it, and that is the whole test.
    """
    before = line[:start].rstrip()
    return not before or not before[-1].isalnum()


def _invocations() -> list[tuple[str, tuple[str, ...]]]:
    """Every command a string in this package offers someone to run.

    Line by line rather than over a whitespace-flattened string: a help table
    puts one command per line, and flattening it makes the last word of one line
    look like the word in front of the next command.
    """
    calls = []
    for where, text in _string_constants():
        for line in text.splitlines():
            for match in _INVOCATION.finditer(line):
                if _in_command_position(line, match.start()):
                    calls.append((where, tuple(match.group(1).split())))
    return calls


@pytest.mark.parametrize(
    "where,words",
    _invocations(),
    ids=lambda value: " ".join(value) if isinstance(value, tuple) else value,
)
def test_commands_we_tell_people_to_run_exist(where, words):
    """A fix line naming a command that does not exist is worse than no fix line.

    Walk as far into the real command tree as the words go. Stopping is only
    allowed at a leaf, where what follows is an argument — `host stamp local`.
    Stopping at a group means the next word was meant to name a subcommand and
    does not: `auth quota` never became `auth quotas`, and this is what says so.
    """
    from comfy_qa.cli import app

    node = _command_tree(app)
    walked: list[str] = []
    for word in words:
        if word not in node:
            assert not node, (
                f"{where}: `comfy-qat {' '.join(walked + [word])}` — "
                f"{'no such command' if not walked else f'{word!r} is not one of'} "
                f"{', '.join(sorted(node))}"
            )
            break
        walked.append(word)
        node = node[word]



@pytest.mark.parametrize(
    "name",
    ["getting-started", "machines", "hosts", "troubleshooting", "cost", "test-criteria"],
)
def test_page_exists_and_is_not_a_stub(name):
    page = DOCS / f"{name}.md"
    assert page.exists(), f"docs/{name}.md is missing"
    assert len(page.read_text().split()) > 100, f"docs/{name}.md is a stub"


def _occurrences(line: str, needle: str) -> list[int]:
    found, start = [], line.find(needle)
    while start != -1:
        found.append(start)
        start = line.find(needle, start + 1)
    return found


def test_docs_do_not_reference_the_old_command_name():
    """The binary is comfy-qat; comfy-qa is a different project's binary.

    Naming the old name to *remove* it is the one legitimate use, so an uninstall
    line is exempt. Telling someone to run it never is.
    """
    for page in DOCS.glob("*.md"):
        for line in page.read_text().splitlines():
            if "pip uninstall" in line:
                continue
            # A path that happens to contain the name is not an invocation.
            invocations = [
                index for index in _occurrences(line, "comfy-qa ")
                if index == 0 or line[index - 1] not in "/-"
            ]
            assert not invocations, f"{page.name}: stale command name in {line!r}"


def test_guide_leads_with_the_single_setup_command():
    """Setup is one command. If guide ever lists steps again, this fails."""
    from comfy_qa.cli import FIRST_RUN

    assert "comfy-qat setup" in FIRST_RUN
    for follow_up in ["list", "status"]:
        assert follow_up in FIRST_RUN


def test_getting_started_leads_with_setup_not_a_command_list():
    text = (DOCS / "getting-started.md").read_text()
    assert "comfy-qat setup" in text
    assert text.index("comfy-qat setup") < text.index("comfy-qat list")


def test_the_module_entry_point_exposes_the_current_surface():
    """`python -m comfy_qa` ran v0's surface long after v0 stopped being the tool.

    An entry point that quietly points at old code is the kind of thing nobody
    notices until they use it.
    """
    from comfy_qa import cli

    # Importing __main__ would run the CLI, so read it instead.
    entry = (DOCS.parent / "comfy_qa" / "__main__.py").read_text()
    assert "from .cli import main" in entry, "the module entry point still points at v0"

    names = {command.name for command in cli.app.registered_commands}
    groups = {group.name for group in cli.app.registered_groups}
    assert {"setup", "guide", "env"} <= names
    assert {"host", "auth"} <= groups


def test_the_package_register_is_the_current_surface_not_v0():
    import comfy_qa
    from comfy_qa.cli import register

    assert comfy_qa.register is register


def test_no_superseded_planning_documents_remain():
    """ROADMAP.md and DEVELOPMENT.md described a scope that no longer exists.

    Features are documented when they ship; a stale plan in the repo root reads as
    current to anyone who has not been in the conversation.
    """
    root = DOCS.parent
    for name in ("ROADMAP.md", "DEVELOPMENT.md"):
        assert not (root / name).exists(), f"{name} is superseded and should be gone"


def test_the_test_criteria_are_actually_pasteable():
    """The first version opened with `QAT=/path/to/venv/bin/comfy-qat`.

    A tester pasted it verbatim — which is the correct thing to do with a block
    labelled copy-paste — and every one of the forty checks after it failed on a
    path that does not exist. A placeholder inside a runnable block is a bug.
    """
    text = (DOCS / "test-criteria.md").read_text()
    assert "/path/to/" not in text
    assert "<your-" not in text

    # The preamble has to define what every later block leans on.
    for name in ["VENV=", "REPO=", "QAT=", "PY=", "qat()"]:
        assert name in text, f"the preamble does not set {name}"

    # `python` is not on PATH on a stock macOS; the venv's interpreter is.
    for line in text.splitlines():
        assert not line.strip().startswith("python "), f"bare python in {line!r}"


def test_nothing_claims_the_tunnel_needs_no_ssh_key():
    """`tunnel.py` opened with "no SSH keys" for a day after it started using one.

    Nothing caught it: the derived docs test checks the errors this tool prints,
    and this was a claim about how the tool works — the kind of confidently wrong
    sentence that sends the next reader down a path that does not exist. The
    forward is `gcloud compute ssh -L`, which uses gcloud's own
    ~/.ssh/google_compute_engine.
    """
    prose = [ROOT / "README.md", ROOT / "comfy_qa" / "tunnel.py",
             ROOT / "comfy_qa" / "host.py"] + list(DOCS.glob("*.md"))
    for path in prose:
        text = path.read_text()
        for claim in ("no SSH key", "no SSH keys", "SSH-less"):
            assert claim not in text, f"{path.name}: {claim!r} is no longer true"


def test_the_documented_tunnel_command_is_the_one_the_tool_builds():
    """Two places described a `start-iap-tunnel` that is no longer what runs."""
    from comfy_qa.config import Host
    from comfy_qa.tunnel import command

    built = " ".join(command(Host(
        name="box", kind="gce", os="Ubuntu 22.04", port=8190,
        gce_instance="box", gce_zone="z", gce_project="p")))
    assert "compute ssh" in built and "-L 127.0.0.1:8190:127.0.0.1:8188" in built

    for path in (ROOT / "README.md", DOCS / "machines.md", DOCS / "test-criteria.md"):
        text = path.read_text()
        assert "start-iap-tunnel" not in text, (
            f"{path.name} still describes the tunnel the tool stopped using"
        )
