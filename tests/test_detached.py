"""ComfyUI runs on the box, so the terminal does not have to.

`go` used to stream ComfyUI's log back over SSH, which is the only reason it
owned a terminal until Ctrl-C — and why two machines could not be used at once,
which is the ordinary case rather than an exotic one. These tests hold the two
halves of fixing that:

  * the log moves to a file **on the box**, and `host logs` reads it;
  * *nothing else moves*. "Started" is still not "serving". A detached launch
    that returned as soon as the box said STARTED would be the booted-VM lie one
    level down — started, billing, serving nothing — so it is still not finished
    until ComfyUI has answered on the tunnel.

The second half is the one worth defending with tests, because it is the one a
plausible-looking refactor deletes.
"""

from __future__ import annotations

import re

import pytest

from comfy_qa.config import Host
from comfy_qa.gcloud import Gcloud, GcloudError
from comfy_qa.lifecycle import (
    TUNNEL_DOWN,
    LifecycleError,
    in_a_new_window,
    read_logs,
    start_detached,
)
from comfy_qa.provision import (
    ALIVE,
    GONE,
    LINUX_LOG,
    NO_LOG_EXIT,
    NO_PYTHON_EXIT,
    STARTED,
    WINDOWS_LOG,
    alive_command,
    launch_command,
    launch_detached_command,
    log_for,
    logs_command,
)
from comfy_qa.stamp import Stamp

WIN = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022", gpu="L4",
           gce_instance="comfy-win", gce_zone="us-central1-a", gce_project="proj")
LINUX = Host(name="comfy-linux", kind="gce", port=8191, os="Ubuntu 22.04", gpu="A100",
             gce_instance="comfy-linux", gce_zone="us-central1-a", gce_project="proj")
LOCAL = Host(name="local", kind="local", port=8188)

STAMP = Stamp(host="comfy-win", url="http://127.0.0.1:8190", comfyui_version="0.33.0")


def said():
    lines = []
    return lines, lines.append


def box(*, status="RUNNING", port_holder="", launch=0, alive=ALIVE, log="",
        repair=0, log_exit=0):
    """A Gcloud whose box answers the way this test wants it to.

    Scripted per question rather than per call order, because the detached flow
    asks several different things and a positional script would break every time
    one more question is added.
    """
    launches = list(launch) if isinstance(launch, list) else [launch]
    calls: list[str] = []

    def runner(args, mode):
        joined = " ".join(args)
        calls.append(joined)
        if joined.startswith("compute instances describe"):
            return {"status": status}
        if joined.startswith("compute instances "):
            return ""
        launching = "nohup" in joined or "Start-Process -FilePath 'powershell'" in joined
        reading = "tail -n" in joined or "Get-Content -Path" in joined
        asking = "pgrep -f" in joined or "Get-Process -Name python" in joined
        holding = "NetTCPConnection" in joined or "sport = :" in joined
        if mode == "output":
            if holding:
                return port_holder or "PORT_FREE"
            if asking:
                return alive
            if reading:
                return log
            return "READY"
        if launching:
            return launches.pop(0) if len(launches) > 1 else launches[0]
        if "pip install -r requirements.txt" in joined:
            return repair
        if reading:
            return log_exit
        return 0

    gc = Gcloud(runner=runner)
    gc.calls = calls  # type: ignore[attr-defined]
    return gc


def detach(host, gc, say, **kwargs):
    """`start_detached` with the clock and the tunnel taken out of the way."""
    kwargs.setdefault("sleep", lambda _seconds: None)
    kwargs.setdefault("timeout", 0)
    return start_detached(gc, host, say, **kwargs)


@pytest.fixture
def no_tunnel(monkeypatch):
    """A local forward that will not open.

    An IAP or a local problem, never the box's — which is the whole reason the
    two endings below have to be told apart.
    """
    from comfy_qa import lifecycle
    from comfy_qa.tunnel import TunnelError

    def refuse(host, tunnel_dir=None):
        raise TunnelError("could not open the tunnel")

    monkeypatch.setattr(lifecycle, "open_tunnel", refuse)


def box_that_holds_its_port_once_launched(**kwargs):
    """PORT_FREE when the launch is decided, held afterwards.

    The shape a real launch makes: nothing on the port, then ComfyUI on it. It
    is the only way to see whether the ending stopped what the launch started,
    because a box that never held the port has nothing to stop.
    """
    gc = box(**kwargs)
    inner = gc.runner
    launched = []

    def runner(args, mode):
        joined = " ".join(args)
        holding = "NetTCPConnection" in joined or "sport = :" in joined
        if holding and mode == "output":
            return "4242 python" if launched else "PORT_FREE"
        if "nohup" in joined or "Start-Process -FilePath 'powershell'" in joined:
            launched.append(joined)
        return inner(args, mode)

    gc.runner = runner
    return gc


def stops_in(gc):
    return [call for call in gc.calls if "kill 4242" in call
            or "Stop-Process -Id 4242" in call]


# --- the promise: it comes back, and only once ComfyUI answers -------------


@pytest.mark.parametrize("host", [WIN, LINUX], ids=["windows", "linux"])
def test_a_detached_launch_returns_once_comfyui_answers(host, tmp_path):
    lines, say = said()
    gc = box()

    code = detach(host, gc, say, tunnel_dir=tmp_path, probe_fn=lambda h: STAMP)

    assert code == 0
    told = " ".join(lines)
    assert "stays running on the box after this command returns" in told
    assert "ComfyUI answering" in told


@pytest.mark.parametrize("host", [WIN, LINUX], ids=["windows", "linux"])
def test_started_is_not_serving(host, tmp_path):
    """The whole premise of this tool, one level down.

    A launch that came back on the box's say-so would leave a GPU machine
    billing while ComfyUI failed to import something — which is exactly the
    failure `bring_up` exists to refuse for the VM itself.
    """
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        detach(host, box(), say, tunnel_dir=tmp_path, probe_fn=lambda h: None)

    assert "without ever answering" in str(caught.value)
    assert "billing" in str(caught.value)
    assert f"comfy-qat down {host.name}" in caught.value.fix
    assert f"comfy-qat logs {host.name}" in caught.value.fix


# --- never asked is not the same as asked and silent -----------------------
#
# The wait has three ways out and only one of them means ComfyUI did not answer.
# A forward that never opens skips the probe entirely, so the deadline used to
# arrive with the question unasked and be reported as ComfyUI having "exited
# without ever answering" — after stopping it. Both halves of that sentence were
# wrong, and the tool had just killed a working server to say it.


def test_a_tunnel_that_never_opens_is_not_reported_as_comfyui_never_answering(
        no_tunnel, tmp_path):
    lines, say = said()
    gc = box_that_holds_its_port_once_launched(log="Starting server")

    with pytest.raises(LifecycleError) as caught:
        detach(LINUX, gc, say, tunnel_dir=tmp_path, probe_fn=lambda h: None)

    assert caught.value.kind == TUNNEL_DOWN
    assert "never opened" in str(caught.value)
    assert "it was not asked" in str(caught.value)
    assert "without ever answering" not in str(caught.value)
    # The money half: a healthy ComfyUI is left alone. Killing it is what made
    # this worth fixing rather than rewording.
    assert stops_in(gc) == []
    assert "the last of its log on" not in " ".join(lines)
    # And it still says which box is costing money, and how to stop it.
    assert f"comfy-qat down {LINUX.name}" in caught.value.fix
    assert f"comfy-qat open {LINUX.name}" in caught.value.fix


def test_the_tunnel_ending_says_what_the_box_said_about_comfyui(no_tunnel, tmp_path):
    """Three answers, not two: up and unreachable, gone, or unanswerable — and
    the last is not a reason to stop anything."""
    for alive, expected in ((ALIVE, "is running on comfy-linux and has been left "
                                    "running"),
                            (GONE, "is no longer running on comfy-linux either"),
                            ("", "would not say whether ComfyUI is still running")):
        gc = box_that_holds_its_port_once_launched(alive=alive)
        with pytest.raises(LifecycleError) as caught:
            detach(LINUX, gc, said()[1], tunnel_dir=tmp_path,
                   probe_fn=lambda h: None)
        assert expected in str(caught.value), alive
        assert stops_in(gc) == [], alive


def test_a_tunnel_that_does_open_still_ends_the_old_way(tmp_path):
    """The guard on the fix. A launch that WAS asked and stayed silent is still
    the failure it always was: the log is read, what this run started is
    stopped, and it says so."""
    lines, say = said()
    gc = box_that_holds_its_port_once_launched(log="Traceback (most recent call last)")

    with pytest.raises(LifecycleError) as caught:
        detach(LINUX, gc, say, tunnel_dir=tmp_path, probe_fn=lambda h: None)

    assert "without ever answering" in str(caught.value)
    assert stops_in(gc) != []
    assert "the last of its log on" in " ".join(lines)


def test_a_held_port_with_no_tunnel_is_not_called_not_answering(no_tunnel, tmp_path):
    """The sibling, one step earlier. `_open_forward`'s answer was discarded, so
    the probe went through a tunnel that was not there, and the guaranteed
    silence became "it is not answering as ComfyUI" plus an offer to kill it."""
    _, say = said()
    gc = box(port_holder="2804 python")

    with pytest.raises(LifecycleError) as caught:
        detach(WIN, gc, say, tunnel_dir=tmp_path, probe_fn=lambda h: None)

    assert caught.value.kind == TUNNEL_DOWN
    assert "could not be asked whether it is ComfyUI" in str(caught.value)
    assert "nothing was stopped" in str(caught.value)
    assert "not answering as ComfyUI" not in str(caught.value)
    assert "Stop-Process -Id 2804" not in caught.value.fix


def test_the_detached_launch_is_the_one_that_runs_not_the_foreground_one(tmp_path):
    _, say = said()
    gc = box()

    detach(LINUX, gc, say, tunnel_dir=tmp_path, probe_fn=lambda h: STAMP)

    ran = " ".join(gc.calls)
    assert "nohup" in ran, "the launch has to survive this command returning"
    assert LINUX_LOG in ran, "and its output has to land somewhere readable"


def test_no_python_on_the_box_is_still_named_as_itself(tmp_path):
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        detach(WIN, box(launch=NO_PYTHON_EXIT), say, tunnel_dir=tmp_path,
               probe_fn=lambda h: None)

    assert "NO_PYTHON" in str(caught.value)
    assert "comfy-qat rdp" in caught.value.fix, "the way onto a Windows box"


# --- the cost of detaching, bought back ------------------------------------


def test_the_log_on_the_box_is_quoted_when_nothing_ever_answers(tmp_path):
    """The tester can no longer watch the startup log scroll past.

    So a failure that said "it never answered" and nothing else would be a worse
    failure than the one detaching replaced. The end of the log is read off the
    box while the box is still reachable, and printed.
    """
    lines, say = said()
    gc = box(log="Traceback (most recent call last):\n  RuntimeError: no CUDA")

    with pytest.raises(LifecycleError):
        detach(LINUX, gc, say, tunnel_dir=tmp_path, probe_fn=lambda h: None)

    told = "\n".join(lines)
    assert "the last of its log on comfy-linux:" in told
    assert "RuntimeError: no CUDA" in told


def test_a_comfyui_that_died_ends_the_wait_instead_of_running_it_out(tmp_path):
    """A ComfyUI that dies four seconds in looks exactly like a slow one.

    The difference is three minutes of GPU time on a machine that is billing,
    so the box is asked — not on every turn, which would be an SSH round trip
    each time, but often enough that the answer is not three minutes late.
    """
    lines, say = said()
    clock = {"t": 0.0}

    def now():
        return clock["t"]

    def sleep(seconds):
        clock["t"] += seconds

    with pytest.raises(LifecycleError):
        start_detached(box(alive=GONE), LINUX, said()[1], tunnel_dir=tmp_path,
                       probe_fn=lambda h: None, sleep=sleep, now=now, timeout=600)

    assert clock["t"] < 600, "it waited out the whole timeout for a dead process"

    lines, say = said()
    with pytest.raises(LifecycleError):
        start_detached(box(alive=GONE), LINUX, say, tunnel_dir=tmp_path,
                       probe_fn=lambda h: None, sleep=lambda _s: None,
                       now=lambda: 0.0, timeout=0)
    assert not any("no longer running" in line for line in lines), (
        "with no time elapsed there is nothing to report yet"
    )


def test_a_box_that_will_not_say_whether_it_is_alive_is_waited_for(tmp_path):
    """None is not False. Reporting "cannot tell" as "it died" would end a wait
    that was going to succeed."""
    answers = iter([ALIVE, ""])
    lines, say = said()
    clock = {"t": 0.0}

    def runner(args, mode):
        joined = " ".join(args)
        if joined.startswith("compute instances describe"):
            return {"status": "RUNNING"}
        if mode == "output":
            if "pgrep -f" in joined:
                return next(answers, "")
            if "sport = :" in joined:
                return "PORT_FREE"
            return ""
        return 0

    def sleep(seconds):
        clock["t"] += seconds
        if clock["t"] > 200:
            raise AssertionError("it should have stopped at the timeout")

    with pytest.raises(LifecycleError) as caught:
        start_detached(Gcloud(runner=runner), LINUX, say, tunnel_dir=tmp_path,
                       probe_fn=lambda h: None, sleep=sleep,
                       now=lambda: clock["t"], timeout=120)

    assert "without ever answering" in str(caught.value)


def test_a_missing_dependency_in_the_log_is_repaired_once(tmp_path):
    """From a real box on 2026-08-27: `ModuleNotFoundError: sqlalchemy`.

    Detached, that failure no longer shows up as a non-zero exit — the launcher
    returns 0 and ComfyUI dies afterwards — so it has to be read out of the log
    or the repair is lost along with the foreground launch.
    """
    lines, say = said()
    gc = box(log="ModuleNotFoundError: No module named 'sqlalchemy'")

    with pytest.raises(LifecycleError):
        detach(LINUX, gc, say, tunnel_dir=tmp_path, probe_fn=lambda h: None)

    told = "\n".join(lines)
    assert told.count("installing its requirements") == 1, "once, not a loop"
    assert sum("pip install -r requirements.txt" in call for call in gc.calls) == 1


def test_a_log_that_says_nothing_useful_is_not_repaired_speculatively(tmp_path):
    lines, say = said()
    gc = box(log="loading model...")

    with pytest.raises(LifecycleError):
        detach(LINUX, gc, say, tunnel_dir=tmp_path, probe_fn=lambda h: None)

    assert not any("installing its requirements" in line for line in lines)


def test_what_this_run_started_is_stopped_when_it_never_answered(tmp_path):
    """`_stop_ours` was a tidy-up and is now load-bearing.

    A detached ComfyUI that binds the port and then dies leaves the port held,
    and every later launch fails with ComfyUI's own "Port 8188 is already in
    use" — which names neither the process nor the tool that left it there.
    """
    lines, say = said()
    gc = box(port_holder="")

    holders = iter(["PORT_FREE", "2380 python"])

    def runner(args, mode):
        joined = " ".join(args)
        gc.calls.append(joined)
        if joined.startswith("compute instances describe"):
            return {"status": "RUNNING"}
        if mode == "output":
            if "sport = :" in joined:
                return next(holders, "2380 python")
            return ""
        return 0

    with pytest.raises(LifecycleError):
        start_detached(Gcloud(runner=runner), LINUX, say, tunnel_dir=tmp_path,
                       probe_fn=lambda h: None, sleep=lambda _s: None, timeout=0)

    assert any("stopped the ComfyUI this run started" in line for line in lines)


# --- the two things fixed in the last day, still true ----------------------


def test_a_port_held_by_a_working_comfyui_is_used_not_refused(tmp_path):
    """It serves, so it is not in the way — it is the answer."""
    lines, say = said()
    opened = []

    code = detach(WIN, box(port_holder="2804 python"), say, tunnel_dir=tmp_path,
                  probe_fn=lambda h: STAMP, open_browser=opened.append)

    told = " ".join(lines)
    assert code == 0
    assert "using it rather than starting a second one" in told
    assert not any("stays running on the box" in line for line in lines), "no second one"
    assert opened == [WIN.url]


def test_a_port_held_by_something_else_still_refuses(tmp_path):
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        detach(WIN, box(port_holder="2804 nginx"), say, tunnel_dir=tmp_path,
               probe_fn=lambda h: None)

    assert "not answering as ComfyUI" in str(caught.value)
    assert "Stop-Process" in caught.value.fix


def test_the_url_for_this_machine_is_said_alongside_the_one_comfyui_prints(tmp_path):
    """ComfyUI announces `http://127.0.0.1:8188`, which is true on the box and
    wrong here, where 8188 is the local install. It is the first line of the log
    `host logs` will show, so the right one is said beside it."""
    lines, say = said()

    detach(WIN, box(), say, tunnel_dir=tmp_path, probe_fn=lambda h: STAMP)

    told = " ".join(lines)
    assert "on this machine that is" in told
    assert WIN.url in told


# --- the machine you were sent to is the machine you asked for -------------
#
# Nothing in a browser tells two ComfyUIs apart: same title, same canvas, same
# favicon, and `127.0.0.1:<port>` the only difference. `mismatch` already writes
# the sentence that catches it and had exactly one caller — `host stamp` — so
# `go` printed "ComfyUI answering: …" and opened a tab onto the contradiction
# without a word. These two hold the fix at the one place it matters: the moment
# before a browser is pointed at a machine.


def watched(lines):
    """A browser whose opening is recorded IN ORDER with what was said.

    Two separate lists cannot say which happened first, and "the identity is on
    the screen before the tab takes over" is entirely a question of order.
    """
    opened = []

    def open_browser(url):
        opened.append(url)
        lines.append(f"<browser {url}>")

    return opened, open_browser


def test_a_machine_that_answers_as_another_machine_is_refused_not_opened(tmp_path):
    """The Mac answering on a Windows box's port is the failure this prevents.

    Refused rather than warned, following `host stamp`: a warning scrolls past
    and the tab opens anyway, and everything generated in it is then attributed
    to the machine that was named rather than the one that ran it.
    """
    lines, say = said()
    opened, open_browser = watched(lines)
    mac = Stamp(host="comfy-win", url=WIN.url, os="darwin", devices=["mps"],
                comfyui_version="0.33.0")

    with pytest.raises(LifecycleError) as caught:
        detach(WIN, box(), say, tunnel_dir=tmp_path, probe_fn=lambda h: mac,
               open_browser=open_browser)

    assert "answered as darwin" in str(caught.value)
    assert "That port is not reaching comfy-win" in str(caught.value)
    assert opened == [], "a browser was opened onto a contradicting machine"
    assert f"comfy-qat down {WIN.name}" in caught.value.fix, "the box is still billing"


def test_the_machine_is_named_beside_the_url_the_browser_is_about_to_open(tmp_path):
    """The last thing said before the tab takes over says WHICH machine it is.

    "ComfyUI answering: …" is several lines and a startup log above by then, and
    under a detached launch the url is printed last of all — so the url carries
    the identity itself rather than relying on what has scrolled past.
    """
    lines, say = said()
    opened, open_browser = watched(lines)

    detach(WIN, box(), say, tunnel_dir=tmp_path, probe_fn=lambda h: STAMP,
           open_browser=open_browser)

    assert opened == [WIN.url]
    assert lines[-1] == f"<browser {WIN.url}>", "the browser opened last"
    assert WIN.url in lines[-2] and STAMP.line() in lines[-2], (
        f"the url and the machine it reaches are not on one line: {lines[-2]!r}"
    )


# --- host logs -------------------------------------------------------------


def test_logs_follows_a_running_box(tmp_path):
    _, say = said()
    gc = box()

    assert read_logs(gc, LINUX, say, tail=50, follow=True) == 0
    assert any("tail -n 50 -f" in call for call in gc.calls)


def test_logs_on_a_stopped_box_says_so_rather_than_hanging():
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        read_logs(box(status="TERMINATED"), WIN, say)

    assert "is not running, so it has no ComfyUI and no log to follow" in str(caught.value)
    assert "comfy-qat go comfy-win" in caught.value.fix


def test_logs_with_no_log_file_says_nothing_started_comfyui_there():
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        read_logs(box(log_exit=NO_LOG_EXIT), WIN, say)

    said_it = str(caught.value)
    assert WINDOWS_LOG in said_it
    assert "The machine is running and billing" in said_it, "it is on, and it is on you"
    assert "comfy-qat down comfy-win" in caught.value.fix


def test_logs_on_the_local_machine_refuses_and_says_where_its_log_is():
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        read_logs(box(), LOCAL, say)

    assert "this tool did not start its ComfyUI" in str(caught.value)
    assert "main.py" in caught.value.fix


def test_a_box_that_will_not_run_the_read_is_reported_with_the_way_in():
    def runner(args, mode):
        if " ".join(args).startswith("compute instances describe"):
            return {"status": "RUNNING"}
        raise GcloudError("failed to connect to backend")

    _, say = said()
    with pytest.raises(LifecycleError) as caught:
        read_logs(Gcloud(runner=runner), WIN, say)

    assert "could not read the ComfyUI log on comfy-win" in str(caught.value)
    assert "comfy-qat down comfy-win" in caught.value.fix


# --- the commands that run on the box --------------------------------------


@pytest.mark.parametrize("host", [WIN, LINUX], ids=["windows", "linux"])
def test_a_detached_launch_writes_a_log_the_reader_can_find(host):
    """Whatever the launch writes to, `logs` has to read from. Two constants
    drifting apart would be invisible until someone followed an empty file."""
    launch = launch_detached_command(host)
    assert log_for(host) in launch
    assert log_for(host) in logs_command(host, follow=True)
    assert log_for(host) in logs_command(host, tail=5, follow=False)


@pytest.mark.parametrize("host", [WIN, LINUX], ids=["windows", "linux"])
def test_each_launch_truncates_the_log_rather_than_appending(host):
    """`host logs` is asked about *this* ComfyUI. Yesterday's traceback sitting
    above today's startup is how twenty minutes go on something already fixed."""
    launch = launch_detached_command(host)
    assert ">> " not in launch and "-Append" not in launch


def test_the_windows_launch_keeps_all_three_powershell_rules():
    """The three that have each cost this project real time, at the top of
    provision.py. A new script is exactly where they come back."""
    launch = launch_detached_command(WIN)
    assert launch.isascii(), "a BOM-less .ps1 is read as CP1252; keep it ASCII"
    assert "ErrorActionPreference" not in launch, "Stop turns native stderr fatal"
    assert "Split-Path" not in launch, "it prompts, and a prompt hangs SSH forever"
    assert "-NonInteractive" in launch


def test_the_windows_launch_quotes_its_interpreter_without_a_literal_quote():
    """The command is already inside double quotes by the time it reaches the
    box, so a literal `"` here would end it — and an interpreter path with a
    space in it is exactly what `Get-Command python` hands back."""
    launch = launch_detached_command(WIN)
    assert "[char]34" in launch
    assert launch.count('"') == 2, "the only double quotes are the ones round -Command"


@pytest.mark.parametrize("host", [WIN, LINUX], ids=["windows", "linux"])
def test_the_detached_launch_still_binds_loopback(host):
    """The forward is an `ssh -L`, which resolves the far address on the box, so
    loopback is exactly right and nothing is exposed on any interface."""
    assert "--listen 127.0.0.1" in launch_detached_command(host)
    assert "--listen 127.0.0.1" in launch_command(host), "and the foreground one too"


@pytest.mark.parametrize("host", [WIN, LINUX], ids=["windows", "linux"])
def test_a_missing_log_exits_rather_than_printing_a_word_into_the_log(host):
    """A marker at the top of a log the tester is reading is a word nobody asked
    for. The exit code carries it instead."""
    reading = logs_command(host, tail=10, follow=False)
    assert str(NO_LOG_EXIT) in reading
    assert "NO_LOG" not in reading


@pytest.mark.parametrize("host", [WIN, LINUX], ids=["windows", "linux"])
def test_following_and_not_following_are_different_commands(host):
    assert logs_command(host, follow=True) != logs_command(host, follow=False)
    for follow in (True, False):
        assert logs_command(host, tail=7, follow=follow).count(" 7") >= 1


def test_the_linux_liveness_check_does_not_match_the_command_carrying_it():
    """`pgrep -f 'main.py --listen'` matches the shell running that very command,
    so a dead ComfyUI reads as alive forever. The bracket is what stops it."""
    asking = alive_command(LINUX)
    assert "[m]ain.py" in asking
    assert not re.search(r"pgrep -f '[^\[]main\.py", asking)


def test_the_windows_liveness_check_is_wrong_only_in_the_safe_direction():
    """Matching a command line on Windows is expensive, so it settles for "is any
    Python running" — over-broad, and never wrong in the direction that would
    report a working box as broken. A false ALIVE only costs the wait we would
    have had anyway."""
    asking = alive_command(WIN)
    assert ALIVE in asking and GONE in asking
    assert "Get-Process -Name python" in asking
    assert asking.isascii()


def test_started_is_printed_so_a_caller_could_tell_a_launch_from_a_refusal():
    for host in (WIN, LINUX):
        assert STARTED in launch_detached_command(host)


# --- --new-window ----------------------------------------------------------


def test_new_window_refuses_plainly_off_macos(monkeypatch):
    """Half-working is the one thing it may not do: a window that silently does
    not appear, on a command that starts a GPU box, is a machine you are paying
    for and cannot see."""
    monkeypatch.setattr("sys.platform", "linux")
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        in_a_new_window(["go", "comfy-win", "--follow"], say)

    assert "can only open a macOS Terminal window" in str(caught.value)
    assert "Nothing was started" in str(caught.value)
    assert "go comfy-win --follow" in caught.value.fix, "print what to paste"


def test_new_window_refuses_when_osascript_is_missing(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("shutil.which", lambda name: None)
    _, say = said()

    with pytest.raises(LifecycleError):
        in_a_new_window(["go", "comfy-win"], say)


def test_new_window_reports_a_terminal_that_would_not_open(monkeypatch):
    import subprocess

    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)

    class Refused:
        returncode = 1
        stdout = ""
        stderr = "Not authorised to send Apple events to Terminal."

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: Refused())
    _, say = said()

    with pytest.raises(LifecycleError) as caught:
        in_a_new_window(["go", "comfy-win"], say)

    assert "could not open a new Terminal window" in str(caught.value)
    assert "Not authorised" in str(caught.value)
    assert "Nothing was started" in str(caught.value)


def test_new_window_runs_the_command_it_says_it_will(monkeypatch):
    import subprocess

    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/" + name)
    seen = {}

    class Ran:
        returncode = 0
        stdout = ""
        stderr = ""

    def record(args, **kwargs):
        seen["args"] = args
        return Ran()

    monkeypatch.setattr(subprocess, "run", record)
    lines, say = said()

    in_a_new_window(["go", "comfy-linux", "--follow"], say)

    script = seen["args"][-1]
    assert script.startswith('tell application "Terminal" to do script "')
    assert "go comfy-linux --follow" in script
    assert any("opened a new Terminal window" in line for line in lines)


# --- the refusal that was a dead end ----------------------------------------
#
# `_serve` splits the same condition — ComfyUI is not answering — into a local
# branch and a remote one. The local branch hands over the command that starts
# it. The remote branch stated the fact and stopped, four lines away from a
# sibling that does not.
#
# Being the DELIBERATE refusal is the reason it needs a way out, not a reason to
# skip one. The user asked for `--no-install`, so the tool is doing exactly what
# it was told; the question left hanging is not "what went wrong" but "then
# what". Both commands are reachable from `go` and from `switch`, which are the
# two callers of this function.


def test_the_no_install_refusal_says_what_to_do_next(capsys):
    """It named the problem and offered nothing. Its sibling offers a command."""
    import typer

    from comfy_qa import host as host_module

    with pytest.raises(typer.Exit) as caught:
        host_module._serve(None, WIN, None, no_install=True)

    assert caught.value.exit_code == 1
    told = capsys.readouterr().err
    assert "--no-install was given" in told
    assert "drop --no-install" in told, (
        "the way out of a refusal the user asked for is to stop asking for it")
    assert "comfy-qat logs comfy-win" in told, (
        "and the way to find out WHY it is not answering is one command")


def test_the_local_refusal_still_hands_over_its_own_command(capsys):
    """The sibling this was measured against, so a fix cannot flatten both."""
    import typer

    from comfy_qa import host as host_module

    with pytest.raises(typer.Exit) as caught:
        host_module._serve(None, LOCAL, None, no_install=True)

    assert caught.value.exit_code == 1
    told = capsys.readouterr().err
    assert "ComfyUI is not running locally" in told
    assert "main.py" in told
