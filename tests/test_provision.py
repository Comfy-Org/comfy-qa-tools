"""The commands that run on the box.

Windows Server has cost this project real time three separate ways, and every one
of them looks like a different problem when it bites. These tests hold the shape
that avoids them.
"""

from __future__ import annotations

import pytest

from comfy_qa.config import Host
from comfy_qa.provision import (
    APT_LOCK_WAIT,
    LINUX_ROOT,
    PYTHON_SERIES,
    PYTHON_SERIES_SUPPORTED,
    WINDOWS_ROOT,
    check_command,
    install_command,
    is_windows,
    launch_command,
    logs_command,
    root_for,
)

WIN = Host(name="w", kind="gce", port=8190, os="Windows Server 2022", gpu="L4",
           gce_instance="w", gce_zone="z", gce_project="p")
LINUX = Host(name="l", kind="gce", port=8191, os="Ubuntu 22.04", gpu="L4",
             gce_instance="l", gce_zone="z", gce_project="p")

ALL = [WIN, LINUX]


def test_the_os_decides_the_script():
    """`root_for` dispatches on the platform, which is all this proves.

    Worth saying, because the two lines below sit above the real pins and read
    as if they covered the values: each compares a function against the constant
    it returns, so they hold for whatever the roots are set to. A reader
    scanning for "is this covered" sees the constant's name inside an assertion
    and stops looking — which is why `LINUX_ROOT` went unpinned for as long as
    it did while `WINDOWS_ROOT`, one line from it, did not.

    They are kept because swapping the two returns really would fail here, and
    that is a different question from what the values are. The values are held
    to literals in `test_windows_installs_into_the_conventional_place` and
    `test_linux_installs_into_the_conventional_place`.
    """
    assert is_windows(WIN) and not is_windows(LINUX)
    assert root_for(WIN) == WINDOWS_ROOT
    assert root_for(LINUX) == LINUX_ROOT


@pytest.mark.parametrize("host", ALL)
@pytest.mark.parametrize("build", [check_command, install_command, launch_command])
def test_every_remote_command_is_ascii_only(host, build):
    """A BOM-less .ps1 is read as CP1252, so a UTF-8 dash becomes a stray quote
    and breaks the parse tens of lines later. ASCII removes the whole class."""
    build(host).encode("ascii")


@pytest.mark.parametrize("host", ALL)
@pytest.mark.parametrize("build", [check_command, install_command, launch_command])
def test_no_remote_command_turns_stderr_into_a_fatal_error(host, build):
    """`$ErrorActionPreference='Stop'` makes any native stderr terminating, so a
    successful install aborts and hides the real message."""
    assert "ErrorActionPreference" not in build(host)


@pytest.mark.parametrize("build", [check_command, install_command, launch_command])
def test_windows_commands_never_prompt(build):
    """A prompt hangs a non-interactive SSH command forever."""
    assert "-NonInteractive" in build(WIN)


def test_the_check_answers_in_one_word():
    for host in ALL:
        command = check_command(host)
        assert "INSTALLED" in command and "MISSING" in command


def test_the_interpreter_is_pinned_below_3_13():
    """Custom nodes still lack wheels for 3.13+."""
    assert PYTHON_SERIES == "3.12"
    assert PYTHON_SERIES in install_command(WIN)
    assert PYTHON_SERIES in install_command(LINUX)
    assert all(series < "3.13" for series in PYTHON_SERIES_SUPPORTED)
    assert PYTHON_SERIES_SUPPORTED[0] == PYTHON_SERIES, "newest first"


def test_linux_installs_its_prerequisites_rather_than_hoping():
    """The first real Linux box died here, and both halves were assumptions.

    `python3.12` was assumed present: Ubuntu 22.04 ships 3.10 and its archive
    has no 3.12 at all. Then the fallback to `python3` found an interpreter
    whose venv module Ubuntu strips into a separate package, so `-m venv`
    produced a directory with no pip in it. Windows never had this problem
    because its branch installs git and python before using them; this one now
    does the same.
    """
    command = install_command(LINUX)
    assert "apt-get" in command
    assert "install git" in command, "git is not on a minimal cloud image either"
    assert "-venv" in command, "a present interpreter is not a usable one on Ubuntu"


def test_linux_asks_the_box_which_python_it_has():
    """Discovered newest-first, never hardcoded to one series."""
    command = install_command(LINUX)
    assert 'command -v "python$v"' in command, "asked, not assumed"
    assert " ".join(PYTHON_SERIES_SUPPORTED) in command, "and tried newest first"
    assert f"python{PYTHON_SERIES} -m venv" not in command, "that is the assumption"
    assert '"$PY" -m venv --clear venv' in command


def test_linux_checks_the_venv_works_before_installing_into_it():
    """Existing is not the test — a venv built before python3.10-venv was there
    has no pip in it, and every later line fails on `No module named pip`."""
    command = install_command(LINUX)
    guard = command.index("INSTALL_INCOMPLETE: the venv has no pip")
    assert guard < command.index("pip install --upgrade pip")


def test_a_venv_that_cannot_run_pip_is_rebuilt_rather_than_installed_into():
    """`python -m venv` over a broken venv leaves it broken; --clear empties it."""
    command = install_command(LINUX)
    assert "-m venv --clear venv" in command
    assert "reusing the venv" in command, "a working one is not thrown away"


def test_an_install_is_not_believed_on_the_strength_of_one_file():
    """main.py alone reported INSTALLED on a box whose venv had no pip. The next
    run skipped the install and died several steps later on a missing module."""
    for host in ALL:
        command = check_command(host)
        assert "pip" in command, "a venv that cannot install into itself is not one"
        assert command.count("MISSING") >= 2, "both ways of not being installed"


def test_every_apt_call_waits_for_the_dpkg_lock():
    """cloud-init and unattended-upgrades hold it for the first minutes of a
    box's life, and apt's default is to fail immediately rather than wait."""
    command = install_command(LINUX)
    assert command.count("apt-get") == command.count(
        f"apt-get -o DPkg::Lock::Timeout={APT_LOCK_WAIT}"
    )


def test_every_apt_call_is_non_interactive():
    """Otherwise apt opens the install with three lines that read as a crash.

    `debconf: unable to initialize frontend: Dialog`, the parenthetical about a
    non-interactive terminal, and `falling back to frontend: Readline` — four
    lines into an install whose next stretch is a silent torch download. That
    pairing is where `go` gets interrupted: nothing has failed, and the only
    thing on screen says it has.

    `sudo env VAR=…`, not `sudo VAR=…`: command-line environment assignments go
    through sudoers and a hardened one refuses them, which would turn a cosmetic
    fix into a failed install.
    """
    command = install_command(LINUX)

    assert command.count("apt-get") == command.count(
        "sudo env DEBIAN_FRONTEND=noninteractive apt-get")
    assert "sudo DEBIAN_FRONTEND=" not in command


@pytest.mark.parametrize("host", ALL)
def test_every_box_binds_loopback(host):
    """This assertion has now been written three ways, and the reason is that it
    depends entirely on how the tunnel forwards — which is the thing that was
    wrong, not the binding.

    With `start-iap-tunnel` the far end is a port on the instance's network
    interface, so loopback is unreachable and 0.0.0.0 plus two firewall rules is
    the only way. With `ssh -L` the far address is resolved on the box, so
    loopback is exactly right: nothing is exposed on any interface, no firewall
    rule exists to get wrong, and the URL ComfyUI prints is true where it is
    printed. Proven against a real box: loopback bind, ssh forward, HTTP 200.
    """
    command = launch_command(host)
    assert "--listen 127.0.0.1" in command
    assert "0.0.0.0" not in command
    assert "--port 8188" in command


def test_a_local_box_binds_loopback_too():
    """No tunnel involved at all here, and the same answer."""
    local = Host(name="local", kind="local", port=8188)
    assert "--listen 127.0.0.1" in launch_command(local)
    assert "0.0.0.0" not in launch_command(local)


@pytest.mark.parametrize("host", ALL)
def test_install_says_what_it_is_doing(host):
    """A silent ten-minute torch install is indistinguishable from a hang."""
    assert "slow part" in install_command(host)


def test_windows_installs_into_the_conventional_place():
    assert WINDOWS_ROOT in install_command(WIN)
    assert WINDOWS_ROOT == r"C:\ComfyUI"


def test_linux_installs_into_the_conventional_place():
    """The pin its neighbour has had all along.

    `WINDOWS_ROOT` is held to a literal one line above and `LINUX_ROOT` was held
    to nothing, so pointing it at `/nonsense` changed every Linux install
    command in the tool and left the suite green. Nobody decided that: the pin
    was written for one of a pair.
    """
    assert LINUX_ROOT in install_command(LINUX)
    assert LINUX_ROOT == "/opt/comfyui"


def test_the_exit_codes_the_remote_scripts_return_are_held_to_their_values():
    """These two are a contract with a shell, and a test cannot use the constant
    as its own oracle.

    `provision` writes the number into a script that runs on the box and
    `lifecycle` compares the code that comes back against the same Python name,
    so both sides move together and nothing here notices. `assert f"exit
    {NO_LOG_EXIT}" in logs_command(...)` is true for every value it could ever
    have — the literal is the only independent oracle there is.

    The values are not arbitrary either: they have to stay clear of what a shell
    returns on its own (126 and 127 for "cannot execute" and "not found", 128+n
    for a signal) and low enough to be obviously ours.
    """
    from comfy_qa.provision import NO_LOG_EXIT, NO_PYTHON_EXIT

    assert NO_PYTHON_EXIT == 3, "nothing on the box can run ComfyUI"
    assert NO_LOG_EXIT == 4, "the box has no ComfyUI log to read"

    # And that each really is what the script returns, which is the half the
    # literal cannot check on its own.
    assert "exit 3" in launch_command(LINUX)
    assert "exit 4" in logs_command(LINUX, tail=20, follow=False)


@pytest.mark.parametrize("host", ALL)
def test_the_interpreter_is_found_not_assumed(host):
    """A live box reported "'.\\venv\\Scripts\\python.exe' is not recognized" —
    ComfyUI was installed, just not with a venv. An install may carry a venv, the
    Windows portable bundle's embedded Python, or neither.
    """
    command = launch_command(host)
    assert "NO_PYTHON" in command, "it must say so rather than fail obscurely"
    if is_windows(host):
        assert "python_embeded" in command, "the portable bundle's layout"
        assert "Test-Path" in command
    else:
        assert "command -v python3" in command


@pytest.mark.parametrize("host", ALL)
def test_the_launch_reports_which_interpreter_it_chose(host):
    """Otherwise a wrong-Python failure looks like a ComfyUI failure."""
    assert "using" in launch_command(host)


@pytest.mark.parametrize("host", ALL)
def test_an_install_checks_its_own_work_before_claiming_success(host):
    """The scripts cannot use `-ErrorActionPreference Stop` — it turns any native
    stderr into a fatal error and hides the real message — so PowerShell carries
    on after a failed step. A clone that failed still reached "install complete"
    and exit 0, and the next thing anyone saw was a launch failure on a box that
    had been reported as installed."""
    command = install_command(host)
    assert "INSTALL_INCOMPLETE" in command
    if is_windows(host):
        # Checked twice: once before anything is installed into the directory
        # that was supposed to be cloned, once at the end.
        assert command.count("INSTALL_INCOMPLETE") == 2
        assert command.count("exit 1") == 2
    else:
        assert "exit 1" in command


def test_the_launch_and_the_tunnel_agree_on_the_port():
    """The tunnel forwards to ComfyUI's own port on the box. If the launch bound
    somewhere else, the tunnel would open onto silence and everything downstream
    would blame ComfyUI."""
    from comfy_qa.tunnel import COMFYUI_PORT

    for host in ALL:
        assert f"--port {COMFYUI_PORT}" in launch_command(host)


def test_the_no_python_exit_code_is_the_one_the_caller_looks_for():
    """`go` turns this exact code into "there is no Python on that box". If the
    two drifted, it would become a generic non-zero exit."""
    from comfy_qa.provision import NO_PYTHON_EXIT

    for host in ALL:
        assert f"exit {NO_PYTHON_EXIT}" in launch_command(host)


def test_the_repair_installs_torch_where_it_can_see_the_gpu():
    """From a real L4 box on 2026-08-27.

    The first repair ran `pip install -r requirements.txt` alone. requirements
    says plain `torch`, PyPI's Windows wheel is CPU-only, and ComfyUI then died
    with "Torch not compiled with CUDA enabled" — on the box whose entire reason
    for existing is the card. A repair that turns a GPU box into a CPU box is
    worse than the failure it was fixing.
    """
    from comfy_qa.provision import repair_command

    windows = repair_command(Host(name="w", kind="gce", os="Windows Server 2022",
                                  gpu="L4", port=8190))
    assert "download.pytorch.org/whl/cu128" in windows
    assert windows.index("torch torchvision torchaudio") < windows.index("requirements.txt"), (
        "torch first: installing requirements first pulls the CPU wheel and "
        "the CUDA one then looks already satisfied"
    )


def test_linux_needs_no_index_because_its_pypi_wheel_carries_cuda():
    """The asymmetry is easy to get wrong in both directions."""
    from comfy_qa.provision import repair_command

    linux = repair_command(Host(name="l", kind="gce", os="Ubuntu 22.04",
                                gpu="L4", port=8191))
    assert "download.pytorch.org" not in linux
    assert "pip install torch torchvision torchaudio" in linux


def test_the_installer_and_the_repair_agree_about_torch():
    """They drifted once. The repair was written later and did not know."""
    from comfy_qa.provision import install_command, repair_command

    for os_name in ("Windows Server 2022", "Ubuntu 22.04"):
        host = Host(name="h", kind="gce", os=os_name, gpu="L4", port=8190)
        installs = "download.pytorch.org/whl/cu128" in install_command(host)
        repairs = "download.pytorch.org/whl/cu128" in repair_command(host)
        assert installs == repairs, f"{os_name}: one uses the CUDA index and the other does not"


def test_replacing_a_wrong_torch_has_to_force_the_reinstall():
    """Watched pip refuse to do this on a real box.

    Pointing pip at the CUDA index is not enough when a same-version CPU wheel
    is already installed: pip matches on version, not on which index a wheel
    came from, so it printed `Requirement already satisfied: torch (2.13.0)`
    and the box stayed CPU-only. The detection was right and the repair was a
    no-op, which is the worst combination — it looks handled.
    """
    from comfy_qa.provision import repair_command

    host = Host(name="w", kind="gce", os="Windows Server 2022", gpu="L4", port=8190)
    forced = repair_command(host, force_torch=True)

    assert "--force-reinstall" in forced
    assert "--no-deps" in forced, (
        "the dependencies are already right, and PyTorch's index does not carry "
        "all of them"
    )
    assert "download.pytorch.org/whl/cu128" in forced


def test_a_plain_repair_does_not_force_anything():
    """A missing module is not a reason to redownload 2.5 GB of torch."""
    from comfy_qa.provision import repair_command

    host = Host(name="w", kind="gce", os="Windows Server 2022", gpu="L4", port=8190)
    assert "--force-reinstall" not in repair_command(host)


@pytest.mark.parametrize("reported,expected", [
    ("CUDA Version: 13.0", "cu130"),
    ("CUDA Version: 12.8", "cu128"),
    ("CUDA Version: 12.4", "cu124"),
    ("CUDA Version: 11.8", "cu118"),
    ("NO_NVIDIA", "cu128"),
    ("", "cu128"),
    (None, "cu128"),
])
def test_the_torch_index_follows_the_driver_on_the_box(reported, expected):
    """The pinned index was a number that went stale on a real machine.

    An L4 running a newer driver was given cu128 and ComfyUI answered "You need
    pytorch with cu130 or higher to use optimized CUDA operations" — installed,
    working, and quietly slower than the card allows, on a box whose entire job
    is measuring how fast things are.

    Unreadable answers fall back rather than guessing high: an index the driver
    cannot run fails the install outright, where an older one only costs speed.
    """
    from comfy_qa.provision import torch_index_for

    assert torch_index_for(reported).endswith(expected)


def test_a_newer_driver_than_we_know_about_gets_the_newest_we_have():
    """CUDA is backwards compatible, so the newest index we know is right."""
    from comfy_qa.provision import torch_index_for

    assert torch_index_for("CUDA Version: 14.2").endswith("cu130")
