# Merge into troubleshooting.md

Not a page of its own — this is the delta for `docs/troubleshooting.md`, kept
separate because another agent owns that file. Delete this file once merged.

The long-form explanation (why sessions expire, what the org can change, what a
tester does about it) is a new page: `docs/session-expiry.md`. Only the error
entries belong in troubleshooting.

---

## 1. Replace the existing "your gcloud session has expired" entry

The current entry says credentials "time out", which reads as ordinary token
expiry and undersells it. Replace it with the following, and add the four new
entries after it.

**`your gcloud session has expired`**
Google asked for a fresh proof of identity — a reauth challenge — and nothing
could ask you for it. This is a Workspace session-length policy, not a fault in
your account, and it is the most common failure this tool has. Run
`gcloud auth login`, then carry on where you left off. If it keeps happening
mid-pass, [session-expiry.md](session-expiry.md) explains why and what the org
can change.

**`gcloud could not refresh your sign-in`**
The stored credential could not be exchanged for a token, and it is not a reauth
challenge — usually a sign-in that was revoked, or an account removed from the
project. `gcloud auth login` fixes it. If it fails again immediately, the
account itself is the problem.

**`could not reach Google Cloud`**
gcloud never got to Google. This is a network failure wearing an authentication
failure's clothing: gcloud reports a token refresh it could not complete, which
used to be printed as an expired session. Signing in again will not help. Check
your connection — including a VPN or proxy that may have dropped — and try again.

**`no project set`** (from a cloud command, not from setup)
You are signed in but no project is chosen, so gcloud does not know where to
look. `gcloud config set project <your-project-id>`, or run `comfy-qat setup`.

**`Required 'compute.instances.start' permission for ...`**
Your sign-in worked and Google refused the action: the account is missing an IAM
role, not a credential. The message names the exact permission. `comfy-qat auth
status` shows which account you are actually using — being signed in as the
wrong one of two accounts is the usual cause.

## 2. Amend the "ComfyUI is not answering" entry

Under **`ComfyUI is not answering on http://127.0.0.1:8190`** after `host up`,
add:

> It can also mean the tunnel never opened. The tunnel runs detached and its exit
> status is not checked, so if your session expired between starting the box and
> opening the tunnel, the tunnel died silently and this message blames ComfyUI.
> Run `comfy-qat auth status` before you go looking on the box.

## 3. Add a pointer at the top of the "Google Cloud" section

> Sessions expiring mid-pass have their own page:
> [session-expiry.md](session-expiry.md).

## 4. tests/test_docs.py

`ERROR_PHRASES` should gain the new sentences, so each keeps its troubleshooting
entry:

```python
    "gcloud could not refresh your sign-in",
    "could not reach Google Cloud",
```

`"no project set"` is already in the list. The permission message keeps gcloud's
own words rather than ours, so it is not a phrase this tool prints and does not
belong in the list.
