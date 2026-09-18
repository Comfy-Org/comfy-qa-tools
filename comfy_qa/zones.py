"""Which zone to try, and in what order.

Nobody should have to type a zone. Picking one by hand is how you end up asking
for a card in a region the project has no grant in, which fails after the
instance name is already taken, or in a zone that never offers that machine type
at all.

The order is decided in four steps, and the first one is the one that is easy to
get wrong:

1. **Regions where this project actually holds quota for that card.** Quota is
   granted per region. Ranking by distance alone confidently picks a zone where
   nothing can start, and the refusal arrives as a quota error that reads like a
   permissions problem. `quota.regions_with_quota` reads the live grant.

2. **Zones in those regions that offer both the card and the machine type.** Two
   read-only gcloud calls, `accelerator-types list` and `machine-types list`.
   Neither says whether there is one *free* — nothing does — but both say
   whether there could ever be one, which is the half that can be checked in
   advance.

3. **Ranked by latency measured from this machine, and by where the fleet
   already is.** No API reports the first. "Iowa is far from London" is true and
   says nothing about which of three Iowa zones is fastest today, and the machine
   this runs on may be behind a VPN that makes geography a lie. So it is
   measured: one TCP connect per candidate region, cached beside the host list.

   A round trip from the laptop is not the only thing that speaks for a region,
   though, and on its own it is the wrong prior — see `_fleet_first`. A region
   that already runs one of your boxes is lifted to second, behind the nearest
   one, so an ordinary create is no slower and a create that has to fall through
   goes where the rest of the fleet lives rather than one town over.

4. **Tried in order, one region at a time, falling through on a stockout.**
   Google publishes no "is there room" endpoint, so capacity is try-and-see, and
   the budget is six real creates of about a minute each. A GPU stockout is very
   often the whole region, so `_spread` asks every region once before it asks any
   region twice — six attempts, six independent answers. That part lives in
   `create.py`, because it is the part that spends money.

## Why `compute.<region>.rep.googleapis.com` and not the obvious names

The obvious candidates do not work, and this was measured rather than assumed
(2026-08-28, from the machine this was written on):

    us-central1-run.googleapis.com          172.217.112.4   203 ms
    europe-west1-run.googleapis.com         172.217.112.4   206 ms
    asia-east1-run.googleapis.com           172.217.112.4   209 ms
    southamerica-east1-run.googleapis.com   172.217.112.4   205 ms

Every `<region>-<service>.googleapis.com` name resolves to the *same* anycast
Google Front End address, so a TCP connect measures the distance to the nearest
GFE and nothing else. All four look identical because they are the same host.

Google's regional endpoints — `<service>.<region>.rep.googleapis.com` — resolve
per region and terminate in it:

    compute.europe-west1.rep.googleapis.com    34.190.117.243   219 ms
    compute.us-central1.rep.googleapis.com     34.190.254.177   311 ms
    compute.asia-northeast1.rep.googleapis.com 34.190.13.219    433 ms
    compute.australia-southeast1.rep.googleapis.com 34.190.67.159 474 ms

Distinct addresses, and an ordering that matches where the regions are. It is
also the endpoint for the exact service being used, which is the closest thing
to measuring the round trip that a `gcloud compute instances create` will make.
"""

from __future__ import annotations

import json
import math
import os
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .config import DEFAULT_CONFIG_PATH

# Google's regional endpoint for the Compute Engine API. See the module docstring
# for why this name and not `<region>-compute.googleapis.com`.
ENDPOINT = "compute.{region}.rep.googleapis.com"
ENDPOINT_PORT = 443

# A connect that has not answered in this long is not going to change the
# ranking, and four regions timing out should not add a minute to a create.
PROBE_TIMEOUT = 2.0

# What an unreachable region scores. Sorts last without being dropped: a region
# whose endpoint refuses a connection may still hold the only quota you have.
UNREACHABLE = 9999.0

# Measured once and reused. A network path does not change minute to minute, and
# re-timing forty-three regions on every create would cost more than it saves.
CACHE_NAME = "zone-latency.json"
CACHE_TTL = 7 * 24 * 60 * 60

# What the numbers in the cache mean. Bump this whenever `ENDPOINT` changes, or
# whenever anything else changes what a stored millisecond is a measurement *of*.
#
# This is not bookkeeping. The mistake in the docstring above — timing
# `<region>-<service>.googleapis.com`, which is one anycast address for every
# region — produces a file of four near-identical numbers that look exactly like
# measurements and rank nothing. Cached, an unversioned file of them would have
# outlived the fix by a week and made the ordering look like it was working.
CACHE_VERSION = 1

# How wide to probe. Every one of these threads is asleep on a TCP connect — no
# CPU, no lock, nothing to contend over — so the pool wants to be as wide as the
# list, and 8 was costing six sequential waves on a live project's forty-three
# regions. Measured, 43 regions with a 250 ms round trip: 1.52s at 8 workers,
# 0.26s at 43.
#
# The case that actually hurts is the degraded one. A region that does not answer
# costs the full PROBE_TIMEOUT, and `_worth_keeping` deliberately refuses to
# cache an unreachable region — so when the network is bad this is paid again on
# every single create, not once a week. All forty-three timing out: 12.02s at 8
# workers, 2.01s at 43.
#
# Capped rather than simply `len(regions)` so a pathological region list cannot
# ask for an unbounded number of threads.
PROBE_WORKERS = 64

# How many regions get their zones looked up. Latency ranks every region the
# project has quota in — forty-three of them on a live project — and asking
# `machine-types list` about a hundred and thirty zones is slow for an answer
# that only ever uses the first few. Widened automatically if the nearest few
# offer nothing.
#
# It has to be LARGER than `MAX_ATTEMPTS`, and it was smaller. `_spread` gives
# each attempt a different region, so a near set of four could only ever fill
# four of the six attempts with distinct regions and the last two went back for
# second zones. Four also meant that from London the whole search was Europe,
# permanently: a create that stocked out reported six European zones and never
# contacted a region on another continent, which is the defect this number is
# half of. Eight, so the six attempts have somewhere to come from even when two
# of the near regions turn out not to offer the machine type.
NEAREST_REGIONS = 8

# How many zones a create will actually try before giving up. Each attempt is a
# real instance create that takes the better part of a minute when it fails, so
# an uncapped fall-through is a command that looks hung.
MAX_ATTEMPTS = 6


@dataclass(frozen=True)
class Ordering:
    """The zones to try, in order, and how that order was arrived at.

    Kept as data rather than printed on the spot so `--dry-run` can show exactly
    what the real run would do, from the same object the real run uses.
    """

    zones: tuple[str, ...]
    regions: tuple[str, ...]
    latency: dict[str, float] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    fall_through: bool = True

    # Every region this project may use where Google offers the card at all,
    # ranked — which is not the same as the regions `zones` came out of, and the
    # difference is the whole reason this field exists.
    #
    # `build` needs to know whether "every zone tried is out of capacity" means
    # everywhere, or means the handful this looked at. It can count the regions
    # it tried, and `regions` tells it where quota reaches, but quota reaches
    # forty-three regions and Google offers an L4 in a fraction of them — so
    # "6 of 43" would be a second true sentence with a false reading. This is the
    # honest denominator: places that hold quota AND offer the card, which is
    # known from the one project-wide `accelerator-types list` that has already
    # been made.
    #
    # Empty when nothing ranked anything — `--zone` is one zone by request, and
    # a caller that says nothing about the wider world must not have `build`
    # invent a wider world on its behalf.
    offering: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.zones)

    def lines(self) -> list[str]:
        """One line per zone, with the measurement that put it there."""
        out = []
        for zone in self.zones:
            region = region_of(zone)
            score = self.latency.get(region)
            if score is None:
                out.append(zone)
            elif score >= UNREACHABLE:
                out.append(f"{zone}  (did not answer — tried last)")
            else:
                out.append(f"{zone}  ({score:.0f} ms to {region})")
        return out


def region_of(zone: str) -> str:
    """`us-central1-a` -> `us-central1`. A region passed in is returned as-is."""
    head, _, tail = zone.rpartition("-")
    return head if head and len(tail) == 1 and tail.isalpha() else zone


# --- how far away is it, really -------------------------------------------


def _connect(region: str, *, timeout: float = PROBE_TIMEOUT) -> float:
    """Milliseconds to open a TCP connection to a region's Compute endpoint.

    Nothing is sent and nothing is read: the handshake is the measurement, and
    the socket is closed immediately. No credential is involved — this is the
    same connection any HTTPS client would make before it authenticated.
    """
    address = ENDPOINT.format(region=region)
    started = time.perf_counter()
    try:
        with socket.create_connection((address, ENDPOINT_PORT), timeout=timeout):
            return (time.perf_counter() - started) * 1000
    except OSError:
        # DNS that does not resolve, a refused connection, a timeout. All three
        # mean "cannot say", and all three sort last rather than disqualifying.
        return UNREACHABLE


def measure(regions: list[str], *, probe=None,
            workers: int = PROBE_WORKERS) -> dict[str, float]:
    """Time every region, in parallel. Serial, forty-three of these is a minute."""
    probe = probe or _connect
    regions = list(dict.fromkeys(regions))
    if not regions:
        return {}
    if len(regions) == 1:
        return {regions[0]: probe(regions[0])}
    with ThreadPoolExecutor(max_workers=min(workers, len(regions))) as pool:
        return dict(zip(regions, pool.map(probe, regions)))


def cache_path(config: Path | None = None) -> Path:
    """Beside the host list, because that is where this tool's state lives."""
    return (config or DEFAULT_CONFIG_PATH).parent / CACHE_NAME


def _read_cache(path: Path, *, now: float) -> dict[str, float]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # A cache that cannot be read is not an error — it is a cache miss. The
        # only cost is measuring again.
        return {}
    if not isinstance(raw, dict):
        return {}
    if raw.get("version") != CACHE_VERSION:
        # Written by something that measured a different thing. A miss costs one
        # round of probing; trusting it costs a week of a meaningless ordering.
        return {}
    measured_at = raw.get("at")
    try:
        if now - float(measured_at) > CACHE_TTL:
            return {}
    except (TypeError, ValueError):
        return {}
    entries = raw.get("regions")
    if not isinstance(entries, dict):
        return {}
    kept = {}
    for region, score in entries.items():
        try:
            value = float(score)
        except (TypeError, ValueError):
            continue
        if not _is_a_round_trip(value):
            continue
        kept[str(region)] = value
    return kept


def _is_a_round_trip(value: float) -> bool:
    """Is this a number a connection could actually have taken?

    Two things get in here that are not measurements. `json.loads` accepts a bare
    `NaN`, and `float(nan)` is a perfectly good float — but NaN compares false
    against everything, so a single one makes `sorted` return an order that
    depends on the input order, and the one thing the ranking promises is that a
    dry run and the run that follows it try the same zones.

    And nothing connects in less than no time. A file that says a region does
    puts that region first on every create from now on, and nothing else in this
    tool would ever question it.
    """
    if not math.isfinite(value):
        return False
    return 0.0 <= value <= UNREACHABLE


def _worth_keeping(scores: dict[str, float]) -> dict[str, float]:
    """The measurements, without the failures to measure.

    `UNREACHABLE` is "could not say", and caching it says it for a week. One
    create run behind a dropped VPN would otherwise write it for every region and
    keep it until the TTL expired, leaving the ordering this module exists to
    provide as alphabetical — silently, and long after the network came back.
    """
    return {region: score for region, score in scores.items()
            if _is_a_round_trip(score) and score < UNREACHABLE}


def _write_cache(path: Path, scores: dict[str, float], *, now: float) -> None:
    body = json.dumps(
        {"version": CACHE_VERSION, "at": now, "regions": _worth_keeping(scores)},
        indent=2, sort_keys=True,
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written beside and renamed over, because two `create`s at once is the
        # ordinary case here — one Linux box, one Windows box, two terminals. A
        # half-written file is only a cache miss, but a cache that misses forever
        # is a minute added to every create from now on. `os.replace` is atomic
        # on the same filesystem, and the temp name carries the pid so the two
        # runs do not collide on that either.
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(body, encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        # Not being able to cache a measurement is not a reason to fail a create.
        # Take the half-written file with us, though: a `.tmp` left beside the
        # host list is the kind of thing somebody later has to decide about.
        try:
            temporary.unlink(missing_ok=True)
        except (OSError, UnboundLocalError):
            pass


def latencies(
    regions: list[str], *, config: Path | None = None, probe=None,
    now: float | None = None, path: Path | None = None,
) -> dict[str, float]:
    """Every region's round trip, measuring only the ones not already known."""
    now = time.time() if now is None else now
    store = path or cache_path(config)
    known = _read_cache(store, now=now)
    missing = [region for region in regions if region not in known]
    if missing:
        known = {**known, **measure(missing, probe=probe)}
        _write_cache(store, known, now=now)
    return {region: known.get(region, UNREACHABLE) for region in regions}


# --- what does Google actually offer where ---------------------------------


def zones_offering(entries: list[dict], accelerator: str) -> list[str]:
    """Zones from an `accelerator-types list` payload that offer exactly this card.

    The name is re-checked here rather than trusted to the server-side filter.
    `--filter=name=nvidia-l4` is exact today and gcloud warns, on every call,
    that its `=` operator is changing to match more than it does now — at which
    point `nvidia-l4` would also return `nvidia-l4-vws`, a virtual-workstation
    allowance that does not let you start an ordinary GPU instance.
    """
    found = []
    for entry in entries or []:
        if (entry.get("name") or "") != accelerator:
            continue
        zone = (entry.get("zone") or "").rstrip("/").rsplit("/", 1)[-1]
        if zone and zone not in found:
            found.append(zone)
    return found


def zones_with_machine_type(entries: list[dict], machine_type: str) -> list[str]:
    """The same, for a `machine-types list` payload."""
    found = []
    for entry in entries or []:
        if (entry.get("name") or "") != machine_type:
            continue
        zone = (entry.get("zone") or "").rstrip("/").rsplit("/", 1)[-1]
        if zone and zone not in found:
            found.append(zone)
    return found


def _nearest(count: int) -> str:
    """`the nearest region offers` / `the 8 nearest regions offer`.

    A note is prose and gets read as prose. "the 1 nearest regions offer no
    nvidia-l4" is a sentence somebody has to stop and re-read.
    """
    if count == 1:
        return "the nearest region offers"
    return f"the {count} nearest regions offer"


def _fleet_first(ranked: list[str], home: set[str]) -> list[str]:
    """The ranked regions, with the ones already running a box lifted to second.

    Latency is measured from the laptop, and the laptop is not where the box
    lives. When every other machine in the host list is in `us-central1`, that
    region has a claim no round trip from London can express: the new box will
    talk to its neighbours over Google's own network, its models are already
    there, and — the part that costs money to learn — it has demonstrably had
    capacity for this project recently.

    Lifted to SECOND rather than to first, which is the whole judgement here.
    The nearest region keeps the first attempt, so on an ordinary day — capacity
    exists, the first create succeeds — nothing about this is slower and nothing
    changed. It is only when the nearest region has none that the order matters,
    and that is exactly when "where the rest of the fleet already runs" is worth
    more than eighty milliseconds.
    """
    if not home or len(ranked) < 2:
        return list(ranked)
    lifted = [region for region in ranked[1:] if region in home]
    if not lifted:
        return list(ranked)
    return [ranked[0], *lifted, *[region for region in ranked[1:] if region not in home]]


def _spread(zones: list[str], regions: list[str]) -> list[str]:
    """One zone per region, in region order, before any region's second zone.

    The order used to be "every zone of the nearest region, then every zone of
    the next", which spends a fixed budget of six real creates on as few as two
    regions. A GPU stockout is very often the whole region — the six attempts
    that produced this change were `europe-west2-a`, `-b`, then all three of
    `europe-west4` — so three of those attempts bought no information at all and
    cost a minute each.

    Round-robin instead, so consecutive attempts land in different places and
    six attempts are six independent questions. The trade is real and is
    accepted: when a stockout is zonal rather than regional, the second zone of
    the nearest region is now tried after a further region rather than before
    it, which can put the box a little further away than it strictly had to be.
    A slower box beats no box.

    Stable, like the sort it replaces: a dry run and the real run that follows
    it have to try the same zones in the same order.
    """
    grouped: dict[str, list[str]] = {}
    for zone in zones:
        grouped.setdefault(region_of(zone), []).append(zone)
    # Regions the caller did not rank go last rather than being dropped. Nothing
    # produces that today; dropping a zone silently is the wrong way to find out
    # that something started to.
    order = [region for region in regions if region in grouped]
    order += [region for region in grouped if region not in set(order)]
    spread: list[str] = []
    while any(grouped[region] for region in order):
        for region in order:
            if grouped[region]:
                spread.append(grouped[region].pop(0))
    return spread


def _named(regions: list[str]) -> str:
    """`us-east5`, `us-east5 or europe-west2`, `the 43 regions you hold quota in`.

    Named while there are few enough to name, counted after that — the point is
    that the sentence describes what was searched rather than what exists.
    """
    places = list(dict.fromkeys(regions))
    if not places:
        return "any region this project holds quota in"
    if len(places) == 1:
        return places[0]
    if len(places) <= 3:
        return f"{', '.join(places[:-1])} or {places[-1]}"
    return f"the {len(places)} regions this project holds quota in"


def choose(
    gc, project: str, *, accelerator: str, machine_type: str,
    regions: list[str], config: Path | None = None, probe=None,
    nearest: int = NEAREST_REGIONS, limit: int = MAX_ATTEMPTS,
    path: Path | None = None, fleet: list[str] | None = None,
) -> Ordering:
    """The zones to try, best first. Read-only: nothing here creates anything.

    `regions` is the set the project holds quota in — the caller reads that from
    `quota.regions_with_quota`, because deciding it needs the whole quota
    payload the caller has already fetched for its own gate.

    `fleet` is where the boxes that already exist are — zones or regions, from
    the host list. It does not narrow anything and it cannot add a region the
    project has no quota in; it only lifts a region that is already allowed.
    See `_fleet_first` for why a laptop's round trip is the wrong prior on its
    own.
    """
    if not regions:
        return Ordering(zones=(), regions=())

    scores = latencies(regions, config=config, probe=probe, path=path)
    ranked_regions = sorted(regions, key=lambda region: (scores.get(region, UNREACHABLE), region))

    offered = zones_offering(gc.accelerator_types(project, accelerator), accelerator)
    in_quota = [zone for zone in offered if region_of(zone) in set(regions)]
    if not in_quota:
        return Ordering(
            zones=(), regions=tuple(ranked_regions), latency=scores,
            # THE REGIONS ACTUALLY LOOKED AT, which the caller has already
            # narrowed. `--region us-east5` searched one region and reported a
            # project-wide fact — false of eighteen stocked regions out of the
            # forty-three this project meters, and the same command with no
            # `--region` found six zones seconds later. A message describing a
            # set the caller narrowed is the derived-set class in prose.
            notes=(f"no zone in {_named(regions)} offers {accelerator}",),
        )

    # Where the card can actually be had, ranked. The denominator `build` uses
    # to say how much of the world it really looked at — see `Ordering.offering`.
    reaches = {region_of(zone) for zone in in_quota}
    offering = tuple(region for region in ranked_regions if region in reaches)

    home = {region_of(place) for place in (fleet or []) if place}
    settled = _fleet_first(ranked_regions, home & set(ranked_regions))

    # Look at the nearest few regions first, and widen only if they turn up
    # nothing. Asking `machine-types list` about a hundred and thirty zones is
    # slow for an answer whose first five entries are the only ones ever used.
    #
    # `dict.fromkeys` rather than a plain tuple: on a project holding quota in
    # four regions or fewer the two widths are the same number, and the loop then
    # made the identical `machine-types list` call twice before giving up.
    notes: list[str] = []
    reason = ""
    widths = list(dict.fromkeys((min(nearest, len(settled)), len(settled))))
    for width in widths:
        near = set(settled[:width])
        candidates = [zone for zone in in_quota if region_of(zone) in near]
        if not candidates:
            # Not the same reason as finding candidates and none of them having
            # the machine type, and saying so mattered: the note blamed the
            # machine type either way, so widening past a near region that simply
            # has no L4 read as "europe-west4 offers no g2-standard-8", which it
            # does offer.
            reason = reason or f"{_nearest(nearest)} no {accelerator}"
            continue
        usable = zones_with_machine_type(
            gc.machine_types(project, _spread(candidates, settled)[:limit * 3], machine_type),
            machine_type,
        )
        both = [zone for zone in candidates if zone in set(usable)]
        if both:
            picked = _spread(both, settled)[:limit]
            if width > widths[0]:
                notes.append(f"{reason}, so this looked further afield")
            notes.extend(_scope(picked, settled, home, offering))
            return Ordering(
                zones=tuple(picked), regions=tuple(ranked_regions), latency=scores,
                notes=tuple(notes), offering=offering,
            )
        reason = reason or f"{_nearest(nearest)} no {machine_type}"

    return Ordering(
        zones=(), regions=tuple(ranked_regions), latency=scores,
        notes=(f"{accelerator} is offered in {len(in_quota)} zone(s) this project has "
               f"quota in, and none of them offers {machine_type}",),
    )


def _scope(picked: list[str], settled: list[str], home: set[str],
           offering: tuple[str, ...]) -> list[str]:
    """What the order does not say about itself, said out loud.

    Two things, and both were invisible before a create had already spent six
    minutes failing. The order looks authoritative — it is a numbered list with
    milliseconds beside each line — and nothing on it admitted that it was drawn
    from a handful of the regions this project may use, nor that the second
    entry was there because a box already lives there rather than because it is
    near. A `--dry-run` is where someone would want to find that out.
    """
    said = []
    chosen = list(dict.fromkeys(region_of(zone) for zone in picked))
    lifted = [region for region in chosen if region in home and region != settled[0]]
    if lifted:
        said.append(f"{', '.join(lifted)} already runs a box, so it is tried early "
                    f"rather than in distance order")
    if len(chosen) < len(offering):
        said.append(f"{len(chosen)} of the {len(offering)} regions with quota and a "
                    f"card were looked at, nearest first — name another with "
                    f"--region, or see them all with: comfy-qat quota list --by-region")
    return said
