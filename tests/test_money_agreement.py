"""A money sentence and the advice printed under it must agree.

`down` answers one question — am I still paying for anything — and it answers it
with a verdict sentence followed, sometimes, by the way to stop paying. Nothing
held the two together. Inverting the `billing` verdict of `down <name>` to

    f"\n{host.name} is left running, and it is not billing."

left the whole suite green, and three lines below it the same command still
printed

    comfy-qat down comfy-win   # stop the box, stop paying

— the box is not billing, and here is how to stop it billing, in consecutive
lines. The same inversion in the other direction survived too: `down --all
--keep-running`'s "nothing was running, so nothing is billing." became "…so
everything is billing." and nothing objected, because nothing anywhere read a
money sentence and its own follow-up together.

**This file does not hold a list of sentences.** A list covers the cases its
author had in hand, which is how both of those got through: the nine sentences
pinned in `tests/test_money_sentences.py` are a different and legitimate set, and
simply do not include these two. What is written here is a rule, applied to
whatever the command actually prints:

    Within one money paragraph:
      a present-tense claim that something IS billing must be accompanied by a
      way to stop paying;
      a present-tense claim that nothing IS billing must NOT be.

Past tense is exempt in both directions and deliberately so — "was billing.
Stopped." is the correct thing to say with no stop command under it, and "nothing
was billing" is correct with none either. So is a hedge: "may have been billing"
claims nothing, and its advice is `list --live`, which is how to find out rather
than how to stop.

The verdicts are not transcribed. `down`'s single-host form is driven once per
entry in `comfy_qa.host.VERDICTS` — the tuple whose own comment says adding a
fifth should be one edit and not a search — so a fifth verdict is held to this
rule the day it is added, and a fifth verdict that never reaches the sentence map
fails here as a KeyError rather than as silence.

A paragraph is what the command's own formatting already marks out: every money
sentence is written with a leading newline and its advice without one, so a blank
line separates one verdict's claim from the next one's. That is what keeps the
`down --all` summary and the undeclared-machines block from being read as each
other's advice — the block offers `gcloud compute instances stop` and says
nothing about billing, correctly, because those are machines this tool will not
touch.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from comfy_qa.gcloud import GcloudError
from comfy_qa.host import VERDICTS, app
from comfy_qa.config import Host
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

# Tense, hedge and negation, as words rather than as sentences. Each list is a
# grammatical family, not a transcription of what the tool says today: a new
# sentence built out of these words is classified without editing this file.
_PAST = re.compile(r"\b(?:was|were|been|had)\b", re.I)
_HEDGE = re.compile(r"\b(?:may|might|could|cannot|unchecked|unknown|if)\b", re.I)
_NEGATED = re.compile(r"\b(?:not|nothing|no|never|none)\b", re.I)

# "how to stop paying", in the two shapes it takes: a command, and a sentence
# telling you to stop. The commands are COMPUTED from the host rather than
# quoted, so renaming `down` or changing `_raw_stop`'s flags cannot leave this
# looking for a string nothing prints any more.
_STOP_ADVICE = re.compile(
    r"\bto stop\b|\bstop (?:them|it|the box|the machine|paying)\b", re.I)

AFFIRMED = "says something IS billing"
DENIED = "says nothing IS billing"


def _paragraphs(out: str) -> list[str]:
    """The command's own paragraphs: a money sentence and the advice under it."""
    return [block for block in re.split(r"\n\s*\n", out) if block.strip()]


def _clauses(paragraph: str) -> list[str]:
    """Sentences, then clauses — a negator binds to the clause it sits in.

    "nothing was running, so nothing is billing." is two claims about two
    different things, and only the second one is about the bill.
    """
    parts: list[str] = []
    # The em dash ends a clause as firmly as a comma does here — "started in
    # us-central1-b and is billing — no move needed" carries a "no" that negates
    # the move and not the bill, and reading it as one would let the tool say a
    # running box is not billing.
    for sentence in re.split(r"(?<=[.;:])\s+|\n|—", paragraph):
        parts.extend(sentence.split(","))
    return [part for part in parts if part.strip()]


def _present_claims(paragraph: str) -> set[str]:
    """What this paragraph asserts about the bill RIGHT NOW, if anything."""
    claims = set()
    for clause in _clauses(paragraph):
        if "billing" not in clause.lower():
            continue
        if _HEDGE.search(clause) or _PAST.search(clause):
            continue  # claims nothing about now: a guess, or a finished state
        claims.add(DENIED if _NEGATED.search(clause) else AFFIRMED)
    return claims


def _offers_a_way_to_stop(paragraph: str, hosts: tuple[Host, ...] = (WIN,)) -> bool:
    """Does this paragraph hand over a way to stop the bill?"""
    commands = [command(host) for host in hosts for command in (stop_paying, _raw_stop)]
    return (any(command in paragraph for command in commands)
            or bool(_STOP_ADVICE.search(paragraph)))


def check_agreement(out: str, where: str) -> set[str]:
    """Hold every money paragraph to the rule; return the claims it made.

    The claims come back so a caller can assert the corpus was not silent. A
    checker that reads nothing agrees with everything.
    """
    seen: set[str] = set()
    for paragraph in _paragraphs(out):
        claims = _present_claims(paragraph)
        seen |= claims
        offered = _offers_a_way_to_stop(paragraph)
        if AFFIRMED in claims:
            assert offered, (
                f"{where}: this paragraph {AFFIRMED} and gives no way to stop "
                f"paying:\n{paragraph}"
            )
        elif DENIED in claims:
            assert not offered, (
                f"{where}: this paragraph {DENIED} and then offers a way to stop "
                f"paying, which only makes sense if it is:\n{paragraph}"
            )
    return seen


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
        # money summary. The undeclared block is covered separately, below.
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
    strings tests gcloud rather than the wording. A verdict added to `VERDICTS`
    and forgotten in the sentence map arrives here as a KeyError.
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
    "everything running, kept up": (["down", "--all", "--keep-running"], "RUNNING"),
    "nothing running, stopped": (["down", "--all"], "TERMINATED"),
    "nothing running, kept up": (["down", "--all", "--keep-running"], "TERMINATED"),
    "nothing readable, stopped": (["down", "--all"], GcloudError("no answer")),
    "nothing readable, kept up": (["down", "--all", "--keep-running"],
                                  GcloudError("no answer")),
}


@pytest.mark.parametrize("case", sorted(ALL_RUNS))
def test_the_down_all_summary_agrees_with_its_own_advice(cli, case):
    """`--all` writes the same claim about several machines, so it has the same
    rule to keep. Its billing paragraph offers "run without --keep-running"
    rather than a command, which is a way to stop paying and is read as one."""
    args, status = ALL_RUNS[case]
    result = cli(*args, cloud=Cloud(status=status))

    assert result.exit_code == 0, result.output
    check_agreement(result.stdout, f"down --all ({case})")


# --- 3. the checker is not agreeing with everything ---------------------------


def test_both_kinds_of_claim_are_actually_read(cli, monkeypatch):
    """A rule that never recognises a claim passes anything.

    So the corpus is required to contain both halves of it: a run that says
    something IS billing, and a run that says nothing is. If a rewording makes
    every sentence unreadable to `_present_claims`, this fails while the checks
    above quietly stop checking.
    """
    from comfy_qa import lifecycle

    seen: set[str] = set()
    for args, status in ALL_RUNS.values():
        seen |= check_agreement(cli(*args, cloud=Cloud(status=status)).stdout, "corpus")
    for verdict in VERDICTS:
        monkeypatch.setattr(lifecycle, "put_away", lambda *a, **k: verdict)
        seen |= check_agreement(cli("down", "comfy-win").stdout, "corpus")

    assert AFFIRMED in seen, "no run said anything IS billing; the rule read nothing"
    assert DENIED in seen, "no run said nothing IS billing; the rule read nothing"


def test_the_claim_reader_tells_the_two_apart():
    """The classifier itself, on the two shapes the tool writes and their
    inversions — so a change that makes it answer the same thing every time is a
    failure here rather than a silent hole above."""
    assert _present_claims("comfy-win is left running, and it is billing.") == {AFFIRMED}
    assert _present_claims("comfy-win is left running, and it is not billing.") == {DENIED}
    assert _present_claims("nothing was running, so nothing is billing.") == {DENIED}
    assert _present_claims("nothing was running, so everything is billing.") == {AFFIRMED}
    # Past and hedge claim nothing about now, in either direction.
    assert _present_claims("comfy-win was billing. Stopped.") == set()
    assert _present_claims("it may have been billing.") == set()


def test_the_offer_reader_tells_advice_from_a_full_stop():
    """"Stopped." is a report, not an offer, and reading it as one would make the
    `caught` verdict fail for saying the true thing."""
    assert _offers_a_way_to_stop(f"  {stop_paying(WIN)}   # stop the box, stop paying")
    assert _offers_a_way_to_stop("Run without --keep-running to stop them.")
    assert _offers_a_way_to_stop(f"  {_raw_stop(WIN)}")
    assert not _offers_a_way_to_stop("comfy-win was billing. Stopped.")
    assert not _offers_a_way_to_stop("  comfy-qat list --live")


# --- 4. the same rule over the source, for commands not driven above ----------

# `move`'s money line and the "already where you wanted it" line in `_offer_move`
# both sit behind a relocation, and building one to read two sentences tests
# gcloud rather than the wording. So they are read where they are written.
#
# `stop_paying`, `_raw_stop` and `_with_the_bill` ARE the offer — the last one is
# named for it: "a fix that ends by saying how to stop paying". A paragraph that
# calls any of them offers a way to stop, whatever the surrounding prose says, so
# they are looked for as calls rather than as text.
OFFERING_HELPERS = {"_with_the_bill", "stop_paying", "_raw_stop"}

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


def _written_paragraphs() -> list[tuple[str, str, bool]]:
    """(where, text, offers a way to stop) for each money paragraph in the source.

    A paragraph is a run of consecutive `say.result` calls, starting again at each
    one whose text opens with a newline — the command's own convention, the same
    one the runs above read off stdout. Only `say.result`: `warn`, `step` and
    `detail` are the story on stderr, and the rule here is about the answer.

    A call that SELECTS a sentence — the verdict map — is skipped, because its
    four sentences are four paragraphs and reading them together says nothing
    about any of them. Those are driven for real, one verdict at a time, above.
    """
    found: list[tuple[str, str, bool]] = []
    for path in sorted(SOURCE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                current: list | None = None
                for statement in block:
                    call = _a_result_call(statement)
                    if call is None or (call.args and isinstance(call.args[0], ast.Subscript)):
                        current = None
                        continue
                    text = _literals(call)
                    if text.startswith("\n") or current is None:
                        current = [f"{path.name}:{call.lineno}", text, _is_an_offer(call)]
                        found.append(current)  # type: ignore[arg-type]
                    else:
                        current[1] += " " + text
                        current[2] = current[2] or _is_an_offer(call)
    return [(where, text, offer) for where, text, offer in found]


def _a_result_call(statement: ast.stmt) -> ast.Call | None:
    if (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
            and ast.unparse(statement.value.func) == "say.result"):
        return statement.value
    return None


def test_every_money_paragraph_in_the_source_agrees_with_itself():
    """The rule, applied where the sentences are written rather than printed.

    This is what covers `move` — "is now in us-central1-b, running and billing
    from now" and the `stop_paying` line under it — and `_offer_move`'s "started
    in <zone> and is billing — no move needed", neither of which is reachable
    without faking a relocation.
    """
    for where, text, offers in _written_paragraphs():
        claims = _present_claims(text)
        if AFFIRMED in claims:
            assert offers, f"{where}: {AFFIRMED} and gives no way to stop paying:\n{text}"
        elif DENIED in claims:
            assert not offers, (
                f"{where}: {DENIED} and then offers a way to stop paying:\n{text}")


def test_the_source_reader_finds_the_claims_that_are_there():
    """Non-vacuity for the check above, which would otherwise pass on a reader
    that has stopped recognising `say.result` — or the whole of `comfy_qa`."""
    paragraphs = _written_paragraphs()
    assert len(paragraphs) > 50, "far too few money paragraphs found; the reader is broken"
    claimed = {claim for _, text, _ in paragraphs for claim in _present_claims(text)}
    assert claimed == {AFFIRMED, DENIED}, (
        f"the source reader found {claimed or 'no claims at all'}; it must find both"
    )
