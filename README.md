# comfy-qa-tools

**QA tooling for setting up and running testing.**

Most testing time is not spent testing. It goes on standing a machine up, getting it
into a state where a result means something, and working out which box or which build
you were actually looking at. This tool is that layer. Less setup, less environment
wrangling, less doubt about what you are pointed at, more time testing.

Features land one at a time, documented here when they ship. Release 1 is `host` —
naming the machines you test on and reaching the one you meant.

---

## Not the same tool as `comfy-qa`

[`Comfy-Org/Comfy-QA`](https://github.com/Comfy-Org/Comfy-QA), maintained by snomiao,
does AI-driven E2E test runs. The similar names are a coincidence — two different
projects, neither replacing the other. This one's binary is **`comfy-qat`**, so they
never collide on `PATH`.

---

## Release 1 — `host` *(in progress)*

Reach the machine you mean, on the OS you need, and know exactly what it is.

Every target is a **declared host**, local or cloud. Naming them all is the point:
local stops being an invisible default, so picking the wrong one becomes something you
do on purpose.

### Available now

```sh
comfy-qat host init        # write a starter host list
comfy-qat host list        # show every declared machine
comfy-qat host discover    # find cloud boxes and add them
comfy-qat host stamp local # what is this machine, exactly?
comfy-qat host            # same as list — read-only is the safe default
```

```
local · local-git · ComfyUI 0.33.0 · darwin · mps (32GB) · torch 2.13.0 · python 3.12.13
```

That line is the point. It is the record nothing else keeps — paste it into a report
and nobody has to ask which machine produced the result. `--json` gives the same
thing using ComfyUI's own field names.

```
NAME         KIND   OS            GPU  URL
local        local  -             -    http://127.0.0.1:8188
comfy-linux  gce    Ubuntu 22.04  L4   http://127.0.0.1:8190
```

### The host list

Lives at `~/.config/comfy-qa-tools/hosts.toml`. `comfy-qat setup` writes it and
fills in your cloud boxes automatically — Google already knows each one's zone,
card and operating system, so none of it needs typing. `comfy-qat host discover`
does the same on demand, and never touches entries you already have.

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

Two rules are enforced offline, before anything else runs:

- **No cloud host may use 8188.** That is the local ComfyUI's port; a tunnel on it
  would silently point you at the wrong machine, so it is refused, not warned about.
- **No two hosts may share a port.** If two do, you cannot tell which one you reached.

Unknown fields are rejected rather than ignored, so a typo'd `gce_zoen` fails loudly.

Switching OS means switching host: one box per OS, selected by name. Nothing is
reimaged.

### Still to come in release 1

The `auth` drawer is next: `auth status`, `auth login`, `auth quota list` and
`auth quota request` — sign-in state, billing, and GPU quota. It lands first because
`host create` gates on quota. Then `up`, `down`, `open`, `stamp` and `create` for
cloud hosts.

### Carried over from v0

`comfy-qat env` still reports which build each deployed environment serves and its
feature-flag state. It is not part of release 1 and stays reachable until its own
release comes round.

---

## First run

One command.

```sh
comfy-qat setup
```

It signs you in to Google Cloud, picks your project, checks billing, sorts out GPU
quota and writes your host list — saying what it is doing, and asking only where the
decision is genuinely yours. If something needs you (no billing account, no project),
it stops and prints the link. Run it again afterwards; it skips what is already done.

```sh
comfy-qat host list       # see your machines
comfy-qat auth status     # re-check readiness at any time
comfy-qat guide           # the short version, in the terminal
```

Setup works without prompts too — `--project`, `--region`, `--non-interactive`.

Full docs in [`docs/`](docs/): [getting started](docs/getting-started.md) ·
[the host list](docs/hosts.md) · [troubleshooting](docs/troubleshooting.md) ·
[cost](docs/cost.md).

## Install

Needs Python 3.11 or newer.

```sh
git clone https://github.com/Comfy-Org/comfy-qa-tools.git
cd comfy-qa-tools
```

**Into an existing virtualenv** — the fewest moving parts, and it works with what
you already have:

```sh
/path/to/venv/bin/pip install -e .
```

`-e` runs it from the checkout, so `git pull` updates it with no reinstall. The
binary lands next to that venv's other scripts, at `/path/to/venv/bin/comfy-qat`.
If that directory is not on your `PATH` — venv `bin` directories usually are not —
either call it by full path or add an alias:

```sh
alias comfy-qat='/path/to/venv/bin/comfy-qat'
```

**Or standalone with uv**, which keeps it out of every virtualenv:

```sh
uv tool install .
```

Two things to check if `comfy-qat` is then "not found": `uv` itself has to be on
your `PATH` (it is often installed inside a virtualenv rather than system-wide), and
`uv` puts binaries in `~/.local/bin`, which is not on `PATH` by default on macOS.
`uv tool update-shell` fixes the second.

### Upgrading from an earlier install

Earlier versions installed as `comfy-qa-cli` or `comfy-qa`. Remove them — the
second in particular installs a `comfy-qa` binary, which is
[a different project's command](https://github.com/Comfy-Org/Comfy-QA):

```sh
pip uninstall -y comfy-qa-cli comfy-qa
```

Then update any shell alias to point at `comfy-qat`.

## What it writes

One file: `~/.config/comfy-qa-tools/hosts.toml`. That is the whole footprint.

Signing in is gcloud's job, so credentials live in gcloud's own store
(`~/.config/gcloud/`) and are refreshed by it. **This tool never sees, stores or
prints a credential.**

It does not touch your ComfyUI installs, your `PATH`, your shell config, or any
service. It will not overwrite a host list you already have, and running `setup`
again is safe — it skips whatever is already done.

## Design

Each feature is a sibling sub-app, added in one line. `comfy-qat <feature> <action>`;
a new feature never touches an existing one.

comfy-cli has **no plugin mechanism** — no entry-points table, all sub-apps registered
by static `add_typer` calls — so this ships as its own binary. `register()` in
`comfy_qa/cli.py` is the object a plugin entry point would take if comfy-cli ever
grows one; the reservation is already in `pyproject.toml`. Nothing here depends on
that happening.

## Tests

```sh
pytest tests/
```
