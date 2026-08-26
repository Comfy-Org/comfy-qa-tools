# The everyday loop

You have a host list. This is what you do with it, from "I need a Windows box with
an L4" to "I am done and not paying for it any more".

## One command: `go`

```sh
comfy-qat host go comfy-win
```

That is the whole thing. In order, it:

1. starts the instance if it is stopped, and waits for it to reach RUNNING;
2. opens an Identity-Aware Proxy tunnel to it, so ComfyUI's port is reachable at
   `http://127.0.0.1:<port>` without any port being opened to the internet;
3. asks whether ComfyUI is already answering — if it is, you get the URL and the
   stamp immediately and nothing is installed or restarted;
4. installs ComfyUI if the box has none, on a pinned Python 3.12;
5. launches it in the foreground with its startup log on your terminal, and opens
   a browser when it starts serving.

Ctrl-C stops ComfyUI. **It does not stop the box, and a stopped ComfyUI on a running
box still bills.** `--no-browser` skips the browser, `--no-install` fails rather than
installing on a box that has none.

Why "up" means *ComfyUI answers* and not *the VM booted*: a machine that has booted
and serves nothing looks exactly like success and bills exactly like success. The
only honest test is asking ComfyUI itself.

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

`host open --dry-run` prints the `gcloud compute start-iap-tunnel` command instead
of running it, which is the thing to paste into a bug report when the tunnel itself
is what misbehaved.

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

Every command names its host, and there is no default. That is deliberate: both a
local ComfyUI and a tunnel to a cloud box answer on `127.0.0.1` and look identical
in a browser. The port rules in [hosts.md](hosts.md) exist for the same reason —
8188 belongs to the local install and no cloud host may take it.

If a result surprises you, `host stamp <name>` is the fastest way to find out you
were looking at the other machine.
