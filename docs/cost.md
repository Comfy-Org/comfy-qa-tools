# Cost

Cloud GPU boxes are billed by the hour while they run. Right now these are on
personal Google accounts, which means it is somebody's own card — so the numbers
matter more than they would on a company account.

## The one rule

**Stop the box when you stop testing.** A GPU instance left on overnight is the
failure mode here, and it is invisible unless something tells you.

Two recent changes make that rule easier to break, so they are worth stating
plainly. **`create` leaves the box running** — an instance bills from the moment
it exists, which is why `create` finishes by printing the `down` command alongside
the `go` one. And **`go` now hands your prompt back** with ComfyUI still running on
the box, so nothing occupies your terminal as a reminder and two machines can be up
at once, billing twice. `comfy-qat down --all` is the end-of-session command:
it takes no name, and stops everything you have declared.

## What costs what

- **Running**: the machine type plus the GPU, per hour. This is the expensive part.
- **Stopped**: only the disk. Cheap — cents per day — which is why keeping one box
  per OS and stopping the idle one is the right pattern rather than deleting and
  recreating.
- **Reserved**: the machine type plus the GPU, per hour, **whether it is running
  or stopped**, from the moment it is created until it is deleted. A box made
  with `create --reserve` has its capacity held by Google, and that is what is
  billed. Stopping it saves nothing. This is the one kind of box "stop it when
  you stop testing" does not cover — see below.
- **No GPU** (`create --gpu none`): the machine type alone, with no card on the
  bill. The cheapest box this tool makes, and the slowest.
- **Deleted**: nothing, but you also lose the ComfyUI install and the models on it.

Prices vary by GPU, machine type and region and change over time, so this page does
not quote figures that would quietly go stale. Check the live rate for what you are
about to start:

```sh
gcloud compute machine-types describe <type> --zone <zone>
```

and the pricing calculator: https://cloud.google.com/products/calculator

## A reserved box is the exception to the one rule

`comfy-qat down` does not stop a reserved box's bill, and neither does anything
else short of deleting it. The tool says so every time it would otherwise have
said "stop paying": `down` ends on `Stopped — but it is reserved, so it is still
billing`, `go` and `up` end on `comfy-qat delete <name>` in place of `comfy-qat
down <name>`, and `down --all` does not print `Nothing is now.` while a reserved
box is declared or a reservation is on the project.

So reserve a box only for as long as you need the place held, and **delete it**
when you are finished with it — `comfy-qat delete <name>` releases the
reservation with the box. `comfy-qat list` shows which boxes are reserved, and
`comfy-qat list --live` also names any reservation on the project that has no box
at all, which bills exactly the same and appears nowhere else.

That a reservation bills for every hour it exists, used or not, is Google's
published pricing for reservations. This tool has no way to measure it and has
not been run against a live reservation yet.

## Checking what you have spent

**This tool cannot tell you.** It has no command that reports money, and none is
planned: `status` answers "is my account ready" — signed in, project, billing
*linked*, GPU quota — and `--json` gives you those same five checks and nothing
else. Whether billing is linked is not what it costs.

Spend lives in Google Cloud's own billing console, for the account the project is
linked to:

- **https://console.cloud.google.com/billing** — the current month, and the
  breakdown by SKU that tells you which box did it.
- Any billing error this tool prints already carries the link to the right page
  for your project, so you rarely have to find it yourself.

What the tool *can* answer is the question underneath — "am I still paying for
anything":

```sh
comfy-qat list --live      # what Google says is running and what is reserved
comfy-qat down --all       # stop every cloud box you have declared
```

`--live` is the honest check: without it, `list` reports only what this machine
knows, which is whether a tunnel is open and what your host list says is
reserved. A tunnel closed by a laptop reboot does not stop the box, and a box
with no tunnel bills exactly the same. It is two calls per project — instances
and reservations — however many boxes you have.

## Quota is not cost

GPU quota is permission to run GPUs, not a charge. Requesting quota costs nothing;
running the machine is what costs. A brand-new project starts with **zero** GPU
quota and often cannot be granted any until it has been billed at least once.

```sh
comfy-qat quota list
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
comfy-qat quota request --gpu l4,a100 --region us-central1
```

Quota gates the **card**, never the operating system. Once a card is approved you
can build either Windows or Linux on it.
