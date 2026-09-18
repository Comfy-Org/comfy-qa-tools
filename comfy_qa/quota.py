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

# FOUR STATES, not three, and the mismatch was a live defect. `AskState` has
# four and this had three, so `denied` and `partial` both collapsed onto `none` —
# whose rendered text is "none — request it". Six refused requests on the live
# project were displayed as an invitation to file all six again, on the one
# command whose job is "what can I run and what do I do next". `setup` already
# refuses to re-ask for a denied card; `quota list` was telling a human to.
#
# `none` now means only what it says: never asked about, the one case where
# "request it" is true advice.
Status = Literal["ready", "pending", "denied", "partial", "none"]

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
    asked: int = 0
    """What was asked for, when Google answered with less. 0 otherwise."""
    refused_in: int = 0
    never_asked_in: int = 0
    """The two halves of a `denied` row, COUNTED IN PLACES rather than in rows.

    A per-card quota is ONE row covering 43 locations, so a refusal in a single
    region marks the whole row denied — and the table then asserted "asking again
    will not help" about 42 regions nobody had asked in. Counting rows finds one
    row and learns nothing; counting the locations a denial actually covers finds
    1 and 42.
    """

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


# The second quota shape, and the one this module could not see at all.
#
# Newer cards are NOT metered as `NVIDIA-<card>-GPUS-per-project-region`. They
# share ONE quota id, `GPUS-PER-GPU-FAMILY-per-project-region`, and the card is a
# DIMENSION on the row rather than part of the id. Read off this project on
# 2026-09-17, from `quotas info describe`:
#
#   dimensions {gpu_family: NVIDIA_H100}                 43 locations, details {}
#   dimensions {gpu_family: NVIDIA_H100_MEGA}            43 locations, details {}
#   dimensions {gpu_family: NVIDIA_H200}                 43 locations, details {}
#   dimensions {gpu_family: NVIDIA_B200}                 43 locations, details {}
#   dimensions {gpu_family: NVIDIA_RTX_PRO_6000}         41 locations, details {}
#   dimensions {gpu_family: NVIDIA_RTX_PRO_6000,
#               region: us-central1}                      1 location,  details {}
#   dimensions null                                      43 locations, details {}
#
# `friendly_name` reads an ID, so it answered None for every one of those, and
# five real cards were invisible in `quota list`: a user could not see that H100
# exists on their project, could not see it was at zero, and had no way to ask
# for it. `--gpu h100` reported "this project reports no quota for 'h100'" about
# a card the project meters perfectly well.
#
# So the unit this module reads is now a ROW, not a record. A row's card comes
# from its own dimensions where the id does not carry one. The `dimensions null`
# row is the catch-all for families not named above and is deliberately dropped —
# it is about no card in particular.
#
# `details {}` with no `value` is how this API reports ZERO, not how it reports
# "unknown". Every family above is at zero.
_FAMILY_DIMENSION = "gpu_family"


def family_name(gpu_family: str | None) -> str | None:
    """`NVIDIA_RTX_PRO_6000` -> `RTX-PRO-6000`. The card a family row is about.

    THE CARD TABLE'S NAME WHEN THE TABLE HAS ONE, so a card has one name across
    every command. `NVIDIA_H100` derives to "H100", and the card Google sells is
    `nvidia-h100-80gb`, which `setup` and `create` both call H100-80GB — so
    `create --gpu h100` printed "H100-80GB: 0" and then "this project has no H100
    quota" in the same run, and `quota list`, the command both of those point at,
    was the one showing the name neither used.

    The join already existed: `Card.quota_family` is `NVIDIA_H100` and
    `quota_aliases` carries "H100". Derivation stays as the fallback, for the
    families this table has never heard of.
    """
    if not gpu_family:
        return None
    from .create import CARDS

    for card in CARDS.values():
        if card.quota_family and card.quota_family.upper() == gpu_family.upper():
            return card.name
    name = gpu_family.upper().replace("_", "-").removeprefix("NVIDIA-")
    return name.strip("-") or None


def row_name(quota_id: str, dimensions: dict | None = None) -> str | None:
    """The card ONE quota row is about. None when the row names no card.

    The whole of the family fix, in one function. Everything else in this module
    reads rows through it, so a surface cannot see one shape and miss the other.
    """
    # `quota_id or ""` HANDLES THE ABSENT ID, and then `friendly_name(quota_id)`
    # was handed the `None` anyway and raised `AttributeError` — from a function
    # whose first line promises None "when the row names no card". A function
    # documented to answer for the absent case has to survive being given one.
    if not quota_id:
        return None
    name = _SCOPE_SUFFIX.sub("", quota_id).upper().replace("_", "-")
    if name.startswith(_NOT_A_CARD):
        return family_name((dimensions or {}).get(_FAMILY_DIMENSION))
    return friendly_name(quota_id)


MANY_REGIONS = re.compile(r"^\d+ regions$")


def where_label(places) -> str:
    """One phrasing for a set of places, read by every surface.

    There were three for the same forty-three regions — `all regions` from
    `Row.where`, `43 regions` from `Pool.where`, and `in 43 region(s)` from
    `create` — and nothing reconciled them, so a reader comparing two lines had
    to know which object produced each.

    The COUNT form wins because it never claims more than it knows: `all
    regions` was this module's label for "several places at once", and
    `pools_for` really does see a 41-of-43 span, which `all regions` would have
    reported as every region there is.
    """
    from .zones import region_of

    # BY REGION. A zone list is a set of regions — nine zones across three
    # regions is three — and counting the zones gave "130 regions" about a card
    # metered in 43. `region_of` leaves a region unchanged, so this narrows and
    # never invents.
    places = {region_of(place) for place in places if place}
    if not places or places == {"global"}:
        return "global"

    # A SPAN AND ITS NAMED SIBLINGS ADD UP. This took `max()` over the spanning
    # labels and discarded every individually named region with it, so a card
    # with twenty-four named rows and a nineteen-location catch-all reported
    # "19 regions" — a count that excluded us-central1, the tool's own default,
    # while `create` said 43 about the same project in the same minute.
    #
    # The two kinds are disjoint by construction: a catch-all dimension row
    # covers exactly the locations that have no row of their own, so adding them
    # cannot double-count. Several spans likewise describe separate allowances.
    spanning = [p for p in places if spans_many(p)]
    named = [p for p in places if not spans_many(p) and p != "global"]
    if spanning:
        total = sum(int(p.split()[0]) for p in spanning) + len(named)
        return f"{total} regions"
    if len(named) == 1:
        return named[0]
    return f"{len(named)} regions"


def spans_many(where: str) -> bool:
    """Is this label "several places" rather than one named region?

    A predicate, because the comparisons it replaces were `== "all regions"` —
    string equality against one of the three spellings, which is exactly how a
    label change breaks a check nobody remembers writing.
    """
    return bool(MANY_REGIONS.match(where or ""))


def same_card(one: str, other: str) -> str | bool:
    """Do these two spellings name the same card?

    `flatten` equality was the join, and it cannot see an alias. The moment
    `family_name` started returning the card table's name — so that one card has
    one name everywhere — the `NVIDIA_H100` row began calling itself H100-80GB
    while every lookup still asked for `h100`, and a GRANTED H100 read as not
    granted. A rename is only as good as the comparison underneath it.

    The card table is the authority: key, name and `quota_aliases` are all the
    same card. Anything the table has never heard of falls back to `flatten`,
    which is what B200, H200 and H100-MEGA still rely on.
    """
    from .create import card_named

    if flatten(one) == flatten(other):
        return True
    first, second = card_named(one), card_named(other)
    return first is not None and second is not None and first.key == second.key


def flatten(value: str) -> str:
    """One spelling for `A100-80GB`, `a100_80gb` and `A100 80GB`."""
    return value.lower().replace("-", "").replace("_", "").replace(" ", "")


def matches(gpu: str, quota_id: str) -> bool:
    """Does a user's `l4` name this quota? Case and separators are forgiven.

    ID-ONLY, so it cannot answer for a family row — `GPUS-PER-GPU-FAMILY` names
    no card by itself. Row-level code asks `row_name` instead; this stays for the
    callers that genuinely hold only an id.
    """
    friendly = friendly_name(quota_id)
    if friendly is None:
        return False
    return bool(same_card(friendly, gpu))


# Beyond this many applicable regions, listing them individually is noise: the
# same allowance applies everywhere and one row says so better than forty-three.
_MANY_REGIONS = 3


@dataclass(frozen=True)
class Row:
    """ONE allowance, for one card, in one place.

    The unit this module reads. It used to be the quota RECORD, which works only
    while one record is about one card — true of `NVIDIA-L4-GPUS-per-project-region`
    and false of `GPUS-PER-GPU-FAMILY-per-project-region`, where a single record
    carries five different cards on five different rows. Reading records made
    those five invisible.

    `dimensions` is the row's own, kept because a REQUEST has to carry them back
    verbatim: Google treats a preference's dimensions as immutable, and matches
    an existing preference on them exactly.
    """

    quota_id: str
    gpu: str
    where: str
    limit: int
    locations: tuple[str, ...]
    dimensions: tuple[tuple[str, str], ...]

    @property
    def dims(self) -> dict[str, str]:
        return dict(self.dimensions)

    @property
    def zone_scoped(self) -> bool:
        return self.quota_id.lower().endswith(_ZONE_SCOPED)


def rows(quotas: list[dict]) -> list[Row]:
    """Every allowance this project reports, as rows about cards.

    The live API puts the places in `applicableLocations` and leaves the per-entry
    `dimensions` null for per-card quotas, so reading a region out of `dimensions`
    finds nothing there. A single allowance covering forty-three regions is one
    row, not forty-three.
    """
    out: list[Row] = []
    for quota in quotas:
        quota_id = quota.get("quotaId") or ""
        for info in quota.get("dimensionsInfos") or []:
            dimensions = info.get("dimensions") or {}
            gpu = row_name(quota_id, dimensions)
            if gpu is None:
                continue
            locations = info.get("applicableLocations") or []

            explicit = dimensions.get("region") or dimensions.get("zone")
            if explicit:
                where = explicit
            elif len(locations) == 1:
                where = locations[0]
            elif locations:
                where = where_label(locations)
            else:
                where = "global"

            raw = (info.get("details") or {}).get("value")
            try:
                limit = int(raw)
            except (TypeError, ValueError):
                # AN INFERENCE, NAMED AS ONE. An absent value is read as zero:
                # `details: {}` is what all five modern GPU families report here,
                # and an independent reading of `PREEMPTIBLE_CPUS` agrees that
                # absent means none. But `CPUS-ALL-REGIONS-per-project` reports
                # `'32'` in the same field, so absence is the API's way of saying
                # something rather than a parse failure — and inferring meaning
                # from an absence is precisely what produced a CPU gate that does
                # not exist. The reading is almost certainly right and it is still
                # an assumption; this is where it is made.
                limit = 0
            out.append(Row(quota_id, gpu, where, limit, tuple(locations),
                           tuple(sorted(dimensions.items()))))
    return out


def _for_card(gpu: str, quotas: list[dict]) -> list[Row]:
    """The rows that decide whether a card can start, and no others.

    A project routinely carries both scopes for the same card, and they disagree:
    this one has L4 at **1** across 43 named regions
    (`NVIDIA-L4-GPUS-per-project-region`) and an **unlimited** per-zone allowance
    across the 130 zones inside them (`...-per-project-zone`, value -1). Read
    together, the card looks unlimited and available in a region the project has
    no grant in. The region-scoped rows are the ones that bind, so where there
    are any they are the only ones read.
    """
    return _prefer_region_scope(
        [row for row in rows(quotas) if same_card(row.gpu, gpu)])


def _prefer_region_scope(found: list[Row]) -> list[Row]:
    """Drop the zone-scoped copies when a region-scoped row exists.

    Written once because both the per-card path and the project-wide ceiling need
    it and only one of them had it. A project carries both scopes of the same
    quota, they map to the same card name, and the zone-scoped copy is the one
    that says -1.
    """
    region_scoped = [row for row in found if not row.zone_scoped]
    return region_scoped or found


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


# --- what did we already ask Google for, and what did Google say ------------
#
# A quota preference is not a yes/no. It is a standing record carrying the value
# asked for, the value granted, and a sentence about where the request got to.
# Read off a live project, and recorded as the SHAPES the API produces rather
# than as a census. The five below were that project's whole preference list when
# this was written; hours later it held seven and two of the five had changed
# state. What does not decay is that these are the shapes the reader has to tell
# apart, and that three of them carry `preferredValue: 1` while meaning entirely
# different things:
#
#   gpus-all-regions-1  GPUS-ALL-REGIONS-per-project      granted 1  wanted 1
#                       stateDetail "Quota request approved to 1"
#   a0e3b926-...        NVIDIA-L4-GPUS-per-project-region granted 1  wanted 1
#                       stateDetail ABSENT
#   a100-80-euw4        NVIDIA-A100-80GB-...-region       granted 0  wanted 1
#                       stateDetail "Quota request denied"
#   rtxpro6000-usc1     GPUS-PER-GPU-FAMILY-...-region    granted 0  wanted 1
#                       stateDetail "Quota request denied"
#   rtxpro6000-euw2     GPUS-PER-GPU-FAMILY-...-region    granted 0  wanted 1
#                       stateDetail "Quota request denied"
#
# THREE of those five are DENIED, and every one of them carries
# `preferredValue: 1`. `_pending_ids` asked only whether somebody had asked for
# more than nothing, so all three read as "pending - waiting on Google", about
# requests Google finished answering on 2026-08-05. A tool that reports a refusal
# as still under consideration is worse than one that says nothing: it tells a
# person to wait for an answer that has already arrived.
#
# It also breaks the only thing standing between `setup` and a second submission.
# "Do not re-ask what is already pending" is the whole of the idempotence rule,
# and it is worth nothing if every settled request looks pending forever.
#
# AND THE SIGNAL IS `reconciling`, NOT THE PROSE. The schema calls it "Output
# only. Is the quota preference pending Google Cloud approval and fulfillment",
# and gcloud's own `--reconciling-only` is implemented as the server filter
# `reconciling:true`. That is authoritative where an English sentence is not.
# The prose is kept as a fallback, not as the rule: `stateDetail` is absent
# entirely on two of the five live rows.
#
# A preference is also PERMANENT — "The ability to delete a QuotaPreference is
# not supported" — so `quotas preferences list` returns every request this
# project has ever made, granted ones included, forever. Presence means nothing.
AskState = Literal["satisfied", "partial", "pending", "denied"]

# gcloud reports the outcome as an English sentence rather than an enum, so the
# fallback matches the word. The failure directions are not symmetric: reading a
# denial as pending strands a person waiting for an answer that came, while
# reading a pending request as denied costs at worst one re-ask.
_DENIED = "denied"


@dataclass(frozen=True)
class Ask:
    """One request this project has already put to Google, and where it got to."""

    quota_id: str
    dimensions: dict[str, str]
    granted: int | None
    preferred: int | None
    """None means the API OMITTED the field, which is not the same as zero.

    `_as_int` flattened both to 0, and `request_value` then compared
    `wanted >= 0` — true for every value — so the guard against lowering a
    standing request returned the default with an empty message whenever
    `preferredValue` was absent. No refusal, nothing printed, in the only
    function in this feature that changes state at Google.

    Fifth time tonight that absent and zero were read as one thing. The rule is
    written a hundred lines up in this module: Google omits a value it has none
    of, so an absence is the API saying something.
    """
    state: AskState
    preference_id: str = ""
    """Its own resource id, which is the ONLY id that can update it."""

    # A REFUSED INCREASE DOES NOT COST THE GRANT YOU ALREADY HAVE. Verified on
    # real hardware rather than reasoned about: `gpus-all-regions-1` was asked to
    # go from 1 to 2, Google refused, and it now reads `preferred 2, granted 1,
    # "Quota request denied"` — the failed ask is recorded and the allowance is
    # untouched. Which is what `grantedValue` being "Output only" implies, but
    # the implication was not worth betting the project's only working GPU
    # allowance on, and now nobody has to.

    @property
    def region(self) -> str | None:
        return self.dimensions.get("region")


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_int_or_none(value: object) -> int | None:
    """`_as_int` for the fields where an absence must stay an absence."""
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def asks(preferences: list[dict] | None) -> list[Ask]:
    """Every standing quota preference, read as a state rather than a presence."""
    out: list[Ask] = []
    for pref in preferences or []:
        quota_id = pref.get("quotaId")
        if not quota_id:
            continue
        config = pref.get("quotaConfig") or {}
        granted = _as_int_or_none(config.get("grantedValue"))
        preferred = _as_int_or_none(config.get("preferredValue"))
        detail = str(config.get("stateDetail") or "").lower()

        if pref.get("reconciling") is True:
            state: AskState = "pending"
        elif (preferred is not None and granted is not None
              and preferred > 0 and granted >= preferred):
            # Satisfied before denied, deliberately. A request denied at 1 on a
            # project that has since reached 1 by some other route is not
            # something to re-ask for, and what Google said last month about a
            # number you now hold is history rather than an obstacle.
            state = "satisfied"
        elif _DENIED in detail:
            state = "denied"
        elif detail:
            # Google answered and gave less than was asked for. Not pending —
            # nobody is still deciding — and not a refusal either.
            state = "partial"
        else:
            # No `reconciling` flag, nothing granted, and Google has said
            # nothing. Still waiting, and the safe reading: do not re-ask.
            state = "pending"
        out.append(Ask(quota_id, dict(pref.get("dimensions") or {}),
                       granted, preferred, state,
                       str(pref.get("name") or "").rsplit("/", 1)[-1]))
    return out


def covers(ask: Ask, row: Row) -> bool:
    """Is this standing request about this allowance row?

    Keyed on the DIMENSIONS as well as the id, which is the difference between a
    useful answer and a wrong one now that one quota id carries five cards. A
    pending H100 request would otherwise mark H200, B200, H100-MEGA and
    RTX-PRO-6000 pending too, because they share
    `GPUS-PER-GPU-FAMILY-per-project-region`.

    A dimension the request does NOT name is a wildcard, per the API's own rule:
    "If a dimension is missing from the map of dimensions, the quota preference
    applies to all the dimension values except for those that have other quota
    preferences configured for the specific value."
    """
    return covers_dimensions(ask.quota_id, ask.dimensions, row)


def covers_dimensions(
    quota_id: str, dimensions: dict[str, str], row: Row,
) -> bool:
    """`covers`, for a caller holding a (quota id, dimensions) pair rather than an Ask.

    Split out so the poll in `quota request --wait` can reuse it. Writing a second
    matcher there is what produced the defect it fixes: `_value_of` (since
    REMOVED) maxed across
    every row of a quota regardless of dimensions, so a T4 granted in forty-two
    other regions answered a request about europe-west4. A parallel implementation
    of the same predicate is how the two shapes diverged in the first place.
    """
    if quota_id != row.quota_id:
        return False
    for key, value in (dimensions or {}).items():
        if key == "region":
            if not _applies(row.where, list(row.locations), value):
                return False
        elif row.dims.get(key) != value:
            return False
    return True


def matching_ask(target: "Target", preferences: list[dict] | None) -> Ask | None:
    """The standing preference for EXACTLY this target, if there is one.

    Exact, not overlapping, and that is Google's rule rather than a choice here.
    Verified live with `--validate-only`:

      minting a new id for a pair that already has a preference is refused —
        "Quota Preference with dimension '{}' already exist for container ...,
         quota id 'GPUS-ALL-REGIONS-per-project', in location 'global'"
      and updating an existing one with different dimensions is refused too —
        "Location in existing quota preference 'a0e3b926-...' does not match:
         'global' in request, 'us-central1' in quota preference"

    So a request either updates an existing preference BY ITS OWN ID and WITH ITS
    OWN DIMENSIONS, or creates a fresh one under an id nothing has claimed. There
    is no third option, and dimensions are immutable, so the match has to be
    exact both ways.
    """
    wanted = target.dims
    for ask in asks(preferences):
        if ask.quota_id == target.quota_id and ask.dimensions == wanted:
            return ask
    return None


@dataclass(frozen=True)
class RequestPlan:
    """How to address ONE quota request so that a re-run updates rather than duplicates."""

    preference_id: str
    dimensions: dict[str, str]
    allow_missing: bool
    existing: Ask | None

    @property
    def updates_existing(self) -> bool:
        return self.existing is not None


def request_plan(target: "Target", preferences: list[dict] | None) -> RequestPlan:
    """Address an existing preference by ITS id, or mint a stable new one.

    The whole of Rule 1 and Rule 2, in one place, so no caller can get half of it.
    Google refuses BOTH of the obvious mistakes:

      * a new id for a (quota id, dimensions) pair that already has a preference —
        "Quota Preference with dimension '{}' already exist for container ...";
      * an update whose dimensions differ from the existing preference's, because
        dimensions are immutable — "Location in existing quota preference
        'a0e3b926-...' does not match: 'global' in request, 'us-central1' in
        quota preference".

    So a deterministic id ALONE is wrong, which is where this started: it works
    only for pairs nobody has ever requested, and on this project that already
    fails for GPUS-ALL-REGIONS, L4, A100-80GB and RTX PRO 6000 twice over. An
    existing preference has to be updated under its own id and with its own
    dimensions, whatever we would have chosen.
    """
    existing = matching_ask(target, preferences)
    if existing is not None:
        return RequestPlan(existing.preference_id or _minted(target),
                           dict(existing.dimensions), allow_missing=False,
                           existing=existing)
    return RequestPlan(_minted(target), target.dims, allow_missing=True,
                       existing=None)


def held_value(target: "Target", quotas: list[dict] | None) -> int | None:
    """The quota this project actually HOLDS for exactly this target.

    `covers_dimensions` rather than a matcher of its own: a third implementation
    of "does this row answer this target" is precisely how the last one diverged.

    `_raw_rows` and NOT `rows`, which drops every row whose quota id it cannot
    name as a card. Reading this through `rows` is how the first draft answered
    "holds nothing" for the CPU and all-regions targets — not because the project
    held nothing, but because the row had been filtered out before it was asked.
    A floor that reads absent when it cannot see is the same defect this function
    was written to close, one layer down.
    """
    best: int | None = None
    for row in _raw_rows(target.quota_id, quotas or []):
        if not covers_dimensions(target.quota_id, target.dims, row):
            continue
        if row.limit == UNLIMITED:
            return UNLIMITED
        if best is None or row.limit > best:
            best = row.limit
    return best


def request_value(
    target: "Target", preferences: list[dict] | None, wanted: int, *,
    allow_lower: bool = False, quotas: list[dict] | None = None,
    release: bool = False, typed: bool = True,
) -> tuple[int | None, str]:
    """The value to actually send, and a sentence about it if it is not `wanted`.

    THE ONE DEFECT TONIGHT THAT CHANGED STATE AT GOOGLE RATHER THAN MISREPORTING
    IT. `quota request --value` defaults to 1, and `request_plan` addresses an
    existing preference by its own id — correct for avoiding a duplicate, and it
    turns a re-run into an `update`. So `setup` filed H100 at 8, and a user
    running `comfy-qat quota request --gpu h100` to check on it silently
    downgraded a live request to a number that cannot start an `a3-highgpu-8g`
    even if granted.

    A value the user did not type must never overwrite one they did, AND no value
    may quietly reduce what this project already holds. So:

    * raising, or asking for something nobody has asked for, is unchanged;
    * a value BELOW the floor is refused, and the refusal says the word "lower" —
      this must be a thing the output names, not an outcome somebody discovers
      days later;
    * the floor is the LARGER of the standing request and the GRANT, because
      either can be the thing that disappears;
    * `allow_lower` performs it and still names it;
    * zero is refused outright, `allow_lower` or not, and needs `release` on top.

    THE GRANT WAS ADDED LAST, after six rounds of work on this function had
    protected only the standing request. That is the whole lesson: we reasoned
    about not overwriting a number the user had typed into a request, and the
    thing that actually matters is the quota the project holds. A card can hold
    quota with no preference at all — true of T4, L4, K80, P100, P4 and V100 on
    this project — so `matching_ask` returned None, this function took the
    "nobody has asked for it" branch, and `--gpu t4 --value 0` built a command
    setting a working grant to zero with no refusal and no LOWERING line. The
    six unprotected cards were precisely the six that work today.

    THE GUARD STANDS ON ITS OWN REASONING and deliberately not on any claim about
    what Google does afterwards: **a value the user did not type must never
    overwrite one they did.** That is sufficient, and it is checkable here.

    An earlier version of this docstring asserted that Google "fulfils a decrease
    immediately and without review, so it is the one quota change that is quick,
    permanent and unremarked". That was relayed rather than read, and it is wrong
    twice over. Nothing in the API schema says "immediately" or "without review";
    and `QuotaDetails.resetValue` is documented as "the Google Cloud defined
    quota value that the quota will be reset to if a quota decrease preference is
    deleted", which contradicts "permanent" outright.

    WHAT THE SCHEMA ACTUALLY SUPPORTS — that Google treats the two directions
    differently, which is all this needs:

      contactEmail  "When requesting a quota increase, the email address is
                    required. When requesting a quota decrease, the email
                    address is optional."
      traceId       "only produced for increase requests ... The quota decrease
                    requests do not have a trace id."
      QuotaSafetyCheck  QUOTA_DECREASE_BELOW_USAGE and
                    QUOTA_DECREASE_PERCENTAGE_TOO_HIGH, with gcloud flags to
                    override each — so a decrease is the direction Google puts
                    guards on.

    Testing the rest would mean lowering a real request, which is the thing this
    function exists to prevent. It stays unverified, and is marked as such rather
    than dressed up.

    A FOOTNOTE ON "A PREFERENCE CANNOT BE DELETED", which this design leans on
    everywhere. Three facts and one gap, in that order:

      * `gcloud quotas preferences` exposes create, describe, list and update —
        and NO delete. So "cannot be deleted" is true of the interface this tool
        uses, which is what the design actually rests on.
      * the API schema nonetheless contemplates deletion: `resetValue` is "the
        value the quota will be reset to if a quota decrease preference is
        deleted".
      * those two are about different surfaces, so they do not contradict.
      * WHICH IS TRUE OF THE REST API IS UNVERIFIED. Nobody has looked, and
        settling it would mean filing something. It changes nothing here — we
        never lower without `--allow-lower`, and we cannot delete through the
        interface we use — so it is recorded rather than resolved.

    In ONE place because both `quota request` and `setup` compute a value and
    both can hit this — `setup`'s ceiling number moves with the card table, so a
    plan computing 2 against a standing 8 would trim it. Seven second-sites
    tonight were enough.
    """
    existing = matching_ask(target, preferences)
    held = held_value(target, quotas)

    # `--allow-lower` IS PERMISSION TO LOWER TO A NUMBER YOU NAMED, not permission
    # for the tool to pick one. The caller collapsed "the user typed N" and
    # "nobody typed anything, so use the default" into one integer before this
    # function could tell them apart — and both guards that preserved the
    # distinction downstream are switched off by `--allow-lower` itself. So
    # `quota request --gpu h100 --allow-lower` sent the tool's own default over a
    # live PENDING request of 16, under that preference's own id.
    #
    # The rule had been stated at the call site for six rounds — "a number they
    # chose may lower a standing request once they confirm it; a default must
    # never touch it" — written where the value is built and lost one line later.
    if not typed:
        allow_lower = False

    # A NEGATIVE IS NOT A QUANTITY, and no flag makes it one. Zero is at least a
    # coherent request — hold none of this card — and is refused because it is
    # destructive; -5 is not a request at all, so it is refused before the API is
    # asked, whatever flags are passed. `--allow-missing` means a negative would
    # CREATE a permanent preference, and `--allow-lower` let one reach a standing
    # request: "LOWERING the standing request ... from 1 to -5". The only guard
    # that could catch either was the don't-lower rule, which cannot fire on a
    # card nobody has asked for — a guard satisfied by the absence of the thing
    # it guards.
    if wanted < 0:
        return None, (
            f"{wanted} is not a number of GPUs; {target.quota_id} was not asked "
            f"for")

    # ZERO IS NOT A SMALL NUMBER, it is a release, and it is refused on its own
    # terms before any comparison — including when `quotas` says this project
    # holds nothing. Absent is not zero: a quota the API did not report may still
    # exist, so "nothing is at risk" is never a thing this function knows. There
    # is no QA reason to zero a GPU quota, and the cost of being wrong is a grant
    # that cannot be recovered by waiting.
    if wanted == 0 and not release:
        losing = held if held is not None else existing.preferred if existing else None
        return None, (
            f"refusing to set {target.quota_id} to 0"
            + (f", releasing the {_shown(losing)} this project holds"
               if losing else "")
            + " — pass --release-quota as well as --allow-lower if that is "
              "really what you mean")

    # `wanted` is non-negative by here, so any finite request is a reduction.
    if held == UNLIMITED:
        if not allow_lower:
            return None, (
                f"this project holds UNLIMITED {target.quota_id}; {wanted} "
                f"would lower it")
        return wanted, (f"LOWERING {target.quota_id} from UNLIMITED to {wanted}")

    if existing is None:
        # THE CARDS THAT WORK TODAY ARE THE ONES THAT REACH HERE. A grant and a
        # standing request are different objects, and six rounds of guard work
        # protected only the second — so `--gpu t4 --value 0` walked this branch
        # and set a live grant to zero, because T4 is granted with no preference,
        # as are L4, K80, P100, P4 and V100 on this project right now.
        return _floored(target, wanted, held, "quota this project holds for",
                        allow_lower=allow_lower)
    if existing.preferred is None:
        # UNKNOWN IS NOT ZERO, and here it is not "go ahead" either. There is no
        # standing value to keep, and sending anything might replace a larger one
        # — so nothing is sent, which is the only choice that cannot do harm.
        if allow_lower:
            # THROUGH `_floored`, like every other lowering path. This returned
            # early and skipped the GRANT floor — the addition this docstring
            # calls its whole lesson — so a project holding 2 sent 1 with a
            # sentence naming only the unreadable standing value and never the 2
            # being given up. `allow_lower` still performs it; it now says what
            # it costs, which is what the docstring promises it does.
            send, note = _floored(target, wanted, held,
                                  "quota this project holds for",
                                  allow_lower=True)
            unreadable = " (its standing request could not be read)"
            return send, ((note + unreadable) if note else
                          f"sending {wanted} for {target.quota_id}{unreadable}")
        return None, (
            f"could not read the standing value for {target.quota_id}, so "
            f"{wanted} was not sent — it might replace a larger request")
    # The floor is the LARGER of the two, because either can be the thing that is
    # lost: a grant of 4 under a standing request of 1 must not be trimmed to 1,
    # and a standing request of 8 over a grant of 1 must not be trimmed to 1.
    floor, what = existing.preferred, "standing request for"
    if held is not None and held > floor:
        floor, what = held, "quota this project holds for"
    return _floored(target, wanted, floor, what, allow_lower=allow_lower)


def _shown(value: int | None) -> str:
    return "UNLIMITED" if value == UNLIMITED else str(value)


def _floored(
    target: "Target", wanted: int, floor: int | None, what: str, *,
    allow_lower: bool,
) -> tuple[int | None, str]:
    """`wanted`, unless it is below what this project would lose by sending it."""
    if floor is None or wanted >= floor:
        return wanted, ""
    if allow_lower:
        return wanted, (f"LOWERING the {what} {target.quota_id} from {floor} "
                        f"to {wanted}")
    return floor, (f"keeping the {what} {target.quota_id} at {floor}; "
                   f"{wanted} would lower it")


def _minted(target: "Target") -> str:
    from .gcloud import quota_preference_id

    return quota_preference_id(target.quota_id, target.dims)


def asks_about(target: "Target", preferences: list[dict] | None) -> list[Ask]:
    """Every standing request about THIS CARD, whatever region it named.

    Deliberately NOT `matching_ask`, and the two answer different questions.
    `matching_ask` is exact because Google keys a preference on (quota id,
    dimensions) and addressing the wrong one is refused. This is about whether
    Google has already had its say on the card, which the region does not change.

    The live case that forced the split: `a100-80-euw4` was refused in
    europe-west4, and an all-regions A100-80GB request has different dimensions,
    so the exact match found nothing and `setup` cheerfully re-asked for a card
    Google had already turned down — which is the one thing a step that files
    irrevocable requests must not do.

    `gpu_family` is still honoured, because it is what says WHICH card: five of
    them share `GPUS-PER-GPU-FAMILY-per-project-region`, so ignoring it would
    read a refused RTX PRO 6000 as a refused H100.

    AND THE REGION IS HONOURED FOR FAMILY-METERED CARDS ONLY, which is the part
    "whatever region it named" got wrong — on the command the whole feature
    exists to deliver. The two shapes are not the same claim:

      * PER-CARD (`NVIDIA-A100-80GB-GPUS-per-project-region`): one grant covers
        all forty-three regions and names none of them — `setup`'s own header
        says "per-card grants cover every region and name none". So a refusal is
        about the CARD, and re-asking in a different region buys nothing while
        costing another irrevocable request to a human reviewer. Region stays
        ignored here, and that is the `a100-80-euw4` case this function was
        written for.
      * FAMILY (`GPUS-PER-GPU-FAMILY-per-project-region`): Google genuinely keys
        it on (gpu_family, region) — this module's `Target` docstring says "both
        dimensions are required". A no in us-central1 is not a no in
        europe-west1, where the card has never been asked for and is in fact
        sold.

    Even for a family card the rule is one-way: a refusal that NAMES NO REGION is
    about the card everywhere and still answers here. Only a refusal pinned to a
    DIFFERENT region is silent.

    Measured: `setup --region europe-west1` reported "nothing to request" while
    `quota list --region europe-west1` said "none — request it" and `quota
    request --gpu h100 --region europe-west1` built a valid request, all in the
    same minute. Three surfaces, one card, one region, two answers.

    A FIRST ATTEMPT APPLIED THE REGION RULE TO BOTH SHAPES and the suite caught
    it: `setup` began re-filing the A100-80GB request Google had already refused,
    which is the one thing a step that files irrevocable requests must not do.
    """
    family = target.dims.get(_FAMILY_DIMENSION)
    region = target.dims.get("region") if family else None
    found = []
    for ask in asks(preferences):
        if ask.quota_id != target.quota_id:
            continue
        if ask.dimensions.get(_FAMILY_DIMENSION) != family:
            continue
        asked_in = ask.dimensions.get("region")
        if region and asked_in and asked_in != region:
            continue
        found.append(ask)
    return found


def _ids_in_state(preferences: list[dict] | None, state: AskState) -> set[str]:
    return {ask.quota_id for ask in asks(preferences) if ask.state == state}


def pending_ids(preferences: list[dict] | None) -> set[str]:
    """Quota ids waiting on Google. Never ask for one of these a second time."""
    return _ids_in_state(preferences, "pending")


def denied_ids(preferences: list[dict] | None) -> set[str]:
    """Quota ids Google has refused. Askable again, but never without saying so."""
    return _ids_in_state(preferences, "denied")


def readiness(
    quotas: list[dict], preferences: list[dict] | None = None, *, region: str | None = None,
) -> list[Readiness]:
    """What can I run today, what is waiting on Google, and what did I never ask for."""
    standing = asks(preferences)
    found: list[Readiness] = []

    # THE RULE `_for_card` HAS HAD ALL ALONG, applied here too: "the region-scoped
    # rows are the ones that bind, so where there are any they are the only ones
    # read." This surface did not apply it, so a card metered both ways produced
    # two rows, and `summarise` SUMMED their counts — A100 reported "never asked
    # in 172" about a project with 43 regions. `allowance` already follows this
    # rule, so `create` computed 0 where `quota list` printed "ready 4": two
    # surfaces disagreeing about whether a box can start. Per card, because one
    # card holding only a zone-scoped quota must still be seen.
    by_card: dict[str, list[Row]] = {}
    for row in rows(quotas):
        by_card.setdefault(row.gpu, []).append(row)
    binding = [row for group in by_card.values()
               for row in _prefer_region_scope(group)]

    for row in binding:
        if not _applies(row.where, list(row.locations), region):
            continue
        where = region if (region and spans_many(row.where)) else row.where
        # KEYED ON THE ROW, not on the quota id alone. Every modern card shares
        # ONE quota id, so "this id has a request" would mark H100, H200, B200
        # and RTX PRO 6000 by whatever was asked about one of them. The same
        # defect existed for per-card quotas one size smaller: a pending L4
        # request in us-central1 marked L4 pending in all 43 regions.
        about = [ask for ask in standing if covers(ask, row)]
        # What was asked for, whenever Google answered with less — carried on
        # EVERY state, not only on `partial`. A card granted 1 of the 2 asked for
        # is genuinely READY: you hold 1 and can start it, and demoting it would
        # break the create gate that reads `usable`. What was invisible is that
        # the raise was cut, which is a note on a usable card rather than a
        # different status.
        # Any answered request that left us short of what it asked for, whether
        # Google called it partial or denied. The live ceiling is the case that
        # matters: granted 1, asked for 2, "Quota request denied". You KEEP the
        # 1 — so the row is ready — and the fact that the raise was refused was
        # invisible, which is half of what a person needs before deciding
        # whether to ask again.
        # BOTH SIDES ARE `int | None`, AND THAT IS THIS MODULE'S OWN DOING.
        # `_as_int_or_none` exists so an ABSENT value can be told from a zero —
        # and Google omits `grantedValue` when it is zero, so a denied preference
        # routinely arrives with one side missing. Comparing them raised
        # `TypeError: '>' not supported between instances of 'int' and
        # 'NoneType'` out of `quota list`, the command whose whole job is saying
        # what you hold.
        #
        # Eleventh instance of absent-versus-zero, living inside the fix for it.
        # Making the distinction representable is half the work; the other half
        # is every consumer of the value, and it is the half that is easy to
        # believe you have already done.
        #
        # ABSENT IS NOT COMPARABLE, so a row with no readable pair is not a
        # shortfall — it is a thing we cannot say that about. Reading a missing
        # `granted` as 0 would invent the very shortfall this looks for.
        short = next((ask for ask in about
                      if ask.state in ("partial", "denied")
                      and ask.preferred is not None
                      and ask.granted is not None
                      and ask.preferred > ask.granted), None)
        asked = short.preferred if short else 0
        if holds_quota(row.limit):
            status: Status = "ready"
        elif any(ask.state == "pending" for ask in about):
            status = "pending"
        elif any(ask.state == "denied" for ask in about):
            # Google answered, and the answer was no. Rendering this as
            # "request it" is advice to re-file a refusal.
            status = "denied"
        elif short is not None:
            # Answered, and answered with nothing. Rare, and not the same fact as
            # never having asked.
            status = "partial"
        else:
            status = "none"
        # THE SPLIT IS ABOUT WHY YOU CANNOT USE A CARD, so a row you CAN use has
        # neither half. `never_asked_in` was `places - refused`, i.e. "not
        # refused" — which made a granted L4 report 43 places nobody had asked
        # in, when the truth is that it is granted in all of them and 42 is the
        # only number the phrase could sensibly mean elsewhere. A count whose
        # name and arithmetic disagree is the proxy shape: it happens to equal
        # the right answer exactly when nothing is granted.
        if holds_quota(row.limit):
            refused_in = never_asked_in = 0
        else:
            from .zones import region_of

            # REGIONS ON BOTH SIDES. `places` below is narrowed with
            # `region_of` and this was not — so a denial naming no region fell
            # back to `row.locations` raw, which for a zone-scoped row is ZONES.
            # A T4 denied across seven zones in two regions reported
            # `refused_in=7` beside its own `where` of "2 regions", rendered as
            # "refused in 7 regions" and keyed `refused_in_regions` in JSON.
            #
            # And the over-count drove `never_asked_in` to 0 through the
            # `max(0, ...)` clamp below, so the split this field exists to report
            # vanished in precisely the case it was wrong about.
            refused = {region_of(place) for ask in about
                       if ask.state == "denied"
                       for place in ([ask.region] if ask.region
                                     else row.locations)}
            # A row with no `applicableLocations` still describes one place —
            # the quota itself — so the split has something to divide. Written
            # with the count rather than the `or` idiom, for the reason recorded
            # in setup.py.
            # REGIONS, for the reason `where_label` gives: a per-zone row's 130
            # locations are 43 regions, and this number is printed as "never
            # asked in N" with the word regions beside it.
            places = len({region_of(p) for p in row.locations}) or 1
            refused_in = len(refused)
            never_asked_in = max(0, places - refused_in)
        found.append(Readiness(row.gpu, where, row.limit, status, row.quota_id,
                               asked, refused_in, never_asked_in))

    # Most cards are metered twice — once per region and once per zone — and both
    # ids carry the same friendly name. Showing "L4  us-central1  1  ready" twice
    # reads as a bug, so collapse to one row per card and place, keeping the
    # larger grant.
    best: dict[tuple[str, str], Readiness] = {}
    for row in found:
        key = (row.gpu, row.region)
        existing = best.get(key)
        if existing is None or row.limit > existing.limit:
            best[key] = row

    return sorted(best.values(),
                  key=lambda r: (_ORDER[r.status], r.gpu, r.region))


# Ready first, then what is still moving, then the answers, then what nobody has
# asked about — which is the order a person reads for "what can I run, and what
# do I do next".
_ORDER = {"ready": 0, "pending": 1, "partial": 2, "denied": 3, "none": 4}


@dataclass(frozen=True)
class CardSummary:
    """One card, one line. What you can run, and roughly where."""

    gpu: str
    limit: int
    status: Status
    where: str
    asked: int = 0
    """What was asked for, when Google answered with less. 0 otherwise."""
    refused_in: int = 0
    """How many places a request was refused in."""
    never_asked_in: int = 0
    """How many places nobody has asked about.

    BOTH, because `_ORDER` cannot mean two things at once. It is the display sort
    AND the definition of "most favourable state a card has anywhere" — and for
    `denied` versus `none` those orders are OPPOSITE: `none` is the strictly more
    actionable of the pair. Ranking `denied` first made the default table assert
    "asking again will not help" about 42 regions nobody had asked in; ranking
    `none` first would have hidden a real refusal. Neither order is right because
    the question is not a ranking, so the row reports the split.
    """

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
        # The SECOND collapse, and it had the same hole as the first: three
        # branches for five states, so a card denied in every region summarised
        # as "none — request it" even once `readiness` knew better. Picking the
        # most favourable state a card has anywhere is what this always did;
        # `_ORDER` is now the single statement of what "most favourable" means,
        # so the two cannot drift.
        status: Status = min((e.status for e in entries), key=lambda s: _ORDER[s])

        relevant = [e for e in entries if e.status == status]
        places = {e.region for e in relevant}
        if places == {"global"}:
            # The project-wide allowance is genuinely global, not "every region".
            where = "global"
        else:
            where = where_label(places)

        summaries.append(CardSummary(
            gpu=gpu, limit=best, status=status, where=where,
            asked=max((e.asked for e in relevant), default=0),
            # ACROSS EVERY ENTRY, not only the winning status. `relevant` keeps
            # the rows matching the status that won, so summing the split over
            # it dropped exactly the rows the split exists to count: a card
            # denied in one region and never asked about in 42 reported
            # "refused in 1, never asked in 0", because the 42 `none` rows were
            # filtered out one line above.
            refused_in=sum(e.refused_in for e in entries),
            never_asked_in=sum(e.never_asked_in for e in entries)))

    return sorted(summaries, key=lambda s: (_ORDER[s.status], s.gpu))


# --- the four pools -----------------------------------------------------------
#
# GOOGLE METERS EACH CARD SEVERAL TIMES OVER, AND THIS MODULE READ ONE OF THEM.
# Verified live on 2026-09-17:
#
#   RTX PRO 6000  on-demand   GPUS-PER-GPU-FAMILY[NVIDIA_RTX_PRO_6000]  none, DENIED
#                 Spot        PREEMPTIBLE-NVIDIA-RTX-PRO-6000-GPUS      1, 43 locations
#                 workstation NVIDIA-RTX-PRO-6000-VWS-GPUS              1, 43 locations
#                 committed   COMMITTED-NVIDIA-RTX-PRO-6000-GPUS        id not present
#
# So `quota list` reported that card as denied — true of the on-demand pool and
# FALSE ABOUT THE CARD, which this project can run today as a Spot
# `g4-standard-48` with no request of any kind. Worse, `setup` filed an
# irrevocable on-demand request for it in two regions while the Spot grant sat
# unused.
#
# The exclusions this replaces were not wrong, they were answering a narrower
# question. `friendly_name` still drops PREEMPTIBLE, COMMITTED and VWS rows,
# because the on-demand table is what "what can I start an ordinary box with"
# means and mixing pools into it would double every card. This layer sits beside
# that one and answers "can this card be run at all, and how".
#
# WHICH POOLS COUNT, and the two that deliberately do not:
#
#   on-demand   yes.
#   Spot        yes, and it is the documented route. It costs something real —
#               a Spot box can be reclaimed mid-run — so `cost` is not optional
#               decoration; replacing one wrong impression with another is not a
#               fix.
#   committed   NO. A committed-use allowance needs a purchased commitment, so
#               holding one does not mean you can start a box today. Reported.
#   workstation NO, and this one is an open question rather than a settled
#               answer. Whether a plain G4 may draw on the VWS pool without a
#               workstation licence is INFERRED and unverified, and this file
#               does not get to encode a guess about it. Reported, never counted.
#
# Note the asymmetry, because it is mechanical and surprising: a card metered
# on-demand under the FAMILY id is still metered per-card under the Spot id —
# `PREEMPTIBLE-NVIDIA-H100-GPUS-per-project-region` exists while
# `NVIDIA-H100-GPUS-per-project-region` does not.


@dataclass(frozen=True)
class Pool:
    """One way a card is metered, and whether it can start a box."""

    name: str
    quota_id: str
    limit: int
    status: Status
    counts: bool
    """False for pools that exist and still do not let you start a box today."""
    cost: str = ""
    """What choosing this pool costs the user. Empty for on-demand."""
    where: str = ""
    """This pool's OWN geography.

    Carried because a row won by a non-on-demand pool used to show the
    on-demand one's: RTX-PRO-6000 read "2 regions", the two places its
    ON-DEMAND request was refused, while the Spot grant that made the row ready
    spans 43. Every column was about a different pool from the one the status
    named.
    """

    @property
    def usable(self) -> bool:
        return self.counts and self.status == "ready"


ON_DEMAND = "on-demand"
SPOT = "Spot"
COMMITTED = "committed"
WORKSTATION = "workstation"

SPOT_COST = "reclaimable mid-run"
_COMMITTED_COST = "needs a purchased commitment before it can be used"
_VWS_COST = ("a workstation allowance; whether a plain instance may draw on it "
             "is unverified")


def _pool_ids(gpu: str) -> list[tuple[str, str, bool, str]]:
    """(pool, quota id, counts, cost) for every pool a card could be metered in.

    Mechanical from the card's own name, which is why a card the table has never
    heard of — RTX PRO 6000 — gets its pools read like any other.

    AND THROUGH `quota_aliases`, WHICH IS THE SAME DEFECT THAT STARTED THIS. The
    pool work exists because RTX PRO 6000 was reported denied while a granted
    Spot allowance sat unread. Interpolating the DISPLAY name reproduces it one
    card over: H100-80GB builds `PREEMPTIBLE-NVIDIA-H100-80GB-GPUS-...`, and
    Google meters it as `...-NVIDIA-H100-GPUS-...`. Latent only because H100 Spot
    is zero today, and it would surface the day any is granted — a pool held and
    never read, which is where this began.

    Every spelling the card answers to, so a name that resolves under either one
    is found under both.
    """
    from .create import card_named

    card = card_named(gpu)
    names = [gpu, *(card.quota_aliases if card is not None else ())]
    pools: list[tuple[str, str, bool, str]] = []
    for name in dict.fromkeys(n.upper() for n in names):
        stem = f"NVIDIA-{name}-GPUS-per-project-region"
        pools += [
            (SPOT, f"PREEMPTIBLE-{stem}", True, SPOT_COST),
            (COMMITTED, f"COMMITTED-{stem}", False, _COMMITTED_COST),
            (WORKSTATION, f"NVIDIA-{name}-VWS-GPUS-per-project-region",
             False, _VWS_COST),
        ]
    return pools


def pools_for(
    gpu: str, quotas: list[dict], preferences: list[dict] | None = None, *,
    region: str | None = None,
    on_demand: "Readiness | CardSummary | None" = None,
) -> list[Pool]:
    """Every pool this project reports for one card, on-demand first.

    `on_demand` is the row the ordinary table already computed, passed in rather
    than recomputed so the two can never disagree about the same card.
    """
    found: list[Pool] = []
    if on_demand is not None:
        # `Readiness` carries a quota id and `CardSummary` does not — it is a
        # collapse of rows that can span several ids, so there is no single one
        # to name. Both are accepted because both are rendered, and the id here
        # is for display rather than for any decision made below.
        found.append(Pool(ON_DEMAND, getattr(on_demand, "quota_id", ""),
                          on_demand.limit, on_demand.status, counts=True,
                          where=getattr(on_demand, "where", None)
                          or getattr(on_demand, "region", "")))

    standing = asks(preferences)
    reported = {q.get("quotaId") or "" for q in quotas}
    for name, quota_id, counts, cost in _pool_ids(gpu):
        if quota_id not in reported:
            continue
        rows_here = [row for row in _raw_rows(quota_id, quotas)
                     if not region or _applies(row.where, list(row.locations), region)]
        if not rows_here:
            continue
        limit = max(row.limit for row in rows_here)
        if holds_quota(limit):
            status: Status = "ready"
        elif any(ask.state == "pending" and ask.quota_id == quota_id
                 for ask in standing):
            status = "pending"
        elif any(ask.state == "denied" and ask.quota_id == quota_id
                 for ask in standing):
            status = "denied"
        else:
            status = "none"
        places = {place for row in rows_here for place in row.locations}
        if len(places) == 1:
            pool_where = next(iter(places))
        elif places:
            pool_where = f"{len(places)} regions"
        else:
            pool_where = rows_here[0].where
        found.append(Pool(name, quota_id, limit, status, counts, cost,
                          where=pool_where))
    return found


def best_pool(pools: list[Pool]) -> Pool | None:
    """The pool a person should actually use, or None if none can start a box.

    MOST FAVOURABLE WINS, across pools, by the same `_ORDER` the per-card and
    per-region collapses use — `ready (Spot)` beats `denied (on-demand)` because
    the user can run the thing. One statement of "most favourable", three axes.
    """
    counting = [pool for pool in pools if pool.counts]
    if not counting:
        return None
    return min(counting, key=lambda pool: (_ORDER[pool.status],
                                           pool.name != ON_DEMAND))


def _raw_rows(quota_id: str, quotas: list[dict]) -> list[Row]:
    """Rows of one quota by id, bypassing `friendly_name`'s on-demand filter."""
    found: list[Row] = []
    for quota in quotas:
        if (quota.get("quotaId") or "") != quota_id:
            continue
        for info in quota.get("dimensionsInfos") or []:
            dimensions = info.get("dimensions") or {}
            locations = info.get("applicableLocations") or []
            explicit = dimensions.get("region") or dimensions.get("zone")
            where = explicit or (locations[0] if len(locations) == 1
                                 else (where_label(locations) if locations else "global"))
            try:
                limit = int((info.get("details") or {}).get("value"))
            except (TypeError, ValueError):
                limit = 0
            found.append(Row(quota_id, "", where, limit, tuple(locations),
                             tuple(sorted(dimensions.items()))))
    return found


@dataclass(frozen=True)
class Target:
    """What a quota request has to name: an id, and the dimensions that pin it.

    Two shapes, and the tool must handle whichever a project reports rather than
    hardcoding either. Both verified against the live API with `--validate-only`:

      per-card   quota_id NVIDIA-A100-GPUS-per-project-region, dimensions {}
                 — granted across all 43 regions in one grant, so naming a region
                   would narrow a request that does not need narrowing
      family     quota_id GPUS-PER-GPU-FAMILY-per-project-region,
                 dimensions {gpu_family: NVIDIA_H100, region: us-central1}
                 — genuinely per region, so both dimensions are required
      ceiling    quota_id GPUS-ALL-REGIONS-per-project, dimensions {}
    """

    quota_id: str
    dimensions: tuple[tuple[str, str], ...] = ()

    @property
    def dims(self) -> dict[str, str]:
        return dict(self.dimensions)

    @property
    def by_family(self) -> bool:
        return _FAMILY_DIMENSION in self.dims


def resolve(gpu: str, quotas: list[dict], *, region: str | None = None) -> str | None:
    """Turn `l4` into the quota id to request. None if this project reports none.

    Kept for the callers that want only an id. `resolve_target` is the one to use
    when the answer will be SUBMITTED, because an id alone cannot express a
    family card — `GPUS-PER-GPU-FAMILY-per-project-region` without a `gpu_family`
    dimension is a request about no card in particular.
    """
    target = resolve_target(gpu, quotas, region=region)
    return target.quota_id if target else None


def needs_family(quota_id: str) -> bool:
    """Does a REQUEST for this quota have to name a `gpu_family`?

    Beside `needs_region` and read the same way — off the id, because the id is
    where Google says so. `GPUS-PER-GPU-FAMILY-per-project-region` meters five
    cards on this project and distinguishes them by dimension alone, so a request
    without one is not a request for any card in particular.
    """
    name = _SCOPE_SUFFIX.sub("", quota_id or "").upper().replace("_", "-")
    return name.startswith(_NOT_A_CARD)


def needs_region(quota_id: str) -> bool:
    """Does a REQUEST for this quota have to name a region?

    Read off the id, because the id is where Google says so: anything ending
    `-per-project-region` defines a `region` dimension, and the API requires
    every defined dimension to be set. `GPUS-ALL-REGIONS-per-project` defines
    none and must be sent with no dimensions at all.

    THE GRANT AND THE REQUEST ARE DIFFERENT QUESTIONS, and conflating them is
    what made this necessary. `quota list` shows the A100 grant as "all regions"
    — one `dimensionsInfos` entry across 43 `applicableLocations` — which is true
    of the allowance and says nothing about how it is ASKED for. Sending
    `NVIDIA-A100-GPUS-per-project-region` with no dimensions was rejected on a
    real submission:

        INVALID_ARGUMENT: Dimension values must be set for all the dimensions
        (except "user" and "resource" if they are defined) defined for the quota.

    Confirmed by the shape of what is already on the project: the L4 preference
    `a0e3b926-...` carries `region=us-central1` despite the L4 grant covering
    every region.
    """
    return quota_id.lower().endswith(_REGION_SCOPED)


def resolve_target(
    gpu: str, quotas: list[dict], *, region: str | None = None,
    pin_region: bool = False, family: str | None = None,
    preferred_region: str | None = None,
) -> Target | None:
    """What to ask Google for, to get more of this card on this project.

    PER-CARD FIRST, FAMILY SECOND, and read from what the project returns rather
    than from a table. Both shapes are live: this project meters L4 and A100 under
    per-card ids and H100 only under the family id, while Google's own CLI guide
    uses the family form as its example. A tool that assumes either one is wrong
    on half the projects it meets.
    """
    per_card = [row for row in _for_card(gpu, quotas)
                if not row.dims.get(_FAMILY_DIMENSION)]
    for row in per_card:
        if region and not _applies(row.where, list(row.locations), region):
            continue
        # A REGION IS REQUIRED WHENEVER THE ID SAYS SO. This sent no dimensions
        # for every per-card quota, reasoning that the grant covers all 43
        # regions in one go and narrowing it would scope the request
        # permanently to somewhere nobody works. The reasoning is sound about
        # the GRANT and wrong about the REQUEST — see `needs_region` — and a
        # real submission rejected it while `--validate-only` had passed the
        # identical request three times.
        #
        # `pin_region` survives for the case it was really about: somebody who
        # typed `--region europe-west4` gets europe-west4 rather than a derived
        # region. It no longer decides WHETHER a region is sent.
        if not needs_region(row.quota_id):
            return Target(row.quota_id)
        where = region or preferred_region
        if not where:
            # Same F11 fallback as the family branch below: only reached when
            # there is nothing to derive from, and it takes whichever location
            # the API listed first.
            where = next((place for place in row.locations), None)
        if not where:
            return None
        return Target(row.quota_id, (("region", where),))

    # THE FAMILY SPELLING IS GIVEN, NOT GUESSED. `gpu_family` is an enum value
    # Google defines — `NVIDIA_RTX_PRO_6000`, `NVIDIA_H100_MEGA` — and matching
    # it by turning it into `RTX-PRO-6000` and comparing that to a card's display
    # name is guesswork that happens to work for the H100 because the table
    # carries an `H100` alias. The card table states the exact value, so a
    # request is built from a fact rather than from a coincidence of spelling.
    #
    # `quota list` is deliberately NOT built this way: it names a row from its own
    # dimension, so a card the table has never heard of is still reported. Display
    # derives from the project; REQUESTS come from the table.
    found = [row for row in rows(quotas)
             if family and row.dims.get(_FAMILY_DIMENSION) == family]
    if not found:
        # A record the API returned with no `dimensionsInfos` at all produces no
        # rows, and is not a reason to refuse to ask for the card — `resolve` is
        # how a request gets BUILT, and a project that reports the id can be
        # asked about it. `readiness` deliberately still shows nothing for such a
        # record: there is no allowance in it to report.
        for quota in _prefer_region_scope_ids(
            [q for q in quotas if matches(gpu, q.get("quotaId") or "")]
        ):
            if not (quota.get("dimensionsInfos") or []):
                return Target(quota.get("quotaId") or "")
        return None
    # Genuinely per region, so one is required. The row carrying the whole region
    # list is the one to copy the family spelling from; the region comes from the
    # caller, who knows where the box is wanted.
    where = region or preferred_region
    if not where:
        # F11: WHICHEVER ROW SORTED FIRST, which on this project was asia-east1 —
        # nothing about the project or the user points there, while `setup`
        # derives us-central1 for the same card and `create` picks by measured
        # latency. Three surfaces, three regions, and this one files a permanent
        # preference in the least useful of them. The caller's `preferred_region`
        # is the same derivation `setup` uses; this fallback only runs when there
        # is nothing to derive from.
        where = next(
            (row.dims["region"] for row in found if row.dims.get("region")), None)
    if not where:
        where = next((loc for row in found for loc in row.locations), None)
    if not where:
        return None
    return Target(found[0].quota_id, tuple(sorted(
        {_FAMILY_DIMENSION: family, "region": where}.items())))


def _prefer_region_scope_ids(records: list[dict]) -> list[dict]:
    """`_prefer_region_scope` for whole records, for the no-rows fallback above."""
    region_scoped = [r for r in records
                     if not (r.get("quotaId") or "").lower().endswith(_ZONE_SCOPED)]
    return region_scoped or records


def global_target(quotas: list[dict]) -> Target | None:
    """The project-wide ceiling, to ask for a raise to it.

    Separate from `resolve_target` only because the ceiling has no region
    dimension and must never be handed one — `setup` learned that by building a
    request Google rejects, from a menu it offered itself.
    """
    for row in _prefer_region_scope(
        [row for row in rows(quotas) if row.gpu == GLOBAL_ALLOWANCE]
    ):
        return Target(row.quota_id)
    return None


def global_quota_id(quotas: list[dict]) -> str | None:
    target = global_target(quotas)
    return target.quota_id if target else None


def available_gpus(quotas: list[dict]) -> list[str]:
    """Every card name this project could ask for, for error messages.

    Read off ROWS. Reading records asked `friendly_name` for an id, which is None
    for the family quota, so H100, H100-MEGA, H200, B200 and RTX-PRO-6000 were
    missing from every "ask for one of:" line on a project that meters all five.
    """
    return sorted({row.gpu for row in rows(quotas) if row.gpu})


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


def holds_quota(limit: int | None) -> bool:
    """Does this limit let you start anything? UNLIMITED counts; 0 and None do not.

    `-1` IS NOT "LESS THAN 1". It is "the question does not apply", and
    `limit > 0` is a comparison written for magnitudes meeting a sentinel — so
    three surfaces read an unlimited grant as no grant at all. `quota list`
    printed `L4  -1  none — request it`, an instruction to file an irrevocable
    request for a card the project holds without limit, while `setup` said
    "granted — unlimited" about the same row in the same minute.

    Sentinel-versus-magnitude, which is absent-versus-zero wearing a number: a
    value with a special meaning meets an operator that does not know it has one.
    `allowance()` knew; its consumers did not. One predicate so the next consumer
    cannot get it wrong either.
    """
    return limit is not None and (limit == UNLIMITED or limit > 0)


def _region_name(location: str) -> str:
    """`us-central1-a` -> `us-central1`. A region is returned unchanged.

    Zone-scoped quota lists zones; region-scoped quota lists regions. Both end up
    in the same set, so both are reduced to the region, which is the unit a
    grant is actually made in.

    ONE IMPLEMENTATION, in `zones`. This was the third identical copy — the
    second-site class waiting to happen, since the next correction to how a zone
    is recognised would have landed on one of the three.
    """
    from .zones import region_of

    return region_of(location)


def _region_names(row: Row) -> set[str]:
    """Every region one row grants a non-zero allowance in."""
    # `not holds_quota` rather than `== 0`: the question is "does this row grant
    # anything", and UNLIMITED grants everything.
    if not holds_quota(row.limit):
        return set()
    places = list(row.locations) or (
        [row.where] if not spans_many(row.where) and row.where != "global"
        else [])
    return {_region_name(place) for place in places if place and place != "global"}


def known_regions(quotas: list[dict]) -> set[str]:
    """Every region this project's quota records name — the API's own universe.

    Forty-three on this project. It exists so a typo can be called a typo:
    `--region not-a-region` reported "this project has no l4 quota in
    not-a-region", which says the region is real and the quota is missing, and
    both halves are false. Conflating "lookup found nothing" with "there is
    nothing there" is the same failure this module keeps circling, and here it
    led straight into recommending a region that sells no GPUs.
    """
    from .zones import region_of

    # REGIONS, and a project's quota records carry ZONES too — one
    # `NVIDIA-L4-GPUS-per-project-zone` row names 130 of them. Swept up raw, the
    # count came out at 174 and the sentence around it says "regions", while
    # `--by-region` in the same output said 43. `region_of` leaves a region
    # unchanged, so this is a narrowing and never an invention.
    # `global` IS NOT A REGION. It is where the project-wide ceiling lives, and
    # nobody can ask for quota there; counting it made the live figure 44 in a
    # sentence whose next clause is a region name.
    return {region_of(place) for row in rows(quotas) for place in row.locations
            if place != "global"}


def regions_with_quota(gpu: str, quotas: list[dict]) -> list[str]:
    """Regions where this project could start `gpu` today, as far as quota knows.

    GRANTED, not metered — `_region_names` drops a row whose limit is zero, which
    is right for this question and wrong for the other one. See
    `regions_metered`, which exists because the two were being read as the same
    thing.
    """
    granted: set[str] = set()
    for row in _for_card(gpu, quotas):
        granted |= _region_names(row)
    return sorted(granted)


def regions_metered(gpu: str, quotas: list[dict]) -> list[str]:
    """Every region this project has a quota row for `gpu` in, at any value.

    METERED IS NOT GRANTED, and reading one as the other put a false sentence on
    the irrevocable path and withheld the only remedy that worked:

        h100: africa-south1 does not offer this card — Google sells it in 20
              regions, none of them metered by this project

    The project meters H100 in all forty-three. `regions_with_quota` answers
    "where could a box start today", so it drops zero-limit rows — and the two
    answers differ exactly when the limit is zero, which is every card anybody
    would ever run `quota request` for. The card that most needed a region to ask
    in was the one guaranteed not to be offered one.
    """
    metered: set[str] = set()
    for row in _for_card(gpu, quotas):
        places = list(row.locations) or (
            [row.where] if not spans_many(row.where) and row.where != "global"
            else [])
        metered |= {_region_name(place) for place in places
                    if place and place != "global"}
    return sorted(metered)


def allowance(gpu: str, quotas: list[dict], *, region: str | None = None) -> int | None:
    """The largest grant this project holds for a card. None if it reports none.

    `UNLIMITED` (-1) is passed through as itself rather than flattened to a big
    number, so a caller can say "unlimited" instead of inventing a ceiling.
    """
    best: int | None = None
    for row in _for_card(gpu, quotas):
        if region and not _applies(row.where, list(row.locations), region):
            continue
        if row.limit == UNLIMITED:
            return UNLIMITED
        if best is None or row.limit > best:
            best = row.limit
    return best


# --- CPU quota: real for N1, and NOT the gate it was first taken for ----------
#
# A GPU VM can consume CPU quota. On this project two readings looked decisive:
# `A2-CPUS-per-project-region` is 0 in every region, and an H100's
# `a3-highgpu-8g` needs 208 vCPU against a general pool of 200 and a
# `CPUS-ALL-REGIONS-per-project` of 32.
#
# BOTH NUMBERS ARE REAL AND THE CONCLUSION DRAWN FROM THEM WAS INVENTED. This
# module was built to gate every GPU card on them, and asserted it on live data
# until somebody checked. Google's resource-usage documentation is explicit:
#
#   "To create A2 VMs, you only need to have the required NVIDIA A100 GPU quotas.
#    You don't need to request CPU quotas."
#
#   "To create A4X Max, A4X, A4, A3, G4, and G2 VMs, you only need to have the
#    required NVIDIA GB300, GB200, B200, H200, H100, RTX PRO 6000, or L4 GPU
#    quotas for the VM type. You don't need to request CPU quotas."
#
# So `A2-CPUS` reading 0 does not stop an A100, and 208 vCPU against a 200-vCPU
# pool does not stop an H100. Reading two zeros and inferring a gate produced a
# tool that told users their card was blocked for a reason that is not the
# reason — worse than no check at all, because it sends the next person to fix
# the wrong thing.
#
# WHAT SURVIVES, because it is documented rather than inferred: N1 is not on that
# list. T4, V100, P100, P4 and K80 all attach to N1 and do consume
# `CPUS-per-project-region` — 200 here against 8 vCPU per box, so the check is
# real and currently silent, which is the honest state for a gate that exists and
# is not biting.
#
# Enforcement cannot be tested from here without creating an instance, so this
# follows the vendor's documentation rather than the inference. That is the
# transferable part: **a quota reading 0 is not evidence that it is enforced.**
#
# THREE SHAPES, and which applies is read from the project rather than assumed —
# the same lesson the GPU quotas taught:
#
#   a dedicated family id   `A2-CPUS-per-project-region`. Only A2 has one here.
#   a family DIMENSION      `CPUS-PER-VM-FAMILY-per-project-region`, keyed on
#                           `vm_family`. It exists on this project and carries
#                           C3D, C4, N4 and friends — no A2, G2, N1 or A3 — so
#                           GPU families do not use it TODAY. Read it anyway.
#   the general pool        `CPUS-per-project-region`, which is what N1 falls
#                           back to here.
_CPU_WAIVED_FAMILIES = frozenset({"A2", "A3", "A4", "A4X", "G2", "G4"})


def cpu_quota_applies(machine_type: str) -> bool:
    """Does creating this machine consume CPU quota at all?"""
    return machine_family(machine_type) not in _CPU_WAIVED_FAMILIES


_CPU_FAMILY_DIMENSION = "vm_family"
_CPU_FAMILY_QUOTA = "CPUS-PER-VM-FAMILY"
_GENERAL_CPU = "CPUS-per-project-region"
CPU_CEILING = "CPUS-ALL-REGIONS-per-project"


def machine_family(machine_type: str) -> str:
    """`a2-highgpu-1g` -> `A2`. The family a machine type's CPU quota is keyed on."""
    head, _, _ = (machine_type or "").partition("-")
    return head.upper()


def _cpu_rows(quota_id: str, quotas: list[dict]) -> list[Row]:
    """Rows of one CPU quota record, named by their own `vm_family` where they have one."""
    found: list[Row] = []
    for quota in quotas:
        if (quota.get("quotaId") or "") != quota_id:
            continue
        for info in quota.get("dimensionsInfos") or []:
            dimensions = info.get("dimensions") or {}
            locations = info.get("applicableLocations") or []
            explicit = dimensions.get("region") or dimensions.get("zone")
            where = explicit or (locations[0] if len(locations) == 1
                                 else (where_label(locations) if locations else "global"))
            try:
                limit = int((info.get("details") or {}).get("value"))
            except (TypeError, ValueError):
                limit = 0
            found.append(Row(quota_id, dimensions.get(_CPU_FAMILY_DIMENSION, ""),
                             where, limit, tuple(locations),
                             tuple(sorted(dimensions.items()))))
    return found


def cpu_target(machine_type: str, quotas: list[dict], *,
               region: str | None = None) -> Target | None:
    """Which CPU quota a machine type is metered against, in whichever shape.

    METERED AGAINST, not gated by. This answers where the number lives; whether
    it constrains anything is `cpu_quota_applies`, and for A2, A3, A4, G2 and G4
    the answer is no. The two were conflated once already and the result was a
    tool naming a gate that does not exist.
    """
    family = machine_family(machine_type)
    ids = {q.get("quotaId") or "" for q in quotas}

    dedicated = f"{family}-CPUS-per-project-region"
    if dedicated in ids:
        return Target(dedicated, (("region", region),) if region else ())

    for row in _cpu_rows(f"{_CPU_FAMILY_QUOTA}-per-project-region", quotas):
        if row.gpu == family:
            return Target(row.quota_id, tuple(sorted(
                {_CPU_FAMILY_DIMENSION: family,
                 "region": region or row.dims.get("region") or ""}.items()))
                if region or row.dims.get("region") else ())

    if _GENERAL_CPU in ids:
        return Target(_GENERAL_CPU, (("region", region),) if region else ())
    return None


def cpu_allowance(machine_type: str, quotas: list[dict], *,
                  region: str | None = None) -> int | None:
    """vCPU this project may spend on this machine type. None if it reports none.

    The smallest of the gates that apply, not the largest: every one of them has
    to accommodate the machine, so the binding one is the answer.
    """
    if not cpu_quota_applies(machine_type):
        return UNLIMITED
    target = cpu_target(machine_type, quotas, region=region)
    if target is None:
        return None
    family = machine_family(machine_type)

    best: int | None = None
    for row in _cpu_rows(target.quota_id, quotas):
        if target.quota_id.startswith(_CPU_FAMILY_QUOTA) and row.gpu != family:
            continue
        if region and not _applies(row.where, list(row.locations), region):
            continue
        if row.limit == UNLIMITED:
            return UNLIMITED
        if best is None or row.limit > best:
            best = row.limit
    return best


def cpu_ceiling(quotas: list[dict]) -> int | None:
    """`CPUS-ALL-REGIONS-per-project` — 32 here, which no H100 can ever fit."""
    best: int | None = None
    for row in _cpu_rows(CPU_CEILING, quotas):
        if row.limit == UNLIMITED:
            return UNLIMITED
        if best is None or row.limit > best:
            best = row.limit
    return best


def global_allowance(quotas: list[dict]) -> int | None:
    """`GPUS_ALL_REGIONS` — the project-wide ceiling across every card.

    Not a card you can pick, and the limit that most often actually bites: it is
    **1** on this project, so a second GPU box cannot start while the first one
    is running, whatever the per-card grant says. None when the project reports
    no such quota at all.

    EVERY matching row is read and then reduced. This returned on the FIRST
    match, so the answer depended on the order gcloud happened to list the
    records in — which is not a contract gcloud offers — and the project carries
    two of them, `GPUS-ALL-REGIONS-per-project` and its `-per-project-zone`
    copy, which map to the same card name. The zone-scoped copy is -1, so
    whenever it sorted first the one limit that governs every create on this
    project read as UNLIMITED.

    Two defences, because the ordering between rows was not the only way in:

    **The zone-scoped copy drops out when a region-scoped row exists**, which is
    what `_for_card` already did for the per-card path, for the same reason.

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
    best: int | None = None
    unlimited = False
    for row in _prefer_region_scope(
        [row for row in rows(quotas) if row.gpu == GLOBAL_ALLOWANCE]
    ):
        if row.limit == UNLIMITED:
            unlimited = True
        elif best is None or row.limit > best:
            best = row.limit
    if best is not None:
        return best
    return UNLIMITED if unlimited else None
