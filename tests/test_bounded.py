"""How long a command may run against a machine that bills by the second.

From a real run: `comfy-qat go` against a V100 box whose GPU driver could not
load was still going at **fifteen minutes**, with `/opt/comfyui` never created,
no install process on the box, and nothing on the terminal since its first line.
It was killed by hand.

Nothing here was unbounded, which is what made it hard to see. `wait_for_driver`
was doing exactly what it says — waiting out `DRIVER_TIMEOUT`, which is 900s,
which is fifteen minutes — and it was doing it in total silence, because its
ticker is a `background=False` `Slow` and nothing ever called `tick()`.

Two separate defects, and the pair is the finding:

  * **Silence.** One line, then 900 seconds of nothing. A tester cannot tell that
    from a hung command, and killing it is the correct response to a tool that
    has stopped speaking.
  * **No total.** Every wait had its own bound and every one of them started a
    FRESH clock, so the bounds stack — 33 minutes before the install begins, and
    the install streams with no timeout at all. "Every wait is bounded" and "the
    command is bounded" are different claims and only the first was true.
"""

from __future__ import annotations

import pytest

from comfy_qa.config import Host
from comfy_qa.gcloud import GcloudError
from comfy_qa.lifecycle import (
    BOOT_TIMEOUT,
    COMFY_TIMEOUT,
    DRIVER_TIMEOUT,
    GO_BUDGET,
    SSH_READY_TIMEOUT,
    Budget,
    LifecycleError,
    ensure_installed,
    wait_for_driver,
    wait_for_ssh,
)

LINUX = Host(name="comfy-linux", kind="gce", port=8190, os="Ubuntu 22.04", gpu="L4",
             gce_instance="comfy-linux", gce_zone="europe-west4-c",
             gce_project="proj")


def clock():
    """A clock that only moves when something sleeps. Waits cost no real time."""
    at = [0.0]

    def now() -> float:
        return at[0]

    def sleep(seconds: float) -> None:
        at[0] += seconds

    return now, sleep


def said():
    lines: list[str] = []
    return lines, lines.append


class Silent:
    """A box that never answers the question being asked of it."""

    def __init__(self, answers=None) -> None:
        self.answers = answers or {}
        self.asked: list[str] = []

    def ssh_output(self, instance, zone, project, command):
        self.asked.append(command)
        for mark, answer in self.answers.items():
            if mark in command:
                return answer
        raise GcloudError("could not connect")

    def ssh(self, *args, **kwargs):
        raise AssertionError("the install must not be started in this test")

    def __getattr__(self, name):
        def unexpected(*args, **kwargs):
            raise AssertionError(f"{name} was not expected here")
        return unexpected


# --------------------------------------------------------------------------
# The silence.
# --------------------------------------------------------------------------


def test_the_driver_wait_says_it_is_still_going():
    """The fifteen minutes of nothing, which is what got the command killed.

    `output.slow(background=False)` prints only when it is told to, and this loop
    slept without telling it. Measured against this exact function before the
    fix: elapsed 900s, lines printed 1. Every other wait in the module already
    ticked; the longest one, by a factor of three, did not.
    """
    now, sleep = clock()
    lines, say = said()

    with pytest.raises(LifecycleError):
        wait_for_driver(Silent(), LINUX, say, now=now, sleep=sleep)

    assert now() == DRIVER_TIMEOUT, "the fixture stopped exercising the long wait"
    ticks = [line for line in lines if "still going" in line]
    assert ticks, f"{len(lines)} line(s) in {now()}s of waiting: {lines}"
    # And the elapsed time is IN the tick, which is the fact a person is deciding
    # on: a wait that says how long it has been running is one you can leave.
    assert any("m" in tick for tick in ticks), ticks


def test_the_driver_wait_says_how_long_it_is_prepared_to_wait():
    """Fifteen minutes is bearable if you know it is fifteen minutes."""
    now, sleep = clock()
    lines, say = said()

    with pytest.raises(LifecycleError):
        wait_for_driver(Silent(), LINUX, say, now=now, sleep=sleep)

    assert any(f"up to {DRIVER_TIMEOUT}s" in line for line in lines), lines


def test_the_ssh_wait_ticks_too():
    """The milder sibling: one sentence, then up to five minutes of nothing."""
    now, sleep = clock()
    lines, say = said()

    with pytest.raises(LifecycleError):
        wait_for_ssh(Silent(), LINUX, say, now=now, sleep=sleep)

    assert any("still going" in line for line in lines), lines
    assert any("up to 300s" in line for line in lines), lines


# --------------------------------------------------------------------------
# The total.
# --------------------------------------------------------------------------


def test_every_wait_being_bounded_is_not_the_command_being_bounded():
    """The arithmetic behind the finding, written down so it cannot drift.

    These are the phases that can stack inside one `go`. Each is individually
    correct and the sum is a different number from any of them — which is how a
    command all of whose waits are bounded ran for fifteen minutes and could have
    run for longer.
    """
    stackable = (BOOT_TIMEOUT + SSH_READY_TIMEOUT + COMFY_TIMEOUT
                 + 300 + DRIVER_TIMEOUT)

    assert stackable > GO_BUDGET, (
        f"the phases sum to {stackable}s and the whole-command budget is "
        f"{GO_BUDGET}s. If the sum ever drops below the budget the budget has "
        f"stopped being the binding constraint, and this test is the only thing "
        f"that would say so."
    )
    assert GO_BUDGET == 1800, "half an hour; see GO_BUDGET for why this number"


def test_a_phase_may_not_outlive_the_whole_command():
    """A budget with a minute left does not buy a fifteen-minute wait."""
    now, sleep = clock()
    budget = Budget(GO_BUDGET, now=now)
    # Everything but a minute of it has already gone on earlier phases.
    sleep(GO_BUDGET - 60)
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        wait_for_driver(Silent(), LINUX, say, now=now, sleep=sleep, budget=budget)

    assert now() == GO_BUDGET, f"it ran to {now()}s, past the whole command's clock"
    assert "this has been working on comfy-linux" in str(caught.value), caught.value


def test_running_out_of_clock_reports_the_whole_time_and_the_bill():
    """One message for every phase, because the fact is about the command.

    "the driver took too long" is a detail inside "this has been going for half
    an hour on a machine that is billing", and the second is what decides what
    somebody does next.
    """
    now, sleep = clock()
    budget = Budget(GO_BUDGET, now=now)
    sleep(GO_BUDGET)
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        wait_for_driver(Silent(), LINUX, say, now=now, sleep=sleep, budget=budget)

    message = str(caught.value)
    assert "30m00s" in message, message
    assert "the GPU driver has still not come up" in message, message
    assert "up and billing" in message
    assert "comfy-qat down comfy-linux" in caught.value.fix


def test_the_install_is_not_started_with_no_clock_left():
    """The one step the budget cannot cut short once it is running.

    The install streams through `gc.ssh`, which has no timeout of its own, so the
    clock is only readable on either side of it. Refusing on the way in is also
    the last moment at which nothing has been half-written to the box — `Silent`
    fails the test outright if `ssh` is reached.
    """
    now, sleep = clock()
    budget = Budget(GO_BUDGET, now=now)
    box = Silent({"nvidia-smi": "GPU 0: L4", "INSTALLED": "MISSING"})
    sleep(GO_BUDGET)
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        ensure_installed(box, LINUX, say, budget=budget)

    assert "ComfyUI is still not installed" in str(caught.value), caught.value


def test_no_budget_means_each_phase_keeps_its_own_bound():
    """`None` is a real value, and it is what every existing caller passes.

    The budget was added without changing any behaviour until somebody asked for
    it, which is what let it be threaded through six functions at once.
    """
    now, sleep = clock()
    _, say = said()

    with pytest.raises(LifecycleError):
        wait_for_driver(Silent(), LINUX, say, now=now, sleep=sleep, budget=None)

    assert now() == DRIVER_TIMEOUT


def test_the_budget_never_hands_a_phase_a_negative_timeout():
    """A negative timeout is a deadline in the past, which every wait reads as
    its OWN clock having expired — so the command would report the driver's
    fifteen seconds rather than the command's half hour."""
    now, sleep = clock()
    budget = Budget(GO_BUDGET, now=now)
    sleep(GO_BUDGET * 2)

    assert budget.allow(DRIVER_TIMEOUT) == 0
    assert budget.gone


# --------------------------------------------------------------------------
# One clock, not one per half.
# --------------------------------------------------------------------------


def test_go_carries_one_clock_across_both_halves(tmp_path, monkeypatch):
    """`go` is `_bring_up` then `_serve`, and the budget spans both.

    A budget started at the install would bound the wrong thing: the minutes
    spent booting the box and tunnelling to it are part of how long the command
    has been running, and on the real failure they were most of it.
    """
    from typer.testing import CliRunner

    from comfy_qa import host as host_module
    from comfy_qa.cli import app

    hosts = tmp_path / "hosts.toml"
    hosts.write_text(
        "[hosts.comfy-linux]\n"
        'kind = "gce"\nos = "Ubuntu 22.04"\ngpu = "L4"\n'
        'gce_instance = "comfy-linux"\ngce_zone = "europe-west4-c"\n'
        'gce_project = "proj"\nport = 8190\n',
        encoding="utf-8",
    )

    seen = []
    monkeypatch.setattr(host_module, "_bring_up",
                        lambda gc, host, hosts_, *a, **kw: seen.append(kw.get("budget")))
    monkeypatch.setattr(host_module, "_serve",
                        lambda gc, host, ready, **kw: seen.append(kw.get("budget")))
    monkeypatch.setattr("comfy_qa.gcloud.Gcloud", lambda *a, **k: object())

    CliRunner().invoke(app, ["go", "comfy-linux", "--config", str(hosts)])

    assert len(seen) == 2, seen
    assert seen[0] is not None, "`go` started no clock at all"
    assert seen[0] is seen[1], (
        "the two halves of `go` are running on different clocks, so neither "
        "bounds the command"
    )
    assert seen[0].total == GO_BUDGET
