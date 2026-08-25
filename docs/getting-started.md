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

## 2. Sign in to Google Cloud

```sh
comfy-qat auth login
```

That prints the two commands to run. It does not run them for you: `gcloud auth
login` opens a browser and needs your input, and running it yourself leaves you the
record of what happened.

This tool never stores a credential. gcloud keeps its own, and refreshes them.

## 3. Check you are ready

```sh
comfy-qat auth status
```

It checks five things in order — gcloud, your account, your project, billing, and
GPU quota — and **stops at the first problem**, printing the command that fixes it.
Fix that one thing, run it again, repeat until every line says `ok`.

Stopping at the first failure is deliberate. Each check depends on the ones before
it, so showing five failures caused by one problem would just be noise.

If it says your GPU quota is zero, see [cost.md](cost.md) and run:

```sh
comfy-qat auth quota list
```

## 4. Declare your machines

```sh
comfy-qat host init
```

That writes a starter list to `~/.config/comfy-qa-tools/hosts.toml` with your local
ComfyUI already in it. Open it and add any cloud boxes you use — see
[hosts.md](hosts.md) for what each field means.

## 5. See what you have

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
