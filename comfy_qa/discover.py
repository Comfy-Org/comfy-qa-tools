"""Find the cloud boxes that already exist, so nobody types a zone.

Everything the host list needs — zone, machine type, card, operating system — is
already known to Google. Asking a person to copy it across by hand is how a host
list ends up wrong in a way that is invisible until a tunnel points somewhere
unexpected.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import COMFYUI_DEFAULT_PORT, Host

# Where discovered hosts start. 8188 belongs to the local ComfyUI, and 8189 is a
# common second local install, so the range begins clear of both.
FIRST_PORT = 8190

# Licence names are stable identifiers; the pretty name is for a person reading a
# table. Anything unrecognised falls back to the licence itself, which is ugly but
# true — better than guessing "Linux" and being wrong about the startup script.
_OS_NAMES = {
    "windows-server-2025-dc": "Windows Server 2025",
    "windows-server-2022-dc": "Windows Server 2022",
    "windows-server-2019-dc": "Windows Server 2019",
    "ubuntu-2404-lts": "Ubuntu 24.04",
    "ubuntu-2204-lts": "Ubuntu 22.04",
    "ubuntu-2004-lts": "Ubuntu 20.04",
    "debian-12": "Debian 12",
    "debian-11": "Debian 11",
    "rocky-linux-9": "Rocky Linux 9",
}


@dataclass(frozen=True)
class Discovered:
    """A cloud instance, described the way the host list wants it."""

    name: str
    os: str
    gpu: str
    gce_instance: str
    gce_zone: str
    gce_project: str
    running: bool

    @property
    def has_gpu(self) -> bool:
        return bool(self.gpu)


def _tail(value: str | None) -> str:
    """gcloud returns full resource URLs; only the last segment is meaningful."""
    return (value or "").rstrip("/").rsplit("/", 1)[-1]


def operating_system(disks: list[dict] | None) -> str:
    """Read the OS off the boot disk's licences."""
    for disk in disks or []:
        if disks and disk is not disks[0] and not disk.get("boot"):
            continue
        for licence in disk.get("licenses") or []:
            name = _tail(licence)
            if name in _OS_NAMES:
                return _OS_NAMES[name]
            if name:
                return name
    return "unknown"


def accelerator(instance: dict) -> str:
    """`.../nvidia-l4` -> `L4`. Empty when the box has no GPU at all."""
    for accel in instance.get("guestAccelerators") or []:
        name = _tail(accel.get("acceleratorType"))
        if not name:
            continue
        name = name.removeprefix("nvidia-").removeprefix("tesla-")
        return name.upper()
    return ""


def parse(instance: dict, project: str) -> Discovered:
    return Discovered(
        name=instance.get("name") or "",
        os=operating_system(instance.get("disks")),
        gpu=accelerator(instance),
        gce_instance=instance.get("name") or "",
        gce_zone=_tail(instance.get("zone")),
        gce_project=project,
        # TERMINATED is Google's word for stopped, which reads as broken. It is
        # not. And everything that is not TERMINATED is running or on its way
        # there — a box in STAGING read as "stopped" here, on the one command
        # whose job is telling you what exists in a project nothing recorded.
        running=(instance.get("status") != "TERMINATED"),
    )


def next_ports(existing: list[Host], count: int) -> list[int]:
    """Pick free local ports, never reusing one and never touching 8188."""
    taken = {host.port for host in existing} | {COMFYUI_DEFAULT_PORT}
    chosen: list[int] = []
    candidate = FIRST_PORT
    while len(chosen) < count:
        if candidate not in taken:
            chosen.append(candidate)
            taken.add(candidate)
        candidate += 1
    return chosen


def _unrecorded(found: list[Discovered], existing: list[Host]) -> list[Discovered]:
    """Discovered boxes with no host list entry naming their instance.

    Matching is on the GCE instance name, not the host's label, so renaming a host
    in your own file does not make it reappear as a duplicate.
    """
    known = {host.gce_instance for host in existing if host.gce_instance}
    return [box for box in found if box.gce_instance and box.gce_instance not in known]


def new_hosts(found: list[Discovered], existing: list[Host]) -> list[tuple[Discovered, int]]:
    """Which discovered boxes can be added to the host list, and what port each gets.

    Missing by instance is not enough, and the invitation above is why. Rename
    your entry for a box to `Comfy-Win` and point it at an instance you renamed by
    hand, and Google's `comfy-win` is genuinely unrecorded — so this returned it,
    and the block written for it is headed with GOOGLE's name, not yours.
    `config.parse` refuses two names that differ only in case, correctly, because
    which machine you reach would then depend on a shift key. The append landed a
    host list nothing could load, and `discover` said "added 1 host" and exited 0.

    So the label about to be written is compared against the labels already
    there, case-insensitively — the same fold the loader applies. Anything this
    returns is something the loader will accept beside what is already in the
    file. What it leaves out is reported by `label_clashes`, never dropped in
    silence: the box is on the project and is not in the host list, and a
    `discover` that says nothing about it looks like one that found nothing to do.
    """
    labelled = {host.name.lower() for host in existing}
    missing = [box for box in _unrecorded(found, existing)
               if box.name.lower() not in labelled]
    ports = next_ports(existing, len(missing))
    return list(zip(missing, ports))


def label_clashes(found: list[Discovered], existing: list[Host]) -> list[tuple[Discovered, str]]:
    """Boxes `new_hosts` had to leave out, each with the label already holding it."""
    labelled = {host.name.lower(): host.name for host in existing}
    return [(box, labelled[box.name.lower()])
            for box in _unrecorded(found, existing)
            if box.name.lower() in labelled]


def clash_note(box: Discovered, label: str) -> str:
    """Why a discovered box was left out, said the same way wherever it is said."""
    return (
        f"{box.name} was not added: your host list already calls a machine "
        f"{label!r}, and two names that differ only in case cannot both be "
        f"declared. Rename {label!r}, or point it at instance {box.gce_instance!r}."
    )


def to_toml(box: Discovered, port: int) -> str:
    """One [hosts.name] block, written to be read by a person."""
    return (
        f"\n[hosts.{box.name}]\n"
        f'kind         = "gce"\n'
        f'os           = "{box.os}"\n'
        f'gpu          = "{box.gpu or "none"}"\n'
        f'gce_instance = "{box.gce_instance}"\n'
        f'gce_zone     = "{box.gce_zone}"\n'
        f'gce_project  = "{box.gce_project}"\n'
        f"port         = {port}\n"
    )
