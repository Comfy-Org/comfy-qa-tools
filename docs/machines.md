# The everyday loop

You have a host list. This is what you do with it, from "I need a Windows box with
an L4" to "I am done and not paying for it any more".

## One command: `go`

```sh
comfy-qat host go comfy-win
comfy-qat host go windows      # the same box, described rather than named
```

That is the whole thing. In order, it:

1. starts the instance if it is stopped, and waits for it to reach RUNNING;
2. asks whether ComfyUI is already answering — if it is, you get the URL and the
   stamp immediately and nothing is installed or restarted;
3. installs ComfyUI if the box has none, on a pinned Python 3.12, with a torch
   built for whatever CUDA that box's driver reports;
4. launches it in the foreground with its startup log on your terminal;
5. forwards a local port to it as soon as it is listening, and opens a browser
   on `http://127.0.0.1:<port>`.

The forward is an `ssh -L` carried over Identity-Aware Proxy. Two consequences
worth knowing, because both were learned the hard way:

- **It reaches the box's own loopback.** ComfyUI binds `127.0.0.1` there, nothing
  is exposed on any interface, and no firewall rule is involved — port 22 is
  already open, which is how every other command here reaches the machine.
- **It can only be opened once ComfyUI is listening.** The connection is tested
  before it will serve, so there is nothing to forward to until the launch has
  happened. That is why the order above puts the launch before the forward.

Ctrl-C stops ComfyUI. **It does not stop the box, and a stopped ComfyUI on a running
box still bills.** `--no-browser` skips the browser, `--no-install` fails rather than
installing on a box that has none.

Why "up" means *ComfyUI answers* and not *the VM booted*: a machine that has booted
and serves nothing looks exactly like success and bills exactly like success. The
only honest test is asking ComfyUI itself.

## Changing machine: Windows to Linux and back

One box per OS, and switching between them is one command:

```sh
comfy-qat host switch linux
comfy-qat host switch windows
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
comfy-qat host list
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
comfy-qat host list --live
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

comfy-linux is untouched — you still have the machine you were on.

Where you can test instead, easiest first:
  comfy-qat host switch comfy-linux   # Ubuntu 22.04, A100
  comfy-qat host switch local         # local install

If it has to be comfy-win:
  Google says us-central1-b has capacity right now.
  comfy-qat host move comfy-win --to us-central1-b
```

Same operating system is offered first, because someone who asked for Windows
usually needs Windows; a machine in the zone that just refused is offered last and
labelled, since it may well hit the same shortage. `move` is second because
rebuilding a box takes minutes and switching takes one command — see "when a zone
has no GPUs left" below for what it actually does.

`host up` prints the same advice. It belongs to the failure, not to one command.

## When you are done

```sh
comfy-qat host down comfy-win
```

Closes the tunnel and stops the instance. A stopped box costs only its disk — cents
per day — which is why the pattern here is one box per OS, stopped when idle, rather
than deleting and rebuilding. `--keep-running` closes only the tunnel and leaves the
machine on, which is occasionally what you want and never what you want overnight.

See [cost.md](cost.md) for the one rule.

## The pieces, separately

`go` is `up` + install + serve. When you want the steps on their own:

```sh
comfy-qat host up comfy-win      # start it, tunnel in, wait for ComfyUI to answer
comfy-qat host open comfy-win    # tunnel only, to a box that is already running
comfy-qat host stamp comfy-win   # what is it running, exactly?
```

`host open --dry-run` prints the `gcloud compute ssh ... -L` command instead of
running it, which is the thing to paste into a bug report when the forward itself
is what misbehaved. Note both ends are written `127.0.0.1` rather than
`localhost`: on macOS that name resolves to `::1` first, and ssh then binds IPv6
only while every attempt on `127.0.0.1` is refused.

`host open` on a box where ComfyUI is not running yet will say so rather than
appear to succeed — there is nothing to forward to. `host go` is the command that
starts it and forwards in one step.

A tunnel outlives the command that opened it, so its process id is recorded in
`~/.config/comfy-qa-tools/tunnels/<host>.pid` and checked rather than assumed.
Opening a tunnel that is already open tells you so and changes nothing; it does not
stack a second one on the same port, which is a failure you would never diagnose.

## Stamp the result

```sh
comfy-qat host stamp comfy-win
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
comfy-qat host move comfy-win
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
  `--clean` removes the ones belonging to this move and stops. It never deletes
  anything without being asked.
- **The zone Google suggests can be out by the time you get there.** It is where
  there was capacity when it answered, not a reservation, and there is no way to
  check a zone's free capacity in advance. If the machine cannot be created after
  the disk is copied, the snapshot is kept, so moving to a different zone repeats
  only the disk rather than the whole copy.

The new box gets a new name — the instance name with a zone suffix — because two
machines that differ only by zone and share a name is how you end up reading
results from the wrong one.

## Which machine am I actually on?

Every command names its host, and there is no default — no "current machine" is
remembered anywhere, and a description is resolved and printed on the spot rather
than stored. That is deliberate: both a local ComfyUI and a tunnel to a cloud box
answer on `127.0.0.1` and look identical in a browser. The port rules in [hosts.md](hosts.md) exist for the same reason —
8188 belongs to the local install and no cloud host may take it.

If a result surprises you, `host stamp <name>` is the fastest way to find out you
were looking at the other machine.
