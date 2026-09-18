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

**no host list at ~/.config/comfy-qa-tools/hosts.toml. Run `comfy-qat init`
to write a starter one.**
You have not set one up yet. `comfy-qat setup` writes one too, along with
everything else a first run needs.

**`~/.config/comfy-qa-tools/hosts.toml already exists`** / **`to fix: --force
overwrites it`**
`comfy-qat init` will not write over a host list you have edited. If you really do want
the starter file back, `comfy-qat init --force` — and copy your cloud hosts
out first, because they are not merged back in.

**`no [hosts.<name>] tables found — the file parses, and declares no machines. Every host is a table named for it, like [hosts.local].`**
The file parses as TOML but declares no machines. Every host is a table named for
it, `[hosts.local]`, and an empty host list is treated as a mistake rather than as
"no machines", because a tool that silently operates nothing is worse than one
that stops.

There is a second way to arrive here, and it is the one that looks like a bug.
`delete` will not remove your last cloud host by writing a list this parser
cannot read — `hostfile` validates the rewrite with this same function and
refuses, leaving the entry and saying so. That is the right refusal: an
unloadable host list is worse than a stale entry. Take the table out by hand.

This message deliberately suggests nothing that overwrites the file. It is quoted
verbatim into the rewrite's own refusal, and at that moment the file is still
intact and still holds your comments — so advice that was safe for an empty file
would have been destructive here. `init --force` exists if you want it, and
documents itself.

**`~/.config/comfy-qa-tools/hosts.toml is not valid TOML: ...`**
A syntax error, quoted from the parser with the line it failed on. The usual cause
is an unquoted string — every value except `port` needs quotes.

**`host 'comfy-win': expected a table, got str`**
Something under `[hosts]` is a bare value rather than a table — usually
`hosts.comfy-win = "..."` where `[hosts.comfy-win]` was meant.

**`host 'comfy-win': kind must be 'local' or 'gce', got None`**
A host has a missing or misspelled `kind`. Those are the only two values.

**`host 'comfy-win': kind 'gce' requires gce_zone, gce_project`**
A cloud host is missing detail needed to locate it in Google Cloud. All of `os`,
`gpu`, `gce_instance`, `gce_zone` and `gce_project` are required.

**`host 'comfy-win': os 'Windwos Server 2022' looks like a misspelling of 'windows'. The os field decides which commands this box is sent — anything not recognised as Windows is given the Linux command set, and 'ssh' and 'rdp' swap over with it. Fix the spelling, or use a name this tool does not recognise at all if the box really is something else.`**
The `os` field is not a label. Which of two command sets a box gets is decided by
whether `os` is recognised as Windows, so a Windows box spelled `Windwos` is
handed the Linux set — `cd /opt/comfyui`, `apt-get`, bash — and `ssh` and `rdp`
swap over, leaving the one command that can reach it refusing to. None of that
prints a word, which is why it is refused at load instead.

Fix the spelling. An `os` this tool has never heard of is fine and passes
untouched — `sles-15`, `unknown`, and whatever `discover` reads off a licence it
does not recognise are all allowed, because a host list the tool will not read is
a machine nobody can stop. Only a near-miss of a name it acts on is rejected.

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
copied entry someone forgot to repoint after `comfy-qat move`.

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
This one costs money. `comfy-qat down` decides what to stop from `kind` alone: for a
local host it reports "local ComfyUI left running" and returns without stopping
anything. A cloud box mistyped as `local` — or edited down to one after a move —
therefore reads as a successful `comfy-qat down` while the GPU bills all night. The
contradiction is refused when the host list is read, so `down` can never be
handed one.

**`host 'local': the name 'local' is reserved for the ComfyUI on this computer,
which is what every example and the starter host list means by it. A cloud box
wearing it puts an invisible default back. Rename the box, e.g. comfy-win or
comfy-linux.`**
`comfy-qat stamp local` has one obvious meaning, and the starter host list
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
unrecognisable, `comfy-qat init --force` writes a fresh starter one, and you
will have to re-add your cloud hosts.

**`could not write a host list to /somewhere/hosts.toml: [Errno 13] Permission
denied`** / **`to fix: give --config a path you can write to — the file itself,
not the folder it goes in`**
`comfy-qat init` could not create the file. Either the folder is not writable, or
`--config` was pointed at a directory. It names the path it tried.

## Entries for machines that no longer exist

Boxes disappear without this tool: deleted in the console, deleted by a
colleague, deleted with raw `gcloud`, or left behind under a new name by a
`move`. The entry stays, and it is not harmless — `create` refuses a name an
entry holds, ports are handed out from the same list, and `comfy-qat go linux`
refuses as ambiguous once several of the machines it matches do not exist.

`comfy-qat list --live` names them: the STATE column reads **`not on the
project`** for a box Google was asked about and does not have, which is a
different answer from `unknown` — that one means the read failed and nobody
knows. `comfy-qat discover --prune` removes the first kind and never the second.
It names every entry before it removes anything and asks first; `--yes` skips the
question for scripts, and `--dry-run` shows what would go without writing.

```sh
comfy-qat discover --prune
```

**Nothing is removed on one read.** A name failing to appear in the project's
instance listing is an inference, and it is only ever as complete as that listing
was. So every entry the listing calls absent is then put to a
`gcloud compute instances describe` of its own, by name and in its own zone, and
removed only if Google answers that there is no such instance — a statement about
that machine rather than about a set it did not turn up in. You will see the
count before the checks start:

```
checking 2 entries against the project one at a time, to be sure before removing anything
```

That is one `gcloud` process per stale entry, so a `--prune` that finds five
takes a few seconds longer than one that finds none. A host list with nothing
stale in it pays nothing — no candidates, no extra calls.

**`the project listing did not have these and a direct check did not confirm they are gone, so nothing here was removed`**
The two reads disagreed, so the entry stays. There are two ways to get here and
the line under the message says which.

*Google described it after all.* The listing did not carry the machine and a
direct read does, which means the listing came back short of what the project
holds. The machine is there, it may well be billing, and this entry is the only
thing in your host list naming it — so removing it would be the worst available
outcome. Nothing needs fixing in the file; run `comfy-qat discover --prune` again
and the listing will usually be complete.

*The check could not be made.* The message carries `gcloud`'s own refusal — a
denied permission, a timeout, no network. Nobody established anything, so nothing
was removed. Note that a resource you may not see is refused with a *permission*
error rather than a "not found", which is why a narrowed role or a
service-account credential lands here and never in the removed list. Fix the
access and run it again.

**`does not exist, which is not a statement about the instance inside it`**
A special case of the one above, and the one that matters most, so it says which
resource Google actually answered about.

`describe` reports not-found about whatever it could not reach, and only one of
those three answers is about the machine:

```
'projects/p/zones/z/instances/n' was not found   <- the instance. This one prunes.
'projects/p/zones/z'             was not found   <- the zone
'projects/p'                     was not found   <- the project
```

An entry is removed only on the first. If you see this message, the `gce_project`
or the `gce_zone` on that entry names something Google does not have — almost
always a typo, since both are typed by hand into a hand-maintained file. The
machine itself has not been ruled out and may be running perfectly well on the
project you meant.

Nothing was removed, and that restraint is the point: read the other way, one
mistyped `gce_project` turns *every* entry naming it into a confirmed ghost in a
single pass, and one `y` then deletes the only record of machines that are still
billing. Fix the field and run it again.

```sh
comfy-qat list          # what your host list says
gcloud projects list    # what you actually have
```

**`could not be listed, so nothing was checked on it — any entry naming it was left alone`**
One of the projects your host list names could not be listed, so nothing on it
was compared and nothing on it was removed. A listing that fails is not a listing
that came back empty: an entry taken out on that basis would be an entry
destroyed because the network was down. Fix the access — usually
`gcloud auth login`, or a project you no longer have rights on — and run it
again. The other projects were still checked.

**`on the project, and not in the zone the entry gives — the entry is wrong, the box is not gone, so nothing here was removed. Find where each one really is with gcloud compute instances list, then correct gce_zone by hand`**
The project has a machine of that name, in a zone your entry does not name. That
is a wrong entry, not a missing box, and the two are worlds apart: the machine
exists, is very likely running and billing, and this entry is the only thing in
your host list that names it — so removing it would leave a GPU running that
`comfy-qat down` can no longer reach. Nothing here is removed for that reason.

It happens when the zone was typed by hand, when the box was recreated somewhere
else from the console, or when a `move` did not finish. The repair is one field:
find the zone the box is really in, then edit `gce_zone` in `hosts.toml`.

```sh
gcloud compute instances list --filter="name=comfy-win"
```

Ghosts are still pruned in the same run; these are listed separately because they
are a different fact.

**`gcloud listed the instances on <project> and printed nothing at all, so what that project holds was not established`**
`gcloud` exited successfully and wrote no output, which is not the same as a
project with no instances — an empty project prints `[]`. Nothing prints nothing,
so what that project holds is unknown, and unknown is not "empty". Every entry
naming that project is left exactly where it is.

Run the listing yourself and see what it does. If it prints instances when you
run it, it was a transient failure and `comfy-qat discover --prune` will work on
the next attempt.

```sh
gcloud compute instances list --project=<project>
```

**`the entries could not be removed`** / **`take them out of the host list by hand — while they are there, `create` refuses those names, their ports stay reserved, and a description that matches several of them refuses as ambiguous`**
The boxes really are gone and the host list could not be rewritten. The message
after the colon says why — most often the file is read-only or on a full disk.
Nothing was half-written: the rewrite is validated and replaced atomically, and a
copy stays beside it as `hosts.toml.bak`. Fix the file's permissions and run
`comfy-qat discover --prune` again, or delete the `[hosts.<name>]` blocks
yourself.

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

**`could not read GPU quota (...). Check later: comfy-qat quota`**
Setup carries on regardless — quota can take days to change and is never a reason
to strand you mid-setup. This is a warning, not a stop; setup still writes your
host list. Check quota afterwards with the command it names.

**`the request was refused: ...`**
Setup offered to ask Google for GPU quota and Google turned the request down on
submission. The most common cause is an account with no billing history — quota is
frequently withheld until a project has been billed at least once. Setup continues;
see [cost.md](cost.md) and ask again later.

**`could not read this project's existing quota requests: ...`**
`comfy-qat quota request` stops rather than guessing. The list of requests already
on the project is what says which preference id a given quota and set of
dimensions already holds — and Google refuses a *new* id for a pair that has one,
while a request filed under the wrong id is permanent, because a quota preference
cannot be deleted. So a failed read is a refusal, not a best effort. `--dry-run`
still works and asks Google for nothing. The usual cause is the same timeout that
affects `quotas info list`; try again.

**`could not read existing quota requests (...), so nothing was asked for — a
second request cannot be ruled out without them`**
Setup asks Google for the GPU quota your project is missing, and the list of
requests you have already made is the only thing that stops it asking twice. A
quota request cannot be withdrawn and may be read by a person at Google, so when
that list cannot be read setup refuses to guess rather than making a best effort.
Nothing was sent and nothing is wrong with your project; the usual cause is the
same timeout that affects `quotas info list`. Run setup again, or ask by hand with
`comfy-qat quota request --gpu l4,t4`.

**`<card>: already requested, nothing sent`**
Not a failure. A quota preference is keyed on its quota id *and* its dimensions,
and Google refuses either a new preference id for a pair that already has one, or
an update whose dimensions differ from the existing preference's. Both mean the
same thing to you: this card has already been asked for. Watch it with
`comfy-qat quota`, or on the console link setup prints.

**`quota requests skipped (--no-quota-request). To ask later: comfy-qat
quota request --gpu l4,t4`**
You passed `--no-quota-request`, so setup reported what your project holds and
asked Google for nothing. Ask later with the command it names.

**`... is 0 and <machine-type> needs <n> vCPU, so this card cannot start even with GPU quota`**
Only ever said about **N1** cards — T4, V100, P100, P4, K80 — which consume the
generic `CPUS-per-project-region`. Every other family this tool orders (A2, A3,
G2, G4) needs no CPU quota at all, so a zero `A2-CPUS-per-project-region` is not
why an A100 will not start, however much it resembles a cause. Raise the generic
pool for the region you are working in.

**`<card>: refusing to lower the standing request to <n>`**
You asked for fewer than a request already with Google. Because a quota
preference is addressed by its own id, sending a smaller number is an **update**
— it replaces the standing request rather than sitting beside it. Google treats
the two directions differently (a decrease needs no contact address and gets no
trace id, and carries its own safety checks), so a smaller number is not simply a
slower version of a bigger one. So `comfy-qat quota request
--gpu h100` with no `--value` will never lower a request `setup` filed at 8; a
number you type and confirm with `--allow-lower` will. The refusal prints both
commands.

**`no region called '<r>' appears in this project's quota`**
`comfy-qat quota --region` used to accept a typo and print a nearly empty table,
which reads as "this project has almost no quota" rather than "you misspelled the
region". It now refuses, the way `comfy-qat quota request` already did for the
same flag. `comfy-qat quota` with no region lists every region the project is
metered in.

**`this project has no <card> quota in <region>`**
The card is metered on this project, just not where you asked. The fix names a
region that does have it, and `--by-region` lists the rest. This used to say "it
is metered in all regions, us-central1" — two API dimension entries printed as
prose, reading as a two-item region list — and then offered a list of cards when
the region was the problem.

**`refusing to set <quota> to 0, releasing the <n> this project holds`**
`--value 0` gives the quota up rather than asking for less of it, and nothing in
this tool can undo that: `gcloud quotas preferences` exposes create, describe,
list and update, and no delete verb at all. It needs
`--release-quota` as well as `--allow-lower`: two flags, because `--allow-lower`
is a thing somebody may reasonably keep in a script for a legitimate reduction,
and a `0` typed by accident beside it would destroy a working grant. Nothing in
QA needs this. The refusal names what would have disappeared.

**`keeping the quota this project holds <quota> at <n>; <m> would lower it`**
The floor is the larger of two things: the request already with Google, and the
quota the project actually HOLDS. A card can hold quota with no request behind it
at all — T4, L4, K80, P100, P4 and V100 are all like that here — so a guard that
only compared against standing requests left the cards that work today
unprotected. Pass `--allow-lower` to go below it, or `--value <n>` to keep it.

**`<n> is a lot: <m> is the most any machine this tool creates takes. Asking for headroom is fine; a typo is not, and Google reads the number`**
A warning, not a refusal. Asking for more quota than one machine needs is
legitimate — headroom, or several boxes — but `--value 999999` used to go through
in silence, and the number reaches a human reviewer. The bound comes from the
card table, so it moves when the table does.

**`<card> is a real card and this tool has no machine type for it, so it cannot create one. This tool can create: ...`**
Not a typo, which is what "no card called '<card>'" used to say about it while
`comfy-qat quota list` printed a row for the same card in the same minute. B200,
H200, H100-MEGA and RTX PRO 6000 are metered by this project and have no machine
type in this tool's table — the machine type and card count cannot be guessed
without creating an instance. `quota list` shows what you hold; `create` will not
order them.

**`<card>: Google has already refused this (<preference id>). Asking again changes nothing unless what a reviewer reads has changed — on a new project that is billing history, not the wording`**
A warning, not a refusal — you typed the command, so it runs. `setup` declines to
re-file a refused request automatically and says "Not asked again automatically";
this is the manual command, and there has to be a way to ask again once something
has changed. On a new project what changes the answer is billing history, not the
wording of the justification: six of seven requests on the test project came back
refused automatically, in under three seconds, with nothing read.

**`no such region '<region>' — this project's quota names <n>, and that is not one of them`**
A typo, caught before it can turn into a statement about quota. This used to
report "this project has no l4 quota in not-a-region", which says the region is
real and the quota is missing — both halves false — and then recommended a region
from the same bad path. `comfy-qat quota list --by-region` lists the real ones.

**`<id> meters several cards and names which one in a dimension, so a raw id cannot say which you mean`**
`GPUS-PER-GPU-FAMILY-per-project-region` meters H100, H100-MEGA, H200, B200 and
RTX PRO 6000 on this project and tells them apart by a `gpu_family` dimension, so
a raw `--quota-id` cannot say which card you mean. The tool used to file it
anyway, with `region` alone — a preference for no card in particular, permanent,
and invisible to the guard that stops a request being lowered. Use
`--gpu <card>`, which resolves both dimensions.

**`<card>: <region> does not offer this card`**
Quota and availability are different questions, and this one is availability. The
project may well be metered for the card in that region, but Google sells no such
accelerator there, so a granted request would buy a box that can never start —
and a quota preference cannot be deleted once filed. Nine of the forty-three
regions in the quota universe stock no NVIDIA accelerator of any kind, among them
africa-south1, which sorts first alphabetically and was for a while what the tool
recommended. The fix line names a region that does stock the card.

**`<card>: Google sells it in <n> regions, none of them metered by this project`**
The card exists in Google's catalogue but nowhere this project holds quota for
it, so there is no region where a request would give you something you can start
today. `comfy-qat quota list --by-region` shows what the project meters, and
`comfy-qat quota list --region <region>` what a region offers.

**`<card>: this project meters it in <n> regions that do`**
The tail of the message above: the card is metered somewhere you can actually use
it. `comfy-qat quota list --by-region` lists those regions, and
`comfy-qat quota list --region <region>` says what any one region offers.

**`<card>: whether <region> offers this card was not checked`**
Not a refusal and not a verdict. Either `gcloud compute accelerator-types list`
could not be read, or the card is one this tool has no accelerator id for — so
the availability question was not answered either way and the request went ahead.
"I could not look" is not "it is not there", and the tool will not turn one into
the other; `comfy-qat quota list --region <region>` reports the same distinction
as "not checked" rather than as absence.

**`<id> is not a GPU quota this project reports — `quota request` asks for GPU quota only`**
`--quota-id` takes a raw Google quota id, and this command reads only the GPU
ones. It used to say "this project reports no quota called '<id>'", which was
false of every CPU, disk and storage quota the project genuinely holds — the list
it checks against is already filtered, so the sentence described the filter rather
than the project. Run `comfy-qat quota` to see the ids this command can ask for,
or use `--gpu <card>` and let the tool resolve it.

**`<card>: not creatable by this tool, whatever quota it holds`**
Your project meters that card and `comfy-qat quota` shows the row, but
`comfy-qat create` has no machine type for it, so a quota request would buy
something unusable. The message lists the cards `create` does accept. See the
`quota list` note beside the card for what your project already holds.

**`denied — Google refused this; asking again will not help`**
`comfy-qat quota` says this instead of "none — request it" when a request for
that card is on the project and was refused. It is the honest answer: the tool
will not re-file it, and neither should you without changing something a
reviewer can act on. On a new project with little billing history, refusals
arrive automatically within seconds and no justification is read — so the thing
that changes the outcome is usage history, not a better-worded request.

**`ready — <n> granted; a raise to <m> was not`**
You hold `n` and can use it. A request to raise the limit to `m` was refused or
trimmed, which is worth seeing before deciding whether to ask again — the
allowance is real, and so is the ceiling on it.

**`this project meters no <card> quota in <region>, so there is nothing to ask for there`**
Not every project meters every card in every region. Two shapes exist: per-card
ids like `NVIDIA-L4-GPUS-per-project-region`, and a shared family quota,
`GPUS-PER-GPU-FAMILY-per-project-region`, where the card is a `gpu_family`
dimension. Newer cards use the second. If a project reports neither for a card in
the region being planned, there is nothing to raise there. Setup reports it and
carries on.

**THE REGION IS IN THE SENTENCE** because the check is per region and the claim
used to be about the whole project — "this project does not meter L4 quota",
printed about a card granted at 1 across forty-three regions, whenever the
planning region had no row of its own. `comfy-qat quota list --by-region` shows
where the card IS metered.

**`<id> is metered per region, so a request for it has to name one`**
An id ending `-per-project-region` defines a region dimension, and Google
requires every defined dimension to be set — a real submission without one came
back `INVALID_ARGUMENT: Dimension values must be set for all the dimensions`. The
tool used to file it anyway, permanently, on the reasoning that naming a region
you did not type would be inventing one. That is true, and refusing is the third
option. Add `--region`, or use `--gpu <card>`, which resolves the dimensions for
you.

**`if Google refuses this as too large a decrease, it wants --allow-high-percentage-quota-decrease, and --allow-quota-decrease-below-usage if the quota is in use. This tool does not add either for you`**
Printed on the `--release-quota` path, and INFERRED rather than verified —
confirming it would mean filing an irrevocable request. Both flags exist on
`gcloud quotas preferences update`, and 1 → 0 is a 100% decrease, so Google may
well refuse the command this tool prints. The tool does not add them for you:
they override Google's own safety checks on the one path here that destroys
something, and the friction is the point. Add them yourself if you mean to.

**`--region <region> ignored: <id> is not metered per region, so a request for it carries no dimensions`**
`GPUS-ALL-REGIONS-per-project` is genuinely global — it defines no dimensions and
a request for it must carry none, so a `--region` alongside it has nowhere to go.
Not an error: the request is correct and goes ahead. It is said out loud because
this command refuses four other kinds of wrong region and staying mute about an
ignored one is the odd behaviour.

**`<name> is what `quota list` calls this card; `--gpu` takes <key>`**
One card has three names and they are not interchangeable: `H100-80GB` is what
`quota list` shows, and `h100` is what `--gpu` takes. `quota request` used to
accept the display name while `create` refused it, so the two commands people use
together disagreed about one card. Both take the key now, and the refusal names
it rather than saying "no card called", which would be false of a card the tool
can plainly see.

**`--gpu and --quota-id both name what to ask for, and they disagree here, so this command cannot tell which you meant`**
Pass one or the other. This used to warn that `--gpu` was "ignored" and then file
requests for those cards anyway — three permanent preferences from one command,
two of them for cards it had just said it was ignoring, and one of those by
updating an existing granted preference. A warning that does not change behaviour
is worse than none, so the combination is refused instead.

**`'<zone>' is a zone; quota is metered per region`**
Every `create` and `gcloud` example names a zone, so a zone is the likeliest
wrong answer to `--region` — and it used to be ACCEPTED. `quota list --region
us-central1-a` printed an empty table at exit 0 for a project holding six cards,
which is worse than a refusal because a script believes it. The tool names the
region the zone is in rather than silently reinterpreting the input.

**`no region called '<region>' — regions are lower case`**
A region wrong by nothing but case. It used to fall to the generic advice, while
a zone name — wrong by a whole segment — got an exact suggestion, because the
nearest-match comparison was case-sensitive.

**`--region was given but empty — if that came from a shell variable, it is unset`**
`--region ""` used to behave exactly like leaving the flag off, deriving a region
and, on a real run, filing an irrevocable request into it. Somebody who typed the
flag has said they care which region. Leave the flag off entirely to have one
derived.

**`Google already refused <card> in <region>, so asking there again will not help`**
`create` used to answer a zero-quota card with "ask, then wait for Google" while
`comfy-qat quota list` said "asking again will not help" about the same card in
the same minute — and the command it printed derived the refused region. A
refusal somewhere is not a refusal everywhere, so the remedy is a different
region; `quota list --by-region` shows where the card is metered.

**`NOTE: <card> needs <n> of this ceiling and it is <m>`**
`GPUS-ALL-REGIONS` caps every card put together. An `a3-highgpu-8g` is eight GPUs,
so a granted H100 request under a ceiling of 1 still cannot start a machine. The
sentence existed but was attached only to the branch that asks for a ceiling
raise — so on a project whose raise was REFUSED, the case that will not fix itself
on the next run, it never printed.

**`no such region '<region>'`** (from `setup`)
The same check `quota list --region` and `quota request --region` make, which
`setup` did not have: a typo used to plan an irrevocable request into a region
that does not exist, and report four granted cards as unmetered in the same
breath. A region counts as real if this project's quota names it or Google sells
any accelerator there.

**`<region> does not offer <cards> — a granted request there buys a box that can never start, and a quota preference cannot be withdrawn`** (from `setup`)
Also new to `setup`, and `quota request` has refused it since the fourth pass: a
granted request in a region that sells no such card buys a box that can never
start, and a quota preference cannot be withdrawn. `comfy-qat quota list --region
<region>` says what a region actually offers.

**`Google refused an earlier request`**
A quota preference is permanent — it cannot be deleted, only lowered — so a
refusal stays on the project and setup can see it. It is reported with the region
it was refused in and the preference id, and setup does **not** re-file it: asking
a human reviewer the same question on every run is how a project gets ignored. Ask
again deliberately with `comfy-qat quota request --gpu <card> --region <region>`,
ideally with a `--justification` saying what changed.

**`could not list cloud boxes (...). Add them by hand if needed.`**
Discovery failed, so nothing was added. Setup finishes anyway; add hosts by hand
from [hosts.md](hosts.md), or run `comfy-qat discover` later.

**`could not read your host list (...), so nothing was added to it`**
Discovery found cloud boxes but your `hosts.toml` will not parse, so setup left it
completely alone rather than appending to a file it cannot read. That restraint is
deliberate: appending to a broken list would add a second `[hosts.<name>]` table
for a box already declared, and a duplicate table is not valid TOML — one fixable
mistake would become a file nothing can load, on the one command that promises to
change nothing. The message carries the parse error; fix that in the file, then run
`comfy-qat discover`.

**`your host list could not be updated (...), so nothing was added to it`**
Your `hosts.toml` reads fine, and the file that appending the discovered boxes
would have produced does not — so it was not written, and the file on disk is
untouched. Discovery writes each block under the name Google has for the instance,
and the loader refuses a list where two names differ only in case or where two
entries name one machine, so an entry of your own can collide with a name
discovery is about to add. The message carries the loader's own refusal, which
names both sides of the collision; rename or repoint your entry, then run
`comfy-qat discover`.

## Creating a box

`comfy-qat create` makes the machine, choosing the zone for you. Nothing here is
created until the plan and the quota have been printed and agreed to, and
`--dry-run` stops before any of it.

The card is the only real decision. **The machine type follows from it** — an L4
is the G2 family with the GPU built into the machine type, a T4 or P100 or V100
is N1 with a card attached — and getting that the wrong way round is the single
most common way a create by hand fails. You never pass a machine type here.

### Before anything exists

**`no card called 'rtx4090'. This tool can create: a100, a100-80gb, h100, l4, t4.`**
A card this tool has no machine-type mapping for. The list is what it can order,
not what your project is allowed — `comfy-qat quota list` is the second half
of the answer. It is also shorter than the table inside the tool: four more cards
are known and deliberately not offered, for the reason in the next entry.

**`this tool cannot bring up a P100, so nothing was created. The driver it installs is the open NVIDIA kernel module, and that needs a GPU System Processor — a GSP — which only Turing and newer cards have. Pascal has none, so no module loads at all: the box would boot, bill, and never see its own GPU. Cards that do work: a100, a100-80gb, h100, l4, t4.`**
P4, P100, V100 and K80 are real cards, your project may hold real quota for them,
and this tool will not order one. Every Linux box it creates installs its driver
through Google's `cuda_installer.pyz`, which lays down the **open** NVIDIA kernel
module, and the open module needs a GPU System Processor — the on-board
microcontroller NVIDIA introduced with Turing. Kepler, Pascal and Volta do not
have one and cannot grow one.

Nothing about the failure looks like a failure, which is why this is refused
rather than warned about. Measured on a V100 box on 2026-09-09: `create` returned
0, the startup script finished with exit status 0 and the packages installed
(`nvidia-open set on hold`), the machine booted, and `lspci` showed the card.
No kernel module loaded, `nvidia-smi` failed, and a clean reboot did not change
it. `dmesg` is the only place it says so:

```
NVRM: The NVIDIA GPU 0000:00:04.0 (PCI ID: 10de:1db1)
NVRM: installed in this system is not supported by open
NVRM: nvidia.ko because it does not include the required GPU
NVRM: System Processor (GSP).
NVRM: The NVIDIA probe routine failed for 1 device(s).
```

A T4 is the same `n1-standard-8` machine as a P4, P100 or V100, costs less than
most of them, and works — `comfy-qat create --os linux --gpu t4`.

**`no operating system called 'freebsd'. Say --os linux or --os windows.`**
Two images, one per operating system: Ubuntu 22.04 and Windows Server 2022.
`ubuntu`, `debian` and `win` are accepted spellings of those two.

**`comfy-win is already taken — a host list entry or an instance on this project has that name. Pick another with --name.`**
Names are checked against your host list *and* against the instances on the
project, because either collision ends the same way: two machines you cannot tell
apart. Without `--name` a free one is chosen for you.

**`could not read your host list (...), so nothing was created`**
Your `hosts.toml` exists and will not parse, so nothing was created — and that
refusal is the point, because this is the command that spends money. `create`
reads the host list twice: once to check the name is free, and once to pick a
port. Treating an unreadable file as an empty one made both of those answers
meaningless, and the run went ahead anyway: the box was created and billed, its
block was appended to a file that still would not load, and the run signed off by
offering `comfy-qat go <name>` and `comfy-qat down <name>` — both of which read
the host list and exit 2. A GPU billing, no way to stop it with this tool, and
nothing in the run saying the file was broken.

Every way in is ordinary: a duplicate port, two entries naming one instance, two
names differing only in case, a typo in the TOML, a stray non-UTF-8 byte. The
message carries the loader's own refusal, which names both sides of the problem.
Fix the file, then run `create` again — nothing was created, so there is nothing
to clean up.

**`could not find an unused name starting comfy-linux. Give one with --name.`**
Ninety-eight boxes named `comfy-linux-2` through `comfy-linux-99` already exist,
which is not a situation this tool is going to guess its way out of.

**`'9lives' is not a name Compute Engine will take. A name starts with a letter, then letters, digits or hyphens, up to 63 characters, and does not end in a hyphen. Nothing was created.`**
Google's own rule for an instance name, checked here rather than at the create.
Spaces, underscores and capitals are fixed for you — `--name "My Box"` becomes
`my-box` — but a name that starts with a digit, ends in a hyphen or runs past 63
characters cannot be fixed without inventing one. Drop `--name` and one is
picked for you.

Checked here rather than left to `gcloud`, because by the time gcloud sees the
name this command has spent a minute reading quota, opened four latency probes
and asked you to confirm. Note that Python considers `é` and `ボ` alphanumeric
and Google does not, so `--name café` looks clean after tidying and is still
refused.

**`a 20 GB disk is too small — the image will not fit and models will not either. Ask for at least 50.`**
The Windows Server image alone does not fit below 50 GB. The default is 200,
which is room for a few checkpoints; `--disk 500` if you are testing something
large.

**`a 20000 GB disk is larger than anything this tool creates. The disk bills by the gigabyte provisioned, from the moment the box exists and whether or not anything is written to it, so a typo here is expensive and silent. Ask for at most 4000, or make a disk that size deliberately in the console.`**
`--disk` has no unit, so `20000` for `2000` is one keystroke and eighteen extra
terabytes — provisioned, billed, and not obviously wrong in any output. There is
a ceiling for the same reason there is a floor. If you genuinely want more than
4 TB, make that disk in the console where the price is on the screen.

### The quota gate

Both allowances are read before anything is created, and printed either way:

```
quota checked:
  L4: 1, in 43 region(s)
  GPUS_ALL_REGIONS (every card, project-wide): 1
  already running and holding 1 card of it: comfy-win
```

The third line only appears when something is already spending the ceiling, and
it counts **cards**, not boxes: one `a3-highgpu-8g` holds eight of it on its own.

**`this project has no L4 quota, so a L4 box cannot start anywhere. Nothing was created.`**
The card has never been granted. Ask for it and wait — Google's answer is
typically minutes to a couple of days:

```sh
comfy-qat quota request --gpu l4 --region us-central1
```

**`H100-80GB needs 8 of this project's GPU allowance and the grant is 1. Nothing was created.`**
Some machine types come as a fixed block of cards — the smallest H100 machine
type is eight of them — so the grant has to cover the whole block, not one card.

**`GPUS_ALL_REGIONS is 0 on this project — that is the ceiling across every card, whatever the L4 grant says, and 1 is needed. Nothing was created.`**
The project-wide ceiling across every GPU, and the one that most often actually
bites: it is **1** on the project this was built against. A per-card grant of 4
means nothing if this is 0. Raising it is a separate request from asking for a
card, made in the console.

**`GPUS_ALL_REGIONS is 1 and comfy-win is already running on it, so a new GPU box cannot start until that one stops. Nothing was created.`**
Not a quota you need to raise — a box you need to stop. The fix hands over
`gcloud compute instances stop <name> --zone=<zone>` rather than `comfy-qat
down`, because the name is a **GCE instance** name: the box holding the only
slot is usually one somebody started in the console, and `comfy-qat down`
resolves host-list names and never `gce_instance`, so it would refuse the
command it had just printed. Stopping it frees the allowance and the create then
goes through. This is checked before anything is made rather than being
discovered as a refusal afterwards.

**`GPUS_ALL_REGIONS is 8 and 2 GPU boxes are already running on it, holding 9 of it between them: comfy-win, comfy-h100. Nothing was created.`**
The same refusal with more than one box running, and the reason it counts cards
rather than boxes: an `a3-highgpu-8g` is eight of the ceiling on its own. Stop
whichever you are not using — again with the raw `gcloud compute instances
stop`, for the reason above — and run the create again. The count comes from
`acceleratorCount` on each running instance, so a box with no card at all does
not appear here however large it is.

**`GPUS_ALL_REGIONS is 1 on this project and comfy-win already holds 1 of it, so the 1-card box this move creates cannot start. Nothing was created.`**

The same ceiling, refused by `move` instead of by `create`. A move builds a
second GPU box while the first one still exists, so **a move of a RUNNING box
needs two of the allowance and a move of a stopped one needs one.** On a project
whose `GPUS_ALL_REGIONS` is 1 that is the difference between a move that works
and a move Google refuses — and it used to refuse it at the **instance create**,
which is the last and most expensive step, after the snapshot and a 300 GB disk
had been made and paid for. This is now checked before any of that.

Stopping the box you are moving is the fix, and it costs nothing: the move does
not need it running, and stopping it frees exactly the allowance the new one
needs. If something else is holding the ceiling you get the raw `gcloud compute
instances stop` for it instead, for the reason the entry above gives.

The check refuses only on arithmetic it is sure of. A ceiling the project does
not report, or one gcloud would not answer for, is "not read" rather than zero
and refuses nothing — so a move can still be refused by Google at the create, as
before. The quota itself is only read when something is actually holding a card,
because that read takes the best part of a minute and cannot change the answer
when nothing is held.

**`this project's L4 grant names no region, so there is nowhere to put the box. The grant itself is <n>. Nothing was created.`**

A quota can carry a limit and name no places. The API leaves the per-entry
`dimensions` null and puts the regions in `applicableLocations`, so a payload
that parses the number and not the locations lands here — and the grant size is
printed precisely so you can tell that apart from having no quota at all.

A grant exists but covers no named region, which is what an empty or malformed
quota record looks like. Ask for the card in a region by name.

**This is the last refusal `create` reaches, not the first.** If the ceiling is
also full you get the `GPUS_ALL_REGIONS` message above instead, and you will see
this one on the retry. That is deliberate: an empty region set is an absence and
can equally mean the payload was not read, while a full ceiling is a number that
was read and a box that was seen — and the ceiling refusal is the only one that
tells you a GPU box is running right now.

It was once claimed that this used to be reported as "this project has no L4
quota". It was not: the two conditions cannot both be true, so the order between
them changes nothing. Running the older code says so directly.

### Choosing the zone

You do not pass a zone. The order is worked out in four steps, and the first one
is the one that is easy to get wrong:

1. **Regions this project holds quota for that card in.** Quota is granted per
   region, so ranking by distance alone confidently picks a zone where nothing
   can start.
2. **Zones in those regions that offer the card *and* the machine type**, read
   from `accelerator-types list` and `machine-types list`.
3. **Ranked by latency measured from your machine**, not inferred from a map. One
   TCP connect to each candidate region's Compute Engine regional endpoint,
   cached for a week in `~/.config/comfy-qa-tools/zone-latency.json`. Delete that
   file to re-measure.
4. **Tried in order, falling through on a stockout.**

Step 3 is measured rather than assumed for a reason worth knowing: every
`<region>-<service>.googleapis.com` name resolves to the *same* anycast Google
front end, so timing those tells you the distance to your nearest Google edge and
nothing about the region. The regional endpoints
(`compute.<region>.rep.googleapis.com`) resolve per region and do not.

```
zone order — 6 to try, quota first, then what is offered, then measured latency (nearest: europe-west4)
  1. europe-west4-a  (208 ms to europe-west4)
  2. europe-west4-b  (208 ms to europe-west4)
  3. europe-west1-b  (219 ms to europe-west1)
```

**`nowhere to put comfy-linux: no region this project has L4 quota in offers g2-standard-8. Nothing was created.`**
Quota and availability do not overlap. The fix line prints the two commands that
show each half separately — where Google offers the card at all, and where you are
allowed to use it.

**`us-central1-f does not offer g2-standard-8, so a L4 box cannot be created there at all. Nothing was created.`**
You named a zone with `--zone` and that zone has never had that machine type.
`--zone` is an override for deliberately testing one zone, so it is one zone and
no fall-through; drop it and a working zone is chosen for you.

**`us-central1-f has never offered nvidia-tesla-t4, so a T4 box cannot be created there at all. Nothing was created.`**
The other half of the same check, and the half that usually catches it. Five of
the nine cards ride on `n1-standard-8`, which almost every zone on Earth offers,
so checking only the machine type would let a `--zone` that has never had that
card through to a create that takes a minute to fail. Both are checked.

With `--zone` the summary says so, rather than describing a ranking that did not
happen:

```
zone order — 1 to try, and it is the one you named with --zone: nothing was ranked or measured
  1. us-central1-f
```

**`note: the nearest region offers no nvidia-l4, so this looked further afield`** /
**`note: the 8 nearest regions offer no g2-standard-8, so this looked further afield`**
Not a failure. Only the nearest few regions get their zones looked up, because
asking `machine-types list` about a hundred and thirty zones is slow for an answer
whose first entries are the only ones ever used. When those few turn up nothing
the search widens, and the note says which half was missing — the card or the
machine type. They are different problems and used to print the same sentence.

**`this project has no L4 quota in me-west1, so nothing can start there. It holds L4 in us-central1, europe-west1, europe-west4 and 40 more. Nothing was created.`**
`--region` narrows the choice without naming a zone, and it can narrow it to
nothing. Drop `--region` and let this pick, or ask for the card there.

It carries the same two improvements as the `--zone` refusal below, for the same
reasons: it **names where the grant does apply**, so a mistyped region is its own
diagnosis at a glance — `me-west9` beside a list containing `me-west1` needs no
further explanation — and the fix line **leads with the cheap check**,
`gcloud compute regions list --filter=name=<region>`, before offering the slow
one. A quota request is days; a spelling check is seconds.

Note it is `regions list` here and `zones list` there. And as below, nothing
offline can tell a typo from a genuine gap: the quota payload lists where the
grant *applies*, not every region Google has, so a real region you hold no card
in is absent from it exactly as a misspelling would be.

**`this project has no L4 quota in me-west1, so nothing can start in me-west1-a. It holds L4 in us-central1, europe-west1, europe-west4 and 40 more. Nothing was created.`**
The same gate, reached through `--zone` instead of `--region`. A zone in a region
you hold no grant in cannot start the box, and this is a cheaper way to find that
out than a create that fails a minute later.

**It reads the same for a mistyped zone, and that is deliberate.** `--zone
us-central9-a` becomes `us-central9`, which this project genuinely holds no
quota in — the sentence is true either way, and nothing offline can tell a typo
from a real gap. There is no built-in list of Google's regions to check against
on purpose: Google adds regions, and a stale list would refuse a real one.

So the message names where the grant DOES apply, which settles it at a glance —
`us-central9` beside a list containing `us-central1` is its own diagnosis — and
the fix line puts `gcloud compute zones list --filter=name=<zone>` first,
because the cheap check should come before the slow one.

**Why the zone is not simply looked up first:** the two reads that would
diagnose it properly come later in the same function, and
`gcloud compute machine-types list --zones=<a zone that does not exist>` is
refused by gcloud on the argument. Moving them ahead of this would swap a clear
refusal for a raw gcloud error in exactly the case being diagnosed.

### While it is being created

Capacity is the one thing that cannot be checked in advance — Google publishes no
"is there room" endpoint — so this is try-and-see, and each attempt is announced
as it happens because a silent thirty-second pause reads as a hang.

```
  trying europe-west4-a…
  europe-west4-a has no L4 free right now
  Google suggests europe-west4-c
  trying europe-west4-c…
```

**`europe-west4-a has no L4 free right now`** / **`Google suggests europe-west4-c`**
Not failures. A stockout in one zone is routine and says nothing about your
account; a zone Google itself names in the refusal is moved to the front of what
is left, because that answer is fresher than anything measured beforehand.

**`Google suggests us-central1-c, which is outside the regions this is allowed to use — not trying it`**
Also not a failure. Google's suggestion is only ever taken inside the ordering
that was already chosen: inside `--region` when you gave one, inside the regions
this project holds quota in when you did not, and not at all when you gave
`--zone`, which means that zone or nothing. A suggestion outside those is a box
somewhere you did not choose, so it is reported and skipped rather than followed.

**`stopping after 6 zones — each attempt takes about a minute`**
The cap on attempts, and the only way to reach it is Google's own suggestions:
the ranked list is six zones long, and each stockout can add one more to the
front of the queue. Without the cap a chain of suggestions is a fall-through with
no end, on a command that is already slow. Run it again to try the rest.

**`every zone tried is out of L4 capacity: europe-west2-a, europe-west1-a, europe-west4-a, europe-west3-b, europe-west6-a, europe-north1-a. That is 6 of the 14 regions this project can use the card in, the nearest ones — not everywhere. Nothing was created and nothing is billing. Not tried, and possibly free: us-central1, us-east1, us-east4 and 5 more.`**
Six attempts is the whole budget — each one is a real create that takes most of a
minute to be refused — so a create looks at the nearest few regions and no
further. This is that message saying so. It is **not** "there is no L4 anywhere":
the regions it names at the end were never contacted, and one of them may have
had room the whole time. This wording exists because the version without the
middle two sentences was true and read as the opposite: six European zones
stocked out, the refusal sounded final, and `create --region us-central1`
succeeded on its second zone a minute later.

Take the fix line's `--region` and name one of the untried regions, or wait —
stockouts usually clear in minutes to hours. `comfy-qat quota list --by-region`
lists everywhere the grant reaches.

**`every zone tried is out of L4 capacity: us-central1-a, us-central1-b. That is every region this project can use the card in, so there is nowhere left to try right now. Nothing was created and nothing is billing.`**
The other half of the message above, and the difference is the whole point of
both. This one really does mean everywhere: every region where this project holds
the card's quota *and* Google offers the card was tried, so there is no `--region`
left to name. Nothing was made, so there is nothing to clean up and nothing to
stop. Wait, or use a card you also have quota for — `comfy-qat quota list`.

**`every zone tried is out of L4 capacity: us-central1-f. Nothing was created and nothing is billing.`**
The same refusal with no sentence about scope, which is what you get after
`--zone`. That flag means "this zone or nothing", nothing was ranked and no
region was measured, so this message deliberately says nothing about how much of
the world was considered — it was one zone, by request. Drop `--zone` and let the
tool pick, or wait for the stockout to clear.

**`stopped after 6 zones, all out of L4 capacity: europe-west4-a, europe-west4-b, europe-west1-b, europe-west1-c, us-central1-a, us-central1-b. Nothing was created and nothing is billing — this is a cap, not the whole world, so there may be room somewhere untried.`**
Different from the message above, and the difference matters. That one means
everywhere you may use the card is short. This one means the tool stopped
counting: every refusal can name another zone, so the queue refills as fast as it
drains, and each attempt is a real create that takes most of a minute to fail. Six
is the cap. There may well be capacity in a zone it never reached — name one with
`--zone`, or run it again.

**`Google refused to create comfy-linux in us-central1-a: ...`**
Not a capacity problem: Google refused for some other reason, and its own sentence
is quoted. Anything after this point could in principle have left something
half-made, so the fix line prints the command that lists the project's instances.

**`comfy-linux exists in us-central1-a and is billing`** / **`to fix: stop it now:
gcloud compute instances stop comfy-linux --zone=us-central1-a --project=your-project`**
**`comfy-linux could not be added to ~/.config/comfy-qa-tools/hosts.toml: ...`** /
**`to fix: add it by hand, or adopt it: comfy-qat discover`**
The machine was created and the host list was not. It is the one message in this
command printed after money is being spent, and the order of it is deliberate:
the box is real, it is billing, and **your host list has no record of it, so
`comfy-qat down` cannot reach it**. The raw `gcloud ... stop` is the only
thing that works, so it leads, on its own line, ahead of both the adoption path
and the interpolated write error — which can be long enough on its own to push a
command at the end of a paragraph out of sight. Adopting it with
`comfy-qat discover` is the other way out, and is second because it leaves
the box running.

**`your host list stopped loading while this ran (...), so port 8190 was chosen from the file as it was before — check it against the entries that no longer parse, and fix the file before comfy-qat go or comfy-qat down`**
The host list loaded when this command started — it refuses outright otherwise —
and does not load now, so something edited it while the create was running. That
is minutes, and another terminal or an editor is all it takes.

This one is a warning rather than a refusal, and the reason is that by now the box
is real and billing. Refusing here would leave a GPU running with nothing in the
host list naming it, which is worse than the problem being reported — so the entry
still goes in, on the best port this run could work out, taken from the file as it
read at the start.

Two things need checking. The port may collide with one in the part of the file
that no longer parses, and `comfy-qat go` and `comfy-qat down` both read the host
list, so they will exit 2 until it loads again. Fix the file first; the message
carries the loader's own refusal, which names what it objected to.

### The NVIDIA driver

**The driver is not in either base image**, and a GPU box without it looks
completely healthy: it boots, it answers, it installs ComfyUI, and it runs on the
CPU. `comfy-qat go` detects that now — but only after you have paid to find out.

**Linux boxes install it themselves.** The create attaches Google's own
`startup_script.sh` from
[GoogleCloudPlatform/compute-gpu-installation](https://github.com/GoogleCloudPlatform/compute-gpu-installation),
which is what
[Install GPU drivers](https://cloud.google.com/compute/docs/gpus/install-drivers-gpu)
points at for automating the install. It reboots the box once or twice and
carries on across the reboots; `comfy-qat go` waits that out. Google notes it does not
work on instances with Secure Boot enabled — nothing here turns Secure Boot on.

**That installer lays down the open kernel module, and that is why four cards are
refused.** The open NVIDIA modules work only on a GPU with a GPU System Processor
— NVIDIA's own README says "can be used on any Turing or later GPU" — so T4
(Turing), L4 (Ada), A100 (Ampere) and H100 (Hopper) work, and P4, P100 (Pascal),
V100 (Volta) and K80 (Kepler) cannot. On those the install still succeeds and the
box still boots; no module ever loads. `comfy-qat create` refuses them before
anything is billed rather than letting you find out. `cuda_installer.pyz` offers
`--installation-branch` and no way at all to ask for the proprietary module, so
driving an older card would mean replacing Google's installer with a hand-rolled
one — the same guess this tool declines to make on Windows, below.

**Windows boxes do not**, and this tool does not pretend otherwise. Google
documents exactly one way to install the driver on Windows Server, and it is a
person at an Administrator PowerShell prompt. `comfy-qat create` prints those two
commands when it finishes:

```powershell
Invoke-WebRequest https://github.com/GoogleCloudPlatform/compute-gpu-installation/raw/main/windows/install_gpu_driver.ps1 -OutFile C:\install_gpu_driver.ps1
C:\install_gpu_driver.ps1
```

Compute Engine does have a `windows-startup-script-url` metadata key, and pointing
it at that script would probably work. "Probably" is how a box gets created,
billed, and found running on its CPU an hour later, so it is left as a deliberate
seam rather than a guess: run the two commands once, by hand, and
`comfy-qat stamp <name>` will show a `cuda:0` device instead of a CPU one.

## Starting and stopping

**`this has been working on comfy-linux for 30m00s and the GPU driver has still
not come up, so it stopped rather than going on. The machine is up and billing.`**
The ceiling on one whole `comfy-qat go`, `up` or `switch`. The clause in the
middle names whichever phase was holding the clock when it ran out — the driver,
the machine not accepting commands, ComfyUI not installed, ComfyUI not answering.

**Every wait in this tool was already bounded when this was added, and the
command still ran for a quarter of an hour and concluded nothing.** Each phase
starts a FRESH clock and the phases stack: boot 300s + tunnel 300s + probe 180s +
SSH 300s + driver 900s is 33 minutes before the install has begun, and the install
streams with no timeout of its own. "Every wait is bounded" and "the command is
bounded" turn out to be different claims, and only the first was true. `GO_BUDGET`
is the second one.

It is 1800s — deliberately less than the sum of those maxima. Each of them means
"this phase alone has gone wrong", so a run that reaches two has already failed
and is spending GPU money to discover it. For scale, a real cold `go` that
installed ComfyUI from nothing took 3m11s and a warm one 52s.

Running the same command again picks up where it left off — an install that
finished is found and skipped — so this is a stop, not a rollback. If you keep
hitting it, the box is the problem rather than the clock: read `comfy-qat logs
<name>`, and note the machine bills the whole time either way.

**`comfy-linux still has no working GPU driver after 900s`**
The phase, rather than the whole command. Fifteen minutes is a long time to watch,
so this step now prints `still going, …` while it waits — it used to print its
opening line and then nothing at all, which is indistinguishable from a hung
command and was killed by hand on a real run at exactly the fifteen-minute mark.
On a card older than Turing this cannot succeed at all and `comfy-qat` refuses
before it starts waiting; see the GPU entries above.

**`ComfyUI is not answering on http://127.0.0.1:8188`**
The local host in your list is not serving. Nothing on your machine is stopped or
started by this tool, so start ComfyUI yourself:

```sh
~/ComfyUI/venv/bin/python ~/ComfyUI/main.py --port 8188 --listen 127.0.0.1
```

**`ComfyUI is not running locally`** from `comfy-qat go`
The same thing, from the command that would otherwise install ComfyUI for you —
which it will not do to your own machine. It prints the line above; run it.

**`ComfyUI is not answering and --no-install was given`**
`comfy-qat go` found no ComfyUI on the box and you told it not to install one. Drop
`--no-install`, or get onto the machine and install by hand.

**`could not run a command on comfy-win: ...`**
`comfy-qat go` reaches the box over SSH through IAP to check for ComfyUI. If that fails,
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

**`ComfyUI is not answering on http://127.0.0.1:8190: it is installed on comfy-win
but nothing has started it. The machine is up and billing.`**
The everyday one, and what you get from `comfy-qat up` on **any box that has been
through `comfy-qat down`**. Nothing on the box starts ComfyUI at boot, so stopping
the machine stops ComfyUI and starting the machine does not bring it back. The
install is still there and still good.

The fix is one command: **`comfy-qat go comfy-win`**. It finds the existing install,
skips installing, launches ComfyUI and waits until it answers — measured at 52
seconds on a real L4, against the three minutes `up` used to spend before saying
something less useful.

`up` does not do this for you on purpose. `up` is the machine-level verb; `go` is
the one that puts ComfyUI on the machine and starts it, and two commands that both
start ComfyUI would be one more than this tool wants. What `up` owes you is a
truthful answer fast, which is what this is.

**`ComfyUI is not answering on http://127.0.0.1:8190: it is not installed on
comfy-win. The machine is up and billing.`**
The same place, the other reason: this box has never had ComfyUI on it, or the
install did not survive. `comfy-qat go comfy-win` installs it and starts it in one
command. Expect minutes rather than seconds — torch is the slow part.

**`comfy-win is running and tunnelled, but ComfyUI is not answering on
http://127.0.0.1:8190. The machine is up and billing; ComfyUI is not installed or
not started.`**
The two entries above are what you normally get, because the tool asks the box
which of them it is. This one is what is left when the box **would not say** — the
`or` is honest here and only here. `comfy-qat go comfy-win` is still the answer to
both halves of it.

The machine and the tunnel are both fine — ComfyUI itself is not serving. **The box
is billing while this is true**, which is why the fix line ends with
`or stop paying for it: comfy-qat down comfy-win`. The error prints how to get
onto it, which differs
by OS: Windows needs a password reset and Remote Desktop over the tunnel, anything
else takes SSH through IAP.

It can also mean **the tunnel never opened.** The tunnel runs detached, so if your
session expired between starting the box and opening the tunnel, the tunnel died
and this message blames ComfyUI. Run `comfy-qat status` before you go looking
on the box — and see [session-expiry.md](session-expiry.md), because this is the
shape that failure takes.

**`could not tell whether comfy-win reached RUNNING: ...`**
The box was asked to start and then gcloud stopped answering, so the tool does not
know what happened — which is different from knowing it failed. **It may be running
and billing.** The message carries gcloud's own reason. Check the instance in the
console, then either try again or `comfy-qat down comfy-win`.

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
The tunnel is gcloud and nothing else, so there is no fallback. The exact
invocation is `gcloud compute ssh <instance> --zone=<zone> --project=<project>
--tunnel-through-iap --quiet -- -N -o ExitOnForwardFailure=no -L 127.0.0.1:<your port>:127.0.0.1:8188
-L [::1]:<your port>:127.0.0.1:8188`, and
`comfy-qat open <host> --dry-run` prints it for you — **that is what to look for
in `ps` when you are hunting your own tunnel.** There are two forwards, one per
loopback family, so `http://localhost:<port>` reaches the box on a machine that
prefers IPv6 for that name as well as `http://127.0.0.1:<port>`. The IPv4 one is
the promise and the IPv6 one is a convenience: `ExitOnForwardFailure=no` is set
explicitly so that a box with IPv6 disabled loses the `::1` bind and keeps the
tunnel. It is not `start-iap-tunnel`;
searching for that finds nothing and reads as "the tunnel is gone". Install the
SDK from the link in the fix line. Note `gcloud` may be on
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
of gcloud's own log instead of being written down. The line that named the cause is
lifted to the front of the message, ahead of the tail it came from; the full log is
in `~/.config/comfy-qa-tools/tunnels/<host>.log`.

The fix line offers `gcloud auth login` **only when the log actually asks for
reauthentication.** It used to offer it for every dead tunnel, which is how the
first live run of this tool sent somebody to repair a session that was working
perfectly — see the entry below.

**`the machine is not accepting SSH connections yet, so there is nothing for the tunnel to travel over. It is still starting up.`**
**`<name> is RUNNING but never started accepting SSH connections, so no tunnel could be opened to it. The machine is up and billing.`**
The machine is powered on and its SSH server is not answering yet. `comfy-qat go`
waits this out with a ticker and retries; the second wording is what you get if it
is still not answering after five minutes, and only then.

In the tunnel log this reads `Error while connecting [4003: 'failed to connect to
backend']. (Failed to connect to port 22)`. That is IAP saying it reached Google
and Google could not reach the far port — and the port it dials is **22**, so it is
a statement about sshd, never about ComfyUI. Google reports the instance `RUNNING`
throughout: RUNNING means the VM is powered on, not that anything on it is
listening. A box created a minute ago is worse again, because its startup script
installs the NVIDIA driver and reboots it once or twice, which `comfy-qat create`
prints as it finishes.

This is the shape of the first live `create` + `go` this tool ever ran. `go`
followed `create`'s own closing instruction, failed immediately, and reported the
tunnel as closed for unknown reasons with `gcloud auth login` as the advice. The
credential was fine; the identical command four minutes later installed ComfyUI and
served it. Both halves of that are fixed: the wait exists now, and reauthentication
is no longer suggested for a log that never mentioned it.

If the second wording does appear, the box really is not coming up. Read its serial
console in the Google Cloud console, or stop it and start it again — and note the
machine is billing the whole time either way.

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
`comfy-qat down comfy-win`, then open this one.

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
cause. Reopen with `comfy-qat open <name>`, or stop paying for the box.

**`gcloud is not signed in, so comfy-win cannot be reached: ... The machine is running and billing.`**
The credential died between starting the box and reaching it. The tool used to keep
retrying for five minutes, which cannot succeed and costs money the whole time, so
it now stops immediately and closes the tunnel behind it. `gcloud auth login`, then
carry on — the box is still running. See [session-expiry.md](session-expiry.md).

**`ComfyUI on comfy-win exited without ever answering on http://127.0.0.1:8190. The machine is up and billing.`**
ComfyUI started and stopped without ever serving — a bad argument, the wrong
directory, a missing requirement printed and gone. Its own log is above the error
on your terminal; read that first. Note the exit code alone would have said
success, which is the same lie as calling a booted VM "up".

**`the tunnel to comfy-win never opened, so ComfyUI was never reached on http://127.0.0.1:8190 — it was not asked, and nothing here says it failed. ComfyUI is running on comfy-win and has been left running. The machine is up and billing.`**
The message above, and this one, are the two ends of the same wait, and the
difference between them is worth more than it looks. That one means ComfyUI was
asked and stayed silent. This one means it was never asked: the local forward
onto the box never came up, so there was nothing to ask through. **Nothing on
the box is stopped in this case** — the server may be perfectly healthy, and
killing it would take away the one thing the launch got right. The last sentence
says which of the three the box reported: still running, no longer running, or
would not say. Read the tunnel's own log in
`~/.config/comfy-qa-tools/tunnels/<host>.log` and reopen with
`comfy-qat open <name>`; an expired session is the usual cause. The box is still
billing either way.

**`something is already listening on comfy-win's ComfyUI port (python, pid 8123), and the tunnel to it could not be opened, so it could not be asked whether it is ComfyUI. Nothing was started, and nothing was stopped.`**
The same distinction one step earlier. A held port is only a problem when what
holds it is not the ComfyUI you wanted, and that is settled by asking it —
through the tunnel. With no tunnel there is no answer, so the tool refuses
rather than assuming: the alternative was calling a possibly-healthy ComfyUI
"not answering" and offering to kill it. Fix the tunnel first, then run the same
command again.

**`Quota 'SSD_TOTAL_GB' exceeded.  Limit: 500.0 in region us-central1.`**
A disk you asked for does not fit under the project's SSD allowance. pd-balanced,
pd-ssd and hyperdisk all count against it; pd-standard does not. A move copies a
300 GB balanced disk into a second 300 GB balanced disk, which needs 600 GB under
an allowance that starts at 500 — so the move used to die at its most expensive
step, having already paid for the snapshot. It now takes pd-standard instead and
says so, along with what to raise if you want the faster disk. Raise it at
https://console.cloud.google.com/iam-admin/quotas

**`no room under this project's SSD allowance for a pd-balanced disk — using pd-standard instead`**
Not an error. The move finished, on a slower boot disk than the original, which
matters if you are comparing load times between machines. For a matching disk:
raise `SSD_TOTAL_GB`, delete the new disk, and run the move again.

**`that looks like a missing dependency rather than a broken install — installing its requirements and trying once more`**
ComfyUI is on the box and would not start. The usual cause is a machine built
from a disk or snapshot taken before a dependency was added — `main.py` is
present, so the install check passes, and the import fails. The requirements are
installed once and the launch is retried once; a second failure is reported
rather than looped on. A box with no route to the internet cannot do this — see
below.

**`ComfyUI on comfy-win-b is missing a dependency, and installing its requirements failed (exit 1)`**
The repair itself failed, so the launch is not retried — a second identical
traceback would teach nothing. Read pip's output above the error. If it timed out
reaching pypi, the box has **no route out**: an instance with no external address
and no Cloud NAT can be reached through IAP and cannot reach the internet, which
is easy to miss because everything reaches *it* perfectly well. The fix line
prints the command:

```sh
gcloud compute instances add-access-config <name> --zone <zone> --project <project>
```

A box created by `comfy-qat move` copies the source instance's networking, so this
only arises on a box built some other way — or one moved before `move` started
copying that networking.

**`torch on comfy-win-b is a CPU-only build and cannot see the L4 — installing the CUDA build instead`**
Not an error: the box was asked whether ComfyUI could start *before* launching it,
the answer was no, and the CUDA build is being installed. This check exists
because the alternative is finding out at launch, on a machine that has already
booted, tunnelled and started billing. It runs on every `comfy-qat go`, costs one SSH
round trip, and does nothing when the answer is fine.

**`torch could not be installed on comfy-win-b (exit 1), so ComfyUI cannot use its GPU`**
The install of the CUDA build failed. Read pip's output above. If it timed out
reaching pypi, the box has no route out — see the entry below on
`add-access-config`.

**`installing torch from cu130, which is what this box's driver supports`**
Not an error. The PyTorch index is chosen from the CUDA version the box's driver
reports, rather than pinned. It used to be fixed at cu128, and a real L4 answered
`You need pytorch with cu130 or higher to use optimized CUDA operations` — the
install worked, the fast path stayed off, and nothing said so. On a machine whose
job is measuring how fast things are, that is a wrong answer rather than a slow
one. If the box cannot be asked, cu128 is used: an index the driver cannot run
fails the install outright, where an older one only costs speed.

**`AssertionError: Torch not compiled with CUDA enabled`** in the ComfyUI log
Torch is installed and cannot see the card. On **Windows** this is almost always
where it came from: PyPI's Windows torch wheel is CPU-only, and the CUDA build
lives on PyTorch's own index. `pip install -r requirements.txt` says plain
`torch`, so it fetches the CPU one and ComfyUI dies on a box rented for its GPU.
Both `comfy-qat go`'s install and its repair pull torch from
`https://download.pytorch.org/whl/cu128` first for exactly this reason. If you
installed by hand, reinstall it the same way — and note `--force-reinstall`,
which is not optional: pip matches on version, not on which index a wheel came
from, so without it a same-version CPU torch satisfies the request and pip
reports "Requirement already satisfied" while the box stays CPU-only.

```sh
python -m pip install --force-reinstall --no-deps torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128
```

On **Linux** the PyPI wheel already carries CUDA, so no index is needed — which
is the asymmetry that makes this easy to get wrong in either direction.

**`ComfyUI is already running on comfy-win (python, pid 2804) — using it rather than starting a second one`**
Not an error. Something already holds the port and it answers as ComfyUI, so it
is what you wanted: the URL is printed and nothing is launched. This used to be
reported as a collision — the run said "ComfyUI answering", printed the URL, then
closed its own tunnel and failed. A held port only matters when what holds it is
not the thing you asked for.

**`something is already listening on comfy-win-b's ComfyUI port (python, pid 2380), and it is not answering as ComfyUI, so a second one cannot start`**
Something on the box already holds port 8188. Usually it is a ComfyUI a previous
run started and could not stop — a launch that dies after binding leaves the
process behind. ComfyUI's own version of this message is "Port 8188 is already in
use" plus a database lock error, neither of which names the process or says where
it came from, which is why this is checked before launching rather than
discovered afterwards.

If it is a working ComfyUI, the tunnel already reaches it: open the URL. If it is
not wanted, the fix line prints the command that stops it, and then the launch
works.

**`stopped the ComfyUI this run started on comfy-win-b (python, pid 2380)`**
Not an error — the tidy-up after a failed launch. Only ever a process this run
started, and only when it is still holding the port.

**`could not stop the ComfyUI left on comfy-win-b (pid 2380) — it still holds the port`**
The tidy-up failed, usually because the box became unreachable. The next launch
will refuse with the message above and print the command to stop it by hand.

### Stopping a box that has already been deleted

**`), and the project does not have it — the box no longer exists, so nothing is billing for it. Its host list entry is stale: comfy-qat discover --prune`**
`comfy-qat down` asked Google to stop a machine and Google answered that there is
no such machine — most often a 404 after somebody deleted the box in the console.
Nothing is billing, because there is nothing left to bill: this is not the
hedged **`it may still be running and billing`**, which is what you get when the
state genuinely could not be read. The only thing still wrong is the record, and
`comfy-qat discover --prune` fixes it.

`comfy-qat delete <name>` also works on a box that is already gone — it asks the
project when its own read fails, and takes the entry out. It still refuses a box
it merely could not reach, because not knowing whether something is running is
not permission to drop the record of it.


## ComfyUI running on the box, and its log

`comfy-qat go` launches ComfyUI **on the machine** and hands the prompt back, so two
boxes can be up at once — Windows in one browser tab, Linux in another. Its
output goes to a file on the box: `C:\ComfyUI\comfyui.log` on Windows,
`/opt/comfyui/comfyui.log` on Linux. `comfy-qat logs <name>` reads it.

Detaching moved where the log goes and nothing else. `go` still does not return
until ComfyUI has answered on the tunnel, because "started" is not "serving" and
a launch that came back on the box's say-so would leave a GPU machine billing
while ComfyUI failed to import something.

**`starting ComfyUI on comfy-win — it stays running on the box after this command returns`**
Not an error. Ctrl-C during this stops *waiting*, not ComfyUI — which is the
point, and the difference from `--follow`. If you meant to stop it, stop the box:
`comfy-qat down comfy-win`.

**`ComfyUI is running on comfy-win and this terminal is free.`**
Not an error, and the last thing `go` says before the URL. The box is up, the
tunnel is open, ComfyUI answered, and nothing is holding this shell. It is still
billing until `comfy-qat down`.

**`ComfyUI is no longer running on comfy-win — it stopped before it ever answered`**
The box was asked, while waiting, whether ComfyUI was still there, and it was
not. This turns a three-minute wait for something that died in four seconds into
an immediate answer. The end of its log is printed underneath, read off the box.

**`the last of its log on comfy-win:`**
Not an error — the lines under it are. A detached launch's startup log is no
longer on your terminal, so when one never answers, the end of the log is fetched
from the box and quoted rather than left there for you to go and find. For all of
it: `comfy-qat logs comfy-win --tail 100`.

**`ComfyUI on comfy-win could not be launched (exit 1).`**
The launch *command* failed, as opposed to ComfyUI failing after it started. On
Linux this is close to impossible — the detached form exits as soon as the
process is spawned — so in practice it means the box refused to run the script at
all. The requirements are installed and it is tried once more; a second failure is
reported rather than looped on.

**`ComfyUI on comfy-win would not start, and its requirements could not be installed either: ...`**
The same repair `--follow` does on a non-zero exit, reached the other way: a
detached ComfyUI that dies on a missing import exits *after* the launcher has
returned 0, so the evidence is in the log on the box rather than in an exit code.
The repair itself then failed. gcloud's reason is quoted; if it timed out
reaching pypi, the box has no route out — see `add-access-config` above.

**`comfy-win is not running, so it has no ComfyUI and no log to follow. Whatever it was writing stopped when the machine did.`**
from `comfy-qat logs`. The instance is stopped, so there is nothing to read and
nothing to wait for — and a command that hung here would be silently waiting on a
box you may still be paying for. `comfy-qat go comfy-win` starts the box and
ComfyUI on it.

**`there is no ComfyUI log at C:\ComfyUI\comfyui.log on comfy-win, so nothing has started ComfyUI there. The machine is running and billing.`**
The box is up and no ComfyUI has ever been launched on it by this tool. That is a
different fact from "the box is off", and it has a different fix: `comfy-qat go comfy-win`. The second sentence is the one that matters — the machine is on.

**`stopped reading. ComfyUI is still running on comfy-win, and so is the machine.`**
Not an error — what Ctrl-C out of `comfy-qat logs` says. It ends the reading and
nothing else. `go --follow` is the other one: there Ctrl-C reaches ComfyUI and
stops it, and the box carries on billing either way.

**`stopped, and ComfyUI stopped with it — that is what Ctrl-C does here. comfy-win
is still running, and a stopped ComfyUI on a running box still bills.`**

Ctrl-C out of `go --follow`. Under it are three lines: `comfy-qat go comfy-win`
to start ComfyUI again, `comfy-qat logs comfy-win` to watch it without stopping
it, and `comfy-qat down comfy-win   # stop the box, stop paying`.

The message used to say only that the machine was still running, which is true
and is not the part that surprises anyone. `--follow` and `comfy-qat logs` are
both log streams and the same key does opposite things in them — here it reaches
ComfyUI through the SSH session and stops it; there it ends the reading and
nothing else. A reflex Ctrl-C out of the wrong one kills the ComfyUI you were
debugging while the box carries on billing, so the difference is now stated in
three places: in `--help` for both flags, on the last line before the stream
starts, and here, in what Ctrl-C prints.

**`stopped. comfy-win is still running.`**
The same interrupt without `--follow` — Ctrl-C while `go` is starting ComfyUI
detached. Nothing was streaming, so there is nothing that went down with it; the
box is running, and the line under it is `comfy-qat down comfy-win   # stop the
box, stop paying`.

**`could not read the ComfyUI log on comfy-win: ...`**
The box would not run the command that reads the log. Usually the same causes as
any other SSH failure here: an expired session, a missing IAP permission, or a
box that has stopped accepting commands. The fix line prints the way onto the
machine, and the way to stop paying for it.

**`reading the ComfyUI log on comfy-win did not finish (exit 255), so what is above this — if anything — is not the whole log.`**
The connection to the box was made, or attempted, and came back with a failure
code rather than a log. 255 is the usual one and means ssh never got through —
the box stopped accepting connections, the session expired mid-read, the network
went. **Until this message existed, `logs` exited 0 in this case**, so a script
that read `$?` could not tell a log it had read from a box it never reached, and
neither could a person who had scrolled past the empty output. Anything printed
above the message is a partial read, not the log. The fix line prints the way
onto the machine and the way to stop paying for it. Ending a `--follow` with
Ctrl-C is not this: that says `stopped reading` and leaves ComfyUI running.

**`local is this machine, and this tool did not start its ComfyUI, so there is no log of its own to follow.`**
`comfy-qat logs local` has nothing to show. Your local ComfyUI was started by you, in
a terminal, and its log is in that terminal — nothing here detached it or
captured it. The fix line prints the command that starts one.

**`--new-window can only open a macOS Terminal window, and this is not a Mac with osascript on it. Nothing was started.`**
`--new-window` is a small convenience with exactly one implementation: macOS
Terminal, driven by `osascript`. Anywhere else it refuses rather than
half-working — a window that silently does not appear, on a command whose job is
to start a GPU box, is a machine you are paying for and cannot see. **Nothing was
started**, so nothing is billing. The fix line prints the exact command to paste
into a window you open yourself.

**`could not open a new Terminal window: ...`**
The same convenience, failing on the machine that can do it — usually Terminal
being denied automation permission (System Settings > Privacy & Security >
Automation). Nothing was started here either. The fix line prints the command;
run it in a window of your own.

**ComfyUI says `To see the GUI go to: http://127.0.0.1:8188` and nothing loads**
That address is correct **on the box** and wrong on yours: 8188 on your machine is
your own local ComfyUI, not the cloud one. Use the URL this tool printed —
`http://127.0.0.1:<the host's port>`, 8191 in `comfy-qat list`. It is now repeated just
before the log starts, because ComfyUI's line is the last one you read.

**No firewall rule is involved, and this tool does not create one.** That is worth
saying plainly here, because it is the wrong turn people take when a box is up,
tunnelled and serving and the browser still says "refused".

It used to be true. When the tunnel was `gcloud compute start-iap-tunnel`, the far
end was a port on the *instance's network interface*, so ComfyUI had to bind
`0.0.0.0` and the port had to be allowed through two firewalls — the VPC's and the
operating system's. Three things to get right, and any one of them failing looked
exactly like "ComfyUI is not running".

The tunnel is `ssh -L` now, and it resolves the far address **on the box**. So
ComfyUI binds `127.0.0.1` as it prefers, nothing is exposed on any interface, and
no rule is needed at either layer: port 22 is already open, which is how this tool
has been running commands on the box all along. If the browser cannot reach the
box, the cause is in this page's other entries — the wrong port, a dead tunnel, or
a ComfyUI that has not started — never a missing firewall rule. Do not go and ask
an administrator for `compute.firewalls.create`; you do not need it, and the box
bills for the round trip.

**`nothing is listening on the machine yet, so there is nothing to tunnel to — starting ComfyUI first`**
**`nothing is listening on port 8188 of the machine yet, so there is nothing to tunnel to`** / **`<name> is running and billing, but ComfyUI is not started on it yet`**
Not a broken tunnel — an ordering fact, and the one that made `comfy-qat go` unable to
work at all on a box that was not already serving.

The forward tests the connection before it will serve, and refuses when the far
port has no listener. So the tunnel cannot exist *before* ComfyUI is started, and
`go` used to open it first: the tunnel refused, the run failed, and the launch it
was about to do was the very thing that would have fixed it. Now the launch happens first and the tunnel is opened as soon as
ComfyUI is listening, from the same watcher that waits for it to answer.

You should not see this from `comfy-qat go`. From `comfy-qat open` it means exactly what it
says: start ComfyUI on the machine first, or use `comfy-qat go`, which does both.

**`NO_PYTHON`** in the ComfyUI startup log
ComfyUI is installed but no interpreter was found beside it — no `venv`, no
portable `python_embeded`, and no system `python`. Get onto the box and create one,
or reinstall with `comfy-qat go` on a box that has none.

**`comfy-win did not reach RUNNING within 300s`**
The instance was asked to start and did not. Check it in the Google Cloud console;
this usually means capacity or a quota problem in that zone rather than a fault
with the box.

**`could not start comfy-win: ...`**
gcloud refused. The most common cause is GPU quota — `comfy-qat quota` shows
what you actually have.

**`comfy-win says kind = 'local' but names a cloud instance (comfy-win, us-central1-a, a-project). Refusing to report it as stopped: if that machine is running, it is billing.`**
A host entry claims to be a local install while carrying the fields that identify
a Google Cloud box. `comfy-qat down` would have closed the tunnel, said "local ComfyUI
left running — this tool did not start it", and left a GPU instance running.
A cloud box is `kind = "gce"`; fix the entry and run `comfy-qat down` again.

**`--all stops every machine, so it takes no name`**
`comfy-qat down --all` is "stop everything"; naming one as well is a contradiction.
Drop the name, or drop `--all`.

**`say which machine, or --all for every one of them`**
`comfy-qat down` with nothing to act on. The question at the end of a session is
usually "am I still paying for anything", and `--all` is the answer to that one.

**`2 of 3 did not stop and may still be billing`**
`--all` keeps going when one machine refuses, because stopping the rest is the
whole point — then it lists the ones that failed with what to do about each.
Exit 1. Anything listed here is still costing money; the console is the
last resort.

**`could not stop comfy-win: ...`**
gcloud refused to stop the machine. What the tool says next depends on what the
project says the machine is actually doing — see "A stop whose answer was lost"
below for the three cases and the exact wording of each. An instance nobody
stopped is the most expensive failure this tool has.

**`Google has no L4 capacity in us-central1-a right now, so comfy-win cannot start.
This is not a fault on your side, and retrying in the same zone will not help.`**
A stockout. The zone has none of that card free, which is routine for GPUs and has
nothing to do with your account, quota or billing.

Google usually names a zone that *does* have capacity, and the fix line repeats it —
"Google says us-central1-b has capacity right now" — with the `comfy-qat move` command
to go there. Otherwise, wait: capacity varies by hour.

## Tunnels

A tunnel outlives the command that opened it, so what it is gets recorded rather
than assumed — the instance, the zone, the project and the port, beside the
process id. Everything below is that record refusing to be taken on trust. All of
it reaches you through `comfy-qat open`, and through `comfy-qat up` and `comfy-qat go`, which
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
`comfy-qat down comfy-win` — then open this one.

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
running but nothing on disk says so, so `comfy-qat down` cannot find it. Kill the pid
in the message, fix whatever made the directory unwritable, and open it again.

## Moving a box to a zone with capacity

`comfy-qat move` snapshots the boot disk, rebuilds the box elsewhere and leaves the
original stopped. Nothing is deleted, at any point, by anything here.

**`comfy-win is local — there is nowhere to move it to`**
Only cloud hosts have a zone. A local install is where it is.

**`--dry-run cannot find a zone with capacity: the only way to ask is to start
comfy-win, and a box that starts is billing`** / **`to fix: name the zone yourself
and the plan prints without touching anything: comfy-qat move comfy-win --to
us-central1-b --dry-run`**
Nothing answers "where is there an L4 free". `move` finds out by trying to start
the machine and reading the zone out of the refusal — and when there is no
refusal, the box is up and billing. That is fine for a real move, which was going
to start something anyway, and not fine at all for `--dry-run`, whose whole
promise is that it changes nothing. So the two are refused together: give
`--dry-run` a `--to` and it prints the full plan, contacting nothing that costs
money.

**`Google did not name a zone with capacity`** / **`to fix: pick one with --to,
e.g. --to us-central1-b`**
Asked to find a zone itself, `move` starts the box and reads the zones out of the
stockout message. This time Google did not suggest any — which happens when the
card is short everywhere, or when the start failed for some other reason. Name a
zone yourself, or wait.

**`comfy-win is untouched in us-central1-a`**
Printed under any failed move, last before the fix. One of the four steps —
snapshot, disk, instance, host list — did not complete, and this is the line that
says where you stand: the original box is exactly as it was, so you can retry, or
move by hand. Any half-made snapshot or disk is left behind for you to look at and
is not cleaned up automatically. You do not have to hunt for it in the console:
every leftover is named in the tool's own output with the exact `gcloud ... delete`
line beside it, and `comfy-qat move <host> --clean` removes them for you.

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

**`which machine? A name, an operating system, a card, or both as os/card.`**

No machine was named. There is deliberately no default and no "the last one you
used": a local ComfyUI and a tunnel to a cloud box both answer on `127.0.0.1` and
look identical in a browser, so the machine is always said out loud. `comfy-qat
list` shows what is declared.

**`No such option: --os`**

`--gpu` gives the same, and this is the argument parser refusing rather than the
tool refusing to act — nothing was contacted and nothing ran. `--os` and `--gpu`
used to be a second spelling of the argument every one of these commands already
takes: `comfy-qat go --os windows --gpu l4` and `comfy-qat go windows/l4` went
through the same resolution and reached the same machine. They were hidden for a
release and said so on stderr every time one was used. Write the description as
the argument: `comfy-qat go windows/l4`.

`create` and `quota request` keep theirs, and they are not the same case as each
other. `comfy-qat create --os windows --gpu l4` describes a box to build — there is
nothing yet to select. `comfy-qat quota request --gpu l4,a100` names a
comma-separated list of cards to ask Google for, which no selector accepts.

Every command that takes a machine accepts its name, an operating system, a card,
or both as `os/card` — `comfy-qat switch windows`, `comfy-qat go l4`, `comfy-qat
up windows/l4`. These are the refusals, and each one is a refusal rather than a
guess on purpose: a tool that picks for you is a tool that reads results from the
wrong box.

**`nothing declared matches 'windows/l4'. Declared: local (local install); comfy-linux (Ubuntu 22.04, A100). Create the box in the Google Cloud console, then `comfy-qat discover` to add it to your host list.`**
You described a machine you do not have. The message lists what you do have, with
each one's OS and card, so you can see which half was wrong. If the box exists in
Google Cloud but not in your host list, `comfy-qat discover` adds it — nothing
needs typing, Google already knows its zone, card and OS.

**`'windows' matches 2 hosts: ... Say which one: add the other half, e.g. `windows/l4`, or use the host's name.`**
Two machines fit and the other axis separates them. Add the card, or name the
host outright. Nothing was started, stopped or contacted; this is decided offline,
before any cloud call.

**`'windows/l4' matches 2 hosts: ... They are the same operating system and the same card, so only the name tells them apart: comfy-win, comfy-win-b.`**
Two machines fit and **no description can separate them** — same OS, same card.
The earlier version of this said "add the other half, e.g. `windows/l4`" to
someone who had just typed `windows/l4`, which is advice to retype the failing
command. When there is no half left to add, the names are the answer, so it lists
them.

**This tool manufactures that collision itself, and it is worth knowing before
you meet it.** `create` names boxes `comfy-linux`, then `comfy-linux-2`, so a
second box of the same kind is identical on both axes a description matches on —
same OS, same card. Every description that worked with one box is refused from
the second onward. It is not a corner case; it is the day you have two Linux
boxes.

It is refused rather than guessed at **on purpose**, and there is no tie-break
coming. Two machines identical on both axes are not ambiguous by accident, they
are indistinguishable, and every rule that could pick one — the newest, the
first declared, the one that happens to be running — invents a preference you
never expressed. This is the same lookup `delete` uses.

If you expect to keep several boxes of a kind, name them when you make them:
`comfy-qat create --os linux --gpu l4 --name linux-cuda12`. A name you chose is
the only thing that stays usable as the list grows.

**`'windows-l4' is two descriptions run together. The separator is '/': windows/l4`**
A hyphen reads as part of a name, and host names contain hyphens, so the two
cannot both be separators. Use `/`.

**`unknown host 'rtx4090'.`** / **`declared: local, comfy-win`** / **`or describe the machine: an operating system (windows, linux, ubuntu, debian, macos, local), a card (l4), or both as os/card`**
Neither a declared name nor a description this tool understands. Three lines, on
purpose: what went wrong, what you actually have, and the vocabulary you can use
instead — an operating system, a card, or both as `os/card`. It used to be one
240-character sentence carrying all three, which is the first message a new tester
meets and took a second reading to untangle.

**`host 'comfy-win': unknown field(s) 'gce_zoen' (did you mean 'gce_zone'?). Known fields: gce_instance, gce_project, gce_zone, gpu, kind, os, port.`**
A misspelt field in `hosts.toml`. This used to be reported as five *missing*
fields that were all present, because the required-field check ran first and the
typo was invisible to it — so the message described a file quite unlike the one in
front of you. Unknown fields are now checked first and the suggestion is offered.

## The old spellings, `host` and `auth`, are gone

**`No such command 'host'.`**

**`No such command 'auth'.`**

Click's own refusal, and the exit code is 2. Every verb on this page was once
reachable with one of those two nouns in front of it as well. They were removed at
1.1.0. **Take the noun off and the rest of the line is right** — the verb was
always the command, and the noun was reaching that same command a second way.

Nothing else changed with them. Nothing in this tool is called `go`, `down`,
`list` or `stamp` other than the commands of those names, so the noun was pure
typing, and on a second operating system it is typing you do twice as often. For
one release the two were hidden from `--help` and warned on stderr each time
anyone used one, naming the verb to type instead; that warning is what made it a
window, and the window has closed.

`comfy-qat env` is hidden too and deliberately says nothing, and it is staying.
It is not a second spelling of anything, so there is no shorter form to point at:
it is a whole command parked until its own release, and telling anyone it was on
its way out would be false.

## When the machine you want cannot start

A GPU stockout is routine, is nothing to do with your account, and is the moment a
tester usually gives up and opens the console. These lines are the tool trying to
keep you testing instead.

**`comfy-linux is untouched — you still have the machine you were on`**
**`comfy-linux, comfy-win are untouched — you still have the machines you were on`**
Reassurance, printed when a `switch` fails — the singular when one machine was
left running, the plural when more than one. The target is brought up *before*
anything is stopped, so a failed switch leaves you exactly where you started. You
have lost nothing but the time.

**`where you can test instead, easiest first:`**
The boxes that can run right now, same OS first, and a box in the same zone listed
last and labelled — it may hit the same shortage. Pick one and carry on.

**`no other machine is declared, so there is nowhere to switch to:`**
You have one box and it cannot start. The line under it —
`comfy-qat discover   # declare a box you already have` — will pick up
anything already in your project; otherwise your options are to wait for capacity
or to move the box.

**`if it has to be comfy-win:`**
When only that machine will do — the install on it, the models on it — this is
followed by the `comfy-qat move` command for a zone Google says has capacity. Read
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

**`an earlier run left this behind, and it is billing:`** followed by a disk or a snapshot
An earlier `comfy-qat move` did not finish, and what it created is still there and still
billing. This is a report, not an error — the move carries on and reuses what it
can, which is what makes a failed move cheap to retry. Each line is followed by the
exact `gcloud ... delete` command that removes it.

It is printed on **stderr**, under `warning: `, with the resources indented under
it — as is `--dry-run: these would be deleted first, and are not`. That is not
cosmetic. The rule this tool holds to is that stdout carries the answer and stderr
carries the story, and this block is the story of an *earlier* run rather than the
answer to this one. On the `--dry-run` path the command used to return before
anything reached stderr at all, so `1>/dev/null` discarded the whole block while
`2>/dev/null` discarded nothing — meaning someone piping a dry run into a file to
read later captured three `--quiet` delete commands and no plan. The list is
printed once now, not once before the `--dry-run` line and again after it.

Nothing is deleted unless you say so. In a terminal you are asked "Delete these and
start the move fresh?" and the move continues either way; `--clean` answers yes
without asking. **Both then carry on and create the box** — `--clean` is "clean up
first, then move", not "clean up instead of moving". Under `--yes`, or with output
piped, the leftovers are kept and reported.

**`also on the project, unrelated to this move and billing:`** followed by a disk or a snapshot

Printed after a move that worked, on stderr, for the same reason. A disk or a
snapshot from some earlier move of a *different* box is sitting on the project and
costing money; this command did not create it and does not touch it, so it is not
part of the answer to "where is my box now" — but an unattached disk looks like
nothing at all in a console, and nothing else here would ever mention it. Each
line comes with the delete command that removes it.

The distinction from the entry above matters when you decide what to delete. That
one is **this move's** unfinished work, and `--clean` removes it. This one is
outside the move's scope, so nothing here is ever removed for you — each line
carries its own `gcloud ... delete` command, and running it is your call.

It appears at the end rather than before the confirmation on purpose: a delete
command for a machine you were not thinking about, three lines above
`Move comfy-win to us-central1-b? [y/N]`, is how the wrong thing gets deleted.

If you have never seen this before on a project you have been testing on for a
while, that is the point of it: these are exactly the resources that look like
nothing at all in a console.

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

**`us-central1-b has no L4 capacity either, so comfy-win-b could not be created.`**
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

**`this would leave 'comfy-win-us-central1-a' and 'comfy-win' naming one machine
— comfy-win in us-central1-a — and a host list with two entries for one box is
one this tool refuses to read, whole. Nothing was created.`**
You are moving a box back into a zone it was moved out of. The first move left
that zone declared under `<name>-<zone>` so `down` could still reach the machine
it left there; moving home puts the box back under its own name, and now two
entries name one instance.

`config.load` refuses that for the **whole file**, not just the entry — so every
command would exit 2, `comfy-qat down` included, while the box runs and bills.
Checked before anything is created, which is the difference between this message
and a snapshot, a disk and an instance you have already paid for. Run
`comfy-qat list --live` to see whether the old box is still there, delete the
leftover entry, and move again.

**`comfy-win is now in us-central1-b, running and billing, but your host list
could not be updated: ...`**
The expensive half of the move worked and the cheap half did not: the instance
exists in the new zone, it is running, and the text rewrite of your host list
failed. An inline comment on a port line (`port = 8192  # the QA port`), a config
directory you cannot write to, and a failed `os.replace` all land here — the
first is simply what a hand-maintained file looks like, so this is not an exotic
failure.

**`comfy-qat down <name>` cannot help you here**, and that is why the raw stop
command leads: `down` reads the host list to find the box, and the host list is
precisely what did not get written, so the entry it would read still names the
zone the box has just left. Stop the new box with the printed
`gcloud compute instances stop`, fix the host list, then run the same `move`
again — it finds what already exists and carries on from there. Nothing is torn
down in the meantime; the instance is the useful half and re-running is the
supported recovery.

Before this was caught, all of the above was a Python traceback.

**`could not delete the snapshot ...`** after a move otherwise succeeded
The new machine is up and in your host list; only the cleanup failed. The snapshot
is still billing and the message repeats the command that removes it.


## Tunnels and NumPy

**`numpy    not installed — every tunnel is slower than it needs to be`** (in `comfy-qat status`)

`setup` installs it; this row is the catch-up for anyone who ran setup before it
did. It is the last check and the only one that blocks nothing — `status` prints
the fix for the first failure, so putting it any earlier would hide a missing
account behind advice about a slow tunnel.

**`NumPy would not install, so tunnels stay slower than they could be. By hand: <python> -m pip install numpy`**

`setup` puts NumPy into gcloud's own virtualenv, because gcloud asks for it on
every tunnel it opens:

    To increase the performance of the tunnel, consider installing NumPy.

IAP forwarding does its framing in Python, and NumPy moves that into compiled
code. This tool opens a tunnel for every `go`, `up`, `open` and `logs`, and pushes
multi-gigabyte torch downloads through them, so it is worth having.

If the install fails, nothing is broken — tunnels are simply slower. The command
in the message is the one to run, and the path in it matters: it is gcloud's OWN
python (`~/.config/gcloud/virtenv/bin/python3.x` on macOS), not the system one.
Installing NumPy anywhere else leaves gcloud unable to see it, which is the reason
this advisory goes unactioned for years.

## Deleting a box

`delete` is the only thing this tool does that cannot be undone. It destroys the
instance and its boot disk, and the ComfyUI install on it goes too. Everything else
here is reversible — a box stops and starts, a move leaves the original where it
was, a bad host list has a backup beside it.

So it refuses more than it warns, and its confirmation is typing the box's name
rather than `y`. A `[y/N]` is answered by reflex at 2am; a name is not.

**`<name> is <state>, not stopped. Stop it first, so that what you are deleting is something you have just looked at`**

If the state reads "in an unknown state", the read succeeded and told us nothing
about the machine — which is a third answer and not a state. The refusal is the
same: not knowing what a box is doing is not permission to destroy it.

GCE would delete a running instance quite happily. This refuses so that the state
of the machine is something you saw seconds ago rather than assumed. `comfy-qat
down <name>`, then delete it.

The check is for TERMINATED specifically, not for "not RUNNING". A Compute Engine
instance has eight states — PROVISIONING, STAGING, RUNNING, STOPPING, SUSPENDING,
SUSPENDED, TERMINATED, REPAIRING — and only one of them means the machine is
certainly doing nothing. A box thirty seconds into booting is in STAGING, and it
is not something anyone has just looked at.

**`no host is called '<name>'. delete takes an exact name, never a description — a description can resolve to a machine you did not picture, and this cannot be undone`**

Every other command takes a description: `go windows`, `stamp l4`, `logs
linux`. `delete` does not, and the reason is that a description resolving to a box
you had not pictured is survivable for `go` and is not survivable here. `comfy-qat
list` shows the names.

**`no host is called '<name>'. Did you mean '<other>'?`**

The name differs only in case. It is not acted on — the suggestion is offered and
you run it yourself.

**`which machine? delete takes a name, never a description`**

No name was given. There is deliberately no default and no "the obvious one".

**`<name> is this machine, not a cloud box`**

`delete` removes cloud instances. The local ComfyUI is not one, and this tool did
not create it.

**`it is still in your host list and could not be removed: <reason>`** / **`take [hosts.<name>] out by hand — while it is there, `create --name <name>` will refuse it, and its port stays reserved for a machine that no longer exists`**

The box and its disk are gone; only the host list entry is left. That matters more
than tidiness: `create` refuses a name that a host list entry holds, and ports are
allocated from the same list. So the leftover reserves both a name and a port for a
machine that does not exist, and the refusal arrives weeks later with nothing to
connect it back to the delete.

Open the file and remove the `[hosts.<name>]` block. `comfy-qat list` shows the
path.

**`this needs a terminal to confirm in`**

The confirmation is typing the box's name, which needs somewhere to type. In a
pipe or a script, pass `--yes` if you have already decided — the refusal is
deliberate, so that redirecting output can never silently take the destructive
branch.

## Rewriting the host list during a move

A move rewrites `hosts.toml` so the box keeps its name, its port and its URL. That
is the one place this tool rewrites a file you maintain by hand, so it refuses
rather than guesses, and it refuses **before** writing anything. The previous file
is copied to `hosts.toml.bak` first — read back and compared before anything is
written, so a copy that did not land stops the rewrite rather than being assumed —
and the swap is atomic and flushed to the disk. When that copy is superseded it is
archived to `backups/hosts.toml.<when>.bak` rather than overwritten, five deep, so
a second move does not destroy the first one's backup. Everything in `backups/` is
this tool's and safe to delete.

All of these arrive at the last step of a move, which means **the new box already
exists and is billing**. The host list not being updated is recoverable; not
knowing you are paying is not. `gcloud compute instances list` shows what is
running, and `comfy-qat down <name>` stops it once the list names it again.

**`the rewritten host list would not load: <reason> Nothing was written.`**

The result would have been a file this tool then refuses to read — after which no
command works at all until somebody edits it. The usual cause is moving a box back
to a zone it came from: the entry retired by the earlier move still names that
instance in that zone, and so does the one coming home, so two entries describe one
machine. Delete the stale `[hosts.<name>-<zone>]` entry, then run the move again.

**`the rewritten host list does not hold what it should — missing <a>, unexpected <b>. Nothing was written.`**

An internal check: the rewrite would have lost or invented a machine. Nothing was
written. This should not happen; if it does, `hosts.toml` is unchanged and worth
sending on with what you ran.

**`the rewritten host list would not parse: <reason>`**

The transform produced invalid TOML. Nothing was written. Most likely something in
the file the rewrite did not expect — send the file and the command.

**`<name> is not in the host list, so it cannot be moved.`**

The name given does not appear in `hosts.toml`. `comfy-qat list` shows what is
declared.

**`the previous <file> could not be copied (<reason>), so this rewrite could not be undone. Nothing was written.`**

The existing host list could not be copied to `hosts.toml.bak`, so the rewrite
would have been one you could not undo. Nothing was written and `hosts.toml` is
exactly as it was. Usually the config directory is not writable, or the volume is
full: `ls -ld ~/.config/comfy-qa-tools` and `df -h ~` between them say which. Fix
that and run the move again.

**`the backup of <file> did not read back the same as the file it was copied from, so the previous host list is not recoverable. Nothing was written.`**

The copy of your existing host list was written and then read back, and it did not
match. Nothing was rewritten, and `hosts.toml` is exactly as it was — this refuses
at the one moment where continuing would mean overwriting the only copy of a file
you maintain by hand. It means the disk or the filesystem is not storing what it
is given, so check free space on the volume holding `~/.config` first, then the
disk itself. Move the box again once writes are landing.

**`<name> has no port line, so its port cannot be freed.`**

Every host needs its own `port`, and a move has to hand the moved box the old
port while giving the retired entry a new one. A `[hosts.<name>]` block with no
`port` line stops that. Add one — any free number above 8188 — and run the move
again.

## A move that ran out of capacity at the far end

**`<zone> has no <card> capacity either, so <name> could not be created. The zone Google named had capacity when it said so and has none now; that is normal, and not a fault on your side.`**

The worst shape a move can fail in: the slow, paid-for half is done — the snapshot
was taken and the disk was copied — and there is no machine at the end of it.

The zone came from Google's own refusal when the original start failed, and that
is a hint rather than a reservation. Minutes pass while a 200-300 GB disk copies,
and by the time the instance is asked for, the capacity is gone.

Retrying into a different zone is cheap by comparison, because the snapshot is
kept: only the disk is copied again, not the whole thing. The message names a zone
to try and, separately, the commands that delete what this attempt left behind if
you would rather stop. Both are printed, because the disk that already exists is
billing either way and choosing between them is yours.

## Asking for two different zones

**`--zone <a> and --region <b> cannot both be right: --zone pins one zone, --region asks for a choice within one region`**

The two options contradict each other by their own descriptions, so passing both is
not an intention this tool can carry out — it is a mistake, usually a stale `--zone`
left in shell history.

It used to let `--zone` win and say nothing about the region it discarded. On a
laptop in London that is the difference between a box 21 ms away and one in Iowa at
115 ms, created, billed, and only noticeable because everything feels slow.

The rule this follows, which is worth knowing because it is not "never combine
flags": **a flag that is ignored is refused when it would have changed the outcome,
and ignored quietly when it could not.** `--yes` with `--dry-run` is the same shape
and is fine, because `--yes` only suppresses a prompt that `--dry-run` never
reaches.

## A switch that stopped the old box and could not start the new one

**`the machines you had are stopped and <name> did not come up, so you are on neither. Check what is running before retrying — <name> may have started and be billing`**

`switch` normally brings the target up FIRST, so a failure leaves you on the
machine you had. When the GPU ceiling is the reason you are switching — one card
across the whole project, which is the usual case here — that is impossible: the
target cannot start until the other box stops. So on that path the order reverses,
and a failure after the stop leaves you on neither machine.

The bare "switch failed" that used to print reads as "nothing happened". Something
did: the box you were working on is down, and the new one may be up and billing.

`comfy-qat list --live` asks Google what is actually running, which is the only
answer worth having before retrying.

## Ctrl-C while something is being created, started or moved

**`interrupted — Ctrl-C stops this tool, it does not cancel a request Google has already accepted.`**

Then one or both of **`this may exist and be billing:`** and **`and this had
already happened when you stopped it:`**, each followed by what was in flight,
and then the exact commands that take it off the bill. The command exits **130**, which is what a shell reports for a process
stopped by Ctrl-C. It used to exit 1 under the word `Aborted!` — a failure code
over a sentence meaning nothing happened, printed over a GPU box that was running.

**What actually happens, because the obvious reading is wrong in both
directions.** Ctrl-C reaches the local `gcloud` process and kills it. It does
*not* reach Compute Engine: the API request has already gone, and the instance is
built on Google's side whether or not the client that asked for it is still
alive. So the resource is at risk, and the tool learns nothing from the interrupt
about whether it succeeded — `subprocess.run` re-raises without returning an exit
code. Measured against the real call path with a stand-in for gcloud: the
interrupt reaches the caller at 0.41s and nothing comes back.

That is why every line says **may**. What makes it useful is not a claim of
certainty it cannot support, but the exact resource and the exact command:

* **`create`** names the instance and the zone *the attempt was actually in* —
  `build` falls through the ranked zones one at a time, so on a stockout-heavy
  day that is rarely the first one. Its box is NOT in your host list: the entry is
  written after the instance exists, so `comfy-qat list` cannot see it and
  `comfy-qat down` cannot reach it. The raw `gcloud compute instances stop` is the
  only thing that works, and `comfy-qat discover` adopts it if you would rather
  keep it.
* **`go`, `up` and `switch`** name the machine, and it is declared, so
  `comfy-qat down <name>` reaches it. `comfy-qat list --live` settles whether it
  needs to.
* **`switch`** is the only command that prints the second heading. On the
  GPU-ceiling path — the normal path when `GPUS_ALL_REGIONS`
  is 1 — the machine you were working on is stopped BEFORE the new one starts, so
  an interrupt in between leaves you on neither. That half is not a bill, and it
  is not reported as one.
* **`move`** lists whatever the run had made by then — the snapshot, the new disk,
  the new instance — with the delete commands, and with the resume: running the
  same `comfy-qat move` again finds what exists and carries on. This is the same
  report a *failed* move prints. Before, it was reachable only on the next `move`
  run, which is the run somebody who has just pressed Ctrl-C is least likely to
  make.
* **`move` with no `--to`**, which is the ordinary way to use it, names the box
  in the zone it is **already** in: `comfy-win (comfy-win in us-central1-a),
  started to ask where there is capacity`. Finding a zone with capacity means
  starting the machine and reading the answer out of Google's refusal — there is
  no API that answers it — so a probe that is *not* refused leaves a GPU box
  running, in the original zone, with the move not yet begun. An interrupt in
  that window used to print nothing at all: the output ended at "asking Google
  where there is capacity", with no report and no stop command, while a box may
  have just started. The exit code was 130 throughout; the sentence was what was
  missing.

* **`rdp`** prints a third heading, **`this may already have happened, and it
  does not undo:`**, and it is the only one of the four that is not about a
  resource. Nothing was created and nothing is billing — what may have happened is
  that the Windows password on the box was reset, so the one that was working has
  stopped working, for you and for anyone else who signs in there. There is no
  object to look up afterwards and no state to read back: the evidence is a
  password nobody has. So the undo is to run the reset again and read the new one
  off the screen. The command says what it is about to do before it does it, and
  says how long it has been going while it waits, precisely so that someone who
  interrupts it knows which side of the reset they are on.

Two interrupts are deliberately not reported here, because they are not failures
and leave nothing behind: Ctrl-C out of `comfy-qat logs` ends the reading, and
Ctrl-C out of `go --follow` stops ComfyUI on the box. Both say so themselves, and
both leave the machine running and say that too.

## A read that said nothing

**`could not tell whether <name> is running, so there is no saying whether it has a log.`**

Asking Google what the machine is doing succeeded and came back without a state.
That is a third answer — not running, not stopped — and it is reported rather than
guessed, because both guesses are wrong in a way that costs something: "it is off"
walks away from a box that may be billing, and "it is on" sends you looking for a
log that does not exist.

`comfy-qat list --live` asks again. It is usually transient.

The same answer reaches `disconnect`, which says "could not tell whether <name>
is running" rather than claiming it is still billing.

## A start whose answer was lost

**`the start of <name> did not report back (<error>), but the machine is <state> — it started, and it is billing.`**

The request reached Google and the reply did not come back — a timeout, a dropped
connection. The tool then asked what the machine is actually doing and found it
running or coming up. So the start worked; only the confirmation was lost.

Nothing needs retrying. `comfy-qat down <name>` stops it if you did not want it.

This is reported rather than swallowed because the obvious reading of a failed
start — "nothing happened, try again" — is the expensive one. A retry against a box
that is already coming up does nothing useful, and walking away leaves a GPU
billing that you believe never started.

**`could not start <name>: <error>`**

The start failed and the machine is stopped, or its state could not be read at all.
If the error was a timeout, the request may still have landed: `comfy-qat list
--live` asks Google what is actually running before you retry.

## A stop whose answer was lost

The mirror of the section above, and the more expensive of the two. A start whose
answer is lost may leave a box running when you think it never started; a **stop**
whose answer is lost leaves one running when you have just been told it stopped —
and `down` is the command people run precisely to stop paying, then close the
laptop.

So when gcloud refuses the stop, the tool asks Google what the machine is actually
doing before it says anything. Three answers, three messages.

**`<name> is stopped — the request landed and only the reply came back broken (<error>)`**

The stop worked; only the confirmation was lost. The machine is TERMINATED, read
back from the project after the failure, so the bill has stopped. This is not
reported as a failure and the run exits 0 — saying otherwise would tell
`down --all`'s summary the opposite of what Google just said.

**`could not stop <name> (<error>), and the project says it is still <state> — it is billing.`**

The stop failed and the machine is genuinely still up. Nothing is hedged here
because nothing needs to be: the state was read and it came back. Exit 1, and the
fix line hands over gcloud's own stop with the zone and project filled in:

```
gcloud compute instances stop <name> --zone=<zone> --project=<project>
```

That raw command leads rather than `comfy-qat down <name>`, because `comfy-qat
down` is the command that has just failed on this box.

**`could not stop <name>: <error>. Reading its state afterwards failed too, so it may still be running and billing.`**

Both calls failed, so nobody can say which way it went. The honest sentence is
that it may still be running — not "it is running", which asserts what was not
read, and not a bare "could not stop", which reads as though the box is off.
`comfy-qat list --live` asks Google what is actually running.

Until this existed, all three cases printed `could not stop <name>: <error>` and
gcloud's own advice, which is empty for the timeout that makes this matter. The
box was left running and the message said nothing about money.

## A capacity probe whose answer was lost

`comfy-qat move <name>` with no `--to` has to find a zone with capacity, and
nothing answers "where is there an L4 free". The only way to ask is to try to
start the machine and read the zone out of the refusal — so **the probe is a
start**, and a probe that is not refused leaves a GPU box running.

A stockout refusal means nothing started, and that path says nothing about money
because there is nothing to say. These are the other failures.

**`asking Google where there is capacity did not report back (<error>), and <name> is <state> — the probe started it, and it is billing.`**

The request reached Google, the reply did not, and reading the machine back found
it running or coming up. The probe worked as a start; only the confirmation was
lost. Exit 1 — the work started and failed — with gcloud's own stop handed over:

```
gcloud compute instances stop <name> --zone=<zone> --project=<project>
```

**`asking Google where there is capacity failed (<error>), and reading <name>'s state afterwards failed too. The probe is a start, so it may have landed — it may be running and billing.`**

Both calls failed, so which way it went cannot be established from here. Exit 1
rather than 2, because 2 means "nothing was changed" and that is the one thing
nobody can assert at this point. `comfy-qat list --live` settles it.

A credential failure is not in this group: it is a refusal, exits 2, and cannot
have started anything — an expired session never reaches the compute API.
Likewise a bad zone or any other flat refusal, where reading the machine back
finds it still stopped.

Until this existed, every one of these exited **2** with gcloud's error and
nothing else — a command asserting that nothing changed, about a probe whose own
job is to start a machine.

## A new box and its GPU driver

**`comfy-p4 has a P4, and nothing this tool installs can drive it: the open NVIDIA kernel module needs a GPU System Processor, and only Turing and newer cards have one. The machine is running and billing. Waiting will not change it — the driver install reports success and the kernel refuses the device.`**

A box with a pre-Turing card — P4, P100, V100, K80 — that this tool did not
create. `comfy-qat create` refuses those cards outright, so the machine came from
the console or from somewhere else and `comfy-qat discover` picked it up.

`go` says this in a second instead of waiting the full fifteen minutes and then
pointing at the installer's log, because on these cards the log is the one place
that looks healthy. Measured on a P4 on 2026-09-09: `google-startup-scripts`
finished, exit status 0, packages installed — and `dmesg | grep -c 'not supported
by open'` returned 15. Confirm it on the box yourself:

```sh
comfy-qat ssh <name>
dmesg | grep 'not supported by open'
```

A reboot does not help; the card has no GSP and never will. Stop the box and use
a T4 — same `n1-standard-8` machine, and it works.

**`<name> still has no working GPU driver after 900s. The machine is running and billing.`**

A box created by `comfy-qat create` installs its NVIDIA driver from a startup
script on first boot, and that reboots the machine once or twice. `go` waits for
that to finish before installing anything, because an install started during a
reboot dies half-done — the symptom is `client_loop: send disconnect: Broken
pipe` and an install that reports failure about a box that was merely restarting.

This message means the wait ran out. The driver install has its own log on the
box:

```
comfy-qat ssh <name>
sudo cat /opt/google/cuda-installer/installer.log
```

Google's installer writes `/opt/google/cuda-installer/cuda_installation` when it
has finished. If that file is absent and the log has stopped moving, the install
failed rather than being slow. The machine is billing either way — `comfy-qat
down <name>` stops it.

## Stopping machines

**`<name> came back from stopping with an outcome this tool does not recognise
(<verdict>), so it is counted as unchecked`**

You should never see this, and if you do it is a bug in this tool rather than
anything about your machine — but it is printed rather than swallowed, and the
machine is counted among the ones that could not be checked rather than among the
ones that were stopped.

`down --all` sorts each machine into stopped, still-billing, or could-not-check by
reading a word back from the routine that stops it. That sorting used to discard
anything it did not recognise into a throwaway list, so a machine could drop out of
every count and every closing sentence in complete silence — in the one command
whose entire purpose is answering "am I still paying for anything". An undercount
there is the expensive direction, so an unrecognised answer is now read as "I do
not know", which is what it is.

Check the machine yourself with `comfy-qat list --live`, and report the verdict in
the brackets.

**`could not tell whether <name> is running: <error>. Check with `comfy-qat list --live``**

`disconnect` closes the tunnel and deliberately leaves the machine on, so it has
to say whether that machine is actually billing. Asking Google failed —
usually an expired login (`gcloud auth login`). The machine's state is unchanged
by this: it is whatever it was before the command ran. `comfy-qat list --live`
asks again.

Not knowing is reported rather than assumed, because the two guesses available
here are "you are paying for something you are not" and "you are not paying for
something you are", and this tool has shipped both.

## Getting onto a box

**`<name> is this machine — open a terminal`**

`ssh` reaches cloud boxes. The local install is already here, so there is nothing
to connect to — open a terminal window.

**`<name> is not running, so there is nothing to open a shell on. SSH needs the
machine up, not just declared.`**

The everyday one: you forgot to `comfy-qat up`. `comfy-qat up <name>` starts it,
and then `ssh` works. Exit **2** — nothing was reached and nothing was changed.

This is checked here because gcloud's own answer to it is a 36-line Python
traceback ending in exit 255, with a suggested `gcloud compute ssh
--troubleshoot` that fails the same way on the same box. `comfy-qat logs` asks
the identical question and answers it in one sentence, so the tool was
contradicting itself between two commands run a second apart.

The price is one extra read of the instance's state before every `ssh`, which is
the read `logs` already pays for the same reason.

**`could not tell whether <name> is running, so there is no saying whether it
will take a shell.`**

Asking Google what the machine is doing succeeded and came back with no state at
all — see [A read that said nothing](#a-read-that-said-nothing), which is the
same third answer reaching `logs`. It is not guessed in either direction:
assuming "running" hands you the traceback above, and assuming "stopped" tells
you to start a box that may already be billing. `comfy-qat list --live` asks
again, and it is usually transient.

**`<name> runs Windows, which has no ssh here`**

Windows Server boxes are reached over Remote Desktop, not SSH. `comfy-qat rdp
<name>` resets the password, prints it, and forwards RDP to `localhost:33389`.

**`<name> is not a Windows cloud box`**

`rdp` is only for Windows. For a Linux box use `comfy-qat ssh <name>`; for the
local install, open a terminal.

**`<name> is not running, so its password was not reset and there is nothing to
forward RDP to. The password in use on it is unchanged.`**

`rdp` asks whether the box is up before it announces anything, and this is the
answer when it is not. The last sentence is the point of the message: `rdp` used
to print "resetting the Windows password on `<name>` — the one in use now stops
working", plus gcloud's warning about losing data encrypted with the old
password, *before* it checked, and then fail. Anyone reading that had been told a
destructive change was under way when nothing had been touched — and the recovery
it invites, resetting again and warning whoever else signs in to that box, is
work created out of a sentence. Exit 2: nothing was changed. `comfy-qat up
<name>`, then `rdp` again.

**`Remote Desktop is not answering on comfy-win after 300s, so nothing was changed
on the machine. It is running and billing.`**
**`its password was not reset — the one in use on it is unchanged.`**
RUNNING is not "Remote Desktop is listening", exactly as it is not "sshd is
listening" and as "the VM booted" is not "ComfyUI is serving". A freshly created
Windows box reaches RUNNING **minutes** before 3389 accepts anything, and `rdp`
waits that gap out rather than walking into it. Exit 2: nothing was changed.

This is what the wait exists to prevent, from a real run on a fresh box:

    password reset in 8s
    user     ali_ranjah
    password <redacted>
    address  localhost:33389
      forwarding RDP — Ctrl-C closes it
    ERROR: ... [4003: 'failed to connect to backend']. (Failed to connect to port 3389)

exit 1 — with the password already changed. **The reset does not undo**, and it
invalidates the password anyone else signed in to that box is holding, so that run
cost a session and bought nothing: the box was not ready, and a retry minutes later
got straight through. The check above the reset used to be "is the box RUNNING",
which that box was.

If you see the 300s form, the box really is not coming up into Remote Desktop.
Give it longer; a Windows first boot is slow, and the machine bills throughout.

**`could not tell whether <name> is running, so its password was not reset — this
will not claim to have changed one it could not reach.`**

The same check, with the third answer. `instance_status` can succeed and say
nothing — a box in another project, a read that came back empty — and neither of
the other two answers may be assumed from it. Guessing RUNNING announces a
destructive change against a machine nobody reached; guessing stopped tells you
to start a box that may already be billing. Exit 2. `comfy-qat list --live`
settles it.

**`--tail <n> is not a number of lines to read.`**

`comfy-qat logs --tail` used to fold anything below one up to a single line, so
`--tail -5` quietly printed one line and looked like it had worked. A negative
count is a typo, not a smaller number of lines, and answering a typo with a
plausible answer is how nobody finds out. `--tail 0` is *not* refused: zero means
zero, and `--tail 0 --follow` is the ordinary "skip the backlog, show me what
happens next" reading. Exit 2; nothing is contacted.

**`gcloud reset the Windows password on <instance> and exited without an error,
but the answer carried no credentials: <shape>. There is no password to hand over,
so nothing was forwarded.`** / **`gcloud reset the password on <instance> but
reported no username and no password, so there is nothing to sign in with`**

Two messages for one condition, from two layers, and both are deliberate. The
first comes from the call itself, which knows what shape the answer was. The
second is `rdp`'s own check, which does not trust its caller to have raised —
because the cost of being wrong here is a person typing a blank password into a
machine they cannot reach.

The reset ran and gcloud exited cleanly, but what came back has no credentials in
it — an empty response, half a pair, or a field renamed on Google's side. Exit 1;
nothing is printed and the RDP forward does not start.

This is refused rather than shown because the failure otherwise looks exactly like
success: a blank user over a blank password is laid out in the same labelled column
as a real pair, under a line saying the forward is starting, and the only symptom
is a Windows login prompt you cannot pass — with nothing in this tool's output
pointing back at it.

Run the reset yourself and read what comes back; the message prints the full
command for the box, zone and project it used:

```sh
gcloud compute reset-windows-password <instance> --zone=<zone> --project=<project>
```

Two causes account for most of it: the box is not RUNNING — the guest agent has to
be up to accept a reset, so start it first — or the account signed in lacks
`compute.instances.setMetadata` on that instance.

**`the reset ran out of clock, which settles nothing: the request had already
reached Google, so the password on <instance> may have been changed anyway — and
nothing here ever saw the new one.`**

The reset did not answer inside `PASSWORD_TIMEOUT`, which is 180 seconds. It is
printed under gcloud's own `gcloud timed out after 180s` line, and it exists
because that line on its own reads as "nothing happened", which is the one thing
nobody can say here.

A reset is not a single call. gcloud writes a public key into the instance's
metadata, waits for that update, and only then polls the serial port for the guest
agent to answer with the encrypted password. The metadata write goes first — so a
timeout anywhere after it leaves the password changed on the box and unreadable
from here. **The client ran out of clock; the operation did not stop.** Exit 1,
not 2, for exactly that reason: 2 in this tool means nothing was changed.

So the old password may already have stopped working, for you and for anyone else
who signs in to that box. Run the reset yourself and read the new one off the
screen:

```sh
gcloud compute reset-windows-password <instance> --zone=<zone> --project=<project>
```

If it is slow every time rather than once, the box is the likely cause: a Windows
instance that has just booted has not started its guest agent yet, and the reset
waits on an agent that is not listening. `comfy-qat list --live` says whether it is
RUNNING; a minute or two after a start is normal.

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
entry that was never repointed after a `comfy-qat move`. `comfy-qat list` shows
what is tunnelled; `comfy-qat down comfy-win` then `comfy-qat open
comfy-win` rebuilds the tunnel.

**`no evidence line was printed, because this one would have named the wrong
machine`** / **`to fix: check the port in your host list and which tunnel is open,
then stamp it again`**
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

**`no such environment: testclod`**
A typo in an environment name. The fix line lists the only ones there are:
`known environments: testcloud, stagingcloud, cloud, local`.

**`cloud was not probed, so there is no evidence for it`**
You asked for the evidence block of an environment that was not in the run. The fix
line names what *was* probed this run. Either add it as a target too, or drop the
other targets: `--evidence` reports on what was probed, not on everything that
exists.

**--expect needs exactly one cloud environment, e.g. `comfy-qat env testcloud
--expect <sha>`**
`--expect` compares one environment against one SHA, so it needs exactly one cloud
target named. With none or several there is nothing unambiguous to compare.

**`testcloud serves 4f2a1b9c, expected 9d7e3a10`** (exit code 1)
The mismatch is on stderr, like every other failure, so `--json` output stays a
document. A pass instead prints `ok    testcloud serves 9d7e3a10 as expected` on
stdout, and prints nothing at all under `--json`, where the exit code says it.

The environment is not running the build you named — which is the whole point of
the check, and usually means the deploy did not land rather than that you typed the
wrong SHA. Confirm the SHA in the frontend repo before assuming the deploy failed.

**`--json and --evidence produce different output; pick one`**
Two answers to the same question. Asking for both used to silently discard one.

**`warning: --flags only changes the evidence block; add --evidence <env>`**
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
role, not a credential. The message names the exact permission. `comfy-qat
status` shows which account you are actually using — being signed in as the wrong
one of two accounts is the usual cause.

**`no active gcloud account`** / **`nobody signed in`**
Run `gcloud auth login`.

**`no project set`** / **`to fix: comfy-qat setup`**
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
comfy-qat quota request --gpu l4 --region us-central1
```
`--quota-id <id>` takes a raw Google quota id instead, if you would rather name one
exactly.

**`name a card to ask for`**
`quota request` was run with nothing to request. It will not guess a card for
you — asking for the wrong one wastes days of approval time. The fix line offers
`comfy-qat quota request --gpu l4,a100`, or `--quota-id`, to name a raw quota
id exactly.

**`a project-wide allowance only, no specific card granted`**
`GPUS-ALL-REGIONS-per-project` is a ceiling on how many GPUs you may run in total.
It is not permission to run any particular card, and on its own it starts nothing —
which is why this reads as a failure rather than a pass. Ask for an actual card:
```sh
comfy-qat quota request --gpu l4 --region us-central1
```

**`this project reports no quota for 'l4' in europe-west4, it is metered in us-central1`**
You have that card, somewhere else. Quota is granted per region, so an L4 approved
in `us-central1` does nothing for a box in `europe-west4`. Either build in the
region that has it, or request it where you want it. The older wording said
`no quota for 'l4' … Available: L4`, which read as a contradiction; the region is
the missing half.

**`there is no point asking for P100 quota: this tool cannot bring that card up. The driver it installs is the open NVIDIA kernel module, which needs a GPU System Processor, and only Turing and newer cards have one.`**
`quota request` refuses P4, P100, V100 and K80 before submitting anything.
Approval takes days, and at the end of it `create --gpu p100` would still refuse —
see the entry for that message under [Creating a box](#before-anything-exists).
Ask for a T4 or an L4 instead.

**`only P100, V100 — cards this tool cannot drive, because the open NVIDIA kernel module it installs needs a GPU System Processor and only Turing and newer have one`**
`comfy-qat status` reads this as a failing row rather than a passing one. The
project holds GPU quota, Google will let you start an instance with it, and every
card granted is one whose GPU cannot initialise under the driver this tool
installs — so nothing it can build will generate anything. Request a card it can
drive:
```sh
comfy-qat quota request --gpu t4,l4 --region us-central1
```

**`this project reports no quota for 'h100'`**
You asked for a card Google does not offer this project, or not in that region.
The fix line lists what it does offer — `ask for one of: A100, L4, T4` — and
`comfy-qat quota` shows the same thing with current limits.

**`request for l4 failed: ...`**
The quota request was rejected on submission. The most common cause is an account
with no billing history — Google frequently will not grant GPU quota until a project
has been billed at least once. Retrying will not change that.

**`still pending: l4, a100. Approval can take days — run this again to keep
waiting, or `comfy-qat quota` to check.`** (exit code 75)
Not an error. The requests went in but have not been approved within the wait window.
Approval can take days. Run the same command again to keep waiting, or
`comfy-qat quota` to check. The console link printed with the request shows the
same thing.
