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

# A project-wide ceiling across every card, not a card you can pick. Worth showing
# in the full table — it can be the thing actually holding you back — but listing
# it among "cards you can run" reads as a GPU model that does not exist.
GLOBAL_ALLOWANCE = "any (global)"

# Real ids are hyphen-separated: `NVIDIA-L4-GPUS-per-project-region`. They were
# written here with underscores first, against invented fixtures, and every
# exclusion below silently failed as a result. The fixtures in the tests are now
# taken from a live project.
_MODEL_PREFIXES = ("PREEMPTIBLE", "COMMITTED", "RESERVED")

# A virtual-workstation allowance does not let you start an ordinary GPU instance
# either, so it belongs with the excluded models rather than in the offered list.
_EXCLUDED_PARTS = ("VWS",)

# Family-level quota, not a card. Real, but not something you pick by name.
_NOT_A_CARD = ("GPUS-PER-GPU-FAMILY",)

_SCOPE_SUFFIX = re.compile(r"-per-project(-region|-zone)?$", re.IGNORECASE)


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
    name = _SCOPE_SUFFIX.sub("", quota_id).upper().replace("_", "-")
    if name.startswith(_MODEL_PREFIXES):
        return None
    if any(part in name.split("-") for part in _EXCLUDED_PARTS):
        return None
    if name.startswith(_NOT_A_CARD):
        return None
    if name in ("GPUS-ALL-REGIONS", "GPUS-ALL-REGIONS-GPUS"):
        return GLOBAL_ALLOWANCE
    name = name.removeprefix("NVIDIA-").removesuffix("-GPUS").removesuffix("-GPU")
    return name.strip("-").strip() or None


def matches(gpu: str, quota_id: str) -> bool:
    """Does a user's `l4` name this quota? Case and separators are forgiven."""
    friendly = friendly_name(quota_id)
    if friendly is None:
        return False
    def flatten(value: str) -> str:
        return value.lower().replace("-", "").replace("_", "").replace(" ", "")

    return flatten(friendly) == flatten(gpu)


# Beyond this many applicable regions, listing them individually is noise: the
# same allowance applies everywhere and one row says so better than forty-three.
_MANY_REGIONS = 3


def _rows(quota: dict) -> Iterable[tuple[str, int, list[str]]]:
    """(where, limit, regions) for each place a quota applies.

    The live API puts the places in `applicableLocations` and leaves the per-entry
    `dimensions` null, so reading a region out of `dimensions` finds nothing. A
    single allowance covering forty-three regions is one row, not forty-three.
    """
    for info in quota.get("dimensionsInfos") or []:
        dimensions = info.get("dimensions") or {}
        locations = info.get("applicableLocations") or []

        explicit = dimensions.get("region") or dimensions.get("zone")
        if explicit:
            where = explicit
        elif len(locations) == 1:
            where = locations[0]
        elif locations:
            where = "all regions"
        else:
            where = "global"

        raw = (info.get("details") or {}).get("value")
        try:
            limit = int(raw)
        except (TypeError, ValueError):
            # An absent value means the project has no explicit grant, which is
            # zero for our purposes — not a parse failure worth crashing over.
            limit = 0
        yield where, limit, locations


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
        for where, limit, locations in _rows(quota):
            if region and where not in (region, "global", "all regions") and region not in locations:
                continue
            if region and where == "all regions":
                where = region
            if limit > 0:
                status: Status = "ready"
            elif quota_id in pending:
                status = "pending"
            else:
                status = "none"
            rows.append(Readiness(gpu, where, limit, status, quota_id))

    # Most cards are metered twice — once per region and once per zone — and both
    # ids carry the same friendly name. Showing "L4  us-central1  1  ready" twice
    # reads as a bug, so collapse to one row per card and place, keeping the
    # larger grant.
    best: dict[tuple[str, str], Readiness] = {}
    for row in rows:
        key = (row.gpu, row.region)
        existing = best.get(key)
        if existing is None or row.limit > existing.limit:
            best[key] = row

    order = {"ready": 0, "pending": 1, "none": 2}
    return sorted(best.values(), key=lambda r: (order[r.status], r.gpu, r.region))


def resolve(gpu: str, quotas: list[dict], *, region: str | None = None) -> str | None:
    """Turn `l4` into the quota id to request. None if this project reports none."""
    for quota in quotas:
        quota_id = quota.get("quotaId") or ""
        if not matches(gpu, quota_id):
            continue
        if region and not any(
            where in (region, "all regions", "global") or region in locations
            for where, _, locations in _rows(quota)
        ):
            continue
        return quota_id
    return None


def available_gpus(quotas: list[dict]) -> list[str]:
    """Every card name this project could ask for, for error messages."""
    names = {friendly_name(q.get("quotaId") or "") for q in quotas}
    return sorted(n for n in names if n)
