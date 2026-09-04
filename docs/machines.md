# The everyday loop

You have a host list. This is what you do with it, from "I need a Windows box with
an L4" to "I am done and not paying for it any more".

## Making the box: `create`

Before there is a loop, there is a machine. One command, and the card is the only
real decision:

```sh
comfy-qat create --os linux --gpu t4
comfy-qat create --os windows --gpu l4
comfy-qat create --os linux --gpu t4 --name box-2 --disk 500
comfy-qat create --os linux --gpu t4 --dry-run
```

**You do not type a machine type.** It follows from the card, and this is the
thing most often got wrong by hand: an L4 is the G2 family with the GPU built
*into* the machine type — passing `--accelerator` alongside one is refused — while
a T4, P4, P100, V100 or K80 is an N1 with a card attached to it.

**You do not type a zone either.** It is chosen, in this order:

1. regions this project holds quota for that card in — quota is granted per
   region, and ranking on distance alone picks somewhere nothing can start;
2. zones inside them that offer both the card and the machine type;
3. ranked by latency **measured** from your machine, cached for a week beside
   your host list;
4. tried in order, moving on when a zone says it has none free.

`--zone us-central1-a` overrides all of that, for deliberately testing one zone —
it is then that zone or nothing, with no fall-through. `--region europe-west4`
narrows without naming a zone.

Quota is checked before anything exists, both the card's grant and
`GPUS_ALL_REGIONS` — the project-wide ceiling across every card, which is the one
that usually bites, and which a box you already have running is spending. A
refusal at that point costs nothing; a quota failure after the instance exists
costs money and a cleanup.

```
quota checked:
  L4: 1, in 43 region(s)
  GPUS_ALL_REGIONS (every card, project-wide): 1

  - create comfy-linux in europe-west4-a: Ubuntu 22.04, L4 (nvidia-l4)
  - machine type g2-standard-8 — built into the machine type
  - 200 GB pd-balanced boot disk from ubuntu-2204-lts
  - startup script installs the NVIDIA driver on first boot
  - add comfy-linux to the host list on the next free port

zone order — 4 to try, quota first, then what is offered, then measured latency (nearest: europe-west4)
  1. europe-west4-a  (208 ms to europe-west4)
  2. europe-west4-b  (208 ms to europe-west4)
  3. us-central1-a   (311 ms to us-central1)
```

`--dry-run` prints exactly that — the plan, the quota it read and the zone order
it would try — and creates nothing.

The finished box is added to your host list on a free port, so `go` works on
it immediately. **The NVIDIA driver is not in either base image**, and a box
without it runs ComfyUI on the CPU while looking perfectly healthy: Linux boxes
install it from a startup script on first boot, Windows boxes are handed the two
commands Google documents and you run them once. See
[troubleshooting](troubleshooting.md#the-nvidia-driver) for why that half is
deliberately manual.

## One command: `go`

```sh
comfy-qat go comfy-win
comfy-qat go windows      # the same box, described rather than named
```

That is the whole thing. In order, it:

1. starts the instance if it is stopped, and waits for it to reach RUNNING;
2. asks whether ComfyUI is already answering — if it is, you get the URL and the
   stamp immediately and nothing is installed or restarted;
3. installs ComfyUI if the box has none, on a pinned Python 3.12, with a torch
   built for whatever CUDA that box's driver reports;
4. launches it **on the box**, detached, with its output going to a log file
   there;
5. forwards a local port to it as soon as it is listening, waits until ComfyUI
   really answers, opens a browser on `http://127.0.0.1:<port>` — and gives you
   the prompt back.

The forward is an `ssh -L` carried over Identity-Aware Proxy. Two consequences
worth knowing, because both were learned the hard way:

- **It reaches the box's own loopback.** ComfyUI binds `127.0.0.1` there, nothing
  is exposed on any interface, and no firewall rule is involved — port 22 is
  already open, which is how every other command here reaches the machine.
- **It can only be opened once ComfyUI is listening.** The connection is tested
  before it will serve, so there is nothing to forward to until the launch has
  happened. That is why the order above puts the launch before the forward.

Why "up" means *ComfyUI answers* and not *the VM booted*: a machine that has booted
and serves nothing looks exactly like success and bills exactly like success. The
only honest test is asking ComfyUI itself. Detaching does not soften that. A
launch that returned as soon as the box said "started" would be the same lie one
level down, so `go` still does not return until ComfyUI has answered on the
tunnel.

`--no-browser` skips the browser; `--no-install` fails rather than installing on a
box that has none.

## Two machines at once

ComfyUI runs on the box. The terminal used to be occupied only because its log
was streamed back over SSH — which meant one machine per terminal, and Ctrl-C
stopped ComfyUI. Now it runs there and you get your prompt back:

```sh
comfy-qat go windows       # detached: starts it, forwards, prints the URL
comfy-qat go linux         # and now both, in one terminal, in two tabs
comfy-qat list             # both tunnelled
```

`down` each one when you are finished with it, or `down --all` to stop the lot.
**A box left running bills whether or not anything is pointed at it**, and two of
them bill twice — which is the cost of this convenience and worth saying plainly.
Two machines up at once used to be impossible; now it is ordinary, and the thing
that used to stop you overspending — the terminal being busy — is gone with it.

### Watching the log

```sh
comfy-qat logs linux            # follow it, as it is written
comfy-qat logs linux --tail 50  # the last 50 lines, then stop
```

It reads a file on the box — `/opt/comfyui/comfyui.log`, or
`C:\ComfyUI\comfyui.log` on Windows — and touches nothing else. **Ctrl-C ends
the reading and stops nothing**, which is the whole difference between this and
`--follow` below. Each launch truncates the file, so what you are reading is this
ComfyUI and not yesterday's traceback above it.

Three answers rather than a wait, when there is no log to read:

| state | what it says |
|---|---|
| the box is stopped | it has no ComfyUI and no log; `go` starts both |
| the box is up, nothing launched | there is no log file, and **the machine is billing** |
| `local` | your own ComfyUI's log is in the terminal you started it in |

### The old behaviour, when you want it

```sh
comfy-qat go linux --follow
```

Streams ComfyUI's startup log onto this terminal exactly as a local `main.py`
would print it, and **Ctrl-C stops ComfyUI** — it does not stop the box, and a
stopped ComfyUI on a running box still bills. Use it when you are debugging a
launch and want to watch it happen.

### A new window instead

```sh
comfy-qat go linux --new-window
```

Opens a new **macOS Terminal** window and runs `go <name> --follow` in it,
leaving this terminal free. That is the only thing it supports: anywhere else it
says so and starts nothing, printing the exact command to paste into a window you
open yourself. A window that silently does not appear, on a command that starts a
GPU box, is a machine you are paying for and cannot see.

## Changing machine: Windows to Linux and back

One box per OS, and switching between them is one command:

```sh
comfy-qat switch linux
comfy-qat switch windows
```

`switch` is `go` with the step people forget on the front. It starts the machine
you asked for, then stops whichever other cloud box was running or tunnelled —
which is the half that costs money, because a GPU box bills all night whether or
not anything is pointed at it. It says what it is starting and what it is
stopping, before it does either:

```
windows -> comfy-win (Windows Server 2022, L4)

  - go to comfy-win (Windows Server 2022, L4) on http://127.0.0.1:8190
  - then stop comfy-linux (Ubuntu 22.04, A100) — running and tunnelled
```

`--dry-run` prints that plan and stops. `--keep-others` leaves the other boxes
running, which is occasionally what you want and never what you want overnight.

**The target comes up first, on purpose.** If it cannot start you still have the
machine you were working on — see "when the box you want is unavailable" below.

### Say what you want, not what you called it

`windows` above is not a special name. Anywhere a host name goes, you can
describe the machine instead:

| you type | it means |
|---|---|
| `comfy-win` | that exact host — a name always wins |
| `windows` | the one declared host running Windows |
| `linux`, `ubuntu`, `debian` | the one running that. `linux` covers all of them |
| `macos`, `local` | the ComfyUI on this machine |
| `l4`, `a100` | the one with that card. `a100` also finds an `A100-80GB` |
| `windows/l4` | both at once, in either order, for when one axis is not enough |

The separator is a slash. Case and spare spaces do not matter, and `windows-l4`
tells you so rather than failing as an unknown host.

Whatever it resolves to is printed, so a description never quietly picks a box:

```
windows/l4 -> comfy-win (Windows Server 2022, L4)
```

If two machines fit, it refuses and shows both with their OS and card, so you can
see which half you left out:

```
'windows' matches 2 hosts: comfy-win (Windows Server 2022, L4), comfy-win-2
(Windows Server 2022, A100-80GB). Say which one: add the other half, e.g.
`windows/l4`, or use the host's name.
```

That refusal is deliberate. Guessing between two machines is exactly how you end
up reading a result from the wrong one.

## Which boxes do I have, and which are up?

```sh
comfy-qat list
```

```
NAME         KIND   OS                   GPU   URL                    STATE
local        local  -                    -     http://127.0.0.1:8188  -
comfy-win    gce    Windows Server 2022  L4    http://127.0.0.1:8190  -
comfy-linux  gce    Ubuntu 22.04         A100  http://127.0.0.1:8191  tunnelled
```

STATE is read from the tunnel files on this machine, so it costs nothing and is
always shown. A tunnel is what makes a cloud box answer on `127.0.0.1`, so
`tunnelled` is the honest answer to "which box am I looking at".

Whether the instances are *running* is a question only Google can answer, and it
is one call per box, so it is asked for rather than paid for every time:

```sh
comfy-qat list --live
```

```
comfy-win    gce    Windows Server 2022  L4    http://127.0.0.1:8190  running, tunnelled
comfy-linux  gce    Ubuntu 22.04         A100  http://127.0.0.1:8191  stopped
```

## When the box you want is unavailable

GPU capacity runs out. When it does, the box you asked for simply will not start,
and that is not a reason to stop testing:

```
Google has no L4 capacity in us-central1-a right now, so comfy-win cannot start.
This is not a fault on your side, and retrying in the same zone will not help.

comfy-linux is untouched — you still have the machine you were on

where you can test instead, easiest first:
    comfy-qat host switch comfy-linux   # Ubuntu 22.04, A100
    comfy-qat host switch local         # local install

if it has to be comfy-win:
    Google says us-central1-b has capacity right now:
    comfy-qat host move comfy-win --to us-central1-b
```

Same operating system is offered first, because someone who asked for Windows
usually needs Windows; a machine in the zone that just refused is offered last and
labelled, since it may well hit the same shortage. `move` is second because
rebuilding a box takes minutes and switching takes one command — see "when a zone
has no GPUs left" below for what it actually does.

`up` prints the same advice. It belongs to the failure, not to one command.

That block is copied verbatim from a real run, which is why the commands in it
still read `comfy-qat host switch` and `comfy-qat host move`: several of the
tool's own messages have not caught up with the verbs moving to the top level.
Both spellings work, and the short one is the one to learn.

## When you are done

```sh
comfy-qat down comfy-win
comfy-qat down --all       # every cloud box you have declared, in one go
```

`--all` exists because the question at the end of a session is never "is
comfy-win stopped", it is **"am I still paying for anything"** — and answering
that by naming each box in turn is how one gets missed, which matters more now
that having two up at once is normal. It takes no name, and one box refusing to
stop does not leave the rest running: it stops the others, then names what did
not stop and what to do about it.

Closes the tunnel and stops the instance — and with it the ComfyUI running on it,
which needs no separate step: nothing survives the machine going away. A stopped
box costs only its disk — cents
per day — which is why the pattern here is one box per OS, stopped when idle, rather
than deleting and rebuilding. `--keep-running` closes only the tunnel and leaves the
machine on — and, deliberately, the ComfyUI on it, which the next `go` finds
and uses rather than starting a second one. Occasionally what you want, never what
you want overnight.

See [cost.md](cost.md) for the one rule.

## The pieces, separately

`go` is `up` + install + serve. When you want the steps on their own:

```sh
comfy-qat up comfy-win      # start it, tunnel in, wait for ComfyUI to answer
comfy-qat open comfy-win    # tunnel only, to a box that is already running
comfy-qat logs comfy-win    # what the ComfyUI on it is saying
comfy-qat stamp comfy-win   # what is it running, exactly?
```

`open --dry-run` prints the `gcloud compute ssh ... -L` command instead of
running it, which is the thing to paste into a bug report when the forward itself
is what misbehaved. Note both ends are written `127.0.0.1` rather than
`localhost`: on macOS that name resolves to `::1` first, and ssh then binds IPv6
only while every attempt on `127.0.0.1` is refused.

`open` on a box where ComfyUI is not running yet will say so rather than
appear to succeed — there is nothing to forward to. `go` is the command that
starts it and forwards in one step.

A tunnel outlives the command that opened it, so its process id is recorded in
`~/.config/comfy-qa-tools/tunnels/<host>.pid` and checked rather than assumed.
Opening a tunnel that is already open tells you so and changes nothing; it does not
stack a second one on the same port, which is a failure you would never diagnose.

## Stamp the result

```sh
comfy-qat stamp comfy-win
```

```
comfy-win · ComfyUI 0.33.0 · windows · cuda:0 NVIDIA L4 (23GB) · torch 2.13.0 · python 3.12.13
```

Do this at the point you get a result, not afterwards from memory. It is the record
almost nothing else keeps, and it is what makes two people's "works for me" and
"broken for me" comparable. `--json` gives the same fields machine-readably, under
the names ComfyUI itself uses.

## When a zone has no GPUs left

GPU stockouts are routine and have nothing to do with your account, quota or
billing. Google simply has no L4 free in that zone right now, and retrying there
will not change it.

```sh
comfy-qat move comfy-win
```

Asks Google where there *is* capacity, then rebuilds the box in that zone with its
ComfyUI install intact: snapshot the boot disk, create a disk from the snapshot in
the new zone, create the instance, add it to your host list on a free port. `--to
us-central1-b` picks the zone yourself, `--dry-run` shows the plan and stops.

Four things worth knowing before you run it:

- **The original is never removed.** It stays stopped in its old zone, and deleting
  it is a deliberate act you take once the new one has proved itself.
- **The snapshot is the slow part.** A boot disk with models on it takes a while.
- **A move that stops part-way is resumed, not restarted.** Nothing is removed, but
  a snapshot and a 300 GB disk may now exist, and they bill. Run the same command
  again: it reads the project first, says what it found, and carries on from there.
  Every leftover it reports comes with the exact command that removes it, and
  `--clean` removes the ones belonging to this move and then carries on with the
  move itself — "clean up first", not "clean up instead". It never deletes
  anything without being asked.
- **The zone Google suggests can be out by the time you get there.** It is where
  there was capacity when it answered, not a reservation, and there is no way to
  check a zone's free capacity in advance. If the machine cannot be created after
  the disk is copied, the snapshot is kept, so moving to a different zone repeats
  only the disk rather than the whole copy.

**The box keeps its name, its port and its URL.** A move used to append a second
host entry under a new name on a new port, leaving the original entry pointing at
the zone that had no capacity — so `go <box>` failed after a move exactly as it
had before one. Google only requires instance names to be unique per *zone*, so
the name travels.

The box you moved away from is not dropped: it still exists and still bills until
somebody deletes it. It stays in the host list renamed by where it is —
`comfy-win-us-central1-a` — so `down` can still reach it. That name says something;
`comfy-win-a` did not, and after two moves neither did `comfy-win-a-b`.

The host list is rewritten in place to do this. It is checked before it lands —
the result must parse and must hold exactly the machines it should — the previous
file is copied to `hosts.toml.bak`, and the swap is atomic. Comments and hosts
this tool does not manage are preserved.

## Which machine am I actually on?

Every command names its host, and there is no default — no "current machine" is
remembered anywhere, and a description is resolved and printed on the spot rather
than stored. That is deliberate: both a local ComfyUI and a tunnel to a cloud box
answer on `127.0.0.1` and look identical in a browser. The port rules in [hosts.md](hosts.md) exist for the same reason —
8188 belongs to the local install and no cloud host may take it.

If a result surprises you, `stamp <name>` is the fastest way to find out you
were looking at the other machine.
