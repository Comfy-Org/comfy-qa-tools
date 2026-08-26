# Test criteria — release 1, end to end

A tester who did not write this tool installs it from nothing and runs it top to
bottom on a real Google Cloud project. Every step is a copy-paste block; every
block prints its own markers, so the whole terminal can be pasted back as the
result.

**How to run it.** Paste one block at a time, in order. Later phases depend on
earlier ones. If a block fails, paste the terminal and stop there; that is a
result, not a wasted run.

Phases E and F **start a cloud GPU box and bill for it**. Phase G stops it. Do not
leave the run half finished overnight.

## The preamble

Paste this once per terminal — including the second terminal in phase E. It sets
the four paths everything else uses and gives you a `qat` command that says so
plainly if the tool is not installed yet, instead of a wall of shell errors.

```sh
VENV=~/ComfyUI/venv                  # the virtualenv to install into
REPO=~/qa-tools-test                 # a fresh clone, so nothing existing is in the way
QAT=$VENV/bin/comfy-qat
PY=$VENV/bin/python
qat() { [ -x "$QAT" ] || { echo "STOP - not installed. Run phase 0."; return 127; }; "$QAT" "$@"; }
echo "venv $( [ -x "$PY" ] && echo ok || echo MISSING ) - tool $( [ -x "$QAT" ] && echo installed || echo "not installed yet" )"
```

Any virtualenv on Python 3.11+ works; `~/ComfyUI/venv` is 3.12 and already here.

---

## Phase 0 — from scratch *(no cost)*

Removes every existing install and your current host list, so phases B and C test
the real first-run path rather than a machine that was already set up.

```sh
echo "=== 0.1 remove any existing install"
$VENV/bin/pip uninstall -y comfy-qa-tools comfy-qa-cli comfy-qa 2>&1 | tail -4
echo "=== 0.2 no binary left behind"
ls $VENV/bin | grep -i "comfy-qa" || echo "clean"
echo "=== 0.3 set your host list aside (restoring it is one command, at the end)"
[ -e ~/.config/comfy-qa-tools ] && mv ~/.config/comfy-qa-tools ~/.config/comfy-qa-tools.before-test
ls -d ~/.config/comfy-qa-tools* 2>&1
echo "=== 0.4 fresh clone of main"
rm -rf "${REPO:?}" && git clone -q https://github.com/Comfy-Org/comfy-qa-tools.git "$REPO" && git -C "$REPO" log --oneline -1
echo "=== 0.5 install exactly as the README says"
cd "$REPO" && $VENV/bin/pip install -e . 2>&1 | tail -3
echo "=== 0.6 does it run"
"$QAT" --help >/dev/null 2>&1 && echo "binary ok at $QAT" || echo "FAIL - no binary at $QAT"
```

- [ ] **0.1** — uninstalls without touching anything else. A "not installed" for
      the two old names is a pass, not a failure.
- [ ] **0.2** — nothing matching `comfy-qa` is left in the venv's `bin`.
- [ ] **0.3** — your real host list is safe at `~/.config/comfy-qa-tools.before-test`
      and the live path no longer exists.
- [ ] **0.4** — the clone is at the same commit as `origin/main`.
- [ ] **0.5** — installs from the checkout with no build error.
- [ ] **0.6** — `binary ok`. **If this fails, stop: nothing after it can pass.**

Now re-run **the preamble** so `qat` picks up the new install.

---

## Phase A — install and surface *(offline, no cloud, no cost)*

```sh
echo "=== A1 version and clean import"; qat --help 2>&1 | head -20
echo "=== A2 no stale binary"; which comfy-qa-cli; which "comfy-qa"; echo "exit $? (1 = clean)"
echo "=== A3 host surface"; qat host --help 2>&1 | sed -n '/Commands/,$p'
echo "=== A4 auth surface"; qat auth --help 2>&1 | sed -n '/Commands/,$p'
echo "=== A5 quota surface"; qat auth quota --help 2>&1 | sed -n '/Commands/,$p'
echo "=== A6 guide"; qat guide
cd "$REPO"; echo "=== A7 tests"; "$PY" -m pytest tests/ -q 2>&1 | tail -3
```

- [ ] **A1** — help prints; no traceback, no import error.
- [ ] **A2** — neither older binary is on `PATH` from this project. Both `which` calls come back empty.
- [ ] **A3** — `host` lists exactly: list, init, discover, up, open, down, go, move, stamp.
- [ ] **A4** — `auth` lists exactly: status, login, quota.
- [ ] **A5** — `auth quota` lists exactly: list, request.
- [ ] **A6** — the guide names `setup` first, then `host list` and `auth status`.
- [ ] **A7** — every test passes.

## Phase B — the host list rules *(offline, no cost)*

Uses throwaway host lists in a scratch directory, so your real one is untouched.
Each rule gets its own file: a check that shares a file with the check before it
can pass for the wrong reason.

```sh
T=$(mktemp -d); echo "=== B0 scratch $T"
echo "=== B1 init"; qat host init --config $T/hosts.toml; head -14 $T/hosts.toml
echo "=== B2 init refuses to clobber"; qat host init --config $T/hosts.toml; echo "exit $?"
echo "=== B3 list"; qat host list --config $T/hosts.toml
echo "=== B4 bare host == list"; qat host --config $T/hosts.toml
gce() { printf '[hosts.%s]\nkind = "gce"\nos = "Ubuntu 22.04"\ngpu = "L4"\ngce_instance = "%s"\ngce_zone = "us-central1-a"\ngce_project = "p"\nport = %s\n\n' "$1" "$1" "$2"; }
{ echo '[hosts.local]'; echo 'kind = "local"'; echo 'port = 8188'; echo; gce bad 8188; } > $T/b5.toml
echo "=== B5 cloud host on 8188 is refused"; qat host list --config $T/b5.toml; echo "exit $?"
{ echo '[hosts.local]'; echo 'kind = "local"'; echo 'port = 8188'; echo; gce one 8190; gce two 8190; } > $T/b6.toml
echo "=== B6 duplicate port is refused"; qat host list --config $T/b6.toml; echo "exit $?"
{ echo '[hosts.local]'; echo 'kind = "local"'; echo 'port = 8188'; echo; printf '[hosts.typo]\nkind = "gce"\ngce_zoen = "us-central1-a"\nport = 8190\n'; } > $T/b7.toml
echo "=== B7 typo'd field is refused"; qat host list --config $T/b7.toml; echo "exit $?"
{ echo '[hosts.nokind]'; echo 'port = 8190'; } > $T/b8.toml
echo "=== B8 missing kind is refused"; qat host list --config $T/b8.toml; echo "exit $?"
{ echo '[hosts.local]'; echo 'kind = "local"'; echo 'port = 8188'; echo; printf '[hosts.noport]\nkind = "gce"\nos = "Ubuntu 22.04"\ngpu = "L4"\ngce_instance = "noport"\ngce_zone = "us-central1-a"\ngce_project = "p"\n'; } > $T/b9.toml
echo "=== B9 cloud host with no port is refused"; qat host list --config $T/b9.toml; echo "exit $?"
echo "=== B10 missing file"; qat host list --config $T/nope.toml; echo "exit $?"
```

- [ ] **B1** — writes the file and says where; the starter has `local` on 8188.
- [ ] **B2** — refuses, names `--force`, exit 2. Nothing overwritten.
- [ ] **B3** — a table with NAME KIND OS GPU URL.
- [ ] **B4** — bare `host` prints the same table as B3, and accepts `--config`.
- [ ] **B5** — names host `bad` and port 8188 and the local ComfyUI. Exit 2.
- [ ] **B6** — names `one` and `two` and the shared port 8190. Exit 2.
      **Not the 8188 message** — that would mean it stopped at an earlier rule.
- [ ] **B7** — names `gce_zoen` as an unknown field. Exit 2.
- [ ] **B8** — says `kind` must be `local` or `gce`. Exit 2.
- [ ] **B9** — says a `gce` host needs an explicit port. Exit 2.
- [ ] **B10** — says there is no host list and how to make one. Exit 2.
- [ ] **B11** — every message in B5–B10 is findable in
      [troubleshooting.md](troubleshooting.md) by pasting it in.

## Phase C — sign-in, billing, quota *(cloud reads only, no cost)*

```sh
echo "=== C1 status"; qat auth status
echo "=== C2 status json"; qat auth status --json | head -30
echo "=== C3 login prints, never signs in"; qat auth login
echo "=== C4 quota (about a minute)"; time qat auth quota list
echo "=== C5 one region"; qat auth quota list --region us-central1
echo "=== C6 by region"; qat auth quota list --by-region 2>&1 | head -15
echo "=== C7 quota json"; qat auth quota list --json 2>&1 | head -20
```

- [ ] **C1** — one line per check: gcloud, account, project, billing, GPU quota. Stops at the first failure rather than printing five.
- [ ] **C2** — valid JSON, same facts, **no credential or token anywhere in it**.
- [ ] **C3** — prints commands for you to run; does not open a browser.
- [ ] **C4** — one row per card with LIMIT / WHERE / STATUS; warns first that it takes about a minute; finishes well under 240s.
- [ ] **C5/C6** — narrowing works and the numbers agree with C4.
- [ ] **C7** — valid JSON with `project`, `gpus`, `by_region`.
- [ ] **C8** — if nothing is usable, it prints the exact `quota request` command to fix that.

## Phase D — the local machine *(no cost)*

Start your local ComfyUI first if it is not running.

```sh
echo "=== D1 real host list"; qat host list
echo "=== D2 stamp local"; qat host stamp local
echo "=== D3 stamp json"; qat host stamp local --json
echo "=== D4 open on a local host"; qat host open local
echo "=== D5 stamp with ComfyUI down (stop it first)"; qat host stamp local; echo "exit $?"
```

- [ ] **D1** — your real machines, including every cloud box on the project.
- [ ] **D2** — one line: host, ComfyUI version, OS, device, torch, python. Correct against what ComfyUI's own `/system_stats` says.
- [ ] **D3** — the same values under ComfyUI's field names.
- [ ] **D4** — says local needs no tunnel and prints the URL. Does not start anything.
- [ ] **D5** — says nothing answered, names the URL, exit 1. Not a traceback.

## Phase E — a cloud box, start to serving *(this bills)*

Replace `BOX` with a cloud host from `host list`.

```sh
BOX=comfy-win
echo "=== E1 discover is idempotent"; qat host discover --dry-run
echo "=== E2 tunnel command"; qat host open $BOX --dry-run
echo "=== E3 go (long; leave it running)"; qat host go $BOX
```

Then, in a **second terminal** while `go` is still running:

```sh
BOX=comfy-win   # paste the preamble above this line first
echo "=== E4 stamp the cloud box"; qat host stamp $BOX
echo "=== E5 tunnel is recorded"; ls -l ~/.config/comfy-qa-tools/tunnels/
echo "=== E6 open twice does not stack"; qat host open $BOX
```

- [ ] **E1** — reports every box already present; adds nothing; writes nothing.
- [ ] **E2** — prints a `gcloud compute start-iap-tunnel` line using the port from your host list, and does not run it.
- [ ] **E3** — says what it is doing at each step: starting, tunnelling, checking for ComfyUI, installing if absent, serving. The ComfyUI startup log appears on the terminal. A browser opens on the local URL and ComfyUI loads.
- [ ] **E4** — the stamp names the **cloud** box's GPU and OS, not your laptop's. This is the single most important check in the run.
- [ ] **E5** — a `.pid` and a `.log` for that host.
- [ ] **E6** — says a tunnel is already open and gives the pid. Does not open a second.
- [ ] **E7** — Ctrl-C in the first terminal stops ComfyUI and says the box is still running, with the `down` command.

## Phase F — the failure paths *(this bills)*

Only F1 and F2 are always runnable; F3 depends on Google being out of capacity.

```sh
echo "=== F1 up on a box that is already up"; qat host up $BOX
echo "=== F2 go with --no-install on a box that has ComfyUI"; qat host go $BOX --no-browser --no-install
echo "=== F3 move, plan only"; qat host move $BOX --dry-run
echo "=== F4 unknown host"; qat host stamp not-a-machine; echo "exit $?"
echo "=== F5 unknown host, lifecycle"; qat host down not-a-machine; echo "exit $?"
```

- [ ] **F1** — recognises it is already up and serving; does not restart anything.
- [ ] **F2** — serves without reinstalling.
- [ ] **F3** — either "started, no move needed", or a numbered plan naming the snapshot, the new disk, the new instance and the target zone, then stops. **Nothing is created.**
- [ ] **F4/F5** — names the host as unknown and lists what is declared. Exit 2, no traceback.
- [ ] **F6** *(opportunistic)* — if a start ever fails on capacity, the message says it is a stockout, names a zone that has capacity, and does not blame quota or billing.

## Phase G — stop paying *(do not skip)*

```sh
echo "=== G1 down"; qat host down $BOX
echo "=== G2 tunnel is gone"; ls -l ~/.config/comfy-qa-tools/tunnels/ 2>&1
echo "=== G3 nothing answers"; qat host stamp $BOX; echo "exit $?"
echo "=== G4 instance is TERMINATED"; gcloud compute instances list
echo "=== G5 down again is harmless"; qat host down $BOX; echo "exit $?"
```

- [ ] **G1** — says it closed the tunnel and stopped the machine.
- [ ] **G2** — the `.pid` for that host is gone.
- [ ] **G3** — nothing answered on that port. Exit 1.
- [ ] **G4** — the instance shows TERMINATED. **If it does not, the tool has left you billing and that is a blocker.**
- [ ] **G5** — does not fail on an already-stopped box.

## Phase H — the promise the README makes

```sh
echo "=== H1 footprint"; find ~/.config/comfy-qa-tools -type f | sed "s|$HOME|~|"
echo "=== H2 no credential anywhere in it"; grep -rIl -e "ya29." -e "-----BEGIN" ~/.config/comfy-qa-tools/ 2>&1; echo "exit $? (1 = clean)"
echo "=== H3 shell untouched"; grep -c "comfy-qat" ~/.zshrc ~/.bashrc 2>/dev/null
echo "=== H4 setup is safe to re-run"; qat setup --non-interactive 2>&1 | tail -20
```

- [ ] **H1** — only `hosts.toml` and `tunnels/`. Nothing outside that directory.
- [ ] **H2** — no token, no key. `grep` finds nothing.
- [ ] **H3** — only lines you added yourself; the tool has not edited your shell config.
- [ ] **H4** — skips what is already done, changes nothing, does not overwrite the host list, and does not prompt.

---

## Putting your machine back

```sh
echo "=== restore the host list"
rm -rf ~/.config/comfy-qa-tools && mv ~/.config/comfy-qa-tools.before-test ~/.config/comfy-qa-tools
ls -R ~/.config/comfy-qa-tools
echo "=== drop the test clone"
rm -rf "${REPO:?}"
```

The install stays where phase 0 put it. If you normally run this from a
development checkout of your own, reinstall from there with `pip install -e .`
once the clone is gone — `-e` means whichever checkout you installed last is the
one the binary runs.

## Reporting the result

Paste the whole terminal. For anything that failed, the useful facts are: the
phase and check id, what it printed, and the exit code. A check that could not be
run — no capacity, no second box — is "not run", not a pass.

A release-1 pass needs: every box in phases A–D and G–H ticked, E3 and E4 ticked,
and no unexplained traceback anywhere in the run.
