"""Real `/system_stats` bodies, recorded from real machines.

Every payload in the first half of this file was captured verbatim — from a
ComfyUI on this Mac, from Comfy Cloud, or from the fixtures comfy-cli itself
records for the same endpoint. They are here rather than hand-written because
the endpoint is not a stable contract: fields have been added, renamed and
blanked between versions, and a stamp built on an invented payload is a stamp
that only works on the machine it was written on.

The second half is the ugly middle: shapes a server has produced or plausibly
can, where the honest answer is "this field is unknown" rather than a guess.

Provenance is recorded per payload. Nothing here is aspirational.
"""

from __future__ import annotations

# --------------------------------------------------------------------------
# Recorded verbatim
# --------------------------------------------------------------------------

# Apple silicon / MPS. Captured from ComfyUI-Remote on this Mac (port 8189)
# via `comfy system-stats`, 2026-08. Note `index: null` — MPS has no ordinal.
MPS_LOCAL = {
    "system": {
        "os": "darwin",
        "ram_total": 34359738368,
        "ram_free": 6378110976,
        "comfyui_version": "0.28.3",
        "required_frontend_version": "1.45.21",
        "installed_templates_version": "0.11.15",
        "required_templates_version": "0.11.15",
        "comfy_package_versions": [
            {"name": "comfyui-frontend-package", "installed": "1.45.21",
             "required": "1.45.21"},
            {"name": "comfyui-workflow-templates", "installed": "0.11.15",
             "required": "0.11.15"},
        ],
        "python_version": (
            "3.12.13 (main, Mar  3 2026, 12:39:30) "
            "[Clang 17.0.0 (clang-1700.6.3.2)]"
        ),
        "pytorch_version": "2.14.0.dev20260731",
        "embedded_python": False,
        "deploy_environment": "local-git",
        "argv": ["/Users/aliranjah/ComfyUI/ComfyUI-Remote/main.py",
                 "--port", "8189", "--listen", "127.0.0.1"],
    },
    "devices": [{
        "name": "mps",
        "type": "mps",
        "index": None,
        "vram_total": 34359738368,
        "vram_free": 6378143744,
        "torch_vram_total": 34359738368,
        "torch_vram_free": 6378143744,
    }],
}

# Comfy Cloud. Captured byte-identical from `curl -s
# https://cloud.comfy.org/api/system_stats` during the 2026-08-25 cloud bug
# hunt. This is the shape that breaks naive parsers: `devices` is empty and
# `os`, `python_version` and `pytorch_version` are EMPTY STRINGS, not missing
# keys and not null. A parser that only guards against `None` will happily
# report a machine whose OS is "".
CLOUD = {
    "devices": [],
    "system": {
        "argv": [],
        "cloud_version": "v0.233.5",
        "comfyui_version": "v0.33.4",
        "deploy_environment": "cloud",
        "embedded_python": False,
        "os": "",
        "python_version": "",
        "pytorch_version": "",
        "ram_free": 0,
        "ram_total": 0,
        "workflow_templates_version": "0.7.64",
    },
}

# testcloud, captured the same way on 2026-08-26. Same shape, and proof that
# `cloud_version` is not always a version: this environment reports a bare
# commit SHA in the field. The stamp repeats what it was told rather than
# trying to make it look like a version number.
TESTCLOUD = {
    "devices": [],
    "system": {
        "argv": [],
        "cloud_version": "8461c5b",
        "comfyui_version": "v0.34.0",
        "deploy_environment": "cloud",
        "embedded_python": False,
        "os": "",
        "python_version": "",
        "pytorch_version": "",
        "ram_free": 0,
        "ram_total": 0,
        "workflow_templates_version": "0.7.64",
    },
}

# An earlier capture of the same cloud endpoint, kept because it proves
# `cloud_version` moves independently of everything else.
CLOUD_EARLIER = {
    "devices": [],
    "system": {
        "cloud_version": "v0.217.0",
        "comfyui_version": "v0.33.1",
        "deploy_environment": "cloud",
        "os": "",
        "python_version": "",
        "pytorch_version": "",
    },
}

# NVIDIA CUDA. Device-name shape taken from comfy-cli's own recorded fixture
# (tests/comfy_cli/command/test_system.py) — an older server that formatted
# the name as "{device} {name}" with no allocator backend.
CUDA_NO_ALLOCATOR = {
    "system": {
        "os": "linux",
        "ram_total": 68719476736,
        "ram_free": 40000000000,
        "comfyui_version": "0.28.3",
        "python_version": "3.11.9 (main, Apr  2 2026, 09:12:44) [GCC 13.2.0]",
        "pytorch_version": "2.5.1+cu124",
        "embedded_python": False,
    },
    "devices": [{
        "name": "cuda:0 NVIDIA GeForce RTX 4090",
        "type": "cuda",
        "index": 0,
        "vram_total": 25769803776,
        "vram_free": 21474836480,
        "torch_vram_total": 4294967296,
        "torch_vram_free": 2147483648,
    }],
}

# Current ComfyUI formats a CUDA name as "{device} {name} : {allocator}".
# The allocator backend is a torch build detail, not machine identity, and it
# is the difference between a readable evidence line and an unreadable one.
CUDA_WITH_ALLOCATOR = {
    "system": {
        "os": "linux",
        "comfyui_version": "0.33.4",
        "python_version": "3.12.3 (main, Feb  4 2026, 14:48:35) [GCC 13.2.0]",
        "pytorch_version": "2.9.0+cu128",
        "embedded_python": False,
        "deploy_environment": "local-git",
    },
    "devices": [{
        "name": "cuda:0 NVIDIA GeForce RTX 5090 : cudaMallocAsync",
        "type": "cuda",
        "index": 0,
        "vram_total": 34089205760,
        "vram_free": 33000000000,
    }],
}

# ROCm reports itself through the CUDA path, so `type` says "cuda" and the
# name says Radeon. The stamp must not translate that into "NVIDIA".
ROCM_AS_CUDA = {
    "system": {
        "os": "linux",
        "comfyui_version": "0.33.4",
        "python_version": "3.12.3",
        "pytorch_version": "2.9.0+rocm6.4",
    },
    "devices": [{
        "name": "cuda:0 Radeon 8060S Graphics : native",
        "type": "cuda",
        "index": 0,
        "vram_total": 103079215104,
    }],
}

# --------------------------------------------------------------------------
# Shapes a server produces that a naive parser gets wrong
# --------------------------------------------------------------------------

# Multi-GPU. ComfyUI lists every visible torch device, primary first. Four
# identical L4s joined naively make a 200-character line nobody will paste.
MULTI_GPU = {
    "system": {
        "os": "linux",
        "comfyui_version": "0.33.4",
        "python_version": "3.12.3",
        "pytorch_version": "2.9.0+cu128",
    },
    "devices": [
        {"name": f"cuda:{i} NVIDIA L4 : cudaMallocAsync", "type": "cuda",
         "index": i, "vram_total": 23609344000}
        for i in range(4)
    ],
}

# Mixed multi-GPU: two different cards. These must NOT be collapsed together.
MULTI_GPU_MIXED = {
    "system": {"comfyui_version": "0.33.4", "os": "linux"},
    "devices": [
        {"name": "cuda:0 NVIDIA H100 80GB HBM3 : cudaMallocAsync",
         "type": "cuda", "index": 0, "vram_total": 85520809984},
        {"name": "cuda:1 NVIDIA L4 : cudaMallocAsync",
         "type": "cuda", "index": 1, "vram_total": 23609344000},
    ],
}

# CPU-only. ComfyUI still emits a device entry, and `vram_total` is system RAM.
CPU_ONLY = {
    "system": {
        "os": "linux",
        "comfyui_version": "0.33.4",
        "python_version": "3.12.3",
        "pytorch_version": "2.9.0+cpu",
    },
    "devices": [{
        "name": "cpu", "type": "cpu", "index": None,
        "vram_total": 16777216000, "vram_free": 800000000,
    }],
}

# An older ComfyUI, before `comfyui_version`, `pytorch_version` and
# `deploy_environment` existed on this endpoint at all. Every one of those is
# unknown, and must be absent rather than guessed.
ANCIENT = {
    "system": {
        "os": "posix",
        "python_version": "3.10.11 (main, Apr 20 2024, 19:02:41) [GCC 11.2.0]",
        "embedded_python": False,
    },
    "devices": [{
        "name": "cuda:0 NVIDIA GeForce RTX 3090",
        "type": "cuda", "index": 0,
        "vram_total": 25757220864, "vram_free": 24000000000,
        "torch_vram_total": 0, "torch_vram_free": 0,
    }],
}

# Every field present and explicitly null — a server that builds the dict up
# front and fills it in, and failed to.
ALL_NULL = {
    "system": {
        "os": None, "comfyui_version": None, "python_version": None,
        "pytorch_version": None, "cloud_version": None,
        "deploy_environment": None,
    },
    "devices": [{"name": None, "type": None, "vram_total": None}],
}

# torch absent: the key is simply not there. Seen on servers that fail to
# import torch and still serve the endpoint.
NO_TORCH = {
    "system": {"os": "linux", "comfyui_version": "0.33.4",
               "python_version": "3.12.3"},
    "devices": [],
}

# `cloud_version` / `deploy_environment` promoted to the top level rather than
# nested under `system`. The parser already accepts both; this pins it.
TOP_LEVEL_CLOUD = {
    "cloud_version": "v0.217.0",
    "deploy_environment": "stagingcloud",
    "system": {"comfyui_version": "v0.33.1"},
    "devices": [],
}

# --------------------------------------------------------------------------
# Hostile / malformed — must never crash, must never guess
# --------------------------------------------------------------------------

PYTHON_VERSION_BLANK = {"system": {"comfyui_version": "0.33.4",
                                   "python_version": "   "}}
PYTHON_VERSION_NUMBER = {"system": {"comfyui_version": "0.33.4",
                                    "python_version": 3.12}}
SYSTEM_IS_LIST = {"system": [], "devices": []}
SYSTEM_IS_STRING = {"system": "unavailable", "devices": []}
DEVICES_IS_DICT = {"system": {"comfyui_version": "0.33.4"},
                   "devices": {"cuda:0": {}}}
DEVICES_IS_STRING = {"system": {"comfyui_version": "0.33.4"},
                     "devices": "none"}
DEVICE_IS_STRING = {"system": {"comfyui_version": "0.33.4"},
                    "devices": ["cuda:0"]}
VRAM_HUMANISED = {"system": {"comfyui_version": "0.33.4"},
                  "devices": [{"name": "cuda:0 NVIDIA L4", "type": "cuda",
                               "vram_total": "22.0 GB"}]}
VRAM_NUMERIC_STRING = {"system": {"comfyui_version": "0.33.4"},
                       "devices": [{"name": "cuda:0 NVIDIA L4", "type": "cuda",
                                    "vram_total": "23609344000"}]}
VRAM_SUB_GIGABYTE = {"system": {"comfyui_version": "0.33.4"},
                     "devices": [{"name": "cuda:0 Jetson", "type": "cuda",
                                  "vram_total": 268435456}]}
VRAM_BOOL = {"system": {"comfyui_version": "0.33.4"},
             "devices": [{"name": "cuda:0 X", "type": "cuda",
                          "vram_total": True}]}
VRAM_NEGATIVE = {"system": {"comfyui_version": "0.33.4"},
                 "devices": [{"name": "cuda:0 X", "type": "cuda",
                              "vram_total": -1}]}
EMPTY = {}
