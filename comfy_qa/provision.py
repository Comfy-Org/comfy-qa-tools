"""Getting ComfyUI onto a box, and starting it where you can watch.

A tunnel to a machine with no ComfyUI on it is a tunnel to nothing. These are the
commands that run *on* the box: find an existing install, set one up if there is
none, then launch it with its log on your terminal.

The scripts are deliberately plain. Windows Server has bitten this project three
times in ways that all look like something else:

  1. `$ErrorActionPreference='Stop'` turns any native stderr into a terminating
     error, so a working install aborts and hides the real message. Left at
     Continue; success is judged by artifacts and exit codes.
  2. A BOM-less .ps1 is read as CP1252, so a UTF-8 dash becomes a stray quote and
     breaks the parse tens of lines later. Everything here is ASCII only.
  3. `Split-Path` with an empty value prompts, which hangs a non-interactive SSH
     command forever. Nothing here prompts, and PowerShell runs -NonInteractive
     -NoProfile. `-NonInteractive` alone is not the whole guard: a machine-wide
     or per-user profile runs BEFORE it takes effect, so a box whose profile
     asks anything hangs a command that carries every protection this file
     documents. `-NoProfile` also makes the invocation the same on every box,
     which is what a QA tool is for.

Because 1 rules out `-ErrorActionPreference Stop`, PowerShell carries on after a
failed step, and an install whose clone failed still ends by printing "install
complete" and exiting 0. So each script checks its own work: the clone is
verified before anything is installed into it, and main.py is verified before
success is claimed. Exit codes are the only thing the caller can trust.
"""

from __future__ import annotations

import re

from .build import COMFY_CLI_VERSION, CUDA_TAGS, DEFAULT, Build
from .config import Host, declares_no_gpu
from .osfamily import is_windows
from .tunnel import COMFYUI_PORT

# Where ComfyUI lives on each kind of box. Windows matches the convention already
# used on the other machines here.
WINDOWS_ROOT = r"C:\ComfyUI"
LINUX_ROOT = "/opt/comfyui"

# Where an interpreter can be on a box, in the order it is looked for. An install
# can be a venv, the Windows portable bundle, or a system interpreter, and
# assuming one of them is how a working box reported "python.exe is not
# recognized".
#
# ONE list, asked by every command that has to find the interpreter. It used to
# be three: the full four Windows layouts here, a two-layout chain written out
# again inside `verify_command`, and the same short chain a third time in
# `repair_command`. Proven on a sandbox whose only interpreter was
# `.venv/bin/python` — a layout `launch_command` explicitly supports — carrying
# a torch that reported `2.5.1+cu121`:
#
#     verify says:      NO_TORCH
#     launch would use: <root>/.venv/bin/python
#
# So the tool said "installed but cannot start" about a box it could start, sent
# the tester to reinstall something that already worked, and the machine billed
# throughout. That is the expensive direction.
#
# `hostfile` had this exact shape and it cost six defects before its three
# copies of one regex were made module-level constants; this is the same fix.
# The order is a search order, so it is part of the truth: the venv the tool
# builds itself comes first, and a system interpreter is the last resort.
WINDOWS_PYTHONS = (
    r"venv\Scripts\python.exe",
    r"python_embeded\python.exe",
    r".venv\Scripts\python.exe",
    r"ComfyUI_windows_portable\python_embeded\python.exe",
)
LINUX_PYTHONS = ("venv/bin/python", ".venv/bin/python")


def windows_python_search(fallback: str) -> str:
    """PowerShell that leaves the box's ComfyUI interpreter in `$py`.

    `fallback` is the PowerShell expression used when none of the layouts is
    there, and it is the one thing the callers genuinely disagree about — not
    the list. A launch wants `(Get-Command python).Source` so it can tell an
    absent interpreter from a present one and exit `NO_PYTHON`; a check wants
    the bare name `'python'` so it still reaches a verdict instead of dying
    before it prints one. That difference is deliberate and is kept.
    """
    candidates = ", ".join(f"'{WINDOWS_ROOT}\\{name}'" for name in WINDOWS_PYTHONS)
    return (
        f"$candidates = @({candidates}); "
        "$py = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1; "
        f"if (-not $py) {{ $py = {fallback} }}; "
    )


def linux_python_search() -> str:
    """sh that leaves the box's ComfyUI interpreter in `$py`, empty if none.

    `command -v python3` is last for the same reason it is last in
    `install_command`: it is whatever the image happens to ship, which on a
    newer one is a series ComfyUI's custom nodes have no wheels for. It is
    resolved to a path rather than left as a bare name so `[ -x ]` can answer
    for it like any other candidate.
    """
    candidates = " ".join(f"{LINUX_ROOT}/{name}" for name in LINUX_PYTHONS)
    return (
        f'py=""; for p in {candidates} $(command -v python3); do '
        '  if [ -x "$p" ]; then py="$p"; break; fi; done; '
    )


# Custom nodes still lack wheels for 3.13+, so the interpreter is pinned.
PYTHON_SERIES = "3.12"

# Git and Python on a Windows box, from their publishers' own installers.
#
# They came from `winget` until the build choice, which on a real box could
# not work: Google's windows-2022 image has no winget. Over SSH the install
# printed `The term 'winget' is not recognized` twice, then `py` was not there
# either, and it stopped at "comfy-cli did not install" (live run 2026-11-03,
# live-s2/evidence/41). Microsoft says why: "WinGet ... is available on Windows
# 11, modern versions of Windows 10, and Windows Server 2025 as a part of the
# App Installer" (https://learn.microsoft.com/en-us/windows/package-manager/winget/,
# read 2026-11-03). Server 2022 is not on that list.
#
# So each comes from where its publisher puts it, pinned to one file and checked
# by SHA-256 before it runs — a download cut short, or a page of HTML where the
# installer should be, is refused rather than run:
#
#   Python 3.12.10, the last 3.12 python.org built a Windows installer for
#   (3.12.11 and later are source-only security releases; their
#   python-3.12.x-amd64.exe are 404). python.org publishes MD5 for it, not
#   SHA-256: the file was fetched on 2026-11-03, its MD5 matched the one
#   python.org's release API gives (5eddb0b6f12c852725de071ae681dde4), and the
#   SHA-256 below is that file's. Run silently as python.org documents in
#   "Installing Without UI"
#   (https://docs.python.org/3.12/using/windows.html#installing-without-ui):
#   `/quiet InstallAllUsers=1 PrependPath=1 Include_test=0`, with TargetDir
#   named so the path below is not a guess about "%ProgramFiles%\Python X.Y".
#
#   Git 2.56.0.windows.4, git-for-windows' latest release on 2026-11-03. Its
#   SHA-256 is the one in that release's own notes, and GitHub's asset digest
#   agrees. Run silently with the flags git-for-windows documents in "Silent or
#   Unattended Installation"
#   (https://gitforwindows.org/silent-or-unattended-installation), less the
#   optional component list; its default directory is C:\Program Files\Git and
#   its default PATH option puts `Git\cmd` on the machine's PATH.
WINDOWS_PYTHON_DIR = r"C:\Program Files\Python312"
WINDOWS_PYTHON = rf"{WINDOWS_PYTHON_DIR}\python.exe"
PYTHON_INSTALLER = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
PYTHON_INSTALLER_SHA256 = "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb"
WINDOWS_GIT_CMD = r"C:\Program Files\Git\cmd"
WINDOWS_GIT = rf"{WINDOWS_GIT_CMD}\git.exe"
GIT_INSTALLER = ("https://github.com/git-for-windows/git/releases/download/"
                 "v2.56.0.windows.4/Git-2.56.0.4-64-bit.exe")
GIT_INSTALLER_SHA256 = "4961a4a8cc778d8b6e00a5819e5fe4d078b754555aff7aedc2d77a5d74c2de80"

# The Visual C++ runtime, which torch on Windows loads its DLLs against and
# Google's windows-2022 image does not carry. The first Windows install that got
# as far as starting ComfyUI died at `import torch`: "Microsoft Visual C++
# Redistributable is not installed, this may lead to the DLL load failure", then
# `[WinError 126] ... Error loading "...\torch\lib\c10.dll" or one of its
# dependencies` (live run 2026-11-03, live-s2/evidence/46).
#
# From Microsoft, by the permalink its "Latest supported Visual C++
# Redistributable downloads" page gives for x64
# (https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist,
# read 2026-11-03). A permalink serves whatever is latest, so there is no digest
# to pin; the download is held to a valid Authenticode signature from Microsoft
# Corporation instead. "Redistributing Visual C++ files"
# (https://learn.microsoft.com/en-us/cpp/windows/redistributing-visual-cpp-files)
# gives the options — `/install`, `/quiet` "for fully unattended installations",
# `/norestart` — and says to skip the install where the registry already records
# a version, under `...\VisualStudio\14.0\VC\Runtimes\x64`, because a package
# older than what is there fails. Its exit codes are Windows Installer's
# (https://learn.microsoft.com/en-us/windows/win32/msi/error-codes): 0, 3010
# "a restart is required to complete the install. This message indicates
# success", and 1638 "another version of this product is already installed".
VC_REDIST = "https://aka.ms/vc14/vc_redist.x64.exe"
VC_REDIST_KEY = r"HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64"

# Linux cannot pin it the way Windows can. Windows installs the interpreter it
# wants (python.org's installer, above); a Linux image ships exactly one python3
# and the archive holds no other — Ubuntu 22.04 has 3.10, 24.04 has 3.12. So the
# box is asked what it has, newest first, and anything 3.13+ is passed over
# rather than used. Assuming `python3.12` was there is what made the first real
# Linux box fail: it fell through to `python3` (3.10) and then to a venv module
# Ubuntu does not install by default.
PYTHON_SERIES_SUPPORTED = ("3.12", "3.11", "3.10")

# apt on a freshly booted cloud image is usually already busy — cloud-init and
# unattended-upgrades both hold the dpkg lock for the first minute or two, and
# the failure is an immediate "could not get lock", not a wait. Every apt call
# here carries this rather than racing it.
APT_LOCK_WAIT = 300

# Where `rdp` forwards Remote Desktop to. 3389 locally would collide with a real
# RDP server on this Mac; the far side is always 3389 because that is Windows.
RDP_PORT = 33389

# Google's Identity-Aware Proxy forwards from this range and only this range.
# A rule scoped to it is not an opening to the internet: reaching the port still
# requires a tunnel authenticated as someone with access to the project.
IAP_RANGE = "35.235.240.0/20"

# One name, so the rule is recognised on the next run rather than duplicated.
FIREWALL_RULE = "comfy-qat-iap-comfyui"

# The launch script's own word for "nothing here can run ComfyUI". Reserved, so
# the caller can recognise it rather than reporting a generic non-zero exit.
NO_PYTHON_EXIT = 3

# `logs_command`'s word for "this box has no ComfyUI log". Reserved for the same
# reason: "the file is not there" and "the box would not answer" are different
# facts with different fixes, and a generic non-zero exit cannot tell them apart.
NO_LOG_EXIT = 4

# Where a detached ComfyUI's output goes, on the box. A launch nobody is watching
# has to write its log somewhere or the whole point of detaching is lost: the
# terminal is free and the startup log is gone with it.
WINDOWS_LOG = rf"{WINDOWS_ROOT}\comfyui.log"
LINUX_LOG = f"{LINUX_ROOT}/comfyui.log"

# What a detached launch prints once ComfyUI is running on the box and this
# command is free to return. It is not "serving" — nothing has been asked yet —
# and the caller is expected to go on and prove that separately.
STARTED = "STARTED"

# What `alive_command` prints. GONE is the useful one: it turns a three-minute
# wait for something that died in four seconds into an immediate answer.
ALIVE = "ALIVE"
GONE = "GONE"


# `is_windows` is imported, not written here. It was written here AND in
# `lifecycle`, byte for byte, and this module and that one decide different halves
# of the same session about the same box — which path root to install into, and
# whether to tell the tester to open RDP or SSH. Two copies of one predicate is
# how they come to disagree.


def root_for(host: Host) -> str:
    return WINDOWS_ROOT if is_windows(host) else LINUX_ROOT


def log_for(host: Host) -> str:
    """Where this box's detached ComfyUI writes its log, in the box's own terms.

    Named in messages rather than kept private, because "read the log" is not an
    instruction anyone can follow without the path — and the path is on a machine
    they would have to tunnel into to look.
    """
    return WINDOWS_LOG if is_windows(host) else LINUX_LOG


def check_command(host: Host) -> str:
    """Print INSTALLED or MISSING. Nothing else, so the caller can branch on it.

    An install is main.py *and* an environment that can install into itself. It
    used to be main.py alone, and a real box proved why: the first attempt cloned
    ComfyUI, then failed to build the venv because Ubuntu ships venv in a separate
    package. main.py was there, so the next run reported "ComfyUI is already
    installed", skipped the install, and died several steps later on
    `No module named pip` — a message about a missing venv, printed by a run that
    had just declared the install complete.

    A venv that cannot run pip is not an environment; it is a directory. If one
    is present it has to work. A portable bundle carries no venv at all, which is
    why this asks rather than requires.

    On Windows the probe is wrapped in `try`, and that is not belt-and-braces.
    The venv this check exists to catch is one whose `pyvenv.cfg` names an
    interpreter that is no longer on the box, and a `python.exe` that cannot
    start is not a native command that exits non-zero — PowerShell cannot launch
    it at all, which is a *terminating* error even at `Continue`, and a
    terminating error here aborts the script before it prints anything. The
    caller reads an empty answer and a non-zero exit as "the box would not answer",
    so the one case this was written for would have come back as `could not run a
    command on comfy-win` — a sentence about the network, printed about a broken
    venv. `$ok` starts false, so every way of not proving pip works says MISSING,
    and every branch still prints one word and exits 0.
    """
    if is_windows(host):
        python = f"{WINDOWS_ROOT}\\venv\\Scripts\\python.exe"
        return (
            "powershell -NoProfile -NonInteractive -Command "
            f"\"if (-not (Test-Path '{WINDOWS_ROOT}\\main.py')) "
            "{ Write-Output 'MISSING'; exit 0 }; "
            f"if (Test-Path '{python}') {{ $ok = $false; "
            f"try {{ & '{python}' -m pip --version *> $null; "
            "$ok = ($LASTEXITCODE -eq 0) } catch { $ok = $false }; "
            "if (-not $ok) { Write-Output 'MISSING'; exit 0 } }; "
            "Write-Output 'INSTALLED'\""
        )
    return (
        f"if [ ! -f {LINUX_ROOT}/main.py ]; then echo MISSING; "
        f"elif [ -e {LINUX_ROOT}/venv ] && "
        f"! {LINUX_ROOT}/venv/bin/python -m pip --version >/dev/null 2>&1; "
        "then echo MISSING; else echo INSTALLED; fi"
    )


# PyPI's Windows torch wheel is CPU-only; the CUDA build lives on PyTorch's own
# index, and which index depends on the driver the box is running.
#
# This was pinned to cu128, which is how a real L4 ended up being told by ComfyUI
# that it "needs pytorch with cu130 or higher to use optimized CUDA operations".
# The install worked and the fast path stayed off — on a machine whose whole job
# is measuring how fast things are. A pinned CUDA version is a number that is
# wrong the moment the images move, so it is asked for instead.
#
# Newest first. A driver runs anything built for its own CUDA or older, so the
# first entry a driver can support is the best one.
TORCH_INDEXES = (
    (130, "https://download.pytorch.org/whl/cu130"),
    (128, "https://download.pytorch.org/whl/cu128"),
    (126, "https://download.pytorch.org/whl/cu126"),
    (124, "https://download.pytorch.org/whl/cu124"),
    (121, "https://download.pytorch.org/whl/cu121"),
    (118, "https://download.pytorch.org/whl/cu118"),
)

# When the box cannot be asked. Deliberately not the newest: an index the driver
# is too old for fails the install outright, where an older one merely leaves
# performance on the table.
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"

_CUDA_VERSION = re.compile(r"CUDA Version:\s*(\d+)\.(\d+)")


def cuda_command(host: Host) -> str:
    """Ask the box which CUDA its driver supports.

    `nvidia-smi` prints it in the header of its default output — there is no
    query field for it, which is why this reads a line rather than asking for a
    column.
    """
    if is_windows(host):
        return (
            "powershell -NoProfile -NonInteractive -Command \""
            "$smi = (& nvidia-smi 2>$null | Out-String); "
            f"if (-not $smi) {{ Write-Output '{NO_NVIDIA}'; exit 0 }}; "
            "$m = [regex]::Match($smi, 'CUDA Version:\\s*\\d+\\.\\d+'); "
            f"if ($m.Success) {{ Write-Output $m.Value }} else {{ Write-Output '{NO_NVIDIA}' }}\""
        )
    return (
        f"nvidia-smi 2>/dev/null | grep -o 'CUDA Version:[ ]*[0-9]*\\.[0-9]*' "
        f"|| echo {NO_NVIDIA}"
    )


def torch_index_for(reported: str | None) -> str:
    """The PyTorch index a box with this driver should install from.

    Anything unreadable falls back to the documented default rather than
    guessing high: an index the driver cannot run fails the install, where an
    older one only costs speed.
    """
    match = _CUDA_VERSION.search(reported or "")
    if not match:
        return TORCH_INDEX
    supported = int(match.group(1)) * 10 + int(match.group(2))
    for version, index in TORCH_INDEXES:
        if version <= supported:
            return index
    return TORCH_INDEX


# For a box declared to have no GPU. The CUDA builds above carry two gigabytes
# of runtime for a card that is not there; this one is torch and nothing else.
CPU_TORCH_INDEX = "https://download.pytorch.org/whl/cpu"


def torch_index(host: Host, reported: str | None = None) -> str:
    """The PyTorch index THIS box should install from.

    Asked of the box's declaration before its driver. `torch_index_for` reads
    `nvidia-smi`'s answer and falls back to a CUDA index when there is none —
    which is the right fallback for a GPU box that could not be asked and the
    wrong one for a box with no GPU, where there is never an answer.
    """
    if declares_no_gpu(host):
        return CPU_TORCH_INDEX
    return torch_index_for(reported)


def torch_install(python: str, host: Host, index: str | None = None) -> str:
    """Install torch so that it can see the card the box was rented for."""
    if declares_no_gpu(host):
        return (f"{python} -m pip install torch torchvision torchaudio "
                f"--index-url {CPU_TORCH_INDEX}")
    if is_windows(host):
        return (f"{python} -m pip install torch torchvision torchaudio "
                f"--index-url {index or TORCH_INDEX}")
    return f"{python} -m pip install torch torchvision torchaudio"


NO_NVIDIA = "NO_NVIDIA"

# What `verify_command` can print. Anything else means the check itself failed,
# which is not the same as the box being broken.
READY = "READY"
NO_COMFYUI = "NO_COMFYUI"
NO_TORCH = "NO_TORCH"
TORCH_NO_CUDA = "TORCH_NO_CUDA"


def verify_command(host: Host) -> str:
    """Ask the box whether ComfyUI could actually start, not whether it exists.

    `check_command` answers "is main.py there", and a box can pass that and be
    unusable in two ways this tool has now seen for real: a dependency added
    after the disk was imaged, and a torch that cannot see the card. The second
    is the dangerous one, because everything looks installed and the failure
    only appears at launch, after the box has been running and billing.

    Windows is checked on the version string rather than by importing torch:
    a CUDA build reports `2.13.0+cu128` and the CPU build reports `2.13.0`, and
    reading that costs nothing where `torch.cuda.is_available()` initialises a
    context. On Linux the PyPI wheel carries CUDA and does not carry the `+cu`
    marker, so there the question has to be asked directly.

    A box declared to have no GPU is asked a question it can pass: is torch
    there at all. Asked the other one it answers "torch cannot see the card"
    every time — true, and read by the caller as "wrong torch, reinstall the
    CUDA build", on every `go`, for a card the machine does not have.
    """
    if declares_no_gpu(host):
        return _verify_without_a_card(host)
    if is_windows(host):
        return (
            "powershell -NoProfile -NonInteractive -Command \""
            f"if (-not (Test-Path '{WINDOWS_ROOT}\\main.py')) "
            f"{{ Write-Output '{NO_COMFYUI}'; exit 0 }}; "
            f"Set-Location '{WINDOWS_ROOT}'; "
            + windows_python_search("'python'")
            + "$v = (& $py -m pip show torch 2>$null | Select-String '^Version:'); "
            f"if (-not $v) {{ Write-Output '{NO_TORCH}'; exit 0 }}; "
            f"if ($v -match '\\+cu') {{ Write-Output '{READY}' }} "
            f"else {{ Write-Output '{TORCH_NO_CUDA}' }}\""
        )
    return (
        f"if [ ! -f {LINUX_ROOT}/main.py ]; then echo {NO_COMFYUI}; exit 0; fi; "
        f"cd {LINUX_ROOT}; "
        + linux_python_search()
        + '[ -n "$py" ] || py=python3; '
        + f"\"$py\" -c \"import torch, sys; "
        f"sys.stdout.write('{READY}' if torch.cuda.is_available() else '{TORCH_NO_CUDA}')\" "
        f"2>/dev/null || echo {NO_TORCH}"
    )


def _verify_without_a_card(host: Host) -> str:
    """`verify_command` for a box with no GPU: READY when torch is installed."""
    if is_windows(host):
        return (
            "powershell -NoProfile -NonInteractive -Command \""
            f"if (-not (Test-Path '{WINDOWS_ROOT}\\main.py')) "
            f"{{ Write-Output '{NO_COMFYUI}'; exit 0 }}; "
            f"Set-Location '{WINDOWS_ROOT}'; "
            + windows_python_search("'python'")
            + "$v = (& $py -m pip show torch 2>$null | Select-String '^Version:'); "
            f"if (-not $v) {{ Write-Output '{NO_TORCH}' }} "
            f"else {{ Write-Output '{READY}' }}\""
        )
    return (
        f"if [ ! -f {LINUX_ROOT}/main.py ]; then echo {NO_COMFYUI}; exit 0; fi; "
        f"cd {LINUX_ROOT}; "
        + linux_python_search()
        + '[ -n "$py" ] || py=python3; '
        + f"\"$py\" -c \"import torch, sys; sys.stdout.write('{READY}')\" "
        f"2>/dev/null || echo {NO_TORCH}"
    )


# What `port_holder_command` prints when nothing is listening on ComfyUI's port.
PORT_FREE = "PORT_FREE"

# What `listening_command` prints. Three properties, each of which was a bug
# first:
#
#   * neither is empty — an empty answer means the box would not say, which is a
#     third outcome and never a "no";
#   * NEITHER IS A SUBSTRING OF THE OTHER. The obvious pair, `LISTENING` and
#     `NOT_LISTENING`, reads perfectly and is a trap: `"LISTENING" in
#     "NOT_LISTENING"` is True, so a caller testing membership sees a closed port
#     as an open one. Written that way, caught within the minute by the test for
#     the very failure it would have reintroduced — `rdp` resetting a Windows
#     password on a box that was not accepting Remote Desktop;
#   * both say what they mean without the caller's question, so a line of either
#     in a log is readable on its own.
LISTENING = "PORT_OPEN"
NOT_LISTENING = "PORT_CLOSED"

# What Remote Desktop listens on, on the box. `RDP_PORT` above is the LOCAL end
# of the forward; these are not the same number and reading one for the other
# asks the box about a port nothing on it has ever used.
RDP_REMOTE_PORT = 3389


def listening_command(host: Host, port: int) -> str:
    """Ask whether anything is listening on a port of the box. One word back.

    `port_holder_command` asks a neighbouring question — WHO holds ComfyUI's port
    — and is fixed to that one port because the answer it wants is a pid to stop.
    This one takes the port and answers yes or no, because the caller is not
    going to stop anything; it is deciding whether to do something irreversible.

    Written for `rdp`. A freshly created Windows box reaches RUNNING minutes
    before Remote Desktop is accepting connections, and `rdp` reset the password
    — which cannot be undone, and which invalidates the one anybody else on that
    box is using — before finding that out. Asking first costs one SSH round
    trip; not asking cost a live tester their password and left them with a box
    they still could not open.

    Both branches exit 0. "Nothing is listening" is an answer, not a failure, and
    a non-zero exit here would be read by the caller as "the box would not say".
    """
    if is_windows(host):
        return (
            "powershell -NoProfile -NonInteractive -Command \""
            f"$c = Get-NetTCPConnection -LocalPort {port} -State Listen "
            "-ErrorAction SilentlyContinue | Select-Object -First 1; "
            f"if ($c) {{ Write-Output '{LISTENING}' }} "
            f"else {{ Write-Output '{NOT_LISTENING}' }}\""
        )
    return (
        f"if ss -lntH 'sport = :{port}' 2>/dev/null | grep -q .; "
        f"then echo {LISTENING}; else echo {NOT_LISTENING}; fi"
    )


def firewall_command(host: Host) -> str:
    """Let ComfyUI's port through the operating system's own firewall.

    The second of two firewalls, and the one nobody remembers: Windows Server
    blocks inbound TCP by default, so a ComfyUI bound to 0.0.0.0 with a VPC rule
    in front of it still refuses the connection. Idempotent — the rule is created
    only if it is not already there, so this runs on every launch and does
    nothing on all but the first.
    """
    if is_windows(host):
        return (
            "powershell -NoProfile -NonInteractive -Command \""
            f"if (-not (Get-NetFirewallRule -DisplayName '{FIREWALL_RULE}' "
            "-ErrorAction SilentlyContinue)) { "
            f"New-NetFirewallRule -DisplayName '{FIREWALL_RULE}' -Direction Inbound "
            f"-Protocol TCP -LocalPort {COMFYUI_PORT} -Action Allow | Out-Null; "
            "Write-Output 'OPENED' } else { Write-Output 'ALREADY' }\""
        )
    # Linux images here run no firewall by default; if ufw is present and active
    # it is the one thing in the way, and if it is not this is a no-op.
    return (
        "if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q active; "
        f"then sudo ufw allow {COMFYUI_PORT}/tcp >/dev/null 2>&1 && echo OPENED; "
        "else echo ALREADY; fi"
    )


def port_holder_command(host: Host) -> str:
    """Ask what, if anything, is already listening on ComfyUI's port on the box.

    A port conflict on the far side of a tunnel is the wrong-machine failure one
    hop further out than usual: ComfyUI prints "Port 8188 is already in use" and
    a database lock error, neither of which says that the thing holding it is a
    ComfyUI this tool started and failed to stop.
    """
    if is_windows(host):
        return (
            "powershell -NoProfile -NonInteractive -Command \""
            f"$c = Get-NetTCPConnection -LocalPort {COMFYUI_PORT} -State Listen "
            "-ErrorAction SilentlyContinue | Select-Object -First 1; "
            f"if (-not $c) {{ Write-Output '{PORT_FREE}'; exit 0 }}; "
            "$p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue; "
            "Write-Output ($c.OwningProcess.ToString() + ' ' + "
            "$(if ($p) { $p.ProcessName } else { 'unknown' }))\""
        )
    return (
        f"pid=$(ss -lptnH 'sport = :{COMFYUI_PORT}' 2>/dev/null | "
        "grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2); "
        f"if [ -z \"$pid\" ]; then echo {PORT_FREE}; "
        "else echo \"$pid $(ps -p $pid -o comm= 2>/dev/null || echo unknown)\"; fi"
    )


def stop_command(host: Host, pid: str) -> str:
    """Stop a process on the box by pid. Used only on one this tool started."""
    if is_windows(host):
        return ("powershell -NoProfile -NonInteractive -Command "
                f"\"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue\"")
    return f"kill {pid} 2>/dev/null || true"


# What a repair says when there is no checkout under it. The caller judges this
# command by its exit code, so this is for whoever is reading the streamed log:
# "pip could not find requirements.txt" is a sentence about pip, printed about a
# box that has no ComfyUI on it.
NOTHING_TO_REPAIR = "NOTHING_TO_REPAIR: no main.py, so there is nothing to repair"


def repair_command(host: Host, *, force_torch: bool = False,
                   index: str | None = None) -> str:
    """Install the requirements of an existing checkout, without touching it.

    An install is not the same thing as a working install. `check_command` asks
    whether `main.py` is there, and a box moved from a snapshot taken before a
    dependency was added has the file and cannot start — on 2026-08-27 a real
    box died on `ModuleNotFoundError: No module named 'sqlalchemy'` after the
    tool had just reported "ComfyUI is already installed".

    Torch is installed first, from PyTorch's index on Windows. The first version
    of this ran `pip install -r requirements.txt` alone, and requirements.txt
    says plain `torch` — so on the same real box, pip fetched PyPI's CPU wheel
    and ComfyUI then died with "Torch not compiled with CUDA enabled". A repair
    that turns a GPU box into a CPU box is worse than the failure it fixes.

    `force_torch` is for when the wrong torch is already there. Pointing pip at
    the CUDA index is not enough on its own: pip matches on version, not on
    where a wheel came from, so `2.13.0` from PyPI satisfies a request for
    `2.13.0` from download.pytorch.org and it reports "Requirement already
    satisfied" while the box stays CPU-only. Watched it do exactly that. The
    reinstall is `--no-deps` because the dependencies are already correct and
    PyTorch's index does not carry all of them.
    """
    force = "--force-reinstall --no-deps " if force_torch else ""
    index = index or TORCH_INDEX
    # A box with no GPU gets the CPU build whatever index it was handed, on
    # both operating systems: an index chosen for a CUDA version is an answer to
    # a question this box was never the subject of.
    cpu_only = declares_no_gpu(host)
    if cpu_only:
        index = CPU_TORCH_INDEX
    what = "the CPU build of torch" if cpu_only else "torch for this GPU"
    if is_windows(host):
        python = windows_python_search("'python'")
        return (
            "powershell -NoProfile -NonInteractive -Command \""
            # The guard `install_command` has and this did not. Its comment names
            # the hazard exactly — "Without this the next line fails quietly and
            # everything after it installs into whatever directory PowerShell
            # happened to be in" — and it bites harder here, because rule 1 keeps
            # PowerShell at `Continue`, where a failed `Set-Location` is
            # NON-TERMINATING. The script carries on and spends several minutes
            # putting 2.5 GB of CUDA torch, then a requirements file, into
            # whatever directory the session started in.
            #
            # Linux guards its own `cd` and says why one line below. Only the
            # Windows half could silently continue — parallel code, one side
            # guarded, which is the shape this project keeps finding. Both sides
            # now say the same sentence, so the answer does not depend on which
            # box you are on.
            f"if (-not (Test-Path '{WINDOWS_ROOT}\\main.py')) "
            f"{{ Write-Output '{NOTHING_TO_REPAIR}'; exit 1 }}; "
            f"Set-Location '{WINDOWS_ROOT}'; "
            + python
            + f"Write-Output 'installing {what} (the slow part)'; "
            f"& $py -m pip install {force}torch torchvision torchaudio "
            f"--index-url {index}; "
            "Write-Output 'installing the rest of the requirements'; "
            "& $py -m pip install -r requirements.txt\""
        )
    return (
        # `|| exit 1`, because this was `cd ... &&` and a repair that cannot
        # reach the checkout must not go on to pip-install into whatever
        # directory the shell landed in. The `main.py` check above it is the
        # same question asked one step earlier, and it is here so that both
        # operating systems answer a missing checkout with the same sentence
        # rather than one getting a shell error and the other a message.
        f"if [ ! -f {LINUX_ROOT}/main.py ]; then echo '{NOTHING_TO_REPAIR}'; exit 1; fi; "
        f"cd {LINUX_ROOT} || exit 1; "
        + linux_python_search()
        + '[ -n "$py" ] || py=python3; '
        + f'"$py" -m pip install {force}torch torchvision torchaudio'
        + (f" --index-url {index}" if cpu_only else "")
        + ' && "$py" -m pip install -r requirements.txt'
    )


# --- installing with comfy-cli ------------------------------------------------
#
# The build choice hands the install to comfy-cli, on the box. It used to be a hand-rolled
# `git clone` of whatever master was that minute, then pip by hand; it is now
# `comfy install` with the build's own options (see `build.py`), so the tool
# that picks the release, the PR or the commit is the one Comfy ships for the
# purpose, and what this file adds around it is what comfy-cli does not do: the
# prerequisites, a pinned comfy-cli of its own, the pin on torch, and a record
# of what was asked for.
#
# Where things live. comfy-cli gets a venv of its own, beside ComfyUI rather than
# inside it, because `comfy install` refuses a workspace that already exists and
# is not a checkout — and a venv inside the workspace would be exactly that. Run
# from a venv, comfy-cli builds the workspace its own `.venv`
# (comfy_cli/resolve_python.py, `ensure_workspace_python`), which is the third
# layout `WINDOWS_PYTHONS` and `LINUX_PYTHONS` already search.
#
# On Linux the checkout itself is made in `/opt/comfy-qat/ComfyUI` and
# `/opt/comfyui` is a link to it. `/opt` belongs to root, and comfy-cli clones
# into a directory it creates — so it cannot create `/opt/comfyui`, and a
# `/opt/comfyui` made for it beforehand is a directory that exists and is not a
# checkout, which it refuses. Everything else in this file keeps saying
# `/opt/comfyui`, and a box from before the build choice, whose `/opt/comfyui` is a real
# checkout, is used where it is.
LINUX_TOOL = "/opt/comfy-qat"
WINDOWS_TOOL = r"C:\comfy-qat"
LINUX_CHECKOUT = f"{LINUX_TOOL}/ComfyUI"
_LINUX_CLI_PY = f"{LINUX_TOOL}/venv/bin/python"
_LINUX_CLI = f"{LINUX_TOOL}/venv/bin/comfy"
_WINDOWS_CLI_PY = rf"{WINDOWS_TOOL}\venv\Scripts\python.exe"
_WINDOWS_CLI = rf"{WINDOWS_TOOL}\venv\Scripts\comfy.exe"

# What the box keeps about its build. `build.txt` is `Build.spec()`, the build it
# was last asked to run; `frontend_root.txt` is where a frontend PR's build
# landed, which only the box can know.
LINUX_RECORD = f"{LINUX_TOOL}/build.txt"
WINDOWS_RECORD = rf"{WINDOWS_TOOL}\build.txt"
LINUX_FRONTEND_ROOT = f"{LINUX_TOOL}/frontend_root.txt"
WINDOWS_FRONTEND_ROOT = rf"{WINDOWS_TOOL}\frontend_root.txt"

# Node.js for building a frontend PR: the frontend's own package.json asks for
# `>=26.8.2 <27` and pnpm 11 (read 2026-11-03), and 26.13.0 is the LTS release
# nodejs.org listed that day. Only a frontend PR fetches it.
NODE_VERSION = "v26.13.0"

# What the install and the build change print first. The suite's fake box tells
# the two apart by these, so they are words a reader of the log can see too.
INSTALLING = "installing ComfyUI with comfy-cli"
CHANGING = "changing the build with comfy-cli"
# Printed only by a clean install, which is the one that deletes the checkout.
CLEANING = "clean install: removing the ComfyUI checkout"

# The Python that turns a frontend PR into a directory: comfy-cli's own builder,
# the function `comfy launch --frontend-pr` calls (comfy_cli/command/install.py,
# `handle_temporary_frontend_pr`), without the launch. No quote characters, so it
# rides inside the single quotes of either shell unchanged; the marker it prints
# comes in as an argument for the same reason.
_FRONTEND_BUILDER = ("import sys;"
                     "from comfy_cli.command.install import handle_temporary_frontend_pr as b;"
                     "r=b(sys.argv[1]);print();print(sys.argv[2]+(r or str()))")


def _cuda_for(index: str | None) -> str | None:
    """comfy-cli's `--cuda-version` for a PyTorch index: `13.0` for `.../cu130`.

    The box's driver still decides, exactly as before: the caller asks it with
    `cuda_command` and passes the index `torch_index` chose. comfy-cli would
    detect the same thing by itself; saying it explicitly keeps the choice this
    tool has always made, and visible in the command it prints.
    """
    tag = (index or "").rstrip("/").rsplit("/", 1)[-1]
    return CUDA_TAGS.get(tag)


def comfy_install_args(host: Host, build: Build = DEFAULT, index: str | None = None
                       ) -> list[str]:
    """`comfy install`'s options for this box and build, unquoted."""
    if declares_no_gpu(host):
        gpu = ["--cpu"]
    else:
        gpu = ["--nvidia"]
        cuda = build.cuda_version or _cuda_for(index)
        if cuda:
            gpu += ["--cuda-version", cuda]
    manager = [] if build.manager else ["--skip-manager"]
    return [*gpu, *build.backend_args(), *manager]


def _sh_args(args: list[str]) -> str:
    """The arguments as sh reads them. Only `#` needs quoting here: it starts a
    comment, so `--pr #17497` would lose its value."""
    return " ".join(f"'{arg}'" if arg.startswith("#") else arg for arg in args)


def _ps_array(args: list[str]) -> str:
    return "@(" + ", ".join(f"'{arg}'" for arg in args) + ")"


def install_command(host: Host, index: str | None = None, *, build: Build | None = None,
                    clean: bool = False) -> str:
    """Set ComfyUI up from nothing with comfy-cli, printing progress as it goes.

    `index` is the PyTorch index this box's driver supports; the caller asks the
    box with `cuda_command` and passes the answer, and it reaches comfy-cli as
    `--cuda-version`. A `+cuXXX` torch pin in `build` wins over it. A box
    declared to have no GPU installs with `--cpu`, whatever was passed.

    A checkout already there — one whose environment was broken, which is when
    `check_command` sends a box here with main.py on it — is installed into with
    `--restore` rather than deleted, so its custom nodes and models survive.
    `clean` is the one that deletes it, and only when asked.
    """
    build = build or DEFAULT
    if is_windows(host):
        return _windows_script(host, build, index, clean=clean, change=False)
    return _linux_script(host, build, index, clean=clean, change=False)


def change_command(host: Host, build: Build, index: str | None = None) -> str:
    """Put a different build on a box that already has ComfyUI, keeping its nodes.

    `comfy install --restore` on the existing checkout: comfy-cli checks out the
    release, the PR or the commit in place and reinstalls requirements and
    Manager, and nothing under `custom_nodes/` or `models/` is touched — they
    are not part of the checkout. A build that names no backend leaves the
    checkout where it is and changes only what it does name.
    """
    if is_windows(host):
        return _windows_script(host, build, index, clean=False, change=True)
    return _linux_script(host, build, index, clean=False, change=True)


def _linux_script(host: Host, build: Build, index: str | None, *, clean: bool,
                  change: bool) -> str:
    # `env DEBIAN_FRONTEND=noninteractive`, not `sudo DEBIAN_FRONTEND=...`:
    # command-line environment assignments go through sudoers, and a hardened
    # one refuses them outright. `env` is a binary run as root and always works.
    #
    # Without it apt opens with three lines that read exactly like a crash —
    # "debconf: unable to initialize frontend: Dialog", "(Dialog frontend will
    # not work on a non-interactive terminal…)", "falling back to frontend:
    # Readline" — four lines into an install whose next stretch is silent while
    # torch downloads. That combination is where `go` gets interrupted: nothing
    # here has failed, and the only output on the screen says it has.
    apt = (f"sudo env DEBIAN_FRONTEND=noninteractive "
           f"apt-get -o DPkg::Lock::Timeout={APT_LOCK_WAIT} -y -qq")
    series = " ".join(PYTHON_SERIES_SUPPORTED)
    args = comfy_install_args(host, build, index)
    shown = " ".join(args)
    parts = [
        "set -e; ",
        f"echo '{CHANGING if change else INSTALLING}: {build.describe()}'; "
        .replace(" · ", ", "),
        "echo 'installing prerequisites'; ",
        f"{apt} update; ",
        f"{apt} install git; ",
        # The interpreter is discovered, not assumed. `python3` is deliberately
        # last: on 24.04 it is 3.12 and fine, on 22.04 it is 3.10 and also fine,
        # but on an image that has moved to 3.13 it is the one answer that must
        # not win, so it is only reached when no supported series is installed.
        f'PY=""; for v in {series}; do ',
        'if command -v "python$v" >/dev/null 2>&1; then PY="python$v"; break; fi; ',
        "done; ",
        f'if [ -z "$PY" ]; then for v in {series}; do ',
        f'if {apt} install "python$v-venv" >/dev/null 2>&1; ',
        'then PY="python$v"; break; fi; done; fi; ',
        'if [ -z "$PY" ]; then ',
        "echo 'INSTALL_INCOMPLETE: no supported python (3.10-3.12) on this image'; ",
        "exit 1; fi; ",
        # Debian and Ubuntu ship venv as a separate package, so a present
        # interpreter is not a usable one. Installing it is cheap and idempotent.
        f'{apt} install "$PY-venv"; ',
        'echo "building with $PY"; ',
        f'sudo mkdir -p {LINUX_TOOL} && sudo chown "$USER" {LINUX_TOOL}; ',
        f"echo 'installing comfy-cli {COMFY_CLI_VERSION} into {LINUX_TOOL}/venv'; ",
        # --clear when one is there and broken: a venv built before python3.x-venv
        # was installed has no pip in it, and `python -m venv` over the top of it
        # leaves that as it is.
        f"if {_LINUX_CLI_PY} -m pip --version >/dev/null 2>&1; ",
        "then echo 'reusing the venv comfy-cli is in'; ",
        f'else "$PY" -m venv --clear {LINUX_TOOL}/venv; fi; ',
        f"if ! {_LINUX_CLI_PY} -m pip --version >/dev/null 2>&1; then ",
        "echo 'INSTALL_INCOMPLETE: the venv for comfy-cli has no pip'; exit 1; fi; ",
        f"{_LINUX_CLI_PY} -m pip install --quiet --upgrade pip; ",
        f"{_LINUX_CLI_PY} -m pip install --quiet comfy-cli=={COMFY_CLI_VERSION}; ",
        f"if [ ! -x {_LINUX_CLI} ]; then ",
        "echo 'INSTALL_INCOMPLETE: comfy-cli did not install'; exit 1; fi; ",
        # The exit tax. comfy-cli flushes telemetry at exit against a five-second
        # deadline unless told not to (reference-comfy-cli-defects.md, defect 1),
        # and it would otherwise pay it on every command it runs here. The other
        # two keep its Rich output from dying on a byte the terminal cannot take.
        "export DO_NOT_TRACK=1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8; ",
    ]
    parts.append(_linux_pin(build))
    if change:
        parts += [
            f"if [ ! -f {LINUX_ROOT}/main.py ]; then ",
            f"echo 'INSTALL_INCOMPLETE: there is no ComfyUI in {LINUX_ROOT} to change'; ",
            "exit 1; fi; ",
            # A tag or a commit newer than the checkout's last fetch is not there
            # to check out. comfy-cli fetches for a PR, not for these.
            f"git -c safe.directory='*' -C {LINUX_ROOT} fetch --tags --quiet origin "
            "|| echo 'could not fetch from origin - using what the checkout has'; ",
            f"WS={LINUX_ROOT}; RESTORE=--restore; ",
        ]
    else:
        if clean:
            parts += [
                f"echo '{CLEANING}, its custom nodes and its models'; ",
                f"target=$(readlink -f {LINUX_ROOT} || echo {LINUX_ROOT}); ",
                'case "$target" in /opt/?*) ;; *) ',
                "echo 'INSTALL_INCOMPLETE: refusing to remove a checkout outside /opt'; ",
                "exit 1;; esac; ",
                f'sudo rm -rf "$target" {LINUX_ROOT}; ',
            ]
        parts += [
            f"if [ -f {LINUX_ROOT}/main.py ]; then ",
            # A checkout whose venv cannot run pip is why `check_command` said
            # MISSING. The venv goes; the checkout, its nodes and models stay.
            f"if [ -e {LINUX_ROOT}/venv ] && ! {LINUX_ROOT}/venv/bin/python -m pip "
            "--version >/dev/null 2>&1; then echo 'rebuilding a venv that has no pip'; ",
            f"rm -rf {LINUX_ROOT}/venv; fi; ",
            f"WS={LINUX_ROOT}; RESTORE=--restore; ",
            "else ",
            f"if [ -e {LINUX_ROOT} ] && [ ! -L {LINUX_ROOT} ]; then ",
            f"sudo rmdir {LINUX_ROOT} 2>/dev/null || {{ echo 'INSTALL_INCOMPLETE: "
            f"{LINUX_ROOT} holds something that is not ComfyUI - move it aside and run "
            "this again'; exit 1; }; fi; ",
            # What an interrupted first attempt leaves: a clone with no main.py,
            # which comfy-cli refuses as "not a valid git repository".
            f"if [ -d {LINUX_CHECKOUT} ] && [ ! -f {LINUX_CHECKOUT}/main.py ]; then "
            f"rm -rf {LINUX_CHECKOUT}; fi; ",
            f'WS={LINUX_CHECKOUT}; RESTORE=""; fi; ',
        ]
    parts += [
        "echo 'comfy-cli installs torch first (the slow part), then the "
        "requirements, then Manager'; ",
        f'echo "+ comfy --skip-prompt --workspace $WS install $RESTORE {shown}"; ',
        f'{_LINUX_CLI} --skip-prompt --workspace "$WS" install $RESTORE {_sh_args(args)}; ',
        f"if [ ! -e {LINUX_ROOT} ]; then sudo ln -s {LINUX_CHECKOUT} {LINUX_ROOT}; fi; ",
        f"if [ ! -f {LINUX_ROOT}/main.py ]; then echo INSTALL_INCOMPLETE; exit 1; fi; ",
    ]
    parts.append(_linux_frontend(build))
    parts += [
        f"printf '%s\\n' '{build.spec()}' > {LINUX_RECORD}; ",
        "echo 'install complete'",
    ]
    return "".join(parts)


def _linux_pin(build: Build) -> str:
    constraint = build.constraint()
    if not constraint:
        return f"rm -f {LINUX_TOOL}/constraints.txt; unset PIP_CONSTRAINT; "
    return (f"printf '%s\\n' '{constraint}' > {LINUX_TOOL}/constraints.txt; "
            f"export PIP_CONSTRAINT={LINUX_TOOL}/constraints.txt; "
            f"echo 'pinning {constraint} for every pip install comfy-cli runs'; ")


def _linux_frontend(build: Build) -> str:
    if not build.frontend_pr:
        return f"rm -f {LINUX_FRONTEND_ROOT}; "
    node = f"{LINUX_TOOL}/node"
    tarball = (f"https://nodejs.org/dist/{NODE_VERSION}/"
               f"node-{NODE_VERSION}-linux-x64.tar.xz")
    return (
        f"echo 'building frontend PR #{build.frontend_pr} with comfy-cli "
        f"(Node.js {NODE_VERSION} and pnpm first)'; "
        f"if [ ! -x {node}/bin/node ]; then mkdir -p {node}; "
        f"curl -fsSL {tarball} | tar -xJ -C {node} --strip-components=1; fi; "
        f"export PATH={node}/bin:$PATH; "
        "command -v pnpm >/dev/null 2>&1 || npm install -g pnpm@11; "
        f"out=$({_LINUX_CLI_PY} -c '{_FRONTEND_BUILDER}' "
        f"'#{build.frontend_pr}' 'FRONTEND_ROOT='); "
        "printf '%s\\n' \"$out\"; "
        "root=$(printf '%s\\n' \"$out\" | sed -n 's/^FRONTEND_ROOT=//p' | tail -n 1); "
        "if [ -z \"$root\" ]; then "
        "echo 'INSTALL_INCOMPLETE: the frontend PR did not build'; exit 1; fi; "
        f"printf '%s\\n' \"$root\" > {LINUX_FRONTEND_ROOT}; "
    )


def _windows_script(host: Host, build: Build, index: str | None, *, clean: bool,
                    change: bool) -> str:
    """The same install on Windows Server. ASCII only, `Continue` throughout,
    no `Split-Path`, judged by exit codes and artefacts — the three rules at
    the top of this file, each of which cost a real box an afternoon."""
    args = comfy_install_args(host, build, index)
    root = WINDOWS_ROOT
    parts = [
        "powershell -NoProfile -NonInteractive -Command \"",
        f"Write-Output '{CHANGING if change else INSTALLING}: "
        f"{build.describe().replace(' · ', ', ')}'; ",
        "Write-Output 'installing prerequisites'; ",
        f"New-Item -ItemType Directory -Force -Path '{WINDOWS_TOOL}' | Out-Null; ",
        _windows_prerequisites(),
        # The installers change the machine's PATH, not this session's, and
        # comfy-cli needs git on PATH for every checkout it makes. Git's own
        # directory is named too, for a session whose registry read comes back
        # stale.
        "$env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + "
        "[Environment]::GetEnvironmentVariable('Path', 'User') + "
        f"';{WINDOWS_GIT_CMD}'; ",
        f"Write-Output 'installing comfy-cli {COMFY_CLI_VERSION} into "
        f"{WINDOWS_TOOL}\\venv'; ",
        # The interpreter by its path, not `py -3.12`: the launcher is one more
        # thing a bare image does not have, and the path is the one just checked.
        f"if (-not (Test-Path '{_WINDOWS_CLI_PY}')) "
        f"{{ & '{WINDOWS_PYTHON}' -m venv '{WINDOWS_TOOL}\\venv' }}; ",
        f"& '{_WINDOWS_CLI_PY}' -m pip install --quiet --upgrade pip; ",
        f"& '{_WINDOWS_CLI_PY}' -m pip install --quiet comfy-cli=={COMFY_CLI_VERSION}; ",
        f"if (-not (Test-Path '{_WINDOWS_CLI}')) "
        "{ Write-Output 'INSTALL_INCOMPLETE: comfy-cli did not install'; exit 1 }; ",
        # See the Linux half for these three.
        "$env:DO_NOT_TRACK = '1'; $env:PYTHONUTF8 = '1'; $env:PYTHONIOENCODING = 'utf-8'; ",
    ]
    constraint = build.constraint()
    if constraint:
        parts += [
            f"Set-Content -Path '{WINDOWS_TOOL}\\constraints.txt' -Value '{constraint}' "
            "-Encoding ascii; ",
            f"$env:PIP_CONSTRAINT = '{WINDOWS_TOOL}\\constraints.txt'; ",
            f"Write-Output 'pinning {constraint} for every pip install comfy-cli runs'; ",
        ]
    else:
        parts.append(f"Remove-Item -Path '{WINDOWS_TOOL}\\constraints.txt' -Force "
                     "-ErrorAction SilentlyContinue; $env:PIP_CONSTRAINT = $null; ")
    parts.append(f"$a = @('--skip-prompt', '--workspace', '{root}', 'install'); ")
    if change:
        parts += [
            f"if (-not (Test-Path '{root}\\main.py')) {{ Write-Output "
            f"'INSTALL_INCOMPLETE: there is no ComfyUI in {root} to change'; exit 1 }}; ",
            f"& git -c safe.directory=* -C '{root}' fetch --tags --quiet origin 2>$null; ",
            "$a += '--restore'; ",
        ]
    else:
        if clean:
            parts += [
                f"Write-Output '{CLEANING}, its custom nodes and its models'; ",
                f"if (Test-Path '{root}') {{ Remove-Item -Recurse -Force '{root}' }}; ",
            ]
        parts += [
            f"if (Test-Path '{root}\\main.py') {{ $a += '--restore' }} ",
            # An interrupted clone, which comfy-cli refuses as not a checkout.
            f"elseif (Test-Path '{root}') {{ Remove-Item -Recurse -Force '{root}' }}; ",
        ]
    parts += [
        f"$a += {_ps_array(args)}; ",
        "Write-Output 'comfy-cli installs torch first (the slow part), then the "
        "requirements, then Manager'; ",
        "Write-Output ('+ comfy ' + ($a -join ' ')); ",
        f"& '{_WINDOWS_CLI}' @a; ",
        "if ($LASTEXITCODE -ne 0) "
        "{ Write-Output ('INSTALL_INCOMPLETE: comfy install exited ' + $LASTEXITCODE); "
        "exit 1 }; ",
        f"if (-not (Test-Path '{root}\\main.py')) "
        "{ Write-Output 'INSTALL_INCOMPLETE'; exit 1 }; ",
    ]
    parts.append(_windows_frontend(build))
    parts += [
        f"Set-Content -Path '{WINDOWS_RECORD}' -Value '{build.spec()}' -Encoding ascii; ",
        "Write-Output 'install complete'\"",
    ]
    return "".join(parts)


def _windows_prerequisites() -> str:
    """Git, then Python, each only when it is not already where it goes.

    Each is downloaded beside the tool, refused unless its SHA-256 is the
    pinned one, run with `Start-Process -Wait -PassThru` — both are GUI
    programs, which `&` starts and does not wait for — and judged twice: by
    its exit code, then by the file it should have left. The installer is
    removed once it has worked. `$ProgressPreference` is silenced first:
    Windows PowerShell 5.1 draws a progress bar per chunk, which makes a 60 MB
    download take minutes. TLS 1.2 is added to what .NET offers, never put in
    place of it, for an image whose defaults predate it.

    Python's installer is a Windows Installer bundle, so 3010 — "succeeded, a
    restart completes it" — is success too; Git's is Inno Setup, where only 0
    is. The quote around TargetDir is `[char]34` for the reason
    `launch_detached_command` gives: the script is already inside double
    quotes on its way to the box.

    Then the Visual C++ runtime torch needs, last and before comfy-cli fetches
    torch. Its permalink has no fixed digest, so it is checked by its
    Authenticode signature instead (see `VC_REDIST`), and "installed" is the
    registry key Microsoft says records it.
    """
    def by_digest(digest: str) -> str:
        def check(name: str) -> str:
            return (
                f"if ((Get-FileHash -Algorithm SHA256 -Path $f).Hash -ne '{digest}') "
                f"{{ Write-Output 'INSTALL_INCOMPLETE: the {name} installer that "
                "downloaded is not the file its publisher gives (its SHA-256 differs)'; "
                "exit 1 }; ")
        return check

    def by_microsofts_signature(name: str) -> str:
        return (
            "$s = Get-AuthenticodeSignature -FilePath $f; "
            "if (($s.Status -ne 'Valid') -or ($s.SignerCertificate.Subject -notlike "
            f"'*O=Microsoft Corporation*')) {{ Write-Output 'INSTALL_INCOMPLETE: the "
            f"{name} installer that downloaded is not signed by Microsoft'; exit 1 }}; ")

    # `$u`, `$f` and `$i` — the URL, the file it is saved as, and what it
    # installs — so each is written once. Written out at every use, the three
    # installers took the longest Windows command to 7,973 characters, within
    # 218 of cmd.exe's 8,191 (see `test_every_windows_command_fits_on_one_cmd_line`).
    def one(name: str, url: str, check, arguments: str, installed: str,
            succeeded: tuple[int, ...]) -> str:
        saved = f"{WINDOWS_TOOL}\\{url.rsplit('/', 1)[-1]}"
        failed = " -and ".join(f"($c -ne {code})" for code in succeeded)
        return (
            f"$u = '{url}'; $f = '{saved}'; $i = '{installed}'; "
            "if (-not (Test-Path $i)) { "
            f"Write-Output ('installing {name} from ' + $u); "
            "Invoke-WebRequest -Uri $u -OutFile $f -UseBasicParsing; "
            "if (-not (Test-Path $f)) { Write-Output "
            f"'INSTALL_INCOMPLETE: the {name} installer did not download'; exit 1 }}; "
            + check(name) +
            f"$p = Start-Process -FilePath $f -ArgumentList {arguments} "
            "-Wait -PassThru; $c = $p.ExitCode; "
            f"if ({failed}) {{ Write-Output ('INSTALL_INCOMPLETE: the {name} installer "
            "exited ' + $c); exit 1 }; "
            "if (-not (Test-Path $i)) { Write-Output ('INSTALL_INCOMPLETE: "
            f"the {name} installer finished but ' + $i + ' is not there'); exit 1 }}; "
            "Remove-Item -Path $f -Force -ErrorAction SilentlyContinue; "
            f"Write-Output '{name} installed' }}; "
        )

    return (
        "$ProgressPreference = 'SilentlyContinue'; "
        "[Net.ServicePointManager]::SecurityProtocol = "
        "[Net.ServicePointManager]::SecurityProtocol -bor "
        "[Net.SecurityProtocolType]::Tls12; "
        "$q = [char]34; "
        + one("Git", GIT_INSTALLER, by_digest(GIT_INSTALLER_SHA256),
              "'/VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS "
              "/RESTARTAPPLICATIONS'",
              WINDOWS_GIT, (0,))
        + one("Python", PYTHON_INSTALLER, by_digest(PYTHON_INSTALLER_SHA256),
              "('/quiet InstallAllUsers=1 PrependPath=1 Include_test=0 TargetDir=' + "
              f"$q + '{WINDOWS_PYTHON_DIR}' + $q)",
              WINDOWS_PYTHON, (0, 3010))
        + one("Visual C++ runtime", VC_REDIST, by_microsofts_signature,
              "'/install /quiet /norestart'",
              VC_REDIST_KEY, (0, 1638, 3010))
    )


def _windows_frontend(build: Build) -> str:
    if not build.frontend_pr:
        return (f"Remove-Item -Path '{WINDOWS_FRONTEND_ROOT}' -Force "
                "-ErrorAction SilentlyContinue; ")
    node = rf"{WINDOWS_TOOL}\node-{NODE_VERSION}-win-x64"
    archive = f"https://nodejs.org/dist/{NODE_VERSION}/node-{NODE_VERSION}-win-x64.zip"
    return (
        f"Write-Output 'building frontend PR #{build.frontend_pr} with comfy-cli "
        f"(Node.js {NODE_VERSION} and pnpm first)'; "
        f"if (-not (Test-Path '{node}\\node.exe')) {{ $ProgressPreference = "
        f"'SilentlyContinue'; Invoke-WebRequest -Uri '{archive}' -OutFile "
        f"'{WINDOWS_TOOL}\\node.zip' -UseBasicParsing; Expand-Archive -Path "
        f"'{WINDOWS_TOOL}\\node.zip' -DestinationPath '{WINDOWS_TOOL}' -Force }}; "
        f"$env:Path = '{node};' + $env:Path; "
        f"if (-not (Test-Path '{node}\\pnpm.cmd')) "
        f"{{ & '{node}\\npm.cmd' install -g pnpm@11 --prefix '{node}' }}; "
        f"$out = (& '{_WINDOWS_CLI_PY}' -c '{_FRONTEND_BUILDER}' "
        f"'#{build.frontend_pr}' 'FRONTEND_ROOT='); "
        "$out | ForEach-Object { Write-Output $_ }; "
        "$hit = $out | Where-Object { $_ -like 'FRONTEND_ROOT=*' } | Select-Object -Last 1; "
        "$root = ''; if ($hit) { $root = $hit.Substring(14) }; "
        "if (-not $root) { Write-Output 'INSTALL_INCOMPLETE: the frontend PR did not "
        "build'; exit 1 }; "
        f"Set-Content -Path '{WINDOWS_FRONTEND_ROOT}' -Value $root -Encoding ascii; "
    )


def record_command(host: Host, build: Build) -> str:
    """Write the box's record and nothing else: a frontend RELEASE change.

    ComfyUI fetches a frontend release itself when it starts, so moving to one
    is a change of record and of launch arguments, not an install.
    """
    if is_windows(host):
        return ("powershell -NoProfile -NonInteractive -Command \""
                f"New-Item -ItemType Directory -Force -Path '{WINDOWS_TOOL}' | Out-Null; "
                f"Set-Content -Path '{WINDOWS_RECORD}' -Value '{build.spec()}' "
                "-Encoding ascii\"")
    return (f'sudo mkdir -p {LINUX_TOOL} && sudo chown "$USER" {LINUX_TOOL}; '
            f"printf '%s\\n' '{build.spec()}' > {LINUX_RECORD}")


def facts_command(host: Host) -> str:
    """Ask the box what it runs: the checkout's commit and branch, and its record.

    Four lines, `KEY=value`, every one of them printed whatever is missing, and
    exit 0 — "nothing recorded" is an answer. git is asked with
    `safe.directory` open because the checkout belongs to whoever installed it,
    and git refuses another user's repository otherwise.
    """
    if is_windows(host):
        return (
            "powershell -NoProfile -NonInteractive -Command \""
            "$git = (Get-Command git -ErrorAction SilentlyContinue).Source; "
            f"if (-not $git) {{ $git = '{WINDOWS_GIT}' }}; "
            "$c = ''; $b = ''; "
            f"if ((Test-Path '{WINDOWS_ROOT}\\.git') -and (Test-Path $git)) {{ "
            f"$c = (& $git -c safe.directory=* -C '{WINDOWS_ROOT}' rev-parse HEAD "
            "2>$null); "
            f"$b = (& $git -c safe.directory=* -C '{WINDOWS_ROOT}' rev-parse "
            "--abbrev-ref HEAD 2>$null) }; "
            f"$r = ''; if (Test-Path '{WINDOWS_RECORD}') "
            f"{{ $r = (Get-Content -Path '{WINDOWS_RECORD}' -TotalCount 1) }}; "
            f"$f = ''; if (Test-Path '{WINDOWS_FRONTEND_ROOT}') "
            f"{{ $f = (Get-Content -Path '{WINDOWS_FRONTEND_ROOT}' -TotalCount 1) }}; "
            "Write-Output ('COMMIT=' + $c); Write-Output ('BRANCH=' + $b); "
            "Write-Output ('RECORD=' + $r); Write-Output ('FRONTEND_ROOT=' + $f)\""
        )
    git = f"git -c safe.directory='*' -C {LINUX_ROOT}"
    return (
        f'echo "COMMIT=$({git} rev-parse HEAD 2>/dev/null)"; '
        f'echo "BRANCH=$({git} rev-parse --abbrev-ref HEAD 2>/dev/null)"; '
        f'echo "RECORD=$(head -n 1 {LINUX_RECORD} 2>/dev/null)"; '
        f'echo "FRONTEND_ROOT=$(head -n 1 {LINUX_FRONTEND_ROOT} 2>/dev/null)"; '
        "exit 0"
    )


# Git's directory, first on a Windows launch's PATH. Manager imports GitPython,
# which needs `git` on PATH when ComfyUI starts, and Git's installer writes its
# directory into the machine's PATH in the registry — which a session opened by
# a service that started before the install is not certain to have re-read. The
# install script reads the registry itself for the same reason. Naming the
# directory costs nothing on a box where it is already on PATH.
_WINDOWS_GIT_ON_PATH = f"$env:Path = '{WINDOWS_GIT_CMD};' + $env:Path; "


def launch_command(host: Host, args: str = "") -> str:
    """Start ComfyUI in the foreground so its log streams back over SSH.

    The interpreter is discovered on the box rather than assumed: a ComfyUI
    install may carry a venv, the Windows portable bundle's embedded Python, or
    neither.

    **Loopback, everywhere.** This went back and forth and the answer depends
    entirely on how the tunnel forwards. `start-iap-tunnel` reaches a port on
    the instance's network interface, so loopback was unreachable and the box
    had to bind 0.0.0.0 behind two firewall rules. The tunnel is `ssh -L` now,
    which resolves the far address *on the box*, so loopback is exactly right:
    nothing is exposed on any interface, no firewall rule is involved, and the
    line ComfyUI prints — `To see the GUI go to http://127.0.0.1:8188` — is a
    true statement about the machine it is printed on.

    A box declared to have no GPU is started with `--cpu`. Without it ComfyUI
    looks for a CUDA device at startup and exits when there is none.

    `args` is the build's own part of the command line — `Build.launch_args`:
    `--enable-manager`, a frontend release or a frontend PR's directory.
    """
    listen = "127.0.0.1"
    cpu = _cpu_flag(host) + _extra(args)
    if is_windows(host):
        return (
            "powershell -NoProfile -NonInteractive -Command \""
            f"Set-Location '{WINDOWS_ROOT}'; "
            + _WINDOWS_GIT_ON_PATH
            + windows_python_search(
                "(Get-Command python -ErrorAction SilentlyContinue).Source")
            + f"if (-not $py) {{ Write-Output 'NO_PYTHON'; exit {NO_PYTHON_EXIT} }}; "
            + "Write-Output ('using ' + $py); "
            + f"& $py main.py --listen {listen} --port {COMFYUI_PORT}{cpu}\""
        )
    return (
        f"cd {LINUX_ROOT}; "
        + linux_python_search()
        + f"if [ -z \"$py\" ]; then echo NO_PYTHON; exit {NO_PYTHON_EXIT}; fi; "
        + "echo \"using $py\"; "
        + f"\"$py\" main.py --listen {listen} --port {COMFYUI_PORT}{cpu}"
    )


def _extra(args: str) -> str:
    """The build's launch arguments, on the END of the line, after `--cpu`.

    The end, for `_cpu_flag`'s reason: `alive_command` recognises a running
    ComfyUI by `main.py --listen`, so nothing may come between the two.
    """
    args = " ".join(str(args or "").split())
    return f" {args}" if args else ""


def _cpu_flag(host: Host) -> str:
    """` --cpu` for a box declared to have no GPU, and nothing otherwise.

    One place, because there are two launch commands and each is on two
    operating systems. It goes on the END of the command line: `alive_command`
    recognises a running ComfyUI by `main.py --listen`, and that has to stay
    the start of what follows `main.py`.
    """
    return " --cpu" if declares_no_gpu(host) else ""


def launch_detached_command(host: Host, args: str = "") -> str:
    """Start ComfyUI on the box and come straight back, with its log on the box.

    The same interpreter search as `launch_command`, and the same loopback bind
    for the same reason — the forward is an `ssh -L`, so `127.0.0.1` on the box
    is exactly where the tunnel arrives. Only two things differ, and both follow
    from nobody watching:

      * **Its output goes to a file on the box**, not down the SSH channel. A
        detached launch whose log went nowhere would trade a blocked terminal for
        a ComfyUI you cannot debug, which is the worse of the two.
      * **The file is truncated, not appended to.** `comfy-qat logs` is asked about
        *this* ComfyUI, and yesterday's traceback sitting above today's startup is
        how you spend twenty minutes fixing something that is already fixed.

    **Windows detaches through WMI**, `Win32_Process.Create` by way of
    `Invoke-CimMethod`, so that the process's parent is the WMI service and not
    the SSH session. It used `Start-Process`, and on a real windows-2022 box
    (live run 2026-11-03, live-s2/evidence/46-50) a process started that way
    did not outlive the SSH call that started it — it never reached its first
    line — while the same body started through WMI ran to its end a minute
    after its session had gone. `go` had printed STARTED and then found neither
    a ComfyUI nor a log. STARTED is now said only when WMI's ReturnValue is 0.

    What WMI starts is a PowerShell that redirects all of its own streams with
    `*>`, the one way to get stdout and stderr into one file. It is handed
    nothing of this session, its PATH included, so it puts Git's directory on
    its own PATH first, for Manager. Its command line is quoted with
    `[char]34` round the whole and `[char]39` round the paths: the outer
    command is already inside double quotes by the time it reaches the box, and
    a literal `"` here would end it. Rule 3 at the top of this file applies
    with full force — nothing below prompts, so nothing below can hang a
    non-interactive SSH command forever.

    Prints `using <interpreter>` and then STARTED. STARTED means the process was
    launched, never that it is serving: proving that is the caller's job, and
    conflating them is exactly the "a booted VM is up" mistake one level down.
    """
    listen = "127.0.0.1"
    cpu = _cpu_flag(host) + _extra(args)
    if is_windows(host):
        return (
            "powershell -NoProfile -NonInteractive -Command \""
            f"Set-Location '{WINDOWS_ROOT}'; "
            + _WINDOWS_GIT_ON_PATH
            + windows_python_search(
                "(Get-Command python -ErrorAction SilentlyContinue).Source")
            + f"if (-not $py) {{ Write-Output 'NO_PYTHON'; exit {NO_PYTHON_EXIT} }}; "
            + "Write-Output ('using ' + $py); "
            + "$q = [char]34; $s = [char]39; "
            + f"$inner = '$env:Path = ' + $s + '{WINDOWS_GIT_CMD};' + $s + "
            + "' + $env:Path; & ' + $s + $py + $s + "
            + f"' main.py --listen {listen} --port {COMFYUI_PORT}{cpu} *> ' + $s + "
            + f"'{WINDOWS_LOG}' + $s; "
            + "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
            + "-Arguments @{ CommandLine = ('powershell -NoProfile -NonInteractive "
            + "-Command ' + $q + $inner + $q); "
            + f"CurrentDirectory = '{WINDOWS_ROOT}' }}; "
            + "if ($r.ReturnValue -ne 0) { Write-Output ('could not start ComfyUI: "
            + "Win32_Process.Create returned ' + $r.ReturnValue); exit 1 }; "
            + f"Write-Output '{STARTED}'\""
        )
    return (
        f"cd {LINUX_ROOT}; "
        + linux_python_search()
        + f"if [ -z \"$py\" ]; then echo NO_PYTHON; exit {NO_PYTHON_EXIT}; fi; "
        + "echo \"using $py\"; "
        + f"nohup \"$py\" main.py --listen {listen} --port {COMFYUI_PORT}{cpu} "
        + f"> {LINUX_LOG} 2>&1 < /dev/null & "
        + f"echo {STARTED}"
    )


def alive_command(host: Host) -> str:
    """Is a ComfyUI still running on the box at all?

    Asked while waiting for a detached launch to answer, and only for the sake of
    the bad case: a ComfyUI that dies four seconds in is otherwise indistinguishable
    from one that is slow, so the wait runs to its full timeout on a machine that
    is billing the whole time.

    Wrong in the safe direction, deliberately. A false ALIVE costs the wait we
    would have had anyway; a false GONE would report a working box as broken. So
    Linux matches the actual command line, and Windows — where matching one is
    expensive — settles for "is any Python running", which is over-broad and never
    wrong in the direction that matters. The bracket in `[m]ain.py` keeps pgrep
    from matching the shell that carries this very command.
    """
    if is_windows(host):
        return (
            "powershell -NoProfile -NonInteractive -Command \""
            "$p = Get-Process -Name python, pythonw -ErrorAction SilentlyContinue; "
            f"if ($p) {{ Write-Output '{ALIVE}' }} else {{ Write-Output '{GONE}' }}\""
        )
    return (
        f"if pgrep -f '[m]ain.py --listen' >/dev/null 2>&1; then echo {ALIVE}; "
        f"else echo {GONE}; fi"
    )


def logs_command(host: Host, *, tail: int = 200, follow: bool = False) -> str:
    """Read the detached ComfyUI's log on the box — the last lines, or forever.

    Exits `NO_LOG_EXIT` and says nothing when the file is not there, so the
    caller can tell "ComfyUI has never been started here" from "the box would not
    answer". Printing a marker instead would put a word nobody asked for at the
    top of a log the tester is reading.

    Following is `tail -f` / `Get-Content -Wait`, which reads a file and touches
    nothing. Ending it stops reading and stops nothing else — which is the whole
    difference between this and `go --follow`, where Ctrl-C reaches ComfyUI.
    """
    # `max(0, ...)`, not `max(1, ...)`. Folding everything below one up to 1 made
    # `--tail 0` print a line the caller asked not to see, and made `--tail -5`
    # look like it had worked. Zero is a real answer here — `tail -n 0 -f` and
    # `Get-Content -Tail 0 -Wait` are both "no backlog, stream from now", which is
    # what you want while watching a generation start — so it is passed through
    # rather than rounded away. The floor stays at 0 because neither shell takes a
    # negative count; `logs_cmd` refuses those at the boundary with a message, so
    # nothing reaches here that this floor has to silently correct.
    lines = max(0, int(tail))
    if is_windows(host):
        wait = " -Wait" if follow else ""
        return (
            "powershell -NoProfile -NonInteractive -Command \""
            f"if (-not (Test-Path '{WINDOWS_LOG}')) {{ exit {NO_LOG_EXIT} }}; "
            f"Get-Content -Path '{WINDOWS_LOG}' -Tail {lines}{wait}\""
        )
    follow_flag = " -f" if follow else ""
    return (
        f"if [ ! -f {LINUX_LOG} ]; then exit {NO_LOG_EXIT}; fi; "
        f"tail -n {lines}{follow_flag} {LINUX_LOG}"
    )
