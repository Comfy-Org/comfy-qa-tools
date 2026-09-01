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
     command forever. Nothing here prompts, and PowerShell runs -NonInteractive.

Because 1 rules out `-ErrorActionPreference Stop`, PowerShell carries on after a
failed step, and an install whose clone failed still ends by printing "install
complete" and exiting 0. So each script checks its own work: the clone is
verified before anything is installed into it, and main.py is verified before
success is claimed. Exit codes are the only thing the caller can trust.
"""

from __future__ import annotations

import re

from .config import Host
from .tunnel import COMFYUI_PORT

# Where ComfyUI lives on each kind of box. Windows matches the convention already
# used on the other machines here.
WINDOWS_ROOT = r"C:\ComfyUI"
LINUX_ROOT = "/opt/comfyui"

# Custom nodes still lack wheels for 3.13+, so the interpreter is pinned.
PYTHON_SERIES = "3.12"

# Linux cannot pin it the way Windows can. Windows installs the interpreter it
# wants (winget carries every series); a Linux image ships exactly one python3
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


def is_windows(host: Host) -> bool:
    return "windows" in (host.os or "").lower()


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
    """Print INSTALLED or MISSING. Nothing else, so the caller can branch on it."""
    if is_windows(host):
        return (
            "powershell -NonInteractive -Command "
            f"\"if (Test-Path '{WINDOWS_ROOT}\\main.py') "
            "{ Write-Output 'INSTALLED' } else { Write-Output 'MISSING' }\""
        )
    return (
        f"if [ -f {LINUX_ROOT}/main.py ]; then echo INSTALLED; else echo MISSING; fi"
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
            "powershell -NonInteractive -Command \""
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


def torch_install(python: str, host: Host, index: str | None = None) -> str:
    """Install torch so that it can see the card the box was rented for."""
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
    """
    if is_windows(host):
        return (
            "powershell -NonInteractive -Command \""
            f"if (-not (Test-Path '{WINDOWS_ROOT}\\main.py')) "
            f"{{ Write-Output '{NO_COMFYUI}'; exit 0 }}; "
            f"Set-Location '{WINDOWS_ROOT}'; "
            "$py = if (Test-Path '.\\venv\\Scripts\\python.exe') "
            "{ '.\\venv\\Scripts\\python.exe' } "
            "elseif (Test-Path '.\\python_embeded\\python.exe') "
            "{ '.\\python_embeded\\python.exe' } else { 'python' }; "
            "$v = (& $py -m pip show torch 2>$null | Select-String '^Version:'); "
            f"if (-not $v) {{ Write-Output '{NO_TORCH}'; exit 0 }}; "
            f"if ($v -match '\\+cu') {{ Write-Output '{READY}' }} "
            f"else {{ Write-Output '{TORCH_NO_CUDA}' }}\""
        )
    return (
        f"if [ ! -f {LINUX_ROOT}/main.py ]; then echo {NO_COMFYUI}; exit 0; fi; "
        f"cd {LINUX_ROOT}; "
        "if [ -x ./venv/bin/python ]; then PY=./venv/bin/python; else PY=python3; fi; "
        f"$PY -c \"import torch, sys; "
        f"sys.stdout.write('{READY}' if torch.cuda.is_available() else '{TORCH_NO_CUDA}')\" "
        f"2>/dev/null || echo {NO_TORCH}"
    )


# What `port_holder_command` prints when nothing is listening on ComfyUI's port.
PORT_FREE = "PORT_FREE"


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
            "powershell -NonInteractive -Command \""
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
            "powershell -NonInteractive -Command \""
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
        return ("powershell -NonInteractive -Command "
                f"\"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue\"")
    return f"kill {pid} 2>/dev/null || true"


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
    if is_windows(host):
        python = (
            "$py = if (Test-Path '.\\venv\\Scripts\\python.exe') "
            "{ '.\\venv\\Scripts\\python.exe' } "
            "elseif (Test-Path '.\\python_embeded\\python.exe') "
            "{ '.\\python_embeded\\python.exe' } else { 'python' }; "
        )
        return (
            "powershell -NonInteractive -Command \""
            f"Set-Location '{WINDOWS_ROOT}'; "
            + python
            + "Write-Output 'installing torch for this GPU (the slow part)'; "
            f"& $py -m pip install {force}torch torchvision torchaudio "
            f"--index-url {index}; "
            "Write-Output 'installing the rest of the requirements'; "
            "& $py -m pip install -r requirements.txt\""
        )
    return (
        f"cd {LINUX_ROOT} && "
        "if [ -x ./venv/bin/python ]; then PY=./venv/bin/python; else PY=python3; fi && "
        f"$PY -m pip install {force}torch torchvision torchaudio && "
        "$PY -m pip install -r requirements.txt"
    )


def install_command(host: Host, index: str | None = None) -> str:
    """Set ComfyUI up from nothing, printing progress as it goes.

    `index` is the PyTorch index this box's driver supports; the caller asks the
    box with `cuda_command` and passes the answer. Without one the documented
    fallback is used, which installs and runs — just not always as fast as the
    card could.
    """
    if is_windows(host):
        # winget is present on Server 2022 images; git and python come from there.
        return (
            "powershell -NonInteractive -Command \""
            "Write-Output 'installing prerequisites'; "
            "winget install --id Git.Git -e --silent "
            "--accept-source-agreements --accept-package-agreements; "
            f"winget install --id Python.Python.{PYTHON_SERIES} -e --silent "
            "--accept-source-agreements --accept-package-agreements; "
            f"Write-Output 'cloning ComfyUI into {WINDOWS_ROOT}'; "
            f"git clone https://github.com/comfyanonymous/ComfyUI.git '{WINDOWS_ROOT}'; "
            # Without this the next line fails quietly and everything after it
            # installs into whatever directory PowerShell happened to be in.
            f"if (-not (Test-Path '{WINDOWS_ROOT}\\main.py')) "
            "{ Write-Output 'INSTALL_INCOMPLETE: the clone did not produce main.py'; "
            "exit 1 }; "
            f"Set-Location '{WINDOWS_ROOT}'; "
            f"py -{PYTHON_SERIES} -m venv venv; "
            "Write-Output 'installing torch (this is the slow part)'; "
            ".\\venv\\Scripts\\python.exe -m pip install --upgrade pip; "
            ".\\venv\\Scripts\\python.exe -m pip install torch torchvision torchaudio "
            f"--index-url {index or TORCH_INDEX}; "
            ".\\venv\\Scripts\\python.exe -m pip install -r requirements.txt; "
            f"if (-not (Test-Path '{WINDOWS_ROOT}\\main.py')) "
            "{ Write-Output 'INSTALL_INCOMPLETE'; exit 1 }; "
            "Write-Output 'install complete'\""
        )
    apt = f"sudo apt-get -o DPkg::Lock::Timeout={APT_LOCK_WAIT} -y -qq"
    series = " ".join(PYTHON_SERIES_SUPPORTED)
    return (
        "set -e; "
        "echo 'installing prerequisites'; "
        f"{apt} update; "
        f"{apt} install git; "
        # The interpreter is discovered, not assumed. `python3` is deliberately
        # last: on 24.04 it is 3.12 and fine, on 22.04 it is 3.10 and also fine,
        # but on an image that has moved to 3.13 it is the one answer that must
        # not win, so it is only reached when no supported series is installed.
        f'PY=""; for v in {series}; do '
        'if command -v "python$v" >/dev/null 2>&1; then PY="python$v"; break; fi; '
        "done; "
        f'if [ -z "$PY" ]; then for v in {series}; do '
        f'if {apt} install "python$v-venv" >/dev/null 2>&1; '
        'then PY="python$v"; break; fi; done; fi; '
        'if [ -z "$PY" ]; then '
        "echo 'INSTALL_INCOMPLETE: no supported python (3.10-3.12) on this image'; "
        "exit 1; fi; "
        # Debian and Ubuntu ship venv as a separate package, so a present
        # interpreter is not a usable one. Installing it is cheap and idempotent.
        f'{apt} install "$PY-venv"; '
        'echo "building with $PY"; '
        f"echo 'cloning ComfyUI into {LINUX_ROOT}'; "
        f"sudo mkdir -p {LINUX_ROOT} && sudo chown \"$USER\" {LINUX_ROOT}; "
        f"git clone https://github.com/comfyanonymous/ComfyUI.git {LINUX_ROOT} || true; "
        f"cd {LINUX_ROOT}; "
        '"$PY" -m venv venv; '
        # Without this the pip lines below run against a half-made venv and the
        # error surfaces a hundred lines later as a missing module.
        "if [ ! -x ./venv/bin/python ]; then "
        "echo 'INSTALL_INCOMPLETE: the venv was not created'; exit 1; fi; "
        "echo 'installing torch (this is the slow part)'; "
        "./venv/bin/python -m pip install --upgrade pip; "
        "./venv/bin/python -m pip install torch torchvision torchaudio"
        + (f" --index-url {index}; " if index else "; ")
        + "./venv/bin/python -m pip install -r requirements.txt; "
        f"if [ ! -f {LINUX_ROOT}/main.py ]; then echo INSTALL_INCOMPLETE; exit 1; fi; "
        "echo 'install complete'"
    )


# An install can be a venv, the Windows portable bundle, or a system interpreter.
# Assuming one of them is how a working box reported "python.exe is not recognized".
WINDOWS_PYTHONS = (
    r"venv\Scripts\python.exe",
    r"python_embeded\python.exe",
    r".venv\Scripts\python.exe",
    r"ComfyUI_windows_portable\python_embeded\python.exe",
)
LINUX_PYTHONS = ("venv/bin/python", ".venv/bin/python")


def launch_command(host: Host) -> str:
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
    """
    listen = "127.0.0.1"
    if is_windows(host):
        candidates = "; ".join(
            f"'{WINDOWS_ROOT}\\{name}'" for name in WINDOWS_PYTHONS
        )
        return (
            "powershell -NonInteractive -Command \""
            f"Set-Location '{WINDOWS_ROOT}'; "
            f"$candidates = @({candidates.replace('; ', ', ')}); "
            "$py = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1; "
            "if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }; "
            f"if (-not $py) {{ Write-Output 'NO_PYTHON'; exit {NO_PYTHON_EXIT} }}; "
            "Write-Output ('using ' + $py); "
            f"& $py main.py --listen {listen} --port {COMFYUI_PORT}\""
        )
    candidates = " ".join(f"{LINUX_ROOT}/{name}" for name in LINUX_PYTHONS)
    return (
        f"cd {LINUX_ROOT}; "
        f"for p in {candidates} $(command -v python3); do "
        "  if [ -x \"$p\" ]; then py=\"$p\"; break; fi; done; "
        f"if [ -z \"$py\" ]; then echo NO_PYTHON; exit {NO_PYTHON_EXIT}; fi; "
        "echo \"using $py\"; "
        f"\"$py\" main.py --listen {listen} --port {COMFYUI_PORT}"
    )


def launch_detached_command(host: Host) -> str:
    """Start ComfyUI on the box and come straight back, with its log on the box.

    The same interpreter search as `launch_command`, and the same loopback bind
    for the same reason — the forward is an `ssh -L`, so `127.0.0.1` on the box
    is exactly where the tunnel arrives. Only two things differ, and both follow
    from nobody watching:

      * **Its output goes to a file on the box**, not down the SSH channel. A
        detached launch whose log went nowhere would trade a blocked terminal for
        a ComfyUI you cannot debug, which is the worse of the two.
      * **The file is truncated, not appended to.** `host logs` is asked about
        *this* ComfyUI, and yesterday's traceback sitting above today's startup is
        how you spend twenty minutes fixing something that is already fixed.

    Windows detaches with `Start-Process`, which cannot merge stdout and stderr
    into one file — it refuses the same path twice. So it starts a hidden
    PowerShell that redirects all of its own streams with `*>`, which can. That
    nesting is also why the inner command quotes the interpreter with
    `[char]34`: the outer command is already inside double quotes by the time it
    reaches the box, and a literal `"` here would end it. Rule 3 at the top of
    this file applies with full force — nothing below prompts, so nothing below
    can hang a non-interactive SSH command forever.

    Prints `using <interpreter>` and then STARTED. STARTED means the process was
    launched, never that it is serving: proving that is the caller's job, and
    conflating them is exactly the "a booted VM is up" mistake one level down.
    """
    listen = "127.0.0.1"
    if is_windows(host):
        candidates = ", ".join(f"'{WINDOWS_ROOT}\\{name}'" for name in WINDOWS_PYTHONS)
        return (
            "powershell -NonInteractive -Command \""
            f"Set-Location '{WINDOWS_ROOT}'; "
            f"$candidates = @({candidates}); "
            "$py = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1; "
            "if (-not $py) { $py = (Get-Command python -ErrorAction SilentlyContinue).Source }; "
            f"if (-not $py) {{ Write-Output 'NO_PYTHON'; exit {NO_PYTHON_EXIT} }}; "
            "Write-Output ('using ' + $py); "
            "$q = [char]34; "
            "$inner = '& ' + $q + $py + $q + "
            f"' main.py --listen {listen} --port {COMFYUI_PORT} *> ' + $q + "
            f"'{WINDOWS_LOG}' + $q; "
            "Start-Process -FilePath 'powershell' "
            "-ArgumentList '-NonInteractive', '-Command', $inner "
            f"-WorkingDirectory '{WINDOWS_ROOT}' -WindowStyle Hidden; "
            f"Write-Output '{STARTED}'\""
        )
    candidates = " ".join(f"{LINUX_ROOT}/{name}" for name in LINUX_PYTHONS)
    return (
        f"cd {LINUX_ROOT}; "
        f"for p in {candidates} $(command -v python3); do "
        "  if [ -x \"$p\" ]; then py=\"$p\"; break; fi; done; "
        f"if [ -z \"$py\" ]; then echo NO_PYTHON; exit {NO_PYTHON_EXIT}; fi; "
        "echo \"using $py\"; "
        f"nohup \"$py\" main.py --listen {listen} --port {COMFYUI_PORT} "
        f"> {LINUX_LOG} 2>&1 < /dev/null & "
        f"echo {STARTED}"
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
            "powershell -NonInteractive -Command \""
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
    lines = max(1, int(tail))
    if is_windows(host):
        wait = " -Wait" if follow else ""
        return (
            "powershell -NonInteractive -Command \""
            f"if (-not (Test-Path '{WINDOWS_LOG}')) {{ exit {NO_LOG_EXIT} }}; "
            f"Get-Content -Path '{WINDOWS_LOG}' -Tail {lines}{wait}\""
        )
    follow_flag = " -f" if follow else ""
    return (
        f"if [ ! -f {LINUX_LOG} ]; then exit {NO_LOG_EXIT}; fi; "
        f"tail -n {lines}{follow_flag} {LINUX_LOG}"
    )
