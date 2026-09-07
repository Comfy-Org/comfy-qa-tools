# Tests that cannot fail

Seven shapes a test in this repo has taken that made it **incapable of failing**.
Not slow, not flaky, not weak — incapable. Each one was written by somebody who
was sure, each was green, and each was found later by accident.

This page exists because they were found one at a time, by different people, and
the knowledge lived in commit messages nobody reads twice. Without it the next
person pays full price again — including a version of us with no memory of the
day these were found.

**Read the last two sections before the list.** The shapes are useful; the two
observations at the end are the reason the list is not a checklist.

---

## 1. A substring match over a whole file

The assertion reads the entire file and asks whether some text appears anywhere
in it.

```python
assert f"comfy-qat {command}" in README.read_text()
```

**The tell:** the assertion's subject is a whole document rather than the part of
it that means something.

Text from somewhere else satisfies it. A sentence saying a command does *not*
exist passes exactly as well as a row documenting one — and two commands reached
the README's own "still to come" list while shipping, with this green. Scoping it
to table rows is the fix, because the table is where a reader looks:

```python
rows = [line for line in README.read_text().splitlines()
        if line.lstrip().startswith("|")]
assert any(f"comfy-qat {command}" in row for row in rows)
```

The guard on that fix is a test proving prose cannot satisfy it.

## 2. A hand-maintained list nothing derives

A tuple or set naming the things to check, updated by remembering.

**The tell:** a collection literal at the top of a test file, and no code that
produces it.

It fails open in **two directions**, and this repo has now had one of each:

- `ERROR_TYPES` silently **dropped** two members, so a whole module's messages
  stopped being required and nothing said so.
- `BILLABLE_ENDINGS` could silently **gain** one: a new command that leaves a
  box running, never added to the list, is simply not checked. That is exactly
  how `disconnect` went unnoticed — the test that would have caught it was
  written for two other commands.

Derive the membership from the source instead, so a new member cannot arrive
without either satisfying the rule or failing the test. **Then read section 7,
because that fix has its own shape.**

## 3. A test that vanishes rather than fails

The test is parametrised over its own subject, so removing the subject removes
the test rather than failing it.

**The tell:** a count that drops with zero failures — or, worse, one that *rises*
while work is lost.

`tests/test_stamp_hostile.py` — 40 tests, the only cover over the redirect
refusal, `looks_like_comfyui`, `MAX_BODY` and every no-traceback path in `fetch`
— was deleted by a merge and nobody noticed for three commits. Git saw "deleted
by us, unmodified by them" and resolved it silently. No conflict, no warning.

What hid it is the part worth internalising: **the total went up.** 841 to 869
across the merge that dropped those 40, because 28 new ones arrived in the same
commit. Every habit anyone has for noticing lost work — the number is bigger, the
suite is green, the diff is mostly additions — reported success.

A rising count is not evidence that nothing went missing, and neither is a green
run: **deleted tests do not fail.** `tests/test_suite_integrity.py` asserts the
file list directly, which is a hand-maintained list (section 2) accepted
deliberately, because the failure it catches is one no amount of reading the
numbers will.

This shape has been found three separate times, most recently inside a commit
written to eliminate it.

## 4. An instrument with only one branch

A detector that can report one outcome. The mutation detector that could only
report survival is the pure case: it never had a code path for "killed", so
every mutant looked like a survivor and the sweep was meaningless.

**The tell:** ask what the instrument prints when the thing it looks for is
*absent*. If you cannot point at that branch, it does not have one.

### The six characters that cost the most

**`xfailed` contains `failed`.**

Any shell test matching the bare substring — `grep failed`,
`case "$out" in *failed*)`, `[[ $out == *failed* ]]` — matches a run in which
**nothing failed**, because pytest's summary always ends `N passed, M xfailed`.
It reports failure on a green suite; invert the habit and it reports success on a
red one.

Both instances were mutation sweeps, where the entire method is "delete the fix
and watch the count move" — so a result-reader that always says the same thing
voids the whole run rather than one case of it. The first void'd a full day of
mutation results. The second cost a second sweep, in a different language,
against the same six characters.

Neither was a Python problem. Both were reading pytest's own `-q` summary in a
shell. Match the anchored, uppercase form pytest prints one per failure:

```sh
grep -c '^FAILED tests/'
```

or read the integers out of the summary. **Never the bare word.**

It has now been demonstrated on a real run rather than argued: a summary of
`3859 passed, 66 xfailed`, exit code 0, zero `^FAILED` lines — and
`'failed' in summary` is `True`. A fully green suite that a substring match
calls a failure.

### The same trap with the sign flipped

**`passed` is a substring of `xpassed`.** This one has bitten nobody yet, and is
written down *because* nobody has met it.

`xfail_strict = true` is set here, so an `xfail` that unexpectedly passes is
reported as `FAILED` with `[XPASS(strict)]` — it lands in the `^FAILED` lines,
where the recommended grep finds it. That is why `xpassed` does not appear in
this repo today. It reappears the moment somebody writes `strict=False`, which
is exactly the escape hatch a future maintainer reaches for when an xfail is
inconvenient. Verified directly:

```
$ pytest test_xp.py -q      # one strict xfail that passes, one non-strict
FAILED test_xp.py::test_passes_under_strict_xfail - [XPASS(strict)] ...
1 failed, 1 xpassed
```

A check for `passed` is satisfied by that line — by a test that was expected to
fail, did not, and is therefore a genuine problem. The first trap reports failure
on success; this one reports **success on a failure**.

**The family is: a status word that is a substring of another status word.** The
next one will not be either of these two, so match anchored forms and read
integers, rather than learning the two words.

## 5. A guard that matches `Exception` but not `BaseException`

```python
except Exception:      # KeyboardInterrupt walks straight through
```

`KeyboardInterrupt` and `SystemExit` derive from `BaseException`, not
`Exception`. A cleanup, a rollback or a "what did this leave running" report
hung off `except Exception` does not run when somebody presses Ctrl-C — which is
precisely when a half-finished operation has left something behind.

Here that meant Ctrl-C during a create, a start or a move left a GPU box billing
under the word "Aborted!" and a failure exit code. The fix was not four more
`except KeyboardInterrupt` blocks — they drift, they cover no command written
next month, and they sit at the wrong altitude. It is an in-flight record that
registers what a mutating call may leave behind, cleared when the call returns or
raises an ordinary `Exception`, and **kept only when a `BaseException` unwinds
it**.

Related, and worth knowing in any Click or Typer program: Click catches the real
`KeyboardInterrupt` inside its own `main()`, prints `Aborted!` and exits 1 before
anything of ours runs. A `BaseException` it does not recognise walks through to
us — which is why the interrupt is re-raised under our own name.

## 6. A walk blind to indirection

An AST walk that recognises `say.fail(...)` and `typer.echo(..., err=True)` and
nothing else. A module that routes its own output through a local helper —

```python
def _refuse(message, fix=None):
    say.fail(message, fix=fix, code=2)
```

— contributes **zero** messages to that walk. `comfy_qa/remove.py`, which holds
the only irreversible command in this tool, was written entirely that way and
was invisible to the docs coverage test. Nothing looked wrong anywhere.

**The tell:** the guarantee you think you have is "every error is documented".
The guarantee you actually have is "every error *not routed through a local
wrapper* is documented", and nobody had written that down.

The fix follows one level of local indirection — a wrapper calling a wrapper is
deliberately not followed, because at that point the module should be using the
shared vocabulary directly.

### Its sibling: a non-strict xfail

An `xfail` that is not strict **passes whether the defect it records is there or
not**. When the defect is fixed the test goes quietly green and stays in the file
forever, describing something that is no longer true. This repo had no pytest
configuration at all, so the default was pytest's own: non-strict.
`xfail_strict = true` is now set, so the next xfail written without the keyword
fails when its defect is closed rather than being noticed by nobody.

## 7. The inclusion criterion is also the pass criterion

A test derives its subject set by scanning for tokens, then clears each member by
scanning for tokens — and the two token sets overlap. **The evidence of guilt is
accepted as the alibi.**

The live example, measured at the time of writing. `_commands_that_can_start_a_machine()`
decides which commands must name the bill by looking for calls to:

    bring_up, _bring_up, put_away, _serve, build

and the assertion then clears a command whose body contains any of:

    "comfy-qat down", stop_paying, _with_the_bill, _serve(, put_away

`_serve` and `put_away` are in both. Per command:

| command | included because | cleared because |
|---|---|---|
| `up_cmd` | `bring_up` | `comfy-qat down`, `stop_paying` |
| `disconnect_cmd` | `put_away` | `comfy-qat down`, `put_away` |
| `down_cmd` | `put_away` | **`put_away`** |

`up_cmd` is genuinely independent. `down_cmd` was cleared *only* by the token
that included it — so every money sentence in it could have been deleted and the
test would have stayed green. One command rather than three, but it was the
`--keep-running` branch, which this suite has already been wrong about twice in
opposite directions.

**Closed.** The two vocabularies are now disjoint: the clearing set is
`("comfy-qat down", "stop_paying", "_with_the_bill")`, with `_serve(` and
`put_away` removed, and a test named
`test_no_token_that_makes_a_command_billable_can_also_clear_it` asserts the
disjointness directly — so the shape cannot come back by someone adding a
convenient token to the wrong set.

Two things about *how* it closed are worth more than the fix. It was found twice
within an hour, independently, from opposite directions — once by reading the
test and once by auditing the commands — which is the evidence that this shape is
discoverable rather than lucky. And the guard that now exists is a guard **on the
relationship between two lists**, not on either list. That is the general remedy
for class 7: assert the disjointness, not the membership.

**The tell:** write down the token that puts a member *on* the list and the token
that takes it *off*. If they intersect, the test cannot fail for that member.

---

## Seven is a mechanism, not a cure

The temptation on reading this page is to treat it as a list of fixed bugs. It is
a list of **shapes**, and every fix has a new one.

Look at 2 and 7 together. The advice for class 2 — *derive the list instead of
typing it* — is correct, and class 7 is **what that cure produces**. The
derivation and the assertion end up reading the same source for the same tokens,
and every member the derivation adds arrives pre-exonerated.

- **Class 2 fails open on forgetting.**
- **Class 7 fails open on remembering.**

Fixing one moved the failure rather than removing it. Expect the same of anything
written after this page: ask what the new guard's own blind side is, in the same
sentence as you write it.

## And they compose

The proof is a single money sentence. Gutting the line that tells a user their
GPU is still billing produced **zero failures**, because two guards covered for
each other:

- **class 3** ate the parametrised case — the subject was removed, so the test
  removed itself rather than failing;
- **class 7** cleared the command — it was on the list because it calls
  `put_away`, and the presence of `put_away` was accepted as proof it names the
  bill.

Neither guard was broken on its own terms. Both reported success. The sentence
that says "your machine is still costing money" was gone.

When you audit a guard, audit it against the guards next to it. A defect can walk
through the seam between two correct tests.

---

## The same shapes turn up where a human is the runner

`docs/test-criteria.md` is the acceptance pack — a person runs it by hand and
ticks boxes. It has the same disease, which is the strongest evidence that these
are shapes rather than Python problems. An audit of it found:

| in the pack | the shape |
|---|---|
| Five `--dry-run` criteria asserting "nothing was created", in blocks with no step that could detect creation | class 4 — an instrument with one branch. The plan prints identically in both worlds |
| `G1` and `G6a` graded on the tool's own sentence about what it stopped | the tool's claim accepted as its own evidence. Phase M gets this right — `M4` makes the claim, `M5` reads `gcloud` and checks it. Same page, same author, one has the observation |
| Phase I says in bold "run this before the run and after it, and compare" — and never runs it before | a comparison with no baseline. It is also the designated backstop for every unobserved negative in the pack, so it compounds rather than sits beside them |
| `G5` "does not fail on an already-stopped box" | unfalsifiable: a `down` that does nothing at all and exits 0 passes identically to one that handles the case |

The sharpest observation from that audit is one my seven did not have, and it
generalises back to the suite: **not a remedy that defeats the check, but a check
with no observation attached.** The tick costs nothing because nothing was ever
looked at. None of those needs a tester to cut a corner — following the words
exactly produces the tick.

Two of those are genuinely new shapes rather than restatements — *the comparison
with no baseline*, and *the expected result is also what the failure produces*.
Both apply to unit tests as readily as to a checklist.

One more, smaller, and it is why this page had to be linked by hand: **nothing
tests the docs index in the README.** A page can be added and never linked, which
is how `session-expiry.md` went missing from it for as long as it did. A free
edit is an unguarded one.

## The only standard that survived contact

**After writing a test, delete the fix and watch it fail by name.**

Not "run the suite". The suite was green for every single item on this page.
Green is the condition under which all seven of these were introduced, and every
author was sure.

Deleting the fix is the only step that distinguishes a test that checks something
from a test that is merely present. It costs about a minute. Every one of these
seven would have been caught by it, at the moment it was written, by the person
who wrote it.

If the fix cannot be deleted cleanly — it is one line inside a function you need
— break the thing it asserts instead: change the message, flip the comparison,
return the wrong constant. The point is to see the failure *by name*, and to
confirm the name is the one you expected.

## A guard whose printed remedy defeats it

Worth its own note, because it is the same disease in a different organ.

A command was added that starts a box, leaves it running, and says nothing about
money. The guard fired correctly and printed instructions for satisfying it. The
instructions were followed exactly. The suite went green — and the command still
said nothing about money.

**A guard whose printed remedy defeats it is worse than no guard, because it is
trusted.** If your assertion's failure message tells someone how to make it pass,
check that the cheapest way to follow that advice is also the correct one. Where
it is not, say what the test is actually protecting, and let the reader work out
the remedy from the rule rather than from the error string.

---

## A finding needs a commit hash attached

Three times in one day a finding here went stale within the hour of being
written: a defect fixed underneath its own report, a count that moved, a
criterion that changed. Twice the report was re-sent without re-checking, and
the reader was handed something that had stopped being true.

**Verify at the working tree as well as at HEAD**, and say which you read. Both
are edited live when more than one person is working, and a claim about
`tests/` or `comfy_qa/` with no sha attached cannot be checked by whoever reads
it next. That is not pedantry about citation — it is the difference between a
finding somebody can act on and one they have to re-derive.

The class-7 instance on this page is the worked example: correct when found,
already being fixed while it was being written up, and closed before anyone
could read it. The *shape* is what the page records. The instance is dated.

## Provenance

Classes 1–6 are drawn from the commits that introduced and closed them. Class 7
and the composition case came from a verification pass and were re-measured
against `host.py` before being written here, then re-checked against the working
tree — which is how the fix in flight was caught. The status-word demonstration
in class 4 was reproduced directly rather than transcribed: `xfail_strict` turns
an unexpected pass into `FAILED`, so `xpassed` needs a `strict=False` to appear
at all, and that correction is why the section says what it says.

The acceptance-pack section is another agent's audit, quoted rather than
re-derived. The `bench_cmd` demonstration in the section above it is reported
from the verification pass and has **not** been reproduced by the author of this
page.
