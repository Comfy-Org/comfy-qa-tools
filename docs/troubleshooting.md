# Troubleshooting

Every error this tool prints, what causes it, and the fix. If you hit something
that is not listed here, that is a bug in this page as much as in the code.

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

**`request failed: ...`**
The quota request was rejected on submission. The most common cause is an account
with no billing history — Google frequently will not grant GPU quota until a project
has been billed at least once. Retrying will not change that.

**`still pending`** (exit code 75)
Not an error. The request went in but has not been approved within the wait window.
Approval can take days. Run the same command again to keep waiting, or
`comfy-qat auth quota list` to check. The console link printed with the request
shows the same thing.

**`gcloud timed out after 60s`**
A gcloud call hung — usually a network problem. Try again.
