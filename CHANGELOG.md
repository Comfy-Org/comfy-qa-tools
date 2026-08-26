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

- `go` swallowed the real failure and then tried SSH against a stopped box. (#16)
- Capacity stockouts were reported as generic start failures; they are now named,
  and the zone Google suggests is repeated back. (#17, #18)
- Quota parsing crashed on the shape Google actually returns. (#9)
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
