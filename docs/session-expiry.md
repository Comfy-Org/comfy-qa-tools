# Why your Google sign-in keeps expiring

Mid-test-pass, every cloud command starts failing at once:

```
ERROR: (gcloud.compute.instances.list) There was a problem refreshing your current
auth tokens: Reauthentication failed. cannot prompt during non-interactive execution.
Please run: $ gcloud auth login
```

This is the most disruptive routine failure in the tool, and it is not a bug in
the tool, in gcloud, or in your account. It is an organisation policy doing
exactly what it was configured to do. This page says what is happening, what to
do in the moment, and what would make it happen less — because "sign in again"
is a workaround, and how often a tester has to do it is a release-process
question, not a personal one.

This lives on its own page rather than in
[troubleshooting.md](troubleshooting.md) because it is not one error with one
fix. It is a policy, a procedure and an ask of whoever administers the Workspace
domain. The error strings themselves are in troubleshooting, where you would
paste them.

## What is actually failing

Two different things expire, and they are easy to confuse.

**An access token** lasts one hour. This expires constantly and you never see
it: gcloud silently exchanges its refresh token for a new one. Nothing about
this is a problem.

**A session** is the thing that has expired here. Google Cloud *session control*
is a Workspace admin setting — Admin console, Security, Access and data control,
Google Cloud session control — that puts a maximum age on user-authorised
sessions. When it lapses, the refresh stops being silent: Google answers with a
**reauth challenge**, which asks the human to prove they are still there, with a
password or a security key. gcloud calls the resulting proof a RAPT, a reauth
proof token.

You can tell the two apart by one word in the error. If it says
**"Reauthentication"**, it is the session, and only a human can clear it. See
Google's own pages on
[reauthentication](https://docs.cloud.google.com/docs/authentication/reauthentication)
and
[session control](https://docs.cloud.google.com/access-context-manager/docs/session-controls-for-reauthentication).

The session length is set by an admin. It can be 1 to 24 hours, and Google's
default for Google Cloud sessions is 16 hours. A tester who signs in at the
start of a working day and is still testing that evening will be challenged, and
so will anyone whose pass runs past midnight.

## Why it fails hard instead of just asking

A reauth challenge is meant to be a ten-second interruption: type a password,
touch a key, carry on. Turning it into a dead command takes one more ingredient,
and it is worth understanding because it is the part that was in our control.

gcloud only offers the challenge when it believes it can talk to a human. Its
test for that is stricter than it looks — from
`googlecloudsdk/core/console/console_io.py`, reauth is attempted only when
`CanPrompt()` is true, and that requires **stdin *and stderr* to both be
terminals**.

Any tool that captures gcloud's output — as this one does, because it turns
gcloud's errors into readable sentences — hands gcloud a pipe for stderr.
gcloud concludes it is running unattended, declines to prompt, and fails. That
is why the error says "cannot prompt during non-interactive execution" even
though you are sitting right there.

The same applies, worse, to the tunnel: it runs detached with its output going
to a log file, so a reauth failure there is completely silent. See
[the gap that is still open](#the-gap-that-is-still-open).

## What the tool does about it now

**It checks before it spends money.** Anything that starts billing — `up`,
`go` on a stopped box, `create`, `move` — proves the credential works before the
first billable call. Failing at the start costs a second. Failing halfway leaves
a GPU box running, billing, and an error about whatever step happened to be
holding the credential. A call that has already reached Google counts as proof,
so on the normal path this costs nothing.

**It offers the prompt a terminal.** When the preflight hits a reauth challenge
and you are at a real terminal, the tool re-runs that one read-only check with
the terminal attached, so gcloud can ask you the question it wanted to ask. Then
it carries on. Nothing signs you in on your behalf — `gcloud auth login` stays
your command — and this only ever happens before the work starts, never in the
middle of it.

**It stops calling everything an expired session.** "Run `gcloud auth login`" is
now printed only for the failures a sign-in actually fixes. A missing project, a
denied permission and a dropped connection each get their own sentence, because
sending a tester to reauthenticate when their Wi-Fi dropped wastes the reauth
*and* leaves them on a machine that still cannot reach Google.

## What you do when it happens

```sh
gcloud auth login
comfy-qat status
```

Then re-run whatever you were doing. If a box was left running, stop paying for
it first:

```sh
comfy-qat down <name>
```

`status` is the quick way to tell an expired session from anything else: it
checks gcloud, account, project, billing and GPU quota in that order and stops
at the first thing that is actually wrong.

If you are on a security key and the challenge appears but does not complete,
you have 15 seconds to touch it. Missing that window reports a token failure
rather than a timeout; just run `gcloud auth login` again.

## What would make it happen less

Ranked. The first two are the only ones that change the frequency; everything
below manages the symptom.

### 1. Lengthen the Google Cloud session — admin only

An admin can raise the session length to its maximum of 24 hours, or disable
session control for Google Cloud entirely. A tester who signs in once a day then
stops being interrupted mid-pass.

**Cost:** this is a real reduction in security posture, and it applies to
everyone in the domain, not just QA. Session control exists so that a stolen
laptop or a walked-away-from terminal cannot drive Google Cloud indefinitely.
Twenty-four hours is a defensible number for a team whose work sessions run
long; "off" is a decision someone should make deliberately, in writing.

**Who can do it:** a Workspace or Cloud Identity super admin. Not a tester.
Google's documented range is 1 to 24 hours; whether "never expires" is still
selectable depends on when the domain was created and which defaults it
inherited — an admin will see what the console offers.

**The narrower version:** the same setting can be scoped, so the long session
applies to a group rather than the whole domain. A `qa-cloud` group of two or
three people is a much smaller exposure than the domain, and is the version
worth asking for first.

### 2. Stop using a human's session for machine work — service account impersonation

Reauth applies to **user** sessions. Service accounts are excluded from session
control entirely, so work driven by one is never challenged.

The right shape for a QA laptop is **impersonation**, not a key file:

```sh
gcloud config set auth/impersonate_service_account qa-driver@<project>.iam.gserviceaccount.com
```

You still sign in as yourself; gcloud mints short-lived tokens for the service
account on each call. Google
[recommends impersonation over keys](https://docs.cloud.google.com/iam/docs/best-practices-for-using-and-managing-service-accounts)
precisely because the audit log still records *which human* acted.

**Cost:** an admin has to create the service account, grant it the compute and
IAP roles the tool needs, and grant you Service Account Token Creator on it.
There is also a real caveat: the underlying user session is still yours, so a
reauth challenge can still appear at the moment tokens are minted. It reduces
the surface; it is not a guarantee.

**What about a key file?** A downloaded service account key would remove reauth
completely, and it is the wrong answer for a laptop. Google's own guidance is to
avoid keys wherever possible: a key file is a bearer credential with no expiry
and no way to tell who used it, it survives losing the laptop, and a compromised
one cannot be revoked cleanly — the remediation is disabling the service account
for an hour or more. Many organisations block key creation outright with the
`iam.disableServiceAccountKeyCreation` org policy, and if ours does, this option
does not exist anyway. Do not create one to dodge a sign-in prompt.

### 3. Make the interruption cheap rather than rare — already done

The preflight and the terminal-attached retry described above. This does not
reduce how often the session expires; it turns the expiry into a prompt you
answer instead of a failure that costs you a running instance and a confusing
error. For a tester, this is most of the pain.

### 4. Sign in deliberately at the start of a pass

Free, needs nobody's permission, and genuinely helps:

```sh
gcloud auth login && comfy-qat status
```

A fresh session at the top of a test pass means the clock starts when you do,
rather than expiring an hour into a two-hour run. Worth putting in the release
checklist next to the other pre-pass steps.

## Things that sound like fixes and are not

- **`gcloud auth application-default login`.** ADC created from a user account
  is subject to exactly the same session control, so it expires the same way.
  This tool does not use ADC at all — it shells out to gcloud, which uses your
  gcloud credentials. Running it changes nothing here.
- **`--no-launch-browser`.** It only stops gcloud opening a browser for you; you
  still open the URL and paste a code back. It makes sign-in work over SSH. It
  does not make it unattended.
- **Refresh tokens "expiring".** The 7-day expiry people hit belongs to OAuth
  clients still in testing status, and the 6-month rule is for tokens nobody has
  used. Neither applies to gcloud in daily use. If the error does not say
  "Reauthentication", it is not this page's problem.
- **Retrying.** A reauth challenge that could not be answered will not be
  answered by asking again. The tool retries once and only with the terminal
  attached, which is the only retry that can change the outcome.

## The gap that is still open

The forward is started as a detached background process with its output
redirected to a log file, and its exit status is not checked. If the session
expires between starting the box and opening the tunnel, the tunnel process dies
immediately of a reauth failure that nobody reads, a pid file is written for a
process that is already gone, and the tool reports "tunnel open". The box is
then diagnosed as "running and tunnelled, but ComfyUI is not answering" — which
sends a tester to look for a broken ComfyUI install that is fine.

On this machine, 131 gcloud invocations logged that exact failure without one of
them reaching a person.

The box is left running and billing. `comfy-qat down <name>` stops it.

The fix is on the tunnel: after starting it, confirm the process is still alive
a moment later and read its log if it is not, so a credential failure is
reported as a credential failure. Until that lands, treat "tunnelled but ComfyUI
is not answering" as *possibly* an expired session, and check with
`comfy-qat status` before going onto the box.
