# Changelog

What has actually shipped, newest first. Features are listed when they land on
`main`, not when they are planned.

## 1.1.0 — the deprecation window closes, and the first real hardware

Two things, and the second is the larger one. One release of notice, and the
second spellings are gone — nothing there adds a capability, it removes ways of
typing the ones that were already there. Alongside it, this tool was driven end
to end against real GCE hardware for the first time: a box created from nothing,
a move between zones, a prune against a live project, a Windows password reset.
Almost every fix below comes from those runs. A fake cloud answers instantly,
always has capacity, and never sends a bill, so the failures collected here are
the ones that could only be found by paying for them.

### Removed

- **`comfy-qat host <verb>` and `comfy-qat auth <verb>`.** Every verb has stood on
  its own at the top level since 1.0.0, and these were the same `Command` objects
  registered a second time under a hidden noun — **42 invocable paths for 22
  distinct implementations**, walked out of the live Click tree rather than
  counted by hand. They were hidden from `--help` and warned on stderr each time
  anyone used one, naming the verb to type instead. That is what made them a
  window rather than a second permanent spelling, and 1.0.0 said the window would
  close at the next minor. This is it. **42 command paths become 22.** Typing one
  now gets click's `No such command 'host'.` and exit 2 — take the noun off, and
  the rest of the line is right. [`docs/commands.md`](docs/commands.md) has the
  whole mapping.

- **`comfy-qat guide`.** One line, printing the first-run text. A first-run text
  you have to know a command name to reach is not serving first runs, so the text
  moved to where the question is actually asked: **bare `comfy-qat` with no host
  list now prints it**, under the help, in place of the single "start with
  `setup`" line it used to print there. Same words, one fewer command, and
  reachable by somebody who knows nothing yet.
  [`docs/getting-started.md`](docs/getting-started.md) still has the long version.

### Changed

- Bare `comfy-qat` with no host list prints `--help` and then the full first-run
  text. It used to print `--help` and one line.

- The acceptance pack moved with the surface, in the same commit, because a run
  sheet that types a command the build no longer has is how a criterion comes to
  be ticked on faith. `A5a`/`A5b` check the old spellings are **gone**; `A6`
  checks the first-run text where the root callback prints it; `B4` checks the
  bare `comfy-qat` listing that the bare `host` one became; `A3` lists 20
  commands rather than 21.

- **`NO_COLOR` is honoured.** Nothing this tool writes itself has ever been
  coloured, and a test holds it to that — but `comfy-qat --help` is rendered by
  Typer through Rich, and came out in 348 escape sequences on a terminal that had
  set the variable. Rich reads it; Typer builds its own console and did not pass
  it on. The help keeps its layout and loses the colour.

- Advice blocks are readable rather than merely present. A `to fix:` offering
  more than one thing used to run four commands and two sentences together at one
  indent, all reading as things to paste; the sentences now open their groups,
  and `#` notes on a run of commands line up in one column instead of being
  spaced by eye. `warning:` stays welded to its own sentence rather than being
  stranded a line above it when a caller asks for air.

### Fixed — the ones that cost money

- **`discover --prune` removed the entry for a machine that was running.**
  Absence was inferred from a bulk `instances list` keyed on `(name, zone)`, so
  an entry whose declared zone was wrong — a hand-typed field, a box recreated
  from the console, a `move` that did not finish — read as "not on the project
  any more", was removed, and exited 0. The L4 it named went on billing with
  nothing left in the host list to reach it, so neither `down` nor `list` could
  see it again. Absence is now decided on the **name across the whole project**;
  a name found in another zone is reported as a zone mismatch and **kept**,
  because the entry is wrong and the box is not gone.
- **And an inference is no longer enough to remove anything.** Every candidate is
  now put to `gcloud compute instances describe`, by name, in its own zone, and
  pruned only on a flat not-found — Google saying so about that machine, rather
  than a listing it failed to appear in. That is one gcloud call per candidate:
  a prune with five stale entries makes five, and says how many entries it is
  about to check before it checks them; a host list with nothing stale in it pays
  nothing. Three details carry the weight. `DENIED` is classified before
  `NOT_FOUND`, so a credential that may not see an instance raises rather than
  confirming an absence. A not-found about the **zone** or the **project** raises
  too, because one mistyped `gce_project` would otherwise answer "not found" for
  every entry naming it and a single `y` would delete the lot. And `instances
  list` printing nothing at all is now an error instead of an empty project — it
  used to read as "you have no machines", which pruned every entry in a
  hand-maintained file that has no other copy on the machine.
- **`move --clean` deleted the previous run's work before deciding it would
  refuse.** The cleanup ran at the top of the command and the guards fifteen
  lines below it, so a move refused by the GPU ceiling had already destroyed the
  snapshot and the 200-300 GB disk that make a resumed move cheap. Every refusal
  that a delete cannot change is now computed first. Verified on hardware: a
  refused `--clean` leaves a 14.3 GB snapshot and a 200 GB disk intact.
- **`move` now rehearses the host-list rewrite before it creates anything.** That
  rewrite is the move's seventh action, and nothing checked it: a host list that
  parses and loads but is spelled in a way the rewrite does not recognise
  (`[hosts."comfy-win"]` and `[ hosts.comfy-win ]` are both valid TOML) failed at
  the end, with a snapshot, a disk and a running GPU already paid for and
  `comfy-qat down comfy-win` still pointing at the old box. The rehearsal is the
  real write minus the write, through the same code, so the two cannot drift.
- **The snapshot ran under a clock written for starting an instance.** Copying a
  disk is not that operation: 200 GB is the default boot disk this tool creates,
  a first snapshot of one is a full copy at Google's pace, and a real move went
  past the 300-second limit — gcloud killed, Google still holding the snapshot in
  `UPLOADING`, and the tool reporting a failure about a resource that was being
  created and would bill. The snapshot has its own hour-long timeout now, clear
  above the whole range rather than at the edge of it.
- **The move's GPU-ceiling guard failed open.** It counted the source's cards out
  of a bulk listing, so a source the listing did not carry, or a row without
  `guestAccelerators`, both read as "no cards needed" — and zero skipped the quota
  read and the check with it, leaving Google to refuse the create after the
  snapshot and the disk existed. The requirement now comes off the `describe`
  payload `move` already holds, and a host declared with a card whose payload
  shows none counts as one rather than none. The quota read is also no longer
  skipped whenever nothing currently holds a card: `held + needed > ceiling` is
  true at `held == 0` when the ceiling is 0, which is a lapsed quota under an
  existing box, and the check that would have caught it could not be reached.
- **`move` started a box you had deliberately stopped, and left it running.**
  Finding out whether a zone has capacity means trying to start the machine, and
  on the path where the answer is "there is capacity here, no move needed" the
  probe was never put back. Proven on hardware: `down comfy-linux`, then `move
  comfy-linux --clean --yes`, and the box came back RUNNING and billing on the
  strength of a question. The state is read before the probe and a box found
  stopped is stopped again; a box found running is left alone and told so.
- **`move --clean` on that same path cleaned nothing at all.** It decided there
  was nothing to move and returned before looking at a single resource, so a user
  running it to tidy up was told everything was fine while the same snapshot and
  disk went on billing. What earlier moves of that box left behind is now found
  from the resources themselves — in whatever zone they are — reported whether or
  not `--clean` was passed, and deleted on `--clean` or an answered prompt.
- **`create` billed a box against a host list it could not read.** The failure was
  swallowed, the instance was created, and the entry that would have made it
  reachable could not be written. It refuses now, before anything exists.

### Fixed — getting onto the box

- **`go` straight after `create` could not open a tunnel, and blamed your
  credentials.** RUNNING is Google's word for "powered on"; it is not "sshd is
  listening", and on a box created moments ago it is not even "this machine will
  stay up", because the startup script installs the NVIDIA driver and reboots it.
  `create` says so in its closing line and promises `go` waits it out — and `go`
  reported a tunnel that closed for unknown reasons, with `gcloud auth login` as
  the fix, on a session that was fine. The same command four minutes later
  installed ComfyUI and served it. A still-booting machine is now a shape the
  tunnel recognises and waits on, for up to five minutes; a box that is genuinely
  ready never enters the wait; and only that shape is retried, because an expired
  credential, a taken port and a tunnel pointing at another machine do not get
  better by being asked again and every retry is GPU time.
- **`up` spent three minutes to say something that named two states and committed
  to neither.** "ComfyUI is not installed or not started", followed by advice to
  get onto the box by hand — on a machine that has been through `down`, which is
  every machine that has ever been stopped. It now asks the box what is on it and
  says which one it is, and hands over `comfy-qat go` either way. Measured on
  hardware: `go` had ComfyUI answering in 52 seconds, immediately after `up` had
  spent 180 concluding the opposite. `up` is the machine-level verb and does not
  start ComfyUI; `go` is the one that serves.
- **A tunnel bound IPv4 only**, so a browser that prefers IPv6 for `localhost` got
  an error page for a tunnel that was working — `curl http://127.0.0.1:8190/`
  returned 200 while `curl -6 'http://[::1]:8190/'` could not connect. Both
  families are forwarded now, and `ExitOnForwardFailure=no` is pinned rather than
  left to the default, so that a user's own `~/.ssh/config` cannot let a failed
  IPv6 bind take the working IPv4 forward down with it.
- **A dead tunnel was explained with the previous session's error.** The log is
  written fresh for each tunnel rather than appended to, so it holds one tunnel's
  output; `down` and `disconnect` remove the `.pid` and the `.json` — the two
  records that claim a tunnel is there — and keep the `.log`, which is the only
  place the reason exists and is exactly what somebody goes looking for after a
  tunnel dies. The diagnosis also reads the whole of that log rather than its last
  six lines: gcloud's final line is its own exit status restated, and the line
  saying why had scrolled off the top of the tail.
- **`rdp` announced a password reset on a machine it had not reached.** It now
  checks the box is running first, says the password in use is unchanged, and
  hands over `comfy-qat up`. A Ctrl-C during the reset is reported the way every
  other mutating call reports one — the password may already have changed.

### Fixed — what it tells you

- **`logs` exited 0 when the read failed.** A read that came back non-zero now
  says that what is above it is not the whole log. Ctrl-C on a follow exits 130
  rather than 0 — it is "ended by SIGINT", not a failure, and it is what stops a
  script reading `$?` from taking an interrupted follow for a log that ended by
  itself. `--tail 0` means zero lines instead of one, and a negative `--tail` is
  refused with the two spellings that do what was probably meant.
- **`.bak` held a state that existed for milliseconds.** `discover --prune` writes
  twice — an append for what it adopted, a rewrite for what it removed — and each
  write kept its own copy, so after a run that both added and pruned,
  `hosts.toml.bak` held the file with the additions already in it. One copy per
  command now: `.bak` answers "what did this look like before I ran that".
- **A `--clean` that could not write a backup found out at the end.** Whether the
  copy `apply` insists on can be made at all is knowable from the local file, so
  it is decided before the move rather than after the snapshot.
- `discover` reports what it did not do as carefully as what it did: entries whose
  box is on the project in another zone, and entries whose absence could not be
  confirmed, are each named with the reason and left alone.
- `disconnect` says whether the machine it left behind is still running and
  billing, and hands over `down`. Killing the ssh yourself frees the port in
  silence, and silence after unplugging from a GPU box reads as "finished".
- `create`'s stockout advice printed `--os Ubuntu 22.04`, which is the image's
  name and not something the flag accepts; it offers `--os linux`, which runs.
  The commands it prints are shell-quoted, so a value with a space in it is still
  one argument.
- `quota request` reported a failure without the fix line it had been given.
- `delete` and `rdp` lost their interrupt report when the call they were making
  raised something other than an ordinary error; a Ctrl-C at the wrong moment said
  nothing about a machine that may already have been destroyed.
- `down --all` no longer says "Nothing is now" when something it could not account
  for is still there.

### The suite

- **8,159 passing with 29 skipped, to 8,414 passing with 11.** The skips that went
  away were turned into assertions rather than deleted: a test that skipped itself
  when a docstring quoted no path now fails if none of them quotes one, and checks
  the path it finds. Real hardware wrote most of the new cases — a prune against a
  live project, a move that was refused, a create followed by a `go` — and each of
  the fixes above arrived with the test that would have caught it.

## 1.0.0 — release 1: `host` and `auth`

Code complete, and numbered accordingly: the command surface below is the one
this tool is committing to, so `0.1.0` was describing a different project. Still
awaiting an end-to-end pass on a real project by someone who did not write it
([`docs/test-criteria.md`](docs/test-criteria.md)) — which is the reason the
build now has a version worth quoting.

**The `host` and `auth` nouns are a deprecation window, not a second permanent
spelling.** Every verb below is also reachable at the top level without its noun,
and that shorter spelling is the one to learn. The nouns are hidden from `--help`
and kept working so that nothing written down before the move breaks. **They close
at the next minor release** — a version and not a date, because `pyproject.toml`
is the one place the version is written and `--version` reads it back. *(They did:
see 1.1.0.)*

### Machines

- `host create` — the box itself, which was the one thing still made by hand in
  the console. `--os linux --gpu t4` is the whole command: the machine type
  follows from the card (an L4 is a G2 with the GPU built in and refuses
  `--accelerator`; a T4 is an N1 with one attached), Windows gets
  `enable-windows-ssh=TRUE` without which nothing here can reach it, and Linux
  gets Google's own driver startup script, because the NVIDIA driver is not in
  the base image and a box without it runs ComfyUI on the CPU while looking
  healthy. **The zone is chosen, not typed**: regions the project holds quota in,
  zones inside them offering the card and the machine type, ranked by latency
  measured from this machine — the `<region>-<service>.googleapis.com` names all
  resolve to one anycast front end, so the regional `compute.<region>.rep.` ones
  are what get timed — and tried in order, falling through on a stockout.
  Quota is read before anything exists, the card's grant *and* `GPUS_ALL_REGIONS`,
  the project-wide ceiling that is 1 here and is the limit that actually bites.
  `--zone`, `--region`, `--name`, `--disk`, `--yes`, `--dry-run`.

- `host init`, `host list`, `host discover` — declare the machines you test on,
  or have them read out of Google Cloud. Bare `host` lists, because read-only is
  the safe default. (#1, #11)
- `host up`, `open`, `down` — start a box, tunnel to it over Identity-Aware Proxy,
  stop it again. "Up" means ComfyUI answers, not that the VM booted: a box that
  serves nothing looks like success and bills like success. (#14)
- `host go` — the everyday command. Start, tunnel, install ComfyUI if the box has
  none, then serve it in the foreground with its startup log on your terminal. (#15)
- `host move` — a GPU stockout cannot be fixed where you are. Snapshot the disk,
  rebuild the box in a zone that has capacity, keep the install, and never remove
  the original. Reads the boot disk off the instance rather than guessing its name,
  resumes a move that stopped part-way instead of colliding with what it left,
  reports every leftover with its size and the command that removes it, and keeps
  the new disk the same type as the one it copies. (#19)
- `host switch` — change machine in one command. Start the one you want, then
  stop whichever other cloud box was running or tunnelled, which is the half
  people forget and the half that bills. `--dry-run`, `--keep-others`.
- **Say what you want, not what you called it.** Every command that takes a host
  now also takes a description of one: `windows`, `l4`, `windows/l4`. It is used
  only when exactly one declared host fits, the host it picked is printed, and
  two candidates are refused with both named. Names always win.
- `host list` gained a STATE column — which box you are tunnelled to, free and
  offline. `--live` adds what Google says about each instance.
- A box that will not start now says where else you can work. A GPU stockout
  used to end with a four-step rebuild; it now offers the other declared
  machines first, same OS first, and the rebuild second.


- `host stamp` — one pasteable line saying what a machine actually is, from
  ComfyUI's own `/system_stats`. `--json` uses ComfyUI's field names. (#6)

### Google Cloud

- `setup` — one command for the whole first run: sign-in, project, billing, GPU
  quota, host list. Works with `--non-interactive`. (#4, #12)
- `auth status`, `auth login` — readiness, stopping at the first real failure.
  gcloud owns credentials; this tool never sees, stores or prints one. (#2)
- `auth quota list`, `auth quota request` — one row per card rather than 130 rows
  of raw quota, several cards per request, and a wait that hands you back rather
  than blocking on an approval that can take days. (#5, #9, #13)

### Fixes worth knowing

- **A stockout could put the box in a zone you had ruled out.** `--zone` means
  this zone or nothing, and `order_zones` said exactly that in the note it
  attached to the ordering — but `build` never read the note. Google's refusal
  names another zone, that zone went to the front of the queue, and the box was
  created there: billing, in the one place the caller had excluded, and reported
  as a success. The same path walked out of `--region`, and out of the regions
  the project holds any quota in at all. A suggestion is now only ever followed
  inside the ordering that was already chosen, and never when `--zone` was given.
- **`MAX_ATTEMPTS` was documented and not enforced.** Six, "because each attempt
  is a real instance create that takes the better part of a minute when it
  fails" — and `build` capped nothing, so a queue that every refusal could refill
  drained no faster than it grew. Giving up at the cap now says it was a cap, not
  the whole world, because "everywhere is short" and "I stopped after six" are
  different facts and only one of them means waiting will not help.
- **`--disk` had a floor and no ceiling.** The disk bills by the gigabyte
  provisioned from the moment the box exists, so `--disk 20000` for `2000` is one
  keystroke and eighteen silent terabytes. Capped at 4 TB.
- The project-wide ceiling was counted in boxes, not in cards. One
  `a3-highgpu-8g` is a single instance and **eight** of `GPUS_ALL_REGIONS`, so an
  H100 block already running read as holding one — and the gate waved through a
  create Google then refused, after the zone probing and after somebody had
  confirmed it. A running GPU box reporting no `acceleratorCount` is now read as
  holding one rather than none, because an unfamiliar payload is not evidence of
  an empty machine.
- An instance name is now checked against Google's rule before anything is
  contacted, rather than a minute of quota reads, four latency probes and a
  confirmation prompt later. Python thinks `é` and `ボ` are alphanumeric; Google
  does not, so a name could survive cleaning and still be refused.
- `--zone me-west1-a` sailed past the quota gate that already refused `--region
  me-west1`, and found out from gcloud instead.
- The latency cache is now stamped with a version, written atomically, and holds
  only numbers a connection could have taken. Unstamped, a file measured against
  the anycast `<region>-<service>.googleapis.com` names — four near-identical
  numbers that look like measurements and rank nothing — would have outlived the
  fix for a week. It also no longer remembers a region that did **not** answer:
  one run behind a dropped VPN used to write "unreachable" for everything and
  keep it for seven days, leaving the ordering alphabetical long after the
  network came back. A negative or `NaN` round trip is discarded rather than
  ranked first — `json.loads` accepts a bare `NaN`, and one of them makes
  `sorted` return an order that depends on its input, which breaks the one thing
  the ranking promises: that a dry run and the real run try the same zones.
- **`host move --dry-run` started a GPU instance.** Nothing answers "where is
  there an L4 free", so `move` finds out by trying to start the box and reading
  the suggested zone out of the refusal — and `--dry-run` was not consulted until
  well after that had happened. The one command that promises to change nothing
  was the one that could quietly cost the most. `--dry-run` now needs a `--to`,
  says so, and prints the whole plan without contacting anything billable.
- `host stamp` gave a cloud box the local machine's advice: "start ComfyUI on
  that machine, or check the port in your host list", when the real reasons
  nothing answered were no tunnel and a stopped instance. A `gce` host with no
  tunnel open is now pointed at `host open` and `host go`, and told the box may
  simply be stopped. With a tunnel up the probe's own advice is kept, because
  then it knows more than the host list does.
- One exit code for the whole `host` group: **2 means nothing was changed**, **1
  means the work started and failed** — and a `to fix:` line is never dropped.
  `move` used to exit 1 with no fix where `auth quota list` and `host discover`
  exited 2 with one, on the same gcloud error.
- The host list enforced that every host had its own port, and never that every
  host was its own machine. Two entries could name one GCE instance — two ports,
  two tunnels, one box, and a matrix recording "reproduced on comfy-win, not on
  comfy-win-b" about the same machine. Refused when the file is read, along with
  two names that differ only in case, a cloud box called `local`, and a `local`
  host carrying `gce_*` fields — that last one because `down` decides what to
  stop from `kind`, so a mistyped cloud box read as a successful `host down`
  while the GPU billed all night. Host names must now be typeable: not blank,
  not padded, not a path, and not something argument parsing reads as an option.
- `load` promised a message and gave a stack trace instead for a `--config`
  pointed at the folder rather than the file in it, a host list with the wrong
  owner, and one that is not UTF-8. `host init` did the same for a folder it
  could not write to.
- `open` reported "tunnel already open" from the name alone, so a tunnel opened
  for a different box that happened to share a name was claimed as this one, with
  this host list's URL beside it. The question is now asked where it can be
  answered — against the recorded instance, zone, project and port.
- `open`, `up` and `go` did not catch `TunnelError`, so a port already held
  produced a traceback rather than the message that was written for it.
- `stamp` printed a clean evidence line for whatever answered. A host declared
  Windows/L4 answering `darwin`/`mps` is now refused rather than warned about:
  the line exists to be pasted into a bug report as proof of which machine ran
  something, and a warning on stderr does not survive being copied. The card half
  of that check compared the declared `gpu` to the answering device as a
  substring, and the two names for one card do not contain each other:
  `A100-80GB` is not inside `NVIDIA A100-SXM4-80GB`. Three of the six cards
  `host discover` can write therefore contradicted themselves, which under a
  refusal costs a tester the command on a correct machine, over a string nobody
  typed by hand. Cards are compared as whole words now, which also stops an
  `L40S` passing as an `L4`.
- A tunnel was trusted on the strength of a process id alone. Pids are recycled,
  so a stale pid file read as "tunnel already open" and `down` would SIGTERM
  whatever now owned that number. The record now names the instance, zone, port
  and the moment the process started, and all of it has to still fit.
- `open` never looked at the port. Anything else already holding it — another
  box's tunnel, a dev server, a run that never died — meant the URL handed back
  answered for that instead. It is now refused rather than reported.
- `stamp` reported fields it had not actually been told. Comfy Cloud sends `os`,
  `python_version` and `pytorch_version` as empty strings, and the line printed
  them as facts while `--json` disagreed about which ones it had. A field that
  cannot be read is now absent from both. Cloud is also reached on its `/api`
  alias instead of being called a wrong port, VRAM no longer rounds a 256 MB
  device to `0GB`, four identical cards are one fact rather than 180 characters,
  and 401/403/404 are told apart from nothing answering at all.
- `stamp` accepted any JSON as ComfyUI, and followed redirects, so a health
  endpoint stamped as a machine and a 302 could report the local Mac under a
  cloud box's name. Both are refused; a hostile field can no longer forge parts
  of the evidence line.
- `go` swallowed the real failure and then tried SSH against a stopped box. (#16)
- Capacity stockouts were reported as generic start failures; they are now named,
  and the zone Google suggests is repeated back. (#17, #18)
- Quota parsing crashed on the shape Google actually returns. (#9)
- A misspelt field in `hosts.toml` was reported as five missing fields that were
  all present. `gce_zoen` now names itself, and suggests `gce_zone`.
- An expired Google session was discovered after the box had started and was
  already billing. Anything billable now proves the credential first, and a
  reauth challenge is offered the terminal instead of failing against a pipe.
  A dropped connection, a missing project and a denied permission had all been
  reported as "your session expired"; each now says what it is. (#21)

### Saying what you ran

- `comfy-qat --version` — prints `comfy-qat 1.0.0`, and the short commit as well
  when it is running from a checkout. A tool whose whole pitch is "record what
  produced this result" could not name its own build, which made every report it
  produced unciteable. The number is read back from the installed package
  metadata, never restated in Python: `version` in `pyproject.toml` is the only
  place it is written down, and a test fails if a second copy appears.
- Installing is now tested, not assumed: CI builds the package, installs it into
  a throwaway venv and runs `comfy-qat --version` from outside the checkout, so
  a missing console script or a file the wheel forgot to ship fails the build
  rather than the first person to `pip install` it.
- CI runs on Python 3.11, 3.12 and 3.13, and on macOS as well as Ubuntu — every
  user of this tool is on a Mac, and `sed`, paths and process handling differ
  there. `ruff` lints the tree on the same run.


### Documentation

- Four pages plus `comfy-qat guide`; every error the tool can print has an entry
  in `troubleshooting.md`, enforced by a test. (#3, #7, #8)
- The README and the docs are tested against the real command surface, so a
  feature cannot ship while the page still calls it "coming next".
- `docs/session-expiry.md` — why the Google sign-in keeps expiring mid-pass, what
  a tester does about it, and the ranked list of what would make it happen less,
  including which options only a Workspace admin has. (#21)

## v0

- `env` — report the build and feature-flag state of every deployed environment.
  Carried forward unchanged under the new binary; it gets rewritten when its own
  release comes round.
