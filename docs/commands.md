# Every command

In the order you meet them, not alphabetically — the first four are your first
hour, the middle ones are every day after that, and the last two are the ones you
only need when something has gone sideways.

`<host>` is a machine's name, or a description of the machine you want:
`windows`, `l4`, `windows/l4`. A description that fits exactly one declared host
is used, and the host it picked is printed before anything happens. One that fits
two is refused with both named. Every command takes `--config` to read a host list
somewhere other than `~/.config/comfy-qa-tools/hosts.toml`.

## Before anything

| command | what it does |
|---|---|
| `comfy-qat --version` | the build you are running — `comfy-qat 1.0.0 (841abaa)` from a checkout, no sha from an installed wheel. Paste it with any result |
| `comfy-qat setup` | first run, all of it: sign-in, project, billing, GPU quota, host list. `--project`, `--region`, `--non-interactive` |
| `comfy-qat guide` | the first-run instructions, printed in the terminal |

## Is my account ready?

| command | what it does |
|---|---|
| `comfy-qat status` | signed in? which project? billing linked? any GPU quota? One line per check, stopping at the first failure. `--json` |
| `comfy-qat login` | prints the two `gcloud` sign-in commands for you to run. It never signs you in itself — the browser step has to be you, and running it yourself leaves you the repro trail |
| `comfy-qat quota list` | one line per card: ready, pending, or never asked for. Takes about a minute, and says so first. `--region`, `--by-region`, `--json` |
| `comfy-qat quota request` | ask Google for cards — `--gpu l4,a100 --region us-central1` — then wait for the answer. `--value`, `--justification`, `--no-wait`, `--dry-run` |

Quota gates the **card**, never the operating system. Once a card is approved you
can build either Windows or Linux on it.

## Getting a machine

| command | what it does |
|---|---|
| `comfy-qat create` | make a GPU box: `--os linux --gpu t4`. The card is the only real decision — the machine type follows from it and the zone is chosen, not typed. `--name`, `--zone`, `--region`, `--disk`, `--yes`, `--dry-run` |
| `comfy-qat discover` | find cloud boxes already on your project and add the missing ones. Never touches an entry you have edited. `--dry-run` |
| `comfy-qat init` | write a starter host list you can edit by hand. `--force` |
| `comfy-qat list` | every declared machine: what it is, where it answers, and what is up. `--live` asks Google whether each box is running, one call per box |

`setup` already runs `discover` for you, so on a project that has boxes you rarely
type it. `init` is for the case where you want to write the file yourself.

## The everyday loop

| command | what it does |
|---|---|
| `comfy-qat go <host>` | the one worth memorising: start the box, install ComfyUI if it has none, launch it **on the box**, forward a port, and hand your prompt back. `--follow` streams the log here instead and Ctrl-C then stops ComfyUI; `--new-window` runs that in a new macOS Terminal; `--no-browser`, `--no-install` |
| `comfy-qat logs <host>` | read the ComfyUI log on the box. Follows by default, because "what is it doing now" is the question people have; `--tail N` prints that many lines and stops. Ctrl-C ends the reading and nothing else |
| `comfy-qat stamp <host>` | ask a machine what it is, in one line you paste into a report. `--json` |
| `comfy-qat switch <host>` | go to that machine and stop the other one. The target comes up first, so a failure leaves you the box you were on. `--keep-others`, `--dry-run`, `--no-browser`, `--no-install` |
| `comfy-qat down <host>` | close the tunnel and stop the machine, so it stops costing money. `--all` stops every declared cloud box and takes no name; `--keep-running` closes only the tunnel |

## The pieces of `go`, separately

| command | what it does |
|---|---|
| `comfy-qat up <host>` | start it, tunnel in, and wait until ComfyUI actually answers. No install |
| `comfy-qat open <host>` | tunnel only, to a box already serving. `--dry-run` prints the `gcloud compute ssh ... -L` command instead of running it — the thing to paste into a bug report when the forward itself misbehaved |

`open` on a box where ComfyUI is not running says so rather than appearing to
succeed: there is nothing to forward to. `go` is the command that starts it and
forwards in one step.

## When something has gone wrong

| command | what it does |
|---|---|
| `comfy-qat move <host>` | rebuild the box in a zone that has capacity, keeping its ComfyUI install. Resumes a move that stopped part-way rather than restarting it, and reports what an earlier one left billing. `--to`, `--yes`, `--dry-run`, `--clean` |

Every error this tool can print has an entry in
[troubleshooting.md](troubleshooting.md) — paste the message in and find it.

## The two spellings that no longer advertise themselves

`comfy-qat host ...` and `comfy-qat auth ...` are how these verbs used to be
typed, and they still work:

| old | now |
|---|---|
| `comfy-qat host go linux` | `comfy-qat go linux` |
| `comfy-qat host list`, `up`, `open`, `down`, `logs`, `switch`, `move`, `stamp`, `init`, `discover`, `create` | drop the `host` |
| `comfy-qat auth status`, `auth login` | `comfy-qat status`, `comfy-qat login` |
| `comfy-qat auth quota list`, `auth quota request` | `comfy-qat quota list`, `comfy-qat quota request` |

`host` was a noun in front of every verb, and nothing else in this tool collides
with `go`, `down`, `list` or `stamp` — so it was pure typing, and on a second
operating system it is typing you do twice as often. The old forms are hidden from
`--help` so that page shows one way to do each thing rather than two, and they are
kept working so that a script, a run sheet or muscle memory written down before
today does not break. Treat them as **a deprecation window, not a second permanent
spelling**: 26 command paths is not a simplification of 13.

One thing to know: some of the tool's own messages still print the old spelling —
`create` finishes by suggesting `comfy-qat host go <name>`, for instance. Both
work; the short form is the one to learn.

## `env`, which belongs to a different tool

```sh
comfy-qat env
```

It reports the build and feature-flag state of **deployed Comfy environments** —
which has nothing to do with operating the machines the rest of this page is
about. It is v0's, it was verified against all three cloud environments, and it is
the only way anyone has to check which build an environment is serving, so
deleting it would take a capability away for nothing. It is hidden rather than
removed: it stays reachable and stops confusing someone reading `--help` for the
first time. It gets rewritten when its own release comes round.
