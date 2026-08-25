# comfy-qa-tools

**QA tooling for testing Comfy: know which machine you are testing, and stamp every
result with it.**

A tester here works across a local Mac, cloud GPU boxes on more than one OS, and
several deployed environments. Nothing in that picture says out loud which machine
answered a request — and both this Mac and the GCE box serve ComfyUI on the same
port. Reaching the wrong one has already happened. This tool makes the machine
explicit, and puts it in the record.

Features land one at a time. Each is described here when it ships, not before.

---

## This is not `comfy-qa`

There is a separate first-party tool at
[`Comfy-Org/Comfy-QA`](https://github.com/Comfy-Org/Comfy-QA), maintained by
snomiao, which publishes the npm binary **`comfy-qa`**. It is E2E QA automation:
you give it a PR or issue URL and it drives Playwright, records video and writes a
structured report.

**They are different tools and the names are easy to confuse, so:**

| | `comfy-qa` (snomiao) | `comfy-qa-tools` (this) |
|---|---|---|
| Question it answers | does this PR behave correctly? | which machine and build produced this result? |
| How | AI-driven Playwright runs, video, reports | reads and operates hosts; emits a provenance stamp |
| Runs | `npx comfy-qa <pr-url>` | `comfy-qat host ...` |
| Repo | `Comfy-Org/Comfy-QA` | `Comfy-Org/comfy-qa-tools` |

This tool's binary is **`comfy-qat`**, deliberately distinct so the two never
collide on `PATH`.

They are complementary, not competing. Neither `comfy-qa` nor
[`comfy-test`](https://github.com/Comfy-Org/ComfyUI_frontend) records which build a
test ran against — `comfy-test` reads `cloud_version`, `comfyui_version` and
`deploy_environment` and then discards all three. That record is what this tool
produces.

---

## Release 1 — `host` *(in progress)*

Reach the machine you mean, on the OS you need, and know exactly what it is.

Every target is a **declared host**, local or cloud. Naming them all is the point:
local stops being an invisible default, so picking the wrong one becomes something
you have to do on purpose rather than something that happens to you.

### Available now

```sh
comfy-qat host init      # write a starter host list
comfy-qat host list      # show every declared machine
comfy-qat host           # same as list — read-only is the safe default
```

```
NAME         KIND   OS            GPU  URL
local        local  -             -    http://127.0.0.1:8188
comfy-linux  gce    Ubuntu 22.04  L4   http://127.0.0.1:8190
```

### The host list

`~/.config/comfy-qa/hosts.toml`:

```toml
[hosts.local]
kind = "local"
port = 8188

[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-a"
gce_project  = "your-project-id"
port         = 8190
```

Two rules are enforced before anything else runs, both offline:

- **No cloud host may use 8188.** That is the local ComfyUI's port. A tunnel on it
  would silently point you at the wrong machine — the exact failure this tool
  exists to prevent — so it is refused, not warned about.
- **No two hosts may share a port.** If two do, you cannot tell which one you
  reached.

Unknown fields are rejected rather than ignored, so a typo'd `gce_zoen` fails loudly
instead of being quietly dropped.

Switching OS means switching host: one box per OS, selected by name. Nothing is
reimaged.

### Still to come in release 1

`up`, `down`, `open`, `stamp` and `create` for cloud hosts, and the `auth` drawer —
sign-in state, billing, and GPU quota requests. `host create` gates on quota, so
`auth` lands first.

### Carried over from v0

`comfy-qat env` still reports which build each deployed environment serves and its
feature-flag state. It is not part of release 1 and will be rewritten when its own
release comes round — it stays reachable meanwhile rather than disappearing.

---

## Install

```sh
git clone https://github.com/Comfy-Org/comfy-qa-tools.git
uv tool install ./comfy-qa-tools
```

`uv tool install` keeps it outside every ComfyUI virtualenv, which matters: this
tool's job is interrogating machines, so it must not depend on any one machine's
install. Requires Python 3.11+.

## Design

Each feature is a sibling sub-app, added in one line. `comfy-qat <feature> <action>`;
a new feature never touches an existing one.

comfy-cli has **no plugin mechanism** — no entry-points table, all sub-apps
registered by static `add_typer` calls — so this ships as its own binary.
`register()` in `comfy_qa/cli.py` is the object a plugin entry point would take if
comfy-cli ever grows one; the reservation is already in `pyproject.toml`. Nothing
here depends on that happening.

## Tests

```sh
pytest tests/
```
