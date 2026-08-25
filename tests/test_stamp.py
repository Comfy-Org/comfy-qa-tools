"""The evidence line: what a machine is, in a form you can paste.

The payloads here are shaped like the real ones — the local sample is taken from a
live ComfyUI on macOS/MPS, the cloud one adds the two fields a deployment carries.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from comfy_qa.stamp import ProbeError, Stamp, fetch, parse

LOCAL = {
    "system": {
        "os": "darwin",
        "comfyui_version": "0.33.0",
        "python_version": "3.12.13 (main, Aug 14 2026, 10:02:11) [Clang 17.0.0]",
        "pytorch_version": "2.13.0",
        "deploy_environment": "local-git",
    },
    "devices": [{"name": "mps", "type": "mps", "vram_total": 34359738368}],
}

CLOUD = {
    "system": {
        "os": "linux",
        "comfyui_version": "0.33.1",
        "python_version": "3.12.3",
        "pytorch_version": "2.13.0+cu128",
        "cloud_version": "1.52.0",
        "deploy_environment": "testcloud",
    },
    "devices": [{"name": "NVIDIA L4", "type": "cuda", "vram_total": 23609344000}],
}


def test_local_machine_reads_as_itself():
    stamp = parse(LOCAL, host="local", url="http://127.0.0.1:8188")
    assert stamp.comfyui_version == "0.33.0"
    assert stamp.pytorch_version == "2.13.0"
    assert stamp.devices == ["mps (32GB)"]
    assert stamp.cloud_version is None


def test_python_version_loses_the_build_noise():
    """`3.12.13 (main, ...) [Clang]` is unreadable in a one-line report."""
    assert parse(LOCAL, host="h", url="u").python_version == "3.12.13"


def test_cloud_fields_are_carried_when_present():
    stamp = parse(CLOUD, host="comfy-linux", url="http://127.0.0.1:8190")
    assert stamp.cloud_version == "1.52.0"
    assert stamp.deploy_environment == "testcloud"


def test_the_line_names_the_hardware_that_explains_a_result():
    line = parse(CLOUD, host="comfy-linux", url="u").line()
    assert "NVIDIA L4" in line
    assert "torch 2.13.0+cu128" in line
    assert line.startswith("comfy-linux")


def test_a_bare_payload_still_produces_a_line():
    """Half the fields missing is not a crash; you still learn which host."""
    stamp = parse({}, host="local", url="u")
    assert stamp.line() == "local"
    assert stamp.devices == []


def test_devices_without_vram_are_still_named():
    payload = {"devices": [{"name": "cpu", "type": "cpu", "vram_total": 0}]}
    assert parse(payload, host="h", url="u").devices == ["cpu"]


def test_json_keys_match_what_comfy_test_already_reads():
    """Same vocabulary means no format negotiation later."""
    keys = parse(CLOUD, host="h", url="u").as_dict()
    for expected in ("comfyui_version", "cloud_version", "deploy_environment"):
        assert expected in keys


def test_nothing_listening_says_so_and_points_at_the_port():
    def refuse(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    with pytest.raises(ProbeError) as caught:
        fetch("http://127.0.0.1:9999", host="ghost", opener=refuse)
    assert "nothing answered" in str(caught.value)
    assert "port" in caught.value.fix


def test_something_else_on_the_port_is_named_as_such():
    """The classic: a dev server, not ComfyUI, answering on the port you tunnelled."""

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def wrong(request, timeout=None):
        return Response(b"<html>hello</html>")

    with pytest.raises(ProbeError) as caught:
        fetch("http://127.0.0.1:8190", host="box", opener=wrong)
    assert "not with ComfyUI" in str(caught.value)


def test_a_successful_fetch_builds_the_url_correctly():
    seen = {}

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def ok(request, timeout=None):
        seen["url"] = request.full_url
        return Response(json.dumps(LOCAL).encode())

    stamp = fetch("http://127.0.0.1:8188/", host="local", opener=ok)
    assert seen["url"] == "http://127.0.0.1:8188/system_stats"
    assert isinstance(stamp, Stamp)
    assert stamp.comfyui_version == "0.33.0"
