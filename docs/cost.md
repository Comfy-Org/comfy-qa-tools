# Cost

Cloud GPU boxes are billed by the hour while they run. Right now these are on
personal Google accounts, which means it is somebody's own card — so the numbers
matter more than they would on a company account.

## The one rule

**Stop the box when you stop testing.** A GPU instance left on overnight is the
failure mode here, and it is invisible unless something tells you.

## What costs what

- **Running**: the machine type plus the GPU, per hour. This is the expensive part.
- **Stopped**: only the disk. Cheap — cents per day — which is why keeping one box
  per OS and stopping the idle one is the right pattern rather than deleting and
  recreating.
- **Deleted**: nothing, but you also lose the ComfyUI install and the models on it.

Prices vary by GPU, machine type and region and change over time, so this page does
not quote figures that would quietly go stale. Check the live rate for what you are
about to start:

```sh
gcloud compute machine-types describe <type> --zone <zone>
```

and the pricing calculator: https://cloud.google.com/products/calculator

## Checking what you have spent

```sh
comfy-qat auth status --json
```

and the billing console for the project, which is linked in any billing error the
tool prints.

## Quota is not cost

GPU quota is permission to run GPUs, not a charge. Requesting quota costs nothing;
running the machine is what costs. A brand-new project starts with **zero** GPU
quota and often cannot be granted any until it has been billed at least once.

```sh
comfy-qat auth quota
```

```
GPU            LIMIT  WHERE        STATUS
L4                 1  all regions  ready
A100               0  all regions  pending — waiting on Google
T4                 0  all regions  none — request it
```

One line per card. Some cards are metered region by region — a live project
returns 25 separate entries for K80 — so `--by-region` shows that detail when you
need it, and `--region us-central1` narrows to one place.

Ask for several cards at once — it costs nothing, and approval is the slow part:

```sh
comfy-qat auth quota request --gpu l4,a100 --region us-central1
```

Quota gates the **card**, never the operating system. Once a card is approved you
can build either Windows or Linux on it.
