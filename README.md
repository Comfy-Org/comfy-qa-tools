# comfy-qa-tools

**QA tooling for setting up and running testing.**

Most testing time is not spent testing. It goes on standing a machine up, getting it
into a state where a result means something, and working out which box or which build
you were actually looking at. This tool is that layer. Less setup, less environment
wrangling, less doubt about what you are pointed at, more time testing.

Features land one at a time and are documented here when they ship — so what is on
this page is what the binary does today.

---

## Not the same tool as `comfy-qa`

[`Comfy-Org/Comfy-QA`](https://github.com/Comfy-Org/Comfy-QA), maintained by snomiao,
does AI-driven E2E test runs. The similar names are a coincidence — two different
projects, neither replacing the other. This one's binary is **`comfy-qat`**, so they
never collide on `PATH`.

---

## Status

**Release 1 is code complete.** Every command below is implemented and tested.

| area | state |
|---|---|
| `setup` | shipped — one-command first run |
| `status`, `login` | shipped |
| `quota list`, `quota request` | shipped |
| `init`, `list`, `discover` | shipped — offline, no cloud call |
| `create` | shipped — name a card, the zone is chosen for you |
| `up`, `open`, `down`, `go` | shipped — start, tunnel, stop |
| `logs` | shipped — read a detached ComfyUI's log on the box |
| `ssh` | shipped — a shell on a box, without the gcloud incantation |
| `delete` | shipped — remove a box and its disk, permanently |
| `disconnect` | shipped — close the tunnel, leave the machine working |
| `rdp` | shipped — Windows password and Remote Desktop forwarding |
| `switch` | shipped — stop the box you were on, go to the one you want |
| `move` | shipped — escape a zone with no GPU capacity |
| `stamp` | shipped — the evidence line |
| `env` | carried over from v0, unchanged, awaiting its own release |

**The verbs are at the top level.** It is `comfy-qat go linux`. The `host ...`
and `auth ...` spellings were a deprecation window rather than a second permanent
way to type everything, and the window closed at 1.1.0: they are removed, and
typing one now exits 2 with `No such command`. An old run sheet that says
`host create` or `auth status` wants the noun taken off.

**What has actually been run against a real Google Cloud project**, by someone
who did not write the tool, on 2026-08-27: phases A–D, G, H and I of
[`docs/test-criteria.md`](docs/test-criteria.md) in full, plus E4, F1, F2, J1–J9
and J12–J14. Not run, and recorded as not run rather than assumed: J10/J11, which
need quota for two GPU boxes at once, and everything covering `create` and
`logs`, both of which landed after that pass.

Four commands — `ssh`, `rdp`, `disconnect` and `delete` — had **no acceptance
criterion at all** until phases S and N were written, so a full pass of that page
could be signed off without exercising the only command here that cannot be
undone. Nothing failed when they were left out, which is why it lasted; a test now
holds every advertised command to a row in the command reference and to a line the
pack actually runs.

Version **1.0.0**. The tests run on Python 3.11, 3.12 and 3.13, on Ubuntu
and macOS, and CI builds the wheel, installs it into a throwaway virtualenv and
runs the binary from outside the checkout — because testing the source tree never
proved the thing people actually install works.

Three rules hold the docs to the code, each enforced by a test: every error the
tool can print has an entry in [troubleshooting](docs/troubleshooting.md), every
command in the binary appears in this README and every command in this README
exists in the binary, and no test file may vanish from the suite unnoticed.

---

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

## First run

One command.

```sh
comfy-qat setup
```

It signs you in to Google Cloud, picks your project, checks billing, sorts out GPU
quota and writes your host list — saying what it is doing, and asking only where the
decision is genuinely yours. If something needs you (no billing account, no project),
it stops and prints the link. Run it again afterwards; it skips what is already done.

It works without prompts too: `--project`, `--region`, `--non-interactive`.

```sh
comfy-qat list            # see your machines
comfy-qat status          # re-check readiness at any time
```

## Everyday use

```sh
comfy-qat go windows       # start it, tunnel in, leave ComfyUI running on the box
comfy-qat logs windows     # what that ComfyUI is saying, whenever you want it
comfy-qat stamp windows    # the line that says what produced your result
comfy-qat down windows     # close the tunnel, stop the box, stop paying
comfy-qat down --all       # stop every cloud box you have declared
```

`windows` there is not a special name — it is the machine described rather than
named. Anywhere a host name goes you can say what you want instead: an operating
system (`windows`, `linux`, `ubuntu`, `debian`, `macos`), a card (`l4`, `a100`),
or both (`windows/l4`). Whatever it resolves to is printed, one host per line:

```
windows -> comfy-win (Windows Server 2022, L4)
```

If two machines fit, it refuses and lists them with their OS and card rather than
picking one. Names always win over a description.

Changing machine is one command, which stops the box you were on:

```sh
comfy-qat switch linux     # start the Linux box, then stop the Windows one
```

Making the box is one command too, and the card is the only real decision:

```sh
comfy-qat create --os linux --gpu t4
```

The machine type follows from the card — an L4 is a G2 with the GPU built in, a
T4 is an N1 with one attached — and **the zone is chosen rather than typed**:
regions this project holds quota in, zones inside them that offer the card and
the machine type, ranked by latency measured from your machine, and tried in
order until one has capacity. Quota is checked before anything exists, both the
card's own grant and `GPUS_ALL_REGIONS`, the project-wide ceiling that is the one
that usually bites. `--dry-run` prints the plan, the quota it read and the zone
order it would try, and creates nothing. The finished box is added to your host
list on a free port, so `go` works on it straight away.

**The NVIDIA driver is not in either base image**, and a box without one runs
ComfyUI on its CPU while looking perfectly healthy. Linux boxes install it from a
startup script on first boot. Windows boxes do not: Google documents exactly one
way to do it there and it is a person at a PowerShell prompt, so `create` prints
those commands rather than guessing at a recipe it cannot test.

`go` is the one command worth memorising. It starts the instance, installs
ComfyUI if the box has none — with a torch built for that box's CUDA — launches
it **on the box**, and forwards a local port to it as soon as it is listening.
The forward is an `ssh -L` over Identity-Aware Proxy: it reaches the box's own
loopback, so nothing is exposed on any interface and no firewall rule is
involved.

**ComfyUI stays running there and you get your prompt back.** It used to be
launched in the foreground with its log streamed onto your terminal, which meant
one machine per terminal and a Ctrl-C that killed ComfyUI. Now `logs <host>`
reads that log whenever you want it, `--follow` brings the old streaming back for
when you are debugging a launch, `--new-window` hands the whole thing to a new
macOS Terminal window, and **two boxes can be up at once from one prompt**. What
has not changed: `go` still does not return until ComfyUI has really answered on
the tunnel, because a box that booted and serves nothing bills exactly like one
that works.

`down` is what stops the billing — and `down --all` stops every declared cloud
box, because the question at the end of a session is never "is comfy-win
stopped", it is "am I still paying for anything".

[`docs/machines.md`](docs/machines.md) covers the whole loop, including what to do
when a zone has no GPUs left.

## Command reference

Every target is a **declared host**, local or cloud. Naming them all is the point:
local stops being an invisible default, so picking the wrong one becomes something
you do on purpose.

`<host>` below is a name, or a description of the machine you want — `windows`,
`l4`, `windows/l4`. A description that fits exactly one declared host is used and
printed; one that fits two is refused with both named.

| command | what it does |
|---|---|
| `comfy-qat --version` | what you are running — `comfy-qat 1.1.0 (0d27bd4)` from a checkout. Paste it with any result |
| `comfy-qat setup` | first run: sign-in, project, billing, quota, host list |
| `comfy-qat status` | signed in? which project? billing? GPU quota? — `--json` too |
| `comfy-qat login` | prints the sign-in commands; gcloud does the signing in |
| `comfy-qat quota list` | one line per card: ready, pending, or never asked for. `--by-region`, `--region`, `--json` |
| `comfy-qat quota request` | ask Google for cards — `--gpu l4,a100 --region us-central1` — then wait |
| `comfy-qat list` | show every declared machine, where it answers, and what is up. `--live` asks Google whether each box is running |
| `comfy-qat create` | make a GPU box: `--os linux --gpu t4`. The zone is chosen, not typed. `--zone`, `--region`, `--name`, `--disk`, `--yes`, `--dry-run` |
| `comfy-qat init` | write a starter host list you can edit |
| `comfy-qat discover` | find cloud boxes on your project and add the missing ones. `--dry-run` |
| `comfy-qat up <host>` | start it and wait until ComfyUI actually answers |
| `comfy-qat open <host>` | tunnel to a box that is already running. `--dry-run` prints the command |
| `comfy-qat down <host>` | close the tunnel and stop the machine. `--all` stops every declared cloud box and takes no name |
| `comfy-qat go <host>` | up + install if needed + launch ComfyUI on the box and hand the prompt back. `--follow` streams its log here instead, `--new-window` opens a macOS Terminal window, `--no-browser`, `--no-install` |
| `comfy-qat logs <host>` | read the ComfyUI log on a box. Follows by default; `--tail N` prints that many lines and stops |
| `comfy-qat ssh <host>` | open a shell on a Linux box through the tunnel. Replaces `gcloud compute ssh <instance> --tunnel-through-iap --zone <zone> --project <project>` |
| `comfy-qat rdp <host>` | reset a Windows box's password, print it, and forward Remote Desktop to `localhost:33389` |
| `comfy-qat disconnect <host>` | close the tunnel and leave the machine running — for when a long job is going on the box and you want the local port back. Was `down --keep-running`, which has been removed |
| `comfy-qat delete <host>` | delete a box and its boot disk permanently. Refuses while it is running, and asks you to type the box's name — a `[y/N]` is answered by reflex, a name is not |
| `comfy-qat switch <host>` | go to that machine and stop the other one. `--keep-others`, `--dry-run` |
| `comfy-qat move <host>` | rebuild the box in a zone that has capacity, keeping its install. Resumes a move that stopped part-way, and reports what an earlier one left billing. `--to`, `--dry-run`, `--yes`, `--clean` |
| `comfy-qat stamp <host>` | ask a machine what it is. `--json` |


Every command takes `--config` to point at a host list somewhere other than the
default.

One thing still runs and is not advertised: `env`, v0's build and feature-flag
check for deployed environments, which belongs to a different tool and would only
confuse a first reader of `--help`. It is hidden, not going away — there is no
shorter spelling of it to point anyone at.

The other two hidden spellings have gone. Every verb above used to be reachable
with a noun in front of it as well, and that noun was a deprecation window rather
than a second permanent way to type everything. The window closed at 1.1.0: those
paths now exit 2 with `No such command`. Drop the noun — the verb is the command.

### The stamp

```
local · local-git · ComfyUI 0.33.0 · darwin · mps (32GB) · torch 2.13.0 · python 3.12.13
```

That line is the point. It is the record nothing else keeps — paste it into a report
and nobody has to ask which machine produced the result. `--json` gives the same
thing using ComfyUI's own field names.

## The host list

Lives at `~/.config/comfy-qa-tools/hosts.toml`. `comfy-qat setup` writes it and
fills in your cloud boxes automatically — Google already knows each one's zone,
card and operating system, so none of it needs typing. `comfy-qat discover`
does the same on demand, and never touches entries you already have.

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

Seven rules are enforced offline, before anything else runs. The two you meet
first:

- **No cloud host may use 8188.** That is the local ComfyUI's port; a tunnel on it
  would silently point you at the wrong machine, so it is refused, not warned about.
- **No two hosts may share a port.** If two do, you cannot tell which one you reached.

The other five — one box per cloud instance, no two names differing only in case,
`local` reserved for this machine, no `gce_` fields on a `local` host, and a name
that is actually typeable — are in [`docs/hosts.md`](docs/hosts.md) with the
reason for each.

Unknown fields are rejected rather than ignored, and the message names the typo and
what it was probably meant to be:

```
host 'comfy-linux': unknown field(s) 'gce_zoen' (did you mean 'gce_zone'?). Known fields: gce_instance, gce_project, gce_zone, gpu, kind, os, port.
```

Switching OS means switching host: one box per OS, and nothing is ever reimaged.
`os` and `gpu` are what `windows`, `l4` and `windows/l4` match on, so with one box
per OS you never have to remember what you called it. Every field is documented in
[`docs/hosts.md`](docs/hosts.md).

## What it writes

Everything lives under `~/.config/comfy-qa-tools/`:

- `hosts.toml` — the host list, and the only file you would ever edit.
- `zone-latency.json` — how long a TCP connect to each Google Cloud region took
  from this machine, measured by `create` and reused for a week. Delete it to
  re-measure; nothing else reads it.
- `tunnels/<host>.pid`, `tunnels/<host>.json` and `tunnels/<host>.log` — written by
  `open`, `up` and `go`. A tunnel outlives the command that started it, so
  what it is gets recorded rather than assumed: the pid, the moment that process
  started, and which instance, zone and port it goes to. Pids are recycled, so the
  number alone is not an identity and a record that no longer fits is treated as
  stale rather than trusted. The log is what gcloud said while opening it. All
  three are removed by `down`.

Signing in is gcloud's job, so credentials live in gcloud's own store
(`~/.config/gcloud/`) and are refreshed by it. **This tool never sees, stores or
prints a credential.**

It does not touch your ComfyUI installs, your `PATH`, your shell config, or any
service on this machine. It will not overwrite a host list you already have, and
running `setup` again is safe — it skips whatever is already done.

On a **cloud box**, `go` does install software: ComfyUI into `C:\ComfyUI`
(Windows) or `/opt/comfyui` (Linux), with a Python 3.12 environment beside it.
That only ever happens on a declared `gce` host, never locally.

## Docs

[getting started](docs/getting-started.md) · [every command](docs/commands.md) ·
[the everyday loop](docs/machines.md) · [the host list](docs/hosts.md) ·
[troubleshooting](docs/troubleshooting.md) · [cost](docs/cost.md) ·
[when your session expires](docs/session-expiry.md) ·
[test criteria](docs/test-criteria.md) ·
[tests that cannot fail](docs/tests-that-cannot-fail.md)

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

The docs are tested too: every error the tool can print has to appear in
[troubleshooting.md](docs/troubleshooting.md), and every command in the CLI has to
appear in this README. Drift between the code and the page is a failing test, not
something to notice six weeks later.
