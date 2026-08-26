# Troubleshooting

Every error this tool prints, what causes it, and the fix. If you hit something
that is not listed here, that is a bug in this page as much as in the code — and
`tests/test_docs.py` reads the errors out of the source, so it is a bug the test
suite will already have told us about.

## Installing

**`comfy-qat: command not found`**
The binary is installed but its directory is not on your `PATH`. Virtualenv `bin`
directories usually are not, and `uv` installs to `~/.local/bin`, which macOS does
not include by default. Call it by full path, add an alias, or run
`uv tool update-shell`.

**`comfy-qa` runs but `comfy-qat` does not**
You have an older install. `pip uninstall -y comfy-qa-cli comfy-qa`, then install
again. Note that `comfy-qa` is also
[a different project's binary](https://github.com/Comfy-Org/Comfy-QA), so leaving it
installed is confusing for more than one reason.

## Host list

**no host list at ~/.config/comfy-qa-tools/hosts.toml. Run `comfy-qat host init`
to write a starter one.**
You have not set one up yet. `comfy-qat setup` writes one too, along with
everything else a first run needs.

**`~/.config/comfy-qa-tools/hosts.toml already exists. Use --force to overwrite
it.`**
`host init` will not write over a host list you have edited. If you really do want
the starter file back, `comfy-qat host init --force` — and copy your cloud hosts
out first, because they are not merged back in.

**`no [hosts.<name>] tables found`**
The file parses as TOML but declares no machines. Every host is a table named for
it, `[hosts.local]`, and an empty host list is treated as a mistake rather than as
"no machines", because a tool that silently operates nothing is worse than one
that stops.

**`~/.config/comfy-qa-tools/hosts.toml is not valid TOML: ...`**
A syntax error, quoted from the parser with the line it failed on. The usual cause
is an unquoted string — every value except `port` needs quotes.

**`unknown host 'comfy-lnux'. Declared: local, comfy-win`**
You named a machine that is not in the host list. The message lists the names that
are, which is faster than opening the file.

**`host 'comfy-win': expected a table, got str`**
Something under `[hosts]` is a bare value rather than a table — usually
`hosts.comfy-win = "..."` where `[hosts.comfy-win]` was meant.

**`host 'comfy-win': kind must be 'local' or 'gce', got None`**
A host has a missing or misspelled `kind`. Those are the only two values.

**`host 'comfy-win': unknown field(s) gce_zoen`**
A typo. Unknown fields are rejected rather than ignored, so this fails now instead
of leaving you puzzled later about why the box cannot be found. Check the spelling
against [hosts.md](hosts.md).

**`host 'comfy-win': kind 'gce' requires gce_zone, gce_project`**
A cloud host is missing detail needed to locate it in Google Cloud. All of `os`,
`gpu`, `gce_instance`, `gce_zone` and `gce_project` are required.

**`host 'comfy-win': kind 'gce' requires an explicit port`**
Only `local` hosts get a default port. Cloud hosts must say which local port their
tunnel uses, because that is the number you will type into a browser.

**`host 'comfy-win': port must be an integer, got '8190'`**
The port is quoted. TOML would accept the string; this tool will not, because a
quoted port silently fails to match the one a tunnel opens.

**`host 'comfy-win': port 80 is outside 1024-65535`**
Ports below 1024 need root. Pick a high one.

**`host 'comfy-win': port 8188 is reserved for the local ComfyUI. A tunnel on it
would silently point you at the wrong machine — pick another port, e.g. 8190.`**
A `gce` host is declared on 8188. That is ComfyUI's own default port, and your
local install answers on it, so a tunnel there would show you the wrong machine
while looking exactly right. This is the single mistake the tool exists to
prevent.

**`hosts 'a' and 'b' both use port 8190. Every host needs its own port, or you
cannot tell which one you reached.`**
Two hosts share a port. Give each its own.

## Setup

Setup stops rather than guessing whenever the fix is something only you can do. It
is safe to run again: it skips whatever is already done.

**`not signed in to Google Cloud`**
You used `--non-interactive`, so setup would not open a browser. Run
`gcloud auth login`, then setup again.

**`sign-in did not complete`**
The browser sign-in was cancelled or failed. Run `comfy-qat setup` again.

**`still not signed in`**
`gcloud auth login` reported success and gcloud still lists no active account.
That is a broken gcloud install or a credential store it cannot write to, not
something setup can work around. Run `gcloud auth list` and `gcloud auth login`
by hand and read what they say.

**`this account has no Google Cloud projects`**
Nothing to work in. Create one at the link printed, then run setup again.

**`no project set and 3 to choose from`**
You used `--non-interactive` with several projects, so setup will not pick for you.
Name it: `comfy-qat setup --project <id>`.

**`no billing account is linked to <project>, so no instance can start`**
Only you can attach billing. The error prints the direct link; do that, then run
setup again.

**`could not read GPU quota (...). Check later: comfy-qat auth quota`**
Setup carries on regardless — quota can take days to change and is never a reason
to strand you mid-setup. This is a warning, not a stop; setup still writes your
host list. Check quota afterwards with the command it names.

**`the request was refused: ...`**
Setup offered to ask Google for GPU quota and Google turned the request down on
submission. The most common cause is an account with no billing history — quota is
frequently withheld until a project has been billed at least once. Setup continues;
see [cost.md](cost.md) and ask again later.

**`could not list cloud boxes (...). Add them by hand if needed.`**
Discovery failed, so nothing was added. Setup finishes anyway; add hosts by hand
from [hosts.md](hosts.md), or run `comfy-qat host discover` later.

## Starting and stopping

**`ComfyUI is not answering on http://127.0.0.1:8188`**
The local host in your list is not serving. Nothing on your machine is stopped or
started by this tool, so start ComfyUI yourself:

```sh
~/ComfyUI/venv/bin/python ~/ComfyUI/main.py --port 8188 --listen 127.0.0.1
```

**`ComfyUI is not running locally. Start it with:`** from `host go`
The same thing, from the command that would otherwise install ComfyUI for you —
which it will not do to your own machine. It prints the line above; run it.

**`ComfyUI is not answering and --no-install was given.`**
`host go` found no ComfyUI on the box and you told it not to install one. Drop
`--no-install`, or get onto the machine and install by hand.

**`could not run a command on comfy-win: ...`**
`host go` reaches the box over SSH through IAP to check for ComfyUI. If that fails,
the instance is missing the `enable-windows-ssh` metadata (Windows) or your account
lacks the IAP tunnel role. The error prints the manual way in.

**`comfy-win is running but not accepting commands after 300s`**
The VM is powered on but its SSH server is not answering. RUNNING means booted, not
ready, and Windows takes minutes to get the rest of the way. Past five minutes it is
usually the `enable-windows-ssh` metadata or an IAP permission rather than slowness —
the error prints the command to try by hand.

**`the ComfyUI install on comfy-win did not finish`**
The install script exited non-zero. Its output is on your terminal above the error —
read that first. Get onto the box with the printed command to finish by hand.

**`comfy-win is running and tunnelled, but ComfyUI is not answering on
http://127.0.0.1:8190. The machine is up and billing; ComfyUI is not installed or
not started.`**
The machine and the tunnel are both fine — ComfyUI itself is not serving. **The box
is billing while this is true.** The error prints how to get onto it, which differs
by OS: Windows needs a password reset and Remote Desktop over the tunnel, anything
else takes SSH through IAP.

**`NO_PYTHON`** in the ComfyUI startup log
ComfyUI is installed but no interpreter was found beside it — no `venv`, no
portable `python_embeded`, and no system `python`. Get onto the box and create one,
or reinstall with `host go` on a box that has none.

**`comfy-win did not reach RUNNING within 300s`**
The instance was asked to start and did not. Check it in the Google Cloud console;
this usually means capacity or a quota problem in that zone rather than a fault
with the box.

**`could not start comfy-win: ...`**
gcloud refused. The most common cause is GPU quota — `comfy-qat auth quota` shows
what you actually have.

**`could not stop comfy-win: ...`**
gcloud refused to stop the machine, so **it is still running and still billing.**
Try again, and if it keeps failing stop it in the Google Cloud console — an
instance nobody stopped is the most expensive failure this tool has.

**`Google has no L4 capacity in us-central1-a right now, so comfy-win cannot start.
This is not a fault on your side, and retrying in the same zone will not help.`**
A stockout. The zone has none of that card free, which is routine for GPUs and has
nothing to do with your account, quota or billing.

Google usually names a zone that *does* have capacity, and the fix line repeats it —
"Google says us-central1-b has capacity right now" — with the `host move` command
to go there. Otherwise, wait: capacity varies by hour.

## Moving a box to a zone with capacity

`host move` snapshots the boot disk, rebuilds the box elsewhere and leaves the
original stopped. Nothing is deleted, at any point, by anything here.

**`comfy-win is local — there is nowhere to move it to.`**
Only cloud hosts have a zone. A local install is where it is.

**`Google did not name a zone with capacity. Pick one with --to, e.g. --to
us-central1-b`**
Asked to find a zone itself, `move` starts the box and reads the zones out of the
stockout message. This time Google did not suggest any — which happens when the
card is short everywhere, or when the start failed for some other reason. Name a
zone yourself, or wait.

**`the move failed: ...`** / **`nothing was removed — comfy-win is untouched in
us-central1-a.`**
One of the four steps — snapshot, disk, instance, host list — did not complete. The
second line is the important one: the original box is exactly as it was, so you can
retry, or move by hand. Any half-made snapshot or disk is left behind for you to
look at and is not cleaned up automatically; delete it in the console once you are
done.

## Stamping a machine

**`nothing answered at http://127.0.0.1:8190`**
Nothing is listening on that port. For a local host, ComfyUI is not running. For a
cloud host, either the box is off or the tunnel is not up. Check the port in your
host list matches what the machine actually serves.

**`http://127.0.0.1:8190 answered, but not with ComfyUI's /system_stats`**
Something is on that port, but it is not ComfyUI — a dev server, or another tunnel.
This is exactly the mix-up the port rules exist to prevent, so it is worth chasing
rather than working around.

## Checking environments

`comfy-qat env` reports what each cloud environment is serving, which is how a
failed deploy gets caught: it leaves the old build running and looks entirely
normal.

**`unknown environment(s): testclod. known: testcloud, stagingcloud, cloud, local`**
A typo in an environment name. Those four are the only ones there are.

**`not probed: cloud`**
You asked for the evidence block of an environment that was not in the run. Either
name it as a target too, or drop the other targets: `--evidence` reports on what was
probed, not on everything that exists.

**--expect needs exactly one cloud environment, e.g. `comfy-qat env testcloud
--expect <sha>`**
`--expect` compares one environment against one SHA, so it needs exactly one cloud
target named. With none or several there is nothing unambiguous to compare.

**`FAIL  testcloud serves 4f2a1b9c, expected 9d7e3a10`**
The environment is not running the build you named — which is the whole point of
the check, and usually means the deploy did not land rather than that you typed the
wrong SHA. Confirm the SHA in the frontend repo before assuming the deploy failed.

## Google Cloud

**`gcloud is not installed or not on PATH.`**
Install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install

**`your gcloud session has expired`**
Credentials time out. Run `gcloud auth login`. This is the most common failure and
it looks alarming in raw gcloud output — it is routine.

**`no active gcloud account`** / **`nobody signed in`**
Run `gcloud auth login`.

**`no project set`** / **`no project set. Run: comfy-qat setup`**
gcloud has no default project, so there is nothing to look in. `comfy-qat setup`
picks one and sets it; `gcloud config set project <your-project-id>` does the same
thing by hand.

**`no billing account linked to <project>`**
A project without billing cannot start any instance. Link one in the console; the
error prints the direct link.

**`gcloud returned output that is not JSON: ...`**
Every call here asks for `--format=json` and gcloud answered with something else,
quoted after the colon. In practice that means a prompt or a warning arrived on
stdout — an SDK component update, or an interactive question. Run the same gcloud
command yourself, answer whatever it asks, then try again.

**`gcloud timed out after 240s`**
Listing quotas returns every compute quota on the project — around 400 records, over
a megabyte — and takes about a minute on a healthy connection. A timeout at 240
seconds means something is genuinely wrong with the network, not that the call is
slow. Try again.

**`zero GPU quota on this project — no GPU instance can start`**
A new project has no GPU quota at all. See [cost.md](cost.md), then:
```sh
comfy-qat auth quota request --gpu l4 --region us-central1
```
`--quota-id <id>` takes a raw Google quota id instead, if you would rather name one
exactly.

**`name what you want: --gpu l4,a100 (or --quota-id for a raw id)`**
`auth quota request` was run with nothing to request. It will not guess a card for
you — asking for the wrong one wastes days of approval time.

**`this project reports no quota for 'h100'. Available: A100, L4, T4`**
You asked for a card Google does not offer this project, or not in that region.
The message lists what is available. `comfy-qat auth quota` shows the same thing
with current limits.

**`request for l4 failed: ...`**
The quota request was rejected on submission. The most common cause is an account
with no billing history — Google frequently will not grant GPU quota until a project
has been billed at least once. Retrying will not change that.

**`still pending: l4, a100`** (exit code 75)
Not an error. The requests went in but have not been approved within the wait window.
Approval can take days. Run the same command again to keep waiting, or
`comfy-qat auth quota` to check. The console link printed with the request shows the
same thing.
