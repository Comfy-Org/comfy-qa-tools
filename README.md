# comfy-qa-cli

**QA tooling for testing Comfy — take the deterministic half of the job, so testers
spend their time on judgement.**

A tester here covers eight surfaces across six environments, records results in four
different vocabularies, and attaches an evidence block written in three mutually
incompatible formats. Almost none of that is judgement — it is setup and
record-keeping, it is deterministic, and it is eating the time that should go to
finding bugs.

**See [ROADMAP.md](ROADMAP.md) for the full scope.** In short:

| | | |
|---|---|---|
| **v0** | `qa env` | which build each environment serves, and its flag state · **shipped** |
| **v1** | `qa labels` | verify cloud-blocked nodes are actually blocked |
| **v2** | `qa report` | fill in the mechanical half of a bug report |
| **v3** | `qa nodes` | run node test workflows, pre-fill tracker rows |
| **v4** | `qa pr` | is this PR's ephemeral ready, or was it never labelled |
| **v5** | `qa local` | test a PR locally and prove which build you got |
| **v6** | `qa flags --set` | set flags · *blocked on self-service access* |
| **v7** | cross-platform | run the same case across macOS, Windows, Linux |

Ordered by (time wasted × frequency) against the QA onboarding guide and its eight
playbooks — not by what is neatest to build. Each ships independently.

Not to be confused with [`Comfy-Org/comfy-qa`](https://github.com/Comfy-Org/comfy-qa),
which runs Playwright E2E tests. That is a testing *method*; this is the setup and
evidence layer around the whole role. They are complementary — this one's `--json`
output gives a Playwright run the build provenance it currently lacks.

## v0 — why it is first

From the QA onboarding guide, on checking which build an environment actually serves:

> A failed deploy leaves the old version running and looks completely normal.
> This catches more wasted days than anything else in this document.

That check appears in **four independent QA documents**, and release 1.50 carried 11
backports — so it re-fires eleven times in one release. This makes it one command.

## Install

Install into the **same virtualenv your comfy-cli lives in**:

```sh
git clone https://github.com/Comfy-Org/comfy-qa-cli.git
~/ComfyUI/venv/bin/pip install ./comfy-qa-cli
```

`~/ComfyUI/venv/bin` is usually not on `PATH`, so either use the full path or add an
alias:

```sh
alias comfy-qa-cli='~/ComfyUI/venv/bin/comfy-qa-cli'
```

Requires Python 3.10+ and `gh` authenticated (to resolve a build SHA to a commit;
without it you still get the SHA — pass `--no-resolve` to skip).

## Use

```sh
comfy-qa-cli qa env                                  # all environments + local
comfy-qa-cli qa env testcloud                        # one
comfy-qa-cli qa env testcloud --expect 06f10afe5     # exit 1 if serving something else
comfy-qa-cli qa env --evidence testcloud --platform "macOS 15 · Desktop"
comfy-qa-cli qa env --json                           # for scripts
```

```
testcloud       9a54e1f0  2026-08-17  refactor: resolve node display name at read time...
stagingcloud    a274cd6c  2026-08-17  [backport cloud/1.50] fix(billing): open billing portal...
cloud           3c2f9f1c  2026-08-14  1.50.7 (#15186)
local           ComfyUI 0.33.0  frontend 1.49.6

32 flags checked, 1 differ across environments:
  new_free_tier_subscriptions   testcloud=ON  stagingcloud=ON  cloud=OFF

WARNING  local frontend is 1.49.6 but cloud is on the 1.50 line — the local half
         of a 1.50 plan would test the wrong build
```

Evidence block, paste-ready in three formats (`--evidence`):

```
stagingcloud · build a274cd6c · team_workspaces_enabled ON + consolidated_billing_enabled ON · Windows 11 · Portable
```

## Safety

`/api/features` also carries Firebase, PostHog, Mixpanel, Sentry and Churnkey
config. **Only boolean values are ever printed** — a whitelist by type, so a secret
added upstream cannot leak into a pasted evidence block.

## Status

**v0 shipped, verified against live environments.** Not yet wired into comfy-cli as a
true `comfy qa` subcommand — comfy-cli has no plugin system, so that needs either a
startup hook or ~10 lines upstream. The command code is identical either way.

No automated tests yet. See [ROADMAP.md](ROADMAP.md) for what is next and what is
deliberately out of scope.
