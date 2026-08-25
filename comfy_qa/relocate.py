"""Moving a box to a zone that actually has the card free.

A GPU stockout is not a fault you can fix where you are: the zone has none of
that machine, and no amount of retrying changes it. The way out is to be
somewhere else — which, done by hand, is a snapshot, a disk, an instance and a
config edit. That is four chances to get it wrong, so it is one command.

The existing disk is snapshotted rather than rebuilt, because the ComfyUI install
lives on it and reinstalling on Windows is the slow, fragile path.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Host


@dataclass(frozen=True)
class Plan:
    """What moving this box will do, before anything is done."""

    host: Host
    to_zone: str
    new_instance: str
    new_disk: str
    snapshot: str
    machine_type: str

    def steps(self) -> list[str]:
        return [
            f"snapshot the boot disk of {self.host.gce_instance} ({self.host.gce_zone})",
            f"create disk {self.new_disk} in {self.to_zone} from that snapshot",
            f"create {self.new_instance} in {self.to_zone} ({self.machine_type})",
            f"add {self.new_instance} to your host list",
            f"leave {self.host.gce_instance} stopped in {self.host.gce_zone}",
        ]


def _tail(value: str | None) -> str:
    return (value or "").rstrip("/").rsplit("/", 1)[-1]


def boot_disk(instance: dict) -> str | None:
    """The disk the machine boots from — the one carrying the install."""
    disks = instance.get("disks") or []
    for disk in disks:
        if disk.get("boot"):
            return _tail(disk.get("source")) or disk.get("deviceName")
    if disks:
        return _tail(disks[0].get("source")) or disks[0].get("deviceName")
    return None


def machine_type(instance: dict) -> str:
    return _tail(instance.get("machineType")) or "g2-standard-8"


def metadata_pairs(instance: dict) -> str | None:
    """Carry across the metadata that matters, notably Windows SSH.

    Losing `enable-windows-ssh` would leave the moved box unreachable by every
    command this tool has, which would look like the move failing.
    """
    keep = []
    for item in (instance.get("metadata") or {}).get("items") or []:
        key = item.get("key")
        if key in ("enable-windows-ssh", "enable-oslogin"):
            keep.append(f"{key}={item.get('value')}")
    return ",".join(keep) or None


def suffix_for(zone: str) -> str:
    """`us-central1-b` -> `b`, so a moved box reads as where it went."""
    return zone.rsplit("-", 1)[-1] or zone


def plan_move(host: Host, instance: dict, to_zone: str) -> Plan:
    disk = boot_disk(instance) or host.gce_instance
    tag = suffix_for(to_zone)
    return Plan(
        host=host,
        to_zone=to_zone,
        new_instance=f"{host.gce_instance}-{tag}",
        new_disk=f"{disk}-{tag}",
        snapshot=f"{disk}-move-{tag}",
        machine_type=machine_type(instance),
    )
