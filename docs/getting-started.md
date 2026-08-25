# Getting started

From nothing to a working setup. You do not need to know what a tunnel is.

## 1. Install

```sh
git clone https://github.com/Comfy-Org/comfy-qa-tools.git
uv tool install ./comfy-qa-tools
```

`uv tool install` keeps it out of every ComfyUI virtualenv on purpose. This tool's
job is inspecting machines, so it must not depend on any one machine's install.

Needs Python 3.11 or newer.

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

`setup` has already written a starter host list to
`~/.config/comfy-qa-tools/hosts.toml`, with your local ComfyUI in it. Open it and
add any cloud boxes you use — [hosts.md](hosts.md) explains every field.

```sh
comfy-qat host list
```

```
NAME         KIND   OS            GPU  URL
local        local  -             -    http://127.0.0.1:8188
comfy-linux  gce    Ubuntu 22.04  L4   http://127.0.0.1:8190
```

That is the setup done. Every machine you test on now has a name, and there is no
invisible default — which is the whole point, because both a local ComfyUI and a
cloud box will happily answer on the same port and look identical.

## When something goes wrong

[troubleshooting.md](troubleshooting.md) lists every error this tool can print,
what causes it, and how to fix it.

## 4. Stamp what you tested

```sh
comfy-qat host stamp local
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
