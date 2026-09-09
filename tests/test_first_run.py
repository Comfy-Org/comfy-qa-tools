"""The first thing a new user does: `create`, then the `go` `create` tells them to run.

Both of these came out of the first live `create` + `go` this tool ever had, on
real hardware, and neither is theoretical.

`create` finishes by printing that the box is installing its NVIDIA driver, that
this reboots it once or twice, and that `comfy-qat go` waits that out. Running
that exact line immediately gave, after "comfy-linux is running":

    could not open the tunnel to comfy-linux: the tunnel closed as soon as it
    was opened (gcloud exited 255).
    to fix: read ~/.config/comfy-qa-tools/tunnels/comfy-linux.log. If it
    mentions credentials or reauthentication, your session has expired:
    gcloud auth login

Two failures in one message. `go` did not wait out the reboot it had just
promised to wait out, and the advice it gave was to repair a gcloud session that
was working perfectly — the real cause, `4003: 'failed to connect to backend'
... (Failed to connect to port 22)`, was in the log and six lines above the tail
anything looked at. The identical command four minutes later installed ComfyUI
and served it.

Then the tunnel's address family. `ssh -L` bound `127.0.0.1` only, so
`http://127.0.0.1:8190` answered and `http://localhost:8190` did not, on any
browser that prefers IPv6 for that name.

And last, the command after the first one: `up` on a box that has been through
`down`. Nothing on the box starts ComfyUI at boot, so the install is present and
dead — and `up` spent its full 180 seconds of billing to end on "ComfyUI is not
installed or not started", which names two states, commits to neither, and offers
a session on the machine. `comfy-qat go` on the same box found the install,
skipped it and had ComfyUI answering in 52 seconds.
"""

from __future__ import annotations

import os
import sys

import pytest

from comfy_qa.config import Host
from comfy_qa.gcloud import GcloudError
from comfy_qa.lifecycle import COMFYUI_ABSENT, LifecycleError, bring_up
from comfy_qa.stamp import Stamp
from comfy_qa.tunnel import (
    BACKEND_NOT_LISTENING,
    SSH_NOT_READY,
    TunnelError,
    _spawn,
    command,
    last_words,
    still_booting,
)

LINUX = Host(name="comfy-linux", kind="gce", port=8190, os="Ubuntu 22.04", gpu="L4",
             gce_instance="comfy-linux", gce_zone="europe-west4-c",
             gce_project="proj")
STAMP = Stamp(host="comfy-linux", url="http://127.0.0.1:8190",
              comfyui_version="0.33.0")


# What gcloud actually wrote to the tunnel log on the failing run, in the order it
# wrote it. The shape is the point: the sentence that names the cause is at the
# top, and gcloud puts its own troubleshooting suggestions and a restatement of
# the exit code underneath — so the last six lines, which is all `last_words`
# quotes, do not contain it.
BOOTING_LOG = """WARNING:
To increase the performance of the tunnel, consider installing NumPy. For instructions,
please see https://cloud.google.com/iap/docs/using-tcp-forwarding
ERROR: (gcloud.compute.start-iap-tunnel) While checking if a connection can be made: Error while connecting [4003: 'failed to connect to backend']. (Failed to connect to port 22)
Recommendation: To check for possible causes of SSH connectivity issues and get
recommendations, rerun the ssh command with the --troubleshoot option.
gcloud compute ssh comfy-linux --project=proj --zone=europe-west4-c --troubleshoot
Or, to investigate an IAP tunneling issue:
gcloud compute ssh comfy-linux --project=proj --zone=europe-west4-c --troubleshoot --tunnel-through-iap
ERROR: (gcloud.compute.ssh) [/usr/bin/ssh] exited with return code [255].
"""


def writes(text: str, code: int = 255) -> list[str]:
    """A command that reproduces one gcloud log and then dies the way gcloud died."""
    return [sys.executable, "-c",
            f"import sys; sys.stderr.write({text!r}); sys.exit({code})"]


def said():
    lines: list[str] = []
    return lines, lines.append


class Cloud:
    """A box Google reports RUNNING, that answers what is on it when asked.

    `comfyui` and `installed` are what the box says to the two one-word probes
    `bring_up` runs when the tunnel is open and nothing answers on it. `None` for
    either is a box that would not say — which is a third answer, not a `False`.
    `asked` records the commands, because "it stopped waiting" and "it asked
    before it stopped" are different claims.
    """

    def __init__(self, status: str = "RUNNING", *, comfyui: str | None = "GONE",
                 installed: str | None = "INSTALLED") -> None:
        self.status = status
        self.comfyui = comfyui
        self.installed = installed
        self.asked: list[str] = []

    def instance_status(self, instance, zone, project):
        return self.status

    def ssh_output(self, instance, zone, project, command):
        self.asked.append(command)
        if "pgrep -f" in command or "Get-Process -Name python" in command:
            answer = self.comfyui
        elif "INSTALLED" in command:
            answer = self.installed
        else:
            raise AssertionError(f"unexpected command: {command}")
        if answer is None:
            raise GcloudError("the box would not answer")
        return answer

    def __getattr__(self, name):
        def unexpected(*args, **kwargs):
            raise AssertionError(f"{name} was not expected here")
        return unexpected


# --------------------------------------------------------------------------
# FINDING A, at the classifier: still booting, or genuinely broken?
# --------------------------------------------------------------------------


def test_iap_refusing_port_22_is_a_machine_still_starting():
    """4003 names the port IAP could not reach, and that port decides everything.

    `gcloud compute ssh --tunnel-through-iap` dials **22**. So a 4003 about port
    22 is a machine whose sshd has not come up — cured by waiting, and by
    nothing else. It is not, and can never be, a statement about ComfyUI.
    """
    assert still_booting(BOOTING_LOG)


def test_iap_refusing_the_comfyui_port_is_still_nothing_listening():
    """The other half of the same code, kept apart deliberately.

    The older `start-iap-tunnel` forward named the far ComfyUI port instead, and
    that genuinely does mean "start ComfyUI first". Telling somebody that about
    a box whose sshd is not up is advice they cannot follow — they cannot get
    onto the machine to follow it.
    """
    comfyui = ("ERROR: While checking if a connection can be made: Error while "
               "connecting [4003: 'failed to connect to backend']. (Failed to "
               "connect to port 8188)")
    assert not still_booting(comfyui)


def test_the_cause_is_read_from_the_whole_log_and_not_from_its_last_six_lines(tmp_path):
    """The regression, exactly as it happened.

    Both halves are asserted, because the first is what made the second possible:
    the quoted tail does NOT carry the words that identify this failure, so a
    classifier reading only the tail — which is what read it — could not have got
    this right however well it was written. Classification reads all of it now
    and only the quote is trimmed.
    """
    log = tmp_path / "comfy-linux.log"

    with pytest.raises(TunnelError) as caught:
        _spawn(writes(BOOTING_LOG), log, grace=15)

    assert "4003" not in last_words(log), (
        "the fixture no longer buries the cause, so this test has stopped "
        "testing the thing that went wrong"
    )
    assert caught.value.kind == SSH_NOT_READY, str(caught.value)
    assert caught.value.kind != BACKEND_NOT_LISTENING


def test_a_still_booting_box_is_never_told_its_credentials_expired(tmp_path):
    """The half of the message that sent somebody to fix a working session.

    `gcloud auth login` was offered for every dead tunnel, on the reasoning that
    an expired credential is the common case. It is — and unconditional advice
    is not advice. On the one run that mattered the session was fine, the box
    was booting, and this line was the only thing on screen suggesting what to
    do next.
    """
    log = tmp_path / "comfy-linux.log"

    with pytest.raises(TunnelError) as caught:
        _spawn(writes(BOOTING_LOG), log, grace=15)

    whole = f"{caught.value} {caught.value.fix}"
    assert "gcloud auth login" not in whole, whole
    assert "still starting up" in str(caught.value), str(caught.value)


def test_a_log_that_does_ask_for_reauthentication_still_gets_told_to(tmp_path):
    """The other direction, so the fix above is not simply the advice deleted."""
    log = tmp_path / "comfy-linux.log"
    expired = ("ERROR: (gcloud.compute.ssh) There was a problem refreshing your "
               "current auth tokens: invalid_grant: Token has been expired or "
               "revoked.\nPlease run: $ gcloud auth login\n")

    with pytest.raises(TunnelError) as caught:
        _spawn(writes(expired), log, grace=15)

    assert caught.value.kind not in (SSH_NOT_READY, BACKEND_NOT_LISTENING)
    assert "gcloud auth login" in (caught.value.fix or "")


def test_a_dead_tunnel_names_its_cause_ahead_of_the_tail(tmp_path):
    """gcloud's last line is `exited with return code [255]` — the exit status
    restated, which the message already carries. The line that says why is
    somewhere above it, and is now lifted to the front."""
    log = tmp_path / "comfy-linux.log"
    refused = ("ERROR: (gcloud.compute.ssh) Could not fetch resource:\n"
               " - Permission denied on 'compute.instances.get'\n"
               "some filler\nmore filler\nyet more filler\nand more\n"
               "ERROR: (gcloud.compute.ssh) [/usr/bin/ssh] exited with return "
               "code [255].\n")

    with pytest.raises(TunnelError) as caught:
        _spawn(writes(refused), log, grace=15)

    said_first = str(caught.value).split("gcloud said")[0]
    assert "Permission denied" in said_first, str(caught.value)


# --------------------------------------------------------------------------
# FINDING A, at the seam a person actually meets it: `go` after `create`.
# --------------------------------------------------------------------------


def clock():
    """A clock that only moves when something sleeps, so a wait costs no time."""
    at = [0.0]

    def now() -> float:
        return at[0]

    def sleep(seconds: float) -> None:
        at[0] += seconds

    return now, sleep


def booting(times: int):
    """A launcher that refuses as a still-booting box would, then opens.

    Returns the launcher and the list of attempts, because "it retried" and "it
    succeeded" are two different claims and the second does not imply the first.
    """
    attempts: list[int] = []

    def launch(cmd, log):
        attempts.append(1)
        if len(attempts) <= times:
            raise TunnelError(
                "the machine is not accepting SSH connections yet",
                kind=SSH_NOT_READY, fix="wait")
        return os.getpid()

    return launch, attempts


def test_go_straight_after_create_waits_the_boot_out_instead_of_failing(tmp_path):
    """The whole finding, at the level `create`'s closing line promises it.

    `create` prints "`comfy-qat go` waits that out". Before this, `go` treated a
    not-yet-listening sshd as a hard failure on the first try, so the promise was
    false for the first four minutes of every new box's life — which is the only
    time anybody follows that line.
    """
    now, sleep = clock()
    launcher, attempts = booting(3)
    lines, say = said()

    ready = bring_up(Cloud(), LINUX, say, tunnel_dir=tmp_path, launcher=launcher,
                     sleep=sleep, now=now, probe_fn=lambda host: STAMP)

    assert ready.stamp is STAMP
    assert len(attempts) == 4, "it gave up rather than waiting the boot out"
    assert f"tunnel open: {LINUX.url}" in lines


def test_the_wait_says_what_it_is_waiting_for_while_it_waits(tmp_path):
    """A minute of silence and a hung command look identical, and this one is on a
    machine that bills by the second."""
    now, sleep = clock()
    launcher, _ = booting(3)
    lines, say = said()

    bring_up(Cloud(), LINUX, say, tunnel_dir=tmp_path, launcher=launcher,
             sleep=sleep, now=now, probe_fn=lambda host: STAMP)

    waiting = [line for line in lines if "not accepting SSH connections yet" in line]
    assert waiting, lines
    assert "up to 300s" in waiting[0], "and how long it is prepared to wait"


def test_only_a_still_booting_box_is_retried(tmp_path):
    """Everything else fails on the first try, because nothing else improves by
    being asked again and every retry is GPU time on a running box."""
    now, sleep = clock()
    attempts: list[int] = []

    def taken(cmd, log):
        attempts.append(1)
        raise TunnelError("something is already listening on 127.0.0.1:8190",
                          fix="lsof it")

    _, say = said()
    with pytest.raises(LifecycleError):
        bring_up(Cloud(), LINUX, say, tunnel_dir=tmp_path, launcher=taken,
                 sleep=sleep, now=now, probe_fn=lambda host: STAMP)

    assert len(attempts) == 1


def test_a_box_that_never_answers_names_the_machine_and_not_the_credential(tmp_path):
    """When the wait does run out, the message has to be about the box.

    This is the sentence the old code printed immediately and wrongly; it is now
    printed only after five minutes of a machine refusing, at which point it is
    true.
    """
    now, sleep = clock()

    def never(cmd, log):
        raise TunnelError("the machine is not accepting SSH connections yet",
                          kind=SSH_NOT_READY, fix="wait")

    _, say = said()
    with pytest.raises(LifecycleError) as caught:
        bring_up(Cloud(), LINUX, say, tunnel_dir=tmp_path, launcher=never,
                 sleep=sleep, now=now, probe_fn=lambda host: STAMP)

    message = f"{caught.value} {caught.value.fix}"
    assert "never started accepting SSH connections" in str(caught.value)
    assert "billing" in message, "the box is on, and the reader has to know"
    assert "gcloud auth login" not in message, message


def test_up_and_go_are_the_same_wait(monkeypatch, tmp_path):
    """`up` makes the same promise and had the same hole, and one fix covers both.

    Worth pinning rather than reasoning about: they share `bring_up`, so the wait
    lives in one place — and the day one of them grows its own path to the tunnel
    is the day this goes red instead of being noticed on real hardware.
    """
    from typer.testing import CliRunner

    from comfy_qa import lifecycle
    from comfy_qa.cli import app

    hosts = tmp_path / "hosts.toml"
    hosts.write_text(
        "[hosts.comfy-linux]\n"
        'kind = "gce"\nos = "Ubuntu 22.04"\ngpu = "L4"\n'
        'gce_instance = "comfy-linux"\ngce_zone = "europe-west4-c"\n'
        'gce_project = "proj"\nport = 8190\n',
        encoding="utf-8",
    )

    reached: list[str] = []

    def fake(gc, host, say, **kwargs):
        reached.append(host.name)
        raise LifecycleError("stop here", fix="nothing")

    monkeypatch.setattr(lifecycle, "bring_up", fake)
    monkeypatch.setattr("comfy_qa.gcloud.Gcloud", lambda *a, **k: Cloud())

    for verb in ("up", "go"):
        CliRunner().invoke(app, [verb, "comfy-linux", "--config", str(hosts)])

    assert reached == ["comfy-linux", "comfy-linux"], (
        "one of `up` and `go` no longer reaches bring_up, so it no longer waits "
        "for a box that is still starting"
    )


# --------------------------------------------------------------------------
# FINDING B: which loopback the tunnel listens on.
# --------------------------------------------------------------------------


def test_the_tunnel_answers_on_both_loopback_families():
    """`localhost` is what people type, and it is IPv6 first on macOS.

    Measured on a real box before this: `curl http://127.0.0.1:8190/` gave 200
    and 20059 bytes, `curl -6 'http://[::1]:8190/'` could not connect, and `lsof`
    showed a single `127.0.0.1:8190 (LISTEN)`. The tool prints the `127.0.0.1`
    form everywhere, so nothing was broken — but a browser handed `localhost`
    showed an error page for a tunnel that was working, and the tunnel log filled
    with `channel N: open failed` while its owner worked out why.
    """
    args = command(LINUX)

    assert "127.0.0.1:8190:127.0.0.1:8188" in args, "the IPv4 forward is the promise"
    assert "[::1]:8190:127.0.0.1:8188" in args, "and localhost now reaches it too"
    assert args.count("-L") == 2


def test_the_second_forward_can_never_take_the_first_one_down():
    """Pinned because it is the default, not in spite of it.

    A user's own `~/.ssh/config` may set `ExitOnForwardFailure yes` globally, and
    under that a box with IPv6 disabled would have the `::1` bind fail and kill
    the working IPv4 tunnel with it. The IPv6 forward is a convenience; it is
    never allowed to cost the forward this tool hands out.
    """
    args = command(LINUX)

    assert "ExitOnForwardFailure=no" in args
    assert args.index("-o") < args.index("-L"), "and set before the forwards"


# --------------------------------------------------------------------------
# FINDING C: `up` on a box that has been through `down`.
#
# Nothing on the box starts ComfyUI at boot, so after any `down` the install is
# present and dead. `up` waited its full 180 seconds and then said "ComfyUI is
# not installed or not started" and offered a shell on the box — three minutes
# of billing to reach a sentence that names two states, commits to neither, and
# points at the long way round. `comfy-qat go` on the same box found the
# install, skipped it, and had ComfyUI answering in 52 seconds.
# --------------------------------------------------------------------------


def test_up_stops_as_soon_as_it_knows_nothing_will_answer(tmp_path):
    """The 180 seconds. Waiting can only help if something is coming up.

    The clock here only moves when something sleeps, so the assertion is about
    elapsed time in the tool's own terms: a wait that ran its course would leave
    `now()` at the timeout, and a wait cut short at the first silence leaves it
    at nought.
    """
    now, sleep = clock()
    box = Cloud(comfyui="GONE", installed="INSTALLED")
    _, say = said()

    with pytest.raises(LifecycleError):
        bring_up(box, LINUX, say, tunnel_dir=tmp_path,
                 launcher=lambda cmd, log: os.getpid(), sleep=sleep, now=now,
                 probe_fn=lambda host: None, comfy_timeout=180)

    assert now() == 0.0, f"it waited {now()}s to be told what it asked in one"
    assert any("pgrep" in c or "Get-Process" in c for c in box.asked), (
        "it stopped waiting without ever asking the box why"
    )


def test_an_install_that_is_present_and_stopped_is_said_to_be_exactly_that(tmp_path):
    """Not "not installed or not started". The tool knows which; it can ask."""
    now, sleep = clock()
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        bring_up(Cloud(comfyui="GONE", installed="INSTALLED"), LINUX, say,
                 tunnel_dir=tmp_path, launcher=lambda cmd, log: os.getpid(),
                 sleep=sleep, now=now, probe_fn=lambda host: None)

    message = str(caught.value)
    assert "it is installed on comfy-linux but nothing has started it" in message
    assert "not installed or not started" not in message, message
    assert "billing" in message


def test_a_box_that_never_had_comfyui_is_said_to_be_exactly_that(tmp_path):
    """The other half of the `or`, so the split is a real question and not a
    single new sentence replacing a single old one."""
    now, sleep = clock()
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        bring_up(Cloud(comfyui="GONE", installed="MISSING"), LINUX, say,
                 tunnel_dir=tmp_path, launcher=lambda cmd, log: os.getpid(),
                 sleep=sleep, now=now, probe_fn=lambda host: None)

    assert "it is not installed on comfy-linux" in str(caught.value)


def test_the_fix_names_go_and_not_a_session_on_the_machine(tmp_path):
    """The remedy was one sibling command away and the message named the long
    way round: `ssh` onto the box and install or start ComfyUI by hand."""
    now, sleep = clock()
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        bring_up(Cloud(comfyui="GONE", installed="INSTALLED"), LINUX, say,
                 tunnel_dir=tmp_path, launcher=lambda cmd, log: os.getpid(),
                 sleep=sleep, now=now, probe_fn=lambda host: None)

    fix = caught.value.fix
    assert "comfy-qat go comfy-linux" in fix, fix
    assert "comfy-qat ssh" not in fix, "the hard way, for a one-command problem"
    assert "comfy-qat down comfy-linux" in fix, "and how to stop paying"


def test_a_box_that_will_not_say_keeps_the_honest_or(tmp_path):
    """The one place "not installed or not started" is the truth.

    Deleting that sentence outright would have been the easy read of this
    finding and the wrong one: when the box will not answer, a message that
    picks a side is guessing, and "not installed" is the guess that sends
    somebody to reinstall over a working install.
    """
    now, sleep = clock()
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        bring_up(Cloud(comfyui=None), LINUX, say, tunnel_dir=tmp_path,
                 launcher=lambda cmd, log: os.getpid(), sleep=sleep, now=now,
                 probe_fn=lambda host: None, comfy_timeout=0)

    assert "not installed or not started" in str(caught.value)
    assert "comfy-qat go comfy-linux" in caught.value.fix, "and `go` still leads"


def test_a_comfyui_that_is_still_coming_up_is_waited_for(tmp_path):
    """ALIVE is not an answer that ends the wait. A ComfyUI that is running and
    has not bound its port yet is the case the timeout exists for, and cutting
    it short would trade three wasted minutes for a box reported broken while it
    was starting."""
    now, sleep = clock()
    _, say = said()

    with pytest.raises(LifecycleError):
        bring_up(Cloud(comfyui="ALIVE"), LINUX, say, tunnel_dir=tmp_path,
                 launcher=lambda cmd, log: os.getpid(), sleep=sleep, now=now,
                 probe_fn=lambda host: None, comfy_timeout=180)

    assert now() >= 180, "it gave up on a ComfyUI that was still starting"


def test_go_does_not_pay_for_the_question_it_is_about_to_answer(tmp_path):
    """`go` and `switch` opt out, and the reason is not tidiness.

    They do not stop at a silent tunnel — `_serve` runs next and installs or
    launches ComfyUI, and `ensure_installed` asks the box the same question
    properly seconds later. An SSH round trip here would be a cost on the
    everyday path of the everyday command, for a sentence nobody would read.
    """
    now, sleep = clock()
    box = Cloud()
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        bring_up(box, LINUX, say, tunnel_dir=tmp_path,
                 launcher=lambda cmd, log: os.getpid(), sleep=sleep, now=now,
                 probe_fn=lambda host: None, comfy_timeout=0,
                 explain_silence=False)

    assert box.asked == [], "go paid for a question it did not need"
    assert caught.value.kind == COMFYUI_ABSENT, "and `go` must still continue past it"


def test_up_still_refuses_to_call_a_silent_box_up(tmp_path):
    """The guarantee that motivated `up`'s docstring, kept while fixing it.

    The fix for this finding was NOT to make `up` start ComfyUI — that would
    leave the tool with two commands that both do, differing only in whether
    they open a browser. `up` stays the machine-level verb, and what it still
    refuses to do is report a booted box that serves nothing as a success.
    """
    now, sleep = clock()
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        bring_up(Cloud(), LINUX, say, tunnel_dir=tmp_path,
                 launcher=lambda cmd, log: os.getpid(), sleep=sleep, now=now,
                 probe_fn=lambda host: None)

    assert caught.value.kind == COMFYUI_ABSENT
