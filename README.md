# comfy-qa-tools

**QA tooling for setting up and running testing across Comfy.**

QA here covers everything Comfy — core, the frontend, Desktop, the MCP server,
templates, custom nodes, partner nodes — on a local Mac, cloud GPU boxes on more
than one OS, and several deployed environments. Most of the effort is not the test
itself. It is getting a machine into a state where a test means something, pointing
at the right one, and keeping a record of what was tested where. This tool is that
layer: the setup, the environments and the record-keeping around the QA role.

Features land one at a time, and each is documented here when it ships, not before.
Release 1 happens to be `host` — naming the machines you test on and reaching the
one you meant.

---

## Not the same tool as `comfy-qa`

There is a separate tool at
[`Comfy-Org/Comfy-QA`](https://github.com/Comfy-Org/Comfy-QA), maintained by
snomiao, which does AI-driven E2E test runs. The similar names are a coincidence —
they are two different projects, and neither replaces the other. This one's binary
is **`comfy-qat`**, so the two never collide on `PATH`.

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

The `auth` drawer is being added now: `auth status`, `auth login`, `auth quota list`
and `auth quota request` — sign-in state, billing, and GPU quota. It lands first
because `host create` gates on quota. Then `up`, `down`, `open`, `stamp` and
`create` for cloud hosts.

### Carried over from v0

`comfy-qat env` still reports which build each deployed environment serves and its
feature-flag state. It is not part of release 1 and will be rewritten when its own
release comes round — it stays reachable meanwhile rather than disappearing.

---

## First run

Four steps. `comfy-qat guide` prints these in the terminal at any time.

```sh
gcloud auth login          # sign in to Google Cloud
comfy-qat auth status      # check you are ready — fix what it names, then repeat
comfy-qat host init        # write a starter host list
comfy-qat host list        # see your machines
```

`auth status` checks gcloud, your account, your project, billing and GPU quota **in
order, stopping at the first problem**, and prints the command that fixes it. Fix
that one thing and run it again. It is the setup guide, so it cannot go stale the
way a README can.

Full docs in [`docs/`](docs/): [getting started](docs/getting-started.md) ·
[the host list](docs/hosts.md) · [troubleshooting](docs/troubleshooting.md) ·
[cost](docs/cost.md).

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
