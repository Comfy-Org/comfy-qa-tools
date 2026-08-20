# Roadmap

## The problem this exists to solve

A QA tester at Comfy Org is not testing one product. Any of these can land on you
in a given week: Comfy Cloud, billing, custom nodes, API/partner nodes, templates,
the Desktop app on two platforms and two build types, local ComfyUI, and the
agent/MCP surfaces.

Across all of them you work in **six environments** (testcloud, stagingcloud,
stagingplatform, production, per-PR ephemerals, local), record results in **four
different result vocabularies** (Pass/Fail/Blocked, Pass/Fail/Partial/Not Tested,
and two more), and attach an evidence block written in **three mutually
incompatible formats** depending on which playbook you are following.

Almost none of that is judgement. It is setup and record-keeping, it is
deterministic, and it is eating the time that should go to actually finding bugs.

**This tool takes the deterministic half.** Every milestone below removes work that
a machine can do exactly, so the tester's time goes to the parts that need a human:
deciding what is risky, spotting what is degenerate, judging what is actually
broken.

This matters more over time, not less. As releases move to nightly — smaller plans,
2–3 hour turnarounds — a slow setup ritual stops being an annoyance and becomes the
thing that stops you finishing.

---

## Ordering principle

Milestones are ordered by **(time wasted × frequency)** as measured against the QA
onboarding guide and the eight playbooks it links — not by what is neatest to
build. Each milestone ships independently and is useful without the ones after it.

Anything blocked on access outside QA's control is pushed later, deliberately.

---

## v0 — `qa env` · **shipped**

**Which build is each environment actually serving, and what is switched on in it.**

The QA onboarding guide on this check:

> A failed deploy leaves the old version running and looks completely normal.
> This catches more wasted days than anything else in this document.

It appears in **four independent documents** — the sheriff checklist twice (at the
Monday cut, and after every backport), the onboarding guide twice, the current test
plan, and the MCP pack in different words. Release 1.50 carried **11 backports**, so
it re-fires eleven times in a single release.

Does:
- reads `x-frontend-version` from testcloud / stagingcloud / cloud, unauthenticated
- resolves each SHA to date, subject and PR number via `gh`
- reads local ComfyUI's version and frontend from `/system_stats`
- diffs 32 feature flags across environments, reporting only what differs
- warns when the local frontend does not match the cloud release line
- emits the evidence block in all three playbook formats
- `--expect <sha>` exits 1 on mismatch, so it works as a pre-pass gate
- `--json` for scripting

Does not: set flags, touch accounts, run tests, or need a login.

---

## v1 — `qa labels` · next

**Verify that nodes which must be blocked on cloud actually are.**

Nodes labelled `WritesToDisk`, `ReadsArbitraryFile` or `DisabledOnCloud` must be
absent from the Add Node menu or disabled. The onboarding guide: *"if one runs
normally, that's a label-mismatch bug and only QA will catch it."*

Today this is checked **by eye, per node class, per pack, per pass**. It is the one
place QA performs a deterministic set difference manually.

Ground truth is three machine-readable artifacts — `supported_nodes.yaml`,
`cloud_disable_config.yaml`, and the runtime `disabled_nodes.json` — diffed against
the served `/object_info`. Fully automatable, scales with node count rather than
release count.

**Open blocker:** confirming where those three files live and whether they are
fetchable outside a deployed environment.

---

## v2 — `qa report`

**Fill in the mechanical half of a bug report.**

Every report needs: environment · build SHA · flag combination · account type ·
steps · expected · actual · screenshot · console error · suspected PR. Node work
adds two more: does it work locally, and is it a regression.

The first four are machine-known. The exact error text is machine-known — the
Custom Node Playbook insists *"copy-paste the error exactly — don't paraphrase"*.
The credit delta for partner-node tests is machine-known.

Generates the skeleton in the guide's impersonal house style, in whichever of the
three formats the target tracker wants. Judgement stays with the tester.

---

## v3 — `qa nodes`

**Run node test workflows and pre-fill tracker rows.**

For each node class in a pack: load its test workflow, run it, detect degenerate
output (black, empty, NaN). The guide is explicit that pass means *"it completes and
the output isn't degenerate"* — you are not judging whether the image looks good.

Writes one row per node class into the Node QA Tracker, whose schema is fully
specified: Node Class · Pack · Category · Labels · Needs Test · Source File · Test
Workflow · Auto-test Result · Manual-test Result · Issues.

**Pre-fill only.** The playbook is explicit that automated and manual results are
*"independent signals, not sequential gates"* — this fills the auto column and
stages the manual one. It never marks a manual result.

Multiplies by pack size: a single pack can carry 50 test workflows.

---

## v4 — `qa pr <n>`

**Is this PR's ephemeral environment ready?**

Per-PR environments at `pr-N.testenvs.comfy.org` exist only once the PR carries the
`preview-gpu` label, and take minutes to deploy. Today a 404 is ambiguous: still
deploying, or never labelled? Testers refresh and guess.

Reads the label via `gh`, polls until live, and reports which case you are in. Small
build, and it fixes a real recording problem — the answer decides whether a case is
**Fail** or **Not Tested**, which are not interchangeable.

---

## v5 — `qa local`

**Test a PR locally, and prove which build you got.**

comfy-cli can already build and serve a frontend PR locally
(`comfy launch --frontend-pr <n>`) and check out a core PR (`comfy install --pr <n>`),
which means most PRs do not need an ephemeral at all.

What is missing is verification. Today `comfy install --pr` performs **no SHA
comparison** against GitHub's head, its `mergeable` flag defaults to `true` when the
API omits it (the source calls this "optimistic"), and the whole path emits **no
JSON**, so nothing downstream can confirm it worked. You can install a PR build and
have no way to prove you are running it.

This wraps that flow and verifies the result — v0's check pointed at localhost. It
also refuses a genuine trap: `--frontend-pr` injects `--front-end-root`, which takes
precedence over `--front-end-version`, so combining them silently tests the wrong
build.

Cannot replace an ephemeral for cloud-only behaviour: `DisabledOnCloud` labels,
billing, auth and workspaces are meaningless locally.

---

## v6 — `qa flags --set` · **blocked externally**

**Set feature flags without asking an engineer.**

Flags are toggled server-side by engineering. The onboarding guide lists this under
things that will confuse you: *"You can't turn feature flags on yourself. You have
to ask. This is a known gap that's being worked on."*

Self-service access is in progress, intended via Datadog on the QA workspace in
staging and testcloud. **Reading** flag state needs no permission and already
shipped in v0; only setting is blocked.

Designed now, built when the access lands.

---

## v7 — cross-platform testing

**Run the same case across macOS, Windows and Linux.**

Not only about CUDA. The guide requires Desktop tested on your assigned platform,
across **both** the Portable and Desktop builds, with every result tagged for which
— and *"if a bug appears in one version but not the other, note the difference
explicitly."* Platform and build tagging is already in the evidence block from v0.

Three distinct problems usually conflated:
- **tagging** — which OS and build produced this result (v0, done)
- **reaching another machine** — routing commands at a remote host over a tunnel
- **GPU-specific reproduction** — an MPS Mac cannot reproduce CUDA behaviour

Last because it serves the narrowest set of cases, and because driving the Desktop
app's UI on another OS stays hands-on regardless.

---

## Explicitly out of scope

No CLI resolves these. They are named here so they get routed to the right place
rather than silently absorbed.

| | Why | Owner |
|---|---|---|
| **Flagged-payment fixtures** | QA cannot create paused / payment-failed accounts, so payment recovery has been untestable end-to-end for two releases running | Backend |
| **Fresh-email signup per billing scenario** | Real email, auth and Stripe | Backend |
| **Setting staging feature flags** | Server-side flag service | Engineering (in progress) |
| **Deduplicating findings before filing** | Slack search is semantic and server-side | Human |
| **Judging output quality** | The guide is explicit this is not QA's job | Human |

---

## Relationship to `Comfy-Org/comfy-qa`

Different tool, same org, confusingly similar name. `comfy-qa` runs AI-driven
Playwright E2E tests against a PR or issue. This is the setup-and-evidence layer
around testing.

They are complementary and there is a clear integration: a Playwright run currently
carries no record of which build it hit or which flags were on. `qa env --json`
produces exactly that, which would make those test reports reproducible.
