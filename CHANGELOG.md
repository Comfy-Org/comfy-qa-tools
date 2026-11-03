# Changelog

What has actually shipped, newest first. Features are listed when they land on
`main`, not when they are planned.

## 1.4.0 — choose the build, and the Windows driver installs itself

Until now every install took whatever was at the tip of ComfyUI's default branch
that minute, and a Windows GPU box needed a person at a PowerShell prompt before
it could use its card. Both are choices or nothing now. `create`, `go` and
`switch` take **the build**: a ComfyUI release, PR or commit, a frontend release
or PR, Manager (on by default), and a torch pin. **comfy-cli does the installing,
on the box.** `stamp` says **the build that is actually running**, read off the
machine. And `create` gives a Windows GPU box Google's own driver script at first
boot, so nothing is left to run by hand.

**What ran on real Google boxes for this release, on 2026-11-03.** On commit
`0e63be5` (the build code), a Linux box with no GPU: `create --version v0.43.0
--frontend-version v1.58.4` wrote the build into the host list; `go` installed it
with comfy-cli in four minutes; and `stamp` showed ComfyUI 0.43.0 at the tag's own
commit `426f5eea52`, frontend 1.58.4 — served, while the package v0.43.0 installed
beside it is 1.57.4 — Manager on, and torch 2.14.1+cpu. Then `switch --pr 17497`
changed the build in place in a minute, and `stamp` showed `PR 17497, 9350a08d80`,
the PR's head on GitHub. The box was stopped and deleted with `down` and `delete`.

On `e5d8e73`, every refusal of bad input was asked of the real GitHub with gcloud
signed out, so each answer came back before any gcloud call: a release, a PR and a
commit that do not exist, a closed PR, a merged PR (pointed at its merge commit),
a frontend release that does not exist, and two backends at once.

Then a Windows Server 2022 box with no GPU, made by `create --version v0.42.1` on
`e5d8e73` with the SSH package fix below, answered SSH within a minute. The first
two `go` runs on it, on `e5d8e73`, stopped where the install called `winget`, which
that image does not have (fixed below). The next, on `a38d549`, installed Git and
Python from their publishers' installers and comfy-cli installed v0.42.1, but
ComfyUI could not start, for two more reasons older than this release, found then
and fixed below: the Visual C++ runtime torch needs was not there, and a process
started from an SSH session did not outlive it. On `ecb3557`, `go --clean`
installed the runtime and v0.42.1 again in 8m38s and served it, and `stamp` showed `ComfyUI 0.42.1
(63be2f9b82)` — tag v0.42.1's own commit on GitHub — frontend 1.55.18, Manager on,
torch 2.15.0+cpu and Python 3.12.10. It was stopped and deleted. After everything,
nothing named `qatest-` was left on the project in instances, reservations, disks
or snapshots, and the project ran no instance at all.

**What did not run.** The one GPU run. `create --os windows --gpu l4 --version
v0.43.0 --no-reserve` on `ecb3557` had the quota and found no L4 free in any of
the six zones it tried (us-central1-a and -b, us-west4-c and -a, us-west1-a and
-b), so it created nothing, and it was not tried again. So Google's Windows driver
script at first boot, a Windows install with `--nvidia`, and `stamp` showing a GPU
have run only against the suite's fakes. So has a frontend PR built on a box. So
have these, which no live run asked for: a GPU install through comfy-cli on Linux
(`--nvidia` with `--cuda-version`; every live install was `--cpu`), a `--torch` pin,
an install with `--commit` (only its refusal ran), `--no-manager`, and custom nodes
and models surviving a build change (none had been added to the box `switch`
changed). Four fixes made after the live run, from the release's audit, have run
only against the suite's fakes too: a PR asked for again after new commits being
fetched again, a release held to its tag's commit, a backend chosen with no
frontend getting the frontend it pins (and `--frontend-version pinned`), and the
build kept by every `create` a refusal hands back.

### Added

- **`--version`, `--pr` or `--commit`** on `create`, `go` and `switch` choose the
  ComfyUI to install: a release tag, an open pull request, or a commit. At most
  one; two at once is refused before anything reaches Google. None keeps what the
  tool always did — the tip of ComfyUI's default branch — and every command says
  which build it chose, the default included.
- **`--frontend-version` or `--frontend-pr`** choose the frontend, independently
  of the backend. A release is ComfyUI's own `--front-end-version`, fetched at
  startup; a PR is built on the box by comfy-cli's own builder (the one `comfy
  launch --frontend-pr` uses; Node.js and pnpm are installed for it) and served
  with `--front-end-root`. `--frontend-version pinned` is the frontend the
  ComfyUI build pins, asked for by name: it puts that back on a box whose record
  names a release, and leaves the backend alone. Left off, a command that chooses
  the backend (`--version`, `--pr` or `--commit`) gets the frontend that backend
  pins — `switch --pr 17497` on a box serving frontend 1.58.4 serves the PR's
  own — and a command that chooses no backend keeps the frontend the box has; a
  plain `go`, which chooses nothing, keeps the whole build.
- **ComfyUI-Manager is on by default** (Ali, 3 Nov: "manager should always be on
  btw, by default"). A plain install, a plain `go` included, has it, and ComfyUI
  is started with `--enable-manager`. `--no-manager` leaves it out; `--help`
  says so.
- **`--torch`** pins PyTorch, with the CUDA build after a `+`: `--torch
  2.12.1+cu130` is Miles's recommendation for NVIDIA machines, quoted in
  [machines.md](docs/machines.md#pinning-pytorch); it is his, not a default.
  The pin reaches every `pip install` comfy-cli runs, as a pip constraint. With
  none, torch is chosen as before, for the box's driver.
- **comfy-cli installs ComfyUI on the box**, replacing the hand-rolled `git
  clone` and pip. A pinned comfy-cli (1.24.0) goes in a venv of its own beside
  ComfyUI, and `comfy install` runs with the build's options, each command
  printed before it runs. comfy-cli runs with `DO_NOT_TRACK=1`, so it does not
  pay its five-second telemetry flush on every command, and ComfyUI is still
  started by this tool's own launch, never by `comfy launch --background`, which
  refuses a second server even on a free port.
- **A new build on a box that has ComfyUI keeps its custom nodes and models**:
  `go` or `switch` with a build option changes it in place (`comfy install
  --restore`), stopping a ComfyUI already serving first. Only what is named
  changes; a frontend release alone is a launch option, not an install.
  Asking again for a PR that has had commits since the box checked it out
  fetches them: the checkout is held to the head GitHub reports now, not to the
  PR's number. `--clean` deletes ComfyUI on the box and installs from nothing.
- **Bad input is refused before anything is created or charged.** A release
  that does not exist, a PR that does not exist or is closed (a merged one is
  pointed at the commit it landed as), an unknown commit, a frontend release
  that is not a GitHub release: each is asked of GitHub, read-only — `gh api`
  when it is installed, the public API otherwise — and refused in plain words
  with the fix, exit 2.
- **A create handed back by a refusal keeps the build.** The same create
  offered after a stock-out, after `--zone` or `--region`, and by every refusal
  `create` makes before anything exists — a disk too small or too large, no such
  operating system, a card it cannot drive, a box with no GPU and `--reserve`, a
  missing `--os` or `--gpu` with nobody to ask — carries the build options as
  they were given, `--no-manager` included. The create `move` hands back to make
  a reserved box again somewhere else carries its host list's `build`. Each of
  them dropped it, so pasted back the box came up on the tip of ComfyUI's
  default branch with Manager on; live, the GPU run's own retry lost `--version
  v0.43.0`.
- **Ready means the build you asked for.** Once ComfyUI answers, what it is
  running is read back and compared with what was asked: the ComfyUI version and
  torch from `/system_stats`, the frontend from the bundle it is serving, Manager
  from whether Manager answers, the commit and PR from git on the box. A
  release is held to its tag's commit, not only its version string, which
  master's tip and a PR branched from the tag report too; a PR is held to the
  head GitHub reported for it. A contradiction is said plainly — `ComfyUI
  0.42.1, not v0.43.0`, `commit abcdef0123, not v0.43.0's commit 426f5eea52`,
  `PR 17497 at 0000000000, not its head 9350a08d80` — with the command
  that puts the build back, which names the whole build, Manager included, so that
  it holds a box that is still serving to it; nothing is reported ready or opened.
  A part that cannot be read is said to be unconfirmed and does not block, and a
  frontend PR is always one: nothing a running ComfyUI answers names the PR its
  frontend was built from.
- **`stamp` shows the build that is running**: `ComfyUI 0.43.0 (PR 17497,
  9350a08d80) · frontend 1.58.4 · Manager on · … · torch 2.14.1+cpu`, and the
  same in `--json` as `comfyui_commit`, `comfyui_pr`, `frontend_version` and
  `manager`. Each is read off the machine, never off what was asked for, and one
  that cannot be read is left out.
- **The host list's `build` field**, which `create` writes when it is given a
  build: the build the first `go` installs on that box. It holds what was asked
  for, not a PR's head on the day `create` ran: the first `go` checks the PR out
  at its head then.

### Changed

- **A Windows GPU box installs its NVIDIA driver itself.** `create` sets the
  `sysprep-specialize-script-url` metadata key to Google's own
  `install_gpu_driver.ps1`, the method Google's "Install GPU drivers" page now
  documents for exactly this, and `go` waits for the driver on Windows as it
  does on Linux. The two PowerShell commands `create` printed are gone. A
  driver that does not come up points at the serial console, where the script's
  output is. The Linux path is unchanged.

### Fixed

- **`down --all` reads gcloud's project whenever the host list names none**
  (carried over from the 1.3.1 audit, behaviour:F2). With a box declared, no
  `project` line and gcloud pointed at another project, `create` makes boxes on
  gcloud's project, and `down --all` read only the declared box's — so it could
  close on `Nothing is now.` while a stray box billed there. A gcloud that cannot
  name its project withholds the all-clear. cost.md and troubleshooting.md say so.
- **No comment calls `state:STOCKOUT` Google's own state code** (carried over,
  behaviour:F4). No real refusal on record carries it; the test comment now says
  so, as `lifecycle._CAPACITY_SIGNS` does, and a test holds every file to it.
- **A Windows box is made with the SSH server Google says it needs.** `create`
  set `enable-windows-ssh=TRUE` alone, which installs no SSH server; Google's own
  command for a Windows VM reached over SSH also sets
  `sysprep-specialize-script-cmd` to `googet -noconfirm=true install
  google-compute-engine-ssh`. A box made the old way on the live run never
  listened on port 22 and `go` waited out its 300 seconds. Every Windows box gets
  both now; the SSH package runs before the driver script on first boot.
- **A Windows box gets Git and Python without `winget`.** The install got both
  from `winget`, which Windows Server 2022 does not have — Microsoft lists it for
  Windows 10, 11 and Server 2025 only. On the live run `go` printed `The term
  'winget' is not recognized` twice, then found no `py` either, and stopped at
  `INSTALL_INCOMPLETE: comfy-cli did not install`, so no Windows box has ever
  installed ComfyUI from nothing. 1.3.0 and 1.3.1 have the same defect: their
  Windows install calls the same two `winget` lines (as did 1.2.0). Git 2.56.0.4
  and Python 3.12.10 now come from git-for-windows' and python.org's own
  installers, each refused unless its SHA-256 is the pinned one, run silently,
  and judged by its exit code and by the file it leaves; the venv is made with
  that Python by its path, and a Windows launch puts Git on its PATH for
  Manager.
- **A Windows box gets the Visual C++ runtime torch needs.** Google's
  windows-2022 image does not carry it, and with Git and Python in place the
  live run's ComfyUI died at `import torch`: `Microsoft Visual C++
  Redistributable is not installed` and `[WinError 126] ... c10.dll or one of
  its dependencies`. The install now fetches it from Microsoft's permalink,
  refuses it unless Microsoft signed it, runs it with Microsoft's documented
  `/install /quiet /norestart`, and checks the registry key Microsoft says
  records it, skipping all of that where the key is already there. 1.3.0 and
  1.3.1 never install it either.
- **ComfyUI started on a Windows box outlives the SSH call that starts it.** The
  detached launch used `Start-Process`, and on the live run a process started
  that way never reached its first line once its SSH session had returned, while
  the same body started through WMI ran to its end a minute later; so `go`
  printed `STARTED` and then found neither a ComfyUI nor a log. The launch goes
  through WMI (`Win32_Process.Create`) now, says `STARTED` only when WMI started
  it, and gives that process Git's directory on its own PATH. The detached
  launch has used `Start-Process` since it was added; 1.2.0, 1.3.0 and 1.3.1
  all have it.
- **IAP's `4047: 'Failed to lookup instance'` on a box just made is a box still
  starting.** The first `go` on the first two boxes the live run created, started
  7 s (Linux) and 19 s (Windows) after `create` returned, was refused with it
  within seconds, and `go` reported a failed tunnel; the same command run again a
  minute or two later opened its tunnel. `go` now waits it out.

### The suite

Four new test files — the build itself, the build through `go`/`switch`/`stamp`,
the build through `create`, and the Windows driver — each run red before the
code it holds. The fake ComfyUI can serve a frontend and Manager's route, and the
suite refuses any test that would ask the real GitHub. Tests that pinned the old
`git clone` install, the hand-run Windows driver and the old metadata now pin the
new ones.

## 1.3.1 — its own Google project, and the leftovers from 1.3.0

Two things. comfy-qat now keeps **its own Google project**, so moving it to a new
project is one line and moves nothing else on the machine. And what the check
before 1.3.0 merged left open is fixed: a crash in `discover`; a `--region`
stock-out that was wrong on a one-region project, offered back a box that was
not the one asked for, and was half untested; printed commands that did not run
as printed, and gcloud errors that were misread; and two places the 1.3.0 docs
were behind the facts.

**What ran on real Google boxes for this release, on 2026-11-03, on commit
`acf517c`**: `discover` adopting a reserved box with no GPU beside an unreserved
one, neither in a host list that has no local machine in it. 1.3.0 crashed on
the same two boxes; this adopts both, on ports 8191 and 8192, and marks only the
reserved one. Then `down` and `delete --yes` — for the reserved box exactly as
`discover` printed them — run with no terminal, stopped and deleted both boxes
and released the reservation. The commits after `acf517c` change none of the
code that run used. Everything else here ran against the suite's fakes only.
`setup` was not run live, because it can ask Google for quota.

### Changed

- **comfy-qat keeps its own Google project, in the host list.** One
  `project = "<id>"` line at the top of the host list says where `create` makes a
  box, what `discover` lists, and what `quota` and `status` read. `setup
  --project <id>` writes it, and no longer runs `gcloud config set project` —
  that moved every other gcloud user on the machine with comfy-qat. Every gcloud
  call that acts on a project now names it with `--project`, and so does every
  gcloud command printed for you to paste, so the line reaches Google whatever
  gcloud is set to. Without the line, gcloud's current project is used, as
  before, and each command that does says so. A misspelt line, or a project's
  display name where its ID goes, is refused rather than ignored.
- **Switching project keeps the old boxes reachable.** Each entry keeps its own
  `gce_project`, so a box made before the switch is still started, stopped and
  deleted where it is, until you delete it; there is no move between projects.
  `list` says which project each box is on, and where a new one will go.
- **A printed command is the command and nothing else.** Offered commands carried
  their reason after them — a `# note`, a dash, words aligned into a column,
  `, then wait for Google` — which an interactive zsh, as it starts, passes to the
  command as arguments, and the command refuses them. The reason is now a line of
  its own above the command, ending in the colon that introduces it. So is the
  reason `setup --no-quota-request` gives for leaving `--region` off when that
  region does not sell a card it would ask for, which came after the command,
  behind a dash. `setup`'s closing lines offer `comfy-qat stamp <name>` only for
  a local machine your host list declares; it offered `stamp local` to host
  lists with no `local` in them.
- **The tool no longer breaks a command across lines when it wraps what it
  prints.** Some were broken once their values were in: the deletes in `move`'s
  report of what an earlier move left, and in its note about a slower boot disk,
  lost `--quiet` — and some `--project` too — to the next line, so the first
  line, copied, stopped to ask, or deleted on gcloud's own project; and a long
  project ID pushed `<id>` off `comfy-qat setup --project <id>`. Nor does it
  start a line with a command a sentence only names: `create`'s warning that the
  host list stopped loading could wrap to a line reading `comfy-qat go or
  comfy-qat down`, refused when pasted, depending on how long the path in front
  of it was. A command named in a sentence is in backticks now.
- **Every printed `comfy-qat delete` carries `--yes`**, so it runs from a
  script or an agent; without it, it stopped to ask for the box's name and
  exited 2 with nothing deleted — and for a reserved box it is the command that
  stops the bill. Typing `delete` yourself still asks.

### Fixed

- **`discover` crashed on a mix of reserved and unreserved boxes**, neither in
  the host list — an `IndexError` after its heading, with nothing adopted. It
  is the mix it is sent to adopt after `create --reserve` could not write the
  host list.
- **A stock-out after `--region` on a project with one usable region** said
  "not everywhere this project can use the card" and offered "drop --region",
  which tried the same zones again. It now says that region is the only one and
  there is nowhere left to try.
- **"The same box, somewhere else" is the same box, wherever it is offered.**
  The command offered after a `--region` stock-out dropped `--disk`, and `--name`
  for a box that was not reserved, so pasted back it made a 200 GB box under
  another name. So did every other place that hands back the same create
  somewhere else: after a `--zone` stock-out (a bare `comfy-qat create`, which
  pasted with no terminal exits 2), after the attempts were capped (two bare
  templates), when the region's allowance is already held, when a reservation of
  that name is left in another zone, and when `--zone` and `--region` are both
  given or the region is spelt wrong. Each keeps the name and the disk now.
- **A gcloud command printed for you to paste names the project.** After a
  switch, the line `down --all` prints to stop a running box nobody declared,
  and the one a create refused on the project-wide ceiling prints, went to
  gcloud's own project — "not found" there, and the box named as running went
  on billing. Every printed gcloud command that acts on a project carries
  `--project` now; with none in hand it reads `--project=<your-project-id>`,
  which a shell refuses as it stands, rather than running somewhere else.
- **`down --all` after a project switch looks on the new project too.** With
  an old box on the old project and new work on the one the host list names, it
  looked for running boxes nobody declared on the old project only, and could
  end on `Nothing is now.` while one ran on the new project and billed. It reads
  every project a declared box is on and the host list's now, and gives no
  all-clear if any of them could not be read.
- **`setup` says what an adopted reserved box bills.** It added the box with
  its reservation and said only that it was added; it now says, as `discover`
  does, that the box bills every hour, running or stopped, until it is deleted,
  and prints the commands that end that.
- **The printed `reset-windows-password` command carries `--quiet`.** The one
  printed when a reset answers with no credentials did not, so it stopped to ask
  from a script; the check for it now reads every module.
- **A box named `…stockout…` is not a stock-out.** The bare word was one of the
  signs, and Google repeats a box's name in most of what it refuses, so an
  already-exists error read as "out of capacity, try another zone". A
  `state:STOCKOUT` code is still recognised, as is the real wrapped text.
- **A line break in gcloud's text no longer hides what it says.** Only the
  stock-out check folded whitespace before matching; Google's suggested zone and
  the kind of error (an expired session, a missing project) are now read through
  the same fold, as are what a not-found names, a quota request already made, a
  sign-in that is missing, and a dead tunnel's log — where a box still booting
  was read as one with nothing listening, and `go` said to start ComfyUI first.
- **A first `create` no longer says the host list stopped loading.** With no
  host list when it started and none when it picked the port, it warned that
  the host list had stopped loading while the box was made, and to fix a file
  that was never there. That warning is now only for a file that is there and
  will not load.
- **The 1.3.0 entry below** said the live run found four defects; it found ten,
  and four were fixed. It had no line for the stock-out text fix. And the
  acceptance pack still said the device name on a box with no GPU had not been
  seen: the live run recorded it, `cpu`.

### The suite

One new test file, for the project setting, and the rest beside what each fix
touches, each written to fail before its fix. Two readings now back the guards:
a walk of every string in every module, and of every string a function builds
out of parts it names, and a check of every line every test prints, with the
values in and `comfy-qat` lines parsed by the CLI itself. `setup`'s tests now
print what it says as well as recording it; recorded only, none of it reached
that check.
Between them: no printed delete or reset stops to ask, no printed command
carries anything but the command or is broken across lines by the tool, no
sentence names a command bare where a wrap could start a line with it — read
before the wrap, so it holds at every length and not only at the one a runner's
temp path gives — and every printed gcloud command that acts on a project
names it. And no argv reaches gcloud, or a fake standing in for it, without its
project, however it was built. No test asks a ComfyUI running on the machine
that runs the suite: three did, on 127.0.0.1:8188, and the network check now
refuses 8188 to 8190 even on loopback, and fails the test when the code it ran
caught the refusal.

## 1.3.0 — reserved boxes, a limit on them, and a box with no GPU

Three things a box could not be until now, and one column that follows from the
first. A box can be **reserved** at `create`, so its capacity is held for it and
it starts again where it was. How many may be reserved is **bounded by the
project's own GPU allowance**. A box can have **no GPU at all**. And `list` says
which boxes are reserved, how old each one is and how much disk it has —
because a reserved box is the first thing this tool makes whose bill does not
stop when the box does.

That last sentence is most of the work. Until this release, "stop the box" and
"stop paying" were the same act, and the tool said so in ten closing lines, four
summaries and twenty-eight failure fixes. Every one of them is false about a
reserved box, and each now says the reserved form instead.

**The feature set was run on real Google Compute Engine boxes on 2026-10-07**: a
reserved L4 box, a box with no GPU, the limit, `list` and `delete`. A reserved T4
box could not be run, for lack of stock. That run observed the three things
about Google this release had assumed — a reservation counts against GPU quota
as a running box does, one for a card built into its machine type is made
without naming the card, and one can be released while a stopped box still
targets it — and found ten defects. Four were fixed before release, below; the
other six were not: a "nothing is billing" said while another box from the same
host list was running, "stop paying" for an unreserved box whose disk still
bills after `down`, and four cosmetic ones. Paths that need a GPU ceiling
above 1 have not been run for real; see Phase V of
[the acceptance pack](docs/test-criteria.md).

### Added

- **`create --reserve`.** Google holds the capacity for the box in one zone, and
  bills for it every hour, running or stopped, until the box is deleted. The
  reservation is made first and the box is bound to it; it is named `<box>-rsv`
  and described `comfy-qat: held for <box>`. Left off, the choice is asked for at
  a terminal — with what each answer costs — and with `--yes` or nobody to ask
  the box is **not** reserved, and the output says so on the line where the
  question would have been. `--no-reserve` answers it in advance. Every way of
  stopping between the two calls says what is left billing, and Ctrl-C between
  them reports the reservation with the command that releases it. Running the
  same create again finds a reservation an earlier run left and puts the box on
  it rather than making a second.
- **A limit on reservations: the project's GPU allowance, in cards.** A
  reservation holds its card whether its box runs or not, so reserved cards are
  counted against `GPUS_ALL_REGIONS` and the per-card regional grant — from the
  project's own `reservations list`, never from the host list, so one made in
  the console counts too. On a project whose ceiling is 1, one reserved box is
  the whole allowance: `create --reserve` says so before it asks, and a later
  `create` is refused naming the reservation and the two commands that release
  it. `switch` and `move` count the same cards with the same function. If the
  reservations cannot be read, a reserving create is refused and an ordinary
  one warns and goes on.
- **`create --gpu none`**, also spelled `cpu`. An `n1-standard-8` with nothing
  attached; ComfyUI runs on its CPU. It needs no GPU quota, is not counted
  against the GPU ceiling, and is checked against the project's vCPU allowance
  instead — so it can be made while the one GPU slot is in use or reserved. No
  NVIDIA driver is installed, `go` does not wait for one, a CPU build of torch
  is installed and left alone, and ComfyUI is started with `--cpu`.
- **`list` shows RESERVED, and `list --live` adds AGE and DISK.** Plain `list`
  still asks Google nothing: RESERVED is what the host list says, and the
  footnote says that is where it came from. `--live` checks it — `yes`, `no`,
  `missing`, `yes (not in host list)`, or `…, unchecked` when a read failed —
  and names any reservation with no box on it, on a project one of your
  declared boxes is on, with Google's command to release it. Two calls per
  project, however many boxes. When a project's instances cannot be read it
  says that which reservations have no box was not worked out. With no cloud
  box declared it asks Google nothing; `down --all` reads the project then.
- **`gce_reservation`** in the host list: the one optional line that says a box
  is reserved. `create --reserve` and `discover` write it.

### Changed

- **`delete` releases a reserved box's reservation**, and is the only command
  that stops its bill. The reservation is released first, so a failure in
  between leaves a stopped box rather than a reservation billing with nothing on
  it; a box that was already deleted some other way still has its reservation
  released; and the entry stays in the host list until both are gone. A
  reservation other machines share, or that the box has no claim on, is never
  released — `delete` refuses and says how to delete the box alone.
- **`delete` reads the box's own record** for a reservation the host list entry
  does not declare, says so before the confirmation, and releases it.
- **`discover --prune` keeps a reserved box's entry** when the box is gone: its
  reservation may still be billing and the entry is what `comfy-qat delete`
  needs to release it. It names the reservation and prints that command.
- **The limit's remedy names a box only if your host list holds it** — matched
  by project, zone and instance, under the name the list gives it. A box on
  the project that is in no entry gets Google's commands, and an entry that
  merely shares the name is never offered for deletion.
- **`down --all` names reservations nobody declared with what releases them**,
  on every project a declared box is on — and, with no cloud box declared, on
  the project gcloud is pointed at.
- **`down` on a reserved box says it is still billing**, in place of `was
  billing. Stopped.`, and ends on `comfy-qat delete <name>`. `down --all` no
  longer prints `Nothing is now.` while a declared box is reserved, and reads
  the project's reservations before printing it at all.
- **`up`, `go`, `logs` and `disconnect`** end on that same line for a reserved
  box, where they end on `comfy-qat down <name>   # stop the box, stop paying`
  for any other. All of host.py's endings now go through one helper.
- **`switch`** says, in its plan, that stopping a reserved box frees neither its
  card nor its bill — and refuses, before anything is started or stopped, when
  reservations alone hold the GPU ceiling. It used to stop the box you were on
  first and then fail to start the other.
- **`move` refuses a reserved box**, before Google is asked anything: a
  reservation is held in one zone. It prints the three commands that delete the
  box and make it again elsewhere.
- **`discover`** adopts a box bound to a reservation with its `gce_reservation`,
  and says what that box costs when it does.
- `create`'s sentences about a reserved box are wrapped at 96 columns on
  stdout, and a menu note that is longer than that continues under its own
  first word.

### Fixed

- **Found on real boxes, 2026-10-07.** Every `gcloud … delete` printed for you to
  paste now ends `--quiet`; without it gcloud stops to ask, and from a script it
  exits 1 with the thing still billing. After `create --reserve`, `go`, `up` and
  `logs` on a reserved box the remedy is two lines, `comfy-qat down` then
  `comfy-qat delete` — `delete` alone is refused while the box runs. A stock-out
  after `--region` says it tried that region, not "every region". And a
  stock-out is recognised in the text Google really prints: gcloud line-wraps
  the error, and three of the four phrases it was recognised by did not match
  the wrapped text, so recognition rested on one. They are matched after
  folding whitespace, tested against the real wrapped text.
- `down <name>` trusted the host list alone, and told a box that was reserved
  without a `gce_reservation` line `was billing. Stopped.` It now reads the
  instance's own record, as `delete` does: bound to a reservation, it gets the
  reserved wording and the `delete` line; if the read fails it says the
  reservation could not be checked, with the command that shows it.
- `delete` on a box that was already gone, from an entry with no
  `gce_reservation` line, said `only the host list entry is left` without
  looking. It reads the project's reservations: its own reservation for that box
  is released (and the entry kept until it is), one that only carries the name is
  named with Google's command and left alone, and a read that fails is said to be
  that.
- `switch` printed `comfy-qat list --live` as the whole remedy when the instances
  could not be read. It and `move` now hand "not read" to the same remedy `create`
  prints, which says so and never says `nothing is on it` about a reservation
  nobody looked at.
- `create --reserve` had two wordings for "could not read this project's
  reservations". There is one now, printed under the quota lines.
- **A box declared `gpu = "none"` was treated as having a card**, because
  `"none"` is not an empty string. `go` waited fifteen minutes for `nvidia-smi`
  on it and then reinstalled a CUDA torch on every run; `switch` counted it as
  one card against the GPU ceiling and could stop the box you were on to make
  room for nothing; `stamp` let a GPU answer for it. Each now asks the one
  question, `has_gpu`.
- `move`'s stockout message said `has no none capacity either` for such a box.

### The suite

Seven new test files — the reservation model, the cloud calls, the build, the
box with no GPU, the command line, `list` and its two reads — and the same
rules as before: the oracle for a money sentence is text typed in the test, the
command is driven, and a printed remedy is pasted back and run. Three new static
guards hold the reserved bill, none of which the words `comfy-qat down` can
satisfy.

## 1.2.0 — every GPU asked for at setup, and a terminal that reads

Two things. `setup` now asks Google for every GPU this project could run, and
every surface tells the truth about the answer — ready, pending, denied with
the reason, or not creatable here. And the output that carries those answers
was rewritten to be read rather than merely to be correct.

The first was driven against a live project with real billing throughout, not
fixtures. Google refused six of seven requests automatically, which is account
standing rather than anything this tool does — but finding that out is the
point, and along the way it turned up a 96 GB Blackwell the project already
held through a Spot quota pool nothing was reading.

### Added

- **All four quota pools are read per card**, not just on-demand: Spot,
  committed use and workstation allowances are enumerated too. A card's status
  is the most favourable state it has anywhere, with the pool named. RTX PRO
  6000 was reported as denied for months while a granted Spot allowance sat
  unread.
- **`create` asks when it can.** `--os` and `--gpu` were required with a menu
  for neither, so a bare `comfy-qat create` was a Typer parse error while
  `drivable_cards()` already held the answer. At a terminal it now asks; with
  no terminal it names the values and a command that runs. A supplied flag is
  confirmed where the prompt would have been, so a scripted run and an
  interactive one produce the same transcript.
- **A region is checked before a request is filed for it.** Asking for quota in
  a region that stocks no NVIDIA card buys a grant that can never start a box,
  and a quota preference cannot be withdrawn.

### Changed

- **Denied is no longer rendered as "none — request it".** Google's answer and
  the absence of a question are different facts; the tool was advising users to
  re-file requests that had been refused seconds earlier.
- **Prose wraps at 96 columns.** 112 message literals were over that before any
  value was interpolated, and every one reached the terminal unbroken — the GSP
  refusal was 372 characters on one line. Commands, URLs and the `to fix:`
  block are deliberately left whole: a wrapped command is the one part of a
  message meant to be copied becoming the one part that cannot be.
- **Every failing check carries its own fix**, not just the first.

### Fixed

- **`quota request` could destroy quota you hold.** `--value` defaulted to `1`
  and a re-run addresses an existing preference by its own id, so running it to
  check on a standing request for 8 silently replaced it with 1. Lowering now
  requires `--allow-lower`, releasing requires `--release-quota` as well, and
  neither can be reached by a value nobody typed.
- **`quota list` crashed** on any denied preference where Google omits a value,
  which it does whenever that value is zero.
- **`--dry-run` said three different things.** Its help claimed it would make no
  call to Google; the plan is computed from a live quota read. The help, the
  runtime line, the docstring and the docs now agree with the code.

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
