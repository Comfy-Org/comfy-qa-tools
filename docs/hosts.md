# The host list

Every machine you test on is declared in `~/.config/comfy-qa-tools/hosts.toml`.
`comfy-qat init` writes a starter one.

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

Most of it is filled in for you. `comfy-qat setup` and `comfy-qat discover`
read your Compute Engine instances and write an entry for each one, assigning a free
local port. Matching is on the GCE instance name, so renaming a host in this file
does not make it come back as a duplicate.

`comfy-qat create` adds its own entry the moment the box exists, on the next free
port, so a machine you just made is usable without a `discover` in between. It
re-reads this file immediately before choosing that port rather than reusing what
it read minutes earlier: creating a box takes minutes, and a `discover` in another
terminal meanwhile would have taken the port it was about to hand out. Two hosts on
one port is the failure you cannot diagnose from the outside.

## The project

One line at the top, above every table, says which Google project comfy-qat works
in:

```toml
project = "your-project-id"
```

It is where `create` makes a new box, what `discover` lists, and what `quota` and
`status` read. `comfy-qat setup --project <id>` writes it, and it is this tool's
own: gcloud's project setting is not read while this line is there, and nothing
here ever changes gcloud's — so other gcloud work on the same machine does not move
comfy-qat, and comfy-qat does not move it. It is the ID, not the project's display
name; `gcloud projects list` shows both.

Without the line, the tool uses gcloud's current project, as it always did, and
each command that does says so:

```
project my-project — gcloud's current project, because your host list names none.
```

**Switching to another project** is that line. Run `comfy-qat setup --project
<new-id>` (add `--no-quota-request` if the new project should not be asked for GPU
quota yet), or change the line by hand. From then on new boxes go to the new
project. **Boxes already in the list stay where they are**: each entry carries its
own `gce_project`, and that is where `up`, `down`, `go` and `delete` reach it, so a
box in the old project stays usable — and stays billing, if it is running or
reserved — until you delete it. There is no move between projects: `move` changes
zone within one. To have a box on the new project, `create` it there and `delete`
the old one. `comfy-qat list` says which project each box is on, and where a new
one will go.

## Fields

| field | applies to | meaning |
|---|---|---|
| `kind` | all | `local` for a ComfyUI on this machine, `gce` for a Google Cloud box |
| `port` | all | where ComfyUI is reached **on your machine**. For `local`, the port it actually serves on. For `gce`, the near end of the tunnel. Defaults to 8188 for `local` |
| `os` | gce | what the box runs, e.g. `Ubuntu 22.04`. Shown in `list`, and matched by `windows`, `linux`, `ubuntu`, `debian` |
| `gpu` | gce | the card, e.g. `L4`, or `none` for a box with no GPU (`cpu` is read as `none`). Matters as much as the OS — not every GPU can run every model. Matched by `l4`, `a100` |
| `gce_instance` | gce | the instance name in Google Cloud |
| `gce_zone` | gce | the zone it lives in, e.g. `us-central1-a` |
| `gce_project` | gce | the project it is billed to |
| `gce_reservation` | gce, optional | the reservation holding this box's capacity, e.g. `comfy-linux-rsv`. Present means the box is **reserved**: it bills every hour, running or stopped, until it is deleted. `create --reserve` and `discover` write it; leave it out for a box that is not reserved |
| `build` | gce, optional | the ComfyUI build a **fresh** install puts on this box, e.g. `"comfyui=v0.43.0 frontend=v1.58.4 manager=on torch=default"`. `create` writes it when it is given a build option; `comfy-qat go` installs it the first time the box has no ComfyUI. Each word is one choice: `comfyui=` a release (`v0.43.0`), `pr:<number>`, `commit:<hash>` or `default` (the tip of ComfyUI's default branch); `frontend=` a release, `pr:<number>` or `default` (the frontend that ComfyUI pins); `manager=on` or `off`; `torch=` a version (`2.12.1+cu130`) or `default`. A line that does not read is refused, never taken for the default. Once the box has ComfyUI, the box keeps its own record of what it runs, and only a build option on `go` or `switch` changes it — see [choosing the build](machines.md#choosing-the-build) |

Anything else is rejected, so a typo like `gce_zoen` fails loudly instead of being
silently ignored and leaving you wondering why the box cannot be found. The
misspelt field is named first, along with what it was probably meant to be —
before the fields it makes look missing, which are not the problem:

```
host 'comfy-linux': unknown field(s) 'gce_zoen' (did you mean 'gce_zone'?).
Known fields: build, gce_instance, gce_project, gce_reservation, gce_zone, gpu, kind, os, port.
```

`os` and `gpu` are not only labels. They are what you can select a host by:
`comfy-qat go windows`, `go l4`, `go windows/l4`. A description is
used only when exactly one declared host fits it, and the host it picked is
printed. See [the everyday loop](machines.md) for the full table.

## The rules

All of them are checked when the file is read, before anything reaches the
network, and all of them are refused outright rather than warned about. Every one
is the same rule seen from a different side: **one entry, one machine, one way to
reach it.** A host list that breaks any of them still loads, still lists and still
stamps — and then a test matrix records a result against a machine that did not
produce it.

**No cloud host may use port 8188.** That is ComfyUI's default and your local
install already holds it. A tunnel on 8188 would point you at a remote machine while
everything on screen looked local — this is the specific mistake the tool exists to
prevent.

**No two hosts may share a port.** If two do, you cannot tell which one you reached,
which defeats the purpose of naming them.

**No two hosts may be the same cloud box.** Two entries naming one
project/zone/instance are two ports and two tunnels onto one machine, so
"reproduced on comfy-win, not on comfy-win-b" says nothing at all. The same
instance name in a *different* zone is a different box and is fine — that is what
`move` leaves behind.

**No two hosts may differ only in case.** `comfy-win` and `Comfy-Win` is one
machine typed two ways far more often than it is two machines, and lookup already
falls back to a case-insensitive match, so with both declared which one you get
depends on a shift key.

**`local` is reserved for the machine you are sitting at.** A `kind = "gce"` host
may not take the name. `stamp local` has one obvious meaning and every
example relies on it.

**A `local` host may not carry `gce_instance`, `gce_zone`, `gce_project` or
`gce_reservation`.**
This is the rule that costs money when it is missing: `down` decides what to
stop from `kind`, so a cloud box declared `local` reads as a successful `down`
while the GPU keeps billing. `os` and `gpu` are not cloud fields — a local host is
welcome to declare both.

**A host name has to be typeable.** It starts with a letter or a digit and holds
only letters, digits, dots, dashes and underscores. TOML would accept `""`,
`"   "`, `"-f"` or `"../evil"` as table keys; a name is an argument you type and
part of a filename, and none of those is either.

## Changing OS means changing host

There is one box per OS, and you pick the one you want. Nothing is ever reimaged:
swapping a running box's OS would destroy its ComfyUI install and take tens of
minutes. Keeping one box per OS and stopping the idle one costs only disk.

```sh
comfy-qat switch windows
```

Name them after whatever actually distinguishes them — `win-l4`, `linux-a100` —
rather than by OS alone, which stops working the day you have two Windows boxes with
different cards. When that day comes, `windows` alone is refused with both boxes
named, and `windows/l4` says which one you meant.
