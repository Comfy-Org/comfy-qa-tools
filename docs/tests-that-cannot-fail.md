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

or read the integers out of the summary. **Never the bare word.** The same trap
sits in `passed` vs `xpassed`, which has not bitten anyone yet only because
nobody has grepped for it.

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

`up_cmd` is genuinely independent. `down_cmd` is cleared *only* by the token that
included it — so every money sentence in it could be deleted and the test would
stay green. That is one command rather than three, but it is the `--keep-running`
branch, which this suite has already been wrong about twice in opposite
directions.

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

## Provenance

Classes 1–6 are drawn from the commits that introduced and closed them; class 7
and the composition case were found by a verification pass and re-measured
against the source before being written here. The class-7 table was produced by
walking `host.py` at the time of writing — if the token sets have since changed,
re-measure rather than trusting the table.

The `bench_cmd` demonstration in the last section is reported from that same
verification pass and has not been reproduced by the author of this page.
