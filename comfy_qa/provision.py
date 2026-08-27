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

from .config import Host
from .tunnel import COMFYUI_PORT

# Where ComfyUI lives on each kind of box. Windows matches the convention already
# used on the other machines here.
WINDOWS_ROOT = r"C:\ComfyUI"
LINUX_ROOT = "/opt/comfyui"

# Custom nodes still lack wheels for 3.13+, so the interpreter is pinned.
PYTHON_SERIES = "3.12"

# The launch script's own word for "nothing here can run ComfyUI". Reserved, so
# the caller can recognise it rather than reporting a generic non-zero exit.
NO_PYTHON_EXIT = 3


def is_windows(host: Host) -> bool:
    return "windows" in (host.os or "").lower()


def root_for(host: Host) -> str:
    return WINDOWS_ROOT if is_windows(host) else LINUX_ROOT


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
# index. On Linux the PyPI wheel already carries CUDA, so no index is needed.
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"


def torch_install(python: str, host: Host) -> str:
    """Install torch so that it can see the card the box was rented for."""
    if is_windows(host):
        return (f"{python} -m pip install torch torchvision torchaudio "
                f"--index-url {TORCH_INDEX}")
    return f"{python} -m pip install torch torchvision torchaudio"


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


def repair_command(host: Host) -> str:
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
    """
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
            f"& $py -m pip install torch torchvision torchaudio --index-url {TORCH_INDEX}; "
            "Write-Output 'installing the rest of the requirements'; "
            "& $py -m pip install -r requirements.txt\""
        )
    return (
        f"cd {LINUX_ROOT} && "
        "if [ -x ./venv/bin/python ]; then PY=./venv/bin/python; else PY=python3; fi && "
        "$PY -m pip install torch torchvision torchaudio && "
        "$PY -m pip install -r requirements.txt"
    )


def install_command(host: Host) -> str:
    """Set ComfyUI up from nothing, printing progress as it goes."""
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
            "--index-url https://download.pytorch.org/whl/cu128; "
            ".\\venv\\Scripts\\python.exe -m pip install -r requirements.txt; "
            f"if (-not (Test-Path '{WINDOWS_ROOT}\\main.py')) "
            "{ Write-Output 'INSTALL_INCOMPLETE'; exit 1 }; "
            "Write-Output 'install complete'\""
        )
    return (
        "set -e; "
        f"echo 'cloning ComfyUI into {LINUX_ROOT}'; "
        f"sudo mkdir -p {LINUX_ROOT} && sudo chown \"$USER\" {LINUX_ROOT}; "
        f"git clone https://github.com/comfyanonymous/ComfyUI.git {LINUX_ROOT} || true; "
        f"cd {LINUX_ROOT}; "
        f"python{PYTHON_SERIES} -m venv venv || python3 -m venv venv; "
        "echo 'installing torch (this is the slow part)'; "
        "./venv/bin/python -m pip install --upgrade pip; "
        "./venv/bin/python -m pip install torch torchvision torchaudio; "
        "./venv/bin/python -m pip install -r requirements.txt; "
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
    neither. It binds to 127.0.0.1 — the tunnel is the only way in, because
    ComfyUI has no authentication.
    """
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
            f"& $py main.py --listen 127.0.0.1 --port {COMFYUI_PORT}\""
        )
    candidates = " ".join(f"{LINUX_ROOT}/{name}" for name in LINUX_PYTHONS)
    return (
        f"cd {LINUX_ROOT}; "
        f"for p in {candidates} $(command -v python3); do "
        "  if [ -x \"$p\" ]; then py=\"$p\"; break; fi; done; "
        f"if [ -z \"$py\" ]; then echo NO_PYTHON; exit {NO_PYTHON_EXIT}; fi; "
        "echo \"using $py\"; "
        f"\"$py\" main.py --listen 127.0.0.1 --port {COMFYUI_PORT}"
    )
