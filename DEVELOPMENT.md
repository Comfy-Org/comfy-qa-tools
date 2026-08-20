# Development plan

**Status: draft, up for review.** This is how the roadmap would actually get built.
Decisions that need someone else's input are marked **[NEEDS DECISION]**; things I
have not been able to confirm are marked **[UNCONFIRMED]** rather than assumed.

---

## 1. How this attaches to comfy-cli

comfy-cli has **no plugin system**. Commands are wired with `add_typer` in
`cmdline.py` (lines ~2089-2143), with one registration-function precedent:
`generate_command.register_with(app)`. There is no entry-point discovery anywhere
in the package.

That leaves three stages, in order of increasing legitimacy. The command code is
**identical** at every stage — only the attachment changes.

### Stage 1 — standalone binary *(where we are)*

`pyproject.toml` declares `comfy-qa-cli = "comfy_qa.commands:main"`. Installs into
the comfy-cli virtualenv and runs as its own command.

Works today, zero risk, survives comfy-cli upgrades. Costs: it is not `comfy qa`,
and it is invisible to `comfy discover`, which is what comfy-mcp and the in-app
agent read to learn the CLI.

### Stage 2 — gated `.pth` bootstrap

Ship a `.pth` file that registers the sub-app at interpreter start, gated hard so it
only fires for the comfy binary:

```python
# comfy_qa/bootstrap.py — invoked by a one-line .pth
import os, sys
if os.path.basename(sys.argv[0] or "") in {"comfy", "comfy-cli", "comfycli"}:
    from comfy_cli.cmdline import app
    from comfy_qa.commands import register
    register(app)
```

Gives real `comfy qa`, appears in `--help`, `--help-json` and `discover`.

The gate is not optional. Without it the `.pth` fires on **every** Python start in
that venv — including ComfyUI's own `main.py` — and drags comfy-cli's import graph
with it. `bootstrap.py` must stay free of top-level heavy imports.

**Risk:** a side channel, not a supported interface. It fails silently if the gate
breaks. Mitigated by the smoke test in §4.

### Stage 3 — upstream entry-point group *(the real fix)*

~10 lines in comfy-cli, mirroring the convention it already uses:

```python
def _load_plugins(app: typer.Typer) -> None:
    from importlib.metadata import entry_points
    for ep in entry_points(group="comfy_cli.commands"):
        try:
            ep.load()(app)          # same signature as generate.register_with
        except Exception as e:      # a broken plugin must never break the CLI
            logging.debug(f"plugin {ep.name} failed to load: {e}")
```

Then we uncomment four lines of `pyproject.toml` and delete the `.pth`.

**[NEEDS DECISION]** Is upstreaming this acceptable to the comfy-cli maintainers?
It affects everyone, not just QA. Worth raising before Stage 2 is built, because if
Stage 3 is welcome we may skip Stage 2 entirely.

**Worth including in that PR:** a plugin currently cannot contribute its output
schemas, error codes, or capability flags. `discovery.COMMAND_SCHEMAS` is a static
dict, the 139-entry error-code list is static, and `capabilities` is hardcoded. So a
plugin command appears in `comfy discover` with `output_schema: null` and
undocumented errors. Fixable in the same change.

---

## 2. Code layout

```
comfy_qa/
├── commands.py       # Typer surface + register(parent). One file per milestone's CLI.
├── env.py            # v0: environment probing
├── render.py         # output shapes — table, JSON, the three evidence formats
├── labels.py         # v1
├── report.py         # v2
├── nodes.py          # v3
├── ephemeral.py      # v4
├── local.py          # v5
└── flags.py          # v6
```

Rules that keep this from rotting:

- **Probing is separate from rendering.** `env.py` returns dataclasses; `render.py`
  decides how they look. Every milestone follows this, so `--json` is never an
  afterthought and the three evidence formats stay in one place.
- **Never reimplement a comfy-cli command — delegate to it.** `qa local` wraps
  `comfy launch`, it does not re-derive how to start ComfyUI.
- **Emit `envelope/1`** via `comfy_cli.output.get_renderer()` once attached, so
  `comfy --json qa …` composes with everything else. Stage 1 uses plain JSON.
- **Whitelist by type, never by key name**, wherever an upstream payload is
  involved. See §5.

---

## 3. Per-milestone implementation

### v1 — `qa labels`

Diff three config artifacts against a live `/object_info`:
`supported_nodes.yaml`, `cloud_disable_config.yaml`, and the runtime
`disabled_nodes.json` produced by `resolve_disabled_nodes.py`.

A node whose label says it must be blocked, but which appears enabled in
`/object_info`, is a finding.

**[UNCONFIRMED] Where those three files live**, and whether they are fetchable via
`gh` or only exist inside a deployed environment. This is the one thing blocking v1
and should be settled before anything is written. If they are deployment-only, v1
needs a different data source and probably drops down the order.

**[NEEDS DECISION]** Does the audit run against cloud only? `DisabledOnCloud` is
meaningless locally, so a local run would produce false findings.

### v2 — `qa report`

Compose from what is already machine-known: v0's evidence block, `comfy env --json`,
`comfy outdated --json` (which carries core `installed` and `commit`), and
`comfy system-stats --json`. Prompt only for steps / expected / actual.

Note `comfy which --json` returns just `workspace_path` and `workspace_type`, while
its pretty output shows python, server and CLI version — so the JSON is not a
substitute for the panel.

Interactive parts use **`comfy_cli.ui.prompt_*`, never raw `questionary`** — the
helpers inherit `--skip-prompt` and agent detection (`CLAUDECODE=1`, non-TTY) for
free. Avoid `ui.prompt_multi_select`: it has no skip gate and will hang
non-interactively. Missing required input in non-interactive mode → typed error
envelope and exit 1, never a prompt.

**[NEEDS DECISION]** Does this file to Notion directly, or only emit text to paste?
Filing means a Notion token and write access. Emitting text is safer and matches
how findings already flow through Slack.

### v3 — `qa nodes`

For each node class: fetch its Test Workflow URL from the Node QA Tracker, run it
via `comfy run`, inspect the output for degeneracy (all-black, empty, NaN), write
the Auto-test Result column.

Tracker schema is known: Node Class Name · Node Pack · Category · Labels · Needs
Test · Source File · Test Workflow · Auto-test Result · Manual-test Result · Issues.

**Hard constraint:** automated and manual results are *independent signals, not
sequential gates*. This writes the auto column only. It must never mark a manual
result.

**[NEEDS DECISION]** Miles owns a custom-node CI effort aligned with pinned Cloud
versions. Check whether that supersedes this before building — it may make v3
redundant, in which case v4 and v5 move up.

### v4 — `qa pr <n>`

`gh` reads the PR's labels; if `preview-gpu` is present, poll
`pr-N.testenvs.comfy.org` until it answers. Report three distinct states — not
labelled / deploying / ready — because they map to different result words, and
conflating them is what produces a wrong Fail vs Not Tested.

Smallest build in the set. No unknowns.

### v5 — `qa local`

Wrap `comfy launch --frontend-pr <n>` (which already builds the PR frontend with
pnpm + vite and caches it), then verify what actually loaded using v0's check
pointed at localhost.

Two traps to encode:
- `--frontend-pr` injects `--front-end-root`, which **takes precedence over**
  `--front-end-version`. Combining them silently tests the wrong build. Refuse it.
- `comfy install --pr` performs no SHA comparison against GitHub's head, its
  `mergeable` flag defaults to `true` when the API omits it, and it emits no JSON.
  So the install cannot be trusted without an independent check — which is the
  entire point of this milestone.

**[UNCONFIRMED]** Which fields ComfyUI's `/system_stats` exposes on the builds we
care about, and whether it reports the *served* frontend or only the *required*
one. Needs a live server to settle. If it only reports "required", verification has
to read the served asset hash instead.

### v6 — `qa flags --set`

Blocked on self-service access landing. Reading shipped in v0. Design when the
mechanism is known — likely Datadog on the QA workspace.

### v7 — cross-platform

Three separable pieces, and they should be built separately:
- platform/build tagging in the evidence block — **done in v0**
- routing commands at a remote host — `ssh -L` tunnel plus `COMFY_LOCAL_URL`, which
  comfy-cli already documents as a process-wide address override. Needs no changes
  to comfy-cli.
- GPU-specific reproduction — the narrowest slice

Driving the Desktop app's UI on another OS stays hands-on regardless.

---

## 4. Testing

**The smoke test that matters most:** assert the command appears in
`comfy --json discover`. That single check catches Stage 2's gate silently
breaking, which is otherwise invisible.

Beyond that:

- **Unit tests on rendering**, from recorded fixtures. The three evidence formats
  and the flag-diff logic are pure functions of a payload — no network needed.
- **Contract tests against live environments**, run manually not in CI. They depend
  on real deploy state, so they will flap by design.
- **A recorded-payload corpus** so tests do not hit the network. Capture
  `/api/features` and a `/system_stats` response, commit them scrubbed.
- **Exit codes are part of the contract** and need explicit tests: 0 on match, 1 on
  mismatch, 2 on bad input. A gate that never fails is worse than no gate.

**[NEEDS DECISION]** CI or not? Contract tests against live environments cannot run
unattended without flapping. Suggest: unit tests in CI, contract tests as a manual
make target.

---

## 5. Safety rules

These are not style preferences; each one prevents a specific failure.

1. **Whitelist by type, never by key name.** `/api/features` carries Firebase,
   PostHog, Mixpanel, Sentry and Churnkey config alongside the flags. Filtering on
   `isinstance(v, bool)` means a new secret added upstream cannot leak. A key-name
   denylist would need updating every time upstream adds a field, and would fail
   silently when nobody did.
2. **Never print a payload wholesale.** Evidence blocks get pasted into Slack.
3. **Read-only by default.** Anything that mutates a remote environment needs an
   explicit flag and should probably not exist at all.
4. **Never forward credentials to a remote host.** Report whether a token is
   present; do not ship one.
5. **Fail loudly on ambiguity.** "Not labelled" and "still deploying" are different
   answers and must never collapse into one.

---

## 6. Release process

Semantic versioning, one milestone per minor release: v0 is `0.1.0`, v1 is `0.2.0`.
Each milestone is independently shippable and independently useful — that is the
whole ordering principle, and it means a milestone that turns out not to earn its
keep can simply stop.

Distribution: `pip install git+https://github.com/Comfy-Org/comfy-qa-cli`. No PyPI
publish until it is used by more than one person.

**[NEEDS DECISION]** Who else on the QA team should be using this, and when? It is
built around one tester's workflow so far. Marwan, Denys and Chaeeun would each
stress different surfaces, and the earlier that happens the less it ossifies around
one machine.

---

## 7. Kill criteria

Each milestone needs a way to tell early that it is not earning its keep. There is
precedent on this machine for tooling that got built and then thrown away, so this
is not hypothetical.

| Milestone | Stop if |
|---|---|
| v0 | after two release cycles, DevTools is still how the build gets checked |
| v1 | the label config turns out to be deployment-only, or CI covers it |
| v2 | findings still get written by hand in Slack |
| v3 | Miles's custom-node CI supersedes it |
| v4 | ephemerals become reliable enough that nobody checks |
| v5 | the frontend pin becomes automatic in `comfy launch` upstream |

---

## Open questions summary

1. Is upstreaming a plugin entry-point group acceptable to comfy-cli maintainers?
2. Where do `supported_nodes.yaml` / `cloud_disable_config.yaml` /
   `disabled_nodes.json` live, and are they fetchable outside a deployment?
3. Does Miles's custom-node CI effort supersede v3?
4. Should `qa report` file to Notion, or only emit text?
5. CI strategy for tests that depend on live deploy state?
6. Who else on the QA team adopts this, and when?
