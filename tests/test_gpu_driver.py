"""The cards this tool can bring up, and the ones it refuses to sell you.

WHAT HAPPENED. `comfy-qat create --os linux --gpu v100 --yes` succeeded on real
hardware on 2026-09-09. The startup script finished with exit status 0, the
driver packages installed, `nvidia-open set on hold` appeared in the log, the
instance booted, and `lspci` showed a `Tesla V100-SXM2-16GB` sitting in it. No
NVIDIA kernel module loaded, `nvidia-smi` failed, and a clean reboot changed
nothing:

    NVRM: The NVIDIA GPU 0000:00:04.0 (PCI ID: 10de:1db1)
    NVRM: installed in this system is not supported by open
    NVRM: nvidia.ko because it does not include the required GPU
    NVRM: System Processor (GSP).

Google's `cuda_installer.pyz` installs the OPEN NVIDIA kernel module, and the
open module needs a GSP — Turing and newer. Four of the cards this tool offered
are older than that: P4 and P100 (Pascal), V100 (Volta), K80 (Kepler). Each one
of them produced a billing box with a dead GPU, and `create` reported success.

WHY THE TESTS ARE SHAPED LIKE THIS. The defect was not in one function; it was
that four separate places advertised a card and no place refused it. So this
file asserts the refusal once and then walks every surface that names a card —
`--gpu` help, the "no card called" list, `quota list`, `quota request`, `status`
— against the same table, so a fifth surface added later is the only way to
reintroduce it. And it pins the driver script itself: if the provisioning stops
installing the open module, `test_the_driver_install_is_still_the_open_module`
fails, which is the prompt to revisit which cards are refused.

The one thing NOT asserted here is the physics. `GSP_ARCHITECTURES` is a claim
about silicon, sourced in the comment above it, and no test can check it — which
is exactly why the comment carries NVIDIA's own wording and the dmesg output
rather than "Turing and newer" on its own.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from comfy_qa.cli import app
from comfy_qa.create import (
    CARDS,
    GSP_ARCHITECTURES,
    LINUX_DRIVER,
    NO_DRIVER,
    card_for,
    card_named,
    drivable_cards,
    plan,
    undrivable_cards,
)
from comfy_qa.lifecycle import LifecycleError

# Named here rather than derived from `CARDS`, because a test that reads its
# answer out of the subject cannot fail. If someone marks the V100 as Turing to
# make a create go through, this is what says otherwise.
PRE_TURING = ("p4", "p100", "v100", "k80")
HAS_GSP = ("t4", "l4", "a100", "a100-80gb", "h100")


# --- the table states which cards work, and why ----------------------------


def test_every_card_names_its_architecture():
    """A card with no architecture cannot be judged, and `Card` will not build
    one — this is the assertion that the field stayed required."""
    for key, card in CARDS.items():
        assert card.architecture, f"{key} does not say what generation it is"


def test_the_two_lists_are_the_whole_table_and_do_not_overlap():
    assert sorted(PRE_TURING + HAS_GSP) == sorted(CARDS)
    assert not set(PRE_TURING) & set(HAS_GSP)


@pytest.mark.parametrize("key", HAS_GSP)
def test_a_turing_or_newer_card_is_drivable(key):
    assert CARDS[key].has_gsp, (
        f"{key} is {CARDS[key].architecture}, which has a GSP, so the open "
        f"kernel module can drive it"
    )
    assert key in drivable_cards()


@pytest.mark.parametrize("key", PRE_TURING)
def test_a_pre_turing_card_is_not_drivable(key):
    assert not CARDS[key].has_gsp, (
        f"{key} is {CARDS[key].architecture} and has no GPU System Processor. "
        f"The open kernel module will not load on it, whatever the create says."
    )
    assert key in undrivable_cards()


def test_the_gsp_line_is_drawn_at_turing():
    """The set is the rule, and the rule is one sentence: Turing introduced the
    GSP. Kepler, Maxwell, Pascal and Volta predate it."""
    for old in ("Kepler", "Maxwell", "Pascal", "Volta"):
        assert old not in GSP_ARCHITECTURES
    for new in ("Turing", "Ampere", "Ada", "Hopper"):
        assert new in GSP_ARCHITECTURES


# --- nothing is created, and nothing is billed -----------------------------


@pytest.mark.parametrize("key", PRE_TURING)
def test_planning_a_pre_turing_box_is_refused_offline(key):
    """`plan` runs before the project is read, before the instance list and
    before the minute of quota — so this refusal costs a second and no money."""
    with pytest.raises(LifecycleError) as raised:
        plan(os_choice="linux", gpu=key)
    message = str(raised.value)
    assert raised.value.kind == NO_DRIVER
    assert "GPU System Processor" in message
    assert CARDS[key].name in message
    assert CARDS[key].architecture in message


@pytest.mark.parametrize("key", HAS_GSP)
def test_planning_a_drivable_box_is_not_refused(key):
    """The other half of the pair. A refusal that fired on everything would pass
    every test above it and make the tool useless."""
    assert plan(os_choice="linux", gpu=key).card.key == key


def test_the_refusal_says_what_to_use_instead():
    with pytest.raises(LifecycleError) as raised:
        plan(os_choice="linux", gpu="p100")
    assert "comfy-qat create --os linux --gpu t4" in (raised.value.fix or "")


def test_the_refusal_follows_the_os_that_was_asked_for():
    """A Windows box gets Windows advice. The fix line used to be the only place
    `--os linux` was hard-coded, and pasting it after asking for Windows silently
    changes the box you get."""
    with pytest.raises(LifecycleError) as raised:
        plan(os_choice="windows", gpu="v100")
    assert "--os windows" in (raised.value.fix or "")


@pytest.mark.parametrize("key", PRE_TURING)
def test_create_refuses_a_pre_turing_card_and_contacts_nothing(tmp_path, key):
    """End to end, with no cloud behind it at all.

    `Gcloud` is NOT faked here, deliberately: if the refusal ever moves to after
    the project read, this test stops passing for a reason rather than passing on
    a fake that happily answers. Exit 2, no traceback, and nothing written.
    """
    path = tmp_path / "hosts.toml"
    path.write_text("[hosts.local]\nkind = \"local\"\nport = 8188\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    result = CliRunner().invoke(
        app, ["create", "--os", "linux", "--gpu", key, "--yes",
              "--config", str(path)])

    assert result.exit_code == 2, result.output
    assert "cannot bring up" in result.output
    assert "GPU System Processor" in result.output
    assert path.read_text(encoding="utf-8") == before, "it wrote to the host list"


# --- nothing advertises a card the provisioning cannot drive ---------------


def test_the_unknown_card_list_offers_only_drivable_ones():
    """`This tool can create: ...` is read as "ask for one of these"."""
    with pytest.raises(LifecycleError) as raised:
        card_for("rtx4090")
    message = str(raised.value)
    listed = message[message.index("can create:") + len("can create:"):]
    offered = {name.strip() for name in listed.strip().rstrip(".").split(",")}
    assert offered == set(drivable_cards()), (
        f"the refusal offers {sorted(offered)}; this tool will create "
        f"{drivable_cards()}"
    )


def _gpu_help() -> str:
    """The `--gpu` help as declared, not as rendered.

    Read off the click command rather than out of `--help` output, because the
    rendered version is wrapped and decorated and a test that greps it is really
    testing the terminal width it happened to run at.
    """
    import typer

    create = typer.main.get_command(app).commands["create"]  # type: ignore[attr-defined]
    gpu = next(param for param in create.params if param.name == "gpu")
    return gpu.help or ""


def test_the_gpu_help_names_every_drivable_card_and_no_other():
    """`--help` is a card list like any other, and it used to end in `a100...`,
    which a reader completes from `quota list` — with a card that cannot work."""
    line = _gpu_help()
    assert line, "--gpu has no help at all"
    for key in drivable_cards():
        assert key in line, f"--gpu help does not mention {key}"
    for key in undrivable_cards():
        assert key not in line, f"--gpu help offers {key}, which create refuses"
    assert "..." not in line, "the list is complete, so it does not trail off"


def test_a_pre_turing_card_still_resolves_by_name():
    """It stays in the table on purpose. `--gpu p100` has to be answered with the
    truth about the P100, not with `no card called 'p100'` — which would send
    somebody looking for a spelling that does not exist."""
    for key in PRE_TURING:
        assert card_for(key).key == key


def test_a_quota_name_finds_its_card():
    """`quota list` speaks Google's friendly names, and they are not the `--gpu`
    spellings. `card_named` is what joins the two, and every surface that marks a
    row as undrivable goes through it."""
    assert card_named("P100") is CARDS["p100"]
    assert card_named("V100") is CARDS["v100"]
    assert card_named("H100") is CARDS["h100"], "metered without the 80GB"
    assert card_named("H100-80GB") is CARDS["h100"]
    assert card_named("A100-80GB") is CARDS["a100-80gb"]
    assert card_named("L40S") is None, "a card this tool has never heard of"
    assert card_named("any (global)") is None, "the project ceiling is not a card"


# --- the driver install is what makes all of the above true ----------------


def test_the_driver_install_is_still_the_open_module():
    """READ THIS BEFORE CHANGING THE STARTUP SCRIPT.

    Every refusal in this file rests on one fact: the Linux startup script runs
    Google's `cuda_installer.pyz install_driver`, which lays down the open kernel
    module and takes no flavour switch. If that changes — a proprietary install,
    a pinned legacy branch, a different installer — then which cards can work
    changes with it, and `GSP_ARCHITECTURES` is no longer the whole answer.

    So this test exists to fail at that moment. It is not asserting that the
    script is correct; it is asserting that nobody has changed the thing the card
    table depends on without coming back here.
    """
    assert "cuda_installer.pyz" in LINUX_DRIVER
    assert "install_driver" in LINUX_DRIVER
    assert "--installation-branch" not in LINUX_DRIVER, (
        "the driver branch is now pinned, so which cards this tool can drive is "
        "no longer decided by GSP alone — re-read create.GSP_ARCHITECTURES"
    )
