# Troubleshooting

Every error this tool prints, what causes it, and the fix. If you hit something
that is not listed here, that is a bug in this page as much as in the code.

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

**`no host list at ~/.config/comfy-qa-tools/hosts.toml`**
You have not set one up yet. Run `comfy-qat host init`.

**`port 8188 is reserved for the local ComfyUI`**
A `gce` host is declared on 8188. That is ComfyUI's own default port, and your local
install answers on it — a tunnel there would silently show you the wrong machine.
Give the cloud host a different port, e.g. 8190.

**`hosts 'a' and 'b' both use port 8190`**
Two hosts share a port, so you could not tell which one you reached. Give each its
own.

**`kind must be 'local' or 'gce'`**
A host has a missing or misspelled `kind`. Those are the only two values.

**`unknown field(s) gce_zoen`**
A typo. Unknown fields are rejected rather than ignored, so this fails now instead
of leaving you puzzled later about why the box cannot be found. Check the spelling
against [hosts.md](hosts.md).

**`kind 'gce' requires gce_zone, gce_project`**
A cloud host is missing detail needed to locate it in Google Cloud. All of `os`,
`gpu`, `gce_instance`, `gce_zone` and `gce_project` are required.

**`kind 'gce' requires an explicit port`**
Only `local` hosts get a default port. Cloud hosts must say which local port their
tunnel uses, because that is the number you will type into a browser.

**`port 80 is outside 1024-65535`**
Ports below 1024 need root. Pick a high one.

## Setup

Setup stops rather than guessing whenever the fix is something only you can do. It
is safe to run again: it skips whatever is already done.

**`not signed in to Google Cloud`**
You used `--non-interactive`, so setup would not open a browser. Run
`gcloud auth login`, then setup again.

**`sign-in did not complete`**
The browser sign-in was cancelled or failed. Run `comfy-qat setup` again.

**`this account has no Google Cloud projects`**
Nothing to work in. Create one at the link printed, then run setup again.

**`no project set and 3 to choose from`**
You used `--non-interactive` with several projects, so setup will not pick for you.
Name it: `comfy-qat setup --project <id>`.

**`no billing account is linked to <project>, so no instance can start`**
Only you can attach billing. The error prints the direct link; do that, then run
setup again.

## Starting and stopping

**`could not run a command on comfy-win: ...`**
`host go` reaches the box over SSH through IAP to check for ComfyUI. If that fails,
the instance is missing the `enable-windows-ssh` metadata (Windows) or your account
lacks the IAP tunnel role. The error prints the manual way in.

**`the ComfyUI install on comfy-win did not finish`**
The install script exited non-zero. Its output is on your terminal above the error —
read that first. Get onto the box with the printed command to finish by hand.

**`ComfyUI is not answering on http://127.0.0.1:8190`** after `host up`
The machine is running and tunnelled, but ComfyUI itself is not serving — it is
either not installed or not started. **The box is billing while this is true.**
The error prints how to get onto it, which differs by OS: Windows needs a password
reset and Remote Desktop over the tunnel, anything else takes SSH through IAP.

**`Google has no L4 capacity in us-central1-a right now`**
A stockout. The zone has none of that card free, which is routine for GPUs and
nothing to do with your account, your quota or your billing. Retrying in the same
zone will not help — wait, or move the box to another zone. Google's raw message
mentions `STOCKOUT` and reads far more alarming than it is.

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

## Stamping a machine

**`nothing answered at http://127.0.0.1:8190`**
Nothing is listening on that port. For a local host, ComfyUI is not running. For a
cloud host, either the box is off or the tunnel is not up. Check the port in your
host list matches what the machine actually serves.

**`answered, but not with ComfyUI's /system_stats`**
Something is on that port, but it is not ComfyUI — a dev server, or another tunnel.
This is exactly the mix-up the port rules exist to prevent, so it is worth chasing
rather than working around.

## Google Cloud

**`gcloud is not installed or not on PATH`**
Install the Google Cloud SDK: https://cloud.google.com/sdk/docs/install

**`your gcloud session has expired`**
Credentials time out. Run `gcloud auth login`. This is the most common failure and
it looks alarming in raw gcloud output — it is routine.

**`no active gcloud account`** / **`nobody signed in`**
Run `gcloud auth login`.

**`no project set`**
Run `gcloud config set project <your-project-id>`.

**`no billing account linked to <project>`**
A project without billing cannot start any instance. Link one in the console; the
error prints the direct link.

**`zero GPU quota on this project — no GPU instance can start`**
A new project has no GPU quota at all. See [cost.md](cost.md), then:
```sh
comfy-qat auth quota list
comfy-qat auth quota request --quota-id <id> --region <region>
```

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
`comfy-qat auth quota list` to check. The console link printed with the request
shows the same thing.

**`could not list cloud boxes (...)`** during setup
Discovery failed, so nothing was added. Setup finishes anyway; add hosts by hand
from [hosts.md](hosts.md), or run `comfy-qat host discover` later.

**`could not read GPU quota (...)`** during setup
Setup carries on regardless — quota can take days to change and is never a reason
to strand you mid-setup. Check it afterwards with `comfy-qat auth quota`.

**`gcloud timed out after 240s`**
Listing quotas returns every compute quota on the project — around 400 records, over
a megabyte — and takes about a minute on a healthy connection. A timeout at 240
seconds means something is genuinely wrong with the network, not that the call is
slow. Try again.
