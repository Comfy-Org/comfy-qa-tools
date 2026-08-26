# Test criteria — release 1, end to end

A tester who did not write this tool runs it top to bottom on a real Google Cloud
project. Every step is a copy-paste block; every block prints its own markers, so
the whole terminal can be pasted back as the result.

**How to run it.** Paste one block at a time. Do not skip a phase — later phases
depend on earlier ones. If a block fails, paste the terminal and stop there; that
is a result, not a wasted run.

**Set this once**, in every terminal you use:

```sh
QAT=/path/to/venv/bin/comfy-qat   # wherever pip put it
$QAT --help >/dev/null && echo "binary ok"
```

Phases E and F **start a cloud GPU box and bill for it**. Phase G stops it. Do not
leave the run half finished overnight.

---

## Phase A — install and surface *(offline, no cloud, no cost)*

```sh
echo "=== A1 version and clean import"; $QAT --help 2>&1 | head -20
echo "=== A2 no stale binary"; which comfy-qa-cli; which "comfy-qa"; echo "exit $? (1 = clean)"
echo "=== A3 host surface"; $QAT host --help 2>&1 | sed -n '/Commands/,$p'
echo "=== A4 auth surface"; $QAT auth --help 2>&1 | sed -n '/Commands/,$p'
echo "=== A5 quota surface"; $QAT auth quota --help 2>&1 | sed -n '/Commands/,$p'
echo "=== A6 guide"; $QAT guide
echo "=== A7 tests"; python -m pytest tests/ -q 2>&1 | tail -3
```

- [ ] **A1** — help prints; no traceback, no import error.
- [ ] **A2** — neither older binary is on `PATH` from this project. Both `which` calls come back empty.
- [ ] **A3** — `host` lists exactly: list, init, discover, up, open, down, go, move, stamp.
- [ ] **A4** — `auth` lists exactly: status, login, quota.
- [ ] **A5** — `auth quota` lists exactly: list, request.
- [ ] **A6** — the guide names `setup` first, then `host list` and `auth status`.
- [ ] **A7** — every test passes.

## Phase B — the host list rules *(offline, no cost)*

Uses a throwaway host list, so your real one is untouched.

```sh
T=$(mktemp -d); echo "=== B0 scratch $T"
echo "=== B1 init"; $QAT host init --config $T/hosts.toml; cat $T/hosts.toml | head -20
echo "=== B2 init refuses to clobber"; $QAT host init --config $T/hosts.toml; echo "exit $?"
echo "=== B3 list"; $QAT host list --config $T/hosts.toml
echo "=== B4 bare host == list"; $QAT host --config $T/hosts.toml 2>&1 | head -3
printf '\n[hosts.bad]\nkind = "gce"\nos = "Ubuntu 22.04"\ngpu = "L4"\ngce_instance = "bad"\ngce_zone = "us-central1-a"\ngce_project = "p"\nport = 8188\n' >> $T/hosts.toml
echo "=== B5 cloud host on 8188 is refused"; $QAT host list --config $T/hosts.toml; echo "exit $?"
sed -i '' 's/^port = 8188$/port = 8190/2' $T/hosts.toml 2>/dev/null || sed -i 's/^port = 8188$/port = 8190/2' $T/hosts.toml
printf '\n[hosts.dupe]\nkind = "gce"\nos = "Ubuntu 22.04"\ngpu = "L4"\ngce_instance = "dupe"\ngce_zone = "us-central1-a"\ngce_project = "p"\nport = 8190\n' >> $T/hosts.toml
echo "=== B6 duplicate port is refused"; $QAT host list --config $T/hosts.toml; echo "exit $?"
printf '\n[hosts.typo]\nkind = "gce"\ngce_zoen = "us-central1-a"\nport = 8191\n' >> $T/hosts.toml
echo "=== B7 typo'd field is refused"; $QAT host list --config $T/hosts.toml; echo "exit $?"
echo "=== B8 missing file"; $QAT host list --config $T/nope.toml; echo "exit $?"
```

- [ ] **B1** — writes the file and says where; the starter has `local` on 8188.
- [ ] **B2** — refuses, names `--force`, exit 2. Nothing overwritten.
- [ ] **B3/B4** — a table with NAME KIND OS GPU URL; bare `host` prints the same.
- [ ] **B5** — names port 8188 and the local ComfyUI. Exit 2, not a traceback.
- [ ] **B6** — names both hosts and the shared port. Exit 2.
- [ ] **B7** — names `gce_zoen` as unknown. Exit 2.
- [ ] **B8** — says there is no host list and how to make one. Exit 2.
- [ ] **B9** — every message in B5–B8 is findable in `docs/troubleshooting.md`.

## Phase C — sign-in, billing, quota *(cloud reads only, no cost)*

```sh
echo "=== C1 status"; $QAT auth status
echo "=== C2 status json"; $QAT auth status --json | head -30
echo "=== C3 login prints, never signs in"; $QAT auth login
echo "=== C4 quota (about a minute)"; time $QAT auth quota list
echo "=== C5 one region"; $QAT auth quota list --region us-central1
echo "=== C6 by region"; $QAT auth quota list --by-region 2>&1 | head -15
echo "=== C7 quota json"; $QAT auth quota list --json 2>&1 | head -20
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
echo "=== D1 real host list"; $QAT host list
echo "=== D2 stamp local"; $QAT host stamp local
echo "=== D3 stamp json"; $QAT host stamp local --json
echo "=== D4 open on a local host"; $QAT host open local
echo "=== D5 stamp with ComfyUI down (stop it first)"; $QAT host stamp local; echo "exit $?"
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
echo "=== E1 discover is idempotent"; $QAT host discover --dry-run
echo "=== E2 tunnel command"; $QAT host open $BOX --dry-run
echo "=== E3 go (long; leave it running)"; $QAT host go $BOX
```

Then, in a **second terminal** while `go` is still running:

```sh
QAT=/path/to/venv/bin/comfy-qat; BOX=comfy-win
echo "=== E4 stamp the cloud box"; $QAT host stamp $BOX
echo "=== E5 tunnel is recorded"; ls -l ~/.config/comfy-qa-tools/tunnels/
echo "=== E6 open twice does not stack"; $QAT host open $BOX
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
echo "=== F1 up on a box that is already up"; $QAT host up $BOX
echo "=== F2 go with --no-install on a box that has ComfyUI"; $QAT host go $BOX --no-browser --no-install
echo "=== F3 move, plan only"; $QAT host move $BOX --dry-run
echo "=== F4 unknown host"; $QAT host stamp not-a-machine; echo "exit $?"
echo "=== F5 unknown host, lifecycle"; $QAT host down not-a-machine; echo "exit $?"
```

- [ ] **F1** — recognises it is already up and serving; does not restart anything.
- [ ] **F2** — serves without reinstalling.
- [ ] **F3** — either "started, no move needed", or a numbered plan naming the snapshot, the new disk, the new instance and the target zone, then stops. **Nothing is created.**
- [ ] **F4/F5** — names the host as unknown and lists what is declared. Exit 2, no traceback.
- [ ] **F6** *(opportunistic)* — if a start ever fails on capacity, the message says it is a stockout, names a zone that has capacity, and does not blame quota or billing.

## Phase G — stop paying *(do not skip)*

```sh
echo "=== G1 down"; $QAT host down $BOX
echo "=== G2 tunnel is gone"; ls -l ~/.config/comfy-qa-tools/tunnels/ 2>&1
echo "=== G3 nothing answers"; $QAT host stamp $BOX; echo "exit $?"
echo "=== G4 instance is TERMINATED"; gcloud compute instances list
echo "=== G5 down again is harmless"; $QAT host down $BOX; echo "exit $?"
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
echo "=== H4 setup is safe to re-run"; $QAT setup --non-interactive 2>&1 | tail -20
```

- [ ] **H1** — only `hosts.toml` and `tunnels/`. Nothing outside that directory.
- [ ] **H2** — no token, no key. `grep` finds nothing.
- [ ] **H3** — only lines you added yourself; the tool has not edited your shell config.
- [ ] **H4** — skips what is already done, changes nothing, does not overwrite the host list, and does not prompt.

---

## Reporting the result

Paste the whole terminal. For anything that failed, the useful facts are: the
phase and check id, what it printed, and the exit code. A check that could not be
run — no capacity, no second box — is "not run", not a pass.

A release-1 pass needs: every box in phases A–D and G–H ticked, E3 and E4 ticked,
and no unexplained traceback anywhere in the run.
