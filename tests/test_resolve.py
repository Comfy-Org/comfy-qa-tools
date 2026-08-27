"""Naming a machine by what you want, instead of by what you called it.

Declaring both boxes and running `host go comfy-win` already switched operating
system; the friction was having to remember the name. A description — an
operating system, a card, or both — is the same act with less typing.

The rule these tests defend is the one the whole tool is built around: a
description is never a guess. It picks a machine only when exactly one fits,
the machine it picked is printed, and anything ambiguous is refused with both
candidates named.
"""

from __future__ import annotations

import pytest

from comfy_qa.config import ConfigError, find, parse, resolve

WIN = {
    "kind": "gce", "os": "Windows Server 2022", "gpu": "L4",
    "gce_instance": "comfy-win", "gce_zone": "us-central1-a",
    "gce_project": "proj", "port": 8190,
}
LINUX = {
    "kind": "gce", "os": "Ubuntu 22.04", "gpu": "A100",
    "gce_instance": "comfy-linux", "gce_zone": "us-central1-a",
    "gce_project": "proj", "port": 8191,
}


def hosts(**declared):
    """A host list. The default is the pair this tool is normally pointed at."""
    return parse({"hosts": declared or {
        "local": {"kind": "local"},
        "comfy-win": dict(WIN),
        "comfy-linux": dict(LINUX),
    }})


def test_an_operating_system_finds_the_host_that_runs_it():
    assert resolve(hosts(), "windows").host.name == "comfy-win"
    assert resolve(hosts(), "linux").host.name == "comfy-linux"


def test_a_card_finds_the_host_that_has_it():
    assert resolve(hosts(), "l4").host.name == "comfy-win"
    assert resolve(hosts(), "a100").host.name == "comfy-linux"


def test_a_card_is_matched_by_its_short_name():
    """Nobody types A100-80GB, and Google's full SKU is what discover writes."""
    declared = hosts(**{"big": dict(LINUX, gpu="A100-80GB")})
    assert resolve(declared, "a100").host.name == "big"


def test_both_axes_together_narrow_to_one():
    declared = hosts(**{
        "win-l4": dict(WIN),
        "win-a100": dict(WIN, gpu="A100", gce_instance="win-a100", port=8192),
    })
    assert resolve(declared, "windows/l4").host.name == "win-l4"
    assert resolve(declared, "windows/a100").host.name == "win-a100"


def test_the_axes_may_be_given_in_either_order():
    assert resolve(hosts(), "l4/windows").host.name == "comfy-win"


@pytest.mark.parametrize("typed", ["WINDOWS", "  windows  ", " WINDOWS/L4 ", "Windows/l4"])
def test_case_and_spacing_do_not_matter(typed):
    assert resolve(hosts(), typed).host.name == "comfy-win"


@pytest.mark.parametrize(
    "declared_os", ["Ubuntu 22.04", "Debian 12", "Rocky Linux 9"])
def test_every_distribution_answers_to_linux(declared_os):
    """No host is ever labelled "Linux" — discover writes Google's licence name."""
    declared = hosts(**{"box": dict(LINUX, os=declared_os)})
    assert resolve(declared, "linux").host.name == "box"


@pytest.mark.parametrize("keyword", ["macos", "local"])
def test_macos_and_local_mean_the_machine_you_are_sitting_at(keyword):
    """A local host usually declares no `os` at all, and still answers to these."""
    declared = hosts(**{"laptop": {"kind": "local"}, "comfy-win": dict(WIN)})
    assert resolve(declared, keyword).host.name == "laptop"


def test_an_exact_name_always_wins_over_a_description():
    """A box called `windows` that runs Ubuntu is still the box called `windows`."""
    declared = hosts(**{"windows": dict(LINUX), "comfy-win": dict(WIN)})
    assert resolve(declared, "windows").host.name == "windows"
    assert resolve(declared, "windows").selector is None


def test_the_machine_a_description_found_is_printed():
    """Resolution is never silent: this line is what stops a wrong-box result."""
    assert resolve(hosts(), "windows").line() == (
        "windows -> comfy-win (Windows Server 2022, L4)"
    )


def test_a_name_has_nothing_to_announce():
    assert resolve(hosts(), "comfy-win").line() is None


def test_a_description_that_fits_two_machines_is_refused():
    """Both axes are shown, so you can see which distinction you left out."""
    declared = hosts(**{
        "comfy-win": dict(WIN),
        "comfy-win-2": dict(WIN, gpu="A100-80GB", gce_instance="comfy-win-2", port=8192),
    })
    with pytest.raises(ConfigError) as raised:
        resolve(declared, "windows")
    message = str(raised.value)
    assert "comfy-win (Windows Server 2022, L4)" in message
    assert "comfy-win-2 (Windows Server 2022, A100-80GB)" in message
    assert "windows/l4" in message, "the sharper description is not suggested"


def test_a_description_that_fits_nothing_says_what_is_declared():
    declared = hosts(**{"local": {"kind": "local"}, "comfy-linux": dict(LINUX)})
    with pytest.raises(ConfigError) as raised:
        resolve(declared, "windows")
    message = str(raised.value)
    assert "nothing declared matches 'windows'" in message
    assert "comfy-linux (Ubuntu 22.04, A100)" in message
    assert "host discover" in message


def test_the_two_axes_are_joined_by_a_slash_and_nothing_else():
    """A near miss names the separator rather than reading as an unknown host."""
    with pytest.raises(ConfigError, match="two descriptions run together"):
        resolve(hosts(), "windows-l4")
    with pytest.raises(ConfigError, match="windows/l4"):
        resolve(hosts(), "windows l4")


def test_an_unknown_word_lists_the_names_the_systems_and_the_cards():
    with pytest.raises(ConfigError) as raised:
        resolve(hosts(), "nope")
    message = str(raised.value)
    assert "unknown host 'nope'" in message
    assert "declared:  local, comfy-win, comfy-linux" in message
    assert "windows" in message and "l4" in message


def test_find_still_hands_back_just_the_machine():
    """Everything that took a name keeps working, unchanged."""
    assert find(hosts(), "comfy-win").port == 8190
    assert find(hosts(), "windows").port == 8190
