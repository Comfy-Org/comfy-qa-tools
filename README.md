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

**1.1.0. Every command is implemented, tested, and has run against real GCE
hardware on both Linux and Windows** — not against fakes.

The verbs are at the top level: `comfy-qat go linux`. The `host ...` and
`auth ...` spellings were a deprecation window rather than a second permanent way
to type everything, and it closed at 1.1.0 — typing one now exits 2. An old run
sheet that says `host create` or `auth status` wants the noun taken off.

`env` is carried over from v0, hidden, and awaiting its own release.


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

It also asks Google for the GPU quota your project is missing, so a fresh install
ends with every card this tool can drive either granted or with a request in
flight. The exact list is printed before anything is sent — a quota request cannot
be withdrawn and may be read by a person at Google — and `--no-quota-request`
skips it entirely.

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

On a box you have just created, `go` waits for it to finish starting. Google
reports a machine as RUNNING the moment it is powered on, which is some minutes
before it accepts SSH — and on Linux the driver install reboots it once or twice
on top of that. A box that is genuinely ready is not delayed by this; a box that
is still coming up says so while it waits, rather than failing on the tunnel.

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

## Commands

Every target is a **declared host**, local or cloud — naming them all is the
point, so local stops being an invisible default and picking the wrong machine
becomes something you do on purpose.

| command | what it does |
|---|---|
| `comfy-qat setup` | get this machine ready, in one command |
| `comfy-qat status` | signed in? which project? billing and GPU quota? |
| `comfy-qat login` | print the sign-in commands |
| `comfy-qat quota list` | what cards you can start, and where — `--by-region` |
| `comfy-qat quota request` | ask Google for cards — `--gpu l4,a100 --region us-central1` |
| `comfy-qat init` | write a starter host list you can edit |
| `comfy-qat list` | every declared machine and what is up — `--live` asks Google |
| `comfy-qat discover` | find cloud boxes and add them — `--prune` drops the entries Google confirms are gone |
| `comfy-qat create` | make a GPU box; the zone is chosen, not typed |
| `comfy-qat delete` | remove a box and its disk, permanently |
| `comfy-qat go` | start it, make sure ComfyUI is on it, hand the prompt back |
| `comfy-qat up` | start a machine, and succeed only if ComfyUI is already serving |
| `comfy-qat open` | tunnel to a machine already running |
| `comfy-qat disconnect` | close the tunnel, leave the machine running |
| `comfy-qat down` | close the tunnel and stop the machine, so it stops costing |
| `comfy-qat ssh` | a shell on a box, through the tunnel |
| `comfy-qat rdp` | reset the Windows password and forward RDP |
| `comfy-qat logs` | follow the ComfyUI log on a box — `--tail N` reads the end and stops |
| `comfy-qat stamp` | ask a machine what it is, and print the line you paste |
| `comfy-qat switch` | start the one you want, stop the one you were on |
| `comfy-qat move` | move a box to a zone with capacity, keeping its ComfyUI |
| `comfy-qat --version` | what you are running, with the commit |

**[docs/commands.md](docs/commands.md)** has the flags and which to reach for.
`comfy-qat <command> --help` says the same at the prompt.


## The host list

Lives at `~/.config/comfy-qa-tools/hosts.toml`. `comfy-qat setup` writes it and
fills in your cloud boxes automatically — Google already knows each one's zone,
card and operating system, so none of it needs typing. `comfy-qat discover`
does the same on demand, and never changes an entry you already have.

`discover --prune` reconciles the other way, and it is the only thing here that
removes one. Boxes disappear without this tool — deleted from the console, by a
colleague, by raw gcloud — and a stale entry is not untidiness, because it holds
a name and a port that nothing else can use. Removal takes two answers from
Google that agree: the name is nowhere on the project, and then a direct
`describe` of that one machine comes back not-found. Anything else is reported
and kept, including the case that matters most — a box that turns up in a zone
other than the one your entry gives is a wrong entry, not a missing machine, and
deleting it would leave a running GPU with nothing naming it.

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
- `hosts.toml.bak`, and `backups/` — anything that rewrites the host list copies
  it first, reads the copy back, and refuses the rewrite if it could not be made.
  `.bak` is the file as it was before the command you last ran; the copy it
  supersedes is archived under a timestamp in `backups/` rather than overwritten,
  and that directory is capped.
- `zone-latency.json` — how long a TCP connect to each Google Cloud region took
  from this machine, measured by `create` and reused for a week. Delete it to
  re-measure; nothing else reads it.
- `tunnels/<host>.pid`, `tunnels/<host>.json` and `tunnels/<host>.log` — written by
  `open`, `up` and `go`. A tunnel outlives the command that started it, so
  what it is gets recorded rather than assumed: the pid, the moment that process
  started, and which instance, zone and port it goes to. Pids are recycled, so the
  number alone is not an identity and a record that no longer fits is treated as
  stale rather than trusted. The log is what gcloud said while opening it.
  `down` and `disconnect` remove the `.pid` and the `.json` — the two that claim
  a tunnel is there — and keep the `.log`, so you can still read why one died
  after you have closed it. Each `open` starts that log fresh, so it holds the
  most recent tunnel for that host and nothing older.

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
[spot instances](docs/spot-instances.md) ·
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
