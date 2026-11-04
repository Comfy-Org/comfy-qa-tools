"""One world for the testing commands: QA Desk, GitHub, cloud, a box, a ComfyUI.

`where`, `pass`, `fail`, `report`, `bug` and `record` read five things — QA
Desk's CLI, GitHub, the cloud environments' frontend header, Google's instance
list, and the ComfyUI behind a box's tunnel — and every one of them is a fake
here, wired in at the seam the package itself goes through: `qadesk.RUNNER`,
`build.github`, `env.probe_cloud`, `gcloud.Gcloud`, and a real HTTP server on a
real port for ComfyUI (`fakes.FakeComfyUI`). The suite's tripwires stay armed
under all of it.
"""

from __future__ import annotations

import base64
import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from fakes import FakeComfyUI, FakeGcloud, FakeQaDesk, _zone_url, free_port

FRONT = "repos/Comfy-Org/ComfyUI_frontend"
BACK = "repos/Comfy-Org/ComfyUI"
BOX = "qatest-box"
PROJECT = "comfy-qa"
ZONE = "us-central1-a"

# What each cloud environment serves (the commit in `x-frontend-version`), as
# live on 3 Nov: testcloud on cloud/1.60, staging and prod on cloud/1.59.
TESTCLOUD_SHA = "5708935b176fc29fcfd436228e8c814da1d37116"
STAGING_SHA = "9da8a5031f3915b4ecb110b2ff60dcfc8dfdf48f"
PROD_SHA = "072c3c1140172bbc8ff25cbdd1644cea3ec2f16c"
TAG_1_58_5 = "58a" * 13 + "5"
TAG_1_60_1 = "60a" * 13 + "1"

GITHUB = {
    f"{FRONT}/releases?per_page=100": [
        {"tag_name": "v1.58.5", "target_commitish": "core/1.58",
         "published_at": "2026-11-03T00:34:12Z"},
        {"tag_name": "v1.58.4", "target_commitish": "core/1.58",
         "published_at": "2026-11-01T19:16:24Z"},
        {"tag_name": "v1.60.0", "target_commitish": "main",
         "published_at": "2026-10-26T21:05:25Z"},
    ],
    f"{FRONT}/git/ref/tags/v1.58.5": {"object": {"sha": TAG_1_58_5, "type": "commit"}},
    f"{FRONT}/releases/tags/v1.58.5": {"tag_name": "v1.58.5"},
    f"{FRONT}/compare/cloud/1.60...{TESTCLOUD_SHA}": {"status": "behind", "behind_by": 1},
    f"{FRONT}/compare/cloud/1.60...{STAGING_SHA}": {"status": "diverged"},
    f"{FRONT}/compare/cloud/1.60...{PROD_SHA}": {"status": "diverged"},
    f"{FRONT}/compare/cloud/1.58...{TESTCLOUD_SHA}": {"status": "diverged"},
    f"{FRONT}/compare/cloud/1.58...{STAGING_SHA}": {"status": "diverged"},
    f"{FRONT}/compare/cloud/1.58...{PROD_SHA}": {"status": "diverged"},
    f"{FRONT}/commits/{TESTCLOUD_SHA}": {"commit": {"message":
        "[backport cloud/1.60] fix(billing): Billing tab style pass (#22856)"}},
    f"{FRONT}/commits/{STAGING_SHA}": {"commit": {"message":
        "[backport cloud/1.59] fix(telemetry): classify Sentry noise (#22853)"}},
    f"{FRONT}/commits/{PROD_SHA}": {"commit": {"message":
        "[backport cloud/1.59] offer only ready deployments (#22774)"}},
    f"{BACK}/pulls/17497": {"state": "open", "merged": False,
                            "head": {"sha": "9350a08d80" + "a" * 30}},
    f"{BACK}/git/ref/tags/v0.43.0": {"object": {"sha": "426f5eea52" + "b" * 30}},
}


def package_json(version: str) -> dict:
    content = json.dumps({"name": "@comfyorg/comfyui-frontend", "version": version,
                          "engines": {"node": ">=26.8.2 <27", "pnpm": ">=11.3"}})
    return {"content": base64.b64encode(content.encode()).decode()}


def frontend_pages(version: str) -> dict:
    """ComfyUI serving the frontend release `version`, as a browser meets it."""
    return {
        "/": ("text/html", '<!doctype html><html><head><script type="module" '
                           'crossorigin src="./assets/index-B08ReEXe.js"></script>'
                           "</head></html>"),
        "/assets/index-B08ReEXe.js": (
            "text/javascript",
            "function initSentry({app:e,dsn:t,enabled:n,isCloud:r}){Ce({app:e,dsn:t,"
            f"enabled:n,release:`{version}`,normalizeDepth:8}})}}"),
    }


@dataclass
class World:
    config: Path
    comfy: FakeComfyUI
    desk: FakeQaDesk
    hub: dict
    gc: FakeGcloud
    asked: list = field(default_factory=list)
    probed: list = field(default_factory=list)
    clouds: dict = field(default_factory=dict)
    tunnelled: set = field(default_factory=set)
    serving: str | None = "1.58.5"
    ran: list = field(default_factory=list)
    recorder: list = field(default_factory=list)

    def serves(self, frontend: str | None = "1.58.5", *, multi_user: bool = False,
               comfyui: str = "0.43.0") -> None:
        """ComfyUI behind the box's tunnel, serving `frontend`."""
        from fakes import SYSTEM_STATS

        payload = json.loads(json.dumps(SYSTEM_STATS))
        payload["system"]["comfyui_version"] = comfyui
        payload["system"]["os"] = "posix"
        # A box with no GPU, as the host list declares it: ComfyUI on its CPU.
        payload["devices"] = [{"name": "cpu", "type": "cpu", "vram_total": 0}]
        self.comfy.payload = payload
        self.comfy.pages.clear()
        if frontend:
            self.comfy.pages.update(frontend_pages(frontend))
        self.comfy.pages["/v2/manager/version"] = ("text/plain", "V4.3")
        users = {"storage": "server", "users": {}} if multi_user else \
            {"storage": "server", "migrated": True}
        self.comfy.pages["/api/users"] = ("application/json", json.dumps(users))
        self.comfy.mode = "serving"
        self.tunnelled.add(BOX)

    def box(self, status: str = "RUNNING") -> None:
        self.gc.instances = [{"name": BOX, "zone": _zone_url(PROJECT, ZONE),
                              "status": status}]


def hosts_toml(path: Path, port: int, *, os_name: str = "Ubuntu 22.04",
               gpu: str = "none") -> Path:
    path.write_text(
        f"[hosts.{BOX}]\n"
        "kind = 'gce'\n"
        f"os = '{os_name}'\n"
        f"gpu = '{gpu}'\n"
        f"gce_instance = '{BOX}'\n"
        f"gce_zone = '{ZONE}'\n"
        f"gce_project = '{PROJECT}'\n"
        f"port = {port}\n", encoding="utf-8")
    return path


def make(monkeypatch, tmp_path, *, desk: FakeQaDesk | None = None,
         os_name: str = "Ubuntu 22.04", gpu: str = "none", gc: FakeGcloud | None = None,
         fake_tunnels: bool = True) -> World:
    from comfy_qa import build, env, qadesk, tunnel
    from comfy_qa import gcloud as gcloud_module

    comfy = FakeComfyUI(mode="reset")
    world = World(config=hosts_toml(tmp_path / "hosts.toml", comfy.port, os_name=os_name,
                                    gpu=gpu),
                  comfy=comfy, desk=desk or FakeQaDesk(), hub=copy.deepcopy(GITHUB),
                  gc=gc or FakeGcloud(statuses=["RUNNING"], installed=True))
    world.clouds = {"testcloud": TESTCLOUD_SHA, "stagingcloud": STAGING_SHA,
                    "cloud": PROD_SHA}
    world.box("RUNNING")

    def github(path):
        world.asked.append(path)
        return (200, world.hub[path]) if path in world.hub else (404, None)

    def probe_cloud(name, url, *, resolve=True):
        world.probed.append(name)
        report = env.EnvReport(name=name, url=url, kind="cloud")
        sha = world.clouds.get(name)
        if sha is None:
            report.error = "unreachable: no route"
        else:
            report.sha = sha
        return report

    monkeypatch.setattr(build, "github", github)
    monkeypatch.setattr(qadesk, "RUNNER", world.desk)
    monkeypatch.setattr(env, "probe_cloud", probe_cloud)
    monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: world.gc)
    if fake_tunnels:
        # Which boxes have a tunnel open, as `where` asks it — a set, rather
        # than pid files, because `where` only ever asks.
        monkeypatch.setattr(tunnel, "status", lambda name, *a, **k: SimpleNamespace(
            running=name in world.tunnelled))
    # The cloud environments' own bundles are not read in a test: a cloud stamp
    # carries the commit, and the version only when the bundle says it.
    from comfy_qa import stamp as stamp_module

    real_served = stamp_module.served_frontend
    monkeypatch.setattr(stamp_module, "served_frontend", lambda url, **kw: (
        None if url.startswith("https://") else real_served(url, **kw)))
    return world


def run(world: World, *args: str):
    from comfy_qa.cli import app

    return CliRunner().invoke(app, [*args, "--config", str(world.config)])


def flat(output: str) -> str:
    return " ".join(output.split())


def unused_port() -> int:
    return free_port()
