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
"""

from __future__ import annotations

from .config import Host

# Where ComfyUI lives on each kind of box. Windows matches the convention already
# used on the other machines here.
WINDOWS_ROOT = r"C:\ComfyUI"
LINUX_ROOT = "/opt/comfyui"

# Custom nodes still lack wheels for 3.13+, so the interpreter is pinned.
PYTHON_SERIES = "3.12"


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
            f"Set-Location '{WINDOWS_ROOT}'; "
            f"py -{PYTHON_SERIES} -m venv venv; "
            "Write-Output 'installing torch (this is the slow part)'; "
            ".\\venv\\Scripts\\python.exe -m pip install --upgrade pip; "
            ".\\venv\\Scripts\\python.exe -m pip install torch torchvision torchaudio "
            "--index-url https://download.pytorch.org/whl/cu128; "
            ".\\venv\\Scripts\\python.exe -m pip install -r requirements.txt; "
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
            "if (-not $py) { Write-Output 'NO_PYTHON'; exit 3 }; "
            "Write-Output ('using ' + $py); "
            "& $py main.py --listen 127.0.0.1 --port 8188\""
        )
    candidates = " ".join(f"{LINUX_ROOT}/{name}" for name in LINUX_PYTHONS)
    return (
        f"cd {LINUX_ROOT}; "
        f"for p in {candidates} $(command -v python3); do "
        "  if [ -x \"$p\" ]; then py=\"$p\"; break; fi; done; "
        "if [ -z \"$py\" ]; then echo NO_PYTHON; exit 3; fi; "
        "echo \"using $py\"; "
        "\"$py\" main.py --listen 127.0.0.1 --port 8188"
    )
