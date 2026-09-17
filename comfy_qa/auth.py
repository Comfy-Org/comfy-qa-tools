"""`comfy-qa-tools auth` — is this machine ready to reach Google Cloud, and as whom?

This tool never implements authentication and never holds a credential. gcloud
already owns Google Cloud auth, keeps credentials in its own store and handles
refresh; wrapping that would mean caching tokens on a QA laptop for no gain.

So `auth` answers a question and hands over the command that fixes the answer.
No token is ever printed, logged, or written to a file here.
"""

from __future__ import annotations

import json
import shlex
import time
from dataclasses import asdict, dataclass
from typing import Annotated, Callable, Optional

import typer

from . import say
from .quota import (
    GLOBAL_ALLOWANCE,
    Target,
    available_gpus,
    request_plan,
    request_value,
    UNLIMITED,
    held_value,
    asks,
    row_name,
    asks_about,
    friendly_name,
    known_regions,
    spans_many,
    needs_family,
    needs_region,
    resolve_target,
    regions_metered,
    regions_with_quota,
    matches,
    readiness,
    resolve,
    summarise,
)
from .gcloud import (
    Gcloud,
    GcloudError,
    console_quota_url,
    quota_request_command,
)

app = typer.Typer(help="Google Cloud sign-in, billing and GPU quota.")
quota_app = typer.Typer(help="GPU quota: what you have, and how to ask for more.")
app.add_typer(quota_app, name="quota")

# How long `request` will wait before handing you back — for the whole command,
# however many cards it asked for. Approval can take days, so waiting forever is
# not an option; the same command re-enters the wait. Both are read at call time,
# so a test can shorten them without a 30-minute suite.
WAIT_TIMEOUT_SECONDS = 30 * 60
POLL_SECONDS = 30


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    fix: str | None = None


def run_checks(gc: Gcloud) -> list[Check]:
    """Check readiness in order, stopping at the first failure.

    Stopping is deliberate: every later check depends on the earlier ones, so
    reporting five failures when one thing is wrong is noise.
    """
    results: list[Check] = []

    if gc.available() is None:
        results.append(Check(
            "gcloud", False, "not installed or not on PATH",
            "https://cloud.google.com/sdk/docs/install",
        ))
        return results
    results.append(Check("gcloud", True, "on PATH"))

    try:
        account = gc.active_account()
    except GcloudError as exc:
        results.append(Check("account", False, str(exc), exc.fix or "gcloud auth login"))
        return results
    if not account:
        results.append(Check("account", False, "nobody signed in", "gcloud auth login"))
        return results
    results.append(Check("account", True, account))

    try:
        project = gc.current_project()
    except GcloudError as exc:
        results.append(Check("project", False, str(exc), exc.fix or "gcloud config set project <id>"))
        return results
    if not project:
        results.append(Check(
            "project", False, "no project set", "gcloud config set project <id>",
        ))
        return results
    results.append(Check("project", True, project))

    try:
        billing = gc.billing_enabled(project)
    except GcloudError as exc:
        results.append(Check(
            "billing", False, str(exc),
            exc.fix or f"https://console.cloud.google.com/billing/linkedaccount?project={project}",
        ))
        return results
    if not billing:
        results.append(Check(
            "billing", False, f"no billing account linked to {project}",
            f"https://console.cloud.google.com/billing/linkedaccount?project={project}",
        ))
        return results
    results.append(Check("billing", True, f"linked to {project}"))

    try:
        quotas = gc.gpu_quotas(project)
    except GcloudError as exc:
        results.append(Check("gpu quota", False, str(exc), exc.fix or "comfy-qat quota"))
        return results

    # Read through the same filter the rest of the tool uses. Counting any quota
    # with a non-zero value meant a project holding nothing but a committed or
    # preemptible allowance passed this check — and then could not start a box.
    # Setup was fixed for exactly this; status was reporting green beside it.
    request_fix = "comfy-qat quota request --gpu <type> --region <region>"
    usable = [row for row in readiness(quotas) if row.usable]
    cards = [row for row in usable if row.gpu != GLOBAL_ALLOWANCE]
    if not usable:
        results.append(Check(
            "gpu quota", False,
            "zero GPU quota on this project — no GPU instance can start",
            request_fix,
        ))
        return results
    if not cards:
        # The project-wide ceiling is not a card. On its own it starts nothing.
        results.append(Check(
            "gpu quota", False,
            "a project-wide allowance only, no specific card granted",
            request_fix,
        ))
        return results
    # Collapsed per card, not per region: Google meters some cards region by
    # region, so the raw rows read `K80=1, K80=1, K80=1, K80=1` — four entries
    # for one card nobody wants — while the L4 you would actually use fell off
    # the end of a silent truncation at four. Say how many there are, and never
    # cut without saying so.
    #
    # And DRIVABLE, not merely granted. Quota for a pre-Turing card starts an
    # instance and cannot start a working one — the open kernel module this tool
    # installs needs a GSP (see `create.GSP_ARCHITECTURES`) — so counting one
    # here reported a project ready when nothing it holds can generate a pixel.
    # That is the same defect this block was already fixed for once, with
    # committed and preemptible allowances, one layer further down.
    from .create import card_named

    def drivable(name: str) -> bool:
        card = card_named(name)
        return card is None or card.has_gsp

    summary = [card for card in summarise(cards) if card.usable]
    stranded = [card.gpu for card in summary if not drivable(card.gpu)]
    summary = [card for card in summary if drivable(card.gpu)]
    if not summary:
        results.append(Check(
            "gpu quota", False,
            f"only {', '.join(stranded)} — cards this tool cannot drive, because "
            f"the open NVIDIA kernel module it installs needs a GPU System "
            f"Processor and only Turing and newer have one",
            "comfy-qat quota request --gpu t4,l4 --region <region>",
        ))
        return results
    shown = ", ".join(f"{card.gpu}={card.limit}" for card in summary[:4])
    if len(summary) > 4:
        shown += f" (+{len(summary) - 4} more)"
    detail = f"{shown} — {say.count(len(summary), 'card')} ready"
    if stranded:
        # Named, not silently dropped: the grant is real, and somebody looking at
        # the console will otherwise see a card here that this row does not.
        detail += f"; {', '.join(stranded)} granted but not drivable"
    results.append(Check("gpu quota", True, detail))

    # LAST, deliberately. Tunnel speed is readiness and belongs here — `setup`
    # installs NumPy into gcloud's own python now, so this row is the catch-up
    # for anyone who ran setup before it did. But `status` prints the fix for the
    # FIRST failing check, so a missing NumPy placed any earlier would mask a
    # missing account or an unlinked billing project behind advice about a
    # slower tunnel. It is the only check here that blocks nothing.
    from .setup import gcloud_numpy

    wanted = gcloud_numpy(gc)
    if wanted is None:
        results.append(Check("numpy", True, "gcloud's tunnel is on the fast path"))
    else:
        blocked = wanted.blocked
        # `ok=True` when setup CANNOT install it. `status` exits 1 on any failed
        # check, so a root-owned gcloud python — which setup deliberately skips
        # rather than escalating to sudo — gave a permanent non-zero exit with a
        # fix that loops back to the command that already declined.
        #
        # A slower tunnel is not a readiness failure. The row still says so, and
        # the row is the point; the exit code was claiming something it should
        # not.
        #
        # `setup --no-numpy` is NOT this case and deliberately still fails the
        # check: nothing declined there, the user did, and `comfy-qat setup`
        # without the flag installs it. A fix line that works is worth an exit
        # code; a fix line that cannot work is not.
        results.append(Check(
            "numpy", bool(blocked),
            f"not installed and {blocked} — tunnels are slower than they need "
            "to be" if blocked else
            "not installed — every tunnel is slower than it needs to be",
            "comfy-qat setup",
        ))

    return results


def _offered_flag(region, gpu, absent_here, unchecked):
    """True, False, or None for "not checked" — which covers both no `--region`
    and an id that could not be resolved. Two ways of not knowing, one answer."""
    # L13: `any (global)` is a ceiling, not a card, so the availability check
    # skips it — and it then fell through to `true`, reporting a check that was
    # deliberately never made. The same record already says `drivable: null` for
    # it; this is the sibling field, and it has the same answer.
    if gpu == GLOBAL_ALLOWANCE:
        return None
    if not region or gpu in unchecked:
        return None
    return gpu not in absent_here


def _match_accelerator(gpu: str, stocked: dict[str, set[str]]) -> list[str]:
    """The accelerator ids that are this card, MATCHED against what exists.

    Never constructed. `"nvidia-" + gpu.lower()` was right for RTX-PRO-6000 and
    B200 and wrong for H100-MEGA (`nvidia-h100-mega-80gb`) and H200
    (`nvidia-h200-141gb`) — and since a constructed id matches nothing anywhere,
    those two claimed "not offered here" in every region, including the ones that
    stock them. There is no rule to fix: `nvidia-tesla-a100` and
    `nvidia-a100-80gb` are one family, `nvidia-l4` has no prefix and no suffix,
    and the capacity suffix appears on some cards and not others.

    Exact first, then a segment-boundary prefix so `nvidia-h100` cannot claim
    `nvidia-h100-mega-80gb`. An ambiguous match returns everything it found and
    the caller unions the zones; nothing at all returns empty, and the caller
    says the check could not be made rather than inventing a verdict.
    """
    # A SEARCH KEY, never an answer. `"nvidia-" + gpu.lower()` is only ever
    # looked UP in `stocked`; it is not returned unless the catalogue confirms
    # it. The version of this that returned it directly is what claimed H200 and
    # H100-MEGA do not exist anywhere. A grep for this line finds the string that
    # caused the defect and the code that no longer commits it — which is why the
    # difference is spelled out here rather than left to the reader.
    key = "nvidia-" + gpu.lower()
    if key in stocked:
        return [key]
    return sorted(name for name in stocked
                  if name.startswith(f"{key}-") and not name.endswith("-vws"))


def _regions_stocking(
    gc, project: str, names, 
) -> dict[str, set[str] | None]:
    """Which regions actually SELL each card. `None` means the check failed.

    ONE implementation, because `quota list --region` had this and `quota
    request --region` did not — and `quota request` is the one that files the
    irrevocable thing. It exited 0 for africa-south1, which stocks zero NVIDIA
    accelerators of any kind and is one of nine such regions inside the
    forty-three-region quota universe, then printed a fix line recommending
    exactly that region because it was `applicableLocations[0]`.

    A `None` value is "I could not look", never "it is not there" — the
    distinction that six defects in this feature came from collapsing. Callers
    must render it as unchecked and must not refuse on it.
    """
    from .create import card_named

    wanted = {n for n in names if n != GLOBAL_ALLOWANCE}
    try:
        catalogue = _catalogue(gc, project)
    except GcloudError:
        return {name: None for name in wanted}

    if not catalogue:
        # AN EMPTY CATALOGUE IS NOT AN EMPTY WORLD. Zero rows from
        # `accelerator-types list` says the lookup told us nothing; it is not
        # evidence that Google sells no GPU anywhere on Earth, which is what
        # reading it as fact would assert. Same rule as a raised error, one level
        # up, and it is the level the first draft missed — every caller passing a
        # region against a catalogue-less fixture was refused outright.
        return {name: None for name in wanted}

    stocked: dict[str, set[str]] = {}
    for entry in catalogue:
        stocked.setdefault(entry.get("name", ""), set()).add(
            entry.get("zone", "").rsplit("/", 1)[-1])

    out: dict[str, set[str] | None] = {}
    for name in wanted:
        known = card_named(name)
        ids = ([known.accelerator] if known else _match_accelerator(name, stocked))
        if not ids:
            out[name] = None
            continue
        out[name] = {_region_of(zone) for i in ids for zone in stocked.get(i, set())}
    return out


def askable_regions(name: str, quotas: list[dict], sells,
                    refused: "set[str] | tuple[str, ...]" = ()) -> list[str]:
    """Where this card could ACTUALLY be requested: metered, stocked, not refused.

    ONE ANSWER TO ONE QUESTION, because two surfaces were composing it
    separately. `create` built its suggestion from `regions_metered` alone and
    offered `africa-south1` — which stocks zero NVIDIA accelerators, so the
    command it printed exits 2, and it appeared twice in its own alternatives
    because nothing de-duplicated the list either.

    METERED alone is where a request is possible; METERED AND STOCKED is where a
    granted request buys something that can start. The second is what a remedy
    should name, and it is the intersection this function exists to stop being
    recomputed by hand.

    `sells` may be an unlooked `Availability`, in which case the stocking half is
    unknown and the metered list is returned as-is — "I could not check" must
    narrow nothing rather than empty the answer.
    """
    metered = set(regions_metered(name, quotas)) - set(refused)
    where = sells.where.get(name) if getattr(sells, "looked", False) else None
    return sorted(metered & set(where)) if where is not None else sorted(metered)


def _refuse_if_unsold(name: str, region: str | None, sells, quotas) -> None:
    """Stop if `region` sells no `name`. One function, both entry paths.

    Extracted because the `--gpu` path had this and the `--quota-id` path did
    not, so the identical request was refused through one flag and filed
    permanently through the other. A guard reachable from one entry point and not
    another is a second-site by construction, and the repair is one callable
    rather than a second copy of the check.
    """
    here = sells.where.get(name) if sells.looked else None
    if not region or here is None or region in here:
        return
    # BOTH SETS, because the sentence names both. `here` is where Google SELLS
    # the card; `regions_metered` is where this project METERS it. Reporting the
    # first while saying the second printed "this project meters it in 18 regions
    # that do" about a count that would have been 18 on a project metered in one.
    #
    # METERED, not granted: `regions_with_quota` drops zero-limit rows, and every
    # card you would be requesting is at zero — that is why you are asking — so
    # that intersection came out empty and the branch reported "none of them
    # metered by this project" about a project metered in all forty-three, then
    # withheld the one fix line that works.
    both = askable_regions(name, quotas, sells)
    say.fail(
        f"{name}: {region} does not offer this card"
        + (f" — this project meters it in {len(both)} region"
           f"{'s' if len(both) != 1 else ''} that do" if both else
           f" — Google sells it in {len(here)} regions, none of them metered by "
           f"this project" if here else " in any zone"),
        fix=say.fix(
            *([f"comfy-qat quota request --gpu {name} --region {both[0]}"
               f"  # metered here and stocked there"] if both else []),
            f"comfy-qat quota list --region {region}"
            "  # what this region actually offers"),
        code=2,
    )


def _region_universe(gc, project: str, quotas: list[dict]) -> set[str]:
    """Every region that exists, as far as this project can tell.

    TWO SOURCES, because neither alone answers the question. The quota records
    name the regions this project is metered in; the accelerator catalogue names
    every region Google sells a GPU in. A region in neither is a typo. A region
    in either is real — and a REAL region a project happens to hold no quota rows
    for is not a typo, which is what using the quota universe alone would have
    called it. `africa-south1` is in the quota set and not the catalogue; most
    regions are in the catalogue and, on a narrow project, not the quota set.

    Shared so `setup`, `quota list --region` and `quota request --region` cannot
    disagree about whether a region exists — which is how `setup` came to be the
    only one of the three with no check at all.
    """
    universe = known_regions(quotas)
    try:
        catalogue = _catalogue(gc, project)
    except GcloudError:
        return universe
    return universe | {_region_of(entry.get("zone", "").rsplit("/", 1)[-1])
                       for entry in catalogue}


@dataclass(frozen=True)
class Availability:
    """Which regions sell each card — or the fact that nobody looked.

    THE CLASS BEHIND SEVERAL ROUNDS OF THIS. Availability used to be a plain
    `dict` initialised to `{}`, computed only under `--by-region`, and consumed
    by a branch reading `if per_region and stocks`. That expression cannot tell
    "never computed" from "computed and found nothing" — so when `--json` emitted
    the `by_region` array WITHOUT `--by-region` setting the flag that computes
    it, every row degraded silently to a confident `ready`. The machine surface
    reported K80 ready in twenty-five regions while the table beside it said "not
    offered here" for all twenty-five.

    This is ABSENT-VERSUS-ZERO ONE LEVEL UP: not a value that is missing versus a
    value that is zero, but a COMPUTATION that never ran versus one that ran and
    found nothing. Everything that applies to the first applies here — they are
    different facts, one of them means "I do not know", and a truthful surface
    says so rather than taking the cheerful reading.

    So the two states are different objects. `not_checked()` is the only way to
    build the first, `offers()` refuses to answer from it, and an empty `where`
    now unambiguously means "looked, and this project meters nothing".
    """

    looked: bool
    where: dict[str, set[str] | None]

    @classmethod
    def not_checked(cls) -> "Availability":
        return cls(looked=False, where={})

    def __bool__(self) -> bool:
        # Falsy when nobody looked, so call sites that still read it as a plain
        # truthiness test keep the conservative behaviour rather than inverting.
        return self.looked

    def offers(self, name: str, place: str) -> bool | None:
        """True, False, or None for "cannot be answered".

        None covers three different reasons and they are all "I do not know":
        nobody looked, the catalogue could not be read for this card, or the card
        is one no accelerator id was found for.
        """
        if not self.looked:
            return None
        places = self.where.get(name)
        return None if places is None else place in places


def _catalogue(gc, project: str) -> list[dict]:
    """`accelerator-types list`, fetched ONCE per command.

    543 rows and about a second. Two callers want it — the availability check and
    the region universe — and adding the second doubled the calls, which a test
    counting them caught immediately. Cached on the `Gcloud` instance because
    that is the object whose lifetime is the command.
    """
    cache = getattr(gc, "_accelerator_cache", None)
    if cache is None:
        cache = {}
        try:
            gc._accelerator_cache = cache
        except AttributeError:      # a fake that forbids attributes
            return gc.accelerator_types(project)
    if project not in cache:
        cache[project] = gc.accelerator_types(project)
    return cache[project]


def _stop_on_region_shape(region: str | None) -> None:
    """Refuse a region that is wrong on its face, before any API call."""
    wrong = region_shape_problem(region)
    if wrong:
        say.fail(wrong[0],
                 fix=say.fix(*wrong[1], "comfy-qat quota list --by-region  "
                             "# every region this project meters"),
                 code=2)


def region_shape_problem(region: str | None) -> tuple[str, list[str]] | None:
    """The half of `region_problem` that needs NO API call at all.

    Empty, zone-shaped and wrong-case are decidable from the string. Only "not
    one of this project's forty-three" needs the quota records — and running the
    whole check after that minute-long read meant a typo cost 51 seconds before
    anybody was told about it.
    """
    if region is None:
        return None
    if not region.strip():
        return ("--region was given but empty — if that came from a shell "
                "variable, it is unset",
                ["--region us-central1  # or leave the flag off to derive one"])
    # CASE IS NOT DECIDABLE FOR FREE, and it looked as though it were. Lowering
    # `US-CENTRAL99` gives `us-central99`, which is not a region either — so the
    # remedy would hand over a second wrong answer with more confidence than the
    # first. Telling case-wrong from merely-wrong needs the universe, so that
    # rule lives in the half that has it. Empty and zone-shaped are genuinely
    # syntactic.
    if _region_of(region) != region:
        return (f"{region!r} is a zone; quota is metered per region",
                [f"--region {_region_of(region)}  # the region that zone is in"])
    return None


def region_problem(gc, project: str, quotas: list[dict], region: str | None,
                   *, membership: bool = True) -> tuple[str, list[str]] | None:
    """`(what is wrong, what to type)` if `region` is not a region, else None.

    ONE CHECK, THREE SURFACES. `quota list`, `quota request` and `setup` each
    grew their own, and the one that stayed local is the one that let a ZONE
    through — `quota list --region us-central1-a` answered "no GPU quotas
    reported" at exit 0 about a project holding six cards, because a local
    universe built from `applicableLocations` folds in the zone names that
    `-per-project-zone` rows carry.

    A confident empty answer is worse than a refusal: the typo path exits
    non-zero and says what is wrong, and this one hands a script a picture it
    will believe.

    Returns rather than raising, because `setup` stops by raising `SetupStopped`
    and the quota commands stop by `say.fail` — the QUESTION is shared even where
    the manner of stopping is not.
    """
    # `None` IS OMITTED; `""` IS TYPED AND EMPTY. These fell together out of one
    # truthiness test, so `--region ""` behaved exactly like leaving the flag off
    # — deriving a region and, on a real run, filing an irrevocable request into
    # it. The realistic source of an empty value is `--region "$REGION"` with the
    # variable unset, and somebody who typed the flag has said they care which
    # region; choosing one for them is the one reading that cannot be meant.
    # `None` IS OMITTED, and it left with the block that was extracted — so this
    # walked straight into `region.lower()` on a create with no `--region`.
    if region is None:
        return None
    # The free half first, so a caller that has not read quota yet gets the same
    # answer for the same inputs.
    shape = region_shape_problem(region)
    if shape:
        return shape
    universe = _region_universe(gc, project, quotas)
    if not universe or region in universe:
        return None

    # `membership=False` FOR `create`, and it is not an exemption. "This region
    # exists and this project has no quota for the card in it" is a question
    # `create` answers BETTER than this function can — it names the regions the
    # card IS held in, from the grant it just read. Telling that caller "no such
    # region" would replace a specific true answer with a vaguer one.
    #
    # The case rule below still applies, because `US-CENTRAL1` is not a question
    # about quota at all.
    if not membership:
        exact = [p for p in universe if p.lower() == region.lower()]
        return ((f"no region called {region!r} — regions are lower case",
                 [f"--region {exact[0]}"]) if exact else None)

    # CASE-INSENSITIVELY. `"US-CENTRAL1".split("-")[0]` is `"US"` and the
    # universe holds `"us"`, so a region wrong by nothing but case fell to the
    # generic advice while a zone — wrong by a whole segment — got an exact
    # suggestion.
    # Case-only errors first: an exact fold means the region is real and the user
    # typed it in the wrong case, which deserves a better sentence than a guess.
    exact = [p for p in universe if p.lower() == region.lower()]
    if exact:
        return (f"no region called {region!r} — regions are lower case",
                [f"--region {exact[0]}"])
    near = sorted(p for p in universe
                  if p.split("-")[0].lower() == region.split("-")[0].lower())
    return (f"no such region {region!r} — this project knows {len(universe)}, "
            f"and that is not one of them",
            ([f"--region {near[0]}  # did you mean one of {len(near)} in "
              f"{region.split('-')[0]}?"] if near else []))


def _region_of(place: str) -> str:
    """`us-central1-a` -> `us-central1`. A region passed in comes back unchanged."""
    head, _, tail = place.rpartition("-")
    return head if head and len(tail) == 1 and tail.isalpha() else place


def _record(row, limit, where, status, pool, drivable, cpu_blocked,
            offered_here=None, quota_id=None) -> dict:
    """One card as `--json` sees it, from the same resolved values the table uses.

    `asdict(row)` first so nothing the dataclass carries is lost, then the
    resolved fields over the top — the on-demand numbers are what they overwrite,
    and they are exactly what made the two surfaces disagree.
    """
    from dataclasses import asdict

    record = asdict(row)
    # NAMES THAT CARRY THEIR UNIT. `asked` was a GPU count sitting between two
    # region counts, one of them `never_asked_in` — so RTX-PRO-6000 reading
    # `asked: 1, refused_in: 2` looked like a contradiction until you knew the
    # units differed. Three sibling integers may not need a footnote to be read.
    record["asked_gpus"] = record.pop("asked", 0)
    record["refused_in_regions"] = record.pop("refused_in", 0)
    record["never_asked_in_regions"] = record.pop("never_asked_in", 0)
    # THE SAME VERDICT THE TABLE PRINTS. `offered_here` alone left `status`
    # saying "ready" about a card the region does not sell, so a script gating on
    # status launched something a human reading the table would not. The fact was
    # present and the verdict was not, which is this class's usual shape.
    if offered_here is False:
        status = "not offered here"
    # M5: THE ID THE LIMIT CAME FROM. The record carried the family id beside
    # `limit: 1` and `pool: "Spot"` — and that family id reports no value at all
    # for RTX PRO 6000, in any of its dimension rows. The 1 is the Spot pool's,
    # and the object never named the quota it came from. Limit, pool and id are
    # one statement or they are three contradicting ones.
    # BOTH ARRAYS. The guard was `... and "quota_id" in record`, and `record` is
    # `asdict(row)` — a `CardSummary` for the collapsed view, which has no such
    # field — so the assignment silently no-opped on one of the two and a
    # consumer reading `gpus[].quota_id` got a KeyError while the identically
    # named field worked in `by_region[]`. The comment justifying this field says
    # "limit, pool and id are one statement or they are three contradicting
    # ones"; on one array it was none at all.
    if quota_id is not None:
        record["quota_id"] = quota_id
    # L18: WHICH POOL THE COUNTS ARE ABOUT. `refused_in_regions: 2` sat beside
    # `pool: "Spot"`, and Spot has no refusals — the refusals are the on-demand
    # request's. The three counts always describe the on-demand pool; saying so
    # costs one field and stops the object asserting something false.
    from .quota import ON_DEMAND

    record.update(limit=limit, status=status, pool=pool,
                  counts_pool=ON_DEMAND,
                  drivable=drivable, cpu_blocked=cpu_blocked or None,
                  offered_here=offered_here)
    if "where" in record:
        record["where"] = where
    else:
        record["region"] = where
    return record


def _value_of(quota: dict) -> int:
    """Pull the effective limit out of a QuotaInfo, tolerating shape changes."""
    details = quota.get("dimensionsInfos") or []
    best = 0
    for entry in details:
        value = (entry.get("details") or {}).get("value")
        if isinstance(value, (int, str)):
            try:
                best = max(best, int(value))
            except (TypeError, ValueError):
                continue
    return best


@app.command("status")
def status_cmd(
    as_json: Annotated[bool, typer.Option("--json", help="Machine-readable output.")] = False,
) -> None:
    """Am I signed in, on which project, with billing and GPU quota?"""
    checks = run_checks(Gcloud())


    if as_json:
        say.result(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        for check in checks:
            say.check(check.ok, f"{check.name:<10} {check.detail}")
        failed = next((c for c in checks if not c.ok), None)
        if failed and failed.fix:
            # The rows are the answer and stay on stdout; the fix belongs to a
            # failure and goes where every other fix goes. This was the one
            # `to fix:` of fourteen that was printed on stdout.
            say.write_fix(failed.fix)

    if any(not c.ok for c in checks):
        raise typer.Exit(code=1)


@app.command("login")
def login_cmd() -> None:
    """Print the sign-in commands.

    `gcloud auth login` opens a browser and is interactive, so it is handed over
    rather than driven. Running it yourself also leaves you the repro trail.
    """
    say.result("run these, then `comfy-qat status`:\n")
    say.result("  gcloud auth login")
    say.result("  gcloud config set project <your-project-id>")
@quota_app.callback(invoke_without_command=True)
def quota_default(ctx: typer.Context) -> None:
    """Showing what you can run is the safe default."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(quota_list_cmd, as_json=False, region=None, by_region=False)


@quota_app.command("list")
def quota_list_cmd(
    region: Annotated[Optional[str], typer.Option("--region", help="Only this region.")] = None,
    by_region: Annotated[bool, typer.Option(
        "--by-region", help="One row per region instead of one per card.")] = False,
    as_json: Annotated[bool, typer.Option(
        "--json", help="Print JSON instead of the table: project, gpus (one per card), "
                       "by_region (one per card and place).")] = False,
) -> None:
    """What can I run today, what is waiting on Google, what did I never ask for."""
    # BEFORE THE MINUTE-LONG READ. Empty, zone-shaped and wrong-case need no API
    # call at all, and asking afterwards cost 51 seconds to reject a typo.
    _stop_on_region_shape(region)

    gc = Gcloud()
    try:
        project = _require_project(gc)
        # Google's quota API is slow enough that a silent minute reads as a hang,
        # so the step says how long it should take and then keeps saying it is
        # still there. On stderr, which is what keeps `--json` a document.
        with say.slow("reading quota", expect="about a minute"):
            # EVERY compute quota, not only the GPU ones. `readiness` names
            # only GPU rows, so the table is unchanged — but the CPU check below
            # needs the CPU records, and reading `gpu_quotas` here meant they
            # never arrived. The annotation was unreachable: dead code that
            # looked like a working guard, which is worse than an absent one.
            quotas = gc.compute_quotas(project)
            prefs = gc.quota_preferences(project)
    except GcloudError as exc:
        say.fail(exc, code=2)

    if region is not None:
        # F9: A TYPO READS AS "this project has almost no quota". `quota request`
        # validates the same flag and `create` warns about exactly this, so
        # `quota list` was the one surface that let it through with exit 0.
        #
        # AND IT WAS THE ONE THAT KEPT ITS OWN COPY of the check, which is how a
        # ZONE got through after the universe was unified: the local set folded
        # in `applicableLocations`, and `-per-project-zone` rows carry zones.
        # Calling the shared question rather than maintaining a second answer.
        wrong = region_problem(gc, project, quotas, region)
        if wrong:
            say.fail(wrong[0],
                     fix=say.fix(*wrong[1], "comfy-qat quota list --by-region  "
                                 "# every region this project meters"),
                     code=2)
    rows = readiness(quotas, prefs, region=region)
    cards = summarise(rows)

    # "ready" here has only ever meant one thing: Google will let you start it.
    # It said nothing about whether this tool can then bring the card up, and for
    # four of the cards it can order, it cannot — the driver it installs is the
    # open kernel module and those cards have no GSP. So a project holding P100
    # quota read `P100 1 ready` off this table, and `create --gpu p100` built a
    # billing box whose GPU never initialised. The grant and the driver are two
    # different questions and this table now answers both.
    from .create import card_named

    from .create import no_gsp, offered

    def undrivable(name: str) -> bool:
        """Known to the table, and the driver cannot bring it up. The GSP case."""
        return no_gsp(name)

    def drivable_flag(name: str):
        """`--json`'s `drivable`, from the one predicate. None for the ceiling,
        which is not a card and cannot be drivable either way."""
        if name == GLOBAL_ALLOWANCE:
            return None
        return offered(name)

    # CPU quota, on the surface where "ready" is printed — and ONLY for the
    # families that actually consume it. Google's resource-usage page says A2,
    # A3, A4, G2 and G4 VMs need no CPU quota at all, so `A2-CPUS` reading 0 is
    # not why an A100 will not start. N1 is not on that list, so T4, V100, P100,
    # P4 and K80 are genuinely gated here.
    #
    # `cpu_quota_applies` IS CHECKED TWICE ON PURPOSE. `cpu_allowance` already
    # waives the family, which covers the regional pool — but the project-wide
    # ceiling below is read from `cpu_ceiling`, which knows nothing about any
    # machine type and cannot waive anything. Without the explicit guard this
    # function kept announcing "H100-80GB — blocked by CPU quota (a3-highgpu-8g
    # needs 208 vCPU, the project-wide CPU ceiling is 32)" after the rest of the
    # false gate had been removed. The correction landed in `setup.py` and missed
    # here, which is the second-site failure `docs/tests-that-cannot-fail.md`
    # describes: the person who writes the fix is the worst placed to find the
    # other copy, because they know where they were looking.
    # A CARD'S STATE IS THE BEST OF ITS POOLS, not the state of the on-demand one.
    # `ready (Spot)` beats `denied (on-demand)` because the user can run the
    # thing — and reporting the on-demand refusal alone called a card denied that
    # this project can start today.
    from .quota import ON_DEMAND, best_pool, pools_for

    def pool_of(name: str, row):
        found = pools_for(name, quotas, prefs, region=region, on_demand=row)
        best = best_pool(found)
        return best, found

    def resolved(row):
        """One answer per card, rendered twice.

        THE TABLE AND `--json` DISAGREED IN THREE FIELDS on the same card in the
        same run — table `ready (Spot) 1`, JSON `"status":"denied","limit":0` —
        because the JSON was built from the on-demand `CardSummary` with only the
        winning pool's NAME bolted on, while the table resolved the pool at
        render time. A script gating on `--json` reached the opposite verdict to
        the human reading the table.

        And the row's own columns had the same split: LIMIT said the on-demand 0
        while STATUS said the Spot 1, and WHERE named the two regions the
        ON-DEMAND request was refused in while the Spot grant that made the row
        ready spans 43. Every column was about a different pool from the one the
        status named.

        So limit, place, status and pool are decided ONCE, here, and both
        renderings read the result.
        """
        best, found = pool_of(row.gpu, row)
        limit, where, status = row.limit, getattr(row, "where", None), row.status
        where = where if where is not None else row.region
        pool = best.name if best is not None else ON_DEMAND
        if best is not None and best.name != ON_DEMAND and best.status == "ready":
            limit, status = best.limit, best.status
            where = best.where
        return best, found, limit, where, status, pool

    def cpu_blocked(name: str) -> str:
        from .quota import (
            UNLIMITED, cpu_allowance, cpu_ceiling, cpu_quota_applies,
        )

        card = card_named(name)
        if card is None or not card.vcpus:
            return ""
        if not cpu_quota_applies(card.machine_type):
            return ""
        held = cpu_allowance(card.machine_type, quotas, region=region)
        if held is not None and held != UNLIMITED and held < card.vcpus:
            return f"{card.machine_type} needs {card.vcpus} vCPU, quota is {held}"
        ceiling = cpu_ceiling(quotas)
        if ceiling is not None and ceiling != UNLIMITED and ceiling < card.vcpus:
            return (f"{card.machine_type} needs {card.vcpus} vCPU, the "
                    f"project-wide CPU ceiling is {ceiling}")
        return ""

    # Both derived sets, computed here because BOTH renderings read them —
    # the table and `--json`. Defined below the `--json` branch, they were
    # a NameError for the machine-readable surface and nothing else.
    #
    # FROM THE REFUSALS THEMSELVES, not from `rows`. `rows` is already filtered by
    # `--region`, so a set whose entire purpose is "refused SOMEWHERE ELSE" was
    # computed from a view with everywhere else removed — it could only ever see
    # refusals in the region being asked about. `--region us-central1` therefore
    # printed "none — request it" for a card refused in europe-west4, while
    # `setup` declined to re-ask for that exact card and `--by-region` said
    # "refused elsewhere". Three surfaces, one card, one minute, and the
    # disagreeing one was the only one issuing an instruction that files
    # something Google will not let you withdraw.
    #
    # Keyed to the REGIONS, because "elsewhere" is the fact a reader then has to
    # go and look up.
    refused_regions: dict[str, set[str]] = {}
    for ask in asks(prefs):
        if ask.state != "denied":
            continue
        card = row_name(ask.quota_id, ask.dimensions)
        if card:
            refused_regions.setdefault(card, set()).add(ask.region or "")
    refused_somewhere = set(refused_regions)

    # QUOTA IS NOT AVAILABILITY, and this column was reporting one as the other.
    # `--region europe-west9` called seven cards ready in a region GCE offers
    # almost none of them in — K80 among them, and K80 no longer exists on GCE
    # anywhere. The GSP defect with a different cause: that was "ready about a
    # card that cannot work", this is "ready about a card that is not there".
    #
    # Checked only when a region was named. Then it is ONE call per card against
    # a list the project already answers for; without a region it would be a call
    # per card per region on top of a fifty-second read, and `create` — which
    # does check — is named in the footnote instead. What must not happen is
    # "ready" meaning two different things depending on a flag, so the default
    # view says outright that the column reports quota.
    absent_here: set[str] = set()
    unchecked: set[str] = set()
    # PER ROW FOR `--by-region`, because every row there already names the region
    # to test against and the catalogue is ONE call for all 543 rows. The reason
    # the collapsed view only checks under `--region` — "a call per card per
    # region on top of a fifty-second read" — simply does not apply: the cost is
    # the same single call either way.
    #
    # Without this the view asserted `ready` against nineteen NAMED regions for
    # K80, a card that is not in GCE's accelerator catalogue anywhere. The
    # footnote saying STATUS reports quota rather than availability is on this
    # view, and it is the wrong answer here: its remedy is "add --region", which
    # collapses the very view the reader asked for.
    stocks = Availability.not_checked()
    # The regions each card has a row of its OWN for, so a catch-all row can be
    # told what it covers: everything metered, minus these.
    named_rows: dict[str, set[str]] = {}
    for row in rows:
        if not spans_many(row.region) and row.region != "global":
            named_rows.setdefault(row.gpu, set()).add(row.region)
    # COMPUTED WHENEVER IT CAN BE EMITTED, which is the other half of the repair.
    # `--json` prints the `by_region` array unconditionally, so gating its input
    # on `--by-region` left the producer and the consumer behind different flags.
    # A field that exists in the output while its input was never gathered is the
    # bug; the flag decides both or neither.
    if (by_region or as_json) and not region:
        stocks = Availability(
            looked=True,
            where=_regions_stocking(gc, project, {c.gpu for c in cards}))
    if region:
        # A FAILED LOOKUP IS NOT A FACT. "I looked and it is not there" and "my
        # lookup found nothing" are different statements, and rendering the
        # second as the first is how a constructed id — `nvidia-h200` for a card
        # Google calls `nvidia-h200-141gb` — claimed a card does not exist in
        # every region on Earth. `_regions_stocking` returns None for that case
        # and this is where it becomes "unchecked" rather than "absent".
        for card, places in _regions_stocking(
                gc, project, {c.gpu for c in cards}).items():
            if places is None:
                unchecked.add(card)
            elif region not in places:
                absent_here.add(card)

    # "request it" is an INSTRUCTION and belongs only where it is true. It used
    # to be the text for every state that was not ready or pending, so six
    # refused requests on the live project read as an invitation to file all six
    # again — while `setup` was refusing to re-ask for exactly those cards.
    notes = {
        "ready": "ready",
        "pending": "pending — waiting on Google",
        "denied": "denied — Google refused this; asking again will not help",
        "partial": "partial",
        "none": "none — request it",
    }

    # What the footnote has to explain, filled in as rows are rendered so a pool
    # nobody holds is never explained. Keyed by pool name, so four cards holding
    # a workstation allowance produce one entry rather than four.
    explained: dict[str, str] = {}
    held_in: dict[str, list[str]] = {}

    # Which cards Google has refused SOMEWHERE, so a per-region row can say so.
    # `--by-region` said "none — request it" for A100 in europe-west4 six lines
    # below the same card reading "denied" in the collapsed table. Honest per
    # region — nobody asked there — and a reader who acts on it re-files a
    # request Google already refused. The card-level fact belongs on the row.




    def uncreatable(name: str, best=None) -> str:
        """Why `create` cannot spend this allowance, or "" if it can.

        Reads `create.unspendable`, shared with `setup`, so the two surfaces
        cannot tell different stories about the same card again.

        CALLED ON EVERY BRANCH THAT NAMES A CARD, which it was not. It hung off
        the rescued-by-another-pool branch alone, so B200, H200 and H100-MEGA —
        which have nothing in any pool, so nothing rescues them — printed a bare
        "none — request it". `create --gpu b200` answers "no card called 'b200'"
        and points back at this table, and `plan_quota` never asks for them
        either: a closed loop ending in an irrevocable request for a card the
        tool will never accept. "Request it" is the only instruction in this
        table and it was wrong on three of fourteen rows.
        """
        from .create import unspendable

        why = unspendable(name, best.name if best is not None else "")
        return f", {why}" if why else ""

    # Cards whose availability could not be decided for at least one row. The
    # caveat below is suppressed per ROW rather than per view because of these:
    # a bucket that is part stocked and part not is exactly as unchecked as a
    # view with no catalogue, and rendered as a bare `ready` it reads like a
    # check that passed.
    not_checked: set[str] = set()

    def _offered_in(name: str, place: str) -> bool | None:
        """Does THIS region sell this card? None when it cannot be answered.

        A bucket place (`any of 42`) names no single region, so there is nothing
        to test and the answer is "not checked" — the honest half of checking
        per row.
        """
        # THE ROW'S OWN PLACE, not the rendered label. `rendered` relabels a
        # bucket from `42 regions` to `any of 42` so nobody reads it as a region
        # — and testing `spans_many` against the RELABELLED string matched
        # nothing, so every bucket row was judged against a catalogue it cannot
        # be tested against. The rename broke the guard written beside it, in the
        # same change.
        if not stocks.looked:
            return None
        if name == GLOBAL_ALLOWANCE:
            # NOT APPLICABLE, NOT UNANSWERED. `any (global)` is not a card — the
            # first footnote in this same output says so — and its row covers one
            # place, `global`. Falling through to the lookup-failed branch put it
            # in a sentence reading "whose rows cover several regions at once",
            # both halves false. `drivable_flag` has carried this guard fourteen
            # lines up all along, for the same reason.
            return None
        places = stocks.where.get(name)
        if places is None:
            not_checked.add(name)
            return None
        if spans_many(place) or place == "global":
            # A BUCKET NAMES NO SINGLE REGION — and its MEMBERSHIP is derivable
            # anyway. A catch-all dimension row covers exactly the regions with
            # no row of their own, so it is the metered regions minus the named
            # siblings, and both of those are already in hand. P100, P4 and V100
            # each read `any of 19 ... ready` while every named row beneath them
            # said otherwise: not the K80 case (they ARE stocked somewhere they
            # are metered) and not unknowable either.
            # METERED, for the same reason: a bucket belonging to a card at
            # zero covered nothing, so it read as unanswerable rather than being
            # checked.
            metered = set(regions_metered(name, quotas))
            covered = metered - named_rows.get(name, set())
            if not covered:
                # Every metered region has a row of its own, so the bucket covers
                # nothing this project could use. Nothing to answer.
                return None
            if not (covered & places):
                return False
            if covered <= places:
                return True
            # Mixed: some of what it covers is stocked and some is not, and one
            # row cannot say both.
            not_checked.add(name)
            return None
        return place in places

    def status_of(name: str, state: str, limit: int, asked: int, row=None,
                  held: int | None = None, offered: bool | None = None) -> str:
        """One verdict, then supporting detail. Never a chain of verdicts.

        THE SHAPE, AND WHY IT IS THIS SHAPE. Caveats used to be appended to
        whatever the branches produced, so a card absent from the region read
        `ready — this region does not offer it — this tool cannot drive it`:
        three verdicts joined by em-dashes with the most misleading one first,
        and a reader scanning the column sees `ready`. Whatever a card that is
        not there is, it is not ready.

        So the verdict is chosen ONCE by precedence — the same move `_ORDER`
        makes for pools, with availability as another input to that decision
        rather than a suffix on its output — and everything after it is detail.

        AND THERE IS ONE TAIL. The pool branch used to return early, duplicating
        the tail badly: it skipped `undrivable` and `cpu_blocked` two rounds ago
        and skipped the availability check this round. Third occurrence of one
        structure, so the fix is where the branches rejoin rather than a third
        copy of the checks.
        """
        best, found = pool_of(name, row) if row is not None else (None, [])

        for pool in found:
            if pool.status != "ready" or pool.name == ON_DEMAND:
                continue
            explained[pool.name] = pool.cost
            held_in.setdefault(pool.name, []).append(name)

        won = (best is not None and best.name != ON_DEMAND
               and best.status == "ready")
        refused, never = (getattr(row, "refused_in", 0),
                          getattr(row, "never_asked_in", 0))
        blocked = uncreatable(name, best if won else None)

        # --- the verdict, in precedence order --------------------------------
        if offered is False or (offered is None and name in absent_here):
            # Google does not sell this card here, so no quota makes it runnable.
            # It leads the line because it is the answer to "can I run this".
            detail = []
            # L16: THE NUMBER THE COLUMN PRINTS. `limit` here is the raw
            # on-demand value — 0 for a card held only through Spot — so
            # RTX-PRO-6000 showed LIMIT 1 and then declined to explain it, while
            # every other limit-1 card said "1 of quota held". `held` is the
            # resolved figure, the one beside it on the line.
            shown_limit = held if held is not None else limit
            if shown_limit:
                detail.append(f"{shown_limit} of quota held")
            if blocked:
                detail.append(blocked.lstrip(", "))
            verdict = "not offered here"
            if detail:
                verdict += " — " + ", ".join(detail)
            return _with_tail(verdict, name)
        if won:
            verdict = f"ready ({best.name}) {best.limit} — {best.cost}"
            if refused:
                # L18: WHOSE REFUSALS. "ready (Spot) 1 — reclaimable mid-run,
                # refused in 2 regions" reads as the Spot grant having been
                # refused, and Spot has no refusals at all: the two are the
                # on-demand request's. The `where` column was corrected for
                # exactly this a round ago and the counts were left behind.
                verdict += (f", {ON_DEMAND} refused in {refused} "
                            f"{'region' if refused == 1 else 'regions'}")
            return _with_tail(verdict + blocked, name)
        if blocked and state == "none":
            # "request it" is the only instruction in this table, and a card
            # `create` will never accept must not be given it.
            return _with_tail(f"none —{blocked[1:]}", name)
        if state == "denied" and never:
            verdict = (f"denied — refused in {refused} "
                       f"{'region' if refused == 1 else 'regions'}, never asked "
                       f"in {never}")
        elif state == "none" and name in refused_somewhere:
            named = sorted(p for p in refused_regions.get(name, set()) if p)
            where_refused = ", ".join(named) if named else "elsewhere"
            verdict = (f"none here — refused in {where_refused}; ask only if "
                       f"this region is new")
        elif asked and state == "ready":
            verdict = f"ready — {limit} granted; a raise to {asked} was not"
        elif asked and state not in ("denied", "none"):
            verdict = f"{notes.get(state, state)} — {limit} of the {asked} asked for"
        else:
            verdict = notes[state]
        return _with_tail(verdict + blocked, name)

    def _with_tail(verdict: str, name: str) -> str:
        """The checks every row gets, applied in one place so no branch can skip
        them. Two have been lost to an early return already."""
        if undrivable(name):
            verdict += " — this tool cannot drive it"
        stopped = cpu_blocked(name)
        if stopped:
            verdict += f" — blocked by CPU quota ({stopped})"
        return verdict

    # Once, under the table, rather than a repeated parenthesis in the STATUS
    # column. The column says which rows; this says why, and what it costs to
    # ignore, which is the half a person needs before asking for more of one.
    def wrapped(text: str) -> None:
        """A footnote, wrapped. The GSP note is 334 characters on one line — the
        widest thing in this output by nearly 3x, and the only width guard in the
        suite excluded `note:` lines by construction, so nothing could see it."""
        import textwrap

        say.result("")
        for line in textwrap.wrap(text, width=96):
            say.result(line)

    def footnote() -> None:
        ceiling = next((c for c in cards if c.gpu == GLOBAL_ALLOWANCE), None)
        if ceiling is not None and ceiling.limit >= 0:
            # F12: IT WAS ONE MORE ROW, sorted in among the cards, with nothing
            # saying it caps the ready ones above it. It is the cap on total GPUs
            # across every card and region, and on this project it is the number
            # that decides what can actually run.
            wrapped(f"note: any (global) is not a card — it is "
                    f"GPUS-ALL-REGIONS-per-project, and at {ceiling.limit} it "
                    f"caps every card above it put together. A second GPU box "
                    f"cannot start while the first is running, whatever the "
                    f"per-card rows say.")
        if not region and (not stocks.looked or not_checked):
            # NOT ON A VIEW THAT CHECKED. `--by-region` now answers availability
            # per row, so this note would sit under rows that contradict it while
            # telling the reader to add a flag that collapses the view they asked
            # for. A note that was true when written and false after a fix is the
            # same trap as a rationale outliving its premise.
            #
            # Said once, plainly, because the column cannot be checked cheaply
            # here and "ready" must not quietly mean two things.
            #
            # AND IT NAMES THE ROWS IT IS ABOUT when only some were unchecked.
            # `--by-region` answers availability per row, so suppressing this for
            # the whole view was true of the named rows and false of the buckets
            # it classifies as mixed — `T4 any of 19 ready` covers nineteen
            # regions of which two sell T4, rendered identically to a row that
            # was checked and passed. The more detailed view was the less honest
            # one.
            about = (f" This applies to {', '.join(sorted(not_checked))}, whose "
                     f"rows cover several regions at once."
                     if not_checked and stocks.looked else "")
            wrapped("note: STATUS reports quota held, not whether a region "
                    "offers the card. `comfy-qat create` checks both and will "
                    "refuse a card this project holds quota for but Google no "
                    "longer sells."
                    + (about or " Add --region to have that checked here."))
        # Pool explanations first: short, and they apply to rows above.
        for pool in sorted(explained):
            who = ", ".join(dict.fromkeys(held_in.get(pool, [])))
            wrapped(f"note: {pool} — {explained[pool]}. Held for: {who}.")
        stranded = sorted({c.gpu for c in cards if undrivable(c.gpu)})
        if not stranded:
            return
        wrapped(
            f"note: {', '.join(stranded)} — quota you hold and this tool will not "
            f"use. It installs the open NVIDIA kernel module, which needs a GPU "
            f"System Processor (GSP); only Turing and newer cards have one, so on "
            f"these the driver installs, no module loads, and nvidia-smi never "
            f"works. `create` refuses them rather than letting you pay to find out."
        )

    def rendered(source, per_region=False):
        """Resolve the pool FIRST, then sort and de-duplicate on the result.

        Three findings had one root: the pool override happened at render time,
        after `summarise` had already sorted and de-duplicated on the on-demand
        values. So a row reading `ready` sorted below three reading `denied`
        (the sort read a status the column did not print); three distinct
        `--by-region` rows collapsed into three byte-identical ones (the dedupe
        keys on the geography the override then replaced); and `--region us-central1`
        left one row saying "43 regions" (the relabel lives in `readiness` and
        `pools_for` computes its own).

        `resolved()` already promised limit, place, status and pool were "decided
        ONCE and both renderings read the result". The sort and the dedupe were a
        third and fourth reading that did not get it. This is where they do.
        """
        from .quota import _ORDER

        out = []
        for item in source:
            best, _f, limit, where, status, pool = resolved(item)
            # M5: the id that produced `limit`. A row won by Spot carried the
            # family id, which reports 0 for that card in every dimension row.
            won = (best is not None and best.name != ON_DEMAND
                   and best.status == "ready")
            qid = best.quota_id if won else getattr(item, "quota_id", None)
            if per_region:
                # F2: A PER-REGION ROW KEEPS ITS OWN REGION. The pool override
                # replaced it with the card-level geography — "43 regions" on all
                # three RTX rows — so the dedupe key `(gpu, where)` collapsed them
                # to whichever sorted first, and that one said "refused in 1"
                # where the card table and `--json` said 2. The one surface whose
                # job is regions was showing a card-level label.
                where = item.region
                # AND A COUNT IS NOT A PLACE. `19 regions` under a column headed
                # REGION reads as one, in the single view whose purpose is to
                # name places — and the row behind it is an undimensioned
                # catch-all covering many locations, so there is no place to
                # name. `any of 19` cannot be misread as somewhere to go.
                #
                # NOT A REVERSAL of unifying on the count form: the collapsed
                # table's WHERE column asks "what does this allowance span", and
                # `42 regions` answers it. This column asks "which region", and a
                # count does not answer that question at all.
                if spans_many(where):
                    where = f"any of {where.split()[0]}"
            elif region and where != "global" and spans_many(where):
                # `spans_many`, NOT `"regions" in where`. The substring version
                # works today and a mutation sweep says so — swapping it back
                # kills no test, and that is recorded here rather than hidden,
                # because the two are behaviourally identical *for the current
                # spelling of the label*. That is exactly the property that
                # failed one branch above: when `42 regions` became `any of 42`,
                # a guard testing the old string silently stopped matching and
                # every bucket row was judged against a catalogue it cannot be
                # tested against. A predicate moves with the label; a substring
                # is a copy of it.
                #
                # The pool's own geography, relabelled the way `readiness`
                # relabels an all-regions row when a region was asked for.
                where = region
            out.append((item, limit, where, status, pool, qid))
        seen: dict[tuple, tuple] = {}
        for entry in out:
            seen.setdefault((entry[0].gpu, entry[2]), entry)
        return sorted(seen.values(),
                      key=lambda r: (_ORDER[r[3]], r[0].gpu, r[2]))

    if as_json:
        def records(source, per_region=False):
            """THROUGH `rendered`, the same function the table reads.

            M4 and L17 were one root: `--json` called `resolved()` directly, so
            it missed both corrections `rendered` applies — a per-region row
            keeping its own region, and the sort being done on the resolved
            status rather than the on-demand one. The result was three
            `--by-region` objects all reading `"region": "43 regions"` where the
            table printed three distinct places, and RTX-PRO-6000 sorted among
            the denied cards while reporting `"status": "ready"`.

            `resolved()` promised limit, place, status and pool were "decided
            ONCE and both renderings read the result". This is the reading that
            did not get it.
            """
            # THE SAME VERDICT THE TABLE PRINTS, per row. `_offered_flag` keys
            # on the single `--region` and knows nothing about a row's own place,
            # so `--by-region` would have reported `offered_here: null` beside a
            # table row reading "not offered here" — the fifteenth second-site,
            # caught before it shipped because the instruction was to look.
            return [_record(item, limit, where, status, pool,
                            drivable_flag(item.gpu), cpu_blocked(item.gpu),
                            (_offered_in(item.gpu, getattr(item, "region", where))
                             if per_region and stocks.looked
                             else _offered_flag(region, item.gpu, absent_here,
                                                unchecked)),
                            quota_id=qid)
                    for item, limit, where, status, pool, qid
                    in rendered(source, per_region=per_region)]

        say.result(json.dumps({
            "project": project,
            # `offered_here` is None without --region: the check is not made,
            # and a script must be able to tell "not offered" from "not asked".
            "gpus": records(cards),
            "by_region": records(rows, per_region=True),
        }, indent=2))
        return

    if not rows:
        say.result(f"{project}: no GPU quotas reported")
        return

    if by_region:
        shown = rendered(rows, per_region=True)
        width = max([len(e[2]) for e in shown] + [6])
        say.result(f"{'GPU':<14} {'REGION':<{width}} {'LIMIT':>5}  STATUS")
        for row, limit, where, _status, _pool, _qid in shown:
            say.result(f"{row.gpu:<14} {where:<{width}} {limit:>5}  "
                       f"{status_of(row.gpu, row.status, row.limit, row.asked, row,
                                    limit, _offered_in(row.gpu, row.region))}")
        footnote()
        return

    shown = rendered(cards)
    width = max([len(e[2]) for e in shown] + [6])
    say.result(f"{'GPU':<14} {'LIMIT':>5}  {'WHERE':<{width}}  STATUS")
    for card, limit, where, _status, _pool, _qid in shown:
        say.result(f"{card.gpu:<14} {limit:>5}  {where:<{width}}  "
                   f"{status_of(card.gpu, card.status, card.limit, card.asked, card, limit)}")

    footnote()

    if not any(c.usable for c in cards):
        # DERIVED, both halves. This read `--gpu l4,a100 --region us-central1`
        # whatever the project held and whatever region was asked about — so a
        # europe-west9 view ended by recommending a request in us-central1 for
        # two cards that may already be granted or refused. The last of the
        # hardcoded hints; its siblings were derived rounds ago.
        from .create import drivable_cards

        askable = [c for c in drivable_cards()
                   if c.upper() not in {g.gpu.upper() for g in cards
                                        if g.status in ("ready", "pending")}]
        say.result("\nnothing is usable yet. Ask for a card:")
        say.result(f"  comfy-qat quota request --gpu "
                   f"{','.join(askable or drivable_cards())}"
                   f" --region {region or '<region>'}")


# One card is enough to reproduce a bug with, and it is the value used only when
# the user names none. It never lowers a standing request — see
# `quota.request_value`.
DEFAULT_VALUE = 1
"""What to ask for when `--value` is left off and no card names a better number.

ONE IS A FLOOR, NOT AN ANSWER. The card table already holds how many GPUs each
machine takes — an H100 is `a3-highgpu-8g`, eight of them — and `setup` reads it
to ask for 8. `quota request` asked for 1, so the tool's own printed remedy filed
a permanent request for a grant that cannot start the only H100 machine it
builds, while `--value 9` warned that 8 is the most any machine takes. The number
was held all along and consulted in one direction only.

`_default_for` reads the table; this stays for `--quota-id`, which names no card
and so has no count to read.
"""


def _default_for(name: str) -> int:
    """How many of this card to ask for when the user did not say."""
    from .create import card_named

    card = card_named(name)
    return card.count if card is not None and card.count else DEFAULT_VALUE


@quota_app.command("request")
def quota_request_cmd(
    gpu: Annotated[Optional[str], typer.Option(
        "--gpu", help="Cards to ask for, comma separated, e.g. l4,a100.")] = None,
    quota_id: Annotated[Optional[str], typer.Option(
        "--quota-id", help="Raw quota id, if you would rather name it exactly.")] = None,
    value: Annotated[Optional[int], typer.Option(
        "--value", help="How many of each card. Left off, as many as one machine "
                        "of that card takes — 8 for an h100, 1 for an l4 — or "
                        "the value of a request already with Google, which is "
                        "never lowered by a number you did not type.")] = None,
    allow_lower: Annotated[bool, typer.Option(
        "--allow-lower",
        help="Permit a value below a request already with Google, or below the "
             "quota this project holds. Without this, a smaller number is "
             "refused rather than quietly replacing the standing one. Nothing "
             "in this tool can undo a decrease: `gcloud quotas preferences` has "
             "create, describe, list and update, and no delete.")] = False,
    release_quota: Annotated[bool, typer.Option(
        "--release-quota",
        help="Permit --value 0, which gives up the quota this project holds for "
             "a card rather than asking for less of it. Needs --allow-lower too. "
             "Nothing in QA needs this; it exists so that a 0 typed by accident, "
             "or left in a script beside a --allow-lower that was there for a "
             "legitimate reduction, cannot destroy a working grant.")] = False,
    region: Annotated[Optional[str], typer.Option(
        "--region", help="The region to be granted the quota in — where the cards you are "
                         "approved for may then be started. Not `quota list --region`, "
                         "which only narrows what that table shows.")] = None,
    justification: Annotated[Optional[str], typer.Option(
        "--justification", help="Why you need it, passed to Google verbatim — this is what a "
                                "human reviewer reads. Left off, nothing is sent at all.")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Wait for approval. On by default.")] = True,
    dry_run: Annotated[bool, typer.Option(
        "--dry-run", help="Print the gcloud calls and reach nothing. Use "
                          "--validate-only to have Google check them.")] = False,
    validate_only: Annotated[bool, typer.Option(
        "--validate-only", help="Ask Google whether the request is valid and create "
                                "nothing. Stronger than --dry-run, which only prints: "
                                "this one reaches Google and checks the real thing.")] = False,
) -> None:
    """Ask Google for GPU quota — several cards at once — then wait for the answer.

    Requesting costs nothing, so asking for every card you might want up front is
    the right move when approval is the slow part. Submitting is instant; approval
    is not, and may go to a human. A brand-new account with no billing history is
    often refused until it has been billed once.
    """
    if not gpu and not quota_id:
        say.fail("name a card to ask for",
                 fix=say.fix("comfy-qat quota request --gpu l4,a100",
                             "or --quota-id, to name a raw quota id exactly"),
                 code=2)

    _stop_on_region_shape(region)

    gc = Gcloud()
    try:
        project = _require_project(gc)
        quotas = gc.gpu_quotas(project)
    except GcloudError as exc:
        # The exception carries the command that fixes it — dropping it left
        # `no project set` with nowhere to go, while `quota list` said `comfy-qat
        # setup` for the same failure. `say.fail` reads `.fix` off the exception,
        # so it can no longer be lost by forgetting a line.
        say.fail(exc, code=2)

    # Asking Google for a card this tool cannot bring up is a request that can
    # only be granted into a dead end: approval takes days, and at the end of it
    # `create --gpu p100` still refuses, because the driver installed here is the
    # open kernel module and Pascal has no GSP. Refused at the point of asking,
    # where it costs nothing, rather than after the wait.
    from .create import card_named, drivable_cards, offered, unspendable

    # READ BEFORE THE LOOP, because two things need it: the guard against
    # lowering a standing request, and the region derivation that stops a
    # no-`--region` request filing in whichever row sorts first. It was read
    # after the targets were built, so the second had nothing to work from.
    try:
        preferences = gc.quota_preferences(project)
    except GcloudError as exc:
        # NEITHER SURFACE GUESSES. This used to carry on with an empty list,
        # reasoning that refusing to ask is worse on the command whose job is to
        # ask — which reads well and is wrong, because it made this the weaker of
        # two paths to the same irrevocable API.
        #
        # Without the list there is no way to know which preference id a
        # (quota id, dimensions) pair already holds, so a request can only mint a
        # fresh one — and Google refuses that for a pair that has one:
        # "Quota Preference with dimension '{}' already exist". The best case is
        # therefore a confusing refusal; the worst is a permanent, undeletable
        # preference filed under the wrong id.
        #
        # EXCEPT UNDER --dry-run, which submits nothing and so cannot file
        # anything wrong. Refusing there too would be a refusal with no hazard to
        # refuse over, on the one path somebody reaches for precisely because
        # something is already going wrong. The comment here originally claimed
        # a dry run "never reaches here", which was simply false — the test
        # written for it said so immediately.
        if dry_run:
            preferences = []
            say.result(
                "could not read existing quota requests, so the preference ids "
                "below are the ones this would MINT. Where a request already "
                "exists for the same quota and dimensions, the real run uses "
                "that preference's own id instead.")
        else:
            say.fail(
                f"could not read this project's existing quota requests: {exc}",
                fix=say.fix(
                    "comfy-qat quota request ... --dry-run  # prints, asks nothing",
                    "then try again — without that list a request could duplicate "
                    "one already on file, and a quota preference cannot be deleted"),
                code=2,
            )

    # (name, target, value sent). The VALUE IS PER CARD now: a re-run that
    # declines to lower a standing request sends that request's number, not the
    # one on the command line, and the wait has to poll for what was sent or it
    # waits for the wrong figure.

    # ONCE, before the loop, because both uses need it and the catalogue is 543
    # rows in a single call. Only when a region was named: without one there is
    # nothing to check a card against — and `is not None` rather than truthiness,
    # so `--region ""` reaches the check instead of passing as an omission.
    if region is not None:
        # M11: BEFORE ANYTHING ELSE USES IT. A typo'd region otherwise travels
        # the whole command and comes back out as a statement about quota.
        wrong = region_problem(gc, project, quotas, region)
        if wrong:
            # THE WHOLE COMMAND, including the card. An earlier version handed
            # over `quota request --region asia-east1` with no `--gpu`, which
            # exits 2 at "name a card to ask for" — while the card the user typed
            # is in the very invocation being refused.
            what = f" --gpu {gpu}" if gpu else f" --quota-id {quota_id}"
            say.fail(
                wrong[0],
                fix=say.fix(
                    *[f"comfy-qat quota request{what} {line}"
                      for line in wrong[1]],
                    "comfy-qat quota list --by-region  # every region this "
                    "project meters"),
                code=2,
            )

    # THE SAME TYPE `quota list` USES, not a second dict with the same
    # weakness. This was `sells: dict = {}` computed under `if region is not
    # None` and read as `sells.get(name)` — correct, but only because its
    # consumers happen to be gated on the same flag as its producer. That is
    # an agreement between two call sites rather than a property of the data,
    # and agreements like it are what keep breaking. One place where "nobody
    # looked" is expressible, and the agreement stops being load-bearing.
    # `not_checked()` HERE IS DEFENCE, NOT BEHAVIOUR, and a mutation sweep says
    # so: replacing it with `Availability(looked=True, where={})` kills no test,
    # because these consumers are gated on `region` — the same flag that decides
    # whether the lookup happens — so the two states cannot be told apart from
    # outside. Recorded rather than papered over with a test that proves nothing.
    # It is the type anyway, so that the day a consumer stops sharing that flag,
    # the honest answer is already the one available.
    # ONE IDENTITY, WHICHEVER FLAG NAMED IT, resolved before any guard runs.
    #
    # `names` was seeded from `--gpu` alone, so `--quota-id` handed every
    # card-keyed guard an EMPTY SET — and an empty set reads as "nothing to
    # check" rather than "I was not told what to check". The two flags then got
    # opposite treatment for the identical request: `--gpu l4 --region
    # africa-south1` refused with a remedy, `--quota-id NVIDIA-L4-GPUS-...
    # --region africa-south1` built a permanent preference for a region selling
    # no NVIDIA card at all.
    #
    # THE FIX IS NOT A SECOND CALL TO THE GUARD. It is that a raw id already
    # carries a card identity — `friendly_name` reads `L4` straight off it — so
    # resolving both flags to one shape HERE makes the whole family of
    # second-sites unrepresentable, including the next one somebody introduces by
    # adding a guard to the `--gpu` path. Same move as `Availability`: make the
    # bad state unconstructible rather than checking for it, one level out.
    asked_for = {n.strip() for n in (gpu or "").split(",") if n.strip()}
    if quota_id:
        named = row_name(quota_id) or friendly_name(quota_id)
        if named:
            asked_for.add(named)

    sells = Availability.not_checked()
    if region is not None:
        sells = Availability(looked=True,
                             where=_regions_stocking(gc, project, asked_for))

    # L15: A BOUND AT THE TOP, NAMED RATHER THAN ENFORCED. `--value 999999` exited
    # 0 with nothing said. Too large is not destructive — Google refuses it, it is
    # not granted — so this warns instead of refusing: asking for headroom is
    # legitimate and only the person who typed it knows which this is. The number
    # comes from the card table, so it moves when the table does.
    if value is not None and value > 0:
        from .create import CARDS

        most = max(card.count for card in CARDS.values())
        if value > most:
            say.warn(f"{value} is a lot: {most} is the most any machine this "
                     f"tool creates takes. Asking for headroom is fine; a typo "
                     f"is not, and Google reads the number.")

    wanted: list[tuple[str, Target]] = []
    if quota_id and gpu:
        # REFUSED, NOT WARNED. This printed "--gpu l4,t4 ignored" and then filed
        # three permanent requests — the raw id AND both cards, one of them by
        # reaching into an existing granted preference. A warning that does not
        # change behaviour is worse than no warning: it tells somebody they have
        # been protected from the thing that is about to happen.
        #
        # And the shape is its own: not a false sentence about the world, but a
        # false sentence about this tool's OWN NEXT ACTION, which a reader has no
        # way to check except by watching what it does.
        #
        # Refusing rather than picking a winner, because the command genuinely
        # cannot know which was meant — and the two were resolved to one identity
        # last round precisely so two paths could not diverge. This is that
        # ambiguity one layer out, at the argument list.
        say.fail(
            "--gpu and --quota-id both name what to ask for, and they disagree "
            "here, so this command cannot tell which you meant",
            fix=say.fix(f"comfy-qat quota request --quota-id {quota_id}"
                        f"  # the quota, exactly as Google names it",
                        f"comfy-qat quota request --gpu {gpu}"
                        f"  # the card or cards, resolved for you"),
            code=2,
        )
    if quota_id:
        # F7: CHECKED AGAINST WHAT THE PROJECT REPORTS. `--quota-id NOT-A-QUOTA`
        # was accepted, exit 0, on the irrevocable path — with no check against
        # the quota records this command has already read.
        if quota_id not in {q.get("quotaId") for q in quotas}:
            say.fail(
                f"this project reports no quota called {quota_id!r}",
                fix=say.fix("comfy-qat quota list — the quota this project has",
                            "comfy-qat quota request --gpu <card> — by card name "
                            "instead of a raw id"),
                code=2,
            )
        # F5: THE REGION IS A DIMENSION, and it was being dropped. A `--region`
        # given alongside a raw id produced a byte-identical command to one given
        # without it: a DIMENSIONLESS preference, which is a different object
        # from the one the user asked for, filed permanently. Region-scoped ids
        # are the ones that take it — `needs_region` decides, from the id.
        # M10: THE FAMILY SHAPE SAYS WHICH CARD IN A DIMENSION, and a raw id
        # cannot supply it. This built `--dimensions=region=us-central1` with no
        # `gpu_family` — a different object from any card's quota, filed
        # permanently — and because it matched no standing preference, the
        # lowering guard could not fire either. One flag switching off a whole
        # class of protection is not a thing to leave reachable.
        if needs_family(quota_id):
            say.fail(
                f"{quota_id} meters several cards and names which one in a "
                f"dimension, so a raw id cannot say which you mean",
                fix=say.fix(
                    "comfy-qat quota request --gpu h100  # name the card instead",
                    "comfy-qat quota list — which cards this id meters"),
                code=2,
            )
        # `needs_region` FIRST, and the missing region is a refusal. This read
        # `if region and needs_region(...)`, so a MISSING region silently dropped
        # the dimension the id requires rather than refusing — the `and` made the
        # guard unreachable in exactly the case it exists for, and the result is
        # the shape `needs_region`'s own docstring records Google rejecting:
        #
        #   INVALID_ARGUMENT: Dimension values must be set for all the dimensions
        #   ... defined for the quota.
        #
        # filed permanently, because `--allow-missing` creates.
        if needs_region(quota_id) and not region:
            say.fail(
                f"{quota_id} is metered per region, so a request for it has to "
                f"name one",
                fix=say.fix(
                    f"comfy-qat quota request --quota-id {quota_id} "
                    f"--region us-central1",
                    "comfy-qat quota list --by-region  # every region this "
                    "project meters"),
                code=2,
            )
        # SAY WHEN A FLAG IS DROPPED. This command refuses an empty region, a
        # zone-shaped one, an uppercase one and an unknown one — and then
        # silently ignored a perfectly good one on the id that takes no
        # dimensions. Being loud about four wrong values and mute about one
        # ignored value is out of character, which is the argument for saying it
        # rather than for refusing.
        if region and not needs_region(quota_id):
            say.warn(f"--region {region} ignored: {quota_id} is not metered per "
                     f"region, so a request for it carries no dimensions")
        dims = ((("region", region),) if needs_region(quota_id) else ())
        # THE SAME GUARDS THE `--gpu` PATH RUNS, keyed on the identity resolved
        # above. Whatever the tool says about `--gpu l4` it must say about
        # `--quota-id NVIDIA-L4-GPUS-per-project-region`: they are one request.
        named = row_name(quota_id) or friendly_name(quota_id)
        if named:
            _refuse_if_unsold(named, region, sells, quotas)
        wanted.append((quota_id, Target(quota_id, dims)))
    # `dict.fromkeys` RATHER THAN A SET, so the order the user typed survives —
    # and so `--gpu l4,l4` files ONE request. It filed two identical preferences,
    # which is the duplicate this whole feature exists to prevent, reachable by
    # typing a card name twice.
    for name in dict.fromkeys(n.strip() for n in (gpu or "").split(",")):
        if not name:
            continue
        card = card_named(name)
        # THE SEVENTH SECOND-SITE, in the command that files irrevocable
        # requests. `quota request --gpu b200` answered "this project reports no
        # quota for 'b200'" — false; the project meters it and `quota list`
        # prints the row. What is true is that `create` will never accept it, and
        # that is what the table says. Same predicate, so they cannot diverge.
        if not offered(name) and card is None:
            # M8: A TYPO AND A REAL CARD GOT THE SAME SENTENCE. "banana: not
            # creatable by this tool, whatever quota it holds" asserts that
            # banana holds quota, and sends the reader to look it up in a table
            # it will never appear in. `known_card` is the distinction, and it
            # reads the same constant `create` refuses from.
            from .create import known_card

            if not known_card(name):
                say.fail(
                    f"no card called {name!r}",
                    fix=say.fix(
                        f"comfy-qat quota request --gpu <card>: "
                        f"{', '.join(drivable_cards())}",
                        "comfy-qat quota list — the cards this project meters"),
                    code=2,
                )
            say.fail(
                f"{name}: not creatable by this tool, whatever quota it holds",
                fix=say.fix(f"comfy-qat create --gpu <card>: {', '.join(drivable_cards())}",
                            "comfy-qat quota list — what this project holds, "
                            "including cards this tool cannot order"),
                code=2,
            )
        if card is not None and not card.has_gsp:
            say.fail(
                f"there is no point asking for {card.name} quota: this tool cannot "
                f"bring that card up. The driver it installs is the open NVIDIA "
                f"kernel module, which needs a GPU System Processor, and only "
                f"Turing and newer cards have one.",
                fix=f"ask for one that works: {', '.join(drivable_cards())}",
                code=2,
            )
        # `pin_region=bool(region)`: the user typed it, so honour it.
        #
        # `family=` is not optional here. Without it the family shape cannot be
        # resolved at all, so `quota request --gpu h100` refused with "this
        # project reports no quota for 'h100'" on a project that meters it —
        # while `setup`, which passed it, asked for the same card successfully.
        # One surface working and the other not is worse than neither, because
        # the broken one reads as a fact about the project.
        # `preferred_region`: where this project has asked before, the same
        # derivation `setup` uses — so a request with no `--region` lands where
        # the user works rather than in whichever row sorts first.
        from .setup import request_region

        preferred, _why = request_region(quotas, preferences, None)
        resolved = resolve_target(name, quotas, region=region,
                                  pin_region=bool(region),
                                  family=card.quota_family if card else None,
                                  preferred_region=preferred)
        if resolved is None:
            # Never a card this tool cannot drive. "ask for one of: P100" is
            # advice that ends in a billing box with a dead GPU.
            # Only cards this tool can actually ASK FOR. `available_gpus` reports
            # what the PROJECT meters, which is the right answer for a table and
            # the wrong one for a fix line: H200, B200, H100-MEGA and
            # RTX-PRO-6000 are all metered here and none is in the card table, so
            # they were offered and then refused by the very next command. A fix
            # line that does not work is worse than no fix line.
            offer = ", ".join(
                found for found in available_gpus(quotas)
                if (card_named(found) is not None and card_named(found).has_gsp)
            ) or "none"
            # A card this project does have, just not where you asked, used to
            # come back as "no quota for 'l4' … Available: L4" — which reads as
            # a contradiction. Say where it is metered instead.
            #
            # F10: `readiness` yields LABELS, not places — "all regions" is one —
            # so this printed "it is metered in all regions, us-central1", which
            # reads as a two-item region list and is two API dimension entries
            # rendered as prose. And when the REGION is what is wrong, a list of
            # CARDS is an answer to a question nobody asked — one that contained
            # the card just typed.
            # METERED. This is "where else could you ask", and a card at zero is
            # still metered somewhere — it is the only kind of card that reaches
            # a request at all.
            places = sorted(regions_metered(name, quotas)) if region else []
            # H2: `places[0]` IS ALPHABETICAL, NOT ADVICE. On this project that
            # is africa-south1, which sells no NVIDIA accelerator of any kind —
            # so "somewhere it is metered" sent people to file an irrevocable
            # request for a box that can never start. Metered AND stocked, and
            # only metered if the catalogue could not be read.
            stocked = sells.where.get(name) if sells.looked else None
            usable = [p for p in places if p in stocked] if stocked else []
            if places and region not in places:
                # A FALLBACK IS ONLY HONEST WHEN THE CHECK COULD NOT BE MADE.
                # `(usable or places)[0]` named `applicableLocations[0]` whenever
                # nothing metered was also stocked — africa-south1 again, one
                # branch over from where that was fixed, and running the
                # suggestion exits 2 at the availability check two screens later.
                # `stocked is None` means the catalogue could not be read, and
                # only then is "somewhere it is metered" all we can say.
                if usable:
                    where = (f"comfy-qat quota request --gpu {name} --region "
                             f"{usable[0]}  # metered here and stocked there")
                elif stocked is None:
                    where = (f"comfy-qat quota request --gpu {name} --region "
                             f"{places[0]}  # somewhere it is metered; whether it "
                             f"is stocked was not checked")
                else:
                    where = (f"no region this project meters {name} in also "
                             f"stocks it")
                say.fail(
                    f"this project has no {name} quota in {region}",
                    fix=say.fix(
                        where,
                        f"comfy-qat quota list --by-region  # "
                        + ("the other " + str(len(places) - 1) + " too"
                           if len(places) > 1 else "every region it is metered in")),
                    code=2,
                )
            say.fail(
                f"this project reports no quota for {name!r}"
                + (f" in {region}" if region else ""),
                fix=(f"ask for one of: {offer}" if offer != "none" else
                     "this project reports no GPU quota at all — "
                     f"comfy-qat quota request --gpu {drivable_cards()[0]} "
                     f"--region {region or '<region>'}"),
                code=2,
            )
        # H2: THE CATALOGUE, ON THE COMMAND THAT FILES THE IRREVOCABLE THING.
        # `quota list --region` consulted it and this did not — the thirteenth
        # time a correction landed on one surface and missed its sibling. Quota
        # in a region that sells no such card buys a preference that cannot be
        # deleted and a box that can never start.
        _refuse_if_unsold(name, region, sells, quotas)
        here = sells.where.get(name) if sells.looked else None
        if region and here is None:
            # NOT A VERDICT. The catalogue could not be read, or the card is not
            # one this tool can name an accelerator id for; either way the answer
            # is "I did not check", and refusing on it would invent an absence.
            say.result(f"{name}: whether {region} offers this card was not checked")
        wanted.append((name, resolved))

    # TWO PHASES, because phase two cannot be taken back. `--gpu l4,t4,h100`
    # filed L4, filed T4, then refused H100 at the floor and exited 2 — with
    # `submitted` discarded, so the `track them:` line naming what HAD been filed
    # never printed either: two permanent requests on the project and no record
    # of them in the output reporting a failure.
    #
    # Everything needed to refuse H100 — the quota records and the preference
    # list — is already in hand before the loop starts. Nothing has to be sent to
    # discover it. So every card is checked first and the command files all of
    # them or none.
    checked: list[tuple[str, Target, object, int]] = []
    for name, resolved in wanted:
        # M6: SAY SO WHEN GOOGLE HAS ALREADY ANSWERED. `setup` declines to re-ask
        # for a refused card and prints "Not asked again automatically"; `create`
        # told the user to ask by hand; this command did it without a word,
        # re-submitting the denied preference unchanged and then waiting for an
        # answer already given. Three commands, three policies, one card.
        #
        # WARNED AND NOT REFUSED, deliberately. `setup` is automatic, so it
        # declines; this is what somebody types on purpose, and there has to be a
        # way to re-ask once the thing a reviewer reads has changed. What there
        # must not be is silence.
        refused = [ask for ask in asks_about(resolved, preferences)
                   if ask.state == "denied"]
        if refused:
            where = ", ".join(sorted(ask.preference_id for ask in refused))
            say.warn(f"{name}: Google has already refused this ({where}). Asking "
                     f"again changes nothing unless what a reviewer reads has "
                     f"changed — on a new project that is billing history, not "
                     f"the wording.")
        plan = request_plan(resolved, preferences)
        # `value is None` means the user did not type one, which is the whole
        # distinction: a number they chose may lower a standing request once they
        # confirm it; a default must never touch it. With `--value` defaulting to
        # 1 the two were indistinguishable, and that is how a live H100 request
        # for 8 got quietly replaced with 1.
        # `value if value is not None else ...`, NEVER `value or ...`. The `or`
        # idiom falsy-tests an Optional[int], so it cannot tell "the user did not
        # type one" from "the user typed 0" — which is the exact distinction the
        # comment above exists to make, broken by the next line. An explicit 0
        # became 1, and on a standing request of 8 the tool announced "LOWERING
        # ... from 8 to 1" about a number nobody had typed.
        send, note = request_value(
            resolved, preferences,
            value if value is not None else _default_for(name),
            allow_lower=allow_lower, quotas=quotas,
            release=release_quota and allow_lower)
        # ONCE. `say.fail` below prints the same sentence, so announcing it here
        # as well printed the refusal twice — which every `in result.output`
        # assertion in the suite is happy with, and which anybody running the
        # command sees immediately.
        # AND NOT WHEN THE COMMAND IS ABOUT TO REFUSE. "keeping the standing
        # request at 8" printed one line above "refusing to lower ... to 2",
        # exit 2, nothing filed — a sentence asserting an action that did not
        # happen. `send != value` with no `--allow-lower` is precisely the
        # refusal below; saying it twice, once wrongly, is worse than once.
        refusing = value is not None and send != value and not allow_lower
        if note and send is not None and not refusing:
            # STDERR. `--dry-run | sh` is what `--dry-run` is for, and this line
            # went into the pipe between two commands — it contains a `;`, so the
            # shell attempted "1 would lower it" as a command of its own. Prose is
            # not the answer this command was run for; the gcloud lines are.
            say.warn(note)
        if send is None:
            # ONE REMEDY PER CAUSE, enumerated rather than appended. `send is
            # None` has FOUR causes and this block once offered TWO remedies, so
            # the others inherited whichever branch they fell through to: a
            # negative was refused and then told to pass `--allow-lower`, which
            # cannot permit a negative — nothing can.
            #
            # THIS COMMENT SAID THREE, AND THERE WERE FOUR, which is how the
            # UNLIMITED case ended up under "the standing value could not be
            # read" — the standing value was read, there is no preference at all,
            # and what is at stake is an unlimited GRANT being traded for a
            # number. An enumeration in a comment does not notice a new arrival;
            # `test_every_cause_of_a_refused_value_gets_its_own_remedy` counts
            # them, and that is what noticing looks like.
            #
            # Sixth instance of "a fix line naming a remedy that cannot work",
            # after `none — request it` on an unrequestable card, `--region
            # africa-south1` as somewhere to ask, and a remedy that rewrote
            # `--quota-id` to `--gpu` and exited 2. The shape is always the same:
            # a generic block reached by a case it was not written for.
            if value is not None and value < 0:
                # NO FLAG, because there is no flag. A negative has no valid
                # form, so the only useful thing to hand over is what to type.
                # THE FLAG THE USER TYPED. This hardcoded `--gpu {name}`, and
                # on the `--quota-id` path `name` IS the raw quota id — so the
                # remedy was `--gpu NVIDIA-L4-GPUS-per-project-region`, which
                # exits 2 at "no card called". Seventh instance of the shape the
                # comment above this block enumerates, inside that very block:
                # every other branch here preserves the flag, and the ceiling
                # case prints `--quota-id GPUS-ALL-REGIONS-per-project` right.
                flag = (f"--quota-id {name}" if quota_id == name
                        else f"--gpu {name}")
                remedy = say.fix(
                    f"comfy-qat quota request {flag} --value 1"
                    f"  # a count of GPUs, 1 or more",
                    "comfy-qat quota — what this project holds")
            elif held_value(resolved, quotas) == UNLIMITED:
                remedy = say.fix(
                    "comfy-qat quota — what this project holds",
                    "--allow-lower  # trade the unlimited grant for a number, "
                    "knowing this tool cannot undo it")
            elif value == 0:
                remedy = say.fix(
                    "comfy-qat quota — what this project holds",
                    "--allow-lower --release-quota  # give the quota up; "
                    "this tool cannot undo it")
            else:
                # The standing value could not be read, so there is no number
                # that is safe to send. Refusing is the only option that cannot
                # replace a larger request with a smaller one.
                remedy = say.fix(
                    "comfy-qat quota — what this project holds",
                    "--allow-lower  # send it anyway, knowing it may replace a "
                    "larger standing request")
            say.fail(f"{name}: {note}", fix=remedy, code=2)
        if value is not None and send != value and not allow_lower:
            # THE FLAG THE USER TYPED, not the one the tool prefers. `--quota-id`
            # was rewritten to `--gpu` in the remedy, and the rewritten command
            # exits 2 — "not creatable by this tool" — because a raw quota id is
            # not a card name. A fix line that does not run is worse than none.
            typed = f"--quota-id {name}" if quota_id == name else f"--gpu {name}"
            say.fail(
                f"{name}: refusing to lower the standing request to {value}",
                fix=say.fix(f"comfy-qat quota request {typed} --value {send}"
                            f"  # keep it where it is",
                            "--allow-lower  # if you really mean to reduce it"),
                code=2,
            )
        if send == 0:
            # INFERRED, AND SAID AS SUCH. `gcloud quotas preferences update`
            # carries `--allow-high-percentage-quota-decrease` and
            # `--allow-quota-decrease-below-usage` (verified in its --help on
            # this machine); going 1 -> 0 is a 100% decrease, so Google may
            # refuse the command printed here. Whether it does is UNVERIFIED —
            # settling it would mean filing an irrevocable request.
            #
            # NOT EMITTED AUTOMATICALLY. Those flags exist to override Google's
            # own safety checks on the one path in this tool that destroys
            # something, and `--release-quota` is deliberate friction rather than
            # a formality. Naming them keeps a refusal legible and leaves the
            # decision to override where it belongs.
            say.warn("if Google refuses this as too large a decrease, it wants "
                     "--allow-high-percentage-quota-decrease, and "
                     "--allow-quota-decrease-below-usage if the quota is in use. "
                     "This tool does not add either for you.")
        checked.append((name, resolved, plan, send))

    # --- nothing above this line has sent anything ---------------------------
    submitted: list[tuple[str, Target, int]] = []
    for name, resolved, plan, send in checked:
        args = quota_request_command(
            project=project, quota_id=resolved.quota_id, value=send,
            preference_id=plan.preference_id, dimensions=plan.dimensions,
            justification=justification, email=gc.active_account(),
            allow_missing=plan.allow_missing, validate_only=validate_only,
        )
        if dry_run:
            # QUOTED, because `--dry-run | sh` is what this output is for and a
            # justification is prose. `--justification=QA for Comfy Org` pastes
            # as `--justification=QA` plus two stray positional arguments.
            say.result("gcloud " + " ".join(shlex.quote(a) for a in args))
            continue
        try:
            gc.run(args)
        except GcloudError as exc:
            # `exc.fix` explicitly. Interpolating the exception into the
            # message leaves `say.error` looking for a `.fix` on a STRING, so
            # the one gcloud's classifier had already worked out — which region
            # to use, which quota id is the real one — was computed and thrown
            # away, on the command where a refusal is the ordinary outcome.
            say.error(f"request for {name} failed: {exc}", exc.fix,
                      blank_line=False)
            continue
        if validate_only:
            # Reached Google, validated the whole request, created nothing.
            say.result(f"valid: {name} = {send}"
                       + (f" in {region}" if region else ""))
            continue
        say.result(f"requested {name} = {send}" + (f" in {region}" if region else ""))
        submitted.append((name, resolved, send))

    if dry_run or validate_only:
        return
    if not submitted:
        # Every request was refused. Exiting 0 told a script it had worked.
        raise typer.Exit(code=2)

    say.result(f"track them: {console_quota_url(project)}")
    if not wait:
        return

    # One window for the whole command, not one per card: waiting on l4,a100
    # serially meant the documented half-hour became an hour, and the second
    # card was not even polled until the first gave up.
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    still_waiting = []
    for name, resolved, sent in submitted:
        granted = wait_for_quota(
            lambda want=resolved: _current_value(gc, project, want), wanted=sent,
            timeout=max(0.0, deadline - time.monotonic()), interval=POLL_SECONDS,
        )
        if granted:
            say.result(f"granted: {name}")
        else:
            still_waiting.append(name)

    if still_waiting:
        # 75 is EX_TEMPFAIL: not an error and not done either. Nothing is broken,
        # so this is a result rather than a failure, and it names what to run
        # next because approval can take days and the wait has to be re-entered.
        say.result(f"still pending: {', '.join(still_waiting)}. Approval can take "
                   "days — run this again to keep waiting, or `comfy-qat quota` "
                   "to check.")
        raise typer.Exit(code=75)


def _require_project(gc: Gcloud) -> str:
    project = gc.current_project()
    if not project:
        raise GcloudError("no project set", fix="comfy-qat setup")
    return project


def _current_value(gc: Gcloud, project: str, target: "Target") -> int | None:
    """What this project holds for EXACTLY the pair that was requested.

    None when the project reports no such quota at all, which is not the same as
    zero — see the caller. Absent-versus-zero has now been the wrong answer in
    three separate places in this module's history, so it is a distinct value
    here rather than a `0` that means two things.

    DIMENSIONS ARE HONOURED, and they were not. This matched on `quotaId` alone
    and handed the whole record to `_value_of`, which takes the maximum across
    every `dimensionsInfos` entry. A T4 granted 1 in forty-two regions therefore
    satisfied `wanted=1` for a request about europe-west4, and `--wait` announced
    "granted: t4" on the first poll about a request Google had not answered. The
    dimensions were in scope at the call site and thrown away.

    GPU-ONLY, DELIBERATELY. `compute_quotas` would widen this to CPU rows, and
    `quota request` files nothing CPU-shaped — every CPU quota this tool would
    ever raise belongs to a family Google waives. If that changes, this is the
    second place to widen; `quota_list_cmd` was the first and had the same bug.
    """
    from .quota import covers_dimensions, rows

    matched = [row for row in rows(gc.gpu_quotas(project))
               if covers_dimensions(target.quota_id, target.dims, row)]
    if not matched:
        return None
    return max(row.limit for row in matched)


def wait_for_quota(
    poll: Callable[[], int],
    *,
    wanted: int,
    timeout: float | None = None,
    interval: float | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] | None = None,
) -> bool:
    """Poll until the quota reaches `wanted`, or the window closes.

    Returns True if granted. Every default is resolved here rather than in the
    signature: bound as defaults they were captured at import, so neither the
    constants nor the clock could be replaced from outside, and driving the wait
    through the command itself meant a test that really slept for half an hour.
    """
    timeout = WAIT_TIMEOUT_SECONDS if timeout is None else timeout
    interval = POLL_SECONDS if interval is None else interval
    sleep = sleep or time.sleep
    now = now or time.monotonic

    deadline = now() + timeout
    while True:
        try:
            found = poll()
            # None is "this project reports no such quota", which polling cannot
            # fix — distinct from 0, "reported and not granted yet", which is
            # exactly what a wait is for. Read as 0 they were indistinguishable
            # and a missing quota burned the whole half-hour window in silence.
            if found is None:
                return False
            if found >= wanted:
                return True
        except GcloudError:
            pass  # a transient read failure is not a denial; keep waiting
        if now() >= deadline:
            return False
        sleep(interval)
