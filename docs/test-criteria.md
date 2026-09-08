# Test criteria — release 1, end to end

A tester who did not write this tool installs it from nothing and runs it top to
bottom on a real Google Cloud project. Every step is a copy-paste block; every
block prints its own markers, so the whole terminal can be pasted back as the
result.

**How to run it.** Paste one block at a time, in order. Later phases depend on
earlier ones. If a block fails, paste the terminal and stop there; that is a
result, not a wasted run.

Phases K, E, L, M, F and J **start a cloud GPU box and bill for it**. Phase G stops
it. Do not leave the run half finished overnight.

## What has already been run

A full pass went against a real Google Cloud project on **2026-08-27**, by someone
who did not write the tool. Rather than repeat it, this is what it covered:

| | |
|---|---|
| **passed** | phases A–D, G, H and I in full; plus E4, F1, F2, J1–J9 and J12–J14 |
| **passed on the path that does nothing** | **D4, F1 and F2** — the only ticks `open`, `up` and `go` carry. Read the next section before you carry any of the three forward |
| **not run** | J10/J11, and all of phase M — they need quota for **two GPU boxes at once**, and this project's `GPUS_ALL_REGIONS` ceiling is 1 |
| **not run** | phase K (`create`) and phase L (`logs`) — both landed after that pass, so nothing in them has ever been run |
| **not run** | E1, E2, E3, E3b, E5, E6, E7, F3–F8, J15 — see the note on each |
| **never had a criterion** | phase S (`ssh`, `rdp`, `disconnect`) and phase N (`delete`). Four commands, added here because the pack could not see them — one of which destroys a machine |
| **never run at all, by anyone** | phase R (`move`). A real move end to end on hardware has never happened; everything believed about the command comes from tests. It is the largest untested surface in the tool |

Two things to hold on to. **Everything about `create` and `logs` is unverified**,
which makes phases K and L the point of the next run rather than a formality. And
**E3 and E7 have been rewritten**: they described a `go` that streamed ComfyUI's
log onto your terminal and a Ctrl-C that stopped it, and `go` no longer does
either. A tick against the old wording would not have meant anything.

The three phases that did not exist before are **K** (`create`), **L** (`logs` and
the two ways of watching a launch) and **M** (two boxes up at once, which is what
detaching bought). K runs before E because E needs a box.

Two more are new here, for a different reason. **S** (`ssh`, `rdp`, `disconnect`)
and **N** (`delete`) cover four commands that had **no criterion anywhere in this
pack** — so a tester could complete the whole thing, sign off release 1, and never
once exercise the only command in the tool that cannot be undone. They were
missing because nothing failed when they were left out. Nothing here cross-checks
the criteria against the binary; until something does, the check is you.

## Three commands that passed on the path that does nothing

Read this before you carry anything forward. **`open`, `up` and `go` are all
recorded as passed, and in each case the code the criterion named never ran.**

| | what was ticked | what it actually exercised |
|---|---|---|
| `open` | **D4**, `qat open local` | the early return. `open` prints `local — there is nothing to tunnel` and returns **before `open_tunnel` is called** — no port check, no `ssh -L`, no identity record. The run of `open` is now **E6d** |
| `up` | **F1**, on a box already up | `bring_up` read RUNNING and skipped `start_instance`, the boot wait, the ssh wait, the stockout branch and the interrupt guard. That path is now **F1b**; the run of `up` is **F0/F1/F1a** |
| `go` | **F2**, `--no-install` on a box that had ComfyUI | `_serve` returned on the stamp before `no_install` was read — the flag could have done anything. The install is now **E3/E3c**; the flag is **E0b** |

Each tick was honest. What was missing in all three is the same thing this pack
audits everywhere else: **an observation the failure could not also produce.** A
criterion whose expected result is printed on both branches grades nothing, and
three of them sat in the phases that cost money.

## The nine commands nothing has ever run, sorted by what they cost

`move`, `create`, `logs`, `ssh`, `rdp`, `disconnect`, `delete`, `discover` and
`quota request` have never been run against real hardware. They do not all need
it. Sorting them is the difference between a nine-command backlog and a
forty-minute evening:

**Offline. No gcloud, no project, no money — run these tonight.** Each is refused
from the host list alone, before a `Gcloud` object is built, so a throwaway
`--config` is enough and no cloud credentials are needed:

| command | checks | why it is free |
|---|---|---|
| `delete` | N1, N2, N3, N4, N5 | resolved and refused by name against the host list; `Gcloud()` is constructed only after the state read, which none of these reach |
| `ssh` | S1a, S1c | `local` and a Windows box are both refused before `Gcloud()` |
| `rdp` | S1b, S1d | same shape: not a Windows cloud box, refused first |
| `logs` | L5a | the `kind == "local"` branch of `read_logs` raises before any gcloud call |
| `disconnect` | S0 | closes local tunnel records, then the local branch returns `idle` — nothing is contacted |
| `move` | R2, R2c | `--dry-run` with no `--to` is refused inside `_zone_with_capacity`, which is the guard, and `move local` never gets that far |
| `create` | K3, K4, K4d | `plan()` is offline and total and runs **before** the project read and the minute of quota — a bad card, a bad OS and `--zone` with `--region` are all string checks |

**Cloud reads. Needs `gcloud auth login` and a project; costs nothing and creates
nothing.** No box required, so these can be run straight after phase C:

| command | checks | what it reads |
|---|---|---|
| `discover` | E1 | one `instances list` on the project. Nothing written under `--dry-run` |
| `create` | K1, K2, K4b, K4c | the project, the instance list, and about a minute of quota |
| `quota request` | C9, C9b, C9d | C9 reads only; C9b files a real request, which costs no money and cannot be withdrawn |

**Needs hardware, and it bills.** There is no cheaper route to any of these, and
every one of them leaves something running or something deleted:

| command | checks | cost, and what to check when it ends |
|---|---|---|
| `create` | K5–K7 | creates a GPU box. It bills from K5 until phase G. Check `gcloud compute instances list` reads TERMINATED |
| `logs` | L1–L3, L5b, L7 | needs a box that has served. L5b needs it stopped — free once it is |
| `ssh` | S2, S2b | a running box. Reading only; the bill is the box |
| `rdp` | S3 | a **running Windows** box, so a second OS and a second create |
| `disconnect` | S4–S6 | deliberately leaves the box billing. S4b is the criterion that it says so |
| `delete` | N6, N7, N7b, N9–N15 | destroys a machine and its disk, and cannot be undone. N11 and N14 are what tell you the money stopped |
| `move` | R0b, R3, R4–R9 | the largest untested surface here: a snapshot, a 300 GB disk and a second instance. R7 and phase I are the accounting |
| `quota request` | C9c | no money, but it holds the terminal for up to 30 minutes and files a request a human reads |

**Nothing in the first two tables needs a box, a card, or a night.** If time is
short, they are the cheapest ticks in the pack and they are the ones nobody has
ever taken.

## The preamble

Paste this once per terminal. It sets the four paths everything else uses and
gives you a `qat` command that says so plainly if the tool is not installed yet,
instead of a wall of shell errors. Phase E used to need a second terminal, because
`go` held the first one; it does not any more.

```sh
VENV=~/ComfyUI/venv                  # the virtualenv to install into
REPO=~/qa-tools-test                 # a fresh clone, so nothing existing is in the way
QAT=$VENV/bin/comfy-qat
PY=$VENV/bin/python
qat() { [ -x "$QAT" ] || { echo "STOP - not installed. Run phase 0."; return 127; }; "$QAT" "$@"; }
echo "venv $( [ -x "$PY" ] && echo ok || echo MISSING ) - tool $( [ -x "$QAT" ] && echo installed || echo "not installed yet" )"
```

Any virtualenv on Python 3.11+ works; `~/ComfyUI/venv` is 3.12 and already here.

---

## Phase 0 — from scratch *(no cost)*

Removes every existing install and your current host list, so phases B and C test
the real first-run path rather than a machine that was already set up.

```sh
echo "=== 0.1 remove any existing install"
$VENV/bin/pip uninstall -y comfy-qa-tools comfy-qa-cli comfy-qa 2>&1 | tail -4
echo "=== 0.2 no binary left behind"
ls $VENV/bin | grep -i "comfy-qa" || echo "clean"
echo "=== 0.3 set your host list aside (restoring it is one command, at the end)"
[ -e ~/.config/comfy-qa-tools ] && mv ~/.config/comfy-qa-tools ~/.config/comfy-qa-tools.before-test
ls -d ~/.config/comfy-qa-tools* 2>&1
echo "=== 0.4 fresh clone of main"
rm -rf "${REPO:?}" && git clone -q https://github.com/Comfy-Org/comfy-qa-tools.git "$REPO" && git -C "$REPO" log --oneline -1
echo "=== 0.5 install exactly as the README says"
cd "$REPO" && $VENV/bin/pip install -e . 2>&1 | tail -3
echo "=== 0.6 does it run"
"$QAT" --help >/dev/null 2>&1 && echo "binary ok at $QAT" || echo "FAIL - no binary at $QAT"
```

- [ ] **0.1** — uninstalls without touching anything else. A "not installed" for
      the two old names is a pass, not a failure.
- [ ] **0.2** — nothing matching `comfy-qa` is left in the venv's `bin`.
- [ ] **0.3** — your real host list is safe at `~/.config/comfy-qa-tools.before-test`
      and the live path no longer exists.
- [ ] **0.4** — the clone is at the same commit as `origin/main`.
- [ ] **0.5** — installs from the checkout with no build error.
- [ ] **0.6** — `binary ok`. **If this fails, stop: nothing after it can pass.**

Now re-run **the preamble** so `qat` picks up the new install.

---

## Phase A — install and surface *(offline, no cloud, no cost)*

```sh
echo "=== A0 what am I testing"; qat --version
echo "=== A1 clean import"; qat --help 2>&1 | head -20
echo "=== A2 no stale binary"; which comfy-qa-cli; which "comfy-qa"; echo "exit $? (1 = clean)"
echo "=== A3 the surface"; qat --help 2>&1 | sed -n '/Commands/,$p'
echo "=== A4 quota surface"; qat quota --help 2>&1 | sed -n '/Commands/,$p'
echo "=== A5a the old spellings still work"; qat host --help 2>&1 | sed -n '/Commands/,$p'
echo "=== A5b and so does the other one"; qat auth --help 2>&1 | sed -n '/Commands/,$p'
echo "=== A5c and env, which is hidden too"; qat env --help >/dev/null 2>&1; echo "exit $? (0 = reachable)"
echo "=== A6 guide"; qat guide
cd "$REPO"; echo "=== A7 tests"; "$PY" -m pytest tests/ -q 2>&1 | tail -3
echo "=== A8 no message offers the old spelling"
grep -rn "comfy-qat host \|comfy-qat auth " comfy_qa/; echo "exit $? (1 = clean)"
echo "=== A8b the same grep, pointed at a spelling that IS there"
grep -rc "comfy-qat down " comfy_qa/ | grep -v ":0$"; echo "exit $? (0 = the grep can see)"
```

- [ ] **A0** — prints `comfy-qat <version> (<sha>)`. **Record this line — every
      result in this run is a result about that build, and a report without it is
      a report about nothing.** From an installed copy the sha is absent, which is
      correct: a wheel is not a checkout.
- [ ] **A1** — help prints; no traceback, no import error.
- [ ] **A2** — neither older binary is on `PATH` from this project. Both `which` calls come back empty.
- [ ] **A3** — the top level lists exactly these 21, in this order: list, init,
      discover, create, up, open, disconnect, down, go, ssh, rdp, logs, switch,
      move, stamp, status, login, delete, setup, guide, quota.
      **`host`, `auth` and `env` must not appear.** One way to do each thing, not
      two — that is what moving the verbs up was for. Count them: a command the
      binary has and this line does not is not a pass, it is this criterion going
      stale again, and the last time it did a correct build was marked FAILED.
- [ ] **A4** — `quota` lists exactly: list, request.
- [ ] **A5a/A5b** — `host` and `auth` still run and still list their subcommands,
      so nothing written down before the move breaks. They are a deprecation
      window: reachable, not advertised.
- [ ] **A5c** — `env` exits 0. It belongs to a different tool and is hidden rather
      than removed, because it is the only way to check which build a deployed
      environment is serving. Hidden must not mean gone.
- [ ] **A6** — the guide names `comfy-qat setup` first, then `comfy-qat list` and
      `comfy-qat status` — the short spellings. If it still says `host list` or
      `auth status`, that is a fail.
- [ ] **A7** — every test passes.
- [ ] **A8** — **no message the tool prints offers the old spelling.** The grep
      prints nothing and **exits 1**. It is a grep and not a judgement call
      because the old forms are a deprecation window: a tool that still teaches
      the spelling it is retiring never finishes retiring it.
      **This passes as of `442f867`.** The three sites the pack used to name —
      `auth.py` twice (`comfy-qat auth quota request ...`) and `gcloud.py` once
      (`comfy-qat auth status ...`) — are gone, and the pack said "this fails
      today" for long enough that a tester following it recorded a fail against a
      build that passed. Any hit here now is a **regression**: write it up with
      the file and line numbers the grep prints, and say which commit you ran.
- [ ] **A8b** — **the grep can still find something.** This is the criterion, not
      A8. `grep … || echo "clean"` — which this block used to be — prints
      `clean` when the pattern is absent, when `comfy_qa/` is not there, and when
      `cd "$REPO"` in A7 failed and you are somewhere else entirely. Three
      worlds, one word, and only one of them is a pass. A8b runs the same grep
      against `comfy-qat down `, a spelling the tool really does print, and it
      must come back with a **count against several files** and exit 0 — seven of
      them on `442f867`, and the number is not the check; a non-empty result is.
      **A8 is a pass only if A8b found lines**; if A8b is empty you are not in
      the checkout and A8 graded nothing.

*Ran 2026-08-27 and passed — but against the old command tree, so **A3, A4 and A5
are new wording and have not been run**. A0–A2, A6 and A7 stand.*

## Phase B — the host list rules *(offline, no cost)*

Uses throwaway host lists in a scratch directory, so your real one is untouched.
Each rule gets its own file: a check that shares a file with the check before it
can pass for the wrong reason.

```sh
T=$(mktemp -d); echo "=== B0 scratch $T"
P=$(gcloud config get-value project 2>/dev/null); echo "=== B0b project $P — read, never invented"
echo "=== B1 init"; qat init --config $T/hosts.toml; head -14 $T/hosts.toml
cp $T/hosts.toml $T/hosts.before
echo "=== B2 init refuses to clobber"; qat init --config $T/hosts.toml; echo "exit $?"
echo "=== B2b and the file is byte-for-byte what it was"; diff $T/hosts.before $T/hosts.toml && echo "unchanged"
echo "=== B3 list"; qat list --config $T/hosts.toml
echo "=== B4 bare host == list (the old spelling, still there)"; qat host --config $T/hosts.toml
gce() { printf '[hosts.%s]\nkind = "gce"\nos = "Ubuntu 22.04"\ngpu = "L4"\ngce_instance = "%s"\ngce_zone = "us-central1-a"\ngce_project = "%s"\nport = %s\n\n' "$1" "$1" "$P" "$2"; }
{ echo '[hosts.local]'; echo 'kind = "local"'; echo 'port = 8188'; echo; gce bad 8188; } > $T/b5.toml
echo "=== B5 cloud host on 8188 is refused"; qat list --config $T/b5.toml; echo "exit $?"
{ echo '[hosts.local]'; echo 'kind = "local"'; echo 'port = 8188'; echo; gce one 8190; gce two 8190; } > $T/b6.toml
echo "=== B6 duplicate port is refused"; qat list --config $T/b6.toml; echo "exit $?"
{ echo '[hosts.local]'; echo 'kind = "local"'; echo 'port = 8188'; echo; printf '[hosts.typo]\nkind = "gce"\ngce_zoen = "us-central1-a"\nport = 8190\n'; } > $T/b7.toml
echo "=== B7 typo'd field is refused"; qat list --config $T/b7.toml; echo "exit $?"
{ echo '[hosts.nokind]'; echo 'port = 8190'; } > $T/b8.toml
echo "=== B8 missing kind is refused"; qat list --config $T/b8.toml; echo "exit $?"
{ echo '[hosts.local]'; echo 'kind = "local"'; echo 'port = 8188'; echo; printf '[hosts.noport]\nkind = "gce"\nos = "Ubuntu 22.04"\ngpu = "L4"\ngce_instance = "noport"\ngce_zone = "us-central1-a"\ngce_project = "%s"\n' "$P"; } > $T/b9.toml
echo "=== B9 cloud host with no port is refused"; qat list --config $T/b9.toml; echo "exit $?"
echo "=== B10 missing file"; qat list --config $T/nope.toml; echo "exit $?"
```

- [ ] **B0b** — **no fixture on this page invents a project id.** Nothing in phase
      B reaches Google, so a made-up id would be inert here — but the pack is
      followed literally by a person, and a page that teaches inventing one teaches
      it for the phases that do reach Google. Read it with `gcloud config
      get-value project`, never type one.
- [ ] **B1** — writes the file and says where; the starter has `local` on 8188.
- [ ] **B2** — refuses, and names `--force` as the way to do it deliberately. Exit 2.
- [ ] **B2b** — **the file is unchanged, and you looked.** `diff` prints nothing
      and says `unchanged`. This was one clause of B2 — "Nothing overwritten" —
      with no step that could have noticed if it had been: a tester read the
      refusal and ticked the claim about the file, which the refusal is not
      evidence for. It is free, offline, and it is the difference between
      believing `init` and checking it.
- [ ] **B3** — a table with NAME KIND OS GPU URL.
- [ ] **B4** — bare `host` prints the same table as B3, and accepts `--config`.
- [ ] **B5** — names host `bad` and port 8188 and the local ComfyUI. Exit 2.
- [ ] **B6** — names `one` and `two` and the shared port 8190. Exit 2.
      **Not the 8188 message** — that would mean it stopped at an earlier rule.
- [ ] **B7** — names `gce_zoen` as an unknown field. Exit 2.
- [ ] **B8** — says `kind` must be `local` or `gce`. Exit 2.
- [ ] **B9** — says a `gce` host needs an explicit port. Exit 2.
- [ ] **B10** — says there is no host list and how to make one. Exit 2.
- [ ] **B11** — every message in B5–B10 is findable in
      [troubleshooting.md](troubleshooting.md) by pasting it in.

*Ran 2026-08-27 — **all of phase B passed**.*

## Phase C — sign-in, billing, quota *(cloud reads only, no cost)*

**Precondition, and it is a STOP.** Every check from here on reads Google Cloud,
and a Workspace session expires on a policy you do not control — which is exactly
what killed the first attempt at this pass. Sign in first, in *your own* terminal,
because the prompt cannot appear in a captured one:

```sh
gcloud auth login
```

**This block takes about six minutes.** Each quota call is a fresh fetch of every
compute quota on the project — roughly a megabyte, about 95 seconds — and there
are four of them. That is expected, not a hang.


```sh
echo "=== C1 status"; qat status
echo "=== C2 status json"; qat status --json | head -30
echo "=== C3 login prints, never signs in"; qat login
echo "=== C4 quota (about a minute)"; time qat quota list
echo "=== C5 one region"; qat quota list --region us-central1
echo "=== C6 by region"; qat quota list --by-region 2>&1 | head -15
echo "=== C7 quota json"; qat quota list --json 2>&1 | head -20
echo "=== C9 asking for a card, without asking"; qat quota request --gpu l4 --region us-central1 --dry-run; echo "exit $?"
```

`--dry-run` is not this command's default. **`--wait` is, and it is on**, so the
form above is the one path in `quota request` that submits nothing and returns at
once — which is why C9 has been the only criterion here. The default submits, then
holds the terminal polling every 30 seconds for up to 30 minutes. Run C9b and C9c
**on their own**, deliberately, once you have read what they do:

```sh
echo "=== C9b submit, and do not wait"; qat quota request --gpu l4 --region us-central1 --no-wait; echo "exit $?"
echo "=== C9c the default: submit, then wait. Up to 30 minutes."
time qat quota request --gpu l4 --region us-central1; echo "exit $?"
echo "=== C9d what it left"; qat quota list --region us-central1
```

- [ ] **C1** — one line per check: gcloud, account, project, billing, GPU quota.
      Stops at the first failure rather than printing five. **If this fails, stop:
      nothing else in phases C to G can pass.**
- [ ] **C2** — valid JSON, same facts, **no credential or token anywhere in it**.
- [ ] **C3** — prints commands for you to run; does not open a browser.
- [ ] **C4** — one row per card with LIMIT / WHERE / STATUS; warns first that it
      takes about a minute; finishes well under 240s. *(Measured live: 95s.)*
- [ ] **C4b** — **the same cards, in the same numbers, as `status`'s quota
      line.** Two commands in one tool must not disagree about what you can run.
      A committed-use, preemptible or VWS grant is not a usable card: it must
      never make either command report a card as ready, and `COMMITTED-` must not
      appear as though it were one. This is how a real defect reached the release
      — `status` counted 25 grants of which 18 could not start anything.
- [ ] **C4c** — if the line is cut short it says so (`+2 more — 6 card(s) ready`).
      A silent truncation reads as the whole answer.
- [ ] **C5/C6** — narrowing works and the numbers agree with C4.
- [ ] **C7** — valid JSON with `project`, `gpus`, `by_region`.
- [ ] **C8** — if nothing is usable, it prints the exact `quota request` command
      to fix that. *(Only reachable on a project with no approved card. If C4 found
      one, record C8 as not run — it cannot be forced from here.)*
- [ ] **C9** — `quota request --dry-run` prints the `gcloud` call it would make —
      the card, the region, the value and the justification — and **submits
      nothing**: it returns before `gc.run` and before the wait, so it is the one
      path here that reaches no branch of the default. Confirm it submitted
      nothing rather than reading it off the word "dry": `qat quota list --region
      us-central1` shows no new pending request. Exit 0.
- [ ] **C9b** — **a real submission, with the waiting turned off.** It prints
      `requested l4 = 1 in us-central1` and then `track them: <console URL>`, and
      **returns immediately** — seconds, not minutes. This is the first half of
      the default path and it is where the money question is settled: a quota
      request creates no instance and no disk and **costs nothing**. What it does
      cost is a request to Google that a human may read and that **cannot be
      withdrawn**, and re-running it files a second one. Ask for a card you
      actually want. *(Skip it, and say so, if you would rather not file a
      request on this project — but then C9c is unreachable too.)*
- [ ] **C9c** — **the default path, which no criterion has ever covered.**
      `--wait` is on unless you turn it off, so this — not C9 — is what a tester
      following the README gets. It submits, prints the tracking URL, and then
      **polls every 30 seconds for up to 30 minutes**, saying so rather than
      going silent. Two endings, and they are different claims:
      - granted inside the window: `granted: l4`, exit 0.
      - still pending at 30 minutes: a `still pending: l4` line saying approval
        can take days and naming `comfy-qat quota` to check, and **exit 75**.
      **75 is the criterion.** Not 0 and not 1: 0 tells a script the quota was
      granted when it was not, and 1 says something broke when nothing did. On a
      project with an approved L4 the request is usually granted at once, so the
      pending branch may not be reachable — record which ending you got, and
      record the other as not run. *(Interactive and slow: run it on its own, and
      `time` it so the 30-minute bound is a measurement and not a belief.)*
- [ ] **C9d** — **and nothing else changed.** `quota list` for that region names
      the card and its status, and agrees with the console URL C9b printed. A
      quota request must not appear anywhere as an instance or a disk; if you
      have run `Q0` already, `diff` its `instances.txt` and expect no change.

*Ran 2026-08-27 — **all of phase C passed**, C4b included. That check exists
because it caught a real defect: `status` counted 25 grants of which 18 could not
start anything.*

## Phase D — the local machine *(no cost)*

If no local ComfyUI is running, start one in **another** terminal and leave it
there — this tool never starts or stops anything on your own machine:

```sh
~/ComfyUI/venv/bin/python ~/ComfyUI/main.py --port 8188 --listen 127.0.0.1
```

```sh
echo "=== D1 declared machines"; qat list
echo "=== D2 stamp local"; qat stamp local; echo "exit $?"
echo "=== D3 stamp json"; qat stamp local --json
echo "=== D3b does the stamp match the machine"; curl -s http://127.0.0.1:8188/system_stats | head -c 600; echo
echo "=== D4 open on a local host"; qat open local; echo "exit $?"
```

Then stop that ComfyUI (Ctrl-C in the other terminal) and run this on its own:

```sh
echo "=== D5 stamp with nothing serving"; qat stamp local; echo "exit $?"
```

- [ ] **D1** — every machine you have **declared**. `list` reads your host
      list and never calls Google, so a box you created and have not discovered
      yet is correctly absent; `discover --dry-run` is what compares the two.
- [ ] **D2** — one line: host, ComfyUI version, OS, device, torch, python. Correct against what ComfyUI's own `/system_stats` says.
- [ ] **D3** — the same values under ComfyUI's field names.
- [ ] **D3b** — every value in the line traces to that raw payload, and anything
      the payload does not contain is **absent** from the line rather than
      guessed or shown as a placeholder.
- [ ] **D4** — says `local — there is nothing to tunnel` and prints the URL, and
      starts nothing. **This is not a run of `open`, and it must not be recorded
      as one.** `open` returns on that sentence before `open_tunnel` is called,
      so nothing this command exists for — the port check, the spawned
      `gcloud compute ssh -L`, the identity record that stops a tunnel opened
      under the right NAME pointing at the wrong MACHINE — is reached. D4 grades
      one early return. The command itself is graded at **E6d**, and until
      2026-09-08 D4 was the only `open` this pack had ever run.
- [ ] **D5** — says nothing answered, names the URL, exit 1. Not a traceback.

*Ran 2026-08-27 — **all of phase D passed**.*

## The baseline for phase I — run this now, before anything exists *(free)*

Phase I asks what the run left behind, and **that is a comparison.** Until now the
page said "run this before the run and after it, and compare" in prose and then
gave one block, run once, at the end — so four criteria that read as comparisons
were ticked against nothing, in the phase whose motivating failure cost real
money. This is that before. It takes ten seconds and it is the difference between
phase I meaning something and phase I being a formality.

Run it here, after phase C proves gcloud works and **before phase K creates the
first box.**

```sh
P=$(gcloud config get-value project 2>/dev/null); echo "=== Q0 project $P"
mkdir -p ~/qa-before
gcloud compute instances list --project $P > ~/qa-before/instances.txt
gcloud compute disks     list --project $P --format="table(name,zone.basename(),sizeGb,type.basename(),users.basename())" > ~/qa-before/disks.txt
gcloud compute snapshots list --project $P --format="table(name,diskSizeGb,storageBytes,creationTimestamp,sourceDisk.basename())" > ~/qa-before/snapshots.txt
qat list > ~/qa-before/list.txt
wc -l ~/qa-before/*.txt; cat ~/qa-before/instances.txt
```

- [ ] **Q0** — four files in `~/qa-before/`, and **you have read
      `instances.txt`.** Whatever is in it now is not this run's doing, however it
      looks. A project that already carries three stopped boxes and a detached
      disk is the normal case here, and without this file you cannot tell one of
      those from a leak you are about to create.

---

## Phase K — making the box *(K1–K4 free; K5 creates one and bills)*

**Nothing in this phase has ever been run.** `create` landed after the 2026-08-27
pass, so every check below is new. It comes before E because E needs a box; if you
already have one, K1–K4 are still worth running, since they cost nothing and
create nothing.

The claim under test: **a tester never opens the Google Cloud console.** The card
is the only real decision, and the machine type, the zone and the driver are the
tool's problem.

```sh
echo "=== K1 the plan, and nothing else"; qat create --os linux --gpu t4 --dry-run; echo "exit $?"
echo "=== K2 a card you have no quota for"; qat create --os linux --gpu a100 --dry-run; echo "exit $?"
echo "=== K3 a card that does not exist"; qat create --os linux --gpu rtx4090 --dry-run; echo "exit $?"
echo "=== K4 an OS that does not exist"; qat create --os plan9 --gpu t4 --dry-run; echo "exit $?"
echo "=== K4d two flags that cannot both be right"; qat create --os linux --gpu t4 --zone us-central1-a --region europe-west2 --dry-run; echo "exit $?"
echo "=== K4b nothing was written"; ls -l ~/.config/comfy-qa-tools/
echo "=== K4c and nothing was CREATED"; diff ~/qa-before/instances.txt <(gcloud compute instances list --project $P) && echo "IDENTICAL"
```

- [ ] **K1** — prints three things and creates nothing: the **quota it read**
      (the card's own grant *and* `GPUS_ALL_REGIONS`), the **plan** as numbered
      steps naming the machine type, the image, the disk and the startup script,
      and the **zone order** with the measured latency that put each zone where it
      is. Ends by saying nothing was created.
- [ ] **K2** — refused **before anything exists**, naming the card and what the
      allowance actually is. Exit non-zero. This is the check that matters most in
      the phase: a quota refusal after the instance exists costs money and a
      cleanup, and refusing costs nothing.
- [ ] **K3/K4** — names what there is rather than failing obscurely. Exit 2, no
      traceback. **These two are offline** — `plan()` runs before the project
      read, the instance list and the minute of quota, so they need no cloud
      credentials at all. The check that they are still offline is the **time
      against K1**: K1 reads quota and takes about a minute, and K3/K4 must come
      back in roughly the time `qat --version` takes. `--os freebsd` once cost 64
      seconds to be told freebsd is not an operating system, and a build that
      regressed to that prints exactly this refusal, just a minute later — the
      wording cannot tell you, only the clock can.
- [ ] **K4d** — **`--zone` and `--region` together are refused, not resolved.**
      They are not an expressible intention: `--zone` pins one zone, `--region`
      asks for a choice inside one. `--zone` used to win silently, so
      `--region europe-west2 --zone us-central1-a` created a box in Iowa without
      mentioning the region it discarded. The refusal names **both** flags and
      prints the two commands that are each one intention. Exit 2, and offline
      like K3/K4 — a refusal that takes as long as K1 was made after the quota
      read, and a flag conflict decided after a minute of reading Google is the
      wrong refusal wearing the right words.
- [ ] **K4b** — `--dry-run` wrote nothing **to your host list**. No new entry.
      `zone-latency.json` may appear or change — a fresh latency measurement is
      expected on the first run and is cached for a week — so that file is not
      part of this check and never was.
- [ ] **K4c** — **and it created nothing on the project.** `IDENTICAL` against the
      `Q0` capture. Four `--dry-run` criteria in this pack asserted that nothing
      was created while running no step that could have noticed; the plan prints
      before the thing it promises not to do, so the output is the same in both
      worlds. This is the step that tells them apart.
- [ ] **K1b** — **the machine type follows from the card, and is right.** `t4` →
      an `n1-` type with `--accelerator`; `l4` → a `g2-` type with the GPU built
      into it and no `--accelerator` at all. Getting this the wrong way round is
      the commonest way a create by hand fails, so it is the thing to read closely.
- [ ] **K1c** — the zone order is **quota first**, then what is offered, then
      latency. A zone in a region the project holds no quota in must not appear at
      all, however near it is.

Now the one that bills:

```sh
echo "=== K5 make it"; qat create --os linux --gpu t4 --name qa-linux --yes; echo "exit $?"
echo "=== K6 it is in the host list"; qat list
echo "=== K7 and Google agrees"; gcloud compute instances list
```

- [ ] **K5** — creates the box, says which zone it landed in, and **adds it to the
      host list on a free port** — no `discover` needed in between. It finishes by
      printing both the `go` command and the `down` command: a message that says
      how to use a GPU box without saying how to stop it is how one bills all
      night.
- [ ] **K5b** — **the driver.** On Linux it says the startup script is installing
      it and that this reboots the box once or twice. On **Windows** it says
      plainly that there is no driver yet, that ComfyUI will run on the CPU until
      there is, and prints the PowerShell commands Google documents. **A guessed
      Windows recipe is a fail** — Google publishes only a manual method, and
      inventing one is worse than the seam.
- [ ] **K5c** — if a zone is out of capacity it **moves to the next one** rather
      than stopping, and says it did. Only when every zone is exhausted is it a
      refusal, and that refusal names the zones tried.
- [ ] **K6/K7** — the host list and the project agree: one new box, the name you
      gave it, the port assigned automatically, no duplicate entry.
- [ ] **K8** — **nothing is orphaned by a failure.** If K5 fails part way, whatever
      it created is named in its own output, with the command that removes it.
      Finding a leak with `gcloud` that the tool never mentioned is a defect in the
      tool. Phase I is where you confirm this. *(Only reachable if K5 actually
      fails part way. Not something to arrange — record it as not run.)*

## Phase E — a cloud box, start to serving *(this bills)*

**`BOX` must be the box phase K just made**, and this is not a convenience. E3 is
the only place in this pack where `go` **installs** ComfyUI, and it can only do
that on a box that has never had it. On a box that already serves, `go` takes an
early return in `_serve` — URL, stamp, stop line — and installs nothing; that path
is E7, and it is what the 2026-08-27 pass ticked as `go`. If you are reusing an
existing box, say so and record **E3c as not run**: you are running E7 twice.

**`go` is detached now**, so this phase no longer needs two terminals: it returns
to your prompt with ComfyUI still running on the box. The second-terminal
instruction that used to be here is gone, and E3 and E7 are rewritten.

**Before you start `go`, capture the state it is about to change.** E0 is that
capture and it takes ten seconds; without it E3's install cannot be told from
E7's early return, because both end with the same URL and the same stop line.

```sh
BOX=qa-linux                 # the box K5 made. Not one that has already served.
echo "=== E0 BEFORE: this box has never had ComfyUI launched on it"
qat logs $BOX --tail 20; echo "exit $?"
echo "=== E0b and --no-install says so without installing anything"
qat go $BOX --no-browser --no-install; echo "exit $?"
echo "=== E1 discover is idempotent"; qat discover --dry-run
echo "=== E2 tunnel command"; qat open $BOX --dry-run
echo "=== E3 go — and on this box it is the one that installs"; time qat go $BOX; echo "exit $?"
echo "=== E4 stamp the cloud box"; qat stamp $BOX
echo "=== E5 tunnel is recorded"; ls -l ~/.config/comfy-qa-tools/tunnels/
echo "=== E7 go again, on a box already serving"; qat go $BOX; echo "exit $?"
```

Then the tunnel, on its own, because it needs the box taken apart and put back:

```sh
echo "=== E6 BEFORE: the tunnel E3 opened, and the process behind it"
cat ~/.config/comfy-qa-tools/tunnels/$BOX.json; echo
PORT=$(sed -n 's/.*"port"[^0-9]*\([0-9][0-9]*\).*/\1/p' ~/.config/comfy-qa-tools/tunnels/$BOX.json)
PID1=$(cat ~/.config/comfy-qa-tools/tunnels/$BOX.pid); echo "port $PORT pid $PID1"
echo "=== E6a open twice does not stack"; qat open $BOX; echo "exit $?"
lsof -nP -iTCP:$PORT -sTCP:LISTEN
echo "=== E6b let the tunnel go, keep the machine"; qat disconnect $BOX; echo "exit $?"
echo "=== E6c BASELINE: no records, and nothing on the port"
ls -l ~/.config/comfy-qa-tools/tunnels/; lsof -nP -iTCP:$PORT -sTCP:LISTEN || echo "port free"
echo "=== E6d open it for real, from nothing"; qat open $BOX; echo "exit $?"
echo "=== E6e AFTER: three files, a new pid, the same machine"
ls -l ~/.config/comfy-qa-tools/tunnels/; cat ~/.config/comfy-qa-tools/tunnels/$BOX.json; echo
echo "pid was $PID1, is now $(cat ~/.config/comfy-qa-tools/tunnels/$BOX.pid)"
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:$PORT/system_stats
```

- [ ] **E0** — **the baseline for E3, and E3 means nothing without it.** `logs`
      on a box that is running with nothing ever launched says there is no log
      file at that path *and that the machine is running and billing*. That
      absent log is the evidence that ComfyUI has never started here, and it is
      the only cheap evidence there is. Record the sentence. If instead you get
      a log, this box has served before: E3 will take the reuse path and you
      record E3c as not run. *(This is the state L5c grades; it is reachable
      exactly here, between `create` and the first `go`.)*
- [ ] **E0b** — **`--no-install` refuses, and that is the run of the flag.**
      `ComfyUI is not answering and --no-install was given`, **exit 1**, and
      nothing installed. F2 is the same flag on a box that already serves, where
      `_serve` returns on the stamp before `no_install` is read at all — so F2
      cannot fail whatever the flag does, and the 2026-08-27 tick against it was
      a tick on a branch that never ran. This is the branch. Costs nothing: the
      box is already up, and this touches it for seconds.
- [ ] **E1** — reports every box already present; adds nothing; writes nothing.
      It ends `--dry-run: nothing written` — and that sentence prints after the
      decision either way, so the check is that `qat list` still matches
      `~/qa-before/list.txt` from `Q0`, plus whatever `create` added and nothing
      else. **This needs no box.** `discover` has never been run, and it is one
      `instances list` on the project, so it can also be run straight after phase
      C, before anything exists — where "no cloud boxes on `<project>`", or a list
      of boxes you did not declare, is itself the answer. It is the only
      `discover` check in the pack and it costs nothing.
- [ ] **E2** — prints a `gcloud compute ssh ... -N -L 127.0.0.1:<your port>:127.0.0.1:8188`
      line and does not run it. Both ends say `127.0.0.1` and not `localhost`:
      macOS resolves that name to `::1` first, and ssh then binds IPv6 only while
      every attempt on `127.0.0.1` is refused.
- [ ] **E3** — says what it is doing at each step: starting, tunnelling, checking
      for ComfyUI, installing if absent, launching. **It then returns you to your
      prompt**, saying ComfyUI is running on the box and this terminal is free,
      and offering `logs` and `down`. A browser opens on the local URL and ComfyUI
      loads. **The startup log must NOT stream onto your terminal** — that is
      `--follow` now. *(Rewritten: the old E3 asked for the opposite.)*
- [ ] **E3b** — **it still does not return until ComfyUI has really answered.**
      Detaching must not soften what "up" means: a box that booted and serves
      nothing looks like success and bills like success. If the prompt comes back
      before the URL works, that is a fail, and a bad one.
- [ ] **E3c** — **this `go` installed ComfyUI, and you can say how you know.**
      Three things distinguish an install from the reuse path, and the closing
      lines distinguish neither — they are identical in both worlds, which is
      how `go` came to be recorded as passed with the install never having run:
      1. E0 found **no log**, and after E3 `qat logs $BOX --tail 200 | head`
         shows this ComfyUI starting from its first line;
      2. the step lines name **installing** — E7's do not, and neither does
         anything the reuse path prints;
      3. `time` reads in **minutes**, not the seconds E7 takes.
      All three, or it is not this criterion. **Cost:** the box runs from K5
      until phase G stops it; the install itself adds no GPU-hours beyond the
      time it takes. *(Needs a box that has never served — the one K5 made. On a
      reused box, record E3c as not run and say which box you used.)*
- [ ] **E4** — the stamp names the **cloud** box's GPU and OS, not your laptop's.
      This is the single most important check in the run. *(Passed 2026-08-27.)*
- [ ] **E5** — **three** files for that host: `.pid`, `.log` and `.json`. The
      `.json` is the identity record — instance, zone, project, port — and it is
      the one that matters: it is what stops a tunnel opened under the right NAME
      from pointing at the wrong MACHINE, which is the failure this whole tool
      exists to prevent. A run that checks only the `.pid` never checks it.
- [ ] **E6** — **the before-capture, and the reason this block exists.** You have
      the `.json` on screen, `PORT` and `PID1` set from the files, and you have
      read them. Every check below is a comparison against those two values; a
      comparison with nothing recorded is what D4 has been standing in for.
- [ ] **E6a** — a second `open` prints **`tunnel already open (pid N)`** and `N`
      **equals `PID1`**, and `lsof` shows **one** listener on `$PORT`. The tool
      prints `tunnel open` for a fresh one and `tunnel already open` for a reuse,
      so the words are the check and not the URL: a build that quietly started a
      second `ssh -L` prints a URL just as happily, and the second forward cannot
      bind the port, so the URL still answers — from the first tunnel.
- [ ] **E6b/E6c** — `disconnect` leaves the machine running, and then **all three
      files for that host are gone and `lsof` says the port is free**. This is
      the baseline for E6d and it is the whole point of taking the tunnel down:
      without it, E6d cannot tell a tunnel it opened from the one that was
      already there. If `lsof` still shows a listener, stop — something else
      holds that port and E6d will be refused for a reason that has nothing to
      do with `open`.
- [ ] **E6d** — **this is the run of `open` this pack has never had.** From the
      E6c baseline it goes through `open_tunnel` for real: the port check, the
      spawned `gcloud compute ssh -L`, and the three records written. It prints
      **`tunnel open (pid N)`** — the fresh-open wording, not `tunnel already
      open` — with **`N` different from `PID1`**, and exits 0. `qat open local`
      reaches none of this: it returns at `local — there is nothing to tunnel`
      before `open_tunnel` is called, which is why D4 is not a substitute and
      why `open` was recorded as passed while the code under test never ran.
- [ ] **E6e** — **three files again, and the `.json` names the same machine.**
      Instance, zone, project and port identical to the E6 capture, and `curl`
      returns **200** on `$PORT`. A tunnel opened onto the wrong machine and one
      opened onto the right one both print `tunnel open` and both answer on the
      port — the identity record is the only thing that tells them apart, and it
      is the thing this command exists for.
- [ ] **E7** — a second `go` on a box that is already serving gives you the URL and
      the stamp straight away and **installs and restarts nothing**. *(Rewritten:
      the old E7 tested a Ctrl-C that stopped ComfyUI. That behaviour now lives
      behind `--follow` and is checked in L4.)*

*Not run: E0, E0b, E1, E2, E3, E3b, E3c, E5, E6–E6e, E7. Only **E4 passed** on
2026-08-27, and E3/E7 did not exist in this form. **`open` has never been run
against a real tunnel by anyone** — the one tick it carries is D4, the local
early return, which does not call `open_tunnel`. E6d is that run.*

## Phase L — the log, and the ways of watching it *(this bills)*

**Nothing in this phase has ever been run.** `logs` exists because `go` stopped
streaming: the question "what is it doing right now" needed somewhere to go.

```sh
echo "=== L1 follow it"; qat logs $BOX     # Ctrl-C after a few lines
echo "=== L2 the last 50 lines, then stop"; qat logs $BOX --tail 50; echo "exit $?"
echo "=== L3 it is still serving"; qat stamp $BOX
echo "=== L5a your own machine"; qat logs local; echo "exit $?"
echo "=== L5b a stopped box"; qat down $BOX; qat logs $BOX; echo "exit $?"
echo "=== L7 launch again, and read from the top"; qat go $BOX --no-browser; qat logs $BOX --tail 200 | head -20
```

L7's `go` also puts the box back up, which phases S, M, F and G all need.

L5c is the third answer and it is only reachable in one place: a box that is
**running with nothing ever launched on it**, which is true exactly once — between
`create` in phase K and the first `go` in phase E. That is the `=== E0` step,
which runs `qat logs $BOX --tail 20` for exactly this reason and doubles as the
baseline for E3. If you skipped it, record L5c as not run: it is not reachable
from here, because L7's truncation means a box that has ever served has a log.

- [ ] **L1** — follows by default, because "what is it doing now" is the question
      people have. It reads a file on the box and touches nothing else.
- [ ] **L1b** — **Ctrl-C ends the reading and nothing else.** It says so, and says
      the machine is still running with the `down` command. If Ctrl-C here stops
      ComfyUI, the whole point of detaching is gone.
- [ ] **L2** — prints that many lines and **exits on its own**, no `--follow`.
- [ ] **L3** — after L1 and L2, ComfyUI is still answering. Proof that reading a
      log is a read.
- [ ] **L4** — `qat go $BOX --follow` streams the log here as a local `main.py`
      would, and **Ctrl-C then stops ComfyUI** — the old behaviour, on purpose,
      for debugging a launch. It stops ComfyUI and not the box, and a stopped
      ComfyUI on a running box still bills; the message must say so. *(Interactive
      — a Ctrl-C into a live stream. Run it by hand, not from the block.)*
- [ ] **L5a** — `local` says your own ComfyUI's log is in the terminal you started
      it in, and offers the `main.py` line to start it there. Exit non-zero, and
      no wait. **Offline**: the `local` branch of `read_logs` raises before any
      gcloud call, so this needs no project and no box and can be run the moment
      you have a host list — it is the one `logs` check that does not need this
      phase's box.
- [ ] **L5b** — a **stopped** box says it is not running, so it has no ComfyUI and
      no log, and points at `go`. It must **answer**, not hang: a command that
      waits silently on a stopped box is the worst of the three, because the box
      it is waiting for may be one you are still paying for.
- [ ] **L5c** — a **running** box with nothing launched says there is no log file
      at that path *and that the machine is running and billing*. **The billing
      sentence is the criterion**, not a nicety — this is the state where you have
      a GPU box costing money and nothing to show for it, and the message is the
      only thing that tells you. *(Run between phase K and phase E — that is the
      `=== E0` step, which grades the same output as the baseline for E3.)*
- [ ] **L6** — `qat go $BOX --new-window` opens a macOS Terminal window running
      the same command and leaves this terminal free. It must **not** have added
      `--follow` on your behalf: under `--follow`, Ctrl-C in that window stops
      ComfyUI on the box, so a flag nobody typed would make closing the window
      kill what it just started. `qat go $BOX --new-window --follow` does stream,
      and there the interrupt stopping ComfyUI is the asked-for behaviour.
      Anywhere that is not a Mac with
      `osascript`, it **says so and starts nothing**, printing the exact command
      to paste. A window that silently does not appear, on a command that starts a
      GPU box, is a machine you are paying for and cannot see — so a half-success
      here is a fail. *(Interactive, and macOS-only on the success path. Run it by
      hand; on any other platform the check is that it starts nothing.)*
- [ ] **L7** — each launch **truncates** the log, so the first lines you read are
      this ComfyUI starting, not the previous run's traceback above it. Compare
      against what L2 printed before the relaunch: if the old lines are still
      there, a tester debugging a failed start reads yesterday's failure and
      diagnoses the wrong thing.

## Phase M — two machines at once *(this bills twice)*

**Not run, and it needs quota for two GPU boxes at once** — the same thing that
blocks J10/J11. On a project whose `GPUS_ALL_REGIONS` ceiling is 1 this cannot be
attempted at all; record it as not run and say why.

This is what detaching bought, so it is worth its own phase.

```sh
echo "=== M1 one"; qat go linux
echo "=== M2 two, from the same prompt"; qat go windows
echo "=== M3 both"; qat list --live
echo "=== M4 stop everything"; qat down --all; echo "exit $?"
echo "=== M5 and Google agrees"; gcloud compute instances list
```

- [ ] **M1/M2** — two cloud boxes brought up **from one terminal**, neither
      command holding it. This was impossible before.
- [ ] **M3** — both show running and tunnelled, on their own ports, and the two
      URLs are different. Two tunnels on one port is the failure you cannot
      diagnose from the outside.
- [ ] **M4** — `down --all` takes **no name**, stops every declared cloud box, and
      says how many. Given a name as well it refuses rather than guessing.
- [ ] **M4b** — **one box refusing to stop does not leave the rest running.** It
      stops the others, then names what did not stop, what it may still be
      costing, and what to do — and exits non-zero. *(Needs a box that will not
      stop, which cannot be arranged to order. **Record it as not run** rather
      than ticking it from a clean run — a run where everything stopped tells you
      nothing about this, and it is the same check as G6d.)*
- [ ] **M5** — every instance reads TERMINATED. **If one says RUNNING you are
      still being billed, and that is a blocker.**

## Phase F — the failure paths *(this bills)*

Only F0–F2 are always runnable; F3 depends on Google being out of capacity.

**F0–F1 stop the box and start it again**, on purpose. `up` has only ever been
run against a box that was already up, where `bring_up` reads RUNNING, skips
`start_instance`, the boot wait, the stockout branch and the interrupt guard
entirely, and prints the same closing lines a real start does. There is no way to
exercise a start except to have something to start, so this costs one stop, one
start, and the GPU-hours from here to phase G. Run it while you are at the
terminal, not on the way to bed.

```sh
echo "=== F0 BEFORE: what Google says, and what tunnel files exist"
gcloud compute instances list --project $P
ls -l ~/.config/comfy-qa-tools/tunnels/
echo "=== F0b stop it, so up has something to start"; qat down $BOX; echo "exit $?"
gcloud compute instances list --project $P     # $BOX must read TERMINATED here
ls -l ~/.config/comfy-qa-tools/tunnels/
echo "=== F1 up a box that is NOT already up"; time qat up $BOX; echo "exit $?"
echo "=== F1a AFTER"; gcloud compute instances list --project $P; ls -l ~/.config/comfy-qa-tools/tunnels/
echo "=== F1b up again, now that it is up"; time qat up $BOX; echo "exit $?"
echo "=== F2 go with --no-install on a box that has ComfyUI"; qat go $BOX --no-browser --no-install
echo "=== F3 move, plan only"; qat move $BOX --to us-central1-b --dry-run   # a zone the box is NOT in
echo "=== F3b and without --to"; qat move $BOX --dry-run; echo "exit $?"
echo "=== F3c neither of those created anything"; diff ~/qa-before/snapshots.txt <(gcloud compute snapshots list --project $P --format="table(name,diskSizeGb,storageBytes,creationTimestamp,sourceDisk.basename())") && echo "IDENTICAL"
echo "=== F4 unknown host"; qat stamp not-a-machine; echo "exit $?"
echo "=== F5 unknown host, lifecycle"; qat down not-a-machine; echo "exit $?"
```

- [ ] **F0** — **the baseline, and F1 is unfalsifiable without it.** You have
      Google's own line for `$BOX` reading **TERMINATED** after F0b, and the
      tunnels directory holding **no files for that host**. Both, and you looked.
      `up` ends with `open <url>` and `comfy-qat down <name>` whether it started
      a machine or found one already running, so the closing lines cannot tell
      you which happened and neither can exit 0.
- [ ] **F1** — **it starts a box that was not running, and says so as it goes.**
      The step lines name the state it found and the wake: `<name> is stopped —
      starting it`, with the timer ticking while it waits, then `running`, then
      the ssh wait, then `tunnel open`, and only then the ComfyUI wait. **`time`
      reads in minutes.** A box that was already running prints `<name> is
      running` and `tunnel already open` and returns in seconds — that is F1b,
      and it is a different sentence, not a faster version of this one.
- [ ] **F1a** — **and the world changed.** `gcloud` reads **RUNNING** where F0
      read TERMINATED, and the three tunnel files exist where F0b showed none.
      That pair is the observation; the URL and the stop line are printed on both
      paths and are evidence of nothing. **The box is billing again from here
      until phase G stops it** — if you stop the run at F1, run `qat down $BOX`
      and check `gcloud compute instances list` before you leave.
- [ ] **F1b** — the already-up path: `<name> is running`, `tunnel already open`,
      nothing restarted, back in seconds. **This is the only `up` the 2026-08-27
      pass ran**, and on its own it exercises no start at all — it is here as the
      contrast that makes F1 legible, not as a run of the command.
- [ ] **F2** — serves without reinstalling. **On a box that is already serving
      this does not exercise `--no-install`**: `go` returns on the stamp before
      the flag is read, so the same output appears whatever the flag does. The
      run of the flag is **E0b**, on a box that has no ComfyUI, where it must
      refuse with exit 1. Tick F2 for what it is — the reuse path — and record
      E0b separately.
- [ ] **F3** — with `--to`, a numbered plan naming the snapshot, the new disk, the
      new instance and the target zone, then it stops.
- [ ] **F3c** — **and the snapshot list is unchanged.** A `move --dry-run` prints
      its plan before it would take the snapshot, so "it printed a plan and said
      nothing was created" is satisfied by a build that took one. The snapshot is
      the first thing a move creates and the cheapest to check for.
- [ ] **F3b** — **without `--to`, `--dry-run` refuses, and that is correct.** Finding
      a zone with capacity means starting the box and reading the zones out of the
      refusal, which is exactly what a dry run must not do. It says so and exits 2.
      *(This criterion used to have no `--to` in its block and asked for the plan
      anyway — a correct build was marked FAILED on it.)*
- [ ] **F4/F5** — names the host as unknown and lists what is declared. Exit 2, no traceback.
- [ ] **F6** — a start that fails on capacity says it is a stockout, names a zone
      that does have capacity, and does not blame quota or billing. *(Needs a real
      stockout, so it cannot be scheduled. Observed live on 2026-08-26: `up
      comfy-win` hit a real L4 stockout in us-central1-a and reported it
      correctly, including the `move` command — a sighting, not a run of this
      block.)*
- [ ] **F7** — **the zone Google suggests can be stale by the time you use it.**
      If the move then fails in the suggested zone, the tool must say what it
      already created, what that costs, and what to do next — not leave you to
      find a 300 GB disk with `gcloud` a week later. *(Needs the suggested zone to
      go stale between the suggestion and the move — not arrangeable.)*
- [ ] **F8** — when the box you asked for cannot start, the tool offers the boxes
      that can. Being told "no capacity" and nothing else is the moment a tester
      gives up and goes back to the console. *(Needs a real stockout, like F6 and
      F7. Record as not run rather than assumed.)*

*Ran 2026-08-27: what passed was **F1b and the reuse half of F2** under the ids
`F1` and `F2`. Both ticks were honest and neither reached the code the criterion
named: `up` found the box already RUNNING and never started anything, and `go`
returned on the stamp without reading `--no-install`. **F0, F1, F1a and E0b have
never been run.** F3–F5 not run; F6–F8 depend on Google actually being out of
capacity at that moment and cannot be scheduled — the 2026-08-26 observation in
F6 is a real sighting, not a run of this block.*

Old spellings in the output of F6–F8 are covered by **A8**, which passes as of
`442f867`. Anything you see there is therefore a regression A8's grep missed —
write it up with the sentence you saw and the command that printed it.

## Phase S — onto the box, and off it again *(needs the box from E; S4 leaves it billing)*

**Nothing in this phase has ever been run, and none of it has ever had a
criterion.** `ssh`, `rdp` and `disconnect` are three of the four commands the pack
could not see; `delete` is the fourth and is phase N.

`ssh` and `rdp` **replace this terminal** — they `execvp` gcloud, so nothing after
them in a pasted block runs. Run S1 as a block; run S2 and S3 one at a time.

**S0 and S1a–S1d are offline** and belong to no phase in particular: all five are
refused from the host list before a `Gcloud` object is built, so they need no
credentials, no project and no box. Run them the moment phase B has given you a
host list with a `local` entry and one of each OS — they are among the cheapest
ticks in the pack and nobody has ever taken them.

```sh
WINBOX=comfy-win; LINUXBOX=comfy-linux   # whatever `qat list` calls yours
echo "=== S0 disconnect on your own machine"; qat disconnect local; echo "exit $?"
SC=$(mktemp -d); printf '[hosts.local]\nkind = "local"\nport = 8188\ngce_instance = "somebox"\n' > $SC/local.toml
echo "=== S0b a local entry that names a cloud instance"; qat disconnect local --config $SC/local.toml; echo "exit $?"
echo "=== S1a your own machine has no box to ssh to"; qat ssh local; echo "exit $?"
echo "=== S1b nor to rdp to"; qat rdp local; echo "exit $?"
echo "=== S1c ssh at a Windows box"; qat ssh $WINBOX; echo "exit $?"
echo "=== S1d rdp at a Linux box"; qat rdp $LINUXBOX; echo "exit $?"
```

```sh
qat ssh $BOX      # === S2 — run on its own. `exit` brings you back.
qat rdp $WINBOX   # === S3 — run on its own. Ctrl-C closes the forward.
```

```sh
echo "=== S4 let go of the tunnel, keep the machine"; qat disconnect $BOX; echo "exit $?"
echo "=== S5 the records are gone"; ls -l ~/.config/comfy-qa-tools/tunnels/
echo "=== S6 the box is still up"; qat list; gcloud compute instances list
echo "=== S7 the flag that went"; qat down $BOX --keep-running; echo "exit $?"
echo "=== S7b and the other two"; qat down --os windows; echo "exit $?"
```

- [ ] **S0** — **`disconnect local` says the local ComfyUI is left running and
      that this tool did not start it**, and exits 0. It is not an error and it
      is not a refusal: there is no tunnel to close and nothing that bills, so a
      statement is the honest answer. It also prints `comfy-qat down local` —
      read that line and say whether it reads right on a machine that costs
      nothing to leave on; every other host `disconnect` is given is one you are
      paying for, and this is the one where the standing rule and the situation
      disagree. Offline; nothing is contacted, and exit 0 here is the answer and
      not an absence of one.
- [ ] **S0b** — **a `local` entry that also names a cloud instance is refused, and
      it is refused at load.** Put `kind = "local"` and a `gce_instance` in a
      scratch host list and point `disconnect --config` at it: the host list
      loader rejects the file with `kind 'local' cannot carry gce_instance`, and
      it gives the reason: stopping is decided from `kind`, so a cloud box
      declared local is never stopped and keeps billing. Exit 2, and `disconnect`
      itself never runs. `put_away` carries a second refusal for the same case;
      **that one is not reachable from a host list** and this criterion does not
      claim it. The pass is the config refusal and the exit 2 — anything that
      reports that host as idle is a blocker. *(The in-code branch needs a `Host`
      built in Python rather than a file; record it as not run.)*
- [ ] **S1a/S1b** — `local` is refused by name: `ssh` says to open a terminal,
      `rdp` says it is not a Windows cloud box. Exit 2 for both, and **no gcloud
      call is made** — these refuse from the host list alone, so they cost nothing
      and are safe to run before you have any box at all.
- [ ] **S1c** — `ssh` at a Windows box refuses and points at `comfy-qat rdp <box>`.
      Exit 2. Being told "no ssh here" without being told what to use instead is
      the failure; the fix line is the criterion.
- [ ] **S1d** — `rdp` at a Linux box refuses and points at `comfy-qat ssh <box>`.
      Exit 2. *(Skip S1c/S1d if you have only one OS declared, and say so.)*
- [ ] **S2** — you land in a shell on the box having typed no zone, no project and
      no `--tunnel-through-iap`. That is the whole command: the long form is four
      arguments the tool already knows, and every fix line in the tool that used
      to hand you that line now says `comfy-qat ssh <box>` instead.
- [ ] **S2b** — **it works with no `comfy-qat open` ever run.** `ssh` goes through
      Google's IAP, not through this tool's forwarded port, so it does not need a
      tunnel and must not behave as though it does. Run it on a box you have only
      `up`'d, or straight after `disconnect`. Anything that reads as "open a tunnel
      first" is a defect in the wording, not in the command.
- [ ] **S3** — `rdp` prints `user`, `password` and `address localhost:33389`
      **before** it takes the terminal, in that order, and only then forwards.
      Order matters at 2am: once the forward starts, this process is gone.
- [ ] **S3b** — **a blank user or a blank password is a fail, not a warning.**
      gcloud exiting 0 with nothing on stdout produces exactly the shape of a real
      credential pair with both halves empty, under a line saying the forward is
      starting — a failure that looks like success, whose only symptom is a login
      prompt that never opens. The tool must refuse and hand you the raw
      `gcloud compute reset-windows-password` line instead.
- [ ] **S3c** — it says the password was **reset**. It is not reading a password
      you already had; Google documents no way around that, and a tester who
      thinks otherwise will not understand why a colleague's saved credential
      stopped working.
- [ ] **S4** — `disconnect` closes the tunnel, leaves the machine running, and
      **says the machine keeps billing**. That is the point of the command, so it
      has to be in the output.
- [ ] **S4b** — it ends with `comfy-qat down <name>`. **The one command whose
      purpose is to leave a GPU box running is the one that most needs to print
      how to stop it**; it once did not.
- [ ] **S4c** — it says the machine is running because it **asked**, not because
      it assumed. If Google cannot be reached, it says it could not tell and
      points at `list --live` — it must not claim a bill it did not check, in
      either direction. Both errors have shipped, a day apart, in opposite
      directions.
- [ ] **S5** — all three tunnel files for that host are gone: `.pid`, `.log` and
      `.json`. Killing the ssh process by hand is what leaves them behind, after
      which `list` reports a tunnel that is not there — which is the reason this
      command exists rather than "just Ctrl-C it".
- [ ] **S6** — `list` shows the box as **not tunnelled**, and `gcloud` shows it
      **RUNNING**. A stopped box and a disconnected one must not read the same.
- [ ] **S7** — `down --keep-running` is **refused by the parser**: `No such
      option: --keep-running`, exit 2, and **nothing is stopped and nothing is
      contacted**. The flag was the negation of its own command — `down --all
      --keep-running` read as "stop everything except don't", the most expensive
      outcome reachable from the cheapest-sounding command — so it was hidden,
      it warned that it had become `comfy-qat disconnect <name>` every time it
      was used, and it has now been removed. The criterion is the refusal, not
      the message: a removed flag that is **silently ignored** would make `down
      --keep-running` stop the box it used to leave running, which is the one
      failure this ordering exists to prevent.
- [ ] **S7b** — the same for `--os` and `--gpu` on the eleven commands that
      select a machine: `No such option: --os`, exit 2, nothing contacted. Say
      the description as the argument instead — `qat down windows` — and check
      that it still resolves and still prints what it resolved to. `create --os
      linux --gpu t4` and `quota request --gpu l4` are **not** affected and are
      exercised in phases K and C; if either has stopped taking its flags, that
      is a blocker, not this criterion passing.

*Never run. Every check here is new.*

## Phase R — moving a box *(this bills, and it is the one nothing has ever proved)*

**A real `move` end to end on hardware has never been run, by anyone.** Everything
believed about this command comes from tests. It renames a host, reassigns its
port, rewrites your hand-maintained host list, takes a snapshot, creates a disk,
creates an instance, and — when you do not name a zone — starts a GPU box to read
a zone out of Google's refusal. Six things that cost money and one that edits the
file every other command depends on.

It goes here, before phase G, on purpose: **phase I exists because of a `move`**
that took a snapshot, built a 300 GB disk, failed creating the instance, said
nothing, and billed for weeks. Running R before I is what finally points that
phase at the thing it was written for.

### R0 — the baseline. Do not skip this; nothing after it means anything without it.

> **STOP THE BOX FIRST.** On a project whose `GPUS_ALL_REGIONS` is 1 — which is
> this one — a running source holds the only slot against the box the move is
> about to build, so the move cannot go ahead while it is up. As of `cdeeddd`
> you are told that **before anything is created**; until then you were told it
> by Google at the instance create, after the snapshot and a 300 GB disk, which
> is the incident phase I was written around. Either way, stopping it first is
> what lets the move run.

```sh
P=$(gcloud config get-value project 2>/dev/null); echo "=== R0 project $P"
TARGET_ZONE=us-central1-b            # a zone $BOX is NOT in, and that has your card
echo "moving $BOX -> $TARGET_ZONE"
mkdir -p ~/move-before
echo "=== R0 stamp it WHILE IT STILL SERVES — R6b compares against this"
qat stamp $BOX > ~/move-before/stamp.txt 2>&1; cat ~/move-before/stamp.txt
echo "=== R0b the ceiling refusal, WHILE IT IS STILL RUNNING — must cost nothing"
qat move $BOX --to $TARGET_ZONE; echo "exit $?"
diff ~/qa-before/snapshots.txt <(gcloud compute snapshots list --project $P --format="table(name,diskSizeGb,storageBytes,creationTimestamp,sourceDisk.basename())") && echo "NO SNAPSHOT — the refusal was free"
echo "=== R0a now stop it — a TERMINATED source holds no GPU allowance"
qat down $BOX; echo "exit $?"
gcloud compute instances list --project $P    # confirm it reads TERMINATED
cp ~/.config/comfy-qa-tools/hosts.toml ~/move-before/hosts.toml
gcloud compute instances list --project $P > ~/move-before/instances.txt
gcloud compute disks     list --project $P > ~/move-before/disks.txt
gcloud compute snapshots list --project $P > ~/move-before/snapshots.txt
qat list > ~/move-before/list.txt
file ~/.config/comfy-qa-tools/hosts.toml            # LF or CRLF — you need to know which
grep -n "" ~/move-before/hosts.toml | head -40      # the file with line numbers
cat ~/move-before/instances.txt ~/move-before/disks.txt ~/move-before/snapshots.txt
```

**Before you capture, annotate.** Put a comment on the moved host's header line
and another on its port line, exactly as the host list's own preamble invites:

```toml
[hosts.comfy-win]  # the windows box
port = 8190        # do not reuse this port
```

A hand-maintained file has notes in it — that is the ordinary case here, not an
exotic one — and until an hour ago both of those annotations made the host
**unmovable**, with a message that was not merely unhelpful but false: `comfy-win
is not in the host list`, about a host on the screen. Do this before R0 so every
step below runs against a realistic file.

- [ ] **R0a** — **the source reads TERMINATED before you go on.** This is not
      tidiness, it is the difference between a move that completes and one that
      spends the expensive half and then fails. A stopped box holds no GPU
      allowance, so the create needs 1 of 1 and fits; a running one holds the only
      slot against itself.
- [ ] **R0b** — **if you forget R0a, the refusal is free — check that it was.**
      Run `qat move $BOX --to $TARGET_ZONE` deliberately with the box still up,
      before stopping it. It must refuse naming the ceiling, what is holding it
      and how many cards the new box needs, and end `Nothing was created.` Then
      confirm that literally: `diff ~/qa-before/snapshots.txt` against a fresh
      snapshot list. **The snapshot is the check**, not the refusal — until
      `cdeeddd` this path was not gated at all and Google refused it at the
      instance create, having already taken the snapshot and built a 300 GB disk.
      A refusal that costs nothing and a refusal that costs a disk read the same
      on the terminal.
- [ ] **R0** — you have five files in `~/move-before/` and you have looked at them.
      **`stamp.txt` has a real stamp in it, not an error.** It is taken before
      R0a stops the box, because `stamp` asks a serving ComfyUI what it is and a
      stopped box cannot answer — and R6b compares the moved box against this
      file. A `stamp.txt` holding "nothing answered" makes R6b unfalsifiable.
      Everything below is a **comparison**, and a comparison without a before is
      the commonest broken check in this pack. If you skip R0 you cannot tell a
      leak this move made from a disk that was already there.

### R1–R2 — the free half. Refusals, and a plan. Nothing is created.

```sh
echo "=== R1 the plan"; qat move $BOX --to $TARGET_ZONE --dry-run; echo "exit $?"
echo "=== R2 no zone, no dry run allowed"; time qat move $BOX --dry-run; echo "exit $?"
echo "=== R2c nowhere to move your own machine to"; time qat move local --dry-run; echo "exit $?"
echo "=== R2b nothing happened"; diff ~/move-before/instances.txt <(gcloud compute instances list --project $P) && echo "unchanged"
```

**R2 and R2c are offline**, and that is worth knowing before you book a night for
this phase: both refuse before a single call reaches Google, so they can be run
against a throwaway `--config` with no project, no credentials and no box. R2 is
the blocker check in the whole pack — a `--dry-run` that goes looking for a zone
starts a GPU box — and it has never been run, on a machine where running it costs
nothing.

**What phase R does not prove.** Every `move` below names a zone with `--to`,
because a repeatable procedure has to. The other path — `move` with no `--to`,
which starts the box to read a zone out of Google's refusal, and where a box that
*fails* to be refused is one you are now paying for with nothing to move — is
exercised here only as the R2 refusal under `--dry-run`. **Completing this phase
proves the `--to` move and leaves zone discovery unproven.** Do not read a
finished phase R as "move works".

- [ ] **R1** — a numbered plan naming **the snapshot, the new disk, the new
      instance and the target zone**, and it ends saying nothing was created.
      Read the numbers: the disk size should match the source box's, not a
      default.
- [ ] **R2** — **without `--to`, `--dry-run` refuses, and this is correct.** The
      only way to ask Google where there is capacity is to try to start the box,
      and a box that starts is billing — so a dry run cannot do it. It must say
      that, in those terms, and print the `--to … --dry-run` form as the fix.
      Exit 2. A `--dry-run` that goes looking for a zone is a **blocker**: it is
      the one command promising to change nothing, spending the most.
      **`time` it, and compare against R1.** The guard sits inside
      `_zone_with_capacity`, ahead of the call that would start the box, so a
      correct build refuses without contacting Google at all and comes back far
      faster than R1's plan, which reads the instance. A refusal that takes as
      long as R1 or longer was made after something was asked of Google — and a
      refusal printed after a start reads on the terminal exactly like this one.
- [ ] **R2c** — **`move local` says there is nowhere to move it to.** Exit 2, and
      the refusal comes from the host list before a `Gcloud` object exists — so
      like R2 it is instant and free. It is the only `move` path in this pack
      that needs neither a project nor a box, which makes it the one check of
      this command anybody can run tonight.
- [ ] **R2b** — the instance list is byte-identical to R0's. `--dry-run` created
      nothing, and you checked rather than believed the word "dry".

### R3 — the refusal that has to be free

This is the check only a real project can make. `move` writes two entries: the box
under its own name in the new zone, and the old one under `<name>-<old-zone>`. If
your list already holds an entry naming the machine it is about to name, the
resulting file is one the tool **refuses to read whole** — every command then
exits 2, `down` included, while the box bills.

```sh
cp ~/.config/comfy-qa-tools/hosts.toml ~/move-before/hosts.clash.toml
# Append a second entry pointing at the SAME instance/zone/project as $BOX,
# under a different name. Then:
echo "=== R3 the clash is refused"; qat move $BOX --to $TARGET_ZONE; echo "exit $?"
echo "=== R3b and it cost nothing"; diff ~/move-before/instances.txt <(gcloud compute instances list --project $P) && echo "unchanged"
diff ~/move-before/snapshots.txt <(gcloud compute snapshots list --project $P) && echo "no snapshot"
# put the clean file back before R4
cp ~/move-before/hosts.toml ~/.config/comfy-qa-tools/hosts.toml
```

- [ ] **R3** — refused, naming **both entries and the machine they would share**,
      and saying plainly `Nothing was created.` Exit 2.
- [ ] **R3b** — **and it really was free.** No new instance, and above all **no
      snapshot**. This is the criterion, not R3: the refusal used to arrive at the
      register step, after the snapshot, the disk and the instance existed and
      were billing. "It refused" is satisfied by both versions; "it refused
      having created nothing" is satisfied only by the right one.

### R4 — the move itself

```sh
echo "=== R4 move"; time qat move $BOX --to $TARGET_ZONE; echo "exit $?"
```

- [ ] **R4** — it says what it is doing at each step, and finishes with
      `<name> is now in <zone>, running and billing from now.` **The words
      "running and billing" have to be there**: creating an instance starts it,
      and a closing line that says "now run `go`" reads as "now start it" — which
      is how a moved box billed silently from the moment the move finished.
- [ ] **R4b** — it then tells you about the **old** box: still in the old zone,
      now called `<name>-<old-zone>`, with its disk. A move that leaves you one
      box is not what happened; you have two, and two host-list entries.
- [ ] **R4c** — **the sentence about the old box matches the box.** `move` never
      stops the source — the "leave it" step is a statement, not an action — so
      what it says depends on what the source was when the plan was made:

      - source was **running**: `running and still billing`, and the closing lines
        offer **both** `comfy-qat down <name>-<old-zone>` and the `gcloud … delete`.
      - source was **stopped**: `stopped`, and **only** the delete line. `down` on
        a stopped box is a no-op, so offering it would be noise.

      **You stopped the box at R0a, so `stopped` is what you should see here** —
      and only the delete line under it. The other branch is what you would get
      having moved a running box, which on this project you cannot do; if you ever
      see `stopped` about a box `gcloud` shows as RUNNING, that is the defect this
      criterion exists for. It said "stopped" unconditionally until 2026-09-01,
      which is a false statement about a GPU that is billing. Check the sentence
      against R9's list rather than taking it on its own.

### R5 — the host list, which is the part with no undo

```sh
echo "=== R5 what changed"; diff ~/move-before/hosts.toml ~/.config/comfy-qa-tools/hosts.toml
echo "=== R5b line endings"; file ~/.config/comfy-qa-tools/hosts.toml
echo "=== R5c the tool can still read it"; qat list; echo "exit $?"
echo "=== R5d backup"; ls -l ~/.config/comfy-qa-tools/hosts.toml.bak
```

- [ ] **R5** — read the `diff` **line by line**. Exactly two things changed: the
      moved host's block was renamed to `[hosts.<name>-<old-zone>]` and given a
      **different** port, and a new block for `<name>` was appended. Everything
      else is untouched — every other host, every comment, and the commented-out
      example `init` writes into every new file.
- [ ] **R5b** — **the renamed block did not move to column 0**, and any
      indentation it had is still there. The rewrite is textual and has to put
      back what the header match consumed; dropping it is a silent whole-file
      diff that looks like a reformat and is not one.
- [ ] **R5c** — the line endings are what R0 said they were. A hard-coded `\n`
      puts a lone LF into a CRLF file, and a host list edited on the Windows box —
      the ordinary case here — comes back mixed.
- [ ] **R5d** — **the two entries have different ports, and neither is 8188.** A
      rename that kept the old port is satisfied by "the host appears under its
      new name", which is why that is not the check. Confirm with `qat list`:
      two rows, two ports.
- [ ] **R5e** — `qat list` still works, exit 0. The file was validated by the
      tool's own loader before it was written — a file that parses as TOML and
      holds the right names can still be refused at load, and if it is, no
      `comfy-qat` command works at all until you hand-edit it.
- [ ] **R5f** — a backup of the previous file is at `hosts.toml.bak`, and it
      matches `~/move-before/hosts.toml`.
- [ ] **R5g** — **the annotated host moved at all.** A note on the header line
      used to make it invisible to both `move` and `delete` — "not in the host
      list", about a host right there — and a note on the port line produced "has
      no port line, so its port cannot be freed" about a line one row below. If
      either message appears, stop: the file is fine and the tool is wrong.
- [ ] **R5h** — **and your notes are still there, both of them**, on the renamed
      header and beside the port. The wrong fix for R5g is a pattern that swallows
      the comment; a rewrite that silently ate `# do not reuse this port` would be
      worse than the refusal it replaced. Check the `diff` — the comments move
      with their lines and are not consumed by them.

### R6 — did the box survive the move?

```sh
echo "=== R6 serve it"; qat go $BOX --no-browser; echo "exit $?"
echo "=== R6b same machine?"; qat stamp $BOX; cat ~/move-before/stamp.txt
```

- [ ] **R6** — `go` reaches the **new** box and ComfyUI is there without being
      reinstalled. The point of `move` over `create` is that the install comes
      with it; if it reinstalls, the move preserved nothing worth having.
- [ ] **R6b** — the stamp names the same OS and card as R0's, in the **new** zone.
      Compare the two files. A move that quietly built a different machine type
      passes every check above this one.

### R7 — what did it leave behind?

```sh
echo "=== R7 instances"; diff ~/move-before/instances.txt <(gcloud compute instances list --project $P)
echo "=== R7b disks";    diff ~/move-before/disks.txt     <(gcloud compute disks list --project $P)
echo "=== R7c snapshots";diff ~/move-before/snapshots.txt <(gcloud compute snapshots list --project $P)
```

- [ ] **R7** — every line the diffs add is one the tool **told you about**. One
      new instance in the target zone, the old one still there, one new disk. If
      `gcloud` shows you something `move` never mentioned, that is a defect in the
      tool, not a tidy-up job — it is the exact failure phase I was written for.
- [ ] **R7b** — **the snapshot is gone, or you were told it is still there and
      billing.** A move takes a snapshot to build the new disk from; leaving it is
      300 GB nobody is looking at. Either outcome can be correct — silence cannot.
- [ ] **R7c** — now run **phase I** in full. R is the only phase that creates the
      kind of leak I detects, so this is the first time that phase has ever been
      pointed at its own reason for existing.

### R9 — what is billing now, read from Google and not from the tool

```sh
echo "=== R9 what is actually running"; gcloud compute instances list --project $P --filter="status=RUNNING"
echo "=== R9b what the tool thinks"; qat list --live
```

- [ ] **R9** — **read from Google what is running, and count it yourself.** You
      stopped the source at R0a and `move` never starts it again, so you should
      see **two instances and one of them RUNNING**: the new box in the target
      zone, running and billing GPU-hours, and the old one — now declared as
      `<name>-<old-zone>` — TERMINATED and costing only its disk. Two boxes and
      two host-list entries either way; it is the second one's *state* that
      depends on how you started. *(Had you moved a running box, both would be
      billing GPU-hours — which on a ceiling of 1 you could not have done. That is
      R0b.)*
- [ ] **R9b** — `qat list --live` agrees with the line above, name for name. This
      is the one place in the phase where the tool's account and Google's can be
      set side by side, and a disagreement here is worth more than either alone.
- [ ] **R9c** — **you have decided what to do with the old box before moving on.**
      Stop it (`comfy-qat down <name>-<old-zone>`), or delete it and its disk with
      the `gcloud` line R4b printed. Phase G's `down --all` will stop it because it
      is declared — but "something later will probably catch it" is how a box runs
      all night, and this is the moment you know it exists.

### R8 — if it goes wrong halfway

**Read this before you start, not after.** A move that fails partway leaves a GPU
box billing and a host list you cannot yet trust. In that order:

```sh
echo "=== R8 what the tool said it left"   # scroll back to the failure and read it
echo "=== R8b is the original still mine"; qat stamp $BOX; echo "exit $?"
qat list --live                     # what exists, and what is running
gcloud compute instances list --project $P
gcloud compute disks     list --project $P
gcloud compute snapshots list --project $P
```

- [ ] **R8** — on failure the tool prints **what it created and that it is
      billing**, one line each, and says the original box is untouched in its old
      zone. It must not exit claiming success, and it must not go silent.
- [ ] **R8b** — **the original is still there and still yours.** `move` builds the
      new box before it retires the old one, so a failure leaves you the machine
      you started with. Check with `qat stamp $BOX` — if the host list was already
      rewritten, `qat stamp <name>-<old-zone>`.
- [ ] **R8c** — **before you go to bed**: stop everything the diffs in R7 show as
      running, and delete anything the tool told you it left. `qat down --all`
      stops declared boxes; a half-made instance that never reached your host list
      is not declared, so stop it with the `gcloud` line the tool printed.
      *(Only reachable on a real failure — record it as not run otherwise.)*
- [ ] **R8d** — a resumed `move` **reuses** what the failed one made rather than
      starting again, and `--clean` deletes the leftovers first and then still
      does the move. `--clean` is "clean up first, then move", never "clean up
      instead of moving". *(Needs a failed move to resume from — not arrangeable
      to order; record as not run.)*

*Never run. Every check in this phase is new, and the phase exists because
`move` is the largest untested surface in the tool.*

## Phase G — stop paying *(do not skip)*

```sh
echo "=== G1 down"; qat down $BOX
echo "=== G2 tunnel is gone"; ls -l ~/.config/comfy-qa-tools/tunnels/ 2>&1
echo "=== G3 nothing answers"; qat stamp $BOX; echo "exit $?"
echo "=== G4 instance is TERMINATED"; gcloud compute instances list
echo "=== G5 down again is harmless"; qat down $BOX; echo "exit $?"
echo "=== G6a stop everything, by not naming anything"; qat down --all; echo "exit $?"
echo "=== G6b --all with a name as well"; qat down --all $BOX; echo "exit $?"
G=$(mktemp -d); printf '[hosts.local]\nkind = "local"\nport = 8188\n' > $G/nocloud.toml
echo "=== G6c --all with no cloud box declared"; qat down --all --config $G/nocloud.toml; echo "exit $?"
```

- [ ] **G1** — says it closed the tunnel and stopped the machine. **If you ran
      phase R, this stops ONE of the two boxes you now have** — `down` takes a
      name, and the retired `<name>-<old-zone>` is a different declared host.
- [ ] **G2** — **all three** of that host's tunnel files are gone: `.pid`, `.log`
      and `.json`. Checking only the `.pid` is the same blind spot as E5 running the
      other way — `list` reads the `.json`, so one left behind reports a tunnel that
      is not there.
- [ ] **G3** — nothing answered on that port. Exit 1.
- [ ] **G4** — **every instance on the project that you are not deliberately
      running shows TERMINATED** — not just the one you named. Read the whole
      list. If you ran phase R there are two of yours, and G1 stopped one; if you
      ran phase K there may be a box from a half-finished create. **Anything still
      RUNNING here that you did not mean to leave running is a blocker.** This
      criterion said "the instance" while the pack could only make one box; phase R
      makes two, and one TERMINATED line is no longer an answer.
- [ ] **G5** — **it says `<name> was already stopped`, in those words.** Not
      merely "exits 0": stopping a box that is already stopped succeeds trivially,
      so a `down` that did nothing whatever — or one that failed to read the state
      and pressed on — exits 0 too. The tool distinguishes the two cases
      internally and prints a different sentence for each (`was already stopped`
      against `was running — stopped it`); this criterion is that the right one
      reaches you. Compare with what G1 printed a moment ago: they must differ.
- [ ] **G6a** — `qat down --all` takes no name and stops every declared cloud box.
      **Read which of three closing sentences you got — not how many it counted.**
      They are different claims and only one is an all-clear:

      - `was billing: <names>. Stopped. Nothing is now.` — those boxes were read as
        RUNNING and then stopped. An observation.
      - `<n> machine(s) could not be checked before stopping, so it may have been
        billing: <names>.` — the state read failed, or the verdict was not
        recognised. **This is not an all-clear**, and it should point you at
        `comfy-qat list --live`.
      - `nothing was running, so nothing was billing.` — the only all-clear, and it
        must appear **only** when nothing was unchecked *and* the project-wide
        survey came back empty.

      A count cannot tell those apart, and that is the whole defect: a session in
      which every state read failed and every stop trivially succeeded printed the
      same "all N stopped" as a clean one. If you see the third sentence, satisfy
      yourself you were not also told something could not be checked — the two
      together are a contradiction and a fail.
- [ ] **G6d** — a box that **refuses to stop** does not leave the rest running: it
      stops the others, then names what did not stop and what it may still be
      costing, and exits non-zero. *(Needs a box that will not stop, so it is not
      arrangeable — record it as not run rather than ticking it from a clean run.
      Same check as M4b, same problem.)*
- [ ] **G6b** — `--all` **with** a name is refused rather than guessing which of
      the two you meant. Exit 2, nothing stopped.
- [ ] **G6c** — on a host list with no cloud boxes at all, `--all` does **not**
      claim an all-clear it has not earned: it either names machines running on
      the project that you have not declared, or says the project could not be
      checked and that this is therefore not an all-clear.

*Ran 2026-08-27 — **all of phase G passed** (G6 excepted; it did not exist).
Never skip this phase: G4 is the check that says you have stopped paying.*

## Phase I — what did it leave behind? *(no cost to run, catches the costly)*

The failure that motivated this phase happened on a real project: a `move` took a
snapshot, created a 300 GB disk in the destination zone, then failed creating the
instance. It left both artifacts behind, said nothing about them, and they billed
for weeks. The tool told the user which zone had capacity and could not build
there — so the expensive half of the work succeeded and the useful half did not.

**This is the second half of a comparison.** The first half is the `Q0` block you
ran before phase K. If you skipped it, stop and say so in the report rather than
ticking these — a single snapshot of a project you did not photograph first tells
you what exists, never what this run did.

```sh
P=$(gcloud config get-value project 2>/dev/null); echo "=== I0 project $P"
echo "=== I1 instances"; gcloud compute instances list --project $P > /tmp/i1.txt
diff ~/qa-before/instances.txt /tmp/i1.txt && echo "IDENTICAL — nothing added"
echo "=== I2 disks — USERS empty means nothing is attached"
gcloud compute disks list --project $P --format="table(name,zone.basename(),sizeGb,type.basename(),users.basename())" > /tmp/i2.txt
diff ~/qa-before/disks.txt /tmp/i2.txt && echo "IDENTICAL — nothing added"
echo "=== I3 snapshots"
gcloud compute snapshots list --project $P --format="table(name,diskSizeGb,storageBytes,creationTimestamp,sourceDisk.basename())" > /tmp/i3.txt
diff ~/qa-before/snapshots.txt /tmp/i3.txt && echo "IDENTICAL — nothing added"
echo "=== I4 anything the host list names that no longer exists"
qat list
```

- [ ] **I1** — **read the `diff`, not the list.** Every `>` line is something this
      run created. Each one is either a box you still mean to have, or a leak.
      Every instance of yours that you are not deliberately running reads
      TERMINATED — and if you ran phase R there are two of them.
- [ ] **I2** — **every `>` line in the disk diff is accounted for.** A disk with an
      empty USERS column is attached to nothing and still bills at full size. The
      point of the diff is that a detached disk which was there before phase 0 is
      not this run's problem and a new one is — and the single-snapshot version of
      this check could not tell you which you were looking at.
- [ ] **I3** — **every `>` line in the snapshot diff is accounted for.** A move
      takes a snapshot to build the new disk from; one left behind is 300 GB
      nobody is looking at, and it is the exact artefact that started this phase.
- [ ] **I3b** — **`IDENTICAL — nothing added` on all three is a pass only if you
      also deleted the box.** After phases K–R and before phase N you *should* see
      added lines; they are the run working. This criterion is not "no changes",
      it is "no changes you cannot name".
- [ ] **I4** — the host list names no machine that is gone, and nothing exists in
      the project that the host list does not know about.
- [ ] **I5** — *(only exists if a `move` ran this session; otherwise not run, and
      excluded from the release-1 set for that reason.)* If a `move` ran during
      this session, the tool **told you** what it
      created and what it left, in its own output. Finding a leak with `gcloud`
      that the tool never mentioned is a defect in the tool, not a tidy-up job.

The rule this phase enforces: **a command that spends money must account for what
it spent it on.** Silence is the defect.

*Ran 2026-08-27 — all of phase I was ticked. **Read that as less than it looks.**
There was no `Q0` block then, so I1–I3 were single snapshots of a project nobody
had photographed first: the boxes were ticked honestly and compared nothing. The
phase has never once been run as a comparison. Run it again after phase K —
`create` is a second command that spends money, so it is a second thing that can
leave something behind — and after phase R, which is the only phase that makes
the kind of leak this phase was written for.*

## Phase N — deleting a box *(this destroys a machine; run it after H and J)*

**Nothing in this phase has ever been run, and until now nothing in this pack
mentioned `delete` at all.** It is the only command here that cannot be undone:
it removes the instance *and* its boot disk, and the ComfyUI on it, the models on
it and whatever a test run left behind go with them. Nothing brings any of it
back.

It is written after phase I on purpose, so that the pairing is on the page: delete
the box phase K made, then **run phase I again**. A delete that leaves a 300 GB
disk behind is the exact leak phase I exists to catch, and this is the one command
guaranteed to produce it if it is wrong.

**Run it after H and J, not here.** N1–N7 are refusals and can be run at any point
— they are the cheapest checks in the pack. But N9 takes the box out of your host
list, and J2–J4 resolve `windows`, `l4` and `windows/l4` against the hosts that
are declared. Delete first and those checks have nothing to resolve to.

N1–N7 are refusals. They cost nothing and destroy nothing, and they are most of
the value of this phase: everything worth protecting here is protected before the
prompt, not by it.

**N1–N5 are offline**, which is stronger than "cheap". All five are resolved and
refused against the host list before `delete` builds a `Gcloud` object, so they
need no project, no credentials and **no box at all** — a throwaway `--config`
holding a `local` entry and one `gce` entry is enough, and `$BOX` need only be a
name that file contains. Run them tonight; they are five of the seven ticks
`delete` has never had. **N6 onwards is where the cloud starts**: N6 reads the
instance's state, and N7 reaches the prompt only after that read, so both need a
real declared box (a stopped one is fine and costs only its disk).

```sh
echo "=== N1 a description is not a name"; qat delete windows; echo "exit $?"
echo "=== N2 no name at all";              qat delete;         echo "exit $?"
echo "=== N3 the right name, wrong case";  qat delete "$(echo $BOX | tr '[:lower:]' '[:upper:]')"; echo "exit $?"
echo "=== N4 a name nothing has";          qat delete nosuchbox; echo "exit $?"
echo "=== N5 your own machine";            qat delete local;   echo "exit $?"
echo "=== N7 type the wrong name at the prompt"; qat delete $BOX   # then type anything else
```

- [ ] **N1** — **`delete windows` is refused.** This is the most important check in
      the phase. Every other command in the tool takes a description — `go
      windows`, `stamp l4` — because "the Windows one" is a fine way to say which
      machine to work on. It is a terrible way to say which machine to destroy,
      and the first version of this command resolved it exactly like the others,
      with a comment claiming it did not. The message says delete takes an exact
      name, never a description, and says why: a description can resolve to a
      machine you did not picture, and this cannot be undone. Exit 2.
- [ ] **N2** — with no name at all it asks which machine and points at
      `comfy-qat list`. Exit 2. It must not default to anything.
- [ ] **N3** — a name that differs only in case is **not** accepted, and the tool
      offers the real one: "no host is called 'COMFY-WIN'. Did you mean
      'comfy-win'?", with the corrected command. Exit 2. Naming it back to you is
      the point — you retype the tool's spelling, not your own.
- [ ] **N4** — a name nothing has is refused by name, pointing at `list`. Exit 2.
- [ ] **N5** — `delete local` says it is this machine, not a cloud box. Exit 2.
- [ ] **N6** — **a box that is not stopped is refused**, naming the state it is
      actually in and pointing at `comfy-qat down <box>`. Not a warning, a refusal:
      GCE will happily delete a running instance, and the promise this command
      makes is that what you destroy is something you looked at seconds ago.
      *(Run at the end of phase E, while the box is still up — it costs nothing
      there, and there is no free way to reach a running box from here.)*
- [ ] **N6b** — the refusal covers **every** state that is not stopped, not just
      RUNNING. A machine has eight states and exactly one of them is stopped;
      STAGING, PROVISIONING, STOPPING and SUSPENDED are none of them, and
      `delete` at a box on its way down is a plausible thing to type. If you
      cannot catch a transitional state, record N6b as **not run** rather than
      assuming it from N6. *(Needs a box mid-transition; the window is seconds.)*
- [ ] **N6c** — **the state it names is never blank.** `instance_status` has a
      third answer besides a state and an error: an empty string, meaning the read
      succeeded and said nothing about the machine. `delete` still refuses, which
      is the right direction — but the refusal has to read `… is in an unknown
      state, not stopped`, not `… is , not stopped`. This is the branch nobody
      probes, and it is one `or` away from being the second kind. *(Needs a
      `describe` that succeeds and returns no status — not arrangeable by hand.)*
- [ ] **N7** — typing anything other than the name at the prompt deletes nothing
      and says `nothing was deleted.` **Exit 2, not 1** — 2 means nothing was
      changed and 1 means the work started and failed, and declining is the first
      of those. It exited 1, which reads to a script as an attempted delete that
      went wrong.
- [ ] **N7b** — with no terminal to prompt in (a pipe, a script) it refuses and
      tells you to pass `--yes` if you are sure. It must never treat "cannot ask"
      as "go ahead". Check with `qat delete $BOX < /dev/null | cat`.
- [ ] **N7c** — the friction is **typing the name**, not a `[y/N]`. A `[y/N]` is
      answered by reflex at 2am; a name is not. If this has become a yes/no
      prompt, that is a fail on its own.

Now the real one. Arrange the host list first, because one delete is all you get
and it is the only way to see what the rewrite does to the file:

```sh
echo "=== N8 keep a copy to compare against"
cp ~/.config/comfy-qa-tools/hosts.toml ~/hosts.before
```

Edit `~/.config/comfy-qa-tools/hosts.toml` so that, before you delete:
the box you are deleting is **not** the first `[hosts.…]` table in the file;
a `# comment line` sits directly above its header; and a
`# DO NOT DELETE — this one holds the checkpoint` line sits directly above the
**next** host's header, **with no blank line between the two blocks**. That is how
a hand-maintained host list actually looks, and it is the arrangement that has
destroyed comments before.

```sh
echo "=== N9 delete it";        qat delete $BOX   # type the name
echo "=== N10 gone from Google"; gcloud compute instances list
echo "=== N11 and its disk";     gcloud compute disks list
echo "=== N12 what changed in the file"; diff ~/hosts.before ~/.config/comfy-qa-tools/hosts.toml
echo "=== N13 the tool still reads it"; qat list; echo "exit $?"
```

- [ ] **N8/N9** — before it does anything it prints what it is about to destroy —
      the instance, its zone, **and its boot disk** — and says this cannot be
      undone and that the ComfyUI on it goes too. Then it asks you to type the
      name.
- [ ] **N10** — the instance is gone from `gcloud compute instances list`. Not
      TERMINATED. Gone.
- [ ] **N11** — **the disk is gone too, and this is the check that costs money if
      it is wrong.** Boot disks here are created `auto-delete=no`, so deleting the
      instance alone leaves 200–300 GB billing with nothing attached to it — which
      looks like nothing at all in a console, and is the leftover people actually
      get caught by. A disk still listed here is a **blocker**.
- [ ] **N12** — the `[hosts.<name>]` entry is **out of the host list**, not left
      behind as a note. This is not tidying: `create` refuses a name that a host
      list entry holds, and ports are allocated from the same list, so a leftover
      entry reserves both a name and a port for a machine that does not exist —
      and the refusal arrives weeks later with nothing to connect it to tonight.
      The tool says so in its closing line: "…and it is out of your host list."
- [ ] **N12a** — **the name is genuinely reusable afterwards**, which is the thing
      the entry was blocking. Confirm it rather than inferring it from the diff:
      `qat create --os linux --gpu t4 --name <the deleted name> --dry-run` must
      plan, not refuse with "already taken". *(A dry run creates nothing.)*
- [ ] **N12b** — **the deleted host's own note went with it.** A comment sitting
      directly above a block describes that block; leaving it strands a note about
      a machine that no longer exists.
- [ ] **N12c** — **the next host's `# DO NOT DELETE` line is still there**, even
      though the two blocks were touching. A block runs to the next `[`, so
      everything in the gap used to be taken as well — and in a hand-maintained
      file the gap is exactly where the next host's notes live. That defect passed
      every guard: the file parsed, the names all matched, and it reported plain
      success while destroying a line saying DO NOT DELETE. Read the `diff`
      output line by line; do not skim it.
- [ ] **N12d** — deleting the **first** host in the file leaves the comment run at
      the top of the file alone. Nothing in the text can tell a file preamble from
      the first host's own note, so for something irreversible the tool errs
      toward leaving something behind: a stranded comment is removed by hand in
      two seconds, a destroyed one is gone. *(Needs a second box to delete, or a
      second run. Record as **not run** rather than assuming it from N12b — this
      is the branch, not a repeat of it.)*
- [ ] **N12g** — **a note ON the header line does not make the host invisible.**
      Give the box you are deleting a `[hosts.<name>]  # something` comment before
      N9. Until an hour ago that produced `no host is called '<name>'` from
      `delete` — and the same annotation took `move` out too, so this is the same
      defect reaching two commands. The refusal is the failure; a rewrite that
      deletes the note instead is the other one. The block goes, the note goes
      with its own block, and nothing else moves.
- [ ] **N12f** — **a host list that arrived with Windows line endings comes back
      with them.** The rewrite is textual, and the separator it writes used to be a
      hard-coded `\n`, which puts a lone LF into a CRLF file. That is not cosmetic
      here: this tool's whole point is testing on a Windows box, so a host list
      edited there is the ordinary case, and a file with mixed endings is one a
      person then has to repair by hand after an operation that cannot be undone.
      Check with `file ~/.config/comfy-qa-tools/hosts.toml` before and after, or
      `grep -c $'\r' `. *(Needs a CRLF host list to start from — make one with
      `unix2dos`, or edit it on the Windows box.)*
- [ ] **N12e** — a backup of the old file is at
      `~/.config/comfy-qa-tools/hosts.toml.bak`, and `~/hosts.before` matches it.
- [ ] **N13** — `qat list` still works and no longer names the box. The rewrite is
      validated by the tool's **own loader** before it is written, not by a proxy
      for it: a file that parses as TOML and holds exactly the right names can
      still be refused at load, after which no `comfy-qat` command works at all
      until somebody hand-edits it.
- [ ] **N14** — **run phase I again now.** Nothing the delete touched appears as an
      unattached disk or an orphan snapshot. This is the phase's real conclusion:
      the refusals above are free, and this is the one that tells you the money
      stopped.
- [ ] **N15** — if the box is deleted but the host list cannot be rewritten, the
      tool says **both** — that the box and its disk are gone, *and* that the entry
      is still in the file and must come out by hand, naming what it will otherwise
      break. **Exit 1, not 2**: the work started and half of it failed, and a 2
      there would say nothing had changed while a machine had just been destroyed.
      *(Hard to arrange deliberately — make the file read-only before N9 if you
      want it, and record it as not run otherwise.)*

*Never run. Every check here is new, and `delete` had no acceptance criterion of
any kind before this phase existed.*

## Phase H — the promise the README makes

```sh
echo "=== H1 footprint"; find ~/.config/comfy-qa-tools -type f | sed "s|$HOME|~|"
echo "=== H2 no credential anywhere in it"; grep -rIl -e "ya29." -e "-----BEGIN" ~/.config/comfy-qa-tools/ 2>&1; echo "exit $? (1 = clean)"
echo "=== H3 shell untouched"; grep -c "comfy-qat" ~/.zshrc ~/.bashrc 2>/dev/null
echo "=== H4 setup is safe to re-run"; qat setup --non-interactive 2>&1 | tail -20
```

- [ ] **H1** — only `hosts.toml`, `tunnels/` and — once `create` has run —
      `zone-latency.json`. Nothing outside that directory.
- [ ] **H2** — no token, no key. `grep` finds nothing.
- [ ] **H3** — only lines you added yourself; the tool has not edited your shell config.
- [ ] **H4** — skips what is already done, changes nothing, does not overwrite the host list, and does not prompt.

*Ran 2026-08-27 — **all of phase H passed**, though H1 was ticked before
`zone-latency.json` existed. That file is written by `create` and reused for a
week; it is expected, and it is the only addition.*

---

## Phase J — switching machines *(offline parts free; E-J together bill)*

The workflow this release exists for: you are testing on one box and you need the
other OS, or the other card. Everything up to J4 is offline and costs nothing.

```sh
echo "=== J1 what have I got, and what is up"; qat list
echo "=== J2 name a machine by its OS"; qat stamp windows; echo "exit $?"
echo "=== J3 name a machine by its card"; qat stamp l4; echo "exit $?"
echo "=== J4 both halves"; qat stamp windows/l4; echo "exit $?"
echo "=== J5 the wrong separator"; qat stamp windows-l4; echo "exit $?"
echo "=== J6 something you do not have"; qat stamp rtx4090; echo "exit $?"
echo "=== J7 the plan, without doing it"; qat switch windows --dry-run; echo "exit $?"
echo "=== J7b the plan that leaves the others up"; qat switch windows --keep-others --dry-run; echo "exit $?"
echo "=== J7c and nothing started or stopped"; gcloud compute instances list --project $P
```

- [ ] **J1** — one line per machine with OS, card, URL and STATE. Without `--live`
      STATE reports **only what this machine knows** — whether a tunnel is open —
      and says so under the table, **including on a host list with no cloud boxes
      in it**, which is the reader who sees nothing but dashes. A cloud box with
      no tunnel reads `not tunnelled`, not a bare `-`: a running box and a stopped
      one must not look identical. The local install reads `-`, which is correct:
      it has no tunnel to have.
- [ ] **J1b** — `--live` goes and asks, and asks **both**: Google what each cloud
      box is doing, one call per project, and this machine whether ComfyUI is
      answering. With ComfyUI up, `local` reads `serving`; stop it and re-run and
      it reads `not serving`. The two runs must not print the same cell — that is
      the whole criterion, and it was `-` in both for the first release. On a
      host list holding only `local` this must make **no** cloud call and still
      answer.
- [ ] **J2/J3/J4** — each resolves to exactly one machine and **prints what it
      resolved to** before doing anything: `windows -> comfy-win (Windows Server
      2022, L4)`. A silent resolution is a fail even if it picks correctly.
- [ ] **J5** — says the separator is `/` and shows `windows/l4`. Exit 2.
- [ ] **J6** — lists what is declared *and* the vocabulary it accepts. Exit 2.
- [ ] **J7** — states what it would start and what it would stop, then stops. It
      may ask Google what is already running — one `describe` per *other* cloud
      box — which is a read and is correct.
- [ ] **J7c** — **the instance states are what they were before J7 ran**, read
      from Google rather than from the plan. `--dry-run` prints what it *would*
      do, and a build that did it prints exactly the same thing.
- [ ] **J7b** — `--keep-others` changes the plan to **start the target and stop
      nothing**, and says so in those words, naming the flag. The plan must make
      plain that the other machines keep running, because they keep billing —
      this is the only flag in the tool whose effect is that two GPU boxes are up
      at once, and a plan that merely omits the "stop" lines is a fail. Free:
      `--dry-run` does nothing either way.

With two or more cloud boxes declared, the ambiguity case matters more than any
of the above:

- [ ] **J8** — with two Windows boxes, `switch windows` refuses and names
      both with their cards. **It must never pick one.** Guessing here is the
      whole failure this tool exists to prevent. *(Needs two Windows boxes
      declared. Declaring them costs nothing; this is why J8/J9 are no longer in
      the release-1 set.)*
- [ ] **J9** — `switch windows/l4` then resolves cleanly to the one you meant.
      *(Same precondition as J8.)*

The real switch, which bills — **and which has never been run.** J10 and J11 need
two cloud boxes able to be *up at the same time*, however briefly: `switch` starts
the target before stopping the one you were on, deliberately, so that a target
that will not start still leaves you a working machine. On a project whose
`GPUS_ALL_REGIONS` ceiling is 1 the second start is refused by quota and the check
cannot be attempted at all. Getting quota for two cards is the only way to run it.

```sh
echo "=== J10 switch"; qat switch windows
echo "=== J11 what is up now"; qat list --live
```

- [ ] **J10** — starts the one you asked for, waits until ComfyUI answers, **then**
      stops the one you were on, and says both. The order matters: if the target
      cannot start you must still have the machine you were using. *(Not run —
      needs quota for two GPUs at once.)*
- [ ] **J11** — exactly one cloud box running, and it is the one you asked for.
      *(Not run, same reason.)*
- [ ] **J10b** — **on a project with a ceiling of 1, `switch` reverses the order on
      purpose, and says why.** It prints something like "your quota allows 1 GPU
      machine at a time, so <target> cannot start until the other one stops",
      stops the old box first, and only then starts the target. This *is* runnable
      on a ceiling-of-1 project and is the check to run in place of J10 there.
      What it must not do is start the target, be refused with `Quota
      'NVIDIA_L4_GPUS' exceeded. Limit: 1.0`, and report that as a failure — that
      happened, and it was arithmetic that was knowable beforehand.
- [ ] **J10c** — the reversal happens **only when the arithmetic is certain**. If
      the ceiling cannot be read, `switch` must fall back to target-first: not
      knowing is not a reason to stop the machine you are working on.

And the case that started all this — switching when the box you want cannot start:

- [ ] **J12** — on a stockout, `switch` leaves the machine you were on untouched
      and says so.
- [ ] **J13** — it then lists where you *can* test, easiest first, same OS before
      a different one, and marks any alternative in the same zone as likely to hit
      the same shortage.
- [ ] **J14** — only after that does it offer `move`, and it says the zone
      Google named is where there was capacity *when it asked* — not a promise.
- [ ] **J15** — with one box and nowhere to go, it says that plainly and points at
      `discover` rather than leaving you at a dead end.

*Ran 2026-08-27: **J1–J9 and J12–J14 passed**. J10/J11 not run — they need quota
for two GPUs at once. J10b, J10c and J15 not run. The old spellings seen in
that output belong to A8, which is where they are counted.*

## Putting your machine back

```sh
echo "=== restore the host list"
rm -rf ~/.config/comfy-qa-tools && mv ~/.config/comfy-qa-tools.before-test ~/.config/comfy-qa-tools
ls -R ~/.config/comfy-qa-tools
echo "=== drop the test clone"
rm -rf "${REPO:?}"
```

The install stays where phase 0 put it. If you normally run this from a
development checkout of your own, reinstall from there with `pip install -e .`
once the clone is gone — `-e` means whichever checkout you installed last is the
one the binary runs.

## Reporting the result

Paste the whole terminal. For anything that failed, the useful facts are: the
phase and check id, what it printed, and the exit code. A check that could not be
run — no capacity, no second box — is "not run", not a pass.

A release-1 pass needs: every box in phases A–D, G, H and I ticked **except I5,
which only exists if a `move` ran**; **K1–K7, K4d and E0, E0b, E3, E3b, E3c, E4,
E6–E6e** ticked; **F0, F1 and F1a** ticked — the run of `up` this pack has never
had; **L1–L3, L5a, L5b and L7** ticked; **J1–J7** ticked; **S0, S1a–S1b, S4, S4b,
S5 and S6** ticked; **N1–N5, N7, N10, N11, N12 and N14** ticked; **R0–R3b and
R2c** ticked — they are free, and R3b is the one that proves a refusal costs
nothing; **Q0** ticked, without which I1–I3 cannot be; and no unexplained
traceback anywhere in the run.

**Six of those are new because the old ones passed on a path that does nothing.**
`E6d` replaces D4 as the run of `open`, `F0/F1/F1a` replace F1 as the run of
`up`, and `E3c` and `E0b` replace F2 as the run of `go` and of `--no-install`.
D4, F1b and F2 stay in the pack and stay tickable — they grade real early returns
— but a release cannot be signed off on them alone, which is what happened twice.

**C9c is not in the required set**, deliberately. It covers `quota request`'s
default `--wait`, which files a request that cannot be withdrawn and can hold the
terminal for half an hour. It costs no money and it is the path a reader of the
README actually gets, so it is worth running once and recording — but a release
should not wait on Google's approvals queue.

**Phase R's billing half (R4–R7) is the most valuable thing in this pack and the
least proved.** It is not in the required set, because a pass should not be
blocked on capacity in a second zone — but if you run it, R4, R4b, R5, R5d, R5e
and R7 must all tick, and if you skip it say so in the report. "A move has never
been run" is the sentence this pack exists to stop being true. Anything needing a
second simultaneous GPU box (J8, J9, J10, J11, all of M) or a real stockout
(F6–F8, J12–J15) is recorded as "not run" rather than assumed, and you say which
and why. **J8 and J9 moved out of the required set for that reason** — they need
two Windows boxes declared, and requiring a box that cannot be exercised is how
G6 came to be ticked on faith for a release.

Two of those need saying plainly. **N11** — the disk gone with the instance — is a
blocker on its own: a delete that leaves the disk behind bills for a machine that
no longer exists. And **A8 now passes**, as of `442f867`: the three sites it used
to name are gone. This page told the tester "this fails today" after they were
fixed, which is the same defect as a tick with no observation attached, running
the other way — a stale criterion grades a correct build as broken, and a tester
who trusts the page writes up a defect that is not there. If A8 does hit, treat it
as a regression against the commit A0 recorded.

**What the next run is actually for.** `create` and `logs` have never been run at
all, and the detached `go` has never been run in the form E3 now describes. Those
are phases K, L and E, and they are the point. **If time runs out, run those and
record the rest as carried forward.**

**What can no longer be carried forward, and why.** The 2026-08-27 pass covered
A–D, G, H, I, E4, F1, F2, J1–J9 and J12–J14, and that used to be the list you
could skip. **21 of those boxes are not the same boxes any more** — the criteria
themselves were rewritten, so a 2026-08-27 tick is a tick against different
words:

| | |
|---|---|
| **A3, A6, A8** | A3 listed 17 commands where the binary has 21; A6 quoted guide text that no longer exists — both failed a correct build. **A8 did the same in reverse**: it told the tester "this fails today" about three sites that had been fixed. It passes as of `442f867`, and A8b is the new check that the grep can see anything at all |
| **B2, B2b, B0b** | "Nothing overwritten" had no step that could notice; B0b stops the page teaching invented project ids |
| **C8, C9** | C8 says when it is unreachable; C9 covers `quota request`, which the pack never ran |
| **G1, G2, G4, G5, G6a–G6d** | G4 checked one instance and phase R makes two; G5 passed on a `down` that did nothing; G6 required behaviour no block ran |
| **I1, I2, I3, I3b, I5** | **the whole phase.** It said "compare" and never captured a before. Q0 is now that before, and it must be run before phase K |

Everything else on the 2026-08-27 list still carries forward: **D, H, E4, F1, F2,
J1–J6 and J12–J14**. If you carry a box forward, say which pass you are carrying
it from — a tick against wording that has since changed is the thing this table
exists to stop.
