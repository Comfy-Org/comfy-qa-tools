# Every command

In the order you meet them, not alphabetically — the first four are your first
hour, the middle ones are every day after that, and the last ones are for when
something has gone sideways or a box has outlived its use.

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
| `comfy-qat down <host>` | close the tunnel and stop the machine, so it stops costing money. `--all` stops every declared cloud box and takes no name |
| `comfy-qat disconnect <host>` | close the tunnel and **leave the machine running** — for a long generation you want to keep, or a laptop you are closing. It says the machine keeps billing, and how to stop it. This was `down --keep-running`, which still works and says so. `--os`, `--gpu` |

## Onto the box itself

| command | what it does |
|---|---|
| `comfy-qat ssh <host>` | a shell on a Linux box. The long form is `gcloud compute ssh <instance> --tunnel-through-iap --zone <zone> --project <project>` and this tool already knows the last three. It replaces this terminal; `exit` brings you back. `--os`, `--gpu` |
| `comfy-qat rdp <host>` | a Windows box: resets the password, prints user, password and `localhost:33389`, **then** forwards RDP. Google documents no way around the password reset, so this does the parts it can and leaves you the one thing only a person can do. Ctrl-C closes the forward. `--os`, `--gpu` |

`ssh` at a Windows box points you at `rdp`, and `rdp` at a Linux box points you at
`ssh`, rather than failing obscurely. Neither needs a `comfy-qat open` first —
they go through Google's IAP, not through this tool's forwarded port.

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
| `comfy-qat delete <host>` | **permanently** remove a box, its boot disk and the ComfyUI on it, and take its entry out of your host list. Takes an exact name — never a description — refuses a box that is not stopped, and asks you to type the name back. `--yes` |

`delete` is the only thing here that cannot be undone. A stopped box costs only
its disk, so the everyday answer is `down`, not this; `delete` is for a box you
are finished with. The boot disk is removed with the instance deliberately: these
disks are created `auto-delete=no`, so an instance deleted on its own leaves
200-300 GB billing with nothing attached to it, which looks like nothing at all in
a console. Leaving the host list entry behind is not tidy either — `create`
refuses a name an entry holds and ports come from the same list, so the entry
would reserve both for a machine that no longer exists.

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

One thing to know: a few of the tool's own messages still print the old spelling.
At the time of writing there are three, all in the sign-in and quota paths, all
saying `comfy-qat auth ...`. Both spellings work; the short form is the one to
learn, and `A8` in [test-criteria.md](test-criteria.md) is the check that counts
them.

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
