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

## 2. A list that nothing prunes

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

**This class was first written down with the wrong rule, and the corrected one is
the point of it.** It said: *derive the set, never enumerate it.* That is wrong,
and following it does harm. Derivation is not always possible, and somebody
trying to obey the rule has two bad exits — contort the code until a derivation
exists, or **loosen a matcher until no list is needed**, which trades a visible
list for an invisible over-match. Both are worse than what they replace.

> **A hand-maintained list is not the defect. An unguarded one is.**

The counter-example is in this repo and it settles it. `NOT_OUR_MESSAGE` in
`tests/test_docs.py` exempts seven troubleshooting entries from the check that
every entry describes something the tool still says — four are other people's
words (the shell's `command not found`, torch's CUDA assertion, gcloud's
permission refusal), three are ours, built so heavily from interpolation that no
literal run is long enough to match. It is hand-typed, and **it is guarded in
both directions**:

- `test_no_exempted_entry_has_quietly_become_checkable` returns an entry to the
  check the moment its message grows a run long enough to match — so the
  exemption cannot outlive its reason;
- `test_no_exempted_entry_has_left_the_page` fails on an exemption for an entry
  nobody has any more — so the list cannot silently accumulate.

**One direction alone is half a guard.** Its own comment states the standard
better than a rule could: *"an allowlist nobody prunes is the hand-maintained
list coming back."*

Re-read the two failures above against that, and they still stand — for a
different reason than first recorded. `ERROR_TYPES` was not a defect because a
human typed it; it was a defect because **nothing pruned it** when two classes
dropped out. `BILLABLE_ENDINGS` likewise: a new command that leaves a box running
could join the codebase and nothing made the list account for it.

**So the detection question is not "who typed this?" but "what prunes this?"**
Derive the membership where you can — it is the cheapest guard available. Where
you cannot, keep the list and add the two tests that stop it rotting in either
direction. **Then read section 7, because deriving has its own shape.**

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
file list directly — a hand-maintained list accepted deliberately, and by
section 2's corrected standard a correct one: it changes only when a file is
added or renamed, the fix is one line, and the failure it catches is one no
amount of reading the numbers will.

This shape has been found three separate times, most recently inside a commit
written to eliminate it.

**One caveat when you go looking for it: a strict `xfail` that has disappeared is
not proof the defect was fixed.** The test may simply have been deleted, which is
this class exactly. Confirm the test still exists and now passes, rather than
reading its absence from the failure list as good news.

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
deleted and the test would have stayed green. One of the three was the
`--keep-running` branch, which this suite had already been wrong about twice in
opposite directions. That flag has since been removed and the capability lives
under `disconnect`; the finding is recorded as it was found.

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

### The sharper version: the guard existed, and the probe was outside it

Worse than an unwritten default is one **somebody already wrote down** — correctly,
in a fixture, for exactly this reason — where the knowledge does not travel.

Driving `comfy-qat go` in-process against a fake `Gcloud`, to verify an unrelated
fix, executed a real `gcloud compute ssh` and appended about twenty lines to the
developer's real `~/.config/comfy-qa-tools/tunnels/comfy-win.log`. It died at
local credential refresh without reaching Google — nothing created, nothing
modified, no stray process — but it ran, and it wrote where it should not have.

Two ambient defaults, and the second is the nastier:

1. `tunnel.TUNNEL_DIR` defaults to the **real** config directory.
2. **`tunnel.py` does not go through the `Gcloud` class at all.** It builds its
   own argv and spawns the binary, so patching `gcloud.Gcloud` — the seam every
   other module uses, and the one any careful person would reach for — covered
   nothing.

**And the suite already guards both.** `tests/test_host_costs.py`'s `cli` fixture
opens by patching `TUNNEL_DIR` into `tmp_path`; every test in that file gets it.
The probe did not, because **a probe is not a test**.

> A fixture is not documentation. It is a local fact that protects only the code
> that happens to ask for it.

Every ad-hoc probe, every `python -c`, every triage script starts outside it. And
this instance was walked into by the person who had named the class and built the
socket tripwire for it — while driving in-process rather than through the harness
that puts `HOME` and `--config` in scratch precisely so it cannot happen. Worth
stating rather than tidying away: knowing the class does not put you inside the
guard.

**The remedy is not "remember to patch `TUNNEL_DIR`."** It is to make the ambient
default **unreachable** rather than overridden — a tripwire armed before the
package is imported, so `subprocess` and non-loopback sockets raise:

```python
from tests import tripwire; tripwire.arm()   # before importing comfy_qa
```

It lives at `tests/tripwire.py`, with `tests/test_tripwire.py` beside it. **The
path is a convenience, not the record** — the contract above is the whole of it,
and rewriting thirty lines from that description beats following a link that has
gone stale.

**Loopback has to pass through, and that exemption is itself pinned.** The test
for the honest case asserts an `OSError` — a refused connection to a port with
nothing on it — and specifically **not** an `AssertionError`. What is being held
in place is the guard *standing aside*, not merely the guard firing.

That matters because a strict-everything version fired on the first honest
ComfyUI port check and would have been switched off within the hour. **A detector
that cannot tell the honest case from the leak gets disabled, and then it
protects nothing** — so the exemption needs a test as much as the alarm does.

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

**And the same class has a second form, caught in the act while that phase was
being fixed.** Adding a step that stops the box moved it *above* the baseline
capture — which included a `stamp`. A stamp of a stopped box records "nothing
answered", and the later criterion compares the moved box's stamp against that
file. Two error strings match each other well enough for a tired reader to tick
it, so the check becomes unfalsifiable.

> A baseline that captures a failure is worse than no baseline, because it looks
> like one.

Caught by its author, mid-change, by asking what the captured file would actually
contain rather than that the capture had run. That question — *what is in it, not
did it happen* — is the one a baseline step needs and the one nobody asks.
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

**A detector that fires on the honest case teaches people to disable it.** The
socket tripwire caught a legitimate loopback connection to `127.0.0.1:8190` on
the very next run after it was armed — a real local port check, not a leak. That
is why the instrument has to separate *reached the outside world* from *did I/O
at all*. An alarm nobody can distinguish from noise gets switched off, and then
the bound in this section is worth nothing.

**And treat a moving count as a question, not a footnote.** Class 2's correction
was found because someone noticed the suite's *skip* count go from two to seven
in one pass and went to look. A skip hides a defect exactly as an `xfail` does,
and a number that moved without anyone deciding it should is the cheapest
available signal that something changed underneath you. It is the same instinct
as class 3's rising total — the difference is only which number moved.

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
up from the tests and into the product: **a set nothing prunes**, failing open on
the member somebody forgot to add. Note which half of class 2 it is — the list
here is not merely hand-typed, it is *unwritten*, so there was never anything to
prune it against.

**And the remedy is the same move for the third time tonight.** The billable
guard already derives *every command that can start a machine* from the three
gcloud primitives that start one. That derivation answers this question too:

> Every command that can start a billable machine must consult the ceiling
> before it spends anything.

One pass over a set that is already computed. Class 6's remedy was *assert the
relationship, not the pattern*; class 7's was *assert the relationship, not the
membership*; this is the same instruction a third time, arrived at from a
different direction. **Three independent instances make it a principle rather
than a trick:** when a rule is supposed to hold across a set, make something
other than memory decide who is in it — derive the set where you can, and where
you cannot, guard the list so it cannot rot in either direction.

*Closed in `cdeeddd`: the check now runs before anything is created, so a move
that cannot fit is refused up front and costs nothing.*

### The same shape again, four lines apart

The second instance is smaller and much easier to see, which is why it is worth
having: **an external contract pinned on one line and not on the next.**

```python
WINDOWS_ROOT = r"C:\ComfyUI"      # provision.py:34
LINUX_ROOT = "/opt/comfyui"       # provision.py:35
```

Both are paths this tool creates on somebody else's machine, so both are
contracts a person could be depending on. `test_provision.py:189` asserts
`WINDOWS_ROOT == r"C:\ComfyUI"`. **Nothing anywhere asserts what `LINUX_ROOT`
is.** Change it in a refactor and the suite agrees with you. Four of these
constants are unpinned against seven that are held, and nobody wrote down which
list a new one joins.

And the near-miss is the instructive part. Two lines *look* like they cover both:

```python
assert root_for(WIN) == WINDOWS_ROOT
assert root_for(LINUX) == LINUX_ROOT
```

They pin neither. They compare a function against the constant it returns, so
they hold for **any** value the constants take — the assertion is circular, which
is class 7 in two lines. A reader scanning for "is this covered" sees the
constant's name in an assertion and stops looking.

Two independent instances, in unrelated parts of the codebase, with the same
remedy: **make something other than memory decide the set the rule applies to.**
That is what makes this a claim rather than an anecdote.

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

## A correction that lands in one place reads as done

From the diff, a fix applied at the site where the problem was noticed looks
finished. **Whoever writes the fix is the worst-placed person to find the second
site, because they know where they were looking.** Four times in one day, in four
different kinds of thing:

| the fix landed in | the site it missed |
|---|---|
| one of two code sites | a leftovers block repaired while the plan note still leaked |
| one of two prose sites | a provenance paragraph corrected, and the same claim left standing eighty lines below, contradicting it |
| one of six modules | a scan widened exactly where the narrow pattern had been spotted |
| **the copy rather than the original** | a generated export edited, and the next run of the generator silently put the old text back |

**The fourth is the one that proves the class is structural.** That fix was
correct. It was applied. It was *verified* — the export was re-read afterwards
and the bad references were gone. Every step was done properly and the fix was
still lost, because it landed in the artefact instead of in the thing that
produces the artefact. Nothing about it was hurried.

**The detection rule:**

> Ask whether the thing you just edited is *derived* from something else. If it
> is, your edit is provisional until the source has it.

The tell is that an artefact and its generator both exist and only one of them is
in front of you.

**And the repair is not "remember to fix the source."** Nobody remembers. The
audit became a step inside the export itself, so the next run re-checks and the
failure cannot recur quietly. Fixing the generator is the first half; **building
the check into the pipeline is the half that holds**, because a discipline that
depends on remembering has an expiry date. That is the same move as every other
remedy on this page: put the guarantee somewhere that runs, not somewhere that is
recalled.

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

**And do not take the reasoning from the commit that touched the file.** A fix's
explanation can sit in a *different* commit's message than the one that changed
the line — one commit swept an entry belonging to another's work under its own
heading, leaving `git log -- <file>` pointing at the wrong reasoning for a change
that is genuinely there. Which argues for the rule this page already applies
everywhere else: **read the code, and use the message only for why.**

The *shape* is what a page records. The instance is dated, and dating it is the
whole job.

## The suite was the weaker instrument

On the quota work of 2026-09-17, **four of the last five defects were found by
running the product against the live project, not by the tests** — and the suite
was green for every one of them.

| how it was found | what it was |
|---|---|
| reading the live table | `any (global) — not creatable by this tool`: a card rule applied to a row that is not a card |
| running `quota list --json` | the JSON returned `denied/0` for a card the table called `ready (Spot) 1` — opposite verdicts, same run |
| running `quota list` twice | the default table said `denied`, `--region europe-west4` said `none`, about the same card |
| running `setup --no-quota-request` | "to ask later: `--gpu l4,t4`" — both already granted, and the missing cards unnamed |

None of these needed a clever test. They needed somebody to run the thing and
read the output. **That is not an argument against the suite** — it caught
plenty, and the mutation sweeps caught more. It is an argument about what a green
run is evidence *of*, on a feature whose output is prose about somebody else's
system: the tests assert what the code computes, and every one of those four was
a case where the computation was fine and the *sentence* was false.

**And the tests written to catch these kept passing for the wrong reason.** Seven
in two days, all of the same shape and all found by mutation rather than by
review:

- a fixture with no undrivable card, asserting that the GSP footnote is wrapped —
  a footnote that never rendered;
- a `--json` parity test driving a card whose two states were identical, so it
  could not detect divergence, while the divergence it was named for was live;
- a width guard whose bound was 120 with the widest row at exactly 120 — a
  boundary pass with no margin, so the next word added anywhere breaks the guard
  rather than being caught by it;
- a "does not re-request" test on a card that was never requested anyway;
- three that asserted the absence of the bad thing and never the presence of the
  good one.

> **Asserting that the wrong output is gone is half a test. Assert that the right
> output is there.**

The cheap habit that catches the whole family: after writing an assertion, ask
what it says when the thing it looks at **does not exist at all**. `count(x) <= 1`
and `x not in line` are both satisfied by an empty page.

And one more, for anyone extending a fixture: **a fixture that cannot reach the
branch proves nothing about it, and looks exactly like one that can.** Every one
of the seven above was a real test, correctly written, pointed at data that could
not exercise the case. Mutation found them all; reading them did not.

### The last step is to run it and read what it prints

Not a lesson — **a step**, at the end of every change, after the suite is green
and you believe you are finished.

Across one long night on the quota feature, **six defects were found this way and
none of them by a test**: a card rule applied to a row that is not a card; a
`--json` record contradicting the table beside it on three fields; two runs of
the same command disagreeing about the same card; a hint recommending quota the
user already held; a footnote omitting the card it was about; and a status column
leading with `ready` for a card that does not exist in the region. Every one was
visible in a single run of the command. Every one had a green suite around it.

The reason is worth stating, because "write better tests" does not fix it. **A
test asserts what you thought to assert.** On a feature whose output is prose
about somebody else's system, the defects are mostly in the sentences — a true
fact in the wrong column, a correct number under a misleading word, an
instruction that contradicts the row above it. Those are visible at a glance and
nearly invisible to an assertion, because an assertion has to name the thing
before it can check it.

> **Run the command. Read the whole output. Read the other surface too.**

The last clause earns its place separately. Four of the six were found by looking
at `--json` and the human table together, or two flags of the same command
together — never by looking at either alone. A defect that survives a fix almost
always survives on the surface nobody re-read.

And **quote what it printed** when you report. Describing the output from memory
of what you built is how a report comes to disagree with the product; that
happened twice in one night, and both times the product was right.

**Say which one it is.** Quoting a fixture's output is not quoting the product,
and a fixture labelled with a real region, project or card id is indistinguishable
from a live run at a glance. That happened within minutes of this section being
written: a table headed `europe-west9` came from a fixture whose invented
catalogue stocked a card that region does not sell, and the reviewer had to go and
check the live API to work out whether a new false statement had been introduced
or a test had been quoted. The code was correct; the report was not.

> A fixture wearing a real name is a claim about the world.

Six words fix it — *fixture stocking X and Y* — and they protect exactly the
reader this section is written for.

### A function tested only in isolation has untested callers

`request_value` is the only function in the quota feature that changes state at
Google. It had six direct tests covering every case of its contract, and all six
passed while `comfy-qat quota request --value 0 --allow-lower` sent `1` — because
the caller wrote `value or DEFAULT_VALUE`, which cannot tell "the user typed 0"
from "the user typed nothing", two lines below a comment explaining that the
distinction is the whole point.

Three defects that night had the same shape: the guard was right and the wiring
was wrong. A dimension matcher honoured dimensions while its caller discarded
them; a pool override was computed correctly and applied after the sort that
needed it; and this.

> When a function is correct and the behaviour is wrong, the defect is in a
> caller — and a suite that only tests the function is structurally blind to it.

Test the contract directly **and** through the seam the user reaches. Neither
replaces the other: the direct tests say what the function promises, and only the
end-to-end one says whether anybody gets it.

### A comment that justifies reversed behaviour is worse than no comment

Removing code is not removing the reasoning for it. On the quota work a rendering
branch was deleted and the comment explaining *why it skipped a case* was left
behind — attached to a `continue` that now guarded nothing, arguing persuasively,
in the codebase's own voice, for the exact rule the replacement had just
reversed. The variable it belonged to was assigned `""` and never reassigned;
four concatenation sites appended an empty string.

**Two people then described that behaviour to each other from the comment rather
than from the output**, and one of them carried it into a brief and a summary.
The rendered output had been correct throughout. The explanation everyone agreed
on was fiction, and it was fiction with a rationale, which is the part that made
it survive review.

> A comment reads as intent. A stale one is an instruction to restore the defect.

**The detection is not a grep, and it is worth saying why.** Sweeping for
comments naming identifiers the module no longer has returns mostly noise —
cross-module references, deliberately-misspelled examples, quoted test names,
historical narrative that is correct *as* history. The one real instance here was
found by reading the terminal and noticing the output did not match the stated
rule. Which is the same lesson as the section above, one more time: **run it and
read what it prints.**

What *is* mechanical: when you delete a branch, delete its rationale in the same
edit, and when you reverse a rule, make the surviving comment say the rule was
reversed and why. A retraction left in place is useful; a rationale left in place
is a trap.

### Reviewing has the same trap, one direction over

The writing rule above is *assert the presence, not just the absence*. The
reviewing rule is its counterpart, and it cost two rounds of crossed messages on
the same night.

**A claim quoted in order to retract it is not a claim, and `grep` cannot tell
them apart.** A sweep for a withdrawn sentence hits the correction that withdraws
it, hits the test named after the bug, and hits the comment explaining why the
old behaviour was wrong — all of which are the fix rather than the defect. One
reviewer re-sent six blocks that had already been fixed, because the phrase was
still findable in the file.

**Check each hit against its surrounding lines, never the line alone.** If a
sweep is worth automating, classify: a hit within a few lines of *"earlier
version"*, *"was wrong"*, *"contradicts"* or *"unverified"* is a retraction, and
everything else is a live claim. That distinction is the whole result; a raw
count of matches is noise.

The same applies to reading a diff. A file that gains the words it is removing
looks unchanged to a search and is the opposite.

## A guard protects an object, and the object has a name

Six rounds of work went into "do not lower a standing request". The seventh found
`comfy-qat quota request --gpu t4 --value 0` building a command that set a
working grant to zero: no refusal, no warning, exit 0.

Every one of those rounds was correct about the thing it guarded. `--value` was
made `Optional[int]` so a default could not overwrite a typed number; `or
DEFAULT_VALUE` was swept out because it could not tell 0 from unset; the refusal
was made to say the word "lower". All of it protected **the number in a
request** — and the thing that can actually be lost is **the quota the project
holds**. A card can hold quota with no request behind it at all. On this project
that was true of T4, L4, K80, P100, P4 and V100: six of fourteen cards, and
precisely the six that work today.

Nothing in the suite could see it, because every test of that guard used a card
that had a preference. The fixtures agreed with the mental model, so they could
only confirm it.

> Write down which object the guard defends, in the guard. If you cannot name it
> in one noun, you are guarding a step and not a thing.

The repair is not "add another check". It is to ask what the irreversible loss
actually is, and make the floor that. Here the floor became `max(standing
request, granted quota)` — and then a mutation sweep asked the question a third
time and found nothing covering an UNLIMITED grant, which `max` reads as -1, the
smallest number there is.

## A remedy that cannot work, eight times

The most frequently recurring defect in this feature was not in a computation. It
was the last line of an error message:

| the refusal | the remedy it printed | why it cannot work |
|---|---|---|
| `none — request it` | file a quota request | the card is one `create` will never accept |
| no quota in this region | `--region africa-south1` | that region sells no NVIDIA accelerator of any kind |
| refusing to lower | `--gpu NVIDIA-L4-GPUS-per-project-region` | the user typed `--quota-id`; rewritten, it exits 2 |
| `-5 is not a number of GPUs` | `--allow-lower` | nothing can permit a negative, by design |
| no such region | `quota request --region asia-east1` | no `--gpu`: exits 2 at "name a card to ask for" |
| no quota in this region | `--region <first metered>` | the same africa-south1, in the fallback branch |

Six of those six are the same command, `comfy-qat quota request`, and five of
them fail for a reason the refusal already knew. None of them is a hard bug.
Every one of them survived a suite that was green, because **no test ever ran the
remedy.**

The mechanism is always an append. A block of advice is written for the cases
that exist when it is written, and later a new cause reaches the same block and
inherits advice aimed at a different problem. `send is None` grew from one cause
to three while the remedy stayed at two, so the third took whichever branch it
fell through to.

> If your error prints a command, something must run that command. A remedy is
> output, and output that nobody executes is output nobody has tested.

The repair that finally stuck was not a seventh fix. It was a rule narrow enough
to check mechanically — *every printed `quota request` invocation names a `--gpu`
or a `--quota-id`, unless it contains a `<placeholder>`* — parametrised over ten
real refusals. It kills the reverted fixes, and it will catch the ninth instance
before a person does.

The general form is harder and worth stating anyway: the strongest version of
this test pastes the remedy back into the CLI and asserts it does not refuse.
That is not always possible — some remedies are templates, some need state the
test does not have — but wherever it *is* possible it is the only version that
cannot drift, because it stops asserting a property of the string and starts
asserting the thing the string promises.

## The remedy leads somewhere, and that somewhere is untested too

The rule from the section below — *if your error prints a command, something must
run that command* — earned its keep within ten minutes of being written, and not
where anybody expected.

A reviewer ran a refusal, got `comfy-qat quota list --by-region` as its remedy,
and ran **that**. It worked. The table it landed on did not:

* the REGION column contained `19 regions`, a count under a heading that names
  places — the same defect as `where_label` calling nine zones "9 regions",
  surviving in the one view whose entire purpose is to name places;
* `K80` read `ready` against nineteen NAMED regions, and K80 is not in GCE's
  accelerator catalogue anywhere.

Neither is in the remedy. Both are one step past it. **A remedy is a promise
about where it takes you, and the destination is part of the promise.**

The second one is the more interesting failure, because the tool was not silent
about it: the view carries a footnote saying STATUS reports quota held rather
than availability. The footnote was true, present, and the wrong answer —
its remedy is "add `--region`", which collapses the very view the reader asked
for, and the check it describes as expensive is **free here**: the catalogue is
one call for all 543 rows and every row in this view already names the region to
test against. The reasoning that justified the caveat was written for the
collapsed table and inherited by a view where its premise does not hold.

> A caveat is what you write when you cannot check. Before writing one, check
> whether this particular surface can.

Fixing it then made the caveat itself false — a note telling the reader to add a
flag to get a check that had just been performed — so the note had to become
conditional in the same change. And the rename that stopped the REGION column
printing a count broke the guard written beside it: `spans_many` was being tested
against the *relabelled* string, so every bucket row was judged against a
catalogue it cannot be tested against. Both were caught by a mutation sweep, not
by the suite.

## A rename breaks the guard standing next to it, in the same change

The hardest version of a stale assumption is the one that goes stale *inside the
commit that creates it*.

`--by-region` printed `42 regions` in a column headed REGION — a count where a
place belongs. The fix relabelled it to `any of 42`, which cannot be read as
somewhere to go. Four lines away, the code deciding whether a row could be
checked against the accelerator catalogue asked:

```python
if not stocks or spans_many(place) or place == "global":
    return None          # a bucket names no single region: not checked
```

`spans_many` matches `^\d+ regions$`. After the rename it matched nothing, so
every bucket row fell through and was judged against a catalogue it cannot be
tested against — and answered `not offered here` on the strength of it. **The
rename and the guard it broke were the same change, by the same author, minutes
apart, and the suite stayed green.**

The mechanism is worth naming because it is not carelessness. A guard that reads
a value *by its shape* has an invisible dependency on whoever produces that
shape. Nothing links them, no type says so, and a search for the guard's name
does not find the producer. Here the producer was four lines up.

> When you change what a value looks like, search for the code that recognises
> it by looking. A predicate that matches on spelling is coupled to the speller.

Two smaller lessons came out with it:

* The repair was to test the row's own place instead of the rendered label —
  **check the thing before it was formatted for a human, not after.** A label is
  for reading; a predicate should not be reading it.
* The same file still had `"regions" in where` one branch over, a hand-rolled
  copy of `spans_many` sitting beside the real one. Swapping it back kills no
  test, because the two are identical *for the current spelling* — which is the
  whole point, and the surviving mutant is documented in the code rather than
  quietly tolerated.

And there is a process note attached to this one. While proving the substring
version was fragile, the demonstration ended with `git checkout comfy_qa/quota.py`
— on a repository whose entire body of work is **uncommitted**. That reverted
1,758 lines to a 419-line HEAD in one command. It was recovered in full from a
`.bak` taken minutes earlier for an unrelated mutation sweep, which is luck
rather than method.

> `git checkout <file>` is a delete. On uncommitted work it is the only delete
> that looks like a navigation command.

## The set you compute it from is the answer you get

A view narrowed by `--region us-central1` printed `none — request it` for a card
refused in europe-west4, while two other surfaces said the refusal out loud. One
line:

```python
refused_somewhere = {row.gpu for row in rows if row.status == "denied"}
```

`rows` is already filtered by `--region`. **A set whose entire purpose is
"refused somewhere ELSE" was computed from a view with everywhere else removed.**
It could only ever contain refusals in the region being asked about — so it was
always empty in the one case it existed for, and the card fell through to the
branch that prints an instruction to file something irrevocable.

The same shape, twice more in the same pass:

* `where_label` took `max()` over its spanning labels and dropped every
  individually named region on the floor, so a card with twenty-four named rows
  and a nineteen-location catch-all reported "19 regions" — a count that
  excluded the tool's own default region, while `create` said 43 about the same
  project in the same minute.
* a bucket row was reported as unanswerable because its membership was treated
  as unknown, when a catch-all covers exactly the regions with no row of their
  own — metered minus named, both of which were already in hand.

> Before trusting a derived set, say out loud what it is supposed to contain and
> where its members would have to come from. If the source has been filtered,
> sorted, collapsed or maxed on the way in, the set cannot mean what its name
> says.

All three had passing tests. The tests fed **homogeneous inputs** — all named or
all spanning, one region or none — and the defect lives in the mixed case, which
is the only case that occurs live.

## An enumeration in a comment does not notice a new arrival

```python
# `send is None` has THREE causes and this block offered TWO remedies, so the
# third inherited whichever branch it fell through to.
```

That comment was written as part of fixing the two-remedies bug. By the time a
reviewer read it there were **four** causes: an UNLIMITED grant had been added to
the function upstream, fell into the branch labelled "the standing value could
not be read", and was told it might replace a standing request that did not
exist.

The comment was accurate when written, was the right thing to write, and still
could not do the job it looked like it was doing. Counting things in prose fixes
the count at the moment of writing.

> If a comment enumerates cases, a test must count them. Otherwise the comment is
> a snapshot presented as an invariant.

The repair was a test that names every cause and asserts each gets a remedy of
its own, so the fifth cause fails a test rather than inheriting a sentence.

## A predicate named for one question, used to answer another

```python
def _region_names(row: Row) -> set[str]:
    """Every region one row grants a non-zero allowance in."""
    if row.limit == 0:
        return set()
```

The docstring is exact. The name is not, and one caller read it as "every region
this project meters this card in". Those agree everywhere except at zero — and
**zero is the state of every card you would ever request quota for.** So the tool
answered:

    h100: africa-south1 does not offer this card — Google sells it in 20
          regions, none of them metered by this project

about a project metered in all forty-three. Worse than the false sentence: the
branch that names a region you *could* ask in was skipped with it, so the one
remedy that works — `--region asia-east1`, which exits 0 and builds a valid
preference — was withheld from the card that needed it, while a card that already
held quota got it. The tool was least helpful exactly where it was most needed.

> A function is used by its name far more often than by its docstring. If the two
> answer different questions, the docstring loses.

The repair was not a condition but a second function: `regions_with_quota` keeps
its meaning, and `regions_metered` exists to be the other question. **Three
callers meant metering and one meant granting**, and only splitting them made it
possible to say which was which — a sweep then showed two of the three had no
test at all, because every fixture in that area held quota at 1 and could not
reach the zero branch.

## A guard that only inspects what was printed cannot see what was withheld

The guard written for the remedy class checked that every printed
`quota request` line names a card. The defect above printed **no** line at all,
and passed:

```python
assert printed or "quota request" not in result.output
```

An `or` that makes absence acceptable is a guard with one branch, in the shape of
a test. Ten parametrised cases, and every one of them held quota at `1` — so not
one could reach the state where a remedy goes missing.

> A guard over output needs two questions, not one: is what was printed correct,
> and was anything that should have been printed missing? The second is the one
> that gets left out, because absence has no line to assert against.

The case list now carries a third column saying whether a remedy must exist, and
the first entry is the card at zero.

## A guard named for a rule, covering one caller of it

```python
def test_every_region_scoped_request_names_a_region(tmp_path):
```

The name states a rule about every region-scoped request. The body loops over one
`setup` run's submissions. `setup` obeyed the rule; `quota request --quota-id`
did not, and **the surface that broke the rule could not reach the test that
names it.**

This is *a function tested only in isolation has untested callers* one level up,
and the inversion is what makes it hard to see: the test was not weak, it was
**mis-populated**. Every assertion in it was correct about the rows it saw.

> When a test's name quantifies over "every X", make the collection of X the
> first thing the test builds. If it iterates one caller's output instead, the
> name is a claim about a population the test has never met.

The repair enumerates the three surfaces that can file a request and asserts the
rule on each, so a fourth caller cannot be added without appearing there.

It is the second time a correctly-named guard covered the wrong population, and
both times the name was the thing that made it invisible — a reviewer reading
`test_every_...` has no reason to check which "every" it meant.

## The third option nobody wrote down

```python
assert not any(a.startswith("--dimensions") for a in cloud.requests[0]), (
    "no region was named, so none is invented")
```

That reasoning is correct. Inventing a region the user did not type would be
wrong. But the code it protected sent the request anyway, without the dimension
the API requires — and a real submission came back `INVALID_ARGUMENT: Dimension
values must be set for all the dimensions`.

Two options had been considered, invent or omit, and the test enshrined the
better of them. The third — **refuse, and say which flag is missing** — was never
written down, so the bad half of a true statement became a pinned behaviour.

> When a test's docstring argues *against* the alternative, check that the
> alternatives were exhaustive. "Not A, therefore B" is only sound when A and B
> are the only options, and a refusal is almost always a third one.

The same shape produced the guard beside it: `if region and needs_region(id)`.
The `and` reads as caution and means the check cannot run in the only case it
exists for.

## Test the guard against the defect that motivated it, reintroduced

Four guards were written in two nights against defects that had just been found.
Every one of them was scoped correctly, argued correctly, and named correctly.
**Three of the four could not see the instance they were written for.**

| guard | why it missed its own subject |
|---|---|
| retracted claim must not ship | searched line by line; the sentence spans three source lines and two adjacent string literals |
| no doubled word in a user-facing string | got the scope right on the third attempt, then `\b(\w+) \1\b` could not match `Ask ask` — case-sensitive |
| every printed remedy names a card | checked the shape of what WAS printed; the defect was a remedy that was never printed at all |
| every surface names the region | enumerated all three surfaces and handed each one an explicit `--region` — and the bug only fires when the region is absent |

The last is the sharpest, because it was written *as the fix* for a guard that
covered the wrong population, and it repeated the error one level in: right
population, wrong inputs.

None of these is subtle in hindsight and none was visible at the time, because a
guard is written while looking at the *shape* of the defect — a doubled word, a
retracted sentence — and the shape is what gets encoded. The **instance** has
properties the shape does not: it wraps, it capitalises, it is absent rather than
malformed, it needs a flag omitted.

> A guard is not finished when it passes. It is finished when you have put the
> original defect back, watched it fail, and taken the defect out again.

That is one command and it caught three of four. The synthetic example you would
otherwise reach for is the same shape as the defect and, by construction, is the
version that fits the guard you just wrote.

## The surface that kept its own copy is the one that broke

A region check was unified across three commands so they could not disagree.
`quota list` kept a local copy — six lines, built from `applicableLocations` —
and that is the one that let a ZONE through:

    $ comfy-qat quota list --region us-central1-a
    exit=0
    GPU            LIMIT  WHERE   STATUS
    any (global)       1  global  ready

Six cards are granted at 1 on that project. The local set folds in
`applicableLocations`, and `-per-project-zone` rows carry zone names, so
`us-central1-a` was a member of the universe it should have been refused by.

Two things are worth separating. The **first** is ordinary: unify a rule and the
holdout is where it fails. The **second** is not — **a confident wrong answer
beats a refusal into a script every time.** The typo path exits non-zero and
names the problem; this one returned exit 0 and an empty table, which is
indistinguishable from "you hold nothing" and is the shape a person believes and
a pipeline propagates.

> Rank output failures by what a caller does next. A refusal is read. A wrong
> answer is used.

The fix was to delete the local check and call the shared one — not to add a zone
case to it. Adding the case would have left two implementations of one question
and put the next divergence one release away.

## An optional read is not an optional behaviour

`create` refused a card with "ask, then wait for Google" while `quota list` said
"asking again will not help" about the same card in the same minute — and the
command it printed derived the very region the refusal was made in.

`create` could not know: it reads quotas and never read preferences. Adding that
read is three lines, and it immediately hit two fakes whose `__getattr__` raised
`create asked the fake for 'quota_preferences'`. That guard is worth naming as a
good one: it turned a silent new dependency into a decision at the moment it was
introduced, in two test files, before it reached a review.

The read is **optional in the only sense that matters** — `None` means "could not
be read", and it may only ever remove advice, never add a refusal. A create that
fails because a secondary lookup failed would be a worse defect than the one
being fixed.

> When a command gains a read it did not need before, decide what it does when
> that read fails BEFORE deciding what it does when it succeeds.

## Absent versus zero, one level up: the computation that never ran

Nine rounds of review kept producing the same family of defect, and the eighth
named the generator. It is not a value that is missing versus a value that is
zero. It is a **computation that never ran** versus one that ran and found
nothing — and every property of the first case applies: they are different facts,
one of them means "I do not know", and a surface that cannot tell them apart will
take the cheerful reading.

```python
stocks: dict[str, set[str] | None] = {}      # the default
if by_region and not region:                 # computed under ONE flag
    stocks = _regions_stocking(...)
...
if per_region and stocks:                    # consumed under ANOTHER
```

`--json` emits the `by_region` array **unconditionally**; `--by-region` decides
whether its input is gathered. So `comfy-qat quota list --json` shipped 140 rows
whose availability verdict was `null`, and the table beside it in the same run
said `not offered here` for 25 of them. The machine surface — the documented one,
the one a script gates on — reported K80 ready in twenty-five regions, for a card
that exists nowhere in Google's catalogue.

**`{}` was spelling two different facts.** No amount of care at the call site
fixes that, because the expression `if ... and stocks` has nothing to read.

### The repair is two rules, and neither is a patch to the call site

**Make "not computed" unrepresentable as "computed and empty."** A small frozen
type with a `looked` flag, a `not_checked()` constructor that is the only way to
build the first state, and an accessor that refuses to answer from it. An empty
result now unambiguously means "looked, found nothing".

**Gate the producer exactly as the consumer is gated.** A field that appears in
the output while its input was conditional is the bug. Either compute it whenever
it is emitted, or do not emit what was not computed — the flag decides both or
neither.

> A default value is a claim. If the code cannot distinguish the default from a
> real result, the default is a lie waiting for the first caller who skips the
> computation.

### Sweeping for the shape rather than the symptom

The question is mechanical, so ask it mechanically: **every name given a falsy
default at function top level, reassigned inside a conditional, and read outside
it.** An `ast` walk over the package found 28 candidates; restricting the
conditional to `if` — a *mode* test, not a loop, because a `for` accumulator's
default IS its answer when nothing matched — left six, and reading them left
**one**.

That one was `quota request`'s own `sells: dict = {}`: the identical shape, and
*correct*, because its consumers happen to be gated on the same flag as its
producer. That is an agreement between two call sites, not a property of the
data, and agreements like it are precisely what had been breaking. It is the same
type now, so the agreement has stopped being load-bearing.

**Reporting the search matters as much as the result.** "I found no more" is only
useful with the method attached, because the next reader needs to know what was
asked, not just what came back.

## The sweep is the deliverable, not the patch

**Ten rounds of adversarial review found instances. Two mechanical sweeps found
the classes.** Both sweeps turned up a defect the round's report did not have,
and both times it was the *sibling* of the reported one — which is a better
description of this codebase's failure mode than anything in the findings
themselves.

| sweep | the question, asked of every function | reported | sweep also found |
|---|---|---|---|
| data flow | a falsy default at top level, reassigned inside a conditional, read outside it | `stocks`, gated on `--by-region`, emitted by `--json` | `sells` — same shape, correct only because its consumers share its producer's flag |
| control flow | an early exit at top level with a guard-shaped call after it | the region check below `if not submit:` | the availability check below the same return, printing a remedy that exits 2 |

Neither sweep is clever. Both are one `ast` walk and an afternoon of reading the
candidates — 28 narrowed to 6 narrowed to 1, and 18 narrowed to 1. **The reading
is the work**, and it cannot be skipped: the narrowing rules exist to make the
list short enough to read, not to decide anything.

> When a defect recurs, stop fixing instances and write down the question that
> finds them. Then answer it mechanically, read every candidate, and say what you
> found — including "nothing else", which is only worth anything with the method
> attached.

What follows is the class the second of those sweeps closed.

## The second-site class, and how to make it unrepresentable

Nineteen corrections in this feature landed on one surface and missed its
sibling. The eighth round closed one generator of that (a computation gated on a
flag its consumer was not); the ninth named the other, and it is about **entry
paths**:

* **a guard reachable from one entry point and not another**, and
* **a validation below an early return**, so what gets checked depends on which
  flags were passed.

The instance: `quota request --gpu l4 --region africa-south1` was refused with a
remedy, while `--quota-id NVIDIA-L4-GPUS-per-project-region --region
africa-south1` — the identical request — built a permanent preference for a
region that sells no NVIDIA card. The card-keyed guards were seeded from `--gpu`
alone, so the raw-id path handed them an **empty name set**, and an empty set
reads as "nothing to check" rather than "I was not told what to check".

That is absent-versus-zero a third time, now in the guard's *population* rather
than in its data.

### The fix is not a second call to the guard

A raw quota id already carries a card identity — `friendly_name` reads `L4`
straight off it. Resolving both flags to **one internal shape at the entry
point**, before any guard runs, makes the whole family unrepresentable: not only
this pair, but the next one somebody introduces by adding a guard to the `--gpu`
path and forgetting the other.

> Two entry points that mean the same thing should stop being two before
> anything reads them. A guard added later then cannot be added to only one.

And for early returns: **every validation goes above every return**, including in
functions whose callers already validate. `ensure_quota_requests` checked the
region below its `if not submit:` return; hoisting it kills no test, because
`run_setup` happens to check first — which is precisely the class. A guard that
appears to work because another caller checks first is a guard that will be
missed the day someone calls it directly.

### Sweeping control flow the way we swept data flow

The data-flow sweep asked: *a falsy default at top level, reassigned in a
conditional, read outside it.* The control-flow sweep asks: **an early exit at a
function's top level with a guard-shaped call after it.** An `ast` walk gave 18
candidates across the package; reading them left **one** real instance — and it
was not in the report.

`setup --no-quota-request --region africa-south1` printed `To ask later:
comfy-qat quota request --gpu h100 --region africa-south1`, and that command
exits 2, because the availability check sits below the same early return the
region check did. Twin of the reported defect, one function over, found by asking
the question mechanically rather than by reading the report again.

## A false sentence about the tool's own next action

Every other misstatement in this feature was about the world: a count that was
not a count, a region that sold nothing, a claim the schema did not support. This
one is different in kind:

```
$ quota request --gpu l4,t4 --quota-id NVIDIA-A100-GPUS-... --region us-central1
warning: --gpu l4,t4 ignored: --quota-id names the quota exactly
...update comfyqat_nvidia-a100-gpus-...   <- the --quota-id
...update a0e3b926-...                    <- L4, an EXISTING granted preference
...update comfyqat_nvidia-t4-gpus-...     <- T4
```

Three permanent requests, two of them for the cards it had just said it was
ignoring, one by reaching into a live granted preference. **A warning that does
not change behaviour is worse than no warning**, because it tells somebody they
have been protected from the thing that is about to happen — and unlike a false
claim about the world, there is nothing they can check it against except by
watching what the tool does next.

The warning was added a round earlier, for a real reason: the tool refuses four
kinds of wrong region and was mute about an ignored one, so a notice seemed
proportionate. It was applied to a second flag by analogy, and there the honest
answer was not a notice but a refusal — the command genuinely cannot know which
of two contradictory arguments was meant.

> A message that describes what the tool is about to do is a test assertion
> written in prose. Assert it in a test, on what was SENT, or do not write it.

The test that covered it asserted the word "ignored" appeared. It never asserted
that anything was ignored, so it passed throughout.

### The same round, twice more

**A fix reported done, commented as done, and pinned by a passing test whose
docstring quotes the defect — with the defect still live.** The region check had
been hoisted above every early return *in one function*; the command calls that
function one frame down, and the caller ran an earlier step with the unvalidated
value. Every word of the comment was true of the function and false of the
command.

**And a remedy that rewrote `--quota-id` to `--gpu`, inside the block whose own
comment enumerates six prior instances of that exact shape and ends by naming
it.** Six branches in that block preserve the flag the user typed; one hardcoded
`--gpu`, on the path where `name` is a raw quota id.

Neither is carelessness, and that is the point worth keeping: a comment counting
its own recurrences does not prevent the next one, and a test whose docstring
quotes the bug does not prove the bug is gone. **Only running the command does.**

## Composing an answer that a shared function already gives

The last defect found in this feature, after ten adversarial rounds, was
`create` printing:

    to fix: comfy-qat quota request --gpu h100 --region africa-south1
            # or africa-south1, asia-east1, asia-east2

`africa-south1` sells no NVIDIA accelerator, so that command exits 2 — and it is
named twice in its own alternatives, which tells the reader the list was checked
when nothing checked it.

The helper that answers this had existed for six rounds. `create` composed the
list itself from `regions_metered` alone, and **metered is where a request is
possible; metered AND STOCKED is where a granted request buys something that can
start.** One of those is a remedy and the other is a guess.

> A second implementation of a question you have already answered is a
> second-site with a delay fuse. It is right on the day it is written, and it
> does not receive the next correction.

The third sweep asked: **which functions emit a `comfy-qat …` suggestion without
calling any of the shared answers?** Nine candidates once docstrings were
excluded — every function that *explains* this feature quotes a command, so
including them gave 34 and no signal. Reading the nine left one more the report
had not found: a zone-typo refusal that ran `region_of` over the mistyped zone
and suggested asking for the card in the resulting region, which is not a region
at all. The comment two lines above it warns about exactly that trap.

**Three sweeps, three classes, and every one of them found something the round's
report did not:**

| sweep | question | also found |
|---|---|---|
| data flow | falsy default, reassigned in a conditional, read outside it | `sells` |
| control flow | early exit with a guard after it | the availability check below the same return |
| composition | a printed suggestion built without the shared answer | the zone-typo remedy |

The composition sweep also shows the limit of the method: it flags
`_ask_somewhere`, which is *correct* — its caller passes the shared answer in.
The sweep cannot see a value handed over as an argument, only a call made by
name. **A sweep narrows a list to something readable; it does not decide.** Every
one of these rounds ended with reading the candidates by hand, and that is where
each of the three real findings came from.

## A count is a claim

Three numbers in one night, each printed beside the word "regions", none of them
a number of regions:

```
never asked in 172        # 43 regions, counted across a region row and a zone row, summed
this project's quota names 174   # 43 regions plus ~130 zones plus "global"
RTX-PRO-6000 ... refused in 2 regions   # beside "ready (Spot)", and Spot had no refusals
```

Each was arithmetically correct on the set it was handed. The defect is in the
sentence: `len(places)` is only a count of regions if `places` holds regions, and
a count printed next to a noun asserts that noun. `applicableLocations` holds
zones for a `-per-project-zone` row and `global` for the ceiling, and the third
one attributes a refusal to a pool that has never been refused.

> A number in a sentence is a claim about the noun beside it. Check the set, not
> the arithmetic.

The last of those is the sharper case, because the fix for it had already been
made: the `where` column had been corrected a round earlier to stop showing the
on-demand geography beside a Spot status. The counts sitting two fields away were
left behind — which is *A correction that lands in one place reads as done*,
again, on the same row of the same table.

## A rationale outlives the premise it was built on

```python
# THE NAME `quota list` SHOWS. `self.card` is the display name — `H100-80GB` —
# and the quota table has an `H100` row and no `H100-80GB` row, so a reader
# following this sentence to the table found nothing by that name.
quota_name=card.quota_names[-1],
```

Every word of that was true when it was written. Then a different fix made
`quota list` print the card table's own name, and the comment went on justifying
code that now produced the defect it was written to prevent: `create --gpu h100`
printing `H100-80GB: 0` and `this project has no H100 quota` four lines apart.

A comment that argues for behaviour is load-bearing in a way a descriptive one is
not: it tells the next reader the line is deliberate, so they leave it alone. The
argument has a premise, and nothing in the repository was watching the premise.

> When you change a fact, grep for the sentences that assert it — not just for
> the code that reads it.

The test guarding this had the same shape. It asserted the literal string
`H100`, so when the rule's *spelling* changed the test failed while the rule
itself was being honoured. It now derives the expected name from the same
function the table renders with, and can no longer tell one from the other
wrongly.

## Do not run the product against a tree something else is editing

A mutation sweep was running in the background — apply a mutant, run the suite,
restore from a `.bak` snapshot — while the product was being run against the live
project in another shell, and while edits were being made to the same files.

Three things went wrong at once, and all three looked like results:

* the sweep's restore overwrote four unrelated edits made after its snapshot;
* the live run produced output for a tree that was mid-mutation, showing a check
  passing that had been switched off seconds earlier and back on seconds later;
* killing the sweep mid-cycle left a mutant in the working tree, where a normal
  suite run would have reported it as a test failure of unknown origin.

None of it was subtle once seen, and none of it announced itself. The output was
plausible in every case.

> A background job that writes to the working tree is a second author. Run it
> alone, or do not run it.

## The instrument failed the same way twice in one session

Class 4 is *an instrument with only one branch*. It happened twice on the same
night, in the same harness, for the same reason:

```sh
SEL="tests/a.py tests/b.py"
pytest -q $SEL          # zsh does not word-split: ONE argument, a path that does not exist
```

pytest collects nothing, prints no line beginning `FAILED`, and the harness
reports **SURVIVED** for every mutant — a clean sweep, which reads exactly like
success. The first time, the fix was to use an array and add a control mutant
that must be killed. The second time, the array had not been carried across to a
new copy of the harness.

> A harness that can only report one outcome has not been tested. Prove it can
> say the other thing, in the same run, every time you write one.

The control now sits in the sweep itself: a docstring-only mutant that must
SURVIVE beside real mutants that must be KILLED. A sweep where everything dies is
as suspect as one where nothing does.

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
