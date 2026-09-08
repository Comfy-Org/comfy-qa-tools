"""A money sentence and the advice printed under it must agree.

`down` answers one question — am I still paying for anything — and it answers it
with a sentence followed, sometimes, by the way to stop paying. Nothing held the
two together, and a systematic census of `host.py` found six sentences that can
be inverted with the whole suite green. Four of them lie in the direction that
costs money:

    is left running, and it is billing        -> ...is not billing
    running and billing from now              -> stopped and not billing from now
    {host} is still running                   -> {host} is not running
    or look at what is running:               -> or look at what is stopped:

and two more over-report, which is safe and equally uncaught:

    was not running, so nothing was billing   -> was running, so it was billing
    nothing else is running, nothing to stop  -> everything else is running...

Each keeps its neighbours. The `billing` verdict of `down <name>` still printed

    comfy-qat down comfy-win   # stop the box, stop paying

three lines under "it is not billing", and `go --follow` still offered the same
command under "comfy-win is not running". **An inversion keeps every literal run
it had, so it stays documented under its old wording and no presence check can
see it.** Only reading a claim together with its own advice can.

**What the sentences have in common, and what this file is built on.** Whether a
GPU box is costing money is asserted three separate ways — by the word *billing*,
by whether the machine is said to be *running* or *stopped*, and by whether a way
to stop paying is offered at all. They are three readings of one fact, so they
must agree:

    (a) a paragraph that says it is OFF may not offer a way to stop paying;
    (b) a paragraph that says it is ON may not say there is nothing to stop;
    (c) a paragraph saying something IS or WAS billing must either stop it or
        say how;
    (d) no paragraph may say both, because a box that runs, bills.

Past tense and hedges make no claim about now and are exempt from (a) and (b) —
that is what keeps "was billing. Stopped." correct with no command under it, and
"it may have been billing" correct with `list --live` under it. (c) is what
separates "was billing. **Stopped.**" from a bare "it was billing", which reports
a bill and walks away.

**Nothing here is a list of sentences.** A list covers the cases its author had
in hand, which is how all six got through: the nine sentences pinned in
`tests/test_money_sentences.py` are a legitimate set and simply do not include
them. The verdicts come from `comfy_qa.host.VERDICTS` — the tuple whose own
comment says adding a fifth should be one edit and not a search — and `down` is
driven once per entry, so a fifth verdict is held to the rule the day it is added
and one that never reaches the sentence map fails as a KeyError.

**Two readers, because the sentences live in two places.** `down`'s are reachable
by running it, and are read off stdout. `move`'s, `go`'s, `switch --dry-run`'s and
`_probe_failed`'s need a relocation, a served ComfyUI or a capacity failure to
print, so those are read where they are written. Both apply the same three rules,
and both are pinned as non-vacuous: a reader that recognises nothing agrees with
everything.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from comfy_qa.config import Host
from comfy_qa.gcloud import GcloudError
from comfy_qa.host import VERDICTS, app
from comfy_qa.lifecycle import _raw_stop, stop_paying

HOSTS = """\
[hosts.local]
kind = "local"
port = 8188

[hosts.comfy-win]
kind         = "gce"
os           = "Windows Server 2022"
gpu          = "L4"
gce_instance = "comfy-win"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8190
"""

WIN = Host(name="comfy-win", kind="gce", os="Windows Server 2022", gpu="L4",
           gce_instance="comfy-win", gce_zone="us-central1-a",
           gce_project="proj", port=8190)


# --- reading a money paragraph ------------------------------------------------

# Tense, hedge and negation as WORDS. Each is a grammatical family rather than a
# transcription of what the tool says today, so a sentence written next month out
# of the same words is classified without editing this file.
_PAST = re.compile(r"\b(?:was|were|been|had)\b", re.I)
_HEDGE = re.compile(r"\b(?:may|might|could|cannot|unchecked|unknown|if|whether)\b", re.I)
_NEGATED = re.compile(r"\b(?:not|nothing|no|never|none)\b", re.I)

# The state of the machine, said in words rather than the word "billing" — which
# is how three of the six inversions dodged every check that looks for a bill. A
# box that is running is billing; one that is stopped is not.
_STOPPED = re.compile(
    r"\b(?:is|are) not running\b|\bnothing(?: else)? is running\b"
    r"|\b(?:is|are) stopped\b|\bwhat is stopped\b", re.I)
_RUNNING = re.compile(
    r"\b(?:is|are)(?: still| now)? running\b|\bleft running\b"
    r"|\b(?:is|are) up\b|\bwhat is running\b", re.I)

# "How to stop paying", in the two shapes it takes: a command, and a sentence
# telling you to stop. The commands are COMPUTED from the host rather than
# quoted, so renaming `down` cannot leave this looking for a string nothing
# prints. The lookbehind matters: "nothing to stop" is the OPPOSITE of an offer
# and contains the words of one.
_STOP_ADVICE = re.compile(
    r"(?<!nothing )\bto stop\b|\bstop (?:them|it|the box|the machine|paying)\b", re.I)
_NOTHING_TO_STOP = re.compile(r"\bnothing to stop\b", re.I)
# The other way a bill is settled: it was stopped, so there is nothing to offer.
_IT_STOPPED = re.compile(r"\bstopped\b", re.I)

BILLING_NOW = "says it IS billing"
NOT_BILLING_NOW = "says it is NOT billing"
WAS_BILLING = "says it WAS billing"
RUNNING_NOW = "says the machine IS running"
STOPPED_NOW = "says the machine is NOT running"

ON = {BILLING_NOW, RUNNING_NOW}
OFF = {NOT_BILLING_NOW, STOPPED_NOW}


def _clauses(paragraph: str) -> list[str]:
    """Sentences, then clauses — a negator binds to the clause it sits in.

    "nothing was running, so nothing is billing." is two claims about two
    different things, and only the second is about the bill. The em dash ends a
    clause as firmly as a comma: "started in us-central1-b and is billing — no
    move needed" carries a "no" that negates the move, not the bill.
    """
    parts: list[str] = []
    for sentence in re.split(r"(?<=[.;:])\s+|\n|—", paragraph):
        parts.extend(sentence.split(","))
    return [part for part in parts if part.strip()]


def _stance(paragraph: str) -> set[str]:
    """Everything this paragraph asserts about whether money is being spent."""
    read: set[str] = set()
    for clause in _clauses(paragraph):
        hedged = bool(_HEDGE.search(clause))
        if "billing" in clause.lower() and not hedged:
            if _NEGATED.search(clause):
                if not _PAST.search(clause):
                    read.add(NOT_BILLING_NOW)
            else:
                read.add(WAS_BILLING if _PAST.search(clause) else BILLING_NOW)
        if hedged or _PAST.search(clause):
            continue
        if _STOPPED.search(clause):
            read.add(STOPPED_NOW)
        elif _RUNNING.search(clause):
            read.add(RUNNING_NOW)
    return read


def _offers_a_way_to_stop(paragraph: str) -> bool:
    return bool(stop_paying(WIN) in paragraph or _raw_stop(WIN) in paragraph
                or _STOP_ADVICE.search(paragraph))


def disagreements(text: str, *, offers: bool, resolvable: bool = True) -> list[str]:
    """Every way this paragraph contradicts itself about the bill."""
    read = _stance(text)
    found = []
    if (read & ON) and (read & OFF):
        found.append(f"it {' and '.join(sorted(read & ON))} and in the same breath "
                     f"{' and '.join(sorted(read & OFF))} — a box that runs, bills")
    if (read & OFF) and offers:
        found.append(f"it {' and '.join(sorted(read & OFF))}, and then offers a "
                     f"way to stop paying — which only makes sense if it is")
    if (read & ON) and _NOTHING_TO_STOP.search(text):
        found.append(f"it {' and '.join(sorted(read & ON))}, and then says there "
                     f"is nothing to stop")
    if (resolvable and {BILLING_NOW, WAS_BILLING} & read
            and not offers and not _IT_STOPPED.search(text)):
        found.append("it reports a bill and neither stops it nor says how to")
    return found


def check_agreement(out: str, where: str) -> set[str]:
    """Hold every money paragraph in `out` to the three rules.

    The stance comes back so a caller can assert the corpus was not silent.
    """
    seen: set[str] = set()
    for paragraph in _paragraphs(out):
        seen |= _stance(paragraph)
        for problem in disagreements(paragraph, offers=_offers_a_way_to_stop(paragraph)):
            raise AssertionError(f"{where}: {problem}:\n{paragraph}")
    return seen


def _paragraphs(out: str) -> list[str]:
    """The command's own paragraphs: a money sentence and the advice under it.

    Every money sentence is written with a leading newline and its advice
    without one, so a blank line separates one verdict's claim from the next
    one's. That is what keeps the `down --all` summary and the undeclared-machines
    block from being read as each other's advice — the block offers `gcloud …
    stop` and says nothing about billing, correctly, because those are machines
    this tool will not touch.
    """
    return [block for block in re.split(r"\n\s*\n", out) if block.strip()]


# --- driving the commands -----------------------------------------------------


@pytest.fixture
def cli(tmp_path, monkeypatch):
    """The real CLI with a fake cloud; the money answer is read off stdout."""
    from comfy_qa import gcloud as gcloud_module, tunnel as tunnel_module

    monkeypatch.setattr(tunnel_module, "TUNNEL_DIR", tmp_path / "tunnels")

    def invoke(*args, cloud=None):
        path = tmp_path / "hosts.toml"
        path.write_text(HOSTS, encoding="utf-8")
        monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: cloud or Cloud())
        return CliRunner().invoke(app, [*args, "--config", str(path)])

    return invoke


class Cloud:
    """Enough gcloud for `down` to reach its summary, and nothing more."""

    def __init__(self, status="RUNNING"):
        self.status = status

    def instance_status(self, instance, zone, project):
        if isinstance(self.status, Exception):
            raise self.status
        return self.status

    def stop_instance(self, instance, zone, project):
        return ""

    def list_instances(self, project):
        # No undeclared machines, so every paragraph in the output belongs to the
        # money summary. The undeclared block is its own paragraph either way.
        return []

    def __getattr__(self, name):
        def unexpected(*args, **kwargs):
            raise AssertionError(f"{name} was not expected here")
        return unexpected


# --- 1. every verdict `down <name>` can reach ---------------------------------


@pytest.mark.parametrize("verdict", VERDICTS)
def test_each_down_verdict_agrees_with_the_advice_under_it(cli, monkeypatch, verdict):
    """Driven once per verdict the tool names, not once per sentence written here.

    `put_away` is replaced by its answer, because the answer is the whole input to
    the sentence being checked and building four cloud states to reach four
    strings tests gcloud rather than the wording. The four sentences live in a
    dict indexed by the verdict, which is why they are covered here and not by
    the source reader below: their advice sits in a separate `if`, and only the
    run knows which sentence went with which branch.
    """
    from comfy_qa import lifecycle

    monkeypatch.setattr(lifecycle, "put_away", lambda *a, **k: verdict)
    result = cli("down", "comfy-win")

    assert result.exit_code == 0, result.output
    assert "comfy-win" in result.stdout, (
        f"the {verdict!r} verdict printed no money answer at all on stdout"
    )
    check_agreement(result.stdout, f"down comfy-win ({verdict})")


def test_the_verdicts_are_read_from_the_tool_and_not_from_here():
    """The parametrize above is only a guard while it is the tool's own list."""
    assert VERDICTS, "comfy_qa.host.VERDICTS is empty; the check above covers nothing"


# --- 2. the `down --all` summary ----------------------------------------------

# Each is a real run of the real branch, named by the state the cloud is in.
ALL_RUNS = {
    "everything running, stopped": (["down", "--all"], "RUNNING"),
    "nothing running, stopped": (["down", "--all"], "TERMINATED"),
    "nothing readable, stopped": (["down", "--all"], GcloudError("no answer")),
}

# There were six. The other three were `--all --keep-running`, the branch that
# closed every tunnel and deliberately left every machine billing, and they are
# gone with the flag. That branch is where this file's rule was hardest to keep
# — a summary about money written under a flag whose whole job was to leave the
# money running — so it is worth saying out loud that the three cases left are
# the three that still exist, and not a narrowing of what is checked.


@pytest.mark.parametrize("case", sorted(ALL_RUNS))
def test_the_down_all_summary_agrees_with_its_own_advice(cli, case):
    """`--all` writes the same claim about several machines, so it has the same
    rule to keep."""
    args, status = ALL_RUNS[case]
    result = cli(*args, cloud=Cloud(status=status))

    assert result.exit_code == 0, result.output
    check_agreement(result.stdout, f"down --all ({case})")


# --- 3. the same rule over the source -----------------------------------------

# `move`'s money line, `go`'s two exit lines, `switch --dry-run`'s plan and
# `_probe_failed`'s fix all sit behind a relocation, a served ComfyUI or a
# capacity failure, and building one to read a sentence tests gcloud rather than
# the wording. So they are read where they are written.
#
# `stop_paying`, `_raw_stop` and `_with_the_bill` ARE the offer — the last is
# named for it: "a fix that ends by saying how to stop paying". A paragraph that
# calls any of them offers a way to stop, so they are looked for as calls rather
# than as text.
OFFERING_HELPERS = {"_with_the_bill", "stop_paying", "_raw_stop"}

# A message and its own remedy in one node. `say.result` has no such pairing —
# its advice is the next statement — so those are grouped by paragraph instead.
CARRIES_ITS_OWN_FIX = {"LifecycleError", "error", "fail", "warn"}

SOURCE = Path(__file__).resolve().parent.parent / "comfy_qa"


def _literals(node: ast.AST) -> str:
    """Every string this expression is built out of, in order."""
    return " ".join(part.value for part in ast.walk(node)
                    if isinstance(part, ast.Constant) and isinstance(part.value, str))


def _is_an_offer(node: ast.AST) -> bool:
    return (any(isinstance(part, ast.Call)
                and ast.unparse(part.func).split(".")[-1] in OFFERING_HELPERS
                for part in ast.walk(node))
            or bool(_STOP_ADVICE.search(_literals(node))))


def _a_result_call(statement: ast.stmt) -> ast.Call | None:
    if (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
            and ast.unparse(statement.value.func) == "say.result"):
        return statement.value
    return None


def _written_paragraphs() -> list[tuple[str, str, bool]]:
    """(where, text, offers a way to stop) for each money paragraph in the source.

    A paragraph is a run of consecutive `say.result` calls, starting again at each
    one whose text opens with a newline — the command's own convention, the same
    one the runs above read off stdout. Only `say.result`: `warn`, `step` and
    `detail` are the story on stderr, and these rules are about the answer.

    A call that PICKS a sentence is handled two ways. `A if cond else B` is two
    alternative paragraphs and each is judged alone — reading them together made
    `switch --dry-run` look as though it said a thing and its opposite. A dict
    indexed by a verdict is skipped entirely: its advice is in a separate branch,
    so nothing here can pair them, and those are driven for real above.
    """
    found: list[tuple[str, str, bool]] = []
    for path in sorted(SOURCE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if isinstance(block, list):
                    found.extend(_paragraphs_in(path.name, block))
    return found


def _paragraphs_in(where: str, block: list[ast.stmt]) -> list[tuple[str, str, bool]]:
    found: list[list] = []
    current: list | None = None
    for statement in block:
        call = _a_result_call(statement)
        if call is None or (call.args and isinstance(call.args[0], ast.Subscript)):
            current = None
            continue
        picked = call.args[0] if call.args else None
        if isinstance(picked, ast.IfExp):
            for arm in (picked.body, picked.orelse):
                found.append([f"{where}:{call.lineno}", _literals(arm), _is_an_offer(call)])
            current = None
            continue
        text = _literals(call)
        if text.startswith("\n") or current is None:
            current = [f"{where}:{call.lineno}", text, _is_an_offer(call)]
            found.append(current)
        else:
            current[1] += " " + text
            current[2] = current[2] or _is_an_offer(call)
    return [(a, b, c) for a, b, c in found]


def _fix_carrying_calls() -> list[tuple[str, str, bool]]:
    """(where, text, offers a way to stop) for each message that carries its fix."""
    found = []
    for path in sorted(SOURCE.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            if ast.unparse(node.func).split(".")[-1] not in CARRIES_ITS_OWN_FIX:
                continue
            text = _literals(node)
            if not ("billing" in text.lower() or _STOPPED.search(text)
                    or _RUNNING.search(text)):
                continue
            found.append((f"{path.name}:{node.lineno}", text, _is_an_offer(node)))
    return found


def test_every_money_paragraph_in_the_source_agrees_with_itself():
    """The rule, applied where the sentences are written rather than printed.

    This is what covers `move`'s "running and billing from now", `go --follow`'s
    "{host} is still running" over a `stop_paying` line, and `switch --dry-run`'s
    "nothing else is running, so nothing to stop".
    """
    for where, text, offers in _written_paragraphs():
        for problem in disagreements(text, offers=offers):
            raise AssertionError(f"{where}: {problem}:\n{text}")


def test_every_message_agrees_with_the_fix_it_carries():
    """The same, for a message and its own `fix=` — one node, both halves.

    This is what covers `_probe_failed`'s "or look at what is running:" standing
    over `gcloud compute instances stop`: invert the label and the fix tells you
    to look at what is stopped and then to stop something.

    Rule (c) is not applied here. A failure's remedy is legitimately something
    other than stopping — sign in, install a driver, try another zone — and
    saying a box is billing while telling you to fix the driver is correct.
    """
    for where, text, offers in _fix_carrying_calls():
        for problem in disagreements(text, offers=offers, resolvable=False):
            raise AssertionError(f"{where}: {problem}:\n{text}")


# --- 4. none of the readers is agreeing with everything -----------------------


def test_both_kinds_of_claim_are_actually_read(cli, monkeypatch):
    """A rule that never recognises a claim passes anything.

    So the corpus is required to contain both halves of it: a run that says
    something IS billing, and a run that says nothing is. If a rewording makes
    every sentence unreadable to `_stance`, this fails while the checks above
    quietly stop checking.
    """
    from comfy_qa import lifecycle

    seen: set[str] = set()
    for args, status in ALL_RUNS.values():
        seen |= check_agreement(cli(*args, cloud=Cloud(status=status)).stdout, "corpus")
    for verdict in VERDICTS:
        monkeypatch.setattr(lifecycle, "put_away", lambda *a, **k: verdict)
        seen |= check_agreement(cli("down", "comfy-win").stdout, "corpus")

    assert BILLING_NOW in seen, "no run said anything IS billing; the rule read nothing"
    assert WAS_BILLING in seen, "no run reported a finished bill; the rule read nothing"
    # NOT_BILLING_NOW is deliberately not required here any more, and this is a
    # LOSS being recorded rather than a bar being relaxed to fit. The only
    # present-tense "nothing is billing" this tool ever printed was the closing
    # line of `down --all --keep-running`, and the flag has been removed. What is
    # left says it in the past — "nothing was running, so nothing was billing" —
    # which is a different claim and is read as one.
    #
    # It is not a hole in the READER: `test_the_reader_tells_the_six_inversions_
    # from_what_is_written` still drives NOT_BILLING_NOW through `disagreements`
    # on written sentences, so the rule that fires on it is still exercised. What
    # is gone is the corpus evidence, and if a command starts saying it again
    # this line should come back.


def test_the_source_readers_find_the_claims_that_are_there():
    """Non-vacuity for the two readers above, which would otherwise pass on a
    walker that has stopped recognising `say.result` — or the whole package."""
    paragraphs = _written_paragraphs()
    assert len(paragraphs) > 50, "far too few money paragraphs found; the reader is broken"
    assert len(_fix_carrying_calls()) > 15, "far too few messages with a fix; likewise"

    read = {claim for _, text, _ in paragraphs + _fix_carrying_calls()
            for claim in _stance(text)}
    # Four, not five. `NOT_BILLING_NOW` left the package with `down
    # --keep-running`; see the note in the test above. Lowered deliberately, in
    # the commit that removed the sentence, rather than found later as drift.
    assert read >= {BILLING_NOW, WAS_BILLING, RUNNING_NOW, STOPPED_NOW}, (
        f"the source readers found only {sorted(read)}; every kind of claim the "
        f"rules turn on must be one they can see"
    )


def test_the_reader_tells_the_six_inversions_from_what_is_written():
    """The classifier itself, on each sentence the census inverted and on its
    inversion — so a change that makes it answer the same thing every time fails
    here rather than leaving a silent hole above."""
    # (a) the four that cost money, each said to be OFF over an offer to stop.
    for said in ("comfy-win is left running, and it is not billing.",
                 "comfy-win is now in us-central1-b, stopped and not billing from now.",
                 "ComfyUI exited (1). comfy-win is not running.",
                 "or look at what is stopped:"):
        assert disagreements(said, offers=True, resolvable=False), f"missed: {said}"
        # ... and each is silent when it says the true thing.
    for said in ("comfy-win is left running, and it is billing.",
                 "comfy-win is now in us-central1-b, running and billing from now.",
                 "ComfyUI exited (1). comfy-win is still running.",
                 "or look at what is running:"):
        assert not disagreements(said, offers=True, resolvable=False), f"false alarm: {said}"
    # (b) ON over "nothing to stop", and (c) a bill reported and abandoned.
    assert disagreements("  - everything else is running, so nothing to stop", offers=False)
    assert not disagreements("  - nothing else is running, so nothing to stop", offers=False)
    assert disagreements("comfy-win was running, so it was billing.", offers=False)
    assert not disagreements("comfy-win was not running, so nothing was billing.", offers=False)
    # Past tense and hedges claim nothing about now, in either direction.
    assert not disagreements("comfy-win was billing. Stopped.", offers=False)
    assert not disagreements("it may have been billing.", offers=False)


def test_the_offer_reader_tells_advice_from_a_full_stop():
    """"Stopped." is a report, not an offer, and "nothing to stop" is its
    opposite while containing every word of one."""
    assert _offers_a_way_to_stop(f"  {stop_paying(WIN)}   # stop the box, stop paying")
    # Prose rather than a command, which the reader has to recognise as an offer
    # just the same. The tool's own example of this was `down --all
    # --keep-running`'s "Run without --keep-running to stop them.", which went
    # with the flag; the sentence is kept here because the READER is what is
    # under test, and it must still recognise advice that names no command.
    assert _offers_a_way_to_stop("Stop them when the work is finished.")
    assert _offers_a_way_to_stop(f"  {_raw_stop(WIN)}")
    assert not _offers_a_way_to_stop("comfy-win was billing. Stopped.")
    assert not _offers_a_way_to_stop("  comfy-qat list --live")
    assert not _offers_a_way_to_stop("  - nothing else is running, so nothing to stop")


# --- 5. omission: a failure must say what its own step may have created -------

# Observed on real hardware, not mutated into existence. `move comfy-linux --to
# us-central1-b` on a 200 GB disk exceeded the tool's 300s gcloud timeout inside
# the snapshot step, and printed:
#
#     the move stopped at: snapshot the boot disk comfy-linux ...
#         (gcloud timed out after 300s: compute disks snapshot ...)
#         comfy-linux is untouched in us-central1-c
#     to fix: run the same command again ...
#
# Every word true, and Google was holding `comfy-linux-move  200 GB  UPLOADING`
# at that moment. **The client timed out; the operation did not stop.** The
# message reports on the instance and is silent about the object the step it just
# named had already brought into existence. The next `move` run does say so,
# prominently, with the delete command — and that is exactly the argument that
# would keep this message wrong forever.
#
# **This is not a contradiction, so nothing above can see it.** Rules (a) to (d)
# compare claims a paragraph makes; this paragraph makes no false claim. It omits
# one. So the fifth rule is about what a message must contain rather than what it
# must not.
#
# **The tool has already written the reasoning down, once.** `_inflight` — the
# Ctrl-C path — counts the step in progress as done, and says why: *"the request
# has reached Google by then, and Ctrl-C reaches only the local gcloud, so
# assuming it did NOT happen is the assumption that costs money."* `_stopped` —
# the step-failed path — is handed the same `done` list without the step in
# progress. Two paths, the same doubt about the same resource, and only one of
# them counts it. A timeout arrives as a `GcloudError`, so it takes the path that
# does not.
#
# Nothing here lists which steps create something. The action kinds come from
# `Plan.actions`, and what each one leaves comes from `_state_after`, so a step
# added later is held to this the day it exists.


def _a_plan():
    from comfy_qa.relocate import Found, Plan

    return Plan(
        host=Host(name="comfy-linux", kind="gce", os="Ubuntu", gpu="L4",
                  gce_instance="comfy-linux", gce_zone="us-central1-c",
                  gce_project="proj", port=8190),
        to_zone="us-central1-b",
        source_disk="comfy-linux",
        new_instance="comfy-linux-b",
        new_disk="comfy-linux-b",
        snapshot="comfy-linux-move",
        machine_type="g2-standard-8",
    ), Found()


def _creating_steps():
    """Every step of a move that leaves something behind, asked of the code.

    A step "creates" when `_state_after` lists something once that step is
    counted as done and nothing else is. That is the tool's own definition of
    what a step leaves, so `SNAPSHOT`, the disk and the instance qualify without
    being named here, and a fourth one would qualify on the day it is written.
    """
    from comfy_qa.relocate import DELETING, _state_after

    plan, found = _a_plan()
    steps = []
    for action in plan.actions(found):
        if action.kind in DELETING:
            continue
        left, _ = _state_after(plan, found, [action.kind])
        if left:
            steps.append((action, left))
    return steps


def test_the_creating_steps_are_found_in_the_code_and_not_listed_here():
    """Non-vacuity: if `Plan.actions` or `_state_after` stops answering, the
    check below covers nothing and this says so rather than passing."""
    steps = _creating_steps()
    assert len(steps) >= 3, (
        f"only {len(steps)} creating steps found; a move creates a snapshot, a "
        f"disk and an instance, so the derivation has broken"
    )


def _what_the_failure_said(action, exc):
    from comfy_qa.relocate import _stopped

    plan, found = _a_plan()
    reported = _stopped(plan, found, [], action, exc)
    return "\n".join((str(reported), *reported.left, *reported.cleanup))


@pytest.mark.parametrize("kind", [a.kind for a, _ in _creating_steps()])
def test_a_step_that_ran_out_of_clock_reports_what_it_may_have_created(kind):
    """The two paths out of a step in progress must name the same resources.

    An interrupt inside a step and a timeout inside a step face the same doubt:
    the request reached Google, and neither Ctrl-C nor a client-side clock tells
    you what Google did with it. `_inflight` resolves that doubt towards "it may
    exist", and says why. This asserts `_stopped` resolves it the same way,
    because a 200 GB snapshot that is UPLOADING does not care which of the two
    happened.

    The kind is `TIMEOUT` because that is the shape the real failure took, and
    because gcloud's own vocabulary calls it the one that "reached Google, or did
    not, but ran out of clock".
    """
    from comfy_qa.gcloud import TIMEOUT
    from comfy_qa.relocate import _inflight

    plan, found = _a_plan()
    action, expected = next((a, e) for a, e in _creating_steps() if a.kind == kind)
    timed_out = GcloudError(
        f"gcloud timed out after 300s: compute disks snapshot {plan.source_disk}",
        kind=TIMEOUT)

    may_exist, _ = _inflight(plan, found, [], action)
    said = _what_the_failure_said(action, timed_out)

    for item in expected:
        assert item in may_exist, (
            f"the interrupt path stopped naming {item!r}; this test is measuring "
            f"against it, so fix that first"
        )
        assert item in said, (
            f"a move that times out inside `{action.kind}` says nothing about "
            f"{item!r}, which that very step may already have created on Google's "
            f"side. The interrupt path names it. What the failure said was:\n{said}"
        )


@pytest.mark.parametrize("kind", [a.kind for a, _ in _creating_steps()])
def test_a_step_that_was_refused_does_not_invent_what_it_did_not_create(kind):
    """The other half, and the reason the rule above is keyed on the kind.

    A refusal is an answer. gcloud saying "quota exceeded" means Google looked and
    made nothing, and naming a snapshot that does not exist is how a tool stops
    being believed about money — `put_away` has the same sentence written into it
    from the day it over-reported four boxes. So this pins the direction the fix
    must NOT drift in: only an unresolved outcome may be counted as maybe-there.
    """
    from comfy_qa.gcloud import QUOTA
    from comfy_qa.relocate import _state_after

    plan, found = _a_plan()
    action, invented = next((a, e) for a, e in _creating_steps() if a.kind == kind)
    refused = GcloudError("quota exceeded", raw="ERROR: quota exceeded", kind=QUOTA)

    already, _ = _state_after(plan, found, [])
    said = _what_the_failure_said(action, refused)
    for item in invented:
        if item in already:
            continue  # something an earlier step really did make
        assert item not in said, (
            f"a move REFUSED inside `{action.kind}` claims {item!r} exists. Google "
            f"answered, and the answer was no. What it said was:\n{said}"
        )
