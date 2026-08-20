# comfy-qa-cli

QA tooling for testing Comfy. **Know what you are testing, and record it.**

Not to be confused with [`Comfy-Org/comfy-qa`](https://github.com/Comfy-Org/comfy-qa),
which runs Playwright E2E tests. This is the setup-and-evidence layer: it tells you
which build an environment is serving and what is switched on in it. The two are
complementary — this one's `--json` output gives a test run its build provenance.

## Why

From the QA onboarding guide, on checking which build an environment actually serves:

> A failed deploy leaves the old version running and looks completely normal.
> This catches more wasted days than anything else in this document.

That check appears in four separate QA documents. This makes it one command.

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

**v0 only.** Roadmap: node label audit, bug-report generation, node test workflows +
tracker rows, ephemeral readiness, local PR builds with verification, cross-platform
testing. Feature-flag *setting* is blocked pending self-service access.
