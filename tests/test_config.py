"""The host list is the one thing that is fully checkable without a network.

Each test here corresponds to a mistake that has actually cost time: reaching the
wrong 8188, two hosts fighting over a port, or a cloud host declared without
enough detail to find it.
"""

from __future__ import annotations

import pytest

from comfy_qa.config import ConfigError, find, parse

GCE = {
    "kind": "gce",
    "os": "Ubuntu 22.04",
    "gpu": "L4",
    "gce_instance": "comfy-linux",
    "gce_zone": "us-central1-a",
    "gce_project": "proj",
    "port": 8190,
}


def test_local_defaults_to_comfyui_port():
    (host,) = parse({"hosts": {"local": {"kind": "local"}}})
    assert host.port == 8188
    assert host.url == "http://127.0.0.1:8188"
    assert not host.is_remote


def test_gce_host_round_trips():
    (host,) = parse({"hosts": {"comfy-linux": dict(GCE)}})
    assert host.is_remote
    assert host.gce_zone == "us-central1-a"
    assert host.url == "http://127.0.0.1:8190"


def test_cloud_host_may_not_claim_the_local_comfyui_port():
    with pytest.raises(ConfigError, match="reserved for the local ComfyUI"):
        parse({"hosts": {"box": dict(GCE, port=8188)}})


def test_two_hosts_may_not_share_a_port():
    with pytest.raises(ConfigError, match="both use port 8190"):
        parse({"hosts": {
            "a": dict(GCE, gce_instance="a"),
            "b": dict(GCE, gce_instance="b"),
        }})


def test_gce_host_needs_enough_detail_to_be_found():
    incomplete = {k: v for k, v in GCE.items() if k != "gce_zone"}
    with pytest.raises(ConfigError, match="gce_zone"):
        parse({"hosts": {"box": incomplete}})


def test_gce_host_needs_an_explicit_port():
    no_port = {k: v for k, v in GCE.items() if k != "port"}
    with pytest.raises(ConfigError, match="explicit port"):
        parse({"hosts": {"box": no_port}})


@pytest.mark.parametrize("kind", ["vm", "", None, "GCE"])
def test_kind_is_closed(kind):
    with pytest.raises(ConfigError, match="kind must be"):
        parse({"hosts": {"box": {"kind": kind, "port": 8190}}})


def test_typos_are_rejected_not_ignored():
    with pytest.raises(ConfigError, match="unknown field"):
        parse({"hosts": {"local": {"kind": "local", "gce_zoen": "x"}}})


def test_a_misspelt_field_is_named_before_the_ones_it_makes_look_missing():
    """`gce_zoen` reported "kind 'gce' requires os, gpu, gce_instance, gce_zone,
    gce_project" — five fields that are all present, and never the misspelt one
    that is the actual cause."""
    typo = {k: v for k, v in GCE.items() if k != "gce_zone"}
    typo["gce_zoen"] = "us-central1-a"

    with pytest.raises(ConfigError) as raised:
        parse({"hosts": {"box": typo}})

    message = str(raised.value)
    assert "gce_zoen" in message
    assert "did you mean 'gce_zone'" in message
    assert "requires" not in message, "the typo is still buried under the missing list"


def test_a_field_with_no_near_match_still_names_the_ones_that_exist():
    with pytest.raises(ConfigError) as raised:
        parse({"hosts": {"local": {"kind": "local", "colour": "red"}}})

    message = str(raised.value)
    assert "'colour'" in message
    assert "Known fields:" in message and "gce_zone" in message


def test_port_must_be_a_real_port():
    with pytest.raises(ConfigError, match="outside 1024-65535"):
        parse({"hosts": {"box": dict(GCE, port=80)}})


def test_empty_config_is_an_error_not_an_empty_list():
    with pytest.raises(ConfigError, match="no \\[hosts"):
        parse({"hosts": {}})


def test_find_names_the_alternatives():
    hosts = parse({"hosts": {"local": {"kind": "local"}}})
    assert find(hosts, "local").name == "local"
    with pytest.raises(ConfigError, match="declared:  local"):
        find(hosts, "nope")


def test_bare_host_accepts_config_like_every_other_command(tmp_path):
    """`comfy-qat --config x` was a usage error while `host list --config x` worked.

    Found by running the end-to-end criteria rather than reasoning about them:
    the default command is the one people reach for first, so it is the worst
    place to have an option that only looks like it is there.
    """
    from typer.testing import CliRunner

    from comfy_qa.host import app

    path = tmp_path / "hosts.toml"
    path.write_text('[hosts.only]\nkind = "local"\nport = 8188\n', encoding="utf-8")

    result = CliRunner().invoke(app, ["--config", str(path)])
    assert result.exit_code == 0, result.output
    assert "only" in result.output


@pytest.mark.parametrize("empty", [
    {},
    {"hosts": {}},
    {"not_hosts": {"local": {"kind": "local", "port": 8188}}},
])
def test_a_host_list_with_no_hosts_says_what_to_do_about_it(empty):
    """Refusing is deliberate; saying nothing about it was not.

    troubleshooting.md has always carried the reasoning — a tool that silently
    operates nothing is worse than one that stops — and the message was four
    words with no way out of them. Every sibling refusal in `config.py` names
    one: the missing file says `init`, the unreadable file says check it is not
    a directory.

    This is pinned HERE rather than left to the docs guard, and the reason is
    worth the lines. Shortening the message back does not fail that guard: the
    forward check is parametrised over the package's messages, so the case
    VANISHES instead of failing (679 to 678, nothing red), and the reverse check
    passes because the short message is still a substring of the entry heading.
    Two guards, neither able to see the change, which is the shape this suite
    keeps finding. An assertion on the text can fail.
    """
    with pytest.raises(ConfigError) as raised:
        parse(empty)

    message = str(raised.value)
    assert "no [hosts.<name>] tables found" in message
    assert "[hosts.local]" in message, "it names the shape of what is missing"

    # THE ASSERTION THAT MATTERS, and it is the inverse of the one that was
    # here. An earlier version of this message offered `comfy-qat init --force`,
    # and that was wrong for a reason no test was watching for: `parse` also
    # validates a CANDIDATE REWRITE for `hostfile.apply`, and is quoted verbatim
    # into its refusal. A `delete` that empties the host list therefore printed
    # "run init --force" directly above remove.py's "take the table out by
    # hand" — two remedies, disagreeing, the overwriting one first, while the
    # file was still intact and still held the user's own comments.
    #
    # So this pins the ABSENCE of destructive advice. It is the only kind of
    # assertion that could have caught it: the message read perfectly well.
    assert "--force" not in message, (
        "this text is quoted into hostfile.apply's refusal, where the file is "
        "intact and must not be overwritten — nothing here may suggest it"
    )
