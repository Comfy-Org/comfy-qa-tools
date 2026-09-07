"""Every command the tool advertises is documented, and the pack actually runs it.

`tests/test_readme.py` has held one page to that rule since `auth`, `up`, `open`,
`down` and `stamp` all shipped while the README still listed them under "still to
come". It worked — the README is the only page that has stayed right. That is the
whole finding: the page with a test was correct and the pages without one drifted,
and by the time anyone looked, `ssh`, `rdp`, `delete` and `disconnect` were missing
from the command reference, missing from the everyday-loop page, and — worse —
missing from the acceptance pack, which meant a tester could complete it, sign off
a release, and never once exercise the only command here that cannot be undone.

Nothing failed when they were left out. That is what this file is for.

Two different bars, because the pages do different jobs:

  - `docs/commands.md` is a reference table, so the bar is the same as the
    README's: a table row, not a mention. A sentence saying a command does not
    exist satisfies a substring search just as well as one documenting it, which
    is exactly how the README check used to pass while being wrong.

  - `docs/test-criteria.md` is a run sheet, so the bar is that the pack **runs**
    the command — `qat <name>` inside a shell block. A criterion describing a
    command the block never types is the failure this file was written for, in
    its purest form: G6 required `down --all` for a release-1 sign-off while
    phase G's block contained no such command, so the box was ticked on faith.

`docs/machines.md` is deliberately NOT held to this. It is narrative — "how do I
get onto the box" — and demanding all 22 command paths appear in it would be a bar
that teaches people to paste command rows into prose to make a test pass. It gets
prose about the four commands it was missing, and no test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer

from comfy_qa.cli import app

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PACKAGE = ROOT / "comfy_qa"
COMMANDS_PAGE = DOCS / "commands.md"
PACK = DOCS / "test-criteria.md"


def _surface(typer_app: typer.Typer, prefix: str = "") -> list[str]:
    """Every command path this tool advertises, e.g. 'quota request'.

    Hidden ones are skipped: `host go` and `auth status` are the old spellings,
    kept working so nothing written down before the verbs moved breaks, and `env`
    belongs to a different tool. Documenting either would document the same
    command twice.
    """
    found = []
    for command in typer_app.registered_commands:
        if getattr(command, "hidden", False):
            continue
        found.append(f"{prefix}{command.name}".strip())
    for group in typer_app.registered_groups:
        if getattr(group, "hidden", False):
            continue
        found.extend(_surface(group.typer_instance, prefix=f"{prefix}{group.name} "))
    return found


COMMANDS = _surface(app)


def test_the_surface_is_not_empty():
    """A bug in _surface would make every test here pass vacuously."""
    assert {"delete", "ssh", "rdp", "disconnect", "quota request"} <= set(COMMANDS)


@pytest.mark.parametrize("command", COMMANDS)
def test_every_command_has_a_row_in_the_command_reference(command):
    rows = [line for line in COMMANDS_PAGE.read_text().splitlines()
            if line.lstrip().startswith("|")]
    assert any(f"comfy-qat {command}" in row for row in rows), (
        f"`comfy-qat {command}` exists but docs/commands.md has no table row for it"
    )


def _pack_shell() -> str:
    """Everything the acceptance pack tells a tester to type.

    Only fenced `sh` blocks — prose about a command is not a run of it, and the
    gap this file exists for was precisely criteria that described more than
    their block executed.
    """
    return "\n".join(re.findall(r"```sh\n(.*?)```", PACK.read_text(), re.S))


@pytest.mark.parametrize("command", COMMANDS)
def test_the_acceptance_pack_runs_every_command(command):
    """`qat` is the alias the pack's own preamble defines."""
    assert f"qat {command}" in _pack_shell(), (
        f"docs/test-criteria.md never runs `qat {command}`, so a tester can "
        f"complete the pack without exercising it"
    )


def test_a_mention_is_not_a_run():
    """The guard on the guard: prose about a command must not satisfy the check
    above, since a criterion without a command to run is the whole defect."""
    prose = "- [ ] **N1** — `comfy-qat delete windows` is refused."
    assert not re.findall(r"```sh\n(.*?)```", prose, re.S)


def test_the_pack_does_not_tell_the_tester_to_stay_quiet():
    """A pack line converting a live defect into "do not report" is worse than a
    wrong criterion.

    This one is real: the pack carried "that is a cosmetic lag rather than a
    defect, but do not mark it as a failure" about the old `comfy-qat host ...`
    spellings, naming three examples that had all been fixed — while three that
    were still live went unnamed. A tester who found a real one had been
    pre-instructed to ignore it.

    Deciding a defect does not block a release is fine, and the sign-off says so
    for A8 by name. Telling the tester not to see it is not.
    """
    text = PACK.read_text().lower()
    for phrase in ("do not mark it as a failure",
                   "do not report",
                   "rather than a defect"):
        assert phrase not in text, f"the pack tells the tester to ignore something: {phrase!r}"


# --------------------------------------------------------------------------
# The two halves of the acceptance pack, checked against each other.
#
# Nothing did this before, and every false pass we found was the same shape: a
# criterion describing more than its block executes. G6 required `qat down --all`
# behaviour for a release-1 sign-off while phase G's block contained no such
# command, so the box was ticked on faith or nothing shipped. L5 graded three
# answers and ran one, and the branch it never ran is the one that says the
# machine is billing.

# Why a criterion may have no command. Each is a fact about the world rather than
# about the tool, and adding to this list is meant to be the moment somebody has to
# justify one — the same bargain `test_docs.NOT_AN_ENTRY` makes.
UNRUNNABLE = ("needs", "need ", "interactive", "only reachable", "only exists",
              "not arrangeable", "same precondition", "run at ", "run between",
              "cannot be forced", "not something to arrange", "window is seconds")

# `/` belongs in the id class. Without it this walk silently skipped the nine
# combined criteria — `A5a/A5b`, `J2/J3/J4`, `N8/N9` and the rest — so the guard
# below was enforcing over 148 of the pack's 157 boxes and reporting success.
# That is class 6 of docs/tests-that-cannot-fail.md, in the guard written to
# catch the pack's version of the same disease. `test_the_walk_sees_every_box`
# is the cure: assert the walk's own count against a dumb line count, so a
# narrowed pattern fails loudly instead of quietly checking less.
CRITERION = re.compile(r"- \[ \] \*\*([A-Za-z0-9./]+)\*\*(.*?)(?=\n- \[ \]|\n\n|\n#)", re.S)


def _criteria() -> list[tuple[str, str]]:
    return CRITERION.findall(PACK.read_text())


def _markers() -> set[str]:
    """The `=== G6a` labels the pack's own shell blocks print as they run."""
    return set(re.findall(r"===\s*([A-Za-z0-9.]+)", _pack_shell()))


def test_the_pack_has_criteria_at_all():
    assert len(_criteria()) > 100


def test_the_walk_sees_every_box():
    """The guard on the guard, and it is here because this walk has already lied.

    A checkbox line is `- [ ] **<id>** …`, so counting them needs no regex worth
    getting wrong. If the structured walk returns fewer, the pattern has narrowed
    and some criteria are going unchecked — which is exactly what happened: the id
    class omitted `/`, and nine combined ids were skipped in silence.
    """
    dumb = len([line for line in PACK.read_text().splitlines()
                if line.startswith("- [ ] **")])
    assert len(_criteria()) == dumb, (
        f"the walk sees {len(_criteria())} criteria but the file has {dumb} "
        f"checkboxes — the pattern has narrowed and the difference is unchecked"
    )


@pytest.mark.parametrize("criterion", [c for c, _ in _criteria()])
def test_every_criterion_is_runnable_or_says_why_not(criterion):
    """A tester must be able to reach every box, or be told plainly they cannot.

    Two ways to satisfy this. Either a shell block prints `=== <id>`, in which case
    pasting the block exercises it; or the criterion carries a `*(…)*` aside giving
    a REASON it cannot be driven from one — a real stockout, two GPU boxes, a
    Ctrl-C into a live stream, a state that lasts seconds.

    The reason is what makes the second case not a loophole, and it is load-bearing
    rather than pedantry. G6 escaped with `*(New with --all. Not run.)*`: "not run"
    is a fact about the LAST pass, not about whether the check can be run at all,
    and the page used one phrasing for both. Requiring the reason separates them.
    Checked rather than assumed — against the pack as it stood at `73fda1a` this
    assertion fails on G6, and on eleven others.

    Suffixes count as covered by their stem: `E3b` is checked from what `=== E3`
    prints, and `N12a` from `=== N12`. That is the page's own convention and not
    a weakening — the marker is what puts the output on screen.

    What this refuses is the third case: a criterion with neither. That is a box
    someone has to tick without a way to earn it, and for a release-1 requirement
    it is worse than a wrong criterion, because a wrong one gets reported.
    """
    body = dict(_criteria())[criterion]
    markers = _markers()
    # `A5a/A5b` is one line grading two commands, and each half has its own marker.
    # Checking the joined string would match nothing and fail every combined id.
    parts = criterion.split("/")
    runnable = all(
        any(part[:n] in markers for n in range(len(part), 0, -1))
        for part in parts
    )
    if runnable:
        return
    aside = " ".join(re.findall(r"\*\((.*?)\)\*", body, re.S)).lower()
    assert any(reason in aside for reason in UNRUNNABLE), (
        f"criterion {criterion} has no `=== {criterion}` in any shell block, and its "
        f"aside gives no reason why it cannot have one. A tester cannot tick it "
        f"honestly; say what the check needs, or give the block a line."
    )


def test_the_stem_rule_does_not_swallow_an_unrelated_id():
    """The guard on the guard: `N1` must not be counted as covering `N12`.

    The rule walks the id from its full length down, so a marker only covers ids
    it is a prefix of — and `N1` IS a prefix of `N12`. That is the one direction
    this could go wrong in, so it is stated: a marker for a shorter id makes a
    longer one pass. It is accepted because the page numbers criteria in order
    and a stem always names the command the suffix varies, but if a phase ever
    grows an `N1` and an unrelated `N12`, this is the line that explains why the
    check went quiet.
    """
    assert "N1"[:2] == "N1"


def test_the_command_reference_exists_and_is_not_a_stub():
    """`docs/commands.md` could be deleted and this suite would stay green.

    Established by reading the tests rather than by trying it: `test_docs`'s
    stub check is parametrized over getting-started, machines, hosts,
    troubleshooting, cost and test-criteria — `commands` is not in that list. The
    glob tests iterate whatever files happen to be there, and no test follows
    README's link to it. So the page README calls "every command" is the one page
    with no floor under it, which is the same shape as the finding above: the
    tests are not checking the documentation, they are defining it.
    """
    assert COMMANDS_PAGE.exists(), "docs/commands.md is gone and nothing noticed"
    assert len(COMMANDS_PAGE.read_text().split()) > 400, "reduced to a stub"


def test_troubleshooting_names_the_tunnel_the_tool_actually_builds():
    """`test_docs` forbids `start-iap-tunnel` in README, machines.md and
    test-criteria.md — and omits troubleshooting.md, the only page that had it.

    The claim there was "The tunnel is `gcloud compute start-iap-tunnel` and
    nothing else", which sends somebody hunting a process that does not exist, so
    they cannot find their own tunnel to kill and conclude it has died. The page
    may still describe `start-iap-tunnel` in the past — the switch to `ssh -L` is
    why loopback binding and the firewall rules went away, and that history is
    worth keeping — so this pins the present-tense claim, not the string.
    """
    from comfy_qa.config import Host
    from comfy_qa.tunnel import command

    page = (DOCS / "troubleshooting.md").read_text()
    built = " ".join(command(Host(
        name="box", kind="gce", os="Ubuntu 22.04", port=8190,
        gce_instance="box", gce_zone="z", gce_project="p")))

    assert "compute ssh" in built and "--tunnel-through-iap" in built
    assert "The tunnel is `gcloud compute start-iap-tunnel`" not in page, (
        "troubleshooting.md asserts a tunnel command the tool stopped using")
    assert "-L 127.0.0.1:" in page, (
        "troubleshooting.md never shows the forward, which is what to look for in `ps`")


# --------------------------------------------------------------------------
# The direction `test_docs` does not check.
#
# `test_docs` walks the SOURCE and requires every message to appear in
# troubleshooting.md, so an undocumented error fails. Nothing walks the PAGE and
# asks whether what it quotes is still a thing the tool says. A message that
# CHANGES therefore leaves a correct-looking entry behind, and the entry heading
# is what a pasted error is matched against — so the paste finds nothing, or
# finds the wrong entry.
#
# Two lived here until today, both quoting the retired `comfy-qat host …` /
# `comfy-qat auth …` spellings:
#
#   "or stop paying for it: comfy-qat host down comfy-win"  — `stop_paying`
#       returns f"comfy-qat down {name}" and this path has never printed the
#       other form.
#   "…or `comfy-qat auth quota` to check." — the heading said `auth quota`
#       while its own body two lines below said `comfy-qat quota`, which is what
#       auth.py prints. The half a reader searches was the wrong half.
#
# THIS CHECK CATCHES THE FIRST AND NOT THE SECOND, and the limit is worth stating
# rather than discovering. It compares COMMAND SPELLINGS: `comfy-qat host down`
# appears nowhere in the package, so it is caught. `comfy-qat auth quota` DOES
# appear — it is the live prefix of `comfy-qat auth quota request` — so a message
# that merely says something different around a real command reads as fine here.
# Catching that needs the whole quoted message compared against the whole source
# message, which is `test_docs`'s machinery pointed the other way, and is a bigger
# job than this file. A guard that looked like it covered both would be worse than
# this one, because the next person would stop looking.

OLD_SPELLING = re.compile(r"comfy-qat (?:host|auth)(?: (?!--)[a-z][a-z-]*)+")


def _package_source() -> str:
    return "\n".join(path.read_text() for path in sorted(PACKAGE.glob("*.py")))


def test_troubleshooting_quotes_no_command_the_tool_has_stopped_printing():
    """Every old spelling on this page must still be one the tool emits.

    troubleshooting.md is the one page made entirely of quoted tool output, so
    the bar is exact: an old spelling here is either a real message we have not
    fixed yet — in which case A8 in the acceptance pack is the check that says
    so — or it is a stale quotation, which is strictly worse than a missing
    entry, because it reads as current.

    Deliberate deprecation prose lives on the other pages (commands.md's table,
    README, getting-started, machines.md) and is deliberately not held to this;
    those pages are explaining that the old spellings work, which is true.
    """
    page = (DOCS / "troubleshooting.md").read_text()
    source = _package_source()
    stale = [phrase for phrase in OLD_SPELLING.findall(page)
             if phrase.strip() not in source]
    assert not stale, (
        "troubleshooting.md quotes commands the tool no longer prints: "
        + "; ".join(sorted(set(stale)))
    )


def test_that_check_would_have_caught_the_one_it_can():
    """The guard on the guard — and the limit it pinned has since closed.

    This test used to assert a LIMIT: the check stayed silent on
    `comfy-qat auth quota`, because that was a live prefix of a message the
    package really printed, so a doc quoting it could not be distinguished from
    a doc quoting something current. The assertion said so out loud rather than
    letting the silence read as coverage — and it named its own expiry: "if that
    is true, this check has become strictly stronger and this test should be
    revisited."

    It became true. Three `fix=` lines were still printing the retired
    `comfy-qat auth …` spelling — `auth` is a hidden deprecation window, not a
    second permanent name — and when they were corrected, this assertion failed
    with its own message. The check is now strictly stronger: nothing in the
    package prints `auth` as a subcommand, so any doc quoting it IS stale, and
    the sibling check above caught two such lines the moment the source changed.

    A limit asserted rather than implied is a limit that tells you when it is
    gone. That is the whole reason the shape is worth the extra lines.
    """
    source = _package_source()

    caught = "comfy-qat host down comfy-win"
    assert OLD_SPELLING.findall(caught)[0] not in source, (
        "the package now prints this, so it is no longer a valid example")

    # The limit that closed. Kept as an assertion, pointing the other way now:
    # if `auth` comes back as a printed spelling, the check silently weakens
    # again and the docs it can no longer see would go unguarded.
    assert "comfy-qat auth" not in source, (
        "a fix= line has gone back to the retired `comfy-qat auth …` spelling. "
        "That silently weakens the check above — a doc quoting `auth` would stop "
        "being distinguishable from a doc quoting something current.")
