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

**`hosts 'comfy-win' and 'comfy-win-b' are the same machine: instance
'comfy-win' in us-central1-a (proj). Two entries, two ports, two tunnels, one box
— and a result recorded against one of those names says nothing whatever about
the other. Delete one, or point it at a different instance.`**
Two `gce` hosts name one GCE instance. The port rule already says every host
answers on its own port; it does not say every host is its own machine, and this
is what does. Left alone it is the worst failure this tool has, because nothing
looks wrong: both entries load, both tunnel, both stamp, and a matrix records
"reproduced on comfy-win, not on comfy-win-b" about the same box. Usually it is a
copied entry someone forgot to repoint after `host move`.

**`hosts 'comfy-win' and 'Comfy-Win' differ only in case. Which machine you
reached would depend on a shift key, so they cannot both be declared. Rename one
of them, or delete it if they are the same box.`**
Two names that differ only in capitalisation are one machine typed two ways far
more often than they are two machines. Looking a host up already falls back to a
case-insensitive match, so with both declared which of them you get depends on
how you typed it.

**`host 'local': kind 'local' cannot carry gce_instance, gce_zone, gce_project.
Stopping a machine is decided from 'kind', so a cloud box declared local is never
stopped and keeps billing. Set kind = "gce" if it is a cloud box, or delete those
fields if it is not.`**
This one costs money. `host down` decides what to stop from `kind` alone: for a
local host it reports "local ComfyUI left running" and returns without stopping
anything. A cloud box mistyped as `local` — or edited down to one after a move —
therefore reads as a successful `host down` while the GPU bills all night. The
contradiction is refused when the host list is read, so `down` can never be
handed one.

**`host 'local': the name 'local' is reserved for the ComfyUI on this computer,
which is what every example and the starter host list means by it. A cloud box
wearing it puts an invisible default back. Rename the box, e.g. comfy-win or
comfy-linux.`**
`comfy-qat host stamp local` has one obvious meaning, and the starter host list
teaches it. A `gce` host holding that name reinstates exactly the invisible
default the rest of this file exists to remove.

**`host name '--config' cannot be used. A name has to start with a letter or a
digit and hold only letters, digits, dots, dashes and underscores — it is typed
as an argument and used as a filename, so a name that is blank, padded, or starts
with a dash is read as an option or cannot be typed at all. Rename it, e.g.
comfy-win.`**
TOML table keys are unrestricted, so `""`, `"   "`, `"../evil"`, `"-f"` and
`"comfy\nwin"` are all valid keys — and a host name is both an argument you type
and part of a filename, so none of them is usable as one. Rename the table.

**`expected a host list of [hosts.<name>] tables, got list`**
Whatever was handed to the parser is not a table at all. From a file this is
unusual — TOML always decodes to a table — so it generally means something is
calling `parse` with the wrong thing.

**`~/.config/comfy-qa-tools/hosts.toml could not be read: [Errno 21] Is a
directory. Check that it is a file rather than a directory, and that you own it —
`--config` pointed at the folder instead of the hosts.toml inside it looks
exactly like this.`**
The path exists but cannot be opened. The usual cause is the one named: `--config`
given `~/.config/comfy-qa-tools` rather than the `hosts.toml` in it. The other is
a file restored from a backup with the wrong owner — `ls -l` it and `chown` it
back.

**`~/.config/comfy-qa-tools/hosts.toml is not UTF-8 text, so it cannot be a host
list. Check it was not saved as UTF-16 by an editor, truncated by a half-finished
write, or overwritten with something binary.`**
The file is not text this tool can decode. Open it and look — if it is
unrecognisable, `comfy-qat host init --force` writes a fresh starter one, and you
will have to re-add your cloud hosts.

**`could not write a host list to /somewhere/hosts.toml: [Errno 13] Permission
denied. Give `--config` a path you can write to — the file itself, not the folder
it goes in.`**
`host init` could not create the file. Either the folder is not writable, or
`--config` was pointed at a directory. It names the path it tried.

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

**`could not read your host list (...), so nothing was added to it`**
Discovery found cloud boxes but your `hosts.toml` will not parse, so setup left it
completely alone rather than appending to a file it cannot read. That restraint is
deliberate: appending to a broken list would add a second `[hosts.<name>]` table
for a box already declared, and a duplicate table is not valid TOML — one fixable
mistake would become a file nothing can load, on the one command that promises to
change nothing. The message carries the parse error; fix that in the file, then run
`comfy-qat host discover`.

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

It can also mean **the tunnel never opened.** The tunnel runs detached, so if your
session expired between starting the box and opening the tunnel, the tunnel died
and this message blames ComfyUI. Run `comfy-qat auth status` before you go looking
on the box — and see [session-expiry.md](session-expiry.md), because this is the
shape that failure takes.

**`could not tell whether comfy-win reached RUNNING: ...`**
The box was asked to start and then gcloud stopped answering, so the tool does not
know what happened — which is different from knowing it failed. **It may be running
and billing.** The message carries gcloud's own reason. Check the instance in the
console, then either try again or `comfy-qat host down comfy-win`.

**`comfy-win did not reach RUNNING within 300s. It was asked to start, so it may be billing already.`**
The start was accepted and the box never came up. Usually capacity or quota in that
zone rather than a fault with the box. The important half is the second sentence:
a start that never finished still creates an instance you can be charged for, so
stop it rather than walking away.

**`could not open the tunnel to comfy-win: ...`**
gcloud could not start the Identity-Aware Proxy tunnel. The nested message says
why — most often an expired session, a missing IAP permission, or something
already holding the local port. **The box is running while this is true**, so the
fix line ends with the command that stops it.

**`gcloud is not installed or not on PATH, so no tunnel can be opened.`**
The tunnel is `gcloud compute start-iap-tunnel` and nothing else, so there is no
fallback. Install the SDK from the link in the fix line. Note `gcloud` may be on
your interactive `PATH` and not on the one a script or a launchd job runs with.

**`could not start the tunnel: ...`** and
**`could not start the tunnel to comfy-win: ...`**
The process could not be launched at all — the first when `Popen` itself failed,
the second when gcloud was not where it was expected. The operating system's own
reason is quoted. Check that gcloud is installed and on your `PATH`.

**`the tunnel closed as soon as it was opened (gcloud exited 1). gcloud said: ...`**
The one failure that used to be recorded as a success. gcloud fails immediately far
more often than it fails later — an expired credential is the common case, and
because its output is captured it cannot prompt, so it exits rather than asking. A
pid file written over that turns a credential failure into "the box is up but
ComfyUI is not answering", with the machine left running and billing. So a fresh
tunnel is watched for a second and a half, and a dead one is reported with the end
of gcloud's own log instead of being written down. If it mentions credentials or
reauthentication, `gcloud auth login` and try again; the full log is in
`~/.config/comfy-qa-tools/tunnels/<host>.log`.

**`something is already listening on 127.0.0.1:8190, and it is not a tunnel this
tool opened. A tunnel started now could not bind that port, so
http://127.0.0.1:8190 would answer for whatever is already there.`**
The port is checked before gcloud is started, because gcloud that cannot bind
still runs — so the URL you are handed reaches whoever holds the port, which on
this Mac is usually a tunnel to a different box or the local ComfyUI. Find it with
`lsof -nP -iTCP:8190 -sTCP:LISTEN`, or give the host a different port in your host
list.

**`a tunnel called 'comfy-win' is already open (pid 4242), but it goes to
comfy-win-2 in us-west1-b on port 8195, not to comfy-win in us-central1-a on port
8190.`**
The name is not the machine. A second host list — `--config other.toml`, or an
edited one — can call a different instance `comfy-win`, and a tunnel is only reused
when the instance, the zone, the project and the port all match what you asked for.
`comfy-qat host down comfy-win`, then open this one.

**"another `comfy-qat` is opening the tunnel to comfy-win right now."**
Two terminals opening at once would each start gcloud: one binds the port, the
other does not, and only one of them ends up recorded — leaving a tunnel running
that nothing can find or stop. The claim is released as soon as the other command
finishes, and is assumed abandoned after two minutes. Wait, then run it again.

**`the tunnel to comfy-win started (pid 4242) but could not be recorded: ..., so it
was closed again.`**
A tunnel nothing has a record of cannot be closed by `down`: it holds a local port
onto a machine you are still paying for until someone finds it by hand. So it is
stopped rather than left running. Check the permissions on
`~/.config/comfy-qa-tools/tunnels/`, then open it again.

**`comfy-win is local — there is nothing to tunnel. It is at http://127.0.0.1:8188.`**
The host is not a cloud box, so there is no `--zone` or `--project` to tunnel with.
Use the URL directly.

**`the tunnel to comfy-win closed, so nothing is listening on http://127.0.0.1:8190 any more. ComfyUI was never reached.`**
The tunnel opened and then died, which from the near end looks exactly like a box
with no ComfyUI on it — silence on a port. Only one of those is fixed by going onto
the machine, so they are now reported separately. Read what gcloud wrote in
`~/.config/comfy-qa-tools/tunnels/<host>.log`; an expired session is the usual
cause. Reopen with `comfy-qat host open <name>`, or stop paying for the box.

**`gcloud is not signed in, so comfy-win cannot be reached: ... Waiting will not fix this, and the machine is running and billing.`**
The credential died between starting the box and reaching it. The tool used to keep
retrying for five minutes, which cannot succeed and costs money the whole time, so
it now stops immediately and closes the tunnel behind it. `gcloud auth login`, then
carry on — the box is still running. See [session-expiry.md](session-expiry.md).

**`ComfyUI on comfy-win exited without ever answering on http://127.0.0.1:8190. The machine is up and billing.`**
ComfyUI started and stopped without ever serving — a bad argument, the wrong
directory, a missing requirement printed and gone. Its own log is above the error
on your terminal; read that first. Note the exit code alone would have said
success, which is the same lie as calling a booted VM "up".

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

**`comfy-win says kind = 'local' but names a cloud instance (comfy-win, us-central1-a, a-project). Refusing to report it as stopped: if that machine is running, it is billing.`**
A host entry claims to be a local install while carrying the fields that identify
a Google Cloud box. `host down` would have closed the tunnel, said "local ComfyUI
left running — this tool did not start it", and left a GPU instance running.
A cloud box is `kind = "gce"`; fix the entry and run `host down` again.

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

## Tunnels

A tunnel outlives the command that opened it, so what it is gets recorded rather
than assumed — the instance, the zone, the project and the port, beside the
process id. Everything below is that record refusing to be taken on trust. All of
it reaches you through `host open`, and through `host up` and `host go`, which
open a tunnel on the way.

**`comfy-win is local — there is nothing to tunnel. It is at
http://127.0.0.1:8188.`**
A `kind = "local"` host is already reachable; there is nothing between you and
it. Nothing is wrong, and the URL is in the message.

**`a tunnel called 'comfy-win' is already open (pid 4021), but it goes to
comfy-win-2 in us-west1-b on port 8195, not to comfy-win in us-central1-a on port
8190.`**
A name is not a machine. A second host list can call a different box `comfy-win`
too, and a tunnel recorded under that name may go anywhere. This used to be
reported as "tunnel already open" with *your* host list's URL beside it, which is
the wrong-machine failure wearing a success message. Close the one that is open —
`comfy-qat host down comfy-win` — then open this one.

**`something is already listening on 127.0.0.1:8190, and it is not a tunnel this
tool opened. A tunnel started now could not bind that port, so
http://127.0.0.1:8190 would answer for whatever is already there.`**
The port is taken by something else — often a tunnel from an earlier session that
outlived its pid file, or a local server. The fix line gives you
`lsof -nP -iTCP:8190 -sTCP:LISTEN` to find it; the alternative is giving that host
a different port in your host list.

**``another `comfy-qat` is opening the tunnel to comfy-win right now.``**
Two commands tried to open the same tunnel at once, which is ordinary on a machine
running several agents. One of them wins; wait a moment and run yours again. A
claim older than two minutes is treated as abandoned, so a killed command does not
lock a host out.

**`could not start the tunnel to comfy-win: ...`**
The `gcloud` process would not launch at all. Check that gcloud is installed and
on your `PATH` — `comfy-qat setup` checks this too.

**`cannot write to ~/.config/comfy-qa-tools/tunnels: ...`**
The tunnel records live beside your host list, and that directory could not be
created. Check the permissions on `~/.config/comfy-qa-tools`.

**`the tunnel to comfy-win started (pid 4021) but could not be recorded: ...
Stop it by hand — nothing here can find it again.`**
The rarest one, and the only one that leaves something behind. The tunnel is
running but nothing on disk says so, so `host down` cannot find it. Kill the pid
in the message, fix whatever made the directory unwritable, and open it again.

## Moving a box to a zone with capacity

`host move` snapshots the boot disk, rebuilds the box elsewhere and leaves the
original stopped. Nothing is deleted, at any point, by anything here.

**`comfy-win is local — there is nowhere to move it to.`**
Only cloud hosts have a zone. A local install is where it is.

**`--dry-run cannot work out which zone has capacity. The only way to ask is to
try to start comfy-win, and if it starts it is billing — so a dry run that did it
would be the most expensive command here. Say where you want it and the rest of
the plan is printed without touching anything: comfy-qat host move comfy-win --to
us-central1-b --dry-run.`**
Nothing answers "where is there an L4 free". `move` finds out by trying to start
the machine and reading the zone out of the refusal — and when there is no
refusal, the box is up and billing. That is fine for a real move, which was going
to start something anyway, and not fine at all for `--dry-run`, whose whole
promise is that it changes nothing. So the two are refused together: give
`--dry-run` a `--to` and it prints the full plan, contacting nothing that costs
money.

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

**`http://127.0.0.1:8190 refused the request (401)`**
Something is there and it wants credentials. A stamp reads ComfyUI's own
`/system_stats`, which needs none — so this is almost always a different service on
that port, or a proxy in front of it. Check the port in your host list against what
the machine actually serves.

**`http://127.0.0.1:8190 answered 404 for /system_stats`**
A web server, but not a ComfyUI: the port answers and the endpoint is not there.
Reported separately from "nothing answered" on purpose — that difference decides
whether you go and start a server or go and find out what is holding the port.

## Naming the machine you want

Every command that takes a machine accepts its name, an operating system, a card,
or both as `os/card` — `host switch windows`, `host go l4`, `host up windows/l4`.
These are the refusals, and each one is a refusal rather than a guess on purpose:
a tool that picks for you is a tool that reads results from the wrong box.

**`nothing declared matches 'windows/l4'. Declared: local (local install); comfy-linux (Ubuntu 22.04, A100). Create the box in the Google Cloud console, then `comfy-qat host discover` to add it to your host list.`**
You described a machine you do not have. The message lists what you do have, with
each one's OS and card, so you can see which half was wrong. If the box exists in
Google Cloud but not in your host list, `comfy-qat host discover` adds it — nothing
needs typing, Google already knows its zone, card and OS.

**`'windows' matches 2 hosts: comfy-win (Windows Server 2022, L4), comfy-win-2 (Windows Server 2022, A100-80GB). Say which one: add the other half, e.g. `windows/l4`, or use the host's name.`**
Two machines fit. Add the other half of the description — the card, here — or name
the host outright. Nothing was started, stopped or contacted; this is decided
offline, before any cloud call.

**`'windows-l4' is two descriptions run together. The separator is '/': windows/l4`**
A hyphen reads as part of a name, and host names contain hyphens, so the two
cannot both be separators. Use `/`.

**`unknown host 'rtx4090'. Declared: local, comfy-win. You can also describe the machine instead: an operating system (windows, linux, ubuntu, debian, macos, local), a card (a100, l4), or both, as os/card.`**
Neither a declared name nor a description this tool understands. The second half of
the message is the vocabulary: OS keywords, cards, or `os/card`.

**`host 'comfy-win': unknown field(s) 'gce_zoen' (did you mean 'gce_zone'?). Known fields: gce_instance, gce_project, gce_zone, gpu, kind, os, port.`**
A misspelt field in `hosts.toml`. This used to be reported as five *missing*
fields that were all present, because the required-field check ran first and the
typo was invisible to it — so the message described a file quite unlike the one in
front of you. Unknown fields are now checked first and the suggestion is offered.

## When the machine you want cannot start

A GPU stockout is routine, is nothing to do with your account, and is the moment a
tester usually gives up and opens the console. These lines are the tool trying to
keep you testing instead.

**`comfy-linux is untouched — you still have the machine you were on.`**
(or `comfy-linux, comfy-win are untouched — you still have the machines you were on.`)
Reassurance, printed when a `switch` fails: the target is brought up *before*
anything is stopped, so a failed switch leaves you exactly where you started. You
have lost nothing but the time.

**`Where you can test instead, easiest first:`**
The boxes that can run right now, same OS first, and a box in the same zone listed
last and labelled — it may hit the same shortage. Pick one and carry on.

**`No other machine is declared, so there is nowhere to switch to:`**
You have one box and it cannot start. The line under it —
`comfy-qat host discover   # declare a box you already have` — will pick up
anything already in your project; otherwise your options are to wait for capacity
or to move the box.

**`If it has to be comfy-win:`**
When only that machine will do — the install on it, the models on it — this is
followed by the `host move` command for a zone Google says has capacity. Read
[the move entries](#moving-a-box-to-a-zone-with-capacity) first: the suggested zone
can be stale by the time you use it, and a move that fails late leaves a disk and a
snapshot behind that you will pay for.

**`could not clean up: ...`**
`--clean` asked Google to delete a disk or a snapshot an earlier move left, and
Google refused. The nested message says why — most often the disk is still
attached to something, or your account lacks the delete permission. Nothing else
was touched. Each leftover's own `gcloud ... delete` command is printed above the
error, so you can run the one that failed yourself and see the full reason.

**`this run left disk comfy-win-b in us-central1-b (300 GB), and it is billing`**
Printed under a failed move, once per thing the run created before it stopped. A
part-finished move is the expensive failure here: the snapshot and the disk are
the slow, costly half and the instance is the useful half, so stopping in between
leaves you paying for work you cannot use. Each line is followed by the exact
delete command. Running the move again is the supported recovery — it finds what
exists and carries on from there — so delete only if you have decided against it.

**`already on the project:`** followed by a disk or a snapshot
An earlier `host move` did not finish, and what it created is still there and still
billing. This is a report, not an error — the move carries on and reuses what it
can. Each line is followed by the exact `gcloud ... delete` command that removes it,
and `comfy-qat host move <name> --clean` removes them all and stops. Nothing is
deleted for you.

**`comfy-win-a-b already exists in us-central1-b, but ...`**
A disk is sitting where the move wants to put one, and it could not be confirmed as
a copy of this box's boot disk — it is attached to something, it came from a
different snapshot, it is a different size, or it is older than the snapshot it
claims to come from. Carrying on with it would boot the wrong machine and look like
a move that worked, so the move stops. Check it is not something you want, then run
the printed delete command and move again.

**`us-central1-b does not offer g2-standard-8 at all`**
The zone does not have that machine type, so the instance could never be created
there. Nothing has been snapshotted. Pick a zone that does:
`gcloud compute machine-types list --filter='name=g2-standard-8'`.

**`us-central1-b has no L4 capacity either`**
The move got as far as creating the machine and the destination zone was out of
capacity by the time it got there. The zone Google names in a stockout message is
where there was capacity when it answered, not a reservation, and it goes stale.
The snapshot and the disk both exist and are billing; the error names them. Trying
another zone reuses the snapshot, so it repeats only the disk, not the slow 300 GB
copy. There is no way to check a zone's free capacity in advance — Google publishes
no API for it — so this failure can only be reported well, not prevented.

**`the move stopped at: ...`**
Some other step failed. The error names what now exists because of the run, and the
original box is untouched and still stopped in its old zone. Run the same command
again: it looks at the project first and carries on from where it stopped.

**`could not delete the snapshot ...`** after a move otherwise succeeded
The new machine is up and in your host list; only the cleanup failed. The snapshot
is still billing and the message repeats the command that removes it.


## Stamping a machine

**`nothing answered at http://127.0.0.1:8190`**
Nothing is listening on that port. For a local host, ComfyUI is not running. For a
cloud host, either the box is off or the tunnel is not up. Check the port in your
host list matches what the machine actually serves.

**`http://127.0.0.1:8190 answered, but not with ComfyUI's /system_stats. Something
else is on that port, and a stamp from it would name the wrong machine.`**
Something is on that port, but it is not ComfyUI — a dev server, or another tunnel.
A JSON body is not enough on its own: a health endpoint answering `{"ok": true}`
would otherwise stamp cleanly as the bare host name, which reads like a successful
probe and says nothing true about any machine. So the body has to carry
`/system_stats`'s own field names before anything is recorded from it. This is
exactly the mix-up the port rules exist to prevent, so it is worth chasing rather
than working around.

**`http://127.0.0.1:8190 redirected to 127.0.0.1:8188 — that is a different
machine, so anything it says would be recorded under the wrong name.`**
Whatever holds the port answered with a redirect, and following it would have
stamped a different machine under this host's name — on this Mac, usually the local
ComfyUI on 8188. ComfyUI does not redirect `/system_stats`, so something else is on
that port: a proxy, a dev server, or a tunnel pointing somewhere you did not mean.
Check what is bound to the port before trusting any line from it.

**`http://127.0.0.1:8190 answered with more than 1024KB, which /system_stats never
does`**
That endpoint is a few hundred bytes. A body this size means something else is on
the port — often one that will stream for as long as you keep reading, since the
timeout covers each read and not the total. Find out what is holding the port.

**`http://127.0.0.1:8190 stopped answering part-way through: ...`**
The connection opened and then broke mid-answer — a tunnel that dropped, a box that
went away, or a read that timed out. Different from "nothing answered": something
was there. Check the tunnel is still open, then run the stamp again.

**`'127.0.0.1:8190' is not a URL this can ask: unknown url type`**
The host's url is missing its scheme, or is not a url at all. Write it in full, as
in `http://127.0.0.1:8188` — a bare `host:port` cannot be fetched.

**`comfy-win is declared as Windows Server 2022, but http://127.0.0.1:8190
answered as darwin. That port is not reaching comfy-win.`** — or the same about a
card: **`comfy-win is declared with a L4, but http://127.0.0.1:8190 answered with
mps (32GB). That port is not reaching comfy-win.`**
The machine that answered contradicts the machine your host list declares, so the
port is not reaching the box you named. Cards are compared as whole words, not as
text: `A100-80GB` and `NVIDIA A100-SXM4-80GB` are Google's name and ComfyUI's
name for one card and do not contradict, while `L4` against an `L40S` does. A
declaration that is merely less specific than the answer — `A100` against an
`A100-SXM4-80GB` — is accepted, so this will not catch a box that has the right
card with the wrong amount of VRAM; that is a real thing to notice and not a
reason to withhold an evidence line. The usual causes are a tunnel left open
to a different machine, a port that your local ComfyUI is holding, or a host
entry that was never repointed after a `host move`. `comfy-qat host list` shows
what is tunnelled; `comfy-qat host down comfy-win` then `comfy-qat host open
comfy-win` rebuilds the tunnel.

**`No evidence line was printed, because this one would have named the wrong
machine. Check the port in your host list and which tunnel is open, then stamp it
again.`**
Follows the message above. The stamp is refused rather than printed with a
warning attached, and that is deliberate: this line exists to be copied into a
bug report as proof of which machine produced a result, and a warning on stderr
does not survive being copied. Printing the line at all is what would create the
false evidence. `--json` is refused on the same contradiction, for the same
reason.

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

**`--json and --evidence produce different output; pick one`**
Two answers to the same question. Asking for both used to silently discard one.

**`note: --flags only affects the evidence block; add --evidence <env> to see it`**
Not an error — `--flags` names the flags called out in an evidence block, so on its
own it changes nothing you can see.

## Google Cloud

Sessions expiring mid-pass have their own page:
[session-expiry.md](session-expiry.md).

**`gcloud is not installed or not on PATH.`**
Install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install

**`your gcloud session has expired`**
Google asked for a fresh proof of identity — a reauth challenge — and nothing could
ask you for it. This is a Workspace session-length policy, not a fault in your
account, and it is the most common failure this tool has. Run `gcloud auth login`,
then carry on where you left off. If it keeps happening mid-pass,
[session-expiry.md](session-expiry.md) explains why and what the org can change.

**`gcloud could not refresh your sign-in`**
The stored credential could not be exchanged for a token, and it is not a reauth
challenge — usually a sign-in that was revoked, or an account removed from the
project. `gcloud auth login` fixes it. If it fails again immediately, the account
itself is the problem.

**`could not reach Google Cloud`**
gcloud never got to Google. This is a network failure wearing an authentication
failure's clothing: gcloud reports a token refresh it could not complete, which
used to be printed as an expired session. Signing in again will not help. Check
your connection — including a VPN or proxy that may have dropped — and try again.

**`Required 'compute.instances.start' permission for ...`**
Your sign-in worked and Google refused the action: the account is missing an IAM
role, not a credential. The message names the exact permission. `comfy-qat auth
status` shows which account you are actually using — being signed in as the wrong
one of two accounts is the usual cause.

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

**`a project-wide allowance only, no specific card granted`**
`GPUS-ALL-REGIONS-per-project` is a ceiling on how many GPUs you may run in total.
It is not permission to run any particular card, and on its own it starts nothing —
which is why this reads as a failure rather than a pass. Ask for an actual card:
```sh
comfy-qat auth quota request --gpu l4 --region us-central1
```

**`this project reports no quota for 'l4' in europe-west4. It is metered in us-central1. Available: L4`**
You have that card, somewhere else. Quota is granted per region, so an L4 approved
in `us-central1` does nothing for a box in `europe-west4`. Either build in the
region that has it, or request it where you want it. The older wording said
`no quota for 'l4' … Available: L4`, which read as a contradiction; the region is
the missing half.

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
