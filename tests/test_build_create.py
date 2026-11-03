"""`create` takes a build (S2-1, S2-3, S2-9), through the real CLI.

The box `create` makes carries the build in its host list entry, and the first
`comfy-qat go` installs it. A build that does not exist is refused before Google
is asked anything, which on `create` is the difference between a refusal and a
box that bills while comfy-cli says the same thing in a traceback.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from comfy_qa import build as build_mod
from comfy_qa import gcloud as gcloud_module
from comfy_qa import zones as zones_module
from comfy_qa.cli import app
from comfy_qa.config import load

# The create flow's own fake, from the file that has always tested it: it refuses
# any call it was not told to expect, which is what lets `calls == []` below
# mean "Google was asked nothing".
from test_create_cli import HOSTS, LATENCY, FakeGcloud


@pytest.fixture(autouse=True)
def never_time_a_real_connection(monkeypatch):
    monkeypatch.setattr(zones_module, "_connect",
                        lambda region, timeout=None: LATENCY.get(region, 500.0))


@pytest.fixture
def cli(tmp_path, monkeypatch):
    def invoke(*args):
        path = tmp_path / "hosts.toml"
        path.write_text(HOSTS, encoding="utf-8")
        cloud = FakeGcloud()
        monkeypatch.setattr(gcloud_module, "Gcloud", lambda *a, **k: cloud)
        result = CliRunner().invoke(app, ["create", *args, "--config", str(path)])
        result.gc = cloud                                    # type: ignore[attr-defined]
        result.hosts = path.read_text(encoding="utf-8")      # type: ignore[attr-defined]
        return result

    return invoke

REPO = "repos/Comfy-Org/ComfyUI"
HUB = {
    f"{REPO}/git/ref/tags/v0.43.0": {"object": {"sha": "426f5ee"}},
    "repos/Comfy-Org/ComfyUI_frontend/releases/tags/v1.58.4": {"tag_name": "v1.58.4"},
}


@pytest.fixture(autouse=True)
def github(monkeypatch):
    asked: list[str] = []

    def answer(path):
        asked.append(path)
        return (200, HUB[path]) if path in HUB else (404, None)

    monkeypatch.setattr(build_mod, "github", answer)
    return asked


def test_create_writes_the_build_into_the_box_s_host_list_entry(cli, tmp_path):
    result = cli("--os", "linux", "--gpu", "none", "--version", "v0.43.0",
                 "--frontend-version", "v1.58.4", "--yes")

    assert result.exit_code == 0, result.output
    assert ('build        = "comfyui=v0.43.0 frontend=v1.58.4 manager=on '
            'torch=default"') in result.hosts
    (box,) = [host for host in load(tmp_path / "hosts.toml") if host.is_remote]
    assert box.declared("build").startswith("comfyui=v0.43.0")


def test_create_says_which_build_go_will_install(cli):
    result = cli("--os", "linux", "--gpu", "none", "--version", "v0.43.0", "--dry-run")
    assert result.exit_code == 0, result.output
    assert "ComfyUI build, installed by `comfy-qat go`: ComfyUI v0.43.0" in result.output


def test_create_with_no_build_says_the_default_and_writes_none(cli):
    result = cli("--os", "linux", "--gpu", "none", "--yes")
    assert result.exit_code == 0, result.output
    assert "tip of its default branch" in result.output
    assert "Manager on" in result.output
    assert "build " not in result.hosts


@pytest.mark.parametrize("args, words", [
    (("--version", "v0.99.0"), "ComfyUI has no release v0.99.0"),
    (("--version", "v0.43.0", "--pr", "17497"), "only one of them can be right"),
    (("--torch", "2.12.1+cu130", "--gpu", "none"), "has no GPU"),
])
def test_create_refuses_a_bad_build_before_google_is_asked(cli, args, words):
    gpu = [] if "--gpu" in args else ["--gpu", "none"]
    result = cli("--os", "linux", *gpu, *args, "--yes")
    assert result.exit_code == 2, result.output
    assert words in result.output
    assert result.gc.calls == [], f"Google was asked something: {result.gc.calls}"


def test_create_no_manager_is_written_down(cli):
    result = cli("--os", "linux", "--gpu", "none", "--no-manager", "--yes")
    assert result.exit_code == 0, result.output
    assert "manager=off" in result.hosts


# --- every create a refusal hands back keeps the build (behaviour:F1) -----------
#
# A remedy that rewrites the command rewrites the box. The refusals `plan` and
# `create` print before anything exists each hand back a corrected `comfy-qat
# create`, and each was rebuilt from `--os`, `--gpu`, the name, `--reserve` and
# `--disk`: the build was dropped, so the box made from the fix came up on the
# tip of ComfyUI's default branch with Manager on. `--torch` without a CUDA
# build here, so the build is one every card in these cases can take.

BUILT = ("--version", "v0.43.0", "--frontend-version", "v1.58.4", "--no-manager",
         "--torch", "2.12.1")


@pytest.mark.parametrize("args", [
    ("--os", "linux", "--gpu", "l4", "--disk", "20"),
    ("--os", "linux", "--gpu", "l4", "--disk", "99999"),
    ("--os", "freebsd", "--gpu", "l4"),
    ("--os", "linux", "--gpu", "v100"),
    ("--os", "linux", "--gpu", "none", "--reserve", "--name", "qatest-cpu"),
    ("--gpu", "l4"),
    ("--os", "linux"),
    ("--os", "linux", "--gpu", "l4", "--zone", "us-central1-a", "--region", "us-east1"),
], ids=["disk-too-small", "disk-too-large", "no-such-os", "undrivable-card",
        "no-gpu-reserved", "no-os", "no-gpu", "zone-and-region"])
def test_every_create_a_refusal_offers_keeps_the_build(cli, args):
    from commandlines import flags, offered_argv

    result = cli(*args, *BUILT, "--yes")
    assert result.exit_code == 2, result.output

    offered = [words for words in offered_argv(result.output, "comfy-qat", "create")
               if not any(word.startswith("<") for word in words)]
    assert offered, f"no create was offered:\n{result.output}"
    wanted = flags(list(BUILT))
    for words in offered:
        given = flags(words[2:])
        assert {flag: given.get(flag) for flag in wanted} == wanted, (
            f"`{' '.join(words)}` drops the build `{' '.join(BUILT)}`")
