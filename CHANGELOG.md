# Changelog

What has actually shipped, newest first. Features are listed when they land on
`main`, not when they are planned.

## 1.0.0 — release 1: `host` and `auth`

Code complete, and numbered accordingly: the command surface below is the one
this tool is committing to, so `0.1.0` was describing a different project. Still
awaiting an end-to-end pass on a real project by someone who did not write it
([`docs/test-criteria.md`](docs/test-criteria.md)) — which is the reason the
build now has a version worth quoting.

### Machines

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
