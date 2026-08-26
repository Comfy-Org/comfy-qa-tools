"""Refusing to stamp the wrong machine, without refusing the right one.

`host stamp` prints the line you paste into a bug report as proof of which
machine produced a result, so when the machine that answered contradicts the one
your host list declares, no line is printed at all. That makes the cost of a
false positive blocking rather than cosmetic: a tester with a perfectly good box
loses the command, on a `gpu` string they never typed — `host discover` wrote it,
out of Google's own `acceleratorType`.

Which is exactly what happened. The comparison was a raw substring test, and the
two vocabularies for one card do not contain each other:

    Google / host discover        ComfyUI /system_stats
    A100-80GB                     NVIDIA A100-SXM4-80GB
    H100-80GB                     NVIDIA H100 80GB HBM3
    H100-MEGA-80GB                NVIDIA H100 80GB HBM3

Three of the six cards `discover` can write refused themselves. The parametrised
cases below are the real acceleratorType strings, because those are the inputs
that actually occur.
"""

from __future__ import annotations

import pytest

from comfy_qa.config import Host
from comfy_qa.discover import accelerator
from comfy_qa.stamp import Stamp, mismatch

# Google's acceleratorType, what `host discover` writes from it, and what ComfyUI
# calls the same card on `/system_stats`. Taken together these are one machine
# describing itself twice, so none of them may ever contradict.
CARDS = [
    ("nvidia-l4", "L4", "cuda:0 NVIDIA L4 (22GB)"),
    ("nvidia-tesla-t4", "T4", "cuda:0 Tesla T4 (15GB)"),
    ("nvidia-tesla-a100", "A100", "cuda:0 NVIDIA A100-SXM4-40GB (40GB)"),
    ("nvidia-a100-80gb", "A100-80GB", "cuda:0 NVIDIA A100-SXM4-80GB (80GB)"),
    ("nvidia-h100-80gb", "H100-80GB", "cuda:0 NVIDIA H100 80GB HBM3 (80GB)"),
    ("nvidia-h100-mega-80gb", "H100-MEGA-80GB", "cuda:0 NVIDIA H100 80GB HBM3 (80GB)"),
    ("nvidia-tesla-v100", "V100", "cuda:0 Tesla V100-SXM2-16GB (16GB)"),
]


def declared(gpu, os="Ubuntu 22.04"):
    return Host(name="comfy-linux", kind="gce", port=8191, os=os, gpu=gpu,
                gce_instance="comfy-linux", gce_zone="us-central1-a",
                gce_project="proj")


def answered(devices, os="Linux 6.8.0"):
    return Stamp(host="comfy-linux", url="http://127.0.0.1:8191", os=os,
                 devices=devices if isinstance(devices, list) else [devices])


@pytest.mark.parametrize("accelerator_type,gpu,_device", CARDS,
                         ids=[card[0] for card in CARDS])
def test_discover_writes_the_gpu_string_these_cases_assume(accelerator_type, gpu, _device):
    """The fixtures are only worth anything if `discover` really produces them."""
    assert accelerator(
        {"guestAccelerators": [{"acceleratorType":
                                f"https://www.googleapis.com/compute/v1/projects/proj/"
                                f"zones/us-central1-a/acceleratorTypes/{accelerator_type}"}]}
    ) == gpu


@pytest.mark.parametrize("_accelerator_type,gpu,device", CARDS,
                         ids=[card[0] for card in CARDS])
def test_a_machine_that_is_the_declared_card_is_not_a_contradiction(_accelerator_type,
                                                                    gpu, device):
    """The regression, one case per card `host discover` can write.

    A substring test failed three of these, and under a refusal that means
    `host stamp` exits 1 and prints nothing on a correct 80GB box.
    """
    assert mismatch(declared(gpu), answered(device)) is None, (
        f"declared {gpu} contradicted itself when the box answered {device!r}"
    )


def test_a_narrower_declaration_accepts_a_fuller_name():
    """`A100` against an `A100-SXM4-80GB` is one card described in more detail.

    Not the same claim as "that port is not reaching comfy-linux", which is the
    only thing this refusal is entitled to make. 40GB versus 80GB is worth
    noticing and is not worth withholding an evidence line over — and `config`
    already reads the card axis this way: "`a100` finds an `A100-80GB`, because
    nobody types the full SKU".
    """
    assert mismatch(declared("A100"), answered("cuda:0 NVIDIA A100-SXM4-80GB (80GB)")) is None


def test_every_card_in_a_multi_gpu_box_counts():
    assert mismatch(declared("L4"), answered(
        ["cuda:0 NVIDIA L4 (22GB)", "cuda:1 NVIDIA L4 (22GB)",
         "cuda:2 NVIDIA L4 (22GB)", "cuda:3 NVIDIA L4 (22GB)"])) is None


def test_a_cpu_entry_is_not_an_accelerator():
    """ComfyUI lists the CPU as a device. Declaring an L4 and being told about a
    CPU alongside it is not a contradiction."""
    assert mismatch(declared("L4"), answered(
        ["cuda:0 NVIDIA L4 (22GB)", "cpu"])) is None


@pytest.mark.parametrize("gpu", ["", "none", "None", "  "])
def test_a_host_that_declares_no_card_has_nothing_to_contradict(gpu):
    """`discover` writes an empty `gpu` for a box with no accelerator, and people
    write `none` by hand. `config` treats both as "no card declared" already."""
    assert mismatch(declared(gpu), answered("cuda:0 NVIDIA L4 (22GB)")) is None


# --- and it still has to catch the thing it exists for ------------------------


@pytest.mark.parametrize("gpu,device", [
    ("L4", "cuda:0 NVIDIA A100-SXM4-80GB (80GB)"),
    ("A100-80GB", "cuda:0 NVIDIA L4 (22GB)"),
    ("H100-80GB", "cuda:0 NVIDIA L4 (22GB)"),
    ("T4", "cuda:0 NVIDIA L4 (22GB)"),
    # The wrong-machine case the refusal exists for: a tunnel that is really
    # reaching this Mac, reported under a cloud box's name.
    ("L4", "mps (32GB)"),
])
def test_a_different_card_is_still_a_contradiction(gpu, device):
    problem = mismatch(declared(gpu), answered(device))
    assert problem is not None, f"declared {gpu}, answered {device!r}, said nothing"
    assert "is not reaching comfy-linux" in problem


def test_an_l40s_no_longer_passes_as_an_l4():
    """The substring test was wrong in this direction too: `"l4" in "nvidia l40s"`
    is true, so a different card cleared. Whole tokens fix both directions at
    once."""
    problem = mismatch(declared("L4"), answered("cuda:0 NVIDIA L40S (48GB)"))
    assert problem is not None
    assert "L40S" in problem


def test_the_operating_system_axis_is_untouched():
    """A Windows box answering darwin is the original case, and it still fires
    before the card is even looked at."""
    problem = mismatch(declared("L4", os="Windows Server 2022"),
                       answered("mps (32GB)", os="darwin"))
    assert problem is not None
    assert "declared as Windows Server 2022" in problem
    assert "answered as darwin" in problem


# --- the operating-system half, checked the same way --------------------------
#
# The card half was wrong because two systems wrote the two sides in vocabularies
# that never agreed. The OS half has exactly the same shape, so it is worth
# asking the same question of it rather than assuming the answer.

# What `discover.operating_system()` writes, and every `os` a real ComfyUI puts
# on `/system_stats` for that same machine. Newer builds report `sys.platform`,
# older ones `os.name`, and Comfy Cloud sends an empty string — all three shapes
# are in `tests/payloads.py`, captured from real servers.
SAME_MACHINE = [
    ("Windows Server 2025", "win32"),
    ("Windows Server 2022", "win32"),
    ("Windows Server 2022", "nt"),
    ("Windows Server 2022", ""),
    ("Ubuntu 24.04", "linux"),
    ("Ubuntu 22.04", "linux"),
    ("Ubuntu 22.04", "posix"),
    ("Debian 12", "linux"),
    ("Rocky Linux 9", "linux"),
    ("macOS 15", "darwin"),
    # `discover` falls back to the raw GCE licence name, or to "unknown", for an
    # image it has no mapping for. Neither may be read as a family.
    ("sles-15", "linux"),
    ("unknown", "linux"),
    ("unknown", ""),
]


@pytest.mark.parametrize("declared_os,answered_os", SAME_MACHINE,
                         ids=[f"{d}-{a or 'empty'}" for d, a in SAME_MACHINE])
def test_a_machine_is_never_contradicted_by_its_own_declaration(declared_os, answered_os):
    """One machine describing itself twice, in the two vocabularies it has.

    This is the card bug asked of the OS axis: none of these pairings is a
    disagreement, so none of them may cost a tester the stamp.
    """
    problem = mismatch(declared("L4", os=declared_os),
                       answered("cuda:0 NVIDIA L4 (22GB)", os=answered_os))
    assert problem is None, f"refused a correct machine: {problem}"


@pytest.mark.parametrize("declared_os,answered_os", [
    ("Windows Server 2022", "darwin"),
    ("Windows Server 2022", "linux"),
    ("Ubuntu 22.04", "win32"),
    ("Ubuntu 22.04", "darwin"),
    ("macOS 15", "linux"),
])
def test_a_genuinely_different_operating_system_is_still_caught(declared_os, answered_os):
    """Failing open must not mean failing silent: the real case still fires."""
    problem = mismatch(declared("L4", os=declared_os),
                       answered("cuda:0 NVIDIA L4 (22GB)", os=answered_os))
    assert problem is not None
    assert "is not reaching comfy-linux" in problem


@pytest.mark.parametrize("answered_os", ["nt", "posix", "", "   ", None])
def test_an_os_that_names_no_family_is_skipped_rather_than_guessed(answered_os):
    """A known gap, and the right way round.

    `nt` and `posix` name no family, so an older ComfyUI is never compared on
    this axis at all. That loses a detection; reading them as a family would cost
    a correct machine its stamp instead. Do not close it by adding `"nt"` to
    `_OS_FAMILIES` — as a substring it reads `ubuntu` as Windows.
    """
    assert mismatch(declared("L4", os="Windows Server 2022"),
                    answered("cuda:0 NVIDIA L4 (22GB)", os=answered_os)) is None


@pytest.mark.parametrize("declared_os", ["macOS 15", "Ubuntu 22.04"])
def test_posix_names_two_families_at_once_so_it_can_never_be_mapped(declared_os):
    """The half of that gap that is not a judgement call.

    `os.name` is `posix` on macOS *and* on Linux, so an older ComfyUI answering
    `posix` is telling you it is one of two families and not which. Both
    declarations below are a machine describing itself correctly, and mapping
    `posix` to either family would refuse one of them.

    Worse than refusing a good machine: mapping it to `linux` would clear a
    tunnel that had really landed on this Mac while claiming to be a cloud box —
    the exact case the caller refuses for. A word-boundary match makes `posix`
    look safe to close, since it collides with no other OS name. It is still
    wrong. Only `nt` is closeable.
    """
    assert mismatch(declared("L4", os=declared_os),
                    answered("cuda:0 NVIDIA L4 (22GB)", os="posix")) is None


def test_discover_writes_the_os_strings_these_cases_assume():
    """Same guard the card cases have: fixtures that drift prove nothing."""
    from comfy_qa.discover import operating_system

    def wrote(licence):
        return operating_system(
            [{"boot": True, "licenses": [f"projects/p/global/licenses/{licence}"]}])

    assert wrote("windows-server-2022-dc") == "Windows Server 2022"
    assert wrote("ubuntu-2204-lts") == "Ubuntu 22.04"
    assert wrote("debian-12") == "Debian 12"
    assert wrote("rocky-linux-9") == "Rocky Linux 9"
    # No mapping: the raw licence tail, which must name no family.
    assert wrote("sles-15") == "sles-15"
    # No disks to read at all.
    assert operating_system([]) == "unknown"
