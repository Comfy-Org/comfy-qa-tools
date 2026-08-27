"""The stamp against every `/system_stats` shape a real machine has produced.

`tests/test_stamp.py` covers the happy path. This file covers the reason the
stamp needs covering at all: the endpoint is not a contract, the stamp is pasted
into bug reports as evidence, and a field that is wrong is worse than a field
that is missing. Every payload comes from `tests/payloads.py`, where each one
records where it was captured.

The rule every test here is checking, in one sentence: **whatever the server
says, the stamp either reports it accurately or does not mention it.**
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

import payloads

from comfy_qa.stamp import ProbeError, fetch, parse


def stamp(payload, host="box", url="http://127.0.0.1:8188"):
    return parse(payload, host=host, url=url)


# --------------------------------------------------------------------------
# The four machines this tool is pointed at
# --------------------------------------------------------------------------

def test_apple_silicon_reads_as_the_mac_it_is():
    """Recorded from ComfyUI-Remote on this Mac. MPS reports `index: null`."""
    line = stamp(payloads.MPS_LOCAL).line()
    assert line == (
        "box · local-git · ComfyUI 0.28.3 · darwin · mps (32GB) · "
        "torch 2.14.0.dev20260731 · python 3.12.13"
    )


def test_nvidia_cuda_names_the_card_without_the_torch_noise():
    """ComfyUI formats CUDA names as `{device} {name} : {allocator_backend}`.

    The allocator backend is a torch build detail, not machine identity, and it
    is what turns a readable evidence line into an unreadable one.
    """
    line = stamp(payloads.CUDA_WITH_ALLOCATOR).line()
    assert "cuda:0 NVIDIA GeForce RTX 5090 (32GB)" in line
    assert "cudaMallocAsync" not in line


def test_an_older_cuda_name_without_an_allocator_is_left_alone():
    line = stamp(payloads.CUDA_NO_ALLOCATOR).line()
    assert "cuda:0 NVIDIA GeForce RTX 4090 (24GB)" in line


def test_a_dangling_allocator_separator_does_not_reach_the_line():
    """An empty allocator backend leaves the name ending in `" : "`."""
    payload = {"system": {}, "devices": [
        {"name": "cuda:0 NVIDIA L4 : ", "type": "cuda", "vram_total": 23609344000}]}
    assert stamp(payload).devices == ["cuda:0 NVIDIA L4 (22GB)"]


def test_rocm_is_not_relabelled_as_nvidia():
    """ROCm reports through the CUDA path: `type` says cuda, the name says Radeon."""
    line = stamp(payloads.ROCM_AS_CUDA).line()
    assert "Radeon 8060S Graphics (96GB)" in line
    assert "NVIDIA" not in line


def test_cpu_only_says_cpu_rather_than_claiming_no_hardware():
    line = stamp(payloads.CPU_ONLY).line()
    assert "cpu (16GB)" in line
    assert "torch 2.9.0+cpu" in line


def test_four_identical_cards_are_one_fact_not_four():
    """Listed one by one this is 180 characters of the same card repeated."""
    line = stamp(payloads.MULTI_GPU).line()
    assert "4 x NVIDIA L4 (22GB)" in line
    assert "cuda:3" not in line
    assert len(line) < 120


def test_the_json_still_carries_every_card_individually():
    """The line summarises; the record does not. Neither invents anything."""
    devices = stamp(payloads.MULTI_GPU).as_dict()["devices"]
    assert devices == [f"cuda:{i} NVIDIA L4 (22GB)" for i in range(4)]


def test_two_different_cards_are_never_collapsed_together():
    line = stamp(payloads.MULTI_GPU_MIXED).line()
    assert "cuda:0 NVIDIA H100 80GB HBM3 (80GB)" in line
    assert "cuda:1 NVIDIA L4 (22GB)" in line
    assert " x " not in line


# --------------------------------------------------------------------------
# Comfy Cloud
# --------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [payloads.CLOUD, payloads.TESTCLOUD,
                                     payloads.CLOUD_EARLIER])
def test_cloud_never_reports_an_empty_string_as_a_fact(payload):
    """Cloud sends `os`, `python_version` and `pytorch_version` as `""`.

    This is the shape that breaks naive parsers: the keys are present, so a
    `None` check passes them straight through into the report.
    """
    s = stamp(payload)
    assert s.os is None
    assert s.python_version is None
    assert s.pytorch_version is None
    assert "os" not in s.as_dict()
    assert "pytorch_version" not in s.as_dict()


def test_cloud_reports_the_two_fields_that_only_it_has():
    line = stamp(payloads.CLOUD).line()
    assert line == "box · cloud · ComfyUI v0.33.4 · cloud v0.233.5"


def test_a_cloud_version_that_is_a_sha_is_repeated_not_reformatted():
    """testcloud reports a bare commit SHA in `cloud_version`."""
    assert stamp(payloads.TESTCLOUD).cloud_version == "8461c5b"


def test_cloud_fields_are_read_from_the_top_level_too():
    s = stamp(payloads.TOP_LEVEL_CLOUD)
    assert s.cloud_version == "v0.217.0"
    assert s.deploy_environment == "stagingcloud"


def test_a_cloud_deployment_is_reached_through_the_api_alias():
    """Comfy Cloud answers the bare path with the frontend's HTML shell.

    Before this, `host stamp` against cloud printed "something else may be
    listening on it" — which sends a tester to check a port that is fine.
    """
    seen = []

    def opener(request, timeout=None):
        seen.append(request.full_url)
        if request.full_url.endswith("/api/system_stats"):
            return _body(json.dumps(payloads.CLOUD).encode())
        return _body(b"<!doctype html><html><title>ComfyUI</title></html>")

    s = fetch("https://cloud.comfy.org", host="cloud", opener=opener)
    assert seen == ["https://cloud.comfy.org/system_stats",
                    "https://cloud.comfy.org/api/system_stats"]
    assert s.cloud_version == "v0.233.5"


# --------------------------------------------------------------------------
# Versions where fields are missing, renamed or null
# --------------------------------------------------------------------------

def test_a_build_older_than_comfyui_version_says_nothing_about_it():
    """No `comfyui_version`, no `pytorch_version`, no `deploy_environment`."""
    s = stamp(payloads.ANCIENT)
    assert s.comfyui_version is None
    assert s.pytorch_version is None
    line = s.line()
    assert "ComfyUI" not in line
    assert "torch" not in line
    assert "None" not in line


def test_every_field_explicitly_null_leaves_only_the_host():
    s = stamp(payloads.ALL_NULL)
    assert s.line() == "box"
    assert s.as_dict() == {"host": "box", "url": "http://127.0.0.1:8188"}


def test_missing_torch_is_absent_rather_than_guessed():
    s = stamp(payloads.NO_TORCH)
    assert s.pytorch_version is None
    assert "torch" not in s.line()


def test_a_bare_payload_names_the_host_and_claims_nothing_else():
    assert stamp(payloads.EMPTY).line() == "box"


# --------------------------------------------------------------------------
# The python version string
# --------------------------------------------------------------------------

def test_the_build_junk_after_the_version_is_dropped():
    assert stamp(payloads.MPS_LOCAL).python_version == "3.12.13"
    assert stamp(payloads.ANCIENT).python_version == "3.10.11"


def test_a_whitespace_only_python_version_is_unknown_not_a_crash():
    """`"   ".split()[0]` raised IndexError and took the whole stamp with it."""
    s = stamp(payloads.PYTHON_VERSION_BLANK)
    assert s.python_version is None
    assert "python" not in s.line()


def test_a_python_version_that_arrives_as_a_number_does_not_crash():
    """`3.12` is a float, and floats have no `.split`."""
    assert stamp(payloads.PYTHON_VERSION_NUMBER).python_version == "3.12"


# --------------------------------------------------------------------------
# VRAM
# --------------------------------------------------------------------------

def test_vram_in_bytes_becomes_gigabytes():
    assert stamp(payloads.MPS_LOCAL).devices == ["mps (32GB)"]


def test_vram_that_is_already_humanised_is_repeated_verbatim():
    """Re-deriving a number from "22.0 GB" would only be guessing at the units."""
    assert stamp(payloads.VRAM_HUMANISED).devices == ["cuda:0 NVIDIA L4 (22.0 GB)"]


def test_vram_that_arrives_as_a_numeric_string_is_still_read_as_bytes():
    assert stamp(payloads.VRAM_NUMERIC_STRING).devices == ["cuda:0 NVIDIA L4 (22GB)"]


def test_a_sub_gigabyte_device_is_not_rounded_to_nothing():
    """`0GB` reads as "no VRAM", which is a different and wrong claim."""
    assert stamp(payloads.VRAM_SUB_GIGABYTE).devices == ["cuda:0 Jetson (0.25GB)"]


def test_a_boolean_vram_is_not_quietly_worth_zero_gigabytes():
    """`isinstance(True, int)` is True, so `True` used to render as `(0GB)`."""
    assert stamp(payloads.VRAM_BOOL).devices == ["cuda:0 X"]


def test_a_negative_vram_is_dropped_rather_than_printed():
    assert stamp(payloads.VRAM_NEGATIVE).devices == ["cuda:0 X"]


# --------------------------------------------------------------------------
# Malformed bodies: never crash, never guess
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "SYSTEM_IS_LIST", "SYSTEM_IS_STRING", "DEVICES_IS_DICT",
    "DEVICES_IS_STRING", "DEVICE_IS_STRING", "PYTHON_VERSION_BLANK",
    "PYTHON_VERSION_NUMBER",
])
def test_a_malformed_body_produces_a_line_instead_of_a_traceback(name):
    """Each of these raised AttributeError or IndexError out of `parse`.

    A stamp is what somebody runs when something is already going wrong. It
    does not get to be the second thing that fails.
    """
    line = stamp(getattr(payloads, name)).line()
    assert line.startswith("box")
    assert "None" not in line


def test_a_device_list_that_is_not_a_list_reports_no_devices():
    assert stamp(payloads.DEVICES_IS_STRING).devices == []
    assert stamp(payloads.DEVICES_IS_DICT).devices == []


# --------------------------------------------------------------------------
# The JSON and the line never disagree
# --------------------------------------------------------------------------

ALL_PAYLOADS = [(n, getattr(payloads, n)) for n in dir(payloads) if n.isupper()]


@pytest.mark.parametrize("name,payload", ALL_PAYLOADS)
def test_the_json_omits_exactly_what_the_line_omits(name, payload):
    """One capture, two shapes. A field in one and not the other is a bug."""
    s = stamp(payload)
    data = s.as_dict()
    for key in ("os", "comfyui_version", "python_version", "pytorch_version",
                "cloud_version", "deploy_environment"):
        value = getattr(s, key)
        assert (key in data) == (value is not None), (
            f"{name}: {key} disagrees between the line and --json"
        )
        if value is not None:
            assert value in s.line(), f"{name}: {key} is in --json but not the line"


@pytest.mark.parametrize("name,payload", ALL_PAYLOADS)
def test_no_payload_ever_puts_a_python_none_into_the_output(name, payload):
    s = stamp(payload)
    assert "None" not in s.line()
    assert "None" not in json.dumps(s.as_dict())


@pytest.mark.parametrize("name,payload", ALL_PAYLOADS)
def test_the_json_uses_comfyuis_own_key_names(name, payload):
    """No renamed fields: anything already reading /system_stats can read this."""
    ours = {"host", "url"}  # which machine we asked, and where
    comfyui = {"os", "devices", "comfyui_version", "python_version",
               "pytorch_version", "cloud_version", "deploy_environment"}
    assert set(stamp(payload).as_dict()) <= ours | comfyui


@pytest.mark.parametrize("name,payload", ALL_PAYLOADS)
def test_the_line_always_names_the_machine_first(name, payload):
    assert stamp(payload).line().split(" · ")[0] == "box"


# --------------------------------------------------------------------------
# Reaching the machine
# --------------------------------------------------------------------------

def _body(raw: bytes):
    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    return Response(raw)


def _http_error(code: int):
    def opener(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, code, "no", {}, None)

    return opener


@pytest.mark.parametrize("code", [401, 403])
def test_an_endpoint_that_wants_credentials_says_so(code):
    """"nothing answered" sent people to check a port that was answering fine."""
    with pytest.raises(ProbeError) as caught:
        fetch("https://cloud.example.com", host="c", opener=_http_error(code))
    assert "refused the request" in str(caught.value)
    assert "credentials" in caught.value.fix


def test_a_server_that_404s_both_paths_is_named_as_the_wrong_server():
    with pytest.raises(ProbeError) as caught:
        fetch("http://127.0.0.1:3000", host="box", opener=_http_error(404))
    assert "answered 404" in str(caught.value)
    assert "not a ComfyUI" in caught.value.fix


def test_a_socket_error_that_is_not_a_urlerror_is_still_reported_cleanly():
    """Not every failure arrives wrapped in URLError."""
    def opener(request, timeout=None):
        raise ConnectionResetError("reset by peer")

    with pytest.raises(ProbeError) as caught:
        fetch("http://127.0.0.1:8188", host="box", opener=opener)
    assert "nothing answered" in str(caught.value)


def test_a_local_comfyui_is_still_reached_on_the_bare_path_first():
    """The /api alias is a fallback, not a second round-trip on every stamp."""
    seen = []

    def opener(request, timeout=None):
        seen.append(request.full_url)
        return _body(json.dumps(payloads.MPS_LOCAL).encode())

    fetch("http://127.0.0.1:8188/", host="local", opener=opener)
    assert seen == ["http://127.0.0.1:8188/system_stats"]
