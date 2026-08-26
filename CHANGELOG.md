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

- `go` swallowed the real failure and then tried SSH against a stopped box. (#16)
- Capacity stockouts were reported as generic start failures; they are now named,
  and the zone Google suggests is repeated back. (#17, #18)
- Quota parsing crashed on the shape Google actually returns. (#9)
- A misspelt field in `hosts.toml` was reported as five missing fields that were
  all present. `gce_zoen` now names itself, and suggests `gce_zone`.

### Documentation

- Four pages plus `comfy-qat guide`; every error the tool can print has an entry
  in `troubleshooting.md`, enforced by a test. (#3, #7, #8)
- The README and the docs are tested against the real command surface, so a
  feature cannot ship while the page still calls it "coming next".

## v0

- `env` — report the build and feature-flag state of every deployed environment.
  Carried forward unchanged under the new binary; it gets rewritten when its own
  release comes round.
