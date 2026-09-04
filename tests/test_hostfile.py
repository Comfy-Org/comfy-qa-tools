"""Rewriting the host list without losing it.

Every other write to hosts.toml appends, which cannot lose anything. This one
rewrites, and the file is hand-maintained, carries comments, and names every
machine the user can reach — a truncated hosts.toml is worse than any move
failure, because after it no command works at all.

So these tests are about the failure, not the feature.
"""

from __future__ import annotations

import tomllib

import pytest

from comfy_qa.hostfile import HostFileError, apply, rename_and_add, without

HOSTS = """\
# my machines — this comment must survive
[hosts.local]
kind = "local"
port = 8188

[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-c"
gce_project  = "proj"
port         = 8192

# the windows one
[hosts.comfy-win]
kind         = "gce"
os           = "Windows Server 2022"
gpu          = "L4"
gce_instance = "comfy-win"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8190
"""

ADDED = """
[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8192
"""


def renamed():
    return rename_and_add(HOSTS, name="comfy-linux",
                          renamed="comfy-linux-us-central1-c",
                          renamed_port=8193, added=ADDED)


def test_the_moved_box_keeps_its_name_and_its_port():
    parsed = tomllib.loads(renamed())["hosts"]
    assert parsed["comfy-linux"]["port"] == 8192
    assert parsed["comfy-linux"]["gce_zone"] == "us-central1-a"


def test_the_old_box_stays_reachable_under_a_name_that_says_where_it_is():
    """It exists in GCE and bills until deleted. Dropping it from the list would
    make it unstoppable by this tool, which is a money bug dressed as tidying."""
    parsed = tomllib.loads(renamed())["hosts"]
    old = parsed["comfy-linux-us-central1-c"]
    assert old["gce_zone"] == "us-central1-c"
    assert old["port"] == 8193, "the canonical port went to the new box"


def test_unrelated_hosts_and_comments_survive():
    out = renamed()
    assert "# my machines — this comment must survive" in out
    assert "# the windows one" in out
    parsed = tomllib.loads(out)["hosts"]
    assert parsed["comfy-win"]["port"] == 8190
    assert parsed["local"]["port"] == 8188


def test_renaming_a_host_that_is_not_there_is_refused():
    with pytest.raises(HostFileError, match="not in the host list"):
        rename_and_add(HOSTS, name="nope", renamed="nope-x",
                       renamed_port=8199, added=ADDED)


def test_a_block_with_no_port_line_is_refused_rather_than_silently_colliding():
    text = "[hosts.comfy-linux]\nkind = \"gce\"\n"
    with pytest.raises(HostFileError, match="no port line"):
        rename_and_add(text, name="comfy-linux", renamed="comfy-linux-z",
                       renamed_port=8193, added=ADDED)


# --- apply(): nothing reaches the file unless it is right --------------------

def test_a_result_that_would_not_parse_never_lands(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    with pytest.raises(HostFileError, match="would not parse"):
        apply(path, "[hosts.broken\n", expect={"x"})
    assert path.read_text(encoding="utf-8") == HOSTS, "the original was touched"


def test_a_result_missing_a_host_never_lands(tmp_path):
    """The failure this check exists for: a transform that quietly drops a box."""
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    with pytest.raises(HostFileError, match="missing comfy-win"):
        apply(path, "[hosts.local]\nkind = \"local\"\nport = 8188\n",
              expect={"local", "comfy-win"})
    assert path.read_text(encoding="utf-8") == HOSTS


def test_a_good_rewrite_lands_and_keeps_a_copy_of_what_was_there(tmp_path):
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    apply(path, renamed(),
          expect={"local", "comfy-win", "comfy-linux", "comfy-linux-us-central1-c"})

    assert tomllib.loads(path.read_text(encoding="utf-8"))["hosts"]["comfy-linux"][
        "gce_zone"] == "us-central1-a"
    backup = path.with_name("hosts.toml.bak")
    assert backup.exists() and backup.read_text(encoding="utf-8") == HOSTS


def test_no_temp_file_is_left_beside_the_host_list(tmp_path):
    """A stray .tmp next to hosts.toml is the kind of thing somebody later has to
    make a decision about."""
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    apply(path, renamed(),
          expect={"local", "comfy-win", "comfy-linux", "comfy-linux-us-central1-c"})
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []


# --- the file that parses, counts right, and still bricks the tool -----------
#
# Found by adversarial testing, and it is the worst thing this module can do:
# names all correct, TOML valid, and `config.load` then refuses it — after which
# no comfy-qat command works until somebody hand-edits the file. Worse, register()
# runs at the REGISTER step, so the box is already moved, running and billing, and
# `down` can no longer reach it.

MOVED_ONCE = """\
[hosts.local]
kind = "local"
port = 8188

[hosts.comfy-linux-us-central1-c]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-c"
gce_project  = "proj"
port         = 8194

[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8192
"""

COMING_HOME = """
[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-c"
gce_project  = "proj"
port         = 8192
"""


def test_moving_a_box_back_where_it_came_from_is_refused_not_written(tmp_path):
    """A stockout pushes the box out of us-central1-c; capacity returns; you move
    it home. The retired entry still names instance `comfy-linux` in zone c, and
    so does the one coming home. Two names, one machine — which the loader
    rejects, correctly, because reading results from the wrong box is the thing
    this tool exists to prevent."""
    path = tmp_path / "hosts.toml"
    path.write_text(MOVED_ONCE, encoding="utf-8")

    text = rename_and_add(MOVED_ONCE, name="comfy-linux",
                          renamed="comfy-linux-us-central1-a",
                          renamed_port=8195, added=COMING_HOME)

    # It parses, and every name is exactly what was expected. That is why the
    # old check waved it through.
    assert tomllib.loads(text)
    expect = {"local", "comfy-linux-us-central1-c",
              "comfy-linux-us-central1-a", "comfy-linux"}
    assert set(tomllib.loads(text)["hosts"]) == expect

    with pytest.raises(HostFileError, match="would not load"):
        apply(path, text, expect=expect)
    assert path.read_text(encoding="utf-8") == MOVED_ONCE, "it was written anyway"
    assert not path.with_name("hosts.toml.bak").exists(), (
        "a refused write must not clobber the backup"
    )


def test_what_the_loader_refuses_this_refuses_to_write(tmp_path):
    """The rule, stated once: validation here is the real loader, not a proxy."""
    path = tmp_path / "hosts.toml"
    path.write_text(HOSTS, encoding="utf-8")
    same_machine = HOSTS + """
[hosts.a-different-name]
kind         = "gce"
os           = "Windows Server 2022"
gpu          = "L4"
gce_instance = "comfy-win"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8199
"""
    with pytest.raises(HostFileError, match="would not load"):
        apply(path, same_machine,
              expect={"local", "comfy-linux", "comfy-win", "a-different-name"})
    assert path.read_text(encoding="utf-8") == HOSTS


# --- removing a host must not take the next one's comments ------------------
#
# `without` ran from the block header to the next `[`, so everything between the
# end of the deleted block and that bracket went too — which in a hand-maintained
# file is exactly where the NEXT host's comments live. Every guard passed: the
# file parsed, the host names matched `expect` exactly, config.parse accepted it.
# The name check cannot see it, because no name is lost. `delete` ended in "and it
# is out of your host list" — unqualified success, having destroyed a line that
# said DO NOT DELETE.

ANNOTATED = """\
[hosts.comfy-win]
kind         = "gce"
gce_instance = "comfy-win"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8190

# comfy-linux holds the 70B checkpoint and the PM-630 fixtures.
# DO NOT DELETE. Ali, 2026-08-14.
[hosts.comfy-linux]
kind         = "gce"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-c"
gce_project  = "proj"
port         = 8192
"""


def test_removing_a_host_keeps_the_next_one_s_comments():
    out = without(ANNOTATED, "comfy-win")

    assert "DO NOT DELETE" in out, "a warning the user wrote was destroyed"
    assert "70B checkpoint" in out
    assert "comfy-win" not in tomllib.loads(out)["hosts"]
    assert "comfy-linux" in tomllib.loads(out)["hosts"]


def test_removing_the_last_host_keeps_the_ones_above_it():
    out = without(ANNOTATED, "comfy-linux")

    assert "comfy-linux" not in tomllib.loads(out)["hosts"]
    assert tomllib.loads(out)["hosts"]["comfy-win"]["port"] == 8190
    assert "DO NOT DELETE" not in out, (
        "that comment belonged to the block being removed"
    )


def test_removing_a_host_with_no_blank_line_after_it_still_works():
    """Not every file is spaced out; the block simply ends at the next header."""
    tight = ("[hosts.a]\nkind = \"local\"\nport = 8188\n"
             "[hosts.b]\nkind = \"local\"\nport = 8189\n")
    out = without(tight, "a")
    assert set(tomllib.loads(out)["hosts"]) == {"b"}


def test_a_comment_directly_above_the_removed_block_goes_with_it():
    """It describes the host being deleted, so keeping it would strand a note
    about a machine that no longer exists."""
    src = ("[hosts.a]\nkind = \"local\"\nport = 8188\n\n"
           "# b is the windows one\n[hosts.b]\nkind = \"local\"\nport = 8189\n")
    assert "# b is the windows one" not in without(src, "b")


# --- the mirror, and CRLF -----------------------------------------------------
#
# The first fix stopped `without` taking the NEXT host's comments. It did not stop
# it taking the PREVIOUS host's, and that is the shape that hits a real file: the
# header pattern used `\s`, which matches newlines, so the match began on the blank
# line above the header and swallowed the very terminator the comment walk depends
# on. Three agents reproduced it independently, and on the real hosts.toml it
# destroyed all eight lines of the commented-out example `init` writes.

MIRROR = """\
[hosts.comfy-win]
kind = "gce"
port = 8190
# comfy-win is the PM-630 box. Keep the port, Linear links to it.

[hosts.comfy-linux]
kind = "gce"
port = 8191
"""


def test_deleting_a_host_keeps_the_previous_one_s_trailing_note():
    out = without(MIRROR, "comfy-linux")
    assert "PM-630 box" in out, "a note belonging to a host we did not touch"
    assert set(tomllib.loads(out)["hosts"]) == {"comfy-win"}


def test_the_same_holds_with_crlf_line_endings():
    """`[ \\t]*$` still fails on CRLF, because `$` matches before the \\n and the
    line ends in \\r. Getting that wrong reinstates the original defect in full."""
    out = without(MIRROR.replace("\n", "\r\n"), "comfy-linux")
    assert "PM-630 box" in out
    assert set(tomllib.loads(out)["hosts"]) == {"comfy-win"}


def test_the_real_host_lists_worked_example_survives_a_delete():
    """`init` writes a commented-out [hosts.comfy-linux] block into every file as
    a worked example. It sits directly above a real host, so it is exactly what
    the mirror destroys."""
    src = ("# a cloud host may never use 8188; that is the local ComfyUI's\n\n"
           "[hosts.local]\nkind = \"local\"\nport = 8188\n\n"
           "# [hosts.comfy-linux]\n# kind         = \"gce\"\n# port         = 8190\n\n"
           "[hosts.comfy-win]\nkind = \"gce\"\nport = 8191\n")
    out = without(src, "comfy-win")

    assert out.count("#") == src.count("#"), "a commented example was destroyed"
    assert "may never use 8188" in out
    assert set(tomllib.loads(out)["hosts"]) == {"local"}


def test_deleting_the_first_host_keeps_the_file_s_own_preamble():
    """The comment walk ran off the top of the file: with only comments above the
    header there is no blank line to stop it, so the whole preamble went.

    A run at offset 0 is either the file's preamble or the first host's own note,
    and nothing can tell them apart from the text — the obvious heuristic passes
    six cases and destroys a legitimate seventh. So this takes the asymmetry the
    rest of the tool takes about money and applies it to data: for something that
    cannot be undone, be wrong in the direction that leaves something behind.
    """
    src = ("# my machines - hand maintained, mind the comments\n"
           "# ports are allocated in order; do not reuse 8190\n"
           "[hosts.comfy-win]\nkind = \"gce\"\nport = 8190\n\n"
           "[hosts.local]\nkind = \"local\"\nport = 8188\n")
    out = without(src, "comfy-win")

    assert "hand maintained" in out, "the file's preamble was destroyed"
    assert "do not reuse 8190" in out
    assert set(tomllib.loads(out)["hosts"]) == {"local"}


def test_a_note_below_the_top_still_goes_with_its_own_host():
    """The rule only applies at offset 0. Anywhere else, a comment directly above
    a block describes that block and goes with it."""
    src = ("[hosts.local]\nkind = \"local\"\nport = 8188\n\n"
           "# comfy-win is the PM-630 box\n"
           "[hosts.comfy-win]\nkind = \"gce\"\nport = 8190\n")
    assert "PM-630" not in without(src, "comfy-win")


# --- the fixtures were kinder than a real file ------------------------------
#
# Every test above uses a host list written for this file. A move against the
# REAL hosts.toml on this machine failed while all of them passed, because the
# port pattern's trailing `\s*` ate the newline and welded the port line to the
# next section header:
#
#     port         = 8195[hosts.comfy-linux-a]
#
# `apply` then refused the write, correctly, and `move` failed. The suite was
# green throughout. So this fixture is deliberately shaped like a file somebody
# maintains rather than one written to pass.

LIVED_IN = """\
# comfy-qat host list.
#
# Rules the tool enforces:
#   - every host needs its own port
#   - a cloud host may never use 8188; that is the local ComfyUI's

[hosts.local]
kind = "local"
port = 8188

# [hosts.comfy-linux]
# kind         = "gce"
# port         = 8190

[hosts.comfy-linux]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux"
gce_zone     = "us-central1-c"
gce_project  = "proj"
port         = 8192
[hosts.comfy-linux-a]
kind         = "gce"
os           = "Ubuntu 22.04"
gpu          = "L4"
gce_instance = "comfy-linux-a"
gce_zone     = "us-central1-a"
gce_project  = "proj"
port         = 8193
"""


def test_a_move_against_a_lived_in_file_produces_valid_toml():
    out = rename_and_add(LIVED_IN, name="comfy-linux",
                         renamed="comfy-linux-us-central1-c",
                         renamed_port=8195,
                         added="\n[hosts.comfy-linux]\nkind = \"gce\"\n"
                               "gce_instance = \"comfy-linux\"\n"
                               "gce_zone = \"us-central1-a\"\n"
                               "gce_project = \"proj\"\nport = 8192\n")

    hosts = tomllib.loads(out)["hosts"]
    assert hosts["comfy-linux"]["gce_zone"] == "us-central1-a"
    assert hosts["comfy-linux"]["port"] == 8192
    assert hosts["comfy-linux-us-central1-c"]["port"] == 8195
    assert hosts["comfy-linux-a"]["port"] == 8193, "the next host was welded on"
    assert out.count("#") == LIVED_IN.count("#")


def test_removing_from_a_lived_in_file_keeps_everything_else():
    out = without(LIVED_IN, "comfy-linux-a")
    assert set(tomllib.loads(out)["hosts"]) == {"local", "comfy-linux"}
    assert out.count("#") == LIVED_IN.count("#")
