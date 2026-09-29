# Spot instances — a plan, and nothing built

**Nothing in this page is implemented.** It is written down because the
measurements behind it were expensive to get and the sequencing matters more
than the code. Read *Before building anything* first; it is the only part that
is load-bearing today.

This project's on-demand GPU requests have all been refused — four of them,
each within three seconds, by an automated eligibility decision that no
justification reaches. Spot is the one route that is not behind that refusal,
because some preemptible quota is **already granted**.

---

## What is measured, and what is inferred

Read live on 2026-09-17 by scanning **every** `dimensionsInfos` row and taking
`details.value` — not the first row, and not the `applicableLocations` count.
That distinction is here because getting it wrong is what produced the first
version of this page:

| quota | value | shape |
|---|---|---|
| `PREEMPTIBLE-NVIDIA-L4-GPUS-per-project-region` | **1** | one undimensioned row, 43 locations |
| `PREEMPTIBLE-NVIDIA-RTX-PRO-6000-GPUS-per-project-region` | **1** | one undimensioned row, 43 locations |
| `PREEMPTIBLE-NVIDIA-T4-GPUS-per-project-region` | **1** | 24 per-region rows + one catch-all, 43 locations |
| `PREEMPTIBLE-NVIDIA-V100-GPUS-per-project-region` | **1** | same shape as T4 |
| `PREEMPTIBLE-NVIDIA-H100-GPUS-per-project-region` | *(no value)* | — |
| `PREEMPTIBLE-CPUS-per-project-region` | *(no value)* | 43 rows, none with a value |
| `CPUS-ALL-REGIONS-per-project` | 32 | — |

**"No value" is read as zero throughout this tool, and that is an inference.**
`CPUS-ALL-REGIONS` reports `'32'` in the same field, so an absence is the API
saying something rather than failing to say it. The reading is almost certainly
right; it is flagged because inferring from an absence is exactly what produced
a CPU-quota gate that does not exist (see `comfy_qa/quota.py`).

### What follows from it

- **A Spot L4 or RTX PRO 6000 is not blocked by CPU quota.** G2 and G4 are both
  on Google's "you don't need to request CPU quotas" list, so
  `PREEMPTIBLE_CPUS` having no value is irrelevant to them. Both already hold
  preemptible GPU quota across 43 locations.
- **A Spot T4 or V100 is blocked, once.** N1 does consume `PREEMPTIBLE_CPUS`.
  Their GPU quota is granted project-wide like the others — an earlier claim
  that it existed "in asia-east1 only" was a measuring error, not a fact.

---

## Before building anything

**Prove an L4 launches.** One `g2-standard-8` with an L4, Spot, in
`us-central1-a`. Everything below is cheap to design correctly once and
expensive to design on an inference.

Why that machine and not the RTX PRO 6000's `g4-standard-48`:

- G2 is on the waived list while `PREEMPTIBLE_CPUS` has no value, so a launch
  settles the CPU-quota question empirically — which is the thing three rounds
  of documentation could not settle.
- 8 vCPU stays under the 32-vCPU `CPUS-ALL-REGIONS` ceiling, so the ceiling
  cannot confound the result. `g4-standard-48` is 48 vCPU and would leave two
  explanations for a single failure.
- The L4 is already proven on real hardware here, so a failure is about Spot
  rather than about anything new.

**Read the error, never the exit code.** 57 of this project's 72 instance
inserts have failed with `ZONE_RESOURCE_POOL_EXHAUSTED`. A capacity stockout is
the single likeliest outcome and says nothing about quota either way. **The test
is conclusive in one direction only:** a launch proves CPU quota does not gate
G2; a failure proves nothing unless the error names quota.

---

## What the tool would need

In dependency order. The first three are small; the fourth is the work.

1. **`--spot` on `create`.** A property of the request, not of the card — no new
   `Card` field. The insert gains `--provisioning-model=SPOT` and
   `--instance-termination-action=DELETE`.

2. **Quota resolution reads the `PREEMPTIBLE-` id.** A third shape for
   `quota.resolve_target`, slotting in beside the per-card and family branches.
   Resolve it from what the project reports, as with the other two — never
   assume a `PREEMPTIBLE-` id exists because the on-demand one does.

3. **`_cpu_shortfall` consults `PREEMPTIBLE-CPUS` for a Spot N1.** Only for N1;
   every other family this tool orders is waived either way.

4. **The lifecycle has to tell "was reclaimed" from "is broken".** This is the
   substantial half. A Spot box can vanish mid-run, and the tool currently has
   one vocabulary for a box that does not answer. `stamp`, `up` and the tunnel
   each need a reclaimed path, because a reclaimed box reported as broken sends
   somebody to debug a machine that did exactly what it was designed to do —
   and this tool has collapsed that distinction before.

Cost is the reason any of this is worth it: Spot is roughly a third to a fifth
of on-demand, on a card this project can already have.
