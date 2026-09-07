# Getting started

From nothing to a GPU box you are testing on. Three commands do it — `setup`,
`create`, `go` — and **you never open the Google Cloud console**. You do not need
to know what a tunnel is, what machine type an L4 needs, or which zone has
capacity today; those are the parts this tool decides for you.

## 1. Install

```sh
git clone https://github.com/Comfy-Org/comfy-qa-tools.git
cd comfy-qa-tools
/path/to/venv/bin/pip install -e .
```

Any virtualenv with Python 3.11 or newer will do. `-e` means it runs from the
checkout, so `git pull` updates it.

The binary lands at `/path/to/venv/bin/comfy-qat`. Virtualenv `bin` directories are
usually not on your `PATH`, so either call it by full path or add an alias:

```sh
alias comfy-qat='/path/to/venv/bin/comfy-qat'
```

`uv tool install .` works too and keeps it out of every virtualenv — but `uv` has to
be on your `PATH`, and it installs binaries to `~/.local/bin`, which is not on
`PATH` by default on macOS. Run `uv tool update-shell` if `comfy-qat` is not found.

If you installed an earlier version as `comfy-qa-cli` or `comfy-qa`, remove it
first: `pip uninstall -y comfy-qa-cli comfy-qa`.

## 2. Run setup

```sh
comfy-qat setup
```

That is the whole thing. It signs you in to Google Cloud, picks your project,
checks billing is linked, sorts out GPU quota, and writes your host list — telling
you what it is doing at each step and asking only where the decision is genuinely
yours.

A browser opens for the Google sign-in. That part has to be you: this tool never
stores a credential, and never sees one. gcloud keeps its own and refreshes them.

If something is not fixable from here — no billing account, no project on the
account — setup stops and prints the link that fixes it. Fix it and run `setup`
again; it picks up where it left off and skips what is already done.

### If you would rather not be prompted

Every part of setup works without questions:

```sh
comfy-qat setup --project my-project --region us-central1 --non-interactive
```

In that mode it never opens a browser: if you are not signed in, it stops and tells
you the command to run. A prompt-only feature is an incomplete one.

## 3. Check what you have

`setup` has already written your host list to
`~/.config/comfy-qa-tools/hosts.toml` — your local ComfyUI, plus every cloud box on
your project. Nothing needed typing: Google knows each box's zone, card and
operating system already.

```sh
comfy-qat list
```

```
NAME         KIND   OS            GPU  URL                    STATE
local        local  -             -    http://127.0.0.1:8188  -
comfy-linux  gce    Ubuntu 22.04  L4   http://127.0.0.1:8190  not tunnelled
```

Every machine you test on now has a name, and there is no invisible default —
which is the whole point, because both a local ComfyUI and a cloud box will
happily answer on the same port and look identical.

Run `comfy-qat discover` any time a box appears on the project that this list does
not know about. It only adds what is missing and never touches what you have
edited. [hosts.md](hosts.md) explains every field.

If you are reading notes written before the verbs moved to the top level, you will
see `comfy-qat host list`, `comfy-qat auth status` and the like. Those still work,
so nothing you have written down breaks — but they are a deprecation window rather
than a second way to type everything, and the short form is the one to learn.
[commands.md](commands.md) lists both.

## 4. Make a box

If that list has no cloud machine on it — a fresh project usually does not — make
one. You do not go to the console for this:

```sh
comfy-qat create --os linux --gpu t4
```

**The card is the only real decision.** The machine type follows from it (an L4 is
a G2 with the GPU built into the machine type; a T4 is an N1 with one attached,
and getting that the wrong way round is the commonest way a create by hand fails),
and the zone is chosen for you: regions your project holds quota in, zones inside
them that offer the card, ranked by latency measured from where you are sitting,
and tried in order until one has capacity. Quota is checked *before* anything
exists, because a refusal costs nothing and a quota failure after the instance
exists costs money and a cleanup.

Run it with `--dry-run` first if you want to see the plan, the quota it read and
the zone order it would try, without creating anything.

One thing it cannot do for you. **The NVIDIA driver is not in either base image**,
and a box without one runs ComfyUI on its CPU while looking perfectly healthy. On
Linux, `create` installs it from a startup script on first boot — which reboots the
box once or twice, and `go` waits that out. On Windows it does not: Google
documents exactly one way to install it there and it is a person at a PowerShell
prompt, so `create` prints those commands for you to run once rather than inventing
a recipe nobody has tested.

The finished box is added to your host list on a free port, so the next step works
on it straight away.

## 5. Go to it

```sh
comfy-qat go comfy-linux
comfy-qat go linux          # the same box, described rather than named
```

That starts the machine, installs ComfyUI if it has none, launches it **on the
box**, forwards a local port to it, opens your browser — and gives you your prompt
back. ComfyUI keeps running there, so you can bring a second machine up from the
same terminal.

```sh
comfy-qat logs linux        # what it is saying, whenever you want to know
comfy-qat down linux        # close the tunnel, stop the box, stop paying
```

**`down` is the one to remember.** A GPU box bills for every hour it is on,
whether or not anything is pointed at it, and nothing on your screen tells you it
is still there. `comfy-qat down --all` stops every cloud box you have declared, for
the end of a session when the question is "am I still paying for anything".
[cost.md](cost.md) has the rest.

## When something goes wrong

[troubleshooting.md](troubleshooting.md) lists every error this tool can print,
what causes it, and how to fix it.

## 6. Stamp what you tested

```sh
comfy-qat stamp local
```

```
local · local-git · ComfyUI 0.33.0 · darwin · mps (32GB) · torch 2.13.0 · python 3.12.13
```

That is the line you paste into a bug report. It is the record almost nothing else
keeps: a recorded test does not carry it, and a hand-written report usually does
not either — which is how "cannot reproduce" happens between two machines that were
never the same in the first place.

`--json` gives the same thing machine-readably, using the field names ComfyUI itself
uses, so anything else that reads them can consume it directly.
