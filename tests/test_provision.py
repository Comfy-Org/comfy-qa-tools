"""The commands that run on the box.

Windows Server has cost this project real time three separate ways, and every one
of them looks like a different problem when it bites. These tests hold the shape
that avoids them.
"""

from __future__ import annotations

import inspect
import re

import pytest

from comfy_qa import provision
from comfy_qa.config import Host
from comfy_qa.provision import (
    APT_LOCK_WAIT,
    LINUX_PYTHONS,
    LINUX_ROOT,
    PYTHON_SERIES,
    PYTHON_SERIES_SUPPORTED,
    WINDOWS_PYTHONS,
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


# Every builder in the module that turns a Host into a command for the box.
#
# DERIVED, not listed. The three rules below used to be parametrised over three
# builders — `check_command`, `install_command`, `launch_command` — out of the
# thirteen that emit PowerShell, and the gap was invisible because the tests
# read as if they covered the module. Measured by mutation on 2026-09-08:
# dropping `-NonInteractive` from `alive_command`, `logs_command`,
# `cuda_command` and `firewall_command`, putting a UTF-8 em-dash into
# `repair_command`, `$ErrorActionPreference='Stop'` into `verify_command` and a
# `Split-Path` into `port_holder_command` left all 6,104 tests green — seven
# reinstatements of the three defects this file exists to prevent, none caught.
#
# A hand-written list is how that happened, so this one is not hand-written: a
# new builder is covered the moment it is added, and one that is renamed cannot
# fall out. The extra arguments are the smallest that make each call legal;
# nothing here depends on their values.
_EXTRA_ARGUMENTS = {"pid": "1234", "python": "py", "tail": 20, "follow": False,
                    # `listening_command` takes the port it is asking about,
                    # because the one caller asks about 3389 and not ComfyUI's.
                    "port": 3389}


def _builders() -> list:
    """Every function in `provision` that takes a Host and returns a command.

    Found by the `host` parameter rather than by position: `torch_install` takes
    the interpreter first, and a rule that only looked at parameter one would
    have dropped it without saying so — which is the failure this whole block
    is about.
    """
    found = []
    for name, function in sorted(vars(provision).items()):
        if not inspect.isfunction(function) or function.__module__ != provision.__name__:
            continue
        parameters = list(inspect.signature(function).parameters.values())
        if not any(p.name == "host" for p in parameters):
            continue
        if name in ("is_windows", "root_for", "log_for"):
            continue          # predicates and paths, not commands
        extra = {p.name: _EXTRA_ARGUMENTS[p.name] for p in parameters
                 if p.name != "host" and p.default is inspect.Parameter.empty}
        found.append(pytest.param(
            (lambda f, kw: lambda host: f(host=host, **kw))(function, extra), id=name))
    return found


EVERY_BUILDER = _builders()


def test_the_builder_list_is_derived_and_finds_them_all():
    """The guard on the guard. If this shrinks, the three rules below have
    quietly stopped covering the module and nothing else will say so."""
    names = {param.id for param in EVERY_BUILDER}
    assert "launch_detached_command" in names, "the detached launch is a builder too"
    assert names >= {
        "alive_command", "check_command", "cuda_command", "firewall_command",
        "install_command", "launch_command", "launch_detached_command",
        "logs_command", "port_holder_command", "repair_command", "stop_command",
        "torch_install", "verify_command",
    }, "a builder stopped being seen by the three rules below"


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


@pytest.mark.parametrize("host", ALL, ids=["windows", "linux"])
@pytest.mark.parametrize("build", EVERY_BUILDER)
def test_every_remote_command_is_ascii_only(host, build):
    """A BOM-less .ps1 is read as CP1252, so a UTF-8 dash becomes a stray quote
    and breaks the parse tens of lines later. ASCII removes the whole class.

    Every builder, because an em-dash in a progress line is the likeliest way
    back in and progress lines are exactly what the untested builders carry:
    `repair_command` says "installing torch for this GPU (the slow part)".
    """
    build(host).encode("ascii")


@pytest.mark.parametrize("host", ALL, ids=["windows", "linux"])
@pytest.mark.parametrize("build", EVERY_BUILDER)
def test_no_remote_command_turns_stderr_into_a_fatal_error(host, build):
    """`$ErrorActionPreference='Stop'` makes any native stderr terminating, so a
    successful install aborts and hides the real message."""
    assert "ErrorActionPreference" not in build(host)


@pytest.mark.parametrize("build", EVERY_BUILDER)
def test_windows_commands_never_prompt(build):
    """A prompt hangs a non-interactive SSH command forever.

    The worst failure mode this tool has. Every other defect here reports
    something wrong; this one reports nothing at all, on a machine that goes on
    billing while the terminal sits there. So it is asserted on every builder
    that emits PowerShell, not on the three somebody happened to list.

    Counted, not searched for. `"-NonInteractive" in command` was a substring
    check, and `launch_detached_command` is the only builder that emits two
    PowerShells — the outer one that runs over SSH and the inner `Start-Process`
    it launches. A substring check cannot tell two from one, so dropping the flag
    from the outer invocation, the one that actually runs over SSH, left the
    suite green. Both carry it today; this is the guard being able to see it if
    one stops.

    Which is the shape this file keeps producing. `7aa7af0` widened these rules
    from three builders to all thirteen and closed the gap *between* builders,
    and this one was still open *inside* one of them. `verify_command` searched
    a subset of the interpreters `launch_command` can start, held to the same
    docstring. Three instances of a check that reads as if it covers the thing
    and covers a subset of it.
    """
    command = build(WIN)
    if "powershell" not in command:
        pytest.skip("this builder emits no PowerShell on Windows")
    assert command.count("-NonInteractive") >= command.count("powershell"), (
        "a PowerShell invocation here does not carry -NonInteractive; if it is "
        "the one that runs over SSH, a prompt hangs it forever")


@pytest.mark.parametrize("build", EVERY_BUILDER)
def test_no_windows_command_uses_split_path(build):
    """`Split-Path` with an empty value prompts for a mandatory parameter, and a
    prompt hangs a non-interactive SSH command forever — the one failure that
    looks like nothing at all. It was asserted on `launch_detached_command`
    alone, which is one of thirteen."""
    assert "Split-Path" not in build(WIN)


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


# --- the interpreter search: one list, asked the same way by everyone ---------
#
# `verify_command` used to carry its own two-layout chain and `repair_command` a
# third copy of it, while `launch_command` searched four. On a sandbox whose
# only interpreter was `.venv/bin/python` — a layout the launch explicitly
# supports — with a torch reporting `2.5.1+cu121`:
#
#     verify says:      NO_TORCH
#     launch would use: <root>/.venv/bin/python
#
# The tool condemned a box it could start, and sent the tester to reinstall a
# working install while the machine went on billing. `hostfile` had the same
# three-copies shape and it cost six defects before the regexes became
# module-level constants.


def _rooted(host) -> list:
    """The interpreter layouts as the box would see them, in search order.

    Rooted, not bare: `venv\\Scripts\\python.exe` is a substring of
    `.venv\\Scripts\\python.exe`, and `python_embeded\\python.exe` of the
    portable bundle's copy, so a bare search finds layouts that are not there
    and would have called the truncated chain complete.
    """
    layouts = WINDOWS_PYTHONS if is_windows(host) else LINUX_PYTHONS
    separator = "\\" if is_windows(host) else "/"
    return [f"{root_for(host)}{separator}{name}" for name in layouts]


def _searchers(host) -> dict:
    """Every builder that chooses between interpreters, and where it names each.

    DERIVED, not listed — the same reason the block at the top of this file
    derives its builders. Naming one interpreter is a location: `check_command`
    probes the venv it expects and `install_command` builds it. Naming two or
    more is a *search*, and a search has to be the whole list. So the consumer
    set is read off the commands themselves: a fourth one is covered the day it
    is written, and one that is renamed cannot fall out.
    """
    found = {}
    for parameter in EVERY_BUILDER:
        command = parameter.values[0](host)
        at = [command.find(path) for path in _rooted(host)]
        if sum(1 for position in at if position >= 0) >= 2:
            found[parameter.id] = at
    return found


@pytest.mark.parametrize("host", ALL, ids=["windows", "linux"])
def test_every_interpreter_search_asks_the_whole_list_in_order(host):
    """One list, and a search order is part of what the list means.

    Membership *and* order: the venv this tool builds itself has to win over a
    stray one, so a chain that holds the right four layouts in the wrong order
    is still wrong. A fifth layout added to `WINDOWS_PYTHONS` alone fails here
    for every consumer that did not get it.
    """
    layouts = _rooted(host)
    searchers = _searchers(host)
    assert searchers, "no builder searches for an interpreter at all"
    for name, at in sorted(searchers.items()):
        missing = [path for path, position in zip(layouts, at) if position < 0]
        assert not missing, (
            f"{name} searches for an interpreter but never looks in {missing} — "
            "that is the box it condemns and could have started")
        assert at == sorted(at), (
            f"{name} searches the layouts in a different order from the list")


@pytest.mark.parametrize("host", ALL, ids=["windows", "linux"])
def test_no_builder_rolls_its_own_interpreter_chain(host):
    """The list and the search that reads it are the same source of truth.

    Behaviour and source have to agree: a builder that searches must be the one
    that calls the shared search, and a builder that calls it must search. The
    correspondence is what stops the next copy — writing the chain out by hand
    fails the first half, and slicing the constant fails the second.
    """
    helper = "windows_python_search" if is_windows(host) else "linux_python_search"
    uses_helper = {
        parameter.id for parameter in EVERY_BUILDER
        if helper in inspect.getsource(getattr(provision, parameter.id))
    }
    assert uses_helper == set(_searchers(host)), (
        "these disagree about who searches for an interpreter: "
        f"calls {helper}={sorted(uses_helper)}, "
        f"actually searches={sorted(_searchers(host))}")


@pytest.mark.parametrize("host", ALL, ids=["windows", "linux"])
def test_the_layout_the_installer_builds_is_the_one_searched_first(host):
    """Search order is a fact about the box, not an arrangement of a tuple.

    The tests above hold every consumer to the same order, which is what stops
    them diverging — but they pass just as happily on a list turned upside down,
    because a consistent wrong order is still consistent. This is the anchor:
    `install_command` builds one venv, and a search that reached a stray `.venv`
    or the portable bundle before it would run ComfyUI out of an environment
    nothing here installed into.
    """
    layouts = WINDOWS_PYTHONS if is_windows(host) else LINUX_PYTHONS
    built = install_command(host)
    assert layouts[0] in built, (
        f"the installer builds none of {layouts[0]!r}, so nothing says which "
        "layout the search should prefer")
    assert not any(other in built for other in layouts[1:]), (
        "the installer builds more than one layout; which one wins is no longer "
        "decided by this list")

def test_a_repair_checks_there_is_something_to_repair_on_both_boxes():
    """The guard `install_command` has had all along, which this did not.

    `install_command`'s comment names the hazard exactly — "Without this the next
    line fails quietly and everything after it installs into whatever directory
    PowerShell happened to be in" — and it bites harder here. Rule 1 keeps
    PowerShell at `Continue`, where a failed `Set-Location` is NON-TERMINATING,
    so the script carried on and spent several minutes putting 2.5 GB of CUDA
    torch, and then a requirements file, into whatever directory the session
    started in.

    Linux guards its own `cd` and says why; Windows did not. Parallel code, one
    side guarded — the shape this project keeps finding — so both sides are
    asserted here rather than only the one that was broken, and both answer a
    missing checkout with the same sentence rather than one getting a shell error
    and the other a message.
    """
    from comfy_qa.provision import NOTHING_TO_REPAIR, repair_command

    for host in ALL:
        command = repair_command(host)
        assert NOTHING_TO_REPAIR in command, f"{host.os}: no guard"
        assert command.index(NOTHING_TO_REPAIR) < command.index("pip install"), (
            f"{host.os}: the guard runs after the install it exists to prevent")
        assert "exit 1" in command, f"{host.os}: it must fail, not just say so"


def test_the_repair_guard_names_the_root_before_the_install_directory_is_used():
    """Windows specifically: the `Set-Location` is what the guard protects, so
    the check has to precede it and not merely precede pip."""
    from comfy_qa.provision import NOTHING_TO_REPAIR, repair_command

    windows = repair_command(WIN)
    assert windows.index(NOTHING_TO_REPAIR) < windows.index("Set-Location")


def _powershell_invocations(command: str) -> list[str]:
    """One segment per `powershell` token in a command.

    `launch_detached_command` emits two — the outer one that runs over SSH and
    the inner `Start-Process` it launches — so any rule about *an invocation*
    has to be asserted once per segment. Asserted on the whole string instead,
    it is a first-occurrence check: it sees the outer pair and says nothing at
    all about the inner one.
    """
    starts = [match.start() for match in re.finditer("powershell", command)]
    edges = starts + [len(command)]
    return [command[edges[i]:edges[i + 1]] for i in range(len(starts))]


def _flag_position(text: str, flag: str) -> int | None:
    """Where a PowerShell flag really appears, ignoring words that merely end in
    it. `Get-Command` contains `-Command`, and every builder that searches for an
    interpreter carries one — counting those inflates the flag by one per
    builder and puts the comparison below on the wrong token.
    """
    match = re.search(rf"(?<!\w){re.escape(flag)}", text)
    return None if match is None else match.start()


@pytest.mark.parametrize("build", EVERY_BUILDER)
def test_no_windows_command_runs_the_boxs_powershell_profile(build):
    """`-NonInteractive` is not the whole guard against a prompt.

    A machine-wide or per-user PowerShell profile runs BEFORE `-NonInteractive`
    takes effect, so a box whose profile asks anything hangs a command carrying
    every other protection in this file — the one failure that reports nothing
    at all while the machine goes on billing. `-NoProfile` also makes the
    invocation identical on every box, which is the point of a QA tool.

    Counted and paired, not searched for, for the same reason
    `test_windows_commands_never_prompt` above is. This rule shipped as
    `"-NoProfile" in command` plus one `.index("-NoProfile") < .index("-Command")`
    — both first-occurrence checks — hours after the docstring above was written
    about exactly that, one test away, in this file. The fix had copied the
    assertion's shape rather than its lesson. Dropping `-NoProfile` from the
    inner `Start-Process` alone left `powershell: 2, -NoProfile: 1` and the
    whole suite green, with the box's profile running on the detached launch:
    the long-running invocation, on the machine that is billing.
    """
    command = build(WIN)
    if "powershell" not in command:
        pytest.skip("this builder emits no PowerShell on Windows")
    assert command.count("-NoProfile") >= command.count("powershell"), (
        "a PowerShell invocation here does not carry -NoProfile; the box's "
        "profile runs before -NonInteractive can stop it prompting")
    invocations = _powershell_invocations(command)
    for number, invocation in enumerate(invocations, start=1):
        profile = _flag_position(invocation, "-NoProfile")
        runs = _flag_position(invocation, "-Command")
        assert profile is not None, (
            f"PowerShell invocation {number} of {len(invocations)} carries no "
            "-NoProfile")
        assert runs is not None, (
            f"PowerShell invocation {number} of {len(invocations)} runs no "
            "-Command")
        assert profile < runs, (
            f"PowerShell invocation {number} of {len(invocations)}: -NoProfile "
            "has to come before the command it is protecting")
