"""Session expiry: naming it correctly, and catching it before it costs money.

Two rules these defend.

The first is about money. A credential that has expired must be discovered
before a GPU instance is started, not after. Failing at the start costs a
second; failing halfway leaves a box running, billing, and an error message
about whichever step happened to be holding the credential.

The second is about honesty. "Your session expired, run `gcloud auth login`" is
the right sentence for exactly one failure. Printing it for a missing project or
a dropped connection sends a tester to fix something that was never broken.
"""

from __future__ import annotations

import pytest

from comfy_qa import gcloud as gc_mod
from comfy_qa.gcloud import (
    CREDENTIALS,
    DENIED,
    NETWORK,
    NO_ACCOUNT,
    NO_PROJECT,
    REAUTH,
    UNKNOWN,
    Gcloud,
    GcloudError,
    classify,
    explain_failure,
)

# Copied from a real failure on this machine, gcloud 579.0.0. The wrapper
# sentence ("problem refreshing your current auth tokens") is generic; the word
# that identifies it is "Reauthentication".
REAUTH_OUTPUT = """ERROR: (gcloud.compute.instances.list) There was a problem \
refreshing your current auth tokens: Reauthentication failed. cannot prompt \
during non-interactive execution.
Please run:

  $ gcloud auth login

to obtain new credentials."""

# The same wrapper, a completely different cause.
OFFLINE_OUTPUT = """ERROR: (gcloud.compute.instances.list) There was a problem \
refreshing your current auth tokens: Unable to reach oauth2.googleapis.com: \
[Errno 8] nodename nor servname provided"""

NO_ACCOUNT_OUTPUT = """ERROR: (gcloud.compute.instances.list) You do not \
currently have an active account selected."""

NO_PROJECT_OUTPUT = """ERROR: (gcloud.compute.instances.list) The required \
property [project] is not currently set."""

DENIED_OUTPUT = """ERROR: (gcloud.compute.instances.start) Required \
'compute.instances.start' permission for 'projects/p/zones/z/instances/i'"""


class Fake(Gcloud):
    """A Gcloud with the subprocess replaced, but the runner seam left alone.

    Injecting `runner` switches the preflight off deliberately — that seam means
    there is no real gcloud and no real credential, so there is nothing to prove.
    These tests therefore go one level lower and replace `run` itself, which is
    what the preflight actually calls.
    """

    def __init__(self, answers: dict | None = None, interactive: int = 0) -> None:
        super().__init__()
        self.answers = answers or {}
        self.calls: list[str] = []
        self.interactive_calls: list[str] = []
        self.interactive = interactive

    def available(self) -> str:
        return "<fake>"

    def run(self, args, *, parse_json=True, timeout=None):
        key = " ".join(args)
        self.calls.append(key)
        for prefix, value in self.answers.items():
            if key.startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                if not gc_mod._is_local_only(args):
                    self.proven = True
                return value
        raise AssertionError(f"unexpected gcloud call: {key}")

    def run_interactive(self, args):
        self.interactive_calls.append(" ".join(args))
        return self.interactive


SIGNED_IN = {"auth list": [{"account": "ali@comfy.org", "status": "ACTIVE"}]}
PROJECT = {"config get-value project": "proj-1"}
STARTS = {"compute instances start": ""}
EXPIRED = GcloudError("your gcloud session has expired", fix="gcloud auth login",
                      raw=REAUTH_OUTPUT, kind=REAUTH)


def no_terminal(monkeypatch):
    monkeypatch.setattr(gc_mod, "can_prompt", lambda: False)


def a_terminal(monkeypatch):
    monkeypatch.setattr(gc_mod, "can_prompt", lambda: True)


# --- naming the failure -----------------------------------------------------

def test_reauth_is_recognised_by_the_word_that_identifies_it():
    assert classify(REAUTH_OUTPUT) == REAUTH
    message, fix, _ = explain_failure(REAUTH_OUTPUT, "", 1)
    assert message == "your gcloud session has expired"
    assert fix == "gcloud auth login"


def test_a_dropped_connection_is_not_an_expired_session():
    """The failure this catches: both wear the same "problem refreshing your
    current auth tokens" wrapper. Signing in again does not fix Wi-Fi, and the
    tester spends the reauth on a machine that still cannot reach Google."""
    assert classify(OFFLINE_OUTPUT) == NETWORK
    message, fix, _ = explain_failure(OFFLINE_OUTPUT, "", 1)
    assert message == "could not reach Google Cloud"
    assert "auth login" not in (fix or "")


def test_nobody_signed_in_is_not_the_same_as_a_session_that_expired():
    assert classify(NO_ACCOUNT_OUTPUT) == NO_ACCOUNT
    message, fix, _ = explain_failure(NO_ACCOUNT_OUTPUT, "", 1)
    assert message == "no active gcloud account"
    assert fix == "gcloud auth login"


def test_a_missing_project_is_never_answered_with_a_sign_in():
    assert classify(NO_PROJECT_OUTPUT) == NO_PROJECT
    message, fix, _ = explain_failure(NO_PROJECT_OUTPUT, "", 1)
    assert message == "no project set"
    assert "config set project" in fix


def test_a_denied_permission_keeps_gcloud_own_words():
    """gcloud names the exact permission. Nothing this tool could write beats it."""
    assert classify(DENIED_OUTPUT) == DENIED
    message, fix, _ = explain_failure(DENIED_OUTPUT, "", 1)
    assert "compute.instances.start" in message
    assert "auth login" not in (fix or "")


def test_a_revoked_sign_in_is_told_apart_from_a_reauth_challenge():
    assert classify("ERROR: invalid_grant: Token has been expired or revoked.") == CREDENTIALS
    message, fix, _ = explain_failure("invalid_grant: bad token", "", 1)
    assert message == "gcloud could not refresh your sign-in"
    assert fix == "gcloud auth login"


def test_an_unrecognised_failure_is_not_forced_into_a_bucket():
    assert classify("ERROR: something new and strange") == UNKNOWN
    assert classify("") == UNKNOWN


def test_the_kind_travels_with_the_error():
    assert GcloudError("x", kind=REAUTH).is_auth is True
    assert GcloudError("x", kind=NO_ACCOUNT).is_auth is True
    assert GcloudError("x", kind=NETWORK).is_auth is False
    assert GcloudError("x").kind == UNKNOWN


# --- the preflight ----------------------------------------------------------

def test_an_expired_session_is_found_before_the_instance_is_started(monkeypatch):
    """The whole point. The box must never start on a credential that is dead."""
    no_terminal(monkeypatch)
    gc = Fake({**SIGNED_IN, **PROJECT, "projects describe": EXPIRED, **STARTS})

    with pytest.raises(GcloudError) as caught:
        gc.start_instance("comfy-win", "us-central1-a", "proj-1")

    assert caught.value.kind == REAUTH
    assert not any(key.startswith("compute instances start") for key in gc.calls), (
        "the credential failed and the machine was started anyway — that is billing"
    )


def test_nobody_signed_in_is_named_without_calling_google(monkeypatch):
    no_terminal(monkeypatch)
    gc = Fake({"auth list": [], **PROJECT, **STARTS})

    with pytest.raises(GcloudError) as caught:
        gc.start_instance("comfy-win", "us-central1-a", "proj-1")

    assert caught.value.kind == NO_ACCOUNT
    assert not any(key.startswith("projects describe") for key in gc.calls)


def test_no_project_stops_the_preflight_with_its_own_answer(monkeypatch):
    no_terminal(monkeypatch)
    gc = Fake({**SIGNED_IN, "config get-value project": "(unset)", **STARTS})

    with pytest.raises(GcloudError) as caught:
        gc.start_instance("comfy-win", "us-central1-a", None)

    assert caught.value.kind == NO_PROJECT
    assert "config set project" in caught.value.fix


def test_a_denied_permission_counts_as_proof_not_as_a_blockage(monkeypatch):
    """To be refused, the credential had to be accepted first.

    Blocking here would strand a tester whose account cannot read project
    metadata but can perfectly well start the box — a worse bug than the one the
    preflight prevents.
    """
    no_terminal(monkeypatch)
    denied = GcloudError("denied", raw=DENIED_OUTPUT, kind=DENIED)
    gc = Fake({**SIGNED_IN, **PROJECT, "projects describe": denied, **STARTS})

    gc.start_instance("comfy-win", "us-central1-a", "proj-1")

    assert any(key.startswith("compute instances start") for key in gc.calls)


def test_a_call_that_already_reached_google_is_not_checked_again(monkeypatch):
    """`host up` describes the instance before it starts it. That describe is
    better proof than any preflight, so the preflight must cost nothing."""
    no_terminal(monkeypatch)
    gc = Fake({"compute instances describe": {"status": "TERMINATED"}, **STARTS})

    gc.instance_status("comfy-win", "us-central1-a", "proj-1")
    gc.start_instance("comfy-win", "us-central1-a", "proj-1")

    assert not any(key.startswith("projects describe") for key in gc.calls)
    assert not any(key.startswith("auth list") for key in gc.calls)


def test_reading_the_local_config_is_not_proof_of_anything(monkeypatch):
    """`gcloud auth list` prints a happy account list with a session that expired
    hours ago. It reads a file; it never asks Google."""
    no_terminal(monkeypatch)
    gc = Fake({**SIGNED_IN, **PROJECT, "projects describe": EXPIRED, **STARTS})

    gc.active_account()
    gc.current_project()

    assert gc.proven is False
    with pytest.raises(GcloudError):
        gc.start_instance("comfy-win", "us-central1-a", "proj-1")


# --- answering the challenge instead of dying of it -------------------------

def test_a_terminal_that_can_answer_the_challenge_is_given_the_chance(monkeypatch):
    """gcloud only offers reauth when it owns stderr, and `run` captures stderr.

    Handing the terminal over for one read-only call is the difference between a
    ten-second interruption and a dead command.
    """
    a_terminal(monkeypatch)
    gc = Fake({**SIGNED_IN, **PROJECT, "projects describe": EXPIRED, **STARTS})

    gc.start_instance("comfy-win", "us-central1-a", "proj-1")

    assert gc.interactive_calls, "the reauth prompt was never offered a terminal"
    assert "projects describe" in gc.interactive_calls[0]
    assert any(key.startswith("compute instances start") for key in gc.calls)


def test_the_rescue_is_read_only_and_never_signs_anybody_in(monkeypatch):
    """Signing in stays the tester's own command, with their own repro trail."""
    a_terminal(monkeypatch)
    gc = Fake({**SIGNED_IN, **PROJECT, "projects describe": EXPIRED, **STARTS})

    gc.start_instance("comfy-win", "us-central1-a", "proj-1")

    assert all("auth login" not in call for call in gc.interactive_calls)
    assert all("compute" not in call for call in gc.interactive_calls)


def test_nothing_is_offered_when_there_is_no_terminal_to_offer_it_to(monkeypatch):
    no_terminal(monkeypatch)
    gc = Fake({**SIGNED_IN, **PROJECT, "projects describe": EXPIRED, **STARTS})

    with pytest.raises(GcloudError):
        gc.start_instance("comfy-win", "us-central1-a", "proj-1")

    assert gc.interactive_calls == []


def test_a_refused_challenge_reports_the_original_failure(monkeypatch):
    a_terminal(monkeypatch)
    gc = Fake({**SIGNED_IN, **PROJECT, "projects describe": EXPIRED, **STARTS},
              interactive=1)

    with pytest.raises(GcloudError) as caught:
        gc.start_instance("comfy-win", "us-central1-a", "proj-1")

    assert caught.value.kind == REAUTH
    assert caught.value.fix == "gcloud auth login"
    assert not any(key.startswith("compute instances start") for key in gc.calls)


def test_only_a_reauth_is_retried(monkeypatch):
    """A network failure is not answerable by a prompt, and retrying it just
    doubles the wait before the same error."""
    a_terminal(monkeypatch)
    offline = GcloudError("could not reach Google Cloud", raw=OFFLINE_OUTPUT, kind=NETWORK)
    gc = Fake({**SIGNED_IN, **PROJECT, "projects describe": offline, **STARTS})

    with pytest.raises(GcloudError) as caught:
        gc.start_instance("comfy-win", "us-central1-a", "proj-1")

    assert caught.value.kind == NETWORK
    assert gc.interactive_calls == []


# --- what the preflight deliberately does not do ----------------------------

def test_the_move_steps_are_all_guarded(monkeypatch):
    """Moving a box is a snapshot, a disk and an instance — the slowest and most
    expensive thing this tool does. None of it should begin on a dead credential.
    """
    no_terminal(monkeypatch)
    for call in (
        lambda g: g.snapshot_disk("d", "z", "proj-1", "s"),
        lambda g: g.create_disk_from_snapshot("d", "z", "proj-1", "s"),
        lambda g: g.create_instance_from_disk("i", "z", "proj-1", "d", "g2-standard-8"),
    ):
        gc = Fake({**SIGNED_IN, **PROJECT, "projects describe": EXPIRED})
        with pytest.raises(GcloudError) as caught:
            call(gc)
        assert caught.value.kind == REAUTH
        assert not any(key.startswith("compute") for key in gc.calls)


def test_stopping_a_machine_is_never_blocked_by_a_preflight(monkeypatch):
    """`host down` is how you stop paying. A credential check that could refuse
    it would keep a box running for the sake of tidiness."""
    no_terminal(monkeypatch)
    gc = Fake({"compute instances stop": ""})

    gc.stop_instance("comfy-win", "us-central1-a", "proj-1")

    assert gc.calls == ["compute instances stop comfy-win --zone=us-central1-a "
                        "--project=proj-1"]


def test_the_injected_runner_seam_skips_the_preflight():
    """Every other test file injects `runner`. That seam stands in for gcloud
    itself, so a credential preflight through it would prove nothing and would
    break every fixture that lists the calls it expects.
    """
    seen = []
    gc = Gcloud(runner=lambda args, mode: seen.append(" ".join(args)) or "")

    gc.start_instance("comfy-win", "us-central1-a", "proj-1")

    assert seen == ["compute instances start comfy-win --zone=us-central1-a "
                    "--project=proj-1"]


# --- the page that explains it ----------------------------------------------

def test_the_session_expiry_page_exists_and_says_what_the_org_can_change():
    """A failure this routine is release process, not a line in an error list.

    The page has to carry three things a troubleshooting entry cannot: why it
    happens, what a tester does in the moment, and what somebody with admin
    rights could change so it happens less.
    """
    from pathlib import Path

    page = Path(__file__).resolve().parent.parent / "docs" / "session-expiry.md"
    assert page.exists(), "docs/session-expiry.md is missing"
    text = page.read_text()
    assert len(text.split()) > 100, "the page is a stub"
    for expected in ["session control", "impersonat", "gcloud auth login",
                     "admin", "Reauthentication"]:
        assert expected in text, f"the page never mentions {expected!r}"


def test_can_prompt_mirrors_the_rule_gcloud_actually_uses(monkeypatch):
    """gcloud requires stderr to be a terminal too, not just stdin — which is
    exactly why capturing stderr makes reauth impossible.
    """
    class Stream:
        def __init__(self, tty): self.tty = tty
        def isatty(self): return self.tty

    monkeypatch.setattr(gc_mod.sys, "stdin", Stream(True))
    monkeypatch.setattr(gc_mod.sys, "stderr", Stream(False))
    assert gc_mod.can_prompt() is False

    monkeypatch.setattr(gc_mod.sys, "stderr", Stream(True))
    assert gc_mod.can_prompt() is True
