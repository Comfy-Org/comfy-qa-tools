"""The stamp is evidence, so it has to be hard to forge and hard to break.

`fetch` talks to whatever is on a port. That is the point — and the risk. On the
near end of a tunnel, "whatever is on that port" can be another machine's
ComfyUI, a dev server, a login page, or something that simply hangs. A stamp is
pasted into a bug report and believed, so anything that lets the wrong machine
produce a plausible-looking line is a defect, and so is anything that turns a
strange answer into a traceback.

The payloads here are deliberately not the ones a healthy ComfyUI sends.
"""

from __future__ import annotations

import http.client
import io
import json

import pytest

from comfy_qa.stamp import ProbeError, fetch, parse

COMFYUI = {
    "system": {
        "os": "darwin",
        "comfyui_version": "0.33.0",
        "python_version": "3.12.13 (main, Aug 14 2026, 10:02:11) [Clang 17.0.0]",
        "pytorch_version": "2.13.0",
    },
    "devices": [{"name": "mps", "type": "mps", "vram_total": 34359738368}],
}


class Response(io.BytesIO):
    """Enough of an http.client.HTTPResponse for `fetch`."""

    def __init__(self, body: bytes, url: str = "http://127.0.0.1:8190/system_stats"):
        super().__init__(body)
        self._url = url

    def geturl(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def serving(payload, url="http://127.0.0.1:8190/system_stats"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def opener(request, timeout=None):
        return Response(body, url)

    return opener


def raising(exc):
    def opener(request, timeout=None):
        raise exc

    return opener


# --- something else on the port, answering in JSON ----------------------------


@pytest.mark.parametrize("payload", [
    {"ok": True},
    {"status": "healthy", "service": "some-api"},
    {"system": "up"},
    [],
    {"devices": []},
])
def test_valid_json_that_is_not_system_stats_is_refused(payload):
    """A JSON body is not proof you reached ComfyUI.

    Every field is optional in the parser, so a health-check endpoint stamps
    cleanly as the bare host name — a line that looks like a successful probe
    and says nothing true about any machine.
    """
    with pytest.raises(ProbeError) as caught:
        fetch("http://127.0.0.1:8190", host="comfy-win", opener=serving(payload))

    assert "not with ComfyUI" in str(caught.value)


def test_a_real_system_stats_body_is_still_accepted():
    """The check above must not reject the thing it is guarding."""
    stamp = fetch("http://127.0.0.1:8190", host="comfy-win", opener=serving(COMFYUI))
    assert stamp.comfyui_version == "0.33.0"


def test_a_redirect_to_another_machine_is_refused():
    """The forgery that is hardest to notice.

    Anything on the tunnelled port can answer 302 and send the probe to the local
    ComfyUI on 8188. urllib follows it silently, and the stamp then reports this
    Mac's build, hardware and torch version under the cloud box's name.
    """
    with pytest.raises(ProbeError) as caught:
        fetch("http://127.0.0.1:8190", host="comfy-win",
              opener=serving(COMFYUI, url="http://127.0.0.1:8188/system_stats"))

    assert "8188" in str(caught.value)


# --- the line has to survive being pasted -------------------------------------


def test_the_stamp_line_is_always_one_line():
    """A newline in any field splits the evidence line into two."""
    payload = {"system": {"os": "darwin\nlocal", "comfyui_version": "0.33.0"}}
    line = parse(payload, host="comfy-win", url="u").line()
    assert "\n" not in line and "\r" not in line


def test_a_hostile_field_cannot_forge_extra_segments():
    """`os` is a string the answering machine chooses. So is every other field.

    The separator is structure. A value carrying it invents fields the machine
    never claimed, in a line whose whole job is to be read as facts.
    """
    payload = {"system": {
        "os": "linux · ComfyUI 9.9.9 · NVIDIA H100 (80GB)",
        "comfyui_version": "0.33.0",
    }}
    line = parse(payload, host="comfy-win", url="u").line()
    assert line.count(" · ") == 2, f"three fields became {line.count(' · ') + 1}: {line}"


def test_terminal_escapes_are_stripped_from_the_line():
    """A stamp is printed to a terminal and pasted into Slack."""
    payload = {"system": {"comfyui_version": "0.33.0\x1b[2K\x1b[1G0.99.0"}}
    line = parse(payload, host="h", url="u").line()
    assert "\x1b" not in line


def test_an_enormous_field_cannot_bury_the_report():
    payload = {"system": {"comfyui_version": "0.33.0" + "!" * 10_000}}
    assert len(parse(payload, host="h", url="u").line()) < 500


# --- shapes a real server will eventually send --------------------------------


@pytest.mark.parametrize("devices", [
    "mps",
    ["mps"],
    [None],
    [["mps"]],
    {"name": "mps"},
    42,
])
def test_devices_of_any_shape_do_not_raise(devices):
    """`devices` is read straight off the wire; only one shape is a dict."""
    assert isinstance(parse({"devices": devices}, host="h", url="u").devices, list)


@pytest.mark.parametrize("system", ["up", ["up"], 3, True])
def test_a_system_field_that_is_not_a_table_does_not_raise(system):
    assert parse({"system": system}, host="h", url="u").host == "h"


@pytest.mark.parametrize("version", [3.12, 312, ["3.12.13"], {"major": 3}, None])
def test_a_version_that_is_not_a_string_does_not_raise(version):
    """`.split()` on a float is an AttributeError, and a traceback is a defect."""
    stamp = parse({"system": {"python_version": version, "os": version}},
                  host="h", url="u")
    assert isinstance(stamp.line(), str)


def test_a_vram_total_that_is_not_a_number_does_not_raise():
    payload = {"devices": [{"name": "mps", "vram_total": "lots"}]}
    assert parse(payload, host="h", url="u").devices == ["mps"]


# --- the network half ---------------------------------------------------------


@pytest.mark.parametrize("failure", [
    TimeoutError("timed out"),
    ConnectionResetError("reset by peer"),
    http.client.RemoteDisconnected("closed without a response"),
    http.client.IncompleteRead(b"{"),
    OSError("host is down"),
])
def test_a_connection_that_breaks_mid_answer_is_a_message_not_a_traceback(failure):
    """A read timeout is not a URLError, and a tunnel that drops is routine."""
    with pytest.raises(ProbeError):
        fetch("http://127.0.0.1:8190", host="comfy-win", opener=raising(failure))


def test_a_service_that_404s_is_not_reported_as_nothing_being_there():
    """"Nothing answered" sends someone to start a server on a taken port.

    A dev server on the tunnelled port answers 404 for /system_stats. That is not
    an absence, it is the wrong machine — and the difference decides what the
    tester does next.
    """
    import urllib.error

    failure = urllib.error.HTTPError("http://127.0.0.1:8190/system_stats", 404,
                                     "Not Found", {}, None)
    with pytest.raises(ProbeError) as caught:
        fetch("http://127.0.0.1:8190", host="comfy-win", opener=raising(failure))

    assert "404" in str(caught.value)
    assert "nothing answered" not in str(caught.value)


@pytest.mark.parametrize("url", ["", "127.0.0.1:8190", "nonsense"])
def test_a_url_that_is_not_a_url_is_a_message_not_a_traceback(url):
    """`urllib.request.Request` raises ValueError before any opener is reached."""
    with pytest.raises(ProbeError):
        fetch(url, host="comfy-win", opener=serving(COMFYUI))


def test_a_body_that_never_ends_is_not_read_into_memory():
    """A wrong service can stream forever; the timeout only covers each read."""
    class Endless(io.RawIOBase):
        def read(self, size=-1):
            if size is None or size < 0:
                raise AssertionError("read the whole body with no limit")
            return b"a" * size

        def geturl(self):
            return "http://127.0.0.1:8190/system_stats"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with pytest.raises(ProbeError) as caught:
        fetch("http://127.0.0.1:8190", host="comfy-win",
              opener=lambda request, timeout=None: Endless())

    # Two properties, and only the first was pinned. `Endless` asserts on an
    # unbounded read, so a dropped `read(MAX_BODY + 1)` fails loudly. But the
    # refusal that follows it was covered only by `raises(ProbeError)`, and
    # deleting that check still raises — an oversized body just falls through to
    # the generic "not with ComfyUI's" after a second wasted round-trip. Proven
    # by removing the check: the suite stayed green at 965. So the size is named
    # here, because "1024KB" is what tells someone the port is streaming at them
    # rather than serving something merely unrecognisable.
    assert "more than" in str(caught.value)


# --- the declared machine and the answering machine ---------------------------


def test_a_machine_that_contradicts_its_declaration_is_flagged():
    """The host list says Windows and an L4; the port answers darwin and mps.

    That is not a cosmetic mismatch — it means the port is not reaching the box
    you named, which is the whole failure this tool exists to prevent.
    """
    from comfy_qa.config import Host
    from comfy_qa.stamp import mismatch

    win = Host(name="comfy-win", kind="gce", port=8190, os="Windows Server 2022",
               gpu="L4", gce_instance="comfy-win", gce_zone="z", gce_project="p")
    stamp = parse(COMFYUI, host="comfy-win", url=win.url)

    complaint = mismatch(win, stamp)
    assert complaint and "darwin" in complaint


def test_a_machine_that_matches_its_declaration_is_not_flagged():
    from comfy_qa.config import Host
    from comfy_qa.stamp import mismatch

    linux = Host(name="comfy-linux", kind="gce", port=8191, os="Ubuntu 22.04",
                 gpu="L4", gce_instance="comfy-linux", gce_zone="z", gce_project="p")
    payload = {
        "system": {"os": "linux", "comfyui_version": "0.33.1"},
        "devices": [{"name": "NVIDIA L4", "type": "cuda", "vram_total": 23609344000}],
    }
    assert mismatch(linux, parse(payload, host="comfy-linux", url=linux.url)) is None


def test_an_undeclared_machine_is_not_flagged():
    """`os` and `gpu` are optional for a local host; silence is not a mismatch."""
    from comfy_qa.config import Host
    from comfy_qa.stamp import mismatch

    local = Host(name="local", kind="local", port=8188)
    assert mismatch(local, parse(COMFYUI, host="local", url=local.url)) is None
