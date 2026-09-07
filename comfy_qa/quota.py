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


def _applies(where: str, locations: list[str], region: str | None) -> bool:
    """Does a quota row cover `region`?

    `all regions` is this module's label for "several places at once", not a
    promise about every place. Reading it as "everywhere" meant
    `quota list --region europe-west4` reported a grant the project does not
    have there — it relabelled an all-regions row as the region asked about —
    and `resolve` handed that region to a quota request Google would refuse.
    A row covers a region when it names it, or when it is genuinely global.
    """
    if not region:
        return True
    if where in ("global", region):
        return True
    if "global" in locations:
        return True
    return region in locations


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
            if not _applies(where, locations, region):
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


@dataclass(frozen=True)
class CardSummary:
    """One card, one line. What you can run, and roughly where."""

    gpu: str
    limit: int
    status: Status
    where: str

    @property
    def usable(self) -> bool:
        return self.status == "ready"


def summarise(rows: list[Readiness]) -> list[CardSummary]:
    """Collapse per-region rows into one line per card.

    Google meters some cards per region individually — K80 comes back as 25
    separate entries — so a row-per-region table ran to 130 lines and told you
    nothing a single line could not. Region detail is available by asking for a
    region; the default answers "what can I run".
    """
    grouped: dict[str, list[Readiness]] = {}
    for row in rows:
        grouped.setdefault(row.gpu, []).append(row)

    summaries: list[CardSummary] = []
    for gpu, entries in grouped.items():
        best = max(entry.limit for entry in entries)
        if any(entry.status == "ready" for entry in entries):
            status: Status = "ready"
        elif any(entry.status == "pending" for entry in entries):
            status = "pending"
        else:
            status = "none"

        relevant = [e for e in entries if e.status == status]
        places = {e.region for e in relevant}
        if places == {"global"}:
            # The project-wide allowance is genuinely global, not "every region".
            where = "global"
        elif {"all regions", "global"} & places:
            where = "all regions"
        elif len(places) == 1:
            where = next(iter(places))
        else:
            where = f"{len(places)} regions"

        summaries.append(CardSummary(gpu=gpu, limit=best, status=status, where=where))

    order = {"ready": 0, "pending": 1, "none": 2}
    return sorted(summaries, key=lambda s: (order[s.status], s.gpu))


def resolve(gpu: str, quotas: list[dict], *, region: str | None = None) -> str | None:
    """Turn `l4` into the quota id to request. None if this project reports none."""
    for quota in quotas:
        quota_id = quota.get("quotaId") or ""
        if not matches(gpu, quota_id):
            continue
        if region and not any(
            _applies(where, locations, region) for where, _, locations in _rows(quota)
        ):
            continue
        return quota_id
    return None


def available_gpus(quotas: list[dict]) -> list[str]:
    """Every card name this project could ask for, for error messages."""
    names = {friendly_name(q.get("quotaId") or "") for q in quotas}
    return sorted(n for n in names if n)


# --- where can this project actually start one -----------------------------
#
# `readiness` answers "what can I run", one row per card and place, for a person
# reading a table. Choosing a zone needs the other shape: the bare set of region
# names, with the per-region grant preferred over the per-zone one. Doing that on
# `readiness` output would mean parsing back out of "all regions", which is a
# label, not a place.

_REGION_SCOPED = "-per-project-region"
_ZONE_SCOPED = "-per-project-zone"

# Google reports "no explicit limit" as -1, not as a missing value. Live proof,
# read off this project on 2026-08-28: `NVIDIA-L4-GPUS-per-project-region` is 1
# across 43 regions, while `NVIDIA-L4-GPUS-per-project-zone` is **-1** across the
# 130 zones inside them. Reading -1 as "none" would drop every zone-scoped grant;
# reading it as a number would rank an unlimited allowance below a limit of 1.
UNLIMITED = -1


def _region_name(location: str) -> str:
    """`us-central1-a` -> `us-central1`. A region is returned unchanged.

    Zone-scoped quota lists zones; region-scoped quota lists regions. Both end up
    in the same set, so both are reduced to the region, which is the unit a
    grant is actually made in.
    """
    head, _, tail = location.rpartition("-")
    return head if head and len(tail) == 1 and tail.isalpha() else location


def _granted_regions(quota: dict) -> set[str]:
    """Every region one quota record grants a non-zero allowance in."""
    granted: set[str] = set()
    for where, limit, locations in _rows(quota):
        if limit == 0:
            continue
        places = locations or ([where] if where not in ("global", "all regions") else [])
        for place in places:
            if place and place != "global":
                granted.add(_region_name(place))
    return granted


def _binding(gpu: str, quotas: list[dict]) -> list[dict]:
    """The quota records that decide whether a card can start, and no others.

    A project routinely carries both scopes for the same card, and they disagree:
    this one has L4 at **1** across 43 named regions
    (`NVIDIA-L4-GPUS-per-project-region`) and an **unlimited** per-zone allowance
    across the 130 zones inside them (`...-per-project-zone`, value -1). Read
    together, the card looks unlimited and available in a region the project has
    no grant in. The region-scoped record is the one that binds, so where there
    is one it is the only one read.
    """
    return _prefer_region_scope(
        [q for q in quotas if matches(gpu, q.get("quotaId") or "")])


def _prefer_region_scope(records: list[dict]) -> list[dict]:
    """Drop the zone-scoped copies when a region-scoped record exists.

    Written once because both the per-card path and the project-wide ceiling
    need it and only one of them had it. A project carries both scopes of the
    same quota, `friendly_name` maps them to the same label, and the zone-scoped
    copy is the one that says -1.
    """
    region_scoped = [r for r in records
                     if not (r.get("quotaId") or "").lower().endswith(_ZONE_SCOPED)]
    return region_scoped or records


def regions_with_quota(gpu: str, quotas: list[dict]) -> list[str]:
    """Regions where this project could start `gpu` today, as far as quota knows."""
    granted: set[str] = set()
    for quota in _binding(gpu, quotas):
        granted |= _granted_regions(quota)
    return sorted(granted)


def allowance(gpu: str, quotas: list[dict], *, region: str | None = None) -> int | None:
    """The largest grant this project holds for a card. None if it reports none.

    `UNLIMITED` (-1) is passed through as itself rather than flattened to a big
    number, so a caller can say "unlimited" instead of inventing a ceiling.
    """
    best: int | None = None
    for quota in _binding(gpu, quotas):
        for where, limit, locations in _rows(quota):
            if region and not _applies(where, locations, region):
                continue
            if limit == UNLIMITED:
                return UNLIMITED
            if best is None or limit > best:
                best = limit
    return best


def global_allowance(quotas: list[dict]) -> int | None:
    """`GPUS_ALL_REGIONS` — the project-wide ceiling across every card.

    Not a card you can pick, and the limit that most often actually bites: it is
    **1** on this project, so a second GPU box cannot start while the first one
    is running, whatever the per-card grant says. None when the project reports
    no such quota at all.

    EVERY matching record is read and then reduced. This returned on the FIRST
    match, so the answer depended on the order gcloud happened to list the
    records in — which is not a contract gcloud offers — and the project carries
    two of them, `GPUS-ALL-REGIONS-per-project` and its `-per-project-zone`
    copy, which `friendly_name` maps to the same label. The zone-scoped copy is
    -1, so whenever it sorted first the one limit that governs every create on
    this project read as UNLIMITED.

    Two defences, because the ordering between records was not the only way in:

    **The zone-scoped copy drops out when a region-scoped record exists**, which
    is what `_binding` already did for the per-card path, for the same reason.

    **And a real limit beats -1 wherever the two meet**, which the scope filter
    does not cover: one correctly region-scoped record carrying an unlimited row
    AND a row of 1 also read as unlimited, in any row order. -1 is Google's
    "this record sets no explicit limit" — the ABSENCE of a constraint, not a
    grant of infinity — and letting an absence overrule a number that was read
    is how a tool cheerfully starts a second box on a ceiling of one. So
    UNLIMITED is the answer only when nothing else was found.

    Among real limits the largest wins, as in `allowance`. Two records
    disagreeing about the project-wide ceiling is not a shape any live project
    has shown, and guessing low would refuse a create the project is entitled
    to — the strict direction is not free either.
    """
    records = _prefer_region_scope(
        [q for q in quotas
         if friendly_name(q.get("quotaId") or "") == GLOBAL_ALLOWANCE])

    best: int | None = None
    unlimited = False
    for quota in records:
        for _where, limit, _locations in _rows(quota):
            if limit == UNLIMITED:
                unlimited = True
            elif best is None or limit > best:
                best = limit
    if best is not None:
        return best
    return UNLIMITED if unlimited else None
