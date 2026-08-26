# Changelog

What has actually shipped, newest first. Features are listed when they land on
`main`, not when they are planned.

## Unreleased — release 1: `host` and `auth`

Code complete. Awaiting an end-to-end pass on a real project by someone who did
not write it ([`docs/test-criteria.md`](docs/test-criteria.md)).

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
  the original. (#19)
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

- A tunnel was trusted on the strength of a process id alone. Pids are recycled,
  so a stale pid file read as "tunnel already open" and `down` would SIGTERM
  whatever now owned that number. The record now names the instance, zone, port
  and the moment the process started, and all of it has to still fit.
- `open` never looked at the port. Anything else already holding it — another
  box's tunnel, a dev server, a run that never died — meant the URL handed back
  answered for that instead. It is now refused rather than reported.
- `stamp` accepted any JSON as ComfyUI, and followed redirects, so a health
  endpoint stamped as a machine and a 302 could report the local Mac under a
  cloud box's name. Both are refused; a hostile field can no longer forge parts
  of the evidence line.
- `go` swallowed the real failure and then tried SSH against a stopped box. (#16)
- Capacity stockouts were reported as generic start failures; they are now named,
  and the zone Google suggests is repeated back. (#17, #18)
- Quota parsing crashed on the shape Google actually returns. (#9)

### Documentation

- Four pages plus `comfy-qat guide`; every error the tool can print has an entry
  in `troubleshooting.md`, enforced by a test. (#3, #7, #8)
- The README and the docs are tested against the real command surface, so a
  feature cannot ship while the page still calls it "coming next".

## v0

- `env` — report the build and feature-flag state of every deployed environment.
  Carried forward unchanged under the new binary; it gets rewritten when its own
  release comes round.
