"""A recorded pid is not a tunnel. These tests are the difference.

Everything here is one question asked in different ways: when the tool says
"tunnel open on http://127.0.0.1:8190", is that true, and is it true about the
machine you asked for? A process id on its own cannot answer that. Numbers are
recycled, ports are held by other people's processes, two terminals race, and a
second host list can name a different box `comfy-win` too.

The failure each one prevents is the same failure: reading a result off a machine
you did not mean to be on.
"""

from __future__ import annotations

import os
import socket

import pytest

from comfy_qa.config import Host
from comfy_qa.tunnel import (
    TunnelError,
    close_tunnel,
    command,
    open_tunnel,
    pid_file,
    record_file,
    status,
)

WIN = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022", gpu="L4",
           gce_instance="comfy-win", gce_zone="us-central1-a", gce_project="proj")

# Same name, different box: what a second host list, or an edited one, produces.
IMPOSTOR = Host(name="comfy-win", kind="gce", port=8195, os="Ubuntu 22.04", gpu="L4",
                gce_instance="comfy-win-2", gce_zone="us-west1-b", gce_project="proj")

LOCAL = Host(name="local", kind="local", port=8188)

ME = os.getpid()


def started(token):
    """A stand-in for 'when did this process start' — the thing a pid alone lacks."""
    return lambda pid: token


def launched_by(record):
    def launcher(cmd, log):
        record.append(cmd)
        return ME
    return launcher


# --- a pid is not an identity -------------------------------------------------


def test_a_recycled_pid_is_not_mistaken_for_a_live_tunnel(tmp_path):
    """The tunnel exits; the OS hands its number to something unrelated.

    `os.kill(pid, 0)` then says yes, and the tool reports a tunnel that is not
    there — on a port that may well be answering for a different machine.
    """
    open_tunnel(WIN, tmp_path, launcher=launched_by([]), identify=started("boot-A"))

    state = status("comfy-win", tmp_path, identify=started("boot-B"))

    assert state.running is False, "a different process wearing the same pid is not our tunnel"


def test_closing_a_recycled_pid_does_not_signal_a_stranger(tmp_path):
    """`down` on a stale record would SIGTERM whatever now owns that number."""
    open_tunnel(WIN, tmp_path, launcher=launched_by([]), identify=started("boot-A"))

    signalled = []
    stopped = close_tunnel("comfy-win", tmp_path,
                           killer=lambda pid, sig: signalled.append(pid),
                           identify=started("boot-B"))

    assert signalled == [], "signalled a process that is not ours"
    assert stopped is False


def test_a_live_tunnel_is_still_recognised_across_calls(tmp_path):
    """The identity check must not break the case it exists to protect."""
    open_tunnel(WIN, tmp_path, launcher=launched_by([]), identify=started("boot-A"))
    assert status("comfy-win", tmp_path, identify=started("boot-A")).running is True


@pytest.mark.parametrize("bad", ["-1", "0", "-99999"])
def test_a_pid_file_that_is_not_a_real_pid_never_signals_anything(tmp_path, bad):
    """`kill(-1, SIGTERM)` is every process you own; `kill(0, …)` is your group.

    A truncated or hand-edited pid file is all it takes, and `os.kill` treats both
    numbers as commands rather than as targets.
    """
    path = pid_file("comfy-win", tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(bad)

    assert status("comfy-win", tmp_path).running is False

    signalled = []
    close_tunnel("comfy-win", tmp_path, killer=lambda pid, sig: signalled.append(pid))
    assert signalled == [], f"a pid file holding {bad!r} was used as a signal target"


def test_a_pid_file_the_record_disagrees_with_is_not_trusted(tmp_path):
    """Half-written, hand-edited, or two tools sharing one directory."""
    open_tunnel(WIN, tmp_path, launcher=launched_by([]), identify=started("boot-A"))
    pid_file("comfy-win", tmp_path).write_text(str(ME + 1))

    assert status("comfy-win", tmp_path, identify=started("boot-A")).running is False


# --- the port is the thing that answers, not the pid --------------------------


def test_a_port_someone_else_is_holding_is_refused_not_tunnelled(tmp_path):
    """The wrong-machine failure in its purest form.

    Something is already listening on 8190 — a tunnel to another box, a dev
    server, a previous run that never died. gcloud cannot bind, so the tunnel it
    starts forwards nothing, and the URL handed back answers with whatever is
    already there.
    """
    launches = []
    with pytest.raises(TunnelError) as caught:
        open_tunnel(WIN, tmp_path, launcher=launched_by(launches),
                    identify=started("boot-A"), port_busy=lambda port: True)

    assert "8190" in str(caught.value)
    assert launches == [], "started a tunnel onto a port that was already taken"
    assert not pid_file("comfy-win", tmp_path).exists()


def test_the_port_check_is_real_and_not_a_guess():
    """The seam above is only worth having if the real probe is right."""
    from comfy_qa.tunnel import _port_busy

    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        taken = held.getsockname()[1]
        assert _port_busy(taken) is True

    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        spare = free.getsockname()[1]
    assert _port_busy(spare) is False


def test_our_own_open_tunnel_does_not_look_like_a_port_conflict(tmp_path):
    """A tunnel we already hold is a reuse, not a clash."""
    open_tunnel(WIN, tmp_path, launcher=launched_by([]), identify=started("boot-A"))

    again = open_tunnel(WIN, tmp_path, launcher=launched_by([]),
                        identify=started("boot-A"), port_busy=lambda port: True)
    assert again.running is True


# --- the record has to name the machine, not just the tunnel ------------------


def test_a_tunnel_recorded_for_another_machine_is_never_reused(tmp_path):
    """Two host lists, one name.

    `--config other.toml` can declare a `comfy-win` that is a different instance
    in a different zone. The pid file is keyed by name alone, so the second one
    reads the first one's tunnel as its own and hands over a URL that reaches the
    first box.
    """
    open_tunnel(WIN, tmp_path, launcher=launched_by([]), identify=started("boot-A"))

    with pytest.raises(TunnelError) as caught:
        open_tunnel(IMPOSTOR, tmp_path, launcher=launched_by([]),
                    identify=started("boot-A"))

    message = str(caught.value)
    assert "comfy-win-2" in message or "8195" in message


def test_the_state_says_which_machine_the_tunnel_actually_goes_to(tmp_path):
    """So a caller can print the truth instead of repeating what it asked for."""
    open_tunnel(WIN, tmp_path, launcher=launched_by([]), identify=started("boot-A"))

    state = status("comfy-win", tmp_path, identify=started("boot-A"))
    assert state.port == 8190
    assert state.instance == "comfy-win"
    assert state.url == "http://127.0.0.1:8190"


# --- names arrive from a file a person edits ----------------------------------


def test_a_host_name_cannot_write_outside_the_tunnel_directory(tmp_path):
    """Host names are TOML table keys: whatever someone typed, including `../`."""
    evil = "../../../../../../tmp/comfy-qat-escaped"
    path = pid_file(evil, tmp_path)

    assert tmp_path in path.parents, f"{path} is outside {tmp_path}"


def test_a_host_name_with_a_slash_still_opens_and_closes(tmp_path):
    weird = Host(name="team/comfy-win", kind="gce", port=8196, os="Ubuntu 22.04",
                 gpu="L4", gce_instance="i", gce_zone="z", gce_project="p")
    state = open_tunnel(weird, tmp_path, launcher=launched_by([]),
                        identify=started("boot-A"))
    assert state.running is True
    assert close_tunnel("team/comfy-win", tmp_path, killer=lambda pid, sig: None,
                        identify=started("boot-A")) is True


def test_two_host_names_that_sanitise_alike_keep_separate_records(tmp_path):
    """`a/b` and `a_b` must not share one pid file and stop each other."""
    one = Host(name="a/b", kind="gce", port=8196, os="o", gpu="g",
               gce_instance="i1", gce_zone="z", gce_project="p")
    two = Host(name="a_b", kind="gce", port=8197, os="o", gpu="g",
               gce_instance="i2", gce_zone="z", gce_project="p")

    open_tunnel(one, tmp_path, launcher=launched_by([]), identify=started("boot-A"))
    open_tunnel(two, tmp_path, launcher=launched_by([]), identify=started("boot-A"))

    assert pid_file("a/b", tmp_path) != pid_file("a_b", tmp_path)


def test_an_absurdly_long_host_name_is_not_a_crash(tmp_path):
    """255 bytes is the filename limit; a long name would raise ENAMETOOLONG."""
    long = Host(name="x" * 400, kind="gce", port=8198, os="o", gpu="g",
                gce_instance="i", gce_zone="z", gce_project="p")
    state = open_tunnel(long, tmp_path, launcher=launched_by([]),
                        identify=started("boot-A"))
    assert state.running is True


def test_a_local_host_is_never_tunnelled(tmp_path):
    """`--zone=None --project=None` is not a command; it is a bug report."""
    with pytest.raises(TunnelError):
        command(LOCAL)
    with pytest.raises(TunnelError):
        open_tunnel(LOCAL, tmp_path, launcher=launched_by([]))


# --- more than one comfy-qat is running. always assume so ---------------------


def test_the_suite_never_starts_a_real_tunnel(tmp_path, never_start_a_real_tunnel):
    """`bring_up` opens a tunnel, and the lifecycle tests call it for real.

    Every `pytest tests/` used to spawn detached `gcloud compute start-iap-tunnel`
    processes against whichever project gcloud was signed in to, and leave them
    running — eleven were alive at once on the machine this was written on. The
    guard lives in conftest.py; this is what notices if it stops applying.
    """
    open_tunnel(WIN, tmp_path, port_busy=lambda port: False)

    assert len(never_start_a_real_tunnel) == 1
    assert never_start_a_real_tunnel[0][:3] == ["gcloud", "compute", "start-iap-tunnel"]


def test_two_processes_cannot_both_launch_a_tunnel_to_one_host(tmp_path):
    """Nothing is written between the check and the launch, so both launch.

    Two tunnels on one port: one binds, one does not, and the pid file names
    whichever wrote last. `down` then stops the wrong one and leaves a tunnel
    forwarding with no record that it exists.
    """
    launches = []
    second = {}

    def launcher(cmd, log):
        launches.append(cmd)
        if len(launches) == 1:
            # A second terminal arrives while this one is still starting gcloud.
            try:
                open_tunnel(WIN, tmp_path, launcher=launcher, identify=started("boot-A"))
                second["outcome"] = "launched"
            except TunnelError as exc:
                second["outcome"] = str(exc)
        return ME

    open_tunnel(WIN, tmp_path, launcher=launcher, identify=started("boot-A"))

    assert len(launches) == 1, "two gcloud tunnels were started for one host"
    assert second["outcome"] != "launched"


def test_close_does_not_delete_a_tunnel_that_was_just_reopened(tmp_path):
    """`down` racing `open`: the record of the new tunnel must survive."""
    open_tunnel(WIN, tmp_path, launcher=launched_by([]), identify=started("boot-A"))

    def killer(pid, sig):
        # The old tunnel dies and another terminal opens a fresh one.
        pid_file("comfy-win", tmp_path).write_text(str(ME + 7))

    close_tunnel("comfy-win", tmp_path, killer=killer, identify=started("boot-A"))

    assert pid_file("comfy-win", tmp_path).read_text().strip() == str(ME + 7), (
        "cleared a record written after the one that was read"
    )


def test_a_directory_it_cannot_write_to_is_a_message_not_a_traceback(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir(mode=0o500)
    try:
        with pytest.raises(TunnelError):
            open_tunnel(WIN, locked / "tunnels", launcher=launched_by([]),
                        identify=started("boot-A"))
    finally:
        os.chmod(locked, 0o700)


def test_gcloud_missing_is_a_message_not_a_traceback(tmp_path):
    def missing(cmd, log):
        raise FileNotFoundError("gcloud")

    with pytest.raises(TunnelError) as caught:
        open_tunnel(WIN, tmp_path, launcher=missing, identify=started("boot-A"))
    assert "gcloud" in str(caught.value)


def test_closing_removes_both_the_pid_and_the_record(tmp_path):
    """A leftover record file would be read as a tunnel by the next command."""
    open_tunnel(WIN, tmp_path, launcher=launched_by([]), identify=started("boot-A"))
    close_tunnel("comfy-win", tmp_path, killer=lambda pid, sig: None,
                 identify=started("boot-A"))

    assert not pid_file("comfy-win", tmp_path).exists()
    assert not record_file("comfy-win", tmp_path).exists()
