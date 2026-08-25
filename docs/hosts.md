# The host list

Every machine you test on is declared in `~/.config/comfy-qa-tools/hosts.toml`.
`comfy-qat host init` writes a starter one.

```toml
[hosts.local]
kind = "local"
port = 8188

[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-a"
gce_project  = "your-project-id"
port         = 8190
```

## Fields

| field | applies to | meaning |
|---|---|---|
| `kind` | all | `local` for a ComfyUI on this machine, `gce` for a Google Cloud box |
| `port` | all | where ComfyUI is reached **on your machine**. For `local`, the port it actually serves on. For `gce`, the near end of the tunnel. Defaults to 8188 for `local` |
| `os` | gce | what the box runs, e.g. `Ubuntu 22.04`. Shown in `host list` |
| `gpu` | gce | the card, e.g. `L4`. Matters as much as the OS — not every GPU can run every model |
| `gce_instance` | gce | the instance name in Google Cloud |
| `gce_zone` | gce | the zone it lives in, e.g. `us-central1-a` |
| `gce_project` | gce | the project it is billed to |

Anything else is rejected, so a typo like `gce_zoen` fails loudly instead of being
silently ignored and leaving you wondering why the box cannot be found.

## The two rules

**No cloud host may use port 8188.** That is ComfyUI's default and your local
install already holds it. A tunnel on 8188 would point you at a remote machine while
everything on screen looked local — this is the specific mistake the tool exists to
prevent, so it is refused outright rather than warned about.

**No two hosts may share a port.** If two do, you cannot tell which one you reached,
which defeats the purpose of naming them.

## Changing OS means changing host

There is one box per OS, and you pick the one you want by name. Nothing is ever
reimaged: swapping a running box's OS would destroy its ComfyUI install and take
tens of minutes. Keeping one box per OS and stopping the idle one costs only disk.

Name them after whatever actually distinguishes them — `win-l4`, `linux-a100` —
rather than by OS alone, which stops working the day you have two Windows boxes with
different cards.
