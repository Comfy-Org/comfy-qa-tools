# Tests that cannot fail

Nine shapes a test in this repo has taken that made it **incapable of failing**.
Not slow, not flaky, not weak — incapable. Each one was written by somebody who
was sure, each was green, and each was found later by accident.

This page exists because they were found one at a time, by different people, and
the knowledge lived in commit messages nobody reads twice. Without it the next
person pays full price again — including a version of us with no memory of the
day these were found.

**Read the last two sections before the list.** The shapes are useful; the two
observations at the end are the reason the list is not a checklist.

One way to tell whether this page is still doing anything: while it was being
written it caught two errors on their way into it — a claim that had gone stale
between being made and being read, and a correction from the person who
commissioned the page. Both were checked only because the page says to check. If
that stops happening, suspect the page before you suspect the code.

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

### Presence, where you meant value

The sharpest small version, and it reads as a real check right up until you test
it. A guard asked whether a sink had an `err` keyword — **not what it was set
to**. So `typer.echo(line, err=False)`, which says *stdout* out loud, satisfied a
guard whose entire purpose was catching stdout sinks.

Measured one shape at a time against a real module: of seven ways to write a
stdout sink, it caught **one**. `print`, `sys.stdout.write`, `say.result` as a
sink, a nested `def`, a `functools.partial` and the explicit `err=False` all
walked through.

Fixed in `ce359a9` by reading the keyword's *value* and four write routes — and
the widened guard **found a real defect on its first run**, which is the whole
argument for this page in one line. An instrument that cannot fail is not
protecting anything; the moment it can, it has something to tell you.

Whenever an assertion asks whether a thing is *there*, ask what it should be
asserting about the thing's *value*, and whether the difference is the entire
point of the check.

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

## 6. A walk that silently skips what it does not recognise

Any scan — an AST walk, a regex over a document — that has a notion of what it is
looking for, meets something outside that notion, and **passes over it without
saying so**. Two routes in, both seen here: indirection the walk cannot follow,
and a pattern too narrow to express what it is matching.

### Route one: indirection

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

### Route two: a pattern too narrow to say what it means

The guard that audits the acceptance pack matched criterion ids with
`[A-Za-z0-9.]+`. That omits `/`, so the nine combined ids — `A5a/A5b`,
`J2/J3/J4`, `N8/N9` and the rest — **were skipped in silence** while the suite
reported success. The auditor was checking 148 of 157 boxes, and its author
reported "147 criteria" twice on the strength of it.

Nothing new was flagged when the pattern was widened, so the blind spot was
benign in outcome. It was not benign in principle, and it would not have stayed
benign.

**The remedy is the transferable part, and it is not "widen the pattern".**

> The cure is the assertion, not the wider pattern.

A second test now counts `- [ ] **` lines — no regex worth getting wrong — and
compares that count against what the walk saw. A scan whose coverage is asserted
against an independent count cannot quietly shrink. Widening the regex fixes
today; counting the boxes fixes the next narrowing too.

This is the same move as class 7's remedy one domain over: **assert the
relationship between two things rather than trusting either one.** Closed in
`be171f5`.

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
| `create_cmd` | `build` | `comfy-qat down` |
| `up_cmd` | `bring_up` | `comfy-qat down`, `stop_paying` |
| `down_cmd` | `put_away` | **`put_away`** |
| `go_cmd` | `_bring_up`, `_serve` | **`_serve(`** |
| `switch_cmd` | `_bring_up`, `_serve` | **`_serve(`, `put_away`** |

`create_cmd` and `up_cmd` are genuinely independent. The other **three** —
`down_cmd`, `go_cmd`, `switch_cmd` — were cleared only by tokens drawn from the
inclusion vocabulary itself, so every money sentence in them could have been
deleted and the test would have stayed green. One of the three is the
`--keep-running` branch, which this suite has already been wrong about twice in
opposite directions.

> **Three of the six passed on nothing else.** — the fix's own comment.

**Read the next paragraph before you trust any table on this page**, because
this one was wrong when first published and the way it was wrong is class 2.

I originally reported **one** command, not three, and defended the narrower
number as a correction to somebody else's count. It came from a measuring script
into which I had typed the three commands I intended to check —
`("disconnect_cmd", "down_cmd", "up_cmd")` — and then reported a result about
six. `go_cmd` and `switch_cmd` were never examined. **A hand-typed list nothing
derives, in my own measurement of the class about hand-typed lists**, producing a
confident narrowing from a sample I had chosen myself.

Nothing about the method looked wrong while I was doing it. The script ran, the
output was specific, the three commands it named were correctly analysed. That
is what this whole page is about, and it applies to the instrument you are
holding as much as to the code you are pointing it at.

**Closed in `e698b63`.** The two vocabularies are now disjoint — the clearing set
is `("comfy-qat down", "stop_paying", "_with_the_bill")`, with `_serve(` and
`put_away` removed — and `test_no_token_that_makes_a_command_billable_can_also_clear_it`
asserts the disjointness directly, so the shape cannot come back by someone
adding a convenient token to the wrong set.

Two things about *how* it closed are worth more than the fix. It was found twice
within an hour, independently, from opposite directions — once by reading the
test and once by auditing the commands — which is the evidence that this shape is
discoverable rather than lucky. And the guard that now exists is a guard **on the
relationship between two lists**, not on either list. That is the general remedy
for class 7: assert the disjointness, not the membership.

**The cure has its own failure mode, and it is the same disease from the other
side.** The obvious way to make the alibi honest is to follow calls: if a command
delegates, look at what it delegates to. Followed to exhaustion, that reaches the
shared ending helpers from almost anywhere in these modules, clears every command,
and hands back a guard that cannot fail. So the alibi is **one hop**, not the
transitive closure. Class 7 is the failure mode of class 2's cure; this is the
failure mode of class 7's. Two turns of the same screw, in one file, in one day.

**And disjointness alone is not enough, because the evidence can be worthless
even when it is not circular.** Two things counted as an alibi that were never
one: an `import` line naming a helper, and advice attached to an *abnormal exit*.
`bring_up` names the bill nine times, and every one of them is a `fix=` or the
`undo=` of an interrupt handler — advice for a run that went wrong. Counting
those cleared a command that starts a box, leaves it running, and says nothing
about money on the path where it succeeds. It is the same `bench` command the
last section of this page describes.

The rule the fix landed on, in its own words: **an alibi drawn from a failure is
no alibi for a success ending.** So the search runs over the body with imports,
`raise`s and abnormal-exit advice removed.

Class 7's remedy is therefore two halves, and neither is sufficient alone:

- **assert the relationship between the two lists** — that no token which puts a
  member on the list can also take it off;
- **assert against the success path** — because a guard about what a command says
  when it works is not satisfied by what it says when it fails.

### And a third half: the suspect set was incomplete

Both of those are about *clearing* a member wrongly. Neither says anything about
a member that was never considered — and that was the larger hole.

A command injected as a test — start a box, leave it running, print only
"warmed up" — passed the whole file green. The guard did not clear it. **The
guard never saw it.**

The mechanism is not that method calls are invisible to the scan; they are not,
the walk reads `obj.method()` by name perfectly well. It is that the *inclusion
vocabulary was itself a hand-typed list of helpers* — `bring_up`, `_serve`,
`build`, `_bring_up` — so it found only commands that call a name somebody
thought to type. **Class 2, one layer down, inside the cure for class 7.**

It also missed a real command, not just an injected one. `move` starts a GPU box
through `gc.start_instance`, inside a helper one call *below* `move_cmd` — a
name nobody would have typed. So `move` was in no list at all: not derived, not
declared, and nothing checked that the one command which starts a box in order to
ask Google a question says how to stop it. It does say it, in two places. Nothing
was enforcing that.

**Closed in `d39895b`**, by keying inclusion on the gcloud calls themselves —
`start_instance`, `create_instance_from_image`, `create_instance_from_disk` —
which is the layer where "a box exists and bills" is a fact rather than a
convention. Every helper that starts one is now reached rather than remembered.

Two things from that fix are worth more than the fix:

**The asymmetry is deliberate.** Inclusion is followed *transitively*; the alibi
still resolves exactly *one hop*. An over-large suspect set costs a command one
line of output it should probably have anyway; an under-large one is a GPU box
billing overnight with nothing said. **Wrong in the cheap direction on the way
in, strict on the way out.** That is the general shape for any guard where the
two errors have different prices.

**And prose was clearing commands.** A comment reading "every lifecycle failure
through `_with_the_bill`" was enough to satisfy the rule — a sentence *about* the
rule accepted as compliance with it, which is the `import` case wearing different
clothes. The scan now strips comments and docstrings.

## 8. The ambient default

A test whose subject is decided by an environment value it never sets: the
platform, an environment variable, the presence of a binary, a file in `$HOME`,
the network.

**Not a face of 7 — file them separately.** These were briefly treated as one
class and they are not: the disjointness assertion that closes 7 does nothing
whatever for 8. In 7 the test names its own subject and the naming is circular;
in 8 the test never names its subject at all, and the room decides.

**The tell:** ask what this test does on a machine unlike yours — a different OS,
no network, a fresh checkout, a cleared cache. If you cannot answer, the test has
a hidden parameter.

The measured example is worth walking through, because one green result was
hiding three different mechanisms.

Two tests call the zone-ordering helper without passing `probe=` or a cache
`path=`. Neither asserts anything about latency — one checks a fall-through flag,
the other case normalisation. The network call is purely incidental to what they
are for. But the defaults are a real socket probe and
`~/.config/comfy-qa-tools/zone-latency.json`, so:

| the machine | what happens | result |
|---|---|---|
| online, no cache | two real TCP connections to Google, a file written into `$HOME` | green |
| online, cache present | zero sockets, ten times faster | green |
| offline | every region unreachable | green |

The artefact is real — 1801 bytes of live per-region latencies, written into a
developer's home directory beside their hand-maintained host list, by running the
unit tests.

**The ambient default is created by the suite itself.** The first run on a machine
and every run after it exercise different code, and nobody would ever see the
difference, because all three are green. Whether those tests measure anything is
decided by whether a previous run left a file in your home directory.

A second instance has the **opposite polarity** and is worth contrasting: a test
read the real production host list, because a monkeypatch that appears in the very
next test was not applied to it. That one *fails* on a fresh machine rather than
passing vacuously — the honest direction. Same class, and the difference between
the two is luck, not design.

Neither is a defect in the code under test; both are the test borrowing
something from the room it runs in.

**Closed in `caa0640`.** `conftest.py` now carries the tripwire the safety
fixture's name had been promising: outbound sockets and writes to the real config
directory both fail loudly from inside the suite, rather than succeeding quietly.
The two leaking call sites were repaired with it, and `tests/test_tripwires.py`
exists to prove the tripwire itself can fire — which is class 4's lesson applied
to the guard rather than to the code.

## 9. A proxy for the question, instead of the question

The assertion checks something that *correlates* with what you meant, and the
correlation is maintained somewhere else.

The clean example: `stopped_first = bool(first)`, standing in for "is this list
non-empty". It agreed with the real question only because a guard in a *different
function* kept them in step. Change that guard and the assertion goes on passing
while meaning something else.

**The tell:** name the thing you want to be true, out loud. Then read the
assertion. If it is a different sentence, ask what keeps the two agreeing — and
whether that thing is in this file.

**A readable signal, from someone who wrote one of these knowingly:** the proxy
and the question usually live in **different scopes**. In every instance below
the proxy came from another function or an earlier state, while the question was
local and one line away.

> This condition is computed further away than the thing it decides.

That is greppable by eye in a way "is this a proxy" is not.

**Three instances, all one day, all with the same one-sentence fix — *ask the
list the answer is built from*:**

| where | the proxy | the question it should have asked |
|---|---|---|
| `b8db71d` | "is there anything to delete", re-derived from `plan` plus two of `found`'s five fields | `mine`, computed on the line above |
| `0891903` | `leftovers(...)[len(mine):]` — a length standing in for "where does my section end" | `split_leftovers` |
| `ed6513f` | `stopped_first = bool(first)` | `bool(others_stopped)` |

The first was wrong in **both** directions at once: it missed `spare_snapshots`,
so a dry run asked a destructive question, and it named a disk unconditionally,
so it announced one that did not exist. The third was not merely a bad report —
the registration below it did `others_stopped[0][0]`, and the two agreed only
because a guard in another function returned `None` for an empty list. An
`IndexError`, on every ceiling switch, taking the whole command down.

The strongest fact about this class is the order those three landed in: **the
third was written by the person who had just read and fixed the other two**, the
same afternoon, knowing the shape. That is the argument for giving it an entry
rather than filing it under carelessness. **A shape that survives knowing about
it earns its own entry.**

### The one sub-property you can actually grep for

Most of class 9 needs judgement. This part does not, and it is worth a standing
check: **a test double whose return type the subject no longer produces.**

A stub returned `False`. The function it stood for had moved to returning one of
four strings, because a bool could not tell a stopped box from an
already-stopped one. The caller then did `.get(found, [])` — so `False` matched
nothing, every host fell into a throwaway list, and the test drove a branch **the
real function can no longer produce**. The assertion it made about that branch
was not merely unearned; it was wrong.

That is the proxy shape with a mechanical signature: the double is a correlate
that has **drifted** from its subject, and drift in a *type* is greppable where
drift in a *meaning* is not. Whenever a function's return type changes, the
doubles standing in for it are the second site — see the note on corrections
landing in one place, further down.

### The fourth instance is a safety fixture, and it is the worst one

`tests/conftest.py` carries an autouse fixture named
`never_write_to_the_real_config`. Its entire body redirects the *tunnel*
directory:

```python
@pytest.fixture(autouse=True)
def never_write_to_the_real_config(monkeypatch, tmp_path):
    """The tunnel directory defaults beside the user's own host list."""
    monkeypatch.setattr(tunnel_module, "TUNNEL_DIR", tmp_path / "tunnels")
```

The name is a proxy for the coverage. It promises a category — *the real config*
— and delivers one path inside it. `zone-latency.json` is written into that same
real directory by a different module, so **this is the fixture that should have
caught class 8's centrepiece**, and its name is exactly why nobody checked
whether it did.

**In the safety half of a suite, the name is what everyone reads instead of the
body.** That is where this class does the most damage: a fixture called
`never_write_to_the_real_config` is not read again by anybody, because it has
already answered the question. Nine words of docstring bought a year of
not-looking.

The rule that follows: **a fixture's name may describe only what its body does.**
If the name states a category, the body must cover the category or the name must
shrink to what it covers.

**The tell:** write down the token that puts a member *on* the list and the token
that takes it *off*. If they intersect, the test cannot fail for that member.

---

## Nine is a mechanism, not a cure

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

**And the chain ran off the end of the test suite.** Each instrument built here
found something the previous one could not see, and the last link is not a test
at all: the guard written to catch commands that never mention the bill turns out
to answer a question about commands that never check the quota. If that keeps
happening, the instruments are still improving — which is a different thing from
the code getting good, and worth not confusing with it.

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

The sharpest observation from that audit is one the first seven did not have, and it
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

## Where we looked and found nothing

A page listing only what was found reads as a catalogue of disasters. This
section is the other half, and it is the more useful one, because it puts a
**bound** on class 8 rather than leaving an absence of evidence.

**The instrument, because it is what makes this evidence rather than assertion:**
inject the *opposite pole* of the ambient value globally, in `conftest`, so that
only tests which set the value themselves are unaffected. Green then means no
test depended on the ambient value.

Measured at `576738c`, against a baseline of 3839 passed / 66 xfailed.

**Green — nothing depended on it:**

| forced to | result |
|---|---|
| `sys.platform` → `"linux"` | 3839 passed |
| `shutil.which` → always found | 3839 passed |
| `COLUMNS` → 40 (Click wraps to terminal width) | 3839 passed |
| `TERM` → `"dumb"`, `NO_COLOR` → `"1"` | 3839 passed |
| whole suite in **reverse file order** | green *(at `e940b0b`, not re-run since)* |
| every test file alone in its own process | no isolated failures |
| canary `hosts.toml` under a fake `HOME`, md5 before and after | identical — **the suite does not write the host list** |

**Not green — and these rows are the more useful ones**, because they fail on a
fresh machine rather than passing vacuously, which is the honest direction:

| forced to | result |
|---|---|
| `shutil.which` → always `None` | 5 failed — 2 in `test_shell_access`, 3 in `test_tunnel` |
| `PATH` → `/usr/bin:/bin` | 2 failed — `test_shell_access` |
| `HOME` → empty tmpdir | 1 failed — `test_readme.py::test_bare_comfy_qat_lists_your_machines` |

That last one is **a class 8 member, not mere fragility**: the test invokes the
CLI with no `--config` and no monkeypatch of the config path, so it reads the
developer's real host list. Both directions were measured — it fails with `HOME`
empty, and passes again with a one-host canary — so the assertion needs the file
to exist and nothing more. The very next test in the same file *does* patch the
config path. The seam was known and not applied.

**Axes nobody has tested**, so that this section does not imply more than it did:
CWD, `PYTHONHASHSEED`, `LANG` as distinct from `LC_ALL`, and any capsys-versus-
CliRunner stream difference.

**Why this counts as a bound rather than an absence.** Two *different*
instruments were used here — a socket tripwire and an environment injector — and
**each returned both outcomes**. The socket tripwire came back clean at one
commit and caught two tests at another; the injector came back clean on four axes
and caught three. Neither has only ever said "clean". An instrument that had
would be class 4, and its silence would mean nothing.

When you extend this page, extend this section too. "We checked and it was fine"
is worth writing down only if you can say what would have happened had it not
been.

## The only standard that survived contact

**After writing a test, delete the fix and watch it fail by name.**

Not "run the suite". The suite was green for every single item on this page.
Green is the condition under which all nine of these were introduced, and every
author was sure.

Deleting the fix is the only step that distinguishes a test that checks something
from a test that is merely present. It costs about a minute. Every one of these
nine would have been caught by it, at the moment it was written, by the person
who wrote it.

If the fix cannot be deleted cleanly — it is one line inside a function you need
— break the thing it asserts instead: change the message, flip the comparison,
return the wrong constant. The point is to see the failure *by name*, and to
confirm the name is the one you expected.

## A test whose premise is somebody else's open bug

**Deliberately not numbered.** The nine are ways a test cannot *fail*; this one
fails loudly, at the wrong time, at the wrong person. It is recorded here because
it is the same underlying mistake — **a test whose subject is a defect rather
than a behaviour** — and because it has a cost the nine do not: it makes the
person who fixed something look like they broke it.

A test needed a host-list write to fail at the last step, after the instance
exists and is billing. It got there by using a real open defect as the trigger:
an inline comment on a port line, which the port pattern did not match, so a move
refused with "has no port line" about a line that was right there.

Then somebody fixed that defect. The trigger stopped triggering, the test went
red, and the redness pointed at the fixer.

The rule the test asserts — *a move that cannot write the host list still says
what is billing* — has nothing to do with commenting style. It was re-pointed at
a cause that **cannot be fixed out from under it**: a directory nobody can write.

> A test whose premise is somebody else's open bug expires the day it is closed.

**This is the mirror of the non-strict xfail in class 6**, and the pair is worth
holding together. An xfail pinned to a defect goes *quietly green* when the
defect is fixed and lives on describing something untrue. A test *triggered* by a
defect goes *loudly red* and accuses the fixer. Both are a test resting on a bug
instead of a behaviour; they differ only in which direction the failure points.

When you need a failure to test a failure path, reach for a cause nobody will
ever fix — an unwritable directory, a closed port, a missing file — rather than
one that is on somebody's list.

## When the tool itself spends before it checks

**The first entry here that is not about a test.** Everything above is a way an
instrument fails to notice something. This is a way the *product* fails, and it
is on the page because the shape is identical one level up.

`move` takes a snapshot, creates a disk, and only then asks Google to create an
instance — which, on a project whose GPU ceiling is already spent, is refused.
The slow expensive half completes and the useful half does not. That is not a
hypothetical: it is the incident that caused `Phase I` of the acceptance pack to
be written, and at the time of writing the pack's own `move` procedure would
reproduce it.

**It was invisible precisely because the check exists.** `create` reads the
project allowance before anything is created. `switch` asks whether the ceiling
is what is in the way. Anyone glancing at the codebase sees a ceiling check and
concludes the tool checks the ceiling. Measured:

| helper | callers |
|---|---|
| `global_allowance` | `create.py` and `_blocked_by_the_ceiling` |
| `_blocked_by_the_ceiling` | **one** — inside `switch_cmd` |
| anything in `relocate.py` | none; its only quota read is for disk, and its own docstring says *"Advisory only."* |

So the defect is not a missing feature. It is **an invariant held at two of three
call sites** — and nobody ever wrote down "the commands that check the ceiling"
as a list, which is exactly what effectively exists. That is class 2, one level
up from the tests and into the product: a hand-maintained set nothing derives,
failing open on the member somebody forgot to add.

**And the remedy is the same move for the third time tonight.** The billable
guard already derives *every command that can start a machine* from the three
gcloud primitives that start one. That derivation answers this question too:

> Every command that can start a billable machine must consult the ceiling
> before it spends anything.

One pass over a set that is already computed. Class 6's remedy was *assert the
relationship, not the pattern*; class 7's was *assert the relationship, not the
membership*; this is the same instruction a third time, arrived at from a
different direction. **Three independent instances make it a principle rather
than a trick:** when a rule is supposed to hold across a set, derive the set and
assert the rule over it — never enumerate the members and trust the list.

*Being fixed; the commit will be named here when it lands.*

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

**Closed, and adversarially verified — not merely fixed.** The guard was re-run
in both directions against a later commit: a command that starts a box and defers
its ending to `_serve` **passes, and correctly**, because `_serve` really does
print the stop-paying line on its success path; a command that calls `bring_up`
and never reaches `_serve` **fails, by name**. That is exactly the case class 7's
success-path half was written for — `bring_up`'s bill mentions are all `fix=` and
`undo=`, so they do not clear a success ending, and the guard says so.

And the failure text no longer hands over the shortcut that defeated it:

> Adding it to `BILLABLE_ENDINGS` is **not** the fix — that is the list of
> commands this rule applies to, not the list of exceptions to it.

That sentence is the lesson of the class, printed at the moment somebody is about
to repeat it. If you write a guard whose remedy could be misread, the error
string is where to say so.

**One trap for anyone re-testing this.** Re-running the *original* injected
command against the fixed guard comes back green, and for a minute looks like the
fix failing. It is not: that command called `_serve`, so it was cleared for a
real reason. Change the injected command to skip `_serve` before concluding
anything.

---

## A finding needs a commit hash attached

Three times in one day a finding here went stale within the hour of being
written: a defect fixed underneath its own report, a count that moved, a
criterion that changed. Twice the report was re-sent without re-checking, and
the reader was handed something that had stopped being true.

**A reading of the working tree is not a reading of HEAD**, and neither is a
reading of the other one. Say which you read. Both are edited live when more than
one person is working, and a claim about `tests/` or `comfy_qa/` with no sha
attached cannot be checked by whoever reads it next. That is not pedantry about
citation — it is the difference between a finding somebody can act on and one
they have to re-derive.

The class-7 instance is the worked example, and it went through **three readings
that disagreed, none of them wrong**:

1. measured live and found real — one command, `down_cmd`, cleared only by its
   own inclusion token;
2. read again in the working tree, where a fix was in flight, and reported
   **closed** — true of the tree, not yet of `HEAD`;
3. read a third time at `HEAD`, which did not have it yet, and reported **still
   open** — a correction that was itself about to go stale, because `e698b63`
   landed the fix minutes later.

Every reading was accurate at the moment it was taken. Every one of them would
have misled somebody an hour later. The only thing that makes any of them useful
a day on is the sha, which is why this page names `e698b63` rather than saying
"fixed".

So the rule is not "attach a hash". A hash alone would not have separated those
three, because all three had one. **A finding needs a hash *and* a direction:
what you read, at which sha, and whether the tree was dirty.**

    read tests/test_host_costs.py at 3e0a78d, clean          -> class 7 open
    read tests/test_host_costs.py in the working tree, dirty  -> fix present
    read tests/test_host_costs.py at e698b63, clean           -> class 7 closed

Three true statements. Only the third is still true, and only because it names
where it looked.

The *shape* is what a page records. The instance is dated, and dating it is the
whole job.

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
