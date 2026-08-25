"""GPU quota, in the terms a person thinks in.

Google meters GPUs per card type per region, under ids like
`NVIDIA_L4_GPUS-per-project-region`. Nobody thinks in those. This module turns
them into "L4 in us-central1: ready", and turns "l4" back into an id when asking
for more.

Quota gates the *card*, never the operating system. Once a card is approved you
can build either Windows or Linux on it, so OS never appears here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal

Status = Literal["ready", "pending", "none"]

# Ids carry a prefix for the billing model. Plain on-demand quota is the one you
# want; the others are separate allowances that will not help you start a box.
_MODEL_PREFIXES = ("PREEMPTIBLE_", "COMMITTED_", "RESERVED_")

_SCOPE_SUFFIX = re.compile(r"-per-project(-region|-zone)?$")


@dataclass(frozen=True)
class Readiness:
    """One card in one place, and whether you can use it today."""

    gpu: str
    region: str
    limit: int
    status: Status
    quota_id: str

    @property
    def usable(self) -> bool:
        return self.status == "ready"


def friendly_name(quota_id: str) -> str | None:
    """`NVIDIA_L4_GPUS-per-project-region` -> `L4`. None if it is not a GPU quota.

    Preemptible and committed allowances are deliberately excluded: they are real
    quotas, but having one does not let you start an ordinary instance, and
    listing them as available would be a lie.
    """
    if "GPU" not in quota_id.upper():
        return None
    name = _SCOPE_SUFFIX.sub("", quota_id).upper()
    if name.startswith(_MODEL_PREFIXES):
        return None
    if name in ("GPUS_ALL_REGIONS", "GPUS_ALL_REGIONS_GPUS"):
        return "any (global)"
    name = name.removeprefix("NVIDIA_").removesuffix("_GPUS").removesuffix("_GPU")
    return name.replace("_", " ").strip() or None


def matches(gpu: str, quota_id: str) -> bool:
    """Does a user's `l4` name this quota? Case and separators are forgiven."""
    friendly = friendly_name(quota_id)
    if friendly is None:
        return False
    wanted = gpu.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    return friendly.lower().replace("-", "").replace("_", "").replace(" ", "") == wanted


def _rows(quota: dict) -> Iterable[tuple[str, int]]:
    """(region, limit) for each place a quota applies. Shapes vary; be forgiving."""
    for info in quota.get("dimensionsInfos") or []:
        dimensions = info.get("dimensions") or {}
        region = dimensions.get("region") or dimensions.get("zone") or "global"
        raw = (info.get("details") or {}).get("value")
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            limit = 0
        yield region, limit


def _pending_ids(preferences: list[dict]) -> set[str]:
    """Quota ids with a request asking for more than is currently granted."""
    pending = set()
    for pref in preferences or []:
        quota_id = pref.get("quotaId")
        if not quota_id:
            continue
        wanted = (pref.get("quotaConfig") or {}).get("preferredValue")
        try:
            if int(wanted) > 0:
                pending.add(quota_id)
        except (TypeError, ValueError):
            pending.add(quota_id)
    return pending


def readiness(
    quotas: list[dict], preferences: list[dict] | None = None, *, region: str | None = None,
) -> list[Readiness]:
    """What can I run today, what is waiting on Google, and what did I never ask for."""
    pending = _pending_ids(preferences or [])
    rows: list[Readiness] = []

    for quota in quotas:
        quota_id = quota.get("quotaId") or ""
        gpu = friendly_name(quota_id)
        if gpu is None:
            continue
        for where, limit in _rows(quota):
            if region and where not in (region, "global"):
                continue
            if limit > 0:
                status: Status = "ready"
            elif quota_id in pending:
                status = "pending"
            else:
                status = "none"
            rows.append(Readiness(gpu, where, limit, status, quota_id))

    order = {"ready": 0, "pending": 1, "none": 2}
    rows.sort(key=lambda r: (order[r.status], r.gpu, r.region))
    return rows


def resolve(gpu: str, quotas: list[dict], *, region: str | None = None) -> str | None:
    """Turn `l4` into the quota id to request. None if this project reports none."""
    for quota in quotas:
        quota_id = quota.get("quotaId") or ""
        if not matches(gpu, quota_id):
            continue
        if region and not any(where == region for where, _ in _rows(quota)):
            continue
        return quota_id
    return None


def available_gpus(quotas: list[dict]) -> list[str]:
    """Every card name this project could ask for, for error messages."""
    names = {friendly_name(q.get("quotaId") or "") for q in quotas}
    return sorted(n for n in names if n)
